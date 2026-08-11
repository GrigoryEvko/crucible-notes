# On-Chip Working-Memory Regions (SBUF/PSUM/scratch)

This page is the **region/aperture-level** map of every on-chip working-memory block a
GPSIMD Q7 core, the fixed-function engines, the SDMA/DGE descriptor machinery, and the
collectives operate in inside one NeuronCore TPB — and the **SoC → Q7 NX-local 32-bit
window mapping** that lets the 32-bit Xtensa cores reach those 64-bit SoC regions. It
covers byte-exact SoC bases / sizes / partition geometry for the **State Buffer (SBUF)**,
**PSUM**, **`STATE_BUF_SCRATCH_RAM`** (the on-chip SBUF scratch), **`DGE_MEMORY`**, and
the **per-core Q7 DataRAM**, then reconciles them against the device-side NX-local window
TLB (`neuron_translate`), the device allocators, and the per-core SDMA apertures.

The **bank internals** — the 16 SBUF / 32 PSUM ECC banks, the `(partition, byte_offset)`
↔ 29-bit linear-address decode (`TPB_PARTITION_ADDR_MASK = 0x1fffffff`), the two-stage
arbiter, the `axi2sram` transpose, the matmul → PSUM datapath — are **not repeated here**;
they live in [On-Chip State-Buffer (SBUF) + PSUM Bank Model](sbuf-psum-banks.md). This
page is the layer *above* that: where each region sits in the SoC decode window and how
the Q7 reaches (or cannot reach) it.

Every base/size below is grounded in the shipped, RTL-generated address-map artifacts and
verified numerically: the Cayman `address_map_flat.yaml` (34,858 nodes) and its
`address_map.h` `#define` twin, the Maverick `al_address_map_db.{pkl,json}`, plus the
device-side Xtensa-disassembled `neuron_translate` / `dram_addr_to_soc_addr` /
`init_neuron_dataram_allocator` bodies. Confidence is tagged
**HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED** per row.

