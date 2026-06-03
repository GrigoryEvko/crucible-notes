# MxuSequence / SequenceInfo

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). Other versions differ.*

## Abstract

`MxuSequence` is the per-matmul-group record the TPU backend builds when it lowers a dot / convolution accumulation chain onto the systolic array. Each chain — one weight latch, the matmul steps that consume it, and the result pops that drain the accumulator — becomes one `MxuSequence`, and the [MXU assignment stage](mxu-assignment-binpacker.md) iterates a `Span<unique_ptr<MxuSequence>>` to bin-pack the chains onto the physical MXU passes. The record itself is *flat and small*: five `{ptr, count, cap}` instruction lists totaling `0x78` bytes. The interesting per-instruction state — latch mode, latch index, MSR bank, MXU-quadrant unit-id — is **not** stored on the `MxuSequence`. It is distributed onto the member `LloInstruction`s, written by the assignment pass as it walks the lists.

The reader who knows LLVM should think of `MxuSequence` as a `MachineInstr` bundle whose only payload is *which* instructions belong together, plus a small set of fields the scheduler stamps onto each member. The latch-index assignment (`SetLatchIndices`) is the analogue of register allocation for a single architectural resource: every latch in a sequence gets a monotone program-order index, with a first-latch special case gated by a per-generation overrun handshake. `LatchLhs` is the producer — it partitions the LHS gain matrix across the per-MXU latch grid and emits the `vlatch.lsf` / `vmatmul` / `vmatres` ops with each one tagged by its MXU quadrant. A second, distinct record — `MxuStat::SequenceInfo` — is the *output* of the bin-packer: two owning vectors plus a `latch_latency` and an `accumulated_latency` snapshot, written into a per-MXU `btree_map<int, SequenceInfo>` (`sequences_`) once the greedy min-makespan select picks which physical MXU the sequence lands on.

This page is the canonical home of both structures: the byte-exact `MxuSequence` field map (from the deleter and the builder), the `SequenceInfo` layout (from the btree-insert field writes), the latch-index assignment in `SetLatchIndices`, the gain-matrix partition in `LatchLhs`, and the `set_mxu` commit. The op families that fill the lists — the latch `0x8d..0x96`, the matmul `0x9b..0xa5`, the matres `0x152`/`0x153` — and their `LloInstruction` field offsets are owned by the [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) and [MXU Slot](../isa/slot-mxu.md) pages and are referenced, not re-derived, here.

For reimplementation, the contract is:

- The `MxuSequence` layout: five custom `{ptr, count, cap}` vectors at fixed offsets, `sizeof 0x78`, and which opcode family each list holds.
- The per-instruction "latch state" model: the fields the assignment pass stamps onto each member `LloInstruction` (latch mode, latch index, MSR, unit-id), not flat scalars on the sequence.
- `SetLatchIndices`: the monotone latch-index assignment and the first-latch `GainLatchModeHasOverrunChecks` gate.
- `LatchLhs`: the LHS-by-transpose grouping, the `ChunksPerTile × num_mxus ≥ ΣPackingFactor` capacity guard, and the per-quadrant `set_unit_id` stamp on the rebuilt latch/matmul/matres ops.
- `MxuStat::SequenceInfo`: the bin-packer's per-sequence output record (two owning vectors + `latch_latency` + `accumulated_latency`) and the latency delta the makespan cost reads.

| | |
|---|---|
| **`MxuSequence` size** | `0x78` (5 lists × `0x18`) — `default_delete<MxuSequence>::operator()` `0x14504c00` |
| **List element** | `{LloInstruction** @+0x00, int64 count @+0x08, int64 cap @+0x10}` (count is integer, not end-ptr) |
| **List offsets** | `+0x00` head, `+0x18` latches, `+0x30` matprep/MUBR aux, `+0x48` matmuls, `+0x60` matreses |
| **Builder** | `CollectAndTransformSequencesInternal` `0x14500800` (per-list grow-realloc append, `mxu_sequence_collector.cc`) |
| **Latch-index assign** | `MxuAssigner::SetLatchIndices` `0x10f3b4c0` → `WORD[op+0x42]` |
| **Gain-matrix partition** | `MxuAssigner::LatchLhs` `0x10f3b5e0` (const) |
| **`SequenceInfo`** | `MxuStat::SequenceInfo`, `0x40` bytes, btree value `sequences_` |
| **Commit** | `AssignMxusForSequenceGroupInternal` `0x10f77ca0` — `sequences_[seq_key] = SequenceInfo` |
| **Per-instruction state** | latch_mode `BYTE[+0x40]`, latch_index `WORD[+0x42]`, MSR `BYTE[+0x44]`, unit-id `WORD[+0x0b]` |

