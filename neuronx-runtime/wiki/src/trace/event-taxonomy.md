# SysTraceEventType Taxonomy (46 Variants)

> *All addresses, offsets, enum values, and struct layouts on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, soname `libnrt.so.1`, version namespace `NRT_2.0.0`, NTFF package `KaenaProfilerFormat-2.31.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present, and `.text`/`.rodata` VMA equals file offset, so every `0x…` is both an analysis VMA and a file offset. The enum and all 46 payload structs are recovered from the DWARF-derived IDA `enums.json` (enum `nrt_sys_trace_event_type`, ordinal 3019, size 4) and `structures.json` (the `nrt_sys_trace_event_data_*` family). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every enum discriminant is read from `enums.json`; every payload field name/type/offset/size is read from `structures.json`; the `notification_type → ntff::notification_trace_type + block_type` map is dual-sourced from the jump table @`0x8570c0` and the decompiled converter @`0xaaf40`; the 112-byte `nrt_sys_trace_event` record offsets are a DWARF struct dump. · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

## Abstract

This page is the **authoritative event catalogue** for the runtime's host-side software-span profiler — producer (2) of the three trace engines ([overview §2](overview.md)). It owns one C-ABI enum, `nrt_sys_trace_event_type`, and the 40-byte `data` union it discriminates. The enum has **46 real variants** (discriminants `0..45`) plus a `COUNT` sentinel (`46`); each variant selects one of 46 payload structs that pack into the fixed 40-byte `data` field of every 112-byte `nrt_sys_trace_event` record. The capture engine ([rust-capture](rust-capture.md)) fills these records into per-NeuronCore rings; the C++ `event_to_proto` @`0x9aec0` and the serde JSON exporter ([rust-serde](rust-serde.md)) are the two readers. This page does **not** re-derive the NTFF wire format (owned by [ntff-format](ntff-format.md)) or the ring mechanics (owned by [rust-capture](rust-capture.md)); it is the index space those pages cite.

The familiar reference frame is a **tagged-union event schema** — a discriminant enum plus a `union` of fixed-size payloads, the way a `perf` `PERF_RECORD_*` type selects a record body, or a tracepoint id selects a format. The Neuron twist is that the discriminant is *also* a bitmap index: `nrt_sys_trace_config_t.capture_enabled_for_event_type[46]` is indexed by exactly this enum (`config[46] == enum COUNT`), and the per-NC enable byte at `NcContext+0x240+et` gates emission per type. Every payload fits in 40 bytes because that is the size of the `data` slot at `nrt_sys_trace_event+48`; the union is the set of 46 alternative interpretations of those 40 bytes. A second, smaller taxonomy lives alongside: the device-profile producer's `notification_type` (`0..10`), which this page maps to the NTFF `notification_trace_type` (`0..5`) + `block_type` (`0..2`) — the only place the on-device notification enum reconciles with the wire schema.

For reimplementation, the catalogue's contract is:

- **The 46-variant enum** — every discriminant `0..45` and its name, exactly as the capture bitmap and `event_to_proto` switch index it.
- **The 40-byte `data` union** — all 46 payload structs, their fields, types, and intra-payload offsets, including the shared 16-byte `sync_info` sub-struct and the START/STOP payload split that several variants carry.
- **The 11-key event schema** — the JSON object shape the serde exporter emits per event (snake_case keys), distinct from the binary 112-byte record.
- **The `notification_type → notification_trace_type + block_type` map** — the device-NQ converter codomain (a *separate* enum from the 46-variant sys_trace taxonomy, reconciled only at the NTFF level).

| | |
|---|---|
| **Enum** | `nrt_sys_trace_event_type` (`enums.json` ord 3019, **size 4**) — 46 variants `0..45` + `COUNT`=46 |
| **Payload union** | `nrt_sys_trace_event_data_t` — **40 bytes**, at `nrt_sys_trace_event+48` |
| **Event record** | `nrt_sys_trace_event` — **112 bytes** (`0x70`); `data` @`+48`, `phase` @`+88`, `nc_idx` @`+92` |
| **Phase enum** | `nrt_sys_trace_event_phase` — 0 `START`, 1 `STOP`, 2 `INSTANT`, 3 `COUNT` |
| **Category enum** | `nrt_sys_trace_event_type_category` — 0 `UNKNOWN`, 1 `HARDWARE`, 2 `SOFTWARE`, 3 `COUNT` |
| **Capture bitmap** | `nrt_sys_trace_config_t.capture_enabled_for_event_type[46]` (`config[46] == enum COUNT`) |
| **Proto mapper** | `event_to_proto` @`0x9aec0` — 46-way switch → `ntff::ntrace_event` + `Map<string,string>` ([ntff-format](ntff-format.md)) |
| **NQ converter** | `nrt_profile_convert_trace_type_from_ntff_params` @`0xaaf40`, jump table @`0x8570c0` (`notification_type 0..8` mapped; 9,10 dropped) |

---

## 1. The Discriminant and the Record

### Purpose

`nrt_sys_trace_event_type` is a 4-byte C enum (`enums.json` ordinal 3019) with 47 members: 46 real event types (`0..45`) and a trailing `NRT_SYS_TRACE_EVENT_TYPE_COUNT` = 46 sentinel. The sentinel is the array bound — `nrt_sys_trace_config_t.capture_enabled_for_event_type` is exactly `bool[46]`, so a reimplementation that sizes the enable bitmap to `COUNT` and indexes it by the discriminant is byte-correct. The enum is dense: there are **no gaps** in `0..45` (verified against all 47 `enums.json` members), so unlike the NTFF `dma_queue_type` and `collectives_channel` schema gaps ([ntff-format §2](ntff-format.md)), a dense `0..45` loop over this enum is safe.

The discriminant is stored at offset `0` of the 112-byte `nrt_sys_trace_event` record; the payload it selects occupies the 40-byte `data` slot at offset `+48`. The capture engine writes `data` as the union member named by `trace_type`; the readers switch on `trace_type` to interpret it.

### The 112-byte record

```text
nrt_sys_trace_event   size 112 (0x70)   (structures.json)
  +0x00  4   nrt_sys_trace_event_type_t  trace_type    ── the 46-variant discriminant (§2)
  +0x08  8   uint64                      thread_id     ── cached gettid (TLS THREAD_ID)
  +0x10  8   uint64                      id            ── event id
  +0x18  8   uint64                      nc_local_id   ── per-NC local id
  +0x20  8   uint64                      tracking_id   ── caller id, else seq | (nc_idx<<50)
  +0x28  8   uint64                      timestamp_ns  ── SystemTime ns
  +0x30  40  nrt_sys_trace_event_data_t  data          ── the union; member selected by trace_type
  +0x58  4   nrt_sys_trace_event_phase_t phase         ── 0 START / 1 STOP / 2 INSTANT
  +0x5c  4   uint32                      nc_idx        ── NeuronCore index
  +0x60  8   nrta_seq_t                  nrta_seq_id   ── runtime async sequence id
  +0x68  1   bool                        has_nrta_seq_id
