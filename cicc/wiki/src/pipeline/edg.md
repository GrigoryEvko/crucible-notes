# EDG 6.6 Frontend

NVIDIA licenses the Edison Design Group (EDG) C/C++ front end — a commercial compiler frontend used by several major compilers including Intel ICC. In cicc v13.0, EDG version 6.6 occupies 3.2 MB of code (`0x5D0000`–`0x8F0000`), making it the **largest single subsystem** in the binary. Unlike most modern compilers that parse directly to an SSA-based IR, EDG operates as a **source-to-source translator**: it parses CUDA C++ source code and emits transformed C code containing CUDA runtime API calls. This output is then fed into a second compilation phase that produces NVVM IR (LLVM bitcode). This two-stage design means the CUDA language extensions (kernel launch syntax, memory space qualifiers, device/host function annotations) are resolved entirely within EDG, and the LLVM-based backend never sees raw CUDA syntax.

The EDG frontend is configured at compile time through 737 `#define` macros, including GCC 8.1 emulation mode and Clang 9.1 emulation mode. Exceptions are disabled by default — CUDA device code cannot use C++ exceptions — while RTTI remains enabled for `dynamic_cast` support in host-side code that interacts with device objects.

| | |
|---|---|
| **EDG version** | 6.6 (string: `"Based on Edison Design Group C/C++ Front End, version 6.6"`) |
| **Entry symbol** | `lgenfe_main` (string at `sub_617BD0`) |
| **GCC emulation** | 8.1 (`DEFAULT_GNU_VERSION = 80100`) |
| **Clang emulation** | 9.1 (`DEFAULT_CLANG_VERSION = 90100`) |
| **C++ standards** | C++98, C++11, C++14, C++17, C++20, C++23 (`unk_4F07778` = year code) |
| **C standards** | C99, C11, C18, C23 |
| **Exceptions** | Disabled by default (`DEFAULT_EXCEPTIONS_ENABLED = 0`) |
| **RTTI** | Enabled by default (`DEFAULT_RTTI_ENABLED = 1`) |
| **Target model** | LP64 (`TARG_SIZEOF_POINTER = 8, TARG_SIZEOF_LONG = 8`) |
| **Backend** | C-codegen (`BACK_END_IS_C_GEN_BE = 1`) — emits C source, not LLVM IR directly |
| **Functions** | ~5,000 in range, 300+ above 5KB |

## Architecture

The compilation flow through EDG has four major phases: CLI parsing (282-case switch), translation unit initialization (keyword tables, parser bootstrapping), parsing and semantic analysis (the bulk of the 3.2 MB), and backend code emission (generating three output files: `.int.c` for internal declarations, `.device.c` for device code, and `.stub.c` for host-side launch stubs). Error recovery uses `setjmp`/`longjmp` — any of the 478 call sites that invoke the abort handler (`sub_721090`) will unwind back to the orchestrator rather than crashing the process.

```
sub_5D2A80 (orchestrator, setjmp error recovery)
  │
  ├─ sub_617BD0 (lgenfe_main: 282-case CLI switch, 737 config #defines)
  │    ├─ sub_610260 (register 300+ CLI options)
  │    └─ sub_6140E0 (option fetcher loop)
  │
  ├─ sub_8D0BC0 (translation unit init)
  │    ├─ sub_706250 (keyword table: ~350 keywords via sub_885C00)
  │    ├─ sub_858C60 (parser entry)
  │    └─ sub_709290 (finalize)
  │
  ├─ sub_709330 ("Generating Needed Template Instantiations", "Wrapping up translation unit")
  │
  └─ sub_5E3AD0 (backend entry: "Generating NVVM IR")
       ├─ Opens .int.c / .device.c / .stub.c output files
       ├─ sub_5DB980 (top-level declaration dispatcher)
       │    ├─ sub_5E13C0 (function declaration printer, 44KB)
       │    ├─ sub_5DBFC0 (expression printer, 41KB, 61 self-references)
       │    ├─ sub_5DFD00 (statement printer, 26KB)
       │    ├─ sub_5D80F0 (initializer printer)
       │    ├─ sub_5DAD30 (struct/union/enum printer)
       │    └─ sub_5DF1B0 (inline asm printer)
       │
       └─ dlopen("libTileIRCompiler_shared.so") [optional, gated by dword_4D045A0]
            └─ dlsym("cudacc_back_end") — 17-entry function pointer table
```

Timer callbacks record `"Front end time"`, `"Back end time"`, and `"Total compilation time"` via `sub_7211D0`.

## Orchestrator — `sub_5D2A80`

The master entry point for the entire frontend. Uses `setjmp` for non-local error recovery — when any of the ~5,000 EDG functions detects an unrecoverable error (type system inconsistency, parser corruption, internal assertion failure), it calls `sub_721090`, which `longjmp`s back to this function. The 478 call sites that reference the abort handler demonstrate just how pervasive error checking is throughout the frontend — roughly 10% of all functions in the EDG range can trigger a fatal abort.

| Global | Purpose |
|---|---|
| `unk_4D045D8` | Phase callback (prints "Generating NVVM IR" etc.) |
| `unk_4D04744` | Timer enable flag |
| `unk_4F074B0` | Error flag (frontend errors occurred) |
| `unk_4F074A8` | Warning count |
| `qword_4F076F0` | Input source filename |

## Frontend Entry — `sub_617BD0` (lgenfe_main)

At 123KB and 3,113 decompiled lines, `lgenfe_main` is the largest function in the EDG range. The name "lgenfe" stands for "LLVM-generating front end" — a hint that this function was originally designed for a different backend before NVIDIA adopted the EDG+LLVM architecture. The function is divided into three distinct regions: a massive 282-case switch for CLI option parsing (2,000 lines), a post-parse validation phase that checks for conflicting options and enforces CUDA-specific constraints, and a file I/O setup phase that installs 11 signal handlers and returns a pointer to the configured compilation context.

Signature: `(int argc, __int64 argv)`.

### Structure

| Region | Lines | Content |
|---|---|---|
| A | 164–2157 | 282-case switch on option ID (`v6`) |
| B | 2157–2700 | Post-parse validation and cross-option consistency |
| C | 2700–3113 | File I/O setup, 11 signal handlers, return `&qword_4D046F0` |

### Architecture Parsing (case `0x52`)

```
compute_75, compute_80, compute_86, compute_87, compute_88, compute_89
compute_90, compute_90a
compute_100, compute_100a, compute_100f
compute_103, compute_103a, compute_103f
compute_110, compute_110a, compute_110f
compute_120, compute_120a, compute_120f
compute_121, compute_121a, compute_121f
```

Storage: `unk_4D045E8` = SM number, `unk_4D045E4` = `a` suffix flag, `unk_4D045E0` = `f` suffix flag.

### Configuration Emission (case `0xE1`)

Emits 737 `#define` macros to configure the EDG compiler. Key defines:

| Define | Value | Meaning |
|---|---|---|
| `VERSION_NUMBER` | `"6.6"` | EDG frontend version |
| `EDG_MAIN` | `"lgenfe_main"` | Entry point symbol |
| `DEFAULT_GNU_VERSION` | `80100` | Emulate GCC 8.1 |
| `DEFAULT_CLANG_VERSION` | `90100` | Emulate Clang 9.1 |
| `DEFAULT_EXCEPTIONS_ENABLED` | `0` | CUDA: no exceptions |
| `TARG_SIZEOF_POINTER` | `8` | 64-bit pointers |
| `TARG_SIZEOF_LONG_DOUBLE` | `16` | 128-bit long double |
| `TARG_LITTLE_ENDIAN` | `1` | x86-64 host |
| `USE_SOFTFLOAT` | `1` | Software FP for constexpr |
| `ABI_COMPATIBILITY_VERSION` | `9999` | Maximum ABI compat |
| `MODULE_MAX_LINE_NUMBER` | `250000` | Max lines per module |

### CLI Option Registration — `sub_610260`

Registers ~300 options via `sub_6101D0(id, name, flag, ...)`. CUDA-specific options include:

| ID | Name | Purpose |
|---|---|---|
| 51 | `no-device-int128` | Disable \_\_int128 on device |
| 59 | `emit-llvm-bc` | Emit LLVM bitcode directly |
| 60 | `device-debug` | Device-side debug info |
| 68 | `force-volatile` | Force volatile on memory space (global/shared/constant/local/generic/all) |
| 73 | `kernel-params-are-restrict` | All kernel pointer params are `__restrict__` |
| 82 | `nv_arch` | `compute_XX` architecture selection |
| 93 | `device-c` | Separate compilation mode |
| 105 | `tile-only` | TileIR-only compilation |
| 124 | `extended-lambda` | Extended lambda support (`--expt-extended-lambda`) |
| 132 | `emit-lifetime-intrinsics` | LLVM lifetime intrinsics |

## Translation Unit Processing

Translation unit processing is where EDG transitions from CLI configuration to actual compilation. The init function sets up the lexer, allocates the translation unit data structure (416 bytes), populates the keyword table with ~350 entries, and enters the recursive-descent parser. EDG uses a keyword-registration model where each keyword is individually registered with its token ID — this allows NVIDIA to add CUDA-specific keywords (like `__shared__` or `__nv_fp8_e4m3`) without modifying the core parser grammar.

### Init — `sub_8D0BC0`

1. Reset token state (`dword_4F063F8 = 0`)
2. Call `sub_727950` (lexer init)
3. Allocate 416-byte TU object via `sub_823970`
4. Call `sub_706250` — keyword table init (~350 keywords)
5. Call parser entry (`sub_858C60` or PCH path `sub_852E40`)
6. Call `sub_709290` — finalize

### Keyword Registration — `sub_706250`

30KB. Calls `sub_885C00(token_id, "keyword_string")` ~350 times. Initializes 30+ subsystems before keyword registration. Categories:

- **C89 keywords**: `auto`, `break`, `case`, `const`, `continue`, `default`, `do`, `double`, `else`, ...
- **C99 additions**: `_Bool`, `_Complex`, `_Generic`, `_Atomic`, `restrict`, `inline`
- **C11/C23**: `_Static_assert`, `_Thread_local`, `_Alignas`, `_Alignof`, `constexpr`, `typeof`
- **C++ keywords**: `class`, `template`, `virtual`, `namespace`, `using`, `try`, `catch`, `throw`, ...
- **C++20**: `co_yield`, `co_return`, `co_await`, `requires`, `concept`
- **Type traits** (~80): `__is_pod`, `__is_abstract`, `__is_trivially_copyable`, `__has_virtual_destructor`, ...
- **NVIDIA extensions**: `__nv_is_extended_device_lambda_closure_type`, `__nv_is_extended_host_device_lambda_closure_type`, `__nv_is_extended_device_lambda_with_preserved_return_type`
- **EDG internal**: `__edg_type__`, `__edg_vector_type__`, `__edg_neon_vector_type__`, `__edg_scalable_vector_type__`

Version-gated by `dword_4F077C4` (language mode), `unk_4F07778` (standard year), `qword_4F077B4` (feature flags).

### Finalization — `sub_709330`

Strings: `"Generating Needed Template Instantiations"`, `"Wrapping up translation unit"`. Calls `sub_8B18F0` for C++ template instantiation when `dword_4F077C4 == 2`.

## Preprocessor

EDG includes its own preprocessor rather than relying on an external `cpp`. This is standard for EDG-based compilers — the preprocessor is tightly integrated with the parser to handle complex interactions between macros and C++ syntax (e.g., `__VA_OPT__` in C++20, which requires the preprocessor to understand syntactic context). The preprocessor occupies ~250KB across four major functions and maintains a 99-entry predefined macro table plus a 25-entry feature-test macro table.

### Token Scanner — `sub_7B8B50` (59KB)

The main preprocessor tokenizer. Handles all C/C++ token kinds: identifiers, numbers (delegates to `sub_7B40D0`), string literals, operators, punctuators, UCN sequences. Detects C++20 `module`/`import` keywords via string comparison.

### Numeric Literal Parser — `sub_7B40D0` (42KB)

Second-largest preprocessor function. Handles: integer suffixes (`u`/`U`/`l`/`L`/`ll`/`LL`), float suffixes (`f`/`F`/`l`/`L`), hex floats (`0x...p...`), binary literals (`0b...`), C++14 digit separators (`'`).

### Macro Expander — `sub_81B8F0` (77KB)

The central macro expansion engine. Features:

- `__VA_ARGS__` (C99) and `__VA_OPT__` (C++20) support
- 99-entry predefined macro table at `off_4B7C440` (stride 40 bytes)
- 25-entry feature-test macro table at `off_4B7C360`
- Recursion limit: 300 expansions (error `0xE3`)
- Intrinsic type-trait macros: `__type_pack_element`, `__is_signed`, `__make_integer_seq`, `__is_pointer`

### Character Scanner — `sub_7BC390` (29KB)

Giant switch on character value. Handles trigraph sequences, line splices, multi-byte characters, comment detection (`//` and `/*`).

## Parser & Declaration Processing

The parser subsystem is the largest part of the EDG frontend — over 1 MB of code spread across dozens of functions. EDG uses a recursive-descent parser augmented with a declaration-specifier state machine. The state machine design is necessary because C/C++ declaration specifiers can appear in any order (`const unsigned long long int` and `int long unsigned long const` are identical), requiring the parser to accumulate specifiers into bitmasks and resolve the final type only after all specifiers have been consumed.

NVIDIA's major contribution to the parser is the CUDA type extension infrastructure: 19 new FP8/FP6/FP4/MX-format type tokens (339–354) for Blackwell's tensor core operations, 9 address-space qualifier tokens (272–280) for GPU memory spaces, and 4 memory-space declaration specifiers (133–136) that piggyback on the existing width-modifier field. These extensions are grafted onto EDG's type system in a way that minimizes changes to the core parser logic — CUDA qualifiers reuse existing state variables with previously-unused value ranges.

### Declaration Specifier State Machine — `sub_672A20` (132KB, 4,371 lines)

The **central parser function** and one of the most complex functions in the binary. A `while(2)/switch` dispatcher on token codes from `word_4F06418[0]` with ~80 case labels. It accumulates type specifiers, qualifiers, storage-class specifiers, and CUDA address-space qualifiers from the token stream into a set of bitmask variables, then constructs the final type node from the accumulated state.

#### State Variables

| Variable | Stack | Bits | Role |
|---|---|---|---|
| `v325` | `[rsp+B8h]` | uint | Type specifier kind (see table below) |
| `v327` | `[rsp+C0h]` | uint64 | Specifier category bitmask |
| `v307` | `[rsp+90h]` | int | CV-qualifier accumulation bits |
| `v302` | `[rsp+78h]` | uint | Long count (0=none, 1=long, 2=long long) |
| `v305` | `[rsp+84h]` | uint | Signedness/width — **reused for CUDA** (4–7) |
| `v299` | `[rsp+68h]` | int | \_Complex (1) / \_Imaginary (2) tracking |

#### Type Specifier Kind (`v325`)

| Value | Meaning | Token Case |
|---|---|---|
| 0 | None yet | — |
| 2 | `char` | 80 |
| 3 | `wchar_t` | 165 |
| 4 | `bool` / `_Bool` | 128 / 120 |
| 5 | `float` | 126 |
| 6 | `double` | 127 |
| 7 | `void` | 180 |
| 8 | `signed` / `__int8` | 93 / 239 |
| 9 | `__float128` | 331 |
| 12 | `int` (explicit) | 89 |
| 14 | `__float16` | 332 |
| 15 | `short` / `half` | 85 |
| 16 | `_Float16` | 333 |
| 17 | `__bf16` | 334 |
| 19 | `bfloat16` | 335 |
| 20 | Resolved typedef/CUDA type name | scope lookup |
| 21 | struct/union/enum tag | 101/104/151 |
| 23 | `decltype()` | 183 |
| 24 | `auto` (deduced) | 186 |
| 25 | Resolved identifier type | C++ lookup |
| 26 | Error recovery type | diagnostic |

#### Specifier Bitmask (`v327`)

