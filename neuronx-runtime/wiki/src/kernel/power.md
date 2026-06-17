# Power Telemetry

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The module is read directly from `neuron_power.c` (448 lines) and `neuron_power.h` (139 lines); both files are read in full. The device-side MiscRAM accessor it calls lives in `neuron_fw_io.c` and the sysfs read callback in `neuron_sysfs_metrics.c`; both are cross-read here and cited explicitly. Every constant, struct field, aggregation step, and lock boundary below is transcribed from the shipped `.c`/`.h` — the source is read, not reverse-engineered. Other driver versions renumber lines. Part III — Kernel Driver · DEEP · [back to index](../index.md)*

## Abstract

`neuron_power.{c,h}` is the kernel driver's **power-utilization telemetry** module. It does exactly one thing: it periodically reads a per-die power-utilization number that the device firmware publishes into a MiscRAM register, folds those readings into a per-minute min/avg/max, and exposes the result as a comma-separated string through the sysfs node `neuron{N}/stats/power/utilization`. The familiar frame is a **read-only hardware monitor sensor** in the `hwmon` style: a worker thread polls a device register on a fixed cadence, keeps a small running aggregate, and a sysfs `show` callback formats the last completed window on demand.

The single most important fact about this module is what it is *not*. There is **no** power-state setter, **no** throttle, **no** clock/frequency control, **no** suspend/resume, and **no** power-budget enforcement anywhere in the file. The driver never writes a power register; `fw_io_device_power_read` is a pure `readl` of MiscRAM (`neuron_fw_io.c:103-121`). The value firmware publishes is "percent of max power with background/baseline power backed out" (`neuron_power.c:9-11`, `neuron_fw_io.h:168`) — a *measurement*, not a *control surface*. A reimplementer who expects to find DVFS here will find a sensor instead.

The second most important fact is the **unit**. Power is carried end-to-end in **basis points** — 1/100 of 1%, range `0..10000` — so that every aggregation step (sum, divide-for-average, min, max) is exact integer math with no floating point in the kernel (`neuron_power.h:25-26`, `:73-74`). The human-facing percentage is reconstructed only at the very last moment, in the sysfs formatter, by integer `÷100` for the whole part and `%100` for the two fractional digits (`neuron_power.c:420-422`). The `10000` ceiling is named `NEURON_MAX_POWER_UTIL_BIPS = (100 * 100)` (`neuron_power.h:55`); `0` is `NEURON_MIN_POWER_UTIL_BIPS` (`:56`).

For reimplementation, the contract is:

- **The device→host read path** — how a per-die `u32` is fetched from MiscRAM at `bar0 + bar0_misc_ram_offset + 0x54 + 4*die`, and how its low 16 bits (utilization) and high 16 bits (sample counter) are split apart.
- **The basis-point convention** — why the in-kernel representation is `0..10000`, why the math is integer-only, and where the `÷100`/`%100` percent conversion happens.
- **The per-minute aggregation** — the running `{count, total, min, max}` accumulator, the wall-clock *minute-boundary* trigger (not a fixed-length window), the `≥ 60` minimum-samples validity gate, and the lock-protected publish of the completed window.
- **The sysfs surface** — the `stats/power/utilization` node registration, the `show` callback that calls `npower_format_stats`, the exact CSV format, and the NULL-device fallback string.

| | |
|---|---|
| **Source** | `neuron_power.c` (448 lines) / `neuron_power.h` (139 lines), aws-neuronx-dkms 2.27.4.0, GPL-2.0 |
| **Nature** | **Telemetry-only** — read path, no power/clock/throttle control surface anywhere |
| **Unit** | **Basis points** (1/100 of 1%), range `0..10000`; `NEURON_MAX_POWER_UTIL_BIPS = 100*100` (`h:55`) |
| **Device register** | MiscRAM `FW_IO_REG_POWER_UTIL_D0_OFFSET = 0x54`, `D1 = 0x58` (`neuron_fw_io.h:175-176`); D1 only on 2-die parts |
| **Per-sample layout** | `u32`: low 16 = utilization bips, high 16 = firmware sample counter (`c:81-95`, `neuron_fw_io.h:167-170`) |
| **Per-device state** | `struct neuron_power` = `{current_samples, current_stats, stats_lock}` (`h:84-88`), one per `neuron_device` |
| **Sample cadence** | driven by metrics thread `nmetric_sample_high_freq` every `nmetric_metric_sample_delay = 50` ms (`neuron_metrics.c:24`, `:1014`) |
| **Aggregation window** | wall-clock minute boundary since epoch (`c:156-167`); validity needs `≥ NEURON_MIN_SAMPLES_PER_PERIOD = 60` (`h:61`) |
| **Sysfs node** | `neuron{N}/stats/power/utilization` (`neuron_sysfs_metrics.c:160-163`, `:944-948`) |
| **Enablement gate** | firmware API version `≥ NEURON_FW_POWER_MIN_API_VERSION = 3` (`c:23`, `:70`) |

