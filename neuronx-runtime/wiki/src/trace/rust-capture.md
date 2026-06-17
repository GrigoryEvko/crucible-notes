# neuron_rustime: sys_trace Capture Engine

> *All addresses, offsets, and symbols on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, soname `libnrt.so.2`, version namespace `NRT_2.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present, and `.text`/`.rodata` VMA equals file offset, so every `0x5b…` is both an analysis VMA and a file offset. The Rust producer is statically linked from `rustc 1.91.1` (`/rustc/ed61e7d7e242494fb7057f2657300d9e77bb4fcb/`), with vendored `crossbeam-queue 0.3.12` and `hashbrown 0.15.5` (SBOM-confirmed). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every function is pinned to a `.symtab` symbol + `.text` address; the static layout (`CONTEXTS` @`0xc0d300`, stride 640) and the in-struct offsets (`+0x230` seq, `+0x238` pushed, `+0x240` state/enable, `+0x80` ring base) are cross-checked against the IDA Hex-Rays decompile **and** objdump byte-level disassembly. The 112-byte `Event` field order is `[MED]` (inferred from the emit store sequence); the 46-member payload union itself is owned by [event-taxonomy](event-taxonomy.md). · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

## Abstract

`neuron_rustime::sys_trace::capture` is **producer (2)** of the runtime's three trace engines (see [overview §2](overview.md)) — the host-side software-span ring. It is a first-party Rust state machine compiled into `libnrt.so`, reached only through the cbindgen C-ABI shims (`nrt_sys_trace_capture_*`, owned by [rust-ffi](rust-ffi.md)). Where the device-profile producer drains on-device Notification Queues and the C `ntrace` ring logs coarse milestones, this engine records fine-grained host spans — `~46` event types across exec / tensor / dmem / collectives — emitted from `~30` instrumentation sites scattered through the runtime, on the *hottest* code paths.

The familiar reference frame is a **sharded, lock-free tracing subscriber drained out-of-band** — the shape of a high-throughput span buffer in `tracing-subscriber` or a per-CPU ftrace ring, but realized with one fixed shard *per NeuronCore* instead of per-thread. The process holds a static `[NcContext; 256]` array (`CONTEXTS` @`0xc0d300`); each `NcContext` owns an independent `crossbeam_queue::ArrayQueue<Event>` ring, an `InternedDataShard` string table, two atomic counters, and a per-event-type enable bitmap, all guarded by that NC's *own* futex `RwLock`. Producers on the same core contend only on the lock-free ring's CAS, never on a mutex; the per-NC `RwLock` is taken in **shared** (read) mode on every emit and drain. A single process-global `GLOBAL_LOCK` (a 3-word `GlobalLocked` region @`0xcb0898`) is taken in **write** mode only for whole-array lifecycle transitions (start/stop) and the cross-NC iteration that fetch/string-readout perform. A 1-byte `G_STATE` flag @`0xcb08b0` is the branch-free fast-path gate: every emit early-outs on `G_STATE & 1 == 0` before touching any lock.

This page documents the engine to reimplementation accuracy: the static layout and the four struct shapes (`NcContext` / `ArrayQueue` / `Slot` / `InternedDataShard`); the **lock-free index/stamp encoding** crossbeam uses to make the ring MPMC; the **overflow policy** (`force_push` → overwrite-oldest, *not* blocking, *not* drop-newest); the two-tier lock protocol; and the two hot algorithms — the `force_push` enqueue and the `capture_start`/`new_event_with_seq` lifecycle. The serde JSON read-out path (`api::fetch_events`) is producer (2)'s *second* terminal and is owned by [rust-serde](rust-serde.md); the 46-variant event model is owned by [event-taxonomy](event-taxonomy.md).

For reimplementation, the contract is:

- **The per-NC shard array** — a fixed `[NcContext; 256]` (stride `0x280`), each independently locked, each carrying its own ring + interner + counters + enable bitmap. There is no global event queue; cores never share a ring.
- **The lock-free ring** — crossbeam's `ArrayQueue<Event>`: head/tail as monotonic lap-encoded `usize`s, per-`Slot` stamps for writable/readable arbitration, CAS on tail (push) / head (pop), exponential `pause`/`yield` backoff.
- **The overflow policy** — `force_push` overwrites the oldest event under pressure and returns the displaced record; there is no backpressure to the instrumented caller. The ring is lossy by design because no live reader waits on it.
- **The two-tier lock protocol** — a global `GlobalLocked` futex `RwLock` (lifecycle + whole-array iteration) layered over 256 per-NC futex `RwLock`s (emit/drain, shared mode), plus a branch-free `G_STATE & 1` capturing gate.
- **The deterministic interner** — `string_db::get_id` is SipHash-1-3 under a *fixed* key, masked to 50 bits +1, matching the 50-bit `tracking_id` field; per-NC `InternedDataShard`s are hashbrown SwissTables merged at read-out.

| | |
|---|---|
| **Source TU** | `src/sys_trace/capture.rs` (Rust 1.91.1) |
| **Shard array** | `CONTEXTS` @`0xc0d300` — `[NcContext; 256]`, stride **640** (`0x280`), total `0x28000` |
| **Global lock region** | `GLOBAL_LOCK` @`0xcb0898` (24 B `GlobalLocked`); `max_events_per_nc` @`0xcb08a8`; `G_STATE` @`0xcb08b0` (1 B, bit0 = capturing) |
| **Arm** | `capture::capture_start` @`0x5b0590` |
| **Emit (hot)** | `capture::new_event_with_seq` @`0x5b1410` ← `new_event` @`0x5b1ff0` |
| **Drain** | `capture::drain_events` @`0x5afea0` (proto path) |
| **Stop / close** | `capture::capture_stop` @`0x5afd30` / `capture_close` @`0x5b0410` → `GlobalLocked::stop_capture` @`0x5af600` |
| **Intern** | `capture::intern_string` @`0x5b0fb0` → `string_db::get_id` @`0x5b4990` |
| **Ring** | `crossbeam_queue::ArrayQueue<Event>` @ `NcContext+0x80`; `new` @`0x5b5140` · `force_push` @`0x5b4b20` · `pop` @`0x5b51f0` |
| **Event record** | **112 bytes** (`0x70`); `Slot` stride **120** (`0x78`) = 112-B `Event` + 8-B stamp |
| **Ring size default** | `max_events_per_nc` = `0x100000` (clamp floor 1024); cost logged as `112*n/2³⁰` GB |
| **Interner** | `InternedDataShard` (48 B), hashbrown `RawTable` keyed by 50-bit id |

---

## 1. The Shard Array and Two-Tier Lock

### Purpose

The engine's spine is the static `CONTEXTS` array @`0xc0d300`: `[NcContext; 256]`, one slot per possible NeuronCore index, stride 640 bytes (`0x280`, total `0x28000`). Every operation routes through exactly one `NcContext` selected by `nc_idx`, except the three whole-array operations — `stop_capture`, `total_event_count`, and `combine_all_interned_strings` — which iterate all 256. There is **no global event queue**: a span emitted for NC `i` lands in `CONTEXTS[i]`'s ring and nowhere else. This is the same per-CPU sharding ftrace uses to make the producer side wait-free, applied at NeuronCore granularity.

Above the 256 per-NC locks sits one process-global lock. `GLOBAL_LOCK` @`0xcb0898` is a 24-byte `GlobalLocked` region: a futex `RwLock` word, a poison flag, and `max_events_per_nc` (the ring size, separately addressable @`0xcb08a8`). A 1-byte `G_STATE` @`0xcb08b0` sits immediately past it; its bit0 is the global "is-capturing" flag and serves as the branch-free fast-path gate.

### Static layout

```text
CONTEXTS  @0xc0d300   [NcContext; 256]   stride 0x280   total 0x28000
  ├─ CONTEXTS[0]   @0xc0d300
  ├─ CONTEXTS[1]   @0xc0d580
  ├─ …
  └─ CONTEXTS[255] @0xc34d80

