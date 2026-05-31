# RDC Mode

CUDA supports two compilation models that fundamentally change how `cudafe++` processes device code: **whole-program mode** (`-rdc=false`, the default) and **separate compilation mode** (`-rdc=true`, also called Relocatable Device Code). The mode switch affects error checking, stub linkage, module ID generation, anonymous namespace mangling, and -- when multiple translation units are involved -- triggers EDG's cross-TU correspondence machinery for structural type verification.

From `cudafe++`'s perspective, the distinction maps to a single CLI flag (`--device-c`, flag index 77) and a handful of global booleans that gate code paths throughout the binary. This page documents what changes between the two modes, how module IDs are generated, how cross-TU IL correspondence works, and how host stub linkage is controlled.

## Key Facts

| Property | Value |
|---|---|
| RDC CLI flag | `--device-c` (flag index 77, no argument) |
| Whole-program mode flag | `dword_106BFBC` (also set by `--debug_mode`) |
| Module ID cache | `qword_126F0C0` (cached string, computed once) |
| Module ID generator | `sub_5AF830` (`make_module_id`, ~450 lines) |
| Module ID setter | `sub_5AF7F0` (`set_module_id`) |
| Module ID getter | `sub_5AF820` (`get_module_id`) |
| Module ID file writer | `sub_5B0180` (`write_module_id_to_file`) |
| Module ID file flag | `--gen_module_id_file` (flag 83) |
| Module ID file path | `--module_id_file_name` (flag 87) |
| Cross-TU IL copier | `sub_796BA0` (`copy_secondary_trans_unit_IL_to_primary`, trans_copy.c) |
| Cross-TU usage marker | `sub_796C00` (`mark_secondary_IL_entities_used_from_primary`) |
| Class correspondence | `sub_7A00D0` (`verify_class_type_correspondence`, 703 lines) |
| TU processing entry | `sub_7A40A0` (`process_translation_unit`) |
| TU switch | `sub_7A3D60` (`switch_translation_unit`) |
| Host stub linkage flag | `--host-stub-linkage-explicit` (flag 47) |
| Static host stub flag | `--static-host-stub` (flag 48) |
| Static template stub flag | `--static-global-template-stub` (set_flag mechanism) |
| EDG source files | `host_envir.c` (module ID), `trans_copy.c`, `trans_corresp.c`, `trans_unit.c` |

## Whole-Program Mode (-rdc=false)

Whole-program mode is the default. All device code for a given translation unit must be defined within that single `.cu` file. No external device symbols are allowed. The host compiler sees the entire program at once, and nvlink is not required for device code linking.

### Constraints Enforced

Five diagnostics are specific to whole-program mode or are closely tied to the internal-linkage consequences of non-RDC compilation:

**1. Inline device/constant/managed variables must have internal linkage.**

```text
An inline __device__/__constant__/__managed__ variable must have
internal linkage when the program is compiled in whole program
mode (-rdc=false)
```

In whole-program mode, the device runtime has no linker step to resolve external inline variables across TUs. An inline `__device__` variable with external linkage would need cross-TU deduplication that only nvlink can provide. The frontend forces `static` (or anonymous-namespace) linkage, emitting an error if the variable has external linkage.

**2. Extern `__global__` function templates are forbidden (with `-static-global-template-stub=true`).**

```text
when "-static-global-template-stub=true", extern __global__ function
template is not supported in whole program compilation mode ("-rdc=false").
To resolve the issue, either use separate compilation mode ("-rdc=true"),
or explicitly set "-static-global-template-stub=false" (but see nvcc
documentation about downsides of turning it off)
```

The `-static-global-template-stub` flag causes template kernel stubs to receive `static` linkage to avoid ODR violations when the same template is instantiated in multiple host-side compilation units. An `extern` template declaration conflicts with this because the `extern` stub expects an external definition while the `static` stub forces a local one. The diagnostic tag for this is `extern_kernel_template`.

**3. `__global__` template instantiations must have local definitions (with `-static-global-template-stub=true`).**

```text
when "-static-global-template-stub=true" in whole program compilation
mode ("-rdc=false"), a __global__ function template instantiation or
specialization (%sq) must have a definition in the current translation
unit.
```

A static stub requires a definition in the same TU. If the instantiation point references a template defined in another header without an explicit instantiation, the stub has no body to emit. The diagnostic tag is `template_global_no_def`.

Both template-related diagnostics recommend either switching to `-rdc=true` or setting `-static-global-template-stub=false`. The 4 usage contexts in the binary for `-static-global-template-stub` all appear in error message strings (at addresses `0x88E588` and `0x88E6E0`).

