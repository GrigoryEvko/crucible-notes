# Configuration Knobs

Three independent knob systems control compiler behavior: LLVM `cl::opt` flags (~1,496 unique), NVVMPassOptions (222 slots), and NVIDIA codegen knobs (~70).

| | |
|---|---|
| **LLVM cl::opt** | 1,496 unique flags across 353 constructor files |
| **NVVMPassOptions** | 222 slots, initialized by `sub_12D6300` (125KB) |
| **Codegen knobs** | ~70, parsed by `sub_1C20170` / `sub_CD9990` from NVVM container |
| **BSS storage** | `0x4F7FEA0`–`0x4FA5xxx` (cl::opt), `a1+0`–`a1+4464` (PassOptions) |
| **Dual PM** | Same options registered for both Legacy PM (`sub_C53080`) and New PM (`sub_16B8280`) |
| **NVIDIA-specific** | 172 of 1,496 cl::opt flags (11.5%) are NVIDIA-added |

## Knob System 1: LLVM cl::opt

### Registration Pattern

Every `cl::opt` follows this initialization sequence in a global constructor:

```c
// Legacy PM path
InterlockedExchangeAdd64(sub_C523C0(), 1);   // atomic option counter
sub_C53080(&option, "option-name", strlen);   // set name
sub_C53130(&option);                          // finalize registration
__cxa_atexit(destructor, &option, &dso_handle);

// New PM path (parallel registration)
InterlockedExchangeAdd64(&unk_4FA0230, 1);
sub_16B8280(&option, "option-name", strlen);
sub_16B88A0(&option);
__cxa_atexit(destructor, &option, &dso_handle);
```

Each `cl::opt<T>` occupies ~224 bytes (0xE0) in BSS. Top constructors by option count: `ctor_600` (30), `ctor_433` (25), `ctor_472` (24), `ctor_609` (22), `ctor_392` (22).

### Category 1: Scalar Optimization (InstCombine + FP)

Constructor: `ctor_165_0` at `0x4D0500` (11,731 bytes). Registers 12 NVIDIA-specific flags plus 4 standard LLVM flags.

| Flag | Type | Default | BSS Addr | Purpose |
|---|---|---|---|---|
| `split-gep-chain` | bool | false | `0x4F901A8` | Split GEP chains to independent GEPs for better address mode selection |
| `Disable-Add-to-Or` | bool | true | — | Disable add-to-or transformation (NVIDIA blocks this LLVM combine) |
| `opt-use-fast-math` | bool | false | — | Enable aggressive FP simplification (set by `-unsafe-math` / `-fast-math`) |
| `opt-use-prec-div` | bool | true | — | Use precise division (set by `-prec-div=1`; cleared by `-prec-div=0`) |
| `opt-no-signed-zeros` | bool | false | — | Ignore signed zero distinction (set by `-no-signed-zeros`) |
| `disable-fp-cast-opt` | bool | false | — | Disable FP-to-int and int-to-FP cast optimizations |
| `reorder-sext-before-cnst-add` | bool | false | — | `sext(add(a,CI))` to `add(sext(a),CI)` rewrite; **hidden** flag |
| `disable-sink` | bool | false | — | Disable instruction sinking in InstCombine |
| `partial-sink` | bool | false | — | Enable partial sinking of instructions |
| `nvptx-rsqrt-approx-opt` | bool | false | — | Enable reciprocal sqrt approximation optimization |
| `disable-rsqrt-opt` | bool | false | — | Disable reciprocal sqrt optimization entirely |
| `check-vn` | bool | false | — | Verify value numbers after transformations (debug) |

Standard LLVM flags in same constructor: `expensive-combines` (bool), `instcombine-maxarray-size` (int, default 1024), `instcombine-visit` (int), `instcombine-lower-dbg-declare` (bool).

### Category 2: Inliner Heuristics