```

> **NOTE —** the binary `data` slot is **40 bytes fixed**, but several payload structs are smaller (the smallest, `exec_tensor_read_batch`, is 4 bytes; `cc_load_start` is 8). The union is sized to its largest members — `cc_exec_start`, `hw_notify`, and `nc_model_switch_start` are each exactly **40 bytes** ([§3](#3-the-40-byte-data-union)). A reimplementation that sizes `data` to anything other than 40 will desync the record stride (112 B) used by `drain_events` and `fetch_events`.

### Two parallel name sets

The C enum (§2) is the **canonical** taxonomy — it indexes the capture bitmap and drives `event_to_proto`. The Rust serde path uses a *separate* set of CamelCase display names from its own dispatch table @`0xa30b98` (string pool @`0xa3112f`), with Rust-only sentinels `Unknown`/`Start` that have no C-enum counterpart. The CamelCase names (`Generic`, `NrtLoad`, `KmgrExec`, `Collectives`, `Barrier`, …) are a serialization view, not a second discriminant space; the wire/bitmap index is always the C enum value `0..45`. The serde display mapping is owned by [rust-serde](rust-serde.md). `[HIGH; pool byte-exact]`

---

## 2. The 46-Variant Catalogue

Every row below is the `enums.json` discriminant paired with its `structures.json` payload struct. The **40-byte payload** column names the union member (the `nrt_sys_trace_event_data_*` struct, prefix elided) and its byte size; START/STOP variants carry two payloads (one per phase). Full per-field layouts are in [§3](#3-the-40-byte-data-union). All confidence **HIGH** unless noted — enum values and payload field names/offsets are direct DWARF reads.

| # | `nrt_sys_trace_event_type::<NAME>` | val | 40-byte payload struct(s) | Meaning | Conf |
|---|---|---|---|---|---|
| 0 | `…_GENERIC` | 0 | (none) | untyped span; no payload member | HIGH |
| 1 | `…_MODEL_LOAD` | 1 | `model_load_stop` (8) | NEFF model load span (STOP carries `model_id`) | HIGH |
| 2 | `…_POST_REQUEST` | 2 | `exec_post_request` (16) | request post: `model_id`, `request_id` | HIGH |
| 3 | `…_EXECUTE_API` | 3 | `nrt_exec` (8) | `nrt_execute` API span: `model_id` | HIGH |
| 4 | `…_RUNTIME_EXECUTE` | 4 | `runtime_exec` (8) | runtime-internal execute: `model_id` | HIGH |
| 5 | `…_CC_LOAD` | 5 | `cc_load_start` (8) / `cc_load_stop` (8) | collectives load: START `g_device_id`/`g_device_count`, STOP `model_id` | HIGH |
| 6 | `…_CC_EXEC_BARRIER` | 6 | `cc_exec_barrier_start` (32) / `_stop` (16) | CC barrier: START `model_id`/`exec_id`/`sync_info`, STOP `sync_info` | HIGH |
| 7 | `…_DEVICE_EXEC` | 7 | `nc_exec_start` (24) / `nc_exec_stop` (16) | on-device execute: START `device_core_idx`/`exec_id`/`sync_info`, STOP `sync_info` | HIGH |
| 8 | `…_CC_EXEC` | 8 | `cc_exec_start` (40) / `cc_exec_stop` (16) | CC op execute: START `gid`/`op_id`/`exec_id`/`sync_info`/`algo` | HIGH |
| 9 | `…_TENSOR_READ` | 9 | `exec_tensor_read` (16) | tensor read: `tensor_id`, `size` | HIGH |
| 10 | `…_TENSOR_WRITE` | 10 | `exec_tensor_write` (16) | tensor write: `tensor_id`, `size` | HIGH |
| 11 | `…_TENSOR_READ_BATCH` | 11 | `exec_tensor_read_batch` (4) | batched read: `num_batches` | HIGH |
| 12 | `…_TENSOR_WRITE_BATCH` | 12 | `exec_tensor_write_batch` (4) | batched write: `num_batches` | HIGH |
| 13 | `…_NUM_ERR` | 13 | `num_err` (24) | numerical-error notif: `device_core_idx`/`exec_id`/`sync_info` | HIGH |
| 14 | `…_ENC_BARRIER_RECV` | 14 | `enc_barrier_recv` (24) | encode-barrier recv: `peer_id`/`exec_id`/`nec_dev_id` | HIGH |
| 15 | `…_ENC_BARRIER_SEND` | 15 | `enc_barrier_send` (24) | encode-barrier send: `peer_id`/`exec_id`/`nec_dev_id` | HIGH |
| 16 | `…_ENC_BARRIER` | 16 | `enc_barrier` (24) | encode-barrier: `peer_id`/`exec_id`/`nec_dev_id` | HIGH |
| 17 | `…_EXEC_PRE` | 17 | `exec_pre` (16) | KBL exec pre stage: `exec_id`, `model_id` | HIGH |
| 18 | `…_EXEC_CORE` | 18 | `exec_core` (16) | KBL exec core stage: `exec_id`, `model_id` | HIGH |
| 19 | `…_EXEC_WAIT` | 19 | `exec_wait` (16) | KBL exec wait stage: `exec_id`, `model_id` | HIGH |
| 20 | `…_EXEC_POST` | 20 | `exec_post` (16) | KBL exec post stage: `exec_id`, `model_id` | HIGH |
| 21 | `…_MODEL_SWITCH` | 21 | `model_switch` (24) | model switch: `device_core_idx`/`model_id`/`is_neuron_core_work` | HIGH |
| 22 | `…_MODEL_SUBMIT` | 22 | `model_submit` (8) | model submit: `model_id` | HIGH |
| 23 | `…_ASYNC_SEMA_WAIT` | 23 | `async_sema_wait` (16) | async semaphore wait: `available_request_slots`/`has_work` | HIGH |
| 24 | `…_HW_NOTIFY` | 24 | `hw_notify` (40) | hardware notif: `device_core_idx`/`hint`/`custom_data`/`exec_id`/`sync_info` | HIGH |
| 25 | `…_TIMESTAMP_SYNC` | 25 | `timestamp_sync` (24) | NC↔CPU clock sync: `exec_id`/`nc_timestamp_ns`/`cpu_timestamp_ns` | HIGH |
| 26 | `…_NC_MODEL_SWITCH` | 26 | `nc_model_switch_start` (40) / `_stop` (16) | per-NC model switch: START `device_core_idx`/`model_id`/`exec_id`/`sync_info` | HIGH |
| 27 | `…_TENSOR_ALLOC` | 27 | `exec_tensor_alloc` (16) | tensor alloc: `tensor_id`, `size` | HIGH |
| 28 | `…_TENSOR_FREE` | 28 | `exec_tensor_free` (24) | tensor free: `tensor_id`/`size`/`ref_count` | HIGH |
| 29 | `…_TENSOR_BLOCK_WHILE_EXEC` | 29 | `exec_tensor_block` (24) | tensor block-on-exec: `tensor_id`/`pending_reader_count`/`pending_writer_count` | HIGH |
| 30 | `…_DMEM_BUF_COPYIN` | 30 | `dmem_buf_copyin` (24) | device-mem copy-in: `mem_type`/`mem_handle`/`offset`/`size` | HIGH |
| 31 | `…_DMEM_BUF_COPYOUT` | 31 | `dmem_buf_copyout` (24) | device-mem copy-out: `mem_type`/`mem_handle`/`offset`/`size` | HIGH |
| 32 | `…_DMEM_BUF_BATCH_COPYIN` | 32 | `dmem_buf_batch_copyin` (24) | batched copy-in: `mem_type`/`num_batches`/`total_num_ops`/`total_size` | HIGH |
| 33 | `…_DMEM_BUF_BATCH_COPYOUT` | 33 | `dmem_buf_batch_copyout` (24) | batched copy-out: `mem_type`/`num_batches`/`total_num_ops`/`total_size` | HIGH |
| 34 | `…_DMA_MEM_ALLOC` | 34 | `dma_mem_alloc_start` (16) / `_stop` (40) | DMA mem alloc: START `hbm_idx`/`usage_type`/`is_nc_hbm`/`is_aligned`/`mla_idx`, STOP `allocated_bytes`/`physical_address`/`return_code`/`usage_type_total_bytes`/`total_bytes` | HIGH |
| 35 | `…_DMA_MEM_DEALLOC` | 35 | `dma_mem_dealloc_start` (24) / `_stop` (24) | DMA mem dealloc: START `hbm_idx`/`is_nc_hbm`/`mla_idx`/`usage_type`/`physical_address`, STOP `freed_bytes`/`usage_type_total_bytes`/`total_bytes` | HIGH |
| 36 | `…_CONSUME_TPB_STATUS_NOTIFICATIONS` | 36 | (none) | drain per-NC TPB status NQ; no `data` member | HIGH |
| 37 | `…_CONSUME_TOPSP_CC_NOTIFICATIONS` | 37 | (none) | drain TopSP collectives NQ; no `data` member | HIGH |
| 38 | `…_CONSUME_ERROR_NOTIFICATIONS` | 38 | (none) | drain error NQ; no `data` member | HIGH |
| 39 | `…_CONSUME_POOL_STDIO` | 39 | (none) | drain gpsimd POOL stdio; no `data` member | HIGH |
| 40 | `…_CC_PREPARE` | 40 | `hostcc_prepare` (12) | host-CC prepare: `ctx_device_id`/`ctx_device_count`/`cc_op` | HIGH |
| 41 | `…_CC_SCHEDULE` | 41 | `hostcc_schedule` (8) | host-CC schedule: `ctx_device_id`/`ctx_device_count` | HIGH |
| 42 | `…_HOSTCC_BUILD_CONTEXT` | 42 | `hostcc_build_ctx` (8) | host-CC build context: `ctx_device_id`/`ctx_device_count` | HIGH |
| 43 | `…_HOSTCC_LOAD` | 43 | `hostcc_load` (16) | host-CC load: `enc_op`/`dtype`/`num_elem` | HIGH |
| 44 | `…_HOSTCC_EXEC` | 44 | `hostcc_exec` (4) | host-CC exec: `stream_id` | HIGH |
| 45 | `…_HOSTCC_WAIT` | 45 | `hostcc_wait` (4) | host-CC wait: `total_notif_expected` | HIGH |
| — | `…_COUNT` | 46 | (sentinel) | array bound = `config.capture_enabled_for_event_type` size; **not a real type** | HIGH |

> **QUIRK —** the four `CONSUME_*` types (36–39) and `GENERIC` (0) carry **no `data` payload** — they are span markers whose meaning is entirely in `trace_type`/`phase`/`timestamp_ns`. A reimplementation that allocates a union member for them will read 40 bytes of stale stack. The capture engine writes nothing into `data` for these; `event_to_proto` emits only the scalar `ntrace_event` fields plus any `Map<string,string>` custom keys.

> **CORRECTION (F-TRACE) —** the consolidated F-TRACE map listed types 34/35 (`DMA_MEM_ALLOC`/`DEALLOC`) payloads as **inferred** from `event_to_proto` proto field names, marking them `[MED]`. The DWARF `structures.json` resolves dedicated payload structs for both, each with explicit START and STOP members (`dma_mem_alloc_start`/`_stop`, `dma_mem_dealloc_start`/`_stop`, [§3](#3-the-40-byte-data-union)); these rows are therefore `[HIGH]`, not inferred. The START payloads carry allocation *intent* (`hbm_idx`, `usage_type`, `mla_idx`), the STOP payloads the *result* (`allocated_bytes`/`freed_bytes`, `physical_address`, running totals).

---

## 3. The 40-Byte `data` Union

Every payload struct below is a `structures.json` member dump (offset : type : name), grouped by shape. The union `nrt_sys_trace_event_data_t` (40 B) is the set of these as alternative interpretations of `nrt_sys_trace_event+48`. The shared 16-byte sub-struct recurs in 8 variants:

```text
nrt_sys_trace_event_data_sync_info   size 16   (structures.json)
  +0x00  8  uint64  nc_timestamp_ns    ── on-device timestamp at the span boundary
  +0x08  8  uint64  cpu_timestamp_ns   ── host timestamp at the same boundary
