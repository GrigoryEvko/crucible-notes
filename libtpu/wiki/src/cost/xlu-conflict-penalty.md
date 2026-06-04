# XLU Conflict-Penalty Table

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped — `nm -C` resolves every symbol below). `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. Other versions differ.*

## Abstract

`XluConflictPenaltyTable` is the **non-MXU structural-hazard half of the per-(op,op) latency model**. When two cross-lane ops issue back-to-back on the *same* [XLU](../isa/xlu-op-roster.md) without a true data dependency between them, the second one cannot enter the cross-lane datapath until the first has drained enough of it — a structural stall the base op-latency grid cannot see. This table prices that stall. It is the direct analog of the [`MxuLatencyTable`](mxu-latency-overview.md) reservation matrix, but at a much coarser granularity: where the MXU model keys a per-resource hold-cycle array, the XLU model collapses every cross-lane op into one of **six** `XluInstrType`s and indexes a flat `int32[from][to][vxpose]` penalty grid.

The familiar reference frame is an LLVM `SubtargetInfo::getWriteProcResBegin` forwarding-latency / read-advance table: an edge from a producer write to a consumer read carries a fixed bypass/hazard cycle count that the list scheduler adds to the dependence. `XluConflictPenaltyTable` is that idea applied to one functional unit and indexed by *operation class* rather than by register. The two ops must be on the same MXU instance (the table is per-MXU-instance, so cross-instance conflicts are not modeled here), and the `vxpose` third index — the MXU-instance id `& 3` — captures an odd/even XLU-FIFO port asymmetry that tilts the penalty per instance.

This page documents three things, each anchored byte-exactly to the binary: the `XluInstrType` enum and the `GetXluInstrType` opcode mapping; the table geometry (the cell-address arithmetic, the `stored = value + 1` "is-set" bias, the bounds checks) shared identically by the writer `SetXluConflictPenaltyBetween` and the reader `XluConflictPenaltyBetween`; and the complete per-generation penalty matrices for Viperfish, Pufferfish, and Ghostlite, transcribed directly from the three constructors' `Set` call sequences. It closes with the scheduler-side consumer — the XLU arm of `LatencyTableViperfish::LatencyBetweenInternal`, a plain-MAX reduction over the XLU hazard terms.

For reimplementation, the contract is:

- The `XluInstrType` 6-value enum and the `GetXluInstrType(LloValue*)` opcode → type mapping (including the `vxpose_mode` transpose sub-table).
- The `int32 cell[from][to][vxpose]` geometry: `cell = base + 72·from + 12·to + 4·vxpose + 8`; the `+1` store bias; the `to < 6` / `vxpose < 3` bounds, `from` unchecked.
- The per-gen install strategy: the **propagating** VF/PF override (auto-fills the `B16` packed siblings) vs the **non-propagating** Ghostlite base `Set` (every cell explicit).
- The three penalty matrices and the `IsPacked` / `Packed16Version` / `IsTranspose` classifiers.
- The high-level `XluConflictPenaltyBetween(LloValue*, LloValue*)` same-MXU check + the final-transpose virtual bypass, and the `LatencyBetweenInternal` MAX reduction that charges the cell.

| | |
|---|---|
| **Class** | `xla::jellyfish::XluConflictPenaltyTable` (embedded at owning `LatencyTable + 0x18`) |
| **Writer** | `SetXluConflictPenaltyBetween(from, to, vxpose, value)` @ `0x1c8a0140` (base, non-propagating) |
| **Cell reader** | `XluConflictPenaltyBetween(from, to, vxpose)` @ `0x1c8a0180` |
| **Value reader** | `XluConflictPenaltyBetween(LloValue*, LloValue*)` @ `0x1c8a01c0` |
| **Opcode → type** | `GetXluInstrType(LloValue*)` @ `0x1c89ff20` (transpose sub-table @ `0xa2dcce0`) |
| **Type → name** | `XluInstrTypeToString` @ `0x1c8a16a0` |
| **Cell math** | `int32`, `cell = base + 72·from + 12·to + 4·vxpose + 8`; stored `= value + 1` |
| **Bounds** | `to ∈ [0,5]` (`ud1`), `vxpose ∈ [0,2]` (`ud1`); `from` caller-guaranteed |
| **VF / PF install** | `LatencyTableViperfish::Set…` @ `0x1c8a42e0` · `LatencyTablePufferfish::Set…` @ `0x1c8a17e0` (byte-identical, propagating) |
| **Consumer** | `LatencyTableViperfish::LatencyBetweenInternal` @ `0x1c8a4ac0` (XLU arm — plain MAX) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The XluInstrType Enum

### Purpose

Every cross-lane op that the conflict model prices is first reduced to one of six `XluInstrType` values — the "non-MXU hazard alphabet." This is the table's row/column space; everything about the op except its cross-lane *kind* and *element width* is discarded. The enum splits the XLU's work into cross-lane **reduce**, **transpose** (three packing widths), and the combined **permute/rotate/broadcast** family.

### Names

`XluInstrTypeToString` @ `0x1c8a16a0` emits the literal name for each value by inline `strcpy` (the first five) or a `.rodata` reference (the sixth, 25 chars @ `0x84ddb81`). The string lengths are written alongside, so the names are byte-exact:

| value | name | width / family | `IsPacked`? | Confidence |
|---|---|---|---|---|
| 0 | `kReduceB32` | 32-bit cross-lane reduce | no | CONFIRMED |
| 1 | `kReduceB16` | 16-bit cross-lane reduce | **yes** | CONFIRMED |
| 2 | `kTransposeB32` | 32-bit transpose | no | CONFIRMED |
| 3 | `kTransposeB16` | bf16-packed transpose | **yes** | CONFIRMED |
| 4 | `kTransposeB8` | 8-bit-packed transpose | **yes** | CONFIRMED |
| 5 | `kPermuteRotateOrBroadcast` | permute / rotate / broadcast-lane | no | CONFIRMED |

The `B32`/`B16`/`B8` suffix is the element-width packing. The three **packed** variants `{1, 3, 4}` are the ones `IsPacked` (below) flags; the `B32` (and permute) types are unpacked.

### GetXluInstrType — opcode → type

`GetXluInstrType(LloValue*)` @ `0x1c89ff20` maps an [`LloOpcode`](../isa/llo-opcode-enum.md) (`WORD[value]`) to its `XluInstrType` via a `switch`. Byte-exact:

```c
int GetXluInstrType(LloValue* v):                        // sub_1C89FF20
    switch (WORD[v]):                                     // LloOpcode
        case 0x8B:  return 5;                             // kVectorSetPermutePattern → permute
        case 0xA6:  // kVectorTranspose
            m = LloInstruction::vxpose_mode(v);           // VxposeMode
            if (m >= 4) return 3;                         // out-of-range → kTransposeB16
            return xpose_subtable[m];                     // .rodata @0xa2dcce0 = {2,3,4,2}
        case 0xA7:  return 3;                             // kVectorTransposeBinary → kTransposeB16
        case 0xF5..0xFC: return 0;                        // cross-lane reduce/index → kReduceB32
        case 0xFD..0x101: return 1;                       // (the B16 reduce band) → kReduceB16
        default:
            if (WORD[v] <= 0x3B && bt(0x0C40000000000000, WORD[v]))
                return 5;                                 // bits {0x36,0x3a,0x3b} → permute/rotate/bcast
            LOG(FATAL) << "Unexpected XLU instruction: " << ToMnemonic(v);   // latency_table.cc:206
