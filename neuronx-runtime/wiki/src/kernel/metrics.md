# Metrics Aggregation

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The engine is read directly from `neuron_metrics.c` (1247 lines) and `neuron_metrics.h` (218 lines); both files are read in full. The source is read, not reverse-engineered — every period constant, `enum nmetric_cw_id` value, `nmetric_defs[]` row, struct field, tick gate, and lock boundary is transcribed from the shipped `.c`/`.h`. Two derivations are flagged where they are hand-recomputed rather than literal (the CW-id ↔ NDS-counter binding, owned by [Sysfs Metrics Tree](sysfs.md); the `NRT_STATUS` crosswalk, owned by [Error and Status Codes](../runtime/error-codes.md)). Other driver versions renumber lines and shift the enum. Part III — Kernel Driver · DEEP · [back to index](../index.md)*

## Abstract

`neuron_metrics.{c,h}` implement the per-device **`ndN metrics`** kernel thread — a `kthread` named `"nd%d metrics"` (`:1139`), one per accelerator, that converts the live [datastore](../datastore/kernel-side.md) counter slabs into a CloudWatch (CW) byte-stream and posts it to firmware over [FW-IO](fw-io.md). The thread runs two cadences on a single 50 ms wait loop. The **fast tick** (every 50 ms) calls `npower_sample_utilization` to accumulate power-utilization samples ([power](power.md)). The **slow tick** (~2.5 min) acquires the datastore lock, walks every in-use datastore entry, and aggregates: it sums per-NeuronCore counters into a flat `curr[]` array, picks the most-frequently-seen version string per component via an 8-deep LFU history, ORs feature bitmaps, and snapshots the non-datastore metrics (driver-reset times, ECC error counts, ultraserver mode). It then serializes a **differential** of that aggregate into a ≤128-byte packed stream of `{id:u8, len:u8, data[len]}` records and posts it.

The familiar frame is a **statsd-style drain agent embedded in the kernel**: the runtime is the in-process client that increments shared counters (it never serializes a metric), and this thread is the out-of-band agent that drains, *differentiates against a per-session baseline*, packs, and ships. The difference from statsd is that the "shared memory" is the kernel-mediated NDS slab, the "agent" is a kernel thread, and the "UDP socket" is an on-device firmware mailbox (`POST_TO_CW`). Every COUNTER and UTILIZATION metric is posted as a delta `(curr + freed − prev)` and *skipped* when it has not advanced (`:617`); `prev[]` is rebased to `curr[]` at the end of every post (`nmetric_start_new_session`, `:991`). The wire value is the change since the last post, never the lifetime total.

The 128-byte budget is split across **two ticks** (`POST_TICK_COUNT = 2`, `h:21`): each of the 46 registry rows carries a `tick` field (`TICK_0` / `TICK_1` / `ALWAYS`), the thread alternates `tick = (tick + 1) % 2`, and `ALWAYS` metrics post on every tick. This page is the kernel half of the telemetry plane whose end-to-end flow (runtime publish → NDS slab → this engine → FW-IO → CloudWatch) is owned by [Telemetry, Metrics & Error Reporting](../trace/telemetry-errors.md). §1 is the thread/timer model; §2 is the aggregation tick as annotated pseudocode; §3 is the CloudWatch record encoding and the differential rule; §4 is the post path. The `metric_id` taxonomy and the `NRT_STATUS` table are sibling-owned and only referenced.

For reimplementation, the contract is:

- **The two-cadence thread** — one 50 ms wait loop that fast-samples power every wake and slow-posts on a drift-free `(jiffies − start) / post_jiffies` interval count, with `PAUSED`/`RESUMING` states and a bounded `RESUMING` catch-up.
- **The aggregation tick** — datastore-lock-held walk of every in-use entry, per-type fan-in (counter sum over `dev_nc_map`, version LFU pick, bitmap OR, constant-u64 last-writer-wins), the `FW_IO_ERR` special, and the process-death `freed` capture; post happens *outside* the lock.
- **The CW record encoding** — the `nmetric_cw_metric {id:u8, len:u8, data[]}` `__packed` wire record, ASCII payload with no trailing NUL, the ≤128-byte stream, and the `nmetric_cw_id` id space.
- **The differential rule** — `crt = curr + freed`; skip if `crt ≤ prev`; post `crt − prev`; rebase `prev := curr` per session — and how the `freed` term is consumed exactly once per tick.

