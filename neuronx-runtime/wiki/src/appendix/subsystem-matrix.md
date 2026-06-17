# Subsystem ↔ Binary ↔ Source-TU Matrix

> **Binaries (version pin):**
> `libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · `2.31.24.0-0b044f4ce` · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 DYN x86-64, NOT stripped, full DWARF v4 ·
> `aws-neuronx-dkms` **2.27.4.0** (GPL-2.0 `.ko`, kernel module) ·
> `libncfw.so` · **BuildID** `a98f8e1c…` · firmware carrier, NO DWARF ·
> `libnrtucode_extisa.so` · **BuildID** `7bb03bc4…` · GPSIMD ext-ISA provider, NO DWARF ·
> `libnccom.so` · **BuildID** `9c00176c…` + `libnccom-net.so` · **BuildID** `3415f096…` · NCCL-fork collectives (dlopen'd, NOT `DT_NEEDED`) ·
> `libnds.a` · datastore static lib (6 `neuron_ds*.c.o` members, compiled once and linked into `libnrt.so`).
>
> **Part XVI — Appendices** / REFERENCE · **Evidence grade:** every source-TU cell is a verbatim DWARF `DW_AT_name` path from `libnrt.so`'s `.debug_info` (331 compile units, `comp_dir` = `/opt/workspace/KaenaRuntime/build/private/develop/almalinux/RelWithDebInfo`); every binary boundary is from `readelf -d/-V` and the dlopen string table.

## Abstract

This page is the master jump table of the book. Every other page documents one subsystem in depth; this matrix answers the orthogonal question — *"I need subsystem X: which binary is it in, which translation unit implements it, and which page covers it?"* — in a single lookup. The columns are deliberately the four coordinates a reimplementer needs before opening any deep page: the **Part** (where the book files it), the **subsystem**, the **binary** it lives in, the **source TU(s)** recovered from DWARF, and the **exact wiki page path**. Each row carries a Confidence tag because the binary→TU→page mapping is itself reverse-engineered: a row is `HIGH` when a DWARF compile-unit path is cited verbatim by a coverage cell, `MEDIUM` when the TU is name-matched only by basename or folded into a neighbour CU by `-O2` inlining, and `LOW` when the binary is present but carries no DWARF (`libncfw.so`, `libnrtucode_extisa.so`) so the TU column is structural, not address-grounded.

The wiki is **dual-organized**, and this matrix is the index across both organizations. One axis is the *subsystem Part* (Parts I–XV group code by what it does — silicon model, model loading, collectives, profiling), and the row order below follows it. The other axis is the *binary lane* (Part II is the `libnrt.so` forensics lane; Part III is the kernel-driver `.ko` lane; Part X/XI/XII are the firmware/microcode/NCCL-fork binary lanes). A subsystem can appear on both axes — the datastore is a one-page map in the Part III kernel lane (`kernel/datastore.md`) and a four-page deep treatment in its own Part XIV (`datastore/*`), because the counter plane straddles the `.ko` and `libnds.a`. The `## Dual-Organization` note at the foot of the page enumerates those straddles so a reader who lands on one face can find the other.

## Binary legend

| Binary | SONAME / name | BuildID | DWARF? | Role |
|---|---|---|---|---|
| `libnrt.so.2.31.24.0` | `libnrt.so.1` | `8bb57aba…` (sha1) | full DWARF v4 | Userspace runtime: public C API, TDRV device driver, NEFF/KELF load, exec, DMA, on-device collectives engine, profiling. 203 first-party CUs / 4,407 fns. |
| `aws-neuronx-dkms` 2.27.4.0 | `neuron.ko` | (GPL `.ko`) | kernel DWARF (GPL source) | Kernel driver: PCI probe, char-dev/IOCTL plane, DMA op layer, UDMA, DHAL vtables, NQ engine, reset, kernel datastore slabs, sysfs/metrics. |
| `libncfw.so` | `libncfw.so` | `a98f8e1c…` | **none** | Collectives-firmware carrier: 8 embedded Xtensa CC-engine blobs + per-arch serializers; dlopen'd by the runtime for firmware upload. |
| `libnrtucode_extisa.so` | `libnrtucode_extisa.so` | `7bb03bc4…` | **none** | GPSIMD/Q7 ext-ISA provider (`nrtucode_*` API), 13 Q7 microcode blobs, IVP vector-ISA tables; dlopen'd by `ucode.c`. |
| `libnccom.so` + `libnccom-net.so` | `libnccom.so` / `libnccom-net.so` | `9c00176c…` / `3415f096…` | (separate pkg) | NCCL-fork multi-node collectives transport + aws-ofi-nccl/libfabric net plugin. **dlopen'd, not `DT_NEEDED`**; the bidirectional ABI is 37 `neuron*` imports ← 16 `nec_*` reverse-callbacks. |
| `libnds.a` | (static archive) | — | folded into `libnrt.so` DWARF | Neuron DataStore static lib: 6 `neuron_ds*.c.o` members == the `nds/` first-party CUs; compiled once, statically linked into `libnrt.so`. |

> **NOTE —** the on-device collectives *algorithm* engine (`enc_*`/`alg_ring`/`kangaring`/`mesh`/`rdh`) is **statically embedded inside `libnrt.so`** (the `enc/` and `tdrv/encd/` CUs), NOT in `libncfw.so` or `libnccom.so`. Only the *multi-node transport* crosses the dlopen boundary into `libnccom.so`. Do not assume "collectives" == "the dlopen'd lib"; most of the collective compute is in `libnrt.so` itself — see Part IX vs Part XII below.

---

## How to read a row

`binary` is where the code executes. `source TU(s)` is the DWARF `DW_AT_name` path under `/opt/workspace/KaenaRuntime/` (first-party) or the vendored/`.ko`/firmware origin. `wiki page` is the **exact** `SUMMARY.md` path — every cell below is a real, shipped page. A `+` between TUs lists co-implementers of one subsystem; a TU listed under two Parts means the book splits one TU's surface across two pages (e.g. `nrt_profile.cpp` → inspect-profile API in Part XIII).

---

## Part 0 — Reference Apparatus

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| The five-binary map (this index's prose companion) | all five | — (cross-binary census) | `front/five-binaries.md` | HIGH |
| First-party source-tree reconstruction | `libnrt.so` | all 203 first-party CUs (DWARF `comp_dir`) | `front/source-tree.md` | HIGH |
| Binary section/segment layout census | `libnrt.so` | (ELF structure, no TU) | `reference/binary-layout.md` | HIGH |
| Extraction provenance and package set | all five | (package manifests) | `reference/extraction-status.md` | HIGH |

---

## Part I — Silicon & Architecture Model

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Generation enum (V2/V3/V4) + cloud naming | `libnrt.so` | `tdrv/tdrv_arch_type.c` | `arch/generations-enum.md` | HIGH |
| PCI device-ID → arch map | `libnrt.so` + `.ko` | `tdrv/tdrv_arch_type.c` (+ kernel `pci-probe`) | `arch/pci-device-ids.md` | HIGH |
| Per-generation HW geometry | `libnrt.so` | `tdrv/encd/archs/{cayman,mariana,sunda}.c` | `arch/hw-geometry.md` | HIGH |
| Coretype numbering | `libnrt.so` | `tdrv/tdrv_arch_type.c` | `arch/coretype-numbering.md` | HIGH |
| Memory hierarchy / BAR / state buffer | `libnrt.so` | `tdrv/tdrv_arch_type.c` + `tdrv/dma_memory.c` | `arch/memory-hierarchy.md` | MEDIUM |

> **NOTE —** the silicon model is recovered from `libnrt.so`'s per-arch fan-out (`cayman`/`mariana`/`sunda`), not from the `.ko`. The three `*ArchHeaders` Brazil packages (`CaymanArchHeaders-1.0.826.0`, `MarianaArchHeaders-1.0.1133.0`, `SundaArchHeaders-1.0.1851.0`) version-stamp the register layouts these TUs compile against but carry no functions.

---

## Part II — Binary Anatomy & Forensics (libnrt.so)

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Heavy-frame census (first-party vs vendored) | `libnrt.so` | all 331 CUs (DWARF partition) | `forensics/overview.md` | HIGH |
| ELF anatomy (`DT_NEEDED`, sections, DWARF) | `libnrt.so` | (ELF structure) | `forensics/elf-anatomy.md` | HIGH |
| Vendored SBOM (Abseil/protobuf/Rust/libarchive/zlib/simdjson) | `libnrt.so` | 122 vendored CUs (see SBOM page) | `forensics/vendored-sbom.md` | HIGH |
| Static-init pipeline | `libnrt.so` | (`.init_array`, cross-TU) | `forensics/static-init.md` | MEDIUM |
| Globals / singletons atlas | `libnrt.so` | `kmgr/xu/comp_handle_pool.c` + cross-TU `.bss` | `forensics/globals-atlas.md` | MEDIUM |
| Dispatch-table taxonomy | `libnrt.so` | `tdrv/tdrv_arch_type.c` + `tdrv/encd/archs/arch.c` | `forensics/dispatch-tables.md` | MEDIUM |
| C++ class hierarchy / RTTI | `libnrt.so` | `enc/*.cc` + Abseil/protobuf vtables | `forensics/rtti-class-hierarchy.md` | MEDIUM |
| String domain | `libnrt.so` | `.rodata` + `nrt/nrt_interned_string_db.cpp` | `forensics/string-domain.md` | HIGH |
| CRT / PLT / loader surface | `libnrt.so` | (CRT glue, `.plt`) | `forensics/crt-plt.md` | HIGH |

---

## Part III — Kernel Driver (DKMS, GPL Source)

Every row in this Part lives in `aws-neuronx-dkms` 2.27.4.0 (`neuron.ko`); the source TUs are kernel `.c` files from the GPL DKMS tree, **not** the `libnrt.so` DWARF set. Listed by page; TUs given where the GPL filename is known.

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Driver overview | `.ko` | (DKMS root) | `kernel/overview.md` | HIGH |
| Module layout / build | `.ko` | `Makefile` + module init | `kernel/module-layout.md` | HIGH |
| PCI probe / device detection | `.ko` | `neuron_pci.c` | `kernel/pci-probe.md` | HIGH |
| Char device, fops, mmap | `.ko` | `neuron_cdev.c` | `kernel/cdev-mmap.md` | HIGH |
| IOCTL dispatch + privilege gate | `.ko` | `neuron_ioctl.c` | `kernel/ioctl-dispatch.md` | HIGH |
| IOCTL catalog | `.ko` | `neuron_ioctl.c` | `kernel/ioctl-catalog.md` | HIGH |
| Memory IOCTL handlers | `.ko` | `neuron_ioctl.c` + `neuron_mempool.c` | `kernel/ioctl-mem.md` | HIGH |
| DMA IOCTL handlers | `.ko` | `neuron_ioctl.c` + `neuron_dma.c` | `kernel/ioctl-dma.md` | HIGH |
| NQ / semaphore IOCTL handlers | `.ko` | `neuron_ioctl.c` + `neuron_nq.c` | `kernel/ioctl-nq.md` | HIGH |
| Pod / misc IOCTL handlers | `.ko` | `neuron_ioctl.c` | `kernel/ioctl-pod.md` | HIGH |
| Memory pool / MC handle table | `.ko` | `neuron_mempool.c` | `kernel/mempool-handles.md` | HIGH |
| DMA op layer / completion marker | `.ko` | `neuron_dma.c` | `kernel/dma-op-layer.md` | HIGH |
| DMA rings / H2T queues | `.ko` | `neuron_ring.c` | `kernel/dma-rings.md` | HIGH |
| UDMA core (Annapurna/Alpine fork) | `.ko` | `udma/udma.c` | `kernel/udma-main.md` | HIGH |
| UDMA M2M builder | `.ko` | `udma/udma_m2m.c` | `kernel/udma-m2m.md` | MEDIUM |
| UDMA IOFIC interrupt controller | `.ko` | `udma/udma_iofic.c` | `kernel/udma-iofic.md` | MEDIUM |
| DHAL core (ndhal vtable-of-vtables) | `.ko` | `ndhal/neuron_dhal.c` | `kernel/dhal-core.md` | HIGH |
| DHAL V2 (Trn1/Inf2) | `.ko` | `ndhal/ndhal_v2.c` | `kernel/dhal-v2.md` | HIGH |
| DHAL V3 (Trn2) | `.ko` | `ndhal/ndhal_v3.c` | `kernel/dhal-v3.md` | HIGH |
| DHAL V4 (Trn3) | `.ko` | `ndhal/ndhal_v4.c` | `kernel/dhal-v4.md` | HIGH |
| Notification-queue engine | `.ko` | `neuron_nq.c` | `kernel/notification-queues.md` | HIGH |
| TopSP notification path | `.ko` | `neuron_topsp.c` | `kernel/topsp.md` | MEDIUM |
| Reset state machine | `.ko` | `neuron_reset.c` | `kernel/reset.md` | HIGH |
| Cooperative RW lock (CRWL) | `.ko` | `neuron_crwl.c` | `kernel/crwl.md` | MEDIUM |
| Pod election (UltraServer) | `.ko` | `neuron_pod.c` | `kernel/pod-election.md` | MEDIUM |
| FW-IO MiscRAM mailbox | `.ko` | `fw_io.c` | `kernel/fw-io.md` | MEDIUM |
| DMA-buf export / P2P | `.ko` | `neuron_dmabuf.c` | `kernel/dmabuf-p2p.md` | MEDIUM |
| Kernel datastore (per-process slabs) | `.ko` | `neuron_ds.c` (kernel) | `kernel/datastore.md` | HIGH |
| Metrics aggregation | `.ko` | `neuron_metrics.c` | `kernel/metrics.md` | MEDIUM |
| Sysfs metrics tree | `.ko` | `neuron_sysfs_metrics.c` | `kernel/sysfs.md` | MEDIUM |
| Power telemetry | `.ko` | `neuron_power.c` | `kernel/power.md` | MEDIUM |
| Log ring / PID / trace / reg / core | `.ko` | `neuron_log.c` + misc | `kernel/misc.md` | MEDIUM |

> **NOTE —** the kernel TU filenames are reconstructed from the GPL DKMS source tree, not from `libnrt.so` DWARF (the `.ko` is a separate binary). The `KaenaHal-2.31.0.0` `al_hal_*`/`aws_hal_*` DHAL has **NO** compile-unit in `libnrt.so` — it is kernel/driver-side; its strings reach `libnrt.so` only through inlined assert headers. That negative DWARF evidence is what assigns DHAL to the Part III `.ko` lane, not the Part IV userspace lane.

---

## Part IV — Userspace Runtime Core (libnrt.so)

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Runtime overview | `libnrt.so` | `nrt/nrt_init.cpp` + cross-TU | `runtime/overview.md` | HIGH |
| Public C API: lifecycle / init / teardown | `libnrt.so` | `nrt/nrt_init.cpp` + `nrt/nrt_infodump.cpp` | `runtime/api-lifecycle.md` | HIGH |
| Public C API: device / config / env parse | `libnrt.so` | `nrt/nrt_config.cpp` + `utils/instance_helpers.c` | `runtime/api-device-config.md` | HIGH |
| Public C API: tensor surface / I/O | `libnrt.so` | `nrt/nrt_tensor.cpp` + `tdrv/tensor.c` | `runtime/api-tensors.md` | HIGH |
| Public C API: async / sendrecv / collectives thunks | `libnrt.so` | `nrt/nrt_async.cpp` + `nrt/nrt_async_sendrecv.cpp` + `nrt/nrt_collectives.cpp` | `runtime/api-async-collectives.md` | HIGH |
| Config structs (`nrt_config`/`nrt_global_config`) | `libnrt.so` | `nrt/nrt_config.cpp` | `runtime/config-structs.md` | HIGH |
| Env-var catalog (`NEURON_RT_*`) | `libnrt.so` | `nrt/nrt_config.cpp` (51 parse fns) | `runtime/env-vars.md` | HIGH |
| Error / status codes (`NRT_STATUS`) | `libnrt.so` | `nrt/nrt_status.cpp` + `nrt/nrt_status_priority.cpp` | `runtime/error-codes.md` | MEDIUM |
| Interned string database | `libnrt.so` | `nrt/nrt_interned_string_db.cpp` | `runtime/interned-strings.md` | HIGH |
| TDRV bring-up / lifecycle | `libnrt.so` | `tdrv/tdrv.c` + `tdrv/init.c` | `runtime/tdrv-lifecycle.md` | HIGH |
| TDRV device-memory allocator (dmem) | `libnrt.so` | `tdrv/dma_memory.c` + `tdrv/mem_ref.c` | `runtime/tdrv-dmem.md` | HIGH |
| TDRV DMA descriptor rings / swap-queue | `libnrt.so` | `tdrv/dma_ring.c` + `tdrv/dma_dynamic_ring_set.c` + `tdrv/sw_dma_queue.c` | `runtime/tdrv-dma-rings.md` | HIGH |
| TDRV HBM scratchpad / sync-point | `libnrt.so` | `tdrv/scratchpad.c` + `tdrv/sync_point.c` | `runtime/tdrv-scratchpad.md` | HIGH |
| TDRV tensor object layer | `libnrt.so` | `tdrv/tensor.c` | `runtime/tdrv-tensor.md` | HIGH |
| TDRV arch-ops dispatch / sync-event accessors | `libnrt.so` | `tdrv/tdrv_arch_type.c` + `tdrv/helper.c` | `runtime/tdrv-arch-ops.md` | HIGH |
| Per-arch geometry / address map | `libnrt.so` | `tdrv/{cayman,sunda,mariana}/tdrv_arch_*.c` | `runtime/arch-geometry.md` | HIGH |
| Per-arch CSR / reg-offset accessors | `libnrt.so` | `tdrv/csr.c` + `tdrv/dma_queue_reg_offset.c` + `tdrv/tpb_reg_offset.c` | `runtime/arch-csr-offsets.md` | HIGH |
| Per-arch notification / INTC HAL | `libnrt.so` | `tdrv/notification.c` + `tdrv/hal_platform.c` | `runtime/arch-notification.md` | MEDIUM |
| Per-arch SDMA / UDMA M2M engine | `libnrt.so` | `tdrv/dma_ring.c` + `tdrv/{cayman}/dma_desc_util.c` | `runtime/arch-sdma.md` | MEDIUM |
| Per-arch STPB engine-init / DGE / pooling | `libnrt.so` | `tdrv/instr_dge.c` + `tdrv/vtpb.c` | `runtime/arch-stpb.md` | MEDIUM |
| KaenaHal adapter / platform services | `libnrt.so` | `tdrv/hal_platform.c` | `runtime/hal-adapter.md` | MEDIUM |
| KaenaHal TPB/STPB arch-dispatch shims | `libnrt.so` | `tdrv/instr_tpb_functions.c` + `tdrv/xt_cc.c` | `runtime/hal-tpb-shims.md` | MEDIUM |
| KaenaHal register / reg-offset accessors | `libnrt.so` | `tdrv/csr.c` + `tdrv/tpb_reg_offset.c` | `runtime/hal-registers.md` | MEDIUM |
| KaenaHal UDMA/SDMA descriptor build + IOFIC | `libnrt.so` | `tdrv/dma_ring.c` + `tdrv/vring.c` | `runtime/hal-udma-iofic.md` | MEDIUM |
| Logging (nlog) / ntrace / Rust logger bridge | `libnrt.so` | `nlog/nlog.c` + `tdrv/ntrace.c` (+ Rust `log` crate) | `runtime/logging.md` | HIGH |
| Device book (db / dbtc) | `libnrt.so` | `tdrv/db.c` + `tdrv/dbtc.c` | `runtime/device-book.md` | HIGH |
| NDL: neuron driver layer (IOCTL/mmap wrappers) | `libnrt.so` (vendored `KaenaDriverLib`) | `KaenaDriverLib-2.27.4.0/src/ndl.c` | `runtime/ndl.md` | HIGH |

> **QUIRK —** `runtime/ndl.md` documents code that is in `libnrt.so` but is **not first-party**: `ndl.c` (123 fns) is the vendored `KaenaDriverLib-2.27.4.0` userspace driver shim, statically linked in. The same package's kernel-side counterpart is the `.ko` IOCTL plane (Part III). The `nec_*` reverse-callbacks from `libnccom.so` tail-call into this NDL layer (e.g. `nec_get_device_count` → `jmp ndl_available_devices`).

---

## Part V — Model Format & Loading (NEFF / KBIN)

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Model-file journey overview | `libnrt.so` | `kelf/neff.cpp` + `kelf/kelf.cpp` | `neff/overview.md` | HIGH |
| NEFF container (gzip-tar) | `libnrt.so` (+ vendored libarchive/zlib) | `kelf/neff.cpp` + `libarchive` + `zlib` | `neff/container.md` | HIGH |
| NEFF metadata schema (TVM-Relay JSON) | `libnrt.so` (+ vendored simdjson) | `kmgr/neff_json_parser.cpp` + `simdjson.cpp` | `neff/metadata-schema.md` | HIGH |
| NEFF section taxonomy | `libnrt.so` | `kelf/kelf.cpp` | `neff/section-taxonomy.md` | MEDIUM |
| dtype system | `libnrt.so` | `kelf/load_numpy.c` + `kelf/kelf.cpp` | `neff/dtype-system.md` | MEDIUM |
| kelf2kbin: JSON → KBIN lowering | `libnrt.so` | `kelf/kelf2kbin.cpp` | `neff/kelf2kbin.md` | HIGH |
| KBIN structures | `libnrt.so` | `tdrv/kbin.c` + `tdrv/kbin_patch.c` | `neff/kbin-structs.md` | HIGH |
| Static memory planning (mem_ref) | `libnrt.so` | `tdrv/mem_ref.c` | `neff/memory-planning.md` | HIGH |
| Compute-resource build (KBL) | `libnrt.so` | `tdrv/tdrv.c` + `kmgr/dlr.cpp` | `neff/compute-resource-build.md` | MEDIUM |
| Load pipeline (parse → build → stage → relocate) | `libnrt.so` | `kmgr/dlr.cpp` + `kmgr/dlr_kelf.cpp` | `neff/load-pipeline.md` | HIGH |

---

## Part VI — TPB Instruction Set: Encoding & Validation

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| TPB engine instruction model overview | `libnrt.so` | `tdrv/instruction_block.c` + `tdrv/instr_common.c` | `isa/overview.md` | HIGH |
| Pseudo-instruction lowering (SP/TopSP) | `libnrt.so` | `tdrv/instr_pseudo_branching.c` + `tdrv/instr_sunda_pseudo_range_check.c` | `isa/pseudo-instruction-lowering.md` | MEDIUM |
| 64-byte instruction record format | `libnrt.so` | `tdrv/instruction_block_common.c` | `isa/instruction-record.md` | HIGH |
| FP8 / dtype encoding | `libnrt.so` | `tdrv/instr_tpb_functions.c` | `isa/fp8-dtype-encoding.md` | MEDIUM |
| ISA validator architecture / entry tree | `libnrt.so` | `tdrv/instruction_block_mariana.c` (297 fns) | `isa/validator-architecture.md` | HIGH |
| Per-arch validators (SUNDA/CAYMAN/MARIANA deltas) | `libnrt.so` | `tdrv/instruction_block_{cayman,sunda,mariana}.c` | `isa/validators-per-arch.md` | HIGH |

---

## Part VII — Execution Engine

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Hot inference path overview | `libnrt.so` | `tdrv/exec.c` + `kmgr/kmgr_async_exec.cc` | `exec/overview.md` | HIGH |
| Exec manager (KMGR) facade / model DB | `libnrt.so` | `kmgr/dlr.cpp` + `kmgr/kmgr_metrics.cpp` | `exec/kmgr-facade.md` | HIGH |
| XU work-queue / worker threads | `libnrt.so` | `kmgr/xu/queue.c` + `kmgr/xu/xu_worker.cc` + `kmgr/xu/tpb_xu.cc` | `exec/xu-workers.md` | HIGH |
| Submit path (bind → stage → doorbell) | `libnrt.so` | `tdrv/hw_exec_queue.c` + `tdrv/exec.c` | `exec/submit-path.md` | HIGH |
| Completion engine (NQ harvest / error decode) | `libnrt.so` | `tdrv/notification.c` + `tdrv/infer_error_subtype.c` | `exec/completion-engine.md` | MEDIUM |
| `xu_queue` / `device_exec_request` ABI | `libnrt.so` | `kmgr/xu/queue.c` + `kmgr/xu/comp_handle_pool.c` | `exec/xu-queue-abi.md` | MEDIUM |

---

## Part VIII — DMA & Descriptor Engine

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| DMA overview | `libnrt.so` | `tdrv/dma_ring.c` + `tdrv/dma_memory.c` | `dma/overview.md` | HIGH |
| 16-byte descriptor wire format | `libnrt.so` | `tdrv/dma_desc_util.c` | `dma/descriptor-format.md` | HIGH |
| SDMA / CCE / TDG meta-control overlays | `libnrt.so` | `tdrv/dma_ring_act_tbl.c` + `tdrv/dma_ring_imcpy.c` | `dma/meta-ctrl-overlays.md` | MEDIUM |
| Ring / trigger / doorbell / completion cycle | `libnrt.so` | `tdrv/dma_ring.c` + `tdrv/dma_ring_setup.c` | `dma/ring-cycle.md` | HIGH |
| Virtual rings (vring) / packet builders | `libnrt.so` | `tdrv/vring.c` + `tdrv/sw_dma_queue.c` | `dma/virtual-rings.md` | HIGH |
| IOFIC interrupt model (userspace view) | `libnrt.so` | `tdrv/hal_platform.c` | `dma/iofic.md` | MEDIUM |

---

## Part IX — On-Device Collectives (enc / encd / cc_op)

The collective compute engine is **statically embedded in `libnrt.so`** (`enc/` = 52 CUs / 1,062 fns; `tdrv/encd/` builders). The multi-node transport (`libnccom.so`) is Part XII.

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Collective-compute architecture overview | `libnrt.so` | `enc/enc.cc` (352 fns) | `collectives/overview.md` | HIGH |
| Engine core (enc_context / accessors) | `libnrt.so` | `enc/enc.cc` | `collectives/engine-core.md` | HIGH |
| Comm context / bootstrap | `libnrt.so` | `enc/neuron_nccl.cc` + `enc/async_sr/kv_store.cc` | `collectives/comm-context.md` | MEDIUM |
| libnrt ↔ libnccom boundary (`nec_`/`nccl`) | `libnrt.so` ↔ `libnccom.so` | `enc/neuron_nccl.cc` + `tdrv/encd/nec_vil.c` | `collectives/nccl-boundary.md` | HIGH |
| Algorithm taxonomy (ring/mesh/hier/kangaring/RDH) | `libnrt.so` | `enc/enc_primitive.cc` (200 fns) + `enc/rdh.cc` | `collectives/algorithm-taxonomy.md` | HIGH |
| Mesh composer (`alg_mesh_initializer`) | `libnrt.so` | `enc/pod_mesh_primitive.cc` | `collectives/mesh-composer.md` | MEDIUM |
| Ring scheduling math | `libnrt.so` | `enc/enc_primitive.cc` | `collectives/ring-scheduling.md` | MEDIUM |
| Hierarchical / RDH composition | `libnrt.so` | `enc/rdh.cc` + `enc/inter_rdh.cc` | `collectives/hierarchical-rdh.md` | HIGH |
| enc primitives (send/recv leaves) | `libnrt.so` | `enc/enc_primitive.cc` + `enc/memory_segment.cc` | `collectives/enc-primitives.md` | HIGH |
| Switch-platform broadcast / barrier tables | `libnrt.so` | `enc/switch_platform/enc_switch_platform.cc` + `enc/barrier.cc` | `collectives/switch-broadcast-barrier.md` | MEDIUM |
| Topology partitioning (union-find) | `libnrt.so` | `enc/replica_groups.cc` | `collectives/topology-partition.md` | MEDIUM |
| Bananaphone IPC / proxy driver | `libnrt.so` | `enc/bananaphone.c` + `enc/proxy_queue.cc` | `collectives/proxy-driver.md` | HIGH |
| Async send/recv (point-to-point) | `libnrt.so` | `enc/async_sr/async_sr.cc` + `async_sr/async_sr_ofi.cc` | `collectives/send-recv.md` | HIGH |
| cc_op_entry on-device ISA | `libnrt.so` | `tdrv/instr_collectives.c` + `tdrv/encd.c` | `collectives/cc-op-isa.md` | MEDIUM |
| 148-byte ring channel descriptor | `libnrt.so` | `tdrv/encd.c` | `collectives/channel-descriptor.md` | MEDIUM |
| encd: device-side descriptor emitter | `libnrt.so` | `tdrv/encd.c` (229 fns) | `collectives/encd-overview.md` | HIGH |
| encd: DMA descriptor / devmem floor | `libnrt.so` | `tdrv/encd.c` + `tdrv/encd/arch_device_mapping.c` | `collectives/encd-dma-devmem.md` | MEDIUM |
| encd: semaphore / TopSP bring-up | `libnrt.so` | `tdrv/encd.c` + `tdrv/instr_collectives.c` | `collectives/encd-sema-topsp.md` | MEDIUM |
| encd: per-arch ops dispatch | `libnrt.so` | `tdrv/encd/archs/{arch,cayman,mariana,sunda}.c` | `collectives/encd-arch-ops.md` | HIGH |

> **GOTCHA —** the per-event switch-platform collective TUs (`enc/switch_platform/events/*` and `ops/*`, ~27 small `.cc` files) are template-instantiated and `-O2`-inlined: `addr2line` on their `low_pc`s resolves to STL header frames, so the ENC-ALG coverage cells folded them into `enc.cc`/`enc_primitive.cc` *by address* without naming the event TU. `collectives/switch-broadcast-barrier.md` and `collectives/algorithm-taxonomy.md` are the owning pages, but the per-event source filenames (e.g. `intra_chip_broadcast_event.cc`, `proxy_buff_supported_local_reduce_event.cc`) are a known source-map gap — see the CORRECTION below.

---

## Part X — Collectives Firmware (libncfw)

`libncfw.so` has **NO DWARF**; TU column is structural (embedded-blob layout, recovered by binary analysis), hence LOW/MEDIUM confidence.

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Firmware overview | `libncfw.so` | (no DWARF) | `firmware/overview.md` | MEDIUM |
| Carrier library | `libncfw.so` | (carrier glue, no DWARF) | `firmware/carrier-library.md` | MEDIUM |
| Embedded payloads (8 Xtensa blobs) | `libncfw.so` | (8 `.rodata` blobs) | `firmware/embedded-payloads.md` | MEDIUM |
| Serializer families (per-arch CC dumpers) | `libncfw.so` | (per-arch serializers, no DWARF) | `firmware/serializer-families.md` | LOW |
| NCFW sequencer (Xtensa LX disasm) | `libncfw.so` | (Xtensa firmware image) | `firmware/ncfw-sequencer.md` | LOW |
| Firmware upload path (DKMS → device DRAM) | `libncfw.so` + `.ko` | `tdrv/ucode_lib.c` (carrier side) + kernel `fw_io.c` | `firmware/upload-path.md` | MEDIUM |

---

## Part XI — GPSIMD / Q7 Microcode & ISA

`libnrtucode_extisa.so` has **NO DWARF**; the `libnrt.so`-side facade (`ucode.c`) does.

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Q7 GPSIMD engine overview | `libnrtucode_extisa.so` | (no DWARF) | `gpsimd/overview.md` | MEDIUM |
| Xtensa / Vision-Q7 identification | `libnrtucode_extisa.so` | (ISA image) | `gpsimd/xtensa-vision-q7.md` | MEDIUM |
| Xtensa toolchain / TIE config | `libnrtucode_extisa.so` | (TIE config) | `gpsimd/xtensa-toolchain.md` | LOW |
| ext-ISA provider (`nrtucode_*` API) | `libnrtucode_extisa.so` | `nrtucode_*` exports (no DWARF) | `gpsimd/extisa-provider.md` | MEDIUM |
| ext-ISA dispatch tables / arch-id scheme | `libnrtucode_extisa.so` | (dispatch tables) | `gpsimd/dispatch-tables.md` | MEDIUM |
| Microcode loader (RELA + FLIX, UCPL) | `libnrt.so` + ext-ISA | `tdrv/ucode_lib.c` + `ucode/ucode.c` | `gpsimd/microcode-loader.md` | MEDIUM |
| 13 Q7 microcode blobs | `libnrtucode_extisa.so` | (13 `.rodata` blobs) | `gpsimd/q7-blobs.md` | MEDIUM |
| IVP vector-ISA catalog (1065 ops) | `libnrtucode_extisa.so` | (IVP op tables) | `gpsimd/ivp-isa-catalog.md` | MEDIUM |
| `ucode.c` dlopen facade | `libnrt.so` | `ucode/ucode.c` + `nrt/nrt_ucode.cpp` | `gpsimd/ucode-facade.md` | HIGH |

---

## Part XII — Multi-Node Collectives: libnccom (NCCL Fork)

The transport lib + its `libnrt.so`-side bridge. `libnccom.so` is dlopen'd; the bridge trampolines (`ncclX` → `neuronX` dlsym pointers) live in `libnrt.so`.

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| NCCL-fork overview (2.31.24+nrt2.0) | `libnccom.so` | (separate pkg) | `nccom/overview.md` | MEDIUM |
| Communicator init / bootstrap | `libnccom.so` | (separate pkg) | `nccom/comm-init.md` | MEDIUM |
| Topology detection / graph build | `libnccom.so` | (separate pkg) | `nccom/topology.md` | MEDIUM |
| Ring algorithm construction (host-side) | `libnccom.so` | (separate pkg) | `nccom/algorithm-ring.md` | MEDIUM |
| Tree / CollNet (dead code) | `libnccom.so` | (separate pkg) | `nccom/algorithm-tree.md` | LOW |
| Send/recv primitives / protocols | `libnccom.so` | (separate pkg) | `nccom/send-recv-prims.md` | MEDIUM |
| Proxy / progress engine (+ libnrt bridge) | `libnccom.so` ↔ `libnrt.so` | `enc/proxy_queue.cc` (bridge side) | `nccom/proxy-engine.md` | MEDIUM |
| Tuning model (dead cost model) | `libnccom.so` | (separate pkg) | `nccom/tuning.md` | LOW |
| Intra-node transport (P2P) | `libnccom.so` | (separate pkg) | `nccom/transport-intra.md` | MEDIUM |
| Inter-node transport (EFA / libfabric) | `libnccom-net.so` | (aws-ofi-nccl plugin) | `nccom/transport-efa.md` | MEDIUM |
| Net plugin (`ncclNet_v6`) | `libnccom-net.so` | (separate pkg) | `nccom/net-plugin.md` | MEDIUM |
| libnrt ↔ libnccom ABI | `libnrt.so` ↔ `libnccom.so` | `enc/neuron_nccl.cc` (37 `neuron*` imports / 16 `nec_*` exports) | `nccom/abi.md` | HIGH |

> **NOTE —** every `libnccom.so`/`libnccom-net.so` TU cell is `(separate pkg)`: those binaries ship in `aws-neuronx-collectives`, absent from the runtime-lib extract, so their internal TUs are unverified (Confidence ≤ MEDIUM). The one HIGH row is `nccom/abi.md` — the *boundary* is fully observable from `libnrt.so`'s import/dlsym table and the 16 exported `nec_*` reverse-callback thunks.

---

## Part XIII — Profiling, Trace & Telemetry

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Three trace producers overview | `libnrt.so` | `nrt/nrt_profile.cpp` (94 fns) | `trace/overview.md` | HIGH |
| Inspect / profile API | `libnrt.so` | `nrt/nrt_inspect.cpp` + `nrt/nrt_inspect_config.cpp` | `trace/inspect-profile-api.md` | HIGH |
| System monitor / debug stream | `libnrt.so` | `nrt/nrt_system_monitor.cpp` + `nrt/nrt_debug_stream.cpp` + `ndebug/ndebug_stream.c` | `trace/system-monitor.md` | HIGH |
| NTFF trace file format (`ntff.proto`) | `libnrt.so` (vendored protobuf) | `ntff.pb.cc` (535 fns, `KaenaProfilerFormat`) | `trace/ntff-format.md` | HIGH |
| NTFF wire tables (TcParseTable decode) | `libnrt.so` | `ntff.pb.cc` + `neuron_trace.pb.cc` | `trace/ntff-wire-tables.md` | MEDIUM |
| SysTraceEventType taxonomy (46 variants) | `libnrt.so` | `nrt/nrt_sys_trace.cpp` + `nrt/nrt_sys_trace_api.cpp` | `trace/event-taxonomy.md` | HIGH |
| neuron_rustime: sys_trace capture engine | `libnrt.so` (first-party Rust) | `neuron_rustime` cgu (Rust std/core) | `trace/rust-capture.md` | MEDIUM |
| neuron_rustime: serde_json serializer | `libnrt.so` (Rust) | `neuron_rustime` + `serde_json-1.0.145` | `trace/rust-serde.md` | MEDIUM |
| neuron_rustime: cbindgen FFI boundary | `libnrt.so` (Rust) | `nrt/nrt_sys_trace_capture.cpp` (C side) + Rust cgu | `trace/rust-ffi.md` | MEDIUM |
| Telemetry / metrics / error reporting | `libnrt.so` | `nrt/nrt_profile.cpp` + `kmgr/kmgr_metrics.cpp` | `trace/telemetry-errors.md` | MEDIUM |

> **NOTE —** the trace producers split across origins inside one binary: the C API + protobuf serializer is first-party C++/`KaenaProfilerFormat` (`nrt/nrt_*`, `ntff.pb.cc`), while the capture/serialize engine is the **first-party** AWS `neuron_rustime` Rust crate (NOT vendored — it has no `index.crates.io` path). The Rust rows are MEDIUM because the cgu-merged Rust CUs do not expose per-function source TUs the way the C++ CUs do.

---

## Part XIV — The Neuron DataStore (NDS)

The datastore straddles two binaries: per-process slabs in the `.ko`, the userspace accessor lib `libnds.a` (== the `nds/` first-party CUs, statically linked into `libnrt.so`).

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Shared-memory counter-plane overview | `libnds.a` (in `libnrt.so`) + `.ko` | `nds/neuron_ds.c` | `datastore/overview.md` | HIGH |
| Kernel side (per-process slabs) | `.ko` | kernel `neuron_ds.c` | `datastore/kernel-side.md` | HIGH |
| Userspace side (`libnds.a`) | `libnds.a` (in `libnrt.so`) | `nds/neuron_ds.c` + `nds/neuron_ds_counters.c` + `nds/neuron_ds_object.c` + `nds/neuron_ds_models.c` + `nds/neuron_ds_process.c` + `nds/neuron_ds_helpers.c` | `datastore/userspace-libnds.md` | HIGH |
| NDS wire format | `libnds.a` / `.ko` | `nds/neuron_ds_object.c` + `nds/neuron_ds_models.c` | `datastore/wire-format.md` | MEDIUM |

> **QUIRK —** `libnds.a` is the only one of the six listed binaries that is **not** an independent runtime artifact: its 6 `neuron_ds*.c.o` members are compiled once and folded into `libnrt.so`, so its functions appear in `libnrt.so`'s DWARF under `nds/`. It is catalogued separately because the *same* source is what the build links, and because the counter plane it implements is shared with the `.ko`'s per-process slabs.

---

## Part XV — Security & Attack Surface (White-Hat)

These pages are cross-cutting analyses, not new code; the binary/TU columns point at the subsystems they audit.

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| Privilege-gate model overview | `.ko` | `neuron_ioctl.c` (audit target) | `security/overview.md` | MEDIUM |
| IOCTL attack surface (14 findings) | `.ko` | `neuron_ioctl.c` + handler TUs | `security/ioctl-attack-surface.md` | MEDIUM |
| FW-IO trust boundary | `.ko` + `libncfw.so` | `fw_io.c` (audit target) | `security/fw-io-trust.md` | MEDIUM |
| Hardening recommendations | all | (cross-cutting) | `security/hardening.md` | MEDIUM |

---

## Part XVI — Appendices

| Subsystem | Binary | Source TU(s) | Wiki page | Conf |
|---|---|---|---|---|
| This matrix | all five + `libnds.a` | all CUs / boundaries | `appendix/subsystem-matrix.md` | HIGH |
| Symbol-version manifest (`.gnu.version_d`) | `libnrt.so` | (`NRT_2.0.0` / `NRT_3.0.0` nodes) | `appendix/symbol-versions.md` | HIGH |
| Global / singleton address index | `libnrt.so` | cross-TU `.bss`/`.data` | `appendix/globals-index.md` | HIGH |
| Known-bugs / anomalies catalog | all | (cross-cutting) | `appendix/known-bugs.md` | MEDIUM |
| Phase-3 deep-dive backlog | all | (open items) | `appendix/deep-dive-backlog.md` | HIGH |

---

## Dual-Organization — subsystem Part vs binary lane

The book files the same physical code under two complementary schemes; a subsystem reached via one will often have a deeper or shallower face under the other. The straddles a reader should know:

| Subsystem | Subsystem-Part face | Binary-lane / other face | Why it splits |
|---|---|---|---|
| **DataStore** | Part XIV `datastore/*` (deep: wire format, slabs, libnds) | Part III `kernel/datastore.md` (map: the `.ko`'s per-process slabs) | The counter plane is shared kernel↔userspace; `libnds.a` is in `libnrt.so`, slabs are in the `.ko`. |
| **On-device collectives** | Part IX `collectives/*` (the `enc/`+`encd/` compute engine, in `libnrt.so`) | Part XII `nccom/*` (the `libnccom.so` transport) | Compute is statically embedded in `libnrt.so`; transport is the dlopen'd `libnccom.so`. The boundary is `collectives/nccl-boundary.md` ↔ `nccom/abi.md`. |
| **KaenaHal / DHAL** | Part IV `runtime/hal-*.md` (the `libnrt.so` adapter shims) | Part III `kernel/dhal-*.md` (the `.ko` ndhal vtable-of-vtables) | `KaenaHal` has no CU in `libnrt.so` — the real DHAL is kernel-side; userspace only carries adapter shims. |
| **DMA engine** | Part VIII `dma/*` (userspace ring/descriptor view, `libnrt.so`) | Part III `kernel/dma-*.md`, `kernel/udma-*.md` (the `.ko` op layer + UDMA core) | The descriptor wire format is shared; the userspace `tdrv/dma_*` builds it, the kernel `neuron_dma.c`/`udma.c` executes it. |
| **NDL ↔ IOCTL** | Part IV `runtime/ndl.md` (vendored `KaenaDriverLib` userspace shim, in `libnrt.so`) | Part III `kernel/ioctl-*.md` (the `.ko` IOCTL plane it calls) | `ndl.c` issues the IOCTLs that `neuron_ioctl.c` services — same package (`KaenaDriverLib`/`KaenaDriver` 2.27.4.0), two sides. |
| **Microcode** | Part XI `gpsimd/microcode-loader.md`, `ucode-facade.md` (loader, in `libnrt.so`) | Part XI `gpsimd/q7-blobs.md`, `ivp-isa-catalog.md` (the `libnrtucode_extisa.so` payload) | The loader `ucode.c` is in `libnrt.so`; the ext-ISA provider and blobs are the no-DWARF `libnrtucode_extisa.so`. |
| **Firmware** | Part X `firmware/*` (the `libncfw.so` carrier + blobs) | Part X `firmware/upload-path.md` (the `.ko` `fw_io.c` upload side) | The carrier holds the images; the `.ko` DMAs them to device DRAM. |

> **CORRECTION (F-SRCMAP §4b) —** the per-event switch-platform collective TUs (`enc/switch_platform/events/*.cc`, e.g. `intra_chip_broadcast_event.cc`, `proxy_buff_supported_local_reduce_event.cc`, `intra_rank_dimr1w_event.cc`, and ~13 peers) are a genuine source-map gap: they have real DWARF compile units but, being template-instantiated and `-O2`-inlined, no coverage cell names them — they are folded by address into `enc.cc`/`enc_primitive.cc`. Their owning pages are `collectives/switch-broadcast-barrier.md` and `collectives/algorithm-taxonomy.md`, but the *source filename* → page link for these specific TUs is asserted by directory membership, not by an address-grounded cite. Treat those event-TU rows (covered collectively under the Part IX switch-platform entries) as the lowest-confidence first-party mappings in this matrix.

## Cross-References

- [Symbol-Version Manifest](symbol-versions.md) — the `NRT_2.0.0` / `NRT_3.0.0` `.gnu.version_d` nodes and the dlopen'd `libnccom` boundary that the collectives rows reference
- [Source-Tree Reconstruction (KaenaRuntime)](../front/source-tree.md) — the full first-party TU inventory (203 CUs / 4,407 fns) that the source-TU column draws from
- [ELF Anatomy](../forensics/elf-anatomy.md) — the `DT_NEEDED` list, DWARF presence, and section table that ground the binary legend
- [Vendored-Library SBOM](../forensics/vendored-sbom.md) — the version pins for the 122 vendored CUs (Abseil, protobuf, Rust, libarchive, zlib, simdjson) that appear as `(+ vendored …)` in the source-TU cells