> **QUIRK — read-only telemetry, basis-point units.** Two assumptions a reimplementer is likely to bring are both wrong. (1) This is *not* a power manager: there is no setter, no throttle, no clock control — the module only ever *reads* MiscRAM and aggregates. (2) The in-kernel number is *not* a percentage and *not* a watt count: it is a **basis-point** integer in `[0, 10000]`. Treating the stored value as a percentage inflates every reading by 100×; treating it as watts is meaningless. The `÷100`/`%100` conversion at `c:420-422` is the *only* place the human percent is produced.

---

## 1. Device → Host Read Path

### Purpose

Fetch the firmware-published power-utilization word for each die of one neuron device, gated on the firmware actually supporting power reporting, and split each word into its `{utilization, counter}` halves. This is a plain MMIO read of a MiscRAM aperture — there is no command/mailbox round-trip for power (contrast the FW-IO command engine, which power does *not* use).

### Entry Point

```text
nmetric_thread_fn (neuron_metrics.c:1047)        ── per-device "ndN metrics" kthread, 50 ms loop
  └─ nmetric_sample_high_freq (:1012)            ── the "fast tick"
       └─ npower_sample_utilization (neuron_power.c:302)   ── THIS module's entry
            ├─ npower_enabled_in_fw (:47)         ── API-version gate (MMIO read of reg 0x00)
            ├─ fw_io_device_power_read (neuron_fw_io.c:103) ── readl of MiscRAM 0x54 + 4*die
            ├─ npower_select_power (:269)         ── split words, dedup, pick largest
            └─ npower_aggregate_stats (:238)      ── on minute boundary, publish window
```

### Algorithm

The MiscRAM accessor is a one-shot `readl` through the DHAL function pointer; it does not poll a doorbell and does not use the firmware mailbox command path.

```c
function fw_io_device_power_read(bar0, *power, die):   // neuron_fw_io.c:103
    if die >= ndhal->ndhal_address_map.dice_per_device: // :107 — bounds-check the die index
        return -EINVAL                                  // :109
    // Per-die words are contiguous u32 MiscRAM registers, so address as an array. (:112-113)
    addr = bar0
         + ndhal->ndhal_address_map.bar0_misc_ram_offset // device's MiscRAM aperture base
         + FW_IO_REG_POWER_UTIL_D0_OFFSET                // 0x54 (neuron_fw_io.h:175)
         + 4 * die                                       // :114 — D1 lands at 0x58
    ret = ndhal->ndhal_fw_io.fw_io_read_csr_array(&addr, power, 1, /*operational=*/true) // :115
    if ret: pr_err("failed to get device power ...")     // :116-118
    return ret
```

The module's own per-tick driver reads every die, but only commits if it can read *all* dice and the firmware supports power:

```c
function npower_sample_utilization(dev):           // neuron_power.c:302
    nd = (struct neuron_device *)dev
    power_samples[NEURON_POWER_MAX_DIE] = {0}       // :304 — MAX_DIE = 2 (h:12)
    failed_read = false

    // Bail entirely during reset or on QEMU/emulator — firmware may be unreachable. (:315-320)
    if nd->device_state != NEURON_DEVICE_STATE_READY  // READY == 1 (neuron_device.h:59)
       || npower_in_simulated_env():                  // narch_is_qemu() || narch_is_emu() (:264-267)
        return -1

    if npower_enabled_in_fw(nd):                       // :325 — API-version gate, see §below
        for die in 0 .. dice_per_device-1:             // :328
            ret[die] = fw_io_device_power_read(nd->npdev.bar0, &power_samples[die], die) // :329
            if ret[die]:
                failed_read = true                     // :332 — defer the decision to commit
                // Rate-limited error log: once per 60 s, count the rest. (:335-343)
    else:
        // Rate-limited info log: once per 30 min when firmware lacks power support. (:347-354)

    if !failed_read:                                   // :358 — only commit a complete read
        npower_select_power(nd, power_samples)         // :359

    ktime_get_real_ts64(&curr_time)                    // :365 — real (wall-clock) time
    if npower_passed_minute_boundary(nd, &curr_time):  // :366
        npower_aggregate_stats(nd, &curr_time,
                               nd->power.current_samples.last_counter) // :367

    return (failed_read == true)                       // :370 — nonzero iff any die read failed
```

