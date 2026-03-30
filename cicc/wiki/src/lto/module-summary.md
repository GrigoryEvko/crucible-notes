# NVModuleSummary Builder

CICC replaces LLVM's `ModuleSummaryAnalysis` with a custom `NVModuleSummary` subsystem that extends the `ModuleSummaryIndex` with GPU-specific information. The builder at `sub_D7D4E0` (74 KB, 2571 decompiled lines) walks every global value in a module, constructs per-function summaries with CUDA-aware call graph edges, assigns four-level import priorities using a custom priority table, tracks function complexity on a profile-guided budget, and records CUDA-specific attributes such as address-space linkage, kernel-vs-device classification, and device memory reference patterns. The summary is the data source for all downstream ThinLTO decisions -- the [ThinLTO importer](./thinlto-import.md) reads these summaries to decide which functions to pull across module boundaries, and the [inliner cost model](./inliner-cost.md) consumes the complexity budget to calibrate cross-module inline thresholds.

Upstream LLVM's `computeFunctionSummary` (in `ModuleSummaryAnalysis.cpp`) counts instructions, builds call graph edges from `CallBase` operands, collects reference edges by walking instruction operands, and records type test / devirtualization metadata. It produces a `FunctionSummary` with a flat instruction count and a call edge list annotated with `CalleeInfo::HotnessType` (Unknown/Cold/None/Hot). NVIDIA's replacement does all of this, then adds: a 4-level import priority classification per function, a 28-bit profile-scaled complexity budget, CUDA address-space tracking (filtering out device-memory-only declarations from import candidacy), kernel identification via first-instruction opcode probing, six separate CUDA-specific accumulator structures for device call context, and a two-phase declaration re-walk that merges forward-declared and defined symbol tables for ThinLTO.

| | |
|---|---|
| **Builder entry** | `sub_D7D4E0` (`0xD7D4E0`, 74 KB) |
| **LTO driver** | `sub_D81040` (`0xD81040`, 56 KB) |
| **Per-function analyzer** | `sub_D741C0` (`0xD741C0`, 19 KB) |
| **Call graph analyzer** | `sub_D6EA70` (`0xD6EA70`, 19 KB) |
| **Summary packer** | `sub_D77220` (`0xD77220`) |
| **Summary serializer** | `sub_1535340` (`0x1535340`, 26 KB) |
| **Summary parser** | `sub_150B5F0` (`0x150B5F0`, 63 KB) |
| **Address range** | `0xD60000`--`0xD82000` (full NVModuleSummary cluster) |
| **Stack frame** | 1,552 bytes (`0x610`) |

## Summary Fields Beyond Upstream

Upstream LLVM's `FunctionSummary` stores instruction count, call edges with hotness, reference edges, type test GUIDs, and a few flags (norecurse, returndoesnotalias, etc). NVIDIA extends this with the following per-function fields:

| Field | Encoding | Width | Description |
|-------|----------|-------|-------------|
| Import priority | `*entry & 0x7` | 3 bits | 4-level priority: 0 = not importable, 1 = low, 2 = standard, 3 = force-import |
| Address-taken flag | `*entry & 0x8` | 1 bit | Set if `sub_B49220(GV)` returns true (function has its address taken) |
| Complexity budget | `*entry >> 4` | 28 bits | Profile-scaled importance, max 0xFFFFFFF (268,435,455) |
| Kernel bit | `flags & (1 << 9)` | 1 bit | Set if first instruction opcode is 36 (kernel entry point) |
| Has-unwind-info | `flags & (1 << 0)` | 1 bit | `sub_B2DCC0(func)` -- has personality function |
| Not-inline | `flags & (1 << 1)` | 1 bit | Function marked noinline |
| Read-none | `flags & (1 << 2)` | 1 bit | Attribute #34 readnone |
| No-unwind | `flags & (1 << 3)` | 1 bit | Attribute #22 nounwind |
| Will-return | `flags & (1 << 4)` | 1 bit | Attribute #31 willreturn |
| No-return | `flags & (1 << 5)` | 1 bit | Attribute #3 noreturn |
| Must-progress | `flags & (1 << 6)` | 1 bit | Attribute #41 mustprogress |
| Has-visible-alias | `flags & (1 << 7)` | 1 bit | Accumulated alias visibility flag |
| Has-non-importable-refs | `flags & (1 << 8)` | 1 bit | References symbols that cannot be imported |
| Has-any-import | module flag bit 6 | 1 bit | OR of device-ref, has-typed-symbol, has-non-importable |

