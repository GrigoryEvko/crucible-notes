# EDG Build-Time `#define` Surface

cicc embeds the **complete Edison Design Group front-end configuration** as a single hand-rolled `case 0xE1u:` block inside the option dispatcher `sub_617BD0` at `0x617BD0`. When the dispatcher is fed option index `0xE1` (the `--gen_config` operation, paired with the file-open setter `0xE0` that writes `dword_4D04944=1`, `dword_4D0493C=0`, `dword_4D04910=1` and rebinds `qword_4F07510` away from `stderr`), it emits **748 preprocessor directives** to the sink file pointer at `0x4F07510` — 558 numeric `#define X N` entries, 85 string/enum `#define X "s"` entries, 103 informational `/* X not defined */` comments, and 2 narrative banner lines. The directives reproduce the *exact* `INCLUDE/edg_*_cfg.h` header that EDG ships to downstream tooling, so a third party can re-link `front_end.a` against an identically-configured driver. Every value is a compile-time constant of the cicc binary itself; nothing here is reachable from the command line at runtime — these are bake-time switches that froze the EDG personality when cicc was linked, and the dump operation is the only way to recover them post-hoc.

The 643-entry list provided to this page (`/tmp/build_defines.txt`) is the deduplicated subset of identifiers reachable via uppercase-only string scan and consequently omits the 105 macros that are emitted as `/* ... not defined */` comments. Both halves matter: the negative space (what EDG explicitly disabled) is as load-bearing for ABI matching as the positive defines, so this page documents the full 748-line surface but anchors the categorical buckets on the 643 enabled identifiers.

## Dispatcher Entry Point and Output Sink

| Element | Address / Symbol | Notes |
|---|---|---|
| Dispatcher function | `sub_617BD0` (`0x617BD0`) | Main EDG long-form option parser; opcode tables run `0x01u`-`0x150u` |
| Trigger opcode | `case 0xE1u` (line 1192378 of `cicc_full.c`) | One of two opcodes that touch the config sink; the other is `0xE0u` |
| Sink-setter opcode | `case 0xE0u` (line 1192373) | Sets `dword_4D04944=1` (enable dump), zeros `dword_4D0493C`, sets `dword_4D04910=1` |
| Output stream | `FILE *qword_4F07510` (`0x4F07510`) | `weak` symbol, initialised to `stderr` in `init_globals`; option `--gen_config_file <path>` (opcode `0xC9u`, not shown here) rebinds it via `sub_685E40` |
| Trailer call | `sub_88CDC0` (`0x88CDC0`) | Emits 2-line `LEGACY_TARGET_CONFIGURATION_NAME NULL` epilogue via `fwrite` |
| Companion opcode | `case 0xE2u` calls `sub_88BDE0(src)` | Dumps a *named* legacy target config; pairs with the EDG `--target_config` mechanism |
| Total fprintf sites against sink | 936 | Of which 748 live inside `case 0xE1u` itself; the rest are diagnostic emitters scattered across `sub_67EA10`, `sub_67EB40`, `sub_681D20` |

The dispatcher fragment is structurally a single 753-line straight-line `case` ending in `sub_88CDC0(); v308 = 1; continue;`. There is no loop, no table lookup, no string sort: every define is a literal `fprintf(qword_4F07510, "#define %s %td\n", "X", N)` instruction emitted in source order. This is one of the largest single basic blocks in cicc (>20 KiB of unique fmt-strings) and is the primary reason `cicc_full.c` exceeds 11 M lines after IDA decompilation.

> **CONFIDENCE:** High for entry point, sink address, opcode, and the 748 emitted strings (extracted directly from decompiled `case 0xE1u`). Medium for the *semantics* of individual switches — names are self-describing in most cases, but a handful (`TARG_LIBGCC_CMP_RETURN_MODE`, `BUILTIN_VA_LIST_OVERRIDE_TYPE_NAME` value `"__gnuc_va_list"`) require EDG documentation to interpret.

## Category Map (643 enabled defines)

The identifiers cluster into eight families by prefix. Counts below sum to the 643 enabled defines; the additional 105 `not defined` placeholders mostly belong to `TARG_*` (fixed-point types, far-pointer arithmetic) and `DEFAULT_*` (rare modes like `DEFAULT_AUTOMATIC_INSTANTIATION_MODE`).

