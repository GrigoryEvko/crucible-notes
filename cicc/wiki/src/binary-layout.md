# Binary Layout

This page is a visual guide to navigating the cicc v13.0 binary in IDA Pro. It covers the ELF structure, section layout, subsystem address ranges, embedded data payloads, and the statically linked jemalloc allocator. If you are opening this binary for the first time, start here to orient yourself before diving into individual subsystems.

## ELF Overview

CICC is a statically linked, stripped x86-64 ELF binary. There are no dynamic symbol tables, no `.dynsym`, no DWARF debug info, and no export table. Every function name was removed at build time. IDA Pro recovers 80,562 functions; Hex-Rays successfully decompiles 80,281 of them (99.65%).

| Property | Value |
|---|---|
| File size | 60,108,328 bytes (57.3 MB) |
| Architecture | x86-64, little-endian |
| Linking | Fully static (no `.interp`, no PLT/GOT) |
| Stripped | Yes, all symbol tables removed |
| Build ID | `cuda_13.0.r13.0/compiler.36424714_0` |
| Compiler | Built with GCC (inferred from CRT stubs and `.init_array` layout) |
| Allocator | jemalloc 5.3.x, statically linked (~400 functions) |

Because the binary is statically linked, libc, libpthread, and libm are all embedded. This inflates the raw function count but also means every call target resolves to a concrete address within the binary itself -- there are no external dependencies at runtime beyond the kernel syscall interface.

## Address Space Map

The binary's `.text` section spans roughly `0x400000` to `0x3C00000`. Within that 56 MB range, subsystems occupy contiguous, non-overlapping regions. The map below is the primary orientation tool for IDA Pro navigation.

```
0x400000 ┌─────────────────────────────────────────┐
         │  CRT startup + libc stubs               │  ~52 KB
0x40D000 ├─────────────────────────────────────────┤
         │  jemalloc stats / vsnprintf              │  ~80 KB
0x420000 ├─────────────────────────────────────────┤
         │  (gap: misc libc, math, string ops)      │  ~64 KB
0x430000 ├─────────────────────────────────────────┤
         │  Global constructors (cl::opt reg)        │  ~1.6 MB
         │  ~1,689 LLVM command-line option objects  │
0x5D0000 ├─────────────────────────────────────────┤
         │  EDG 6.6 C++ Frontend                    │  3.2 MB
         │  Parser, constexpr evaluator, IL walker   │
0x8F0000 ├─────────────────────────────────────────┤
         │  CLI / Real Main / NVVM Bridge            │  520 KB
         │  sub_8F9C90 (real main), dual-path dispatch│
0x960000 ├─────────────────────────────────────────┤
         │  Architecture detection, NVVM options     │  576 KB
0x9F0000 ├─────────────────────────────────────────┤
         │  Bitcode reader (parseFunctionBody)       │  ~1 MB
0xAF0000 ├─────────────────────────────────────────┤
         │  X86 AutoUpgrade (legacy, 457KB fn)       │  ~1 MB
0xBF0000 ├─────────────────────────────────────────┤
         │  LLVM IR Verifier                        │  500 KB
0xC00000 ├─────────────────────────────────────────┤
         │  LLVM Support / ADT library              │  ~3.2 MB
         │  (see detailed sub-map below)             │
0x12D0000├─────────────────────────────────────────┤
         │  PassManager / NVVM bridge                │  4.2 MB
         │  Pipeline assembly (sub_12E54A0)          │
0x12FC000├ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┤
         │  jemalloc core (~400 functions)           │  ~256 KB
0x1700000├─────────────────────────────────────────┤
         │  Backend / machine passes                 │  8 MB
         │  RegAlloc, Block Remat, Mem2Reg           │
0x1F00000├─────────────────────────────────────────┤
         │  SelectionDAG                            │  2 MB
         │  LegalizeTypes (348KB), LegalizeOp        │
0x2100000├─────────────────────────────────────────┤
         │  NVPTX PTX emission                      │  1 MB
0x2340000├─────────────────────────────────────────┤
         │  New PM / pass registration               │  768 KB
         │  2,816-line registrar at sub_2342890      │
0x2A00000├─────────────────────────────────────────┤
         │  Loop passes                             │  4 MB
         │  LoopVectorize, SLP, Unroll               │
0x3000000├─────────────────────────────────────────┤
         │  NVPTX ISel + lowering                    │  7 MB
         │  343KB intrinsic switch (sub_33B0210)     │
0x3700000├─────────────────────────────────────────┤
         │  Machine-level passes (tail)              │  ~3 MB
         │  BlockPlacement, Outliner, StructurizeCFG │
0x3A00000├─────────────────────────────────────────┤
         │  (trailing code, CRT finalization)        │
         └─────────────────────────────────────────┘

DATA SECTIONS:
0x3EA0080   Embedded libdevice bitcode (Path A)    456 KB
0x420FD80   Embedded libdevice bitcode (Path B)    456 KB
0x4F00000+  Global BSS (cl::opt storage, hash tables, state)
```

## Detailed Subsystem Map at Pass Granularity

The coarse map above partitions the binary into ~18 zones. The following map refines every zone to individual-pass resolution, giving the factory address of each identified pass or subsystem entry point. Addresses prefixed with `sub_` are IDA function names. Sizes in parentheses are decompiled C output; actual machine code is typically 2-3x smaller.

### Zone 1: CRT, libc, jemalloc stats (0x400000 - 0x42FFFF)

```
0x400000   _start / CRT entry (ELF entry point)
0x40D5CA   sub_40D5CA   vsnprintf (jemalloc stats formatting)
0x420000   libc math/string helpers (memcpy, memset, strlen, etc.)
```

No LLVM or NVIDIA code lives here. Pure runtime support.

### Zone 2: Global constructors (0x430000 - 0x5CFFFF)

~1,689 `cl::opt` registration constructors execute before `main()`. Each registers a command-line option string, description, default value, and storage pointer into the global option registry. The `.init_array` section holds function pointers to these constructors.

### Zone 3: EDG 6.6 C++ Frontend (0x5D0000 - 0x8EFFFF)

The complete Edison Design Group C++ frontend, version 6.6. Contains the lexer, parser, constexpr evaluator, template instantiator, overload resolver, IL walker/copier, diagnostic engine, SARIF output, and CUDA-specific extensions (kernel launch grammar, `__shared__`/`__device__` memory space parsing, atomic builtin stubs).