Constructor: `ctor_186_0` at `0x4DBEC0` (14,109 bytes). Nine NVIDIA-specific flags governing the custom CGSCC inliner at `sub_1864060`.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `profuseinline` | bool | false | Verbose inlining diagnostics (NVIDIA profuse framework, **not** PGO profuse) |
| `inline-total-budget` | int | none | Global total budget across all callers; unset = unlimited |
| `nv-inline-all` | bool | false | Force inline ALL function calls (used by OptiX ray tracing) |
| `inline-budget` | int | 20000 | Per-caller inlining cost budget; `-aggressive-inline` sets to 40000 |
| `inline-adj-budget1` | int | none | Secondary adjusted per-caller budget |
| `inline-switchctrl` | int | none | Tune heuristic for switch-containing callees |
| `inline-numswitchfunc` | int | none | Threshold for switch-heavy function penalty |
| `inline-maxswitchcases` | int | none | Max switch cases before inlining penalty kicks in |
| `disable-inlined-alloca-merging` | bool | false | Disable post-inline alloca merging into single frame slot |

"none" means the knob is unset by default and the heuristic falls back to internal logic.

### Category 3: GVN (Global Value Numbering)

Constructor: `ctor_201` at `0x4E0990`. Eleven knobs (8 NVIDIA-specific + 3 upstream).

| Flag | Type | Default | BSS Addr | Purpose |
|---|---|---|---|---|
| `profusegvn` | bool | true | `0x4FAE7E0` | Verbose GVN diagnostics (unusually, defaults **on**) |
| `gvn-dom-cache` | bool | true | `0x4FAE700` | Cache dominator tree nodes; cache size = 32 |
| `max-recurse-depth` | int | 1000 | `0x4FAE620` | Max recursion during value numbering (safety valve for template-heavy code) |
| `enable-phi-remove` | int | 2 | `0x4FAEC40` | PHI removal aggressiveness: 0=off, 1=trivial only, **2**=post-leader substitution |
| `dump-phi-remove` | int | 0 | `0x4FAEB60` | Dump PHI removal decisions (debug) |
| `no-split-stores-below` | int | -1 | `0x4FAEA80` | Min store width for splitting (bits); -1 = no limit |
| `no-split-stores-above` | int | -1 | `0x4FAE9A0` | Max store width for splitting (bits); -1 = no limit |
| `split-stores` | bool | true | `0x4FAE8C0` | Master enable for NVIDIA store-splitting in GVN |
| `enable-pre` | bool | true | `0x4FAEEE0` | Enable Partial Redundancy Elimination (upstream LLVM) |
| `enable-load-pre` | bool | true | `0x4FAEE00` | Enable load PRE across edges (upstream LLVM) |
| `enable-split-backedge-in-load-pre` | bool | false | `0x4FAED20` | Allow backedge splitting during load PRE (upstream LLVM) |

Store splitting uses a custom NVIDIA registrar (`sub_190BE40`) that takes a default-value pointer. Both limit knobs default to -1 = all sizes eligible.

### Category 4: Loop Strength Reduction

Constructor: `ctor_214_0` at `0x4E4B00`. Eleven NVIDIA-specific LSR flags (69% NVIDIA customization rate).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `disable-unknown-trip-lsr` | bool | false | Disable LSR for loops with unknown trip count |
| `lsr-check-rp` | bool | false | Check register pressure before applying LSR |
| `lsr-rp-limit` | int | none | Skip LSR entirely when RP exceeds this limit |
| `filter-bad-formula` | bool | false | Filter out poor-quality LSR formulae early |
| `do-lsr-64-bit` | bool | false | Enable 64-bit loop strength reduction |
| `count-sxt-opt-for-reg-pressure` | bool | false | Factor sign-extension elimination savings into RP analysis |
| `lsr-sxtopt` | bool | false | Perform sign-extension elimination within LSR |
| `lsr-loop-level` | int | none | Apply LSR only at specific loop nesting level |
| `lsr-skip-outer-loop` | bool | false | Ignore outer-loop induction variables in LSR |
| `disable-lsr-for-sharedmem32-ptr` | bool | false | Disable LSR for 32-bit shared memory pointers (GPU-specific) |
| `disable-lsr-complexity-discount` | bool | false | Disable complexity estimation discount heuristic |

