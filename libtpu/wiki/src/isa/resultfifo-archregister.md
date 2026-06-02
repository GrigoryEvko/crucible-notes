# ResultFifo and ArchRegister Enums

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

The TensorCore LLO layer names two flat enumerations that the per-opcode metadata table (`opcode_info_big`) and the cross-lane-unit (XLU) scheduler reference constantly: **`ResultFifo`**, the 25 hardware result FIFOs a bundle slot can push to or pop from, and **`ArchRegister`**, the 50-member *physical* architectural-register numbering that the per-opcode read/written register lists are expressed in. Neither is the same as the *virtual* LLO register space. `ResultFifo` is a hardware-resource enum (matmul staging banks, transpose banks, the EUP and cross-lane drains); `ArchRegister` is an instance-resolved physical slot index, the bottom layer beneath the gen-specific arch-register numbering documented in [ArchRegno Numbering](archregno-numbering.md).

If you know LLVM, the closest analogy for `ArchRegister` is `MCRegister` — a target-physical register number — and for `ResultFifo`, a fixed pool of architecturally-visible queues like the x87 stack or a SIMD accumulator file, except that here several FIFOs are *banked* (multiple physical instances selected by a runtime instance index). The enums themselves carry no per-target sizing; the numbering that turns an `ArchRegister` ordinal into a printable `v3` / `s12` / `vm5` / `p2` is built at `Target` construction (see [ArchRegno Numbering](archregno-numbering.md)). This page pins both enums by name, decodes the banked-vs-single split, and traces the one consumer that reads them together — `LloXluGraphOptimizer::ComputeXluOperations`, which walks the dependency graph and emits one XLU operation per cross-lane opcode with its read and written `ArchRegister` sets attached.

For reimplementation, the contract is:

- The 25 `ResultFifo` ordinals and what each FIFO is, plus the `FifoInstance` bank arithmetic that maps a `(ResultFifo, instance)` pair to a flat physical FIFO id, and the per-FIFO per-version depth (`ResultFifoEntryCount`).
- The `RegisterType` 5-member enum and the `ArchRegister` 50-member physical numbering: which 12 ordinals are multi-instance banks, which 38 are single registers, and the `(RegisterType, regno)` pair each resolves to.
- `ComputeXluOperations`: the topological walk, the 21-opcode XLU selection, the `opcode_info_big +0x08` read-list / `+0x14` written-list construction, and the `variant<TransposeTile, RpuOperation, XluControlOperation>` it emits per op.

| | |
|---|---|
| **`ResultFifo` stringify** | `ResultFifoToString` @ `0x14441340` — 25 inline-immediate arms |
| **`ResultFifo` instance resolver** | `internal::FifoInstance` @ `0x14446040` — 5 banked arms + pass-through |
| **`ResultFifo` depth** | `ResultFifoEntryCount` @ `0x1d631520` — 25 arms, `TpuVersion < 6` gate |
| **`RegisterType` stringifiers** | `RegisterTypeToString` @ `0x1d640560`, `RegisterTypeToMnemonic` @ `0x1d640600` |
| **`ArchRegister` instance resolver** | `internal::ArchRegisterInstance` @ `0x126b3240` — 50-arm switch |
| **Per-opcode metadata** | `opcode_info_big` @ `0x227b5570` — `LloOpcodeBigInfo[461]`, 28-byte stride |
| **XLU consumer** | `LloXluGraphOptimizer::ComputeXluOperations` @ `0x126d9780` |

---

## RegisterType

`RegisterType` is the 5-member register-file class enum. Two stringifiers spell it byte-exactly, both as plain `switch` tables:

- `RegisterTypeToString` @ `0x1d640560` writes the long name into a small-string buffer (with the length stored at `buffer[23]`).
- `RegisterTypeToMnemonic` @ `0x1d640600` writes the one- or two-character prefix that arch-register names are built from.