---

## The MxuSequence Record

### Purpose

`MxuSequence` groups the LLO instructions of one matmul accumulation chain so the MXU-assignment pass can treat the chain as a unit: the latch that loads the weights, the matmul steps that clock the array, and the matres ops that drain the result. It is the container the [bin-packer](mxu-assignment-binpacker.md) iterates and the [MRB/MSR placement](mrb-fifo-msr-placement.md) consumes. It carries no scheduling scalars of its own — only the five lists and the instructions inside them.

### Layout (sizeof `0x78`)

The deleter `std::default_delete<MxuSequence>::operator()` (`0x14504c00`) is the primary layout proof: it frees five vector backing-stores in reverse order (`seq[12]`, `seq[9]`, `seq[6]`, `seq[3]`, `seq[0]` — i.e. the `ptr` word at `+0x60`/`+0x48`/`+0x30`/`+0x18`/`+0x00`), zeroing each list's second word (the count, at `ptr+0x08`) before the free, then `free(seq)` of the whole record.

```c
function default_delete_MxuSequence(seq):     // 0x14504c00, demangled symbol present
    if seq == nullptr: return
    if seq[12]: seq[13] = 0; free(seq[12])     // list @ +0x60 (matreses) : ptr @+0x60, count @+0x68
    if seq[ 9]: seq[10] = 0; free(seq[ 9])     // list @ +0x48 (matmuls)
    if seq[ 6]: seq[ 7] = 0; free(seq[ 6])     // list @ +0x30 (matprep / MUBR aux)
    if seq[ 3]: seq[ 4] = 0; free(seq[ 3])     // list @ +0x18 (latches)
    if seq[ 0]: seq[ 1] = 0; free(seq[ 0])     // list @ +0x00 (head / setup)
    free(seq)                                  // free(seq, 0x78)
```

Each list is a custom three-word vector — `{ptr, count, cap}` — *not* the libc++ `{begin, end, end_cap}` triple: the count word is an integer element count, not an end-pointer. `SetLatchIndices` proves this by looping `idx < seq[+0x20]` and indexing `seq[+0x18][8*idx]` (a count, used as a loop bound, not subtracted from a base pointer). The deleter's "zero the second word of each pair" pattern (`seq[1]`, `seq[4]`, `seq[7]`, `seq[10]`, `seq[13]`) is consistent with the count being cleared before the backing store is freed; the lists are spaced `0x18` apart (`ptr` words at `seq[0]`/`seq[3]`/`seq[6]`/`seq[9]`/`seq[12]`), and five of them give `0x78`. The builder (`mxu_sequence_collector.cc`) appends to these lists with a grow-realloc that updates `{ptr@+0x00, count@+0x08, cap@+0x10}`, confirming the third word is the capacity.

| Offset | List | Element opcodes / consumer | Confidence |
|---|---|---|---|
| `+0x00` | list0 — head / setup | INFERRED head-of-sequence setup | MEDIUM |
| `+0x18` | latches | `0x8d..0x96` — `SetLatchIndices` count @`+0x20` | CERTAIN |
| `+0x30` | matprep / MUBR aux | appended alongside each matmul (builder line ~1190, "Adding vmatprep MUBR") | HIGH |
| `+0x48` | matmuls (`matmuls`) | `0x9b..0xa5` — builder appends the matmul op; `LatchLhs` ΣPackingFactor + `seq->matmuls.size()` CHECK, count @`+0x50` | CERTAIN |
| `+0x60` | matreses (`matreses`) | `0x152`/`0x153` — builder `emplace_back` after `kVectorMatres` CHECK, count @`+0x68` | CERTAIN |
| `0x78` | sizeof | `free(seq, 0x78)` | CERTAIN |

