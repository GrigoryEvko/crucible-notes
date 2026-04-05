# Synchronization & Warp Intrinsics

This page documents how ptxas v13.0.88 handles the lowering of synchronization primitives and warp-level intrinsic operations from PTX source through Ori IR to final SASS instructions. The coverage spans warp vote, shuffle, match, and redux operations; thread-block barriers; memory barriers and fences; warp-level synchronization; and asynchronous barriers (mbarrier).

| | |
|---|---|
| **PTX codegen handlers** | `sub_580E50` (vote), `sub_5801D0` (shfl), `sub_58A730` (match), `sub_567680` (redux), `sub_524FB0` (bar), `sub_570290` (barrier), `sub_500BF0` (bar.arrive), `sub_570940` (barrier.arrive), `sub_52D590` (bar.red), `sub_5889B0` (barrier.red), `sub_56A5A0` (bar.warp), `sub_4DB410` (membar) |
| **Intrinsic IDs** | `0x01`--`0x11` (reduxsync, 17), `0x89`--`0x1FA` (sm70 warp/barrier/wmma, 370) |
| **Ori IR opcodes** | 96 (barrier), 119 (vote), 130 (BAR/sync internal) |
| **SASS opcodes** | `VOTE`, `VOTEU`, `BAR`, `BAR_INDEXED`, `MATCH`, `MEMBAR`, `WARPSYNC`, `BSYNC`, `BSSY`, `DEPBAR`, `ERRBAR`, `ELECT`, `NANOSLEEP` |
| **Blackwell additions** | `FENCE_G`, `FENCE_S`, `FENCE_T`, `CGABAR_ARV`, `CGABAR_GET`, `CGABAR_SET`, `CGABAR_WAIT`, `CGAERRBAR`, `SYNCS_BASIC`, `SYNCS_LD_UNIFM` |
| **Optimizer phases** | 25 (StageAndFence), 26 (OriRemoveRedundantBarriers), 42 (ExpandMbarrier), 71 (OptimizeSyncInstructions), 72 (LateExpandSyncInstructions), 99, 100, 114 |
| **Intrinsic detection** | `sub_A9A410` (sm70 warp-sync prefix matcher), `sub_A94440` (mbarrier classifier) |
| **Related EIATTR** | `EIATTR_NUM_BARRIERS`, `EIATTR_NUM_MBARRIERS`, `EIATTR_MBARRIER_INSTR_OFFSETS`, `EIATTR_SYNC_STACK`, `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS`, `EIATTR_GEN_ERRBAR_AT_EXIT` |
| **CLI options** | `--assume-extern-functions-do-not-sync`, `--no-membermask-overlap`, `--print-potentially-overlapping-membermasks` |
| **Related pages** | [Synchronization & Barriers (passes)](../passes/sync-barriers.md), [Intrinsic Table](index.md) |

## Instruction Classification

ptxas groups synchronization and warp operations into three Ori IR opcode categories that govern scheduling, dependency tracking, and optimization treatment throughout the pipeline.

| Ori Opcode | Category | Instructions | WAR Resource Mask |
|---|---|---|---|
| 96 | Barrier | `bar.sync`, `bar.red`, `bar.arrive`, `barrier.*` | `0x200001` (bit 0 + bit 21) |
| 119 | Vote | `vote.{all,any,uni,ballot}`, `match.*`, `redux.*`, `activemask`, `elect.sync` | `0x1` (bit 0 only) |
| 130 | Sync internal | BAR/MEMBAR pseudo-ops during optimization | Used by phases 26, 71 for redundancy analysis |

The WAR resource mask at opcode 96 (`2097153 = 0x200001`) uniquely identifies barrier instructions to the scoreboard tracker. For all other opcodes, the base mask is 1. The scheduler uses this to insert appropriate stall cycles between barrier producers and consumers.

## Warp Vote

Warp vote operations evaluate a per-lane predicate across the active threads in a warp and return a collective result. In ptxas these are lowered through the `vote` codegen handler at `sub_580E50` (approximately 3.2KB of decompiled code).

### PTX to SASS Mapping

| PTX Instruction | SASS Opcode | Result | Membermask |
|---|---|---|---|
| `vote.sync.all.pred p, q, membermask` | `VOTE.ALL` | Predicate: true iff all active lanes have `q=true` | Explicit 32-bit mask |
| `vote.sync.any.pred p, q, membermask` | `VOTE.ANY` | Predicate: true iff any active lane has `q=true` | Explicit 32-bit mask |
| `vote.sync.uni.pred p, q, membermask` | `VOTE.UNI` | Predicate: true iff `q` is uniform across active lanes | Explicit 32-bit mask |
| `vote.sync.ballot.b32 d, q, membermask` | `VOTE.BALLOT` | `R`: 32-bit ballot mask of lanes where `q=true` | Explicit 32-bit mask |
| `activemask.b32 d` | `CS2R` (read `SR_LANEMASK_ACTIVE`) | `R`: current active lane mask | Implicit (all active) |
| `elect.sync d, membermask` | `ELECT.SYNC` | Predicate: true for exactly one active lane | Explicit 32-bit mask (sm75+) |