| | |
|---|---|
| **Owning TU** | `neuron_metrics.c` (1247 lines) / `neuron_metrics.h` (218 lines), GPL-2.0, dkms 2.27.4.0 |
| **Per-device state** | `struct neuron_metrics` embedded in `neuron_device.metrics` (`neuron_device.h:104`) |
| **kthread** | `nmetric_thread_fn` (`:1047`), spawned `kthread_run(…, "nd%d metrics", index)` (`:1139`) |
| **Per-tick pipeline** | `nmetric_aggregate_and_post_tick` (`:1017`) — aggregate+cache under lock → post → new session |
| **Registry** | `nmetric_defs[]` (`:172-243`, **46 rows**) of `nmetric_def_t {index,type,count,tick,cw_id,ds_id,flags}` (`h:90-98`, 7×`u8`) |
| **Metric types** | 10 (`CONSTANT 0` … `ECC_ERR_COUNTER 9`, `h:23-32`); each has a mirror aggregate/post path |
| **CW wire record** | `struct nmetric_cw_metric { u8 id; u8 len; u8 data[]; }` `__packed` (`:254-258`); ASCII, no NUL |
| **CW id space** | `enum nmetric_cw_id` (`:42-170`), ids `11..253`; error band `200..253` — taxonomy **owned by** [sysfs](sysfs.md) |
| **Budget** | `NEURON_METRICS_MAX_POSTING_BUF_SIZE = 128` (`h:14`); `posting_buffer[129]` (`h:143`); split over 2 ticks |
| **Periods** | post delay `150000 ms` (~2.5 min, module-param, `:23`); sample delay `50 ms` (`:24`, not a param); `nmetric_log_posts = 1` (bit0=CW, bit1=trace, `:25`) |
| **Post target** | `ndhal->ndhal_fw_io.fw_io_post_metric(ctx, buf, size)` (`:900`, POST_TO_CW); ftrace fallback `trace_metrics_post` (`:442`) |
| **Confidence** | HIGH — both files read in full; all signatures, registry rows, enum values, periods, tick logic, and lock boundaries are verbatim |

---

## 1. The Thread and Timer Model

### Purpose

One `kthread` per accelerator owns all metric collection. There is no per-NeuronCore thread and no workqueue: `nmetric_create_thread` (`:1135`) does a single `kthread_run(nmetric_thread_fn, nd, "nd%d metrics", nd->device_index)` (`:1139`), and the thread body multiplexes a **fast** 50 ms power-sample cadence and a **slow** ~2.5 min posting cadence onto one `wait_event_interruptible_timeout` loop. The fast tick has no post; it only feeds the power subsystem so the eventual `NC_UTILIZATION` metric has high-resolution input. The slow tick is the whole aggregate-and-post pipeline.

### The two cadences

```text
nmetric_thread_fn (:1047)  ── the kthread body, one per neuron_device
  │  sample_delay_in_jiffies = msecs_to_jiffies(50)        :1074  (nmetric_metric_sample_delay)
  │  post_delay_in_jiffies   = msecs_to_jiffies(150000)    :1075  (nmetric_metric_post_delay, ~2.5 min)
  │
  └─ loop while !kthread_should_stop() && state != STOPPED:
        wait_event_interruptible_timeout(wq, STOPPED || RESUMING, sample_delay)   ── 50 ms wake
        ├─ device not READY?  → continue                   :1094  (no sampling on a down device)
        ├─ FAST TICK:  nmetric_sample_high_freq(nd)         :1099  every wake (50 ms)
        │                └─ npower_sample_utilization(nd)    :1014  [BOUNDARY → power]
        └─ SLOW TICK:  slow_tick = (jiffies - start) / post_delay   :1104  (drift-free interval count)
              ├─ state == PAUSED?  → continue               :1107  (skip post, keep sampling)
              └─ slow_tick != last_logged_slow_tick:
                    budget = (state==RESUMING) ? min(Δ, POST_TICK_COUNT=2) : 1   :1115-1121
                    for i in 0..budget:
                        nmetric_aggregate_and_post_tick(nd, …, tick)   ── §2
                        tick = (tick + 1) % 2               :1126  (alternate the band)
                    last_logged_slow_tick = slow_tick       :1129
```

### State machine and the `nmetric_state` enum

The thread reads a `volatile enum nmetric_state` (`h:118-123`: `STOPPED 0`, `RUNNING 1`, `PAUSED 2`, `RESUMING 3`) that the cdev `metrics_ctrl` ioctl flips through `nmetric_set_mode` (`:1158`, `ENABLE → RESUMING`, `DISABLE → PAUSED`, from `neuron_cdev.c:1690`). The three live states gate distinct behaviour:

| State | Fast tick (power) | Slow tick (post) | Set by |
|---|---|---|---|
| `RUNNING` | yes | yes (budget = 1) | steady state |
| `PAUSED` | yes | **no** (`continue` at `:1107`) | `nmetric_set_mode(DISABLE)` |
| `RESUMING` | yes | yes, with **catch-up** (budget = `min(Δ, 2)`) | `nmetric_set_mode(ENABLE)` after a pause |
| `STOPPED` | — (loop exits) | — | `nmetric_stop_thread` (`:1147`) |

> **QUIRK — the slow tick is a drift-free interval count, not a deadline.** `:1104` computes `slow_tick = (jiffies − start_jiffies) / post_delay_in_jiffies` and posts only when that integer count changes versus `last_logged_slow_tick`. Because the count is recomputed from the original start each wake, a slow tick that wakes late (the 50 ms wait returned late, or a `PAUSED` window elapsed) does not drag the schedule — the next post still lands on the original ~2.5 min grid. A reimplementer that instead does `if (now − last_post >= post_delay)` will accumulate drift across every late wake.

