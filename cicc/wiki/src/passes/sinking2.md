# Sinking2 (NVIDIA Code Sinking)

`sinking2` is an NVIDIA-proprietary instruction sinking pass that moves instructions closer to their uses, with specific awareness of GPU texture and surface memory operations. It is entirely distinct from LLVM's stock `sink` pass: while both perform code sinking, Sinking2 is tailored for NVIDIA's memory hierarchy and iterates to a fixed point rather than making a single pass. The primary motivation is reducing register pressure by deferring computation of values until just before they are consumed, which is especially impactful on GPUs where register files are shared across hundreds of concurrent threads.

The pass is particularly focused on sinking instructions into texture load blocks. Texture operations on NVIDIA GPUs have high latency but are served by a dedicated cache; by sinking the address computation and other operands into the block that performs the texture fetch, the compiler reduces the live range of those values and frees registers for other warps. This directly improves occupancy -- the number of warps that can execute simultaneously on an SM.

## Pipeline Position

| Field | Value |
|---|---|
| Pass name (pipeline) | `sinking2` |
| Pass ID | `sink2` |
| Display name | `Code sinking` |
| Pass type | FunctionPass (NVIDIA-custom) |
| Class | `llvm::Sinking2Pass` |
| Legacy PM entry | `sub_1CCA270` |
| New PM entry | `sub_2D1C160` (19KB) |
| Legacy PM registration | `sub_1CC7010` |
| New PM registration | `sub_2D1B410` |
| Knob constructor | `ctor_275` at `0x4F7750` |
| Vtable (Legacy) | `off_49F8BC0` |
| Vtable (New PM) | `off_4A260F0` |

## Relationship to All Sink Passes in cicc

CICC v13.0 contains **five** distinct sinking mechanisms. Understanding which is which is essential when reading the pipeline or debugging register pressure issues:

| Pass ID / Factory | Class | Origin | Key Difference |
|---|---|---|---|
| `sink` / `sub_1A634D0` | LLVM `SinkingPass` | Upstream LLVM | Stock single-pass sinking, uses MemorySSA for alias safety |
| `sink2` / `sub_1CCA270` | `llvm::Sinking2Pass` | NVIDIA | Texture-aware, iterative fixpoint, custom AA layer |
| `sink<rp-aware>` | Parameterized variant | LLVM + NVIDIA | Register-pressure-aware sinking (stock sink with `rp-aware-sink=true`) |
| `NVVMSinking2` / `sub_1CC60B0` | NVIDIA late sinking | NVIDIA | Late-pipeline SM-specific sinking, gated by `opts[3328]` |
| `MachineSink` | LLVM `MachineSinking` | LLVM | MIR-level sinking, opt-in for NVPTX via `nvptx-enable-machine-sink` |

The stock LLVM sink (`sub_1869C50`, called with params `(1,0,1)`) uses MemorySSA for alias queries and makes a single pass. Sinking2 uses its own alias analysis layer routed through `sub_13575E0` and iterates to convergence. NVVMSinking2 (`sub_1CC60B0`) is a separate NVIDIA pass that runs late in the pipeline after barrier lowering and warp-level optimizations, gated by the SM-specific pass group flag `opts[3328]`.

## Algorithm

### Entry Point

The legacy PM entry `sub_1CCA270` performs these steps:

1. Fetches `DominatorTree` analysis (via `DominatorTreeWrapperPass` at `unk_4F9E06C`)
2. Fetches `LoopInfo` analysis (via `LoopInfoWrapperPass` at `unk_4F96DB4`)
3. Reads `sink-into-texture` knob (`qword_4FBF2C0[20]`) -- must be non-zero (enabled)
4. Reads `sink-limit` knob (`qword_4FBF1E0[20]`) -- must be greater than zero
5. Calls the main worklist driver `sub_1CC9110`

The New PM entry `sub_2D1C160` (19KB) performs the same logic using `AnalysisManager` to fetch analyses, then dispatches to `sub_2D1CFB0` (13KB).

The pass does **not** require ScalarEvolution (SCEV), MemorySSA, or PostDominatorTree, keeping it simpler and cheaper than loop-oriented or MemorySSA-dependent passes.

