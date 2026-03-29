# Methodology

This page documents how the reverse engineering of cicc v13.0 was performed. It serves as both a transparency record -- so readers can assess the confidence of any claim in this wiki -- and as a practical guide for anyone who wants to reproduce or extend the analysis.

## Scope and Scale

CICC is a 60 MB stripped x86-64 ELF binary with no debug symbols, no export table, and no DWARF information. The scale of the analysis:

| Metric | Value |
|---|---|
| Total functions detected | 80,562 |
| Functions decompiled | 80,281 (99.65%) |
| Strings extracted | 188,141 |
| LLVM base version | 20.0.0 (internal fork) |
| LLVM pass classes identified | ~402 standard + 35 NVIDIA custom |
| CLI options registered | ~1,689 `cl::opt` + 222 NVVMPassOptions |
| NVVM builtins catalogued | 770 (IDs 1-770) |

The 281 functions that Hex-Rays could not decompile are predominantly very small thunks, computed-jump trampolines, or hand-written assembly stubs in the CRT startup and jemalloc fast paths. None are in critical compiler logic.

## Toolchain

All analysis was performed with IDA Pro 8.x and the Hex-Rays x86-64 decompiler. No dynamic analysis (debugging, tracing, instrumentation) was used -- the entire effort is static analysis of the binary at rest. Supplementary tools:

| Tool | Purpose |
|---|---|
| IDA Pro 8.x | Disassembly, auto-analysis, cross-referencing, type reconstruction |
| Hex-Rays decompiler | Pseudocode generation for all 80,281 recovered functions |
| IDA Python scripting | Bulk string extraction, function size enumeration, xref graph walking |
| Custom scripts | Address range classification, pass name extraction, vtable enumeration |

No runtime instrumentation, no `strace`/`ltrace`, no `gdb` breakpoints. Every finding derives from static analysis of the binary's code and data sections.

## Function Identification Strategies

Identifying functions in a stripped binary of this size requires multiple complementary strategies. They are listed below in order of reliability.

### String Cross-References (Highest Confidence)

LLVM is a string-rich codebase. Error messages, pass names, option descriptions, and assertion text are compiled into the binary. A string like `"Running pass 'NVVMMemorySpaceOpt'"` appears at exactly one address in `.rodata`, and IDA's xref from that string leads directly to the function that prints it. This is the most reliable identification technique and produces VERY HIGH confidence identifications.

Specific high-value string patterns:

- **LLVM pass registration**: `"instcombine"`, `"gvn"`, `"nvvm-memspace-opt"` -- each appears in exactly one `RegisterPass` constructor or `PassInfo` initializer.
- **`cl::opt` names**: `"-nvvm-enable-remat"`, `"-nvvm-branch-dist-threshold"` -- each names a global variable and its registration constructor.
- **Error messages with context**: `"parseFunctionBody: ..."` (174 unique error strings in the bitcode reader), `"visitCallInst: ..."` (298 verification messages in the verifier).
- **Timer names**: `"CUDA C++ Front-End"`, `"LibNVVM"`, `"Optimizer"` -- appear in timer-creation calls that bracket pipeline stages.
- **EDG error templates**: `"expected a %s"`, `"declaration not allowed here"` -- 2,500+ diagnostic strings anchoring the frontend parser.

### LLVM Pass Registration Patterns (Very High Confidence)

Every LLVM pass follows a predictable structural pattern. A pass class has a vtable with virtual methods at fixed offsets (`runOnFunction` at slot N, `getAnalysisUsage` at slot M). The pass registers itself via a global constructor that stores a `PassInfo` object containing the pass name string, the pass ID address, and a factory function pointer. By enumerating all `.init_array` entries that write a `PassInfo`-shaped structure, all ~437 passes were catalogued systematically.

The New Pass Manager (at `sub_2342890`, a 2,816-line registrar function) contains a massive string-to-pass-factory dispatch table with ~268 pass name entries. Decompiling this single function yields the name-to-address mapping for every New PM pass in the binary.

### Vtable Analysis (High Confidence)

LLVM's class hierarchy is deep and regular. `Pass` -> `FunctionPass` -> `LoopPass`, `Pass` -> `ModulePass`, etc. Each level adds virtual methods at predictable vtable slots. By reconstructing vtable layouts (finding pointers to `__cxa_pure_virtual` for abstract methods, then tracing concrete overrides), the class hierarchy was reconstructed without debug symbols.

For the NVPTX backend specifically, vtable analysis identified `NVPTXTargetLowering` (2.3 MB of lowering logic), `NVPTXInstrInfo`, `NVPTXRegisterInfo`, and `NVPTXFrameLowering` as distinct classes with their own method tables.

### Callgraph Propagation (High Confidence)

Once a function is identified with high confidence, its callees and callers gain contextual identity. If `sub_12E54A0` is the pipeline assembly function (confirmed by string refs to pass names it registers), then the functions it calls to create individual passes are the pass factory functions. This propagation is transitive: identifying a factory function identifies its return type's vtable, which identifies the pass's `runOnFunction` method.

The pipeline orchestrator at `sub_12C35D0` (41 KB) is a particularly productive anchor: it calls into the LNK, OPT, OPTIXIR, and LLC stages in sequence, and each stage's entry point was identified by following its callgraph edges.

