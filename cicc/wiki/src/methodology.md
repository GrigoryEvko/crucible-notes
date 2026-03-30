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
| Custom Python scripts | Callgraph analysis, module taxonomy, evidence indexing, pipeline tracing |

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

## Analysis Pipeline and Scripts

The manual IDA Pro work was augmented by a systematic scripted pipeline that processed the exported IDA databases into structured evidence. The pipeline operates in two phases: L0 (foundation) builds indices and classifies all 80,562 functions automatically, and L1 (module analysis) organizes functions into per-module directories with metadata for human review.

All scripts live in `cicc/scripts/`. The pipeline requires four JSON databases exported from IDA: `cicc_functions.json` (80,562 function records), `cicc_strings.json` (188,141 string records), `cicc_xrefs.json` (cross-reference records), and `cicc_callgraph.json` (call edge records). These exports are stored in `cicc/databases/`.

### L0 Foundation Pipeline

The L0 pipeline runs as a single sequential batch via `scripts/run_foundation_analysis.sh`. Each step depends on the output of the previous step.

**Step 0: Extract Wiki Knowledge** (`foundation/00_extract_wiki_knowledge.py`)

Scans all existing wiki markdown files for hex addresses (regex `\b0x[0-9a-fA-F]{6,}\b`) and builds a ground-truth mapping of address-to-module from prior manual analysis. This seed data provides the highest-confidence module assignments (100% confidence) used to bootstrap the automated classifier.

Output: `foundation/taxonomy/modules/wiki_known_functions.json`, `wiki_module_addresses.json`.

**Step 1: Build Fast Lookup Indices** (`foundation/01_build_indices.py`)

Loads the three IDA JSON databases (functions, strings, xrefs) and builds four pickle-serialized indices for O(1) lookup in subsequent steps:

- `addr_to_func.pkl` -- address to function metadata (name, size, instruction count, library/thunk flags).
- `string_to_xrefs.pkl` -- string address to string value and xref list.
- `func_to_callers.pkl` -- function name to list of caller names.
- `func_to_callees.pkl` -- function name to list of callee names.

Output: `foundation/indices/`.

**Step 2: Classify Strings** (`foundation/02_classify_strings.py`)

Applies four regex-based pattern sets to all 188,141 strings, classifying each into one or more semantic categories:

- **Error messages**: strings matching `error`, `failed`, `invalid`, `unsupported`, `expected`, etc.
- **Optimization passes**: strings matching `pass`, `optimize`, `transform`, `inline`, `unroll`, `gvn`, `licm`, etc.
- **Architecture features**: strings matching `sm_\d+`, `tensor`, `warp`, `FP4`, `blackwell`, `hopper`, etc.
- **Debug messages**: strings matching `debug`, `trace`, `dump`, `verbose`.

Each classified string retains its address and xref list, so the classifier output doubles as a "which functions reference optimization-related strings" index.

Output: `foundation/taxonomy/strings/error_messages.json`, `optimization_passes.json`, `architecture_features.json`, `debug_messages.json`, `extracted_pass_names.json`.

**Step 3: Build Module Taxonomy** (`foundation/03_build_module_taxonomy.py`)

The core classification engine. Assigns each of the 80,562 functions to one of eight compiler subsystem modules (or `unknown`) using four strategies applied in decreasing confidence order:

1. **Wiki ground truth** (100% confidence) -- addresses found in wiki pages in Step 0.
2. **String content analysis** (80% confidence) -- functions whose string xrefs match module-specific keyword patterns (e.g., a function referencing `"tensor"`, `"mma"`, or `"tcgen"` strings is classified as `tensor_core_codegen`).
3. **Call proximity propagation** (30-60% confidence, 3 iterations) -- unclassified functions are assigned to the module voted by their callers (weighted 2x) and callees. A minimum of 2 votes is required. Each iteration propagates classifications outward from already-classified functions.
4. **Code location heuristics** (40% confidence) -- address range rules for known code regions (e.g., `0x2F00000-0x3000000` maps to `register_allocation`).

The eight modules are: `optimization_framework`, `register_allocation`, `compilation_pipeline`, `ptx_emission`, `instruction_selection`, `error_handling`, `tensor_core_codegen`, `architecture_detection`.