| Bit | Mask | Meaning |
|---|---|---|
| 0 | `0x1` | Storage class (extern/static/etc.) |
| 1 | `0x2` | CV-qualifier seen |
| 2 | `0x4` | Type specifier seen |
| 3 | `0x8` | `friend` specifier |
| 4 | `0x10` | `__declspec` / attribute seen |
| 5 | `0x20` | `explicit` specifier |
| 6 | `0x40` | `inline` specifier |
| 7 | `0x80` | `_Thread_local` / `thread_local` |
| 10 | `0x400` | `typeof` / `decltype` |
| 12 | `0x1000` | `__declspec()` already processed |
| 13 | `0x2000` | `explicit(bool)` already processed |
| 14 | `0x4000` | `_Noreturn` / `[[noreturn]]` |
| 15 | `0x8000` | `_Atomic` |

#### CV-Qualifier Bits (`v307`)

| Bit | Mask | Qualifier |
|---|---|---|
| 0 | `0x01` | `const` (case 81) |
| 1 | `0x02` | `volatile` (case 107) |
| 2 | `0x04` | `restrict` / `__restrict` (cases 118/119) |
| 3 | `0x08` | `__unaligned` (case 263 with parens) |
| 4 | `0x10` | `__ptr32` (case 264) |
| 5 | `0x20` | `__ptr64` (case 265) |
| 6 | `0x40` | `__sptr` / `__uptr` (case 266) |

Duplicate CV qualifiers trigger diagnostic 83.

#### CUDA Memory Space Tokens (133–136)

These piggyback on the signedness/width field `v305` with values 4–7:

| Token | Keyword | `v305` | `v325` | Formula |
|---|---|---|---|---|
| 133 | `__shared__` | 4 | 2 | Special case |
| 134 | `__device__` | 5 | 8 | `token - 129` |
| 135 | `__constant__` | 6 | 8 | `token - 129` |
| 136 | `__managed__` | 7 | 8 | `token - 129` |

Clean separation: values 0–3 = standard C width modifiers, 4–7 = CUDA address-space qualifiers. The type-construction switch handles both ranges.

#### CUDA Extended Type Tokens (339–354)

| Token | Type | Format |
|---|---|---|
| 236 | `__nv_fp8_e4m3` | FP8 |
| 339 | `__nv_fp8_e5m2` | FP8 |
| 340–343 | `__nv_fp8x{2,4}_e{4m3,5m2}` | FP8 vector |
| 344–345 | `__nv_fp6_e{2m3,3m2}` | FP6 |
| 346–347 | `__nv_fp6x2_e{2m3,3m2}` | FP6 vector |
| 348–349 | `__nv_mxfp8_e{4m3,5m2}` | MX-format FP8 |
| 350–351 | `__nv_mxfp6_e{2m3,3m2}` | MX-format FP6 |
| 352 | `__nv_mxfp4_e2m1` | MX-format FP4 |
| 353 | `__nv_satfinite` | Saturation type |
| 354 | `__nv_e8m0` | Exponent-only E8M0 |

All resolve via `sub_6911B0()` → type node, then set `v325=20, v327|=4`.

#### CUDA Address Space Qualifier Tokens (272–280)

| Token | Keyword | Space ID | Handler |
|---|---|---|---|
| 272 | `__attribute__((address_space(N)))` | parsed int | `sub_6210B0` |
| 273 | `__global__` | 0 | `sub_667B60(0,...)` |
| 274 | `__shared__` (addr space) | 2 | `sub_667B60(2,...)` |
| 275 | `__constant__` (addr space) | 3 | `sub_667B60(3,...)` |
| 276 | `__generic__` | — | `sub_72B620(type, cv)` |
| 277 | `__nv_tex_surf_handle_t` | — | `sub_72BA30(unk_4F06A51)` |
| 278 | `__nv_buffer_handle_t` | — | `sub_72BA30(unk_4F06A60)` |
| 279 | `__nv_grid_constant` | — | `sub_72C390()` |
| 280 | `__nv_is_extended_device_lambda` | — | `sub_72C270()` |

#### Type Construction Functions

| Function | Purpose | Trigger |
|---|---|---|
| `sub_72BA30(code)` | Fundamental signed integer type | int, short, long, long long |
| `sub_72BC30(code)` | CUDA extended-width integer | CUDA mode + `v305 > 3` |
| `sub_72BCF0(code)` | Unsigned fundamental type | unsigned combos |
| `sub_72BDB0(code)` | CUDA unsigned extended type | CUDA mode + unsigned |
| `sub_72BF70()` | float type | `v325 == 5` |
| `sub_72C030()` | double type | `v325 == 6` |
| `sub_72C0F0()` | long double type | long + double |
| `sub_72C1B0()` | `__float128` type | `v325 == 9` |
| `sub_72C610(kind)` | Float-by-kind (mapped from v325) | FP8/FP6/BF16/etc. |
| `sub_72C6F0(kind)` | `_Complex` float variant | `v299 == 1` |
| `sub_72C7D0(kind)` | `_Imaginary` float variant | `v299 == 2` |
| `sub_72C930(code)` | Error/placeholder type | diagnostic issued |
| `sub_72CBA0()` | Dependent type | `v325 == 25` |
| `sub_72CBE0(...)` | `__int128` type | `v325 == 1` |
| `sub_73C570(type, cv, flags)` | Apply CV-qualifiers to type | post-construction |

#### Accumulation Flow

1. **Initialize**: all state variables to 0
2. **Loop**: read `word_4F06418[0]`, dispatch through switch — set bitmask bits, update kind/cv/width
3. **Exit**: unrecognized token → `LABEL_8` (default exit)
4. **Type construction**: switch on `v325 × v302 × v305` → call appropriate `sub_72B*/sub_72C*`
5. **CV application**: `sub_73C570` wraps the type with const/volatile/restrict
6. **Return**: type stored at `ds->field_272`, CV bits at `ds->field_120`

### Declaration Specifier Parser — `sub_7C0F00` (184KB, 3,953 lines)

Uses **goto-driven dispatch** (393 `LABEL_` references) — NOT a switch/case. This is a massive state machine for declaration specifier resolution. Self-recursive at line 2407 with `flags=20` for nested declarator parsing.

### Top-Level Declaration Parser — `sub_662DE0` (61KB)

Declarator parsing — handles pointer (`*`), reference (`&`/`&&`), array (`[]`), and function (`()`) declarators. Uses SSE `__m128i` for bulk struct copying of 64-byte EDG type nodes.

### Overload Resolution — `sub_6523A0` (64KB)

The master overload resolution function. Given a declaration being introduced and a set of existing candidates from name lookup, it decides whether the declaration is a new overload, a redeclaration, or an error. At 2,448 decompiled lines with 39 diagnostic call sites, it is one of the heaviest diagnostic emitters in the frontend.

**Candidate collection** uses a 72-byte ranking context (`v320` on stack) and dispatches to one of three collectors: `sub_644100` for non-member/ADL candidates, `sub_648CF0` for member + using-declaration candidates (chosen when C++ mode, prior declaration exists, and the class has base classes or is a template), or `sub_6418E0` for C-linkage functions. The best candidate is selected by `sub_641B60`.

**`__builtin_` prefix forwarding** (lines 2060-2162): after resolution, if the resolved symbol is a bodyless non-member function, the resolver checks if a compiler builtin equivalent exists. It hardcodes three function names by length: `"abs"` (3), `"ceil"` (4), `"strlen"` (6). For each, it constructs `"__builtin_"` + name in a scratch buffer at `qword_4F06C50`, looks it up via `sub_878540`, then compares parameter types via `sub_8DED30(type1, type2, 0x100004)` (exact match + qualification conversion). On match, the builtin's scope entry is linked into the user function's auxiliary data at offset +256 field 8.

**OpenMP variant dispatch** (lines 727-752): when `unk_4D03A10` is set, the resolver renames the declaration to `"<name>$$OMP_VARIANT%06d"` using a monotonic counter `unk_4D03A0C`. This creates unique internal names for each device/host specialization.

**Constexpr/consteval propagation** (lines 2288-2301): gated by `unk_4F07778` (C++ standard year). For C++11 and later, byte +204 of the scope entry is bit-packed with three globals: bits 5-6 = `unk_4F06C58` (constexpr disposition), bits 1-2 = `unk_4F06C5A` (consteval disposition), bits 3-4 = `unk_4F06C59` (immediate-function flag). Diagnostic 2383 fires on constexpr mismatch between declaration and definition.

