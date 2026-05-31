# EDG Build Configuration

cudafe++ is built from Edison Design Group (EDG) C/C++ front end source code, version 6.6. At build time, NVIDIA sets approximately 750 compile-time constants that control every aspect of the front end's behavior -- from which backend generates output, to how the IL system operates, to what ABI conventions are followed. These constants are baked into the binary and cannot be changed at runtime. They represent the specific EDG configuration NVIDIA chose for CUDA compilation.

The function `dump_configuration` (`sub_44CF30`, 785 lines) prints all 747 constants as C preprocessor `#define` statements when invoked with `--dump_configuration`. Of these, 613 are defined and 134 are explicitly listed as "not defined." The output is written to `qword_126EDF0` (the configuration output stream, typically stderr) in alphabetical order.

```bash
$ cudafe++ --dump_configuration
/* Configuration data for Edison Design Group C/C++ Front End */
/* version 6.6, built on Aug 20 2025 at 13:59:03. */

#define ABI_CHANGES_FOR_ARRAY_NEW_AND_DELETE 1
#define ABI_CHANGES_FOR_CONSTRUCTION_VTBLS 1
...
#define WRITE_SIGNOFF_MESSAGE 1

/* Legacy configuration: <unnamed> */
#define LEGACY_TARGET_CONFIGURATION_NAME NULL
```

The constants fall into seven categories: backend selection, IL system, internal checking, diagnostics, target platform model, compiler compatibility, and feature defaults.

## Backend Selection

The EDG front end supports multiple backend code generators. NVIDIA configured cudafe++ for the **C++ code generation backend** (cp_gen_be), which means the front end's output is C++ source code -- not object code, not C, and not a serialized IL file.

| Constant | Value | Meaning |
|---|---|---|
| `BACK_END_IS_CP_GEN_BE` | `1` | Backend generates C++ source (the `.ii` / `.int.c` output) |
| `BACK_END_IS_C_GEN_BE` | `0` | Not the C code generation backend |
| `BACK_END_SHOULD_BE_CALLED` | `1` | Backend phase is active (front end does not stop after parsing) |
| `CP_GEN_BE_TARGET_MATCHES_SOURCE_DIALECT` | `1` | Generated C++ targets the same dialect as the input |
| `GEN_CPP_FILE_SUFFIX` | `".int.c"` | Output file suffix for generated C++ |
| `GEN_C_FILE_SUFFIX` | `".int.c"` | Output file suffix for generated C (same as C++, unused) |

This is the central architectural fact about cudafe++. It is a source-to-source translator: CUDA C++ goes in, host-side C++ with device stubs comes out. The cp_gen_be backend walks the IL tree and emits syntactically valid C++ that the host compiler (gcc/clang/MSVC) can consume. The generated code preserves the original types, templates, and namespaces rather than lowering to a simpler representation.

The `CP_GEN_BE_TARGET_MATCHES_SOURCE_DIALECT=1` setting means the backend does not down-level the output. If the input is C++17, the generated code uses C++17 constructs. This avoids the complexity of translating modern C++ features into older dialects.

### Disabled Backend Features

Several backend capabilities are compiled out:

| Constant | Value | Meaning |
|---|---|---|
| `GCC_IS_GENERATED_CODE_TARGET` | `0` | Output is not GCC-specific C |
| `CLANG_IS_GENERATED_CODE_TARGET` | `0` | Output is not Clang-specific C |
| `MSVC_IS_GENERATED_CODE_TARGET` | `0` | Output is not MSVC-specific C |
| `SUN_IS_GENERATED_CODE_TARGET` | `0` | Output is not Sun/Oracle compiler C |
| `MICROSOFT_DIALECT_IS_GENERATED_CODE_TARGET` | `0` | Output does not use Microsoft C++ extensions |

None of the compiler-specific code generation targets are enabled. The cp_gen_be emits portable C++ that is syntactically valid across all major compilers. This is possible because CUDA's host compilation already controls dialect selection through its own flag forwarding to the host compiler.

## IL System

The Intermediate Language (IL) system is the core data structure connecting the parser to the backend. NVIDIA's configuration makes a critical choice: the IL is never serialized to disk.

| Constant | Value | Meaning |
|---|---|---|
| `IL_SHOULD_BE_WRITTEN_TO_FILE` | `0` | IL stays in memory -- never written to an IL file |
| `DO_IL_LOWERING` | `0` | No IL transformation passes before backend |
| `IL_WALK_NEEDED` | `1` | IL walker infrastructure is compiled in |
| `IL_VERSION_NUMBER` | `"6.6"` | IL format version, matches EDG version |
| `ALL_TEMPLATE_INFO_IN_IL` | `1` | Complete template metadata in the IL graph |
| `PROTOTYPE_INSTANTIATIONS_IN_IL` | `1` | Uninstantiated function prototypes preserved |
| `NEED_IL_DISPLAY` | `1` | IL display/dump routines compiled in |
| `NEED_NAME_MANGLING` | `1` | Name mangling infrastructure compiled in |
| `NEED_DECLARATIVE_WALK` | `0` | Declarative IL walker not needed |

### Why IL_SHOULD_BE_WRITTEN_TO_FILE=0 Matters

