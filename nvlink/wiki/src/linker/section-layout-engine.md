# Section Layout Engine

The section layout engine (`sub_4325A0`) is the central placement routine of nvlink's layout phase. After the merge phase has accumulated an unordered linked list of data contributions for every output section, this single 408-byte function is invoked from at least eleven distinct call sites to assign each contribution a final byte offset within its parent section. It is the only function in the binary that simultaneously (a) consults the architecture's GPU-memory-space policy, (b) sorts contributions by alignment to minimize padding, (c) walks each contribution computing an aligned cursor, and (d) writes the resulting offset into *both* the data node and the underlying symbol record. Every byte position of every kernel-visible variable — `.text` function bodies, `.nv.constant<N>` parameters, `.nv.shared.<kernel>` shared-memory globals, `.nv.local.<kernel>` stack frames, and `.nv.global` data — passes through this routine.

This page documents the engine in reimplementation-grade detail. For the surrounding pipeline context — how layout fits between merge and relocation, and which list of sections the layout phase walks — see [Layout Phase](../pipeline/layout.md). For the per-memory-space merge primitives that *feed* the engine, see [Section Merging](section-merging.md). For the 104-byte record the engine reads and updates, see [Section Record](../structs/section-record.md).

## Key Facts

| Property | Value | Confidence |
|---|---|---|
| Address | `0x4325A0` | HIGH |
| End | `0x432738` | HIGH |
| Size | 408 bytes (105 instructions, 19 basic blocks) | HIGH |
| ABI | `__fastcall (ctx: rdi, section: rsi, initial_offset: edx) -> int64` | HIGH |
| Distinct call sites | 11 across 3 callers (`sub_438C60`, `sub_438DD0`, `sub_439830`) | HIGH |
| Strings referenced | `"section not found"`, `"variable %s at offset %d\n"`, `"should only reach here with no opt"` | HIGH |
| Direct callees | `sub_440590` (get sym record), `sub_4647D0` (linked-list merge sort), `sub_432440` (alignment comparator), `sub_467460` (diagnostic), `fprintf` | HIGH |
| Comparator size | 36 bytes — a two-field lexicographic comparison on `(alignment desc, size asc)` | HIGH |

## Why a Dedicated Engine Exists

A conventional CPU linker can lay out a single section with a trivial loop: it walks the input objects in order, rounds the cursor up to each contribution's alignment, and emits bytes. A CUDA device linker cannot. Four properties of CUDA memory spaces force the design of a dedicated engine:

1. **Architecture-conditional sort policy.** Pre-Hopper architectures permit nvlink to freely re-order contributions within a `.nv.shared.*` section to minimize padding. Hopper and later (the extended-shared-memory ISA) require contributions to retain their input order because shared-memory layout is observable through specific instructions. The engine consults the architecture vtable on every call to decide whether to sort.

2. **Multiple section kinds, one cursor algorithm.** The same per-contribution alignment-and-place loop applies to `.text` function bodies, `.nv.global`, `.nv.global.init`, `.nv.shared.*`, `.nv.local.*`, `.nv.constant<N>`, and `.nv.constant<N>.<kernel>`. Cloning the loop across eleven call sites would multiply bugs; instead, every caller passes a section pointer and an initial offset to one routine.

3. **Per-contribution natural alignment fallback.** When a contribution records `alignment = 0`, the engine falls back to natural alignment derived from the contribution's *size*, capped at 8 bytes. This rule is identical for every section kind but cannot be expressed at the merge layer because the merge layer does not know the final cursor.

4. **Dual offset write-back.** Every contribution lives in two places: the section's data-node linked list (the offset the ELF writer needs) and the symbol record (the value relocations resolve against). The engine writes the same offset into both fields atomically, which is impossible at the merge layer because the merge layer accumulates an *unordered* list.

Confidence: HIGH (architecture-conditional sort verified at line 21 of decompiled `sub_4325A0`; dual write-back verified at lines 41-42 and 61-62 of the decompiled source).

## Signature

