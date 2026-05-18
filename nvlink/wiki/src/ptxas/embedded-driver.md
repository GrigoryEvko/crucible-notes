# Embedded ptxas: Compilation Driver (`sub_1112F30`)

> **Note**: This page is the algorithm reference for the per-module compilation driver inside nvlink's embedded ptxas. It is a binary-recovered companion to the standalone wiki -- for the equivalent algorithm in the open-coded standalone `ptxas` (without nvlink's split-compile harness, EWP fallback rules, or 168-byte input-container plumbing) see [ptxas: Pipeline Overview](../../ptxas/pipeline/overview.html) and [ptxas: Entry Point](../../ptxas/pipeline/entry.html). The 26-phase ordering, callback identities, and stack-snapshot widths documented below are unique to the nvlink-embedded copy in v13.0.88; their cross-version stability is unverified.

The function at `0x1112F30` is the per-module compilation orchestrator in nvlink's embedded ptxas. Hex-Rays recovers 2,164 lines (12,219 instruction bytes, 65,018 source bytes) from a single function with no helper extraction. It is reached from two entry points -- `sub_4BD760` (PTX JIT path) and `sub_4BC6F0` (LTO finalization after libnvvm produces PTX) -- with a module-context pointer in `rdi` and a PTX module descriptor in `rsi`. The body partitions cleanly into 26 sequential phases: option capture, timing, callback registration, mode dispatch, header emission, flag negotiation, table allocation, per-function configuration, the inner compile loop (sequential or thread-pooled), and teardown. The function returns 0 on success; failures longjmp through `sub_45CAC0` to the linker's top-level error handler.

This page documents each phase, the per-function inner pipeline at Phase 23, the mode-flag matrix at Phase 6, the C pseudocode of the driver loop, and three quirks that distinguish this driver from the standalone `ptxas` entry path.

## Identity and Provenance

| Address | Symbol | Size | Decompilation depth | Confidence |
|---|---|---|---|---|
| `0x1112F30` | `sub_1112F30` (= `ptxas_compile_module`) | 12,219 B (65,018 src B) | 2,164 lines (`decompiled/sub_1112F30_0x1112f30.c`) | **HIGH** |
| `0x4BD760` | `ptxas_jit_compile` | -- | calling site #1 (PTX from disk) | HIGH |
| `0x4BC6F0` | `compile_linked_lto_ir` | -- | calling site #2 (LTO post-libnvvm) | HIGH |

The arguments at entry are:

- `a1` (rdi) -- the module compilation context: a >5,000-byte structure holding option booleans (offsets ~104..~890), timing buffers (`a1+128`, `a1+144`, `a1+160`), the cancellation callback (`a1+288..a1+304`), the IR walk roots (`a1+408`, `a1+416`), the per-function descriptor array head (`a1+256`), the codegen-config slot (`a1+1192`), and the SM dispatch vtable pointer.
- `a2` (rsi) -- the PTX module descriptor: contains `.target` string (`a2+184`), `.version` integer (`a2+196`), file path (`a2+144`), entry-point list head (`a2+88`), and ~30 mode-affecting flag bytes between offsets 218 and 1065.

Both pointers are owned by the caller; the driver never frees them.

## Phase Catalog

