# Optimization Levels

cicc v13.0 supports four standard optimization levels (O0 through O3) and three fast-compile tiers (Ofcmin, Ofcmid, Ofcmax). These are mutually exclusive with the custom `--passes=` interface. The pipeline name is selected in the new-PM driver `sub_226C400` and assembled by `sub_12E54A0`. The full optimization pipeline builder is `sub_12DE330`, with tier-specific insertion handled by `sub_12DE8F0`.

## Pipeline Name Selection

The new-PM driver at `sub_226C400` selects a pipeline name string based on boolean flags in the config struct:

| Config Offset | Flag | Pipeline Name |
|---------------|------|---------------|
| byte[888] | O0 | `nvopt<O0>` |
| byte[928] | O1 | `nvopt<O1>` |
| byte[968] | O2 | `nvopt<O2>` |
| byte[1008] | O3 | `nvopt<O3>` |
| qw[131..132] | fc="max" | `nvopt<Ofcmax>` |
| qw[131..132] | fc="mid" | `nvopt<Ofcmid>` |
| qw[131..132] | fc="min" | `nvopt<Ofcmin>` |

Combining `-O#` with `--passes=` is an error:

> "Cannot specify -O#/-Ofast-compile=\<min,mid,max\> and --passes=/--foo-pass, use -passes='default\<O#\>,other-pass'"

The pipeline name is passed to `sub_2277440` (new-PM text parser), which constructs the actual PassManager. The `nvopt` prefix is registered as a pipeline element in `sub_225D540` (new PM) and `sub_12C35D0` (legacy PM), with vtables at `0x4A08350` / `0x49E6A58`.

## Fast-Compile Level Encoding

The fast-compile level is stored as an integer at offset 1640 (or 1648 in the clone) of the compilation context:

| Value | CLI Source | Behavior |
|-------|-----------|----------|
| 0 | (no flag, or `-Ofast-compile=0`) | Normal O-level pipeline |
| 1 | `-Ofast-compile=0` | Forwarded then reset to 0 |
| 2 | `-Ofast-compile=max` / `-Ofc=max` | Minimal pipeline, fastest compile |
| 3 | `-Ofast-compile=mid` / `-Ofc=mid` | Medium pipeline |
| 4 | `-Ofast-compile=min` / `-Ofc=min` | Close to full optimization |

Any other value produces: `"libnvvm : error: -Ofast-compile called with unsupported level"`.

## Tier Summary

| Pipeline | Approx Passes | LSA-Opt | MemSpaceOpt | Compile Speed |
|----------|--------------|---------|-------------|---------------|
| `nvopt<O0>` | 5--8 | off | off | Fastest (no opt) |
| `nvopt<Ofcmax>` | 12--15 | forced 0 | forced 0 | Fast |
| `nvopt<Ofcmid>` | 25--30 | normal | enabled | Medium |
| `nvopt<Ofcmin>` | 30--35 | normal | enabled | Slower |
| `nvopt<O1>` | ~35 | normal | enabled | Normal |
| `nvopt<O2>` | ~35+ | normal | enabled | Normal |
| `nvopt<O3>` | ~35+ | normal | enabled | Slowest |

O1/O2/O3 all call `sub_12DE330` (the full pipeline builder) which adds the same ~35 passes. The difference manifests through the tiered pass inserter `sub_12DE8F0`, which gates certain passes on the tier level, and through parameter differences in loop unrolling, vectorization, and CGSCC iteration counts.

## O1/O2/O3 Full Pipeline (sub\_12DE330)

When an explicit O-level is set and fast-compile is 0, the pipeline builder constructs approximately 40 passes:

| # | Pass | Address | Notes |
|---|------|---------|-------|
| 1 | VerifierPass | `sub_1654860(1)` | |
| 2 | CGSCC/Inliner | `sub_1A62BF0(1,0,0,1,0,0,1)` | |
| 3 | NVVMReflect | `sub_1B26330()` | |
| 4 | SROA | `sub_185D600()` | |
| 5 | NVVMLowerArgs | `sub_1C6E800()` | |
| 6 | NVVMLowerAlloca | `sub_1C6E560()` | |
| 7 | SimplifyCFG | `sub_1857160()` | |
| 8 | InstCombine | `sub_1842BC0()` | |
| 9 | GVN | `sub_17060B0(1,0)` | Conditional on config[3160] |
| 10 | NVVMVerify | `sub_12D4560()` | |
| 11 | LoopRotate | `sub_18A3090()` | |
| 12 | LICM | `sub_184CD60()` | |
| 13 | IndVarSimplify | `sub_1869C50(1,0,1)` | Conditional on !config[1040] |
| 14 | LoopUnroll(3) | `sub_1833EB0(3)` | Factor 3 |
| 15 | GVN | `sub_17060B0(1,0)` | |
| 16 | LoopIndexSplit/SCCP | `sub_1952F90(-1)` | Threshold -1 (unlimited) |
| 17 | CGSCC/Inliner | `sub_1A62BF0(1,0,0,1,0,0,1)` | |
| 18 | DSE | `sub_1A223D0()` | |
| 19 | GVN | `sub_17060B0(1,0)` | |
| 20 | MemCpyOpt | `sub_1A7A9F0()` | |
| 21 | CGSCC/Inliner | `sub_1A62BF0(1,0,0,1,0,0,1)` | |
| 22 | ADCE | `sub_1A02540()` | |
| 23 | JumpThreading/CVP | `sub_198DF00(-1)` | |
| 24 | NVVMDivergenceLowering | `sub_1C76260()` | Conditional on !config[1320] |
| 25 | Reassociate | `sub_195E880(0)` | Conditional on config[2880] |
| 26 | SpeculativeExecution | `sub_19C1680(0,1)` | Conditional on !config[1360] |
| 27 | GVN (verified) | `sub_17060B0(1,0)` | Conditional on config[3160] |
| 28 | SCCP | `sub_19401A0()` | |
| 29 | GlobalDCE/ConstantProp | `sub_1968390()` | |
| 30 | GlobalOpt | `sub_196A2B0()` | |
| 31 | LoopVectorize/SLP | `sub_19B73C0(2,-1,-1,-1,-1,-1,-1)` | |
| 32 | GVN | `sub_17060B0(1,0)` | |
| 33 | EarlyCSE | `sub_190BB10(0,0)` | |
| 34 | TailCallElim | `sub_1A13320()` | |
| 35 | NewGVN | `sub_18F5480()` | |
| 36 | Sink | `sub_18DEFF0()` | |
| 37 | CGSCC/Inliner | `sub_1A62BF0(1,0,0,1,0,0,1)` | |
| 38 | Sinking2 | `sub_18B1DE0()` | NVIDIA custom |
| 39 | LoopSimplify/LCSSA | `sub_1841180()` | |

After this sequence, a common tail adds MemorySpaceOpt, NVVMFinal cleanup, VerifierPass, and machine-level pass setup.

## Ofcmax Pipeline (Fastest Compile)

Ofcmax bypasses the full pipeline entirely. It forces two optimizer flags:

- `-lsa-opt=0` (disables LSA optimization)
- `-memory-space-opt=0` (disables MemorySpaceOpt pass)

The minimal pass sequence:

1. **Sinking2Pass** (fast mode): `sub_18B3080(1)`
2. **SimplifyCFG**
3. **LoopStrengthReduce** (if applicable)
4. **NVVMReflect**
5. **NVVMVerify**
6. **LICM**
7. **LowerSwitch**
8. **NVVMVerify**

This is confirmed in both `sub_9624D0` (line 1358) and `sub_12CC750` (line 2025): when `fc_level == 2`, LSA and MemorySpaceOpt are unconditionally disabled.

## Ofcmid Pipeline (Medium)

Ofcmid runs a more complete optimization pipeline (~25--30 passes) without forcing LSA or MemorySpaceOpt off. Key passes include:

LICM, AnnotationCleanup, NVVMReflect, CorrelatedValuePropagation, NVVMPeephole, NVVMPeephole2, GVN (conditional), JumpThreading, NVVMLowerArgs, LoopSimplify, CGSCC (5 iterations), DCE, LCSSA, Sink, GVN, LICM, Reassociate, EarlyCSE, LoopVectorize (conservative), DSE, MemorySpaceOpt, MemorySpaceOpt2, BranchFolding, and Sinking2.

## Post-Optimization Common Tail

Regardless of pipeline tier, `sub_12E54A0` always appends:

1. **MemorySpaceOpt** (conditional, unless forced off by Ofcmax)
2. **NVVMFinal** / cleanup: `sub_1CEBD10()`
3. **VerifierPass**: `sub_1654860(1)`
4. **Machine-level pass setup**: `sub_12DFE00()`

## Always-Added Analysis Passes

Before any optimization, the pipeline assembler inserts:

- **TargetLibraryInfo**: `sub_149CBC0`
- **Target transform info**: `sub_1BFB9A0`
- **LLVM alias analysis**: `sub_1611EE0`
- **Module verifier** (conditional)

## Tiered Pass Differences (O1 vs O2 vs O3)

`sub_12DE8F0(tier)` adds tier-specific passes on top of the base pipeline:

- **Tier 1 (O1)**: ConstantEval, AnnotationCleanup, DSE, SimplifyCFG, InstCombine, LoopRotate, GVN, LICM.
- **Tier 2 (O2)**: adds EarlyCSE, LoopIndexSplit, Reassociate, NVVMAliasAnalysis, IndVarSimplify, LoopUnroll, NVVMDivergenceLowering, ADCE, InlineFunction, MemCpyOpt, DSE, SCCP, GlobalDCE, GlobalOpt, LoopVectorize/SLP, TailCallElim, NewGVN, Sink, Sinking2.
- **Tier 3 (O3)**: adds more aggressive LoopUnroll, SpeculativeExecution, full SCCP, more aggressive vectorizer parameters, ADCE, and higher inlining thresholds.