```c
// sub_4325A0 -- assign final offsets to every data contribution in a section
// a1 (rdi): elfw context pointer (linker output state)
// a2 (rsi): pointer to the 104-byte section_record
// a3 (edx): initial cursor offset (usually 0; non-zero for per-entry shared
//           sections that must be placed after the global shared region)
// Returns: final cursor value -- the last assigned offset, NOT the section size.
//          The total size is written separately to section+32.
uint32_t section_layout_engine(elfw_ctx *ctx,
                               section_record *section,
                               uint32_t initial_offset);
```

The return value is the offset *of the last contribution*, not the cursor past the last contribution. The high-water cursor is stored in `section+32` (`sh_size`) at the end of the loop. Callers that need the total size read it from the section record after the call.

Confidence: HIGH (return value `a3` is updated only when a contribution is placed, not when the loop exits; section size `*(_QWORD *)(a2 + 32) = v9` written at line 82).

## Top-Level Algorithm

```c
uint32_t section_layout_engine(elfw_ctx *ctx, section_record *section,
                               uint32_t initial_offset)
{
    // (1) Null guard. A NULL section pointer is a logic bug, not a
    //     recoverable condition.
    if (section == NULL)
        fatal_error("section not found");

    // (2) Architecture-conditional sort. If extended-smem mode is OFF, or
    //     if the architecture does NOT mark this section type as
    //     "preserve order", sort the data-node linked list by alignment
    //     (descending), tie-broken by size (ascending). Sorting uses
    //     sub_4647D0 (recursive bottom-up linked-list merge sort) with
    //     sub_432440 as the comparator.
    bool extended_smem_mode = ctx->extended_smem_mode;            // ctx+100
    bool arch_preserves_order =
        ctx->arch_vtable->preserves_layout_order(section->sh_type); // vtable+200
    if (!extended_smem_mode || !arch_preserves_order)
        list_merge_sort(&section->data_list_head, alignment_cmp);

    // (3) Promote section alignment to the maximum contribution alignment.
    //     The head node's alignment is the largest after sort.
    data_node *head = section->data_list_head;
    data_node *node = head->next;
    uint64_t  first_align = node->alignment;
    if (first_align > section->sh_addralign)
        section->sh_addralign = first_align;

    // (4) Walk the sorted list, placing each contribution.
    uint32_t cursor = initial_offset;
    while (node != NULL) {
        symbol_record *sym = get_sym_record(ctx, node->source_sym_index);
        uint64_t       align = node->alignment;     // node+16
        uint64_t       size  = node->size;          // node+24
        uint32_t       placed_offset;

        if (align > 0) {
            // (4a) Explicit alignment: round cursor up to alignment.
            if (cursor % align != 0)
                cursor += align - (cursor % align);
            placed_offset = cursor;
        }
        else if (size > 0) {
            // (4b) Natural alignment fallback: use min(size, 8).
            uint64_t natural = (size <= 8) ? size : 8;
            if (cursor % natural != 0)
                cursor += natural - (cursor % natural);
            placed_offset = cursor;
        }
        else {
            // (4c) Both alignment and size are zero. Only valid in no-opt
            //      mode (ctx+90). In optimized mode this is a logic error.
            if (!ctx->no_opt_mode)
                fatal_error("should only reach here with no opt");
            // Fall through with cursor unchanged; offset == previous offset.
            node = node->next;
            continue;
        }

        // (5) Dual write-back: store the placed offset in BOTH the symbol
        //     record (relocations resolve against it) and the data node
        //     (ELF writer reads it).
        sym->value      = placed_offset;     // symbol+8
        node->offset    = placed_offset;     // node+8

        // (6) Verbose trace.
        if (ctx->verbose_flags & 2)
            fprintf(stderr, "variable %s at offset %d\n",
                    sym->name, placed_offset);

        // (7) Advance cursor past this contribution.
        cursor = placed_offset + node->size;
        node   = node->next;
    }

    // (8) Write back final section size and return the last placed offset.
    section->sh_size = cursor;
    return /* a3 -- last placed offset */ placed_offset;
}
```

