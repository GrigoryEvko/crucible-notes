# Sysfs Metrics Tree

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. The counter tree is read directly from `neuron_sysfs_metrics.c` (1160 lines) and `neuron_sysfs_metrics.h` (224 lines); both files are read in full. The source is read, not reverse-engineered — every node-name string, attribute-group leaf, `enum` value, struct field, and lock is transcribed from the shipped `.c`/`.h`. The metric-id arithmetic derives from `neuron_sysfs_metrics.h:11-16` joined to the shared counter enums in `share/neuron_driver_shared.h`; that recomputation is HIGH-confidence and noted where it is a hand-derivation rather than a literal. Other driver versions renumber lines and shift the enum. Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

`neuron_sysfs_metrics.c` builds the read-only-mostly counter tree under each accelerator's sysfs node — the directory hierarchy a userspace agent walks at `/sys/class/neuron_device/neuronN/` (equivalently `/sys/devices/virtual/neuron_device/neuronN/`) to read per-NeuronCore and per-device telemetry: inference success/failure tallies, host/device memory usage by category, ECC error counts, power and PE-array activity, model-load and time-in-use counters. The tree is a forest of **directory kobjects** ("nodes"), each exposing a small `attribute_group` of files. The common leaf shape is the triple `total` / `present` / `peak`; a handful of leaves expose a single `OTHER` file (`arch_type`, `sram_ecc_uncorrected`, `serial_number`, `utilization`, `pe_cntrs`, `notify_delay`) read live from the firmware-IO, DHAL, or power subsystems instead of from an in-memory counter. The whole structure is registered at device-probe time by `nsysfsmetric_register` (`neuron_sysfs_metrics.c:883`), called from the cdev node builder ([cdev-mmap](cdev-mmap.md)) at `neuron_cdev.c:3683`, and torn down at PCI-remove by `nsysfsmetric_destroy` (`:992`).

The closest familiar frame is a `/proc`-style telemetry surface fused with a static schema: every numeric file is backed by one `struct nsysfsmetric_counter {total, present, peak}` (`neuron_sysfs_metrics.h:86-91`) indexed by a `(metric_id, nc_id)` pair, and the in-memory counter arrays live inside the device's embedded `struct nsysfsmetric_metrics`. A `show` reads the counter word and prints `"%llu\n"`; a `store` (any write) **resets** the counter to zero. Counter *values* are not produced here — they are pushed in from the rest of the driver via `inc`/`dec`/`set` (memory pool, reset path) and bulk-aggregated from the [datastore slabs](../datastore/kernel-side.md) when an owning process exits (`nsysfsmetric_nds_aggregate`, `:1127`). This page owns the **node tree, the metric-id taxonomy, the counter struct, and the static + dynamic build paths**; it is the reference [error-codes](../runtime/error-codes.md) cites for the userspace-visible names of the `NDS_*_COUNTER` error mirror, and the surface [datastore/kernel-side](../datastore/kernel-side.md) folds its slab counters into.

For reimplementation, the contract is:

- **The node tree** — the device-level subtree (`info/`, `stats/{memory_usage,hardware,power}`) plus one `neuron_core{N}/` subtree per enabled NeuronCore (`stats/{status,memory_usage,other_info,tensor_engine}`, `info/architecture`), and which DHAL-injected leaves are platform-conditional.
- **The metric-id flat space** — the three-category arithmetic that folds `(category, id)` into a single `metric_id ∈ [0, 136)` index into the counter arrays, and which ids are per-NC vs whole-ND.
- **The counter struct and its `(metric_id, nc_id)` indexing** — `nrt_metrics[MAX_METRIC_ID][8]` (per-NC), `nrt_nd_metrics[MAX_METRIC_ID]` (whole-ND), each cell a `{node, total, present, peak}`.
- **The two build paths** — the static skeleton built at `register` time from `counter_node_info` tables, and the dynamic per-`metric_id` node-creation path driven by a runtime-supplied bitmap (which is **non-functional** as shipped — see the NULL-deref callout).

| | |
|---|---|
| **Owning TU** | `neuron_sysfs_metrics.c` (1160 lines) / `neuron_sysfs_metrics.h` (224 lines), GPL-2.0, dkms 2.27.4.0 |
| **Per-device state** | `struct nsysfsmetric_metrics` embedded in `neuron_device.sysfs_metrics` (`neuron_device.h:116`) |
| **Build entry** | `nsysfsmetric_register(nd, &device->kobj)` (`:883`), from `neuron_cdev.c:3683` |
| **Teardown entry** | `nsysfsmetric_destroy(nd)` (`:992`), from `neuron_cdev.c:3715` |
| **Root kobject** | the cdev `device` kobject (`/sys/class/neuron_device/neuronN/`); struct-copied into `root.kobj` (`:804`) |
| **Counter cell** | `struct nsysfsmetric_counter {node, u64 total, present, peak}` — 32 B (`:h86-91`) |
| **Counter arrays** | `nrt_metrics[MAX_METRIC_ID][8]` per-NC · `nrt_nd_metrics[MAX_METRIC_ID]` whole-ND (`:h105-106`) |
| **`MAX_METRIC_ID`** | **136** = `NDS_ND_COUNTER_COUNT(31) + NDS_EXT_NC_COUNTER_LAST(41) + NON_NDS_COUNTER_COUNT(64)` (`:h11`) |
| **Leaf shape** | `total`/`present`/`peak` (counter) or a single `OTHER` file (live read) |
| **File mode** | `0644` (`S_IWUSR\|S_IRUGO`, `:488`) — world-readable; any write **resets** the counter to 0 |
| **Change-notify** | global `bool nsysfsmetric_notify = false` (`:63`) gates `sysfs_notify()`; toggled via the `notify_delay` write |
| **Confidence** | HIGH — both files read in full; all node names, attr types, struct layouts, and enum values are verbatim |

