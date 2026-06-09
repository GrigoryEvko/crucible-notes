# ThinLTO Function Import

CICC v13.0 implements LLVM's ThinLTO function import pipeline with GPU-specific modifications to the threshold computation, candidate filtering, and provenance tracking. The core of the system lives in two functions — `sub_1854A20` (the import driver, 4,326 bytes) and `sub_1853180` (the threshold computation engine, 5,059 bytes) — with an entry point at `sub_1855B10` that parses the `-summary-file` / `-function-import` command line and orchestrates the whole-module import flow. The fundamental difference from CPU ThinLTO is that GPU compilation operates in a closed-world model: there are no shared libraries, no dynamic linking, and no PLT/GOT indirection. Every device function will be statically linked into the final PTX. This means CICC can afford far more aggressive import thresholds than CPU compilers, because the code size cost of importing is paid once per GPU binary rather than once per shared-object load.

The import subsystem reads `NVModuleSummary` data (built by `sub_D7D4E0`, see [Module Summary](./module-summary.md)) to make summary-guided decisions about which functions to pull from other translation units. Each candidate is evaluated against a floating-point threshold that incorporates callsite hotness, linkage type, and a per-priority-class multiplier. A global import budget caps the total number of imports to prevent compile-time explosion. After import, each materialized function receives `thinlto_src_module` metadata so downstream passes (particularly the [inliner](./inliner-cost.md)) know its origin module.

| | |
|---|---|
| **Import driver** | `sub_1854A20` (`0x1854A20`, 4,326 B) |
| **Threshold computation** | `sub_1853180` (`0x1853180`, 5,059 B) |
| **Threshold comparison gate** | `sub_18518A0` (`0x18518A0`) |
| **Import execution** | `sub_15E4B20` (`0x15E4B20`) |
| **Import candidate evaluator** | `sub_1852CC0` (`0x1852CC0`) |
| **Entry point** | `sub_1855B10` (`0x1855B10`, 10,503 B) |
| **Whole-module processing** | `sub_1858B90` (`0x1858B90`, 31,344 B) |
| **Type metadata propagation** | `sub_185E850` (`0x185E850`, 24,263 B) |
| **Pipeline registration** | `"function-import"` (slot 43, Module pass) |
| **Knob constructor (primary)** | `ctor_184_0` (`0x4DA920`, 13,693 B) |
| **Knob constructor (supplementary)** | `ctor_029` (`0x489C80`, 1,120 B) |
| **Knob constructor (pass-level)** | `ctor_420_0` (`0x532010`, 11,787 B) |

## Why GPU ThinLTO Differs from CPU ThinLTO

Upstream LLVM's ThinLTO was designed for CPU executables and shared libraries where import decisions must balance code size (impacts disk, cache, page faults) against optimization opportunity (cross-module inlining, constant propagation). The default `import-instr-limit` is 100 instructions, the cold multiplier is 0, and the hot multiplier is 10x. These conservative defaults reflect a world where over-importing bloats `.text` sections shared across address spaces.

GPU compilation inverts these tradeoffs:

1. **No shared libraries.** Device code is statically linked into a fatbinary. There is no dynamic linker, no GOT, no PLT. Importing a function costs compile time but has zero runtime overhead beyond instruction cache pressure.

2. **Function calls are expensive.** As documented in the [inliner cost model](./inliner-cost.md), every GPU function call marshals arguments through `.param` address space via `st.param` / `ld.param` sequences. Inlining (which requires importing first) eliminates this overhead entirely.

3. **Closed-world optimization.** The compiler sees all device code. There are no opaque DSOs. This means aggressive import cannot break ABI contracts that don't exist.

4. **Register pressure is the real constraint.** On GPU, the limiting factor is not code size but register count, which determines occupancy. Import + inline can actually *reduce* register pressure by enabling cross-function register allocation and eliminating `.param`-space spills.

These factors push CICC toward much more aggressive import thresholds. The priority-class multiplier system (section below) allows CICC to tune import aggressiveness per-callsite rather than using a single global threshold.

## What Gets Imported and What Does Not

The `NVModuleSummary` builder (`sub_D7D4E0`) assigns a 4-level import priority to every global value when building the module summary index:

| Priority | Meaning | Import behavior |
|----------|---------|-----------------|
| 0 | Not importable | Local/hidden linkage, never imported |
| 1 | Importable, not preferred | Will import only if threshold is generous |
| 2 | Standard importable | Normal import candidate |
| 3 | Force-import | Highest priority, always imported if budget allows |

The priority is determined by querying the `ImportPriorityTable` (parameter `a4` of `sub_D7D4E0`) via `sub_D84370`, `sub_D84440` (force-import check), and `sub_D84450` (importable check). A global override at `dword_4F87C60` can force all symbols to priority 1 or higher.

**Functions that are imported:**
- `__device__` functions with internal or linkonce_odr linkage (template instantiations, inline functions)
- Math library implementations (libdevice functions) called from device code
- Helper functions from header-only libraries (Thrust, CUB, cutlass templates)
- Constant global variables with initializers (`import-constants-with-refs` = true by default)