| Ordinal | `ToString` | `ToMnemonic` | Length | Meaning | Opcode count |
|---|---|---|---|---|---|
| 0 | `"none"` | `""` | 4 / 0 | No GPR result | 216 |
| 1 | `"pregs"` | `"p"` | 5 / 1 | Predicate register | 11 |
| 2 | `"sregs"` | `"s"` | 5 / 1 | Scalar register | 58 |
| 3 | `"vmregs"` | `"vm"` | 6 / 2 | Vector-mask register | 19 |
| 4 | `"vregs"` | `"v"` | 5 / 1 | Vector register | 157 |

The string immediates are byte-confirmed: case 1 stores the DWORD `"preg"` then `'s'`, case 2 `"sreg"`+`'s'`, case 4 `"vreg"`+`'s'`; cases 0 and 3 use a `strcpy` of `"none"` / `"vmregs"`; the mnemonic arm uses the two-byte immediates `'p'`, `'s'`, `'v'` and a `strcpy("vm")`. The "opcode count" column is the per-`RegisterType` histogram of which class each LLO opcode produces.

| | | Confidence |
|---|---|---|
| Five-member enum, ordinals 0..4 | Both stringifiers are 5-arm switches | CERTAIN |
| Names and mnemonics | Inline immediates decoded | CERTAIN |
| Opcode-count histogram | Cross-referenced produced-type histogram | HIGH |

> **NOTE —** the register allocator splits these five classes into two groups, witnessed by two assertion strings in the binary: `"type == RegisterType::kPreg || type == RegisterType::kVmreg"` (the non-spillable predicate/mask group) and `"type == RegisterType::kSreg || type == RegisterType::kVreg"` (the spillable scalar/vector data group). `kNone` is never allocated.

---

## ArchRegister and its Physical Numbering

`ArchRegister` is a 50-member enum (ordinals 1..0x32) that is **not** a name set. It is a physical numbering: `internal::ArchRegisterInstance(ArchRegister, optional<int> instance)` @ `0x126b3240` maps an `(enum, instance-index)` pair to a flat physical arch-register slot. The function is a 50-arm `switch` with a bound `(arch_reg - 1) <= 0x31`; the default arm returns the ordinal unchanged.

### Banked versus single registers

Twelve ordinals are **multi-instance banks** — registers that exist in several physical copies and require a runtime instance selector. Their `switch` arms validate the instance index (`*unit_id < count`) then return `ordinal + instance`, so each bank occupies a contiguous physical run `[ordinal .. ordinal + count - 1]`. The other 38 ordinals fall through to the default arm and return the ordinal directly.

| Banked ordinal | Instance count | Physical base | Slot range |
|---|---|---|---|
| 0x01 | 3 | 1 | 1..3 |
| 0x05 | 3 | 5 | 5..7 |
| 0x0b | 4 | 11 | 11..14 |
| 0x0f | 4 | 15 | 15..18 |
| 0x13 | 4 | 19 | 19..22 |
| 0x17 | 4 | 23 | 23..26 |
| 0x1b | 4 | 27 | 27..30 |
| 0x1f | 4 | 31 | 31..34 |
| 0x26 | 4 | 38 | 38..41 |
| 0x2a | 4 | 42 | 42..45 |
| 0x2e | 4 | 46 | 46..49 |
| 0x32 | 4 | 50 | 50..53 |

The two count-3 banks (0x01, 0x05) assert `*unit_id < 3`; the ten count-4 banks assert `*unit_id < 4`. The base always equals the ordinal (`return ordinal + instance`).

| | | Confidence |
|---|---|---|
| 50-member enum, bound `(reg-1) <= 0x31` | Switch bound check | CERTAIN |
| 12 banked ordinals, counts 3/3 then 4×10 | Per-arm `*unit_id < N` + `+base` | CERTAIN |
| 38 single registers | Default arm returns ordinal | CERTAIN |
| Per-ordinal symbolic name (which is a loop counter vs sync-flag bank, etc.) | No static `ArchRegister->ToString` exists | LOW |

> **GOTCHA —** there is no static `ArchRegister`-to-name table. The printable name of any arch register is produced one level up by `RegisterNumbering::ToArchRegString` @ `0x1275e2a0`, which prints `"<RegisterTypeToMnemonic(type)><regno>"` (e.g. `v3`, `s12`, `vm5`, `p2`) *after* resolving the slot through the per-`Target` numbering table built by `Target::InitRegisterNumbering`. So "ArchRegister 23" does not have a fixed name — it prints as `vN` / `sN` / etc. only once the target's numbering is bound. See [ArchRegno Numbering](archregno-numbering.md).