| Address | Size | Identity |
|---|---|---|
| `sub_5D2A80` | | EDG main entry (called from real main) |
| `sub_610000`-`sub_62FFFF` | 128 KB | Expression parser core |
| `sub_750000`-`sub_76FFFF` | 128 KB | Declaration processing |
| `sub_840000`-`sub_87FFFF` | 256 KB | Template / constexpr |
| `sub_880000`-`sub_8EFFFF` | 448 KB | SARIF, diagnostics, keywords |

### Zone 4: CLI / Real Main / Dual-Path Entry (0x8F0000 - 0x9EFFFF)

| Address | Size | Identity |
|---|---|---|
| `sub_8F9C90` | | Real main (after CRT/jemalloc init) |
| `sub_900130` | | Path A CLI parsing (LibNVVM API mode) |
| `sub_902D10` | | Path A simple compile entry |
| `sub_905EE0` | 43 KB | Path A multi-stage pipeline |
| `sub_90AEE0` | 109 KB | Path A builtin resolution table |
| `sub_960000`-`sub_9EFFFF` | 576 KB | Architecture detection, NVVM option parsing |

### Zone 5: Bitcode Reader / X86 AutoUpgrade / Verifier (0x9F0000 - 0xBFFFFF)

| Sub-range | Contents |
|---|---|
| `0x9F0000`-`0xAEFFFF` | Bitcode reader (`sub_A24000` parseFunctionBody ~166KB) |
| `0xAF0000`-`0xBEFFFF` | X86 AutoUpgrade (`sub_A939D0` 457KB -- legacy intrinsic upgrader) |
| `0xBF0000`-`0xBFFFFF` | LLVM IR Verifier entry points |

### Zone 6: LLVM Support Library (0xC00000 - 0xCAFFFF)

1,653 functions. Pure LLVM infrastructure -- no NVIDIA-specific modifications except a single `!Flat` address space annotation in the sample profile reader at `sub_C29E70`.

| Sub-range | Functions | Contents |
|---|---|---|
| `0xC00000`-`0xC0F000` | 65 | **IR Verifier** (`sub_C05FA0` visitInstruction 75KB, `sub_C0A940` verify 12KB) |
| `0xC0D4F0` | 1 | `sub_C0D4F0` TargetRegistry::lookupTarget (8KB) |
| `0xC0F6D0` | 1 | `sub_C0F6D0` IR module linker (48KB) |
| `0xC10000`-`0xC2FFFF` | ~400 | InstrProf reader, Sample Profile reader/writer, hashing |
| `0xC30000`-`0xC3FFFF` | 214 | ImmutableMap/Set, APInt printing |
| `0xC40000`-`0xC4FFFF` | 197 | **APInt core arithmetic** (div, mul, shift) |
| `0xC50000`-`0xC5FFFF` | 141 | **CommandLine parser** (`cl::opt` infrastructure) |
| `0xC60000`-`0xC6FFFF` | 135 | JSON parser, debug counters, error handling |
| `0xC70000`-`0xC7FFFF` | 114 | **ConstantRange arithmetic** |
| `0xC80000`-`0xC8FFFF` | 194 | SHA-1 hash, regex, SmallVector, sorting |
| `0xC90000`-`0xC9FFFF` | 139 | Timer/profiling, TimeTrace (Chrome trace) |
| `0xCA0000`-`0xCAFFFF` | 186 | YAML lexer/parser, TypeSize, VFS |

### Zone 7: NVVM Container, SCEV, DWARF, MC Layer (0xCB0000 - 0x10CFFFF)

This 4 MB zone contains LLVM mid-level infrastructure and the NVVM container format.

| Sub-range | Contents | Key functions |
|---|---|---|
| `0xCB0000`-`0xCBFA60` | YAML parser/emitter (libyaml) | `sub_CB9640` main parser (26KB) |
| `0xCC0130`-`0xCCABA0` | LLVM Triple parsing | `sub_CC0130` Triple_normalize (35KB) |
| `0xCCBB10`-`0xCDCA30` | **NVVM container format** | `sub_CDD2D0` serialize, `sub_CD1D80` deserialize, `sub_CCD5F0` version validator (9KB) |
| `0xCD9990` | | NVVM options parser (calls 60+ parse helpers) |
| `0xD60000`-`0xD82000` | **NV Module Summary / LTO** | `sub_D7D4E0` buildModuleSummary (74KB), `sub_D81040` runOnModule (56KB) |
| `0xD83000`-`0xDFD000` | **ScalarEvolution (SCEV)** | SCEV framework, AddRecExpr, backedge analysis |
| `0xE00000`-`0xE0FFFF` | DWARF debug info string/enum tables | |
| `0xE10000`-`0xE2FFFF` | Itanium C++ name demangler | `sub_E18BB0` parseExpr (47KB) |
| `0xE30000`-`0xEBFFFF` | MC assembler layer | ELF/COFF/MachO section parsers, expression evaluator |
| `0xEC0000`-`0xED0000` | MC assembler directives | `sub_ECB300` ELF section parser (40KB) |
| `0xED0000`-`0xEF8000` | InstrProf / MemProf reader | Profiling data infrastructure |
| `0xEF8000`-`0xF05000` | Bitstream remark serialization | |
| `0xF05000`-`0xF6FFFF` | **SelectionDAG infrastructure** | DAG node creation, SDValue, EVT/MVT helpers |
| `0xF70000`-`0xF8FFFF` | **Loop vectorization runtime checks** | `sub_F77B70` vectorizeLoop (37KB), `sub_F72730` canVectorizeMemory (29KB) |
| `0xF90000`-`0xFCFFFF` | **SimplifyCFG + code sinking** | `sub_FB0000` switch table gen, `sub_FA0000` speculative exec |
| `0xFD0000`-`0xFEFFFF` | AliasSet, register pressure tracking, CFG graphviz | |
| `0xFF0000`-`0x101FFFF` | Block scheduling, RPO traversal, constant folding | |
| `0x1020000`-`0x103FFFF` | **Inline ASM + scheduling model** | `sub_1035170` CUTLASS kernel detection (41KB) |
| `0x1040000`-`0x106FFFF` | Divergence analysis, DAG utilities, IR linker | |
| `0x1070000`-`0x10AFFFF` | MC object emission, **InstructionSimplify** | `sub_10ACA40` visitAdd (94KB) |