| Family | Count | Pattern | Purpose |
|---|---:|---|---|
| `ABI_*`, `__GXX_ABI*` | ~6 | ABI compatibility versioning | C++ ABI shape, RTTI/vtable layout |
| `TARG_*` | ~106 | Target-machine attributes | Sizes, alignments, endian, FP format, char encodings |
| `DEFAULT_*` | ~130 | Default language-mode toggles | What the front end does in absence of CLI override |
| `*_ENABLING_POSSIBLE` | ~16 | Feature *capability* gates | "Can this be turned on at all?" — separate from defaults |
| `*_ALLOWED` | ~25 | Dialect permissions | Static grammar permission set |
| `MACRO_DEFINED_WHEN_*`, `DEFINE_MACRO_WHEN_*` | ~17 | Macro-emission policies | Whether `__cpp_*` or `__GXX_*` macros are predefined |
| `LOWER_*`, `*_LOWERING_*`, `IL_*` | ~24 | IL/lowering pipeline switches | What gets transformed before backend handoff |
| `GCC_*`, `CLANG_*`, `MSVC_*`, `MICROSOFT_*`, `IA64_*`, `CFRONT_*` | ~38 | Emulation-mode flags | Per-compiler-personality behaviours |
| `HOST_*` | ~7 | Host-machine attributes | What the compiler runs on, not what it targets |
| `EDG_*`, `USING_*`, `BACK_END_*`, `FRONT_END_*` | ~10 | Driver/integration identity | Self-identification of the EDG instance |
| Diagnostics / instrumentation | ~25 | `DEBUG`, `CHECKING`, `EXPENSIVE_CHECKING`, `DUMP_CONFIG_ENABLED`, `TRACK_INTERPRETER_ALLOCATIONS`, … | Compile-time instrumentation hooks |
| Other / catch-all | ~239 | One-off identifiers | Token handling, paths, numeric limits, niche features |

Each category is detailed below with 5-15 representative keys and their *baked* values as observed in this cicc build.

### 1. ABI compatibility (6 keys)

These freeze the C++ ABI shape. The values reflect the Itanium ABI (`IA64_ABI = 1`) plus EDG's compatibility-version dial.

| Key | Value | Notes |
|---|---:|---|
| `ABI_CHANGES_FOR_ARRAY_NEW_AND_DELETE` | 1 | Distinct `[]` operator pairing |
| `ABI_CHANGES_FOR_CONSTRUCTION_VTBLS` | 1 | Construction-vtable layout |
| `ABI_CHANGES_FOR_COVARIANT_VIRTUAL_FUNC_RETURN` | 1 | Covariant return thunks |
| `ABI_CHANGES_FOR_PLACEMENT_DELETE` | 1 | Matching placement `delete` |
| `ABI_CHANGES_FOR_RTTI` | 1 | Itanium RTTI vtable slot |
| `ABI_COMPATIBILITY_VERSION` | **9999** | Tracks latest; see QUIRK 1 |
| `IA64_ABI` | 1 | Selects Itanium ABI personality |
| `DRIVER_COMPATIBILITY_VERSION` | 9999 | Mirrors the EDG dial |
| `DEFAULT_GNU_ABI_VERSION` | (CLI-driven, see DEFAULT_GNU_VERSION) | g++ `-fabi-version` default |

### 2. Target attributes — `TARG_*` (~106 keys)

The largest family. Encodes the data model. cicc targets x86-64 (LP64) host-side and emits to a separate device pipeline; the values below are the *host* side that the EDG front-end uses for sizeof/alignof in constant evaluation.

| Key | Value | Meaning |
|---|---:|---|
| `TARG_CHAR_BIT` | 8 | Bits per byte |
| `TARG_LITTLE_ENDIAN` | 1 | Endian |
| `TARG_HAS_IEEE_FLOATING_POINT` | 1 | IEEE-754 |
| `TARG_HAS_SIGNED_CHARS` | 1 | `char` is `signed char` |
| `TARG_NULL_IS_ALL_BITS_ZERO` | 1 | `nullptr_t` representation |
| `TARG_SIZEOF_INT` | 4 | LP64 int |
| `TARG_SIZEOF_LONG` | 8 | LP64 long |
| `TARG_SIZEOF_LONG_LONG` | 8 | `long long` |
| `TARG_SIZEOF_POINTER` | 8 | 64-bit |
| `TARG_SIZEOF_SHORT` | 2 | |
| `TARG_SIZEOF_LARGEST_INTEGER` | 16 | `__int128` width |
| `TARG_SIZEOF_LARGEST_ATOMIC` | 16 | DCAS available |
| `TARG_SIZEOF_FLOAT` | 4 | |
| `TARG_SIZEOF_DOUBLE` | 8 | |
| `TARG_SIZEOF_LONG_DOUBLE` | 16 | x86-64 80-bit padded |
| `TARG_SIZEOF_FLOAT80` | 16 | Padded `__float80` |
| `TARG_SIZEOF_FLOAT128` | 16 | `_Float128`/`__float128` |
| `TARG_ALIGNOF_POINTER` | 8 | |
| `TARG_ALIGNOF_LONG_DOUBLE` | 16 | |
| `TARG_DBL_MANT_DIG` | 53 | IEEE-754 binary64 |
| `TARG_LDBL_MANT_DIG` | 64 | x87 extended |
| `TARG_FLT128_MANT_DIG` | 113 | IEEE-754 binary128 |
| `TARG_FLT128_MAX_EXP` | 0x4000 (16384) | |
| `TARG_SUPPORTS_X86_64` | **1** | |
| `TARG_SUPPORTS_ARM64` | **0** | See QUIRK 2 |
| `TARG_SUPPORTS_ARM32` | 0 | |
| `TARG_BOOL_INT_KIND` | `((an_integer_kind)ik_char)` | String-valued; `bool` shape |