### Main Worklist Driver (`sub_1CC9110`, 22KB)

The core algorithm is a fixpoint iteration over the dominator tree:

```
function SinkingWorklist(F, DT, LI, textureLevel, sinkLimit):
    changed = false
    do:
        roundChanged = false
        sinkCount = 0

        // Walk dominator tree in DFS preorder
        for BB in DT.dfs_preorder():
            // Skip loop headers to avoid creating loop-carried deps
            if LI.isLoopHeader(BB):
                continue

            // Process instructions bottom-up within each block
            for I in reverse(BB.instructions()):
                if sinkCount >= sinkLimit:
                    break                  // complexity limiter

                if I.mayHaveSideEffects() or I.isTerminator():
                    continue               // unsinkable
                if I.use_empty():
                    continue               // dead, leave for DCE

                // Level 3: consider instructions used only outside BB
                if textureLevel < 3 and allUsesInSameBlock(I, BB):
                    continue

                targetBB = findBestSinkTarget(I, DT, LI)  // sub_1CC7510
                if targetBB == BB:
                    continue               // already in best position

                // Profitability: prefer texture/surface blocks
                if textureLevel >= 1:
                    if not blockContainsTextureOps(targetBB):
                        if not dominatesTextureBlock(targetBB, DT):
                            continue       // not profitable

                // Safety: alias analysis check
                if not isSafeToSink(I, BB, targetBB):     // sub_1CC8920
                    continue
                // Safety: memory dependency check
                if not checkMemDep(I, BB, targetBB):       // sub_1CC8CA0
                    continue

                I.moveBefore(targetBB.firstNonPHI())
                roundChanged = true
                sinkCount++

        changed |= roundChanged
    while roundChanged            // iterate until no more changes

    return changed
```

Key design points:

- **DFS preorder** ensures parent blocks are processed before children. Instructions sunk from a parent into a child on one iteration may expose further sinking opportunities for grandchild blocks on the next iteration -- hence the fixpoint loop.
- **Bottom-up within each block** processes the last instruction first. This is important because sinking an instruction may make an earlier instruction's operands dead, which DCE will clean up later.
- **Loop headers are skipped** to prevent creating loop-carried dependencies (a value defined in the header, consumed in the latch, sunk into the latch would create a cycle).

### Instruction Processing (`sub_1CC7510`, 16KB)

For each candidate instruction, this function:

1. Walks the use chain to find all consumers (via `sub_15F4D60`, multi-use check)
2. For each user, determines the containing basic block
3. Computes the **lowest common dominator** (LCD) of all user blocks using the dominator tree
4. If LCD == current block, no benefit from sinking -- the instruction is already as close to its uses as possible while dominating all of them
5. Builds a sink mapping: instruction to target block
6. Checks memory safety via alias analysis (`sub_13575E0`)
7. Validates that sinking does not violate memory ordering constraints
8. Respects PHI nodes (LLVM opcode `PHI`) as sink boundaries -- an instruction cannot be sunk past a PHI insertion point

The target block selection algorithm effectively finds the **nearest common dominator** of all uses that is **strictly dominated by** the current block. If the instruction has a single use, the target is trivially the use's block (or its immediate dominator if the use is a PHI operand).

### Dominance Ordering (`sub_1CC8170`, 13KB)

Implements a hash-based ordering of basic blocks for comparing sink profitability. Uses DFS numbering from the dominator tree to determine which block comes "earlier" in the program. This ordering ensures:

- Instructions are only sunk **toward uses**, never away from them
- When multiple sink targets exist (multi-use instruction), the lowest common dominator is chosen
- The ordering is consistent across iterations of the fixpoint loop

### Alias Checking (`sub_1CC8920`, 4KB)

Validates that moving instruction `I` from block `From` to block `To` does not reorder `I` past any conflicting memory access:

```
function isSafeToSink(I, From, To):
    if not I.mayReadOrWriteMemory():
        return true                    // pure computation, always safe

    // Walk all instructions on the domtree path From -> To
    for BB in pathBlocks(From, To):
        for J in BB.instructions():
            if J == I: continue
            if AA.getModRefInfo(I, J) != NoModRef:
                return false           // conflict: I aliases with J

    return true
```

