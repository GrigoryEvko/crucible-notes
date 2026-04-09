# Methodology

> *All addresses in this page apply to nvlink v13.0.88 (CUDA 13.0). Other versions will differ.*

This page documents how the reverse engineering of nvlink v13.0.88 was performed. It serves as both a transparency record -- so readers can assess the confidence of any claim in this wiki -- and as a practical guide for anyone who wants to reproduce or extend the analysis.

## Binary Provenance

The analyzed binary is the CUDA device linker shipped with CUDA Toolkit 13.0. It is obtained from a standard toolkit installation at `<cuda>/bin/nvlink`.

| Property | Value |
|---|---|
| Tool name | `NVIDIA (R) Cuda linker` |
| Version string | `Cuda compilation tools, release 13.0, V13.0.88` |
| Build string | `Build cuda_13.0.r13.0/compiler.36424714_0` |
| Copyright | `Copyright (c) 2005-2025 NVIDIA Corporation` |
| File format | ELF 64-bit LSB executable, x86-64 |
| Binary size | ~37 MB |
| Linking | Dynamically linked (libc, libpthread, libm, libgcc_s, libdl, librt) |
| Strip status | Fully stripped -- no debug symbols, no DWARF, no `.symtab`, no RTTI |
| Compiler | GCC (detected by IDA from prologue patterns and CRT initialization) |
| C++ ABI | Itanium (confirmed by the embedded C++ name demangler) |

The binary is a hybrid linker-compiler: roughly half of its 37 MB implements CUDA device linking (symbol resolution, section merging, relocation, ELF emission), and the other half is a full embedded GPU compiler backend (a statically linked copy of ptxas) covering instruction selection, register allocation, scheduling, and SASS encoding for 22 SM architectures. The two halves share infrastructure (memory arenas, hash tables, error reporting, thread pool) but operate on different data structures.

## Scope and Scale

| Metric | Value |
|---|---|
| Binary size | ~37 MB |
| Total functions detected | 40,532 |
| Functions decompiled (Hex-Rays) | 40,366 (99.6%) |
| Decompilation failures | 168 (0.4%) |
| Strings extracted | 31,237 |
| Call graph edges | 552,453 |
| Cross-references | 7,513,413 |
| IDA comments (auto-generated) | 82,893 |
| Named locations | 16,650 |
| Imported functions (PLT) | 156 |
| ELF segments | 25 |
| .rodata size | 7,543,312 bytes (~7.2 MB) |
| .text coverage (sweep) | 0x400000 -- 0x1D32172 (25.2 MB) |
| Disassembly files exported | 40,376 |
| Control flow graphs exported | 80,752 (JSON + DOT, two files per function) |
| Raw sweep report lines | 60,899 |
| Wiki work report lines | 14,509 |
| Quality improvement report lines | 1,300 |
| Wiki pages | 93 |
| Raw reports (total) | 119 |

The 168 functions that Hex-Rays could not decompile fall into three categories: very small CRT thunks and computed-jump trampolines, hand-written assembly stubs in the startup code, and approximately 5 "mega-functions" exceeding the decompiler's internal limits (functions with 50,000+ lines of pseudocode that cause Hex-Rays to time out or exhaust memory). The mega-functions include the main entry point (57,970 bytes), several ISA encoder dispatch tables, and the largest instruction selection hubs. For these, raw disassembly and basic-block CFGs were used instead.

## Toolchain

All analysis was performed with IDA Pro 9.x and the Hex-Rays x86-64 decompiler. No dynamic analysis (debugging, tracing, instrumentation) was used -- the entire effort is static analysis of the binary at rest.

| Tool | Purpose |
|---|---|
| IDA Pro 9.x | Disassembly, auto-analysis, cross-referencing, type reconstruction |
| Hex-Rays decompiler | Pseudocode generation for 40,366 recovered functions |
| `analyze_nvlink.py` (IDA Python) | Complete database extraction: all JSON artifacts, per-function decompilation, disassembly, and CFGs |
| Claude Opus 4.6 (AI agents) | Systematic sweep analysis, pattern identification, wiki page generation |

No runtime instrumentation, no `strace`/`ltrace`, no `gdb` breakpoints. Every finding derives from static analysis of the binary's code and data sections.

### IDA Pro Setup

nvlink is a dynamically-linked ELF with 156 PLT imports but no symbol table beyond those imports. IDA auto-analysis settings:

1. **Processor**: Meta PC (x86-64).
2. **Analysis options**: default. IDA correctly identifies the `.ctors`/`.dtors` sections, PLT stubs, and CRT initialization code.
3. **Auto-analysis time**: approximately 10-20 minutes on a modern machine for the ~37 MB binary.
4. **Compiler detection**: IDA identifies GCC as the compiler. The binary uses the Itanium C++ ABI (confirmed by the embedded C++ name demangler).

