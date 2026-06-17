# Error and Status Codes (NRT_STATUS)

> *All addresses, enum values, jump-table bands, and `.rodata` string offsets on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **not** stripped and carries DWARF v4: the `NRT_STATUS` enum is read verbatim from `DW_TAG_enumeration_type @<d6ed>` (linkage `10NRT_STATUS`, typedef `@<d7b7>`, `DW_AT_byte_size=4`, `DW_AT_encoding=7` → `uint32_t`), and every status string is the NUL-terminated `.rodata` token the `nrt_get_status_as_str` jump table dereferences. `.text`/`.rodata` VMA equals file offset, so every `0x…` is an analysis VMA. All `file:line` citations into the kernel are against `aws-neuronx-dkms 2.27.4.0` (GPL-2.0, `usr/src/aws-neuronx-2.27.4.0/`). Other versions will differ.*
>
> *Part IV — Userspace Runtime Core / REFERENCE catalogue · **Evidence grade:** **Confirmed (DWARF + jump-table anchored)** — enum values are `DW_AT_const_value`; strings are dereferenced `.rodata`; the priority bands are decoded from the `nrt_get_status_priority` range gates; kernel counter bridges are source-anchored `file:line`. The two `2.31+` codes absent from the pjrt switch (`101`, `1206`) and the inferred errno→NRT edge are confidence-tagged. · [back to index](../index.md)*

## Abstract

Neuron has no single error code. A failure is described differently at six layers as it climbs from silicon to the public API, and `NRT_STATUS` — the 32-bit return enum of every `nrt_*` entry point — is only the top of that stack. Below it sit a firmware mailbox status byte (`FW_IO_SUCCESS/FAIL/UNKNOWN_COMMAND`), a hardware TPB error-notification taxonomy drained from the `NQ_TYPE_ERROR` ring, a libnrt-internal `INFER_ERROR_SUBTYPE` classification of those notifications, the kernel's persistent per-NeuronCore `NDS_*_COUNTER` mirror (surfaced as named sysfs attributes), and the CloudWatch metric ids the kernel posts to firmware. The relationship is **containment**: a `FW_IO` mailbox error is one input to the runtime's status decision; an `NQ_TYPE_ERROR` entry is decoded into an `INFER_ERROR_SUBTYPE`, which collapses into one of the execution-band `NRT_STATUS` codes; and every code that reaches a NeuronCore counter is bridged into the kernel datastore by two runtime functions. This page is the authoritative reference every error path cites — it owns the **full `NRT_STATUS` value space** and the layered model that produces it.

The enum is **sparse and band-partitioned** by subsystem, not contiguous: `0..15` is the general/lifecycle core, `101` is a `2.31+` exec-unit code, `1002..1006` is per-inference execution, `1100` is a collectives-pending sentinel, and `1200..1206` is the hardware-execution subcategory (HBM/SRAM UE, DMA abort, NQ overflow, network-proxy). Each band exists because a different subsystem returns into it, and the gaps (`8`, `12`, `16..100`, `102..1001`, `1007..1099`, `1101..1199`) are deliberate partitioning, never missing codes. Two functions in `libnrt` consume the whole space and a reimplementer must reproduce both: `nrt_get_status_as_str @0xb95c0` maps a code to its **bare identifier** string (`"NRT_*"`, default `"UNKNOWN"` — *not* the bracketed troubleshoot message, which lives only in `libneuronpjrt`), and `nrt_get_status_priority @0xb9790` ranks a code `1..4` so that when several errors race the runtime surfaces the most severe. The master table below carries every code with its string, priority, and origin layer; the band sections explain what each range means; the layer diagram and the `get_status_priority` pseudocode close the page.

## The Six Layers

The error model is six numbering spaces, each authoritative at its own boundary, bridged by table lookup — never a shared formula. A reimplementer must carry each as a literal map.

