# LLVM Optimizer

NVIDIA's LLVM optimizer in cicc v13.0 is not a straightforward invocation of the upstream LLVM `opt` pipeline. Instead, it implements a proprietary **two-phase compilation model** where the same 49.8KB pipeline assembly function (`sub_12E54A0`) is called twice with different phase counters, allowing analysis passes to run in Phase I and codegen-oriented passes in Phase II. Individual passes read a TLS variable (`qword_4FBB3B0`) to determine which phase is active and skip themselves accordingly.

The optimizer also supports **concurrent per-function compilation**: after Phase I completes on the whole module, Phase II can be parallelized across functions using a thread pool sized to `get_nprocs()` or a GNU Jobserver token count. This is a significant departure from upstream LLVM, which processes functions sequentially within a single pass manager invocation.

The entire optimization behavior is controlled by the **NVVMPassOptions** system — a 4,512-byte struct with 221 option slots (114 string + 100 boolean + 6 integer + 1 string-pointer) that provides per-pass enable/disable toggles and parametric knobs. This system is completely proprietary and has no upstream equivalent.

Address range `0x12D0000`–`0x16FFFFF` (~4.2 MB of code).

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
| **jemalloc** | 5.3.x statically linked (767 functions at `0x12FC000`) |

## Architecture

```
sub_12E1EF0 (10 KB native, concurrent compilation entry)
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

The two-phase model exists because certain optimization passes (e.g., inter-procedural memory space propagation, global inlining decisions) require whole-module visibility, while others (register pressure-driven rematerialization, instruction scheduling) operate per-function and benefit from parallelization. Phase I runs the whole-module analysis and early optimization passes; Phase II runs the per-function backend-oriented passes.

Both phases call the **same** `sub_12E54A0`. The difference: `qword_4FBB3B0` (TLS variable) is set to 1 or 2 before each call. Individual passes read this counter and skip themselves if the current phase doesn't match their intended execution phase. When the module contains only a single defined function, the phase mechanism is bypassed entirely — a single unphased call handles everything.

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

When cicc is invoked from a parallel `make -jN` build, it can participate in the GNU Jobserver protocol to limit its own thread count to the available parallelism tokens. This prevents oversubscription — without it, a `-j16` build could spawn 16 cicc processes each creating their own thread pool, resulting in hundreds of threads competing for CPU time. The jobserver reads the `--jobserver-auth=R,W` pipe file descriptors from the `MAKEFLAGS` environment variable.

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

Split-module compilation is NVIDIA's mechanism for the `-split-compile=N` flag. It decomposes a multi-function module into individual per-function bitcode blobs, compiles each independently (potentially in parallel), then re-links the results. This trades away inter-procedural optimization opportunities for compilation speed and reduced peak memory usage — a worthwhile tradeoff for large CUDA kernels during development iteration.

When optimization level (a4+4104) is negative, enters split-module mode:

1. Each function's bitcode is extracted via `sub_1AB9F40` with filter callback `sub_12D4BD0`
2. Module name: `"<split-module>"` (14 chars)
3. After thread pool completes, split modules are re-linked via `sub_12F5610`
4. Linkage attributes restored from hash table (external linkage types: bits 0–5, dso_local: bit 6 of byte+33)

## Pipeline Assembly — `sub_12E54A0`

The pipeline assembly function is the heart of the optimizer. At 49.8KB with ~150 `AddPass` calls, it constructs the complete LLVM pass pipeline at runtime rather than using a static pipeline description. The function first sets up target machine infrastructure (triple, data layout, subtarget features), then dispatches into one of three language-specific paths that determine which passes run and in what order. After the language-specific path completes, a shared finalization phase runs barriers, critical edge breaking, and codegen preparation.

A distinguishing feature of NVIDIA's pipeline is the **tier system**: passes are organized into Tiers 0–3, each gated by a threshold counter. As compilation progresses through the main loop (which iterates over external plugin/extension pass entries), tiers fire when the accumulated pass count exceeds their threshold. This allows NVIDIA to precisely control where in the pipeline their custom passes interleave with standard LLVM passes.

### Language-Specific Paths

The pipeline branches based on `a4[3648]` (language string). The three paths represent different optimization strategies for different IR maturity levels:

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

Tier 0 is the most aggressive optimization sub-pipeline. It runs ~40 passes in a carefully ordered sequence that interleaves standard LLVM passes with NVIDIA-specific ones. The ordering reveals NVIDIA's optimization strategy: start with GVN and SCCP for value simplification, then run NVIDIA's custom NVVMReflect and NVVMVerifier to clean up NVVM-specific constructs, followed by aggressive loop transformations (LoopIndexSplit, LoopUnroll, LoopUnswitch), and finally register-pressure-sensitive passes (Rematerialization, DSE, DCE) to prepare for codegen.

~40 passes in order:

> **Confidence note:** Pass identifications are based on diagnostic strings, factory signatures, and pipeline ordering. Most are HIGH confidence. Entries with `[MEDIUM confidence]` are inferred from code structure rather than direct string evidence.

| # | Factory | Likely Pass | Guarded By |
|---|---|---|---|
| 1 | `sub_1654860(1)` | BreakCriticalEdges | — |
| 2 | `sub_1A62BF0(1,...)` | LLVM standard pipeline #1 | — |
| 3 | `sub_1B26330` | MemCpyOpt | — |
| 4 | `sub_185D600` | IPConstantPropagation | — |
| 5 | `sub_1C6E800` | GVN | — |
| 6 | `sub_1C6E560` | NewGVN/GVNHoist `[MEDIUM confidence]` | — |
| 7 | `sub_1857160` | NVVMReflect | — |
| 8 | `sub_1842BC0` | SCCP | — |
| 9 | `sub_12D4560` | NVVMVerifier | — |
| 10 | `sub_18A3090` | NVVMPredicateOpt | — |
| 11 | `sub_184CD60` | ConstantMerge | — |
| 12 | `sub_1869C50(1,0,1)` | Sink/MemSSA `[MEDIUM confidence]` | `!opts[1040]` |
| 13 | `sub_1833EB0(3)` | TailCallElim/JumpThreading `[MEDIUM confidence]` | — |
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
| 33 | `sub_18B1DE0` | NVVMLoopPass `[MEDIUM confidence]` | — |
| 34 | `sub_1841180` | FunctionAttrs | — |

### "mid" Path — Complete Pass Ordering

The "mid" path is the primary optimization pipeline for standard CUDA compilation. At ~45 passes, it is the most comprehensive of the three paths. The key pattern is **repeated interleaving** of NVIDIA custom passes with standard LLVM passes: NVVMIntrinsicLowering runs 4 times at different points, NVVMReflect runs 3 times, and NVVMIRVerification runs after each major transformation to catch correctness regressions early. The MemorySpaceOpt pass appears once in this sequence (gated by `!opts[1760]`) — it runs again later via the parameterized `<second-time>` invocation in Tier 1/2/3.

ConstantMerge → NVVMIntrinsicLowering → MemCpyOpt → SROA → NVVMPeephole → NVVMAnnotations → LoopSimplify → GVN → NVVMIRVerification → SimplifyCFG → InstCombine → LLVM standard #5 → NVVMIntrinsicLowering → DeadArgElim → FunctionAttrs → DCE → ConstantMerge → LICM → NVVMLowerBarriers → MemorySpaceOpt → Reassociate → LLVM standard #8 → NVVMReflect → ADCE → InstructionSimplify → DeadArgElim → TailCallElim → DeadArgElim → CVP → Sink → SimplifyCFG → DSE → NVVMSinking2 → NVVMIRVerification → EarlyCSE → NVVMReflect → LLVM standard #8 → NVVMIntrinsicLowering → IPConstProp → LICM → NVVMIntrinsicLowering → NVVMBranchDist → NVVMRemat

## NVVMPassOptions — `sub_12D6300`

NVVMPassOptions is NVIDIA's proprietary mechanism for fine-grained control over every optimization pass. Unlike LLVM's `cl::opt` system (which uses global command-line options), NVVMPassOptions stores per-pass configuration in a flat struct that is allocated once and passed through the pipeline by pointer. This design avoids the global-state problems of `cl::opt` and allows different compilation units to have different pass configurations within the same process — critical for the concurrent per-function compilation model.

The 125KB initialization function is the largest in the optimizer range. Its size comes from the sheer number of option slots: each of the 221 slots requires a hash-table lookup, a default-value resolution, and a type-specific store, with most slots organized in pairs (a string parameter + a boolean enable flag).

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
| Integer | 16B | `sub_16D2BB0` (string-to-int64 parser) | 6 |
| String pointer | 28B | direct qword write (slot 181 only) | 1 |

### Pair Organization

Slots are organized in pairs: **even** = string parameter (the pass's configuration value or name), **odd** = boolean enable/disable toggle (the `do-X` flag). This consistent pairing means each "pass knob" has both a parametric value and an on/off switch, allowing passes to be individually disabled without removing their configuration — useful for A/B testing optimizations.

Exceptions to the pair pattern: slots 160–162 (3 consecutive strings — a pass with 3 string parameters), slots 192–193 (2 consecutive bools — a pair of binary flags), slot 181 (the only string-pointer type, storing a `char*` + length directly — likely a file path or regex pattern).

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

NVIDIA maintains both the Legacy Pass Manager and the New Pass Manager in cicc v13.0. The New PM registration lives in a single 2,816-line function that registers every analysis, pass, and printer by calling `sub_E41FB0(pm, class_name, len, pass_name, len)` for each. Standard LLVM passes use the `llvm::` prefix (stripped during registration), while NVIDIA custom passes use their own class names.

The registration function also handles **parameterized pass parsing**: when the pipeline text parser encounters a pass name with angle-bracket parameters (e.g., `memory-space-opt<first-time;warnings>`), it calls a registered parameter-parsing callback that returns a configured pass options struct. This is how MemorySpaceOpt can run twice with different configurations in the same pipeline.

### NVIDIA Custom Passes (35 total)

**Module passes (12):** check-gep-index, check-kernel-functions, cnp-launch-check, ipmsp, nv-early-inliner, nv-inline-must, nvvm-pretreat, nvvm-verify, printf-lowering, select-kernels, lower-ops\*, set-global-array-alignment\*

**Function passes (20):** basic-dbe, branch-dist, byval-mem2reg, bypass-slow-division, normalize-gep, nvvm-reflect-pp, nvvm-peephole-optimizer, old-load-store-vectorizer, remat, propagate-alignment, reuse-local-memory, set-local-array-alignment, sinking2, d2ir-scalarizer, sink\<rp-aware\>, memory-space-opt\*, lower-aggr-copies\*, lower-struct-args\*, process-restrict\*

**Loop pass (1):** loop-index-split

**Analyses (2):** rpa (RegisterPressureAnalysis), merge-sets (MergeSetsAnalysis)

\* = parameterized

### Key Discoveries

- **nvvm-reflect-pp** is actually `SimplifyConstantConditionalsPass`, not a reflection pass. It runs *after* NVVMReflect resolves `__nvvm_reflect()` calls to constants, cleaning up the resulting dead branches and unreachable code. The misleading name ("pp" = post-processing) obscures what is essentially a targeted dead-code-elimination pass.
- **memory-space-opt** runs **twice** in the pipeline with different parameterizations: `<first-time>` early in optimization (conservative, uses available alias information) and `<second-time>` late (aggressive, benefits from earlier optimizations having simplified the IR). This two-pass approach is necessary because address space resolution depends on pointer analysis quality, which improves as other passes simplify the code.
- **d2ir-scalarizer** reuses LLVM's `ScalarizerPass` class under a different name, suggesting NVIDIA added a custom registration point to control when scalarization happens in the NVPTX pipeline without modifying the upstream pass.
- **Legacy PM co-existence**: both Legacy PM and New PM registrations exist for the same passes, with slightly different names (e.g., `"memory-space-opt-pass"` vs `"memory-space-opt"`). This dual registration is necessary during the LLVM Legacy→New PM migration — cicc v13.0 appears to be in the middle of this transition.

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

## NVVMPassOptions Deep Dive

### Memory Layout

The 4,512-byte NVVMPassOptions struct is allocated on the heap via `sub_22077B0(4512)` at the start of each compilation. The layout divides into four regions:

```
Offset 0x000 [8B]  : int32 opt_level (from config+112) + 4B padding
Offset 0x008 [8B]  : qword ptr to PassOptionRegistry (hash table source)
Offset 0x010 [4464B]: 221 option slots (indices 1-221)
Offset 0x1180[32B] : 4 qwords zeroed (sentinel/trailer)
```

The slots start at offset 16 and are packed contiguously. Each slot occupies a fixed size depending on its type, but the stride varies: string options take 24 bytes, boolean options take 16 bytes, integer options take 16 bytes, and the single string-pointer option (slot 181) takes 28 bytes. The overall packing is not uniform-stride; the offset of each slot must be computed from the cumulative widths of all preceding slots.

### Slot Type Formats

Five distinct slot types exist, each written by a dedicated helper:

```c
// TYPE A: String option (114 instances)
// Written by sub_12D6090 (pass-option string writer)
struct StringSlot {        // 24 bytes
    char*   value_ptr;     // +0: pointer to string value
    int32_t option_index;  // +8: 1-based slot index
    int32_t flags;         // +12: from PassDef byte+40
    int32_t opt_level;     // +16: optimization level context
    int32_t pass_id;       // +20: resolved via sub_1691920
};