```

The transpose sub-table at `.rodata 0xa2dcce0` is four `int32`s `{2, 3, 4, 2}` (`xxd` confirmed): `VxposeMode` 0 → `kTransposeB32`, 1 → `kTransposeB16`, 2 → `kTransposeB8`, 3 → `kTransposeB32`. So a `kVectorTranspose` (`0xa6`) resolves its packing width from its `vxpose_mode`; a `kVectorTransposeBinary` (`0xa7`) is fixed at `kTransposeB16`. The reduce split is by opcode band: `0xf5..0xfc` → `kReduceB32`, `0xfd..0x101` → `kReduceB16`. The permute/rotate/broadcast group is opcodes `{0x36, 0x3a, 0x3b}` (matched by the `bt 0x0C40000000000000` bit-mask, bits 0x36/0x3a/0x3b set) plus `0x8b`.

> **NOTE —** the `vxpose_mode` axis here (the *element-width* selector inside `GetXluInstrType`) is a different thing from the `vxpose` table index (the *MXU-instance id* selector, below). The name collision is unfortunate; the first is a `VxposeMode` enum read off the transpose op, the second is `mxu_id & 3`. See [Transpose-Reservation Latency](xpose-reservation-latency.md) for the `VxposeMode` enum proper.

---

## Table Geometry

### Layout and the cell address

The penalty grid is an `int32 cell[from=6][to=6][vxpose=3]` block embedded inside the owning per-gen `LatencyTable` at `+0x18` (so the VF/PF override writes land at `LatencyTable+0x20`). The cell-address arithmetic is identical in the writer and the reader — confirmed twice:

```c
// writer  XluConflictPenaltyTable::SetXluConflictPenaltyBetween       sub_1C8A0140
// reader  XluConflictPenaltyTable::XluConflictPenaltyBetween          sub_1C8A0180
int32* cell(base, from, to, vxpose):
    if (to     >= 6) ud1;                 // bounds: to ∈ [0,5]
    if (vxpose >= 3) ud1;                 // bounds: vxpose ∈ [0,2]
    return (int32*)(base + 72*from + 12*to + 4*vxpose + 8);
    // (from is NOT range-checked — the caller guarantees from ∈ [0,5])
