# The Neuron DataStore — Section Map

> **Binaries:** `libnrt.so.2.31.24.0` (package `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce`, ELF64, non-stripped, DWARF-v4; `libnds.a` statically linked in, and shipped standalone at `opt/aws/neuron/lib/libnds.a` — 6 TUs, DWARF present) · GPL kernel slab side from `aws-neuronx-dkms_2.27.4.0` (`neuron_ds.c` / `neuron_ds.h`, unstripped C source, cited `file:line`). The kernel↔userspace layout contract lives in the *shared* header `share/neuron_driver_shared.h` (L260-432).
> **Status:** Reimplementation-grade map · **Evidence grade:** Confirmed (byte-anchored) — the full 67 200-byte region map is a 3-way agreement (DWARF `ptype /o` struct offsets · `share/neuron_driver_shared.h` macro algebra · disassembled `lea`/`imul` constants in the accessors), and reconciles byte-for-byte with the kernel slab description. · **Part XIV — The Neuron DataStore (NDS)** · [back to index](../index.md)

## Abstract

The **Neuron DataStore (NDS)** is a process-shared, `mmap`-backed counter plane: a single fixed-layout shared-memory blob, one per `(NeuronDevice, PID)`, into which the runtime publishes per-NeuronDevice / per-NeuronCore atomic counters, per-model node metadata, and a small set of generic process-info records, so that *other* processes — `neuron-monitor`, `neuron-top`, `dlr_model` — can observe a running inference process's state without any IPC round-trip. The mental model is a `/proc`-style telemetry surface fused with a shared-memory ring: the producer writes through to the mapping with x86 `LOCK`-prefixed atomics; readers attach the *same physical pages* read-only and snapshot them. There is no socket, no syscall on the hot path, and no shared mutex across the process boundary.