// TYPE B: Boolean compact (83 instances)
// Written by sub_12D6100 (pass-option boolean writer)
struct BoolCompactSlot {   // 16 bytes
    uint8_t value;         // +0: 0 or 1
    uint8_t pad[3];        // +1: padding
    int32_t option_index;  // +4
    int32_t flags;         // +8
    int32_t pass_id;       // +12
};

// TYPE C: Boolean inline (17 instances)
// Written directly as byte + int32 fields
struct BoolInlineSlot {    // 16 bytes
    uint8_t value;         // +0: 0 or 1
    uint8_t pad[3];        // +1
    int32_t option_index;  // +4: from sub_12D6240 return hi32
    int32_t opt_level;     // +8
    int32_t pass_id;       // +12: resolved inline
};

// TYPE D: Integer (6 instances)
// Value parsed by sub_16D2BB0 (string-to-int64 parser)
struct IntegerSlot {       // 16 bytes
    int32_t value;         // +0: parsed integer
    int32_t option_index;  // +4
    int32_t opt_level;     // +8
    int32_t pass_id;       // +12
};

// TYPE E: String pointer (1 instance, slot 181 only)
struct StringPtrSlot {     // 28 bytes
    char*   char_ptr;      // +0: raw string data pointer
    int64_t str_length;    // +8: length of string
    int32_t option_index;  // +16
    int32_t opt_level;     // +20
    int32_t pass_id;       // +24
};
```

### Helper Function Chain

The initialization function `sub_12D6300` populates the struct by iterating all 221 slot indices and calling a chain of helpers for each:

1. **`sub_12D6170` (pass-option registry lookup)** -- looks up a slot index in the hash table at `registry+120`. Returns a pointer to an `OptionNode` struct: `[+40] int16 flags`, `[+48] qword* value_array_ptr`, `[+56] int value_count`. Returns null if the option was not set on the command line.

2. **`sub_12D6240` (pass-option boolean resolver)** -- resolves a boolean option. Calls `sub_12D6170` to find the option, then if a string value exists, lowercases it via `sub_16D2060` and tests if the first char is `'1'` (0x31) or `'t'` (0x74). If the option was not found, defaults to true (enabled). Returns the boolean packed with the flags in the low 40 bits.

3. **`sub_1691920` (pass-definition table getter)** -- looks up a PassDef entry in a table where each entry is 64 bytes. Computes: `table[0] + (index - 1) * 64`. The PassDef at `[+32]` holds the pass_id, at `[+36]` a `has_overrides` flag, and at `[+40]` an override index.

### Initial Slots (1-6): Global Configuration

The first six slots are all string types at a uniform 24-byte stride, starting at offset 16. They do not follow the pair pattern and represent global pipeline parameters rather than per-pass knobs:

| Slot | Offset | Likely Content |
|------|--------|----------------|
| 1 | 16 | `ftz` (flush-to-zero mode string) |
| 2 | 40 | `prec-div` (precise division setting) |
| 3 | 64 | `prec-sqrt` (precise square root setting) |
| 4 | 88 | `fmad` (fused multiply-add policy) |
| 5 | 112 | `opt-level` (optimization level string) |
| 6 | 136 | `sm-arch` (target SM architecture string) |

### CLI Interface

Users interact with NVVMPassOptions via the `-opt` flag, which appends key=value pairs to the PassOptionRegistry before `sub_12D6300` flattens them:

```
cicc -opt "-do-ip-msp=0"            # disable memory space propagation
cicc -opt "-do-licm=0"              # disable LICM
cicc -opt "-remat-max-live-limit=50" # set rematerialization threshold
cicc -opt "-dump-remat"             # enable remat dump output
```

The registry is a hash table populated from these CLI strings. Each `-opt` argument is parsed into a key (the option name) and value (the string after `=`). When `sub_12D6300` runs, it queries the registry for each of the 221 slot indices. If a CLI override exists, it takes precedence; otherwise the compiled-in default is used.

### Option Anomalies

Several regions break the standard string/boolean pair pattern:

- **Slots 160-162**: Three consecutive string slots with no interleaved boolean. `[LOW confidence]` This represents a pass (likely MemorySpaceOpt or the CSSA pass) that takes three string configuration parameters followed by a single boolean enable flag at slot 163. The pass identity is uncertain because neither MemorySpaceOpt nor CSSA has been confirmed to consume three string parameters; the association is based on pipeline position proximity only.
- **Slots 192-193**: Two consecutive boolean slots. One is the main enable toggle; the other appears to be a sub-feature flag (both default to disabled).
- **Slot 181 (offset 3648)**: The only `STRING_PTR` type. Its default is `byte_3F871B3` (an empty string in `.rodata`). The raw pointer + length storage suggests this holds a file path or regex pattern for pass filtering.
- **Slots 196-207**: Alternating string + integer slots instead of string + boolean. `[LOW confidence]` This high-numbered region contains all six integer options, likely controlling late-pipeline passes with numeric thresholds (unroll counts, live-variable limits, iteration bounds). The specific pass-to-slot associations are unconfirmed; the "unroll counts, live-variable limits, iteration bounds" interpretation is based on typical LLVM integer-valued pass options, not direct evidence.

### Complete Slot-to-Offset Map with Known Consumers

The following table maps NVVMPassOptions slot indices to struct byte offsets, types, defaults, and -- where the cross-reference to the pipeline assembler's `a4[offset]` guards could be established -- the consuming pass(es). Offsets marked with `*` are confirmed by cross-referencing `a4[offset]` guards in `sub_12E54A0` and `sub_12DE8F0`.

| Slot | Offset | Type | Default | Known Knob Name | Consuming Pass |
|------|--------|------|---------|----------------|----------------|
| 1 | 16 | STRING | | `ftz` | Global: flush-to-zero mode |
| 2 | 40 | STRING | | `prec-div` | Global: precise division |
| 3 | 64 | STRING | | `prec-sqrt` | Global: precise sqrt |
| 4 | 88 | STRING | | `fmad` | Global: fused multiply-add |
| 5 | 112 | STRING | | `opt-level` | Global: optimization level |
| 6 | 136 | STRING | | `sm-arch` | Global: target SM architecture |
| 7 | 160 | BOOL | 0 | | |
| 8 | 176 | STRING | | | |
| 9 | 200* | INTEGER | 1 | | Opt level for `sub_12DFE00` codegen |
| 10 | 216 | STRING | | | |
| 11 | 240 | BOOL | 0 | | |
| 13 | 280* | BOOL | 0 | `no-dce` | `sub_18DEFF0` (DCE) |
| 15 | 320* | BOOL | 0 | `no-tailcallelim` | `sub_1833EB0` (TailCallElim) |
| 17 | 360* | BOOL | 0 | `no-late-opt` | `sub_1C46000` (NVVMLateOpt) |
| 19 | 400* | BOOL | **1** | `no-inline-a` | Inlining variant A |
| 21 | 440* | BOOL | 0 | `no-inline-b` | `sub_1C4B6F0` (AlwaysInliner) |
| 23 | 480* | BOOL | 0 | `no-inline-c` | `sub_1C4B6F0` in `sub_12DE8F0` |
| 25 | 520* | BOOL | **1** | | `sub_1AAC510` (NVIDIA pass A) |
| 27 | 560* | BOOL | 0 | | `sub_1AAC510` (NVIDIA pass B) |
| 29 | 600* | BOOL | 0 | `no-nvvm-verify` | `sub_12D4560` (NVVMVerifier) |
| 33 | 680* | BOOL | 0 | `no-func-attrs` | `sub_1841180` (FunctionAttrs) |
| 35 | 720* | BOOL | 0 | `no-sccp` | `sub_1842BC0` (SCCP) |
| 37 | 760* | BOOL | 0 | `no-dse` | `sub_18F5480` (DSE) |
| 43 | 880* | BOOL | 0 | `no-nvvm-reflect` | `sub_1857160` (NVVMReflect) |
| 45 | 920* | BOOL | 0 | `no-ipconst` | `sub_185D600` (IPConstProp) |
| 47 | 960* | BOOL | 0 | `no-simplifycfg` | `sub_190BB10` (SimplifyCFG) |
| 49 | 1000* | BOOL | 0 | `no-instcombine` | `sub_19401A0` (InstCombine) |
| 51 | 1040* | BOOL | 0 | `no-sink` | `sub_1869C50` (Sink/MemSSA) |
| 53 | 1080* | BOOL | 0 | `no-dump` | `sub_17060B0` (PrintModulePass) |
| 55 | 1120* | BOOL | 0 | `no-predopt` | `sub_18A3430` (NVVMPredicateOpt) |
| 57 | 1160* | BOOL | 0 | `no-loopindexsplit` | `sub_1952F90` (LoopIndexSplit) |
| 59 | 1200* | BOOL | 0 | `no-simplifycfg-b` | SimplifyCFG variant B |
| 61 | 1240* | BOOL | 0 | `do-licm` (inverted) | `sub_195E880` (LICM) |
| 63 | 1280* | BOOL | 0 | `no-reassoc` | `sub_1B7FDF0` (Reassociate) |
| 65 | 1320* | BOOL | 0 | `no-adce-a` | `sub_1C76260` (ADCE variant) |
| 67 | 1360* | BOOL | 0 | `no-loopunroll` | `sub_19C1680` (LoopUnroll) |
| 69 | 1400* | BOOL | 0 | `no-sroa` | `sub_1968390` (SROA) |
| 71 | 1440* | BOOL | 0 | `no-earlycse` | `sub_196A2B0` (EarlyCSE) |
| 73 | 1480* | BOOL | 0 | `no-adce-b` | ADCE variant B |
| 75 | 1520* | BOOL | 0 | `no-loopsimplify` | `sub_198DF00` (LoopSimplify) |
| 83 | 1680* | BOOL | 0 | | `sub_19CE990` (NVIDIA pass) |
| 87 | 1760* | BOOL | 0 | `do-ip-msp` (inverted) | `sub_1C8E680` (MemorySpaceOpt) |
| 91 | 1840* | BOOL | 0 | `no-adce-c` | `sub_1C6FCA0` (ADCE) |
| 93 | 1880 | BOOL | **1** | | NVVMReduction param A |
| 95 | 1920 | BOOL | **1** | | NVVMReduction param B |
| 97 | 1960* | BOOL | 0 | `no-constmerge` | `sub_184CD60` (ConstantMerge) |
| 99 | 2000* | BOOL | 0 | `no-intrin-lower` | `sub_1CB4E40` (NVVMIntrinsicLowering) |
| 101 | 2040* | BOOL | 0 | `no-memcpyopt` | `sub_1B26330` (MemCpyOpt) |
| 105 | 2120* | BOOL | 0 | `no-branchdist-b` | `sub_1CB73C0` (NVVMBranchDist B) |
| 109 | 2200* | BOOL | 0 | `no-generic2nvvm` | `sub_1A02540` (GenericToNVVM) |
| 113 | 2280* | BOOL | 0 | `no-loweralloca-b` | NVVMLowerAlloca B |
| 115 | 2320* | BOOL | 0 | `do-remat` (inverted) | `sub_1A13320` (NVVMRemat) |
| 117 | 2360 | BOOL | **1** | | `sub_1CC3990` (NVVMUnreachBlockElim) |
| 121 | 2440* | BOOL | 0 | `no-sinking2` | `sub_1CC60B0` (NVVMSinking2) |
| 127 | 2560* | BOOL | 0 | `no-genericaddropt` | `sub_1CC71E0` (NVVMGenericAddrOpt) |
| 129 | 2600* | BOOL | 0 | `no-irverify` | `sub_1A223D0` (NVVMIRVerification) |
| 131 | 2640* | BOOL | 0 | `no-loopopt` | `sub_18B1DE0` (NVVMLoopOpt) |
| 133 | 2680* | BOOL | 0 | `no-memspaceopt-b` | MemorySpaceOpt in `sub_12DE8F0` |
| 135 | 2720* | BOOL | 0 | `no-instsimplify` | `sub_1A7A9F0` (InstructionSimplify) |
| 141 | 2840* | BOOL | **1** | | Enable ADCE (`sub_1C6FCA0`, reversed) |
| 143 | 2880* | BOOL | **1** | `do-licm` | Enable LICM (reversed logic) |
| 149 | 3000* | BOOL | 0 | | Extra DeadArgElim trigger |
| 151 | 3040 | BOOL | **1** | | Enable CorrelatedValuePropagation |
| 155 | 3120* | BOOL | **1** | | Address space optimization flag |
| 157 | 3160* | BOOL | **1** | `dump-*` master | Debug dump mode (PrintModulePass) |
| 159 | 3200* | BOOL | **1** | | Enable advanced NVIDIA passes group |
| 165 | 3328* | BOOL | **1** | | Enable SM-specific warp/reduction/sinking |
| 173 | 3488* | BOOL | 0 | | Enable barrier optimization |
| 175 | 3528* | BOOL | 0 | | Tier 1 optimization enable |
| 177 | 3568* | BOOL | 0 | | Tier 2 optimization enable |
| 179 | 3608* | BOOL | 0 | | Tier 3 optimization enable |
| 181 | 3648* | STR_PTR | `""` | | Language string (`"ptx"/"mid"/"idn"`) |
| 183 | 3704* | BOOL | 0 | | Late optimization / address-space mode |
| 193 | 3904* | BOOL | 0 | | Debug: verify after each plugin pass |
| 195 | 3944* | BOOL | 0 | | Debug: rename BBs to `"F%d_B%d"` |
| 197 | 3984 | INTEGER | 20 | | Limit/threshold (e.g., unroll count) |
| 203 | 4104 | INTEGER | -1 | | Sentinel: unlimited/auto |
| 205 | 4144 | INTEGER | -1 | | Sentinel: unlimited/auto |
| 207 | 4184 | INTEGER | -1 | | Sentinel: unlimited/auto |
| 209 | 4224* | BOOL | 0 | | Master optimization switch |
| 211 | 4264 | BOOL | **1** | | |
| 213 | 4304* | BOOL | 0 | | Device-code / separate-compilation |
| 215 | 4344 | INTEGER | 0 | | Disabled counter |
| 217 | 4384* | BOOL | 0 | | Fast-compile / bypass LLVM pipeline |
| 219 | 4424 | BOOL | **1** | | |
| 221 | 4464* | BOOL | 0 | | Disable late CFG cleanup variant B |

Slots not listed have no confirmed cross-reference to pipeline assembler guards. The full 221-slot table is in the [NVVMPassOptions Reference](../config/nvvm-pass-options.md).

### Complete Option Name Inventory

The following option names were extracted from binary string references in `.rodata`. They are set via `-opt "-name=value"` on the cicc command line (requires NVVMCCWIZ=553282 in non-release builds).

**Boolean toggles (do-X / no-X):**

| Name | Effect |
|------|--------|
| `do-ip-msp` | Enable inter-procedural memory space propagation |
| `do-licm` | Enable LICM (loop-invariant code motion) |
| `do-remat` | Enable NVVMRematerialization |
| `do-clone-for-ip-msp` | Enable function cloning for IPMSP |
| `do-cssa` | Enable Conventional SSA construction |
| `do-scev-cgp` | Enable SCEV-based CodeGenPrepare |
| `do-function-scev-cgp` | Enable function-level SCEV-CGP |
| `do-scev-cgp-aggresively` | Aggressive SCEV-CGP mode [sic] |
| `do-base-address-strength-reduce` | Enable base address strength reduction |
| `do-base-address-strength-reduce-chain` | Enable chained base address SR |
| `do-comdat-renaming` | Enable COMDAT group renaming |
| `do-counter-promotion` | Enable counter promotion |
| `do-lsr-64-bit` | Enable 64-bit loop strength reduction |
| `do-sign-ext-expand` | Enable sign extension expansion |
| `do-sign-ext-simplify` | Enable sign extension simplification |

**Parametric knobs:**

| Name | Type | Default | Purpose |
|------|------|---------|---------|
| `remat-for-occ` | string | | Rematerialization occupancy target |
| `remat-gep-cost` | string | | GEP rematerialization cost |
| `remat-ignore-single-cost` | string | | Skip single-use cost analysis |
| `remat-lli-factor` | string | | Live-interval factor |
| `remat-load-param` | string | | Parameter load remat policy |
| `remat-loop-trip` | string | | Loop trip count for remat decisions |
| `remat-max-live-limit` | string | | Maximum live variable count |
| `remat-maxreg-ceiling` | string | | Register ceiling for remat |
| `remat-move` | string | | Rematerialization move policy |
| `remat-single-cost-limit` | string | | Single-value cost limit |
| `remat-use-limit` | string | | Use count limit for remat |
| `branch-dist-block-limit` | string | | Block count limit for branch distribution |
| `branch-dist-func-limit` | string | | Function-level branch dist limit |
| `branch-dist-norm` | string | | Normalization factor |
| `scev-cgp-check-latency` | string | | Latency check threshold |
| `scev-cgp-control` | string | | CGP control mode |
| `scev-cgp-cross-block-limit` | string | | Cross-block analysis limit |
| `scev-cgp-idom-level-limit` | string | | Immediate dominator depth limit |
| `scev-cgp-inst-limit` | string | | Instruction count limit |
| `scev-cgp-norm` | string | | Normalization factor |
| `scev-cgp-old-base` | string | | Legacy base address mode |
| `scev-cgp-tid-max-value` | string | | Thread ID maximum value |
| `base-address-strength-reduce-iv-limit` | string | | IV count limit for base addr SR |
| `base-address-strength-reduce-max-iv` | string | | Maximum IV for base addr SR |
| `cssa-coalesce` | string | | CSSA coalescing mode |
| `cssa-verbosity` | string | | CSSA debug verbosity |

**Dump/debug flags:**

| Name | Purpose |
|------|---------|
| `dump-ip-msp` | Dump IPMSP analysis results |
| `dump-ir-before-memory-space-opt` | Dump IR before MemorySpaceOpt |
| `dump-ir-after-memory-space-opt` | Dump IR after MemorySpaceOpt |
| `dump-memory-space-warnings` | Dump address space warnings |
| `dump-remat` | Dump rematerialization decisions |
| `dump-remat-add` | Dump remat additions |
| `dump-remat-iv` | Dump remat induction variables |
| `dump-remat-load` | Dump remat load decisions |
| `dump-branch-dist` | Dump branch distribution analysis |
| `dump-scev-cgp` | Dump SCEV-CGP analysis |
| `dump-base-address-strength-reduce` | Dump base address SR |
| `dump-sink2` | Dump Sinking2 pass output |
| `dump-before-cssa` | Dump IR before CSSA |
| `dump-phi-remove` | Dump PHI node removal |
| `dump-normalize-gep` | Dump GEP normalization |
| `dump-simplify-live-out` | Dump live-out simplification |
| `dump-process-restrict` | Dump restrict processing |
| `dump-process-builtin-assume` | Dump builtin assume processing |
| `dump-conv-dot` | Dump convergence as DOT graph |
| `dump-conv-func` | Dump convergence per function |
| `dump-conv-text` | Dump convergence as text |
| `dump-nvvmir` | Dump NVVM IR |
| `dump-va` | Dump value analysis |

## Tier-Based Pass Ordering

### The Threshold Dispatch Mechanism

NVIDIA's tier system is a priority-driven scheduling mechanism that interleaves optimization sub-pipelines with external plugin passes. The master pipeline function `sub_12E54A0` iterates over a pass registration array at `a4[4488]` (16-byte stride entries: `[+0] vtable_ptr`, `[+8] phase_id`). As it processes each entry, it checks whether the entry's `phase_id` exceeds a threshold. When it does, the corresponding tier sub-pipeline fires once:

```c
// Pseudocode for the main loop in sub_12E54A0
for (entry = a4[4488]; entry < a4[4496]; entry += 16) {
    int phase_id = *(int*)(entry + 8);

    if (opt_enabled && phase_id > opt_threshold) {
        sub_12DE330(PM, opts);      // Tier 0: full optimization
        opt_enabled = 0;            // fire once
    }
    if (tier1_flag && phase_id > tier1_threshold) {
        sub_12DE8F0(PM, 1, opts);   // Tier 1
        tier1_flag = 0;
    }
    if (tier2_flag && phase_id > tier2_threshold) {
        sub_12DE8F0(PM, 2, opts);   // Tier 2
        tier2_flag = 0;
    }
    if (tier3_flag && phase_id > tier3_threshold) {
        sub_12DE8F0(PM, 3, opts);   // Tier 3
        tier3_flag = 0;
    }

    // Insert the plugin/external pass itself
    pass = vtable_call(entry, +72);  // entry->createPass()
    AddPass(PM, pass, 1, 0);
}

