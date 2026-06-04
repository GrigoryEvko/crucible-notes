# RouteCacheDeduplicator — Cache Key and the Three Codename Sets

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary ships with full C++ symbols (`.text` VMA == file offset, base `0xe63c000`); every address below is a VMA. Demangled names, struct offsets, and string literals are cross-checked against the IDA decompile. Other versions will differ.*

## Abstract

`RouteCacheDeduplicator` is the **lookup table that turns a slice's shape into a pre-baked route-cache blob**, and the mechanism that lets every axis rotation of a shape share *one* baked blob. It is the first thing `InitRouteSolution` touches on the precomputed (fast) path: before any blob is read off disk, the topology's `{dim_sizes, is_twisted, orientation}` triple is hashed against a populated `FlatHashMap`. A hit returns the **canonical** identifier the blob was baked under, plus the `vector<proto::Orientation>` rotation that remaps that canonical chip-id space onto *this* topology. That rotation vector is exactly what [`route-cache-decompress.md`](route-cache-decompress.md)'s `CacheRead` consumes to re-key the in-memory maps.

The deduplicator exists because the baked-blob inventory is small: there are only eight twisted shapes in `kRouteCacheSet`, but a physical slice may be a 12x12x24 torus twisted on the *X*, *Y*, or *Z* axis. Rather than bake a blob per (shape × axis), the producer bakes one canonical blob per shape and registers all four orientations (X/Y/Z + the un-oriented form) of every shape as map keys that point back to that one canonical entry. The map **value** is the orientation rotation that recovers the queried orientation from the canonical one — the geometric payoff that the rotation helper in `CacheRead` applies.

This page owns the **dedup key** (`RouteCacheIdentifier`), the **`Find`/`Insert`/`UpdateDeduplicator` lookup-and-populate path**, and the **per-codename set selection** — the no-arg singleton fed only by `kRouteCacheSet`, versus the codename-keyed `GetCacheDeduplicator(int)` singletons that layer `k6acc60406RouteCacheSet` or `kViperfishRouteCacheSet` on top of the base set. The cache *container* (the four `FlatHashMap`s and the runtime read-side dispatch) lives on [`toroidal-route-cache.md`](toroidal-route-cache.md); the `Decompress` + proto-to-map expansion lives on [`route-cache-decompress.md`](route-cache-decompress.md); the per-axis rotation math lives on [`route-cache-codec.md`](route-cache-codec.md).

For reimplementation, the contract is:

- **The key** — `RouteCacheIdentifier{ vector<int> dim_sizes; bool is_twisted; optional<proto::Orientation> orientation }`, hashed via an Abseil `combine<vector<int>, bool, optional<Orientation>>` with `is_twisted` hashed *inverted* (`^ 1`).
- **`Find`** — a `FlatHashMap` lookup that, on hit, copies out the matched slot's canonical `RouteCacheIdentifier` *and* the value `vector<Orientation>` rotation; on miss returns a not-found flag.
- **`UpdateDeduplicator`** — for each topology string in a set, `ParseTopologyString` it, then `Insert` four variants (X/Y/Z + NONE) so all rotations register against one canonical entry.
- **The three sets** — `kRouteCacheSet` (base, eight twisted shapes), `k6acc60406RouteCacheSet`, `kViperfishRouteCacheSet`. The no-arg singleton loads only the base set; the `6acc60406` (TPU7x) and viperfish singletons each load the base set **plus** their codename-specific set.

