# The NDS Wire Format

> *Userspace addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (ELF64, non-stripped, DWARF-v4; `libnds.a` — 6 TUs under `KaenaRuntime/nds/` — statically linked, occupying the `.text 0x507070..0x508160` band; `.text`/`.rodata`/`.data` VMA == file offset, so every `0x50…`/`0xc0…` is both an analysis VMA and a file offset). The kernel↔userspace layout contract is the shared header `share/neuron_driver_shared.h` (GPL-2.0, `aws-neuronx-dkms 2.27.4.0`), cited `file:line`; struct offsets tagged "DWARF" are from `gdb`-`ptype /o` on the `libnds.a` debug info.*
> *Evidence grade: **Confirmed (byte-anchored)** — the full 67 200-byte region map is a 3-way agreement: DWARF `ptype /o` struct offsets · `share/neuron_driver_shared.h` macro algebra (evaluated arithmetically) · disassembled `lea`/`imul`/`cmp` constants in the accessors. Other versions will differ. · Part XIV — The Neuron DataStore (NDS) · [back to index](../index.md)*

## Abstract

The **Neuron DataStore (NDS)** is a versioned shared-memory ABI: a single fixed-layout blob, one per `(NeuronDevice, PID)`, that the kernel backs and the runtime fills, and that *both sides* must agree on byte-for-byte. It is the lowest-level data structure in the telemetry plane — the kernel `memset`s and reads it back during metrics aggregation; userspace stamps it, lays out its regions, and publishes counters and records into it; monitoring tools (`neuron-monitor`, `neuron-top`) attach the *same physical pages* read-only and snapshot them. The mental model is a `/proc`-style telemetry surface with a frozen on-disk schema: there is no socket and no serialization step — the in-memory C structs *are* the wire format, and a reader in another process re-derives every field from the same offset macros the producer used.

The blob occupies the head of a kernel-allocated 256 KiB slab (`NEURON_DATASTORE_SIZE = 262144`, `neuron_ds.h:17`); the runtime maps the slab and writes a fixed **`NDS_REQUIRED_SIZE = 67200`-byte (`0x10680`)** region map into it, leaving the ~195 KiB tail unused by this layout version. The map is **not** a flat array — it is eight regions with hard-coded starts, threaded together by a chain of `#define`s in the shared header (`L392-430`) that the kernel and userspace both `#include`. Two of those regions carry an in-place format extension (the EXT bands, for NeuronCores 4-15 and for counter indices ≥ 31 on cores 0-3) whose start is computed by a `NDS_ALIGN` rounding step around a *hard-coded* legacy constant `NDS_EXT_OFFSET_OLD = 44588` — the compatibility hinge a reimplementer most easily gets wrong.

Three of the regions hold self-describing **object records** (process-info, process-info-ext, and the 64 model entries); each begins with a 32-bit **FNV-1a-32** hash over the record body (with the hash word zeroed), and that hash *is* the cross-process synchronization protocol — there is no seqlock and no shared mutex on the record path. The remaining regions are plain **`u64` counter arrays** (per-NeuronDevice, per-NeuronCore, and the two EXT bands) updated with `LOCK`-prefixed atomics and read with a plain 8-byte load; they carry no hash. This page is the byte-exact reference: every region start, every struct field offset, the NC-counter addressing math, the EXT-alignment algebra, the header proto, the 12-slot DMA mem-usage record, and the record-hash scheme. The architecture prose — the kernel/userspace split and the torn-read safety model — lives in **[the NDS overview](overview.md)**; this page is the layout that page points to.

For reimplementation, the wire-format contract is:

- **The 67 200-byte region map** (`NDS_REQUIRED_SIZE = 0x10680`): header → ND counters → NC counters → process-info → 64-slot model bitmap → 64 model records → process-info-ext → (pad) → EXT NC counters → EXT NC-data. Every start, size, count, and stride is fixed and shared via `share/neuron_driver_shared.h`.
- **The header proto** — `"nds\0"` + `int version = 100000`, the single `u64 0x000186a06e6473` written by the producer and compared by readers, the gate that proves the slab is a committed NDS.
- **Every wire struct, field-by-field at a verbatim offset** — the 672-byte model record, the 64-byte process-info record, the 260-byte process-info-ext record, and the 16-byte `nds_mem_usage_info` × 24 grid inside each model record.
- **The 3-band NC-counter addressing math** — `idx = 32 + pnc*31 + ctr` (cores 0-3, ctr ≤ 30), `idx = 6992 + pnc*64 + (ctr-31)` (cores 0-3, ctr ≥ 31), `idx = 7248 + (pnc-4)*96 + ctr` (cores 4-15) — and the `NDS_ALIGN(44588 + 11296) = 55936` derivation behind it.
- **The FNV-1a-32 record-hash scheme** — basis `0x811c9dc5`, prime `0x01000193`, hash field at record `+0`, zeroed-before-hash, byte-range over the whole record, with the one-`usleep`-retry verify on the read side.

| | |
|---|---|
| **Blob size** | `NDS_REQUIRED_SIZE = 67200` (`0x10680`) inside a `262144`-byte (`0x40000`) slab |
| **Map definition** | `share/neuron_driver_shared.h:392-430` (kernel + userspace `#include`) |
| **Header proto** | `"nds\0"` + `0x000186a0` (100000) = `u64 0x000186a06e6473`; `.data` `header_proto` @`0xc0c820` |
| **Open size gate** | mapped size `> 0x1067f` (≥ `0x10680`), `nds_open` @`0x5070d0` |
| **Record hash** | FNV-1a-32, basis `0x811c9dc5`, prime `0x01000193`; `nds_hash` @`0x5080c0` |
| **Model record** | `nds_model_node_data_internal`, **672 B** (`0x2a0`), 64 slots @`0x528 + slot*672` |
| **Process record** | `nds_process_info_internal`, **64 B** @`0x4e0`; `_ext`, **260 B** @`0xad28` |
| **Counter geometry** | ND 31 @`0x008` · NC 4×31 @`0x100` · EXT 4×64 @`0xda80` · EXT-data 12×96 @`0xe280` |
| **Byte-agreement** | kernel · userspace · shared header all author the identical layout (§9) |

