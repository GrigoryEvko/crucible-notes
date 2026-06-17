# TopSP Notification Path

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0**, shipped under `/usr/src/aws-neuronx-2.27.4.0/`. This page owns exactly two files — `neuron_topsp.c` (68 lines) and `neuron_topsp.h` (61 lines), both read in full. The per-generation TopSP bodies it dispatches through (`v2/neuron_dhal_v2.c`, `v3/neuron_dhal_v3.c`) belong to [DHAL V2](dhal-v2.md) / [DHAL V3](dhal-v3.md) and are cited here only at the vtable boundary. The boundary structs (`struct ndhal_topsp`, `ndhal_address_map`) are cited at their definition site in `neuron_dhal.h`. Other driver versions renumber lines.*
> *Evidence grade: **Confirmed (source-anchored)** — the lifecycle, the dispatch, and the sync-primitive model are direct C, not reverse-engineered. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

A **TopSP** ("Top Sync Processor") is a per-device engine whose one instruction engine exists "mainly to orchestrate collective operations (e.g. allreduce) on a neuron device" (`neuron_topsp.c:6-11`). It is **absent on V1** and present "only on V2 and after" (`neuron_topsp.c:6`): V2 ships 6 per device, V3/V4 ship 16. Each TopSP's instruction stream is **fed through DMA** (`neuron_topsp.c:11`) — the kernel never authors that stream; the device-side collectives emitter does (see [encd](../collectives/encd-overview.md)). As the TopSP engine runs, it **produces** completion and profiling messages into a circular **Notification Queue** in device memory that the hardware writes and applications drain by **device DMA** (`NEURON_IOCTL_MEM_BUF_COPY`, `neuron_topsp.c:14-20`). For synchronizing those programs against hardware blocks and software, the TopSP exposes two primitives the engine's instructions can wait on: **events** (a 1-bit bitmap holding 0 or 1) and **semaphores** (any value in the **signed 32-bit** range) (`neuron_topsp.c:22-28`).

The kernel's role in all of this is narrow and clerical. The TopSP's *instruction stream* and its *semaphore/event programming* are the device program's concern, not the driver's; what the kernel owns is the **TopSP NQ lifecycle** — allocate the ring backing store, program the per-TopSP NOTIFIC base/size registers, hand back an `mmap` offset, and tear the ring down on reset or device removal. The owned translation unit `neuron_topsp.c` is **arch-neutral and tiny**: it defines exactly two functions — `ts_nq_destroy` (one NQ) and `ts_nq_destroy_all` (device-wide) — both of which delegate the architecture-specific work through the DHAL vtable `ndhal->ndhal_topsp` (a `struct ndhal_topsp`, `neuron_dhal.h:63-70`). The two remaining lifecycle entry points the header declares — `ts_nq_init` and `ts_nq_destroy_one` — have **no body here**; their implementations live in the per-generation DHAL (`ts_nq_init_v{2,3}`, `ts_nq_destroy_one_v{2,3}`) and are reached only through the function pointers.

This is the **teardown + sync-model** sibling of the [Notification Queue Engine](notification-queues.md) page. That page owns the NQ machinery in full — the `0x28`-byte register block, the `nq_id` formula, the host-vs-device alloc rule, and the `ts_nq_init` init body. This page does **not** re-derive any of that. It documents the three things `neuron_topsp.c` actually owns: the **V2+-only gating** and per-gen TopSP count, the **arch-neutral destroy path** (`ts_nq_destroy` / `ts_nq_destroy_all`) and how it routes back through the DHAL, and the **semaphore/event sync model** (signed-32-bit semaphores, 1-bit events) the TopSP banner specifies — together with one genuine off-by-one in the NeuronCore-facing event accessor that shares that model.

For reimplementation, the contract is:

- **The V2+ gate and per-gen TopSP count.** No TopSP on V1; `ndhal_address_map.ts_per_device` is `6` on V2 (`V2_TS_PER_DEVICE`, `v2/address_map.h:56`) and `16` on V3/V4 (`V3_TS_PER_DEVICE = V3_NC_PER_DEVICE(8) * V3_TS_PER_NC(2)`, `v3/address_map.h:71`). Every `ts_id` bound and the `destroy_all` loop are driven by this scalar.
- **The `ndhal_topsp` vtable** — `{ts_nq_init, ts_nq_destroy_one, ts_nq_get_nqid, ts_nq_set_hwaddr}` (`neuron_dhal.h:64-69`). The two owned functions touch hardware **only** through `ts_nq_get_nqid` and `ts_nq_set_hwaddr`; they never compute a BAR0 offset themselves.
- **The arch-neutral destroy semantics** — `ts_nq_destroy` halts then frees **one** TopSP NQ in a single function (zero the hwaddr, `mc_free`), idempotent on a `NULL` handle; `ts_nq_destroy_all` sweeps every TopSP via `ts_nq_destroy_one`.
- **The sync-primitive model** — signed-32-bit semaphores and 1-bit events, the hardware the TopSP engine's `wait`-instructions test, with a per-device count of 256 each (`V{2,3}_SEMAPHORE_COUNT`/`_EVENTS_COUNT`, `v{2,3}/address_map.h`).

| | |
|---|---|
| **Owns** | `neuron_topsp.c` (68 lines) · `neuron_topsp.h` (61 lines), both read in full |
| **Defined here** | `ts_nq_destroy` (`neuron_topsp.c:42`) · `ts_nq_destroy_all` (`:62`) |
| **Declared, defined in DHAL** | `ts_nq_init` (proto `neuron_topsp.h:27`) · `ts_nq_destroy_one` (proto `:50`) |
| **Dispatch vtable** | `struct ndhal_topsp` (`neuron_dhal.h:63-70`) — `{ts_nq_init, ts_nq_destroy_one, ts_nq_get_nqid, ts_nq_set_hwaddr}` |
| **V2+ gate** | "TOP_SPs are only on V2 and after" (`neuron_topsp.c:6`); V1 has no TopSP |
| **TopSP count** | `ts_per_device` = **6** V2 (`v2/address_map.h:56`), **16** V3/V4 (`v3/address_map.h:71`) |
| **Handle store** | `nd->ts_nq_mc[MAX_TS_PER_DEVICE(16)][MAX_NQ_SUPPORTED(96)]` (`neuron_device.h:12,93`) |
| **NQ-id formula** | `nq_id = nq_type * MAX_NQ_QUEUES(16) + index` (`v2/neuron_dhal_v2.c:261`, `v3/...:441`) |
| **NQ drain path** | hardware writes; userspace reads via `NEURON_IOCTL_MEM_BUF_COPY` (`neuron_topsp.c:20`) |
| **Sync primitives** | semaphores = **signed 32-bit**; events = **1-bit** (`neuron_topsp.c:22-28`); 256 each per device |
| **Inbound (init)** | `neuron_cdev.c:2029/2060` → `ndhal_topsp.ts_nq_init` when `nq_dev_type == NQ_DEVICE_TYPE_TOPSP` |
| **Inbound (destroy)** | `neuron_nq.c:135` per-core sweep → `ndhal_topsp.ts_nq_destroy_one` |
| **Down boundary** | ring backing → [Memory Pool](mempool-handles.md) (`mc_free`); register geometry → [DHAL V2](dhal-v2.md)/[V3](dhal-v3.md) |

---

## 1. What a TopSP Is, and the V2+ Gate

### Purpose

The TopSP is the device's **collective-orchestration sequencer**. A NeuronCore runs the math kernels; a TopSP runs a small instruction stream — fed in over DMA, authored by the device-side emitter — that sequences a collective (an allreduce, a ring step, a barrier) across the device's engines, gating each step on semaphores and events until the participants are aligned. The driver does not see the collective; it sees only the TopSP's telemetry ring and its lifecycle.

The banner comment (`neuron_topsp.c:6-29`) is the only in-source prose that specifies the engine, and every clause of it is a reimplementation fact:

| Clause | Source | Reimplementation consequence |
|---|---|---|
| "TOP_SPs are only on V2 and after" | `neuron_topsp.c:6` | V1 builds set no `ndhal_topsp` body; `ts_per_device` is the gate |
| One engine, "orchestrate collective operations (e.g. allreduce)" | `neuron_topsp.c:10` | the TopSP is a collectives sequencer, not a compute core |
| "Each engine's instruction stream is fed through DMA" | `neuron_topsp.c:11` | the kernel never writes the op-stream; the emitter does ([encd](../collectives/encd-overview.md)) |
| NQ is "a circular buffer in device memory" the hardware writes | `neuron_topsp.c:19` | producer = hardware, consumer = userspace |
| consumed "by device DMA (`NEURON_IOCTL_MEM_BUF_COPY`)" | `neuron_topsp.c:20` | no host `mmap` of the ring on the device-DRAM path; drain by DMA copy |
| events = "either 1 or 0"; semaphores "any value in signed 32 bit range" | `neuron_topsp.c:25-26` | the sync primitives are 1-bit events + **s32** semaphores (§4) |

### The gate: `ts_per_device`

The V2+ gate is not an `#ifdef` — it is the scalar `ndhal->ndhal_address_map.ts_per_device` (`neuron_dhal.h:47`), set per generation by the arch registrar. Every owned function reads it as the upper bound on a TopSP id.

| Generation | `ts_per_device` | Derivation | Source |
|---|---|---|---|
| V1 | — (no TopSP) | TopSP absent | `neuron_topsp.c:6` |
| V2 (Trn1/Inf2) | **6** | `V2_TS_PER_DEVICE` literal | `v2/address_map.h:56`, assigned `v2/neuron_dhal_v2.c:1398` |
| V3 (Trn2) | **16** | `V3_NC_PER_DEVICE(8) * V3_TS_PER_NC(2)` | `v3/address_map.h:71`, assigned `v3/neuron_dhal_v3.c:1890` |
| V4 (Trn3) | **16** | inherits V3 wiring (piggybacks) | `v4/neuron_dhal_v4.c:453` |

A compile-time `static_assert( MAX_TS_PER_DEVICE >= V{2,3}_TS_PER_DEVICE, ...)` (`v2/neuron_dhal_v2.c:1366`, `v3/...:1857`) guards the first dimension of `ts_nq_mc[MAX_TS_PER_DEVICE][...]` (`= 16`, `neuron_device.h:12,93`) against a generation that ever exceeds 16 TopSPs.

> **QUIRK —** the V2+ gate is realized purely as data, not control flow. There is no `if (arch >= V2)` anywhere in `neuron_topsp.c`; the gate is that on V1 no registrar ever installs an `ndhal_topsp` body, and `ts_per_device` is the loop/range bound that makes the destroy paths no-ops. A reimplementer who hard-codes a TopSP count, instead of reading the per-arch `ts_per_device`, will over- or under-sweep — V2's 6 and V3/V4's 16 differ, and the array is dimensioned for the 16-wide worst case (`MAX_TS_PER_DEVICE`), not the live count.

> **QUIRK —** V4 ships **no** TopSP code of its own. The V4 registrar leaves all four `ndhal_topsp` pointers at their V3 values (`v4/neuron_dhal_v4.c:453` flags the design as "V4 is piggybacking on V3"; the topsp pointers are never re-overridden after the V3 base registrar ran). A reimplementation that treats V4 as a self-contained arch must still run the V3 TopSP wiring underneath it — see the [DHAL Core](dhal-core.md) V4-on-V3 compose.

### Considerations

The two owned functions form the *teardown* half of the lifecycle; the *init* half (`ts_nq_init`, the alloc + register-program body) lives in the DHAL and is documented by [notification-queues §6](notification-queues.md). The split is asymmetric on purpose: init needs the full per-arch NOTIFIC geometry inline, so it lives where the geometry constants are; destroy needs only the DHAL `ts_nq_set_hwaddr` (to zero the registers) plus a `mc_free`, so it can stay arch-neutral.

---

## 2. The TopSP NQ Lifecycle and the Vtable

### Purpose

Every hardware-touching action on a TopSP NQ goes through the four-pointer `struct ndhal_topsp` vtable. The owned `neuron_topsp.c` functions reach exactly two of those four; the other two are the init / per-TopSP-destroy bodies the DHAL provides. Reproducing the boundary correctly means knowing which symbol is dispatched and which is defined locally.

