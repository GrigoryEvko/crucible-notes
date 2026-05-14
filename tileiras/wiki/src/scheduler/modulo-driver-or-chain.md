# Modulo Driver and 4-Arm OR-Chain

## Abstract

The modulo scheduler makes one placement attempt per candidate initiation interval. `sub_981D50` drives that attempt — a 215-byte trampoline that builds two per-attempt SwissTable scratches, walks the ready-group worklist, and runs a four-arm OR-chain on every ready group. Arms fire cheapest-first: a priority permutation, a DSU-based group fusion, a snapshot-driven retry, and a cost-based fallback. When every arm refuses, the driver clears its DSU snapshot and reruns the two heaviest arms from scratch. Only when the desperate retry also fails does the driver return zero and force the outer caller `sub_982210` to bump `II`.

[Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) covers the placement algorithm at fixed `II`, the resource reservation table, and the cost-based arm's scoring. This page sticks to the driver itself and the dispatch shape.

## Driver Signature

```c
__int64 sub_981D50(__int64        *schedulerState,   /* a1 */
                   __int64       ***opListPtr,       /* a2 */
                   __int64         ctx,              /* a3 */
                   __int64         groupHdr,         /* a4 */
                   __int64         aux);             /* a5 */
```

Triple indirection on `opListPtr` is deliberate: `**a2` is the first `ScheduleBlock*` of the outer worklist. `groupHdr` is a small header `{ groups*, _, numGroups, _ }` where each group occupies two `i32` slots — first the group id, then a readiness flag. `ctx` is the `ScheduleBlock` carrying the active `II`, the stage count, and the operation vector. `aux` is an opaque cookie passed through to every arm unchanged.

The 215-byte frame holds two SwissTable headers (`v36 / v37 / v38` for the outer "op-seen" table, `v39 / v40 / v41` for the inner DSU snapshot) plus a re-usable scratch `v42 / v43` that the desperate-retry block resets.

## Per-Attempt SwissTable Scratches

Both tables are rebuilt every driver call before any arm runs. They never persist across `II` values, and they never persist across calls within the same `II` — every placement attempt gets a fresh pair.

```c
/* Per-attempt SwissTable header, used twice in the driver frame. */
struct SwissTableHdr {
    void *slots;          /*+0x00 */  /* flat bucket array               */
    uint32_t size;        /*+0x08 */  /* live entry count                */
    uint32_t capacity;    /*+0x0C */  /* slot count (power of two)       */
};

/* Outer "op-seen" bucket: 16 bytes, stride 16. */
struct OuterBucket {
    uint64_t key;         /*+0x00 */  /* op handle; -4096 empty,         */
                                      /* -8192 tombstone                  */
    uint32_t size_mark;   /*+0x08 */  /* counter slot, or 0x7FFFFFFF      */
                                      /* once an arm marks the op dead    */
    uint32_t reserved;    /*+0x0C */
};

/* Inner DSU-snapshot bucket: 16 bytes, stride 16. */
struct DsuBucket {
    uint64_t op_handle;   /*+0x00 */  /* same sentinels as OuterBucket   */
    uint32_t depth;       /*+0x08 */  /* recorded schedule depth, or     */
                                      /* 0x7FFFFFFF = "dead, skip in     */
                                      /* retry"                           */
    uint32_t pad;         /*+0x0C */
};
```

`sub_95BF00` populates the outer table with the per-op callback `sub_95BEF0`. The driver packs `{ &v36, ctx }` into a `__m128i`, hands it plus `&v33` to the visitor, and the visitor walks every op on the first block, writing each op key into a slot at stride 16. The inner DSU snapshot then falls out of the outer table via the canonical Abseil SwissTable scan: skip the `-4096` and `-8192` sentinels, compute `H1` through the fmix64 helper `sub_944B20` (carrying the `0x9DDFEA08EB382D69` finalizer constant), derive `H2 = (h >> 9) ^ (h >> 4)`, linear-probe through `v39`, and route insertion through the standard Abseil load-factor rule:

```c
if (4 * (size + 1) < 3 * capacity) {
    /* fast direct insert */
} else if (capacity - tombstones - (size + 1) <= capacity / 8) {
    /* rehash in place — tombstones dominate the table   */
} else {
    /* double capacity via sub_8FC010 / sub_8F9240,
     * clear new slots to -4096, rehash each live entry   */
}
```

The outer table is the "op-seen header" arms consult to recognise ops already on the worklist. The inner DSU snapshot is a `{ op_handle -> depth }` map — the only piece of state the driver hands sideways into Arms 3 and 4, which read it to decide whether each op is still placeable at the current attempt.

## 4-Arm OR-Chain

For every group whose readiness flag clears the `(unsigned)gid > 0xFFFFFFFD` and `flag < 0` filters, the driver runs four arms in fixed dispatch order. The chain short-circuits on the first arm that returns true. The 6th argument carrying the DSU snapshot reaches only Arms 3 and 4 — Arms 1 and 2 consult the scheduler-level DSU at `schedulerState + 112` directly.

```c
for (int *g = groupArrayBase; g != groupArrayEnd; g += 2) {
    if (skip_unready_or_sentinel(g)) {
        continue;
    }

    int gid = *g;       /* a4 of every arm is the group id, not a mode */

    if ( sub_978B50(state, opListPtr, ctx, gid, aux)              /* arm 1: PERMUTE */
      || sub_97D050(state, opListPtr, ctx, gid, aux)              /* arm 2: FUSE    */
      || sub_9661B0(state, opListPtr, ctx, gid, aux, &dsuSnap)    /* arm 3: RETRY   */
      || sub_980290(state, opListPtr, ctx, gid, aux, &dsuSnap) ) {/* arm 4: CBS     */
        continue;   /* group placed; advance worklist */
    }

    /* All four arms refused. Desperate retry with a fresh empty
     * snapshot — re-runs only the two heavy completeness arms.    */
    SwissTableHdr fresh = { .slots = NULL, .size = 0, .capacity = 0 };
    bool ok = sub_9661B0(state, opListPtr, ctx, gid, aux, &fresh);
    sub_4560420(fresh.slots, 16ULL * fresh.capacity, 8);
    if (!ok) {
        fresh = (SwissTableHdr){ NULL, 0, 0 };
        ok = sub_980290(state, opListPtr, ctx, gid, aux, &fresh);
        sub_4560420(fresh.slots, 16ULL * fresh.capacity, 8);
        if (!ok) {
            return 0;   /* outer caller sub_982210 bumps II */
        }
    }
}
return 1;
```

Arm signatures differ only in arity. Arms 1 and 2 are `(state, opListPtr, ctx, gid, aux)`; Arms 3 and 4 add a 6th `SwissTableHdr *` parameter for the snapshot. Every arm receives the same `gid` value — the long-standing "pass-mode enum" reading of `a4` was wrong. The pass mode is purely positional, decided by which arm address the driver calls.

Arm 2 carries one runtime-dispatched branch on `gid == 4`. Line 682 of `sub_97D050` reads `v390 = (a4 == 4)`, then propagates the flag through a 3-tuple aux bundle `{ ctx, &gid, &is_combiner }` to comparator `sub_962F50` lines 138-139, which selects between the inline `vec0` (gid ≠ 4) and `vec1` (gid == 4) views within a 208-byte SwissTable bucket. The branch also picks between `sub_9706D0` (with-buffer `stable_sort` after a successful `get_temporary_buffer`) and `sub_97CFD0` (no-buffer `stable_sort`). The `4` is the combiner-mode sentinel gid; no other `gid` value reaches that code path.

## Per-Arm Roster

Each arm pulls from a distinct set of placement primitives, which is what makes them statically separable in the disassembly. PERMUTE reads the scheduler-level DSU directly and commits through `sub_96A820`; FUSE walks pairwise group merges through `sub_95AFC0` / `sub_95B220`; RETRY consumes the snapshot via the Abseil fmix64 probe with the depth-viability gate; CBS, the heaviest, delegates to the cost-evaluator ladder from a sibling strand. The table below records each arm's entry point, size, role, and the callees that disambiguate it from its neighbours.