> **GOTCHA — `RESUMING` catches up at most two ticks.** When the thread comes out of `PAUSED`, the slow-tick count may have advanced by more than one (`Δ = slow_tick − last_logged_slow_tick > 1`). The catch-up budget is `min(Δ, POST_TICK_COUNT)` = at most **2** (`:1115-1121`), exactly enough to flush both the `TICK_0` and `TICK_1` bands once. A pause longer than two slow-tick periods does **not** replay every missed tick — it posts one of each band and resyncs. This is intentional (the two bands together are one logical post), but a reimplementer expecting "replay all missed intervals" will over-post. (Confidence HIGH.)

### Lifecycle

The thread is created late and torn down explicitly; the constant metrics that need no device are gathered once at module load.

```text
neuron_module.c:73   nmetric_init_constants_metrics()    ── ONCE, global: read board_asset_tag, driver_version
neuron_pci.c:363     nmetric_init_driver_metrics(nd)     ── probe: atomic64_set 0 across reset-metric arrays
neuron_pci.c:459     nmetric_init(nd)                    ── probe: zero ds_freed_*, state=STOPPED, create thread
                       └─ nmetric_create_thread(nd) → kthread_run "nd%d metrics"   :1139
neuron_pci.c:472,507 nmetric_stop_thread(nd)            ── remove/reset: state=STOPPED; wake; kthread_stop (blocks)
neuron_cdev.c:1690   nmetric_set_mode(nd, mode)         ── ioctl pause/resume  [BOUNDARY → cdev]
```

`nmetric_init_constants_metrics` (`:264`) reads the EC2 instance id from `/sys/devices/virtual/dmi/id/board_asset_tag` (`:39,277`) via `filp_open`/`kernel_read`, defaulting to `"0"` on failure and stripping a trailing space (`:280-281`); it `memcpy`s `driver_version` with a `BUG_ON` if it exceeds 63 bytes (`:288`). These three constants (instance id, driver version, performance-profile id) are device-independent and computed before any thread exists.

---

## 2. The Aggregation Tick

### Purpose

One slow tick is `nmetric_aggregate_and_post_tick` (`:1017`). It is a three-stage pipeline with one hard rule: **aggregation and the freed-buffer snapshot run under the datastore lock; posting runs after the lock is released.** Holding the datastore lock across the FW-IO post would serialize firmware round-trips against every datastore mutation; releasing it first keeps the (slow, ≤30 s worst-case, [fw-io](fw-io.md)) post off the datastore's critical path.

### The per-tick pipeline

```c
// Models nmetric_aggregate_and_post_tick (neuron_metrics.c:1017).
void nmetric_aggregate_and_post_tick(struct neuron_device *nd, …, u8 tick) {
    u64 curr[NMETRIC_COUNTER_COUNT];     // =30 (h:79); per-tick counter/util accumulators
    u64 feature_bitmap = 0;              // BITMAP fan-in
    u64 const_u64[NMETRIC_CONSTANT_U64_COUNT];           // =2 (h:84)
    struct nmetric_versions versions[NMETRIC_VERSION_COUNT];   // =3, snapshot of component_versions

    neuron_ds_acquire_lock(&nd->datastore);                              // :1019  [BOUNDARY → datastore]
        nmetric_full_aggregate(nd, curr, &feature_bitmap, const_u64, tick);   // :1020  (walk every entry)
        nmetric_cache_shared_bufs(nd, freed, versions, &freed_bm,
                                  freed_const_u64, tick);                // :1022  (snapshot+reset freed)
    neuron_ds_release_lock(&nd->datastore);                             // :1025  ── POST IS OUTSIDE THE LOCK

    nmetric_post_metrics(nd, curr, prev, freed, versions, feature_bitmap,
                         freed_bm, const_u64, freed_const_u64, tick);    // :838   §3
    nmetric_start_new_session(nd, curr, prev, freed, …, tick);          // :973   prev := curr (baseline)
}
```

### Stage 1 — the datastore walk

`nmetric_full_aggregate` (`:400`) loops `NEURON_MAX_DATASTORE_ENTRIES_PER_DEVICE`, and for every in-use entry calls the per-entry fan-in `nmetric_aggregate_nd_counter_entry` (`:352`). The fan-in is a `switch` on the metric `type`; each type writes a different accumulator. After the loop it injects the one non-datastore counter that belongs to the live path — the FW-IO error count.

```c
// Models nmetric_full_aggregate (:400) + nmetric_aggregate_nd_counter_entry (:352).
void nmetric_full_aggregate(nd, u64 *curr, u64 *feature_bm, u64 *const_u64, u8 tick) {
    for (i = 0; i < NEURON_MAX_DATASTORE_ENTRIES_PER_DEVICE; i++) {       // :406
        entry = &nd->datastore.entries[i];
        if (!neuron_ds_check_entry_in_use(entry)) continue;             // :407  [BOUNDARY → datastore]

        for each metric m in nmetric_defs[]:                            // per-entry, per-metric
            if (!nmetric_check_post_tick(tick, m)) continue;            // :335  central tick gate
            switch (m->type) {

            case VERSION:                                               // :303 nmetric_aggregate_version_metrics
                v = NDS_ND_COUNTERS(ds_base)[m->ds_id];                 // raw nmetric_version_t word
                // LFU over NEURON_METRICS_VERSION_MAX_CAPACITY = 8 slots (:313):
                //   if v already present → ++version_usage_count[slot]
                //   else                 → evict least-used slot, install v, usage = 1
                aggregate_into(nd->metrics.component_versions[m->index], v);

            case UTILIZATION:
            case COUNTER:                                               // sum over every NC in dev_nc_map
                for (nc in dev_nc_map):                                 // :372-373  [BOUNDARY → dhal/addr-map]
                    curr[m->index] += get_neuroncore_counter_value(entry, nc, m->ds_id);  // :376 [→ datastore]

            case BITMAP:                                                // feature flags: OR across entries
                *feature_bm |= NDS_ND_COUNTERS(ds_base)[m->ds_id];      // :381

            case CONSTANT_U64:                                          // device-cluster-id / agg-neff-id
                const_u64[m->index] = NDS_ND_COUNTERS(ds_base)[m->ds_id];  // :385  LAST WRITER WINS
            }
    }
    curr[NMETRIC_FW_IO_ERR_IDX] = fw_io_get_err_count(nd->fw_io_ctx);    // :412  idx 17  [BOUNDARY → FW-IO]
}
```