The 26 `TARG_*_FIXED_POINT`, `TARG_*_NEAR_POINTER`, `TARG_*_FAR_POINTER`, and `TARG_*_SIGNED_ACCUM` keys are all emitted as `/* X not defined */` — EDG declares them in its master header set but they have no meaning on x86-64.

### 3. Default language-mode toggles — `DEFAULT_*` (~130 keys)

These are the *initial* values of EDG runtime feature flags before any `--c++14` / `--gnu++17` / `-std=gnu++20`-equivalent CLI option flips them. Numeric values for version dials are encoded as `MMmmpp`-style integers.

| Key | Value | Meaning |
|---|---:|---|
| `DEFAULT_CPP_MODE` | 1 | Source is C++ by default |
| `DEFAULT_C99_MODE` | 1 | C is C99 (cf. ANSI C) |
| `DEFAULT_GNU_COMPATIBILITY` | 1 | g++ personality on by default |
| `DEFAULT_GNU_VERSION` | **80100** | g++ 8.1.0 emulation; see QUIRK 3 |
| `DEFAULT_CLANG_VERSION` | 90100 | clang 9.1.0 emulation |
| `DEFAULT_CLANG_COMPATIBILITY` | 0 | …but not enabled by default |
| `DEFAULT_MICROSOFT_VERSION` | 1926 | MSVC 19.26 (VS 2019 v16.6) |
| `DEFAULT_MICROSOFT_COMPATIBILITY` | 0 | Off |
| `DEFAULT_MICROSOFT_EXTENSIONS` | 0 | Off |
| `DEFAULT_BOOL_IS_KEYWORD` | 1 | C++ default |
| `DEFAULT_WCHAR_T_IS_KEYWORD` | 1 | |
| `DEFAULT_NAMESPACES_ENABLED` | 1 | |
| `DEFAULT_RTTI_ENABLED` | 1 | |
| `DEFAULT_EXCEPTIONS_ENABLED` | 1 | |
| `DEFAULT_LAMBDAS_ENABLED` | 1 | C++11 lambdas |
| `DEFAULT_VARIADIC_TEMPLATES_ENABLED` | 1 | |
| `DEFAULT_RVALUE_REFERENCES_ENABLED` | 1 | |
| `DEFAULT_NULLPTR_ENABLED` | 1 | |
| `DEFAULT_RANGE_BASED_FOR_ENABLED` | 1 | |
| `DEFAULT_AUTO_TYPE_SPECIFIER_ENABLED` | 0 | C++11 `auto` off by default — see QUIRK 4 |
| `DEFAULT_MODULES_ENABLED` | 0 | C++20 modules |
| `DEFAULT_REFLECTION_ENABLED` | 0 | Static reflection (TS) |
| `DEFAULT_VLA_ENABLED` | 1 | C99 VLAs |
| `DEFAULT_RESTRICT_ENABLED` | 1 | C99 `restrict` keyword |
| `DEFAULT_MAX_PENDING_INSTANTIATIONS` | 200 | Template depth |
| `DEFAULT_CONTEXT_LIMIT` | 10 | Diagnostic context stack |
| `DEFAULT_INLINE_STATEMENT_LIMIT` | 100 | Inliner heuristic |
| `DEFAULT_MAX_MANGLED_NAME_LENGTH` | (large; mangler bound) | |
| `DEFAULT_MAX_COST_CONSTEXPR_CALL` | (limit) | Constexpr engine budget |
| `DEFAULT_MAX_DEPTH_CONSTEXPR_CALL` | (limit) | Constexpr recursion |
| `DEFAULT_TMPDIR` | `"/tmp"` | String-valued |
| `DEFAULT_USR_INCLUDE` | `"/usr/include"` | |
| `DEFAULT_INCLUDE_FILE_SUFFIX_LIST` | `"::stdh:"` | |
| `DEFAULT_INSTANTIATION_FILE_SUFFIX_LIST` | `"c:C:cpp:CPP:cxx:CXX:cc"` | |
| `DEFAULT_MSVC_EXECUTION_CHARACTER_SET` | `"1252"` | Windows-1252 |
| `DEFAULT_UNICODE_SOURCE_KIND` | `usk_utf8` | Enum value |
| `DEFAULT_OUTPUT_MODE` | `om_text` | Enum value |
| `DEFAULT_INSTANTIATION_MODE` | `tim_none` | Enum value |
| `DEFAULT_EDG_BASE` | `""` | |
| `DEFAULT_EDG_COLORS` | `"error=01;31:warning=01;35:note=01;36:locus=01:quote=01:range1=32"` | ANSI escape set |
| `DEFAULT_FLOAT_KIND_FOR_FLOAT80` | `fk_long_double` | |
| `DEFAULT_FLOAT_KIND_FOR_FLOAT128` | `fk_float128` | |