| Layer | Space | What it is | Authority |
|---|---|---|---|
| **L1 `NRT_STATUS`** | 28 sparse values | Public 32-bit return enum of every `nrt_*` API (the spine of this page) | DWARF `@<d6ed>` |
| **L2 `INFER_ERROR_SUBTYPE`** | 10 values (0..9) | libnrt-internal per-inference classification, decoded from TPB error notifications; producer of the `1003`/`1004`/`1200`-series codes | DWARF `@<39f45>` |
| **L3 TPB error taxonomy** | 11 + 7 values | Raw HW notification types (`NEURON_ISA_TPB_ERROR_TYPE @<166cb09>`) + sequencer subtypes (`@<166cb6c>`) that L2 is derived from | DWARF |
| **L4 `FW_IO` error_code** | 3 values (0/1/2) | Kernel↔firmware mailbox response status (`SUCCESS/FAIL/UNKNOWN_COMMAND`), collapsed to `0/-1/-ENOTSUPP` at the kernel boundary | `neuron_fw_io.h:126-130` |
| **L5 `NDS_*_COUNTER`** | 31 + 64 ids | Kernel datastore (sysfs) per-NeuronCore error counters — the persistent mirror of L1/L2 | `share/neuron_driver_shared.h:300-378` |
| **L6 `NMETRIC_CW_ID_*`** | ids 200..253 | Kernel CloudWatch metric ids posted to firmware; each carries an inline comment naming the `NRT_STATUS` it tracks | `neuron_metrics.c:106-169` |

> **NOTE —** kernel `errno` (`-EINVAL`, `-ENOMEM`, `-EIO`, …) is the transport on the ioctl boundary; it is **not** `NRT_STATUS`. `libnrt` translates ioctl `errno` into `NRT_STATUS` at each syscall wrapper. The translation is **per-call-site**, not a single binary-resident table — the observed conventions are `-ENOMEM → NRT_RESOURCE`, `-EINVAL → NRT_INVALID`, `-EBUSY → NRT_EXEC_NC_BUSY` (MED confidence: inferred from call-site context, no lookup table). Driver-wide the ioctl `errno` distribution is `-EINVAL` 163, `-ENOMEM` 49, `-EIO` 26.

---

## The Master NRT_STATUS Table

