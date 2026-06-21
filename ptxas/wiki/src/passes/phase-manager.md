# Phase Manager Infrastructure

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

The PhaseManager is the central orchestration layer in ptxas. It **registers 159 phase objects** for the optimization and code generation pipeline, constructs each as a polymorphic object via an abstract factory, and drives execution through a virtual dispatch loop. The **default driver dispatches 157 of the 159** registered phases (IDs 0–156): construct all 159 phase objects, fetch the 157-entry default order array, iterate it calling `execute()` on each, optionally collect per-phase timing and memory statistics, then tear down. The two trailing registered phases — ID 157 `DebuggerBreak` and ID 158 `NOP` — are constructed but not in the default order; they run only through the recipe/named-phases path (OCG knob 298). The PhaseManager also hosts an optional NvOptRecipe sub-manager (440 bytes) for architecture-specific "advanced phase" hooks that inject additional processing at 16 defined points in the pipeline.

The design is a textbook Strategy + Abstract Factory pattern: a 159-case switch statement maps phase indices to vtable pointers, each vtable provides `execute()`, `isNoOp()`, and `getIndex()` virtual methods, and the dispatch loop iterates a flat order array (the 157-entry identity table at `0x22BEEA0`) that defines execution order. This makes the pipeline fully data-driven — reordering, disabling, or injecting phases requires only modifying the order array, not the dispatch logic.

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
| **Registered phases** | 159 (all named in `off_22BD0C0`) |
| **Dispatched by default** | 157 (IDs 0–156); IDs 157/158 are recipe-only |
| **AdvancedPhase hooks** | 16 insertion points (banner-suppressed by default, `execute()` still runs) |
| **Default order table** | `0x22BEEA0` — identity `[0..156]` (157 entries) |
| **Order accessor** | `sub_C60D20` — returns `(&0x22BEEA0, 157)` in `(rax, rdx)` |
| **Phase name table** | Static array at `off_22BD0C0` (159 string pointers, 1272 bytes) |
| **Vtable range** | `off_22BD5C8` (phase 0) through `off_22BEE78` (phase 158) |
| **Callers** | `sub_7FB6C0` (compilation driver, default path), `sub_9F63D0` (recipe/named-phases path) |

## PhaseManager Object Layout

The PhaseManager is a plain C++ object (no vtable of its own) allocated by the compilation driver. Minimum size is 112 bytes, though the full extent depends on whether timing and NvOptRecipe are enabled.

```text
PhaseManager (112+ bytes)
  +0    int64     compilation_unit      // back-pointer to owning compilation unit
  +8    int64*    allocator             // pool allocator (from compilation_unit->field_16)
  +16   void*     sorted_name_table     // sorted {name_ptr, index} pairs for binary search
  +24   int       sorted_name_count
  +28   int       sorted_name_capacity
  +32   int64*    allocator2            // copy of allocator (for phase list ops)
  +40   void*     phase_list            // array of 16-byte {phase_ptr, pool_ptr} pairs
  +48   int       phase_list_count      // always 159 (the registered count, PM+0x68/+0x6c)
                                        //   note: the *dispatch* count is the separate 157
                                        //   returned by sub_C60D20, not this field
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

```text
Phase (16 bytes)
  +0    vtable*   // points to one of 159 vtable instances
  +8    void*     // pool pointer (memory pool for phase-local allocations)
