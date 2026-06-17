# Memory Hierarchy, BAR Layout and State Buffer

> *Runtime addresses, symbols, BAR0 region offsets, HBM bases and on-core SRAM sizes on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (`libnrt.so.2.31.24.0`, build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64, **not** stripped, DWARF present; `.text`/`.rodata`/`.data` VMA == file offset, `.bss` is `NOBITS`). Kernel `file:line` citations are into the GPL-2.0 source of `aws-neuronx-dkms 2.27.4.0`, shipped under `/usr/src/aws-neuronx-2.27.4.0/` (read directly, not reverse-engineered). The runtime supplies the device-address constants; the kernel supplies the host BAR0/BAR4 window math the runtime's addresses are projected into. Other versions renumber both. · Part I — Silicon & Architecture Model, DEEP · [back to overview](overview.md)*

## Abstract

A Neuron accelerator presents three address spaces to the software that drives it, and every pointer the runtime computes lives in one of them. The first is the **host BAR window** — the PCIe MMIO apertures the kernel `iomap`s, through which the host CPU reaches device CSRs (BAR0, the APB/register window) and HBM directly (BAR4, the DRAM window). The second is the **device physical address space** — the absolute addresses the accelerator's own DMA engines and sequencers use, spanning HBM, the per-NeuronCore TPB register tiles, and the APB sub-engine windows; these are the addresses baked into a model's static memory plan. The third is **on-core SRAM** — the per-NeuronCore State Buffer (SBUF) and PSUM accumulator planes, the scratch the compute pipeline actually reads and writes, mapped as BAR0 sub-windows and addressed by a device-side predicate rather than allocated. This page is the reimplementation-grade map of all three: which BAR carries what, how HBM is organized and tagged, where SBUF/PSUM live, and how a model variable's *device physical address* is finally computed.

Two facts dominate the device-address arithmetic and are the page's central findings. First, the kernel device-memory allocator maps each HBM genpool **`va == pa`**, but the Linux `gen_pool` backend cannot represent address `0` and `pa == 0` is a legal HBM address — so every device genpool's *virtual* axis is the physical address OR'd with **bit 63** (`GENPOOL_DEVMEM_BASE 0x1ull << 63`), while the physical axis carries the true PA. A reimplementer who treats the bit-63 value as a real address, or who feeds the clean PA back to the genpool, corrupts the allocator. Second, the host CPU does **not** see HBM at its device-physical address: BAR4 is a remap window that packs the two 16 GiB HBM stacks (device-physical `0x0` and `0x10_0000_0000` = 64 GiB) into a *contiguous* `0..32 GiB` host window. The device addresses a model carries (in its `mem_ref` plan) are device-physical; the host-side `mmap` of those tensors goes through the BAR4 remap. The two are not interchangeable.

The chain that produces a usable address runs build → plan → resolve. The compiler emits a static placement (the `mem_ref` plan; the planner is owned by [memory-planning](../neff/memory-planning.md)); the loader stages each staged variable into device HBM via the kernel allocator, writing the resolved `physical_address` into a 208-byte runtime `mem_ref_t`; at execute time `mem_ref_to_addr` resolves a `(variable, offset)` pair to an absolute device address with a single add. This page documents the address spaces those values live in — §1 the BAR windows and the FW-IO MISC RAM mailbox, §2 HBM organization and the bit-63 convention, §3 the SBUF/PSUM geometry and the SBUF address predicate, §4 scratchpad HBM, §5 the `physical_address` resolution math — and links the allocator ([mempool-handles](../kernel/mempool-handles.md)) and the planner ([memory-planning](../neff/memory-planning.md)) as boundaries rather than re-deriving them.

For reimplementation, the contract is:

- **The three BAR windows** — BAR0 (APB/CSR register window, base of every MMIO transaction), BAR2 (AXI, `BAR_UNUSED` on every shipping arch), BAR4 (DRAM/HBM window, remapped); the per-arch BAR *indices* are a [pci-device-ids](pci-device-ids.md) boundary, the *window contents* are this page's.
- **The MISC RAM / FW-IO mailbox** — host BAR0 offset `0x30fa0000` (V2), the firmware-status/handshake region the runtime polls for device-ready and reset completion.
- **The HBM model** — `MAX_DRAM_CHANNELS(4) × MAX_DDR_REGIONS(4)` genpool grid, the device-physical channel bases, and the bit-63 device-address (devmem-base) convention — reproduce it exactly or the allocator rejects `pa == 0`.
- **The per-NeuronCore SBUF/PSUM geometry** — 32 MiB SBUF + 32 MiB PSUM per NC, registered as BAR0 sub-windows, and the device-side SBUF address-range predicate (`is_address_in_sbuf`).
- **The scratchpad HBM model** — the backwards-growing contiguous scratchpad and the per-kbin local/shared scratchpad sizes the planner reserves.
- **The `mem_ref` `physical_address` math** — `addr = offset + physical_address`, where `physical_address` is `0xFFFF…F` until the loader stages the variable into HBM.

## At a glance

