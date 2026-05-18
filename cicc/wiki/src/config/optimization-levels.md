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

Selection logic in `sub_226C400` (lines 828--874):

```text
if (O1_flag)       -> "nvopt<O1>"
else if (O2_flag)  -> "nvopt<O2>"
else if (O3_flag)  -> "nvopt<O3>"
else if (fc_len == 3) {
  if (fc == "max") -> "nvopt<Ofcmax>"
  if (fc == "mid") -> "nvopt<Ofcmid>"
  if (fc == "min") -> "nvopt<Ofcmin>"
}
else               -> "nvopt<O0>"
```

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

When level=1, the flag is forwarded to the optimizer phase as a pass argument and then the level is reset to 0 at offset 1640 (so it becomes normal O-level optimization). When level=2 (max), the optimizer arg string `-Ofast-compile=max` is appended. When level=3 (mid), `-Ofast-compile=mid` is appended. When level=4 (min), `-Ofast-compile=min` is appended.

## Tier Summary

| Pipeline | Approx Passes | LSA-Opt | MemSpaceOpt | Compile Speed |
|----------|--------------|---------|-------------|---------------|
| `nvopt<O0>` | 5--8 | off | off | Fastest (no opt) |
| `nvopt<Ofcmax>` | 12--15 | forced 0 | forced 0 | Fast |
| `nvopt<Ofcmid>` | 25--30 | normal | enabled | Medium |
| `nvopt<Ofcmin>` | 30--35 | normal | enabled | Slower |
| `nvopt<O1>` | ~40 + tier-1 | normal | enabled | Normal |
| `nvopt<O2>` | ~40 + tier-1/2 | normal | enabled | Normal |
| `nvopt<O3>` | ~40 + tier-1/2/3 | normal | enabled | Slowest |

## Pipeline Architecture: Tier 0 + Tiers 1/2/3

O1/O2/O3 share a common pipeline construction path. The key insight is that optimization happens in layers:

1. **Tier 0** (`sub_12DE330`): The full base pipeline of ~40 passes. Fires for ALL of O1, O2, and O3 when `opts[4224]` (optimization-enabled) is set.
2. **Tier 1** (`sub_12DE8F0(PM, 1, opts)`): Additional passes gated by `opts[3528]`. Fires for O1, O2, and O3.
3. **Tier 2** (`sub_12DE8F0(PM, 2, opts)`): Additional passes gated by `opts[3568]`. Fires for O2 and O3 only.
4. **Tier 3** (`sub_12DE8F0(PM, 3, opts)`): Additional passes gated by `opts[3608]`. Fires for O3 only.

The tier control fields in the NVVMPassOptions struct at 4512 bytes:

| Offset | Type | Meaning |
|--------|------|---------|
| 3528 | bool | Tier 1 enable (O1+) |
| 3532 | int | Tier 1 phase threshold |
| 3568 | bool | Tier 2 enable (O2+) |
| 3572 | int | Tier 2 phase threshold |
| 3608 | bool | Tier 3 enable (O3+) |
| 3612 | int | Tier 3 phase threshold |
| 4224 | bool | Tier 0 enable (any O-level) |
| 4228 | int | Tier 0 phase threshold |

The assembler loop in `sub_12E54A0` (lines 481--553) iterates over the plugin/external pass list at `opts[4488]`. Each entry has a phase_id; when the phase_id exceeds a tier's threshold, that tier fires:

```text
for each entry in opts[4488..4496]:
  phase_id = entry[8..12]
  if (opts[4224] && phase_id > opts[4228]):
    sub_12DE330(PM, opts)   // Tier 0
    opts[4224] = 0          // one-shot
  if (opts[3528] && phase_id > opts[3532]):
    sub_12DE8F0(PM, 1, opts) // Tier 1
    opts[3528] = 0
  if (opts[3568] && phase_id > opts[3572]):
    sub_12DE8F0(PM, 2, opts) // Tier 2
    opts[3568] = 0
  if (opts[3608] && phase_id > opts[3612]):
    sub_12DE8F0(PM, 3, opts) // Tier 3
    opts[3608] = 0
  AddPass(PM, entry->createPass())
```

After the loop, any remaining unfired tiers fire unconditionally.

## Tier 0: Full Base Pipeline (sub\_12DE330)

`sub_12DE330` at `0x12DE330` is called for all O1/O2/O3 compilations. It constructs the ~40-pass base pipeline:

| # | Factory | Pass | Guard | Notes |
|---|---------|------|-------|-------|
| 1 | `sub_1654860(1)` | VerifierPass | always | |
| 2 | `sub_1A62BF0(1,0,0,1,0,0,1)` | CGSCC/Inliner | always | Pipeline EP 1, 1 iteration |
| 3 | `sub_1B26330()` | NVVMReflect | always | |
| 4 | `sub_185D600()` | SROA | always | |
| 5 | `sub_1C6E800()` | NVVMLowerArgs | always | |
| 6 | `sub_1C6E560()` | NVVMLowerAlloca | always | |
| 7 | `sub_1857160()` | SimplifyCFG | always | |
| 8 | `sub_1842BC0()` | InstCombine | always | |
| 9 | `sub_17060B0(1,0)` | GVN | `opts[3160]` | Debug-dump enabled |
| 10 | `sub_12D4560()` | NVVMVerify | always | |
| 11 | `sub_18A3090()` | LoopRotate | always | |
| 12 | `sub_184CD60()` | LICM | always | |
| 13 | `sub_1869C50(1,0,1)` | IndVarSimplify | `!opts[1040]` | |
| 14 | `sub_1833EB0(3)` | LoopUnroll | always | Factor = 3 |
| 15 | `sub_17060B0(1,0)` | GVN | always | |
| 16 | `sub_1952F90(-1)` | LoopIndexSplit/SCCP | always | Threshold = -1 (unlimited) |
| 17 | `sub_1A62BF0(1,0,0,1,0,0,1)` | CGSCC/Inliner | always | |
| 18 | `sub_1A223D0()` | DSE | always | |
| 19 | `sub_17060B0(1,0)` | GVN | always | |
| 20 | `sub_1A7A9F0()` | MemCpyOpt | always | |
| 21 | `sub_1A62BF0(1,0,0,1,0,0,1)` | CGSCC/Inliner | always | |
| 22 | `sub_1A02540()` | ADCE | always | |
| 23 | `sub_198DF00(-1)` | JumpThreading/CVP | always | Threshold = -1 |
| 24 | `sub_1C76260()` | NVVMDivergenceLowering | `!opts[1320]` | |
| 25 | `sub_195E880(0)` | Reassociate | `opts[2880]` | Default on (slot 143) |
| 26 | `sub_19C1680(0,1)` | SpeculativeExecution | `!opts[1360]` | |
| 27 | `sub_17060B0(1,0)` | GVN | `opts[3160]` | Debug-dump enabled |
| 28 | `sub_19401A0()` | SCCP | always | |
| 29 | `sub_1968390()` | GlobalDCE/ConstantProp | always | |
| 30 | `sub_196A2B0()` | GlobalOpt | always | |
| 31 | `sub_19B73C0(2,-1,-1,-1,-1,-1,-1)` | LoopVectorize/SLP | always | Width=2, thresholds=-1 |
| 32 | `sub_17060B0(1,0)` | GVN | always | |
| 33 | `sub_190BB10(0,0)` | EarlyCSE | always | |
| 34 | `sub_1A13320()` | TailCallElim | always | |
| 35 | `sub_17060B0(1,1)` | GVN (verified) | `opts[3160]` | Verify mode |
| 36 | `sub_18F5480()` | NewGVN | always | |
| 37 | `sub_18DEFF0()` | Sink | always | |
| 38 | `sub_1A62BF0(1,0,0,1,0,0,1)` | CGSCC/Inliner | always | |
| 39 | `sub_18B1DE0()` | Sinking2 | always | NVIDIA custom |
| 40 | `sub_1841180()` | LoopSimplify/LCSSA | always | |

After `sub_12DE330` returns, `opts[4224]` is cleared (one-shot).

## Tiers 1/2/3: Phase-Specific Sub-Pipeline (sub\_12DE8F0)

`sub_12DE8F0` at `0x12DE8F0` is a single function called with `tier` in {1, 2, 3}. The tier value is stored into `qword_4FBB410` (phase tracker). When `tier==3` and `qword_4FBB370` byte4 is 0, the feature flags are set to 6 (enabling advanced barrier opt + memory space opt gates).

The following table lists every pass in `sub_12DE8F0` with its tier-dependent guard condition. A pass runs only when ALL conditions in its Guard column are satisfied.

