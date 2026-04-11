# Phase Manager Infrastructure

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The PhaseManager is the central orchestration layer in ptxas. It owns the entire 159-phase optimization and code generation pipeline, constructs each phase as a polymorphic object via an abstract factory, and drives execution through a virtual dispatch loop. Every compilation unit passes through the same PhaseManager sequence: construct all 159 phase objects, iterate the phase index array calling `execute()` on each, optionally collect per-phase timing and memory statistics, then tear down. The PhaseManager also hosts an optional NvOptRecipe sub-manager (440 bytes) for architecture-specific "advanced phase" hooks that inject additional processing at 16 defined points in the pipeline.

The design is a textbook Strategy + Abstract Factory pattern: a 159-case switch statement maps phase indices to vtable pointers, each vtable provides `execute()`, `isNoOp()`, and `getName()` virtual methods, and the dispatch loop iterates a flat index array that defines execution order. This makes the pipeline fully data-driven -- reordering, disabling, or injecting phases requires only modifying the index array, not the dispatch logic.

| | |
|---|---|
| **Core range** | `0xC60000`--`0xC66000` (13 functions, ~17.5 KB) |
| **Constructor** | `sub_C62720` (4,734 bytes) |
| **Destructor** | `sub_C61B20` (1,753 bytes) |
| **Phase factory** | `sub_C60D30` (3,554 bytes, 159-case switch) |
| **Dispatch loop** | `sub_C64F70` (1,455 bytes) |
| **Name lookup** | `sub_C641D0` (305 bytes, case-insensitive binary search) |
| **Timing reporter** | `sub_C64310` (3,168 bytes) |
| **Pool reporter** | `sub_C62200` (888 bytes) |
| **Total phases** | 159 (139 explicitly named + 20 arch-specific) |
| **AdvancedPhase hooks** | 16 no-op-by-default insertion points |
| **Default phase table** | Static array at `0x22BEEA0` (returned by `sub_C60D20`) |
| **Phase name table** | Static array at `off_22BD0C0` (159 string pointers) |
| **Vtable range** | `off_22BD5C8` (phase 0) through `off_22BEE78` (phase 158) |
| **Callers** | `sub_7FB6C0` (main compilation driver), `sub_9F63D0` (library/ftrace entry) |

## PhaseManager Object Layout

The PhaseManager is a plain C++ object (no vtable of its own) allocated by the compilation driver. Minimum size is 112 bytes, though the full extent depends on whether timing and NvOptRecipe are enabled.

```
PhaseManager (112+ bytes)
  +0    int64     compilation_unit      // back-pointer to owning compilation unit
  +8    int64*    allocator             // pool allocator (from compilation_unit->field_16)
  +16   void*     sorted_name_table     // sorted {name_ptr, index} pairs for binary search
  +24   int       sorted_name_count
  +28   int       sorted_name_capacity
  +32   int64*    allocator2            // copy of allocator (for phase list ops)
  +40   void*     phase_list            // array of 16-byte {phase_ptr, pool_ptr} pairs
  +48   int       phase_list_count      // always 159 after construction
  +52   int       phase_list_capacity
  +56   int64     nvopt_recipe_ptr      // NvOptRecipe sub-manager, or NULL
  +64   int64     (reserved)
  +72   bool      timing_enabled        // set from compilation_unit->config->options[17928]
  +76   int       (flags/padding)
  +80   bool      flag_byte             // initialized to 1, reset after first timing report
  +88   int64*    timing_allocator
  +96   void*     phase_name_raw_table  // 159 name string pointers, copied from off_22BD0C0
  +104  int       phase_name_raw_count
  +108  int       phase_name_raw_capacity
```

The two allocator fields (`+8` and `+32`) both point to the same pool allocator extracted from the compilation unit, but are used in different contexts: `+8` for name table operations, `+32` for phase list operations.

## Phase Object Model

Each phase is a 16-byte polymorphic object:

```
Phase (16 bytes)
  +0    vtable*   // points to one of 159 vtable instances
  +8    void*     // pool pointer (memory pool for phase-local allocations)
```

The vtable provides the interface contract:

| Vtable offset | Method | Signature |
|---|---|---|
| `+0` | `execute` | `void execute(phase*, compilation_context*)` |
| `+8` | `getIndex` | `int getIndex(phase*)` -- returns the factory/table index (0--158) |
| `+16` | `isNoOp` | `bool isNoOp(phase*)` -- returns 0 for active phases, 1 for gates skipped by default |
| `+24` | *(NULL)* | Unused -- NULL in all 159 vtable instances |
| `+32` | *(NULL)* | Unused -- NULL in all 159 vtable instances |

The vtable addresses span `off_22BD5C8` (phase 0) through `off_22BEE78` (phase 158), with a stride of `0x28` (40 bytes) between consecutive entries. All vtables reside in `.data.rel.ro`.

## Phase Factory -- `sub_C60D30`

The factory is a 159-case switch statement that serves as the sole point of phase instantiation. For each case:

1. Extracts the pool allocator from `context->field_16`
2. Allocates 16 bytes via `pool_alloc` (`sub_424070`)
3. Writes the case-specific vtable pointer at offset `+0`
4. Returns a `{phase_ptr, pool_ptr}` pair

The default case returns `{NULL, NULL}`, which the caller treats as an invalid phase index.