### 4. Feature capability gates — `*_ENABLING_POSSIBLE` (~16 keys)

Distinct from `DEFAULT_*_ENABLED`. A `_POSSIBLE` value of `0` hard-disables the feature regardless of CLI flags — the front end will reject `-fexceptions` if `EXC_SPEC_IN_FUNC_TYPE_ENABLING_POSSIBLE = 0`. All `_POSSIBLE` keys in this cicc build are `1`.

| Key | Value |
|---|---:|
| `ARRAY_NEW_AND_DELETE_ENABLING_POSSIBLE` | 1 |
| `BOOL_ENABLING_POSSIBLE` | 1 |
| `COMPOUND_LITERAL_ENABLING_POSSIBLE` | 1 |
| `COROUTINE_ENABLING_POSSIBLE` | 1 |
| `CPPCLI_ENABLING_POSSIBLE` | 0 |
| `CPPCX_ENABLING_POSSIBLE` | 0 |
| `EXC_SPEC_IN_FUNC_TYPE_ENABLING_POSSIBLE` | 1 |
| `EXPORT_ENABLING_POSSIBLE` | 1 |
| `FLOAT128_ENABLING_POSSIBLE` | 1 |
| `FLOAT80_ENABLING_POSSIBLE` | 1 |
| `REFLECTION_ENABLING_POSSIBLE` | 1 |
| `RTTI_ENABLING_POSSIBLE` | 1 |
| `WCHAR_T_ENABLING_POSSIBLE` | 1 |

### 5. Compiler-emulation flags

| Key | Value | Notes |
|---|---:|---|
| `GNU_TARGET_VERSION_NUMBER` | (build-fixed) | Macro `__GNUC__` source |
| `MIN_GNU_VERSION` | 30200 | gcc 3.2 floor |
| `GNU_EXTENSIONS_ALLOWED` | 1 | |
| `GNU_VECTOR_TYPES_ALLOWED` | 1 | `__attribute__((vector_size))` |
| `GNU_X86_ASM_EXTENSIONS_ALLOWED` | 1 | GAS-style inline asm |
| `GNU_X86_ATTRIBUTES_ALLOWED` | 1 | |
| `GNU_NAKED_ATTRIBUTE_ALLOWED` | 1 | |
| `GNU_VISIBILITY_ATTRIBUTE_ALLOWED` | 1 | |
| `GNU_INIT_PRIORITY_ATTRIBUTE_ALLOWED` | 1 | |
| `GNU_FUNCTION_MULTIVERSIONING` | 1 | `target_clones` |
| `GCC_BUILTIN_VARARGS` | 1 | `__builtin_va_*` |
| `GCC_BUILTIN_VARARGS_IN_GENERATED_CODE` | 1 | |
| `GCC_IS_GENERATED_CODE_TARGET` | 1 | C-gen backend emits g++-flavoured C |
| `CLANG_IS_GENERATED_CODE_TARGET` | 0 | |
| `MSVC_IS_GENERATED_CODE_TARGET` | 0 | |
| `MICROSOFT_DIALECT_IS_GENERATED_CODE_TARGET` | 0 | |
| `MICROSOFT_EXTENSIONS_ALLOWED` | 1 | But default off |
| `RECOGNIZE_MICROSOFT_ATTRIBUTES` | 1 | |
| `CFRONT_2_1_OBJECT_CODE_COMPATIBILITY` | 0 | Antiquated |
| `CFRONT_3_0_OBJECT_CODE_COMPATIBILITY` | 0 | Antiquated |
| `CFRONT_GLOBAL_VS_MEMBER_NAME_LOOKUP_BUG` | 0 | Bug-emulation toggle |