After auto-analysis completes, the `analyze_nvlink.py` extraction script is run to export all artifacts in bulk. No manual function creation or type annotation is required before the export -- the bulk extraction captures the IDA database in its auto-analyzed state.

## Extraction Script: `analyze_nvlink.py`

The IDA Python script `analyze_nvlink.py` drives a complete, unattended extraction of the IDA database into structured files. It runs inside IDA's headless mode (`idat64 -A -S"analyze_nvlink.py" nvlink.i64`) and waits for IDA's auto-analysis to complete before beginning extraction.

The script exports twelve data categories:

| Export | Output file | Format | Records |
|---|---|---|---|
| Strings | `nvlink_strings.json` | JSON | 31,237 strings with address, value, type, and per-string xref list (source address + containing function for each reference) |
| Functions | `nvlink_functions.json` | JSON | 40,532 function records with start/end address, name, byte size, instruction count, library/thunk flags, caller list, and callee list |
| Imports | `nvlink_imports.json` | JSON | 156 imported symbols with module name, symbol name, address, and ordinal |
| Segments | `nvlink_segments.json` | JSON | 25 ELF segments with name, start/end address, size, type, and permission flags |
| Cross-references | `nvlink_xrefs.json` | JSON | 7,513,413 xref records with source address, source function, target address, target function, and xref type code |
| Comments | `nvlink_comments.json` | JSON | 82,893 regular and repeatable comments with address and text |
| Named locations | `nvlink_names.json` | JSON | 16,650 address-to-name mappings (IDA auto-names, user names, import names) |
| Read-only data | `nvlink_rodata.bin` | Binary | 7,543,312 bytes raw dump of the `.rodata` segment |
| Call graph | `nvlink_callgraph.json` + `.dot` | JSON + DOT | 552,453 directed call edges (caller name/address to callee name/address) with DOT graph for visualization |
| Disassembly | `disasm/{name}_{addr}.asm` | Text | One file per function: header comment block, then `address: hexbytes  disasm` lines |
| Decompilation | `decompiled/{name}_{addr}.c` | Text | One file per function: header comment block, then Hex-Rays pseudocode |
| Control flow graphs | `graphs/{name}_{addr}.json` + `.dot` | JSON + DOT | One graph per function: basic blocks (with per-block instruction lists) and edges |

The script processes functions sequentially and logs progress to stdout every 50-100 functions. Total extraction time is approximately 4-8 hours depending on system performance, dominated by the Hex-Rays decompilation pass (40,532 functions) and the xref enumeration pass (7.5M records).

### Key Design Decisions

**Per-function xrefs in string records.** Each string entry includes a list of every code location that references it, with the containing function name resolved. This allows direct lookup of "which functions reference this error message" without a separate join against the xref database.

**Separate caller/callee lists per function.** The function export embeds both inbound (caller) and outbound (callee) call edges directly in each function record, enabling local analysis without loading the full call graph.

**Raw binary dump of .rodata.** The `.rodata` section contains string literals, vtable pointers, jump tables, constant arrays, and SASS instruction encoding tables. The raw dump enables offline analysis of data structures that IDA does not fully parse (particularly the large opcode encoding tables used by the embedded ptxas).

**DOT format for graphs.** Both the global call graph and per-function CFGs are exported in Graphviz DOT format alongside JSON, supporting visual inspection with `dot`, `xdot`, or `sfdp`.

## Analysis Artifacts Inventory

The complete analysis data is organized as follows:

```
nvlink/
  analyze_nvlink.py             # IDA Python extraction script
  .gitignore                    # Excludes binary, IDA DB, and large JSON from git
  raw/                          # Raw analysis reports (119 files, 76,708 lines total)
    p1.01-sweep-*.txt           #   20 Phase 1 address-range sweep reports (60,899 lines)
    W001_*_report.txt           #   91 Phase 2 wiki writing work reports (14,509 lines)
    P048_*_report.txt           #   8 Phase 2.5 quality improvement reports (1,300 lines)
  wiki/                         # mdBook wiki (92 pages)
    src/                        #   Markdown source files
      index.md                  #   Landing page and reading guide
      methodology.md            #   This page
      binary-layout.md          #   Address-space memory map
      function-map.md           #   Key function address table
      versions.md               #   Binary metadata and version info
      SUMMARY.md                #   mdBook table of contents
      pipeline/                 #   11 pages: entry, CLI, mode dispatch, input loop, ...
      input/                    #   8 pages: file type detection, ELF parsing, cubin, ...
      linker/                   #   9 pages: symbol resolution, section merging, ...
      lto/                      #   6 pages: LTO overview, libnvvm, split compile, ...
      ptxas/                    #   8 pages: embedded compiler overview, arch dispatch, ...
      mercury/                  #   6 pages: SASS encoder, capmerc format, fnlzr, ...
      targets/                  #   8 pages: per-SM architecture profiles
      elf/                      #   7 pages: device ELF format, NVIDIA sections, ...
      debug/                    #   5 pages: DWARF processing, line tables, ...
      infra/                    #   6 pages: memory arenas, error reporting, ...
      structs/                  #   5 pages: linker context, ELF writer, ...
      config/                   #   3 pages: CLI flags, env vars, ptxas options
      reference/                #   5 pages: R_CUDA catalog, section catalog, ...
    book.toml                   #   mdBook configuration
  decompiled/                   # (gitignored) Hex-Rays output: 40,210 per-function C files
  disasm/                       # Per-function disassembly: 40,376 ASM files
  graphs/                       # Per-function CFGs: 80,752 files (JSON + DOT pairs)
  nvlink_strings.json           # (gitignored) 31,237 string records
  nvlink_functions.json         # (gitignored) 40,532 function records
  nvlink_callgraph.json         # (gitignored) 552,453 call edges
  nvlink_callgraph.dot          # (gitignored) DOT format call graph
  nvlink_xrefs.json             # (gitignored) 7,513,413 cross-reference records
  nvlink_comments.json          # (gitignored) 82,893 IDA comment records
  nvlink_names.json             # (gitignored) 16,650 named location records
  nvlink_imports.json           # (gitignored) 156 PLT import records
  nvlink_segments.json          # (gitignored) 25 ELF segment records
  nvlink_rodata.bin             # (gitignored) 7.2 MB raw .rodata dump
  nvlink                        # (gitignored) The binary itself
  nvlink.i64                    # (gitignored) IDA Pro database
```

The large JSON artifacts and the binary itself are excluded from git via `.gitignore`. The wiki source, raw reports, extraction script, disassembly files, and control flow graphs are tracked in version control.

### Artifact Sizes

| Artifact | Approximate Size | Records |
|---|---|---|
| `nvlink_xrefs.json` | ~900 MB | 7,513,413 |
| `nvlink_functions.json` | ~90 MB | 40,532 |
| `nvlink_callgraph.json` | ~60 MB | 552,453 |
| `nvlink_strings.json` | ~5 MB | 31,237 |
| `nvlink_comments.json` | ~6 MB | 82,893 |
| `nvlink_names.json` | ~1 MB | 16,650 |
| `nvlink_imports.json` | ~15 KB | 156 |
| `nvlink_segments.json` | ~3 KB | 25 |
| `nvlink_rodata.bin` | 7.2 MB | (raw binary) |
| `decompiled/` (total) | ~2.5 GB | 40,210 files |
| `disasm/` (total) | ~1.8 GB | 40,376 files |
| `graphs/` (total) | ~3.2 GB | 80,752 files |

Total storage for all artifacts (including decompiled, disasm, and graphs): approximately 8-9 GB. The xref database alone is nearly 1 GB and requires 16+ GB RAM to load into memory for analysis.

## Analysis Process

### Phase 1: Systematic Address-Range Sweeps

The 25.2 MB `.text` section was divided into 20 contiguous address ranges, each assigned to an independent AI analysis agent (Claude Opus 4.6). Each agent received:

- The address range boundaries
- All decompiled functions within that range (Hex-Rays pseudocode)
- All strings referenced by functions in the range
- The caller/callee relationships for each function
- Cross-references from `.rodata` into the range

Each agent produced a structured sweep report covering every function above a minimum size threshold (typically 2-3 KB). The 20 sweep regions and their coverage:

| Sweep | Address Range | Size | Primary Content |
|---|---|---|---|
| p1.01 | 0x400000 -- 0x470000 | 448 KB | Entry point (main), PLT/imports, option parsing, memory arenas |
| p1.02 | 0x470000 -- 0x530000 | 768 KB | ELF structure management, symbol/section infrastructure |
| p1.03 | 0x530000 -- 0x620000 | 960 KB | Merge engine, shared memory layout, callgraph DCE |
| p1.04 | 0x620000 -- 0x7A0000 | 1,536 KB | Relocation engine, data overlap optimization, LTO pipeline |
| p1.05 | 0x7A0000 -- 0x920000 | 1,536 KB | PTX parser, peephole optimizer, instruction-level transforms |
| p1.06 | 0x920000 -- 0xA70000 | 1,344 KB | Register allocator, spill code generation |
| p1.07 | 0xA70000 -- 0xB80000 | 1,088 KB | Instruction scheduler, dependency analysis |
| p1.08 | 0xB80000 -- 0xCA0000 | 1,152 KB | IR node infrastructure, SSA construction |
| p1.09 | 0xCA0000 -- 0xDA0000 | 1,024 KB | Architecture dispatch, vtable-driven ISA abstraction |
| p1.10 | 0xDA0000 -- 0xF16000 | 1,496 KB | SASS instruction encoder/decoder (Blackwell ISA) |
| p1.11 | 0xF16000 -- 0x100C000 | 984 KB | SASS encoder/decoder continued (Hopper/Ada ISA) |
| p1.12 | 0x100C000 -- 0x11EA000 | 1,912 KB | SASS encoder/decoder continued (Ampere/Turing ISA) |
| p1.13 | 0x11EA000 -- 0x12B0000 | 792 KB | Opcode dispatch tables, instruction format tables |
| p1.14 | 0x12B0000 -- 0x1430000 | 1,536 KB | Instruction selection hub (SelectionDAG-like) |
| p1.15 | 0x1430000 -- 0x15C0000 | 1,600 KB | Instruction selection continued, legalization |
| p1.16 | 0x15C0000 -- 0x16E0000 | 1,152 KB | Machine IR passes, peephole, scheduling |
| p1.17 | 0x16E0000 -- 0x1850000 | 1,472 KB | NV-specific compiler passes, Mercury post-link |
| p1.18 | 0x1850000 -- 0x1A00000 | 1,728 KB | NV-info propagation, constant bank management |
| p1.19 | 0x1A00000 -- 0x1B60000 | 1,408 KB | ELF output serialization, compression (LZ4) |
| p1.20 | 0x1B60000 -- 0x1D32172 | 1,864 KB | ISel lowering, ABI/calling convention, ELF builder, C++ demangler, DWARF generation |

The 20 sweep reports total 60,899 lines of structured analysis notes. Each report follows a consistent format: executive summary identifying the region's primary subsystem, a subsystem map listing functional groupings, and per-function entries with identity, confidence level, evidence, call relationships, and key strings.

### Sweep Report Structure

Each sweep report follows a standardized template:

```
================================================================================
NVLINK v13.0.88 REVERSE ENGINEERING SWEEP
Region: 0xAAAA000 - 0xBBBB000 (~NNN KB)
Primary content: [subsystem description]
================================================================================

Total files in range: NNN
Functions >2KB analyzed: ~NNN
Date: YYYY-MM-DD
Analyst: Automated RE sweep (Opus 4.6)

================================================================================
SUBSYSTEM MAP (functions organized by role)
================================================================================

A. [SUBSYSTEM NAME]
B. [SUBSYSTEM NAME]
...

================================================================================
FUNCTION-BY-FUNCTION ANALYSIS
================================================================================

### 0xAAAAAA -- sub_AAAAAA (NNNN bytes)
**Identity**: [Function identification]
**Confidence**: HIGH / MEDIUM / LOW
**Evidence**:
  - String: "..."
  - Calls: sub_BBBBBB (known function)
  - Structure: [pattern match description]
```

Each function entry records the address, size, proposed identity, confidence level, evidence citations, call relationships, and key observations. The reports are raw working notes -- they contain preliminary hypotheses, corrections, and evolving understanding that was refined as more context became available.

### Phase 2: Targeted Wiki Page Writing

The Phase 1 sweep reports serve as the raw evidence base. Phase 2 synthesizes the sweep findings into the structured wiki pages organized by subsystem (pipeline stages, input processing, linker core, LTO, embedded ptxas, Mercury, targets, ELF format, debug info, infrastructure, data structures, configuration, reference tables).

Each wiki page is written by an agent that receives:

- The relevant sweep report sections
- The JSON databases (strings, callgraph, names, xrefs) for cross-referencing
- The decompiled pseudocode of key functions
- Cross-references to the open-source kernel module (`firmware/open-gpu-kernel-modules/`) for NVLink protocol context and structure definitions

Phase 2 produced 91 wiki work reports (W001 through W092, `W089` skipped) totaling 14,509 lines, plus the 93 wiki pages themselves.

### Phase 2.5: Quality Improvement

After the initial wiki page drafts were complete, a quality improvement pass audited every page for:

- **Confidence markers**: Every function identification carries a confidence tag (HIGH/MEDIUM/LOW).
- **Cross-references**: Internal wiki links verified and fixed across all pages.
- **Address verification**: Spot-checked 50 key function addresses against the decompiled pseudocode files.
- **Speculation removal**: Audited all instances of "likely", "probably", "presumably" and either strengthened the evidence or removed the claim.
- **Table formatting**: Standardized column names and formatting across all 92 pages.

Phase 2.5 produced 8 quality improvement reports (P048, P052, P055-P060) totaling 1,300 lines.

## Function Identification Strategies

Identifying functions in a stripped binary requires multiple complementary strategies. They are listed below in order of reliability.

### String Cross-References (Highest Confidence)