```c
// Pseudocode for sub_C60D30
pair<phase*, pool*> PhaseFactory(int phase_index, context* ctx) {
    pool* p = ctx->allocator;
    phase* obj = p->alloc(16);
    switch (phase_index) {
        case 0:   obj->vtable = off_22BD5C8; break;  // OriCheckInitialProgram
        case 1:   obj->vtable = off_22BD5F0; break;  // ApplyNvOptRecipes
        case 2:   obj->vtable = off_22BD618; break;  // PromoteFP16
        // ... 156 more cases ...
        case 158: obj->vtable = off_22BEE78; break;  // sentinel/NOP
        default:  return {NULL, NULL};
    }
    return {obj, p};
}
```

Called exclusively by the constructor (`sub_C62720`).

## Construction Sequence -- `sub_C62720`

The constructor performs 11 steps, building all internal data structures and instantiating every phase:

```c
// Pseudocode for sub_C62720
bool PhaseManager::construct(compilation_unit* cu) {
    this->cu          = cu;
    this->allocator   = cu->field_16;      // extract pool allocator
    this->allocator2  = cu->field_16;

    // 1. Check timing flag
    this->timing_enabled = cu->config->options[17928];

    // 2. Allocate and copy phase name table (1272 = 159 * 8 bytes)
    this->phase_name_raw_table = alloc(1272);
    memcpy(this->phase_name_raw_table, off_22BD0C0, 1272);
    this->phase_name_raw_count    = 159;
    this->phase_name_raw_capacity = 159;

    // 3. Initialize timing records
    resize_timing(/*capacity=*/159);                    // sub_C62580
    cu->timing_count++;                                 // at cu+1576
    append_timing({index=-1, name=0x2030007, time=0, flags=0});  // sentinel

    // 4. Create all 159 phase objects
    resize_phase_list(/*capacity=*/159);                // sub_C62640
    for (int i = 0; i < 159; i++) {
        auto [phase, pool] = PhaseFactory(i, cu);       // sub_C60D30
        phase_list[i] = {phase, pool};
    }

    // 5. Optionally create NvOptRecipe sub-manager
    if (cu->config->getOption(391)) {
        auto* recipe = alloc(440);
        // initialize hash table, ref-counted lists, timing arrays (8 entries)
        // inherit phase chain from previous execution context
        this->nvopt_recipe_ptr = recipe;
    }
    this->flag_byte = 1;
    return true;
}
```

Key constants:
- **159** -- total phase count, used as loop bound and array capacities
- **1272** -- `159 * 8`, phase name pointer table size in bytes
- **440** -- NvOptRecipe sub-manager object size
- **0x2030007** (33,739,079) -- timing sentinel magic value
- **Option 17928** -- enables per-phase timing/memory reporting
- **Option 391** -- enables NvOptRecipe sub-manager

## Destruction Sequence -- `sub_C61B20`

Teardown mirrors construction in reverse order, with careful handling of the NvOptRecipe's reference-counted shared state:

```c
// Pseudocode for sub_C61B20
void PhaseManager::destroy() {
    // 1. Free raw name table
    timing_allocator->free(phase_name_raw_table);

    // 2. Tear down NvOptRecipe if present
    if (nvopt_recipe_ptr) {
        auto* r = nvopt_recipe_ptr;
        // decrement shared_list ref-count at +432
        if (--r->shared_list_refcount == 0)
            free_list_nodes(r->shared_list);
        free(r->hash_buckets);        // +408
        free(r->sorted_array);        // +376
        free(r->timing_records);      // +344, stride=584 per entry
        free(r->node_pool);           // +16
        free(r);
    }

    // 3. Free each phase object via pool_free (sub_4248B0)
    for (int i = 0; i < phase_list_count; i++) {
        auto [phase, pool] = phase_list[i];
        pool_free(phase);             // sub_4248B0 -- returns 16 bytes to pool
    }

    // 4. Free base arrays
    allocator2->free(phase_list);
    allocator->free(sorted_name_table);
}
```

The ref-count decrement-and-destroy pattern on `shared_list` at `+432` follows C++ `shared_ptr` semantics: the NvOptRecipe may share state across multiple compilation units in library mode.

## Phase Dispatch Loop -- `sub_C64F70`

The dispatch loop is the runtime engine. It takes a slice of the phase index array and executes each phase in order:

```c
// Pseudocode for sub_C64F70
bool PhaseManager::dispatch(int* phase_indices, int count) {
    memory_snapshot_t base_snap;
    take_snapshot(&base_snap);                          // sub_8DADE0

    for (int i = 0; i < count; i++) {
        int idx = phase_indices[i];
        phase* p = this->phase_list[idx].phase;

        // Resolve phase name -- always, even for no-op phases
        int name_idx = p->getName();                    // vtable+8
        const char* name = this->phase_name_raw_table[name_idx];

        // Record timing entry -- unconditional, no-op phases get a record too
        append_timing({idx, name, opt_level, flags, metrics});

        // Take pre-execution snapshot -- unconditional
        memory_snapshot_t pre_snap;
        take_snapshot(&pre_snap);                       // sub_8DADE0

        // isNoOp() gates diagnostic strings, NOT execute().
        // execute() is always called; a no-op phase's execute body returns
        // immediately.  The isNoOp check is performed twice: once before
        // execute (gates the "Before" string) and once after (gates the
        // "After" string).  This double-check allows a phase to dynamically
        // toggle its no-op status during execution.
        if (!p->isNoOp()) {                             // vtable+16, pre-check
            diagnostic("Before " + name);               // alloc, write, free
        }

        p->execute(this->cu);                           // vtable+0, ALWAYS called

        if (!p->isNoOp()) {                             // vtable+16, post-check
            diagnostic("After " + name);                // alloc, write, free
        }

        // Report per-phase stats -- unconditional when timing_enabled,
        // regardless of isNoOp() result
        if (this->timing_enabled) {
            report_phase_stats(name, &pre_snap, false); // sub_C64310
            this->flag_byte = 0;
        }
    }

    // Summary after all phases
    if (this->timing_enabled) {
        report_phase_stats("All Phases Summary", &base_snap, true);
        report_pool_consumption();                      // sub_C62200
    }
    return true;
}
```

