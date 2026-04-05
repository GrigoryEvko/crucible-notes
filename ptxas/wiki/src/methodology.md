# Methodology

This page documents how the reverse engineering of ptxas v13.0.88 was performed. It serves as a transparency record so readers can assess the confidence of any claim in this wiki, and as a practical guide for anyone who wants to reproduce or extend the analysis.

## Scope and Scale

PTXAS is a 37.7 MB stripped x86-64 ELF binary with no debug symbols, no DWARF information, and no export table beyond 146 libc/libpthread PLT stubs. Unlike NVIDIA's cicc (which is an LLVM fork), ptxas contains no LLVM code, no EDG frontend, and no third-party optimizer components. Every pass, data structure, and encoding table is proprietary NVIDIA code. This makes the analysis harder than LLVM-derived binaries -- there is no upstream source to compare against.

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
| Functions with 0 static callers | 15,907 (39.6%) -- vtable-dispatched |
| Functions < 100 bytes | 11,532 (28.7%) |
| Functions > 10 KB | 86 (0.2%) |
| Named functions (not `sub_*`) | 319 (0.8%) |
| Internal codenames | OCG (Optimizing Code Generator), Mercury (SASS encoder), Ori (IR) |

The 304 functions that Hex-Rays could not decompile are predominantly PLT stubs, computed-jump trampolines in the Flex DFA scanner, and the four mega-dispatch functions exceeding 200 KB (too large for Hex-Rays to handle within default limits). None are in critical analysis paths -- the dispatch functions are understood from their callee lists and the PLT stubs from their import names.

## Why PTXAS Is Harder Than LLVM-Based Binaries

