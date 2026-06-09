# Frontend Wrapup

`fe_wrapup` (`sub_588F90`, 1433 bytes at `0x588F90`, from `fe_wrapup.c:776`) is the sixth stage of the cudafe++ pipeline. It runs after the parser has built the complete EDG IL tree and before the backend emits the `.int.c` file. The function performs five sequential passes over the translation unit chain, each pass iterating the linked list rooted at `qword_106B9F0`. After the five passes, it runs a series of post-pass operations: cross-TU consistency checks, graph optimization, template validation, memory statistics reporting, and global state teardown. The function has 51 direct callees.

The five passes transform the raw IL tree into a finalized, pruned representation: Pass 1 cleans up parsing artifacts, Pass 2 computes which entities are needed, Pass 3 marks entities that must be preserved in the IL for device compilation, Pass 4 eliminates everything not marked, and Pass 5 serializes the result and validates scope consistency. The entire sequence is the bridge between the parser's "everything parsed" state and the backend's "only what matters" input.

## Key Facts

| Property | Value |
|---|---|
| Function | `sub_588F90` (`fe_wrapup`) |
| Binary address | `0x588F90` |
| Binary size | 1433 bytes |
| EDG source | `fe_wrapup.c`, line 776 |
| Direct callees | 51 |
| Debug trace name | `"fe_wrapup"` (level 1 via `sub_48AE00`) |
| Assertion | `"bad translation unit in fe_wrapup"` if `dword_106BA08 == 0` |
| Error check | `qword_126ED90` — passes 2-4 skip TUs with errors |
| Language gate | `dword_126EFB4 == 2` gates C++-only operations in pass 4 |

## Architecture Overview

```text
sub_588F90 (fe_wrapup)
  |
  |-- Preamble: debug trace, assertion, C++ wrapup, diagnostic hooks
  |
  |-- Pass 1: per-TU basic declaration processing (sub_588C60)
  |-- Pass 2: template/inline instantiation + needed-flags (sub_707040)
  |       |-- gated by !qword_126ED90 (skip error TUs)
  |       |-- preceded by cross-TU marking (sub_796C00) on first run
  |-- Pass 3: keep-in-IL marking for device code (sub_610420 with arg 23)
  |       |-- sets dword_106B640=1 guard, clears after
  |-- Pass 4: constant folding + CUDA transforms + dead entity elimination
  |       |-- sub_5CCA40 (C++ only), sub_5CC410, sub_5CCBF0
  |-- Pass 5: per-TU final cleanup (sub_588D40)
  |
  |-- Post-pass: cross-TU consistency (sub_796BA0)
  |-- Post-pass: graph optimization (sub_707480 double-loop)
  |-- Post-pass: template validation (sub_765480)
  |-- Post-pass: final main-TU cleanup (sub_588D40)
  |-- Post-pass: file index processing (sub_6B8B20 loop)
  |-- Post-pass: output flush (sub_5F7DF0)
  |-- Post-pass: close output files (sub_4F7B10 x3)
  |-- Post-pass: memory statistics (10 subsystem counters -> sub_6B95C0)
  |-- Post-pass: debug dumps (sub_702DC0, sub_6C6570)
  |-- Post-pass: final teardown (sub_5E1D00, sub_4ED0E0, zero 6 globals)
```

## Translation Unit Chain

All five passes iterate the same linked list structure. Each translation unit descriptor is a 424-byte allocation. The first qword of each descriptor is the `next` pointer, forming a singly-linked list. The head is `qword_106B9F0` (the primary TU). For standard single-file CUDA compilation, there is typically one primary TU and zero secondary TUs, but the multi-TU infrastructure exists for module compilation and precompiled headers.

Before processing each TU, `sub_7A3D60` (set_current_translation_unit) is called to switch global state to point at that TU. This updates `qword_106BA10` (current TU descriptor), which is then used by all subsystems to find the current scope, IL root, file info, and error state.

The file scope IL node — the root of the IL tree for a TU — is at `*(qword_106BA10 + 8)`.

The iteration pattern shared by all passes:

```c
// Walk secondary TUs (linked from primary)
node = *(qword **)qword_106B9F0;     // first secondary TU
while (node) {
    sub_7A3D60(node);                 // set node as current TU
    // ... pass-specific work on *(qword_106BA10 + 8) ...
    node = *(qword **)node;           // follow next pointer at +0
}
// Then process primary TU
sub_7A3D60(qword_106B9F0);
// ... pass-specific work on main TU ...
```

## Preamble

Before the five passes begin, `fe_wrapup` performs:

1. **Debug trace**: If `dword_126EFC8` (debug mode), logs `"fe_wrapup"` at level 1 via `sub_48AE00`.
2. **Set current TU**: Calls `sub_7A3D60(qword_106B9F0)` to select the primary TU.
3. **Assertion**: Checks `dword_106BA08 != 0` — the "full compilation mode" flag. If false, triggers a fatal assertion: `"bad translation unit in fe_wrapup"`. This flag is set during TU initialization; its absence here indicates a corrupted pipeline state.
4. **C++ template wrapup**: If `dword_126EFB4 == 2` (C++ mode), calls `sub_78A9D0` (`template_and_inline_entity_wrapup`). This performs cross-TU template instantiation setup, walking all TUs and their pending instantiation lists.
5. **No-op hook**: Calls `nullsub_5` — a disabled debug hook in the exprutil address range (`0x56DC80`). Likely a compile-time-disabled expression validation point.
6. **CUDA diagnostics**: If `dword_106C268` is set, calls `sub_6B3260` (CUDA-specific diagnostic processing).
7. **Source sequence debug**: If debug mode and the `"source_file_for_seq_info"` flag is active, calls `sub_5B9580` to dump source file sequence information.

## Pass 1: Basic Declaration Processing

**Function:** `sub_588C60` (`file_scope_il_wrapup`)
**Address:** `0x588C60`
**Per-TU:** Yes (iterates all secondary TUs, then processes the primary TU)
**Error-gated:** No — runs unconditionally

This pass performs initial cleanup on each translation unit's IL tree. It runs unconditionally on every TU, regardless of error status, because the cleanup operations (template state release, exception spec finalization) are safe and necessary even after errors.

Operations per TU:

| Step | Function | Purpose |
|---|---|---|
| 1 | `sub_7C2690` | Template cleanup — release deferred template instantiation state |
| 2 | `sub_68A0C0` | Exception handling cleanup — finalize exception specifications, resolve pending catch-block types |
| 3 | `sub_446F80` | Diagnostic finalization (conditional: only if `dword_106C2BC` or `dword_106C2B8` is set, and `dword_106C29C` is clear — i.e., not preprocessing-only mode) |
| 4 | `sub_706710` | IL tree walk with parameters `(root, 0, scope_list, 1, 0, 0)` — traverses the full IL tree performing bookkeeping: arg 2=0 means initial walk, arg 4=1 enables scope processing, arg 3 passes the TU scope list at `qword_106BA10 + 24` |
| 5 | `sub_706F40` | IL finalize — post-walk finalization of the IL root node, marks it as ready for lowering |
| 6 | `sub_5BD350` | Destroy temporaries (C++ only, `dword_126EFB4 == 2`) — cleans up temporary objects from expression evaluation |
| 7 | (inline loop) | Clear deferred declaration flags (C++ only, `dword_126EE50 == 0`): iterates the declaration chain at `*(root + 280)`, and for each declaration where bit 2 of byte `+81` is set and `sub_5CA6F0` returns true, clears the pointer at `+40` and clears bit 2 of byte `+81`. This removes deferred-initialization markers from declarations whose initialization has completed. |
| 8 | `sub_65D9A0` | Overload resolution cleanup — releases candidate sets and viability data |

After all secondary TUs are processed, the primary TU itself gets the same treatment:

```c
for (tu = *primary_tu; tu != NULL; tu = *tu)
    set_current_tu(tu);
    file_scope_il_wrapup();           // sub_588C60
set_current_tu(primary_tu);
file_scope_il_wrapup();               // for the primary TU itself
```

### Cross-TU Marking (Between Pass 1 and Pass 2)

Before Pass 2 begins, if no errors have occurred (`!qword_126ED90`), `sub_796C00` (`mark_secondary_trans_unit_IL_entities_used_from_primary_as_needed`) is called. This function:

1. Calls `sub_60E4F0` with callbacks `sub_796A60` (IL walk visitor) and `sub_796A20` (scope visitor) to walk the primary TU's IL and mark entities referenced from secondary TUs.
2. Iterates the file table (`dword_126EC80` entries starting at index 2), and for each valid file scope that is not bit-2 flagged in byte `-8` and has a non-zero scope kind byte `+28`, calls `sub_610200` with the same visitor callbacks.
3. Runs the walk twice (controlled by a counter: first pass with callback `sub_796A60`, second with NULL). The two-pass design ensures transitive closure: the first pass discovers direct references, the second propagates through chains of indirect references.

## Pass 2: Template/Inline Instantiation and Needed-Flags

**Function:** `sub_707040` (`set_needed_flags_at_end_of_file_scope`)
**Address:** `0x707040`
**Per-TU:** Yes, but skips TUs with errors (`qword_126ED90` check)
**Source:** `scope_stk.c:8090`

This pass determines which entities are "needed" — must be preserved in the IL for backend consumption. It is the EDG "needed flags" computation, which decides based on linkage, usage, and language rules whether each declaration must survive to the output.

The function operates on a file scope IL node and walks four declaration lists at different offsets:

| Offset | List | Entity Kind | Processing |
|---|---|---|---|
| `+168` | Nested scopes | Namespace/class scopes | Recursively calls `sub_707040` on each scope's IL root at `*(entry + 120)`, skipping entries with bit 0 of byte `+116` set (extern linkage marker) |
| `+104` | Type declarations | Classes (kind 9-11 at byte `+132`) | Calls `sub_61D7F0(entry, 6)` to set needed flag; recursively processes the class scope at `*(*(entry+152) + 128)` if non-null and bit 5 of byte `+29` is clear |
| `+112` | Variable declarations | Variables/objects | Complex multi-condition evaluation (see below) |
| `+144` | Routine declarations | Functions/methods | Checks template body availability at `*(entry+240)` and `*(*(entry+240)+8)`, bit 2 of byte `+186` (not-needed marker), and entity class at byte `+164`; marks via `sub_61CE20(entry, 0xB)`; preserves and restores bit 5 of byte `+177` across the call |

### Variable needed-flag logic

For each variable in the `+112` list, the algorithm checks (in order of precedence):

1. If bit 3 of byte `+80` is set (external/imported), skip — always mark as needed via `sub_61CE20(entry, 7)`.
2. Check `sub_7A7850(*(entry+112))` — if referenced, mark as needed.
3. Check `sub_7A7890(*(entry+112))` — if used, mark as needed.
4. Otherwise evaluate:
   - Byte `+162` bit 4 set and full compilation mode: check linkage class at byte `+128` (1=external) and base type completeness via `sub_75C1F0`.
   - Byte `+128` == 0 (no linkage) or byte `+169` == 2: check initializer pointer at `+224` and constexpr flags at byte `+164`.
   - Internal/external linkage with specific storage class: check definition pointer at `+200`, storage class byte `+128`, and flag patterns in bytes `+160`, `+161`.

At the start of file scope processing, `dword_106B640` is set to 1. At the end, after optionally calling `sub_6FE8C0` (C++ scope merging), it is cleared to 0.

Debug trace: prints `"Start/End of set_needed_flags_at_end_of_file_scope"` when the `"needed_flags"` debug flag is active.

## Pass 3: Keep-in-IL Marking (Device Code Selection)

**Function:** `sub_610420` (`mark_to_keep_in_il`)
**Address:** `0x610420`
**Per-TU:** Yes, skips error TUs
**Source:** `il_walk.c:1959`
**Argument:** 23 (the file-scope walk mode)

This is the critical CUDA-specific pass. It determines which entities must be preserved in the intermediate language for device code compilation by `cicc`. The guard flag `dword_106B640` is set to 1 before the call and cleared to 0 after, preventing accidental re-invocation.

The keep-in-IL bit is bit 7 (0x80) of the byte at `(entity_pointer - 8)`. Testing uses signed comparison: `*(entry - 8) < 0` means "marked for keeping."

### Operation

1. **Save/restore state**: Saves and restores 9 global callback/state variables (`qword_126FB88` through `dword_126FB60`), installing `sub_617310` (`prune_keep_in_il_walk`) as the walk prune callback at `qword_126FB78`. All other callback slots are zeroed. The callback set at `dword_126FB58` is set to `(byte_at_a1_minus_8 & 2) != 0` — derived from a flag in the scope node header.

