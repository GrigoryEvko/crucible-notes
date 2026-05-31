# .int.c File Format

When cudafe++ processes a CUDA source file, the backend code generator emits a **transformed C++ translation** called the `.int.c` file (short for "intermediate C"). This is the host-side output that the downstream host compiler (GCC, Clang, or MSVC) will compile. The file preserves all host-visible declarations from the original source but replaces device code with stubs, injects CUDA runtime boilerplate, and appends registration tables and anonymous namespace support. The entire emission is driven by `process_file_scope_entities` (`sub_489000`), a 723-line function in `cp_gen_be.c` that serves as the backend entry point. It initializes output state, opens the output stream, emits a fixed sequence of preamble sections, walks the EDG intermediate language source sequence to generate the transformed C++ body, then appends a fixed trailer with `_NV_ANON_NAMESPACE` handling, `#pragma pack()` for MSVC, and CUDA host reference arrays.

## Key Facts

| Property | Value |
|---|---|
| Backend entry point | `sub_489000` (`process_file_scope_entities`, 723 lines) |
| EDG source file | `cp_gen_be.c` (lines 19916-26628) |
| Default output name | `<input>.int.c` (via `sub_5ADD90` string concatenation) |
| Output override global | `qword_106BF20` (set by CLI flag `gen_c_file_name`, case 45) |
| Stdout sentinel | `"-"` (output filename compared character-by-character) |
| Output stream global | `stream` (FILE pointer at fixed address) |
| Line counter | `dword_1065820` (incremented on every `\n`) |
| Column counter | `dword_106581C` (character position within current line) |
| Indent level | `dword_1065834` (decremented with `--` around directive blocks) |
| Needs-line-directive flag | `dword_1065818` (triggers `#line` emission before next output) |
| Source sequence cursor | `qword_1065748` (current IL entry being processed) |
| Device stub mode toggle | `dword_1065850` (0=normal, 1=generating `__wrapper__device_stub_`) |
| Empty file guard string | `"int __dummy_to_avoid_empty_file;"` at `0x83AED8` |
| Anon namespace macro string | `"_NV_ANON_NAMESPACE"` at `0x83AF45` |
| Managed RT boilerplate | inline `static` functions for `__managed__` variable support |

## Output File Naming

The output filename is determined by three inputs, checked in order:

```c
// sub_489000, decompiled lines 153-177
char *input_name = qword_126EEE0;   // source filename from CLI

// 1. Check for stdout mode
if (strcmp(input_name, "-") == 0) {
    stream = stdout;
}
else {
    // 2. Check for explicit output name override
    char *output_name = qword_106BF20;
    if (!output_name)
        // 3. Default: append ".int.c" to input filename
        output_name = sub_5ADD90(input_name, ".int.c");

    stream = sub_4F48F0(output_name, 0, 0, 0, 1701);  // open for writing
}
```

The `-` sentinel enables piping cudafe++ output to stdout for debugging or toolchain integration. The `qword_106BF20` override is set by the `gen_c_file_name` CLI option (case 45 in the CLI parser at `sub_459630`), allowing nvcc to specify an explicit output path. The default `.int.c` suffix means a file `kernel.cu` produces `kernel.cu.int.c`.

## Complete .int.c File Structure

A fully-generated `.int.c` file follows this fixed section ordering, top to bottom:

```text
+------------------------------------------------------------------+
| 1. #line directive (initial source position)                     |
+------------------------------------------------------------------+
| 2. #pragma GCC diagnostic ignored "-Wunused-local-typedefs"      |
|    #pragma GCC diagnostic ignored "-Wattributes"                 |
+------------------------------------------------------------------+
| 3. #pragma GCC diagnostic push                                   |
|    #pragma GCC diagnostic ignored "-Wunused-variable"            |
|    #pragma GCC diagnostic ignored "-Wunused-function"            |
+------------------------------------------------------------------+
| 4. Managed runtime boilerplate                                   |
|    (static __nv_inited_managed_rt, __nv_init_managed_rt, etc.)   |
+------------------------------------------------------------------+
| 5. #pragma GCC diagnostic pop                                    |
+------------------------------------------------------------------+
| 6. #pragma GCC diagnostic ignored "-Wunused-variable"            |
|    #pragma GCC diagnostic ignored "-Wunused-private-field"       |
|    #pragma GCC diagnostic ignored "-Wunused-parameter"           |
+------------------------------------------------------------------+
| 7. Extended lambda macro definitions (or #define false stubs)    |
+------------------------------------------------------------------+
| 8. MAIN BODY: transformed C++ from source sequence walk          |
|    - #include "crt/host_runtime.h" (injected at first CUDA type) |
|    - Device stubs for __global__ kernels                         |
|    - #if 0 / #endif around device-only code                     |
|    - All host-visible declarations, types, functions             |
+------------------------------------------------------------------+
| 9. Empty file guard (if no entities generated)                   |
+------------------------------------------------------------------+
| 10. Breakpoint placeholders (debug builds only)                  |
+------------------------------------------------------------------+
| 11. _NV_ANON_NAMESPACE define / include / undef trick            |
+------------------------------------------------------------------+
| 12. #pragma pack() (MSVC only)                                   |
+------------------------------------------------------------------+
| 13. Module ID file output (if dword_106BFB8 set)                 |
+------------------------------------------------------------------+
| 14. Host reference arrays (.nvHRKI, .nvHRDE, etc.)               |
+------------------------------------------------------------------+
```

## Section 1: Initial #line Directive

After opening the output stream, `sub_489000` emits a `#line` directive via `sub_46D1A0` to establish the initial source mapping. This directive points the host compiler's diagnostic messages back to the original `.cu` file:

```c
// sub_489000, decompiled lines 283-287
sub_46D1A0(v10, v11);  // emit #line <number> "<filename>"
```

The `#line` directive format depends on the host compiler. For GCC/Clang hosts (`dword_126E1F8` set), the `line` keyword is omitted (producing `# 1 "file.cu"`). For MSVC hosts (`dword_126E1D8` set), the full `#line 1 "file.cu"` form is used. This pattern recurs throughout the file wherever source position changes.

## Section 2-6: Diagnostic Suppressions

The preamble contains a layered set of `#pragma GCC diagnostic` directives that suppress warnings the host compiler would otherwise emit on the generated code. The exact set depends on which host compiler is active and its version.

### Suppression Decisions

The conditions controlling each suppression are checked against host compiler identification globals:

| Global | Meaning |
|---|---|
| `dword_126E1E8` | Host is Clang |
| `dword_126E1F8` | Host is GCC (including Clang in GCC-compat mode) |
| `dword_126E1D8` | Host is MSVC |
| `qword_126EF90` | Clang version number |
| `qword_126E1F0` | GCC/Clang version number |
| `dword_106BF6C` | Alternative host compiler mode |
| `dword_106BF68` | Secondary host compiler flag |

### -Wunused-local-typedefs

Emitted early, outside any push/pop scope:

```c
// sub_489000, decompiled lines 182-187
if ((dword_126E1E8 && qword_126EF90 > 0x7787)   // Clang > 30599
    || (!dword_106BF6C && !dword_106BF68
        && dword_126E1F8 && qword_126E1F0 > 0x9F5F))  // GCC > 40799
{
    emit("#pragma GCC diagnostic ignored \"-Wunused-local-typedefs\"");
}
```

This targets GCC 4.8+ and Clang 3.1+, which introduced the `-Wunused-local-typedefs` warning. CUDA template machinery frequently creates local typedefs that are used only by device code (suppressed in `#if 0` blocks), triggering spurious warnings.

### -Wattributes

```c
// sub_489000, decompiled lines 188-189
if (dword_126EFA8 && dword_106C07C)
    emit("\n#pragma GCC diagnostic ignored \"-Wattributes\"\n");
```

Suppresses warnings about unknown or ignored `__attribute__` annotations. Emitted when CUDA-specific attribute processing is active (`dword_126EFA8`) and a secondary flag (`dword_106C07C`) indicates the host compiler would reject CUDA-specific attributes.

### Push/Pop Block with -Wunused-variable and -Wunused-function