| | |
|---|---|
| **Lookup** | `RouteCacheDeduplicator::Find(const RouteCacheIdentifier&)` @ `0x20b59000` |
| **Insert (canonicalize)** | `RouteCacheDeduplicator::Insert(const RouteCacheIdentifier&)` @ `0x20b58340` |
| **Populate a set** | `(anon)::UpdateDeduplicator(RouteCacheDeduplicator&, SetView<string_view>)` @ `0x1fbe3780` |
| **Key hash** | `absl::…::combine<vector<int>, bool, optional<proto::Orientation>>` @ `0x20b5aa40` |
| **Parse a shape string** | `slice_builder::ParseTopologyString(string_view)` @ `0x20b480c0` |
| **No-arg singleton** | `(anon)::GetCacheDeduplicator()::deduplicator` (guard @ `0x2258fc28`) |
| **Codename singletons** | `(anon)::GetCacheDeduplicator(int)::{pf,vf,gf}_deduplicator` |
| **Base set** | `kRouteCacheSet` @ `0x22011f88` (8 `{const char*, size_t}` pairs, `.data.rel.ro`) |
| **Codename sets** | `k6acc60406RouteCacheSet`, `kViperfishRouteCacheSet` (`ResilientToroidalTopology::` members) |
| **Source** | `platforms/accel_ssw/deepsea/slice_builder/internal/{toroidal_route_cache,topology}.cc` |

Confidence is **Confirmed (byte-anchored)** for the key field walk, the `Find` slot-copy offsets, the `Insert` canonicalization chain, the `UpdateDeduplicator` four-variant fan-out, and the three-set codename layering — each cross-checked against the IDA decompile (the `combine<…>` template demangle, the `FlatHashMapPolicy<RouteCacheIdentifier, pair<…, vector<Orientation>>>` `find`/`find_or_prepare_insert` instantiations, and the `MakeCheckFailString` literals naming each set). The two **LOW** / **HIGH** items are called out inline (the `int → ToroidalRouteCacheType` table, and the `code` source).

---

## 1. The dedup key — `RouteCacheIdentifier`

The key is a three-field struct that identifies a unique route-cache entry by *slice shape*, not by chip pair. Two slices with the same shape, twist, and axis-orientation share an entry; the per-`(src,dst)` data lives inside the blob that entry points to.

```c
struct RouteCacheIdentifier {                       // sizeof ~0x28
    std::vector<int>            dim_sizes;           // +0x0 base / +0x8 end / +0x10 cap
    bool                        is_twisted;          // +0x18   (hashed inverted: ^ 1)
    std::optional<proto::Orientation> orientation;   // +0x1c value (int) / +0x20 has_value (bool)
};
```

The byte offsets are confirmed from the hash-combine field walk at `0x20b5aa40`. Its demangled template signature is `combine<std::vector<int>, bool, std::optional<proto::Orientation>>` — i.e. the three fields in declaration order:

```c
// 0x20b5aa40 — HashStateBase<MixingHashState>::combine<vector<int>, bool, optional<Orientation>>
//   step 1: CRC32 over the dim_sizes int vector (4 bytes / 8 bytes per stride; tail-folded)
v11 = _mm_crc32_u32(v11, *(uint32_t*)v7);                 // each int element
…
// step 2: is_twisted, hashed INVERTED — (*a3 ^ 1)
v15 = _mm_crc32_u64(HIDWORD(v14), -(int64_t)(uint8_t)*a3);
v16 = _mm_crc32_u64((unsigned)v14, 3LL * (*a3 ^ 1u) - 3); // line 0x20b5aae4
// step 3: the optional<Orientation> { value @ +0, has_value @ +4 }
```

> **QUIRK — `is_twisted` is hashed inverted (`^ 1`).** The boolean is XOR-ed with 1 before it enters the CRC mix (`3 * (is_twisted ^ 1) - 3`). This is harmless to correctness — the hash is internally consistent — but a reimplementer who hashes the raw boolean produces a *different* hash table layout and will not interoperate with a baked dedup if one were ever serialized. It is purely an artifact of how the struct's bytes were fed to `absl::Hash`; reproduce it verbatim only if byte-for-byte hash compatibility matters.

The `proto::Orientation` enum (used both as the optional key field and as the value-vector element type) is a dense `[0,6]` protobuf enum:

| value | name | | value | name |
|-------|------|-|-------|------|
| 0 | `UNKNOWN` | | 4 | `A` |
| 1 | `X` | | 5 | `B` |
| 2 | `Y` | | 6 | `C` |
| 3 | `Z` | | | |

Only `X`/`Y`/`Z` (1/2/3) and the un-set (`has_value == false`) form are produced by `UpdateDeduplicator` (§3); `A`/`B`/`C` are part of the enum range but unused as dedup orientations in this build.

---

## 2. The deduplicator map and `Find` @0x20b59000

The deduplicator is a single Abseil flat hash map whose policy is byte-confirmed from the `find` instantiation inside `Find`:

```text
absl::container_internal::raw_hash_set<
  absl::container_internal::FlatHashMapPolicy<
    RouteCacheIdentifier,                                            // KEY
    std::pair<RouteCacheIdentifier, std::vector<proto::Orientation>> // VALUE
  >>
```

So each slot stores `(key, value)` where `value` is itself `pair<canonical RouteCacheIdentifier, vector<Orientation>>`. The **value's** `RouteCacheIdentifier` is the *canonical* (un-rotated) shape the baked blob was written under; the `vector<Orientation>` is the rotation that maps that canonical shape to the queried one. Multiple keys (the four orientations of a shape) point at the same canonical entry — that is the dedup.

`Find` is a hit-copy routine. On a hit it materialises a small `StatusOr<RouteCacheIdentifier>`-shaped result by copying the matched slot's value:

```c
// 0x20b59000 — Find(out <- a1, const RouteCacheIdentifier* key <- a2, hash <- a3)
__int64 Find(out, key, hash) {
    if (map.find<RouteCacheIdentifier>(key, hash)) {       // FlatHashMapPolicy find → slot ptr v5
        // --- copy the slot's CANONICAL RouteCacheIdentifier into out[0x0..0x20] ---
        out[0..0x10] = {};                                 // empty dim_sizes vector header
        n = slot->dim_sizes.size();                        // [slot+0x30]
        if (n) {
            out.dim_sizes = new int[n];                    // operator new(4*n)
            memcpy(out.dim_sizes, slot->dim_sizes, 4*n);   // [slot+0x28] → out+0x0
        }
        out.is_twisted  = *(uint8_t*)(slot+0x48);          // canonical is_twisted
        out.orientation = *(uint64_t*)(slot+0x40);         // canonical optional<Orientation>
        // --- copy the VALUE vector<Orientation> (the rotation) into out[0x28..0x38] ---
        m = slot->orient_vec.size();                       // [slot+0x58]
        if (m) {
            out.orient_vec = new Orientation[m];           // operator new(4*m)
            memcpy(out.orient_vec, slot->orient_vec, 4*m); // [slot+0x50] → out+0x28
        }
        out.ok = 1;                                        // [out+0x40] = found
    } else {
        out.dim_sizes_hdr = 0;
        out.ok = 0;                                        // [out+0x40] = not found
    }
    return out;
}
```

The slot offsets are byte-confirmed: the canonical `dim_sizes` vector is at `slot+0x28/0x30`, the canonical `is_twisted`/`orientation` at `slot+0x48`/`slot+0x40`, and the value `vector<Orientation>` rotation at `slot+0x50/0x58`. The result's success byte is `[out+0x40]`. On a miss the result carries an empty `dim_sizes` header and `ok == 0`.

> **NOTE — what the caller does with each half.** The returned canonical `RouteCacheIdentifier` feeds `GetRouteCacheDataPath` to compute the `embed://…/<shape>.binarypb.compressed` path (the *canonical* shape names the file); the returned `vector<Orientation>` feeds `CacheRead`'s `TopologyRotationHelper` (the rotation re-keys the loaded maps onto the *queried* topology). A reimplementer must thread *both* halves of the result, not just the hit/miss flag. See [`route-cache-decompress.md`](route-cache-decompress.md).

---