Standard LLVM LSR flags in same constructor: `enable-lsr-phielim`, `lsr-insns-cost`, `lsr-exp-narrow`, `lsr-filter-same-scaled-reg`, `lsr-fix-iv-inc`.

### Category 5: IndVarSimplify

Constructor: `ctor_203_0` at `0x4E1CD0` (7,007 bytes).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `Disable-unknown-trip-iv` | bool | false | Disable IV substitution for unknown-trip-count loops |
| `iv-loop-level` | int | none | Control which loop nesting levels get IV substitution |

### Category 6: SimplifyCFG

Constructor: `ctor_243_0` at `0x4ED0C0`.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `disable-jump-threading` | bool | false | Disable jump threading (for OCG experiments) |
| `fold-with-var-cond` | bool | false | Fold branches with variance-based conditions |

### Category 7: NVPTX Backend Math/Scheduling

Constructor: `ctor_607` at `0x584B60` (13,700 bytes). Core numeric precision and FMA controls. Defaults are set by the CLI flag routing in `sub_9624D0`, not by the cl::opt constructors.

| Flag | Type | CLI Default | Purpose |
|---|---|---|---|
| `nvptx-sched4reg` | bool | false | Schedule for register pressure (key NVPTX strategy) |
| `nvptx-fma-level` | int | 1 | FMA contraction: 0=off, **1**=on, 2=aggressive. CLI `-fma=1` is default |
| `nvptx-prec-divf32` | int | 1 | F32 div precision: 0=approx, **1**=full, 2=IEEE rnd+ftz, 3=IEEE no-ftz |
| `nvptx-prec-sqrtf32` | int | 1 | Sqrt precision: 0=approx, **1**=rn. CLI `-prec-sqrt=1` is default |
| `nvptx-approx-log2f32` | bool | false | Use `lg2.approx` for log2 (only set by `-unsafe-math`) |
| `nvptx-force-min-byval-param-align` | bool | false | Force 4-byte minimum alignment for byval parameters |
| `nvptx-normalize-select` | bool | false | Override `shouldNormalizeToSelectSequence` in TLI |
| `enable-bfi64` | bool | false | Enable 64-bit BFI (bit-field insert) instructions |

**Note**: These cl::opt knobs have no explicit default in their constructor (they init to 0/false). The effective defaults come from the CLI flag catalog: `-fma=1` routes `-nvptx-fma-level=1`, `-prec-div=1` routes `-nvptx-prec-divf32=1`, `-prec-sqrt=1` routes `-nvptx-prec-sqrtf32=1`.

### Category 8: NVPTX Backend Passes/Features

Constructor: `ctor_609_0` at `0x585D30` (22 options total, largest NVPTX constructor).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `disable-nvptx-load-store-vectorizer` | bool | false | Disable load/store vectorizer |
| `disable-nvptx-require-structured-cfg` | bool | false | Turn off structured CFG requirement (transitional) |
| `nvptx-short-ptr` | bool | false | 32-bit pointers for const/local/shared address spaces |
| `nvptx-enable-machine-sink` | bool | false | Enable machine-level instruction sinking |
| `enable-new-nvvm-remat` | bool | true | Enable new NVVM rematerialization engine |
| `nv-disable-remat` | bool | false | Disable all rematerialization passes |
| `nv-disable-mem2reg` | bool | false | Disable machine-IR mem2reg promotion |
| `nv-disable-scev-cgp` | bool | true | Disable SCEV-based address mode optimization (on = disabled) |
| `nvptx-32-bit-smem` | bool | false | Use 32-bit pointers for shared address space |
| `nvptx-exit-on-unreachable` | bool | true | Lower `unreachable` as PTX `exit` instruction |
| `nvptx-early-byval-copy` | bool | false | Create copy of byval function args in entry block |
| `enable-nvvm-peephole` | bool | true | Enable NVVM peephole optimizer |
| `no-reg-target-nvptxremat` | bool | false | Only run old remat on kernels without register targets |
| `lower-func-args` | bool | true | Lower large aggregate function parameters to copies |
| `enable-sink` | bool | true | Enable LLVM sinking pass |
| `disable-post-opt` | bool | false | Disable IR optimizations in post-opt phase |
| `usedessa` | int | 2 | deSSA method: 0=off, 1=basic, **2**=full |
| `ldg` | bool | true | Load-via-texture (ld.global.nc) constant transform |