The managed runtime boilerplate (section 4) is wrapped in a diagnostic push/pop block:

```c
// sub_489000, decompiled lines 190-234
emit("#pragma GCC diagnostic push");
emit("#pragma GCC diagnostic ignored \"-Wunused-variable\"");
emit("#pragma GCC diagnostic ignored \"-Wunused-function\"");

// ... managed runtime boilerplate here ...

emit("#pragma GCC diagnostic pop");
```

The push/pop scope isolates these suppressions to the managed runtime code. The conditions for emitting this block check Clang presence (`dword_126E1E8`), or GCC version > 40599 (`qword_126E1F0 > 0x9E97`). The managed runtime functions are `static` and may be unused in translation units without `__managed__` variables.

### Post-Pop File-Level Suppressions

After the pop, additional file-scoped suppressions are emitted that remain active for the rest of the file:

```c
// sub_489000, decompiled lines 243-250
emit("#pragma GCC diagnostic ignored \"-Wunused-variable\"\n");

if (dword_126E1E8) {  // Clang only
    emit("#pragma GCC diagnostic ignored \"-Wunused-private-field\"\n");
    emit("#pragma GCC diagnostic ignored \"-Wunused-parameter\"\n");
}
```

The `-Wunused-private-field` and `-Wunused-parameter` suppressions are Clang-specific. GCC does not have `-Wunused-private-field`, and GCC's `-Wunused-parameter` behavior differs.

### Summary of All Suppressions

| Warning | Scope | Host Compiler | Version Threshold |
|---|---|---|---|
| `-Wunused-local-typedefs` | File-level | Clang, GCC | Clang > 30599, GCC > 40799 |
| `-Wattributes` | File-level | GCC/Clang | When CUDA attrs active |
| `-Wunused-variable` | Push/pop block | Clang, GCC >= 40599 | Around managed RT only |
| `-Wunused-function` | Push/pop block | Clang, GCC >= 40599 | Around managed RT only |
| `-Wunused-variable` | File-level | Clang, GCC >= 40199 | Rest of file |
| `-Wunused-private-field` | File-level | Clang only | Always |
| `-Wunused-parameter` | File-level | Clang only | Always |

## Section 7: Extended Lambda Macros

When extended lambda mode is NOT active (`dword_106BF38 == 0`), three stub macros are defined:

```c
// sub_489000, decompiled lines 259-264
emit("#define __nv_is_extended_device_lambda_closure_type(X) false\n");
emit("#define __nv_is_extended_host_device_lambda_closure_type(X) false\n");
emit("#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false\n");
emit("#if defined(__nv_is_extended_device_lambda_closure_type)"
     " && defined(__nv_is_extended_host_device_lambda_closure_type)"
     "&& defined(__nv_is_extended_device_lambda_with_preserved_return_type)\n"
     "#endif\n");
```

These macros are consumed by `crt/host_runtime.h` to conditionally compile lambda wrapper infrastructure. When extended lambdas are disabled, all three evaluate to `false`, causing the runtime header to skip lambda wrapper code. The `#if defined(...) && defined(...)` block that immediately follows is an existence check -- it verifies the macros are defined, producing a compilation error if some other header has `#undef`'d them.

When extended lambda mode IS active (`dword_106BF38 != 0`), these defines are skipped entirely. The lambda preamble injection system (via `sub_6BCC20`) provides the real implementations later in the main body.

## Section 8: Main Body -- Source Sequence Walk

The main body is generated by iterating the global source sequence list (`qword_1065748`), which is a linked list of EDG IL entries representing every top-level declaration in the translation unit. For each entry, the backend dispatches to `sub_47ECC0` (`gen_template` / `process_source_sequence`), which handles all declaration kinds:

```c
// sub_489000, decompiled lines 288-316 (simplified)
while (qword_1065748) {
    entry = qword_1065748;
    kind = entry->kind;  // byte at offset +16

    if (kind == 57) {
        // Pragma interleaving -- handled inline
        handle_pragma(entry);
    } else if (kind == 52) {
        // End-of-construct -- should not appear at top level
        fatal_error("Top-level end-of-construct entry");
    } else {
        entities_generated = 1;
        sub_47ECC0(0);  // gen_template at recursion level 0
    }
}
```