GLOBAL_LOCK @0xcb0898  (GlobalLocked, symbol size 24)
  +0x00  i32  futex RwLock state
  +0x08  u8   poison flag
  +0x10  u64  max_events_per_nc  (== ring_size(); also addressable @0xcb08a8)
G_STATE     @0xcb08b0  u8  (bit0 = capturing; just past the GlobalLocked region)
```

The 256-stride and the `0x28000` total are proven two ways: by the `readelf` symbol size on `CONTEXTS`, and by the `v += 160 (i32 stride) ; v3 -= 640` loop pairs in `stop_capture` / `total_event_count` / `combine_all_interned_strings` (the i32 index advances by 160 four-byte words = 640 bytes per NC). `[HIGH]`

### The futex RwLock protocol

Both lock tiers use the same `std::sys::sync::rwlock::futex::RwLock` encoding (byte-verified identical across every locking function):

```text
state word (i32):
   0           = free
   0x3FFFFFFF  = write-locked
   n           = n readers held   (read acquire caps at 0x3FFFFFFD)
acquire write : CAS 0 -> 0x3FFFFFFF        (else write_contended)
acquire read  : incr, cap 0x3FFFFFFD       (else read_contended)
release write : xadd 0xC0000001            (then wake_writer_or_readers)
release read  : dec & 0xBFFFFFFF           (then maybe wake)
```

The crucial design fact: the **emit and drain hot paths take the per-NC lock in SHARED (read) mode only.** Multiple producers on the same NC therefore proceed concurrently through the read-lock and serialize *only* on the lock-free ring's tail CAS. The per-NC write lock is taken only by `intern_string` (table insert) and by the lifecycle teardown in `stop_capture`. `[HIGH]`

### When each lock is taken

| Operation | `GLOBAL_LOCK` | per-NC `RwLock` | Why |
|---|---|---|---|
| `capture_start` @`0x5b0590` | **write** | — (writes fields under global write) | whole-array realloc; exclusive |
| `new_event_with_seq` @`0x5b1410` | none (`G_STATE` gate) | **read** | wait-free fast path; ring CAS does the work |
| `drain_events` @`0x5afea0` | **read** | **read** | single-NC pop loop; global read pins lifecycle |
| `intern_string` @`0x5b0fb0` | (via FFI gate) | **write** | hashbrown table insert mutates the shard |
| `total_event_count` @`0x5b12e0` | **read** | **read** (per NC) | sum `+0x230` across 256 NCs |
| `combine_all_interned_strings` @`0x5b1cb0` | **read** | **read** (per NC) | merge 256 shards into one map |
| `stop_capture` @`0x5af600` | **write** | **write** (per NC) | drop every ring; set state byte = 2 |

> **QUIRK —** the engine is **sharded per NeuronCore, not per thread.** A reimplementer porting a per-thread tracing subscriber will assume each producing thread owns a buffer; here a single NC's ring is written by *every* thread that emits for that core, and the lock-free MPMC ring (not thread-locality) is what makes that safe. The `tracking_id` reflects this: when no caller id is supplied, it is `seq | (nc_idx << 50)` — the NC index is folded into the high bits so ids are globally unique across the 256 shards (`shl/shr $0x32` = 50, @`0x5b15cc`/`0x5b16f4`). The thread identity is recorded separately as a cached `gettid` (`syscall 186`, TLS `THREAD_ID`), not used for sharding.

---

## 2. Struct Layouts

Every offset below is byte-verified against the decompile and objdump (the verifying instruction is cited inline). The one `[MED]` region is the field order *within* the 112-byte `Event`.

### `NcContext` (stride 640 = `0x280`)

| Field | Offset | Type | Role |
|---|---|---|---|
| lock state | `+0x000` | `i32` | per-NC futex `RwLock` state (same encoding as `GlobalLocked`) |
| poison | `+0x008` | `u8` | set on panic-while-locked; checked by every guard drop |
| ring | `+0x080` | `ArrayQueue<Event>` | the lock-free per-NC ring (base = `+0x80`; `lea 0x80`, @`0x5aff49`) |
| interner | `+0x200` | `InternedDataShard` | 48-B hashbrown string table (id → name) |
| events_created | `+0x230` | `i64` | `lock xadd` on every accepted emit — the **seq source**; summed by `total_event_count` (`xadd` @`0x5b15b8`) |
| events_pushed | `+0x238` | `i64` | `lock incq` after `force_push`; `stop_capture` logs "captured `<n>`" (`incq` @`0x5b184d`) |
| enable / state | `+0x240` | `u8[46]` | per-event-type enable array; `+0x240` byte doubles as the **NC state**: `== 2` ⇒ closed (`cmpb 2` @`0x5b1594`); `+0x240+et == 1` ⇒ type `et` (0..45) captured (`cmpb 1` @`0x5b15a4`) |
| (padding) | `+0x26e..+0x280` | — | alignment to the 640-byte stride |

> **GOTCHA —** byte `+0x240` is **overloaded**: it is simultaneously enable-bit for event type 0 *and* the NC lifecycle state (`2` = closed). `stop_capture` writes a cleared `0x200`-byte block into `NcContext+0x80` whose state byte is `2`, which both disables type-0 and marks the NC closed; the emit filter then rejects with a single `cmpb 2` before the per-type `cmpb 1`. A reimplementation that separates "enabled[0]" from "nc_closed" into two fields is functionally equivalent but will not match the byte image — the binary fuses them.

### `ArrayQueue<Event>` (base = `NcContext+0x80`; crossbeam-queue 0.3.12)

| Field | Offset (rel. base) | Type | Role |
|---|---|---|---|
| head | `+0x000` | `AtomicUsize` | dequeue index; `CachePadded` → owns its 64-B line |
| tail | `+0x080` | `AtomicUsize` | enqueue index; `CachePadded` |
| cap | `+0x100` | `usize` | logical capacity = `max_events_per_nc` |
| one_lap | `+0x108` | `usize` | smallest power of two `> cap`; the lap-bit boundary |
| buffer | `+0x110` | `*Slot` | `Box<[Slot]>` data pointer (len follows) |

Offsets `cap@+0x100`, `one_lap@+0x108`, `buffer@+0x110` and the slot stride `0x78` are byte-verified in `pop` (`imul $0x78`, @`0x5b51f0`). The two `CachePadded` atomics each occupy a full 64-byte cache line so producers (writing `tail`) and consumers (writing `head`) never false-share. `[HIGH]`

### `Slot` (stride 120 = `0x78`)

| Field | Offset | Type | Role |
|---|---|---|---|
| event | `+0x000` | `Event` (112 B) | the payload record |
| stamp | `+0x070` | `AtomicUsize` | lap/availability arbiter: writable iff `stamp == tail`; readable iff `stamp == head + 1` |

### `Event` (112 bytes = `0x70`) `[MED]`

The record `force_push`'d into the ring. Field *presence* is certain (the 12-arg emit store sequence at `0x5b1780..0x5b1850`); exact per-field byte offsets within the 112 bytes are inferred, and the 40-byte `data` union's 46 members are owned by [event-taxonomy §the data union](event-taxonomy.md).

| Field | Type | Role |
|---|---|---|
| `event_type` | `u32` | 0..45 discriminant (gates the `+0x240` enable filter) |
| `phase` | `u32` | START / STOP / INSTANT |
| `nc_idx` | `u32` | NeuronCore index |
| `tid` | `u64` | cached `gettid` (TLS `THREAD_ID`, `syscall 186`) |
| `seq` | `u64` | monotonic per-NC sequence (`fetch-add` on `+0x230`) |
| `tracking_id` | `u64` | caller-supplied if nonzero, else `seq \| (nc_idx << 50)` |
| `timestamp_ns` | `u64` | `SystemTime::now().duration_since(UNIX_EPOCH)` in ns |
| `data` | 40-B union | per-event-type payload (46 members — [event-taxonomy](event-taxonomy.md)) |
| (discriminant) | `u8` @`+104` | `Some/None` tag used by `Option<Event>` / `force_push` return |

### `InternedDataShard` (48 bytes = `0x30`; `NcContext+0x200`)

| Field | Offset | Type | Role |
|---|---|---|---|
| RawTable a | `+0x00` | hashbrown `RawTable` | `bucket_mask` / `ctrl` ptr (empty ctrl from `.rodata` `0xC01E90`) |
| RawTable b | `+0x10` | hashbrown `RawTable` | `items` / `growth_left` (init from `.rodata` `0xC01EA0`) |
| siphash k0 | `+0x20` | `u64` | per-thread random SipHash key word 0 (`hashmap_random_keys`) `[MED]` |
| siphash k1 | `+0x28` | `u64` | per-thread random SipHash key word 1 `[MED]` |

Entries are `id:u64 → (name_ptr:u64, name_len:u64)` packed in the SwissTable buckets. Note the **two key regimes**: the *entry id* is computed by `string_db::get_id` under a **fixed** SipHash key (deterministic, so the same string interns to the same id across NCs and runs), while the table's *bucket hashing* uses the per-thread random `RandomState` at `+0x20/+0x28`. Merging at read-out is therefore safe — ids collide deterministically, bucket placement does not. The interner itself is owned by [interned-strings](../runtime/interned-strings.md). `[HIGH]`

---

## 3. The Lock-Free Enqueue Path

### Purpose

`ArrayQueue::force_push` @`0x5b4b20` is crossbeam-queue 0.3.12's bounded MPMC enqueue with the **overwrite-oldest** overflow policy. This is the engine's single most consequential design choice: under capture pressure the ring never blocks and never refuses — it evicts the oldest event and admits the newest, returning the displaced record. The runtime discards that return value, so head-of-trace events are silently lost when a ring fills. This is acceptable *because no live consumer waits on the ring* — NTFF is an offline, drained-later artifact (see [overview §1](overview.md)).

### Entry Point

```text
new_event_with_seq (0x5b1410)         ── builds the 112-B Event under per-NC read-lock
  └─ ArrayQueue<Event>::force_push (0x5b4b20)   ── overwrite-oldest enqueue
       ├─ Slot::stamp load / compare           ── writable iff stamp == tail
       ├─ CAS tail (tail -> next)              ── claim the slot
       ├─ (full) CAS head (head -> next)       ── advance: evict oldest
       └─ write Event; store stamp = tail + 1  ── publish