### Zone 8: InstCombine Mega-Region (0x10D0000 - 0x122FFFF)

The single largest contiguous pass in the binary. NVIDIA's modified InstCombine spans 1.4 MB of code with three NVIDIA-custom opcodes (`0x254D`, `0x2551`, `0x255F`) for proprietary intrinsic folding.

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x10D0000`-`0x10EFFFF` | InstCombine visitors (casts, shifts, memory) | Various visitXxx functions |
| `0x10EE7A0` | **InstCombine main visitor** | `sub_10EE7A0` (405KB / 9,258 lines -- largest function in binary) |
| `0x10F0000`-`0x1100000` | Sub-visitors for specific opcodes | |
| `0x1100000`-`0x1170000` | Intrinsic folding, demanded bits | `sub_1169C30` intrinsic folder (87KB), `sub_11A7600` computeKnownBits (127KB) |
| `0x1180000`-`0x119FFFF` | InstCombine core worklist | `sub_1190310` main dispatch (88KB) |
| `0x11A0000`-`0x11AFFFF` | **ValueTracking / KnownBits** | `sub_11AE870` SimplifyDemandedBits |
| `0x11B0000`-`0x11BFFFF` | InstCombine tail (vector, extract/insert) | |
| `0x11D0000`-`0x11FFFFF` | **SimplifyLibCalls** | Math function optimization |
| `0x11FF000`-`0x122FFFF` | LLVM textual IR parser (LLParser) | |

### Zone 9: NVVM Bridge / Builtin System / IR Codegen (0x1230000 - 0x12CFFFF)

This zone is the core NVIDIA bridge between the EDG frontend AST and the LLVM IR optimizer.

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x1230000`-`0x125FFFF` | LLVM IR codegen from AST | Expression, statement, type codegen |
| `0x125FB30` | Path B CLI parsing | `sub_125FB30` (standalone/nvcc mode) |
| `0x1262860` | Path B simple compile | `sub_1262860` |
| `0x1265970` | Path B multi-stage pipeline | `sub_1265970` (48KB) |
| `0x126A7B0` | Builtin lookup helper | `sub_126A7B0` |
| `0x126A910` | **Builtin registration table** | `sub_126A910` (126KB) -- registers 717 builtins (IDs 1-770) |
| `0x12B3FD0` | **Builtin resolution dispatch** | `sub_12B3FD0` (103KB) -- giant switch on builtin ID |
| `0x12C06E0` | **Bitcode linker** | `sub_12C06E0` (libdevice linking) |

### Zone 10: Pipeline Builder / Pass Options (0x12D0000 - 0x12FFFFF)

The pipeline assembler constructs the complete LLVM pass pipeline, inserting passes by calling factory functions whose addresses scatter across the entire binary.

| Address | Size | Identity |
|---|---|---|
| `sub_12D3E60` | | Module split-range helper |
| `sub_12D4560` | 325 B | Pass factory: creates NVIDIA custom pass |
| `sub_12D6300` | 125 KB | **NVVMPassOptions initializer** -- populates 222 pass option slots into 4,480-byte struct |
| `sub_12DE0B0` | 3.5 KB | **AddPass** -- hash-table-based pass insertion into pipeline |
| `sub_12DE330` | 4.8 KB | **Tier 0** sub-pipeline builder (full optimization, 40 passes) |
| `sub_12DE8F0` | | **Tier 1/2/3** sub-pipeline builder (85-pass superset, tier-gated) |
| `sub_12DFE00` | | **Codegen dispatch** -- routes to backend machine pass pipeline |
| `sub_12E54A0` | 49.8 KB | **Master pipeline assembler** -- 1,553 lines, two major pipelines (normal + fast) |
| `sub_12EB010` | | Machine pass assembly (Pipeline B fast path) |
| `sub_12EC4F0` | | Machine codegen execution |
| `sub_12FC000`+ | ~256 KB | **jemalloc core** (~400 functions) |
| `sub_12FCDB0` | 129 KB | `malloc_conf_init` (parses 199 config strings from `MALLOC_CONF`) |

### Zone 11: IR Infrastructure / PassManager (0x1300000 - 0x16FFFFF)

Dense LLVM infrastructure: IR types, constants, instructions, metadata, use-lists, PassManager execution engine, IR linker, bitcode reader, regex, and DataLayout.

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x1300000`-`0x135FFFF` | IR constants, types, APInt, APFloat | |
| `0x1360000`-`0x13FFFFF` | IR instructions, basic blocks, functions | `sub_1361950` AssumptionCacheTracker |
| `0x1400000`-`0x14FFFFF` | TargetLibraryInfo, pass scheduling | `sub_149CCE0` TLI wrapper, `sub_14A04B0` TLI creation, `sub_14A3CD0` NVPTX TargetPassConfig |
| `0x1500000`-`0x15FFFFF` | IR builder, GEP, PHI, branch creation | `sub_15F83E0` conditional branch, `sub_15F9210` load, `sub_15F9650` store |
| `0x1600000`-`0x160FFFF` | PassManager execution engine | `sub_160FB70` PassManager::run, `sub_1611EE0` PassManagerBuilder init |
| `0x1610000`-`0x162FFFF` | Pass scheduling, metadata RAUW | `sub_1619140` register target passes, `sub_1619BD0` PassManager::finalize |
| `0x1630000`-`0x16FFFFF` | **IR Linker**, bitcode reader, regex | `sub_16786A0` IRLinker::run (61KB), `sub_166A310` parseFunctionBody (60KB) |

### Zone 12: InstCombine (NewPM) + Sanitizers + PGO (0x1700000 - 0x17FFFFF)

946 functions. Dominated by the New Pass Manager version of InstCombine (~600 functions, ~3.5 MB decompiled), with sanitizer instrumentation (MSan, TSan, coverage) and PGO/GCov infrastructure.

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x1700000`-`0x17B0000` | **InstCombine (NewPM)** | `sub_1743DA0` main visitor (168KB), `sub_17A9010` liveness (111KB) |
| `0x17B0000`-`0x17BFFFF` | GCov instrumentation | `sub_17BF860` coverage notes (53KB) |
| `0x17C0000`-`0x17CFFFF` | PGO indirect-call promotion | `sub_17C2DB0` (39KB) |
| `0x17D0000`-`0x17DFFFF` | MemorySanitizer | `sub_17DDCE0` shadow propagation (58KB) |
| `0x17E0000`-`0x17EFFFF` | PGO instrumentation | `sub_17EEF60` InstrProfiling reader (81KB) |
| `0x17F0000`-`0x17FFFFF` | ThreadSanitizer, SanitizerCoverage | `sub_17FF260` TSan entry (51KB), `sub_17F91F0` SanCov (44KB) |
| `sub_17060B0` | | **PrintModulePass** (debug dump, inserted ~30x in pipeline) |