On sm100+ (Blackwell), `VOTEU` is available as a uniform-register variant of `VOTE` for cases where the result feeds only uniform consumers.

### Codegen Handler Structure -- `sub_580E50`

The vote handler follows the standard intrinsic codegen pattern: allocates a 50,000-byte scratch buffer via `sub_424070`, then builds an inline PTX function body through sequential `sprintf()` calls. The handler dispatches on three architecture tiers:

```
sub_580E50(ctx, string_table):
    instr = *(ctx + 1096)
    buf = alloc(50000)

    // Common prologue: function header + parameter declarations
    sprintf(buf, string_table[309261...])   // ".reg .pred %p; ..."

    // Feature check: sub_70B6E0(instr) -- has membermask operand?
    if has_membermask:
        mask_val = sub_70B780(instr)
        sprintf(buf, format_string, mask_val)

    // Architecture dispatch:
    if (sub_70FA00(instr, 11) || SM > 89) && sub_7081E0(instr) == 1:
        // Path 1: sm90+ with sync variant
        // Emits: vote.sync.{mode} with full operand set
        // Reads operands 0,1,2 via sub_70B8E0, sub_70CA70, sub_709E80, sub_70B4F0

    else if SM > 69 && sub_7081E0(instr) == 1:
        if sub_70FA00(instr, 10) || sub_709E60(instr) == 1:
            // Path 2a: sm70+ with explicit sync
            // Reads operands via sub_70B510, sub_70B8E0

        else:
            // Path 2b: sm70+ standard path
            // Checks sub_70FA00(instr, 17) -- has predicate output
            // Checks sub_70BA10(instr) -- ballot mode
            // SM > 75 branch: different register conventions
            // sub_70A910(instr) == 1: uniform result path

    // Epilogue: closing braces, return buffer
```

The accessor `sub_70FA00(instr, 0)` returns the SM architecture level (e.g., 70, 75, 80, 89, 90). The value at parameter 11 checks for a specific feature flag (cluster/sync extension). `sub_7081E0(instr)` returns the instruction variant (1 = sync form).

### Intrinsic Registration

Vote intrinsics are registered as part of the sm70 block (`0x89`--`0x1FA`) with four entries:

| Intrinsic Name | PTX Equivalent |
|---|---|
| `__cuda_sm70_votesync_all` | `vote.sync.all.pred` |
| `__cuda_sm70_votesync_any` | `vote.sync.any.pred` |
| `__cuda_sm70_votesync_ballot` | `vote.sync.ballot.b32` |
| `__cuda_sm70_votesync_uni` | `vote.sync.uni.pred` |

Detection of these intrinsics at the IR level is handled by `sub_A9A410` (194 bytes binary, 908 bytes decompiled), which performs prefix matching against three string patterns:

```c
// sub_A9A410 -- IntrinsicDetector::isSM70WarpSync
static const char* prefixes[] = {
    "__cuda_sm70_warpsync",
    "__cuda_sm70_votesync_",
    "__cuda_sm70_matchsync_",
};
for (each prefix) {
    name = getSymbolName(instr.symbol_id);
    if (!strncmp(prefix, name, strlen(prefix)))
        return 1;
}
return 0;
```

This function is called during instruction lowering (subsystem 6 at `0xA9F000`--`0xAA8000`) to identify warp-synchronous call sites that need special handling during barrier optimization.

## Warp Shuffle

Warp shuffle moves data between lanes within a warp. The codegen handler `sub_5801D0` (approximately 3.3KB decompiled) generates inline PTX for the four shuffle modes.

### PTX to SASS Mapping

| PTX Instruction | SASS Opcode | Data Movement |
|---|---|---|
| `shfl.sync.idx.b32 d\|p, a, b, c, membermask` | `SHF` / `SHFL` | `d = lane[b]` (direct indexed) |
| `shfl.sync.up.b32 d\|p, a, b, c, membermask` | `SHF` / `SHFL` | `d = lane[laneid - b]` (shift up) |
| `shfl.sync.down.b32 d\|p, a, b, c, membermask` | `SHF` / `SHFL` | `d = lane[laneid + b]` (shift down) |
| `shfl.sync.bfly.b32 d\|p, a, b, c, membermask` | `SHF` / `SHFL` | `d = lane[laneid ^ b]` (butterfly XOR) |

The `c` operand packs the clamp value and width: `c = ((width - 1) << 8) | clamp`. The optional predicate output `p` indicates whether the source lane was within bounds.

### Codegen Handler -- `sub_5801D0`

The shuffle handler is structurally similar to vote. It reads up to 5 operands (source value, lane offset, clamp/width, membermask, and optional predicate output) through the accessor chain:

```
sub_5801D0(ctx, string_table):
    // string_table offsets start at 311376
    // Prologue with 4 sprintf calls for parameter declarations

    if (SM >= 90 || feature_flag_11) && variant == 1:
        // sm90+ path: reads operands 0..4
        // sub_70B960(instr) -- gets shuffle mode enum
        // sub_70B450(instr) -- gets data type
        // Emits shfl.sync.{mode}.b32 with full 8-operand format

    else if SM > 69 && variant == 1:
        if feature_10 || sub_709E60(instr) == 1:
            // sm70+ explicit sync path
            // 7-operand format

        else:
            // Standard sm70 path
            // Checks sub_70BA10 for predicate output
            // SM > 75: different operand packing
```

The shuffle mode is obtained via `sub_70B960(instr)`, returning an enum: 0=idx, 1=up, 2=down, 3=bfly. `sub_70B450(instr)` returns the data type (b32 for standard shuffles).

### Intrinsic Registration

| Intrinsic Name | PTX Equivalent |
|---|---|
| `__cuda_sm70_shflsync_idx` | `shfl.sync.idx.b32` |
| `__cuda_sm70_shflsync_up` | `shfl.sync.up.b32` |
| `__cuda_sm70_shflsync_down` | `shfl.sync.down.b32` |
| `__cuda_sm70_shflsync_bfly` | `shfl.sync.bfly.b32` |

Each has a variant with predicate output (e.g., `__cuda_sm70_shflsync_idx_pred`).

## Warp Match

Warp match instructions compare a value across lanes and return which lanes hold matching values. The codegen handler `sub_58A730` (approximately 4.5KB decompiled) is the largest in this group.

### PTX to SASS Mapping

| PTX Instruction | SASS Opcode | Result |
|---|---|---|
| `match.sync.any.b32 d, a, membermask` | `MATCH.ANY` | `d`: mask of lanes holding the same value as the calling lane |
| `match.sync.all.b32 d\|p, a, membermask` | `MATCH.ALL` | `d`: mask of lanes holding the same value; `p`: true if ALL active lanes match |
| `match.sync.any.b64 d, a, membermask` | `MATCH.ANY` | 64-bit value comparison variant |
| `match.sync.all.b64 d\|p, a, membermask` | `MATCH.ALL` | 64-bit value comparison variant |

### Codegen Handler -- `sub_58A730`

The match handler has three architecture tiers and handles both b32 and b64 operand widths:

```
sub_58A730(ctx, string_table):
    // string_table offsets start at 323786

    if (SM >= 90 || feature_flag_11) && variant == 1:
        // sm90+ path: 7-operand format
        // Reads: sub_70B4F0, sub_709E80, sub_70CA70, sub_70B8E0(0..2)

    else if SM > 69 && variant == 1:
        // sm70+ path
        // Checks feature_10 and sub_709E60 for explicit sync
        // sub_70B940(instr) -- has match predicate output?
        // sub_70D1F0(instr, 0) -- gets operand by index
        // sub_70B950(instr) -- gets comparison mode
```

### Intrinsic Registration

| Intrinsic Name | Variants |
|---|---|
| `__cuda_sm70_matchsync_any_b32` | Standard |
| `__cuda_sm70_matchsync_any_b64` | 64-bit |
| `__cuda_sm70_matchsync_all_b32` | With predicate output |
| `__cuda_sm70_matchsync_all_b64` | With predicate output, 64-bit |

Detection uses the same `sub_A9A410` prefix matcher with `"__cuda_sm70_matchsync_"`.

## Warp Redux

Warp redux performs a warp-wide reduction operation and returns the result to all participating lanes. The codegen handler `sub_567680` (approximately 2.0KB decompiled) is relatively compact.

### PTX to SASS Mapping

| PTX Instruction | SASS Functional Unit | Operation |
|---|---|---|
| `redux.sync.add.s32 d, a, membermask` | `redux` | Warp-wide signed integer addition |
| `redux.sync.min.s32 d, a, membermask` | `redux` | Warp-wide signed integer minimum |
| `redux.sync.max.s32 d, a, membermask` | `redux` | Warp-wide signed integer maximum |
| `redux.sync.min.u32 d, a, membermask` | `redux` | Warp-wide unsigned integer minimum |
| `redux.sync.max.u32 d, a, membermask` | `redux` | Warp-wide unsigned integer maximum |
| `redux.sync.add.u32 d, a, membermask` | `redux` | Warp-wide unsigned integer addition |
| `redux.sync.and.b32 d, a, membermask` | `redux` | Warp-wide bitwise AND |
| `redux.sync.or.b32 d, a, membermask` | `redux` | Warp-wide bitwise OR |
| `redux.sync.xor.b32 d, a, membermask` | `redux` | Warp-wide bitwise XOR |
| `redux.sync.min.f32.NaN d, a, membermask` | `redux` | Warp-wide float minimum (NaN-propagating) |
| `redux.sync.max.f32.NaN d, a, membermask` | `redux` | Warp-wide float maximum (NaN-propagating) |
| `redux.sync.min.f32.abs d, a, membermask` | `redux` | Warp-wide float absolute minimum |

The scheduler tracks redux operations on the dedicated `redux` functional unit pipeline, alongside `adu`, `alu`, `cbu`, `fma2x`, `fma`, `half`, `transcendental`, `ipa`, `lsu`, `schedDisp`, `tex`, `ttu`, `udp`, and the various MMA pipelines.