In a standard EDG deployment (like the Comeau C++ compiler or Intel ICC's older front end), the IL can be serialized to a binary file for separate backend processing. With `IL_SHOULD_BE_WRITTEN_TO_FILE=0`, NVIDIA eliminates the entire IL serialization path. The IL exists only as an in-memory graph during compilation:

1. The parser builds IL nodes in region-based arenas (file-scope region 1, per-function region N)
2. The IL walker traverses the graph to select device vs. host code
3. The cp_gen_be backend reads the IL graph directly and emits C++ source
4. The arenas are freed

This design means the `IL_FILE_SUFFIX` constant is left undefined -- there is no suffix because there is no file. The constants `LARGE_IL_FILE_SUPPORT`, `USE_TEMPLATE_INFO_FILE`, `TEMPLATE_INFO_FILE_SUFFIX`, `INSTANTIATION_FILE_SUFFIX`, and `EXPORTED_TEMPLATE_FILE_SUFFIX` are all similarly undefined.

### Why DO_IL_LOWERING=0 Matters

IL lowering is an optional transformation pass that simplifies the IL before the backend processes it. In a lowering-enabled build, complex C++ constructs (VLAs, complex numbers, rvalue adjustments) are reduced to simpler forms. With `DO_IL_LOWERING=0`, NVIDIA bypasses all of this:

| Constant | Value | Meaning |
|---|---|---|
| `DO_IL_LOWERING` | `0` | Master lowering switch is off |
| `LOWER_COMPLEX` | `0` | No lowering of `_Complex` types |
| `LOWER_VARIABLE_LENGTH_ARRAYS` | `0` | VLAs passed through as-is |
| `LOWER_CLASS_RVALUE_ADJUST` | `0` | No rvalue conversion lowering |
| `LOWER_FIXED_POINT` | `0` | No fixed-point lowering |
| `LOWER_IFUNC` | `0` | No indirect function lowering |
| `LOWER_STRING_LITERALS_TO_NON_CONST` | `0` | String literals keep const qualification |
| `LOWER_EXTERN_INLINE` | `1` | Exception: extern inline functions are lowered |
| `LOWERING_NORMALIZES_BOOLEAN_CONTROLLING_EXPRESSIONS` | `0` | No boolean normalization |
| `LOWERING_REMOVES_UNNEEDED_CONSTRUCTIONS_AND_DESTRUCTIONS` | `0` | No dead construction removal |

The only lowering that remains active is `LOWER_EXTERN_INLINE=1`, which handles `extern inline` functions that need special treatment in the generated output. Everything else passes through the IL untransformed.

This makes sense for cudafe++'s role. As a source-to-source translator, it benefits from preserving the original code structure. The host compiler handles all the actual lowering when it compiles the generated `.ii` file.

### Why IL_WALK_NEEDED=1 Matters

Despite no serialization and no lowering, the IL walk infrastructure is compiled in. This is because cudafe++ uses the IL walker for its primary CUDA-specific task: **device/host code separation**. The walker traverses the IL graph and marks each entity with execution space flags (`__host__`, `__device__`, `__global__`), then the backend selectively emits code based on which space is being generated.

### Template Information Preservation

| Constant | Value | Meaning |
|---|---|---|
| `ALL_TEMPLATE_INFO_IN_IL` | `1` | Full template definitions in the IL, not a separate database |
| `PROTOTYPE_INSTANTIATIONS_IN_IL` | `1` | Even uninstantiated prototypes kept |
| `RECORD_TEMPLATE_STRINGS` | `1` | Template argument strings preserved |
| `RECORD_HIDDEN_NAMES_IN_IL` | `1` | Names hidden by using declarations still recorded |
| `RECORD_UNRECOGNIZED_ATTRIBUTES` | `1` | Unknown `[[attributes]]` preserved in IL |
| `RECORD_RAW_ASM_OPERAND_DESCRIPTIONS` | `1` | Raw asm operand text kept |
| `KEEP_TEMPLATE_ARG_EXPR_THAT_CAUSES_INSTANTIATION` | `1` | Template argument expressions that trigger instantiation are retained |

With `ALL_TEMPLATE_INFO_IN_IL=1`, template definitions, partial specializations, and instantiation directives live directly in the IL graph. This eliminates the need for a separate template information file (`USE_TEMPLATE_INFO_FILE` is undefined). Combined with `PROTOTYPE_INSTANTIATIONS_IN_IL=1`, the IL retains complete template metadata -- even for function templates that have been declared but not yet instantiated. This is essential for CUDA's device/host separation, where a template might be instantiated in different execution spaces.

## Internal Checking

NVIDIA builds cudafe++ with assertions enabled. This produces a binary with extensive runtime self-checking.

| Constant | Value | Meaning |
|---|---|---|
| `CHECKING` | `1` | Internal assertion macros are active |
| `DEBUG` | `1` | Debug-mode code paths are compiled in |
| `CHECK_SWITCH_DEFAULT_UNEXPECTED` | `1` | Default cases in switch statements trigger assertions |
| `EXPENSIVE_CHECKING` | `0` | Costly O(n) verification checks are disabled |
| `OVERWRITE_FREED_MEM_BLOCKS` | `0` | No memory poisoning on free |
| `EXIT_ON_INTERNAL_ERROR` | `0` | Internal errors do not call exit() directly |
| `ABORT_ON_INIT_COMPONENT_LEAKAGE` | `0` | No abort on init-time leaks |
| `TRACK_INTERPRETER_ALLOCATIONS` | `0` | constexpr interpreter does not track allocations |

### Assertion Infrastructure

With `CHECKING=1`, the internal assertion macro `internal_error` (`sub_4F2930`) is live. The binary contains 5,178 call sites across 2,139 functions that invoke this handler. Each call site passes the source file name, line number, function name, and a diagnostic message pair. When an assertion fires, the handler constructs error 2656 with severity level 11 (catastrophic) and reports it through the standard diagnostic infrastructure.

The `DEBUG=1` setting enables additional code paths that perform intermediate consistency checks during parsing and IL construction. These checks are less expensive than `EXPENSIVE_CHECKING` (which is off) but still add measurable overhead to compilation time. NVIDIA presumably leaves both `CHECKING` and `DEBUG` on because cudafe++ is a critical toolchain component where silent corruption is far worse than a slightly slower compilation.

The `CHECK_SWITCH_DEFAULT_UNEXPECTED=1` setting means that every `switch` statement in the EDG source that handles enumerated values will trigger an assertion if control reaches the default case. This catches missing case handling when new enum values are added.

## Diagnostics Configuration

These constants control the default formatting and behavior of compiler error messages.

| Constant | Value | Meaning |
|---|---|---|
| `DEFAULT_BRIEF_DIAGNOSTICS` | `0` | Full diagnostics by default (not one-line) |
| `DEFAULT_DISPLAY_ERROR_NUMBER` | `0` | Error numbers hidden by default |
| `COLUMN_NUMBER_IN_BRIEF_DIAGNOSTICS` | `1` | Column numbers included in brief-mode output |
| `DEFAULT_ENABLE_COLORIZED_DIAGNOSTICS` | `1` | ANSI color codes enabled by default |
| `MAX_ERROR_OUTPUT_LINE_LENGTH` | `79` | Diagnostic lines wrap at 79 characters |
| `DEFAULT_CONTEXT_LIMIT` | `10` | Maximum 10 lines of instantiation context shown |
| `DEFAULT_DISPLAY_ERROR_CONTEXT_ON_CATASTROPHE` | `1` | Show context even on fatal errors |
| `DEFAULT_ADD_MATCH_NOTES` | `1` | Add notes explaining overload/template resolution |
| `DEFAULT_DISPLAY_TEMPLATE_TYPEDEFS_IN_DIAGNOSTICS` | `0` | Use raw types, not typedef aliases, in messages |
| `DEFAULT_OUTPUT_MODE` | `om_text` | Default output is text, not SARIF JSON |
| `DEFAULT_MACRO_POSITIONS_IN_DIAGNOSTICS` | (undefined) | Macro expansion position tracking is off |
| `ERROR_SEVERITY_EXPLICIT_IN_ERROR_MESSAGES` | `1` | Severity word ("error"/"warning") always printed |
| `DIRECT_ERROR_OUTPUT_TO_STDOUT` | `0` | Errors go to stderr |
| `WRITE_SIGNOFF_MESSAGE` | `1` | Print summary line at compilation end |

### Color Configuration

The `DEFAULT_EDG_COLORS` constant encodes ANSI SGR (Select Graphic Rendition) color codes for diagnostic categories:

```text
"error=01;31:warning=01;35:note=01;36:locus=01:quote=01:range1=32"
```

| Category | SGR Code | Appearance |
|---|---|---|
| `error` | `01;31` | Bold red |
| `warning` | `01;35` | Bold magenta |
| `note` | `01;36` | Bold cyan |
| `locus` | `01` | Bold (default color) |
| `quote` | `01` | Bold (default color) |
| `range1` | `32` | Green (non-bold) |

This matches GCC's diagnostic color scheme, which is intentional -- cudafe++ is designed to produce diagnostics that look visually consistent with the host GCC compiler's output.

## ABI Configuration

| Constant | Value | Meaning |
|---|---|---|
| `ABI_COMPATIBILITY_VERSION` | `9999` | Maximum ABI compatibility level |
| `IA64_ABI` | `1` | Uses Itanium C++ ABI (standard on Linux) |
| `ABI_CHANGES_FOR_ARRAY_NEW_AND_DELETE` | `1` | Array new/delete ABI changes active |
| `ABI_CHANGES_FOR_CONSTRUCTION_VTBLS` | `1` | Construction vtable ABI changes active |
| `ABI_CHANGES_FOR_COVARIANT_VIRTUAL_FUNC_RETURN` | `1` | Covariant return ABI changes active |
| `ABI_CHANGES_FOR_PLACEMENT_DELETE` | `1` | Placement delete ABI changes active |
| `ABI_CHANGES_FOR_RTTI` | `1` | RTTI ABI changes active |
| `DRIVER_COMPATIBILITY_VERSION` | `9999` | Maximum driver-level compatibility |

The `ABI_COMPATIBILITY_VERSION=9999` is a sentinel meaning "accept all ABI changes." In EDG's versioning scheme, specific ABI compatibility versions can be set to match a particular compiler release (e.g., GCC 3.2's ABI). Setting it to 9999 means cudafe++ uses the latest ABI rules for every construct, which is appropriate because it generates source code that the host compiler will re-ABI anyway.

