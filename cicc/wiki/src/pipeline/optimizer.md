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
| Integer | 16B | `sub_16D2BB0` (parseInt) | 6 |
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
// Written by sub_12D6090 (writeStringOption)
struct StringSlot {        // 24 bytes
    char*   value_ptr;     // +0: pointer to string value
    int32_t option_index;  // +8: 1-based slot index
    int32_t flags;         // +12: from PassDef byte+40
    int32_t opt_level;     // +16: optimization level context
    int32_t pass_id;       // +20: resolved via sub_1691920
};

// TYPE B: Boolean compact (83 instances)
// Written by sub_12D6100 (writeBoolOption)
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
// Value parsed by sub_16D2BB0 (parseInt)
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

1. **`sub_12D6170` (PassOptionRegistry::lookupOption)** -- looks up a slot index in the hash table at `registry+120`. Returns a pointer to an `OptionNode` struct: `[+40] int16 flags`, `[+48] qword* value_array_ptr`, `[+56] int value_count`. Returns null if the option was not set on the command line.

2. **`sub_12D6240` (getBoolOption)** -- resolves a boolean option. Calls `sub_12D6170` to find the option, then if a string value exists, lowercases it via `sub_16D2060` and tests if the first char is `'1'` (0x31) or `'t'` (0x74). If the option was not found, defaults to true (enabled). Returns the boolean packed with the flags in the low 40 bits.

3. **`sub_1691920` (PassDefTable::getPassDef)** -- looks up a PassDef entry in a table where each entry is 64 bytes. Computes: `table[0] + (index - 1) * 64`. The PassDef at `[+32]` holds the pass_id, at `[+36]` a `has_overrides` flag, and at `[+40]` an override index.

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

- **Slots 160-162**: Three consecutive string slots with no interleaved boolean. This represents a pass (likely MemorySpaceOpt or the CSSA pass) that takes three string configuration parameters followed by a single boolean enable flag at slot 163.
- **Slots 192-193**: Two consecutive boolean slots. One is the main enable toggle; the other appears to be a sub-feature flag (both default to disabled).
- **Slot 181 (offset 3648)**: The only `STRING_PTR` type. Its default is `byte_3F871B3` (an empty string in `.rodata`). The raw pointer + length storage suggests this holds a file path or regex pattern for pass filtering.
- **Slots 196-207**: Alternating string + integer slots instead of string + boolean. This high-numbered region contains all six integer options, likely controlling late-pipeline passes with numeric thresholds (unroll counts, live-variable limits, iteration bounds).

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

### Tier 1/2/3 Incremental Additions

`sub_12DE8F0(PM, tier, opts)` adds passes incrementally. It first stores the tier number into `qword_4FBB410` (the tier tracker global), then checks the phase counter `qword_4FBB3B0` for phase-dependent behavior.

**Tier 1 (baseline)** adds a conservative set: NVVMIntrinsicLowering (twice, at different points), NVVMIRVerification, NVVMVerifier, and -- when `opts[3200]` (advanced optimization flag) is set -- IPConstPropagation, NVVMReflect, SCCP, ConstantMerge, LoopRotate, and LICM. Tier 1 explicitly *skips* SimplifyCFG, LoopIndexSplit, EarlyCSE, and Sink (these are gated by `tier != 1`).

**Tier 2** adds everything from Tier 1 plus the passes that Tier 1 skips: SimplifyCFG, LoopIndexSplit (with threshold -1), EarlyCSE, Sink, ADCE, LoopUnroll, InstCombine, SROA, LoopUnswitch, NVVMRematerialization, DSE, DCE, MemorySpaceOpt, NVVMGenericAddrOpt, ADCE, and LoopOpt/BarrierOpt.

**Tier 3** adds the most aggressive passes on top of Tier 2: TailCallElim (gated by `tier == 3`), NVVMReflect (run again at late position for `tier == 3`), NVVMLateOpt, and when `qword_4FBB370` byte 4 was previously zero, it sets the feature flag register to 6 (enabling both barrier optimization and memory space optimization gates).

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

### The AddPass Mechanism

`sub_12DE0B0` is the hash-table-based pass insertion function that all pipeline assembly calls go through:

```c
// Pseudocode for sub_12DE0B0
void AddPass(PassManager* PM, Pass* pass, uint8_t flags, char barrier) {
    // 1. Hash the pass pointer for dedup/lookup
    uint64_t hash = (pass >> 9) ^ (pass >> 4);

    // 2. Open-addressing insert into hash table at PM+80
    //    Stores pass pointer + (flags | (barrier ? 2 : 0))
    hashtable_insert(PM + 80, hash, pass, flags | (barrier << 1));

    // 3. Append to dynamic pass array at PM[0]
    //    8-byte slots, count at PM+8
    PM->passes[PM->count++] = pass;
}
```

The `flags` parameter encodes the pass type: `0` for module/analysis passes, `1` for function passes. The `barrier` parameter is a scheduling hint that marks the pass as requiring all preceding passes to complete before it runs -- used for passes that must see the module in a consistent state.

### Complete Construction Sequence

The full pipeline construction in `sub_12E54A0` proceeds through eight phases:

**Phase 0 (Infrastructure):** Always runs. Adds TargetLibraryInfo, TargetTransformInfo, Verifier, AssumptionCacheTracker, and ProfileSummaryInfo. These are analysis passes that later optimization passes depend on.

**Phase 1 (Language dispatch):** Reads `a4[3648]` (language string). Three paths exist:
- `"ptx"`: Light pipeline (~15 passes) for already-lowered PTX text input
- `"mid"`: Full pipeline (~45 passes) for mid-level IR from the frontend
- Default: General pipeline (~40 passes) for bitcode from external sources