**Device/host overload sets**: CUDA allows the same function name to have both `__host__` and `__device__` overloads. EDG does not treat execution space as part of the function signature for overload resolution purposes -- the standard C++ overload rules apply first, and execution space filtering happens later during code generation. The `$$OMP_VARIANT` renaming mechanism is used for OpenMP dispatch variants that need distinct host/device specializations, but regular CUDA `__host__`/`__device__` overloads rely on the backend's execution space filtering rather than frontend overload resolution. This means that if two functions have identical C++ signatures but differ only in `__host__` vs `__device__`, they are treated as redeclarations (not overloads) at the EDG level, and the execution space annotation at scope entry offset +198 determines which version survives into device or host code.

### CUDA Memory Space Processing — `sub_6582F0` (22KB)

Validates `__shared__`, `__constant__`, `__managed__` attributes on declarations. Emits diagnostic for automatic variables in inappropriate memory spaces.

## Type System

### Type Node Layout (192 bytes = 12 x `__m128i`)

| Offset | Size | Field |
|---|---|---|
| +8 | 8 | Next pointer (linked lists) |
| +40 | 8 | Name pointer |
| +48 | 1 | Declaration kind byte |
| +80 | 1 | Entity kind byte |
| **+140** | **1** | **TYPE KIND DISCRIMINATOR** (the central dispatch key) |
| +160 | 8 | Inner/child type pointer (typedef chains, pointer bases) |
| +168 | 8 | Member list / parameter chain |
| +173 | 1 | Specifier/node kind byte |
| +176 | 2 | Entity kind (`uint16`, dispatch key for constexpr evaluator) |
| +185 | 1 | CV-qualifier bits (bit 0=const, 1=volatile, 2=restrict) |
| +200 | 1 | Attribute flags |

Type kind discriminator values at offset +140:

| Value | Type | Notes |
|---|---|---|
| 0 | `void` | |
| 1 | error type | Sentinel |
| 2–4 | fundamental (char, int, ...) | |
| 5 | pointer | Follows +160 chain |
| 6 | pointer-to-member | |
| 7 | function type | Complex: 17 sub-kinds for calling conventions |
| 8 | array | Element count at +128, element type at +160 |
| 9–11 | class / struct / union | Members at +168 |
| 12 | **typedef / cv-qualified** | **Follow +160 for underlying type** (critical: skip in type-walk loops) |
| 13 | enum | |
| 14 | void (incomplete) | |
| 15 | vector | Element count at +128 |
| 19 | decltype | |
| 21 | placeholder / auto | |

### Scope Table Entry (776 bytes)

Indexed by `dword_4F04C64` into base `qword_4F04C68`:

| Offset | Field |
|---|---|
| +0 | Scope identifier |
| +4 | Scope kind (5=namespace, 6=class, 7=function, 8=block, 9=enum, 12=template) |
| +6–10 | Flag bytes |
| +24 | Name list head |
| +32 | Name list tail |
| +208 | Class type pointer |
| +232 | Deferred list |
| +328 | Template info |
| +552 | Parent scope index |
| +624 | Declaration pointer |
| +680 | Linkage specification |

### Type Comparison — `sub_7386E0` (23KB)

The core type equivalence engine. Takes two type node pointers packed in an `__int128` and a flags word, returns boolean equality. The flags word controls comparison mode: bits 0-1 select cv-qualifier strictness (0=strict, 1=relaxed, 2=overload), bit 2 enables template matching (class-equivalence shortcuts), and bit 5 enables anonymous-class structural comparison.

**Entry sequence**: both types are first canonicalized through `sub_72EC50`, which peels through chains of non-template typedef aliases. The canonicalizer checks three fields on the elaborated type node: `+173 == 12` (typedef kind), `+176 == 1` (single-member), and `+170 bit 4 == 0` (no template specialization). If all hold, it unwraps one level via `sub_72E9A0` and loops. This means `typedef int MyInt; typedef MyInt YourInt;` canonicalizes `YourInt` directly to `int`.

After canonicalization, a **quick-reject** compares three header bytes without recursing: byte +24 (type kind) must match exactly, bytes +25 XOR must be zero for bits 0x03 (const/volatile) and 0x40 (restrict), and byte +26 XOR must be zero for bit 0x04. Any mismatch short-circuits to `return 0`.

The main switch dispatches on 38 type kinds. Key cases for CUDA:

- **Case 1 (fundamental)**: compares sub-kind at +56, extra flags at +58 (bits 0x3A), and the base type chain at +72. For integer sub-kind (`sub_kind == 'i'`), follows a resolution chain to find the underlying class scope. In template matching mode (flags bit 2), uses `sub_8C7520` to check whether two class instantiations share the same primary template, then `sub_89AB40` to compare template argument lists. This path handles CUDA's exotic numeric types (`__nv_fp8_e4m3`, `__nv_fp8_e5m2`, etc.) which are represented as fundamental types with distinct sub-kinds.
- **Case 3 (class/struct/union)**: fast identity via scope pointer equality, then unique-ID shortcut via `dword_4F07588`. For anonymous classes with template matching, calls `sub_740200` to extract canonical member lists and performs structural comparison. This is relevant for CUDA lambda closure types, which are anonymous classes.
- **Case 33 (using-declaration/alias)**: in overload mode (flags bit 1), performs a hash table lookup via `*qword_4D03BF8` to retrieve base class triples and compare element-by-element. This ensures that two `using` declarations resolving to different base classes are treated as distinct for overload discrimination.

**Overload mode specifics** (flags & 2): the post-switch check additionally verifies that both types agree on the presence/absence of the +80 "extra declaration" pointer. Template parameters are forced unequal (never match for overload purposes without being identical). Scope pointer equivalence is verified via unique-ID for using-declaration discrimination.

**CUDA type equivalence**: the NVIDIA-specific float types (`__nv_fp8_e4m3`, `__bf16`, `_Float16`, etc.) each have distinct sub-kind values at type node +56 (see the type mangling table: sub-kind 0 = `_Float16`, 1 = `__fp16`, 9 = `__bf16`, 0xA = `_Float16` alternate, 0xB = `_Float32`, 0xC = `_Float64`, 0xD = `_Float128`). The type comparison treats them as distinct fundamental types -- `_Float16` and `__fp16` are NOT equivalent despite both being 16-bit floats. The `half` type in CUDA maps to `_Float16` (sub-kind 0 or 0xA depending on context), while `__half` in `cuda_fp16.h` is a wrapper struct (type kind 9, class/struct), so `half` and `__half` are never type-equivalent at the EDG level. User code relies on implicit conversions defined in the CUDA headers, not on type equivalence.

### Type-to-String Emitter — `sub_74A390` (29KB, 19 callers)

The backbone type printer. Walks type nodes recursively, emitting textual representation for diagnostics. Handles NVIDIA-specific types: `__surface_type__`, `__texture_type__`, `__nv_bool`.

## IL Tree Infrastructure

EDG represents parsed code as an Intermediate Language (IL) tree — a rich AST that preserves full C++ semantic information including template instantiation state, scope chains, and type qualifiers. The IL is not LLVM IR; it is EDG's proprietary tree representation that predates the LLVM integration. All semantic analysis, template instantiation, and overload resolution operate on this tree.

The IL tree is traversed by four structurally identical walker functions that share the same 87 node-type dispatch table. The walkers are instantiated from a common template with different callback functions — a design pattern where the traversal logic is fixed but the action at each node is parameterized through function pointers stored in six global variables. This callback-driven walker system is central to EDG's architecture: template instantiation, type checking, code emission, and tree copying all use the same walker infrastructure with different callbacks.

| Function | Size | Self-recursive Calls | Purpose |
|---|---|---|---|
| `sub_7506E0` | 190KB | 297 | Primary walker |
| `sub_760BD0` | 109KB | 427 | Parallel walker (deeper traversal) |
| `sub_75C0C0` | 87KB | 316 | Third-pass walker |
| `sub_766570` | 148KB | 2 | Copier/transformer (takes callback params) |

### Walker Callback System

Six global function pointers form the visitor dispatch table:

| Global | Role |
|---|---|
| `qword_4F08028` | Node pointer remapper (called before recursion) |
| `qword_4F08020` | Linked-list child remapper |
| `qword_4F08038` | String field processor |
| `qword_4F08030` | Pre-visit callback (return nonzero to skip) |
| `qword_4F08040` | Post-visit callback |
| `dword_4F08014` | Skip-shared-nodes flag |
| `dword_4F08018` | Clear/detach mode (null out fields for ownership transfer) |

### IL Node Types (87 types, from walker case labels)

| ID | Type | ID | Type |
|---|---|---|---|
| 1 | source\_file | 28 | integral\_constant |
| 2 | scope (15 sub-kinds) | 29 | float\_constant |
| 3 | type\_qualifier | 30 | expression (generic) |
| 4 | simple\_type | 41 | call\_expression |
| 5 | pointer\_type | 42 | cast\_expression |
| 6 | function\_type (17 sub-kinds) | 43 | conditional\_expression |
| 7 | class\_type | 44 | string\_literal |
| 8 | enum\_type | 48 | template\_argument (4 sub-kinds) |
| 9 | array\_type | 59 | concept\_expression (10 sub-kinds) |
| 10 | bitfield\_type | 65 | type\_list (core linked list) |
| 13 | statement (30+ sub-kinds) | 75 | block/compound\_statement |
| 23 | scope\_entry (root) | 76 | access\_specifier |

### Deep Copy — `sub_766570` with `sub_8C2C50`

`sub_8C2C50` calls `sub_766570` with copy callback `sub_8C38E0` and list-copy callback `sub_8C3810`. Node size table at `qword_4B6D500[node_type]` provides `memcpy` sizes. Critical for template instantiation.

## Constexpr Evaluator

The constexpr evaluator is arguably the most technically impressive subsystem in the EDG frontend. It is a **complete tree-walking interpreter** that can execute arbitrary C++ code at compile time, implementing the full C++20 constexpr specification including heap allocation (`constexpr new`), string literals, virtual function dispatch, and complex control flow. At 317KB for the expression evaluator alone, plus 77KB for the statement executor and ~200KB in supporting functions, it constitutes nearly 20% of the entire EDG frontend.

The evaluator operates on EDG's IL tree directly — it does not compile to bytecode or any intermediate form. Instead, it recursively walks expression and statement nodes, maintaining its own memory model (a 3-tier page arena), variable bindings (an open-addressing hash table), and lifetime tracking (scope epoch counters). This design trades execution speed for implementation simplicity and guaranteed semantic fidelity with the compiler's own type system.

Signature:

```c
bool constexpr_eval_expr(
    constexpr_ctx *ctx,     // a1: evaluation context (hash table, arena, flags)
    expr_node **expr,       // a2: expression AST node
    __m128i *result,        // a3: output value slot (16 or 32 bytes)
    char *frame_base        // a4: stack frame base pointer for lifetime tracking
);
```

### Expression Evaluator — `sub_786210` (317KB, 9,075 lines)

The **largest function in the entire EDG frontend**. Two-level dispatch: outer switch on expression kind `*(a2+24)`, inner switch on operator code `*(a2+56)` with 124 cases.

#### Outer Switch — Expression Kinds

| Kind | Hex | Meaning | Notes |
|---|---|---|---|
| 0 | `0x00` | Void/empty | Sets `ctx+132 |= 0x40` |
| 1 | `0x01` | Operator expression | → 124-case inner switch on `*(a2+56)` |
| 2 | `0x02` | Variable reference | Hash table lookup, kind==1(const) or kind==3(constexpr) |
| 3 | `0x03` | Function reference / enumerator | Subkind==5: has constexpr body → recurse |
| 4 | `0x04` | Literal (int/float constant) | Immediate return — value is in the node |
| 5–6 | `0x05–06` | String / compound literal | C++20 mode required (`dword_4F077C4 == 2`) |
| 7 | `0x07` | Function call | Most complex case (~1200 lines) |
| 10 | `0x0A` | Parenthesized expression | Recurse on `a2[7]` |
| 11 | `0x0B` | Member access (`->`) | Navigate member hierarchy via type-size table |
| 17 | `0x11` | Lambda expression | Save/restore `ctx+72`, execute body via `sub_7987E0` |
| 18 | `0x12` | Capture variable | Hash table lookup by `a2[7]` |
| 20 | `0x14` | Address-of | Set flags `a3+8 = 0x20` (IS_SYMBOLIC) |
| 23 | `0x17` | sizeof / alignof | Delegate to `sub_620D80` |
| 24 | `0x18` | Subscript (`array[index]`) | Bounds check, compute `elem_size * index` |
| 27 | `0x1B` | Implicit conversion | Navigate chain, recurse on inner |
| 31 | `0x1F` | Requires expression (C++20) | Execute body via `sub_79B7D0` |
| 32 | `0x20` | Type trait | `sub_693DC0` → `xmmword_4F08280`/`xmmword_4F08290` |
| 33 | `0x21` | SFINAE / substitution failure | Template context check, `sub_6F2300` |

#### Inner Switch — Operator Codes (124 cases, selected)

| Cases | Category | Operations |
|---|---|---|
| 0–1 | Assignment | `=` / initialization (ref types: 32-byte memcpy) |
| 3–4 | Conversion | Lvalue-to-rvalue via `sub_7A0070` |
| 5 | Type cast | `static_cast` — massive dispatch: int→int(`sub_622780`), float→float(`sub_709EF0`), int→float(`sub_710280`), ptr→ptr(`sub_770010`) |
| 14–15 | Member access | `.` and `->` — offset via `sub_8D5CF0`, virtual base via `sub_771030` |
| 16–17 | Pointer arithmetic | Subtraction, `ptrdiff_t` via `sub_7764B0` |
| 20, 29 | Comparison | `==`, `!=` via `sub_7759B0` |
| 26–28 | Unary | `++`, `--`, unary minus (`sub_621DB0`) |
| 30–31 | Vector ops | Element-wise comparison loop, broadcast |
| 39–45 | Arithmetic | `+`(`sub_621270`), `-`(`sub_6215F0`), `*`(`sub_621F20`), `/`(`sub_6220A0`), `%`(`sub_6220C0`), `<<`(`sub_70BBE0`), `>>`(`sub_70BCF0`) — all with overflow/divzero checks |
| 46–49 | Bitwise | `&`, `\|`, `^`, `~` |
| 50–57 | Logical | `&&`, `\|\|` with short-circuit evaluation |
| 58–59 | Detailed comparison | Integer(`sub_621000`), float(`sub_70BE30`), pointer(address+symbolic) |
| 64 | Spaceship | `<=>` → strong\_ordering values at `unk_4F06BD8`–`unk_4F06C30` |
| 73–84 | Compound assignment | `+=` through `^=` with lifetime validation, const-check (diag 0x1318) |
| 91–93 | Conditional | Ternary `?:`, array subscript (bounds-checked, error 0xA84) |
| 94–95 | Virtual dispatch | Vtable lookup → `sub_79CCD0` |
| 96–97 | Allocation | Placement new / `operator new` |
| 103 | Exception | `throw` (always fails in constexpr) |
| 105–108 | Delegated | → `sub_77FCB0` (builtin operators) |

#### Value Slot Layout (16 bytes at `a3`)

| Offset | Size | Field |
|---|---|---|
| 0–7 | 8 | Primary value (integer, IEEE float, or arena pointer) |
| 8 | 1 | **Flags byte** (see below) |
| 9–11 | 3 | Alignment info, compound assignment tracking |
| 12–15 | 4 | Scope epoch ID (lifetime validation) |

Extended slot (32 bytes for reference types) adds secondary address at +16 and frame base at +24.

#### Flags Byte (offset +8)

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | `0x01` | IS_POINTER | Value is an indirect pointer |
| 1 | `0x02` | IS_PAST_END | One-past-the-end pointer |
| 2 | `0x04` | HAS_CLEANUP | Destructor chain at +16 |
| 3 | `0x08` | HAS_SUBOBJECT | Refers to a subobject |
| 4 | `0x10` | HAS_BITFIELD | Bitfield offset in bits 8–31 |
| 5 | `0x20` | IS_SYMBOLIC | Unresolved symbolic reference |
| 6 | `0x40` | IS_CONST | From a const declaration |
| 7 | `0x80` | IS_ARRAY_MEMBER | Part of array storage |

