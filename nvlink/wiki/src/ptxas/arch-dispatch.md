# Architecture Dispatch (vtables)

The embedded ptxas compiler in nvlink supports 22 architecture strings across 12 distinct silicon targets. Rather than scattering per-SM `if/else` chains throughout 24 MB of code, the compiler concentrates all architecture-dependent selection into a single initialization function (`sub_15C0CE0`) that populates 7 hash-map vtables. Each vtable maps an architecture string (e.g. `"sm_90"`) to a function pointer or data pointer implementing that SM's behavior for one compilation aspect. Callers never check the SM version directly -- they look up the appropriate callback from the correct vtable and call through the function pointer. This page documents the singleton initializer, the 7 vtable maps, the 22 registered architecture strings, pointer sharing between SM variants, the accessor/dispatcher functions, and the related 11-byte ISel mega-hub wrappers at `0x5272D0`--`0x527310`.

## Key Functions

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_15C0CE0` | `init_sm_dispatch_tables` | 14,517 B | Singleton init: creates 7 hash maps, registers all 22 arch strings |
| `sub_15C1CA0` | `destroy_sm_dispatch_tables` | 288 B | Teardown: clears all 7 maps, resets guard flag |
| `sub_15C3D60` | `lookup_cpf_optx_callback` | 80 B | Accessor: returns cpf_optx (B8) or cpf_optx_alt (B0) callback by SM + mode |
| `sub_15C3DB0` | `lookup_nv_info_emitter` | 48 B | Accessor: returns nv.info emitter callback from map A8 |
| `sub_15C3DD0` | `lookup_compute_capability` | 80 B | Accessor: returns compute_XX version byte from map A0 |
| `sub_15C3E00` | `lookup_perf_stats_callback` | 80 B | Accessor: retrieves + invokes perf-stats callback from map 90 |
| `sub_15C3E50` | `lookup_codegen_options_callback` | 96 B | Accessor: retrieves + invokes codegen-options callback from map 88 |
| `sub_4489C0` | `LinkerHash::create` | 112 B | Hash map constructor (mode 0: MurmurHash3 string keys) |
| `sub_448E70` | `LinkerHash::insertOrUpdate` | 3,728 B | Hash map insert used during registration |
| `sub_449A80` | `LinkerHash::lookup` | 592 B | Hash map lookup used by all accessors |
| `sub_5272C0` | `instruction_opcode_dispatch_table` | 79,511 B | 4,115-line switch on IR opcode class -> SASS encoding ID |
| `sub_5272D0` | `isel_mega_hub_sm75_wrapper` | 11 B | Wrapper: `(a1,a2,a3) -> sub_FBB810(a2,a3)` |
| `sub_5272E0` | `isel_mega_hub_sm80_wrapper` | 11 B | Wrapper: `(a1,a2,a3) -> sub_D5FD70(a2,a3)` |
| `sub_5272F0` | `isel_mega_hub_sm89_90_wrapper` | 11 B | Wrapper: `(a1,a2,a3) -> sub_119BF40(a2,a3)` |
| `sub_527300` | `isel_mega_hub_ptx_wrapper` | 11 B | Wrapper: `(a1,a2,a3) -> sub_126CA30(a2,a3)` |
| `sub_527310` | `isel_mega_hub_mercexpand_wrapper` | 11 B | Wrapper: `(a1,a2,a3) -> sub_5B1D80(a2,a3)` |

## Global Data

Seven global `qword` pointers store the hash map objects. A `byte` flag guards singleton initialization.

```
Address          Name              Content
---------------------------------------------------------
qword_2A644B8    map_cpf_optx      Hash map: arch -> cpf_optx callback
qword_2A644B0    map_cpf_optx_alt  Hash map: arch -> cpf_optx_alt callback
qword_2A644A8    map_nv_info       Hash map: arch -> nv.info emitter callback
qword_2A644A0    map_compute_cap   Hash map: arch -> compute_XX byte array pointer
qword_2A64498    map_reserved      Hash map: arch -> (unused/reserved; created but no inserts observed)
qword_2A64490    map_perf_stats    Hash map: arch -> perf-stats callback
qword_2A64488    map_codegen_opts  Hash map: arch -> codegen-options callback
byte_2A644C0     init_guard        Singleton flag: 0 = not initialized, 1 = initialized
```

All seven maps use mode-0 (string keys) with `sub_44E000` (MurmurHash3) and `sub_44E180` (strcmp) as the hash and compare functions. Each map entry stores an 8-byte value: either a function pointer or a pointer to a static byte array.

## Singleton Initialization

`sub_15C0CE0` is called lazily on every vtable access. It checks `byte_2A644C0` and returns immediately if already initialized. The initialization is wrapped in `setjmp`/`longjmp` for exception safety -- if any allocation or registration fails, the longjmp path restores the error context without leaving the maps in a half-initialized state.

The init sequence:

1. Acquire lock via `sub_4FFBF0(5)` (mutex index 5).
2. Save the current error handler context via `sub_44F410(5, ...)`.
3. Install a `setjmp` landing pad. On longjmp, restore the previous error handler and set error flags.
4. Double-check `byte_2A644C0` (DCL pattern -- another thread may have initialized while we waited on the lock).
5. Call `sub_45CAE0(0, 0)` to save the current arena state (for rollback on failure).
6. Create 7 hash maps via `sub_4489C0(sub_44E000, sub_44E180, 8)` -- initial capacity hint of 8 slots each.
7. Register all architecture entries (154 insert calls total).
8. Register `sub_15C1CA0` as the cleanup callback via `sub_45CC80`.
9. Restore the arena state via `sub_45CAE0(saved, 0)`.
10. Set `byte_2A644C0 = 1`.
11. Restore previous error handler.
12. Release lock via `sub_4FFC10(5)`.

## The 7 Vtable Maps

### Map B8: cpf_optx callback (`qword_2A644B8`)

Each entry is a function pointer with signature `bool (*)(int64_t context, int64_t compilation_state)`. The callback resolves the `"cpf_optx"` option through the compilation state's vtable at offset +40, stores the resolved option ID at compilation_state+100, then calls `sub_166DA30(state, 0)` to apply the option. The `0` argument distinguishes this from map B0. All 12 silicon targets register distinct function pointers, though the bodies are structurally identical -- they differ only in address, allowing per-SM specialization through different `sub_166DA30` dispatch paths within the same codegen driver.

Representative decompilation (sm_75 entry, `sub_15C2AA0`):
```c
bool cpf_optx_sm75(int64_t ctx, int64_t state) {
    *(int32_t*)(state + 100) = (***vtable(state + 40))(*(state + 40), "cpf_optx");
    return sub_166DA30(state, 0) != 0;
}
```

### Map B0: cpf_optx_alt callback (`qword_2A644B0`)

Same signature and structure as map B8, but calls `sub_166DA30(state, 1)` instead of `sub_166DA30(state, 0)`. The second argument selects an alternate codegen path. This pair of maps (B8 and B0) implements a two-mode cpf_optx dispatch: the accessor `sub_15C3D60` selects B8 or B0 based on a boolean mode flag in argument `a2`.

Representative decompilation (sm_75 entry, `sub_15C2A70`):
```c
bool cpf_optx_alt_sm75(int64_t ctx, int64_t state) {
    *(int32_t*)(state + 100) = (***vtable(state + 40))(*(state + 40), "cpf_optx");
    return sub_166DA30(state, 1) != 0;  // mode=1 selects alternate path
}
```

### Map A8: nv.info emitter callback (`qword_2A644A8`)

Each entry is a function pointer with signature `DWORD* (*)(int64_t ctx, int64_t options, int64_t arena, int64_t arch_profile)`. The callback allocates and initializes a per-SM compilation state structure (approximately 1,936 bytes) by calling `sub_189F230(arena)`, then sets architecture-specific constants:

- Offset 344: constant value (e.g. `0x100000` for all observed SMs)
- Offset 348: compute capability encoding (e.g. `24577` for sm_75, `0x8000` for sm_90)
- Offset 108: function table size computed from arch_profile
- Offsets 460, 470, 477, 478, 482: feature flags for the target SM

The structure returned by these callbacks becomes the per-function codegen context used throughout the compilation pipeline.

### Map A0: compute capability byte array (`qword_2A644A0`)

Each entry is a pointer to a static 4-byte array in `.data` containing the compute capability version bytes. The accessor `sub_15C3DD0` dereferences this pointer and returns a 32-bit unsigned int. A return value of `0xFFFFFFFF` (`-1`) indicates the architecture string was not found in the map.

This map uniquely registers both `sm_XX` and `compute_XX` keys pointing to the same data, so lookups by either naming convention succeed:

```
byte_2A5EE40 -> compute_75 byte array
byte_2A5EE3C -> compute_80 byte array
byte_2A5EE38 -> compute_86 byte array
byte_2A5EE34 -> compute_87 byte array
byte_2A5EE30 -> compute_88 byte array
byte_2A5EE2C -> compute_89 byte array
asc_2A5EE28  -> compute_90 byte array (shared with sm_90a)
byte_2A5EE24 -> compute_100 byte array (shared with sm_100a, sm_100f)
byte_2A5EE20 -> compute_110 byte array (shared with sm_110a, sm_110f)
asc_2A5EE1C  -> compute_103 byte array (shared with sm_103a, sm_103f)
asc_2A5EE18  -> compute_120 byte array (shared with sm_120a, sm_120f)
asc_2A5EE14  -> compute_121 byte array (shared with sm_121a, sm_121f)
```

### Map 98: reserved (`qword_2A64498`)

Created during initialization but no `sub_448E70` insert calls are observed in the decompiled code. This map is allocated and remains empty. It may be reserved for a future vtable category or populated by a code path not captured in the current decompilation.

### Map 90: perf-stats callback (`qword_2A64490`)

Each entry is a function pointer with signature `int (*)(void)`. All observed callbacks call `sub_467460(dword_2A5EEF0, "sm_20", "--perf-stats")`, suggesting they query the option database for the `--perf-stats` flag relative to a baseline `sm_20` profile. Despite using the same pattern, each SM registers a distinct function address -- likely for future per-SM perf-stats customization or to allow hot-patching.

Representative decompilation (sm_75 entry, `sub_15C1C80`):
```c
int perf_stats_sm75(void) {
    return sub_467460(dword_2A5EEF0, "sm_20", "--perf-stats");
}
```

### Map 88: codegen options callback (`qword_2A64488`)

Each entry is a function pointer with signature `int64_t (*)(DWORD* arch_params, int reg_count, int smem_size, bool flag_a, bool flag_b, uint32_t* result)`. These callbacks compute architecture-specific codegen parameters -- primarily occupancy calculations. They read hardware constants from the `arch_params` structure at offsets 20 (total regs), 21 (total smem), 23 (warp size), 27 (max blocks), and 28 (reg granularity), then compute the maximum number of concurrent blocks given the resource requirements. The result is written through the `result` pointer, and the function returns 0 on success or a nonzero error code (1 or 2) on constraint violation.

Representative decompilation (sm_75 entry, `sub_15C2610`):
```c
int64_t codegen_opts_sm75(DWORD *arch, int regs, int smem,
                          bool flag_a, bool flag_b, uint32_t *out) {
    uint32_t granularity = arch[28];
    uint32_t total_regs  = arch[20];
    uint32_t blocks_by_reg = (total_regs >> 2) / (4 * granularity);
    // ... occupancy calculation ...
    *out = max_blocks;
    return 0;
}
```

## Architecture Registration Table

The following table lists every architecture string registered in `sub_15C0CE0`, grouped by silicon target. Within each group, all variants share identical function pointers for all 7 maps.

| Group | Arch Strings | Silicon | Map B8 (cpf_optx) | Map B0 (cpf_optx_alt) | Map A8 (nv.info) | Map 90 (perf) | Map 88 (codegen) |
|---|---|---|---|---|---|---|---|
| 1 | `sm_75` | Turing | `sub_15C2AA0` | `sub_15C2A70` | `sub_15C3210` | `sub_15C1C80` | `sub_15C2610` |
| 2 | `sm_80` | Ampere GA100 | `sub_15C2BF0` | `sub_15C2BC0` | `sub_15C3310` | `sub_15C1EB0` | `sub_15C28B0` |
| 3 | `sm_86` | Ampere GA10x | `sub_15C2C80` | `sub_15C2CB0` | `sub_15C3B60` | `sub_15C1EF0` | `sub_15C1FF0` |
| 4 | `sm_87` | Jetson Orin | `sub_15C2E30` | `sub_15C2D10` | `sub_15C3C60` | `sub_15C1FD0` | `sub_15C2990` |
| 5 | `sm_88` | Ampere ext. | `sub_15C2DA0` | `sub_15C2DD0` | `sub_15C3A60` | `sub_15C1E30` | `sub_15C2530` |
| 6 | `sm_89` | Ada Lovelace | `sub_15C2D40` | `sub_15C2C20` | `sub_15C3740` | `sub_15C1F90` | `sub_15C2370` |
| 7 | `sm_90`, `sm_90a` | Hopper | `sub_15C2CE0` | `sub_15C2B30` | `sub_15C3520` | `sub_15C1ED0` | `sub_15C2290` |
| 8 | `sm_100`, `sm_100a`, `sm_100f` | Blackwell | `sub_15C2B60` | `sub_15C2B00` | `sub_15C3840` | `sub_15C1FB0` | `sub_15C27D0` |
| 9 | `sm_110`, `sm_110a`, `sm_110f` | Thor | `sub_15C2E60` | `sub_15C1E80` | `sub_15C3950` | `sub_15C1F30` | `sub_15C21B0` |
| 10 | `sm_103`, `sm_103a`, `sm_103f` | Blackwell Ultra | `sub_15C1E50` | `sub_15C2C50` | `sub_15C3630` | `sub_15C1F50` | `sub_15C20D0` |
| 11 | `sm_120`, `sm_120a`, `sm_120f` | RTX 50 consumer | `sub_15C2D70` | `sub_15C2B90` | `sub_15C1D20` | `sub_15C1F10` | `sub_15C2450` |
| 12 | `sm_121`, `sm_121a`, `sm_121f` | DGX Spark | `sub_15C2E00` | `sub_15C2AD0` | `sub_15C3410` | `sub_15C1F70` | `sub_15C26F0` |

Map A0 (compute capability) entries also include `compute_XX` aliases for each `sm_XX` key. The total registration count: 12 SMs x 7 maps = 84 entries, plus 10 variant aliases (90a, 100a/f, 110a/f, 103a/f, 120a/f, 121a/f) x 7 maps = 70 entries. The `compute_XX` aliases add another entry per distinct silicon for map A0 only, totaling 22 compute_ entries. Grand total: **154 `sub_448E70` insert calls** (plus the 22 compute_ entries already counted within the 154).

## Variant Pointer Sharing

Architecture suffixes `a` and `f` denote silicon die variants that share the same ISA and codegen behavior within a generation. The dispatch tables encode this by registering identical function pointers:

- **`sm_90` / `sm_90a`**: Same pointers. The `a` suffix indicates the accelerated SXM variant (H100 SXM vs H100 PCIe). No ISA difference.
- **`sm_100` / `sm_100a` / `sm_100f`**: Same pointers. Blackwell datacenter, with `f` for the fabric-attached variant (NVSwitch-connected).
- **`sm_103` / `sm_103a` / `sm_103f`**: Same pointers. Blackwell Ultra (GB300).
- **`sm_110` / `sm_110a` / `sm_110f`**: Same pointers. Jetson Thor.
- **`sm_120` / `sm_120a` / `sm_120f`**: Same pointers. RTX 50 consumer / enterprise Pro.
- **`sm_121` / `sm_121a` / `sm_121f`**: Same pointers. DGX Spark.

This means the 22 architecture strings collapse to 12 distinct codegen configurations.

## Accessor Functions

Five accessor functions wrap the lazy-init + hash-lookup pattern. Each calls `sub_15C0CE0` to ensure tables are initialized, then calls `sub_449A80` to look up the appropriate map.

### `sub_15C3D60` -- cpf_optx dispatcher

```c
int64_t lookup_cpf_optx(uint64_t arch_key, bool mode) {
    init_sm_dispatch_tables(arch_key, ...);
    if (mode)
        return LinkerHash_lookup(map_cpf_optx, arch_key);      // B8
    else
        return LinkerHash_lookup(map_cpf_optx_alt, arch_key);  // B0
}
```

The `mode` boolean selects between the two cpf_optx maps. The caller (in the codegen driver) sets `mode=1` for standard compilation and `mode=0` for an alternate pass ordering.

### `sub_15C3DB0` -- nv.info emitter lookup

```c
int64_t lookup_nv_info_emitter(uint64_t arch_key) {
    init_sm_dispatch_tables(arch_key, ...);
    return LinkerHash_lookup(map_nv_info, arch_key);  // A8
}
```

Returns the per-SM compilation state factory function. The returned function pointer is called to create a 1,936-byte codegen context structure.

### `sub_15C3DD0` -- compute capability lookup

```c
uint32_t lookup_compute_capability(uint64_t arch_key) {
    init_sm_dispatch_tables(arch_key, ...);
    uint32_t *ptr = LinkerHash_lookup(map_compute_cap, arch_key);  // A0
    if (ptr)
        return *ptr;
    return 0xFFFFFFFF;  // not found
}
```

Returns a 32-bit compute capability encoding. The sentinel `0xFFFFFFFF` indicates an unrecognized architecture string.

### `sub_15C3E00` -- perf-stats dispatch

```c
int64_t dispatch_perf_stats(uint64_t arch_key, ..., arg3, arg4, arg5) {
    init_sm_dispatch_tables(arch_key, ...);
    callback = LinkerHash_lookup(map_perf_stats, arch_key);  // 90
    return callback(a2, arg3, arg4, arg5);
}
```

Looks up and immediately invokes the perf-stats callback, forwarding the caller's arguments.

### `sub_15C3E50` -- codegen options dispatch

```c
int64_t dispatch_codegen_opts(uint64_t arch_key, ..., a3, a4, a5, a6, a7) {
    init_sm_dispatch_tables(arch_key, ...);
    callback = LinkerHash_lookup(map_codegen_opts, arch_key);  // 88
    return callback(a2, a3, a4, a5, a6, a7);
}
```

Looks up and immediately invokes the codegen-options callback with 6 arguments. This is the occupancy calculator entry point.

## ISel Mega-Hub Wrappers

At `0x5272D0`--`0x527310`, five 11-byte functions serve as vtable-compatible wrappers around the ISel mega-hub functions. Each accepts 3 arguments `(context, ir_node, output)` but discards the first argument and forwards the remaining two to the actual mega-hub. These wrappers adapt a 3-argument vtable call convention to the 2-argument mega-hub interface.

```
sub_5272D0: (a1, a2, a3) -> sub_FBB810(a2, a3)    // SM75 Turing  (280 KB)
sub_5272E0: (a1, a2, a3) -> sub_D5FD70(a2, a3)    // SM80 Ampere  (239 KB)
sub_5272F0: (a1, a2, a3) -> sub_119BF40(a2, a3)   // SM89/90 Ada/Hopper (231 KB)
sub_527300: (a1, a2, a3) -> sub_126CA30(a2, a3)    // Shared PTX ISel (239 KB)
sub_527310: (a1, a2, a3) -> sub_5B1D80(a2, a3)    // MercExpand (204 KB)
```

These wrappers have 0 direct callers in the binary because they are invoked exclusively through function pointer tables. The function pointer table is populated during compilation setup and indexed by the target architecture family. The discarded `a1` parameter is the dispatch context (self pointer) that the vtable call convention requires but the mega-hubs do not need.

## The Opcode Dispatch Table

`sub_5272C0` (79,511 bytes, 4,115 lines) is the master opcode-to-SASS-encoding-ID dispatch table. It is not part of the SM vtable system described above -- it lives in the same address neighborhood and is called from the encoding engine, not through the dispatch maps. However, it is closely related: its output encoding IDs are consumed by the per-SM instruction encoders that the vtable system selects.

The function implements a two-level switch:

```c
int64_t opcode_dispatch(int64_t ctx, int64_t unused, int64_t ir_node) {
    switch (*(uint16_t*)(ir_node + 12)) {    // primary opcode class (0x000-0x174)
        case 0:
            switch (*(uint8_t*)(ir_node + 14)) {   // sub-opcode
                case 0: case 1: case 2: return 197;
                case 3: case 4:         return 691;
                case 7:                 return 526;
                case 8:                 return 697;
                default:                return 772;  // sentinel: unsupported
            }
        case 1: ...
        case 2: return 636;
        case 3: return 22;
        ...
        case 0x174: return 647;
        default:    return 772;
    }
}
```

The primary switch covers opcode classes 0 through 0x174 (372 classes). Many classes have a secondary switch on the sub-opcode byte at `ir_node + 14`, which can range from 0 to 0x56 in the most complex cases (opcode class 0x12, the memory/load-store family). The return values are SASS encoding IDs in the range 0--772, where 772 serves as the sentinel "unsupported opcode" value. There are approximately 200 distinct encoding IDs returned.

## Teardown

`sub_15C1CA0` is registered as a cleanup callback during initialization via `sub_45CC80`. It is invoked when the compilation context is destroyed. The function checks `byte_2A644C0`, and if set, clears it to 0 and calls `sub_448A40` on each of the 7 maps to free their entries. This makes the singleton re-initializable for subsequent compilation invocations within the same process.

```c
void destroy_sm_dispatch_tables(int64_t ctx, uint64_t a2) {
    if (init_guard) {
        init_guard = 0;
        LinkerHash_clear(map_cpf_optx, a2);
        LinkerHash_clear(map_cpf_optx_alt, a2);
        LinkerHash_clear(map_nv_info, a2);
        LinkerHash_clear(map_compute_cap, a2);
        LinkerHash_clear(map_reserved, a2);
        LinkerHash_clear(map_perf_stats, a2);
        LinkerHash_clear(map_codegen_opts, a2);
    }
}
```

## Dispatch Flow

The full dispatch flow from a compilation request to SM-specific codegen:

```
nvlink receives PTX/LTO input for sm_100
    |
    v