This is **not** MemorySSA-based (unlike stock LLVM sink). The pass invokes the traditional `AliasAnalysis` query interface through `sub_13575E0`. This is less precise than MemorySSA but avoids the cost of building and maintaining the MemorySSA graph, which matters because Sinking2 iterates to fixpoint and would need to update MemorySSA on every move.

### Memory Dependency Checking (`sub_1CC8CA0`, 6KB)

Additional memory safety layer beyond alias checking:

- **Store-load forwarding**: if `I` is a load and there is a store between `From` and `To` that may alias the loaded location, sinking would change the value loaded
- **Store ordering**: if `I` is a store, moving it past another store to a potentially-aliasing location changes program semantics
- **Volatile/atomic barrier**: volatile loads/stores and atomic operations are never sunk (treated as having side effects)
- **Synchronization intrinsics**: barrier calls (`__syncthreads`, `bar.sync`) are treated as memory fences; no instruction may be sunk past them

### Texture/Surface Awareness

The pass identifies "texture blocks" -- basic blocks containing calls to texture/surface intrinsics (the `tex.*`, `suld.*`, `sust.*` family). Address computations that feed these intrinsic calls are the primary sink candidates, because texture address computation chains (`GEP` + index arithmetic) produce intermediate values that are consumed only at the texture fetch site. Without sinking, these intermediates occupy registers across potentially many instructions.

The `sink-into-texture` knob controls aggressiveness:

| Level | Behavior |
|---|---|
| 0 | Disabled -- no texture-aware sinking |
| 1 | **Cross-block only**: move instructions across block boundaries into texture blocks |
| 2 | **Cross-block + intra-block**: also reorder instructions within a block to position them immediately before their texture use |
| 3 (default) | **All of the above + outside-only**: consider instructions whose only uses are in blocks other than where the instruction is defined |

Level 3 catches the important case where a GEP in a preheader feeds a texture load inside a loop -- the GEP has no uses in its own block, only "outside" uses.

Address space checks for NVPTX (see [reference/address-spaces](../reference/address-spaces.md)):
- AS 1 (global): may alias with texture reads in some configurations
- AS 3 (shared): texture operations never access shared memory, so shared-space stores are not barriers to texture sinking
- AS 4 (const): texture/surface descriptors typically live in constant memory
- AS 5 (local): thread-local, no cross-thread interference

### Loop Considerations

Sinking2 is loop-aware but conservative:

1. **Never sinks OUT of a loop**: moving an instruction from a loop body to an exit block would change its execution count. The pass skips this entirely.
2. **May sink INTO loop bodies**: when an instruction in a loop preheader feeds only uses inside the loop (particularly texture fetches), sinking it into the loop is profitable despite increasing execution count -- the register pressure reduction from shorter live ranges outweighs the extra computation.
3. **Skips loop headers**: prevents creating loop-carried dependencies.
4. **Runs after LoopSimplify**: the early instance (`sub_18B1DE0`) runs after LoopSimplify/LCSSA have canonicalized loop structure, so preheaders, latches, and exit blocks are well-formed.

This creates a deliberate tension with LICM:
- **LICM** hoists loop-invariant code **into** the preheader (reducing execution count)
- **Sinking2** sinks non-invariant address computation **out of** the preheader and into the loop body (reducing register pressure)

The two passes run at different pipeline positions and balance each other. LICM runs first; Sinking2 runs after GVN and CGSCC inlining, when texture patterns are fully exposed.

### Barrier Awareness

Sinking2 itself does not contain explicit `__syncthreads` / `bar.sync` detection logic. Instead, it relies on the LLVM side-effect model:

- Barrier intrinsics are marked as having side effects, so they are never sunk
- Barrier intrinsics are treated as memory fences by alias analysis, so no memory instruction may be sunk past them

The **late** NVVMSinking2 (`sub_1CC60B0`) runs after barrier lowering (`sub_1CB73C0`) and warp-level optimization passes. By that point, barriers have been lowered to their final form. The pipeline ordering is:

```
NVVMBranchDist -> NVVMWarpShuffle -> NVVMReduction -> NVVMSinking2
```

This sequence ensures NVVMSinking2 can sink past warp-level operations that are no longer opaque barriers, while still respecting the lowered barrier representation.

## Multi-Run Pipeline Pattern

Sinking2 appears at **three to four** pipeline positions. Each run has different context and different opportunities:

| Position | Factory | Mode | Context |
|---|---|---|---|
| Early (pass ~39) | `sub_18B1DE0()` | Standard | After stock Sink, GVN, and CGSCC inlining. Texture patterns are exposed. |
| Post-peephole | `sub_18B3080(1)` | Fast (flag=1) | After NVVMPeephole. Peephole may create new sinking opportunities. Reduced iteration budget. |
| Late SM-specific | `sub_1CC60B0()` | SM-gated | After barrier lowering and warp shuffle. Gated by `opts[3328] && !opts[2440]`. |

For fast-compile mode (Ofcmax), only `sub_18B3080(1)` runs -- the single Sinking2 in fast mode with reduced iteration budget. No stock Sink, no NVVMSinking2.

The rationale for multiple runs:
- **Run 1 (stock Sink)** handles straightforward cases using MemorySSA's precise alias information
- **Run 2 (Sinking2 early)** performs texture-aware sinking now that GVN/CGSCC have simplified the IR
- **Run 3 (Sinking2 fast)** cleans up opportunities created by peephole optimization
- **Run 4 (NVVMSinking2)** performs SM-specific late sinking after barrier and warp-level transforms

### NVVMPassOptions Gating

| Offset | Type | Effect |
|---|---|---|
| `opts[1040]` | bool | Disable stock Sink/MemSSA |
| `opts[2440]` | bool | Disable NVVMSinking2 (`sub_1CC60B0`) |
| `opts[3328]` | bool | Enable SM-specific warp/reduction/sinking pass group (gates NVVMSinking2) |

## Cost Model (New PM)

The New PM object (176 bytes) contains floating-point thresholds at offsets `+88` and `+144`, both initialized to `1065353216` (IEEE 754 `1.0f`). These thresholds suggest the New PM implementation has a more sophisticated cost model than the Legacy PM version:

- **Profitability threshold** (`+88`): minimum benefit score for a sink to be accepted. A value of 1.0 means the benefit must at least equal the cost.
- **Cost threshold** (`+144`): maximum acceptable cost for the sinking motion itself. A value of 1.0 means the movement cost must not exceed the baseline.

The Legacy PM version uses a simpler boolean profitability model (is the target a texture block? yes/no).

## Configuration Knobs

### Sinking2-Specific (ctor_275 at `0x4F7750`)

| Knob | Type | Default | Storage | Description |
|---|---|---|---|---|
| `sink-into-texture` | int | 3 | `qword_4FBF2C0` | Texture sinking aggressiveness (0=off, 1=cross-block, 2=+intra, 3=+outside-only) |
| `sink-limit` | int | 20 | `qword_4FBF1E0` | Max instructions to sink per invocation (complexity limiter) |
| `dump-sink2` | bool | false | `qword_4FBF100` | Dump debug information during sinking |

### Related Sinking Knobs (other passes, NOT Sinking2)

| Knob | Type | Default | Owner | Description |
|---|---|---|---|---|
| `sink-check-sched` | bool | true | stock Sink | Check scheduling effects of sinking |
| `sink-single-only` | bool | true | stock Sink | Only sink single-use instructions |
| `rp-aware-sink` | bool | false | stock Sink | Consider register pressure (controls `sink<rp-aware>` variant) |
| `max-uses-for-sinking` | int | (default) | stock Sink | Don't sink insts with too many uses |
| `sink-ld-param` | bool | (default) | NVPTX backend | Sink one-use `ld.param` to use point |
| `hoist-load-param` | bool | (default) | NVPTX backend | Hoist all `ld.param` to entry block (counterpart to `sink-ld-param`) |
| `enable-andcmp-sinking` | bool | (default) | CodeGenPrepare | Sink and/cmp into branches |
| `aggressive-no-sink` | bool | (default) | (unknown) | Sink all generated instructions |
| `instcombine-code-sinking` | bool | (default) | InstCombine | Enable code sinking within instcombine |
| `nvptx-enable-machine-sink` | bool | (default) | NVPTX backend | Enable MIR-level MachineSink |
| `SinkRematEnable` | bool | (default) | ptxas | Enable sink+rematerialization in ptxas |