All five `ABI_CHANGES_FOR_*` constants are set to 1, meaning every ABI improvement EDG has made is active. These affect name mangling, vtable layout, and RTTI representation. Since cudafe++ emits C++ source rather than object code, these primarily affect name mangling output and the structure of compiler-generated entities.

## Compiler Compatibility Layer

cudafe++ emulates GCC by default. These constants configure the compatibility surface.

| Constant | Value | Meaning |
|---|---|---|
| `DEFAULT_GNU_COMPATIBILITY` | `1` | GCC compatibility mode is on by default |
| `DEFAULT_GNU_VERSION` | `80100` | Default GCC version = 8.1.0 |
| `GNU_TARGET_VERSION_NUMBER` | `70300` | Target GCC version = 7.3.0 |
| `DEFAULT_GNU_ABI_VERSION` | `30200` | Default GNU ABI version = 3.2.0 |
| `DEFAULT_CLANG_COMPATIBILITY` | `0` | Clang compat off by default |
| `DEFAULT_CLANG_VERSION` | `90100` | Clang version if enabled = 9.1.0 |
| `DEFAULT_MICROSOFT_COMPATIBILITY` | `0` | MSVC compat off by default |
| `DEFAULT_MICROSOFT_VERSION` | `1926` | MSVC version if enabled = 19.26 (VS 2019) |
| `MSVC_TARGET_VERSION_NUMBER` | `1926` | Same: MSVC 19.26 target |
| `GNU_EXTENSIONS_ALLOWED` | `1` | GNU extensions compiled into the parser |
| `GNU_X86_ASM_EXTENSIONS_ALLOWED` | `1` | GNU inline asm syntax supported |
| `GNU_X86_ATTRIBUTES_ALLOWED` | `1` | GNU `__attribute__` on x86 targets |
| `GNU_VECTOR_TYPES_ALLOWED` | `1` | GNU vector types (`__attribute__((vector_size(...)))`) |
| `GNU_VISIBILITY_ATTRIBUTE_ALLOWED` | `1` | `__attribute__((visibility(...)))` support |
| `GNU_INIT_PRIORITY_ATTRIBUTE_ALLOWED` | `1` | `__attribute__((init_priority(...)))` support |
| `MICROSOFT_EXTENSIONS_ALLOWED` | `0` | MSVC extensions not available |
| `SUN_EXTENSIONS_ALLOWED` | `0` | Sun/Oracle extensions not available |

