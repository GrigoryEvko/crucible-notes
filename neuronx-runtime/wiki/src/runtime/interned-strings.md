# Interned String Database

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, soname `libnrt.so.1`, real file `libnrt.so.2.31.24.0`, version namespace `NRT_2.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present, and `.text`/`.rodata`/`.data` VMA equals file offset, so every `0x5…`/`0xc…` is both an analysis VMA and a file offset. The Rust engine is statically linked from `rustc 1.91.1` (`/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb/`) with vendored `hashbrown 0.15.5` (SBOM-confirmed, module path `/rust/deps/hashbrown-0.15.5/src/raw/mod.rs` @`.rodata`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — the six C-ABI shims and the six `string_db` engine functions are each pinned to a `.symtab` symbol + `.text` address; the 48-byte `InternedDataShard`, the 32-byte SwissTable bucket, the SipHash key, and the per-NC placement at `CONTEXTS+0x200` (stride 640) are cross-checked against the IDA Hex-Rays decompile **and** objdump byte-level disassembly. The verbatim 32-byte key string `"uespemosarenegylmodnarodsetybdet"` and the `0x3FFFFFFFFFFFF`/`+1` masking are read directly from `.rodata` and the `get_id` body. The bucket field at `B-0x20` (`key_len`) being a distinct field vs. a monomorphized key duplicate is `[MED]`. · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

`neuron_rustime::string_db` is the runtime's **content-addressed string interner**: it turns arbitrary byte strings (model names, tensor names, span labels) into stable 50-bit `u64` ids that travel in the trace wire format as compact tokens, and it materializes the `id → string` map only at snapshot time. It is a first-party Rust crate compiled into `libnrt.so`, reached from C++ through six thin cbindgen-style C-ABI shims (`nrt_interned_string_db_*`, `0x508a10..0x509642`). The design splits cleanly into two halves that never touch: a **pure id function** (`string_db::get_id` @`0x5b4990`) that is just `bytes → u64`, and a **sharded storage table** (`InternedDataShard`) that maps `id → owned bytes`.

The familiar reference frame is a **deduplicating symbol interner backed by a SwissTable** — the shape of `string_cache` or an LLVM `StringPool` — but with two Neuron-specific twists. First, the id is a **fixed-key SipHash-1-3**, not a per-process-seeded hash and not a monotonic counter: the same string yields the **same non-zero id in every process, on every run, on every NeuronCore** (`get_id` carries a hard-coded 32-byte key `"uespemosarenegylmodnarodsetybdet"` with no env/runtime override). Because the id is a deterministic hash, lookup on the hot path never consults the table at all — callers compute `get_id(name)` and emit the id directly as the trace token; the `id → string` direction exists only for offline read-out. Second, the storage is **sharded one table per NeuronCore**: the 256 `InternedDataShard`s live *inside* the `sys_trace` `CONTEXTS[256]` array (each at `NcContext+0x200`), each behind that core's own futex `RwLock`. There is no global table; the shards are unioned into one map at snapshot.

This page documents the interner to reimplementation accuracy: the **shard placement and lock protocol** (per-NC `RwLock`, shared on intern, layered under one global `RwLock` for the whole-array merge); the **`get_id` SipHash-1-3 function** with its key constants and masking; the **C↔Rust (de)serialization** at each of the six shim boundaries (C string in → UTF-8 validate → shard select → SwissTable insert → id out, and the reverse export to two C arrays); and the **fixed-key consequence** — that ids are globally stable, which is precisely what makes the cross-NC merge a deduplicating union rather than a relabel. The `CONTEXTS` array itself, the per-NC `RwLock` encoding, and the event ring that fills alongside each shard are owned by [trace/rust-capture](../trace/rust-capture.md); this page describes the shard's *placement* and links there, and does not re-derive the trace engine.

For reimplementation, the contract is:

- **The id function** — `get_id` is SipHash-1-3 (1 compression / 3 finalization rounds) under a **fixed 32-byte key**, hashing `bytes ++ [0xFF]`, then `(h & 0x3FFFFFFFFFFFF) + 1`. Deterministic, non-zero, range `[1, 2^50]`; id `0` is reserved as the "Invalid interned string" sentinel. The function is **pure** — it never reads or writes any shard.
- **The shard** — `InternedDataShard` is a `hashbrown 0.15.5` SwissTable `HashMap<u64 id, (ptr, len)>` (48 bytes: 32-byte `RawTableInner` + 16-byte `RandomState`), keyed on the **id**, value = a heap-copied owned byte slice. Bucket element `T` is 32 bytes (`{key_len, id, value_ptr, value_len}`).
- **The two hash regimes** — the *entry id* uses the fixed SipHash key (deterministic); the *bucket placement* uses the shard's per-thread random `RandomState` at `+0x20/+0x28`. The merge is safe because ids collide deterministically while bucket layout does not.
- **The sharded lock protocol** — 256 per-NC futex `RwLock`s (write on `register_entry`, read on `combine`) under one process-global `GLOBAL_LOCK` taken read for the whole-array merge.
- **The export round-trip** — `get_entries` walks the SwissTable into two malloc'd C arrays (`u64 ids[]`, `char* names[]`); the C++ ntff layer fills `std::unordered_map<u64,std::string>`; `free_entries` reclaims both arrays (`CString::from_raw` per name).

| | |
|---|---|
| **Source TU** | `src/string_db.rs` (Rust 1.91.1); C-ABI shims in `src/lib.rs` |
| **C-ABI shim band** | `0x508a10..0x509642` (6 fns, statically linked, local `t` symbols — not dynamic exports) |
| **Id function** | `string_db::get_id` @`0x5b4990` — SipHash-1-3, fixed key, 50-bit `+1` |
| **SipHash key** | `"uespemosarenegylmodnarodsetybdet"` (32 B, `.rodata`, `qmemcpy`'d; no per-process seed) |
| **Id mask** | `(h & 0x3FFFFFFFFFFFF) + 1` → non-zero, `[1, 2^50]`; id 0 = `"Invalid interned string"` |
| **Shard** | `InternedDataShard` (48 B = `0x30`); hashbrown `RawTable` keyed by 50-bit id |
| **Bucket element** | `T` = **32 B** (`{key_len:u64, id:u64, value_ptr:u64, value_len:u64}`) |
| **Sharding factor** | **256** — one shard per NeuronCore at `CONTEXTS[nc]+0x200` (`CONTEXTS` @`0xc0d300`, stride 640) |
| **Engine core** | `register_entry` @`0x5b4480` · `new` @`0x5b4830` · `combine` @`0x5b48a0` · `entries` @`0x5b4950` · `into_entries` @`0x5b4410` |
| **Bucket hash** | `BuildHasher::hash_one(id)` @`0x50a910`; `reserve_rehash` @`0x6bcf0` |
| **Global merge lock** | `GLOBAL_LOCK` @`0xcb0898` (24 B futex `RwLock`); `G_STATE` @`0xcb08b0` |

---

## 1. The Shard Model

### Purpose

The interner is **not** a single global table. It is sharded one `InternedDataShard` per NeuronCore, and the shards are embedded inside the `sys_trace` capture array `CONTEXTS` @`0xc0d300` — a static `[NcContext; 256]`, stride 640 (`0x280`), total `0x28000`. Each `NcContext` carries the per-core trace ring, counters, and enable bitmap, and — at byte offset `+0x200` — one 48-byte `InternedDataShard` guarded by that core's own futex `RwLock` (`NcContext+0x00`). A string interned for NeuronCore `i` lands in `CONTEXTS[i]`'s shard and nowhere else.

This placement is owned by [trace/rust-capture](../trace/rust-capture.md), which derives the full 640-byte `NcContext` and the two-tier lock; here only the shard's slot (`+0x200`) and its lock discipline are in scope. The six `nrt_interned_string_db_*` shims operate on a `*InternedDataShard` they are handed (or one they heap-allocate via `shard_alloc` @`0x509490` for the snapshot temp), so the shims are shard-location-agnostic — the per-NC topology is imposed by `sys_trace::capture`, not by the shims.

### Static placement

```text
CONTEXTS  @0xc0d300   [NcContext; 256]   stride 0x280 (640)   total 0x28000
  ├─ CONTEXTS[0]   @0xc0d300
  │    +0x000  RwLock        ── per-NC futex RwLock (owned by trace/rust-capture)
  │    +0x200  InternedDataShard (48 B)   ── THIS page's subject
  │    +0x240  enable[46] / state byte    ── state==2 ⇒ NC closed (skip on merge)
  ├─ …
  └─ CONTEXTS[255] @0xc34d80