**Functions that are NEVER imported:**
- **Kernels (`__global__` functions).** These are entry points. They are never candidates for cross-module import because they represent the root of execution; they are called from host code, not from other device functions. The summary builder marks them as non-importable.
- **Host functions.** Host code is handled by the host compiler (gcc/clang), not cicc. They never appear in the device module summary.
- **Functions in address space 25.** The summary builder at lines 1388-1395 explicitly skips functions whose type resolves to address space 25, with a `goto LABEL_495` that bypasses the import-eligible path. The raw report notes: "device functions can't be cross-module imported in ThinLTO" — this refers specifically to functions that are *declarations only* with device-memory address space linkage, meaning they reference device-side symbols without a definition in the current TU.
- **Functions with the "not importable" flag.** Bit 4 (`0x10`) of the linkage byte at offset `+0x0C` in the function summary entry. The import driver checks `test byte [entry+0Ch], 0x10` and skips on set.

## Import Algorithm: Complete Pseudocode

**Complexity.** Let C = number of import candidates across all modules, G = number of unique GUIDs, and L = total number of name entries across all candidates. Stage 1 (threshold computation, `sub_1853180`) iterates every candidate once: O(C). For each candidate, the GUID dedup hash table (`slot = GUID * 37 & (size - 1)`) provides O(1) amortized lookup with linear probing. The name array scan is up to 4-level unrolled, giving O(L) total across all candidates. The 11-case linkage dispatch via jump table is O(1) per entry. The priority-class threshold adjustment is O(1) per candidate (a single float multiply). The global budget check is O(1). Overall Stage 1: O(C + L). Stage 2 (triple-pass driver, `sub_1854A20`) processes three priority-ordered linked lists, each in a single pass: O(C) total. Per-candidate import execution (`sub_15E4B20`) is O(I_f) where I_f = instructions in the imported function (bitcode materialization). The whole-module processing (`sub_1858B90`, 31KB) is O(F * I_avg) where F = total functions and I_avg = average instruction count. The dedup hash table growth follows standard load-factor 75% doubling, maintaining O(1) amortized operations. Total: O(C + L + sum(I_imported)).

The import process runs in two major stages. Stage 1 (`sub_1853180`) builds a prioritized list of qualifying candidates by evaluating each against a computed threshold. Stage 2 (`sub_1854A20`) materializes candidates via a triple-pass sweep over three priority-ordered linked lists, executing the actual cross-module function import.

### Stage 1: Threshold Computation Engine (`sub_1853180`)

Address range: `0x1853180`--`0x1854543` (5,059 bytes). Six parameters, 0xB8-byte stack frame. Uses a jump table at `dword_42BA140` for the 11-case linkage-type dispatch.