The `DEFAULT_GNU_VERSION=80100` encodes GCC 8.1.0 as `major*10000 + minor*100 + patch`. This is the baseline GCC version cudafe++ emulates when nvcc does not specify an explicit `--compiler-bindir` host compiler. At runtime, nvcc overrides this with the actual detected host GCC version via `--gnu_version=NNNNN`.

The version numbers stored here serve as fallback defaults. They affect which GNU extensions and builtins are available, which warning behaviors are emulated, and how `__GNUC__` / `__GNUC_MINOR__` / `__GNUC_PATCHLEVEL__` are defined for the preprocessor.

### Disabled Compatibility Modes

| Constant | Value | Meaning |
|---|---|---|
| `CFRONT_2_1_OBJECT_CODE_COMPATIBILITY` | `0` | No AT&T cfront 2.1 compat |
| `CFRONT_3_0_OBJECT_CODE_COMPATIBILITY` | `0` | No AT&T cfront 3.0 compat |
| `CFRONT_GLOBAL_VS_MEMBER_NAME_LOOKUP_BUG` | `0` | No cfront name lookup bug emulation |
| `DEFAULT_SUN_COMPATIBILITY` | (undefined) | No Sun/Oracle compat |
| `CPPCLI_ENABLING_POSSIBLE` | `0` | C++/CLI (managed C++) disabled |
| `CPPCX_ENABLING_POSSIBLE` | `0` | C++/CX (WinRT extensions) disabled |
| `DEFAULT_UPC_MODE` | `0` | Unified Parallel C disabled |
| `DEFAULT_EMBEDDED_C_ENABLED` | `0` | Embedded C extensions disabled |

NVIDIA disables every compatibility mode except GCC. This is consistent with CUDA's host compiler support matrix: GCC and Clang on Linux, MSVC on Windows. The cfront, Sun, UPC, and embedded C modes are EDG capabilities that NVIDIA does not need.

## Target Platform Model

The `TARG_*` constants describe the target architecture's data model. Since cudafe++ is a source-to-source translator for the **host** side, these model x86-64 Linux.

### Data Type Sizes (bytes)

| Type | Size | Alignment |
|---|---|---|
| `char` | 1 | 1 |
| `short` | 2 | 2 |
| `int` | 4 | 4 |
| `long` | 8 | 8 |
| `long long` | 8 | 8 |
| `__int128` | 16 | 16 |
| `pointer` | 8 | 8 |
| `float` | 4 | 4 |
| `double` | 8 | 8 |
| `long double` | 16 | 16 |
| `__float80` | 16 | 16 |
| `__float128` | 16 | 16 |
| `ptr-to-data-member` | 8 | 8 |
| `ptr-to-member-function` | 16 | 8 |
| `ptr-to-virtual-base` | 8 | 8 |