The `+0x18`, `+0x48`, `+0x60` identities are byte-exact: the deleter proves five lists at these offsets, and independent consumers index them with opcode-checked accesses. `SetLatchIndices` reads `+0x18`/`+0x20` and asserts `0x8d..0x96`. In `CollectAndTransformSequencesInternal` a matmul-family op (`(uint16)(op-0x9b) <= 0xa`) is appended to the `+0x48` list (and a paired matprep/MUBR value to the `+0x30` list), and a matres-family op (`(op & ~1) == 0x152`, gated by `opcode == kVectorMatres`) is `emplace_back`'d into the `+0x60` list. `LatchLhs` then reads the same `+0x48` list as `seq->matmuls` — its capacity loop walks `seq[+0x48]` (count @`+0x50`) over opcodes `0x9b`/`0xa3`, and the source-field name is anchored by the CHECK `"...== seq->matmuls.size()"` which reads `*(seq+0x50)`. The `+0x00` (head/setup) and `+0x30` (matprep/MUBR aux) identities are weaker: `+0x30` is the list that grows in lockstep with `+0x48` and is logged as "vmatprep MUBR" (HIGH), while `+0x00` is filled but its opcode membership was not isolated cell-by-cell (MEDIUM).

> **GOTCHA — the per-sequence latch state is on the member instructions, not the sequence.** The deleter frees exactly five vectors and a flat `0x78` record — there is no scalar field block on `MxuSequence` for latch mode, latch index, MSR, or quadrant. Those live on each member `LloInstruction`: latch_mode `BYTE[+0x40]`, latch_index_in_sequence `WORD[+0x42]`, MSR `BYTE[+0x44]`, unit-id pack `WORD[+0x0b]`. A reimplementation that adds those as `MxuSequence` members will diverge from the binary's data flow — the scheduler reads and writes them *through* the lists, op by op. See [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md#lloinstruction-field-layout) for the full field map.

### Builder

`CollectAndTransformSequencesInternal` (`0x14500800`, source `mxu_sequence_collector.cc`) is the producer. It walks the region's LLO values, classifies each by opcode (`(uint16)(op - 0x9b) <= 0xa` → the matmul family appended to the *last* sequence's `+0x48` matmuls list, with its paired matprep/MUBR value appended to `+0x30`; `(op & 0xfffe) == 0x152` gated by `opcode == kVectorMatres` → `emplace_back` into the `+0x60` matreses list; the latch family → the `+0x18` list), filling them by per-list grow-realloc into the `array<Span<unique_ptr<MxuSequence>>, 4>` quadrant slots. It enforces ordering with a `"Matres before matmul"` `FailedPrecondition`. The balance invariant it checks — `ExpectedMatresesPerMatmuls(last_sequence->matmuls) >= total_matreses_count` (`mxu_sequence_collector.cc`, reading `last_sequence` at `+0x48`/`+0x50`) — is the matmul/matres count relationship `LatchLhs` later relies on.

---

## SetLatchIndices — the Latch-Index Assignment

### Purpose

Once the latch ops of a sequence are in program order on the `+0x18` list, each latch needs a *latch index in sequence* — its position in the chain — written into `WORD[op+0x42]`. The bundle packer downstream treats colliding latch indices as a slot-legality constraint, so the index is the commit that ties an op to a latch slot. `SetLatchIndices` (`0x10f3b4c0`) assigns it, with one special case: the **first** latch is indexed only when its `GainLatchMode` carries overrun checks.

### Entry Point

```text
MxuAssigner::VisitRegion                     0x10f3a640
  └─ MxuAssigner::SetLatchIndices            0x10f3b4c0  ── per-sequence latch ordering
       ├─ LloInstruction::latch_mode         0x1d4e7500  ── BYTE[op+0x40] (GainLatchMode)
       ├─ Target::GainLatchModeHasOverrunChecks  vtbl+0x358  ── first-latch gate
       └─ LloInstruction::set_latch_index_in_sequence  0x1d4e7960  ── WORD[op+0x42] = idx
```

### Algorithm

```c
function SetLatchIndices(span_ptr, span_count):    // 0x10f3b4c0; arg = Span<unique_ptr<MxuSequence>>
    if span_count == 0: return
    for seq in span_ptr[0 .. span_count):           // 8-byte stride, seq = *span_ptr
        if seq[+0x20] == 0: continue                // empty latch list — skip
        for idx = 0 .. seq[+0x20) - 1:              // latch count @ seq+0x20
            op = seq[+0x18][idx]                     // latch list @ seq+0x18, 8-byte stride
            check (uint16)(opcode(op) - 0x8d) < 0xa  // 0x8d..0x96, else FATAL
                                                     //   "LloOpcodeIsVectorLatch(opcode)" mxu_assigner.cc:420
            target = op.region.module.target         // [[op+0x10]+0x38]+0x10
            glm    = latch_mode(op)                   // BYTE[op+0x40]
            has_overrun = target.vtbl[+0x358](glm)    // GainLatchModeHasOverrunChecks
            if idx == 0 and not has_overrun: break    // first latch, no overrun ⇒ abandon this sequence
            set_latch_index_in_sequence(op, idx)      // WORD[op+0x42] = idx (check idx <= 65535)
```

