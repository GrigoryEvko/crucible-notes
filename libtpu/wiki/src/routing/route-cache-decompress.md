# CompressedToroidalRouteCache::Decompress — Proto-to-Map Expansion

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64` wheel (build `libtpu_lts_20260413_b_RC00`, build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary ships with full C++ symbols (`.text` VMA == file offset, base `0xe63c000`); every address below is a VMA. Demangled names and field offsets are cross-checked against the IDA decompile. Other versions will differ.*

## Abstract

`Decompress` is the **first stage of the precomputed route-cache load path**: it takes a `proto::CompressedToroidalRouteCache` — the Brotli-compressed wrapper that `tsl::ReadBinaryProto` reads off an `embed://…/<shape>.binarypb.compressed` baked resource — and produces the inner `proto::ToroidalRouteCache` message. It is a thin riegeli decode: validate `format == 2`, wrap the `data` field (an `absl::Cord`) in a `riegeli::BrotliReader`, and `ParseMessage` the decompressed bytes into the inner proto. The decompressed proto is *not yet* the in-memory lookup structure; it is a list of per-`(src,dst)` `RouteScheme` records.

The page then documents the two stages that turn that proto into the in-memory `(src,dst) → path` map. The **`ToroidalRouteCache` constructor** (`@0x20b5d8e0`) sets the object's in-memory **layout discriminant** `[obj+0]` from the *first* `RouteScheme`'s `route` oneof case — distance vs static-path vs bit-encoded vs random-hop — which selects *which* of four `FlatHashMap` slots holds the per-pair value. **`CacheRead`** (`@0x20b5da20`) is the **per-entry expansion loop**: it builds a `TopologyRotationHelper` from the dedup orientation vector, then for every `RouteScheme` rotates `(src_chip_id, dest_chip_id)` into this topology's chip-id space, decodes the path body according to the layout discriminant, and inserts the `(rotated_src, rotated_dst) → value` pair into the matching map.

The dedup orientation vector that `CacheRead` consumes is produced by [`route-cache-dedup.md`](route-cache-dedup.md); the bit-stream body of the type-2 (`bit_encoded_path`) variant is unpacked by `DecodePathFromBits`, documented on [`route-cache-codec.md`](route-cache-codec.md); the container object, its embedding in the topology, and the runtime read-side dispatch live on [`toroidal-route-cache.md`](toroidal-route-cache.md). This page owns the **`Decompress` body**, the **`ToroidalRouteCacheType` proto enum** (the *file* selector, distinct from the layout discriminant), and the **`CacheRead` expansion loop**.

For reimplementation, the contract is:

- **`Decompress`** — the `format == 2` gate, the `absl::Cord` → `riegeli::BrotliReader` setup, and the `ParseMessage` into `proto::ToroidalRouteCache`.
- **The two discriminants** — `proto::ToroidalRouteCacheType` (the dense `[0,4]` *file/codename* enum that picks which baked blob loads) versus the in-memory `[obj+0]` *layout* int `{0,1,2,3}` (which oneof the schemes carry, hence which map).
- **The expansion loop** — for each `RouteScheme`: `RotateId` the two chip ids, decode the path per layout type (`RotateCoordinates` / `RotateOrientation` per hop / `DecodePathFromBits`), `find_or_prepare_insert` into the per-type map, and reject duplicate `(src,dst)` keys.

| | |
|---|---|
| **Decompress** | `slice_builder::Decompress(const proto::CompressedToroidalRouteCache&)` @ `0x20b63320` |
| **Constructor (sets `[obj+0]`)** | `ToroidalRouteCache::ToroidalRouteCache(const proto::ToroidalRouteCache&, const vector<proto::Orientation>&)` @ `0x20b5d8e0` |
| **Expansion loop** | `ToroidalRouteCache::CacheRead(const proto::ToroidalRouteCache&, const vector<proto::Orientation>&)` @ `0x20b5da20` |
| **File-selector enum** | `proto::ToroidalRouteCacheType`, dense span `[0,4]`, descriptor `@0x20c06b00` |
| **Brotli reader vtable** | `off_22015FE0` (`riegeli::BrotliReader`) over `off_22015E78` (`riegeli::CordReader`) |
| **Parse entry** | `riegeli::parse_message_internal::ParseMessageImpl` @ `0x20be5d60` |
| **Path codec (type 2)** | `DecodePathFromBits` @ `0x20b5c5a0` → [`route-cache-codec.md`](route-cache-codec.md) |
| **Source** | `platforms/accel_ssw/deepsea/slice_builder/internal/compressed_toroidal_route_cache.cc`; `…/toroidal_route_cache.cc` |