The enablement gate is itself a MiscRAM read of register `0x00` (the firmware API version), deliberately *not* cached across calls so a firmware rollback cannot strand the driver in a stale "enabled" state:

```c
function npower_enabled_in_fw(nd):                  // neuron_power.c:47
    extern unsigned int nmetric_log_posts            // :52
    // If metric posting is globally disabled (bringup HW / simulation), power is off. (:54-58)
    if !nmetric_log_posts: return false              // :56

    ret = fw_io_api_version_read(nd->npdev.bar0, &api_version_num) // :63 — readl of reg 0x00
    if ret: pr_err("Failed to read firmware API version ...")      // :64-66

    // Cache the bool so cheap callers (e.g. "should I log?") need not re-read MMIO. (:68-69)
    power_enabled_in_fw = (ret == 0) &&
                          (api_version_num >= NEURON_FW_POWER_MIN_API_VERSION) // :70 — MIN == 3
    return power_enabled_in_fw                        // :72
```

The two half-word extractors are trivial but define the on-wire layout:

```c
static inline u16 npower_get_utilization(u32 sample):  // :81 — low 16 bits = power bips
    return (u16)(sample & 0xFFFF)                       // :83
static inline u16 npower_get_sample_num(u32 sample):    // :92 — high 16 bits = fw sample counter
    return (u16)((sample >> 16) & 0xFFFF)               // :94
```

### Considerations

> **NOTE — the read is gated on three independent conditions.** A sample is taken only when (1) `device_state == READY` and not on QEMU/emu (`c:318`), (2) `nmetric_log_posts != 0` (`c:56`), and (3) firmware API version `≥ 3` (`c:70`). All three are re-evaluated every 50 ms tick; the API-version read is intentionally uncached (`c:60-62`) so a firmware downgrade is observed within one tick.

> **GOTCHA — `NEURON_POWER_MAX_DIE` (2) is a buffer bound, not the live die count.** Arrays are sized `[NEURON_POWER_MAX_DIE]` (`h:12`, `c:304`), but every loop iterates `ndhal->ndhal_address_map.dice_per_device` (`c:328`, `:136`, `:277`). On v2 silicon `dice_per_device == 1` (`v2/neuron_dhal_v2.c:1395`, `V2_NUM_DIE_PER_DEVICE`); on v3 it is `2` (`v3/neuron_dhal_v3.c:1887`, `V3_NUM_DIE_PER_DEVICE`). A reimplementation that drives loops off the array length will read the unused D1 slot on single-die parts.

---

## 2. Basis-Point Convention

### Purpose

Keep all aggregation in exact integer arithmetic. Power utilization has two-decimal-place resolution as published by firmware; representing `37.42%` as the integer `3742` lets the kernel sum, average (with integer division), min, and max without ever touching floating point — which the kernel avoids by policy. The percent is reconstructed only at the formatting boundary.

### Algorithm

The convention is defined entirely by named constants and the final format expression. There is no runtime "conversion function"; the representation simply *is* bips until `snprintf`:

```c
// neuron_power.h
#define NEURON_MAX_POWER_UTIL_BIPS (100 * 100)   // :55 — 10000 == 100.00%
#define NEURON_MIN_POWER_UTIL_BIPS 0             // :56 — 0 == 0.00%
// "expressed in basis points so that we can stick to integer math
//  while preserving resolution" (:25-26, :73-74)

// neuron_power.c — the ONLY bips -> percent conversion, in the sysfs formatter (:418-422)
snprintf(buffer, bufflen, "%.32s,%lld,%u.%02u,%u.%02u,%u.%02u",
         status_string, time_of_sample_sec,
         stats.min_power_bips / 100, stats.min_power_bips % 100,   // whole.frac for MIN
         stats.max_power_bips / 100, stats.max_power_bips % 100,   // MAX
         stats.avg_power_bips / 100, stats.avg_power_bips % 100);  // AVG
```

Validation of incoming samples is against the bips ceiling, not a percentage:

```c
function npower_store_utilization(nd, utilization, counter[]):  // :107
    if utilization > NEURON_MAX_POWER_UTIL_BIPS:   // :113 — reject anything above 10000 bips
        // Rate-limited error, then drop the sample. "Should never happen." (:114-124)
        return -1
    ...
```

### Considerations

The storage widths follow directly from the bips range. A single utilization sample is `u16` (`max 10000 < 65535`): `min_power_bips`/`max_power_bips` are `u16` (`h:31-32`, `:81-82`). The *sum* of samples is `u64` (`total_power_util_bips`, `h:30`) so it cannot overflow across a window. The published *average* is `u32` (`avg_power_bips`, `h:79`) even though it can never exceed 10000 — a deliberate width margin, harmless.

> **NOTE — why integer division for the average is safe.** `avg = total / count` (`c:196-197`) with `total : u64` and `count : u32`. Because every sample is `≤ 10000` and `count ≥ 60` for a valid window, `avg ≤ 10000` and fits the `u32` (and is truncating-toward-zero, which loses at most 0.01% of resolution — below the published precision). The `%100` fractional digits in the formatter therefore always print `00..99`.

---

## 3. Per-Minute Min / Avg / Max Aggregation

### Purpose

Convert the stream of individual samples into one `{min, avg, max, status, time}` record per wall-clock minute. The window is *not* a fixed number of samples or a fixed jiffy interval — it is "the top of each minute since epoch", an explicit customer request (`c:152-154`). Samples accumulate into a running aggregate; when the next tick observes that the wall clock has crossed a minute boundary, the aggregate is finalized into the published stats and reset.

### Algorithm — the running accumulator

Each accepted sample updates a `struct neuron_power_samples` in place. Note the inverted initialization of `min`/`max` so the first real sample always wins both comparisons:

```c
function npower_init_samples(samples, last_count[]):   // :34
    samples->num_data_points    = 0                      // :38
    samples->total_power_util_bips = 0                   // :39
    samples->min_power_bips = NEURON_MAX_POWER_UTIL_BIPS // :40 — start min HIGH (10000)
    samples->max_power_bips = NEURON_MIN_POWER_UTIL_BIPS // :41 — start max LOW  (0)
    for die in 0 .. dice_per_device-1:                   // :42
        samples->last_counter[die] = last_count[die]     // :43 — seed dedup counters

function npower_store_utilization(nd, utilization, counter[]):  // :107
    if utilization > NEURON_MAX_POWER_UTIL_BIPS: return -1       // :113 (see §2)
    s = &nd->power.current_samples
    s->total_power_util_bips += utilization               // :126 — running sum (u64)
    s->num_data_points++                                  // :127 — running count
    if utilization < s->min_power_bips: s->min_power_bips = utilization  // :128-130
    if utilization > s->max_power_bips: s->max_power_bips = utilization  // :131-133
    for die in 0 .. dice_per_device-1:                    // :136
        s->last_counter[die] = counter[die]               // :137 — remember fw sample numbers
    return 0
```

### Algorithm — which sample to store (per-die dedup + max-of-dice)

Firmware advances a 16-bit counter (the high half-word) each time it publishes a fresh value. The driver samples faster than firmware updates, so it must drop repeats. The rule: if *any* die's counter is unchanged since last store, treat the whole multi-die read as a duplicate and skip it; otherwise store the **largest** utilization seen across the dice this tick.

```c
function npower_select_power(nd, power_samples[]):   // :269
    duplicate_read = false
    largest_power  = 0
    for die in 0 .. dice_per_device-1:                // :277
        current_power       = npower_get_utilization(power_samples[die])  // :278 low16
        current_counters[die] = npower_get_sample_num(power_samples[die]) // :279 high16
        if current_counters[die] == nd->power.current_samples.last_counter[die]: // :281
            duplicate_read = true                     // :282 — fw hasn't advanced this die
        else if current_power > largest_power:        // :283
            largest_power = current_power             // :284
    if !duplicate_read:                               // :289 — only when ALL dice are fresh
        npower_store_utilization(nd, largest_power, current_counters)     // :290
```

### Algorithm — minute-boundary trigger and finalize

