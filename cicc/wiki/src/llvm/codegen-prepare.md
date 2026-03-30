# CodeGenPrepare and SCEV-CGP

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **LLVM version note:** Upstream CodeGenPrepare is stock LLVM 20.0.0 `CodeGenPrepare.cpp` with all 20+ `cl::opt` knobs unchanged. SCEV-CGP is a fully proprietary NVIDIA pass with no upstream equivalent; it is disabled by default (`nv-disable-scev-cgp = true`).

cicc v13.0 contains two distinct passes that prepare LLVM IR for the NVPTX backend's instruction selection. The first is upstream LLVM's `CodeGenPreparePass`, registered as `"codegenprepare"` in the New PM pipeline (line 216 of `sub_2342890`), which sinks address computations, creates PHI nodes for sunk values, and splits critical edges. The second is NVIDIA's proprietary SCEV-CGP (Scalar-Evolution-based Code Generation Preparation), a fully custom pass that uses SCEV analysis to rewrite address expressions with GPU thread ID as an induction variable.

Both passes operate at the LLVM IR level, immediately before SelectionDAG construction. They share the goal of making address expressions cheap for the backend to lower, but they work at different abstraction levels: CodeGenPrepare operates syntactically on individual memory instructions; SCEV-CGP operates semantically on entire address expression families using scalar evolution. NVIDIA disables SCEV-CGP by default (`nv-disable-scev-cgp` defaults to `true`), relying on upstream CodeGenPrepare plus the downstream [Base Address Strength Reduction](../passes/base-address-sr.md) and [Common Base Elimination](../passes/common-base-elim.md) passes to handle GPU address optimization.

## Key Facts

| Property | Value |
|---|---|
| Pass name (upstream) | `codegenprepare` (New PM) |
| Pass name (NVIDIA) | SCEV-CGP (no formal New PM pass name found in binary) |
| Binary range (v12.x) | `0x1D60000`--`0x1D7FFFF` (helpers + main transforms) |
| Binary range (v13.0) | `0x2D75700`--`0x2D88660` (Cluster 6 in `0x2D` sweep) |
| Address sinking | `sub_1D73760` / `sub_2D75700` (65--72 KB), string `"sunkaddr"` |
| PHI sinking | `sub_1D706F0` / `sub_2D784F0` (64--68 KB), string `"sunk_phi"` |
| Block splitting | `sub_1D7AA30` / `sub_2D88660` (54--74 KB), strings `".unlikely"`, `".cond.split"` |
| Main transform | `sub_2D80050` (54 KB) -- orchestrates address mode lowering |
| SCEV-CGP knob ctor | `ctor_263_0` at `0x4F36F0` (9.9 KB, 44 option strings) |
| CGP knob ctor | `ctor_288_0` at `0x4FA950` (8.6 KB, 44 option strings) |
| Master disable | `nv-disable-scev-cgp` (default: `true` -- SCEV-CGP is disabled) |
| Upstream source | `llvm/lib/CodeGen/CodeGenPrepare.cpp` |
| Pipeline position | Late IR, immediately before SelectionDAG ISel |

## Upstream CodeGenPrepare

### Purpose

CodeGenPrepare is the last IR-level pass before instruction selection. Its job is to transform the IR into a form that the SelectionDAG builder can lower efficiently: address computations should be adjacent to their memory uses (reducing live ranges), complex addressing modes should be materialized as GEP chains that ISel can pattern-match, and unlikely branches should be split into cold blocks so that block placement can isolate them.

On NVPTX this pass is less critical than on x86 because PTX has simpler addressing modes (base + offset, no scaled index), but it still performs three important transforms.

### Transform 1: Address Sinking (`sunkaddr`)

The address sinking logic lives in `sub_1D73760` (v12.x) / `sub_2D75700` (v13.0). It identifies memory instructions whose address operand is computed in a dominating block, then sinks the computation to the block containing the memory instruction. The sunk address is named `"sunkaddr"` in the IR, appearing as a GEP, `inttoptr`, or `bitcast` chain:

```text
Before:
  entry:
    %addr = getelementptr float, ptr %base, i64 %idx
    br label %loop

  loop:
    %val = load float, ptr %addr          ; addr live across loop

After:
  entry:
    br label %loop

  loop:
    %sunkaddr0 = getelementptr float, ptr %base, i64 %idx
    %val = load float, ptr %sunkaddr0     ; addr local to use
```

The naming convention `"sunkaddr"` with a numeric suffix (20+ occurrences in binary string references) is the standard LLVM naming. Each sunk address gets a unique suffix: `sunkaddr0`, `sunkaddr1`, etc.

The sinking decision is controlled by a cache called `ValueToSunkAddr` (a `DenseMap` at `sub_2CE7CF0` in the v13.0 build). Before sinking a value, the pass checks whether the same address expression has already been sunk into the target block. If so, it reuses the existing sunk copy rather than creating a duplicate.

The core sinking algorithm:

```
for each basic block BB in function:
    for each instruction I in BB:
        if I is a memory instruction (load/store/atomic):
            addr = I.getPointerOperand()
            if addr.getParent() != BB:
                // addr defined in a dominating block
                addr_mode = matchAddressMode(addr)           // sub_2D67BB0
                if addr_mode.isFoldable():
                    sunk = materializeAddrMode(addr_mode, BB) // sub_2D68450
                    I.setPointerOperand(sunk)
                    mark changed
```

Key helpers in the v13.0 build:

| Function | Address | Size | Role |
|---|---|---|---|
| -- | -- | `sub_2D749D0` | -- |
| -- | -- | `sub_2D67BB0` | -- |
| -- | -- | `sub_2D6E640` | -- |
| -- | -- | `sub_2D68450` | -- |
| -- | -- | `sub_2CE7CF0` | -- |

### Transform 2: PHI Sinking (`sunk_phi`)

When an address computation has multiple uses in successor blocks of a conditional branch, the pass creates a PHI node in the merge block rather than sinking independent copies into each successor. The resulting PHI is named `"sunk_phi"`:

```text
Before:
  entry:
    %addr = getelementptr float, ptr %base, i64 %idx
    br i1 %cond, label %then, label %else

  then:
    %v1 = load float, ptr %addr
    br label %merge

  else:
    %v2 = load float, ptr %addr
    br label %merge

After (conceptual):
  then:
    %sunkaddr0 = getelementptr float, ptr %base, i64 %idx
    %v1 = load float, ptr %sunkaddr0
    br label %merge

  else:
    %sunkaddr1 = getelementptr float, ptr %base, i64 %idx
    %v2 = load float, ptr %sunkaddr1
    br label %merge
```

When the two sunk copies would be identical and the value is needed in the merge block for other uses, the pass instead creates:

```text
  merge:
    %sunk_phi = phi ptr [ %sunkaddr0, %then ], [ %sunkaddr1, %else ]
```

The PHI creation calls `sub_B44260` (PHI node setup), with naming via `sub_BD6B50`. The `addr-sink-new-phis` cl::opt knob (registered at `ctor_288_0`) controls whether the pass is allowed to create new PHIs during address sinking. The `addr-sink-new-select` knob similarly controls creation of new `select` instructions.

### Transform 3: Block Splitting

`sub_1D7AA30` (v12.x) / `sub_2D88660` (v13.0) splits basic blocks to isolate unlikely paths. The pass creates blocks with suffixes `".unlikely"` and `".cond.split"`, allowing MachineBlockPlacement to push cold code away from the hot path. This is driven by branch probability metadata and profile-guided section prefix hints.

On NVPTX, block splitting interacts with StructurizeCFG: the split blocks must still form reducible control flow, otherwise StructurizeCFG will have to insert additional flow blocks to restore structure. The `profile-guided-section-prefix` knob controls whether section prefix metadata (`.hot`, `.unlikely`, `.unknown`) is attached to split blocks.

## Upstream CodeGenPrepare Knobs

All registered at `ctor_288_0` (`0x4FA950`, 8.6 KB, 44 strings). These are standard LLVM cl::opt knobs, unchanged from upstream:

| Knob | Type | Effect |
|------|------|--------|
| `disable-cgp-branch-opts` | bool | Disable CodeGenPrepare branch optimizations |
| `disable-cgp-gc-opts` | bool | Disable CodeGenPrepare GC optimizations |
| `disable-cgp-select2branch` | bool | Disable select-to-branch conversion |
| `addr-sink-using-gep` | bool | Use GEP instructions for address sinking (vs. inttoptr) |
| `enable-andcmp-sinking` | bool | Sink `and`/`cmp` instruction pairs into branches |
| `disable-cgp-store-extract` | bool | Disable store-extractvalue optimization |
| `stress-cgp-store-extract` | bool | Stress test store-extractvalue path |
| `disable-cgp-ext-ld-promotion` | bool | Disable extension-load promotion |
| `disable-preheader-prot` | bool | Disable loop preheader protection |
| `profile-guided-section-prefix` | bool | Attach section prefix based on profile data |
| `cgp-freq-ratio-to-skip-merge` | int | Block frequency ratio threshold to skip block merging |
| `force-split-store` | bool | Force store splitting |
| `cgp-type-promotion-merge` | bool | Merge type promotions |
| `disable-complex-addr-modes` | bool | Disable complex addressing mode optimization |
| `addr-sink-new-phis` | bool | Allow creating new PHIs during address sinking |
| `addr-sink-new-select` | bool | Allow creating new select during address sinking |
| `addr-sink-combine-base-reg` | bool | Combine base register in address sink |
| `addr-sink-combine-gv` | bool | Combine global value in address sink |
| `addr-sink-combine-offs` | bool | Combine offset in address sink |
| `addr-sink-combine-scaled-reg` | bool | Combine scaled register in address sink |
| `cgp-split-large-offset-gep` | bool | Split GEPs with large offsets |

### GPU Relevance of Upstream Knobs

Most of these knobs are effectively no-ops on NVPTX because the target's addressing modes are simple (base + immediate offset, no scaled index register). However, a few matter:

- **`addr-sink-using-gep`**: Controls whether sunk addresses use GEP or inttoptr chains. On NVPTX, GEP chains are preferred because they preserve address space information through lowering. The inttoptr path strips address space, forcing the backend to re-derive it.

- **`cgp-split-large-offset-gep`**: Relevant for large array accesses where the constant offset exceeds the PTX immediate encoding width (±2^31 for 64-bit addressing). Splitting the GEP allows the backend to use a base register plus a small offset rather than a 64-bit constant.

- **`addr-sink-new-phis`**: On GPU, creating new PHIs can increase divergent live ranges. If the condition driving the PHI is thread-divergent, the PHI result will be divergent, potentially requiring a wider (per-lane) register allocation.

## NVIDIA SCEV-CGP

### What Is It?

SCEV-CGP is a fully custom NVIDIA pass that uses LLVM's ScalarEvolution analysis to optimize address mode expressions at the function level, with specific awareness of GPU thread ID as an induction variable. Where upstream CodeGenPrepare operates syntactically (pattern-matching individual instructions), SCEV-CGP operates semantically: it analyzes address expressions as SCEV recurrences, factors out common base computations, and rewrites them to minimize register pressure.

The pass is registered in `ctor_263_0` at `0x4F36F0` alongside [Base Address Strength Reduction](../passes/base-address-sr.md) knobs. The 44 strings registered in this single constructor cover both SCEV-CGP and BASR, confirming they are part of the same address optimization subsystem.

### Why NVIDIA Disables It By Default

The `nv-disable-scev-cgp` knob defaults to `true` (the description reads `"Disable optimize addr mode with SCEV pass"` and the raw data at `ctor_609_0` marks it as `def=on` meaning disabled). This is a deliberate choice:

1. **Redundancy with BASR/CBE.** NVIDIA has invested heavily in [Base Address Strength Reduction](../passes/base-address-sr.md) (62 KB) and [Common Base Elimination](../passes/common-base-elim.md) (39 KB), which handle the most profitable GPU address optimizations (sharing base computations across array accesses in loop bodies). These passes are simpler, more predictable, and better-tested than the general SCEV-CGP framework.