```

The `from` stride is `72 = 6·12` bytes (one `[to][vxpose]` plane); the `to` stride is `12 = 3·4` bytes (one `vxpose` row); the `vxpose` stride is `4` bytes (one `int32`); the `+8` is the per-block header skip. The active read region spans `base+8 .. base+8+72·6 = base+0x1b8`.

The high-level reader expresses the same address in `int32` units: `this[18·from + 3·to + vxpose + 2]` (`18·4 = 72`, `3·4 = 12`, `2·4 = 8`) — identical.

### The +1 store bias

Every `Set` variant stores `value + 1`, and the reader returns the raw stored word without subtracting:

```c
void SetXluConflictPenaltyBetween(from, to, vxpose, value):
    *cell(base, from, to, vxpose) = value + 1;            // inc — the +1 "is-set" bias

int XluConflictPenaltyBetween(from, to, vxpose):
    return *cell(base, from, to, vxpose);                 // raw stored word
```

> **GOTCHA —** the `+1` is **not** subtracted on read. A cell never written holds `0` (the sentinel for "no entry"); a cell explicitly set to a `k`-cycle penalty holds `k+1`, and the scheduler charges the **stored** value — so an explicit penalty is effectively `k+1` cycles, and a `0`-cycle penalty (`Set(...,0)`) stores `1` and so charges `1`. A reimplementation that stores the raw penalty and returns it will be off by one on every set cell and will lose the ability to distinguish "unset" from "set to 0". Store `value + 1`; charge the stored word. (All penalty tables below list the **raw `Set` argument**; the stored/charged value is that `+ 1`.)

### Initialization vs the active stride

`InitializeConflictLatency` @ `0x1c8a0040` zero-fills the block before the constructors install cells, but it walks **6 sub-blocks at an 84-byte (`0x54`) stride**, writing a `{1, 1}` (`0x0000000100000001`) header pair at each sub-block start and clearing the rest with `vmovups` of a zeroed YMM. The read/write cell math uses the **72-byte (`0x48`) from-stride**, not 84.

> **QUIRK —** the init stride (84) and the indexed-access stride (72) disagree by 12 bytes per block. The active table is the 72-byte-stride region (confirmed in both the writer and the reader); the 12-byte excess per init sub-block is reserved/header padding, and the `{1,1}` words written at each init sub-block start are **not** read by the indexed accessors. A reimplementation can ignore the 84-byte init layout entirely and zero a flat `int32[6][6][3]` plus the `+8` header skip; the discrepancy is an artifact of the init routine, not a second table. (FUNCTIONAL — the header words are not consumed by any traced read.)

---

## Per-Generation Install Strategy

The three decoded generations install their cells two different ways, and the difference is what makes the matrices look so unbalanced in size (VF 18 calls, PF 10, GL 56).

### Propagating override — Viperfish and Pufferfish

`LatencyTableViperfish::SetXluConflictPenaltyBetween` @ `0x1c8a42e0` and `LatencyTablePufferfish::SetXluConflictPenaltyBetween` @ `0x1c8a17e0` are **byte-identical** (the only difference is the `LOG(FATAL)` source string — `latency_table_vf.cc:1119` vs `latency_table_pf.cc:145`). Both are the *propagating* form: a single `Set(from, to, vxpose, value)` auto-fills the `B16`-packed siblings of `from` and `to`:

```c
void Set_propagating(from, to, vxpose, value):            // VF sub_1C8A42E0 ≡ PF sub_1C8A17E0
    CHECK(!IsPacked(from) && !IsPacked(to));              // override only sets the *unpacked* cell
    if (to >= 6) ud1;  if (vxpose >= 3) ud1;
    v = value + 1;
    *cell(base, from, to, vxpose) = v;                    // [from][to][vxpose]
    to16 = Packed16Version(to);                           // B16 sibling of `to`, if any
    if (to16.present)   *cell(base, from, to16, vxpose) = v;     // [from][B16(to)]
    from16 = Packed16Version(from);                       // B16 sibling of `from`, if any
    if (from16.present):
        vv = v + 8*(from == 3);                           // +8 iff from == kTransposeB16 (dead — see below)
        *cell(base, from16, to, vxpose) = vv;             // [B16(from)][to]
        if (to16.present) *cell(base, from16, to16, vxpose) = vv;  // [B16(from)][B16(to)]