The per-entry summary record in the primary hash table is 16 bytes. The lower 32 bits pack the priority/address-taken/budget fields. The upper 64 bits hold a pointer to the full `FunctionSummary` record built by `sub_D77220`.

## Builder Algorithm

The builder executes in three phases within `sub_D7D4E0`. The LTO driver `sub_D81040` calls the builder after reading module flags (`EnableSplitLTOUnit`, `UnifiedLTO`, `ThinLTO`) and iterating all functions via a callback iterator.

### Phase 1: Global Value Walk (lines 559--1671)

The module's global value list is a linked list rooted at `Module+72` (the `GlobalList` field). The sentinel node is at `Module+72` itself; the first real element is at `Module+80`.

```c
// Phase 1: iterate all GlobalValues in the module
GlobalValue *sentinel = (GlobalValue *)(module + 72);
GlobalValue *cur = *(GlobalValue **)(module + 80);

while (cur != sentinel) {
    uint8_t opcode = cur->ir_node[0];  // IR opcode byte
    switch (opcode) {
    case 61: /* '=' -- Function definition */
        process_function(cur);
        break;
    case 62: /* '>' -- GlobalVariable */
        process_global_variable(cur);
        break;
    case 34: /* '"' -- Alias (kind 1) */
    case 40: /* '(' -- Alias (kind 2) */
        process_alias(cur);
        break;
    case 85: /* 'U' -- Declaration/extern */
        process_declaration(cur);
        break;
    }
    cur = cur->next;
}
```

For each function (opcode 61), the builder performs:

**1. Import priority assignment.** Queries the `ImportPriorityTable` via `sub_D84370(table, func, PSI, 0)`. If found and the table is non-null, the priority is determined:

```c
entry = getImportKind(priority_table, func, PSI, 0);
if (entry.found) {
    if (isImported(priority_table, entry))          // sub_D84440
        priority = 3;  // force-import
    else if (isImportCandidate(priority_table, entry, 3) == 0)  // sub_D84450
        priority = 2;  // standard importable
    else
        priority = 1;  // low priority
} else {
    priority = 0;  // not importable
}
```

**2. Complexity budget computation.** When `ProfileSummaryInfo` is available and the function was found in the priority table, the builder computes a profile-scaled importance value:

```c
uint64_t profile_count = getProfileCount(PSI, func);  // sub_FDD860
uint64_t threshold = getHotThreshold(PSI);              // sub_FDC4B0

if (profile_count exists) {
    APInt importance = computeScaledImportance(profile_count, threshold);  // sub_F04200
    normalizeImportPriority(&importance, 8);  // sub_D78C90: right-shift by 8
    budget += importance.getZExtValue();
    budget = min(budget, 0xFFFFFFF);  // clamp to 28-bit max
}

// Pack into entry: lower 4 bits = priority | address_taken, upper 28 bits = budget
*entry_word = (budget << 4) | (*entry_word & 0xF);
```

The 28-bit budget is consumed downstream by ThinLTO to decide how much inlining budget to allocate for functions imported from other modules. A budget of 0 means the function has no profile data and gets the baseline threshold; a budget near the 268M ceiling means the function is extremely hot and will receive aggressive cross-module inlining.

**3. Call graph edge construction.** For functions with call graph info (bit 5 of byte 7: `func->ir_node[7] & 0x20`), the builder extracts two kinds of edges:

- **Direct call edges** from attribute group #35: the callee list. Each callee gets a GUID via `sub_9E27D0`, and edges are collected into a temporary vector (4-byte stride per GUID).
- **Reference edges with type info** from attribute group #34: operand bundles encoding reference edges with type metadata. Each reference carries a `CalleeType` byte and parameter type pairs extracted from MDNode operands. The MDNode decoding walks: operand -> parent (opcode 1 = MDString) -> offset 136 (opcode 17 = MDTuple) -> string data at offset 24.

Call graph edge records are 136 bytes each (stride 136 in the edge vector) and contain source name, target name, and edge attributes. Type-metadata edges are 72 bytes each.

**4. CUDA address-space filtering.** When the CUDA-mode flag (`a6`) is set and a declaration has address space 25 in its type chain, the function sets the device-reference flag (`v327`). Functions whose type resolves to address space 25 are excluded from import candidacy -- device-memory-only declarations cannot be cross-module imported in ThinLTO. The check:

```c
if (cuda_mode && is_declaration(func)) {
    Type *ty = func->type_at_offset_minus_2;
    if (getAddressSpace(ty) == 25) {
        has_device_ref = true;
        goto skip_import;  // do not mark as importable
    }
}
```

Address space 25 appears to be an internal NVVM encoding for device-side linkage. This differs from the standard NVPTX address spaces (0 = generic, 1 = global, 3 = shared, 4 = constant, 5 = local). The summary records this flag so the importer can avoid attempting to import device-side-only symbols, which would fail at link time.

**5. CUDA call context collection.** For functions with the device attribute bit (`func[33] & 0x20`), the builder calls `sub_D7CF70` to populate six parallel accumulator structures:

| Accumulator | Offset | Likely content |
|-------------|--------|----------------|
| `v408` | +0 | Direct device call targets |
| `v415` | +1 | Shared memory references |
| `v422` | +2 | Texture/surface references |
| `v429` | +3 | Constant memory references |
| `v436` | +4 | Kernel launch edges |
| `a5` | +5 | Additional context (passed from caller) |

These six vectors capture the GPU-specific dependency information that upstream LLVM's summary has no concept of. The ThinLTO importer uses this to make GPU-aware import decisions -- for example, a function that references shared memory in another module must also import the shared memory declaration.

### Phase 2: ThinLTO Declaration Re-Walk (lines 1673--1911)

When `thinlto_mode` (parameter `a8`) is true, the builder performs a second pass over forward-declared symbols:

**Step 1.** Re-walk function declarations collected during Phase 1. For each, remove from the "seen" set and re-analyze via `sub_D7B190` into a secondary hash table.

**Step 2.** Re-walk global variable declarations through a separate dedup mechanism using `sub_C8CA60` for hash-based deduplication.

**Step 3.** Merge the secondary (forward-declared) and primary (defined) hash tables. On collision -- the same symbol appears as both declared and defined -- `sub_D76140` removes the entry from the defined table and `sub_D7AF10` re-inserts into the merged table with updated visibility. This merge ensures that the summary captures cross-module edges even for symbols that are only forward-declared in the current module.

The two-phase design is necessary because CUDA compilation units frequently contain forward declarations of device functions defined in other translation units. Without this re-walk, the summary would miss the cross-module edges for these declarations, and ThinLTO would fail to import them.

### Phase 3: Finalize and Emit (lines 1912--2569)

**Module-level flag assembly.** After processing all globals, the builder computes two flag words:

```c
// v134: module-level attribute summary (bits 0-10)
v134 = (linkage & 0xF)               // bits 0-3
     | ((visibility & 0x3) << 4)     // bits 4-5
     | (has_any_import << 6)          // bit 6: OR of v327|v316|v358
     | (has_comdat << 7)              // bit 7
     | (has_comdat_attr << 8)         // bit 8
     | (dll_storage_class << 9);      // bits 9-10

// v143: per-function flags (bits 0-9)
v143 = has_unwind_info               // bit 0
     | (not_inline << 1)             // bit 1
     | (readnone << 2)               // bit 2
     | (nounwind << 3)               // bit 3
     | (willreturn << 4)             // bit 4
     | (noreturn << 5)               // bit 5
     | (mustprogress << 6)           // bit 6
     | (has_visible_alias << 7)      // bit 7
     | (has_non_importable_refs << 8) // bit 8
     | (is_kernel << 9);             // bit 9
```

The kernel detection walks to the function's first instruction via offset 24, verifies the opcode is in range 30--40 (basic block terminators), and checks specifically for opcode 36, which encodes a kernel entry point. This is how the summary distinguishes `__global__` kernel functions from `__device__` helper functions without relying on metadata -- it inspects the compiled IR structure directly.

**Summary record packing.** All collected data is packed into the final `FunctionSummary` via `sub_D77220`, which takes 14 arguments:

```c
sub_D77220(
    &result,             // output FunctionSummary*
    module_flags,        // v134
    instruction_count,   // v324
    function_flags,      // v143 (includes kernel bit)
    &priority_slice,     // import priority table slice
    guid_ref_list,       // GUID reference list
    &typed_refs,         // type-checked reference list (72-byte entries)
    &typed_edges,        // typed call graph edges (136-byte entries)
    &simple_edges,       // simple call graph edges (GUID array)
    device_context,      // CUDA device context edges
    additional_edges,    // extra edge data
    &bundle_refs,        // operand bundle references
    &cross_module_calls, // cross-module call records
    &param_types         // per-parameter type metadata
);
```

