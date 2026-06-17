# Userspace Runtime Core — Internal Architecture Map

> *All addresses, offsets, and symbol names on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (real file `libnrt.so.2.31.24.0`, SONAME `libnrt.so.1`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, git `0b044f4ce`). The ELF is **122,956,336 bytes**, ELF64 x86-64, **not stripped**, and carries **full DWARF** (`.debug_info` present; `DW_AT_comp_dir = /opt/workspace/KaenaRuntime/build/private/develop/almalinux/RelWithDebInfo`, `DW_AT_producer = GNU C++17 14.2.1 -O2 -fPIC`). `.text` and `.rodata` are VMA==fileoffset; `.data`/`.bss` are not (`.data` delta `0x400000`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — the layer-to-TU map is read from the binary's own 331-CU DWARF compile-unit table (`P1-F-SRCMAP`); every layer pins to a verbatim `DW_AT_name` source-TU path and at least one nm-verified symbol. · Part IV — Userspace Runtime Core · [back to index](../index.md)*

## Abstract

`libnrt.so` is the **userspace Neuron Runtime**: the single shared object an ML framework (PyTorch-Neuron, JAX, the `aws-neuronx-collectives` stack) links against to load a compiled model (a NEFF) and run inference on Trainium/Inferentia silicon. It is **one source tree** — `/opt/workspace/KaenaRuntime/` — compiled to **4,407 first-party functions** across **203 translation units**, with another **4,384 functions** of statically-linked vendored code (Abseil, the Rust `neuron_rustime` runtime, the KaenaProfilerFormat protobuf, Libarchive+zlib, Simdjson, and the `KaenaDriverLib` shim). DWARF exposes the whole thing: there are no opaque blobs in the call path, only inlined headers. This page is the **map** of that tree — it names each architectural layer, pins it to the owning TU(s) and a representative symbol, shows how a runtime call descends the stack, and routes the reader to the ~26 sibling pages that derive each layer byte-for-byte.

The familiar reference frame is a **classic layered userspace device runtime**, the shape of a CUDA userspace driver or a vendor OpenCL ICD: a stable public C ABI at the top, a thin kernel-boundary syscall shim at the bottom, and — in between — an execution manager that owns the model database and the work queue, a device-driver core that owns device memory and the instruction-block builders, a vendored hardware-abstraction layer for register access, and a per-silicon device layer that specializes geometry and ISA for each chip generation. The runtime never issues a raw `ioctl` from anywhere except the bottom layer; every device transaction funnels through the `ndl_*` boundary into `/dev/neuron<N>`. That single funnel is the invariant a reimplementer must preserve.

The page is organized top-down. After the reimplementation contract and the at-a-glance table, **§1** gives the six-layer stack diagram with the owning TU and symbol prefix for each layer; **§2** walks one representative call (`nrt_execute`) down all six layers to show where each boundary is crossed; **§3** is the layer → TU → owning-sub-page routing table that orients the rest of Part IV; and **§4** is the cross-cutting infrastructure sidebar (logging, the device book, interned strings, virtual rings) that every layer leans on. Each layer's internal algorithm, struct layout, and decision logic live on a **sibling page** — this page links them and does not duplicate their derivations.

For reimplementation, the contract of the runtime core is:

- **The six-layer descent** — `nrt_*` public API → KMGR exec manager → TDRV device-driver core → KaenaHal register/platform adapter → NDL kernel-boundary shim → kernel `ioctl`. Each layer calls only the layer directly below it (plus the cross-cutting infra of §4); a reimplementation that lets, e.g., TDRV `ioctl` the kernel directly breaks the compatibility-mode fallback the NDL layer implements.
- **The single kernel boundary** — every device syscall (`open`/`ioctl`/`mmap` on `/dev/neuron<N>`, magic byte `'N'`) is emitted exclusively from `ndl.c`; the layers above hold only opaque handles (`H_NN`, `dmem_t*`, mem-handles) and never a raw fd except the one cached inside a mem-handle.
- **The per-arch fan-out** — three silicon generations (codenames **sunda**, **cayman**, **mariana**) each get their own `tdrv_arch_*.c`, `instruction_block_*.c`, and `dve_dynamic_config_*.c`; a one-shot arch-ops vtable (`tdrv_arch_ops @0xc97180`) selects the family at bring-up.
- **The TU ownership map** — which source TU implements which layer, so a reimplementer can mirror the module boundaries rather than reinvent them.

| | |
|---|---|
| **Source root** | `/opt/workspace/KaenaRuntime/` — 203 first-party TUs, 4,407 fns (DWARF CU table) |
| **Public entry (init)** | `nrt_init` `@0x94e90` (`nrt/nrt_init.cpp`); master bring-up |
| **Public entry (run)** | `nrt_execute` `@0x91de0` (`nrt/nrt_exec.cpp`) |
| **Exec manager** | `kmgr_init` `@0xde080`, `kmgr_load_nn_nc` `@0xde280` (`kmgr/dlr.cpp`) |
| **Device-driver core** | `tdrv_init` `@0x26a310` (`tdrv/init.c`); 84 TUs / 2,413 fns |
| **Arch-ops vtable** | `tdrv_arch_ops` `@0xc97180` (.bss, 488 B / 47 slots) |
| **HAL adapter** | `al_reg_read32` `@0x2658a0` (`tdrv/hal_platform.c`); backs vendored `KaenaHal-2.31.0.0` |
| **Kernel-boundary shim** | `ndl_device_ioctl`, `ndl_misc_ioctl` (`KaenaDriverLib-2.27.4.0/src/ndl.c`, 123 fns) |
| **Kernel char device** | `/dev/neuron%d` (0..63), ioctl magic `'N'` (0x4E) |

---

## 1. The Six-Layer Stack

The runtime core is six layers deep from the framework-visible ABI to the kernel syscall. Each layer is owned by a distinct family of TUs under `/opt/workspace/KaenaRuntime/` (the bottom two are statically-linked vendored TUs). The diagram below pins each layer to its owning TU(s) and the symbol prefix that names its functions; the per-layer derivation lives on the sibling page named in §3.

```text
 framework (PyTorch-Neuron / JAX / aws-neuronx-collectives)
        │  links against the @@NRT_2.0.0 versioned ABI
        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ [1] PUBLIC C API          nrt_*        nrt/*.cpp  (27 TU / 377 fn)            │
 │     init/load/exec/tensor/profile      nrt_init@0x94e90  nrt_execute@0x91de0  │
 │     • per-thread nlog tag • 4-state NRT_INIT_STATE guard • delegate, flush    │
 └─────────────────────────────────────────────────────────────────────────────┘
        │  nrt_execute → kmgr_*   ·   nrt_load → kmgr_load_nn_nc
        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ [2] EXEC MANAGER (KMGR)   kmgr_/dlr_   kmgr/dlr.cpp + kmgr/xu/* (10 TU/117 fn)│
 │     model DB (dlr_model_set) • sync XU work-queue • async worker engine       │
 │     kmgr_init@0xde080  kmgr_load_nn_nc@0xde280  dlr_kickoff_exec               │
 └─────────────────────────────────────────────────────────────────────────────┘
        │  schedule → build descriptors → ring doorbell
        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ [3] DEVICE-DRIVER CORE    tdrv_/dmem_/ tdrv/*.c  (84 TU / 2413 fn)            │
 │     hw_exec_queue/tensor   device bring-up • dmem allocator • instruction     │
 │     tdrv_init@0x26a310     blocks • DMA rings • per-arch arch_ops vtable       │
 │     arch_ops@0xc97180      tensor objects • hw_exec_queue • sync-point         │
 └─────────────────────────────────────────────────────────────────────────────┘
        │  al_reg_read32/write32 · al_mem_read/write_buf · al_copy_descriptor
        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ [4] KaenaHal ADAPTER      al_*/aws_hal_ tdrv/hal_platform.c + helper.c        │
 │     register/mem/barrier   al_reg_read32@0x2658a0 → csr.c (csr_read@0x315680) │
 │     ── backs vendored KaenaHal-2.31.0.0 (al_udma_/al_iofic_/aws_hal_*)        │
 └─────────────────────────────────────────────────────────────────────────────┘
        │  ndl_memory_alloc · ndl_memory_map · ndl_memory_copy_as · ndl_*_ioctl
        ▼
 ┌─────────────────────────────────────────────────────────────────────────────┐
 │ [5] NDL kernel shim       ndl_*        KaenaDriverLib-2.27.4.0/src/ndl.c (123)│
 │     IOCTL/mmap wrappers    ndl_device_ioctl (per-dev fd) · ndl_misc_ioctl     │
 │     + compatibility-mode (_cm) fallback stubs for missing driver features     │
 └─────────────────────────────────────────────────────────────────────────────┘
        │  open()/ioctl(magic 'N')/mmap() on /dev/neuron<N>
        ▼
 ╔═════════════════════════════════════════════════════════════════════════════╗
 ║ [K] KERNEL DRIVER (DKMS, out of this binary) — Part III · ncdev_ioctl dispatch║
 ╚═════════════════════════════════════════════════════════════════════════════╝

 cross-cutting (called from every layer — §4):
   nlog/  (logging+backtrace)   ·   tdrv/db.c+dbtc.c  (device book)
   nrt/nrt_interned_string_db.cpp (interned strings)  ·  tdrv/vring.c (virtual rings)
```

> **NOTE —** layers [4] and [5] are **vendored TUs statically linked in**, not first-party KaenaRuntime code, but the boundary they form is real and load-anchored. NDL's source package (`KaenaDriverLib-2.27.4.0`) **lags** the runtime package (`2.31.24.0`); this version skew is a confirmed DWARF fact, not an extraction error. The KaenaHal package (`KaenaHal-2.31.0.0`) has **no DWARF CU of its own in `libnrt.so`** — its `al_udma_*`/`aws_hal_*` code reaches the binary only through inlined headers; the `al_*` *adapter* that backs it (`tdrv/hal_platform.c`) **is** first-party and is what layer [4] documents.

### Why the layering is shaped this way

The split is not cosmetic; each boundary earns its place.

- **API ↔ KMGR** separates the *idempotent, fork-aware, state-guarded* public surface (every `nrt_*` entry runs the same `NRT_INIT_STATE` preamble and a tail log-flush — see [api-lifecycle](api-lifecycle.md)) from the *model-database orchestration* that does not care about C-ABI concerns. KMGR keys everything on `dlr_model_set` (an `unordered_map<u32 H_NN.id, dlr_model*>` under an rwlock), so the API layer can hand out opaque `H_NN` handles and never touch model internals.
- **KMGR ↔ TDRV** separates *which model to run and when* from *how the device is driven*. KMGR builds and rings; TDRV owns every device resource (the `dmem` allocator, the per-arch instruction-block builders that are the single densest code mass at 783 combined functions, the DMA rings, the tensor objects).
- **TDRV ↔ KaenaHal** is the *Alpine porting seam*: TDRV calls the Annapurna/Alpine `al_*` register and memory primitives; the adapter (`hal_platform.c`) implements those primitives over `csr.c` (PCIe CSR access) so the vendored `al_udma`/`al_iofic`/`aws_hal` code compiles unchanged.
- **KaenaHal ↔ NDL ↔ kernel** is the *syscall funnel*. NDL is the only TU that opens `/dev/neuron<N>` and issues `ioctl`; it also implements the **compatibility-mode fallback** (the `_cm` stub set) so that a runtime built against a newer ABI degrades gracefully on an older driver. Letting any higher layer syscall directly would defeat that fallback.

---

## 2. How a Call Descends the Stack

The clearest way to read the stack is to follow one call. Below is `nrt_execute` — the hot inference path — from the framework's call to the kernel doorbell. Each `→` crosses one layer boundary; the symbol that does the crossing is named. (The full per-stage logic is owned by [exec/kmgr-facade](../exec/kmgr-facade.md) and [exec/submit-path](../exec/submit-path.md); this is the descent skeleton only.)

```c
// [1] PUBLIC API — nrt/nrt_exec.cpp
nrt_execute(model, in_tensors, out_tensors):     // @0x91de0
    nlog_coalescing_init_thread()                //  per-thread log tag
    if NRT_INIT_STATE != INIT: return guard_status   //  4-state guard (api-lifecycle.md)
    model_id = nrt_get_interned_model_id(...)    //  @0x96fa0 → interned-string DB (§4)
    rc = kmgr_sync_exec(model->h_nn, io, ...)    //  → [2] cross into exec manager
    nlog_flush_if_unflushed("Generic API Failure"); return rc

// [2] EXEC MANAGER — kmgr/dlr.cpp + kmgr/xu/*
kmgr_sync_exec(h_nn, io):                        //  resolve dlr_model from dlr_model_set (rwlock + refcount)
    req = tpb_xu_schedule_request(model, io)     //  kmgr/xu/tpb_xu.cc — build a work-queue request
    dlr_add_to_hw_exec_queue(req)                //  → [3] the shared "build + ring" funnel
    dlr_kickoff_exec(req)                         //  → [3]
    kmgr_exec_wait(req)                           //  eventfd-block on completion (NQ harvest)

// [3] DEVICE-DRIVER CORE — tdrv/hw_exec_queue.c + tdrv/dma_memory.c + tdrv/tensor.c
hw_exec_queue_add_exec_request(req):             //  bind tensors, stage inputs
    dmem_buf_batch_copyin(io.inputs)             //  tdrv/dma_memory.c @0x228eb0 — H2D staging
        → dmem_batch_transfer_common(...)        //  build neuron_memcpy_batch_t[]
            → ndl_memory_buf_batch_copy(...)     //  → [5]  (one batched copy ioctl)
    instruction_block_<arch>_build(...)          //  per-arch IB emitter (sunda/cayman/mariana)
    csr_write(doorbell_reg, ring_tail)           //  → [4]  ring the hardware doorbell

// [4] KaenaHal ADAPTER — tdrv/hal_platform.c → tdrv/csr.c
al_reg_write32(reg, val):                        //  @ hal_platform.c — porting-shim primitive
    csr_write(bar, off, val)                     //  tdrv/csr.c @0x315820 — PCIe CSR write
        → (mmap'd BAR store; mapping established earlier via ndl_memory_map → [5])

// [5] NDL KERNEL SHIM — KaenaDriverLib-2.27.4.0/src/ndl.c
ndl_memory_buf_batch_copy(device, batches, n, dir):
    fd = device->context.nd_fd                   //  cached per-device fd (struct +632)
    ioctl(fd, NEURON_IOC_<copy>, &arg)           //  → [K]  magic 'N' (0x4E); -errno on fail
                                                 //  or, if driver lacks the ioctl: *_cm fallback stub
```

Three facts a reimplementer must lift from this descent:

- **The kernel boundary is reached only at [5].** The H2D copy in [3] does not `ioctl`; it builds a `neuron_memcpy_batch_t[]` array and hands it to one `ndl_memory_buf_batch_copy` — a *single* batched syscall, not one per tensor. The doorbell ring in [4] is a store to an already-`mmap`'d BAR, and that mapping was itself established earlier by `ndl_memory_map` at [5].
- **Layer [4] is a porting shim, not a dispatcher.** `al_reg_write32` adds no arch logic; it forwards to `csr_write`. The arch-specific behavior lives one layer up, in the `tdrv_arch_ops` vtable and the per-arch `instruction_block_*` emitters at [3].
- **The compatibility-mode fork is at [5], invisible above.** Whether the batched copy uses the real ioctl or the `_cm` fallback is decided inside `ndl.c` based on driver capability; layers [1]–[4] are oblivious. (See [ndl](ndl.md) for the dual-dispatch pattern.)

---

## 3. Layer → TU → Owning Sub-Page

This is the routing table for the rest of Part IV: each architectural layer, the TU family that implements it (verbatim `/opt/workspace/KaenaRuntime/` paths from the DWARF CU set), a representative nm-verified symbol, and the sibling page that derives it. Confidence is HIGH throughout — every TU path is a verbatim `DW_AT_name` string and every symbol is nm-verified (`P1-F-SRCMAP §6`).

| Layer | Owning TU(s) | TU fns | Representative symbol | Owning sub-page(s) | Conf |
|---|---|---|---|---|---|
| **[1] Public API — lifecycle** | `nrt/nrt_init.cpp`, `nrt_exec.cpp`, `nrt_infodump.cpp` | 16+3+9 | `nrt_init @0x94e90` | [api-lifecycle](api-lifecycle.md) | HIGH |
| **[1] Public API — device/config** | `nrt/nrt_config.cpp`, `utils/instance_helpers.c` | 51+11 | `nrt_config_parse_init_config @0x8a1d0` | [api-device-config](api-device-config.md), [config-structs](config-structs.md), [env-vars](env-vars.md) | HIGH |
| **[1] Public API — tensors** | `nrt/nrt_tensor.cpp` → `tdrv/tensor.c` | 5→26 | `nrt_tensor_allocate` | [api-tensors](api-tensors.md) | HIGH |
| **[1] Public API — async/CC** | `nrt/nrt_async.cpp`, `nrt_async_sendrecv.cpp`, `nrt_collectives.cpp` | 22+11+6 | `nrt_load_collectives @0xaa4c0` | [api-async-collectives](api-async-collectives.md) | HIGH |
| **[2] Exec manager (KMGR)** | `kmgr/dlr.cpp`, `kmgr/xu/{queue,tpb_xu,xu_worker}.{c,cc}` | 43+9+21+4 | `kmgr_load_nn_nc @0xde280` | [exec/kmgr-facade](../exec/kmgr-facade.md), [exec/xu-workers](../exec/xu-workers.md), [exec/submit-path](../exec/submit-path.md) | HIGH |
| **[3] TDRV — bring-up** | `tdrv/init.c`, `tdrv/tdrv.c` | 28+67 | `tdrv_init @0x26a310` | [tdrv-lifecycle](tdrv-lifecycle.md) | HIGH |
| **[3] TDRV — dmem allocator** | `tdrv/dma_memory.c` | 25 | `dmem_alloc_internal @0x228640` | [tdrv-dmem](tdrv-dmem.md) | HIGH |
| **[3] TDRV — DMA rings** | `tdrv/dma_ring.c`, `dma_queue_reg_offset.c`, `ioq_switcher.c` | 44+20+4 | `dma_ring.c` family | [tdrv-dma-rings](tdrv-dma-rings.md) | HIGH |
| **[3] TDRV — scratchpad/sync** | `tdrv/scratchpad.c`, `sync_point.c` | 11+3 | `dmem_alloc_contiguous_scratchpad_page @0x229080` | [tdrv-scratchpad](tdrv-scratchpad.md) | HIGH |
| **[3] TDRV — tensor objects** | `tdrv/tensor.c` | 26 | `tdrv/tensor.c` family | [tdrv-tensor](tdrv-tensor.md) | HIGH |
| **[3] TDRV — arch-ops dispatch** | `tdrv/tdrv_arch_type.c`, `events.c` | 83+4 | `tdrv_arch_ops @0xc97180` (vtable) | [tdrv-arch-ops](tdrv-arch-ops.md) | HIGH |
| **[3] TDRV — instruction blocks** | `tdrv/instruction_block_{sunda,cayman,mariana}.c` | 238+248+297 | `instruction_block_mariana.c` (largest TU) | [isa/instruction-record](../isa/instruction-record.md), [isa/validators-per-arch](../isa/validators-per-arch.md) | HIGH |
| **Per-arch device layer** | `tdrv/{sunda,cayman,mariana}/tdrv_arch_*.c`, `isa.c` | 47×3 | `tdrv_arch_sunda.c` | [arch-geometry](arch-geometry.md), [arch-csr-offsets](arch-csr-offsets.md), [arch-notification](arch-notification.md), [arch-sdma](arch-sdma.md), [arch-stpb](arch-stpb.md) | HIGH |
| **[4] KaenaHal adapter** | `tdrv/hal_platform.c`, `helper.c`, `csr.c` | 14+20+8 | `al_reg_read32 @0x2658a0` | [hal-adapter](hal-adapter.md), [hal-tpb-shims](hal-tpb-shims.md), [hal-registers](hal-registers.md), [hal-udma-iofic](hal-udma-iofic.md) | HIGH |
| **[5] NDL kernel shim** | `KaenaDriverLib-2.27.4.0/src/ndl.c` (vendored) | 123 | `ndl_device_ioctl`, `ndl_misc_ioctl` | [ndl](ndl.md) | HIGH |
| **[K] Kernel driver** | DKMS module (separate binary) | — | `ncdev_ioctl` | [kernel/ioctl-dispatch](../kernel/ioctl-dispatch.md) | HIGH |

> **QUIRK —** the public API is the *smallest* layer by code mass (377 fns across 27 TUs) and the device-driver core is by far the largest (2,413 fns across 84 TUs). Most `nrt_*` public functions are thin wrappers — `nrt_tensor.cpp` (5 fns) forwards straight to `tdrv/tensor.c` (26 fns), `nrt_async_sendrecv.cpp` thunks into `enc/async_sr/*`. A reimplementer who budgets effort by the public-header surface will badly under-scope TDRV. The weight is in the instruction-block builders and the collectives engine ([Part IX](../collectives/overview.md)), not the ABI.

> **NOTE —** the **on-device collectives engine** (`enc/` + `encd/`, 52 TUs / 1,062 fns) and the **NEFF/KELF container loader** (`kelf/`, 5 TUs / 318 fns, with Libarchive+zlib+Simdjson) are peer subsystems that sit *beside* this six-layer spine, not inside it — they are driven from KMGR/TDRV but own their own architecture. They are mapped by [Part IX](../collectives/overview.md) and [Part V](../neff/overview.md) respectively and are out of scope for this page.

### 3.1 The per-arch fan-out

Three of the layers above are **triplicated by silicon generation**, and the triplication is a structural fact a reimplementer must mirror, not flatten. The DWARF CU set shows the same three codenames recurring across TDRV, the per-arch device layer, and the DVE config: **sunda**, **cayman**, **mariana**. Each gets its own copy of the geometry/ISA/config TUs, and a single one-shot vtable picks the family at bring-up.

| Per-arch concern | sunda TU | cayman TU | mariana TU | Selector |
|---|---|---|---|---|
| Arch geometry / address map | `tdrv/sunda/tdrv_arch_sunda.c` (47) | `tdrv/cayman/tdrv_arch_cayman.c` (47) | `tdrv/mariana/tdrv_arch_mariana.c` (46) | `tdrv_arch_register_{sunda,cayman,mariana}` |
| Instruction-block emitter | `tdrv/instruction_block_sunda.c` (238) | `instruction_block_cayman.c` (248) | `instruction_block_mariana.c` (297) | per-arch direct call from IB build |
| ISA constants | `tdrv/sunda/isa.c` (1) | `tdrv/cayman/isa.c` (1) | — (inlined) | arch-ops slot |
| DVE dynamic config | `sunda/dve_dynamic_config_sunda.c` | `cayman/dve_dynamic_config_cayman.c` | `mariana/dve_dynamic_config_mariana.c` | `dve_config_{sunda,cayman,mariana}.c` |
| Encd device-side ops | `encd/archs/sunda.c` (88) | `encd/archs/cayman.c` (95) | `encd/archs/mariana.c` (95) | `arch_device_mapping.c @ encd` |

The selector is the **`tdrv_arch_ops` vtable** (`@0xc97180`, .bss, 488 B / 47 slots), populated **exactly once, lazily**, by one of `tdrv_arch_register_{sunda,cayman,mariana}` according to the silicon's arch type (gate `tdrv_arch_ops_initialized @0xc97368`; `switch(arch_type){2→sunda, 3→cayman, 4→mariana}`). Every `tdrv_arch_get_*` geometry accessor double-checks the gate and routes through a slot — so the per-arch divergence is resolved by **one indirect call**, not by `if (arch == …)` scattered through the device-driver core. The instruction-block emitters are the exception: they are dispatched directly by the IB builder rather than through the vtable, and they are the **single densest code mass** in the binary (783 combined functions). The arch-ops install and the geometry accessors are owned by [tdrv-arch-ops](tdrv-arch-ops.md); the per-arch instruction builders by [isa/validators-per-arch](../isa/validators-per-arch.md).

> **GOTCHA —** the codenames are silicon generations, not products: a reimplementer mapping them to cloud instance names must go through the arch enum (`arch_type` `2/3/4`), not the marketing name. The mapping is owned by [arch/generations-enum](../arch/generations-enum.md). Do **not** assume a fourth `…_v4` arch exists in this build merely because the enum reaches 4 — `instruction_block_mariana.c` inlines its `isa.c`, so the per-arch TU inventory is asymmetric (sunda/cayman have a separate `isa.c`, mariana does not).

---

## 4. Cross-Cutting Infrastructure

Four small subsystems are called from *every* layer of the stack and belong to no single one. They are the runtime's shared plumbing; the stack diagram of §1 lists them at the bottom as cross-cutting. Each has its own derivation page; this sidebar names the TU, the entry symbol, and the role so the reader knows where each utility lives.

| Infra | Owning TU(s) | Entry symbol | Role | Owning page |
|---|---|---|---|---|
| **nlog** (logging + backtrace) | `nlog/nlog.c`, `backtrace.c`, `resolutions.c` (3 TU / 15 fn) | `nlog_write @0x224d40`, `nlog_coalescing_init_thread`, `nlog_backtrace @0x508220` | Per-thread coalescing log ring + DWARF symbol resolution; the tail "Generic API Failure" flush every `nrt_*` entry runs | [logging](logging.md) |
| **device book** (db / dbtc) | `tdrv/db.c` (15 fn), `tdrv/dbtc.c` (9 fn) | `db_physical_core_get_mla_and_tpb`, `db_tdrv_ctx_get` | Process-global registry mapping `(device, NeuronCore)` → `mla_t`/`physical_core_t`/`tdrv_ctx_t`; how every layer resolves "which core" | [device-book](device-book.md) |
| **interned strings** | `nrt/nrt_interned_string_db.cpp` (5 fn) | `nrt_interned_string_db_get_id @0x509250` | Model-id and tag strings → stable `uint64` ids; deduplicates trace/profile payloads | [interned-strings](interned-strings.md) |
| **vring** (virtual rings) | `tdrv/vring.c` (34 fn), `vtpb.c` (21 fn) | `vring.c` family | Software DMA ring / virtual-TPB packet builders that sit between TDRV and the DMA descriptor engine | [dma/virtual-rings](../dma/virtual-rings.md) |

Two of these are worth a reimplementer's attention up front. **The device book** is the indirection that lets every layer name a NeuronCore without holding device pointers: `dmem_alloc_internal` calls `db_physical_core_get_mla_and_tpb(pcore, &mla, &tpb)` to resolve the owning MLA/TPB before it allocates, and `db_tdrv_ctx_get()` returns the per-process `tdrv_ctx_t` that the profile and EFA-BDF paths read. **The interned-string DB** is what makes the trace plane cheap: a model id is hashed to a `uint64` once (`nrt_get_interned_model_id @0x96fa0 → nrt_interned_string_db_get_id`) and thereafter every trace event carries the integer, not the string. Both are global singletons initialized inside `nrt_init`'s subsystem-init phase (`nlog_init`, then the per-vcore book) and are torn down in reverse by `nrt_close`.

> **NOTE —** the trace/profile producer chain (`nrt/nrt_profile.cpp`, `nrt_sys_trace*.cpp`, `nrt_inspect*.cpp`) and its vendored KaenaProfilerFormat protobuf backend (`ntff.pb.cc`/`neuron_trace.pb.cc`, 730 generated fns) read these infra services heavily but are their own subsystem — see [Part XIII](../trace/overview.md). The Rust `neuron_rustime` capture engine bridges into `nlog` and the interned-string DB via a cbindgen FFI boundary ([trace/rust-ffi](../trace/rust-ffi.md)).

---

## Cross-References

**The six layers, top to bottom**
- [api-lifecycle](api-lifecycle.md) — layer [1]: `nrt_init`/`nrt_close`, the 4-state `NRT_INIT_STATE` guard, driver-compat-id negotiation
- [api-device-config](api-device-config.md) — layer [1]: device/topology query wrappers and config parsing
- [exec/kmgr-facade](../exec/kmgr-facade.md) — layer [2]: the KMGR exec manager facade and the `dlr_model_set` model DB
- [tdrv-lifecycle](tdrv-lifecycle.md) — layer [3]: `tdrv_init` device bring-up and the `tdrv_arch_ops` vtable
- [tdrv-dmem](tdrv-dmem.md) — layer [3]: the `dmem` device-memory allocator (the worked-example H2D staging path)
- [hal-adapter](hal-adapter.md) — layer [4]: the `al_*` KaenaHal platform-services adapter over `csr.c`
- [ndl](ndl.md) — layer [5]: the `ndl_*` IOCTL/mmap shim and the compatibility-mode fallback dispatch

**Cross-cutting and peer subsystems**
- [device-book](device-book.md) · [interned-strings](interned-strings.md) · [logging](logging.md) — the §4 shared plumbing
- [tdrv-tensor](tdrv-tensor.md) — the `nrt_tensor_t` object layer that bridges API [1] and TDRV [3]
- [collectives/overview](../collectives/overview.md) — the `enc`/`encd` peer subsystem beside the spine ([Part IX](../collectives/overview.md))
- [neff/overview](../neff/overview.md) — the NEFF/KELF container loader peer subsystem ([Part V](../neff/overview.md))
- [kernel/ioctl-dispatch](../kernel/ioctl-dispatch.md) — the kernel `ncdev_ioctl` side of the NDL boundary ([Part III](../kernel/overview.md))
- [front/source-tree](../front/source-tree.md) — the full 203-TU KaenaRuntime source-tree reconstruction this map summarizes
- [back to index](../index.md)