GLOBAL_LOCK @0xcb0898  (24 B futex RwLock)  ── read-locked for the whole-array merge
G_STATE     @0xcb08b0  u8  (bit0 = capturing)
```

The 256 factor and the `+0x200` shard offset are proven two ways: the `readelf` symbol size on `CONTEXTS` (`0x28000 = 256 * 640`), and the per-NC loop bound `163840` (`= 256 * 640`) in `combine_all_interned_strings` with shard access at `base + 512` (`0x200`). `[HIGH]`

### `InternedDataShard` (48 bytes = `0x30`)

The shard is `Box`/inline of a hashbrown `HashMap<u64, V, RandomState>` — a 32-byte `RawTableInner` followed by a 16-byte SipHash `RandomState`. Offsets are read from `new` (@`0x5b4830`, three `movups` of the empty-table blobs) and `shard_alloc` (@`0x509490`, `__rust_alloc(0x30, 8)`).

| Field | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| `bucket_mask` | `+0x00` | `u64` | `buckets - 1`; `0` when empty | HIGH |
| `ctrl` | `+0x08` | `*u8` | control-byte array; → `Group::static_empty()` (16×`0xFF`) when empty | HIGH |
| `growth_left` | `+0x10` | `u64` | slots before next rehash; `0` when empty | HIGH |
| `items` | `+0x18` | `u64` | live entry count; `0` when empty *(`+0x00..+0x18` = `RawTableInner`)* | HIGH |
| `siphash k0` | `+0x20` | `u64` | bucket-hash `RandomState` word 0 (per-thread, `hashmap_random_keys`) | HIGH |
| `siphash k1` | `+0x28` | `u64` | bucket-hash `RandomState` word 1 *(`+0x20..+0x30` = `RandomState`)* | HIGH |

Empty-table init blobs are stored in `.rodata`/`.data.rel.ro`: `anon_5d375ae1…_9 @+0x00` (`{mask=0, ctrl=Group::EMPTY, growth=0}`) and `xmmword_C01EA0 @+0x10` (`{items=0, …}`). `new` writes these two blobs then draws fresh random key words at `+0x20/+0x28` from the TLS `hashmap_random_keys` counter (`std::sys::random::linux`, lazily seeded). `shard_alloc` is the heap-boxed equivalent.

### Bucket element `T` (32 bytes)

hashbrown stores elements *downward* from the control pointer: slot base `B = ctrl - 32*idx`. The four stored qwords are read from the `register_entry` stores and the `-32*idx` stride in every iterator.

| Field | Offset (rel. `B`) | Type | Role | Confidence |
|---|---|---|---|---|
| `key_len` | `B-0x20` | `usize` | `strlen` of the registered string (duplicate of `value_len`) | MED |
| `id` | `B-0x18` | `u64` | the SipHash id — **the HashMap key** | HIGH |
| `value_ptr` | `B-0x10` | `*u8` | heap-copied interned bytes (`__rust_alloc(len)`) | HIGH |
| `value_len` | `B-0x08` | `usize` | byte length of the interned string | HIGH |

The SwissTable control byte is `h2 = (hash_one(id) >> 57) & 0x7f` — the top 7 bits of the *bucket* hash of the id (not of the string). `[HIGH]`

> **CORRECTION (W2-RUST-STRINGDB) —** an earlier reading (cell L-API-10) inferred a 24-byte bucket holding `HashMap<u64, Box<[u8]>>` (a 16-byte fat pointer value). The in-binary stride is `32*idx` and `register_entry` stores **four** qwords per slot, so `T = 32 B` with an explicit `value_ptr`/`value_len` pair plus a duplicated `key_len`, not a `Box<[u8]>` fat pointer. The same cell also gave the register shim signature as `(s, id, len)`; the true 3-arg signature is `(s, id, shard)` — the `len` is recomputed by `strlen` inside the shim and the *shard* is the third argument. `[HIGH]`

> **QUIRK —** the shard holds **two independent hash regimes**, and conflating them breaks the merge. The *entry id* at `B-0x18` comes from `get_id` under a **fixed** key (deterministic — same string → same id everywhere, §2). The *bucket placement* uses `hash_one(id)` under the shard's **per-thread random** `RandomState` at `+0x20/+0x28`. So two cores interning the same string store the **same id** but at **different bucket positions** — which is exactly why `combine` (§4) can union 256 shards into one as an idempotent dedup: ids collide deterministically, layouts do not.

---

## 2. The Id Function — `get_id`

### Purpose

`string_db::get_id` @`0x5b4990` is the entire content-addressing scheme: `(bytes, len) → u64 id`. It is **pure** — it takes no shard argument and touches no table — so a caller can compute an id without any lock and use it directly as the trace token. The function is a SipHash-1-3 with a **hard-coded** 32-byte key (no per-process seed), which is the deliberate design choice that makes ids cross-process and cross-shard stable.

### The key and the masking

The 32-byte key is copied by `qmemcpy` from `.rodata` and reads verbatim as:

```text
"uespemosarenegylmodnarodsetybdet"
   (32 bytes, the verbatim .rodata byte order; stored byte-reversed —
    de-reversed it reads "detbytesdornamodlygenerasomepseu",
    ~ "det bytes dorna mod ly genera some pseu[do]", phrase meaning inferred)
