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

```text
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
| `--ignore-host-info` | No | — |
| `--relocatable-link` / `-r` | No | — (implies ignore-host-info) |
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
```text
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

The `cudadevrt` library is exempt from this check because it never contains host reference info — it is a pure device-side runtime library.

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

After liveness seeding, `sub_426AE0` calls `sub_44AD40` — the core dead code elimination function at 22,503 bytes. This function iterates the merged callgraph and removes every function (and its associated sections) that is not reachable from any live root.

### Algorithm Overview

The pass operates in two phases within a single function. Phase 1 iterates every callgraph entry, applies a cascade of liveness predicates, and removes functions that are conclusively dead. Functions whose liveness cannot be determined (because their section-to-symbol resolution fails) are deferred to Phase 2, which re-examines them after Phase 1 has cleaned up the callgraph.

### Phase 1: Primary Sweep

Phase 1 iterates all entries in the callgraph vector (`ctx+408`) from index 1 through `count - 1` (index 0 is reserved). For each entry it applies seven liveness tests in order; the first matching test determines the disposition:

```c
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

The liveness tests form a priority cascade. Tests 1-3 check structural properties of the callgraph node itself (root status, incoming edges, address-taken). Test 4 is a resolution check that gates the remaining tests — if the section cannot be resolved to a symbol, the function is deferred rather than killed, because a later Phase 1 removal might make the symbol resolvable. Tests 5-7 check properties of the resolved symbol (syscall identity, pinned flag, UF-stub prefix).

The `entry.caller_list` field at offset `+40` is a singly-linked list of `(caller_section_id, call_site_offset)` pairs built during callgraph construction. A non-NULL value means at least one other function calls this one. The `entry.address_taken` flag at offset `+50` is set during callgraph construction when a function pointer load targeting this function is observed in any relocation.

### Phase 2: Deferred Re-Examination

After Phase 1 completes, `sub_464740` counts the deferred list. If it is empty, the pass returns immediately. Otherwise Phase 2 iterates the deferred entries and performs a more expensive liveness check:

```c
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
    list_free(entry.alt_call_list)                    // entry+8  (external/indirect callee edges)
    list_free(entry.caller_list)                      // entry+40
    arena_free(entry)
    callgraph[idx] = NULL

free(deferred_list)
```

Phase 2 differs from Phase 1 in two key respects:

1. **Exhaustive caller scan.** Phase 1 relies on `entry.caller_list` — the direct caller list attached to each node. Phase 2 additionally performs a full scan of every remaining callgraph entry's callee list to detect indirect references. This catches cases where a function was originally deferred (because its section symbol was unresolvable) but other nodes still reference it through their outgoing edges. The scan walks each callee linked list at `callgraph_entry+16`, comparing the `callee_section_id` field (int32 at node offset `+8`) against the target function's section ID.

2. **No section cascade.** Phase 2 performs a simplified removal that only marks the section dead and frees the callgraph entry's linked lists. It does not perform the full associated-section removal cascade (NVIDIA info, rela, note, OCG constants, shared/local memory) that Phase 1 does. This is because deferred functions are those whose section-symbol resolution failed — they lack the resolved section index needed to locate associated sections via `sub_442760`. The section's flags byte is still updated (`flags = (flags & 0xFC) | 0x01`) so downstream passes know to skip it.

### Why Two Phases?

The two-phase design handles a specific ordering problem in the callgraph. During Phase 1, entries are visited in vector order. A function B that is only called by function A might be visited before A. When B is visited, its section-symbol resolution may fail because A's section is still present (making B's link appear valid), or A may not yet have been removed. By deferring B and revisiting it after Phase 1 has removed A, Phase 2 can correctly determine that B has no remaining callers.

The deferred list is implemented as a singly-linked list of `(next_ptr, callgraph_index)` pairs, built via `sub_4644C0` (prepend) and counted via `sub_464740` (walk and count). After Phase 2 finishes, the deferred list is freed via `sub_464520`.

### Section Removal Cascade (Phase 1 Only)

When Phase 1 determines a function is dead, it removes not just the function's code section but all associated sections in a six-stage cascade. Phase 2 does not perform this cascade (see above). The full removal sequence:

```c
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
    list_free(entry.alt_call_list)                   // entry+8  (external/indirect callee edges)
    list_free(entry.caller_list)                     // entry+40
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

## Worked Example: Callgraph Sweep DCE