```

### The lock-free index/stamp encoding

crossbeam encodes `head` and `tail` as monotonically increasing `usize`s that carry a **lap** in the bits above `one_lap`. The physical slot index is the low bits; the lap parity is what distinguishes "this slot was already written this lap" from "ready to write". A reimplementer must reproduce three invariants exactly:

```text
one_lap  = smallest power of two > cap          // e.g. cap=0x100000 -> one_lap=0x100000
slot_idx = index & (one_lap - 1)                // physical slot
lap_bits = index & !(one_lap - 1)               // the lap counter (high bits)

WRITABLE slot for an enqueuer holding `tail`:   slot.stamp == tail
READABLE slot for a dequeuer holding `head`:    slot.stamp == head + 1

publish after write (push):   slot.stamp = tail + 1
publish after read  (pop):    slot.stamp = head + one_lap   // bump to next lap

advance past last slot:
  if slot_idx + 1 >= cap:  next = lap_bits + one_lap        // new lap, low index resets to 0
  else:                    next = index + 1
```

### Algorithm — `force_push`

```c
// Models crossbeam_queue::array_queue::ArrayQueue<Event>::force_push @0x5b4b20.
// Returns the displaced Event (Some) when the ring was full, else None (out+104 tag).
function force_push(q, ev):                       // q = &ArrayQueue @ NcContext+0x80
    backoff = 0
    tail = atomic_load(q.tail)                    // q+0x80
    loop:
        idx   = tail & (q.one_lap - 1)            // q+0x108: physical slot
        slot  = q.buffer + idx * 0x78             // q+0x110; Slot stride 120
        stamp = atomic_load(slot.stamp)           // slot+0x70

        if stamp == tail:                         // slot is WRITABLE this lap
            // compute the next tail (advance one slot, or roll the lap)
            if idx + 1 < q.cap:  next = tail + 1
            else:                next = (tail & ~(q.one_lap - 1)) + q.one_lap
            if CAS(q.tail, tail -> next):          // claim the slot
                write_event(slot.event, ev)        // 112-byte Event store
                atomic_store(slot.stamp, tail + 1) // publish: readable
                lock_incq(NcContext.events_pushed) // +0x238
                return None                        // nothing displaced

        else if (stamp + q.one_lap) == (tail + 1): // ring is FULL at this slot
            // OVERWRITE-OLDEST: advance head past the oldest, then retake the slot.
            head = atomic_load(q.head)            // q+0x00
            head_next = advance(head)             // same lap/roll rule on head
            if CAS(q.head, head -> head_next):     // evict the oldest reader-slot
                displaced = read_event(slot.event) // the victim record
                write_event(slot.event, ev)        // overwrite with the new event
                atomic_store(slot.stamp,           // republish at the new lap
                             head + q.one_lap)
                return Some(displaced)             // displaced returned (runtime drops it)
            // else: lost the head race; spin and retry

        // contended or transient: exponential backoff, then re-read tail
        backoff = backoff + 1
        if backoff <= 6:  for _ in 0..(1<<backoff): _mm_pause()   // spin (cap ~6/7)
        else:             std::thread::yield_now()                // cap ~0xB
        tail = atomic_load(q.tail)
