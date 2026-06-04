# Matprep, IAR, and Latch Sub-Slots

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). Other versions differ.*

## Abstract

Before the systolic array of the [MXU slot](slot-mxu.md) can clock a single multiply step it must be *fed*. Feeding the array is three distinct jobs that this page covers, all running through the same `VectorExtended` bundle slot the matmul itself uses: **matprep** stages the moving operand (activations) and prepares the stationary gain matrix; the **IAR** (Index Address Register) is the per-TensorCore index-register file that drives indexed (gather) memory access into the operand pool; and the **latch** ops load the prepared gain matrix into the array's weight banks. The matmul step itself is the visible op, but the latch/matprep/IAR machinery is what makes it correct and cheap.

If you have read the LLVM NVPTX backend, the closest analog to the latch is the implicit register-file write that precedes a `wgmma` accumulator group, and the closest analog to the IAR is a base+index addressing mode — except the TPU exposes the index register *as an architectural object* (`IAR0`/`IAR1`) that a `SetIar` op explicitly loads and a `VectorLoadIndexed` op implicitly consumes. There is no per-element index operand; the IAR *is* the index. The latch is weight-stationary in the same sense `wgmma` is accumulator-stationary: one latch amortizes across many matmul steps.

The page is built in three units that mirror those three jobs, plus the per-gen cost-model treatment that ties them together. Each unit names the builder function that constructs the op, the exact `LloInstruction` field offsets it writes, and the per-gen field/count tables. Every offset and every perf-row ordinal below was cross-checked against the IDA decompile of the named `sub_ADDR`; the verification status is in the Confidence columns.

For reimplementation, the contract is:

- The **IAR field layout**: a 64-bit value at `LloInstruction+0x50→+0x6c`, low 32 bits = index, bit 32 = present; built by `CreateVectorSetIarHelper`, bounded by `IarsPerTensorCore` (= 2 on every gen). The Lane/Sublane/Raw split lives in the ISA bundle-slot opcode, not the value.
- The **latch-op family** (`0x8d..0x96`) and its `LloInstruction` field map: GainLatchMode `@+0x40`, latch index `@+0x42`, MSR `@+0x44` (opcode-multiplexed), unit-id / MXU-quadrant + source-bus packed into the control word `@+0x0b`.
- The **matprep mechanism per gen**: GL/GF give each matprep variant a fixed binary-search perf row; VF folds it into the matmul-format table plus a modifier-keyed reservation; PF folds it into the latch ops; JF folds the transpose-of-gains into the matmul opcode.
- The **first-latch overrun handshake** (`GainLatchModeHasOverrunChecks`, vtable `+0x358`) that gates whether the first latch in a sequence is indexed — non-trivial only on Viperfish.

| | |
|---|---|
| **Latch op family** | `kVectorLatchLsf` `0x8d` … `kVectorLatch3Msk` `0x96` (10 opcodes) |
| **Matprep op family** | `kVectorMatprepSubr` `0x97`/`0x98`, `kVectorMatprepMubr` `0x99`/`0x9a` |
| **IAR op family** | `kVectorReadIar` `0x01`, `kVectorSetIarLane`/`Raw`/`Sublane` `0x02`/`0x03`/`0x04` |
| **IAR value field** | `QWORD[LloInstruction+0x50] → +0x6c`; bit 32 present, low 32 index |
| **IarsPerTensorCore** | `DWORD[Target+0x4a8]` = **2** all gens (`IAR0`/`IAR1`) |
| **Latch builder** | `LloInstruction::CreateVectorLatchLsf` `sub_1D4D7AA0`, `CreateVectorLatchHelper` `sub_1D4D8360` |
| **IAR setter** | `LloInstruction::CreateVectorSetIarHelper` `sub_1D4DF080` |
| **Latch sequencing** | `MxuAssigner::SetLatchIndices` `sub_10F3B4C0`, `LatchLhs` `sub_10F3B5E0` |
| **Sequence record** | `MxuSequence`, sizeof `0x78`, five `{ptr,count,cap}` lists |
| **Home slot** | `VectorExtended` (shared with matmul); matres → `VectorResult` |

---

## The IAR — Index Address Register

### Purpose

The IAR is the TensorCore's gather-index register. A `VectorLoadIndexed` op reads the operand pool at `base + IAR * stride`, but it carries **no explicit index field** — the IAR register *is* the per-element gather index. To use it, a `SetIar` op loads an index value into one of two architectural registers (`IAR0` / `IAR1`); a later `VectorLoadIndexed0` / `Indexed1` consumes the chosen register; a `ReadIar0` / `ReadIar1` drains it back into a vector register. This is the addressing primitive underneath the SparseCore/TensorCore embedding-gather path. It is distinct from the SparseCore TEC `TileSpmemLoadIndexed`, which *does* carry an explicit per-element `Index` VREG.

### IAR Value Field Layout