| | |
|---|---|
| **BAR set (runtime-addressed)** | BAR0 `apb_bar=0` (CSR/MISC RAM) · BAR2 `axi_bar=BAR_UNUSED(-1)` · BAR4 `dram_bar=4` (HBM) — [pci-device-ids](pci-device-ids.md) |
| **APB window base in BAR0** | `V2_PCIE_BAR0_APB_OFFSET 0x30000000` (`v2/address_map.h:29`) — every CSR VA = `bar0 + 0x30000000 + relbase` |
| **MISC RAM / FW-IO mailbox** | host BAR0 `0x30fa0000` = `0x30000000 + IOFAB_RELBASE 0xe00000 + IOFAB_MISC_RAM_RELBASE 0x1a0000` (`v2/address_map.h:30-32`) |
| **FW status reg** | `0x30fa0808` (`+ FW_IO_REG_FW_STATUS_OFFSET 0x808`); DEVICE_READY bit `0x8` (`neuron_reset.h:24-25`) |
| **HBM grid** | `MAX_DRAM_CHANNELS 4 × MAX_DDR_REGIONS 4` genpools (`neuron_mempool.h:70-71`) — V2 = 2 active channels |
| **HBM device bases (V2)** | `HBM_0_BASE 0x0` · `HBM_1_BASE 0x1000000000` (64 GiB); each `HBM_*_SIZE 0x400000000` (16 GiB) (`v2/address_map.h:77-92`) |
| **Devmem-base convention** | `GENPOOL_DEVMEM_BASE 0x1ull<<63` (`neuron_mempool.c:32`) — device genpool VA = `pa \| bit63` |
| **BAR4 HBM remap** | `mmap_get_bar4_offset_v2` (`neuron_dhal_v2.c:752`) — HBM1 64–80 GiB → 16–32 GiB on BAR4 |
| **SBUF / NeuronCore** | `V2_SBUF_SIZE 0x2000000` (32 MiB) (`v2/address_map.h:26`); per-NC TPB BAR0 tile |
| **PSUM / NeuronCore** | 32 MiB (`0x2000000`); BAR0 TPB-PSUM sub-window, TPB base + arch stride |
| **SBUF predicate** | `is_address_in_sbuf` / `is_address_in_my_sbuf` — `soc_struct_t +392 / +400` (`tdrv/encd/archs/sunda.c`) |
| **mariana BAR0 atlas builder** | `tdrv_arch_csr_register_device_mariana @0x30cd50` (`tdrv/encd/archs/mariana.c`) — 8 TPB + 8 TPB-PSUM + 4 preproc + apb_io/se + 16 top_sp |
| **PA resolution** | `mem_ref_to_addr @0x22da60` — `addr = offset + physical_address`; unstaged = `0xFFFFFFFFFFFFFFFF` |
| **Confidence** | HIGH — BAR/MISC-RAM/HBM/SBUF constants `file:line`-verbatim (DKMS) or addr/symbol-pinned (libnrt); BAR4 remap and PA math read from bodies |

The memory-map below is the single artifact a reimplementer matches every address against. Host-offset columns are V2 (Trn1/Inf2); the device-address columns are arch-keyed where they diverge (V2 vs the mariana mesh). A region is *device-physical* if the accelerator's own engines address it, *host BAR* if the CPU reaches it through a PCIe window.

| Region | Window / BAR | Base (host offset · device PA) | Size | Role | Confidence |
|---|---|---|---|---|---|
| APB / CSR register window | BAR0 (`apb_bar=0`) | host `bar0 + 0x30000000` | per-arch | base of every MMIO register transaction; FWIO target | HIGH |
| MISC RAM / FW-IO mailbox | BAR0 | host `0x30fa0000` | `0x40000` band | firmware status + reset/device-ready handshake (`v2/address_map.h:30-32`) | HIGH |
| FW status register | BAR0 | host `0x30fa0808` | 4 B | `DEVICE_READY` bit `0x8`; polled by reset wait (`neuron_reset.h:24-25`) | HIGH |
| Per-NC TPB tile (SBUF + regs) | BAR0 | dev `0x0 + TPB_0_SIZE·nc_id` (V2) / `0xd0000000+i·0x4000000` (mariana) | 64 MiB (V2) / 32 MiB tile (mariana) | NeuronCore register + State Buffer plane | HIGH |
| Per-NC PSUM tile | BAR0 | mariana `0xd2000000 + i·0x4000000` | `0x2000000` (32 MiB) | matmul accumulator SRAM | HIGH |
| Preproc tile | BAR0 | mariana `0xc0000000 + i·0x4000000` | `0x34C0000` | preprocessing engine window (×4) | HIGH |
| apb_io 0/1 | BAR0 | dev `0x8000000000` / `0x808000000000` | `0x20000000` (512 MiB) | top-level APB I/O window (mariana mesh halves) | HIGH |
| apb_se 0..3 | BAR0 | dev `0x1000000000` / `0x5000000000` / `0x801000000000` / `0x805000000000` | `0xC800000` (200 MiB) | per-SENG-pair APB sub-engine window | HIGH |
| top_sp 0..15 | BAR0 | dev `((i&7)<<30)+0x8280000000` (+mesh hi) | `0x400000` (4 MiB) | TopSP sequencer windows | HIGH |
| HBM channel 0 | BAR4 (`dram_bar=4`) | dev `0x0`; host BAR4 `0x0` | 16 GiB (V2) | DRAM stack 0; identity-mapped on BAR4 | HIGH |
| HBM channel 1 | BAR4 | dev `0x1000000000` (64 GiB); host BAR4 `0x400000000` (16 GiB) | 16 GiB (V2) | DRAM stack 1; remapped 64–80 → 16–32 GiB | HIGH |
| Firmware carveout | HBM (device) | top 16 MiB of each channel×region | `0x1000000` | firmware-reserved; allocator-protected ([mempool-handles §5](../kernel/mempool-handles.md)) | HIGH |
| Scratchpad HBM | HBM (device) | backwards from `main_pool_end_addr` | per-kbin plan | contiguous LIFO scratch; staged tensors | HIGH |