### Statement Executor — `sub_795660` (77KB)

Dispatch on `*(a2+40)` — statement kind:

| Case | Kind | Notes |
|---|---|---|
| 0 | Declaration | Arena alloc → eval initializer → insert into scoped hash table |
| 1–4 | If / if-else / if-init / if-constexpr | Condition → bool via `sub_620EE0` → branch |
| 5 | While loop | Step counter at `ctx+120`, limit from `qword_4D042E0` (~1M). Error 0x97F on exceeded. |
| 6 | Jump (break/continue/goto) | Sets control flow bits: bit 1=continue, bit 2=break, bit 3=goto |
| 7,15,24 | Null/empty | Return success |
| 8 | Return | Walk call chain at `ctx+72`, store result, set "returned" flag |
| 11 | Expression statement | Evaluate for side effects via `sub_7987E0` |
| 12 | For loop | Init → alloc → [condition → body → increment → cleanup] loop |
| 13 | Do-while | Delegates to `sub_7A0E60` |
| 14 | Range-based for | 4 temp slots via `sub_77A250`, iterator advance via `sub_7A0470` |

### Memory Management — 3-Tier Page Arena

| Tier | Location | Page Size | Threshold | Purpose |
|---|---|---|---|---|
| Primary | `ctx+16`/`ctx+24` | 64KB | default | Expression evaluation temporaries |
| Secondary | `ctx+144`/`ctx+152` | 64KB | lazy init (`ctx+132 & 8`) | Variable declarations |
| Tertiary | `ctx+80` | 64KB | nullable | String/compound literals |

Overflow: allocations >1024 bytes go to heap via `sub_822B10(size+16)`, forming a singly-linked list from `ctx+32`. Freed by walking until scope epoch matches.

Value slot header: type pointer at offset -8 (8 bytes), lifetime bits at offset -9 (1 byte, bit 0 = "initialized").

**Scope epoch**: monotonic counter at `ctx+128`. Hash table at `ctx+56`/`ctx+64` maps epoch → page state. Arena rewound on scope exit.

### Hash Table (`ctx+0`/`ctx+8`)

Open-addressing with 16-byte entries `[key, value]`. Hash: `key_pointer >> 3`. Collision: linear probing. Doubles at `2 * count > capacity` (via `sub_7704A0`). Secondary table at `ctx+56`/`ctx+64`/`ctx+68` uses 4-byte integer keys (scope epoch IDs).

### Diagnostic Codes

| Code | Hex | Meaning |
|---|---|---|
| 61 | `0x3D` | Division by zero |
| 2431 | `0x97F` | Step limit exceeded |
| 2692 | `0xA84` | Array index out of bounds |
| 2695 | `0xA87` | Unsupported jump in constexpr |
| 2698 | `0xA8A` | Null pointer dereference |
| 2705 | `0xA91` | Negative shift count |
| 2707 | `0xA93` | Integer overflow/underflow |
| 2712 | `0xA98` | Use of uninitialized variable |
| 2721 | `0xAA1` | Not a constant expression (generic) |
| 2727 | `0xAA7` | Invalid type conversion |
| 2735 | `0xAAF` | Pointer below array start |
| 2751 | `0xABF` | Access outside lifetime |
| 2766 | `0xACE` | Modification through null pointer |
| 2959 | `0xB8B` | Missing return in constexpr function |
| 3007 | `0xBBF` | `reinterpret_cast` in constexpr |
| 3022 | `0xBCE` | Call to undefined constexpr function |

Silent mode: `ctx+132` bit 5 (0x20) suppresses diagnostics (SFINAE contexts).

### Constexpr and CUDA: Host-Side Evaluation of Device Code

A key architectural question for any CUDA compiler is whether `constexpr` functions annotated `__device__` are evaluated at host compile time. In cicc v13.0, the answer is **yes, conditionally**. The constexpr evaluator operates entirely within the EDG frontend, which runs on the host. When a `constexpr __device__` function is used in a context requiring a constant expression (template argument, array bound, `static_assert`, `constexpr` variable initializer), the evaluator executes it using its tree-walking interpreter regardless of the function's execution space annotation. The execution space attributes (`__device__`, `__host__`, `__global__`) are semantic annotations for code generation, not for the constexpr evaluator -- the evaluator sees only the IL tree and does not distinguish between host and device function bodies.

This works because EDG's constexpr evaluator uses **software floating point** (`USE_SOFTFLOAT = 1` in the 737-define configuration block). All floating-point arithmetic in constexpr contexts goes through the softfloat library (`sub_70B8D0` add, `sub_70B9E0` sub, `sub_70BBE0` mul, `sub_70BCF0` div, `sub_709EF0` convert) rather than the host CPU's FPU. This guarantees that constexpr evaluation of device code produces results consistent with IEEE 754 semantics regardless of the host platform's floating-point behavior. The softfloat library handles all precision levels including `_Float16`, `__bf16`, `_Float32`, `_Float64`, and `__float128`.

SM architecture gates influence constexpr relaxations. The global `qword_4F077A8` (SM version) gates certain constexpr features:

- SM >= 89 (`qword_4F077A8 > 0x15F8F`): relaxed constexpr rules for variables with incomplete types
- `dword_4F077C4 == 2`: C++20 features including constexpr `new`, constexpr string literals, and constexpr member access (expression evaluator cases 5/6)
- `dword_4D04880`: C++14 relaxed constexpr (loops, local variable mutation, multiple return statements)
- C++23/26 extensions: constexpr `try`-`catch` (statement executor case 14), constexpr placement `new` (expression evaluator case 103), constexpr `dynamic_cast` (error `0xBB7`)

The evaluator enforces a step limit (`qword_4D042E0`, default ~1M iterations) to prevent infinite loops in constexpr evaluation. This limit applies uniformly to both host and device constexpr functions. When exceeded, diagnostic `0x97F` ("constexpr evaluation step limit exceeded") is emitted.

One important consequence: `__global__` (kernel) functions cannot be `constexpr` because they have no return value in the conventional sense -- they are launched asynchronously. The parser enforces this at the declaration specifier level, not in the constexpr evaluator.

### Supporting Functions

| Function | Size | Role |
|---|---|---|
| `sub_79CCD0` | 67KB | Object member accessor (base classes, virtual bases, union tracking) |
| `sub_799B70` | 33KB | Aggregate initializer (arrays, structs, designated init, brace elision) |
| `sub_79B7D0` | 29KB | Function call evaluator (argument binding, body execution, recursion limits) |
| `sub_7987E0` | 11KB | Statement list executor entry |
| `sub_77FCB0` | 150KB | Top-level dispatch (80 expression types + 62-entry intrinsic table) |
| `sub_7764B0` | 18KB | Type size calculator (Robin Hood hash memoization, 64MB cap) |
| `sub_7707D0` | — | Clone constexpr object |
| `sub_7790A0` | — | Trivial aggregate copy |
| `sub_7A0070` | — | Lvalue-to-rvalue load |
| `sub_77F5C0` | — | Bounds check (ptr, type → idx, err, size) |
| `sub_76FFC0` | — | Run cleanup/destructor chain |

### Bigint Library (`sub_621*`)

| Function | Operation |
|---|---|
| `sub_621000` | compare(a, width\_a, b, width\_b) → {-1,0,1} |
| `sub_621270` | add(dst, src, width, overflow\_out) |
| `sub_6215F0` | sub(dst, src, width, overflow\_out) |
| `sub_621F20` | mul(dst, src, width, overflow\_out) |
| `sub_6220A0` | div(dst, src, width, divzero\_out) |
| `sub_6220C0` | mod(dst, src, width, divzero\_out) |
| `sub_621DB0` | negate(dst) |
| `sub_620EE0` | to\_int(value, width, result\_out) |

### Float Library (`sub_70B*`)

| Function | Operation |
|---|---|
| `sub_70B8D0` | add(type, lhs, rhs, dst, inexact, exception) |
| `sub_70B9E0` | sub |
| `sub_70BAF0` | negate |
| `sub_70BBE0` | mul |
| `sub_70BCF0` | div |
| `sub_70BE30` | compare(type, lhs, rhs, nan\_result) → {-1,0,1,NaN} |
| `sub_709EF0` | convert(src, src\_prec, dst, dst\_prec, inexact) |