### The vtable

`struct ndhal_topsp` (`neuron_dhal.h:63-70`), in member order:

| # | Member | Signature (abbrev) | Defined in | Owned here? |
|---|---|---|---|---|
| 0 | `ts_nq_init` | `(nd, ts_id, eng_index, nq_type, size, on_host, dram_ch, dram_reg, force, **nq_mc, *mmap_off) → int` | `ts_nq_init_v{2,3}` (`v2:308`, `v3:491`) | no — DHAL |
| 1 | `ts_nq_destroy_one` | `(nd, ts_id) → void` | `ts_nq_destroy_one_v{2,3}` (`v2:355`, `v3:539`) | no — DHAL |
| 2 | `ts_nq_get_nqid` | `(nd, index, nq_type) → u8` | `ts_nq_get_nqid_v{2,3}` (`v2:258`, `v3:441`) | **called by** `ts_nq_destroy` |
| 3 | `ts_nq_set_hwaddr` | `(nd, ts_id, index, nq_type, size, queue_pa) → void` | `ts_nq_set_hwaddr_v{2,3}` (`v2:275`, `v3:458`) | **called by** `ts_nq_destroy` |

> **GOTCHA —** the header `neuron_topsp.h` *declares* `ts_nq_init` (`:27`) and `ts_nq_destroy_one` (`:50`) as if they were ordinary functions, but **neither has a definition outside the DHAL**. There is no `int ts_nq_init(...)` body anywhere except `ts_nq_init_v2`/`_v3`; every live call routes through `ndhal->ndhal_topsp.ts_nq_init` (`neuron_cdev.c:2029,2060`) or `.ts_nq_destroy_one` (`neuron_nq.c:135`). The bare prototypes are effectively vestigial — a single-arch reimplementation that calls the bare symbol `ts_nq_init` will fail to link; it must install and dispatch through the pointer.

### The lifecycle, end to end

```text
CREATE (init — owned by notification-queues §6, DHAL body)
  ioctl NOTIFICATIONS_INIT_V2 / _WITH_REALLOC_V2
    └─ ncdev_nc_nq_init_libnrt / _with_realloc_libnrt   (neuron_cdev.c:2014 / 2045)
         if arg.nq_dev_type == NQ_DEVICE_TYPE_TOPSP:    (neuron_cdev.c:2028 / 2059)
           └─ ndhal->ndhal_topsp.ts_nq_init(...)         (neuron_cdev.c:2029 / 2060)
                == ts_nq_init_v{2,3}  → alloc ring, ts_nq_set_hwaddr, record ts_nq_mc[ts_id][nq_id]

RUN (not the kernel's concern)
  device-side emitter feeds the TopSP op-stream via DMA   (neuron_topsp.c:11; → collectives/encd)
  hardware produces NQ messages into the circular ring     (neuron_topsp.c:14-20)
  userspace drains the ring by device DMA (MEM_BUF_COPY)   (neuron_topsp.c:20)

DESTROY (owned here)
  per-core sweep:  neuron_nq.c:135
    └─ ndhal->ndhal_topsp.ts_nq_destroy_one(nd, ts_id)
         == ts_nq_destroy_one_v{2,3}                       (v2:355 / v3:539)
              for eng_index 0..MAX_NQ_ENGINE(16):
                for nq_type 0..MAX_NQ_TYPE(6):
                  └─ ts_nq_destroy(nd, ts_id, eng_index, nq_type)   ◀ INTO this cell (neuron_topsp.c:42)
  device-wide:     ts_nq_destroy_all(nd)                   (neuron_topsp.c:62)  [no in-tree caller — §3]
```

The inbound edge that matters is the one **back into** this cell: the DHAL's `ts_nq_destroy_one_v{2,3}` loops `(eng_index, nq_type)` over the full `16 × 6` grid and calls the arch-neutral `ts_nq_destroy` for each slot (`v2/neuron_dhal_v2.c:359-361`). So the per-TopSP teardown is a DHAL loop, and the per-NQ teardown is this page's `ts_nq_destroy`.