---

## 1. The Node Tree

### The kobject hierarchy

The root is the cdev `device` kobject itself — `nsysfsmetric_init_and_add_root_node` does a *struct copy* of the passed `kobject` into `root.kobj` (`:804`) and marks `is_root = true`, so the tree hangs off `/sys/class/neuron_device/neuronN/` directly with no extra "metrics" intermediate directory. Every other node is a child `struct nsysfsmetric_node` whose `kobj` is `kobject_init_and_add`'d under its parent (`:573`). The diagram below is the full static skeleton built by `register`; `{BOUNDARY}` marks subtrees whose existence and leaf set come from the DHAL ([dhal-core](dhal-core.md)) and so vary by platform.

```text
/sys/class/neuron_device/neuronN/            ← root = cdev device kobj (:804, is_root)
├── info/                                     (:895)
│   └── architecture/                         (:901)  root_arch_node_attrs_info_tbl (:154)
│       ├── arch_type        (OTHER)          NON_NDS_OTHER_NEURON_ARCH_TYPE       → "%s\n"
│       ├── instance_type    (OTHER)          NON_NDS_OTHER_NEURON_INSTANCE_TYPE   → "%s\n"
│       ├── device_name      (OTHER)          NON_NDS_OTHER_NEURON_DEVICE_NAME     → "%s\n"
│       └── … {BOUNDARY: ndhal root_info_node_attrs_info_tbl — count+leaves from DHAL}
├── stats/                                    (:908)
│   ├── memory_usage/                         (:914)
│   │   └── host_mem/                         (:920)  host_mem_category_counter_nodes_info_tbl (:118)
│   │       ├── dma_buffers   (total/present/peak)
│   │       ├── tensors            "         each = NON_NDS_ND_COUNTER_MEM_USAGE_*_HOST
│   │       ├── constants          "
│   │       ├── application_memory "
│   │       ├── driver_memory      "
│   │       ├── notifications      "
│   │       ├── dma_rings          "
│   │       └── uncategorized      "
│   ├── hardware/      {BOUNDARY: ndhal add_ecc_nodes → "hardware" (:932)} ecc_attrs_info_tbl (:147)
│   │   ├── sram_ecc_uncorrected            (OTHER)  NON_NDS_COUNTER_ECC_SRAM_UNCORRECTED → fw_io_ecc_read
│   │   ├── mem_ecc_uncorrected             (OTHER)  NON_NDS_COUNTER_ECC_HBM_UNCORRECTED
│   │   └── mem_ecc_repairable_uncorrected  (OTHER)  NON_NDS_COUNTER_ECC_REPAIRABLE_HBM_UNCORRECTED
│   └── power/                                (:945)  power_utilization_attrs_info_tbl (:161)
│       └── utilization      (OTHER)          NON_NDS_OTHER_POWER_UTILIZATION      → npower_format_stats
│
└── neuron_core{N}/   ← one per enabled nc_id in dev_nc_map (:640)   name "neuron_core%d" (:646)
    ├── stats/                                (:654)
    │   ├── status/                           (:661)  status_counter_nodes_info_tbl (:75) — 25 leaves
    │   │   ├── success                  (total/present)  NDS_NC_COUNTER_INFER_SUCCESS
    │   │   ├── failure / timeout / exec_bad_input / hw_error
    │   │   ├── execute_completed_with_error / execute_completed_with_num_error
    │   │   ├── generic_error / resource_error / resource_nc_error
    │   │   ├── execute_failed_to_queue / invalid_error / unsupported_neff_version
    │   │   ├── oob_error / hw_collectives_error / hw_hbm_ue_error / hw_nc_ue_error
    │   │   ├── hw_dma_abort_error / execute_sw_nq_overflow
    │   │   ├── execute_sw_semaphore_error / execute_sw_event_error
    │   │   ├── execute_sw_psum_collision / execute_sw_sequencer_fatal
    │   │   └── hw_repairable_hbm_ue_error
    │   ├── memory_usage/                      (:673)
    │   │   ├── host_mem/    (total/present/peak)  NON_NDS_COUNTER_HOST_MEM        (:679)
    │   │   └── device_mem/  (total/present/peak)  NON_NDS_COUNTER_DEVICE_MEM      (:685)
    │   │       └── … device_mem_category_counter_nodes_info_tbl (:103) — 11 leaves:
    │   │           model_code, tensors, constants, model_shared_scratchpad,
    │   │           runtime_memory, driver_memory, collectives, nonshared_scratchpad,
    │   │           notifications, dma_rings, uncategorized   (each total/present/peak)
    │   ├── other_info/                        (:697)  custom_counter_nodes_info_tbl (:131) — 6 leaves
    │   │   ├── model_load_count  (total/present)  NON_NDS_COUNTER_MODEL_LOAD_COUNT
    │   │   ├── reset_req_count        "           NON_NDS_COUNTER_RESET_REQ_COUNT
    │   │   ├── reset_fail_count       "           NON_NDS_COUNTER_RESET_FAIL_COUNT
    │   │   ├── inference_count        "           NON_NDS_COUNTER_INFERENCE_COUNT
    │   │   ├── flop_count             "           NDS_NC_COUNTER_MAC_COUNT  (stored as 2×MAC)
    │   │   └── nc_time_in_use         "           NDS_NC_COUNTER_TIME_IN_USE
    │   └── tensor_engine/  {BOUNDARY: ndhal add_tensor_engine_node (:709)} tensor_engine_attrs_info_tbl (:166)
    │       └── pe_cntrs     (OTHER)            NON_NDS_COUNTER_PE_ARRAY_ACTIVITY  → pe_format_activity_stats
    └── info/                                  (:715)
        └── architecture/                      (:721)  arch_info_attrs_info_tbl (:142)
            └── arch_type    (OTHER)            NON_NDS_OTHER_NEURON_ARCH_TYPE (per-NC suffix)
```