```

`sync_info` pairs the device clock with the host clock so the consumer can align the two timelines; it is the STOP payload of every `*_start`/`*_stop` device-span pair (types 6, 7, 8, 26) and the tail of `hw_notify`, `num_err`, and `nc_exec_start`.

### `{exec_id, model_id}` family — the exec-stage spans (8 + 16 B)

Types 17–20 (`EXEC_PRE`/`CORE`/`WAIT`/`POST`), plus `exec_core`/`exec_post`/`exec_pre`/`exec_wait` and the `model_exec` alias, all share one 16-byte shape; the single-`model_id` payloads are 8 B:

```text
exec_pre / exec_core / exec_wait / exec_post / model_exec   size 16
  +0x00  8  uint64  exec_id
  +0x08  8  uint64  model_id

nrt_exec / runtime_exec / model_load_stop / model_submit / cc_load_stop   size 8
  +0x00  8  uint64  model_id          ── (exec_id absent)

exec_post_request   size 16
  +0x00  8  uint64  model_id
  +0x08  4  uint32  request_id

cc_load_start   size 8
  +0x00  4  uint32  g_device_id
  +0x04  4  uint32  g_device_count
```

### The device-span START/STOP pairs (with `sync_info`)

```text
nc_exec_start   size 24                     nc_exec_stop   size 16
  +0x00  4  uint32  device_core_idx           +0x00  16  sync_info
  +0x04  4  uint32  exec_id
  +0x08  16 sync_info

