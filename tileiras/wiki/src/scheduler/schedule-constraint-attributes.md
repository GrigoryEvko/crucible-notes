# Schedule Constraint Attributes

## Abstract

Before the modulo scheduler runs, `sub_97B770` parses every op carrying a `tileas.schedule.constraint.*` or `tileas.*` attribute. The 1037-byte routine extracts nine well-known attribute strings off the op and folds them into a `ConstraintMap` consulted at scheduling time. The map drives two subsystems: the placement driver reads four pipeline-control fields per op, and the rematerialisation passes read five remat-policy fields. The parser also seeds a disjoint-set-union structure at `state + 112`, unifying ops that share a `leader_gid` so the driver later treats them as a single fused group.

This page covers the attribute strings, the storage layout of the `ConstraintMap` slot, the two-step inherent-then-discardable lookup, and the DSU seeding that ties the parser to the driver.

## Parsed Attribute Strings

Nine attribute strings come off every op, split between two consumer groups. Four feed the placement driver — `gid`, `leader_gid`, `max_depth`, and the `force_serial_execution` unit attribute. The remaining five govern rematerialisation policy: `preferred_atom_size`, `max_num_slices_for_non_reduce_axis`, `max_num_of_recomputations`, plus the unit attributes `enable_defusion_if_fusion_extending_liveness` and `recomputable`. Frontends may emit any subset; absent strings leave the matching slot field at its zero-fill default.

| String | Type | Consumer | Role |
|---|---|---|---|
| `tileas.schedule.constraint.gid` | i32 | placement driver | op's group id |
| `tileas.schedule.constraint.leader_gid` | i32 | placement driver | group-leader gid for DSU union |
| `tileas.schedule.constraint.max_depth` | i32 | placement driver | viability gate for retry arm (G2 admission) |
| `tileas.schedule.constraint.force_serial_execution` | UnitAttr | placement driver | forces sequential placement of this op |
| `tileas.preferred_atom_size` | i32 | remat pass | preferred atom size for slicing |
| `tileas.max_num_slices_for_non_reduce_axis` | i32 | remat pass | per-axis slice cap |
| `tileas.max_num_of_recomputations` | i32 | remat pass | recomputation budget |
| `tileas.enable_defusion_if_fusion_extending_liveness` | UnitAttr | remat pass | allows defusion when fusion grows liveness |
| `tileas.recomputable` | UnitAttr | remat pass | marks the op as recomputable |

The parser keeps the verbatim attribute strings in its read-only string table and matches them by pointer-or-content compare against the op's attribute dictionary keys.

## Two-Step Lookup

`sub_97B770` tries the inherent attribute dictionary first, then falls back to the discardable dictionary. Inherent attributes live in the op's `Properties` storage and survive cloning; discardable attributes sit in a `DictionaryAttr` on the op header and do not. Frontends emit scheduling constraints as inherent properties when the op definition reserves a property slot for them, and as discardable attributes otherwise.

```c
Attribute lookupAttr(Op *op, StringRef key) {
    if (Attribute a = sub_446DC50(op, key))    /* inherent dict */
        return a;
    return sub_440E370(op, key);               /* discardable dict */
}
```

`sub_446DC50` is the inherent-attribute accessor; `sub_440E370` is the discardable one. The parser invokes the pair once per attribute string and takes the first non-null return as the value.

## ConstraintMap Layout

The `ConstraintMap` keys on the op handle. `sub_94A550(state, op)` returns a pointer to a 16-byte record carrying the placement-driver fields, plus three i32 fields immediately after it for the remat numerics:

```c
/* Slot returned by sub_94A550. Stride 28 bytes; placement driver reads */
/* the first 16, remat passes read the trailing 12.                     */
struct ConstraintSlot {
    uint32_t gid;          /*+0x00 */  /* tileas.schedule.constraint.gid */
    uint32_t leader_gid;   /*+0x04 */  /* leader gid for DSU union       */
    uint32_t max_depth;    /*+0x08 */  /* viability gate (G2)            */
    uint32_t flags;        /*+0x0C */  /* bit 0: force_serial_execution  */
                                       /* bit 1: recomputable            */
                                       /* bit 2: enable_defusion_if_     */
                                       /*        fusion_extending_       */
                                       /*        liveness                */
    uint32_t preferred_atom_size;                       /*+0x10 */
    uint32_t max_num_slices_for_non_reduce_axis;        /*+0x14 */
    uint32_t max_num_of_recomputations;                 /*+0x18 */
};
```