// Any tier that didn't fire during the loop fires now
if (opt_enabled)  sub_12DE330(PM, opts);
if (tier1_flag)   sub_12DE8F0(PM, 1, opts);
if (tier2_flag)   sub_12DE8F0(PM, 2, opts);
if (tier3_flag)   sub_12DE8F0(PM, 3, opts);
```

This design means tier placement is data-driven: the thresholds stored at config offsets 4224/4228 (Tier 0), 3528/3532 (Tier 1), 3568/3572 (Tier 2), and 3608/3612 (Tier 3) determine exactly where in the plugin pass sequence each tier's sub-pipeline gets inserted. Changing the threshold shifts an entire tier of ~40 passes to a different position relative to the external passes. After each tier fires, its flag is cleared so it cannot fire again.

### Tier 0 Ordering Strategy

Tier 0 (`sub_12DE330`) is the most comprehensive sub-pipeline at ~40 passes. Its ordering reflects NVIDIA's optimization philosophy for GPU code:

**Phase A -- Value Simplification** (passes 1-8): BreakCriticalEdges normalizes the CFG, then the CGSCC inliner framework runs first to create optimization opportunities. NVVMReflect resolves `__nvvm_reflect()` calls to compile-time constants (GPU architecture queries), and SCCP propagates those constants. GVN and NewGVN/GVNHoist eliminate redundant computations.

**Phase B -- NVIDIA-Specific Cleanup** (passes 9-12): NVVMVerifier catches NVVM-specific IR errors early. NVVMPredicateOpt optimizes predicate expressions. ConstantMerge reduces module size.

**Phase C -- Loop Transformations** (passes 13-27): This is the core loop optimization sequence. Sink/MemSSA moves code out of hot paths. LoopIndexSplit divides loops at index boundaries. LICM hoists invariants. LoopUnroll with factor 3 expands small loops. LoopUnswitch moves conditionals out of loops. ADCE removes dead code exposed by loop transformations.

**Phase D -- Register Pressure Management** (passes 28-40): InstCombine and SROA simplify the IR further. NVVMRematerialization recomputes values to reduce register pressure -- critical for GPU occupancy. DSE and DCE clean up dead stores and code. The final CGSCC pass and FunctionAttrs prepare for per-function Phase II processing.

### Tier 1/2/3 Incremental Additions -- `sub_12DE8F0`

| | |
|---|---|
| **Address** | `0x12DE8F0` |
| **Size** | 17,904 bytes |
| **Signature** | `int64 sub_12DE8F0(int64 passMgr, int tier, int64 opts)` |

`sub_12DE8F0` adds passes incrementally based on the tier value (1, 2, or 3). Its first action stores the tier into `qword_4FBB410` (the tier tracker global), then checks `qword_4FBB3B0` (phase counter) for phase-dependent behavior. Nearly every pass insertion is gated by a boolean in the NVVMPassOptions struct.

The full pass list for `sub_12DE8F0` (all tiers combined, with tier-specific gates):

```
sub_1CB4E40(1) [!opts[2000]]            NVVMIntrinsicLowering (level=1)
sub_1A223D0()  [!opts[2600]]            NVVMIRVerification
sub_1CB4E40(1) [!opts[2000]]            NVVMIntrinsicLowering (barrier=1)
sub_18E4A00()  [opts[3488]]             NVVMBarrierAnalysis
sub_1C98160(0) [opts[3488]]             NVVMLowerBarriers
sub_12D4560()  [!opts[600]]             NVVMVerifier
sub_185D600()  [opts[3200]&&!opts[920]] IPConstPropagation         [advanced group]
sub_1857160()  [opts[3200]&&!opts[880]] NVVMReflect                [advanced group]
sub_18A3430()  [opts[3200]&&!opts[1120]] NVVMPredicateOpt          [advanced group]
sub_1842BC0()  [opts[3200]&&!opts[720]] SCCP                       [advanced group]
sub_12D4560()  [!opts[600]]             NVVMVerifier
sub_18A3090()  [opts[3200]&&!opts[2160]] NVVMPredicateOpt variant  [advanced group]
sub_184CD60()  [opts[3200]&&!opts[1960]] ConstantMerge             [advanced group]
sub_190BB10(1,0)[tier!=1 && guards]     SimplifyCFG                [TIER 2/3 ONLY]
sub_1952F90(-1)[tier!=1 && guards]      LoopIndexSplit             [TIER 2/3 ONLY]
sub_12D4560()  [tier!=1 && !opts[600]]  NVVMVerifier               [TIER 2/3 ONLY]
sub_195E880(0) [opts[3704]&&opts[2880]] LICM
sub_1C8A4D0(v) [v=1 if opts[3704]]     EarlyCSE
sub_1869C50(1,0,1)[tier!=1&&!opts[1040]] Sink                     [TIER 2/3 ONLY]
sub_1833EB0(3) [tier==3 && !opts[320]]  TailCallElim              [TIER 3 ONLY]
sub_1CC3990()  [!opts[2360]]            NVVMUnreachableBlockElim
sub_18EEA90()  [opts[3040]]             CorrelatedValuePropagation
sub_12D4560()  [!opts[600]]             NVVMVerifier
sub_1A223D0()  [!opts[2600]]            NVVMIRVerification
sub_1CB4E40(1) [!opts[2000]]            NVVMIntrinsicLowering
sub_1C4B6F0()  [!opts[440]&&!opts[480]] Inliner
sub_1A7A9F0()  [!opts[2720]]            InstructionSimplify
sub_12D4560()  [!opts[600]]             NVVMVerifier
sub_1A02540()  [!opts[2200]]            GenericToNVVM
sub_198DF00(-1)[!opts[1520]]            LoopSimplify
sub_1C76260()  [!opts[1320]&&!opts[1480]] ADCE
sub_195E880(0) [opts[2880]&&!opts[1240]] LICM
sub_1C98160(v) [opts[3488]]             NVVMLowerBarriers
sub_19C1680(0,1)[!opts[1360]]           LoopUnroll
sub_19401A0()  [!opts[1000]]            InstCombine
sub_196A2B0()  [!opts[1440]]            EarlyCSE
sub_1968390()  [!opts[1400]]            SROA
sub_19B73C0(t,...)[tier!=1]             LoopUnswitch (SM-dependent) [TIER 2/3 ONLY]
sub_1A62BF0(1,...)[!opts[600]]          LLVM standard pipeline #1
sub_1A223D0()  [!opts[2600]]            NVVMIRVerification
sub_1CB4E40(1) [!opts[2000]]            NVVMIntrinsicLowering
sub_190BB10(0,0)[!opts[960]]            SimplifyCFG
sub_1922F90()  [opts[3080]]             NVIDIA-specific loop pass
sub_195E880(0) [opts[2880]&&!opts[1240]] LICM
sub_1A13320()  [!opts[2320]]            NVVMRematerialization
sub_1968390()  [!opts[1400]]            SROA
sub_18EEA90()  [opts[3040]]             CorrelatedValuePropagation
sub_18F5480()  [!opts[760]]             DSE
sub_18DEFF0()  [!opts[280]]             DCE
sub_1A62BF0(1,...)[!opts[600]]          LLVM standard pipeline #1
sub_1AAC510()  [!opts[520]&&!opts[560]] NVIDIA-specific pass
sub_1A223D0()  [!opts[2600]]            NVVMIRVerification
sub_1CB4E40(1) [!opts[2000]]            NVVMIntrinsicLowering
sub_1C8E680()  [!opts[2680]]            MemorySpaceOpt (from opts[3120])
sub_1CC71E0()  [!opts[2560]]            NVVMGenericAddrOpt
sub_1C98270(1,v)[opts[3488]]            NVVMLowerBarriers variant
sub_1C6FCA0()  [opts[2840]&&!opts[1840]] ADCE
sub_18B1DE0()  [opts[3200]&&!opts[2640]] LoopOpt/BarrierOpt        [advanced group]
sub_1857160()  [opts[3200]&&tier==3]    NVVMReflect                [TIER 3 ONLY]
sub_1841180()  [opts[3200]&&!opts[680]] FunctionAttrs              [advanced group]
sub_1C46000()  [tier==3&&!opts[360]]    NVVMLateOpt                [TIER 3 ONLY]
sub_1841180()  [opts[3200]&&!opts[680]] FunctionAttrs (2nd call)   [advanced group]
sub_1CBC480()  [!opts[2240]&&!opts[2280]] NVVMLowerAlloca
sub_1CB73C0()  [!opts[2080]&&!opts[2120]] NVVMBranchDist
sub_1C7F370(1) [opts[3328]&&!opts[1640]] NVVMWarpShuffle           [SM-specific]
sub_1CC5E00()  [opts[3328]&&!opts[2400]] NVVMReduction             [SM-specific]
sub_1CC60B0()  [opts[3328]&&!opts[2440]] NVVMSinking2              [SM-specific]
sub_1CB73C0()  [opts[3328]&&guards]     BranchDist (2nd call)      [SM-specific]
sub_1B7FDF0(3) [opts[3328]&&!opts[1280]] Reassociate               [SM-specific]
```

**Tier 1 (baseline)** adds the passes above EXCEPT those gated by `tier!=1`: SimplifyCFG, LoopIndexSplit, Sink, and LoopUnswitch are all skipped. This is a conservative set focused on NVIDIA-specific cleanup without expensive LLVM optimization.

**Tier 2** adds everything Tier 1 has plus the `tier!=1`-gated passes. The LoopUnswitch parameters are SM-architecture-dependent: `sub_19B73C0` receives different vector widths based on the target subtarget.

**Tier 3** adds TailCallElim (gated `tier==3`), NVVMReflect at a late position (gated `tier==3`), and NVVMLateOpt (gated `tier==3`). Critically, it also triggers feature flag escalation (see below).

### Feature Flag Escalation

A notable pattern occurs only in Tier 3: if `BYTE4(qword_4FBB370[2])` is zero (no advanced features enabled), the tier handler allocates a new integer with value 6 and stores it via `sub_16D40E0`. The value 6 (binary `110`) enables two feature gates used by later passes: barrier optimization and memory-space optimization. This means Tier 3 (O3) automatically enables optimization features that lower tiers leave disabled, without requiring explicit CLI flags.

## O-Level Pipeline Comparison

### Pipeline Selection

The new-PM driver `sub_226C400` selects pipeline name strings based on config flags:

```
byte[888]  set  →  "nvopt<O0>"
byte[928]  set  →  "nvopt<O1>"
byte[968]  set  →  "nvopt<O2>"
byte[1008] set  →  "nvopt<O3>"
```

These strings are passed to `sub_2277440` (the new-PM text pipeline parser). The `nvopt` prefix is registered as a pipeline element in both `sub_225D540` (new PM) and `sub_12C35D0` (legacy PM), with vtables at `0x4A08350` and `0x49E6A58` respectively.

### O0: No Optimization

O0 skips the full pipeline entirely. The code falls through to `LABEL_159` which calls only `sub_1C8A4D0(0)` (NVVMFinalCleanup), then proceeds directly to finalization. No Tier 0/1/2/3 sub-pipelines fire. The result is ~5-8 passes total: TargetLibraryInfo, TargetTransformInfo, Verifier, AssumptionCache, ProfileSummary, NVVMFinalCleanup, and codegen setup.

### O1/O2/O3: Full Pipeline with Tier Differentiation

All three levels call `sub_12DE330` for the same ~40-pass Tier 0 sub-pipeline. The differences manifest through four mechanisms:

**1. Tier sub-pipeline gating.** `sub_12DE8F0` is called with the tier number corresponding to the O-level. O1 gets `tier=1` (conservative, skips several passes). O2 gets `tier=2` (full set). O3 gets `tier=3` (aggressive + feature flag escalation).

**2. CGSCC iteration counts.** The CGSCC pass manager wrapper `sub_1A62BF0` takes an iteration count as its first argument. In the O1/O2/O3 base pipeline, it is called with 1 (single inliner pass). In the "mid" fast-compile path, it is called with 5 iterations. In the default path, it varies from 1 to 8 depending on pipeline position, allowing more aggressive devirtualization and inlining at higher optimization levels.

**3. Loop unroll factor.** `sub_1833EB0` is called with factor 3 in the standard pipeline. Tier 3 adds an additional call to TailCallElim and more aggressive LoopUnswitch parameters (the `sub_19B73C0` call receives SM-arch-dependent vector widths at Tier 2/3).

**4. Vectorizer parameters.** `sub_19B73C0` receives different arguments based on tier:
- Tier 0: `(2, -1, -1, -1, -1, -1, -1)` -- conservative vector width 2, all thresholds unlimited
- "mid" path: `(3, -1, -1, 0, 0, -1, 0)` -- vector width 3, some thresholds zeroed (disabled)
- Tier 2/3: Parameters vary by SM architecture via config struct lookups

### Fast-Compile Levels vs O-Levels

| Pipeline | Entry Path | Passes | LSA | MemSpaceOpt | Key Difference |
|----------|-----------|--------|-----|-------------|----------------|
| `nvopt<O0>` | LABEL_159 | ~5-8 | off | off | No optimization |
| `nvopt<Ofcmax>` | LABEL_196 | ~12-15 | forced 0 | forced 0 | Sinking2(fast) + minimal canonicalization |
| `nvopt<Ofcmid>` | LABEL_297 | ~25-30 | normal | enabled | CGSCC(5), LoopVectorize(conservative) |
| `nvopt<Ofcmin>` | LABEL_297 | ~30-35 | normal | enabled | Like Ofcmid but more aggressive loop settings |
| `nvopt<O1>` | sub_12DE330 | ~35 | normal | enabled | Tier 1: conservative set |
| `nvopt<O2>` | sub_12DE330 | ~35+ | normal | enabled | Tier 2: full optimization set |
| `nvopt<O3>` | sub_12DE330 | ~35+ | normal | enabled | Tier 3: aggressive + feature escalation |

Ofcmax is architecturally distinct: it forces `-lsa-opt=0` and `-memory-space-opt=0` in the optimizer flags (confirmed in both `sub_9624D0` line 1358 and `sub_12CC750` line 2025). This means two of NVIDIA's most important proprietary passes -- LSA optimization and MemorySpaceOpt -- are unconditionally disabled regardless of what the user requests.

## Pipeline Text Strings and `nvopt<>` Dispatch

### The `nvopt<>` Naming Convention

NVIDIA replaces LLVM's standard `default<O2>` pipeline naming with a proprietary `nvopt<>` prefix. The new-PM driver `sub_226C400` (35KB, at `0x226C400`) selects one of exactly seven pipeline name strings based on optimization level and fast-compile flags. These strings are passed verbatim to `sub_2277440` (60KB, at `0x2277440`) -- NVIDIA's equivalent of LLVM's `PassBuilder::buildDefaultPipeline()`.

```
nvopt<O0>       Optimization disabled. ~5-8 infrastructure passes only.
nvopt<O1>       Standard optimization, Tier 1 (conservative).
nvopt<O2>       Standard optimization, Tier 2 (full).
nvopt<O3>       Standard optimization, Tier 3 (aggressive + feature escalation).
nvopt<Ofcmax>   Fast-compile maximum speed. Forces -lsa-opt=0, -memory-space-opt=0.
nvopt<Ofcmid>   Fast-compile medium. MemorySpaceOpt enabled, CGSCC(5) iterations.
nvopt<Ofcmin>   Fast-compile minimum. Like Ofcmid but more aggressive loop settings.
```

### Selection Algorithm (`sub_226C400`)

The config struct encodes O-level flags at fixed byte offsets. The fast-compile level string (if present) is at qwords 131/132 (offset 1048/1056), encoded as a 3-byte sequence compared via 2-byte word + 1-byte suffix:

```c
// sub_226C400, lines 828-874 (pseudocode)
char* select_pipeline_name(Config* cfg) {
    if (cfg->byte[928])   return "nvopt<O1>";     // 9 chars
    if (cfg->byte[968])   return "nvopt<O2>";     // 9 chars
    if (cfg->byte[1008])  return "nvopt<O3>";     // 9 chars

    char* fc = cfg->qword[131];
    int fc_len = cfg->qword[132];
    if (fc_len == 3) {
        // Word comparison: *(uint16_t*)fc, then byte fc[2]
        if (*(uint16_t*)fc == 24941 && fc[2] == 120)  // "max" = 'a','m' + 'x'
            return "nvopt<Ofcmax>";   // 14 chars
        if (*(uint16_t*)fc == 26989 && fc[2] == 100)  // "mid" = 'i','m' + 'd'
            return "nvopt<Ofcmid>";   // 14 chars
        if (*(uint16_t*)fc == 26989 && fc[2] == 110)  // "min" = 'i','m' + 'n'
            return "nvopt<Ofcmin>";   // 14 chars
    }
    return "nvopt<O0>";              // 9 chars
}
```

The `nvopt` prefix is registered as a pipeline element in `sub_225D540` (new PM, vtable `0x4A08350`) and `sub_12C35D0` (legacy PM, vtable `0x49E6A58`). Both route into an `nvopt` pipeline builder class that creates a 512-byte pipeline object via `sub_12EC960`.

### Mutual Exclusion

Combining `-O#` with `--passes=` or `--foo-pass` is an error:

```
Cannot specify -O#/-Ofast-compile=<min,mid,max> and --passes=/--foo-pass,
use -passes='default<O#>,other-pass' or -passes='default<Ofcmax>,other-pass'
```

### Pipeline Text Parser (`sub_2277440`)

`sub_2277440` (60KB) is the new-PM `buildDefaultPipeline()` equivalent. It tokenizes the pipeline name string via `sub_2352D90`, then dispatches to the appropriate pipeline builder based on the `nvopt<>` parameter. NVIDIA custom passes are injected via extension point callbacks at `[PassBuilder+2208]` (stride 32 bytes per entry, count at `[PassBuilder+2216]`). Each callback entry has a guard pointer at `[+16]` and a callback function at `[+24]`.

### Fast-Compile Level Encoding

In the libnvvm config struct, offset 1640 holds an integer encoding:

| Value | CLI Source | Pipeline Name | Notes |
|-------|-----------|---------------|-------|
| 0 | (no `-Ofast-compile`) | normal O-level | Default |
| 1 | `-Ofast-compile=0` | reset to 0 | Treated as "off" |
| 2 | `-Ofc=max` | `nvopt<Ofcmax>` | Forces `-lsa-opt=0`, `-memory-space-opt=0` |
| 3 | `-Ofc=mid` | `nvopt<Ofcmid>` | MemorySpaceOpt enabled |
| 4 | `-Ofc=min` | `nvopt<Ofcmin>` | Closest to full optimization |

