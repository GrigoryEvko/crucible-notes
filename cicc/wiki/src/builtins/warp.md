# Warp-Level Operation Builtins

Warp-level builtins provide lane-to-lane communication within a 32-thread warp. They cover four major categories: shuffle (data exchange between lanes), vote (predicate aggregation), match (value matching across lanes), and redux (warp-wide reductions). The shuffle operations also serve as the lowering target for the WMMA fragment load/store operations described in the [tensor core page](tensor-mma.md).

## Shuffle Operations (IDs 413--416)

The `__shfl_sync` family enables direct register-to-register communication between warp lanes. Four shuffle modes exist, each registered as a `_sync` variant:

| ID | Builtin | Mode | Description |
|---|---|---|---|
| 413 | `__nvvm_shfl_up_sync` | Up | Lane reads from `lane - delta` |
| 414 | `__nvvm_shfl_down_sync` | Down | Lane reads from `lane + delta` |
| 415 | `__nvvm_shfl_bfly_sync` | Butterfly | Lane reads from `lane XOR delta` |
| 416 | `__nvvm_shfl_idx_sync` | Index | Lane reads from arbitrary `srcLane` |

### Shuffle Dispatch via Table Lookup

All shuffle builtins route through `sub_12B3540` (EDG) / `sub_954F10` (NVVM), the table-based lowering handler. Three groups of 8 IDs each cover the complete shuffle interface:

| ID Range | Group | Description |
|---|---|---|
| 302--309 | Legacy `__shfl` | Non-sync variants (4 modes x 2 types: i32/f32) |
| 338--345 | `__shfl_sync` | Sync variants with mask (4 modes x 2 types) |
| 395--402 | `__shfl_*_sync` | Newer SM interface (4 modes x 2 types) |

Within each group of 8, the layout is:

| Offset | Mode | i32 Variant | f32 Variant |
|---|---|---|---|
| +0, +1 | shfl_up | offset +0 | offset +1 |
| +2, +3 | shfl_down | offset +2 | offset +3 |
| +4, +5 | shfl_xor | offset +4 | offset +5 |
| +6, +7 | shfl_idx | offset +6 | offset +7 |

The handler builds the argument list (mask, value, delta/lane, width), looks up the target intrinsic by shuffle mode and data type from its red-black tree map, and emits a function call.

## Vote Operations (IDs 351--358)

Warp vote builtins aggregate a boolean predicate across all participating lanes. Both legacy (non-sync) and sync variants are registered.

| ID | Builtin | Operation | Sync |
|---|---|---|---|
| 351 | `__nvvm_vote_all` | All predicates true? | No |
| 352 | `__nvvm_vote_any` | Any predicate true? | No |
| 353 | `__nvvm_vote_uni` | All predicates equal? | No |
| 354 | `__nvvm_vote_ballot` | Bitmask of predicates | No |
| 355 | `__nvvm_vote_all_sync` | All predicates true? | Yes |
| 356 | `__nvvm_vote_any_sync` | Any predicate true? | Yes |
| 357 | `__nvvm_vote_uni_sync` | All predicates equal? | Yes |
| 358 | `__nvvm_vote_ballot_sync` | Bitmask of predicates | Yes |

### Vote Lowering

The handler `sub_12ABB90` (EDG) / `sub_94D570` (NVVM) takes parameters:

```
(result, ctx, vote_op, args, is_ballot, is_sync)
```

The `vote_op` encoding: 0 = all, 1 = any, 2 = uni, 3 = ballot.

When `is_sync=1`, an extra mask argument is consumed from the call arguments. For non-sync variants, the handler looks up intrinsic 5301 (`llvm.nvvm.vote`). For sync variants, it generates an inline predicate pattern. The ballot variant (vote_op=3) sets `is_ballot=1`, which changes the return type from `i1` (predicate) to `i32` (bitmask).

## Match Operations (IDs 361--364)