```

SipHash-1-3 = **1** SipRound per message block (compression) and **3** SipRounds at finalization; the canonical rotation constants `13/16/17/21/32` are confirmed in the body. The input is the raw string bytes with a single `0xFF` terminator byte appended, then the 64-bit result is masked to 50 bits and incremented:

```text
id = (SipHash_1_3(key, bytes ++ [0xFF]) & 0x3FFFFFFFFFFFF) + 1
```

The `+1` guarantees a non-zero id; id `0` is reserved as the `"Invalid interned string"` sentinel (registered under id 0 by `capture_start`, see [trace/rust-capture §4](../trace/rust-capture.md)). The 50-bit width is not arbitrary: it matches the 50-bit `tracking_id` field in the `Event` record, so an event's `tracking_id` and an interned string id share one 50-bit namespace and can reference each other without a side table.

### Algorithm — `get_id`

```c
// Models neuron_rustime::string_db::get_id::h35ce7f10bf22ee46 @0x5b4990.
// PURE: no shard argument, no table access — just bytes -> u64.
function get_id(bytes_ptr, len):                 // (*u8, usize) -> u64
    // FIXED 32-byte key, qmemcpy'd from .rodata; NO per-process seed.
    key = "uespemosarenegylmodnarodsetybdet"     // stored byte-reversed

    h = SipHasher13::new_with_key(key)            // c=1 compression, d=3 finalize
    h.write(bytes_ptr, len)                       // hash the raw string bytes
    h.write(&0xFF, 1)                             // single 0xFF terminator byte
    raw = h.finish()                              // 3 finalization SipRounds (rot 13/16/17/21/32)

    return (raw & 0x3FFFFFFFFFFFF) + 1            // mask to 50 bits, +1 => id in [1, 2^50]
                                                  //   id 0 reserved = "Invalid interned string"