### Codegen Handler -- `sub_567680`

```
sub_567680(ctx, string_table):
    // Prologue: function header

    if (SM >= 90 || feature_flag_11) && variant == 1:
        // sm90+ path: 8-operand format
        // Reads: sub_709E80, sub_70CA70, sub_707530, sub_7087C0,
        //        sub_707630, sub_70B8E0(0..2)
        // sub_707630 -- gets reduction operation type
        // sub_7087C0 -- gets data type qualifier

    else if SM > 79 && variant == 1:
        // sm80+ path
        // Two sub-branches:
        //   - feature_10 || explicit_sync || feature_19: simplified format
        //   - Standard: full format with sub_707650, sub_7087F0,
        //     sub_707540, sub_70D1F0
```

The accessor `sub_707630(instr)` returns the reduction operation enum (add/min/max/and/or/xor), while `sub_7087C0(instr)` returns the data type qualifier (s32/u32/b32/f32). Note that redux requires sm80+ in the hardware; the sm70 block in the intrinsic table registers redux-sync intrinsics as software emulation routines.

### Redux Sync Intrinsic Registration (IDs `0x01`--`0x11`)

The earliest 17 intrinsic IDs are dedicated to software-emulated redux-sync operations for pre-sm80 targets:

| ID | Intrinsic Name | Operation |
|---|---|---|
| `0x01` | `__cuda_reduxsync_b32_and` | Bitwise AND reduction |
| `0x02` | `__cuda_reduxsync_b32_or` | Bitwise OR reduction |
| `0x03` | `__cuda_reduxsync_b32_xor` | Bitwise XOR reduction |
| `0x04` | `__cuda_reduxsync_f32_max` | Float maximum |
| `0x05` | `__cuda_reduxsync_f32_min` | Float minimum |
| `0x06` | `__cuda_reduxsync_f32_max_abs` | Float absolute maximum |
| `0x07` | `__cuda_reduxsync_f32_min_abs` | Float absolute minimum |
| `0x08` | `__cuda_reduxsync_f32_max_NaN` | Float maximum (NaN-propagating) |
| `0x09` | `__cuda_reduxsync_f32_min_NaN` | Float minimum (NaN-propagating) |
| `0x0A` | `__cuda_reduxsync_s32_add` | Signed integer sum |
| `0x0B` | `__cuda_reduxsync_s32_max` | Signed integer maximum |
| `0x0C` | `__cuda_reduxsync_s32_min` | Signed integer minimum |
| `0x0D` | `__cuda_reduxsync_u32_add` | Unsigned integer sum |
| `0x0E` | `__cuda_reduxsync_u32_max` | Unsigned integer maximum |
| `0x0F` | `__cuda_reduxsync_u32_min` | Unsigned integer minimum |
| `0x10`--`0x11` | (additional variants) | Extended operations |

On sm80+, `redux` PTX instructions lower directly to hardware SASS instructions and bypass the software emulation path.

## Thread-Block Barriers

Thread-block barriers synchronize all threads within a CTA (Cooperative Thread Array). ptxas provides codegen handlers for three PTX barrier families plus their PTX 8.0 cluster-aware equivalents.

### PTX Barrier Family

| PTX Handler | Address | Size | PTX Instructions |
|---|---|---|---|
| `sub_524FB0` | `0x524FB0` | 1.8KB | `bar.sync`, `bar` |
| `sub_500BF0` | `0x500BF0` | 1.2KB | `bar.arrive` |
| `sub_52D590` | `0x52D590` | 1.5KB | `bar.red.{and,or,popc}` |
| `sub_570290` | `0x570290` | 2.5KB | `barrier.sync`, `barrier` |
| `sub_570940` | `0x570940` | -- | `barrier.arrive` |
| `sub_5889B0` | `0x5889B0` | 4.8KB | `barrier.red` |
| `sub_56A5A0` | `0x56A5A0` | 1.9KB | `bar.warp.sync` |

### PTX to SASS Mapping

| PTX Instruction | SASS Opcode | Behavior |
|---|---|---|
| `bar.sync N` | `BAR.SYNC` | Block until all CTA threads arrive at barrier N |
| `bar.sync N, count` | `BAR.SYNC` | Block until `count` threads arrive at barrier N |
| `bar.arrive N` | `BAR.ARV` | Signal arrival at barrier N without blocking |
| `bar.arrive N, count` | `BAR.ARV` | Signal arrival with thread count |
| `bar.red.and N, p` | `BAR.RED.AND` | Barrier + warp-level AND reduction on predicate |
| `bar.red.or N, p` | `BAR.RED.OR` | Barrier + warp-level OR reduction |
| `bar.red.popc N, d` | `BAR.RED.POPC` | Barrier + warp-level population count |
| `barrier.cta.sync N` | `BAR.SYNC` | PTX 8.0 cluster-aware CTA barrier |
| `barrier.cta.arrive N` | `BAR.ARV` | PTX 8.0 cluster-aware CTA arrive |
| `barrier.cta.red N` | `BAR.RED` | PTX 8.0 cluster-aware CTA reduction |