This section walks through an end-to-end DCE run on a miniature merged object. The example is constructed to exercise every liveness test that matters in practice: an entry point, a directly-called helper, a transitively-called helper, and a fully unreferenced function. All addresses and field offsets match the decompiled `sub_44AD40` exactly; hash values are shown in the same form that the string-interner (`sub_449A80`) stores them.

### Setup

Four device functions survive the merge phase and land in the callgraph vector at `ctx+408`:

| Name | Kind | Section name | Liveness story |
|---|---|---|---|
| `main_kernel` | `__global__` | `.text.main_kernel` | Entry point (reached via `<<<>>>`) |
| `helper_a` | `__device__` | `.text.helper_a` | Called by `main_kernel` |
| `helper_b` | `__device__` | `.text.helper_b` | Called by `helper_a` (transitive) |
| `dead_fn` | `__device__` | `.text.dead_fn` | Never referenced anywhere |

After the merge phase, the section table (stored in the section array at `ctx+360`, accessed via `sub_442270`) contains the following entries. Column meanings: **Idx** is the section index (slot into the `ctx+360` vector); **sh_type** is the raw ELF type; **Flags** is the `section+5` byte (internal); **Sym** is the section-symbol index (`section+24`, later remapped via `ctx+456`); **Info** is the `sh_info` linkage used by `sub_442760`:

```text
   Idx  Section name                sh_type      Flags  Sym  Info  Notes
   ───  ──────────────────────────  ───────────  ─────  ───  ────  ─────────────────────────
    1   .text.main_kernel           PROGBITS     0x10    1    0    SHF_EXECINSTR, entry
    2   .nv.info.main_kernel        0x70000000   0x00   12    1    EIATTR stream
    3   .rela.text.main_kernel      RELA  (=9)   0x00   13    1    Relocations for (1)
    4   .nv.note.main_kernel        NOTE  (=4)   0x00   14    1    Per-kernel note
    5   .text.helper_a              PROGBITS     0x00    2    0    Ordinary device func
    6   .rela.text.helper_a         RELA  (=9)   0x00   15    5    Relocations for (5)
    7   .text.helper_b              PROGBITS     0x00    3    0    Ordinary device func
    8   .text.dead_fn               PROGBITS     0x00    4    0    The sacrifice
    9   .rela.text.dead_fn          RELA  (=9)   0x00   16    8    Relocations for (8)
   10   .nv.info.dead_fn            0x70000000   0x00   17    8    EIATTR stream
   11   .nv.constant.dead_fn        PROGBITS     0x00   18    8    OCG constant pool
```

The flag byte on `.text.main_kernel` has bit `0x10` set by the front end to mark the section as a kernel entry — `sub_44A520` reads this byte and returns true for section type `STT_SECTION (=3, low nibble of byte+4)` with `flags & 0x10` set.

The callgraph vector at `ctx+408` holds one node per function. Each node is 64 bytes; the fields read by `sub_44AD40` are:

```text
   offset   field                 type   notes
   ──────   ───────────────────   ─────  ─────────────────────────────────────
      +0    section_id            i32    negative => from ctx+352, positive => ctx+344
      +4    name_token            i32    interned string-table offset of the symbol's name; set by sub_44BA60 for -2 (address-taken-by-name) records, which also stamps +50 address_taken=1. NOT an index into the callgraph; sub_44C030 matches this against -3 alt_call_list name offsets to resolve by-name references back to this node.
      +8    alt_call_list head    ptr    external/indirect callee edges built by sub_44BAA0
     +16    callee_list head      ptr    outgoing direct calls built by sub_44B9F0; each node holds callee section_id at node+8
     +32    fn_ptr_edge_list      ptr    fn-pointer/address-taken edges built by sub_44BF90
     +40    caller_list head      ptr    incoming edges; non-NULL ⇒ "has callers" (Test 2);
                                        returned by sub_44C740 (callgraph_get_callers)
     +50    address_taken         u8     nonzero if a fn-pointer reloc observed
```

### Alt-Call Resolution Pass (`sub_44C030` Phase 1)

Before liveness propagation runs, `sub_44C030` performs an O(N x M x K) resolution pass that ties `-3` alt_call records to their concrete address-taken targets. The structure is three nested loops over the callgraph at `ctx+408`:

```c
for src in callgraph[1 .. node_count]:                       # outer: each potential caller
    head = *(src + 8)                                         # alt_call_list head built by sub_44BAA0
    for entry in linked-list(head, next=entry[0]):            # walk -3 entries hung off src
        name_off = *(u32*)(entry + 8)                         # name-string offset stored by -3 builder
        for tgt in callgraph[1 .. node_count]:               # inner: every node
            if *(u8*)(tgt + 50) == 1 and *(u32*)(tgt + 4) == name_off:
                sub_4644C0(tgt.name_id, src + 4)              # record resolved edge at src
```

