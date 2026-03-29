# LLVM Optimizer

Pass pipeline assembly, two-phase compilation, NVVMPassOptions knob system, and New PM integration. Address range `0x12D0000`–`0x16FFFFF` (~4.2 MB of code).

| | |
|---|---|
| **Pipeline assembler** | `sub_12E54A0` (49.8KB, 1,553 lines, ~150 pass insertions) |
| **Phase orchestrator** | `sub_12E7E70` (9.4KB, Phase I / Phase II) |
| **Concurrent entry** | `sub_12E1EF0` (51.3KB, jobserver + split-module + thread pool) |
| **PassOptions init** | `sub_12D6300` (125KB, 4,786 lines, 221 option slots) |
| **New PM registration** | `sub_2342890` (2,816 lines, 35 NVIDIA + ~350 LLVM passes) |
| **Target creation** | `sub_12EA530` (4.1KB, `"nvptx"` / `"nvptx64"`) |
| **AddPass** | `sub_12DE0B0` (3.5KB, hash-table-based pass insertion) |
| **Tier 0 sub-pipeline** | `sub_12DE330` (4.8KB, ~40 passes) |
| **Tier 1/2/3 sub-pipeline** | `sub_12DE8F0` (17.9KB, phase-conditional) |
| **Codegen dispatch** | `sub_12DFE00` (20.7KB) |
| **LTO pipeline** | `sub_12F5F30` (37.8KB, dead kernel elimination) |
| **jemalloc** | 5.3.x statically linked (~400 functions at `0x12FC000`) |

## Architecture

```
sub_12E1EF0 (51KB, concurrent compilation entry)
  │
  ├─ GNU Jobserver init (sub_16832F0, --jobserver-auth=R,W from MAKEFLAGS)
  ├─ Bitcode reading + verification (sub_153BF40)
  ├─ Function sorting by priority (sub_12E0CA0)
  ├─ Thread pool creation (sub_16D4AB0, min(requested, num_functions) threads)
  │
  └─ sub_12E7E70 (9.4KB, two-phase orchestrator)
       │
       ├─ Phase I: qword_4FBB3B0 = 1
       │    └─ sub_12E54A0 (whole-module analysis + early optimization)
       │
       ├─ Concurrency check: sub_12D4250 (>1 defined function?)
       │    ├─ Yes, threads>1 → per-function Phase II via thread pool
       │    │    └─ sub_12E86C0 per function (qword_4FBB3B0 = 2)
       │    └─ No → sequential Phase II
       │         └─ sub_12E54A0 (qword_4FBB3B0 = 2)
       │
       └─ qword_4FBB3B0 = 3 (done)

sub_12E54A0 (49.8KB, MASTER PIPELINE ASSEMBLY)
  │
  ├─ Top branch: a4[4384] → Pipeline B (fast/codegen-only)
  │                    else → Pipeline A (normal LLVM)
  │
  ├─ Target machine setup
  │    ├─ Triple: "nvptx64" or "nvptx" (based on pointer size)
  │    ├─ sub_16D3AC0 → TargetRegistry::lookupTarget()
  │    ├─ TargetOptions: PIC=1, CodeModel=8, OptLevel=1, ThreadModel=1
  │    └─ DataLayout from qword_4FBB430
  │
  ├─ Phase 0: Infrastructure (TLI, TTI, Verifier, AssumptionCache, ProfileSummary)
  ├─ Phase 1: Language dispatch (a4[3648]: "ptx"/"mid"/default)
  ├─ Phase 2: Pre-optimization passes
  ├─ Phase 3: Main optimization loop (tier threshold dispatch)
  ├─ Phase 4: Post-opt language-specific pipelines
  ├─ Phase 5: Finalization (NVVMLowerBarriers, BreakCriticalEdges, codegen)
  ├─ Phase 6: Phase 2 codegen check (qword_4FBB3B0 == 2)
  ├─ Phase 7: PassManager::run
  └─ Phase 8: Basic block naming ("F%d_B%d" for debug)
```

## Two-Phase Compilation — `sub_12E7E70`