Output: `foundation/taxonomy/modules/function_to_module_map.json`, `module_list.json`.

**Step 4: Analyze Call Graph** (`foundation/04_analyze_callgraph.py`)

Computes three structural properties of the call graph:

- **Entry points** -- functions with zero callers and nonzero callees (top 100 by callee count). These are pipeline entry points, API functions, or global constructors.
- **Leaf functions** -- functions with zero callees and nonzero callers (top 1,000 by caller count). These are utility functions, allocators, and assertion handlers.
- **Hot paths** -- functions ranked by caller count (top 1,000). The highest-traffic functions in the binary.

Output: `foundation/callgraph/entry_points.json`, `leaf_functions.json`, `hot_paths.json`.

**Step 5: Assign Priorities** (`foundation/05_assign_priorities.py`)

Computes a composite priority score for each function to guide analysis effort allocation. The scoring formula:

- **Size component**: 1000 points for functions over 10 KB, 700 for 5-10 KB, 400 for 2-5 KB, 200 for 1-2 KB, 100 for 500 B-1 KB.
- **Call frequency component**: 500 points for 1000+ callers, 300 for 500+, 150 for 100+, 75 for 50+.
- **Named function bonus**: 200 points if the function has a recovered name (not `sub_`).
- **Critical module bonus**: 300 points if the function belongs to a critical module (compilation_pipeline, tensor_core_codegen, architecture_detection, register_allocation, instruction_selection, ptx_emission).

Functions scoring 1000+ are tier CRITICAL, 500+ are HIGH, 200+ are MEDIUM, below 200 are LOW.

Output: `foundation/priorities/scoring_report.json`, `critical.json`, `high.json`, `medium.json`, `low.json`.

**Step 6: Generate Coverage Tracker** (`foundation/06_generate_coverage_tracker.py`)

Aggregates all prior outputs into a master JSON tracker that records, per module and per function, the analysis status (pending/in-progress/complete), the assigned analyst, and the evidence quality score. This tracker serves as the coordination database for the L1 phase.

Output: `foundation/coverage/tracker.json`.

### L1 Module Analysis Pipeline

The L1 pipeline runs via `scripts/run_l1_programmatic.sh` and requires L0 completion. It organizes CRITICAL and HIGH priority functions into per-module directories for systematic human review.

**Step 1: Create Module Structure** (`modules/01_create_module_structure.py`)

Creates the directory tree `modules/{module}/functions/{critical,high}/` for each of the eight modules. MEDIUM and LOW tiers are intentionally excluded from L1 to focus effort on the most important functions.

**Step 2: Extract Function Metadata** (`modules/02_extract_function_metadata.py`)

For each CRITICAL and HIGH function, creates a directory `modules/{module}/functions/{tier}/{address}/` containing a `metadata.json` file with: address, name, module, priority score, size, call frequency, scoring reasons, top 50 callers, top 50 callees, and paths to decompiled/disassembly/CFG files if they exist on disk.

**Step 3: Generate Module READMEs** (`modules/03_generate_module_readmes.py`)

Generates a skeleton `README.md` for each module with function counts, analysis progress tracking fields, and section headings for purpose, key functions, integration points, and data structures. These serve as the starting point for human-written module documentation.

### Standalone Analysis Scripts

Six additional scripts perform targeted analyses independent of the L0/L1 pipeline:

**`analyze_nvvm_pipeline.py`** -- Loads the NVVM call graph (`nvvm_callgraph.json`, exported from the LibNVVM shared object analysis) and traces the compilation flow from `nvvmCompileProgram`. Identifies NVVM API entry points, finds LLVM optimization pass function symbols, traces call paths to depth 10, identifies hub functions (nodes with in-degree or out-degree above 10), and extracts the optimization pass ordering reachable from the compile entry point.

**`deep_pipeline_trace.py`** -- Performs deep BFS traversal (up to depth 15, width 100 per level) from `nvvmCompileProgram` through the NVVM call graph. Annotates each function with structural characteristics (LEAF, HUB, FANOUT, FANIN) and groups results by call depth to reveal the pipeline's stage boundaries. Also traces from secondary API entry points (`nvvmVerifyProgram`, `nvvmAddModuleToProgram`, `nvvmCreateProgram`).