> **GOTCHA —** the per-TopSP destroy loop iterates `nq_type` over `0 .. MAX_NQ_TYPE(6)` (`v2/neuron_dhal_v2.c:360`) **even on V3**, where `V3_MAX_NQ_TYPE` is 5 (V3 drops `THROTTLE`; `v3/address_map.h:82`). This is harmless: `ts_nq_get_nqid` returns a larger `nq_id` for type 5, but `ts_nq_mc[ts_id][nq_id] == NULL` for a queue that was never created, so `ts_nq_destroy` early-returns 0 (`neuron_topsp.c:53-54`). A reimplementer who tightens the loop to `V3_MAX_NQ_TYPE` on V3 is equivalent; one who feeds type 5 into a V3 *init* allocates a real ring past the throttle gap. (Same asymmetry as [notification-queues §2](notification-queues.md); this cell inherits it from the shared `MAX_NQ_TYPE`.)

---

## 3. The Destroy Path — `ts_nq_destroy` / `ts_nq_destroy_all`

### Purpose

These are the two functions `neuron_topsp.c` actually defines. `ts_nq_destroy` tears down **one** TopSP NQ — halt its hardware then free its backing memory, both in one call. `ts_nq_destroy_all` tears down **every** TopSP on the device by looping `ts_nq_destroy_one` over `ts_per_device`. Both are arch-neutral; both reach hardware only through the vtable.

### Algorithm

```c
// ts_nq_destroy(nd, ts_id, eng_index, nq_type) — neuron_topsp.c:42-60.  PUBLIC, arch-neutral.
// Halt + free ONE TopSP NQ.  Idempotent on an absent ring.
function ts_nq_destroy(nd, ts_id, eng_index, nq_type):
    if nd == NULL || ts_id >= ndhal->ndhal_address_map.ts_per_device:   // :46  TopSP-id validity
        return -EINVAL                                                   //      (flat ts_id, not nc_id)

    nq_id = ndhal->ndhal_topsp.ts_nq_get_nqid(nd, eng_index, nq_type)    // :49  (nq_type*16)+eng_index
    if nq_id >= MAX_NQ_SUPPORTED:                                        // :50  (= 96)
        return -EINVAL

    if nd->ts_nq_mc[ts_id][nq_id] == NULL:                              // :53  no ring here
        return 0                                                        // :54  idempotent no-op

    ndhal->ndhal_topsp.ts_nq_set_hwaddr(nd, ts_id, eng_index, nq_type, 0, 0)  // :56  HALT: size=0, pa=0
                                                                        //      → zero NQ base/size regs
    mc_free(&nd->ts_nq_mc[ts_id][nq_id])                               // :58  FREE + NULL the handle
    return 0                                                            // :59


// ts_nq_destroy_all(nd) — neuron_topsp.c:62-68.  PUBLIC, arch-neutral.  Device-wide sweep.
function ts_nq_destroy_all(nd):
    for ts_id in 0 .. ndhal->ndhal_address_map.ts_per_device - 1:       // :65  6 on V2, 16 on V3/V4
        ndhal->ndhal_topsp.ts_nq_destroy_one(nd, ts_id)                 // :66  → DHAL loop → ts_nq_destroy
```

Three facts a reimplementer must preserve:

1. **The id is a flat `ts_id`, bounded by `ts_per_device`** (`neuron_topsp.c:46`) — not a `(nc_id, dev_nc_map)` pair like the NeuronCore path. A TopSP is addressed by a device-global index `[0, ts_per_device)`.
2. **Halt-then-free is fused in one function.** `ts_nq_set_hwaddr(..., 0, 0)` zeroes the ring's hardware base/size registers so the producer stops, *then* `mc_free` releases the host/DRAM backing. The order matters — freeing before halting would leave the hardware's base register pointing at freed memory. Unlike the NeuronCore sweep (which splits halt and free across two loops with a `msleep(1)` between, [notification-queues §5.1](notification-queues.md)), `ts_nq_destroy` does both for one NQ with **no per-NQ drain delay**; the per-core sweep's single `msleep(1)` (`neuron_nq.c:125`) covers the TopSPs too, because `nnq_destroy_nc` calls `ts_nq_destroy_one` *after* its own drain.
3. **`mc_free(&nd->ts_nq_mc[ts_id][nq_id])`** takes the address of the slot, so the helper NULLs the handle as it frees (`neuron_topsp.c:58`) — which is exactly what makes the next `ts_nq_destroy` on the same slot the idempotent no-op at `:53-54`.

