# Transpose-Reservation Latency

> *Every address, offset, ordinal, and immediate on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — `nm -C` resolves every method). `.text` and `.rodata` VMAs equal their file offsets (`.rodata` section at `0x84a0000`); `.data.rel.ro` VMA − 0x200000 = file offset. Other libtpu builds will differ.*

## Abstract

The [XLU conflict-penalty table](xlu-conflict-penalty.md) is a static `6×6×3` int32 grid that prices the worst-case structural hazard between two adjacent cross-lane ops. That table cannot capture one cost: how long the cross-lane fabric is *held* by a transpose whose hold time depends on the transpose's data shape (its height × width) and on the packing density of its `VxposeMode`. **`XposeXLUReservationLatency`** is the dynamic term that supplies exactly that — a computed (not table-looked-up) latency taken on the edge out of the *final transpose of a transpose sequence*, which **adds** a shape-dependent hold-cycle count on top of the static conflict-penalty cell.

The reference frame is the same `MCSchedModel`-style reservation idea the [MXU latency model](mxu-latency-overview.md) uses, but specialised to the cross-lane unit and to one detail upstream LLVM has no analog for: a vector transpose moves a fixed number of *packed elements per cycle*, and that throughput is a function of the operand element width. An unpacked 32-bit transpose (`B32`) moves 1 element per lane per cycle; a `Compressed B8` transpose moves 4. The off-diagonal element count `(width − height)` that must drain through the cross-lane engine therefore costs four times fewer hold cycles in `B8` than in `B32` — and that ratio is exactly `ElementCount(VxposeMode)`, the packing factor this page pins.

This page documents three byte-anchored pieces:

1. **`XposeXLUReservationLatency`** — the dynamic transpose-reservation formula, for all three latency-table shapes (base Jellyfish / GhostLite, Viperfish, Pufferfish), including the high-level dispatcher that selects it and feeds it the transpose's shape.
2. **`VxposeMode`** — the 5-ordinal transpose-mode enum, its `ElementCount` packing factors, the `LloInstruction` byte that stores it, the `VxposeMode → XluInstrType` subtable, and the per-`Target` `SupportsVectorXpose` masks.
3. **`MxuStat`** — the per-MXU running-state record the greedy min-makespan bin-packer reads and writes. `MxuStat` records the busy-interval state that the transpose/latch sequences occupy; its layout and the interval-extension cost function are pinned here.

For reimplementation, the contract is:

- The per-gen `XposeXLUReservationLatency` closed form: `static_cell + max(0, shape_term)`, where the base form divides the shape term by `2 × ElementCount(vxpose)` and VF/PF drop the divisor and add a `+7` setup constant (PF additionally floors).
- The dispatcher that selects the dynamic path on `IsFinalTransposeInSequence` and resolves `vxpose_mode` / height / width from the final-transpose op.
- The `VxposeMode` ordinals `{B32, Compressed B16, Compressed B8, Segmented B32, Segmented B16}`, the `ElementCount` table `{1,2,4,1,2}`, and the `+0x44` `LloInstruction` byte.
- The 40-byte `MxuStat` layout and the `LatchLatencyChangeAfterAdding` interval-extension delta `c + x − y2`.