## Analysis Dependencies

| Legacy PM | New PM | Purpose |
|---|---|---|
| `DominatorTreeWrapperPass` (`unk_4F9E06C`) | `DominatorTreeAnalysis` (`sub_CF6DB0`) | Dominator tree for sink legality and ordering |
| `LoopInfoWrapperPass` (`unk_4F96DB4`) | `LoopAnalysis` (`sub_B1A2E0`) | Avoid sinking out of loops; skip loop headers |

Does **not** require: SCEV, MemorySSA, PostDominatorTree, BranchProbabilityInfo.

This is a key difference from stock LLVM `SinkingPass`, which requires `MemorySSAAnalysis`. Sinking2 uses its own alias analysis queries through helpers `sub_1CC8920` and `sub_1CC8CA0`, routed through the traditional AA interface at `sub_13575E0`. This avoids the overhead of building/maintaining MemorySSA across fixpoint iterations.

## Pass Object Layout

**Legacy PM** (160 bytes):

| Offset | Type | Content |
|---|---|---|
| +0 | ptr | Vtable pointer (`off_49F8BC0`) |
| +8 | ptr | Pass link (next pass in chain) |
| +16 | ptr | Pass ID pointer (`&unk_4FBF0F4`) |
| +24 | int32 | Mode (default=3, from `sink-into-texture`) |
| +28 | int32 | Sink limit (default=20, from `sink-limit`) |
| +32--48 | ptr[3] | Worklist data (head, tail, size) |
| +56 | ptr | DominatorTree* (set during runOnFunction) |
| +64 | ptr | List head 1 (self-referential sentinel) |
| +72--80 | ptr[2] | List next/prev 1 |
| +96 | int64 | Counter (sink count for current iteration) |
| +104 | ptr | LoopInfo* (set during runOnFunction) |
| +112 | ptr | List head 2 (self-referential sentinel) |
| +120--128 | ptr[2] | List next/prev 2 |
| +144 | int64 | Data field |
| +152 | byte | Changed flag (for fixpoint termination) |

**New PM** (176 bytes): two embedded worklists and float thresholds at offsets +88 and +144 (value `1065353216` = `1.0f` IEEE 754).

## Differences from Upstream LLVM

| Aspect | Upstream LLVM `sink` | NVIDIA `sinking2` |
|---|---|---|
| Alias analysis backend | MemorySSA | Custom AA layer (`sub_13575E0`) |
| Iteration strategy | Single pass | Fixpoint iteration |
| Texture awareness | None | 3-level configurable |
| Address space awareness | Generic | NVPTX-specific (AS 1,3,4,5) |
| Complexity limiter | None | `sink-limit` knob (default=20) |
| Intra-block reordering | No | Level >= 2 |
| Outside-only pattern | No | Level == 3 |
| Debug dump | Standard LLVM debug | `dump-sink2` knob |
| Cost model | Boolean (profitable or not) | Float thresholds in New PM |
| Pipeline occurrences | 1 | 3--4 (multi-run strategy) |
| Fast-compile variant | Same pass | Dedicated fast=1 mode |

## Diagnostic Strings

| String | Context |
|---|---|
| `"llvm::Sinking2Pass]"` | RTTI name at `sub_2315E20` |
| `"sink2"` | Pipeline parser ID |
| `"Code sinking"` | Display name (shared with stock LLVM sink) |
| `"sinking2"` | New PM pipeline string match |

## Function Map