2. **Interaction with LSR.** Both SCEV-CGP and [Loop Strength Reduction](./lsr.md) operate on SCEV expressions. If both are active, they can fight over the same address expressions: LSR rewrites IVs for loop-carried efficiency, then SCEV-CGP undoes part of that work to optimize address modes. The result can be worse than either pass alone. By disabling SCEV-CGP, NVIDIA lets LSR (with its full GPU-aware formula solver) handle SCEV-based address optimization without interference.

3. **Compile-time cost.** SCEV-CGP with aggressive mode (`do-scev-cgp-aggresively` [sic]) is expensive. The `scev-cgp-inst-limit` and `scev-cgp-control` knobs exist precisely because uncontrolled SCEV-CGP can balloon compile times on large kernels with many address expressions.

4. **Overflow hazards.** The `ignore-32-bit-overflow` and `ignore-signed-32-bit-overflow` knobs in `ctor_263_0` indicate that SCEV-CGP can produce address arithmetic that overflows 32-bit intermediates. On GPU where 32-bit addressing is common (shared memory, constant memory), this is a correctness risk that NVIDIA mitigates by keeping the pass off by default.

### When SCEV-CGP Would Be Beneficial

Despite being disabled by default, the pass has 11 dedicated knobs -- NVIDIA clearly uses it selectively:

- **Kernels with complex strided access patterns** where thread ID participates in multi-dimensional address calculations (e.g., `base + tid.x * stride_x + tid.y * stride_y + tid.z * stride_z`). BASR handles the case where multiple accesses share a base, but it does not factor thread ID expressions across dimensions.

- **Register-pressure-critical kernels at occupancy cliffs** where SCEV-based address strength reduction can save enough registers to cross an occupancy boundary. The `scev-cgp-tid-max-value` knob lets the pass reason about the bounded range of thread IDs, enabling tighter value range analysis.

- **Function-level address optimization** (enabled by `do-function-scev-cgp`) where cross-loop base sharing matters more than per-loop IV optimization.

### Thread ID Max Value Knob

The `scev-cgp-tid-max-value` knob deserves special attention. It provides SCEV analysis with the maximum possible value of a GPU thread ID, which is architecture-dependent:

- **threadIdx.x**: max 1024 (all architectures sm_70+)
- **threadIdx.y**: max 1024
- **threadIdx.z**: max 64
- **blockIdx.x**: max 2^31 - 1

By telling SCEV that `threadIdx.x` is bounded by 1024, the analysis can prove that `threadIdx.x * element_size` fits in 32 bits for element sizes up to ~2 million bytes. This enables 32-bit address arithmetic where the expression would otherwise be widened to 64 bits. The knob links to the Known Bits analysis documented in [Known Bits](./known-bits.md), where the `nvvm-intr-range` pass provides similar bounded-range information for special registers.

### SCEV-CGP Knobs (Complete Reference)

All registered in `ctor_263_0` at `0x4F36F0`. These are NVVMPassOptions values, stored in the 222-slot pass option registry.

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `do-scev-cgp` | bool | true `[MEDIUM confidence]` | Master enable for SCEV-based CodeGenPrepare transforms. Default inferred from the fact that `nv-disable-scev-cgp` exists as an override, implying this defaults to enabled. |
| `do-scev-cgp-aggresively` [sic] | bool | false `[MEDIUM confidence]` | Enable aggressive SCEV-CGP mode with expanded search. Default inferred from naming convention (aggressive modes typically off by default). |
| `do-function-scev-cgp` | bool | false `[MEDIUM confidence]` | Enable function-level (cross-loop) SCEV-CGP. Default inferred from naming convention. |
| `nv-disable-scev-cgp` | bool | **true** | Master disable switch in NVPTX backend (overrides `do-scev-cgp`) |
| `scev-cgp-control` | int | unknown | Limit the total number of SCEV-CGP transformations per function |
| `scev-cgp-cross-block-limit` | int | unknown | Max number of common base expressions from a single block |
| `scev-cgp-idom-level-limit` | int | unknown | Max dominator tree depth for hoisting base computations |
| `scev-cgp-inst-limit` | int | unknown | Max instructions analyzed per parameter expression |
| `scev-cgp-old-base` | bool | unknown | Use old (legacy) base computation method instead of new |
| `scev-cgp-tid-max-value` | int | arch-dependent | Maximum value of thread ID for address range analysis |
| `scev-cgp-check-latency` | int | unknown | Latency threshold for address computation profitability |
| `scev-cgp-norm` | int | unknown | Normalization control for SCEV expression canonicalization |
| `print-after-scev-cgp` | bool | false | Dump function IR after SCEV-CGP completes |
| `dump-scev-cgp` | bool | false | Debug dump during SCEV-CGP execution |