### Category 9: NVPTX Backend Extended

Constructor: `ctor_610` at `0x5888A0` (7,400 bytes).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `unroll-assumed-size` | int | 4 | Assumed element count for unknown-size local arrays during unroll analysis |
| `enable-loop-peeling` | bool | false | Enable loop peeling transformation |
| `enable-256-bit-load-store` | bool | false | Enable 256-bit (32-byte) vector load/store generation |
| `ias-param-always-point-to-global` | bool | false | Assume function parameter pointers always point to global memory |
| `ias-strong-global-assumptions` | bool | false | Stronger assumption: constant-buffer pointers resolve to globals |
| `ias-wmma-memory-space-opt` | bool | false | Enable MemorySpaceOpt specialization for WMMA/tensor operations |

### Category 10: Memory Space Optimization

Scattered across `ctor_267`, `ctor_528`, `ctor_531`. Twenty NVIDIA-specific knobs for the address space inference engine. See [IPMSP](../passes/ipmsp.md) for the full algorithm.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `mem-space-alg` | int | — | Switch between MSO algorithm variants |
| `dump-ir-before-memory-space-opt` | bool | false | Dump IR before MSO |
| `dump-ir-after-memory-space-opt` | bool | false | Dump IR after MSO |
| `track-indir-load` | bool | false | Track indirect loads during MSO dataflow |
| `track-int2ptr` | bool | false | Track IntToPtr casts in MSO |
| `param-always-point-to-global` | bool | false | Parameter pointers always point to global memory |
| `devicefn-param-always-local` | bool | false | Treat parameter space as local in device functions |
| `ignore-address-space-check` | bool | false | Ignore address-space checks during branch distribution |
| `sink-into-texture` | int | 0 | Sink loads into texture blocks: 1=cross-block, 2=+intra, 3=+consolidate |
| `ldg` | bool | true | Load Global Constant Transform (ld.global.nc) |
| `do-clone-for-ip-msp` | int | — | Control function cloning limit for IP memory space propagation |
| `dump-ip-msp` | bool | false | Dump interprocedural MSP info |
| `lower-read-only-devicefn-byval` | bool | false | Handle byval attribute of args to read-only device functions |
| `reuse-lmem-very-long-live-range` | int | — | Threshold for very-long live range in local memory reuse |
| `hoist-load-param` | bool | false | Generate all `ld.param` in entry basic block |
| `sink-ld-param` | bool | false | Sink one-use `ld.param` to use point |

### Category 11: Rematerialization