Every latch gets a monotone index equal to its position in the `+0x18` list. The first-latch gate is the only branch: if `idx == 0` **and** the gen's `GainLatchModeHasOverrunChecks(glm)` is false, the loop `break`s without indexing the first latch and moves to the next sequence. On the four generations whose `+0x358` override is flat `FALSE`, the first latch is never indexed; only Viperfish has a live handshake. The store itself bounds the index: `set_latch_index_in_sequence` (`0x1d4e7960`) re-checks the opcode family (`(uint16)(opcode - 0x8d) >= 0xa` → FATAL `LloOpcodeIsVectorLatch(opcode())` at `llo_instruction.cc:3399`), asserts `index <= (65535)` (`llo_instruction.cc:3400`), then writes `*((uint16*)op + 33) = idx` — i.e. `WORD[op+0x42]`.

> **QUIRK — index 0 may be deliberately absent.** A reimplementation that always stamps every latch's index (including the first) will over-constrain the bundle packer on the four non-Viperfish gens, where the first latch is intentionally left unindexed because its mode carries no overrun handshake. The gate is per-generation data (`vtbl+0x358`), not a compile-time constant. The full per-gen `GainLatchModeHasOverrunChecks` truth table and the overrun-reservation cost are on [Latch Assignment & Overrun](latch-assignment-overrun.md).

The reader `latch_index_in_sequence` is the symmetric accessor (same `WORD[op+0x42]`, same opcode gate), and `latch_mode` (`0x1d4e7500`) returns `BYTE[op+0x40]` for the latch family or `kVectorMatprepSubr`, FATAL `"Unsupported opcode"` otherwise.

---

## LatchLhs — the Gain-Matrix Latch Partition

### Purpose

`LatchLhs` (`0x10f3b5e0`, a `const` member) is the *producer* of the latch / matmul / matres ops that `SetLatchIndices` later indexes and the [MRB/MSR allocator](mrb-fifo-msr-placement.md) later stamps. It takes the LHS (the stationary gain / weight operand) of a matmul group, partitions its packed columns across the per-MXU latch grid, and rebuilds the latch+matmul+matres sequence into a fresh region with each op tagged by its MXU quadrant. It is the input/gain side that runs *before* the result-side `AllocateMrb` / `Bounce` in `VisitRegion`.

### Entry Point

```text
MxuAssigner::LatchLhs                        0x10f3b5e0  (const)
  ├─ BuildXposeSequences                     0x10f813a0  ── group LHS by transpose op
  ├─ MatmulDataFormatPackingFactor           0x1d629300  ── column-pack factor (table @0xb53c6bc)
  ├─ Target::ChunksPerTile                   0x1d60f2c0  ── hwcfg[+0x198] / hwcfg[+0x1a0]
  ├─ LloRegionBuilder::VlatchLsf             0x1d573ec0  ── emit kVectorLatchLsf
  ├─ LloRegionBuilder::Vmatmul               0x1d575a60  ── emit matmul (K-tile loop)
  ├─ LloRegionBuilder::Vmatres               0x1d5761a0  ── emit matres
  ├─ LloValue::set_unit_id (inlined)         0x12698c00  ── WORD[op+0x0b] quadrant stamp
  └─ ExpectedMatresesPerMatmul               0x145005e0  ── matmul/matres balance
```

### Algorithm