## 3. Populating a set — `UpdateDeduplicator` @0x1fbe3780 and `Insert` @0x20b58340

`UpdateDeduplicator` takes a `gtl::SetView<string_view>` (one of the three `k*RouteCacheSet` tables) and registers every shape in it. For each topology string it parses the shape, then inserts the four orientation variants:

```c
// 0x1fbe3780 — UpdateDeduplicator(RouteCacheDeduplicator& dedup, SetView<string_view> set)
for (string_view topo : set) {
    auto [dims, is_twisted] = ParseTopologyString(topo);   // 0x20b480c0
    // four Insert variants — orientation value (v30) X(1) / Y(2) / Z(3), then the NONE form
    Insert(dedup, {dims, is_twisted, /*orient=*/X, /*has=*/true});   //  v30=1  @0x1fbe385c-ish
    Insert(dedup, {dims, is_twisted, /*orient=*/Y, /*has=*/true});   //  v30=2
    Insert(dedup, {dims, is_twisted, /*orient=*/Z, /*has=*/true});   //  v30=3
    Insert(dedup, {dims, is_twisted, /*orient=*/NONE, /*has=*/false});//  the un-oriented form
}
```

The decompile confirms the fan-out: a chain of `Insert` calls with the orientation field (`v30`) set to `1`, `2`, `1`, `2`, then `3`, plus the un-oriented (NONE/`has_value == false`) path — the four-key registration per shape that lets one canonical blob serve every axis rotation.

`ParseTopologyString` @ `0x20b480c0` splits the string on `'x'` (Abseil `ByChar('x')`), parses each segment with `safe_strto32_base` into `dim_sizes`, and sets `is_twisted` from a `"_twisted"` suffix test (`setne @0x20b481a1`). So `"12x12x24_twisted"` → `dims=[12,12,24], is_twisted=true`, and the 2-axis `"4x8_twisted"` → `dims=[4,8], is_twisted=true`.

`Insert` @ `0x20b58340` does not store the queried identifier verbatim; it **canonicalizes** first:

```c
// 0x20b58340 — Insert(RouteCacheDeduplicator& dedup, const RouteCacheIdentifier& id)
auto faulty   = ToFaultyDimensions(id);          // 0x20b57b40 — vector<FaultyDimension> (stride 12)
sort(faulty);                                     // introsort 0x20b59140
auto canonical = ToRouteCacheIdentifier(faulty);  // 0x20b57d80 — the un-rotated canonical shape
auto rotation  = ToRotationMapper(canonical, id); // 0x20b58160 — the Orientation rotation list
dedup.find_or_prepare_insert(canonical) = {canonical, rotation};   // 0x20b5b680
```

`ToRouteCacheIdentifier` validates that at most one dimension is "faulty" (i.e. at most one axis is rotated); two trip the abort string **"Only one faulty orientation is allowed but found two:"** (`.rodata` @ `0xa244c87`, with a `" v.s."` separator @ `0xa291630`). The stored value pair is `{canonical key, rotation vector}` — exactly what `Find` copies out.

> **GOTCHA — the key you query is not the key that is stored.** `Insert` registers the *canonical* identifier as the map key but is *called* once per orientation variant. The four variants all canonicalize to (near) the same shape but carry distinct rotations, so the map ends up with one entry per distinct canonical shape whose value records how to reach the queried orientation. A reimplementer who stores the queried `RouteCacheIdentifier` directly (skipping `ToFaultyDimensions → ToRouteCacheIdentifier`) breaks the rotation payoff: `CacheRead` would then receive an identity rotation and mis-key a rotated slice's maps.

---

## 4. The three codename sets and per-singleton selection

There are **three** topology-string sets and **two distinct families of singleton** that consume them. This is the structural heart of the page and the one place earlier reconstruction conflated the sets.

### 4.1 The base set — `kRouteCacheSet` @0x22011f88