| Field | Value |
|---|---|
| Address | `0x12E7E70` |
| Size | 9.4KB |
| Strings | `"Phase I"`, `"Phase II"`, `"Concurrent=Yes/No"` |

Both phases call the **same** `sub_12E54A0`. The difference: `qword_4FBB3B0` (TLS variable) is set to 1 or 2 before each call. Individual passes read this to decide whether to run.

```
Phase State Machine:

  START → [phase=1] → sub_12E54A0 (Phase I)
    │
    error? → RETURN
    │
    count_functions()
    ├─ 1 func → [phase=2] → sub_12E54A0 → [phase=3] → DONE
    ├─ N funcs, threads>1 → per-function Phase II (thread pool) → [phase=3] → DONE
    └─ N funcs, threads≤1 → [phase=2] → sub_12E54A0 → [phase=3] → DONE
```

Single-function modules skip the phase mechanism entirely — a single unphased call to `sub_12E54A0`.

### GNU Jobserver Integration

In `sub_12E1EF0` (lines 833–866), when `a4+3288` is set:

```c
v184 = sub_16832F0(&state, 0);   // parse MAKEFLAGS for --jobserver-auth=R,W
if (v184 == 5 || v184 == 6)      // pipe issues
    warning("jobserver pipe problem");
elif (v184 != 0)
    fatal("GNU Jobserver support requested, but an error occurred");
```

`sub_16832F0` allocates a 296-byte state structure, parses `MAKEFLAGS`, creates a pipe for token management, and spawns a pthread to manage tokens. Throttles concurrent per-function compilations to match the build's `-j` level.

### Split-Module Compilation

When optimization level (a4+4104) is negative, enters split-module mode:

1. Each function's bitcode is extracted via `sub_1AB9F40` with filter callback `sub_12D4BD0`
2. Module name: `"<split-module>"` (14 chars)
3. After thread pool completes, split modules are re-linked via `sub_12F5610`
4. Linkage attributes restored from hash table (external linkage types: bits 0–5, dso_local: bit 6 of byte+33)

## Pipeline Assembly — `sub_12E54A0`

### Language-Specific Paths

The pipeline branches based on `a4[3648]` (language string):

| String | Path | Pass Count | Key Difference |
|---|---|---|---|
| `"ptx"` | Path A | ~15 | Light: NVVMPeephole → LLVM standard → DCE → MemorySpaceOpt |
| `"mid"` | Path B | ~45 | Full: SROA → GVN → LICM → LoopIndexSplit → Remat → all NVIDIA passes |
| (default) | Path C | ~40 | General: 4 LLVM standard passes + NVIDIA interleaving |

### Tier System

The main loop iterates over entries at `a4[4488]` (16-byte stride: vtable + phase_id):

```c
if (opt_enabled && phase_id > opt_threshold) → sub_12DE330  // Tier 0 (full)
if (tier1_flag && phase_id > tier1_threshold) → sub_12DE8F0(1) // Tier 1
if (tier2_flag && phase_id > tier2_threshold) → sub_12DE8F0(2) // Tier 2
if (tier3_flag && phase_id > tier3_threshold) → sub_12DE8F0(3) // Tier 3
```

Each tier fires once (flag cleared after execution). Remaining tiers fire unconditionally after the loop.

### Tier 0 — Full Optimization (`sub_12DE330`)

~40 passes in order:

| # | Factory | Likely Pass | Guarded By |
|---|---|---|---|
| 1 | `sub_1654860(1)` | BreakCriticalEdges | — |
| 2 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | — |
| 3 | `sub_1B26330` | MemCpyOpt | — |
| 4 | `sub_185D600` | IPConstantPropagation | — |
| 5 | `sub_1C6E800` | GVN | — |
| 6 | `sub_1C6E560` | NewGVN/GVNHoist | — |
| 7 | `sub_1857160` | NVVMReflect | — |
| 8 | `sub_1842BC0` | SCCP | — |
| 9 | `sub_12D4560` | NVVMVerifier | — |
| 10 | `sub_18A3090` | NVVMPredicateOpt | — |
| 11 | `sub_184CD60` | ConstantMerge | — |
| 12 | `sub_1869C50(1,0,1)` | Sink/MemSSA | `!opts[1040]` |
| 13 | `sub_1833EB0(3)` | TailCallElim/JumpThreading | — |
| 14 | `sub_1952F90(-1)` | LoopIndexSplit | — |
| 15 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | — |
| 16 | `sub_1A223D0` | NVVMIRVerification | — |
| 17 | `sub_1A7A9F0` | InstructionSimplify | — |
| 18 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | — |
| 19 | `sub_1A02540` | GenericToNVVM | — |
| 20 | `sub_198DF00(-1)` | LoopSimplify | — |
| 21 | `sub_1C76260` | ADCE | `!opts[1320]` |
| 22 | `sub_195E880(0)` | LICM | `opts[2880]` |
| 23 | `sub_19C1680(0,1)` | LoopUnroll | `!opts[1360]` |
| 24 | `sub_19401A0` | InstCombine | — |
| 25 | `sub_1968390` | SROA | — |
| 26 | `sub_196A2B0` | EarlyCSE | — |
| 27 | `sub_19B73C0(2,...)` | LoopUnswitch | — |
| 28 | `sub_190BB10(0,0)` | SimplifyCFG | — |
| 29 | `sub_1A13320` | NVVMRematerialization | — |
| 30 | `sub_18F5480` | DSE | — |
| 31 | `sub_18DEFF0` | DCE | — |
| 32 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | — |
| 33 | `sub_18B1DE0` | NVVMLoopPass | — |
| 34 | `sub_1841180` | FunctionAttrs | — |

### "mid" Path — Complete Pass Ordering

The longest path (~45 passes), used for mid-level IR optimization:

ConstantMerge → NVVMIntrinsicLowering → MemCpyOpt → SROA → NVVMPeephole → NVVMAnnotations → LoopSimplify → GVN → NVVMIRVerification → SimplifyCFG → InstCombine → LLVM standard #5 → NVVMIntrinsicLowering → DeadArgElim → FunctionAttrs → DCE → ConstantMerge → LICM → NVVMLowerBarriers → MemorySpaceOpt → Reassociate → LLVM standard #8 → NVVMReflect → ADCE → InstructionSimplify → DeadArgElim → TailCallElim → DeadArgElim → CVP → Sink → SimplifyCFG → DSE → NVVMSinking2 → NVVMIRVerification → EarlyCSE → NVVMReflect → LLVM standard #8 → NVVMIntrinsicLowering → IPConstProp → LICM → NVVMIntrinsicLowering → NVVMBranchDist → NVVMRemat

## NVVMPassOptions — `sub_12D6300`

| Field | Value |
|---|---|
| Address | `0x12D6300` |
| Size | 125KB (4,786 lines) |
| Output struct | 4,512 bytes (allocated via `sub_22077B0(4512)`) |
| Slot count | 221 (indices 1–221) |
| Slot types | 114 string + 100 boolean + 6 integer + 1 string-pointer |

### Struct Layout

| Region | Offset | Content |
|---|---|---|
| Header | 0–7 | `int opt_level` (from `a2+112`) |
| Registry ptr | 8–15 | Pointer to PassOptionRegistry |
| Slot pairs | 16–4479 | 221 option slots (string/bool/int pairs) |
| Sentinel | 4480–4511 | 4 qwords zeroed |

### Option Slot Types

| Type | Size | Writer | Count |
|---|---|---|---|
| String | 24B | `sub_12D6090` | 114 |
| Bool (compact) | 16B | `sub_12D6100` | 83 |
| Bool (inline) | 16B | direct byte write | 17 |
| Integer | 16B | `sub_16D2BB0` (parseInt) | 6 |
| String pointer | 28B | direct qword write (slot 181 only) | 1 |

### Pair Organization

Slots are organized in pairs: **even** = string parameter, **odd** = boolean enable/disable toggle. Each "pass knob" has a string value and a do-X flag.

Exceptions: slots 160–162 (3 consecutive strings), slots 192–193 (2 consecutive bools), slot 181 (string pointer with char* + length).

### Defaults Enabled (14 of 100 booleans)

Slots: 19, 25, 93, 95, 117, 141, 143, 151, 155, 157, 159, 165, 211, 219. These are passes that run by default and must be explicitly disabled.