| | |
|---|---|
| **Dynamic reservation (base/GL)** | `xla::jellyfish::XluConflictPenaltyTable::XposeXLUReservationLatency` `@0x1c8a0640` |
| **Dynamic reservation (Viperfish)** | `xla::viperfish::LatencyTableViperfish::XposeXLUReservationLatency` `@0x1c8a4e60` (vtable `+0x10`) |
| **Dynamic reservation (Pufferfish)** | `xla::pufferfish::LatencyTablePufferfish::XposeXLUReservationLatency` `@0x1c8a13e0` (vtable `+0x10`) |
| **Dispatcher** | `XluConflictPenaltyBetween(LloValue*, LloValue*)` `@0x1c8a01c0` |
| **Raw-cell reader** | `XluConflictPenaltyBetween(XluInstrType, XluInstrType, uint)` `@0x1c8a0180` |
| **`IsTranspose(XluInstrType)`** | `@0x1c8a04e0` — `(t − 2) < 3` ⇒ `{2,3,4}` |
| **`VxposeMode` string** | `xla::jellyfish::VxposeModeString` `@0x1d629f60` (5 cases) |
| **`ElementCount(VxposeMode)`** | `@0x1d62a140` — `int[5]` table at `0xb53c830` = `{1,2,4,1,2}` |
| **`vxpose_mode()` accessor** | `LloInstruction::vxpose_mode` `@0x1d4e7440` — `byte[inst + 0x44]` for op `0xa6`/`0xa7` |
| **`MxuStat` init / select** | `AssignMxusForSequenceGroupInternal` `@0x10f77ca0` (init `@0x10f77d30`, select `@0x10f784d0`) |
| **Interval-extension cost** | `MxuStat::LatchLatencyChangeAfterAdding` `@0x10f7f3e0`; rebalance `LatencyChangeIfMoveTo` `@0x10f7fb40` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Two Transpose-Hold-Cycle Paths

The XLU edge model charges transpose hold cycles on one of two paths, selected by whether the earlier op is the *final* transpose of a transpose sequence:

| path | selected when | cost | function |
|---|---|---|---|
| **static** | non-final / inter-op hazard | `XluConflictPenaltyTable[from][to][mxuIdx]` — a fixed `6×6×3` int32 cell | [conflict-penalty table](xlu-conflict-penalty.md) |
| **dynamic** | the earlier op `IsFinalTransposeInSequence` | the static cell **plus** a data-shape hold term | `XposeXLUReservationLatency` (this page) |

The dynamic path does not replace the static cell — it reads the *same* cell and adds the shape term. The static table prices the structural FIFO hazard; the dynamic term prices the extra cycles the cross-lane datapath stays occupied draining the transpose's off-diagonal elements. (A third, *consume-side* edge — `LatencyBetweenXposeInstrAndResult`, the latency a downstream op pays to read a final transpose's result — lives at `@0x1c8a06e0`/`@0x1c8a4fa0`/`@0x1c8a1520`, vtable `+0x30`; it is documented with the [conflict-penalty table](xlu-conflict-penalty.md).)

---

## XposeXLUReservationLatency

### Purpose

`XposeXLUReservationLatency` is a virtual method at latency-table vtable slot `+0x10`. Its demangled signature is:

```c
long XposeXLUReservationLatency(VxposeMode vx, XluInstrType from, XluInstrType to,
                                unsigned int mxuIdx, int height, int width) const;
```

`vx` is the transpose's `VxposeMode` (drives the packing divisor on the base path); `from`/`to` are the `XluInstrType` of the earlier (transpose) and later op; `mxuIdx` is the physical MXU-instance index (`unit_id & 3`, bound `< 3`) that selects the third dimension of the static cell; `height`/`width` are the transpose matrix's dimensions, both resolved from the final-transpose op `A`.

### The Dispatcher

`XluConflictPenaltyBetween(LloValue* A, LloValue* B)` `@0x1c8a01c0` is the high-level entry. It resolves both ops' `XluInstrType` via `GetXluInstrType`, asserts both carry a valid `unit_id` (`unit_id == later->unit_id()`, CHECK lines 245/246/248), and when `A` is a final transpose, takes the dynamic path through the vtable. Byte-exact:

```c
// XluConflictPenaltyBetween(LloValue* A /*earlier*/, LloValue* B /*later*/)  @0x1c8a01c0
unsigned fromType = GetXluInstrType(A);                  //  @0x1c89ff20
unsigned toType   = GetXluInstrType(B);
// unit_id is the 2-bit field in WORD[A+0xb] bits 8-9, gated by valid bit 10 (& 0x400):
unsigned mxuIdx   = (HIBYTE(*(u16*)(A + 11)) & 3);       //  asserts WORD[A+0xb] & 0x400, lines 245/248
LloInstruction* a = LloInstruction::FromValue(A);
if (a->IsFinalTransposeInSequence()) {                   //  @0x1c8a025b
    VxposeMode vx = a->vxpose_mode();                    //  @0x1c8a0267
    CHECK(IsTranspose(GetXluInstrType(A)));              //  else FATAL line 322
    int h = a->GetTransposeHeight();                     //  @0x1c8a0292
    CHECK(IsTranspose(GetXluInstrType(A)));              //  redundant guard, FATAL line 328
    int w = a->GetTransposeWidth();                      //  @0x1c8a02b9
    return (*(this->vtable + 0x10))(this, vx, fromType, toType, mxuIdx, h, w);  // virtual @0x1c8a02d7
}
// non-final: read the static cell directly (the other path)
return *((u32*)this + 18*fromType + 3*toType + mxuIdx + 2);   // == base + 72*from + 12*to + 4*mxuIdx + 8
```