The result is stored via `sub_D7A690(index, func, &result)` which merges the summary into the module-level index.

**Callback invocation.** The `a9` parameter is a callback object with vtable layout: `a9+16` points to a `shouldSkip()` predicate; `a9+24` points to a `processFunction(a9, GlobalValue*)` handler. When `shouldSkip()` returns null, the callback is invoked for each function. The callback result is processed by `sub_D8D9B0` which extracts additional summary information (likely profile or LTO-specific metadata).

## Serialization and the NVVM Container

The summary is serialized into bitcode by `sub_1535340` (writeModuleSummary, 26 KB). This function writes a `MODULE_STRTAB_BLOCK` and `GLOBALVAL_SUMMARY_BLOCK` into the LLVM bitcode stream using standard bitcode encoding (VBR integers, abbreviation-driven records). The strings `"ThinLTO"` and `"Unexpected anonymous function when writing summary"` appear in this function.

On the reading side, `sub_150B5F0` (parseModuleSummaryIndex, 63 KB) and `sub_9EBD80` (parseGlobalSummaryBlock, 82 KB) deserialize the summary from bitcode back into the in-memory `ModuleSummaryIndex`. These parsers handle GUID hashes, function/alias/global summaries, and module paths.

The bitcode writer at `sub_1538EC0` writes the producer string as `"LLVM7.0.1"` despite CICC being built on LLVM 20.0.0 internally -- this is the NVVM IR compatibility layer. The summary blocks are embedded in this bitcode stream alongside the IR, so the NVVM container format (see [NVVM Container](../structs/nvvm-container.md)) carries both the IR and its summary in a single bitcode file.

## Import Priority System

The 4-level priority system is the primary extension over upstream LLVM's binary importable/not-importable model. Upstream uses `GlobalValueSummary::ImportKind` which is essentially a boolean; NVIDIA introduces graduated priority levels that feed a floating-point threshold multiplier in the importer.

| Level | Value | Meaning | Importer behavior |
|-------|-------|---------|-------------------|
| 0 | `0b000` | Not importable | Never imported |
| 1 | `0b001` | Low priority | Threshold multiplied by cold multiplier (`dword_4FAACC0`) |
| 2 | `0b010` | Standard | Threshold multiplied by default multiplier (`dword_4FAB040`) |
| 3 | `0b011` | Force-import | Threshold multiplied by hot multiplier (`dword_4FAAE80`) |

The importer at `sub_1853180` converts the integer base threshold to float, multiplies by the per-priority-level constant, converts back to integer, and compares against the function's cost from the summary (stored at offset `0x40` in the summary entry). A fourth multiplier (`dword_4FAADA0`) handles "critical" priority (priority class 4 in the importer's switch), though the summary builder only produces levels 0--3.

For comdat/linkonce symbols discovered during Phase 3, a special minimum priority applies:

```c
min_priority = 3 * (dword_4F87C60 != 2) + 1;
// dword_4F87C60 == 2: min_priority = 1 (conservative)
// dword_4F87C60 != 2: min_priority = 4 (aggressive import)
```

## Hash Table Infrastructure

The builder manages multiple open-addressing hash tables with different entry sizes. All use the standard DenseMap pointer hash and growth policy; see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the common implementation.

| Table | Entry size | Probe strategy | Purpose |
|-------|-----------|----------------|---------|
| Primary (`v384`--`v387`) | 16 bytes | Linear probing | Main summary entries (ptr + metadata) |
| Secondary (`v388`--`v393`) | 8 bytes | Linear probing | Forward-declared symbol GUIDs |
| GUID dedup (`v406`--`v407`) | 8 bytes | Linear scan + memmove | Deduplication during merge |
| Seen set (`v451`--`v455`) | Variable | Flat array or hash | Tracks processed GlobalValues |

The "seen set" has two modes selected by `v455`: when `v455 = 1`, it uses a flat inline buffer at `v456` with `HIDWORD(v453)` as the count; when `v455 = 0`, it switches to a hash table via `sub_C8CA60`. This dual-mode design optimizes for the common case of small modules (flat scan is faster when count is low) while scaling to large modules.

