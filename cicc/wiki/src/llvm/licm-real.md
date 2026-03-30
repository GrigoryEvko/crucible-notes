# LICM (Loop-Invariant Code Motion)

Loop-Invariant Code Motion in cicc v13.0 operates at three distinct levels: an IR-level pass (`"licm"`, backed by MemorySSA), a pre-RA machine pass (`"early-machinelicm"`), and a post-RA machine pass (`"machinelicm"`). The IR-level pass runs in two modes within the same pipeline -- a **hoist invocation** early in the optimization sequence that pulls invariant computations and loads out of loops into preheaders, and a **sink invocation** via `LoopSinkPass` (or implicit re-processing) later that pushes unprofitable hoists back into cold loop blocks. On a CPU, hoisting is almost universally profitable because the preheader executes once per loop entry rather than once per iteration. On a GPU, the calculus is different: every value hoisted into the preheader extends its live range across the entire loop body, consuming a register for all iterations. If that extra register pushes the kernel past an occupancy cliff -- the threshold where the SM can fit one fewer warp -- the net effect is a slowdown, not a speedup. NVIDIA addresses this tension through the interplay of the two invocations, the NVVM alias analysis pipeline that makes cross-address-space loads trivially hoistable, and the downstream rematerialization passes that can undo hoists that turned out to be unprofitable after register allocation.

## Key Facts

| Property | Value |
|---|---|
| **IR pass name** | `"licm"` (new PM), `"LICMPass"` (legacy) |
| **IR pass factory** | `sub_195E880(0)` -- creates LICM with `AllowSpeculation=false` |
| **IR pass factory (alt)** | `sub_184CD60()` -- creates LICM (also identified as ConstantMerge in some sweeps; identity ambiguous -- see Analysis Notes) |
| **Machine pass (pre-RA)** | `"early-machinelicm"` / `EarlyMachineLICMPass` |
| **Machine pass (post-RA)** | `"machinelicm"` / `MachineLICMPass` |
| **Knob registration** | `ctor_457_0` at `0x544C40` (18,398 bytes -- 11 knobs) |
| **MachineLICM knob registration** | `ctor_305` (4 knobs) |
| **Pipeline disable flag** | `-disable-LICMPass` via `-Xcicc` |
| **PassOptions disable** | `-opt "-do-licm=0"` (also forced by `--emit-optix-ir`) |
| **NVVMPassOptions slot** | `opts[1240]` (disable), `opts[2880]` (enable, reversed logic) |
| **Upstream LLVM source** | `llvm/lib/Transforms/Scalar/LICM.cpp`, `llvm/lib/CodeGen/MachineLICM.cpp` |

## Pipeline Positions

LICM appears at multiple pipeline positions depending on the optimization tier and compilation mode. The pass uses two distinct factory functions, and the identification of which is definitively LICM versus another pass is uncertain in some cases due to the stripped binary. The following table lists all confirmed appearances.

### IR-Level LICM

| Position | Call site | Factory | Guard condition | Context |
|---|---|---|---|---|
| O1 baseline, position 12 | `sub_12DE330` | `sub_184CD60()` | none | After LoopRotate, before IndVarSimplify. First hoist invocation. |
| Main optimizer, mid-pipeline | `sub_12DE8F0` | `sub_195E880(0)` | `!opts[1240]` | Guarded by the LICM disable flag. Runs after DCE and before NVVMLowerBarriers. |
| Main optimizer, late | `sub_12DE8F0` | `sub_195E880(0)` | `opts[2880] && !opts[1240]` | Second invocation, guarded by both enable and disable flags. Runs after ADCE, before LoopUnroll. |
| Extended pipeline | `sub_12E54A0` | `sub_195E880(0)` | `opts[2880] && !opts[1240]` | After NVVMLowerBarriers, before LoopUnroll. |
| Late pipeline | `sub_12E54A0` | `sub_195E880(0)` | `!opts[1240]` | After LoopIdiomRecognize and LoopSimplify, before SimplifyCFG. Late cleanup invocation. |
| Aggressive (O3, "mid" path) | `sub_12E54A0` | `sub_184CD60()` | none | Position 1 and position 18 of the aggressive pipeline. Second invocation follows GVN. |