`vx`, `height`, and `width` all come from the **final transpose op `A`**, never from `B`. `mxuIdx` is the *MXU-instance* axis (port asymmetry), a separate concern from `vx` (the data-format axis) — they are distinct dimensions, conflated easily because both end up as arguments to the same call (see the [VxposeMode vs cell-index note](#vxposemode-is-not-the-cell-index)).

### The Per-Gen Formulas

All three latency-table subclasses override the vtable `+0x10` slot. They share the static-cell read and differ only in how they shape the hold term. Byte-exact from the decompile:

```c
// base Jellyfish / GhostLite  @0x1c8a0640
long XposeXLUReservationLatency(vx, from, to, mxuIdx, height, width) {
    if ((unsigned)(from - 2) >= 3)  FATAL("IsTranspose(earlier)", latency_table.cc:335);
    if (to     >= 6)  ud1;                       // bound check
    if (mxuIdx >= 3)  ud1;                        // bound check
    int cell  = *(int*)(this + 72*from + 12*to + 4*mxuIdx + 8);   // raw static cell
    int shape = (width - height) / (int)(2 * ElementCount(vx));   // SIGNED idiv @0x1c8a0690
    if (shape <= 0) shape = 0;                    // cmovle to 0
    return cell + shape;                          // NO additive constant
}

// Viperfish  @0x1c8a4e60   (vtable +0x10, thunk @0x1c8a4f00)
long XposeXLUReservationLatency(vx, from, to, mxuIdx, height, width) {
    if (!IsTranspose(from))  FATAL("IsTranspose(earlier)", latency_table_vf.cc:1038);
    int cell  = XluConflictPenaltyBetween(from, to, mxuIdx);   // == raw cell @0x1c8a0180
    int shape = width - height;
    if (shape <= 0) shape = 0;                    // cmovg
    return cell + shape + 7;                       // +7 transpose-setup; NO throughput divisor
}

// Pufferfish  @0x1c8a13e0   (vtable +0x10, thunk @0x1c8a1480)
long XposeXLUReservationLatency(vx, from, to, mxuIdx, height, width) {
    if (!IsTranspose(from))  FATAL("IsTranspose(earlier)", latency_table_pf.cc:62);
    int tmp = (width - height) + XluConflictPenaltyBetween(from, to, mxuIdx);   // NO clamp, NO divisor
    if (tmp < -5)  tmp = -6;                       // cmp $0xfffffffb / mov $0xfffffffa / cmovl
    return tmp + 7;                                // floor: result >= 1 (when tmp<-5 -> 1)
}
```

The bounds (`to < 6`, `mxuIdx < 3`) and the cell address arithmetic are byte-identical to the static-table reader `XluConflictPenaltyBetween(from, to, mxuIdx)` `@0x1c8a0180` (`*(base + 72*from + 12*to + 4*mxuIdx + 8)`). The base form inlines the cell read; VF/PF call the accessor — but it is the same cell. The `from − 2 >= 3` test is `IsTranspose` inlined: a transpose op must be `XluInstrType ∈ {2,3,4}`.

### Interpretation

| gen | shape term | constant | floor |
|---|---|---|---|
| base / GL | `max(0, (width − height) / (2·ElementCount(vx)))` | none | implicit `≥ 0` from `max(0,·)` |
| Viperfish | `max(0, width − height)` | `+7` | `≥ 7` |
| Pufferfish | `(width − height)` (no clamp) | `+7` | `≥ 1` (via `tmp ≥ −6`) |

- The dominant cost is the **static conflict-penalty cell** — the worst-case structural hazard.
- On the base/GL path the hold adder is the **off-diagonal element count `(width − height)` divided by the per-mode transpose throughput** `2 × ElementCount(vx)`. `2·ElementCount(vx) = {2,4,8,2,4}` is the elements-per-cycle the cross-lane engine moves for that mode; `(width − height)` is the element count that must drain through it; their quotient is the extra hold cycles. The widest unpacked `B32` (divisor 2) costs most; the densely packed `B8` (divisor 8) costs least — the packing factor *is* the throughput speedup.
- A **square transpose** (`width == height`) pays only the static cell (plus the per-gen `+7` on VF/PF); the shape term clamps to 0.
- VF and PF **drop the divisor** (modelling the transpose as 1 element/cycle) and instead add a flat `+7` transpose-setup constant. PF additionally **floors** the result: if `cell + (width − height) < −5` it clamps to `−6`, so the returned value is `≥ 1`. This guards against a deeply negative static cell driving the reservation below one cycle.

### Worked Numbers

**Base / GhostLite** — a final `Compressed B8` transpose (`vx = 2`, `ElementCount = 4`) feeding a permute, `height = 8`, `width = 512`, `mxuIdx = 0`:

```text
shape  = (512 - 8) / (2*4) = 504 / 8  = 63        result = cell + 63
```

The same transpose as unpacked `B32` (`vx = 0`, `ElementCount = 1`):

```text
shape  = (512 - 8) / (2*1) = 504 / 2  = 252       result = cell + 252
```

The `B8` packing makes the identical transpose `4×` cheaper in XLU hold cycles — exactly the `ElementCount` ratio `4/1`.

**Viperfish** — a final `B32` transpose, `height = 128`, `width = 128` (square):

```text
shape  = max(0, 128 - 128) = 0        result = cell + 0 + 7 = cell + 7
```

A square transpose pays only the static cell plus the 7-cycle setup; a tall/wide one pays `(width − height)` more.

---

## VxposeMode

### Purpose

`VxposeMode` is the transpose data-format enum: it names the element width and whether the transpose is segmented, and it is the sole input to the per-mode throughput divisor in the base reservation formula. It is stored as a one-byte field on the transpose `LloInstruction`.

### The Enum

Five ordinals, confirmed three independent ways — the string table, the `ElementCount` packing factor, and the `XluInstrType` subtable all agree. From `VxposeModeString` `@0x1d629f60` (five `switch` cases, inline string stores), `ElementCount` `@0x1d62a140` (`int[5]` at `0xb53c830`, `xxd` = `01 00 00 00  02 00 00 00  04 00 00 00  01 00 00 00  02 00 00 00`), and the `GetXluInstrType` transpose subtable at `0xa2dcce0` (`xxd` = `02 00 00 00  03 00 00 00  04 00 00 00  02 00 00 00`):

| ordinal | `VxposeModeString` | `ElementCount` | packed elems / 32-bit lane | `XluInstrType` |
|---|---|---|---|---|
| 0 | `"B32"` | 1 | 1 (unpacked 32-bit) | 2 = `kTransposeB32` |
| 1 | `"Compressed B16"` | 2 | 2 (packed 16-bit) | 3 = `kTransposeB16` |
| 2 | `"Compressed B8"` | 4 | 4 (packed 8-bit) | 4 = `kTransposeB8` |
| 3 | `"Segmented B32"` | 1 | 1 (segmented 32-bit) | 2 = `kTransposeB32` |
| 4 | `"Segmented B16"` | 2 | 2 (segmented 16-bit) | 3 = `kTransposeB16` (default; subtable size 4) |

`ElementCount` is the number of packed elements per 32-bit lane: `B32 → 1`, `B16 → 2`, `B8 → 4`. The string-table case 0 stores the literal as three bytes (`0x3342` then `'2'` ⇒ `"B32"`); cases 1–4 are `strcpy` of the named literals. The subtable is indexed by `vxpose_mode` directly for `vx ∈ {0,1,2,3}`; `GetXluInstrType` `@0x1c89ff20` reads `subtable[vx]` and clamps `vx ≥ 4` to `XluInstrType = 3` (`kTransposeB16`), so `Segmented B16` presents as `kTransposeB16`.

### Where It Lives — `vxpose_mode()`

`LloInstruction::vxpose_mode()` `@0x1d4e7440` reads a single byte off the instruction. Byte-exact:

```c
// LloInstruction::vxpose_mode()  @0x1d4e7440
VxposeMode vxpose_mode() const {
    if ((WORD[this] & 0xFFFE) == 0xA6)            // opcode 0xa6 (Vxpose) or 0xa7 (VxposeBinary)
        return BYTE[this + 0x44];                  // the VxposeMode byte
    FATAL("LloOpcodeIsTranspose(opcode())");       // llo_instruction.cc:3367
}
```

The mask `& 0xFFFE` makes the test cover both `0xa6` (`kVectorTranspose`) and `0xa7` (`kVectorTransposeBinary`) — the pair differs only in bit 0. `VxposeMode` is the byte at `LloInstruction + 0x44`, valid only for those two opcodes; reading it on any other opcode is a hard `FATAL`.

### Per-Target Support — SupportsVectorXpose

Whether a generation can emit a given `VxposeMode` is gated by `Target::SupportsVectorXpose(VxposeMode)`. Byte-exact from each per-gen override:

| Target (gen) | `SupportsVectorXpose(vx)` | accepts |
|---|---|---|
| `JellyfishTarget` `@0x1d48f780` | `vx == 0` | `B32` only |
| `GhostliteTarget` `@0x1d497160` | `vx < 3` | `B32`, `Compressed B16`, `Compressed B8` |
| `ViperfishTarget` `@0x1d49a000` | `vx != 2` | everything except `Compressed B8` |
| `PufferfishTarget` `@0x1d4940a0` | `vx != 2` | everything except `Compressed B8` |
| `Target` (base) `@0x1d61ce00` | abstract | — |

> **NOTE —** The PF/VF `SupportsVectorXpose` mask is `mode != 2`: PF/VF support **every mode except `Compressed B8`** (`vx == 2`), not only `B8`. This matches the Pufferfish transpose emitter, which rejects `Compressed B8` with `InvalidArgument("compressed B8 format is not supported on PxC")`. (The ICF-folded `cmp esi,2; ret` thunk can read as `mode == 2` if the `setne`/`sete` polarity is misjudged; the decompiled bodies are `return a2 != 2`.) See the [XLU op roster](../isa/xlu-op-roster.md) for the full mode set.

> **NOTE — segmented modes are Pufferfish-only.** No generation reports `SupportsVectorXpose` for `Segmented B32`/`Segmented B16` (`vx` 3/4) — base = `vx==0`, GL = `vx<3`, VF/PF = `vx!=2` which does *accept* 3/4. The segmented ISA encodings exist only in the deepsea PxC (Pufferfish) instruction set; on other gens the segmented modes are reachable only via the subtable default arm, not by a confirmed emitter. Their `ElementCount` `{1,2}` and `XluInstrType` mapping are pinned, but their reservation cost is exercised only on a Pufferfish transpose.

### VxposeMode Is Not the Cell Index

A subtle trap: `XposeXLUReservationLatency` takes `vx` (a `VxposeMode`) *and* `mxuIdx` (the third index of the static `6×6×3` cell), and they are distinct axes:

- **`mxuIdx`** is the physical **MXU-instance** id (`unit_id & 3`, bound `< 3`) — the third dimension of `XluConflictPenaltyTable[from][to][mxuIdx]`, modelling per-port hazard asymmetry.
- **`vx`** is the transpose **data format** — it only feeds `ElementCount(vx)` in the base shape divisor; it never indexes the static cell.

The static cell's third index is the MXU instance, not the transpose mode. A reimplementation that uses `VxposeMode` to index the conflict-penalty cell is wrong.

---

## MxuStat — the Bin-Packer Running State

### Purpose

The MXU/latch sequences that transpose ops belong to are assigned to physical MXUs by a greedy min-makespan bin-packer (`AssignMxusForSequenceGroupInternal` `@0x10f77ca0`). `MxuStat` is the per-MXU running-state record the packer reads and writes: one per physical MXU (Jellyfish has 4). It records the accumulated makespan and a time-sorted map of the sequences occupying that MXU, each with a busy interval — the state against which the transpose-reservation cost is charged when a sequence is placed.

### Layout

`MxuStat` is 40 bytes (`sizeof = 0x28`), confirmed byte-exact from the array-init loop `@0x10f77d30` (each iteration writes the five fields then advances 5 qwords = 40 bytes) and the count arithmetic `(end − 40) / 0x28 + 1` `@0x10f7910d`:

```c
struct MxuStat {                       // 40 bytes; one per physical MXU (Jellyfish = 4)
    long          accumulated_latency; // +0x00  init 0     running per-MXU makespan term (read in the score)
    CycleTable*   cycle_table;         // +0x08  init arg    shared back-ref to the CycleTable arg
    btree_node*   root;                // +0x10  init &EmptyNode   absl::btree_map<int, SequenceInfo> root
    btree_node*   rightmost;           // +0x18  init &EmptyNode   btree rightmost-node cache
    size_t        size;                // +0x20  init 0      btree element count
};
```

The init loop writes, per record: `*p = 0` (accumulated_latency), `p[1] = cycle_table_arg`, `p[2] = p[3] = &EmptyNode` (the absl btree empty sentinel at VA `0x2181cb90`), `p[4] = 0` (size). The embedded `btree_map<int, SequenceInfo>` at `MxuStat + 0x10` keys each sequence by an int and stores its `SequenceInfo` value; each btree slot is a `pair<const int, SequenceInfo>` of 0x48 bytes (int key at slot+0, value following). The cost functions read only the value's busy interval — `busy_start` at pair `+0x38`, `busy_end` at pair `+0x40`. The full `SequenceInfo` record (its `latch_latency` and two owning `vector<int>`) is documented on [MxuSequence / SequenceInfo](../sched/mxu-sequence-struct.md); this page pins only the top-level `MxuStat` layout and the interval the cost model consumes.

The btree node layout the cost functions walk is the standard `absl::btree_node`: `node+0x0a` = element count (u8), `node+0x0b` = is-internal flag (u8, 0 ⇒ leaf), `node+0x10` = first slot (stride 0x48), `node+0x130` = child-pointer array (internal nodes only).

### Interval-Extension Cost — LatchLatencyChangeAfterAdding

`MxuStat::LatchLatencyChangeAfterAdding` `@0x10f7f3e0` is the per-`(MXU, new-sequence)` delta: how many extra busy cycles the MXU's occupied window grows by when a new latch/matmul sequence is inserted at its time-sorted slot. It locates the slot with key == arg and its predecessor, then computes the interval-extension delta. Byte-exact (tail `@0x10f7f5d5`):

```c
// MxuStat::LatchLatencyChangeAfterAdding(int key, long new_val, long free)  @0x10f7f3e0
//   locate slot with key==arg  -> found_busy_start  (= found_slot interval start)
//   locate predecessor of key  -> pred_busy_end     (= pred_slot interval end)
//   CHECK_EQ(new_val == pred_slot.latch_latency)    // "latch_latency == prev_it->second.latch_latency"
//                                                    //  FATAL mxu_latency_balancing.cc:236
long c  = max(0, new_val          - pred_busy_end);  // cmovle to 0
long x  = max(0, found_busy_start - free);           // cmovle to 0
long y2 = max(0, found_busy_start - pred_busy_end);  // cmovle to 0
return (x - y2) + c;                                 // the interval-extension delta
```

The decompiler reads the busy fields off the located btree slot at qword offsets `9·idx + 9` (`found_busy_start`) and `9·idx + 10` (`pred_busy_end`) — slot stride 9 qwords = 0x48, the `SequenceInfo` interval pair. The `CHECK_EQ` at line 236 asserts the candidate's `latch_latency` matches the predecessor's recorded value.

### The Greedy Select Loop

The select loop `@0x10f784d0` picks, for each `MxuSequence`, the MXU that minimises the resulting makespan. Byte-exact:

```c
long best = 0x7FFFFFFFFFFFFFFF;        // running min makespan
int  argmin = current_best;
for (int i = 0; i < num_mxus; i++) {   // num_mxus = Target MXU count (Jellyfish = 4)
    long delta = mxus[i].LatchLatencyChangeAfterAdding(key, new_val, free);
    long score = delta + free + mxus[i].accumulated_latency;   // accumulated = MxuStat+0x00
    if (best <= score) argmin = current_index;   // cmovle — smaller index wins ties
    if (best >  score) /* mark improved */ ;
    best = min(best, score);                       // cmovge
    /* advance mxus by stride 0x28 */
}
// assign sequence -> argmin MXU
```

The stride between `MxuStat` entries is 0x28 (40 bytes), re-confirming the layout. The score sums the interval-extension `delta`, the free-window `free`, and the MXU's `accumulated_latency`; the MXU with the minimum resulting makespan wins, ties broken toward the smaller index. The transpose-reservation latency feeds this makespan through the `CycleTable` that seeds each sequence's busy interval.

A second-pass rebalance, `MxuStat::LatencyChangeIfMoveTo` `@0x10f7fb40`, re-scores moving an already-placed sequence to another MXU (reading the same `busy_start`/`busy_end` interval, tail-calling `LatchLatencyChangeAfterAdding` at the destination). It guards against a missing/self move with `FATAL("it != sequences_.end()", mxu_latency_balancing.cc:267)`. Whether pass 2 iterates to a fixpoint or runs a single improving swap is not pinned (INFERRED single pass).

---

## Function Map

| Function | Address | Role |
|---|---|---|
| `jellyfish::…::XposeXLUReservationLatency` | `0x1c8a0640` | base/GL dynamic reservation; `cell + max(0, shape/(2·EC))` |
| `viperfish::…::XposeXLUReservationLatency` | `0x1c8a4e60` | VF override; `cell + max(0, w−h) + 7` (thunk `0x1c8a4f00`) |
| `pufferfish::…::XposeXLUReservationLatency` | `0x1c8a13e0` | PF override; floor-1, `+7`, no divisor (thunk `0x1c8a1480`) |
| `XluConflictPenaltyBetween(LloValue*, LloValue*)` | `0x1c8a01c0` | dispatcher; selects dynamic path on `IsFinalTransposeInSequence` |
| `XluConflictPenaltyBetween(InstrType, InstrType, uint)` | `0x1c8a0180` | raw static-cell reader `base + 72·f + 12·t + 4·m + 8` |
| `IsTranspose(XluInstrType)` | `0x1c8a04e0` | `(t − 2) < 3` ⇒ `{2,3,4}` |
| `GetXluInstrType(LloValue*)` | `0x1c89ff20` | op → `XluInstrType`; transpose subtable `0xa2dcce0` = `{2,3,4,2}` |
| `VxposeModeString(VxposeMode)` | `0x1d629f60` | 5-case enum-name table |
| `ElementCount(VxposeMode)` | `0x1d62a140` | `int[5]` at `0xb53c830` = `{1,2,4,1,2}` |
| `LloInstruction::vxpose_mode()` | `0x1d4e7440` | `byte[inst + 0x44]` for op `0xa6`/`0xa7` |
| `JellyfishTarget::SupportsVectorXpose` | `0x1d48f780` | `vx == 0` |
| `GhostliteTarget::SupportsVectorXpose` | `0x1d497160` | `vx < 3` |
| `ViperfishTarget::SupportsVectorXpose` | `0x1d49a000` | `vx != 2` |
| `PufferfishTarget::SupportsVectorXpose` | `0x1d4940a0` | `vx != 2` |
| `AssignMxusForSequenceGroupInternal` | `0x10f77ca0` | bin-packer; init `0x10f77d30`, select `0x10f784d0` |
| `MxuStat::LatchLatencyChangeAfterAdding` | `0x10f7f3e0` | interval-extension delta `c + x − y2` (FATAL 236) |
| `MxuStat::LatencyChangeIfMoveTo` | `0x10f7fb40` | pass-2 rebalance score (FATAL 267) |

---

## What Is Not Pinned

- The full `SequenceInfo` member roster (the two owning `vector<int>` at `+0x08`/`+0x18` and `latch_latency` at `+0x00`): byte-exact in layout, but which int-vector is the instruction-set list vs the result-chunk list is inferred from build-site provenance. Documented on [MxuSequence / SequenceInfo](../sched/mxu-sequence-struct.md). LOW on the element semantics.
- Whether VF/PF intentionally model the transpose at 1 element/cycle (the dropped `/(2·ElementCount)` divisor) or fold the throughput into the per-gen `+7`/floor constants — both are byte-exact; the design rationale is inferred.
- The `CycleTable::Instruction` per-instruction cycle accessor (`[vtable+0x10]`) that seeds each sequence's `busy_start`/`busy_end`: the summation loop is byte-exact, but the `Instruction` record body and the accessor return are taken on faith from the type name.
- The pass-2 rebalance termination (single improving swap vs iterate-to-fixpoint): `LatencyChangeIfMoveTo` control flow is byte-exact; the enclosing loop bound is INFERRED single pass.
- The `Segmented B32`/`B16` (`vx` 3/4) reservation cost is reachable only on a Pufferfish transpose (segmented ISA is PxC-only); not exercised by a confirmed emitter on other gens here.

---

## Cross-References

- [XLU Conflict-Penalty Table](xlu-conflict-penalty.md) — the static `6×6×3` cell this term adds to, the `XluInstrType` enum, and the consume-side `LatencyBetweenXposeInstrAndResult` edge.
- [XLU Op Roster](../isa/xlu-op-roster.md) — the cross-lane op family, `VxposeMode`/`ElementCount` geometry in the transpose slot-fit predicate, and the `Vxpose`/`VxposeBinaryCompressedB16` factories.
- [XLU Reemit Cost](xlu-reemit-cost.md) — `CyclesAddedByXluOperation`, the marginal-latency expression the combine/reorder stages consume.
- [XLU Combine / Source-Bus](xlu-combine-sourcebus.md) — `ComputeCombinablePairs` and the source-bus pack the XLU optimizer runs.
- [MXU Latency Overview](mxu-latency-overview.md) — the MXU-side reservation model whose `MxuLatencyTable` prices matmul/latch occupancy; the sibling of this transpose-reservation term.
- [MxuSequence / SequenceInfo](../sched/mxu-sequence-struct.md) — the full per-sequence record the bin-packer stores in each `MxuStat` btree and the `set_mxu` commit.