**4. Kernel launch from `__device__` or `__global__` functions requires separate compilation.**

```text
kernel launch from __device__ or __global__ functions requires
separate compilation mode
```

Dynamic parallelism -- launching a kernel from device code (a `__device__` or `__global__` function calling `<<<...>>>`) -- requires the device linker (nvlink) to resolve cross-module kernel references. In whole-program mode, no device linking occurs, so the construct is illegal. The diagnostic tag is `device_launch_no_sepcomp`.

**5. Address of internal linkage device function (bug mitigation).**

```text
address of internal linkage device function (%sq) was taken
(nv bug 2001144). mitigation: no mitigation required if the
address is not used for comparison, or if the target function
is not a CUDA C++ builtin. Otherwise, write a wrapper function
to call the builtin, and take the address of the wrapper
function instead
```

This diagnostic fires in whole-program mode when code takes the address of a `static __device__` function. Because device functions with internal linkage get module-ID-based name mangling, their addresses may differ across compilations or across TUs even when they refer to the "same" function. The warning documents a known NVIDIA bug (2001144) and provides a workaround: wrap the builtin in a non-internal function and take the wrapper's address instead. This diagnostic has no associated tag name -- it is emitted unconditionally when the condition is detected.

### Deferred Function List

When `dword_106BFBC` (whole-program mode) is set and `dword_106BFDC` (skip-device-only) is clear, `gen_routine_decl` (`sub_47BFD0`) adds device-only functions to a deferred linked list (`qword_1065840`) rather than emitting dummy bodies inline. Each list node is 32 bytes:

| Offset | Field |
|---|---|
| +0 | `next` pointer |
| +8 | Source position (start) |
| +16 | Source position (end) |
| +24 | Name string (strdup'd, or NULL) |

This list is consumed during the breakpoint placeholder phase in `process_file_scope_entities` (`sub_489000`), where each entry produces a `static __attribute__((used)) void __nv_breakpoint_placeholder<N>_<name>(void) { exit(0); }` function for debugger support.

## Separate Compilation Mode (-rdc=true)

When `nvcc` passes `--device-c` (flag index 77) to `cudafe++`, separate compilation mode is activated. This:

- Allows `__device__`, `__constant__`, and `__managed__` variables to have external linkage
- Permits `extern __global__` template functions
- Enables dynamic parallelism (kernel launches from device code)
- Requires nvlink to resolve device-side cross-TU references
- Generates a module ID that uniquely identifies each compilation unit for runtime registration

In this mode, the host stubs are generated with external linkage (by default) so the host linker can resolve cross-TU kernel calls. The module ID is embedded in the registration code to match host stubs with their corresponding device fatbinary segments.

### Multi-TU Processing in EDG

When multiple translation units are compiled in a single `cudafe++` invocation (as happens during RDC compilation with `nvcc`), the EDG frontend processes them sequentially using a stack-based TU management system:

| Global | Purpose |
|---|---|
| `qword_106BA10` | Current translation unit pointer |
| `qword_106B9F0` | Primary (first) translation unit |
| `qword_106BA18` | TU stack top |
| `dword_106B9E8` | TU stack depth (excluding primary) |

**`process_translation_unit`** (`sub_7A40A0`, trans_unit.c) is the main entry point called from `main()` for each source file:

1. Allocates a 424-byte TU descriptor via `sub_6BA0D0`
2. Initializes scope state and copies registered variable defaults
3. Sets the primary TU pointer (`qword_106B9F0`) for the first file
4. Links the TU into the processing chain
5. Opens the source file and sets up include paths
6. Runs the parser (`sub_586240`)
7. Dispatches to standard compilation (`sub_4E8A60`) or module compilation (`sub_6FDDF0`)
8. Calls finalization (`sub_588E90`)
9. Pops the TU from the stack

**`switch_translation_unit`** (`sub_7A3D60`, trans_unit.c, line 514) saves/restores per-TU state when the frontend needs to reference entities from a different TU:

1. Asserts `qword_106BA10 != 0` (current TU exists)
2. If target differs from current: saves current TU via `sub_7A3A50`
3. Restores target TU state via `memcpy` from per-TU buffer
4. Sets `qword_106BA10 = target`
5. Restores scope chain: `xmmword_126EB60`, `qword_126EB70`, etc.
6. Recomputes file scope indices via `sub_704490`

Per-TU state is registered through `f_register_trans_unit_variable` (`sub_7A3C00`, trans_unit.c, line 227), which accumulates variables into a linked list (`qword_12C7AA8`). Each registration record is 40 bytes with fields for the variable pointer, name, prior size, and buffer offset. The total per-TU buffer size is tracked in `qword_12C7A98`.

Three core variables are always registered (`sub_7A4690`):
- `dword_106BA08` (is_recompilation), 4 bytes
- `qword_106BA00` (current_filename), 8 bytes
- `dword_106B9F8` (has_module_info), 4 bytes

## Module ID Generation

Every compilation unit in CUDA needs a unique identifier to associate host-side registration code with the correct device fatbinary. This identifier -- the **module ID** -- is generated by `make_module_id` (`sub_5AF830`, host_envir.c, ~450 lines) and cached in `qword_126F0C0`.

### Algorithm

The module ID generator has three source modes, tried in order:

**Mode 1: Module ID file.** If `qword_106BF80` (set by `--module_id_file_name`) is non-NULL, the entire contents of the specified file are read and used as the module ID. This allows build systems to inject deterministic identifiers.

**Mode 2: Explicit numeric token.** If the caller provides a non-NULL string argument (`nptr`), it is parsed via `strtoul`. If the parse succeeds, the numeric value is used directly. If the parse fails (the string is not a pure integer), the string itself is CRC32-hashed and the hash is used.

**Mode 3: Default computation.** The default path builds the ID from several components:

1. Calls `stat()` on the source file to obtain `mtime`
2. Formats `ctime()` of the modification time
3. Reads `getpid()` for the current process ID
4. Collects `qword_106C038` (command-line options hash input)
5. Computes the CRC32 hash of the options string
6. Takes the output filename, strips it to basename
7. If the source filename exceeds 8 characters, replaces it with its CRC32 hex representation

The final string is assembled in the format:

```text
{options_crc}_{output_name_len}_{output_name}_{source_or_crc}[_{extra}][_{pid}]
```

All non-alphanumeric characters in the result are replaced with underscores. The string is allocated permanently and cached in `qword_126F0C0`.

Debug tracing (gated by `dword_126EFC8`) emits:
```text
make_module_id: str1 = %s, str2 = %s, pid = %ld
make_module_id: final string = %s
```

### CRC32 Implementation

The function contains an inline CRC32 implementation that appears **three times** (for the options hash, the source filename, and the extra string). All three copies use the same algorithm:

- Polynomial: `0xEDB88320` (standard reflected CRC-32)
- Initial value: `0xFFFFFFFF`
- Processing: bit-by-bit, 8 iterations per byte
- Final XOR: implicit via the reflected algorithm

The triple inlining suggests the CRC32 was originally a macro or small inline function that the compiler expanded at each call site. The polynomial `0xEDB88320` is the bitwise reversal of the standard CRC-32 polynomial `0x04C11DB7`, confirming this is the ubiquitous CRC-32/ISO-HDLC algorithm.

### PID Incorporation

The `getpid()` call ensures that concurrent compilations of the same source file produce different module IDs. Without the PID, two parallel `nvcc` invocations compiling the same `.cu` file with the same flags would generate identical module IDs, potentially causing runtime registration collisions. The PID is appended as the final underscore-separated component.

### Module ID File Output

When `--gen_module_id_file` (flag 83) is set, `write_module_id_to_file` (`sub_5B0180`) generates the module ID via `sub_5AF830(0)` and writes it to the file specified by `qword_106BF80` (`--module_id_file_name`, flag 87). If the filename is not set, it emits `"module id filename not specified"`. If the write fails, it emits `"error writing module id to file"`.

In the backend output phase, if `dword_106BFB8` (emit-symbol-table flag) is set, `sub_5B0180` is also called to write the module ID before the host reference arrays are emitted.

### Entity-Based Module ID Selection

An alternative module ID source is available through `use_variable_or_routine_for_module_id_if_needed` (`sub_5CF030`, il.c, line 31969, ~65 lines). Rather than computing a hash from file metadata, this function selects a representative entity (variable or function) from the current TU whose mangled name can serve as a stable identifier. The selection criteria are strict:

- Entity kind must be 7 (variable) or 11 (routine), tested via `(kind - 7) & 0xFB == 0`
- Must have a definition (for variables: offset `+169 != 0`; for routines: has a body)
- Must not be a class member
- Must not be in an unnamed namespace
- Must have storage class `== 0` (no explicit `static`, `extern`, or `register`)
- Must not be template-related or marked with special compilation flags
- For routines: must not have explicit specialization, return type must not be a builtin

The selected entity is stored in `qword_126F140` with its kind byte in `byte_126F138` (7 for variable, 11 for routine). This entity's name is then fed into `sub_5AF830` to produce the final module ID string. The entity-based approach provides a more deterministic ID than the PID-based default, since it is derived from source content rather than runtime state.

### Anonymous Namespace Mangling

The module ID directly controls how anonymous namespaces are mangled in the `.int.c` output. The function `sub_6BC7E0` (in `nv_transforms.c`) constructs the anonymous namespace identifier:

```c
// sub_6BC7E0 implementation:
if (qword_1286A00)                      // cached?
    return qword_1286A00;
module_id = sub_5AF830(0);              // get or compute module ID
buf = malloc(strlen(module_id) + 12);   // "_GLOBAL__N_" = 11 chars + NUL
strcpy(buf, "_GLOBAL__N_");
strcat(buf, module_id);
qword_1286A00 = buf;                    // cache for reuse
return buf;
```

This `_GLOBAL__N_<module_id>` string is emitted in the `.int.c` trailer as:

```c
#define _NV_ANON_NAMESPACE _GLOBAL__N_<module_id>
#ifdef _NV_ANON_NAMESPACE
#endif
#include "<source_file>"
#undef _NV_ANON_NAMESPACE
```

The `#define` gives anonymous namespace entities a stable, unique mangled name that is consistent between the device and host compilation paths. The `#ifdef`/`#endif` guard is defensive -- it tests that the macro was defined (it always is at this point). The `#include` re-includes the original source file with the macro defined, allowing the host compiler to see the anonymous namespace entities with their module-ID-qualified names. The `#undef` cleans up to avoid polluting later inclusions.

The anonymous namespace hash also appears during host reference array name construction. For `static` or anonymous-namespace device entities, the scoped name prefix builder (`sub_6BD2F0`) inserts `_GLOBAL__N_<module_id>` as the namespace component, ensuring the mangled name in the `.nvHR*` section uniquely identifies the entity even across TUs with the same anonymous namespace structure.

### Usage in Output

The module ID appears in three places in the generated `.int.c` output:

1. **Anonymous namespace mangling**: `sub_6BC7E0` constructs `_GLOBAL__N_<module_id>` for anonymous-namespace symbols in device code, producing unique mangled names per TU.

2. **Registration boilerplate**: The `__cudaRegisterFatBinary` call passes the module ID to the CUDA runtime, which uses it to match host registration with the correct device fatbinary.

3. **Module ID file**: When requested, the ID is written to a separate file for consumption by the build system or nvlink.

## Cross-TU IL Correspondence

When multiple TUs are processed in a single `cudafe++` invocation, the same C++ types, templates, and declarations may appear in multiple TUs. EDG's correspondence system verifies structural equivalence and establishes canonical entries to avoid duplicate definitions in the merged output.

### trans_copy.c: IL Copying Between TUs

The `trans_copy.c` file contains a single function at address `0x796BA0`:

**`copy_secondary_trans_unit_IL_to_primary`** -- Copies IL entries from secondary translation units into the primary TU's IL tree. Called after all TUs have been parsed, during the `fe_wrapup` finalization phase (specifically, after the 5-pass multi-TU iteration). This function ensures that device-reachable IL entries from secondary TUs are available in the primary TU's output scope.

A closely related function exists at `0x796C00`:

**`mark_secondary_IL_entities_used_from_primary`** (`sub_796C00`) -- Called during `fe_wrapup` pass 2 (IL lowering), before the TU iteration loop that applies `sub_707040` to each TU's file-scope IL. This function marks IL entities in secondary TUs that are referenced from the primary TU, ensuring they survive any dead-code elimination in later passes.

### trans_corresp.c: Structural Equivalence Checking

The `trans_corresp.c` file (address range `0x796E60`--`0x7A3420`, 88 functions) implements the full cross-TU correspondence verification system. The core functions:

**`verify_class_type_correspondence`** (`sub_7A00D0`, 703 lines) is the centerpiece. It performs a deep structural comparison of two class types from different TUs:

1. **Base class comparison** via `sub_7A27B0` (`verify_base_class_correspondence`) -- iterates base class lists, comparing virtual/non-virtual status, accessibility, and type identity
2. **Friend declaration comparison** via `sub_7A1830` (`verify_friend_declaration_correspondence`) -- walks friend lists checking structural equivalence
3. **Member function comparison** via `sub_7A1DB0` (`verify_member_function_correspondence`, 411 lines) -- compares function signatures, attributes, constexpr status, and virtual overrides
4. **Nested type comparison** via `sub_798960` (`equiv_member_constants`) -- verifies nested class/enum/typedef correspondence
5. **Template parameter comparison** via `sub_7B2260` -- validates template parameter lists match structurally
6. **Using declaration comparison** -- dispatches by kind: `36` = alias, `6`/`11` = using declaration, `7`/`58` = namespace using declaration

If any comparison fails, the function delegates to `sub_797180` to emit a diagnostic (error codes 1795/1796), then falls through to `f_set_no_trans_unit_corresp` (5 variants at `sub_797B50`-`sub_7981A0` for different entity kinds).

The type node layout used by the correspondence system:
- Offset `+132`: type kind (9=struct, 10=class, 11=union)
- Offset `+144`: referenced type / next pointer
- Offset `+152`: class info pointer
- Offset `+161`: flags byte (bits for anonymous, elaborated, template, local)
- Class info at `+128`: scope block with members at indexed offsets `[12]`, `[13]`, `[14]`, `[18]`, `[22]`

**Supporting verification functions**:

| Address | Name | Scope |
|---|---|---|
| `sub_7A0E10` | `verify_enum_type_correspondence` | Enum underlying type and enumerator list |
| `sub_7A1230` | `verify_function_type_correspondence` | Parameter and return type |
| `sub_7A1390` | `verify_type_correspondence` | Dispatcher to class/enum/function variants |
| `sub_7A1460` | `set_type_correspondence` | Links two types as corresponding |
| `sub_7A1CC0` | `verify_nested_class_body_correspondence` | Nested class scope comparison |
| `sub_7A2C10` | `verify_template_parameter_correspondence` | Template parameter list |
| `sub_7A3140` | `check_decl_correspondence_with_body` | Declaration with definition |
| `sub_7A3420` | `check_decl_correspondence_without_body` | Declaration-only case |
| `sub_7A38A0` | `check_decl_correspondence` | Dispatcher (with/without body) |
| `sub_7A38D0` | `same_source_position` | Source position comparison |
| `sub_7999C0` | `find_template_correspondence` | Cross-TU template entity matching (601 lines) |
| `sub_79A5A0` | `determine_correspondence` | General correspondence determination |
| `sub_79B8D0` | `mark_canonical_instantiation` | Updates instantiation canonical status |
| `sub_79C1A0` | `get_canonical_entry_of` | Returns canonical entity for a TU entry |
| `sub_79D080` | `establish_instantiation_correspondences` | Links instantiations across TUs |
| `sub_79DFC0` | `set_type_corresp` | Sets type correspondence |
| `sub_79E760` | `find_routine_correspondence` | Cross-TU function matching |
| `sub_79F320` | `find_namespace_correspondence` | Cross-TU namespace matching |

### Correspondence Lifecycle

The correspondence system uses three hash tables (`qword_12C7800`, `qword_12C7880`, `qword_12C7900`, each 0x70 bytes / 14 slots) plus linked lists to track established correspondences. The lifecycle:

1. **Registration** (`sub_7A3920`): Registers three global variables (`dword_106B9E4`, `dword_106B9E0`, `qword_12C7798`) for per-TU save/restore
2. **Initialization** (`sub_7A3980`): Zeroes all correspondence hash tables and list pointers
3. **Discovery** during parsing: As the secondary TU is parsed, types/functions that match primary-TU entities are identified through name and scope comparison
4. **Verification**: `verify_class_type_correspondence` and its siblings perform deep structural comparison
5. **Linkage**: `set_type_correspondence` (`sub_7A1460`) and `f_set_trans_unit_corresp` (`sub_79C400`, 511 lines) connect matching entities
6. **Canonicalization**: `canonical_ranking` (`sub_796E60`) determines which TU's entity is the canonical representative; `mark_canonical_instantiation` (`sub_79B8D0`) updates instantiation records

The correspondence allocation uses 24-byte nodes from a free list (`qword_12C7AB0`) managed by `alloc_trans_unit_corresp` (`sub_7A3B50`) and `free_trans_unit_corresp` (`sub_7A3BB0`). The free function decrements a refcount at offset `+16`; when it reaches 1, the node returns to the free list.

### Integration with fe_wrapup

The cross-TU correspondence system hooks into the 5-pass multi-TU architecture in `fe_wrapup` (`sub_588E90`):

| Pass | Action | Cross-TU Role |
|---|---|---|
| 1 | Per-file IL wrapup (`sub_588C60`) | Iterates TU chain, prepares file scope IL |
| 2 | IL lowering (`sub_707040`) | Calls `sub_796C00` (mark secondary IL) before loop |
| 3 | IL emission (`sub_610420`, arg 23) | Marks device-reachable entries per TU |
| 4 | C++ class finalization | Deferred member processing |
| 5 | Per-file part 3 (`sub_588D40`) | Final per-TU cleanup |
| Post | Cleanup | Calls `sub_796BA0` (copy secondary IL to primary) |

After all five passes complete, `sub_796BA0` copies remaining secondary-TU IL into the primary TU's tree, and scope renumbering fixes up any index conflicts.

## Host Reference Arrays and Linkage Splitting

The six `.nvHR*` ELF sections emitted in the `.int.c` output trailer encode device symbol names for CUDA runtime discovery. These arrays are split along two axes: **symbol type** (kernel, device variable, constant variable) and **linkage** (external, internal). The split is critical for RDC: external-linkage symbols are globally resolvable by `nvlink` across all TUs, while internal-linkage symbols are TU-local and require module-ID-based prefixing to avoid collisions.

| Section | Array Name | Symbol Type | Linkage |
|---|---|---|---|
| `.nvHRKE` | `hostRefKernelArrayExternalLinkage` | `__global__` kernel | external |
| `.nvHRKI` | `hostRefKernelArrayInternalLinkage` | `__global__` kernel | internal |
| `.nvHRDE` | `hostRefDeviceArrayExternalLinkage` | `__device__` variable | external |
| `.nvHRDI` | `hostRefDeviceArrayInternalLinkage` | `__device__` variable | internal |
| `.nvHRCE` | `hostRefConstantArrayExternalLinkage` | `__constant__` variable | external |
| `.nvHRCI` | `hostRefConstantArrayInternalLinkage` | `__constant__` variable | internal |

The emission is driven by 6 calls to `nv_emit_host_reference_array` (`sub_6BCF80`, 79 lines, `nv_transforms.c`) with parameters `(emit_callback, is_kernel, is_device, is_internal_linkage)`:

```c
// From sub_489000 (process_file_scope_entities), backend output phase:
if (dword_106BFD0 || dword_106BFCC) {
    sub_6BCF80(sub_467E50, 1, 0, 1);  // kernel, internal
    sub_6BCF80(sub_467E50, 1, 0, 0);  // kernel, external
    sub_6BCF80(sub_467E50, 0, 1, 1);  // device, internal
    sub_6BCF80(sub_467E50, 0, 1, 0);  // device, external
    sub_6BCF80(sub_467E50, 0, 0, 1);  // constant, internal
    sub_6BCF80(sub_467E50, 0, 0, 0);  // constant, external
}
```

Each call iterates a separate global list that was populated during the entity walk:

| List Address | Content |
|---|---|
| `unk_1286880` | kernel external |
| `unk_12868C0` | kernel internal |
| `unk_1286780` | device external |
| `unk_12867C0` | device internal |
| `unk_1286800` | constant external |
| `unk_1286840` | constant internal |

Entity registration into these lists is performed by `nv_get_full_nv_static_prefix` (`sub_6BE300`, 370 lines, `nv_transforms.c:2164`). This function examines each device-annotated entity and routes it to the appropriate list based on its execution space bits (at entity offset `+182`) and linkage (internal linkage = `static` or anonymous namespace, determined by flags at entity offset `+80`).

For **internal linkage** entities, the function builds a scoped name prefix:
1. Recursively constructs the scope path via `sub_6BD2F0` (`nv_build_scoped_name_prefix`)
2. For anonymous namespaces, inserts the `_GLOBAL__N_<module_id>` prefix (via `qword_1286A00`)
3. Hashes the full path with `format_string_to_sso` (`sub_6BD1C0`)
4. Constructs the prefix: `off_E7C768 + len + "_" + filename + "_"`
5. Caches the prefix in `qword_1286760` for reuse
6. Appends `"_"` and the entity's mangled name

For **external linkage** entities, the path is simpler: the `::` scope-qualified name is used directly without module-ID-based prefixing.

The generated output for each symbol:

```c
extern "C" {
    extern __attribute__((section(".nvHRKE")))
           __attribute__((weak))
    const unsigned char hostRefKernelArrayExternalLinkage[] = {
        0x5f, 0x5a, /* ... mangled name bytes ... */ 0x00
    };
}
```

The `__attribute__((weak))` allows multiple TUs to define the same array without linker errors -- the CUDA runtime reads whichever copy survives.

## Host Stub Linkage Flags

Three CLI flags control the linkage of generated host stubs:

### --host-stub-linkage-explicit (Flag 47)

When set, host stubs are emitted with explicit linkage specifiers rather than relying on the default linkage of the surrounding context. This ensures that the stub's linkage matches what `nvcc`/`nvlink` expects regardless of the source file's linkage context (e.g., inside an anonymous namespace or `extern "C"` block).

### --static-host-stub (Flag 48)

Forces all generated host stubs (`__wrapper__device_stub_*`) to have `static` linkage. This is used in single-TU compilation where the stubs do not need to be visible to other object files. It prevents symbol conflicts when the same kernel name appears in multiple compilation units that are linked together.

### --static-global-template-stub (set_flag Mechanism)

Unlike the direct CLI flags above, `-static-global-template-stub` is set through the generic `--set_flag` mechanism (flag 193), which looks up the name in the `off_D47CE0` table and stores the value. It has 4 usage contexts in the binary, all in error message strings.

When enabled (`=true`), template `__global__` function stubs receive `static` linkage. This prevents ODR violations in whole-program mode when the same template kernel is instantiated in multiple host-side TUs. The tradeoff is that `extern` template kernels and out-of-TU instantiations become illegal (see the constraints in the whole-program section above).

## Output Differences Between Modes

| Output Aspect | Whole-Program (-rdc=false) | Separate Compilation (-rdc=true) |
|---|---|---|
| Host stub linkage | Can be `static` (with flags 47/48) | External (default) |
| Template stub linkage | `static` (with -static-global-template-stub) | External |
| Module ID generation | Generated but less critical | Required for registration matching |
| Module ID file | Optional | Typically generated |
| Device code embedding | Inline fatbinary in host object | Relocatable device object (.rdc) |
| nvlink requirement | No | Yes (resolves device symbols) |
| Dynamic parallelism | Forbidden | Allowed |
| Extern device variables | Forbidden | Allowed |
| Anonymous namespace hash | Used for device symbol uniqueness | Used for device symbol uniqueness |
| Deferred function list | Active (breakpoint placeholders) | Behavior depends on `dword_106BFDC` |
| Cross-TU correspondence | N/A (single TU) | Active when multi-TU invocation |

## Global Variables

| Address | Size | Name | Purpose |
|---|---|---|---|
| `dword_106BFBC` | 4 | `whole_program_mode` | Whole-program mode; also set by `--debug_mode` (flag 82, which sets `dword_106BFC4=1`, `dword_106BFC0=1`, `dword_106BFBC=1`) |
| `dword_106BFDC` | 4 | `skip_device_only` | Disables deferred function list accumulation |
| `dword_106BFB8` | 4 | `emit_symbol_table` | Emit symbol table + module ID to file |
| `dword_106BFD0` | 4 | `device_registration` | Device registration / cross-space reference checking |
| `dword_106BFCC` | 4 | `constant_registration` | Constant registration flag |
| `qword_126F0C0` | 8 | `cached_module_id` | Cached module ID string |
| `qword_106BF80` | 8 | `module_id_file_path` | Module ID file path (from `--module_id_file_name`) |
| `qword_106BA10` | 8 | `current_translation_unit` | Pointer to current TU descriptor |
| `qword_106B9F0` | 8 | `primary_translation_unit` | Pointer to first TU (primary) |
| `qword_106BA18` | 8 | `translation_unit_stack` | Top of TU stack |
| `dword_106B9E8` | 4 | `tu_stack_depth` | TU stack depth (excluding primary) |
| `qword_12C7AA8` | 8 | `registered_variable_list_head` | Per-TU variable registration list |
| `qword_12C7A98` | 8 | `per_tu_storage_size` | Total per-TU buffer size |
| `qword_12C7AB0` | 8 | `corresp_free_list` | Correspondence node free list |
| `qword_12C7AB8` | 8 | `stack_entry_free_list` | TU stack entry free list |
| `qword_1065840` | 8 | `deferred_function_list` | Breakpoint placeholder linked list head |

## Function Map

| Address | Name | Source File | Lines | Role |
|---|---|---|---|---|
| `sub_5AF830` | `make_module_id` | host_envir.c | ~450 | CRC32-based unique TU identifier |
| `sub_5AF7F0` | `set_module_id` | host_envir.c | ~10 | Setter for cached module ID |
| `sub_5AF820` | `get_module_id` | host_envir.c | ~3 | Getter for cached module ID |
| `sub_5B0180` | `write_module_id_to_file` | host_envir.c | ~30 | Writes module ID to file |
| `sub_5CF030` | `use_variable_or_routine_for_module_id_if_needed` | il.c:31969 | ~65 | Selects representative entity for ID |
| `sub_6BC7E0` | (anon namespace hash) | nv_transforms.c | ~20 | Generates `_GLOBAL__N_<module_id>` |
| `sub_6BCF80` | `nv_emit_host_reference_array` | nv_transforms.c | 79 | Emits `.nvHR*` ELF section with symbol names |
| `sub_6BD2F0` | `nv_build_scoped_name_prefix` | nv_transforms.c | ~95 | Recursive scope-qualified name builder |
| `sub_6BE300` | `nv_get_full_nv_static_prefix` | nv_transforms.c:2164 | ~370 | Scoped name + host ref array registration |
| `sub_796BA0` | `copy_secondary_trans_unit_IL_to_primary` | trans_copy.c | ~50 | Copies secondary TU IL to primary |
| `sub_796C00` | `mark_secondary_IL_entities_used_from_primary` | -- | -- | Marks secondary IL referenced from primary |
| `sub_796E60` | `canonical_ranking` | trans_corresp.c | -- | Determines canonical TU entry |
| `sub_7975D0` | `may_have_correspondence` | trans_corresp.c | -- | Quick correspondence eligibility check |
| `sub_797990` | `f_change_canonical_entry` | trans_corresp.c | -- | Updates canonical representative |
| `sub_7983A0` | `f_same_name` | trans_corresp.c | -- | Cross-TU symbol name comparison |
| `sub_79C400` | `f_set_trans_unit_corresp` | trans_corresp.c | 511 | Establishes entity correspondence |
| `sub_7A00D0` | `verify_class_type_correspondence` | trans_corresp.c | 703 | Deep class structural comparison |
| `sub_7A0E10` | `verify_enum_type_correspondence` | trans_corresp.c | -- | Enum comparison |
| `sub_7A1230` | `verify_function_type_correspondence` | trans_corresp.c | -- | Function type comparison |
| `sub_7A1460` | `set_type_correspondence` | trans_corresp.c | -- | Links corresponding types |
| `sub_7A1DB0` | `verify_member_function_correspondence` | trans_corresp.c | 411 | Member function comparison |
| `sub_7A27B0` | `verify_base_class_correspondence` | trans_corresp.c | -- | Base class list comparison |
| `sub_7A3920` | `register_trans_corresp_variables` | trans_corresp.c | -- | Registers per-TU state variables |
| `sub_7A3980` | `init_trans_corresp_state` | trans_corresp.c | -- | Zeroes all correspondence state |
| `sub_7A3A50` | `save_translation_unit_state` | trans_unit.c | -- | Saves current TU state to buffer |
| `sub_7A3C00` | `f_register_trans_unit_variable` | trans_unit.c | -- | Registers a per-TU variable |
| `sub_7A3CF0` | `fix_up_translation_unit` | trans_unit.c | -- | Finalizes TU state |
| `sub_7A3D60` | `switch_translation_unit` | trans_unit.c | -- | Saves/restores TU context |
| `sub_7A3EF0` | `push_translation_unit_stack` | trans_unit.c | -- | Pushes TU onto stack |
| `sub_7A3F70` | `pop_translation_unit_stack` | trans_unit.c | -- | Pops TU from stack |
| `sub_7A40A0` | `process_translation_unit` | trans_unit.c | -- | Main TU processing entry point |
| `sub_7A4690` | `register_builtin_trans_unit_variables` | trans_unit.c | -- | Registers 3 core per-TU vars |

## Cross-References

- [Kernel Stub Generation](./kernel-stubs.md) -- `-static-global-template-stub` details and the stub toggle mechanism
- [Device/Host Separation](./device-host-separation.md) -- How the single-pass tag-and-filter architecture works
- [.int.c File Format](../output/int-c-format.md) -- Anonymous namespace mangling and module ID in output
- [Backend Code Generation](../pipeline/backend.md) -- Module ID output phase
- [Host Reference Arrays](../output/host-reference-arrays.md) -- `.nvHR*` section format and runtime discovery
- [CLI Flag Inventory](../config/cli-flags.md) -- Flag indices 47, 48, 77, 83, 87
- [CUDA Error Catalog](../diagnostics/cuda-errors.md) -- Category 11 (RDC / whole-program diagnostics)
- [EDG 6.6 Overview](../edg/overview.md) -- Cross-TU correspondence section
- [Template Engine](../edg/template-engine.md) -- Template instantiation deduplication across TUs
- [Global Variable Index](../reference/global-variables.md) -- All globals referenced here