### 6. Macro emission policies — `MACRO_DEFINED_WHEN_*` / `DEFINE_MACRO_WHEN_*`

Controls whether the *predefined macros* (the things visible to user code as `__cpp_*`, `__GXX_*`, etc.) are emitted into the preprocessing context when a feature is active.

| Key | Value |
|---|---:|
| `MACRO_DEFINED_WHEN_BOOL_IS_KEYWORD` | 1 |
| `MACRO_DEFINED_WHEN_RTTI_ENABLED` | 1 |
| `MACRO_DEFINED_WHEN_EXCEPTIONS_ENABLED` | 1 |
| `MACRO_DEFINED_WHEN_VARIADIC_TEMPLATES_ENABLED` | 1 |
| `MACRO_DEFINED_WHEN_TYPE_TRAITS_HELPERS_ENABLED` | 1 |
| `MACRO_DEFINED_WHEN_IA64_ABI` | 1 |
| `MACRO_DEFINED_WHEN_IA64_CTORS_DTORS_RETURN_THIS` | 1 |
| `MACRO_DEFINED_WHEN_RUNTIME_USES_NAMESPACES` | 1 |
| `MACRO_DEFINED_WHEN_LONG_LONG_IS_DISABLED` | 0 |
| `DEFINE_MACRO_WHEN_PLACEMENT_DELETE_ENABLED` | 1 |
| `DEFINE_FEATURE_TEST_MACRO_OPERATORS_IN_ALL_MODES` | 1 |
| `DEFINE_STDC_IN_MICROSOFT_MODE` | 0 |

### 7. IL / lowering pipeline switches

These instruct the EDG-to-NVVM lowering layer (run before `cicc` hands off to its LLVM core) what to expand vs. preserve.

| Key | Value | Meaning |
|---|---:|---|
| `DO_IL_LOWERING` | 1 | Master switch |
| `DO_FULL_PORTABLE_EH_LOWERING` | 1 | C++ EH expanded to portable form |
| `DO_UNORDERED_EH_PROCESSING` | 0 | |
| `DO_RETURN_VALUE_OPTIMIZATION_IN_LOWERING` | 1 | RVO at IL level |
| `LOWER_CLASS_RVALUE_ADJUST` | 1 | |
| `LOWER_COMPLEX` | 1 | `_Complex` → struct |
| `LOWER_DESIGNATED_INITIALIZERS` | 1 | C99 `.field =` |
| `LOWER_EXTERN_INLINE` | 1 | |
| `LOWER_FIXED_POINT` | 1 | But emit; see fixed-point notes |
| `LOWER_IFUNC` | 1 | ELF ifunc |
| `LOWER_LVALUE_RETURNING_OPERATIONS` | 1 | |
| `LOWER_STRING_LITERALS_TO_NON_CONST` | 0 | |
| `LOWER_VARIABLE_LENGTH_ARRAYS` | 1 | VLA → `alloca` |
| `LOWERING_NORMALIZES_BOOLEAN_CONTROLLING_EXPRESSIONS` | 1 | |
| `LOWERING_REMOVES_UNNEEDED_CONSTRUCTIONS_AND_DESTRUCTIONS` | 1 | |
| `IL_VERSION_NUMBER` | (build-fixed) | EDG IL format version |
| `IL_SHOULD_BE_WRITTEN_TO_FILE` | 1 | |
| `IL_WALK_NEEDED` | 1 | |

### 8. Host attributes — `HOST_*` (7 keys)

What the compiler binary runs on — distinct from `TARG_*` (what it generates code for).

| Key | Value |
|---|---:|
| `HOST_ALIGNMENT_REQUIRED` | 8 |
| `HOST_POINTER_ALIGNMENT` | 8 |
| `HOST_ALLOCATION_INCREMENT` | 0x10000 (64 KiB) |
| `HOST_FP_VALUE_IS_128BIT` | 1 |
| `HOST_HAS_FLOAT16_TYPE` | 0 |
| `HOST_IL_ENTRY_PREFIX_ALIGNMENT` | (build-fixed) |
| `HOST_TARGET_ENDIAN_MISMATCH_OKAY` | (build-fixed) |