The hardware provides 16 named barriers (indices 0--15). The `EIATTR_NUM_BARRIERS` metadata records the maximum barrier index used per kernel, which the driver uses to partition the convergence barrier file at launch.

### Codegen Handler Details -- `sub_524FB0`

The `bar.sync` / `bar` handler dispatches across three architecture generations:

```
sub_524FB0(ctx, string_table):
    // string_table offsets start at 294205

    if feature_flag_11 || SM > 89:
        // sm90+ path: 4 format strings for prologue
        // Checks sub_70B930(instr) for count variant:
        //   count != 2: single-operand format (barrier index only)
        //   count == 2: two-operand format (barrier index + thread count)

    else if SM > 69:
        if !feature_13 || sub_70B860(instr) > 69:
            // sm70+ standard path
            // Same count dispatch as sm90
        else:
            // sm70 with feature_13: extended format
            // Reads sub_709400 (barrier scope) and sub_708200 (barrier type)

    else:  // SM <= 69 (pre-Volta)
        // Legacy path
        // sub_709400 -- barrier scope identifier
        // sub_708200 -- barrier operation type
        // count dispatch with 3-operand format strings
```

The accessor `sub_70B930(instr)` returns the operand count mode: 1 for single-operand (barrier index only), 2 for two-operand (barrier index + thread count). `sub_70C180(instr)` returns a special value (-1 for default thread count).

### Named Barrier Intrinsic Registration

The sm70 intrinsic block registers barrier operations combinatorially:

| Sub-Category | Count | Combinatorial Source |
|---|---|---|
| `__cuda_sm70_barrier_arrive_{0..15}` | 16 | 16 barrier indices |
| `__cuda_sm70_barrier_arrive_{0..15}_cnt` | 16 | With explicit thread count |
| `__cuda_sm70_barrier_sync_{0..15}` | 16 | 16 barrier indices |
| `__cuda_sm70_barrier_sync_{0..15}_cnt` | 16 | With explicit thread count |
| `__cuda_sm70_barrier_red_and_{0..15}` | 16 | AND reduction per barrier |
| `__cuda_sm70_barrier_red_and_{0..15}_cnt` | 16 | With thread count |
| `__cuda_sm70_barrier_red_or_{0..15}` | 16 | OR reduction per barrier |
| `__cuda_sm70_barrier_red_or_{0..15}_cnt` | 16 | With thread count |
| `__cuda_sm70_barrier_red_popc_{0..15}` | 16 | POPC reduction per barrier |
| `__cuda_sm70_barrier_red_popc_{0..15}_cnt` | 16 | With thread count |

This combinatorial explosion produces 160 intrinsic entries for barriers alone (16 indices x 2 count variants x 5 operation types).

### Barrier Codegen Pattern -- `sub_570290`

The `barrier` (PTX 8.0 form) handler at `sub_570290` (2.5KB) is the most complex barrier handler. It adds cluster-awareness for sm90+ and handles the `barrier.cta.*` variants. The handler has an elaborate multi-level dispatch:

```
sub_570290(ctx, string_table):
    // sm90+ path: additional CTA scope parameters
    //   sub_709E80(instr) -- barrier scope enum
    //   sub_70B8E0(instr, 0) -- barrier index operand
    //   sub_70B8E0(instr, 1) -- thread count operand

    // sm70+ with feature_10 or explicit sync:
    //   Separate code paths for count=1 vs count=2

    // sm70+ standard (no explicit sync, no feature_10):
    //   Six format strings building an elaborate inline PTX body
    //   Handles sub_70B930 count modes 1 and 2
    //   Checks sub_70C180 for default-count (-1) vs explicit count
    //   Generates bar.sync N [, count]; or bar.arrive N [, count];
```

## Memory Barriers and Fences

### Memory Barriers -- `sub_4DB410`

The `membar` codegen handler at `sub_4DB410` (84 lines decompiled) is the smallest sync handler. It generates memory barrier instructions across three scope levels.

| PTX Instruction | SASS Opcode | Scope |
|---|---|---|
| `membar.cta` | `MEMBAR.CTA` | Thread block (CTA) |
| `membar.gpu` | `MEMBAR.GPU` | Device (GPU) |
| `membar.sys` | `MEMBAR.SYS` | System (all agents) |

```
sub_4DB410(ctx, string_table):
    mode = sub_709FE0(instr)

    if mode != 2 && mode != 4:
        // Standard 3-operand format
        // sub_70B710(instr) -- scope qualifier
        // sub_709FF0(instr) -- barrier type
        // sub_70A530(instr) -- additional qualifier

    else:  // mode 2 or 4 (cta or sys)
        if SM > 49:
            // sm50+ uses 3-operand format with scope
        else:
            // Pre-sm50: 2-operand membar + separate scope annotation
```

The `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` metadata records the byte offset of every `membar.sys` instruction in the output SASS. This allows the driver to apply software workarounds (WAR patches) at specific `membar.sys` locations for known hardware errata.

### Fence Operations