> **NOTE —** `dev_nc_map` (`:640`) and `ndhal->ndhal_address_map.nc_per_device` gate how many `neuron_core{N}/` subtrees exist: a V2 platform (Trn1/Inf2) exposes 2, a V3/V4 platform up to 8 (`MAX_NC_PER_DEVICE = 8`, `neuron_device.h:11`). The `hardware/`, `power/`, and `tensor_engine/` nodes plus the trailing `info/architecture` leaves are installed through the `ndhal->ndhal_sysfs_metrics` function table ([dhal-core](dhal-core.md), `neuron_dhal.h:112-134`), so their precise membership is a DHAL fact, not a fact of this file. The skeleton above is what `register` builds for a fully-populated platform.

> **QUIRK —** the tree carries two distinct leaf shapes that a walker must distinguish by *file presence*, not by directory name. A **counter** node has `total`+`present` (and usually `peak`) regular files; an **OTHER** node has a single oddly-named file (`arch_type`, `utilization`, `pe_cntrs`, …) whose `show` reads live hardware/firmware state. A monitoring agent cannot assume every leaf under `stats/` has a `total` — `stats/hardware/` and `stats/power/` are OTHER nodes. The discriminant is `attr_type` on each attribute (`:h24-29`), not the path.

### Leaf attribute files and value formats

The numeric leaf file names are the macro-defined literals `"total"` (`:25`), `"present"` (`:27`), `"peak"` (`:29`); a counter node has 2 or 3 of them depending on its table's `attr_cnt`. The OTHER leaves print live values:

| Leaf file | Node | `show` source | Format | `file:line` |
|---|---|---|---|---|
| `total` / `present` / `peak` | any counter node | in-memory `nsysfsmetric_counter` word | `"%llu\n"` | `:261/284/306` |
| `arch_type` / `instance_type` / `device_name` | `info/architecture` | `nsysfsmetric_get_neuron_architecture` (DHAL suffixes) | `"%s\n"` | `:237`, `:333` |
| `sram_ecc_uncorrected` | `stats/hardware` | `fw_io_ecc_read` masked `& 0x0000ffff` | `"%u\n"` | `:347` |
| `mem_ecc_uncorrected` / `mem_ecc_repairable_uncorrected` | `stats/hardware` | `ndhal nsysfsmetric_get_hbm_error_count` | `"%u\n"` | `:357` |
| `serial_number` | (DHAL info) | `fw_io_serial_number_read` | `"%016llx\n"` | `:367` |
| `utilization` | `stats/power` | `npower_format_stats` | `"%s\n"` | `:381` |
| `pe_cntrs` | `neuron_core{N}/stats/tensor_engine` | `ndhal->ndhal_tpb.pe_format_activity_stats` | `"%s"` (no `\n`) | `:390` |
| `notify_delay` | (no node — see below) | `nsysfsmetric_notify` state | `"0\n"` / `"-1\n"` | `:340` |

> **GOTCHA —** `0xdeadbeef` is the firmware "register unreadable" sentinel and is squashed to `0` / empty by the OTHER `show` handlers: ECC reads of `0xdeadbeef` print `0` (`:353-355`), a serial read of `0xdeadbeef` or `0` prints a bare `"\n"` (`:369-370`). A reimplementer must reproduce the squash — surfacing `0xdeadbeef` verbatim would mislead a telemetry agent into reporting 3.7 billion ECC errors during a reset window. `pe_cntrs` is the lone leaf with **no trailing newline** (`:394`), an asymmetry to copy exactly if the wire format must match.

---

## 2. The metric_id Taxonomy

Every counter is addressed by a single flat `metric_id` that folds three source enums into one index space `[0, MAX_METRIC_ID)`. `nsysfsmetric_get_metric_id(category, id)` (`:817`) is the fold; the three macros at `neuron_sysfs_metrics.h:14-16` are the arithmetic:

```c
NDS_NC_COUNTER_ID_TO_SYSFS_METRIC_ID(x) = x                                              // :h14
NDS_ND_COUNTER_ID_TO_SYSFS_METRIC_ID(x) = x + NDS_EXT_NC_COUNTER_LAST                    // :h15
NON_NDS_ID_TO_SYSFS_METRIC_ID(x)        = x + NDS_ND_COUNTER_COUNT + NDS_EXT_NC_COUNTER_LAST  // :h16
```

With the shared-header constants `NDS_EXT_NC_COUNTER_LAST = 41`, `NDS_ND_COUNTER_COUNT = 31`, `NON_NDS_COUNTER_COUNT = 64`, the three categories occupy disjoint sub-bands of the flat space, and `MAX_METRIC_ID = 31 + 41 + 64 = 136` (`:h11`). The fold is what lets one `nrt_metrics[136][8]` array hold every counter regardless of which subsystem produced it.

| Category (`enum`, `:h31-35`) | `metric_id` sub-band | Scope | Source enum | Surfaced under | Confidence |
|---|---|---|---|---|---|
| `NDS_NC_METRIC = 0` | `[0, 40]` (= `x`) | **per-NC** | datastore NC counters + extended (`NDS_NC_COUNTER_*`, `NDS_EXT_NC_COUNTER_*`, `share:300-378`) | `neuron_core{N}/stats/status/*`, `other_info/{flop_count,nc_time_in_use}` | HIGH |
| `NDS_ND_METRIC = 1` | `[41, 71]` (= `x+41`) | **per-device** | datastore ND counters (`NDS_ND_COUNTER_*`) | not statically created here — reserved for device-level aggregation (`dev_metrics[]`, `:h108` "TODO") | MED |
| `NON_NDS_METRIC = 2` | `[72, 135]` (= `x+72`) | **mixed** | `enum nsysfsmetric_non_nds_ids` (`:h37-76`, 35 named of 64 reserved) | `memory_usage/*`, `hardware/*`, `power/*`, `info/architecture/*`, `other_info/{model_load,reset_*,inference}` | HIGH |