### Key Globals

| Variable | Purpose |
|---|---|
| `dword_4F077C4` | C++ standard version (2 = C++20, enables constexpr new/string) |
| `dword_4D04880` | C++14 relaxed constexpr (enables loops, mutation) |
| `qword_4D042E0` | Max constexpr evaluation steps (~1M) |
| `xmmword_4F08280` | Canonical constexpr TRUE |
| `xmmword_4F08290` | Canonical constexpr FALSE |
| `qword_4F08380` | Global type-size hash table base |
| `qword_4F08060` | Global allocator function pointer (constexpr new detection) |

## CUDA-Specific Extensions

NVIDIA's extensions to the EDG frontend fall into four categories: memory space qualifiers that map to GPU address spaces, kernel launch syntax that gets lowered to CUDA runtime API calls, registration stubs that tell the CUDA runtime about compiled kernels, and atomic builtin generation for the C++11 atomics model on GPU. These extensions are concentrated in the `0x650000`–`0x810000` range and reference SM architecture version globals extensively — many features are gated by `qword_4F077A8` comparisons against architecture thresholds.

### CUDA Keyword Extensions

NVIDIA extends the EDG keyword table with execution space qualifiers, memory space qualifiers, and type intrinsics. These are registered during `sub_706250` (keyword table initialization) and processed as first-class tokens throughout the parser -- not as attributes or pragmas. The parser's declaration specifier state machine (`sub_672A20`) handles them through dedicated token cases.

**Execution space qualifiers** are not registered as keywords in the traditional token table. Instead, `__device__`, `__host__`, and `__global__` are processed through the attribute system at token 272-280 in the declaration specifier parser. The memory space byte at scope entry offset +198 (bit 4 = device function) and the execution space character codes in `sub_6582F0` map them:

| Char Code | Qualifier | Space ID | Meaning |
|---|---|---|---|
| `'W'` (0x57) | `__device__` | 1 | Global device memory |
| `'X'` (0x58) | `__global__` | 0 | Kernel entry point |
| `'Z'` (0x5A) | `__shared__` | 2 | Per-block shared memory |
| `'['` (0x5B) | `__constant__` | 3 | Read-only constant memory |
| `'f'` (0x66) | `__managed__` | 5 | Unified memory (host+device) |
| `'k'` (0x6B) | `__cluster_dims__` | -- | Thread block cluster dimensions |

**Memory space declaration specifiers** (token cases 133-136) piggyback on the signedness/width field `v305` in the declaration specifier state machine with values 4-7, cleanly separated from the standard C width modifiers (0-3):

| Token | Keyword | `v305` Value | Handler |
|---|---|---|---|
| 133 | `__shared__` | 4 | Special case |
| 134 | `__device__` | 5 | `token - 129` |
| 135 | `__constant__` | 6 | `token - 129` |
| 136 | `__managed__` | 7 | `token - 129` |

**Address space qualifier tokens** (272-280) are processed by dedicated handlers in the declaration specifier parser:

| Token | Keyword | Handler |
|---|---|---|
| 272 | `__attribute__((address_space(N)))` | `sub_6210B0` (parses integer argument) |
| 273 | `__global__` (addr space) | `sub_667B60(0, ...)` |
| 274 | `__shared__` (addr space) | `sub_667B60(2, ...)` |
| 275 | `__constant__` (addr space) | `sub_667B60(3, ...)` |
| 276 | `__generic__` | `sub_72B620(type, cv)` |
| 279 | `__nv_grid_constant` | `sub_72C390()` |

The `__grid_constant__` qualifier (token 279, handler `sub_72C390`) marks kernel parameters as grid-constant -- the parameter is read-only across all thread blocks and may be placed in constant memory by the backend. This is a SM 70+ feature.

**NVIDIA type intrinsic keywords** are always registered (not version-gated):

| Token | Keyword |
|---|---|
| 328 | `__nv_is_extended_device_lambda_closure_type` |
| 329 | `__nv_is_extended_host_device_lambda_closure_type` |
| 330 | `__nv_is_extended_device_lambda_with_preserved_return_type` |

These type traits are used by CUDA's extended lambda machinery to query whether a lambda closure type carries device or host-device execution space annotations. They participate in SFINAE and `if constexpr` contexts for compile-time dispatch between host and device lambda implementations.

### Memory Space Attributes

`sub_6582F0` and `sub_65F400` validate `__shared__`, `__constant__`, `__managed__` on declarations. Token cases 133-136 in the parser handle these as first-class declaration specifiers. The validation logic enforces CUDA semantics: `__shared__` variables cannot have initializers (shared memory is not initialized on kernel launch), `__constant__` variables must have static storage duration, and `__managed__` variables require unified memory support on the target architecture.

The validation in `sub_6582F0` follows a multi-phase pipeline. Phase 1 handles `__managed__` pre-resolution: when not in whole-program mode, managed variables are silently downgraded to `__device__` (space code 1). Later phases validate mutual exclusivity -- `__constant__` combined with `__shared__` triggers diagnostic 3481, `__constant__` combined with `__managed__` triggers 3568. The `thread_local` storage class is incompatible with all device memory spaces (diagnostic 892/3578). Automatic (stack) variables cannot carry device memory qualifiers (diagnostic 3484), because GPU shared and constant memory are not per-thread allocations.

The diagnostic messages use the memory space name strings directly: `"__shared__"`, `"__constant__"`, `"__device__"`, `"__managed__"` -- these are format arguments to the diagnostic emitter, not hardcoded strings in the error message table.

### Kernel Launch Lowering — `sub_7F2B50` (16KB)

Transforms CUDA's `<<<gridDim, blockDim, sharedMem, stream>>>` kernel launch syntax into CUDA runtime API calls. The lowered sequence allocates a parameter buffer via `cudaGetParameterBufferV2`, copies kernel arguments into it, and launches with `cudaLaunchDeviceV2`. For the simpler launch path, it generates `__cudaPushCallConfiguration` followed by individual `__cudaSetupArg`/`__cudaSetupArgSimple` calls. This lowering happens entirely within EDG — by the time the code reaches the LLVM backend, kernel launches are ordinary function calls.

### Registration Stub Generator — `sub_806F60`

Generates `__cudaRegisterAll` function with calls to:
`__cudaRegisterEntry`, `__cudaRegisterVariable`, `__cudaRegisterGlobalTexture`, `__cudaRegisterGlobalSurface`, `__cudaRegisterManagedVariable`, `__cudaRegisterBinary`, `____cudaRegisterLinkedBinary`.

Host-side stubs generated by `sub_808590`: `"__device_stub_%s"`, `"__cudaLaunch"`, `"__cudaSetupArg"`, `"__cudaSetupArgSimple"`.

### Atomic Builtin Generator — `sub_6BBC40` (34KB)

Constructs `__nv_atomic_fetch_{add,sub,and,xor,or,max,min}` names with type suffixes (`_s`, `_f`, `_u`) and width (`_%u`).

### SM Architecture Gates

Two functions configure ~160 optimization/feature flags based on SM version:

| Function | Role | Thresholds |
|---|---|---|
| `sub_60D650` | Optimization level → 109 `unk_4D04*` flags | Single integer parameter (O-level) |
| `sub_60E7C0` | SM arch → 60 `unk_4D04*` feature flags | SM 75 (30399), SM 80 (40000), SM 90 (89999), SM 100 (109999), SM 120 (119999) |

Each flag is gated by a `byte_4CF8*` user-override check, preventing auto-configuration when the user explicitly sets a flag via CLI.

### TileIR Backend

`sub_5E3AD0` optionally loads `libTileIRCompiler_shared.so` via `dlopen` and looks up symbol `"cudacc_back_end"`. A 17-entry function pointer table is passed. Gated by `dword_4D045A0`.

## Diagnostic System

EDG's diagnostic system supports three output formats: human-readable terminal output (with ANSI color and word-wrapping), SARIF JSON for IDE integration, and a machine-readable log format for automated tooling. All three share the same diagnostic numbering scheme and severity classification. The terminal output handler alone is 37KB — it implements its own word-wrapping algorithm with configurable terminal width, recursive child diagnostic emission (for "note: see declaration of X" chains), and color coding by severity level.

