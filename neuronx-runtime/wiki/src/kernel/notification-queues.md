# Notification Queue Engine

> *All `file:line` citations on this page are into the GPL-2.0 C source of **aws-neuronx-dkms 2.27.4.0** (`usr/src/aws-neuronx-2.27.4.0/`). The arch-neutral engine is `neuron_nq.c` (149 lines) / `neuron_nq.h` (93 lines); the per-generation geometry is `v2/notific.{c,h}` (125 / 52 lines) and `v3/notific.{c,h}` (132 / 65 lines); the register-programming DHAL methods are in `v2/neuron_dhal_v2.c` and `v3/neuron_dhal_v3.c`, all read verbatim. Boundary structs are cited at their definition site (`share/neuron_driver_shared.h`, `neuron_core.h`, `neuron_device.h`, `neuron_dhal.h`). The driver also ships as a stripped `.ko`, but the GPL C is authoritative — every offset below is a `#define` or a struct field, not a recovered binary offset.*
> *Evidence grade: **Confirmed (source-anchored)** — full C source, no decompilation. · Part III — Kernel Driver · [back to index](../index.md)*

## Abstract

The notification-queue (NQ) engine is the kernel half of Neuron's telemetry path. As the compute engines on a NeuronCore (and the orchestration engine on a TopSP) execute instructions, the hardware **produces** small fixed-size messages — completion markers, explicit `NOTIFY` events, error/throttle records, and DMA trace entries — by writing them into a **circular buffer**. That buffer lives in host memory (or, optionally, device DRAM); the hardware is the producer, and userspace is the consumer, draining the ring by `mmap`-ing it (`neuron_nq.c:6-14`). The kernel driver does **not** consume notifications and ships **no** in-kernel ISR for them: it only allocates the ring backing store, programs the per-NQ hardware base-address/size registers, hands an `mmap` offset back to userspace, and tears the ring down on reset or device removal.

The familiar frame is a **NIC completion queue**. A NIC CQ is a host-memory ring the device writes and the driver polls; the device knows the ring's physical base and length from registers the driver programmed at queue setup, and a head index it advances. The NQ is exactly this shape, with one structural difference: the kernel programs only three registers per ring — `BASE_ADDR_LO`, `BASE_ADDR_HI`, `F_SIZE` — and never touches the head. The `NOTIFIC_NQ_HEAD` register (`+0x0c` in each ring's register block) is read by the user-mapped consumer, not the kernel (`neuron_nq.h:53`). There is no kernel-side flow control, no interrupt coalescing, no doorbell: the entire head/tail dance is `mmap` + one hardware-advanced head register. This makes the kernel engine astonishingly thin — its whole job is *address arithmetic*: given a `(nc_id, engine, nq_type)` triple, compute which 0x28-byte register block in BAR0 owns this NQ, and write three words into it.

That arithmetic is split across three layers, and this page documents all three. The **arch-neutral engine** (`neuron_nq.c`) owns validation, memory allocation, the `nd->nq_mc[][]` ring-handle store, and the destroy loops; it touches no hardware directly. Every register access is delegated through the [DHAL](dhal-core.md) `ndhal_nq` / `ndhal_topsp` vtables to a **per-generation geometry layer** (`v{2,3}/notific.{c,h}`) that turns a triple into a BAR0 APB offset, and to **per-generation register writers** (`nnq_set_hwaddr_v{2,3}`, `ts_nq_set_hwaddr_v{2,3}`) that emit the three `reg_write32`s. There are two init *paths* — `nnq_init` for NeuronCore NQs and `ts_nq_init` for TopSP NQs (the latter dispatched entirely through the DHAL `ndhal_topsp.ts_nq_init` pointer) — that are near-identical bodies differing only in which register-base function they call and which handle array (`nq_mc` vs `ts_nq_mc`) they index.

For reimplementation, the contract is:

- **The 0x28-byte per-NQ register block** (`neuron_nq.h:13-53`): `BASE_ADDR_LO @+0x00`, `BASE_ADDR_HI @+0x04`, `F_SIZE @+0x08`, `HEAD @+0x0c`, stride `0x28` per NQ instance, accessed at `apb_base + 0x100 + index*0x28`. The kernel writes the first three; userspace reads HEAD.
- **The NQ-id layout**: `nq_id = nq_type * MAX_NQ_QUEUES(16) + engine_index`, and its inverse `nq_instance = (offset - 0x10c) / 0x28; nq_type = nq_instance / 16; nq_id = nq_instance % 16`.
- **The two init paths** (`nnq_init`, `ts_nq_init`) and the **memory-alloc + mmap-offset** rule: `mc_alloc_align` host-or-device, `nnq_set_hwaddr` programs registers, `*mmap_offset = nmmap_offset(mc)` for host rings or `-1` for device rings.
- **The realloc variant**: `force_alloc_mem` makes init replace an existing ring (alloc new, program, free old) instead of returning the cached handle.
- **The per-arch geometry deltas** (V2 vs V3): the BAR0 APB base computation for per-NC, per-SDMA-engine, and per-TopSP NOTIFIC blocks, and the V3-only `MAX_NQ_TYPE` asymmetry (5 vs 6).

| | |
|---|---|
| **Arch-neutral engine** | `neuron_nq.c` (149 lines), `neuron_nq.h` (93 lines) |
| **Per-arch geometry** | `v2/notific.{c,h}`, `v3/notific.{c,h}` |
| **Register writers (DHAL)** | `nnq_set_hwaddr_v2` (`v2/neuron_dhal_v2.c:427`), `nnq_set_hwaddr_v3` (`v3/neuron_dhal_v3.c:610`) |
| **Public NeuronCore entry** | `nnq_init` (`neuron_nq.c:72`) |
| **Public TopSP entry** | `ndhal->ndhal_topsp.ts_nq_init` → `ts_nq_init_v{2,3}` (`v2/neuron_dhal_v2.c:308`, `v3/neuron_dhal_v3.c:491`) |
| **Register block** | `NOTIFIC_NQ_SIZE = 0x28`; LO `+0x100`, HI `+0x104`, F_SIZE `+0x108`, HEAD `+0x10c` (`neuron_nq.h:13-53`) |
| **NQ-id formula** | `(nq_type * MAX_NQ_QUEUES) + engine_index` (`v2/neuron_dhal_v2.c:412`, `v3/neuron_dhal_v3.c:595`) |
| **NQ type count** | `MAX_NQ_TYPE = 6` (`neuron_core.h:86`); V3 ships 5 (`V3_MAX_NQ_TYPE`, drops THROTTLE) |
| **Handle store** | `nd->nq_mc[MAX_NC_PER_DEVICE][MAX_NQ_SUPPORTED]`, `nd->ts_nq_mc[...]` (`neuron_device.h:91,93`) |
| **IOCTL callers** | `NOTIFICATIONS_INIT_V1/_V2/_WITH_REALLOC_V2` (`neuron_cdev.c:1996/2014/2045`) |
| **Up boundary** | NQ/Sema IOCTL handlers → [ioctl-nq](ioctl-nq.md) |
| **Side boundary** | register writes, geometry → [DHAL core](dhal-core.md), [DHAL V2](dhal-v2.md), [DHAL V3](dhal-v3.md) |
| **Down boundary** | ring backing memory → [Memory Pool](mempool-handles.md); `mmap` offset → [cdev/mmap](cdev-mmap.md) |
| **Sibling** | TopSP destroy + semaphores → [TopSP](topsp.md) |