| # | Factory | Pass | Guard | O1 | O2 | O3 |
|---|---------|------|-------|:--:|:--:|:--:|
| 1 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` | Y | Y | Y |
| 2 | `sub_1A223D0()` | NVVMIRVerification | `!opts[2600]` | Y | Y | Y |
| 3 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` | Y | Y | Y |
| 4 | `sub_18E4A00()` | NVVMBarrierAnalysis | `opts[3488]` | Y | Y | Y |
| 5 | `sub_1C98160(0)` | NVVMLowerBarriers | `opts[3488]` | Y | Y | Y |
| 6 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` | Y | Y | Y |
| 7 | `sub_12D4560()` | NVVMVerifier | `!opts[600]` | Y | Y | Y |
| 8 | `sub_185D600()` | IPConstPropagation | `opts[3200] && !opts[920]` | Y | Y | Y |
| 9 | `sub_1857160()` | NVVMReflect | `opts[3200] && !opts[880]` | Y | Y | Y |
| 10 | `sub_18A3430()` | NVVMPredicateOpt | `opts[3200] && !opts[1120]` | Y | Y | Y |
| 11 | `sub_1842BC0()` | SCCP | `opts[3200] && !opts[720]` | Y | Y | Y |
| 12 | `sub_17060B0(1,0)` | PrintModulePass | `!opts[1080]` | Y | Y | Y |
| 13 | `sub_12D4560()` | NVVMVerifier | `!opts[600]` | Y | Y | Y |
| 14 | `sub_18A3090()` | NVVMPredicateOpt variant | `opts[3200] && !opts[2160]` | Y | Y | Y |
| 15 | `sub_184CD60()` | ConstantMerge | `opts[3200] && !opts[1960]` | Y | Y | Y |
| 16 | `sub_190BB10(1,0)` | SimplifyCFG | **`tier!=1`** `&& !opts[1040] && !opts[1200]` | - | Y | Y |
| 17 | `sub_1952F90(-1)` | LoopIndexSplit | (same as #16) `&& !opts[1160]` | - | Y | Y |
| 18 | `sub_12D4560()` | NVVMVerifier | (same as #16) `&& !opts[600]` | - | Y | Y |
| 19 | `sub_17060B0(1,0)` | PrintModulePass | (same as #16) `&& !opts[1080]` | - | Y | Y |
| 20 | `sub_195E880(0)` | LICM | `opts[3704] && opts[2880] && !opts[1240]` | Y | Y | Y |
| 21 | `sub_1C8A4D0(v12)` | EarlyCSE | always; `v12=1 if opts[3704]` | Y | Y | Y |
| 22 | `sub_1869C50(1,0,1)` | Sink | **`tier!=1`** `&& !opts[1040]` | - | Y | Y |
| 23 | `sub_1833EB0(3)` | TailCallElim | **`tier==3`** `&& !opts[320]` | - | - | Y |
| 24 | `sub_1CC3990()` | NVVMUnreachableBlockElim | `!opts[2360]` | Y | Y | Y |
| 25 | `sub_18EEA90()` | CorrelatedValuePropagation | `opts[3040]` | Y | Y | Y |
| 26 | `sub_12D4560()` | NVVMVerifier | `!opts[600]` | Y | Y | Y |
| 27 | `sub_1A223D0()` | NVVMIRVerification | `!opts[2600]` | Y | Y | Y |
| 28 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` | Y | Y | Y |
| 29 | `sub_1C4B6F0()` | Inliner | `!opts[440] && !opts[480]` | Y | Y | Y |
| 30 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` | Y | Y | Y |
| 31 | `sub_1A7A9F0()` | InstructionSimplify | `!opts[2720]` | Y | Y | Y |
| 32 | `sub_12D4560()` | NVVMVerifier | `!opts[600]` | Y | Y | Y |
| 33 | `sub_1A02540()` | GenericToNVVM | `!opts[2200]` | Y | Y | Y |
| 34 | `sub_198DF00(-1)` | LoopSimplify | `!opts[1520]` | Y | Y | Y |
| 35 | `sub_1C76260()` | ADCE | `!opts[1320] && !opts[1480]` | Y | Y | Y |
| 36 | `sub_17060B0(1,0)` | PrintModulePass | (same as #35) `&& !opts[1080]` | Y | Y | Y |
| 37 | `sub_12D4560()` | NVVMVerifier | (same as #35) `&& !opts[600]` | Y | Y | Y |
| 38 | `sub_195E880(0)` | LICM | `opts[2880] && !opts[1240]` | Y | Y | Y |
| 39 | `sub_1C98160(0/1)` | NVVMLowerBarriers | `opts[3488]` | Y | Y | Y |
| 40 | `sub_19C1680(0,1)` | LoopUnroll | `!opts[1360]` | Y | Y | Y |
| 41 | `sub_17060B0(1,0)` | PrintModulePass | `!opts[1080]` | Y | Y | Y |
| 42 | `sub_19401A0()` | InstCombine | `!opts[1000]` | Y | Y | Y |
| 43 | `sub_196A2B0()` | EarlyCSE | `!opts[1440]` | Y | Y | Y |
| 44 | `sub_1968390()` | SROA | `!opts[1400]` | Y | Y | Y |
| 45 | `sub_19B73C0(tier,...)` | LoopVectorize/SLP (1st) | **`tier!=1`**; params vary by SM | - | Y | Y |
| 46 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` | Y | Y | Y |
| 47 | `sub_19B73C0(tier,...)` | LoopVectorize/SLP (2nd) | `!opts[2760]` | Y | Y | Y |
| 48 | `sub_1A62BF0(1,...)` | LLVM standard pipeline | `!opts[600]` | Y | Y | Y |
| 49 | `sub_1A223D0()` | NVVMIRVerification | `!opts[2600]` | Y | Y | Y |
| 50 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` | Y | Y | Y |
| 51 | `sub_17060B0(1,0)` | PrintModulePass | `!opts[1080]` | Y | Y | Y |
| 52 | `sub_190BB10(0,0)` | SimplifyCFG | `!opts[960]` | Y | Y | Y |
| 53 | `sub_1922F90()` | NVIDIA loop pass | `opts[3080]` | Y | Y | Y |
| 54 | `sub_195E880(0)` | LICM | `opts[2880] && !opts[1240]` | Y | Y | Y |
| 55 | `sub_1A13320()` | NVVMRematerialization | `!opts[2320]` | Y | Y | Y |
| 56 | `sub_1968390()` | SROA | `!opts[1400]` | Y | Y | Y |
| 57 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` | Y | Y | Y |
| 58 | `sub_18EEA90()` | CorrelatedValuePropagation | `opts[3040]` | Y | Y | Y |
| 59 | `sub_18F5480()` | DSE | `!opts[760]` | Y | Y | Y |
| 60 | `sub_18DEFF0()` | DCE | `!opts[280]` | Y | Y | Y |
| 61 | `sub_1A62BF0(1,...)` | LLVM standard pipeline | `!opts[600]` | Y | Y | Y |
| 62 | `sub_1AAC510()` | NVIDIA-specific pass | `!opts[520] && !opts[560]` | Y | Y | Y |
| 63 | `sub_1A223D0()` | NVVMIRVerification | `!opts[2600]` | Y | Y | Y |
| 64 | `sub_1CB4E40(1)` | NVVMIntrinsicLowering | `!opts[2000]` | Y | Y | Y |
| 65 | `sub_1C8E680()` | MemorySpaceOpt | `!opts[2680]`; param from `opts[3120]` | Y | Y | Y |
| 66 | `sub_1A223D0()` | NVVMIRVerification | `opts[3120] && !opts[2600]` | Y | Y | Y |
| 67 | `sub_17060B0(1,0)` | PrintModulePass | `!opts[1080]` | Y | Y | Y |
| 68 | `sub_1CC71E0()` | NVVMGenericAddrOpt | `!opts[2560]` | Y | Y | Y |
| 69 | `sub_1C98270(1,opts[2920])` | NVVMLowerBarriers variant | `opts[3488]` | Y | Y | Y |
| 70 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3160] && !opts[1080]` | Y | Y | Y |
| 71 | `sub_1C6FCA0()` | ADCE | `opts[2840] && !opts[1840]` | Y | Y | Y |
| 72 | `sub_18B1DE0()` | LoopOpt/BarrierOpt | `opts[3200] && !opts[2640]` | Y | Y | Y |
| 73 | `sub_1857160()` | NVVMReflect (late) | `opts[3200] && `**`tier==3`**` && !opts[880]` | - | - | Y |
| 74 | `sub_1841180()` | FunctionAttrs | `opts[3200] && !opts[680]` | Y | Y | Y |
| 75 | `sub_1C46000()` | NVVMLateOpt | **`tier==3`**` && !opts[360]` | - | - | Y |
| 76 | `sub_1841180()` | FunctionAttrs (2nd) | `opts[3200] && !opts[680]` | Y | Y | Y |
| 77 | `sub_1CBC480()` | NVVMLowerAlloca | `!opts[2240] && !opts[2280]` | Y | Y | Y |
| 78 | `sub_1CB73C0()` | NVVMBranchDist | `!opts[2080] && !opts[2120]` | Y | Y | Y |
| 79 | `sub_1C7F370(1)` | NVVMWarpShuffle | `opts[3328] && !opts[1640]` | Y | Y | Y |
| 80 | `sub_1CC5E00()` | NVVMReduction | `opts[3328] && !opts[2400]` | Y | Y | Y |
| 81 | `sub_1CC60B0()` | NVVMSinking2 | `opts[3328] && !opts[2440]` | Y | Y | Y |
| 82 | `sub_1CB73C0()` | NVVMBranchDist (2nd) | `opts[3328] && !opts[2080] && !opts[2120]` | Y | Y | Y |
| 83 | `sub_17060B0(1,0)` | PrintModulePass | `opts[3328] && !opts[1080]` | Y | Y | Y |
| 84 | `sub_1B7FDF0(3)` | Reassociate | `opts[3328] && !opts[1280]` | Y | Y | Y |
| 85 | `sub_17060B0(1,0)` | PrintModulePass (final) | `opts[3160] && !opts[1080]` | Y | Y | Y |

## O1 vs O2 vs O3: Complete Diff

The three O-levels differ through exactly five mechanisms. Every pass that is NOT listed here runs identically at all three levels.

### 1. Tier guard: `tier!=1` (O2/O3 only)

These passes are present in `sub_12DE8F0` but skip when tier==1 (O1):

| Pass | Factory | Effect of skipping at O1 |
|------|---------|--------------------------|
| SimplifyCFG | `sub_190BB10(1,0)` | No inter-tier CFG cleanup |
| LoopIndexSplit | `sub_1952F90(-1)` | No inter-tier loop splitting |
| NVVMVerifier (post-split) | `sub_12D4560()` | No verification after split |
| Sink | `sub_1869C50(1,0,1)` | No inter-tier instruction sinking |
| LoopVectorize/SLP (1st call) | `sub_19B73C0(tier,...)` | No aggressive vectorization |

At O1, the base pipeline (Tier 0) already includes one instance of LoopVectorize with `sub_19B73C0(2,-1,-1,-1,-1,-1,-1)` -- width 2, all thresholds at -1 (unlimited). The tier!=1 guard blocks a SECOND, more aggressive vectorization pass with SM-dependent parameters.

### 2. Tier guard: `tier==3` (O3 only)

These passes run exclusively at O3:

| Pass | Factory | Purpose |
|------|---------|---------|
| TailCallElim | `sub_1833EB0(3)` | Additional tail call optimization pass |
| NVVMReflect (late) | `sub_1857160()` | Second-round __nvvm_reflect resolution |
| NVVMLateOpt | `sub_1C46000()` | O3-exclusive NVIDIA custom late optimization |

`sub_1C46000` (`NVVMLateOpt`) is the most significant O3-exclusive pass. It runs only when `!opts[360]` (not disabled) and only at tier==3. This is a dedicated NVIDIA optimization pass that performs additional transformations after the main pipeline is complete.

### 3. Feature flag `qword_4FBB370` escalation

When `tier==3` and `qword_4FBB370` byte4 is 0, the function sets `qword_4FBB370 = 6` (binary 110). This enables two feature gates:
- **Advanced barrier optimization** (bit 1)
- **Memory space optimization extensions** (bit 2)

These gates affect behavior in downstream passes that read `qword_4FBB370`, such as `sub_12EC4F0` (the machine pass pipeline executor).

### 4. LoopVectorize/SLP parameter differences

`sub_19B73C0` is called with different parameters depending on context:

| Call site | Parameters | Tier |
|-----------|-----------|------|
| Tier 0 (`sub_12DE330` #31) | `(2, -1, -1, -1, -1, -1, -1)` | All O1/O2/O3 |
| Tier 1/2/3, 1st call (#45) | `(tier, ...)` SM-dependent | O2/O3 only |
| Tier 1/2/3, 2nd call (#47) | `(tier, ...)` | All tiers |
| Ofcmid language path | `(3, -1, -1, 0, 0, -1, 0)` | Fast-compile |

The 7 parameters to `sub_19B73C0` control:
- `arg1`: Vector width factor (2 at Tier 0, `tier` at higher tiers)
- `arg2..arg7`: Thresholds for cost model, trip count, and SLP width. Value -1 means unlimited/auto; value 0 means conservative/disabled.

At O2, `sub_19B73C0(2, ...)` provides moderate vectorization. At O3, `sub_19B73C0(3, ...)` increases the vector width factor, enabling wider SIMD exploration. The SM-architecture-dependent parameters are resolved at runtime based on the target GPU.

### 5. CGSCC iteration count

`sub_1A62BF0` is the CGSCC (Call Graph SCC) pass manager factory. The first argument is the pipeline extension point / iteration count:

| Context | Call | Iterations |
|---------|------|-----------|
| Tier 0 (all O-levels) | `sub_1A62BF0(1,0,0,1,0,0,1)` | 1 |
| Ofcmid path | `sub_1A62BF0(5,0,0,1,0,0,1)` | 5 |
| Language "mid" path | `sub_1A62BF0(8,0,0,1,1,0,1)` | 8, with extra opt flag |

O1/O2/O3 all use 1-iteration CGSCC in their shared Tier 0 pipeline. The iteration count differences appear in the fast-compile and language-specific paths, not between O-levels.

## Complete O-Level Comparison Matrix

| Feature | O0 | O1 | O2 | O3 |
|---------|:--:|:--:|:--:|:--:|
| Tier 0 base pipeline (~40 passes) | - | Y | Y | Y |
| Tier 1 sub-pipeline | - | Y | Y | Y |
| Tier 2 sub-pipeline | - | - | Y | Y |
| Tier 3 sub-pipeline | - | - | - | Y |
| LoopVectorize (base, width=2) | - | Y | Y | Y |
| LoopVectorize (tier, SM-dependent) | - | - | Y | Y |
| SimplifyCFG (inter-tier) | - | - | Y | Y |
| LoopIndexSplit (inter-tier) | - | - | Y | Y |
| Sink (inter-tier) | - | - | Y | Y |
| TailCallElim (extra) | - | - | - | Y |
| NVVMReflect (late round) | - | - | - | Y |
| NVVMLateOpt (`sub_1C46000`) | - | - | - | Y |
| Feature flags escalation (6) | - | - | - | Y |
| NVVMDivergenceLowering | - | Y | Y | Y |
| SpeculativeExecution | - | Y | Y | Y |
| MemorySpaceOpt | - | Y | Y | Y |
| NVVMWarpShuffle | - | Y | Y | Y |
| NVVMReduction | - | Y | Y | Y |
| NVVMRematerialization | - | Y | Y | Y |
| NVVMBranchDist | - | Y | Y | Y |
| LSA optimization | off | on | on | on |

## O0 Pipeline (Minimal)

When no O-level flag is set and no fast-compile level is active, the assembler falls through to `LABEL_159` which calls:

```text
sub_1C8A4D0(0)   -- NVVMFinalCleanup or similar minimal pass
```

Then the common tail at `LABEL_84` adds:
1. MemorySpaceOpt (conditional, skipped at O0 since `opts[3488]` is typically unset)
2. `sub_1CEBD10()` -- NVVMFinal / cleanup
3. `sub_1654860(1)` -- VerifierPass
4. `sub_12DFE00()` -- Codegen pass setup

The O0 pipeline does NOT call `sub_12DE330` or `sub_12DE8F0`. It runs only the infrastructure passes (TargetLibraryInfo, TargetTransformInfo, BasicAA, AssumptionCacheTracker, ProfileSummaryInfo) plus minimal canonicalization.

## Ofcmax Pipeline (Fastest Compile)

Ofcmax bypasses the full pipeline entirely. It forces two optimizer flags:

- `-lsa-opt=0` (disables LSA optimization)
- `-memory-space-opt=0` (disables MemorySpaceOpt pass)

This forcing happens in BOTH `sub_9624D0` (line 1358--1361) and `sub_12CC750` (line 2025--2079). The condition is:

```text
if (!compare(lsa_opt_flag, "0") || fc_level == 2):
  append("-lsa-opt=0")
  append("-memory-space-opt=0")
