# Host Reference Arrays

When cudafe++ splits a CUDA source file into device and host halves, the host-side `.int.c` output is compiled by a standard C++ compiler (GCC, Clang, or MSVC) that has no concept of device symbols. The CUDA runtime, however, needs to know which `__global__` kernels, `__device__` variables, and `__constant__` variables exist so it can register them at program startup. cudafe++ solves this by emitting **host reference arrays** — static byte arrays containing the mangled names of device symbols, placed into specially-named ELF sections that downstream tools (the fatbinary linker and `crt/host_runtime.h` registration code) read to enumerate device entities. The mechanism exists because the host compiler's symbol table contains only host-side symbols; the `.nvHR*` sections provide the complementary device-side symbol directory that the CUDA runtime needs to build the host-device binding table.

The arrays are emitted at the very end of the `.int.c` file, after the `#undef _NV_ANON_NAMESPACE` cleanup, by six calls to `nv_emit_host_reference_array` (`sub_6BCF80`, 79 lines, `nv_transforms.c`). Each call handles one combination of symbol type (kernel, device variable, constant variable) and linkage class (external, internal). The split by linkage is critical for RDC (relocatable device code) compilation: external-linkage symbols are globally visible across translation units and resolved by `nvlink`, while internal-linkage symbols (from `static` declarations or anonymous namespaces) are TU-local and must carry module-ID-based name prefixes to avoid collisions.

## Key Facts

| Property | Value |
|---|---|
| Emission function | `sub_6BCF80` (`nv_emit_host_reference_array`, 79 lines) |
| EDG source file | `nv_transforms.c` |
| Caller | `sub_489000` (`process_file_scope_entities`, lines 713--721) |
| Guard condition | `dword_106BFD0 \|\| dword_106BFCC` (device or constant registration enabled) |
| Emit callback | `sub_467E50` (primary string emitter to output stream) |
| Registration function | `sub_6BE300` (`nv_get_full_nv_static_prefix`, 370 lines, `nv_transforms.c:2164`) |
| Scope prefix builder | `sub_6BD2F0` (`nv_build_scoped_name_prefix`, 95 lines) |
| Expression walker | `sub_6BE330` (`nv_scan_expression_for_device_refs`, 89 lines) |
| List data structure | `std::list<std::string>`-like containers at 6 global addresses |
| Static prefix cache | `qword_1286760` |
| Anonymous namespace name | `qword_1286A00` (format: `_GLOBAL__N_<module_id>`) |
| Prefix format string | at `off_E7C768`, expanded as `"%s%lu_%s_"` |
| Assert guard | `nv_transforms.c:2164`, `"nv_get_full_nv_static_prefix"` |

## The Six Sections

The arrays are organized into 6 ELF sections along two axes: **symbol type** (3 values) and **linkage** (2 values):

| Section | Array Name | Symbol Type | Linkage | Global List Address |
|---|---|---|---|---|
| `.nvHRKE` | `hostRefKernelArrayExternalLinkage` | `__global__` kernel | External | `unk_1286880` |
| `.nvHRKI` | `hostRefKernelArrayInternalLinkage` | `__global__` kernel | Internal | `unk_12868C0` |
| `.nvHRDE` | `hostRefDeviceArrayExternalLinkage` | `__device__` variable | External | `unk_1286780` |
| `.nvHRDI` | `hostRefDeviceArrayInternalLinkage` | `__device__` variable | Internal | `unk_12867C0` |
| `.nvHRCE` | `hostRefConstantArrayExternalLinkage` | `__constant__` variable | External | `unk_1286800` |
| `.nvHRCI` | `hostRefConstantArrayInternalLinkage` | `__constant__` variable | Internal | `unk_1286840` |

The section name encoding is: `.nvHR` (host reference) + one letter for symbol type (`K`=kernel, `D`=device, `C`=constant) + one letter for linkage (`E`=external, `I`=internal).

Note that `__shared__` variables are **not** included — they have no host-visible address and exist only within a kernel's execution lifetime.

## Emission Architecture

### Invocation from the Backend

The backend entry point `sub_489000` (`process_file_scope_entities`) calls `sub_6BCF80` six times at the very end of `.int.c` generation (decompiled lines 713--721). The calls are guarded by two flags: `dword_106BFD0` (device registration mode) and `dword_106BFCC` (constant registration mode). If neither is set, no arrays are emitted.

