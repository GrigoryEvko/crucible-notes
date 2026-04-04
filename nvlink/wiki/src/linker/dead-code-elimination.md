# Dead Code Elimination

nvlink includes a dead code elimination (DCE) pass that removes unreachable device functions and their associated sections from the linked output. The pass runs between the merge phase and the layout phase, operating on the merged callgraph to determine which functions are live. Unlike a traditional linker's `--gc-sections`, nvlink's DCE is driven by explicit liveness information supplied either by the host compiler (via `--use-host-info`) or by the user (via `--kernels-used` / `--variables-used`).

DCE is the primary mechanism by which nvlink avoids bloating the final cubin with device code that the host application never launches. In a typical separable compilation workflow, every translation unit contributes all its `__global__` and `__device__` functions to the link, but only a subset may be reachable from `<<<>>>` launch sites in the host code. The DCE pass eliminates the rest.

## Key Facts

| Property | Value |
|---|---|
| Gate function | `sub_426AE0` (`mark_used_symbols`) |
| Core DCE function | `sub_44AD40` (`dead_code_eliminate`) |
| Address / size | `0x426AE0` (2,178 B) / `0x44AD40` (22,503 B) |
| Kernel filter | `sub_43F360` (`kernels_used_filter`) |
| Variable filter | `sub_43F950` (`variables_used_filter`) |
| Host-info dispatcher | `sub_43E7A0` (`add_referenced_symbols`) |
| Pipeline position | After merge, before shared-memory layout |
| Guard condition | `byte_2A5F214 && (!byte_2A5F288 \|\| byte_2A5F285)` |
| Verbose diagnostic | `ctx+64 bit 0` (the `-v` flag) |
| Diagnostic messages | `"dead function %d(%s)\n"`, `"removed un-used section %s (%d)\n"` |

## Activation Conditions

DCE is controlled by three interacting CLI options and one internal flag:

```
byte_2A5F214  -- mark-used          (composite: set if any liveness source is active)
byte_2A5F213  -- use-host-info      (host compiler provided reference lists)
byte_2A5F212  -- ignore-host-info   (force-disable host info)
byte_2A5F288  -- link-time-opt      (-lto flag)
byte_2A5F285  -- force-partial-lto
```

The option parser (`sub_427AE0`) determines the final state:

```c
// Simplified logic from sub_427AE0 at line ~1117
if (relocatable_link)
    ignore_host_info = 1;                         // -r disables DCE

if (kernels_used || variables_used) {
    if (use_host_info)
        warn("ignore -use-host-info because -kernels-used or -variables-used is specified");
    use_host_info = 0;
    mark_used = 1;                                // explicit user lists take precedence
} else if (!ignore_host_info) {
    use_host_info = 1;
    mark_used = 1;                                // default: use host info when available
}
```

The guard in `main()` additionally suppresses DCE when full LTO is active (since the LTO pipeline performs its own whole-program optimization), unless `--force-partial-lto` is set:

```c
// main() at ~line 1427
if (mark_used && (!lto || force_partial_lto))
    sub_426AE0(ctx, input_object_list);
```

### Summary of Modes

| Configuration | DCE active? | Liveness source |
|---|---|---|
| Default (no explicit flags) | Yes | Host reference info from input objects |
| `--kernels-used=...` and/or `--variables-used=...` | Yes | User-provided name lists |
| `--use-host-info` (explicit) | Yes | Host reference info |
| `--ignore-host-info` | No | -- |
| `--relocatable-link` / `-r` | No | -- (implies ignore-host-info) |
| `--lto` (full LTO) | No | LTO does its own DCE |
| `--lto --force-partial-lto` | Yes | Host reference info or user lists |

## Liveness Seeding

Before the core DCE pass runs, the linker must determine which symbols are "live roots". There are two liveness sources, with explicit user lists taking priority over host info.

### Source 1: Host Reference Info (`--use-host-info`)