### Additional ctor_263_0 Knobs (BASR/CBE Related)

The same constructor also registers these knobs, documented in their respective pages:

| Knob | See |
|------|-----|
| `do-base-address-strength-reduce` | [Base Address Strength Reduction](../passes/base-address-sr.md) |
| `do-base-address-strength-reduce-chain` | [Base Address Strength Reduction](../passes/base-address-sr.md) |
| `base-address-strength-reduce-iv-limit` | [Base Address Strength Reduction](../passes/base-address-sr.md) |
| `base-address-strength-reduce-max-iv` | [Base Address Strength Reduction](../passes/base-address-sr.md) |
| `topo-sort-begin` | Topological sort starting point for address expression graph |
| `ignore-bad-base` | Bypass validity checks on base pointer classification |
| `ignore-32-bit-overflow` | Skip 32-bit overflow checks in address arithmetic |
| `ignore-signed-32-bit-overflow` | Skip signed 32-bit overflow checks |

## Interaction with LSR

CodeGenPrepare/SCEV-CGP and [Loop Strength Reduction](./lsr.md) both optimize address expressions, but at different pipeline stages and granularities.

| Aspect | LSR | CodeGenPrepare | SCEV-CGP |
|--------|-----|----------------|----------|
| **Pipeline position** | Late IR optimization (loop passes) | Pre-ISel (after all IR opts) | Pre-ISel (NVIDIA custom position) |
| **Scope** | Per-loop IV rewriting | Per-instruction address sinking | Per-function address expression rewriting |
| **SCEV usage** | Full: formula generation, stride factoring, chain construction | None (syntactic pattern matching) | Full: base decomposition, range analysis |
| **Register pressure** | Explicit RP tracking with occupancy ceiling | Implicit (sinking reduces live ranges) | Implicit via `scev-cgp-cross-block-limit` |
| **Address space** | Full awareness (shared memory protection, 64-bit IV gating) | No special GPU handling | Thread ID aware (`scev-cgp-tid-max-value`) |
| **Default status** | **Enabled** (with GPU-custom formula solver) | **Enabled** (standard upstream) | **Disabled** (`nv-disable-scev-cgp = true`) |

The key insight is the pipeline ordering: LSR runs first during the optimization phase, rewriting IVs across the loop. CodeGenPrepare runs later, sinking the results into individual use sites. If SCEV-CGP were also enabled, it would run between these two, potentially undoing LSR's IV choices to create "better" address modes -- which may conflict with LSR's register-pressure-informed formula selection.

NVIDIA's solution is pragmatic: keep SCEV-CGP off, let LSR handle SCEV-level optimization, let BASR/CBE handle GPU-specific base sharing, and let upstream CodeGenPrepare handle the final address sinking.

## Differences from Upstream LLVM

| Area | Upstream LLVM | cicc v13.0 |
|------|---------------|------------|
| **CodeGenPrepare pass** | Standard, used as-is | Retained unchanged from LLVM 20.0.0 |
| **SCEV-CGP** | Does not exist | NVIDIA proprietary, disabled by default |
| **Address sinking** | Always uses `TTI::getAddrModeType` | Same, but NVPTX TTI returns simple modes (base+offset only) |
| **Block splitting** | Hot/cold based on PGO | Same, but must preserve reducibility for StructurizeCFG |
| **BASR/CBE** | Do not exist | NVIDIA proprietary alternatives to SCEV-CGP for GPU |
| **Knob count** | ~20 cl::opt for CGP | 20 upstream CGP + 14 SCEV-CGP + 8 BASR = 42 total |