This is the standard LP64 data model (long and pointer are 64-bit). `TARG_ALL_POINTERS_SAME_SIZE=1` confirms there are no near/far pointer distinctions.

### Key Target Properties

| Constant | Value | Meaning |
|---|---|---|
| `TARG_CHAR_BIT` | `8` | 8 bits per byte |
| `TARG_HAS_SIGNED_CHARS` | `1` | `char` is signed by default |
| `TARG_HAS_IEEE_FLOATING_POINT` | `1` | IEEE 754 floating point |
| `TARG_SUPPORTS_X86_64` | `1` | x86-64 target support |
| `TARG_SUPPORTS_ARM64` | `0` | No ARM64 target support |
| `TARG_SUPPORTS_ARM32` | `0` | No ARM32 target support |
| `TARG_DEFAULT_NEW_ALIGNMENT` | `16` | `operator new` returns 16-byte aligned |
| `TARG_IA64_ABI_USE_GUARD_ACQUIRE_RELEASE` | `1` | Thread-safe static local init guards |
| `TARG_CASE_SENSITIVE_EXTERNAL_NAMES` | `1` | Symbol names are case-sensitive |
| `TARG_EXTERNAL_NAMES_GET_UNDERSCORE_ADDED` | `0` | No leading underscore on symbols |

The `TARG_SUPPORTS_ARM64=0` and `TARG_SUPPORTS_ARM32=0` confirm that this build of cudafe++ targets x86-64 Linux only. NVIDIA produces separate cudafe++ builds for other host platforms (ARM64 Linux, Windows).

### Floating Point Model

| Constant | Value | Meaning |
|---|---|---|
| `FP_USE_EMULATION` | `1` | Floating-point constant folding uses software emulation |
| `USE_SOFTFLOAT` | `1` | Software floating-point library linked |
| `APPROXIMATE_QUADMATH` | `1` | `__float128` operations use approximate arithmetic |
| `USE_QUADMATH_LIBRARY` | `0` | Not linked against libquadmath |
| `HOST_FP_VALUE_IS_128BIT` | `1` | Host FP value representation uses 128 bits |
| `FP_LONG_DOUBLE_IS_80BIT_EXTENDED` | `1` | `long double` is x87 80-bit extended precision |
| `FP_LONG_DOUBLE_IS_BINARY128` | `0` | `long double` is not IEEE binary128 |
| `FLOAT80_ENABLING_POSSIBLE` | `1` | `__float80` type can be enabled |
| `FLOAT128_ENABLING_POSSIBLE` | `1` | `__float128` type can be enabled |

The `FP_USE_EMULATION=1` and `USE_SOFTFLOAT=1` settings mean cudafe++ does not use the host CPU's floating-point unit for constant folding during compilation. Instead, it uses a software emulation library. This guarantees deterministic results regardless of the build machine's FPU behavior, rounding mode, or x87 precision settings. The `APPROXIMATE_QUADMATH=1` indicates that `__float128` constant folding uses an approximate (but portable) implementation rather than requiring libquadmath.

## Memory and Host Configuration

| Constant | Value | Meaning |
|---|---|---|
| `USE_MMAP_FOR_MEMORY_REGIONS` | `1` | IL memory regions use `mmap` |
| `USE_MMAP_FOR_MODULES` | `1` | C++ module storage uses `mmap` |
| `HOST_ALLOCATION_INCREMENT` | `65536` | Arena grows in 64 KB increments |
| `HOST_ALIGNMENT_REQUIRED` | `8` | Host requires 8-byte alignment |
| `HOST_IL_ENTRY_PREFIX_ALIGNMENT` | `8` | IL node prefix aligned to 8 bytes |
| `HOST_POINTER_ALIGNMENT` | `8` | Pointer alignment on host platform |
| `USE_FIXED_ADDRESS_FOR_MMAP` | `0` | No fixed mmap addresses |
| `NULL_POINTER_IS_ZERO` | `1` | Null pointer has all-zero bit pattern |

The `USE_MMAP_FOR_MEMORY_REGIONS=1` setting means the IL's region-based arena allocator uses `mmap` system calls (likely `MAP_ANONYMOUS`) rather than `malloc`. This gives EDG more control over memory layout and allows whole-region deallocation via `munmap` without fragmentation concerns. The 64 KB allocation increment (`HOST_ALLOCATION_INCREMENT=65536`) means each arena expansion maps a new 64 KB page-aligned chunk.

## Code Generation Controls

These constants affect what the cp_gen_be backend emits.