Fence operations enforce ordering between different memory proxy domains. These are not exposed as separate PTX codegen handlers but are inserted by the compiler's synchronization passes (phases 25 and 72).

| PTX Instruction | SASS Opcode (sm100+) | Purpose |
|---|---|---|
| `fence.proxy.alias` | (expanded inline) | Orders generic/alias memory accesses |
| `fence.proxy.async` | (expanded inline) | Orders async copy completion visibility |
| `fence.proxy.async.global` | (expanded inline) | Global memory async fence |
| `fence.sc.cta` | `FENCE_S` | Sequentially-consistent fence, CTA scope |
| `fence.sc.gpu` | `FENCE_G` | Sequentially-consistent fence, GPU scope |
| `fence.acq_rel.cta` | `FENCE_T` | Acquire-release fence, CTA scope |

On Blackwell (sm100+), dedicated `FENCE_G`, `FENCE_S`, and `FENCE_T` SASS opcodes replace the older pattern of `MEMBAR` + proxy annotation sequences.

### StageAndFence (Phase 25)

The `StageAndFence` pass (`sub_1392E30`, 166 bytes entry, `sub_1390B30` 8,956 bytes core) inserts fence instructions after loop unrolling to re-establish memory ordering correctness. When loop unrolling replicates memory operations that crossed a synchronization boundary in the original loop body, this pass inserts `fence.proxy` or `MEMBAR` pseudo-instructions at the boundaries of unrolled iterations.

The core function takes floating-point parameters (double/\_\_m128d), indicating it incorporates latency and throughput heuristics when deciding fence placement -- preferring to merge adjacent fences or delay them to overlap with independent computation.

## Warp-Level Synchronization

### WARPSYNC

`WARPSYNC mask` synchronizes the threads in a warp identified by the lane mask. This is the fundamental warp-level sync primitive on sm70+ (Volta and later).

| PTX | SASS | Purpose |
|---|---|---|
| `bar.warp.sync membermask` | `WARPSYNC mask` | Synchronize warp lanes specified by mask |

The intrinsic `__cuda_sm70_warpsync` (single entry in the sm70 block) is the simplest warp-sync intrinsic, and is detected by the same `sub_A9A410` prefix matcher that handles vote and match.

### BSSY / BSYNC (Convergence Barriers)

The `BSSY` / `BSYNC` instruction pair replaces the pre-Volta implicit reconvergence stack. The compiler must insert these pairs explicitly at divergence/reconvergence points:

| SASS Opcode | Purpose |
|---|---|
| `BSSY B, target` | Push a synchronization barrier; `target` is the reconvergence point |
| `BSYNC B` | Pop and wait at the convergence barrier B |

These are not directly exposed as PTX instructions -- they are inserted by the compiler during phase 72 (`LateExpandSyncInstructions`) and the architecture-specific sync expansion passes (phases 99, 100, 114). The `EIATTR_SYNC_STACK` metadata records the convergence barrier stack depth.

### ELECT

`ELECT.SYNC` (sm75+) elects a single active lane from the warp, setting a predicate register to true for exactly one thread.

In the SASS opcode table, `ELECT` appears among the Blackwell-era additions alongside `ENDCOLLECTIVE`, `PREXIT`, `SETMAXREG`, and `SETSMEMSIZE`. The `ELECT` opcode is used for leader-based algorithms where only one thread per warp performs a shared operation.

## Asynchronous Barriers (MBARRIER)

Introduced with sm90 (Hopper), mbarrier provides hardware-accelerated asynchronous barriers in shared memory. These are critical for async copy (`cp.async.bulk`), TMA operations, and warpgroup-level synchronization.

### MBARRIER Operation Classification

ptxas classifies mbarrier operations through `sub_A94440` (MBarrierDetector, 412 bytes binary) and `sub_A9A5F0` (MBarrierClassifier). The classifier resolves the mbarrier symbol name (prefix `%mbarrier_`) and returns an operation type enum:

| Type ID | Suffix | Operation |
|---|---|---|
| 0 | `INIT` | Initialize barrier object in shared memory |
| 1 | `ARRIVE` | Signal arrival (non-blocking) |
| 2 | `ARRIVE_NOCOMPLETE` | Arrive without completing the phase |
| 3 | `ARRIVE_DROP` | Arrive and decrement expected count |
| 4 | `ARRIVE_DROP_NOCOMPLETE` | Arrive, drop, no completion |
| 5 | `TEST_WAIT` | Test if barrier phase is complete |
| 6 | `TEST_WAIT_PARITY` | Phase-parity-based completion test |
| 7 | `CP_ASYNC_ARRIVE` | cp.async arrival notification |
| 8 | `INVAL` | Invalidate barrier |
| 9 | `TRY_WAIT` | Wait with timeout |
| 10 | `TRY_WAIT_PARITY` | Phase-parity-based wait with timeout |
| 11 | `EXPECT_TX` | Set expected transaction byte count |
| 12 | `COMPLETE_TX` | Mark transaction bytes as complete |

The "trivial" mbarrier operations (types 0--8) are handled inline; "non-trivial" operations (types 9--12, including `EXPECT_TX` and the extended `TRY_WAIT` variants) require more complex lowering.