Scattered across `ctor_609_0`, `ctor_362`, and others. Twenty-two knobs for NVVM's multi-pass remat engine.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `nv-disable-remat` | bool | false | Disable all remat passes |
| `enable-new-nvvm-remat` | bool | true | Enable new NVVM remat engine (disables old) |
| `no-reg-target-nvptxremat` | bool | false | Only old remat for kernels without register targets |
| `fp-remat` | bool | false | Allow rematerializing floating-point instructions |
| `high-cost-remat` | bool | false | Allow rematerializing high-cost instructions |
| `cost-threshold-remat` | int | — | Cost threshold per remat action |
| `block-freq-cap-remat` | int | — | Maximum raw block frequency value |
| `block-freq-norm-range-remat` | int | — | Normalization range for block frequency in remat cost |
| `collect-candidate-scale-remat` | int | — | Scaling ratio for high-RP candidate collection |
| `incremental-update-remat` | bool | false | Incrementally update RP analysis after each remat |
| `verify-update-remat` | bool | false | Debug: verify incremental update vs full analysis |
| `print-verify-remat` | bool | false | Debug: print problematic RP on verification failure |
| `rp-remat` | int | — | Debug: set a target register pressure number |
| `late-remat-update-threshold` | int | — | Threshold for copy with many other copy uses |
| `remat-load-param` | bool | false | Support rematerializing constant `ld.param` not in NVVM IR |

### Category 12: SCEV-CGP (Address Mode Optimization)

Eleven NVIDIA-specific knobs for SCEV-based CodeGenPrepare. See [CodeGenPrepare](../llvm/codegen-prepare.md).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `do-scev-cgp` | bool | false | Enable SCEV-based CodeGenPrepare |
| `do-scev-cgp-aggresively` | bool | false | Aggressive SCEV-CGP mode [sic] |
| `do-function-scev-cgp` | bool | false | Function-level SCEV-CGP |
| `nv-disable-scev-cgp` | bool | true | Disable SCEV address mode optimization (master kill switch, **on** by default) |
| `scev-cgp-control` | int | — | Control max transformations applied |
| `scev-cgp-cross-block-limit` | int | — | Max common-base expressions from a single block |
| `scev-cgp-idom-level-limit` | int | — | Max dominator tree levels to walk |
| `scev-cgp-inst-limit` | int | — | Max instructions for a single parameter |
| `scev-cgp-old-base` | bool | false | Force SCEV-CGP to create new base (vs reusing old) |
| `scev-cgp-tid-max-value` | int | — | Max value of thread ID in SCEV expressions |
| `print-after-scev-cgp` | bool | false | Print function after SCEV-CGP phase |

### Category 13: Branch Distribution

Seven NVIDIA-specific flags.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `branch-dist-block-limit` | int | — | Max blocks to apply branch distribution |
| `branch-dist-func-limit` | int | — | Max functions to apply branch distribution |
| `branch-dist-norm` | int | — | Normalization control |
| `no-branch-dist` | string | — | Comma-separated list of functions to skip |
| `disable-complex-branch-dist` | bool | false | Disable complex branch distribution |
| `dump-branch-dist` | bool | false | Dump branch distribution info |

### Category 14: Sinking / Code Motion

Ten knobs across multiple constructors.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `dump-sink2` | bool | false | Debug dump for Sinking2 pass |
| `enable-andcmp-sinking` | bool | false | Sink and/cmp sequences into branches |
| `aggressive-no-sink` | bool | false | Sink all generated instructions |
| `max-uses-for-sinking` | int | — | Don't sink instructions with too many uses |
| `rp-aware-sink` | bool | false | Consider register pressure impact when sinking |
| `instcombine-code-sinking` | bool | false | Enable code sinking within InstCombine |
| `hoist-const-stores` | bool | false | Hoist loop-invariant stores |

### Category 15: Register Pressure / Allocation

Ten NVIDIA-specific knobs.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `maxreg` | int | none | Maximum register count (`--maxrregcount` equivalent) |
| `register-usage-level` | int | — | Register usage level control |
| `cta-reconfig-aware-mrpa` | bool | false | CTA reconfiguration-aware machine RP analysis |
| `cta-reconfig-aware-rpa` | bool | false | CTA reconfiguration-aware RP analysis |
| `pred-aware-mcse` | bool | false | Predicate-aware MachineCSE |
| `rp-aware-mcse` | bool | false | Register-pressure-aware MachineCSE |
| `verify-update-mcse` | bool | false | Debug: verify incremental RP update in MachineCSE |
| `pred-target-adjust` | int | 0 | Predicate register target adjustment (-10 to +10) |
| `donot-insert-dup-copies` | bool | false | Skip duplicate copies to predecessor basic block |
| `nv-disable-mem2reg` | bool | false | Disable machine-level mem2reg |