### Zone 13: GVN + Scalar Passes + NVIDIA Custom IR Passes (0x1800000 - 0x1CFFFFF)

This 5 MB zone contains the bulk of LLVM's scalar optimization passes and all of NVIDIA's custom IR-level passes.

**GVN family (0x1900000 - 0x193FFFF):**

| Address | Size | Identity |
|---|---|---|
| `sub_1900BB0` | 83 KB | GVN::runOnFunction (core fixed-point iteration) |
| `sub_1906720` | 26 KB | GVN PRE (Partial Redundancy Elimination) |
| `sub_1930810` | 3 KB | NewGVN expression printing |
| `sub_1933B40` | 43 KB | NewGVN core value numbering |

**Standard scalar passes (0x1830000 - 0x1AFFFFF):**

| Address | Size | Identity (pipeline factory call) |
|---|---|---|
| `sub_1832270` | | InstructionCombining (Old PM wrapper) |
| `sub_1833EB0` | | TailCallElim / JumpThreading |
| `sub_1841180` | | FunctionAttrs |
| `sub_1842BC0` | | SCCP (Sparse Conditional Constant Propagation) |
| `sub_184CD60` | | ConstantMerge / GlobalDCE |
| `sub_1857160` | | **NVVMReflect** |
| `sub_185D600` | | IPConstantPropagation / ArgumentPromotion |
| `sub_1869C50` | | Sink / MemorySSA |
| `sub_18A3430` | | NVVMPredicateOpt / SelectionOpt |
| `sub_18B1DE0` | | LoopPass (barrier optimization) |
| `sub_18DEFF0` | | DCE (Dead Code Elimination) |
| `sub_18EEA90` | | CorrelatedValuePropagation |
| `sub_18F5480` | | DSE (Dead Store Elimination) |
| `sub_18FD350` | | DeadArgumentElimination |
| `sub_190BB10` | | SimplifyCFG |
| `sub_195E880` | | LICM / LoopRotate |
| `sub_1952F90` | | LoopIndexSplit |
| `sub_197E720` | | LoopUnroll / LoopVectorize |
| `sub_198DF00` | | LoopSimplify / IndVarSimplify |
| `sub_198E2A0` | | SROA (Scalar Replacement of Aggregates) |
| `sub_19401A0` | | InstCombine variant |
| `sub_19B73C0` | | SROA variant / LoopUnswitch |
| `sub_19CE990` | | NVIDIA pass (unknown) |
| `sub_1A13320` | | **NVVMRematerialization** (IR-level remat) |
| `sub_1A223D0` | | **NVVMIRVerification** |
| `sub_1A62BF0` | | **LLVM standard pass pipeline** (parameterized, called ~8x with different configs) |
| `sub_1A68E70` | | LoopIdiomRecognize / IndVarSimplify |
| `sub_1A7A9F0` | | InstructionSimplify / ValueTracking |

**Loop unrolling + switch lowering (0x1B00000 - 0x1B7FFFF):**

| Address | Size | Identity |
|---|---|---|
| `sub_1B01A40` | 68 KB | LoopUnroll main driver |
| `sub_1B07290` | 55 KB | Unroll-and-Jam |
| `sub_1B0BF10` | 39 KB | Loop peeling |
| `sub_1B12B90` | 65 KB | Unroll prologue/epilogue generation |
| `sub_1B51110` | 51 KB | Code sinking (".sink.split") |
| `sub_1B5C580` | 30 KB | SimplifyCFG condition combining |
| `sub_1B60700` | 83 KB | Switch-to-lookup-table transformation |

**Loop/SLP vectorizer (0x1B80000 - 0x1BFFFFF):**

| Address | Size | Identity |
|---|---|---|
| `sub_1BB6740` | 43 KB | LoopVectorize main driver ("loop-vectorize") |
| `sub_1BAB460` | 32 KB | VPlan builder |
| `sub_1BDDB00` | 47 KB | SLP horizontal reduction ("slp-vectorizer") |
| `sub_1BD0660` | 62 KB | SLP shuffle/reorder engine |

**NVVM module validation + configuration (0x1C00000 - 0x1C3FFFF):**

| Address | Size | Identity |
|---|---|---|
| `sub_1C20170` | 33 KB | **NVVM codegen config parser** (70+ knobs: AdvancedRemat, CSSACoalescing, DoMMACoalescing, PGO, OCGKnobs) |
| `sub_1C21CE0` | 28 KB | **NVVM compile mode parser** (WHOLE_PROGRAM_NOABI/ABI, SEPARATE_ABI, opt level, debug info) |
| `sub_1C32740` | 30 KB | **Kernel attribute validator** (cluster launch, parameter size, Hopper constraints) |
| `sub_1C36530` | 112 KB | **NVVM intrinsic lowering** (tex/surf/syncwarp/ISBE/MAP/ATTR validation) |
| `sub_1C3BC10` | 48 KB | **NVVM module validator** (data layout, target triple, UnifiedNVVMIR) |

**NVIDIA custom IR passes (0x1C40000 - 0x1CFFFFF):**

This 1 MB block contains the majority of NVIDIA's proprietary IR-level optimization passes. Every pass listed here has no upstream LLVM equivalent.