Any other value produces: `"libnvvm : error: -Ofast-compile called with unsupported level, only supports 0, min, mid, or max"`.

## Pass Registration Architecture

### Dual Pass Manager Support

cicc v13.0 maintains registrations for both the Legacy Pass Manager and the New Pass Manager simultaneously. This dual support is necessary during the LLVM Legacy-to-New PM migration. The Legacy PM path is taken when `a4[4384] != 0` (the fast-compile/bypass flag), while the New PM path handles normal compilation.

**Legacy PM registration** occurs in pass constructor functions scattered throughout the binary. For example, MemorySpaceOpt registers as `"memory-space-opt-pass"` via `sub_1C97F80`. Each Legacy PM pass calls `RegisterPass<>` with a pass ID and description string.

**New PM registration** is centralized in `sub_2342890` -- a single 2,816-line function that registers every analysis, pass, and printer. It calls `sub_E41FB0(pm, class_name, len, pass_name, len)` for each pass, inserting into a StringMap with open-addressing and linear probing.

### New PM Registration Structure

`sub_2342890` registers passes in a strict ordering by pipeline level:

| Section | Lines | Count | Content |
|---------|-------|-------|---------|
| Module analyses | 514-596 | ~18 | CallGraph, ProfileSummary, LazyCallGraph, etc. |
| Module passes | 599-1153 | ~95 | AlwaysInline, GlobalOpt, NVIDIA module passes |
| CGSCC analyses | 1155-1163 | ~5 | FunctionAnalysisManagerCGSCC, etc. |
| CGSCC passes | 1170-1206 | ~15 | Inliner, Attributor, ArgumentPromotion |
| Function analyses | 1208-1415 | ~65 | DominatorTree, LoopInfo, MemorySSA, **rpa**, **merge-sets** |
| Function passes | 1420-2319 | ~185 | SROA, GVN, LICM, all NVIDIA function passes |
| LoopNest passes | 2320-2339 | ~8 | LoopInterchange, LoopFlatten |
| Loop analyses | 2340-2362 | ~10 | LoopAccessAnalysis, IVUsers |
| Loop passes | 2367-2482 | ~40 | IndVarSimplify, LICM, LoopUnroll, **loop-index-split** |
| Machine analyses | 2483-2580 | ~30 | LiveIntervals, SlotIndexes |
| Machine passes | 2581-2815 | ~80 | ExpandPostRAPseudos, BranchFolding |

### Parameterized Pass Parsing

When the pipeline text parser encounters a pass name with angle-bracket parameters (e.g., `memory-space-opt<first-time;warnings>`), a registered callback parses the parameter string. The parsing flow:

1. `sub_2337DE0` matches the pass name via a `starts_with` comparison
2. `sub_234CEE0` extracts the `<...>` parameter string
3. The parameter-parsing callback (e.g., `sub_23331A0` for MemorySpaceOpt) is invoked
4. The parser splits on `;` and matches each token against known parameter names
5. A configured pass options struct is returned and used to construct the pass

For MemorySpaceOpt, the parameter parser (`sub_23331A0`) recognizes four tokens:

| Token | Length | Effect |
|-------|--------|--------|
| `first-time` | 10 | Sets `first_time = true` (default) |
| `second-time` | 11 | Sets `first_time = false` |
| `warnings` | 8 | Enables address-space warnings |
| `no-warnings` | 11 | Disables warnings |

Invalid parameters produce: `"invalid MemorySpaceOpt pass parameter '{0}'"`.

### Pass Serialization

Each parameterized NVIDIA pass also registers a serializer for pipeline text output (used by `--print-pipeline-passes`). The serializers write the pass class name followed by the current parameter state:

| Pass | Serializer | Output Format |
|------|-----------|---------------|
| MemorySpaceOpt | `sub_2CE0440` | `MemorySpaceOptPass]<first-time;...>` |
| BranchDist | `sub_2311040` | `BranchDistPass]` |
| Sinking2 | `sub_2315E20` | `llvm::Sinking2Pass]` |
| Remat | `sub_2311820` | `RematerializationPass]` |
| NVVMPeephole | `sub_2314DA0` | `NVVMPeepholeOptimizerPass]` |
| LoopIndexSplit | `sub_2312380` | `LoopIndexSplitPass]` |

## Pipeline Construction Flow

### The AddPass Mechanism -- `sub_12DE0B0`

| | |
|---|---|
| **Address** | `0x12DE0B0` |
| **Size** | 3,458 bytes |
| **Signature** | `int64 sub_12DE0B0(int64 passMgr, int64 passObj, uint8 flags, char barrier)` |
| **Call count** | ~137 direct calls from `sub_12E54A0`, ~40 from `sub_12DE330`, ~50+ per tier |

`sub_12DE0B0` is the **sole** entry point for adding passes to the pipeline. Every pass factory call in the entire pipeline assembler funnels through this function. It performs three operations atomically: hash-table insertion for O(1) lookup, flag encoding for the pass scheduler, and append to the ordered pass array.

```c
// Detailed pseudocode for sub_12DE0B0
int64 AddPass(PassManager* PM, Pass* pass, uint8_t flags, char barrier) {
    // --- Step 1: Hash the pass pointer ---
    // Uses a custom shift-XOR hash, NOT a standard hash function.
    // The two shifts (9 and 4) spread pointer bits across the table.
    uint64_t hash = ((uint64_t)pass >> 9) ^ ((uint64_t)pass >> 4);

    // --- Step 2: Open-addressing insert into hash table at PM+80 ---
    // The hash table is a flat array of 16-byte entries at PM+80:
    //   [+0] uint64 pass_pointer (0 = empty slot)
    //   [+8] uint8  combined_flags
    // Table capacity is stored at PM+72 (initial: derived from 0x800000000 mask).
    // Collision resolution: linear probing with step 1.
    uint8_t combined = flags | (barrier ? 2 : 0);
    //   Bit 0 (0x01): 1 = FunctionPass, 0 = ModulePass/AnalysisPass
    //   Bit 1 (0x02): 1 = barrier (scheduling fence)
    //   Remaining bits: reserved

    size_t capacity = PM->ht_capacity;       // at PM+72
    size_t idx = hash & (capacity - 1);      // power-of-2 masking
    Entry* table = (Entry*)(PM + 80);

    while (table[idx].pass != 0) {
        if (table[idx].pass == pass) {
            // Pass already inserted -- update flags only
            table[idx].flags = combined;
            return 0;  // dedup: no second insertion
        }
        idx = (idx + 1) & (capacity - 1);   // linear probe
    }
    table[idx].pass = pass;
    table[idx].flags = combined;

    // --- Step 3: Append to ordered pass array at PM[0] ---
    // PM[0] = pointer to dynamic array of 8-byte pass pointers
    // PM[1] = count of passes (PM+8)
    // Growth: geometric reallocation (not shown here)
    uint64_t* array = (uint64_t*)PM->passes; // PM[0]
    array[PM->count] = (uint64_t)pass;
    PM->count++;                              // PM+8

    return 0;
}
```

The `flags` parameter encodes the pass type: `0` for module/analysis passes, `1` for function passes. The `barrier` parameter (bit 1) is a scheduling fence that tells the pass manager all preceding passes must complete before this pass runs -- used for passes that require the module in a globally consistent state (e.g., after whole-module inlining).

The hash table serves two purposes: (a) deduplication -- if the same pass factory is called twice (which happens for NVVMReflect, NVVMIntrinsicLowering, etc.), the second call updates flags rather than inserting a duplicate; and (b) O(1) flag lookup during the codegen dispatch phase (`sub_12DFE00`), where each pass's type and barrier status must be queried efficiently.

The pass manager container is initialized at line 390 of `sub_12E54A0` with inline storage: `v270 = v272` (stack buffer), `v271 = 0x800000000` (capacity/flags encoding with 33-bit sentinel).

### Complete 8-Phase Construction Algorithm

The full pipeline construction in `sub_12E54A0` proceeds through eight phases. The pseudocode below is reconstructed from the decompiled 49.8KB function at lines 300-757 of the decompilation output. All `a4` offsets refer to the CompilerOptions struct (parameter 4, ~4500 bytes).

#### Phase 0: Infrastructure (lines 396-420, always runs)

```c
// Phase 0: Analysis infrastructure required by all subsequent passes
#01  TLI = sub_149CCE0(malloc(368), sub_14A04B0(triple));
     AddPass(PM, TLI, 0, 0);     // TargetLibraryInfoWrapperPass [Module]

#02  TTI = sub_1BFB520(malloc(208), sub_1BFB9A0(dataLayout));
     AddPass(PM, TTI, 1, 0);     // TargetTransformInfoWrapperPass [Function]

#03  verifier = sub_14A7550();
     AddPass(PM, verifier, 0, 0); // VerifierPass / BasicAliasAnalysis [Module]

#04  assumptions = sub_1361950();
     AddPass(PM, assumptions, 0, 0); // AssumptionCacheTracker [Module]

#05  profile = sub_1CB0F50();
     AddPass(PM, profile, 1, 0); // ProfileSummaryInfoWrapperPass [Function]
```

These five analysis passes have no upstream-LLVM equivalent in terms of initialization ordering. NVIDIA always adds them first regardless of optimization level, language, or fast-compile mode.

#### Phase 1: Language Dispatch (lines 421-488)

