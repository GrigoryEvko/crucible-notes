# Methodology

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

This page documents how the reverse engineering of ptxas v13.0.88 was performed. It serves as a transparency record so readers can assess the confidence of any claim in this wiki, and as a practical guide for anyone who wants to reproduce or extend the analysis.

## Scope and Scale

PTXAS is a 37.7 MB stripped x86-64 ELF binary with no debug symbols, no DWARF information, and no export table beyond 146 libc/libpthread PLT stubs. Unlike NVIDIA's cicc (which is an LLVM fork), ptxas contains no LLVM code, no EDG frontend, and no third-party optimizer components. Every pass, data structure, and encoding table is proprietary NVIDIA code. This makes the analysis harder than LLVM-derived binaries — there is no upstream source to compare against.

| Metric | Value |
|---|---|
| Binary size | 37,741,528 bytes |
| Build string | `cuda_13.0.r13.0/compiler.36424714_0` |
| Total functions detected | 40,185 |
| Functions decompiled | 39,881 (99.2%) |
| Strings extracted | 30,632 |
| Call graph edges | 548,693 |
| Cross-references | 7,427,044 |
| IDA comments recovered | 66,598 |
| IDA auto-names recovered | 16,019 |
| Control flow graphs exported | 80,078 |
| PLT imports | 146 (libc, libpthread, libm, libgcc) |
| Functions with 0 static callers | 15,907 (39.6%) — vtable-dispatched |
| Functions < 100 bytes | 11,532 (28.7%) |
| Functions > 10 KB | 86 (0.2%) |
| Named functions (not `sub_*`) | 319 (0.8%) |
| Internal codenames | OCG (Optimizing Code Generator), Mercury (SASS encoder), Ori (IR) |

The 304 functions that Hex-Rays could not decompile are predominantly PLT stubs, computed-jump trampolines in the Flex DFA scanner, and the four mega-dispatch functions exceeding 200 KB (too large for Hex-Rays to handle within default limits). None are in critical analysis paths — the dispatch functions are understood from their callee lists and the PLT stubs from their import names.

## Why PTXAS Is Harder Than LLVM-Based Binaries