```

The vtable provides the interface contract:

| Vtable offset | Method | Signature |
|---|---|---|
| `+0` | `execute` | `void execute(phase*, compilation_context*)` |
| `+8` | `getIndex` | `int getIndex(phase*)` — returns the factory/table index (0–158) |
| `+16` | `isNoOp` | `bool isNoOp(phase*)` — returns 0 for active phases, 1 for gates skipped by default |
| `+24` | *(NULL)* | Unused — NULL in all 159 vtable instances |
| `+32` | *(NULL)* | Unused — NULL in all 159 vtable instances |

The vtable addresses span `off_22BD5C8` (phase 0) through `off_22BEE78` (phase 158), with a stride of `0x28` (40 bytes) between consecutive entries. All vtables reside in `.data.rel.ro`.

## Phase Factory — `sub_C60D30`

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

Called exclusively by the constructor (`sub_C62720`). The factory is a 159-arm jump table (`cmp $0x9e` upper bound, valid 0–158); all 159 arms are distinct. Arm 157 builds the `DebuggerBreak` phase (ctor at `0xc60d61`) and arm 158 builds the `NOP` phase (ctor at `0xc61ae8`). Both objects are constructed on every compile, but neither ID appears in the default 157-entry order table, so their `execute()` is never reached unless a recipe explicitly schedules them.

## Registered (159) vs Dispatched (157)

Two distinct counts apply to the PhaseManager, and they must not be conflated:

| Object | Builder / accessor | What it is | Count |
|---|---|---|---|
| Phase-name registry (rodata) | `off_22BD0C0` | `const char*[]` of phase names | 159 ptrs (1272 bytes) |
| PhaseManager name array | built by `sub_C62720` | runtime copy of the registry | 159 (`PM+0x6c = 159`) |
| Default order table | `0x22BEEA0` | `int32[]` of phase IDs to run; identity `0..156` | **157** (`0x9D`) |

The order accessor `sub_C60D20` returns a `(pointer, count)` pair: the order-table pointer in `rax` and `0x9D = 157` in `rdx`. In the Hex-Rays C view the inferred prototype returns a single pointer, so the `rdx` count is dropped and the body shows only `return &unk_22BEEA0;`. The driver (`sub_7FB6C0`) reloads only `rdi`/`rsi` between the two calls, so `rdx = 157` flows untouched into the third integer argument of the dispatch loop `sub_C64F70`, where it bounds the loop at `&order[157]`. The table holds the identity sequence `[0..156]`; the bytes after entry 156 belong to a different rodata structure. Registry = 159; default schedule = 157; `DebuggerBreak` (157) and `NOP` (158) are registered but not dispatched by default.

## Construction Sequence — `sub_C62720`

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
- **159** — total phase count, used as loop bound and array capacities
- **1272** — `159 * 8`, phase name pointer table size in bytes
- **440** — NvOptRecipe sub-manager object size
- **0x2030007** (33,739,079) — timing sentinel magic value
- **Option 17928** — enables per-phase timing/memory reporting
- **Option 391** — enables NvOptRecipe sub-manager

## Destruction Sequence — `sub_C61B20`

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

## Phase Dispatch Loop — `sub_C64F70`

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
than being omitted.  This means `--ftime` output shows a row for all 157 dispatched
phase slots, with no-op phases contributing empty rows.

The "Before" / "After" diagnostic strings use an interesting encoding trick: the string `"Before "` is stored as the 64-bit integer `0x2065726F666542` in little-endian, allowing the compiler to emit a single `mov` instruction instead of a `memcpy`. The string `"After "` is stored as two writes: a 4-byte `dword 0x65746641` ("Afte") plus a 2-byte `word 0x2072` ("r ") plus a null terminator byte, totaling 7 bytes at `0xC651F7`--`0xC65208`.

## Phase Name Lookup — `sub_C641D0`

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

```text
<indent><phase_name>  ::  [Total 1234 KB]  [Freeable 567 KB]  [Freeable Leaked 12 KB] (2%)
```

The reporter computes three memory deltas from snapshot pairs:

| Metric | Helper | Meaning |
|---|---|---|
| Total | `sub_8DAE20` | Total memory allocated since snapshot |
| Freeable | `sub_8DAE30` | Memory eligible for release |
| Freeable Leaked | `sub_8DAE40` | Freeable memory not actually released |

Size formatting thresholds:
- 0–1023: raw bytes (suffix `B`)
- 1024–10,485,760: kilobytes with 3 decimal places (suffix `KB`)
- above 10 MB: megabytes with 3 decimal places (suffix `MB`)

After all phases complete, the loop prints an "All Phases Summary" line using the same reporter, then calls `sub_C62200` to print the pool consumption total:

```text
[Pool Consumption = 45.678 MB]
```

### Timing Record Format

Each timing entry is 32 bytes:

```text
Timing Record (32 bytes)
  +0    int       phase_index       // -1 for sentinel
  +8    int64     phase_name        // string pointer, or 0x2030007 for sentinel
  +16   int64     timing_value      // elapsed time
  +24   int       memory_flags      // opt level / additional metrics
```

Records are stored in a growable array at `compilation_unit+1560`. Growth uses a 1.5x strategy: `new_capacity = max(old + old/2 + 1, requested)`.

## NvOptRecipe Sub-Manager (440 bytes)

When option 391 is enabled, the constructor creates a 440-byte NvOptRecipe sub-manager at `PhaseManager+56`. This object provides the runtime for "AdvancedPhase" hooks — the 16 phases that are no-ops by default but can be activated for architecture-specific or optimization-level-specific processing. The NvOpt level (0–5) controls per-phase aggressiveness independently of the `-O` CLI level: `-O` gates which phases run at all, while the NvOpt level controls how aggressively active phases behave.

### Object Layout

```text
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

**Ref-Counted List Node (24 bytes)** — used at `+16`, `+392`, `+432`:

```text
RefCountedListNode (24 bytes)
  +0    int64     refcount        // manual shared_ptr: decrement-and-destroy
  +8    void*     next            // singly-linked list chain
  +16   void*     allocator       // for self-deallocation when refcount → 0
```

When the refcount reaches zero, the destructor walks the `next` chain freeing each node, then frees the head node itself through the allocator at `+16`.

**Hash Bucket Entry (24 bytes)** — array at `+408`:

```text
HashBucketEntry (24 bytes)
  +0    void*     chain_head      // first element in bucket chain
  +8    void*     chain_sentinel  // end-of-chain sentinel
  +16   int32     chain_count     // number of elements in this bucket
```

**Timing Record (584 bytes)** — array at `+344`:

```text
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

The constructor (`sub_C62720`, lines 356–850 in decompilation) performs these steps:

1. **Check option 391** — fast path: `*(config_obj[9] + 28152) != 0`; slow path: virtual call with argument `391`. If disabled, skip entirely.

2. **Read option 391 value** — the value is the `recipe_data` pointer. Fast path checks type tag `5` (int64) at config offset `28152`, reads the 64-bit value at offset `28160`. This is an externally-provided pointer, not computed locally.

3. **Allocate 440 bytes** from the pool allocator at `compilation_unit->field_16`.

4. **Initialize core fields** — back-pointers at `+0`/`+8`, `node_pool` at `+16` (24-byte ref-counted node, refcount=1), zero `+24`/`+32`/`+40`, store `recipe_data` at `+312`.

5. **Initialize timing** — zero `+344`, set `+352` to `-1` (empty sentinel), zero `+360`, copy allocator to `+336` and `+368`.

6. **Allocate sorted_array** — initial capacity 8 entries (32 bytes), pre-fill 7 entries, set `+384` = 7, `+388` = 8.

7. **Allocate `ref_counted_list_2`** at `+392` (24-byte node, refcount=1), zero `+400`/`+408`/`+416`.

8. **Allocate `shared_list`** at `+432` (24-byte node, refcount=1).

9. **Inherit from previous recipe** — if `PhaseManager+56` already holds an NvOptRecipe from a prior compilation unit:
   - Decrement old `shared_list` refcount; free if zero
   - Migrate hash bucket chains from old recipe to new `ref_counted_list_2`
   - Walk old timing records backward (stride 584), freeing sub-allocations
   - Drain old secondary hash table, release old `node_pool`
   - Free old NvOptRecipe object

10. **Install** — set `PhaseManager+56` = new recipe, `PhaseManager+64` = allocator.

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

Valid levels are 0–5. The level is consumed as a bitmask `1 << nvopt_level`, passed to a vtable call that dispatches on a recipe configuration byte at target descriptor offset `35280` (8-case switch: cases 0–5, 7). This byte controls which recipe application mode is used for the target architecture.

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

## NvOptRecipe String Applier — `sub_9F4040`

The 440-byte sub-manager described above is the *runtime container*; the actual string-driven phase reordering lives in a separate 9,093-byte function called from the alternate compilation entry. Two top-level entry points exist:

```c
// sub_7FB6C0 (compilation driver) at line 38–47
v3 = phase_manager_options(...);
v4 = (v3 == sub_6614A0) ? (config[9][21456] != 0)   // fast path: type tag of option 298
                        : v3(phase_mgr, 298);        // slow path: virtual call
if ( v4 )
    sub_9F63D0(cu);                                  // recipe path
else
    { sub_C62720(stack, cu); sub_C64F70(stack, sub_C60D20()); ... }   // default path
```

When option **298** is set (recipe-string option, type tag 5 = string pointer at config offset 21464), control diverges into `sub_9F63D0`, a 12-line trampoline:

```c
// sub_9F63D0 -- complete decompilation
__int64 sub_9F63D0(__int64 cu) {
    char pm[112];           // PhaseManager stack object
    _QWORD order[129];      // 1024-byte phase order array (256 int32 slots + slack)
    sub_C62720(pm, cu);                  // construct PhaseManager
    memset(order, 0, 0x400);
    LODWORD(order[0]) = 158;             // sentinel
    sub_9F4040(cu, (__int64)pm, order);  // build per-CU phase order from recipe
    sub_C64F70(pm, order);               // dispatch using the modified order
    return sub_C61B20(pm);               // destruct
}
```

`sub_9F4040` (the recipe applier) is responsible for parsing the option-298 string and writing the resulting phase index sequence into `order[]`. It supports **three operating modes** plus DCE/CopyProp injection slots:

1. **NamedPhases mode** — explicit ordered phase-name list
2. **`pNNN` mode** — explicit per-slot phase index override (243 slots)
3. **shuffle mode** — start from default order, then apply `reps` rounds of six parameterized swaps

### Recipe String Grammar

The recipe string is consumed by the generic key/value tokenizer `sub_798B60` ("NamedPhases::ParsePhaseList") at lines 442–477:

```c
// sub_798B60 token loop -- comma-only separator
v42 = 1;                                  // 1 = expect key, 0 = expect value
while ( (token = strtok_r(s, ",", &save_ptr)) ) {
    if ( v42 ) keys[N]   = token;         // even tokens -> keys array
    else       values[N] = token, N++;    // odd tokens  -> values array
    v42 ^= 1;
    raw[i++] = token;                     // every token also written sequentially
    s = NULL;
}
```

Three parallel buffers are populated:

| Buffer | Indexing | Holds |
|---|---|---|
| `s2[256]` | by key position | Even-numbered tokens (the directive names) |
| `nptr[256]` | by key position | Odd-numbered tokens (the directive values) |
| `v343[520]` | by raw position | Every token in source order (used by NamedPhases mode to read phase-name lists of arbitrary length) |

Concretely, the recipe is a flat **comma-delimited** sequence with no `=` sign. To set `swap1=3`, the actual string is:

```text
swap1,3
```

Multiple directives chain with commas:

```text
shuffle,1,reps,2,swap1,3,swap2,5,swap3,11,swap4,17,swap5,23,swap6,31
```

### Three Operating Modes

After tokenizing, `sub_9F4040` searches the `s2[]` key array in priority order:

```c
// Pseudocode for sub_9F4040 control flow
fill dest[0..255] with sentinel index 158;        // line 351-356
parse_recipe(option_298_string, s2, nptr, v343);
v340 = recipe_present;                            // line 363, 1770

// PRIORITY 1 -- NamedPhases mode
if ( find_key(s2, "NamedPhases") && nptr[k] ) {   // line 374-396
    for ( i = 0; v343[i+1] != NULL && i < 256; ++i ) {
        name = v343[i + 1];                       // skip "NamedPhases" itself
        dest[i] = (*name == '-') ? 158            // dash-prefixed = sentinel/skip
                                 : phase_lookup(name);   // sub_C641D0
    }
    goto dispatch;
}

