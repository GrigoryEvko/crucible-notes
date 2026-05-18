# Function Map

Address-to-identity lookup table. Confidence: VERY HIGH = string evidence, HIGH = strong structural evidence, MEDIUM = inferred from context/callgraph.

## Top Functions by Size

| Function | Address | Size | Confidence |
|---|---|---|---|
| X86 AutoUpgrade (intrinsic rename, leftover from LLVM x86 target) | `0xA939D0` | 457KB | VERY HIGH |
| InstCombine::visitCallInst / visitIntrinsic | `0x10EE7A0` | 396KB | HIGH |
| SelectionDAG LegalizeTypes workhorse (ExpandOp/PromoteOp) | `0x20019C0` | 341KB | HIGH |
| New PassManager pipeline parser (function-level, 268 pass names) | `0x2368220` | 326KB | VERY HIGH |
| EDG constexpr expression evaluator core (124 operator opcodes, 9,075 lines) | `0x786210` | 317KB | VERY HIGH |
| SelectionDAG LegalizeOp main switch | `0x20ACAE0` | 295KB | HIGH |
| SelectionDAGBuilder::visit (IR → DAG) | `0x2081F00` | 261KB | HIGH |
| LLVM IR Verifier (visitCallInst), 298 verification messages | `0xBFC6A0` | 207KB | VERY HIGH |
| X86 Intrinsic Upgrade Helper (broadcastf32x4, compress, etc.) | `0xA8A170` | 195KB | HIGH |
| EDG IL tree walker #1 (297 self-recursive, 87 node types, 305 cases) | `0x7506E0` | 190KB | HIGH |
| EDG declaration specifier parser (393 LABEL\_ gotos, NOT switch/case) | `0x7C0F00` | 184KB | HIGH |
| Bitcode Reader parseFunctionBody, 174 error strings | `0x9F2A40` | 182KB | VERY HIGH |
| EDG constexpr top-level dispatch (80 expression types + 62 intrinsics) | `0x77FCB0` | 150KB | HIGH |
| EDG IL tree copier/transformer (callback params a3/a4, template instantiation) | `0x766570` | 148KB | HIGH |
| SelectionDAG LegalizeTypes dispatch (967 case labels) | `0x1FFB890` | 137KB | HIGH |
| EDG declaration specifier state machine (80 token cases, 4,371 lines) | `0x672A20` | 132KB | VERY HIGH |
| je\_malloc\_conf\_init (199 config strings) | `0x12FCDB0` | 15.4KB | VERY HIGH |
| computeKnownBits / SimplifyDemandedBits | `0x11A7600` | 125KB | VERY HIGH |
| EDG lgenfe\_main (282-case CLI switch, 737 config macros, EDG 6.6) | `0x617BD0` | 123KB | VERY HIGH |
| NVVM Builtin Resolution table (post-opt, 770 entries) | `0x126A910` | 123KB | VERY HIGH |
| NVVMPassOptions init (4,786 lines, 221 slots in 4,512-byte struct) | `0x12D6300` | 125KB | VERY HIGH |
| PassOptionRegistry::lookupOption (hash table at registry+120) | `0x12D6170` | — | HIGH |
| PassOptionRegistry::getBoolOption (triple: '1'/true, 't'/true) | `0x12D6240` | — | HIGH |
| writeStringOption (24-byte entry to output struct) | `0x12D6090` | — | HIGH |
| writeBoolOption (16-byte entry to output struct) | `0x12D6100` | — | HIGH |
| 4-stage pipeline orchestrator (LNK/OPT/OPTIXIR/LLC), nvopt+nvllc objects | `0x12C35D0` | 41KB | VERY HIGH |
| Bitcode linker: triple validation, IR version check, symbol size matching | `0x12C06E0` | 63KB | VERY HIGH |
| NVVM IR version checker (nvvmir.version metadata, NVVM_IR_VER_CHK env) | `0x12BFF60` | 9KB | VERY HIGH |
| NVVM container format parser (arch, FTZ, IEEE, opt level extraction) | `0x12642A0` | — | HIGH |
| Concurrent worker entry (dispatches Phase I/II) | `0x12E7B90` | 3KB | HIGH |
| Concurrent compilation entry (jobserver, thread pool, split-module) | `0x12E1EF0` | 51KB | VERY HIGH |
| Function sorting by priority (insertion sort / introsort) | `0x12E0CA0` | — | HIGH |
| Per-function compilation callback (completion handler) | `0x12E8D50` | — | HIGH |
| Phase II per-function optimizer (sets qword_4FBB3B0=2) | `0x12E86C0` | — | HIGH |
| Concurrency eligibility check (counts defined functions) | `0x12D4250` | — | HIGH |
| GNU Jobserver init (parse MAKEFLAGS, create pipe, spawn pthread) | `0x16832F0` | — | HIGH |
| Bitcode Metadata Reader (parseMetadata) | `0xA09F80` | 121KB | VERY HIGH |
| EDG IL function body processor (14 params, scope stack management) | `0x627530` | 114KB | HIGH |
| EDG IL tree walker #2 (427 self-recursive, parallel traversal) | `0x760BD0` | 109KB | HIGH |
| EDG IL codegen (node type dispatch on byte+80, 2,589 lines) | `0x8BA620` | 108KB | HIGH |
| NVVM Builtin Resolution table (pre-opt, 770 entries) | `0x90AEE0` | 107KB | VERY HIGH |
| NVVM Builtin lowering engine (pre-opt, wgmma/tex/surf, 3571 lines) | `0x955A70` | 103KB | HIGH |
| New PassManager pipeline parser (CGSCC-level) | `0x2377300` | 103KB | HIGH |

## Pipeline Functions