Confidence is **Confirmed (byte-anchored)** for the `Decompress` body, the `[obj+0]` discriminant arithmetic, the four `CacheRead` map branches and their `find_or_prepare_insert` policy types, and the `ToroidalRouteCacheType` enum names — each cross-checked against the IDA decompile and `.rodata` strings. The single **LOW**-confidence item is called out inline (the proto wire-tag for `random_first_hop`).

---

## Where Decompress sits in the load path

`Decompress` is one step of `ToroidalRouteCache::Create` (`@0x20b5d6e0`), the call that materialises a precomputed cache. The chain is:

```text
ToroidalRouteCache::Create (0x20b5d6e0)
  ├─ GetRouteCacheData (0x20b5c420)
  │    ├─ GetRouteCacheDataPath (0x20bf2080)      ── "embed://<lower(Type)>_data/<shape>.binarypb.compressed"
  │    ├─ tsl::ReadBinaryProto (0x20d02060)        ── read CompressedToroidalRouteCache off the baked resource
  │    └─ Decompress (0x20b63320)                  ── *** THIS PAGE: Brotli → proto::ToroidalRouteCache ***
  ├─ ToroidalRouteCache::ctor (0x20b5d8e0)         ── set [obj+0] = layout type from 1st RouteScheme oneof
  │    └─ CacheRead (0x20b5da20)                   ── *** THIS PAGE: proto → 4 FlatHashMaps, rotated ***
  └─ move the 4 maps into the StatusOr<ToroidalRouteCache> result
```

`GetRouteCacheDataPath` is where the **first** of the two type discriminants is consumed: it lowercases the `ToroidalRouteCacheType` enum name and `StrFormat`s it into the resource path. `Decompress`/`CacheRead` are where the inner proto is expanded. The container and its `Create` orchestration are detailed on [`toroidal-route-cache.md`](toroidal-route-cache.md); this page picks up at the `Decompress` boundary.

---

## Decompress @0x20b63320 — Brotli, not zstd

`Decompress` consumes a `proto::CompressedToroidalRouteCache`, whose schema is just two fields:

| field | tag | type | in-memory offset | role |
|-------|-----|------|------------------|------|
| `format` | 1 | `int32` | `[proto+0x28]` | compression discriminant; **must be `2`** |
| `data` | 15 | `bytes` (`absl::Cord`) | `[proto+0x18]` | the Brotli-compressed `ToroidalRouteCache` bytes |

The decode is a fixed riegeli pipeline. The `format` field at `[proto+0x28]` is read as a dword and compared against `2`; any other value short-circuits to an error.

```c
// 0x20b63320 — Decompress(this <- StatusOr storage, const CompressedToroidalRouteCache* cc)
StatusOr<proto::ToroidalRouteCache> Decompress(const CompressedToroidalRouteCache* cc) {
    if (*((int32_t*)cc + 10) != 2)                          // [cc+0x28] == format; line 0x20b63335
        return MakeError(InvalidArgument,                   // status code 13
                         StrFormat("Unsupported format: %d", cc->format));

    // cc->data is an absl::Cord at [cc+0x18]; ref-bump it into a local Cord (v26/v27),
    // then construct a BrotliReader<CordReader<Cord>> on the heap (operator new(0x158)).
    void*  reader = operator new(0x158);
    *(void**)reader          = &off_22015FE0;               // riegeli::BrotliReader vtable
    *(void**)(reader + 112)  = &off_22015E78;               // riegeli::CordReader  vtable
    // copy the borrowed Cord into the reader's owned slot [reader+0x148]
    riegeli::CordReaderBase::Initialize(reader + 112, /*Cord*/ reader + 328);   // 0x1db11ec0
    riegeli::BrotliReaderBase::Initialize(reader, /*src=*/ reader + 112);       // 0x20b64ee0

    proto::ToroidalRouteCache out;                          // ctor(&out, 0)
    // default_recursion_limit_ packed into the high word; v16 = vtable[+0x60](reader, 1)
    int64_t st = riegeli::parse_message_internal::ParseMessageImpl(reader, &out, ...);  // 0x20be5d60
    if (st == 1) reader->vtable[+0x58]();                   // VerifyEndAndClose on success
    if (!riegeli::Object::Close(reader)) { ... fold reader status into st ... }
    reader->vtable[+0x8]();                                 // ~reader / dtor

    if (st != 1)                                            // parse failed
        return CreateStatusAndConditionallyLog(             // tagged compressed_toroidal_route_cache.cc:52
                   StatusBuilder::InitRepImpl(st));
    return out;                                             // move/CopyFrom into the StatusOr payload
}
```