---

## 1. The Region Map

### Purpose

The map is the spine of the whole format: every other table on this page is one of its rows expanded. It is authored once, as a chain of `#define`s in the shared header, where each region's `_START` is the previous region's `_START + _SIZE`. Because both the kernel and the userspace `libnds.a` `#include` that header, the two code lines compute identical byte offsets without ever exchanging a layout descriptor — the agreement is structural, not negotiated at runtime.

The eight content regions, with the inter-region pad, total exactly `NDS_REQUIRED_SIZE = 67200` bytes. `nds_open` (@`0x5070d0`) refuses any mapping whose size is `≤ 0x1067f` (i.e. it requires `≥ 0x10680`), so a conforming slab always contains the whole map.

### The map

Every start/size is a 3-way agreement (shared-header macro algebra · DWARF struct offset · disassembled accessor constant). The `Confidence` column flags the few rows where the *meaning* is softer than the *offset*.

| Region | Start (dec / hex) | Size (B) | Count × stride | Meaning | Confidence |
|---|---|---|---|---|---|
| `nds_header` | 0 / `0x000` | 8 | `nds_header_t` | `"nds\0"` + `u32` version 100000 | CERTAIN |
| ND counters | 8 / `0x008` | 248 | 31 × `u64` | per-NeuronDevice counters (`NDS_ND_COUNTER_COUNT`) | CERTAIN |
| NC counters | 256 / `0x100` | 992 | 4 NC × 31 × `u64` | original per-NeuronCore counters, cores 0-3, ctr 0-30 | CERTAIN |
| process-info | 1248 / `0x4e0` | 64 | `nds_process_info_internal` | `hash(u32)` + 60-B process payload | CERTAIN |
| model bitmap | 1312 / `0x520` | 8 | `u64` occupancy bitmap | 64-slot first-clear-bit allocator | CERTAIN |
| model entries | 1320 / `0x528` | 43008 | 64 × 672 B | `nds_model_node_data_internal[64]` | CERTAIN |
| process-info-ext | 44328 / `0xad28` | 260 | `nds_process_info_ext_internal` | `hash(u32)` + 256-B tag payload | CERTAIN |
| *(pad to 64-B align)* | 44588 / `0xae2c` | 1348 | — | `NDS_EXT_OFFSET_OLD` (44588) → `NDS_EXT_OFFSET` (55936) | HIGH |
| EXT NC counters | 55936 / `0xda80` | 2048 | 4 NC × 64 × `u64` | overflow counters (ctr ≥ 31) for cores 0-3 | CERTAIN |
| EXT NC-data | 57984 / `0xe280` | 9216 | 12 NC × 96 × `u64` | full 95-counter set (+1 pad) for cores 4-15 | CERTAIN |
| **total** | **67200 / `0x10680`** | — | = `NDS_REQUIRED_SIZE` | end of the defined map | CERTAIN |

The slab is `0x40000` = 262144 bytes; the map ends at `0x10680` = 67200; the `0x10680..0x40000` tail (≈ 195 KiB) is untouched by this layout version (HIGH that `map ≤ slab`; MEDIUM that the tail is *unused* by any future producer).

### The macro chain (shared-header derivation)

The offsets above are not magic numbers — they are the evaluated `#define` chain from `share/neuron_driver_shared.h`. The forward (non-EXT) regions are a simple running sum:

```c
// share/neuron_driver_shared.h:392-403  — forward region chain
NDS_HEADER_START            = 0
NDS_HEADER_SIZE             = sizeof(nds_header_t)          // = 8  (4 + 4)
NDS_ND_COUNTERS_START       = 0 + 8                          // = 8     (0x008)
NDS_ND_COUNTERS_SIZE        = 31 * sizeof(u64)               // = 248
NDS_NEURONCORE_COUNTERS_START = 8 + 248                      // = 256   (0x100)
NDS_NEURONCORE_COUNTERS_SIZE  = 31 * 4 * sizeof(u64)         // = 992
// process-info(64) + bitmap(8) + 64*672 model records + process-info-ext(260)
// land process-info-ext's END at exactly 44588:
//   1248(0x4e0) + 64 + 8 + 43008 + 260 = 44588 = NDS_EXT_OFFSET_OLD
```

> **NOTE —** `NDS_ND_COUNTER_COUNT` and `NDS_NC_COUNTER_COUNT` are both **31**, but for different reasons: ND is `NDS_ND_COUNTER_LAST (14) + NDS_ND_COUNTER_RESERVED (17)` (`L292-294`); NC is `NDS_NC_COUNTER_LAST (31) + NDS_NC_COUNTER_RESERVED (0)` (`L352-354`). The `_RESERVED` slack is deliberate forward-compatibility headroom — new counters are appended before `_LAST`, decrementing `_RESERVED`, so the byte layout never moves (`L262-267`). A reimplementer must keep the *count* at 31 even though only 14 ND / 31 NC indices are presently named.

### The EXT alignment hinge

The two EXT bands sit *after* process-info-ext, at a 64-byte-aligned offset derived from a **hard-coded legacy constant**, not from a running sum. This is the format's compatibility seam:

```c
// share/neuron_driver_shared.h:405-417  — EXT-offset derivation
NDS_EXT_OFFSET_OLD          = 44588                          // hard-coded (== process-info-ext END)
NDS_EXT_NC_COUNTER_COUNT_OLD   = 65
NDS_TOTAL_NC_COUNTER_COUNT_OLD = 96
NDS_EXT_NEURONCORE_COUNTERS_SIZE_OLD = 65 * 4  * 8           // = 2080
NDS_EXT_NEURONCORE_NC_DATA_SIZE_OLD  = 96 * 12 * 8           // = 9216
NDS_EXT_SECTION_SIZE_OLD    = 2080 + 9216                    // = 11296
NDS_ALIGN(v)                = v + (-v & 63)                  // round up to 64
NDS_EXT_OFFSET              = NDS_ALIGN(44588 + 11296)
                           = NDS_ALIGN(55884) = 55936        // (0xda80)
```

