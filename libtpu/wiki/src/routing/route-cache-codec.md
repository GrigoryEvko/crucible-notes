# Route-Cache Bit Codec

> *Addresses apply to `libtpu.so` from the libtpu-0.0.40-cp314 wheel. Other versions differ.*
> **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`, build `libtpu_lts_20260413_b_RC00`; `.text` VMA == file offset). Demangled names and addresses are cross-checked against the IDA decompile (`ToroidalRouteCache::DecodePathFromBits`, `BitDecoder::{GetVarInt,GetGamma,GetBits64NoInline}`, `TopologyRotationHelper::{Create,RotateId,RotateCoordinates}`, `Direction::OrientationToDimension`).

## Abstract

This page documents the **bit codec** for a single cached route path: how a hop sequence is packed into the `bit_encoded_path` (type-2) cache value and unpacked back, the **primitive bit-reader/writer toolkit** that codec rides on (`gloop` `bitcoding.cc` / `coder.cc`), and the **axis-permutation math** (`TopologyRotationHelper`) that canonicalizes a path's coordinate frame before encode and after decode. It is one of four pages on the toroidal route cache; the [Decompress driver](route-cache-decompress.md) inflates the on-disk blob, the [container](toroidal-route-cache.md) holds the message hierarchy and walks every scheme, the [deduplicator](route-cache-dedup.md) supplies the cache key and the per-shape orientation list. **This page owns the inner codec**: `DecodePathFromBits`, the inferred `BitEncoder` path writer, the four `BitDecoder` primitives (`GetVarInt`/`GetGamma`/`GetBits64`/`SkipBits`) plus their `BitEncoder` duals, and the `TopologyRotationHelper` rotation.

The codec is small but exact. The cache must serve every X/Y/Z axis-orientation of a torus shape from **one** baked blob written in a fixed canonical axis order. So the decode path is two stages: (1) `DecodePathFromBits` reconstructs a `vector<proto::Direction>` from the bit stream in the *canonical* frame; (2) `TopologyRotationHelper` relabels the chip ids and coordinate frames so the canonical path lands on the actual physical axes. The encode path (offline generator, not shipped in `libtpu.so`) is the exact inverse — its byte format is recovered from the shipped decoder and round-tripped against the shared `gloop` writer toolkit.

For reimplementation, the contract is:

- **Decode** (`DecodePathFromBits` @`0x20b5c5a0`): a self-delimiting `GetVarInt(chunk=4)` hop-count header, then per hop a 2-bit orientation field (`{0,1,2}` → X/Y/Z) plus 1 polarity bit (`{0,1}` → POSITIVE/NEGATIVE), with field `3` a *repeat-previous-hop* escape that consumes no polarity bit. LSB-first 64-bit window over `BitEncoder::mask_[k]=(1<<k)-1`; byte-aligned zero-pad validated at the tail (hard error on any non-zero or ≥1-byte slack).
- **Encode** (inferred `BitEncoder` packer): the byte-exact inverse — `PutVarInt(4, n)` then per hop `PutBits(2, orient-1)`+`PutBits(1, polarity-1)` or `PutBits(2, 3)` for a same-direction repeat, then a zero-pad `Flush()`. The min-bits escape policy is *inferred* from the decoder, not byte-traced (generator absent from the binary).
- **Primitives** (`gloop` `bitcoding.cc`): four `BitDecoder` readers and their `BitEncoder` writers over one LSB-first 64-bit window and one shared `mask_` table. The route cache uses **only** `GetVarInt`/`PutVarInt(chunk=4)` plus fixed 2-bit/1-bit `PutBits`; `GetGamma`/`GetBits64`/`SkipBits` belong to other consumers (the device-trace profiler) and are documented for completeness.
- **Rotation** (`TopologyRotationHelper`): a *pure axis permutation* `out[k] = in[orient[k]-1]` of coordinate vectors and a mixed-radix chip-id relabel — no sign flip, no offset.

| | |
|---|---|
| **Decode entry** | `ToroidalRouteCache::DecodePathFromBits(string_view)` @`0x20b5c5a0` (584 decompiled lines) |
| **Encode entry** | inferred `BitEncoder` path packer (offline; exact inverse, INFERRED-POLICY) |
| **Primitive readers** | `BitDecoder::{GetVarInt @0x20b5cfa0, GetGamma @0x21073160, GetBits64NoInline @0x21073760, SkipBitsNoInline @0x21073580}` |
| **Primitive writers** | inlined `BitEncoder::{PutBits, PutVarInt, PutGamma, Flush}`; witnessed by `BitEncoder::Initialize` @`0x21072d40` |
| **Byte buffer** | `gloop` `Encoder` (coder.cc); dtor @`0x21073980` (`CHECK buf_ <= limit_`) |
| **Mask table** | `BitEncoder::mask_` @`0xbe79440` (.rodata, 65 qwords, `mask_[k]=(1<<k)-1`) |
| **Rotation** | `TopologyRotationHelper::{Create @0x20bf2380, RotateId @0x20bf3020, RotateCoordinates @0x20bf2f00}` |
| **Source files** | `gloop/util/coding/{coder.cc, bitcoding.cc}`; `slice_builder/internal/{toroidal_route_cache.cc, topology_rotation_helper.cc}`; `slice_builder/friends/direction.cc` |

---

## 1. Where the codec sits

The codec is the leaf of the cache decode. The [Decompress driver](route-cache-decompress.md) turns a `CompressedToroidalRouteCache` blob into a `ToroidalRouteCache` proto; the [container](toroidal-route-cache.md) walks each `RouteScheme` and dispatches on its variant tag. The `bit_encoded_path` (type-2) variant carries the most-compact form of a route — a length-prefixed bit stream — and is the one this codec unpacks. The other variants (`distance`, `static_path`, `random_hop`) are handled in the container; this page is **only** the bit stream and the rotation that wraps it.

```text
CompressedToroidalRouteCache  (on-disk, embedded blob)
        │  Decompress(...)               → route-cache-decompress.md
        ▼