```

So one VF/PF call writes up to **four** cells: the base unpacked cell, its `B16`-`to` column sibling, its `B16`-`from` row sibling, and the `B16`×`B16` corner. The `CHECK(!IsPacked(...))` enforces that the override is only ever *called* with unpacked args — the packed cells are reached purely by propagation. `Packed16Version` maps `kReduceB32 → kReduceB16`, `kTransposeB32 → kTransposeB16`, and reports `kPermuteRotateOrBroadcast` as having no `B16` version (so a permute `from`/`to` propagates nothing).

> **QUIRK —** the `+8*(from == 3)` bump in the propagated value is **dead** in practice. `from == 3` is `kTransposeB16`, which `IsPacked` flags — and the leading `CHECK(!IsPacked(from))` forbids ever calling the override with `from == 3`. The bump is defensive code that never fires under the actual install sequences (VF/PF only call with `from ∈ {0, 2, 5}`). A reimplementation may omit it.

### Non-propagating base — Ghostlite

`LatencyTableGhostlite` (ctor @ `0x1c8b0c00`) installs **no override**: it calls the base `XluConflictPenaltyTable::SetXluConflictPenaltyBetween` @ `0x1c8a0140` directly, which writes exactly one cell and propagates nothing. Ghostlite therefore sets **every** cell it wants explicitly, including the `B16`/`B8` packed rows and columns — 56 calls for 27 distinct `(from, to)` pairs (one pair, `kReduceB32 → kTransposeB8`, is set twice; the second write wins).

---

## The Penalty Matrices

All values below are the **raw `Set` arguments** (the table stores `value + 1`; the scheduler charges the stored value). `XluInstrType`: 0 `kReduceB32`, 1 `kReduceB16`, 2 `kTransposeB32`, 3 `kTransposeB16`, 4 `kTransposeB8`, 5 `kPermuteRotateOrBroadcast` (abbreviated `Perm`). The matrix is **directional** (`from → to`) and parameterized by `vxpose` = the MXU-instance id `& 3`.

### Viperfish — `LatencyTableViperfish` ctor @ `0x1c8a3f20`

18 explicit `Set` calls (the propagating override auto-fills the `B16` siblings). Viperfish exercises all three `vxpose` modes:

| from | to | vx=0 | vx=1 | vx=2 | Confidence |
|---|---|---:|---:|---:|---|
| `kReduceB32` | `kTransposeB32` | 40 | 57 | 40 | CONFIRMED |
| `kReduceB32` | `Perm` | 19 | 24 | 23 | CONFIRMED |
| `Perm` | `kReduceB32` | 41 | 41 | 40 | CONFIRMED |
| `Perm` | `kTransposeB32` | 33 | 52 | 37 | CONFIRMED |
| `kTransposeB32` | `kReduceB32` | 105 | 88 | 105 | CONFIRMED |
| `kTransposeB32` | `Perm` | 102 | 82 | 97 | CONFIRMED |

The `B16` siblings of these rows/columns (`kReduceB16`, `kTransposeB16`) are filled at ctor time by propagation with the **same** values — e.g. `kReduceB16 → kTransposeB16` carries the same `40/57/40` as `kReduceB32 → kTransposeB32`.

### Pufferfish — `LatencyTablePufferfish` ctor @ `0x1c8a1960`

10 explicit `Set` calls — 5 distinct `(from, to)` pairs, each installed at `vxpose 0` and `vxpose 1` with **identical** values (Pufferfish duplicates across the two modes rather than varying them):

| from | to | vx=0 | vx=1 | Confidence |
|---|---|---:|---:|---|
| `kReduceB32` | `kTransposeB32` | 56 | 56 | CONFIRMED |
| `Perm` | `kTransposeB32` | 46 | 46 | CONFIRMED |
| `kReduceB32` | `Perm` | 17 | 17 | CONFIRMED |
| `kTransposeB32` | `Perm` | 96 | 96 | CONFIRMED |
| `kTransposeB32` | `kReduceB32` | 86 | 86 | CONFIRMED |

> **NOTE —** The PF ctor (`@0x1c8a1960`) makes **10** `SetXluConflictPenaltyBetween` calls, including an explicit `(2, 0, 1, 86)` — so the `kTransposeB32 → kReduceB32` cell is set to 86 on **both** `vxpose` planes. The PF matrix is fully symmetric across `vxpose 0/1`.

### Ghostlite — `LatencyTableGhostlite` ctor @ `0x1c8b0c00`

56 explicit base `Set` calls covering 27 distinct `(from, to)` pairs (the non-propagating path sets the packed rows/columns directly). The full matrix:

| from | to | vx=0 | vx=1 | Confidence |
|---|---|---:|---:|---|
| `kReduceB32` | `kTransposeB32` | 44 | 50 | CONFIRMED |
| `kReduceB32` | `kTransposeB16` | 44 | 44 | CONFIRMED |
| `kReduceB32` | `kTransposeB8` | 36 | 32 | CONFIRMED |
| `kReduceB32` | `Perm` | 21 | 16 | CONFIRMED |
| `kReduceB16` | `kTransposeB32` | 48 | 54 | CONFIRMED |
| `kReduceB16` | `kTransposeB16` | 48 | 48 | CONFIRMED |
| `kReduceB16` | `Perm` | 25 | 20 | CONFIRMED |
| `kTransposeB32` | `kReduceB32` | 44 | 38 | CONFIRMED |
| `kTransposeB32` | `kReduceB16` | 44 | 38 | CONFIRMED |
| `kTransposeB32` | `kTransposeB16` | 35 | 29 | CONFIRMED |
| `kTransposeB32` | `kTransposeB8` | 39 | 29 | CONFIRMED |
| `kTransposeB32` | `Perm` | 40 | 29 | CONFIRMED |
| `kTransposeB16` | `kReduceB32` | 12 | 12 | CONFIRMED |
| `kTransposeB16` | `kReduceB16` | 12 | 12 | CONFIRMED |
| `kTransposeB16` | `kTransposeB32` | 3 | 9 | CONFIRMED |
| `kTransposeB16` | `kTransposeB8` | 7 | 3 | CONFIRMED |
| `kTransposeB16` | `Perm` | 8 | 3 | CONFIRMED |
| `kTransposeB8` | `kReduceB32` | 16 | 20 | CONFIRMED |
| `kTransposeB8` | `kReduceB16` | 16 | 20 | CONFIRMED |
| `kTransposeB8` | `kTransposeB32` | 15 | 25 | CONFIRMED |
| `kTransposeB8` | `kTransposeB16` | 15 | 19 | CONFIRMED |
| `kTransposeB8` | `Perm` | 4 | 3 | CONFIRMED |
| `Perm` | `kReduceB32` | 30 | 35 | CONFIRMED |
| `Perm` | `kReduceB16` | 30 | 35 | CONFIRMED |
| `Perm` | `kTransposeB32` | 29 | 40 | CONFIRMED |
| `Perm` | `kTransposeB16` | 29 | 34 | CONFIRMED |
| `Perm` | `kTransposeB8` | 17 | 18 | CONFIRMED |

> **GOTCHA —** The `kReduceB32 → kTransposeB8` pair is installed **twice** in the GL ctor: first `(0, 4, 0, 32)` / `(0, 4, 1, 28)`, then `(0, 4, 0, 36)` / `(0, 4, 1, 32)`. The second write wins, so the live stored values are **36 / 32** (raw), not the first pair's 32 / 28. A reader who stops at the first install of this cell records the wrong penalty.

### What the numbers say

The matrix encodes the XLU FIFO-push ordering hazard. A few patterns a reimplementer can lean on:

- **Direction matters.** `kTransposeB32 → kReduceB32` is the most expensive edge by a wide margin (VF `105` cycles — a full XLU drain — vs the reverse direction's far smaller cost), modeling a transpose feeding a reduce stalling the cross-lane drain pipeline. PF prices the same direction at `86`.
- **Packing scales the cost down.** The widest unpacked `B32 → B32` pairs cost the most; the `B16`/`B8` packed variants cost progressively less (GL `kTransposeB16 → Perm = 8/3`, `kTransposeB8 → Perm = 4/3`).
- **The `vxpose` index tilts the cost.** On Viperfish, `kReduceB32 → kTransposeB32` is `40 / 57 / 40` — instance 1 is `+17` more expensive than instances 0 and 2, an odd/even XLU-FIFO port asymmetry. Ghostlite likewise differs across `vxpose 0/1` (e.g. `Perm → kTransposeB32 = 29/40`). Pufferfish does **not** vary by `vxpose` (every pair is duplicated 0/1).

---

## The Value Reader — `XluConflictPenaltyBetween(LloValue*, LloValue*)`

`XluConflictPenaltyBetween(LloValue* A, LloValue* B)` @ `0x1c8a01c0` is the form the scheduler calls with two instruction values. It classifies both, enforces the same-MXU invariant, and either takes the dynamic transpose-reservation virtual or reads the static cell. Byte-exact:

```c
int XluConflictPenaltyBetween(LloValue* A, LloValue* B):           // sub_1C8A01C0
    fromType = GetXluInstrType(A);
    toType   = GetXluInstrType(B);
    // both must have a valid MXU/unit id (bit 0x400 of WORD[v+0xb])
    CHECK(A.unit_id().has_value());                                // latency_table.cc:245
    CHECK(B.unit_id().has_value());                                // latency_table.cc:246
    CHECK(A.unit_id() == B.unit_id());                             // latency_table.cc:248 — SAME MXU
    if (LloInstruction::IsFinalTransposeInSequence(A)):
        CHECK(IsTranspose(GetXluInstrType(A)));                    // latency_table.cc:322/328
        m = A.vxpose_mode();
        h = A.GetTransposeHeight();  w = A.GetTransposeWidth();
        return this->vtable[+0x10](m, fromType, toType, A.unit_id()&3, h, w);  // XposeXLUReservationLatency
    vxpose = A.unit_id() & 3;                                      // MXU instance → table index
    if (vxpose == 3) ud1;                                          // instance 3 has no table column
    return cell[fromType][toType][vxpose];