nvlink is a string-rich binary. Error messages, diagnostic strings, pass names, phase names, and format validation messages are compiled into `.rodata`. A string like `"merge_elf failed"` appears at exactly one `.rodata` address, and IDA's xref from that string leads directly to the function that references it. This is the most reliable identification technique.

Specific high-value string patterns in nvlink:

- **Error messages with subsystem context**: `"cubin not an elf?"`, `"fatbin wrong format?"`, `"should never see bc files"`, `"error in LTO callback"` -- each anchors a specific code path in the input processing or LTO pipeline.
- **Phase/timer names**: `"init"`, `"read"`, `"merge"`, `"layout"`, `"relocate"`, `"finalize"`, `"write"`, `"cicc-lto"`, `"ptxas-lto"` -- nine phase names passed to the timing infrastructure, directly identifying the pipeline stages.
- **Format magic numbers**: The fatbin magic `0xBA55ED50` and the ELF machine type `190` (EM_CUDA) appear in validation code, anchoring the input parsers.
- **ELF section names**: `".nv.constant"`, `".nv.shared"`, `".nv.info"`, `".nv.callgraph"`, `".nvFatBinSegment"` -- each string reference identifies a section builder or parser function.
- **Option names and help strings**: The option parser registers CLI flags by name, and each registration call references a string literal that identifies the option.

### Structural Pattern Recognition (High Confidence)

Many function families in nvlink follow rigid structural templates. The SASS instruction encoder/decoder region (0xDA0000 -- 0x12B0000, approximately 4.5 MB) contains over 2,000 functions that each follow a nearly identical pattern: load a format descriptor from a constant table, set bitfield positions in a 128-bit instruction word, and store operand metadata into a fixed-layout structure. The encoder and decoder populations were identified by template matching against these structural invariants.

Similarly, the ELF manipulation functions (`elfw_*` family) share a consistent pattern: take a context pointer as first argument, validate section indices against bounds, and manipulate structures whose field offsets match the ELF specification.

### Vtable and Virtual Dispatch Analysis (High Confidence)

The embedded ptxas component uses C++ virtual dispatch extensively. Architecture-specific backends (one per SM target) implement virtual methods for instruction legalization, register class definitions, and scheduling parameters. By reconstructing vtable layouts from `.rodata` pointer arrays and tracing virtual call sites, the per-architecture backend classes were identified without symbols.

The vtable at a given `.rodata` address contains method pointers at fixed offsets. Cross-referencing which functions store a vtable pointer into an object's first field identifies the constructor, and the vtable's method entries identify the virtual method implementations.

### Callgraph Propagation (Medium-High Confidence)

Once a function is identified with high confidence, its callees and callers gain contextual identity. The `main` function (identified at 0x409800 by `__libc_start_main` xref and string evidence) calls a sequence of subfunctions whose order matches the pipeline phases. Identifying `main` propagates identity to the option parser, the input loop, the merge/layout/relocate/finalize/write sequence, and the LTO orchestrator.

The call graph's 552,453 edges make this propagation particularly productive: a single high-confidence identification can cascade identity to dozens of related functions through direct call relationships.

### Size and Structural Fingerprinting (Medium Confidence)

Some functions are identifiable by their size and internal structure alone. The `main` function at 57,970 bytes is by far the largest non-encoder function in the binary. The PTX parser functions contain characteristic string switch tables over PTX directive names. The relocation engine contains switch statements over relocation type constants whose case values match the known R_CUDA_* enumeration.

## Confidence Levels

Every function identification in this wiki carries one of three confidence levels:

| Level | Meaning | Basis |
|---|---|---|
| **HIGH** | Identification is certain or near-certain | Direct string evidence naming the function or its subsystem, multiple corroborating indicators (string + callgraph + structure), or the function is a trivial wrapper around a known target |
| **MEDIUM** | Identification is probable | Single strong indicator (vtable match, size fingerprint, callgraph position) corroborated by context, or structural match to a known pattern without direct string evidence |
| **LOW** | Identification is speculative | Inferred solely from callgraph context, parameter patterns, or address proximity without independent corroboration |

Approximately 50% of identified functions carry HIGH confidence, concentrated in the linker core (rich in error messages and ELF section name strings), the pipeline orchestration (phase timer strings), and the input processing (format magic and validation strings). The remaining functions are MEDIUM or LOW, concentrated in the compiler backend (SASS encoder/decoder tables, register allocator internals, scheduling heuristics) where string evidence is sparse.

## Cross-Reference Databases

The analysis relies on four primary JSON databases for cross-referencing:

**`nvlink_strings.json`** (31,237 entries) -- Every string in the binary with its address, value, string type, and a list of code locations that reference it. This is the single most important evidence source. Querying "which functions reference a string containing `relocat`" immediately narrows the scope to relocation-related functions.