**isNoOp timing behavior (binary evidence from `0xC64F70`):** The timing record
(`append_timing` at `+1560`) and the pre-execution memory snapshot (`sub_8DADE0`
into `var_68`) are both written **before** the first `isNoOp()` call at `0xC65078`.
When timing is enabled, `sub_C64310` is called at `0xC65121` in the common path
that both no-op and active phases reach.  A disabled phase therefore appears in the
timing array with near-zero elapsed time and a zero-delta memory snapshot, rather
than being omitted.  This means `--ftime` output always shows all 159 phase slots,
with no-op phases contributing empty rows.

The "Before" / "After" diagnostic strings use an interesting encoding trick: the string `"Before "` is stored as the 64-bit integer `0x2065726F666542` in little-endian, allowing the compiler to emit a single `mov` instruction instead of a `memcpy`. The string `"After "` is stored as two writes: a 4-byte `dword 0x65746641` ("Afte") plus a 2-byte `word 0x2072` ("r ") plus a null terminator byte, totaling 7 bytes at `0xC651F7`--`0xC65208`.

## Phase Name Lookup -- `sub_C641D0`

External callers (e.g., `--ftrace-phase-after` option processing in `sub_9F4040`) resolve phase names to indices through a case-insensitive binary search:

```c
// Pseudocode for sub_C641D0
int PhaseManager::lookup_phase(const char* name) {
    ensure_sorted();                                    // sub_C63FA0

    int lo = 0, hi = sorted_name_count - 1;
    while (lo <= hi) {
        int mid = (lo + hi) / 2;
        int cmp = strcasecmp(sorted_name_table[mid].name, name);
        if (cmp == 0) return sorted_name_table[mid].index;
        if (cmp < 0)  lo = mid + 1;
        else           hi = mid - 1;
    }
    return 158;  // sentinel: last phase (NOP)
}
```

The sorted name table is rebuilt on demand by `sub_C63FA0` when the raw count differs from the sorted count. Sorting uses an iterative quicksort (`sub_C639A0`) with median-of-three pivot selection and three-way partitioning. The sort stack is pre-allocated to 33 entries, sufficient for `ceil(log2(160))`.

## Per-Phase Timing and Memory Reporting

When timing is enabled (option 17928), the dispatch loop calls `sub_C64310` after each phase to print memory statistics:

```
<indent><phase_name>  ::  [Total 1234 KB]  [Freeable 567 KB]  [Freeable Leaked 12 KB] (2%)
```

The reporter computes three memory deltas from snapshot pairs:

| Metric | Helper | Meaning |
|---|---|---|
| Total | `sub_8DAE20` | Total memory allocated since snapshot |
| Freeable | `sub_8DAE30` | Memory eligible for release |
| Freeable Leaked | `sub_8DAE40` | Freeable memory not actually released |

Size formatting thresholds:
- 0--1023: raw bytes (suffix `B`)
- 1024--10,485,760: kilobytes with 3 decimal places (suffix `KB`)
- above 10 MB: megabytes with 3 decimal places (suffix `MB`)

After all phases complete, the loop prints an "All Phases Summary" line using the same reporter, then calls `sub_C62200` to print the pool consumption total:

```
[Pool Consumption = 45.678 MB]
```

### Timing Record Format

Each timing entry is 32 bytes:

```
Timing Record (32 bytes)
  +0    int       phase_index       // -1 for sentinel
  +8    int64     phase_name        // string pointer, or 0x2030007 for sentinel
  +16   int64     timing_value      // elapsed time
  +24   int       memory_flags      // opt level / additional metrics
```

Records are stored in a growable array at `compilation_unit+1560`. Growth uses a 1.5x strategy: `new_capacity = max(old + old/2 + 1, requested)`.

## NvOptRecipe Sub-Manager (440 bytes)

When option 391 is enabled, the constructor creates a 440-byte NvOptRecipe sub-manager at `PhaseManager+56`. This object provides the runtime for "AdvancedPhase" hooks -- the 16 phases that are no-ops by default but can be activated for architecture-specific or optimization-level-specific processing. The NvOpt level (0--5) controls per-phase aggressiveness independently of the `-O` CLI level: `-O` gates which phases run at all, while the NvOpt level controls how aggressively active phases behave.

### Object Layout