The **non-NDS** sub-band is the largest and the one whose leaves this file directly creates. Its 35 named ids (the rest of the 64-wide reservation are spare) partition by the file(s) each exposes:

| `metric_id` (= id + 72) | non-NDS id (`:h37-76`) | Scope | Files | Meaning | Conf |
|---|---|---|---|---|---|
| 72 | `HOST_MEM` (0) | per-NC | total/present/peak | host DRAM bytes attributed to this NC | HIGH |
| 73 | `DEVICE_MEM` (1) | per-NC | total/present/peak | device HBM bytes attributed to this NC | HIGH |
| 74–81 | `ND_COUNTER_MEM_USAGE_*_HOST` (2–9) | per-device | total/present/peak | host-mem by category (uncategorized*, code, tensors, constants, misc, ncdev, notification, dma_rings) | HIGH |
| 82–92 | `NC_COUNTER_MEM_USAGE_*_DEVICE` (10–20) | per-NC | total/present/peak | device-mem by category (uncategorized*, code, tensors, constants, scratchpad, misc, ncdev, collectives, nonshared_scratchpad, notification, dma_rings) | HIGH |
| 93 | `RESET_REQ_COUNT` (21) | per-NC | total/present | reset requests issued for this NC | HIGH |
| 94 | `RESET_FAIL_COUNT` (22) | per-NC | total/present | reset attempts that failed | HIGH |
| 95 | `MODEL_LOAD_COUNT` (23) | per-NC | total/present | NEFF/model loads on this NC | HIGH |
| 96 | `INFERENCE_COUNT` (24) | per-NC | total/present | inferences executed on this NC | HIGH |
| 97 | `ECC_SRAM_UNCORRECTED` (25) | per-device | OTHER | SRAM uncorrected ECC count (`fw_io_ecc_read & 0xffff`) | HIGH |
| 98 | `ECC_HBM_UNCORRECTED` (26) | per-device | OTHER | HBM uncorrected ECC (DHAL, non-repairable) | HIGH |
| 99 | `ECC_REPAIRABLE_HBM_UNCORRECTED` (27) | per-device | OTHER | HBM uncorrected ECC (DHAL, repairable) | HIGH |
| 100 | `PE_ARRAY_ACTIVITY` (28) | per-NC | OTHER | PE-array activity (DHAL `pe_format_activity_stats`) | HIGH |
| 101 | `OTHER_NEURON_ARCH_TYPE` (29) | mixed | OTHER | arch string ("NC…"/"ND…" suffix) | HIGH |
| 102 | `OTHER_NEURON_INSTANCE_TYPE` (30) | per-device | OTHER | EC2 instance-type name | HIGH |
| 103 | `OTHER_NEURON_DEVICE_NAME` (31) | per-device | OTHER | device-name string | HIGH |
| 104 | `OTHER_NOTIFY_DELAY` (32) | per-device | OTHER | change-notify toggle — **no node created** (see §6 callout) | HIGH |
| 105 | `OTHER_SERIAL_NUMBER` (33) | per-device | OTHER | board serial (`fw_io_serial_number_read`, `"%016llx"`) | HIGH |
| 106 | `OTHER_POWER_UTILIZATION` (34) | per-device | OTHER | power stats (`npower_format_stats`) | HIGH |

> **NOTE —** the two `*_UNCATEGORIZED_*` ids (2 and 10) carry a header comment of "old runtimes only" (`:h89,97`): newer runtimes report into the specific categories and leave uncategorized at zero. The `flop_count` and `nc_time_in_use` leaves under `other_info/` are the only **NDS-category** ids surfaced by name in the static tree — they use `NDS_NC_COUNTER_MAC_COUNT` and `NDS_NC_COUNTER_TIME_IN_USE` (category 0), wired through the same `custom_counter_nodes_info_tbl` (`:131-138`) as the four non-NDS `other_info` counters. The 25 `status/` leaves are likewise category-0 `NDS_NC_COUNTER_*` ids and are the 1:1 userspace mirror of the `NRT_STATUS` error bands documented in [error-codes](../runtime/error-codes.md).

> **CORRECTION (K-SYSFS) —** the per-device `NDS_ND_METRIC` band (ids 41–71) and the `dev_metrics[MAX_METRIC_ID]` array (`:h108`) are **declared but not built** by `register` in 2.27.4.0: the array is `memset`-zeroed at root init (`:810`) and carries an explicit "TODO: device metrics" comment. No static node maps into it, and the only writers are the (broken) dynamic path. A reimplementer should treat the ND-counter sysfs surface as reserved-but-empty in this version, not as a live device-level counter set. (Confidence HIGH that it is unused beyond the memset.)

---

## 3. The Counter Struct and Its Indexing

Every numeric file is one `struct nsysfsmetric_counter` (`neuron_sysfs_metrics.h:86-91`), a 32-byte cell:

| Field | Offset | Type | Meaning | Who writes |
|---|---|---|---|---|
| `node` | +0 | `struct nsysfsmetric_node *` | back-pointer to the owning kobject node; used by `sysfs_notify`. Nulled by `destroy_counters` to break the node→counter→node cycle before teardown | wired at attr-group init (`:541-545`); cleared at `:957` |
| `total` | +8 | `u64` | monotonic accumulation; printed by the `total` file; zeroed on any write to `total` | `inc`/`dec`/`set`/`nds_aggregate` |
| `present` | +16 | `u64` | the **last delta** applied (see semantics note); printed by `present` | `inc_counter` `= delta` (`:1051`); `dec_counter` (`:1088`) |
| `peak` | +24 | `u64` | maximum `total` ever observed; printed by `peak` | updated inside `inc_counter` when `total` exceeds it |