sub_4BD760 / sub_4BC6F0 (ptxas entry points)
    |
    v
sub_15C3DB0("sm_100", ...)                    -- lookup nv.info emitter
    |
    +-- sub_15C0CE0()                          -- lazy init (no-op if already done)
    +-- sub_449A80(map_nv_info, "sm_100")      -- hash lookup
    +-- returns sub_15C3840                     -- sm_100 nv.info emitter
    |
    v
sub_15C3840(ctx, options, arena, profile)      -- creates 1,936-byte codegen state
    |
    v
sub_15C3D60("sm_100", mode=1)                 -- lookup cpf_optx callback
    |
    +-- returns sub_15C2B60                    -- sm_100 cpf_optx handler
    |
    v
sub_15C2B60(ctx, state)                        -- resolves "cpf_optx" option, invokes codegen
    |
    v
sub_5272D0-sub_527310 (vtable wrapper)         -- selected per arch family
    |
    +-- discards context, forwards to mega-hub
    |
    v
sub_D5FD70 / sub_FBB810 / sub_119BF40 / ...   -- ISel mega-hub for target SM
    |
    v
sub_5272C0(ctx, unused, ir_node)               -- opcode -> encoding ID
    |
    v
per-instruction encoder (SM100+ at 0x620000)   -- emit 128-bit SASS word
```

## Design Observations

1. **Separation of concerns.** The 7 vtable maps cleanly separate 7 aspects of per-SM behavior: two cpf_optx modes, nv.info emission, compute capability query, perf-stats, codegen option calculation, and one reserved slot. Adding an 8th per-SM aspect requires only an 8th map creation in the initializer and an 8th accessor function.

2. **String-keyed dispatch.** Using string keys (`"sm_100"`, `"compute_100"`) rather than integer SM numbers means the dispatch tables naturally handle the naming convention used throughout the compilation pipeline. No integer-to-string conversion is needed at lookup time.

3. **Variant aliasing is free.** Registering `sm_100`, `sm_100a`, `sm_100f` with the same function pointer costs only 3 hash insertions per map. The alternative -- a normalization function that strips suffixes before lookup -- would add code complexity and a lookup-time cost.

4. **Double-checked locking.** The init function checks `byte_2A644C0` twice: once without the lock (fast path for already-initialized case) and once after acquiring mutex 5 (correctness under concurrent access). This is the standard DCL pattern for lazy singletons.

5. **11-byte wrappers.** The ISel mega-hub wrappers at `0x5272D0`--`0x527310` are remarkable for their size: each is exactly 11 bytes of x86-64 code (`mov rdi, rsi; mov rsi, rdx; jmp target`). They exist solely to adapt a 3-argument vtable calling convention to a 2-argument function, discarding the self pointer. The zero direct callers confirm they are invoked exclusively through function pointer indirection.