### 9. Driver / integration identity

| Key | Value |
|---|---|
| `EDG_AUXILIARY_INFO_DIR_NAME` | `"lib"` |
| `EDG_MAIN` | `lgenfe_main` |
| `EDG_WIN32` | 0 |
| `EDG_MULTIBYTE_CHAR_TEST_MODE` | 0 |
| `EDG_NATIVE_MULTIBYTE_TEST_MODE` | 0 |
| `BACK_END_IS_C_GEN_BE` | 1 |
| `BACK_END_IS_CP_GEN_BE` | 0 |
| `BACK_END_SHOULD_BE_CALLED` | 1 |
| `C_GEN_BE_GENERATES_ANSI_C` | 0 |
| `USING_DRIVER` | 0 |
| `USING_KAI_INLINER` | 0 |
| `MAKE_FRONT_END_CALLABLE` | 1 |
| `FRONT_END_SHOULD_BE_CALLED` | (implicit via `MAKE_FRONT_END_CALLABLE`) |
| `VERSION_NUMBER` | 606 (i.e. 6.6) |
| `VERSION_NUMBER_FOR_MACRO` | 606 |

### 10. Diagnostics & instrumentation (25 keys)

| Key | Value |
|---|---:|
| `DEBUG` | 0 |
| `CHECKING` | 0 |
| `EXPENSIVE_CHECKING` | 0 |
| `DUMP_CONFIG_ENABLED` | 1 |
| `TRACK_INTERPRETER_ALLOCATIONS` | 0 |
| `ABORT_ON_INIT_COMPONENT_LEAKAGE` | 0 |
| `EXIT_ON_INTERNAL_ERROR` | 0 |
| `COLUMN_NUMBER_IN_BRIEF_DIAGNOSTICS` | 1 |
| `ERROR_SEVERITY_EXPLICIT_IN_ERROR_MESSAGES` | 0 |
| `DIRECT_ERROR_OUTPUT_TO_STDOUT` | 0 |
| `SEQUENCING_DIAGNOSTICS_ENABLED` | 1 |
| `UNICODE_VULNERABILITY_DETECTION_SUPPORTED` | 1 |
| `FUNC_AVAILABLE` | 1 |
| `STAT_AVAILABLE` | 1 |
| `UNIQUE_FILE_IDENTIFIER_AVAILABLE` | 1 |
| `STACK_REFERENCED_INCLUDE_DIRECTORIES` | 1 |
| `WRITE_SIGNOFF_MESSAGE` | 1 |
| `OVERWRITE_FREED_MEM_BLOCKS` | 0 |
| `FREE_MEMORY_REGIONS_EARLY` | 0 |
| `USE_MMAP_FOR_MEMORY_REGIONS` | 0 |
| `USE_MMAP_FOR_MODULES` | 1 |
| `USE_FIXED_ADDRESS_FOR_MMAP` | 0 |
| `MAINTAIN_ALLOCATION_SEQUENCE_NUMBER` | 0 |
| `EXPLICITLY_UNROLL_CRITICAL_LOOPS` | 1 |
| `IMPLEMENTATION_SUPPORTS_MULTIPLE_THREADS` | **0** | See QUIRK 5 |

### 11. Other (239 catch-all)

Highlights from the bulk: path constants, token-handling, generated-C personality, name-mangling.