The match key is the interned string-table offset, not a section or symbol index, which is why `sub_44BA60` (the `-2` builder) deliberately stores the name offset at `entry+4` and stamps `address_taken=1` at `entry+50` — those two fields are exactly what this loop consumes. A node with `+50 == 0` is invisible to the matcher even if its `+4` happens to coincide, so only symbols the front end marked as having their address taken can ever resolve an alt call. Before entering the loop, `sub_44C030` seeds `entry+24 := entry+16` on every node, snapshotting the head of the direct-call list so the subsequent reachability walk can extend the resolved edges without disturbing the original `callee_list`.

The remainder of `sub_44C030` is a DFS over the augmented graph: bytes `+48` (visited) and `+49` (on-stack) flag each node; an `on-stack` hit prints `recursion at function %d` when verbose, and any reachable node has its name index registered in the entry's reachable-set via `sub_4644C0`. A final block walks `elfw+392` looking for entries whose interned name begins with `$` (compiler-emitted markers) and feeds them through a temporary hashset (`sub_465020`/`sub_465720`/`sub_4655C0`) to validate that every `$`-prefixed referent is reachable from the discovered root kernel; unreachable references raise diagnostic 0x24 via `sub_450970`.

For our four functions the post-merge callgraph looks like this (callee edges on the left, caller edges in brackets):

```text
   cg[1]  main_kernel  (sym 1)    callees -> helper_a            [callers: —       ]  addr_taken=0
   cg[2]  helper_a     (sym 2)    callees -> helper_b            [callers: main_kernel ]  addr_taken=0
   cg[3]  helper_b     (sym 3)    callees -> —                   [callers: helper_a    ]  addr_taken=0
   cg[4]  dead_fn      (sym 4)    callees -> —                   [callers: —       ]  addr_taken=0
```

Four string-interner hashes are live at this point. These come from `sub_449A80` which looks up an `int*` keyed on the name. For this example the hashes resolve to the displayed section-symbol IDs:

```text
   sub_449A80(ctx+288, ".text.main_kernel")     -> int*  =  1
   sub_449A80(ctx+288, ".text.helper_a")        -> int*  =  2
   sub_449A80(ctx+288, ".text.helper_b")        -> int*  =  3
   sub_449A80(ctx+288, ".text.dead_fn")         -> int*  =  4
```

The liveness seeder (`sub_426AE0`) has already processed host info and inserted `"main_kernel"` into the external-kernel set at `ctx+520`. No `--kernels-used` / `--variables-used` was passed, so `ctx+568` (the forced-root slot) is zero.

### Phase 1 Iteration

`sub_44AD40` begins by calling `sub_464BB0(*(ctx+408))` to get the callgraph node count (5: slot 0 reserved plus four functions), then iterates `v4 = 1 .. 4`. For each iteration we show the values of the key locals as they are computed:

**Iteration 1 — `cg[1] = main_kernel`**

```text
v7      = sub_464DB0(ctx+408, 1)            = cg[1] node ptr
v12     = sub_440590(ctx, *v7 = 1)          = section record for idx 1 (.text.main_kernel)
v13     = *(ctx+568)                        = 0                (no forced root)
v14     = *v7                               = 1                (section id)
v6      = sub_44A520(ctx, 1)                = TRUE             (flags 0x10 ⇒ entry)
```

`sub_44A520` reads `section+5 = 0x10`, takes the `(v9 & 0x10) != 0` branch, and — because `sub_43FB20` returns false — checks `*(ctx[51] + 50)` which is the address-taken byte. Even if zero, the outer test already established entry status via the subsequent path; the function returns 1. The `if (!v6 && ...)` guard in `sub_44AD40` short-circuits and the loop moves on. `cg[1]` is untouched.

**Iteration 2 — `cg[2] = helper_a`**

```text
v7      = cg[2] node ptr
v12     = section record for idx 5 (.text.helper_a)
v6      = sub_44A520(ctx, 2)                = FALSE            (flags 0x00)
*((_QWORD*)v7 + 5)                          = caller_list head (non-NULL; main_kernel)
```

The condition `!v6 && !*((_QWORD*)v7+5)` is false because the caller list is non-NULL, so Test 2 ("has callers") keeps `helper_a` alive. No further tests run. The loop moves on.