### Machine-Level LICM

| Position | Pass | Guard | Context |
|---|---|---|---|
| Pre-RA | `early-machinelicm` | `enable-mlicm` | After EarlyTailDuplicate, before MachineCSE. Controlled by the NVPTX target. |
| Post-RA | `machinelicm` | `!disable-postra-machine-licm` | After ExpandPostRAPseudos, before post-RA MachineSink. |

## Algorithm

### IR-Level: Hoist Mode

LICM's hoist mode is the upstream LLVM 20.0.0 algorithm with no visible NVIDIA patches to the core logic. The NVIDIA delta is entirely in the *analysis results* that LICM consumes (NVVM AA, MemorySSA precision, convergent-call handling) and in the *pipeline orchestration* (multiple invocations, register-pressure-aware sink mode).

The algorithm processes each loop from innermost to outermost:

```
for each loop L in post-order (innermost first):
    preheader = L.getLoopPreheader()
    if preheader is null: skip

    // 1. Collect candidates
    for each basic block BB in L:
        for each instruction I in BB:
            if isLoopInvariant(I, L) and isSafeToHoist(I, L):
                candidates.push(I)

    // 2. Hoist each candidate
    for I in candidates:
        if I is a load:
            // Query MemorySSA walker for clobbering stores
            clobber = MSSA.getClobberingMemoryAccess(I)
            if clobber is outside L:
                hoist(I, preheader)
        else if I is a pure computation (no side effects):
            hoist(I, preheader)
        else if I is a store and hoist-const-stores is enabled:
            if store address is loop-invariant and
               no other store in L aliases this address:
                hoist(I, preheader)
```

The `isLoopInvariant` check verifies that all operands of the instruction are either defined outside the loop or are themselves loop-invariant. The `isSafeToHoist` check queries MemorySSA to determine whether the instruction's memory behavior is loop-invariant -- for loads, this means no store inside the loop may alias the load's address.

**MemorySSA walker interaction.** When LICM calls `getClobberingMemoryAccess(load_in_loop)`, the MemorySSA walker walks upward from the load's MemoryUse through the MemorySSA graph. If the walk reaches the loop's entry MemoryPhi without encountering a MemoryDef that may-alias the load, the load is hoistable. The walk is bounded by `licm-mssa-optimization-cap` to prevent compile-time explosion on functions with dense memory SSA graphs.

**The `licm-mssa-max-acc-promotion` knob** limits how many MemoryAccesses LICM will attempt to promote (scalar-replace loads from loop-invariant addresses with SSA values held in registers across iterations). This is the LICM variant of store-to-load forwarding within a loop.

### IR-Level: Sink Mode

The LoopSink pass (`"loop-sink"`, registered at pipeline parser entry 271) is the inverse of hoist mode. It runs late in the pipeline and pushes instructions that were hoisted to the preheader back into the loop body, specifically into cold blocks that execute infrequently relative to the loop header.

The decision to sink is driven by block frequency analysis:

```
for each instruction I in preheader:
    if I has uses only in cold blocks of the loop:
        coldest_block = argmin(blockFreq(B) for B where I is used in B)
        if blockFreq(preheader) / blockFreq(coldest_block) > threshold:
            sink(I, coldest_block)
```

On GPUs, the sink mode is particularly important because:

1. **Occupancy recovery.** A hoist that added one live register at the preheader may have pushed the kernel from 8 to 7 warps per SM. Sinking that value back undoes the damage.
2. **Divergent control flow.** If the hoisted value is only used in a branch taken by some threads (divergent execution), hoisting forces all threads to compute it. Sinking limits the computation to the threads that actually take the branch.

### Machine-Level: MachineLICM