2. **File scope walk**: When `a2 == 23` and scope kind byte `*(a1+28)` is 0 (file scope), clears bit 7 of byte `*(a1-8)` via `AND 0x7F`. Then calls `sub_6115E0(a1, 23)` — the recursive `walk_tree_and_set_keep_in_il` traversal on the file scope root.

3. **C++ companion walk**: For C++ mode (`dword_126EFB4 == 2`), calls `sub_6175F0(a1)` to walk scopes and mark out-of-line definitions and friend declarations.

4. **Guard assertion**: Asserts `dword_106B640 != 0`. If the guard was cleared during the walk, fires a fatal assertion at `il_walk.c:1959` with function name `mark_to_keep_in_il`.

5. **Pending entity lists**: Iterates the deferred entity list at `qword_126EBA0`, calling `sub_6115E0(entity, 55)` for each entry with bit 2 set in byte `*(entity[1] + 187)` (the "deferred instantiation needed" flag).

6. **43 category-specific walks**: Iterates 43 global lists, each containing entities of a specific IL category. Each list is walked with a category-specific tag argument:

   | Global range | Tags | Count |
   |---|---|---|
   | `qword_126E610` — `qword_126E770` | 1--23 | 23 lists |
   | `qword_126E7B0` — `qword_126E7E0` | 27--30 | 4 lists |
   | `qword_126E810` — `qword_126E8A0` | 33--42 | 10 lists |
   | `qword_126E8E0` — `qword_126E900` | 46--48 | 3 lists |
   | `qword_126E9B0`, `qword_126E9D0`, `qword_126E9E0`, `qword_126E9F0` | 59, 61, 62, 63 | 4 lists |
   | `qword_126EA80` | 72 | 1 list |

   These lists follow a reverse-linked structure where the back-pointer is at `*(list_entry - 16)`, not at offset +0. Each entity's tag tells `sub_6115E0` what kind of entity it is processing, which affects how the keep_in_il mark propagates to dependents.

7. **Using-declaration fixed-point**: Processes namespace member entries at `*(root + 256)` via `sub_6170C0(member, is_file_scope, &changed)` in a loop that repeats until `changed == 0`. The `is_file_scope` flag is derived from `*(a1+28)` being 2 or 17.

8. **Hidden name resolution**: If `*(a1+264)` is non-NULL, walks hidden name entries. Each entry has a linked list at `entry[1]` with per-entry kind at `*(entry + 16)` (byte). Five kinds are handled:

   | Kind | Name | Action |
   |---|---|---|
   | `0x35` | Instantiation | Walk via `sub_6170C0` on `*(entry[3] + 8)` |
   | `0x33` | Function template | Conditional marking based on scope type and entity mark |
   | `0x34` | Variable template | Same as 0x33 with `v111 = entry[3]` |
   | `0x36` | Alias template | Same as 0x33 with `v110 = entry[3]` |
   | `6` | Class/struct | Special handling: checks typedef chain at byte `+132 == 12` with non-null source at `+8`; marks via `sub_6115E0(entity, 6)` for file-scope entries |

   For each marked hidden name entry, the keep_in_il bit at `*(entry - 8)` is set via OR with `0x80`.

9. **Context restore**: Restores all saved function pointers and state variables.

Debug trace: `"Beginning/Ending file scope keep_in_il walk"` when the `"needed_flags"` flag is active.

For full details on the keep-in-IL mechanism, see [Keep-in-IL](../il/keep-in-il.md).

## Pass 4: Constant Folding, CUDA Transforms, and Dead Entity Elimination

**Per-TU:** Yes, skips error TUs
**C++ only:** The `sub_5CCA40` call is gated by `dword_126EFB4 == 2`

This pass has three sub-stages per TU. The first (`sub_5CCA40`) clears flags to prevent unnecessary work. The second (`sub_5CC410`) removes function bodies. The third (`sub_5CCBF0`) removes entire IL entries.

### Stage 4a: Clear Unneeded Instantiation Flags — `sub_5CCA40`

**Address:** `0x5CCA40`
**Source:** `il.c:29450` (`clear_instantiation_required_on_unneeded_entities`)
**C++ only:** Asserts `dword_126EFB4 == 2`

Walks the same four declaration lists as Pass 2 (nested scopes at `+168`, types at `+104`, routines at `+144`, and for non-file scopes variables at `+112`). For routines that are not marked for keeping but have instantiation-required flags set, calls `sub_78A380(entity, 0, 2)` to clear the instantiation-required bit. This prevents the template engine from instantiating definitions that will be eliminated in the next sub-stage.

The conditions for clearing a routine's instantiation-required flag are:
- Byte `+80` bit 3 clear (not an external/imported entity)
- Byte `+179` bit 4 clear (not a special instantiation)
- Byte `+179` bits 1-2 == `0b10` (has "instantiation required" set) OR (`dword_126E204` is set AND byte `+176` bit 7 is set)
- Non-null template pointer at `*(entity + 0)` (has a source template)
- Byte `+176` bit 1 clear (not already processed)

For non-file scopes (byte `+28` of scope is nonzero), additionally processes variables in the `+112` list with an analogous pattern: byte `+162` bit 6 clear, bits 4-5 in the pattern `(v8 & 0xB0) == 0x10`, with a non-null pointer at `*(entry + 0)`.

### Stage 4b: Eliminate Unneeded Function Bodies — `sub_5CC410`

**Address:** `0x5CC410`
**Source:** `il.c:29231` (`eliminate_bodies_of_unneeded_functions`)
**Gate:** `dword_126E55C != 0` (deferred class members exist)

Iterates the scope table (`qword_126EB98`, 16-byte entries: `{qword scope_ptr, int file_index, pad}`). The iteration runs from index 1 through `dword_126EC78`. For each entry:

1. Checks that the file reference at `qword_126EC88[file_index]` is non-null.
2. Checks TU ownership:
   - Primary TU (`qword_106BA10 == qword_106B9F0`): checks `(*(scope_ptr - 8) >> 1) ^ 1) & 1` — bit 1 of the pre-header flags byte must be clear.
   - Secondary TU: checks `qword_126DFE0[*(scope_ptr + 24)] == qword_106BA10` — the scope's file index maps to the current TU.
3. Verifies scope kind byte `+28` == 17 (class/namespace scope).
4. Checks the keep-in-il mark: bit 2 of byte `*(scope_ptr + 187)` must be clear (not needed) AND the scope file entry has bit 0 of byte `+29` set (eligible for elimination).
5. If all checks pass, calls `sub_5CAB40` to remove the function body from the scope.

