# Function Map

Address-to-identity lookup table. Confidence: VERY HIGH = string evidence, HIGH = strong structural evidence, MEDIUM = inferred from context/callgraph.

## Top Functions by Size

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0xA939D0` | 457KB | X86 AutoUpgrade (intrinsic rename, leftover from LLVM x86 target) | VERY HIGH |
| `0x10EE7A0` | 396KB | InstCombine::visitCallInst / visitIntrinsic | HIGH |
| `0x20019C0` | 341KB | SelectionDAG LegalizeTypes workhorse (ExpandOp/PromoteOp) | HIGH |
| `0x2368220` | 326KB | New PassManager pipeline parser (function-level, 268 pass names) | VERY HIGH |
| `0x786210` | 317KB | EDG constexpr tree-walking interpreter (124 C++ operator opcodes) | VERY HIGH |
| `0x20ACAE0` | 295KB | SelectionDAG LegalizeOp main switch | HIGH |
| `0x2081F00` | 261KB | SelectionDAGBuilder::visit (IR → DAG) | HIGH |
| `0xBFC6A0` | 207KB | LLVM IR Verifier (visitCallInst), 298 verification messages | VERY HIGH |
| `0xA8A170` | 195KB | X86 Intrinsic Upgrade Helper (broadcastf32x4, compress, etc.) | HIGH |
| `0x7506E0` | 190KB | EDG IL tree walker (297 self-recursive calls, 87 node types) | HIGH |
| `0x7C0F00` | 185KB | EDG declaration specifier parser (393 LABEL_ gotos, NOT switch/case) | HIGH |
| `0x9F2A40` | 182KB | Bitcode Reader parseFunctionBody, 174 error strings | VERY HIGH |
| `0x77FCB0` | 150KB | EDG constexpr dispatch (80 expression types + 62 intrinsics) | HIGH |
| `0x766570` | 148KB | EDG IL tree copier/transformer (callback-driven, template instantiation) | HIGH |
| `0x1FFB890` | 137KB | SelectionDAG LegalizeTypes dispatch (967 case labels) | HIGH |
| `0x672A20` | 132KB | EDG declaration specifier state machine (80 token cases, 4,371 lines) | VERY HIGH |
| `0x672A20` | 129KB | Compilation pipeline entry (IR-to-PTX orchestrator) | KNOWN |
| `0x12FCDB0` | 129KB | jemalloc malloc_conf_init (199 config strings) | VERY HIGH |
| `0x11A7600` | 125KB | computeKnownBits / SimplifyDemandedBits | VERY HIGH |
| `0x617BD0` | 123KB | EDG lgenfe_main (282-case CLI switch, 737 config #defines) | VERY HIGH |
| `0x126A910` | 123KB | NVVM Builtin Resolution table (post-opt, 770 entries) | VERY HIGH |
| `0x12D6300` | 125KB | NVVMPassOptions init (4,786 lines, 221 slots in 4,512-byte struct) | VERY HIGH |
| `0x12D6170` | — | PassOptionRegistry::lookupOption (hash table at registry+120) | HIGH |
| `0x12D6240` | — | PassOptionRegistry::getBoolOption (triple: '1'/true, 't'/true) | HIGH |
| `0x12D6090` | — | writeStringOption (24-byte entry to output struct) | HIGH |
| `0x12D6100` | — | writeBoolOption (16-byte entry to output struct) | HIGH |
| `0x12C35D0` | 41KB | 4-stage pipeline orchestrator (LNK/OPT/OPTIXIR/LLC), nvopt+nvllc objects | VERY HIGH |
| `0x12C06E0` | 63KB | Bitcode linker: triple validation, IR version check, symbol size matching | VERY HIGH |
| `0x12BFF60` | 9KB | NVVM IR version checker (nvvmir.version metadata, NVVM_IR_VER_CHK env) | VERY HIGH |
| `0x12642A0` | — | NVVM container format parser (arch, FTZ, IEEE, opt level extraction) | HIGH |
| `0x12E7B90` | 3KB | Concurrent worker entry (dispatches Phase I/II) | HIGH |
| `0x12E1EF0` | 51KB | Concurrent compilation entry (jobserver, thread pool, split-module) | VERY HIGH |
| `0x12E0CA0` | — | Function sorting by priority (insertion sort / introsort) | HIGH |
| `0x12E8D50` | — | Per-function compilation callback (completion handler) | HIGH |
| `0x12E86C0` | — | Phase II per-function optimizer (sets qword_4FBB3B0=2) | HIGH |
| `0x12D4250` | — | Concurrency eligibility check (counts defined functions) | HIGH |
| `0x16832F0` | — | GNU Jobserver init (parse MAKEFLAGS, create pipe, spawn pthread) | HIGH |
| `0xA09F80` | 121KB | Bitcode Metadata Reader (parseMetadata) | VERY HIGH |
| `0x627530` | 114KB | EDG IL function body processor (14 params, scope stack, member handling) | HIGH |
| `0x760BD0` | 109KB | EDG IL tree walker #2 (427 self-recursive calls) | HIGH |
| `0x8BA620` | 108KB | EDG IL codegen (node type dispatch on byte+80, 2,589 lines) | HIGH |
| `0x90AEE0` | 107KB | NVVM Builtin Resolution table (pre-opt, 770 entries) | VERY HIGH |
| `0x955A70` | 103KB | NVVM Builtin lowering entry (pre-opt, wgmma/tex/surf) | HIGH |
| `0x2377300` | 103KB | New PassManager pipeline parser (CGSCC-level) | HIGH |

## Pipeline Functions

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x4396A0` | tiny | `main()` thunk → `sub_8F9C90` | KNOWN |
| `0x8F9C90` | 10KB | Real main: CLI parsing, wizard check, dispatch | VERY HIGH |
| `0x902D10` | — | Simple compile entry (Path A) | HIGH |
| `0x1262860` | — | Simple compile entry (Path B) | HIGH |
| `0x905EE0` | 43KB | LibNVVM pipeline driver (Path A): 14-phase flow, libdevice linking, API dispatch | VERY HIGH |
| `0x1265970` | 48KB | LibNVVM compilation entry (Path B): 4-stage pipeline, embedded builtins | VERY HIGH |
| `0x905880` | 6KB | CUDA C++ Front-End stage (lgenfe): timer "CUDA C++ Front-End" | HIGH |
| `0x9047E0` | 10KB | NVVM IR Container → Module opt setup | HIGH |
| `0x908850` | 10KB | Backend SM config + EDG binding, triple construction | HIGH |
| `0x903BA0` | 5KB | LNK stage verbose callback | HIGH |
| `0x903730` | 5KB | LLC stage verbose callback | HIGH |
| `0x900130` | — | CLI processing (Path A): -arch, -maxreg, -split-compile, -gen-lto | HIGH |
| `0x125FB30` | — | CLI processing (Path B) | HIGH |
| `0x5D2A80` | 2KB | Core EDG orchestration (setjmp error recovery, timers) | VERY HIGH |
| `0x5E3AD0` | 11KB | Backend: "Generating NVVM IR", opens .int.c/.device.c/.stub.c, TileIR dlopen | VERY HIGH |
| `0x9685E0` | — | Multi-stage orchestrator: .lnk.bc → .opt.bc → .ptx | HIGH |
| `0x95EB40` | 15KB | Architecture detection: -arch → triple fan-out | VERY HIGH |
| `0x9624D0` | — | NVVM option parsing (all -opt-*, -llc-*, -gen-*, -Xopt) | HIGH |
| `0x8FE280` | — | Flag mapping table (O0-O3, nvcc flag translation) | HIGH |
| `0xB6EEA0` | — | LLVM cl::opt bulk registration (~1500 options) | HIGH |
| `0xC996C0` | — | Timer/context creation ("CUDA C++ Front-End", "LibNVVM") | HIGH |