```c
function LatchLhs(target, lhs_span, sequences):    // 0x10f3b5e0
    xpose = BuildXposeSequences(lhs_span)           // 0x10f813a0
    //   vec1 = ops with (opcode & 0xfffe) == 0xa6  → {0xa6 kVectorTranspose, 0xa7 kVectorTransposeBinary}
    //   vec2 = ops with opcode == 0x154            → {kVectorTransposeResult}
    //   xpose.size == 1 ⇒ single-xpose fast path; else multi/no-xpose path

    for seq in sequences:
        // --- capacity guard: packed columns must fit the per-MXU latch grid and tile-align ---
        acc = 0
        for op in seq.matmuls[+0x48 .. count@+0x50):     // matmul-family ops
            if op != 0x9b and op != 0xa3: abort           // also requires matmul_data_format(op)-1 <= 1
            acc += MatmulDataFormatPackingFactor(matmul_data_format(op))
        num_mxus = target[+0x4ac]                          // target int[299]
        check( ChunksPerTile() * num_mxus >= acc )    // else abort
        check( acc % ChunksPerTile() == 0 )           // tile.size()*PackingFactor == ChunksPerTile()

        // --- rebuild per quadrant, stamping the MXU-quadrant unit-id ---
        for each matmul in seq.matmuls[+0x48 .. count@+0x50):
            q   = program_order & 3                    // the MXU quadrant 0..3 (cmp 0x4 bound)
            glm = byte_table_0xac0913e[matmul_op - 0x9b]   // {0×8, 0xb, 0xb}: plain→0, packed→0xb
            latch = VlatchLsf(builder, lhs_value, glm, 0)  // emit kVectorLatchLsf (0x8d)
            WORD[latch+0x0b] = (WORD[latch+0x0b] & 0xf8ff) | ((q << 8) + 0x400)   // = set_unit_id(q)
            for k = 0 .. MatmulDataFormatPackingFactor(fmt) - 1:   // K-tile split
                m = Vmatmul(builder, fmt, ...); WORD[m+0x0b] = set_unit_id(q)
            r = Vmatres(builder, fmt, ...);   WORD[r+0x0b] = set_unit_id(q)
        check( ExpectedMatresesPerMatmul-balance )     // matres_index <= matreses.size() - pushes
```

The three components are each byte-exact:

- **`BuildXposeSequences` (`0x10f813a0`)** scans the LHS span and builds two vectors: `vec1` of ops with `(opcode & 0xfffe) == 0xa6` (the transpose-prep pair `0xa6`/`0xa7`) and `vec2` of ops with `opcode == 0x154` (`0x154` = 340, the transpose-result). `LatchLhs` tests the result's size `== 1` for the single-transpose fast path.
- **The capacity guard** walks the `+0x48` matmuls list (count @`+0x50`), rejects any op that is not `0x9b`/`0xa3` (or whose `matmul_data_format - 1 > 1`), sums `MatmulDataFormatPackingFactor` over it, and requires `ChunksPerTile() * target[+0x4ac] >= acc` and `acc % ChunksPerTile() == 0`. `MatmulDataFormatPackingFactor` (`0x1d629300`) reads `int32 table @0xb53c6bc = {1,2,4,4,4,4,8,8,4,4}` indexed by `fmt - 1` (bounds-checked, FATAL `"Unsupported MatmulDataFormat"` at `matmul_data_format.cc:197`). `ChunksPerTile` (`0x1d60f2c0`) is `target[119]->[+0x198] / target[119]->[+0x1a0]` (the lane-count-derived tile granule).
- **The quadrant stamp** reads `glm` from the `.rodata` byte table `@0xac0913e = {0,0,0,0,0,0,0,0,0xb,0xb}` (op `0x9b..0xa2` → GainLatchMode 0 bf16; op `0xa3`/`0xa4` packed → 0xb = `GLM_PACKED_BF16`), emits the latch via `VlatchLsf`, and stamps every emitted op (`VlatchLsf`, the `PackingFactor`-many `Vmatmul`, the `Vmatres`) with `WORD[op+0x0b] = (WORD[op+0x0b] & 0xf8ff) | ((q & 3) << 8) + 0x400`.

That stamp is byte-identical to `LloValue::set_unit_id(int)` (`0x12698c00`):

```c
function set_unit_id(v, unit):                 // 0x12698c00
    WORD[v+0x0b] = ((unit & 3) << 8) + (WORD[v+0x0b] & 0xf8ff) + 0x400;   // bits 8-9 quadrant, bit 10 has-mxu
    check unit <= 3                            // "unit_id_ == unit_id" llo_value.h:408
```