cc_exec_start   size 40                     cc_exec_stop   size 16
  +0x00  4  uint32  gid                        +0x00  16  sync_info
  +0x04  4  uint32  op_id
  +0x08  4  uint32  exec_id
  +0x10  16 sync_info
  +0x20  4  uint32  algo

cc_exec_barrier_start   size 32            cc_exec_barrier_stop   size 16
  +0x00  8  uint64  model_id                  +0x00  16  sync_info
  +0x08  4  uint32  exec_id
  +0x10  16 sync_info

nc_model_switch_start   size 40            nc_model_switch_stop   size 16
  +0x00  4  uint32  device_core_idx           +0x00  16  sync_info
  +0x08  8  uint64  model_id
  +0x10  8  uint64  exec_id
  +0x18  16 sync_info
```

> **NOTE —** `cc_exec_start` (type 8) and `nc_model_switch_start` (type 26) are the **40-byte maxima** that size the union. Their STOP payloads collapse to a bare 16-byte `sync_info`. A consumer must select the member by `(trace_type, phase)` jointly: type 8 with `phase==START` reads `cc_exec_start`, with `phase==STOP` reads `cc_exec_stop`. The discriminant alone is insufficient for the start/stop variants.

### Hardware-notify, error, timestamp, switch (singular spans)

```text
hw_notify   size 40                        num_err   size 24
  +0x00  4  uint32  device_core_idx          +0x00  4  uint32  device_core_idx
  +0x04  2  uint16  hint                      +0x04  4  uint32  exec_id
  +0x08  4  uint32  custom_data               +0x08  16 sync_info
  +0x10  8  uint64  exec_id
  +0x18  16 sync_info