```

> **QUIRK —** `force_push` is **overwrite-oldest, not drop-newest and not blocking.** Three policies are plausible for a full bounded ring; the binary picks the one that keeps the *newest* span and evicts the *oldest*, and it does so by advancing `head` (a normally-consumer-only field) from the *producer*. That cross-field CAS is why the consumer (`pop`/`drain_events`) and producer can both legitimately run under the **same shared per-NC read-lock**: neither holds exclusive access; the lap/stamp invariants are the entire synchronization. A reimplementer who guards `head` as consumer-exclusive will deadlock the overwrite path or corrupt the ring. The plain `ArrayQueue::push` (which *returns the event on full*) exists in crossbeam but is **not** the one this engine calls — the emit path always takes `force_push`.

### Backoff

Contention backoff is crossbeam's `Backoff`: an exponential count of `_mm_pause()` spins (the snooze count caps near 6/7) escalating to `std::thread::yield_now()` once the count crosses ~`0xB`. There is no futex sleep — the ring is wait-free in the common (uncontended) case and merely livelock-resistant under contention. `[HIGH]`

---

## 4. The Capture Lifecycle

### Purpose

`capture_start` arms the engine; `new_event_with_seq` is the per-event hot path; `stop_capture`/`capture_close` tear it down. All three lifecycle transitions take the global write lock; emit takes neither global lock (only the branch-free `G_STATE` gate) nor a per-NC write lock.

### Entry Point

```text
nrt_sys_trace_capture_start (C-ABI, 0x509740)        [rust-ffi]
  └─ capture::Config::from (0x5b2110)                ── marshal C config; clamp ring size
  └─ capture::capture_start (0x5b0590)               ── write-lock GLOBAL_LOCK; alloc per NC
       ├─ ArrayQueue<Event>::new (0x5b5140)          ── Box<[Slot; cap]>; head=tail=0
       └─ InternedDataShard::new (0x5b4830)          ── empty hashbrown + random siphash key