**`nvlink_names.json`** (16,650 entries) -- All named locations in the binary. In a stripped binary, most names are IDA auto-generated (`sub_XXXXXX`, `loc_XXXXXX`), but 156 import names from the PLT and a smaller number of IDA-recognized library signatures provide ground truth anchors.

**`nvlink_callgraph.json`** (552,453 edges) -- The complete static call graph. Each edge records the caller function name/address and callee function name/address. This enables both forward tracing ("what does this function call?") and backward tracing ("who calls this function?"). The graph is also available in Graphviz DOT format for visualization.

**`nvlink_xrefs.json`** (7,513,413 entries) -- All cross-references (code-to-code, code-to-data, data-to-data) with source address, source function, target address, target function, and type code. This is the most complete relationship database but also the largest and most expensive to query.

Supplementary databases include `nvlink_functions.json` (40,532 function metadata records), `nvlink_imports.json` (156 PLT imports), `nvlink_segments.json` (25 ELF segments), and `nvlink_comments.json` (82,893 IDA-generated comments).

### What Each Artifact Reveals

**Functions** (`nvlink_functions.json`): The master index. Every function's address, size, instruction count, caller list, and callee list. The caller/callee lists are the basis for callgraph analysis. The `is_thunk` flag identifies PLT stubs (exclude from analysis). The `is_library` flag identifies functions IDA tagged as library code (CRT startup, allocator internals).

**Strings** (`nvlink_strings.json`): The primary identification tool. Each string's xref list shows which functions reference it. Searching for `"merge"` returns strings that anchor the merge engine. Searching for `".nv."` returns NVIDIA section names that anchor the ELF manipulation functions. The phase timer names (`"init"`, `"read"`, `"merge"`, `"layout"`, `"relocate"`, `"finalize"`, `"write"`) are direct pipeline stage identifiers.

**Call graph** (`nvlink_callgraph.json`): The structural backbone. Each edge records a direct call from one function to another. Indirect calls (vtable dispatch, function pointer callbacks) are not captured, which is the primary limitation. The call graph is used for module boundary detection, propagation from known functions, and entry/exit point analysis.

**Cross-references** (`nvlink_xrefs.json`): The most comprehensive artifact. Contains all code-to-code, code-to-data, and data-to-data references detected by IDA. At 7.5 million entries, it is too large to load into memory on machines with less than 16 GB RAM. Used for deep analysis of specific functions: finding all references to a particular `.rodata` constant, tracing data flow through global variables, and identifying vtable consumers.

**Comments** (`nvlink_comments.json`): IDA's auto-generated comments (e.g., `"File format: \\x7FELF"`) on instruction operands. The auto-comments on function prologues identify calling conventions and stack frame layouts.

**Names** (`nvlink_names.json`): IDA's auto-generated names for data and code addresses. Of 16,650 entries, most are auto-generated string reference names, with 156 import names from the PLT providing ground truth anchors.

**Imports** (`nvlink_imports.json`): The 156 PLT imports. Key imports include `pthread_*` (thread pool infrastructure), `malloc`/`free`/`realloc`, `dlopen`/`dlsym` (used by the LTO pipeline to load libnvvm at runtime), `_setjmp`/`longjmp` (error recovery), and `clock`/`gettimeofday` (timing infrastructure).

**Segments** (`nvlink_segments.json`): The 25 ELF segments/sections. Used to establish the address space layout and map code/data boundaries. The `.rodata` section (7.2 MB) is particularly important -- it contains string literals, vtable pointers, jump tables, constant arrays, and SASS instruction encoding tables.

## Kernel-Side Cross-Referencing

For NVLink protocol context -- register definitions, packet formats, topology negotiation, error handling semantics -- the open-source kernel module at `firmware/open-gpu-kernel-modules/` provides an invaluable cross-reference. The kernel-side NVLink driver defines structures and constants that the userspace nvlink binary must interoperate with. Matching constant values, register offsets, and error code enumerations between the stripped binary and the kernel source provides additional identification confidence for nvlink functions that interface with the driver.

## Cross-Referencing with Sibling Binaries

nvlink shares significant code with two other CUDA toolkit binaries analyzed in sibling wikis:

**ptxas** (standalone GPU assembler, [ptxas wiki](../ptxas/index.html)): The embedded ptxas component in nvlink is a statically linked copy of ptxas. Functions in the nvlink address range 0x7A0000 -- 0x1D32172 correspond to ptxas functions covering PTX parsing, instruction selection, register allocation, scheduling, peephole optimization, and SASS encoding. The standalone ptxas wiki provides detailed analysis of these subsystems that directly applies to their nvlink-embedded counterparts. String anchors, structural patterns, and algorithm descriptions from the ptxas wiki were used to accelerate identification of the same functions at different addresses in nvlink.