Four distinct fan-in disciplines share one walk, and a reimplementer must keep them straight:

- **COUNTER / UTILIZATION → additive across NeuronCores.** Each in-use entry contributes `Σ_nc get_neuroncore_counter_value(entry, nc, ds_id)` into `curr[index]`. The datastore's 3-band counter reader is the [datastore](../datastore/kernel-side.md) boundary; this engine only sums.
- **VERSION → most-frequent pick via 8-deep LFU.** `nmetric_aggregate_version_metrics` (`:303`) maintains a `{u32 version_usage_count[8]; u64 version_metrics[8]}` history (`h:113-116`) per component. A version already in history bumps its usage count; a new version evicts the least-used of the 8 slots. The post step (§3) emits the *max-usage* version — i.e. the modal version across all live processes, not the newest. This is how a device running mixed runtime versions reports the dominant one.
- **BITMAP → OR across entries.** Feature flags are unioned: a feature in use by any live process is reported.
- **CONSTANT_U64 → last writer wins.** `const_u64[index]` is overwritten, not accumulated; whichever in-use entry is walked last sets the device-cluster-id / agg-neff-id. These are device-wide constants, so any live entry carries the same value and the overwrite is harmless.

> **NOTE — the FW-IO error count is not a datastore counter.** `curr[17]` (`NMETRIC_FW_IO_ERR_IDX`, `:252`) is filled *after* the entry loop from `fw_io_get_err_count(nd->fw_io_ctx)` (`:412`), the only `curr[]` slot sourced outside the datastore. Its registry row (`type FW_IO_ERR`, `cw11`, `ds 0xFF`) carries `ds_id = 0xFF` precisely because there is no datastore counter behind it. A reimplementer must special-case this index. (Confidence HIGH.)

### Stage 2 — the freed-buffer snapshot and process-death capture

The differential model needs counts from *dying* processes that the slow-tick walk would otherwise miss: when a process's datastore entry is freed, its counters vanish from the live walk. `nmetric_partial_aggregate` (`:420`) — called from `neuron_ds.c:161` under the datastore lock on entry release — rolls the dying entry's counters into device-level `ds_freed_*` buffers with `tick = POST_TIME_ALWAYS` (so both bands capture them). These buffers are the `freed` term in the differential.

`nmetric_cache_shared_bufs` (`:916`) runs *inside* the tick's datastore lock and does the consume-exactly-once dance: it snapshots `ds_freed_metrics_buf` / `component_versions` / `ds_freed_feature_bitmap_buf` into thread-local buffers **and zeroes the shared device buffers for the matching tick**, so a freed counter is counted by exactly one of the two ticks. It also refreshes the performance-profile constant from `ndhal->ndhal_perf.current_performance_profile` (`:954`).

> **GOTCHA — freed counters are consumed per-tick, and `partial_aggregate` writes with `ALWAYS`.** Because process-death capture uses `POST_TIME_ALWAYS` (`:421-424`) but `cache_shared_bufs` snapshots-and-resets per `tick`, a counter freed between the two ticks of one logical post is consumed by whichever tick's `cache_shared_bufs` runs after the free. The reset-per-tick guarantees no double-count *within* a tick; cross-tick exactly-once depends on the `ALWAYS` write landing before the next `cache_shared_bufs`. The window where a free races a tick boundary is the subtle one. (Confidence HIGH on the mechanism; the precise race-window behaviour is MED.)

### Stage 3 — snapshot the non-datastore metrics

Three metric families never touch the datastore and are read at post time, not in the walk:

- **Driver-reset metrics** — max/avg device & TPB reset time and fail counts live only in `struct nmetric_driver_metrics` (`h:66-69`) as `atomic64_t final_metrics[6]` + `intermediate_metrics[4]`, written lock-free by [reset](reset.md) (`nmetric_set_reset_time_metrics` `:1196` CAS-bumps max, `atomic64_add`s total, `atomic64_inc`s count; `nmetric_increment_reset_failure_count` `:1233`) and read+reset at post time.
- **ECC error counts** — read live via `ndhal->ndhal_sysfs_metrics.nsysfsmetric_get_hbm_error_count()` (the *same* helper the [sysfs](sysfs.md) `mem_ecc_uncorrected` attribute uses, for cross-surface consistency), then differenced against `ecc_prev[index]`.
- **Ultraserver mode** — read via `ndhal->ndhal_npe.npe_pod_info()` (`:754`); skipped entirely if the device is in no pod (`NEURON_POD_TYPE_NONE`).

