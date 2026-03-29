# Configuration Knobs

Three independent knob systems control compiler behavior: LLVM `cl::opt` flags (~1,689), NVVMPassOptions (222 slots), and NVIDIA codegen knobs (~70).

| | |
|---|---|
| **LLVM cl::opt** | ~1,689 flags, registered at `0x450000`–`0x5CFFFF` in global constructors |
| **NVVMPassOptions** | 222 slots, initialized by `sub_12D6300` (125KB) |
| **Codegen knobs** | ~70, parsed by `sub_1C20170` / `sub_CD9990` from NVVM container |
| **BSS storage** | `0x4F7FEA0`–`0x4FA5xxx` (cl::opt), `a1+0`–`a1+4464` (PassOptions) |
| **Dual PM** | Same options registered for both Legacy PM and New PM |

## Knob System 1: LLVM cl::opt

### Registration Pattern

Every `cl::opt` follows this initialization sequence in a global constructor:

```c
// 1. Atomic option counter increment
InterlockedExchangeAdd64(sub_C523C0(), 1);
// 2. Initialize ~20 fields of option struct at BSS global
// 3. Set option name
sub_C53080(&option, "option-name", strlen);  // Legacy PM
sub_16B8280(&option, "option-name", strlen); // New PM
// 4. Finalize registration
sub_C53130(&option);  // Legacy PM
sub_16B88A0(&option); // New PM
// 5. Register cleanup
__cxa_atexit(destructor, &option, &dso_handle);
```

Each `cl::opt<T>` occupies ~224 bytes (0xE0) in BSS.

### NVIDIA-Specific cl::opt Flags

#### InstCombine / FP Optimization (12 flags, `ctor_165_0` at `0x4D0500`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `split-gep-chain` | bool | — | Split GEP chains to independent GEPs |
| `Disable-Add-to-Or` | bool | true | Disable add-to-or transformations |
| `opt-use-fast-math` | bool | false | More aggressive FP simplification |
| `opt-use-prec-div` | bool | — | Don't use fast division approximation |
| `opt-no-signed-zeros` | bool | — | No signed zero (-0.0) |
| `disable-fp-cast-opt` | bool | — | Disable FP cast optimizations |
| `reorder-sext-before-cnst-add` | bool | false | `sext(add(a,CI))` → `add(sext(a),CI)` (hidden) |
| `disable-sink` | bool | — | Disable sinking |
| `partial-sink` | bool | — | Partial sinking |
| `nvptx-rsqrt-approx-opt` | bool | — | Enable reciprocal sqrt optimization |
| `disable-rsqrt-opt` | bool | — | Disable reciprocal sqrt optimization |
| `check-vn` | bool | — | Check value numbers on transformations |

#### Inliner (9 flags, `ctor_186_0` at `0x4DBEC0`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `profuseinline` | bool | — | Profuse inlining diagnostics |
| `inline-total-budget` | int | — | Total inlining budget |
| `nv-inline-all` | bool | — | Inline all function calls |
| `inline-budget` | int | 20000 | Per-caller inlining budget |
| `inline-adj-budget1` | int | — | Adjusted per-caller budget |
| `inline-switchctrl` | int | — | Tune heuristic on switches |
| `inline-numswitchfunc` | int | — | Heuristic on switch functions |
| `inline-maxswitchcases` | int | — | Heuristic on switch cases |
| `disable-inlined-alloca-merging` | bool | — | Disable alloca merging |

#### GVN (8 flags, `ctor_201` at `0x4E0990`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `profusegvn` | bool | — | Profuse GVN diagnostics |
| `gvn-dom-cache` | bool | — | Cache dominator tree nodes |
| `max-recurse-depth` | int | 1000 | Max recursion depth |
| `enable-phi-remove` | bool | — | Enable PHI removal |
| `dump-phi-remove` | bool | — | Dump PHI removal info |
| `no-split-stores-below` | int | — | Don't split stores below threshold |
| `no-split-stores-above` | int | — | Don't split stores above threshold |
| `split-stores` | bool | — | Store splitting control |

#### Loop Strength Reduction (11 flags, `ctor_214_0` at `0x4E4B00`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `disable-unknown-trip-lsr` | bool | — | Disable LSR for unknown-trip loops |
| `lsr-check-rp` | bool | — | Check register pressure |
| `lsr-rp-limit` | int | — | Skip LSR at register pressure limit |
| `filter-bad-formula` | bool | — | Filter out bad formulae |
| `do-lsr-64-bit` | bool | — | Loop strength reduce for 64-bit |
| `count-sxt-opt-for-reg-pressure` | bool | — | Count sign-ext elim for RP |
| `lsr-sxtopt` | bool | — | Sign-extension elimination in LSR |
| `lsr-loop-level` | int | — | LSR on specific loop levels |
| `lsr-skip-outer-loop` | bool | — | Ignore outer loop IV in LSR |
| `disable-lsr-for-sharedmem32-ptr` | bool | — | **Disable LSR for 32-bit shared mem pointers** |
| `disable-lsr-complexity-discount` | bool | — | Disable complexity estimation discount |

#### IndVarSimplify (2 flags, `ctor_203_0` at `0x4E1CD0`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `Disable-unknown-trip-iv` | bool | — | Disable IV-subst for unknown trip loops |
| `iv-loop-level` | int | — | Control loop-levels for IV-subst |

#### SimplifyCFG (2 flags, `ctor_243_0` at `0x4ED0C0`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `disable-jump-threading` | bool | — | Disable jump threading for OCG experiments |
| `fold-with-var-cond` | bool | — | Fold branches with variance conditions |