Key facts, all byte-anchored:

- **`format == 2` is the only accepted value.** The compare is at `0x20b63335`; the failure path runs `FormatPack("Unsupported format: %d", 22)` and `MakeErrorImpl<13>` (`InvalidArgument`), tagged at `compressed_toroidal_route_cache.cc:40`. There is no `case 1`/zstd branch — the on-disk `.compressed` resource is always a Brotli stream.
- **The reader is a `riegeli::BrotliReader` layered over a `riegeli::CordReader`.** The two vtables are written at object offsets `+0` (`off_22015FE0`) and `+112` (`off_22015E78`); `CordReaderBase::Initialize` (`@0x1db11ec0`) and `BrotliReaderBase::Initialize` (`@0x20b64ee0`) wire the chain. The `data` `Cord` is borrowed via an `_InterlockedAdd(refcount, 2)` ref-bump and copied into the reader's owned Cord slot at `[reader+0x148]`.
- **The inner message is parsed by riegeli's `ParseMessageImpl`** (`@0x20be5d60`), with the standard `proto2::io::CodedInputStream::default_recursion_limit_`. On success (`st == 1`) the parsed `proto::ToroidalRouteCache` is `CopyFrom`/`InternalSwap`-ed into the `StatusOr` payload (the `Copy`/`Swap` choice at the tail depends on whether the two arenas match — `0x20c040a0`/`…e0`).
- **Failure is structured, not fatal.** A parse or reader error returns a non-OK `Status` (tagged `compressed_toroidal_route_cache.cc:52`); the caller (`GetRouteCacheData`) propagates it. (Contrast the *constructor* below, which `CHECK`-fails on a malformed proto — the two stages have different failure disciplines.)

> **NOTE — the riegeli format byte vs the on-disk magic.** `format == 2` is the `CompressedToroidalRouteCache.format` *proto field*, not a Brotli/riegeli stream magic byte. It is a producer-side contract: whoever bakes the `.binarypb.compressed` resource writes `format = 2` and a Brotli `data` blob. A future format `1`/`3` would land in the `Unsupported format` error until a decode branch is added.

---

## The two type discriminants (do not conflate)

Two distinct integers both read as "type" in this subsystem. They live in different places and drive different decisions.

### 1. `proto::ToroidalRouteCacheType` — the file/codename selector

A dense protobuf enum, span `[0,4]`, descriptor `@0x20c06b00` (table `@0x224ab9c0`). The five value names are present verbatim in `.rodata`:

| value | enum name | role |
|-------|-----------|------|
| 0 | `CACHE_UNKNOWN_TYPE` | sentinel / unset |
| 1 | `CACHE_TWISTED_TORUS` | the non-resilient slice-builder twisted torus |
| 2 | `CACHE_ICI_RESILIENCY_PUFFERFISH` | resilient — pufferfish silicon generation |
| 3 | `CACHE_ICI_RESILIENCY_VIPERFISH` | resilient — viperfish silicon generation |
| 4 | `CACHE_ICI_RESILIENCY_6acc60406` | resilient — `6acc60406` (ghostfish) silicon generation |

This enum is the input to `GetRouteCacheDataPath` (`@0x20bf2080`), **not** to `CacheRead`. The decompile shows the dense-enum lookup `NameOfDenseEnum<&ToroidalRouteCacheType_descriptor, 0, 4>` bounded by `type <= 4`, then `AsciiStrToLower`, then `FormatPack` into the resource template:

```text
"embed://%s_data/%s.binarypb.compressed"          (the path template; AsciiStrToLower @0x20bf2080)
   │            │
   │            └─ StrCat( JoinAlgorithm(dims, "x"), is_twisted ? "_twisted" : "",
   │                       orientation ? "_" + NameOfDenseEnum<Orientation,0,6>(orient) : "" )
   └─ lower(NameOfDenseEnum<ToroidalRouteCacheType,0,4>(type))   e.g. "cache_twisted_torus"

  ⇒ "embed://cache_twisted_torus_data/12x12x24_twisted.binarypb.compressed"
```