### Mbarrier Symbol Naming

Mbarrier objects are tracked through shared memory symbols following the pattern:

```
%mbarrier_{basename}_{operation}
```

The resolver at `sub_A9A920` extracts the base name from the full symbol (e.g., `%mbarrier_pipeline0_ARRIVE` yields base name `pipeline0`). The format string `"%%mbarrier_%s_%s"` at `sub_AA33C0` is used for symbol construction during mbarrier expansion.

Reserved shared memory regions for TMA pipeline mbarriers:
- `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier`
- `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier_parity`

### ExpandMbarrier (Phase 42)

Phase 42 expands mbarrier pseudo-instructions into native barrier sequences through architecture vtable dispatch:

```asm
mov    rdi, [rsi+0x630]     ; rdi = ctx->arch_backend (offset 1584)
mov    rax, [rdi]            ; rax = arch_backend->vtable
jmp    [rax+0x168]           ; call vtable[45] -- ExpandMbarrier impl
```

The expansion is architecture-specific:
- **Pre-sm90**: No mbarrier pseudo-ops exist; the phase is a no-op
- **sm90 (Hopper)**: Expands to hardware mbarrier instruction sequences, resolves shared memory addresses, inserts `fence.proxy` for coherence
- **sm100+ (Blackwell)**: Extended semantics for `tcgen05.fence`, cluster-level barriers, and async pipeline operations

Expansion pattern:

```
Before (Ori pseudo-ops):           After (native SASS):
MBARRIER_INIT  %mbar, count       MBARRIER.INIT  [smem], count
MBARRIER_ARRIVE_EXPECT_TX          MBARRIER.ARRIVE.EXPECT_TX  [smem], bytes
  %mbar, bytes
CP.ASYNC.BULK.TENSOR               CP.ASYNC.BULK.TENSOR  [dst], [src], [smem]
  dst, src, %mbar
MBARRIER_TRY_WAIT_PARITY           MBARRIER.TRY_WAIT.PARITY  pred, [smem],
  %mbar, parity, pred                parity
```

### EIATTR Metadata

| EIATTR | Purpose |
|---|---|
| `EIATTR_NUM_MBARRIERS` | Count of mbarrier objects used by the kernel |
| `EIATTR_MBARRIER_INSTR_OFFSETS` | Byte offsets of mbarrier instructions for driver patching |

### Blackwell CGA Barriers

On sm100+ (Blackwell), a new class of CGA (Cooperative Grid Array) barriers extends the mbarrier concept to cluster-level synchronization:

| SASS Opcode | Purpose |
|---|---|
| `CGABAR_ARV` | CGA barrier arrive |
| `CGABAR_GET` | Query CGA barrier state |
| `CGABAR_SET` | Set CGA barrier parameters |
| `CGABAR_WAIT` | Wait on CGA barrier |
| `CGAERRBAR` | CGA error barrier |

## Synchronization Pipeline Summary

The complete synchronization processing pipeline spans 8 optimizer phases:

| Phase | Name | Category | Purpose |
|---|---|---|---|
| 25 | `StageAndFence` | Lowering | Insert fences after loop unrolling |
| 26 | `OriRemoveRedundantBarriers` | Optimization | Dataflow-driven barrier elimination (multi-function) |
| 42 | `ExpandMbarrier` | Lowering | Expand mbarrier pseudo-ops via arch vtable |
| 71 | `OptimizeSyncInstructions` | Optimization | Sync instruction redundancy elimination |
| 72 | `LateExpandSyncInstructions` | Lowering | Final sync pseudo-op expansion to SASS |
| 99 | (Architecture-specific) | Lowering | Post-RA sync expansion |
| 100 | (Architecture-specific) | Lowering | Architecture vtable dispatch for sync |
| 114 | (Post-scheduling) | Fixup | Post-scheduling dependency barrier fixup |

The progression is: early fence insertion (25) -> cross-function barrier elimination (26) -> mbarrier expansion (42) -> optimization within partial-SSA (71) -> final expansion (72) -> post-RA architecture hooks (99, 100) -> post-scheduling fixup (114).

### Ori IR Opcode 130 -- Sync Analysis Target

The optimizer phases 26 and 71 identify synchronization instructions by checking for Ori opcode 130 (`HSET2` in the ROT13 name table; used as an internal Ori IR marker for barrier/sync instructions -- actual SASS BAR is opcode 61, MEMBAR is opcode 111). For each barrier instruction found:

1. Extract the destination operand from `field+84`
2. Resolve the register through the register table at `context+88`
3. Test whether the register's use-count (`reg+24`) indicates consumption
4. If the barrier result is dead (no thread observes it before the next dominating barrier), eliminate the barrier
5. At block boundaries, attempt to merge barriers with compatible scopes

### Knobs and Gating

