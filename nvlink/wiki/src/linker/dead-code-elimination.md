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

The pass operates in two phases within a single function. Phase 1 iterates every callgraph entry, applies a cascade of liveness predicates, and removes functions that are conclusively dead. Functions whose liveness cannot be determined (because their section-to-symbol resolution fails) are deferred to Phase 2, which re-examines them after Phase 1 has cleaned up the callgraph.

### Phase 1: Primary Sweep

Phase 1 iterates all entries in the callgraph vector (`ctx+408`) from index 1 through `count - 1` (index 0 is reserved). For each entry it applies seven liveness tests in order; the first matching test determines the disposition:

```
deferred_list = empty

for i = 1 to callgraph_count - 1:
    entry = callgraph[i]                         // sub_464DB0(ctx+408, i)
    func_id = entry.section_id                   // entry+0 (int32)
    section = get_section_record(ctx, func_id)   // sub_440590

    # ── Test 1: forced root via ctx+568 ──────────────────────────
    #   When ctx+568 is nonzero, it holds the section ID of a single
    #   designated root (set by certain LTO paths). Only that function
    #   is considered an entry point.
    if ctx.forced_root != 0:
        is_entry = (func_id == ctx.forced_root)
    else:
        is_entry = is_entry_point(ctx, func_id)  // sub_44A520

    if is_entry:
        continue                                 // always live

    # ── Test 2: has callers ──────────────────────────────────────
    if entry.caller_list != NULL:                 // entry+40
        continue                                 // called by someone → live

    # ── Test 3: address taken ────────────────────────────────────
    if entry.address_taken:                       // byte at entry+50
        if verbose:
            print("function %d(%s) has address taken but no call to it",
                  func_id, section.name)
        if extra_warnings:                       // byte at ctx+93
            emit_warning(func_id, section.name)  // sub_467460
        continue                                 // conservatively keep

    # ── Test 4: symbol resolution ────────────────────────────────
    #   sub_440350 resolves the section's sh_link to a symbol table
    #   entry. If the section has no valid symbol link (returns 0),
    #   the function's liveness is ambiguous — defer to Phase 2.
    if !resolve_section_symbol(ctx, section):     // sub_440350
        list_prepend(i, &deferred_list)           // sub_4644C0
        continue

    # ── Test 5: CUDA syscall stub ────────────────────────────────
    if is_cuda_syscall_target(ctx, func_id):      // sub_443500
        continue                                  // never eliminate

    # ── Test 6: pinned section (flag 0x04) ───────────────────────
    flags = section.flags                         // byte at section+5
    if flags & 0x04:
        continue                                  // explicitly kept

    # ── Test 7: unified-function stub ────────────────────────────
    if section.name && is_uf_stub(section.name):  // sub_440230 → "__cuda_uf_stub_"
        continue                                  // never eliminate

    # ── Dead: remove function and all associated sections ────────
    remove_dead_function(ctx, entry, i, section)
```

The liveness tests form a priority cascade. Tests 1-3 check structural properties of the callgraph node itself (root status, incoming edges, address-taken). Test 4 is a resolution check that gates the remaining tests -- if the section cannot be resolved to a symbol, the function is deferred rather than killed, because a later Phase 1 removal might make the symbol resolvable. Tests 5-7 check properties of the resolved symbol (syscall identity, pinned flag, UF-stub prefix).

The `entry.caller_list` field at offset `+40` is a singly-linked list of `(caller_section_id, call_site_offset)` pairs built during callgraph construction. A non-NULL value means at least one other function calls this one. The `entry.address_taken` flag at offset `+50` is set during callgraph construction when a function pointer load targeting this function is observed in any relocation.

### Phase 2: Deferred Re-Examination

After Phase 1 completes, `sub_464740` counts the deferred list. If it is empty, the pass returns immediately. Otherwise Phase 2 iterates the deferred entries and performs a more expensive liveness check:

```
if list_length(deferred_list) == 0:              // sub_464740
    return

for each deferred_entry in deferred_list:
    idx = deferred_entry.callgraph_index
    entry = callgraph[idx]                       // sub_464DB0
    if entry == NULL:
        continue                                 // already removed

    func_id = entry.section_id
    section = get_section_record(ctx, func_id)

    # ── Re-apply entry point and caller tests ────────────────────
    if ctx.forced_root != 0:
        is_entry = (func_id == ctx.forced_root)
    else:
        is_entry = is_entry_point(ctx, func_id)

    if is_entry:
        continue                                 // live

    if entry.caller_list != NULL:                // entry+40
        continue                                 // still has callers

    # ── Re-try symbol resolution ─────────────────────────────────
    if resolve_section_symbol(ctx, section):
        continue                                 // now resolvable → keep

    # ── Exhaustive callgraph scan for remaining callers ──────────
    #   Phase 1 may have removed the only caller, but the callee list
    #   in *other* nodes may still reference this function. Scan the
    #   entire callgraph to see if anyone's callee list contains
    #   this function's ID.
    found_caller = false
    target_id = func_id

    for j = 1 to callgraph_count - 1:
        other = callgraph[j]                     // sub_464DB0
        if other == NULL:
            continue
        callee_node = other.callee_list          // offset +16
        if callee_node == NULL:
            continue

        # Walk the callee linked list looking for target_id
        while callee_node != NULL:
            if callee_node.callee_section_id == target_id:   // int32 at +8
                found_caller = true
                break
            callee_node = callee_node.next       // pointer at +0
        if found_caller:
            break

    if found_caller:
        continue                                 // someone still calls us

    # ── Check CUDA syscall name ──────────────────────────────────
    sym_name = section.name                      // offset +32 in section
    if is_cuda_syscall_name(ctx, sym_name):       // sub_444830
        continue

    # ── Dead: simplified removal (no section cascade) ────────────
    if verbose:
        print("dead function %d(%s)\n", func_id, section.name)

    section.flags = (section.flags & 0xFC) | 0x01    // mark dead
    list_free(entry.callee_list)                      // entry+16
    list_free(entry.caller_list)                      // entry+8
    list_free(entry.attribute_list)                    // entry+40
    arena_free(entry)
    callgraph[idx] = NULL

free(deferred_list)
```

Phase 2 differs from Phase 1 in two key respects:

1. **Exhaustive caller scan.** Phase 1 relies on `entry.caller_list` -- the direct caller list attached to each node. Phase 2 additionally performs a full scan of every remaining callgraph entry's callee list to detect indirect references. This catches cases where a function was originally deferred (because its section symbol was unresolvable) but other nodes still reference it through their outgoing edges. The scan walks each callee linked list at `callgraph_entry+16`, comparing the `callee_section_id` field (int32 at node offset `+8`) against the target function's section ID.

2. **No section cascade.** Phase 2 performs a simplified removal that only marks the section dead and frees the callgraph entry's linked lists. It does not perform the full associated-section removal cascade (NVIDIA info, rela, note, OCG constants, shared/local memory) that Phase 1 does. This is because deferred functions are those whose section-symbol resolution failed -- they lack the resolved section index needed to locate associated sections via `sub_442760`. The section's flags byte is still updated (`flags = (flags & 0xFC) | 0x01`) so downstream passes know to skip it.

### Why Two Phases?

The two-phase design handles a specific ordering problem in the callgraph. During Phase 1, entries are visited in vector order. A function B that is only called by function A might be visited before A. When B is visited, its section-symbol resolution may fail because A's section is still present (making B's link appear valid), or A may not yet have been removed. By deferring B and revisiting it after Phase 1 has removed A, Phase 2 can correctly determine that B has no remaining callers.

The deferred list is implemented as a singly-linked list of `(next_ptr, callgraph_index)` pairs, built via `sub_4644C0` (prepend) and counted via `sub_464740` (walk and count). After Phase 2 finishes, the deferred list is freed via `sub_464520`.

### Section Removal Cascade (Phase 1 Only)

When Phase 1 determines a function is dead, it removes not just the function's code section but all associated sections in a six-stage cascade. Phase 2 does not perform this cascade (see above). The full removal sequence:

```
function remove_dead_function(ctx, entry, cg_index, section):
    func_id   = entry.section_id
    func_name = section.name                         // offset +32

    if verbose:
        print("dead function %d(%s)\n", func_id, func_name)

    # ── Stage 1: mark callgraph section dead ─────────────────────
    section.flags = (section.flags & 0xFC) | 0x01

    # ── Stage 2: remove the code section (.text.<func>) ──────────
    code_secidx = get_section_index(ctx, func_id)    // sub_4411F0
    code_symidx = resolve_section_symbol(ctx, section)
    is_entry_flag = section.flags & 0x10             // kernel entry?
    code_section = get_section_record(ctx, code_secidx)

    symidx = resolve_section_symbol(ctx, code_section)
    sym_record = get_sym_record(ctx, symidx)         // sub_442270
    sym_record.data_ptr = NULL                       // offset +32 → 0
    sym_record.size = 1                              // offset +48 → 1 (sentinel)

    # free all relocation entries in the code section
    for relo in sym_record.relo_list:                // linked list at +72
        arena_free(relo.data)                        // sub_431000
    list_free(sym_record.relo_list)                  // sub_464520
    sym_record.relo_list = NULL
    sym_record.relo_tail = NULL

    if verbose:
        print("removed un-used section %s (%d)\n",
              sym_record.name, sym_record.index)

    code_section.flags = (code_section.flags & 0xFC) | 0x01

    # ── Stage 3: remove NVIDIA info section (type 0x70000000) ────
    nvidia_secidx = find_related_section(ctx, code_secidx, 0x70000000)
    if nvidia_secidx:
        remove_section(ctx, nvidia_secidx)           // same zero+sentinel pattern

    # ── Stage 4: remove relocation section (SHT_RELA = 9) ───────
    rela_secidx = find_related_section(ctx, code_secidx, SHT_RELA)
    if rela_secidx:
        remove_section(ctx, rela_secidx)

    # ── Stage 5: remove note section (SHT_NOTE = 4) ─────────────
    note_secidx = find_related_section(ctx, code_secidx, SHT_NOTE)
    if note_secidx:
        remove_section(ctx, note_secidx)

    # ── Stage 6: remove OCG constant section ─────────────────────
    ocg_prefix = elf_writer_vtable.get_ocg_prefix()  // vtable+136
    ocg_name = sprintf("%s.%s", ocg_prefix, func_name)
    ocg_secidx = section_lookup(ctx, ocg_name)       // sub_4411D0

    if ocg_secidx:
        ocg_record = get_sym_record(ctx, ocg_secidx)
        if ocg_record && ocg_record.parent_idx == code_secidx:
            # single instance — remove directly
            remove_section(ctx, ocg_secidx)
        else:
            # multiple instances — scan all sections for matching parent
            if verbose:
                print("dead ocg constant section %s has multiple instances\n",
                      func_name)
            total = section_count(ctx)               // sub_464BB0(ctx+360)
            for k = 0 to total - 1:
                rec = get_section_at(ctx+360, k)     // sub_464DB0
                if rec.parent_idx == code_secidx:
                    remove_section(ctx, k)

    # ── Stage 7: remove shared/local memory (entry points only) ──
    if is_entry_flag:
        # constant bank section via writer vtable+72 prefix
        const_prefix = elf_writer_vtable.get_const_prefix()
        const_name = sprintf("%s.%s", const_prefix, func_name)
        const_secidx = section_lookup(ctx, const_name)
        if const_secidx:
            remove_section(ctx, const_secidx)

        # .nv.shared.<func_name>
        shared_secidx = section_lookup(ctx, ".nv.shared." + func_name)
        if shared_secidx:
            remove_section(ctx, shared_secidx)

        # .nv.local.<func_name>
        local_secidx = section_lookup(ctx, ".nv.local." + func_name)
        if local_secidx:
            remove_section(ctx, local_secidx)

    # ── Cleanup callgraph entry ──────────────────────────────────
    list_free(entry.callee_list)                     // entry+16
    list_free(entry.caller_list)                     // entry+8
    list_free(entry.attribute_list)                   // entry+40
    arena_free(entry)
    callgraph[cg_index] = NULL
```

The `find_related_section` call (`sub_442760`) searches for a section whose `sh_info` field (ELF section header info, stored at offset `+44` in the internal record) matches the code section index and whose `sh_type` matches the requested type. This is how nvlink locates the `.nv.info.<func>`, `.rela.text.<func>`, and `.nv.note.<func>` sections that the ELF format associates with each function.

The `remove_section` primitive performs the same pattern for every section it removes:
1. Set `data_ptr` (offset `+32`) to NULL
2. Set `size` (offset `+48`) to 1 (a sentinel value distinguishing "removed" from "empty")
3. Walk the relocation linked list at offset `+72`, freeing each entry via `sub_431000`
4. Free the list head via `sub_464520`, null both list pointers (`+72` and `+80`)
5. Print `"removed un-used section %s (%d)\n"` when verbose

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
| `0x4644C0` | `list_prepend` | Prepends a value to a singly-linked list (used for deferred list in Phase 2) |
| `0x464740` | `list_length` | Counts elements in a singly-linked list by walking it |
| `0x442760` | `find_related_section` | Finds section with matching `sh_info` and `sh_type` (locates `.nv.info`, `.rela`, `.nv.note`) |
| `0x4411D0` | `section_lookup_by_name` | Looks up a section index by name string |
| `0x4411F0` | `get_section_index` | Gets the section index for a given function ID |
| `0x442270` | `get_sym_record` | Gets the internal symbol/section record for a given index |
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