> **GOTCHA —** the `_OLD` constants (`65`, `96`, `2080`, `9216`) are **only used to compute where the EXT section starts** — they reflect a *previous* counter geometry. The EXT section's *actual* contents use the *current* counts: `NDS_EXT_NC_COUNTER_COUNT = 64` (`L419`) and `NDS_EXT_NEURONCORE_NC_DATA_COUNT = 95 + 1 = 96` (`L426`). A reimplementer who sizes the EXT bands from the `_OLD` constants will lay out 65×4 and 96×12 instead of 64×4 and 96×12, and every counter past the first core will land at the wrong byte. The rule: use `_OLD` for the *start offset only*, current counts for the *contents*. (CONFIDENCE HIGH — both `_OLD` and current macros are present in `L407-428`; the disassembled accessor §4 reads the current 64/96 strides.)

The comment in the header states the intent verbatim (`L406`): *"hardcoded because it's easier than to move all the structs here and sizeof them"* — i.e. `NDS_EXT_OFFSET_OLD` is a frozen snapshot of `process-info-ext END` that must equal the running sum, which it does (44588 == 44588, §1.3).

The second EXT band is similarly aligned:

```c
// share/neuron_driver_shared.h:419-428
NDS_EXT_NEURONCORE_COUNTERS_START = NDS_EXT_OFFSET            // 55936 (0xda80)
NDS_EXT_NEURONCORE_COUNTERS_SIZE  = 64 * 4 * 8               // = 2048
NDS_EXT_NEURONCORE_NC_DATA_START  = NDS_ALIGN(55936 + 2048)  // = NDS_ALIGN(57984) = 57984 (0xe280)
NDS_EXT_NEURONCORE_NC_DATA_COUNT  = 95 + 1                   // = 96  (+1 = 64-B align pad per NC)
NDS_EXT_NEURONCORE_NC_DATA_SIZE   = 12 * 96 * 8              // = 9216
// END = 57984 + 9216 = 67200 = NDS_REQUIRED_SIZE (0x10680)
```

> **QUIRK —** the `+1` in `NDS_EXT_NEURONCORE_NC_DATA_COUNT = 95 + 1` is a **per-NeuronCore pad to 64-byte alignment**, not a 96th counter (`L425` comment: *"1 added as padding for 64 byte alignment per NC"*). Each of the 12 extended cores gets 95 live `u64` counters plus one dead `u64`, so the per-core stride is `96 * 8 = 768` bytes — a multiple of 64. Reading core `n`'s counter `c` as `base + 0xe280 + n*768 + c*8` is correct only because of this pad; sizing the stride at `95*8 = 760` would mis-align every core after the first.

---

## 2. The Header Proto

### Encoding

The header is the first 8 bytes of the slab and the gate that proves it is a committed NDS. It is a `struct nds_header` (shared-header `L382-385`): a 4-byte signature followed by a 4-byte `int` version.

| Field | Offset | Size | Bytes (LE) | Value | Source |
|---|---|---|---|---|---|
| `signature[4]` | `+0` | 4 | `6e 64 73 00` | `'n' 'd' 's' '\0'` | `neuron_driver_shared.h:383` |
| `version` | `+4` | 4 | `a0 86 01 00` | `100000` (`0x000186a0`) | `neuron_driver_shared.h:384` |

As a single little-endian `u64`, the committed header reads `0x000186a06e6473`. This exact 8-byte constant lives in `libnrt.so`'s `.data` as the symbol `header_proto` @`0xc0c820` (verified: the bytes at file offset `0xc0c820` are `6e 64 73 00 a0 86 01 00`, the only occurrence in the binary).

### Commit and check

The header is written and validated as a **single 8-byte store / compare** — there is no field-wise parsing. The producer:

```c
// nds_commit_header — libnrt.so 0x507070
function nds_commit_header(inst):
    *(u64*)inst->nds_ptr = *(u64*)&header_proto;   // one 64-bit store
// disasm: mov 0x18(%rdi),%rax ; mov header_proto(%rip),%rdx ; mov %rdx,(%rax) ; ret
```

The reader validates with the inverse compare, retrying once after a `usleep(500)` if the producer has not committed yet:

```c
// nds_check_header — libnrt.so 0x507080
function nds_check_header(inst):
    if *(u64*)inst->nds_ptr == header_proto: return 0
    usleep(500)                                    // 0x1f4 — producer may be mid-init
    if *(u64*)inst->nds_ptr == header_proto: return 0
    return -EINVAL                                 // 0xffffffea
```

> **NOTE —** the **kernel never writes this header** — it only `memset(0)`s the slab. The `"nds\0"` magic and version are a userspace responsibility, stamped by `nds_commit_header` after `mmap`; a freshly acquired slab reads `0x0000000000000000` at `+0` until the producer commits. This is why the reader retries: it may attach in the window between `mmap` and commit. (CONFIDENCE HIGH — no signature store exists in any kernel `.c`; the only writer is `nds_commit_header`.)

> **GOTCHA —** `version` is a plain `int`, **not** the runtime/FAL/framework versions that also live in the blob (those are `nds_version_info` records inside process-info, §6). `version = 100000` is the *format* version of the layout itself; it gates nothing at runtime in this build (no migration branch keys off it — see [the overview](overview.md) §3 D4 candidate), but a reader should treat a mismatched version word as "unknown layout" and refuse to parse the regions below.

---

## 3. The Counter Arrays

### Purpose

The ND and NC counter regions are plain arrays of naturally-aligned `u64`s, updated with `LOCK ADD`/`SUB`/`OR` and read with a plain 8-byte load. They carry **no hash** — they rely on the architectural atomicity of an aligned 64-bit access. Their *index→meaning* binding is the kernel counter enum (`neuron_driver_shared.h:272-378`), shared with the producer.