In C++ mode with `dword_126EFB4 == 2`, also calls `sub_6FFBA0` to reorganize namespace-level declarations after body removal.

Debug trace: `"eliminate_bodies_of_unneeded_functions"` at level 3.

### Stage 4c: Eliminate Unneeded IL Entries — `sub_5CCBF0`

**Address:** `0x5CCBF0`
**Source:** `il.c:29598` (`eliminate_unneeded_il_entries`)
**Gate:** `dword_126E55C != 0`

The heaviest sub-stage. First calls `sub_703C30(a1)` to get a scope summary structure (7-element qword array stored at `v2`), asserting the result is non-null. Then walks four entity lists, removing entries whose keep-in-IL mark (bit 7 of byte at entity - 8) is clear:

| List | Offset | Entity Type | Removal actions |
|---|---|---|---|
| Variables | `+112` | Variable declarations | Unlink from list; for C++, call `sub_7B0B60` on type pointers at `+112` and `+216` with callback `sub_5C71B0` (id 147) to clean up associated type metadata |
| Routines | `+144` | Function/method declarations | Unlink from list; same `sub_7B0B60` type cleanup on type pointers at `+144` and `+248`; set bit 5 of byte `+87` in the routine supplement at `*(entity+152)` |
| Types | `+104` | Type declarations | Unlink from list; for class entities (kind 9-11 at byte `+132`), call `sub_5CB920` (C++ member cleanup) then `sub_5E2D70` (scope deallocation); set bit 5 of byte `+87` in the entity supplement |
| Hidden names | `+272` | Hidden name entries | Unlink unmarked entries from list |

After variable/routine/type processing, the tail pointers are stored into `v2[5]`, `v2[6]`, and `v2[4]` respectively (the scope summary structure).

For file-scope nodes (byte `+28` == 0), additionally calls `sub_5CC570` (eliminate unneeded scope orphaned list entries) after variable processing, and `sub_718720` (scope-level cleanup) after type/hidden-name processing.

After list processing, walks `qword_126EBE0` (a global deferred entity chain) and removes entries where `*(entry - 8) >= 0` (bit 7 clear = not marked).

### String arithmetic in debug output

The diagnostic output uses a pointer arithmetic trick: `"TARG_VERT_TAB_CHAR" + 17` evaluates to `"R"`, so the format string `"%semoving variable "` produces either `"Removing variable ..."` (when the entity is being removed) or `"Not removing variable ..."` (when kept).

### Deferred-Class-Members Flag

Pass 4 checks `dword_126E55C` after each TU's stage 4a. This flag indicates whether there are deferred class member definitions that need processing. If no errors occurred and the flag is set, stages 4b and 4c run. If errors are present during the per-TU loop, the flag is simply cleared to 0 and stages 4b/4c are skipped for that TU.

## Pass 5: Per-TU Final Cleanup

**Function:** `sub_588D40` (`file_scope_il_wrapup_part_3`)
**Address:** `0x588D40`
**Source:** `fe_wrapup.c:559`
**Per-TU:** Yes (all TUs, no error skip)

This pass performs final statement-level processing and scope validation, then optionally re-runs the Pass 2-4 sequence for the main compilation unit.

### Operations

1. **Statement finalization**: `sub_5BAD30` — finalizes statement-level IL nodes (label resolution, goto target binding, fall-through analysis).

2. **Scope stack assertion** (C++ with `dword_106BA08`): Verifies that `*(qword_126C5E8 + 784 * dword_126C5E4 + 496) == qword_126E4C0`. The scope stack is an array of 784-byte entries at `qword_126C5E8`, indexed by `dword_126C5E4` (current depth). The assertion checks that the scope pointer at offset +496 of the current entry matches the expected file scope entity (`qword_126E4C0`). On mismatch, triggers a fatal assertion at `fe_wrapup.c:559` with function name `file_scope_il_wrapup_part_3`.

3. **Scope cleanup**: For C++ mode, calls `sub_5C9E10(0)` — finalizes class scope processing, resolves deferred member access checks.

4. **IL output**: `sub_709250` — serializes the IL tree to the IL output stream. This produces the internal representation that the backend reads, not the final `.int.c` file.

5. **Template output**: `sub_7C2560` — serializes template instantiation information to the output.

6. **Mirrored 3-pass sequence** (only when `dword_106BA08` — full compilation mode): Re-runs passes 2-4 on the main TU's file scope node. This handles entities that were discovered or modified during the per-TU passes. The re-run is necessary because secondary TU processing may have added new cross-references to the primary TU's entities:
   - `sub_707040(file_scope)` (needed flags) — if errors appear (`qword_126ED90`), clears `dword_126E55C` and skips remaining
   - `sub_610420(file_scope, 23)` with `dword_106B640 = 1/0` guard — again abort if errors
   - `sub_5CCA40(file_scope)` (clear instantiation flags, C++ only)
   - `sub_5CC410()` + `sub_5CCBF0(file_scope)` (eliminate, if `dword_126E55C`)

7. **Source file state**: `sub_6B9580` — updates source file tracking counters.

8. **Diagnostic flush**: `sub_4F4030` — flushes pending diagnostic messages for this TU.

9. **File scope cleanup**: `sub_6B9340(dword_126EC90)` — closes file scope state, passing the current error count for this file.

## Post-Pass Operations

After all five passes complete, `fe_wrapup` performs a series of global operations that are not per-TU.

### Cross-TU IL Consistency — `sub_796BA0`

**Address:** `0x796BA0`
**Source:** `trans_copy.c:3003` (`copy_secondary_trans_unit_IL_to_primary`)

Called only when there are no errors (`!qword_126ED90`), the multi-TU flag is clear (`!dword_106C2B4`), and there are secondary TUs (`*(qword_106B9F0) != 0`). In the current binary, this function always triggers a fatal assertion at `trans_copy.c:3003` — the multi-TU IL copy infrastructure is compiled but disabled, likely reserved for future C++ module compilation support. The function traces `"copy_secondary_trans_unit_IL_to_primary"` before aborting.

### Scope Renumbering — `sub_707480`

**Address:** `0x707480`
**Source:** `scope_stk.c`

Called when `dword_126C5A0` (scope renumbering flag) is set and `dword_126EC78 > 0` (scope count is positive). Executes a double-loop:

```c
unsigned pass = 1;
do {
    for (int idx = 1; idx < dword_126EC78; idx++)
        sub_707480(idx, pass);
    if (!pass) break;
    pass = 0;
} while (dword_126EC78 > 0);
dword_126C5A0 = 0;
```

For each scope entry at `qword_126EB98 + 16 * idx`:
- Extracts the scope pointer at `+0` and file index at `+8`
- Checks non-null scope pointer, valid file reference in `qword_126EC88[file_index]`
- Verifies scope kind byte `+28` == 17 (class/namespace scope)
- In `pass=1`: skips entries where byte `+176` of the entity at `*(scope+32)` is non-negative
- Checks bit 1 of byte `*(scope-8)` is clear and bit 0 of byte `+29` is clear
- In C++ mode with bit 5 of byte `*(*(scope+32) + 186)` set: calls `sub_6FFBA0` to reorganize scope members
- Calls `sub_6FE2A0(scope, 0, 1)` to renumber the scope's declaration entries

After the double-loop, clears `dword_126C5A0 = 0`.

### Template Validation — `sub_765480`

**Address:** `0x765480`
**Source:** `templates.c:19822` (`remove_unneeded_instantiations`)

Called unless `dword_106C094 == 1` (minimal compilation mode). Walks the instantiation pending list at `qword_12C7740` (linked via offset `+8`) and removes template instantiations that are no longer needed:

| Referent kind (byte `+80`) | Entity kind | Action |
|---|---|---|
| 9 | Class template instantiation | If function body exists and is unreferenced (or `dword_106C094 == 2`), call `sub_5CE710` to eliminate class definition |
| 7 | Function template instantiation | Same check with `dword_106C094 != 2` guard |
| 10-11 | Variable/alias template | Call `sub_5BBC70` to find underlying function, then `sub_5CAB40` to remove body |

Each entry has: offset `+16` = template entity pointer, offset `+24` = referent entity, offset `+80` = flags byte.

### Final Main-TU Cleanup

Calls `sub_588D40` one more time on the main translation unit (not iterating the chain). This ensures the primary TU gets the same final cleanup treatment as secondary TUs.

### File Index Processing

If the primary TU has secondary TUs (`*(qword_106B9F0) != 0`), iterates the file table starting at index 2 through `dword_126EC80`:

```c
for (int idx = 2; idx <= dword_126EC80; idx++) {
    if (!qword_126EC88[idx] || *(byte *)(qword_126EB90[idx] + 28))
        continue;
    sub_6B8B20(idx);
}
```

`sub_6B8B20` resets the file state for each valid, non-header file index, updating the source file manager's tracking structures.

### Output Flush and File Close

1. **Conditional flush**: If `dword_106C250` is set and no errors, calls `sub_5F7DF0(0)` — flushes the IL output stream.
2. **Close three output files** via `sub_4F7B10`:

   | Call | File pointer | ID | Identity |
   |---|---|---|---|
   | `sub_4F7B10(&qword_106C280, 1513)` | Primary output | 1513 | Main `.int.c` output (or stdout) |
   | `sub_4F7B10(&qword_106C260, 1514)` | Secondary output | 1514 | Module interface or IL dump |
   | `sub_4F7B10(&qword_106C258, 1515)` | Tertiary output | 1515 | Template instantiation log |

   `sub_4F7B10` checks if the file pointer is non-null, zeroes it, calls `sub_5AEAD0` (fclose wrapper), and on error triggers diagnostic `sub_4F7AA0` with the given ID.

### Memory Statistics Reporting

Triggered when any of these conditions hold:
- `dword_106BC80` is set (always-report-stats flag)
- `dword_126EFCC > 0` (verbosity level > 0)
- Debug mode (`dword_126EFC8`) with `"space_used"` flag active

Sums the return values of 10 subsystem `space_used` functions:

| # | Function | Address | Subsystem | Report Header |
|---|---|---|---|---|
| 1 | `sub_74A980` | `0x74A980` | Symbol table | `"Symbol table use:"` |
| 2 | `sub_6B6280` | `0x6B6280` | Macro table | `"Macro table use:"` |
| 3 | `sub_4ED970` | `0x4ED970` | Error/diagnostic table | `"Error table use:"` |
| 4 | `sub_6887C0` | `0x6887C0` | Conversion table | (conversion/cast subsystem) |
| 5 | `sub_4E8F60` | `0x4E8F60` | Declaration table | (declarations subsystem) |
| 6 | `sub_56D8C0` | `0x56D8C0` | Expression table | `"Expression table use:"` |
| 7 | `sub_5CEA80` | `0x5CEA80` | IL table | (IL node/class subsystem) |
| 8 | `sub_726C80` | `0x726C80` | Mangling table | (name mangling subsystem) |
| 9 | `sub_6FDF00` | `0x6FDF00` | Lowering table | (IL lowering subsystem) |
| 10 | `sub_419150` | `0x419150` | Diagnostic table | (diagnostic output subsystem) |

Each function prints its own detailed allocation table to stderr in a standardized format with columns `Table / Number / Each / Total`, tracks "lost" entries (allocated count minus free-list traversal count), and returns its total byte count.

The cumulative sum is passed to `sub_6B95C0` (`print_memory_management_statistics` at `0x6B95C0`), which prints the grand total accounting report:

```text
Memory management table use:
                    Table   Number     Each    Total
             text buffers      NNN       40     NNNN
                    Total                       NNNN

Allocated space in all categories:
           Total of above                    NNNNNNN
    Skipped for alignment                       NNNN
       File mapped memory                          0
          Mapped from PCH                          0  (included in previous line)
      Mapped IL file size                          0
               Not listed                      NNNNN
               Total used                    NNNNNNN
  Avail in used mem blocks                      NNNN
 Avail in freed mem blocks                         0
             Max mem alloc                   NNNNNNN
```

The "Not listed" entry is computed as `qword_1280700 + qword_1280708 - qword_12806F8 - total_above` — it captures memory allocated by subsystems that do not have their own `space_used` reporter.

### Debug Dumps

If debug mode (`dword_126EFC8`) is active:
- `"scope_stack"` flag: calls `sub_702DC0` — dumps the entire scope stack to stderr, showing all active scopes with their indices, kinds, and entity counts.
- `"viability"` flag: calls `sub_6C6570` — dumps overload viability information, showing candidate sets and resolution decisions.

### Final Teardown