---

## 1. The Per-NQ Register Block

### Purpose

Every NQ a reimplementer must drive is addressed by one **0x28-byte register block** inside BAR0's APB region. The kernel never touches the ring data itself (that is host memory the hardware writes); it touches only this block, and only three of its four defined words. Get this layout and its stride right and the rest of the engine is bookkeeping.

### Layout

`neuron_nq.h:13-53` fixes the block. Each NQ instance `index` within a NOTIFIC region occupies `0x28` bytes starting at `apb_base + 0x100`. The four defined words:

| Field | Offset (start) | Per-instance address | Kernel writer | Reset |
|---|---|---|---|---|
| `BASE_ADDR_LO` | `0x100` | `apb_base + 0x100 + index*0x28` | `notific_write_nq_base_addr_lo` (`neuron_nq.h:19`) | `0x00000000` (`h:17`) |
| `BASE_ADDR_HI` | `0x104` | `apb_base + 0x104 + index*0x28` | `notific_write_nq_base_addr_hi` (`neuron_nq.h:32`) | `0x00000000` (`h:30`) |
| `F_SIZE` | `0x108` | `apb_base + 0x108 + index*0x28` | `notific_write_nq_f_size` (`neuron_nq.h:45`) | `0x00000000` (`h:43`) |
| `HEAD` | `0x10c` | `apb_base + 0x10c + index*0x28` | **none** — userspace reads (`neuron_nq.h:53`) | — |

The three writers are trivial `reg_write32` wrappers (`neuron_nq.h:19-51`); their entire content is the offset macro plus the write:

```c
// neuron_nq.h:19-25 — representative of all three writers.
static inline void notific_write_nq_base_addr_lo(void __iomem *base, size_t index, uint32_t value)
{
    const size_t offset = NOTIFIC_NQ_BASE_ADDR_LO_OFFSET(index);  // 0x100 + index*0x28 + 0
    reg_write32(base + offset, value);
}
```

The 64-bit ring physical address is split LO/HI; `F_SIZE` is the ring size in bytes (which the engine has already validated is a power of two). To program an NQ the kernel writes exactly HI, then LO, then F_SIZE (the ordering is fixed by `nnq_set_hwaddr`, §4); to *stop* an NQ it writes all three as zero (`nnq_halt`, §5.1).

> **NOTE —** the block stride is `0x28` (40 bytes) but only the first `0x10` (16 bytes) is defined here — LO/HI/F_SIZE/HEAD. The `0x10..0x27` span inside each instance's stride is not described in this source (MEDIUM confidence on its meaning; likely additional per-NQ control such as TAIL/threshold/enable, but no shipped `#define` names it). A reimplementer who only writes LO/HI/F_SIZE matches the driver exactly; do not invent the reserved fields.

> **QUIRK —** `NOTIFIC_NQ_SIZE` (`= 0x28`, `neuron_nq.h:13`) is commented "total size of the NQ register space" but it is used as the **per-instance stride** in every offset macro and every decoder (`(off - 0x10c) % NOTIFIC_NQ_SIZE`). The name is misleading: it is the spacing between consecutive NQ register blocks, not a one-shot region size. The number of blocks in a region is the region size divided by `0x28`.

### Considerations

`reg_write32` is a posted MMIO write into BAR0; there is no readback or fence in the NQ writers. The `apb_base` passed to these writers is `nd->npdev.bar0 + <relative offset>`, i.e. a `void __iomem *` already offset into the mapped BAR — the geometry layer (§3) computes the *relative* offset, the DHAL writer (§4) adds `bar0`.

---

## 2. NQ Identity, Types, and the Handle Store

### Purpose

Before any register is touched, the engine maps a caller's `(nc_id, engine_index, nq_type)` triple onto a single linear `nq_id`, validates it, and uses it to index a per-core array of ring handles. Reproducing this index math exactly is mandatory: it is both the storage key (`nq_mc[nc][nq_id]`) and the basis for the inverse register-offset decode (§7).

### The NQ-type enum

`enum NQ_TYPE` (`share/neuron_driver_shared.h:117-124`) enumerates the message classes the hardware produces:

| Type | Value | Meaning |
|---|---|---|
| `NQ_TYPE_TRACE` | 0 | Implicit notifications generated during execution |
| `NQ_TYPE_NOTIFY` | 1 | Explicit, via a `NOTIFY` instruction |
| `NQ_TYPE_EVENT` | 2 | Event set/clear |
| `NQ_TYPE_ERROR` | 3 | Error condition (infinity, NaN, …) |
| `NQ_TYPE_TRACE_DMA` | 4 | Implicit, generated by DMA transfers — **special-cased** in the register path |
| `NQ_TYPE_THROTTLE` | 5 | HAM throttling activity |
| `NQ_TYPE_MAX` | 6 | count |

`NQ_TYPE_TRACE_DMA` is the one type whose NOTIFIC block lives in a *per-SDMA-engine* region rather than the per-NC region; every other type shares the per-NC NOTIFIC block. This is the single branch in the register-programming path (§4).

### The id formula and the handle store

The linear id is `nq_id = nq_type * MAX_NQ_QUEUES + engine_index`, computed in the DHAL `nnq_get_nqid` method (`v2/neuron_dhal_v2.c:412`, `v3/neuron_dhal_v3.c:595`). With `MAX_NQ_QUEUES = 16`, type scales by 16 and engine occupies the low 4 bits; the inverse is `nq_type = nq_instance / 16`, `engine = nq_instance % 16`.

Sizing constants pin the array bounds (`neuron_core.h:86-89`):

| Constant | Value | Note |
|---|---|---|
| `MAX_NQ_TYPE` | 6 | arch-neutral loop bound (v1=4, v2=6) |
| `MAX_NQ_ENGINE` | 16 | engines/queues per core (v1=4) |
| `MAX_NQ_SUPPORTED` | 96 | `MAX_NQ_TYPE * MAX_NQ_ENGINE` — the per-core handle-array width |
| `V2_MAX_NQ_QUEUES` | 16 | `v2/address_map.h:73` |
| `V2_MAX_NQ_TYPE` | 6 | `v2/address_map.h:74` |
| `V3_MAX_NQ_QUEUES` | 16 | `v3/address_map.h:81` |
| `V3_MAX_NQ_TYPE` | **5** | `v3/address_map.h:82` — V3 drops `THROTTLE` |