Rehash strategy: `new_capacity = max(64, next_power_of_2(4 * current_count))`. The power-of-2 is computed via `_BitScanReverse`. If the new capacity equals the old, the table is cleared in-place via `memset` to the empty sentinel (`0xFF` for 8-byte entries, `0xF8` for 16-byte entries). Otherwise the old buffer is freed and a new one allocated via `sub_C7D670` (`aligned_alloc(8, size)`).

## Knobs and Global Variables

| Symbol | Type | Default | Effect |
|--------|------|---------|--------|
| `dword_4F87C60` | int | 0 | Import priority override: 0 = normal, 1 = force all importable, 2 = conservative mode |
| `qword_4F878A8` | bool | false | When set in ThinLTO mode, forces re-analysis of all referenced-but-undefined symbols |
| `byte_3F871B3` | byte | (varies) | Cross-module GUID namespace prefix, distinguishes same-named symbols across modules |
| `dword_4FAB120` | int | -1 | Global import budget (-1 = unlimited) |
| `dword_4FAA770` | int | 0 | Running count of imports performed |
| `dword_4FAAE80` | float | (varies) | Hot function threshold multiplier |
| `dword_4FAACC0` | float | (varies) | Cold function threshold multiplier |
| `dword_4FAADA0` | float | (varies) | Critical section threshold multiplier |
| `dword_4FAB040` | float | (varies) | Default threshold multiplier |
| `byte_4FAAA20` | bool | false | Enable `thinlto_src_module` metadata annotation on imported functions |

The `dword_4F87C60` override is the most impactful knob. Setting it to 1 makes every function importable regardless of its linkage or visibility, which is useful for whole-program optimization but can cause link-time explosions. Setting it to 2 enables conservative mode where comdat symbols get minimal priority (level 1 instead of 4), preventing aggressive cross-module import of weakly-linked symbols.

## Comparison with Upstream ModuleSummaryAnalysis

| Aspect | Upstream LLVM | CICC NVModuleSummary |
|--------|--------------|---------------------|
| Entry point | `computeFunctionSummary()` | `sub_D7D4E0` (2571 lines vs ~400) |
| Priority levels | Binary (importable or not) | 4 levels (0--3) with float multipliers |
| Complexity metric | Flat instruction count | 28-bit profile-scaled budget |
| Call edge annotation | `CalleeInfo::HotnessType` (4 values) | 136-byte records with full type metadata |
| Address space awareness | None | Filters device-only (AS 25) from import |
| Kernel detection | None | Opcode-36 probe for `__global__` functions |
| Declaration re-walk | None | Two-phase merge of declared + defined |
| CUDA context | None | 6 accumulators for device call patterns |
| Hash table sizing | LLVM DenseMap | Custom open-addressing with dual-mode seen set |
| Profile integration | BFI-based hotness | `ProfileSummaryInfo` scaled budget |
| Serialization | Standard `ModuleSummaryIndex` bitcode | Same format, extended fields |