MachineLICM operates on `MachineInstr` after instruction selection. The pre-RA variant (`early-machinelicm`) is gated by the `enable-mlicm` knob, which is controlled by the NVPTX target. The post-RA variant (`machinelicm`) runs unconditionally unless `disable-postra-machine-licm` is set.

The machine-level algorithm differs from the IR level in that it has concrete register pressure information:

```
for each machine loop ML (innermost first):
    preheader = ML.getLoopPreheader()
    for each MachineInstr MI in ML:
        if isLoopInvariant(MI) and isSafeToHoist(MI):
            // Compute pressure impact
            pressure_delta = estimatePressureIncrease(MI, preheader)
            if sink-insts-to-avoid-spills and
               pressure_delta would cause spills:
                skip MI  // Do not hoist
            else:
                hoist(MI, preheader)
```

The `sink-insts-to-avoid-spills` knob (registered at `ctor_305`) is the critical GPU-specific control: it tells MachineLICM to abandon a hoist when the resulting register pressure in the preheader would exceed the spill threshold. This directly prevents the occupancy-cliff problem at the machine level.

## GPU-Specific Considerations

### Register Pressure and Occupancy Cliffs

On NVIDIA GPUs, register allocation is per-thread, and the total register file per SM is shared among all resident threads. The relationship between per-thread register count and achievable occupancy is a step function with sharp cliffs:

| Registers/thread | Max warps/SM (Ampere) | Occupancy |
|---|---|---|
| 32 | 64 | 100% |
| 40 | 48 | 75% |
| 48 | 32 | 50% |
| 64 | 32 | 50% |
| 80 | 24 | 37.5% |
| 128 | 16 | 25% |
| 255 | 8 | 12.5% |

Hoisting one additional value into the preheader extends its live range across the entire loop body, increasing peak register pressure by one. If that increase crosses an occupancy cliff boundary, the kernel loses an entire warp's worth of parallelism per SM. This is why cicc invokes LICM early (to expose optimization opportunities for GVN, DSE, and InstCombine) and then relies on the downstream rematerialization infrastructure to undo hoists that became unprofitable after the register allocator made its decisions.

### NVVM AA and Cross-Address-Space Independence

The single most impactful NVIDIA-specific behavior in LICM is not a patch to LICM itself but the NVVM alias analysis (`nvptx-aa`) that feeds into MemorySSA. When LICM queries whether a load from `addrspace(1)` (global memory) is clobbered by a store to `addrspace(3)` (shared memory), NVVM AA returns `NoAlias` immediately. This means:

- A load from global memory inside a loop is trivially hoistable past any number of shared memory stores.
- A shared memory load is hoistable past global stores.
- Only stores to the *same* address space (or to `addrspace(0)` / generic) prevent hoisting.

This dramatically increases the set of hoistable instructions compared to a flat-memory architecture. Without NVVM AA, a conservative alias analysis would assume any store could clobber any load, making most loads inside GPU kernels non-hoistable.

### Barrier-Aware Motion Constraints

CUDA `__syncthreads()` barriers are lowered to `llvm.nvvm.barrier0` intrinsic calls, which are marked `convergent` and have memory side effects on shared memory. The `convergent` attribute prevents LICM from hoisting any instruction that depends (directly or transitively through the call graph) on a convergent call. The memory side effect on the barrier prevents hoisting loads across it even when the load does not depend on the barrier's value, because the barrier's MemoryDef in MemorySSA clobbers all shared-memory accesses.

This means LICM correctly refuses to hoist a shared memory load from below a `__syncthreads()` to above it -- doing so would read a value that the barrier was supposed to synchronize.

The `NVVMLowerBarriers` pass (`sub_1C98160`) runs between LICM invocations in the pipeline. Its position matters: barriers are still at the intrinsic level during the first LICM invocation, providing the convergent/memory-effect constraint. After lowering, the barrier semantics are encoded differently, which could affect what a later LICM invocation can move.

### Interaction with Downstream Passes

LICM's hoist decisions feed into several downstream passes that can undo or refine them:

1. **Rematerialization** (`nvvmrematerialize`, `nv-remat-block`): If hoisting increased register pressure past the target, the rematerialization pass will clone the hoisted instruction back to each use site, effectively undoing the hoist while keeping the optimization benefits at the IR level. See [Rematerialization](../passes/rematerialization.md).

2. **Sinking2** (`sub_1CC60B0`): NVIDIA's custom sinking pass runs after LICM and can push instructions back toward their uses. The `rp-aware-sink` and `max-uses-for-sinking` knobs control whether the sink considers register pressure impact. See [Sinking2](../passes/sinking2.md).

3. **Base Address Strength Reduction**: Hoisted address computations are candidates for strength reduction. The `sub_1C51340` function checks whether a base address is loop-invariant, which is trivially true after LICM has hoisted it.

## Configuration

### IR-Level LICM Knobs (`ctor_457_0` at `0x544C40`)

These are standard LLVM knobs present in the cicc binary. No NVIDIA-specific knobs were found in the IR-level LICM registration.

| Knob | Type | Default | Effect |
|---|---|---|---|
| `disable-licm-promotion` | bool | false | Disable scalar promotion of memory locations (store-to-load forwarding within loops). When set, LICM will not replace repeated loads from a loop-invariant address with a register-held value. |
| `licm-control-flow-hoisting` | bool | false | Enable hoisting of instructions with control-flow-dependent execution. When disabled, only instructions that dominate the loop latch can be hoisted. |
| `licm-force-thread-model-single` | bool | false | Override the thread model to single-threaded, allowing LICM to hoist atomic operations. Not useful on GPU. |
| `licm-max-num-uses-traversed` | int | 8 | Maximum number of uses to traverse when checking whether all uses of a hoisted value are inside the loop. Limits compile time on values with many uses. |
| `licm-max-num-fp-reassociations` | int | (default) | Maximum FP reassociation chains LICM will attempt to hoist as a group. |
| `licm-hoist-bo-association-user-limit` | int | (default) | User count limit for binary operator association hoisting. |
| `licm-skip-unrolled-loops` | bool | false | Skip LICM on loops that have been unrolled (identified by metadata). Avoids re-hoisting values that were deliberately placed by the unroller. |
| `licm-insn-limit` | int | (default) | Maximum number of instructions LICM will process per loop. Compile-time safety valve. |
| `licm-max-num-int-reassociations` | int | (default) | Maximum integer reassociation chains for group hoisting. |
| `licm-mssa-optimization-cap` | int | (default) | Maximum number of MemorySSA accesses the walker will visit per query. Prevents pathological compile times on functions with dense memory access patterns. |
| `licm-mssa-max-acc-promotion` | int | (default) | Maximum number of MemoryAccesses LICM will attempt to promote (scalar-replace) per loop. |

### IR-Level LICM Pipeline Parameters

The pass text-pipeline parser accepts two parameters for the `"licm"` pass:

| Parameter | Effect |
|---|---|
| `allowspeculation` | Allow speculative execution of hoisted instructions (loads that might trap). |
| `conservative-calls` | Use conservative call analysis -- treat all calls as potentially clobbering. |

The factory function `sub_195E880(0)` creates LICM with `AllowSpeculation=false`, which is the safe default for GPU code where speculative loads from unmapped memory would fault the entire kernel.

### Machine-Level MachineLICM Knobs (`ctor_305`)

| Knob | Type | Default | Effect |
|---|---|---|---|
| `avoid-speculation` | bool | (default) | Avoid hoisting instructions that could speculatively execute and trap. |
| `hoist-cheap-insts` | bool | (default) | Hoist instructions with very low cost even when register pressure is high. |
| `sink-insts-to-avoid-spills` | bool | (default) | **Critical GPU knob.** When enabled, MachineLICM will sink (not hoist) instructions when hoisting would increase register pressure past the spill threshold. This directly trades code motion for spill avoidance. |
| `hoist-const-stores` | bool | (default) | Hoist stores of constant values out of loops. Enabled at the NVIDIA sinking/code-motion category level. |

### NVPTX Target Gating Knobs