| Arm | Address | Size | Role | Distinguishing callees |
|---|---|---|---|---|
| 1 PERMUTE | `sub_978B50` | 11 KB | Priority-bucket permutation heuristic; the cheapest arm, fires first. Sorts each priority bucket by predecessor distance via comparator `sub_962F50` with the `4*A vs B` priority-delta clamp; commit-probes via `sub_96A820`; on success calls `sub_976BE0` (DSU find) at line 1235 and `sub_976DE0` (DSU union) at line 1298 to merge the placed group into the fixed schedule. Reads the scheduler-level DSU at `schedulerState + 112` directly — no snapshot. | `sub_9621A0`, `sub_966070`, `sub_96A820`, `sub_96A8B0`, `sub_962F50` |
| 2 FUSE | `sub_97D050` | 12.3 KB | Combiner-mode pairwise group fusion via DSU. Walks every two placed groups and tries to merge them, gated on MLIR `isProperAncestor` + property-typeID sentinels. The only arm with a runtime-dispatched specialisation (`gid == 4` combiner branch). | `sub_95AFC0`, `sub_95B220`, `sub_94C4F0`, `sub_94C5A0`, `sub_9706D0`, `sub_97CFD0` |
| 3 RETRY | `sub_9661B0` | 11 KB | Two-phase DSU-snapshot retry. Phase A consults the snapshot's `H2` probe to short-circuit on sentinel `0x7FFFFFFF` ("op already dead — skip in retry"). Phase B sorts each op's predecessor list via in-place `sub_94B190` or out-of-place `sub_94B8C0` with a `sub_44A8C50`-allocated spill. Phase C runs the canonical three-round Abseil fmix64 probe `0x9DDFEA08EB382D69 * (HIDWORD(x) ^ ((8*x & 0x7FFFFFFF8) - 0xAE502812AA7333))` with a depth-viability gate `*((u32*)sub_94A550(ctx, &op) + 2) <= v343`. Commits via three calls to the RRT teardown-and-re-emit primitive `sub_8DDEF0` at lines 1001, 1136, 1171. Phase E re-runs B–D over the remaining unplaced ops. 2171 decompiled lines. | `sub_8DDEF0`, `sub_94A550`, `sub_94B190`, `sub_94B8C0`, `sub_44A8C50`, `sub_448DF00` |
| 4 CBS | `sub_980290` | 6.8 KB | `CostBasedScheduleGenerator` from sibling strand H02. Same 6-argument signature as Arm 3; consumes the same DSU snapshot. Heaviest of the four — delegates to the cost-evaluator ladder documented under [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md). | (cross-strand; see H02) |

The four arms keep non-overlapping callee sets outside the shared readyList / priority / reseed quintet (`sub_958C00`, `sub_959B30`, `sub_959A10`, `sub_93E7D0`, `sub_93E480`) and the shared allocator group (`sub_45603F0`, `sub_4560420`, `sub_456A580`). Those distinguishing callees are how the driver's static disassembly partitions into four arms in the first place.

## Arm 2 FUSE: stable_sort Cluster and StructConstraint Comparator

Arm 2 at `sub_97D050` performs combiner-mode pairwise group fusion via DSU, and it is the only arm with a runtime-dispatched specialisation. Its Phase B sorts a vector of 64-byte `StructConstraint` records via libc++ `std::stable_sort`, dispatching at runtime between a with-buffer driver and a no-buffer driver depending on whether `std::get_temporary_buffer` succeeded.

The libc++ `std::stable_sort` template fully inlines against the 64-B `StructConstraint` element type, producing seven recognisable functions. The 7.2-KB body at `sub_96EAC0` is the recursive merge step the with-buffer driver invokes — its size comes from HexRays expanding the libc++ template against a 64-B element, not from template explosion in the original C++.