Phase 1 reads the language string at `a4[3648]` (pointer) with length at `a4[3656]`. Three language paths exist; each produces a fundamentally different pass sequence. See the [Language Path Differences](#language-path-differences) section below for the complete per-path pass lists.

```c
// Phase 1: Language-based pipeline branching
char* lang = *(char**)(a4 + 3648);
int lang_len = *(int*)(a4 + 3656);

bool opt_enabled = *(bool*)(a4 + 4224);
bool fc_max = false, fc_mid = false;
int v238 = *(int*)(a4 + 4304);  // device-code / additional-opt flag

if (lang_len == 3) {
    uint16_t w = *(uint16_t*)lang;
    if (w == 0x7470 && lang[2] == 0x78) {        // "ptx"
        goto PATH_A_PTX;
    }
    if (w == 0x696D && lang[2] == 0x64) {         // "mid"
        goto PATH_B_MID;
    }
    // "idn" (w == 0x696D && lang[2] == 0x6E) shares the default path
}
// Fall through to PATH_C_DEFAULT

// Fast-compile dispatch (within the language check):
// fc="max" AND !v238 → v244=1, v238=1, goto LABEL_191 (minimal + O0)
// fc="max" AND v238  → goto LABEL_196 → LABEL_188 (Sinking2 + common)
// fc="mid"           → goto LABEL_297 (mid pipeline)
// fc="min"           → goto LABEL_297 (min pipeline, differs via v238)
// no fc, no O-level  → LABEL_159 (O0 minimal pipeline)
// O-level set        → LABEL_38 → LABEL_39 (process pass list + tiers)
```

#### Phase 2: Pre-Optimization (lines 442-480)

Only when optimization is not completely skipped. Each pass is gated by a per-pass disable flag in the NVVMPassOptions struct.

```c
// Phase 2: Early passes before the main optimization loop
if (!a4[1960] || a4[3000])                           // not disabled OR extra trigger
    AddPass(PM, sub_1857160(), 1, 0);                // NVVMReflect

if (a4[3000])                                        // extra DeadArgElim trigger
    AddPass(PM, sub_18FD350(0), 1, 0);              // DeadArgElimination

if (!a4[1680])                                       // NVIDIA pass not disabled
    AddPass(PM, sub_19CE990(), 1, 0);               // LoopStrengthReduce (NVIDIA)

AddPass(PM, sub_1CB4E40(0), 1, 0);                  // NVVMIntrinsicLowering(level=0)

if (!a4[2040])
    AddPass(PM, sub_1B26330(), 1, 0);                // MemCpyOpt

AddPass(PM, sub_12D4560(), 1, 0);                    // NVVMVerifier

if (!a4[1960])
    AddPass(PM, sub_184CD60(), 1, 0);                // ConstantMerge

if (!a4[440] && !a4[400])
    AddPass(PM, sub_1C4B6F0(), 1, 0);               // AlwaysInliner

if (a4[3160])                                        // debug dump enabled
    AddPass(PM, sub_17060B0(1, 0), 1, 0);           // PrintModulePass
```

#### Phase 3: Main Optimization Loop (lines 481-553)

The tier-threshold-driven loop iterates over the plugin/external pass array at `a4[4488]`. Each entry is 16 bytes (vtable pointer + phase_id). When a threshold is crossed, the corresponding tier sub-pipeline fires once and never again.

```c
// Phase 3: Tier dispatch within the main plugin pass loop
uint64_t* entry = *(uint64_t**)(a4 + 4488);
uint64_t* end   = *(uint64_t**)(a4 + 4496);

while (entry < end) {
    int phase_id = *(int*)((char*)entry + 8);

    // Tier 0: full optimization sub-pipeline
    if (*(bool*)(a4+4224) && phase_id > *(int*)(a4+4228)) {
        sub_12DE330(PM, opts);        // ~40 passes
        *(bool*)(a4+4224) = false;    // fire once
    }
    // Tier 1: conservative
    if (*(bool*)(a4+3528) && phase_id > *(int*)(a4+3532)) {
        sub_12DE8F0(PM, 1, opts);
        *(bool*)(a4+3528) = false;
    }
    // Tier 2: full
    if (*(bool*)(a4+3568) && phase_id > *(int*)(a4+3572)) {
        sub_12DE8F0(PM, 2, opts);
        *(bool*)(a4+3568) = false;
    }
    // Tier 3: aggressive
    if (*(bool*)(a4+3608) && phase_id > *(int*)(a4+3612)) {
        sub_12DE8F0(PM, 3, opts);
        *(bool*)(a4+3608) = false;
    }

    // Insert the plugin/external pass itself
    Pass* plugin = vtable_call(entry, +72);  // entry->createPass()
    AddPass(PM, plugin, 1, 0);

    // Optional debug verification after each plugin pass
    if (a4[3904]) {
        sub_12D3E60();  // insert verification/print pass
        sub_16E8CB0();
        sub_15E9F00();
    }

    entry = (uint64_t*)((char*)entry + 16);  // next entry (16-byte stride)
}

// Any tier that didn't fire during the loop fires unconditionally now
if (*(bool*)(a4+4224))  sub_12DE330(PM, opts);
if (*(bool*)(a4+3528))  sub_12DE8F0(PM, 1, opts);
if (*(bool*)(a4+3568))  sub_12DE8F0(PM, 2, opts);
if (*(bool*)(a4+3608))  sub_12DE8F0(PM, 3, opts);
```

#### Phase 4: Post-Optimization Language Paths (lines 580-1371)

After the main loop, language-specific post-optimization runs. This is where the three paths diverge most significantly. Each path ends by falling through to LABEL_84 (Phase 5). See [Language Path Differences](#language-path-differences) for complete pass lists per path.

#### Phase 5: Finalization (LABEL_84, lines 640-653)

Always runs after the language-specific optimization path completes.

```c
// Phase 5: Finalization -- barriers, cleanup, codegen
if (!v244 && a4[3488])                              // barrier optimization enabled
    AddPass(PM, sub_1C98160(a4[2920]!=0), 1, 0);   // NVVMLowerBarriers

AddPass(PM, sub_1CEBD10(), 1, 0);                   // NVVMFinalLowering (cleanup)

if (!a4[2800] && !a4[4464])                         // late CFG cleanup not disabled
    AddPass(PM, sub_1654860(1), 1, 0);              // BreakCriticalEdges

sub_12DFE00(PM, subtargetInfo, a4);                  // Codegen dispatch (see below)
```

#### Phase 6: Phase II Codegen Check (lines 654-693)

Reads the compilation phase counter and conditionally enters a special codegen extension block for multi-phase compilation.

```c
// Phase 6: Phase-II-specific codegen extensions
int phase = sub_16D40F0(qword_4FBB3B0);  // read cl::opt<int> phase counter
if (phase == 2 && (*(int*)(a4 + 4480) & 4)) {
    // Enter special Phase II codegen block
    // Calls vtable at v245+56 (TargetMachine::addPhaseIIPasses)
    // Passes SubtargetInfo (v253) and CodeGenOpt config (v262)
    target_machine->addPhaseIIPasses(subtarget, codegen_config);
}
```

#### Phase 7: Pipeline Execution (lines 694-698)

```c
// Phase 7: Run the assembled pipeline
sub_160FB70(PM, *output, output[1]);   // PassManager::run(Module, outputs)
sub_1619BD0(PM, module);               // PassManager::finalize(Module)
free(v274);                            // cleanup allocations
sub_160FE50(PM);                       // PassManager::destroy()
```

#### Phase 8: Basic Block Naming (lines 700-757)

Only when `a4[3944]` (debug/naming mode) is set. Produces deterministic block names for debugging.

```c
// Phase 8: Debug block naming for IR dump readability
if (a4[3944]) {
    int funcIdx = 0;
    for (Function* F = module->functions; F; F = F->next) {
        if (sub_15E4F60(F))  continue;  // skip declarations
        funcIdx++;
        int blockIdx = 0;
        for (BasicBlock* BB = F->blocks; BB; BB = BB->next) {
            blockIdx++;
            char name[32];
            sprintf(name, "F%d_B%d", funcIdx, blockIdx);
            sub_164B780(BB, &name);  // BB->setName()
        }
    }
}
```

### Language Path Differences

The three language paths in Phase 1/4 represent fundamentally different IR maturity levels. The `a4[3648]` string pointer determines which path is taken, with length at `a4[3656]`.

#### Path A: `"ptx"` -- Light Pipeline (~15 passes)

PTX text input has already been lowered by an earlier compilation stage. This path applies only light cleanup and canonicalization:

```
sub_1CEF8F0()               NVVMPeephole
sub_215D9D0()               NVVMAnnotationsProcessor
sub_1857160()  [!a4[880]]   NVVMReflect
sub_1A62BF0(1,0,0,1,0,0,1)  LLVM standard pipeline #1
sub_1B26330()  [!a4[2040]]  MemCpyOpt
sub_17060B0(0,0)            PrintModulePass (debug)
sub_18DEFF0()  [!a4[280]]   DCE
sub_1A62BF0(1,0,0,1,0,0,1)  LLVM standard pipeline #1 (repeat)
sub_18B1DE0()  [!a4[2640]]  LoopPass / BarrierOpt
sub_1C8E680(0) [!a4[1760]]  MemorySpaceOptimization
 --> LABEL_84 (finalization)
```

Key difference: no SROA, no GVN, no loop transformations, no CGSCC inlining. The PTX path trusts that the earlier compilation already optimized the code.

#### Path B: `"mid"` -- Full Optimization (~45 passes)

The primary path for standard CUDA compilation. The IR comes from the EDG frontend through IR generation and is at "mid-level" maturity (high-level constructs lowered, but not yet optimized).

```
sub_184CD60()  [!a4[1960]]    ConstantMerge
sub_1CB4E40(0) [!a4[2000]]    NVVMIntrinsicLowering (1st of 4)
sub_1B26330()  [!a4[2040]]    MemCpyOpt
sub_198E2A0()                  SROA
sub_1CEF8F0()                  NVVMPeephole
sub_215D9D0()                  NVVMAnnotationsProcessor
sub_198DF00(-1)[!a4[1520]]     LoopSimplify
sub_1C6E800()                  GVN
sub_1A223D0()  [!a4[2600]]    NVVMIRVerification (1st of 5+)
sub_190BB10(0,0)               SimplifyCFG
sub_1832270(1)                 InstructionCombining
sub_1A62BF0(5,0,0,1,0,0,1)    CGSCC pipeline (5 iterations)
sub_1CB4E40(0) [!a4[2000]]    NVVMIntrinsicLowering (2nd)
sub_18FD350(0)                 DeadArgElim
sub_1841180()  [!a4[680]]     FunctionAttrs
sub_18DEFF0()  [!a4[280]]     DCE
sub_184CD60()  [!a4[1960]]    ConstantMerge
sub_195E880(0) [!a4[1240]]    LICM
sub_1C98160(0)                 NVVMLowerBarriers
sub_1C8E680(0) [!a4[1760]]    MemorySpaceOpt (1st invocation)
sub_1B7FDF0(3) [!a4[1280]]    Reassociate
sub_1A62BF0(8,0,0,1,1,0,1)    CGSCC pipeline (8 iterations)
sub_1857160()  [!a4[880]]     NVVMReflect (2nd of 3)
sub_1C6FCA0()  [!a4[1840]]    ADCE
sub_1A7A9F0()  [!a4[2720]]    InstructionSimplify
sub_18FD350(0)                 DeadArgElim
sub_1833EB0(3) [!a4[320]]     TailCallElim
sub_18FD350(0)                 DeadArgElim
sub_18EEA90()                  CorrelatedValuePropagation
sub_1869C50(1,0,1)             Sink (MemorySSA-based)
sub_190BB10(0,0)[!a4[960]]     SimplifyCFG
sub_18F5480()  [!a4[760]]     DSE
sub_1CC60B0()  [!a4[2440]]    NVVMSinking2
sub_1A223D0()  [!a4[2600]]    NVVMIRVerification
sub_1C8A4D0(0)                 EarlyCSE
sub_1857160()  [!a4[880]]     NVVMReflect (3rd)
sub_1A62BF0(8,0,0,1,1,0,1)    CGSCC pipeline (8 iterations)
sub_1CB4E40(0) [!a4[2000]]    NVVMIntrinsicLowering (3rd)
sub_185D600()  [!a4[920]]     IPConstPropagation
sub_195E880(0) [!a4[1240]]    LICM
sub_1CB4E40(0) [!a4[2000]]    NVVMIntrinsicLowering (4th)
sub_1CB73C0()  [!a4[2120]]    NVVMBranchDist
sub_1A13320()  [!a4[2320]]    NVVMRematerialization
 --> LABEL_84 (finalization)
```

Key pattern: NVVMIntrinsicLowering runs 4 times, NVVMReflect runs 3 times, NVVMIRVerification runs 5+ times. The CGSCC pipeline is called with 5 and 8 iteration counts (aggressive devirtualization).

#### Path C: Default -- General Pipeline (~40 passes)

Used for bitcode from external sources (not marked as "ptx" or "mid"). Balances optimization breadth with conservative assumptions about IR maturity.

```
sub_1A62BF0(4,0,0,1,0,0,1)    LLVM standard pipeline #4
sub_1857160()  [!a4[880]]     NVVMReflect (1st)
sub_1CB4E40(0) [!a4[2000]]    NVVMIntrinsicLowering
sub_1857160()  [!a4[880]]     NVVMReflect (2nd)
sub_1CEF8F0()                  NVVMPeephole
sub_215D9D0()                  NVVMAnnotationsProcessor
sub_1A7A9F0()  [!a4[2720]]    InstructionSimplify
sub_1A62BF0(5,0,0,1,0,0,1)    LLVM standard pipeline #5
sub_185D600()  [!a4[920]]     IPConstPropagation
sub_1B26330()  [!a4[2040]]    MemCpyOpt
sub_184CD60()  [!a4[1960]]    ConstantMerge
sub_1A13320()  [!a4[2320]]    NVVMRematerialization
sub_1833EB0(3) [!a4[320]]     TailCallElim
sub_1C6E800()                  GVN
sub_1842BC0()  [!a4[720]]     SCCP
sub_18DEFF0()  [!a4[280]]     DCE
sub_184CD60()  [!a4[1960]]    ConstantMerge
sub_18FD350(0)                 DeadArgElim
sub_18EEA90()                  CorrelatedValuePropagation
sub_1A62BF0(1,0,0,1,0,0,1)    LLVM standard pipeline #1
sub_197E720()                  LoopUnroll
sub_19401A0()  [!a4[1000]]    InstCombine
sub_1857160()  [!a4[880]]     NVVMReflect (3rd)
sub_1A62BF0(7,0,0,1,0,0,1)    LLVM standard pipeline #7
sub_1C8A4D0(0)                 EarlyCSE
sub_1A223D0()  [!a4[2600]]    NVVMIRVerification
sub_1832270(1)                 InstructionCombining
sub_1869C50(1,0,1)             Sink
sub_1A68E70()                  LoopIdiomRecognize
sub_198DF00(-1)[!a4[1520]]     LoopSimplify
sub_195E880(0) [!a4[1240]]    LICM
sub_190BB10(0,0)[!a4[960]]     SimplifyCFG
sub_19B73C0(3,-1,-1,0,0,-1,0)  LoopUnswitch
sub_1A223D0()  [!a4[2600]]    NVVMIRVerification
sub_1C98160(0)                 NVVMLowerBarriers
sub_1C8E680(0) [!a4[1760]]    MemorySpaceOpt
sub_1B7FDF0(3) [!a4[1280]]    Reassociate
sub_18B1DE0()  [!a4[2640]]    LoopPass
sub_1952F90(-1)[!a4[1160]]     LoopIndexSplit
sub_18FD350(0)                 DeadArgElim
sub_1CC60B0()  [!a4[2440]]    NVVMSinking2
sub_1A62BF0(2,0,0,1,0,0,1)    LLVM standard pipeline #2
sub_1A223D0()  [!a4[2600]]    NVVMIRVerification
sub_18A3430()  [!a4[1120]]    NVVMPredicateOpt
sub_1A62BF0(4,0,0,1,1,0,1)    LLVM standard pipeline #4 (inlining)
 --> LABEL_84 (finalization)
```

Key difference from "mid": default path uses LLVM standard pipeline wrappers (IDs 1,2,4,5,7) more heavily, runs SCCP explicitly, includes LoopIdiomRecognize, and uses a conservative LoopUnswitch with zeroed thresholds `(3,-1,-1,0,0,-1,0)`.

### Codegen Dispatch -- `sub_12DFE00`

| | |
|---|---|
| **Address** | `0x12DFE00` |
| **Size** | 20,729 bytes |
| **Signature** | `int64 sub_12DFE00(int64 passMgr, int64 subtargetInfo, int64 opts)` |
| **Called from** | Phase 5 of `sub_12E54A0` (LABEL_84, line 640) |

The codegen dispatch does **not** simply append passes to the pipeline. It performs a full dependency analysis over every pass already inserted, constructs an ordering graph, and then emits codegen passes in topologically-sorted order. This is necessary because machine-level passes (register allocation, instruction scheduling, frame lowering) have strict ordering dependencies that the flat AddPass model cannot express.

```c
// Pseudocode for sub_12DFE00 (codegen dispatch with dependency analysis)
void CodegenDispatch(PassManager* PM, SubtargetInfo* STI, CompilerOpts* opts) {
    // Step 1: Read optimization level to determine analysis depth
    int opt_level = *(int*)(opts + 200);  // opts[200] = optimization level
    bool do_deps = (opt_level > 1);       // dependency tracking for O2+

    // Step 2: Classify existing passes
    // Iterates PM->passes[0..PM->count], calling two vtable methods per pass
    HashTable dep_graph;   // secondary hash table for dependencies (v134..v137)
    init_hashtable(&dep_graph);

    for (int i = 0; i < PM->count; i++) {
        Pass* p = PM->passes[i];

        // 2a. Check if pass is codegen-only (vtable+112)
        bool is_codegen = p->vtable->isCodeGenOnly(p);   // vtable offset +112
        if (is_codegen)
            continue;  // already classified, skip

        // 2b. Check registration status
        int status = sub_163A1D0(p);   // pass registry check
        sub_163A340(p, &status);       // update status

        // 2c. If pass needs codegen support, mark it in the hash table
        if (pass_needs_codegen(p)) {
            // Set flag |= 2 in the AddPass hash table entry
            // This marks the pass as "codegen-interacting"
            Entry* e = hashtable_find(PM + 80, p);
            if (e) e->flags |= 2;
        }

        // 2d. Build dependency edges (getAnalysisUsage)
        if (do_deps) {
            AnalysisUsage AU;
            p->vtable->getAnalysisUsage(p, &AU);   // vtable offset +16

            // For each required analysis, create an ordering edge
            // in the dependency hash table
            for (AnalysisID* req = AU.required; req; req = req->next) {
                dep_graph_add_edge(&dep_graph, p, req->pass);
            }
        }
    }

    // Step 3: Emit codegen passes in dependency-respecting order
    // Calls the SubtargetInfo hook to get the ordered codegen pass list
    // vtable+16 at STI -> STI->emitCodeGenPasses(PM, dep_graph)
    STI->vtable->emitCodeGenPasses(STI, PM, &dep_graph);
    // Each emitted pass gets a flag:
    //   0 = normal pass (no special ordering)
    //   1 = pass with codegen requirement (flag bit 0 from AddPass)
}
```

The dependency graph construction is what makes this function 20KB: it must handle the full LLVM analysis dependency model, including transitive dependencies and analysis preservation. The `getAnalysisUsage` calls return `Required`, `RequiredTransitive`, and `Preserved` sets that define the ordering constraints between passes.

For O0 compilation (`opt_level == 0`), the dependency tracking is skipped entirely -- codegen passes are emitted in a fixed default order since no optimization passes exist that could create ordering conflicts.

## Pass Iteration and Convergence

### CGSCC Fixed-Point Iteration

The CGSCC (Call Graph Strongly Connected Component) pass manager `sub_1A62BF0` wraps a standard LLVM InlinerWrapper with a configurable iteration count. The first parameter controls how many times the CGSCC pipeline iterates over the call graph:

| Pipeline Position | Iteration Count | Context |
|-------------------|----------------|---------|
| O1/O2/O3 base (sub_12DE330) | 1 | Standard inlining: one pass over the call graph |
| "mid" path (Ofcmid/Ofcmin) | 5 | Aggressive: 5 iterations to resolve indirect calls |
| Default path (general IR) | 1, 2, 4, 5, 7, or 8 | Varies by position in pipeline |

Higher iteration counts allow the CGSCC framework to resolve more indirect calls through devirtualization. After each iteration, newly-inlined code may expose new call targets, which the next iteration can inline. The diminishing returns typically plateau after 3-5 iterations, which explains NVIDIA's choice of 5 for the "mid" fast-compile path (balancing compile time against code quality).

### NVVMReflect Multi-Run Pattern

NVVMReflect (`sub_1857160`) runs multiple times in the pipeline because NVVM IR may contain `__nvvm_reflect("__CUDA_ARCH")` calls at different nesting depths. The first run resolves top-level reflect calls to constants. Subsequent optimization passes (inlining, constant propagation, loop unrolling) may expose new reflect calls that were hidden inside inlined functions or unrolled loop bodies. Running NVVMReflect again after these transformations catches these newly-exposed calls.

In the "mid" path, NVVMReflect appears at three distinct positions:
1. Early (before GVN) -- resolves top-level architecture queries
2. Mid (after CGSCC inlining and DeadArgElim) -- catches reflect calls exposed by inlining
3. Late (after LoopSimplify and second CGSCC) -- catches reflect calls exposed by loop transformations

### NVVMIntrinsicLowering Repetition

Similarly, NVVMIntrinsicLowering (`sub_1CB4E40`) runs 4 times in the "mid" path. Each invocation lowers a different subset of NVVM intrinsics based on what the preceding optimization passes have simplified. The pass takes a `level` parameter (0 or 1) that controls which lowering rules are active. Level 0 handles basic intrinsic lowering; level 1 handles barrier-related lowering that only becomes safe after certain control flow transformations.

### NVVMIRVerification as a Convergence Check

NVVMIRVerification (`sub_1A223D0`) runs after every major transformation group -- not for optimization, but as a correctness invariant check. In the "mid" path it appears at 5+ positions. In the tier 1/2/3 sub-pipeline it appears 4 times (after NVVMIntrinsicLowering, after barrier lowering, after GenericToNVVM, and after the late optimization sequence). If any transformation violates NVVM IR constraints (invalid address space usage, malformed intrinsic signatures, broken metadata), this pass reports the error immediately rather than allowing it to propagate to codegen where diagnosis would be much harder.

### The Repeat-Until-Clean Philosophy

NVIDIA's pipeline does not use explicit fixed-point loops (run passes until IR stops changing). Instead, it achieves convergence through **strategic repetition**: the same pass appears at multiple carefully-chosen pipeline positions, with different optimization passes running between repetitions. This is more predictable than a true fixed-point approach because compilation time is bounded by the static pipeline length rather than by how many iterations are needed for convergence. The tradeoff is that the pipeline may not reach a true fixed point -- some optimization opportunities exposed by late passes may not be caught -- but in practice, the multi-position placement catches the vast majority of cases.

## LLVM Standard Pass Pipeline Factory -- `sub_1A62BF0`

The LLVM standard pass pipeline is invoked multiple times throughout the optimizer via `sub_1A62BF0`. The first parameter is a **pipeline ID** that selects which LLVM extension point to inject passes at:

| Pipeline ID | LLVM Extension Point | Usage Context |
|-------------|---------------------|---------------|
| 1 | `EP_EarlyAsPossible` / basic cleanup | Tier 0, default path |
| 2 | `EP_LoopOptimizerEnd` | Default path late |
| 4 | `EP_ScalarOptimizerLate` | Default path, Tier sub-pipeline |
| 5 | `EP_VectorizerStart` | "mid" path, default path |
| 7 | `EP_OptimizerLast` | Default path |
| 8 | `EP_CGSCCOptimizerLate` | "mid" path (with opt flag = 1 for inlining) |

The signature is `sub_1A62BF0(pipelineID, 0, 0, 1, optFlag, 0, 1, outBuf)`, where `optFlag` at position 5 enables inlining within the CGSCC sub-pipeline (observed as 1 for pipeline IDs 4 and 8 in the "mid" path: `sub_1A62BF0(8,0,0,1,1,0,1)`).

Each call potentially returns a cleanup callback stored in `v298`, invoked as `v298[0](s, s, 3)` for destructor/finalization. The factory is called 9+ times across the three language paths.

## CompilerOptions Struct Flag Map

The `a4` parameter to `sub_12E54A0` is a ~4500-byte CompilerOptions struct. The following offsets have been confirmed through cross-referencing guards in the pipeline assembler and tier sub-pipelines.

| Offset | Type | Purpose | Cross-Reference |
|--------|------|---------|-----------------|
| +200 | int | Optimization level (0-3) | `sub_12DFE00` codegen depth |
| +280 | bool | Disable DCE | `sub_18DEFF0` guard |
| +320 | bool | Disable TailCallElim | `sub_1833EB0` guard |
| +360 | bool | Disable NVVMLateOpt | `sub_1C46000` guard |
| +400 | bool | Disable inlining variant A | |
| +440 | bool | Disable inlining variant B | `sub_1C4B6F0` guard |
| +480 | bool | Disable inlining variant C | `sub_12DE8F0` guard |
| +520 | bool | Disable NVIDIA pass A | `sub_1AAC510` guard |
| +560 | bool | Disable NVIDIA pass B | `sub_1AAC510` guard |
| +600 | bool | Disable NVVMVerifier | `sub_12D4560` guard |
| +680 | bool | Disable FunctionAttrs | `sub_1841180` guard |
| +720 | bool | Disable SCCP | `sub_1842BC0` guard |
| +760 | bool | Disable DSE | `sub_18F5480` guard |
| +880 | bool | Disable NVVMReflect | `sub_1857160` guard |
| +920 | bool | Disable IPConstPropagation | `sub_185D600` guard |
| +960 | bool | Disable SimplifyCFG | `sub_190BB10` guard |
| +1000 | bool | Disable InstCombine | `sub_19401A0` guard |
| +1040 | bool | Disable Sink/MemSSA | `sub_1869C50` guard |
| +1080 | bool | Disable PrintModulePass | `sub_17060B0` guard |
| +1120 | bool | Disable NVVMPredicateOpt | `sub_18A3430` guard |
| +1160 | bool | Disable LoopIndexSplit | `sub_1952F90` guard |
| +1240 | bool | Disable LICM | `sub_195E880` guard |
| +1280 | bool | Disable Reassociate | `sub_1B7FDF0` guard |
| +1320 | bool | Disable ADCE variant A | `sub_1C76260` guard |
| +1360 | bool | Disable LoopUnroll | `sub_19C1680` guard |
| +1400 | bool | Disable SROA | `sub_1968390` guard |
| +1440 | bool | Disable EarlyCSE | `sub_196A2B0` guard |
| +1520 | bool | Disable LoopSimplify | `sub_198DF00` guard |
| +1680 | bool | Disable NVIDIA pass | `sub_19CE990` guard |
| +1760 | bool | Disable MemorySpaceOpt | `sub_1C8E680` guard |
| +1840 | bool | Disable ADCE C | `sub_1C6FCA0` guard |
| +1960 | bool | Disable ConstantMerge | `sub_184CD60` guard |
| +2000 | bool | Disable NVVMIntrinsicLowering | `sub_1CB4E40` guard |
| +2040 | bool | Disable MemCpyOpt | `sub_1B26330` guard |
| +2120 | bool | Disable NVVMBranchDist B | `sub_1CB73C0` guard |
| +2200 | bool | Disable GenericToNVVM | `sub_1A02540` guard |
| +2320 | bool | Disable NVVMRematerialization | `sub_1A13320` guard |
| +2440 | bool | Disable NVVMSinking2 | `sub_1CC60B0` guard |
| +2560 | bool | Disable NVVMGenericAddrOpt | `sub_1CC71E0` guard |
| +2600 | bool | Disable NVVMIRVerification | `sub_1A223D0` guard |
| +2640 | bool | Disable NVVMLoopOpt | `sub_18B1DE0` guard |
| +2720 | bool | Disable InstructionSimplify | `sub_1A7A9F0` guard |
| +2840 | bool | Enable ADCE (reversed logic) | `sub_1C6FCA0` |
| +2880 | bool | Enable LICM (reversed logic) | `sub_195E880` |
| +2920 | bool | NVVMLowerBarriers param | `sub_1C98160` |
| +3000 | bool | Extra DeadArgElim trigger | `sub_18FD350` |
| +3040 | bool | Enable CVP | `sub_18EEA90` |
| +3080 | bool | Enable NVIDIA loop pass | `sub_1922F90` |
| +3120 | bool | Address space optimization flag | `sub_1C8E680` param |
| +3160 | bool | Debug dump mode | `sub_17060B0` enable |
| +3200 | bool | Enable advanced NVIDIA group | IPConst/Reflect/SCCP/etc. |
| +3328 | bool | Enable SM-specific passes | Warp/Reduction/Sinking2 |
| +3488 | bool | Enable barrier optimization | `sub_1C98160`, `sub_18E4A00` |
| +3528 | bool | Tier 1 enable | Phase 3 loop |
| +3532 | int | Tier 1 phase threshold | Phase 3 loop |
| +3568 | bool | Tier 2 enable | Phase 3 loop |
| +3572 | int | Tier 2 phase threshold | Phase 3 loop |
| +3608 | bool | Tier 3 enable | Phase 3 loop |
| +3612 | int | Tier 3 phase threshold | Phase 3 loop |
| +3648 | ptr | Language string (`"ptx"/"mid"/"idn"`) | Phase 1 dispatch |
| +3656 | int | Language string length | Phase 1 dispatch |
| +3704 | bool | Late optimization mode | `sub_195E880`, `sub_1C8A4D0` |
| +3904 | bool | Debug: verify after plugins | Phase 3 loop |
| +3944 | bool | Debug: BB naming `"F%d_B%d"` | Phase 8 |
| +4224 | bool | Optimization master switch | Tier 0 gate |
| +4228 | int | Optimization phase threshold | Tier 0 gate |
| +4304 | bool | Device-code flag | Phase 1 `v238` |
| +4384 | bool | Fast-compile / bypass pipeline | Top branch Pipeline A vs B |
| +4464 | bool | Disable late CFG cleanup B | Phase 5 `sub_1654860` |
| +4480 | ptr | SM feature capability | Phase 6: `& 4` = codegen ext |
| +4488 | ptr | Plugin pass array start | Phase 3 loop |
| +4496 | ptr | Plugin pass array end | Phase 3 loop |

## Pass Factory Address Inventory

All unique pass factory addresses called from the pipeline assembler and tier sub-pipelines:

| Function | Address | Size | Role |
|---|---|---|---|
| NVVMVerifier | `sub_12D4560` | many (tiers) | many (tiers) |
| AssumptionCacheTracker | `sub_1361950` | 1 | 1 |
| TargetLibraryInfoWrapperPass | `sub_149CCE0` | 1 | 1 |
| VerifierPass / BasicAA | `sub_14A7550` | 1 | 1 |
| BreakCriticalEdges | `sub_1654860` | 2 | 2 |
| PrintModulePass (debug dump) | `sub_17060B0` | ~30+ | ~30+ |
| InstructionCombining | `sub_1832270` | 2 | 2 |
| TailCallElim / JumpThreading | `sub_1833EB0` | 3 | 3 |
| FunctionAttrs | `sub_1841180` | 3 | 3 |
| SCCP | `sub_1842BC0` | 2 | 2 |
| NVVMReflect | `sub_1857160` | ~8 | ~8 |
| IPConstantPropagation | `sub_185D600` | 3 | 3 |
| Sink (MemorySSA-based) | `sub_1869C50` | 3 | 3 |
| NVVMPredicateOpt variant | `sub_18A3090` | 2 | 2 |
| NVVMPredicateOpt / SelectionOpt | `sub_18A3430` | 2 | 2 |
| NVVMLoopOpt / BarrierOpt | `sub_18B1DE0` | 3 | 3 |
| Sinking2Pass (fast=1 for fc mode) | `sub_18B3080` | 1 | 1 |
| DCE | `sub_18DEFF0` | 4 | 4 |
| NVVMBarrierAnalysis | `sub_18E4A00` | 1 | 1 |
| CorrelatedValuePropagation | `sub_18EEA90` | 3 | 3 |
| DSE | `sub_18F5480` | 2 | 2 |
| DeadArgElimination | `sub_18FD350` | 5 | 5 |
| SimplifyCFG | `sub_190BB10` | 4 | 4 |
| NVIDIA-specific loop pass | `sub_1922F90` | 1 | 1 |
| LoopIndexSplit | `sub_1952F90` | 3 | 3 |
| LICM / LoopRotate | `sub_195E880` | 4 | 4 |
| SROA | `sub_1968390` | 2 | 2 |
| EarlyCSE | `sub_196A2B0` | 2 | 2 |
| LoopUnroll | `sub_197E720` | 1 | 1 |
| LoopSimplify | `sub_198DF00` | 3 | 3 |
| SROA (variant) | `sub_198E2A0` | 1 | 1 |
| InstCombine | `sub_19401A0` | 2 | 2 |
| LoopUnswitch (7 params) | `sub_19B73C0` | 3 | 3 |
| LoopUnroll variant | `sub_19C1680` | 2 | 2 |
| NVIDIA custom pass | `sub_19CE990` | 1 | 1 |
| GenericToNVVM | `sub_1A02540` | 1 | 1 |
| NVVMRematerialization | `sub_1A13320` | 3 | 3 |
| NVVMIRVerification | `sub_1A223D0` | 5+ | 5+ |
| LLVM StandardPassPipeline | `sub_1A62BF0` | ~9 | ~9 |
| LoopIdiomRecognize | `sub_1A68E70` | 1 | 1 |
| InstructionSimplify | `sub_1A7A9F0` | 3 | 3 |
| NVIDIA-specific pass | `sub_1AAC510` | 1 | 1 |
| MemCpyOpt | `sub_1B26330` | 4 | 4 |
| Reassociate | `sub_1B7FDF0` | 3 | 3 |
| TTIWrapperPass | `sub_1BFB520` | 1 | 1 |
| NVVMLateOpt | `sub_1C46000` | 1 | 1 |
| Inliner / AlwaysInline | `sub_1C4B6F0` | 2 | 2 |
| NewGVN / GVNHoist | `sub_1C6E560` | 1 | 1 |
| GVN | `sub_1C6E800` | 2 | 2 |
| ADCE | `sub_1C6FCA0` | 2 | 2 |
| ADCE variant | `sub_1C76260` | 2 | 2 |
| NVVMWarpShuffle | `sub_1C7F370` | 1 | 1 |
| EarlyCSE / GVN variant | `sub_1C8A4D0` | 3 | 3 |
| MemorySpaceOptimization | `sub_1C8E680` | 4 | 4 |
| NVVMLowerBarriers | `sub_1C98160` | 4 | 4 |
| NVVMLowerBarriers variant | `sub_1C98270` | 1 | 1 |
| ProfileSummaryInfo | `sub_1CB0F50` | 1 | 1 |
| NVVMIntrinsicLowering | `sub_1CB4E40` | ~10 | ~10 |
| NVVMBranchDist | `sub_1CB73C0` | 3 | 3 |
| NVVMLowerAlloca | `sub_1CBC480` | 1 | 1 |
| NVVMUnreachableBlockElim | `sub_1CC3990` | 1 | 1 |
| NVVMReduction | `sub_1CC5E00` | 1 | 1 |
| NVVMSinking2 | `sub_1CC60B0` | 3 | 3 |
| NVVMGenericAddrOpt | `sub_1CC71E0` | 1 | 1 |
| NVVMFinalLowering | `sub_1CEBD10` | 1 | 1 |
| NVVMPeephole | `sub_1CEF8F0` | 2 | 2 |
| NVVMAnnotationsProcessor | `sub_215D9D0` | 2 | 2 |

Total unique pass factory addresses: ~55.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| NVVM pass-options init (populates 4,512-byte options struct) | `sub_12D6300` | 125KB | -- |
| Pass-option string writer (24-byte string slot) | `sub_12D6090` | ~100B | -- |
| Pass-option boolean writer (16-byte boolean slot) | `sub_12D6100` | ~80B | -- |
| Pass-option registry lookup (hash-table) | `sub_12D6170` | ~200B | -- |
| Pass-option boolean resolver (default = true) | `sub_12D6240` | ~300B | -- |
| Pass-definition table getter (64-byte stride) | `sub_1691920` | ~50B | -- |
| String-to-int64 parser | `sub_16D2BB0` | ~100B | -- |
| Pipeline assembler (master) | `sub_12E54A0` | 49.8KB | 8-phase pipeline construction |
| AddPass | `sub_12DE0B0` | 3.5KB | Hash-table-based insertion |
| Tier 0 sub-pipeline | `sub_12DE330` | 4.8KB | ~40 passes, full optimization |
| Tier 1/2/3 sub-pipeline | `sub_12DE8F0` | 17.9KB | Phase-conditional, incremental |
| Codegen dispatch | `sub_12DFE00` | 20.7KB | Dependency-ordered codegen |
| Phase I/II orchestrator | `sub_12E7E70` | 9.4KB | Two-phase state machine |
| New PM registration | `sub_2342890` | ~50KB | 2,816 lines, 35 NVIDIA + ~350 LLVM |
| registerPass (hash insert) | `sub_E41FB0` | ~300B | StringMap insertion |
| Pass name prefix matcher | `sub_2337DE0` | ~100B | starts_with comparison |
| Parameterized pass parser | `sub_234CEE0` | ~200B | Extracts `<params>` |
| MemorySpaceOpt param parser | `sub_23331A0` | ~300B | first-time/second-time/warnings |
| New PM pipeline driver | `sub_226C400` | 35KB | nvopt<O0/O1/O2/O3/Ofcmax/Ofcmid/Ofcmin> selection |
| New PM text parser (`buildDefaultPipeline`) | `sub_2277440` | 60KB | Parses pipeline name strings |
| nvopt registration (new PM) | `sub_225D540` | ~32KB | Pipeline element vtable at `0x4A08350` |
| nvopt registration (legacy PM) | `sub_12C35D0` | ~500B | Pipeline element vtable at `0x49E6A58` |
| nvopt object initializer | `sub_12EC960` | ~100B | Creates 512-byte pipeline object |
| LLVM standard pipeline factory | `sub_1A62BF0` | varies | Pipeline IDs 1,2,4,5,7,8 |
| Pass registry check | `sub_163A1D0` | ~100B | Pass registration status |
| Pass status update | `sub_163A340` | ~100B | Used in codegen dispatch |
| Pipeline text tokenizer | `sub_2352D90` | ~200B | Tokenizes `nvopt<>` strings |

## Reimplementation Checklist

1. **Two-phase compilation model.** Implement a TLS phase variable (values 1=Phase I, 2=Phase II, 3=done) read by individual passes to skip themselves when the current phase does not match their intended execution phase. Phase I runs whole-module analysis; Phase II runs per-function codegen-oriented passes.
2. **Pipeline assembly function (~150 AddPass calls).** Build the master pipeline at runtime using hash-table-based pass insertion (`AddPass`), with language-specific dispatch (paths for `"ptx"`, `"mid"`, and default), tier-based interleaving (Tiers 0--3 fired by accumulated pass-count thresholds), and phase-conditional pass inclusion.
3. **NVVMPassOptions system (4,512-byte struct, 221 slots).** Implement the proprietary per-pass enable/disable and parametric knob system with 114 string + 100 boolean + 6 integer + 1 string-pointer option slots, parsed from CLI flags and routed to individual passes.
4. **Concurrent per-function compilation.** After Phase I completes on the whole module, split Phase II across a thread pool sized to `get_nprocs()` or GNU Jobserver token count, with per-function bitcode extraction, independent compilation, and re-linking of results.
5. **GNU Jobserver integration.** Parse `--jobserver-auth=R,W` from `MAKEFLAGS` environment variable, create a token management pipe, and spawn a pthread to throttle concurrent compilations to the build system's `-j` level.
6. **Split-module compilation.** Implement the `-split-compile=N` mechanism: decompose multi-function modules into per-function bitcode blobs via filter callbacks, compile each independently (potentially in parallel), re-link results, and restore linkage attributes from a hash table.
7. **Tier 0 full optimization sub-pipeline.** Assemble the ~40-pass Tier 0 sequence: BreakCriticalEdges, GVN, NVVMReflect, SCCP, NVVMVerifier, LoopIndexSplit, ADCE, LICM, LoopUnroll, InstCombine, SROA, EarlyCSE, LoopUnswitch, SimplifyCFG, NVVMRematerialization, DSE, DCE, with per-pass NVVMPassOptions gating.

## Cross-References

- [Optimization Levels](../config/optimization-levels.md) -- detailed O0/O1/O2/O3 and fast-compile pipeline construction
- [Memory Space Optimization](../passes/memory-space-opt.md) -- the MemorySpaceOpt pass (first-time/second-time parameterization)
- [Rematerialization](../passes/rematerialization.md) -- NVVMRematerialization pass and its register-pressure knobs
- [Loop Strength Reduction](../llvm/lsr.md) -- NVIDIA's custom LSR overlay with 11 GPU-specific knobs
- [Sinking2](../passes/sinking2.md) -- NVIDIA's enhanced sinking pass
- [CGSCC & LazyCallGraph](../infra/lazycallgraph.md) -- the inliner framework and iteration model
- [Pipeline Entry](entry.md) -- top-level compilation entry and two-phase orchestration
- [SROA](../llvm/sroa.md), [EarlyCSE](../llvm/early-cse.md), [JumpThreading](../llvm/jump-threading.md) -- scalar pass details (hub: [scalar-passes](../llvm/scalar-passes.md))