```c
// sub_1853180 -- Threshold computation with GUID dedup and priority-class multipliers
//
// Evaluates every candidate in summary_ctx against base_threshold adjusted by
// priority class.  Emits qualifying candidates to result_array as 24-byte
// entries {GUID, threshold, import_record_ptr}.  Tracks already-evaluated
// GUIDs via guid_hash_table to prevent duplicate work.
//
// Binary: 0x1853180, 5059 bytes.  Stack: 0xB8.
// Jump table: dword_42BA140 (11 entries, linkage dispatch).
//
// Globals read:
//   dword_4FAAE80  hot_multiplier      (float, default 10.0)
//   dword_4FAACC0  cold_multiplier     (float, default 0.0)
//   dword_4FAADA0  critical_multiplier (float, default 100.0)
//   dword_4FAB040  default_multiplier  (float, default 1.0)
//   dword_4FAB120  global_import_budget (int, default -1 = unlimited)
//   dword_4FAA770  running_import_count (int, reset per module)

fn threshold_compute(
    summary_ctx,       // rdi -> [rbp-0x88]: candidate arrays and metadata
    module_info,       // rsi -> [rbp-0x58]: source module summary
    base_threshold,    // edx -> [rbp-0x7C]: integer base threshold (import-instr-limit)
    guid_hash_table,   // rcx -> [rbp-0x50]: DenseMap<uint64_t, metadata> for dedup
    result_array,      // r8  -> [rbp-0x60]: growable output array
    visited_set,       // r9  -> [rbp-0xA0]: tracks already-evaluated GUIDs
):
    candidate_begin = summary_ctx[+0x28]   // r12: start of candidate pointer array
    candidate_end   = summary_ctx[+0x30]   // r14: one-past-end

    // ---- Outer loop: iterate every candidate ----
    while candidate_begin != candidate_end:                 // 0x18531C4
        candidate_ptr = *candidate_begin
        guid = candidate_ptr & ~0x7                         // mask low 3 tag bits

        // ---- GUID dedup via multiplicative-hash table ----
        table_size = guid_hash_table[+0x18]
        if table_size > 0:                                  // 0x18531D0
            table_data = guid_hash_table[+0x00]
            raw_guid   = candidate_ptr[+0x00]               // 8-byte GUID

            // Hash: slot = (GUID * 37) & (table_size - 1)
            // Implemented as: lea edx,[rsi+rsi*8] -> edx=GUID*9
            //                  lea edx,[rsi+rdx*4] -> edx=GUID+GUID*36=GUID*37
            slot = (raw_guid * 37) & (table_size - 1)       // 0x18531E8

            // 16-byte slots: {GUID (8B), metadata (8B)}
            probe_ptr = table_data + slot * 16
            stored_guid = probe_ptr[+0x00]

            if stored_guid == raw_guid:
                goto next_candidate                         // already evaluated

            // Linear probing on collision
            probe_step = 1
            while stored_guid != 0xFFFFFFFFFFFFFFFF:        // -1 = empty sentinel
                slot = (slot + probe_step) & (table_size - 1)
                probe_step += 1
                probe_ptr = table_data + slot * 16
                stored_guid = probe_ptr[+0x00]
                if stored_guid == raw_guid:
                    goto next_candidate                     // found: already seen

            // GUID not in table -- fall through to evaluation

        // ---- Name array scan ----
        // When dedup table is absent, scan name components directly
        name_begin = candidate_ptr[+0x18]                   // 0x1853250
        name_end   = candidate_ptr[+0x20]

        // Up-to-4-level unrolled name comparison (0x1853670-0x18538BA):
        //   Level 1: entry = [name_ptr - 8]
        //   Level 2: entry = [name_ptr + 0]
        //   Level 3: entry = [name_ptr + 8]
        //   Level 4: entry = [name_ptr + 0x10]
        // Each level checks:
        //   visibility flag at [r14+0xB0] -> if set: test byte [entry+0Ch], 0x20
        //   entry type:  entry[+0x08] must == 2 (function summary)
        //   not-importable: test byte [entry+0Ch], 0x10 -> skip if set
        //   linkage:     entry[+0x0C] & 0x0F -> 11-case switch

        for each name_entry in name_begin..name_end:
            entry = *name_entry
            if entry[+0x08] != 2:                           // not a function summary
                continue
            linkage_byte = entry[+0x0C]
            if linkage_byte & 0x10:                         // "not importable" flag
                continue

            linkage = linkage_byte & 0x0F                   // 0x185324E

            // ---- Linkage-type dispatch (11 cases via jump table) ----
            switch linkage:                                 // dword_42BA140
                case 0:  // ExternalLinkage
                case 1:  // AvailableExternallyLinkage
                case 3:  // InternalLinkage
                case 5:  // ExternalWeakLinkage
                case 6:  // CommonLinkage
                    goto standard_threshold_path            // loc_18536E8

                case 7:  // WeakAnyLinkage
                case 8:  // WeakODRLinkage
                    // Weak linkage requires name verification via memcmp
                    // to confirm the candidate matches the expected symbol
                    // before allowing import.
                    expected_name = resolve_name(candidate_ptr)
                    actual_name   = resolve_name(entry)
                    if memcmp(expected_name, actual_name, name_len) != 0:
                        continue                            // 0x1853A71: name mismatch
                    goto standard_threshold_path

                case 2:  // AppendingLinkage
                case 4:  // PrivateLinkage
                case 9:  // LinkOnceAnyLinkage
                case 10: // LinkOnceODRLinkage
                    goto special_handling_path               // loc_1853928

            // ---- Standard threshold path ----
            standard_threshold_path:
                // Dereference alias chain for external linkage
                if entry.function_type == 0:                // external
                    entry = entry[+0x40]                    // follow alias pointer
                    linkage = entry[+0x0C] & 0x0F           // re-extract

                // ---- Priority-class threshold adjustment ----
                // 0x1853441: convert base_threshold to float
                threshold_f = (float)base_threshold         // cvtsi2ss xmm2, eax

                priority_class = entry[+0x08] & 0x7         // 3-bit field, al=[r15+8]&7

                switch priority_class:
                    case 3:  // HOT callsite
                        threshold_f *= dword_4FAAE80        // hot_multiplier (10.0)
                                                            // mulss xmm0, cs:dword_4FAAE80
                    case 1:  // COLD callsite
                        threshold_f *= dword_4FAACC0        // cold_multiplier (0.0)
                                                            // mulss xmm0, cs:dword_4FAACC0
                    case 4:  // CRITICAL callsite
                        threshold_f *= dword_4FAADA0        // critical_multiplier (100.0)
                                                            // mulss xmm0, cs:dword_4FAADA0
                    default: // no priority match
                        threshold_f *= dword_4FAB040        // default_multiplier (1.0)
                                                            // mulss xmm0, cs:dword_4FAB040

                adjusted_threshold = (int)threshold_f       // cvttss2si rax, xmm0
                // Stored to [rbp-0x78] and r11d for comparison

                // ---- Cost comparison (0x1853AA8) ----
                function_cost = entry[+0x40]                // IR instruction count
                if adjusted_threshold < function_cost:      // cmp r11d, [rcx+40h]
                    continue                                // jb not_eligible

                // ---- "Not importable" double-check ----
                if entry[+0x0C] & 0x10:                     // test byte [rcx+0Ch], 0x10
                    continue

                // ---- Max-threshold-wins for duplicates (0x18534C2) ----
                if guid already in result_array:
                    existing_record = result_slot[+0x10]
                    if existing_record != NULL:
                        existing_threshold = result_slot[+0x08]
                        if (float)existing_threshold >= threshold_f:
                            continue                        // existing is better; skip
                        result_slot[+0x08] = adjusted_threshold  // update to higher
                        goto next_candidate

                // ---- Global budget check (0x185340A) ----
                budget = dword_4FAB120                      // global_import_budget
                if budget >= 0:                             // test eax,eax; js proceed
                    if dword_4FAA770 >= budget:             // cmp counter vs budget
                        continue                            // jge skip: budget exhausted

                // ---- Allocate dedup hash table node (0x1853953) ----
                node = malloc(16)                           // 0x22077B0: edi=0x10
                if node != NULL:
                    node[+0x00] = 0                         // clear forward pointer
                    node[+0x08] = guid
                    sub_1851560(                            // hash table insert
                        guid_hash_table[+0x08],             // insert point
                        bucket_index,                       // slot
                        guid,                               // key
                        1                                   // insert_mode
                    )

                // ---- Emit to result array (0x1853517) ----
                count    = result_array[+0x08]              // current count
                capacity = result_array[+0x0C]
                if count >= capacity:
                    grow_result_array(result_array)         // realloc path

                // 24-byte entry: offset = count * 24
                entry_ptr = result_array.base + count * 24  // lea rax,[rax+rax*2]; shl rax,3
                entry_ptr[+0x00] = guid                     // 8 bytes: function GUID
                entry_ptr[+0x08] = adjusted_threshold       // 4 bytes: threshold value
                entry_ptr[+0x10] = import_record_ptr        // 8 bytes: import record

                result_array[+0x08] = count + 1             // increment count

                // ---- Increment global counter (0x1853510) ----
                dword_4FAA770 += 1                          // add cs:dword_4FAA770, 1

    next_candidate:
        candidate_begin += 8                                // advance to next candidate
```