```

> **QUIRK —** the id is a **fixed-key hash, not a counter and not a per-process-seeded hash.** A reimplementer who reaches for `DefaultHasher` (per-process random seed) or a monotonic `AtomicU64` counter will produce ids that are *not* stable across processes or across the 256 shards, and the cross-NC merge in §4 will then either relabel or collide. The whole interner depends on `get_id` being a deterministic function of the bytes alone. There is no observed environment or runtime override of the key. `[HIGH]`

> **NOTE —** because lookup *is* the hash, there is **no `id → string` query on the hot path**. The tensor/exec call sites (`tensor_allocate`/`tensor_free`/`tensor_read`/`tensor_write`/`tensor_block_while_exec`, `nrt_get_interned_model_id`) call `nrt_interned_string_db_get_id` (@`0x509250`) to obtain the token and never consult any shard. The reverse direction is materialized only at snapshot (§4–§5).

---

## 3. `register_entry` — the C↔Rust Boundary

### Purpose

`nrt_interned_string_db_register_entry` @`0x5092a0` is the write half of the C-ABI surface: it takes a C string, an id (supplied by the caller, **not** recomputed), and a target shard, validates and UTF-8-checks the string, then inserts `id → bytes` into the shard's SwissTable. It is the place where the C ABI and the Rust engine meet, so the (de)serialization is explicit: a raw `char*` becomes a validated `&str`, and the engine heap-copies the bytes into an owned slice the shard then owns.

### Entry Point

```text
nrt_interned_string_db_register_entry (0x5092a0)     ── C-ABI shim (src/lib.rs)
  ├─ strlen(s)                                        ── C string length
  ├─ core::ffi::c_str::CStr::to_str                   ── UTF-8 validate (else NRT_INVALID)
  └─ string_db::InternedDataShard::register_entry (0x5b4480)
       ├─ BuildHasher::hash_one(id) (0x50a910)        ── bucket hash of the u64 id
       ├─ RawTable probe (SSE2 movmskb/tzcnt groups)  ── SwissTable group scan
       ├─ reserve_rehash (0x6bcf0)                     ── if growth_left == 0
       └─ __rust_alloc(len) + memcpy(bytes)            ── heap-copy the owned value slice