| Constant | Value | Meaning |
|---|---|---|
| `GENERATE_SOURCE_SEQUENCE_LISTS` | `1` | Source sequence lists (instantiation ordering) generated |
| `GENERATE_LINKAGE_SPEC_BLOCKS` | `1` | `extern "C"` blocks preserved in output |
| `USING_DECLARATIONS_IN_GENERATED_CODE` | `1` | `using` declarations appear in output |
| `GENERATE_EH_TABLES` | `0` | No EH tables -- host compiler handles exceptions |
| `GENERATE_MICROSOFT_IF_EXISTS_ENTRIES` | `0` | No `__if_exists` / `__if_not_exists` output |
| `SUPPRESS_ARRAY_STATIC_IN_GENERATED_CODE` | `1` | `static` in array parameter declarations suppressed |
| `GCC_BUILTIN_VARARGS_IN_GENERATED_CODE` | `0` | No GCC `__builtin_va_*` in output |
| `USE_HEX_FP_CONSTANTS_IN_GENERATED_CODE` | `0` | No hex float literals in output |
| `ADD_BRACES_TO_AVOID_DANGLING_ELSE_IN_GENERATED_C` | `0` | No extra braces for dangling else |
| `DOING_SOURCE_ANALYSIS` | `1` | Source analysis mode (affects what is preserved) |

The `GENERATE_EH_TABLES=0` is significant. Exception handling tables are not generated because cudafe++ emits source code -- the host compiler is responsible for generating the actual EH tables when it compiles the `.ii` output. Similarly, `GCC_BUILTIN_VARARGS_IN_GENERATED_CODE=0` means the output uses standard `<stdarg.h>` varargs rather than GCC builtins, keeping the output compiler-portable.

## Template and Instantiation Model

| Constant | Value | Meaning |
|---|---|---|
| `AUTOMATIC_TEMPLATE_INSTANTIATION` | `0` | No automatic instantiation to separate files |
| `INSTANTIATION_BY_IMPLICIT_INCLUSION` | `1` | Template definitions found via implicit include |
| `INSTANTIATE_TEMPLATES_EVERYWHERE_USED` | `0` | Not every use triggers instantiation |
| `INSTANTIATE_EXTERN_INLINE` | `0` | Extern inline templates not instantiated eagerly |
| `INSTANTIATE_INLINE_VARIABLES` | `0` | Inline variables not instantiated eagerly |
| `INSTANTIATE_BEFORE_PCH_CREATION` | `0` | No instantiation before PCH |
| `DEFAULT_INSTANTIATION_MODE` | `tim_none` | No separate instantiation mode |
| `DEFAULT_MAX_PENDING_INSTANTIATIONS` | `200` | Maximum pending instantiations per TU |
| `MAX_TOTAL_PENDING_INSTANTIATIONS` | `256` | Hard cap on total pending |
| `MAX_UNUSED_ALL_MODE_INSTANTIATIONS` | `200` | Limit on unused instantiation entries |
| `DEFAULT_MAX_DEPTH_CONSTEXPR_CALL` | `256` | Maximum constexpr recursion depth |
| `DEFAULT_MAX_COST_CONSTEXPR_CALL` | `2000000` | Maximum constexpr evaluation cost |

The `AUTOMATIC_TEMPLATE_INSTANTIATION=0` and `DEFAULT_INSTANTIATION_MODE=tim_none` disable EDG's automatic template instantiation mechanism. This mechanism (where EDG writes instantiation requests to a file for later processing) is unnecessary because cudafe++ processes each translation unit in a single pass -- templates are instantiated inline as the parser encounters them, and the backend emits the instantiated code directly.

## Feature Enablement Constants

The `DEFAULT_*` constants set the initial values of runtime-configurable features. These can be overridden by command-line flags, but they establish the baseline behavior when no flags are specified.

### Enabled by Default

| Constant | Value | Feature |
|---|---|---|
| `DEFAULT_GNU_COMPATIBILITY` | `1` | GCC compatibility mode |
| `DEFAULT_EXCEPTIONS_ENABLED` | `1` | C++ exception handling |
| `DEFAULT_RTTI_ENABLED` | `1` | Runtime type identification |
| `DEFAULT_BOOL_IS_KEYWORD` | `1` | `bool` is a keyword (not a typedef) |
| `DEFAULT_WCHAR_T_IS_KEYWORD` | `1` | `wchar_t` is a keyword |
| `DEFAULT_NAMESPACES_ENABLED` | `1` | Namespaces are supported |
| `DEFAULT_ARG_DEPENDENT_LOOKUP` | `1` | ADL (Koenig lookup) active |
| `DEFAULT_CLASS_NAME_INJECTION` | `1` | Class name injected into its own scope |
| `DEFAULT_EXPLICIT_KEYWORD_ENABLED` | `1` | `explicit` keyword recognized |
| `DEFAULT_EXTERN_INLINE_ALLOWED` | `1` | `extern inline` permitted |
| `DEFAULT_IMPLICIT_NOEXCEPT_ENABLED` | `1` | Implicit noexcept on dtors/deallocs |
| `DEFAULT_IMPLICIT_TYPENAME_ENABLED` | `1` | `typename` implicit in dependent contexts |
| `DEFAULT_TYPE_TRAITS_HELPERS_ENABLED` | `1` | Compiler intrinsic type traits |
| `DEFAULT_STRING_LITERALS_ARE_CONST` | `1` | String literals have const type |
| `DEFAULT_TYPE_INFO_IN_NAMESPACE_STD` | `1` | `type_info` in `std::` |
| `DEFAULT_C_AND_CPP_FUNCTION_TYPES_ARE_DISTINCT` | `1` | C and C++ function types differ |
| `DEFAULT_FRIEND_INJECTION` | `1` | Friend declarations inject names |
| `DEFAULT_DISTINCT_TEMPLATE_SIGNATURES` | `1` | Template signatures are distinct |
| `DEFAULT_ARRAY_NEW_AND_DELETE_ENABLED` | `1` | `operator new[]` / `operator delete[]` |
| `DEFAULT_CPP11_DEPENDENT_NAME_PROCESSING` | `1` | C++11-style dependent name processing |
| `DEFAULT_ENABLE_COLORIZED_DIAGNOSTICS` | `1` | ANSI color in diagnostics |
| `DEFAULT_CHECK_FOR_BYTE_ORDER_MARK` | `1` | UTF-8 BOM detection on |
| `DEFAULT_CHECK_PRINTF_SCANF_POSITIONAL_ARGS` | `1` | printf/scanf format checking |
| `DEFAULT_ALWAYS_FOLD_CALLS_TO_BUILTIN_CONSTANT_P` | `1` | `__builtin_constant_p` folded |