```c
function npower_passed_minute_boundary(nd, curr_time):   // :156
    curr_minute = curr_time->tv_sec / 60                  // :161 — integer minute since epoch
    last_minute = nd->power.current_stats.time_of_sample.tv_sec / 60     // :162
    return curr_minute != last_minute                     // :163-166

function npower_calculate_stats(current_samples, *new_stats, curr_time): // :186
    if current_samples->num_data_points >= NEURON_MIN_SAMPLES_PER_PERIOD: // :192 — >= 60
        new_stats->status        = POWER_STATUS_VALID                      // :193
        new_stats->min_power_bips = current_samples->min_power_bips         // :194
        new_stats->max_power_bips = current_samples->max_power_bips         // :195
        new_stats->avg_power_bips =
            current_samples->total_power_util_bips / current_samples->num_data_points // :196-197
    else:
        // Too few samples: emit a sentinel "no data" window. min was init'd to 10000,
        // so clamp the *logged* min down to max to avoid a misleading 100.00%. (:199-204)
        new_stats->status        = POWER_STATUS_NO_DATA                     // :212
        new_stats->min_power_bips = NEURON_MIN_POWER_UTIL_BIPS              // :213 — 0
        new_stats->max_power_bips = NEURON_MIN_POWER_UTIL_BIPS              // :214 — 0
        new_stats->avg_power_bips = NEURON_MIN_POWER_UTIL_BIPS              // :215 — 0
    new_stats->time_of_sample = *curr_time                                  // :217-218

function npower_aggregate_stats(nd, curr_time, current_fw_counter[]):       // :238
    // Compute OUTSIDE the lock to minimize contention with sysfs readers. (:241-242)
    struct neuron_power_stats new_stats
    npower_calculate_stats(&nd->power.current_samples, &new_stats, curr_time) // :244
    npower_init_samples(&nd->power.current_samples, current_fw_counter)        // :247 — reset window
    npower_acquire_lock(nd)                                                    // :250 — mutex
    npower_copy_stats(&new_stats, &nd->power.current_stats)                    // :254 — publish (*dst = *src, :229)
    npower_release_lock(nd)                                                    // :255
```

### Considerations

> **GOTCHA — the window is wall-clock, not sample-count or jiffy-based.** The boundary test compares `tv_sec / 60` between now and the last published window (`c:161-163`) using `ktime_get_real_ts64` — *real* time, which NTP and timezone changes can move. The source acknowledges this: a clock adjustment can produce a minute with more or fewer samples than usual (`c:152-154`, `h:67`). A reimplementation that uses a fixed 60-second timer instead of the epoch-minute boundary will *not* be bit-compatible — readings will land at different wall-clock instants and the "top of each minute" guarantee is lost.

> **GOTCHA — `≥ 60` samples needed, but firmware update rate, not tick rate, gates it.** The fast tick runs every 50 ms (`neuron_metrics.c:24`), so ~1200 ticks land in a minute — far above the 60-sample floor (`h:61`). But `npower_select_power` drops every tick where firmware has *not* advanced its counter (`c:281-289`). If firmware publishes slower than ~1 Hz, a window can fall below 60 accepted samples and be marked `POWER_STATUS_NO_DATA` (`c:212`) even though the hardware is healthy. The minimum is on *accepted* samples, not on ticks.

> **NOTE — no overflow risk in the accumulator.** Worst case per window: `count` is bounded by the tick rate (~1200/min, a `u32`), and `total` accumulates `count × ≤10000` into a `u64` — astronomically far from `2^64`. The only width that is "tight" is `u16` for an individual sample, validated `≤ 10000` before it is stored (`c:113`), so it can never wrap a `u16`.

---

## 4. Sysfs `power_utilization` Exposure

### Purpose

Surface the last completed per-minute window to userspace as a single readable line. The node lives in the metrics sysfs tree, not in a separate power class — consistent with the module being telemetry, not control.

### Entry Point

```text
nsysfsmetric_init_and_add_one_node (neuron_sysfs_metrics.c:944)  ── creates "power" node under stats/
  attr table: power_utilization_attrs_info_tbl (:160)            ── single attr "utilization"
                                                                     metric id NON_NDS_OTHER_POWER_UTILIZATION

read()  -> sysfs show callback (:377)                            ── on cat of .../stats/power/utilization
  └─ npower_format_stats(nd, buffer, 256) (neuron_power.c:396)   ── format last window
       └─ npower_get_stats(nd, &stats) (:382)                    ── lock + copy current_stats out
```

### Algorithm — node registration

The node is registered once at metrics init. The full sysfs path is `neuron{N}/stats/power/utilization` (`neuron_sysfs_metrics.c:944-948`, `:160-161`):