### The MRB pseudo-register namespace

Above the ~50 real arch registers sits a pseudo-register namespace for matmul-result-buffer (MRB) entries. `PseudoArchRegisterFromMrbEntry` @ `0x1443c860` returns `(mrb_id << 9) + mrb_entry + 0x36` with `mrb_entry < 0x200` and `mrb_id < 4` — four banks of 512 entries, based at physical slot 0x36, immediately above the real arch registers. These are the FIFO-backed pseudo-register slots the scheduler tracks for result-buffer liveness.

---

## ResultFifo

`ResultFifo` enumerates the 25 hardware result FIFOs (ordinals 0..0x18). The authoritative name source is `ResultFifoToString` @ `0x14441340`, a pure `switch` where each arm writes the name as inline ASCII immediates and stores the length at `buffer[23]`. The count is independently cross-confirmed by `ResultFifoEntryCount` @ `0x1d631520`, which has the same 25 valid arms.

| Ordinal | Name | FIFO class | Confidence |
|---|---|---|---|
| 0x00–0x03 | `kMsrA0`..`kMsrA3` | Matmul staging-result, bank A, instances 0..3 | CERTAIN |
| 0x04–0x07 | `kMsrB0`..`kMsrB3` | Matmul staging-result, bank B, instances 0..3 | CERTAIN |
| 0x08–0x0b | `kMrf0`..`kMrf3` | Matmul result FIFO, instances 0..3 | CERTAIN |
| 0x0c–0x0e | `kTsf0`..`kTsf2` | Transpose staging FIFO, instances 0..2 | CERTAIN |
| 0x0f–0x11 | `kTrf0`..`kTrf2` | Transpose result FIFO, instances 0..2 | CERTAIN |
| 0x12 | `kErf` | EUP result FIFO (transcendental/activation drain) | CERTAIN |
| 0x13 | `kV2sf` | Vector-to-scalar FIFO (vector→scalar bridge) | CERTAIN |
| 0x14 | `kSfrf` | Sync-flag result FIFO (sync-flag read-back) | CERTAIN |
| 0x15 | `kCrf` | Cross-lane result FIFO (XLU permute/reduce result) | CERTAIN |
| 0x16 | `kDrf` | DivRem result FIFO (scalar divide/remainder) | CERTAIN |
| 0x17 | `kSccf` | Cross-core / channel result FIFO | HIGH (name), LOW (gloss) |
| 0x18 | `kCcrf` | Cmem / cross-core result FIFO | HIGH (name), LOW (gloss) |

The names are byte-exact: arms 0..7 are `strcpy("kMsrA0")` … `strcpy("kMsrB3")`; arms 8..17 store the DWORD prefix (`"kMrf"`, `"kTsf"`, `"kTrf"`) plus a single digit byte; arms 18..24 store `"kErf"`, `"kV2sf"`, `"kSfrf"`, `"kCrf"`, `"kDrf"`, `"kSccf"`, `"kCcrf"`. The class gloss for the first ten staging/result FIFOs is anchored by both the name prefix and the `FifoInstance` bank arithmetic; the `kSccf` / `kCcrf` expansion is inferred from the prefix.

> **QUIRK —** a sibling stringifier `MsrToString` @ `0x1d629720` confirms the two matmul staging-result banks are `"msra"` / `"msrb"`, matching `kMsrA*` / `kMsrB*`. A separate `XmrToString` @ `0x1d629740` names the matrix-register file `"gmra"` (gain-matrix register) and `"lmr"` (latch-matrix register) — these are a *different* register file from `ResultFifo`, surfaced in [Slot: MXU](slot-mxu.md).

### FifoInstance — the bank arithmetic

`internal::FifoInstance(ResultFifo, optional<int> instance)` @ `0x14446040` collapses a `(base-FIFO, instance)` pair into a flat physical FIFO id. Only the multi-instance staging/result FIFOs have dedicated `switch` arms; everything else passes through unchanged.