```

### Algorithm — the register shim

```c
// Models nrt_interned_string_db_register_entry @0x5092a0 (shim) + register_entry @0x5b4480 (engine).
// TRUE signature: (s:char*, id:u64, shard:*InternedDataShard) -> NRT_STATUS.
// The id is supplied by the caller; the shim does NOT call get_id.
function register_entry_shim(s, id, shard):
    if s == NULL:
        log!(level=2, "neuron_rustime", "src/lib.rs", …)   // null-arg path
        return NRT_INVALID                                  // == 2
    len = strlen(s)
    if len == 0:
        log!(level=2, …)
        return NRT_INVALID
    str = CStr::to_str(s)                                   // UTF-8 validate
    if str is Err:
        log!(level=2, "The input is not valid UTF-8")
        return NRT_INVALID

    conflict = InternedDataShard::register_entry(shard, id, str.ptr, len)  // engine, 0x5b4480
    return NRT_SUCCESS                                      // == 0  (conflict is logged, not surfaced)

// Engine core (0x5b4480): hashbrown insert keyed on the id.
function register_entry(shard, id, bytes, len):            // -> bool (1 = conflict handled)
    h  = hash_one(id)                                       // 0x50a910, RandomState @shard+0x20
    h2 = (h >> 57) & 0x7f                                   // SwissTable control tag
    slot = swisstable_find(shard, id, h)                    // SSE2 group probe (movmskb/tzcnt)

    if slot is occupied:
        if slot.bytes == bytes:   return 0                  // dup id + SAME bytes: idempotent
        else:                                               // dup id + DIFFERENT bytes: collision
            log!(warn, "Interned string db found conflicting id {} with name {} and {}")
            return 1

    if shard.growth_left == 0:
        reserve_rehash(shard)                               // 0x6bcf0: grow + re-probe
    owned = __rust_alloc(len); memcpy(owned, bytes, len)    // heap-copy: shard now OWNS the bytes
    swisstable_insert(shard, slot, {key_len:len, id, value_ptr:owned, value_len:len})
    return 0