**cicc** (CUDA C++ compiler, [cicc wiki](../cicc/index.html)): The LTO pipeline in nvlink dynamically loads `libnvvm.so` (itself a component of cicc) via `dlopen`/`dlsym`. The cicc wiki's analysis of the NVVM optimization pipeline, pass registration patterns, and IR format provides context for understanding the LTO callback interface and the data formats exchanged between nvlink and the loaded compiler library.

## Limitations and Known Gaps

This analysis has several inherent limitations:

- **Stripped binary, no symbols.** Every function name in this wiki is reconstructed from evidence. The original NVIDIA symbol names are unknown. Assigned names are descriptive approximations chosen for clarity, not authoritative labels.

- **No dynamic validation.** All findings are from static analysis. Runtime behavior under specific inputs (unusual SM targets, edge-case CUDA constructs, malformed input files) has not been verified.

- **ROT13 obfuscation on internal pass names.** The embedded ptxas applies ROT13 encoding to some internal pass/phase names in its string pool. These have been decoded where identified, but additional obfuscated strings may remain undetected. See [ROT13-Encoded Pass Names](reference/rot13-passes.md) for the decoded catalog.

- **Five mega-functions exceed Hex-Rays limits.** Approximately 5 functions (including `main` at 57,970 bytes and several ISel hub functions exceeding 200 KB) are too large for Hex-Rays to fully decompile in a single pass. For these, analysis was performed on raw disassembly and per-basic-block CFGs, which is slower and lower-confidence than working from pseudocode. The ISel hubs are the most impactful loss -- they contain the pattern-matching dispatch tables that map IR operations to machine instructions for each SM architecture.

- **Inlined functions are invisible.** Functions that the compiler inlined during the build of nvlink itself have no standalone address and cannot be independently identified. Small utility functions (string comparison, vector operations, hash computations) are likely inlined throughout.

- **Indirect calls are underrepresented in the call graph.** The 552,453 call edges capture only direct `call` instructions. Virtual dispatch through vtable pointers, function pointer callbacks, and computed jumps are not fully represented. This primarily affects the embedded ptxas's architecture dispatch and the pass manager's polymorphic invocations.

- **Proprietary NVIDIA code has no public reference.** The linker core, Mercury format support, NV-info metadata processing, and SASS encoding tables are entirely NVIDIA-proprietary. These are identified purely from string evidence and structural analysis with no upstream source to compare against.

- **Statically linked ptxas obscures module boundaries.** The embedded ptxas is statically linked into nvlink -- there is no shared library boundary, no separate PLT, and no linker symbol table separating the two components. The division between "linker code" and "compiler code" in this wiki is inferred from code structure and string evidence. Functions at the boundary (e.g., the LTO orchestrator that bridges both sides) are documented with their dual roles but the exact module boundary is approximate.

- **Version-specific.** All findings apply to nvlink v13.0.88 (build `cuda_13.0.r13.0/compiler.36424714_0`, CUDA Toolkit 13.0). Addresses, function sizes, and feature sets differ in other CUDA toolkit versions.

- **Module boundary ambiguity.** The binary does not contain module or compilation unit boundaries. The subsystem assignments in this wiki (linker core vs. embedded ptxas vs. Mercury) are inferred from code structure and string evidence. Functions at subsystem boundaries may be misclassified.

- **Decompiled file count vs function count discrepancy.** The decompiled directory contains 40,210 files rather than the expected 40,366 (the number of successfully decompiled functions). The difference of 156 corresponds approximately to the PLT import stubs, which IDA counts as functions but the decompiler skips or produces empty output for. Similarly, the disassembly directory contains 40,376 files and the graph directory contains 80,752 files (40,376 function pairs of JSON + DOT).

## Verification Approaches

To verify any specific finding in this wiki:

1. **Open IDA at the stated address.** Every function identification includes a hex address. Navigate to it, press F5 to decompile, and check whether the decompiled code matches the described behavior.

2. **Check string xrefs.** For HIGH confidence identifications, search for the quoted string in IDA's Strings window (Alt+T). The xref should lead to the stated function address or a direct caller.

3. **Trace from main.** Start at 0x409800 (main) and follow the call chain through the pipeline phases. Every function reachable from main through a chain of identified functions has a verified callgraph path.

4. **Cross-reference with the kernel module.** For NVLink protocol structures and register definitions, compare the constants and offsets found in the binary against the definitions in `firmware/open-gpu-kernel-modules/`. Agreement between the stripped binary and the kernel source significantly increases confidence.

5. **Compare encoder/decoder pairs.** In the SASS encoder/decoder region, every encoder function has a mirror decoder. The encoder sets bitfields; the decoder reads them. If an encoder is identified for a specific opcode, the corresponding decoder should exhibit the inverse operation on the same bit positions.