Confidence: HIGH. Every numbered step has a direct correspondence in the decompiled source:
- Step 1 ↔ line 26-27 (`if (!a2) sub_467460(..., "section not found")`)
- Step 2 ↔ line 28-29 (vtable dispatch + `sub_4647D0` call)
- Step 3 ↔ lines 30-34 (read head, promote `sh_addralign`)
- Step 4a ↔ lines 41-46 (explicit alignment, modulo round-up)
- Step 4b ↔ lines 62-67 (natural alignment capped at 8)
- Step 4c ↔ lines 74-75 (`"should only reach here with no opt"`)
- Step 5 ↔ lines 48-49 and 68-69 (dual write to `v13+8` and `v7+8`)
- Step 6 ↔ line 53 (`fprintf(stderr, "variable %s at offset %d\n", ...)`)
- Step 8 ↔ line 82 (`*(_QWORD *)(a2 + 32) = v9`)

## Per-Region Placement Logic

A single function lays out every region in the output cubin. The control flow is identical, but the *caller* selects the region by passing a different section pointer. The eleven call sites split across three callers, each responsible for one or more region families.

### `.text.*` — Function Bodies

`sub_438C60` invokes the engine for `.text.*` sections during the text-layout sub-phase. Function bodies have explicit alignment derived from the architecture's instruction width (typically 32 bytes for warp-aligned entry points, 4 or 8 bytes for ordinary functions). Cursor starts at 0; the engine sorts by alignment to bring entry points to the front, which packs nicely against the 32-byte boundary.

| Caller | Call site | Section family |
|---|---|---|
| `sub_438C60` | `0x438C8F`, `0x438CB4` | `.text.*` per-function code |

The natural-alignment fallback (step 4b) effectively never fires here because the ELF emitter records explicit `sh_addralign` on every input function section.

Confidence: HIGH for call sites and caller identity; MEDIUM for the claim that natural alignment never fires (no instrumented run, but every observed input cubin records explicit alignment).

### `.nv.global` and `.nv.global.init` — Global Data

The layout phase Phase 3 (in `sub_439830`) calls the engine once for `.nv.global` (uninitialized globals, `SHT_CUDA_GLOBAL`) and once for `.nv.global.init` (initialized globals, `SHT_CUDA_GLOBAL_INIT`). Both are placed at initial offset 0. Globals carry explicit per-symbol alignment recorded during merge, so step 4b is rarely taken.

| Caller | Call site | Section family |
|---|---|---|
| `sub_439830` | `0x43993F`, `0x439963` | `.nv.global`, `.nv.global.init` |

The promotion of `sh_addralign` (step 3) is significant for these regions because the final ELF program-header writer uses the section's alignment to align the segment in the output file.

Confidence: HIGH (Phase 3 call sites match the two adjacent `sub_4325A0` calls at offsets `0x439830 + 287` and `0x439830 + 323`).

### `.nv.shared` and `.nv.shared.<kernel>` — Shared Memory

Shared memory has the most intricate dispatch. The engine is invoked directly only in **no-opt mode** (ctx+90 set) for the global shared region. In optimized mode, the engine is *not* called for global shared — the shared-memory optimizer (`sub_436BD0`) handles graph-coloring placement instead. For per-entry shared memory sections, the engine *is* called, but only after the caller has computed an `initial_offset` that places the per-entry region after the global shared high-water mark.

| Caller | Call site | Section family |
|---|---|---|
| `sub_439830` | `0x439E5A`, `0x439F1C` | `.nv.shared` (no-opt path), per-entry shared |

The architecture-conditional sort (step 2) matters most here. On extended-shared-memory architectures, the `arch_vtable->preserves_layout_order` predicate returns true for `SHT_CUDA_SHARED` (`0x7000000A`), which suppresses the sort and preserves input order.

> ⚡ **QUIRK — extended-smem mode disables alignment sort**
> When the architecture vtable's `preserves_layout_order(section->sh_type)` returns true and `ctx->extended_smem_mode` is set, the engine **skips** the merge-sort entirely. The implication is that on Hopper and later, two functionally identical PTX inputs can produce different `.nv.shared.*` layouts depending on input order, even though both compile to identical instruction streams. This is intentional: extended shared memory instructions encode region IDs that depend on the layout order in ptxas's emitted code. Reordering would silently break those instructions.