Ring handles are stored in two flat per-device arrays (`neuron_device.h:91,93`):

```text
nd->nq_mc[MAX_NC_PER_DEVICE][MAX_NQ_SUPPORTED]      // NeuronCore NQs   (this page's nnq_* engine)
nd->ts_nq_mc[MAX_TS_PER_DEVICE][MAX_NQ_SUPPORTED]   // TopSP NQs        (ts_nq_* path, shared w/ TopSP page)
```

`nq_mc[nc_id][nq_id] == NULL` is the "no ring here" predicate that every halt/destroy/init path checks first.

> **GOTCHA —** the V3 type-count asymmetry is a live trap. The arch-neutral destroy loop `nnq_destroy_nc` iterates `nq_type` over `0 .. MAX_NQ_TYPE(6)` (`neuron_nq.c:119,128`) **even on V3**, where `V3_MAX_NQ_TYPE` is 5 (THROTTLE absent). This is harmless on the destroy side: `nnq_get_nqid_v3` happily returns a larger id for type 5, but `nq_mc[nc][nq_id] == NULL` for an NQ that was never created, so `nnq_halt`/`nnq_destroy` early-return 0 (`neuron_nq.c:42,63`). A reimplementer who tightens the destroy loop to `V3_MAX_NQ_TYPE` on V3 is equivalent; one who feeds type 5 into a V3 *init* must understand it will allocate a real NQ at an id past the V3 throttle gap. HIGH confidence (traced end-to-end).

---

## 3. Per-Arch Geometry — BAR0 APB Offsets

### Purpose

This is where a `(nc_id, engine)` becomes a byte offset into BAR0. The geometry layer is the only generation-aware code in the NQ subsystem; the engine and the register writers are arch-neutral and reach it through `notific_get_relative_offset_*`. There are three offset families — per-NC NOTIFIC, per-SDMA-engine NOTIFIC, per-TopSP NOTIFIC — and each generation lays them out differently.

### V2 geometry (`v2/notific.{c,h}`)

V2 has two SENGs (one per NeuronCore), each containing a 16-entry SDMA base table and one TPB-top NOTIFIC region; TopSPs live in a separate IOFAB region. All offsets are relative to BAR0 (the DHAL writer adds `bar0`).

```c
// notific_get_relative_offset_v2(nc_idx) — v2/notific.h:31-38.  Per-NC NOTIFIC block.
function notific_get_relative_offset_v2(nc_idx):
    base = (nc_idx == 0) ? V2_APB_SENG_0_RELBASE     // 0x0
                         : V2_APB_SENG_1_RELBASE;     // 0x700000
    return base + V2_PCIE_BAR0_APB_OFFSET             // 0x30000000
                + V2_APB_SENG_0_TPB_TOP_RELBASE       // 0x530000
                + V2_APB_SENG_0_TPB_TOP_NOTIFIC_RELBASE;  // 0x5000

// notific_get_relative_offset_sdma_v2(nc_id, eng_id) — v2/notific.c:28-33.  Per-SDMA NOTIFIC block.
function notific_get_relative_offset_sdma_v2(nc_id, eng_id):
    // seng_sdma_base[nc][eng] is a 2x16 table of absolute APB bases (v2/notific.c:12-25)
    seng_sdma_relbase = seng_sdma_base[nc_id][eng_id] - V2_APB_BASE;   // 0xffff0000000
    sdma_base = V2_PCIE_BAR0_APB_OFFSET + seng_sdma_relbase;
    return sdma_base + V2_APB_SENG_0_SDMA_0_NOTIFIC_RELBASE;            // 0x1000

// notific_get_relative_offset_topsp_v2(ts_idx) — v2/notific.h:42-49.  Per-TopSP NOTIFIC block.
function notific_get_relative_offset_topsp_v2(ts_idx):
    off = V2_PCIE_BAR0_APB_OFFSET + V2_APB_IOFAB_RELBASE          // 0xe00000
        + V2_APB_IOFAB_TOP_SP_0_RELBASE                          // 0x0
        + V2_APB_IOFAB_TOP_SP_0_NOTIFIC_RELBASE;                 // 0x4000
    return off + V2_APB_IOFAB_TOP_SP_0_SIZE * ts_idx;            // stride 0x40000
```

The V2 SDMA base table (`seng_sdma_base[2][16]`, `v2/notific.c:12-25`) holds 32 absolute APB addresses; consecutive engines are `0x7000` apart (`V2_APB_SENG_0_SDMA_0_BASE = 0xffff0400000`, `..._1_BASE = 0xffff0407000`). The per-SDMA block size used by the decoder is computed live as `seng_sdma_base[nc][1] - seng_sdma_base[nc][0]` = `0x7000`.

### V3 geometry (`v3/notific.{c,h}`)

V3 reorganizes around four SENGs across dies, with the SDMA NOTIFIC living in a per-engine "misc" sub-block. The per-NC and per-TopSP offsets gain die-distance terms (`V3_PCIE_BAR0_APB_IO_DIST * (idx / per_die)`); the SDMA path computes a misc base from per-SENG SDMA tables.

```c
// get_sdma_misc_base(nc_id, eng_id) — v3/notific.c:12-23.  V3-only helper.
function get_sdma_misc_base(nc_id, eng_id):
    seng_id     = nc_id / V3_NC_PER_SENG;                          // 2 NCs per SENG
    seng_eng_id = eng_id + (nc_id % V3_NC_PER_SENG) * V3_DMA_ENG_PER_NC;  // 16 eng/NC
    return sdma_bases[seng_id]                                     // {SE_0..SE_3}_SDMA_0_BASE
         + seng_eng_id * V3_APB_SDMA_DIST                          // 0x100000 per engine
         + V3_APB_SDMA_MISC_OFFSET;                                // 0x40000

// notific_get_relative_offset_sdma_v3(nc_id, eng_id) — v3/notific.c:26-40.
function notific_get_relative_offset_sdma_v3(nc_id, eng_id):
    seng_id = nc_id / V3_NC_PER_SENG;
    misc_relbase = get_sdma_misc_base(nc_id, eng_id) - se_bases[seng_id];
    return V3_PCIE_BAR0_APB_SE_0_OFFSET                            // 0x80000000
         + V3_PCIE_BAR0_APB_SE_DIST * seng_id
         + misc_relbase
         + V3_APB_SENG_0_SDMA_0_NOTIFIC_RELBASE;                   // 0x1000

// notific_get_relative_offset_v3(nc_idx) — v3/notific.h:31-48.  Per-NC NOTIFIC block.
function notific_get_relative_offset_v3(nc_idx):
    seng_id = nc_idx / V3_NC_PER_SENG;
    off = se_relbases[seng_id]                                     // {IO_0_SE_0,..}_RELBASE
        + V3_PCIE_BAR0_APB_IO_0_OFFSET                             // 0x0
        + V3_PCIE_BAR0_APB_IO_DIST * (nc_idx / V3_NC_PER_DIE)      // die distance
        + V3_APB_IO_0_SE_0_TPB_0_SIZE * (nc_idx % V3_NC_PER_SENG)  // 0x180000 per NC-in-SENG
        + V3_APB_IO_0_SE_0_TPB_TOP_RELBASE                         // 0x0
        + V3_APB_IO_0_SE_0_TPB_TOP_NOTIFIC_RELBASE;               // 0x1000
    return off;

// notific_get_relative_offset_topsp_v3(ts_idx) — v3/notific.h:52-61.  Per-TopSP NOTIFIC block.
function notific_get_relative_offset_topsp_v3(ts_idx):
    return V3_PCIE_BAR0_APB_IO_0_OFFSET
         + V3_PCIE_BAR0_APB_IO_DIST * (ts_idx / V3_TS_PER_DIE)
         + V3_APB_IO_0_USER_IO_RELBASE                             // 0x6800000
         + V3_APB_IO_0_USER_IO_TOP_SP_0_RELBASE                    // 0x200000
         + V3_APB_IO_0_USER_IO_TOP_SP_0_SIZE * (ts_idx % V3_TS_PER_DIE)  // 0x40000 stride
         + V3_APB_IO_0_USER_IO_TOP_SP_0_NOTIFIC_RELBASE;          // 0x0
```