The placement driver reads `max_depth` via `*((u32*)slot + 2) <= 1` — that direct word load is the G2 admission gate documented in [Serial and Cost-Based Schedule Generators](serial-vs-cost-based-generators.md). All three UnitAttr flags share the same i32 so the driver can probe them with a single masked compare.

## DSU Seeding at state+112

A union-find structure sits at offset `+112` from the scheduler state base. `sub_976BE0` is the find primitive with path compression; `sub_976DE0` is the union primitive. The parser uses both to fold every op sharing a `leader_gid` into the same group:

```c
void parseConstraints(Op *op, void *state, ConstraintMap *map) {
    ConstraintSlot s = {0};

    if (Attribute a = lookupAttr(op, "tileas.schedule.constraint.gid"))
        s.gid = a.getInt();
    if (Attribute a = lookupAttr(op, "tileas.schedule.constraint.leader_gid"))
        s.leader_gid = a.getInt();
    if (Attribute a = lookupAttr(op, "tileas.schedule.constraint.max_depth"))
        s.max_depth = a.getInt();
    if (lookupAttr(op, "tileas.schedule.constraint.force_serial_execution"))
        s.flags |= 1u << 0;

    if (Attribute a = lookupAttr(op, "tileas.preferred_atom_size"))
        s.preferred_atom_size = a.getInt();
    if (Attribute a = lookupAttr(op, "tileas.max_num_slices_for_non_reduce_axis"))
        s.max_num_slices_for_non_reduce_axis = a.getInt();
    if (Attribute a = lookupAttr(op, "tileas.max_num_of_recomputations"))
        s.max_num_of_recomputations = a.getInt();
    if (lookupAttr(op, "tileas.enable_defusion_if_fusion_extending_liveness"))
        s.flags |= 1u << 2;
    if (lookupAttr(op, "tileas.recomputable"))
        s.flags |= 1u << 1;

    if (s.leader_gid != s.gid) {
        sub_976DE0((char *)state + 112, s.gid, s.leader_gid);   /* DSU union */
    }

    map->insert(op, s);
}
```

DSU seeding is the parser's only side effect outside the map. It runs once per op during parsing, so the driver sees a fully-built DSU before its first arm fires.

## Usage and Contract

The parser runs once per op at scheduler-init time, before any placement arm fires. It consults the op's inherent properties dictionary first and falls back to the discardable attributes dictionary, reading only the nine string keys listed above — every other attribute on the op is ignored. Two outputs reach the rest of the scheduler. The first is the per-op `ConstraintSlot` keyed by op handle inside the `ConstraintMap`, retrieved by every later consumer through `sub_94A550(state, op)`. The second is the seeded disjoint-set forest at `state + 112`, written only when an op's `leader_gid` differs from its `gid`. Frontends emitting the constraint attributes must keep `leader_gid` consistent across every op in a fusion group — the parser does no symmetry check, and a divergent group will produce two DSU roots that the placement driver treats as independent.

## Reimplementation Invariants

- Parse all nine attribute strings off each op; do not skip strings just because the op type does not normally carry them.
- Try inherent attributes first via `sub_446DC50`, then discardable via `sub_440E370`; never reverse that order.
- Pack the three UnitAttr flags into a single i32 at slot offset `+12`; keep bit 0 for `force_serial_execution`, bit 1 for `recomputable`, bit 2 for `enable_defusion_if_fusion_extending_liveness`.
- Store `max_depth` at slot offset `+8` so the driver's G2 gate can read it as `*((u32*)slot + 2)`.
- Seed the DSU at `state + 112` with every op whose `leader_gid` differs from its `gid`; this is what makes ops with a shared `leader_gid` co-fuse later.
- Run parsing once before the modulo scheduler, not lazily during placement.

## Cross-References

[Modulo Driver and 4-Arm OR-Chain](modulo-driver-or-chain.md) documents the placement driver that reads the `max_depth` G2 admission gate and consults the DSU built here. [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) documents the cost-based arm that honours `force_serial_execution`. [Serial and Cost-Based Schedule Generators](serial-vs-cost-based-generators.md) explains the G2 viability check that gates retry.