Confidence: HIGH for the gate at line 28; MEDIUM for the reason (no instruction-set documentation in the binary, inferred from architecture vtable structure).

### `.nv.local.<kernel>` — Per-Entry Local Stack

Phase 7 (in `sub_439830`) iterates the per-entry local list (`ctx+280`) and calls the engine once per kernel entry, each at initial offset 0. Local variables have explicit alignment recorded by ptxas in the input cubin.

| Caller | Call site | Section family |
|---|---|---|
| `sub_439830` | `0x43A1F6` | `.nv.local.<kernel>` |

Confidence: HIGH (call site at `0x43A1F6` matches Phase 7 in the decompiled `sub_439830`).

### `.nv.constant<N>` and `.nv.constant<N>.<kernel>` — Constant Banks

Phase 9 of the layout dispatches constants through multiple sub-paths. The engine is invoked directly for:

- **Sub-path 9b** — non-OCG constant sections (typically `.nv.constant0`), with an optional initial offset taken from `ctx+504` (a syscall-const reserve).
- **OCG per-entry constants** after the per-entry copy is created.

| Caller | Call site | Section family |
|---|---|---|
| `sub_439830` | `0x43A5DB`, `0x43A7F9`, `0x43AFDC` | `.nv.constant<N>`, OCG per-entry |
| `sub_438DD0` | `0x439157` | Bindless constant target (post-rewrite) |

The bindless invocation from `sub_438DD0` is interesting: the bindless pass synthesizes `$NVLINKBINDLESSOFF_<name>` symbols and rewrites relocations, then asks the engine to re-lay-out the constant section so the synthetic symbols receive offsets. See [Bindless Relocations](bindless-relocations.md).

Confidence: HIGH for call site addresses; MEDIUM for the claim that `ctx+504` is a syscall-const reserve (matches verbose string `"constant entry %s:"` context in `sub_439830`).

## Alignment and Size Constraint Table

The engine treats every `(alignment, size)` pair according to the table below. Behavior is identical for every region kind; only the cursor's starting value differs.

| `node->alignment` | `node->size` | Behavior | Source line |
|---|---|---|---|
| `> 0` | `> 0` | Cursor rounded up to `alignment`; offset assigned; cursor += size | 36-50 |
| `> 0` | `== 0` | Same as above with size 0; cursor unchanged after placement | 36-50 |
| `== 0` | `> 0` | Cursor rounded up to `min(size, 8)`; offset assigned; cursor += size | 59-69 |
| `== 0` | `== 0` | If `ctx->no_opt_mode` (`ctx+90`): cursor unchanged, contribution placed at current cursor. Otherwise: fatal `"should only reach here with no opt"`. | 74-75 |

The natural-alignment cap at 8 bytes (step 4b) means a 16-byte contribution with `alignment = 0` is *under-aligned* to 8 bytes — the engine never aligns past 8 unless the contribution explicitly requests it. This is consistent with the ELF default for object-file alignment and matches the cap CUDA front-ends apply when emitting cubin.

> ⚡ **QUIRK — natural alignment is capped at 8**
> When a contribution records `alignment = 0` and `size > 0`, the engine derives natural alignment from `min(size, 8)`. A 16-byte struct that forgets to set explicit alignment will be 8-byte-aligned, not 16-byte-aligned, even though hardware loads of `.f64x2` and `.b128` require 16-byte alignment. Front-ends are expected to emit explicit alignment; this fallback exists only for `__device__` variables whose ABI alignment was never propagated through the IR pipeline. A miscompile here surfaces only as a misaligned load fault at kernel launch.

Confidence: HIGH (cap at 8 explicit at lines 62-64: `v17 = 8; if (v16 <= 8) v17 = ...`).

## Function Map