### The geometry delta, by dimension

The two generations differ along a small set of axes; the table describes the *shape* of the difference rather than dumping both constant sets.

| Dimension | V2 | V3 | Source |
|---|---|---|---|
| SENGs / NC NOTIFIC regions | 2 (`V2_MMAP_TPB_COUNT`) | 8 NC slots over 4 SENGs (`V3_MMAP_TPB_COUNT=8`, `V3_SENG_PER_DEVICE`) | `v2:93`, `v3:170,47` |
| TopSPs per device | 6 (`V2_TS_PER_DEVICE`) | `V3_TS_PER_DEVICE` (2 per NC, `V3_TS_PER_NC=2`) | `v2:56`, `v3:71,69` |
| Per-NC NOTIFIC base | SENG relbase + TPB_TOP + `0x5000` | SE relbase + die-dist + per-NC size + `0x1000` | `v2/notific.h:31`, `v3/notific.h:31` |
| Per-SDMA NOTIFIC base | 16-entry abs table, stride `0x7000`, + `0x1000` | computed misc base, `SDMA_DIST 0x100000` + `MISC 0x40000` + `0x1000` | `v2/notific.c:28`, `v3/notific.c:26` |
| Per-TopSP NOTIFIC base | IOFAB + `0x4000`, stride `0x40000` | USER_IO + `0x200000`, stride `0x40000`, +die-dist | `v2/notific.h:42`, `v3/notific.h:52` |
| TPB NOTIFIC region size | `0x1000` (`V2_APB_SENG_0_TPB_NOTIFIC_SIZE`) | `0x1000` (`V3_APB_IO_0_SE_0_TPB_NOTIFIC_SIZE`) | `v2:180`, `v3:203` |
| Die-distance term | absent (single die) | `V3_PCIE_BAR0_APB_IO_DIST * (idx / per_die)` | — / `v3/notific.h:43,55` |
| NQ types | 6 (incl. THROTTLE) | 5 (THROTTLE dropped) | `v2:74`, `v3:82` |

> **QUIRK —** V2 derives the per-SDMA NOTIFIC base by *subtracting* a global APB base from an absolute address table (`seng_sdma_base[nc][eng] - V2_APB_BASE`, `v2/notific.c:30`), then re-adding the BAR0 PCIe offset. V3 never materializes absolute addresses that way; it computes a relative misc base directly from per-SENG SDMA roots (`get_sdma_misc_base`, `v3/notific.c:12`). The two reach the same kind of answer (a BAR0-relative NOTIFIC offset) by opposite arithmetic. Do not assume V3 has an equivalent of the V2 absolute base table — it does not.

---

## 4. Register Programming — `nnq_set_hwaddr`

### Purpose

This is the only function that touches NQ hardware on the init path. It resolves the geometry (§3) to an `apb_base`, splits the ring physical address, and emits the three `reg_write32`s. The arch-neutral engine reaches it only through `ndhal->ndhal_nq.nnq_set_hwaddr` (`neuron_nq.c:96`); the per-generation bodies are identical except for which `notific_get_relative_offset_*_v{2,3}` they call.

### Algorithm

```c
// nnq_set_hwaddr_v2(nd, nc_id, index, nq_type, size, queue_pa) — v2/neuron_dhal_v2.c:427-444.
// (v3 body is structurally identical with _v3 geometry — v3/neuron_dhal_v3.c:610-627)
function nnq_set_hwaddr_v2(nd, nc_id, index, nq_type, size, queue_pa):
    if nq_type == NQ_TYPE_TRACE_DMA:                              // :430  DMA-trace special case
        apb_base = nd->npdev.bar0 + notific_get_relative_offset_sdma_v2(nc_id, index)
        index = 0     // per-SDMA base already selects the engine; write to block index 0
    else:
        apb_base = nd->npdev.bar0 + notific_get_relative_offset_v2(nc_id)  // per-NC NOTIFIC

    low  = queue_pa & 0xffffffff
    high = queue_pa >> 32

    notific_write_nq_base_addr_hi(apb_base, index, high)         // :440  +0x104 + index*0x28
    notific_write_nq_base_addr_lo(apb_base, index, low)          // :441  +0x100 + index*0x28
    notific_write_nq_f_size  (apb_base, index, size)            // :442  +0x108 + index*0x28
```

Two facts a reimplementer must preserve. First, for `NQ_TYPE_TRACE_DMA` the engine *index* is folded into the **base** (per-SDMA-engine offset) and the in-block `index` is forced to `0` (`v2/neuron_dhal_v2.c:432`, `v3/neuron_dhal_v3.c:614`) — every other type writes block `index` within a single per-NC base. Second, the write order is **HI, LO, F_SIZE**: the high address word lands before the low word, and `F_SIZE` (which arms the ring) is written last. Writing `size = 0` with `queue_pa = 0` is the *halt* (§5.1).

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `nnq_set_hwaddr_v2` | `v2/neuron_dhal_v2.c:427` | Resolve geometry + write HI/LO/F_SIZE for a NeuronCore NQ | CERTAIN |
| `nnq_set_hwaddr_v3` | `v3/neuron_dhal_v3.c:610` | Same, V3 geometry | CERTAIN |
| `nnq_get_nqid_v2` / `_v3` | `v2:412` / `v3:595` | `(nq_type * MAX_NQ_QUEUES) + index` | CERTAIN |
| `notific_get_relative_offset_v{2,3}` | `v2/notific.h:31`, `v3/notific.h:31` | Per-NC NOTIFIC BAR0 offset | CERTAIN |
| `notific_get_relative_offset_sdma_v{2,3}` | `v2/notific.c:28`, `v3/notific.c:26` | Per-SDMA-engine NOTIFIC BAR0 offset | CERTAIN |
| `notific_write_nq_*` | `neuron_nq.h:19/32/45` | `reg_write32` into the 0x28 block | CERTAIN |