> **KEYSTONE (HIGH / OBSERVED — carried from [§838](sbuf-psum-banks.md)).** The Q7 cores
> reach **SBUF** as an AXI bus master through a pinned NX window onto the `0x2000000000`
> SoC block. **PSUM is unreachable from the Q7**: `PSUM_BUF` sits in a *disjoint*
> top-level SoC block based at `0x2800000000` — there is **no pinned NX window onto PSUM
> and no `axi2sram`-equivalent PSUM bridge**, so an AXI master has no path to it at all.
> This page proves the disjointness at the region level ([§1](#1-the-tpb_0-on-chip-memory-region-table), [§3](#3-psum--the-disjoint-block-q7-cannot-reach)).
> See also [Keystone Facts](../orientation/keystone-facts.md).

Related pages: the SBUF/PSUM **bank model** in [SBUF + PSUM Bank Model](sbuf-psum-banks.md);
the **SoC ↔ Q7 translation windows** in [The SoC ↔ Q7 Translation Windows](../control/address/soc-q7-translation-windows.md);
the **device allocators** in [Device Memory Allocators](../abi/device-allocators.md);
the **unified SoC memory map** in [The Unified GPSIMD / Cayman SoC Memory Map](../control/address/unified-soc-memory-map.md);
the on-core LSU view in [LSU & Memory](../uarch/lsu-memory.md).

---

## 1. The TPB_0 on-chip memory region table

A single physical TPB carries **seven** working-memory regions inside its 32.0625 GiB
(`0x804000000`) SoC decode window (container `TPB_0` base `0x2000000000`). In SoC address
order, byte-exact from `address_map_flat.yaml` L867–L909 (the file cosmetically zero-pads
bases to 15 hex digits) and re-checked against the `CAYMAN_TPB_0_*_{BASE,SIZE}` `#define`
macros in `cayman/address_map.h` L3472–L3502 — **both views are byte-identical**:

| region | SoC base | size | human | top-level block | what it is | confidence |
| --- | --- | --- | --- | --- | --- | --- |
| `TPB_0_STATE_BUF` | `0x2000000000` | `0x2000000` | 32 MiB | `0x2000000000` | the SBUF — systolic-array on-chip state buffer (pure SRAM, no CSR) | HIGH/OBS |
| `TPB_0_TPB_RESERVED10` | `0x2002000000` | `0x2000000` | 32 MiB | `0x2000000000` | decode pad (the upper half of the SBUF NX window, [§4](#4-the-soc--q7-nx-local-window-mapping)) | HIGH/OBS |
| `TPB_0_STATE_BUF_SCRATCH_RAM` | `0x2004000000` | `0x2000000` | **32 MiB** | `0x2000000000` | on-chip SBUF scratch SRAM (pure SRAM, no CSR) | HIGH/OBS |
| `TPB_0_TPB_RESERVED11` | `0x2006000000` | `0x3A000000` | 928 MiB | `0x2000000000` | decode pad | HIGH/OBS |
| `TPB_0_DGE_MEMORY` | `0x2040000000` | `0x40000000` | 1 GiB | `0x2000000000` | descriptor-generation-engine descriptor RAM ([§5](#5-dge_memory--the-1-gib-descriptor-ram)) | HIGH/OBS |
| `TPB_0_TPB_RESERVED12` | `0x2080000000` | `0x780000000` | 30 GiB | `0x2000000000` | large decode pad | HIGH/OBS |
| `TPB_0_TPB_RESERVED_SBUF` | `0x2800000000` | `0x2000000` | 32 MiB | `0x2800000000` | the SBUF decode aperture / **cluster pseudo-base** the POOL engines tile off | HIGH/OBS row; MED "pure aperture" |
| `TPB_0_PSUM_BUF` | `0x2802000000` | `0x400000` | 4 MiB | `0x2800000000` | PE-array accumulator buffer (pure SRAM, no CSR) | HIGH/OBS |
| `TPB_0_EVT_SEM` | `0x2802700000` | `0x100000` | 1 MiB | `0x2800000000` | event/semaphore aperture (nested; boundary marker, [§6](#6-region-boundaries--evt_sem-and-the-per-core-dram-neighbourhood)) | HIGH/OBS |
| `TPB_0_POOL_Q7_CORE{n}_DRAM` | `0x2803180000 + n·0x100000` | `0x40000` ea | 256 KiB ea | `0x2800000000` | the 8 on-core Q7 DataRAMs ([§7](#7-per-core-q7-dataram--three-levels-reconciled)) | HIGH/OBS |

**Contiguity (Python verifier).** The first eight rows tile `[0x2000000000, 0x2802400000)`
with **zero gaps and zero overlaps** — every `base == previous_end` through `PSUM_BUF`'s
end. `STATE_BUF(32) + RESERVED10(32) + SCRATCH(32) + RESERVED11(928 MiB) = 0x2040000000`
= `DGE_MEMORY` base; `…+ DGE(1 GiB) + RESERVED12(30 GiB) = 0x2800000000` = the
`RESERVED_SBUF` block base. [HIGH / OBSERVED — arithmetic verified.]

**No data-region carries a CSR.** None of `STATE_BUF` / `SCRATCH_RAM` / `DGE_MEMORY` /
`PSUM_BUF` has a `json:` binding in the map — they are **pure memory / decode leaves**.
The SBUF/PSUM *control* register files live on a separate control-plane path (see
[§838 §7](sbuf-psum-banks.md#7-access-asymmetry--q7-via-axi-aperture-vs-engines-via-arbiter-clients)),
and the per-bank ECC register files (16 SBUF + 32 PSUM `erg_parity_model` blocks) live
under the PEB path — both documented in the bank-model page, not here. [HIGH / OBSERVED.]

> **CORRECTION (vs the page-spec / orientation shorthand).** The on-chip
> `STATE_BUF_SCRATCH_RAM` SoC region is **32 MiB** (`0x2000000`), **not 64 MiB** —
> verified byte-exact in *both* `address_map_flat.yaml` L869 and
> `cayman/address_map.h:3482`. The "64 MiB" figure is the size of the **SBUF NX-pinned
> *window*** (TLB record 3, mask `0xfffffffffc000000`), which maps the *first* 64 MiB of
> the TPB window = `STATE_BUF` (32) + `RESERVED10` (32) and ends **exactly** at the
> scratch base `0x2004000000`. The window is 64 MiB; the scratch *region* is 32 MiB and
> sits *just past* the window's far edge ([§4](#4-the-soc--q7-nx-local-window-mapping)).
> Do not conflate the window size with the region size.

> **QUIRK — two distinct "scratch" regions; never conflate them.**
> **(A)** `STATE_BUF_SCRATCH_RAM` @ SoC `0x2004000000`, 32 MiB — **on-chip SRAM** inside
> the TPB, adjacent to SBUF, at a *fixed* SoC address. **(B)** `hbm_scratch`
> (`extended_isa::sdk::hbm_scratch`) — the **HBM-resident** scratch heap, *runtime* SoC
> base (it lives in the HBM region `0x0..`), mapped to NX `0x84000000` via the pinned
> 64 MiB TLB record 4, and the backing store for `neuron_hbm_allocate`. (A) is on-chip
> at a static address; (B) is off-chip at a runtime address reached through a pinned NX
> window. The **per-core DataRAM** ([§7](#7-per-core-q7-dataram--three-levels-reconciled))
> is a *third*, separate scratch family. [HIGH / OBSERVED — separate symbols / regions.]

---

## 2. SBUF — region geometry (region-level)

`TPB_0_STATE_BUF` is **SoC `0x2000000000`, 32 MiB**, pure SRAM — the first region in the
TPB window (its base *is* the container base). It is the on-chip state buffer the systolic
PE array, the activation/pooling engines, the DVE, the SDMA/DGE, and the GPSIMD Q7 cores
read/write tensors from. [HIGH / OBSERVED.]

The partition geometry — **128 partitions × 256 KiB stride (192 KiB active on sunda)**,
the 29-bit `PartitionOffset` decode, and the **16 SBUF ECC banks** (8 clusters × 2 halves)
— is the bank-model page's subject and is reproduced there byte-exact
([§838 §1–§3](sbuf-psum-banks.md#1-geometry-constants--the-per-generation-machine-model)).
At the region level the only facts this page adds are the **two SBUF apertures**:

- the **physical SRAM data region** at SoC `0x2000000000` (the 32 MiB above), and
- a **second SBUF decode aperture** at SoC `0x2800000000` (`TPB_0_TPB_RESERVED_SBUF`,
  32 MiB) — the **cluster pseudo-base** the POOL Q7-cluster IP and the engine sub-blocks
  (PSUM/ACT/PE/SP/DVE/POOL) are laid out *relative to*. This second aperture is a
  decode-reserved SBUF window, not a second 32 MiB of physical SRAM. [HIGH/OBSERVED two
  distinct rows; **MED** that the second is a pure decode aperture — the map carries no
  node-type field, the reading is from the name + the fact the engines tile off it.]

The Q7's path to SBUF is the AXI/`axi2sram` aperture detailed in
[§838 §7.3–§7.4](sbuf-psum-banks.md#73-the-q7--gpsimd-cores--the-sbuf-axi-aperture); its
NX-local view is record 3 of the window TLB ([§4](#4-the-soc--q7-nx-local-window-mapping)).

---

## 3. PSUM — the disjoint block Q7 cannot reach

`TPB_0_PSUM_BUF` is **SoC `0x2802000000`, 4 MiB**, pure SRAM — the PE-array partial-sum
accumulator the matmul systolic array writes into and ACT/POOL read out of. Its
accumulator-bank structure (**32 ECC banks** = 4 clusters × 8; 8 HW banks of 2048 B per
partition) is the bank-model page's subject
([§838 §4](sbuf-psum-banks.md#4-psum-banking--128-partitions--4-cluster--8--32-ecc-banks-8-bankspartition)).

The region-level fact this page owns is the **disjointness keystone**:

> **KEYSTONE PROOF (HIGH / OBSERVED).** `STATE_BUF` lives in the `0x2000000000`-based
> top-level block; `PSUM_BUF` lives in the **separate `0x2800000000`-based block** (the
> `RESERVED_SBUF` cluster-pseudo-base aperture + 0x2000000). The two block bases are
> `0x800000000` (= 32 GiB) apart. PSUM's *only* other SoC presence is the PEB control
> plane (`PEB_…_PSUM_CLUSTER{0..3}_ERG_CSR_{0..7}` ECC CSRs) — not a data aperture. There
> is **no pinned NX window onto PSUM** (the TLB's two pins are `sbuf` + `hbm_scratch`,
> [§4](#4-the-soc--q7-nx-local-window-mapping)) and **no `axi2sram`-equivalent PSUM
> bridge**, so an AXI bus master (the Q7) has *no physical path* to PSUM. The compiler's
> "GPSIMD/customop operands must be in SBUF, never PSUM" rule is therefore not a policy —
> it is the absence of a PSUM aperture. See [Keystone Facts](../orientation/keystone-facts.md).

> **NOTE — Q7 software access to PSUM is impossible, not merely unmapped.** Even the
> dynamic 16 MiB TLB slots cannot help: a dynamic window can be *re-pointed* at any SoC
> region, but PSUM is read/written only by the PE/ACT/POOL **datapath ports**, which are
> not AXI-bus-visible — there is no SoC-bus master read/write port onto the PSUM SRAM for
> a window to target. The Q7 has no route at all. [MED — the dynamic-route-also-fails
> reading is INFERRED from the keystone's "no AXI bridge".]

---

## 4. The SoC → Q7 NX-local window mapping

The Q7 cores address everything through a **32-bit NX-local** space. The mapping from the
57-bit Cayman SoC physical address into that 32-bit space is the device-side
`neuron_translate` **5-entry window TLB** (`_translation_ctx`, 168 B = 5 × 32-byte
`_map_record` + a `next_alloc` cursor), decoded instruction-by-instruction from
`translation.o`. (Full byte-exact decode in
[The SoC ↔ Q7 Translation Windows](../control/address/soc-q7-translation-windows.md).)
For the on-chip working-memory regions of this page:

| NX window base | SoC region | granule | TLB record | reach |
| --- | --- | --- | --- | --- |
| `0x80000000` | SBUF (32 MiB data + 32 MiB `RESERVED10`) | 64 MiB (mask `0xfffffffffc000000`) | **3 (PINNED)** | the Q7's view of the state buffer; never re-pointed |
| `0x84000000` | `hbm_scratch` (HBM, runtime SoC base) | 64 MiB (mask `0xfffffffffc000000`) | **4 (PINNED)** | the HBM-scratch heap — **not** the on-chip SBUF scratch |
| `0x07000000` / `0x09000000` / `0x0a000000` | general HBM tensor data (+ the HBM stack, on-chip SBUF scratch, on demand) | 16 MiB (mask `0xffffffffff000000`) | **0,1,2 (DYNAMIC)** | round-robin `%3`; a 4th distinct 16 MiB region evicts the oldest slot |
| `[0x80000, 0x90000)` | per-core Q7 DataRAM (each core's own DRAM) | direct | (no TLB) | **DIRECT** NX deref — the local pointer *is* the address |

**The translate arithmetic (byte-exact, HIGH / OBSERVED).** On a tag hit the result is a
pure add; the SoC tag is `ptr & mask`, the offset is the low bits:

```c
// neuron_translate(void* ctx, uint64_t soc_ptr) -> void* (32-bit NX address).
// Decoded instruction-by-instruction from translation.o (ncore2gp Xtensa disasm).
// _map_record { uint64 ptr@0; uint32 window@8; uint64 mask@0x10; uint64 reg_loc@0x18 }.

void* soc_to_q7_window(_translation_ctx* ctx, uint64_t soc_ptr) {
    // PHASE A — search the 5 records for a tag hit (unrolled 5x in the binary):
    for (int i = 0; i < 5; ++i)
        if ((soc_ptr & ctx->records[i].mask) == ctx->records[i].ptr)
            // PHASE B — HIT: window base + in-window offset (low 24/26 bits).
            return (void*)(ctx->records[i].window
                           + (uint32_t)(soc_ptr & ~ctx->records[i].mask));

    // PHASE C — MISS: refill ONE of the 3 dynamic 16 MiB slots (next_alloc % 3),
    //   reprogram the HW window register at records[na].reg_loc, return immediately.
    //   The refill granule is hardwired 16 MiB (mask 0xFF000000); the 64 MiB pins
    //   (records 3,4) are HIT-only and never refilled.
    uint8_t na = ctx->next_alloc;                 // victim slot
    uint32_t off    = (uint32_t)soc_ptr & 0x00FFFFFF;   // 16 MiB granule
    uint32_t prefix = (uint32_t)soc_ptr & 0xFF000000;   // window-aligned SoC base (lo)
    ctx->records[na].ptr = (soc_ptr & 0xFFFFFFFFFF000000ull);   // install new TAG
    *(volatile uint32_t*)(ctx->records[na].reg_loc + 4) = (uint32_t)(soc_ptr >> 32);
    *(volatile uint32_t*)(ctx->records[na].reg_loc + 0) = prefix;  // program HW window
    ctx->next_alloc = (uint8_t)((na + 1) % 3);    // round-robin over slots {0,1,2}
    return (void*)(ctx->records[na].window + off);
}
```

**SBUF window arithmetic (HIGH / OBSERVED).** The SBUF SoC base `0x2000000000` is already
64 MiB-aligned (`0x2000000000 & ~0x3FFFFFF == 0x2000000000`), so the 64 MiB window
`[NX 0x80000000, 0x84000000)` maps SoC `[0x2000000000, 0x2004000000)` = `STATE_BUF`
(32 MiB) + `RESERVED10` (32 MiB) = exactly the first 64 MiB of the TPB window. The
on-chip scratch `STATE_BUF_SCRATCH_RAM` begins at SoC `0x2004000000` = the window's far
edge, so **it is *not* inside the pinned SBUF window** and would ride a dynamic 16 MiB
slot for Q7 software access. [HIGH / OBSERVED for the geometry; **MED** that the scratch
rides a dynamic slot rather than a second pin — the TLB lists only the two `sbuf`/
`hbm_scratch` pins, so the dynamic route is the only one left.]

> **NOTE — three scratch/memory families, one more time, by NX route.**
> on-chip SBUF scratch (SoC `0x2004000000`) → **dynamic 16 MiB** window (past the pin);
> HBM scratch (runtime SoC, HBM) → **pinned 64 MiB** window @ NX `0x84000000`;
> per-core DataRAM (Q7 DRAM) → **direct NX-local** `[0x80000, 0x90000)`. [HIGH / OBS the
> three routes; the SBUF-scratch dynamic-window route is MED, as above.]

> **GOTCHA — the device translate has no bounds guard.** On-core `neuron_translate`
> unconditionally evicts a slot and programs the HW window for *whatever* 64-bit pointer
> it is given — no null check, no range check. An out-of-region SoC pointer yields an
> out-of-region window *silently*. Region-legality is enforced **host-side**, by the
> `cayman_memory_bounds` table (8 × uint64; one entry is the TPB_0 base
> `0x0000020000000000` / size `0x18000`). A Q7 op trusts that its SBUF/scratch/DataRAM
> SoC pointer is legal; the host validated it. [HIGH/OBS device-no-guard; **MED**
> host-table-is-the-guard.]

---

## 5. DGE_MEMORY — the 1 GiB descriptor RAM

`TPB_0_DGE_MEMORY` is **SoC `0x2040000000`, 1 GiB** (`0x40000000`) — the **largest single
on-chip-addressed region** in the TPB window, the descriptor RAM the **Descriptor-
Generation Engine** stages SDMA/collective descriptor rings in. It is a single undivided
leaf: `rg -c 'TPB_0_DGE_MEMORY'` over `address_map_flat.yaml` returns **exactly one node**
— no ECC/ERG CSR and no `LOCAL_REG` is bound to it. [HIGH / OBSERVED; cross-checked vs
`cayman/address_map.h:3488` `DGE_MEMORY_BASE 0x00002040000000` / `_SIZE 0x40000000`.]

> **NOTE — hardware descriptor RAM ≠ firmware control structures.** The DGE *firmware*
> control structures — the priority-class map (up to 5 classes) and the 4-slot
> `dge_mailbox` — live in the **per-core Q7 DataRAM** (at `core+0x20` dram_base, mailbox
> at `+0x28`), i.e. inside the 256 KiB `POOL_Q7_CORE{n}_DRAM` ([§7](#7-per-core-q7-dataram--three-levels-reconciled)),
> **not** inside this 1 GiB hardware region. The 1 GiB `DGE_MEMORY` is the descriptor-
> *staging* RAM; the per-core DRAM holds the host↔Q7 control mailbox and priority map. The
> two are functionally related (both serve descriptor generation) but are **separate
> address families**. [HIGH for the distinction; **MED** on the "staging" purpose — the
> 1 GiB leaf is undivided in the map, so the internal layout is name-inferred.]

---

## 6. Region boundaries — EVT_SEM and the per-core DRAM neighbourhood

`TPB_0_EVT_SEM` @ SoC `0x2802700000`, 1 MiB, is the TPB **event/semaphore** aperture (a
nested container): `EVT_SEM_EVENT @0x2802700000` (`0x400`) + four 1 KiB semaphore-op
windows `SEMAPHORE_{READ,SET,INC,DEC}` at `0x2802701000`/`…1400`/`…1800`/`…1C00`. This is
the synchronization aperture the engines and collectives use to sequence work. It is a
**boundary marker** here — the full decode belongs to the event/semaphore unit's page; it
is listed only to fix the region boundary between the working-memory blocks (this page)
and the sync unit. [HIGH / OBSERVED location; full decode elsewhere.]

---

## 7. Per-core Q7 DataRAM — three levels reconciled

The "DataRAM" the GPSIMD cores use exists at **three distinct address levels** that must
not be conflated — reconciling them is the key result of this section.

**Level 1 — the physical on-core DRAM (SoC region).** `TPB_0_POOL_Q7_CORE{n}_DRAM`:
**256 KiB each** (`0x40000`), 8 cores, **1 MiB slot pitch** (`0x100000`).
`CORE0_DRAM @ SoC 0x2803180000`; `CORE{n}_DRAM = 0x2803180000 + n·0x100000` →
`{0x2803180000, 0x2803280000, …, 0x2803880000}`. (The interleaved `RESERVED*` / IRAM rows
between cores set the 1 MiB pitch.) [HIGH / OBSERVED — `address_map_flat.yaml` L949–L977.]

**Level 2 — the Q7's 32-bit NX-local view of its own DataRAM.** `dram_addr_to_soc_addr`
(`data_transfer.o`) asserts the local DataRAM window is `[0x80000, 0x90000)` — a **64 KiB**
NX-local aperture (guard `(dram_addr & 0xFFFF0000) == 0x80000`, assert string
`data_transfer.cpp:160`). A Q7 core sees its own DataRAM at NX-local offset `0x80000`,
64 KiB wide. [HIGH / OBSERVED disasm + assert.]

**Level 3 — the allocatable heap pool carved inside that window.**
`init_neuron_dataram_allocator` carves the xmem DataRAM heap pool at
`buffer = DSM + 0x3200`, `size = 1 << 14 = 0x4000 = 16 KiB` (`DSM = *data_scratch_map` =
the on-core DataRAM base). So the allocatable DataRAM heap is **16 KiB**, inside the
64 KiB NX window, inside the 256 KiB physical core DRAM. (The libc heap pool sits just
below at `DSM+0xa00`, 10 KiB.) [HIGH / OBSERVED disasm; full allocator in
[Device Memory Allocators](../abi/device-allocators.md).]

### 7.1 The per-core SDMA aperture — how SDMA reaches a core's DataRAM

The SDMA engine masters the SoC bus and **cannot see a core's 32-bit local DataRAM window
directly**, so `dram_addr_to_soc_addr` computes a per-core SoC alias of that local buffer.
`cpu_id = rsr.prid` (asserts `cpu_id < 8`); `window_index = 2·cpu_id + 9`:

```c
// dram_addr_to_soc_addr(uint32 dram_addr) -> uint64 SoC bus address. [HIGH/OBSERVED]
// Pre: (dram_addr & 0xFFFF0000) == 0x80000   (local DataRAM window [0x80000,0x90000)).
uint64_t dram_addr_to_soc_addr(uint32_t dram_addr) {
    uint32_t cpu_id       = rsr_prid();              // asserts cpu_id < 8
    uint32_t window_index = 2u * cpu_id + 9u;        // -> {9,11,13,15,17,19,21,23}
    uint64_t soc_base     = cat_u32(ureg[0x2C], ureg[0x28]);   // runtime SoC_BASE (hi:lo)
    return soc_base + ((uint64_t)window_index << 16) + (uint64_t)(dram_addr - 0x80000);
}
```

Verified per-core aperture offsets (`window_index << 16`):

| cpu_id | idx | aperture offset |
| --- | --- | --- |
| 0 | 9 | `0x90000` |
| 1 | 11 | `0xB0000` |
| 2 | 13 | `0xD0000` |
| 3 | 15 | `0xF0000` |
| 4 | 17 | `0x110000` |
| 5 | 19 | `0x130000` |
| 6 | 21 | `0x150000` |
| 7 | 23 | `0x170000` |

Each core gets its own **64 KiB SoC aperture** (`idx·0x10000` apart), anchored at a runtime
`SoC_BASE` read from two fixed low words (`ureg[0x28]`/`[0x2C]`). [HIGH / OBSERVED — the
PRID-switch `movi` arms + the aperture math.]

> **NOTE — aperture (64 KiB) vs physical core DRAM (256 KiB).** The SDMA aperture exposes
> the **low 64 KiB** of each core's 256 KiB physical DRAM — exactly matching the local
> window `[0x80000, 0x90000)` being 64 KiB. The upper 192 KiB is reachable by the core's
> own load/store (the DGE `dge_mailbox` / priority-map at `core+0x20`/`+0x28` live there,
> [§5](#5-dge_memory--the-1-gib-descriptor-ram)) but is **not** in the 64 KiB SDMA
> aperture. The aperture and the `POOL_Q7_CORE{n}_DRAM` region are the **same physical
> SRAM seen through two address paths** — the local NX window for the core, the SoC
> aperture for the SDMA master. The two odd-spaced indices leave the even indices for a
> sibling (paired IRAM / engine). [HIGH the sizes; **MED** the same-SRAM equivalence — the
> runtime `SoC_BASE` that would equate them byte-exact is not in the static artifacts.]

> **NOTE — DataRAM needs no translate.** Unlike HBM, the per-core DataRAM is *not* routed
> through `neuron_translate`: the NX pointer **is** the local address. `dram_addr_to_soc_addr`
> is the *other* direction (local DataRAM NX → SoC), used only so the SDMA master can
> address the local buffer. Contrast HBM tensor data, which goes through the dynamic /
> pinned NX windows ([§4](#4-the-soc--q7-nx-local-window-mapping)). [HIGH / OBSERVED.]

---

## 8. Per-TPB replication + die mapping

The 8 physical TPBs (`TPB_0..7`) each carry a full copy of every region. Per-TPB
`STATE_BUF` / `PSUM_BUF` bases (grepped byte-exact from `address_map_flat.yaml`):

| TPB | `STATE_BUF` base | `PSUM_BUF` base |
| --- | --- | --- |
| 0 | `0x2000000000` | `0x2802000000` |
| 1 | `0x3000000000` | `0x3802000000` |
| 2 | `0x6000000000` | `0x6802000000` |
| 3 | `0x7000000000` | `0x7802000000` |
| 4 | `0x802000000000` | `0x802802000000` |
| 5 | `0x803000000000` | `0x803802000000` |
| 6 | `0x806000000000` | `0x806802000000` |
| 7 | `0x807000000000` | `0x807802000000` |

**Die mapping.** `TPB_{4..7} = TPB_{0..3} | bit47` (`0x800000000000`, the Cayman DIE bit) —
TPBs 0–3 on die0, 4–7 on die1. **Within-die stride** is `0x1000000000` (64 GiB) for the
0→1 and 2→3 steps and `0x3000000000` for 1→2: the 4 die-local TPBs interleave with the
HBM stacks / PREPROC engines in the LOCAL space rather than packing contiguously (each
TPB container is 32.0625 GiB, so a 64 GiB stride leaves a 32 GiB gap). [HIGH / OBSERVED.]

---

## 9. Cross-generation differences

Three RTL address maps ship — **cayman** (`cayman-arch-regs`) and **mariana / mariana_plus**
(customop-lib arch-headers) — plus the Maverick `al_address_map_db.{pkl,json}`. Generation
mapping: **CAYMAN = NC-v3** (sunda = NC-v2, mariana = NC-v4, maverick = NC-v5).

**Byte-identical across cayman / mariana / mariana_plus** (grep-verified): `STATE_BUF`
`0x2000000000`/32 MiB, `STATE_BUF_SCRATCH_RAM` `0x2004000000`/32 MiB, `DGE_MEMORY`
`0x2040000000`/1 GiB, `TPB_RESERVED_SBUF` `0x2800000000`/32 MiB, `PSUM_BUF` `0x2802000000`/
4 MiB, container `TPB_0` `0x2000000000`/32.0625 GiB. The only cross-gen memory change in
this neighbourhood is the **POOL Xtensa-NX DRAM** (`POOL_NX_DRAM`) doubling 64 → 128 KiB
(cayman `0x10000` vs mariana(+) `0x20000`) — that is POOL front-matter, **not** the tensor
working-memory regions. [HIGH / OBSERVED.]

> **NOTE — v5 / MAVERICK divergence (header/db-OBSERVED; interiors INFERRED).** Maverick
> reorganizes the map into a `USER_INT / SENG` hierarchy and diverges on the SBUF/PSUM
> region sizes:
> - **SBUF region = 128 MiB** (`0x8000000`): `MAVERICK_USER_INT_SENG_0_n_SIZE 0x8000000`
>   in `maverick/address_map/user_int/seng/tpb/sbuf.h`, corroborated by
>   `al_address_map_db`: `USER_INT_SENG_0_TPB_0_SBUF_STATE_BUF base=0x2000000000 size=0x8000000`.
>   This matches the ISA-header `STATE_BUF_SZ = 0x8000000` (see [§838 §1](sbuf-psum-banks.md#1-geometry-constants--the-per-generation-machine-model)).
>   [HIGH / OBSERVED, maverick `sbuf.h` + pkl/json.]
> - **PSUM is sized out / absent.** The ISA header carries **`PSUM_BUF_SZ = 0x0`**, and the
>   maverick `address_map/` tree and `address_map.h` contain **no PSUM region at all**
>   (`rg PSUM` returns zero matches in both). [HIGH / OBSERVED the absence.]
>
> Because v5 is header / db-OBSERVED only (no byte-grounded firmware image), the maverick
> region **interiors** (bank wiring, partition stride) are flagged **INFERRED** per
> [§838](sbuf-psum-banks.md). v2–v4 region bases/sizes are byte-grounded.

---

## 10. Confidence ledger

**HIGH / OBSERVED** (byte-exact in `address_map_flat.yaml` + `cayman/address_map.h` +
the Xtensa-disassembled device objects):

- The 10-row TPB_0 region table ([§1](#1-the-tpb_0-on-chip-memory-region-table)): YAML and
  header `#define` byte-identical; gap-free contiguity verified.
- The SBUF / PSUM disjoint top-level blocks (`0x2000000000` vs `0x2800000000`,
  `0x800000000` apart) — the keystone ([§3](#3-psum--the-disjoint-block-q7-cannot-reach)).
- `DGE_MEMORY` single undivided 1 GiB leaf @ `0x2040000000` ([§5](#5-dge_memory--the-1-gib-descriptor-ram)).
- The 5-entry window TLB: 2 pinned 64 MiB (sbuf @ NX `0x80000000`, hbm_scratch @ NX
  `0x84000000`) + 3 dynamic 16 MiB; HIT = `window + (ptr & ~mask)`; MISS = refill one of 3
  dynamic slots round-robin `%3` + program HW reg ([§4](#4-the-soc--q7-nx-local-window-mapping)).
- The SBUF 64 MiB window ↔ SoC `[0x2000000000, 0x2004000000)` arithmetic; scratch begins at
  the window's far edge.
- Per-core DataRAM 3 levels: physical 256 KiB @ `0x2803180000 + n·0x100000` (1 MiB pitch);
  NX-local `[0x80000, 0x90000)` 64 KiB; heap pool `DSM+0x3200` 16 KiB; SDMA aperture
  `idx = 2·cpu_id + 9`, offset `idx<<16` ([§7](#7-per-core-q7-dataram--three-levels-reconciled)).
- 8-TPB replication + die mapping (`TPB_{4..7} = TPB_{0..3} | bit47`) ([§8](#8-per-tpb-replication--die-mapping)).
- Cross-gen: regions byte-identical cayman/mariana/mariana_plus; maverick SBUF = 128 MiB,
  PSUM absent (`PSUM_BUF_SZ = 0`) ([§9](#9-cross-generation-differences)).

**MED** — `TPB_RESERVED_SBUF` as pure decode aperture vs physical SRAM (no node-type
field); the SBUF-scratch dynamic-window route (the TLB pins only sbuf+hbm_scratch); the
per-core SoC aperture as the same physical SRAM as `POOL_Q7_CORE{n}_DRAM` (runtime
`SoC_BASE` not in the static map); `DGE_MEMORY` "descriptor staging" purpose
(name-inferred). **LOW** — TLB record-3 SoC tag read via two MMIO loads whose exact
register is not separately decoded (the NX window *value* `0x80000000` is OBSERVED, the
SoC tag is runtime); `SoC_BASE` (`ureg[0x28]/[0x2C]`) is a runtime UREG, not a documented
CSR field.

**CORRECTION issued** — `STATE_BUF_SCRATCH_RAM` on-chip region is **32 MiB**, not 64 MiB;
the 64 MiB is the SBUF NX-pinned *window* (record 3), which maps `STATE_BUF + RESERVED10`
and ends at the scratch base. Verified `address_map_flat.yaml:869` +
`cayman/address_map.h:3482`.