### Category 16: Restrict / Aliasing

Five NVIDIA-specific flags. See [Alias Analysis](../infra/alias-analysis.md).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `process-restrict` | bool | false | Process `__restrict__` keyword for alias analysis |
| `allow-restrict-in-struct` | bool | false | Allow `__restrict__` inside struct members |
| `apply-multi-level-restrict` | bool | false | Apply restrict to all pointer levels |
| `dump-process-restrict` | bool | false | Debug dump during restrict processing |
| `strict-aliasing` | bool | false | Datatype-based strict aliasing |

### Category 17: CSSA / deSSA

Four knobs. See [CSSA](../passes/cssa.md).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `cssa-coalesce` | int | — | Control PHI operand coalescing strategy |
| `cssa-verbosity` | int | 0 | Verbosity level |
| `dump-before-cssa` | bool | false | Dump specific PHI operands being coalesced |
| `usedessa` | int | 2 | deSSA method: 0=off, 1=basic, **2**=full |

### Category 18: Loop / Unrolling

Eight NVIDIA-specific knobs (beyond the 20+ standard LLVM loop-unrolling flags).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `nv-disable-loop-unrolling` | bool | false | Disable loop unrolling in all passes |
| `aggressive-runtime-unrolling` | bool | false | OCG-style unrolling heuristics |
| `aggressive-runtime-unrolling-fixed-factor` | int | — | Force fixed unroll factor |
| `aggressive-runtime-unrolling-max-factor` | int | — | Maximum unroll factor |
| `aggressive-runtime-unrolling-max-filler-instructions-per-batch` | int | — | Max filler instructions |
| `unroll-runtime-nv-expensive` | bool | false | NVIDIA heuristics for expensive loops |
| `unroll-runtime-convergent` | bool | false | Allow unrolling with `convergent` instructions |
| `track-trip-count-more` | bool | false | Track loop trip count more aggressively |

### Category 19: GEP / Address Strength Reduction

Eight NVIDIA-specific knobs. See [Base Address Strength Reduction](../passes/base-address-sr.md).

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `normalize-gep` | bool | false | Normalize 64-bit GEP subscripts |
| `dump-normalize-gep` | bool | false | Debug dump for GEP normalization |
| `do-base-address-strength-reduce` | int | 0 | Two levels: 1=unconditional, 2=with conditions |
| `dump-base-address-strength-reduce` | bool | false | Debug dump |
| `do-lsr-64-bit` | bool | false | Loop strength reduction for 64-bit (shared with LSR) |
| `do-sign-ext-expand` | bool | false | Expand sign-extension during SCEV build |
| `balance-dot-chain` | bool | false | Balance chain of dot operations |
| `special-reassociate-for-threadid` | bool | false | Don't move back expressions containing thread ID |

### Category 20: Aggregate / Byval Lowering

Ten knobs.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `aggressive-max-aggr-lower-size` | int | — | Threshold size for lowering aggregates |
| `aggressive-lsv` | bool | false | Merge smaller dtypes in aggregate before vectorization |
| `vect-split-aggr` | bool | false | Split aggregates before vectorization |
| `lower-aggr-unrolled-stores-limit` | int | — | Limit stores in unrolled aggregate lowering |
| `large-aggr-store-limit` | int | — | Create loops for aggregate store exceeding limit |
| `lower-func-args` | bool | true | Lower large aggregate function parameters |
| `lsa-opt` | bool | false | Optimize copying of struct args to local memory |
| `skiploweraggcopysafechk` | bool | false | Skip safety check in `loweraggcopy` |
| `memdep-cache-byval-loads` | bool | true | Preprocess byval loads to reduce compile time |
| `ldstmemcpy-glue-max` | int | — | Limit for gluing ld/st of memcpy |

