# Binary Layout

cudafe++ ships as a single statically-linked, stripped ELF 64-bit x86-64 executable. Static linking pulls in the entirety of libstdc++ (locale facets, iostream, exception handling), Berkeley SoftFloat 3e (half/quad-precision arithmetic), and glibc CRT startup code. The resulting 8.5 MB binary has no external shared library dependencies -- it runs identically on any Linux x86-64 host regardless of installed C++ runtime version.

This page documents the complete segment and section layout, the internal organization of each major section, and the key data structures located within each region. All addresses are virtual addresses from the ELF load image.

## ELF Header

| Property | Value |
|---|---|
| Format | ELF 64-bit LSB executable |
| Architecture | x86-64 (AMD64) |
| Linking | Statically linked |
| Stripped | Yes (no debug symbols, no .symtab) |
| File size | 8,910,936 bytes (8.5 MB) |
| Entry point | `0x40918C` (`_start`, glibc CRT) |
| Main | `0x408950` |

## Complete Section Table

| Section | Start | End | Size (bytes) | Size (human) | Permissions | Purpose |
|---|---|---|---|---|---|---|
| LOAD (ELF hdr) | `0x400000` | `0x402A18` | 10,776 | 10.5 KB | r-x | ELF headers and program header table |
| `.init` | `0x402A18` | `0x402A30` | 24 | 24 B | r-x | Initialization stub (calls `init_proc`) |
| `.plt` | `0x402A30` | `0x403300` | 2,256 | 2.2 KB | r-x | Procedure Linkage Table (141 entries) |
| **`.text`** | **`0x403300`** | **`0x829722`** | **4,351,010** | **4.15 MB** | **r-x** | **All executable code** |
| `.fini` | `0x829724` | `0x829732` | 14 | 14 B | r-x | Finalization stub (empty body) |
| **`.rodata`** | **`0x829740`** | **`0xAA3FA3`** | **2,599,011** | **2.48 MB** | **r--** | **Read-only data** |
| `.eh_frame_hdr` | `0xAA3FA4` | `0xAB0350` | 50,092 | 48.9 KB | r-- | Exception frame header index |
| `.eh_frame` | `0xCB1210` | `0xD3F398` | 582,024 | 568.4 KB | rw- | Exception unwind tables (CFI) |
| `.gcc_except_table` | `0xD3F398` | `0xD42854` | 13,500 | 13.2 KB | rw- | GCC LSDA exception handler tables |
| `.ctors` | `0xD42858` | `0xD428B0` | 88 | 88 B | rw- | Constructor table (9 function pointers + 2 sentinels) |
| `.dtors` | `0xD428B0` | `0xD428C0` | 16 | 16 B | rw- | Destructor table (2 sentinels, empty) |
| `.data.rel.ro` | `0xD428C0` | `0xD45E00` | 13,632 | 13.3 KB | rw- | Vtables and relocation-read-only data |
| `.got` | `0xD45FC0` | `0xD45FF8` | 56 | 56 B | rw- | Global Offset Table |
| `.got.plt` | `0xD46000` | `0xD46478` | 1,144 | 1.1 KB | rw- | GOT for PLT entries |
| **`.data`** | **`0xD46480`** | **`0xE7EFF0`** | **1,280,880** | **1.22 MB** | **rw-** | **Initialized globals** |
| **`.bss`** | **`0xE7F000`** | **`0x12D6F20`** | **4,554,528** | **4.34 MB** | **rw-** | **Zero-initialized globals** |
| `.tls` | `0x12D6F20` | `0x12D6F38` | 24 | 24 B | --- | Thread-local storage (exception state) |
| `extern` | `0x12D6F38` | `0x12D73A8` | 1,136 | 1.1 KB | --- | External symbol stubs |

Total virtual address space consumed: `0x12D73A8 - 0x400000` = 18.9 MB.

## .text -- Executable Code (4.15 MB)

The `.text` section contains all 6,501 functions in the binary. It divides into four distinct regions, laid out contiguously by the linker:

```
0x403300                                                          0x829722
|-- assert stubs --|-- ctors --|---- EDG main body ----|-- C++ runtime ----|
0x403300    0x408B40  0x409350                  0x7DF400          0x829722
   34 KB      8 KB              3.61 MB                  304 KB
```

### Assert Stub Region (0x403300 -- 0x408B40, 34 KB)

Contains 235 small `__noreturn` functions, each encoding a single assertion site. Every stub loads three string constants -- source file path, line number, and function name -- then calls `sub_4F2930` (the `internal_error` handler in `error.c`). These stubs are called from the bodies of larger functions when an impossible condition is detected.

The linker groups all stubs from all 52 `.c` source files into this contiguous block, sorted approximately by source file name. Of the 235 stubs:

- 200 map to `.c` source files (e.g., `attribute.c:10897` at `0x403300`, `cp_gen_be.c:22342` at `0x4036F6`)
- 35 map to `.h` header files inlined into `.c` compilation units (e.g., `types.h` at `0x40345C`)

Each stub is exactly 29 bytes: a `lea` for the file path, a `mov` for the line number, a `lea` for the function name, then a `call` to `sub_4F2930`.

### Constructor Region (0x408B40 -- 0x409350, 8 KB)

Contains 9 C++ global constructor functions (`ctor_001` through `ctor_009`) registered in the `.ctors` table. These run before `main()` via `__libc_start_main`'s init callback at `0x829640`. The constructors, in execution order:

| Constructor | Address | Identity | What It Initializes |
|---|---|---|---|
| `ctor_001` | `0x408B40` | EDG diagnostic list | Doubly-linked list at `E7FE40..E7FE68` (self-referencing empty sentinel) |
| `ctor_002` | `0x408B90` | Stream state table | 13 qwords at `126ED80..126EDE0` (output channel array including `126EDF0` = stderr `FILE*`) |
| `ctor_003` | `0x408C20` | EDG internal caches | `ios_base::Init` + 7 doubly-linked lists at `12C6A40`, `12868C0..1286780` (symbol/type caches) |
| `ctor_004` | `0x408E50` | Emergency exception pool | 72,704-byte malloc pool at `12D4870`, free-list at `12D4868`, with pthread mutex |
| `ctor_005` | `0x408ED0` | Locale once-flags (set 1) | 8 flags at `12D6A68..12D6AA0` |
| `ctor_006` | `0x408F50` | Locale once-flags (set 2) | 8 flags at `12D6AF0..12D6B28` |
| `ctor_007` | `0x408FD0` | Locale once-flags (set 3) | 12 flags at `12D6D28..12D6D80` |
| `ctor_008` | `0x409090` | Locale once-flags (set 4) | 12 flags at `12D6DE8..12D6E40` |
| `ctor_009` | `0x409150` | Stream buffer destructors | `__cxa_atexit` for `basic_streambuf<char>` and `basic_streambuf<wchar_t>` |

Constructors 4--9 belong to statically-linked libstdc++. Only constructors 1--3 initialize EDG/NVIDIA state.

### EDG Main Body (0x409350 -- 0x7DF400, 3.61 MB)

The core of the compiler. Contains 5,115 functions compiled from 52 EDG `.c` source files plus 3 NVIDIA-specific source files. Functions are laid out in approximate alphabetical order by source file name -- the linker processed object files in directory-listing order:

```
0x409350   attribute.c     (170 functions)
0x419280   class_decl.c    (264 functions)
0x44B250   cmd_line.c      (43 functions)
0x461C20   const_ints.c    (3 functions)
0x466F90   cp_gen_be.c     (201 functions)
  ...
0x6BE300   nv_transforms.c (1 mapped function, NVIDIA)
0x6BE4A0   overload.c      (281 functions)
  ...
0x7A4940   types.c
0x7C0C60   modules.c
0x7D0EB0   floating.c
  ~0x7DF400  end of EDG code
```

The 52 source files break down by subsystem:

| Subsystem | Files | Functions | Description |
|---|---|---|---|
| Parser | 15 `.c` | ~800 | Lexer, expression/declaration parser, statements |
| Type system | 6 `.c` | ~350 | Type representation, checking, conversion |
| Templates | 5 `.c` | ~300 | Parsing, instantiation, deduction |
| IL subsystem | 8 `.c` | ~250 | Node types, allocation, walking, display, comparison |
| Infrastructure | 12 `.c` | ~400 | Memory, errors, name mangling, scope management |
| Code generation | 3 `.c` | ~150 | Backend `.int.c` emission |
| NVIDIA additions | 3 `.c` | ~110 | CUDA transforms, attribute validation, lambda wrappers |

See [Function Map](./function-map.md) for the complete address-to-source-file table.

### C++ Runtime Region (0x7DF400 -- 0x829722, 304 KB)

Statically-linked library code with no EDG source attribution. Contains approximately 900 functions from three libraries:

**Berkeley SoftFloat 3e** (0x7E0D30 -- 0x7E4150, ~80 functions). IEEE 754 arithmetic for half-precision (float16), extended precision (float80), and quad-precision (float128). Operations: add, sub, mul, div, sqrt, comparisons, int/float conversions. Global state at `12D4820` (exception flags) and `12D4821` (rounding mode). Used by the EDG `floating.c` subsystem for constant folding of non-native float types.

**libstdc++ / libsupc++** (0x7E42E0 -- 0x829600, ~800 functions). The C++ runtime:
- `operator new`/`operator delete` with new-handler retry loop (`0x7E42E0`)
- Exception handling: `__cxa_throw` (`0x823050`), `__cxa_begin_catch` (`0x822EB0`), `__cxa_allocate_exception` (`0x7E4750`), `std::terminate` (`0x8231A0`)
- Emergency exception pool: 72,704-byte fallback allocator for OOM during exception handling (`0x7E45C0`)
- iostream initialization: `ios_base::Init` constructor/destructor (`0x7E5650`/`0x7E5F20`) setting up cout/cin/cerr + wide variants
- Full locale system: 600+ functions implementing ctype, num_get, num_put, numpunct, collate, time_get/put, money_get/put, moneypunct, messages, and codecvt facets for both `char` and `wchar_t`

**CUDA-aware name demangler** (at `0x7CABB0`, technically in the EDG tail region). NVIDIA's custom Itanium ABI demangler with extensions for CUDA lambda wrapper templates. Recognizes mangled prefixes: `"Unvdl"` for `__nv_dl_wrapper_t<>`, `"Unvdtl"` for `__nv_dl_wrapper_t<>` with trailing return, and `"Unvhdl"` for `__nv_hdl_wrapper_t<>`.

**CRT startup** (0x40918C and 0x829640 -- 0x829722). `_start` at `0x40918C` calls `__libc_start_main(main@0x408950, init@0x829640, fini@0x8296D0)`. The `.fini_array` processor at `0x8296E0` iterates backwards through function pointers at `off_D428A0`.

## .rodata -- Read-Only Data (2.48 MB)

The `.rodata` section at `0x829740` -- `0xAA3FA3` holds all constant data: string literals, jump tables, error message templates, IL metadata tables, and format strings. Major structures:

### Error Message Table (off_88FAA0)

The EDG diagnostic system's message template table. An array of 3,795 `const char*` pointers, indexed by error code 0--3794:

```
off_88FAA0[0]    = ""                           // error 0: unused
off_88FAA0[1]    = "last line of file ends ..."  // error 1
  ...
off_88FAA0[3794] = "..."                         // error 3794
```

Each pointer references a NUL-terminated format string elsewhere in `.rodata` containing `%` fill-in specifiers (`%t` = type, `%s` = string, `%n` = name, `%sq` = quoted string, `%p` = position, `%d` = decimal). Error codes above 3456 are CUDA-specific (338 entries covering execution space violations, lambda restrictions, architecture feature gates). See [Diagnostic Overview](./diagnostics/overview.md).

### IL Entry Kind Name Table (off_E6DD80)

