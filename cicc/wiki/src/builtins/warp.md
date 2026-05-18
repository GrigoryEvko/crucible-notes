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

## Convergence Semantics and Independent Thread Scheduling

All `_sync` warp builtins take an explicit 32-bit `unsigned int membermask` parameter naming the lanes that must converge before the operation can proceed. Hardware spins each named lane at the instruction's program counter until all members arrive; non-members are not blocked. The mask is encoded as a single i32 operand on the LLVM intrinsic and emerges in PTX as the first operand of `shfl.sync`, `vote.sync`, `match.sync`, etc.

```text
membermask validation contract (binary-recovered):
  popc(membermask) >= 1                # at least one participant
  membermask >> lane_id & 1 == 1       # caller's lane must be in mask
  ∀ lane ∈ membermask:
       PC[lane] points at same _sync   # textual program-counter equality
  participation set := membermask ∩ active_mask_at_PC
```

If any named lane is not active at the instruction, hardware behavior is undefined (Volta+) — older `__shfl` (no `_sync`) implicitly used the full active mask, masking this class of bug behind warp-lockstep execution. Volta's Independent Thread Scheduling (ITS) broke that assumption: lanes can sit at arbitrary PCs after divergent branches, so the compiler can no longer prove "all lanes are here" without the explicit mask.

> ⚡ **QUIRK — Legacy `__shfl` is still emitted by libdevice**
> The non-sync legacy IDs 302–309 are not just back-compat documentation: cicc still routes them through `sub_954F10` and emits `shfl.{up,down,bfly,idx}.b32` without a mask operand. On SM 70+ hardware these execute as `shfl.sync.b32` with an implicit full-mask, but the ptxas-side gate is the only thing rejecting them on SM 90+ when ITS scheduling makes implicit convergence undefined. Mixing legacy and `_sync` shuffles in the same warp can silently produce wrong results on Volta-and-later because the legacy lowering loses the membership contract.

> ⚡ **QUIRK — Ballot return type is silently re-typed**
> Vote operations IDs 351–353/355–357 return `i1` (a one-bit predicate), but ID 354 / 358 (`vote.ballot{,.sync}`) returns `i32`. The dispatch table at `sub_94D570` does not differentiate: it sets `is_ballot=1` based on `vote_op==3` and re-types the SDNode result *after* IR generation. A user-facing wrapper that calls the ballot intrinsic with the wrong return type clamps to one bit silently — no IR-verifier error — because the type rewrite happens past verification.

## Shuffle Dispatch Pseudocode

The handler at `sub_954F10` (NVVM) walks a three-step table lookup before emitting the call. The table groups (302–309, 338–345, 395–402) all share the same 8-entry layout (`up`/`down`/`xor`/`idx` × `i32`/`f32`), and the handler relies on that uniformity:

```text
lower_shfl(builtin_id, args):
    # Step 1: classify mask discipline
    if builtin_id in 302..309:
        group_base   = 302
        needs_mask   = false              # legacy: implicit full mask
        sync_variant = false
    elif builtin_id in 338..345:
        group_base   = 338
        needs_mask   = true
        sync_variant = true
    elif builtin_id in 395..402:
        group_base   = 395
        needs_mask   = true
        sync_variant = true

    # Step 2: decode mode and element type from offset
    offset = builtin_id - group_base       # 0..7
    mode   = offset >> 1                   # 0=up,1=down,2=xor,3=idx
    is_f32 = offset & 1                    # 0=i32, 1=f32

    # Step 3: build the LLVM intrinsic call
    intrinsic_id = SHFL_TABLE[mode][is_f32 | (sync_variant<<1)]
    call_args    = []
    if needs_mask:
        call_args.append(args.mask)        # i32 membermask
    call_args += [args.value, args.delta_or_lane, args.width_mask]

    emit_call(intrinsic_id, call_args)     # sub_1285290 / sub_921880
```

`SHFL_TABLE` is the red-black tree (`std::map<int, int>`) lazily initialized on first use; the keys are encoded `(mode, is_f32, sync)` triples and the values are LLVM intrinsic IDs from the NVVM intrinsic enum.

## Width Operand Encoding

Both the C builtin and the PTX `shfl` instruction accept a `width` parameter that segments the warp into smaller logical groups. The intrinsic encodes this as a single i32 packing two fields:

| Bits | Field | Meaning |
|---|---|---|
| 4:0 | `clamp` | Computed lane wrap value: `(32 - width) << 8 \| 0x1F` |
| 7:5 | `segmask` | `(32 - width) >> 3` — segment boundary bitmask |

Cicc does **not** compute this packing; it forwards the user's `width` argument verbatim and relies on ptxas to lower the packing. The validation that `width` is a power of two in `[1, 32]` is enforced only at NVVM IR verification (intrinsic argument constraint), not at the C/C++ frontend.

## Cross-References

- [Barriers and Synchronization](./barriers.md) — the mask discipline established by warp builtins is paired with named-barrier semantics
- [Tensor / MMA Builtins](./tensor-mma.md) — WMMA fragment load/store reduces to shuffle on the lowering path
- [Convergence and Structurization](../llvm/structurizecfg.md) — the structured CFG pass that constrains where divergent warps may sit when a `_sync` op is reached