```

Two key behaviors: the **same-MXU `CHECK`** (`unit_id()` are the MXU-instance bits 8-9 of `WORD[v+0xb]`, present-gated by bit `0x400`) means this table only prices conflicts *within one MXU instance* — a `LOG(FATAL)` fires otherwise; and the **final-transpose bypass** routes a transpose that ends a sequence to the dynamic, height/width-dependent `XposeXLUReservationLatency` virtual (`vtable[+0x10]`) instead of the static cell. That virtual is documented on [Transpose-Reservation Latency](xpose-reservation-latency.md); on Viperfish (`@0x1c8a4e60`) it is `XluConflictPenaltyBetween(fromType, toType, vxpose) + max(0, width − height) + 7` — i.e. the static cell **plus** a data-shaped term.

> **GOTCHA —** the `vxpose` index is the MXU-instance id, and the reader traps (`ud1`) on instance **3**. The table only has three `vxpose` columns, but a TensorCore can have a 4th MXU instance. The conflict model assumes the two conflicting ops are never both on instance 3; an op pair landing there would crash. (No instance-3 path is reachable in the install sequences — VF/PF/GL only populate columns 0-2 — so this is a real invariant the caller must uphold, not dead defensive code.)

---

## How the Scheduler Charges the Penalty

The conflict cell is one of several XLU hazard terms `LatencyTableViperfish::LatencyBetweenInternal` @ `0x1c8a4ac0` reduces with a **plain MAX** on a non-true-dependency, non-(MXU∧MXU) edge — mirroring the MXU structural-hazard path. The XLU arm, byte-exact:

```c
long LatencyBetweenInternal(LloValue* A, LloValue* B):              // VF sub_1C8A4AC0 (vtable+0x18)
    if ((WORD[A] - 233) < 4)  return 0;                             // ops 0xe9..0xec: no edge
    base = GetLatency(A);                                           // intrinsic op latency
    true_dep = this->vtable[+0x20](A, B);                           // IsTrueDependencyBetween (vtable+0x20)
    if (!true_dep && LloOpcodeUsesMxu(A) && LloOpcodeUsesMxu(B))
        return MxuLatencyTable::GetLatencyBetween(this+0x1d8, A, B);// not-true-dep ∧ both-MXU → MXU matrix

    // --- the XLU arm (this page) ---
    // true-dependency does NOT early-return; it only suppresses the MXU branch.
    // r seeds at base when true_dep, else at 0 (the not-true-dep, non-both-MXU case).
    r = (true_dep ? base : 0);
    if (HasSetPermutePatternReservation(A, B)):                     // both involve a set-permute push, same MXU
        rsv = GetXluPathReservation(A);                             // XLU-slot occupancy gate
        r = max(base, rsv);
        r = max(r, XluConflictPenaltyBetween(GetXluInstrType(A), GetXluInstrType(B), A.unit_id()&3));
    if (IsFinalTransposeFollowedByResult(A, B)):
        r = max(r, this->vtable[+0x30](A, B, base));                // LatencyBetweenXposeInstrAndResult
    if (ArePushesToSameXluFifo(A, B)):                              // both push the same XLU FIFO, same MXU
        r = max(r, XluConflictPenaltyBetween(A, B));               // the LloValue* form (final-transpose aware)
    if (IsIndexedStoreFollowedByLoad(A, B))  r = max(r, 5);
    if (IsSetIarFollowedByIndexedLoad(A, B)) r = max(r, 8);
    if (WORD[A] == 21 && WORD[B] == 22 && r < 31) r = 30;          // cross-lane-max → max-index fixup
    if (WORD[A] == 25 && WORD[B] == 26 && r < 37) r = 36;          // cross-lane-min → min-index fixup
    return max(r, GetResourceLatency(A, B));