```
NvOptRecipe (440 bytes)
  +0    int64     compilation_unit           // back-pointer to owning CU
  +8    int64     phase_manager_backref      // back-pointer to PhaseManager
  +16   void*     node_pool                  // 24-byte ref-counted list node
  +24   int64     secondary_bucket_count     // secondary hash (migration buffer)
  +32   void*     secondary_bucket_array     // secondary hash bucket array
  +40   int64     secondary_total_entries    // secondary hash entry count
  +48   (264 B)   [opaque internal region]   // +48..+311 undecoded
  +312  int64     recipe_data                // from option 391 value (ext. pointer)
  +320  int64     (reserved)                 // zeroed in constructor
  +328  (8 B)     [alignment gap]
  +336  int64     allocator                  // cu->field_16->field_16
  +344  void*     timing_records             // stride = 584 bytes per entry
  +352  int32     timing_count               // init -1 (empty sentinel)
  +356  int32     timing_flags               // init 0
  +360  int32     timing_extra               // init 0
  +364  (4 B)     (padding)
  +368  int64*    timing_allocator            // cu->field_16->field_16 copy
  +376  void*     sorted_array               // 4-byte entries, init capacity = 8
  +384  int32     sorted_count               // init 7 (pre-filled)
  +388  int32     sorted_capacity            // init 8
  +392  void*     ref_counted_list_2         // 24-byte ref-counted list node
  +400  int32     hash_bucket_count          // primary hash table bucket count
  +404  (4 B)     (padding)
  +408  void*     hash_buckets               // primary hash, 24-byte stride/bucket
  +416  int64     hash_size                  // total entries across all buckets
  +424  (8 B)     (padding)
  +432  void*     shared_list_ptr            // ref-counted, shared across CUs
```

### Sub-Structures

**Ref-Counted List Node (24 bytes)** -- used at `+16`, `+392`, `+432`:

```
RefCountedListNode (24 bytes)
  +0    int64     refcount        // manual shared_ptr: decrement-and-destroy
  +8    void*     next            // singly-linked list chain
  +16   void*     allocator       // for self-deallocation when refcount → 0
```

When the refcount reaches zero, the destructor walks the `next` chain freeing each node, then frees the head node itself through the allocator at `+16`.

**Hash Bucket Entry (24 bytes)** -- array at `+408`:

```
HashBucketEntry (24 bytes)
  +0    void*     chain_head      // first element in bucket chain
  +8    void*     chain_sentinel  // end-of-chain sentinel
  +16   int32     chain_count     // number of elements in this bucket
```

**Timing Record (584 bytes)** -- array at `+344`:

```
TimingRecord (584 bytes)
  +0    (40 B)    header
  +40   void*     sub_allocator   // allocator for sub-data at +48
  +48   void*     sub_data        // freed during cleanup
  +56   int32     sub_count       // set to -1 when cleaned
  +60   int32     cleanup_flag    // if >= 0: sub_data exists, free it
  +64   (520 B)   timing/metric data
```

Records are iterated backward during cleanup (`base + 584 * (count + 1) - 584` down to `base`). The sentinel value `-1` at offset `+56` marks an entry as already cleaned up.

### Construction Sequence

The constructor (`sub_C62720`, lines 356--850 in decompilation) performs these steps:

1. **Check option 391** -- fast path: `*(config_obj[9] + 28152) != 0`; slow path: virtual call with argument `391`. If disabled, skip entirely.

2. **Read option 391 value** -- the value is the `recipe_data` pointer. Fast path checks type tag `5` (int64) at config offset `28152`, reads the 64-bit value at offset `28160`. This is an externally-provided pointer, not computed locally.

3. **Allocate 440 bytes** from the pool allocator at `compilation_unit->field_16`.

4. **Initialize core fields** -- back-pointers at `+0`/`+8`, `node_pool` at `+16` (24-byte ref-counted node, refcount=1), zero `+24`/`+32`/`+40`, store `recipe_data` at `+312`.

5. **Initialize timing** -- zero `+344`, set `+352` to `-1` (empty sentinel), zero `+360`, copy allocator to `+336` and `+368`.

6. **Allocate sorted_array** -- initial capacity 8 entries (32 bytes), pre-fill 7 entries, set `+384` = 7, `+388` = 8.

7. **Allocate `ref_counted_list_2`** at `+392` (24-byte node, refcount=1), zero `+400`/`+408`/`+416`.

8. **Allocate `shared_list`** at `+432` (24-byte node, refcount=1).

9. **Inherit from previous recipe** -- if `PhaseManager+56` already holds an NvOptRecipe from a prior compilation unit:
   - Decrement old `shared_list` refcount; free if zero
   - Migrate hash bucket chains from old recipe to new `ref_counted_list_2`
   - Walk old timing records backward (stride 584), freeing sub-allocations
   - Drain old secondary hash table, release old `node_pool`
   - Free old NvOptRecipe object

10. **Install** -- set `PhaseManager+56` = new recipe, `PhaseManager+64` = allocator.

### Destruction Sequence

The destructor (`sub_C61B20`) tears down the recipe in reverse:

1. Decrement `shared_list_ptr` (`+432`) refcount; free linked nodes if zero
2. Walk hash buckets (`+408`, stride 24, count from `+416`): for each chain element, clean sub-entries (timing at offsets `+56`/`+60`/`+64`/`+76`), decrement per-entry refcounts at element `[9]`, append to `ref_counted_list_2`; zero bucket; reset `+400` to 0
3. Clean up `ref_counted_list_2` (`+392`); free if refcount zero
4. Free `sorted_array` (`+376`) if `sorted_count` (`+388`) >= 0
5. Walk `timing_records` (`+344`) backward, stride 584, freeing sub-allocations; reset `+352` to `-1`
6. Drain secondary hash (`+24`/`+32`/`+40`), move chains to `node_pool`
7. Release `node_pool` (`+16`); free if refcount zero
8. Free the 440-byte object via `PhaseManager+64` allocator