### Considerations

The DHAL writer is the boundary between the arch-neutral engine and BAR0. `nd->npdev.bar0` is the mapped APB BAR; the geometry function returns a BAR-relative offset; the inline writer adds the per-instance `0x100 + index*0x28`. There is no error return — the writers are `void` and posted; a bad triple cannot fail here because it was validated upstream (`nq_id < MAX_NQ_SUPPORTED`) before `set_hwaddr` is reached.

---

## 5. NeuronCore Init Path — `nnq_init`

### Purpose

`nnq_init` is the sole public entry for creating a NeuronCore NQ ring. It validates the request, allocates the backing memory (host or device), programs the hardware via the DHAL writer, records the handle, and returns the `mmap` offset userspace needs to map the ring. It is reached from three IOCTL handlers that differ only in their `force_alloc_mem` and host/device arguments.

### Entry Point

```text
ioctl NEURON_IOCTL_NOTIFICATIONS_INIT_V1   (deprecated)  ── neuron_cdev.c:3333
  └─ ncdev_nc_nq_init_deprecated (neuron_cdev.c:1996)     ── force_alloc_mem=false, host=true (hard-coded)
ioctl NEURON_IOCTL_NOTIFICATIONS_INIT_V2                 ── neuron_cdev.c:3335
  └─ ncdev_nc_nq_init_libnrt (neuron_cdev.c:2014)         ── NEURON_CORE → nnq_init; TOPSP → ts_nq_init
ioctl NEURON_IOCTL_NOTIFICATIONS_INIT_WITH_REALLOC_V2    ── neuron_cdev.c:3337
  └─ ncdev_nc_nq_init_with_realloc_libnrt (neuron_cdev.c:2045)  ── passes arg.force_alloc_mem through
       └─ nnq_init (neuron_nq.c:72)
            ├─ ndhal_nq.nnq_get_nqid       (DHAL)          ── triple → nq_id
            ├─ mc_alloc_align              (K-MEMPOOL)      ── host or device ring backing
            ├─ ndhal_nq.nnq_set_hwaddr     (DHAL, §4)       ── program HI/LO/F_SIZE
            └─ nmmap_offset                (K-MMAP)         ── host ring → mmap offset; device → -1
```

### Algorithm

```c
// nnq_init(nd, nc_id, eng_index, nq_type, size, on_host_memory, dram_channel,
//          dram_region, force_alloc_mem, *nq_mc, *mmap_offset) — neuron_nq.c:72-110.  PUBLIC.
function nnq_init(...):
    if size & (size - 1):                                        // :77  power-of-2 check
        pr_err("notification ring size must be power of 2")      // :78
        return -EINVAL
    if nd == NULL || nc_id >= nc_per_device                      // :81  device + core validity
       || ((1 << nc_id) & dev_nc_map) == 0:                      // :82  core present in this device?
        return -EINVAL

    nq_id = ndhal->ndhal_nq.nnq_get_nqid(nd, nc_id, eng_index, nq_type)  // :85  (type*16)+eng
    if nq_id >= MAX_NQ_SUPPORTED: return -EINVAL                 // :86  (= 96)

    mc = nd->nq_mc[nc_id][nq_id]                                 // :89  cached handle (may be NULL)
    if mc == NULL || force_alloc_mem:                            // :90  alloc unless reusing
        _mc = NULL
        ret = mc_alloc_align(nd, MC_LIFESPAN_DEVICE, size,
                  on_host_memory ? 0 : size,                     // :92  align = size for device rings
                  on_host_memory ? MEM_LOC_HOST : MEM_LOC_DEVICE,
                  dram_channel, dram_region, nc_id,
                  on_host_memory ? NEURON_MEMALLOC_TYPE_NOTIFICATION_HOST
                                 : NEURON_MEMALLOC_TYPE_NOTIFICATION_DEVICE, &_mc)  // :93
        if ret: return ret                                       // alloc failed → propagate
        ndhal->ndhal_nq.nnq_set_hwaddr(nd, nc_id, eng_index, nq_type, size, _mc->pa)  // :96  PROGRAM HW
        nd->nq_mc[nc_id][nq_id] = _mc                            // :97  record new handle
        if mc:                                                   // :98  realloc: free the OLD ring
            mc_free(&mc)                                         // :99
        mc = _mc                                                 // :101

    if mc->mem_location == MEM_LOC_HOST:                         // :103
        *mmap_offset = nmmap_offset(mc)                          // :104  host ring → user-mappable offset
    else:
        *mmap_offset = -1                                        // :106  device ring → not mappable
    *nq_mc = mc                                                  // :107  hand handle back to IOCTL
    return 0
```

The validation order is: power-of-two `size` first (the only check with a `pr_err`), then device/core, then id bound. `mc_alloc_align`'s alignment argument is `0` for host rings and `size` for device rings (`neuron_nq.c:92`) — device-DRAM NQs are size-aligned so the hardware base lands on a natural boundary; host rings ride the mempool's default alignment. The memory-type tag (`NEURON_MEMALLOC_TYPE_NOTIFICATION_HOST/_DEVICE`, `share/neuron_driver_shared.h:239,250`) lets the mempool account NQ rings separately.

The returned `*mmap_offset` for a host ring is `nmmap_offset(mc)`, which is simply `mc->pa` (`neuron_mmap.c:237-239`) — the ring's physical address doubles as the `mmap` page-offset cookie the userspace consumer feeds back to `mmap(2)`. For a device-DRAM ring there is no host mapping; the offset is `-1` and the consumer drains it by device DMA instead.

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `nnq_init` | `neuron_nq.c:72` | Validate + alloc + program + return mmap offset | CERTAIN |
| `ncdev_nc_nq_init_deprecated` | `neuron_cdev.c:1996` | V1 IOCTL: host-only, no realloc | CERTAIN |
| `ncdev_nc_nq_init_libnrt` | `neuron_cdev.c:2014` | V2 IOCTL: NEURON_CORE→`nnq_init`, TOPSP→`ts_nq_init` | CERTAIN |
| `ncdev_nc_nq_init_with_realloc_libnrt` | `neuron_cdev.c:2045` | Realloc IOCTL: passes `force_alloc_mem` | CERTAIN |
| `mc_alloc_align` | K-MEMPOOL | Allocate ring backing (host or device DRAM) | HIGH (boundary) |
| `nmmap_offset` | `neuron_mmap.c:237` | `return mc->pa` — the mmap offset cookie | CERTAIN |