| Address | Size | Pass name | Identity |
|---|---|---|---|
| `sub_1C47810` | 63 KB | dead-sync-elim | **Dead Synchronization Elimination** -- removes redundant `__syncthreads()` barriers via fixed-point R/W dataflow |
| `sub_1C4D210` | 69 KB | | Alloca cloning / PHI insertion (mem2reg extension) |
| `sub_1C585C0` | 39 KB | | NVIDIA pass helper (dead-sync / common-base infrastructure) |
| `sub_1C5DFC0` | 39 KB | common-base-elim | **Common Base Elimination** -- removes redundant base address computations |
| `sub_1C5FDC0` | 26 KB | | Block-level analysis infrastructure ("Processing", "Block") |
| `sub_1C637F0` | 28 KB | | Base address bitcast helper ("baseValue", "bitCastEnd") |
| `sub_1C67780` | 59 KB | base-addr-sr | **Base Address Strength Reduction** ("BaseAddressStrengthReduce") |
| `sub_1C6A6C0` | 54 KB | | MemorySpaceOpt loop index analysis ("phi maxLoopInd") |
| `sub_1C6E800` | | | GVN or LICM variant |
| `sub_1C6FCA0` | | | ADCE (Aggressive DCE) |
| `sub_1C70910` | 75 KB | memspace-opt (core) | **MemorySpaceOpt function cloning** -- specializes generic pointers to global/shared/local |
| `sub_1C7B2C0` | 84 KB | loop-index-split | **LoopIndexSplit** -- splits loops on index conditions (three modes: all-but-one, single-iter, range-split) |
| `sub_1C82A50` | 40 KB | lower-aggr-copies | **Memmove Unrolling** -- forward/reverse element copy loops |
| `sub_1C86CA0` | 73 KB | lower-aggr-copies | **Struct/Aggregate Splitting** -- element-wise memcpy decomposition |
| `sub_1C8A4D0` | | | EarlyCSE / GVN variant |
| `sub_1C8C170` | 26 KB | lower-ops | **FP128/I128 Emulation** -- replaces 128-bit ops with `__nv_*` library calls |
| `sub_1C8E680` | | nvvm-memspace-opt | **MemorySpaceOpt** entry (pipeline factory address) |
| `sub_1C98160` | | | NVVMLowerBarriers / BarrierLowering |
| `sub_1CA2920` | 32 KB | | MemorySpaceOpt address space resolution (warnings for illegal atomics on const/local) |
| `sub_1CA9E90` | 28 KB | | MemorySpaceOpt secondary resolver |
| `sub_1CB1E60` | 31 KB | printf-lowering | **Printf Lowering** -- lowers `printf` to `vprintf` + local buffer packing |
| `sub_1CB4E40` | | nvvm-intrinsic-lower | **NVVMIntrinsicLowering** (most frequently inserted pass, ~10 occurrences in pipeline) |
| `sub_1CB73C0` | | branch-dist | **NVVMBranchDist** |
| `sub_1CBFA40` | 75 KB | | RLMCAST transformation (register-level multicast) |
| `sub_1CC60B0` | | sinking2 | **NVVMSinking2** (NVIDIA enhanced code sinking) |
| `sub_1CD74B0` | 75 KB | iv-demotion | **IV Demotion** -- narrows 64-bit induction variables to 32-bit ("demoteIV", "newBaseIV") |
| `sub_1CDC1F0` | 35 KB | | NLO (NVIDIA Live Output) helper ("nloNewAdd", "nloNewBit") |
| `sub_1CDE4D0` | 80 KB | | Instruction classification / cost model (NLO/remat) |
| `sub_1CE10B0` | 48 KB | | **Simplify Live Output** (NLO pass -- "nloNewBit") |
| `sub_1CE3AF0` | 56 KB | | Rematerialization pull-in cost analysis ("Total pull-in cost") |
| `sub_1CE67D0` | 32 KB | | Rematerialization block executor ("remat_", "uclone_" prefixes) |
| `sub_1CE7DD0` | 67 KB | remat | **NVVMRematerialization main driver** -- live-in/live-out pressure analysis per block |
| `sub_1CEBD10` | | | Final NVVM lowering / intrinsic cleanup |
| `sub_1CEE970` | 27 KB | | Formal parameter space overflow checker |
| `sub_1CEF8F0` | | nvvm-peephole | **NVVMPeephole** |
| `sub_1CFDD60` | 49 KB | | Instruction scheduling helper (physical register constraints) |

### Zone 14: SelectionDAG ISel / CodeGenPrepare / Backend (0x1D00000 - 0x1EFFFFF)

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x1D00000`-`0x1D60000` | **SelectionDAG ISel core** | `sub_1D4BB00` bytecode interpreter (97KB, 131-case switch), `sub_1D54C20` runOnMachineFunction (72KB, "sdagisel") |
| `0x1D1B0D0` | | `sub_1D1B0D0` computeKnownBits (87KB, 62-case ISD switch) |
| `0x1D210A0` | | `sub_1D210A0` SimplifyDemandedBits (46KB, 118-case switch, calls NVPTX hooks at `sub_1F58D40`) |
| `0x1D70000`-`0x1D7FFFF` | **CodeGenPrepare** | `sub_1D73760` address sinking (65KB, "sunkaddr") |
| `0x1D07BB0` | 57 KB | Pre-RA instruction scheduling | |
| `0x1D80000`-`0x1DFFFFF` | Deque worklist, block splitting | `sub_1D7AA30` (74KB, ".unlikely", ".cond.split") |
| `0x1E00000`-`0x1EFFFFF` | Register allocation infrastructure | Greedy RA, live intervals, spill cost |

### Zone 15: Backend CodeGen Infrastructure (0x1F00000 - 0x20FFFFF)

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x1F00000`-`0x1F0C000` | **ScheduleDAG infrastructure** | `sub_1F0A020` DAG builder/emitter (41KB) |
| `0x1F0BF50`-`0x1F0EBC0` | **Shrink Wrapping** | `sub_1F0DCB0` core analysis (27KB, "shrink-wrap") |
| `0x1F10000`-`0x1F15000` | **SlotIndexes + SpillPlacement** | `sub_1F10320` "slotindexes", `sub_1F12110` "spill-code-placement" |
| `0x1F15000`-`0x1F1F000` | **LiveInterval utilities** | `sub_1F19E60` "Impossible to implement partial COPY" |
| `0x1F20000`-`0x1F5FFFF` | Register coalescer, VirtRegRewriter | |
| `0x1F58D40` | | NVPTX target hook for SimplifyDemandedBits |
| `0x1F60000`-`0x1FFFFF` | TwoAddressInstruction, stack protection | |
| `0x2000000`-`0x20FFFFF` | **LegalizeTypes** | `sub_20019C0` (341KB -- third largest function in binary) |