The engine sits at the bottom of a small calltree. The five direct callees are tightly scoped: three are arithmetic/lookup helpers, one is a recursive list sort, and one is the fatal-error reporter.

| Address | Name | Size | Role | Confidence |
|---|---|---|---|---|
| `0x4325A0` | `section_layout_engine` | 408 B | The page subject | HIGH |
| `0x432440` | `alignment_size_cmp` | 36 B | Comparator: `(alignment, size)` lexicographic — alignment descending, size ascending | HIGH |
| `0x4647D0` | `list_merge_sort_recursive` | 234 B | Bottom-up recursive merge sort over a singly-linked list of `[next, payload]` pairs | HIGH |
| `0x440590` | `get_sym_record` | 41 B | Index translator: positive index reads `ctx+344` table, negative reads `ctx+352` (signed-index convention for local vs global) | HIGH |
| `0x467460` | `fatal_diagnostic` | 1543 B | Fatal error reporter — never returns | HIGH |
| `0x432738` | `(epilogue)` | — | Final epilogue address — end of function | HIGH |

Callers (11 invocations across 3 functions):

| Address | Caller | Phase / role |
|---|---|---|
| `0x438C8F`, `0x438CB4` | `sub_438C60` (text-layout helper) | Lay out `.text.*` function bodies |
| `0x439157` | `sub_438DD0` (bindless rewrite) | Re-lay-out constant sections after bindless symbol synthesis |
| `0x43993F`, `0x439963` | `sub_439830` Phase 3 | Lay out `.nv.global` and `.nv.global.init` |
| `0x439E5A`, `0x439F1C` | `sub_439830` Phase 4-5 | Lay out `.nv.shared.*` in no-opt mode |
| `0x43A1F6` | `sub_439830` Phase 7 | Lay out per-entry `.nv.local.<kernel>` |
| `0x43A5DB`, `0x43A7F9`, `0x43AFDC` | `sub_439830` Phase 9 | Lay out `.nv.constant<N>` and per-entry constant banks |

Confidence: HIGH (every address verified from `nvlink_functions.json` caller list).

## Sort Algorithm: `sub_4647D0` and `sub_432440`

The sort is not `qsort` over an array. The 104-byte section record holds the data nodes as a *singly-linked list* via the `next` pointer at offset 0 of each 40-byte node. The merge sort routine (`sub_4647D0`) is a recursive bottom-up implementation that:

1. Splits the list in half using the slow/fast pointer technique.
2. Recursively sorts each half.
3. Merges the two sorted halves using the caller-supplied comparator.

The comparator (`sub_432440`) returns `true` (sort `a` before `b`) when `a.alignment > b.alignment`, or when `a.alignment == b.alignment` and `a.size < b.size`:

```c
bool alignment_size_cmp(data_node *a, data_node *b) {
    uint64_t aa = a->alignment;   // a+16
    uint64_t ba = b->alignment;   // b+16
    if (aa == ba)
        return a->size < b->size; // a+24
    return aa > ba;
}
```

The implication: *highest alignment first; among equal-alignment contributions, smallest first*. The "smallest first" tie-breaker is a packing heuristic — placing small contributions early lets them fill the alignment-induced gaps that would otherwise be wasted.

> ⚡ **QUIRK — sort is NOT stable across equal `(alignment, size)` pairs**
> The recursive merge sort in `sub_4647D0` *is* stable (a merge sort always preserves input order on equal keys), but the comparator returns *strict less-than* on size, not less-than-or-equal. Two contributions with identical alignment AND identical size compare equal in both directions, which means the merge step picks whichever the implementation visits first. In practice this is the first-input order from the merge phase, but it is not guaranteed by the comparator itself. A future refactor that changes `sub_4647D0` to use `<=` instead of `<` would silently reorder identically-sized symbols and could break golden-output tests.

Confidence: HIGH for comparator semantics (decompiled `sub_432440`); MEDIUM for the stability claim (sort *implementation* is stable per inspection, but spec is undefined).

## Strings Referenced

Two diagnostic strings are emitted directly from the engine:

| Address | String | When emitted |
|---|---|---|
| `0x1d38739` | `variable %s at offset %d\n` | Verbose mode (`ctx->verbose_flags & 2`); printed once per placed contribution |
| `0x1d38758` | `should only reach here with no opt` | Fatal: alignment 0 AND size 0 in optimized mode |

A third string, `"section not found"`, is loaded by the engine's null guard but lives elsewhere in `.rodata`. It is shared with every other call site that uses the same fatal-diagnostic reporter.

Confidence: HIGH (both strings cross-referenced in `nvlink_strings.json` with `referenced_by_functions = ["sub_4325A0"]`).

## Interaction with the Section Record

The engine reads and writes a single 104-byte record. The field offsets it touches:

| Offset | Field | Read/Write | Purpose |
|---|---|---|---|
| `+4` | `sh_type_ext` | Read | Passed to `arch_vtable->preserves_layout_order` to decide sort policy |
| `+32` | `sh_size` | Write | Final cursor stored here after the loop |
| `+48` | `sh_addralign` | Read + Write | Promoted to max contribution alignment after sort |
| `+64` | `verbose_flag_byte` (on `ctx`, NOT section) | Read | Bit 1 enables the verbose `fprintf` |
| `+72` | `data_list_head` | Read + Write (via sort) | Linked list of 40-byte data nodes |

The engine does **not** touch `sh_offset` (+24, ELF file offset), `sh_flags` (+8), `sh_info` (+40), `sh_link` (+44), `sh_entsize` (+56), `section_index` (+64), `symbol_list_tail` (+80), or `name_ptr` (+96). Those are written by the merge phase or the ELF writer.

> ⚡ **QUIRK — `sh_size` is the cursor, not the byte count**
> The engine writes `cursor` (the next free offset) into `section->sh_size` at line 82, then returns the *last placed offset* (not the cursor). For a section with three 4-byte contributions starting at offset 0, `sh_size` becomes 12 but the return value is 8. Callers that need the byte count must read `section->sh_size`, not use the return value. The ELF writer uses `sh_size` correctly; only ad-hoc callers that interpret the return value as "size" are at risk.

Confidence: HIGH (return path verified at decompiled source: the assignment `*(_QWORD *)(a2 + 32) = v9` writes the cursor `v9`, and `return a3` returns the *last assigned* offset, which is updated only inside the placement branches).

## Re-entry from Bindless Path

The bindless pass (`sub_438DD0`, called from layout Phase 2) creates synthetic symbols (`$NVLINKBINDLESSOFF_<name>`) and *re-inserts* them into a previously-laid-out constant section's data list. After insertion, `sub_438DD0` calls the engine again to assign offsets to the new synthetic symbols. The engine handles this correctly: the re-sort places the synthetic symbols among the existing ones according to their alignment, and the dual write-back updates both the new symbols and the existing ones (who keep their offsets only because the sort happens to be stable for unchanged inputs).

> ⚡ **QUIRK — repeated invocation can move existing symbols**
> Re-running the engine on a section that has already been laid out does NOT preserve previous offsets. Every contribution is re-placed from `initial_offset = 0` based on the current sorted order. This is safe for the bindless path because the bindless pass re-runs the engine before any relocation has been applied. It would be unsafe for any future caller that calls the engine after relocations have been resolved against the previous offsets — those relocations would silently point to wrong addresses.

Confidence: MEDIUM (bindless re-entry behavior inferred from `sub_438DD0`'s call site at `0x439157` and the engine's lack of any "already laid out" guard; no test harness to confirm).

## Error Conditions

| Condition | Diagnostic | Severity | Source |
|---|---|---|---|
| `section == NULL` at entry | `"section not found"` | Fatal | line 26-27 |
| `alignment == 0 && size == 0` in optimized mode | `"should only reach here with no opt"` | Fatal | lines 74-75 |
| Comparator returns inconsistent result | (none; undefined behavior in sort) | Silent | n/a |
| Contribution placed past 32-bit cursor wraparound | (none) | Silent | n/a |