```c
// sub_489000 trailer, decompiled lines 713-721
if (dword_106BFD0 || dword_106BFCC) {
    // nv_emit_host_reference_array(emit_fn, is_kernel, is_device, is_internal)
    sub_6BCF80(sub_467E50, 1, 0, 1);  // kernel,   internal  -> .nvHRKI
    sub_6BCF80(sub_467E50, 1, 0, 0);  // kernel,   external  -> .nvHRKE
    sub_6BCF80(sub_467E50, 0, 1, 1);  // device,   internal  -> .nvHRDI
    sub_6BCF80(sub_467E50, 0, 1, 0);  // device,   external  -> .nvHRDE
    sub_6BCF80(sub_467E50, 0, 0, 1);  // constant, internal  -> .nvHRCI
    sub_6BCF80(sub_467E50, 0, 0, 0);  // constant, external  -> .nvHRCE
}
```

The function signature is:

```c
void nv_emit_host_reference_array(
    void (*emit)(const char *),  // a1: string emission callback
    int is_kernel,               // a2: 1 = kernel, 0 = variable
    int is_device,               // a3: 1 = __device__, 0 = __constant__ (only when is_kernel=0)
    int is_internal              // a4: 1 = internal linkage, 0 = external linkage
);
```

The flag decoding for selecting which global list, section name, and array name to use works as follows:

```python
if is_kernel (a2 != 0):
    if is_internal (a4 != 0):  list = unk_12868C0, section = ".nvHRKI", name = "hostRefKernelArrayInternalLinkage"
    else:                       list = unk_1286880, section = ".nvHRKE", name = "hostRefKernelArrayExternalLinkage"
else if is_internal (a4 != 0):
    if is_device (a3 != 0):    list = unk_12867C0, section = ".nvHRDI", name = "hostRefDeviceArrayInternalLinkage"
    else:                       list = unk_1286840, section = ".nvHRCI", name = "hostRefConstantArrayInternalLinkage"
else:
    if is_device (a3 != 0):    list = unk_1286780, section = ".nvHRDE", name = "hostRefDeviceArrayExternalLinkage"
    else:                       list = unk_1286800, section = ".nvHRCE", name = "hostRefConstantArrayExternalLinkage"
```

Note the precedence: the kernel flag is checked first. When `is_kernel=1`, the `is_device` flag is ignored entirely — kernels are always kernels regardless of `is_device`.

### Emission Output Format

For each section, `sub_6BCF80` emits a single array declaration:

```c
extern "C" {
extern __attribute__((section(".nvHRKE")))
       __attribute__((weak))
const unsigned char hostRefKernelArrayExternalLinkage[] = {
/* _Z8myKernelPfi */
0x5f,0x5a,0x38,0x6d,0x79,0x4b,0x65,0x72,0x6e,0x65,0x6c,0x50,0x66,0x69,0x0,
/* _Z12otherKernelPd */
0x5f,0x5a,0x31,0x32,0x6f,0x74,0x68,0x65,0x72,0x4b,0x65,0x72,0x6e,0x65,0x6c,0x50,0x64,0x0,
0x0};
}
```

Key details about the emitted C:

- **`extern "C"`** wrapping ensures no C++ name mangling is applied to the array itself. The section name in the ELF binary is the sole identifier.
- **`__attribute__((section(".nvHRXX")))`** places the array in a named ELF section that downstream tools scan by name.
- **`__attribute__((weak))`** allows multiple translation units to define the same array name without causing linker errors. When multiple TUs each emit their own `hostRefKernelArrayExternalLinkage`, the linker keeps one copy. This is safe because the CUDA runtime reads the section contents, not the symbol — it concatenates all `.nvHRKE` section contributions from all object files.
- **`const unsigned char[]`** encodes each mangled name as individual hex bytes, not as a string literal. This avoids any issues with embedded NUL bytes or special characters in mangled names.
- Each symbol name is preceded by a `/* mangled_name */` comment for human readability.
- Each name is terminated by `0x0` (NUL byte).
- If the list is empty (no symbols of that type/linkage), the array contains a single `0x0` sentinel.

The iteration traverses a doubly-linked list rooted at the global list variable. From the decompiled code:

```c
// Decompiled iteration in sub_6BCF80, lines 56-73
for (node = list[3]; list + 1 != node; node = next_node(node)) {
    emit("/* ");
    emit(*(char **)(node + 32));   // mangled name string
    emit(" */\n");
    size_t len = *(size_t *)(node + 40);  // string length
    for (size_t j = 0; j < len; j++) {
        char byte = *(char *)(*(char **)(node + 32) + j);
        snprintf(buf, 128, "0x%x,", byte);
        emit(buf);
    }
    emit("0x0,");  // NUL terminator for this name
}
```

Each node in the linked list stores:
- `+32`: pointer to the mangled name string
- `+40`: length of the mangled name

The list structure itself is a `std::list<std::string>`-compatible container where `list[3]` (offset +24) points to the first data node and `list + 1` (offset +8) is the sentinel/end node.

## Symbol Registration Pipeline

The host reference arrays are the output of a two-phase pipeline: (1) symbol collection during compilation, and (2) array emission at the end of the backend pass.

### Phase 1: Collection During Compilation

As cudafe++ processes the AST, it encounters declarations marked with `__global__`, `__device__`, or `__constant__`. Each such entity must be registered in the appropriate global list so it appears in the host reference array. This registration is performed by two cooperating functions:

**`nv_scan_expression_for_device_refs` (`sub_6BE330`, 89 lines)** recursively walks expression trees looking for references to device-annotated entities. It dispatches on expression kind:

| Expression Kind | Handling |
|---|---|
| 7 (variable reference) | Checks `__global__` bit, registers if device-annotated |
| 11 (function reference) | Checks function attributes, registers if `__global__` |
| 15 (member access) | Recurses on the member |
| 16 (pointer dereference) | Recurses on the operand |
| 17 (expression list) | Recurses on each element |
| 20 (call expression) | Checks the callee |
| 24 (cast expression) | Recurses on the operand |

When the walker finds a device entity, it tail-calls into `nv_get_full_nv_static_prefix`.

**`nv_get_full_nv_static_prefix` (`sub_6BE300`, 370 lines)** is the master registration function. It determines the symbol's linkage class and constructs the name that goes into the host reference array. The function begins with two early-exit checks:

```c
if (!entity) return;
if ((entity[182] & 0x40) == 0) return;  // not __global__
```

Byte `+182` of the entity node carries execution space bits. Bit 6 (`0x40`) indicates `__global__`. Byte `+179` carries additional flags where bits `0x12` indicate device/constant annotation. Byte `+80` bits `0x70` encode the linkage class: `0x10` = internal (static/anonymous), `0x30` = external.

The function then splits into two paths based on linkage:

### Internal Linkage Path

For `static` functions, anonymous-namespace entities, or entities with forced internal linkage, the name must include a TU-unique prefix to prevent collisions across translation units:

1. **Scope prefix construction** (`sub_6BD2F0`): Recursively walks the entity's enclosing scopes (byte `+28 == 3` indicates "has parent scope"). For each scope level, the scope name is extracted from `+32 -> +8` (the scope's identifier string). For anonymous namespaces (where the scope name pointer is NULL), the function substitutes `_GLOBAL__N_<module_id>`, constructing and caching this string in `qword_1286A00`.

2. **Hash computation** (`sub_6BD1C0`): The scope-qualified name is hashed using `vsnprintf` with format string at address `8573734` (likely `"%s%lu"` or similar) and a 32-byte buffer. This produces a deterministic hash of the scope path.

3. **Static prefix construction**: The full prefix is assembled as:
   ```c
   snprintf(buf, size, "%s%lu_%s_", off_E7C768, strlen(module_id), module_id)
   ```
   where `off_E7C768` is a fixed prefix string (likely `"__nv_static_"` or similar) and `module_id` comes from `sub_5AF830` (the CRC32-based module identifier). The result is cached in `qword_1286760` so it is computed only once per TU.

4. **Name assembly**: The prefix, a `"_"` separator, and the entity's mangled name (from entity `+8`) are concatenated.

5. **List insertion**: The assembled name is pushed into the internal-linkage list (`unk_12868C0` for kernels, `unk_12867C0` for device variables, `unk_1286840` for constants) via a `std::list::push_back`-equivalent call.

### External Linkage Path

For entities with default (external) linkage, the path is simpler:

1. A `" ::"` scope prefix is prepended (string at address `10998575`, corresponding to `" ::"` — two bytes).

2. If the entity has a parent scope (byte `+28 == 3` at the scope entry), the scope-qualified name is built by recursing through parent scopes, concatenating `"::"` separators and hashing each level with `sub_6BD1C0`.