**`extract_pipeline_structure.py`** -- Parses the 188,141 strings database for `disable-*Pass` patterns and `Disable *` description strings to extract the complete list of optimization passes by name. Categorizes passes into groups (Dead Code Elimination, Loop Optimizations, Inlining, Memory, NVVM-Specific, Lowering, etc.) and reconstructs the 13-stage compilation pipeline from NVVM module loading through PTX code generation. Also extracts compilation mode information (fast-compile, split-compile, partial-link).

**`analyze_performance_hotspots.py`** -- Loads the full function database (`cicc_functions.json`) and computes: global hotspot ranking (top 100 most-called functions), hot path chains (BFS from top 50 hotspots through callees, tracking weighted call frequency), size-efficiency analysis (bytes per call for each function), loop depth estimation (regex-based nesting analysis of decompiled C files), bottleneck identification (functions with 500+ callers), and module-level hotspot distribution.

**`catalog_optimization_framework.py`** -- Specialized script for the optimization_framework module. Reads per-function metadata from the L1 module directories, builds a critical function registry sorted by size, extracts HIGH-tier statistics (size tier distribution, top 20 most-called), scans decompiled code for optimization-related string patterns (pass references, iteration patterns, technique keywords), and identifies entry points (functions with 2 or fewer callers).

**`validate_callgraph.py`** -- Comprehensive validation system that cross-checks the call graph data against module classifications. Performs six verification analyses: cross-module call matrix verification (counting inter-module edges and sampling for spot-checks), entry point validation (confirming claimed entry points have zero callers), reachability analysis (BFS from main to find dead code), module dependency cycle detection (DFS on the module dependency graph), integration hotspot verification (functions called by all 8 modules), and bridge function identification (functions that both call into and are called from 2+ other modules).

### Evidence Index Builders

Two versions of the evidence aggregation engine synthesize all data sources into per-function quality scores:

**`build_evidence_index.py`** (v1) -- Loads all five databases (functions, callgraph, strings, xrefs, names, comments, module map) into memory. For each of the 80,562 functions, counts eight evidence types (metadata, callers, callees, strings, xrefs, name pattern, size, module consistency) and computes a weighted confidence score (string evidence weighted highest at 20 points, callgraph at 15 each, xrefs at 15, metadata and name at 10 each, module at 10, size at 5). Produces nine output files including quality tier assignments (GOLD >= 80%, SILVER >= 50%, BRONZE < 50%), citation density analysis, cross-reference statistics, and prioritized recommendations for further analysis.

**`build_evidence_index_v2.py`** (v2, optimized) -- Memory-efficient reimplementation that avoids loading the full xref list into memory. Instead of building complete xref lookup tables, it streams the xref file line-by-line and counts only. The callgraph is preprocessed into a caller/callee count map rather than a full edge list. Produces the same nine analysis files as v1 with identical quality tier logic. Recommended for systems with less than 32 GB RAM.

### Cross-Module Dependency Analysis

**`07_analyze_cross_module_dependencies.py`** -- The most complex standalone analysis. Streams the full call graph (using `ijson` for memory-efficient parsing) four times to compute:

1. **Inter-module call matrix** -- for each pair of the 8 modules, the number of call edges crossing the boundary.
2. **Module dependency depth** -- per-module statistics on how many other modules each function depends on, identifying isolated functions and hub functions.
3. **Critical bridges** -- functions that call into 3 or more other modules (top 100 by bridge count).
4. **Integration hotspots** -- functions called by 3 or more other modules (top 100 by fan-in).
5. **Module dependency graph** -- a JSON graph structure with weighted edges suitable for visualization.
6. **Integration patterns** -- entry point modules (highest out-degree), utility hub modules (highest in-degree), and linear dependency chains.

## Data Flow and Directory Structure

The complete analysis data is organized as follows:

```
cicc/
  databases/                    # IDA exports (input data)
    cicc_functions.json         #   80,562 function records
    cicc_strings.json           #   188,141 string records
    cicc_xrefs.json             #   cross-reference records
    cicc_callgraph.json         #   call edge records
    cicc_names.json             #   recovered names
    cicc_comments.json          #   IDA comments
  foundation/                   # L0 pipeline output
    indices/                    #   pickle indices for fast lookup
    taxonomy/
      modules/                  #   function-to-module map, module list
      strings/                  #   classified string databases
    callgraph/                  #   entry points, leaf functions, hot paths
    priorities/                 #   priority scoring and tier assignments
    coverage/                   #   master progress tracker
    analyses/                   #   evidence index, quality tiers, cross-module data
  modules/                      # L1 pipeline output
    {module}/
      functions/
        critical/{addr}/        #   metadata.json per critical function
        high/{addr}/            #   metadata.json per high function
      analysis/                 #   module-level analysis files
      README.md                 #   module documentation skeleton
  decompiled/                   # Hex-Rays output (per-function C files)
  disasm/                       # IDA disassembly output (per-function ASM files)
  graphs/                       # Control flow graphs (JSON and DOT)
  scripts/                      # All analysis scripts
    foundation/                 #   L0 pipeline scripts (00-07)
    modules/                    #   L1 pipeline scripts (01-03)
    run_foundation_analysis.sh  #   L0 batch runner
    run_l1_programmatic.sh      #   L1 batch runner
```

## Verification Approaches

To verify any specific finding in this wiki:

1. **Open IDA at the stated address.** Every function identification includes an address. Navigate to it, press F5 to decompile, and check whether the decompiled code matches the described behavior.

2. **Check string xrefs.** For VERY HIGH and KNOWN identifications, search for the quoted string in IDA's Strings window. The xref should lead to the stated function address or a function that directly calls it.

3. **Compare with upstream LLVM.** CICC is based on LLVM 20.0.0. The LLVM source tree at the corresponding git tag contains the original implementations of all standard passes. Structural comparison (switch case counts, parameter counts, error message text) between the decompiled code and the LLVM source is the gold standard for verification.

4. **Cross-reference the dual paths.** Path A and Path B contain near-duplicate code. If a function is identified in Path A, the corresponding Path B function should exhibit the same structure. Agreement between the two paths increases confidence.

5. **Trace from known entry points.** Start at `sub_8F9C90` (real main, KNOWN confidence) and follow the call chain. Every function reachable from main through a chain of identified functions has a verified callgraph path.

6. **Run the validation script.** Execute `scripts/validate_callgraph.py` to cross-check the call graph against module classifications. The script produces a `CALLGRAPH_VALIDATION_REPORT.json` with quantitative metrics: entry point accuracy, cross-module call counts, reachability percentage, bridge function inventory, and module dependency cycles. A healthy analysis should show entry point confidence above 90% and reachability above 80%.

7. **Re-run the evidence index.** Execute `scripts/foundation/build_evidence_index_v2.py` to regenerate quality tier assignments. Compare the GOLD/SILVER/BRONZE percentages against the expected distribution (majority SILVER or GOLD for classified functions). Functions that drop to BRONZE after a wiki edit indicate a regression in evidence consistency.

## Reproducing the Full Analysis

To reproduce this analysis from scratch:

1. **Obtain the binary.** Install CUDA Toolkit 13.0. The binary is at `<cuda>/nvvm/bin/cicc`. SHA-256 and build string `cuda_13.0.r13.0/compiler.36424714_0` must match.

2. **Run IDA auto-analysis.** Open cicc in IDA Pro 8.x with default x86-64 analysis settings. Allow auto-analysis to complete (5-10 minutes for a binary of this size). Accept the detected compiler (GCC).

3. **Batch decompile.** Run the following IDA Python script to decompile all functions and export per-function C files:
   ```python
   import idautils, ida_hexrays, idc
   for func_ea in idautils.Functions():
       try:
           cfunc = ida_hexrays.decompile(func_ea)
           name = idc.get_func_name(func_ea)
           addr = f"0x{func_ea:X}"
           with open(f"decompiled/{name}_{addr}.c", "w") as f:
               f.write(str(cfunc))
       except:
           pass
   ```

4. **Export databases.** Use IDA Python to export the five JSON databases (functions, strings, xrefs, callgraph, names) to `cicc/databases/`. The function export should iterate `Functions()` and record address, name, size, instruction count, is_library, is_thunk, callers, and callees for each. The string export should iterate IDA's string list and record address, value, and xrefs.