### ND counters — `0x008`, 31 × `u64`

The per-NeuronDevice band is 31 `u64`s at `base + 0x008` (index `i` at `base + (i+1)*8`, the `+1` skipping the 8-byte header). The first 14 indices are named; indices 14-30 are `NDS_ND_COUNTER_RESERVED` headroom.

| Index | Enum name | Meaning | Confidence |
|---|---|---|---|
| 0 | `NDS_ND_COUNTER_RUNTIME_VERSION` | runtime version (packed) | HIGH |
| 1 | `NDS_ND_COUNTER_FRAMEWORK_VERSION` | framework version (packed) | HIGH |
| 2 | `NDS_ND_COUNTER_FAL_VERSION` | FAL version (packed) | HIGH |
| 3 | `NDS_ND_COUNTER_FEATURE_BITMAP` | feature bits, written via `nds_or_nd_counter` | HIGH |
| 4 / 5 | `NDS_ND_COUNTER_MIN_/MAX_NEFF_VERSION` | NEFF version range | HIGH |
| 6-10 | `NDS_ND_COUNTER_MEM_USAGE_{CODE,TENSORS,CONSTANTS,SCRATCHPAD,MISC}_HOST` | host mem usage by category | HIGH |
| 11 | `NDS_ND_COUNTER_DYNAMIC_SYSFS_METRIC_BITMAP` | dynamic sysfs metric mask | HIGH |
| 12 / 13 | `NDS_ND_COUNTER_DEVICE_CLUSTER_ID` / `AGG_NEFF_ID` | cluster id / aggregated NEFF id | HIGH |
| 14-30 | *(reserved)* | `NDS_ND_COUNTER_RESERVED = 17` forward-compat slots | HIGH |

> **QUIRK —** the `FEATURE_BITMAP` counter (ND index 3) is the one counter written with `OR` rather than `ADD`: `nds_or_nd_counter` (@`0x507560`) does `LOCK OR base+(i+1)*8, (1 << bit)`. The two defined feature bits are `BIT_INDEX_TEST_FEATURE = 0` and `BIT_INDEX_MULTICORE_FEATURE = 1` (public header `neuron_ds.h:96-97`). A reimplementation that adds into this slot instead of OR-ing it will corrupt the bitmap.

### NC counters — `0x100`, 4 NC × 31 × `u64`

The original NeuronCore band holds the 31 named NC counters for the first 4 cores (`NDS_MAX_NEURONCORE_COUNT = 4`), row-major by core: core `n`'s counter `c` is at `base + 0x100 + (n*31 + c)*8`. The 31 indices (`neuron_driver_shared.h:300-352`):

| Index group | Enum names | Meaning |
|---|---|---|
| 0 | `TIME_IN_USE` | wall time the NC was in use |
| 1-6 | `INFER_COMPLETED`, `…_WITH_ERR`, `…_WITH_NUM_ERR`, `INFER_TIMED_OUT`, `INFER_INCORRECT_INPUT`, `INFER_FAILED_TO_QUEUE` | inference outcome tallies |
| 7-12 | `ERR_GENERIC`, `ERR_NUMERICAL`, `ERR_MODEL`, `ERR_TRANSIENT`, `ERR_HW`, `ERR_RT` | error class tallies (**order-locked** — runtime indexes them by `error_code` offset, `L310-312`) |
| 13-15 | `LATENCY_DEVICE`, `LATENCY_TOTAL`, `NC_TIME` | latency / time accumulators |
| 16-20 | `GENERIC_FAIL`, `ERR_RESOURCE`, `ERR_RESOURCE_NC`, `ERR_INVALID`, `ERR_UNSUPPORTED_NEFF_VERSION` | newer error tallies (appended after old set, `L324-336`) |
| 21 | `CC_TIME` | collective-compute time |
| 22-26 | `MEM_USAGE_{CODE,TENSORS,CONSTANTS,SCRATCHPAD,MISC}_DEVICE` | device mem usage by category |
| 27-28 | `MODEL_LOAD_COUNT`, `INFERENCE_COUNT` | load / inference counts |
| 29 | `MAC_COUNT` | multiply-accumulate count |
| 30 | `OOB` | out-of-bounds sentinel (`NDS_NC_COUNTER_LAST = 31`) |

> **GOTCHA —** the six `ERR_*` counters at indices 7-12 are **order-locked**: the header comment (`L310-312`) states the runtime *"assumes these are offset by error code"*, i.e. it computes `index = ERR_GENERIC + error_code` rather than switching on the code. Reordering them — or inserting a counter between them — silently re-routes every device error to the wrong tally. The newer counters (16-20) were appended at the *end* precisely so old-driver/new-runtime combos write into reserved slots without disturbing this block (`L324-331`).

### EXT NC counters — `0xda80`, 4 NC × 64 × `u64`

For cores 0-3, counter indices **≥ 31** overflow into this band (the original band only holds 0-30). `NDS_EXT_NC_COUNTER_COUNT = 64` slots per core; core `n`'s extended counter `c` (where `c = counter_index - 31`) is at `base + 0xda80 + (n*64 + c)*8`. The ten named extended counters (`neuron_driver_shared.h:365-376`, indexed from `NDS_NC_COUNTER_COUNT = 31`):

| `counter_index` | `c` (= idx-31) | Enum name |
|---|---|---|
| 31-34 | 0-3 | `HW_ERR_COLLECTIVES`, `HW_ERR_HBM_UE`, `HW_ERR_NC_UE`, `HW_ERR_DMA_ABORT` |
| 35-40 | 4-9 | `ERR_SW_NQ_OVERFLOW`, `ERR_SW_SEMAPHORE_ERROR`, `ERR_SW_EVENT_ERROR`, `ERR_SW_PSUM_COLLISION`, `ERR_SW_SEQUENCER_FATAL`, `HW_ERR_REPAIRABLE_HBM_UE` |
| 41-94 | 10-63 | *(reserved)* — `NDS_EXT_NC_COUNTER_ADDED_RESERVED = 54` forward-compat slots |