### NvOpt Level Validation

The recipe application function `sub_C173E0` validates the NvOpt level at each recipe record:

```c
// At sub_C173E0 + 0x2FD9 (line 1431)
int nvopt_level = *(int*)(recipe_record + 344);
if (nvopt_level > 5) {
    emit_warning(cu + 1232, 8000, "Invalid nvopt level : %d.", nvopt_level);
    // warning 8000 (0x1F40) -- non-fatal, compilation continues
}
```

Valid levels are 0--5. The level is consumed as a bitmask `1 << nvopt_level`, passed to a vtable call that dispatches on a recipe configuration byte at target descriptor offset `35280` (8-case switch: cases 0--5, 7). This byte controls which recipe application mode is used for the target architecture.

### Shared State in Library Mode

The `shared_list` at `+432` enables recipe state persistence across compilation units in library mode (multiple `.ptx` files compiled by one ptxas invocation):

- Each new NvOptRecipe sets its `shared_list` refcount to 1
- During inheritance (step 9), hash bucket contents are **migrated** from the old recipe to the new one, accumulating per-kernel recipe decisions
- When a PhaseManager is destroyed, the recipe decrements the shared_list refcount; only the last reference frees the nodes
- This allows the NvOptRecipe to cache per-kernel optimization decisions across compilation passes

### Key Constants

| Value | Meaning |
|---|---|
| **440** | NvOptRecipe object size (bytes) |
| **584** | Per-entry timing record stride (bytes) |
| **24** | Hash bucket entry size / ref-counted list node size |
| **8** | Initial `sorted_array` capacity |
| **7** | Initial `sorted_count` (pre-filled entries) |
| **391** | Option ID (enables NvOptRecipe; value = recipe data pointer) |
| **28152** | Option 391 type-tag offset in config storage |
| **28160** | Option 391 value offset (8 bytes after type tag) |
| **0x1F40** | Warning code 8000: "Invalid nvopt level" |
| **5** | Maximum valid NvOpt level |
| **35280** | Recipe config byte offset in target descriptor |

## Multi-Function Dispatch -- `sub_C60BD0`

When a compilation unit contains more than one function, `sub_C60BD0` redirects to a per-function dispatch path:

```c
// Pseudocode for sub_C60BD0
void PhaseManager::invoke_multi(compilation_unit* cu) {
    int func_count = get_function_count(cu);            // sub_7DDB50
    if (func_count > 1) {
        auto list1 = create_refcounted_list();
        auto list2 = create_refcounted_list();
        this->phase_chain = current_chain;              // +88
        per_function_dispatch(cu, list1, list2);        // sub_790A40
        release(list1);
        release(list2);
    }
}
```

## Complete Phase Table

> **Stage numbering.** The 7 groups below are a coarse summary of the 159-phase OCG pipeline. The authoritative fine-grained grouping is the 10-stage scheme in the [Pass Inventory](../passes/index.md) (OCG-Stage 1--10). The 7-group table here collapses several of those stages for brevity; phase boundaries differ slightly. When citing a stage by number, prefer the Pass Inventory's 10-stage numbering.

### Group 1: Initial Setup (phases 0--12)

| Index | Phase Name | Purpose |
|---|---|---|
| 0 | `OriCheckInitialProgram` | Validate initial Ori IR |
| 1 | `ApplyNvOptRecipes` | Apply NvOptRecipe transformations |
| 2 | `PromoteFP16` | Promote FP16 operations where beneficial |
| 3 | `AnalyzeControlFlow` | Build/analyze control flow graph |
| 4 | `AdvancedPhaseBeforeConvUnSup` | **Hook** -- before unsupported op conversion |
| 5 | `ConvertUnsupportedOps` | Lower unsupported operations to supported sequences |
| 6 | `SetControlFlowOpLastInBB` | Mark control flow ops as last in basic block |
| 7 | `AdvancedPhaseAfterConvUnSup` | **Hook** -- after unsupported op conversion |
| 8 | `OriCreateMacroInsts` | Create macro instruction patterns |
| 9 | `ReportInitialRepresentation` | Diagnostic dump of initial IR |
| 10 | `EarlyOriSimpleLiveDead` | Early dead code elimination |
| 11 | `ReplaceUniformsWithImm` | Replace uniform register loads with immediates |
| 12 | `OriSanitize` | IR consistency checks |

### Group 2: Early Optimization (phases 13--36)

| Index | Phase Name | Purpose |
|---|---|---|
| 13 | `GeneralOptimizeEarly` | First GeneralOptimize pass (peephole + simplify) |
| 14 | `DoSwitchOptFirst` | Switch statement optimization, first pass |
| 15 | `OriBranchOpt` | Branch simplification and folding |
| 16 | `OriPerformLiveDeadFirst` | Liveness analysis, first pass |
| 17 | `OptimizeBindlessHeaderLoads` | Optimize bindless texture header loads |
| 18 | `OriLoopSimplification` | Canonicalize loop structure |
| 19 | `OriSplitLiveRanges` | Split long live ranges to reduce pressure |
| 20 | `PerformPGO` | Apply profile-guided optimizations |
| 21 | `OriStrengthReduce` | Strength reduction on induction variables |
| 22 | `OriLoopUnrolling` | Loop unrolling |
| 23 | `GenerateMovPhi` | Convert phi nodes to MOV-phi representation |
| 24 | `OriPipelining` | Software pipelining of loops |
| 25 | `StageAndFence` | Memory staging and fence insertion |
| 26 | `OriRemoveRedundantBarriers` | Remove unnecessary barrier instructions |
| 27 | `AnalyzeUniformsForSpeculation` | Identify uniform values for speculative execution |
| 28 | `SinkRemat` | Sink rematerializable instructions |
| 29 | `GeneralOptimize` | Main GeneralOptimize pass |
| 30 | `DoSwitchOptSecond` | Switch optimization, second pass |
| 31 | `OriLinearReplacement` | Replace complex patterns with linear sequences |
| 32 | `CompactLocalMemory` | Compact local memory layout |
| 33 | `OriPerformLiveDeadSecond` | Liveness analysis, second pass |
| 34 | `ExtractShaderConstsFirst` | Extract shader constants, first pass |
| 35 | `OriHoistInvariantsEarly` | Early loop-invariant hoisting |
| 36 | `EmitPSI` | Emit program state information |

