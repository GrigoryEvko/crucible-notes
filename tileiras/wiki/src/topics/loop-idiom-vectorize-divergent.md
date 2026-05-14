# LoopIdiomVectorize + Divergent-Target Gate

## Abstract

Tileiras carries LLVM's `LoopIdiomVectorize` pass alongside a nearby NVIDIA legality check in `LoopVectorize`. They solve different problems. `LoopIdiomVectorize` recognizes scalar byte-search loops and rewrites them into masked or VP-predicated vector form — naturally SIMT-friendly because it predicates individual lanes instead of cloning scalar and vector loop versions behind a runtime branch.

The divergent-target gate belongs to `LoopVectorize`, not `LoopIdiomVectorize`. It prevents the ordinary runtime-pointer-check path from versioning a loop when the target may execute branch paths divergently. Responsibility splits cleanly: `LoopVectorize` refuses a versioning strategy that would require a uniform runtime check, while `LoopIdiomVectorize` remains available for idioms expressible with per-lane masks.

Both pieces live on one page because together they answer one user-facing question — "why did my loop vectorize here but not there on this SIMT target?" Without seeing both the predicated LIV path (which works on a SIMT target) and the runtime-versioning veto (which does not), an upstream-LLVM reader cannot reconcile the contradictory reports. The page therefore reads top-down: what LIV is, what `CantVersionLoopWithDivergentTarget` is, and why these two strategies sit on opposite sides of the divergence question.

## LoopIdiomVectorize Role

LIV walks loops looking for a small set of scalar idioms whose control flow can be represented as vector compares, masks, and reductions. Its target interaction is limited to the normal cost model: it asks the target which vector width and predication style are profitable, then emits generic vector IR. It never asks whether the target has branch divergence, because it never introduces a uniform runtime-versioning branch.

The pass lives in the ordinary LLVM optimization pipeline under the canonical pass name `loop-idiom-vectorize`. It is not a CUDA-tile-only pass; treat it as an inherited LLVM mid-end transformation with NVIDIA target-cost participation.

## Three idiom expanders

| Idiom | Expansion shape | Distinguishing IR names |
|-------|----------|------------------------------------------|
| `byte.compare` | Builds a masked byte comparison and feeds the result into the shared mismatch machinery. | `byte.compare` |
| `find_first_vec` | Creates a vector header, match check, and lane calculation for the first matching byte. | `scalar_preheader`, `find_first_vec_header`, `match_check_vec`, `calculate_match`, `needle_check_vec` |
| `mismatch_vec_loop` | Builds the vector mismatch loop, found predicate, index calculation, and final LCSSA values. | `mismatch_vec_loop_pred`, `mismatch_vec_index`, `mismatch_vec_found_pred`, `mismatch_vec_found_index` |

The user-facing controls match LLVM's pass-level knobs:

- `disable-loop-idiom-vectorize-all`
- `disable-loop-idiom-vectorize-bytecmp`
- `loop-idiom-vectorize-bytecmp-vf`
- `disable-loop-idiom-vectorize-find-first-byte`
- `loop-idiom-vectorize-style=none|masked|predicated`
- `loop-idiom-vectorize-verify`

No separate NVIDIA-only option exists for the LIV idiom recognizer. NVIDIA's behavior comes from the target cost model and from the adjacent `LoopVectorize` legality gate.

## Divergent-Target Gate

`LoopVectorize` evaluates the gate before accepting a plan that needs runtime pointer checks. Such checks usually create a version branch: one side runs a vectorized loop under a no-alias assumption, the other a fallback loop. On a SIMT target the branch condition has to be uniform across the warp. If the target can diverge and the loop still needs runtime pointer checks, NVIDIA's legality hook rejects the plan and emits an optimization remark.

The observable remark uses three stable pieces of text:

- remark name: `CantVersionLoopWithDivergentTarget`
- pass name: `Not inserting runtime ptr check for divergent target`
- message: `runtime pointer checks needed. Not enabled for divergent target`

```c
static bool can_version_loop_on_target(const Loop *loop,
                                       const TargetTransformInfo *tti,
                                       const RuntimePointerChecks *checks) {
    if (tti_has_branch_divergence(tti) && runtime_pointer_checks_needed(checks, loop)) {
        emit_loop_vectorize_remark(loop,
                                   "CantVersionLoopWithDivergentTarget",
                                   "Not inserting runtime ptr check for divergent target",
                                   "runtime pointer checks needed. Not enabled for divergent target");
        return false;
    }

    return true;
}
```

This is why the two pieces coexist without conflict. `LoopVectorize` refuses only the specific runtime-versioning strategy that is unsafe for a divergent target. LIV remains free to transform recognized idioms because its output uses masks and predicates, not a branch whose condition must be uniform.