### Disabled by Default (Require Explicit Enabling)

| Constant | Value | Feature |
|---|---|---|
| `DEFAULT_CPP_MODE` | `199711` | Default language standard is C++98 |
| `DEFAULT_LAMBDAS_ENABLED` | `0` | Lambdas off (enabled by C++ version selection) |
| `DEFAULT_RVALUE_REFERENCES_ENABLED` | `0` | Rvalue refs off (enabled by C++ version) |
| `DEFAULT_VARIADIC_TEMPLATES_ENABLED` | `0` | Variadic templates off (enabled by C++ version) |
| `DEFAULT_NULLPTR_ENABLED` | `0` | `nullptr` off (enabled by C++ version) |
| `DEFAULT_RANGE_BASED_FOR_ENABLED` | `0` | Range-for off (enabled by C++ version) |
| `DEFAULT_AUTO_TYPE_SPECIFIER_ENABLED` | `0` | `auto` type deduction off (enabled by C++ version) |
| `DEFAULT_COMPOUND_LITERALS_ALLOWED` | `0` | C99 compound literals off |
| `DEFAULT_DESIGNATORS_ALLOWED` | `0` | C99/C++20 designated initializers off |
| `DEFAULT_C99_MODE` | `0` | Not in C99 mode |
| `DEFAULT_VLA_ENABLED` | `0` | Variable-length arrays off |
| `DEFAULT_CPP11_SFINAE_ENABLED` | `0` | C++11 SFINAE rules off (enabled by C++ version) |
| `DEFAULT_MODULES_ENABLED` | `0` | C++20 modules off |
| `DEFAULT_REFLECTION_ENABLED` | `0` | C++ reflection off |
| `DEFAULT_MICROSOFT_COMPATIBILITY` | `0` | MSVC compat off |
| `DEFAULT_CLANG_COMPATIBILITY` | `0` | Clang compat off |
| `DEFAULT_BRIEF_DIAGNOSTICS` | `0` | Full diagnostic output |
| `DEFAULT_DISPLAY_ERROR_NUMBER` | `0` | Error numbers hidden |
| `DEFAULT_INCOGNITO` | `0` | Not in incognito mode |
| `DEFAULT_REMOVE_UNNEEDED_ENTITIES` | `0` | Dead code not removed |

The `DEFAULT_CPP_MODE=199711` (C++98) looks surprising, but this is simply the EDG default. In practice, nvcc always passes an explicit `--std=c++NN` flag to cudafe++ that overrides this default, typically `--std=c++17` in modern CUDA. The C++11/14/17/20 features listed as "disabled by default" are all enabled by the standard version selection code in `proc_command_line`.

## Predefined Macro Constants

These constants control which macros cudafe++ automatically defines for the preprocessor.

| Constant | Value | Effect |
|---|---|---|
| `DEFINE_MACRO_WHEN_EXCEPTIONS_ENABLED` | `1` | `--exceptions` causes `#define __EXCEPTIONS` |
| `DEFINE_MACRO_WHEN_RTTI_ENABLED` | `1` | `--rtti` causes `#define __RTTI` |
| `DEFINE_MACRO_WHEN_BOOL_IS_KEYWORD` | `1` | `bool` keyword causes `#define _BOOL` |
| `DEFINE_MACRO_WHEN_WCHAR_T_IS_KEYWORD` | `1` | `wchar_t` keyword causes `#define _WCHAR_T` |
| `DEFINE_MACRO_WHEN_ARRAY_NEW_AND_DELETE_ENABLED` | `1` | Causes `#define __ARRAY_OPERATORS` |
| `DEFINE_MACRO_WHEN_PLACEMENT_DELETE_ENABLED` | `1` | Causes `#define __PLACEMENT_DELETE` |
| `DEFINE_MACRO_WHEN_VARIADIC_TEMPLATES_ENABLED` | `1` | Causes `#define __VARIADIC_TEMPLATES` |
| `DEFINE_MACRO_WHEN_CHAR16_T_AND_CHAR32_T_ARE_KEYWORDS` | `1` | Causes `#define __CHAR16_T_AND_CHAR32_T` |
| `DEFINE_MACRO_WHEN_LONG_LONG_IS_DISABLED` | `1` | Causes `#define __NO_LONG_LONG` when long long is off |
| `DEFINE_FEATURE_TEST_MACRO_OPERATORS_IN_ALL_MODES` | `1` | Feature test macros available in all modes |
| `MACRO_DEFINED_WHEN_IA64_ABI` | `"__EDG_IA64_ABI"` | Always defined (since `IA64_ABI=1`) |
| `MACRO_DEFINED_WHEN_TYPE_TRAITS_HELPERS_ENABLED` | `"__EDG_TYPE_TRAITS_ENABLED"` | Always defined (since type traits are on) |