### Considerations

> **NOTE —** `NEURON_IOCTL_NOTIFICATIONS_DESTROY_V1` is an inline `return 0` in the IOCTL dispatch (`neuron_cdev.c:3338-3339`) — there is **no** per-NQ destroy IOCTL. NQ teardown is bulk-only, driven by reset or device removal (§5.1). A reimplementer must not expect userspace to free individual NQs; the kernel reclaims them all when the core resets.

> **QUIRK —** the V1 (deprecated) handler hard-codes `on_host_memory = true` and `dram_channel = dram_region = 0` while passing `force_alloc_mem = false` (`neuron_cdev.c:2006-2007`); only the V2 and realloc handlers honor the request's host/device and channel/region fields. The realloc handler is the *only* path that can set `force_alloc_mem = true` and thus trigger the alloc-new-then-free-old branch (`neuron_nq.c:98-100`).

---

### 5.1 Teardown — halt, destroy, and the per-core loop

#### Purpose

NQ teardown is a two-phase per-core sweep: **halt** every NQ (zero its hardware registers so the producer stops writing), wait one millisecond for in-flight writes to drain, then **destroy** (free the backing memory). The whole sweep is bulk — there is no targeted single-NQ destroy on the NeuronCore path.

#### Algorithm

```c
// nnq_halt(nd, nc_id, eng_index, nq_type) — neuron_nq.c:29-48.  static.  Stop HW writes, keep memory.
function nnq_halt(...):
    if nd==NULL || nc_id>=nc_per_device || !(dev_nc_map & (1<<nc_id)): return -EINVAL  // :33
    nq_id = ndhal_nq.nnq_get_nqid(nd, nc_id, eng_index, nq_type)     // :37
    if nq_id >= MAX_NQ_SUPPORTED: return -EINVAL                     // :38
    if nd->nq_mc[nc_id][nq_id] == NULL: return 0                     // :41  no ring → nothing to halt
    ndhal_nq.nnq_set_hwaddr(nd, nc_id, eng_index, nq_type, 0, 0)     // :45  size=0, pa=0 → HW stops
    return 0                                                         // memory is NOT freed here

// nnq_destroy(nd, nc_id, eng_index, nq_type) — neuron_nq.c:50-69.  static.  Free backing memory.
function nnq_destroy(...):
    ... same validation as nnq_halt (:54-60) ...
    if nd->nq_mc[nc_id][nq_id] == NULL: return 0                     // :62
    mc_free(&nd->nq_mc[nc_id][nq_id])                               // :66  free + NULL the handle
    return 0

// nnq_destroy_nc(nd, nc_id) — neuron_nq.c:112-137.  PUBLIC.  Tear down one core's NQs + its TopSPs.
function nnq_destroy_nc(nd, nc_id):
    for eng_index in 0 .. MAX_NQ_ENGINE-1:        // 16
        for nq_type in 0 .. MAX_NQ_TYPE-1:        // 6  (see V3 GOTCHA §2)
            nnq_halt(nd, nc_id, eng_index, nq_type)   // :120  zero all HW regs first
    msleep(1)                                     // :125  let halted queues drain
    for eng_index in 0 .. MAX_NQ_ENGINE-1:
        for nq_type in 0 .. MAX_NQ_TYPE-1:
            nnq_destroy(nd, nc_id, eng_index, nq_type)  // :129  then free memory

    ts_per_nc = ts_per_device / nc_per_device                       // :133
    for ts_id in [nc_id*ts_per_nc .. (nc_id+1)*ts_per_nc):          // :134  this core's TopSPs
        ndhal->ndhal_topsp.ts_nq_destroy_one(nd, ts_id)            // :135  → TopSP page

// nnq_destroy_all(nd) — neuron_nq.c:139-148.  PUBLIC.  Every present core.
function nnq_destroy_all(nd):
    for nc_id in 0 .. nc_per_device-1:
        if !(dev_nc_map & (1 << nc_id)): continue                  // :144  skip absent cores
        nnq_destroy_nc(nd, nc_id)
```

The **halt-all-then-destroy-all** ordering matters: every NQ on the core is silenced before *any* memory is freed, and a `msleep(1)` (`neuron_nq.c:125`) sits between the two loops to let the hardware finish writes that were already in flight when it was halted. Freeing a ring while the producer could still write it would be a use-after-free of host memory the hardware still owns. `nnq_destroy_nc` also tears down the **TopSP** NQs for the core after its NeuronCore NQs, via the DHAL `ts_nq_destroy_one` (the TopSP page owns that body).

| Caller | Source | Path |
|---|---|---|
| `nnq_destroy_nc` | `neuron_reset.c:281` | Core reset (per-core) |
| `nnq_destroy_all` | `neuron_pci.c:192` | Device removal (all cores) |

> **GOTCHA —** `nnq_halt` only zeroes hardware registers (`set_hwaddr(...,0,0)`); it does **not** free `nq_mc[][]`. `nnq_destroy` only frees memory; it does **not** re-zero registers (it trusts the prior halt). Calling `nnq_destroy` without a preceding `nnq_halt` would `mc_free` a ring whose hardware base register still points at it — the producer could DMA into freed host memory. The two-loop structure in `nnq_destroy_nc` is what guarantees halt-before-free; a reimplementer must never collapse the loops.

---

## 6. TopSP Init Path — `ts_nq_init` (via DHAL)

### Purpose

TopSP NQs are the orchestration engine's telemetry rings (collectives / all-reduce sequencing). The body is a near-clone of `nnq_init`, but it lives **inside the DHAL** (`ts_nq_init_v{2,3}`) rather than in the arch-neutral engine, and it indexes `ts_nq_mc[ts_id][nq_id]` instead of `nq_mc[nc_id][nq_id]`. The public symbol is the function pointer `ndhal->ndhal_topsp.ts_nq_init`; the V2/V3 IOCTL handlers call it directly when `nq_dev_type == NQ_DEVICE_TYPE_TOPSP` (`neuron_cdev.c:2029,2060`).

### Entry Point

```text
ioctl NOTIFICATIONS_INIT_V2 / _WITH_REALLOC_V2
  └─ ncdev_nc_nq_init_libnrt / _with_realloc_libnrt
       if arg.nq_dev_type == NQ_DEVICE_TYPE_TOPSP:                 ── neuron_cdev.c:2027/2059
         └─ ndhal->ndhal_topsp.ts_nq_init (DHAL pointer)
              → ts_nq_init_v2 (v2/neuron_dhal_v2.c:308)            ── or ts_nq_init_v3 (v3/...:491)
                   ├─ ts_nq_get_nqid_v2     (v2:258)               ── (type*16)+eng, same formula
                   ├─ mc_alloc_align        (K-MEMPOOL)            ── ring backing
                   ├─ ts_nq_set_hwaddr_v2   (v2:275)               ── per-TopSP NOTIFIC offset + 3 writes
                   └─ nmmap_offset          (K-MMAP)
```