Maps the 85 `entry_kind` enum values (0--84) to human-readable strings. Used by the IL display subsystem (`il_to_str.c`) for debug output:

```
off_E6DD80[0]  = "scope"
off_E6DD80[6]  = "type"
off_E6DD80[11] = "routine"
off_E6DD80[23] = "variable"
  ...
off_E6DD80[84] = "last"       // sentinel
```

The `il_one_time_init` function (`sub_5CF7F0`) validates at startup that this table ends with the `"last"` sentinel, catching version mismatches between the table and the enum.

### EDG Source File Path Strings

Approximately 65 string literals of the form `/dvs/p4/build/sw/rel/gpgpu/toolkit/r13.0/compiler/drivers/compiler/edg/EDG_6.6/src/<file>.<ext>`. These are `__FILE__` expansions embedded in assertion macros. Each is referenced by the corresponding assert stub in the 0x403300 region.

### Jump Tables

Switch-statement jump tables for the major dispatch functions. The largest are:
- Expression parser dispatch (~120 case targets)
- Declaration specifier dispatch (~80 case targets)
- IL walker entry-kind dispatch (~85 case targets)
- Backend code generation dispatch (~90 case targets)

### Format Strings

Printf-style format strings for the `.int.c` backend emitter. These include CUDA runtime boilerplate templates (`"#include \"crt/host_runtime.h\""`, `"static __nv_managed_rt ..."`, `"void __device_stub__..."`) and IL display format strings.

## .data -- Initialized Globals (1.22 MB)

The `.data` section at `0xD46480` -- `0xE7EFF0` holds all initialized global variables. Major structures, ordered by address:

### Attribute Descriptor Table (off_D46820)

The master attribute dispatch table, starting at `0xD46820` and extending to approximately `0xD47A60`. Each entry is 32 bytes and describes one EDG/CUDA attribute kind: kind code (1 byte), flags (1 byte), name string pointer, validation function pointer, and application function pointer. See [Attribute System Overview](./attributes/overview.md).

### Diagnostic Fill-in Tables (off_D481E0)

Named-label fill-in descriptors for the diagnostic system. Maps fill-in label strings to format specifier dispatch codes. Located at `0xD481E0`.

### Keyword Tables

The EDG keyword registration system stores keyword-to-token-ID mappings. Initialized during `fe_translation_unit_init` (`sub_5863A0`) with 200+ C/C++ keywords (from `auto` through `co_yield`), 60+ type trait intrinsics (`__is_class`, `__has_trivial_copy`, etc.), and CUDA extension keywords (`__device__`, `__global__`, `__shared__`, `__constant__`, `__managed__`, `__launch_bounds__`, `__grid_constant__`).

### Error Severity Override Table

Maps error codes to their overridden severity levels. Populated by `--diag_suppress`, `--diag_warning`, `--diag_error` CLI flags.

### libstdc++ Vtables (0xD428C0 -- 0xD45E00, in .data.rel.ro)

The `.data.rel.ro` section holds vtables for all statically-linked C++ classes. Key vtables:

| Address | Class |
|---|---|
| `off_D42C00` | `__gnu_cxx::__concurrence_lock_error` |
| `off_D42C28` | `__gnu_cxx::__concurrence_unlock_error` |
| `off_D42CD8` | `std::bad_alloc` |
| `off_D45740` | `std::basic_istream<char>` |
| `off_D457C0` | `std::basic_istream<wchar_t>` |
| `off_D45860` | `std::basic_ostream<char>` |
| `off_D458E0` | `std::basic_ostream<wchar_t>` |
| `off_D45A28` | `std::basic_streambuf<char>` |
| `off_D45A78` | `std::basic_streambuf<wchar_t>` |

### Exception Handler Pointers (0xE7EExx)

Located at the tail of `.data`:

| Address | Type | Identity |
|---|---|---|
| `off_E7EEB0` | `qword` | atexit target: `basic_streambuf<wchar_t>` object |
| `off_E7EEB8` | `qword` | atexit target: `basic_streambuf<char>` object |
| `off_E7EEC0` | `qword` | `std::unexpected_handler` pointer |
| `off_E7EEC8` | `qword` | `std::terminate_handler` pointer |