// PRIORITY 2 -- pNNN mode (243 explicit per-slot indices)
if ( find_key(s2, "p%d") ) {                      // line 463-509
    dest[0..2] = first 12 bytes of default phase table;
    for ( v18 = 0; v18 < 243; ++v18 ) {
        sprintf(s, "p%d", v18);
        if ( find_key(s2, s) && nptr[k] )
            dest[v18] = clamp(strtol(nptr[k]), 0, 159);
    }
    goto dispatch;
}

// PRIORITY 3 -- shuffle mode (default order + DCE/CopyProp injection + swaps)
if ( find_key(s2, "shuffle") && nptr[k] ) {       // line 841-876
    parse_int(s2, "reps",  &reps);    // -> v250  (loop iteration count, clamped 0..256)
    parse_int(s2, "swap1", &swap1);   // -> v248  (swap base offset 1)
    parse_int(s2, "swap2", &swap2);   // -> v246
    parse_int(s2, "swap3", &swap3);   // -> v232
    parse_int(s2, "swap4", &swap4);   // -> v48
    parse_int(s2, "swap5", &swap5);   // -> v9
    parse_int(s2, "swap6", &swap6);   // -> v230
    parse_int(s2, "dce1",  &dce1);    // -> v51   (DCE injection slot 1)
    parse_int(s2, "dce2",  &dce2);    // -> v234
    parse_int(s2, "dce3",  &dce3);    // -> v240
    parse_int(s2, "cpy1",  &cpy1);    // -> v236  (CopyProp injection slot 1)
    parse_int(s2, "cpy2",  &cpy2);    // -> v222
    parse_int(s2, "cpy3",  &cpy3);    // -> v42

    // Phase 1: copy default phase table, injecting DCE/CopyProp at marked slots
    write = 0;
    for ( read = 0; read < default_count; ++read ) {
        if ( read == dce1 || read == dce2 || read == dce3 )
            dest[write++] = phase_lookup("OriPerformLiveDead");   // line 1556-1564
        if ( read == cpy1 || read == cpy2 || read == cpy3 )
            dest[write++] = phase_lookup("OriCopyProp");          // line 1645-1650
        dest[write++] = default_phase_table[read];                // line 1669-1671
    }
    N = write;                                    // post-injection length

    // Phase 2: bubble-shuffle (lines 1689-1729)
    for ( i = 0; i < reps; ++i ) {
        swap_pair(dest, swap1, i, N);
        swap_pair(dest, swap2, i, N);
        swap_pair(dest, swap3, i, N);
        swap_pair(dest, swap4, i, N);
        swap_pair(dest, swap5, i, N);
        swap_pair(dest, swap6, i, N);
    }
}