ToroidalRouteCache proto  +  per-shape vector<proto::Orientation> (the dedup VALUE → route-cache-dedup.md)
        │  CacheRead(proto, orients)     → toroidal-route-cache.md  (@0x20b5da20)
        │     helper = TopologyRotationHelper::Create(topo, orients)        ── §4
        │     for scheme in proto.routing_schemes:
        │        skey = helper.RotateId(scheme.src_chip_id)                  ── §4.2
        │        dkey = helper.RotateId(scheme.dest_chip_id)
        │        switch scheme.variant:
        │           2 bit_encoded:  hops = DecodePathFromBits(bytes)         ── §2  (THIS PAGE)
        ▼
vector<proto::Direction>  (canonical frame; rotated keys index the actual frame)
```

> **NOTE —** the codec produces hops in the **canonical** axis frame; the *coordinates/ids* are moved to the actual frame by `RotateId`/`RotateCoordinates` (§4). A reimplementer must keep these two halves distinct: `DecodePathFromBits` never touches the rotation, and the rotation never touches the bit stream.

---

## 2. Decode — `DecodePathFromBits` (@`0x20b5c5a0`)

`DecodePathFromBits(string_view bytes)` is the only consumer of `GetVarInt` in the entire binary (rel32 call-site scan: exactly one site, at `0x20b5c5e2`). It reconstructs a `vector<proto::Direction>` from the type-2 stream. The decompiled body is 584 lines; the structure is:

```c
// ToroidalRouteCache::DecodePathFromBits @0x20b5c5a0  (decompiled, condensed)
StatusOr<vector<Direction>> DecodePathFromBits(string_view bytes) {
    BitDecoder bd(bytes);                       // LSB-first 64-bit window (§3.1)
    uint32_t count;
    if (!bd.GetVarInt(/*chunk=*/4, &count))     // line 102; esi=4 @0x20b5c5dd
        return Error("ICI routing cache does not encode path length correctly.");  // @0x9fd728f
    out.reserve(count);                         // line 114
    Direction prev{};
    for (uint32_t h = 0; h < count; ++h) {
        uint32_t field = bd.ReadBits(2);        // mask_[2]=0x3; buffer>>=2  (line 251/261)
        if (field == 3) {                       // REPEAT-PREVIOUS escape
            out.push_back(prev);                //   copy prev.orientation(+0x18)+polarity(+0x1c)
            continue;                            //   NO polarity bit consumed
        }
        uint32_t pol = bd.ReadBits(1);          // mask_[1]=0x1; buffer>>=1  (line 250/492)
        Direction d;
        d.orientation = field + 1;              // 0/1/2 → X(1)/Y(2)/Z(3)   (+0x18)
        d.polarity    = pol   + 1;              // 0/1   → POSITIVE(1)/NEGATIVE(2)  (+0x1c)
        d.presence   |= 0x3;                    // set has-bits for both fields (+0x10)
        out.push_back(d);                       // emplace_back, stride 0x20  (line 541)
        prev = d;
    }
    // TAIL VALIDATION (the encoder's flush contract)
    long remaining = bd.bits_avail + (bd.end - bd.cursor) * 8;   // lea [rcx+rdi*8] @0x20b5cb83
    if (remaining >= 8) return Error("...Remaining bits >= 1 byte.");           // @0xa0764b0
    if ((bd.buffer & mask_[remaining]) != 0)
        return Error("...Remaining bits are not cleared to zero.");             // @0xa044c1c
    return out;
}
```

### 2.1 The stream layout

| Field | Width | Encoding | Meaning |
|---|---|---|---|
| `hop_count` | variable | `GetVarInt(chunk=4)` | number of hops (self-delimiting; §3.2) |
| per hop: `orient_field` | 2 bits | `mask_[2]` | `{0,1,2}` → axis X/Y/Z; `3` → repeat-previous escape |
| per hop: `polarity` | 1 bit | `mask_[1]` | only when `orient_field<3`; `0`→POSITIVE, `1`→NEGATIVE |
| tail | <8 bits | zero pad | byte-align to a boundary; must be all-zero |

> **KEY —** the 2-bit field covers **only three axes**. Value `3` is never a 4th axis (A); it is the *repeat-previous-hop* escape that reuses the prior hop's `(orientation, polarity)` and consumes **no** polarity bit. This is the codec's run-length device: a straight multi-hop run along one direction packs as one explicit 3-bit hop followed by N−1 two-bit repeats. Confirmed in the decompile: `cmp field, 3` at `0x20b5c697`, the escape copies `prev+0x18`/`prev+0x1c` at `0x20b5c6aa..dd`, and the explicit arm reads the polarity bit at `0x20b5c708`.

### 2.2 The `proto::Direction` element

Each decoded hop is one `proto::Direction` (size `0x20`, vtable @`0x22019600`):

| Offset | Field | Type | Values |
|---|---|---|---|
| `+0x00` | vptr | — | — |
| `+0x08` | arena / `InternalMetadata` | — | — |
| `+0x10` | has-bits / presence | uint32 | `\|= 0x3` (both fields present) |
| `+0x18` | `orientation` | int | X=1, Y=2, Z=3 |
| `+0x1c` | `polarity` | int | POSITIVE=1, NEGATIVE=2 |

There is **no** `hop_count` field inside a `proto::Direction` in the bit-encoded form — one element is exactly one hop. This is distinct from the *runtime* packed code (`|d|<<6 | polarity<<3 | orient&7`) used by `CreateRoutePathFromDistance`, which carries a per-axis hop count in one entry; both decode to the same `{orientation, polarity}` pair but the cache form is one hop per element with the escape standing in for run-length.

### 2.3 Failure modes

| Failure | Cause | Diagnostic (`.rodata`) |
|---|---|---|
| header read failed | stream exhausted before the varint terminates | "ICI routing cache does not encode path length correctly." @`0x9fd728f` |
| orientation underflow | stream ended mid-hop on the 2-bit field | "ICI routing failed to retrieve %dth hop dimension from bit encoded cache data." @`0xa0ba533` |
| polarity underflow | stream ended before the 1-bit polarity | "ICI routing failed to retrieve %dth hop polarity from bit encoded cache data." @`0xa0ba4e5` |
| trailing slack ≥ 1 byte | `hop_count` too small / stream corrupt | "ICI route's encoded schema is erroneous. Remaining bits >= 1 byte." @`0xa0764b0` |
| trailing bits non-zero | flush did not zero-pad | "ICI route's encoding schema is erroneous. Remaining bits are not cleared to zero." @`0xa044c1c` |

> **GOTCHA —** the tail validation is strict on **both** sides. `remaining = bits_avail + (end-cursor)*8` must be `< 8` (no whole byte left) **and** the residual `<8` bits must be exactly zero. A reimplemented encoder that emits a single slack bit, or that pads with anything but zero, will be rejected on read. This pins the encoder's `Flush()` semantics (§3.3).

---

## 3. The primitive toolkit (`gloop` `bitcoding.cc` / `coder.cc`)

The codec rides on a small, shared bit-stream toolkit. The reader half is `BitDecoder` (four methods); the writer half is the inlined `BitEncoder` over a `gloop` `Encoder` byte buffer. No standalone `Put*`/`WriteBits` symbols exist — every writer primitive is inlined; they were reconstructed byte-exact from `BitEncoder::Initialize` @`0x21072d40` (a gamma-table self-test that encodes 1..255 with `PutGamma` then re-decodes with `GetGamma` and `CHECK`-equals) and from the profiler trace encoders that use the same toolkit with fixed widths.

### 3.1 The LSB-first 64-bit window + the `mask_` table

`BitDecoder` is a 0x28-byte sliding window over a byte stream:

```c
struct BitDecoder {            // 0x28 bytes
  /*+0x00*/ const char* base;  // string_view start (not touched by the refill ladder)
  /*+0x08*/ const char* cursor;// next byte to pull
  /*+0x10*/ const char* end;   // one-past-last byte
  /*+0x18*/ uint64_t    buffer;// LSB-first bit buffer; sentinel 0xFFFFFFFFFFFFFFFF == "empty/needs refill"
  /*+0x20*/ int32_t     bits_avail;
};
```

A `ReadBits(n)` is: if `bits_avail < n`, refill the buffer LSB-first one byte at a time (an 8-way unrolled byte ladder shifts each new byte up by the current bit position and OR-s it in, pulling up to 8 bytes / 64 bits); then `out = buffer & mask_[n]; buffer >>= n; bits_avail -= n`. (Confirmed in the decompile: `GetVarInt` reads `buffer` at `*((_QWORD*)this+3)` = `+0x18`, tests it against `-1`, and reads `bits_avail` at `*((_DWORD*)this+8)` = `+0x20`.)

`BitEncoder::mask_` @`0xbe79440` (.rodata, 65 qwords, `mask_[k]=(1<<k)-1`, indices 0..64) is shared by reader and writer — the reader to extract `n` bits, the writer to clamp a value to `n` bits before OR-ing into the accumulator:

| k | `mask_[k]` | k | `mask_[k]` |
|---|---|---|---|
| 0 | `0x0` | 8 | `0xff` |
| 1 | `0x1` | 16 | `0xffff` |
| 2 | `0x3` | 24 | `0xffffff` |
| 3 | `0x7` | 32 | `0xffffffff` |
| 4 | `0xf` | 63 | `0x7fffffffffffffff` |
| 7 | `0x7f` | 64 | `0xffffffffffffffff` |

> **NOTE —** the route cache reads only `mask_[1]` and `mask_[2]` in the per-hop loop (decompile lines 250–251: `v79 = mask_[1]; v6 = mask_[2]`) plus a variable `mask_[n]` in `GetVarInt` and the tail check. The full table exists because the same `BitDecoder` class serves wider fixed-width reads elsewhere (§3.4).

### 3.2 The four reader primitives

| Method | Address | Encoding | Route-cache use |
|---|---|---|---|
| `GetVarInt(chunk, *out)` | `0x20b5cfa0` | self-delimiting integer | hop-count header, chunk=4 (only) |
| `GetGamma(*out)` | `0x21073160` | Elias-gamma (value ≥ 1) | none in route cache |
| `GetBits64NoInline(n, *out)` | `0x21073760` | fixed n-bit, n∈[0,64] | none in route cache |
| `SkipBitsNoInline(n)` | `0x21073580` | advance n bits, no readout | none in route cache |

**`GetVarInt(chunk, *out)`** — the only varint the route cache uses (always `chunk=4`). `k` = number of leading 1-bits (LSB-first) of the buffer, found by `not buffer; tzcnt` (decompile: `_RAX = ~v11; tzcnt r8, rax`). Consume those `k` ones **and** the terminating 0 (`k+1` bits); `num_groups = k+1`. Then read `num_groups` groups of `chunk` bits; `value = sum_{g} group_g << (chunk*g)`. Returns a bool (`al=1` ok / `al=0` stream exhausted). Cost = `(k+1)·(1+chunk)` bits. Worked examples (chunk=4): `v=12` → `00011` (prefix `0`, group `1100`); `v=255` → `1011111111` (prefix `10`, groups `1111`,`1111`); `v=4096` → 20 bits.

**`GetGamma(*out)`** — Elias gamma, value ≥ 1. `b` = number of leading 0-bits (`not buffer; tzcnt`); if `b > 0x1F` → fail (decompile: `if ((unsigned)_RDX > 0x1F) ... return 0`); consume `b` zeros + the 1 terminator (`b+1` bits); `suffix = buffer & mask_[b]`; `value = (1<<b) + suffix`. Codeword length `2b+1`. Examples: `v=1`→`1`; `v=2`→`010`; `v=8`→`0001000`; `v=255`→`000000011111111` (15 bits). **Used only by the `BitEncoder::Initialize` self-test (1 call site, @`0x210730bd`) — it is on no route-cache or live-runtime path in this build.**

**`GetBits64NoInline(n, *out)`** — fixed n-bit, `n∈[0,64]` (`n>64` → `ud1` trap). `out = buffer & mask_[n]; buffer>>=n; bits_avail-=n`. If `n` straddles a refill, takes the low `bits_avail` bits, refills, then OR-s the high `(n-bits_avail)` bits shifted up. The profiler trace decoders' workhorse (6423 call sites, none in routing).

**`SkipBitsNoInline(n)`** — advance `n` bits without readout: if `n < bits_avail` just `buffer>>=n; bits_avail-=n`; else jump `cursor` by `(n-bits_avail)/8` bytes and refill the `n%8` remainder. 578 call sites, all profiler-trace (skip reserved fields).

### 3.3 The writer half (`Encoder` + `BitEncoder`)

The byte buffer is `gloop`'s `Encoder` (coder.cc, 0x20 bytes):

```c
struct Encoder {               // 0x20 bytes
  /*+0x00*/ char* cursor;      // buf_  (next write position)
  /*+0x08*/ char* limit;       // limit_
  /*+0x10*/ char* begin;       // owned alloc base (freed in dtor)
  /*+0x18*/ char* alloc_end | flags;  // top bit set ⇒ owns heap; inline-SSO marked 0x8000000000000028
};
// ~Encoder @0x21073980:  CHECK buf_ <= limit_  ("buf_ <= limit_", overrun guard) → free(begin)
```

`BitEncoder` wraps an `Encoder` + a `uint64` accumulator + a bit count. The inlined primitives:

| Writer | Behavior | Dual of |
|---|---|---|
| `PutBits(value, n)` | `acc \|= (value & mask_[n]) << bitpos; bitpos += n;` spill whole bytes LSB-first while `bitpos>=8` (`mov [cursor],acc_lo8; inc cursor; acc>>=8; bitpos-=8`) | `GetBits64` |
| `PutVarInt(chunk, v)` | `num_groups = max(1, ceil(bitlen(v)/chunk))`; emit `num_groups-1` one-bits + a 0, then `num_groups` little-endian `chunk`-bit groups | `GetVarInt` |
| `PutGamma(v≥1)` | `b = floor(log2(v))`; emit `b` zeros + a 1 + `(v & mask_[b])` | `GetGamma` |
| `Flush()` | pad the final partial byte with zeros and spill it | — |

The `PutBits` byte-spill loop is byte-confirmed in a real encoder (`EncodeUhiOciRequestRead` @`0xf5c6b20`: `mov [r8],sil; inc r8; shr rsi,8; ecx-=8`). `BitEncoder::Initialize` @`0x21072d40` builds `gamma_` (@`0x22593d00`, .bss, 256×uint32, `gamma_[i]=i|((2·floor(log2 i)+1)<<24)`) at runtime and proves `PutGamma`/`GetGamma` are exact inverses for 1..255.

> **NOTE —** `gamma_` is `.bss` (runtime-built by `Initialize`), `mask_` is `.rodata` (baked). A reimplementer needs only `mask_` for the route-cache codec; `gamma_` is part of the gamma path that routing never exercises.

### 3.4 Two consumers, one toolkit

The same `BitDecoder`/`Encoder`/`BitEncoder` toolkit serves two unrelated subsystems with **different layouts**:

| Consumer | Primitives used | Layout | Call sites |
|---|---|---|---|
| Route cache (this page) | `GetVarInt`/`PutVarInt(chunk=4)` + fixed `PutBits(2)`/`PutBits(1)` | varint header + 2b/1b per hop | `GetVarInt`: 1 (`DecodePathFromBits` @`0x20b5c5e2`) |
| Device-trace profiler | `GetBits64`/`PutBits` (fixed width) + `SkipBits` | fixed-width `TraceHeader` fields | `GetBits64`: 6423; `SkipBits`: 578; `Encoder` dtor: 566 |

The profiler's fixed-width trace headers are out of scope here (see [Profiling](../profiling/overview.md) and [VFC/VLC/GFC payloads](../profiling/payload-vfc-vlc-gfc.md)). The relevant fact for routing: **no route-cache field uses gamma or fixed-width `GetBits64`** — the body is purely `PutBits(2)`+`PutBits(1)` with a single `GetVarInt(chunk=4)` header.

---

## 4. The encode side — the inferred path packer

The offline route-cache generator is **not** present in `libtpu.so` (the `.binarypb.compressed` blobs are precomputed and embedded). The packer below is reconstructed as the byte-exact inverse of `DecodePathFromBits` and round-trips in software against the shared `BitEncoder`; the tail contract (§2.3) pins its `Flush()` semantics:

```c
// inverse of DecodePathFromBits — INFERRED-POLICY (escape preference reasoned from decoder)
string EncodePathToBits(const vector<proto::Direction>& hops) {
    BitEncoder enc;
    enc.PutVarInt(/*chunk=*/4, hops.size());          // self-delimiting hop-count header
    for (size_t h = 0; h < hops.size(); ++h) {
        if (h > 0 && hops[h].orientation == hops[h-1].orientation
                  && hops[h].polarity    == hops[h-1].polarity) {
            enc.PutBits(2, 3);                         // REPEAT-PREVIOUS escape (field==3)
        } else {
            enc.PutBits(2, hops[h].orientation - 1);   // X(1)/Y(2)/Z(3) → 0/1/2
            enc.PutBits(1, hops[h].polarity   - 1);    // POSITIVE(1)/NEGATIVE(2) → 0/1
        }
    }
    enc.Flush();                                       // zero-pad to byte boundary (tail contract)
    return enc.bytes();
}
```

Per-hop budget: 3 bits explicit (2 orient + 1 polarity), 2 bits for a same-direction repeat. The header is `(k+1)·5` bits via `GetVarInt(chunk=4)`. The whole stream is byte-aligned with a zero-padded final partial byte.

> **CONFIDENCE (INFERRED) —** the `{2-bit-repeat | 3-bit-explicit}` alphabet is *forced* by the decoder (field==3 is exclusively the repeat escape), so any conforming encoder is bounded to it. But the generator's exact **tie-breaking** — whether it always prefers the escape on a same-direction repeat, or ever emits an explicit hop to keep some alignment — is not byte-traced because the generator binary is not shipped. The "always prefer the escape" policy above is the most-compact legal encoding and is what a minimal reimplementation should emit; it is marked **LOW** for the precise generator behavior, **HIGH** for the byte format and the flush contract.

---

## 5. The rotation — `TopologyRotationHelper` (canonicalization)

`TopologyRotationHelper` is the object the dedup VALUE (`vector<proto::Orientation>`) is consumed through. It is *not* part of the bit stream — it is the coordinate-frame transform applied to the decoded path's keys and coordinate vectors so that one canonical blob serves every X/Y/Z axis-orientation of a shape. `CacheRead` (@`0x20b5da20`) builds it once per proto and applies it to every scheme.

### 5.1 `Create` — building the two frames (@`0x20bf2380`)

`Create(topo, orients)` allocates a 0x28-byte helper holding **two** base `Topology` objects and the orientation list:

```c
struct TopologyRotationHelper {     // 0x28 bytes
  Topology* actual;     // +0x00  Topology(ORIGINAL dims, ORIGINAL wraps)  — running/physical frame
  Topology* canonical;  // +0x08  Topology(PERMUTED dims, PERMUTED wraps)  — baked-blob frame
  vector<proto::Orientation> orientations;  // +0x10 base / +0x18 size / +0x20 cap (the permutation)
};