| Function | Address | Size | Confidence |
|---|---|---|---|
| `main()` thunk → `sub_8F9C90` | `0x4396A0` | tiny | KNOWN |
| Real main: CLI parsing, wizard check, dispatch | `0x8F9C90` | 10KB | VERY HIGH |
| Simple compile entry (Path A) | `0x902D10` | — | HIGH |
| Simple compile entry (Path B) | `0x1262860` | — | HIGH |
| LibNVVM pipeline driver (Path A): 14-phase flow, libdevice linking, API dispatch | `0x905EE0` | 43KB | VERY HIGH |
| LibNVVM compilation entry (Path B): 4-stage pipeline, embedded builtins | `0x1265970` | 48KB | VERY HIGH |
| CUDA C++ Front-End stage (lgenfe): timer "CUDA C++ Front-End" | `0x905880` | 6KB | HIGH |
| NVVM IR Container → Module opt setup | `0x9047E0` | 10KB | HIGH |
| Backend SM config + EDG binding, triple construction | `0x908850` | 10KB | HIGH |
| LNK stage verbose callback | `0x903BA0` | 5KB | HIGH |
| LLC stage verbose callback | `0x903730` | 5KB | HIGH |
| CLI processing (Path A): -arch, -maxreg, -split-compile, -gen-lto | `0x900130` | — | HIGH |
| CLI processing (Path B) | `0x125FB30` | — | HIGH |
| EDG master orchestrator (setjmp recovery, timer callbacks) | `0x5D2A80` | 2KB | VERY HIGH |
| Backend entry: "Generating NVVM IR", file output (.int.c/.device.c/.stub.c), TileIR dlopen | `0x5E3AD0` | 11KB | VERY HIGH |
| Multi-stage orchestrator: .lnk.bc → .opt.bc → .ptx | `0x9685E0` | — | HIGH |
| Architecture detection: -arch → triple fan-out | `0x95EB40` | 15KB | VERY HIGH |
| NVVM option parsing (all -opt-*, -llc-*, -gen-*, -Xopt) | `0x9624D0` | — | HIGH |
| Flag mapping table (O0-O3, nvcc flag translation) | `0x8FE280` | — | HIGH |
| LLVM cl::opt bulk registration (~1500 options) | `0xB6EEA0` | — | HIGH |
| Timer/context creation ("CUDA C++ Front-End", "LibNVVM") | `0xC996C0` | — | HIGH |

## EDG 6.6 Frontend

### Core Orchestration

| Function | Address | Size | Confidence |
|---|---|---|---|
| EDG master orchestrator (setjmp recovery, timer callbacks) | `0x5D2A80` | 2KB | VERY HIGH |
| EDG lgenfe\_main (282-case CLI switch, 737 config macros, EDG 6.6) | `0x617BD0` | 123KB | VERY HIGH |
| CLI option registration table (~300 options via sub_6101D0) | `0x610260` | 22KB | HIGH |
| Option fetcher (called in main loop of sub_617BD0) | `0x6140E0` | 6KB | HIGH |
| Backend entry: "Generating NVVM IR", file output (.int.c/.device.c/.stub.c), TileIR dlopen | `0x5E3AD0` | 11KB | VERY HIGH |
| Translation unit init (416-byte TU object, keyword init, parser entry) | `0x8D0BC0` | — | VERY HIGH |
| Semantic analysis init (zeroes 6 globals) | `0x8D0F00` | tiny | HIGH |
| Keyword table init (~350 keywords via sub_885C00) | `0x706250` | 30KB | VERY HIGH |
| TU finalization ("Generating Needed Template Instantiations") | `0x709330` | 5KB | HIGH |
| Register single keyword: `(token_id, "keyword_string")` | `0x885C00` | tiny | HIGH |

### AST-to-Source Printer Cluster