#### NVPTX Backend — Math/Scheduling (`ctor_607` at `0x584B60`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `nvptx-sched4reg` | bool | — | Schedule for register pressure |
| `nvptx-fma-level` | int | — | FMA contraction (0=off, 1=on, 2=aggressive) |
| `nvptx-prec-divf32` | int | — | F32 div precision (0=approx, 1=full, 2=IEEE+ftz, 3=IEEE) |
| `nvptx-prec-sqrtf32` | int | — | Sqrt precision (0=approx, 1=rn) |
| `nvptx-approx-log2f32` | bool | — | Use `lg2.approx` for log2 |
| `nvptx-force-min-byval-param-align` | bool | — | Force 4-byte byval alignment |
| `nvptx-normalize-select` | bool | — | Override shouldNormalizeToSelectSequence |
| `enable-bfi64` | bool | — | Enable 64-bit BFI instructions |

#### NVPTX Backend — Passes/Features (`ctor_609_0` at `0x585D30`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `disable-nvptx-load-store-vectorizer` | bool | — | Disable load/store vectorizer |
| `disable-nvptx-require-structured-cfg` | bool | — | Turn off structured CFG requirement |
| `nvptx-short-ptr` | bool | — | 32-bit pointers for const/local/shared |
| `nvptx-enable-machine-sink` | bool | — | Enable machine sinking |
| `enable-new-nvvm-remat` | bool | on | Enable new rematerialization |
| `nv-disable-remat` | bool | — | Disable all remat passes |
| `nv-disable-mem2reg` | bool | — | Disable machine IR mem2reg |
| `nv-disable-scev-cgp` | bool | on | Disable SCEV address mode opt |
| `nvptx-32-bit-smem` | bool | — | 32-bit shared memory pointers |
| `nvptx-exit-on-unreachable` | bool | on | Lower unreachable as exit |
| `nvptx-early-byval-copy` | bool | — | Copy byval args early |
| `enable-nvvm-peephole` | bool | on | Enable NVVM peephole optimizer |
| `no-reg-target-nvptxremat` | bool | — | Only old remat w/o reg targets |
| `lower-func-args` | bool | on | Lower large aggregate params |
| `enable-sink` | bool | on | Enable sinking |
| `disable-post-opt` | bool | — | Disable IR opts post-opt |
| `usedessa` | int | 2 | Select deSSA method |
| `ldg` | bool | on | Load Global Constant Transform |

#### NVPTX Backend — Extended (`ctor_610` at `0x5888A0`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `unroll-assumed-size` | int | 4 | Assumed size for unknown local arrays |
| `enable-loop-peeling` | bool | — | Enable loop peeling |
| `enable-256-bit-load-store` | bool | — | Enable 256-bit vector loads/stores |
| `ias-param-always-point-to-global` | bool | — | Params always point to globals |
| `ias-strong-global-assumptions` | bool | — | Strong global assumptions |
| `ias-wmma-memory-space-opt` | bool | — | MemorySpaceOpt for WMMA |

#### Core Compiler (`ctor_043_0` at `0x48D7F0`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `debug-compile` | bool | — | Compile for debugging |
| `generate-line-info` | bool | — | Emit line info even without `-G` |
| `nvptx-f32ftz` | bool | — | Flush f32 subnormals to zero (hidden) |
| `w` | bool | — | Disable warnings (hidden) |
| `Werror` | bool | — | Treat warnings as errors (hidden) |
| `Osize` | bool | — | Optimize for code size (hidden) |
| `Om` | bool | — | Maximum optimization (hidden) |
| `maxreg` | int | — | Max register count |
| `nvptx-nan` | bool | — | NaN handling control (hidden) |
| `jump-table-density` | int | 10 | Min density for jump table |

#### Pass Control (NVIDIA-specific, `ctor_028_0` at `0x489160`)

| Flag | Type | Default | Purpose |
|---|---|---|---|
| `pass-control` | int | -1 | Disable all optional passes after specified pass number |
| `disable-passno` | list | — | Disable pass(es) by number (comma-separated) |

## Knob System 2: NVVMPassOptions

222 pass option slots initialized by `sub_12D6300` (125KB). Each slot is accessed by integer index (1–221) and stored in a ~4,480-byte struct.

### Access Functions

| Function | Purpose |
|---|---|
| `sub_12D6170(base+120, index)` | Fetch pass option descriptor by index |
| `sub_1691920(base+8, index)` | Fetch pass option value from table |
| `sub_12D6090(a1+offset, ...)` | Store string-typed option |
| `sub_12D6100(a1+offset, ...)` | Store integer-typed option |
| `sub_12D6240(a1, index, "0")` | Get option with default value |

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
| `NVVMCCWIZ` | Wizard mode (value 553282) | `sub_8F9C90` (real main) |
| `bar` | Extended debug pass registration | `ctor_107_0` at `0x4A64D0` |
| `NVVM_IR_VER_CHK` | Override IR version check | `sub_12BFF60` |
| `LLVM_OVERRIDE_PRODUCER` | Override bitcode producer string (default `"7.0.1"`) | `ctor_154` at `0x4CE640` |
| `MALLOC_CONF` | jemalloc allocator tuning | `sub_12FCDB0` |

## NVIDIA Modification Density

| Subsystem | NVIDIA Knobs | LLVM Knobs | Customization Rate |
|---|---|---|---|
| LSR | 11 | 5 | 69% |
| InstCombine | 12 | 4 | 75% |
| Inliner | 9 | 1 | 90% |
| GVN | 8 | 3 | 73% |
| NVPTX Backend | 30+ | 0 | 100% |
| SimplifyCFG | 2 | 8+ | 20% |
| Vectorizer | 0 | 18+ | 0% |
| SCEV | 0 | 10+ | 0% |