| Table / callee | Address | Role | Confidence |
|---|---|---|---|
| `BuildXposeSequences` | `0x10f813a0` | group LHS: vec1 `{0xa6,0xa7}`, vec2 `{0x154}` | CERTAIN |
| `MatmulDataFormatPackingFactor` | `0x1d629300` | `int32[fmt-1] @0xb53c6bc = {1,2,4,4,4,4,8,8,4,4}` | CERTAIN |
| `Target::ChunksPerTile` | `0x1d60f2c0` | `hwcfg[+0x198] / hwcfg[+0x1a0]` | CERTAIN |
| `num_mxus` | `Target+0x4ac` | per-region MXU count | CERTAIN |
| GLM byte table | `0xac0913e` | `(op-0x9b)→GLM` : `{0×8, 0xb, 0xb}` | CERTAIN |
| `LloRegionBuilder::VlatchLsf` | `0x1d573ec0` | emit `vlatch.lsf` (`LloValue*`, GainLatchMode, int) | CERTAIN |
| `LloRegionBuilder::Vmatmul` | `0x1d575a60` | emit matmul (K-tile loop, PackingFactor× per latch) | CERTAIN |
| `LloRegionBuilder::Vmatres` | `0x1d5761a0` | emit matres | CERTAIN |
| `LloValue::set_unit_id` (inlined) | `0x12698c00` | `WORD[v+0x0b]` quadrant pack | CERTAIN |

> **QUIRK — the matmul loop runs `PackingFactor` times, not once.** Packed/nibble formats (`PackingFactor` 2, 4, or 8) emit multiple `Vmatmul` ops per latch — the K-tiling that splits the packed contracting dimension across systolic passes. A reimplementation that emits one matmul per latch under-counts the systolic steps for every format wider than bf16 (fmt 1). The `ExpectedMatresesPerMatmul` balance check downstream depends on this count.

---

## MxuStat::SequenceInfo — the Bin-Packer Output Record

### Purpose

Where `MxuSequence` is the *input* the assignment pass iterates, `MxuStat::SequenceInfo` is the *output* it produces. The bin-packer (`AssignMxusForSequenceGroupInternal`, `0x10f77ca0`) maintains, per physical MXU, a `MxuStat` struct (stride `0x28`) whose `sequences_` member is an `absl::btree_map<int, SequenceInfo>`. For each `MxuSequence`, after a greedy min-makespan select chooses which MXU it lands on, the pass inserts a `SequenceInfo` keyed by the sequence index into that MXU's `sequences_` btree, recording the sequence's two owning vectors plus a `latch_latency` and the per-MXU `accumulated_latency` snapshot.

### Layout (`0x40` bytes)

Recovered from the btree-insert field-write site (`btree_map_container<…map_params_impl<int, MxuStat::SequenceInfo>>::operator[]<int,0>` returning the value slot, followed by an 8-qword field write at `0x10f77ca0` lines ~1064-1079). The value object is `0x40` bytes laid out as **two owning vectors followed by two `long`s** — the first two words of each vector are freed-on-overwrite:

```c
struct SequenceInfo {              // 0x40 = 64 bytes; btree value
    void* vec1_begin;              // +0x00  owning vector #1 begin (freed on overwrite)
    long  vec1_end;                // +0x08  vector #1 end count
    long  vec1_cap;                // +0x10  vector #1 capacity
    void* vec2_begin;              // +0x18  owning vector #2 begin (freed on overwrite)
    long  vec2_end;                // +0x20  vector #2 end count
    long  vec2_cap;                // +0x28  vector #2 capacity
    long  latch_latency;           // +0x30  per-sequence latch latency (the `free`/new_val arg)
                                   //        CHECK "latch_latency == prev_it->second.latch_latency"
    long  accumulated_latency;     // +0x38  per-MXU accumulated_latency snapshot at select time
};
```

Both vectors are owning (allocated via `operator new`, `memcpy`-filled from caller buffers, freed-on-overwrite). Their element type is UNVERIFIED-as-`int`: the `+0x18` vector's `__throw_length_error` names `vector<CycleTable::Instruction>` (element stride `0x28`), and the `+0x00` vector copies from an `8 * count`-sized buffer (8-byte element). So neither is a plain `vector<int>`; *which* list holds the sequence's instruction set vs its result/cycle records is INFERRED, not a named source field. The two trailing `long`s are byte-exact: `+0x30` is the value the cost function `LatchLatencyChangeAfterAdding` compares against the predecessor's via the `CHECK "latch_latency == prev_it->second.latch_latency"` (`mxu_latency_balancing.cc:236`), and `+0x38` is the per-MXU `accumulated_latency` snapshot (`*(stat_array_base)`) written alongside.