| Base ordinal | FIFO | Instance count | Returns |
|---|---|---|---|
| 0x00 | `kMsrA0` | 4 (`*unit_id < 4`) | `instance` |
| 0x04 | `kMsrB0` | 4 | `instance + 4` |
| 0x08 | `kMrf0` | 4 | `instance + 8` |
| 0x0c | `kTsf0` | 3 (`*unit_id < 3`) | `instance + 12` |
| 0x0f | `kTrf0` | 3 | `instance + 15` |
| *(other)* | — | — | ordinal (pass-through) |

So the instance-bearing physical domain is the matmul + transpose staging/result banks; the higher ordinals (`kTrf1/2` as named enum values, `kErf`, `kV2sf`, `kSfrf`, `kCrf`, `kDrf`, `kSccf`, `kCcrf`) are single-instance FIFOs that do not pass through the bank arithmetic.

> **CORRECTION (P3-345) —** an earlier reading bounded `ResultFifo` at 0..0xf from `FifoInstance`'s `cmp edi,0xf`. That bound is the *physical-instance sub-domain* — the banks that take an instance index — not the enum size. `ResultFifoToString` and `ResultFifoEntryCount` both show 25 members (0..0x18). The enum is 25 wide; only the first 16 ordinals participate in `FifoInstance` arithmetic.

### ResultFifoEntryCount — per-FIFO, per-version depth

`ResultFifoEntryCount(ResultFifo, TpuVersion)` @ `0x1d631520` returns the buffer depth of a FIFO. It is a 25-arm `switch` over the FIFO ordinal; each arm gates on `TpuVersion < 6` (versions 6 and above hit a `"invalid platform type"` fatal). The depth resolution falls into two patterns:

- **Constant depth**, independent of version: `kTsf0`/`kTsf1`/`kTsf2` (cases 12–14) all return **16**; `kSfrf` (case 19) returns **128**.
- **Version-keyed table**: every other FIFO indexes a per-FIFO `int[]` table by `TpuVersion`. Several arms share a table (e.g. cases 0–2 read one table, 4–6 another), reflecting FIFO groups whose depths track silicon generation together.

```c
function ResultFifoEntryCount(fifo, version):        // sub_1d631520
    switch (fifo):
        case kTsf0..kTsf2:                           // cases 12-14
            if (version >= 6) Fatal("invalid platform type")
            return 16                                // depth fixed across gens
        case kSfrf:                                  // case 19
            if (version >= 6) Fatal("invalid platform type")
            return 128
        case <grouped FIFOs>:
            if (version >= 6) Fatal("invalid platform type")
            return depth_table_for_group[version]    // version-indexed int[]
```

> **NOTE —** the `TpuVersion < 6` gate means this build's depth table covers `kJellyfish`(0) through a version-5 codename; version 6+ is a not-yet-supported platform. The full 25 × `TpuVersion` depth matrix is mechanism-confirmed but not enumerated cell-by-cell here.

---

## opcode_info_big — Where the Enums Are Consumed

Both enums are referenced from the per-opcode metadata table `opcode_info_big` @ `0x227b5570` (`LloOpcodeBigInfo[461]`, 28-byte stride, indexed by `LloOpcode`). Each record carries three sentinel-terminated `int8` lists:

```c
struct LloOpcodeBigInfo {              // sizeof 28 (0x1c)
    int8_t result_fifos[8];            // +0x00 : ResultFifo 0..0x18, neg-terminated (forward reader)
    int8_t arch_registers_read[12];    // +0x08 : ArchRegister 1..0x32, neg-terminated (-12 counter reader)
    int8_t arch_registers_written[8];  // +0x14 : ArchRegister 1..0x32, neg-terminated (forward reader)
};
```

The read list at `+0x08` is read by a loop that starts a counter at `-12` and indexes `record[counter + 0x14]`, so it sweeps offsets `+0x08..+0x13` (12 entries) and stops at the first negative byte. The written list at `+0x14` and the result-fifo list at `+0x00` are read forward. Each `ArchRegister` code is resolved to a physical slot through `ArchRegisterInstance`, with the instance index drawn from the LLO value's metadata word (below). See [opcode_info_big](record-format.md) for the full descriptor.