## EDG 6.6 Frontend

### Core Orchestration

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x5D2A80` | 2KB | Master orchestrator (setjmp recovery, timer callbacks) | VERY HIGH |
| `0x617BD0` | 123KB | lgenfe_main: 282-case CLI switch, 737 config macros, EDG 6.6 | VERY HIGH |
| `0x610260` | 22KB | CLI option registration table (~300 options via sub_6101D0) | HIGH |
| `0x6140E0` | 6KB | Option fetcher (called in main loop of sub_617BD0) | HIGH |
| `0x5E3AD0` | 11KB | Backend entry: "Generating NVVM IR", file output, TileIR dlopen | VERY HIGH |
| `0x8D0BC0` | — | Translation unit init (416-byte TU object, keyword init, parser entry) | VERY HIGH |
| `0x8D0F00` | tiny | Semantic analysis init (zeroes 6 globals) | HIGH |
| `0x706250` | 30KB | Keyword table init (~350 keywords via sub_885C00) | VERY HIGH |
| `0x709330` | 5KB | TU finalization ("Generating Needed Template Instantiations") | HIGH |
| `0x885C00` | tiny | Register single keyword: `(token_id, "keyword_string")` | HIGH |

### AST-to-Source Printer Cluster

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x5DBFC0` | 41KB | Main expression/statement emitter (61 self-references, recursive) | HIGH |
| `0x5E13C0` | 44KB | Function declaration printer (\_\_sti\_\_, #pragma section, nv\_linkonce\_odr) | HIGH |
| `0x5DFD00` | 26KB | Statement printer (if/else/for/while/switch/case/return) | HIGH |
| `0x5D9330` | 12KB | Declaration printer (linkage/storage, \_\_builtin\_va\_alist) | HIGH |
| `0x5DA0F0` | 13KB | Scope/block printer (bit-fields, array dimensions) | HIGH |
| `0x5DAD30` | 9KB | Struct/union/enum printer (#pragma pack) | HIGH |
| `0x5D80F0` | 17KB | Variable initializer printer (memcpy, aggregate init) | HIGH |
| `0x5DF1B0` | 11KB | Inline asm printer (volatile, constraints, format specifiers) | HIGH |
| `0x5D5A80` | 7KB | Identifier printer (keyword mangling: auto→\_\_xauto) | HIGH |
| `0x5DB980` | 7KB | Top-level declaration dispatcher | HIGH |
| `0x5D7860` | 6KB | Function parameter list printer (\_\_text\_\_/\_\_surf\_\_ annotations) | HIGH |

### Parser & Declaration Processing

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x672A20` | 132KB | Declaration specifier state machine (while/switch, 80 token cases) | VERY HIGH |
| `0x7C0F00` | 184KB | Declaration specifier parser (393 LABEL\_ gotos, NOT switch/case) | HIGH |
| `0x662DE0` | 61KB | Top-level declaration/declarator parser | HIGH |
| `0x6523A0` | 64KB | Overloaded function resolution (\_\_builtin\_ detection, OMP variants) | HIGH |
| `0x66AC40` | 49KB | Struct/union/class specifier processing | HIGH |
| `0x66F9E0` | 39KB | Enum specifier processing | HIGH |
| `0x63CAE0` | 67KB | Block-level declaration/statement processor (largest in 0x630000 zone) | HIGH |
| `0x661400` | 28KB | Declaration statement parsing (35 token refs, 14 diagnostics) | HIGH |
| `0x66DF40` | 24KB | Function declarator processing (parameter lists, return types) | HIGH |
| `0x668EE0` | 26KB | Declaration specifier combination validator | HIGH |
| `0x668230` | 9KB | Storage class specifier processor (\_Thread\_local validation) | HIGH |
| `0x6333F0` | 26KB | Primary declarator-to-IL conversion (type kind dispatch) | HIGH |
| `0x64BAA0` | 46KB | Name/identifier processing | HIGH |
| `0x64A920` | 25KB | Builtin/intrinsic recognition (53 string refs, C++20/23 reflection) | HIGH |
| `0x627530` | 114KB | IL function body processor (14 params, scope stack management) | HIGH |
| `0x62C0A0` | 63KB | IL statement processing (16 params, IL walker/transformer) | HIGH |

### Type System

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x713ED0` | 36KB | Type conversion checker (recursive, vector type handling) | HIGH |
| `0x7115B0` | 17KB | Binary operation type checker (11 callers — very central) | HIGH |
| `0x712770` | 12KB | Usual arithmetic conversions (10 params) | HIGH |
| `0x7386E0` | 23KB | Type node comparator (parallel tree walk, canonicalization) | HIGH |
| `0x739430` | 20KB | Declaration-level type comparison | HIGH |
| `0x74A390` | 29KB | Type-to-string emitter (19 callers, backbone of diagnostics) | VERY HIGH |
| `0x748000` | 45KB | Constant expression emitter (alignof, sizeof, nullptr, zero-init) | HIGH |
| `0x74D110` | 10KB | Declarator emitter (19 callers, paired with sub_74A390) | HIGH |
| `0x73A9D0` | 19KB | Type node deep-copy | HIGH |
| `0x73F780` | 6KB | Declaration node deep-copy (192 bytes = 12 x \_\_m128i) | HIGH |
| `0x73CC20` | 9KB | Operator overloadability checker | HIGH |

### IL Tree Infrastructure

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x7506E0` | 190KB | IL tree walker #1 (297 self-recursive, 87 node types, 305 cases) | HIGH |
| `0x760BD0` | 109KB | IL tree walker #2 (427 self-recursive, parallel traversal) | HIGH |
| `0x75C0C0` | 87KB | IL tree walker #3 (316 self-recursive) | HIGH |
| `0x766570` | 148KB | IL tree copier/transformer (callback params a3/a4, template instantiation) | HIGH |
| `0x759B50` | 31KB | Walker driver/setup (5 callbacks + flags) | HIGH |
| `0x75B260` | 16KB | Copier driver (parallel to sub_759B50) | HIGH |
| `0x75AFC0` | — | Master walker driver (sets all 6 global callback pointers) | HIGH |

### Constexpr Evaluator

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x786210` | 317KB | Expression evaluator core (124 operator opcodes, 9,075 lines) | VERY HIGH |
| `0x795660` | 77KB | Statement executor (declarations, loops, switch, compound blocks) | HIGH |
| `0x79CCD0` | 67KB | Object member accessor (base classes, virtual bases, union tracking) | HIGH |
| `0x799B70` | 33KB | Aggregate initializer evaluator (arrays, structs, designated init) | HIGH |
| `0x79B7D0` | 29KB | Function call evaluator (argument binding, recursion limits) | HIGH |
| `0x77FCB0` | 150KB | Top-level dispatch (80 expression types + 62 intrinsics) | HIGH |
| `0x7764B0` | 18KB | Type size calculator (Robin Hood hash memoization, 64MB cap) | HIGH |
| `0x7987E0` | 11KB | Loop/range-for evaluator | HIGH |
| `0x77C870` | 18KB | Builtin call evaluator (dispatched from case 0x3D) | HIGH |
| `0x77D750` | 34KB | Aggregate initializer evaluator (struct/array/union at compile time) | HIGH |

### Preprocessor

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x7B8B50` | 59KB | Main preprocessor token scanner (all C/C++ token kinds) | HIGH |
| `0x81B8F0` | 77KB | Macro expansion engine (99-entry predefined table, \_\_VA\_OPT\_\_) | HIGH |
| `0x7B40D0` | 42KB | Numeric literal tokenizer (hex float, binary, digit separators) | HIGH |
| `0x7BC390` | 29KB | Character classification / next-token dispatch (trigraphs, line splices) | HIGH |
| `0x7B6B00` | 13KB | String literal scanner (escape processing, raw strings) | HIGH |
| `0x8200E0` | 22KB | Macro body substitution (\_\_VA\_ARGS\_\_, \_\_VA\_OPT\_\_) | HIGH |
| `0x7B2B10` | 16KB | Source character reader / tokenizer bootstrap | HIGH |
| `0x7B8270` | 8KB | Preprocessing directive dispatcher | HIGH |

### Template Engine

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x7A9440` | 40KB | Complete template instantiation engine (parameter lists, member iteration) | HIGH |
| `0x7410C0` | 42KB | Template argument type resolution/matching | HIGH |
| `0x743600` | 19KB | Template type instantiation handler | HIGH |
| `0x5EBF70` | 30KB | Template instantiation engine (word\_4F06418 SM-arch checks) | HIGH |
| `0x5FBCD0` | 38KB | Template argument deduction engine (pattern matching, pack expansion) | HIGH |

### Semantic Analysis

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x6040F0` | 64KB | Deep semantic analysis (29 SM-arch refs, 27 sub\_8D\* calls) | HIGH |
| `0x607B60` | 32KB | Overload resolution main (43 SM-arch refs — highest) | HIGH |
| `0x609F00` | 58KB | Expression parsing/semantic ("Parsing Lambda", \_\_nv\_parent) | HIGH |
| `0x5FE9C0` | 28KB | Declaration processing (9 SM version refs) | HIGH |
| `0x5F94C0` | 24KB | Class hierarchy analysis (vtable layout, diamond inheritance) | HIGH |
| `0x5F4F20` | 21KB | Conversion function lookup (33 sub\_8D\* calls) | HIGH |
| `0x5F2920` | 23KB | Operator overload resolution | HIGH |
| `0x84EC30` | 71KB | Declaration elaboration (type-spec strings "A;P", "O;F", "I", "B") | HIGH |
| `0x8708D0` | 63KB | Declaration semantic analysis (148 global refs, highest density) | HIGH |

### CUDA-Specific Frontend

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x6582F0` | 22KB | Memory space attribute processing (\_\_shared\_\_, \_\_constant\_\_, \_\_managed\_\_) | HIGH |
| `0x65F400` | 24KB | Declaration with memory space annotation (15 diagnostic calls) | HIGH |
| `0x6BBC40` | 34KB | Atomic builtin name generator (\_\_nv\_atomic\_fetch\_\*) | HIGH |
| `0x804B20` | 28KB | CUDA device code generation master | HIGH |
| `0x806F60` | 8KB | CUDA registration stub (\_\_cudaRegisterAll, \_\_cudaRegisterEntry) | VERY HIGH |
| `0x808590` | 11KB | Device stub generator ("\_\_device\_stub\_%s", \_\_cudaLaunch) | HIGH |
| `0x7F2B50` | 16KB | CUDA kernel launch lowering (cudaGetParameterBufferV2) | HIGH |
| `0x801880` | 7KB | Static init with CUDA memory space (\_\_sti\_\_, \_\_constant\_\_) | HIGH |
| `0x60D650` | 6KB | Optimization flag configurator (109 flags from O-level) | HIGH |
| `0x60E7C0` | 12KB | SM-arch feature gate (56 qword\_4F077A8 comparisons) | HIGH |

### Name Mangling (Itanium ABI)

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x8E74B0` | 29KB | Primary mangling entry | HIGH |
| `0x8E9FF0` | 26KB | Type mangling | HIGH |
| `0x816460` | 24KB | Type component mangling (\_\_real\_\_, \_\_imag\_\_) | HIGH |
| `0x80E340` | 23KB | Builtin type mangling (DF16\_, Cu6\_\_bf16, u6\_\_mfp8) | HIGH |
| `0x80FE00` | 8KB | NVIDIA extension mangling (Unvdl, Unvdtl, Unvhdl) | HIGH |
| `0x80C5A0` | 11KB | Special type mangling (basic\_ostream, allocator substitution) | HIGH |
| `0x813790` | 13KB | Expression mangling | HIGH |

### Diagnostics & Support

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x681D20` | 37KB | Diagnostic emitter (severity labels, ANSI color, word-wrap) | VERY HIGH |
| `0x6837D0` | 20KB | SARIF JSON diagnostic output (ruleId, level, locations) | HIGH |
| `0x67FCF0` | 40KB | Type name formatter (quoted type names for error messages) | HIGH |
| `0x721090` | tiny | EDG abort / \_\_builtin\_unreachable (478 callers!) | VERY HIGH |
| `0x720FF0` | — | Exit with status ("Compilation aborted/terminated") | HIGH |
| `0x724DC0` | — | IR node alloc with context (204 callers) | HIGH |
| `0x724E30` | — | IR node free (196 callers) | HIGH |
| `0x72C930` | — | Get/create void type singleton at qword\_4F07BA8 (145 callers) | HIGH |
| `0x7247C0` | — | Arena allocator (63 callers) | HIGH |
| `0x72DB90` | 8KB | IR node hash (polynomial: v10 += ch + 32\*v10, 9 callers) | HIGH |
| `0x822B10` | — | Tracked heap allocation (linked list at qword\_4F195F8) | HIGH |
| `0x823310` | — | Hash table bucket chain finalizer | HIGH |
| `0x823970` | — | EDG heap pool allocator (152-byte, 416-byte, etc. entries) | HIGH |

### Class Layout & Vtable

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x7E3EE0` | 7KB | Class layout emitter (\_\_vptr, \_\_v\_, \_\_b\_ prefixes) | HIGH |
| `0x7E57B0` | 9KB | Virtual base offset calculator | HIGH |
| `0x7E88E0` | 11KB | Virtual call lowering (node\_kind==103) | HIGH |
| `0x7E9AF0` | 13KB | Class definition emitter (vtable, nested types, friends) | HIGH |
| `0x7EE560` | 45KB | Statement emission mega-function (largest in class layout zone) | HIGH |
| `0x7FEC50` | 48KB | Class member emission (\_\_cxa\_atexit, \_\_cxa\_vec\_cctor) | HIGH |
| `0x7FCF80` | 17KB | Function definition emission (ctor initializers, default args) | HIGH |

## LLVM cl::opt Registration Infrastructure

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0xC523C0` | — | Global option counter (atomic increment) | HIGH |
| `0xC53080` | — | cl::Option::setArgStr(name, len) — Legacy PM | HIGH |
| `0xC53130` | — | cl::Option::addArgument() — Legacy PM | HIGH |
| `0xC57470` | — | cl::OptionCategory getter | HIGH |
| `0x16B8280` | — | cl::opt name setter — New PM | HIGH |
| `0x16B88A0` | — | cl::opt finalization — New PM | HIGH |
| `0xC8D5F0` | — | SmallVector::grow() | HIGH |

### Key Constructors (cl::opt registration)

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x4397F0` | ~102KB | ctor\_010\_0: TargetLibraryInfo VecFuncs table (9 vector math libs, NOT decompiled) | VERY HIGH |
| `0x48D7F0` | 30KB | ctor\_043\_0: NVIDIA CICC-specific options (19 opts, XOR cipher hidden flag) | VERY HIGH |
| `0x4A5950` | 7KB | MASTER pass/analysis registration (~172 init calls) | VERY HIGH |
| `0x4A64D0` | 59KB | ctor\_107\_0: MC/Target options (131 opts, getenv("bar") backdoor) | VERY HIGH |
| `0x4B0180` | 29KB | ctor\_133\_0: Known library function table (422 C/POSIX functions) | VERY HIGH |
| `0x4CC760` | 20KB | ctor\_147\_0: PassManager debug/print options | HIGH |
| `0x4CEB50` | 9KB | ctor\_156\_0: CLI infrastructure (help, version, print-options) | HIGH |
| `0x4DBEC0` | 14KB | ctor\_186\_0: Inliner heuristics (NVIDIA: profuseinline, inline-budget) | HIGH |
| `0x4E0990` | 9KB | ctor\_201: GVN options (NVIDIA: profusegvn, gvn-dom-cache) | HIGH |
| `0x4E4B00` | 8KB | ctor\_214\_0: LSR options (NVIDIA: disable-lsr-for-sharedmem32-ptr) | HIGH |
| `0x4E5C30` | 21KB | ctor\_216\_0: Loop Unrolling options (largest unroll ctor) | HIGH |
| `0x4F0FB0` | 17KB | ctor\_259\_0: CICC core compiler options (debug-compile, maxreg) | HIGH |
| `0x4F2830` | 10KB | ctor\_262\_0: BranchDist pass options | HIGH |
| `0x4F36F0` | 10KB | ctor\_263\_0: SCEV-CGP pass options (44 strings!) | HIGH |
| `0x4F54D0` | 10KB | ctor\_267\_0: MemorySpaceOpt options (18 strings) | HIGH |
| `0x4F7BE0` | 7KB | ctor\_277\_0: Rematerialization options (39 strings, remat-for-occ) | HIGH |
| `0x507310` | 29KB | ctor\_335\_0: MASTER codegen pass configuration (88 strings) | VERY HIGH |
| `0x50C890` | 16KB | ctor\_356\_0: NVPTX SM enum + PTX version table | VERY HIGH |
| `0x50E8D0` | 21KB | ctor\_358\_0: NVPTX pass enable/disable (43 strings, usedessa) | HIGH |
| `0x5108E0` | 8KB | ctor\_361\_0: NV Remat Machine Block options (30 strings) | HIGH |
| `0x512DF0` | 39KB | ctor\_376\_0: LTO/bitcode/plugin options | HIGH |
| `0x516190` | 44KB | ctor\_377\_0: PassBuilder pipeline configuration (77 strings) | HIGH |
| `0x51B710` | 15KB | ctor\_388\_0: Optimizer pipeline enables (enable-ml-inliner, etc.) | HIGH |
| `0x584510` | 3KB | ctor\_605: SM processor table (45 entries, sm\_20–sm\_121f, PTX version map) | VERY HIGH |
| `0x585D30` | 37KB | ctor\_609\_0: NVPTX backend options (25+ opts, usedessa, enable-nvvm-peephole) | HIGH |
| `0x57F210` | 59KB | ctor\_600\_0: CodeGen/TargetMachine mega-options (118 strings) | HIGH |

## NVIDIA Custom Pass Implementations

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x2CDD6D0` | reg | MemorySpaceOptPass registration | HIGH |
| `0x2CDFF20` | factory | MemorySpaceOptPass factory | HIGH |
| `0x2CDA660` | 10KB | MemorySpaceOpt core analysis | HIGH |
| `0x2CD7710` | 9KB | MemorySpaceOpt address space inference | HIGH |
| `0x1C6FBC0` | reg | IPMSPPass (interprocedural memory space) registration | HIGH |
| `0x1CE7DD0` | 13KB | RematerializationPass (IR-level) implementation | HIGH |
| `0x2186D90` | 9KB | Machine Block Rematerialization | HIGH |
| `0x1C4B520` | reg | BranchDistPass registration | HIGH |
| `0x1C7B2C0` | 11KB | LoopIndexSplitPass implementation | HIGH |
| `0x2CAF0F0` | reg | NVVMPeepholeOptimizerPass registration | HIGH |
| `0x2CD6510` | 350B | ByValMem2RegPass | HIGH |
| `0x2CD2690` | 366B | BasicDeadBarrierEliminationPass | HIGH |
| `0x1CEBC30` | reg | CNPLaunchCheckPass (Dynamic Parallelism validation) | HIGH |
| `0x1CB0B80` | name | PrintfLoweringPass | HIGH |
| `0x2342890` | 32KB | Pass registration master function (all 402+20 passes) | VERY HIGH |
| `0x233C410` | — | Pass name listing (pipeline names for all passes) | HIGH |

## MMA / Tensor Core Emission

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x21E74C0` | 17KB | MMA instruction operand builder (shapes, types, rounding modes) | VERY HIGH |
| `0x21E8CD0` | 2KB | tcgen05 Blackwell scaled MMA operands (scaleD, negA, negB, transA) | VERY HIGH |
| `0x21DFBF0` | 5KB | HMMA store-C (hmmastc), SM ≥ 70 | HIGH |
| `0x21E0360` | 3KB | HMMA load-A/B (hmmaldab), SM ≥ 70 | HIGH |
| `0x21E0630` | 3KB | HMMA load-C (hmmaldc), SM ≥ 70 | HIGH |
| `0x21E0870` | 4KB | HMMA MMA (hmmamma), SM ≥ 70 | HIGH |
| `0x21E1280` | 4KB | IMMA load-A/B (immaldab), SM ≥ 72 | HIGH |
| `0x21E15D0` | 3KB | IMMA load-C (immaldc), SM ≥ 72 | HIGH |
| `0x21E1830` | 5KB | IMMA store-C, SM ≥ 72 | HIGH |
| `0x21E1D20` | 6KB | IMMA MMA w/ saturation (immamma), SM ≥ 72 | HIGH |
| `0x21E2280` | 6KB | Binary MMA (bmmamma, b1 .and.popc/.xor.popc), SM ≥ 75 | HIGH |
| `0x21DEF90` | — | MMA address-space resolver (opcode → addrspace enum) | HIGH |
| `0x35F3E90` | — | tcgen05 scaled MMA operands (NVPTX backend copy) | HIGH |
| `0x36E9630` | — | tcgen05.mma full instruction lowering (10 shape variants) | HIGH |
| `0x304E6C0` | — | tcgen05.mma SelectionDAG lowering | HIGH |
| `0x30462A0` | — | tcgen05 infrastructure ops (fence/wait/alloc/dealloc/cp/commit) | HIGH |

## PTX Emission

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x215A3C0` | — | Function header orchestrator (.entry/.func, params, attrs, pragmas) | VERY HIGH |
| `0x214DA90` | — | Kernel attribute emission (.reqntid, .maxntid, cluster, .maxnreg) | VERY HIGH |
| `0x2158E80` | 17KB | Stack frame emission (__local_depot, %SP, %SPL, register decls) | VERY HIGH |
| `0x21583D0` | — | Register class → encoded ID (9 classes, 0x10000000–0x90000000) | HIGH |
| `0x2163730` | — | Register class → PTX type suffix (.pred, .b16, .b32, .b64, .f32, .f64, .b128) | HIGH |
| `0x21638D0` | — | Register class → PTX prefix (%p, %rs, %r, %rd, %f, %fd, %h, %hh, %rq) | HIGH |
| `0x215DC20` | — | GenericToNVVM pass registration ("generic-to-nvvm") | VERY HIGH |
| `0x215E100` | 36KB | GenericToNVVM pass body (addrspace 0→1 rewriting) | HIGH |
| `0x215ACD0` | — | Module emission entry (global ctor rejection, DWARF init) | HIGH |
| `0x2156420` | — | Global variable emission (texref/surfref/samplerref/data) | HIGH |
| `0x21E5E70` | — | Atomic opcode emission (13 ops, scope prefix) | VERY HIGH |
| `0x21E6420` | — | L2 cache-hinted atomic emission (Ampere+) | HIGH |
| `0x21E94F0` | — | Memory barrier emission (membar.cta/gpu/sys, fence.sc.cluster) | HIGH |
| `0x21E8EA0` | — | Cluster barrier emission (arrive/wait + relaxed) | HIGH |
| `0x21E86B0` | — | Special register emission (%tid, %ctaid, %ntid, %nctaid) | VERY HIGH |
| `0x21E9060` | — | Cluster special register emission (15 regs, SM 90+) | HIGH |
| `0x21E7FE0` | — | Address space conversion + MMA helpers (cvta, rowcol, abtype) | HIGH |

## Hash Infrastructure

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0xCBF760` | — | wyhash v4 hash function (multi-length dispatch) | VERY HIGH |
| `0xC92610` | — | Thin wrapper → sub_CBF760 (hash for builtin names) | HIGH |
| `0xC92740` | — | Hash table insert-or-find (quadratic probing, triangular numbers) | VERY HIGH |
| `0xC92860` | — | Hash table find-only (same probing) | HIGH |
| `0xC929D0` | — | Rehash at 75% load factor (double or tombstone cleanup) | HIGH |
| `0xC7D670` | — | String entry allocator (length+17, 8-byte aligned) | HIGH |

## NVVM Builtin Infrastructure

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x90ADD0` | 56 lines | Hash table insertion helper (pre-opt) | VERY HIGH |
| `0x913450` | 27 lines | Builtin dispatcher (pre-opt): name → ID | VERY HIGH |
| `0x12731E0` | 25 lines | Builtin dispatcher (post-opt): name → ID | VERY HIGH |
| `0x955A70` | 103KB | Builtin lowering engine (pre-opt, 3571 lines) | HIGH |
| `0x12B3FD0` | 101KB | Builtin lowering engine (post-opt, 3408 lines) | HIGH |
| `0xC92610` | — | Hash function for builtin names | HIGH |
| `0xC92740` | — | Hash table probe (open addressing) | HIGH |

## Register Allocation

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0xB612D0` | 102KB | Instruction constraint emission (180+ case opcode switch) | HIGH |
| `0x1081400` | 13KB | SimplifyAndColor phase | HIGH |
| `0x1090BD0` | 10KB | SelectNodeForRemoval / Briggs criterion (K=15 at 3 locations) | VERY HIGH |
| `0x12E1EF0` | 11KB | AssignColorsAndOptimize | HIGH |
| `0xA778C0` | — | Operand constraint spec creator (type 14=GPR, 40=FP, 78=vec) | HIGH |
| `0xA78010` | — | Final instruction emitter with allocated registers | HIGH |

## jemalloc (Statically Linked, v5.3.x)

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x4134A7` | 83KB | je\_stats\_print\_arena (per-arena stats, HPA shards) | HIGH |
| `0x40F894` | 37KB | je\_stats\_print\_bins (18 stat columns per bin) | HIGH |
| `0x411419` | 32KB | je\_stats\_general (version, build config, runtime opts) | HIGH |
| `0x417CBD` | 14KB | je\_stats\_print (top-level: allocated, active, resident, mapped) | HIGH |
| `0x40EF06` | 13KB | je\_stats\_print\_large (large extent class stats) | HIGH |
| `0x40D5CA` | 21KB | je\_malloc\_vsnprintf (custom format printer, avoids reentrancy) | HIGH |
| `0x40E5B5` | 7KB | je\_mutex\_stats\_read (mutex profiling counters) | HIGH |
| `0x12FCDB0` | 129KB | je\_malloc\_conf\_init (199 config strings) | VERY HIGH |

## Constructor Highlights

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x456120` | — | ctor\_027: DOES NOT EXIST (phantom, no decompiled file) | DISPROVED |
| `0x4397F0` | ~102KB | ctor\_010\_0: VecFuncs table (960 string xrefs, NOT decompiled) | VERY HIGH |
| `0x4B4360` | ~99KB | ctor\_145: MISSING from decompilation (too large for Hex-Rays) | HIGH |
| `0x5A8850` | ~70KB | ctor\_701: MISSING data blob (likely instruction encoding tables) | MEDIUM |
| `0x4A64D0` | 59KB | ctor\_107\_0: mass cl::opt registration + getenv("bar") | VERY HIGH |
| `0x4A5950` | 7KB | sub\_4A5950: LLVM pass pipeline registration (172 init calls) | VERY HIGH |
| `0x50C890` | 16KB | ctor\_356\_0: SM target registration (45 entries, sm\_20–sm\_121f) | VERY HIGH |
| `0x584510` | 3KB | ctor\_605: NVPTX subtarget processor table (PTX version mapping) | VERY HIGH |
| `0x585D30` | 37KB | ctor\_609\_0: NVPTX backend options (22 knobs) | HIGH |
| `0x5108E0` | 8KB | ctor\_361\_0: nv-remat-\* machine block knobs (11 knobs) | HIGH |
| `0x4F2830` | 10KB | ctor\_262\_0: BranchDist knobs | HIGH |
| `0x4F7BE0` | 7KB | ctor\_277\_0: Remat IR-level knobs (20 knobs) | HIGH |
| `0x4F45B0` | — | ctor\_264: IP-MSP knobs | HIGH |
| `0x593380` | — | ctor\_637\_0: disable-\*Pass flag registration (48 flags) | HIGH |
| `0x48CC90` | 2KB | ctor\_036: LLVM version = "20.0.0" (via LLVM\_OVERRIDE\_PRODUCER fallback) | VERY HIGH |