Reverse engineering cicc (NVIDIA's LLVM-based CUDA compiler) benefits from extensive prior art: LLVM's open-source codebase provides structural templates, pass names are registered in predictable patterns, and `cl::opt` strings directly name their global variables. PTXAS offers none of these advantages:

- **No upstream source.** Every identified function is identified from first principles -- string evidence, callgraph position, structural fingerprinting, or decompiled algorithm analysis. There is no reference implementation to compare against.
- **ROT13 obfuscation.** Internal names for tuning knobs and PTX opcode mnemonics are ROT13-encoded in the binary, requiring decoding before they become useful anchors.
- **Vtable-heavy architecture.** 39.6% of functions have zero static callers because they are dispatched through vtable pointers or function pointer tables. The call graph alone cannot reach them.
- **Template-generated code.** The SASS backend contains approximately 4,000 encoding handler functions generated from templates, each structurally near-identical. These dominate the function count but carry almost no unique identifying features.
- **No pass registration infrastructure.** LLVM passes register themselves via `PassInfo` objects with name strings. PTXAS phases are allocated by a factory switch (`sub_C60D30`) and their names are only visible through the `NamedPhases` registry and `AdvancedPhase*` timing strings -- far fewer anchors than LLVM's registration system.

## Toolchain

All analysis was performed with IDA Pro 8.x and the Hex-Rays x86-64 decompiler. The entire effort is static analysis of the binary at rest -- no dynamic analysis (debugging, tracing, instrumentation) was used for function identification. Runtime tools (`ptxas --stat`, `DUMPIR` knob, `--keep`) were used only for validation and cross-referencing.

| Tool | Purpose |
|---|---|
| IDA Pro 8.x | Disassembly, auto-analysis, cross-referencing, vtable reconstruction |
| Hex-Rays decompiler | Pseudocode generation for 39,881 recovered functions |
| IDA Python scripting | Complete database extraction: all 8 JSON artifact exports |
| Custom Python script | `analyze_ptxas.py`: batch string, function, graph, xref, and decompilation export |
| ptxas CLI | `--stat`, `--verbose`, `--compiler-stats`, `--fdevice-time-trace` for runtime validation |
| ptxas DUMPIR knob | `-knob DUMPIR=<phase>` to dump IR at specific pipeline points |
| ROT13 decoder | Standard `codecs.decode(s, "rot_13")` for 2,000+ obfuscated knob/opcode names |

## IDA Pro Setup and Initial Analysis

### Loading the Binary

PTXAS is a dynamically-linked ELF with 146 PLT imports but no symbol table beyond those imports. IDA auto-analysis settings:

1. **Processor**: Meta PC (x86-64)
2. **Analysis options**: default. IDA correctly identifies the Flex DFA scanner tables, Bison parser tables, and the `.ctors`/`.dtors` sections.
3. **Auto-analysis time**: approximately 8-10 minutes on a modern machine for the 37.7 MB binary.
4. **Compiler detection**: IDA identifies GCC as the compiler. The binary uses the Itanium C++ ABI (confirmed by the embedded C++ name demangler at `sub_1CDC780`, 93 KB).

### Post-Auto-Analysis Steps

After auto-analysis completes:

1. **Run string extraction.** IDA's auto-analysis finds 30,632 strings. All are exported via the `analyze_ptxas.py` IDA Python script.
2. **Force function creation.** Some address ranges, particularly the template-generated encoding handlers, are not automatically recognized as functions. IDA's "Create function" (P key) was applied selectively in the `0xD27000`--`0x1579000` range where encoding handler stubs are tightly packed.
3. **Batch decompile.** The IDA Python script iterates all 40,185 detected functions and calls `ida_hexrays.decompile()` on each, saving per-function `.c` files. 39,881 succeeded; 304 failed (PLT stubs, computed-jump trampolines, and 4 mega-functions exceeding decompiler limits).
4. **Export control flow graphs.** For each function, the script extracts the `FlowChart` (basic blocks, edges, per-instruction disassembly) as JSON. 80,078 graph files were produced.

### Type Recovery

PTXAS uses no C++ RTTI (no `typeid`, no `dynamic_cast` -- the binary has no `.data.rel.ro` RTTI structures). Type recovery relies on:

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
| `SZN` | `FMA` |
| `FRGC` | `SETP` |
| `ERGHEA` | `RETURN` |
| `RKVG` | `EXIT` |

These strings are used by the PTX parser to match instruction mnemonics. Each xref from one of these strings leads to a parser action or instruction validator function.

### Timing and Phase Name Strings

The compilation driver at `sub_446240` emits per-stage timing via format strings:

```
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

- `"Please use -knob DUMPIR=AllocateRegisters for debugging"` -- identifies the register allocator failure path at `sub_9714E0`
- `"SM does not support LDCU"` -- identifies SM capability checking in the instruction legalizer
- `"Invalid knob identifier"`, `"Invalid knob specified (%s)"` -- identifies the knob parsing infrastructure around `sub_79D070`
- `"fseek() error knobsfile %s"`, `"[knobs]"` -- identifies `ReadKnobsFile` at `sub_79D070`

### Source File Path

One recovered source path provides a structural anchor:

```
/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/common/utils/generic/impl/generic_knobs_impl.h
```

This string (at `0x202D4D8`, 66 xrefs) is referenced from assertion checks throughout the knobs infrastructure, confirming that the knob system is a shared utility component (`generic_knobs_impl.h`) used across NVIDIA's compiler drivers.

### Build and Version Strings

```
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

The SASS backend uses vtable dispatch for instruction encoding. Each SASS opcode variant has its own encoding handler function, registered in dispatch tables rather than called directly. This explains why 15,907 functions (39.6%) have zero static callers -- they are reached exclusively through indirect calls via function pointer tables.

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

The three peephole dispatch mega-functions (`sub_143C440` at 233 KB, `sub_18A2CA0` at 231 KB, `sub_198BCD0` at 239 KB) each serve a different SM generation family and call 1,100--1,336 pattern matcher functions. These dispatchers were identified by their enormous callee counts and their position in the pipeline after instruction encoding.

## Callgraph Analysis

The 548,693-edge call graph, exported from IDA, reveals the binary's module structure and function relationships. Several callgraph properties were systematically exploited.

### Hub Function Identification

Functions with extreme callee or caller counts serve as structural anchors:

**Top callees (hub functions -- "fan-out" nodes):**

| Address | Name | Size | Callees | Role |
|---|---|---|---|---|
| `sub_169B190` | ISel master dispatch | 280 KB | 15,870 | The single largest function in the binary. Dispatches to all ISel pattern matchers. |
| `sub_143C440` | SM120 peephole dispatch | 233 KB | 13,425 | SM120 (RTX 50-series) peephole optimization |
| `sub_198BCD0` | Peephole dispatch (variant 2) | 239 KB | 13,391 | Peephole optimization for another SM family |
| `sub_18A2CA0` | Peephole dispatch (variant 1) | 231 KB | 12,974 | Peephole optimization for another SM family |
| `sub_BA9D00` | Bitvector/CFG analysis | 204 KB | 11,335 | Dataflow framework core |

**Top callers (utility functions -- "fan-in" nodes):**

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

```
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
| **Call graph** | `ptxas_callgraph.json` | 64 MB | 548,693 | `{from, from_addr, to, to_addr}` -- one edge per call site |
| **Cross-references** | `ptxas_xrefs.json` | 978 MB | 7,427,044 | Complete xref database (code, data, string references) |
| **Comments** | `ptxas_comments.json` | 5.9 MB | 66,598 | `{addr, type, text}` -- IDA auto-comments and analyst annotations |
| **Names** | `ptxas_names.json` | 972 KB | 16,019 | `{addr, name}` -- IDA auto-generated and analyst-assigned names |
| **Imports** | `ptxas_imports.json` | 17 KB | 146 | `{module, name, addr, ordinal}` -- PLT import stubs |
| **Segments** | `ptxas_segments.json` | 3 KB | 24 | `{name, start, end, size, type, perm}` -- ELF segment map |

Total artifact storage: 1.14 GB (dominated by the 978 MB xref database).

### What Each Artifact Reveals

**Functions** (`ptxas_functions.json`): The master index. Every function's address, size, instruction count, caller list, and callee list. The caller/callee lists are the basis for callgraph analysis. The `is_thunk` flag identifies PLT stubs (exclude from analysis). The `is_library` flag identifies functions IDA tagged as library code (CRT startup, jemalloc-like allocator internals).

**Strings** (`ptxas_strings.json`): The primary identification tool. Each string's xref list shows which functions reference it. Searching for `"AdvancedPhase"` returns 15 strings, each xref pointing to a pipeline boundary in the PhaseManager. Searching for strings starting with `"Z"` (ROT13 "M" for "Mercury") returns the Mercury subsystem's knob names. The 2,035 hex-encoded default value strings (`"0k..."` / `"0x..."`) are paired 1:1 with knob name strings in the constructors.

**Call graph** (`ptxas_callgraph.json`): The structural backbone. Each edge records a direct call from one function to another. Indirect calls (vtable dispatch, function pointer callbacks) are not captured, which is the primary limitation -- the 15,907 zero-caller functions are almost all vtable-dispatched. The call graph is used for module boundary detection, propagation from known functions, and entry/exit point analysis.

**Cross-references** (`ptxas_xrefs.json`): The most comprehensive artifact. Contains all code-to-code, code-to-data, and data-to-data references detected by IDA. At 7.4 million entries, it is too large to load into memory on machines with less than 16 GB RAM. Used for deep analysis of specific functions: finding all references to a particular `.rodata` constant, tracing data flow through global variables, and identifying vtable consumers.

**Comments** (`ptxas_comments.json`): IDA's auto-generated comments (e.g., `"File format: \\x7FELF"`) plus analyst-added annotations. The auto-comments on function prologues identify calling conventions and stack frame layouts. Analyst comments record identification rationale for reviewed functions.

**Names** (`ptxas_names.json`): IDA's auto-generated names for data and code addresses. Of 16,019 entries, approximately 9,670 are auto-generated string reference names (`aLib64LdLinuxX8`, `aGnu`, etc.) and ~6,349 are analyst-assigned or IDA-recovered names (PLT stubs, constructors, etc.). These names appear in the callgraph edges as `from`/`to` identifiers.

**Imports** (`ptxas_imports.json`): The 146 PLT imports. Key imports include `pthread_*` (13 functions), `malloc`/`free`/`realloc`, `_setjmp`/`longjmp` (used by the error recovery system), `select`/`fcntl` (used by the GNU Make jobserver client), and `clock` (used by the timing infrastructure).

**Segments** (`ptxas_segments.json`): The 24 ELF segments/sections. Used to establish the address space layout and map code/data boundaries. The `.ctors` section (104 bytes, 12 entries) is particularly important -- it lists the static constructors that initialize the ROT13 tables and the knob registry.

## The 30-Region Sweep Approach

The primary analysis was conducted as a systematic address-range sweep of the entire `.text` section, divided into 30 contiguous regions. Each region was analyzed independently in a single session, producing a raw sweep report. The 40 report files (including sub-region splits) total 34,880 lines of working notes.

### Region Partitioning

The `.text` section (`0x403520`--`0x1CE2DE2`, 26.2 MB) was divided into approximately 870 KB regions. The partitioning was not arbitrary -- region boundaries were chosen to align with subsystem boundaries where possible, so that each sweep report covers a coherent functional area.

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

```
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

Each function entry records the address, size, decompiled line count, proposed identity, confidence level, evidence citations, and key code excerpts. The reports are raw working notes -- they contain false starts, corrections, and evolving hypotheses that were resolved as more context became available.

### Analysis Ordering

The sweep was not performed in address order. The analysis followed an information-maximizing sequence:

1. **p1.01** (infrastructure + CLI) first -- establishes the allocator, hash map, TLS, and diagnostic patterns that appear throughout the binary.
2. **p1.11** (PhaseManager) second -- identifies all 159 phases and their vtable entries, providing the skeleton of the optimization pipeline.
3. **p1.07** (register allocator) and **p1.06** (scheduler) third -- these are the highest-complexity subsystems with the richest string evidence.
4. **p1.12--p1.15** (SASS encoders) in batch -- once the encoding template was recognized, all encoder regions were swept rapidly with template matching.
5. **p1.30** (library layer) late -- identifies shared infrastructure (ELF emitter, demangler, thread pool) referenced by earlier regions.
6. Remaining regions filled in by decreasing information density.

## Cross-Referencing with PTXAS CLI

Several ptxas command-line features and internal mechanisms provide runtime validation of static analysis findings.

### `--stat` and `--verbose`

Running `ptxas --stat input.ptx` prints per-kernel resource usage (register count, shared memory, stack frame size). This output is generated by `sub_A3A7E0` (the IR statistics printer), which was identified from the format strings:

```
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

The DUMPIR output format reveals the IR structure: basic block headers, instruction opcodes, register names (R0--R255, UR0--UR63, P0--P7, UP0--UP7), and operand encodings. This runtime output was used to validate the IR format reconstructed from static analysis.

### `--keep` Flag

The `--keep` flag preserves intermediate files. While ptxas does not emit intermediate text files in the same way as nvcc, the `--keep` behavior in the overall CUDA compilation pipeline (nvcc -> cicc -> ptxas) allows inspecting the PTX input that reaches ptxas, confirming the PTX grammar and instruction format expectations.

## Confidence Levels

Every function identification in this wiki carries one of three confidence levels:

| Level | Meaning | Basis |
|---|---|---|
| **CERTAIN** | Identity is certain | Direct string evidence naming the function, or the function is a PLT import with a known name |
| **HIGH** | Strong identification (>90%) | Multiple corroborating indicators: string xrefs, callgraph position, structural fingerprint, decompiled algorithm match |
| **MEDIUM** | Probable identification (70--90%) | Single indicator (vtable position, size fingerprint, callgraph context) or inferred from surrounding identified functions |

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
   - `sub_446240` (real main -- called from `main`, contains timing format strings)
   - `sub_C60D30` (phase factory -- 159-case switch)
   - `sub_C62720` (PhaseManager constructor -- references phase vtable table)
   - `sub_79B240` (GetKnobIndex -- inline ROT13 decoding)
   - `sub_42FBA0` (diagnostic emitter -- 2,350 callers, severity dispatch)

6. **Sweep the address space.** Work through the `.text` section in regions of ~870 KB. For each region:
   - Count functions and decompiled file sizes
   - Identify string anchors (search for region-specific strings)
   - Classify functions by structural template (encoding handler, phase body, utility, etc.)
   - Propagate identities from known callers/callees
   - Record findings in the sweep report format

7. **Cross-reference with runtime.** Compile a simple CUDA kernel and run `ptxas --stat --verbose --compiler-stats` to observe runtime behavior. Use `-knob DUMPIR=<phase>` to dump IR at specific pipeline points. Compare the dumped IR format against the IR structure reconstructed from decompiled code.

### Dependencies

The extraction script (`analyze_ptxas.py`) requires IDA Pro 8.x with Hex-Rays decompiler and Python 3.x. No external Python packages are needed -- only the IDA Python API (`idautils`, `idc`, `idaapi`, `ida_bytes`, `ida_funcs`, `ida_segment`, `ida_nalt`, `ida_gdl`, `ida_hexrays`).

Post-export analysis requires only the Python 3.8+ standard library (`json`, `codecs`, `collections`).

## Debug Infrastructure: bugspec.txt

ptxas contains an internal fault injection framework that deliberately corrupts the Mercury IR to test compiler verification passes. The mechanism is entirely file-driven: if a file named `./bugspec.txt` exists in the current working directory when ptxas runs, the function `sub_A83AC0` reads it and injects controlled mutations into the post-register-allocation instruction stream. No CLI flag activates this -- file presence alone is sufficient. If the file is absent, a diagnostic is printed to stdout (`Cannot open file with bug specification`) and compilation proceeds normally.

### File Format

The file contains a single line of six integers:

```
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

Each injected fault record carries a kind code (1--10) mapped to a string table at `0x21F0500`:

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

- **Register bugs (field0):** Scans operands for type-tag 1 (register) with register class 6 (general) or 3 (predicate), excluding opcodes 41--44. Eligible instructions are collected into a candidate list.
- **Predicate bugs (field1):** Checks flag byte at instruction+73 for bit 0x10 (predicated). Eligible instructions are collected separately.
- **Offset/spill bugs (field2):** Calls `sub_A56DE0` / `sub_A56CE0` against the register allocator state (`context[133]`) to identify spill/refill instructions.
- **Remat bugs (field3):** Queries the rematerialization hash table (`context+21` via `sub_A54200`) for instructions with remat entries.
- **R2P/P2R bugs (field4):** Checks instruction opcode (offset +72) for values 268, 155, 267, 173 (the R2P and P2R conversion opcodes, with bit-masked variants).
- **Bit-spill bugs (field5):** Checks operand count > 2, flag bit 0x10 at offset +28, and calls `sub_A53DB0` / `sub_A53C40` / `sub_A56880` for bit-spill eligibility.

**2. Random selection.** Seeds the RNG with `time(0)` via `srand()`. For each active category, `sub_A83490` randomly selects N instruction indices from the candidate list, where N is the count from bugspec.txt. The selector uses FNV-1a hashing on instruction addresses for collision avoidance, re-rolling duplicates.

**3. Mutation application.** For register and predicate categories, `sub_A5EC40` iterates over selected instructions and calls `sub_A5E9E0`, which finds the last register operand, allocates a new register of the same class via `sub_91BF30`, and replaces the operand value. For offset bugs, the mutation adds +16 to the signed 24-bit offset field directly: `*operand = (sign_extend_24(*operand) + 16) & 0xFFFFFF | (*operand & 0xFF000000)`.

**4. Reporting.** Prints to stdout:

```
Num forced bugs N
Created a bug at index I : kind K inst # ID [OFF] in operand OP correct val V replaced with W
```

### Fault Record Structure (40 bytes)

| Offset | Size | Field |
|---|---|---|
| +0 | 4 | Kind (1--10) |
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

## Limitations and Known Gaps

- **No dynamic validation of optimization correctness.** All findings are from static analysis. The identified phase algorithms have not been tested against runtime inputs to verify they produce correct output for all corner cases.

- **39.6% of functions are vtable-dispatched.** Functions with zero static callers can only be reached by finding the vtable or function pointer table that references them. Some vtables in deep `.rodata` may have been missed, leaving some functions orphaned.

- **No upstream reference for any code.** Unlike cicc (LLVM fork) or nvcc (EDG frontend), ptxas has no open-source analog. Every identification is from first principles. This limits confidence for functions where string evidence is absent and structural analysis is the only basis.

- **Template-generated code is indistinguishable.** The ~4,000 SASS encoding handlers are generated from internal templates. Without the template source, mapping individual handlers to specific opcodes requires tracing the dispatch table entries, which has only been done for select handlers.

- **Mega-functions are partially opaque.** The four functions exceeding 200 KB (`sub_169B190` at 280 KB, `sub_143C440` at 233 KB, `sub_198BCD0` at 239 KB, `sub_18A2CA0` at 231 KB) could not be decompiled by Hex-Rays. Their behavior is understood from their callee lists (13,000--15,870 callees each) and their position in the pipeline, but the internal dispatch logic is known only at the disassembly level.

- **ROT13 decoding is necessary but not sufficient.** Decoding the 2,000+ knob names reveals the *existence* of tuning parameters but not their *semantics*. A knob named `MercuryPresumeXblockWaitBeneficial` can be decoded from ROT13, but understanding what "xblock wait beneficial" means requires analyzing the code paths that read the knob.

- **Version-specific addresses.** All addresses in this wiki apply to ptxas v13.0.88 (build `cuda_13.0.r13.0/compiler.36424714_0`). Other CUDA toolkit versions will have different addresses, different function counts, and potentially different phase orderings. However, the analysis methodology (string-driven, vtable-driven, callgraph propagation) applies to any version.

- **Indirect calls are undercounted.** The 548,693-edge call graph captures only direct `call` instructions resolved by IDA. Virtual calls through vtable pointers, function pointer callbacks, and computed jumps are not fully captured. The true call graph is significantly denser than what is recorded.