### Zone 16: NVPTX Target Backend (0x2100000 - 0x21FFFFF)

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x2100000`-`0x210FFFF` | Register allocation support | `sub_210BC20` seedLiveRegs ("regalloc"), `sub_210BE60` "ran out of registers" |
| `0x2110000`-`0x212FFFF` | DAG type legalization/promotion | |
| `0x2130000`-`0x213FFFF` | DAG combiners, ISel patterns | |
| `0x2140000`-`0x214FFFF` | **NVPTXAsmPrinter** | PTX header/kernel emission |
| `0x2150000`-`0x215FFFF` | PTX function/param emission | `sub_215D9D0` NVVMAnnotationsProcessor / GenericToNVVM |
| `0x2160000`-`0x216FFFF` | **NVPTXTargetMachine** | Pass pipeline, SubtargetInfo |
| `0x2170000`-`0x218AFFF` | Atomics lowering, **rematerialization** (machine-level) | |
| `0x21BC000`-`0x21BFFFF` | Alloca hoisting, image opt | |
| `0x21C0000`-`0x21CFFFF` | MemorySpace lowering (machine-level) | |
| `0x21D0000`-`0x21DFFFF` | **DAG lowering mega-function**, peephole, prolog/epilog | |
| `0x21E0000`-`0x21EFFFF` | **MMA/tensor codegen**, atomics, special regs, cluster ops | |
| `0x21F0000`-`0x21FFFFF` | Ldg transform, vec split, mem2reg, register pressure | |

### Zone 17: New PM Pass Registration (0x2340000 - 0x23FFFFF)

| Address | Size | Identity |
|---|---|---|
| `sub_2342890` | ~2,816 lines | **Master pass registration** -- registers all 526 passes (121 module + 174 function + 23 loop + 48 MF + analyses) into StringMap |
| `sub_233C410` | | Print available passes (--print-pipeline-passes) |
| `sub_233F860` | | Function pass pipeline text parser |
| `sub_2377300` | | Module pipeline text parser |
| `sub_2368220` | | Inner function/loop pipeline parser |
| `sub_233BD40` | | Alias analysis name resolver (globals-aa, basic-aa, scev-aa, tbaa) |
| `sub_E41FB0` | | Hash table insertion (pass_name -> constructor) |

### Zone 18: IPO / Attributor / OpenMP Optimization (0x2400000 - 0x29FFFFF)

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x2400000`-`0x25FFFFF` | Attributor framework | `sub_251CD10` runTillFixpoint (53KB) |
| `0x2590000`-`0x265FFFF` | Sanitizer instrumentation (ASan, HWASan) | |
| `0x266E000`-`0x269FFFF` | **OpenMP target offloading** | `sub_2686D90` runtime table (215KB, ~160 `__kmpc_*` entries), `sub_26968A0` Generic-to-SPMD transform (61KB, "OMP120") |
| `0x2678420` | 41 KB | OpenMP state machine for generic kernels | `sub_2678420` ("worker_state_machine.begin") |
| `0x2680940` | 52 KB | Parallel region merging | `sub_2680940` ("omp.par.merged") |
| `0x26A0000`-`0x29FFFFF` | Coroutine support, LTO infrastructure, PGO lowering | |

### Zone 19: Loop Transforms (0x2A00000 - 0x2CFFFFF)

| Address | Size | Identity |
|---|---|---|
| `sub_2A07DE0` | 76 KB | LoopPeeling ("llvm.loop.peeled.count") |
| `sub_2A0CFD0` | 65 KB | LoopRotation (".lr.ph", "h.rot") |
| `sub_2A15A20` | 85 KB | UnrollLoop main ("loop-unroll", "UnrollCount") |
| `sub_2A1CF00` | 58 KB | UnrollAndJamLoop ("loop-unroll-and-jam") |
| `sub_2A25260` | 91 KB | Runtime unrolling (".epil.preheader", ".prol.preheader") |
| `sub_2A76A40` | 67 KB | IndVarSimplify IV widening ("iv.rem", ".sext", ".zext") |
| `sub_2A79EE0` | 82 KB | WidenIV / IV transformation |
| `sub_2C84BA0` | 94 KB | **Dead Synchronization Elimination** (island -- the larger copy; see also `sub_1C47810`) |

Note: `sub_2C84BA0` is a second copy of the dead synchronization elimination pass located outside the main NVIDIA custom pass zone. This is the 94KB variant analyzed in depth (p2b.6-01), with the four-category fixed-point R/W dataflow algorithm and red-black tree maps.

### Zone 20: Codegen Target Options / SelectionDAG Lowering (0x2D00000 - 0x2FFFFFF)

5,217 functions. Contains LLVM TargetMachine option registration and the core SelectionDAG infrastructure used by the NVPTX backend.

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x2D00000`-`0x2D8FFFF` | SelectionDAG core | DAG combine, node creation, legalization helpers |
| `0x2D97F20` | 112 KB | **TargetOptions registration** (all `cl::opt` for -march/-mcpu/-mattr/relocation/code model) |
| `0x2E00000`-`0x2FFFFF` | SelectionDAG continued | Type legalization, custom lowering, pattern matching |

### Zone 21: NVPTX ISel + SelectionDAG Lowering (0x3000000 - 0x36FFFFF)

7 MB. The NVPTX instruction selection and target-specific DAG lowering.

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x3000000`-`0x328FFFF` | DAG node construction, EVT/MVT helpers | |
| `0x3290000`-`0x32FFFFF` | **NVPTXTargetLowering** | `sub_32E3060` LowerOperation dispatcher (111KB), `sub_32A1EF0` type legalization (109KB), `sub_32D2680` load/store lowering (81KB) |
| `0x3300000`-`0x33AFFFF` | Intrinsic lowering (DAG level) | `sub_33B0210` intrinsic switch (343KB) |
| `0x33B0000`-`0x36FFFFF` | ISel pattern helpers, register info | |

### Zone 22: NVPTX Instruction Selector / Machine Tail (0x3700000 - 0x3BFFFFF)