### Integer Defaults

| Slot | Default | Likely Purpose |
|---|---|---|
| 9 | 1 | Iteration count / threshold |
| 197 | 20 | Limit (e.g., unroll count) |
| 203 | -1 | Sentinel (unlimited/auto) |
| 205 | -1 | Sentinel |
| 207 | -1 | Sentinel |
| 215 | 0 | Disabled counter |

### Known Option Names

**Boolean toggles** (do-X / no-X):
`do-ip-msp`, `do-licm`, `do-remat`, `do-clone-for-ip-msp`, `do-cssa`, `do-scev-cgp`, `do-function-scev-cgp`, `do-scev-cgp-aggresively`, `do-base-address-strength-reduce`, `do-base-address-strength-reduce-chain`, `do-comdat-renaming`, `do-counter-promotion`, `do-lsr-64-bit`, `do-sign-ext-expand`, `do-sign-ext-simplify`

**Parametric knobs:**
`remat-for-occ`, `remat-gep-cost`, `remat-max-live-limit`, `remat-maxreg-ceiling`, `remat-move`, `remat-single-cost-limit`, `remat-use-limit`, `branch-dist-block-limit`, `branch-dist-func-limit`, `branch-dist-norm`, `scev-cgp-check-latency`, `scev-cgp-control`, `scev-cgp-cross-block-limit`, `scev-cgp-idom-level-limit`, `scev-cgp-inst-limit`, `scev-cgp-norm`, `cssa-coalesce`, `cssa-verbosity`, `base-address-strength-reduce-iv-limit`

**Dump flags:**
`dump-ip-msp`, `dump-remat`, `dump-branch-dist`, `dump-scev-cgp`, `dump-sink2`, `dump-before-cssa`, `dump-normalize-gep`, `dump-simplify-live-out`

## New PM Pass Registration — `sub_2342890`

2,816-line function registering every analysis, pass, and printer. Uses `sub_E41FB0(pm, class_name, len, pass_name, len)` for registration.

### NVIDIA Custom Passes (35 total)

**Module passes (12):** check-gep-index, check-kernel-functions, cnp-launch-check, ipmsp, nv-early-inliner, nv-inline-must, nvvm-pretreat, nvvm-verify, printf-lowering, select-kernels, lower-ops\*, set-global-array-alignment\*

**Function passes (20):** basic-dbe, branch-dist, byval-mem2reg, bypass-slow-division, normalize-gep, nvvm-reflect-pp, nvvm-peephole-optimizer, old-load-store-vectorizer, remat, propagate-alignment, reuse-local-memory, set-local-array-alignment, sinking2, d2ir-scalarizer, sink\<rp-aware\>, memory-space-opt\*, lower-aggr-copies\*, lower-struct-args\*, process-restrict\*

**Loop pass (1):** loop-index-split

**Analyses (2):** rpa (RegisterPressureAnalysis), merge-sets (MergeSetsAnalysis)

\* = parameterized

### Key Discoveries

- **nvvm-reflect-pp** is actually `SimplifyConstantConditionalsPass` — it runs *after* NVVMReflect to clean up dead branches from resolved `__nvvm_reflect()` calls
- **memory-space-opt** runs **twice** in the pipeline: `<first-time>` then `<second-time>`, with more information available on the second pass
- **d2ir-scalarizer** reuses LLVM's `ScalarizerPass` class under a different name
- Legacy PM has separate registrations with slightly different names (e.g., `"memory-space-opt-pass"` vs `"memory-space-opt"`)

## Key Global Variables

| Variable | Purpose |
|---|---|
| `qword_4FBB3B0` | Phase counter TLS: 1=Phase I, 2=Phase II, 3=done |
| `qword_4FBB370` | Feature flag register (value 6 = barrier opt + memspace opt) |
| `qword_4FBB410` | Tier execution tracker |
| `qword_4FBB430` | Optimization level store |
| `qword_4FBB510` | Debug/trace verbosity level |
| `byte_3F871B3` | NVIDIA global flag byte (empty/null string in .rodata) |
| `byte_4F99740` | CUTLASS optimization enable flag |