5. **Run L0 foundation pipeline.**
   ```bash
   cd cicc/scripts
   bash run_foundation_analysis.sh
   ```
   This executes Steps 0-6 in sequence, producing all indices, classifications, and the coverage tracker. Expected runtime: 2-5 minutes on a modern machine.

6. **Run L1 module setup.**
   ```bash
   bash run_l1_programmatic.sh
   ```
   This creates the per-module directory structure, extracts metadata for CRITICAL and HIGH functions, and generates module README skeletons. Expected runtime: under 1 minute.

7. **Run standalone analyses** (optional, for deeper investigation):
   ```bash
   python3 analyze_nvvm_pipeline.py       # NVVM pipeline trace
   python3 deep_pipeline_trace.py          # Deep BFS from nvvmCompileProgram
   python3 extract_pipeline_structure.py   # Pass extraction from strings
   python3 analyze_performance_hotspots.py # Hotspot ranking
   python3 validate_callgraph.py           # Validation report
   ```

8. **Run evidence indexing** (optional, for quality assessment):
   ```bash
   cd foundation
   python3 build_evidence_index_v2.py
   ```

9. **Begin manual analysis.** With the foundation data in place, start from the CRITICAL priority list and the string anchors described in the Function Identification Strategies section above. The [Function Map](./function-map.md) page is the primary lookup table.

### Dependencies

The analysis scripts require only the Python 3.8+ standard library with one exception: `07_analyze_cross_module_dependencies.py` uses `ijson` for streaming JSON parsing of the large callgraph file. Install with `pip install ijson`. All other scripts use only `json`, `pickle`, `re`, `collections`, `pathlib`, `statistics`, `dataclasses`, and `typing`.

### Binary Address Sweep Reports

In addition to the automated scripts, the analysis produced 90+ raw binary sweep reports stored in `cicc/raw/`. Each report covers a contiguous address range (typically 128 KB to 512 KB) and contains per-function identification notes, string evidence citations, structural observations, and confidence assessments. The reports are named by address range (e.g., `p1.3-01-sweep-0x8F0000-0x90FFFF.txt` covers the compilation pipeline entry region) and organized into 10 sweep phases corresponding to the binary's major sections. A second sweep phase (`p2-*` and `p2a-p2g`) provides focused analyses of specific subsystems (EDG frontend, IR generation, optimization passes, SelectionDAG, register allocation, scheduling, configuration).

These raw reports are the primary source material from which the wiki pages were written. They are not cleaned or edited for presentation -- they contain working notes, false starts, and corrections made during the analysis process.

## Limitations and Known Gaps

This analysis has several inherent limitations:

- **No dynamic validation.** All findings are from static analysis. Runtime behavior under specific inputs (unusual SM targets, edge-case CUDA constructs) has not been verified.
- **EDG internals are partially opaque.** The EDG frontend is a licensed third-party component. Its internal data structures are less well-documented in the LLVM literature, making identification harder. The IL tree format and scope management structures are identified at MEDIUM confidence.
- **Inlined functions are invisible.** If the compiler inlined a function during the build of cicc itself, that function has no standalone address and cannot be independently identified. Some small LLVM utility functions (SmallVector operations, StringRef comparisons) are likely inlined throughout.
- **Proprietary NVIDIA code has no public reference.** The 35 custom NVIDIA passes, the NVVM bridge layer, and the NVVMPassOptions system have no upstream source to compare against. These are identified purely from string evidence and structural analysis.
- **Version-specific.** All findings apply to cicc v13.0 (build `cuda_13.0.r13.0/compiler.36424714_0`). Addresses, function sizes, and pass counts will differ in other CUDA toolkit versions.
- **Module classification accuracy degrades at the boundary.** The automated taxonomy assigns ~60% of functions with high confidence (wiki ground truth or strong string evidence). The remaining functions are classified by call proximity propagation or address range heuristics at 30-60% confidence. Functions at module boundaries may be misclassified; the `validate_callgraph.py` script quantifies this.
- **Callgraph completeness depends on IDA's xref analysis.** Indirect calls through function pointers (vtable dispatch, callback registrations) are not fully captured by IDA's static analysis. The call graph is therefore a lower bound on the true call relationships. This primarily affects LLVM's pass manager dispatch and the EDG frontend's visitor pattern implementations.