nrt_sys_trace_new_event[_with_seq] (0x509c90/0x509cc0)
  └─ capture::new_event (0x5b1ff0)
       └─ capture::new_event_with_seq (0x5b1410)     ── G_STATE gate; per-NC read-lock; force_push
```

### Algorithm — `capture_start`

```c
// Models capture::capture_start @0x5b0590.  Caller holds nothing; this takes GLOBAL_LOCK write.
function capture_start(config):                       // config = marshaled Config (0x5b2110)
    write_lock(GLOBAL_LOCK)                            // @0xcb0898; exclusive

    n = visible_virtual_cores()                       // 0x508550
    if n > 0x100:                                      // MAX_VIRTUAL_TPB guard
        panic("visible cores <n> exceeds MAX_VIRTUAL_TPB nc: <m>")
    if G_STATE & 1:                                    // already capturing
        warn("tracing has already been stopped…")      // (idempotency warn)
        unlock; return 1

    cap = config.max_events_per_nc                     // Config+256; default 0x100000, floor 1024
    log("Allocating memory for <n> events (<x> GB) for system trace ring buffer")
                                                       // x = 112 * cap / 2^30  (imul $0x70)
    for i in 0..n:                                      // each visible NeuronCore
        ctx = &CONTEXTS[i]                             // 0xc0d300 + i*0x280
        if config.enabled_for_nc[i] == 0:
            log("System trace capture disabled for this NeuronCore: <i>")
            continue
        drop_in_place(ctx.ring)                        // 0x5b2440: tear down any old ring/shard
        ctx.ring     = ArrayQueue::new(cap)            // 0x5b5140
        ctx.interner = InternedDataShard::new()        // 0x5b4830 (seeded w/ sentinel id)
        copy ctx.enable[0..46] = config.enabled_for_event_type[0..46]   // +0x240
        // ctx.enable[0] (the state byte) cleared to "open" (!=2)

    GlobalLocked.max_events_per_nc = cap               // @0xcb08a8
    G_STATE = 1                                         // bit0 set: capturing
    unlock(GLOBAL_LOCK)