`TwistedTorusTopology::kRouteCacheSet` @ `0x22011f88` is eight `{const char*, size_t}` pairs in `.data.rel.ro`, RELA-resolved to eight twisted-shape strings. (`ResilientToroidalTopology::kRouteCacheSet` @ `0x21f57380` is a *distinct* data symbol with a **different, larger** inventory — twelve `{const char*, size_t}` pairs covering both plain and twisted shapes: `4x4x4, 4x4x8, 4x4x8_twisted, 4x4x12, 4x4x16, 4x8x8, 4x8x8_twisted, 8x8x8, 8x8x16_twisted, 8x16x16_twisted, 12x12x12, 12x12x24_twisted` (byte-verified by relocating the RELA pointers at `0x21f57380`). The resilient *base* set the §4.3 codename singletons layer on is this 12-shape `ResilientToroidalTopology::kRouteCacheSet`, **not** the 8-shape twisted set tabulated below.)

| idx | str VMA | len | string | dims |
|-----|---------|-----|--------|------|
| 0 | `0x87022ba` | 16 | `12x12x24_twisted` | `[12,12,24]` |
| 1 | `0x87022a9` | 16 | `12x24x24_twisted` | `[12,24,24]` |
| 2 | `0x87022cb` | 16 | `16x16x32_twisted` | `[16,16,32]` |
| 3 | `0x870227c` | 13 | `4x4x8_twisted` | `[4,4,8]` |
| 4 | `0x870227e` | 11 | `4x8_twisted` | `[4,8]` |
| 5 | `0x870226e` | 13 | `4x8x8_twisted` | `[4,8,8]` |
| 6 | `0x8702299` | 15 | `8x16x16_twisted` | `[8,16,16]` |
| 7 | `0x870228a` | 14 | `8x8x16_twisted` | `[8,8,16]` |

These eight shapes × the four `{X,Y,Z,NONE}` orientation variants register 32 dedup keys (canonicalized down to one entry per distinct canonical shape). Index 4 is the only 2-axis shape (`4x8_twisted`).

### 4.2 The no-arg singleton (slice-builder twisted torus)