timestamp_sync   size 24                   model_switch   size 24
  +0x00  8  uint64  exec_id                   +0x00  4  uint32  device_core_idx
  +0x08  8  uint64  nc_timestamp_ns           +0x08  8  uint64  model_id
  +0x10  8  uint64  cpu_timestamp_ns          +0x10  1  bool    is_neuron_core_work

async_sema_wait   size 16
  +0x00  8  uint64  available_request_slots
  +0x08  1  bool    has_work
```

> **GOTCHA —** `hw_notify.hint` is a **`uint16`** at `+0x04` (not a `uint32`), and `custom_data` is a `uint32` at `+0x08` — the 2-byte field leaves a 2-byte hole at `+0x06`. `timestamp_sync` inlines the same `nc_timestamp_ns`/`cpu_timestamp_ns` pair that `sync_info` carries, but as flat fields after `exec_id` rather than as a nested `sync_info` (24 B with a leading `exec_id`, vs the 16-B sub-struct). Do not assume `timestamp_sync` embeds `sync_info`; it does not.

### Tensor lifecycle (read/write/alloc/free/block/batch)

```text
exec_tensor_read / exec_tensor_write / exec_tensor_alloc   size 16
  +0x00  8  uint64  tensor_id
  +0x08  8  uint64  size

exec_tensor_free   size 24                 exec_tensor_block   size 24
  +0x00  8  uint64  tensor_id                +0x00  8  uint64  tensor_id
  +0x08  8  uint64  size                     +0x08  8  uint64  pending_reader_count
  +0x10  8  uint64  ref_count                +0x10  8  uint64  pending_writer_count