The cursor is a 32-bit value (`unsigned int`) per the function signature. A section larger than 4 GiB would wrap the cursor and silently corrupt the layout. This is a non-issue in practice because no CUDA section approaches that size — `.nv.shared` is bounded by per-block shared memory (~228 KiB max on Blackwell), constant banks are 64 KiB or less, and `.nv.global` rarely exceeds tens of MB.

> ⚡ **QUIRK — `initial_offset` is 32-bit but the cursor must be 64-bit-clean**
> The `a3` parameter is `unsigned int` (32-bit), which the engine widens to 64-bit via `v9 = a3`. All cursor arithmetic is 64-bit, but the *return value* is the 32-bit `a3`. If a caller passes a 32-bit initial offset that is later widened past 4 GiB by cursor advances, the return value silently truncates. The fix is to read `section->sh_size` instead of the return value for any caller that needs the post-call cursor. The current callers (`sub_439830`, `sub_438C60`, `sub_438DD0`) all do exactly that, but the bug is latent in the signature.

Confidence: HIGH (the assignment `LODWORD(v9) = v11` at line 46 explicitly truncates to 32 bits, then widens back via `v9 = a3`).

## Cross-References

- [Section Merging](section-merging.md) — the merge layer that builds the data-node linked list the engine consumes
- [Layout Phase](../pipeline/layout.md) — the eleven call sites in pipeline context
- [Section Record](../structs/section-record.md) — the 104-byte structure the engine reads and writes
- [Bindless Relocations](bindless-relocations.md) — the only caller that re-invokes the engine on an already-laid-out section
- [R\_CUDA Relocations](r-cuda-relocations.md) — relocations resolve against the symbol offsets this engine writes
- [Data Layout Optimization](data-layout-opt.md) — the constant-dedup pass that may run between merge and this engine

## Confidence Assessment

| Claim | Confidence | Evidence |
|---|---|---|
| Address `0x4325A0`, 408 bytes, 19 basic blocks, 105 instructions | HIGH | `nvlink_functions.json` and context file |
| Eleven distinct call sites across `sub_438C60`, `sub_438DD0`, `sub_439830` | HIGH | Caller list extracted from `nvlink_functions.json` |
| Null guard emits `"section not found"` | HIGH | Decompiled lines 26-27; string at `0x1d38758` with sole referrer `sub_4325A0` |
| Sort gate uses `ctx->extended_smem_mode` (`+100`) AND vtable predicate (`+200` from `+488`) | HIGH | Decompiled line 28 with both conjuncts visible |
| Sort routine is `sub_4647D0` with comparator `sub_432440` | HIGH | Decompiled line 29 |
| Comparator is `(alignment desc, size asc)` strict-less-than | HIGH | Decompiled `sub_432440` is 16 lines and unambiguous |
| Natural alignment cap at 8 when `alignment == 0` | HIGH | Decompiled lines 62-64: `v17 = 8; if (v16 <= 8) v17 = ...` |
| Dual write-back to symbol record and data node | HIGH | Lines 41-42, 61-62, 68-69 |
| Verbose gate `ctx+64 & 2` controls `fprintf` | HIGH | Lines 43, 63 |
| `"should only reach here with no opt"` gated by `ctx+90` | HIGH | Lines 74-75 |
| Final `sh_size` written to `section+32`, return value is last offset | HIGH | Line 82 (`*(_QWORD *)(a2 + 32) = v9`) followed by `return a3` |
| Per-region call sites map to documented phases | HIGH | Call addresses cross-referenced with `sub_439830` phase boundaries |
| Natural-alignment cap rationale (matches CUDA ABI default) | MEDIUM | Inferred from CUDA documentation; no direct evidence in binary |
| Bindless re-entry preserves previous offsets (it does not) | MEDIUM | Inferred from absence of "already laid out" guard |
| Sort stability claim | MEDIUM | `sub_4647D0` *is* a stable merge sort by inspection, but spec is undefined |
| 32-bit cursor wraparound is latent in signature | HIGH | `a3` is `unsigned int`, decompiled `LODWORD(v9) = v11` truncates |