The IAR value lives on the `LloInstruction` "modifier" sub-object at `+0x50` (the same sub-object that holds `precision_type` at `+0x68`). `iar()` reads it as a single 64-bit field:

```c
function LloInstruction::iar(this):       // sub_1D4E7120
    sub = *(QWORD*)(this + 0x50);          // the modifier sub-object
    if (sub == nullptr) return 0;          // no IAR present
    return *(QWORD*)(sub + 0x6c);          // 64-bit packed field
```

The builder is `CreateVectorSetIarHelper`. It writes the index into the low 32 bits and a present-byte into byte 4 of the same qword, which sets bit 32:

```c
function CreateVectorSetIarHelper(opcode, iar_value, source, region):   // sub_1D4DF080
    // bound the index against the per-TensorCore register count
    check(iar_value < Target::IarsPerTensorCore())    // sub_1D617280 → DWORD[Target+0x4a8]
    check(opcode_produced_register_type[source.opcode] == 4)  // source must be a VREG producer
    op = LloInstruction::New(opcode, {source}, region)
    sub = op + 0x50                       // allocate the 0xf0 modifier sub-object if absent (zeroed)
    *(DWORD*)(sub + 0x6c) = iar_value      // sub_1D4DF169 — low 32 bits = the index value
    *(BYTE *)(sub + 0x70) = 1              // sub_1D4DF16C — byte 4 of the qword ⇒ BIT 32 = present
    return op
```

So `iar() == (1u64 << 32) | iar_value`. The "IAR present" bit is bit 32; the index value is the low 32 bits. `CreateVectorSetIarRaw` (`sub_1D4DF260`) is simply `CreateVectorSetIarHelper(opcode = 0x03, …)`; all of `SetIarLane`/`SetIarSublane`/`SetIarRaw` route through the one helper.

> **QUIRK —** the Lane / Sublane / Raw distinction is *not* in the 64-bit value. The value field is identical across all three forms. The hardware write mode is encoded in the ISA bundle-slot opcode (see below); the LLO-opcode number (`0x02`/`0x03`/`0x04`) and the bundle-slot opcode (2/3/4) **do not line up**: LLO `SetIarRaw = 0x03` maps to slot opcode 4, and LLO `SetIarSublane = 0x04` maps to slot opcode 3. A reimplementation that assumes LLO-opcode == slot-opcode crosses the Sublane and Raw encodings.

### Bundle-Slot Encoding (PxC / TensorCoreVectorStore)

At the ISA level the three `SetIar` forms differ only in a 5-bit opcode subfield of `word@0x18`; their operand accessors are byte-identical. The IAR register select is a single bit, which is what fixes `IarsPerTensorCore` at 2.