6. **Cross-reference with the ptxas wiki.** For functions in the embedded ptxas region (0x7A0000 -- 0x1D32172), the standalone [ptxas wiki](../ptxas/index.html) documents the same algorithms and data structures at different addresses. Structural agreement between the nvlink-embedded version and the standalone ptxas version increases confidence.

7. **Verify against decompiled files.** For any claimed function behavior, locate the corresponding `.c` file in `decompiled/` (naming convention: `{name}_{hex_addr}.c`). Read the Hex-Rays pseudocode and confirm the described logic matches. For the 168 functions that failed decompilation, check the `.asm` file in `disasm/` instead.

## Reproducing the Analysis

To reproduce this analysis from scratch:

1. **Obtain the binary.** Install CUDA Toolkit 13.0. The binary is at `<cuda>/bin/nvlink`. The version string must be `"Cuda compilation tools, release 13.0, V13.0.88"` and the build string `"Build cuda_13.0.r13.0/compiler.36424714_0"`.

2. **Run IDA auto-analysis.** Open nvlink in IDA Pro 9.x with default x86-64 ELF analysis settings. Allow auto-analysis to complete (typically 10-20 minutes for a 37 MB binary). Accept the detected compiler (GCC).

3. **Run the extraction script.** Execute `analyze_nvlink.py` via IDA's script runner or headless mode:
   ```
   idat64 -A -S"analyze_nvlink.py" nvlink.i64
   ```
   This produces all JSON databases, per-function disassembly/decompilation, CFG graphs, and the raw `.rodata` dump. Expected runtime: 4-8 hours, dominated by the Hex-Rays decompilation pass.

4. **Verify extraction.** Check the log output against the expected counts: 40,532 functions, 31,237 strings, 552,453 call edges, 7,513,413 xrefs, 82,893 comments, 16,650 names.

5. **Sweep the .text section.** Divide the address range 0x400000 -- 0x1D32172 into manageable regions (448 KB to 1.9 MB each). For each region, systematically analyze every function above the size threshold using the decompiled pseudocode, string cross-references, and callgraph context. Record findings in structured sweep reports.

6. **Synthesize into wiki pages.** Use the sweep reports as evidence to write per-subsystem documentation. Cross-reference every claim against the JSON databases and cite specific addresses and strings.

### Dependencies

The extraction script (`analyze_nvlink.py`) requires IDA Pro 9.x with Hex-Rays decompiler and Python 3.x. No external Python packages are needed -- only the IDA Python API (`idautils`, `idc`, `idaapi`, `ida_bytes`, `ida_funcs`, `ida_segment`, `ida_nalt`, `ida_gdl`, `ida_hexrays`).

Post-export analysis requires only the Python 3.8+ standard library (`json`, `collections`, `pathlib`).

## Version-Stability Assessment

Not everything changes between nvlink versions. Understanding what is stable reduces update effort when a new CUDA toolkit ships.

**Version-stable** (survives across minor and most major releases):

| Category | Examples | Why stable |
|---|---|---|
| Algorithm logic | MurmurHash3 constants, LZ4 compression, relocation resolution sequence | Algorithms are rarely rewritten between releases |
| Pipeline phase names | `"init"`, `"read"`, `"merge"`, `"layout"`, `"relocate"`, `"finalize"`, `"write"` | Phase names are embedded in timing format strings |
| ELF section name strings | `".nv.constant"`, `".nv.shared"`, `".nv.info"` | NVIDIA section names are part of the device ELF ABI |
| R_CUDA relocation type values | `R_CUDA_32`, `R_CUDA_ABS32_LO_20`, etc. | Relocation codes are part of the toolchain ABI |
| Error message text | `"cubin not an elf?"`, `"fatbin wrong format?"` | Diagnostic strings are rarely reworded |
| ROT13 encoding scheme | `codecs.decode(s, "rot_13")` for internal pass names | Obfuscation scheme has been consistent across versions |
| SASS encoding handler template | Bitfield insert, `movaps` format descriptor, operand registration, finalize | Template structure is generated from a stable code generator |
| Subsystem names | OCG, Mercury, Ori | Internal codenames are stable across releases |

**Version-fragile** (changes with every recompilation):

| Category | Examples | Why fragile |
|---|---|---|
| Function addresses | Every `sub_XXXXXX` reference | Code shifts when any preceding section grows |
| Address ranges | Sweep boundaries, subsystem regions | Functions move when preceding code changes |
| Function sizes | `main` at 57,970 bytes | Optimizer decisions change between builds |
| Caller/callee counts | 552,453 call graph edges | New call sites and functions |
| Struct offsets | Context struct field positions | New fields inserted into structures |
| `.rodata` addresses | String locations, encoding table addresses | Data layout shifts with code changes |
| Total function count | 40,532 | New SM targets add encoding handlers |
