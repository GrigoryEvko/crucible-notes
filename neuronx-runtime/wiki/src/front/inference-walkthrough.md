# An Inference, End to End

> **Binaries:** `libnrt.so` 2.31.24.0 (build `0b044f4ce`, SONAME `libnrt.so.1`; ELF64, not stripped, full DWARF; `.text` VMA == file offset) and kernel DKMS `aws-neuronx-dkms` 2.27.4.0 (GPL source, `neuron_cdev.c`). All `libnrt` addresses are `.text` VAs; all kernel cites are `file:line`.
>
> *Part 0 — orientation NARRATIVE. This page follows **one** inference from a NEFF file on disk to a completed output tensor, naming the symbol at each step and linking to the page that derives it. It does **not** re-derive byte-level detail — each stage forward-links to its owning page. · [back to index](../index.md)*

## Abstract

A Neuron inference is two cooperating programs. The first is the **runtime** (`libnrt.so`), a userspace library that a framework (`libneuronpjrt`, PyTorch-XLA, …) calls through the `nrt_*` public API. The second is the **device**: one or more NeuronCores, each a Tensilica-sequenced TPB engine cluster, reached only through a single character device `/dev/neuronN` whose entire contract is an `ioctl` table (magic `'N'`) plus `mmap`. Between them sits a strict division of labor: the runtime parses, plans, stages, and rings a doorbell; the device runs the program and writes notifications back into `mmap`'d rings; the runtime harvests those rings to learn the inference finished. There is no kernel-side completion signal — completion is **notification-driven**, polled out of device memory.

This page walks one inference through that machine. It has two phases. **Load** (`nrt_load`) is a six-stage, mostly-userspace pipeline that turns a NEFF byte blob into an executable `model_t` bound to a physical core, with every variable assigned a fixed device address — static memory planning, so nothing is allocated per-operation at execute time. **Execute** (`nrt_execute`) is the hot path: bind the caller's input/output tensors into pre-staged DMA rings, stage a 52-byte device request plus its trigger descriptors, ring the doorbell with one semaphore-increment ioctl, then block reading a completion eventfd while a worker harvests the device's INFER_STATUS notification rings. If the model is a collective (`nrt_load_collectives`), a ring all-reduce rides inside that same execute, its step schedule emitted on-device by the `enc/alg` composer.

Read this page as the map; read the deep pages for the territory. Each stage below is one or two paragraphs anchored to a real symbol or address, and ends with a forward link to the page that owns its derivation. The byte-level container format, the descriptor wire layout, the ioctl catalog, and the ring-scheduling math each live elsewhere — pointers are at every stage and gathered at the close.

## At a glance

| | |
|---|---|
| **Public load entry** | `nrt_load` `@0xa9fe0` (collectives: `nrt_load_collectives` `@0xaa4c0`) |
| **Load orchestrator** | `kmgr_load_nn_nc` `@0xde280` (six-stage pipeline) |
| **Load-time per-core build** | `kbl_model_add` `@0x3058e0` (7744-byte `model_t`) |
| **Public execute entry** | `nrt_execute` `@0x91de0` → `nrt_execute_repeat` `@0x91650` → `kmgr_exec` `@0xdfd50` |
| **Per-exec tensor bind** | `kbl_compute_build_compute_resources` `@0x306790` |
| **Doorbell ioctl** | `SEMAPHORE_INCREMENT` #41 = `0x80084E29` (the only kernel call on submit) |
| **First load ioctl** | `MEM_ALLOC_V2MT64` #102 = `0x80384E66` (at the stage boundary, not before) |
| **Completion harvest** | `exec_request_progress_one_step` `@0x263330` (INIT → WAIT_BARRIER_PROXY → WAIT_CORE → DONE) |
| **Collective emitter** | `enc_metaring_primitive::compose_operation` `@0x178170` |

---

## The end-to-end stage diagram