### EXT NC-data — `0xe280`, 12 NC × 96 × `u64`

For the *additional* cores 4-15 (`NDS_EXT_MAX_NEURONCORE_COUNT = 12`), there is no original-band entry at all — each gets the **full** 95-counter set (`NDS_TOTAL_NC_COUNTER_COUNT = 31 + 64`) plus a 1-`u64` alignment pad, for a 96-`u64` (768-byte) per-core stride. Extended core `m` (where `m = nc_index - 4`, `m` in 0-11) holds the full counter `c` (0-94) at `base + 0xe280 + (m*96 + c)*8`. Indices 0-30 here mean the same as the original NC band; 31-94 mean the same as the EXT NC band.

> **QUIRK —** the layout is **asymmetric across the 16 cores**: cores 0-3 store counters 0-30 at `0x100` and counters 31-94 at `0xda80` (split across two regions); cores 4-15 store counters 0-94 contiguously at `0xe280`. A reader must pick the region by core index *and* counter index — there is no single formula spanning all 16 cores. This asymmetry is the whole reason the addressing in §4 has three branches.

---

## 4. NC-Counter Addressing

### Algorithm

`nds_increment_nc_counter` (@`0x507220`) and its siblings resolve `(pnc_index, counter_index)` to a `u64*` via a 3-band dispatch, identical to the kernel's `get_neuroncore_counter_value` (`neuron_ds.c:207`). The arithmetic, in `u64` *element* indices (byte address = `base + idx*8`):

```c
// nds_*_nc_counter address resolver — libnrt.so 0x507220 (and kernel neuron_ds.c:207)
//   base = inst->nds_ptr ; returns &counter as u64*, or error
function nc_counter_addr(base, pnc_index, counter_index):
    if base == NULL:                  return -EPERM           // 0xffffffff
    if pnc_index <= 3:                                        // original 4 cores
        if counter_index <= 30:
            idx = 32   + pnc_index*31 + counter_index         // band @0x100  (32*8 = 256)
        else:                                                 // counter overflow
            idx = 6992 + pnc_index*64 + (counter_index - 31)  // band @0xda80 (6992*8 = 55936)
    else if pnc_index <= 15:                                  // extended cores 4-15
        if counter_index > 94:        return -EINVAL          // 0xffffffea
        idx = 7248 + (pnc_index-4)*96 + counter_index         // band @0xe280 (7248*8 = 57984)
    else:                             return -EINVAL
    return (u64*)base + idx                                   // *8 implicit in pointer arith
```

The three base constants are the byte starts divided by 8: `32 = 0x100/8`, `6992 = 0xda80/8`, `7248 = 0xe280/8` — i.e. the addressing math *is* the region map re-expressed in `u64` strides, and the strides `31`, `64`, `96` are exactly `NDS_NC_COUNTER_COUNT`, `NDS_EXT_NC_COUNTER_COUNT`, and `NDS_EXT_NEURONCORE_NC_DATA_COUNT`.

| Band | Condition | Element index | Byte address | Stride |
|---|---|---|---|---|
| NC counters | `pnc ≤ 3`, `ctr ≤ 30` | `32 + pnc*31 + ctr` | `0x100 + (pnc*31+ctr)*8` | 31 `u64`/core |
| EXT NC counters | `pnc ≤ 3`, `ctr ≥ 31` | `6992 + pnc*64 + (ctr-31)` | `0xda80 + (pnc*64+(ctr-31))*8` | 64 `u64`/core |
| EXT NC-data | `4 ≤ pnc ≤ 15`, `ctr ≤ 94` | `7248 + (pnc-4)*96 + ctr` | `0xe280 + ((pnc-4)*96+ctr)*8` | 96 `u64`/core |

The ND-counter resolver is the simple case — `nds_increment_nd_counter` (@`0x5074e0`) addresses index `i ≤ 30` at element `i+1` (`base + (i+1)*8`), the `+1` skipping the header.

> **GOTCHA —** every counter writer is **read-only gated**: the resolver (inlined `get_counter_addr`) returns `-EPERM (0xffffffff)` when `inst->pid != 0`, so a reader process cannot mutate another PID's counters. Out-of-range `pnc_index`/`counter_index` return `-EINVAL (0xffffffea)`. A reimplementation that omits the read-only gate turns every monitoring tool into a counter-corruption vector. (CONFIDENCE HIGH — the `pid != 0` branch is `nds_is_read_only` @`0x507210`, threaded through every `nds_increment/decrement/set/or_*` accessor.)

---

## 5. The Model Record

### Encoding

Each of the 64 model slots holds a `nds_model_node_data_internal` — the 672-byte (`0x2a0`) self-describing wire record, at `base + 0x528 + slot*672`. It is `{ hash, node_info, mem_usage }`: a 32-bit FNV hash, a 284-byte node-info struct, and a 384-byte mem-usage grid. All struct offsets DWARF (`ptype /o` on `libnds.a`); the public sub-structs are also defined verbatim in `neuron_ds.h:48-67`.

| Field | Offset | Size | Type | Meaning | Confidence |
|---|---|---|---|---|---|
| `hash` | `+0` | 4 | `uint32_t` | FNV-1a-32 over the record (this word zeroed) | CERTAIN |
| `node_info` | `+4` | 284 | `nds_model_node_info` | model identity + name + uuid | CERTAIN |
| `mem_usage` | `+288` | 384 | `nds_model_node_mem_usage_info` | the 2×12 mem-usage grid (§5.2) | CERTAIN |
| | | **672** | `nds_model_node_data_internal` | total (`0x2a0`) | CERTAIN |

### `nds_model_node_info` — 284 B (record `+4`)

DWARF `ptype /o`, mirroring `neuron_ds.h:54-61` (offsets relative to the **record**, i.e. node-info offset + 4):