Two halves cooperate. The **kernel** (`neuron_ds.c`, GPL) owns the *backing store*: at device probe it pre-allocates a fixed array of sixteen 256 KiB host-DRAM slabs (`NEURON_DATASTORE_SIZE = 262144`, `neuron_ds.h:17`; `NEURON_MAX_PROCESS_PER_DEVICE = 16`, `neuron_ds.h:18`), hands a process its slab via `mmap` on `NEURON_IOCTL_ACQUIRE_NEURON_DS` (ioctl #71), and never writes a byte of content beyond `memset(0)` — it only *reads* counters back during metrics aggregation. The **userspace** half (`libnds.a` + the in-`libnrt.so` `nds_*` band, `.text 0x507070..0x507cc0`) owns the *blob*: it stamps the `"nds\0"` + version-100000 header, lays out the 67 200-byte region map inside the slab, and runs the read/write accessors. The boundary is a single mapping primitive — `ndl_nds_open`/`ndl_nds_close` (`0xc4110`/`0xc41b0` in `libnrt.so`) — the NDL layer owns the *mapping*; this section owns the *blob*.

The counterintuitive core, the thing a reimplementer must get exactly right, is the **cross-process synchronization protocol**: it is **not** a seqlock and **not** a shared mutex. Self-describing object records (process-info, process-info-ext, the 64 model entries) each carry a leading 32-bit **FNV-1a hash**; the producer recomputes the hash over the record (hash field zeroed), then `memcpy`s the record into shared memory; a reader copies it out, re-zeroes the hash, recomputes, and on mismatch (a torn read) **`usleep`s and retries exactly once**. The plain counter arrays (ND/NC/EXT) carry no hash at all — they are updated with `LOCK ADD`/`SUB`/`OR` and read with a plain 8-byte load, relying on natural 64-bit atomicity. This page maps the four NDS sub-pages onto that split; it does not re-derive their byte-level layouts.

For reimplementation, the contract of the NDS plane as observed in the binaries is:

- **The 67 200-byte region map** (`NDS_REQUIRED_SIZE = 0x10680`) inside the 256 KiB slab: header → ND counters → NC counters → process-info → model bitmap → 64 model records → process-info-ext → 64-byte-aligned EXT NC counter sections. Every start/size/stride is fixed and shared between kernel and userspace via `share/neuron_driver_shared.h`.
- **The single-owner-by-PID model**, not a refcount: `pid == 0` on open means producer/RW (slab for the calling TGID); `pid != 0` means reader/RO (attach to another process's slab). The kernel guards release by exact `task_tgid_nr(current)` match.
- **The hash-stamp / hash-verify / one-retry protocol** for self-describing records, and the `LOCK`-atomic, hash-free protocol for counters. Getting the *which-records-have-a-hash* split wrong is the trap.
- **The two-tier model-record machinery**: a 64-slot occupancy bitmap (`u64` at `+0x520`), first-clear-bit allocation under a *same-process* `pthread_mutex`, and a 672-byte record per slot at `+0x528 + slot*672`.

## At a glance

| Aspect | Kernel side (backing store) | Userspace side (the blob) |
|---|---|---|
| Owning TU | `neuron_ds.c` / `neuron_ds.h` (GPL, dkms 2.27.4.0) | `libnds.a` 6 TUs + `libnrt.so` `nds_*` band |
| Entry surface | `neuron_ds_acquire_pid` (`neuron_ds.c:126`); ioctl #71/#72 | `nds_open` @`0x5070d0`, `nds_close` @`0x5071b0` |
| Mapping primitive | `nmmap_offset(mc)` → `remap_pfn_range` | `ndl_nds_open`/`ndl_nds_close` @`0xc4110`/`0xc41b0` |
| What it writes | `memset(0)` only — *never* the magic/version | `"nds\0"`+100000 header, region map, all records |
| Slab size | `NEURON_DATASTORE_SIZE = 262144` (0x40000) | requires mapped size `> 0x1067f` (≥ `NDS_REQUIRED_SIZE`) |
| Defined map size | — (kernel treats `1248..44588` gap as opaque) | `NDS_REQUIRED_SIZE = 67200` (0x10680) |
| Sync model | one `nds->lock` mutex over the 16-slot array; owner-by-PID | FNV-1a hash + 1× `usleep` retry (records); `LOCK` atomics (counters) |
| Header magic | `"nds\0"` + `0x000186a0` (100000), written by **userspace only** | `nds_commit_header` @`0x507070` (single 8-byte store) |
| Counter geometry | `get_neuroncore_counter_value` 3-section dispatch (`neuron_ds.c:207`) | `nds_increment_nc_counter` @`0x507220` (same 3 bands) |

---

## 1. The two halves and the boundary

The NDS is realized by two cooperating subsystems that share the same physical host-DRAM pages but split responsibility cleanly: the kernel owns *who gets which slab and when it is freed*; userspace owns *what bytes live in the slab*.

### 1.1 Kernel: the per-process slab array

The kernel datastore is a fixed array of sixteen pre-allocated 256 KiB host-DRAM slabs, one array per `neuron_device` (`struct neuron_datastore` embedded in `neuron_device` as field `datastore`). At device probe `neuron_ds_init` (`neuron_ds.c:14`, called from `neuron_pci.c:442`) allocates all 16 chunks up front (`mc_alloc_align`, `MC_LIFESPAN_DEVICE`, `MEM_LOC_HOST`, `NEURON_MEMALLOC_TYPE_NCDEV_HOST`) and `memset`s each to zero — reserving `16 × 256 KiB = 4 MiB` of host DRAM per device that is never grown or shrunk thereafter.

A process claims a slab through `neuron_ds_acquire_pid` (`neuron_ds.c:126`), reached from `ncdev_acquire_neuron_ds` (`cdev.c:2076`) on `NEURON_IOCTL_ACQUIRE_NEURON_DS` (#71, `ioctl.h:758`):

- `pid == 0` (an inference app claiming its own slab) → `neuron_ds_create_and_acquire_pid(task_tgid_nr(current))` → if no slot exists yet, `neuron_ds_add_pid` picks an empty slot via the LRU `neuron_ds_find_empty_slot` (`neuron_ds.c:70`) and stamps `entry->pid`.
- `pid != 0` (a monitor attaching to a target) → `neuron_ds_acquire_existing_pid`; `-ENOENT` if that PID owns no slab.

The handler returns `{mmap_offset = nmmap_offset(mc), size = mc->size = 262144}` (`cdev.c:2092-2093`); userspace then `mmap`s the offset to obtain the slab VA. Slot reclamation is **owner-guarded and LRU-timestamped**, not refcounted: `neuron_ds_release_entry` (`neuron_ds.c:155`) only acts if `entry->pid == task_tgid_nr(current)`, then rolls the dying process's counters into device-level metrics (`nmetric_partial_aggregate`, `nsysfsmetric_nds_aggregate`) and `neuron_ds_clear_entry` zeroes the slab and bumps the global `ds_entry_clear_counter` LRU tick (`neuron_ds.c:148-152`).

> **CORRECTION (K-DS R1) —** the kernel header doc comments (`neuron_ds.h:56,68`) describe acquire/release as "increase/decrease ref count, deallocate if 0." The code has **no refcount field**: acquire merely stamps `entry->pid`; release fires only for the owning TGID and immediately zeroes the slab. Semantics are single-owner-by-PID. A reimplementation that builds a refcount here will diverge from the binary. (CONFIDENCE HIGH — `grep` finds no refcount in any `.c`.)

> **NOTE —** the kernel **never** writes the `"nds\0"` magic or the version word — only `memset(0)`. The header is a userspace responsibility, written by `nds_commit_header` after `mmap`. The kernel only *reads* counter words back through the `NDS_*` offset macros during aggregation (`neuron_ds.c:207`). Confirmed: no signature store exists in any kernel `.c` (K-DS R2, CONFIDENCE HIGH).

### 1.2 Userspace: the blob and its accessors

The userspace half is `libnds.a` (6 TUs under `KaenaRuntime/nds/`: `neuron_ds.c`, `neuron_ds_counters.c`, `neuron_ds_helpers.c`, `neuron_ds_models.c`, `neuron_ds_object.c`, `neuron_ds_process.c`), statically linked into `libnrt.so` where the same code occupies the `.text 0x507070..0x507cc0` band (24 functions, all ELF `LOCAL FUNC` — internal plumbing, *not* NRT public API). It allocates a 72-byte host-only `nds_instance_t` handle (`calloc 0x48` in `nds_open` @`0x5070d0`), records `{pid, device, nds_size, nds_ptr, pthread_mutex_t}`, and gates every access on `pid == 0` (producer) vs `pid != 0` (reader).

The producer path: `nds_open(device, pid=0)` → `ndl_nds_open` (kernel slab) → size gate `> 0x1067f` → `pthread_mutex_init` → `nds_commit_header` writes the 8-byte header. The reader path: `nds_open(device, pid=TARGET)` → `ndl_nds_open(TARGET)` → `nds_check_header` validates the magic, retrying once after `usleep(500)` if the producer has not committed it yet (`-EINVAL` if still wrong). All writers are read-only-gated: `nds_obj_commit` (`0x507cd0`) and every counter increment reject when `nds_is_read_only` (i.e. `inst->pid != 0`).

### 1.3 The boundary: NDL owns the mapping

The single edge between the two halves is the NDL (Neuron Driver Layer) mapping primitive `ndl_nds_open`/`ndl_nds_close` (`0xc4110`/`0xc41b0` in `libnrt.so`) — the ioctl(#71/#72) + `mmap` wrapper that acquires the kernel per-PID slab and returns `{base ptr, size}`. **NDL owns the *mapping*; this section owns the *blob*** that lives inside it. The kernel and userspace agree on the byte layout solely through the shared header `share/neuron_driver_shared.h` (L382-430) — the `nds_header_t` definition and the `NDS_*` offset macros are compiled into both sides.

```text
  PRODUCER PROCESS (inference app, e.g. libtorchneuron)         READER PROCESSES (neuron-monitor / neuron-top / dlr_model)
  ┌─────────────────────────────────────────────┐              ┌──────────────────────────────────────────┐
  │ libnrt.so  nds_open(dev, pid=0)  ── RW owner │              │ libnds.a  nds_open(dev, pid=TARGET) ── RO  │
  │   nds_commit_header  "nds\0"+100000          │              │   nds_check_header (usleep 500 ×1)         │
  │   nds_increment_nc_counter  LOCK ADD ───┐    │              │   nds_get_nc_counter   plain u64 load ◄─┐  │
  │   nds_commit_model_node_info  hash+publish│   │             │   nds_read_all_model_nodes  hash-verify │  │
  └──────────────────┬───────────────────────│───┘             └───────────────┬──────────────────────────┼──┘
                     │ ndl_nds_open (ioctl #71 + mmap)                          │ ndl_nds_open(TARGET) (ioctl #71 RO)
                     ▼                       │                                  ▼                          │
  ╔══════════════════════════════════════════│══════════════════════════════════════════════════════════ │═╗
  ║ KERNEL  neuron_ds.c                       │                                                            │ ║
  ║   neuron_ds_acquire_pid                    └──────────►  SAME PHYSICAL 256 KiB SLAB  ◄─────────────────┘ ║
  ║     pid==0: create_and_acquire (own slab)               (one of 16 per device; remap_pfn_range)         ║
  ║     pid!=0: acquire_existing  (-ENOENT if none)         kernel only memset(0)s + reads counters back    ║
  ║   16 × 256 KiB pre-allocated at probe (4 MiB)                                                           ║
  ╚════════════════════════════════════════════════════════════════════════════════════════════════════════╝
        within the slab, the userspace blob lays out NDS_REQUIRED_SIZE = 67200 B (see §3) ; ~195 KiB tail unused
```

---

## 2. The torn-read safety model

This is the protocol a reimplementer is most likely to get wrong, because the obvious assumption — "shared memory needs a lock or a seqlock" — is false here. NDS publishes self-describing records with an embedded checksum and relies on a single bounded retry to detect (not prevent) a torn read.

> **QUIRK —** there is **no seqlock and no cross-process mutex** in the NDS sync path. The per-instance `pthread_mutex_t` (`nds_instance_t+0x20`) is process-local — it only serializes *same-process threads* during model-slot allocation. Cross-process consistency rests entirely on a per-record FNV-1a-32 hash plus a one-shot `usleep` retry. A reader that sees a half-written record detects it by hash mismatch and re-reads once; if the producer is mid-write across both attempts, the reader returns the stale-but-consistent prior record, never a torn one. Plain counters get *no* hash — they lean on natural 64-bit load/store atomicity and `LOCK`-prefixed RMW.

### 2.1 The hash: FNV-1a-32

The checksum is canonical 32-bit FNV-1a, computed in `nds_hash` (`neuron_ds_helpers.c:24`, `0x5080c0` in `libnrt.so`; basis `0x811c9dc5`, prime `0x01000193` confirmed in the disassembly at `nds_hash+0x03`/`+0x29`). The record's own hash field is the *first* `u32` of the record and is zeroed before hashing:

```c
// nds_hash(ptr, len)            // neuron_ds_helpers.c:24 / libnrt.so 0x5080c0
uint32_t nds_hash(const void *ptr, size_t len):
    uint32_t h = 0x811c9dc5;                 // FNV-1a offset basis (nds_hash+0x03)
    for (size_t i = 0; i < len; i++):
        h ^= ((const uint8_t*)ptr)[i];
        h *= 0x01000193;                     // FNV prime (imul, nds_hash+0x29)
    return h;
```

### 2.2 Producer stamp, reader verify, one retry

The producer builds the record in a private staging buffer, zeroes the hash field, computes the hash over the whole record, writes it into field[0], then `memcpy`s the staging buffer into the shared mapping (hash word first, then payload via `rep movsq`). The reader inverts it and retries exactly once on a torn or uninitialized read:

```c
// reader side: nds_read_generic_obj  (neuron_ds_object.c, 0x507ad0 commit twin)
//   record_len = 64 (process_info @ +0x4e0)  |  260 (process_info_ext @ +0xad28)
//   model records use the same shape with len = 672 (nds_read_model_node_info_data @0x507890)
for (attempt = 0; attempt < 2; attempt++):          // at most TWO reads
    memcpy(local, base + region_start, record_len);  // snapshot the shared record
    if (local.hash == 0):                            // producer hasn't published yet
        usleep(200); continue;                       // 0xc8 — uninitialized retry
    stored = local.hash;
    local.hash = 0;
    if (nds_hash(local, record_len) != stored):      // torn write detected
        usleep(200); continue;                       // 0xc8 — torn-read retry
    return publish(local);                            // consistent snapshot
// fall through after 2 attempts: caller sees the last (possibly stale) copy
```

> **GOTCHA —** the retry budget is **one** (`attempt < 2` → two reads total), and the back-off windows are fixed: `usleep(500)` for the *header* check (`nds_check_header`, `0x1f4`) and `usleep(200)` for *record* reads (`0xc8`). The header check guards a single 8-byte compare against `"nds\0"`+100000; the record checks guard the FNV hash. A reimplementation that loops unboundedly, or that uses one window for both, does not match the binary — and an unbounded loop can spin against a producer stuck mid-`memcpy`. The bounded retry deliberately *tolerates* staleness rather than blocking. (CONFIDENCE HIGH — full disassembly of `nds_commit_generic_obj` / `nds_read_generic_obj` / `nds_commit_model_node_info`.)

> **NOTE (memory ordering, MEDIUM) —** the producer publishes with `memcpy`/`rep movsq` and the reader does plain `memcpy` loads; there is no explicit fence or `LOCK` on the record path (only the *counter* path uses `LOCK`). On x86-TSO the hash-after-payload store order and the reader's hash-last read order make the protocol sound in practice, but the ordering is not enforced by an architectural barrier — a strict-reimplementation on a weaker memory model would need an explicit release/acquire pair around the hash word. (This is the L-INFRA-09 D4 race-audit candidate.)

---

## 3. The region map (orientation)

The slab is laid out by userspace into a fixed `NDS_REQUIRED_SIZE = 67200`-byte (`0x10680`) map; the remaining ~195 KiB of the 256 KiB slab is unused by this layout version. The full byte-level derivation — every struct field offset, the NC-counter addressing math, the EXT-section alignment algebra — is the subject of **[The NDS Wire Format](wire-format.md)**. The map's *shape*, which orients all four sub-pages, is:

| Region | Start (hex) | Size (B) | Count × stride | Sync |
|---|---|---|---|---|
| `nds_header` | `0x000` | 8 | `"nds\0"` + `u32` version 100000 | single 8-B store |
| ND counters | `0x008` | 248 | 31 × `u64` (per-NeuronDevice) | `LOCK ADD/SUB/OR` |
| NC counters | `0x100` | 992 | 4 NC × 31 × `u64` (original) | `LOCK ADD/SUB/OR` |
| process-info | `0x4e0` | 64 | `hash(u32)` + 60-B payload | FNV-1a + retry |
| model bitmap | `0x520` | 8 | 64-slot occupancy `u64` | producer mutex |
| model entries | `0x528` | 43008 | 64 × 672-B records | FNV-1a + retry |
| process-info-ext | `0xad28` | 260 | `hash(u32)` + 256-B payload | FNV-1a + retry |
| *(pad to 64-B align `NDS_EXT_OFFSET`)* | `0xad2c` | — | end `0xae2c` → `0xda80` | — |
| EXT NC counters | `0xda80` | 2048 | 4 NC × 64 × `u64` (cores 0-3 overflow) | `LOCK ADD/SUB/OR` |
| EXT NC_DATA | `0xe280` | 9216 | 12 NC × 96 × `u64` (cores 4-15, 95+1 pad) | `LOCK ADD/SUB/OR` |
| **total** | **`0x10680`** | **67200** | = `NDS_REQUIRED_SIZE` | — |

The split between the original NC band (`0x100`, cores 0-3, counters 0-30) and the two EXT bands (`0xda80` overflow for cores 0-3's counters 31+, `0xe280` for the full 95-counter set of cores 4-15) reflects an in-place format extension: `NDS_MAX_NEURONCORE_COUNT = 4` original cores plus `NDS_EXT_MAX_NEURONCORE_COUNT = 12` extended cores = up to 16 NeuronCores, and `NDS_NC_COUNTER_COUNT = 31` original counters plus `NDS_EXT_NC_COUNTER_COUNT = 64` = `NDS_TOTAL_NC_COUNTER_COUNT = 95` per-core counters. The `NDS_EXT_OFFSET = NDS_ALIGN(44588 + 11296) = 55936` algebra (where `44588 = NDS_EXT_OFFSET_OLD` is hard-coded and `NDS_ALIGN(v) = v + (-v & 63)`) is reproduced identically by kernel and userspace — the compatibility hinge documented in **[The NDS Wire Format](wire-format.md)** and the kernel's `get_neuroncore_counter_value` 3-section dispatch.

> **QUIRK —** the kernel treats the entire `1248..44588` gap (process-info + the 64 model records + process-info-ext) as **opaque** — it never indexes into it. Those structs are written and read exclusively by userspace; the kernel reads only the counter bands. This is why the model/process-info layout lives in the userspace sub-pages, not the kernel one. (K-DS R4, CONFIDENCE HIGH.)

---

## 4. The four sub-pages

This map orients four sibling pages; each owns one slice of the NDS and carries its byte-level derivation. Read them in the order below — kernel backing → userspace accessors → wire format — to follow the data from `mmap` to published counter.

| Sub-page | Owns | Key anchors |
|---|---|---|
| **[Kernel Side (Per-Process Slabs)](kernel-side.md)** | The 16-slab array, LRU acquire/release, owner-by-PID lifecycle, ioctl #71/#72 wiring, metrics-aggregation read path | `neuron_ds_acquire_pid` (`neuron_ds.c:126`), `neuron_ds_find_empty_slot` (`:70`), `get_neuroncore_counter_value` (`:207`), `NEURON_DATASTORE_SIZE = 262144` |
| **[Userspace Side (libnds.a)](userspace-libnds.md)** | The 6-TU accessor library: open/close, the `nds_obj_*` handle lifecycle, counter inc/dec/get/set, model-slot allocation, the 64-slot bitmap helpers | `nds_open` @`0x5070d0`, `nds_increment_nc_counter` @`0x507220`, `nds_commit_model_node_info` @`0x5076c0`, `nds_get_empty_map_slot` @`0x508100` |
| **[The NDS Wire Format](wire-format.md)** | The byte-exact region map, every wire struct (`nds_model_node_data_internal` 672 B, `nds_process_info_internal` 64 B, ext 260 B), NC-counter addressing math, EXT alignment algebra, the header magic | region table §3, `share/neuron_driver_shared.h` L382-430, `NDS_REQUIRED_SIZE = 0x10680`, 3-section NC addressing |
| *(this page)* **Overview** | The kernel/userspace split, the torn-read sync model, and the cross-page orientation | `ndl_nds_open` @`0xc4110`, FNV-1a `nds_hash` @`0x5080c0`, `usleep(500/200)` retry windows |

---

## 5. Object and counter taxonomy

NDS carries three kinds of payload, distinguished by *how they are synchronized* — the single most important axis for a reimplementer, because it dictates whether a write needs a hash stamp or a `LOCK` prefix.

| Payload class | Records | Sync mechanism | Producer entry | Reader entry |
|---|---|---|---|---|
| Plain counters | ND (31) + NC (4×31) + EXT (4×64, 12×96) `u64` | `LOCK ADD/SUB/OR`; plain `u64` load | `nds_increment_nc_counter` @`0x507220`, `nds_increment_nd_counter` @`0x5074e0`, `nds_or_nd_counter` @`0x507560` | `nds_get_nc_counter` @`0x507380`, kernel `get_neuroncore_counter_value` |
| Generic objects | process-info (64 B), process-info-ext (260 B) | FNV-1a hash + 1× `usleep(200)` retry | `nds_commit_generic_obj` @`0x507ad0` | `nds_read_generic_obj` |
| Model nodes | 64 × 672-B records + 64-slot bitmap | FNV-1a hash + retry; bitmap alloc under process-local mutex | `nds_commit_model_node_info` @`0x5076c0` | `nds_read_all_model_nodes` @`0x5079e0` |

The object handles carry a 1-byte type tag: `OBJECT_TYPE_MODEL_NODE_INFO = 0`, `OBJECT_TYPE_PROCESS_INFO = 1`, `OBJECT_TYPE_PROCESS_INFO_EXT = 2` (public header `neuron_ds.h`), dispatched by the `nds_obj_commit`/`nds_obj_delete` jump tables. `nds_obj_new` (`0x507d90`, out of the L-INFRA-09 cell) `calloc`s the host wrapper by type (`0 → 712`, `1 → 88`, `2 → 280` bytes). The counter *semantics* — `INFER_COMPLETED`, `MAC_COUNT`, `LATENCY_TOTAL`, the `MEM_USAGE_*` family, the ND `FEATURE_BITMAP` written via `nds_or_nd_counter` — are enumerated in the kernel enum (`share/neuron_driver_shared.h` L272-378) and re-exposed by `neuron-monitor`; the per-index table is owned by **[The NDS Wire Format](wire-format.md)** and **[Kernel Side](kernel-side.md)**.

> **NOTE —** the producer call sites that *bump* each counter live in the runtime hot path, not in the NDS cell: `exec_request_progress_one_step` → `nds_increment_nc_counter`, `tdrv_update_ds_mem_usage` → `nds_{inc,dec}_nc_counter`, `kmetric_update_nds_exec_latencies` → `nds_set_nc_counter`, `tdrv_set_feature_bitmap` → `nds_or_nd_counter`. These are documented on the execution and telemetry pages; here they are boundary in-edges only.

---

## 6. Verification notes

> The kernel/userspace split, the region map, and the torn-read protocol were cross-checked across three independent evidence sources:
> - **Region map** — 3-way agreement: DWARF `ptype /o` struct offsets, the `share/neuron_driver_shared.h` macro algebra (evaluated arithmetically), and disassembled `lea`/`imul` constants in the accessors all agree on `0x100, 0x4e0, 0x520, 0x528, 0xad28, 0xda80, 0xe280, 0x10680`. The userspace map reconciles byte-for-byte with the kernel's `neuron_ds.c:207` 3-section description and the `EXT_OFFSET` algebra at `share/neuron_driver_shared.h:407-417`.
> - **Header magic** — `.data` global at `libnrt.so 0xc0c820` reads `6e 64 73 00 a0 86 01 00` (`"nds\0"` + `0x000186a0` = 100000), written by `nds_commit_header` @`0x507070` as one 8-byte store and validated by `nds_check_header` @`0x507080`.
> - **FNV-1a identity** — `nds_hash` @`0x5080c0` disassembly: basis `0x811c9dc5` (`= 2166136261`), prime `0x01000193` (`= 16777619`), matching the canonical 32-bit FNV-1a; called with `len = 0x2a0` (672) on model commit/read and `64`/`260` on the generic-object path.
> - **Single-owner-by-PID** — `neuron_ds_release_entry` (`neuron_ds.c:155`) `entry->pid == task_tgid_nr(current)` owner guard; `neuron_ds_acquire_existing_pid` `-ENOENT` reader path; no refcount field in any kernel `.c`.
>
> **[MEDIUM]** The *meaning* of each NC counter band beyond the addressing math (which physical-NC group maps to which engine) is not labeled in DWARF; the index→name binding is read from the kernel enum (`share/neuron_driver_shared.h` L272-378, HIGH for the enum, MEDIUM only for "the runtime never reorders the members"). The record-path memory ordering (§2.3 note) is sound on x86-TSO but not enforced by an architectural fence — flagged for the race-audit deep-dive.

---

## Cross-References

**NDS sub-pages (Part XIV)**
- [Kernel Side (Per-Process Slabs)](kernel-side.md) — the 16-slab array, LRU acquire/release, owner-by-PID lifecycle, the metrics-aggregation read path
- [Userspace Side (libnds.a)](userspace-libnds.md) — the 6-TU accessor library, the `nds_obj_*` handle lifecycle, counter and model-record accessors
- [The NDS Wire Format](wire-format.md) — the byte-exact 67 200-B region map, every wire struct, the NC-counter addressing math and EXT alignment algebra

**Kernel driver (Part III)**
- [Kernel Datastore](../kernel/datastore.md) — the GPL `neuron_ds.c` map this page's §1.1 summarizes
- [Metrics Aggregation](../kernel/metrics.md) — `nmetric_partial_aggregate` / full-tick aggregation that reads the slab counters back
- [Sysfs Metrics Tree](../kernel/sysfs.md) — `nsysfsmetric_nds_aggregate`, the sysfs surface fed by NDS counters
- [Char Device, fops and mmap](../kernel/cdev-mmap.md) — `ncdev_acquire_neuron_ds`, `nmmap_offset`, the `remap_pfn_range` that backs `nds_ptr`
- [IOCTL Catalog](../kernel/ioctl-catalog.md) — `NEURON_IOCTL_ACQUIRE_NEURON_DS` (#71) / `RELEASE` (#72)

**Userspace runtime (Part IV)**
- [NDL: Neuron Driver Layer (IOCTL/mmap Wrappers)](../runtime/ndl.md) — `ndl_nds_open`/`ndl_nds_close`, the mapping primitive this section sits on top of
- [TDRV: Device Bring-Up and Lifecycle](../runtime/tdrv-lifecycle.md) — `tdrv_init_nds_for_device` / `tdrv_close_nds_for_device`, the producer open/close call sites

**Trace & telemetry consumers (Part XIII)**
- [The System Monitor and Debug Stream](../trace/system-monitor.md) — `neuron-monitor`-class readers that attach RO to another PID's slab
- [Telemetry, Metrics & Error Reporting](../trace/telemetry-errors.md) — how published NDS counters surface to telemetry consumers

**Reference**
- [Binary Layout](../reference/binary-layout.md) — the `libnds.a` 6-TU breakdown and the NDS wire-struct table
- [back to index](../index.md)