// Create @0x20bf2380 (decompiled, condensed)
helper = (TopologyRotationHelper*) operator new(0x28);           // line 106
dims   = topo.GetDimensionSizes();        // vtable+0x50
pdims  = PermuteVector<int>(dims, orients);     // @0x20bf2900  (line 68)
wraps  = topo.GetDimensionWraps();        // vtable+0x60
pwraps = PermuteVector<bool>(wraps, orients);   // @0x20bf2c60  (line 91)
helper->actual    = new Topology(dims,  wraps);   // 0x58-byte Topology  (line 184)
helper->canonical = new Topology(pdims, pwraps);  // 0x58-byte Topology  (line 188)
helper->orientations = orients;                   // copy into +0x10..
```

(Decompile confirms: `operator new(0x28)` for the helper, two `operator new(0x58u)` + `Topology::Topology` ctors, and `PermuteVector<int>`/`PermuteVector<bool>` calls.) The permuted inner topology applies the orientation list to **both** dims and wraps, so the canonical-frame radix used by `GetId` matches the blob's axis ordering.

### 5.2 `RotateId` — the per-key remap (@`0x20bf3020`)

Each cached `(src_chip_id, dest_chip_id)` is re-keyed before insertion:

```c
int RotateId(int id) {
    Coordinates coord  = actual.GetCoordinate(id);    // vtable+0x88; id→coord, actual-frame radix
    Coordinates rcoord = RotateCoordinates(coord);    // @0x20bf2f00; gather coords by orientation
    return canonical.GetId(rcoord);                   // vtable+0x90 (call [...+144]); coord→id, canonical radix
}
```

`GetCoordinate` and `GetId` are row-major mixed-radix over the per-frame dim sizes (`coord[k] = id mod dimsize[k]`, quotient carried forward; `GetCoordinate` @`0x20bf50e0` is an `idiv [dims+k*4]` chain). So `RotateId` converts a canonical-frame linear chip id to the actual-frame chip id of the axis-permuted topology. (Decompile confirms the `RotateCoordinates` call and the `canonical->GetId` vtable call at offset `144` = `0x90`.)

### 5.3 `RotateCoordinates` / `PermuteVector` — a pure axis gather (@`0x20bf2f00`)

```c
Coordinates RotateCoordinates(const Coordinates& c) {
    vector<int> vals = c.GetCoordinates();
    vector<int> out  = PermuteVector<int>(vals, orientations);  // out[k] = vals[orientations[k]-1]
    return Coordinates(Span<int>(out));
}
// PermuteVector<T>(in, orients):  for k:  dim = OrientationToDimension(orients[k]);  out[k] = in[dim]
```

> **KEY —** the rotation is a **pure axis permutation** of the coordinate vector: `out[k] = in[orient[k]-1]`. There is **no per-axis sign flip and no additive offset** — the dedup never mirrors or shifts, it only relabels which physical axis plays the role of canonical axis `k`. Any seam/twist geometry lives in the cached blob and the live distance metric, not here.

### 5.4 The Orientation↔Dimension bijection

`PermuteVector` maps an orientation to a dimension index via `Direction::OrientationToDimension` (@`0x20c027c0`) and its inverse `DimensionToOrientation` (@`0x20c02700`):

| `proto::Orientation` | int | `OrientationToDimension` |
|---|---|---|
| UNKNOWN | 0 | **ERROR** "Unknown orientation" @`0x85edbbb` |
| X | 1 | 0 |
| Y | 2 | 1 |
| Z | 3 | 2 |
| A | 4 | 3 |
| B | 5 | 4 |
| C | 6 | 5 |

`OrientationToDimension(o) = o - 1` (decompile: `*(_DWORD*)(a1+8) = *a2 - 1`, with the UNKNOWN guard); `DimensionToOrientation(d) = d + 1` for `d∈[0,5]` — exact inverses. `RotateOrientation(o)` (@`0x20bf3180`) is the *inverse* permutation of a single axis label: `wmemchr` the orientation list for `o`, return `index+1`, else error "Rotation doesn't include dimension %s" @`0x857efd8`.

> **NOTE —** `OrientationToDimension` supports 6 axes (X/Y/Z/A/B/C), but the bit-encoded path's 2-bit field only reaches X/Y/Z (`field∈{0,1,2}`). The slice-builder twisted shapes are ≤3 physical axes, so A/B/C are reachable by the rotation API but never appear in a baked twisted blob — consistent with the 2-bit field width.

### 5.5 Why this is the 4× dedup payoff

The [deduplicator](route-cache-dedup.md) registers each shape under multiple orientation keys `{X,Y,Z,NONE}`; the dedup VALUE is the `vector<proto::Orientation>` mapping canonical→actual. **One** baked blob (written in the canonical axis order) serves every X/Y/Z rotation of that shape because `CacheRead` re-keys each `(src,dst)` by `RotateId` and re-orients each distance coordinate by `RotateCoordinates` — a permutation of axes only. The codec emits the same canonical hop sequence; the rotation lands it on the right physical chips.

---

## 6. Round-trip summary

```text
ENCODE (offline generator; INFERRED inverse)      DECODE (DecodePathFromBits @0x20b5c5a0; CONFIRMED)
  enc = BitEncoder()                                dec = BitDecoder(bytes)
  enc.PutVarInt(4, n)        ── header ──           n = dec.GetVarInt(4)            // mask_[n], unary prefix
  for hop in hops:                                  out.reserve(n); for h in 0..n-1:
    if repeat: enc.PutBits(2, 3)  ── per hop ──       f = dec.ReadBits(2)            // mask_[2]
    else: enc.PutBits(2, o-1)                         if f==3: out += copy(prev)     // repeat escape
          enc.PutBits(1, p-1)                         else:    p = dec.ReadBits(1)   // mask_[1]
  enc.Flush()  (zero-pad)    ── tail ──                        out += Dir{o=f+1, p=p+1}; prev=out.back()
                                                    assert remaining < 8 && all-zero // tail contract

