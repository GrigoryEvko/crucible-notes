# Decode-Side: JF / PF

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (`libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols; `.text` and `.rodata` mapped 1:1, VA == file offset). Other wheel versions differ.*

## Abstract

This page documents the **disassembler inverse** of the two oldest TensorCore MXU encoders: the Jellyfish (TPU v2) `VectorExtended` slot and the Pufferfish (TPU v4) dual-MXU slots. Where the [Jellyfish 41B Bundle](bundle-jf-41b.md) and [Pufferfish 51B Bundle](bundle-pf-51b.md) pages map how a `VectorExtendedInstruction` proto is *packed* into a wire bundle, this page maps how a wire bundle is *parsed back* into that proto — the path a TPU program disassembler, a bundle validator, or a round-trip golden test takes. The decode side is the independent confirmation of the encode side: a decoder that reads bit `N` to recover a field the encoder wrote at bit `N` proves both readings are correct.

The two generations decode with two structurally different mechanisms, and the split is the central fact of this page. **Jellyfish** packs its MXU op into a single contiguous 6-bit `VectorExtendedOpcode` field at absolute bits 29..34, so its decoder (`DecoderJf::DecodeVectorExtendedSlot` @ `0x1e854000`) is a **two-level nested dispatch**: the top three bits (32..34) select an opcode *family*, and the low three bits (29..31) select the sub-opcode within it, mapping the 6-bit value to a `VectorExtendedOpcode` enumerator and writing it to the proto. **Pufferfish** abandons the contiguous-opcode model: its MXU op is recognized by a **linear `Opcode::Matches` sweep** — the decoder (`TensorCoreVectorExtended0Decoder::Decode` @ `0x1ed76f20`) stages the bundle bytes into a scratch struct, then tries each per-opcode predicate (`Noop`, `MatrixMultiply…`, `PushGains…`, `Transpose…`) in a fixed order until one matches. This `Matches`-sweep shape is the v4 origin of the codec design that carries through every later generation; the [VF / GXC decode-side](decode-side-vf-gxc.md) page documents its v5–TPU7x descendants.

The closest LLVM analog is the difference between a `switch`-on-opcode disassembler (Jellyfish: extract the opcode field, jump) and a `MatcherTable`-style predicate cascade (Pufferfish: test each pattern's mask/value pair in priority order). The TPU twist is that the Pufferfish predicate cascade is *generated per-opcode* — there is no single opcode field to switch on, because matmul, push-gains, and transpose occupy overlapping bit windows of different widths that only the per-op mask disambiguates.

For reimplementation, the contract is:

- The **Jellyfish two-level decode**: predication first (slot index 6, abs 35), then the family group (abs 32..34) and sub-opcode (abs 29..31), then the `VectorExtendedOpcode` reconstruction tables and the data-source recovery.
- The **`VectorExtendedOpcode` classifier ranges** (`IsMatrixMultiply`, `IsPushGains`, `IsTranspose`, `IsRpu`, `VectorExtendedUsesData`) that bound the opcode space, read directly from the decompiled predicates.
- The **Pufferfish staged-copy + `Opcode::Matches` sweep**: the `min(len,13)`-byte stage, predication-first read, and the per-opcode mask/value predicates for MXU0 (abs 89..102) and the −20 MXU1 twin (abs 69..82).
- The **encode/decode bit-position agreement** — every decode position confirms the encoder's `BitCopy` / shift constant on the corresponding bundle page.

| | |
|---|---|
| **JF decoder entry** | `DecoderJf::DecodeVectorExtendedSlot` @ `0x1e854000` |
| **JF dispatch** | nested `switch`: family `(qword0>>32)&7` (abs 32..34) → sub-opcode `(qword0>>29)&7` (abs 29..31) |
| **JF opcode sink** | `VectorExtendedInstruction` proto `+0x60` (`[VEinst+0x60]`), has-bit `[VEinst+0x10] \|= 0x20` |
| **JF data-source** | `SetVectorRegisterForData` @ `0x1e854c40` — `vex_source` = `(qword0>>27)&3` (abs 27..28) |
| **JF latch sub-table** | `asc_B833FA0` = `{7,8,9,0,10,11,12}` (indexed `(abs29..31)−1`) |
| **PF decoder entry** | `TensorCoreVectorExtended0Decoder::Decode` @ `0x1ed76f20` (MXU0), `…Extended1Decoder::Decode` @ `0x1edcea40` (MXU1) |
| **PF stage** | `memcpy(stage+8, span, min(len,13))`; `stage[0]=1` size-tag; reads at `stage+0x10` = abs bit 64.. |
| **PF MXU0 opcode** | matmul abs 89..97 (w9); PushGains / Transpose abs 91..97 (w7); predication abs 98..102 (w5) |
| **PF MXU1 twin** | exactly −20 (matmul 69..77, opcode 71..77, predication 78..82) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## Jellyfish (v2) — The Two-Level Jump-Table Decode

### Purpose

`DecoderJf::DecodeVectorExtendedSlot` reverses `EncoderJf::EncodeVectorExtendedInstruction` (`0x1e869f00`, [Jellyfish bundle](bundle-jf-41b.md#vector-extended--mxu-struct-0x0c--encodevectorextendedinstruction--0x1e869f00)). It reads the 41-byte `HardwareBundle` bit-view, reconstructs a `VectorExtendedInstruction` proto submessage in the `Bundle`'s arena, and records any malformed-opcode condition in the `VectorProgramErrors` accumulator. It is one of the per-slot decoders `DecoderJf::DecodeBundle` (`0x1e837e00`) drives once per populated slot.

### Entry Point

```text
DecoderJf::DecodeBundle @0x1e837e00            ── per-slot decode dispatch (41-byte bundle)
  └─ DecodeVectorExtendedSlot @0x1e854000      ── the MXU / VectorExtended slot
       ├─ DecodePredication(this, bundle, 6, …)  ── slot index 6 → predicate @ abs 35
       ├─ (nested switch on the 6-bit opcode field)
       ├─ SetVectorRegisterForData @0x1e854c40   ── vex_source + data vreg (abs 27..28)
       └─ SetRotateCountForVectorExtended @0x1e854e00 ── RPU rotate operand (families 3,4)
```

### Algorithm

The decoder reads bundle qword 0 (abs 0..63) into `rcx`, splits the 6-bit opcode into a 3-bit family (abs 32..34) and a 3-bit sub-opcode (abs 29..31), and writes the reconstructed `VectorExtendedOpcode` to the proto. The structure is a `switch` on the family with a nested `switch`/table per family — the compiler's realization of the two-level decode.

```c
// DecoderJf::DecodeVectorExtendedSlot @ 0x1e854000 (decompiled, verified)
function DecodeVectorExtendedSlot(this, bundle, out_bundle, errors):
    if (!DecodePredication(this, bundle, /*slot=*/6, out_bundle))   // predicate @ abs 35
        return Ok                                                   // slot is a nop
    out_bundle[0x10] |= 0x80                                        // mark VE slot present
    ve = arena.DefaultConstruct<VectorExtendedInstruction>()        // proto +0x50 of out_bundle
    CHECK(bundle.encoding().size() == 41)                           // decoder_jf.cc:721

    qword0 = bundle.qword[0]                                        // abs 0..63
    family   = (qword0 >> 32) & 7                                   // abs 32..34  (top 3 opcode bits)
    sub      = (uint32)qword0 >> 29                                 // abs 29..31  (low 3 opcode bits)

    switch (family):
      case 0:                                                       // matmul group
          switch (sub):                                             //   sub 0 = invalid
            1..7: veopcode = sub - 1                                //   → {0,1,2,3,4,5,6}
            0:    errors.set_bad_vector_op_code(1); return Error
          ve[0x60] = veopcode;  ve[0x10] |= 0x20                    // write opcode + has-bit
          if (VectorExtendedUsesData(veopcode))                     //   op != 3
              SetVectorRegisterForData(qword_ptr, ve, errors)
      case 1:                                                       // latch / PushGains group
          i = sub - 1
          if (((0x77 >> i) & (i < 7)) == 0) { errors.set_…; return Error }   // valid i={0,1,2,4,5,6} → sub={1,2,3,5,6,7}
          ve[0x60] = asc_B833FA0[i]                                 // {7,8,9,0,10,11,12}[i]
          ve[0x10] |= 0x20;  SetVectorRegisterForData(…)
      // families 2,5,6,7 guard sub<=4 (qword0 >= 0xA0000000 → sub>=5 is invalid-opcode error)
      case 2: if (sub>=5) error; ve[0x60] = sub + 13;  …; SetVectorRegisterForData(…)  // {13..17}
      case 3: ve[0x60] = 18;        …; SetRotateCountForVectorExtended(…); SetVectorRegisterForData(…)
      case 4: ve[0x60] = 19;        …; SetRotateCountForVectorExtended(…); SetVectorRegisterForData(…)
      case 5: if (sub>=5) error; ve[0x60] = sub + 20;  …; SetVectorRegisterForData(…)  // {20..24}
      case 6: if (sub>=5) error; ve[0x60] = sub + 25;  …; SetVectorRegisterForData(…)  // {25..29}
      case 7: if (sub>=5) error; ve[0x60] = sub + 30;  …; SetVectorRegisterForData(…)  // {30..34}
    return Ok
```

The opcode field is the same 6-bit window the encoder cleared with mask `0xFFFFFFF81FFFFFFF` (bits 29..34); the decoder reads `(qword0>>32)&7` for the family and `(uint32)qword0>>29` for the sub-opcode, which together reconstruct exactly that field. Because struct byte `0x0C` of the encoder's scratch is absolute bit 96 (the [12-byte-strip law](bundle-jf-41b.md#the-12-byte-strip-and-the-absolute-bit-law)), the encoder's qword-0 shift constants read as their absolute bit positions verbatim, and the decoder's shifts match them one-for-one. This raises the JF opcode / data-source / predicate fields to CERTAIN-grade cross-confirmation.

> **QUIRK — the sub-opcode is offset by one, not zero-based.** For family 0 the decoder maps sub-opcode `1..7` to `VectorExtendedOpcode 0..6`, and sub-opcode `0` is an *invalid-opcode* error, not opcode 0. The latch family (1) does the same: it indexes `asc_B833FA0` with `sub−1`. A reimplementation that treats the sub-opcode as a direct opcode index will be off by one on every family-0/1 op and will silently accept the reserved sub-opcode 0. The on-wire value 0 in the sub-field is the encoder's way of reserving the all-zero slot.

> **NOTE —** families 3 and 4 carry no sub-opcode in the proto opcode (they decode to the fixed values 18 and 19), but they *do* read a rotate operand via `SetRotateCountForVectorExtended` (`0x1e854e00`) — these are the RPU rotate/permute ops whose shift count is a separate operand, not part of the opcode. Families 2, 5, 6, 7 form the rest of the `{13..34}` transpose/RPU range (each guards `sub<=4`) and read only the data-source vreg.

### The VectorExtendedOpcode Classifier Ranges

The 6-bit `VectorExtendedOpcode` space is partitioned by five `ProtoUtils` predicates, each a tiny arithmetic test read directly from the decompile. These are the authoritative ranges — they are constants in the binary, not inferred:

```c
// platforms_deepsea::jellyfish::isa::ProtoUtils (verified)
IsMatrixMultiply(op):       return (op < 7) & (0x77 >> op);   // {0,1,2,4,5,6}   @ 0x1e875b20
IsPushGains(op):            return (uint)(op - 7) < 6;        // {7..12}          @ 0x1e875b80
IsTranspose(op):            return (uint)(op - 15) < 2;       // {15,16}          @ 0x1e875b40
IsRpu(op):                  return (uint)(op - 17) < 0x12;    // {17..34}         @ 0x1e875b60
VectorExtendedUsesData(op): return op != 3;                  // op 3 reads no data @ 0x1e876160
```

| VEopcode range | Classifier | Family | Confidence |
|---|---|---|---|
| `0,1,2,4,5,6` | `IsMatrixMultiply` (`op<7 & 0x77>>op`) | dense matmul step | CONFIRMED |
| `3` | excluded from `IsMatrixMultiply`; `!VectorExtendedUsesData` | staging-only matmul (reads no vector data operand) | CONFIRMED |
| `7..12` | `IsPushGains` | weight-latch (the `GainLatchMode 0..5` range) | CONFIRMED |
| `15,16` | `IsTranspose` | matrix transpose (matprep) | CONFIRMED |
| `17..34` | `IsRpu` | reduce / permute-unit family | CONFIRMED |

> **GOTCHA —** `IsTranspose` is the decompiled predicate (`0x1e875b40`) `(op − 15) < 2`, i.e. opcodes **`{15,16}`** — the transpose pair sits *below* the `IsRpu` range (`{17..34}`), not inside it. A reimplementation that classifies the transpose ops at 17/18 will mislabel two RPU ops as transposes and miss the real transpose pair. Likewise `IsMatrixMultiply` is `(op<7) & (0x77>>op)`, which **excludes opcode 3** (`0x77 = 0b1110111` has bit 3 clear); opcode 3 is the staging-only matmul flagged by `VectorExtendedUsesData(op)==false`.

### The Data-Source Recovery

`SetVectorRegisterForData` (`0x1e854c40`) reads the 2-bit `vex_source` selector at abs 27..28 and, per its value, recovers the data vreg from a *different* bit window — the source kind dictates where the register number lives:

```c
// SetVectorRegisterForData @ 0x1e854c40 (decompiled, verified)
function SetVectorRegisterForData(qword_ptr, ve, errors):
    switch ((qword0 >> 27) & 3):                       // vex_source @ abs 27..28
      case 0: ve[0x64] = 0;  reg = (word@14 | byte@16<<16) >> 14 & 0x1F   // vs0-relative
      case 1: ve[0x64] = 1;  reg = (dword@10 >> 15) & 0x1F                // vs1-relative
      case 2: ve[0x64] = 2;  reg = (word@8 >> 11)                         // vs2-relative
      case 3: errors.set_bad_vex_source(1); return Error  // "Bad vex_source value: 3"
    ve[0x6c] = reg;  append reg to ve.repeated_field;  ve[0x10] |= 0x141
```

> **QUIRK — abs 27..28 is the data-*source* selector, not a physical MXU id.** The encoder writes this 2-bit field from proto `+0x64` (which the [Jellyfish bundle page](bundle-jf-41b.md#vector-extended--mxu-struct-0x0c--encodevectorextendedinstruction--0x1e869f00) labels "mxu-id"), but the decoder names it `vex_source` and uses it to pick *which vector-source port* (vs0/vs1/vs2) the systolic-feed register is read relative to — value 3 is an invalid-source error. On plain Jellyfish there is only one MXU (`MatrixStagingRegisterCount` @ `0x1d490340` returns 1), so this field's "which MXU" reading collapses; on Dragonfish (v3) it is live. The register number itself is read from a different bit window per source value, so a decoder cannot recover the data vreg without first decoding this 2-bit selector. See [MXU Slot](slot-mxu.md#jellyfish-v2--the-direct-pack-vectorextended-slot) for the encode-side view.

---

## Pufferfish (v4) — The Staged-Copy / Opcode::Matches Sweep

### Purpose

Pufferfish's MXU op is not a single opcode field, so its decoder cannot `switch`. Instead `TensorCoreVectorExtended0Decoder::Decode` (`0x1ed76f20`, MXU0) and `…Extended1Decoder::Decode` (`0x1edcea40`, MXU1) reconstruct a `TensorCoreVectorExtended0`/`1` proto by trying every per-opcode `Opcode::Matches` predicate in turn. Each predicate is a mask/value test over the staged bundle bits; the first that matches names the op and selects its operand-field accessors. This is the byte-exact inverse of the `BitCopy`-packed Pufferfish slot ([Pufferfish bundle](bundle-pf-51b.md#vector-extended--mxu-slots--0x1edb0900-mxu0-0x1ee08060-mxu1)).

### Entry Point

```text
TensorCoreCodecBase<…>::Decode @0x1d223240          ── 12-slot decode dispatch
  ├─ TensorCoreVectorExtended0Decoder::Decode @0x1ed76f20   ── MXU0 (abs 83..102)
  │    ├─ TensorCoreVectorExtended0PredicationField::GetConcatenatedValue  (read FIRST)
  │    ├─ TensorCoreVectorExtended0NoopOpcode::Matches      @0x1eda6400
  │    ├─ …MatrixMultiply{Rounded,Low,Hi,…}{Mxu0..3}Opcode::Matches @0x1eda6420..
  │    ├─ …PushGains{Rounded,Low,Hi,Byte}[Masked]Opcode::Matches    @0x1eda7060..
  │    └─ …{DoneWithGains,Transpose,PackedTranspose,…}Opcode::Matches
  └─ TensorCoreVectorExtended1Decoder::Decode @0x1edcea40   ── MXU1 (abs 63..82, the −20 twin)
```

### Algorithm

The decoder default-constructs the proto in the bundle arena, stages up to 13 bundle bytes into a scratch struct, reads the predication field first (bounding it to `< 0x20`), then sweeps the `Opcode::Matches` predicates. The staged struct layout is the recurring V4+ shape: a size-tag at offset 0, the bundle bytes at offset 8, so a predicate that reads the scratch quadword at offset `0x10` is reading absolute bundle bits `(0x10−8)*8 = 64` and up.

```c
// TensorCoreVectorExtended0Decoder::Decode @ 0x1ed76f20 (decompiled, verified)
function Decode(span):
    ve = arena.DefaultConstruct<TensorCoreVectorExtended0>()
    stage[0] = 1                                            // size-tag
    n = min(span.len, 13)                                   // 13-byte stage covers abs 63..102
    memcpy(stage + 8, span.data, n)                         // bundle bytes at stage+8 (abs 0..)
    pred = TensorCoreVectorExtended0PredicationField::GetConcatenatedValue(stage)
    if (pred >= 0x20) return Error("… does not match any encodings")
    ve.predication = pred

    // linear Opcode::Matches sweep — first match wins
    if (NoopOpcode::Matches(stage))            { ve.opcode = Noop;        return Ok }
    if (MatrixMultiplyRoundedMxu0::Matches(s)) { ve.opcode = …Mxu0;       return Ok }
    …                                                       // 154 Extended0 predicates total
    if (PushGainsRoundedOpcode::Matches(s))    { ve.opcode = PushGains…;  return Ok }
    …
```

> **NOTE —** the staged-copy-then-`Matches` shape is uniform from v4 through TPU7x; only the abs bit positions in the masks shift per generation. The [VF / GXC decode-side](decode-side-vf-gxc.md) page documents the same `GetConcatenatedValue` + `Opcode::Matches` mechanism for Viperfish, Ghostlite, and the `6acc60406` family, where it is the only decode path. Pufferfish is the v4 origin.

### MXU0 Opcode Predicates — the Mask/Value Table

Each `Opcode::Matches` reads the staged quadword at offset `0x10` (abs 64..127), ANDs a mask, and compares to a value. The mask pins the field width and base; the value is the opcode. Read byte-for-byte from the predicate bodies:

| Opcode::Matches | Mask (staged qword @ `+0x10`) | Abs bits | Value | Meaning | Confidence |
|---|---|---|---|---|---|
| `NoopOpcode` (`0x1eda6400`) | `~val & 0x7C00000000 == 0` | 98..102 | all-ones | predicate == 31 (`kNeverExecute`) | CONFIRMED |
| `MatrixMultiplyRoundedMxu0` (`0x1eda6420`) | `(word@+19) & 0x3FE == 0` | 89..97 (w9) | mxu-num 0 | op-hi 0, mxu-num @ 89..90 = 0 | CONFIRMED |
| `MatrixMultiplyRoundedMxu1` (`0x1eda6440`) | `& 0x3FE000000 == 0x2000000` | 89..97 | mxu-num 1 | bit 89 set | CONFIRMED |
| `PushGainsRounded` (`0x1eda7060`) | `& 0x3F8000000 == 0x100000000` | 91..97 (w7) | `0x20` | weight-latch rounded | CONFIRMED |
| `PushGainsLow` (`0x1eda7080`) | `& 0x3F8000000 == 0x108000000` | 91..97 | `0x21` | latch `.low` | CONFIRMED |
| `PushGainsByte` (`0x1eda70e0`) | `& 0x3F8000000 == 0x120000000` | 91..97 | `0x24` | latch `.byte` | CONFIRMED |
| `DoneWithGainsGsfn` (`0x1eda7020`) | `& 0x3F8000000 == 0xC0000000` | 91..97 | `0x18` | end-of-gains (gsfn) | CONFIRMED |
| `Transpose` (`0x1eda7360`) | `& 0x3F8000000 == 0x200000000` | 91..97 | `0x40` | systolic transpose op | CONFIRMED |

The two field bases fall straight out of the masks: `0x3FE000000` is bits 25..33 of the staged qword (= abs 89..97, a 9-bit matmul opcode), and `0x3F8000000` is bits 27..33 (= abs 91..97, a 7-bit non-matmul opcode). Shifting each comparison value down by its base recovers the opcode: `0x100000000 >> 27 = 0x20` (PushGainsRounded), `0x120000000 >> 27 = 0x24` (PushGainsByte), `0xC0000000 >> 27 = 0x18` (DoneWithGainsGsfn), `0x200000000 >> 27 = 0x40` (Transpose). This independently confirms the [Pufferfish bundle](bundle-pf-51b.md#vector-extended--mxu-slots--0x1edb0900-mxu0-0x1ee08060-mxu1) MXU0 layout (opcode @ 91 w7, matmul @ 89 w9, predicate @ 98 w5) and the [MXU Slot](slot-mxu.md#pufferfish-v4--the-dual-mxu-codec-origin) `PushGains opcode = 0x20 + variant + masked·0x10` formula, byte-for-byte from the decode side.

> **GOTCHA — Noop is predicate-all-ones, not predicate-zero.** `NoopOpcode::Matches` is `(~val & 0x7C00000000) == 0`, i.e. it matches when bits 98..102 are **all set** — the 5-bit predicate equals 31 (`kNeverExecute`), the empty-slot stamp. An earlier reading described Noop as "bits 98..102 zero". A reimplementation that detects an empty MXU slot by testing the predicate field for zero will mis-classify a valid predicate-register-0 op as a nop and miss the real `kNeverExecute` empty marker. The empty slot is the *maximum* 5-bit value, consistent with the [`kNeverExecute` prefill](bundle-pf-51b.md#the-kneverexecute-prefill).

> **QUIRK — the matmul opcode widens to absorb the physical MXU number.** The non-matmul opcode is a 7-bit field at abs 91..97 (mask `0x3F8000000`); the matmul opcode is a 9-bit field at abs 89..97 (mask `0x3FE000000`), and its low two bits @ 89..90 carry the *physical* MXU number 0..3 (`Mxu0` mask requires bits 89..90 clear; `Mxu1` requires bit 89 set). So `matmul opcode = (op-hi << 2) | mxu-num`, and the four physical arrays (`mxu_count = 4`) are selected inside the opcode, not by the bundle slot. A decoder that masks the matmul opcode as 7 bits loses the MXU-num and decodes every matmul as `Mxu0`. See [MXU Slot](slot-mxu.md#opcode-field--matmul-widens-carries-the-physical-mxu-number).

### MXU1 — the −20-Bit Twin

`TensorCoreVectorExtended1Decoder::Decode` (`0x1edcea40`) is the same sweep over a control region shifted down exactly 20 bits. The MXU1 predicates read the staged **dword** at offset `0x10` (the lower 32 bits cover abs 64..95) rather than the qword, and their masks are the MXU0 masks shifted down by 20:

| Opcode::Matches | Mask (staged dword @ `+0x10`) | Abs bits | Value | Confidence |
|---|---|---|---|---|
| `Extended1MatrixMultiplyLowMxu0` (`0x1edfdd00`) | `& 0x3FE0 == 128` | 69..77 (w9) | bit 71 set | CONFIRMED |
| `Extended1PushGainsRounded` (`0x1edfe8c0`) | `& 0x3F80 == 4096` | 71..77 (w7) | `0x20` | CONFIRMED |
| `Extended1Noop` (`0x1edfdc60`) | `~val & 0x7C000 == 0` | 78..82 | all-ones (31) | CONFIRMED |

The arithmetic confirms the −20 offset exactly: `0x3FE0` is bits 5..13 of the staged dword (= abs 69..77, the 9-bit matmul opcode at 89..97 minus 20); `0x3F80` is bits 7..13 (= abs 71..77, the 7-bit opcode at 91..97 minus 20); `4096 = 0x1000`, and `0x1000 >> 7 = 0x20`, the same PushGainsRounded value as MXU0. The Noop mask `0x7C000` is bits 14..18 (= abs 78..82, the predicate at 98..102 minus 20).

| Field | MXU0 abs | MXU1 abs | Δ | Confidence |
|---|---|---|---|---|
| matmul opcode | 89..97 | 69..77 | −20 | CONFIRMED |
| non-matmul opcode | 91..97 | 71..77 | −20 | CONFIRMED |
| predication | 98..102 | 78..82 | −20 | CONFIRMED |

> **QUIRK — two MXU control slots, four physical MXUs.** Pufferfish has two `VectorExtended` *control* slots (MXU0/MXU1, the −20 twin) and four physical arrays. The slot picks the control lane; the matmul opcode's low two bits pick the array. The −20 twin here is the v4 origin of the same dual-MXU geometry that becomes −20 on Viperfish, −21 on Ghostlite, and −25 on the `6acc60406` family — see [VF / GXC decode-side](decode-side-vf-gxc.md) and the [MXU Slot](slot-mxu.md#cross-generation-field-summary) cross-generation summary.

---

## Per-Generation Decode-Mechanism Summary

The five-generation decode reference, this page (JF, PF) plus its [VF / GXC](decode-side-vf-gxc.md) companion:

| Gen | Codename | Bundle | Decode mechanism | MXU opcode field | Twin | Confidence |
|---|---|---|---|---|---|---|
| v2 | jellyfish | 41 B | two-level nested `switch` on 6-bit opcode (family abs 32..34 + sub abs 29..31) | abs 29..34 (single VE slot) | n/a | CONFIRMED |
| v4 | pufferfish | 51 B | staged copy + linear `Opcode::Matches` sweep | MXU0 abs 89/91; MXU1 abs 69/71 | −20 | CONFIRMED |
| v5 | viperfish | 64 B | staged copy + `Opcode::Matches` sweep | MXU0 push@59 mm@57 | −20 | [VF/GXC](decode-side-vf-gxc.md) |
| v6e | ghostlite | 64 B | staged copy + `Opcode::Matches` sweep | MXU0 unified op@58 (w8) | −21 | [VF/GXC](decode-side-vf-gxc.md) |
| TPU7x | `6acc60406` | 64 B | staged copy + `Opcode::Matches` sweep | MXU0 unified op@62 (w8) | −25 | [VF/GXC](decode-side-vf-gxc.md) |

Jellyfish is the only generation with a single VE issue slot (no twin) and a jump-table-style opcode decode, because its opcode is one contiguous 6-bit field. Every generation from Pufferfish onward uses the staged-copy + `Opcode::Matches` codec; Pufferfish is the v4 origin of both the codec design and the −N dual-MXU twin.

---

## Related Components

| Component | Relationship |
|---|---|
| `DecoderJf::DecodeVectorExtendedSlot` `0x1e854000` | JF MXU slot decoder (two-level opcode decode) |
| `SetVectorRegisterForData` `0x1e854c40` | JF data-source / vreg recovery (`vex_source` abs 27..28) |
| `ProtoUtils::Is{MatrixMultiply,PushGains,Transpose,Rpu}` `0x1e875b20`..`b60` | JF `VectorExtendedOpcode` classifiers |
| `TensorCoreVectorExtended0Decoder::Decode` `0x1ed76f20` | PF MXU0 decoder (staged copy + `Opcode::Matches`) |
| `TensorCoreVectorExtended1Decoder::Decode` `0x1edcea40` | PF MXU1 decoder (the −20 twin) |
| `EncoderJf::EncodeVectorExtendedInstruction` `0x1e869f00` | JF encode side this page inverts |

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW bundle, slot dispatch, and `kNeverExecute` convention the MXU slot lives inside.
- [Jellyfish 41B Bundle](bundle-jf-41b.md) — the v2 `VectorExtended` encode side; the 12-byte-strip law that makes the JF shift constants equal their absolute bits.
- [Pufferfish 51B Bundle](bundle-pf-51b.md) — the v4 dual-MXU `BitCopy`-packed slot map this page's decode confirms byte-for-byte.
- [Decode-Side: VF / GXC](decode-side-vf-gxc.md) — the v5–TPU7x counterpart: the same staged-copy + `Opcode::Matches` codec, the −20/−21/−25 twins, and the abs57/58 Transpose/Target fields.
- [MXU Slot](slot-mxu.md) — the cross-generation MXU op family, opcode roster, and the matmul/PushGains/transpose semantics this page decodes.
- [V5+ EmitX Bit Positions](v5plus-emitx-bit-positions.md) — the `EmitX` → `BitCopy` chain whose decode inverse this page documents for the older JF/PF gens.
- [MC Emitter](mc-emitter.md) — the parallel LLVM-MC encoding path for the same vmatmul/vmatprep MachineInstrs.
- [Record Format](record-format.md) — the on-disk record framing around the encoded bundle bytes.