So `ToroidalRouteCacheType` selects **which baked blob loads** — one per codename family. The choice of codename (and hence which dedup singleton and which enum value) is made upstream in `InitRouteSolution`; see [`route-cache-dedup.md`](route-cache-dedup.md). The `Orientation` enum used for the optional path suffix is itself a dense `[0,6]` enum (`{UNKNOWN=0, X=1, Y=2, Z=3, A=4, B=5, C=6}`).

### 2. `[obj+0]` — the in-memory layout discriminant

A plain `int` at the head of the `ToroidalRouteCache` object, value `{0,1,2,3}`, set by the constructor from the *first* `RouteScheme`'s `route` oneof case. It selects **which of four `FlatHashMap` slots** holds the per-pair value. It is independent of `ToroidalRouteCacheType`: a `CACHE_TWISTED_TORUS` blob and a `CACHE_ICI_RESILIENCY_VIPERFISH` blob can both carry `bit_encoded_path` schemes and both end up `[obj+0]==2`.

> **NOTE.** A *third* unrelated "type" int exists — the twist-shape discriminant `[topo+0xe8]` `{1,2,3}` on the topology object (`k·k·2k` / `k·2k·2k` / `k·2k·nk`). It is documented with the twisted-torus geometry and is not consumed here.

---

## The constructor @0x20b5d8e0 — setting `[obj+0]`

The constructor zero-initialises the four map slots (`[obj+0x8]`, `[obj+0x28]`, `[obj+0x48]`, `[obj+0x68]`), computes `[obj+0]`, then calls `CacheRead`. The layout int is derived **only from the first scheme's oneof tag**, except that *any* scheme carrying random-first-hops escalates a `static_path` cache to the random-hop layout.

```c
// 0x20b5d8e0 — ToroidalRouteCache(this, const proto::ToroidalRouteCache* cache_pb, const vector<Orientation>* orients)
void ToroidalRouteCache::ToroidalRouteCache(int* obj, const proto::ToroidalRouteCache* cache_pb, ...) {
    obj[0x8>>2] = obj[0x28>>2] = obj[0x48>>2] = obj[0x68>>2] = 0;   // 4 empty FlatHashMaps

    CHECK(cache_pb->routing_schemes_size() != 0);    // hard CHECK; toroidal_route_cache.cc:128
                                                     // ("cache_pb.routing_schemes_size() != 0")

    auto& scheme0 = cache_pb->routing_schemes(0);    // [cache_pb+0x18] repeated field, element 0
    int oneof = scheme0._oneof_case;                 // [scheme0+0x38]

    int type;
    if      (oneof == 3) type = 0;                   // distance        → distance map  [obj+0x8]
    else if (oneof == 5) type = 2;                   // bit_encoded_path→ vec<Dir> map  [obj+0x28]
    else {                                           // static_path (4) and everything else
        type = 1;                                    //                 → vec<Dir> map  [obj+0x28]
        for (auto& s : cache_pb->routing_schemes)    // any scheme with random-first-hops?
            if (s.random_first_hop_size() > 0) {     // [s+0x20] > 0
                type = 3;                            //                 → random-hop maps [obj+0x48]/[+0x68]
                break;
            }
    }
    obj[0] = type;                                   // *** the layout discriminant ***  0x20b5d97b

    CHECK_OK( this->CacheRead(cache_pb, orients) );  // toroidal_route_cache.cc:152
}
```

| `oneof` (`[scheme0+0x38]`) | extra condition | `[obj+0]` | layout |
|---------------------------|-----------------|-----------|--------|
| `3` (distance) | — | `0` | distance vector |
| `5` (bit_encoded_path) | — | `2` | bit-packed Direction path |
| `4` (static_path) | no scheme has random_first_hop | `1` | single Direction path |
| `4` (static_path) | **some** scheme has `random_first_hop > 0` | `3` | random-hop path set |

Two CHECKs make this constructor non-recoverable on a malformed proto: an empty `routing_schemes` (`toroidal_route_cache.cc:128`) and a non-OK `CacheRead` result (`:152`). Since the proto comes from a *baked, trusted* resource, a failure here is a build/packaging bug, hence the abort-class `LogMessageFatal` rather than a propagated `Status`. The decompile also guards the `routing_schemes(0)` access with `LogIndexOutOfBoundsAndAbort` (standard generated-proto bounds check).