```

> **GOTCHA —** the shim **trusts the caller's `id`** and never recomputes it from the string. `register_entry` is keyed on the id and only *stores* the bytes — it does not verify that `id == get_id(bytes)`. If a caller passes a string and an unrelated id, the table will faithfully map that id to that string. The runtime's own intern path (`sys_trace::capture::intern_string` @`0x5b0fb0`) always pairs them correctly (`id = get_id(bytes)` immediately before `register_entry`), but the C-ABI shim is a thin store and imposes no such invariant. The shim has **no in-binary callers** — it is an external C-ABI surface. `[HIGH]`

> **NOTE —** insertion is **idempotent on (id, same bytes)** and **warns on (id, different bytes)**. The duplicate-same-bytes case returns 0 with no allocation, so re-interning a string is cheap; the conflicting-bytes case is a SipHash collision under the 50-bit mask and is logged (the format pieces live in `.data.rel.ro` arg tables, recovered as `"Interned string db found conflicting id … with name … and …"`). `[HIGH]`

---

## 4. Lookup, Export, Free, and the Lock Protocol

### Purpose

The remaining five shims cover the read side: the pure-hash lookup, the bulk export to C arrays, the free of those arrays, and the two shard-lifecycle helpers (`shard_alloc`/`shard_free`). Layered over them is the sharded lock protocol that lets many cores intern concurrently while one global lock serializes the whole-array merge.

### Lookup is the hash

```c
// Models nrt_interned_string_db_get_id @0x509250.
function get_id_shim(s):                          // (char*) -> u64
    if s == NULL: return 0
    str = CStr::to_str(s)                          // UTF-8 validate
    if str is Err: return 0                        // invalid UTF-8 => 0 (sentinel)
    return string_db::get_id(str.ptr, strlen(s))   // 0x5b4990, fixed-key SipHash-1-3
```

No shard is consulted: the id *is* the content address. Callers (`tensor_*`, `nrt_get_interned_model_id`) use the returned 50-bit id directly as the trace token.

### The sharded lock protocol

Both lock tiers use the same `std::sys::sync::rwlock::futex::RwLock` encoding (Linux futex; write tag `0x3FFFFFFF`, release-write `xadd 0xC0000001`, read acquire `CAS +1`), identical to the per-NC lock derived in [trace/rust-capture §1](../trace/rust-capture.md).

| Operation | `GLOBAL_LOCK` | per-NC `RwLock` | Why |
|---|---|---|---|
| `intern_string` @`0x5b0fb0` (write path) | — (`G_STATE` gate) | **write** | SwissTable insert mutates the shard |
| `get_id` shim @`0x509250` (lookup) | — | — | pure hash; no shard touched |
| `combine_all_interned_strings` @`0x5b1cb0` (merge) | **read** | **read** (per NC) | iterate 256 shards into one |
| `get_entries` @`0x508ba0` (export) | — | (caller holds) | walk one (temp) shard into C arrays |
| `shard_free` @`0x509540` | — | — | drop a heap shard (snapshot temp) |

The intern path takes the **per-NC write lock** (the only writer of a shard); concurrent interns for *different* cores never contend. The merge takes the **global read lock plus each NC's read lock** in turn, so it runs concurrently with interns on already-merged cores. `[HIGH]`

### Export and free

```c
// Models nrt_interned_string_db_get_entries @0x508ba0 (the heaviest shim, 1540 B / 62 bb).
function get_entries(shard, out_ids, out_names, out_count):   // -> NRT_STATUS
    if any arg == NULL: return NRT_INVALID                     // == 2
    ids   = Vec<u64>::new()
    names = Vec<*char>::new()
    for (id, value_ptr, value_len) in shard:                   // hashbrown RawIter (ctrl-byte scan)
        ids.push(id)
        names.push(CString::new(value_ptr, value_len).into_raw())  // spec_new_impl; caller owns
    *out_ids   = ids.leak_as_c_array()                         // malloc'd u64[count]
    *out_names = names.leak_as_c_array()                       // malloc'd char*[count]
    *out_count = count
    return NRT_SUCCESS

// Models nrt_interned_string_db_free_entries @0x508a10.
function free_entries(ids, names, count):
    if ids == NULL || names == NULL: log!(error); return       // null-arg => no-op
    for k in 0..count:
        CString::from_raw(names[k])                            // reclaim each CString -> __rust_dealloc
    __rust_dealloc(names)
    __rust_dealloc(ids)