| Knob | Type | Default | Effect |
|---|---|---|---|
| `enable-mlicm` | bool | opt-level dependent | Master enable for pre-RA `EarlyMachineLICM` on NVPTX. |
| `disable-machine-licm` | bool | false | Disable pre-RA MachineLICM (stock LLVM knob). |
| `disable-postra-machine-licm` | bool | false | Disable post-RA MachineLICM (stock LLVM knob). |

### Global Pipeline Controls

| Control | Mechanism | Effect |
|---|---|---|
| `do-licm=0` | PassOptions (`-opt` flag) | Disables IR-level LICM entirely. Automatically set by `--emit-optix-ir`. |
| `disable-LICMPass` | `-Xcicc` flag | Disables IR-level LICM via the pass-disable mechanism. |
| `opts[1240]` | NVVMPassOptions bit | Per-invocation disable flag for IR LICM. |
| `opts[2880]` | NVVMPassOptions bit | Per-invocation enable flag for IR LICM (reversed logic). |

## Diagnostic Strings

The IR-level LICM pass emits optimization remarks via the standard LLVM remark infrastructure. The following remark identifiers are present in upstream LLVM 20 and apply unchanged in cicc:

| Remark | Condition |
|---|---|
| `"hoisted"` | Instruction was successfully hoisted to preheader. |
| `"sunk"` | Instruction was sunk from preheader into a loop block. |
| `"promoted"` | Memory location was scalar-promoted (repeated load replaced with register). |
| `"licm"` | General LICM diagnostic (pass name in remark metadata). |

MachineLICM emits its own set:

| String | Condition |
|---|---|
| `"Hoisting to BB#%d"` | Machine instruction hoisted to the specified preheader block. |
| `"Won't hoist cheap instruction"` | Instruction deemed too cheap to justify the pressure increase. |
| `"Can't hoist due to spill pressure"` | `sink-insts-to-avoid-spills` vetoed the hoist. |

## Analysis Notes

### Identity Ambiguity: `sub_184CD60` and `sub_195E880`

The pipeline analysis identified two factory functions as LICM candidates:

- **`sub_195E880(0)`**: Called with explicit LICM disable guards (`!opts[1240]`, `opts[2880]`). Present in the main optimizer and extended pipeline. This is the higher-confidence identification as the IR-level LICM factory.

- **`sub_184CD60()`**: Called in the O1 baseline pipeline at position 12 (after LoopRotate), and in the aggressive pipeline. Some sweeps identify this as `ConstantMerge` or `GlobalDCE`. The O1 pipeline context (LoopRotate -> `sub_184CD60` -> IndVarSimplify) strongly suggests this is LICM, as this is the canonical upstream LLVM loop optimization sequence. However, the aggressive pipeline uses it in a position where `ConstantMerge` would also make sense. Without the stripped symbol, the definitive identification relies on structural context.

Both functions likely create the same underlying `LICMPass` -- the difference may be in the parameters (e.g., `AllowSpeculation`, `ConservativeCalls`) or the analysis dependencies they request.

### No Visible NVIDIA Patches to IR-Level LICM

Unlike DSE, GVN, and InstCombine, the IR-level LICM code does not appear to contain NVIDIA-specific modifications. The 11 knobs registered at `ctor_457_0` are all standard upstream LLVM options. The NVIDIA delta for LICM is architectural:

1. **Analysis precision**: NVVM AA and enhanced MemorySSA provide better aliasing information, making LICM more aggressive without code changes.
2. **Pipeline orchestration**: Multiple invocations at different pipeline stages with different guard conditions.
3. **Machine-level integration**: `sink-insts-to-avoid-spills` and `enable-mlicm` provide GPU-specific pressure management.
4. **Downstream safety net**: Rematerialization undoes unprofitable hoists after register allocation.

### LICM Disabled for OptiX IR