exec_tensor_read_batch / exec_tensor_write_batch   size 4
  +0x00  4  uint32  num_batches
```

### Device-memory buffer copy (single + batch)

```text
dmem_buf_copyin / dmem_buf_copyout   size 24    dmem_buf_batch_copyin / _copyout   size 24
  +0x00  1  uint8   mem_type                       +0x00  1  uint8   mem_type
  +0x08  8  uint64  mem_handle                      +0x04  4  uint32  num_batches
  +0x10  4  uint32  offset                          +0x08  4  uint32  total_num_ops
  +0x14  4  uint32  size                            +0x10  8  uint64  total_size
```

### Encode-barrier (recv/send/plain)

```text
enc_barrier_recv / enc_barrier_send / enc_barrier   size 24
  +0x00  4  int     peer_id
  +0x08  8  uint64  exec_id
  +0x10  4  int     nec_dev_id
```

### DMA-mem alloc/dealloc (START intent / STOP result)

```text
dma_mem_alloc_start   size 16                dma_mem_alloc_stop   size 40
  +0x00  4  uint32  hbm_idx                    +0x00  8  uint64  allocated_bytes
  +0x04  4  dma_mem_usage_type_t usage_type    +0x08  8  uint64  physical_address
  +0x08  1  bool    is_nc_hbm                   +0x10  8  uint64  return_code
  +0x09  1  bool    is_aligned                  +0x18  8  uint64  usage_type_total_bytes
  +0x0c  4  uint32  mla_idx                     +0x20  8  uint64  total_bytes

dma_mem_dealloc_start   size 24             dma_mem_dealloc_stop   size 24
  +0x00  4  uint32  hbm_idx                    +0x00  8  uint64  freed_bytes
  +0x04  1  bool    is_nc_hbm                   +0x08  8  uint64  usage_type_total_bytes
  +0x08  4  uint32  mla_idx                     +0x10  8  uint64  total_bytes
  +0x0c  4  dma_mem_usage_type_t usage_type
  +0x10  8  uint64  physical_address
```

`dma_mem_usage_type_t` is the 23-member alloc-class enum (`0 GENERIC … 22 MAX`; e.g. `4 IO`, `6 WEIGHT`, `12 TENSOR`, `15 CC`) — a *different* enum from the NTFF `memory_usage_type` ([ntff-format §2](ntff-format.md)) despite overlapping names; do not conflate the two index spaces.

### Host-collectives (hostcc) spans

```text
hostcc_prepare   size 12                    hostcc_load   size 16
  +0x00  4  uint32  ctx_device_id             +0x00  4  uint32  enc_op
  +0x04  4  uint32  ctx_device_count          +0x04  4  uint32  dtype
  +0x08  4  uint32  cc_op                     +0x08  8  uint64  num_elem

hostcc_schedule / hostcc_build_ctx   size 8    hostcc_exec   size 4    hostcc_wait   size 4
  +0x00  4  uint32  ctx_device_id              +0x00 4 stream_id        +0x00 4 total_notif_expected
  +0x04  4  uint32  ctx_device_count