Reverse engineering cicc (NVIDIA's LLVM-based CUDA compiler) benefits from extensive prior art: LLVM's open-source codebase provides structural templates, pass names are registered in predictable patterns, and `cl::opt` strings directly name their global variables. PTXAS offers none of these advantages:

- **No upstream source.** Every identified function is identified from first principles — string evidence, callgraph position, structural fingerprinting, or decompiled algorithm analysis. There is no reference implementation to compare against.
- **ROT13 obfuscation.** Internal names for tuning knobs and PTX opcode mnemonics are ROT13-encoded in the binary, requiring decoding before they become useful anchors.
- **Vtable-heavy architecture.** 39.6% of functions have zero static callers because they are dispatched through vtable pointers or function pointer tables. The call graph alone cannot reach them.
- **Template-generated code.** The SASS backend contains approximately 4,000 encoding handler functions generated from templates, each structurally near-identical. These dominate the function count but carry almost no unique identifying features.
- **No pass registration infrastructure.** LLVM passes register themselves via `PassInfo` objects with name strings. PTXAS phases are allocated by a factory switch (`sub_C60D30`) and their names are only visible through the `NamedPhases` registry and `AdvancedPhase*` timing strings — far fewer anchors than LLVM's registration system.

## Toolchain

All analysis was performed with IDA Pro 8.x and the Hex-Rays x86-64 decompiler. The entire effort is static analysis of the binary at rest — no dynamic analysis (debugging, tracing, instrumentation) was used for function identification. Runtime tools (`ptxas --stat`, `DUMPIR` knob, `--keep`) were used only for validation and cross-referencing.

| Tool | Purpose |
|---|---|
| IDA Pro 8.x | Disassembly, auto-analysis, cross-referencing, vtable reconstruction |
| Hex-Rays decompiler | Pseudocode generation for 39,881 recovered functions |
| IDA Python scripting | Complete database extraction: all 8 JSON artifact exports |
| Custom Python script | `analyze_ptxas.py`: batch string, function, graph, xref, and decompilation export |
| ptxas CLI | `--stat`, `--verbose`, `--compiler-stats`, `--fdevice-time-trace` for runtime validation |
| ptxas DUMPIR knob | `-knob DUMPIR=<phase>` to dump IR at specific pipeline points |
| ROT13 decoder | Standard `codecs.decode(s, "rot_13")` for 2,000+ obfuscated knob/opcode names. A turnkey script lives at `tools/decode_rot13_knobs.py` (operates on `ptxas_strings.json`, supports `--prefix` filtering for subsystem triage). |

## IDA Pro Setup and Initial Analysis

### Loading the Binary

PTXAS is a dynamically-linked ELF with 146 PLT imports but no symbol table beyond those imports. IDA auto-analysis settings:

1. **Processor**: Meta PC (x86-64)
2. **Analysis options**: default. IDA correctly identifies the Flex DFA scanner tables, Bison parser tables, and the `.ctors`/`.dtors` sections.
3. **Auto-analysis time**: approximately 8-10 minutes on a modern machine for the 37.7 MB binary.
4. **Compiler detection**: IDA identifies GCC as the compiler. The binary uses the Itanium C++ ABI (confirmed by the embedded C++ name demangler at `sub_1CDC780`, 15,988 B native / 90 KB decomp).

### Post-Auto-Analysis Steps

After auto-analysis completes:

1. **Run string extraction.** IDA's auto-analysis finds 30,632 strings. All are exported via the `analyze_ptxas.py` IDA Python script.
2. **Force function creation.** Some address ranges, particularly the template-generated encoding handlers, are not automatically recognized as functions. IDA's "Create function" (P key) was applied selectively in the `0xD27000`--`0x1579000` range where encoding handler stubs are tightly packed.
3. **Batch decompile.** The IDA Python script iterates all 40,185 detected functions and calls `ida_hexrays.decompile()` on each, saving per-function `.c` files. 39,881 succeeded; 304 failed (PLT stubs, computed-jump trampolines, and 4 mega-functions exceeding decompiler limits).
4. **Export control flow graphs.** For each function, the script extracts the `FlowChart` (basic blocks, edges, per-instruction disassembly) as JSON. 80,078 graph files were produced.

### Type Recovery

PTXAS uses no C++ RTTI (no `typeid`, no `dynamic_cast` — the binary has no `.data.rel.ro` RTTI structures). Type recovery relies on:

- **Vtable layout analysis.** Each vtable is a contiguous array of function pointers in `.data.rel.ro` (4,256 bytes total). The vtable at `off_22BD5C8` contains 159 entries, one per optimization phase. Each entry points to the phase's constructor function.
- **Structure offset patterns.** The pool allocator struct has free-list bins at offset +2128 and a mutex at +7128. The thread-local context is a 280-byte struct accessed via `pthread_getspecific`. These offsets were recovered from the decompiled code of `sub_424070` (pool alloc, 3,809 callers) and `sub_4280C0` (TLS accessor, 3,928 callers).
- **Parameter/return type propagation.** Once a function's signature is established (e.g., `pool_alloc(pool*, size_t) -> void*`), Hex-Rays propagates types to all 3,809 call sites, improving decompilation quality throughout the binary.

## String-Driven Analysis

Strings are the single most productive source of function identification in ptxas. Of the 30,632 strings extracted, several categories are particularly valuable.

### ROT13-Encoded Knob Names (2,000+ entries)

PTXAS uses ROT13 encoding as a light obfuscation layer on internal configuration names. Two massive static constructors populate these tables at startup:

- **`ctor_005`** at `0x40D860` (80 KB) registers approximately 2,000 general OCG tuning knobs
- **`ctor_007`** at `0x421290` (8 KB) registers 98 Mercury scheduler knobs

Each entry pairs a ROT13-encoded name with a hex-encoded default value. Decoding examples:

| ROT13 in binary | Decoded name |
|---|---|
| `ZrephelHfrNpgvirGuernqPbyyrpgvirVafgf` | `MercuryUseActiveThreadCollectiveInsts` |
| `ZrephelGenpxZhygvErnqfJneYngrapl` | `MercuryTrackMultiReadsWarLatency` |
| `ZrephelCerfhzrKoybpxJnvgOrarsvpvny` | `MercuryPresumeXblockWaitBeneficial` |
| `ZrephelZretrCebybthrOybpxf` | `MercuryMergePrologueBlocks` |
| `ZrephelTraFnffHPbqr` | `MercuryGenSassUCode` |
| `FpniVayvarRkcnafvba` | `ScavInlineExpansion` |
| `FpniQvfnoyrFcvyyvat` | `ScavDisableSpilling` |

The knob names directly reveal subsystem organization. Names prefixed with `Mercury*` belong to the SASS encoder. Names prefixed with `Scav*` belong to the register allocator's scavenger. Names like `XBlockWait*` and `WarDeploy*` belong to the instruction scheduler. The knob lookup function `GetKnobIndex` at `sub_79B240` performs inline ROT13 decoding and case-insensitive comparison, which was itself identified by tracing the xrefs from the ROT13-encoded strings.

### ROT13-Encoded PTX Opcode Names (~900 entries)

A third static constructor, `ctor_003` at `0x4095D0` (17 KB), populates a table of ~900 ROT13-encoded PTX opcode mnemonics. Decoding examples:

| ROT13 | Decoded |
|---|---|
| `NPDOHYX` | `ACQBULK` |
| `OFLAP` | `BSYNC` |
| `VFRGC` | `ISETP` |
| `QFRGC` | `DSETP` |
| `SFRGC` | `FSETP` |
| `OERNX` | `BREAK` |
| `QRCONE` | `DEPBAR` |
| `YQGENZ` | `LDTRAM` |

These strings are used by the PTX parser to match instruction mnemonics. Each xref from one of these strings leads to a parser action or instruction validator function.

### Timing and Phase Name Strings

The compilation driver at `sub_446240` emits per-stage timing via format strings:

```text
Parse-time            : %.3f ms (%.2f%%)
CompileUnitSetup-time : %.3f ms (%.2f%%)
DAGgen-time           : %.3f ms (%.2f%%)
OCG-time              : %.3f ms (%.2f%%)
ELF-time              : %.3f ms (%.2f%%)
DebugInfo-time        : %.3f ms (%.2f%%)
PeakMemoryUsage = %.3lf KB
```

Tracing the xrefs from these format strings identifies the code that brackets each pipeline stage, revealing the stage boundaries within `sub_446240`.

The `NamedPhases` registry (string at `0x21B64C8`, xrefs to `sub_9F4040`) and the `AdvancedPhase*` timing strings provide phase-level anchors within the 159-phase optimization pipeline:

- `AdvancedPhaseBeforeConvUnSup`, `AdvancedPhaseAfterConvUnSup`
- `AdvancedPhaseEarlyEnforceArgs`, `AdvancedPhaseLateConvUnSup`
- `AdvancedPhasePreSched`, `AdvancedPhaseAllocReg`, `AdvancedPhasePostSched`
- `AdvancedPhaseOriPhaseEncoding`, `AdvancedPhasePostFixUp`
- `GeneralOptimizeEarly`, `GeneralOptimize`, `GeneralOptimizeMid`, `GeneralOptimizeMid2`
- `GeneralOptimizeLate`, `GeneralOptimizeLate2`
- `OriPerformLiveDead`, `OriPerformLiveDeadFirst` through `OriPerformLiveDeadFourth`

Each `AdvancedPhase*` string xrefs to exactly one call site, which is a boundary marker in the phase pipeline. These 15 markers divide the 159-phase pipeline into named segments whose boundaries were used to identify the phases between each pair of markers.

### Error and Diagnostic Strings

The central diagnostic emitter `sub_42FBA0` (2,350 callers) prints error messages whose text reveals the calling function's purpose. Examples:

- `"Please use -knob DUMPIR=AllocateRegisters for debugging"` — identifies the register allocator failure path at `sub_9714E0`
- `"SM does not support LDCU"` — identifies SM capability checking in the instruction legalizer
- `"Invalid knob identifier"`, `"Invalid knob specified (%s)"` — identifies the knob parsing infrastructure around `sub_79D070`
- `"fseek() error knobsfile %s"`, `"[knobs]"` — identifies `ReadKnobsFile` at `sub_79D070`

### Build and Version Strings

```text
Cuda compilation tools, release 13.0, V13.0.88
Build cuda_13.0.r13.0/compiler.36424714_0
```

The version string at `sub_612DE0` identifies both the exact build and the version reporting function. The `Usage  :` string at `0x1CE3666` identifies the usage printer. The `"\nCompile-unit with entry %s"` string identifies the per-kernel compilation loop within the driver.

## Vtable-Driven Discovery

### The Phase Vtable Table

The most productive vtable discovery was the phase vtable table at `off_22BD5C8` in `.rodata`. This is an array of 159 pointers, each pointing to a vtable for one optimization phase class. The phase factory function at `sub_C60D30` is a 159-case switch statement that allocates a 16-byte phase object and assigns the corresponding vtable from this table:

```c
// Simplified from decompiled sub_C60D30
switch (phase_index) {
    case 0:  obj->vtable = off_22BD5C8[0];  break;
    case 1:  obj->vtable = off_22BD5C8[1];  break;
    ...
    case 158: obj->vtable = off_22BD5C8[158]; break;
}
return obj;
```

Each vtable contains pointers to the phase's virtual methods. The virtual method at slot 0 is `execute()` (the phase body). The virtual method at slot 1 is `isNoOp()` (returns whether the phase should be skipped). The virtual method at slot 2 is `getName()` (returns the phase name string).

By following each of the 159 vtable entries to their `execute()` slot, every optimization phase's main function was identified. The `getName()` slot provided the phase name for phases that implement it. For phases that return a constant empty string, the name was inferred from the `NamedPhases` registry or from the `AdvancedPhase*` timing strings that bracket the phase in the pipeline.

### Encoding Handler Vtables

The SASS backend uses vtable dispatch for instruction encoding. Each SASS opcode variant has its own encoding handler function, registered in dispatch tables rather than called directly. This explains why 15,907 functions (39.6%) have zero static callers — they are reached exclusively through indirect calls via function pointer tables.

The encoding handler vtables were identified by their structural uniformity: every handler in the `0xD27000`--`0x1579000` range follows an identical template:

1. Set opcode ID via bitfield insert into the instruction word at `a1+544`
2. Load a 128-bit format descriptor from `.rodata` via SSE (`movaps xmm0, xmmword_XXXXXX`)
3. Initialize a 10-slot register class map
4. Register operand descriptors via `sub_7BD3C0` / `sub_7BD650` / `sub_7BE090`
5. Finalize encoding via `sub_7BD260`
6. Extract bitfields from the packed instruction word

The uniformity of this template allowed batch identification: once the template was recognized in a few handlers, the remaining ~4,000 were identified by structural matching alone.

### Peephole Optimizer Vtable

The `PeepholeOptimizer` class at `0x7A5D10` has a reconstructed vtable with 7 virtual methods:

| Slot | Method | Purpose |
|---|---|---|
| 0 | `Init` | Initialize peephole state for a compilation unit |
| 1 | `RunOnFunction` | Entry point for per-function peephole optimization |
| 2 | `RunOnBB` | Per-basic-block dispatch |
| 3 | `RunPatterns` | Standard pattern matching pass |
| 4 | `SpecialPatterns` | Architecture-specific pattern pass |
| 5 | `ComplexPatterns` | Multi-instruction pattern pass |
| 6 | `SchedulingAwarePatterns` | Schedule-preserving pattern pass |

The three peephole dispatch mega-functions (`sub_143C440` at 233 KB, `sub_18A2CA0` at 231 KB, `sub_198BCD0` at 239 KB) each serve a different SM generation family and call 1,100–1,336 pattern matcher functions. These dispatchers were identified by their enormous callee counts and their position in the pipeline after instruction encoding.

## Callgraph Analysis

The 548,693-edge call graph, exported from IDA, reveals the binary's module structure and function relationships. Several callgraph properties were systematically exploited.

### Hub Function Identification

Functions with extreme callee or caller counts serve as structural anchors:

**Top callees (hub functions — "fan-out" nodes):**

| Address | Name | Size | Callees | Role |
|---|---|---|---|---|
| `sub_169B190` | ISel master dispatch | 280 KB | 15,870 | The single largest function in the binary. Dispatches to all ISel pattern matchers. |
| `sub_143C440` | SM120 peephole dispatch | 233 KB | 13,425 | SM120 (RTX 50-series) peephole optimization |
| `sub_198BCD0` | Peephole dispatch (variant 2) | 239 KB | 13,391 | Peephole optimization for another SM family |
| `sub_18A2CA0` | Peephole dispatch (variant 1) | 231 KB | 12,974 | Peephole optimization for another SM family |
| `sub_BA9D00` | Bitvector/CFG analysis | 204 KB | 11,335 | Dataflow framework core |

**Top callers (utility functions — "fan-in" nodes):**

| Address | Name | Size | Callers | Role |
|---|---|---|---|---|
| `sub_B28F30` | (unknown leaf) | 12 B | 31,399 | Tiny utility, likely a type tag or opcode check |
| `sub_10AE5C0` | (unknown leaf) | 60 B | 30,768 | Small encoding helper |
| `.sprintf` | libc sprintf | 6 B | 20,398 | String formatting (PLT stub) |
| `sub_7B9B80` | Bitfield insert | 216 B | 18,347 | Inserts bits into the 1280-bit instruction word |
| `sub_424070` | Pool allocator | 2,098 B | 3,809 | Custom memory allocator |
| `sub_4280C0` | TLS context accessor | 597 B | 3,928 | Thread-local storage via `pthread_getspecific` |
| `sub_42FBA0` | Diagnostic emitter | 2,388 B | 2,350 | Central error/warning reporter |

The fan-out nodes identify the mega-dispatch functions: ISel, peephole, and dataflow. The fan-in nodes identify the shared infrastructure layer: memory allocation, encoding primitives, string formatting, and error reporting.

### Module Boundary Detection

The call graph reveals clear module boundaries. Functions in the `0x400000`--`0x67F000` range (PTX frontend) rarely call functions in `0xC52000`--`0x1CE3000` (SASS backend) directly, and vice versa. The optimizer region (`0x67F000`--`0xC52000`) bridges the two, calling into both the frontend (for IR construction) and the backend (for encoding).

The call graph was used to validate the three-subsystem decomposition:

| Call direction | Edge count | Interpretation |
|---|---|---|
| Frontend -> Frontend | ~8,000 | Internal frontend cohesion |
| Frontend -> Optimizer | ~1,200 | IR construction handoff |
| Optimizer -> Optimizer | ~15,000 | Phase-to-phase internal calls |
| Optimizer -> Backend | ~3,500 | Scheduling, encoding setup |
| Backend -> Backend | ~18,000 | Encoding handler internal calls |
| Backend -> Frontend | ~500 | Shared infrastructure (allocator, hash) |

### Propagation from Known Functions

Once a high-confidence function is identified, its callees and callers gain contextual identity. The most productive propagation chains:

1. **`sub_446240` (real main, CERTAIN)** -> calls stage entry points for Parse, DAGgen, OCG, ELF, DebugInfo. Each stage's entry point was identified by following the timing format string pattern.

2. **`sub_C62720` (PhaseManager constructor)** -> allocates 159 phase objects via `sub_C60D30` (factory). The factory's 159 case targets are the phase constructors. Each constructor installs a vtable whose slot 0 points to the phase's `execute()` method.

3. **`sub_79B240` (GetKnobIndex)** -> called from every function that reads a tuning knob. The first argument to `GetKnobIndex` is the ROT13-encoded knob name, so every call site reveals which knob a function checks.

4. **`sub_42FBA0` (diagnostic emitter)** -> the format string argument at each of the 2,350 call sites reveals the error context. A call with `"Cannot take address of texture/surface variable (%s)"` identifies a PTX semantic checker.

## Pattern Recognition

### 16-Byte Phase Objects

All 159 optimization phases share a uniform object layout:

```text
Offset 0: vtable pointer (8 bytes) -- points to phase-specific vtable
Offset 8: phase data pointer or inline data (8 bytes)
```

The phase factory (`sub_C60D30`) allocates each phase as a 16-byte object from the pool allocator, sets the vtable pointer from the vtable table at `off_22BD5C8`, and returns the object. The PhaseManager stores these 159 objects in its internal array and iterates them to execute the pipeline.

### Pool Allocator Usage Pattern

The custom pool allocator (`sub_424070`, 3,809 callers) is the dominant allocation mechanism. Its usage pattern is recognizable throughout the binary:

```c
ptr = sub_424070(pool, size);   // Allocate
if (!ptr) sub_42BDB0();         // Fatal OOM -- never returns
// ... use ptr ...
sub_4248B0(ptr);                // Free (1,215 callers)
```

The OOM handler `sub_42BDB0` (14 bytes, 3,825 callers) is a tiny wrapper that calls `sub_42F590` (fatal internal error). Because every allocation site checks for failure and calls the same handler, the allocator usage pattern is a reliable structural marker. Finding `sub_42BDB0` in a function's callee list confirms that function performs heap allocation.

### SASS Encoding Handler Template

Every encoding handler in the backend follows a rigid 6-step template (described in the vtable section above). The key identification markers:

- Calls to `sub_7B9B80` (bitfield insert, 18,347 callers)
- SSE `movaps` loading a 128-bit constant from `.rodata`
- Calls to `sub_7BD3C0`, `sub_7BD650`, or `sub_7BE090` (operand registrars)
- Final call to `sub_7BD260` (encoding finalize)

Any function matching this pattern is a SASS encoding handler. This template recognition identified approximately 4,000 handlers spanning 6 SM architecture generations.

### Hash Map Infrastructure Pattern

The MurmurHash3-based hash map infrastructure (`sub_426150` insert, `sub_426D60` lookup, `sub_427630` MurmurHash3) appears throughout the binary with a consistent usage pattern:

```c
map = sub_425CA0(hash_fn, cmp_fn, initial_capacity);  // Create
sub_426150(map, key, value);                           // Insert (2,800 callers)
result = sub_426D60(map, key);                         // Lookup (422 callers)
sub_425D20(map);                                       // Destroy
```

The MurmurHash3 constants (`0xcc9e2d51`, `0x1b873593`) in `sub_427630` confirmed the hash algorithm. The hash map supports three modes (custom function pointers, pointer hash, integer hash) selected by flags at struct offset 84.

## Data Artifacts

The complete IDA database was exported via `analyze_ptxas.py` into 8 JSON artifacts. These artifacts are the foundation for all subsequent analysis.

| Artifact | File | Size | Entries | Schema |
|---|---|---|---|---|
| **Functions** | `ptxas_functions.json` | 92 MB | 40,185 | `{addr, end, name, size, insn_count, is_library, is_thunk, callers[], callees[]}` |
| **Strings** | `ptxas_strings.json` | 4.8 MB | 30,632 | `{addr, value, type, xrefs[{from, func, type}]}` |
| **Call graph** | `ptxas_callgraph.json` | 64 MB | 548,693 | `{from, from_addr, to, to_addr}` — one edge per call site |
| **Cross-references** | `ptxas_xrefs.json` | 978 MB | 7,427,044 | Complete xref database (code, data, string references) |
| **Comments** | `ptxas_comments.json` | 5.9 MB | 66,598 | `{addr, type, text}` — IDA auto-comments and analyst annotations |
| **Names** | `ptxas_names.json` | 972 KB | 16,019 | `{addr, name}` — IDA auto-generated and analyst-assigned names |
| **Imports** | `ptxas_imports.json` | 17 KB | 146 | `{module, name, addr, ordinal}` — PLT import stubs |
| **Segments** | `ptxas_segments.json` | 3 KB | 24 | `{name, start, end, size, type, perm}` — ELF segment map |

Total artifact storage: 1.14 GB (dominated by the 978 MB xref database).

### What Each Artifact Reveals

**Functions** (`ptxas_functions.json`): The master index. Every function's address, size, instruction count, caller list, and callee list. The caller/callee lists are the basis for callgraph analysis. The `is_thunk` flag identifies PLT stubs (exclude from analysis). The `is_library` flag identifies functions IDA tagged as library code (CRT startup, jemalloc-like allocator internals).

**Strings** (`ptxas_strings.json`): The primary identification tool. Each string's xref list shows which functions reference it. Searching for `"AdvancedPhase"` returns 15 strings, each xref pointing to a pipeline boundary in the PhaseManager. Searching for strings starting with `"Z"` (ROT13 "M" for "Mercury") returns the Mercury subsystem's knob names. The 2,035 hex-encoded default value strings (`"0k..."` / `"0x..."`) are paired 1:1 with knob name strings in the constructors.

**Call graph** (`ptxas_callgraph.json`): The structural backbone. Each edge records a direct call from one function to another. Indirect calls (vtable dispatch, function pointer callbacks) are not captured, which is the primary limitation — the 15,907 zero-caller functions are almost all vtable-dispatched. The call graph is used for module boundary detection, propagation from known functions, and entry/exit point analysis.

**Cross-references** (`ptxas_xrefs.json`): The most comprehensive artifact. Contains all code-to-code, code-to-data, and data-to-data references detected by IDA. At 7.4 million entries, it is too large to load into memory on machines with less than 16 GB RAM. Used for deep analysis of specific functions: finding all references to a particular `.rodata` constant, tracing data flow through global variables, and identifying vtable consumers.

**Comments** (`ptxas_comments.json`): IDA's auto-generated comments (e.g., `"File format: \\x7FELF"`) plus analyst-added annotations. The auto-comments on function prologues identify calling conventions and stack frame layouts. Analyst comments record identification rationale for reviewed functions.

**Names** (`ptxas_names.json`): IDA's auto-generated names for data and code addresses. Of 16,019 entries, approximately 9,670 are auto-generated string reference names (`aLib64LdLinuxX8`, `aGnu`, etc.) and ~6,349 are analyst-assigned or IDA-recovered names (PLT stubs, constructors, etc.). These names appear in the callgraph edges as `from`/`to` identifiers.

**Imports** (`ptxas_imports.json`): The 146 PLT imports. Key imports include `pthread_*` (13 functions), `malloc`/`free`/`realloc`, `_setjmp`/`longjmp` (used by the error recovery system), `select`/`fcntl` (used by the GNU Make jobserver client), and `clock` (used by the timing infrastructure).

**Segments** (`ptxas_segments.json`): The 24 ELF segments/sections. Used to establish the address space layout and map code/data boundaries. The `.ctors` section (104 bytes, 12 entries) is particularly important — it lists the static constructors that initialize the ROT13 tables and the knob registry.

## The 30-Region Sweep Approach

The primary analysis was conducted as a systematic address-range sweep of the entire `.text` section, divided into 30 contiguous regions. Each region was analyzed independently in a single session, producing a raw sweep report. The 40 report files (including sub-region splits) total 34,880 lines of working notes.

### Region Partitioning

The `.text` section (`0x403520`--`0x1CE2DE2`, 26.2 MB) was divided into approximately 870 KB regions. The partitioning was not arbitrary — region boundaries were chosen to align with subsystem boundaries where possible, so that each sweep report covers a coherent functional area.

| Report | Address Range | Size | Functions | Subsystem |
|---|---|---|---|---|
| p1.01 | `0x400000`--`0x4D5000` | 853 KB | 1,383 | Runtime infra + CLI + PTX validators |
| p1.02 | `0x4D5000`--`0x5AA000` | 853 KB | 581 | PTX text generation (580 formatters) |
| p1.03 | `0x5AA000`--`0x67F000` | 853 KB | 628 | Intrinsics + SM profiles |
| p1.04 | `0x67F000`--`0x754000` | 469 KB | ~500 | Mercury core + scheduling engine |
| p1.05 | `0x754000`--`0x829000` | 853 KB | 1,545 | Knobs + peephole optimizer class |
| p1.06 | `0x829000`--`0x8FE000` | 853 KB | 1,069 | Debug tables + scheduler + HW profiles |
| p1.07 | `0x8FE000`--`0x9D3000` | 853 KB | 1,090 | Register allocator (fatpoint) |
| p1.08 | `0x9D3000`--`0xAA8000` | 853 KB | 1,218 | Post-RA pipeline + NamedPhases |
| p1.09 | `0xAA8000`--`0xB7D000` | 853 KB | 4,493 | GMMA/WGMMA + ISel + emission |
| p1.10 | `0xB7D000`--`0xC52000` | 853 KB | 1,086 | CFG analysis + bitvectors |
| p1.11 | `0xC52000`--`0xD27000` | 853 KB | 1,053 | PhaseManager + phase factory |
| p1.12 | `0xD27000`--`0xDFC000` | 853 KB | 592 | SM100 SASS encoders (set 1) |
| p1.13 | `0xDFC000`--`0xED1000` | 853 KB | 591 | SM100 SASS encoders (set 2) + decoders |
| p1.14 | `0xED1000`--`0xFA6000` | 853 KB | 683 | SM100 SASS encoders (set 3) |
| p1.15 | `0xFA6000`--`0x107B000` | 853 KB | 678 | SM100 SASS encoders (set 4) |
| p1.16 | `0x107B000`--`0x1150000` | 853 KB | 3,396 | SM100 codec + 2,095 bitfield accessors |
| p1.17 | `0x1150000`--`0x1225000` | 853 KB | 733 | SM89/90 codec (decoders + encoders) |
| p1.18 | `0x1225000`--`0x12FA000` | 853 KB | 1,552 | Reg-pressure scheduling + ISel + encoders |
| p1.19 | `0x12FA000`--`0x13CF000` | 853 KB | 1,282 | Operand legalization + peephole |
| p1.20 | `0x13CF000`--`0x14A4000` | 853 KB | 1,219 | SM120 peephole pipeline |
| p1.21 | `0x14A4000`--`0x1579000` | 853 KB | 606 | Blackwell ISA encode/decode |
| p1.22 | `0x1579000`--`0x164E000` | 853 KB | 1,324 | Encoding + peephole matchers |
| p1.23 | `0x164E000`--`0x1723000` | 853 KB | 899 | ISel pattern matching core |
| p1.24 | `0x1723000`--`0x17F8000` | 853 KB | 631 | ISA description database |
| p1.25 | `0x17F8000`--`0x18CD000` | 853 KB | 1,460 | SASS printer + peephole dispatch |
| p1.26 | `0x18CD000`--`0x19A2000` | 853 KB | 1,598 | Scheduling + peephole dispatchers |
| p1.27 | `0x19A2000`--`0x1A77000` | 853 KB | 1,393 | GPU ABI + SM89/90 encoders |
| p1.28 | `0x1A77000`--`0x1B4C000` | 853 KB | 1,518 | SASS emission backend |
| p1.29 | `0x1B4C000`--`0x1C21000` | 853 KB | 1,974 | SASS emission + format descriptors |
| p1.30 | `0x1C21000`--`0x1CE3000` | 780 KB | 1,628 | ELF emitter + infra library layer |

Several regions were further split into sub-reports (p1.04a/b, p1.05a/b, p1.06a/b, p1.07a/b, p1.08a/b) when the initial analysis revealed that a region contained multiple distinct subsystems requiring separate treatment.

### Sweep Report Structure

Each sweep report follows a consistent format:

```text
================================================================================
P1.XX SWEEP: Functions in address range 0xAAAA000 - 0xBBBB000
================================================================================
Range: 0xAAAA000 - 0xBBBB000
Files found: NNN decompiled .c files (of which ~MMM are > 1KB)
Total decompiled size: X,XXX,XXX bytes
Functions in range (from DB): NNN
Named functions: NNN (or 0 if all are sub_XXXXXX)
Functions with identified callers: NNN

CONTEXT: [1-paragraph summary of the region's purpose]

================================================================================
SECTION 1: [Subsystem name]
================================================================================

### 0xAAAAAA -- sub_AAAAAA (NNNN bytes / NNN lines)
**Identity**: [Function identification]
**Confidence**: [CERTAIN / HIGH / MEDIUM]
**Evidence**:
  - [String evidence]
  - [Structural evidence]
  - [Callgraph evidence]
**Key code**:
  [Relevant decompiled excerpts]
**Note**: [Additional observations]
```

Each function entry records the address, size, decompiled line count, proposed identity, confidence level, evidence citations, and key code excerpts. The reports are raw working notes — they contain false starts, corrections, and evolving hypotheses that were resolved as more context became available.

### Analysis Ordering

The sweep was not performed in address order. The analysis followed an information-maximizing sequence:

1. **p1.01** (infrastructure + CLI) first — establishes the allocator, hash map, TLS, and diagnostic patterns that appear throughout the binary.
2. **p1.11** (PhaseManager) second — identifies all 159 phases and their vtable entries, providing the skeleton of the optimization pipeline.
3. **p1.07** (register allocator) and **p1.06** (scheduler) third — these are the highest-complexity subsystems with the richest string evidence.
4. **p1.12–p1.15** (SASS encoders) in batch — once the encoding template was recognized, all encoder regions were swept rapidly with template matching.
5. **p1.30** (library layer) late — identifies shared infrastructure (ELF emitter, demangler, thread pool) referenced by earlier regions.
6. Remaining regions filled in by decreasing information density.

## Cross-Referencing with PTXAS CLI

Several ptxas command-line features and internal mechanisms provide runtime validation of static analysis findings.

### `--stat` and `--verbose`

Running `ptxas --stat input.ptx` prints per-kernel resource usage (register count, shared memory, stack frame size). This output is generated by `sub_A3A7E0` (the IR statistics printer), which was identified from the format strings:

```text
ptxas info    : Used %d registers, %d bytes smem, %d bytes cmem[0]
```

Comparing the `--stat` output against the decompiled statistics printer confirms the register counting and resource tracking logic.

### `--compiler-stats`

Enables the timing output (`Parse-time`, `DAGgen-time`, `OCG-time`, etc.) from `sub_446240`. This confirms the pipeline stage ordering and the stage boundary functions identified by string xrefs.

### `--fdevice-time-trace`

Generates Chrome trace JSON output showing per-phase timing. The trace parser at `sub_439880` and the `ftracePhaseAfter` string at `0x1CE383F` confirm the per-phase instrumentation infrastructure. The trace output lists phase names that can be cross-referenced against the 159-entry phase table.

### DUMPIR Knob

The internal `DUMPIR` knob (accessed via `-knob DUMPIR=<phase_name>`) dumps the Ori IR at specified pipeline points. The string `"Please use -knob DUMPIR=AllocateRegisters for debugging"` at `0x21EFBD0` confirms this mechanism. The `NamedPhases` registry at `sub_9F4040` maps phase names to pipeline positions. Available DUMPIR points include:

- `OriPerformLiveDead`, `OriPerformLiveDeadFirst` through `OriPerformLiveDeadFourth`
- `AllocateRegisters` (the register allocation phase)
- `swap1` through `swap6` (swap elimination phases)
- `shuffle` (instruction scheduling)

The DUMPIR output format reveals the IR structure: basic block headers, instruction opcodes, register names (R0–R255, UR0–UR63, P0–P7, UP0–UP7), and operand encodings. This runtime output was used to validate the IR format reconstructed from static analysis.

### `--keep` Flag

The `--keep` flag preserves intermediate files. While ptxas does not emit intermediate text files in the same way as nvcc, the `--keep` behavior in the overall CUDA compilation pipeline (nvcc -> cicc -> ptxas) allows inspecting the PTX input that reaches ptxas, confirming the PTX grammar and instruction format expectations.

## Confidence Levels

Every function identification in this wiki carries one of three confidence levels:

| Level | Meaning | Basis |
|---|---|---|
| **CERTAIN** | Identity is certain | Direct string evidence naming the function, or the function is a PLT import with a known name |
| **HIGH** | Strong identification (>90%) | Multiple corroborating indicators: string xrefs, callgraph position, structural fingerprint, decompiled algorithm match |
| **MEDIUM** | Probable identification (70–90%) | Single indicator (vtable position, size fingerprint, callgraph context) or inferred from surrounding identified functions |

The distribution across the ~200 key identified functions in the [Function Map](./function-map.md):

- **CERTAIN**: ~30 functions (PLT imports, `main`, functions with unique identifying strings)
- **HIGH**: ~130 functions (string evidence + structural confirmation)
- **MEDIUM**: ~40 functions (inferred from callgraph context or structural similarity)

The remaining ~39,985 functions are either unidentified (template-generated encoding handlers, small utility stubs) or identified at subsystem level only (e.g., "this is an SM100 SASS encoding handler" without knowing which specific opcode it encodes).

## Reproducing the Analysis

To reproduce this analysis from scratch:

1. **Obtain the binary.** Install CUDA Toolkit 13.0. The binary is at `<cuda>/bin/ptxas`. Verify: `ptxas --version` should report `V13.0.88` and the binary should be 37,741,528 bytes. Build string: `cuda_13.0.r13.0/compiler.36424714_0`.

2. **Run IDA auto-analysis.** Open ptxas in IDA Pro 8.x with default x86-64 settings. Allow auto-analysis to complete (8-10 minutes). Accept GCC as the detected compiler.

3. **Run the extraction script.** Load `analyze_ptxas.py` in IDA's Python console. The script exports all 8 JSON artifacts plus per-function decompiled C files, disassembly files, and control flow graph JSON files. Expected runtime: 4-8 hours for the full export (the xref export dominates).

4. **Decode ROT13 strings.** Apply `codecs.decode(s, "rot_13")` to all strings in the knob constructors (`ctor_003`, `ctor_005`, `ctor_007`). This decodes ~3,000 obfuscated names into readable English identifiers.

5. **Identify anchor functions.** Start with the highest-confidence identifications:
   - `main` at `0x409460` (named in symbol table)
   - `sub_446240` (real main — called from `main`, contains timing format strings)
   - `sub_C60D30` (phase factory — 159-case switch)
   - `sub_C62720` (PhaseManager constructor — references phase vtable table)
   - `sub_79B240` (GetKnobIndex — inline ROT13 decoding)
   - `sub_42FBA0` (diagnostic emitter — 2,350 callers, severity dispatch)

6. **Sweep the address space.** Work through the `.text` section in regions of ~870 KB. For each region:
   - Count functions and decompiled file sizes
   - Identify string anchors (search for region-specific strings)
   - Classify functions by structural template (encoding handler, phase body, utility, etc.)
   - Propagate identities from known callers/callees
   - Record findings in the sweep report format

7. **Cross-reference with runtime.** Compile a simple CUDA kernel and run `ptxas --stat --verbose --compiler-stats` to observe runtime behavior. Use `-knob DUMPIR=<phase>` to dump IR at specific pipeline points. Compare the dumped IR format against the IR structure reconstructed from decompiled code.

### Dependencies

The extraction script (`analyze_ptxas.py`) requires IDA Pro 8.x with Hex-Rays decompiler and Python 3.x. No external Python packages are needed — only the IDA Python API (`idautils`, `idc`, `idaapi`, `ida_bytes`, `ida_funcs`, `ida_segment`, `ida_nalt`, `ida_gdl`, `ida_hexrays`).

Post-export analysis requires only the Python 3.8+ standard library (`json`, `codecs`, `collections`).

## Debug Infrastructure: bugspec.txt

ptxas contains an internal fault injection framework that deliberately corrupts the Mercury IR to test compiler verification passes. The mechanism is entirely file-driven: if a file named `./bugspec.txt` exists in the current working directory when ptxas runs, the function `sub_A83AC0` reads it and injects controlled mutations into the post-register-allocation instruction stream. No CLI flag activates this — file presence alone is sufficient. If the file is absent, a diagnostic is printed to stdout (`Cannot open file with bug specification`) and compilation proceeds normally.

### File Format

The file contains a single line of six integers:

```text
COUNT0,COUNT1,COUNT2,COUNT3 COUNT4 COUNT5
```

The first four are comma-separated; then a space; then two space-separated values. Each integer specifies the number of faults to inject for that bug category. Zero or negative disables the category.

| Field | Variable | Category | Target |
|---|---|---|---|
| COUNT0 | v78 | Register bugs | General (R) and uniform (UR) register operands |
| COUNT1 | v79 | Predicate bugs | Predicated instruction operands |
| COUNT2 | v80 | Offset/spill bugs | Memory offsets in spill/refill instructions |
| COUNT3 | v81 | Remat bugs | Rematerialized value operands |
| COUNT4 | v82 | R2P/P2R bugs | Register-to-predicate conversion instructions |
| COUNT5 | v83 | Bit-spill bugs | Bit-level spill storage operands |

Example: `3,2,1,0 0 1` injects 3 register bugs, 2 predicate bugs, 1 offset bug, and 1 bit-spill bug.

### Bug Kind String Table

Each injected fault record carries a kind code (1–10) mapped to a string table at `0x21F0500`:

| Kind | String | Meaning |
|---|---|---|
| 1 | `r-ur register` | General or uniform register replaced with wrong register |
| 2 | `p-up register` | Predicate or uniform predicate register corrupted |
| 3 | `any reg` | Any register class operand corrupted |
| 4 | `offset` | Memory offset shifted by +16 bytes |
| 5 | `regular bug` | Generic operand value replacement |
| 6 | `predicated bug` | Predicate source operand corrupted |
| 7 | `remat bug` | Rematerialization value corrupted |
| 8 | `spill-regill bug` | Spill or refill path value corrupted |
| 9 | `r2p-p2r bug` | Register-predicate conversion operand corrupted |
| 10 | `bit-spill bug` | Bit-level spill storage operand corrupted |

### Injection Algorithm

The injection proceeds in four phases:

**1. Candidate collection.** The function walks the Mercury IR instruction linked list (from `context[0]+272`). For each instruction, it checks which bug categories are active and whether the instruction qualifies:

- **Register bugs (field0):** Scans operands for type-tag 1 (register) with register class 6 (general) or 3 (predicate), excluding opcodes 41–44. Eligible instructions are collected into a candidate list.
- **Predicate bugs (field1):** Checks flag byte at instruction+73 for bit 0x10 (predicated). Eligible instructions are collected separately.
- **Offset/spill bugs (field2):** Calls `sub_A56DE0` / `sub_A56CE0` against the register allocator state (`context[133]`) to identify spill/refill instructions.
- **Remat bugs (field3):** Queries the rematerialization hash table (`context+21` via `sub_A54200`) for instructions with remat entries.
- **R2P/P2R bugs (field4):** Checks instruction opcode (offset +72) for values 268, 155, 267, 173 (the R2P and P2R conversion opcodes, with bit-masked variants).
- **Bit-spill bugs (field5):** Checks operand count > 2, flag bit 0x10 at offset +28, and calls `sub_A53DB0` / `sub_A53C40` / `sub_A56880` for bit-spill eligibility.

**2. Random selection.** Seeds the RNG with `time(0)` via `srand()`. For each active category, `sub_A83490` randomly selects N instruction indices from the candidate list, where N is the count from bugspec.txt. The selector uses FNV-1a hashing on instruction addresses for collision avoidance, re-rolling duplicates.

**3. Mutation application.** For register and predicate categories, `sub_A5EC40` iterates over selected instructions and calls `sub_A5E9E0`, which finds the last register operand, allocates a new register of the same class via `sub_91BF30`, and replaces the operand value. For offset bugs, the mutation adds +16 to the signed 24-bit offset field directly: `*operand = (sign_extend_24(*operand) + 16) & 0xFFFFFF | (*operand & 0xFF000000)`.

**4. Reporting.** Prints to stdout:

```text
Num forced bugs N
Created a bug at index I : kind K inst # ID [OFF] in operand OP correct val V replaced with W
```

### Fault Record Structure (40 bytes)

| Offset | Size | Field |
|---|---|---|
| +0 | 4 | Kind (1–10) |
| +8 | 8 | Pointer to Mercury instruction node |
| +16 | 4 | Operand index within instruction |
| +20 | 4 | Original operand value |
| +24 | 4 | Replacement operand value |
| +28 | 4 | Selection index (position in candidate list) |
| +32 | 4 | Instruction ID (from instruction+16) |

Records are stored in a dynamic array at `context[135]`.

### Function Map

| Address | Function | Role | Confidence |
|---|---|---|---|
| `0xA83AC0` | `sub_A83AC0` | bugspec.txt reader and injection coordinator | **CERTAIN** (string: `./bugspec.txt`) |
| `0xA83490` | `sub_A83490` | Random index selector with FNV-1a dedup | HIGH |
| `0xA5E9E0` | `sub_A5E9E0` | Register operand mutation (allocates new register) | HIGH |
| `0xA5EC40` | `sub_A5EC40` | Batch mutation applicator (iterates selected instructions) | HIGH |
| `0xA832D0` | `sub_A832D0` | Hash table resize for dedup tracking | MEDIUM |

### Significance

This is NVIDIA's internal compiler testing infrastructure for stochastic fault injection. It targets specific vulnerability surfaces in the register allocator and post-allocation pipeline: wrong-register assignments, address calculation errors, predicate propagation failures, rematerialization correctness, spill code integrity, and register-predicate conversion accuracy. The `time(0)`-seeded RNG produces different fault patterns on each run for the same bugspec.txt, enabling randomized stress testing of verification passes.

## Embedded C++ Name Demangler

PTXAS statically embeds an Itanium ABI C++ name demangler rather than linking `libc++abi` or `libstdc++`. The demangler is a self-contained 41-function cluster spanning `0x1CD8B00`--`0x1CE1E60` in `.text`, with a single external entry point. The core recursive-descent parser at `sub_1CDC780` (15,988 B native / 90 KB decomp, 3,432 lines) handles the full Itanium mangling grammar: nested names, template arguments, substitutions, function types, and special names.

### API and Integration

The public-facing function is `sub_1CE23F0`, whose signature matches `__cxa_demangle` exactly: it takes a mangled name string, an optional output buffer with length pointer, and a status pointer; it returns a `malloc`-allocated demangled string or `NULL` with a status code (`-1` = memory failure, `-3` = invalid arguments). The only caller of this function is the embedded terminate handler at `sub_1CD7850`, which prints the standard `"terminate called after throwing an instance of '...'"` diagnostic to stderr, demangling the exception type name before display.

### Why Embedded

PTXAS imports only `libc`, `libpthread`, `libm`, and `libgcc_s` (146 PLT stubs total). It has no dependency on any C++ runtime library. The only C++ ABI symbol in the PLT is `__cxa_atexit` (at `0x401989`), used to register the terminate handler. By embedding the demangler and terminate handler directly, NVIDIA avoids a runtime dependency on `libstdc++` or `libc++abi`, which would otherwise be required solely for exception type name display in fatal error messages. This is consistent with the binary's overall strategy of minimizing external dependencies.

### Function Map

| Address | Function | Size | Role | Confidence |
|---|---|---|---|---|
| `sub_1CDC780` | Demangler core (recursive-descent parser) | 15,988 B (native) / 90 KB (decomp) | Parses Itanium-mangled names via large switch dispatch | **HIGH** (size, structure, callgraph isolation) |
| `sub_1CE0600` | Recursive dispatch wrapper | 580 B | Re-enters the parser for nested name components (76 call sites from core) | **HIGH** (mutual recursion with `sub_1CDC780`) |
| `sub_1CE23F0` | `__cxa_demangle`-compatible API | 340 B | Public entry: mangled string in, demangled string out, `malloc`-allocated | **CERTAIN** (API shape, status codes, `free`/`memcpy`/`strlen` callees) |
| `sub_1CE1E60` | Parse entry point | ~200 B | Initializes parse state and invokes the core | **HIGH** (bridge between API and parser) |
| `sub_1CD7850` | Terminate handler (`__cxa_terminate`) | 280 B | Prints `"terminate called after throwing..."` to stderr | **CERTAIN** (string: `"terminate called after throwing an instance of '"`) |

## Version Update Procedure

All addresses, function counts, and structural offsets in this wiki are specific to ptxas v13.0.88 (build `cuda_13.0.r13.0/compiler.36424714_0`, 37,741,528 bytes). When a new CUDA toolkit ships a different ptxas binary, the wiki must be updated. This section documents the procedure.

### Version-Stable vs Version-Fragile Findings

Not everything changes between versions. Understanding what is stable dramatically reduces update effort.

**Version-stable** (survives across minor and most major releases unchanged):

| Category | Examples | Why stable |
|---|---|---|
| Algorithm logic | Copy propagation worklist walk, fatpoint pressure computation, MurmurHash3 constants | Algorithms are rarely rewritten between releases |
| Data structure layouts | Pool allocator bins at +2128, Mercury instruction node at 112 bytes, 16-byte phase objects | Struct layouts change only when fields are added or reordered |
| Knob names | `MercuryUseActiveThreadCollectiveInsts`, `ScavInlineExpansion`, all 2,000+ ROT13 names | Knob names are API-like — changing them breaks internal test harnesses |
| ROT13 encoding | The ROT13 obfuscation layer itself, decoded by `codecs.decode(s, "rot_13")` | Obfuscation scheme has been consistent across observed versions |
| Phase count and ordering | 159 phases in the OCG pipeline, ordered by the PhaseManager vtable table | Phase count may grow but existing phases retain their relative order |
| Pipeline stage names | `Parse-time`, `DAGgen-time`, `OCG-time`, `ELF-time`, `DebugInfo-time` | Stage names are embedded in format strings unlikely to change |
| Subsystem names | OCG, Mercury, Ori, Scav | Internal codenames are stable across releases |
| Encoding handler template | 6-step pattern: opcode ID, `movaps` format descriptor, register class map, operand registration, finalize, bitfield extract | Template structure is generated from a stable code generator |
| Error message text | `"SM does not support LDCU"`, `"Invalid knob identifier"` | Diagnostic strings are rarely reworded |

**Version-fragile** (changes with every recompilation):

| Category | Examples | Why fragile |
|---|---|---|
| Function addresses | Every `sub_XXXXXX` reference, vtable addresses like `off_22BD5C8` | ASLR-style shifts from any code or data size change |
| Address ranges | Sweep boundaries `0x400000`--`0x4D5000`, subsystem regions | Functions move when preceding code grows or shrinks |
| Function sizes | `sub_446240` at 12,345 bytes | Inlining decisions change, optimizer improvements add/remove code |
| Caller/callee counts | `sub_424070` at 3,809 callers | New call sites added, old ones removed |
| Struct offsets | `context[133]`, `context+1584` | New fields inserted into context structs |
| `.rodata` addresses | String locations like `0x202D4D8`, encoding table addresses | Data layout shifts with code changes |
| Call graph edge counts | 548,693 edges | New functions and call sites |
| Total function count | 40,185 | New SM targets add encoding handlers |

### Identifying Function Address Changes

When loading a new ptxas version into IDA:

1. **Extract the same 8 JSON artifacts** using `analyze_ptxas.py` (or equivalent). The critical artifacts for diffing are `ptxas_functions.json` (address, size, callee list) and `ptxas_strings.json` (string content, xref locations).

2. **Match functions by invariant properties.** Functions cannot be matched by address alone. Use these matching criteria in priority order:

   - **String anchors.** Functions containing unique string references (e.g., the function referencing `"Please use -knob DUMPIR=AllocateRegisters"`) can be matched across versions by searching for the same string in the new binary. This is the highest-confidence matching method.
   - **Size + callee signature.** For functions without string anchors, match by (approximate size, sorted callee list). A function of ~2,100 bytes calling the pool allocator, OOM handler, and hash map insert is almost certainly the same function even if its address shifted by megabytes.
   - **Callgraph position.** Functions identified by their caller/callee topology: the phase factory is the function called from the PhaseManager constructor with 159+ case targets. The diagnostic emitter is the function with 2,000+ callers that calls `vfprintf`.
   - **Vtable slot position.** Phase `execute()` methods are at vtable slot 0. If the vtable table address changes but still contains 159 entries, the slot positions identify each phase.
   - **Template fingerprinting.** Encoding handlers matching the 6-step template (bitfield insert via the highest-caller utility, `movaps` from `.rodata`, operand registrars, finalize call) are encoding handlers in any version.

3. **Diff the function lists.** Produce a mapping `{old_addr -> new_addr}` for all matched functions. Functions present in the new binary but absent in the old are new (likely new SM target support). Functions absent in the new binary are removed (dropped legacy SM support) or merged.

### Updating Sweep Reports

The 30-region sweep reports in `ptxas/raw/` are version-locked historical records — they document the analysis of v13.0.88 and should not be overwritten. For a new version:

1. **Re-run the sweep** with new address ranges derived from the new binary's function list. The region partitioning should follow the same subsystem-aligned strategy: infrastructure first, then PhaseManager, then high-complexity subsystems, then batch encoding handlers.

2. **Name new reports** with a version suffix: `p2.01-sweep-v13.1-0xNNN-0xMMM.txt` (or whatever scheme distinguishes the version).

3. **Cross-reference against old reports.** For each region, note which functions moved, which are new, and which disappeared. The old sweep reports provide the expected function identities; the new sweep validates whether those identities still hold at the new addresses.

### Pages Most Sensitive to Version Changes

These wiki pages require immediate updates when the binary changes:

| Page | Sensitivity | What changes |
|---|---|---|
| `function-map.md` | **Critical** | Every address in every table row. The entire page is address-indexed. |
| `binary-layout.md` | **Critical** | Section addresses, subsystem boundaries, address-range diagram. |
| `VERSIONS.md` | **Critical** | Binary size, build string, function count, version number. |
| `pipeline/overview.md` | High | Phase factory address, PhaseManager constructor address, vtable table address. |
| `scheduling/algorithm.md` | High | Scheduler function addresses, priority function addresses. |
| `regalloc/algorithm.md` | High | Allocator function addresses, fatpoint computation address. |
| `codegen/encoding.md` | High | Encoding handler address ranges, format descriptor addresses. |
| `config/knobs.md` | Medium | Knob constructor addresses (content of knob names is stable). |
| `ir/instructions.md` | Medium | Opcode numbers may shift if new instructions are added. |
| `targets/index.md` | Medium | New SM targets may appear, changing validation table sizes. |
| `methodology.md` | Low | The methodology itself is version-stable; only the "Scope and Scale" table needs updating. |

### Recommended Update Workflow

The update follows a five-step sequence. Steps 1-2 are mechanical; steps 3-5 require analyst judgment.

**Step 1: Extract new IDA artifacts.**

Load the new ptxas binary into IDA Pro 8.x. Run `analyze_ptxas.py` to produce the 8 JSON artifacts and per-function decompiled `.c` files. Store them in a version-specific directory (e.g., `ptxas/ida-v13.1/` or alongside the existing artifacts with clear version labeling).

**Step 2: Diff against the old artifacts.**

Write or use a diff script that:
- Compares `ptxas_functions.json` (old vs new) by matching on string anchors, size+callee signature, and callgraph position.
- Produces a `{old_addr -> new_addr}` mapping for matched functions.
- Lists unmatched functions in both directions (new functions, removed functions).
- Compares `ptxas_strings.json` to detect new strings, removed strings, and strings whose xref functions changed.
- Reports total function count delta, binary size delta, and new section addresses.

**Step 3: Update address-sensitive pages.**

Using the address mapping from Step 2:
- Update every `sub_XXXXXX` reference in `function-map.md`, `binary-layout.md`, and all pages listed in the sensitivity table above.
- Update the "Scope and Scale" table in `methodology.md` with new function counts, string counts, binary size, and build string.
- Update `VERSIONS.md` with the new binary metadata.
- For pages with address ranges (sweep boundaries, subsystem regions), recompute the ranges from the new function list.

**Step 4: Verify key struct layouts.**

Struct offset changes are the most dangerous kind of version drift because they silently invalidate decompiled code analysis. For each documented struct:
- Re-decompile the struct's primary accessor function (e.g., `sub_424070` for the pool allocator, `sub_4280C0` for the TLS context).
- Compare field offsets against the documented layout.
- If offsets shifted, update the struct documentation and propagate the change to all pages that reference those offsets.

Priority structs to verify: pool allocator (free-list bins at +2128, mutex at +7128), TLS context (280 bytes), Mercury instruction node (112 bytes), scheduler context (~1000 bytes), allocator state (1590+ bytes), phase objects (16 bytes).

**Step 5: Validate phase pipeline.**

- Re-extract the phase vtable table (find the new address of the 159-entry pointer array in `.data.rel.ro`).
- Verify all 159 phases are present and in the expected order.
- Check for new phases (count > 159) or removed phases (count < 159).
- Re-run `ptxas --fdevice-time-trace` on a test kernel and cross-reference the phase names in the trace output against the wiki's phase list.

### Raw Data Locations

All raw analysis artifacts for the current version (v13.0.88) live in the repository under `ptxas/`:

| Directory | Contents |
|---|---|
| `ptxas/raw/` | 40 sweep reports (`p1.01`--`p1.30` plus sub-region splits), per-task investigation reports (`P0_*`, `P1_*`, `P2_*`, etc.) |
| `ptxas/decompiled/` | Per-function Hex-Rays decompiled C files (`sub_XXXXXX.c`, named functions like `ctor_003_0x4095d0.c`) |
| `ptxas/disasm/` | Per-function disassembly files |
| `ptxas/graphs/` | Per-function control flow graph JSON files (80,078 files) |
| `ptxas/` (root) | The 8 JSON artifacts (`ptxas_functions.json`, `ptxas_strings.json`, `ptxas_callgraph.json`, `ptxas_xrefs.json`, `ptxas_comments.json`, `ptxas_names.json`, `ptxas_imports.json`, `ptxas_segments.json`), the IDA database (`ptxas.i64`), the extraction script (`analyze_ptxas.py`), and the binary itself (`ptxas`) |
| `ptxas/wiki/src/` | The wiki source pages (this document and all others) |

When updating to a new version, preserve the existing artifacts for v13.0.88 (rename or move to a versioned subdirectory) and store new artifacts alongside them. The sweep reports in `ptxas/raw/` are historical records and should never be overwritten.

## Limitations and Known Gaps

- **No dynamic validation of optimization correctness.** All findings are from static analysis. The identified phase algorithms have not been tested against runtime inputs to verify they produce correct output for all corner cases.

- **39.6% of functions are vtable-dispatched.** Functions with zero static callers can only be reached by finding the vtable or function pointer table that references them. Some vtables in deep `.rodata` may have been missed, leaving some functions orphaned.

- **No upstream reference for any code.** Unlike cicc (LLVM fork) or nvcc (EDG frontend), ptxas has no open-source analog. Every identification is from first principles. This limits confidence for functions where string evidence is absent and structural analysis is the only basis.

- **Template-generated code is indistinguishable.** The ~4,000 SASS encoding handlers are generated from internal templates. Without the template source, mapping individual handlers to specific opcodes requires tracing the dispatch table entries, which has only been done for select handlers.

- **Mega-functions are partially opaque.** The four functions exceeding 200 KB (`sub_169B190` at 280 KB, `sub_143C440` at 233 KB, `sub_198BCD0` at 239 KB, `sub_18A2CA0` at 231 KB) could not be decompiled by Hex-Rays. Their behavior is understood from their callee lists (13,000–15,870 callees each) and their position in the pipeline, but the internal dispatch logic is known only at the disassembly level.

- **ROT13 decoding is necessary but not sufficient.** Decoding the 2,000+ knob names reveals the *existence* of tuning parameters but not their *semantics*. A knob named `MercuryPresumeXblockWaitBeneficial` can be decoded from ROT13, but understanding what "xblock wait beneficial" means requires analyzing the code paths that read the knob.

- **Version-specific addresses.** All addresses in this wiki apply to ptxas v13.0.88 (build `cuda_13.0.r13.0/compiler.36424714_0`). Other CUDA toolkit versions will have different addresses, different function counts, and potentially different phase orderings. However, the analysis methodology (string-driven, vtable-driven, callgraph propagation) applies to any version.

- **Indirect calls are undercounted.** The 548,693-edge call graph captures only direct `call` instructions resolved by IDA. Virtual calls through vtable pointers, function pointer callbacks, and computed jumps are not fully captured. The true call graph is significantly denser than what is recorded.

## Corrections Log

This section documents every factual error discovered and corrected during the wiki improvement pass. Each entry records the error, the correction, affected pages, and the agent task that performed the fix. The full detail for each correction is in `ptxas/raw/P5_11_corrections_log_report.txt`.

### Summary

| Metric | Count |
|---|---|
| Distinct factual errors corrected | 22 |
| Wiki pages with at least one fix | 30+ |
| Agent tasks that discovered errors | 15 |
| Agent tasks that propagated fixes | 5 |

### Corrections by Severity

#### Systematic errors (affected 5+ pages each)

| # | Error | Correction | Pages | Agent |
|---|---|---|---|---|
| 01 | Opcode numbering: wiki assumed two numbering systems; "Selected Opcode Values" table had wrong SASS mnemonic labels (e.g., 93=CALL, 95=EXIT, 97=MOV, 130=BAR) | One numbering system: ROT13 name table index IS the instruction opcode. Correct labels: 93=OUT_FINAL, 95=STS, 97=STG, 130=HSET2 | 15 pages (ir/instructions, ir/cfg, passes/predication, passes/sync-barriers, passes/liveness, passes/general-optimize, passes/rematerialization, passes/copy-prop-cse, passes/strength-reduction, regalloc/abi, regalloc/spilling, intrinsics/sync-warp, codegen/isel, scheduling/latency-model, scheduling/algorithm) | P0-01, P4-02, P5-01 |
| 02 | Register class 6 = UB (Uniform Barrier); classes 2-6 all wrong | Class 6 = Tensor/Accumulator (MMA/WGMMA). Correct table: 2=R(alt), 3=UR, 4=UR(ext), 5=P/UP, 6=Tensor/Acc. Barrier regs use reg_type 9, outside the 7-class system | 7 pages (ir/registers, regalloc/overview, regalloc/algorithm, regalloc/spilling, passes/gmma-pipeline, intrinsics/tensor, ir/overview) | P0-02 |
| 03 | context+1584 had 5 conflicting names: code_object, sched_ctx, arch_backend, optimizer_state, function manager | Single object: SM-specific architecture backend ("sm_backend"), constructed per-compilation-unit in sub_662920 via SM version switch | 3 pages corrected (ir/data-structures, ir/overview, passes/copy-prop-cse); 14 pages acceptable as-is | P0-03 |

#### Identity misattributions

| # | Error | Correction | Pages | Agent |
|---|---|---|---|---|
| 06 | sub_83EF00 (29KB) listed as "Top-level unrolling driver" | sub_83EF00 is MainPeepholeOptimizer (opcode switch on 2, 134, 133, 214, 213, 210). Actual unrolling driver: sub_1390B30 via Phase 22 entry sub_1392E30 | passes/loop-passes.md | P1-04, P5-03 |
| 07 | sub_926A30 (22KB) listed as "Main pipelining engine (modulo scheduling)" | sub_926A30 is the operand-level latency annotator and interference weight builder, called by sub_92C0D0 per-instruction | passes/loop-passes.md | P1-06 |
| 08 | sub_7E7380 described as "full structural equivalence" (opcode, type, all operands, register class comparison) | sub_7E7380 is 30 lines / 150 bytes: narrow predicate-operand compatibility check (predicate bit parity + last operand 24-bit ID + penultimate 8-byte encoding). Full structural comparison done by the 21 callers | passes/copy-prop-cse.md, passes/general-optimize.md | P1-07, P5-06 |

#### Inverted semantics

| # | Error | Correction | Pages | Agent |
|---|---|---|---|---|
| 05 | isNoOp()=1 "means it executes unconditionally" | isNoOp()=1 means the dispatch loop SKIPS execute(). Code: `if (!phase->isNoOp()) { phase->execute(ctx); }` | passes/rematerialization.md | P0-05 |
| 09 | Hot-cold priority: "1 = cold, 0 = hot" | 1 = hot = higher priority, 0 = cold = lower priority. sub_A9CDE0 (hot detector) returns true -> bit 5 set -> higher priority | passes/hot-cold.md | P1-09, P5-06 |
| 10 | "Fatpoint" implied to be maximum-pressure point | Fatpoint scans for MINIMUM-cost slot. The name refers to the exhaustive (fat) scan evaluating all slots, not to picking the maximum | (verified correct across all pages — 0 fixes needed) | P1-10, P5-06 |

#### Wrong numeric values

| # | Error | Correction | Pages | Agent |
|---|---|---|---|---|
| 04 | context+1552 = "Legalization stage counter" with 3 values (3, 7, 12) | Pipeline progress counter with 22 values (0-21) spanning all pipeline categories | 4 pages (ir/data-structures, passes/late-legalization, passes/rematerialization, passes/copy-prop-cse) | P0-04 |
| 12 | 5 SASS opcode mnemonic typos: PSMTEST, LGDEPBAR, LGSTS, UBLKPC, UTMAREDG | CSMTEST, LDGDEPBAR, LDGSTS, UBLKCP, UTMREDG | reference/sass-opcodes.md | P2-11 |
| 14 | WGMMA case 9 = 0x1D5D (7517), case 10 = 0x1D5E (7518) | Case 9 = 0x1D5E (7518), case 10 = 0x1D60 (7520). Codes 0x1D5D/0x1D5F are advisory (non-serialization) warnings | passes/gmma-pipeline.md | P3-25 |
| 15 | ABI minimum: gen 5 (sm_60-sm_89) = 16 regs, gen 9+ = 24 regs | gen 3-4 (sm_35-sm_53) = 16, gen 5-9 (sm_60-sm_100) = 24. Binary: `(generation - 5) < 5 ? 24 : 16` | regalloc/abi.md | P3-26 |
| 17 | Unrolling rejection table at 0x21D1980 with 36-byte structures | Rejection string pointer array at 0x21D1EA0 with simple integer indices 7-24. The 0x21D1980 table is for peephole operand range lookups | passes/loop-passes.md | P1-04 |

#### Phantom data and scope errors

| # | Error | Correction | Pages | Agent |
|---|---|---|---|---|
| 11 | "Approximately 80 additional entries bulk-copied from unk_21C0E00" at SASS opcode indices 322-401, "totaling roughly 402 named opcodes" | Table has exactly 322 entries. The 1288-byte block at unk_21C0E00 is a 322-element identity map {0,1,...,321} copied to a different data structure (encoding category map at obj+0x2478) | reference/sass-opcodes.md | P2-11 |
| 13 | "139 explicitly named phases and 20 architecture-specific unnamed phases" | All 159 phases have names in the static table at off_22BD0C0. The original 139-phase inventory missed 20 phases (e.g., OriCopyProp, Vectorization, MercConverter, AllocateRegisters) | pipeline/overview.md, passes/index.md | P2-14, P4-03 |
| 16 | Warning 7018 (0x1B6A) attributed to SUSPEND/preserved scratch diagnostic | Code 0x1B6A does not exist in the binary. The actual code is 7011 (0x1B63) | regalloc/abi.md | P3-26 |
| 18 | Unrolling rejection codes listed as 0x80000001-0x80000018 | Those hex values appear in diagnostic message STRINGS, not as internal codes. Internal codes are simple integers 7-24 | passes/loop-passes.md | P1-04 |

#### Minor corrections

| # | Error | Correction | Pages | Agent |
|---|---|---|---|---|
| 19 | sub_80B700/sub_80BC80 listed as unrolling functions | Both are peephole optimizer functions (called through sub_83EF00), not unrolling | passes/loop-passes.md | P1-04 |
| 22 | general-optimize.md called sub_7E7380 "instruction_equivalent" / "structural instruction equivalence" in 6 locations | Renamed to "predicate_operand_compatible" / "predicate-operand compatibility check" | passes/general-optimize.md | P5-06 |

### Error Categories

| Category | Count | Examples |
|---|---|---|
| Identity misattribution | 5 | Wrong function-to-role mappings, wrong names for context fields |
| Wrong numeric values | 5 | Wrong opcode labels, wrong hex codes, wrong thresholds, wrong addresses |
| Inverted semantics | 3 | isNoOp skip-vs-execute, hot-cold bit polarity, fatpoint min-vs-max |
| Conflicting definitions | 3 | Register class contradictions across pages |
| Phantom data | 2 | Nonexistent SASS entries 322-401, nonexistent warning 7018 |
| Scope mischaracterization | 2 | context+1552 scope too narrow, phase naming scope too narrow |
| Encoding confusion | 2 | Hex-in-message-string vs internal code, wrong address for lookup table |

### Lessons Learned

1. **Behavioral inference is unreliable for opcode identity.** Observing that an opcode appears in branch contexts does not make it BRA. Always check the authoritative ROT13 name table.

2. **Cross-page consistency checks catch conflicting speculations.** Five pages independently naming the same field (context+1584) is a strong signal that at least four are wrong.

3. **Counts from partial analysis are systematically low.** The "3 values" for context+1552 and "139 named phases" both resulted from stopping the search too early. Exhaustive binary sweeps consistently reveal more entries.

4. **Function size is not a reliable identity signal.** sub_83EF00 (29KB) was large enough to seem like a major driver, but size alone does not distinguish a peephole optimizer from a loop unroller.

5. **ROT13 decoding + binary cross-validation is the gold standard.** Every correction that replaced speculative labels with ROT13-decoded names has held up under subsequent audits.