> **UNVERIFIED — busy-interval framing.** An earlier reading of this page modeled `SequenceInfo` as `{latch_latency, vec1, vec2, busy_start, busy_end}` with the two `long`s as a scheduled busy *interval*. The write site does not support that: the value object's two trailing `long`s are the `latch_latency`/new-value arg and the per-MXU `accumulated_latency` snapshot, not a start/end pair, and `+0x00` is a vector begin pointer (freed on overwrite), not a `latch_latency` scalar. The cost function reads its predecessor `latch_latency` and the interval endpoints through absl btree-node-internal offsets (`9*idx+9`, `9*idx+10` qwords), which are *node* offsets, not value-struct offsets, and were not fully resolved to the two trailing `long`s. Treat the precise meaning of the cost-function reads as UNVERIFIED.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `vec1` | `+0x00` / `+0x08` / `+0x10` | owning vector (8-byte elem) | per-sequence list #1 | HIGH |
| `vec2` | `+0x18` / `+0x20` / `+0x28` | owning vector (`CycleTable::Instruction`, `0x28` elem) | per-sequence cycle/result records | HIGH |
| `latch_latency` | `+0x30` | `long` | per-sequence latch latency (CHECK-anchored) | CERTAIN |
| `accumulated_latency` | `+0x38` | `long` | per-MXU accumulated-latency snapshot | HIGH |

### The set_mxu Commit

`AssignMxusForSequenceGroupInternal` (`0x10f77ca0`) holds a `vector<InlinedVector<MxuAssignment, 4>>`; each `MxuStat` (stride `0x28`, i.e. `5 * idx` qwords) carries its `accumulated_latency` and its `sequences_` btree. The commit is a greedy min-makespan select followed by a `sequences_` insert:

```c
function AssignMxusForSequenceGroupInternal(stats, sequences, cycle_table, ...):   // 0x10f77ca0
    for seq in sequences:                               // stats: vector<InlinedVector<MxuAssignment, 4>>
        best = +INF; argmin = current
        for i = 0 .. num_mxus - 1:                      // MxuStat stride 0x28 (5 qwords)
            score = stats[i].accumulated_latency        // *(stat_base)
                  + stats[i].LatchLatencyChangeAfterAdding(seq_key, new_val, free)  // 0x10f7f3e0
                  + stats[i].free_extra                 // stat[5*i + 0]
            if score < best: best = score; argmin = i    // smaller-index tiebreak (>= keeps prior)
        check( sequences_.find(seq_index) == sequences_.end() )   // mxu_latency_balancing.cc:256, not yet assigned
        // operator[]<int,0> returns the value slot, then the 8-qword write:
        stats[argmin].sequences_[seq_key] = SequenceInfo{ vec1, vec2, latch_latency, accumulated_latency }
```

The per-MXU cost delta is `MxuStat::LatchLatencyChangeAfterAdding` (`0x10f7f3e0`), which does a `lower_bound` and a predecessor lookup in `sequences_` (the btree) and returns an interval-extension delta — byte-exact in its arithmetic, though the two btree-node-internal reads are at node offsets `9*idx+9` / `9*idx+10` qwords (not directly resolved to the value-struct fields):

```c
function LatchLatencyChangeAfterAdding(this, seq_key, new_val, free):   // 0x10f7f3e0
    a = btree_lower_bound_field(seq_key)      // node qword [9*idx+9]
    pred = predecessor(seq_key)
    if pred.key == seq_key:                   // pred.latch_latency read
        check( new_val == pred.latch_latency )  // "latch_latency == prev_it->second.latch_latency"
                                                //   mxu_latency_balancing.cc:236
        new_val = 0
    b = pred_field                            // node qword [9*idx+10]
    c  = max(0, new_val - b)
    x  = max(0, a - free)
    y2 = max(0, a - b)
    return c + x - y2                          // the delta the makespan sums (return v21 + v22 - v23)
```

The makespan select tracks the running minimum across MXUs and keeps the argmin with a smaller-index tiebreak (the `v78 <= v83` / `v78 >= v83` asymmetry at `0x10f77ca0` lines ~938-946). The cost reads only the btree interval fields and the per-MXU `accumulated_latency` — the two owning vectors are not read by the cost path.