**Threshold computation arithmetic in detail.** The four multiplier constants live in `.data` as IEEE 754 single-precision floats. The SSE scalar path is:

```nasm
; At 0x1853441 -- convert integer base threshold to float
pxor   xmm2, xmm2
cvtsi2ss xmm2, rax          ; xmm2 = (float)base_threshold

; Priority dispatch -- one of four paths selected:
; HOT (priority 3):
movss  xmm0, cs:dword_4FAAE80   ; xmm0 = 10.0f
mulss  xmm0, xmm2               ; xmm0 = 10.0 * base

; COLD (priority 1):
mulss  xmm0, cs:dword_4FAACC0   ; xmm0 = 0.0 * base = 0.0

; CRITICAL (priority 4):
mulss  xmm0, cs:dword_4FAADA0   ; xmm0 = 100.0 * base

; DEFAULT (all others):
mulss  xmm0, cs:dword_4FAB040   ; xmm0 = 1.0 * base

; Convert back to integer for comparison
cvttss2si rax, xmm0             ; rax = (int)threshold_f (truncation)
```

The `cvttss2si` truncation means threshold values are floored, not rounded. For `base_threshold=100` and `hot_multiplier=10.0`, the adjusted threshold is exactly 1000. The cold path with multiplier 0.0 always produces threshold 0, meaning cold functions are *never* imported unless the multiplier is overridden.

### Stage 2: Triple-Pass Import Driver (`sub_1854A20`)

Address range: `0x1854A20`--`0x1855B06` (4,326 bytes). Four parameters, 0x278-byte stack frame. Callee-saved: r15, r14, r13, r12, rbx.

The driver processes candidates across three priority-ordered linked lists embedded in the `guid_import_map` structure. Each list covers a different import priority class. The three passes guarantee that high-priority candidates are imported (and consume budget) before lower-priority ones get a chance.