---

## 3. The CloudWatch Record Encoding

### Purpose

The aggregate is serialized into a single packed byte buffer of length ≤128. The wire format is a flat sequence of variable-length records; there is no envelope, no count word, no checksum — the firmware `POST_TO_CW` path treats the buffer as opaque bytes and CloudWatch parses the record stream. This section owns the record layout, the id space, and the differential-vs-absolute rule that decides each record's payload.

### The wire record

```c
struct nmetric_cw_metric {     // neuron_metrics.c:254-258, __packed__
    u8 id;                     // CloudWatch metric id (enum nmetric_cw_id)
    u8 len;                    // length of data[] in bytes
    u8 data[];                 // ASCII payload — NO trailing NUL on the wire
} __packed;
```

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `id` | +0 | `u8` | the `cw_id` from the metric's `nmetric_defs[]` row; `enum nmetric_cw_id` (`:42-170`) | HIGH |
| `len` | +1 | `u8` | byte count of `data[]`; the `n` from `snprintf` of the value | HIGH |
| `data` | +2 | `u8[len]` | the value as **ASCII text** (decimal `"%llu"`, hex `"%llx"`, or `"%d.%d.%d"` version) | HIGH |

The whole post is `posting_buffer[129]` (`h:143` — `NEURON_METRICS_MAX_POSTING_BUF_SIZE = 128` plus one). `nmetric_post_metrics` writes records back-to-back, advancing a cursor; the available budget starts at 128 (`:851`). A record consumes `2 + len` bytes. There is no per-record alignment and no terminator.

> **QUIRK — the payload is ASCII, not binary.** A counter delta of `12345` is posted as the five bytes `'1','2','3','4','5'`, not a little-endian `u64`. Every post helper `snprintf`s into a scratch buffer and `memcpy`s the ASCII into `data[]` with `len = strlen`, no trailing NUL (the `:254-258` struct documents the no-NUL invariant). A reimplementer encoding values as binary will produce a stream CloudWatch cannot parse. Hex is used for exactly one metric — `AGG_NEFF_ID` (`cw80`), via `nmetric_post_hex_constant_u64` (`:705`, `"%llx"`, source `:876-877`); everything else is decimal.

### The id space

`enum nmetric_cw_id` (`:42-170`) is the `u8` id namespace. It is **owned by** [Sysfs Metrics Tree](sysfs.md) as the authoritative `metric_id` taxonomy and the [error-codes](../runtime/error-codes.md) page as the `NRT_STATUS` crosswalk — the table below describes the *bands* a reimplementer needs to lay out the stream, not the per-id binding.

| Band | Ids | Type(s) | What it carries | Confidence |
|---|---|---|---|---|
| Driver / FW | `11..13` | `FW_IO_ERR`, `CONSTANT` | FW-IO error count, instance id, driver version | HIGH |
| Reset / platform | `50..58` | `DRIVER_RESET`, `CONSTANT`, `DRIVER_USERVER` | reset max/avg/fail, perf-profile id, ultraserver modes/mode | HIGH |
| Aggregate ids | `80`, `90` | `CONSTANT_U64`, `UTILIZATION` | agg-NEFF id (hex), NC utilization (%) | HIGH |
| Versions | `180..196` | `VERSION` | runtime (`180-181`), framework + type (`190-193`), FAL (`195-196`) | HIGH |
| Error band | `200..253` | `COUNTER`, `BITMAP`, `ECC_ERR_COUNTER` | the `NRT_STATUS`-mirrored error counters, feature bitmap, ECC UE | HIGH |