---

## 1. The BAR Windows and the MISC RAM Mailbox

### Purpose

The kernel maps up to three PCIe BARs per device; the runtime addresses two of them. **BAR0** is the APB/CSR register window — the host's gateway to every device control register, the FW-IO mailbox, and the per-NeuronCore register tiles. **BAR4** is the DRAM window — direct host access to HBM, through a remap (§2). **BAR2** (the AXI window) is `BAR_UNUSED(-1)` on every shipping arch and is never mapped. The BAR *indices* (which PCIe BAR carries which role) are latched per-arch from the DHAL and are the property of [pci-device-ids](pci-device-ids.md); this section owns the *contents* of the windows the runtime addresses, which that page defers here.

The single arithmetic fact a reimplementer must internalize: every device CSR the host touches is a **host BAR0 offset** computed as `bar0 + V2_PCIE_BAR0_APB_OFFSET(0x30000000) + relbase`, where `relbase` is the device-APB relative base of the target block. The `0x30000000` constant is the position of the APB aperture *inside* BAR0 — the device-APB address space is windowed into BAR0 at that fixed offset (`v2/address_map.h:29`). Device-side engines, by contrast, address the same blocks at their full device-APB addresses (e.g. `0xffff0d00000` for the H2D UDMA); the conversion the kernel's DMA-init code performs is `host_offset = 0x30000000 + (device_apb_addr - V2_APB_BASE)`, with `V2_APB_BASE 0xffff0000000` (`neuron_dhal_v2.c:1072-1077`, `address_map.h:77`).

### Entry Point

```text
neuron_pci probe  ──► ndhal->ndhal_pci.{apb_bar=0, axi_bar=-1, dram_bar=4}   [pci-device-ids]
  ├─ npdev.bar0  ◄── iomap(apb_bar=0)      ── CSR / MISC RAM register window
  ├─ npdev.bar2  ◄── (axi_bar=-1)          ── unused; reserve/map are no-ops, stays NULL
  └─ npdev.bar4  ◄── iomap(dram_bar=4)     ── HBM window (WC-mapped when wc_enable)

CSR transaction:  device_apb_addr ──► host_offset = 0x30000000 + (addr - 0xffff0000000)
                                       host_VA     = bar0 + host_offset
runtime CSR path: al_reg_{read,write}32 ──► csr_{read,write} ──► ndl_bar_{read,write}(BAR0)   [arch-csr-offsets]
```

### The MISC RAM / FW-IO mailbox

The firmware-I/O (FW-IO) mailbox lives in the **MISC RAM** block of the IOFAB region. It is the host's handshake surface with on-device firmware: device-ready status, reset completion, ECC counters, metric posting. Its host BAR0 base is a fixed composition of three relbases (`v2/address_map.h:30-32`):

```c
// MISC RAM host BAR0 base (V2) — neuron_dhal_v2.c reset/fwio paths
misc_ram_base = bar0
              + V2_PCIE_BAR0_APB_OFFSET        // 0x30000000   APB aperture inside BAR0
              + V2_APB_IOFAB_RELBASE           // 0x00e00000   IOFAB block
              + V2_APB_IOFAB_MISC_RAM_RELBASE; // 0x001a0000   MISC RAM within IOFAB
              // = bar0 + 0x30fa0000            (MMAP_BAR0_APB_MISC_RAM_OFFSET, :32)

// the firmware-status register the reset-wait poll reads (neuron_dhal_v2.c:194)
fw_status_addr = misc_ram_base + V2_FW_IO_REG_FW_STATUS_OFFSET;  // +0x808 → bar0 + 0x30fa0808
// device-ready test: (status & V2_FW_IO_REG_FW_STATUS_DEVICE_READY_MASK(0x8))   (neuron_reset.h:24-25)
```

`nr_wait_for_reset_completion_v2` (`neuron_dhal_v2.c:188-208`) polls `bar0 + 0x30fa0808` through `fw_io_read_csr_array` until the device-ready bit clears, retrying `NR_RESET_RETRY_COUNT(5)` times with a `100ms * i` backoff. The MISC RAM block also carries the `ncdev_bar0_write_blocked_addrs_v2[]` list — a set of MISC-RAM offsets the driver refuses to let userspace write through BAR0 (`ndma_is_bar0_write_blocked_v2`, `neuron_dhal_v2.c:1123-1170`) — so the mailbox is both the firmware handshake and a write-protected region.

> **NOTE —** the `0x30000000` APB-aperture offset is BAR0-internal, not a device address. A device-APB block at relative `R` is reached by the host at `bar0 + 0x30000000 + R` and by an on-device engine at `V2_APB_BASE(0xffff0000000) + R`. The two views of the same register differ by `0x30000000 - 0xffff0000000`; a reimplementer must keep the host-offset and device-PA forms distinct, exactly as `ndma_init_v2` does when it converts `H2D_UDMA_BASE 0xffff0d00000` to a host offset by subtracting `V2_APB_BASE` and adding `0x30000000` (`neuron_dhal_v2.c:1072-1077`).