During this walk, several CUDA-specific injections occur:

1. **`#include "crt/host_runtime.h"`** -- injected by `sub_4864F0` (`gen_type_decl`) or `sub_47ECC0` when the first CUDA-tagged entity at global scope is encountered. The flag `dword_E85700` prevents duplicate inclusion.

2. **Device stub pairs** -- `__global__` kernel functions trigger two calls to `gen_routine_decl` (`sub_47BFD0`): first the forwarding body, then the static `cudaLaunchKernel` placeholder, controlled by the `dword_1065850` toggle.

3. **`#if 0` / `#endif` guards** -- device-only declarations are wrapped in preprocessor guards to hide them from the host compiler.

4. **Interleaved pragmas** -- source sequence entries of kind 57 represent `#pragma` directives from the original source (including `#pragma pack`, `#pragma STDC`, and user pragmas), which are re-emitted at their original positions.

## Section 9: Empty File Guard

If the source sequence walk produced no entities (`v12 == 0`) and the compilation is not in pure CUDA mode (`dword_126EFB4 != 2`), a dummy declaration is emitted to prevent the host compiler from rejecting an empty translation unit:

```c
// sub_489000, decompiled lines 565-569
if (!entities_generated && dword_126EFB4 != 2) {
    emit("int __dummy_to_avoid_empty_file;");
    newline();
}
```

Some host compilers (notably older GCC versions) produce warnings or errors on completely empty `.c` files. The `int __dummy_to_avoid_empty_file;` declaration is a minimal valid C/C++ statement that suppresses this.

## Section 10: Breakpoint Placeholders

When the deferred function list (`qword_1065840`) is non-empty, the backend emits one breakpoint placeholder function per entry. These are used for debugger support in whole-program compilation mode:

```c
// sub_489000, decompiled lines 573-651 (simplified)
node = qword_1065840;  // linked list of deferred functions
index = 0;
while (node) {
    emit("static __attribute__((used)) void __nv_breakpoint_placeholder");
    emit_decimal(index);
    putc('_', stream);
    if (node->name)
        emit(node->name);
    emit("(void) ");

    // Set source position from node
    set_source_position(node->source_start);
    emit("{ ");
    set_source_position(node->source_end);
    emit("exit(0); }");

    node = node->next;
    index++;
}
```

Each placeholder has the form `static __attribute__((used)) void __nv_breakpoint_placeholderN_funcname(void) { exit(0); }`. The `__attribute__((used))` prevents the linker from stripping these functions. The debugger uses their addresses to set breakpoints on device functions that have been stripped from the host binary.

The deferred list is populated by `gen_routine_decl` when `dword_106BFBC` (whole-program mode) is set and `dword_106BFDC` is clear -- device-only functions that need host-side breakpoint anchors are pushed onto this list rather than receiving dummy bodies inline.

## Section 11: _NV_ANON_NAMESPACE Trick

The trailer contains a four-step sequence that handles C++ anonymous namespace mangling for CUDA. Anonymous namespaces in C++ create translation-unit-local symbols, but CUDA device code requires globally unique symbol names (because device code from multiple TUs is linked together by the device linker). The `_NV_ANON_NAMESPACE` mechanism assigns a deterministic, globally unique identifier to each TU's anonymous namespace.

### Step-by-Step Emission