```text
  NEFF file (tar.gz blob)
        │
   ── LOAD  (nrt_load @0xa9fe0  ──►  kmgr_load_nn_nc @0xde280) ──────────────────────────
        │
   [S1] PARSE container     neff_parse @0x4ca3f0      hash-verify + libarchive tar → RB-tree
        │                                              (+ TNC dedup cache: shared parse)
   [S2] PARSE graph         parse_neff_json @0xe1d10  →  kelf_load @0x49a6b0   (per-subgraph)
        │
   [S3] KBL BUILD           kbl_model_add @0x3058e0   calloc 7744-B model_t; 14 sub-builders
        │
   [S4] STAGE → device DRAM dmem_buf_copyin @0x229820 weights/tables/instrs → HBM
        │                   ╞══ MEM_ALLOC_V2MT64 (#102, 0x80384E66)  ◄─ FIRST kernel ioctl
        │
   [S5] RELOCATE + READY    var-id→PA (ioqs_mr_var_id_to_pa @0x445c00, tensor_get_pa @0x30fae0)
        │                   ring buf_ptr → abs phys (imcpy_load_vring @0x31bc10)
        │                   register dlr_model; state = 4 (READY)
        ▼
   model-ready  ── returns 284-B nrt_model_t to caller
        │
   ── EXECUTE  (nrt_execute @0x91de0  ──►  kmgr_exec @0xdfd50) ────────────────────────────
        │
   [E1] BIND tensors        kbl_compute_build_compute_resources @0x306790
        │                     ddrs_build_dma_rings + ioqs_build_swap_data  (wire HBM PAs into rings)
   [E2] STAGE descriptors   hw_exec_queue_add_exec_request_impl @0x320810
        │                     device_exec_request_t (52 B) + 3 trigger desc pairs (16 B each)
   [E3] DOORBELL            ndl_nc_semaphore_increment @0xc3ba0
        │                     ╞══ ioctl(0x80084E29)  ◄─ the ONLY kernel call on submit (per TPB)
        │                                                       │
        │                                                       ▼
        │                              ╔═════════════ DEVICE ═════════════╗
        │                              ║  engines run the staged program  ║
   [C-]  (collective?) ───────────────►║  ring all-reduce: reduce-scatter ║
        │   enc compose @0x178170      ║   + all-gather steps over ICI     ║
        │                              ║  writes INFER_STATUS notifications║
        │                              ╚════════════════│══════════════════╝
        ▼                                               ▼
   [E4] HARVEST             exec_request_progress_one_step @0x263330   (poll NQ rings, mmap'd)
        │                     START → END per engine; all COMPLETE → DONE
        ▼
   output tensors ready  ── kmgr_exec copies device outputs back; returns NRT_SUCCESS
```

---

## Stage 1 — Load: parse the NEFF

`nrt_load` (`@0xa9fe0`) is a thin guard: it rejects `nc_count` outside `{-1, 2}` (`"vnc_count is deprecated. only use -1."`), claims a virtual NeuronCore, parses the model config, and tail-calls the orchestrator `kmgr_load_nn_nc` (`@0xde280`). That orchestrator runs a six-stage pipeline whose first four stages are pure userspace — **no ioctls fire until staging**. Stage 1 is the container parse: `neff_parse` (`@0x4ca3f0`) preflights the header, rejects NEFF `version_major > 2` and unknown feature bits, verifies the package hash (SHA-256 for packager 1, MD5 for 2), then drives `libarchive` over the gzip-pax-tar payload, inserting each member (`neff.json`, the per-subgraph `def.json`, the `<engine>.bin` blobs) into a red-black tree keyed by path (`neff_t::files`).

One subtlety worth naming up front: concurrent loaders of the *same* NEFF share a single parse via a process-global, UUID-keyed **TNC cache** (`tnc @0xc5d880`), a four-state machine (UNINIT/PENDING/READY/FAILED) so a second caller of the same model waits on a semaphore rather than re-parsing. The full container schema, the simdjson walk, and the TNC state machine are derived in the load-pipeline page.

→ [neff/load-pipeline](../neff/load-pipeline.md) — the six-stage `nrt_load` pipeline, NEFF container format, and TNC dedup cache.

## Stage 2 — Load: parse the graph and build the kbin

With the tar tree in hand, `parse_neff_json` (`@0xe1d10`) reads `neff.json`, finds the `__kelf` node, and builds the input/output `IO_MAP`s by tensor name. `dlr_kelf_load` (`@0xe0830`) then walks each subgraph through `kelf_load` (`@0x49a6b0`) and `kelf_load_from_neff` (`@0x4c0870`), parsing `def.json` (variables, DMA queues, state-buffer carveouts, collective streams, fp8 config) and the per-engine instruction/DMA blocks. The product per subgraph is a 424-byte **`kbin`**, built by `gen_kbin` (`@0x4af930`) from a 440-byte parser working-set (`mla_resources`). This is also where **static memory planning** begins: `parse_one_variable` (`@0x4b36b0`) emits the compiler's on-disk `kbin_mem_ref` records (152 bytes each) — the plan that fixes every variable's device address.