Match builtins find lanes with equal values and return a bitmask of matching lanes. Available in 32-bit and 64-bit variants with two matching modes.

| ID | Builtin | Width | Mode | Intrinsic |
|---|---|---|---|---|
| 361 | `__match32_any_sync` | 32-bit | Any match | `0x1011` |
| 362 | `__match64_any_sync` | 64-bit | Any match | `0x1011` |
| 363 | `__match32_all_sync` | 32-bit | All match | `0x100F` |
| 364 | `__match64_all_sync` | 64-bit | All match | `0x100F` |

The handler `sub_12AD230` (EDG) dispatches on two opcodes: `0x1011` for any-match and `0x100F` for all-match. The NVVM-side handler `sub_94F430` uses intrinsic pairs `0x2017` / `0x2018` with mode variants 0, 1, 2 to encode the width and match type.

## Warp Redux (IDs 413--416 range, via `sub_12ADD20`)

Warp-wide reduction operations perform arithmetic reductions across all active lanes in a single instruction. These are dispatched through `sub_12ADD20` (EDG) / `sub_94F250` (NVVM).

| ID | Operation | NVVM Intrinsic | Description |
|---|---|---|---|
| redux.sync.add | `0x24F5` (9461) | Sum reduction | Sum of values across warp |
| redux.sync.min | `0x24ED` (9453) | Minimum reduction | Minimum value across warp |
| redux.sync.max | `0x24E9` (9449) | Maximum reduction | Maximum value across warp |
| redux.sync.or | `0x24F1` (9457) | Bitwise OR reduction | OR of values across warp |

The EDG side uses intrinsic codes `0x2332` and `0x2330` for the two redux variant families.

## Activemask and Lanemask

The active mask and per-lane mask builtins are handled through `sub_12ADB00` (EDG) / `sub_94CF30` (NVVM):

These builtins return the set of currently active lanes (`__activemask()`) or per-lane positional masks (`__lanemask_lt()`, `__lanemask_le()`, `__lanemask_eq()`, `__lanemask_ge()`, `__lanemask_gt()`). They compile to PTX special register reads (`%lanemask_*`).

## Predicate-Register Conversion (IDs 411--412)

Two builtins convert between predicate registers and general-purpose registers:

| ID | Builtin | Direction | Description |
|---|---|---|---|
| 411 | `__nv_p2r` | Predicate -> Register | Pack predicates into a 32-bit register |
| 412 | `__nv_r2p` | Register -> Predicate | Unpack a 32-bit register into predicates |

The handler generates element-wise operations: `sub_9483E0` iterates over vector elements using `sub_39FAC40` to compute the element count, then builds per-element `extractelement` + store (for p2r) or load + `insertelement` (for r2p) chains.

## Nanosleep and CP.Async

Warp-adjacent utility builtins handled through `sub_12AD230` / `sub_94ED50`:

| ID Range | Operation | Description |
|---|---|---|
| 367--369 | `__nv_memcpy_async_shared_global_{4,8,16}_impl` | Asynchronous copy (cp.async) |

These builtins combine data movement with implicit synchronization and are lowered through `sub_12AB730` / `sub_94C5F0`, which builds the `cp.async` PTX instruction with the specified transfer size (4, 8, or 16 bytes).

## Architecture Requirements

| Feature | Minimum SM | Notes |
|---|---|---|
| `__shfl` (legacy, non-sync) | SM 30+ | Deprecated; requires full warp convergence |
| `__shfl_sync` | SM 70+ (Volta) | Explicit mask; independent thread scheduling |
| Vote (non-sync) | SM 30+ | Deprecated |
| Vote (`_sync`) | SM 70+ | Explicit mask required |
| Match (`_sync`) | SM 70+ | Warp-level value matching |
| Redux (`redux.sync.*`) | SM 80+ (Ampere) | Hardware-accelerated warp reduction |
| Elect sync | SM 90+ (Hopper) | Single-lane election from active mask |
| `cp.async` | SM 80+ | Asynchronous shared memory copy |