The cells live in three arrays inside the per-device `struct nsysfsmetric_metrics` (`:h102-110`, embedded at `neuron_device.sysfs_metrics`):

```c
struct nsysfsmetric_metrics {                                   // neuron_sysfs_metrics.h:102
    struct nsysfsmetric_node  root;                             // == the cdev device kobj (:804)
    struct nsysfsmetric_node *dynamic_metrics_dirs[8];          // :h104  MAX_NC_PER_DEVICE — NEVER POPULATED (bug)
    struct nsysfsmetric_counter nrt_metrics[136][8];            // :h105  per-NC   [metric_id][nc_id]
    struct nsysfsmetric_counter nrt_nd_metrics[136];            // :h106  whole-ND [metric_id]  (nc_id == -1)
    struct nsysfsmetric_counter dev_metrics[136];               // :h107  device   — declared TODO, memset only
    uint64_t bitmap;                                            // :h109  accumulated dynamic-request bits
};
```

The `(metric_id, nc_id)` pair is the address: a per-NC counter is `nrt_metrics[metric_id][nc_id]`, a whole-ND counter (the OTHER attrs with `nc_id == -1`) is `nrt_nd_metrics[metric_id]`. The `attr_type` (`TOTAL=0`, `PRESENT=1`, `PEAK=2`, `OTHER=3`, `:h24-29`) selects which field of the cell — or, for `OTHER`, that there is no cell at all and the `show` reads live. `MAX_CHILD_NODES_NUM = 32` (`:h9`) caps a node's children; `MAX_COUNTER_ATTR_TYPE_COUNT = 3` (`:h12`) is the `total`/`present`/`peak` ceiling per counter node.

> **QUIRK — `present` is the last delta, not a window sum.** `inc_counter` sets `counter->present = delta` (`:1051`, an assignment, not `+=`) and `dec_counter` does likewise with a negative-sense delta (`:1088`). So `present` reflects only the most recent update, despite the header comment calling it "counter value at the current window" (`:h89`). A telemetry agent that reads `present` expecting a windowed accumulation will see a single-update value. (Confidence MED — could be intended "last update" semantics; the behavior is HIGH-confidence, the *intent* is the uncertain part.)

---

## 4. The Static Skeleton Build

The whole static tree is built by one entry point at probe time. `nsysfsmetric_register` (`:883`) lays the root, the device-level `info`/`stats` nodes, and then loops the per-NC subtrees. Each leaf is created by `nsysfsmetric_init_and_add_one_node` (`:561`), which does the `kobject_init_and_add` + `attribute_group` create and links every counter's `node` back-pointer.

### Algorithm — `nsysfsmetric_register`

```c
function nsysfsmetric_register(nd, neuron_device_kobj):           // :883
    metrics = &nd->sysfs_metrics
    nsysfsmetric_init_and_add_root_node(metrics, neuron_device_kobj)   // :888
        // root.kobj = *neuron_device_kobj  (STRUCT COPY, :804)
        // root.is_root = true; metrics->bitmap = 0
        // memset(nrt_metrics) / memset(nrt_nd_metrics) / memset(dev_metrics) = 0  (:807-810)

    info_node  = init_and_add_one_node(metrics, &root, "info",  ..0 attrs..)        // :895
    init_and_add_one_node(metrics, info_node, "architecture",
                          root_arch_node_attrs_info_tbl, cnt)        // :901  arch/instance/device_name OTHER
        + ndhal->ndhal_sysfs_metrics.root_info_node_attrs (count+tbl)  // :903  {BOUNDARY: DHAL}

    stats_node = init_and_add_one_node(metrics, &root, "stats", ..0 attrs..)        // :908
    if (!info_node) goto fail                                        // :909  ← copy/paste guard bug (should be !stats_node)

    mem_node = init_and_add_one_node(metrics, stats_node, "memory_usage", ..)       // :914
    host_mem = init_and_add_one_node(metrics, mem_node, "host_mem", ..)             // :920
    init_and_add_nodes(metrics, host_mem,
                       host_mem_category_counter_nodes_info_tbl, cnt, nc_id=-1)     // :926  8 total/present/peak leaves

    ndhal->ndhal_sysfs_metrics.nsysfsmetric_add_ecc_nodes(nd, stats_node)           // :932  {BOUNDARY} -> "hardware"

    nsysfsmetric_init_and_add_nc_default_nodes(nd, &root)                           // :938  per-NC subtrees (below)

    init_and_add_one_node(metrics, stats_node, "power", ..)                         // :945
    init_and_add_nodes(.., power_utilization_attrs_info_tbl, .., nc_id=-1)          // :950  OTHER utilization
    return 0
```

### Algorithm — per-NC subtree and one-node create