```

`shard_free` @`0x509540` is the drop of a heap shard: it walks the full buckets, `__rust_dealloc`s each value byte-slice, frees the ctrl/bucket array (size `33*buckets`), then frees the box — three dealloc tiers. `shard_alloc` @`0x509490` is its inverse (`__rust_alloc(0x30,8)` + empty-table + fresh keys).

> **GOTCHA —** the two C arrays returned by `get_entries` are **owned by the caller and must be freed only via `free_entries`.** Each `names[k]` is a `CString::into_raw` (Rust allocator); freeing it with libc `free` mismatches allocators. `free_entries` reclaims each via `CString::from_raw` before deallocating the array spines. A reimplementer mixing `malloc`/`free` with the Rust allocator here corrupts the heap. `[HIGH]`

---

## 5. Snapshot Serialization — Shards → Trace Stream

### Purpose

The `id → string` direction is materialized only at snapshot, by merging all 256 per-NC shards into one aggregate and exporting it. This is the bridge from the Rust interner to the NTFF wire format (`ntff::interned_data_db`), where interned ids appear as the tokens that the export resolves back to names.

### Algorithm — combine and export

```c
// Models sys_trace::capture::combine_all_interned_strings @0x5b1cb0
//   and nrt_sys_trace_get_all_interned_strings @0xb9d60 (the C++ bridge).
function get_all_interned_strings(out_map):                   // out_map = std::unordered_map<u64,string>&
    temp = nrt_interned_string_db_shard_alloc()               // 0x509490: fresh aggregate shard

    read_lock(GLOBAL_LOCK)                                     // @0xcb0898
    for nc in 0..256:                                          // CONTEXTS[nc]
        ctx = &CONTEXTS[nc]                                    // 0xc0d300 + nc*0x280
        read_lock(ctx.lock)                                    // per-NC shared
        if ctx.state_byte != 2:                                // +0x240 != 2 => NC open
            for (id, ptr, len) in ctx.shard:                   // shard @ ctx+0x200
                register_entry(temp, id, ptr, len)             // idempotent: dup ids skipped
        read_unlock(ctx.lock)
    read_unlock(GLOBAL_LOCK)

    get_entries(temp, &ids, &names, &n)                        // 0x508ba0: temp -> 2 C arrays
    for k in 0..n:
        out_map[ids[k]] = names[k]                             // C++ fill (operator[] + _M_replace)
    free_entries(ids, names, n)                                // 0x508a10
    shard_free(temp)                                           // 0x509540