```

### Algorithm — `new_event_with_seq` (hot path)

```c
// Models capture::new_event_with_seq @0x5b1410.  THE hot path: ~30 instrumented callers.
function new_event_with_seq(event_type, phase, nc_idx, data…, caller_tracking_id):
    if (G_STATE & 1) == 0:  return                     // branch-free fast-path gate (no lock)

    if nc_idx >= 0x100:                                // clamp out-of-range NC
        warn("…"); nc_idx = 0
    ctx = &CONTEXTS[nc_idx]                            // 0xc0d300 + nc_idx*0x280

    read_lock(ctx.lock)                                // per-NC SHARED lock (concurrent emit OK)
    // filter: NC open AND event_type in range AND this type enabled
    if ctx.enable[0] == 2:      goto done              // +0x240 == 2 => NC closed
    if event_type > 0x2D:       goto done              // 46 types (0..45)
    if ctx.enable[event_type] != 1:  goto done         // per-type gate

    seq = atomic_fetch_add(ctx.events_created, 1)      // +0x230 (lock xadd) — seq source
    if (seq >> 50) != 0:                                // 50-bit field overflow
        warn("sequence id overflow…")
    tracking_id = caller_tracking_id != 0
                  ? caller_tracking_id
                  : (seq | (nc_idx << 50))             // fold NC idx into high bits

    tid = THREAD_ID                                    // cached TLS gettid (syscall 186)
    ts  = SystemTime::now().duration_since(UNIX_EPOCH) // ns timestamp
    ev  = Event{ event_type, phase, nc_idx, tid, seq, tracking_id, ts, data }   // 112 B

    force_push(ctx.ring, ev)                            // 0x5b4b20: overwrite-oldest
    // (events_pushed at +0x238 is incremented inside force_push on success)