| Address | Size | Role in libc++ idiom |
|---|---|---|
| `sub_950050` | 306 B | `std::get_temporary_buffer`. Canonical halving-on-failure malloc loop plus SSO-aware `__uninitialized_copy` of 64-B `StructConstraint` elements. Each element is initialised with `*ptr = ptr+2; *(ptr+1) = 0x600000000` — the cap=6/size=0 inline-SmallVector tail. |
| `sub_9706D0` | 90 B | `__stable_sort_move` with-buffer driver. Entered when get_temporary_buffer returned a non-null buffer. |
| `sub_97CFD0` | 80 B | `__stable_sort_with_no_buffer` no-buffer driver. 14-element insertion-sort switch matching libc++'s `_LIBCPP_STABLE_SORT_SWITCH` value `896 / 64`. |
| `sub_96EAC0` | 7.2 KB | `__buffered_inplace_merge`. The recursive merge step the with-buffer driver invokes. |
| `sub_97BBF0` | 7.5 KB | `__merge_without_buffer`. Bufferless rotate-merge that calls the GCD-cycle `std::rotate` at `sub_944840`. |
| `sub_964AC0` | ~360 B | Chunk insertion plus bottom-up merge. |
| `sub_963B10` | 2.0 KB | Insertion-sort body for runs below the switch threshold. |

Inside the 7.2-KB body at `sub_96EAC0` sit eight repeated `StructConstraint]` TypeID intern stubs, each guarded by `__cxa_guard_acquire` (`sub_44A8A10`) / `__cxa_guard_release` (`sub_44A8AC0`) around the TypeID factory `sub_44A6CA0`. The eight stubs match the eight call sites of the comparator probe `sub_962B10` inside the merge recursion. The `StructConstraint` TypeID is interned exactly once via `sub_44A6CA0("mlir::nv_tile_ir::as::schedule_utils::StructConstraint]", 54)`; subsequent calls hit the guard and return the cached pointer.

The comparator chain runs `sub_962B10` → `sub_957D10`. `sub_957D10` reads the 3-tuple aux bundle `{ ctx, &gid, &is_combiner = (gid == 4) }` the FUSE arm assembled before entering the sort. Phase B's sort threads the `is_combiner` flag through, but only the priority-bucket inner comparator `sub_962F50` at lines 138-139 consumes it — selecting between the inline `vec0` view (gid ≠ 4) and the inline `vec1` view (gid == 4) within a 208-byte SwissTable bucket. The two inline views are the first and second `SmallVector` tails of the same 64-B `StructConstraint` record; `gid == 4` switches which tail the comparator keys on.

That `gid == 4` test is the only magic number in the four-arm OR-chain. It flips a single bit that routes the comparator to read the second SmallVector tail (the combiner / fused subgroup view) rather than the first. Every other reference to `gid` in the chain is positional — a small integer the driver hands to each arm verbatim. No enum, no mode field, no second magic value: the entire combiner-vs-non-combiner distinction collapses to one equality test against the literal 4.

Phase B as a self-contained pseudocode reduction:

```c
bool sortConstraints(StructConstraint *v, size_t n, Schedule *S, uint32_t gid) {
    bool      is_combiner = (gid == 4);
    AuxTuple  aux         = { .ctx = S, .gid = &gid, .is_combiner = &is_combiner };

    void *buf = sub_950050(v, n);          /* std::get_temporary_buffer */
    if (buf) {
        sub_9706D0(v, buf, n, &aux);       /* with-buffer stable_sort   */
    } else {
        sub_97CFD0(v, n, &aux);            /* no-buffer  stable_sort    */
    }
    return true;
}
```

Both paths produce bit-identical output for tied keys — the whole reason libc++ exposes the buffer-failure fallback as a separate driver rather than degrading to a non-stable sort. Tileiras leans on it: ties in the `StructConstraint` order must resolve identically across builds, because the next pass commits fusions in iteration order over the sorted vector.

## Desperate Retry and DSU Reset Semantics

The block at the bottom of the OR-chain is not a true DSU reset. The scheduler-level DSU at `schedulerState + 112` — the group-leader parent-pointer table populated upstream by `sub_97B770`'s attribute parser — stays untouched between attempts. Only the per-attempt snapshot resets.