The most architecturally significant difference is the priority system. Upstream LLVM makes a binary import/no-import decision based on a single threshold comparison. NVIDIA's 4-level system allows the importer to process functions in priority order (primary/secondary/tertiary passes in `sub_1854A20`) with different threshold multipliers per level, enabling much finer control over cross-module optimization aggressiveness.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `NVModuleSummary::buildModuleSummary()` -- main builder | `0xD7D4E0` | 74 KB | -- |
| `NVModuleSummary::runOnModule()` -- LTO driver | `0xD81040` | 56 KB | -- |
| `NVModuleSummary::analyzeFunction()` | `0xD741C0` | 19 KB | -- |
| `NVModuleSummary::processGlobalRef()` | `0xD6FF50` | 47 KB | -- |
| `NVModuleSummary::collectGlobalInfo()` | `0xD6A180` | 21 KB | -- |
| `NVModuleSummary::analyzeCallGraph()` | `0xD6EA70` | 19 KB | -- |
| `NVModuleSummary::visitInstruction()` | `0xD7B190` | 9 KB | -- |
| Alias processing helper | `0xD738B0` | 11 KB | -- |
| `NVModuleSummary::computeImportCost()` | `0xD72D40` | 9 KB | -- |
| `NVModuleSummary::resolveReferences()` | `0xD64DE0` | 16 KB | -- |
| `NVModuleSummary::getTypeMetadata()` | `0xD669C0` | 11 KB | -- |
| `NVModuleSummary::processTypeId()` | `0xD640E0` | 12 KB | -- |
| `NVModuleSummary::computeVisibility()` | `0xD63080` | 11 KB | -- |
| Summary serialization helper (recursive) | `0xD60CE0` | 15 KB | -- |
| Summary serialization helper | `0xD61E90` | 10 KB | -- |
| `NVModuleSummary::packFunctionSummary()` -- 14-arg final packer | `0xD77220` | -- | -- |
| `NVModuleSummary::addInlineSummary()` -- CUDA context collector | `0xD7CF70` | -- | -- |
| `NVModuleSummary::addEdge()` | `0xD76530` | -- | -- |
| `NVModuleSummary::addRef()` | `0xD768F0` | -- | -- |
| `NVModuleSummary::addSpecialGlobal()` (llvm.used etc.) | `0xD76CA0` | -- | -- |
| `NVModuleSummary::addTypeRef()` | `0xD76D40` | -- | -- |
| `NVModuleSummary::computeNextPrime()` -- hash table sizing | `0xD76FC0` | -- | -- |
| `NVModuleSummary::getModuleHash()` | `0xD771D0` | -- | -- |
| `NVModuleSummary::destroyEdgeList()` | `0xD77880` | -- | -- |
| `NVModuleSummary::destroyRefList()` | `0xD786F0` | -- | -- |
| `NVModuleSummary::compareImportPriority()` | `0xD788E0` | -- | -- |
| `NVModuleSummary::computeSymbolHash()` | `0xD789D0` | -- | -- |
| `NVModuleSummary::resizeTable()` | `0xD78B00` | -- | -- |
| `NVModuleSummary::normalizeImportPriority()` | `0xD78C90` | -- | -- |
| `NVModuleSummary::addCallEdge()` | `0xD793D0` | -- | -- |
| Rehash/resize (next power-of-2, min 64) | `0xD79200` | -- | -- |
| `NVModuleSummary::copyTable()` | `0xD7A410` | -- | -- |
| `NVModuleSummary::mergeSymbols()` | `0xD7A690` | -- | -- |
| `NVModuleSummary::computeFinalOrder()` | `0xD7AC80` | -- | -- |
| `NVModuleSummary::getOrInsertSummary()` | `0xD7BAA0` | -- | -- |
| `NVModuleSummary::visitGlobalValue()` | `0xD7BD50` | -- | -- |
| `NVModuleSummary::getImportKind()` | `0xD84370` | -- | -- |
| `NVModuleSummary::isImported()` | `0xD84440` | -- | -- |
| `NVModuleSummary::isImportCandidate()` | `0xD84450` | -- | -- |
| `NVModuleSummary::processInliningDecisions()` | `0xD8B020` | 21 KB | -- |
| `NVModuleSummary::computeInlineBenefit()` | `0xD8C2B0` | 8 KB | -- |
| `NVModuleSummary::buildCalleeList()` | `0xD8D9B0` | 9 KB | -- |
| `NVModuleSummary::cloneModuleSummary()` | `0xD8E7E0` | 32 KB | -- |
| GUID lookup/creation (namespace-aware) | `0x9CA390` | -- | -- |
| Get attribute group by kind from GlobalValue | `0xB91C10` | -- | -- |
| `ProfileSummaryInfo::getProfileCount()` | `0xFDD860` | -- | -- |
| `ProfileSummaryInfo::getHotThreshold()` | `0xFDC4B0` | -- | -- |
| `writeModuleSummary()` -- bitcode serializer | `0x1535340` | 26 KB | -- |
| `parseModuleSummaryIndex()` -- bitcode deserializer | `0x150B5F0` | 63 KB | -- |

## Cross-References

- [Inliner Cost Model](./inliner-cost.md) -- consumes complexity budget for cross-module inline decisions
- [ThinLTO Function Import](./thinlto-import.md) -- reads summaries, applies threshold multipliers per priority level
- [NVVM Container Format](../structs/nvvm-container.md) -- the bitcode container that carries serialized summaries
- [GlobalOpt](./globalopt.md) -- uses summary visibility information for global optimization
- [WholeProgramDevirtualization](./devirtualization.md) -- consumes type test GUIDs from the summary