1. **IL allocator check** — `sub_5E1D00` (`check_local_constant_use` at `il_alloc.c:1177`): Copies `qword_126EFB8` to `qword_126EDE8` (restores the IL source position to a baseline). Asserts `qword_126F680 == 0` — no pending local constants should remain after wrapup. If nonzero, fires a fatal assertion.

2. **Zero 6 global state variables**:
   - `qword_126DB48 = 0` — pending entity pointer (scope tracking)
   - Call `sub_4ED0E0()` — declaration subsystem cleanup (releases declaration pools)
   - `dword_126EE48 = 0` — init-complete flag (cleared, marking end of frontend processing)
   - `qword_106BA10 = 0` — current TU descriptor (no active TU)
   - `qword_12C7768 = 0` — template state pointer 1
   - `qword_12C7770 = 0` — template state pointer 2

3. **Timing**: If debug mode, calls `sub_48AFD0` (print trace timing footer for the fe_wrapup section).

## Error Gating Summary

Each pass has a distinct error-gating pattern. The conditions below are verified against the decompiled `sub_588F90`:

| Pass | Error behavior | Decompiled condition |
|---|---|---|
| Pass 1 (`sub_588C60`) | No gate — always runs. Cleanup operations (template release, exception spec finalization) are safe and necessary even after errors. | None. Unconditional iteration of all secondary TUs followed by primary. |
| Cross-TU (`sub_796C00`) | Skipped entirely if any errors occurred. This prevents cross-TU marking from propagating errors between units. | `if (!qword_126ED90) sub_796C00();` (line 67-68 of decompiled) |
| Pass 2 (`sub_707040`) | Per-TU skip. Inside the TU iteration loop, each TU is independently gated: if errors exist when that TU is selected, it is skipped but subsequent TUs may still run. | `sub_7A3D60(tu); if (!qword_126ED90) sub_707040(*(qword_106BA10 + 8));` (lines 77-84) |
| Pass 3 (`sub_610420`) | Per-TU skip. Same per-TU gating as Pass 2. When a TU is skipped, `dword_106B640` is never set to 1, so the guard flag remains 0. | `sub_7A3D60(tu); if (!qword_126ED90) { dword_106B640 = 1; sub_610420(..., 23); dword_106B640 = 0; }` (lines 97-108) |
| Pass 4 (`sub_5CCA40` etc.) | Per-TU skip. On error for a TU: `dword_126E55C` is cleared to 0, which prevents stages 4b (`sub_5CC410`) and 4c (`sub_5CCBF0`) from running for that TU. Stage 4a (`sub_5CCA40`) is additionally gated by `dword_126EFB4 == 2` (C++ only). | `sub_7A3D60(tu); if (!qword_126ED90) { ... if (dword_126E55C) { sub_5CC410(); sub_5CCBF0(v8); } } else { dword_126E55C = 0; }` (lines 120-137) |
| Pass 5 (`sub_588D40`) | No gate on the per-TU iteration — always runs. However, the internal mirrored 2-3-4 re-run within `sub_588D40` is individually error-gated at each stage. | Unconditional iteration. Internal re-run checks `qword_126ED90` before each of `sub_707040`, `sub_610420`, `sub_5CCA40`. |
| Post-passes | `sub_796BA0` requires `!qword_126ED90 && !dword_106C2B4 && *(qword_106B9F0) != 0`. `sub_5F7DF0` requires `dword_106C250 && !qword_126ED90`. All others run unconditionally. | Line 158: `if (!qword_126ED90 && !dword_106C2B4 && *v4) sub_796BA0();` Line 213: `if (dword_106C250 && !qword_126ED90) sub_5F7DF0(0);` |

## Data Flow Summary

| Input | Description |
|---|---|
| `qword_106B9F0` | TU chain head — linked list of all translation units |
| `*(qword_106BA10 + 8)` | File scope IL root node — the IL tree for each TU |
| `qword_126ED90` | Error flag — nonzero means compilation errors occurred |
| `dword_126EFB4` | Language mode — 2 for C++, gates pass 4 and template operations |
| `dword_106BA08` | Full compilation mode flag — gates Pass 5's mirrored sequence |

| Output | Description |
|---|---|
| Finalized IL tree | Entities marked for keeping preserved; all others eliminated |
| `dword_106B640` | IL emission guard flag — 0 at completion |
| `dword_126E55C` | Deferred class members flag — 0 after processing |
| Closed output files | Three output streams (IDs 1513-1515) flushed and closed |
| Zeroed globals | `qword_106BA10`, `dword_126EE48`, `qword_126DB48`, template state — all cleared |

## Function Map