Reference-catalogue table (style-guide exception to the 40-row limit — this is the body of the page). **Value** and **Name** are `DW_AT_const_value` / `DW_AT_name` from the DWARF enum `@<d6ed>`. **String** is the literal `nrt_get_status_as_str @0xb95c0` returns; codes whose `.rodata` token offset is pinned carry it inline, the rest return the bare `"NRT_*"` identifier from the same pool (`0x83edbd..0x83fa20`); any code outside the jump-table bands returns `"UNKNOWN"` (`@0x83edbd`). **Priority** is the `1..4` rank `nrt_get_status_priority @0xb9790` assigns (4 = most severe; see [the pseudocode](#the-priority-classifier-get_status_priority)). **Origin layer** is the subsystem that returns the code. **Conf** is HIGH unless the code is a `2.31+` addition absent from the cross-checking pjrt switch.

| Value | Name | String (`nrt_get_status_as_str`) | Priority | Origin layer | Conf |
|---|---|---|---|---|---|
| `0` | `NRT_SUCCESS` | `NRT_SUCCESS` | 0 (no error) | General / API return | HIGH |
| `1` | `NRT_FAILURE` | `NRT_FAILURE` | 1 | General — catch-all runtime failure | HIGH |
| `2` | `NRT_INVALID` | `NRT_INVALID` (`@0x83edc5`) | 1 | General — invalid input parameter(s) | HIGH |
| `3` | `NRT_INVALID_HANDLE` | `NRT_INVALID_HANDLE` (`@0x83edd1`) | 1 | General — invalid handle to API | HIGH |
| `4` | `NRT_RESOURCE` | `NRT_RESOURCE` (`@0x83ede4`) | 1 | MemoryAlloc — insufficient device (HBM) memory | HIGH |
| `5` | `NRT_TIMEOUT` | `NRT_TIMEOUT` | 1 | Timeout — exceeded max execution time | HIGH |
| `6` | `NRT_HW_ERROR` | `NRT_HW_ERROR` | 3 | Hardware — NeuronCore/component HW failure | HIGH |
| `7` | `NRT_QUEUE_FULL` | `NRT_QUEUE_FULL` | 1 | QueueMgmt — exec input queue at capacity | HIGH |
| `9` | `NRT_LOAD_NOT_ENOUGH_NC` | `NRT_LOAD_NOT_ENOUGH_NC` | 1 | ModelLoading — not enough NeuronCores for NEFF | HIGH |
| `10` | `NRT_UNSUPPORTED_NEFF_VERSION` | `NRT_UNSUPPORTED_NEFF_VERSION` | 1 | ModelLoading — NEFF from unsupported compiler | HIGH |
| `11` | `NRT_FAIL_HOST_MEM_ALLOC` | `NRT_FAIL_HOST_MEM_ALLOC` | 1 | ModelLoading — host (system) memory alloc failed | HIGH |
| `13` | `NRT_UNINITIALIZED` | `NRT_UNINITIALIZED` | 1 | RuntimeState — API before `nrt_init` / after `nrt_close` | HIGH |
| `14` | `NRT_CLOSED` | `NRT_CLOSED` | 1 | RuntimeState — API after `nrt_close` | HIGH |
| `15` | `NRT_QUEUE_EMPTY` | `NRT_QUEUE_EMPTY` | 1 | QueueMgmt — dequeue with no pending data | HIGH |
| `101` | `NRT_EXEC_UNIT_UNRECOVERABLE` | `NRT_EXEC_UNIT_UNRECOVERABLE` | 4 | Exec-unit — TPB exec-unit unrecoverable | MED `2.31+` |
| `1002` | `NRT_EXEC_BAD_INPUT` | `NRT_EXEC_BAD_INPUT` | 1 | Execution — bad input tensor name/shape/dtype | HIGH |
| `1003` | `NRT_EXEC_COMPLETED_WITH_NUM_ERR` | `NRT_EXEC_COMPLETED_WITH_NUM_ERR` (`@0x7d54a8`) | 1 | Execution — completed, NaN/Inf in output (L2/L3) | HIGH |
| `1004` | `NRT_EXEC_COMPLETED_WITH_ERR` | `NRT_EXEC_COMPLETED_WITH_ERR` | 1 | Execution — completed w/ logical/physical error (L2/L3) | HIGH |
| `1005` | `NRT_EXEC_NC_BUSY` | `NRT_EXEC_NC_BUSY` | 1 | Execution — NeuronCore locked by another process | HIGH |
| `1006` | `NRT_EXEC_OOB` | `NRT_EXEC_OOB` | 1 | Execution — indirect copy / embedding-update OOB | HIGH |
| `1100` | `NRT_COLL_PENDING` | `NRT_COLL_PENDING` (`@0x83eefb`) | 1 | Collectives — collective op pending completion | HIGH |
| `1200` | `NRT_EXEC_HW_ERR_COLLECTIVES` | `NRT_EXEC_HW_ERR_COLLECTIVES` (`@0x83ef0c`) | 2 | Collectives — stuck (missing notifications) | HIGH |
| `1201` | `NRT_EXEC_HW_ERR_HBM_UE` | `NRT_EXEC_HW_ERR_HBM_UE` (`@0x83ef28`) | 2 | Hardware — HBM unrepairable uncorrectable error | HIGH |
| `1202` | `NRT_EXEC_HW_ERR_NC_UE` | `NRT_EXEC_HW_ERR_NC_UE` | 2 | Hardware — NC on-chip SRAM parity (UE) | HIGH |
| `1203` | `NRT_EXEC_HW_ERR_DMA_ABORT` | `NRT_EXEC_HW_ERR_DMA_ABORT` (`@0x83ef55`) | 2 | Hardware — DMA engine unrecoverable error | HIGH |
| `1204` | `NRT_EXEC_SW_NQ_OVERFLOW` | `NRT_EXEC_SW_NQ_OVERFLOW` (`@0x83ef6f`) | 2 | Execution — SW notification-queue overflow | HIGH |
| `1205` | `NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE` | `NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE` (`@0x7d54c8`) | 2 | Hardware — HBM repairable uncorrectable error | HIGH |
| `1206` | `NRT_NETWORK_PROXY_FAILURE` | `NRT_NETWORK_PROXY_FAILURE` | 2 | Network-proxy failure | MED `2.31+` |

> **QUIRK — the string is the identifier, not the message.** `nrt_get_status_as_str @0xb95c0` returns the bare `"NRT_*"` token (or `"UNKNOWN"`), *never* the human-facing `[Category]: [NRT_*] status_code=N … troubleshoot URL` message. That formatted message is built only by `NrtErrMsg` in `libneuronpjrt @0x8496440`, and the short reworded labels by `nrt_status_to_message @0x19b5d0` in `libtorchneuron` — both outside this binary. The proof that `libnrt` does not format the message: the `/nrt-troubleshoot.html#nrt-troubleshooting` URL appears in `libnrt`'s strings **exactly once** (`strings | rg -c = 1`). A reimplementer must not expect `libnrt` to produce a user message; it produces a symbol.

> **CORRECTION (F-ERRCODES) — `101` and `1206` are not in the pjrt enum.** A prior cross-reference catalogued `NRT_STATUS` from the `libneuronpjrt` `NrtErrMsg` switch, which has no case for `NRT_EXEC_UNIT_UNRECOVERABLE(101)` or `NRT_NETWORK_PROXY_FAILURE(1206)`. Both are present in `libnrt`'s own DWARF enum `@<d6ed>` (`<d759>…=101`, `<d7b3>…=1206`) — they are genuine `2.31+` additions that the pjrt message layer has not yet learned. A reimplementer building from the pjrt switch alone will be missing two codes; build the enum from the runtime's DWARF, which is the authority.

---

## The Bands

The enum's gaps are subsystem partitioning. Each band is a contiguous range a single subsystem returns into; the bands below are the units a reimplementer reasons about.

### `0..15` — General / Lifecycle Core

The classic return space every `nrt_*` API can produce. `0` is success; `1..3` are the generic input/handle errors; `4`/`11` split device-memory vs host-memory allocation failure; `5`/`6`/`7`/`15` are timeout, hardware, and the two queue states; `9`/`10` are the load-time NeuronCore-count and NEFF-version gates; `13`/`14` are the lifecycle guards (called before `nrt_init` / after `nrt_close`). **Gaps `8` and `12` are intentional** — reserved holes in the original enum, never assigned. These are the only codes the runtime→kernel generic-status bridge knows about (see [the bridge](#the-runtimekernel-counter-bridge)).

### `101` — Exec-Unit Unrecoverable

A single `2.31+` code, `NRT_EXEC_UNIT_UNRECOVERABLE`, sitting alone in the `100..199` exec-unit band. It is the unrecoverable end-state of a TPB execution unit and carries the **highest** priority (`4`), so it dominates any racing status. It has no pjrt message and no NeuronCore counter; it is a terminal signal that the exec unit cannot continue. The `nrt_get_status_priority` early-out gate (`cmp $0x65` = 101) special-cases it (MED confidence on band semantics — the value is DWARF-certain, the "unrecoverable" meaning is from the symbol).

### `1002..1006` — Per-Inference Execution

The execution-result band, returned by the execute path after an inference completes. `1002` is a pre-execution input-validation failure (bad tensor name/shape/dtype). `1003` (`COMPLETED_WITH_NUM_ERR`) and `1004` (`COMPLETED_WITH_ERR`) are the two "completed but…" outcomes — the inference ran to completion but produced a numerical error (NaN/Inf) or a logical/physical error (parity, double-clear). **Both are produced by the L2/L3 decode chain**: a TPB `NQ_TYPE_ERROR` notification is classified into an `INFER_ERROR_SUBTYPE`, and the subtype flags collapse into `1003` (numerical subtypes) or `1004` (memory/fake/semaphore/event/psum/sequencer subtypes). `1005` is a contention error (NeuronCore owned by another process); `1006` is an out-of-bounds indirect-copy / embedding-update access.

### `1100` — Collectives Pending

A lone sentinel, `NRT_COLL_PENDING`, in the `1100..1199` collectives band. It is **not an error** — it signals that a collective operation has not yet completed and the caller should poll again. It is range-gated separately in both `nrt_get_status_as_str` (the `0x44c` = 1100 refinement gate) and `nrt_get_status_priority`. A reimplementer must treat it as a retry signal, not a failure.

### `1200..1206` — Hardware-Execution Subcategories

The hardware-execution band, the finest-grained failure space, returned when the L2/L3 decode determines the completion error was a specific silicon fault rather than a generic one. `1200` is a stuck collective (missing notifications); `1201`/`1205` split HBM uncorrectable errors into unrepairable vs repairable; `1202` is NeuronCore on-chip SRAM parity; `1203` is a DMA-engine abort; `1204` is a software notification-queue overflow (the consumer fell behind the `NQ_TYPE_ERROR` producer); `1206` is a `2.31+` network-proxy failure. Every `1200`-band code carries priority `2`, above the generic `1` but below `NRT_HW_ERROR(6)`=3 and `101`=4. This band is the 1:1 source of the kernel's extended NeuronCore counters (`NDS_EXT_NC_COUNTER_*`, ids 31..40).

---

## The Error-Layer Model

This is the central diagram a reimplementer reproduces: how a raw silicon fault travels up six layers into a public `NRT_STATUS` and a persistent sysfs counter. The flow has two converging inputs — the **TPB error-notification ring** (the per-inference path) and the **FW-IO mailbox** (the device-health path) — and one persistence sink (the kernel datastore).

```text
                 ┌─────────────────────── per-inference error path ───────────────────────┐
 silicon fault   │                                                                          │
 (TPB exec)  ───► NQ_TYPE_ERROR ring entry            FW-IO mailbox response                │
                 (NEURON_ISA_TPB_ERROR_TYPE, L3)      (error_code byte, L4)                 │
                 [kernel/notification-queues]          [kernel/fw-io]                        │
                       │ drained by userspace                │ FW_IO_SUCCESS=0 → ret 0       │
                       ▼                                     │ FW_IO_FAIL=1     → ret -1     │
       v2_error_get_infer_error_subtype @0x321760           │ FW_IO_UNKNOWN=2  → ret -1     │
       via sunda_error_subtypes @0x9e0fa0 (L3→L2 remap)     │ (++ctx->fw_io_err_count)      │
                       │                                     ▼                               │
                       ▼                            ┌──── kernel errno ────┐                 │
       INFER_ERROR_SUBTYPE (L2)                     │  -EINVAL / -ENOMEM…  │                 │
       packed into kbl_infer_errors.infer_error_flags[9]   └──────────────┘                 │
                       │                                     │ (ioctl boundary)              │
                       ▼                                     ▼                               │
       exec path collapses flags → NRT_STATUS:      libnrt syscall wrapper:                 │
         numerical  → 1003                           errno → NRT_STATUS (per-callsite, MED)  │
         mem/fake/sem/evt/psum/seq → 1004              -ENOMEM→4, -EINVAL→2, -EBUSY→1005     │
         HBM/SRAM UE, DMA, NQ overflow → 1200..1205   ──────────────┬───────────────────────┘
                       │                                            │
                       └──────────────┬─────────────────────────────┘
                                      ▼
                              NRT_STATUS (L1, this page)
                                      │
            ┌─────────────────────────┼──────────────────────────────┐
            ▼                         ▼                              ▼
  nrt_get_status_as_str      nrt_get_status_priority        runtime→kernel bridge
  @0xb95c0 → "NRT_*"         @0xb9790 → 1..4                kmetric_update_nds_*
            │                         │                              ▼
            ▼                         ▼                    NDS_*_COUNTER (L5, per-NC)
   (pjrt NrtErrMsg builds      pick most-severe                      │
    the user message,          when statuses race          ┌────────┴────────┐
    out of this binary)                                     ▼                 ▼
                                                  sysfs attr name      NMETRIC_CW_ID (L6)
                                                  [kernel/sysfs]       200..253 → firmware
```

### The infer-error-subtype decode (L3 → L2 → L1)

The decode chain that produces `1003`/`1004` and the `1200`-series is three table lookups, end to end:

1. A `NQ_TYPE_ERROR` ring entry carries a raw `NEURON_ISA_TPB_ERROR_TYPE` (L3, `@<166cb09>`): `0 FP_UNDERFLOW, 1 FP_NAN, 2 FP_INF, 3 FP_OVERFLOW, 4 MEMORY_ERROR, 5 FAKE_ERROR, 6 SEMAPHORE_ERROR, 7 EVENT_ERROR, 8 PSUM_COLLISION, 9 SEQUENCER_NONFATAL, 10 SEQUENCER_FATAL`. The kernel does **not** classify these — it only allocates and programs the ring ([kernel/notification-queues](../kernel/notification-queues.md)); the contents are parsed in userspace.
2. `v2_error_get_infer_error_subtype @0x321760` remaps the notification index through `sunda_error_subtypes @0x9e0fa0` (12 × u32 = `{0,1,0,0,3,4,5,6,7,2,8,0}`) into an `INFER_ERROR_SUBTYPE` (L2, `@<39f45>`): `0 NONE, 1 NUMERICAL, 2 TRANSIENT, 3 MEMORY_ERROR, 4 SW_FAKE_ERROR, 5 SW_SEMAPHORE_ERROR, 6 SW_EVENT_ERROR, 7 SW_PSUM_COLLISION_ERROR, 8 SW_SEQUENCER_ERROR, 9 MAX`. The subtype is packed into `kbl_infer_errors.infer_error_flags[9]` (`@<3f1ead>`, one byte per subtype index).
3. The exec path turns those flags into the public `NRT_STATUS`: numerical subtypes → `NRT_EXEC_COMPLETED_WITH_NUM_ERR(1003)`; memory/fake/semaphore/event/psum/sequencer subtypes → `NRT_EXEC_COMPLETED_WITH_ERR(1004)`; the specific HBM/SRAM-UE/DMA-abort faults → the `1200`-series. The exact `mov $code,%eax` emitter site is not individually pinned here (MED).

> **GOTCHA — four SW subtypes have no public NRT_STATUS.** `INFER_ERROR_SUBTYPE_SW_SEMAPHORE/EVENT/PSUM/SEQUENCER (5..8)` each have a dedicated kernel extended counter (`NDS_EXT_NC_COUNTER_ERR_SW_* 36..39`) but **no** distinct `NRT_STATUS` — at the public API they all collapse into `NRT_EXEC_COMPLETED_WITH_ERR(1004)`. A reimplementer reading only `NRT_STATUS` cannot recover which SW subtype fired; that resolution exists only in the kernel counter and the L2 enum. The four subtypes are observable in sysfs (`execute_sw_semaphore_error`, …) but invisible to the return code.

### The runtime→kernel counter bridge

Two `libnrt` functions persist a status into the kernel datastore's per-NeuronCore counters ([datastore/kernel-side](../datastore/kernel-side.md) owns the slab layout these write into):

- **`kmetric_update_nds_generic_status(int nc, int x, NRT_STATUS s) @0xe0cd0`** maps the `0..15` core band. It computes `edx = s - 1`; if `edx <= 9` it indexes `CSWTCH.6 @0x8576a0` (10 × i32 = `{16, 19, -1, 17, -1, -1, -1, -1, 18, 20}`, `-1` = skip) and increments the resulting NeuronCore counter. The verified mapping (`NRT_STATUS → NDS_NC_COUNTER`): `1→16 GENERIC_FAIL`, `2→19 ERR_INVALID`, `3→skip`, `4→17 ERR_RESOURCE`, `9→18 ERR_RESOURCE_NC`, `10→20 ERR_UNSUPPORTED_NEFF_VERSION`. Codes `5..8` (`TIMEOUT`/`HW_ERROR`/`QUEUE_FULL`/gap-8) route through other paths.
- **`kmetric_update_nds_error_stats(int nc, int x, const kbl_infer_errors *e) @0xe0f60`** maps the L2 SW subtypes. For each set `infer_error_flags[bx]` with `bx ∈ 5..8`, it increments counter `bx + 0x1f` — i.e. subtype `5..8 → NDS_EXT_NC_COUNTER 36..39`. The same function also writes the latency/CC-time counters (`0xd/0xe/0xf/0x15`) from float durations.

The kernel's `NDS_EXT_NC_COUNTER` enum (`share/neuron_driver_shared.h:365-378`, ids `31..40`) is a **1:1 mirror** of the `NRT 1200`-series plus the four SW subtypes. The header comment at `:310` is explicit — *"these must be in this specific order … runtime assumes these are offset by error code"* — so a reimplementer must preserve the counter ordering exactly. Each counter is then surfaced under a fixed sysfs attribute name (`"hw_collectives_error"`, `"execute_sw_semaphore_error"`, …) and posted to firmware as a `NMETRIC_CW_ID` (200..253), each of which carries an inline comment naming the `NRT_STATUS` it tracks ([kernel/sysfs](../kernel/sysfs.md) owns the attribute tree).

---

## The Priority Classifier (`get_status_priority`)

`nrt_get_status_priority @0xb9790` (TU `nrt/nrt_status_priority.cpp`) ranks a status `1..4` so the runtime can pick the most important code when an operation produces several. It is **not** a string map — it is a severity classifier keyed on the same range bands as `nrt_get_status_as_str`. A reimplementer reproduces it verbatim to get the "which error wins" decision right.

```c
// Models nrt_get_status_priority @0xb9790 — nrt/nrt_status_priority.cpp.
// Returns a severity rank: higher wins when statuses race. 0 = success, 4 = terminal.
// Range gates are the decoded jump-table compares (0x3ec=1004, 0x4af=1199, 0x4b0..0x4b4=1200..1204).
int nrt_get_status_priority(NRT_STATUS s):
    if s == NRT_SUCCESS:                          // 0 — no error
        return 0

    // --- highest: terminal exec-unit failure (2.31+) -------------------------
    if s == NRT_EXEC_UNIT_UNRECOVERABLE:          // 101 (cmp $0x65) — early-out gate
        return 4                                  // dominates every other status

    // --- general / lifecycle / execution core: lowest non-zero rank ----------
    if s <= NRT_EXEC_COMPLETED_WITH_ERR:          // s <= 0x3ec (1004); covers 1..15, 1002..1004
        if s == NRT_HW_ERROR:                     // 6 — hardware fault, lifted above generic
            return 3
        return 1                                  // all other general/exec codes

    // --- per-inference tail (1005, 1006) and collectives-pending -------------
    if s <= NRT_COLL_PENDING:                      // s <= 0x44c (1100); 1005, 1006, 1100
        return 1                                   // contention / OOB / pending → generic rank

    // --- hardware-execution subcategory band: elevated rank ------------------
    if s <= NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE:     // s <= 0x4b5 (1205); 1200..1205
        return 2                                   // HBM/SRAM UE, DMA abort, NQ overflow, collectives-stuck

    if s == NRT_NETWORK_PROXY_FAILURE:             // 1206 (2.31+)
        return 2

    return 1                                       // any unbanded code → generic
```

> **QUIRK — the rank is not monotonic in the numeric value.** `NRT_HW_ERROR(6)` ranks `3`, above the `1200`-band hardware codes which rank `2`, even though they are numerically larger and more specific. The reason is severity, not specificity: a bare `NRT_HW_ERROR` means the runtime could not even attribute the fault to a subcategory, so it is treated as *more* alarming than a precisely-decoded HBM-repairable error. And `NRT_EXEC_UNIT_UNRECOVERABLE(101)`, numerically the smallest of the "exec" codes, ranks `4` — the highest of all. A reimplementer that ranks by numeric value will surface the wrong status when an unrecoverable unit error races a generic one.

> **NOTE — priority is for racing statuses, not for display.** This rank is consumed when multiple errors arrive for one operation (e.g. several NeuronCores each return a status); the runtime keeps the highest-priority one. It has no bearing on the string or the pjrt message. A reimplementer wiring only the single-status return path does not need it; one aggregating multi-core or multi-inference results does, and must reproduce the bands exactly.

---

## Cross-References

- [Profiling, Trace & Telemetry — Section Map](../trace/overview.md) — where the error counters feed the telemetry path: the `NQ_TYPE_ERROR` producer side and the metric posting this catalogue's L5/L6 layers terminate in
- [The FW-IO MiscRAM Mailbox Protocol](../kernel/fw-io.md) — the L4 `FW_IO_SUCCESS/FAIL/UNKNOWN_COMMAND` error_code, its collapse to `0/-1/-ENOTSUPP`, and the `ctx->fw_io_err_count` tally that becomes the FW-IO CloudWatch metric
- [Notification Queue Engine](../kernel/notification-queues.md) — the `NQ_TYPE_ERROR` ring (L3) the per-inference decode chain drains; the kernel allocates and programs it but does not classify its entries
- [Sysfs Metrics Tree](../kernel/sysfs.md) — the userspace-visible attribute names (`hw_hbm_ue_error`, `execute_sw_semaphore_error`, …) that mirror the `NDS_*_COUNTER` ids this page's bridge writes
- [Kernel Side (Per-Process Slabs)](../datastore/kernel-side.md) — the NDS datastore slab layout the two `kmetric_update_nds_*` bridge functions increment, and the `NDS_NC_COUNTER` / `NDS_EXT_NC_COUNTER` enum these codes mirror into
- [Environment Variable Catalog (NEURON_RT_*)](env-vars.md) — the `NEURON_RT_NUMERICAL_ERRORS_VERBOSITY` / `NEURON_FAIL_ON_NAN` knobs that gate whether the `1003`/`1004` numerical-error path is armed