done:
    read_unlock(ctx.lock)
```

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `capture::capture_start` | `0x5b0590` | arm: write-lock, per-NC alloc, set `G_STATE`/ring size | HIGH |
| `capture::new_event_with_seq` | `0x5b1410` | emit hot path: gate → read-lock → filter → seq → `force_push` | HIGH |
| `capture::new_event` | `0x5b1ff0` | thin forwarder → `new_event_with_seq(…, seq_flag=0)` | HIGH |
| `capture::drain_events` | `0x5afea0` | proto drain: per-NC `pop` loop into 112-B caller buffer | HIGH |
| `capture::intern_string` | `0x5b0fb0` | per-NC write-lock; `get_id` + `register_entry`; returns id | HIGH |
| `capture::capture_stop` | `0x5afd30` | write-lock → `stop_capture` | HIGH |
| `capture::capture_close` | `0x5b0410` | idempotent close (Ok even if already stopped) | HIGH |
| `GlobalLocked::stop_capture` | `0x5af600` | iterate 256 NCs write-locked; drop ring; state byte = 2; `G_STATE=0` | HIGH |
| `GlobalLocked::total_event_count` | `0x5afc00` | sum `+0x230` across open NCs | HIGH |
| `capture::ring_size` | `0x5b2010` | read `GlobalLocked.max_events_per_nc` @`0xcb08a8` | HIGH |
| `capture::combine_all_interned_strings` | `0x5b1cb0` | merge 256 shards → unified id→name map | HIGH |
| `capture::Config::from` | `0x5b2110` | marshal C `nrt_sys_trace_config_t` → Rust `Config`; clamp ring size | HIGH |
| `string_db::get_id` | `0x5b4990` | SipHash-1-3, fixed key, 50-bit id +1 | HIGH |
| `InternedDataShard::register_entry` | `0x5b4480` | hashbrown SwissTable insert; idempotent on dup id | HIGH |
| `ArrayQueue::new` | `0x5b5140` | `Box<[Slot; cap]>`; `head=tail=0`; `one_lap = next_pow2(>cap)` | HIGH |
| `ArrayQueue::force_push` | `0x5b4b20` | overwrite-oldest enqueue (§3) | HIGH |
| `ArrayQueue::pop` | `0x5b51f0` | dequeue → `Option<Event>` | HIGH |

### Considerations

`stop_capture` does **not** drain — it drops every ring. Any events still in a ring at stop are lost unless `drain_events`/`fetch_events` ran first; the lifecycle is *arm → emit → drain → stop*, and the inspect controller ([inspect-profile-api](inspect-profile-api.md)) sequences the drain before the stop. `capture_close` differs from `capture_stop` only in idempotency: it returns `Ok(0)` even when already stopped, so a double-close is harmless.

> **CORRECTION (W2-SYSTRACE-01) —** the `G_STATE` byte @`0xcb08b0` was initially read as the low byte of the `GlobalLocked` struct's discriminant. The 24-byte `GLOBAL_LOCK` symbol spans `0xcb0898..0xcb08b0` and ends *exactly* where `G_STATE` begins, so `G_STATE` is a **separate** 1-byte static immediately adjacent, not a field inside `GlobalLocked`. The emit fast path reads it without taking the global lock — which would be unsound if it lived inside the lock-guarded region — confirming the split. `[HIGH]`

---

## 5. The Interner Read-Out

### Purpose

Each NC's `InternedDataShard` holds the strings interned *for that core*. At read-out, `combine_all_interned_strings` @`0x5b1cb0` merges all 256 shards into one `id → name` map for serialization. Because `get_id` is deterministic (fixed-key SipHash), a string interned independently on two cores produces the **same** id, so the merge is a deduplicating union, not a relabel.

### Algorithm — `get_id` and merge

```c
// Models string_db::get_id @0x5b4990.
function get_id(name, len):
    h = SipHash_1_3(key = "detbytesdornamodlygenerasomepseu", bytes = name ++ [0xFF])
        //  ^ FIXED 32-byte key, stored byte-reversed in .rodata ("uespemos…detbyted")
    return (h & 0x3FFFFFFFFFFFF) + 1        // mask to 50 bits, +1 => id in [1, 2^50]
                                            //   matches the 50-bit tracking_id field