`TwistedTorusTopology::InitRouteSolution` @ `0x20b3f7c0` uses the no-arg `GetCacheDeduplicator()` (anon-ns singleton, guard @ `0x2258fc28`). Its lazy `Create()` lambda walks `kRouteCacheSet` (the loop at `0x20b3f990` indexes `&kRouteCacheSet[v15]` / `[v15+1]` as `{ptr,len}` pairs) and calls `UpdateDeduplicator` once. A miss on this path is fatal: **"Cannot find route cache: "** (`.rodata`, seen at the function's error builder). This is the single default deduplicator; it never sees a codename set.

### 4.3 The codename-keyed singletons (resilient path)

`ResilientToroidalTopology::InitRouteSolution` @ `0x1fbdf8a0` uses the int overload `GetCacheDeduplicator(int code)`. The decompiled `switch (code)` selects one of three anonymous-namespace singletons, and each lazily populates itself by layering its codename set **on top of** the base `kRouteCacheSet`. The set names are byte-confirmed from the `CHECK`/`MakeCheckFailString` literals:

| `code` | singleton | `UpdateDeduplicator` calls (in order) | `ToroidalRouteCacheType` |
|--------|-----------|---------------------------------------|--------------------------|
| `1` | `pf_deduplicator` (pufferfish) | `kRouteCacheSet` | `CACHE_ICI_RESILIENCY_PUFFERFISH` (2) |
| `2` | `vf_deduplicator` (viperfish) | `kRouteCacheSet`, then `kViperfishRouteCacheSet` | `CACHE_ICI_RESILIENCY_VIPERFISH` (3) |
| `4` | `gf_deduplicator` (`6acc60406` / TPU7x) | `kRouteCacheSet`, then `k6acc60406RouteCacheSet` | `CACHE_ICI_RESILIENCY_6acc60406` (4) |

The exact decompiled `CHECK` strings, verbatim:

```text
"UpdateDeduplicator( **pf_deduplicator, ResilientToroidalTopology::kRouteCacheSet) is OK"
"UpdateDeduplicator( **vf_deduplicator, ResilientToroidalTopology::kRouteCacheSet) is OK"
"UpdateDeduplicator( **vf_deduplicator, ResilientToroidalTopology::kViperfishRouteCacheSet) is OK"
"UpdateDeduplicator( **gf_deduplicator, ResilientToroidalTopology::kRouteCacheSet) is OK"
"UpdateDeduplicator( **gf_deduplicator, ResilientToroidalTopology::k6acc60406RouteCacheSet) is OK"
```

> **CORRECTION (DEDUP-1) — the codename sets are *additive*, not replacements.** Pufferfish loads only the base `kRouteCacheSet`. Viperfish and `6acc60406` each load the base set **and then** their codename-specific set — viperfish's via the `RouteCacheDeduplicator::CreateResilientViperfish()` `Create` lambda (its `__policy_func` is wired before the second `UpdateDeduplicator`), `6acc60406`'s via `k6acc60406RouteCacheSet`. So a viperfish slice can hit *either* a base twisted shape *or* a viperfish-only shape. Earlier reconstruction folded `6acc60406` into the base set or treated the three as mutually exclusive; the distinct symbol names, the two-call-per-singleton structure, and the verbatim `CHECK` literals confirm three sets with the base set common to all. Marked HIGH.

The `code` value comes from the fault topology (traced to `TopologyFaults::GetSymmetryProperty` @ `0x20bfbd60` / `GetMinFaultLocation` @ `0x20bfbc00`). After the `Find`, a *second* `switch` in the same function (cases `1 → 4 → 2` at the post-Find branch) re-derives the `ToroidalRouteCacheType` passed to `GetRouteCacheData` (the per-codename baked blob). The structural binding (`pf → PUFFERFISH`, `vf → VIPERFISH`, `gf → 6acc60406`) is byte-confirmed via the dedup singletons; the precise `code → enum` integer arithmetic of that second switch was read structurally, not exhaustively tabulated. Marked LOW.

> **NOTE — `6acc60406` is a redacted silicon codename, not a hex address.** It appears verbatim as a string token in the enum name (`CACHE_ICI_RESILIENCY_6acc60406`), the set symbol (`k6acc60406RouteCacheSet`), and the resource-path prefix. It denotes the TPU7x silicon generation; treat it as an opaque identifier.

---

## 5. End-to-end placement (one diagram)

```text
[slice-builder, non-resilient] TwistedTorusTopology::InitRouteSolution     # 0x20b3f7c0
  dedup = GetCacheDeduplicator()              # no-arg singleton; UpdateDeduplicator(kRouteCacheSet)
  res   = dedup.Find({this.dims, twisted, orient})                          # 0x20b59000
  if !res.ok: FATAL "Cannot find route cache: "
  blob  = ToroidalRouteCache::Create(res.canonical, CACHE_TWISTED_TORUS, res.orient_vec)
                                              # → route-cache-decompress.md

[slice-builder, resilient]     ResilientToroidalTopology::InitRouteSolution # 0x1fbdf8a0
  code  = f(TopologyFaults.GetSymmetryProperty / GetMinFaultLocation)
  dedup = GetCacheDeduplicator(code)          # switch: 1→pf / 2→vf / 4→gf singleton
          # pf: UpdateDeduplicator(kRouteCacheSet)
          # vf: UpdateDeduplicator(kRouteCacheSet); UpdateDeduplicator(kViperfishRouteCacheSet)
          # gf: UpdateDeduplicator(kRouteCacheSet); UpdateDeduplicator(k6acc60406RouteCacheSet)
  res   = dedup.Find(...)                      # 0x20b59000
  type  = {pf→PUFFERFISH, vf→VIPERFISH, gf→6acc60406}                       # CACHE_ICI_RESILIENCY_*
  blob  = ToroidalRouteCache::Create(res.canonical, type, res.orient_vec)
```

`Find`'s two outputs — the canonical `RouteCacheIdentifier` (names the blob file) and the `vector<Orientation>` (rotates the loaded maps) — are the entire interface this page exposes to the rest of the load path.

---

## 6. Function and symbol map

| Address | Symbol | Role | Confidence |
|---------|--------|------|------------|
| `0x20b59000` | `RouteCacheDeduplicator::Find` | hash-lookup; copy canonical key + rotation on hit | C |
| `0x20b58340` | `RouteCacheDeduplicator::Insert` | canonicalize (`ToFaultyDimensions`→`ToRouteCacheIdentifier`→`ToRotationMapper`) + `find_or_prepare_insert` | C |
| `0x1fbe3780` | `(anon)::UpdateDeduplicator` | per-string parse + 4-variant Insert fan-out | C |
| `0x20b5aa40` | `combine<vector<int>,bool,optional<Orientation>>` | key hash (CRC32; `is_twisted ^ 1`) | C |
| `0x20b480c0` | `slice_builder::ParseTopologyString` | `'x'`-split + `_twisted` suffix → `{dims, is_twisted}` | C |
| `0x20b57b40` | `RouteCacheDeduplicator::ToFaultyDimensions` | `vector<FaultyDimension>` (stride 12) | C |
| `0x20b57d80` | `RouteCacheDeduplicator::ToRouteCacheIdentifier` | canonical shape; "Only one faulty orientation…" `0xa244c87` | C |
| `0x20b58160` | `RouteCacheDeduplicator::ToRotationMapper` | canonical→queried Orientation list | C |
| `0x20b5b680` | `…raw_hash_set<…>::find_or_prepare_insert_large` | dedup map insert | C |
| `0x20b3f7c0` | `TwistedTorusTopology::InitRouteSolution` | no-arg dedup; `kRouteCacheSet` walk @ `0x20b3f990` | C |
| `0x1fbdf8a0` | `ResilientToroidalTopology::InitRouteSolution` | codename `switch`; pf/vf/gf singletons | C |
| `0x22011f88` | `TwistedTorusTopology::kRouteCacheSet` | 8 twisted-shape `{ptr,len}` pairs (non-resilient path) | C |
| `0x21f57380` | `ResilientToroidalTopology::kRouteCacheSet` | base set for the resilient path | C |
| `0x21f57440` | `ResilientToroidalTopology::kViperfishRouteCacheSet` | viperfish-only codename set | C |
| — | `k6acc60406RouteCacheSet` | TPU7x codename set (recovered from `CHECK` strings; not an independent `nm` symbol) | C |

---

## Cross-References

- [`toroidal-route-cache.md`](toroidal-route-cache.md) — the `ToroidalRouteCache` container the dedup result feeds: the four `FlatHashMap` layout, embedding in the topology, `Create` orchestration, and the runtime read-side dispatch.
- [`route-cache-decompress.md`](route-cache-decompress.md) — `Decompress` (`@0x20b63320`) + the `CacheRead` expansion loop that consumes the `vector<Orientation>` rotation this page's `Find` returns; also the `ToroidalRouteCacheType` *file/codename* enum vs the `[obj+0]` *layout* discriminant.
- [`route-cache-codec.md`](route-cache-codec.md) — `DecodePathFromBits` and the `TopologyRotationHelper` `RotateId`/`RotateCoordinates`/`RotateOrientation` per-axis permutation math that turns the dedup rotation into remapped chip ids.
- [`overview.md`](overview.md) — the ICI routing section map: where the precomputed `ToroidalRouteCache` lookup sits relative to the live `*ToroidalWildFirstPaths` generator and the `InitRouteSolution` cache-vs-generate decision (it cites the three dedup sets).
- [back to index](../index.md)