**Phase 2 (Pre-optimization):** Adds early passes gated by per-pass disable flags. NVVMReflect, DeadArgElim, NVVMVerifier, ConstantMerge, and the AlwaysInliner all run here if not disabled.

**Phase 3 (Main optimization loop):** The tier-threshold-driven loop described above. External/plugin passes interleave with Tier 0/1/2/3 sub-pipelines.

**Phase 4 (Post-opt language paths):** After the main loop, language-specific post-optimization runs. The "mid" path adds NVVMBranchDist and NVVMRematerialization. The default path adds LoopIndexSplit, NVVMSinking2, and final CGSCC passes.

**Phase 5 (Finalization):** Always runs. Adds NVVMLowerBarriers (conditional on `a4[3488]`), NVVMFinal cleanup via `sub_1CEBD10`, BreakCriticalEdges, and the codegen dispatch via `sub_12DFE00`.

**Phase 6 (Phase II codegen check):** Reads `qword_4FBB3B0`. If phase == 2 and `a4[4480] & 4` is set, enters a special codegen extension block that calls target machine hooks for phase-II-specific machine pass insertion.

**Phase 7 (Execution):** Calls `sub_160FB70` (PassManager::run) followed by `sub_1619BD0` (PassManager::finalize) and cleanup.

**Phase 8 (BB naming):** When `a4[3944]` (debug mode) is set, iterates all functions and basic blocks, naming each block `"F%d_B%d"` for debugging.

### Codegen Dispatch — `sub_12DFE00`

The codegen dispatch at `sub_12DFE00` does not simply append passes. It performs a dependency analysis over the entire pass pipeline built so far:

1. Reads the optimization level from `opts[200]`: level 0 means minimal codegen; level >1 enables dependency tracking
2. Iterates all passes in the PassManager, calling `vtable+112` (pass->isCodeGenOnly) to identify codegen-specific passes
3. Calls `vtable+16` (pass->getAnalysisUsage) to build a dependency graph in a secondary hash table
4. For passes with ordering constraints (depends on later codegen passes), establishes explicit ordering edges
5. Finally calls the SubtargetInfo hook (`vtable+16`) to emit the codegen passes in dependency-respecting order

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

## Function Map

| Address | Identity | Size | Notes |
|---------|----------|------|-------|
| `sub_12D6300` | NVVMPassOptions::init | 125KB | Populates 4,512-byte options struct |
| `sub_12D6090` | writeStringOption | ~100B | Writes 24-byte string slot |
| `sub_12D6100` | writeBoolOption | ~80B | Writes 16-byte boolean slot |
| `sub_12D6170` | PassOptionRegistry::lookupOption | ~200B | Hash table lookup |
| `sub_12D6240` | getBoolOption | ~300B | Boolean resolution with default |
| `sub_1691920` | PassDefTable::getPassDef | ~50B | 64-byte stride table lookup |
| `sub_16D2BB0` | parseInt | ~100B | String to int64 |
| `sub_12E54A0` | Pipeline assembler (master) | 49.8KB | 8-phase pipeline construction |
| `sub_12DE0B0` | AddPass | 3.5KB | Hash-table-based insertion |
| `sub_12DE330` | Tier 0 sub-pipeline | 4.8KB | ~40 passes, full optimization |
| `sub_12DE8F0` | Tier 1/2/3 sub-pipeline | 17.9KB | Phase-conditional, incremental |
| `sub_12DFE00` | Codegen dispatch | 20.7KB | Dependency-ordered codegen |
| `sub_12E7E70` | Phase I/II orchestrator | 9.4KB | Two-phase state machine |
| `sub_2342890` | New PM registration | ~50KB | 2,816 lines, 35 NVIDIA + ~350 LLVM |
| `sub_E41FB0` | registerPass (hash insert) | ~300B | StringMap insertion |
| `sub_2337DE0` | Pass name prefix matcher | ~100B | starts_with comparison |
| `sub_234CEE0` | Parameterized pass parser | ~200B | Extracts `<params>` |
| `sub_23331A0` | MemorySpaceOpt param parser | ~300B | first-time/second-time/warnings |
| `sub_226C400` | New PM pipeline driver | ~2KB | nvopt<O0/O1/O2/O3> selection |
| `sub_2277440` | New PM text parser | ~5KB | Parses pipeline name strings |
| `sub_225D540` | nvopt registration (new PM) | ~500B | Pipeline element vtable |
| `sub_12C35D0` | nvopt registration (legacy PM) | ~500B | Pipeline element vtable |

## Cross-References

- [Optimization Levels](../config/optimization-levels.md) -- detailed O0/O1/O2/O3 and fast-compile pipeline construction
- [Memory Space Optimization](../nvidia/memspaceopt.md) -- the MemorySpaceOpt pass (first-time/second-time parameterization)
- [Rematerialization](../nvidia/remat.md) -- NVVMRematerialization pass and its register-pressure knobs
- [Loop Strength Reduction](../llvm/lsr.md) -- NVIDIA's custom LSR overlay with 11 GPU-specific knobs
- [Sinking2](../nvidia/sinking2.md) -- NVIDIA's enhanced sinking pass
- [CGSCC & LazyCallGraph](../infra/lazycallgraph.md) -- the inliner framework and iteration model
- [Pipeline Entry](entry.md) -- top-level compilation entry and two-phase orchestration
- [Scalar Passes](../llvm/scalar-passes.md) -- SROA, EarlyCSE, JumpThreading details