| Address | Size | Role |
|---|---|---|
| `sub_1CC7010` | -- | Legacy PM pass registration |
| `sub_1CC7100` | -- | Legacy PM factory |
| `sub_1CC71E0` | -- | Legacy PM alternate factory |
| `sub_1CC7510` | 16KB | `processInstruction`: sink candidate evaluation, use-chain walk, LCD computation |
| `sub_1CC8170` | 13KB | Dominance ordering: DFS numbering for block comparison |
| `sub_1CC8920` | 4KB | Alias checking helper: validates no conflicting memory accesses on path |
| `sub_1CC8CA0` | 6KB | Memory dependency helper: store-load forwarding, store ordering, volatile |
| `sub_1CC9110` | 22KB | Main worklist driver: fixpoint iteration over dominator tree |
| `sub_1CCA270` | -- | Legacy PM `runOnFunction` entry |
| `sub_2D1B410` | -- | New PM pass registration |
| `sub_2D1BC50` | -- | New PM factory |
| `sub_2D1C160` | 19KB | New PM `run()` entry |
| `sub_2D1CFB0` | 13KB | New PM core logic |
| `sub_2D1D770` | 7KB | New PM helper |
| `sub_2D1DCF0` | 7KB | New PM helper |
| `sub_2315E20` | -- | RTTI name printer |
| `0x4F7750` | -- | Knob constructor (`ctor_275`) |

**Related pipeline factories:**

| Address | Role |
|---|---|
| `sub_18B1DE0` | Sinking2 early-pipeline factory |
| `sub_18B3080` | Sinking2 fast-mode factory (accepts fast flag parameter) |
| `sub_1CC60B0` | NVVMSinking2 late-pipeline factory |
| `sub_1A634D0` | Stock LLVM Sink legacy PM registration |
| `sub_29776B0` | Stock LLVM Sink New PM registration |
| `sub_1B51110` | Stock Sink core (51KB, creates `.sink.split` / `.sink` blocks) |
| `sub_1869C50` | Stock Sink pipeline factory (called with params `1,0,1`) |

**Total code size:** ~80KB (Legacy PM) + ~65KB (New PM) = ~145KB

## GPU-Specific Motivation

Register pressure is the dominant performance constraint on NVIDIA GPUs. Each SM has a fixed register file (e.g., 65,536 registers on SM 8.x) shared among all active warps. Every register consumed by a warp reduces the number of warps that can be resident, directly reducing occupancy and the GPU's ability to hide memory latency through warp switching.

Sinking instructions closer to their uses shortens live ranges and reduces the peak number of simultaneously live registers. This is especially valuable for texture load sequences, which typically involve address computation (GEP chains, index arithmetic) that produces values consumed only at the texture fetch site. Without sinking, these intermediate values occupy registers across potentially many instructions, bloating register pressure unnecessarily.

The three-level `sink-into-texture` design reflects a graduated approach to this optimization: level 1 handles the common case (cross-block sinking), level 2 adds intra-block reordering for tighter packing, and level 3 (the default) handles the edge case where an instruction's only uses are in blocks other than where it is defined, enabling more aggressive motion.

The multi-run pattern (early Sinking2, post-peephole fast Sinking2, late NVVMSinking2) ensures that sinking opportunities created by other optimization passes are captured throughout the pipeline, rather than relying on a single sinking point that may miss opportunities not yet exposed.

## Cross-References

- [Dead Synchronization Elimination](dead-sync-elimination.md) -- runs earlier, removes barriers that Sinking2 would otherwise treat as memory fences
- [LICM](../llvm/licm-real.md) -- counterpart: hoists loop-invariant code into preheaders; Sinking2 sinks address computation out of preheaders
- [NVVMPeephole](nvvm-peephole.md) -- runs before late Sinking2, may create new sinking opportunities
- [Rematerialization](rematerialization.md) -- runs after all sinking; rematerialization + sinking together minimize register pressure (ptxas `SinkRematEnable` knob)
- [MemorySpaceOpt](../passes/other.md) -- changes address spaces which affects sinking profitability
- [NVVMPassOptions](../config/nvvm-pass-options.md) -- `opts[1040]` disables stock Sink; `opts[2440]` disables NVVMSinking2
- [Register Allocation](../llvm/register-allocation.md) -- ultimate consumer of the register pressure reduction that sinking provides
- [Optimization Levels](../config/optimization-levels.md) -- Ofcmax runs only fast-mode Sinking2; O2/O3 run full multi-run pattern