| Field | Node-info off | Record off | Size | Type | Meaning |
|---|---|---|---|---|---|
| `model_id` | `+0` | `+4` | 4 | `uint32_t` | parent model id |
| `model_node_id` | `+4` | `+8` | 4 | `uint32_t` | node id within the model |
| `name[256]` | `+8` | `+12` | 256 | `char[256]` | model name (NUL-terminated) |
| `uuid[16]` | `+264` | `+268` | 16 | `char[16]` | model uuid (raw 16 bytes) |
| `nc_index` | `+280` | `+284` | 1 | `uint8_t` | NeuronCore the node runs on |
| `sg_index` | `+281` | `+285` | 1 | `uint8_t` | subgraph index (+2 tail pad to 284) |

### `nds_model_node_mem_usage_info` — 384 B (record `+288`): the 12-slot DMA grid

The mem-usage member is a `[MODEL_MEM_USAGE_LOCATION_COUNT = 2][NDS_DMA_MEM_USAGE_SLOT_COUNT = 12]` array of `nds_mem_usage_info` (16 B each) — 2 × 12 × 16 = 384 bytes (`neuron_ds.h:64-67`). The two locations are HOST and DEVICE; the 12 slots are DMA mem-usage categories (`neuron_ds.h:38-45`).

```c
// nds_mem_usage_info — neuron_ds.h:48-51 / DWARF ptype /o   (16 B)
struct nds_mem_usage_info {
    /* +0  | 8 */ size_t   total_size;     // aggregated byte total for this (location, slot)
    /* +8  | 4 */ uint32_t chunk_count;    // number of chunks; +4 tail pad → 16 B
};
```

Element `[loc][slot]` (loc ∈ {0,1}, slot ∈ 0..11) is at record offset `288 + (loc*12 + slot)*16`. The 12 slots:

| Slot | Enum (`neuron_ds.h:39-44`) | Category | Confidence |
|---|---|---|---|
| 0 | `NDS_DMA_MEM_USAGE_SLOT_CODE` | code | HIGH |
| 1 | `NDS_DMA_MEM_USAGE_SLOT_TENSORS` | tensors | HIGH |
| 2 | `NDS_DMA_MEM_USAGE_SLOT_CONSTANTS` | constants | HIGH |
| 3 | `NDS_DMA_MEM_USAGE_SLOT_SCRATCHPAD` | scratchpad | HIGH |
| 4 | `NDS_DMA_MEM_USAGE_SLOT_MISC` | misc | HIGH |
| 5-11 | *(unnamed)* | aggregated into the 5 named slots; `_SLOT_COUNT = 12` ("do not change") | MEDIUM |

> **NOTE —** only 5 of the 12 slots are named; the header comment (`neuron_ds.h:28-35`) states there are *"only 12 slots … so we aggregate them using the same logic as for the 'per NC' memory tracker … Monitor … aggregated them even further by adding them together."* The full `dma_mem_usage_type` enumeration that maps producer chunk types into these slots lives in `inc/tdrv/dma_mem_usage_type.h` (not in scope here); slots 5-11 are reserved/aggregated. The `12` is a frozen ABI width (`L44`: "do not change"). (MEDIUM on the slot 5-11 meanings; HIGH on the 12-slot width and the 16-B element stride.)

### Slot allocation: the 64-bit occupancy bitmap

The 64 records are allocated through a `u64` bitmap at `base + 0x520` (one bit per slot). The bitmap is read/written under the *process-local* `nds_instance_t` mutex (`+0x20`) — it is the *only* part of the format guarded by a lock, and the lock is same-process only:

```c
// nds_get_empty_map_slot — libnrt.so 0x508100   (arg = the u64 bitmap value)
function nds_get_empty_map_slot(map):
    if map == 0xffffffffffffffff: return 0xffffffff     // all 64 slots full → caller maps to -ENOSPC (28)
    return index_of_first_clear_bit(map)                // 0..63
// nds_mark_map_slot — libnrt.so 0x508130 : set/clear bit `slot` in *(u64*)(base+0x520)
```

On first commit, `nds_commit_model_node_info` (@`0x5076c0`) takes the slot, marks it, sets `wrapper->data_source = base + 0x528 + slot*672`, builds the 672-byte staging record, stamps the hash, and `memcpy`s it into the record. Delete (`nds_delete_model_node_info` @`0x507820`) zeroes the 672-byte record and clears the bitmap bit. (Full lifecycle: [the userspace page](userspace-libnds.md).)

---

## 6. The Process-Info Records

### `nds_process_info_internal` — 64 B (`0x4e0`)

The process-info record is the producer's identity card: framework type, a 32-byte tag, and three packed version triples. It is `{ hash, proc_info }` — a 32-bit FNV hash over a 60-byte `nds_process_info` payload (DWARF `ptype /o`; public payload `neuron_ds.h:77-83`).

| Field | Record off | Payload off | Size | Type | Meaning |
|---|---|---|---|---|---|
| `hash` | `+0` | — | 4 | `uint32_t` | FNV-1a-32 over the 64-B record (this word zeroed) |
| `framework_type` | `+4` | `+0` | 1 | `int8_t` | framework enum (+3 hole to align next field) |
| `tag[32]` | `+8` | `+4` | 32 | `char[32]` | free-form process tag (NUL-terminated) |
| `framework_version` | `+40` | `+36` | 8 | `nds_version_info` | framework version triple |
| `fal_version` | `+48` | `+44` | 8 | `nds_version_info` | FAL version triple |
| `runtime_version` | `+56` | `+52` | 8 | `nds_version_info` | runtime version triple |
| | | | **64** | `nds_process_info_internal` | total |

```c
// nds_version_info — neuron_ds.h:70-74 / DWARF ptype /o   (8 B)
struct nds_version_info {
    /* +0 | 1 */ uint8_t  major;
    /* +1 | 1 */ uint8_t  minor;     // +2 hole (build is 4-aligned)
    /* +4 | 4 */ uint32_t build;
};
```