| Sub-range | Contents | Key functions |
|---|---|---|
| `0x3700000`-`0x37AFFFF` | **Table-driven instruction selector** | `sub_376DE90` main pattern matcher (138KB -- per-SM opcode legality gating via compressed table at offset 521536) |
| `0x372FEE0` | 104 KB | DAG operand tree copier (recursive) | |
| `0x374DD20` | 67 KB | NVPTX custom lowering entry | |
| `0x3900000`-`0x396FFFF` | NVIDIA register pressure / remat (machine-level) | `sub_396A6C0` RP reporting ("Register Pressure: N"), `sub_3964ED0` ".remat" naming |
| `0x3937240` | 14 KB | **ABI Preserve directive emission** | `.abi_preserve`, `.abi_preserve_after`, `.abi_preserve_uniform`, `.abi_preserve_control` |
| `0x395CFD0` | 11 KB | **GEP Splitting pass** | "splitGEPI.base", "splitGEPI.replace" |
| `sub_395DD20` | 66 KB | DAG pattern computation | "dot.res" (dot product) |
| `0x3970000`-`0x397FFFF` | **AsmPrinter / PTX emission** | `sub_3979400` emitFunctionBody (62KB), `sub_397DF10` emitInlineAsm (30KB) |
| `sub_3970E40` | 18 KB | BB print + `.pragma "nounroll"` | NVIDIA mod: emits PTX pragma from `llvm.loop.unroll.disable` metadata |
| `0x3980000`-`0x3BFFFFF` | MC layer, DWARF, ELF emission | Object file writers, section management |

## Pass Factory Address Summary

The pipeline assembler (`sub_12E54A0`) calls pass factory functions to construct the pipeline. Each factory address below is called directly from the pipeline builder and uniquely identifies a pass in the binary.

| Factory address | Pass identity | Type |
|---|---|---|
| `sub_1654860` | BreakCriticalEdges | F |
| `sub_17060B0` | PrintModulePass (debug dump) | M |
| `sub_1832270` | InstructionCombining | F |
| `sub_1833EB0` | TailCallElim / JumpThreading | F |
| `sub_1841180` | FunctionAttrs | M |
| `sub_1842BC0` | SCCP | F |
| `sub_184CD60` | ConstantMerge / GlobalDCE | M |
| `sub_1857160` | **NVVMReflect** | F |
| `sub_185D600` | IPConstantPropagation | M |
| `sub_1869C50` | Sink / MemorySSA | F |
| `sub_18A3430` | NVVMPredicateOpt | F |
| `sub_18B1DE0` | LoopPass (barrier opt) | F |
| `sub_18DEFF0` | DCE | F |
| `sub_18EEA90` | CorrelatedValuePropagation | F |
| `sub_18F5480` | DSE | F |
| `sub_18FD350` | DeadArgumentElimination | M |
| `sub_190BB10` | SimplifyCFG | F |
| `sub_195E880` | LICM / LoopRotate | F |
| `sub_1952F90` | LoopIndexSplit | L |
| `sub_197E720` | LoopUnroll / LoopVectorize | F |
| `sub_198DF00` | LoopSimplify / IndVarSimplify | F |
| `sub_198E2A0` | SROA | F |
| `sub_19401A0` | InstCombine variant | F |
| `sub_19B73C0` | SROA variant / LoopUnswitch | F |
| `sub_19CE990` | NVIDIA pass (unknown) | F |
| `sub_1A13320` | **NVVMRematerialization** (IR-level) | F |
| `sub_1A223D0` | **NVVMIRVerification** | M |
| `sub_1A62BF0` | LLVM standard pass pipeline (parameterized) | M |
| `sub_1A68E70` | LoopIdiomRecognize | F |
| `sub_1A7A9F0` | InstructionSimplify | F |
| `sub_1B26330` | MemCpyOpt | F |
| `sub_1B7FDF0` | Reassociate / Sinking | F |
| `sub_1C4B6F0` | AlwaysInliner | M |
| `sub_1C6FCA0` | ADCE | F |
| `sub_1C8A4D0` | EarlyCSE | F |
| `sub_1C8E680` | **NVVMMemorySpaceOpt** | M |
| `sub_1C98160` | NVVMLowerBarriers | F |
| `sub_1CB4E40` | **NVVMIntrinsicLowering** (~10 insertions) | F |
| `sub_1CB73C0` | **NVVMBranchDist** | F |
| `sub_1CC60B0` | **NVVMSinking2** | F |
| `sub_1CE7DD0` | **NVVMRematerialization** (main) | F |
| `sub_1CEBD10` | Final NVVM lowering | F |
| `sub_1CEF8F0` | **NVVMPeephole** | F |
| `sub_1CB0F50` | ProfileSummaryInfoWrapper / NVVMModulePass | F |
| `sub_12D4560` | NVVMVerifier / ModuleVerifier | M |
| `sub_215D9D0` | NVVMAnnotationsProcessor | M |
| `sub_149CCE0` | TargetLibraryInfoWrapperPass | M |
| `sub_1BFB520` | TargetTransformInfoWrapperPass | F |
| `sub_14A7550` | createVerifierPass / BasicAliasAnalysis | M |
| `sub_1361950` | AssumptionCacheTracker | M |

Type: M = ModulePass, F = FunctionPass, L = LoopPass.

## Embedded Data Payloads

### Libdevice Bitcode

Two identical copies of NVIDIA's libdevice are embedded directly in the `.rodata` section as raw LLVM bitcode. Each copy is approximately 456 KB and contains around 400 math intrinsic implementations (`__nv_sinf`, `__nv_expf`, `__nv_sqrtf`, etc.). The duplication supports the dual-path architecture: Path A (LibNVVM API mode) references one copy at `0x3EA0080`; Path B (standalone mode) references the other at `0x420FD80`. The bitcode is linked into the user's module during the LNK phase via the bitcode linker at `sub_12C06E0`.

### String Tables

IDA Pro extracts 188,141 strings from the binary. These fall into several categories:

| Category | Approximate count | Example |
|---|---|---|
| LLVM `cl::opt` descriptions | ~1,689 | `"Enable aggressive reassociation"` |
| LLVM error/diagnostic messages | ~5,000 | `"Invalid bitcode signature"` |
| EDG error messages | ~2,500 | `"expected a declaration"` |
| LLVM pass names | ~440 | `"instcombine"`, `"gvn"`, `"nvvm-memspace-opt"` |
| PTX instruction templates | ~800 | `"mov.b32 %0, %1;"` |
| NVVM builtin names | ~770 | `"__nvvm_atom_cas_gen_i"` |
| jemalloc config strings | ~200 | `"background_thread"`, `"dirty_decay_ms"` |
| NVVM container field names | ~144 | `"SmMajor"`, `"FastMath.Ftz"` |
| Miscellaneous (format strings, assertions) | ~170,000+ | `"%s:%d: assertion failed"` |