> **GOTCHA —** BAR2 (`axi_bar`) is `BAR_UNUSED(-1)` on V2/V3/V4. The reserve/map ladder short-circuits it to a no-op and `npdev.bar2` stays `NULL`, yet `fw_io_setup` is still handed `bar2`/`bar2_size` (both zero) — FWIO drives its readless-read path off BAR0 and tolerates the absent AXI window. A reimplementer must not assume a usable BAR2; build the runtime to drive every register transaction off BAR0. (Boundary: the reserve/map ladder is [pci-device-ids](pci-device-ids.md).)

### The mariana BAR0 atlas

On the 8-NeuronCore mesh silicon (mariana / Trn3), the full BAR0 region map is built once at device bring-up by `tdrv_arch_csr_register_device_mariana` (`@0x30cd50`, `tdrv/encd/archs/mariana.c`), which registers each window into the runtime's `csr_regions_v4` table via `csr_register_region` (the table walk and bounds-check are [arch-csr-offsets §1](../runtime/arch-csr-offsets.md)). Every offset below is a BAR0 offset relative to `device->csr_base[0]` (the `ndl_device_t +0x48` BAR0 mmap base); the byte literals are read from `.rodata` xmmword tables:

```c
// tdrv_arch_csr_register_device_mariana @0x30cd50 — the mariana BAR0 atlas (csr_base[0]-relative)
//   8× TPB        @ 0xd0000000, 0xd4000000 … 0xec000000   size 0x2000000 (32 MiB)   .rodata @0x853CA0
//   8× TPB-PSUM   @ 0xd2000000, 0xd6000000 … 0xee000000   size 0x2000000 (32 MiB)   .rodata @0x853CE0
//   4× preproc    @ 0xc0000000, 0xc4000000, 0xc8000000, 0xcc000000  size 0x34C0000  .rodata @0x853D20
//   apb_io_0  dev 0x8000000000   @ csr_base+0x00000000   size 0x20000000 (512 MiB)
//   apb_io_1  dev 0x808000000000 @ csr_base+0x40000000   size 0x20000000
//   apb_se_0  dev 0x1000000000   @ csr_base+0x80000000   size 0xC800000  (200 MiB)
//   apb_se_1  dev 0x5000000000   @ csr_base+0x90000000   size 0xC800000
//   apb_se_2  dev 0x801000000000 @ csr_base+0xA0000000   size 0xC800000
//   apb_se_3  dev 0x805000000000 @ csr_base+0xB0000000   size 0xC800000
//   16× top_sp: dev (i<8)  ((i&7)<<30)+0x8280000000   @ csr_base+0xF0000000+((i&7)<<22)
//               dev (i>=8) ((i&7)<<30)+0x808280000000 @ csr_base+0xF2800000+((i&7)<<22)
//               size 0x400000 (4 MiB) each
// each failed registration → nlog_write("Could not register <region>%d BAR region") + non-zero NRT_STATUS
```