void swap_pair(int dest[], int base, int i, int N) {
    int a = (base + i)         % N;
    int b = (a    + i + 1)     % N;       // == (base + 2i + 1) % N
    swap(dest[a], dest[b]);
}
```

### The Six Swap Slots

The headline finding: **`swap1`--`swap6` do not target named phase pairs**. Each is a **user-supplied integer base offset** into the phase order array; the swap operation that uses it pairs `dest[base+i]` with `dest[base+2i+1]` (mod `N`) for every iteration `i` of the `reps` loop. All six slots default to **0** if absent from the recipe, and the entire shuffle block is skipped unless `reps > 0`. The slots are otherwise interchangeable — the parser exists solely to give a recipe author six independent base offsets per `reps` round, so a single recipe can perturb up to six widely separated regions of the pipeline simultaneously.

| Slot | Stored at | Value source | Default | Effect per iteration `i` |
|---|---|---|---|---|
| `swap1` | local `v248` | `strtol(nptr["swap1"], 0, 10)`, clamped `[0, 256]` | `0` | `swap(dest[(swap1+i)%N], dest[(swap1+2i+1)%N])` |
| `swap2` | local `v246` | `strtol(nptr["swap2"], 0, 10)`, clamped `[0, 256]` | `0` | `swap(dest[(swap2+i)%N], dest[(swap2+2i+1)%N])` |
| `swap3` | local `v232` | `strtol(nptr["swap3"], 0, 10)`, clamped `[0, 256]` | `0` | `swap(dest[(swap3+i)%N], dest[(swap3+2i+1)%N])` |
| `swap4` | local `v48`  | `strtol(nptr["swap4"], 0, 10)`, clamped `[0, 256]` | `0` | `swap(dest[(swap4+i)%N], dest[(swap4+2i+1)%N])` |
| `swap5` | local `v9`   | `strtol(nptr["swap5"], 0, 10)`, clamped `[0, 256]` | `0` | `swap(dest[(swap5+i)%N], dest[(swap5+2i+1)%N])` |
| `swap6` | local `v230` | `strtol(nptr["swap6"], 0, 10)`, clamped `[0, 256]` | `0` | `swap(dest[(swap6+i)%N], dest[(swap6+2i+1)%N])` |

`N` is the post-injection phase count (= default count if no `dceN`/`cpyN` slots fire, otherwise `default + (number of dce hits) + (number of cpy hits)`). The number of swap pairs executed by a recipe is therefore exactly `6 * reps`. With `reps == 0` (the default) the loop is fully skipped, even if all six `swapN` directives are set — so `swap1..swap6` are inert without an accompanying `reps,N` (with `N >= 1`).

### Vestigial Slots

**No swap slot is vestigial.** All six are read independently in the parser (lines 950, 1007, 1061, 1119, 1162, 1202) and all six are dereferenced once per `reps` iteration in the swap loop (lines 1695, 1700, 1705, 1710, 1715, 1720). Removing any one of them would change the observable behavior of any recipe that sets a non-zero value for that key. No string reference to `swap0` or `swap7` exists anywhere in the binary.

The six-slot count appears to be a hard-coded budget rather than a list of "named phase pairs", and the matching `dce1/2/3` + `cpy1/2/3` injection budget is similarly fixed. The naming convention (`swapN`, `dceN`, `cpyN`) suggests the intended use was to give a recipe author six independent perturbation points, three independent DCE injection points, and three independent CopyProp injection points — a total of 12 + 6 = 18 independent integer parameters that together describe a deterministic transformation of the default 159-phase order.

### How the Swap Modifies the Phase Sequence

The swap installation **physically reorders the `dest[]` array** before it is handed to `sub_C64F70` for dispatch. There is no swap-attribute that the dispatcher honors at run time — by the time `sub_C64F70` receives `order`, every swap has already happened in `sub_9F4040`. The dispatcher itself is unmodified by the recipe; it walks the array linearly, calling `execute()` on whatever phase indices are present.

This has two consequences:

1. **Re-ordering is bounded by `reps`**, not by recipe complexity. A recipe with `reps,10000` will run 60,000 swap operations on a ~159-element array regardless of how many `swapN` keys are set.
2. **The same phase index can appear multiple times** if the swap pattern produces it — the dispatch loop will then `execute()` that phase multiple times. There is no de-duplication step. Recipes that abuse high `reps` values can trivially produce sequences with phases run twice, run zero times, or run out of dependency order; the dispatcher's only validation is the per-phase index range check.

### Worked Example

Default phase order (first 12 entries from `0x22BEEA0`):

```text
[0]  OriCheckInitialProgram
[1]  ApplyNvOptRecipes
[2]  PromoteFP16
[3]  AnalyzeControlFlow
[4]  AdvancedPhaseBeforeConvUnSup
[5]  ConvertUnsupportedOps
[6]  SetControlFlowOpLastInBB
[7]  AdvancedPhaseAfterConvUnSup
[8]  OriCreateMacroInsts
[9]  ReportInitialRepresentation
[10] EarlyOriSimpleLiveDead
[11] ReplaceUniformsWithImm
```

Recipe string passed via option 298:

```text
shuffle,1,reps,2,swap1,3,swap2,8,dce1,5
```

Step-by-step expansion:

1. **Phase 1 (DCE injection)**: `dce1 = 5`. Walking the default table, when `read == 5` (`ConvertUnsupportedOps`), inject `OriPerformLiveDead` *before* it. The `dest[]` prefix becomes:
    ```text
    [0]  OriCheckInitialProgram
    [1]  ApplyNvOptRecipes
    [2]  PromoteFP16
    [3]  AnalyzeControlFlow
    [4]  AdvancedPhaseBeforeConvUnSup
    [5]  OriPerformLiveDead    <- injected
    [6]  ConvertUnsupportedOps
    [7]  SetControlFlowOpLastInBB
    [8]  AdvancedPhaseAfterConvUnSup
    [9]  OriCreateMacroInsts
    ...
    ```
    `N` becomes `default_count + 1`.

2. **Phase 2 (shuffle), iteration `i = 0`**:
    - `swap1`: `swap(dest[(3+0)%N], dest[(3+0+1)%N]) = swap(dest[3], dest[4])`
        → `AnalyzeControlFlow` ↔ `AdvancedPhaseBeforeConvUnSup`
    - `swap2`: `swap(dest[(8+0)%N], dest[(8+0+1)%N]) = swap(dest[8], dest[9])`
        → `AdvancedPhaseAfterConvUnSup` ↔ `OriCreateMacroInsts`
    - `swap3..swap6` all default to `0`: `swap(dest[(0+0)%N], dest[(0+0+1)%N]) = swap(dest[0], dest[1])` — executed **four times**, which is two pairs of net no-ops on `dest[0]` and `dest[1]`.

3. **Phase 2 (shuffle), iteration `i = 1`**:
    - `swap1`: `swap(dest[(3+1)%N], dest[(3+1+1+1)%N]) = swap(dest[4], dest[6])`
    - `swap2`: `swap(dest[(8+1)%N], dest[(8+1+1+1)%N]) = swap(dest[9], dest[11])`
    - `swap3..swap6` (base 0): `swap(dest[(0+1)%N], dest[(0+1+1+1)%N]) = swap(dest[1], dest[3])` ×4 — two no-op pairs.

After both iterations, the prefix of `dest[]` is a deterministic permutation of the default order with `OriPerformLiveDead` injected at slot 5 and four pair-swaps applied. The dispatcher then executes phases in the resulting order.

### Recipe-Mode Selection Priority

If the recipe contains keys for multiple modes simultaneously, the parser uses the first mode it finds in this fixed order:

1. `NamedPhases` — highest priority; consumes all subsequent tokens as phase names from `v343[1..]`
2. `pNNN` — if no `NamedPhases` and any `p<digits>` key is present
3. `shuffle` — only checked if neither of the above matched; entry condition is the literal string `shuffle` AND the string `reps` with a non-zero value

DCE and CopyProp injection (`dce1..3`, `cpy1..3`) are **only honored in shuffle mode**; they are read inside the shuffle-mode branch (lines 1486–1666) and have no effect on `NamedPhases` or `pNNN` modes.

### Recipe Applier Constants

| Value | Meaning |
|---|---|
| **298** | Option ID enabling the recipe applier (string-typed, type tag 5) |
| **21456** | Option 298 type tag offset in config storage (`config[9] + 21456`) |
| **21464** | Option 298 string pointer offset (`config[9] + 21464`) |
| **256** | Maximum number of key/value pairs in recipe string (parser buffer size) |
| **243** | Maximum `pNNN` slot index (`p0`..`p242` — the 159 phase slots plus headroom) |
| **159** | Phase index clamp ceiling for `pNNN` values (`v121 > 159 ? 159 : v121`) |
| **0..256** | `swapN` / `repsN` / `dceN` / `cpyN` clamp range (`strtol` then clamped) |
| **6** | Number of `swap` slots (no `swap0` or `swap7` strings exist in the binary) |
| **3** | Number of `dceN` and `cpyN` injection slots each |
| **`OriPerformLiveDead`** | Phase name injected by `dceN` (resolved via `sub_C641D0` at line 1556) |
| **`OriCopyProp`** | Phase name injected by `cpyN` (resolved via `sub_C641D0` at line 1648) |

## Multi-Function Dispatch — `sub_C60BD0`

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

> **Stage numbering.** The 7 groups below are a coarse summary of the 159-phase OCG pipeline. The authoritative fine-grained grouping is the 10-stage scheme in the [Pass Inventory](../passes/index.md) (OCG-Stage 1–10). The 7-group table here collapses several of those stages for brevity; phase boundaries differ slightly. When citing a stage by number, prefer the Pass Inventory's 10-stage numbering.

### Group 1: Initial Setup (phases 0–12)

| Index | Phase Name | Purpose |
|---|---|---|
| 0 | `OriCheckInitialProgram` | Validate initial Ori IR |
| 1 | `ApplyNvOptRecipes` | Apply NvOptRecipe transformations |
| 2 | `PromoteFP16` | Promote FP16 operations where beneficial |
| 3 | `AnalyzeControlFlow` | Build/analyze control flow graph |
| 4 | `AdvancedPhaseBeforeConvUnSup` | **Hook** — before unsupported op conversion |
| 5 | `ConvertUnsupportedOps` | Lower unsupported operations to supported sequences |
| 6 | `SetControlFlowOpLastInBB` | Mark control flow ops as last in basic block |
| 7 | `AdvancedPhaseAfterConvUnSup` | **Hook** — after unsupported op conversion |
| 8 | `OriCreateMacroInsts` | Create macro instruction patterns |
| 9 | `ReportInitialRepresentation` | Diagnostic dump of initial IR |
| 10 | `EarlyOriSimpleLiveDead` | Early dead code elimination |
| 11 | `ReplaceUniformsWithImm` | Replace uniform register loads with immediates |
| 12 | `OriSanitize` | IR consistency checks |

### Group 2: Early Optimization (phases 13–36)

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
| 27 | `AnalyzeUniformsForSpeculation` | Analyze constant bank accesses for speculation safety |
| 28 | `SinkRemat` | Sink rematerializable instructions |
| 29 | `GeneralOptimize` | Main GeneralOptimize pass |
| 30 | `DoSwitchOptSecond` | Switch optimization, second pass |
| 31 | `OriLinearReplacement` | Replace complex patterns with linear sequences |
| 32 | `CompactLocalMemory` | Compact local memory layout |
| 33 | `OriPerformLiveDeadSecond` | Liveness analysis, second pass |
| 34 | `ExtractShaderConstsFirst` | Extract shader constants, first pass |
| 35 | `OriHoistInvariantsEarly` | Early loop-invariant hoisting |
| 36 | `EmitPSI` | Emit program state information |

### Group 3: Mid-Level Optimization (phases 37–58)

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
| 47 | `AdvancedPhaseEarlyEnforceArgs` | **Hook** — before argument restrictions |
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

### Group 4: Late Optimization (phases 59–95)

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
| 77 | `AdvancedPhaseLateConvUnSup` | **Hook** — before late unsupported op expansion |
| 78 | `LateExpansionUnsupportedOps` | Late lowering of unsupported operations |
| 79 | `OriHoistInvariantsLate2` | Second late invariant hoisting |
| 80 | `ExpandJmxComputation` | Expand JMX (join/merge) computations |
| 81 | `LateArchOptimizeSecond` | Architecture-specific late optimization, second pass |
| 82 | `AdvancedPhaseBackPropVReg` | **Hook** — before back-copy propagation |
| 83 | `OriBackCopyPropagate` | Backward copy propagation |
| 84 | `OriPerformLiveDeadFourth` | Liveness analysis, fourth pass |
| 85 | `OriPropagateGmma` | GMMA/WGMMA propagation |
| 86 | `InsertPseudoUseDefForConvUR` | Insert pseudo use/def for uniform reg conversion |
| 87 | `FixupGmmaSequence` | Fix up GMMA instruction sequences |
| 88 | `OriHoistInvariantsLate3` | Third late invariant hoisting |
| 89 | `AdvancedPhaseSetRegAttr` | **Hook** — before register attribute setting |
| 90 | `OriSetRegisterAttr` | Set register attributes (types, constraints) |
| 91 | `OriCalcDependantTex` | Calculate dependent texture operations |
| 92 | `AdvancedPhaseAfterSetRegAttr` | **Hook** — after register attribute setting |
| 93 | `LateExpansionUnsupportedOps2` | Second late unsupported op expansion |
| 94 | `FinalInspectionPass` | Final IR validity checks |
| 95 | `SetAfterLegalization` | Mark legalization complete |

### Group 5: Scheduling and Register Allocation (phases 96–105)

| Index | Phase Name | Purpose |
|---|---|---|
| 96 | `ReportBeforeScheduling` | Diagnostic dump before scheduling |
| 97 | `AdvancedPhasePreSched` | **Hook** — before scheduling |
| 98 | `BackPropagateVEC2D` | Back-propagate 2D vector instructions |
| 99 | `OriDoSyncronization` | Insert synchronization instructions |
| 100 | `ApplyPostSyncronizationWars` | Apply post-synchronization write-after-read fixes |
| 101 | `AdvancedPhaseAllocReg` | **Hook** — register allocation |
| 102 | `ReportAfterRegisterAllocation` | Diagnostic dump after regalloc |
| 103 | `Get64bRegComponents` | Extract 64-bit register components |
| 104 | `AdvancedPhasePostExpansion` | **Hook** — before post-RA expansion worker (phase 127) |
| 105 | `ApplyPostRegAllocWars` | Apply post-regalloc write-after-read fixes |

### Group 6: Post-Schedule and Code Generation (phases 106–131)

| Index | Phase Name | Purpose |
|---|---|---|
| 106 | `AdvancedPhasePostSched` | **Hook** — before post-scheduling worker (phase 110); writes `ctx+1552=14` |
| 107 | `OriRemoveNopCode` | Remove NOP instructions |
| 108 | `OptimizeHotColdInLoop` | Hot/cold partitioning within loops |
| 109 | `OptimizeHotColdFlow` | Hot/cold partitioning across flow |
| 110 | `PostSchedule` | Post-scheduling fixups |
| 111 | `AdvancedPhasePostFixUp` | **Hook** — before post-fixup worker (phase 140 `PostFixUp`); writes `ctx+1552=20` |
| 112 | `PlaceBlocksInSourceOrder` | Reorder blocks to match source order |
| 113 | `PostFixForMercTargets` | Mercury target-specific fixups |
| 114 | `FixUpTexDepBarAndSync` | Fix texture dependency barriers and sync |
| 115 | `AdvancedScoreboardsAndOpexes` | **Hook** — before scoreboard generation |
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
| 127 | `AdvancedPhaseOriPhaseEncoding` | **Hook** — before final encoding |
| 128 | `UpdateAfterFormatCodeList` | Update after code list formatting |
| 129 | `DumpNVuCodeText` | Dump NV microcode as text (debug) |
| 130 | `DumpNVuCodeHex` | Dump NV microcode as hex (debug) |
| 131 | `DebuggerBreak` | Debugger breakpoint (debug) |

### Group 7: Late Cleanup (phases 132–158)

| Index | Phase Name | Purpose |
|---|---|---|
| 132 | `UpdateAfterConvertUnsupportedOps` | Bookkeeping after late conversion |
| 133 | `MergeEquivalentConditionalFlow` | Merge equivalent conditional branches |
| 134 | `AdvancedPhaseAfterMidExpansion` | **Hook** — after mid-expansion (Type-C, writes `pipeline_progress = 3`) |
| 135 | `AdvancedPhaseLateExpandSyncInstructions` | **Hook** — after late sync expansion (Type-B, vtable-override) |
| 136 | `LateMergeEquivalentConditionalFlow` | Late merge of equivalent conditionals |
| 137 | `LateExpansionUnsupportedOpsMid` | Mid-point late unsupported op expansion |
| 138 | `OriSplitHighPressureLiveRanges` | Split live ranges under high register pressure |
| 139 | `ProcessO0WaitsAndSBs` | sm50+ conservative scoreboard insertion (`sm_version > 0x3FFF` gate); execute `sub_C5E2A0` |
| 140 | `PostFixUp` | Target vtable+0x148 post-fixup dispatch; execute `sub_C5E270` |
| 141 | `MercConverter` | Second MercConverter pass re-lowering opt-introduced PTX opcodes; execute `sub_C60300` -> `sub_9F3760` |
| 142 | `MercEncodeAndDecode` | Ori -> Mercury encode + round-trip decode verification; execute `sub_C60310` -> `sub_18F21F0` |
| 143 | `MercExpandInstructions` | Mercury pseudo-instruction expansion (`ctx+0x570` bit 5); execute `sub_C60320` -> `sub_C3DFC0` |
| 144 | `MercGenerateWARs1` | WAR-hazard annotation pass 1 (`ctx+0x570` bit 7); execute `sub_C60340` -> `sub_6FC240` |
| 145 | `MercGenerateOpex` | Operand-exchange annotation (`ctx+0x570` bit 6); execute `sub_C60380` -> `sub_7032A0` |
| 146 | `MercGenerateWARs2` | WAR-hazard annotation pass 2 (catches hazards from Opex); same entry as 144 |
| 147 | `MercGenerateSassUCode` | Final SASS microcode emission (`ctx+0x571` bit 0); execute `sub_C603A0` -> `sub_6EEE90` -> `sub_6E4110` |
| 148 | `ComputeVCallRegUse` | Target vtable+0x2B8 virtual-call register-usage computation; execute `sub_C5E160` |
| 149 | `CalcRegisterMap` | Final physical-to-logical register mapping (`ctx+0x590` bit 1); execute `sub_C603C0` -> `sub_95A350` (6.3 KB) |
| 150 | `UpdateAfterPostRegAlloc` | **`nullsub_630`** — stripped from release, `isNoOp=1` suppresses diagnostics |
| 151 | `ReportFinalMemoryUsage` | **`nullsub_629`** — stripped from release, `isNoOp=1` suppresses diagnostics |
| 152 | `AdvancedPhaseOriPhaseEncoding` | **Hook** — Type-C gate writing `pipeline_progress = 21` (execute `sub_C5E0B0`, 11 bytes) |
| 153 | `FormatCodeList` | Code-list emitter dispatch through `(*ctx+0x648)->vtbl[+0x10]`; execute `sub_C5E080` |
| 154 | `UpdateAfterFormatCodeList` | **`nullsub_628`** — stripped from release, `isNoOp=1` suppresses diagnostics |
| 155 | `DumpNVuCodeText` | Debug SASS-text dumper gate (`ctx+0x598 > 0`); tail-call target is `nullsub_31` in release |
| 156 | `DumpNVuCodeHex` | Debug SASS-hex dumper gate; tail-call target is `nullsub_30` in release |
| 157 | `DebuggerBreak` | **`nullsub_627`** — registered but not in the default schedule; debug-only breakpoint marker (recipe-only) |
| 158 | `NOP` | **`nullsub_626`** — registered but not in the default schedule; the lookup-failure sentinel (`sub_C641D0` returns 158) and recipe-seed value |

All 20 phases in the 139–158 range have names in the static table at `off_22BD0C0` (159 entries total, not 139). Name resolution goes through each phase's `getIndex()` virtual method (vtable+8) returning the phase index as a constant (`mov eax, 0x8b..0x9e; ret`), which the dispatch loop (`sub_C64F70`) uses as the lookup key into the name table. The earlier claim that these phases had names "returned by a `getName()` virtual method" was incorrect.

Of the 20 phases, **five** have `nullsub` execute bodies in release ptxas (150, 151, 154, 157, 158), **two** (155, 156) have non-trivial gate cascades but resolve to nullsub tail-call targets, and **four** set `isNoOp() = 1` to suppress the diagnostic frame around their call (150, 151, 152, 154). `isNoOp = 1` does **not** skip the execute call — it only suppresses the `"Before <phase>"` / `"After <phase>"` diagnostic prints, and `sub_C64F70:86` `goto LABEL_4` still falls through to the execute dispatch. See [Optimization Pipeline Stage 10](index.md#stage-10----late-cleanup--late-pipeline-phases-132--158) for the full per-phase algorithm breakdown with execute addresses, pseudocode, and gate conditions.

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
| 104 | `AdvancedPhasePostExpansion` | Before post-RA expansion worker (dispatches to phase 127 `PostExpansion`) |
| 106 | `AdvancedPhasePostSched` | Before post-scheduling worker (Type-C thunk writes `ctx+1552=14`; phase 110 `PostSchedule` follows) |
| 111 | `AdvancedPhasePostFixUp` | Before post-fixup worker (dispatches to phase 140 `PostFixUp`; Type-C thunk writes `ctx+1552=20`) |
| 115 | `AdvancedScoreboardsAndOpexes` | Before scoreboard/opex generation |
| 127 | `AdvancedPhaseOriPhaseEncoding` | Before final instruction encoding |
| 134 | `AdvancedPhaseAfterMidExpansion` | After mid-level expansion |
| 135 | `AdvancedPhaseLateExpandSyncInstructions` | After late sync instruction expansion |

## Mercury Encoding Sub-Pipeline

Phases 113–122 form a self-contained sub-pipeline that transforms the optimized, register-allocated Ori IR into final SASS machine code via the Mercury encoding format:

```text
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
| `sub_9F4040` | 9,093 | NvOptRecipe string applier (NamedPhases / `pNNN` / shuffle modes) | VERY HIGH |
| `sub_9F63D0` | 51 | Recipe-path trampoline (constructs PM, calls applier, dispatches, destructs) | VERY HIGH |
| `sub_798B60` | 1,776 | Generic comma-delimited key/value tokenizer (shared with knob env parser) | VERY HIGH |
| `sub_7FB6C0` | — | Compilation driver: option-298 fork to recipe path or default path | HIGH |

## Cross-References

- [Pass Inventory & Ordering](./index.md) — full phase sequence and stage grouping
- [GeneralOptimize Bundles](./general-optimize.md) — phases 13, 29, 37, 46, 58, 65
- [Synchronization & Barriers](./sync-barriers.md) — phases 26, 71, 72, 99, 100
- [Liveness Analysis](./liveness.md) — phases 10, 16, 33, 61, 84
- [Mercury Encoder](../codegen/mercury.md) — phases 113–122
- [Memory Pool Allocator](../infra/memory-pools.md) — pool allocation infrastructure used by PhaseManager
- [Optimization Levels](../config/opt-levels.md) — how opt level controls phase behavior
- [DUMPIR & NamedPhases](../config/dumpir.md) — phase name resolution for debug output