### Category 21: Normalization / Canonicalization

Four knobs.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `norm-fold-all` | bool | false | Fold all regular instructions |
| `norm-preserve-order` | bool | false | Preserve original instruction order |
| `norm-rename-all` | bool | false | Rename all instructions |
| `norm-reorder-operands` | bool | false | Sort/reorder operands in commutative operations |

### Category 22: NVVM Infrastructure

Five knobs.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `nvvm-lower-printf` | bool | false | Enable printf lowering |
| `nvvm-reflect-enable` | bool | true | NVVM reflection (reads `__CUDA_FTZ`, `__CUDA_PREC_DIV`, etc.) |
| `nvvm-verify-show-info` | bool | false | Info messages during NVVM verification |
| `enable-nvvm-peephole` | bool | true | NVVM peephole optimizer |
| `nv-ocl` | bool | false | Deprecated OpenCL compatibility flag |

### Category 23: Compilation Control

Constructor: `ctor_043_0` at `0x48D7F0` + `ctor_028_0` at `0x489160`.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `debug-compile` | bool | false | Compile for debugging (set by `-g`) |
| `generate-line-info` | bool | false | Emit line info even without `-G` |
| `nvptx-f32ftz` | bool | false | Flush f32 subnormals to zero; **hidden** |
| `w` | bool | false | Disable warnings; **hidden** |
| `Werror` | bool | false | Treat warnings as errors; **hidden** |
| `Osize` | bool | false | Optimize for code size; **hidden** |
| `Om` | bool | false | Maximum optimization mode; **hidden** |
| `maxreg` | int | none | Maximum register count (no limit if unset) |
| `nvptx-nan` | bool | false | NaN handling control; **hidden** |
| `jump-table-density` | int | 10 | Minimum density (%) for jump table lowering |
| `pass-control` | int | -1 | Disable all optional passes after pass N; -1 = no limit |
| `disable-passno` | list | empty | Disable pass(es) by number (comma-separated) |
| `sep-comp` | bool | false | Separate compilation mode |
| `proffile` | string | — | Filename for PGO profile information |
| `R` | string | — | Resource constraint: `name=<int>` format |
| `lnk-disable-allopts` | bool | false | Disable all linker optimization passes |
| `disable-peephole` | bool | false | Disable peephole optimizer |
| `disable-early-taildup` | bool | false | Disable pre-regalloc tail duplication |

### Category 24: Divergence / GPU Execution

Three flags.

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `spec-exec-only-if-divergent-target` | bool | false | Speculative execution only when target is divergent |
| `prefer-predicated-reduction-select` | bool | false | Prefer predicated reduction over after-loop select |
| `openmp-opt-disable-barrier-elimination` | bool | false | Disable OpenMP barrier elimination |

## Knob System 2: NVVMPassOptions

222 pass option slots initialized by `sub_12D6300` (125KB). Each slot is accessed by integer index (1--221) and stored in a ~4,480-byte struct.

### Access Functions

| Function | Purpose |
|---|---|
| `sub_12D6170(base+120, index)` | Fetch pass option descriptor by index |
| `sub_1691920(base+8, index)` | Fetch pass option value from table |
| `sub_12D6090(a1+offset, ...)` | Store string-typed option |
| `sub_12D6100(a1+offset, ...)` | Store integer-typed option |
| `sub_12D6240(a1, index, "0")` | Get option with default value |

See [NVVMPassOptions](nvvm-pass-options.md) for the complete 222-slot inventory.

## Knob System 3: NVIDIA Codegen Knobs