### EDG Diagnostic List Head (0xE7FE40)

A 40-byte doubly-linked list structure at `0xE7FE40..0xE7FE68`. Initialized by `ctor_001` as an empty self-referencing sentinel (both forward and backward pointers point to the list head). Used to chain diagnostic records during compilation.

## .bss -- Zero-Initialized Globals (4.34 MB)

The `.bss` section at `0xE7F000` -- `0x12D6F20` is the largest section by virtual size. It contains all zero-initialized global state for both the EDG compiler and the statically-linked runtime. The `.bss` occupies no space in the ELF file on disk -- it is allocated and zeroed by the OS loader.

The 4.34 MB `.bss` divides into three logical regions:

### EDG Compiler State (0xE7F000 -- 0x1290000, ~4.1 MB)

The bulk of `.bss` holds the EDG frontend's global state. Major structures:

**Scope stack and symbol tables** (~1.5 MB). The EDG scope stack (`scope_stk.c`) maintains nested scope contexts during parsing. Each scope entry is 784 bytes. The scope stack globals, various hash tables for name lookup, and the associated symbol table arrays consume the largest contiguous blocks.

**IL region tracking** (~800 KB). Region indices, region-to-scope mappings (`qword_126EB90`), region memory tables (`qword_126EC88`), and IL entry list heads. The region counter at `dword_126EC80` tracks active regions. Each function definition creates a new region.

**Translation unit state** (~400 KB). The TU descriptor itself is dynamically allocated (424 bytes), but the per-TU global variables -- source file table, include stack, macro state, conditional compilation depth -- live in `.bss`. `sub_7A4860` (`reset_tu_state`) zeroes these between compilations.

**Parser state** (~600 KB). Token lookahead buffers, declaration nesting depth, template argument stacks, expression evaluation context. The lexer maintains character classification tables and identifier hash buckets.

**Error and diagnostic state** (~200 KB). Error count (`qword_126ED90`), warning count (`qword_126ED98`), error limit (`qword_126ED60`), diagnostic suppression bitmaps, and the stream state table at `126ED80..126EDE0` (13 qwords including the stderr `FILE*` at `qword_126EDF0`).

**Configuration flags** (~100 KB). The `0x106xxxx` region contains hundreds of `dword` flags set by CLI parsing and used throughout compilation. Examples:

| Address | Type | Identity |
|---|---|---|
| `dword_106B640` | int | Keep-in-IL guard flag |
| `dword_106B4B0` | int | Catastrophic error re-entry guard |
| `dword_106B4BC` | int | Warnings-as-errors recursion guard |
| `dword_106B9E8` | int | TU stack depth |
| `dword_106BA08` | int | TU-copy mode flag |
| `dword_106BBB8` | int | Output format (0=text, 1=SARIF) |
| `dword_106BCD4` | int | Predefined macro file mode |
| `dword_106C088` | int | Warnings-are-errors mode |
| `dword_106C188` | int | `wchar_t` keyword enabled |
| `dword_106C254` | int | Skip backend (errors present) |
| `dword_106C2C0` | int | GPU mode flag |
| `dword_1065928` | int | Internal error re-entry guard |

**Lambda capture bitmasks** (~256 bytes). Two 1024-bit bitmasks recording which lambda capture counts were used during parsing:

| Address | Size | Identity |
|---|---|---|
| `unk_1286900` | 128 bytes | Host-device lambda capture counts |
| `unk_1286980` | 128 bytes | Device lambda capture counts |

Bit N set means a lambda with N captures was encountered, triggering emission of the corresponding `__nv_dl_wrapper_t` or `__nv_hdl_wrapper_t` specialization in the backend.

**IL walker callbacks** (5 function pointers at `qword_126FB68..126FB88`). The five IL tree-walk callback slots: entry filter, entry replace, pre-walk check, string callback, and entry callback. Swapped in and out by different IL traversal passes.

