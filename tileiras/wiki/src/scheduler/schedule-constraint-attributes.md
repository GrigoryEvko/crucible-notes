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

> ⚡ **QUIRK — inherent and discardable can disagree, inherent always wins silently**
> If an op carries the same constraint key in both its inherent properties slot and its discardable dictionary with different values — which can happen when a pass copies an attribute forward without removing the source — the parser commits the inherent value and never even reads the discardable one. There is no diagnostic, no warning, and the discardable side stays on the op as a dangling shadow that the next dump pretty-prints alongside the value actually in force. A frontend that round-trips IR through textual form and re-parses risks promoting the shadow into the inherent slot on the second pass and silently flipping the scheduler decision.

Integer-valued attributes are unwrapped through the standard `IntegerAttr::getInt()` truncation: any storage width is narrowed to a signed 64-bit value, then reinterpreted as a 32-bit unsigned field when written into the slot. UnitAttr keys are tested for presence only — the parser does not read the unit attribute's content, so a non-UnitAttr value living under one of the unit keys (which the verifier should reject upstream) still trips the flag bit. The five integer fields default to zero when their attribute is absent; the three flag bits default to clear. The parser does not distinguish "explicitly set to zero" from "absent" for the integer fields, so a `max_depth = 0` attribute behaves identically to a missing one — both make the G2 admission gate fire on every retry-arm attempt.

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