```c
function nsysfsmetric_init_and_add_nc_default_nodes(nd, parent):    // :633
    for nc_id in 0..MAX_NC_PER_DEVICE-1:
        if (!(dev_nc_map & (1 << nc_id))) continue                  // :640  only enabled NCs
        snprintf(name, "neuron_core%d", nc_id)                      // :646
        nc   = init_and_add_one_node(metrics, parent, name, ..0 attrs..)            // :647
        st   = init_and_add_one_node(metrics, nc, "stats", ..)                      // :654
        status = init_and_add_one_node(metrics, st, "status", ..)                   // :661
        init_and_add_nodes(metrics, status,
                           status_counter_nodes_info_tbl, 25, nc_id)               // :667  25 total/present leaves
        mu   = init_and_add_one_node(metrics, st, "memory_usage", ..)              // :673
        init_and_add_one_node(metrics, mu, "host_mem",   host_mem_attrs, nc_id)    // :679  total/present/peak
        dm = init_and_add_one_node(metrics, mu, "device_mem", device_mem_attrs, nc_id)  // :685
        init_and_add_nodes(metrics, dm,
                           device_mem_category_counter_nodes_info_tbl, 11, nc_id)  // :691
        oi   = init_and_add_one_node(metrics, st, "other_info", ..)                // :697
        init_and_add_nodes(metrics, oi,
                           custom_counter_nodes_info_tbl, 6, nc_id)                // :703  model_load/reset/inference/flop/time
        ndhal->ndhal_sysfs_metrics.nsysfsmetric_add_tensor_engine_node(nd, st, nc_id)  // :709 {BOUNDARY}
        inf  = init_and_add_one_node(metrics, nc, "info", ..)                      // :715
        init_and_add_one_node(metrics, inf, "architecture", arch_info_attrs, nc_id) // :721

function nsysfsmetric_init_and_add_one_node(metrics, parent, name, is_root, nc_id,
                                            attr_cnt, attr_info_tbl):               // :561  PUBLIC
    new_node = kzalloc(sizeof(*new_node))
    mutex_init(&new_node->lock)
    kobject_init_and_add(&new_node->kobj, &ktype, &parent->kobj, name)             // :573
    if err: return NULL  // {LEAK: new_node not freed, :582-585}
    new_node->attr_group = nsysfsmetric_init_attr_group(attr_cnt, attr_info_tbl,
                                                        nc_id, new_node, metrics)  // :578
        // for each attr: create_attr() wires show/store by attr_type; mode 0644 (:488);
        // and back-links the counter: nrt_metrics[id][nc_id].node = new_node   (:541-545)
        //                        or   nrt_nd_metrics[id].node     = new_node   (nc_id == -1)
    if !attr_group: return NULL  // {LEAK, :588-591}
    sysfs_create_group(&new_node->kobj, new_node->attr_group)                      // :595
    parent->child_nodes[parent->child_node_num++] = new_node                       // :600
    return new_node
```

The counter back-pointer wiring at `:541-545` is the join between the kobject tree and the counter arrays: when a leaf is created for `(metric_id, nc_id)`, the matching `nrt_metrics[metric_id][nc_id].node` (or `nrt_nd_metrics[metric_id].node`) is set to the new node, so a later `inc`/`dec` can call `sysfs_notify` on the right kobject without re-walking the tree.

> **GOTCHA — copy/paste guard bug at `:909`.** The failure check after creating `stats_node` (`:908`) reads `if (!info_node)` instead of `if (!stats_node)`. A `stats_node` allocation failure is therefore *not* detected, and the code proceeds to dereference a NULL `stats_node` at `:914`. In practice `kzalloc` of a small node rarely fails, so the bug is latent, but a reimplementer copying the structure must fix the predicate. (Confidence HIGH — verbatim from source.)