> **NOTE — bit numbering.** Every absolute bit position on this page is **LSB-first**, matching the universal v5+ packer convention documented on [Bundle Model](bundle-model-overview.md#abstract): bit 0 is the least-significant bit of byte 0, so `word@0x18 bit 13` is bit 13 of the 8-byte little-endian word at byte `0x18`, and the predicate-mask `word@0x18 & 0x3e0000000` selects the five bits 33..37 of that same word. There is no MSB-first ordering anywhere in the encode/decode path.

| Form | Matches predicate (`word@0x18 & 0x3e0000000`) | slot opcode | accessor `sub_ADDR` | Confidence |
|---|---|---|---|---|
| `SetIarLane` | `== 0x40000000` | 2 | `sub_1EE390E0` | CERTAIN |
| `SetIarSublane` | `== 0x60000000` | 3 | `sub_1EE39100` | CERTAIN |
| `SetIarRaw` | `== 0x80000000` | 4 | `sub_1EE39120` | CERTAIN |

| Field | Bit position | Width | Confirmed accessor | Confidence |
|---|---|---|---|---|
| `IarField` (which IAR register) | `word@0x18` bit 13 | 1 bit (`IAR0`/`IAR1`) | `sub_1EE3B380` (`>>13 & 1`) | CERTAIN |
| `VsrcField` (source VREG) | `byte@0x1b` | 5 bits | `sub_1EE3B360` (`& 0x1f`) | CERTAIN |

The read/use side lives in the `TensorCoreVectorLoad` family (major opcode `word@0x10` bits[60:62]=7), where a 2-bit subfield at `word@0x18` bit 11 selects the form: `VectorLoadIndexed0` = 0, `VectorLoadIndexed1` = 1, `ReadIar0` = 2 (`ReadIar1` is the complement form, not bit-resolved to a single integer — LOW). The indexed-load carries `DestVreg` (6-bit @bit5), `Stride` (4-bit @byte0x17), a cross-word 2-bit `BaseAddress`, and a 4-bit `SublaneMask` — but no `Index` field.

### IarsPerTensorCore — the Register Count

`IarsPerTensorCore()` is a one-instruction accessor, and the value is **not** a code constant:

```c
function Target::IarsPerTensorCore(this):   // sub_1D617280
    return *(uint32*)(this + 0x4a8);
```

The sole writer of `Target+0x4a8` is the shared `Target::Init` (`sub_1D60FC20`) — no per-gen `*Target` constructor writes it. `Init` reads it from the `VectorIsa` field 7 of the embedded per-gen `*_chip_parts.binarypb` proto (loaded at runtime via `embed://tpu_chip_parts/<version>_chip_parts.binarypb`). Extracted from the embedded blobs, the value is **2 on every generation** — the IAR file is gen-stable, matching the 1-bit `IarField` ceiling.

| Gen (codename) | IarsPerTensorCore (`Target+0x4a8`) | source | Confidence |
|---|---|---|---|
| v2 Jellyfish / v3 Dragonfish | 2 | `jellyfish`/`dragonfish` blob `VectorIsa.f7` | HIGH |
| v4 Pufferfish | 2 | `pufferfish` blob | HIGH |
| v5p Viperfish | 2 | `viperfish` blob | HIGH |
| v6e Ghostlite | 2 | `ghostlite` blob | HIGH |
| v7x (6acc60406) | 2 | `6acc60406` blob | HIGH |

> **NOTE —** the value is data, not code, so the version pin above is necessary but not sufficient: a future chip whose `VectorIsa.f7` differs would widen the `IarField`. The 1-bit `IarField` accessor (`sub_1EE3B380`) and the count of 2 are mutually consistent *for this binary's chip-parts blobs only*. See [Chip-Parts Binarypb](../targets/chip-parts-binarypb.md) for the load path.

### IAR Cost — the Perf-Row Sentinel Split

The cost classifier keys the indexed-memory perf row on a sentinel `S = ((iar & 0x1ffffffff) == 0x100000000)` — i.e. *IAR present and index value zero* (an aligned base gather, the cheaper row) versus a nonzero index value (the offset-add row). The seven IAR-class arms of the Ghostlite classifier `GetGhostliteInstruction` (`sub_1C8B1740`) are byte-exact below; the four `ReadIar`/`SetIar` arms additionally `FATAL` (`"iar.has_value()"`) unless bit 32 is set, while the three indexed-memory arms (`LoadIndexed`, `StoreIndexed`, `StoreIndexedMsk`) read `iar()` unconditionally and tolerate a missing IAR.

```c
// the IAR-class arms of GetGhostliteInstruction, verified in the decompile
case 0x01 ReadIar:        if (!(iar & 1<<32)) FATAL;  return 2*((u32)iar != 0) + 0x18c;  // 0x18c / 0x18e
case 0x02 SetIarLane:     if (!(iar & 1<<32)) FATAL;  return 0x1d5 - ((u32)iar == 0);     // 0x1d4 / 0x1d5
case 0x03 SetIarRaw:      if (!(iar & 1<<32)) FATAL;  return 0x1d9 - ((u32)iar == 0);     // 0x1d8 / 0x1d9
case 0x04 SetIarSublane:  if (!(iar & 1<<32)) FATAL;  return 0x1d7 - ((u32)iar == 0);     // 0x1d6 / 0x1d7
case 0x32 LoadIndexed:    /* no bit-32 gate */         return 2*((iar & 0x1ffffffff) != S) + 0x188; // 0x188 / 0x18a
case 0x40 StoreIndexed:   /* no bit-32 gate */         return ((iar & 0x1ffffffff) == S) ^ 0x1d1;    // 0x1d0 / 0x1d1
case 0x44 StoreIndexedMsk:/* no bit-32 gate */         return ((iar & 0x1ffffffff) == S) ^ 0x1d3;    // 0x1d2 / 0x1d3
```

| LLO op | name | arm `sub_ADDR` | sentinel-S row | non-S row | Confidence |
|---|---|---|---|---|---|
| `0x01` | `kVectorReadIar` | `sub_1C8B1A94` | `0x18c` (low32==0) | `0x18e` | CERTAIN |
| `0x02` | `kVectorSetIarLane` | `sub_1C8B19DE` | `0x1d4` | `0x1d5` | CERTAIN |
| `0x03` | `kVectorSetIarRaw` | `sub_1C8B1A71` | `0x1d8` | `0x1d9` | CERTAIN |
| `0x04` | `kVectorSetIarSublane` | `sub_1C8B1959` | `0x1d6` | `0x1d7` | CERTAIN |
| `0x32` | `kVectorLoadIndexed` | `sub_1C8B1926` | `0x188` | `0x18a` | CERTAIN |
| `0x40` | `kVectorStoreIndexed` | `sub_1C8B19AD` | `0x1d0` | `0x1d1` | CERTAIN |
| `0x44` | `kVectorStoreIndexedMasked` | `sub_1C8B197C` | `0x1d2` | `0x1d3` | CERTAIN |

The symbolic *enumerator names* for these `GhPerf::Instruction` ordinals are not in the binary (no `ToString`); the ordinals are byte-exact but unnamed (a uniform gap shared with the matmul perf rows).

---

## The Latch — Loading the Gain Matrix

### Purpose

The latch ops load the prepared stationary gain (weight) matrix into the MXU's per-quadrant weight banks. Loading is weight-stationary: one latch amortizes across many matmul steps. *How* the gains are loaded — transpose, dtype packing, byte-plane staging — is the `GainLatchMode` (GLM) operand. The family has ten opcodes: a "load-stationary-from-FIFO" pair (`Lsf`), a plain pair, and three indexed sub-bank pairs.

### Op Family and Builders

Each `LloRegionBuilder::Vlatch*` wrapper routes through an `LloInstruction::Create*` constructor, which routes through either `CreateVectorLatchLsf` (the LSF special case) or `CreateVectorLatchHelper` (general).

| `LloOpcode` | name | builder | constructor | Confidence |
|---|---|---|---|---|
| `0x8d` | `kVectorLatchLsf` | `VlatchLsf` | `CreateVectorLatchLsf` (`sub_1D4D7AA0`) | CERTAIN |
| `0x8e` | `kVectorLatchLsfMsk` | `VlatchLsfMsk` | `CreateVectorLatchLsfMasked` (`sub_1D4D8140`) | CERTAIN |
| `0x8f` | `kVectorLatch` | `Vlatch` | `CreateVectorLatch` (`sub_1D4D8900`) | CERTAIN |
| `0x90` | `kVectorLatchMsk` | `VlatchMsk` | `CreateVectorLatchMasked` (`sub_1D4D8C40`) | CERTAIN |
| `0x91` | `kVectorLatch1` | `Vlatch1` | `CreateVectorLatch1` (`sub_1D4D8940`) | CERTAIN |
| `0x92` | `kVectorLatch1Msk` | `Vlatch1Msk` | `CreateVectorLatch1Masked` (`sub_1D4D8C80`) | CERTAIN |
| `0x93` | `kVectorLatch2` | `Vlatch2` | `CreateVectorLatch2` (`sub_1D4D8A80`) | CERTAIN |
| `0x94` | `kVectorLatch2Msk` | `Vlatch2Msk` | `CreateVectorLatch2Masked` (`sub_1D4D8CC0`) | CERTAIN |
| `0x95` | `kVectorLatch3` | `Vlatch3` | `CreateVectorLatch3` (`sub_1D4D8B60`) | CERTAIN |
| `0x96` | `kVectorLatch3Msk` | `Vlatch3Msk` | `CreateVectorLatch3Masked` (`sub_1D4D8D00`) | CERTAIN |

`VlatchI(value, long idx, glm)` (`sub_1D574580`) dispatches its `long idx` to `Vlatch1` (`0x91`) or `Vlatch2` (`0x93`) — the indexed latch picks a sub-bank. The general `Create*` route through `CreateVectorLatchHelper`.

### LloInstruction Field Layout

The constructed latch op carries its operands in these fields, byte-exact from the setters and their symmetric readers:

| Offset | Field | Setter / reader | Meaning | Confidence |
|---|---|---|---|---|
| `WORD[+0x00]` | `LloOpcode` | `New()` | `0x8d..0x96` | CERTAIN |
| `BYTE[+0x0a]` | `register_number` | `set_register_number` / `sub_1D5A8E20` | gain-source VREG number | CERTAIN |
| `WORD[+0x0b]` | control word | `set_unit_id` / `ValidateAndSetMxuAndSourceBus` | unit-id + source-bus (below) | CERTAIN |
| `BYTE[+0x40]` | `latch_mode` (GLM) | `set_latch_mode` `sub_1D4D7C20` / `latch_mode` `sub_1D4E7500` | the GainLatchMode | CERTAIN |
| `WORD[+0x42]` | `latch_index_in_sequence` | `set_latch_index_in_sequence` `sub_1D4E7960` | assigned by `SetLatchIndices` | CERTAIN |
| `BYTE[+0x44]` | `matrix_staging_register` (Msr) | `set_matrix_staging_register` `sub_1D4D7D40` | latch-bank / MSR destination | CERTAIN |

The control word `WORD[+0x0b]` is two packed bitfields:

```c
// LloValue::set_unit_id (sub_12698C00) — the GMR / MXU-quadrant pack
WORD[v+0x0b] = (WORD[v+0x0b] & 0xf8ff) + ((unit & 3) << 8) + 0x400;   // check unit <= 3
//   bits 8-9   : unit_id = which MXU quadrant (0..3) the gain matrix latches into
//   bit  10    : has-mxu flag (0x400)

// source-bus pack (ValidateAndSetMxuAndSourceBus, sub_1D4D7E80)
WORD[v+0x0b] = (WORD[v+0x0b] & 0xc7ff) + ((bus & 3) << 11) + 0x2000;
//   bits 11-12 : VEX source-bus (0..3)
//   bit  13    : has-source-bus flag (0x2000)
```

> **GOTCHA —** `WORD[op+0x42]` is the latch index for the latch family (`0x8d..0x96`), but `BYTE[op+0x42]` is the MSR for the *load-LMR* family (`0xaa`/`0xab`). They share the byte address but apply to **disjoint** opcode families, so there is no aliasing within one op. A reimplementation that reads `+0x42` without first checking the opcode family will mis-decode one or the other.

The MSR setter is opcode-multiplexed — the same field name lands at four different offsets:

```c
function set_matrix_staging_register(op, msr):   // sub_1D4D7D40
    switch opcode_family(op):
        case 0x9b..0xa5 (matmul):        BYTE[op+0x46] = msr
        case 0x8d..0x96 (latch):         BYTE[op+0x44] = msr   // ← the latch family
        case 0xaa/0xab  (load-LMR):      BYTE[op+0x42] = msr
        case 0xa8       (done-with-gains): BYTE[op+0x41] = msr
        default: FATAL "msr unsupported for opcode"
```

### CreateVectorLatchLsf — the LSF Build Sequence

`CreateVectorLatchLsf` is the canonical latch constructor (`VlatchLsf` is the wrapper that appends it). It guards the gain source and the GLM, then stamps the fields:

```c
function CreateVectorLatchLsf(gain_src, glm, unit_id, region):   // sub_1D4D7AA0
    if (opcode_produced_register_type[gain_src.opcode] != 4)     // gain source must be reg-type 4
        UpdateStatus("chunk->ProducesVreg()")                    // slow diagnostic path otherwise
    if (glm > 0x33 || !bittest(0xf0000003c0c03, glm))            // LSF GLM-validity mask
        FATAL "LSF latch mode not expected."
    op = LloInstruction::New(0x8d /*kVectorLatchLsf*/, {gain_src}, region)
    set_latch_mode(op, glm)                                      // BYTE[op+0x40]
    set_matrix_staging_register(op, 1)                           // BYTE[op+0x44] = 1 (LSF staging slot)
    ValidateAndSetMxuAndSourceBus(unit_id, op)                   // WORD[op+0x0b] unit-id (+ src-bus)
    return op
```

`VprepareForLatch` (`sub_1D573BA0`) runs before the constructor: if the gen does not natively `SupportsGainLatchMode(glm)` (vtable `+0x368`) it rewrites the gain source into a software byte-plane representation before re-checking. The two constructors admit different GLM sets:

| Constructor | GLM-validity mask | Accepts GLM | Confidence |
|---|---|---|---|
| `CreateVectorLatchLsf` | `0xf0000003c0c03` | `{0,1,10,11,18,19,20,21,48,49,50,51}` (bf16, F8E5M2, S8, fp8-conv) | CERTAIN |
| `CreateVectorLatchHelper` | `0xf000003fffc3f` | `{0-5,10-25,48-51}` (full set incl. F8E4M3FN/F32 and nibble fmt7/8) | CERTAIN |

`ValidateAndSetMxuAndSourceBus` (`sub_1D4D7E80`) bounds the MXU id (`>= 0`, `< MxusPerTensorCore()` = `Target+0x4ac`), stamps the unit-id, and — only if `HasVexSourceBuses()` (vtable `+0x408`, **true only on Pufferfish**) and `LloOpcodeUsesSourceBus(op)` (true for `0x8f..0x96`, false for the `0x8d`/`0x8e` LSF forms) — stamps the source-bus. So the VEX source-bus field is populated only on Pufferfish (v4), and only for the non-LSF latch ops.

### The First-Latch Overrun Handshake

`SetLatchIndices` assigns each latch op in a sequence its program-order index, but the **first** latch is indexed only when its GLM carries overrun checks. The gate is the per-gen `GainLatchModeHasOverrunChecks` (vtable `+0x358`):

```c
function SetLatchIndices(span<MxuSequence*>):    // sub_10F3B4C0
    for each seq in span:
        for idx = 0 .. seq.latches.count - 1:        // latches list @ seq+0x18, count @ seq+0x20
            op = seq.latches[idx]
            check LloOpcodeIsVectorLatch(op)         // (opcode - 0x8d) < 0xa, else FATAL
            tgt = op.region.module.target            // [[op+0x10]+0x38]+0x10
            glm = latch_mode(op)                     // BYTE[op+0x40]
            has_overrun = tgt.vtbl[+0x358](glm)      // GainLatchModeHasOverrunChecks
            if (idx == 0 && !has_overrun): break      // first latch, no overrun ⇒ abandon sequence
            set_latch_index_in_sequence(op, idx)      // WORD[op+0x42] = idx
```

Four of the five gens are flat `FALSE` — their first latch is never indexed. Viperfish is the sole gen with the handshake, and only for the wide non-bf16 NO_XPOSE modes:

```c
function ViperfishTarget::GainLatchModeHasOverrunChecks(glm):   // sub_1D49AB20
    if (LatchModeIsTranspose(glm)) return false;                // transpose ⇒ no overrun
    fmt = GainLatchModeToMatmulDataFormat(glm);
    return MatmulDataFormatIsIntegral(fmt) | ((fmt - 3) < 2);   // ⇒ fmt ∈ {3,4,5,6,7,8}
```

| Gen | `GainLatchModeHasOverrunChecks` (`+0x358`) | `HasMsrOverrunChecks` | Confidence |
|---|---|---|---|
| Jellyfish (`sub_1D4925E0`) | `FALSE` (always) | `FALSE` | CERTAIN |
| Dragonfish (`sub_1D4901C0`) | `FALSE` | `FALSE` | CERTAIN |
| Pufferfish (`sub_1D494880`) | `FALSE` | `FALSE` | CERTAIN |
| Viperfish (`sub_1D49AB20`) | non-transpose AND fmt∈{3..8} → GLM `{14,16,18,20,22,24}` | **`TRUE`** (`sub_1D49AAC0`) | CERTAIN |
| Ghostlite (`sub_1D497940`) | `FALSE` | `FALSE` | CERTAIN |
| base Target (`sub_1D61D8C0`) | `LogFatal` stub | `LogFatal` | CERTAIN |

> **NOTE —** Viperfish (TPU v5p) is the only generation with the MSR/first-latch overrun handshake at the gen level, which is exactly why its per-GLM `+0x358` override is the only non-trivial body and why the overrun-cost reservation lives in the Viperfish namespace. The full overrun behavior — first-latch index assignment, MSR reservation cost — is detailed on [Latch Assignment & Overrun](../sched/latch-assignment-overrun.md).

---

## Matprep — Staging the Operand, Per Gen

### Purpose

Matprep stages the moving operand (activations) and prepares the gain matrix for latching. The *true* matprep opcodes are `kVectorMatprepSubr` (`0x97`/`0x98`, sub-row form) and `kVectorMatprepMubr` (`0x99`/`0x9a`, block-row form), plus the `kVectorMatmulLmr` / `kVectorDoneWithGains` / `kVectorLoadGmr` helpers (`0xa5`/`0xa8`/`0xa9`). These are distinct from the gain-LATCH family (`0x8d..0x96`).

The key reimplementation fact is that matprep has **no uniform cost representation** — each generation expresses it differently, which is the divergence a reimplementer must reproduce:

| Family | Jellyfish/Dragonfish (v2/v3) | Pufferfish (v4) | Viperfish (v5p) | Ghostlite/GF (v6e/v7) | Confidence |
|---|---|---|---|---|---|
| matmul | flat cell (LUT collapse to 5 instrs) | raw 2-bit plane + base | `matmul_data_format → a2d05c0` | `matmul_data_format → a2d05d0` | HIGH |
| matprep `0x97..0x9a` | folded into matmul (transpose-of-gains) | via Latch ops `0xdc`/`0xe6` | matprep ops **FATAL**; via matmul-fmt + modifier reservation | fixed binary-search rows | HIGH |
| transpose accepted | `{B32}` only | `{B32,CompB16,SegB32,SegB16}` | `{B32,CompB16,SegB32,SegB16}` | `{B32,CompB16,CompB8}` | HIGH |

### GL / GF — Fixed Binary-Search Rows

On Ghostlite/GF the matprep opcodes are not in the classifier jump table (`opcode-1 ≥ 0x96` falls to default-FATAL), so they resolve through the 258-entry binary-search remap (`@0x4067dc8`) to **fixed** perf rows — one row per matprep variant, *not* fanned out per data format:

| LLO op | name | `GhPerf::Instruction` | GF flat latency | Confidence |
|---|---|---|---|---|
| `0x97` | `kVectorMatprepSubr` | `0x120` | 1 | HIGH |
| `0x98` | `kVectorMatprepSubrMsk` | `0x121` | (matprep) | HIGH |
| `0x99` | `kVectorMatprepMubr` | `0x11c` | 1 | HIGH |
| `0x9a` | `kVectorMatprepMubrMsk` | `0x11d` | 1 | HIGH |
| `0xa5` | `kVectorMatmulLmr` | `0x154` | −1 default (grid-priced) | HIGH |
| `0xa8`/`0xa9` | `kVectorDoneWithGains` / `kVectorLoadGmr` | `0x157` (shared) | −1 default | HIGH |

The matprep band `0x11c..0x121` sits just below the matmul band `0x124..`; the rows carry flat latency 1 and are throughput-priced through the resource grid. The flat-latency-1 reading is from the GF perf constructor `sub_1C8D3740` (which Hex-Rays does not decompile — read at the disassembly level only, hence the HIGH rather than CERTAIN grade on this band).

### VF — Folded into the Matmul-Format Table + Modifier Reservation

On Viperfish the matprep opcodes `0x97..0x9a` **FATAL** in `GetViperfishInstruction` (`sub_1C8A3300`, default arm `sub_1C8A3E6A`). The matmul opcode `0x9b` reads `matmul_data_format()` and indexes the new VF table `a2d05c0`:

| `MatmulDataFormat` | dtype | VFinstr ordinal | flat latency | grid (r2 prep / r3 throughput) | Confidence |
|---|---|---|---|---|---|
| 1 | f32 | `0xd4` | 131 | r2:7 r3:8 | HIGH |
| 2 | bf16 | `0xda` | 131 | r2:7 r3:16 | HIGH |
| 3 | f8e5m2→bf16 | `0xf8` | 131 | r2:7 r3:32 | HIGH |
| 4 | f8e4m3b11→bf16 | `0xfe` | 131 | r2:7 r3:32 | HIGH |
| 5 | u8 | `0xe0` | 121 | r3:16 | HIGH |
| 6 | s8 | `0xe6` | 121 | r3:16 | HIGH |
| 7 | u4 | `0xec` | 121 | r3:16 | HIGH |
| 8 | s4 | `0xf2` | 121 | r3:16 | HIGH |

The throughput port `r3` is the per-format reservation width: f32=8, bf16=16, fp8=32, int8/int4=16. The bf16-class (`0xd4..0xfe`) carries a separate prep port (`r2:7`) and base latency 131; the int-class (`0xe0..0xf7`) drops `r2` and uses 121. Each matmul-format ordinal is followed by a group of matprep-stage ordinals (e.g. `0xd5/0xd6/0xd8/0xd9` between `0xd4` and `0xda`) that add the 4-stage systolic-feed pipeline `r4:4 r5:12 r6:20 r7:28`.

The matprep stages do *not* carry standalone classifier ordinals — they are produced by `MxuLatencyTable::GetResourceUsage` (`sub_1C8AE5C0`), which builds a `MatpushModifier { MatmulDataFormat, is_transpose, Msr }` key and looks it up in a `FlatHashMap<Modifier, array<int,19>>` reservation table (the matprep `r4..r7` stages are 4 of the 19 `MxuResource` ports). See [Matmul-Mode Modifiers](../cost/matmul-mode-modifiers.md).

### PF — Folded into the Latch Ops

On Pufferfish the matprep opcodes `0x97..0x9a` also FATAL (default arm `sub_1C8A2A08`). PF expresses matprep through the single-ordinal Latch / LatchMsk arms, gated by `SupportsGainLatchMode` (vtable `+0x368`):

| LLO op | name | arm `sub_ADDR` | PFinstr | Confidence |
|---|---|---|---|---|
| `0x8f` | `kVectorLatch` | `sub_1C8A2781` | `0xdc` | HIGH |
| `0x90` | `kVectorLatchMsk` | `sub_1C8A226F` | `0xe6` | HIGH |

`0xdc`/`0xe6` are the entry into the PF "MXU matprep band"; PF's matprep is just this single Latch/LatchMsk pair, throughput-priced through resource-grid ports.

### JF / DF — Transpose-of-Gains Folded into the Matmul

On Jellyfish/Dragonfish there is no standalone matprep classifier — `CycleTableInstruction` (`sub_1C89CA80`) collapses the 11 `MatmulDataFormat` values to 5 instructions via a LUT, and the transpose-of-gains is folded into the matmul opcode: `EmitVectorMatmul` (`sub_140B92C0`) dispatches the VEOpcode on `DoneWithGainsMode == 2 (TRANSPOSED)` — `0x9b → 0/4`, `0x9d → 2/6`, `0x9e → 1/5`. The standalone transpose accepts only `VxposeMode 0` (B32) → VEOpcode `0xf`; every other mode FATALs ("JFC/DFC only support B32 transpose instructions").

> **QUIRK —** the matprep *representation* migrated across gens: JF absorbs it into the matmul, PF into the Latch ops, VF into the matmul-format table plus a modifier reservation, and only GL/GF gives each matprep variant a dedicated fixed perf row. A reimplementation that assumes one matprep cost model across gens will mis-price four of the five.

---

## The MxuSequence Record

### Purpose

`MxuSequence` is the per-sequence record that `MxuAssigner` iterates: `SetLatchIndices` orders the latches, `LatchLhs` partitions the gain matrix and emits the latch+matmul+matres ops, and `AllocateMrb`/`Bounce` (the output side) assign result-FIFO addresses and MSR banks. It holds five instruction lists; the per-instruction "latch state" is distributed onto the member `LloInstruction`s, not stored as flat scalars.

### Layout (sizeof `0x78`)

Recovered from the deleter `default_delete<MxuSequence>::operator()` (`sub_14504C00`), which frees five `{ptr, count, cap}` lists then `free(seq, 0x78)`:

| Offset | List | Element opcodes / consumer | Confidence |
|---|---|---|---|
| `+0x00` | list0 (setup/head latches) | INFERRED head-of-sequence | MEDIUM |
| `+0x18` | latches / matpushes | `0x8d..0x96` — `SetLatchIndices` count @`+0x20`; `Bounce` MSR stamp | CERTAIN |
| `+0x30` | list2 (prep / xpose aux) | INFERRED matprep/transpose `0xa6`/`0xa7` | MEDIUM |
| `+0x48` | matreses | `0x152` — `AllocateMrb` pop; `LatchLhs` ΣPackingFactor, count @`+0x50` | CERTAIN |
| `+0x60` | matmuls | `0x9b`/`0xa3` — `AllocateMrb` push; `LatchLhs` balance, count @`+0x68` | CERTAIN |
| `0x78` | sizeof | `free(seq, 0x78)` | CERTAIN |

The `+0x18`, `+0x48`, `+0x60` list identities are byte-exact (confirmed by the deleter and three independent consumers); the `+0x00` and `+0x30` identities are inferred by category. The full record and the `set_mxu` commit are on [MxuSequence / SequenceInfo](../sched/mxu-sequence-struct.md).

### LatchLhs — the Gain-Matrix Partition

`LatchLhs` (`sub_10F3B5E0`) is the producer of the latch+matmul+matres ops that `SetLatchIndices` later indexes. It groups the LHS by transpose op, runs a per-MXU capacity guard, then rebuilds the sequence with each op tagged by its MXU quadrant:

```c
function LatchLhs(target, lhs_span, sequences):   // sub_10F3B5E0
    xpose = BuildXposeSequences(lhs_span)          // vec1 = {0xa6,0xa7}, vec2 = {0x154}
    // capacity guard per sequence
    acc = Σ over matreses of MatmulDataFormatPackingFactor(matmul_data_format(op))
    check( ChunksPerTile() * num_mxus >= acc )     // ChunksPerTile = hwcfg[+0x198]/hwcfg[+0x1a0]
    check( acc % ChunksPerTile() == 0 )            // tile-aligned, num_mxus = Target+0x4ac
    // rebuild per quadrant
    for each matmul:
        q   = program_order & 3                    // the MXU quadrant (0..3)
        glm = GLM_byte_table[matmul_op - 0x9b]     // @0xac0913e: {0×8, 0xb, 0xb} ⇒ plain→0, packed→0xb
        VlatchLsf(value, glm, 0)                    // emit kVectorLatchLsf (sub_1D573EC0)
        WORD[emit+0x0b] = (WORD[+0x0b] & 0xf8ff) | ((q<<8)+0x400)   // set_unit_id(q)
        repeat Vmatmul / Vmatres PackingFactor(fmt)× (K-tile split), each unit_id-stamped
```

`MatmulDataFormatPackingFactor` (`sub_1D629300`) indexes the table `@0xb53c6bc` = `{1,2,4,4,4,4,8,8,4,4}` (fmt 1..10) — the column-pack factor that drives the K-tile loop count. The `unit_id` (= MXU quadrant) is the gain-matrix-register bank; the GLM is the latch mode; the MSR (output side) is the staging bank.

---

## Related Components

| Name | Relationship |
|---|---|
| MXU Slot | Consumes the latched gains and matprep'd operand; the matmul step itself |
| Jellyfish 41-Byte Bundle | The v3 `VectorExtended` encoding that serializes these fields |
| Latch Assignment & Overrun | `SetLatchIndices` + the per-gen overrun handshake (scheduling side) |
| MxuSequence / SequenceInfo | The full sequence record and `set_mxu` commit |
| Matmul-Mode Modifiers | The VF `Modifier → array<19>` matprep reservation table |

## Cross-References

- [MXU Slot](slot-mxu.md) — the systolic-array op family these sub-slots feed; matmul / matprep / latch share `VectorExtended`
- [Jellyfish 41-Byte Bundle](bundle-jf-41b.md) — the v3 bundle that encodes the latch/matprep fields into the `VectorExtended` slot
- [Latch Assignment & Overrun](../sched/latch-assignment-overrun.md) — `SetLatchIndices`, `LatchLhs`, and the Viperfish first-latch overrun handshake
- [MxuSequence / SequenceInfo](../sched/mxu-sequence-struct.md) — the byte-exact `MxuSequence` record and the per-instruction latch state
- [MXU Latency Overview](../cost/mxu-latency-overview.md) — the per-gen reservation matrices that price the matprep/latch perf rows
- [IARs Per TensorCore](../cost/iars-per-tensorcore.md) — the `Target+0x4a8` register-count field and its chip-parts source
- [Matmul-Mode Modifiers](../cost/matmul-mode-modifiers.md) — the VF `MatpushModifier`-keyed `array<int,19>` matprep reservation
- [Chip-Parts Binarypb](../targets/chip-parts-binarypb.md) — the embedded per-gen proto that supplies `IarsPerTensorCore`