// Models combine_all_interned_strings @0x5b1cb0.
function combine_all_interned_strings(out):            // out = fresh InternedDataShard
    read_lock(GLOBAL_LOCK)
    for i in 0..256:
        ctx = &CONTEXTS[i]
        read_lock(ctx.lock)
        if ctx.enable[0] != 2:                          // skip closed NCs
            for (id, name, len) in ctx.interner:        // hashbrown SSE ctrl-byte scan
                register_entry(out, id, name, len)      // idempotent: dup ids skipped
        read_unlock(ctx.lock)
    read_unlock(GLOBAL_LOCK)
```

The fixed-key/50-bit choice is what ties the interner to the event record: a `tracking_id` and a string id occupy the **same 50-bit space**, so an event's `tracking_id` can directly reference an interned name without a side table. `register_entry` skips duplicate ids, making the cross-NC merge idempotent. `[HIGH]`

---

## Related Components

| Name | Relationship |
|---|---|
| `api::fetch_events` @`0x5aa3b0` | producer (2)'s second terminal — drains rings then serde-serializes JSON ([rust-serde](rust-serde.md)) |
| `event_to_proto` @`0x9aec0` | the C++ join seam that maps `Event` → `ntff::ntrace_event` for the NTFF path |
| `nrt_sys_trace_capture_*` @`0x509680..0x509980` | the cbindgen C-ABI shims that are the only callers ([rust-ffi](rust-ffi.md)) |
| `Config::from` @`0x5b2110` | marshals `nrt_sys_trace_config_t` (312 B) → Rust `Config`, clamps ring size |
| `InternedDataShard` / `string_db` | the deterministic interner shared with the read-out path ([interned-strings](../runtime/interned-strings.md)) |

## Cross-References

- [Overview: the Three Trace Producers](overview.md) — where this engine sits as producer (2); the producer/consumer boundary that makes the lossy `force_push` policy sound
- [neuron_rustime: serde_json Serializer](rust-serde.md) — `api::fetch_events`, the JSON read-out terminal that drains the rings this engine fills
- [SysTraceEventType Taxonomy (46 Variants)](event-taxonomy.md) — the 46-variant `event_type` enum and the 40-byte `data` union packed into each 112-byte `Event`
- [Interned String Database](../runtime/interned-strings.md) — the `string_db` / `InternedDataShard` SipHash interner and its hashbrown SwissTable
- [back to index](../index.md)