### Algorithm

```c
// ts_nq_init_v2(nd, ts_id, eng_index, nq_type, size, on_host_memory, dram_channel,
//               dram_region, force_alloc_mem, *nq_mc, *mmap_offset) — v2/neuron_dhal_v2.c:308-347.
// (v3: v3/neuron_dhal_v3.c:491-530, identical body with V3 geometry + V3_TS_PER_NC)
function ts_nq_init_v2(...):
    if size & (size - 1):                                          // :313  power-of-2 (same pr_err)
        pr_err("notification ring size must be power of 2"); return -EINVAL
    if nd == NULL || ts_id >= ts_per_device: return -EINVAL        // :318  TopSP-id validity (not nc_id!)

    nq_id = ts_nq_get_nqid_v2(nd, eng_index, nq_type)              // :321  (type*16)+eng
    if nq_id >= MAX_NQ_SUPPORTED: return -EINVAL                   // :322

    mc = nd->ts_nq_mc[ts_id][nq_id]                                // :325  TopSP handle array
    if mc == NULL || force_alloc_mem:                              // :326
        nc_id = ts_id / (V2_TS_PER_DEVICE / V2_NC_PER_DEVICE)      // :329  owning core for mempool accounting
        ret = mc_alloc_align(nd, MC_LIFESPAN_DEVICE, size, on_host_memory?0:size,
                  on_host_memory?MEM_LOC_HOST:MEM_LOC_DEVICE, dram_channel, dram_region, nc_id,
                  on_host_memory?NEURON_MEMALLOC_TYPE_NOTIFICATION_HOST
                                :NEURON_MEMALLOC_TYPE_NOTIFICATION_DEVICE, &_mc)  // :330
        if ret: return ret
        ts_nq_set_hwaddr_v2(nd, ts_id, eng_index, nq_type, size, _mc->pa)  // :333  per-TopSP geometry
        nd->ts_nq_mc[ts_id][nq_id] = _mc                          // :334
        if mc: mc_free(&mc)                                        // :335-337  realloc: free old
        mc = _mc
    if mc->mem_location == MEM_LOC_HOST: *mmap_offset = nmmap_offset(mc)  // :340
    else:                                *mmap_offset = -1         // :343
    *nq_mc = mc                                                    // :344
    return 0

// ts_nq_set_hwaddr_v2(nd, ts_id, index, nq_type, size, queue_pa) — v2/neuron_dhal_v2.c:275-289.
function ts_nq_set_hwaddr_v2(...):
    apb_base = nd->npdev.bar0 + notific_get_relative_offset_topsp_v2(ts_id)  // :281  ALWAYS per-TopSP
    notific_write_nq_base_addr_hi(apb_base, index, queue_pa >> 32)           // :286
    notific_write_nq_base_addr_lo(apb_base, index, queue_pa & 0xffffffff)    // :287
    notific_write_nq_f_size  (apb_base, index, size)                        // :288
```

The structural differences from `nnq_init`, all that a reimplementer must reproduce:

1. **Validity check is `ts_id >= ts_per_device`** (`v2:318`), not the `nc_id` + `dev_nc_map` pair — TopSPs are addressed by a flat TopSP id.
2. **Handle array is `ts_nq_mc[ts_id][nq_id]`** (`neuron_device.h:93`), a separate per-TopSP store.
3. **No `NQ_TYPE_TRACE_DMA` branch** in `ts_nq_set_hwaddr` — TopSPs have no SDMA NOTIFIC region; the base is *always* the per-TopSP offset (`v2:281`, `v3:464`). This is the one place the TopSP writer is *simpler* than the NeuronCore writer.
4. **Owning-core derivation** (`nc_id = ts_id / (TS_PER_DEVICE / NC_PER_DEVICE)` on V2, `ts_id / V3_TS_PER_NC` on V3) is computed purely to pass a core id to the mempool for accounting (`v2:329`, `v3:504`).

### Function Map

| Function | Source | Role | Confidence |
|---|---|---|---|
| `ts_nq_init_v2` / `_v3` | `v2/neuron_dhal_v2.c:308`, `v3/neuron_dhal_v3.c:491` | TopSP NQ create (validate/alloc/program/mmap) | CERTAIN |
| `ts_nq_set_hwaddr_v2` / `_v3` | `v2:275`, `v3:458` | Per-TopSP NOTIFIC offset + HI/LO/F_SIZE | CERTAIN |
| `ts_nq_get_nqid_v2` / `_v3` | `v2:258`, `v3:441` | `(nq_type * MAX_NQ_QUEUES) + index` | CERTAIN |
| `ts_nq_destroy` | `neuron_topsp.c:42` | Halt + free one TopSP NQ (TopSP page) | CERTAIN |
| `ts_nq_destroy_one_v2` / `_v3` | `v2:355`, `v3:539` | Loop-destroy all NQs of one TopSP | CERTAIN |
| DHAL pointer assignment | `v2:1404-1407`, `v3:1896-1899` | Wires `ts_nq_init/_destroy_one/_get_nqid/_set_hwaddr` into `ndhal_topsp` | CERTAIN |

### Considerations

> **NOTE —** `ts_nq_init` has no body in the arch-neutral tree; the public `neuron_topsp.h:27` prototype is satisfied **only** through the DHAL function pointer. There is no `int ts_nq_init(...)` definition outside `ts_nq_init_v{2,3}` — every caller routes through `ndhal->ndhal_topsp.ts_nq_init` (`neuron_cdev.c:2029,2060`). A reimplementer wiring a single-arch build must still install the pointer; calling a bare `ts_nq_init` symbol will not link.

> **QUIRK —** the `ts_nq_destroy` *teardown* path (`neuron_topsp.c:42-60`) is in the arch-neutral `neuron_topsp.c`, but the *init* path is in the DHAL. The asymmetry is because destroy needs only the DHAL `set_hwaddr` (to zero the registers) plus a `mc_free`, whereas init needs the full per-arch geometry inline. `ts_nq_destroy` does its own halt (`set_hwaddr(...,0,0)`, `neuron_topsp.c:56`) **and** free in one function — unlike the NeuronCore path which splits halt and destroy across two loops (§5.1). For a single TopSP NQ there is no in-flight-drain `msleep`; the per-core sweep's `msleep(1)` covers the TopSPs too, since `nnq_destroy_nc` calls `ts_nq_destroy_one` *after* its own drain.

---

## 7. The Inverse Decode — Exported but Uncalled

### Purpose