> **NOTE —** the three `nds_version_info` records here are the *human-readable* framework/FAL/runtime versions, distinct from the *packed* version `u64`s in the ND counter band (indices 0-2, §3). Both exist: the counter words are the machine-aggregated form `neuron-monitor` reads numerically; the process-info triples are the `{major, minor, build}` form a tool prints. A reimplementer publishing one must publish both to match the binary.

### `nds_process_info_ext_internal` — 260 B (`0xad28`)

The extended record is `{ hash, tag[256] }` — a 32-bit FNV hash over a 256-byte tag (`nds_process_info_ext`, `neuron_ds.h:86-88`):

| Field | Offset | Size | Type | Meaning |
|---|---|---|---|---|
| `hash` | `+0` | 4 | `uint32_t` | FNV-1a-32 over the 260-B record (this word zeroed) |
| `tag[256]` | `+4` | 256 | `char[256]` | extended free-form tag (NUL-terminated) |
| | | **260** | `nds_process_info_ext_internal` | total |

Both records are published by `nds_commit_generic_obj` (@`0x507ad0`) and read by `nds_read_generic_obj` (@`0x507e00`) under the FNV-hash protocol (§7). The host-side handle wrappers returned by `nds_obj_new` (@`0x507d90`) are *not* in shared memory: type 1 (process-info) `calloc`s 88 B, type 2 (process-info-ext) 280 B, type 0 (model) 712 B — these are staging copies, distinct from the 64/260/672-byte wire records.

---

## 7. The Record Hash

### Scheme

Every self-describing record (process-info, process-info-ext, the 64 model entries) begins with a 32-bit FNV-1a-32 hash at offset `+0`, computed over the **entire record with the hash word zeroed**, by `nds_hash` (@`0x5080c0`). This is the cross-process torn-read detector — there is no seqlock and no shared mutex on the record path (architecture: [overview](overview.md) §2). The hash is canonical 32-bit FNV-1a:

```c
// nds_hash(ptr, len) — libnrt.so 0x5080c0   (basis/prime confirmed in the disasm)
uint32_t nds_hash(const uint8_t *ptr, size_t len) {
    uint32_t h = 0x811c9dc5;                 // FNV-1a offset basis  (mov $0x811c9dc5,%eax @+0x03)
    for (size_t i = 0; i < len; i++) {
        h ^= ptr[i];                         // xor %edx,%eax        (@+0x27)
        h *= 0x01000193;                     // FNV prime, imul      (imul $0x1000193 @+0x29)
    }
    return h;
}
```

The byte loop runs over the **whole record length** — `len = 64` (process-info), `260` (process-info-ext), or `672` (model record, `0x2a0`) — *including* the zeroed 4-byte hash field at `+0` (those four bytes are 0 during hashing, so they contribute the basis mixed with four zero bytes; the protocol is self-consistent because both producer and reader zero them identically).

### Producer stamp / reader verify

```c
// PRODUCER: nds_commit_generic_obj (0x507ad0) / nds_commit_model_node_info (0x5076c0)
//   build the record in a private staging buffer, then:
staging->hash = 0;                            // zero the hash word first
staging->hash = nds_hash(staging, record_len);// hash the whole record (hash word now = computed value)
memcpy(shared_record, staging, record_len);   // publish: hash word stored first, then payload

// READER: nds_read_generic_obj (0x507e00) / nds_read_model_node_info_data (0x507890)
for (attempt = 0; attempt < 2; attempt++) {   // at most TWO reads
    memcpy(local, shared_record, record_len);
    if (local.hash == 0) { usleep(200); continue; }     // 0xc8 — not yet published
    stored = local.hash; local.hash = 0;
    if (nds_hash(local, record_len) != stored) { usleep(200); continue; } // 0xc8 — torn read
    return publish(local);                    // consistent snapshot
}                                             // falls through after 2 attempts → last (stale) copy
```