> **NOTE —** `init_and_add_one_node` leaks `new_node` (and a partially-built `attr_group`) on `kobject_init_and_add` failure (`:582-585`) and on `init_attr_group` failure (`:588-591`): it returns NULL without `kobject_put`/`kfree`. Both are error-path leaks, not steady-state, but the unwind is incomplete versus the cdev node builder ([cdev-mmap §5](cdev-mmap.md#5-node-lifecycle--birth-death-and-the-chrdev-region)), which does unwind each stage. (Confidence MED.)

---

## 5. The Dynamic per-metric_id Build (Non-Functional)

Alongside the static skeleton, the file carries a *dynamic* path meant to let a runtime request extra counter nodes at process-exit by setting bits in a datastore bitmap (`NDS_ND_COUNTER_DYNAMIC_SYSFS_METRIC_BITMAP`). `nsysfsmetric_init_and_add_dynamic_counter_nodes` (`:1000`) walks the bitmap and, per requested `metric_id`, creates a `"new_metric_%d"` node with `total`+`present` leaves under a per-NC directory. As shipped in 2.27.4.0 the path is **dead** — it NULL-derefs on first use.

### Algorithm — the dynamic walk and its defect

```c
function nsysfsmetric_init_and_add_dynamic_counter_nodes(nd, ds_val):     // :1000
    metrics = &nd->sysfs_metrics
    metrics->bitmap |= ds_val                                              // accumulate request bits
    for nc_id in enabled NCs:                                              // :1011 (dev_nc_map)
        for metric_id in set-bits(ds_val):
            nsysfsmetric_init_and_add_one_dynamic_counter_node(
                metrics,
                metrics->dynamic_metrics_dirs[nc_id],   // :1015  ← ALWAYS NULL (never assigned anywhere)
                metric_id, nc_id)

function nsysfsmetric_init_and_add_one_dynamic_counter_node(metrics, parent_node,
                                                            metric_id, nc_id):  // :764
    if (MAX_CHILD_NODES_NUM <= parent_node->child_node_num) return -ENOSPC      // :771  ← NULL->child_node_num deref
    mutex_lock(&parent_node->lock)
    if nsysfsmetric_find_node(metric_id, parent_node) >= 0: ...dedupe...        // :748
    snprintf(name, "new_metric_%d", metric_id)                                  // :788
    init_and_add_one_node(metrics, parent_node, name, {total, present}, nc_id)  // 2 attrs
```

> **GOTCHA — the dynamic counter feature is dead: NULL deref.** `metrics->dynamic_metrics_dirs[]` (`:h104`) is *declared but never assigned* anywhere in the driver — a tree-wide grep finds only the declaration and the read site. The dynamic walk passes `dynamic_metrics_dirs[nc_id]` (always NULL, `:1015`) as `parent_node` into `..._one_dynamic_counter_node`, which immediately evaluates `MAX_CHILD_NODES_NUM <= parent_node->child_node_num` (`:771`) — a NULL dereference. This fires the moment any runtime sets `NDS_ND_COUNTER_DYNAMIC_SYSFS_METRIC_BITMAP > 0`, reached from `nsysfsmetric_nds_aggregate` at `:1138-1139`. A reimplementer must either populate `dynamic_metrics_dirs[nc_id]` (e.g. with the per-NC `stats` node) at `register` time, or treat the feature as removed. As shipped it is non-functional. (Confidence HIGH.)

---

## 6. The Counter Bridge — How Values Land

The nodes built above hold no values until the rest of the driver pushes them in. There are three writer surfaces, all funneling through `nrt_metrics[metric_id][nc_id]` under `root.lock`:

- **Direct push** from the mem-pool and reset paths. `nsysfsmetric_inc_counter` / `dec_counter` / `set_counter` (`:1029` / `:1067` / `:845`) take `(category, id, nc_id, delta)`, fold to a `metric_id` via `get_metric_id` (`:817`), update `total`/`present`/`peak`, and — if `nsysfsmetric_notify` is set — `sysfs_notify` the counter's `node`. The mem-pool ([mempool-handles](mempool-handles.md)) pushes `HOST_MEM`/`DEVICE_MEM` and the category ids on alloc/free; the reset path ([reset](reset.md)) pushes `RESET_REQ_COUNT`/`RESET_FAIL_COUNT` via the thin wrappers `inc_reset_req_count` (`:1122`) and `inc_reset_fail_count` (`:1117`).
- **Datastore aggregation** at process exit. `nsysfsmetric_nds_aggregate(nd, entry)` (`:1127`) is called from `neuron_ds_release_entry` (`neuron_ds.c:162`) when an owning process releases its [datastore slab](../datastore/kernel-side.md). It reads each NC's slab counters via the datastore's 3-band `get_neuroncore_counter_value` and `inc_counter`s the matching sysfs counters: all 25 status counters, plus `model_load`, `inference`, `flop` (stored as `2 × MAC_COUNT` since one MAC = a multiply + an add, `:1157`), and `time_in_use`. This is the path that makes the persistent error counters survive the dying process. It also reads the dynamic bitmap and calls the (broken) dynamic path (`:1138`).
- **Change-notify toggle.** Writing `-1` (off) or `0` (on) to the `notify_delay` OTHER attribute flips the global `nsysfsmetric_notify` (set handler `:454-468`), which gates whether `inc`/`dec` and the generic `store` fire `sysfs_notify` — so a userspace `poll()`/`select()` on a `total`/`present`/`peak` file wakes on change.

```c
// the fold + push, model of nsysfsmetric_inc_counter (:1029)
function nsysfsmetric_inc_counter(nd, category, id, nc_id, delta, acquire_lock):
    metric_id = nsysfsmetric_get_metric_id(category, id)         // :817  (category, id) -> [0,136)
    if acquire_lock: mutex_lock(&nd->sysfs_metrics.root.lock)
    c = &nd->sysfs_metrics.nrt_metrics[metric_id][nc_id]
    c->total  += delta
    c->present  = delta                                          // :1051  ASSIGN (last-delta semantics)
    if c->total > c->peak: c->peak = c->total
    if nsysfsmetric_notify && c->node: sysfs_notify(&c->node->kobj, NULL, "total")
    if acquire_lock: mutex_unlock(&nd->sysfs_metrics.root.lock)
```

The userspace-visible names this bridge feeds are exactly the L5 `NDS_*_COUNTER` mirror that [error-codes](../runtime/error-codes.md#the-runtimekernel-counter-bridge) documents: `hw_collectives_error`, `hw_hbm_ue_error`, `execute_sw_semaphore_error`, and the rest of the `status/` leaves are the sysfs face of the `NDS_NC_COUNTER`/`NDS_EXT_NC_COUNTER` ids that `kmetric_update_nds_*` writes from `libnrt`. The counter ordering must be preserved exactly, because (as the shared-header comment at `share/neuron_driver_shared.h:310` states) the runtime assumes the counters are offset by error code.

> **GOTCHA — `set_counter` early-return unbalances the lock.** `nsysfsmetric_set_counter` (`:845`) has a fast path: if `counter->total == val` it `mutex_unlock(&...root.lock)` and returns (`:861-864`) — *unconditionally*, ignoring its `acquire_lock` argument. When called with `acquire_lock = false` (the documented optimization for a caller that already holds the lock, `:h211-213`), this unlocks a mutex the function never took, corrupting the lock state the caller still relies on. A reimplementer must guard the unlock on `acquire_lock`. (Confidence HIGH.)

> **NOTE — `node_release` safety is fragile-but-correct.** `nsysfsmetric_node_release` (`:170`, the kobj `.release`) unconditionally walks `attr_group->attrs[]` to `kfree` each `metric_attribute`. Nodes created with zero attrs (`stats`, `info`, `neuron_core{N}`) still get an `attr_group` whose `attrs[0] == NULL` (`:527,547`); `container_of(NULL, struct metric_attribute, attr)` then yields `-offsetof(...)`, which is `0` *only because* `struct attribute attr` is the **first** field of `metric_attribute` (`:65-66`), making the computed pointer NULL and the `while (attr != NULL)` loop body skip. It is correct as written but breaks the instant a field is prepended to `metric_attribute`. (Confidence HIGH that it currently works; flagged as fragile.)

---

## Function Map

| Function | `file:line` | Role | Confidence |
|---|---|---|---|
| `nsysfsmetric_register` | `:883` | `[PUBLIC]` build the whole static tree at probe (`neuron_cdev.c:3683`) | HIGH |
| `nsysfsmetric_destroy` | `:992` | `[PUBLIC]` lock root, `destroy_counters`, recursive `destroy_nodes` (`neuron_cdev.c:3715`) | HIGH |
| `nsysfsmetric_init_and_add_root_node` | `:802` | struct-copy device kobj into `root.kobj`; zero the 3 counter arrays | HIGH |
| `nsysfsmetric_init_and_add_one_node` | `:561` | `[PUBLIC]` create one node: kobj + attr-group + back-link counters | HIGH |
| `nsysfsmetric_init_and_add_nodes` | `:607` | loop `one_node` over a `counter_node_info` table | HIGH |
| `nsysfsmetric_init_and_add_nc_default_nodes` | `:633` | per-enabled-NC subtree builder | HIGH |
| `nsysfsmetric_init_attr_group` | `:519` | `kzalloc attrs[cnt+1]`; per-attr `create_attr`; wire counter `node` back-ptr (`:541-545`) | HIGH |
| `nsysfsmetric_create_attr` | `:476` | one `metric_attribute`; mode `0644`; wire show/store by `attr_type` | HIGH |
| `nsysfsmetric_get_metric_id` | `:817` | `(category, id) -> metric_id ∈ [0,136)` fold | HIGH |
| `nsysfsmetric_set_counter` | `:845` | `[PUBLIC]` set `total = val`; **early-return unlock bug** (`:861-864`) | HIGH |
| `nsysfsmetric_inc_counter` / `dec_counter` | `:1029` / `:1067` | `[PUBLIC]` push delta; `present = delta`; `sysfs_notify` if enabled | HIGH |
| `nsysfsmetric_inc_reset_req_count` / `_fail_count` | `:1122` / `:1117` | `[PUBLIC]` reset-path thin wrappers | HIGH |
| `nsysfsmetric_nds_aggregate` | `:1127` | `[PUBLIC]` fold a dying process's slab counters into the tree (`neuron_ds.c:162`) | HIGH |
| `nsysfsmetric_init_and_add_dynamic_counter_nodes` | `:1000` | `[PUBLIC]` bitmap-driven dynamic nodes — **NULL-deref, non-functional** | HIGH |
| `nsysfsmetric_init_and_add_one_dynamic_counter_node` | `:764` | one `"new_metric_%d"` node; NULL-parent deref at `:771` | HIGH |
| `nsysfsmetric_node_release` | `:170` | kobj `.release`: free attrs/array/group/node — fragile-but-correct | HIGH |
| `nsysfsmetric_find_metrics` | `:188` | walk kobj parents to the root, recover `nsysfsmetric_metrics` | HIGH |
| `nsysfsmetric_generic_metric_attr_show` / `_store` | `:200` / `:212` | `sysfs_ops` dispatchers → per-`attr_type` handler; `store` fires `sysfs_notify` | HIGH |
| `nsysfsmetric_show_nrt_other_metrics` | `:328` | OTHER `show`: arch/ECC/serial/power/PE/notify dispatch by `metric_id` | HIGH |
| `nsysfsmetric_destroy_counters` / `_destroy_nodes` | `:957` / `:968` | memset counter arrays (kill back-ptrs); recursive `kobject_put` | HIGH |

> **NOTE — dead declarations.** Two header decls have no live use in 2.27.4.0: `struct sysfs_mem_thread` (`:h80-84`), whose comment claims a "metrics every 1 second" aggregation thread, is never instantiated — aggregation is **event-driven** (process-exit `nds_aggregate`), not periodic; and `dev_metrics[]` (`:h107`) is `memset`-only (§2 correction). A reimplementer should not infer a polling thread or a populated device-counter array from these decls. (Confidence HIGH that both are unused.)

---

## Related Components

| Component | Relationship |
|---|---|
| `nsysfsmetric_register` / `destroy` (`:883`/`:992`) | called from the cdev node builder at `neuron_cdev.c:3683`/`:3715` — node birth/death is owned by [cdev-mmap](cdev-mmap.md) |
| `nsysfsmetric_nds_aggregate` (`:1127`) | invoked by `neuron_ds_release_entry` (`neuron_ds.c:162`); slab counter layout owned by [datastore/kernel-side](../datastore/kernel-side.md) |
| `ndhal->ndhal_sysfs_metrics` (`neuron_dhal.h:112-134`) | injects `stats/hardware`, `stats/tensor_engine`, arch suffixes, and `root_info` leaves — owned by [dhal-core](dhal-core.md) |
| `nsysfsmetric_inc/dec_counter` | the mem-pool ([mempool-handles](mempool-handles.md)) and reset ([reset](reset.md)) paths that push counter deltas |
| `fw_io_ecc_read` / `fw_io_serial_number_read` / `npower_format_stats` | external OTHER-attr value sources — [fw-io](fw-io.md), [power](power.md) |

## Cross-References

- [Char Device, fops and mmap](cdev-mmap.md) — the per-node sysfs schema and `nsysfsmetric_register` callsite (`:3683`) that brings this tree into existence at node birth; the cdev layer owns the `device` kobject this tree's root is struct-copied from
- [Error and Status Codes (NRT_STATUS)](../runtime/error-codes.md) — the `NDS_*_COUNTER` error mirror whose userspace-visible names (`hw_hbm_ue_error`, `execute_sw_semaphore_error`, …) are the `status/` leaves here, and the `kmetric_update_nds_*` runtime bridge that drives them
- [Kernel Datastore — Kernel Side (Per-Process Slabs)](../datastore/kernel-side.md) — the per-process counter slabs `nsysfsmetric_nds_aggregate` reads via `get_neuroncore_counter_value` and folds into this tree at process exit
- [Metrics Aggregation](metrics.md) — the sibling `nmetric_*` aggregation path that shares the `neuron_ds_release_entry` trigger and the datastore counter reader
- [Kernel Driver — Overview](overview.md) — where the sysfs metrics tree sits in the driver's device-model surface