**Iteration 3 — `cg[3] = helper_b`**

Same shape as iteration 2: `sub_44A520` returns false, but the caller list (holding the `helper_a -> helper_b` edge) is non-NULL. Test 2 passes. `helper_b` is kept.

**Iteration 4 — `cg[4] = dead_fn`**

```text
v7      = cg[4] node ptr
v12     = section record for idx 8 (.text.dead_fn)
v6      = sub_44A520(ctx, 4)                = FALSE
*((_QWORD*)v7 + 5)                          = NULL             (no callers)
*((_BYTE*)v7 + 50)                          = 0                (not address-taken)
```

Control falls into the big `else if ((unsigned int)sub_440350(ctx, v12, ...))` block. `sub_440350` reads `*(u16*)(v12+6) == 0xFFFF`, takes the post-merge remap path, and returns a nonzero symbol index — so we do **not** defer. `sub_443500` returns false (not a CUDA syscall). The flag byte is `0x00`, so `v28 & 4 == 0`, and `sub_440230(0)` returns false (the section has no UF-stub prefix).

All seven tests have now failed to keep `dead_fn` alive. The removal cascade begins.

### Removal Cascade for `dead_fn`

With `-v` active (`ctx+64` low bit), the first diagnostic is printed:

```text
dead function 4(dead_fn)
```

**Stage 1.** `*(section+5) = (flags & 0xFC) | 0x01` sets the callgraph-side death bit. For idx 8 this flips from `0x00` to `0x01`.

**Stage 2.** The code section itself is neutralized:

```text
v34 = sub_440350(ctx, v12, ...)             = 4      (resolved sym idx)
v162 = sub_440590(ctx, 4)                   = section record for idx 8
v40 = sub_440350(ctx, v162, ...)            = 4      (self-resolution for sanity)
v46 = sub_442270(ctx, 4, ...)               = sym record for sec sym 4
*(v46 + 32) = 0                              ; data_ptr = NULL
*(v46 + 48) = 1                              ; size = sentinel 1
walk *(v46+72) reloc list, sub_431000 each, sub_464520 head
*(v46 + 72) = 0; *(v46 + 80) = 0
```

Verbose output:
```text
removed un-used section .text.dead_fn (4)
```

**Stage 3.** `sub_442760(ctx, 4, 0x70000000)` walks the sh_info chain looking for the NVIDIA info section whose `+44` field equals 4. It finds idx 10 (`.nv.info.dead_fn`), and the same zero+sentinel pattern applies:
```text
removed un-used section .nv.info.dead_fn (17)
```

**Stage 4.** `sub_442760(ctx, 4, 9)` finds idx 9 (`.rela.text.dead_fn`):
```text
removed un-used section .rela.text.dead_fn (16)
```

**Stage 5.** `sub_442760(ctx, 4, 4)` (SHT_NOTE) returns 0 — `dead_fn` had no per-function note, so nothing happens here.

**Stage 6.** The OCG constant section is located by name rather than sh_info. The writer vtable at `*(ctx+488) + 136` yields the OCG prefix (`".nv.constant"` in this build); `sprintf(s, "%s.%s", prefix, "dead_fn")` gives `".nv.constant.dead_fn"`. `sub_4411D0(ctx, s)` returns 11. `sub_442270(ctx, 11)` pulls the record; its `+44 (sh_info)` is 8, matching our code section idx, so the single-instance path runs and prints:
```text
removed un-used section .nv.constant.dead_fn (18)
```

**Stage 7.** `dead_fn` does not have `flags & 0x10` (kernel-entry) set, so the `.nv.shared.dead_fn` / `.nv.local.dead_fn` cleanup is **skipped**. This is only reached for kernel entries — the per-kernel constant bank, dynamic shared memory, and local stack sections are owned by the entry point, not by ordinary device functions.

**Cleanup.** The three lists attached to the callgraph node are freed, the node itself is freed, and `sub_464D10(*(ctx+408), 4, 0)` nulls slot 4 of the callgraph vector.

### State After Phase 1

No entries were deferred (`v165` is still NULL — `sub_464740(NULL)` returns 0), so Phase 2 is skipped entirely and `sub_44AD40` returns.

The callgraph vector now holds:

```text
   cg[0]  (reserved)
   cg[1]  main_kernel     (untouched)
   cg[2]  helper_a        (untouched)
   cg[3]  helper_b        (untouched)
   cg[4]  NULL            (slot cleared by sub_464D10)
```