| Key | Value |
|---|---|
| `DIRECTORY_SEPARATOR` | `'/'` |
| `DIRECTORY_SEPARATOR_STRING` | `"/"` |
| `FILE_NAME_FOR_STDIN` | `"-"` |
| `OBJECT_FILE_SUFFIX` | `".o"` |
| `GEN_C_FILE_SUFFIX` | `".c"` |
| `PCH_FILE_SUFFIX` | `".pch"` |
| `BACKSLASH_IS_ALSO_DIR_SEPARATOR` | 0 |
| `WINDOWS_PATHS_ALLOWED` | 0 |
| `MAX_MULTIBYTE_CHAR_LENGTH` | (build-fixed) |
| `MAX_CHAR16_T_ENCODING_LENGTH` | (build-fixed) |
| `MAX_ERROR_OUTPUT_LINE_LENGTH` | (build-fixed) |
| `MAX_INCLUDE_FILES_OPEN_AT_ONCE` | (build-fixed) |
| `MAX_SIZEOF_LARGEST_INTEGER` | 16 |
| `MAX_TOTAL_PENDING_INSTANTIATIONS` | 256 |
| `MAX_UNUSED_ALL_MODE_INSTANTIATIONS` | (build-fixed) |
| `NULL_POINTER_IS_ZERO` | 1 |
| `BUILTIN_FUNCTIONS_ENABLED` | `1` (string-valued) |
| `BUILTIN_VA_LIST_OVERRIDE_TYPE_NAME` | `"__gnuc_va_list"` |
| `BUILTIN_VA_START_TAKES_ADDRESS_OF_VARIABLE` | 1 |
| `MANGLE_ALL_NAMES` | 0 |
| `ONLY_MANGLE_TYPES_NEEDED_FOR_EXTERNAL_NAMES` | 1 |
| `REPLACE_SPECIAL_CHARACTERS_IN_MANGLED_NAMES` | 0 |
| `NEED_NAME_MANGLING` | 1 |
| `LONG_LONG_ALLOWED` | 1 |
| `INT128_EXTENSIONS_ALLOWED` | 1 |
| `VLA_ALLOWED` | 1 |
| `VLA_DEALLOCATIONS_IN_IL` | 1 |
| `VLA_DEALLOCATION_REQUIRED` | 0 |
| `FIXED_POINT_ALLOWED` | 0 |
| `THREAD_LOCAL_STORAGE_SPECIFIER_ALLOWED` | 1 |
| `NAMED_ADDRESS_SPACES_ALLOWED` | 1 |
| `NAMED_REGISTERS_ALLOWED` | 1 |
| `PRAGMA_WEAK_ALLOWED` | 1 |
| `ALIAS_DIRECTIVE` | 0 |
| `REDEFINE_EXTNAME_PRAGMA_ENABLED` | 0 |
| `IDENT_DIRECTIVE_AND_PRAGMA` | 1 |
| `ATT_PREPROCESSING_EXTENSIONS_ALLOWED` | 0 |
| `EMBEDDED_C_ALLOWED` | 0 |
| `UPC_EXTENSIONS_ALLOWED` | 0 |
| `SUN_EXTENSIONS_ALLOWED` | 0 |
| `SUN_IS_GENERATED_CODE_TARGET` | 0 |
| `MULTIBYTE_CHARS_IN_SOURCE_SUPPORTED` | 1 |
| `UNICODE_SOURCE_SUPPORTED` | 1 |
| `NATIVE_MULTIBYTE_CHARS_SUPPORTED_WITH_UNICODE` | 1 |

## QUIRK Callouts

> **QUIRK 1 — `ABI_COMPATIBILITY_VERSION = 9999`.** EDG's source dial uses `9999` as a sentinel meaning "latest known". It is *not* a literal version number. Downstream code paths that compare against this value must treat any 4-digit-and-above reading as "current". Pairs with `DRIVER_COMPATIBILITY_VERSION = 9999` — together they mean cicc never advertises that its own ABI is older than the latest EDG point release, even when emulating older personalities via CLI overrides.

> **QUIRK 2 — `TARG_SUPPORTS_ARM64 = 0` despite NVIDIA shipping aarch64 CUDA.** This is the *host* arm64 support flag for the EDG front end's own arithmetic preview, not the device target. cicc generates NVVM IR; the actual aarch64 codegen happens in `cudafe++`/`nvcc` host-side tooling and is not exercised here. Build a Grace-Hopper-host cicc and this would flip — but the x86-64 cicc binary documented in this wiki has it hard-zeroed.

> **QUIRK 3 — `DEFAULT_GNU_VERSION = 80100` (gcc 8.1.0) is anachronistic.** NVIDIA's cicc v13.0 advertises a gcc-8.1.0 emulation default. Modern host toolchains routinely run gcc 12-14, so users who don't pass an explicit `-ccbin`/`--use_fast_math`/`--gnu_version` end up with cicc claiming a 2018-era g++. The disagreement matters when consumers inspect `__GNUC__` to gate `<bit>`, `std::format`, or other C++20+ features in headers cicc preprocesses.

> **QUIRK 4 — `DEFAULT_AUTO_TYPE_SPECIFIER_ENABLED = 0`.** C++11's `auto` *type* specifier is off by default in EDG; the runtime driver flips it via `-std=c++11`+ but the raw EDG personality is C++03-shaped. This is harmless in normal nvcc usage because the driver always supplies a `-std=` flag, but it bites anyone embedding `front_end.a` directly.