The KBL build proper is the staging-time step. `kbl_model_add` (`@0x3058e0`, the 2907-byte master constructor) `calloc`s the 7744-byte device `model_t` and runs fourteen sub-builders — DMA-queue LUTs, data-refill rings, IO queues, activation-table storage, the per-engine instruction blocks (`sequencer_setup_instr @0x4483d0`). It is the function the compute-resource-build lane is named for.

→ [neff/kelf2kbin](../neff/kelf2kbin.md) — KELF/`def.json` parse and the `mla_resources`→`kbin` transform (`gen_kbin`).
→ [neff/compute-resource-build](../neff/compute-resource-build.md) — `kbl_model_add` and the `model_t` build pipeline.

## Stage 3 — Load: stage to device DRAM and relocate

Staging is where the runtime first touches the device. `dlr_kelf_stage` (`@0xe0970`) drives, per subgraph, a `dmem_alloc` into device DRAM (`TONGA_DRAM`) and a `dmem_buf_copyin` (`@0x229820`) of weights, activation tables, and instruction RAM into HBM. The very first device ioctl of the whole load is the `MEM_ALLOC_V2MT64` (#102, `0x80384E66`) carve-out fired here — confirming the parse/stage boundary. Multi-core (LNC) loads fan out one pthread per `kbin` (up to 256) and roll back already-staged cores on any failure.

"Relocation" is not one pass. It happens in two layers during the build. **(a)** Instruction-stream var-id→physical-address resolution: inside `sequencer_setup_instr`, the pseudo→ISA translator resolves compiler variable ids to SBUF/HBM physical addresses via `ioqs_mr_var_id_to_pa` (`@0x445c00`) and `tensor_get_pa` (`@0x30fae0`), and records INSERT/DELETE instruction-pointer deltas with `kbin_patch_append` (`@0x2fb0f0`) so the host-side debug/profile IP view stays accurate. **(b)** DMA-ring `buf_ptr` relocation: `imcpy_load_vring` (`@0x31bc10`) rewrites each ring's *relative* receive pointer (tag bits select tx/rx, mask `&0x3FFF…` = 62-bit phys) to an *absolute* device physical address. The static plan itself — every variable assigned a fixed offset by `mem_ref_copy_and_stage_mr` — is detailed in the completion-engine page (it owns the planner). Once relocated, `kmgr_load_nn_nc` registers the userspace `dlr_model` in `dlr_model_set`, sets `state = 4 (READY)`, and returns a 284-byte `nrt_model_t` to the caller.

→ [neff/load-pipeline](../neff/load-pipeline.md) — staging fan-out, relocation layers, and the IOCTL boundary.

## Stage 4 — Execute: bind tensors and stage descriptors

`nrt_execute` (`@0x91de0`) is a state-guarded wrapper that tail-calls `nrt_execute_repeat` (`@0x91650`, host-side input-count validation) and dispatches into `kmgr_exec` (`@0xdfd50`). `kmgr_exec` resolves the model handle and branches on a process-global `async_exec_workers` pointer: the default **sync** path blocks on a completion eventfd; the **async** path stages the same hardware submission inline and returns a handle immediately. Both share the bind→stage→doorbell chain.

**Bind** is `kbl_compute_build_compute_resources` (`@0x306790`), called once per execution from `kmgr_exec_pre` (`@0xdf820`). Per physical TPB the model spans, it drives `ddrs_build_dma_rings` (`@0x3179a0`) and `ioqs_build_swap_data` (`@0x446060`) to wire *this* call's input/output tensors — looked up by name through the model's `io_mr_to_name_map` — into the per-engine DMA TX/RX rings at their fixed HBM physical addresses. **Stage** is `hw_exec_queue_add_exec_request_impl` (`@0x320810`): it fills a 52-byte `device_exec_request_t` (model-switch flags, per-engine instruction-buffer addresses), `dmem_buf_copyin`s it to device DMEM, then builds **three** 16-byte trigger descriptor pairs — a 52-byte request copy, a 4-byte event poke (so the queue engine refetches config), and a 4-byte tail-increment. Because every variable already has a fixed address from load-time planning, nothing is allocated here — the rings just point at the plan.

→ [exec/submit-path](../exec/submit-path.md) — the full bind→stage→doorbell→publish chain, sync/async branch, and `device_exec_request_t`.
→ [runtime/api-tensors](../runtime/api-tensors.md) — tensor sets, IO maps, and the name→HBM binding contract.

## Stage 5 — The kernel boundary

The runtime crosses into the kernel exactly once per TPB on the submit hot path, through `ndl_nc_semaphore_increment` (`@0xc3ba0`), which issues `ioctl(fd, 0x80084E29, {nc, INFERENCE_START_evtid, 1})` — the `SEMAPHORE_INCREMENT` command, nr 41. That single semaphore bump is the doorbell: it tells the device's queue engine that a new execution request is staged and ready. For a multi-core model, the kickoff loop (`dlr_kickoff_exec @0xdd890`) fires this ioctl once per `sg_count` TPB, lockstep with the descriptor staging.

On the kernel side, `neuron_cdev.c`'s `ncdev_ioctl` (`:3188`) is a single giant `if/else-if` chain on `cmd` over base char `'N'` (`0x4E`). It NULL-guards `private_data`, writes an audit record, splits free-access (`O_WRONLY`) fds to a restricted `ncdev_misc_ioctl` subset, gates ~19 mutating commands behind `npid_is_attached`, then dispatches — `SEMAPHORE_INCREMENT` lands at `:3316` (`semaphore_ioctl`). The command space is intentionally overloaded (same `_IOC_NR`, different `_IOC_SIZE`/`_IOC_DIR`) so newer struct versions coexist with old; the load-time `MEM_ALLOC_V2MT64` (#102) is one such size-overloaded sibling. There is **no** Linux capability check anywhere — access control is file permission on `/dev/neuronN` plus the attach gate.

> **QUIRK —** completion does *not* come back through the kernel. The doorbell ioctl is fire-and-forget; the runtime learns the inference finished by polling `mmap`'d notification rings (Stage 7), never by a kernel signal or a blocking read on the device fd.

→ [kernel/ioctl-dispatch](../kernel/ioctl-dispatch.md) — `ncdev_ioctl` dispatch, the privilege gate, and the `_IOC_NR`/`_IOC_SIZE` overload model.
→ [kernel/ioctl-dma](../kernel/ioctl-dma.md) — the DMA/mem-alloc ioctl family that backs staging.
→ [dma/descriptor-format](../dma/descriptor-format.md) — the 16-byte `al_udma_desc` trigger-descriptor wire layout.

## Stage 6 — A collective in flight

If this model was loaded by `nrt_load_collectives` (`@0xaa4c0`), the staged program includes collective-communication steps, and a ring all-reduce runs inside the very same execution the doorbell just kicked off. The schedule is emitted on-device by `libnrt`'s own `enc/alg` layer (the device-side ring step loop lives here, not in `libnccom`). `enc_metaring_primitive::compose_operation` (`@0x178170`) is the master per-op emit loop: it sizes chunks (`__set_dynamic_chunk_size @0x14bba0`), selects active channels, builds page tables, then for each channel dispatches `__compose_channel_ring` (`@0x177340`) on the `enc_op_type`.

For an all-reduce, `__compose_allreduce_channel` (`@0x171600`) emits the canonical NCCL-style two-phase schedule: a `(nranks-1)`-step **reduce-scatter** (`enc_primitive::send` → `recv_reduce_send` → `recv_reduce_copy_send`) followed by a `(nranks-1)`-step **all-gather** (`direct_recv_send` → `direct_copy_send` → `direct_recv`), with half-chunk (CHUNK_H0/H1) pipelining and credit flow control. The ring *order* is decided host-side by `libnccom`; `libnrt` re-derives the device neighbor order from the MLA topology into `enc_alg_metaring.ring_ranks[]` via `nccl_setup_alg_ring_info` (`@0x1005c0`). Each step primitive emits the actual cc-op plus its DMA descriptors — the device program that moves and reduces the data over the inter-chip interconnect.

> **NOTE —** the collective adds a substate to completion harvest. Before WAIT_CORE, the harvest state machine sits in WAIT_BARRIER_PROXY (Stage 7), where `enc_check_proxy_network_task_status` gates on the network/barrier proxy tasks the collective's net edges drive.

→ [collectives/ring-scheduling](../collectives/ring-scheduling.md) — the metaring composer, the ring all-reduce step math, and chunk sizing.
→ [collectives/overview](../collectives/overview.md) — the collective stack map and the `libnccom`/`libnrt` boundary.

## Stage 7 — Execute: harvest completion

The device writes a 16-byte notification entry into a per-engine **Notification-Queue (NQ)** INFER_STATUS ring for every START and END event; the runtime reads those `mmap`'d rings to learn the inference is done. The harvest engine is a per-request state machine, `exec_request_progress_one_step` (`@0x263330`), driven through INIT → WAIT_BARRIER_PROXY → WAIT_CORE → DONE. In WAIT_CORE it polls `notification_read_exec_queue` (`@0x2ff170`) for each `(pcore, engine)`, decodes the entry's subtype (2 = START, 3 = END), and advances a per-engine `START_PENDING → END_PENDING → COMPLETE` sub-state. When all engines reach COMPLETE, the request is DONE.

Two drivers consume that step function. The synchronous path busy-polls in `exec_wait_round_robin` (`@0x2655c0`, `usleep(1)` loop), the inner half of `kmgr_exec_wait`. The async/worker path is the lock-free XU thread `tpb_xu_step` (`@0xe8940`), which on completion calls `tpb_xu_base_report_complete` (`@0xe8800`) to pop the SPSC slot and wake the blocked caller via its `mark_comp_efd`. A stalled sweep checks elapsed time against the model timeout and probes INTC cause bit 29 to disambiguate `NRT_TIMEOUT` from `SW_NQ_OVERFLOW`; an error sweep (`notification_consume_error_block @0x2ff250`) decodes ECC/numerical/transient infer errors. Back in `kmgr_exec`, device outputs are copied to the caller's tensors and `NRT_SUCCESS` returns — the inference is complete.

→ [exec/completion-engine](../exec/completion-engine.md) — the harvest state machine, NQ decode, error sweep, and the static memory planner.
→ [exec/xu-workers](../exec/xu-workers.md) — the lock-free XU SPSC queue, the worker thread, and `mark_comp_efd` signalling.

---

## The whole map

The walkthrough touches one symbol per stage; each owning page derives the bytes:

- **Load.** [neff/load-pipeline](../neff/load-pipeline.md) (the six stages) · [neff/kelf2kbin](../neff/kelf2kbin.md) (graph→`kbin`) · [neff/compute-resource-build](../neff/compute-resource-build.md) (`model_t` build) · [runtime/api-lifecycle](../runtime/api-lifecycle.md) (`nrt_load`/`nrt_unload` API).
- **Execute.** [exec/submit-path](../exec/submit-path.md) (bind→doorbell) · [exec/completion-engine](../exec/completion-engine.md) (harvest) · [exec/xu-workers](../exec/xu-workers.md) (worker model) · [runtime/api-tensors](../runtime/api-tensors.md) (tensor binding).
- **Kernel & DMA.** [kernel/ioctl-dispatch](../kernel/ioctl-dispatch.md) (the `'N'` dispatch) · [kernel/ioctl-dma](../kernel/ioctl-dma.md) (mem/DMA family) · [dma/descriptor-format](../dma/descriptor-format.md) (descriptor wire format).
- **Collectives.** [collectives/overview](../collectives/overview.md) (the stack) · [collectives/ring-scheduling](../collectives/ring-scheduling.md) (ring emit math).
- **Whole runtime.** [runtime/overview](../runtime/overview.md) — the `libnrt` architecture this walkthrough is a slice through.

## Cross-References

- [neff/load-pipeline](../neff/load-pipeline.md) — Stages 1–3 owner: NEFF parse, KBL build, stage/relocate, TNC cache.
- [neff/kelf2kbin](../neff/kelf2kbin.md) — Stage 2 owner: KELF/`def.json` parse and the `mla_resources`→`kbin` build.
- [neff/compute-resource-build](../neff/compute-resource-build.md) — Stage 2/3 owner: `kbl_model_add` and the 7744-byte `model_t`.
- [runtime/api-lifecycle](../runtime/api-lifecycle.md) — `nrt_load`/`nrt_load_collectives`/`nrt_unload` public-API contract.
- [runtime/api-tensors](../runtime/api-tensors.md) — Stage 4 owner: tensor sets and the name→HBM bind.
- [exec/submit-path](../exec/submit-path.md) — Stages 4–5 owner: `nrt_execute` bind→stage→doorbell.
- [exec/completion-engine](../exec/completion-engine.md) — Stage 7 owner: notification harvest + static memory planner.
- [exec/xu-workers](../exec/xu-workers.md) — Stage 7 owner: the lock-free XU worker and completion signalling.
- [kernel/ioctl-dispatch](../kernel/ioctl-dispatch.md) — Stage 5 owner: `ncdev_ioctl` dispatch and the privilege model.
- [kernel/ioctl-dma](../kernel/ioctl-dma.md) — the mem-alloc / DMA ioctl family behind staging and submit.
- [dma/descriptor-format](../dma/descriptor-format.md) — the 16-byte trigger-descriptor wire layout.
- [collectives/ring-scheduling](../collectives/ring-scheduling.md) — Stage 6 owner: the ring all-reduce step schedule.
- [collectives/overview](../collectives/overview.md) — the collective stack map and `libnccom` boundary.
- [runtime/overview](../runtime/overview.md) — the whole `libnrt` runtime this inference is a path through.