| Knob / Flag | Effect |
|---|---|
| Knob 487 | Master gate for sync optimization (checked by phases 25, 71, 72) |
| Knob 358 | Sync mode selector (0=none, 1=conservative, 2=aggressive, 3+=full multi-function) |
| Knob 472 | Barrier liveness analysis mode |
| `context+1368 bit 0` | Global sync optimization enable |
| `context+1397 bits[6:7]` | Architecture-specific sync configuration |
| `context+1398 bits[3:4]` | Sync expansion mode (architecture-dependent) |
| `DisableErrbarAfterMembar` | Suppress error barrier insertion after membar |

## SASS Opcode Summary Table

Complete mapping of all synchronization and warp SASS opcodes, with their ROT13-encoded internal names as found in the ptxas binary:

| ROT13 (Binary) | SASS Opcode | Table Offset | Category |
|---|---|---|---|
| `IBGR` | `VOTE` | 4600 | Warp vote |
| `IBGRH` | `VOTEU` | -- | Uniform warp vote (sm100+) |
| `ONE` | `BAR` | 5160 | Thread-block barrier |
| `ONE_VAQRKRQ` | `BAR_INDEXED` | -- | Indexed barrier variant |
| `REEONE` | `ERRBAR` | 4184 | Error barrier |
| `QRCONE` | `DEPBAR` | -- | Dependency barrier (scoreboard) |
| `ZNGPU` | `MATCH` | -- | Warp match |
| `ZRZONE` | `MEMBAR` | -- | Memory barrier |
| `JNECFLAP` | `WARPSYNC` | -- | Warp synchronize |
| `OFLAP` | `BSYNC` | -- | Convergence barrier sync |
| `OFFL` | `BSSY` | -- | Convergence barrier set |
| `E2O` | `R2B` | 5128 | Register to barrier transfer |
| -- | `ELECT` | -- | Warp lane election (sm75+) |
| -- | `NANOSLEEP` | -- | Nanosleep hint |
| -- | `FENCE_G` | -- | Global fence (sm100+) |
| -- | `FENCE_S` | -- | Shared fence (sm100+) |
| -- | `FENCE_T` | -- | Texture fence (sm100+) |
| -- | `CGABAR_ARV` | -- | CGA barrier arrive (sm100+) |
| -- | `CGABAR_GET` | -- | CGA barrier query (sm100+) |
| -- | `CGABAR_SET` | -- | CGA barrier set (sm100+) |
| -- | `CGABAR_WAIT` | -- | CGA barrier wait (sm100+) |
| -- | `CGAERRBAR` | -- | CGA error barrier (sm100+) |
| -- | `SYNCS_BASIC` | -- | Basic sync (sm100+) |
| -- | `SYNCS_LD_UNIFM` | -- | Sync with uniform load (sm100+) |

## Key Function Reference

| Address | Identity | Size | Purpose |
|---|---|---|---|
| `sub_580E50` | VoteCodegen | ~3.2KB | PTX `vote.*` to inline PTX body |
| `sub_5801D0` | ShflCodegen | ~3.3KB | PTX `shfl.*` to inline PTX body |
| `sub_58A730` | MatchCodegen | ~4.5KB | PTX `match.*` to inline PTX body |
| `sub_567680` | ReduxCodegen | ~2.0KB | PTX `redux.*` to inline PTX body |
| `sub_524FB0` | BarSyncCodegen | ~1.8KB | PTX `bar.sync` / `bar` |
| `sub_570290` | BarrierCodegen | ~2.5KB | PTX `barrier.*` (PTX 8.0) |
| `sub_500BF0` | BarArriveCodegen | ~1.2KB | PTX `bar.arrive` |
| `sub_570940` | BarrierArriveCodegen | -- | PTX `barrier.arrive` |
| `sub_52D590` | BarRedCodegen | ~1.5KB | PTX `bar.red.{and,or,popc}` |
| `sub_5889B0` | BarrierRedCodegen | ~4.8KB | PTX `barrier.red` |
| `sub_56A5A0` | BarWarpCodegen | ~1.9KB | PTX `bar.warp.sync` |
| `sub_4DB410` | MembarCodegen | ~0.8KB | PTX `membar.*` |
| `sub_A9A410` | isSM70WarpSync | 194B | Intrinsic prefix detection |
| `sub_A94440` | isNonTrivialMBarrier | 412B | Mbarrier operation classifier |
| `sub_A9A5F0` | classifyMBarrier | -- | Mbarrier type enum resolver |
| `sub_A9A920` | resolveMBarrierBaseName | -- | Extract mbarrier base name from symbol |
| `sub_AA33C0` | constructMBarrierSymbol | -- | Build `%%mbarrier_%s_%s` symbol |
| `sub_1392E30` | StageAndFence (phase 25) | 166B entry | Post-unroll fence insertion |
| `sub_1390B30` | StageAndFence core | 8,956B | Main fence insertion logic |
| `sub_790A40` | RemoveRedundantBarriers | 2,288B | Cross-function barrier elimination |
| `sub_90A340` | OptimizeSyncInstructions | 1,670B | Sync instruction optimization |
| `sub_1381DA0` | LateExpandSync | 1,517B | Final sync expansion |
| `sub_C0EB10` | InstructionSelector | 185KB | Main ISel (handles all sync opcodes) |