The error band (`200..253`) is the 1:1 mirror of the `NDS_NC_COUNTER` / `NDS_EXT_NC_COUNTER` ids — each `nmetric_defs[]` row in this band carries an inline comment naming the exact `NRT_STATUS` it tracks, which is the single most explicit cross-layer crosswalk in the driver. The band is **split across the two ticks**: the core `0..15`-derived counters are `TICK_0`, the extended HW/SW error counters are `TICK_1` (visible in the registry's `tick` column, `:196-228`). The `NRT_STATUS` ↔ `cw_id` binding itself is owned by [error-codes](../runtime/error-codes.md).

> **NOTE — two registry rows post nothing.** The `BITMAP idx0 → cw0 (UNUSED)` row (`NDS_ND_COUNTER_DYNAMIC_SYSFS_METRIC_BITMAP`, `:227`) targets `cw_id = 0` (`UNUSED`) and is therefore **not** emitted — it is aggregated but never serialized. Gaps `203/210/211` in the enum are deprecated/reserved (`:187`). A reimplementer should not invent records for `UNUSED` ids. (Confidence HIGH.)

### The differential-vs-absolute rule

The payload of a COUNTER/UTILIZATION record is a *delta*; the payload of a CONSTANT/CONSTANT_U64/VERSION record is the *absolute* current value. The discriminant is the metric type, and the differential helpers enforce a skip-on-no-change.

```c
// Models nmetric_post_counter (:608) — the differential rule, mirror of the aggregate.
int nmetric_post_counter(u64 curr, u64 prev, u64 freed,
                         const nmetric_def_t *m, u8 *dest, int avail) {
    u64 crt = curr + freed;                 // live walk + process-death-captured counts
    if (crt <= prev) return 0;              // :617  no advance / overflow wrap → SKIP the record entirely
    u64 delta = crt - prev;                 // value posted = change since last session
    char ascii[NMETRIC_VERSION_STRING_MAX_LEN + 1];
    int n = snprintf(ascii, sizeof ascii, "%llu", delta);
    int sz = 2 + n;                         // id + len + n data bytes
    if (avail < sz) return 0;               // budget guard; SMALLER later metrics may still fit
    dest[0] = m->cw_id;
    dest[1] = (u8)n;
    memcpy(dest + 2, ascii, n);
    return sz;
}

// Models nmetric_post_feature_bitmap (:636) — OR'd value, skip if empty.
int nmetric_post_feature_bitmap(const nmetric_def_t *m, u8 *dest,
                                u64 curr_bm, u64 freed_bm, int avail) {
    u64 value = curr_bm | freed_bm;
    if (value == 0) return 0;               // no features in use → skip
    return post_u64_decimal(m, value, dest, avail);
}

// Models nmetric_start_new_session (:973) — rebase the differential baseline AFTER the post.
void nmetric_start_new_session(nd, u64 *curr, u64 *prev, u64 *freed, …, u8 tick) {
    for each COUNTER/UTILIZATION metric m gated by tick:
        prev[m->index] = curr[m->index];    // :991  next session's baseline
    zero(const_u64); zero(curr); zero(freed); zero(feature_bitmaps);   // clean slate for next tick
}
```

The three rules a reimplementer must reproduce exactly:

- **`crt = curr + freed`.** The differential includes both live processes (`curr`, from the walk) and dying ones (`freed`, from `cache_shared_bufs`). Dropping `freed` loses every count from a process that exited inside the post window.
- **Skip when `crt ≤ prev`.** A counter that did not advance (or that wrapped, making `crt < prev`) emits **no record** (`:617`). The absence of a record is itself the "no change" signal; CloudWatch is not told a zero delta. This both saves budget bytes and avoids posting a garbage value on wrap.
- **`prev := curr` after the post, per tick.** `nmetric_start_new_session` (`:991`) rebases the baseline *after* `post_metrics`, gated by the same tick, so each band's baseline advances only when that band posts. The `freed` and `curr` accumulators are zeroed for the next tick.

The special-value helpers refine the rule for non-counter types: `nmetric_post_constant_u64_fmt` (`:676`) honours `SKIP_ZERO` (fall back to the freed value, then skip if still zero) and `PREFER_FREED` (swap the live and freed values); `nmetric_post_utilization` (`:568`) post-processes `NC_UTILIZATION` from picoseconds to integer percent (`/1000` ps→ns, `/nc_per_device`, `×100 / elapsed_ns`, `:588-589`) with a divide-by-zero guard on `elapsed_jiffies == 0` (`:577`); `nmetric_post_version_with_max_usage` (`:460`) picks the modal version (§2), formats `"%d.%d.%d"`, and — for `VERS_ALLOW_TYPE` metrics — also posts the framework type (`reserved % 10`, `:484`) on `cw_id + 1`; `nmetric_post_and_reset_driver_metrics` (`:714`) computes `AVG_* = total_time / total_count` via `atomic64_xchg(…, 0)` (reset-on-read).

> **GOTCHA — the budget guard does not abort the loop.** When `available_size <= 0`, `nmetric_post_metrics` (`:852`) only `pr_err_once` — it does **not** `break` or `continue`. The per-metric helpers each independently re-check `avail < metric_size` and return `0`, so no overflow occurs, but the warning can fire while later, *smaller* metrics still fit and are written. A reimplementer who treats the warning as "buffer full → stop" will drop records that would have fit. The behaviour is safe-by-construction but counter-intuitive. (Confidence HIGH; only the design *intent* is LOW.)

---

## 4. The Post Path

### Purpose

After `post_metrics` has packed the records, the buffer goes to one or both of two sinks selected by the `nmetric_log_posts` module parameter (`:25`, default `1`): bit0 → firmware via FW-IO `POST_TO_CW`, bit1 → ftrace via `trace_metrics_post`. The firmware post is the real telemetry channel; ftrace is a debug mirror consumed by `neuron-monitor`.

### Entry point

```text
nmetric_post_metrics (:838)              ── pack records, then dispatch to sink(s)
  │  accumulates data_size into nd->metrics.posting_buffer[129]   (≤128 B)
  ├─ if (nmetric_log_posts & 2):                                  :896  TRACE sink
  │     nmetric_mock_fw_io_post_metric(buf, data_size)            :433
  │       └─ walk records, emit trace_metrics_post(id, len, data) per record   :442  [BOUNDARY → ftrace]
  └─ if (data_size && (nmetric_log_posts & 1)):                   :899  CW sink
        ndhal->ndhal_fw_io.fw_io_post_metric(nd->fw_io_ctx, buf, data_size)     :900  [BOUNDARY → FW-IO]
```

### The FW-IO POST_TO_CW boundary

The CW sink calls through the DHAL FW-IO function table to `fw_io_post_metric` (implemented at `neuron_fw_io.c:650`, [fw-io](fw-io.md)). That function stages the ≤128-byte blob into the firmware DATA window and issues a `FW_IO_CMD_POST_TO_CW` request. The 128-byte engine budget on this side is exactly the FW-IO DATA-window size on that side; the two limits are the same number by design, so a full 128-byte post fits in one firmware transaction. The post-time lock interaction (`fw_io_post_metric` already holds `ctx->lock` while staging, so `POST_TO_CW` skips the re-lock inside `execute_request`) is owned by [fw-io](fw-io.md) — this engine only hands the buffer across the boundary.

> **NOTE — the trace sink is independent of the CW sink, and the CW sink is gated on a non-empty buffer.** With `nmetric_log_posts = 3` both fire; with `= 2` only ftrace fires (the FW-IO post is skipped entirely, useful on a bench with no firmware to receive metrics). The CW post is additionally gated on `data_size != 0` (`:899`) — an empty tick (every counter skipped by the differential rule) posts nothing to firmware, while the trace sink, if enabled, still walks an empty buffer (a no-op). A reimplementer must replicate that the CW post is conditional on content while the trace dispatch is not. (Confidence HIGH.)

---

## Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `nmetric_thread_fn` | `:1047` | `[kthread]` the per-device thread body: fast power sample + drift-free slow post | HIGH |
| `nmetric_create_thread` | `:1135` | `kthread_run(…, "nd%d metrics", index)` (`:1139`) | HIGH |
| `nmetric_stop_thread` | `:1147` | `[PUBLIC]` state=STOPPED; wake; `kthread_stop` (blocking) | HIGH |
| `nmetric_set_mode` | `:1158` | `[PUBLIC]` ENABLE→RESUMING / DISABLE→PAUSED (cdev ioctl) | HIGH |
| `nmetric_init` | `:1181` | `[PUBLIC]` zero `ds_freed_*`; state=STOPPED; create thread (probe) | HIGH |
| `nmetric_init_constants_metrics` | `:264` | `[PUBLIC]` once-global: read instance id + driver version + profile | HIGH |
| `nmetric_init_driver_metrics` | `:1170` | `[PUBLIC]` `atomic64_set 0` the reset-metric arrays | HIGH |
| `nmetric_aggregate_and_post_tick` | `:1017` | one slow tick: lock → aggregate+cache → unlock → post → new session | HIGH |
| `nmetric_full_aggregate` | `:400` | walk every in-use entry; FW_IO_ERR special at `curr[17]` (`:412`) | HIGH |
| `nmetric_aggregate_nd_counter_entry` | `:352` | per-entry per-type fan-in switch (sum / LFU / OR / overwrite) | HIGH |
| `nmetric_aggregate_version_metrics` | `:303` | 8-deep LFU version history per component (`:313`) | HIGH |
| `nmetric_partial_aggregate` | `:420` | `[PUBLIC]` roll a dying entry into `ds_freed_*` (`tick=ALWAYS`); from `neuron_ds.c:161` | HIGH |
| `nmetric_check_post_tick` | `:335` | central tick gate: `tick==ALWAYS \|\| m->tick==ALWAYS \|\| tick==m->tick` | HIGH |
| `nmetric_cache_shared_bufs` | `:916` | snapshot+reset `ds_freed_*` per tick; refresh perf-profile constant (`:954`) | HIGH |
| `nmetric_post_metrics` | `:838` | mirror post switch; pack records; dispatch to trace/CW sinks (`:896-900`) | HIGH |
| `nmetric_post_counter` | `:608` | differential `crt=curr+freed`; skip `crt≤prev`; post `crt−prev` decimal | HIGH |
| `nmetric_post_utilization` | `:568` | `NC_UTILIZATION` ps→% transform; div-by-zero guard (`:577`) | HIGH |
| `nmetric_post_feature_bitmap` | `:636` | `value = curr_bm \| freed_bm`; skip 0 | HIGH |
| `nmetric_post_version` / `_with_max_usage` | `:542` / `:460` | modal version `"%d.%d.%d"`; `ALLOW_TYPE` posts fw_type on `cw_id+1` (`:484`) | HIGH |
| `nmetric_post_constant` / `_constant_u64_fmt` | `:530` / `:676` | string constant; `SKIP_ZERO`/`PREFER_FREED` u64 | HIGH |
| `nmetric_post_hex_constant_u64` | `:705` | `"%llx"` — AGG_NEFF_ID only (`:876-877`) | HIGH |
| `nmetric_post_and_reset_driver_metrics` | `:714` | reset-metric emit; `AVG_* = total/count` via `atomic64_xchg(…,0)` | HIGH |
| `nmetric_post_driver_userver_metrics` | `:744` | ultraserver mode/modes via `npe_pod_info` (`:754`); skip if no pod | HIGH |
| `nmetric_post_driver_ecc_metrics` | `:787` | HBM UE / repairable-UE via shared sysfs helper; underflow guard (`:809-816`) | HIGH |
| `nmetric_start_new_session` | `:973` | `prev := curr` baseline (`:991`); zero curr/freed/bitmaps | HIGH |
| `nmetric_sample_high_freq` | `:1012` | fast tick → `npower_sample_utilization` | HIGH |
| `nmetric_mock_fw_io_post_metric` | `:433` | trace sink: walk records → `trace_metrics_post` (`:442`) | HIGH |
| `nmetric_set_reset_time_metrics` | `:1196` | `[PUBLIC]` lock-free CAS max + `atomic64_add` total + `atomic64_inc` count (from reset) | HIGH |
| `nmetric_increment_reset_failure_count` | `:1233` | `[PUBLIC]` `atomic64_inc` the reset-fail counter (from reset) | HIGH |
| `nmetric_set_performance_profile` | `:1244` | `[PUBLIC]` snprintf profile constant — **ignores its `profile` arg** (reads ndhal field) | LOW |

> **CORRECTION (K-METRICS) —** an earlier scan (SCAN-08 §9a) left `neuron_metrics.c:245-1042` — the `enum nmetric_cw_id` body and all the posting helpers — ungrounded. This page grounds the full range: the id enum (`:42-170`), the 46-row registry (`:172-243`), every post helper (`:433-787`), and the orchestration (`:838-1042`) are read in full and pinned `file:line`. The prior "CW-id enum body ungrounded" gap is closed. A re-count of `nmetric_defs[]` against the source (`rg -c '_DEF\('` over `:172-243`, cross-checked by `nmetric_count = sizeof(nmetric_defs)/sizeof(nmetric_def_t)` at `:244`) yields **46 rows**, correcting an earlier "41 rows" figure that under-counted the registry.

> **NOTE — `nmetric_set_performance_profile` has a dead parameter.** `nmetric_set_performance_profile(nd, int profile)` (`:1244`) takes an `int profile` argument but writes `ndhal->ndhal_perf.current_performance_profile` into the profile constant, **ignoring the arg** (`:1246`). Likely intentional — the ndhal field is the single source of truth — but the unused parameter is a latent bug a reimplementer should either honour or drop, not silently mirror. (Confidence LOW on intent; HIGH that the arg is unused.)

---

## Related Components

| Component | Relationship |
|---|---|
| `neuron_ds_acquire_lock` / `get_neuroncore_counter_value` (`:1019`, `:376`) | the datastore lock + 3-band counter reader the aggregation walk uses — owned by [datastore/kernel-side](../datastore/kernel-side.md) |
| `neuron_ds.c:161 → nmetric_partial_aggregate` | the process-death capture trigger; same `neuron_ds_release_entry` site that drives [sysfs](sysfs.md)'s `nds_aggregate` |
| `ndhal->ndhal_fw_io.fw_io_post_metric` (`:900`) | the FW-IO `POST_TO_CW` sink (`neuron_fw_io.c:650`); `fw_io_get_err_count` (`:412`) the FW_IO_ERR source — owned by [fw-io](fw-io.md) |
| `ndhal->ndhal_sysfs_metrics.nsysfsmetric_get_hbm_error_count` (`:795,798`) | the shared ECC reader so the CW metric and the sysfs `mem_ecc_uncorrected` attribute agree — owned by [sysfs](sysfs.md) |
| `npower_sample_utilization` (`:1014`) | the fast-tick power sampler feeding `NC_UTILIZATION` — owned by [power](power.md) |
| `nmetric_set_reset_time_metrics` / `_increment_reset_failure_count` (`:1196`/`:1233`) | the lock-free reset-metric writers called from [reset](reset.md) |

## Cross-References

- [Kernel Side (Per-Process Slabs)](../datastore/kernel-side.md) — the NDS counter slabs this engine walks under the datastore lock; owns `get_neuroncore_counter_value` and the `neuron_ds_release_entry` process-death trigger that drives `nmetric_partial_aggregate`
- [Sysfs Metrics Tree](sysfs.md) — **owns** the `metric_id` taxonomy (`MAX_METRIC_ID`, the `NDS_*_COUNTER` namespace) and the named status-counter attributes this engine's `cw_id` band mirrors; the shared `nsysfsmetric_get_hbm_error_count` ECC reader
- [Telemetry, Metrics & Error Reporting](../trace/telemetry-errors.md) — the end-to-end telemetry plane (runtime publish → NDS slab → this engine → FW-IO → CloudWatch); this page is the kernel half of its `nmetric_full_aggregate → fw_io_post_metric` POST_TO_CW path
- [The FW-IO MiscRAM Mailbox Protocol](fw-io.md) — the `fw_io_post_metric` / `FW_IO_CMD_POST_TO_CW` boundary the packed ≤128-byte buffer is handed across; the 128-byte budget equals the FW-IO DATA-window size
- [Power Telemetry](power.md) — the fast-tick `npower_sample_utilization` source behind the `NC_UTILIZATION` metric
- [Error and Status Codes (NRT_STATUS)](../runtime/error-codes.md) — **owns** the `NRT_STATUS` value space the error-band `cw_id`s (`200..253`) crosswalk to via the inline registry comments
- [Kernel Driver — Overview](overview.md) — where the metrics engine sits in the driver's device-model surface