```

So the XLU conflict penalty is charged when two cross-lane ops on the **same MXU** push the same XLU FIFO — the structural-hazard cost between adjacent permute/reduce/transpose ops. It enters the MAX twice: once via the type-indexed cell (under `HasSetPermutePatternReservation`) and once via the `LloValue*` form (under `ArePushesToSameXluFifo`, which dispatches to the final-transpose virtual when the producer ends a transpose sequence). A *true data dependency* (`IsTrueDependencyBetween`, `vtable+0x20`) does **not** short-circuit the XLU arm — it only suppresses the both-MXU branch and seeds the running MAX `r` at `base` instead of `0`, so the conflict cell is still folded in by MAX. The both-MXU MXU-matrix branch is taken only when the edge is **not** a true dependency and both ops use the MXU.

The gating helpers (all byte-confirmed):

| Helper | Address | Gate | Confidence |
|---|---|---|---|
| `HasSetPermutePatternReservation` | `0x1c89fe00` | one of A/B is `kVectorSetPermutePattern` (`0x8b`), `LloInstructionPushesToXluFifos`, same unit id | CONFIRMED |
| `ArePushesToSameXluFifo` | `0x1c8a05a0` | both `LloInstructionPushesToXluFifos`, same unit id | CONFIRMED |
| `IsFinalTransposeFollowedByResult` | `0x1c8a0500` | A is a final-in-sequence transpose feeding B's result | HIGH |
| `GetXluPathReservation` (VF) | `0x1c8a3200` | op `0x8b` → `1` (or `8` if field `+0x40` set); else `ViperfishPerformance::GetResourceUsage(instr, 14)` | CONFIRMED |

> **NOTE —** `GetXluPathReservation` (`@0x1c8a3200`) special-cases op `0x8b` (`kVectorSetPermutePattern`): it returns `1`, or `8` when the field at `+0x40` is nonzero; every other op returns `ViperfishPerformance::GetResourceUsage(instr, /*resource=*/14)`. The `0x8b` branch also runs a soft `LloCheckForFailure` that the opcode is `kVectorSetPermutePattern`.

---

## Cross-References

- [XLU Op Roster](../isa/xlu-op-roster.md) — the LLO cross-lane opcodes (`0x36/0x3a/0x3b/0x8b/0xa6/0xa7/0xf5..0x101/…`) that `GetXluInstrType` classifies, and the XLU op-combining pipeline that runs before this cost is charged.
- [Transpose-Reservation Latency](xpose-reservation-latency.md) — `XposeXLUReservationLatency` (`vtable[+0x10]`), the dynamic height/width transpose path the final-transpose bypass routes to, and the `VxposeMode` enum.
- [XLU Combine / Source-Bus](xlu-combine-sourcebus.md) — `ComputeCombinablePairs` / `AssignSourceBus` and the distinct `PreXluAssignmentLatencyTable` (`ceil(base / xlu_count)`) optimizer edge model — not this table.
- [XLU Reemit Cost](xlu-reemit-cost.md) — `CyclesAddedByXluOperation`, the marginal-latency function the XLU optimizer consumes through the optimizer's own latency table.
- [MXU Latency Overview](mxu-latency-overview.md) — the MXU structural-hazard sibling (`MxuLatencyTable::GetLatencyBetween`) this table parallels; the second arm of `LatencyBetweenInternal`.
- [Resource Enum](resource-enum.md) — the 23-slot `ResourceVector` where `R[2] = Xlu` is the coarse cycle-weight bucket this finer per-(op,op) hazard refines.
- [LLO Opcode Enum](../isa/llo-opcode-enum.md) — the `LloOpcode` numbering that `GetXluInstrType` switches on.
