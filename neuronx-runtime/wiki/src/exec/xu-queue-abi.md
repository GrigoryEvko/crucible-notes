# The xu_queue and device_exec_request ABI

> *All addresses, offsets, struct ordinals, and bitfield positions on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64 x86-64, **not** stripped, full C++ symbols + DWARF present). `.text`/`.rodata` VMA == file offset, so every `0x…` is an analysis VMA verifiable against `nm -n libnrt.so`; struct ordinals are IDA `structures.json` ordinals. Source asserts name `/opt/workspace/KaenaRuntime/kmgr/xu/queue.c` and `tdrv/hw_exec_queue.c`. Other versions will differ.*
> *Evidence grade: **Confirmed (DWARF-anchored)** — every field offset/type below is read from the IDA `structures.json` ordinal for that struct (`xu_queue_t` 13198, `xuq_exec_info_t` 13199, `seq_id_t`/`xu_id_t` bitfield unions, `device_exec_request_t` 21907, `tpb_execution_info_t` 13351 + its inner union), and the producer/consumer staging from the decompiled bodies of `xuq_client_submit` @`0xe9f10`, `xuq_client_trigger_next` @`0xea090`, `xuq_worker_pop_current_exec` @`0xea1a0`, and `hw_exec_queue_add_exec_request_impl` @`0x320810`. · Part VII — Execution Engine · [back to index](../index.md)*

## Abstract

This page is the byte-level ABI companion to [the XU work-queue and worker page](xu-workers.md). That page established the *behaviour* — the three-cursor SPSC ring, its fences, the per-vNC worker — and deliberately deferred the on-wire layout of the three structs that cross the producer/consumer boundary to here. This page pins those structs to the byte: the **`xu_queue_t`** ring header and its **`xuq_exec_info_t`** slot (the userspace hand-off between the submit thread and the XU worker thread), the **`device_exec_request_t`** (the 52-byte block the submit path stages into device DRAM, the *device-facing* half of the same execution), and the **completion entry** — which, on this runtime, is not a struct the worker reads but the cursor arithmetic plus the `mark_comp_efd` write that *is* the completion record. Read it as the wire format that two cooperating agents — the [submit-path](submit-path.md) producer and the [xu-workers](xu-workers.md) consumer — must agree on byte-for-byte, plus the third agent (the on-device sequencer) that the `device_exec_request_t` speaks to.

There are two distinct ABIs in flight per execution, and conflating them is the central reimplementation hazard. The **host SPSC ABI** (`xu_queue_t` + `xuq_exec_info_t`) is a *userspace* contract: the producer writes a 32-byte slot carrying a `seq_id`, a status out-pointer, a completion eventfd, and an opaque work-item pointer; the consumer reads it. Nothing in this ABI ever touches the device. The **device request ABI** (`device_exec_request_t`) is the *opposite* end of the same execution: a 52-byte block of model-switch flags and ten per-engine instruction-block address words that the submit path DMA-copies into the sequencer's DMEM. The slot's `exec_info` pointer (`+0x18`) reaches a 208-byte `tpb_execution_info_t` work item that bridges them — it is what the worker dereferences to find the model, the resources, and the per-TPB completion-response area. This page lays out all three, the work item that ties them, and the producer-stages-then-consumer-reads sequence with its release/acquire pairing.

The memory model is the detail that governs correctness. Every cursor advance is a single `_InterlockedAdd64` (`LOCK XADD`, a full barrier on x86-64), and the discipline is "write the body, *then* publish the cursor" on the producer and "capture the body, *then* advance the cursor" on the consumer. The `device_exec_request_t` has its own publish boundary — the request body is `dmem_buf_copyin`'d to the device *before* the trigger descriptors that make the sequencer fetch it are appended to the ring. Get the ordering wrong on either ABI and a torn slot (host) or a stale request (device) escapes; this page calls out exactly which field is published last on each.

For reimplementation, the contract is:

- **The `xu_queue_t` / `xuq_exec_info_t` byte layout** — the 256-byte cache-line-isolated ring header (ord 13198) and the 32-byte slot (ord 13199), reproduced field-for-field consistent with the [xu-workers ring model](xu-workers.md#1-the-lock-free-spsc-ring); this page does **not** re-derive the SPSC loop, only the bytes.
- **The `seq_id_t` / `xu_id_t` bitfield encoding** — the exact bit positions (`seq_num:48`, `xu_id:16 = {type:4, queue:4, vnc_id:8}`) so a reimplementer packs `seq_id` byte-identically.
- **The `device_exec_request_t` 52-byte layout** (ord 21907) and the per-engine `{lo,hi}` address arithmetic that fills it, plus the `cc_dma_increment` trailer copied separately to byte `0x34`.
- **The `tpb_execution_info_t` work item** (208 bytes, ord 13351) the slot's `exec_info` points at — the producer/consumer shared payload, including the `infer_resp_t` completion-response area the worker reads at DONE.
- **The completion-entry model** — that there is no separate completion descriptor: completion is the consumer's `exec_head` bump plus the `mark_comp_efd` write, with `xuq_get_last_completed` returning `exec_head − 1` as the self-describing "what finished" record.

| | |
|---|---|
| **Ring header** | `xu_queue_t` — **256 B** (ord 13198); cursors `+0x40`/`+0x80`/`+0xC0` |
| **Ring slot** | `xuq_exec_info_t` — **32 B** (ord 13199) |
| **seq_id** | `seq_id_t` — 8 B union, `{seq_num:48, xu_id:16}` |
| **xu_id** | `xu_id_t` — 2 B union, `{type:4, queue:4, vnc_id:8}` |
| **Device request** | `device_exec_request_t` — **52 B** (`0x34`, ord 21907); built @`0x320810` |
| **Request trailer** | `cc_dma_increment` — 4 B at file-offset `0x34` (outside the struct) |
| **Work item** | `tpb_execution_info_t` — **208 B** (`0xD0`, ord 13351); `exec_info` → this |
| **Resp area** | `infer_resp_t` — 112 B at work-item `+0x28` (the worker's DONE read) |
| **Producer** | `xuq_client_submit` @`0xe9f10` → write slot, `LOCK XADD scheduled_tail` |
| **Consumer** | `xuq_worker_pop_current_exec` @`0xea1a0` → capture, `LOCK XADD exec_head`, signal |
| **Device copyin** | `dmem_buf_copyin(req_buf, &req, 0, 0x34)` then `(…, 0x34, 4)` for the trailer |

---

## 1. The Host SPSC ABI — `xu_queue_t` and `xuq_exec_info_t`

### Purpose

The `xu_queue_t` is the wire format of the userspace hand-off. The [xu-workers page](xu-workers.md#1-the-lock-free-spsc-ring) owns the *semantics* of its three cursors (which region a slot is in, the full/empty predicates, the start-at-1 convention); this section pins only the *bytes*, so a reimplementer who has read the behaviour can lay out the struct verbatim. The one rule this section adds is the layout invariant that makes the lock-freedom physical: the three cursors sit at `+0x40`, `+0x80`, `+0xC0` — exactly 64 bytes apart — so the producer's atomic write to `scheduled_tail` never invalidates the cache line the consumer reads `exec_head` from.

### Struct: `xu_queue_t` (256 bytes, ord 13198)

The ring header. Fields `xu_id`/`buffer`/`capacity` pack into the first cache line (`+0x00..+0x17`); the three cursors each own a full 64-byte line. The gaps (`+0x18..+0x3F`, `+0x48..+0x7F`, `+0x88..+0xBF`, `+0xC8..+0xFF`) are pure padding — never read or written.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `xu_id` | `+0x00` | `xu_id_t` (2 B) | this queue's id; copied into the high 16 bits of every `seq_id` (§2) | HIGH |
| *(pad)* | `+0x02` | 6 B | alignment to the 8-byte `buffer` pointer | HIGH |
| `buffer` | `+0x08` | `xuq_exec_info_t*` | `calloc(capacity, 0x20)` slot ring; indexed `buffer[cursor % capacity]` | HIGH |
| `capacity` | `+0x10` | `uint64_t` | slot count; the modulus and the full-predicate bound | HIGH |
| *(pad)* | `+0x18` | 40 B | false-sharing isolation before `exec_head` | HIGH |
| `exec_head` | `+0x40` | `uint64_t` | **consumer cursor**; `LOCK XADD` by `xuq_worker_pop_current_exec` (own cache line) | HIGH |
| *(pad)* | `+0x48` | 56 B | isolation | HIGH |
| `scheduled_head` | `+0x80` | `uint64_t` | **released-to-HW cursor**; `LOCK XADD` by `xuq_client_trigger_next` (own cache line) | HIGH |
| *(pad)* | `+0x88` | 56 B | isolation | HIGH |
| `scheduled_tail` | `+0xC0` | `uint64_t` | **producer cursor**; `LOCK XADD` by `xuq_client_submit` (own cache line) | HIGH |
| *(pad)* | `+0xC8` | 56 B | tail padding to the 256-byte size | HIGH |

> **NOTE —** the cursors are typed `uint64_t` in the struct (full 64-bit storage) but the *value* space is 48-bit: only `seq_num` (bits 47:0) is ever non-zero in the cursor, because `xuq_client_submit` asserts `scheduled_tail < 0xFFFFFFFFFFFE` ([xu-workers §1](xu-workers.md#the-seq_id-encoding)). The top 16 bits of a cursor are always zero; the `xu_id` is *not* folded into the cursor itself — it is OR'd in only when the cursor is materialised into a `seq_id` (§2). A reimplementer must keep the cursor a clean 48-bit counter and add the `xu_id` at `seq_id` construction, never store it in the cursor.

### Struct: `xuq_exec_info_t` (32 bytes, ord 13199)

One ring slot — the actual unit of the hand-off. The producer (`xuq_client_submit`) writes `seq_id`/`exec_status`/`mark_comp_efd`/`exec_info`; a later waiter (`tpb_xu_base_get_comp_efd`) overwrites `mark_comp_efd`/`efd_from_pool` under `mark_comp_lock`; the consumer reads `seq_id`/`mark_comp_efd`/`efd_from_pool` at pop. The 3-byte hole at `+0x15..+0x17` is alignment padding before the 8-byte `exec_info`.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `seq_id` | `+0x00` | `seq_id_t` (8 B) | self-describing sequence (§2); the peek asserts `seq_num == cursor` | HIGH |
| `exec_status` | `+0x08` | `NRT_STATUS*` | caller's status out-pointer; the worker writes the per-exec result through it at DONE | HIGH |
| `mark_comp_efd` | `+0x10` | `int` | completion eventfd; `-1` at submit, installed later by a blocking waiter | HIGH |
| `efd_from_pool` | `+0x14` | `bool` | `true` ⇒ pooled fd: signal-and-keep; `false` ⇒ fresh fd: signal-and-`close()` | HIGH |
| *(pad)* | `+0x15` | 3 B | alignment to the 8-byte `exec_info` | HIGH |
| `exec_info` | `+0x18` | `void*` | opaque work item — a `tpb_execution_info_t*` (§4); the bridge to the device-side state | HIGH |

> **QUIRK —** the slot stores a *raw* `int` eventfd at `+0x10`, not a structure or an index, and `-1` is the sentinel for "no waiter yet." The producer always writes `-1` ([submit-path §6](submit-path.md#the-lock-free-publish)); a waiter that decides to *block* installs its real fd into the slot **after** submit, under `mark_comp_lock`, racing the worker's read of the same field. The lock that closes that race is documented in [xu-workers §5](xu-workers.md#the-completion-signal) — it is *not* a ring lock. A reimplementer who writes the fd at submit time (rather than `-1`-then-install) removes the ability to submit without a waiter, which the explicit-async API (`NRT_3.0.0`) depends on.

### The slot lifecycle, byte view

The same 32 bytes are written by three different agents at three different times. The table makes the temporal ownership explicit — it is what a torn-slot bug looks like if the fences are wrong:

| Field | Written by | When | Read by |
|---|---|---|---|
| `seq_id` | producer (`xuq_client_submit`) | at submit, before `scheduled_tail` bump | both peeks (assert), `xuq_client_trigger_next`, the worker at pop |
| `exec_status` | producer | at submit | the worker at DONE (writes result through it) |
| `exec_info` | producer | at submit | the worker (`tpb_xu_step`) to find the work item |
| `mark_comp_efd` | producer = `-1`; waiter = real fd | submit, then optionally at block | the worker at pop (signals it) |
| `efd_from_pool` | waiter | at block (with the fd) | the worker at pop (gates `close()`) |

---

## 2. The `seq_id` Encoding

### Purpose

`seq_id_t` is the self-describing 64-bit handle that lets `nrta_is_completed` test completion with no side table: its high 16 bits name the owning XU, its low 48 are the per-XU monotone counter. Both the submit path and "last completed" materialise it from a cursor by the *identical* expression, so a reimplementer must reproduce the bit positions exactly — a `seq_id` returned to a caller is later compared field-for-field against `exec_head − 1`.

### Encoding: `seq_id_t` (8 bytes) and `xu_id_t` (2 bytes)

`seq_id_t` is a union of a `uint64_t raw` and a bitfield; `xu_id_t` likewise. The IDA bitfield ordinals give the exact widths:

```text
seq_id_t  (8 bytes, little-endian)
 bit: 63              48 47                                              0
      ┌─────────────────┬───────────────────────────────────────────────┐
      │   xu_id  (16)   │              seq_num  (48)                      │
      └─────────────────┴───────────────────────────────────────────────┘
            │
            ▼  xu_id_t (16 bits)
 bit: 15        8 7     4 3     0
      ┌──────────┬───────┬───────┐
      │ vnc_id(8)│queue 4│ type 4│
      └──────────┴───────┴───────┘
```

| Struct | Field | Bits | Meaning | Confidence |
|---|---|---|---|---|
| `seq_id_t` | `seq_num` | `[47:0]` | per-XU monotone counter (= a cursor value); asserts `< 0xFFFFFFFFFFFE`, never wraps | HIGH |
| `seq_id_t` | `xu_id` | `[63:48]` | the owning XU's `xu_id_t` | HIGH |
| `xu_id_t` | `type` | `[3:0]` | XU type; `NRTA_XU_COMPUTE = 1` in this build | HIGH |
| `xu_id_t` | `queue` | `[7:4]` | queue index within the XU (0 here) | HIGH |
| `xu_id_t` | `vnc_id` | `[15:8]` | virtual-NeuronCore id (`= vcore->vtpb`); the index into `tpb_xus` | HIGH |

The construction, identical in `xuq_client_submit` and `xuq_get_last_completed`:

```c
// seq_id materialisation — byte-identical at submit and at last-completed
seq_id.raw = (cursor & 0xFFFFFFFFFFFF) | ((uint64_t)xu_id.raw << 48)
//            └──────── seq_num:48 ────────┘  └──── xu_id:16 ────┘
//   cursor = scheduled_tail   in xuq_client_submit   (0xe9f10)
//          = exec_head − 1     in xuq_get_last_completed (0xe9e40)
```

> **GOTCHA —** because `vnc_id` is the **high byte** of `xu_id` and `xu_id` is the **high 16 bits** of `seq_id`, the vNC id ends up at `seq_id` bits `[63:56]` — i.e. `HIBYTE(seq_id.raw)` is the vNC. `tpb_xu_get` ([xu-workers §3](xu-workers.md#the-two-lookups)) exploits exactly this: it resolves an XU from a bare `seq_id`/`xu_id` by reading the high byte as the array index. A reimplementer who lays the bitfield out big-endian, or who puts `type` in the high nibble, breaks both the `tpb_xu_get` lookup and the cross-XU `seq.xu_id == xq->xu_id` assert that every queue op runs before indexing the buffer.

---

## 3. The Device Request ABI — `device_exec_request_t`

### Purpose

`device_exec_request_t` is the *opposite* end of the same execution from the SPSC slot: where the slot is a userspace hand-off between two host threads, the request is the host→device hand-off — a 52-byte block the submit path patches with this execution's per-engine instruction-block addresses and DMA-copies into the sequencer's DMEM. The [submit path](submit-path.md#3-stage-building-the-device_exec_request_t) owns the *staging algorithm* (the skip-config diff, the three trigger-descriptor pairs that carry it to the device); this section pins the *byte layout* and the publish boundary, because the request is the device-visible wire format a reimplementer must reproduce alongside the host ABI.

### Struct: `device_exec_request_t` (52 bytes, `0x34`, ord 21907)

All fields are `uint32_t`. The two address arrays are five-wide each — one entry per TPB engine in order **PE, ACT, POOL, DVE, SP** — split into a low-32 array and a high-32 array (not interleaved `{lo,hi}` pairs). The struct is exactly `0x34` bytes, proven by the two `dmem_buf_copyin` lengths in the staging code.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `do_model_switch` | `+0x00` | `uint32_t` | `= model_switch` arg (1 if this model differs from the last posted) | HIGH |
| `eng_addresses_lo[5]` | `+0x04` | `uint32_t[5]` | per-engine instruction-block address, low 32 bits (PE/ACT/POOL/DVE/SP) | HIGH |
| `eng_addresses_hi[5]` | `+0x18` | `uint32_t[5]` | per-engine instruction-block address, high 32 bits | HIGH |
| `reload_seq_config` | `+0x2C` | `uint32_t` | `1` ⇒ sequencer must re-read TPB config (skip-config optimization, [submit-path §3](submit-path.md#the-skip-config-optimization)) | HIGH |
| `load_act_dve_tables` | `+0x30` | `uint32_t` | `1` ⇒ ACT/DVE lookup tables changed, reload them | HIGH |

The per-engine address that fills the `{lo,hi}` pair is computed from the model's `ib_addrs` table (one `ib_addrs_one_eng_t` per engine):

```c
// per-engine address arithmetic — hw_exec_queue_add_exec_request_impl @0x320810
for i in 0..4:                                   // PE, ACT, POOL, DVE, SP
    base = mod->ib_addrs[i].sunda.base
    addr = base->_pa + base->align_offset + mod->ib_addrs[i].sunda.one_offset.offset
    req.eng_addresses_lo[i] = addr & 0xFFFFFFFF
    req.eng_addresses_hi[i] = addr >> 32
```

> **NOTE —** `cc_dma_increment` is **not** a field of `device_exec_request_t`. It is a separate 4-byte value copied to file-offset `0x34` of the *request buffer* — immediately past the 52-byte struct body — by a second `dmem_buf_copyin(req_buf, &cc_dma_increment, 0x34, 4)`. The struct is `0x34` bytes; the request *buffer* the device fetches is `0x38` bytes (`0x34` body + 4-byte trailer). A reimplementer must size the device-side buffer for both and issue the trailer copyin separately — folding it into the struct as a sixth field would shift `reload_seq_config`/`load_act_dve_tables` and corrupt the device's read.

> **CORRECTION (ABI-1) —** an earlier framing (carried in the seed scan) read `do_model_switch` and the two skip-config flags as 1-byte fields packed at `+0x00..+0x03`, with `eng_addresses` as interleaved `{lo,hi}` 8-byte pairs. The IDA `structures.json` ordinal 21907 and the decompiler's `+4`-stride pointer writes confirm the layout above: a `uint32` `do_model_switch`, then five `uint32` engine-lows, five engine-highs, then two full `uint32` flag words. The decompiler *aliased* the `+4`-stride stores through `do_model_switch`/`eng_addresses_lo[4]`, which is where the byte-field misreading originated. Net size is `0x34` either way; the field layout here is the DWARF-anchored truth. This matches [submit-path §3](submit-path.md#the-request-struct) (`CORRECTION (SUBMIT-2)`).

### The device publish boundary

The request body has its own release-before-publish ordering, parallel to the host ring's. The 52-byte body is DMA-copied to the device *first*; only afterwards are the trigger descriptors (req-copy / evt-poke / tail-inc) that make the sequencer fetch and execute it appended to the ring:

```c
// device-side publish order — hw_exec_queue_add_exec_request_impl @0x320810
fill device_exec_request_t (model_switch, 5×{lo,hi} eng addrs, skip-config flags)
dmem_buf_copyin(req_buf, &exec_req, 0, 0x34)          // (1) STORE the 52-byte body to device DRAM
dmem_buf_copyin(req_buf, &cc_dma_increment, 0x34, 4)  // (2) STORE the 4-byte trailer
// ... only AFTER the body is on the device:
append trigger-desc pair 1 (req-copy, 52 B)           // (3) DMA the body into sequencer DMEM
append trigger-desc pair 2 (evt-poke, 4 B)            //     poke the request-load event
append trigger-desc pair 3 (tail-inc, 4 B ×2)         // (4) PUBLISH: bump the ring tail -> device fetches
```

> **GOTCHA —** the tail-increment descriptor pair (pair 3) is the device-side equivalent of the host ring's `LOCK XADD scheduled_tail`: it is the publish, and everything that defines the request must be on the device *before* it. The byte layout above is staged into the request buffer, then the *event poke* tells the sequencer "new config is ready" and the *tail-inc* tells the DMA engine "there is a new descriptor to fetch." A reimplementation that appends the tail-inc before the body copyin completes exposes a half-written request to the sequencer — the device-memory analogue of a torn slot. The three trigger pairs and their wire format are owned by [submit-path §3](submit-path.md#the-three-trigger-descriptor-pairs) and [the descriptor format page](../dma/descriptor-format.md); this page pins only that the body precedes them.

---

## 4. The Work Item — `tpb_execution_info_t`

### Purpose

The slot's `exec_info` pointer (`+0x18`) is the bridge between the two ABIs. It points at a 208-byte `tpb_execution_info_t` — the producer/consumer *shared payload* that the submit path `calloc`s and fills, and the worker dereferences to find the model, the bound resources, the submission timestamp, and the per-TPB completion-response area it writes at DONE. This is the struct that carries an execution from producer to consumer; the SPSC slot is just the 32-byte envelope that points at it.

### Struct: `tpb_execution_info_t` (208 bytes, `0xD0`, ord 13351)

The outer struct is a 4-byte `type` discriminant at `+0x00` followed (after 4 bytes of alignment) by a 200-byte union at `+0x08` whose `EXECUTE` arm is the `exec` substruct below. All `exec.*` offsets are relative to `+0x08`, so the absolute offset of a field is `0x08 + exec_offset`.

| Field | Offset | Type | Meaning | Confidence |
|---|---|---|---|---|
| `type` | `+0x00` | `tpb_exec_req_type_t` | `EXECUTE = 0` / `STOP = 1`; the worker's first dispatch ([xu-workers §5](xu-workers.md#5-the-worker-step-wait--dispatch--complete--kmetric)) | HIGH |
| *(pad)* | `+0x04` | 4 B | alignment to the 8-byte union | HIGH |
| `exec.mod_ref` | `+0x08` | `dlr_model*` | ref-bumped model; `nn_ref_decrement` at DONE | HIGH |
| `exec.resources` | `+0x10` | `kmgr_exec_resources_t*` | the bound resources ([submit-path §2](submit-path.md#struct-kmgr_exec_resources_t-56-bytes-0x38)); freed at DONE | HIGH |
| `exec.compute_idx` | `+0x18` | `uint64_t` | filled by `kbl_compute_setup` (`compute_idx_tail++`); identifies the multi-NC execution | HIGH |
| `exec.sync_point_cpu_timestamp` | `+0x20` | `uint64_t` | set when the queue was idle at submit (the sync anchor) | HIGH |
| `exec.resp` | `+0x28` | `infer_resp_t` (112 B) | per-TPB completion-response area; the worker writes timings/output info here at DONE | HIGH |
| `exec.wait_event_data` | `+0x90` | `nrt_sys_trace_event_data_t` (40 B) | trace span data; overlaps the `device_id`/`device_tpb_idx` store offsets (see note) | HIGH |
| `exec.wait_event_tracking_id` | `+0xB8` | `uint64_t` | trace tracking id | HIGH |
| `exec.sync_exec` | `+0xC8` | `bool` | sync (1) vs async (0) barrier/path selector | HIGH |

> **NOTE —** the submission timestamp the worker reads at DONE (`get_timespec_delta(now, &info->submission_ts)`, [xu-workers §5](xu-workers.md#5-the-worker-step-wait--dispatch--complete--kmetric)) is the `clock_gettime` store the schedule path makes at `+0x38` — which falls **inside** `exec.resp` (`infer_resp_t.ts_start` at `resp+0x10`). Likewise `device_id`/`device_tpb_idx` are stored at absolute `+0x90`/`+0x92`, overlapping `exec.wait_event_data`. These are the *same union storage* viewed two ways: the schedule path ([submit-path §4](submit-path.md#struct-tpb_execution_info_t-208-bytes-0xd0)) writes by absolute byte offset; the DWARF names the union arm. The store offsets `+0x38`/`+0x90`/`+0x92`/`+0xC8` are HIGH (concrete in the schedule disassembly); the *inner field names* at those offsets are the union's `EXECUTE`-arm view.

### The `infer_resp_t` completion-response area (112 bytes, at work-item `+0x28`)

This is the closest thing to a "completion entry struct" the worker fills — the per-execution timing/output record it populates at DONE before signalling. It is not read off the device as one block; the worker assembles it from the NQ harvest and the kmetric step.

| Field | Offset (in resp) | Type | Meaning | Confidence |
|---|---|---|---|---|
| `combined_output_info` | `+0x00` | `kbl_output_info_t` (64 B) | per-output completion info (counts, status) | HIGH |
| `mla_time` | `+0x40` | `double` | MLA-measured execution time | HIGH |
| `ts_start` | `+0x48` | `timespec` (16 B) | submission timestamp (the `+0x38` absolute store) | HIGH |
| `ts_done` | `+0x58` | `timespec` (16 B) | completion timestamp (`clock_gettime` at DONE) | HIGH |
| `start_nd` | `+0x68` | `uint16_t` | starting NeuronDevice index | HIGH |
| `start_nc` | `+0x6A` | `uint16_t` | starting NeuronCore index | HIGH |

> **QUIRK —** the work item is `calloc`'d (`tpb_xu_schedule_exec`/`tpb_xu_schedule_stop`, [xu-workers §6](xu-workers.md#the-work-item-tpb_execution_info_t-208-bytes-0xd0)) and the *producer* owns its allocation, but the *consumer* (`tpb_xu_step`) owns its `free` — the work item crosses the SPSC boundary by pointer and changes owning thread at the slot. A reimplementer must `free(info)` on the worker side, after the last read (the kmetric loop reads `info->resp`/`info->submission_ts`/`info->resources`) and before `report_complete` pops the slot. Freeing on the producer side, or before the kmetric loop, is a use-after-free across the ABI boundary.

---

## 5. The Completion-Entry Model

### Purpose

A reimplementer arriving from a GPU command-queue background will look for a *completion ring* parallel to the submission ring — a separate descriptor the device or the worker writes to announce "seq N done." There is none on the host SPSC ABI. This section pins the actual completion record, because its absence is itself a defining design fact: completion is encoded entirely in the consumer cursor and the eventfd write.

### The completion record is the cursor

When the worker finishes an execution it does exactly two ABI-visible things, both in `xuq_worker_pop_current_exec` ([xu-workers §2](xu-workers.md#2-producer-trigger-and-consumer-the-fences)): it bumps `exec_head` by one `LOCK XADD`, and it writes 8 bytes (`1`) into the slot's `mark_comp_efd`. There is no "completion entry" struct deposited anywhere. The "what completed" query is pure cursor arithmetic:

```c
// xuq_get_last_completed @0xe9e40 — the entire completion-query ABI
seq_id_t xuq_get_last_completed(xu_queue_t *xq):
    return (seq_id_t){ .raw = ((xq->exec_head - 1) & 0xFFFFFFFFFFFF)
                              | ((uint64_t)xq->xu_id.raw << 48) }
```

A caller tests completion of its `seq_id` by comparing it against this value: `seq <= xuq_get_last_completed(xq)` means done. Because the cursor is monotone and the `seq_id` is monotone, the single `uint64` comparison *is* the completion check — no per-slot "done" flag, no completion descriptor, no side table. This is the basis of `nrta_is_completed` ([xu-workers §1](xu-workers.md#the-seq_id-encoding)).

| "Completion entry" component | What it actually is | Where | Confidence |
|---|---|---|---|
| "seq N is done" record | `exec_head` advanced past N (the `LOCK XADD`) | consumer, `xuq_worker_pop_current_exec` | HIGH |
| "last completed" query | `(exec_head − 1)` materialised as a `seq_id` | `xuq_get_last_completed` @`0xe9e40` | HIGH |
| the wakeup | 8-byte `write(mark_comp_efd, 1)` | consumer, same function | HIGH |
| the per-exec result | `*exec_status = <NRT_STATUS>` through the slot's `+0x08` out-pointer | worker at DONE, via `report_complete` | HIGH |
| the per-exec timings | the `infer_resp_t` at work-item `+0x28` (§4) | worker fills, caller reads via `out_info` | HIGH |

> **QUIRK —** the result of an execution is split across **two** ABI channels, not one. The coarse "did it finish and with what status" travels through the slot's `exec_status` out-pointer (`+0x08`) — a single `NRT_STATUS` the worker writes and the caller reads. The fine-grained timings and per-output info travel through the `infer_resp_t` in the *work item* (`exec_info` → `+0x28`), copied out to the caller's `out_info` at FINALIZE ([completion-engine §1](completion-engine.md#algorithm)). A reimplementer who routes everything through one channel (e.g. a fat completion struct in the slot) will inflate the 32-byte slot and break the cache-line layout that makes the ring lock-free. The slot stays 32 bytes precisely because the bulk result rides the work-item pointer, not the slot.

> **NOTE —** there is no device-written completion descriptor on *this* ABI either. The device announces completion by DMA-writing 16-byte records into the per-engine Notification-Queue rings, which the worker *polls* ([completion-engine §2-3](completion-engine.md#2-the-per-engine-nq-harvest)) — that is a different ABI (the NQ wire format) owned by the completion engine. The host SPSC ABI on this page never sees those 16-byte records; it sees only the worker's cursor bump and eventfd write *after* the worker has harvested them. The two must not be conflated: the NQ entry is device→worker; the completion record here is worker→submitter.

---

## 6. Producer and Consumer Staging — the Memory Ordering

### Purpose

This section is the single sequence a reimplementer must get exactly right: the order in which the producer writes the slot relative to the cursor publish, and the order in which the consumer captures the slot relative to the cursor advance. The [xu-workers page](xu-workers.md#2-producer-trigger-and-consumer-the-fences) states the fences in prose; here they are the annotated producer→consumer staging, pinned to the byte offsets of §1, so the release/acquire pairing is unambiguous.

### Algorithm — producer stages, then publishes

```c
// PRODUCER — xuq_client_submit @0xe9f10 (called under submit_work_lock by the funnel)
NRT_STATUS xuq_client_submit(xu_queue_t *xq, void *item,
                             NRT_STATUS *exec_status, seq_id_t *out_seq_id):
    if (scheduled_tail - exec_head) >= capacity: return NRT_QUEUE_FULL   // depth = tail - head

    seq  = (scheduled_tail & 0xFFFFFFFFFFFF) | (xu_id.raw << 48)         // §2
    slot = &buffer[scheduled_tail % capacity]                           // index BEFORE publish
    // ---- STORE the 32-byte body (offsets from §1) ----
    slot->seq_id        = seq          //  +0x00  ┐
    slot->exec_status   = exec_status  //  +0x08  │  body stores — must all land
    slot->exec_info     = item         //  +0x18  │  BEFORE the cursor publish
    slot->mark_comp_efd = -1           //  +0x10  ┘  (no waiter yet)
    if out_seq_id: out_seq_id->raw = seq

    _InterlockedAdd64(&scheduled_tail, 1)   // ===== RELEASE: LOCK XADD = full barrier =====
    assert scheduled_tail < 0xFFFFFFFFFFFE  // never wraps (queue.c:0x5C)
    return NRT_SUCCESS

// then, after stage+doorbell, the SAME producer thread publishes to the worker:
// xuq_client_trigger_next @0xea090:  _InterlockedAdd64(&scheduled_head, 1)   // release to worker
```

The slot body (all four fields, `+0x00`/`+0x08`/`+0x10`/`+0x18`) is stored **before** the `LOCK XADD` of `scheduled_tail`. On x86-64 the `LOCK`-prefixed add is a full barrier, so the body stores cannot sink past the publish. The worker becomes able to read the slot only when `scheduled_head` moves past it — and `scheduled_head` is bumped by `xuq_client_trigger_next` *after* submit returns, on the same thread, via the same `LOCK XADD`. So there are two release points (`scheduled_tail`, then `scheduled_head`), both barriers, both after the body is written.

### Algorithm — consumer captures, then advances

```c
// CONSUMER — xuq_worker_pop_current_exec @0xea1a0 (worker thread, under mark_comp_lock)
NRT_STATUS xuq_worker_pop_current_exec(xu_queue_t *xq):
    rc = xuq_worker_peek_current_exec(xq, &info)   // empty if exec_head == scheduled_head (ACQUIRE)
    if rc != NRT_SUCCESS: return rc                // peek asserts info->seq_id.seq_num == exec_head

    // ---- CAPTURE the slot fields (offsets from §1) BEFORE advancing exec_head ----
    seq           = info->seq_id          //  +0x00  ┐  once exec_head moves, this slot
    mark_comp_efd = info->mark_comp_efd   //  +0x10  │  re-enters the producer's free region
    efd_from_pool = info->efd_from_pool   //  +0x14  ┘  and may be overwritten

    _InterlockedAdd64(&exec_head, 1)       // ===== RELEASE: reclaim the slot (full barrier) =====

    if mark_comp_efd != -1:                // signal the blocked submitter (the completion record, §5)
        if write(mark_comp_efd, &one, 8) == -1:
            log("Failed to signal completion event for request 0x%lx. errno: %d", seq, errno)
            if !efd_from_pool: close(mark_comp_efd)
            return NRT_FAILURE
        if !efd_from_pool: close(mark_comp_efd)   // fresh fd: signal-and-close; pooled: reuse
    return NRT_SUCCESS
```

The consumer's read of `exec_head == scheduled_head` in the peek is the acquire that pairs with the producer's `scheduled_head` release: once the worker observes `scheduled_head` has advanced, the `LOCK XADD` barrier guarantees it also sees the slot body the producer wrote before that release. The consumer then *captures* `mark_comp_efd`/`efd_from_pool`/`seq_id` **before** its own `LOCK XADD exec_head` — because the instant `exec_head` advances, the slot re-enters the producer's free region and a concurrent submit may overwrite `mark_comp_efd` with `-1`.

| Edge | Producer (release) | Consumer (acquire) | Pairing |
|---|---|---|---|
| submit → worker visibility of slot | `LOCK XADD scheduled_tail` after body stores | — | tail is bumped, but the worker gates on `scheduled_head` |
| trigger → worker visibility | `LOCK XADD scheduled_head` after stage+doorbell | peek reads `exec_head == scheduled_head` | the real producer→consumer release/acquire |
| pop → producer slot reuse | full-check reads `scheduled_tail − exec_head` | `LOCK XADD exec_head` after field capture | worker reclaim is visible to the producer's next full-check |

> **NOTE —** the field published **last** on the producer side is the cursor (`scheduled_tail`, then `scheduled_head`), never a slot field; the field captured **first** on the consumer side is the slot body (`seq_id`/`mark_comp_efd`/`efd_from_pool`), never the cursor. The release/acquire pair that actually matters is `scheduled_head` (producer release) ↔ the peek's `exec_head == scheduled_head` load (consumer acquire) — *not* `scheduled_tail`, because the worker never reads a slot the producer has only `submit`-ted but not `trigger`-ed. On x86-64 the `LOCK XADD` is the full barrier on both sides; on a weaker ISA the reimplementer must make the producer's two cursor bumps `store-release` and the consumer's peek-load `load-acquire`, and additionally insert a store-store fence between the slot-body stores and the `scheduled_tail` bump. The peek-time `seq_num == cursor` asserts ([xu-workers §2](xu-workers.md#2-producer-trigger-and-consumer-the-fences)) are the cheapest detector that this ordering is wrong.

> **GOTCHA —** the `device_exec_request_t` (§3) and the SPSC slot are published in a *fixed cross-ABI order* inside the funnel: the device request is staged and doorbelled (its tail-inc descriptor published) **before** `xuq_client_submit` writes the host slot, which is **before** `xuq_client_trigger_next` releases it to the worker, which is **before** `write(has_work_efd)` wakes the worker ([submit-path §4](submit-path.md#4-the-submit-funnel-and-the-per-tpb-loop)). The device must already be executing before the worker is told to harvest it; the worker peeks the slot only after the producer has both submitted *and* triggered. A reimplementer who wakes the worker (or releases `scheduled_head`) before the doorbell lets the worker poll the NQ rings for a completion the device has not been told to produce.

---

## 7. Reimplementation Checklist

A byte-ABI reimplementation is correct when it reproduces, exactly:

1. **`xu_queue_t`** — 256 bytes: `xu_id` `+0x00`, `buffer` `+0x08`, `capacity` `+0x10`, cursors `exec_head` `+0x40` / `scheduled_head` `+0x80` / `scheduled_tail` `+0xC0`, each on its own 64-byte cache line; cursors are clean 48-bit counters (top 16 bits zero).
2. **`xuq_exec_info_t`** — 32 bytes: `seq_id` `+0x00`, `exec_status` `+0x08`, `mark_comp_efd` `+0x10` (`int`, init `-1`), `efd_from_pool` `+0x14` (`bool`), `exec_info` `+0x18` (`void*`).
3. **`seq_id` packing** — `(cursor & 0xFFFFFFFFFFFF) | (xu_id.raw << 48)`; `xu_id` = `{type:4 @[3:0], queue:4 @[7:4], vnc_id:8 @[15:8]}`; vNC id therefore at `seq_id` bits `[63:56]` (`HIBYTE`).
4. **`device_exec_request_t`** — 52 bytes: `do_model_switch` `+0x00`, `eng_addresses_lo[5]` `+0x04`, `eng_addresses_hi[5]` `+0x18`, `reload_seq_config` `+0x2C`, `load_act_dve_tables` `+0x30`; `cc_dma_increment` is a separate 4-byte copyin at buffer offset `0x34` (not a struct field).
5. **`tpb_execution_info_t`** — 208 bytes: `type` `+0x00`, then the `exec` union at `+0x08` (`mod_ref` `+0x08`, `resources` `+0x10`, `compute_idx` `+0x18`, `sync_point_cpu_timestamp` `+0x20`, `infer_resp_t` `+0x28`, `sync_exec` `+0xC8`); the slot's `exec_info` points here; the worker `free`s it.
6. **Completion model** — no completion descriptor: completion = consumer `LOCK XADD exec_head` + `write(mark_comp_efd, 1)`; the query is `xuq_get_last_completed = (exec_head − 1) | (xu_id<<48)`; result splits across `exec_status` (status) and `infer_resp_t` (timings).
7. **Producer ordering** — write all four slot fields, **then** `LOCK XADD scheduled_tail`; after stage+doorbell, `LOCK XADD scheduled_head`; device-side: `dmem_buf_copyin` the 52-byte body **then** the 4-byte trailer **then** publish via the tail-inc descriptor.
8. **Consumer ordering** — peek (acquire on `scheduled_head`), **capture** `seq_id`/`mark_comp_efd`/`efd_from_pool`, **then** `LOCK XADD exec_head`, **then** signal `mark_comp_efd` (close fresh fds, keep pooled).

> **GOTCHA —** there are two independent ABIs per execution, and they have opposite publish directions: the host SPSC ABI (`xu_queue_t`/slot) is userspace producer→consumer, published by the host `LOCK XADD`; the device request ABI (`device_exec_request_t`) is host→device, published by the DMA tail-increment. A reimplementer must keep them distinct — the slot's `exec_info` bridges them by pointing at the `tpb_execution_info_t` work item, which holds the model/resources the device request was built from and the `infer_resp_t` the worker fills from the device's NQ records. Folding the device request into the host slot, or the work item's `infer_resp_t` into the slot, breaks both the 32-byte slot size and the 52-byte request size and corrupts both wire formats.

---

## Cross-References

- [The XU Work-Queue and Worker Threads](xu-workers.md) — owns the SPSC ring *model*: the three-cursor regions, the fences, the per-vNC worker loop this page supplies the byte layout for
- [The Submit Path (Bind → Stage → Doorbell)](submit-path.md) — the producer that fills the slot (`xuq_client_submit`) and stages the `device_exec_request_t` whose bytes this page pins
- [The Completion Engine (NQ Harvest & Error Decode)](completion-engine.md) — the worker-side harvest that fills the `infer_resp_t` and writes the `mark_comp_efd` this page's completion model describes
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the trigger-descriptor wire format that carries the `device_exec_request_t` body to the device