```c
// sub_489000, decompiled lines 654-710

// Step 1: #line back to original source
emit("#");
if (!dword_126E1F8)  // MSVC: include "line" keyword
    emit("line");
emit(" 1 \"");
emit(path_transform(qword_106BF88));  // original source file path
emit("\"");

// Step 2: #define _NV_ANON_NAMESPACE <hash>
emit("#define ");
emit("_NV_ANON_NAMESPACE");
emit(" ");
emit(sub_6BC7E0());   // generate unique hash string
newline();

// Step 3: #ifdef / #endif (force inclusion check)
emit("#ifdef ");
emit("_NV_ANON_NAMESPACE");
newline();
emit("#endif");
newline();

// Step 3b: #pragma pack() for MSVC
if (dword_126E1D8) {   // MSVC host
    emit("#pragma pack()");
    newline();
}

// Step 4: #include "<original_file>"
emit("#");
if (!dword_126E1F8)
    emit("line");
emit(" 1 \"");
emit(path_transform(qword_106BF88));
emit("\"");
newline();
emit("#include ");
emit("\"");
emit(path_transform(qword_106BF88));
emit("\"");
newline();

// Step 5: Reset #line and #undef
emit("#");
if (!dword_126E1F8)
    emit("line");
emit(" 1 \"");
emit(path_transform(qword_106BF88));
emit("\"");
newline();
emit("#undef ");
emit("_NV_ANON_NAMESPACE");
newline();
```

### The Hash Generator (sub_6BC7E0)

The `_NV_ANON_NAMESPACE` value is produced by `sub_6BC7E0`, which constructs the string `_GLOBAL__N_` followed by the module ID hash:

```c
// sub_6BC7E0 (20 lines)
if (cached_result)
    return cached_result;

char *module_id = sub_5AF830(0);   // compute CRC32-based module ID
size_t len = strlen(module_id);
char *result = allocate(len + 12);
strcpy(result, "_GLOBAL__N_");
strcpy(result + 11, module_id);
cached_result = result;
return result;
```

The module ID (`sub_5AF830`) is a CRC32-based hash incorporating the source filename, compiler options, file modification time, and process ID. This produces values like `_GLOBAL__N_1a2b3c4d5e6f7890` -- deterministic enough for reproducible builds, but unique enough to avoid collisions between TUs.

### Why the Define/Include/Undef Sequence

The three-step define/include/undef pattern serves a specific purpose:

1. **`#define _NV_ANON_NAMESPACE <hash>`** -- establishes the macro before the source file is re-included.

2. **`#include "<original_file>"`** -- re-includes the original `.cu` source. During this second inclusion, any code inside anonymous namespaces that uses `_NV_ANON_NAMESPACE` gets the unique hash substituted, producing globally unique symbol names for device code.

3. **`#undef _NV_ANON_NAMESPACE`** -- cleans up the macro after inclusion.

The `#ifdef _NV_ANON_NAMESPACE` / `#endif` block between define and include is a safety check -- it verifies the macro was actually defined before proceeding.

This mechanism works in conjunction with the EDG frontend's anonymous namespace handling. When the frontend encounters `namespace { ... }` containing device code, it generates references to `_NV_ANON_NAMESPACE` that become concrete identifiers during the re-inclusion pass. The name mangling in the demangler (`sub_7CA140`, `sub_7C5650`, `sub_7C4E80`) also uses `_NV_ANON_NAMESPACE` to produce consistent mangled names.

## Section 12: #pragma pack() for MSVC

When the host compiler is MSVC (`dword_126E1D8` set), a bare `#pragma pack()` is emitted to reset the packing alignment to the compiler default:

```c
// sub_489000, decompiled lines 676-681
if (dword_126E1D8) {
    emit("#pragma pack()");
    newline();
}
```

This reset ensures that any `#pragma pack(N)` directives from the original source or from included CUDA headers do not leak into subsequent translation units. On GCC/Clang, the `#pragma pack()` push/pop mechanism is typically handled differently, so this emission is MSVC-specific.

## Section 13-14: Module ID and Host Reference Arrays

The final two sections are conditional:

**Module ID output** (`sub_5B0180`): When `dword_106BFB8` is set, the module ID string (the same CRC32-based hash from `sub_5AF830`) is written to a separate file. This ID is used by the CUDA runtime to match host-side registration code with the device fatbinary.

**Host reference arrays** (`sub_6BCF80`): When `dword_106BFD0` (device registration) or `dword_106BFCC` (constant registration) is set, six calls to `sub_6BCF80` emit ELF section declarations for host reference arrays:

```c
// sub_489000, decompiled lines 713-721
// nv_emit_host_reference_array(emit_fn, is_kernel, is_device, is_internal)
sub_6BCF80(emit_callback, 1, 0, 1);  // kernel,   internal  -> .nvHRKI
sub_6BCF80(emit_callback, 1, 0, 0);  // kernel,   external  -> .nvHRKE
sub_6BCF80(emit_callback, 0, 1, 1);  // device,   internal  -> .nvHRDI
sub_6BCF80(emit_callback, 0, 1, 0);  // device,   external  -> .nvHRDE
sub_6BCF80(emit_callback, 0, 0, 1);  // constant, internal  -> .nvHRCI
sub_6BCF80(emit_callback, 0, 0, 0);  // constant, external  -> .nvHRCE
```

These produce `extern "C"` declarations with `__attribute__((section(".nvHRXX")))` annotations, where `XX` is one of `KE`, `KI`, `DE`, `DI`, `CE`, `CI` (Kernel/Device/Constant + External/Internal). The arrays contain mangled names of device symbols, enabling the CUDA runtime to locate and register them at program startup.

## Complete Example

For a source file `kernel.cu` containing a single `__global__` kernel function and a host function, the generated `kernel.cu.int.c` looks approximately like this:

```c
# 1 "kernel.cu"
#pragma GCC diagnostic ignored "-Wunused-local-typedefs"
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#pragma GCC diagnostic ignored "-Wunused-function"
static char __nv_inited_managed_rt = 0;
static void **__nv_fatbinhandle_for_managed_rt;
static void __nv_save_fatbinhandle_for_managed_rt(void **in) {
    __nv_fatbinhandle_for_managed_rt = in;
}
static char __nv_init_managed_rt_with_module(void **);
static inline void __nv_init_managed_rt(void) {
    __nv_inited_managed_rt = (__nv_inited_managed_rt
        ? __nv_inited_managed_rt
        : __nv_init_managed_rt_with_module(
            __nv_fatbinhandle_for_managed_rt));
}
#pragma GCC diagnostic pop
#pragma GCC diagnostic ignored "-Wunused-variable"
#pragma GCC diagnostic ignored "-Wunused-private-field"
#pragma GCC diagnostic ignored "-Wunused-parameter"
#define __nv_is_extended_device_lambda_closure_type(X) false
#define __nv_is_extended_host_device_lambda_closure_type(X) false
#define __nv_is_extended_device_lambda_with_preserved_return_type(X) false
#if defined(__nv_is_extended_device_lambda_closure_type) \
 && defined(__nv_is_extended_host_device_lambda_closure_type) \
 && defined(__nv_is_extended_device_lambda_with_preserved_return_type)
#endif

/* === main body begins here === */
#include "crt/host_runtime.h"

# 5 "kernel.cu"
void host_function(int *data, int n) {
    for (int i = 0; i < n; i++) data[i] *= 2;
}
# 10 "kernel.cu"
void my_kernel(float *data, int n) {
    ::my_kernel::__wrapper__device_stub_my_kernel(data, n);
    return;
}
#if 0
/* original __global__ kernel body suppressed */
#endif
static void __wrapper__device_stub_my_kernel(float *data, int n) {
    ::cudaLaunchKernel(0, 0, 0, 0, 0, 0);
}
/* === main body ends === */

# 1 "kernel.cu"
#define _NV_ANON_NAMESPACE _GLOBAL__N_a1b2c3d4e5f67890
#ifdef _NV_ANON_NAMESPACE
#endif
# 1 "kernel.cu"
#include "kernel.cu"
# 1 "kernel.cu"
#undef _NV_ANON_NAMESPACE
```

## Initialization State

Before emitting any output, `sub_489000` zeroes all output-related global state and initializes four large hash tables (each 512KB, cleared with `memset`). It also sets up a function pointer table (`xmmword_1065760` through `xmmword_10657B0`) containing code generation callbacks:

```c
// sub_489000, decompiled lines 62-97 (summarized)
dword_1065834 = 0;         // indent level
stream = NULL;             // output file handle
dword_1065820 = 0;         // line counter
dword_106581C = 0;         // column counter
dword_1065818 = 0;         // needs-line-directive
qword_1065748 = 0;         // source sequence cursor
qword_1065740 = 0;         // alternate cursor
dword_1065850 = 0;         // device stub mode

// Clear four 512KB hash tables
memset(&unk_FE5700, 0, 0x7FFE0);   // 524,256 bytes
memset(&unk_F65720, 0, 0x7FFE0);
memset(qword_E85720, 0, 0x7FFE0);
memset(&xmmword_F05720, 0, 0x5FFE8);  // 393,192 bytes (smaller)

// Callback setup
if (!dword_126DFF0)                     // not MSVC mode
    qword_10657C0 = sub_46BEE0;        // gen_be callback
qword_10657C8 = loc_469200;            // line directive callback
qword_10657D0 = sub_466F40;            // output callback
qword_10657D8 = sub_4686C0;            // error callback
```

## #line Directive Protocol

Throughout the file, `#line` directives maintain the mapping between generated output and original source positions. The emission protocol differs by host compiler:

| Host Compiler | #line Format | Example |
|---|---|---|
| GCC / Clang | `# <line> "<file>"` | `# 42 "kernel.cu"` |
| MSVC | `#line <line> "<file>"` | `#line 42 "kernel.cu"` |

The `dword_1065818` flag (`needs_line_directive`) is set whenever the current source position changes. Before emitting the next declaration or statement, `sub_467DA0` checks this flag and emits a `#line` directive if needed, then clears the flag. The source position is tracked in two globals: `qword_1065810` (pending position) and `qword_126EDE8` (current position).

## Function Map

| Address | Name | Role |
|---|---|---|
| `sub_489000` | `process_file_scope_entities` | Backend entry point; orchestrates entire .int.c emission |
| `sub_47ECC0` | `gen_template` / `process_source_sequence` | Walks source sequence, dispatches all declaration kinds |
| `sub_47BFD0` | `gen_routine_decl` | Function declaration/definition generator; kernel stub logic |
| `sub_4864F0` | `gen_type_decl` | Type declaration generator; injects `#include "crt/host_runtime.h"` |
| `sub_484A40` | `gen_variable_decl` | Variable declaration generator; managed memory registration |
| `sub_467E50` | (emit string) | Primary string emission to output stream |
| `sub_468190` | (emit raw string) | Raw string emission without line directive check |
| `sub_46BC80` | (emit directive) | Emits `#if` / `#endif` preprocessor lines |
| `sub_467DA0` | (emit line directive) | Conditionally emits `#line` when `dword_1065818` is set |
| `sub_467D60` | (emit newline) | Emits newline and flushes pending line directive |
| `sub_46CF20` | (emit source position) | Sets source position for next `#line` directive |
| `sub_5ADD90` | (string concat) | Concatenates input filename with `.int.c` extension |
| `sub_4F48F0` | (file open) | Opens output file for writing (mode 1701) |
| `sub_6BC7E0` | (anon namespace hash) | Generates `_GLOBAL__N_<module_id>` string |
| `sub_5AF830` | `make_module_id` | CRC32-based unique TU identifier |
| `sub_5B0180` | `write_module_id_to_file` | Writes module ID to separate file |
| `sub_6BCF80` | `nv_emit_host_reference_array` | Emits `.nvHRKE`/`.nvHRDI`/etc. ELF sections |
| `sub_4F7B10` | (file close) | Closes output stream (mode 1701) |

## Cross-References

- [Kernel Stub Generation](../cuda/kernel-stubs.md) -- detailed stub mechanism using `dword_1065850` toggle
- [Device/Host Separation](../cuda/device-host-separation.md) -- how device-only code gets `#if 0` guards
- [CUDA Runtime Boilerplate](./cuda-runtime.md) -- managed memory initialization functions
- [Host Reference Arrays](./host-reference-arrays.md) -- `.nvHRKI`/`.nvHRDE` section format
- [Module ID & Registration](./module-id.md) -- CRC32 hash computation details
- [Pipeline Overview](../pipeline/overview.md) -- where backend generation fits in the 8-stage pipeline
- [Extended Lambda Overview](../lambda/overview.md) -- lambda macro definitions and preamble injection