> **NOTE — first-scheme homogeneity.** The discriminant is read from scheme `0` only (plus the global random-hop scan). The cache producer is therefore required to emit a *homogeneous* `route` oneof across all schemes in one blob — a mixed distance/static_path file would be mis-typed. `CacheRead`'s per-scheme `RetCheck`s (below) defend against exactly that, turning a heterogeneous blob into a non-OK status rather than a silent mis-insert.

---

## CacheRead @0x20b5da20 — the per-entry expansion loop

`CacheRead` is the workhorse: it turns the parsed proto into the four in-memory maps. It is large because the four layout branches are fully inlined, each with its own `raw_hash_set` `find_or_prepare_insert` and value-construction code, but the structure is uniform.

### Prologue — build the rotation helper

```c
// 0x20b5da20 — CacheRead(this, const proto::ToroidalRouteCache* cache_pb, const vector<Orientation>* orients)
topo = slice_builder::Topology( cache_pb->topology() );        // field 1 → Topology ctor @0x20bf3820
rot  = TopologyRotationHelper::Create(topo, *orients);         // @0x20bf2380   (orients = dedup payoff)
if (!rot.ok()) return rot.status();                            // toroidal_route_cache.cc:161
int type = this->obj[0];                                       // the layout discriminant
```

`orients` is the `vector<proto::Orientation>` the dedup `Find` returned (see [`route-cache-dedup.md`](route-cache-dedup.md)). The cached blob is written in a single **canonical** orientation; the helper `rot` is the remap from that canonical chip-id/coordinate space onto *this* topology's axes. Every key and every coordinate that follows is pushed through `rot` — that is what lets one baked blob serve all X/Y/Z rotations of a shape. The per-axis permutation math of `RotateId`/`RotateCoordinates`/`RotateOrientation` itself belongs to [`route-cache-codec.md`](route-cache-codec.md); here we only note *where* it is applied.

### The per-scheme loop

For each `RouteScheme`, the same skeleton runs:

```c
for (auto& scheme : cache_pb->routing_schemes) {               // field 3, repeated
    int skey = rot.RotateId(scheme.src_chip_id);               // [scheme+0x28]  @0x20bf3020
    int dkey = rot.RotateId(scheme.dest_chip_id);              // [scheme+0x2c]  @0x20bf3020
    pair<int,int> key{skey, dkey};
    // ... decode value per `type`, then find_or_prepare_insert(key, value) ...
}
```

`RotateId` is `topo.GetCoordinate(id) → RotateCoordinates → topo.GetId(coord)` (the `GetCoordinate`/`GetId` are virtual, `vtable+0x88`/`+0x90`). Both chip ids are remapped *before* they form the map key, so the in-memory map is keyed by *this topology's* chip-id pairs, not the canonical blob's.

### The four value-decode branches

The dispatch is `if (type == 3) … else if (type == 2) … else if (type != 0 /*type 1*/) … else /*type 0*/`. Each branch ends in a `find_or_prepare_insert` against a different `FlatHashMapPolicy`, and each is guarded by a `RetCheck` that the scheme actually carries the expected oneof:

| `type` | proto oneof per scheme | value decode | target map | `find_or_prepare` policy value type |
|--------|------------------------|--------------|------------|-------------------------------------|
| `0` | `distance` (3) — guarded by `RetCheck(has_distance())` `:166` | `RotateCoordinates(scheme.distance)` `@0x20b5da20:3039` | `[obj+0x8]` | `superpod::routing::Coordinates` |
| `1` | `static_path` (4) — guarded by `RetCheck(has_static_path())` `:189` | rotate each `static_path.hops[i]` via `RotateOrientation` | `[obj+0x28]` | `vector<proto::Direction>` |
| `2` | `bit_encoded_path` (5) — guarded by `RetCheck(has_bit_encoded_path())` `:216` | `DecodePathFromBits(scheme.bit_encoded_path)` `@0x20b5c5a0` | `[obj+0x28]` | `vector<proto::Direction>` |
| `3` | `random_first_hop` (repeated) | one `vector<Direction>` per `random_first_hop[i].hop`; `probability` → `int8` weights | `[obj+0x48]` (paths) + `[obj+0x68]` (weights) | `vector<vector<proto::Direction>>` + `vector<int8_t>` |