The `--emit-optix-ir` mode (triggered by OptiX runtime compilation with device type `0xDEED` or `0xABBA`) automatically sets `do-licm=0`, disabling LICM entirely. This suggests that OptiX IR is intended to be consumed by a downstream optimizer (the OptiX JIT compiler) that performs its own code motion decisions, and pre-hoisting at the cicc level would interfere with those decisions.

## Function Map

| Function | Address | Role |
|---|---|---|
| `LICMPass::create` | `sub_195E880` | IR-level LICM factory (AllowSpeculation=false) |
| `LICMPass::create` (alt) | `sub_184CD60` | IR-level LICM factory (identity ambiguous, may be ConstantMerge) |
| LICM knob registration | `ctor_457_0` (`0x544C40`) | 11 `cl::opt` registrations for IR LICM |
| MachineLICM knob registration | `ctor_305` | 4 `cl::opt` registrations for MachineLICM |
| `EarlyMachineLICMPass` | (in codegen pipeline) | Pre-RA machine-level LICM |
| `MachineLICMPass` | (in codegen pipeline) | Post-RA machine-level LICM |
| `LoopSinkPass` | pipeline parser entry 271 | Inverse of LICM hoist -- sinks unprofitable hoists |
| `NVVMLowerBarriers` | `sub_1C98160` | Runs between LICM invocations; lowers barrier intrinsics |
| NVVM AA query | `sub_146F1B0` | Address-space-based NoAlias determination used by MemorySSA |
| MemorySSA clobber walk | `sub_1A6AFB3` | Walker that LICM uses to determine load hoistability |
| Loop-invariant check | `sub_1C51340` | Utility for checking if a value is loop-invariant |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM 20 | cicc v13.0 |
|---|---|---|
| **Pipeline invocations** | Typically one LICM invocation in the function pipeline, plus LoopSink. | 4-6 invocations at different pipeline stages with conditional guards. |
| **Alias analysis precision** | BasicAA + TBAA. Cross-address-space aliasing not exploited (all code shares one address space). | NVVM AA returns `NoAlias` for cross-address-space pairs, dramatically increasing hoistable instruction count. |
| **MemorySSA sparsity** | Dense graphs on flat-memory architectures. | Sparse graphs due to NVVM AA, reducing walker overhead and improving LICM precision. |
| **Register pressure feedback** | MachineLICM has `sink-insts-to-avoid-spills` but no GPU occupancy model. | `sink-insts-to-avoid-spills` interacts with NVPTX's occupancy-based register targets. `enable-mlicm` provides target-level gating. |
| **Speculative hoisting** | Allowed by default on most targets. | Disabled (`AllowSpeculation=false`) because GPU kernels fault on speculative loads from unmapped memory. |
| **OptiX mode** | N/A. | LICM entirely disabled for OptiX IR emission. |
| **Downstream undo** | No systematic mechanism to undo unprofitable hoists. | Rematerialization (`nvvmrematerialize`, `nv-remat-block`) systematically undoes hoists that increase pressure past the occupancy target. |

## Cross-References

- [MemorySSA Builder for GPU](../infra/memoryssa.md) -- how MemorySSA exploits NVVM AA for sparse dependency graphs
- [Alias Analysis & NVVM AA](../infra/alias-analysis.md) -- the cross-address-space NoAlias analysis that enables aggressive hoisting
- [Rematerialization](../passes/rematerialization.md) -- the safety net that undoes unprofitable hoists
- [Sinking2](../passes/sinking2.md) -- NVIDIA's custom sinking pass that complements LICM sink mode
- [LLVM Optimizer](../pipeline/optimizer.md) -- pipeline assembly and two-phase compilation
- [Optimization Levels](../config/optimization-levels.md) -- per-tier pipeline configuration
- [Machine-Level Passes](./machine-passes.md) -- MachineLICM pre-RA and post-RA placement
- [Loop Passes (Standard)](./loop-passes-standard.md) -- LoopRotate, LCSSA, LoopSimplify that canonicalize before LICM
- [Loop Unrolling](./loop-unroll.md) -- runs after LICM in the pipeline; the LoopUnroll pass factory at `sub_19B73C0` was previously mislabeled as LICM