| Function | Address | Size | Confidence |
|---|---|---|---|
| Main expression/statement emitter (61 self-references, recursive) | `0x5DBFC0` | 41KB | HIGH |
| Function declaration printer (\_\_sti\_\_, #pragma section, nv\_linkonce\_odr) | `0x5E13C0` | 44KB | HIGH |
| Statement printer (if/else/for/while/switch/case/return) | `0x5DFD00` | 26KB | HIGH |
| Declaration printer (linkage/storage, \_\_builtin\_va\_alist) | `0x5D9330` | 12KB | HIGH |
| Scope/block printer (bit-fields, array dimensions) | `0x5DA0F0` | 13KB | HIGH |
| Struct/union/enum printer (#pragma pack) | `0x5DAD30` | 9KB | HIGH |
| Variable initializer printer (memcpy, aggregate init) | `0x5D80F0` | 17KB | HIGH |
| Inline asm printer (volatile, constraints, format specifiers) | `0x5DF1B0` | 11KB | HIGH |
| Identifier printer (keyword mangling: auto→\_\_xauto) | `0x5D5A80` | 7KB | HIGH |
| Top-level declaration dispatcher | `0x5DB980` | 7KB | HIGH |
| Function parameter list printer (\_\_text\_\_/\_\_surf\_\_ annotations) | `0x5D7860` | 6KB | HIGH |

### Parser & Declaration Processing

| Function | Address | Size | Confidence |
|---|---|---|---|
| Declaration specifier state machine (while/switch, 80 token cases) | `0x672A20` | 132KB | VERY HIGH |
| Declaration specifier parser (393 LABEL\_ gotos, NOT switch/case) | `0x7C0F00` | 184KB | HIGH |
| Top-level declaration/declarator parser | `0x662DE0` | 61KB | HIGH |
| Overloaded function resolution (\_\_builtin\_ detection, OMP variants) | `0x6523A0` | 64KB | HIGH |
| Struct/union/class specifier processing | `0x66AC40` | 49KB | HIGH |
| Enum specifier processing | `0x66F9E0` | 39KB | HIGH |
| Block-level declaration/statement processor (largest in 0x630000 zone) | `0x63CAE0` | 67KB | HIGH |
| Declaration statement parsing (35 token refs, 14 diagnostics) | `0x661400` | 28KB | HIGH |
| Function declarator processing (parameter lists, return types) | `0x66DF40` | 24KB | HIGH |
| Declaration specifier combination validator | `0x668EE0` | 26KB | HIGH |
| Storage class specifier processor (\_Thread\_local validation) | `0x668230` | 9KB | HIGH |
| Primary declarator-to-IL conversion (type kind dispatch) | `0x6333F0` | 26KB | HIGH |
| Name/identifier processing | `0x64BAA0` | 46KB | HIGH |
| Builtin/intrinsic recognition (53 string refs, C++20/23 reflection) | `0x64A920` | 25KB | HIGH |
| IL function body processor (14 params, scope stack management) | `0x627530` | 114KB | HIGH |
| IL statement processing (16 params, IL walker/transformer) | `0x62C0A0` | 63KB | HIGH |

### Type System

| Function | Address | Size | Confidence |
|---|---|---|---|
| Type conversion checker (recursive, vector type handling) | `0x713ED0` | 36KB | HIGH |
| Binary operation type checker (11 callers — very central) | `0x7115B0` | 17KB | HIGH |
| Usual arithmetic conversions (10 params) | `0x712770` | 12KB | HIGH |
| Type node comparator (parallel tree walk, canonicalization) | `0x7386E0` | 23KB | HIGH |
| Declaration-level type comparison | `0x739430` | 20KB | HIGH |
| Type-to-string emitter (19 callers, backbone of diagnostics) | `0x74A390` | 29KB | VERY HIGH |
| Constant expression emitter (alignof, sizeof, nullptr, zero-init) | `0x748000` | 45KB | HIGH |
| Declarator emitter (19 callers, paired with sub_74A390) | `0x74D110` | 10KB | HIGH |
| Type node deep-copy | `0x73A9D0` | 19KB | HIGH |
| Declaration node deep-copy (192 bytes = 12 x \_\_m128i) | `0x73F780` | 6KB | HIGH |
| Operator overloadability checker | `0x73CC20` | 9KB | HIGH |

### IL Tree Infrastructure

| Function | Address | Size | Confidence |
|---|---|---|---|
| IL tree walker #1 (297 self-recursive, 87 node types, 305 cases) | `0x7506E0` | 190KB | HIGH |
| IL tree walker #2 (427 self-recursive, parallel traversal) | `0x760BD0` | 109KB | HIGH |
| IL tree walker #3 (316 self-recursive) | `0x75C0C0` | 87KB | HIGH |
| IL tree copier/transformer (callback params a3/a4, template instantiation) | `0x766570` | 148KB | HIGH |
| Walker driver/setup (5 callbacks + flags) | `0x759B50` | 31KB | HIGH |
| Copier driver (parallel to sub_759B50) | `0x75B260` | 16KB | HIGH |
| Master walker driver (sets all 6 global callback pointers) | `0x75AFC0` | — | HIGH |

### Constexpr Evaluator

| Function | Address | Size | Confidence |
|---|---|---|---|
| EDG constexpr expression evaluator core (124 operator opcodes, 9,075 lines) | `0x786210` | 317KB | VERY HIGH |
| Statement executor (declarations, loops, switch, compound blocks) | `0x795660` | 77KB | HIGH |
| Object member accessor (base classes, virtual bases, union tracking) | `0x79CCD0` | 67KB | HIGH |
| Aggregate initializer evaluator (arrays, structs, designated init) | `0x799B70` | 33KB | HIGH |
| Function call evaluator (argument binding, recursion limits) | `0x79B7D0` | 29KB | HIGH |
| EDG constexpr top-level dispatch (80 expression types + 62 intrinsics) | `0x77FCB0` | 150KB | HIGH |
| Type size calculator (Robin Hood hash memoization, 64MB cap) | `0x7764B0` | 18KB | HIGH |
| Loop/range-for evaluator | `0x7987E0` | 11KB | HIGH |
| Builtin call evaluator (dispatched from case 0x3D) | `0x77C870` | 18KB | HIGH |
| Aggregate initializer evaluator (struct/array/union at compile time) | `0x77D750` | 34KB | HIGH |

### Preprocessor

| Function | Address | Size | Confidence |
|---|---|---|---|
| Main preprocessor token scanner (all C/C++ token kinds) | `0x7B8B50` | 59KB | HIGH |
| Macro expansion engine (99-entry predefined table, \_\_VA\_OPT\_\_) | `0x81B8F0` | 77KB | HIGH |
| Numeric literal tokenizer (hex float, binary, digit separators) | `0x7B40D0` | 42KB | HIGH |
| Character classification / next-token dispatch (trigraphs, line splices) | `0x7BC390` | 29KB | HIGH |
| String literal scanner (escape processing, raw strings) | `0x7B6B00` | 13KB | HIGH |
| Macro body substitution (\_\_VA\_ARGS\_\_, \_\_VA\_OPT\_\_) | `0x8200E0` | 22KB | HIGH |
| Source character reader / tokenizer bootstrap | `0x7B2B10` | 16KB | HIGH |
| Preprocessing directive dispatcher | `0x7B8270` | 8KB | HIGH |

### Template Engine

| Function | Address | Size | Confidence |
|---|---|---|---|
| Complete template instantiation engine (parameter lists, member iteration) | `0x7A9440` | 40KB | HIGH |
| Template argument type resolution/matching | `0x7410C0` | 42KB | HIGH |
| Template type instantiation handler | `0x743600` | 19KB | HIGH |
| Template instantiation engine (word\_4F06418 SM-arch checks) | `0x5EBF70` | 30KB | HIGH |
| Template argument deduction engine (pattern matching, pack expansion) | `0x5FBCD0` | 38KB | HIGH |

### Semantic Analysis

| Function | Address | Size | Confidence |
|---|---|---|---|
| Deep semantic analysis (29 SM-arch refs, 27 sub\_8D\* calls) | `0x6040F0` | 64KB | HIGH |
| Overload resolution main (43 SM-arch refs — highest) | `0x607B60` | 32KB | HIGH |
| Expression parsing/semantic ("Parsing Lambda", \_\_nv\_parent) | `0x609F00` | 58KB | HIGH |
| Declaration processing (9 SM version refs) | `0x5FE9C0` | 28KB | HIGH |
| Class hierarchy analysis (vtable layout, diamond inheritance) | `0x5F94C0` | 24KB | HIGH |
| Conversion function lookup (33 sub\_8D\* calls) | `0x5F4F20` | 21KB | HIGH |
| Operator overload resolution | `0x5F2920` | 23KB | HIGH |
| Declaration elaboration (type-spec strings "A;P", "O;F", "I", "B") | `0x84EC30` | 71KB | HIGH |
| Declaration semantic analysis (148 global refs, highest density) | `0x8708D0` | 63KB | HIGH |

### CUDA-Specific Frontend

| Function | Address | Size | Confidence |
|---|---|---|---|
| Memory space attribute processing (\_\_shared\_\_, \_\_constant\_\_, \_\_managed\_\_) | `0x6582F0` | 22KB | HIGH |
| Declaration with memory space annotation (15 diagnostic calls) | `0x65F400` | 24KB | HIGH |
| Atomic builtin name generator (\_\_nv\_atomic\_fetch\_\*) | `0x6BBC40` | 34KB | HIGH |
| CUDA device code generation master | `0x804B20` | 28KB | HIGH |
| CUDA registration stub (\_\_cudaRegisterAll, \_\_cudaRegisterEntry) | `0x806F60` | 8KB | VERY HIGH |
| Device stub generator ("\_\_device\_stub\_%s", \_\_cudaLaunch) | `0x808590` | 11KB | HIGH |
| CUDA kernel launch lowering (cudaGetParameterBufferV2) | `0x7F2B50` | 16KB | HIGH |
| Static init with CUDA memory space (\_\_sti\_\_, \_\_constant\_\_) | `0x801880` | 7KB | HIGH |
| Optimization flag configurator (109 flags from O-level) | `0x60D650` | 6KB | HIGH |
| SM-arch feature gate (56 qword\_4F077A8 comparisons) | `0x60E7C0` | 12KB | HIGH |

### Name Mangling (Itanium ABI)

| Function | Address | Size | Confidence |
|---|---|---|---|
| Primary mangling entry | `0x8E74B0` | 29KB | HIGH |
| Type mangling | `0x8E9FF0` | 26KB | HIGH |
| Type component mangling (\_\_real\_\_, \_\_imag\_\_) | `0x816460` | 24KB | HIGH |
| Builtin type mangling (DF16\_, Cu6\_\_bf16, u6\_\_mfp8) | `0x80E340` | 23KB | HIGH |
| NVIDIA extension mangling (Unvdl, Unvdtl, Unvhdl) | `0x80FE00` | 8KB | HIGH |
| Special type mangling (basic\_ostream, allocator substitution) | `0x80C5A0` | 11KB | HIGH |
| Expression mangling | `0x813790` | 13KB | HIGH |

### Diagnostics & Support

| Function | Address | Size | Confidence |
|---|---|---|---|
| Diagnostic emitter (severity labels, ANSI color, word-wrap) | `0x681D20` | 37KB | VERY HIGH |
| SARIF JSON diagnostic output (ruleId, level, locations) | `0x6837D0` | 20KB | HIGH |
| Type name formatter (quoted type names for error messages) | `0x67FCF0` | 40KB | HIGH |
| EDG abort / \_\_builtin\_unreachable (478 callers!) | `0x721090` | tiny | VERY HIGH |
| Exit with status ("Compilation aborted/terminated") | `0x720FF0` | — | HIGH |
| IR node alloc with context (204 callers) | `0x724DC0` | — | HIGH |
| IR node free (196 callers) | `0x724E30` | — | HIGH |
| Get/create void type singleton at qword\_4F07BA8 (145 callers) | `0x72C930` | — | HIGH |
| Arena allocator (63 callers) | `0x7247C0` | — | HIGH |
| IR node hash (polynomial: v10 += ch + 32\*v10, 9 callers) | `0x72DB90` | 8KB | HIGH |
| Tracked heap allocation (linked list at qword\_4F195F8) | `0x822B10` | — | HIGH |
| Hash table bucket chain finalizer | `0x823310` | — | HIGH |
| EDG heap pool allocator (152-byte, 416-byte, etc. entries) | `0x823970` | — | HIGH |

### Class Layout & Vtable

| Function | Address | Size | Confidence |
|---|---|---|---|
| Class layout emitter (\_\_vptr, \_\_v\_, \_\_b\_ prefixes) | `0x7E3EE0` | 7KB | HIGH |
| Virtual base offset calculator | `0x7E57B0` | 9KB | HIGH |
| Virtual call lowering (node\_kind==103) | `0x7E88E0` | 11KB | HIGH |
| Class definition emitter (vtable, nested types, friends) | `0x7E9AF0` | 13KB | HIGH |
| Statement emission mega-function (largest in class layout zone) | `0x7EE560` | 45KB | HIGH |
| Class member emission (\_\_cxa\_atexit, \_\_cxa\_vec\_cctor) | `0x7FEC50` | 48KB | HIGH |
| Function definition emission (ctor initializers, default args) | `0x7FCF80` | 17KB | HIGH |

## LLVM cl::opt Registration Infrastructure

| Function | Address | Size | Confidence |
|---|---|---|---|
| Global option counter (atomic increment) | `0xC523C0` | — | HIGH |
| cl::Option::setArgStr(name, len) — Legacy PM | `0xC53080` | — | HIGH |
| cl::Option::addArgument() — Legacy PM | `0xC53130` | — | HIGH |
| cl::OptionCategory getter | `0xC57470` | — | HIGH |
| cl::opt name setter — New PM | `0x16B8280` | — | HIGH |
| cl::opt finalization — New PM | `0x16B88A0` | — | HIGH |
| SmallVector::grow() | `0xC8D5F0` | — | HIGH |

### Key Constructors (cl::opt registration)

| Function | Address | Size | Confidence |
|---|---|---|---|
| ctor\_010\_0: TargetLibraryInfo VecFuncs table (9 vector math libs, 960 string xrefs, NOT decompiled) | `0x4397F0` | ~102KB | VERY HIGH |
| ctor\_027: DOES NOT EXIST (phantom, no decompiled file) | `0x456120` | — | DISPROVED |
| ctor\_036: LLVM version = "20.0.0" (via LLVM\_OVERRIDE\_PRODUCER fallback) | `0x48CC90` | 2KB | VERY HIGH |
| ctor\_043\_0: NVIDIA CICC-specific options (19 opts, XOR cipher hidden flag) | `0x48D7F0` | 30KB | VERY HIGH |
| MASTER pass/analysis registration (~172 init calls) | `0x4A5950` | 7KB | VERY HIGH |
| ctor\_107\_0: MC/Target options (131 opts, getenv("bar") backdoor) | `0x4A64D0` | 59KB | VERY HIGH |
| ctor\_133\_0: Known library function table (422 C/POSIX functions) | `0x4B0180` | 29KB | VERY HIGH |
| ctor\_145: MISSING from decompilation (too large for Hex-Rays) | `0x4B4360` | ~99KB | HIGH |
| ctor\_147\_0: PassManager debug/print options | `0x4CC760` | 20KB | HIGH |
| ctor\_156\_0: CLI infrastructure (help, version, print-options) | `0x4CEB50` | 9KB | HIGH |
| ctor\_186\_0: Inliner heuristics (NVIDIA: profuseinline, inline-budget) | `0x4DBEC0` | 14KB | HIGH |
| ctor\_201: GVN options (NVIDIA: profusegvn, gvn-dom-cache) | `0x4E0990` | 9KB | HIGH |
| ctor\_214\_0: LSR options (NVIDIA: disable-lsr-for-sharedmem32-ptr) | `0x4E4B00` | 8KB | HIGH |
| ctor\_216\_0: Loop Unrolling options (largest unroll ctor) | `0x4E5C30` | 21KB | HIGH |
| ctor\_259\_0: CICC core compiler options (debug-compile, maxreg) | `0x4F0FB0` | 17KB | HIGH |
| ctor\_262\_0: BranchDist pass options | `0x4F2830` | 10KB | HIGH |
| ctor\_263\_0: SCEV-CGP pass options (44 strings!) | `0x4F36F0` | 10KB | HIGH |
| ctor\_264: IP-MSP knobs | `0x4F45B0` | — | HIGH |
| ctor\_267\_0: MemorySpaceOpt options (18 strings) | `0x4F54D0` | 10KB | HIGH |
| ctor\_277\_0: Rematerialization options (39 strings, remat-for-occ) | `0x4F7BE0` | 7KB | HIGH |
| ctor\_335\_0: MASTER codegen pass configuration (88 strings) | `0x507310` | 29KB | VERY HIGH |
| ctor\_356\_0: NVPTX SM enum + PTX version table (45 entries, sm\_20–sm\_121f) | `0x50C890` | 16KB | VERY HIGH |
| ctor\_358\_0: NVPTX pass enable/disable (43 strings, usedessa) | `0x50E8D0` | 21KB | HIGH |
| ctor\_361\_0: NV Remat Machine Block options (30 strings, nv-remat-\*) | `0x5108E0` | 8KB | HIGH |
| ctor\_376\_0: LTO/bitcode/plugin options | `0x512DF0` | 39KB | HIGH |
| ctor\_377\_0: PassBuilder pipeline configuration (77 strings) | `0x516190` | 44KB | HIGH |
| ctor\_388\_0: Optimizer pipeline enables (enable-ml-inliner, etc.) | `0x51B710` | 15KB | HIGH |
| ctor\_600\_0: CodeGen/TargetMachine mega-options (118 strings) | `0x57F210` | 59KB | HIGH |
| ctor\_605: SM processor table (45 entries, sm\_20–sm\_121f, PTX version map) | `0x584510` | 3KB | VERY HIGH |
| ctor\_609\_0: NVPTX backend options (25+ opts, usedessa, enable-nvvm-peephole) | `0x585D30` | 37KB | HIGH |
| ctor\_637\_0: disable-\*Pass flag registration (48 flags) | `0x593380` | — | HIGH |
| ctor\_701: MISSING data blob (likely instruction encoding tables) | `0x5A8850` | ~70KB | MEDIUM |

## NVIDIA Custom Pass Implementations

| Function | Address | Size | Confidence |
|---|---|---|---|
| MemorySpaceOptPass registration | `0x2CDD6D0` | reg | HIGH |
| MemorySpaceOptPass factory | `0x2CDFF20` | factory | HIGH |
| MemorySpaceOpt core analysis | `0x2CDA660` | 10KB | HIGH |
| MemorySpaceOpt address space inference | `0x2CD7710` | 9KB | HIGH |
| IPMSPPass (interprocedural memory space) registration | `0x1C6FBC0` | reg | HIGH |
| RematerializationPass (IR-level) implementation | `0x1CE7DD0` | 13KB | HIGH |
| Machine Block Rematerialization | `0x2186D90` | 9KB | HIGH |
| BranchDistPass registration | `0x1C4B520` | reg | HIGH |
| LoopIndexSplitPass implementation | `0x1C7B2C0` | 11KB | HIGH |
| NVVMPeepholeOptimizerPass registration | `0x2CAF0F0` | reg | HIGH |
| ByValMem2RegPass | `0x2CD6510` | 350B | HIGH |
| BasicDeadBarrierEliminationPass | `0x2CD2690` | 366B | HIGH |
| CNPLaunchCheckPass (Dynamic Parallelism validation) | `0x1CEBC30` | reg | HIGH |
| PrintfLoweringPass | `0x1CB0B80` | name | HIGH |
| Pass registration master function (all 402+20 passes) | `0x2342890` | 32KB | VERY HIGH |
| Pass name listing (pipeline names for all passes) | `0x233C410` | — | HIGH |

## MMA / Tensor Core Emission

| Function | Address | Size | Confidence |
|---|---|---|---|
| MMA instruction operand builder (shapes, types, rounding modes) | `0x21E74C0` | 17KB | VERY HIGH |
| tcgen05 Blackwell scaled MMA operands (scaleD, negA, negB, transA) | `0x21E8CD0` | 2KB | VERY HIGH |
| HMMA store-C (hmmastc), SM ≥ 70 | `0x21DFBF0` | 5KB | HIGH |
| HMMA load-A/B (hmmaldab), SM ≥ 70 | `0x21E0360` | 3KB | HIGH |
| HMMA load-C (hmmaldc), SM ≥ 70 | `0x21E0630` | 3KB | HIGH |
| HMMA MMA (hmmamma), SM ≥ 70 | `0x21E0870` | 4KB | HIGH |
| IMMA load-A/B (immaldab), SM ≥ 72 | `0x21E1280` | 4KB | HIGH |
| IMMA load-C (immaldc), SM ≥ 72 | `0x21E15D0` | 3KB | HIGH |
| IMMA store-C, SM ≥ 72 | `0x21E1830` | 5KB | HIGH |
| IMMA MMA w/ saturation (immamma), SM ≥ 72 | `0x21E1D20` | 6KB | HIGH |
| Binary MMA (bmmamma, b1 .and.popc/.xor.popc), SM ≥ 75 | `0x21E2280` | 6KB | HIGH |
| MMA address-space resolver (opcode → addrspace enum) | `0x21DEF90` | — | HIGH |
| tcgen05 scaled MMA operands (NVPTX backend copy) | `0x35F3E90` | — | HIGH |
| tcgen05.mma full instruction lowering (10 shape variants) | `0x36E9630` | — | HIGH |
| tcgen05.mma SelectionDAG lowering | `0x304E6C0` | — | HIGH |
| tcgen05 infrastructure ops (fence/wait/alloc/dealloc/cp/commit) | `0x30462A0` | — | HIGH |

## PTX Emission

| Function | Address | Size | Confidence |
|---|---|---|---|
| Function header orchestrator (.entry/.func, params, attrs, pragmas) | `0x215A3C0` | — | VERY HIGH |
| Kernel attribute emission (.reqntid, .maxntid, cluster, .maxnreg) | `0x214DA90` | — | VERY HIGH |
| Stack frame emission (__local_depot, %SP, %SPL, register decls) | `0x2158E80` | 17KB | VERY HIGH |
| Register class → encoded ID (9 classes, 0x10000000–0x90000000) | `0x21583D0` | — | HIGH |
| Register class → PTX type suffix (.pred, .b16, .b32, .b64, .f32, .f64, .b128) | `0x2163730` | — | HIGH |
| Register class → PTX prefix (%p, %rs, %r, %rd, %f, %fd, %h, %hh, %rq) | `0x21638D0` | — | HIGH |
| GenericToNVVM pass registration ("generic-to-nvvm") | `0x215DC20` | — | VERY HIGH |
| GenericToNVVM pass body (addrspace 0→1 rewriting) | `0x215E100` | 36KB | HIGH |
| Module emission entry (global ctor rejection, DWARF init) | `0x215ACD0` | — | HIGH |
| Global variable emission (texref/surfref/samplerref/data) | `0x2156420` | — | HIGH |
| Atomic opcode emission (13 ops, scope prefix) | `0x21E5E70` | — | VERY HIGH |
| L2 cache-hinted atomic emission (Ampere+) | `0x21E6420` | — | HIGH |
| Memory barrier emission (membar.cta/gpu/sys, fence.sc.cluster) | `0x21E94F0` | — | HIGH |
| Cluster barrier emission (arrive/wait + relaxed) | `0x21E8EA0` | — | HIGH |
| Special register emission (%tid, %ctaid, %ntid, %nctaid) | `0x21E86B0` | — | VERY HIGH |
| Cluster special register emission (15 regs, SM 90+) | `0x21E9060` | — | HIGH |
| Address space conversion + MMA helpers (cvta, rowcol, abtype) | `0x21E7FE0` | — | HIGH |

## Hash Infrastructure

| Function | Address | Size | Confidence |
|---|---|---|---|
| wyhash v4 hash function (multi-length dispatch) | `0xCBF760` | — | VERY HIGH |
| Thin wrapper → sub_CBF760 (hash for builtin names) | `0xC92610` | — | HIGH |
| Hash table insert-or-find (quadratic probing, triangular numbers) | `0xC92740` | — | VERY HIGH |
| Hash table find-only (same probing) | `0xC92860` | — | HIGH |
| Rehash at 75% load factor (double or tombstone cleanup) | `0xC929D0` | — | HIGH |
| String entry allocator (length+17, 8-byte aligned) | `0xC7D670` | — | HIGH |

## NVVM Builtin Infrastructure

| Function | Address | Size | Confidence |
|---|---|---|---|
| Hash table insertion helper (pre-opt) | `0x90ADD0` | 56 lines | VERY HIGH |
| Builtin dispatcher (pre-opt): name → ID | `0x913450` | 27 lines | VERY HIGH |
| Builtin dispatcher (post-opt): name → ID | `0x12731E0` | 25 lines | VERY HIGH |
| Builtin lowering engine (pre-opt, wgmma/tex/surf, 3571 lines) | `0x955A70` | 103KB | HIGH |
| Builtin lowering engine (post-opt, 3408 lines) | `0x12B3FD0` | 101KB | HIGH |

## Register Allocation

| Function | Address | Size | Confidence |
|---|---|---|---|
| Instruction constraint emission (180+ case opcode switch) | `0xB612D0` | 102KB | HIGH |
| SimplifyAndColor phase | `0x1081400` | 13KB | HIGH |
| SelectNodeForRemoval / Briggs criterion (K=15 at 3 locations) | `0x1090BD0` | 10KB | VERY HIGH |
| AssignColorsAndOptimize (address unverified, was erroneously listed as 0x12E1EF0) | `0x10841C0` | 11KB | MEDIUM |
| Operand constraint spec creator (type 14=GPR, 40=FP, 78=vec) | `0xA778C0` | — | HIGH |
| Final instruction emitter with allocated registers | `0xA78010` | — | HIGH |

## jemalloc (Statically Linked, v5.3.x)

| Function | Address | Size | Confidence |
|---|---|---|---|
| je\_stats\_print\_arena (per-arena stats, HPA shards) | `0x4134A7` | 83KB | HIGH |
| je\_stats\_print\_bins (18 stat columns per bin) | `0x40F894` | 37KB | HIGH |
| je\_stats\_general (version, build config, runtime opts) | `0x411419` | 32KB | HIGH |
| je\_stats\_print (top-level: allocated, active, resident, mapped) | `0x417CBD` | 14KB | HIGH |
| je\_stats\_print\_large (large extent class stats) | `0x40EF06` | 13KB | HIGH |
| je\_malloc\_vsnprintf (custom format printer, avoids reentrancy) | `0x40D5CA` | 21KB | HIGH |
| je\_mutex\_stats\_read (mutex profiling counters) | `0x40E5B5` | 7KB | HIGH |
| je\_malloc\_conf\_init (199 config strings) | `0x12FCDB0` | 15.4KB | VERY HIGH |

## Optimizer Pipeline Assembly

Functions discovered during wiki writing (W101--W241). These assemble the LLVM optimization pipeline from NVVMPassOptions slots.

### Pipeline Builders

| Function | Address | Size | Confidence |
|---|---|---|---|
| Master pipeline assembler (reads opts struct, ~150 pass-insertion decisions) | `0x12E54A0` | 50KB | VERY HIGH |
| Tier 0 full optimization sub-pipeline (~40 passes, base for O1/O2/O3) | `0x12DE330` | — | VERY HIGH |
| Tier 1/2/3 phase-specific sub-pipeline (phase-conditional pass insertion) | `0x12DE8F0` | — | VERY HIGH |
| Codegen pass dispatch (reads opts[200] optimization threshold) | `0x12DFE00` | 20.7KB | HIGH |
| OPT stage two-phase orchestrator (sets qword\_4FBB3B0 to 1 or 2) | `0x12E7E70` | — | VERY HIGH |
| New-PM driver: pipeline name selector (O0/O1/O2/O3/Ofcmin/Ofcmid/Ofcmax) | `0x226C400` | — | HIGH |
| NVPTXTargetMachine creation (NVIDIA options, standalone path) | `0x12F4060` | 16KB | HIGH |
| OptiX IR generation core function | `0x12F9270` | ~6KB | HIGH |

### Pass Factories (Pipeline Insertion Order)

Each factory creates a pass instance; referenced from `sub_12E54A0`, `sub_12DE330`, and `sub_12DE8F0`.

| Function | Address | Size | Confidence |
|---|---|---|---|
| NVVMReflect factory (~8 pipeline insertions) | `0x1857160` | — | HIGH |
| SCCP factory | `0x1842BC0` | — | HIGH |
| NVVMVerifier wrapper (creates context, invokes module verifier) | `0x12D4560` | — | HIGH |
| NVVMPredicateOpt factory (AggressiveInstCombine variant) | `0x18A3430` | — | HIGH |
| NVVMPredicateOpt variant / LoopRotate factory | `0x18A3090` | — | HIGH |
| ConstantMerge / GlobalDCE / LICM factory | `0x184CD60` | — | HIGH |
| FunctionAttrs factory (infers readonly, nounwind, etc.) | `0x1841180` | — | HIGH |
| LICM factory (parameter 0 = standard mode) | `0x195E880` | — | HIGH |
| LoopVectorize/SLP factory (7 params: width, thresholds) | `0x19B73C0` | — | HIGH |
| CGSCC standard pipeline factory (InlinerWrapper, 1--5 iterations) | `0x1A62BF0` | — | HIGH |
| PrintModulePass factory (debug dump, params: level, verbose) | `0x17060B0` | — | HIGH |
| JumpThreading / CVP factory (parameter: threshold) | `0x198DF00` | — | HIGH |
| EarlyCSE factory | `0x196A2B0` | — | HIGH |
| SROA factory | `0x1968390` | — | HIGH |
| DCE (DeadCodeElimination) factory | `0x18DEFF0` | — | HIGH |
| Sink/MemSSA factory (3 params: mode, flags) | `0x1869C50` | — | HIGH |
| NVVMLoopOpt/BarrierOpt / IV Demotion factory | `0x18B1DE0` | — | HIGH |
| NVVMIntrinsicLowering factory (level 0 = basic, level 1 = barrier) | `0x1CB4E40` | — | HIGH |
| MemCpyOpt factory | `0x1B26330` | — | HIGH |
| LoopUnroll / SpeculativeExecution factory (2 params) | `0x19C1680` | — | HIGH |
| ADCE (AggressiveDeadCodeElimination) factory | `0x1C76260` | — | HIGH |
| ADCE variant factory (separate pipeline position) | `0x1C6FCA0` | — | HIGH |
| SimplifyCFG factory (2 params: mode, flags) | `0x190BB10` | — | HIGH |
| InstructionSimplify factory | `0x1A7A9F0` | — | HIGH |
| NVVMRematerialization factory (IR-level) | `0x1A13320` | — | HIGH |
| Reassociate factory (parameter: tier) | `0x1B7FDF0` | — | HIGH |
| LoopStrengthReduce factory | `0x19CE990` | — | HIGH |
| NVVMBranchDist factory (two pipeline positions) | `0x1CB73C0` | — | HIGH |
| NVVMSinking2 factory (SM-specific late sinking) | `0x1CC60B0` | — | HIGH |
| NVVMGenericAddrOpt factory (generic address optimization) | `0x1CC71E0` | — | HIGH |
| NVVMReduction factory (SM-specific) | `0x1CC5E00` | — | HIGH |
| NVVMUnreachableBlockElim factory | `0x1CC3990` | — | HIGH |
| NVVMLateOpt factory (Tier 3 only) | `0x1C46000` | — | HIGH |
| NVVMLowerAlloca factory (dual gate: opts[2240] + opts[2280]) | `0x1CBC480` | — | HIGH |
| NVVMLowerBarriers factory (runs between LICM invocations) | `0x1C98160` | — | HIGH |
| Sinking2Pass fast-mode factory (flag=1, Ofcmin pipeline) | `0x18B3080` | — | HIGH |
| VerifierPass factory (late CFG cleanup guard at opts[4464]) | `0x1654860` | — | HIGH |
| NVIDIA loop pass factory (opts[3080] guard) | `0x1922F90` | — | MEDIUM |
| EarlyCSE MemorySSA variant / NVVMBarrierAnalysis factory | `0x18E4A00` | — | HIGH |
| EarlyCSE variant (v=1 if opts[3704]) | `0x1C8A4D0` | — | HIGH |
| NVVMAnnotationsProcessor factory | `0x215D9D0` | — | HIGH |
| NVIDIA Custom Inliner (CGSCC, 20,000-unit per-caller budget) | `0x1864060` | 75KB | VERY HIGH |

## NVPTX Backend (SelectionDAG & ISel)

| Function | Address | Size | Confidence |
|---|---|---|---|
| NVPTXTargetLowering::LowerIntrinsicCall (largest function in binary) | `0x33B0210` | 343KB | VERY HIGH |
| NVPTXDAGToDAGISel::Select (ISel entry, hash-based cost table) | `0x3090F90` | 91KB | VERY HIGH |
| computeKnownBitsForTargetNode (112 opcodes, 399x sub\_969240 calls) | `0x33D4EF0` | 114KB | HIGH |
| NVPTXTargetLowering::LowerCall (PTX `.param` calling convention) | `0x3040BF0` | 88KB | HIGH |
| LLVM standard InlineCostAnalysis (library function) | `0x30DC7E0` | 51KB | HIGH |
| Vector legalization type-split record mapping | `0x3302A00` | — | HIGH |
| Operand type classifier (reads byte\_444C4A0) | `0x34961A0` | 26.6KB | HIGH |

## NVVM Verifier Subsystem

| Function | Address | Size | Confidence |
|---|---|---|---|
| NVVMModuleVerifier (data layout, address space, triple validation) | `0x2C80C90` | 51KB | HIGH |
| NVVMIntrinsicVerifier (SM gates, types, MMA, atomics, tex/surf) | `0x2C7B6A0` | 143KB | VERY HIGH |
| Frontend verifier (convergent intrinsic SM-version gating) | `0x1C36530` | — | HIGH |
| NVVMIntrinsicLowering core engine (2,460 lines) | `0x2C63FB0` | 140KB | HIGH |

## LTO Subsystem

| Function | Address | Size | Confidence |
|---|---|---|---|
| NVModuleSummary builder (ThinLTO, two-phase declaration merge) | `0xD7D4E0` | 74KB | HIGH |
| New PM CGSCC inliner (inside LazyCallGraph framework) | `0x2613930` | 69KB | HIGH |
| IP-MSP module-pass variant (LIBNVVM path, DenseMap-based) | `0x1C6A6C0` | 54KB | HIGH |
| LinkUserModules (wrapper around LLVM Linker::linkModules) | `0x12F5610` | ~4KB | HIGH |

## LLVM IR Utility Functions

Common LLVM IR manipulation functions referenced across many passes.

| Function | Address | Size | Confidence |
|---|---|---|---|
| operator new / BumpPtrAllocator (SDNode, BasicBlock, pass objects) | `0x22077B0` | — | HIGH |
| Value::replaceAllUsesWith / salvageDebugInfo | `0xBD84D0` | — | HIGH |
| Instruction::eraseFromParent / SDUse remove from use list | `0xB43D60` | — | HIGH |
| getCalledFunction / BranchInst::getCondition | `0xB43CB0` | — | HIGH |
| Function::hasAttribute(N) (noimplicitfloat, optnone, convergent) | `0xB2D610` | — | HIGH |
| Function::getName / IR node name getter | `0xBD5D20` | — | HIGH |
| PHINode::Create / SDNode alloc variant (80 bytes) | `0xBD2DA0` | — | HIGH |
| hasAttribute(26) (convergent/varargs marker check) | `0xB91C10` | — | HIGH |
| TTI::getInstructionCost (IR-level) / MDString::getString | `0xB91420` | — | HIGH |
| Ref-count decrement on metadata/debug-info | `0xB91220` | — | HIGH |
| Ref-count increment on metadata/debug-info | `0xB96E90` | — | HIGH |
| Value::setName / SetValueName (assigns %name to IR value) | `0x164B780` | — | HIGH |
| IRBuilder::CreateBinOp / SCEV type extension (349x callers) | `0x1623A60` | — | HIGH |
| ReleaseDebugLoc / debug location list removal | `0x161E7C0` | — | HIGH |
| Fatal error emitter ("Broken module found, compilation aborted!") | `0x16BD130` | — | HIGH |
| Create binary OR instruction (opcode 27) | `0x15FB440` | — | HIGH |
| DataLayout::getPointerSizeInBits(addressSpace) | `0x15A9520` | — | HIGH |
| DataLayout::getStructLayout (struct size computation) | `0x15A9930` | — | HIGH |
| SCEV fold/normalize / NVVM AA address-space NoAlias query | `0x146F1B0` | — | HIGH |
| CombineTo / ReplaceAllUsesWith (DAG use-chain + worklist push) | `0xF162A0` | — | HIGH |
| Function cloner (coroutine resume/destroy) | `0xD2E510` | — | HIGH |
| Create runtime library call instruction (OpenMP, MMA, barriers) | `0x921880` | — | HIGH |
| Builtin function call emitter (pre-opt path, EDG builtins) | `0x1285290` | — | HIGH |
| Kernel metadata emitter (cluster\_dim, blocksareclusters) | `0x93AE30` | ~5.6KB | HIGH |
| ExpandIntegerResult (type legalization, 632 case labels) | `0x201BB90` | 75KB | HIGH |

## Machine-Level Infrastructure

| Function | Address | Size | Confidence |
|---|---|---|---|
| InstrEmitter DenseMap grow / rehash (hash: key\*37) | `0x2E29BA0` | — | HIGH |
| TwoAddressInstruction DenseMap (SrcEqClassMap) | `0x1F4E3A0` | — | HIGH |
| GlobalISel MachineVerifier::visitMachineInstrBefore (G_PHI/G_VSCALE/G_ASSERT_ZEXT type/operand checks, 60+ generic-MIR error strings) | `0x2EF30A0` | 18KB | HIGH |
| NVPTX getTargetNodeName / SDNode opname dispatch (6,634-case switch over SDNode::getOpcode, emits every PTX operand keyword: `mmarowcol`, `scaleD`, `cta_group`, `parity_op`) | `0x35F6D40` | 875KB | VERY HIGH |

## Dark-Continent Backend Functions

Large NVPTX/SelectionDAG functions with no obvious string anchors — identified by callgraph position and address neighborhood. Add to dedicated pages as evidence accrues.

| Function | Address | Size | Best-guess role | Confidence |
|---|---|---|---|---|
| Module-level New-PM pipeline parser (`PassBuilder::parseModulePass`, 335 pass-name string xrefs incl. `loop-simplify`, `wasm-eh-prepare`, `print<memoryssa>`, `cannot have a no-rerun module-to-function adaptor`) | `0x2382460` | 74KB | NewPM module-level counterpart of `sub_2368220`/`sub_2377300` | HIGH |
| LLVM Verifier::visitCallInst-style validator (cleanup-funclet, convergencectrl, GC personality checks; "external or indirect", "unsupported operand bundle") | `0x29ED7A0` | 20KB | IR call-site verifier, sibling of `sub_BFC6A0` for call instructions | HIGH |
| SelectionDAG LegalizeOp recursive visitor (22 self-calls, calls SCEV/DAG ops cluster `sub_1D332F0`) | `0x20A2AF0` | 25KB | DAGCombiner / LegalizeVectorOps inner visitor | MED |
| NVPTX SDNode helper #1 ("getVectorNumElements scalable-vector" warning, callees in `0x2D43050`/`0x33CB110` ISel-cost cluster) | `0x333BF30` | 55KB | NVPTX ISel cost/legalization scoring helper | MED |
| NVPTX SDNode helper #2 (same scalable-vector warnings, sibling of `sub_333BF30`, smaller fan-out) | `0x3333CD0` | 25KB | NVPTX ISel legalization helper, sibling of `sub_333BF30` | MED |
| Machine-pass recursive structural walker (14 self-calls, in `0x2B00000` codegen region near `sub_2B71BE0`) | `0x2BA65A0` | 18KB | LiveInterval / RegAlloc graph walker | LOW |
| Machine-codegen self-recursive analyzer (`sub_2B9A8C0` and tail `sub_2B9A850`) | `0x2B9A8C0` | 21KB | MachineFunction/MIR analysis pass, paired with `sub_2BA65A0` | LOW |
| LLVM IR attribute-keyword scanner (`noinline`, `nonnull`, `zeroext`, `extern_weak`, `fastcc`, `anyregcc` etc., parent `sub_1205200`) | `0x11FF4E0` | 24KB | AsmParser attribute-keyword lookup table, paired with bitcode reader | HIGH |
| LLVM AutoUpgrade intrinsic-name table (`avx512.mask.cmp.`, `clz.ll`, `popc.ll`, `sse2.add.sd`) | `0x156E800` | 64KB | NVPTX/LLVM IR intrinsic name-rewrite table (sibling of x86 `sub_A939D0`) | HIGH |
| LLVM AutoUpgrade x86 SSE/AVX intrinsic renamer (`sse41.ptest`, `sse4a.movnt`, `avx.dp.ps.256`) | `0x15644B0` | 19KB | x86 intrinsic upgrade helper, leftover from generic LLVM AutoUpgrade | HIGH |
| ThinLTO summary `.dot` dumper ("digraph Summary {", "// Cross-module edges:", "// Module:") | `0xBB0D30` | 17KB | Module summary index DOT-graph printer | HIGH |
| New-PM CGSCC/loop-pass inliner cluster member (in `0x1A00000` PassBuilder zone) | `0x1ADC640` | 19KB | PassBuilder helper near `sub_186CA00`/`sub_189A480` | LOW |
| InstCombine comprehensive folder helper #1 (parent `sub_1205200`-class, FuncAttr-aware) | `0x1136650` | 23KB | InstCombine internal folder (sibling of main `sub_10EE7A0`) | MED |
| Function-attribute string→bitset parser (`denormal-fp-math`, `frame-pointer`, `cpu-name`, `mattr`, `a1,+a2,-a3,...`) | `0x2D97F20` | 30KB | TargetMachine option/feature decoder | HIGH |