When the host compiler (nvcc's separable compilation mode) generates device code, it embeds reference lists into each input object. These lists record which device kernels, constants, and globals are referenced from the host side. The gate function `sub_426AE0` iterates the input object list and dispatches six categories of references:

```c
// sub_426AE0 -- per-object host info dispatch
for (obj = input_list; obj; obj = obj->next) {
    host_info = obj->host_info_record;
    if (host_info->external_kernels)   sub_43F020(ctx);   // "external kernel"
    if (host_info->internal_kernels)   sub_43F040(ctx);   // "internal kernel"
    if (host_info->external_constants) sub_43F100(ctx);   // "external constant"
    if (host_info->internal_constants) sub_43F1C0(ctx);   // "internal constant"
    if (host_info->external_globals)   sub_43F280(ctx);   // "external global"
    if (host_info->internal_globals)   sub_43F340(ctx);   // "internal global"
}
```

Each of these six small dispatcher functions calls the shared `sub_43E7A0` (`add_referenced_symbols`), passing the appropriate liveness set from the linker context. The context maintains six symbol sets at fixed offsets:

| Offset | Set | Populated by |
|---|---|---|
| `ctx+520` | External kernels | `sub_43F020` |
| `ctx+528` | Internal kernels | `sub_43F040` |
| `ctx+536` | External constants | `sub_43F100` |
| `ctx+544` | Internal constants | `sub_43F1C0` |
| `ctx+552` | External globals | `sub_43F280` |
| `ctx+560` | Internal globals | `sub_43F340` |

`sub_43E7A0` iterates the reference list from the input object and, for each symbol name not already present in the set, allocates a copy and inserts it:

```c
// sub_43E7A0 -- add_referenced_symbols
void add_referenced_symbols(ctx, set_ptr, ref_list, category_name) {
    for (iter = list_begin(ref_list); !list_end(iter); iter = list_next(iter)) {
        name = list_value(iter);
        if (!set_contains(*set_ptr, name)) {
            if (verbose)
                fprintf(stderr, "add referenced %s: %s\n", category_name, name);
            copy = arena_strdup(name);
            set_insert(*set_ptr, copy);
        }
    }
}
```

When verbose mode is active, each insertion prints a diagnostic:
```
add referenced external kernel: my_kernel
add referenced internal constant: my_const
```

#### Incomplete Host Info

`sub_426AE0` checks a flag at `host_info+24` on each input object to determine whether host info is present. If any input object lacks host info (the flag is 0) and the object is not `cudadevrt`, the linker sets `ignore_host_info = 1` and `mark_all_used = 1`, effectively disabling selective DCE:

```c
// Simplified from sub_426AE0
all_have_info = true;
has_host_info_objects = false;

for (obj = input_list; obj; obj = obj->next) {
    if (obj->has_host_info) {
        if (!strstr(obj->name, "cudadevrt"))
            has_host_info_objects = true;
    } else {
        if (!strstr(obj->name, "cudadevrt"))
            all_have_info = false;
    }
}

if (all_have_info) {
    // All objects have host info; safe to use it for DCE
    mark_all_used = true;    // byte_2A5F211
} else if (ignore_host_info_flag) {
    // Explicitly told to ignore -> skip host info processing entirely
    mark_all_used = true;
} else {
    // Some objects lack host info -> incomplete, cannot safely DCE
    if (verbose)
        fwrite("incomplete so ignore host info\n", stderr);
    ignore_host_info = true;
    mark_all_used = true;
}
```

The `cudadevrt` library is exempt from this check because it never contains host reference info -- it is a pure device-side runtime library.

### Source 2: Explicit Symbol Lists (`--kernels-used` / `--variables-used`)

When the user passes `--kernels-used=name1,name2,...` or `--variables-used=name1,name2,...`, these take absolute precedence over host info. The option parser sets `use_host_info = 0` and warns if it was previously enabled.

#### Kernel Name Normalization (`sub_43F360`)

`sub_43F360` processes the `--kernels-used` list and normalizes each name into a wildcard pattern for matching:

```c
// sub_43F360 -- kernels_used_filter
for (item = kernels_used_list; item; item = item->next) {
    name = item->value;
    len = strlen(name);
    first = name[0];
    last = name[len - 1];

    if (first == '*' && last == '*') {
        // Already has both wildcards: "*foo*" -> copy as-is
        pattern = arena_strdup(name);
    } else if (first == '*') {
        // Leading wildcard only: "*foo" -> "*foo*"  (append trailing *)
        pattern = arena_sprintf("%s*", name);
    } else if (last == '*') {
        // Trailing wildcard only: "foo*" -> "*foo*"  (prepend leading *)
        pattern = arena_sprintf("*%s", name);
    } else {
        // No wildcards: "foo" -> "*foo*"  (wrap both sides)
        pattern = arena_sprintf("*%s*", name);
    }
    item->value = pattern;
}

// Apply to both external and internal kernel sets
ctx->mark_used_flag = 1;
mark_matching(ctx, &ctx->external_kernel_set, kernels_used_list, "external kernel");
mark_matching(ctx, &ctx->internal_kernel_set, kernels_used_list, "internal kernel");
```

The wildcard wrapping ensures substring matching. A user-specified `--kernels-used=my_kern` will match any kernel whose mangled name contains `my_kern` as a substring, since the pattern becomes `*my_kern*`. The matching is performed by `sub_43E7A0` using the same set-insertion logic as host info.

#### Variable Name Normalization (`sub_43F950`)

`sub_43F950` processes the `--variables-used` list with identical wildcard normalization. However, it inserts matches into four variable sets rather than two kernel sets:

```c
// sub_43F950 -- variables_used_filter
for (item = variables_used_list; item; item = item->next) {
    name = item->value;
    // ... same wildcard normalization as kernels ...
    pattern = normalize_wildcards(name);

    if (verbose)
        fprintf(stderr, "add referenced variable: %s\n", pattern);

    ctx->mark_used_flag = 1;
    set_insert(ctx->external_constant_set, pattern);    // ctx+536
    set_insert(ctx->internal_constant_set, pattern);    // ctx+544
    set_insert(ctx->external_global_set, pattern);      // ctx+552
    set_insert(ctx->internal_global_set, pattern);      // ctx+560
}
```

Variables are inserted into all four data sets (external/internal constants and globals) because the user typically does not distinguish between constant memory and global memory when specifying variable names.

## Core DCE Pass (`sub_44AD40`)

After liveness seeding, `sub_426AE0` calls `sub_44AD40` -- the core dead code elimination function at 22,503 bytes. This function iterates the merged callgraph and removes every function (and its associated sections) that is not reachable from any live root.

### Algorithm Overview

The pass operates in two phases within a single function:

**Phase 1: Primary sweep.** Iterate all entries in the callgraph (stored in the vector at `ctx+408`). For each function:

1. Retrieve the function's section record via `sub_440590`.
2. Check if the function is an entry point (a `__global__` kernel) using `sub_44A520`. Entry points are always live.
3. Check if the function has callers (field at `callgraph_entry+40`). Functions with callers are live.
4. If the function has its address taken (`callgraph_entry+50` flag) but no direct callers, it is kept alive but a diagnostic is printed:
   ```
   function %d(%s) has address taken but no call to it
   ```
   When `--extra-warnings` is set, this also emits a linker warning via `sub_467460`.
5. If the function is reachable via `sub_440350` (resolves the section's link to the symbol table), check whether the function's callee is `__cuda_syscall_32f3056bbb` (via `sub_444830`). Syscall stubs are never eliminated.
6. Check whether the section has flag `0x04` set (indicating a kept/pinned section) or whether the function is a UF stub (`sub_440230` checks for `__cuda_uf_stub_` prefix). These are never eliminated.
7. If none of the above conditions apply, the function is dead.

**Phase 2: Deferred sweep.** Some functions cannot be determined dead in the first pass because they reference sections that still appear live. These are collected into a deferred list (`v165`) during Phase 1 via `sub_4644C0`. After the primary sweep completes, the deferred list is re-examined with the same liveness criteria. Functions that remain unreachable after Phase 1's removals are eliminated in this second pass.

### Section Removal

When a function is determined dead, `sub_44AD40` removes not just the function's code section but all associated sections:

```c
// Simplified from sub_44AD40 -- removing a dead function
if (verbose)
    fprintf(stderr, "dead function %d(%s)\n", func_id, section->name);

// 1. Mark the function section as dead (flags = flags & 0xFC | 1)
section->flags = (section->flags & 0xFC) | 0x01;

// 2. Get the section index of the function's code
code_secidx = get_section_index(ctx, func_id);
code_section = get_section_record(ctx, code_secidx);

// 3. Remove the code section itself
remove_section(ctx, code_secidx);

// 4. Remove associated SHT_PROGBITS with type 0x70000000 (NVIDIA-specific)
nvidia_secidx = find_related_section(ctx, code_secidx, 0x70000000);
if (nvidia_secidx)
    remove_section(ctx, nvidia_secidx);

// 5. Remove associated SHT_RELA (relocation section, type 9)
rela_secidx = find_related_section(ctx, code_secidx, SHT_RELA);
if (rela_secidx)
    remove_section(ctx, rela_secidx);

// 6. Remove associated SHT_NOTE (type 4)
note_secidx = find_related_section(ctx, code_secidx, SHT_NOTE);
if (note_secidx)
    remove_section(ctx, note_secidx);
```

Each `remove_section` call:
- Zeros the section's data pointer (offset 32) and sets its size to 1 (a sentinel indicating removal)
- Iterates the section's relocation list, calling `sub_431000` (arena free) on each relocation entry
- Frees the relocation list via `sub_464520`
- Prints `"removed un-used section %s (%d)\n"` when verbose

### OCG Constant Section Handling

For functions that have associated OCG (optimized constant generation) sections, the pass performs additional cleanup. It constructs a composite section name from the ELF writer's prefix and the function name:

```c
sprintf(buf, "%s.%s", elf_writer_prefix(), function_name);
```

If this section exists (looked up via `sub_4411D0`), and it belongs to the same parent section as the dead function's code, it is removed. If the OCG constant has multiple instances (its section count does not match), the pass falls through to a broader sweep:

```c
if (verbose)
    fprintf(stderr, "dead ocg constant section %s has multiple instances\n", name);

// Iterate ALL sections and remove those whose parent matches
for (i = 0; i < section_count; i++) {
    if (get_section_parent(ctx, i) == dead_section_parent)
        remove_section(ctx, i);
}
```

### Shared and Local Memory Cleanup

When a dead function has the entry-point flag (`0x10` at section flags byte), the pass also removes the function's associated shared-memory and local-memory sections:

```c
if (is_entry_point_flag) {
    // Remove .nv.shared.<func_name>
    sprintf(buf, "%s%s", ".nv.shared.", function_name);
    secidx = section_lookup(ctx, buf);
    if (secidx)
        remove_section(ctx, secidx);

    // Remove .nv.local.<func_name>
    sprintf(buf, "%s%s", ".nv.local.", function_name);
    secidx = section_lookup(ctx, buf);
    if (secidx)
        remove_section(ctx, secidx);

    // Also remove the entry's constant bank section using
    // the constant-bank ELF writer prefix (offset 72 in writer vtable)
    sprintf(buf, "%s.%s", const_prefix(), function_name);
    secidx = section_lookup(ctx, buf);
    if (secidx)
        remove_section(ctx, secidx);
}
```

### Callgraph Entry Cleanup

After removing all associated sections, the callgraph entry itself is freed:

```c
list_free(callgraph_entry->callee_list);     // offset +16
list_free(callgraph_entry->caller_list);     // offset +8
list_free(callgraph_entry->attribute_list);  // offset +40
arena_free(callgraph_entry);
vector_set(ctx->callgraph, index, NULL);     // null out the slot
```

## Interaction with `--keep-system-libraries`

The `--keep-system-libraries` flag (`byte_2A5F2C2`) interacts with DCE through the `cudadevrt` handling path. Normally, when full LTO is active and all translation units were compiled to IR, `main()` detects that `cudadevrt` is unnecessary and removes it from the input list:

```
LTO on everything so remove libcudadevrt from list
```

When `--keep-system-libraries` is set, this removal is suppressed -- cudadevrt remains in the link, and its functions participate in the normal DCE process. This is relevant because cudadevrt contains functions like `cudaDeviceSynchronize` that may be called from device code and must be preserved.

Additionally, in `sub_426AE0`'s host-info completeness check, cudadevrt objects are always skipped (identified by `strstr(name, "cudadevrt")`). Whether an object is cudadevrt does not affect the completeness determination.

## Interaction with LTO

When `--lto` is active, the LTO pipeline performs its own whole-program dead code elimination at the IR level, which is generally more thorough than the linker-level pass. The guard condition `(!byte_2A5F288 || byte_2A5F285)` ensures that:

- **Full LTO** (`--lto` alone): DCE is skipped; the IR-level pass handles it.
- **Partial LTO** (`--lto --force-partial-lto`): Both passes run. The IR-level pass handles LTO modules, while the linker-level pass handles non-LTO modules.

When DCE is active alongside LTO, the LTO IR collection phase (`sub_426CD0`) also checks `byte_2A5F214` to decide whether to pass `-has-global-host-info` to the NVVM compiler, enabling host-info-aware optimization within the IR compilation step.

## Callgraph Diagnostic Output

nvlink provides two options for inspecting the callgraph that drives DCE decisions:

- `--dump-callgraph` (`byte_2A5F216`): emits the callgraph in Graphviz DOT format via `sub_44CCF0`
- `--dump-callgraph-no-demangle` (`byte_2A5F215`): same but without C++ name demangling

The DOT output is written to the file specified by `--dot-file` and contains edges of the form:

```dot
digraph callgraph {
    "caller_name" -> "callee_name";
    ...
}
```

This can be visualized with `dot -Tpng callgraph.dot -o callgraph.png` to inspect which functions are connected before DCE runs.

## Function Map

| Address | Name | Role |
|---|---|---|
| `0x426AE0` | `mark_used_symbols` | Gate: checks host info completeness, dispatches host-info categories, calls core DCE |
| `0x43E7A0` | `add_referenced_symbols` | Iterates a reference list, inserts symbol names into a liveness set |
| `0x43F020` | `add_ext_kernels` | Wrapper: calls `add_referenced_symbols` for external kernels (`ctx+520`) |
| `0x43F040` | `add_int_kernels` | Wrapper: calls `add_referenced_symbols` for internal kernels (`ctx+528`) |
| `0x43F100` | `add_ext_constants` | Wrapper: calls `add_referenced_symbols` for external constants (`ctx+536`) |
| `0x43F1C0` | `add_int_constants` | Wrapper: calls `add_referenced_symbols` for internal constants (`ctx+544`) |
| `0x43F280` | `add_ext_globals` | Wrapper: calls `add_referenced_symbols` for external globals (`ctx+552`) |
| `0x43F340` | `add_int_globals` | Wrapper: calls `add_referenced_symbols` for internal globals (`ctx+560`) |
| `0x43F360` | `kernels_used_filter` | Normalizes `--kernels-used` patterns, inserts into kernel sets |
| `0x43F950` | `variables_used_filter` | Normalizes `--variables-used` patterns, inserts into all variable sets |
| `0x44AD40` | `dead_code_eliminate` | Core pass: iterates callgraph, removes unreachable functions and sections |
| `0x44A520` | `is_entry_point` | Checks if a callgraph node is a `__global__` kernel entry |
| `0x440350` | `resolve_section_symbol` | Resolves a section's link to the global symbol table |
| `0x443500` | `is_cuda_syscall_target` | Checks if a function's callee is a CUDA syscall (never eliminated) |
| `0x444830` | `is_cuda_syscall_name` | String match against `__cuda_syscall_32f3056bbb` |
| `0x440230` | `is_uf_stub` | Checks for `__cuda_uf_stub_` prefix (unified function stubs, never eliminated) |
| `0x44A5D0` | `callgraph_detect_recursion` | DFS-based recursion detection on callgraph |
| `0x44C030` | `callgraph_traverse` | Property propagation through callgraph (register counts, stack sizes) |
| `0x44CCF0` | `callgraph_dump_dot` | Writes callgraph in Graphviz DOT format |

## Global Variables

| Address | Name | Type | Description |
|---|---|---|---|
| `byte_2A5F214` | `mark_used` | bool | Master DCE enable: set if any liveness source is active |
| `byte_2A5F213` | `use_host_info` | bool | Use host reference info for liveness |
| `byte_2A5F212` | `ignore_host_info` | bool | Force-disable host info (set by `--ignore-host-info` or `-r`) |
| `byte_2A5F211` | `mark_all_used` | bool | All symbols considered live (fallback when host info incomplete) |
| `byte_2A5F2C2` | `keep_system_libraries` | bool | Preserve cudadevrt even when unnecessary |
| `byte_2A5F216` | `dump_callgraph` | bool | Emit callgraph DOT file |
| `byte_2A5F215` | `dump_callgraph_no_demangle` | bool | DOT output without demangling |
| `qword_2A5F2B8` | `kernels_used_list` | list* | User-specified kernel name patterns |
| `qword_2A5F2B0` | `variables_used_list` | list* | User-specified variable name patterns |

## Verbose Output

With `-v`, the DCE pass produces detailed diagnostics. A typical verbose run:

```
add referenced external kernel: _Z10my_kernelPfi
add referenced internal constant: _ZN6detail9my_constE
use host info
dead function 7(unused_helper)
removed un-used section .text.unused_helper (7)
removed un-used section .nv.info.unused_helper (12)
removed un-used section .rela.text.unused_helper (13)
dead function 14(another_dead_func)
removed un-used section .text.another_dead_func (14)
removed un-used section .nv.shared.another_dead_func (21)
removed un-used section .nv.local.another_dead_func (22)
```

When a function has its address taken but no callers, the pass logs:
```
function 9(callback_func) has address taken but no call to it
```

When host info is incomplete across input objects:
```
incomplete so ignore host info
```