Notes per branch, byte-anchored:

- **Type 0 (distance).** The `superpod::routing::Coordinates` value is constructed from the scheme's `distance` `ChipCoordinate` (`Coordinates::Coordinates` `@…:3038`), pushed through `RotateCoordinates` (`@…:3039`), then inserted into `FlatHashMapPolicy<pair<int,int>, superpod::routing::Coordinates>` (`find_or_prepare_insert_large` `@…:3047`). The distance value is a **`superpod::routing::Coordinates`**, the signed per-axis distance vector — *not* a `slice_builder::Coordinates`. This is the map the runtime `GetRouteDistance` reads.
- **Type 1 (static_path).** Each `Direction` in `scheme.static_path.hops` (a `RepeatedPtrField`; the `StaticPath` default instance is `…_StaticPath_globals_`) is copied and `RotateOrientation`-remapped, accumulating a `vector<proto::Direction>` that is inserted into the `vec<Direction>` map at `[obj+0x28]`. The `RetCheck(has_static_path())` is at `toroidal_route_cache.cc:189`.
- **Type 2 (bit_encoded).** The single `scheme.bit_encoded_path` `bytes` field is handed to `DecodePathFromBits` (`@0x20b5c5a0`), which returns a `vector<proto::Direction>` inserted into the *same* `[obj+0x28]` map as type 1 — type 1 and 2 are two encodings of the identical in-memory value. The bit-stream layout (`BitDecoder::GetVarInt` header, `orientation`(2b)/`polarity`(1b) per Direction) is owned by [`route-cache-codec.md`](route-cache-codec.md). `RetCheck(has_bit_encoded_path())` at `:216`.
- **Type 3 (random-hop).** The bulkiest branch: for each `random_first_hop[i]`, a `Direction` (`hop`, `RotateOrientation`-remapped) becomes a one-element `vector<Direction>`, and the set of them becomes a `vector<vector<Direction>>` inserted at `[obj+0x48]` (`FlatHashMapPolicy<pair<int,int>, vector<vector<Direction>>>`). The per-hop `probability` (`int32` in the proto) is narrowed to `int8` and accumulated into a parallel `vector<int8_t>` inserted at `[obj+0x68]`. Both maps are keyed by the same rotated `(src,dst)` pair, so the path set and its weight vector stay aligned.

### Duplicate-key rejection

Every branch checks the `find_or_prepare_insert` result and **rejects a second scheme with the same rotated `(src,dst)` key**:

```c
// e.g. type 3, around 0x20b5da20:+offset (the "$_0" lambda inline)
if (!inserted_fresh) {
    return MakeError(/*code 3 = InvalidArgument*/,
        StrCat("Duplicate insertion associated with source: ", skey,
               " and destination: ", dkey));                  // toroidal_route_cache.cc:322
}
```

The `StrCat` joins the literal `"Duplicate insertion associated with source: "`, the source chip id, `" and destination: "`, and the destination chip id (via `FastIntToBuffer`). A duplicate is therefore a *hard, surfaced* error: the cache contract is exactly one `RouteScheme` per `(src,dst)` pair *after rotation* — collisions (a producer bug, or two pre-rotation keys that alias post-rotation) abort the load with a precise diagnostic rather than silently overwriting.

### Result

On the OK path `CacheRead` returns `Status::OK` (`v15 == 1`) and the four `FlatHashMap` slots on the object are populated; `Create` then move-constructs them into the `StatusOr<ToroidalRouteCache>` payload (the per-map C2 movers `@0x1fbe41e0`/`4220`/`4260`/`42a0`). Any `RetCheck` mismatch or duplicate produces a non-OK `Status` that the constructor's `CHECK_OK` (`:152`) then turns fatal.

---

## End-to-end (one diagram)