The in-memory section table has four sections marked dead (flag byte bit 0 set, data_ptr=NULL, size=1). The section records themselves are still in the `ctx+360` section array — DCE does not compact the vector in place, it only marks. Compaction happens later, when the layout phase rebuilds the final section table and populates the remap arrays at `ctx+456` and `ctx+464`.

### Remap Table Construction (`elfw+456` / `elfw+464`)

After DCE, the layout phase walks the section array at `ctx+360` in order and assigns each surviving section a new dense index. For each old-index *i*, it writes `(ctx+456)[i] = new_i` where `new_i` is the compacted index, or 0 for sections that were killed. The parallel array at `ctx+464` handles negative symbol indices (which refer to the negative symbol array at `ctx+352`, used for symbols created late in the pipeline such as synthesized stubs).

Before Phase 1 runs, both remap pointers are NULL — `sub_4411F0` and `sub_440350` check `a1[57]` / `*(a1+456)` and return the input unchanged when the table does not exist yet. After DCE, the layout phase allocates the arrays and fills them based on the surviving sections.

For our example the forward remap table `*(ctx+456)` ends up as:

```text
   old idx   new idx   comment
   ───────   ───────   ────────────────────────────────────
      0         0      reserved slot
      1         1      .text.main_kernel         (kept)
      2         2      .nv.info.main_kernel      (kept)
      3         3      .rela.text.main_kernel    (kept)
      4         4      .nv.note.main_kernel      (kept)
      5         5      .text.helper_a            (kept)
      6         6      .rela.text.helper_a       (kept)
      7         7      .text.helper_b            (kept)
      8         0      .text.dead_fn             ← killed, remap=0
      9         0      .rela.text.dead_fn        ← killed, remap=0
     10         0      .nv.info.dead_fn          ← killed, remap=0
     11         0      .nv.constant.dead_fn      ← killed, remap=0
```

After DCE the forward remap table contains no holes (because `dead_fn` sat at the end of the vector), but in general a kill anywhere in the middle causes all higher slots to shift down. Any later call to `sub_4411F0` or `sub_440350` that receives an old-index 8, 9, 10, or 11 will first read `(ctx+456)[old]`, see zero, and trigger the `"reference to deleted symbol"` error via `sub_467460` — this is how the linker catches stale references that survived DCE (e.g., a relocation in some other surviving section that still points at a dead function).

The negative-side remap `*(ctx+464)` is empty for this example because no synthesized negative-index sections were created; it would be populated identically if the merge phase had produced any (e.g., synthesized stubs for `__cuda_syscall_*` targets).

The `ctx+472` "secidx virtual" guard array — consulted whenever `*(ctx+82)` is set — stores a reverse lookup so that `sub_442270` can assert `*(ctx+472)[a2] -> ctx+368[] -> a2` is consistent. After DCE, the four killed slots have their virtual-mapping entries zeroed, so any accidental access through `sub_442270` will fail the `"secidx not virtual"` assertion instead of silently returning stale data.

### Final Merged ELF

When the layout phase completes, the output ELF contains exactly seven sections derived from the four original kept sections, in the dense post-remap order:

```text
   [ 1] .text.main_kernel        PROGBITS    AX      sym 1
   [ 2] .nv.info.main_kernel     0x70000000  —       sym 2
   [ 3] .rela.text.main_kernel   RELA        I       sym 3 → (1)
   [ 4] .nv.note.main_kernel     NOTE        —       sym 4
   [ 5] .text.helper_a           PROGBITS    AX      sym 5
   [ 6] .rela.text.helper_a      RELA        I       sym 6 → (5)
   [ 7] .text.helper_b           PROGBITS    AX      sym 7
```

The symbol table has had `dead_fn` and its section symbols stripped; because the forward remap maps slots 8-11 to 0, any stale reloc addend or debug entry referencing those slots now resolves to the STN_UNDEF symbol and triggers the `"reference to deleted symbol"` diagnostic path rather than producing a dangling pointer.

Full verbose transcript for this example:

```text
add referenced external kernel: main_kernel
use host info
dead function 4(dead_fn)
removed un-used section .text.dead_fn (4)
removed un-used section .nv.info.dead_fn (17)
removed un-used section .rela.text.dead_fn (16)
removed un-used section .nv.constant.dead_fn (18)
```