Parsed from the NVVM container format by `sub_1C20170` and `sub_CD9990`. See [NVIDIA Custom Passes](../passes/index.md#nvidia-codegen-knobs--sub_1c20170) for the complete inventory.

## Hidden / Obfuscated Flags

### Obfuscated Flag (`ctor_043_0` at `~0x48EE80`)

A 4-byte CLI flag name computed via XOR-based obfuscation from `unk_3F6F7C7`:
```c
v40 = v37 ^ (-109 * ((offset + 97) ^ 0x811C9DC5));
```
Stored at `qword_4F857C0` with flag bits `0x87 | 0x38` = hidden + really-hidden. NVIDIA deliberately hides this option from static analysis using FNV-1a-like constants.

### Environment Variable Backdoors

| Variable | Purpose | Location |
|---|---|---|
| `NVVMCCWIZ` | Wizard mode (value 553282) -- unlocks `-v`, `-keep`, `-dryrun`, `-lgenfe`, `-opt`, `-llc`, `-lnk`, `-libnvvm` | `sub_8F9C90` |
| `bar` | Extended debug pass registration | `ctor_107_0` at `0x4A64D0` |
| `NVVM_IR_VER_CHK` | Override IR version check (set to "0" to disable) | `sub_12BFF60` |
| `LLVM_OVERRIDE_PRODUCER` | Override bitcode producer string (default `"7.0.1"`) | `ctor_154` at `0x4CE640` |
| `MALLOC_CONF` | jemalloc allocator tuning | `sub_12FCDB0` |
| `LIBNVVM_DISABLE_CONCURRENT_API` | Force single-threaded NVVM compilation | `ctor_104` at `0x4A5810` |

## CLI Defaults Set by Flag Routing

These are effective defaults applied by the flag catalog parser (`sub_9624D0`), not by cl::opt constructors. When no user flag is specified, the parser injects these:

| CLI flag | Default value | Routed cl::opt |
|---|---|---|
| `-arch=compute_<N>` | `compute_75` (SM 75, Turing) | target architecture |
| `-opt=<N>` | `3` (O3) | optimization level |
| `-ftz=<N>` | `0` (no flush-to-zero) | `nvptx-f32ftz` |
| `-prec-sqrt=<N>` | `1` (precise) | `nvptx-prec-sqrtf32=1` |
| `-prec-div=<N>` | `1` (precise) | `nvptx-prec-divf32=1` (CUDA) / `=0` (CL) |
| `-fma=<N>` | `1` (enabled) | `nvptx-fma-level=1` |
| `-opt-fdiv=<N>` | `0` (off) | optimizer fast-div control |
| `-Ofast-compile=<level>` | `0` (off) | fast-compile pipeline |

## NVIDIA Modification Density

| Subsystem | NVIDIA Knobs | LLVM Knobs | Customization Rate |
|---|---|---|---|
| LSR | 11 | 5 | 69% |
| InstCombine | 12 | 4 | 75% |
| Inliner | 9 | 1 | 90% |
| GVN | 11 | 3 | 79% |
| NVPTX Backend | 30+ | 0 | 100% |
| SimplifyCFG | 2 | 8+ | 20% |
| Memory Space Opt | 16 | 0 | 100% |
| Rematerialization | 15 | 0 | 100% |
| SCEV-CGP | 11 | 0 | 100% |
| Register Pressure | 10 | 0 | 100% |
| Vectorizer | 0 | 18+ | 0% |
| SCEV | 0 | 10+ | 0% |

## Cross-References

- [NVVMPassOptions](nvvm-pass-options.md) -- 222-slot pass option system
- [CLI Flags](cli-flags.md) -- complete flag-to-pipeline routing
- [Environment Variables](env-vars.md) -- all verified env vars
- [Optimization Levels](optimization-levels.md) -- O0/O1/O2/O3 and fast-compile pipelines
- [IPMSP](../passes/ipmsp.md) -- memory space optimization engine
- [Alias Analysis](../infra/alias-analysis.md) -- restrict propagation
- [CodeGenPrepare](../llvm/codegen-prepare.md) -- SCEV-CGP pass