Shared primitives (LSB-first window, mask_[k]=(1<<k)-1 @0xbe79440):
  GetVarInt/PutVarInt : unary prefix + N·chunk-bit groups   (route cache, chunk=4 — 1 call site)
  GetGamma/PutGamma   : Elias gamma                          (BitEncoder::Initialize self-test only)
  GetBits64/PutBits   : fixed N-bit                          (profiler trace headers; route cache uses 2b/1b)
  SkipBits            : advance N bits                       (profiler trace only)

Then per scheme (toroidal-route-cache.md CacheRead):  skey = RotateId(src); dkey = RotateId(dst)
  RotateId(id)         = canonical.GetId( RotateCoordinates( actual.GetCoordinate(id) ) )
  RotateCoordinates(c) = Coordinates( PermuteVector<int>(c.vals, orients) )   // out[k]=in[orient[k]-1]
```

---

## 7. Function & data map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `ToroidalRouteCache::DecodePathFromBits` | `0x20b5c5a0` | type-2 bit-stream decoder (584 lines) | HIGH |
| `BitDecoder::GetVarInt` | `0x20b5cfa0` | self-delimiting varint reader (chunk=4 in route cache) | HIGH |
| `BitDecoder::GetGamma` | `0x21073160` | Elias-gamma reader (Initialize self-test only) | HIGH |
| `BitDecoder::GetBits64NoInline` | `0x21073760` | fixed n-bit reader (profiler) | HIGH |
| `BitDecoder::SkipBitsNoInline` | `0x21073580` | advance n bits (profiler) | HIGH |
| `BitEncoder::Initialize` | `0x21072d40` | gamma-table builder + encode/decode self-test (writer witness) | HIGH |
| `Encoder::~Encoder` | `0x21073980` | byte-buffer dtor (`CHECK buf_ <= limit_`) | HIGH |
| `BitEncoder::mask_` | `0xbe79440` | 65-qword `(1<<k)-1` table (.rodata) | HIGH |
| `BitEncoder::gamma_` | `0x22593d00` | 256-uint32 runtime gamma table (.bss) | HIGH |
| inferred path packer | — | `PutVarInt(4,n)`+`PutBits(2)/(1)`+`Flush` inverse | HIGH format / LOW policy |
| `TopologyRotationHelper::Create` | `0x20bf2380` | builds actual+canonical Topology + orient list | HIGH |
| `TopologyRotationHelper::RotateId` | `0x20bf3020` | id→coord→permute→id chip-key remap | HIGH |
| `TopologyRotationHelper::RotateCoordinates` | `0x20bf2f00` | axis-gather of a coordinate vector | HIGH |
| `TopologyRotationHelper::RotateOrientation` | `0x20bf3180` | inverse axis-label permutation (`wmemchr`) | HIGH |
| `PermuteVector<int>` / `<bool>` | `0x20bf2900` / `0x20bf2c60` | `out[k]=in[orient[k]-1]` gather | HIGH |
| `Direction::OrientationToDimension` | `0x20c027c0` | `o-1` (UNKNOWN → error) | HIGH |
| `Direction::DimensionToOrientation` | `0x20c02700` | `d+1`, d∈[0,5] | HIGH |
| `proto::Direction` ctor / vtable | `0x20c0ad60` / `0x22019600` | 0x20-byte hop element (orient+0x18, polarity+0x1c) | HIGH |

---

## Cross-References

**Route cache — siblings (this codec's neighbors)**
- [Route-Cache Decompress](route-cache-decompress.md) — `Decompress(CompressedToroidalRouteCache)`, the inflate step that feeds this codec
- [Toroidal Route Cache](toroidal-route-cache.md) — the message hierarchy and `CacheRead`, which calls `DecodePathFromBits` and `RotateId`/`RotateCoordinates` per scheme
- [Route-Cache Dedup](route-cache-dedup.md) — `RouteCacheDeduplicator::Find`, the cache key, and the `vector<proto::Orientation>` value this rotation consumes

**Routing context**
- [Routing — Section Map](overview.md) — where the cache sits in the route pipeline (family B, §2.2)
- [Route-Table Generation](route-table-generation.md) — the live generator (family A) whose offline twin produces the baked blobs this codec reads
- [Randomized Toroidal WildFirst](randomized-toroidal-wildfirst.md) — the path algorithm whose output is encoded into the cache
- [Get Distances](get-distances.md) — the torus-reduced distance the `distance` (type-0) scheme variant stores, sibling to the bit-encoded path

**Other toolkit consumer**
- [Profiling — overview](../profiling/overview.md) · [VFC/VLC/GFC payloads](../profiling/payload-vfc-vlc-gfc.md) — the device-trace profiler, the fixed-width `GetBits64`/`SkipBits` consumer of the same `gloop` bit toolkit

- [back to index](../index.md)