| Address | Identity | Source file | Role in fe_wrapup |
|---|---|---|---|
| `sub_588F90` | `fe_wrapup` | `fe_wrapup.c:776` | Top-level entry, called from `main()` |
| `sub_588C60` | `file_scope_il_wrapup` | `fe_wrapup.c` | Pass 1: template/exception cleanup, IL walk, IL finalize |
| `sub_588D40` | `file_scope_il_wrapup_part_3` | `fe_wrapup.c:559` | Pass 5: statement finalization, scope assertion, IL/template output |
| `sub_588E90` | `translation_unit_wrapup` | `fe_wrapup.c` | Called from `process_translation_unit`, not directly from fe_wrapup |
| `sub_707040` | `set_needed_flags_at_end_of_file_scope` | `scope_stk.c:8090` | Pass 2: compute needed-flags on all entity lists |
| `sub_610420` | `mark_to_keep_in_il` | `il_walk.c:1959` | Pass 3: mark entities for device code preservation |
| `sub_5CCA40` | `clear_instantiation_required_on_unneeded_entities` | `il.c:29450` | Pass 4a: prevent unnecessary template instantiation |
| `sub_5CC410` | `eliminate_bodies_of_unneeded_functions` | `il.c:29231` | Pass 4b: remove dead function bodies |
| `sub_5CCBF0` | `eliminate_unneeded_il_entries` | `il.c:29598` | Pass 4c: remove dead entities from IL lists |
| `sub_796C00` | `mark_secondary_trans_unit_IL_entities_used_from_primary_as_needed` | `scope_stk.c` | Between Pass 1 and 2: cross-TU reference marking |
| `sub_796BA0` | `copy_secondary_trans_unit_IL_to_primary` | `trans_copy.c:3003` | Post-pass: dead in CUDA build (always asserts) |
| `sub_707480` | scope renumber (inferred) | `scope_stk.c` | Post-pass: renumber scope declarations |
| `sub_765480` | `remove_unneeded_instantiations` | `templates.c:19822` | Post-pass: prune template instantiation list |
| `sub_6B95C0` | `print_memory_management_statistics` | memory mgmt | Post-pass: grand total memory report |
| `sub_5E1D00` | `check_local_constant_use` | `il_alloc.c:1177` | Post-pass: assert no pending local constants |
| `sub_7A3D60` | `set_current_translation_unit` | `trans_unit.c` | Called before every per-TU operation |
| `sub_706710` | IL tree walk | IL subsystem | Pass 1 via `sub_588C60` |
| `sub_706F40` | IL finalize | IL subsystem | Pass 1 via `sub_588C60` |
| `sub_6115E0` | `walk_tree_and_set_keep_in_il` | `il_walk.c` | Pass 3: recursive keep_in_il walker |
| `sub_6170C0` | namespace member walk | `il_walk.c` | Pass 3: using-declaration fixed-point |
| `sub_6175F0` | C++ companion walk | `il_walk.c` | Pass 3: out-of-line definitions |
| `sub_617310` | `prune_keep_in_il_walk` | `il_walk.c` | Pass 3: installed as walk prune callback |
| `sub_5BD350` | destroy temporaries | IL subsystem | Pass 1: C++ temporary cleanup |
| `sub_7C2690` | template cleanup | template engine | Pass 1: release deferred template state |
| `sub_68A0C0` | exception cleanup | exception handling | Pass 1: finalize exception specs |
| `sub_78A9D0` | `template_and_inline_entity_wrapup` | C++ support | Preamble: C++ pre-wrapup |
| `sub_78A380` | clear instantiation-required flag | template engine | Pass 4a via `sub_5CCA40` |
| `sub_5CAB40` | eliminate function body | IL subsystem | Pass 4b via `sub_5CC410`, post-pass via `sub_765480` |
| `sub_5CE710` | eliminate class definition | IL subsystem | Post-pass via `sub_765480` |
| `sub_5CB920` | C++ member cleanup | class subsystem | Pass 4c via `sub_5CCBF0` |
| `sub_5E2D70` | scope deallocation | scope subsystem | Pass 4c via `sub_5CCBF0` |
| `sub_5CC570` | eliminate scope orphaned entries | IL subsystem | Pass 4c via `sub_5CCBF0` |
| `sub_718720` | scope-level cleanup | scope subsystem | Pass 4c via `sub_5CCBF0` |
| `sub_703C30` | get scope summary | scope subsystem | Pass 4c via `sub_5CCBF0` |
| `sub_7B0B60` | walk type tree | type subsystem | Pass 4c: type metadata cleanup |
| `sub_5C71B0` | type cleanup callback | type subsystem | Pass 4c: invoked via `sub_7B0B60` with id 147 |
| `sub_6FE2A0` | renumber scope entries | scope subsystem | Post-pass via `sub_707480` |
| `sub_6FFBA0` | reorganize scope members | scope subsystem | Pass 4b, scope renumbering |
| `sub_6FE8C0` | C++ scope merge | scope subsystem | Pass 2: merge declaration/scope lists |
| `sub_4F7B10` | close output file | file I/O | Post-pass: close 3 files |
| `sub_5F7DF0` | flush IL output | IL output | Post-pass: conditional flush |
| `sub_6B8B20` | process file entry | source file mgr | Post-pass: file index loop |
| `sub_4ED0E0` | declaration cleanup | declarations | State teardown |
| `sub_709250` | IL output | IL output | Pass 5: serialize IL tree |
| `sub_7C2560` | template output | template engine | Pass 5: serialize template info |
| `sub_5BAD30` | statement finalization | statement subsystem | Pass 5: finalize statement-level nodes |
| `sub_5C9E10` | class scope finalization | class subsystem | Pass 5: C++ scope cleanup |
| `sub_6B9580` | source file state update | source file mgr | Pass 5: update file tracking |
| `sub_4F4030` | diagnostic flush | diagnostics | Pass 5: flush pending messages |
| `sub_6B9340` | file scope close | source file mgr | Pass 5: close file scope with error count |
| `sub_702DC0` | scope stack dump | scope subsystem | Post-pass: debug dump |
| `sub_6C6570` | viability dump | overload resolution | Post-pass: debug dump |
| `sub_48AE00` | debug trace enter | debug subsystem | Preamble, Pass 4b/4c |
| `sub_48AFD0` | debug trace exit/timing | debug subsystem | Final: print timing |
| `sub_48A7E0` | debug flag check | debug subsystem | Multiple: check named trace flags |

## Diagnostic Strings

| String | Location | When emitted |
|---|---|---|
| `"fe_wrapup"` | `sub_588F90` preamble | Debug trace at function entry |
| `"bad translation unit in fe_wrapup"` | `sub_588F90` preamble | Fatal assertion when `dword_106BA08 == 0` |
| `"source_file_for_seq_info"` | `sub_588F90` preamble | Debug flag name for source sequence dump |
| `"Start of set_needed_flags_at_end_of_file_scope"` | `sub_707040` entry | Pass 2 debug trace |
| `"End of set_needed_flags_at_end_of_file_scope"` | `sub_707040` exit | Pass 2 debug trace |
| `"needed_flags"` | `sub_707040`, `sub_610420` | Debug flag name for needed-flags diagnostics |
| `"bad scope kind"` | `sub_707040` | Fatal assertion when scope kind is not 0, 3, or 6 |
| `"variable_needed_even_if_unreferenced"` | `sub_707040` | Assertion function name at `scope_stk.c:7999/8001` |
| `"Beginning file scope keep_in_il walk"` | `sub_610420` entry | Pass 3 debug trace |
| `"Ending file scope keep_in_il walk"` | `sub_610420` exit | Pass 3 debug trace |
| `"mark_to_keep_in_il"` | `sub_610420` | Fatal assertion function name at `il_walk.c:1959` |
| `"file_scope_il_wrapup_part_3"` | `sub_588D40` | Assertion function name at `fe_wrapup.c:559` |
| `"clear_instantiation_required_on_unneeded_entities"` | `sub_5CCA40` | Assertion function name at `il.c:29450` |
| `"eliminate_bodies_of_unneeded_functions"` | `sub_5CC410` | Debug trace at level 3 |
| `"eliminate_unneeded_il_entries"` | `sub_5CCBF0` | Debug trace at level 3 |
| `"Removing variable ..."` | `sub_5CCBF0` | Verbose output when removing a variable entity |
| `"Not removing variable ..."` | `sub_5CCBF0` | Verbose output when keeping a variable entity |
| `"Removing routine ..."` | `sub_5CCBF0` | Verbose output when removing a function entity |
| `"Not removing routine ..."` | `sub_5CCBF0` | Verbose output when keeping a function entity |
| `"Removing hidden name entry for ..."` | `sub_5CCBF0` | Verbose output during hidden name cleanup |
| `"check_local_constant_use"` | `sub_5E1D00` | Assertion function name at `il_alloc.c:1177` |
| `"copy_secondary_trans_unit_IL_to_primary"` | `sub_796BA0` | Debug trace + fatal assertion at `trans_copy.c:3003/3008` |
| `"remove_unneeded_instantiations"` | `sub_765480` | Assertion function name at `templates.c:19822/19848` |
| `"scope_stack"` | `sub_588F90` post-pass | Debug flag name for scope stack dump |
| `"viability"` | `sub_588F90` post-pass | Debug flag name for viability analysis dump |
| `"space_used"` | `sub_588F90` post-pass | Debug flag name for memory statistics |
| `"dump_elim"` | `sub_5CCBF0`, `sub_5CC410` | Debug flag name for entity removal details |
| `"Memory management table use:"` | `sub_6B95C0` | Memory statistics report header |
| `"Symbol table use:"` | `sub_74A980` | Symbol table statistics header |
| `"Macro table use:"` | `sub_6B6280` | Macro table statistics header |
| `"Error table use:"` | `sub_4ED970` | Error table statistics header |
| `"Expression table use:"` | `sub_56D8C0` | Expression table statistics header |