### Group 3: Mid-Level Optimization (phases 37--58)

| Index | Phase Name | Purpose |
|---|---|---|
| 37 | `GeneralOptimizeMid` | Mid-pipeline GeneralOptimize |
| 38 | `OptimizeNestedCondBranches` | Simplify nested conditional branches |
| 39 | `ConvertVTGReadWrite` | Convert vertex/tessellation/geometry read/write ops |
| 40 | `DoVirtualCTAExpansion` | Expand virtual CTA operations |
| 41 | `MarkAdditionalColdBlocks` | Mark additional basic blocks as cold |
| 42 | `ExpandMbarrier` | Expand mbarrier intrinsics |
| 43 | `ForwardProgress` | Ensure forward progress guarantees |
| 44 | `OptimizeUniformAtomic` | Optimize uniform atomic operations |
| 45 | `MidExpansion` | Mid-pipeline lowering and expansion |
| 46 | `GeneralOptimizeMid2` | Second mid-pipeline GeneralOptimize |
| 47 | `AdvancedPhaseEarlyEnforceArgs` | **Hook** -- before argument restrictions |
| 48 | `EnforceArgumentRestrictions` | Enforce ABI argument constraints |
| 49 | `GvnCse` | Global value numbering and common subexpression elimination |
| 50 | `OriReassociateAndCommon` | Reassociation and commoning |
| 51 | `ExtractShaderConstsFinal` | Extract shader constants, final pass |
| 52 | `OriReplaceEquivMultiDefMov` | Replace equivalent multi-def MOVs |
| 53 | `OriPropagateVaryingFirst` | Varying propagation, first pass |
| 54 | `OriDoRematEarly` | Early rematerialization |
| 55 | `LateExpansion` | Late lowering of complex operations |
| 56 | `SpeculativeHoistComInsts` | Speculatively hoist common instructions |
| 57 | `RemoveASTToDefaultValues` | Remove AST nodes set to default values |
| 58 | `GeneralOptimizeLate` | Late GeneralOptimize |

### Group 4: Late Optimization (phases 59--95)

| Index | Phase Name | Purpose |
|---|---|---|
| 59 | `OriLoopFusion` | Fuse compatible loops |
| 60 | `DoVTGMultiViewExpansion` | Expand multi-view VTG operations |
| 61 | `OriPerformLiveDeadThird` | Liveness analysis, third pass |
| 62 | `OriRemoveRedundantMultiDefMov` | Remove redundant multi-def MOVs |
| 63 | `OriDoPredication` | If-conversion / predication |
| 64 | `LateOriCommoning` | Late value commoning |
| 65 | `GeneralOptimizeLate2` | Second late GeneralOptimize |
| 66 | `OriHoistInvariantsLate` | Late invariant hoisting |
| 67 | `DoKillMovement` | Move kill instructions for better scheduling |
| 68 | `DoTexMovement` | Move texture instructions for latency hiding |
| 69 | `OriDoRemat` | Main rematerialization pass |
| 70 | `OriPropagateVaryingSecond` | Varying propagation, second pass |
| 71 | `OptimizeSyncInstructions` | Optimize synchronization instructions |
| 72 | `LateExpandSyncInstructions` | Expand sync instructions to HW sequences |
| 73 | `ConvertAllMovPhiToMov` | Convert all MOV-phi to plain MOV |
| 74 | `ConvertToUniformReg` | Promote values to uniform registers |
| 75 | `LateArchOptimizeFirst` | Architecture-specific late optimization, first pass |
| 76 | `UpdateAfterOptimize` | Post-optimization bookkeeping |
| 77 | `AdvancedPhaseLateConvUnSup` | **Hook** -- before late unsupported op expansion |
| 78 | `LateExpansionUnsupportedOps` | Late lowering of unsupported operations |
| 79 | `OriHoistInvariantsLate2` | Second late invariant hoisting |
| 80 | `ExpandJmxComputation` | Expand JMX (join/merge) computations |
| 81 | `LateArchOptimizeSecond` | Architecture-specific late optimization, second pass |
| 82 | `AdvancedPhaseBackPropVReg` | **Hook** -- before back-copy propagation |
| 83 | `OriBackCopyPropagate` | Backward copy propagation |
| 84 | `OriPerformLiveDeadFourth` | Liveness analysis, fourth pass |
| 85 | `OriPropagateGmma` | GMMA/WGMMA propagation |
| 86 | `InsertPseudoUseDefForConvUR` | Insert pseudo use/def for uniform reg conversion |
| 87 | `FixupGmmaSequence` | Fix up GMMA instruction sequences |
| 88 | `OriHoistInvariantsLate3` | Third late invariant hoisting |
| 89 | `AdvancedPhaseSetRegAttr` | **Hook** -- before register attribute setting |
| 90 | `OriSetRegisterAttr` | Set register attributes (types, constraints) |
| 91 | `OriCalcDependantTex` | Calculate dependent texture operations |
| 92 | `AdvancedPhaseAfterSetRegAttr` | **Hook** -- after register attribute setting |
| 93 | `LateExpansionUnsupportedOps2` | Second late unsupported op expansion |
| 94 | `FinalInspectionPass` | Final IR validity checks |
| 95 | `SetAfterLegalization` | Mark legalization complete |