The placement driver reads `max_depth` via `*((u32*)slot + 2) <= 1` — that direct word load is the G2 admission gate documented in [Serial and Cost-Based Schedule Generators — G2: Max-Depth Viability](serial-vs-cost-based-generators.md#g2-max-depth-viability). All three UnitAttr flags share the same i32 so the driver can probe them with a single masked compare.

The 28-byte stride is not the natural sum of seven `uint32_t` fields rearranged for cache — it is the layout the placement driver hard-codes through direct word indices. `sub_94A550` returns a base pointer and the consumers index it with `*((u32*)slot + n)` for `n ∈ {0..6}`. There is no struct definition shared between parser and consumers; the layout exists only as a calling convention spelled out in word offsets at every read site. A reimplementation that reorders these fields must update every consumer simultaneously or the masked-compare in the placement driver reads `flags` from the `max_depth` slot and gates retry on the wrong word.

The split between the placement-driver words (`+0x00`–`+0x0C`) and the remat-pass words (`+0x10`–`+0x18`) matches the consumer split exactly: the placement driver reads only the first 16 bytes, the remat pass reads only the trailing 12. Neither side ever touches the other's region, and the parser zero-fills the slot before writing, so a placement-driver read of a remat-only-tagged op sees zeroed `gid`/`leader_gid`/`max_depth`/`flags` and falls through every gate to the default behaviour.

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

> ⚡ **QUIRK — leader_gid defaults to zero, which is itself a valid gid**
> When an op carries no `leader_gid` attribute, the zero-fill default leaves `s.leader_gid == 0`. The parser then compares against `s.gid`, and any op whose `gid` is non-zero ends up unioned with the phantom gid-0 root — silently grafted onto whichever real gid-0 group exists in the same scheduler state. A frontend that uses gid 0 for "the entry group" and then forgets to set `leader_gid` on a gid-7 op will see that op fused with the entry group and scheduled as if it belonged there. The fix at the frontend is to always emit `leader_gid` equal to `gid` for ops that lead their own groups, since the parser cannot tell "absent" from "explicitly zero". Tileiras' own emitters do this; ad-hoc IR test inputs frequently do not.

> ⚡ **QUIRK — DSU union direction is gid → leader_gid, not symmetric**
> `sub_976DE0` takes `(state+112, child, parent)` in that order and grafts the `child` root under the `parent` root before path compression runs. The parser passes `(s.gid, s.leader_gid)`, so the gid root becomes a child of the leader_gid root and every later `find(gid)` returns the leader's root. If two ops in the same intended group disagree on which side is "leader" — `op_a` says `leader_gid = 7, gid = 3` while `op_b` says `leader_gid = 3, gid = 7` — the two unions cancel out into a chain `7 → 3` then `3 → 7` and one of the two roots ends up parenting the other depending on parse order. The placement driver's leader-consistency check at G4 will then occasionally pick the wrong leader and treat the group as split when probed against the third op. There is no diagnostic; the symptom is non-deterministic schedule output across builds with the same input.

## Twin Seeding: DSU and Pending-Set

The parser does not seed one scheduler-state structure but two, and the pair is the full picture of how the attribute pass primes downstream scheduling. The DSU at `state + 112` is one half; an Abseil-layout SwissTable pending-set at `state + 392` (`49 * 8` bytes past the state base) is the other. The parser fills both in the same walk, and both stay frozen for the rest of the schedule.

| Structure | Offset | Shape | Consumer |
|---|---|---|---|
| Disjoint-set forest | `state + 112` | Parent-pointer DSU, `find` with path compression, directional `union(child=gid, parent=leader_gid)` — no rank, no size heuristic | Placement arms — fuse and retry consult it to keep group leaders consistent |
| Pending-set | `state + 392` | SwissTable, control-byte sentinels `0x80` / `0xFE` / `0xFF`, fmix64 group hash | Cost-based generator's gate G1 |

The DSU records the must-fuse equivalence classes implied by `leader_gid`. Every op whose `leader_gid` differs from its `gid` is unioned with its leader, so the resulting forest's roots are the actual scheduling groups. Placement arms walk the DSU through `find` whenever they need to know whether two candidate ops belong to the same group; the gate-G4 leader-consistency check in [Serial vs Cost-Based Generators — G4: Leader-Group DSU Consistency](serial-vs-cost-based-generators.md#g4-leader-group-dsu-consistency) is the highest-traffic consumer.

The pending-set records ops that have been temporarily removed from consideration — the carry state the cost-based generator uses to hold a candidate over to the next placement attempt without permanently failing it. The gate-G1 membership probe is a single SwissTable `find` against this table; rejection means "skip this op for this iteration, try again next round." The parser populates the table once at scheduler-init time so the very first gate-G1 probe has a fully-built table to consult.

A reimplementation must seed both structures from the same walk. Splitting the seeding into two passes risks the gate-G1 probe seeing a partially-built pending-set or the gate-G4 check seeing a partial DSU, and either bug surfaces only intermittently when the order of pipeline values happens to expose the seam.

## Parse Order and Determinism

The parser is called once per op as the scheduler-init pass walks the region in MLIR's intrinsic block-then-operation order — the same order `Operation::walk` yields. That order determines two things the downstream consumers rely on: the order DSU unions execute (and therefore which gid wins as the root when two ops disagree about leadership, as the second QUIRK above notes), and the order ops are inserted into the `ConstraintMap`. Both surfaces are stable across re-runs on the same input IR because MLIR's walk is deterministic, but they are not stable across IR transformations that reorder ops within a block. A pass that hoists or sinks a constraint-bearing op between the front-end and the scheduler can flip the DSU root for groups whose members carry inconsistent `leader_gid` values, with the symptom that the same source produces different schedules depending on which passes ran upstream. The cure is to enforce `leader_gid == gid` for group leaders at the frontend so the DSU root choice is no longer order-sensitive.

## Usage and Contract

The parser runs once per op at scheduler-init time, before any placement arm fires. It consults the op's inherent properties dictionary first and falls back to the discardable attributes dictionary, reading only the nine string keys listed above — every other attribute on the op is ignored. Two outputs reach the rest of the scheduler. The first is the per-op `ConstraintSlot` keyed by op handle inside the `ConstraintMap`, retrieved by every later consumer through `sub_94A550(state, op)`. The second is the seeded disjoint-set forest at `state + 112`, written only when an op's `leader_gid` differs from its `gid`. Frontends emitting the constraint attributes must keep `leader_gid` consistent across every op in a fusion group — the parser does no symmetry check, and a divergent group will produce two DSU roots that the placement driver treats as independent.

## Cross-References

[Modulo Driver and 4-Arm OR-Chain](modulo-driver-or-chain.md) documents the placement driver that reads the `max_depth` G2 admission gate and consults the DSU built here. [Schedule::solve and Cost Evaluators](schedule-solve-and-cost-evaluators.md) documents the cost-based arm that honours `force_serial_execution`. [Serial and Cost-Based Schedule Generators — G2: Max-Depth Viability](serial-vs-cost-based-generators.md#g2-max-depth-viability) explains the G2 viability check that gates retry.