```c
/* Same II, same attempt, but the four-arm chain refused.
 * Clear the local snapshot header, free its slot array, and
 * re-run the two heavy completeness arms from a known-empty
 * starting state. The scheduler-level DSU is unchanged.       */

LODWORD(v43) = 0;
v42 = _mm_setzero_si128();        /* fresh empty SwissTableHdr  */
ok = sub_9661B0(state, opListPtr, ctx, gid, aux, v42.m128i_i64);
sub_4560420(v42.m128i_i64[0], 16ULL * (uint32_t)v43, 8);
```

A second identical block reruns `sub_980290` when Arm 3b fails. Clearing the snapshot wipes the `0x7FFFFFFF` "skip" markers previous arms wrote into snapshot `depth` slots — the pre-built snapshot accumulates "ops already considered dead" as the chain progresses, so by the time Arm 3 sees it many ops are already off-limits. The fresh-state retry trades that pre-seeded knowledge for from-scratch semantics: the same arms place more aggressively when they aren't constrained by earlier arms' negative results.

If even the fresh-state retry returns false, the driver writes `v15 = 0` and falls through to the cleanup label. Both SwissTable arrays are freed unconditionally via `sub_4560420(slots, 16 * capacity, 8)` on the way out, and the zero return propagates to `sub_982210`, which bumps `II` and reruns the whole pass.

## State Machine of One Driver Call

```text
[ENTRY]  sub_981D50(schedulerState, opListPtr, ctx, groupHdr, aux)
          |
          v
[SETUP]   sub_95BF00 + sub_95BEF0  -> outer "op-seen" table  (v36)
          Abseil SwissTable scan   -> inner DSU snapshot     (v39)
          |
          v
[for each ready group g in groupHdr, skip sentinels]
          |
          v
[OR_CHAIN] arm_1 PERMUTE  sub_978B50(state, ops, ctx, gid, aux)
            succeed? --> next group
           arm_2 FUSE     sub_97D050(state, ops, ctx, gid, aux)
            succeed? --> next group
           arm_3 RETRY    sub_9661B0(state, ops, ctx, gid, aux, &v39)
            succeed? --> next group
           arm_4 CBS      sub_980290(state, ops, ctx, gid, aux, &v39)
            succeed? --> next group
            else     --v
          |
          v
[DESPERATE RETRY] fresh empty snapshot &v42:
           arm_3b RETRY2  sub_9661B0(state, ops, ctx, gid, aux, &v42)
            succeed? --> next group
           arm_4b CBS2    sub_980290(state, ops, ctx, gid, aux, &v42)
            succeed? --> next group
            else --> return 0  -->  outer sub_982210 bumps II
          |
          v
[CLEANUP] free outer + inner SwissTable arrays, return 1
```

## Usage and Contract

The modulo scheduler `sub_982210` invokes the driver once per candidate `II`. It consumes the scheduler state (carrying the active `II`, stage count, global RRT, and the DSU at `state + 112` seeded by the constraint-attribute parser), the worklist of ready operations, the group header describing per-group readiness, and an opaque cookie threaded to every arm. Its boolean return value tells the outer scheduler whether to keep the current `II` or bump it; on success, Arms 1, 3, or 4's commit paths have mutated the schedule state in place, and the materializer reads the final `(stage, order)` placement off the per-op slot. The driver itself owns no persistent state — every SwissTable scratch it allocates is freed before return on every exit path.

## Cross-References

[Modulo Scheduler and Rau](modulo-scheduler-and-rau.md) documents the placement algorithm at fixed `II` and the arm-level scheduling model. [Resource Constraint Builder and RRT](resource-constraint-builder-and-rrt.md) covers the footprint tables that Arms 1 and 3 commit through. [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) covers Arm 4's cost ranking. [Serial vs Cost-Based Generators](serial-vs-cost-based-generators.md) explains where the cost-based arm fits in the broader generator family. [Schedule Constraint Attributes](schedule-constraint-attributes.md) documents the DSU at `state + 112` that the FUSE arm consults directly.