> **GOTCHA —** the retry budget is **one** (`attempt < 2` → at most two reads); the back-off windows are fixed and *differ by path*: `usleep(500)` (`0x1f4`) for the *header* check, `usleep(200)` (`0xc8`) for *record* reads. A `hash == 0` record means "producer has not published this slot yet" (the slab `memset` left it zero, and `nds_hash` of an all-zero record is not zero, so a published record's hash is never coincidentally 0 in practice). The protocol *tolerates* staleness — after two failed attempts the reader returns the last copy rather than blocking — so a strict reimplementation must not loop unboundedly against a producer stuck mid-`memcpy`. (CONFIDENCE HIGH — full disasm of the commit/read pairs.)

### Records that have a hash vs. those that do not

| Region | Has FNV hash? | Sync mechanism |
|---|---|---|
| `nds_header` | no | single `u64` store/compare + `usleep(500)` retry |
| ND / NC / EXT counters | **no** | `LOCK ADD/SUB/OR`; plain `u64` load (natural atomicity) |
| model bitmap (`0x520`) | no | process-local `pthread_mutex` (same-PID only) |
| process-info (`0x4e0`) | **yes** | FNV-1a + 1× `usleep(200)` retry |
| process-info-ext (`0xad28`) | **yes** | FNV-1a + 1× `usleep(200)` retry |
| model entries (`0x528`) | **yes** | FNV-1a + 1× `usleep(200)` retry |

> **GOTCHA —** the single most error-prone split in the whole format is *which records carry a hash*. The counter arrays do **not** — adding a hash word to them, or hashing them on read, will not match the producer and will desync every monitoring tool. The object records **do** — skipping the hash there will let a torn read through. Get this table right before anything else.

---

## 8. Worked Byte-Layout Examples

A reimplementer can validate an implementation against these fully-resolved addresses (all `base`-relative, `base = inst->nds_ptr`).

```text
HEADER
  base+0x000 .. +0x007   nds_header   "nds\0" 6e 64 73 00 | version a0 86 01 00

ND counter 3 (FEATURE_BITMAP)
  base + (3+1)*8 = base+0x020         u64, written via LOCK OR (1<<bit)

NC counter (core 2, INFER_COMPLETED = idx 1)
  idx = 32 + 2*31 + 1 = 95            byte addr = base + 95*8 = base+0x2f8

NC overflow (core 1, HW_ERR_HBM_UE = counter_index 32)
  idx = 6992 + 1*64 + (32-31) = 7057  byte addr = base + 7057*8 = base+0xdc88

EXT core (nc_index 7 → m=3, MAC_COUNT = idx 29)
  idx = 7248 + 3*96 + 29 = 7565       byte addr = base + 7565*8 = base+0xec68

PROCESS-INFO record
  base+0x4e0  hash(u32) | +0x4e4 framework_type | +0x4e8 tag[32]
  +0x508 framework_version(8) | +0x510 fal_version(8) | +0x518 runtime_version(8)

MODEL slot 5 record
  rec = base + 0x528 + 5*672 = base+0x1248
  rec+0x000 hash | rec+0x004 model_id | rec+0x008 model_node_id | rec+0x00c name[256]
  rec+0x10c uuid[16] | rec+0x11c nc_index | rec+0x11d sg_index
  rec+0x120 mem_usage[2][12]    (loc l, slot s) → rec+0x120 + (l*12+s)*16
            mem_usage[1][3].chunk_count → rec+0x120 + (12+3)*16 + 8 = rec+0x218

PROCESS-INFO-EXT record
  base+0xad28 hash(u32) | +0xad2c tag[256]   (ends 0xae2c = NDS_EXT_OFFSET_OLD)
```

---

## 9. Three-Way Byte-Agreement

The layout is authored by three independent sources that must agree exactly — the kernel (which reads counters back), userspace `libnds.a` (which writes everything), and the shared header (which both `#include`). Verified field-by-field:

| Aspect | Shared header (macro algebra) | Userspace `libnds.a` (disasm) | Kernel `neuron_ds.c` | Agree |
|---|---|---|---|---|
| total map size | `NDS_REQUIRED_SIZE = 0x10680` | `nds_open` gate `cmp size,0x1067f; jbe fail` | counter table ends at 67200 | **yes** |
| header proto | `nds_header_t` `"nds\0"`+100000 | `.data header_proto` @`0xc0c820` = `6e6473 00 a0860100` | `memset(0)` only (never written) | **yes** (writer is userspace) |
| ND start/count | `0x008`, 31 | `(i+1)*8` resolver @`0x5074e0` | — | **yes** |
| NC band | `0x100`, 4×31 | `32 + pnc*31 + ctr` @`0x507220` | `get_neuroncore_counter_value:207` band 1 | **yes** |
| EXT NC band | `0xda80`, 4×64 | `6992 + pnc*64 + (ctr-31)` | `:207` band 2 | **yes** |
| EXT NC-data band | `0xe280`, 12×96 | `7248 + (pnc-4)*96 + ctr` | `:207` band 3 | **yes** |
| EXT offset algebra | `NDS_ALIGN(44588+11296)=55936` | base const `6992 = 55936/8` | `:207` matches | **yes** |
| process-info | `0x4e0`, 64 B (`hash`+60) | staging `calloc 0x40`, `map_source=base+0x4e0` | opaque (kernel never indexes) | **yes** |
| model bitmap | `0x520`, `u64` | `nds_get_empty_map_slot` @`0x508100` | opaque | **yes** |
| model record | `0x528`, 64×672 | `data_source = base+0x528+slot*672`, `nds_hash(...,0x2a0)` | opaque | **yes** |
| process-info-ext | `0xad28`, 260 B (`hash`+256) | staging `calloc 0x104`, `map_source=base+0xad28` | opaque | **yes** |
| FNV-1a basis/prime | (record protocol, not in header) | `0x811c9dc5` / `0x01000193` @`nds_hash` | — | **yes** (matches canonical FNV) |

> **Verification —** the region map is a 3-way agreement on every start: `0x008, 0x100, 0x4e0, 0x520, 0x528, 0xad28, 0xda80, 0xe280, 0x10680`. The shared-header macro chain evaluated arithmetically (mirroring a compiled `calc.c`) produces these exact values; the disassembled accessor constants (`lea`/`imul`/`cmp`: base elements `32`/`6992`/`7248`, strides `31`/`64`/`96`, gate `0x1067f`) re-encode the same offsets in `u64` units; and the kernel's `get_neuroncore_counter_value` (`neuron_ds.c:207`) 3-band dispatch reconciles byte-for-byte. The header proto is verified directly: the bytes at `libnrt.so` file offset `0xc0c820` are `6e 64 73 00 a0 86 01 00`, the sole occurrence in the binary, loaded by `nds_commit_header` as a single 8-byte store.
>
> **[MEDIUM]** Two soft spots: (1) the EXT-section pad region (`44588..55936`) is computed from the `_OLD` constants and is *unused content* in this version — HIGH that the *offset* is 55936, MEDIUM that the runtime never reads the gap. (2) The slot 5-11 meanings in the 12-slot DMA grid are not named in the public header (only slots 0-4 are) — HIGH on the 16-B element stride and 12-slot width, MEDIUM on the per-slot category beyond `CODE/TENSORS/CONSTANTS/SCRATCHPAD/MISC`.

---

## Cross-References

- [Overview: the Shared-Memory Counter Plane](overview.md) — the kernel/userspace split, the torn-read safety model, and the cross-page orientation this page's layout serves
- [Kernel Side (Per-Process Slabs)](kernel-side.md) — the 16-slab array, `NEURON_DATASTORE_SIZE = 262144`, and `get_neuroncore_counter_value`'s 3-band counter read that mirrors §4
- [Userspace Side (libnds.a)](userspace-libnds.md) — the 6-TU accessor library: `nds_open`/`nds_commit_*`/`nds_read_*`, the slot allocator, and the full handle-wrapper lifecycle behind §5-§7
- [Kernel Datastore](../kernel/datastore.md) — the GPL `neuron_ds.c` backing store and the shared-header `#include` that makes this layout binding on both sides
- [back to index](../index.md)