### Terminal Output — `sub_681D20` (37KB)

Formats error/warning/remark messages with:
- Severity labels: remark (2), warning (4), caution (5), severe-warning (6), error (7–8), catastrophe (9–10), internal-error (11)
- Source location: `file:line:col`
- ANSI color escapes (gated by `dword_4F073CC`)
- Word-wrapping at `dword_4D039D0` (terminal width)
- Recursive child diagnostic emission

### SARIF JSON Output — `sub_6837D0` (20KB)

Structured diagnostics for IDE integration, enabled by `--diagnostics_format=sarif` (CLI case `0x125`, sets `unk_4D04198 = 1`). The output is a comma-separated stream of SARIF result objects -- NOT a complete SARIF envelope with `$schema`, `runs[]`, etc. The caller or a post-processor is expected to wrap the stream in the standard SARIF container.

Each diagnostic emits one JSON object:

```json
{
  "ruleId": "EC<number>",
  "level": "error",
  "message": {"text": "<json-escaped message>"},
  "locations": [
    {
      "physicalLocation": {
        "artifactLocation": {"uri": "file://<path>"},
        "region": {
          "startLine": 42,
          "startColumn": 17
        }
      }
    }
  ],
  "relatedLocations": [
    {
      "message": {"text": "see declaration of X"},
      "physicalLocation": { ... }
    }
  ]
}
```

**Rule ID format**: `"EC"` + decimal error number from the diagnostic record at offset +176. For example, EDG error 1234 becomes `"EC1234"`.

**Severity mapping** (byte at diagnostic node +180):

| Severity | EDG Meaning | SARIF `level` |
|---|---|---|
| 4 | remark | `"remark"` |
| 5 | warning | `"warning"` |
| 7, 8 | error | `"error"` |
| 9 | catastrophe | `"catastrophe"` |
| 11 | internal error | `"internal_error"` |

Note that SARIF spec only defines `"warning"`, `"error"`, and `"note"` as standard levels. The `"remark"`, `"catastrophe"`, and `"internal_error"` values are EDG extensions -- consuming tools should treat unknown levels as `"error"`.

**Message text escaping**: `sub_683690` renders the diagnostic text into `qword_4D039E8`, then copies character-by-character into the output buffer, escaping `"` as `\"` and `\` as `\\`. No other JSON escaping (e.g., control characters, Unicode) is applied.

**Location resolution**: `sub_67C120` calls `sub_729E00` to decompose the packed source location into (file-id, line, column), then `sub_722DF0` to resolve the file-id to a filesystem path. The `startColumn` field is omitted when column is zero.

**Related locations**: the linked list at diagnostic node +72 chains "note" sub-diagnostics. Each is emitted as a `relatedLocations` array entry with its own message and physical location.

**Filtering before emission**: diagnostics pass through severity threshold check (`byte_4F07481[0]`), duplicate detection (`byte_4CFFE80[4*errnum + 2]` bit flags), pragma-based suppression (`sub_67D520`), and error limit check (`unk_4F074B0 + unk_4F074B8 >= unk_4F07478`). All filtering happens before the SARIF/text format branch.

### Machine-Readable Log

Writes to `qword_4D04908` in format: `<severity-char> "<filename>" <line> <col> <message>\n`. Severity chars from `"RwweeccccCli"`: R=remark, w=warning, e=error, c=catastrophe.

## Name Mangling (Itanium ABI)

EDG includes a complete implementation of the Itanium C++ ABI name mangling specification. NVIDIA extends the standard mangling with three proprietary prefixes (`Unvdl`, `Unvdtl`, `Unvhdl`) for device lambdas, device template lambdas, and host-device lambdas respectively. These extensions are necessary because CUDA's execution model requires distinguishing between host and device versions of the same lambda — they must have different mangled names to avoid linker collisions when both host and device code are linked into the same binary.

Address range `0x810000`–`0x8EFFFF`:

| Function | Size | Role |
|---|---|---|
| `sub_8E74B0` | 29KB | Primary mangling entry |
| `sub_8E9FF0` | 26KB | Type mangling |
| `sub_816460` | 24KB | Type component mangling |
| `sub_813790` | 13KB | Expression mangling |
| `sub_80E340` | 23KB | Builtin type mangling (incl. `DF16_`, `DF16b`, `Cu6__bf16`, `u6__mfp8`) |
| `sub_80FE00` | 8KB | NVIDIA extension mangling (`Unvdl`, `Unvdtl`, `Unvhdl`) |

**NVIDIA lambda mangling extensions** (`sub_80FE00`): standard Itanium ABI uses `Ul<params>E<index>_` for unnamed lambda types and `Ut<index>_` for unnamed non-lambda types. NVIDIA adds three proprietary prefixes chosen based on flag byte +92 of the lambda's closure descriptor:

| Prefix | Meaning | Condition |
|---|---|---|
| `Unvdl` | `__device__` lambda | `flag_byte_92 & 0x20` set, not host-device, not template |
| `Unvdtl` | `__device__` template lambda | `flag_byte_92 & 0x20` set, `flag_byte_92 & 4` set |
| `Unvhdl` | `__host__ __device__` lambda | `flag_byte_92 & 0x20` set, `flag_byte_92 & 0x10` set |

The `Unvhdl` prefix carries three single-digit flags separated by underscores after the prefix: `Unvhdl<index>_<has_explicit_return>_<is_host_device>_<has_template_params>_`. Each flag is `'0'` or `'1'`. This is richer than the standard `Ul` which only encodes parameter types.

**NVIDIA vendor type manglings** (`sub_80E340`): the type mangler handles CUDA-specific types as Itanium vendor types (prefix `u` + length + name):

| Type | Mangling | Notes |
|---|---|---|
| `__bf16` (bfloat16) | `u6__bf16` or `DF16b` | ABI-gated: `qword_4F077B4` lo32 selects vendor vs C++23 encoding |
| `__mfp8` (FP8) | `u6__mfp8` | NVIDIA micro-float 8-bit for transformer inference |
| `__metainfo` | `U10__metainfo` | Kernel parameter metadata type attribute |
| `float80` | `u7float80` | x87 extended precision (vendor type) |

The `__bf16` mangling has a three-way gate reflecting the ongoing ABI transition: `qword_4F077B4` lo32 != 0 selects `"u6__bf16"` (vendor type); hi32 == 0 selects `"DF16b"` (C++23 standardized P1467); otherwise `qword_4F06A78` determines which encoding. The ABI version variable `unk_4D04250` controls this and other encoding decisions, with known thresholds at `0x76BF` (GCC 3.3 compat) and `0xC350` (GCC 12 compat).

Standard float types follow Itanium: `_Float16` = `"DF16_"`, `__fp16` = `"Dh"`, `float` = `"f"`, `double` = `"d"`, `__float128` = `"g"`, with the complex variants adding a `'C'` prefix.

## Key Global Variables

| Variable | Size | Role |
|---|---|---|
| `dword_4F077C4` | 4 | Language mode: 0=neither, 1=C, 2=C++ |
| `unk_4F07778` | 4 | C/C++ standard year (199711, 201103, 201402, 201703, 202002, 202310) |
| `qword_4F077B4` | 8 | Dialect extension flags (lo=CUDA extensions, hi=GNU extensions) |
| `dword_4F077BC` | 4 | NVCC mode flag |
| `dword_4F077C0` | 4 | GCC compatibility mode |
| `qword_4F077A8` | 8 | SM architecture version (controls feature gates throughout) |
| `word_4F06418` | 2 | Current parser token |
| `qword_4F04C68` | 8 | Scope table base pointer (776-byte entries) |
| `dword_4F04C64` | 4 | Current scope index |
| `qword_4CF7CE0` | 8 | AST printer callback vtable |
| `qword_4D03FF0` | 8 | Current translation unit pointer |
| `qword_4D04908` | 8 | Machine-readable diagnostic log FILE* |
| `qword_4F08028`–`qword_4F08040` | 48 | IL tree walker callback table |
| `dword_4D045A0` | 4 | TileIR mode flag |
