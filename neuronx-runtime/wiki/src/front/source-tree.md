# Source-Tree Reconstruction (KaenaRuntime)

> **Binary (version pin):** `libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · `2.31.24.0-0b044f4ce` · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 DYN x86-64, NOT stripped, full DWARF v4.
>
> **Part 0 — Reference Apparatus** / REFERENCE · **Evidence grade:** every directory and every translation-unit path below is a verbatim `DW_AT_name` string read from `libnrt.so`'s own `.debug_info` (331 `DW_TAG_compile_unit` records; `DW_AT_comp_dir` = `/opt/workspace/KaenaRuntime/build/private/develop/almalinux/RelWithDebInfo`; `DW_AT_producer` = `GNU C++17 14.2.1 20250110 -O2 -fPIC -ggdb3`). The tree is reconstructed *from the shipped binary*, not transcribed from any source distribution.

## Abstract

`libnrt.so` ships with full DWARF, and every object it was built from leaves one `DW_TAG_compile_unit` record carrying its source path in `DW_AT_name` and a common build root in `DW_AT_comp_dir`. Reading that compile-unit table back yields the first-party source tree without ever seeing the source: **203 KaenaRuntime translation units** (`.c` / `.cc` / `.cpp`), grouped under thirteen subsystem directories rooted at `/opt/workspace/KaenaRuntime/`, defining **4,407 functions** with their own `DW_TAG_subprogram` body (a `DW_AT_low_pc` inside the CU, deduped by `low_pc`). The remaining 122 of the 331 CUs are vendored object code statically linked in — Abseil, the Rust std/core/alloc runtime, `KaenaProfilerFormat` protobuf, libarchive, simdjson, zlib, `KaenaDriverLib` — versioned on the [Vendored-Library SBOM](../forensics/vendored-sbom.md) and excluded from this page's first-party tree.

This page is the spine for locating *which page owns which `.c`/`.cc`*. It is the directory-tree view of the same translation units the [Subsystem Matrix](../appendix/subsystem-matrix.md) indexes by subsystem Part: where the matrix answers "I need subsystem X, which TU and which page?", this page answers the inverse — "I am holding `tdrv/dma_ring.c`, where in the book does it live?". The owning-page column below is the exact `SUMMARY.md` path, kept consistent with that matrix; the subsystem→page logic is not re-derived here, only the directory tree and the per-TU function counts that the matrix draws from. Per-function semantics belong to the deep pages; this is the map, not the territory.

| | |
|---|---|
| **Source root** | `/opt/workspace/KaenaRuntime/` (`DW_AT_comp_dir` of every first-party CU) |
| **First-party CUs** | 203 translation units / 4,407 defined functions |
| **Subsystem directories** | 13 (`tdrv/`, `enc/`, `nrt/`, `kelf/`, `kmgr/`, `nds/`, `ucode/`, `utils/`, `nlog/`, `ndebug/`, `dve_config/`, `dx/`, `hw_decode/`) + a 0-fn `build/` blob-stub group |
| **Total DWARF CUs** | 331 (203 first-party + 122 vendored + 6 zlib builtin folded into vendored) |
| **Largest TU (C)** | `tdrv/instruction_block_mariana.c` — 297 fns |
| **Largest TU (C++)** | `enc/enc.cc` — 352 fns |
| **Excluded** | 122 vendored CUs (→ SBOM); the no-DWARF siblings `libncfw.so`, `libnrtucode_extisa.so` |

---

## 1. The subsystem directory tree

A compact hierarchical view of the thirteen first-party directories, fn-weighted. Each line is `CUs / fns`; the full per-TU breakdown is in [§2](#2-per-tu-inventory). The per-arch triplication (`cayman` / `mariana` / `sunda`) is real in the DWARF — each silicon gets its own `tdrv_arch_*.c`, `instruction_block_*.c`, `encd/archs/*.c`, and `dve_config_*.c`.

```text
/opt/workspace/KaenaRuntime/
├─ tdrv/         84 CU / 2413 fn   device driver, DMA, instruction-block builders, per-arch
│   ├─ cayman/    4 CU             arch fan-out: dma_desc_util, isa, tdrv_arch, dve_config
│   ├─ mariana/   2 CU             arch fan-out: tdrv_arch, dve_config
│   ├─ sunda/     4 CU             arch fan-out: isa, tdrv_arch, tsync, dve_config
│   └─ encd/      6 CU             execution-engine descriptor compiler
│       └─ archs/ 3 CU             per-arch encd: arch / cayman / mariana / sunda
├─ enc/          52 CU / 1062 fn   on-device collectives compute engine
│   ├─ async_sr/  7 CU             async point-to-point sendrecv (OFI / rendezvous / kv_store)
│   └─ switch_platform/ 29 CU      switch-fabric collective composer
│       ├─ events/{broadcast,reduce,handshake,sync,function}/   per-event algorithm TUs
│       └─ ops/{all_gather,all_reduce,all_to_all,reduce_scatter}/ op selectors (1–3 fn each)
├─ nrt/          27 CU /  377 fn   public C API + config/profile/sys_trace
├─ kelf/          5 CU /  318 fn   KELF/KBIN/NEFF container parse + numpy/HLO stats
├─ kmgr/         10 CU /  117 fn   model/kernel manager (dlr loader, xu/ worker queue)
│   └─ xu/        4 CU             execution-unit work-queue + worker threads
├─ nds/           6 CU /   36 fn   Neuron DataStore (== libnds.a, folded into libnrt)
├─ ucode/         1 CU /   26 fn   microcode loader glue
├─ utils/         5 CU /   23 fn   md5 / sha256 / instance / time helpers
├─ nlog/          3 CU /   15 fn   logging + backtrace + symbol resolution
├─ ndebug/        1 CU /   10 fn   ndebug_stream
├─ dve_config/    4 CU /    7 fn   DVE (dynamic-vector-engine) config + per-arch
├─ dx/            1 CU /    2 fn   dx/ring.c (notification ring)
├─ hw_decode/     1 CU /    1 fn   hw_decode table glue
└─ build/.../     3 CU /    0 fn   GENERATED blob stubs (*_bins.c: dve / hw_decode / ucode)
```

> **NOTE —** the directory names are not a convention imposed by this analysis; they are the leading path components of the `DW_AT_name` strings in `.debug_info` (e.g. the CU named `tdrv/encd/archs/cayman.c`). The `comp_dir` root is constant across all 203 first-party CUs, so a relative path under it is unambiguous.

> **QUIRK —** `nds/` (6 CUs) is the static archive `libnds.a` compiled once and linked in; its six `neuron_ds*.c.o` members appear in `libnrt.so`'s DWARF as first-party `nds/` CUs even though the library is also catalogued as a separate artifact. The counter plane it implements is shared with the kernel `.ko` — see [`datastore/userspace-libnds.md`](../datastore/userspace-libnds.md).

---

## 2. Per-TU inventory

Every first-party translation unit, grouped by directory. **fns** = `DW_TAG_subprogram` records with a `DW_AT_low_pc` inside the CU, deduped by `low_pc` (defined bodies only; declarations and inlined-away statics do not count). **Owning page** is the exact `SUMMARY.md` path consistent with the [Subsystem Matrix](../appendix/subsystem-matrix.md); a TU whose surface the book splits across pages is listed at its primary owner. **Confidence** grades the fn-count + owning-page assignment: `HIGH` when the matrix cites the TU path verbatim; `MEDIUM` when the page assignment is the best directory/basename fit but the TU is folded into a neighbour CU by `-O2`, or the count rolls inlined bodies upward.

> **NOTE —** the fn-counts are an `-O2` per-CU-of-definition fact: a body inlined into another CU is counted in the CU that owns the out-of-line copy, so a thin public-wrapper TU can show fewer fns than its API surface, and a per-arch builder can absorb its callees' bodies. This is a property of the build, not a miscount.

### tdrv/ — 84 CU / 2413 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `tdrv/tdrv.c` | 67 | TDRV bring-up / device lifecycle | `runtime/tdrv-lifecycle.md` | HIGH |
| `tdrv/init.c` | 28 | TDRV init sequence | `runtime/tdrv-lifecycle.md` | HIGH |
| `tdrv/tdrv_arch_type.c` | 83 | generation/arch enum + arch-ops dispatch table | `runtime/tdrv-arch-ops.md` | HIGH |
| `tdrv/helper.c` | 20 | arch-ops helpers / sync-event accessors | `runtime/tdrv-arch-ops.md` | HIGH |
| `tdrv/dma_memory.c` | 25 | device-memory (dmem) allocator | `runtime/tdrv-dmem.md` | HIGH |
| `tdrv/dma_memory_logger.c` | 22 | dmem alloc tracing | `runtime/tdrv-dmem.md` | MEDIUM |
| `tdrv/mem_ref.c` | 5 | memory-reference / static planning | `runtime/tdrv-dmem.md` | HIGH |
| `tdrv/dma_ring.c` | 44 | DMA descriptor ring engine | `runtime/tdrv-dma-rings.md` | HIGH |
| `tdrv/dma_dynamic_ring_set.c` | 5 | dynamic ring-set allocation | `runtime/tdrv-dma-rings.md` | HIGH |
| `tdrv/sw_dma_queue.c` | 6 | software DMA queue / swap-queue | `runtime/tdrv-dma-rings.md` | HIGH |
| `tdrv/dma_desc_util.c` | 11 | 16-byte descriptor builder | `dma/descriptor-format.md` | HIGH |
| `tdrv/cayman/dma_desc_util.c` | 1 | cayman descriptor delta | `runtime/arch-sdma.md` | HIGH |
| `tdrv/dma_queue_reg_offset.c` | 20 | DMA-queue register offsets | `runtime/arch-csr-offsets.md` | HIGH |
| `tdrv/dma_ring_act_tbl.c` | 10 | SDMA/CCE activation table overlay | `dma/meta-ctrl-overlays.md` | MEDIUM |
| `tdrv/dma_ring_imcpy.c` | 7 | inline-memcpy descriptor overlay | `dma/meta-ctrl-overlays.md` | MEDIUM |
| `tdrv/dma_ring_setup.c` | 2 | ring trigger/doorbell setup | `dma/ring-cycle.md` | HIGH |
| `tdrv/scratchpad.c` | 11 | HBM scratchpad | `runtime/tdrv-scratchpad.md` | HIGH |
| `tdrv/sync_point.c` | 3 | sync-point accessors | `runtime/tdrv-scratchpad.md` | HIGH |
| `tdrv/pseudo_core_barrier.c` | 6 | pseudo-core barrier | `runtime/tdrv-scratchpad.md` | MEDIUM |
| `tdrv/tensor.c` | 26 | tensor object layer | `runtime/tdrv-tensor.md` | HIGH |
| `tdrv/csr.c` | 8 | CSR / control-status registers | `runtime/arch-csr-offsets.md` | HIGH |
| `tdrv/tpb_reg_offset.c` | 9 | TPB register offsets | `runtime/arch-csr-offsets.md` | HIGH |
| `tdrv/notification.c` | 19 | notification / INTC harvest | `runtime/arch-notification.md` | HIGH |
| `tdrv/hal_platform.c` | 14 | KaenaHal platform-services adapter | `runtime/hal-adapter.md` | HIGH |
| `tdrv/instr_tpb_functions.c` | 6 | TPB/STPB FP8-dtype shim | `runtime/hal-tpb-shims.md` | MEDIUM |
| `tdrv/xt_cc.c` | 4 | Xtensa CC arch-dispatch shim | `runtime/hal-tpb-shims.md` | MEDIUM |
| `tdrv/vring.c` | 34 | virtual rings / packet builders | `dma/virtual-rings.md` | HIGH |
| `tdrv/vtpb.c` | 21 | virtual-TPB / STPB pooling | `runtime/arch-stpb.md` | MEDIUM |
| `tdrv/vtpb_info.c` | 5 | vtpb descriptor info | `runtime/arch-stpb.md` | MEDIUM |
| `tdrv/instr_dge.c` | 2 | DGE engine-init builder | `runtime/arch-stpb.md` | MEDIUM |
| `tdrv/db.c` | 15 | device book | `runtime/device-book.md` | HIGH |
| `tdrv/dbtc.c` | 9 | device-book TC | `runtime/device-book.md` | HIGH |
| `tdrv/ntrace.c` | 7 | ntrace / nlog bridge | `runtime/logging.md` | MEDIUM |
| `tdrv/pool_stdio.c` | 6 | stdio pool allocator | `runtime/logging.md` | MEDIUM |
| `tdrv/ring_buffer.c` | 4 | log ring buffer | `runtime/logging.md` | MEDIUM |
| `tdrv/exec.c` | 15 | exec hot-path driver | `exec/overview.md` | HIGH |
| `tdrv/events.c` | 4 | exec event plumbing | `exec/overview.md` | MEDIUM |
| `tdrv/hw_exec_queue.c` | 8 | submit-path HW exec queue | `exec/submit-path.md` | HIGH |
| `tdrv/io.c` | 7 | I/O request handling | `exec/submit-path.md` | MEDIUM |
| `tdrv/infer_error_subtype.c` | 4 | inference error decode | `exec/completion-engine.md` | MEDIUM |
| `tdrv/ht.c` | 19 | hash-table (exec/model index) | `exec/kmgr-facade.md` | MEDIUM |
| `tdrv/lru_cache.c` | 7 | LRU cache | `exec/kmgr-facade.md` | MEDIUM |
| `tdrv/instruction_block.c` | 8 | instruction-block builder base | `isa/overview.md` | HIGH |
| `tdrv/instr_common.c` | 4 | shared instruction helpers | `isa/overview.md` | HIGH |
| `tdrv/instruction_block_common.c` | 8 | 64-byte instruction record format | `isa/instruction-record.md` | HIGH |
| `tdrv/instruction_block_mariana.c` | 297 | mariana ISA validator / builder | `isa/validator-architecture.md` | HIGH |
| `tdrv/instruction_block_cayman.c` | 248 | cayman ISA validator / builder | `isa/validators-per-arch.md` | HIGH |
| `tdrv/instruction_block_sunda.c` | 238 | sunda ISA validator / builder | `isa/validators-per-arch.md` | HIGH |
| `tdrv/instr_pseudo_branching.c` | 8 | SP/TopSP pseudo-branch lowering | `isa/pseudo-instruction-lowering.md` | HIGH |
| `tdrv/instr_sunda_pseudo_range_check.c` | 4 | sunda pseudo range check | `isa/pseudo-instruction-lowering.md` | HIGH |
| `tdrv/instr_cayman.c` | 7 | cayman instruction helpers | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/instr_sunda.c` | 90 | sunda instruction helpers | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/instr_sunda_embedding_update.c` | 3 | sunda embedding-update instr | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/instr_sunda_swap_queue_set.c` | 7 | sunda swap-queue-set instr | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/instr_collectives.c` | 20 | cc_op on-device ISA | `collectives/cc-op-isa.md` | HIGH |
| `tdrv/instr_collectives_cayman.c` | 1 | cayman cc_op delta | `collectives/cc-op-isa.md` | MEDIUM |
| `tdrv/encd.c` | 229 | device-side descriptor emitter | `collectives/encd-overview.md` | HIGH |
| `tdrv/encd/arch_device_mapping.c` | 7 | encd DMA-descriptor / devmem floor | `collectives/encd-dma-devmem.md` | HIGH |
| `tdrv/encd/nec_vil.c` | 10 | nec/VIL libnccom-boundary glue | `collectives/nccl-boundary.md` | HIGH |
| `tdrv/encd/archs/arch.c` | 101 | per-arch encd ops dispatch (base) | `collectives/encd-arch-ops.md` | HIGH |
| `tdrv/encd/archs/cayman.c` | 95 | cayman encd ops + HW geometry | `collectives/encd-arch-ops.md` | HIGH |
| `tdrv/encd/archs/mariana.c` | 95 | mariana encd ops + HW geometry | `collectives/encd-arch-ops.md` | HIGH |
| `tdrv/encd/archs/sunda.c` | 88 | sunda encd ops + HW geometry | `collectives/encd-arch-ops.md` | HIGH |
| `tdrv/host_collectives.c` | 7 | host-side collective glue | `collectives/cc-op-isa.md` | MEDIUM |
| `tdrv/kbin.c` | 2 | KBIN container structs | `neff/kbin-structs.md` | HIGH |
| `tdrv/kbin_patch.c` | 8 | KBIN relocation patcher | `neff/kbin-structs.md` | HIGH |
| `tdrv/ucode_lib.c` | 8 | microcode carrier (upload side) | `gpsimd/microcode-loader.md` | HIGH |
| `tdrv/cayman/tdrv_arch_cayman.c` | 47 | cayman geometry / address map | `runtime/arch-geometry.md` | HIGH |
| `tdrv/mariana/tdrv_arch_mariana.c` | 46 | mariana geometry / address map | `runtime/arch-geometry.md` | HIGH |
| `tdrv/sunda/tdrv_arch_sunda.c` | 47 | sunda geometry / address map | `runtime/arch-geometry.md` | HIGH |
| `tdrv/cayman/isa.c` | 1 | cayman ISA constant table | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/sunda/isa.c` | 1 | sunda ISA constant table | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/sunda/tsync.c` | 8 | sunda time-sync | `runtime/arch-notification.md` | MEDIUM |
| `tdrv/sequencer_sunda.c` | 9 | sunda sequencer | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/cayman/dve_dynamic_config_cayman.c` | 1 | cayman DVE dynamic config | `forensics/dispatch-tables.md` | MEDIUM |
| `tdrv/mariana/dve_dynamic_config_mariana.c` | 1 | mariana DVE dynamic config | `forensics/dispatch-tables.md` | MEDIUM |
| `tdrv/sunda/dve_dynamic_config_sunda.c` | 1 | sunda DVE dynamic config | `forensics/dispatch-tables.md` | MEDIUM |
| `tdrv/dve_dynamic_config.c` | 2 | DVE dynamic-config base | `forensics/dispatch-tables.md` | MEDIUM |
| `tdrv/ds.c` | 16 | descriptor-store helpers | `runtime/tdrv-lifecycle.md` | MEDIUM |
| `tdrv/model_switch.c` | 1 | model-switch path | `neff/load-pipeline.md` | MEDIUM |
| `tdrv/mr_pointer.c` | 3 | memory-ref pointer fixups | `neff/memory-planning.md` | MEDIUM |
| `tdrv/mla_addr_check.c` | 1 | MLA address validation | `isa/validators-per-arch.md` | MEDIUM |
| `tdrv/hash_64a.c` | 2 | FNV-1a 64 hash | `forensics/string-domain.md` | MEDIUM |
| `tdrv/ioq_switcher.c` | 4 | I/O-queue switcher | `runtime/tdrv-dma-rings.md` | MEDIUM |

### enc/ — 52 CU / 1062 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `enc/enc.cc` | 352 | collective-compute engine core | `collectives/engine-core.md` | HIGH |
| `enc/enc_primitive.cc` | 200 | algorithm taxonomy / ring scheduling | `collectives/algorithm-taxonomy.md` | HIGH |
| `enc/rdh.cc` | 75 | hierarchical / RDH composition | `collectives/hierarchical-rdh.md` | HIGH |
| `enc/inter_rdh.cc` | 35 | inter-node RDH composition | `collectives/hierarchical-rdh.md` | HIGH |
| `enc/memory_segment.cc` | 15 | enc memory segment / leaves | `collectives/enc-primitives.md` | HIGH |
| `enc/replica_groups.cc` | 28 | topology partitioning (union-find) | `collectives/topology-partition.md` | HIGH |
| `enc/pod_mesh_primitive.cc` | 14 | mesh composer (`alg_mesh_initializer`) | `collectives/mesh-composer.md` | HIGH |
| `enc/barrier.cc` | 24 | switch-platform barrier tables | `collectives/switch-broadcast-barrier.md` | HIGH |
| `enc/neuron_nccl.cc` | 4 | libnrt↔libnccom comm context | `collectives/comm-context.md` | HIGH |
| `enc/nec.cc` | 17 | nec collective entry | `collectives/nccl-boundary.md` | MEDIUM |
| `enc/proxy_queue.cc` | 23 | proxy / progress engine bridge | `collectives/proxy-driver.md` | HIGH |
| `enc/bananaphone.c` | 17 | bananaphone IPC | `collectives/proxy-driver.md` | HIGH |
| `enc/async_sr/async_sr.cc` | 12 | async point-to-point sendrecv | `collectives/send-recv.md` | HIGH |
| `enc/async_sr/async_sr_ofi.cc` | 10 | OFI sendrecv transport | `collectives/send-recv.md` | HIGH |
| `enc/async_sr/async_sr_comm.cc` | 4 | sendrecv comm context | `collectives/send-recv.md` | MEDIUM |
| `enc/async_sr/async_sr_context.cc` | 1 | sendrecv context object | `collectives/send-recv.md` | MEDIUM |
| `enc/async_sr/async_sr_request.cc` | 3 | sendrecv request object | `collectives/send-recv.md` | MEDIUM |
| `enc/async_sr/async_sr_service_thread.cc` | 17 | sendrecv service thread | `collectives/send-recv.md` | MEDIUM |
| `enc/async_sr/kv_store.cc` | 25 | bootstrap KV store | `collectives/comm-context.md` | MEDIUM |
| `enc/switch_platform/enc_switch_platform.cc` | 27 | switch-platform composer | `collectives/switch-broadcast-barrier.md` | HIGH |
| `enc/switch_platform/enc_primitive_switch_platform.cc` | 9 | switch-platform primitive | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/inter_axis_broadcast_event.cc` | 11 | inter-axis broadcast event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/inter_axis_bw_opt_broadcast_event.cc` | 6 | inter-axis BW-opt broadcast | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/intra_axis_broadcast_event.cc` | 14 | intra-axis broadcast event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/intra_axis_bw_opt_broadcast_event.cc` | 3 | intra-axis BW-opt broadcast | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/intra_chip_broadcast_event.cc` | 10 | intra-chip broadcast event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/proxy_buff_pull_event.cc` | 6 | proxy-buffer pull event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/proxy_buff_supported_broadcast_event.cc` | 6 | proxy-buffer broadcast event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/proxy_buff_supported_chunk_broadcast_event.cc` | 4 | proxy-buffer chunk broadcast | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/broadcast/rmv_only_src_dst_routing_all_to_all_broadcast_event.cc` | 7 | rmv-routing all-to-all broadcast | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/reduce/inter_axis_dimr1w_event.cc` | 8 | inter-axis dim-r1w reduce | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/events/reduce/intra_axis_chipr1w_event.cc` | 7 | intra-axis chip-r1w reduce | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/events/reduce/intra_rank_dimr1w_event.cc` | 8 | intra-rank dim-r1w reduce | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/events/reduce/proxy_buff_supported_local_reduce_event.cc` | 8 | proxy-buffer local reduce | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/events/reduce/proxy_buff_supported_remote_reduce_event.cc` | 7 | proxy-buffer remote reduce | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/events/reduce/single_step_reduce_event.cc` | 8 | single-step reduce event | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/events/handshake/all_rank_handshake_event.cc` | 3 | all-rank handshake event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/handshake/intra_chip_handshake_event.cc` | 3 | intra-chip handshake event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/handshake/single_hop_pcie_peer_handshake_event.cc` | 3 | single-hop PCIe peer handshake | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/sync/dma_sync.cc` | 3 | DMA sync event | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/events/function/function.cc` | 2 | event-function base | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| `enc/switch_platform/ops/all_gather/all_gather_bw_opt.cc` | 2 | all-gather BW-opt selector | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_gather/all_gather_latency_opt.cc` | 2 | all-gather latency-opt selector | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_gather/all_gather_one_rank_per_chip.cc` | 2 | all-gather one-rank-per-chip | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_reduce/all_reduce_bw_opt.cc` | 2 | all-reduce BW-opt selector | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_reduce/all_reduce_latency_opt.cc` | 2 | all-reduce latency-opt selector | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_reduce/all_reduce_one_rank_per_chip.cc` | 2 | all-reduce one-rank-per-chip | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_reduce/all_reduce_single_step.cc` | 2 | all-reduce single-step | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_to_all/all_to_all_latency_opt.cc` | 2 | all-to-all latency-opt selector | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/all_to_all/all_to_all_rmv_only_src_dst_routing.cc` | 3 | all-to-all rmv-routing selector | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/reduce_scatter/reduce_scatter.cc` | 2 | reduce-scatter selector | `collectives/algorithm-taxonomy.md` | MEDIUM |
| `enc/switch_platform/ops/reduce_scatter/reduce_scatter_one_rank_per_chip.cc` | 2 | reduce-scatter one-rank-per-chip | `collectives/algorithm-taxonomy.md` | MEDIUM |

> **GOTCHA —** the 27 `enc/switch_platform/events/*` and `ops/*` TUs are template-instantiated and `-O2`-inlined; `addr2line` on their `low_pc`s resolves to STL header frames, so the address-band coverage cells folded them into `enc.cc` / `enc_primitive.cc` *by address* without naming the event TU. Their owning-page cells above (`switch-broadcast-barrier.md` for broadcast/barrier/handshake/sync, `algorithm-taxonomy.md` for reduce/op-selectors) are asserted by *directory membership*, not by an address-grounded source-filename cite — the lowest-confidence first-party rows on this page. This is the same gap recorded in the matrix CORRECTION (F-SRCMAP §4b).

### nrt/ — 27 CU / 377 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `nrt/nrt_init.cpp` | 16 | public API lifecycle / init | `runtime/api-lifecycle.md` | HIGH |
| `nrt/nrt_infodump.cpp` | 9 | info-dump API | `runtime/api-lifecycle.md` | HIGH |
| `nrt/nrt_config.cpp` | 51 | `NEURON_RT_*` env parse table | `runtime/env-vars.md` | HIGH |
| `nrt/nrt_tensor.cpp` | 5 | tensor API wrapper | `runtime/api-tensors.md` | HIGH |
| `nrt/nrt_async.cpp` | 22 | async API | `runtime/api-async-collectives.md` | HIGH |
| `nrt/nrt_async_sendrecv.cpp` | 11 | sendrecv API thunks | `runtime/api-async-collectives.md` | HIGH |
| `nrt/nrt_collectives.cpp` | 6 | collectives API thunks | `runtime/api-async-collectives.md` | HIGH |
| `nrt/nrt_barrier.cpp` | 2 | barrier API | `runtime/api-async-collectives.md` | MEDIUM |
| `nrt/nrt_exec.cpp` | 3 | exec API surface | `exec/overview.md` | MEDIUM |
| `nrt/nrt_model.cpp` | 5 | model API surface | `neff/load-pipeline.md` | MEDIUM |
| `nrt/nrt_status.cpp` | 1 | `NRT_STATUS` strings | `runtime/error-codes.md` | HIGH |
| `nrt/nrt_status_priority.cpp` | 1 | status-priority ranking | `runtime/error-codes.md` | HIGH |
| `nrt/nrt_state.cpp` | 3 | runtime state object | `runtime/overview.md` | MEDIUM |
| `nrt/nrt_stats.cpp` | 0 | stats API (fully inlined) | `runtime/overview.md` | MEDIUM |
| `nrt/nrt_ucode.cpp` | 1 | ucode dlopen facade (C side) | `gpsimd/ucode-facade.md` | HIGH |
| `nrt/nrt_interned_string_db.cpp` | 5 | interned string database | `runtime/interned-strings.md` | HIGH |
| `nrt/nrt_vnc_usage.cpp` | 5 | virtual-NC usage tracking | `runtime/api-device-config.md` | MEDIUM |
| `nrt/nrt_profile.cpp` | 94 | profile API / three trace producers | `trace/overview.md` | HIGH |
| `nrt/nrt_inspect.cpp` | 76 | inspect/profile API | `trace/inspect-profile-api.md` | HIGH |
| `nrt/nrt_inspect_config.cpp` | 22 | inspect config parse | `trace/inspect-profile-api.md` | HIGH |
| `nrt/nrt_system_monitor.cpp` | 14 | system monitor | `trace/system-monitor.md` | HIGH |
| `nrt/nrt_debug_stream.cpp` | 3 | debug stream | `trace/system-monitor.md` | HIGH |
| `nrt/nrt_sys_trace.cpp` | 2 | SysTraceEventType taxonomy | `trace/event-taxonomy.md` | HIGH |
| `nrt/nrt_sys_trace_api.cpp` | 4 | sys_trace API | `trace/event-taxonomy.md` | HIGH |
| `nrt/nrt_sys_trace_api_options.cpp` | 5 | sys_trace API options | `trace/event-taxonomy.md` | MEDIUM |
| `nrt/nrt_sys_trace_capture.cpp` | 3 | sys_trace capture (Rust FFI C side) | `trace/rust-ffi.md` | HIGH |
| `nrt/nrt_sys_trace_capture_config.cpp` | 8 | sys_trace capture config | `trace/rust-ffi.md` | MEDIUM |

### kelf/ — 5 CU / 318 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `kelf/kelf.cpp` | 184 | KELF/NEFF container parse + section taxonomy | `neff/section-taxonomy.md` | HIGH |
| `kelf/kelf2kbin.cpp` | 126 | JSON → KBIN lowering | `neff/kelf2kbin.md` | HIGH |
| `kelf/neff.cpp` | 6 | NEFF container entry | `neff/container.md` | HIGH |
| `kelf/load_numpy.c` | 1 | numpy dtype loader | `neff/dtype-system.md` | HIGH |
| `kelf/model_hlo_stats.cpp` | 1 | HLO model stats | `neff/overview.md` | MEDIUM |

### kmgr/ — 10 CU / 117 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `kmgr/dlr.cpp` | 43 | load pipeline / exec-manager facade | `neff/load-pipeline.md` | HIGH |
| `kmgr/dlr_kelf.cpp` | 7 | KELF-specific load path | `neff/load-pipeline.md` | HIGH |
| `kmgr/dlr_io_check.cc` | 4 | load I/O validation | `neff/load-pipeline.md` | MEDIUM |
| `kmgr/neff_json_parser.cpp` | 8 | NEFF JSON metadata parse | `neff/metadata-schema.md` | HIGH |
| `kmgr/kmgr_metrics.cpp` | 6 | exec-manager metrics | `exec/kmgr-facade.md` | MEDIUM |
| `kmgr/kmgr_async_exec.cc` | 11 | async exec manager | `exec/overview.md` | HIGH |
| `kmgr/xu/queue.c` | 9 | XU work-queue | `exec/xu-workers.md` | HIGH |
| `kmgr/xu/xu_worker.cc` | 4 | XU worker thread | `exec/xu-workers.md` | HIGH |
| `kmgr/xu/tpb_xu.cc` | 21 | TPB execution unit | `exec/xu-workers.md` | HIGH |
| `kmgr/xu/comp_handle_pool.c` | 4 | completion-handle pool | `exec/xu-queue-abi.md` | HIGH |

### nds/ — 6 CU / 36 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `nds/neuron_ds.c` | 6 | datastore overview / accessor | `datastore/userspace-libnds.md` | HIGH |
| `nds/neuron_ds_counters.c` | 9 | counter plane | `datastore/userspace-libnds.md` | HIGH |
| `nds/neuron_ds_object.c` | 6 | NDS object / wire format | `datastore/wire-format.md` | HIGH |
| `nds/neuron_ds_models.c` | 8 | NDS model records | `datastore/wire-format.md` | HIGH |
| `nds/neuron_ds_process.c` | 4 | per-process plane | `datastore/userspace-libnds.md` | HIGH |
| `nds/neuron_ds_helpers.c` | 3 | NDS helpers | `datastore/userspace-libnds.md` | MEDIUM |

### ucode/ — 1 CU / 26 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `ucode/ucode.c` | 26 | microcode loader / ext-ISA dlopen facade | `gpsimd/microcode-loader.md` | HIGH |

### utils/ — 5 CU / 23 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `utils/instance_helpers.c` | 11 | instance-type detection | `runtime/api-device-config.md` | HIGH |
| `utils/md5.c` | 5 | MD5 digest | `forensics/string-domain.md` | MEDIUM |
| `utils/sha256.c` | 4 | SHA-256 digest | `forensics/string-domain.md` | MEDIUM |
| `utils/time_helpers.c` | 3 | time helpers | `forensics/string-domain.md` | MEDIUM |
| `utils/file.cpp` | 0 | file helpers (fully inlined) | `forensics/string-domain.md` | MEDIUM |

### nlog/ — 3 CU / 15 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `nlog/nlog.c` | 14 | logging core | `runtime/logging.md` | HIGH |
| `nlog/backtrace.c` | 1 | backtrace capture | `runtime/logging.md` | MEDIUM |
| `nlog/resolutions.c` | 0 | symbol resolution (fully inlined) | `runtime/logging.md` | MEDIUM |

### ndebug/ — 1 CU / 10 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `ndebug/ndebug_stream.c` | 10 | ndebug stream | `trace/system-monitor.md` | HIGH |

### dve_config/ — 4 CU / 7 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `dve_config/dve_config.c` | 1 | DVE config base | `forensics/dispatch-tables.md` | MEDIUM |
| `dve_config/dve_config_cayman.c` | 2 | cayman DVE config | `forensics/dispatch-tables.md` | MEDIUM |
| `dve_config/dve_config_mariana.c` | 2 | mariana DVE config | `forensics/dispatch-tables.md` | MEDIUM |
| `dve_config/dve_config_sunda.c` | 2 | sunda DVE config | `forensics/dispatch-tables.md` | MEDIUM |

### dx/, hw_decode/, build/ — 5 CU / 3 fn

| TU | fns | Role | Owning page | Conf |
|---|---:|---|---|---|
| `dx/ring.c` | 2 | notification ring | `kernel/notification-queues.md` | MEDIUM |
| `hw_decode/hw_decode.c` | 1 | hw_decode table glue | `neff/dtype-system.md` | MEDIUM |
| `build/.../dve_config/dve_config_bins.c` | 0 | generated DVE blob stub | `forensics/static-init.md` | MEDIUM |
| `build/.../hw_decode/hw_decode_bins.c` | 0 | generated hw_decode blob stub | `forensics/static-init.md` | MEDIUM |
| `build/.../ucode/ucode_bins.c` | 0 | generated ucode blob stub | `forensics/static-init.md` | MEDIUM |

---

## 3. The total and what is excluded

The thirteen subsystem directories plus the `build/` blob stubs sum to the **203 first-party CUs / 4,407 defined functions** reported in the at-a-glance table. The per-subsystem rollup:

| Directory | CUs | fns |
|---|---:|---:|
| `tdrv/` | 84 | 2413 |
| `enc/` | 52 | 1062 |
| `nrt/` | 27 | 377 |
| `kelf/` | 5 | 318 |
| `kmgr/` | 10 | 117 |
| `nds/` | 6 | 36 |
| `ucode/` | 1 | 26 |
| `utils/` | 5 | 23 |
| `nlog/` | 3 | 15 |
| `ndebug/` | 1 | 10 |
| `dve_config/` | 4 | 7 |
| `dx/` | 1 | 2 |
| `hw_decode/` | 1 | 1 |
| `build/` (blob stubs) | 3 | 0 |
| **Total first-party** | **203** | **4407** |

What this tree deliberately excludes:

- **The 122 vendored CUs** — the other half of the 331-CU table. Their object code is statically linked into `libnrt.so`'s `.text` (Abseil, Rust std/core/alloc + crates, `KaenaProfilerFormat`'s `ntff.pb.cc` / `neuron_trace.pb.cc`, libarchive, simdjson, zlib, `KaenaDriverLib`'s `ndl.c`), but they are not KaenaRuntime source and are versioned on the [Vendored-Library SBOM](../forensics/vendored-sbom.md), not here. The `KaenaProfilerFormat` protobuf is *generated* from an AWS-authored `.proto`, so it lands in the vendored half by origin, not by being third-party OSS.
- **The no-DWARF sibling binaries** — `libncfw.so` (collectives firmware carrier) and `libnrtucode_extisa.so` (GPSIMD/Q7 ext-ISA provider) carry no compile units, so no source tree is recoverable from them; their structure is documented in Parts X–XI from binary layout, not DWARF.
- **The kernel `.ko`** (`aws-neuronx-dkms`) — a separate GPL binary with its own source tree; its TU filenames are reconstructed from the GPL DKMS tree in Part III, not from `libnrt.so`'s DWARF.

> **NOTE —** the DWARF compile-unit count (203 first-party) is the authoritative TU count and supersedes the earlier `__FILE__`-string-derived figure (221), which counted headers (`.h` / `.hpp`) that have no compile unit of their own. The DWARF count is *translation units only*: a header included by many TUs leaves no CU, so it never appears in this tree.

---

## Cross-References

- [Subsystem ↔ Binary ↔ Source-TU Matrix](../appendix/subsystem-matrix.md) — the same 203 TUs indexed by subsystem Part; the source of every owning-page cell above and the dual-organization straddles
- [Vendored-Library SBOM](../forensics/vendored-sbom.md) — version pins for the 122 vendored CUs this tree excludes (Abseil, protobuf, Rust, libarchive, zlib, simdjson, `KaenaDriverLib`)
- [Binary Layout](../reference/binary-layout.md) — the ELF section/segment census these compile units compile into
- [Symbol-Version Manifest](../appendix/symbol-versions.md) — the `NRT_2.0.0` / `NRT_3.0.0` `.gnu.version_d` nodes that export the public surface of the `nrt/` TUs