No `dead function` line is emitted for `main_kernel`, `helper_a`, or `helper_b` because each survives at a different liveness test: `main_kernel` at Test 1 (entry point), `helper_a` at Test 2 (has caller `main_kernel`), `helper_b` at Test 2 (has caller `helper_a`). Note in particular that DCE does **not** perform a recursive traversal from roots — it relies on the caller list at `node+40` already being populated by the merge phase so that every transitively-live function has a non-NULL caller list by the time Phase 1 visits it. A bug in the caller-list construction would manifest here as a transitively-reachable function getting killed because its caller list was never linked.

## Interaction with `--keep-system-libraries`

The `--keep-system-libraries` flag (`byte_2A5F2C2`) interacts with DCE through the `cudadevrt` handling path. Normally, when full LTO is active and all translation units were compiled to IR, `main()` detects that `cudadevrt` is unnecessary and removes it from the input list:

```text
LTO on everything so remove libcudadevrt from list
```

When `--keep-system-libraries` is set, this removal is suppressed — cudadevrt remains in the link, and its functions participate in the normal DCE process. This is relevant because cudadevrt contains functions like `cudaDeviceSynchronize` that may be called from device code and must be preserved.

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

```text
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
```text
function 9(callback_func) has address taken but no call to it
```

When host info is incomplete across input objects:
```text
incomplete so ignore host info
```

## Cross-References

- [Symbol Resolution](symbol-resolution.md) — symbol lookup used by `resolve_section_symbol` and liveness seeding
- [Weak Symbols](weak-symbols.md) — weak resolution runs before DCE during the merge phase
- [Section Merging](section-merging.md) — merge phase that builds the callgraph consumed by DCE
- [Data Layout Optimization](data-layout-opt.md) — constant dedup that uses DCE liveness information (reachability check via `sub_43FB70`)
- [LTO Overview](../lto/overview.md) — LTO pipeline performs its own IR-level DCE, suppressing the linker-level pass
- [Whole vs Partial LTO](../lto/whole-vs-partial.md) — `--force-partial-lto` re-enables linker DCE alongside LTO
- [Merge Phase](../pipeline/merge.md) — pipeline phase that precedes DCE
- [Layout Phase](../pipeline/layout.md) — pipeline phase that follows DCE

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| `sub_44AD40` at 0x44AD40 is the core DCE function | HIGH | Decompiled `sub_44AD40_0x44ad40.c` exists; contains `"dead function %d(%s)"` at line 209 |
| `"dead function %d(%s)\n"` diagnostic string | HIGH | Decompiled line 209: exact format string confirmed |
| `"removed un-used section %s (%d)\n"` diagnostic string | HIGH | Decompiled lines 244, 277, 310, 343, 386, 389, 441, 444: string appears 8+ times |
| `"function %d(%s) has address taken but no call to it"` diagnostic | HIGH | Decompiled line 195: exact format string confirmed |
| `"incomplete so ignore host info"` string | HIGH | String confirmed in `nvlink_strings.json` |
| `"__cuda_uf_stub_"` prefix check for UF stubs (never eliminated) | HIGH | String confirmed in `nvlink_strings.json`; used in `sub_440230` |
| Phase 1 iterates callgraph vector at ctx+408 from index 1 | HIGH | Decompiled `sub_44AD40` loop structure starts from index 1 through count-1 |
| Seven liveness tests in priority cascade (forced root, callers, address-taken, resolution, syscall, pinned, UF-stub) | HIGH | All test branches visible in decompiled code; address-taken at entry+50, caller_list at entry+40 confirmed |
| Phase 2 deferred re-examination with exhaustive caller scan | HIGH | Two-phase structure visible in decompiled code; deferred list via `sub_4644C0` |
| Section removal cascade: code, .nv.info, .rela, .nv.note, OCG constants, shared/local | HIGH | Multiple `"removed un-used section"` emissions in decompiled code confirm multi-section removal |
| `sub_426AE0` gate function with `mark_used` flag at `byte_2A5F214` | HIGH | Function exists in decompiled output; global variable addresses consistent with option parser |
| Six host-info category dispatchers at `sub_43F020` through `sub_43F340` | MEDIUM | Functions inferred from `sub_426AE0` dispatch; individual decompiled files not all verified |
| Kernel name wildcard normalization `*name*` in `sub_43F360` | MEDIUM | Reconstructed from decompiled analysis; wrapping logic consistent with `arena_sprintf` patterns |
| `--dump-callgraph` DOT output via `sub_44CCF0` | MEDIUM | Function address referenced; DOT format inferred from string evidence |
| Guard condition `(!lto \|\| force_partial_lto)` for DCE activation | MEDIUM | Reconstructed from main() decompiled analysis; flag addresses consistent |