```

Because `get_id` is deterministic (§2), a string interned independently on two cores produces the **same** id, so `register_entry` into `temp` skips it as a duplicate — the merge is a deduplicating union, not a relabel. The resulting `unordered_map<u64,string>` is what the C++ ntff serializer writes into the `interned_data_db` protobuf, and the same ids appear in the trace events as 50-bit `tracking_id` references (see [trace/ntff-format](../trace/ntff-format.md)). `[HIGH]`

> **QUIRK — ids are globally unique across all 256 shards.** Because the SipHash domain is **shared** (one fixed key, applied to the raw string bytes only — the NC index is *not* mixed into `get_id`), an id identifies a *string*, not a (string, core) pair. The same string has the same id in every shard; distinct strings get distinct ids (modulo the 50-bit collision probability that `register_entry` warns on). This is the opposite of the trace `Event.tracking_id`'s *fallback* encoding, where the NC index *is* folded into the high bits (`seq | (nc_idx << 50)`) to disambiguate auto-generated span ids — that folding happens in the event builder, not in `string_db`. For the interner: one global id space, 256 storage shards. `[HIGH]`

> **CORRECTION (W2-RUST-STRINGDB) —** an earlier note left "single global shard vs. per-thread shards merged at snapshot" unresolved. The resolved topology is **per-NeuronCore** (256 shards inside `CONTEXTS`, `0x28000 = 256*640`), merged at snapshot by `combine_all_interned_strings`. It is neither a single global shard nor per-*thread*: every thread emitting for core `i` writes the same `CONTEXTS[i]` shard under that core's write lock. `[HIGH]`

---

## Function Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `string_db::get_id` | `0x5b4990` | SipHash-1-3, fixed key, 50-bit id `+1` (pure) | HIGH |
| `InternedDataShard::new` | `0x5b4830` | in-place ctor: empty SwissTable + random `RandomState` keys | HIGH |
| `InternedDataShard::register_entry` | `0x5b4480` | hashbrown insert keyed on id; idempotent on dup, warns on conflict | HIGH |
| `InternedDataShard::combine` | `0x5b48a0` | iterate src buckets → `register_entry` into dst (per-core → aggregate) | HIGH |
| `InternedDataShard::entries` | `0x5b4950` | borrowing `RawIter` over the SwissTable | HIGH |
| `InternedDataShard::into_entries` | `0x5b4410` | consuming `RawIntoIter` (carries alloc/dealloc layout) | HIGH |
| `nrt_interned_string_db_get_id` | `0x509250` | C-ABI lookup shim: UTF-8 validate → `get_id`; 0 on null/bad-UTF8 | HIGH |
| `nrt_interned_string_db_register_entry` | `0x5092a0` | C-ABI write shim `(s, id, shard)`; UTF-8 validate → `register_entry` | HIGH |
| `nrt_interned_string_db_get_entries` | `0x508ba0` | C-ABI export: SwissTable → `u64 ids[]` + `char* names[]` + count | HIGH |
| `nrt_interned_string_db_free_entries` | `0x508a10` | C-ABI free of the two export arrays (`CString::from_raw` per name) | HIGH |
| `nrt_interned_string_db_shard_alloc` | `0x509490` | `__rust_alloc(0x30,8)` heap shard + empty table + fresh keys | HIGH |
| `nrt_interned_string_db_shard_free` | `0x509540` | drop heap shard (value slices → ctrl array → box; 3 tiers) | HIGH |
| `nrt_interned_string_db_combine_shards` | `0x508960` | C-ABI mirror of `combine`; no in-binary callers (external surface) | HIGH |
| `BuildHasher::hash_one` | `0x50a910` | bucket hash of the `u64` id under the shard `RandomState` | HIGH |
| `RawTable::reserve_rehash` | `0x6bcf0` | grow + re-probe when `growth_left == 0` | HIGH |
| `combine_all_interned_strings` | `0x5b1cb0` | merge 256 `CONTEXTS` shards into one aggregate (read-locked) | HIGH |
| `nrt_sys_trace_get_all_interned_strings` | `0xb9d60` | C++ bridge: alloc → combine → export → fill `unordered_map` → free | HIGH |

> **NOTE — gaps.** The exact hashbrown growth/rehash load-factor thresholds are delegated to `reserve_rehash` @`0x6bcf0` and were not individually disassembled (hashbrown 0.15.5 internal). The `key_len` field at `B-0x20` is `[MED]` — it is observably the same `len` value stored a second time, but whether it is a distinct struct field or a monomorphized key duplicate was not byte-disambiguated. The human meaning of the SipHash key plaintext (reads as reversed phrase fragments) is inferred; the **bytes** are `[HIGH]`.

## Cross-References

- [neuron_rustime: sys_trace Capture Engine](../trace/rust-capture.md) — owner of the `CONTEXTS[256]` array, the per-NC `RwLock`, and the event ring the shard sits beside (`InternedDataShard` @`NcContext+0x200`)
- [NTFF Trace File Format (ntff.proto)](../trace/ntff-format.md) — the `ntff::interned_data_db` wire message where the exported `id → string` map lands, and where 50-bit ids appear as trace tokens
- [Overview: the Three Trace Producers](../trace/overview.md) — how the interner feeds the offline NTFF profile that the producer side never reads back
- [Userspace Runtime Core — Internal Architecture Map](overview.md) — the layered runtime and where interned strings sit as cross-cutting infrastructure
- [back to index](../index.md)