> **QUIRK 5 — `IMPLEMENTATION_SUPPORTS_MULTIPLE_THREADS = 0`.** Despite NVIDIA's cicc using parallel device compilation pipelines downstream, the EDG front end itself declares *non-threadsafe*. The `--threads` mechanism added in CUDA 11.5 spawns separate cicc processes, not threads inside one cicc, so this flag is structurally correct — but easy to mistake for an indicator of a serial compiler. Cross-reference [infra/concurrent-compilation.md](../infra/concurrent-compilation.md).

## Pre-Defined Macro Layer (downstream of `0xE1`)

The dump is not the only consumer of these defines: every macro that gets *predefined* into user translation units (`__GNUC__`, `__cplusplus`, `__cpp_constexpr`, `__cpp_lambdas`, …) is gated by the `MACRO_DEFINED_WHEN_*` family above. The actual macro definitions flow from a parallel table walked by `sub_88BDE0` (the `case 0xE2u` consumer) and by per-mode initialisers in the EDG predefined-macro file (`PREDEFINED_MACRO_FILE_NAME`, defined here, points to the file under `EDG_AUXILIARY_INFO_DIR_NAME/include`). The `--gen_config` output is therefore the *upper-half* mirror of what user code sees as macros — modify a `DEFAULT_*` here and the lower-half changes automatically.

## How to Reproduce the Dump

```bash
# The driver-side switch is --gen_config; cicc opcode is 0xE1.
# Direct invocation (the EDG-style long form has been preserved):
cicc --gen_config --gen_config_file edg_cfg.h <minimal.cu>

# The output is identical bytewise to the 748 fprintf calls in case 0xE1u.
# Use this to diff cicc builds across CUDA versions to detect personality drift.
diff <(cicc-13.0 --gen_config ...) <(cicc-12.6 --gen_config ...)
```

A clean output looks like:

```
/* Configuration data for Edison Design Group C/C++ Front End */
/* version 6.6, built on <date> at <time>. */

#define ABI_CHANGES_FOR_ARRAY_NEW_AND_DELETE 1
#define ABI_CHANGES_FOR_CONSTRUCTION_VTBLS 1
#define ABI_CHANGES_FOR_COVARIANT_VIRTUAL_FUNC_RETURN 1
...
#define WRITE_SIGNOFF_MESSAGE 1

/* Legacy configuration: <unnamed> */
#define LEGACY_TARGET_CONFIGURATION_NAME NULL
```

The build-date strings come from `off_4B6EB10` and `off_4B6EB08[0]` in `.rodata` — they are the C macros `__DATE__`/`__TIME__` evaluated at *cicc link time*, not at runtime.

## Cross-References

- [pipeline/edg.md](../pipeline/edg.md) — EDG 6.6 frontend, dispatcher overview, and the CLI options that feed `sub_617BD0`
- [targets/index.md](../targets/index.md) — Where the `TARG_*` host-side values are converted into per-SM device attributes
- [config/cli-flags.md](./cli-flags.md) — The 0x150-entry option table that includes `0xE0u`/`0xE1u`/`0xE2u`
- [config/env-vars.md](./env-vars.md) — Parallel runtime config surface (24 environment variables); the build-time defines on this page are the static counterpart
- [infra/concurrent-compilation.md](../infra/concurrent-compilation.md) — Why `IMPLEMENTATION_SUPPORTS_MULTIPLE_THREADS=0` is structurally correct

## Open Follow-Ups

- The 105 `/* X not defined */` placeholders are not enumerated here; build a diff against the EDG public `egcfg.h` to confirm which are EDG-internal vs. genuinely unused on x86-64 LP64.
- Five `*_INT_KIND` defines (`TARG_BOOL_INT_KIND`, `TARG_CHAR16_T_INT_KIND`, `TARG_CHAR32_T_INT_KIND`, `TARG_PTRDIFF_T_INT_KIND`, `TARG_SIZE_T_INT_KIND`) emit cast expressions (`((an_integer_kind)ik_long)`) rather than literal integers; the `an_integer_kind` enum is defined inside `front_end.a` and is not currently mirrored in this wiki.
- The companion opcode `0xE2u` (`sub_88BDE0`) emits *named* legacy target configurations. The set of recognised names and their value overlays is undocumented.
- Cross-version drift: comparing the 748-line surface across CUDA 11.x → 12.x → 13.x would expose which EDG personality changes ride along with each toolkit release. This is the most direct way to date a stripped cicc binary.

> **CONFIDENCE:** Categories and counts are mechanically derived from `cicc_full.c` and `/tmp/build_defines.txt`. Quirks are interpretive — flagged accordingly.