> **CORRECTION (P3-345) —** the `+0x08..+0x13` field was earlier called a "reserved gap read by no consumer". It is the `arch_registers_read[12]` list. The three readers — `ComputeXluOperations` and both `GetPseudoArchRegistersRead<…>` instantiations — each start a loop counter at `-12` (`0xfffffffffffffff4`) and add it to a `+0x14` displacement, which lands the read at `+0x08`, not `+0x14`. The consumer literally named `GetPseudoArchRegisters***Read***` confirms the field is the registers-read list.

---

## LloXluGraphOptimizer::ComputeXluOperations

### Purpose

`ComputeXluOperations` @ `0x126d9780` builds the list of cross-lane-unit (XLU) operations plus, for each, the read and written `ArchRegister` sets. That per-op register dataflow is what the XLU scheduler turns into a cross-XLU dependency graph (see [ArchRegno Numbering](archregno-numbering.md#crossxlu-data-dependency-tracker)). This is the single consumer that reads both `ArchRegister` lists and emits the XLU-operation variant.

### Algorithm

```c
function ComputeXluOperations(this):                 // sub_126d9780
    nodes = LloDependencyGraph::NodesInTopologicalOrder(false)  // sub_1442b8c0
    for node in nodes:
        op = WORD[node.value]                         // LloOpcode
        // dispatch: lea ecx,[op-0x8b]; cmp 0xca; ja skip → JT[op-0x8b]
        if not (0x8b <= op <= 0x155) or JT[op-0x8b] == skip_arm:
            continue                                  // 182 of 203 band opcodes are non-XLU
        // ---- this is one of the 21 XLU opcodes ----
        instance = nullopt
        meta = WORD[node.value + 0xb]
        if (meta & 0x400):                            // explicit-instance flag
            instance = ((meta >> 8) & 3) | has_value  // optional<int>

        // READ set: opcode_info_big +0x08 list, -12 loop, neg-terminated
        record = &opcode_info_big[28 * op]
        for c = -12; c < 0; ++c:
            code = record[c + 0x14]                   // offsets +0x08..+0x13
            if code < 0: break
            read_set.emplace(ArchRegisterInstance(code, instance))   // InlinedVector<ArchRegister,2>

        // WRITTEN set: forward +0x14 list
        written_set = LloOpcodeArchRegistersWritten(op, instance)    // sub_126b2ea0

        // emit one variant per op (stride 0x48, discriminant at +0x40)
        emit variant<TransposeTile, RpuOperation, XluControlOperation>(op, read_set, written_set)
    return StatusOr<vector<variant<...>>>
```

The dispatch is a jump table at `0xadf5504` indexed by `op - 0x8b` (203 entries) with only two arms: an XLU arm (21 opcodes) and a skip arm (182 opcodes). Out-of-band opcodes (`< 0x8b` or `> 0x155`) skip too.

### The 21 XLU opcodes

| Opcode | Name | Variant | Confidence |
|---|---|---|---|
| 0x08b | `kVectorSetPermutePattern` | `XluControlOperation` | CERTAIN (opcode), HIGH (variant) |
| 0x08c | `kVectorSetSegmentPattern` | `XluControlOperation` | CERTAIN / HIGH |
| 0x0a6 | `kVectorTranspose` | `TransposeTile` | CERTAIN / HIGH |
| 0x0a7 | `kVectorTransposeBinary` | `TransposeTile` | CERTAIN / HIGH |
| 0x0f5 | `kVectorMinReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0f6 | `kVectorMaxReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0f7 | `kVectorAddReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0f8 | `kVectorMaxIndexReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0f9 | `kVectorMinIndexReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0fa | `kVectorMaxSegmentReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0fb | `kVectorMinSegmentReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0fc | `kVectorAddSegmentReduceF32` | `RpuOperation` | CERTAIN / HIGH |
| 0x0fd | `kVectorMinReduceBf16` | `RpuOperation` | CERTAIN / HIGH |
| 0x0fe | `kVectorMaxReduceBf16` | `RpuOperation` | CERTAIN / HIGH |
| 0x0ff | `kVectorAddReduceBf16` | `RpuOperation` | CERTAIN / HIGH |
| 0x100 | `kVectorMaxIndexReduceBf16` | `RpuOperation` | CERTAIN / HIGH |
| 0x101 | `kVectorMinIndexReduceBf16` | `RpuOperation` | CERTAIN / HIGH |
| 0x14f | `kVectorXlaneResult` | `XluControlOperation` (pops `kCrf`) | CERTAIN / HIGH |
| 0x150 | `kVectorPermuteResult` | `XluControlOperation` | CERTAIN / HIGH |
| 0x154 | `kVectorTransposeResult` | `TransposeTile` (pops `kTrf*`) | CERTAIN / HIGH |
| 0x155 | `kVectorTransposeClear` | `TransposeTile` | CERTAIN / HIGH |

The variant assignment is decided at emission time by four byte-exact classifiers (`LloOpcodeUsesTranspose`, `LloOpcodeUsesRpu`, `LloOpcodeIsRpuControl`, `LloOpcodeIsRpuResult`); the per-opcode → variant cells are confirmed in [ArchRegno Numbering](archregno-numbering.md#per-opcode-variant-classification). See [XLU Op Roster](xlu-op-roster.md) for the opcode→factory mapping at the encode side.

> **NOTE —** the reduce family (0xf5..0x101) is contiguous: F32 min/max/add reduce, F32 max/min index-reduce, F32 max/min/add segment-reduce, then the bf16 mirror (min/max/add reduce + max/min index-reduce). This contiguity is exactly the range `LloOpcodeUsesRpu` tests with `(op - 0xf5) < 0xd`.

### A worked example — kVectorAddReduceF32 (0xf7)

1. Topological walk reaches the node; opcode `0xf7`. Dispatch index `0xf7 - 0x8b = 0x6c`; the jump table selects the XLU arm.
2. Read set: `opcode_info_big[0xf7] + 0x08` list, each code resolved via `ArchRegisterInstance(code, instance)` where `instance = (WORD[value+0xb] >> 8) & 3` if bit `0x400` is set, else `nullopt`. This op reads the vreg holding the reduce source.
3. Written set: `LloOpcodeArchRegistersWritten(0xf7)` reads the `+0x14` forward list — the cross-lane-result FIFO (`kCrf`) backing register the result lands in.
4. Emit `RpuOperation` into the XLU-op vector. The read/written sets feed the cross-XLU dependency tracker, so a later `kVectorXlaneResult` (0x14f) that *reads* `kCrf` is ordered after this reduce that *writes* it.

---

## Related Components

| Component | Relationship |
|---|---|
| `opcode_info_big` ([record format](record-format.md)) | Holds the `result_fifos` / `arch_registers_read` / `arch_registers_written` lists keyed by these enums |
| `RegisterNumbering` ([archregno numbering](archregno-numbering.md)) | Turns an `ArchRegister` physical slot into a printable `(RegisterType, regno)` per target |
| XLU scheduler ([archregno numbering](archregno-numbering.md#crossxlu-data-dependency-tracker)) | Consumes the per-op read/written `ArchRegister` sets `ComputeXluOperations` builds |

## Cross-References

- [ArchRegno Numbering](archregno-numbering.md) — how `ArchRegister` ordinals become per-gen `(RegisterType, regno)` arch-register numbers, and the cross-XLU dependency tracker that consumes the read/written sets
- [XLU Op Roster](xlu-op-roster.md) — the opcode→factory table for the cross-lane unit ops named here
- [Slot: VPU](slot-vpu.md) — the vector-processing slot whose ops produce the vregs these FIFOs drain
- [Slot: MXU](slot-mxu.md) — the matmul slot that fills the `kMsr*` / `kMrf*` staging and result FIFOs
- [MC Emitter](mc-emitter.md) — the machine-code emitter that lays these ops into bundle slots
- [opcode_info_big Record Format](record-format.md) — the 28-byte per-opcode descriptor the enums are stored in
- [Bundle Model](bundle-model-overview.md) — the VLIW bundle these slots are issued from