```text
GetRouteCacheData(dims, twisted, orient, ToroidalRouteCacheType)            # 0x20b5c420
  path = "embed://" + lower(NameOf(type)) + "_data/" + <shape> + ".binarypb.compressed"
  ReadBinaryProto(path) → CompressedToroidalRouteCache{ format, data:Cord }  # 0x20d02060
  └─ Decompress: format==2 ? BrotliReader(data) → ParseMessage              # 0x20b63320  [THIS PAGE]
                 → proto::ToroidalRouteCache{ topology, routing_schemes[] }
ctor(proto, dedup_orientations)                                            # 0x20b5d8e0  [THIS PAGE]
  [obj+0] = {distance→0, static_path→1, bit_encoded→2, +random_hop→3}
  CacheRead(proto, dedup_orientations):                                    # 0x20b5da20  [THIS PAGE]
     rot = TopologyRotationHelper(topology, dedup_orientations)            # canonical→actual remap
     for scheme in routing_schemes:
        key = ( rot.RotateId(src_chip_id), rot.RotateId(dest_chip_id) )
        switch [obj+0]:
          0: map@+0x8 [key] = rot.RotateCoordinates(distance)              # superpod::routing::Coordinates
          1: map@+0x28[key] = [rot.RotateOrientation(h) for h in static_path.hops]   # vector<Direction>
          2: map@+0x28[key] = DecodePathFromBits(bit_encoded_path)         # vector<Direction>  → codec page
          3: map@+0x48[key] = [[hop] for hop in random_first_hop];         # vector<vector<Direction>>
             map@+0x68[key] = [int8(p.probability) for p in random_first_hop]
        on collision: error "Duplicate insertion associated with source: …"
Create: move the 4 maps into StatusOr<ToroidalRouteCache>                  # 0x20b5d6e0
```

---

## Reimplementation checklist

- [ ] **`Decompress`**: reject `format != 2` with `InvalidArgument("Unsupported format: %d")`; wrap the `data` `Cord` in `riegeli::BrotliReader<CordReader<Cord>>`; `ParseMessage` into `proto::ToroidalRouteCache`; propagate (do not abort on) parse failure.
- [ ] **Constructor**: `CHECK` non-empty `routing_schemes`; set `[obj+0]` from `routing_schemes(0)._oneof_case` (`3→0, 5→2, 4→1`), escalating `1→3` if *any* scheme has `random_first_hop_size() > 0`.
- [ ] **`CacheRead`**: build the `TopologyRotationHelper` from the dedup orientation vector; per scheme, `RotateId` both chip ids into the key; decode per layout type with `RetCheck` on the matching `has_*()`; insert into the per-type map; reject duplicate keys with the exact `"Duplicate insertion associated with source: …"` message.
- [ ] Keep the four value types byte-exact: distance → `superpod::routing::Coordinates`; static/bit-encoded → `vector<proto::Direction>` (same map); random-hop → `vector<vector<proto::Direction>>` plus a parallel `vector<int8_t>` weight map.

> **LOW confidence.** The `random_first_hop` field tag is reported as wire tag 7 in the upstream descriptor decode; this page anchors only the in-memory `[scheme+0x20]` count read used by the constructor's escalation and the per-element `hop`/`probability` access — the exact proto field *number* (7) was not re-derived from the `EnumDescriptorProto` bytes here. Treat the `(7)` annotation in the diagrams as descriptor-sourced, not independently re-confirmed on this page.

---

## Cross-References

- [`route-cache-dedup.md`](route-cache-dedup.md) — the `RouteCacheDeduplicator`, the `RouteCacheIdentifier` key, and the `vector<proto::Orientation>` rotation vector that `CacheRead` consumes; also the no-arg vs codename-keyed `GetCacheDeduplicator` overload that picks the `ToroidalRouteCacheType`.
- [`route-cache-codec.md`](route-cache-codec.md) — `DecodePathFromBits` (the type-2 bit-stream unpack) and the `TopologyRotationHelper` `RotateId`/`RotateCoordinates`/`RotateOrientation` permutation math applied throughout `CacheRead`.
- [`toroidal-route-cache.md`](toroidal-route-cache.md) — the `ToroidalRouteCache` container object, its four-`FlatHashMap` layout and embedding in the topology, `Create`'s orchestration, and the runtime read-side dispatch (`CacheHit`/`GetRouteDistance`/`GetRoutePath`) on `[obj+0]`.
- [`overview.md`](overview.md) — the ICI routing section map: where the precomputed `ToroidalRouteCache` sits relative to the live `*ToroidalWildFirstPaths` generator and the `InitRouteSolution` cache-vs-generate decision.
- [`get-static-path.md`](get-static-path.md) — the packed `(hop_count<<6 | polarity<<3 | orientation)` `DirectionHops` encoding shared by the cache's `vector<proto::Direction>` entries and the live path generator.