### Function Map

| Function | file:line | Role | Confidence |
|---|---|---|---|
| `ts_nq_destroy` | `neuron_topsp.c:42` | Halt + free one TopSP NQ; idempotent on `NULL` handle | CERTAIN |
| `ts_nq_destroy_all` | `neuron_topsp.c:62` | Device-wide sweep over `ts_per_device` via `ts_nq_destroy_one` | CERTAIN |
| `ts_nq_destroy_one_v2` / `_v3` | `v2/neuron_dhal_v2.c:355`, `v3/neuron_dhal_v3.c:539` | DHAL loop `16×6` → back into `ts_nq_destroy` | CERTAIN |
| `ndhal_topsp.ts_nq_get_nqid` | `v2:258`, `v3:441` | `(nq_type * MAX_NQ_QUEUES(16)) + index` | CERTAIN |
| `ndhal_topsp.ts_nq_set_hwaddr` | `v2:275`, `v3:458` | per-TopSP NOTIFIC offset + write HI/LO/F_SIZE (zero on halt) | CERTAIN |
| `mc_free` | K-MEMPOOL boundary | free ring backing + NULL the handle slot | HIGH (boundary) |
| `ndhal_topsp.ts_nq_init` | `v2:308`, `v3:491` | init body (alloc/program/mmap) — owned by [notification-queues §6](notification-queues.md) | CERTAIN |

### Considerations

`ts_nq_set_hwaddr_v{2,3}` is the one place the destroy path touches BAR0. It resolves the **per-TopSP NOTIFIC** offset and writes three registers; on the halt call all three carry zero. The base is *always* the per-TopSP offset — TopSPs have no SDMA-trace NOTIFIC region, so the `NQ_TYPE_TRACE_DMA` special case that complicates the NeuronCore writer is absent here (`v2/neuron_dhal_v2.c:281`, `v3:464`):

```c
// ts_nq_set_hwaddr_v2(nd, ts_id, index, nq_type, size, queue_pa) — v2/neuron_dhal_v2.c:275-289.
// (v3 body identical with _v3 geometry — v3/neuron_dhal_v3.c:458-472)
function ts_nq_set_hwaddr_v2(...):
    apb_base = nd->npdev.bar0 + notific_get_relative_offset_topsp_v2(ts_id)   // :281  ALWAYS per-TopSP
    notific_write_nq_base_addr_hi(apb_base, index, queue_pa >> 32)           // :286
    notific_write_nq_base_addr_lo(apb_base, index, queue_pa & 0xffffffff)    // :287
    notific_write_nq_f_size      (apb_base, index, size)                     // :288  size=0 on halt
```

The per-TopSP offset is a strided base — `..._TOP_SP_0_RELBASE + ..._TOP_SP_0_NOTIFIC_RELBASE + ..._TOP_SP_0_SIZE * ts_idx` on V2 (`v2/notific.h:42-49`; stride `V2_APB_IOFAB_TOP_SP_0_SIZE = 0x40000`, `v2/address_map.h:185`), gaining a die-distance term on V3 (`v3/notific.h:52-61`). The full geometry is owned by [notification-queues §3](notification-queues.md); this cell only needs that `ts_nq_set_hwaddr` selects one TopSP's NOTIFIC block and the `index` selects the NQ within it.