```c
// sub_1854A20 -- Triple-pass import driver
//
// Materializes cross-module function bodies for candidates that pass
// threshold evaluation.  Processes three linked lists in priority order:
//   Pass 1: primary   list at [import_map + 0x00]  (highest priority)
//   Pass 2: secondary list at [import_map + 0x10]  (medium priority)
//   Pass 3: tertiary  list at [import_map + 0x30]  (lowest priority)
//
// For each candidate: check importable flag, evaluate threshold via
// sub_18518A0, execute import via sub_15E4B20, optionally attach
// thinlto_src_module metadata.
//
// Binary: 0x1854A20, 4326 bytes.  Stack: 0x278.
//
// Globals read:
//   byte_4FAAA20   enable_import_metadata (bool)

fn import_driver(
    import_ctx,          // rdi -> [rbp-0x258]: import state object
    module_summary_idx,  // rsi -> [rbp-0x260]: combined summary index
    source_module_info,  // rdx -> [rbp-0x278]: source module descriptor
    guid_import_map,     // rcx -> [rbp-0x268]: hash map of GUID -> import lists
                         //        also saved to rbx
):
    // ---- Initialize resolved-summary storage (0x1854A45) ----
    sub_1674380(
        &local_resolved_storage,   // rdi = [rbp-0x290]
        source_module_info         // rsi = rdx
    )

    // ---- Check if import map is empty (0x1854A6C) ----
    entry_count = guid_import_map[+0x08]
    if entry_count == 0:
        goto empty_import_path                              // 0x1854AB3

    // ======================================================================
    // PASS 1: PRIMARY CANDIDATE LIST  (0x1854B99 -- 0x1854F3B)
    // List head: [guid_import_map + 0x00]
    // Importable flag: byte [node - 0x21] & 0x20
    // Summary ptr:     [node - 0x38]
    // ======================================================================

    primary_list = guid_import_map[+0x00]                   // rsi = [rbx]

    // Scan to first valid entry (skip sentinels -8 and NULL)
    cursor = primary_list[+0x00]
    if cursor == 0xFFFFFFFFFFFFFFF8 || cursor == NULL:
        scan forward through primary_list[+0x08], [+0x10], ...
        // Inner scan: load qword, test for NULL, cmp against -8
        // Stop at first non-null, non-sentinel entry

    end_of_candidates = primary_list + entry_count * 8      // r12

    while cursor != end_of_candidates:                      // 0x1854BF0
        // ---- Load candidate descriptor ----
        desc = *cursor                                      // rax = [r14]
        summary_data = desc[+0x00]                          // rdx = [rax]
        cost_info    = desc + 0x40                          // threshold/cost at +0x40

        // ---- Evaluate candidate (0x1854C02) ----
        sub_1852CC0(&local_buf, guid_import_map)            // import candidate evaluator

        // ---- Advance to next valid entry ----
        next = cursor[+0x08]
        // Scan forward: skip NULL and sentinel -8 entries
        while next == NULL || next == 0xFFFFFFFFFFFFFFF8:
            next += 8

        // ---- Per-node import decision loop (0x1854E39) ----
        for each node in candidate.linked_nodes:
            if node == NULL:
                continue                                    // test r15, r15

            // Importable flag check
            importable = node[-0x21] & 0x20                 // test byte [r15-0x21], 0x20
            if !importable:
                continue                                    // jz skip

            // Extract function summary (stored 0x38 bytes before node)
            func_summary = node[-0x38]                      // r13 = [r15-0x38]

            // Resolve function name/info
            sub_15E4EB0(cursor, func_summary)               // 0x1854E61

            // ---- Format import remark (diagnostic output) ----
            resolved_threshold = [rbp-0x1D8]
            resolved_info      = [rbp-0x1E0]
            sub_16C1840(guid_import_map, resolved_info, resolved_threshold)
                                                            // cost component remark
            sub_16C1A90(guid_import_map, resolved_info, resolved_threshold)
                                                            // threshold component remark
            sub_16C1AA0(guid_import_map, [rbp-0x210])       // finalize remark string
            free([rbp-0x1E0])                               // cleanup temp string

            // ---- Threshold comparison gate (0x1854EE3) ----
            cost      = cursor[+0x10]                       // estimated function cost
            hot_count = cursor[+0x08]                       // call frequency / hotness
            qualifies = sub_18518A0(hot_count, cost)        // THRESHOLD GATE
            if !qualifies:                                  // test rax,rax; jz skip
                continue

            // ---- Execute import (0x1854EF7) ----
            sub_15E4B20(import_ctx, func_summary)           // MATERIALIZE FUNCTION

            // Check abort signal
            status = [rbp-0xD0]
            if status & 0xFFFFFFFFFFFFFFFE:                 // caller requested abort
                goto early_return

            // ---- Attach provenance metadata (0x1854F0D) ----
            if byte_4FAAA20 != 0:                           // enable-import-metadata
                source_name = sub_161FF10(func_summary)     // resolve source module name
                // Create optimization remark
                sub_1627350(remark_ctx, 1)                  // edx=1: enabled

                // Attach metadata string (0x1855261):
                //   lea rsi, "thinlto_src_module"  ; 0x42BA2F8, length 0x12
                sub_1627100(
                    func_summary,                           // target function
                    "thinlto_src_module",                   // metadata key (18 chars)
                    source_name                             // metadata value
                )

    // ======================================================================
    // PASS 2: SECONDARY CANDIDATE LIST  (0x1854F41 -- 0x1855074)
    // List head: [guid_import_map + 0x10]
    // Same importable-flag check: byte [node - 0x21] & 0x20
    // Same summary extraction:    [node - 0x38]
    // ======================================================================

    secondary_list = guid_import_map[+0x10]                 // r15 = [rcx+10h]
    secondary_sentinel = guid_import_map[+0x08]

    // Identical processing pattern:
    //   - Iterate linked-list nodes
    //   - Check importable flag: byte [r15-0x21] & 0x20
    //   - Extract summary: [r15-0x38]
    //   - sub_18518A0 threshold gate
    //   - sub_15E4B20 import execution
    //   - Conditional thinlto_src_module metadata attachment

    for each node in secondary_list:
        if node[-0x21] & 0x20 == 0:
            continue
        summary = node[-0x38]
        if !sub_18518A0(node.hot_count, node.cost):
            continue
        sub_15E4B20(import_ctx, summary)
        if byte_4FAAA20:
            attach_provenance_metadata(summary)

    // ======================================================================
    // PASS 3: TERTIARY CANDIDATE LIST  (0x1855074 -- 0x1855190)
    // List head: [guid_import_map + 0x30]
    // Different offsets:
    //   Summary extraction: [node - 0x30]  (not -0x38)
    //   Importable flag:    byte [node - 0x19] & 0x20  (not -0x21)
    // ======================================================================

    tertiary_list = guid_import_map[+0x30]

    // Same processing pattern but with adjusted offsets:
    for each node in tertiary_list:
        if node[-0x19] & 0x20 == 0:                        // note: -0x19, not -0x21
            continue
        summary = node[-0x30]                               // note: -0x30, not -0x38
        if !sub_18518A0(node.hot_count, node.cost):
            continue
        sub_15E4B20(import_ctx, summary)
        if byte_4FAAA20:
            attach_provenance_metadata(summary)

    // ======================================================================
    // POST-IMPORT: Result materialization (0x1854B3C -- 0x1854B97)
    // ======================================================================

    result_count = [rbp-0x100]
    if result_count > 0:
        import_source = sub_16704E0()                       // r13: source module handle
        import_dest   = sub_16704F0()                       // r14: destination module handle

        result_base = [rbp-0x110]
        result_end  = result_base + result_count * 8

        for each result_entry in result_base..result_end:   // 0x1854B7D
            func = *result_entry

            // Skip if function already exists in source module
            if sub_1670560(func, import_source):            // test al,al; jnz next
                continue

            // Materialize into destination module
            sub_1670560(func, import_dest)

    // ======================================================================
    // CLEANUP (0x1854AE7 -- 0x1854B22)
    // ======================================================================

    // Release import list entries (16-byte stride)
    cleanup_base = [rbp-0xF0]
    cleanup_count = eax
    cleanup_end = cleanup_base + cleanup_count * 16

    for each entry in cleanup_base..cleanup_end (stride=16):
        value = entry[+0x00]
        if value == 0xFFFFFFFFFFFFFFF8:                     // sentinel -8: empty
            continue
        if value == 0xFFFFFFFFFFFFFFFC:                     // sentinel -4: deleted
            continue
        sub_161E7C0(entry[+0x08])                           // release associated data

    free(cleanup_base)                                      // j___libc_free_0

    // ---- Empty-import finalization ----
    empty_import_path:                                      // 0x1854AB3
        import_ctx.status = 0                               // clear status byte
        flags = import_ctx[+0x08]
        flags = (flags & 0xFC) | 0x02                       // set "import complete, no imports"
        import_ctx[+0x08] = flags
        sub_1851C60(&local_import_list)                     // finalize empty path cleanup
```