String cross-referencing is the single most productive technique for identifying functions in a stripped binary. The LLVM pass registration pattern is especially reliable: a string like `"nvvm-memspace-opt"` appears exactly once, in the constructor of that pass, which IDA locates via xref.

### NVVM Container Format

The binary includes a proprietary container format for wrapping LLVM bitcode with compilation metadata. The container uses a 24-byte binary header with magic `0x7F4E5C7D`, followed by delta-encoded tag/value pairs (only fields that differ from defaults are serialized). There are 144 distinct tag IDs spanning core options (tags 1-39), compression metadata (tag 99), extended target options (tags 101-173), blob data (tags 201-218), and structured hardware descriptors (tags 401-402 for TMA/TCGen05 configurations). Serialization and deserialization are handled by `sub_CDD2D0` and `sub_CD1D80` respectively.

## jemalloc Integration

NVIDIA statically links jemalloc 5.3.x as the process-wide memory allocator. The jemalloc functions cluster around `0x12FC000` (approximately 400 functions). The configuration initialization function `sub_12FCDB0` (129 KB, one of the largest functions in the binary) parses 199 configuration strings from the `MALLOC_CONF` environment variable.

Key jemalloc entry points visible in the binary:

| Address | Identity |
|---|---|
| `0x12FCDB0` | `malloc_conf_init` (199 config strings) |
| `0x40D5CA` | `vsnprintf` (jemalloc stats formatting) |
| `0x12FC000` range | Core arena management, tcache, extent allocator |

The jemalloc integration is significant for reverse engineering because it means `malloc`/`free` calls throughout the binary resolve to jemalloc's arena-based allocator rather than glibc's `ptmalloc2`. When tracing memory allocation patterns in IDA, look for calls into the `0x12FC000` range.

## Global Constructors

The region from `0x430000` to `0x5CFFFF` (~1.6 MB) is dominated by global constructors that execute before `main()`. The primary purpose of these constructors is LLVM `cl::opt` registration: approximately 1,689 command-line option objects are initialized, each registering a string name, description, default value, and storage location into LLVM's global option registry.

The `.init_array` section contains function pointers to these constructors. They execute in linker-determined order and populate a global hash table that `sub_8F9C90` (the real main) later queries during CLI parsing. In IDA Pro, navigating to any `cl::opt` constructor reveals the option name string and its associated global variable, which is invaluable for understanding what flag controls what behavior.

Additional global constructors handle:

- LLVM pass registration (`RegisterPass<T>` and `PassInfo` objects)
- LLVM target initialization (NVPTX target machine factory)
- jemalloc allocator bootstrapping
- EDG frontend static initialization tables

## Dual-Path Code Duplication

A distinctive structural feature of the binary is the presence of two near-complete copies of the NVVM bridge and backend entry points. Path A (LibNVVM API mode) lives around `0x90xxxx`; Path B (standalone/nvcc mode) lives around `0x126xxxx`. Each path has its own:

| Component | Path A | Path B |
|---|---|---|
| Simple compile entry | `sub_902D10` | `sub_1262860` |
| Multi-stage pipeline | `sub_905EE0` (43 KB) | `sub_1265970` (48 KB) |
| CLI parsing | `sub_900130` | `sub_125FB30` |
| Builtin resolution table | `sub_90AEE0` (109 KB) | `sub_126A910` (123 KB) |
| Embedded libdevice ref | `unk_3EA0080` | `unk_420FD80` |
| Version string | `nvvm-latest` | `nvvm70` |

In IDA, if you have identified a function in one path, search for a structurally similar function at the corresponding offset in the other path. The code is not byte-identical -- Path B is generally slightly larger due to additional standalone-mode logic -- but the control flow graphs are nearly congruent.

## IDA Pro Navigation Tips

When opening cicc in IDA Pro for the first time, the auto-analysis will take several minutes due to the 60 MB size. The following workflow accelerates orientation:

1. **Start with strings.** Open the Strings window (Shift+F12), filter for known LLVM pass names (`"instcombine"`, `"gvn"`, `"nvvm-"`). Each xref leads directly to a pass constructor or registration site.

2. **Use the address map above.** If you are looking at an address in the `0xC00000`-`0x12CFFFF` range, you are in LLVM optimization passes. The `0x3000000`-`0x36FFFFF` range is NVPTX instruction selection. The `0x5D0000`-`0x8EFFFF` range is EDG. Context narrows the search space immediately.

3. **Watch for vtable patterns.** LLVM passes are C++ classes with virtual methods. IDA's vtable reconstruction reveals inheritance hierarchies. Every `FunctionPass`, `ModulePass`, and `LoopPass` subclass has a vtable with `runOnFunction`/`runOnModule` at a consistent slot offset.

4. **Anchor on mega-functions.** The largest functions are the easiest to locate and serve as landmarks: `sub_A939D0` (457 KB, X86 AutoUpgrade), `sub_10EE7A0` (396 KB, InstCombine), `sub_20019C0` (341 KB, LegalizeTypes). These anchors partition the address space.

5. **Follow the pipeline.** Entry at `sub_8F9C90` calls into EDG at `sub_5D2A80`, pipeline assembly at `sub_12E54A0`, and PTX emission starting at `0x2100000`. Tracing callgraph edges from these known entry points maps out the entire compilation flow.

6. **Mark jemalloc early.** Identifying and labeling the jemalloc cluster at `0x12FC000` prevents wasted time reverse-engineering well-known allocator internals. The 199-string `malloc_conf_init` function is an unmistakable fingerprint.

7. **Locate NVIDIA passes via factory addresses.** The Pass Factory Address Summary table above maps every pipeline-inserted pass to its constructor address. In IDA, setting a breakpoint at `sub_12DE0B0` (AddPass) and logging the second argument reveals the exact pass insertion order at runtime.

## Cross-References

- [Pipeline Overview](pipeline/overview.md) -- compilation flow from entry to PTX emission
- [LLVM Pipeline](llvm/pipeline.md) -- 526-pass registration table and tier execution order
- [Optimizer](llvm/optimizer.md) -- two-phase model, AddPass mechanism, tier system
- [Pass Inventory](passes/index.md) -- complete pass catalog with dedicated deep-dive pages
- [NVVMPassOptions](config/nvvm-pass-options.md) -- 222-slot pass configuration system
- [Function Map](function-map.md) -- address-to-identity lookup table
- [CLI Flags](config/cli-flags.md) -- flag-to-pipeline routing