The 26 phases below are listed in execution order. Phase numbers correspond to the trim-preserved table in [Architecture Overview](overview.md#phase-table); this page expands each row with the actual callees and the structural role.

### Phase 1 -- Option Query and Cache Configuration

```c
bool def_load   = option_get_bool("def-load-cache");      // sub_42E... family
bool force_load = option_get_bool("force-load-cache");
bool def_store  = option_get_bool("def-store-cache");
bool force_store = option_get_bool("force-store-cache");
// Booleans captured into stack locals; consumed later at Phase 16.
```

Four queries against the option store populated by `sub_1103030` (option definition table) and `sub_1104950` (option processing). Stored on the driver's stack frame; not written to `a1`.

### Phase 2 -- Cancellation Check

```c
if (a1->cancel_token /* a1+288 */) {
    __int128 cb = a1->cancel_callback;       // a1+296
    if (((int(*)(void*, __int64))(a1+296))(cb.lo, cb.hi) == 1)
        goto LABEL_114;  /* longjmp to error handler */
}
```

The host (nvlink front-end, or a CUDA-driver caller for JIT) can register a non-null callback at `a1+296`. Returning 1 from the callback aborts the compile immediately, before any IR is touched.

### Phase 3 -- Timing Gate

If `a1[104]` (profiling enabled) is set, `sub_45CCD0((struct timeval*)(a1+128))` is called to record wall-clock start; if `a1[105]` is set, `sub_44EF30()` writes a high-resolution start at `a1+160`. The dual-flag arrangement lets the host capture both timeval-grade and ns-grade timing without paying for the high-res clock when unused. Phase 25 emits the matching stops.

### Phase 4 -- Callback Registration

```c
sub_464700(a1[408], (cb_t)sub_1108860, a1);  // per-PTX-file walker
sub_464700(a1[416], (cb_t)sub_1101EB0, a1);  // per-PTX-string walker
sub_12B30E0(a2);                              // install version compat table
sub_12B31D0(a2);                              // install version exceptions
```

Two universal callbacks are installed. `sub_1108860` is the file walker: for each PTX file in `a1[408]` it copies eight pre-flight bytes (`a2[214,176,245,249,266]`) into the PTX descriptor and calls `sub_12AF200` to parse. `sub_1101EB0` is the string walker: it does the same byte copy and calls `sub_12AF550` against the `application ptx input` literal. `sub_12B30E0` initializes the version compatibility tables and runs `sub_44E4F0(target_string)` to compute the SM ordinal; `sub_12B31D0` installs the version exception table.

### Phase 5 -- SM Version Validation

```c
int target_sm; sscanf(a2->target /* a2+184 */, "sm_%d", &target_sm);
if (!sub_12A8360(a2->version, target_sm))
    fatal_error("PTX/SM incompatibility");
```

`.target sm_XX` is parsed from the PTX header and matched against the `.version` field. `sub_12A8360` consults the version table installed in Phase 4 and aborts via `dword_2A5DCA0` if the module's SM ordinal exceeds the maximum compiled-in support.

### Phase 6 -- Mode Flag Dispatch

Picks one of four `(init_fn, begin_fn)` pairs from `--compile-only`, `--compile-as-tools-patch`, `--assyscall`, `--extensible-whole-program`, and `--device-debug`. See the [Compilation Mode Matrix](overview.md#compilation-mode-matrix). The selected pair is stashed at `a1+1184` / `a1+1188` for Phase 15 and the per-function loop.

### Phase 7 -- PTX Header Emission

If `a2[178]` is set and `a2[236]` is clear (in-memory mode), `sub_12AF550` emits a synthetic header into a temporary buffer:

```c
sprintf(buf, ".version 8.6\n.target sm_%d\n.entry __cuda_dummy_entry__ { ret; }\n", sm);
```

Otherwise the driver opens the file at `a2+184` via `fopen`, writes the same triple, and re-parses via `sub_12AF200`. The dummy entry exists because the PTX validator (`sub_147EF50`, 288 KB) refuses to operate on a header-only module; the entry is later pruned during dead-code elimination if no real entries reference it.

### Phase 8 -- Tools-Patch Warnings

When `--compile-as-tools-patch` is active, the driver checks `a2[860..864]` for cross-references to additional shared memory, textures, surfaces, samplers, and constants. Each set bit emits a warning through `sub_467460(dword_2A5D940, …)` naming the resource class. The same five bits are re-checked under `--assyscall` against `dword_2A5D940` but with a different warning string. This is the only phase that has no functional side effect -- pure diagnostics.

### Phase 9 -- Compilation Flag Setup

```c
if (a2[218]) {                              // calls-without-ABI module
    a1[861] = 0;                            // disable --fast-compile
    a1[889] = 0;                            // disable --extensible-whole-program
    if (a2->pic_string /* a2+224 */)
        warn("PIC conflict with calls-without-ABI");
}
if (sm < 70 && a1->legacy_bar_warp_wide /* a1+672 */) warn();
if (sm < 100 && a1->g_tensor_memory_check)            warn();
```

Resolves three classes of flag conflict: ABI-less modules disable `--fast-compile` and `--extensible-whole-program`; `--legacy-bar-warp-wide-behavior` is rejected outside SM70; `--g-tensor-memory-access-check` is rejected outside SM100+. The `--position-independent-code` flag at `a2+248` is silently dropped for ABI-less modules.

### Phase 10 -- Hash Maps and Codegen Context

```c
for (int i = 0; i < 8; ++i)
    a1->maps[i] = sub_4489C0(/*cap*/ caps[i] /* 0x100, 0x400, 0x40, 0x20, ... */);
a1->func_resource_array = sub_465020(/*entry_size=*/48, sub_12AE300(a2));
a1->result_array        = sub_465020(/*entry_size=*/112, …);
```

Eight `LinkerHash` maps are constructed for symbol -> codegen-record lookup, callee usage tracking, alias resolution, and per-function diagnostic queues. The capacities (0x100, 0x400, 0x40, 0x20) are fixed and never resized; the per-function resource array at `a1+336` is sized to the function count returned by `sub_12AE300`.

### Phase 11 -- Register Callbacks on Module IR

```c
walk(a1->func_list,    sub_1102AC0);  // per-function entry
walk(a1->symbol_list,  sub_1101E90);  // per-PTX-symbol
walk(a1->func_ir_list, sub_1111DB0);  // per-function-IR
if (!compile_only)
    walk(a1->global_list, sub_1101DE0);  // per-global object
walk(a1->section_list, sub_110F5E0);  // per-section
walk(a1->symbol_list,  sub_1101F60);  // post-process pass
```

Six IR walker callbacks are installed; the fourth (per-global) is suppressed under `--compile-only` to avoid touching state that the tools-patch path leaves uninitialized.

### Phase 12 -- Address Width and Register Budget

```c
if (sm <= 13) {
    address_width = 32;
    maxnreg = 32;
} else {
    address_width = (a2->meta & ADDR_WIDTH_MASK) ? 64 : 32;
    if (sm > 90 && address_width == 32)
        fatal("32-bit address mode unsupported on SM90+");
}
a1->address_width = address_width;
```

SM13 and earlier are hard-coded to 32-bit with a 32-register budget (Tesla generation). Modern SMs read the width from a metadata byte; 32-bit mode is fatal on SM90 and above because Hopper's MMA instructions assume 64-bit pointers in their operand encoding.

### Phase 13 -- Entry Point Collection

When `-e <name>` or `-E <regex>` is passed, the driver iterates the module symbol table to find matches and builds an ordered entry list. Otherwise it takes the head pointer at `a2+88` (the module's default entry list). The ordered list is stored at `a1+424`.

### Phase 14 -- Transfer State into Codegen Context

```c
memcpy(a1 + 1072, &a1->raw_flags, 224);      // copy 224 B of flag state
a1->alias_map      = sub_4489C0(0x100);      // alias resolution map
a1->callee_use_map = sub_4489C0(0x418);      // per-function callee usage
```

The driver snapshots ~224 bytes of flag state at offset 1072 of `a1`. This snapshot is what each per-function compile reads from -- subsequent flag mutations in Phase 23 worker threads operate on per-thread copies and never alias the snapshot.

### Phase 15 -- `init_callback(ctx, entries)`

Calls the `init_fn` selected at Phase 6 (`sub_110CD20`, `sub_110CBA0`, `sub_110D0B0`, or `sub_110D110`). All four iterate the entry list and call `sub_110BC90` once per entry to allocate a codegen descriptor (`sub_110BC90` returns a `pthread_mutexattr_t *` pointing at a fresh descriptor; this is a Hex-Rays artifact -- the actual type is `CodegenRecord`). The descriptor is inserted into the map at `a1+1192` keyed by the entry's symbol name.

```c
for (entry = entries; entry; entry = entry->next) {
    record = alloc_codegen_record(a1, entry->name, profiling, entry->kind);
    hash_insert(a1->codegen_map /* a1+1192 */, entry->name, record);
}
```

### Phase 16 -- Load/Store Cache Mode

Per function in the codegen map: `force-load-cache` overrides everything (mode 2); else `def-load-cache` (mode 1); else the callee analysis chooses mode 0. Same scheme for stores. The result is written into `record->load_cache_mode` and `record->store_cache_mode`.

### Phase 17 -- Indirect Call and MMA Validation

For each function in the codegen map, if the function has indirect calls and references `mma.f64`, emit a warning (indirect dispatch defeats the hardware tensor-core scheduler). If the function carries a mutual-recursion marker (set by `sub_1101F60` at Phase 11), abort -- mutual recursion is unsupported by the PTX call ABI.

### Phase 18 -- Scheduling Class Assignment

Each function gets a scheduling class in `{0, 1, 2}` propagated through the call graph. Class 0 is the default; class 1 enables the standard scheduler; class 2 enables the aggressive scheduler that walks callee bodies during latency estimation. Class 2 requires `--fast-compile` to be off.

### Phase 19 -- Debug Info Setup

```c
if (a1->device_debug /* a1+105 */) {
    a1->dwarf_ctx = sub_1672520(a1);   // dwarf_init
}
```

`sub_1672520` allocates a 216-byte DWARF state object; subsequent passes record `.loc` directives into it.

### Phase 20 -- Reserved Register Configuration

```c
int first = max(4, a1->first_reserved_rreg);   // min 4 (R0-R3 are caller-saved)
int count = a1->reserved_rreg_count;
a1->reserved_rreg_range = (count << 16) | first;
```

`R0..R3` are always reserved (caller-saved); user options may reserve additional registers from the top. The encoded range is read by the register allocator at Phase 23's pass 22.

### Phase 21 -- Build Per-Function Codegen Config and `CodegenPipeline`

The driver constructs a config struct packing ~50 flags: `device_debug`, `lineinfo`, `fast_compile`, `maxrregcount`, `opt_level`, `compile_only`, `tools_patch`, `ewp`, `preserve_relocs`, `sm_version`, `address_width`, `default_load_cache`, `default_store_cache`, `pic`, `legacy_bar`, `g_tensor_check`, plus 30 minor toggles. The config is handed to `sub_16257C0` which constructs a `CodegenPipeline` object (vtable + state). Stored at `a1+1296`.

### Phase 22 -- Output File Setup

If `--output` is set, the file is opened with `O_TRUNC` so the SASS dump appended in Phase 23-finalize starts clean.

### Phase 23 -- Per-Function Compile Loop

The largest phase. Selects between sequential and parallel based on `a1->thread_count /* a1+668 */`:

```c
if (a1->thread_count == 0) {
    /* Phase 23a: sequential */
    for (rec = codegen_records; rec; rec = rec->next) {
        sub_110AA30(rec, a1);            // codegen_init: alloc 360-B state
        sub_1655A60(rec, a1);            // codegen_per_func: 48-pass pipeline
        sub_1102B30(rec, ..., a1, ...);  // codegen_compile: setjmp + vtable->compile
        timing_record(rec);
        sub_110D2A0(rec, a1);            // codegen_finalize: ELF emit
    }
} else {
    /* Phase 23b: parallel */
    pool = sub_43FDB0(a1->thread_count);
    for (rec = codegen_records; rec; rec = rec->next) {
        WorkItem *item = alloc_48B_workitem(rec, a1);
        snapshot_15x16(item, a1);        // copy 240 B of driver state
        item->dwarf = dwarf_register(a1->dwarf_ctx);  // 216-B local
        sub_43FF50(pool, sub_1107420, item);  // enqueue
    }
    sub_43FFE0(pool);                    // barrier
    sub_43FE70(pool);                    // destroy
    /* merge per-thread maps */
    for (rec = codegen_records; rec; rec = rec->next)
        sub_110D2A0(rec, a1);            // finalize in main thread
}
```

Worker function `sub_1107420` runs `sub_1102B30` (setjmp-wrapped `vtable->compile`) and records timing + peak memory. The barrier is mandatory: register-budget propagation in Phase 24 relies on every function having a finalized resource record.

#### Per-Function Inner Pipeline

| Sub-stage | Address | Role |
|---|---|---|
| `codegen_init` | `sub_110AA30` | Allocate 360-B per-function state; create OCG context; set `producer="NVIDIA"`, `tool="ptxocg.0.0"`; configure ~30 SM-specific fields via the dispatch vtable |
| `codegen_per_func` | `sub_1655A60` | Drive the 48-pass codegen pipeline (passes 0..47; see [Architecture Overview](overview.md#the-48-pass-codegen-pipeline-sub_1655a60)) |
| `codegen_compile` | `sub_1102B30` | `setjmp(env)`; on longjmp, set retry/fail flags and report through `dword_2A5DCA0`; else call `vtable->compile(ctx, func, &record)` |
| timing | -- | record start/end via `sub_45CCD0` + `sub_44EF30` |
| `codegen_finalize` | `sub_110D2A0` | Emit `.text`, `.nv.info`, `.nv.constant` sections; write EIATTR register-usage records; emit SASS binary; release per-function OCG state |

### Phase 24 -- Post-Compilation Cleanup

```c
if (a1->compile_only /* a1+726 */) {
    /* Cross-check caller/callee register budgets through the call graph. */
    walk_callgraph(a1->register_budget_map, validate_budget);
}
```

Under `--compile-only` (the tools-patch path), the driver re-walks the call graph to verify that each caller's register budget can host its callees' usage. Fatal if any pair exceeds the per-function `maxrregcount` set in Phase 21.

### Phase 25 -- Pipeline Config Teardown

`sub_1626480` destroys the `CodegenPipeline` object built in Phase 21. Timing snapshots from Phase 3 are captured into the host's profiling buffer if `a1[104] || a1[105]`.

### Phase 26 -- Final Cleanup

```c
for (int i = 0; i < 8; ++i) sub_4650A0(a1->maps[i]);
free(a1->func_resource_array);
free(a1->result_array);
return 0;
```

Eight maps from Phase 10 are destroyed via `sub_4650A0`, the two arrays are freed, and the driver returns 0. The caller (`sub_4BD760` or `sub_4BC6F0`) is responsible for tearing down `a1` itself.

## Top-Level Pseudocode

```c
int ptxas_compile_module(ModuleCtx *a1, PtxModule *a2) {
    /* Phase 1: capture cache booleans */
    bool def_l = opt_b("def-load-cache"),  fc_l = opt_b("force-load-cache");
    bool def_s = opt_b("def-store-cache"), fc_s = opt_b("force-store-cache");

    /* Phase 2: cancellation */
    if (a1->cancel && a1->cancel_cb(a1->cancel_ctx) == 1) goto err;

    /* Phase 3: timing */
    if (a1->prof_wall) gettimeofday(&a1->t_wall);
    if (a1->prof_hi)   a1->t_hi = hi_res_clock();

    /* Phase 4: callbacks */
    walk(a1->file_list,   sub_1108860, a1);
    walk(a1->string_list, sub_1101EB0, a1);
    sub_12B30E0(a2); sub_12B31D0(a2);

    /* Phase 5: SM check */
    int sm; sscanf(a2->target, "sm_%d", &sm);
    if (!sub_12A8360(a2->version, sm)) fatal();

    /* Phase 6: mode dispatch */
    auto [init_fn, begin_fn] = pick_mode(a1, a2);

    /* Phase 7: header emit */
    emit_ptx_header(a2, sm);

    /* Phases 8-12: warnings, flag setup, table alloc, callbacks, addr width */
    emit_tools_patch_warnings(a1, a2);
    resolve_flag_conflicts(a1, a2);
    for (int i = 0; i < 8; ++i) a1->maps[i] = sub_4489C0(caps[i]);
    install_ir_walkers(a1);
    a1->address_width = pick_address_width(sm, a2);

    /* Phase 13: entry points */
    a1->entries = resolve_entries(a1, a2);

    /* Phase 14: state snapshot */
    memcpy(a1 + 1072, &a1->flags, 224);

    /* Phase 15: init callback */
    init_fn(a1, a1->entries);

    /* Phases 16-22: per-function config */
    for (auto *r : a1->records) {
        r->load_cache  = pick_load_mode(r, fc_l, def_l);
        r->store_cache = pick_store_mode(r, fc_s, def_s);
        validate_indirect_mma(r);
        r->sched_class = propagate_class(r);
    }
    if (a1->device_debug) a1->dwarf = sub_1672520(a1);
    a1->reserved_rregs = pack_reserved();
    a1->pipeline = sub_16257C0(build_config(a1));
    if (a1->output) freopen(a1->output, "w", stdout);

    /* Phase 23: compile loop */
    if (a1->thread_count == 0) {
        for (auto *r : a1->records)
            sub_110D2A0(r, a1, sub_1102B30(r, ..., sub_1655A60(r, sub_110AA30(r, a1))));
    } else {
        auto *pool = sub_43FDB0(a1->thread_count);
        for (auto *r : a1->records) sub_43FF50(pool, sub_1107420, mk_workitem(r, a1));
        sub_43FFE0(pool); sub_43FE70(pool);
        for (auto *r : a1->records) sub_110D2A0(r, a1);
    }

    /* Phases 24-26: teardown */
    if (a1->compile_only) validate_callgraph_budgets(a1);
    sub_1626480(a1->pipeline);
    for (int i = 0; i < 8; ++i) sub_4650A0(a1->maps[i]);
    return 0;
}
```

## Quirks

**QUIRK 1: EWP+debug silently degrades to compile-only.** When both `--extensible-whole-program` and `--device-debug` are set, the Phase 6 dispatch matrix selects the *compile-only* `(init_fn, begin_fn)` pair (`sub_110CD20`, `sub_11089E0`), not the EWP pair. There is no warning. The user-facing flag combination is technically accepted but the whole-program optimization is silently disabled because the EWP code path mutates symbol visibility in ways that break DWARF location lists. This is unique to the nvlink-embedded driver -- standalone ptxas's entry path rejects the combination outright; see [ptxas: Entry Point](../../ptxas/pipeline/entry.html).

**QUIRK 2: Phase 7 always emits a dummy entry.** Even when the module has real `.entry` definitions, the header-emission phase writes `.entry __cuda_dummy_entry__ { ret; }` into the synthetic header. The dummy entry is required because the PTX semantic analyzer (`sub_147EF50`) refuses to validate header-only or entry-less modules; after Phase 13 collects the real entries, dead-code elimination removes the dummy. Standalone ptxas does not need this trick because its frontend operates on a file directly. The dummy entry sometimes surfaces in DWARF debug output for empty modules -- this is the cause.

**QUIRK 3: Parallel mode runs `codegen_finalize` sequentially.** Phase 23b's barrier (`sub_43FFE0`) is followed by a *sequential* finalize loop in the main thread. Each `sub_110D2A0` call mutates module-wide register-budget state (the cross-function constraint propagation at Phase 24), so finalize must observe a deterministic order. The performance cost is a function of the slowest-to-finalize record, but for typical CUDA modules (5--50 functions) this overhead is dominated by parse and ISel. Standalone ptxas under `--split-compile-extended` has the same constraint -- see [ptxas: Pipeline Overview](../../ptxas/pipeline/overview.html) for the equivalent.

## Cross-References

### nvlink Internal
- [Architecture Overview](overview.md) -- the 26-phase table this page expands, plus the embedded-ptxas address map
- [Architecture Dispatch (vtables)](arch-dispatch.md) -- the 7 SM dispatch maps consulted from Phase 21 onward
- [Instruction Selection Hubs](isel-hubs.md) -- the five mega-hubs invoked by Phase 23's pass 23-38
- [Register Allocation](register-allocation.md) -- the regalloc consumed by Phase 23 passes 22 and 23-38
- [Instruction Scheduling](scheduling.md) -- `ScheduleInstructions` invoked by Phase 23
- [PTX Parsing](ptx-parsing.md) -- `sub_12AF200` / `sub_12AF550` called from Phases 4 and 7
- [Function Map](../function-map.md#sub_1112F30) -- one-line role and entry in the global function index
- [Split Compilation](../lto/split-compilation.md) -- how the thread-pool mode at Phase 23b is configured from the linker driver
- [CLI Option Parsing](../pipeline/cli-options.md) -- where the Phase 1 booleans originate

### Sibling Wikis
- [ptxas: Pipeline Overview](../../ptxas/pipeline/overview.html) -- standalone ptxas 159-phase pipeline (this driver corresponds to its entry/dispatch path)
- [ptxas: Entry Point](../../ptxas/pipeline/entry.html) -- standalone ptxas `main()` and option processing
- [ptxas: Codegen Overview](../../ptxas/codegen/overview.html) -- the 48-pass per-function pipeline run by Phase 23's `sub_1655A60`
- [ptxas: Instruction Selection](../../ptxas/codegen/isel.html) -- the ISel algorithm dispatched through Phase 23
- [ptxas: Scheduling Algorithm](../../ptxas/scheduling/algorithm.html) -- the scheduling algorithm invoked from Phase 18 and Phase 23
- [ptxas: Passes Index](../../ptxas/passes/index.html) -- the standalone pass numbering for reference