### Size and Structural Fingerprinting (Medium Confidence)

Some functions are identifiable by their size and structural characteristics alone. LLVM's `InstCombine::visitCallInst` is famously enormous (396 KB in this binary) because it handles every LLVM intrinsic. `SelectionDAG::LegalizeTypes` (348 KB) contains a switch with 967 case labels. These mega-functions have no structural equivalents and can be identified by size alone with reasonable confidence.

Similarly, the EDG frontend's constexpr evaluator (`sub_786210`, 317 KB) is identifiable by its 124 case labels corresponding to C++ operator opcodes -- a characteristic that matches the known EDG evaluator design.

### Known Library Fingerprinting (Medium Confidence)

jemalloc was identified by its 199 configuration string names (`"background_thread"`, `"dirty_decay_ms"`, `"narenas"`, etc.), which are unique to jemalloc's `malloc_conf_init` function. Once the allocator library was identified, its ~400 functions were bulk-labeled, removing them from the analysis scope.

The X86 AutoUpgrade function (`sub_A939D0`, 457 KB) is an LLVM artifact -- leftover x86 intrinsic renaming code that ships in every LLVM-based binary regardless of target. It was identified by its intrinsic name strings (`"llvm.x86.sse2.*"`, `"llvm.x86.avx.*"`) and excluded from NVPTX-specific analysis.

## Confidence Levels

Every function identification in this wiki carries one of four confidence levels:

| Level | Meaning | Basis |
|---|---|---|
| **KNOWN** | Identity is certain | Direct string evidence naming the function, or the function is a trivial thunk to a known target |
| **VERY HIGH** | Effectively certain | Multiple corroborating string references, structural match to known LLVM code, consistent callgraph position |
| **HIGH** | Strong identification | Single strong indicator (vtable match, size fingerprint, callgraph position) corroborated by context |
| **MEDIUM** | Probable identification | Inferred from callgraph context, parameter patterns, or structural similarity without direct string evidence |

Approximately 60% of identified functions are VERY HIGH or KNOWN confidence. The remaining 40% are HIGH or MEDIUM, concentrated in areas with fewer string anchors (machine-level passes, register allocation internals, EDG IL tree walkers).

## Verification Approaches

To verify any specific finding in this wiki:

1. **Open IDA at the stated address.** Every function identification includes an address. Navigate to it, press F5 to decompile, and check whether the decompiled code matches the described behavior.

2. **Check string xrefs.** For VERY HIGH and KNOWN identifications, search for the quoted string in IDA's Strings window. The xref should lead to the stated function address or a function that directly calls it.

3. **Compare with upstream LLVM.** CICC is based on LLVM 20.0.0. The LLVM source tree at the corresponding git tag contains the original implementations of all standard passes. Structural comparison (switch case counts, parameter counts, error message text) between the decompiled code and the LLVM source is the gold standard for verification.

4. **Cross-reference the dual paths.** Path A and Path B contain near-duplicate code. If a function is identified in Path A, the corresponding Path B function should exhibit the same structure. Agreement between the two paths increases confidence.

5. **Trace from known entry points.** Start at `sub_8F9C90` (real main, KNOWN confidence) and follow the call chain. Every function reachable from main through a chain of identified functions has a verified callgraph path.

## Limitations and Known Gaps

This analysis has several inherent limitations:

- **No dynamic validation.** All findings are from static analysis. Runtime behavior under specific inputs (unusual SM targets, edge-case CUDA constructs) has not been verified.
- **EDG internals are partially opaque.** The EDG frontend is a licensed third-party component. Its internal data structures are less well-documented in the LLVM literature, making identification harder. The IL tree format and scope management structures are identified at MEDIUM confidence.
- **Inlined functions are invisible.** If the compiler inlined a function during the build of cicc itself, that function has no standalone address and cannot be independently identified. Some small LLVM utility functions (SmallVector operations, StringRef comparisons) are likely inlined throughout.
- **Proprietary NVIDIA code has no public reference.** The 35 custom NVIDIA passes, the NVVM bridge layer, and the NVVMPassOptions system have no upstream source to compare against. These are identified purely from string evidence and structural analysis.
- **Version-specific.** All findings apply to cicc v13.0 (build `cuda_13.0.r13.0/compiler.36424714_0`). Addresses, function sizes, and pass counts will differ in other CUDA toolkit versions.

## Reproducibility

To reproduce this analysis from scratch:

1. Obtain cicc v13.0 from the CUDA 13.0 toolkit (the binary is at `<cuda>/nvvm/bin/cicc`).
2. Open in IDA Pro 8.x with default analysis settings. Allow auto-analysis to complete (~5-10 minutes).
3. Run Hex-Rays batch decompilation on all functions (IDA Python: iterate `Functions()`, call `ida_hexrays.decompile()` on each).
4. Extract all strings (Shift+F12, export to file). This yields 188,141 entries.
5. Begin identification from the string anchors described above, propagating outward through the callgraph.

The [Function Map](./function-map.md) page provides the complete address-to-identity lookup table with confidence levels for each entry, serving as both the primary output of this analysis and the starting point for further investigation.