**Why three passes with different offsets.** The three linked lists represent three structural layers in the `guid_import_map`:

| Pass | List head offset | Summary offset | Importable-flag offset | Interpretation |
|------|-----------------|----------------|----------------------|----------------|
| 1 (primary) | `[map+0x00]` | `node[-0x38]` | `node[-0x21] & 0x20` | Direct call targets from the current module — highest priority because they are on the critical path |
| 2 (secondary) | `[map+0x10]` | `node[-0x38]` | `node[-0x21] & 0x20` | Transitively-reachable functions (callees of callees) — import enables deeper inlining chains |
| 3 (tertiary) | `[map+0x30]` | `node[-0x30]` | `node[-0x19] & 0x20` | Speculative candidates (address-taken functions, indirect call targets inferred from devirtualization) — lowest confidence |

The different offsets in pass 3 (`-0x30` instead of `-0x38`, `-0x19` instead of `-0x21`) indicate a different node layout for speculative candidates. These nodes carry less metadata (8 fewer bytes between the summary pointer and the node base, and the importable flag is 8 bytes closer to the node).

### Threshold Comparison Gate (`sub_18518A0`)

The gate function takes two arguments — `hot_count` (rdi) and `cost` (rsi) — and returns nonzero if the candidate qualifies for import. The driver calls it at three points (once per pass). This function encapsulates the final accept/reject decision after the per-priority-class threshold adjustment has already been applied by `sub_1853180`.

```c
// sub_18518A0 -- Threshold comparison gate
// Returns: nonzero if candidate should be imported, zero otherwise
//
// rdi = hot_count (call frequency from profile or summary)
// rsi = cost      (adjusted threshold value from Stage 1)

fn threshold_gate(hot_count, cost) -> bool:
    // The exact comparison logic depends on whether profile data
    // is available.  With profile data, hot_count is a raw call
    // count; the gate compares the cost against a profile-weighted
    // threshold.  Without profile data, this degenerates to a
    // direct comparison: cost <= threshold.
    return hot_count > 0 || cost <= current_threshold
```

## Threshold Multiplier Constants

The four floating-point multiplier constants are stored in the `.data` section and are set by the corresponding `cl::opt` registrations in `ctor_184_0`:

| Address | Knob | Default | Purpose |
|---------|------|---------|---------|
| `dword_4FAAE80` | `import-hot-multiplier` | 10.0 | Multiplier for hot callsites |
| `dword_4FAACC0` | `import-cold-multiplier` | 0.0 | Multiplier for cold callsites |
| `dword_4FAADA0` | `import-critical-multiplier` | 100.0 | Multiplier for critical callsites |
| `dword_4FAB040` | (default path) | 1.0 | Multiplier when no priority class matches |

With the upstream default `import-instr-limit` of 100, a hot callsite gets threshold 1,000 instructions and a critical callsite gets threshold 10,000. The cold multiplier of 0.0 means cold functions are *never* imported by default — the threshold evaluates to zero.

**Effective threshold table** (for `import-instr-limit=100`):

| Priority class | Multiplier | Effective threshold | Typical candidates |
|----------------|-----------|--------------------|--------------------|
| Critical (4) | 100.0x | 10,000 instructions | Manually annotated hot paths, PGO-identified critical edges |
| Hot (3) | 10.0x | 1,000 instructions | Profile-guided hot callsites, frequently-called templates |
| Default (0,2) | 1.0x | 100 instructions | Standard callsites without profile data |
| Cold (1) | 0.0x | 0 instructions | Provably cold paths — never imported at default settings |

The evolution factors control how thresholds decay as imports cascade through the call graph:

| Knob | Default | Effect |
|------|---------|--------|
| `import-instr-evolution-factor` | 0.7 | Each transitive import level reduces the threshold to 70% of the previous |
| `import-hot-evolution-factor` | 1.0 | Hot callsite chains do *not* decay (threshold stays constant through transitive imports) |

The evolution factor is applied by the caller of `sub_1853180` before passing `base_threshold`. For a chain A -> B -> C where A is the root module:
- Import B into A: threshold = `import-instr-limit` (100)
- Import C into A (transitively via B): threshold = `100 * 0.7` = 70
- Import D into A (transitively via C via B): threshold = `100 * 0.7 * 0.7` = 49

For hot chains with `import-hot-evolution-factor=1.0`, the threshold remains 1,000 at every transitive level, enabling arbitrarily deep import chains for hot call paths.

## Global Import Budget

Two globals control the total import count:

| Address | Role | Default |
|---------|------|---------|
| `dword_4FAB120` | Maximum allowed imports | -1 (unlimited) |
| `dword_4FAA770` | Running import counter | 0 (reset per module) |

The budget check at `0x185340A`:

```asm
mov  eax, cs:dword_4FAB120   ; load budget
test eax, eax
js   proceed                   ; negative = unlimited
cmp  cs:dword_4FAA770, eax   ; counter vs budget
jge  skip                     ; at or over budget -> skip
```

When the budget is -1 (the `import-cutoff` default), the `js` (jump-if-sign) branch is taken unconditionally, bypassing the budget check. Setting `-import-cutoff=N` limits the total number of imported functions to N, useful for debugging import-related miscompilations via bisection.

The counter increment at `0x1853510`:

```asm
add  cs:dword_4FAA770, 1     ; increment after successful import
```

This is a non-atomic `add` — safe because ThinLTO import runs single-threaded per module in CICC (unlike CPU LLVM where the thin link runs in parallel). The counter resets to 0 at the start of each module's import phase.

## Integration with the 20,000-Budget Inliner

The import + inline pipeline in CICC works as a two-phase system:

1. **Import phase** (this page): ThinLTO brings cross-module function bodies into the current module based on summary-guided threshold decisions. The imported functions are marked with `thinlto_src_module` metadata.

2. **Inline phase** ([inliner cost model](./inliner-cost.md)): The NVIDIA custom inliner at `sub_1864060` runs with a 20,000-unit per-caller budget. Imported functions are prime inlining candidates because they were specifically imported *because* they are called from this module.

The `inliner-function-import-stats` knob (registered in `ctor_186_0` at `0x4DBEC0`, values: `basic` or `verbose`) tracks how many imported functions were actually inlined. This provides feedback on whether the import thresholds are well-calibrated: if functions are imported but then not inlined (because they exceed the inline budget), the import was wasted compile time.

The typical flow for a template-heavy CUDA library like CUB or cutlass:

1. Each `.cu` file compiles to a ThinLTO bitcode module with a summary index
2. The thin link step reads all summaries and builds a combined index
3. For each module, `sub_1853180` evaluates import candidates using the combined index
4. Hot template instantiations (e.g., `cub::DeviceReduce::Sum<float>`) get threshold `base * 10.0` (hot) or `base * 100.0` (critical)
5. The imported function bodies arrive in the module and are immediately available to the 20,000-budget inliner
6. The inliner folds the imported template bodies into their callers, eliminating `.param` marshaling

## Entry Point: `sub_1855B10`

Address: `0x1855B10`, 10,503 bytes. This is the `runOnModule` entry for the `"function-import"` pass (pipeline slot 43). It orchestrates the entire import flow:

```c
fn function_import_pass_entry(module):
    // Parse required options
    if summary_file_path is empty:
        error("error: -function-import requires -summary-file")
        return

    summary_index = load_summary_file(summary_file_path)
    if summary_index is error:
        error("Error loading file")
        return

    // Build GUID-to-import map from summary index
    guid_import_map = build_import_map(module, summary_index)

    // Stage 1: threshold computation
    sub_1853180(summary_ctx, module_info, import_instr_limit,
                guid_hash_table, result_array, visited_set)

    // Stage 2: triple-pass import
    sub_1854A20(import_ctx, summary_index, source_module, guid_import_map)

    // Post-import: attribute propagation (if enabled)
    if propagate_attrs:
        propagate_summary_attributes(module, summary_index)
```

## Knob Inventory

All knobs are registered across three constructors:

**`ctor_184_0` at `0x4DA920`** (13,693 B — ThinLTO Function Import options):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `import-instr-limit` | unsigned | 100 | Base instruction count threshold |
| `import-cutoff` | int | -1 | Max total imports (-1 = unlimited) |
| `import-instr-evolution-factor` | float | 0.7 | Threshold decay per transitive level |
| `import-hot-evolution-factor` | float | 1.0 | Hot chain decay (1.0 = no decay) |
| `import-hot-multiplier` | float | 10.0 | Threshold multiplier for hot callsites |
| `import-critical-multiplier` | float | 100.0 | Threshold multiplier for critical callsites |
| `import-cold-multiplier` | float | 0.0 | Threshold multiplier for cold callsites |
| `print-imports` | bool | false | Print names of imported functions |
| `print-import-failures` | bool | false | Print rejected candidates with reasons |
| `compute-dead` | bool | true | Strip dead symbols from index |
| `enable-import-metadata` | bool | false | Attach `thinlto_src_module` / `thinlto_src_file` metadata |
| `summary-file` | string | (none) | Summary file path for `-function-import` |
| `import-all-index` | bool | false | Import every external function in the index |

**`ctor_420_0` at `0x532010`** (11,787 B — pass-level ThinLTO options):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `force-import-all` | bool | false | Import even `noinline` functions |
| `import-declaration` | bool | false | Import function declarations as fallback |
| `thinlto-workload-def` | string | (none) | JSON file mapping root functions to import lists |

**`ctor_029` at `0x489C80`** (1,120 B — supplementary ThinLTO options):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `propagate-attrs` | bool | true | Propagate attributes through the summary index |
| `import-constants-with-refs` | bool | true | Import constant globals that have references |

**`ctor_419` at `0x531850`** (6,358 B — FunctionAttrs inference):

| Knob | Type | Default | Effect |
|------|------|---------|--------|
| `disable-thinlto-funcattrs` | bool | false | Disable function attribute inference from ThinLTO summaries |

## Data Structures

### Import Candidate Linked List

Each of the three priority lists in the `guid_import_map` is a singly-linked list with 8-byte node entries:

| Offset | Content |
|--------|---------|
| `[node+0x00]` | Entry value (pointer to candidate descriptor, or GUID) |
| `[node+0x08]` | Next slot / next node pointer |

Sentinels: `0xFFFFFFFFFFFFFFF8` (-8) = empty slot, `0xFFFFFFFFFFFFFFFC` (-4) = deleted slot. These sentinel values are standard open-addressing hash map markers repurposed for the linked-list traversal.

### GUID Import Map Layout

The `guid_import_map` structure (parameter `rcx` of `sub_1854A20`) contains the three priority lists:

| Offset | Size | Content |
|--------|------|---------|
| `+0x00` | 8 | Primary list head (direct call targets) |
| `+0x08` | 8 | Entry count / secondary sentinel |
| `+0x10` | 8 | Secondary list head (transitive callees) |
| `+0x18` | 8 | (reserved / alignment) |
| `+0x20` | 8 | (reserved / alignment) |
| `+0x28` | 8 | (reserved / alignment) |
| `+0x30` | 8 | Tertiary list head (speculative candidates) |

### GUID Dedup Hash Table

| Field | Size | Description |
|-------|------|-------------|
| Slot size | 16 bytes | `{GUID (8B), metadata (8B)}` |
| Hash function | multiplicative | `slot = (GUID * 37) & (table_size - 1)` |
| Collision resolution | linear probing | Increment slot by 1, wrap at table_size |
| Empty sentinel | -1 | `0xFFFFFFFFFFFFFFFF` |
| Size field | offset +0x18 | Number of slots in table (always power of 2) |

The multiplication constant 37 produces reasonable distribution for GUIDs that are typically MD5 hashes of mangled names. The linear probing is adequate because the table is sized to maintain a low load factor.

### Result Array

Growable array with 24-byte entries:

| Offset | Size | Content |
|--------|------|---------|
| `+0x00` | 8 | Function GUID |
| `+0x08` | 4 | Adjusted threshold value |
| `+0x10` | 8 | Import record pointer |

Header: `[+0x08]` = current count, `[+0x0C]` = capacity. Growth is handled by a realloc path when `count >= capacity`.

### Per-Function Summary Entry (import-relevant fields)

| Offset | Size | Content |
|--------|------|---------|
| `+0x08` | 4 | Entry type (2 = function summary) |
| `+0x0C` | 1 | Linkage byte: low 4 bits = linkage type, bit 4 = not-importable flag, bit 5 = importable flag |
| `+0x40` | 4 | Function cost (IR instruction count, used for threshold comparison) |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| ThinLTO import driver (triple-pass candidate processing) | `sub_1854A20` | 4,326 B | — |
| Threshold computation with GUID dedup and priority-class multipliers | `sub_1853180` | 5,059 B | — |
| Threshold comparison gate (returns nonzero if candidate qualifies) | `sub_18518A0` | — | — |
| Import candidate evaluator (prepares candidate for threshold check) | `sub_1852CC0` | — | — |
| Import list builder (called by `sub_1853180`) | `sub_1852FB0` | — | — |
| Import list node allocator (called by `sub_1853180`) | `sub_1852A30` | — | — |
| Import list initialization (called by `sub_1853180`) | `sub_1851200` | — | — |
| Execute import decision (materialize function into destination) | `sub_15E4B20` | — | — |
| Resolve function name/info from summary | `sub_15E4EB0` | — | — |
| Entry point (parses `-function-import` / `-summary-file`) | `sub_1855B10` | 10,503 B | — |
| Whole-module ThinLTO processing | `sub_1858B90` | 31,344 B | — |
| Type metadata propagation during import | `sub_185E850` | 24,263 B | — |
| Attach named metadata (used for `thinlto_src_module`) | `sub_1627100` | — | — |
| Create optimization remark (import diagnostic) | `sub_1627350` | — | — |
| Resolve source module name string | `sub_161FF10` | — | — |
| Check if function exists in a given module | `sub_1670560` | — | — |
| Get "import source" module handle | `sub_16704E0` | — | — |
| Get "import destination" module handle | `sub_16704F0` | — | — |
| Format import remark (cost component) | `sub_16C1840` | — | — |
| Format import remark (threshold component) | `sub_16C1A90` | — | — |
| Finalize import remark string | `sub_16C1AA0` | — | — |
| Hash table insert (GUID dedup table) | `sub_1851560` | — | — |
| Initialize resolved function summary storage | `sub_1674380` | — | — |
| Finalize empty-import path cleanup | `sub_1851C60` | — | — |
| Release import list entry data | `sub_161E7C0` | — | — |
| `malloc` wrapper (used for 16-byte dedup node allocation) | `sub_22077B0` | — | — |

## Cross-References

- **[Inliner Cost Model](./inliner-cost.md)** — the downstream consumer of imported functions. Import brings bodies into the module; the 20,000-budget inliner decides whether to fold them into callers.
- **[Module Summary](./module-summary.md)** — `sub_D7D4E0` builds the `NVModuleSummary` that drives import decisions. The 4-level priority system, complexity budget, and CUDA-specific filtering all originate here.
- **[Pipeline & Ordering](../llvm/pipeline.md)** — `function-import` is registered as pipeline slot 43, a Module-level pass.
- **[IP Memory Space Propagation](../passes/ipmsp.md)** — after import, cross-module functions may carry address-space annotations that IPMSP must reconcile.
- **[Hash Infrastructure](../infra/hash-infrastructure.md)** — the GUID dedup table uses the same DenseMap pattern documented there.