The `0x800000000000` high bit on `apb_io_1`, `apb_se_2/3` and the upper top_sp half is the **mesh second-die** selector (Trn3 is 2-die). The two dies occupy the low (`0x10/0x50/0x8280`) and high (`0x80100/0x80500/0x808280`) device-address quadrants; the geometry-relevant quadrant split is [hw-geometry](hw-geometry.md#die--mesh-neighbor-topology), and the full per-arch CSR band offsets are [arch-csr-offsets](../runtime/arch-csr-offsets.md).

### Function Map

| Function / symbol | Addr / file:line | Role | Confidence |
|---|---|---|---|
| `tdrv_arch_csr_register_device_mariana` | `@0x30cd50` | builds the mariana BAR0 region atlas into `csr_regions_v4` | HIGH |
| `tdrv_arch_csr_get_regions_mariana` | `@0x30cd30` | returns `csr_regions_v4`, `*num=672` | HIGH |
| `csr_regions_v4` | `.bss @0xc9f480` | 672 × 48-byte `csr_region_t` window records | HIGH |
| `nr_wait_for_reset_completion_v2` | `neuron_dhal_v2.c:188` | polls `bar0 + 0x30fa0808` for device-ready | HIGH |
| `ndma_is_bar0_write_blocked_v2` | `neuron_dhal_v2.c:1123` | MISC-RAM write-protect list enforcement | HIGH |
| `MMAP_BAR0_APB_MISC_RAM_OFFSET` | `v2/address_map.h:32` | `0x30fa0000` — MISC RAM host base | HIGH |

---

## 2. HBM Organization and the Bit-63 Devmem Convention

### Purpose

HBM is the device's main DRAM plane and the home of every staged model tensor. The kernel allocator partitions it into a fixed `MAX_DRAM_CHANNELS(4) × MAX_DDR_REGIONS(4)` grid of independent `gen_pool` arenas (`neuron_mempool.h:70-71,85`); a given arch lights up a subset (V2 = 2 channels). This section owns the *device-physical organization* and the *bit-63 device-address tagging*; the **allocator** that carves and frees those arenas — `mc_alloc_internal`, the small-pool strategy, the lifespan GC, the handle table — is the property of [mempool-handles](../kernel/mempool-handles.md) and is not re-derived here.

### HBM device-physical layout (V2)

The two HBM stacks sit at fixed, far-apart device-physical bases. They are **not** contiguous in the device address space — channel 1 begins at 64 GiB, not at 16 GiB:

```c
// v2/address_map.h:77-92 — V2 (Trn1/Inf2) HBM device-physical map
V2_HBM_0_BASE = 0x0;             V2_HBM_0_SIZE = 0x400000000;   // 16 GiB at device PA 0
V2_HBM_1_BASE = 0x1000000000;    V2_HBM_1_SIZE = 0x400000000;   // 16 GiB at device PA 64 GiB
// device PA space:  [0 .. 16 GiB) = stack 0   ;   [64 GiB .. 80 GiB) = stack 1
// the 16..64 GiB device-PA gap is unpopulated — channels do NOT abut.
```

cayman (V3 / Trn2) lights up 4 channels at 24 GiB each; mariana (V4 / Trn3) the same 4 channels at 36 GiB — the per-channel/per-stack sizes and channel count are the [hw-geometry](hw-geometry.md#the-geometry-dimension-table) axis. What is constant across arches is the *organization*: a channel × region genpool grid, each region a `gen_pool` over a slice of one channel's span (`region_size = channel_span / mp_device_num_regions`).

### The bit-63 device-address convention

The allocator makes a deliberate simplification: for device memory the genpool's *virtual* address equals the *physical* address (`va == pa`), so a chunk's PA is recoverable by a cheap `gen_pool_virt_to_phys` with no side mapping table. Two facts collide with that — `lib/genalloc.c` uses a returned address of `0` as its out-of-memory sentinel, and `pa == 0` is a legal HBM address (the base of channel 0). The fix is a single high bit on the genpool's virtual axis:

```c
// neuron_mempool.c — device genpool construction (the va==pa workaround)
#define GENPOOL_DEVMEM_BASE  (0x1ull << 63)              // :32  bit 63

gen_pool_add_virt(mp.gen_pool,
                  start_addr | GENPOOL_DEVMEM_BASE,      // :174  VIRTUAL axis = PA | bit63
                  start_addr,                            //       PHYSICAL axis = true PA
                  main_size, -1);
// → every device VA has bit 63 set and can never be 0;
//   gen_pool_virt_to_phys() strips it back to the clean PA.
// the chunk records BOTH: mc->va = (pa | bit63)   mc->pa = clean PA
```

> **QUIRK —** the bit-63 tag lives **only** on the genpool VA axis and on `mc->va`. `mc->pa` is always the clean physical address, and the per-device PA rbtree, the DMA layer, and every device-physical address a `mem_ref` carries are keyed on the *clean* PA. `gen_pool_free` and `gen_pool_virt_to_phys`, by contrast, take the *VA* — the bit-63 value in `mc->va` — so freeing with the clean PA corrupts the genpool. The two axes are not interchangeable: `mc->va` is the genpool key, `mc->pa` is everyone else's key. Host pools never set the bit (their VA is a real kernel VA from `dma_alloc_coherent`). The derivation and the small-pool/carveout machinery are [mempool-handles §2](../kernel/mempool-handles.md#2-the-device-pool--grid-small-pool-and-the-bit-63-convention) — this page cross-links it; it does not re-own it.

### The BAR4 host remap

A model carries device-physical HBM addresses, but the host CPU reaches HBM through BAR4, which is **not** an identity window. `mmap_get_bar4_offset_v2` (`neuron_dhal_v2.c:752-762`) packs the two far-apart 16 GiB device stacks into one contiguous `0..32 GiB` host window:

```c
// mmap_get_bar4_offset_v2 (neuron_dhal_v2.c:752) — device HBM PA → BAR4 host offset
function mmap_get_bar4_offset_v2(start_addr, size, out offset):
    if start_addr in [V2_HBM_0_BASE(0x0) .. +V2_HBM_0_SIZE(0x400000000)) and start+size < end:
        *offset = start_addr                                   // :755  HBM0 identity-mapped on BAR4
    else if start_addr in [V2_HBM_1_BASE(0x1000000000) .. +V2_HBM_1_SIZE):
        *offset = start_addr - V2_HBM_1_BASE + V2_HBM_0_SIZE;  // :758  64–80 GiB → 16–32 GiB
        // subtract 64 GiB (device base of stack 1), add 16 GiB (size of stack 0)
        //   → BAR4 packs both 16 GiB stacks into a contiguous 0..32 GiB host window
    else: return -EINVAL                                       // :760
```

> **GOTCHA —** the BAR4 bound check is strict-exclusive: a transfer whose `start + size` lands *exactly* on a stack boundary is rejected (`start+size < base+size`, `:755`). And the device-physical address `0x10_0000_0000` (64 GiB, HBM1 base) maps to host BAR4 offset `0x4_0000_0000` (16 GiB), **not** to `0x10_0000_0000`. A reimplementer who host-maps an HBM tensor by its device PA — instead of running it through the remap — reads the wrong 48 GiB-shifted window for every stack-1 tensor. The device-PA form is what `mem_ref` resolution (§5) returns; the BAR4 offset is what an `mmap` of that tensor needs.

### Function Map

| Function / symbol | Addr / file:line | Role | Confidence |
|---|---|---|---|
| `GENPOOL_DEVMEM_BASE` | `neuron_mempool.c:32` | `0x1ull<<63` — bit-63 device-VA tag | HIGH |
| `mp_init_device_mem` | `neuron_mempool.c:136` | builds one channel×region genpool with bit-63 VA axis | HIGH |
| `mmap_get_bar4_offset_v2` | `neuron_dhal_v2.c:752` | device HBM PA → contiguous BAR4 host offset | HIGH |
| `V2_HBM_{0,1}_BASE` / `_SIZE` | `v2/address_map.h:77-92` | device-physical HBM stack bases / sizes | HIGH |
| `mpset_init_device_pools` | `neuron_mempool.c:378` | DHAL DRAM query → grid build → carveout (boundary: [mempool-handles](../kernel/mempool-handles.md)) | HIGH |

---

## 3. SBUF / PSUM State-Buffer Geometry and the SBUF Predicate

### Purpose

The State Buffer (SBUF) is the per-NeuronCore on-core SRAM the compute pipeline reads its operands from and writes its activations to; PSUM is the partial-sum accumulator plane the matmul engine drains into. Neither is allocated — both are fixed-size BAR0 sub-windows, one pair per NeuronCore, addressed directly. This section owns their geometry and the device-side predicate the runtime uses to decide whether an address falls inside an SBUF plane. The headline sizes (32 MiB each) are also tabulated in [hw-geometry](hw-geometry.md#the-geometry-dimension-table); here they get their address-range arithmetic.

### Geometry

Each NeuronCore owns a 32 MiB SBUF and a 32 MiB PSUM region. On V2 they are reached as a per-NC BAR0 tile; on the mariana mesh they are explicit BAR0 regions in the §1 atlas (8 TPB tiles at `0xd0000000 + i·0x4000000`, 8 TPB-PSUM tiles at `0xd2000000 + i·0x4000000`). The kernel addresses the per-NC SBUF/semaphore/event base directly off BAR0:

```c
// nc_get_semaphore_base_v2 (neuron_dhal_v2.c:377-381) — per-NC TPB/SBUF tile base
sem_base = bar0 + V2_PCIE_BAR0_TPB_0_OFFSET(0x0)
                + V2_PCIE_BAR0_TPB_0_SIZE(0x4000000) * nc_id;   // 64 MiB per-NC stride
//   NC0 → bar0 + 0x0 ; NC1 → bar0 + 0x4000000

// nc_get_event_addr_v2 (neuron_dhal_v2.c:393-399) — event CSR within the NC tile
ev_base = bar0 + TPB_0_OFFSET + TPB_0_SIZE*nc_id + V2_MMAP_NC_EVENT_OFFSET(0x2700000);
ev_addr = ev_base + event_index * NC_EVENT_SIZE(4);             // 256 events, 4 B each

// V2_SBUF_SIZE = 0x2000000 (32 MiB)   (v2/address_map.h:26)
// semaphore window within the NC tile: SEMA_READ +0x1000, SET +0x1400, INCR +0x1800,
//   DECR +0x1c00, SEMA_SIZE 0x2000   (v2/address_map.h:20-26); 256 semaphores, 256 events.
```

The per-NC TPB tile (`TPB_0_SIZE 0x4000000` = 64 MiB) holds the 32 MiB SBUF plus the register/semaphore/event sub-windows; the separate PSUM 32 MiB region sits at the TPB-PSUM BAR0 offset (mariana `0xd2000000 + i·0x4000000`, a `TPB + arch_stride` apart from its TPB tile).

### The SBUF address predicate

The runtime carries a device-side test for whether an arbitrary device address lands inside an SBUF plane — the encoder uses it to validate instruction operands and pick DMA paths. It is a pair of `soc_struct_t` vtable slots installed by the per-arch `*_arch_init` (`tdrv/encd/archs/sunda.c` and siblings): `is_address_in_sbuf` (any core's SBUF) at `soc_struct_t +392`, and `is_address_in_my_sbuf(tpb_idx, addr)` (a specific core's SBUF) at `+400`. The shape is a range test against the per-NC SBUF window:

```c
// is_address_in_my_sbuf (soc_struct_t +400) — device-side SBUF range predicate
//   the SBUF window for NeuronCore tpb_idx spans [sbuf_base(tpb_idx), sbuf_base + SBUF_SIZE)
function is_address_in_my_sbuf(tpb_idx, addr):           // bool, dma_addr_t arg
    base = sbuf_base_for_core(tpb_idx)                   // per-NC SBUF plane base (BAR0 tile)
    return base <= addr && addr < base + SBUF_SIZE(0x2000000)   // 32 MiB window

// is_address_in_sbuf (soc_struct_t +392) — any-core variant
function is_address_in_sbuf(addr):                        // bool
    for each NeuronCore c:
        if is_address_in_my_sbuf(c, addr): return true
    return false
```

> **QUIRK —** SBUF/PSUM are **not** in the `mem_ref` allocation plan. A `mem_ref`'s `physical_address` (§5) is always an *HBM* address — SBUF is the compute-pipeline scratch the sequencer streams operands into and out of, addressed by the predicate above, not staged by the loader. A reimplementer who treats SBUF like HBM (alloc'd, tracked in the PA rbtree) inverts the model: HBM is allocated and tracked; SBUF is a fixed window tested by range. The predicate is the boundary check, not an allocator. *(Confidence: the slot offsets `+392/+400` and the range-test shape are HIGH from `soc_struct_t` — ARCH-13; the exact `sbuf_base_for_core` body is MED — a per-arch leaf not byte-decoded on this page, modelled from the BAR0 TPB-tile stride.)*

### Function Map

| Function / symbol | Addr / offset | Role | Confidence |
|---|---|---|---|
| `is_address_in_sbuf` | `soc_struct_t +392` | any-core SBUF range predicate | HIGH (slot) / MED (body) |
| `is_address_in_my_sbuf` | `soc_struct_t +400` | per-core SBUF range predicate `(tpb_idx, addr)` | HIGH (slot) / MED (body) |
| `nc_get_semaphore_base_v2` | `neuron_dhal_v2.c:377` | per-NC TPB/SBUF tile BAR0 base | HIGH |
| `nc_get_event_addr_v2` | `neuron_dhal_v2.c:393` | per-NC event CSR address | HIGH |
| `V2_SBUF_SIZE` | `v2/address_map.h:26` | `0x2000000` (32 MiB) SBUF plane size | HIGH |
| `get_num_sbuf_partitions` / `get_sbuf_partition_size` | `soc_struct_t +576 / +568` | SBUF partition geometry getters | HIGH (slot) |

---

## 4. Scratchpad HBM

### Purpose

Scratchpad is the region of HBM a model uses for transient intermediates — the values that live only during a single inference and never cross to the host. It is allocated out of the same HBM genpools as weights, but with a distinct discipline: a single contiguous region that grows **backwards** from the top of a region's main pool, and a per-kbin *plan* (local + shared scratchpad sizes) that the loader reserves up front. This section names the model; the planner that *computes* the scratchpad sizes (`tdrv_scratchpad_model_init`) is the property of [memory-planning](../neff/memory-planning.md), and the backwards-growing allocator sub-path is [mempool-handles §3](../kernel/mempool-handles.md#3-the-allocator--mc_alloc_internal).

### The model

The static memory plan header (`kbin_mem_ref_set_t`, 24 B) carries two scratchpad sizes the planner sets from the compiler's lifetime analysis:

```c
// kbin_mem_ref_set_t (24 B header, ordinal 6557) — the static memory-plan header
struct kbin_mem_ref_set_t {
    uint64_t local_scratchpad_size;    // +0    per-NeuronCore-private scratch
    uint64_t shared_scratchpad_size;   // +8    cross-core shared scratch
    uint32_t nmem;                     // +0x10 number of mem_ref entries
    kbin_mem_ref_t mem[];              // +0x18 flexible array
};
// tdrv_scratchpad_model_init (@0x302b70) reads these to size + lay out the scratchpad,
//   reserving pages via dmem_alloc_contiguous_scratchpad_page  (planner: memory-planning.md)
```

The kernel allocator backs the contiguous scratchpad with a backwards-growing fixed-offset genpool allocation (`get_offset_for_scratchpad_alloc`, `neuron_mempool.c:617`): each scratchpad chunk sits immediately below the previous one, `region_size - small_pool_size - scratchpad_size - alloc_size`, making the scratchpad a LIFO stack capped at `main_pool_end_addr` (`neuron_mempool.c:628`).

> **NOTE —** the scratchpad's two sizes (`local`/`shared`) come from the compiler, not from a runtime probe — they are the static plan. A reimplementer sizes the scratchpad from `kbin_mem_ref_set_t.{local,shared}_scratchpad_size`, reserves it as one contiguous HBM region per NeuronCore group at load, and never grows it during inference. The LIFO backwards-growth and the topmost-page free check (`pa + scratchpad_size == main_pool_end_addr`) are the allocator's enforcement of that contiguity — [mempool-handles §3](../kernel/mempool-handles.md#the-three-device-sub-paths).

### Function Map

| Function / symbol | Addr / file:line | Role | Confidence |
|---|---|---|---|
| `kbin_mem_ref_set_t` | ordinal 6557 (24 B) | static-plan header: local/shared scratchpad sizes + `mem[]` | HIGH |
| `tdrv_scratchpad_model_init` | `@0x302b70` | sizes/lays out scratchpad from the plan (boundary: [memory-planning](../neff/memory-planning.md)) | HIGH |
| `get_offset_for_scratchpad_alloc` | `neuron_mempool.c:617` | backwards-growing contiguous offset | HIGH |

---

## 5. Resolving a `mem_ref` Physical Address

### Purpose

The end of the chain: a model variable's *device physical address*. At execute time the runtime holds a `(variable_id, offset, size)` triple and must produce an absolute device address; it does so with a single add against a `physical_address` the loader resolved once, at stage time. This section owns the *resolution math*; the **planner** that decided the placement and the full `mem_ref` object model (the 10-class build-side tree, the 152-byte on-disk record, the 208-byte runtime struct) are the property of [memory-planning](../neff/memory-planning.md) — here it is the address arithmetic alone.

### The runtime `mem_ref_t` resolved state

The 208-byte runtime `mem_ref_t` (`calloc 0xD0`, `mem_ref_get_new_mr @0x2fb640`) carries the resolved staging state in a union at `+0x20`; for a staged tensor that is the device physical address and the backing `dmem` handle:

```c
// mem_ref_t resolved-state union (+0x20), staged-tensor variant — load/resolve side
//   +0x08  kbin_mr_type_t  type   // MR_SB / MR_PTR / MR_VIRTUAL_TMP_BUF / MR_REMOTE …
//   +0x10  size_t          size   // tensor byte size (bounds the resolve)
//   +0x20  uint64  physical_address  // device PA; init 0xFFFFFFFFFFFFFFFF (= UNSTAGED)
//   +0x28  dmem_t* mem               // backing device-memory handle
//   +0x30  uint64  mem_offset        // offset within the dmem region (0 for whole-tensor)
//   +0x38  uint64  va                // kernel VA (used by mem_ref_get_va for virtual_tmp_buf)
```

`physical_address` is written by `mem_ref_copy_and_stage_mr` (`@0x2fb780`) when it stages the variable into HBM: it calls `dmem_alloc_aligned` (device DRAM) + `dmem_buf_copyin` (host→device), then writes `mr->mem (+0x28)` and `mr->physical_address (+0x20)` from the `dmem_t` handle's resolved PA, and `mem_offset (+0x30) = 0`. Until that runs, `physical_address` is the all-ones sentinel `0xFFFFFFFFFFFFFFFF` (`mem_ref_get_new_mr` inits it so).

### The resolution math

```c
// mem_ref_to_addr (@0x22da60, tdrv/dma_ring.c) — (variable, offset, size) → absolute device PA
function mem_ref_to_addr(mr, offset, size, out addr):
    // 1. bounds: the access must fit the tensor
    if mr->size < offset + size:                              // bounds-check
        FATAL("invalid offset in %s, %lu < (%lu + %lu)")      //   mr->size < offset+size
    // 2. pointer tensors: only a whole-tensor (offset 0) reference is legal
    if mr->type == MR_PTR(7) && offset != 0:
        FATAL("offset == 0")
    // 3. the staging guard: reject an unstaged variable
    if mr->physical_address == 0xFFFFFFFFFFFFFFFF:            // == UNSTAGED sentinel
        FATAL("Variable %s is not staged in HBM")
    // 4. THE resolution — a single add
    *addr = offset + mr->physical_address                    // absolute device PA
    return NRT_SUCCESS
```

The whole point of the static plan is that this is the *only* arithmetic at execute time — no allocation, no table walk beyond the `var_id → mem_ref_t` hashtable lookup (`lookup_memref_by_idx @0x22da30`, `ht_find` against the model's `mem_ref_set`). The address is `offset + physical_address`, and `physical_address` is a clean device HBM PA (no bit-63 tag — that lived only on the genpool VA axis, §2).

> **GOTCHA —** the unstaged sentinel is `0xFFFFFFFFFFFFFFFF`, not `0` — a device PA of `0` is a *legal* staged address (HBM channel 0 base, the same reason the allocator forces the bit-63 VA tag in §2). A reimplementer who uses `0` as the "not yet placed" marker conflates a valid stage-0 placement with the unstaged state. Test against the all-ones sentinel, exactly as `mem_ref_to_addr` does (`mem_ref_get_new_mr` writes it at `+0x20` init time). Conversely, a `MR_VIRTUAL_TMP_BUF` resolves through `mem_ref_get_va` (`@0x2fc370`) — VA, not PA — when the access fits the backing `dmem` region; the staged-tensor PA path above is the common case.

### Function Map

| Function / symbol | Addr | Role | Confidence |
|---|---|---|---|
| `mem_ref_to_addr` | `@0x22da60` | `(var, offset, size)` → absolute device PA = `offset + physical_address` | HIGH |
| `lookup_memref_by_idx` | `@0x22da30` | `var_id → mem_ref_t*` hashtable lookup | HIGH |
| `mem_ref_get_new_mr` | `@0x2fb640` | `calloc` 208-byte `mem_ref_t`; inits `physical_address = 0xFFFF…F` | HIGH |
| `mem_ref_copy_and_stage_mr` | `@0x2fb780` | stage to HBM; write `physical_address` + `mem` from the `dmem` handle | HIGH |
| `mem_ref_get_va` | `@0x2fc370` | VA resolution for `MR_VIRTUAL_TMP_BUF` | HIGH |
| `dmem_alloc_aligned` | `@0x228f20` | device-DRAM allocation backing a staged `mem_ref` (boundary) | HIGH |

---

## Cross-References

- [PCI Device-ID → Arch Map](pci-device-ids.md) — the BAR set per device (`apb_bar=0` / `axi_bar=BAR_UNUSED` / `dram_bar=4`), the reserve/map ladder, and the per-arch BAR-index profile this page's window contents project into
- [Memory Pool and MC Handle Table](../kernel/mempool-handles.md) — the kernel HBM genpool allocator: the 4×4 channel×region grid, the bit-63 `GENPOOL_DEVMEM_BASE` derivation, the small-pool/scratchpad sub-paths, the 16 MiB firmware carveout, and the lifespan GC this page cross-links but does not re-derive
- [Static Memory Planning (mem_ref)](../neff/memory-planning.md) — the full `mem_ref` object model (10-class build tree, 152-byte on-disk record, 208-byte runtime struct) and `tdrv_scratchpad_model_init`, the planner that assigns the `physical_address` this page resolves
- [Per-Arch Device Layer: CSR and Register-Offset Accessors](../runtime/arch-csr-offsets.md) — the `csr_*` MMIO primitives and the per-arch CSR band offsets the BAR0 windows in §1 are addressed through
- [Per-Generation Hardware Geometry](hw-geometry.md) — the HBM channel/size axis, the SBUF/PSUM headline sizes, and the 4-quadrant mesh PA scheme whose BAR0 atlas §1 owns