```

Additionally, when `fc_level == 2` AND lsa_opt is NOT already "0", the libnvvm path also injects `-lsa-opt=0`, `mem2reg`, `-memory-space-opt=0`.

The minimal pass sequence:

| # | Factory | Pass |
|---|---------|------|
| 1 | `sub_18B3080(1)` | Sinking2Pass (fast mode, flag=1) |
| 2 | `sub_1857160()` | SimplifyCFG |
| 3 | `sub_19CE990()` | LoopStrengthReduce (if applicable) |
| 4 | `sub_1B26330()` | NVVMReflect |
| 5 | `sub_12D4560()` | NVVMVerify |
| 6 | `sub_184CD60()` | LICM |
| 7 | `sub_1C4B6F0()` | LowerSwitch |
| 8 | `sub_12D4560()` | NVVMVerify |

## Ofcmid Pipeline (Medium)

Ofcmid runs ~25--30 passes without forcing LSA or MemorySpaceOpt off. The pass sequence from `sub_12E54A0` (lines 814--861):

| # | Factory | Pass | Guard |
|---|---------|------|-------|
| 1 | `sub_184CD60()` | LICM | `!opts[1960]` |
| 2 | `sub_1CB4E40(0)` | AnnotationCleanup | always |
| 3 | `sub_1B26330()` | NVVMReflect | always |
| 4 | `sub_198E2A0()` | CorrelatedValuePropagation | always |
| 5 | `sub_1CEF8F0()` | NVVMPeephole | always |
| 6 | `sub_215D9D0()` | NVVMPeephole2/TcgenAnnotation | always |
| 7 | `sub_17060B0(1,0)` | GVN | `!opts[1080]` |
| 8 | `sub_198DF00(-1)` | JumpThreading/CVP | always |
| 9 | `sub_17060B0(1,0)` | GVN | `!opts[1080]` |
| 10 | `sub_1C6E800()` | NVVMLowerArgs | always |
| 11 | `sub_1832270(1)` | LoopSimplify | always |
| 12 | `sub_1A62BF0(5,0,0,1,0,0,1)` | CGSCC (5 iterations) | always |
| 13 | `sub_1CB4E40(0)` | AnnotationCleanup | always |
| 14 | `sub_18FD350(0)` | DCE | always |
| 15 | `sub_1841180()` | LCSSA | always |
| 16 | `sub_18DEFF0()` | Sink | always |
| 17 | `sub_17060B0(1,0)` | GVN | always |
| 18 | `sub_184CD60()` | LICM | always |
| 19 | `sub_195E880(0)` | Reassociate | always |
| 20 | `sub_190BB10(0,0)` | EarlyCSE | always |
| 21 | `sub_19B73C0(3,-1,-1,0,0,-1,0)` | LoopVectorize (conservative) | always |
| 22 | `sub_1A223D0()` | DSE | always |
| 23 | `sub_1C98160(0)` | MemorySpaceOpt | always |
| 24 | `sub_1C8E680(0)` | MemorySpaceOpt2 | always |
| 25 | `sub_1B7FDF0(3)` | BranchFolding/CFGSimplify | always |
| 26 | `sub_18B1DE0()` | Sinking2 | always |

Key differences from the O1+ pipeline: Ofcmid uses 5-iteration CGSCC (vs 1 at O1+), includes NVVMPeephole/Peephole2 early, uses conservative LoopVectorize parameters `(3,-1,-1,0,0,-1,0)` with some thresholds zeroed, and skips NVVMDivergenceLowering, SpeculativeExecution, NVVMBranchDist, NVVMRematerialization, and the entire tier sub-pipeline.

## Ofcmin Pipeline (Closest to Full Optimization)

Ofcmin takes the same path as Ofcmid through `LABEL_297` in `sub_12E54A0` but with the `v238` flag set differently, enabling more aggressive settings. The pipeline is essentially the Ofcmid sequence with:

- More aggressive loop optimizer thresholds
- Additional CGSCC framework passes
- Closer parameter alignment to the O2 full pipeline

Ofcmin does NOT force `-lsa-opt=0` or `-memory-space-opt=0`. Like Ofcmid, it still skips the tier 1/2/3 sub-pipeline entirely, keeping compile time lower than O1.

## Post-Optimization Common Tail

Regardless of pipeline tier, `sub_12E54A0` always appends at `LABEL_84` (lines 640--653):

| # | Factory | Pass | Guard |
|---|---------|------|-------|
| 1 | `sub_1C98160(opts[2920]!=0)` | MemorySpaceOpt | `!v244 && opts[3488]` |
| 2 | `sub_1CEBD10()` | NVVMFinal / cleanup | always |
| 3 | `sub_1654860(1)` | VerifierPass | `!opts[2800] && !opts[4464]` |
| 4 | `sub_12DFE00(PM, v253, opts)` | Codegen pass dispatch | always |

`sub_12DFE00` (codegen dispatch) reads the optimization level from `opts[200]` to determine codegen aggressiveness. When `opts[200] > 1`, full dependency tracking is enabled across all codegen passes.

## Always-Added Analysis Passes

Before any optimization, the pipeline assembler inserts (lines 396--420):

| # | Factory | Pass |
|---|---------|------|
| 1 | `sub_149CCE0` (368 bytes alloc) | TargetLibraryInfoWrapperPass |
| 2 | `sub_1BFB520` (208 bytes alloc) | TargetTransformInfoWrapperPass |
| 3 | `sub_14A7550()` | VerifierPass / BasicAliasAnalysis |
| 4 | `sub_1361950()` | AssumptionCacheTracker |
| 5 | `sub_1CB0F50()` | ProfileSummaryInfoWrapperPass |

These five passes run at ALL optimization levels including O0.

## NVVMPassOptions Offset-to-Guard Map

The passes gated by NVVMPassOptions boolean flags (opts struct at 4512 bytes). Slot defaults from `sub_12D6300`:

| Offset | Slot | Default | Controls | Used By |
|--------|------|---------|----------|---------|
| 280 | 15 | off | DCE disable | Tier 0 #37, Tier 1/2/3 #60 |
| 320 | 17 | off | TailCallElim disable | Tier 1/2/3 #23 (O3 only) |
| 360 | 19 | **on** | NVVMLateOpt disable | Tier 1/2/3 #75 (O3 only) |
| 440 | 23 | off | Inliner flag A disable | Tier 1/2/3 #29 |
| 480 | 25 | **on** | Inliner flag B disable | Tier 1/2/3 #29 |
| 600 | 31 | off | NVVMVerifier disable | Tier 1/2/3 #7,13,18,26,32,37 |
| 680 | 35 | off | FunctionAttrs disable | Tier 1/2/3 #74,76 |
| 720 | 37 | off | SCCP disable | Tier 1/2/3 #11 |
| 760 | 39 | off | DSE disable | Tier 1/2/3 #59 |
| 880 | 45 | off | NVVMReflect disable | Tier 1/2/3 #9,73 |
| 920 | 47 | off | IPConstPropagation disable | Tier 1/2/3 #8 |
| 960 | 49 | off | SimplifyCFG disable | Tier 1/2/3 #52 |
| 1000 | 51 | off | InstCombine disable | Tier 1/2/3 #42 |
| 1040 | 53 | off | Sink/SimplifyCFG disable | Tier 0 #13, Tier 1/2/3 #16,22 |
| 1080 | 55 | off | PrintModulePass disable | many |
| 1120 | 57 | off | NVVMPredicateOpt disable | Tier 1/2/3 #10 |
| 1160 | 59 | off | LoopIndexSplit disable | Tier 1/2/3 #17 |
| 1200 | 61 | off | SimplifyCFG tier guard | Tier 1/2/3 #16 |
| 1240 | 63 | off | LICM disable | Tier 1/2/3 #20,38,54 |
| 1280 | 65 | off | Reassociate disable | Tier 1/2/3 #84 |
| 1320 | 65 | off | NVVMDivergenceLow disable | Tier 0 #24, Tier 1/2/3 #35 |
| 1360 | 67 | off | LoopUnroll disable | Tier 0 #26, Tier 1/2/3 #40 |
| 1400 | 69 | off | SROA disable | Tier 1/2/3 #44,56 |
| 1440 | 71 | off | EarlyCSE disable | Tier 1/2/3 #43 |
| 1480 | 73 | off | ADCE extra guard | Tier 1/2/3 #35 |
| 1520 | 75 | off | LoopSimplify disable | Tier 1/2/3 #34 |
| 1640 | 81 | off | NVVMWarpShuffle disable | Tier 1/2/3 #79 |
| 1760 | 87 | off | MemorySpaceOpt disable | Common tail, language paths |
| 1840 | 91 | off | ADCE variant disable | Tier 1/2/3 #71 |
| 1960 | 97 | off | ConstantMerge disable | Tier 1/2/3 #15 |
| 2000 | 101 | off | NVVMIntrinsicLowering disable | Tier 1/2/3 #1,3,28,50,64 |
| 2080 | 103 | off | NVVMBranchDist disable A | Tier 1/2/3 #78,82 |
| 2120 | 105 | off | NVVMBranchDist disable B | Tier 1/2/3 #78,82 |
| 2200 | 109 | off | GenericToNVVM disable | Tier 1/2/3 #33 |
| 2240 | 111 | off | NVVMLowerAlloca A disable | Tier 1/2/3 #77 |
| 2280 | 113 | off | NVVMLowerAlloca B disable | Tier 1/2/3 #77 |
| 2320 | 115 | off | NVVMRematerialization disable | Tier 1/2/3 #55 |
| 2360 | 117 | **on** | NVVMUnreachableBlockElim disable | Tier 1/2/3 #24 |
| 2400 | 119 | off | NVVMReduction disable | Tier 1/2/3 #80 |
| 2440 | 121 | off | NVVMSinking2 disable | Tier 1/2/3 #81 |
| 2560 | 127 | off | NVVMGenericAddrOpt disable | Tier 1/2/3 #68 |
| 2600 | 129 | off | NVVMIRVerification disable | Tier 1/2/3 #2,27,49,63,66 |
| 2640 | 131 | off | LoopOpt/BarrierOpt disable | Tier 1/2/3 #72 |
| 2680 | 133 | off | MemorySpaceOpt (2nd) disable | Tier 1/2/3 #65 |
| 2720 | 135 | off | InstructionSimplify disable | Tier 1/2/3 #31 |
| 2760 | 137 | off | LoopVectorize 2nd disable | Tier 1/2/3 #47 |
| 2840 | 141 | **on** | ADCE enable (reversed) | Tier 1/2/3 #71 |
| 2880 | 143 | **on** | LICM enable (reversed) | Tier 0 #25, Tier 1/2/3 #20,38,54 |
| 2920 | 145 | off | LowerBarriers parameter | Common tail |
| 3000 | 151 | **on** | Early pass guard | Pre-opt phase |
| 3040 | 153 | off | CorrelatedValueProp enable | Tier 1/2/3 #25,58 |
| 3080 | 155 | **on** | NVIDIA loop pass enable | Tier 1/2/3 #53 |
| 3120 | 155 | **on** | MemorySpaceOpt(2nd) enable | Tier 1/2/3 #65,66 |
| 3160 | 157 | **on** | PrintModulePass enable | Tier 0 #9,27,35; Tier 1/2/3 many |
| 3200 | 159 | **on** | Advanced NVIDIA passes group | Tier 1/2/3 #8-11,14-15,72-76 |
| 3328 | 165 | **on** | SM-specific late passes block | Tier 1/2/3 #79-84 |
| 3488 | 173 | off | NVVMBarrierAnalysis enable | Tier 1/2/3 #4,5,39,69 |
| 3528 | 175 | off | Tier 1 enable | Pipeline assembler |
| 3568 | 177 | off | Tier 2 enable | Pipeline assembler |
| 3608 | 179 | off | Tier 3 enable | Pipeline assembler |
| 3648 | 181 | "" | Language/fc-level string ptr | Pipeline name selection |
| 3704 | 183 | off | Late optimization flag | Tier 1/2/3 #20,21; Pipeline B |
| 3904 | 192 | off | Debug/naming mode flag | BB naming loop |
| 4064 | 201 | off | Concurrent compilation flag | Thread count decision |
| 4104 | 203 | -1 | Thread count (integer) | `sub_12E7E70` |
| 4224 | 209 | off | Tier 0 enable (opt active) | Pipeline assembler loop |
| 4304 | 213 | off | Device-code / additional opt | Pipeline B; fc dispatch |
| 4384 | 217 | off | Fast-compile bypass flag | Pipeline A vs B branch |
| 4464 | 221 | off | Late CFG cleanup guard | Common tail #3 |

## Codegen Optimization Level Propagation

The `-optO` and `-llcO` flags propagate the optimization level to the backend code generator. In `sub_12E54A0` (lines 1451--1460):

```text
if (lsa_opt == "0" && some_flag == "1"):
  append("-optO<level>")
  append("-llcO2")
```

The codegen dispatch `sub_12DFE00` reads `opts[200]` (the integer optimization level):
- `opts[200] == 0`: Minimal codegen (no dependency tracking)
- `opts[200] >= 1`: Standard codegen
- `opts[200] >= 2`: Full dependency tracking enabled (`v121 = true`)

## Cross-References

- [NVVMPassOptions System](nvvm-pass-options.md) -- complete 221-slot, 4,512-byte struct layout
- [Pipeline Pass Registration](../llvm/pipeline.md) -- 526-pass registration table
- [Optimizer Architecture](../pipeline/optimizer.md) -- two-phase model, AddPass mechanism
- [CLI Flags](cli-flags.md) -- `-O#`, `-Ofc=`, `--passes=` routing
- [Knobs Reference](knobs.md) -- all 1496 cl::opt knobs
- [Concurrent Compilation](../infra/concurrent-compilation.md) -- Phase I/II threading model