### libstdc++ Runtime State (0x1290000 -- 0x12D6F20, ~280 KB)

**SoftFloat globals** (16 bytes). Exception flags at `byte_12D4820`, rounding mode at `byte_12D4821`.

**Emergency exception pool** (24 bytes of metadata). Free-list head (`qword_12D4868`), base address (`qword_12D4870`), capacity (`qword_12D4878` = 72,704 bytes). The pool itself is heap-allocated at startup by `ctor_004`.

**Locale system** (~2 KB). The "C" locale singleton (`unk_12D5E60`), global locale impl pointer (`qword_12D5E70`), classic locale impl pointer (`qword_12D5E78`), character classification tables (`12D5BE0..12D5D50`), locale ID counter (`dword_12D5E58`), and `pthread_once` control variables.

**iostream objects** (~2 KB). The six standard stream objects and their backing file buffers:

| Address | Identity |
|---|---|
| `0x12D6000` | `std::cerr` |
| `0x12D6060` | `std::cin` |
| `0x12D60C0` | `std::cout` |
| `0x12D5EE0` | `std::wcerr` |
| `0x12D5F40` | `std::wcin` |
| `0x12D5FA0` | `std::wcout` |

Each stream object is backed by a `basic_filebuf` at a known offset (e.g., cout's filebuf at `0x12D67E0`).

**Demangler caches** (40 bytes). Template argument cache at `qword_12C7B40/12C7B48/12C7B50` (capacity/count/buffer pointer, grows by 500 entries via realloc). Block-scope suppress flag at `dword_12C6A24`.

**EDG internal lists** (7 x 48 bytes). Seven doubly-linked list structures at `12868C0..1286780` initialized by `ctor_003`. Serve as symbol/scope/type caches with destructor `sub_6BD820`.

### Thread-Local Storage (0x12D6F20 -- 0x12D6F38, 24 bytes)

The `.tls` section holds exactly 24 bytes of thread-local data. This is the `__cxa_eh_globals` structure (accessed via `__readfsqword(0) - 16`):

```c
struct __cxa_eh_globals {
    void     *caught_exception_stack;   // +0x00: linked list of caught exceptions
    uint32_t  uncaughtExceptions;       // +0x08: count of in-flight exceptions
};
```

Despite cudafe++ being single-threaded, the TLS infrastructure exists because libstdc++ exception handling unconditionally uses TLS offsets compiled into the static library.

## .ctors / .dtors -- Constructor/Destructor Tables

The `.ctors` section at `0xD42858` is 88 bytes: a `-1` sentinel (8 bytes), 9 constructor function pointers (72 bytes), and a `0` terminator (8 bytes). The 9 constructors are `ctor_001` through `ctor_009` documented above.

The `.dtors` section at `0xD428B0` is 16 bytes: a `-1` sentinel and a `0` terminator. No destructors are registered -- all cleanup is done via `__cxa_atexit` handlers registered during construction.

## .eh_frame / .gcc_except_table -- Exception Handling

The `.eh_frame` section (582 KB) contains DWARF Call Frame Information (CFI) records for stack unwinding during C++ exception propagation. The `.gcc_except_table` section (13.2 KB) contains GCC Language-Specific Data Area (LSDA) records that map program counters to catch handlers and cleanup functions.

The `.eh_frame_hdr` section (48.9 KB) is a binary search index into `.eh_frame`, enabling O(log n) lookup of unwind information by instruction pointer during exception throw.

These sections exist because libstdc++ exception handling requires them. cudafe++ itself rarely throws exceptions -- the EDG frontend uses `longjmp`-based error recovery. However, the statically-linked libstdc++ code (particularly `operator new` and locale initialization) uses C++ exceptions internally.

## .plt / .got.plt -- PLT Stubs

The `.plt` section (2.2 KB, 141 entries) and `.got.plt` (1.1 KB) implement lazy binding for the 141 libc functions that cudafe++ imports despite static linking. These are glibc internal symbols resolved at load time. The PLT stubs are the standard x86-64 two-instruction pattern: indirect jump through GOT, then fallback to the dynamic linker (which never executes since the binary is statically linked -- the GOT is pre-resolved by the static linker).

## Static Libraries Linked

The binary statically links four library components:

| Library | Functions | .text Range | Purpose |
|---|---|---|---|
| libstdc++ (locale) | ~600 | `0x7EA800` -- `0x829600` | Full locale facet implementations |
| libstdc++ (iostream/exception) | ~60 | `0x7E42E0` -- `0x7EA800` | Streams, exceptions, operator new |
| Berkeley SoftFloat 3e | ~80 | `0x7E0D30` -- `0x7E4150` | float16/float80/float128 arithmetic |
| glibc CRT | ~10 | `0x40918C`, `0x829640` -- `0x829722` | `_start`, init, fini |

No shared libraries are loaded at runtime. The binary is fully self-contained.

## Virtual Address Space Map

```
0x400000 +-----------------------+
         | ELF headers           |  10.5 KB
0x402A18 | .init                 |  24 B
0x402A30 | .plt                  |  2.2 KB
0x403300 | .text                 |  4.15 MB
         |   assert stubs        |    34 KB    (0x403300 - 0x408B40)
         |   constructors        |    8 KB     (0x408B40 - 0x409350)
         |   EDG main body       |    3.61 MB  (0x409350 - 0x7DF400)
         |   C++ runtime         |    304 KB   (0x7DF400 - 0x829722)
0x829722 | padding               |
0x829724 | .fini                 |  14 B
0x829740 | .rodata               |  2.48 MB
         |   error table         |    30 KB    (off_88FAA0)
         |   string literals     |    ~2 MB
         |   IL kind names       |    <1 KB    (off_E6DD80)
         |   jump tables         |    ~400 KB
0xAA3FA3 | .eh_frame_hdr         |  48.9 KB
         |       [gap]           |
0xCB1210 | .eh_frame             |  568 KB
0xD3F398 | .gcc_except_table     |  13.2 KB
0xD42858 | .ctors                |  88 B
0xD428B0 | .dtors                |  16 B
0xD428C0 | .data.rel.ro          |  13.3 KB   (vtables)
0xD45E00 |       [padding/GOT]   |
0xD46480 | .data                 |  1.22 MB
         |   attribute table     |    ~5 KB    (off_D46820)
         |   keyword tables      |    variable
         |   handler pointers    |    (at 0xE7EExx)
         |   diagnostic list     |    (at 0xE7FE40)
0xE7EFF0 |       [padding]       |
0xE7F000 | .bss                  |  4.34 MB
         |   EDG compiler state  |    ~4.1 MB  (0xE7F000 - 0x1290000)
         |   libstdc++ state     |    ~280 KB  (0x1290000 - 0x12D6F20)
0x12D6F20| .tls                  |  24 B
0x12D6F38| extern                |  1.1 KB
0x12D73A8+-----------------------+
```

## Key Observations

**The .bss dominates.** At 4.34 MB, the `.bss` is the largest section -- larger than `.text`. This reflects the EDG frontend's design: hundreds of global variables hold parser state, scope stacks, symbol tables, and IL region metadata. A reimplementation should strongly consider replacing these globals with a context struct passed through the call chain.

**Static linking adds 304 KB of dead-weight code.** The C++ runtime region (0x7DF400 -- 0x829722) contains 900 functions, the majority of which (600+ locale facet methods) are never called by cudafe++. The locale system is pulled in transitively through iostream initialization. A reimplementation that avoids `std::cout`/`std::cerr` could eliminate this entirely.

**The EDG code is tightly packed.** The 3.61 MB EDG main body has almost no inter-function padding. Functions from the same source file are contiguous, and the alphabetical ordering by filename is consistent across the entire range. This makes address-to-source-file attribution reliable.

**The binary is position-dependent.** No PIE (Position-Independent Executable) flag is set. All code references use absolute addressing. The `.got` is minimal (56 bytes / 7 entries) -- almost all data references are direct.