`notific_decode_nq_head_reg_access_v{2,3}` invert the geometry: given a raw BAR0 APB offset (e.g. one a userspace head-register access would carry), they recover `(nc_id, nq_type, instance, is_top_sp)`. They are the mathematical inverse of §3+§2 and are exported (non-`static`), but **no in-tree caller exists** in this DKMS source — a fact a reimplementer should weigh before porting them.

### Algorithm

```c
// notific_decode_nq_head_reg_access_v2(offset, *nc_id, *nq_type, *instance, *is_top_sp)
// v2/notific.c:89-125.  EXPORTED.  Three-pass range scan; v3 is the structural twin (v3/notific.c:96-131).
function notific_decode_nq_head_reg_access_v2(offset, ...):
    *is_top_sp = false
    // pass 1: SDMA NOTIFIC ranges → nq_type = NQ_TYPE_TRACE_DMA
    for i in 0 .. V2_MMAP_TPB_COUNT-1:                            // :95
        start = seng_sdma_base[i][0]; blk = seng_sdma_base[i][1]-start
        end   = seng_sdma_base[i][V2_NUM_DMA_ENGINES_PER_TPB-1] + blk
        if start <= offset < end: *nc_id=i; return decode_sdma(offset,i,...)  // :101
    // pass 2: per-NC NOTIFIC ranges
    for i in 0 .. V2_MMAP_TPB_COUNT-1:                            // :105
        start = notific_get_relative_offset_v2(i); end = start + V2_APB_SENG_0_TPB_NOTIFIC_SIZE
        if start <= offset < end: *nc_id=i; return decode_nc(offset,i,...)    // :110
    // pass 3: per-TopSP NOTIFIC ranges
    for i in 0 .. V2_TS_PER_DEVICE-1:                             // :114
        start = notific_get_relative_offset_topsp_v2(i); end = start + V2_APB_IOFAB_TOP_SP_0_SIZE
        if start <= offset < end: *nc_id=i; *is_top_sp=true; return decode_topsp(offset,i,...)  // :120
    return -EINVAL

// each leaf decoder — e.g. notific_decode_nc_nq_head_reg_access (v2/notific.c:55-70):
//   nq_relative = offset - region_start
//   if nq_relative < NOTIFIC_NQ_HEAD_OFFSET(0x10c): return -EINVAL   // must be a HEAD-register access
//   nq_relative -= 0x10c
//   if nq_relative % NOTIFIC_NQ_SIZE(0x28): return -EINVAL           // must land on a block boundary
//   nq_instance = nq_relative / 0x28
//   *nq_type = nq_instance / MAX_NQ_QUEUES; *nq_id = nq_instance % MAX_NQ_QUEUES
```

Every leaf requires the offset to be a **HEAD-register** access: `offset >= region_start + 0x10c` and `(offset - region_start - 0x10c) % 0x28 == 0`. That is the tell about intended use — these decoders exist to interpret a userspace access to the NQ **head** register (`+0x10c`), the one register the kernel never writes. The SDMA leaf additionally hard-sets `nq_type = NQ_TYPE_TRACE_DMA` and derives `nq_id` from the per-engine block index (`v2/notific.c:42-43`).

### Considerations

> **NOTE —** a tree-wide search finds exactly four references to each decode entry point: the two `.c` definitions and the two `.h` prototypes — **zero callers** (HIGH confidence on "no in-tree caller"; MEDIUM on intended consumer). The naming (`..._nq_head_reg_access`) and the HEAD-register gate strongly suggest an `mmap` head-register mediation path — where the kernel would trap a userspace access to the NQ head register, decode which NQ it targets, and act — but no such trap is shipped in this build unit. The functions are dead here, reserved for a path compiled elsewhere or in a different driver version. A reimplementer building only the producer-programs / consumer-drains model documented above does **not** need them; one reproducing a head-mediation path does, and should treat the geometry inversion as the canonical reference for it.

---

## Related Components

| Name | Relationship |
|---|---|
| `struct ndhal_nq` | `{nnq_get_nqid, nnq_set_hwaddr}` vtable the engine dispatches through (`neuron_dhal.h:77-79`) |
| `struct ndhal_topsp` | `{ts_nq_init, ts_nq_destroy_one, ts_nq_get_nqid, ts_nq_set_hwaddr}` (`neuron_dhal.h:63-69`) |
| `enum NQ_TYPE` | The six message classes; `TRACE_DMA` is the special-cased one (`share/neuron_driver_shared.h:117`) |
| `enum NQ_DEVICE_TYPE` | `{NEURON_CORE, TOPSP}` — the IOCTL discriminator that picks `nnq_init` vs `ts_nq_init` (`share:111`) |
| `nd->nq_mc` / `nd->ts_nq_mc` | Per-core / per-TopSP ring-handle arrays (`neuron_device.h:91,93`) |
| `notific_get_relative_offset_*` | The geometry functions that turn a triple into a BAR0 offset (`v{2,3}/notific.{c,h}`) |

## Cross-References

- [TopSP Notification Path](topsp.md) — the sibling that owns `ts_nq_destroy` teardown, TopSP semaphores/events, and the `ts_nq_mc` lifecycle this page's `ts_nq_init` populates
- [DHAL Core (ndhal Vtable-of-Vtables)](dhal-core.md) — the `ndhal_nq` / `ndhal_topsp` dispatch tables every register access on this page routes through
- [DHAL V2 (Trn1 / Inf2)](dhal-v2.md) — `nnq_set_hwaddr_v2`, `ts_nq_init_v2`, and the V2 NOTIFIC geometry constants
- [DHAL V3 (Trn2)](dhal-v3.md) — `nnq_set_hwaddr_v3`, `ts_nq_init_v3`, the per-engine SDMA misc-base math, and the 5-vs-6 type asymmetry
- [DMA Rings and H2T Queues](dma-rings.md) — the sibling kernel ring abstraction; the NQ `TRACE_DMA` type is produced by the same SDMA engines whose copy rings that page documents
- [NQ and Semaphore IOCTL Handlers](ioctl-nq.md) — the `NOTIFICATIONS_INIT_V1/_V2/_WITH_REALLOC_V2` argument parsing that calls `nnq_init` / `ts_nq_init`
- [Memory Pool and MC Handle Table](mempool-handles.md) — `mc_alloc_align` / `mc_free` that back and reclaim every NQ ring, and the `NEURON_MEMALLOC_TYPE_NOTIFICATION_*` accounting tags
- [Char Device, fops and mmap](cdev-mmap.md) — how the host-ring `mmap_offset` (`= mc->pa`) becomes a userspace mapping of the ring
- [Per-Arch Device Layer: Notification and INTC HAL](../runtime/arch-notification.md) — the runtime-side **consumer**: how the user library maps the ring and reads the HEAD register the kernel never touches
- [Overview: the Three Trace Producers](../trace/overview.md) — what the messages in these rings *mean*: the producer side that emits TRACE / NOTIFY / TRACE_DMA records the runtime later decodes