### Group 5: Scheduling and Register Allocation (phases 96--105)

| Index | Phase Name | Purpose |
|---|---|---|
| 96 | `ReportBeforeScheduling` | Diagnostic dump before scheduling |
| 97 | `AdvancedPhasePreSched` | **Hook** -- before scheduling |
| 98 | `BackPropagateVEC2D` | Back-propagate 2D vector instructions |
| 99 | `OriDoSyncronization` | Insert synchronization instructions |
| 100 | `ApplyPostSyncronizationWars` | Apply post-synchronization write-after-read fixes |
| 101 | `AdvancedPhaseAllocReg` | **Hook** -- register allocation |
| 102 | `ReportAfterRegisterAllocation` | Diagnostic dump after regalloc |
| 103 | `Get64bRegComponents` | Extract 64-bit register components |
| 104 | `AdvancedPhasePostExpansion` | **Hook** -- after post-expansion |
| 105 | `ApplyPostRegAllocWars` | Apply post-regalloc write-after-read fixes |

### Group 6: Post-Schedule and Code Generation (phases 106--131)

| Index | Phase Name | Purpose |
|---|---|---|
| 106 | `AdvancedPhasePostSched` | **Hook** -- after scheduling |
| 107 | `OriRemoveNopCode` | Remove NOP instructions |
| 108 | `OptimizeHotColdInLoop` | Hot/cold partitioning within loops |
| 109 | `OptimizeHotColdFlow` | Hot/cold partitioning across flow |
| 110 | `PostSchedule` | Post-scheduling fixups |
| 111 | `AdvancedPhasePostFixUp` | **Hook** -- after post-schedule fixup |
| 112 | `PlaceBlocksInSourceOrder` | Reorder blocks to match source order |
| 113 | `PostFixForMercTargets` | Mercury target-specific fixups |
| 114 | `FixUpTexDepBarAndSync` | Fix texture dependency barriers and sync |
| 115 | `AdvancedScoreboardsAndOpexes` | **Hook** -- before scoreboard generation |
| 116 | `ProcessO0WaitsAndSBs` | Process O0-level waits and scoreboards |
| 117 | `MercEncodeAndDecode` | Mercury encode to SASS and decode-verify |
| 118 | `MercExpandInstructions` | Expand macro instructions to SASS |
| 119 | `MercGenerateWARs1` | Generate write-after-read hazard stalls, pass 1 |
| 120 | `MercGenerateOpex` | Generate operand exchange stalls |
| 121 | `MercGenerateWARs2` | Generate write-after-read hazard stalls, pass 2 |
| 122 | `MercGenerateSassUCode` | Emit final SASS microcode |
| 123 | `ComputeVCallRegUse` | Compute virtual call register usage |
| 124 | `CalcRegisterMap` | Calculate final register map |
| 125 | `UpdateAfterPostRegAlloc` | Post-regalloc bookkeeping |
| 126 | `ReportFinalMemoryUsage` | Report final memory consumption |
| 127 | `AdvancedPhaseOriPhaseEncoding` | **Hook** -- before final encoding |
| 128 | `UpdateAfterFormatCodeList` | Update after code list formatting |
| 129 | `DumpNVuCodeText` | Dump NV microcode as text (debug) |
| 130 | `DumpNVuCodeHex` | Dump NV microcode as hex (debug) |
| 131 | `DebuggerBreak` | Debugger breakpoint (debug) |

### Group 7: Late Cleanup (phases 132--158)

| Index | Phase Name | Purpose |
|---|---|---|
| 132 | `UpdateAfterConvertUnsupportedOps` | Bookkeeping after late conversion |
| 133 | `MergeEquivalentConditionalFlow` | Merge equivalent conditional branches |
| 134 | `AdvancedPhaseAfterMidExpansion` | **Hook** -- after mid-expansion |
| 135 | `AdvancedPhaseLateExpandSyncInstructions` | **Hook** -- after late sync expansion |
| 136 | `LateMergeEquivalentConditionalFlow` | Late merge of equivalent conditionals |
| 137 | `LateExpansionUnsupportedOpsMid` | Mid-point late unsupported op expansion |
| 138 | `OriSplitHighPressureLiveRanges` | Split live ranges under high register pressure |
| 139--158 | *(architecture-specific)* | 20 additional phases with names in vtable `getString()` methods |

Phases 139--158 are not in the static name table at `off_22BD0C0`. Their names are returned by each phase's `getName()` virtual method. These are conditionally-enabled phases for specific architecture targets (SM variants) or optimization levels.

## AdvancedPhase Hook Points

The 16 AdvancedPhase entries are insertion points for architecture-specific or optimization-level-specific processing. All return `isNoOp() == true` by default. When activated (typically by NvOptRecipe configuration for a specific SM target), they execute additional transformations at precisely defined points in the pipeline:

| Index | Hook Name | Insertion Context |
|---|---|---|
| 4 | `AdvancedPhaseBeforeConvUnSup` | Before `ConvertUnsupportedOps` |
| 7 | `AdvancedPhaseAfterConvUnSup` | After `ConvertUnsupportedOps` |
| 47 | `AdvancedPhaseEarlyEnforceArgs` | Before `EnforceArgumentRestrictions` |
| 77 | `AdvancedPhaseLateConvUnSup` | Before `LateExpansionUnsupportedOps` |
| 82 | `AdvancedPhaseBackPropVReg` | Before `OriBackCopyPropagate` |
| 89 | `AdvancedPhaseSetRegAttr` | Before `OriSetRegisterAttr` |
| 92 | `AdvancedPhaseAfterSetRegAttr` | After `OriSetRegisterAttr` |
| 97 | `AdvancedPhasePreSched` | Before scheduling pipeline |
| 101 | `AdvancedPhaseAllocReg` | Register allocation entry point |
| 104 | `AdvancedPhasePostExpansion` | After post-regalloc expansion |
| 106 | `AdvancedPhasePostSched` | After scheduling |
| 111 | `AdvancedPhasePostFixUp` | After post-schedule fixup |
| 115 | `AdvancedScoreboardsAndOpexes` | Before scoreboard/opex generation |
| 127 | `AdvancedPhaseOriPhaseEncoding` | Before final instruction encoding |
| 134 | `AdvancedPhaseAfterMidExpansion` | After mid-level expansion |
| 135 | `AdvancedPhaseLateExpandSyncInstructions` | After late sync instruction expansion |

## Mercury Encoding Sub-Pipeline

Phases 113--122 form a self-contained sub-pipeline that transforms the optimized, register-allocated Ori IR into final SASS machine code via the Mercury encoding format:

```
PostFixForMercTargets (113)
  → FixUpTexDepBarAndSync (114)
    → [AdvancedScoreboardsAndOpexes hook (115)]
      → ProcessO0WaitsAndSBs (116)
        → MercEncodeAndDecode (117)      ← encode to SASS + decode for verification
          → MercExpandInstructions (118) ← expand remaining macros
            → MercGenerateWARs1 (119)    ← first WAR hazard pass
              → MercGenerateOpex (120)   ← operand exchange stalls
                → MercGenerateWARs2 (121)← second WAR hazard pass
                  → MercGenerateSassUCode (122) ← final microcode emission
```

"Mercury" is NVIDIA's internal name for the SASS encoding format on recent GPU architectures (Blackwell-era SM 100/103/110/120).

## Diagnostic Strings

| Address | String | Emitted By | Context |
|---|---|---|---|
| `0x22BC3B3` | `"[Pool Consumption = "` | `sub_C62200` | After all phases summary |
| `0x22BC416` | `"All Phases Summary"` | `sub_C64F70` | End of dispatch loop |
| (inline) | `"  ::  "` | `sub_C64310` | Phase timing line separator |
| (inline) | `"[Total "` | `sub_C64310` | Total memory delta |
| (inline) | `"[Freeable "` | `sub_C64310` | Freeable memory delta |
| (inline) | `"[Freeable Leaked "` | `sub_C64310` | Leaked memory delta |
| (inline) | `"Before "` / `"After "` | `sub_C64F70` | Phase execution diagnostic |

## Function Map

| Address | Size | Function | Confidence |
|---|---|---|---|
| `sub_C60D20` | 16 | Default phase table pointer | HIGH |
| `sub_C60D30` | 3,554 | Phase factory (159-case switch) | VERY HIGH |
| `sub_C60BD0` | 334 | Multi-function phase invoker | MEDIUM-HIGH |
| `sub_C61B20` | 1,753 | PhaseManager destructor | VERY HIGH |
| `sub_C62200` | 888 | Pool consumption reporter | VERY HIGH |
| `sub_C62580` | 253 | Timing record array resizer | HIGH |
| `sub_C62640` | 223 | Phase list resizer | HIGH |
| `sub_C62720` | 4,734 | PhaseManager constructor | VERY HIGH |
| `sub_C639A0` | 1,535 | Case-insensitive quicksort | HIGH |
| `sub_C63FA0` | 556 | Phase name table sort/rebuild | HIGH |
| `sub_C641D0` | 305 | Phase name-to-index lookup | VERY HIGH |
| `sub_C64310` | 3,168 | Per-phase timing reporter | VERY HIGH |
| `sub_C64F70` | 1,455 | Phase dispatch loop | VERY HIGH |

## Cross-References

- [Pass Inventory & Ordering](./index.md) -- full phase sequence and stage grouping
- [GeneralOptimize Bundles](./general-optimize.md) -- phases 13, 29, 37, 46, 58, 65
- [Synchronization & Barriers](./sync-barriers.md) -- phases 26, 71, 72, 99, 100
- [Liveness Analysis](./liveness.md) -- phases 10, 16, 33, 61, 84
- [Mercury Encoder](../codegen/mercury.md) -- phases 113--122
- [Memory Pool Allocator](../infra/memory-pools.md) -- pool allocation infrastructure used by PhaseManager
- [Optimization Levels](../config/opt-levels.md) -- how opt level controls phase behavior
- [DUMPIR & NamedPhases](../config/dumpir.md) -- phase name resolution for debug output