```

---

## 4. The 11-Key Event Schema (JSON)

The serde JSON exporter (`api::fetch_events` @`0x5aa3b0`, owned by [rust-serde](rust-serde.md)) emits each event as a JSON object with **11 keys** (snake_case, byte-read from the `.rodata` key pool @~`0xa3139c`). This is a *projection* of the 112-byte record, not the record itself: it flattens the binary fields, renames `trace_type`→`event_type`, adds the derived `worker_gid` (from `vnc_idx_to_gid`), and emits `data` as the per-variant payload object. The JSON keys `event_type`, `data_version`, `worker_gid`, `nc_local_id`, `tracking_id`, `nrta_seq_id` are all confirmed present in `strings.json`.

| Key | Source (112-B record) | Type | Meaning |
|---|---|---|---|
| `event_type` | `trace_type` @`+0` | enum | the 46-variant discriminant (CamelCase via serde, §1) |
| `phase` | `phase` @`+88` | enum | `START`/`STOP`/`INSTANT` (serde display) |
| `id` | `id` @`+16` | uint64 | event id |
| `nc_idx` | `nc_idx` @`+92` | uint32 | NeuronCore index |
| `nc_local_id` | `nc_local_id` @`+24` | uint64 | per-NC local id |
| `tracking_id` | `tracking_id` @`+32` | uint64 | caller id, else `seq \| (nc_idx<<50)` ([rust-capture §1](rust-capture.md)) |
| `thread_id` | `thread_id` @`+8` | uint64 | cached `gettid` |
| `worker_gid` | derived | uint64 | global worker id via `vnc_idx_to_gid` @`0x5aaca0` (not in the binary record) |
| `timestamp_ns` | `timestamp_ns` @`+40` | uint64 | host ns timestamp |
| `nrta_seq_id` | `nrta_seq_id` @`+96` | uint64 | runtime async sequence id (gated by `has_nrta_seq_id` @`+104`) |
| `data` | `data` @`+48` | object | the per-variant payload (§3), serialized by `EventDataWrapper` |

The outer JSON document is `{"events":[…],"data_version":2}` — the per-event objects above wrapped in an `events` array with a `data_version` of `2`. The `data_version` here is the JSON exporter's own version constant, distinct from the NTFF `ntrace_info.data_version` wire field.

> **QUIRK —** `worker_gid` exists **only in the JSON**, not in the binary 112-byte record. It is computed at serialize time from `nc_idx` via `vnc_idx_to_gid` @`0x5aaca0` (vNC index → global worker id). A reimplementer mapping the JSON back onto the record will find no `worker_gid` field to populate — it is a fetch-time derivation, not a captured value. The 11-key JSON object thus has one more field than the record has captured scalars (10 captured + 1 derived).

---

## 5. The `notification_type` → NTFF Mapping

### Purpose

The device-profile producer (producer (1), not this engine) drains on-device Notification Queues whose entries carry a `notification_type` (`0..10`, `enums.json`). Before a notification can be wrapped in an `ntff::trace_info`, that type is converted into an `ntff::notification_trace_type` (`0..5`) plus an `ntff::block_type` (`0..2`). This catalogue owns the *enum reconciliation*; the `trace_info` wire layout is owned by [ntff-format §6](ntff-format.md). The converter is `nrt_profile_convert_trace_type_from_ntff_params` @`0xaaf40`, dispatched through a 9-entry jump table @`0x8570c0` (`cmp $0x8 / ja default`).

The three enums (all `enums.json`):

```text
notification_type (size 4)              ntff::notification_trace_type (size 4)   ntff::block_type (size 4)
  0 TRACE          6 TOPSP_TRACE          0 INSTRUCTION  3 EXPLICIT                 0 BLOCK_TYPE_NC
  1 EVENT          7 TOPSP_EVENT          1 EVENT        4 DMA                       1 BLOCK_TYPE_DMA
  2 ERROR          8 TOPSP_ERROR          2 ERROR        5 THROTTLE                  2 BLOCK_TYPE_TOPSP
  3 INFER_STATUS   9 TOPSP_CC_STATUS      (+ INT_MIN/INT_MAX sentinels)             (+ INT_MIN/INT_MAX sentinels)
  4 DMA           10 MAX
  5 THROTTLE
```

### Algorithm

```c
// Models nrt_profile_convert_trace_type_from_ntff_params @0xaaf40.
// out: *trace_type (ntff::notification_trace_type), *block_type (ntff::block_type); ret NRT_STATUS.
// jump table @0x8570c0, valid range 0..8 (cmp $0x8 / ja default).
function convert_trace_type(notification_type, &trace_type, &block_type):
    switch (notification_type):
        case 0 TRACE:        *trace_type = 0 INSTRUCTION; *block_type = 0 NC;    return SUCCESS
        case 1 EVENT:        *trace_type = 1 EVENT;       *block_type = 0 NC;    return SUCCESS
        case 2 ERROR:        *trace_type = 2 ERROR;       *block_type = 0 NC;    return SUCCESS
        case 3 INFER_STATUS: *trace_type = 0 INSTRUCTION; *block_type = 0 NC;    return SUCCESS
        case 4 DMA:          *trace_type = 4 DMA;         *block_type = 1 DMA;   return SUCCESS
        case 5 THROTTLE:     *trace_type = 5 THROTTLE;    *block_type = 0 NC;    return SUCCESS
        case 6 TOPSP_TRACE:  *trace_type = 0 INSTRUCTION; *block_type = 2 TOPSP; return SUCCESS
        case 7 TOPSP_EVENT:  *trace_type = 1 EVENT;       *block_type = 2 TOPSP; return SUCCESS
        case 8 TOPSP_ERROR:  *trace_type = 2 ERROR;       *block_type = 2 TOPSP; return SUCCESS
        default:  // 9 TOPSP_CC_STATUS, 10 MAX
            nlog("Invalid notification type: %d");   // @0x83e9b7
            return NRT_INVALID                        // = 2; notification dropped