```c
// neuron_sysfs_metrics.c
static const nsysfsmetric_attr_info_t power_utilization_attrs_info_tbl[] = {  // :160
    ATTR_INFO("utilization",
              NON_NDS_ID_TO_SYSFS_METRIC_ID(NON_NDS_OTHER_POWER_UTILIZATION), OTHER), // :161
};
...
struct nsysfsmetric_node *power_node =
    nsysfsmetric_init_and_add_one_node(metrics, stats_node, "power", false, -1,       // :945-948
                                       power_utilization_attrs_info_tbl_cnt,
                                       power_utilization_attrs_info_tbl);
```

### Algorithm — the show callback and CSV format

```c
// sysfs "show" dispatch on metric id NON_NDS_OTHER_POWER_UTILIZATION (neuron_sysfs_metrics.c:377)
nd = container_of(sysfs_metrics, struct neuron_device, sysfs_metrics)  // :378
char buffer[256]                                                       // :380
int ret = npower_format_stats(nd, buffer, 256)                          // :381
if ret: pr_err("sysfs failed to read power stats from FWIO ...")        // :382-384
len = nsysfsmetric_sysfs_emit(buf, "%s\n", buffer)                      // :385

function npower_get_stats(dev, *stats):              // neuron_power.c:382
    nd = (struct neuron_device *)dev
    npower_acquire_lock(nd)                            // :386 — mutex, pairs with aggregate publish
    npower_copy_stats(&nd->power.current_stats, stats) // :387 — copy OUT under lock
    npower_release_lock(nd)                            // :388

function npower_format_stats(dev, buffer, bufflen):  // :396
    if !dev:                                          // :402 — NULL-device fallback
        jiffies_to_timespec64(jiffies, &currtime)     // :404
        snprintf(buffer, bufflen, "%.32s,%lld,0.00,0.00,0.00",  // :405-406
                 status_string[POWER_STATUS_NO_DATA], currtime.tv_sec)
        return -1                                     // :407
    npower_get_stats(dev, &stats)                      // :410 — locked copy
    status = stats.status
    if status > POWER_STATUS_MAX: status = POWER_STATUS_MAX  // :413-415 — clamp before stringify
    bytes = snprintf(buffer, bufflen,
        "%.32s,%lld,%u.%02u,%u.%02u,%u.%02u",          // :418 — STATUS,EPOCH,MIN,MAX,AVG
        status_string[status], stats.time_of_sample.tv_sec,
        stats.min_power_bips/100, stats.min_power_bips%100,   // :420
        stats.max_power_bips/100, stats.max_power_bips%100,   // :421
        stats.avg_power_bips/100, stats.avg_power_bips%100)   // :422
    return (bytes > bufflen) || (bytes == bufflen && buffer[bufflen-1] != '\0') // :424-425 — truncation flag
```

The status string is produced by an X-macro stringifier table (`POWER_STATUS_VALID`, `POWER_STATUS_NO_DATA`, `POWER_STATUS_INVALID`, `POWER_STATUS_MAX`) generated from `FOREACH_POWER_STATUS` (`neuron_power.h:41-50`, `c:394`), so the enum and its names cannot drift apart.

### Considerations

The emitted line is a fixed six-field CSV: `STATUS,EPOCH_SECONDS,MIN.ff,MAX.ff,AVG.ff` where each power figure is `whole.fraction` percent. A reader on a freshly-initialized device (before the first valid minute) sees `POWER_STATUS_NO_DATA,...,0.00,0.00,0.00` because `npower_init_stats` seeds `status = POWER_STATUS_NO_DATA` (`c:444`).

> **NOTE — the only lock is around the published stats, not the accumulator.** `stats_lock` (`h:87`) is held *only* while copying the completed `current_stats` in (`c:250-255`) or out (`c:386-388`). The running `current_samples` accumulator is updated lock-free because the metrics kthread is its sole writer; only the publish/read of `current_stats` races between that kthread and a sysfs reader. Computing `new_stats` *before* taking the lock (`c:241-244`) keeps the critical section to a single struct copy.