> **NOTE —** `ts_nq_destroy_all` has **no in-tree caller**. A tree-wide search finds only its definition (`neuron_topsp.c:62`) and its two declarations (`neuron_topsp.h:53,58`) — zero call sites. Device teardown reaches TopSP NQs through the *per-core* path instead: `nnq_destroy_nc` computes this core's TopSP range `[nc_id*ts_per_nc, (nc_id+1)*ts_per_nc)` and calls `ts_nq_destroy_one` per TopSP (`neuron_nq.c:133-135`), and `nnq_destroy_all` runs `nnq_destroy_nc` over every present core ([reset](reset.md) / device-removal paths). Treat `ts_nq_destroy_all` as a reserve/symmetry API — present, correct, but dead in this build (HIGH confidence on "no in-tree caller"). A reimplementer can omit it and lose nothing; the per-core sweep already covers every TopSP.

---

## 4. The Sync Model — Signed-32-bit Semaphores and 1-bit Events

### Purpose

The TopSP's reason to exist is *synchronization*, and the banner (`neuron_topsp.c:22-28`) specifies the two hardware primitives its instruction stream tests: **events** and **semaphores**. This section pins their model exactly as the source describes it, and documents the kernel-facing accessors that read/write the same primitive family — including one off-by-one bound a reimplementer would otherwise copy.

### The model, verbatim from source

```c
// neuron_topsp.c:22-28 — the only in-source specification of the sync primitives.
//   Events:     "simple bitmap which hold either 1 or 0"          → 1 bit, {0, 1}
//   Semaphores: "any value in signed 32 bit range"               → s32, [-2^31, 2^31-1]
//   The TopSP engine runs instructions that can:
//     - "wait for semaphore to reach a certain value"            → semaphore compare-wait
//     - "wait ... a particular event is set"                     → event test-wait
//   "Applications can use this to manipulate execution of the program."
```

The division of labor is the key fact. A **semaphore** carries a full **signed 32-bit** counter — engines increment/decrement it and other engines block until it crosses a threshold, the classic producer/consumer count for "N transfers done." An **event** is a single **bit** — a one-shot "this happened" flag an engine can set and another can wait on. The TopSP program (fed in over DMA) is a sequence of compute steps interleaved with `wait-semaphore` and `wait-event` instructions; that is how a collective keeps its participants in lockstep without the host in the loop.

These primitives are **not** programmed by the code on this page. The TopSP's own semaphore/event manipulation lives in its instruction stream (authored by the device-side emitter, [encd](../collectives/encd-overview.md)); the kernel's exposure of the *same primitive family* is the NeuronCore-facing accessor set in `neuron_core.c`, which userspace uses to read/poke a core's semaphores and events.

### The kernel accessors (NeuronCore-facing, same primitive family)

`neuron_core.c` exposes read/write/increment/decrement for semaphores and get/set for events, each bounded by a per-device count from `ndhal_address_map`:

| Accessor | file:line | Bound check | Backing |
|---|---|---|---|
| `nc_semaphore_read` | `neuron_core.c:50` | `index >= semaphore_count` → `-EINVAL` | `fw_io_read_csr_array` at sema base + `index*NC_SEMAPHORE_SIZE` |
| `nc_semaphore_write` | `neuron_core.c:68` | `index >= semaphore_count` | `writel` |
| `nc_semaphore_increment` | `neuron_core.c:88` | `index >= semaphore_count` | `writel` |
| `nc_semaphore_decrement` | `neuron_core.c:107` | `index >= semaphore_count` | `writel` |
| `nc_event_get` | `neuron_core.c:126` | `index > event_count` ◀ **off-by-one** | `fw_io_read_csr_array` |
| `nc_event_set` | `neuron_core.c:142` | `index > event_count` ◀ **off-by-one** | `writel` |

The per-device counts are 256 each on both shipped generations: `V{2,3}_SEMAPHORE_COUNT = 256` and `V{2,3}_EVENTS_COUNT = 256` (`v2/address_map.h:62-63`, `v3/address_map.h:76-77`), assigned into `ndhal_address_map.{semaphore_count, event_count}` by each registrar (`v2/neuron_dhal_v2.c:1396-1397`, `v3/...:1888-1889`).