## Function Map

### CodeGenPrepare (v12.x Addresses)

| Function | Address | Size | Role |
|---|---|---|---|
| -- | `sub_1D73760` | 65 KB | `optimizeMemoryInst` -- address sinking, creates `"sunkaddr"` |
| -- | `sub_1D706F0` | 68 KB | PHI optimization, creates `"sunk_phi"` |
| -- | `sub_1D7AA30` | 74 KB | Block splitting, creates `".unlikely"`, `".cond.split"` |
| -- | `sub_1D779D0` | 71 KB | IR transform (DAG combine-level, possibly `optimizeInst`) |
| -- | `sub_1D765D0` | 34 KB | Select lowering (`"cond.false"`, `"cond.end"`) |
| -- | `sub_1D7F9D0` | 31 KB | Deque-based worklist processor |

### CodeGenPrepare (v13.0 Addresses)

| Function | Address | Size | Role |
|---|---|---|---|
| -- | `sub_2D75700` | 72 KB | Address sinking with `"sunk_phi"`, ValueToSunkAddr DenseMap |
| -- | `sub_2D784F0` | 64 KB | Address mode lowering orchestrator, calls `sub_2D75700` |
| -- | `sub_2D80050` | 54 KB | Main CodeGenPrepare transform, calls TTI and address mode logic |
| -- | `sub_2D82850` | 62 KB | Late lowering/expansion (type widening, custom lowering) |
| -- | `sub_2D88660` | 70 KB | Block splitting with branch weights (`"hot"`, `"unlikely"`, `"unknown"`) |
| -- | `sub_2D749D0` | -- | Address mode cache lookup |
| -- | `sub_2D67BB0` | -- | Address mode legality test |
| -- | `sub_2D6E640` | -- | Address mode cache insert |
| -- | `sub_2D68450` | -- | Address mode materialization |
| -- | `sub_2D6DEE0` | -- | Address mode matching |
| -- | `sub_2D69E90` | -- | Cleanup/init |

### Helper Range (0x1D60000--0x1D6FFFF)

This 64 KB sub-range contains CodeGenPrepare helper functions. The sweep identifies it as `"CodeGenPrepare helpers"` but no individual functions are called out with string evidence. These likely include address mode computation utilities, operand analysis, and GEP canonicalization.

### SCEV-CGP Option Registration

| Function | Address | Size | Role |
|---|---|---|---|
| -- | `ctor_263_0` (`0x4F36F0`) | 9.9 KB | Registers 44 cl::opt strings for SCEV-CGP + BASR |
| -- | `ctor_288_0` (`0x4FA950`) | 8.6 KB | Registers 44 cl::opt strings for upstream CodeGenPrepare |
| -- | `ctor_591` (`0x57C1A0`) | 9.3 KB | Additional CodeGenPrepare sink/split options |
| -- | `ctor_544_0` (`0x56C190`) | 13.1 KB | CodeGenPrepare options (v13.0 duplicate registration) |
| -- | `ctor_609_0` (`0x585D30`) | 37.3 KB | NVPTX backend mega-block, includes `nv-disable-scev-cgp` |

## Cross-References

- [Loop Strength Reduction](./lsr.md) -- SCEV-based IV rewriting, runs before CGP
- [Base Address Strength Reduction](../passes/base-address-sr.md) -- NVIDIA's preferred GPU address optimization
- [Common Base Elimination](../passes/common-base-elim.md) -- inter-block complement to BASR
- [SCEV Analysis](./scev.md) -- the scalar evolution infrastructure both LSR and SCEV-CGP depend on
- [Known Bits](./known-bits.md) -- thread ID range analysis that `scev-cgp-tid-max-value` feeds into
- [Code Generation Overview](../pipeline/codegen.md) -- pipeline position context
- [NVPTX Target & TTI](../infra/nvptx-target.md) -- the `nv-disable-scev-cgp` registration in `ctor_609_0`
- [Optimizer Pipeline](../pipeline/optimizer.md) -- `do-scev-cgp` in the NVVMPassOptions system