```

The full mapping, byte-verified against both the jump table @`0x8570c0` and the decompiled switch @`0xaaf40` (independent agreement):

| `notification_type` (in) | → `ntff::notification_trace_type` | → `ntff::block_type` | ret |
|---|---|---|---|
| 0 `TRACE` | 0 `INSTRUCTION` | 0 `NC` | SUCCESS |
| 1 `EVENT` | 1 `EVENT` | 0 `NC` | SUCCESS |
| 2 `ERROR` | 2 `ERROR` | 0 `NC` | SUCCESS |
| 3 `INFER_STATUS` | 0 `INSTRUCTION` | 0 `NC` | SUCCESS |
| 4 `DMA` | 4 `DMA` | 1 `DMA` | SUCCESS |
| 5 `THROTTLE` | 5 `THROTTLE` | 0 `NC` | SUCCESS |
| 6 `TOPSP_TRACE` | 0 `INSTRUCTION` | 2 `TOPSP` | SUCCESS |
| 7 `TOPSP_EVENT` | 1 `EVENT` | 2 `TOPSP` | SUCCESS |
| 8 `TOPSP_ERROR` | 2 `ERROR` | 2 `TOPSP` | SUCCESS |
| 9 `TOPSP_CC_STATUS` | — | — | NRT_INVALID (dropped) |
| 10 `MAX` | — | — | NRT_INVALID (dropped) |

> **GOTCHA —** the converter's codomain is `notification_trace_type` ∈ {0,1,2,4,5} — value **3 `EXPLICIT` is never produced here.** `EXPLICIT` is written directly by the explicit collectives-status path (`nrt_profile_session_append_cc_notifications` @`0xaf700`), which sets `trace_info.trace_type = EXPLICIT` without going through this converter. A reimplementation that routes every `trace_info` through `convert_trace_type` will never emit an `EXPLICIT` trace, silently dropping the collectives-status channel. Likewise types 9/10 return `NRT_INVALID` (= 2) and the notification is dropped ("`Unexpected notification type! Dropping…`").

> **NOTE —** this `notification_type` taxonomy (`0..10`) is **disjoint** from the 46-variant `nrt_sys_trace_event_type` (§2) and from the 16-value `ntrace_data_type` C ring ([ntff-format §3](ntff-format.md), `15 = COUNT` sentinel). The three enums share *concepts* (each has exec/error notions) but distinct discriminant spaces; they reconcile only at the `ntff::` message level. The per-engine NQ subscribe lists pin which types each block produces: TopSP block `{6,8,7}` (`.rodata` @`0x857128`), TPB/per-NC block `{0,3,1,2,4,5}` (@`0x857138`).

---

## Related Components

| Name | Relationship |
|---|---|
| `nrt_sys_trace_event` (112 B) | the record whose `trace_type`/`data` this catalogue indexes ([rust-capture §2](rust-capture.md)) |
| `event_to_proto` @`0x9aec0` | the 46-way switch that maps each variant → `ntff::ntrace_event` + `Map<string,string>` |
| `nrt_sys_trace_config_t.capture_enabled_for_event_type[46]` | the per-event-type enable bitmap indexed by this enum (`[46] == COUNT`) |
| `nrt_profile_convert_trace_type_from_ntff_params` @`0xaaf40` | the device-NQ `notification_type` → `notification_trace_type`/`block_type` converter (§5) |
| `api::fetch_events` @`0x5aa3b0` | the serde exporter that emits the 11-key JSON projection (§4) |

## Cross-References

- [sys_trace Capture Engine](rust-capture.md) — the Rust lock-free ring that fills the 112-byte records this catalogue describes; the 40-byte `data` union is packed there
- [NTFF Trace File Format](ntff-format.md) — the `ntff::ntrace_event` wire schema that `event_to_proto` targets, the `trace_info` join point, and the full `notification_type → trace_info` wire mapping
- [NTFF Wire Tables](ntff-wire-tables.md) — the `TcParseTableBase` decode that independently confirms the `ntrace_event` field set the JSON/proto paths populate
- [The Inspect / Profile API](inspect-profile-api.md) — the umbrella session that arms the capture engine and drives `event_to_proto` at harvest
- [Overview: the Three Trace Producers](overview.md) — where this taxonomy sits among the three disjoint trace enumerations
- [back to index](../index.md)