> **GOTCHA —** the **event** accessors use `event_index > event_count` (strict `>`), while the **semaphore** accessors correctly use `semaphore_index >= semaphore_count`. With `event_count == 256`, the events check admits `event_index == 256` — one past the valid `[0, 255]` range — and lets it through to `nc_get_event_addr` + a `writel`/CSR read at `base + 256*stride`, one event slot beyond the array. The semaphore path rejects the same out-of-bounds index. This is a genuine off-by-one in the shipped source (`neuron_core.c:130,147` use `>`; compare `:55,73,92,111` which use `>=`). A reimplementer must use `>=` for both to match the *intended* (and semaphore-consistent) bound; copying the event `>` verbatim reproduces the bug. Severity LOW (a privileged-IOCTL one-slot over-read/over-write of a CSR region, not a user-reachable memory-safety break), but it is a real divergence worth flagging.

### Function Map

| Function | file:line | Role | Confidence |
|---|---|---|---|
| (banner spec) | `neuron_topsp.c:22-28` | the s32-semaphore / 1-bit-event model the TopSP engine waits on | CERTAIN |
| `nc_semaphore_{read,write,increment,decrement}` | `neuron_core.c:50/68/88/107` | NeuronCore semaphore accessors, bound `>= semaphore_count` | CERTAIN |
| `nc_event_{get,set}` | `neuron_core.c:126/142` | NeuronCore event accessors, bound `> event_count` (off-by-one) | CERTAIN |
| `ndhal_address_map.{semaphore_count,event_count}` | `neuron_dhal.h:45-46` | per-device counts (256/256), set by registrar | CERTAIN |

### Considerations

The semaphore/event hardware blocks themselves (their BAR0 base, the per-index stride `NC_SEMAPHORE_SIZE`, the `nc_get_semaphore_base` / `nc_get_event_addr` resolvers) live in the per-arch DHAL (`ndhal_nc`, `neuron_dhal.h`) and are out of this cell's scope; this page documents the *model* the TopSP banner specifies and the accessor *bounds*, not the register map. What a reimplementer takes from here is the contract: 1-bit events, signed-32-bit semaphores, 256 of each per device, tested by the TopSP engine's wait-instructions to sequence a collective — and the `>=`-vs-`>` discrepancy to avoid.

---

## Related Components

| Name | Relationship |
|---|---|
| `struct ndhal_topsp` | `{ts_nq_init, ts_nq_destroy_one, ts_nq_get_nqid, ts_nq_set_hwaddr}` — the vtable both owned functions dispatch through (`neuron_dhal.h:63-70`) |
| `nd->ts_nq_mc[16][96]` | per-TopSP ring-handle store this page frees and `ts_nq_init` populates (`neuron_device.h:93`) |
| `ndhal_address_map.ts_per_device` | the V2+ gate / loop bound (`6`/`16`) driving every `ts_id` range (`neuron_dhal.h:47`) |
| `enum NQ_DEVICE_TYPE` | `{NEURON_CORE, TOPSP}` — the IOCTL discriminator routing init to `ts_nq_init` (`share/neuron_driver_shared.h`) |
| `nnq_destroy_nc` | the per-core sweep (`neuron_nq.c:135`) that is the only live caller of `ts_nq_destroy_one` |
| `nc_event/semaphore_*` | the NeuronCore-facing accessors for the same s32/1-bit primitive family (`neuron_core.c:50-147`) |

## Cross-References

- [Notification Queue Engine](notification-queues.md) — owns the NQ machinery in full: the `0x28`-byte register block, the `nq_id` formula, the host-vs-device alloc rule, and the `ts_nq_init` init body this page's teardown unwinds
- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the `ndhal_topsp` slot, the `ts_per_device` scalar, and the V4-on-V3 layering that gives V4 its TopSP wiring for free
- [encd: the Device-Resident Descriptor Emitter](../collectives/encd-overview.md) — the device-side collectives driver that authors the TopSP instruction stream fed in over DMA, the producer side of this page's NQ
- [The cc_op_entry On-Device ISA](../collectives/cc-op-isa.md) — the on-device collective op encoding the TopSP sequences, the program whose completion these NQ messages mark
- [Reset Engine](reset.md) — the per-core reset/teardown path through which `nnq_destroy_nc` reaches `ts_nq_destroy_one`, the live route into this cell's destroy code
