# ToroidalRouteCache

> *Addresses apply to `libtpu.so` from the libtpu-0.0.40-cp314 wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset, base `0xe63c000`). Demangled names, member offsets, and `.rodata` strings are cross-checked against the IDA decompile (`ToroidalRouteCache::{Create, GetRouteCacheData, GetRoutePath, GetRoutePaths, GetRouteDistance}`).

## Abstract

`ToroidalRouteCache` is the **in-memory container for a precomputed (src chip, dst chip) → route table**, materialized from one baked `.binarypb.compressed` resource at slice bring-up. It is the fast-path arm of the cache-vs-generate decision in [`ResilientToroidalTopology::InitRouteSolution`](overview.md#3-the-cache-vs-generate-decision): when the discovered fault pattern matches a baked shape, `Create` loads the matching blob and lowers it into four `absl::flat_hash_map<pair<int,int>, V>` slots; the live `*ToroidalWildFirstPaths` generator only runs on a miss. Once built, the on-chip route-table emitter consults it per (src,dst) pair before computing any fresh route.

The container is deliberately polymorphic over *one* layout discriminant. A single `int` at `[obj+0]` (the `_cache_type_`) records which of four representations the baked schemes carried — signed **distance vector**, single **static path**, **bit-encoded path**, or **random-hop path set** — and that int selects which of the four maps holds the per-pair value. Three read-side accessors (`GetRouteDistance`, `GetRoutePath`, `GetRoutePaths`) each first validate that the requested representation matches `[obj+0]`, then look up the `pair<int,int>` chip-id key. A representation mismatch and a key miss are two *distinct* surfaced errors, not one — a reimplementer who collapses them will mis-diagnose a cache.

This page owns the **container object and its four-map layout**, **`Create`'s orchestration** (the load → decompress → construct → move-into-`StatusOr` chain), the **per-codename / per-twist cache-set split** that decides *which* baked blob a given silicon generation loads, and the **read-side lookup dispatch**. The proto→map expansion loop (`CacheRead`) and the `Decompress` Brotli inflate live on [`route-cache-decompress.md`](route-cache-decompress.md); the dedup cache *key* that selects the blob is on [`route-cache-dedup.md`](route-cache-dedup.md); the type-2 bit codec is on [`route-cache-codec.md`](route-cache-codec.md).

For reimplementation, the contract is:

- **The object layout** — a leading `int` layout discriminant `{0,1,2,3}` followed by four `flat_hash_map<pair<int,int>, V>` slots, only one of which is ever populated per cache.
- **`Create`'s orchestration** — `GetRouteCacheData` (path → `ReadBinaryProto` → `Decompress`) gated on a `StatusOr` OK, then ctor + `CacheRead`, then move the four maps into the `StatusOr<ToroidalRouteCache>` result; a load failure propagates as a non-OK `Status` (source line 120) rather than aborting.
- **The cache-set split** — three static `flat_set<string_view>` shape arrays (`kRouteCacheSet` 12, `kViperfishRouteCacheSet` 4, `TwistedTorusTopology::kRouteCacheSet` 8) that bound which shapes each codename family can load, and the per-codename which-set rule.
- **The lookup** — the `[obj+0]`-gated dispatch shared by all three accessors: type-guard probe → key lookup → return value or one of two errors.

| | |
|---|---|
| **Container** | `accel_ssw::deepsea::slice_builder::ToroidalRouteCache` (`_cache_type_` at `[obj+0]`) |
| **Create** | `ToroidalRouteCache::Create(Span<const int> dims, bool twisted, optional<Orientation>, const proto::ToroidalRouteCacheType&, vector<Orientation>)` @`0x20b5d6e0` (490 B) |
| **Load helper** | `GetRouteCacheData` @`0x20b5c420` (368 B) → `GetRouteCacheDataPath` @`0x20bf2080` (765 B) |
| **Constructor** | `ToroidalRouteCache::ToroidalRouteCache(const proto::ToroidalRouteCache&, const vector<Orientation>&)` @`0x20b5d8e0` (310 B) |
| **Expansion loop** | `CacheRead` @`0x20b5da20` (15157 B) → [`route-cache-decompress.md`](route-cache-decompress.md) |
| **Read accessors** | `GetRouteDistance` @`0x20b615c0` · `GetRoutePath` @`0x20b61720` · `GetRoutePaths` @`0x20b61880` |
| **Layout discriminant** | `[obj+0]` int `{0=distance, 1=static_path, 2=bit_encoded, 3=random_hop}` |
| **Four map slots** | `+0x8` distance · `+0x28` path · `+0x48` random-hop paths · `+0x68` random-hop weights |
| **Callers of Create** | `ResilientToroidalTopology::InitRouteSolution` @`0x1fbdf8a0` · `TwistedTorusTopology::InitRouteSolution` @`0x20b3f7c0` |
| **Source** | `platforms/accel_ssw/deepsea/slice_builder/internal/toroidal_route_cache.cc` |

---

## 1. The container object

`ToroidalRouteCache` is a small fixed-header struct: one `int` discriminant and four hash maps, all keyed by the same `pair<int,int>` chip-id pair `(src_chip_id, dst_chip_id)`. The offsets below are read directly from `Create` (which constructs the four maps at fixed offsets in the `StatusOr` payload) and from the three accessors (which dispatch on `[obj+0]` and index the maps).

> **NOTE — two offset frames.** `Create` builds the object *inside* a `StatusOr<ToroidalRouteCache>` payload `a1`, where `[a1+0]` is the `StatusOr` OK discriminant (`1`) and the object proper starts at `[a1+8]`. So in `Create` the layout int is `[a1+8]` and the maps are at `[a1+16/+48/+80/+112]`. In the read accessors the `this` pointer is the bare object: `[obj+0]` is the layout int and the maps are at `[obj+0x8/+0x28/+0x48/+0x68]`. Both frames describe the same struct; this page uses the bare-object frame (`+0x8`, `+0x28`, …) as canonical and notes the `StatusOr`-relative offsets where they appear.

### Object layout

| Field | Offset (bare obj) | Type | Meaning |
|-------|-------------------|------|---------|
| `_cache_type_` | `+0x0` | `int` `{0,1,2,3}` | layout discriminant; set by the ctor from the first `RouteScheme`'s `route` oneof (see [`route-cache-decompress.md`](route-cache-decompress.md#the-constructor-0x20b5d8e0--setting-obj0)) |
| distance map | `+0x8` | `flat_hash_map<pair<int,int>, superpod::routing::Coordinates>` | populated iff `_cache_type_ == 0`; the signed per-axis distance vector per pair |
| path map | `+0x28` | `flat_hash_map<pair<int,int>, vector<proto::Direction>>` | populated iff `_cache_type_ ∈ {1,2}`; the single hop sequence per pair |
| random-hop paths | `+0x48` | `flat_hash_map<pair<int,int>, vector<vector<proto::Direction>>>` | populated iff `_cache_type_ == 3`; the equal-cost path *set* per pair |
| random-hop weights | `+0x68` | `flat_hash_map<pair<int,int>, vector<signed char>>` | populated iff `_cache_type_ == 3`; the per-path `int8` probability weights, index-aligned to `+0x48` |

The exact value types are confirmed from the four `raw_hash_set<FlatHashMapPolicy<…>>` move-constructors that `Create` calls (`@0x1fbe41e0` / `4220` / `4260` / `42a0`) and from the templated `find<pair<int,int>>` instantiations in the three accessors.

> **QUIRK — four slots, one live.** Only the map matching `_cache_type_` is ever populated; the other three are constructed empty and stay empty for the life of the cache. The struct carries all four because the cache producer may bake *any* of the four representations, and the layout is fixed at compile time, not chosen per object size. A reimplementer can collapse the unused slots to a `std::variant` of four maps; the binary pays the empty-`raw_hash_set` cost (a null backing-array pointer each) instead.

> **NOTE — types 1 and 2 share a map.** `static_path` (`_cache_type_ == 1`) and `bit_encoded_path` (`_cache_type_ == 2`) both lower to `vector<proto::Direction>` and both populate the *same* `+0x28` map; they are two on-disk *encodings* of the identical in-memory value. Every shipped table in this build is `static_path` (type 1); `bit_encoded_path` is decoded by `DecodePathFromBits` on [`route-cache-codec.md`](route-cache-codec.md).

### How the discriminant is set

The ctor (`@0x20b5d8e0`) zero-initializes the four maps, then sets `[obj+0]` from the *first* scheme's `route` oneof tag (`3→0`, `5→2`, `4→1`), escalating `1→3` if **any** scheme carries `random_first_hop`. This is documented in full on [`route-cache-decompress.md`](route-cache-decompress.md#the-constructor-0x20b5d8e0--setting-obj0); the only fact this page needs is that the discriminant is *homogeneous per blob* — the cache producer must emit a single `route` representation across all schemes in one file, and `CacheRead`'s per-scheme `RetCheck`s turn a heterogeneous file into a non-OK `Status` rather than a silent mis-insert.

---

## 2. `Create` @0x20b5d6e0 — load, decompress, construct, package

`Create` is the one entry that materializes a precomputed cache. It is 490 bytes and reads as a `StatusOr` happy-path with a single early-out on load failure. It is invoked by **both** `ResilientToroidalTopology::InitRouteSolution` (`@0x1fbdf8a0`, the resilient family) and `TwistedTorusTopology::InitRouteSolution` (`@0x20b3f7c0`, the twisted family) — the same container serves both, with the codename/twist split living in the cache-set arrays of §3 and in the `ToroidalRouteCacheType` argument that picks the blob.

### Entry point

```text
ResilientToroidalTopology::InitRouteSolution  0x1fbdf8a0   ── resilient family (per-codename)
TwistedTorusTopology::InitRouteSolution       0x20b3f7c0   ── twisted family
  └─ ToroidalRouteCache::Create               0x20b5d6e0   ── *** THIS PAGE ***
       ├─ GetRouteCacheData                   0x20b5c420   ── path → ReadBinaryProto → Decompress
       │    ├─ GetRouteCacheDataPath          0x20bf2080   ── "embed://<lower(Type)>_data/<shape>.binarypb.compressed"
       │    ├─ tsl::ReadBinaryProto           (Env::Default) ── read CompressedToroidalRouteCache off the baked resource
       │    └─ Decompress                     0x20b63320   ── Brotli → proto::ToroidalRouteCache  → route-cache-decompress.md
       ├─ ToroidalRouteCache::ctor            0x20b5d8e0   ── set [obj+0]; then CacheRead → 4 maps  → route-cache-decompress.md
       └─ move 4 maps into StatusOr<ToroidalRouteCache>
```

### Algorithm

```c
// 0x20b5d6e0 — Create(a1 <- StatusOr storage, dims, twisted, orient, ToroidalRouteCacheType, dedup_orientations)
StatusOr<ToroidalRouteCache> Create(...) {
    StatusOr<proto::ToroidalRouteCache> data = GetRouteCacheData(...);   // 0x20b5c420

    if (!data.ok()) {                                       // v25 != OK sentinel (&dword_0+1)
        a1.status = AddSourceLocationImpl(data.status,      // toroidal_route_cache.cc:120
                       "…/toroidal_route_cache.cc");        // propagate, DO NOT abort
        return a1;
    }

    proto::ToroidalRouteCache pb;                           // ctor(&pb, 0)
    // arena-aware adopt: InternalSwap if same arena, else CopyFrom  (0x20c040e0 / 0x20c040a0)
    if (same_arena(pb, data)) pb.InternalSwap(&data.value);
    else                      pb.CopyFrom(&data.value);

    ToroidalRouteCache obj(pb, dedup_orientations);         // 0x20b5d8e0 — sets obj._cache_type_, runs CacheRead
    a1[8] = obj._cache_type_;                               // copy the layout int into the StatusOr payload

    // move-construct the four maps into the StatusOr payload at +16/+48/+80/+112
    move(a1+16,  obj.distance_map);        // FlatHashMapPolicy<pair<int,int>, Coordinates>          0x1fbe41e0
    move(a1+48,  obj.path_map);            // FlatHashMapPolicy<pair<int,int>, vector<Direction>>    0x1fbe4220
    move(a1+80,  obj.random_paths_map);    // FlatHashMapPolicy<pair<int,int>, vector<vector<Dir>>>  0x1fbe4260
    move(a1+112, obj.random_weights_map);  // FlatHashMapPolicy<pair<int,int>, vector<int8>>         0x1fbe42a0

    a1.discriminant = 1;                                    // StatusOr now holds a value (*a1 = 1)
    return a1;
}
```

Byte-anchored facts:

- **OK is a sentinel-pointer test, not a tag read.** `GetRouteCacheData` returns into a `StatusOr` whose payload-pointer equals `(&dword_0 + 1)` when OK (the absl inlined-OK sentinel). The compare at `0x20b5d6e0`+offset is exactly `v25 == &dword_0+1`; any other value is the error path.
- **The load failure is structured.** On `!data.ok()` the only work is `AddSourceLocationImpl(status, 120, "…/toroidal_route_cache.cc")` — the error is tagged with the source line and propagated up to `InitRouteSolution`, which then falls to the live generator or fails the slice. Contrast the *constructor*, which `CHECK`-fails on a malformed proto (a baked-resource bug). Two failure disciplines: a *missing/corrupt resource* is recoverable; a *malformed parsed proto* is a build bug and aborts.
- **The adopt is arena-aware.** Whether the parsed proto is moved (`InternalSwap`) or copied (`CopyFrom`) into the local `pb` depends on whether the two messages share a protobuf arena (the `(v24 & 1)` / `(v27 & 1)` arena-pointer tests). Functionally identical; a reimplementation may always copy.
- **`Create` does not touch the maps' contents.** All per-scheme decode happens inside the ctor's call to `CacheRead`; `Create` only moves the finished maps. The four move-ctors and the matching `destructor_impl` calls (`@0x1fbe42e0`/`43c0`/`4660`) bracket the move so the temporary `obj` is left empty.

> **NOTE — the resource path is built in `GetRouteCacheDataPath`.** The `ToroidalRouteCacheType` enum (lowercased) and the dimension/twist/orientation suffix are `StrFormat`ed into `"embed://%s_data/%s.binarypb.compressed"` *before* the read. That path-build is where the codename is bound to a blob; it is documented on [`route-cache-decompress.md`](route-cache-decompress.md#1-prototoroidalroutecachetype--the-filecodename-selector). `Create` itself never sees the codename string — only the already-resolved `ToroidalRouteCacheType` argument.

### Function map

| Function | Addr | Size | Role | Confidence |
|----------|------|------|------|------------|
| `ToroidalRouteCache::Create` | `0x20b5d6e0` | 490 | load → decompress → construct → move into `StatusOr` | CERTAIN |
| `GetRouteCacheData` | `0x20b5c420` | 368 | build path, `ReadBinaryProto`, `Decompress` | CERTAIN |
| `GetRouteCacheDataPath` | `0x20bf2080` | 765 | `ToroidalRouteCacheType` + shape → `embed://…` path | HIGH |
| `ToroidalRouteCache::ctor` | `0x20b5d8e0` | 310 | set `[obj+0]`, call `CacheRead` | CERTAIN |
| `CacheRead` | `0x20b5da20` | 15157 | per-scheme decode into the four maps | CERTAIN |

---

## 3. The per-codename / per-twist cache-set split

`Create` loads exactly one blob; *which* shapes a codename family is allowed to load is bounded ahead of time by three static `flat_set<string_view>` shape-name arrays. These are `.data` constants whose member count is fixed by the `array<string_view, N>` template parameter, so the inventory is byte-exact.

### The three cache sets

| Cache set | Symbol (`.data`) | N | Shapes | Confidence |
|-----------|------------------|---|--------|------------|
| `kRouteCacheSet` | `ResilientToroidalTopology::kRouteCacheSet` @`0x21f57380` | 12 | `4x4x4`, `4x4x8`, `4x4x8_twisted`, `4x4x12`, `4x4x16`, `4x8x8`, `4x8x8_twisted`, `8x8x8`, `8x8x16_twisted`, `8x16x16_twisted`, `12x12x12`, `12x12x24_twisted` | HIGH |
| `kViperfishRouteCacheSet` | `ResilientToroidalTopology::kViperfishRouteCacheSet` @`0x21f57440` | 4 | `16x16x16`, `20x20x20`, `12x24x24_twisted`, `16x16x32_twisted` (the large pods) | HIGH |
| `TwistedTorusTopology::kRouteCacheSet` | @`0x22011f88` | 8 | `4x8_twisted`, `4x4x8_twisted`, `4x8x8_twisted`, `8x8x16_twisted`, `8x16x16_twisted`, `12x12x24_twisted`, `12x24x24_twisted`, `16x16x32_twisted` | HIGH |

(Shapes listed in alphabetical `flat_set` order; the `_twisted` suffix is the geometry tag, not a separate axis.)

### Per-codename which-set rule

The selector is **not** the TPU version. `InitRouteSolution` switches on `HIDWORD(v90)` — the `ocs_fault_links_symmetry` discriminant returned by `TopologyFaults::GetSymmetryProperty` — whose **only** valid values are `{1, 2, 4}` (any other value yields `MakeErrorImpl<12>("Invalid ocs_fault_links_symmetry: …")` at source line 266). Each case lazily constructs one of three function-local `RouteCacheDeduplicator` singletons inside `GetCacheDeduplicator(int)` — `pf_deduplicator`, `vf_deduplicator`, `gf_deduplicator` — then `UpdateDeduplicator`s it with the bounding `flat_set`s. The default set is built by `RouteCacheDeduplicator::Create` (lambda `@0x21f574c0`); viperfish *adds* the large-pod set via `CreateResilientViperfish` (reached through `0x1fbe3e60`) **after** the default set, so it covers `12 + 4 = 16` shapes. The symmetry→deduplicator→sets mapping (each `UpdateDeduplicator` is `CHECK`-asserted OK) is:

| `ocs_fault_links_symmetry` | Deduplicator | Sets loaded (asserted by `CHECK`-fail strings) | Codename family |
|----------------------------|--------------|------------------------------------------------|-----------------|
| `1` | `pf_deduplicator` | `kRouteCacheSet` (12) | pufferfish (v4) |
| `2` | `vf_deduplicator` | `kRouteCacheSet` (12) + `kViperfishRouteCacheSet` (4) = 16, via `CreateResilientViperfish` | viperfish (v5) |
| `4` | `gf_deduplicator` | `kRouteCacheSet` (12) + `k6acc60406RouteCacheSet` (4) = 16 | `6acc60406` / TPU7x (gen 6) |
| any other | — | none — `MakeErrorImpl<12>("Invalid ocs_fault_links_symmetry: …")`, line 266 | invalid |

The codename string is bound separately: a secondary `switch(HIDWORD(v90))` (lines 429/432/435) maps `1→2`, `2→3`, `4→4` into a `proto::ToroidalRouteCacheType` value, which `GetRouteCacheDataPath` lowercases into the `embed://<codename>_data/…` path — so symmetry-1 loads `pufferfish_*`, symmetry-2 loads `viperfish_*`, symmetry-4 loads `6acc60406_*`. Codenames that never reach this switch (jellyfish v2, dragonfish v3, ghostlite v6e) have **no** resilient cache deduplicator here; a fault on those parts fails the symmetry gate and the slice reshapes (see [`route-table-generation.md`](route-table-generation.md)).

> **QUIRK — the split is *coverage*, not algorithm.** All baked tables are the same `*ToroidalWildFirstPaths` output; `vf`/`gf` differ from `pf` only in (a) which *shapes* have a baked table — `vf` and `gf` each add their own 4-shape large-pod set on top of the shared 12 — and (b) how *many* fault links each shape's table tolerates (`6acc60406`'s `_fault_dim_*` fault catalog is materially larger than viperfish's, which is in turn larger than pufferfish's — the exact `FileToc` sizes were not re-derived here). A reimplementer who copies the pufferfish 12-shape set onto `vf`/`gf` silicon silently loses protection for the four large pods and falls to the live generator on a large-pod fault.

> **CANON — `6acc60406` is TPU7x, not "ghostfish."** The deduplicator variable is named `gf_deduplicator`, but that abbreviation is not a binary-attested codename; the only attested token is the hash `6acc60406`, which is the TPU7x generation (gen 6, distinct from ghostlite/v6e). The earlier "ghostfish" gloss is dropped — it was an inference from the `gf_` prefix, not a string in the binary.

> **CORRECTION (CACHE-1) —** the exported resilient shape-name arrays are **two** `flat_set<string_view>`s: `ResilientToroidalTopology::kRouteCacheSet` (12) and `ResilientToroidalTopology::kViperfishRouteCacheSet` (4); the twisted topology carries its **own** third (`TwistedTorusTopology::kRouteCacheSet`, 8). A fourth, `k6acc60406RouteCacheSet` (4), is referenced only via the `gf_deduplicator` `UpdateDeduplicator` `CHECK`-fail string and the `off_21F57480` local — it is the symmetry-4 (`6acc60406`/TPU7x) large-pod set, structurally the `gf` counterpart of `kViperfishRouteCacheSet`. So three deduplicators (`pf`/`vf`/`gf`) draw from four bounding arrays: `pf`→{12}, `vf`→{12,4}, `gf`→{12,4}. The `flat_set` arrays bound *which shape strings can hit*; the deduplicators are *which populated lookup table answers*. Marked CERTAIN (each `UpdateDeduplicator` target is named verbatim in a `CHECK` assertion string).

### Embedded resources

The shapes resolve to a small number of `tsl::memfile`-registered blobs (registered at static-init via `GlobalRegisterFiles(name, FileToc*)` @`0x20c04960`), **not** one file per shape. The embedded route-cache resources are, by `cache_ici_resiliency_*` / `cache_twisted_torus_*` name prefix in `.rodata`:

- a `_config` and per-axis `_fault_dim_x` / `_fault_dim_y` / `_fault_dim_z` fault catalog for **each** of `pufferfish`, `viperfish`, and `6acc60406` (`6acc60406`'s fault catalogs are the largest);
- per-axis `_x_data` / `_y_data` / `_z_data` Brotli `.binarypb.compressed` route tables for **each** of `pufferfish`, `viperfish`, **and `6acc60406`** — i.e. `6acc60406`/TPU7x *does* ship baked route tables, not just a fault config;
- the twisted pair `cache_twisted_torus_config` + `cache_twisted_torus_data` (plus a `cache_twisted_torus_all` shape catalog).

The `FileToc` struct is `{+0x00 name*, +0x08 data*, +0x10 size:u64, +0x18 hash[16]}`. The compressed-resource decode is owned by [`route-cache-decompress.md`](route-cache-decompress.md); the dedup-key→shape-name selection by [`route-cache-dedup.md`](route-cache-dedup.md).

---

## 4. The read-side lookup

Once built, the cache is consulted per (src,dst) pair by three accessors that share one dispatch skeleton. They are the bridge from the DMA descriptor's routing-table index (which *is* the `(src_chip_id, dst_chip_id)` pair, via `net_util::MapSrcDstCoreToRoutingTableIndex` @`0x1c6aea80`) to a concrete `vector<proto::Direction>` or distance vector. Route generation consults this map *before* computing a fresh route — a hit is the whole point of the precomputed cache.

### The three accessors

| Accessor | Addr | Reads map | Valid when `[obj+0]` is | Returns |
|----------|------|-----------|-------------------------|---------|
| `GetRouteDistance(src,dst)` | `0x20b615c0` | `+0x8` distance | `0` | `superpod::routing::Coordinates` (signed per-axis vector) |
| `GetRoutePath(src,dst)` | `0x20b61720` | `+0x28` path | `1` or `2` | `vector<proto::Direction>` (single hop sequence) |
| `GetRoutePaths(src,dst)` | `0x20b61880` | `+0x48` random-hop paths | `3` | `vector<vector<proto::Direction>>` (equal-cost path set) |

### Dispatch skeleton

All three are byte-for-byte the same shape (the `find` instantiations and the source-line tags differ). The logic is a **type guard** followed by a **key lookup**, with two distinct error exits:

```c
// 0x20b61720 — GetRoutePath(this <- StatusOr<...> storage, obj, src, dst)
StatusOr<...> GetRoutePath(const ToroidalRouteCache* obj, int src, int dst) {
    pair<int,int> key{src, dst};

    // --- STAGE 1: type guard. Probe whether THIS object's layout can answer the
    //     requested representation at all. The guard is keyed on [obj+0]. ---
    if (obj->_cache_type_ == 3) {                          // object holds random-hop sets …
        if (random_paths_map.find(key)) goto found;        //   +0x48
        goto type_mismatch;
    }
    if (obj->_cache_type_ != 0) {                          // type 1 or 2: holds vector<Direction>
        if (path_map.find(key)) goto found;                //   +0x28
type_mismatch:
        // requested a path, but layout is distance (type 0) → wrong representation
        return MakeErrorImpl<9>(                           // toroidal_route_cache.cc:393
            "ICI route cache is not stored in full path format.");
    }
    // _cache_type_ == 0 (distance) but caller wants a path → type mismatch
    if (!distance_map.find(key)) goto type_mismatch;       //   +0x8

    // --- STAGE 2: key lookup in the CORRECT map for this accessor ---
found:
    if (path_map.find(key)) {                              //   +0x28 again, return the value
        this->value = &found_value;                        //   16-byte vector header copy
        this->discriminant = 1;
        return this;
    }
    // key absent → distinct from a type mismatch
    return MakeErrorImpl<3>(FormatPack(                    // toroidal_route_cache.cc:401
        "ICI routing scheme between src %d and dest %d does not exist in cache.",
        src, dst));
}
```

The other two accessors are the same with the stage-2 map swapped: `GetRoutePaths` looks up the `+0x48` random-hop map (lines 413/419) and `GetRouteDistance` looks up the `+0x8` distance map (lines 373/381), with the type-guard message changed to `"… not stored in distance vector format."` for the distance accessor.

> **GOTCHA — two errors, not one. Both are surfaced, neither aborts.** A *type mismatch* (`MakeErrorImpl<9>`, "not stored in … format") means the cache *exists* but holds a different representation than the accessor wants — a caller-side bug, calling `GetRoutePath` on a distance-only cache. A *key miss* (`MakeErrorImpl<3>`, "ICI routing scheme between src %d and dest %d does not exist in cache.") means the cache is the right type but has no entry for that pair — a coverage gap. A reimplementation that returns one error for both will mis-route a diagnostic: the first says "you asked the wrong accessor," the second says "this (src,dst) was never baked." The status codes differ deliberately (9 = `FailedPrecondition` vs 3 = `InvalidArgument`).

> **NOTE — the stage-1 probe is in a *different* map ordering than stage-2.** In the decompile each accessor's stage-1 guard uses a temporary key `__PAIR64__(dst,src)` while stage-2 uses `{src,dst}`. The guard's purpose is solely to convert a layout/representation mismatch into the type-mismatch error before the real lookup; the canonical `(src,dst)` key for the value lookup is the stage-2 one. Reimplement stage 1 as a pure `_cache_type_` check against the accessor's expected type; the probe `find` is an artifact of the inlined guard and does not affect correctness.

### What a hit yields

A `GetRoutePath` hit returns a `vector<proto::Direction>` — a strict dimension-ordered hop list (all X hops, then Y, then Z) for the minimal-distance pairs, or a wild-first detour (one non-minimal hop out, the remainder in dimension order, one hop back) for pairs whose minimal path would cross a faulty link. Each `Direction` lowers, downstream, to an `output_link_index` (0..3 SerDes port) plus a `next_hop_chip_id` row in the `PerLinksRoutingTable` that the on-chip routing engine indexes. The `Direction` packing (`orientation` + `polarity`) is shared with the live generator and is documented on [`get-static-path.md`](get-static-path.md); the per-hop bit codec on [`route-cache-codec.md`](route-cache-codec.md). A `GetRouteDistance` hit returns the signed torus-reduced distance vector for the pair — the same quantity [`get-distances.md`](get-distances.md) computes live.

---

## Related Components

| Component | Relationship |
|-----------|--------------|
| [`route-cache-decompress.md`](route-cache-decompress.md) | `Decompress` (Brotli inflate) + the ctor `[obj+0]` derivation + the `CacheRead` per-scheme expansion loop that fills the four maps `Create` then moves |
| [`route-cache-dedup.md`](route-cache-dedup.md) | the `RouteCacheDeduplicator` cache key that selects *which* shape/blob `Create` loads, and the `vector<Orientation>` `CacheRead` consumes |
| [`route-cache-codec.md`](route-cache-codec.md) | the type-2 `bit_encoded_path` decoder and the `TopologyRotationHelper` axis permutation applied to every cached key/coordinate |
| [`route-table-generation.md`](route-table-generation.md) | the live `*ToroidalWildFirstPaths` generator that runs on a cache miss, and the fault-symmetry gate that decides whether any cache applies |
| [`get-static-path.md`](get-static-path.md) | the `proto::Direction` packing shared by the cache's `vector<Direction>` entries and the live path generator |
| [`get-distances.md`](get-distances.md) | the live torus-reduced distance the `+0x8` distance map mirrors |

## Cross-References

- [`overview.md`](overview.md) — the ICI routing section map; `ToroidalRouteCache::Create` is the cache arm of `InitRouteSolution`'s cache-vs-generate decision, and the three dedup sets vs the cache-set arrays
- [`route-cache-decompress.md`](route-cache-decompress.md) — the `Decompress` boundary, the constructor's `[obj+0]` discriminant, and the `CacheRead` expansion loop
- [`route-cache-dedup.md`](route-cache-dedup.md) — the dedup key and orientation vector
- [`route-cache-codec.md`](route-cache-codec.md) — the bit codec and rotation math
- [`route-table-generation.md`](route-table-generation.md) — the live fallback generator and the fault gate
- [`get-static-path.md`](get-static-path.md) — the `DirectionHops` packing of the cached path entries
- [`get-distances.md`](get-distances.md) — the torus-reduced distance vector
- [`../forensics/trailing-zstd-blob.md`](../forensics/trailing-zstd-blob.md) — the binary's trailing compressed payload (distinct from the per-resource Brotli route-cache blobs decoded here)
- [back to index](../index.md)