> **CORRECTION (POWER-1) — init/lifecycle wiring is not in this DKMS tree.** `npower_init_stats` (`c:428`) and the `mutex_init` for `nd->power.stats_lock` are defined/declared here but have **no caller** anywhere in the shipped `aws-neuronx-2.27.4.0/` source (whole-tree `rg` finds references only in `neuron_power.{c,h}`, `neuron_metrics.c`, `neuron_sysfs_metrics.c`, and none invoke `npower_init_stats` or initialize the mutex). They are called from device-bring-up code not included in this package. Treated as **MEDIUM** confidence: the functions are certain (read from source), but *where they are invoked* is inferred, not observed. A reimplementer must wire `npower_init_stats(nd)` and `mutex_init(&nd->power.stats_lock)` into device init themselves.

---

## Function Map

| Function | File:Line | Role | Confidence |
|---|---|---|---|
| `npower_init_samples` | `neuron_power.c:34` | Reset the running accumulator; init `min`=10000, `max`=0, seed dedup counters | CERTAIN |
| `npower_enabled_in_fw` | `neuron_power.c:47` | API-version gate; MMIO-read firmware version, cache `power_enabled_in_fw` | CERTAIN |
| `npower_get_utilization` | `neuron_power.c:81` | Extract low 16 bits (utilization bips) from a sample word | CERTAIN |
| `npower_get_sample_num` | `neuron_power.c:92` | Extract high 16 bits (firmware sample counter) from a sample word | CERTAIN |
| `npower_store_utilization` | `neuron_power.c:107` | Validate `≤10000`, fold one sample into `{sum,count,min,max}` | CERTAIN |
| `npower_passed_minute_boundary` | `neuron_power.c:156` | True when wall-clock `tv_sec/60` differs from last window | CERTAIN |
| `npower_acquire_lock` / `npower_release_lock` | `neuron_power.c:169` / `:174` | `mutex_lock`/`unlock` on `stats_lock` | CERTAIN |
| `npower_calculate_stats` | `neuron_power.c:186` | Finalize a window: `VALID` if `≥60` samples else `NO_DATA` sentinel | CERTAIN |
| `npower_copy_stats` | `neuron_power.c:227` | `*dst = *src` struct copy of stats | CERTAIN |
| `npower_aggregate_stats` | `neuron_power.c:238` | Compute new stats, reset accumulator, lock+publish into `current_stats` | CERTAIN |
| `npower_in_simulated_env` | `neuron_power.c:264` | True on QEMU/emulator (`narch_is_qemu \|\| narch_is_emu`) | CERTAIN |
| `npower_select_power` | `neuron_power.c:269` | Per-die dedup by counter; store largest power when all dice fresh | CERTAIN |
| `npower_sample_utilization` | `neuron_power.c:302` | Public per-tick entry: gate, read all dice, select, minute-boundary aggregate | CERTAIN |
| `npower_get_stats` | `neuron_power.c:382` | Lock + copy `current_stats` out to caller | CERTAIN |
| `npower_format_stats` | `neuron_power.c:396` | Public: format last window as `STATUS,EPOCH,MIN,MAX,AVG` CSV; bips→percent | CERTAIN |
| `npower_init_stats` | `neuron_power.c:428` | Public: init accumulator + set status `NO_DATA` at boot (caller not in this tree) | HIGH |
| `fw_io_device_power_read` | `neuron_fw_io.c:103` | MiscRAM `readl` of `0x54 + 4*die`; the device→host fetch | CERTAIN |
| `fw_io_api_version_read` | `neuron_fw_io.c:123` | MiscRAM `readl` of `0x00`; backs the enablement gate | CERTAIN |
| sysfs `show` (power case) | `neuron_sysfs_metrics.c:377` | Read callback: calls `npower_format_stats`, emits the line | CERTAIN |

---

## Cross-References

- [Metrics Aggregation](metrics.md) — owns the `ndN metrics` kthread that calls `npower_sample_utilization` every 50 ms (`nmetric_sample_high_freq`, `:1014`) and the `nmetric_log_posts` knob that gates power reporting.
- [Sysfs Metrics Tree](sysfs.md) — owns the `neuron{N}/stats/...` node hierarchy and the `nsysfsmetric_*` registration helpers that mount `stats/power/utilization`.
- [FW-IO MiscRAM Mailbox Protocol](fw-io.md) — owns `fw_io_device_power_read` / `fw_io_api_version_read`, the MiscRAM register map (`0x54`/`0x58`/`0x00`), and the `fw_io_read_csr_array` DHAL accessor power rides on.
- [Telemetry, Metrics & Error Reporting](../trace/telemetry-errors.md) — the end-to-end telemetry plane this module is the power-sensor leaf of.