> **NOTE — `sequences_` is a btree keyed by sequence index, not a `MxuSequence*` map.** The per-`MxuStat` assignment record is the `btree_map<int, SequenceInfo>` confirmed here (CHECK `sequences_.find(seq_index) == sequences_.end()`; the value type `MxuStat::SequenceInfo` is named in the `operator[]<int,0>` `map_params_impl<int, MxuStat::SequenceInfo>` instantiation). The btree insert and the `vector<InlinedVector<MxuAssignment, 4>>` storage are CERTAIN from this decompile. *A previously asserted parallel `flat_hash_map<MxuSequence*, long>` set-mxu commit was not found in this function and is now treated as UNVERIFIED.* The upstream that fixes each matmul's program-order quadrant *before* `LatchLhs` reads `program_order & 3` was not walked to the individual op (UNVERIFIED).

---

## How the Chain Flows Through the Pass

A bf16 LHS gain matrix latched into a 2-MXU region walks the structures in this order (`VisitRegion`, `0x10f3a640`):

```text
1. CollectAndTransformSequencesInternal  ── builds per-quadrant MxuSequences (the 5 lists)
2. LatchLhs                              ── partition LHS, emit vlatch.lsf/matmul/matres,
                                            stamp WORD[op+0x0b] = unit_id (MXU quadrant)
3. SetLatchIndices                       ── walk seq[+0x18]; WORD[op+0x42] = program-order index
                                            (first latch only if GainLatchModeHasOverrunChecks)
4. AllocateMrbEntriesAsFifo / Bounce     ── result-FIFO address + MSR-A/B bank (output side)
5. AssignMxusForSequenceGroupInternal    ── greedy min-makespan select → sequences_[key]=SequenceInfo
6. bundle packer                         ── reads WORD[op+0x42] latch index as a slot-legality input
```

The staging is fully described by the per-instruction fields: `unit_id` (`WORD[+0x0b]`, from `LatchLhs`) = which MXU quadrant the gain matrix latches into; `GainLatchMode` (`BYTE[+0x40]`) = how it is loaded (bf16 / packed); `latch_index_in_sequence` (`WORD[+0x42]`, from `SetLatchIndices`) = program order; MSR (`BYTE[+0x44]`, from `Bounce`) = which staging bank; MRB address = where the result lands. None of these are on `MxuSequence` itself — it is purely the grouping container, and `SequenceInfo` is purely the per-sequence assignment output.

---

## Related Components

| Name | Relationship |
|---|---|
| `MxuAssigner::SetLatchIndices` `0x10f3b4c0` | writes `WORD[op+0x42]` latch index onto the `+0x18` list |
| `MxuAssigner::LatchLhs` `0x10f3b5e0` | producer of the latch/matmul/matres ops + `unit_id` stamp |
| `CollectAndTransformSequencesInternal` `0x14500800` | builds the five `MxuSequence` lists |
| `AssignMxusForSequenceGroupInternal` `0x10f77ca0` | greedy makespan select + `SequenceInfo` commit |
| `MxuStat::LatchLatencyChangeAfterAdding` `0x10f7f3e0` | the latency delta the makespan sums |
| `default_delete<MxuSequence>::operator()` `0x14504c00` | the layout proof (five lists, `free(seq, 0x78)`) |

## Cross-References

- [MXU Assignment Bin-Packer](mxu-assignment-binpacker.md) — `AssignMxusForSequenceGroup`, the greedy makespan algorithm that consumes these records and produces the `SequenceInfo` output.
- [Latch Assignment & Overrun](latch-assignment-overrun.md) — `SetLatchIndices` in depth and the per-gen `GainLatchModeHasOverrunChecks` first-latch handshake.
- [MRB Chain Allocator](mrb-chain-allocator.md) — the accumulation-chain reservation timeline that consumes the matmul/matres lists.
- [MRB FIFO / MSR Placement](mrb-fifo-msr-placement.md) — `AllocateMrbEntriesAsFifo` / `BounceBetweenMsrs`, the output side that stamps the MSR (`BYTE[op+0x44]`) after `LatchLhs`.
- [MXU Slot](../isa/slot-mxu.md) — the systolic op family (`vlatch`/`vmatmul`/`vmatres`) the lists hold and the per-gen bundle encoding.
- [Matprep / IAR / Latch](../isa/slot-matprep-iar-latch.md) — the latch-op builders and the full `LloInstruction` field map (latch_mode `+0x40`, latch_index `+0x42`, MSR `+0x44`, unit-id `+0x0b`).
- [MXU Latency Overview](../cost/mxu-latency-overview.md) — the per-gen reservation model that prices the matmul/matprep occupancy these sequences schedule.
- [Scheduling Overview](overview.md) — Stage 2 (MXU sequence assignment) in the full scheduling pipeline.