3. The entity's mangled name (from entity `+8`) is appended directly.

4. The result is pushed into the external-linkage list (`unk_1286880` for kernels, `unk_1286780` for device variables, `unk_1286800` for constants).

### Phase 2: Emission (Backend Trailer)

After the entire source file has been processed and all entity walks have populated the 6 global lists, the backend trailer calls `sub_6BCF80` six times. Each call drains one list and emits the corresponding ELF section declaration. The emission is always performed for all 6 sections, even if some lists are empty (producing arrays with only a `0x0` sentinel).

## Internal vs. External Linkage Split

The split into internal and external linkage sections serves two distinct purposes:

### Whole-Program Mode (`-rdc=false`)

In whole-program (non-RDC) mode, all device code from a single TU is embedded directly in the host object file as a fatbinary. The host reference arrays tell `crt/host_runtime.h`'s `__cudaRegisterLinkedBinary` machinery which symbols exist in the fatbinary so it can register them with the CUDA driver at program startup.

Internal-linkage symbols require the TU-unique prefix to avoid name collisions if two TUs define identically-named `static __global__` kernels. The prefix incorporates the module ID (a CRC32 of the TU's representative entity) to ensure uniqueness.

### Separate Compilation Mode (`-rdc=true`)

In RDC mode, device code is compiled to relocatable device objects (`.rdc` files) that `nvlink` links together. External-linkage device symbols must be globally resolvable across TUs. The `.nvHRKE`/`.nvHRDE`/`.nvHRCE` sections provide the symbol directory that `nvlink` uses to match device symbols with their host-side registration entries.

Internal-linkage symbols in RDC mode remain TU-local. They carry module-ID prefixes and are placed in the `*I` sections, which `nvlink` processes separately. The split ensures that `nvlink` does not attempt to deduplicate or cross-reference symbols that were intentionally given internal linkage.

## Downstream Consumption

### Host Compiler

GCC/Clang/MSVC compiles the `.int.c` file and sees the `extern "C"` array declarations with `__attribute__((section(...)))`. The host compiler places each array into the named ELF section (or PE section on Windows). Because the arrays are `const unsigned char[]` with `weak` linkage, they impose no runtime overhead and can be safely deduplicated by the linker.

### Fatbinary Linker (fatbinary / nvlink)

The fatbinary linker reads the `.nvHR*` sections from each object file to discover which device symbols need registration. For each entry in the byte arrays, it extracts the mangled name (scanning for `0x0` terminators) and matches it against the device code in the fatbinary or relocatable device object.

### CUDA Runtime (`crt/host_runtime.h`)

At program startup, the CUDA runtime's `__cudaRegisterLinkedBinary` function (or `__cudaRegisterFatBinary` in whole-program mode) walks the `.nvHR*` sections to:

1. Register each `__global__` kernel with `cudaRegisterFunction`
2. Register each `__device__` variable with `cudaRegisterVar`
3. Register each `__constant__` variable with `cudaRegisterVar` (with the constant flag)

This registration enables the host-side API (`cudaLaunchKernel`, `cudaMemcpyToSymbol`, etc.) to resolve device symbols by name at runtime.

## Supporting Data Structures

### Global List Nodes

Each of the 6 global lists (`unk_1286780` through `unk_12868C0`) is a `std::list<std::string>`-compatible doubly-linked list. The list head structure occupies 48 bytes (3 pointers + metadata):

| Offset | Field | Description |
|---|---|---|
| +0 | `allocator` | Allocator state |
| +8 | `sentinel` | Sentinel/end node address (comparison target for iteration end) |
| +16 | `size` | Number of entries |
| +24 | `first` | Pointer to first data node |

Each data node stores:

| Offset | Field | Description |
|---|---|---|
| +0 | `prev` | Previous node pointer |
| +8 | `next` | Next node pointer |
| +16 | `data_start` | Start of string data area |
| +32 | `str_ptr` | Pointer to mangled name character data |
| +40 | `str_len` | Length of the mangled name |

The strings use SSO (Small String Optimization): if the mangled name is 15 bytes or shorter, the character data is stored inline starting at offset `+16`; otherwise `str_ptr` at `+32` points to a heap allocation and offset `+16` stores the heap capacity.

### Static Prefix Cache

`qword_1286760` caches the internal-linkage prefix string computed by `nv_get_full_nv_static_prefix`. The format is:

```text
<off_E7C768><module_id_length>_<module_id>_
```

Where `off_E7C768` is a fixed string (the NVIDIA static prefix marker), the module ID comes from `sub_5AF830` (CRC32-based), and the underscores separate the components. This prefix is allocated once via `sub_5E03D0` and reused for all internal-linkage entities in the TU.

### Anonymous Namespace Name Cache

`qword_1286A00` caches the anonymous namespace identifier, constructed as `_GLOBAL__N_<module_id>`. This follows the Itanium ABI convention for anonymous namespace mangling but uses the CUDA module ID instead of a random hash. It is allocated once by `sub_6BD2F0` and reused for all entities in anonymous namespaces.

### Scope-Qualified Name Builder

`sub_6BD2F0` (`nv_build_scoped_name_prefix`) recursively constructs scope-qualified names for internal-linkage entities:

```c
void nv_build_scoped_name_prefix(char **scope_name, scope_entry *parent, string *result) {
    // Recurse to parent scope first
    if (parent && parent->kind == 3)  // byte +28 == 3
        nv_build_scoped_name_prefix(parent->parent->name, parent->parent->scope, result);

    char *name = *scope_name;
    if (!name)
        name = get_or_create_anon_namespace_name();  // _GLOBAL__N_<module_id>

    // Build: hash(name) via vsnprintf with format at 8573734, 32-byte buffer
    // Append to result string
    format_string_to_sso(&tmp, vsnprintf, 32, 8573734, name_len);
    string_append(result, tmp);
}
```

The recursion visits ancestor scopes from outermost to innermost, concatenating hashed scope names. This produces a deterministic, collision-resistant path that uniquely identifies the entity's position in the namespace hierarchy.

## Host Reference Trie

During compilation, cudafe++ maintains a trie (prefix tree) structure for deduplicating host reference entries. This trie is stored alongside the linear lists and prevents the same symbol from being registered twice if it is referenced from multiple points in the source.

The trie is cleaned up at the end of compilation by:
- `sub_6BD530` (`nv_free_host_ref_tree`, 257 lines) — deeply recursive tree destructor with 9 levels of inlined recursion
- `sub_6BD820` (`nv_free_host_ref_list`, 34 lines) — iterates the linked list, calling `nv_free_host_ref_tree` for each node's tree, then frees the node

Each trie node structure:

| Offset | Field | Description |
|---|---|---|
| +0 | `next` | Next sibling pointer |
| +8 | (reserved) | Alignment/flags |
| +16 | `child_chain` | First child in chain |
| +24 | `child_tree` | Child subtree pointer |
| +32 | `data_ptr` | Pointer to name data (or `+48` if inline) |
| +40 | `data_len` | Length of name data |
| +48 | `inline_data` | Inline storage for short names |

If `data_ptr == &node[48]` (the inline data area), no separate allocation was made; otherwise `data_ptr` points to a heap-allocated string that `nv_free_host_ref_tree` frees separately.

## Complete Emission Example

For a source file containing:

```cuda
__global__ void myKernel(float *data, int n) { /* ... */ }
__device__ int d_counter;
static __constant__ float c_table[256];
```

The `.int.c` trailer emits:

```c
extern "C" {
extern __attribute__ ((section (".nvHRKI"))) __attribute__((weak)) const unsigned char hostRefKernelArrayInternalLinkage[] = {
0x0};

extern "C" {
extern __attribute__ ((section (".nvHRKE"))) __attribute__((weak)) const unsigned char hostRefKernelArrayExternalLinkage[] = {
/* _Z8myKernelPfi */
0x5f,0x5a,0x38,0x6d,0x79,0x4b,0x65,0x72,0x6e,0x65,0x6c,0x50,0x66,0x69,0x0,
0x0};
}

extern "C" {
extern __attribute__ ((section (".nvHRDI"))) __attribute__((weak)) const unsigned char hostRefDeviceArrayInternalLinkage[] = {
0x0};
}

extern "C" {
extern __attribute__ ((section (".nvHRDE"))) __attribute__((weak)) const unsigned char hostRefDeviceArrayExternalLinkage[] = {
/* _Z9d_counter */
0x5f,0x5a,0x39,0x64,0x5f,0x63,0x6f,0x75,0x6e,0x74,0x65,0x72,0x0,
0x0};
}

extern "C" {
extern __attribute__ ((section (".nvHRCI"))) __attribute__((weak)) const unsigned char hostRefConstantArrayInternalLinkage[] = {
/* __nv_static_42_kernel_cu_c_table */
0x5f,0x5f,0x6e,0x76,0x5f,...,0x0,
0x0};
}

extern "C" {
extern __attribute__ ((section (".nvHRCE"))) __attribute__((weak)) const unsigned char hostRefConstantArrayExternalLinkage[] = {
0x0};
}
```

Note how `c_table` (declared `static __constant__`) appears in the internal-linkage `.nvHRCI` section with its module-ID-prefixed name, while `myKernel` (external linkage by default) appears in `.nvHRKE` with its standard Itanium-ABI mangled name.

## Function Map

| Address | Name | Source | Lines | Role |
|---|---|---|---|---|
| `sub_6BCF80` | `nv_emit_host_reference_array` | nv_transforms.c | 79 | Selects section/list by flags, emits array declaration |
| `sub_6BE300` | `nv_get_full_nv_static_prefix` | nv_transforms.c:2164 | 370 | Master registration: determines linkage, builds name, inserts into list |
| `sub_6BE330` | `nv_scan_expression_for_device_refs` | nv_transforms.c | 89 | Recursive expression walker that finds device entity references |
| `sub_6BD2F0` | `nv_build_scoped_name_prefix` | nv_transforms.c | 95 | Recursive scope-qualified name builder for internal-linkage entities |
| `sub_6BD1C0` | `format_string_to_sso` | nv_transforms.c | 48 | Formats via `vsnprintf` into std::string SSO buffer |
| `sub_6BD530` | `nv_free_host_ref_tree` | nv_transforms.c | 257 | Recursive deep-free of deduplication trie |
| `sub_6BD820` | `nv_free_host_ref_list` | nv_transforms.c | 34 | Frees linked list of host reference entries |
| `sub_6BCF10` | `nv_check_device_variable_in_host` | nv_transforms.c | 16 | Validates device variable not improperly referenced from host |
| `sub_5AF830` | `make_module_id` | host_envir.c | ~450 | CRC32-based TU identifier used in internal-linkage prefixes |
| `sub_489000` | `process_file_scope_entities` | cp_gen_be.c | 723 | Backend entry point; calls `sub_6BCF80` x6 in trailer |
| `sub_467E50` | (emit string) | cp_gen_be.c | — | Primary string emission callback passed to `sub_6BCF80` |

## Global Variables

| Address | Type | Name | Purpose |
|---|---|---|---|
| `unk_1286780` | list | device external list | Accumulates `__device__` external-linkage symbol names |
| `unk_12867C0` | list | device internal list | Accumulates `__device__` internal-linkage symbol names |
| `unk_1286800` | list | constant external list | Accumulates `__constant__` external-linkage symbol names |
| `unk_1286840` | list | constant internal list | Accumulates `__constant__` internal-linkage symbol names |
| `unk_1286880` | list | kernel external list | Accumulates `__global__` external-linkage symbol names |
| `unk_12868C0` | list | kernel internal list | Accumulates `__global__` internal-linkage symbol names |
| `qword_1286760` | char* | static prefix cache | Cached internal-linkage prefix string (computed once per TU) |
| `qword_1286A00` | char* | anon namespace name | Cached `_GLOBAL__N_<module_id>` string |
| `dword_106BFD0` | int | device registration flag | Enables device symbol registration (guard for emission) |
| `dword_106BFCC` | int | constant registration flag | Enables constant symbol registration (guard for emission) |

## Cross-References

- [.int.c File Format](./int-c-format.md) — complete file structure showing where host reference arrays sit (sections 13--14)
- [CUDA Runtime Boilerplate](./cuda-runtime.md) — managed memory initialization that references registered symbols
- [Module ID & Registration](./module-id.md) — CRC32 hash computation used in internal-linkage prefixes
- [RDC Mode](../cuda/rdc-mode.md) — how the internal/external split interacts with separate compilation
- [Memory Spaces](../cuda/memory-spaces.md) — `__device__` / `__constant__` / `__shared__` attribute encoding
- [Name Mangling](../edg/name-mangling.md) — `nv_get_full_nv_static_prefix` and Itanium ABI encoding
- [Backend Code Generation](../pipeline/backend.md) — Phase 7 host reference array emission
- [CLI Flag Inventory](../config/cli-flags.md) — flags controlling device/constant registration