These macros allow header files to conditionally compile based on which compiler features are active. They are part of EDG's mechanism for compatibility with GCC's predefined macro surface -- GCC defines `__EXCEPTIONS` when exceptions are on, so cudafe++ does the same.

## Miscellaneous Constants

| Constant | Value | Meaning |
|---|---|---|
| `VERSION_NUMBER` | `"6.6"` | EDG front end version |
| `VERSION_NUMBER_FOR_MACRO` | `606` | Numeric form for `__EDG_VERSION__` macro |
| `DIRECTORY_SEPARATOR` | `'/'` | Unix path separator |
| `FILE_NAME_FOR_STDIN` | `"-"` | Standard Unix convention for stdin |
| `OBJECT_FILE_SUFFIX` | `".o"` | Unix object file suffix |
| `PCH_FILE_SUFFIX` | `".pch"` | Precompiled header suffix |
| `PREDEFINED_MACRO_FILE_NAME` | `"predefined_macros.txt"` | File with platform-defined macros |
| `DEFAULT_TMPDIR` | `"/tmp"` | Default temp directory |
| `DEFAULT_USR_INCLUDE` | `"/usr/include"` | Default system include path |
| `DEFAULT_EDG_BASE` | `""` | EDG base directory (empty = use argv[0] path) |
| `MAX_INCLUDE_FILES_OPEN_AT_ONCE` | `8` | Limit on simultaneously open include files |
| `MODULE_MAX_LINE_NUMBER` | `250000` | Maximum source lines per module |
| `COMPILE_MULTIPLE_SOURCE_FILES` | `0` | One source file per invocation |
| `COMPILE_MULTIPLE_TRANSLATION_UNITS` | `0` | One TU per invocation |
| `USING_DRIVER` | `0` | Not integrated into a driver binary |
| `EDG_WIN32` | `0` | Not a Windows build |
| `WINDOWS_PATHS_ALLOWED` | `0` | No backslash path separators |

The `VERSION_NUMBER="6.6"` identifies this as EDG C/C++ front end version 6.6, which is the latest major release. `VERSION_NUMBER_FOR_MACRO=606` becomes the `__EDG_VERSION__` predefined macro, allowing header files to detect the exact EDG version (e.g., `#if __EDG_VERSION__ >= 606`).

The legacy configuration section at the bottom of the dump output reports `LEGACY_TARGET_CONFIGURATION_NAME` as `NULL`, meaning this build does not use a named legacy target configuration. In EDG's framework, named target configurations are used to preset constants for specific compilers (e.g., "gnu" or "microsoft"). NVIDIA's configuration is fully custom and does not map to any of EDG's predefined configurations.

## Relationship Between Build Configuration and Runtime Flags

The build configuration constants and the [runtime CLI flags](cli-flags.md) form a two-layer system:

1. **Build-time constants** (`CHECKING=1`, `BACK_END_IS_CP_GEN_BE=1`, `IL_SHOULD_BE_WRITTEN_TO_FILE=0`) determine what code paths exist in the binary. If `IL_SHOULD_BE_WRITTEN_TO_FILE=0`, the IL serialization code is not compiled in -- no runtime flag can enable it.

2. **`DEFAULT_*` constants** set initial values for features that can be toggled at runtime. `DEFAULT_EXCEPTIONS_ENABLED=1` means exceptions are on unless `--no_exceptions` is passed. These defaults are loaded by `default_init` (`sub_45EB40`) before command-line parsing.

3. **`*_ENABLING_POSSIBLE` constants** gate whether a feature can be toggled at all. `COROUTINE_ENABLING_POSSIBLE=1` means the `--coroutines` / `--no_coroutines` flag pair is registered. `REFLECTION_ENABLING_POSSIBLE=0` means the reflection flag pair is not even registered -- the feature cannot be turned on.

This layering means the build configuration determines the binary's permanent capabilities, while the CLI flags select among the enabled possibilities.

## Function Reference

| Function | Address | Lines | Role |
|---|---|---|---|
| `dump_configuration` | `sub_44CF30` | 785 | Print all 747 constants as `#define` statements |
| `default_init` | `sub_45EB40` | 470 | Initialize 350 config globals from `DEFAULT_*` values |
| `init_command_line_flags` | `sub_452010` | 3,849 | Register all CLI flags (gated by `*_ENABLING_POSSIBLE`) |
| `proc_command_line` | `sub_459630` | 4,105 | Parse flags and override `DEFAULT_*` settings |