## Key Global Variables

| Variable | Address | Role in fe_wrapup |
|---|---|---|
| `qword_106B9F0` | `0x106B9F0` | TU chain head. Iterated by all 5 passes. |
| `qword_106BA10` | `0x106BA10` | Current TU descriptor. Switched by `sub_7A3D60` before each TU. |
| `qword_126ED90` | `0x126ED90` | Error flag. Passes 2-4 skip TUs when nonzero. |
| `dword_126EFB4` | `0x126EFB4` | Language mode. 2 = C++. Gates `sub_5CCA40`, `sub_78A9D0`, template operations. |
| `dword_106BA08` | `0x106BA08` | Full compilation flag. Gates preamble assertion and Pass 5's mirrored sequence. |
| `dword_106B640` | `0x106B640` | IL emission guard. Set=1 during Pass 2 (file scope entry) and Pass 3 (caller). Asserted by `sub_610420`. Cleared=0 at end. |
| `dword_126E55C` | `0x126E55C` | Deferred class members flag. When set, enables stages 4b and 4c. Cleared on error exit. |
| `dword_126C5A0` | `0x126C5A0` | Scope renumbering flag. When set, enables post-pass `sub_707480` double-loop. Cleared after. |
| `dword_126EC78` | `0x126EC78` | Scope count. Controls iteration bounds for `sub_707480` and `sub_5CC410`. |
| `qword_126EB98` | `0x126EB98` | Scope table base. 16-byte entries: `{qword scope_ptr, int file_index, pad}`. |
| `dword_126EC80` | `0x126EC80` | File table entry count. Controls file index processing loop. |
| `qword_126EC88` | `0x126EC88` | File table (name/scope pointers). Indexed by file ID. |
| `qword_126EB90` | `0x126EB90` | File table (info entries). Indexed by file ID. |
| `dword_106C094` | `0x106C094` | Compilation mode. Value 1 skips `sub_765480` (template validation). |
| `dword_106C250` | `0x106C250` | Output flush flag. When set with no errors, calls `sub_5F7DF0(0)`. |
| `dword_106C268` | `0x106C268` | CUDA diagnostics flag. Gates `sub_6B3260` in preamble. |
| `dword_106C2B4` | `0x106C2B4` | Cross-TU copy disabled. When set, skips `sub_796BA0`. |
| `dword_126EFC8` | `0x126EFC8` | Debug/trace mode. Enables trace output and debug dumps throughout. |
| `dword_126EFCC` | `0x126EFCC` | Diagnostic verbosity level. Level > 0 enables memory stats, > 2 enables dump_elim. |
| `dword_106BC80` | `0x106BC80` | Always-report-stats flag. Forces memory statistics regardless of verbosity. |
| `dword_126EE48` | `0x126EE48` | Init-complete flag. Set to 1 during `fe_init_part_1`, cleared to 0 during teardown. |
| `qword_126DB48` | `0x126DB48` | Scope tracking pointer. Cleared during teardown. |
| `qword_12C7768` | `0x12C7768` | Template state pointer 1. Cleared during teardown. |
| `qword_12C7770` | `0x12C7770` | Template state pointer 2. Cleared during teardown. |
| `qword_126E4C0` | `0x126E4C0` | Expected file scope entity. Compared in Pass 5 scope assertion. |
| `qword_126C5E8` | `0x126C5E8` | Scope stack base pointer. Array of 784-byte entries. |
| `dword_126C5E4` | `0x126C5E4` | Current scope stack depth index. |
| `dword_126E204` | `0x126E204` | Template mode flag. Affects instantiation-required clearing in Pass 4a. |
| `qword_126EBA0` | `0x126EBA0` | Deferred entity list head. Walked in Pass 3. |
| `qword_126EBE0` | `0x126EBE0` | Global deferred entity chain. Cleaned in Pass 4c. |
| `qword_12C7740` | `0x12C7740` | Template instantiation pending list. Walked by `sub_765480`. |
| `qword_126DFE0` | `0x126DFE0` | File-index-to-TU mapping table. Used for TU ownership checks. |

## Cross-References

- [Pipeline Overview](./overview.md) — fe_wrapup is stage 6 in the 8-stage pipeline
- [Keep-in-IL](../il/keep-in-il.md) — detailed coverage of the device code selection mechanism (Pass 3)
- [IL Overview](../il/overview.md) — the IL data structures walked by all five passes
- [Backend Code Generation](./backend.md) — stage 7, consumes the finalized IL produced by fe_wrapup
- [Entry Point & Initialization](./entry.md) — the `main()` function that calls `sub_588F90`
- [Frontend Invocation](./frontend.md) — stage 5, builds the IL tree that fe_wrapup finalizes
- [Timing & Exit](./timing-exit.md) — fe_wrapup completion marks the end of "Front end time"
- [Device/Host Separation](../cuda/device-host-separation.md) — the keep_in_il mechanism's relationship to device code isolation
