# Silicon & Architecture Model — Runtime Lens

> **Runtime lib:** `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` · **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`; ELF64, unstripped, DWARF present; `.text`/`.rodata` VMA == file offset).
> **Kernel:** `aws-neuronx-dkms 2.27.4.0` (unstripped GPL-2.0 source; cited `file:line`).
> **Status:** Reimplementation-grade map · **Evidence grade:** byte/offset/`file:line`-anchored · **Part I — Silicon & Architecture Model** / Runtime-facing summary · [back to index](../index.md)

## Abstract

This page is the **map of how the Neuron runtime stack models the silicon it drives**. Picture the familiar mental model of any accelerator: a small set of fixed-function **engines** wired to **memory planes**, replicated into a **core hierarchy** that the host addresses through PCI BARs. The Neuron stack is exactly this — but the model is split across two layers that must agree byte-for-byte. The **kernel driver** (DKMS) answers "which silicon generation is installed" from PCI; the **userspace runtime** (libnrt.so) answers "what is its geometry and where is every register" from a per-arch ops vtable. A reimplementer who gets the two layers' generation/geometry numbering out of sync will mis-address CSRs and corrupt DMA — so this page's job is to nail the shared model and then hand off the canonical detail to the five sibling pages.

The generation taxonomy has **two names for the same thing**. The kernel calls them **V2 / V3 / V4** (`enum neuron_arch`, `neuron_arch.h:14-16`); the runtime calls them **sunda / cayman / mariana** (`enum al_hal_tpb_arch_type`, values 2/3/4). They are the same three generations under different vocabularies, and both number from 2 with a deliberate gap at 0/1. V2/sunda is Trainium1 + Inferentia2 (2 NeuronCores, two PCI device-IDs sharing one arch); V3/cayman is Trainium2 (8 NeuronCores); V4/mariana is Trainium3 (8 NeuronCores, mesh topology). The runtime additionally carries a `mariana_plus` core-type family (Trn3 successor) in its coretype enum that has no kernel arch value yet — a forward-declaration the reimplementer should expect.

Each generation is reduced, at runtime, to a populated **`tdrv_arch_ops`** vtable (488 bytes, the global `tdrv_arch_ops_1`) of ~60 function pointers: geometry constants (num NeuronCores, num sequencer engines, DMA-queues-per-engine), register-offset math (CSR/notific/mem APB windows), and DMA/semaphore/event address tables. `tdrv_arch_ops_init` switches on `al_hal_tpb_get_arch_type()` and installs `tdrv_arch_register_{sunda,cayman,mariana}`; thereafter every generic `tdrv_arch_get_*` getter dispatches through the installed slot. The pages below derive each axis in full — this page summarizes them and pins the cross-layer numbering invariants.

For reimplementation, the contract this map fixes is:

- **The two-vocabulary generation model** and the rule that V2↔sunda(2), V3↔cayman(3), V4↔mariana(4) are the same enum value across kernel and runtime — and that there is **no value 1** in either.
- **The engine complement** PE/ACT/POOL/DVE/SP (+ TopSP, + GPSIMD/Q7) and the per-generation NeuronCore counts (2 / 8 / 8) that all geometry math derives from.
- **The memory-plane model** the runtime addresses: the three PCI BARs (APB / AXI / DRAM-window) and how per-arch register math projects core-relative CSR offsets onto BAR0.
- **The dispatch model**: arch detected once (kernel, from PCI; runtime, from `al_hal_tpb_get_arch_type`), cached, then served to a per-generation ops vtable — and the strict ordering that arch must be known before the vtable is installed.

## At a glance

| Axis | Kernel (DKMS) | Runtime (libnrt.so) |
|---|---|---|
| Generation enum | `enum neuron_arch` V2=2/V3=3/V4=4 (`neuron_arch.h:14-16`) | `enum al_hal_tpb_arch_type` SUNDA=2/CAYMAN=3/MARIANA=4/NUM=5 (`_enums.json`) |
| Detection source | PCI device-ID switch (`neuron_pci.c:213-222`) | `al_hal_tpb_get_arch_type()` → `tdrv_arch_ops_init` @`0x308e80` |
| Cache | file-static `arch_info`, first-wins (`neuron_arch.c:25-39`) | global `tdrv_arch_ops_1` (488 B), installed once |
| Per-gen install | DHAL `ndhal_register_funcs_v{2,3,4}` (`neuron_dhal.c:36-51`) | `tdrv_arch_register_{sunda,cayman,mariana}` @`0x30b6a0`/`0x30c7d0`/`0x30d900` |
| Engine complement | — (driver is engine-agnostic) | PE/ACT/POOL/DVE/SP (`al_hal_tpb_eng_type` 0..4) + TopSP + Q7 |
| Core count V2/V3/V4 | `MAX_NC_PER_DEVICE 8` cap (`neuron_device.h:11`) | `tdrv_arch_get_num_tpb_*` = 2 / 8 / 8 (byte-verified) |
| Memory planes | 3 BARs: bar0 APB / bar2 AXI / bar4 DRAM (`neuron_device.h:45-55`) | `ndl_device_t.csr_base[0]` = BAR0 base (`+0x48`) |
| Platform axis | STD/ULTRASERVER/PDS (`neuron_arch.h:21-23`, DMI-matched) | — (kernel-owned, orthogonal to arch) |

---

## The generation model — two vocabularies, one enum value

The single most important invariant on this page: the kernel and the runtime number the generations **identically** but name them differently.

```text
generation   kernel enum (neuron_arch.h)   runtime enum (al_hal_tpb_arch_type)   silicon
─────────────────────────────────────────────────────────────────────────────────────
 (gap)        INVALID = 0                   INVALID = 0
 (gap)        — no value 1 —                INVALID_1 = 1                          (reserved)
 V2 / sunda   NEURON_ARCH_V2 = 2            AL_HAL_TPB_ARCH_TYPE_SUNDA = 2         Trn1 + Inf2
 V3 / cayman  NEURON_ARCH_V3 = 3            AL_HAL_TPB_ARCH_TYPE_CAYMAN = 3        Trn2
 V4 / mariana NEURON_ARCH_V4 = 4            AL_HAL_TPB_ARCH_TYPE_MARIANA = 4       Trn3
 terminator   NEURON_ARCH_NUM = 5           AL_HAL_TPB_ARCH_TYPE_NUM = 5
```

Both enums skip 1 deliberately (the kernel has *no* value-1 slot; the runtime keeps `INVALID_1=1` as a reserved sentinel). The numbering matches the DHAL source directory names `v2/ v3/ v4/`. The runtime coretype enum (`nrtucode_coretype_t`, `_enums.json`) carries a fourth family, `MARIANA_PLUS` (values 23-30) — a Trn3-successor placeholder with NX engines + Q7, with **no** corresponding kernel `neuron_arch` value in DKMS 2.27.4.0.

> **GOTCHA —** never key cross-layer logic on the *name*. A reimplementer who maps "V3" → string "v3" → the runtime's enum-by-name will be fine, but one who assumes the runtime numbers from 0 (sunda=0) will mis-index `tdrv_arch_ops_init` (which switches on the integer 2/3/4, `tdrv_arch_type.c:0x41`) and silently install the wrong arch vtable. The integers are the contract; the strings are decoration.

> **NOTE —** the kernel's generation is detected from the **PCI device-ID** (`neuron_pci_set_device_architecture`, `neuron_pci.c:205-227`): device-IDs `0x7164`(Trn1)/`0x7264`(Inf2) → V2, `0x7364`(Trn2) → V3, `0x7564`/`0x7565`(Trn3) → V4, all under vendor `AMZN_VENDOR_ID 0x1D0F` (`neuron_device.h:35-40`). The runtime instead reads `al_hal_tpb_get_arch_type()`. The two paths must converge on the same integer; the full device-ID table is the canonical detail of [PCI Device-ID → Arch Map](pci-device-ids.md).

---

## Per-generation geometry

Every geometry constant below is **byte-verified** from the runtime's per-arch leaf functions (sunda/cayman/mariana TDRV arch-ops). These are the hard-coded HW constants the rest of the runtime's addressing math derives from. The columns are the geometry *dimensions*; the rows are the three generations. Anchors are symbol+address (runtime leaf) or `file:line` (kernel).

| Dimension | V2 / sunda | V3 / cayman | V4 / mariana | Anchor (runtime leaf / kernel) |
|---|---|---|---|---|
| NeuronCores (num TPB) | **2** | **8** | **8** | `tdrv_arch_get_num_tpb_sunda` @`0x30b540` (`mov $0x2`); `_cayman` @`0x30ba40`; `_mariana` @`0x30cb70` (both `mov $0x8`) |
| Sequencer engines (SENG) | — | **4** | 4 (4×2 TPB) | `tdrv_arch_get_num_seng_cayman` @`0x30ba20` (=4) |
| TPB per SENG | — | **2** | 2 | `tdrv_arch_get_num_tpb_per_seng_cayman` @`0x30ba30` (=2) |
| Engine complement | PE·ACT·POOL·DVE·SP | + TopSP | + TopSP | `al_hal_tpb_eng_type` PE=0..SP=4 (`_enums.json`) |
| GPSIMD / Q7 | Q7-POOL | Q7-POOL·Q7-CCE | Q7-POOL·Q7-CCE | `nrtucode_coretype_t` (per-arch Q7 entries) |
| TPB per HBM | **1** | 2 (`idx/2`) | 2 (`idx/2`) | `tdrv_arch_get_num_tpb_per_hbm_sunda` @`0x30b550` (=1); `_default_hbm_index_cayman` @`0x30ba90` (`idx/2`) |
| DMA queues / engine | **16** | 16 | 16 | `tdrv_arch_get_num_dma_queues_per_engine_sunda` @`0x30b530` (=16); `_num_dma_per_tpb_cayman` @`0x30ba10` (=16) |
| Q7-POOL core-type id | (sunda=6) | **13** | (mariana=21) | `tdrv_arch_get_q7_pool_core_type_cayman` @`0x30ba70` (=13); cf. `nrtucode_coretype_t` |
| H2D DMA engine ids | — | — | **{128,129,130,131}** | `mariana_h2d_dma_eng_id` @`0x9df200` (byte-decoded) |
| TopSP per device (cap) | — | — | 16 regions | `MAX_TS_PER_DEVICE 16` (`neuron_device.h:12`); 16× top_sp in `csr_register_device_mariana` |

A few load-derivations a reimplementer needs:

- **V2/sunda is the odd one out.** It has 2 NeuronCores and **1 TPB per HBM** (`get_num_tpb_per_hbm_sunda` returns 1), whereas V3/V4 pack **2 TPB per HBM** (`hbm_index = tpb_idx/2`). The per-TPB memory-window stride on sunda is `idx << 26` = 64 MiB/TPB (`get_tpb_mem_offset_sunda` @`0x30b4e0`), and the per-TPB APB stride is `0x700000` = 7 MiB applied only when `device_tpb_idx != 0` (so TPB0's base is 0; `get_tpb_csr_apb_offset_sunda` @`0x30b600`).
- **V3/V4 share the 8-core, 4-SENG, 2-TPB-per-SENG shape** (4 seng × 2 TPB = 8). The `MAX_NC_PER_DEVICE 8` kernel cap (`neuron_device.h:11`) is exactly this worst case; V2 uses only 2 of those slots.
- **The engine complement is fixed across generations**: `al_hal_tpb_eng_type` enumerates exactly PE=0, ACT=1, POOL=2, DVE=3, SP=4 (`MAX_ENG=5`) for all three archs. What changes per generation is the *coretype numbering* assigned to those engines, plus the Q7/CCE additions — see the next section.

> **QUIRK —** V4/mariana adds **off-TPB H2D DMA engines** numbered 128-131 (`mariana_h2d_dma_eng_id @0x9df200 = {128,129,130,131,0,0,0,0}`, indexed by `idx / num_tpb_per_seng`). These four IDs are special-cased in `tdrv_arch_get_dma_eng_sdma_base_mariana` (@`0x30cc40`) and predicated by `tdrv_arch_is_h2d_engine_mariana` (@`0x30ccd0`, returns `(eng-128) <= 3`). A reimplementer treating engine IDs as a dense `[0..15]` range will mis-route every host-to-device transfer on Trn3.

---

## The engine complement and coretype numbering

Each generation's NeuronCore is the same five-engine machine — **PE** (tensor/matmul), **ACT** (activation), **POOL** (pooling/vector), **DVE** (data/vector engine, also the seed-set/RNG owner), **SP** (sequencer/scalar processor) — plus a **TopSP** per device and a **GPSIMD/Q7** vector core (Tensilica Vision-Q7). What differs per generation is the integer **core-type** assigned to each (engine, generation) pair.

The runtime's `nrtucode_coretype_t` (`_enums.json`) is a *flat* enum that interleaves generation and engine: each generation gets a contiguous block of 7-8 values:

```text
sunda:         NX_ACT=0 NX_DVE=1 NX_POOL=2 NX_PE=3 NX_SP=4 NX_TOPSP=5 Q7_POOL=6
cayman:        NX_ACT=7 NX_DVE=8 NX_POOL=9 NX_PE=10 NX_SP=11 NX_TOPSP=12 Q7_POOL=13 Q7_CCE=14
mariana:       NX_ACT=15 NX_DVE=16 NX_POOL=17 NX_PE=18 NX_SP=19 NX_TOPSP=20 Q7_POOL=21 Q7_CCE=22
mariana_plus:  NX_ACT=23 ... Q7_CCE=30
```

The mapping from the *hardware engine index* (`al_hal_tpb_eng_type`: PE=0,ACT=1,POOL=2,DVE=3,SP=4) to this flat core-type is a per-arch lookup table. For cayman it is `CSWTCH_16_0` (.rodata @`0x9de900`, byte-decoded `[10, 7, 9, 8, 11]`), consumed by `tdrv_arch_get_nx_core_type_cayman` @`0x30ba50`:

```c
// tdrv_arch_get_nx_core_type_cayman @0x30ba50
int get_nx_core_type_cayman(al_hal_tpb_eng_type eng):
    if eng > SP(4): return -1                       // out of range
    return CSWTCH_16_0[eng]                          // @0x9de900 = {10,7,9,8,11}
    //  PE(0)->10  ACT(1)->7  POOL(2)->9  DVE(3)->8  SP(4)->11
    //  == CAYMAN_NX_{PE=10, ACT=7, POOL=9, DVE=8, SP=11} in nrtucode_coretype_t
```

The LUT is exactly the inverse permutation that re-orders the hardware `{PE,ACT,POOL,DVE,SP}` order into the coretype enum's `{ACT,DVE,POOL,PE,SP}` declaration order, offset by the generation block base (cayman base = 7). This is why the LUT looks scrambled: it is a declaration-order remap, not arbitrary.

> **GOTCHA —** there are *two* numbering systems and they are NOT the same. `al_hal_tpb_eng_type` (PE=0..SP=4) is the **hardware engine index** used for register addressing; `nrtucode_coretype_t` (0..30) is the **microcode core-type** used to select the right GPSIMD/Q7 ucode blob. The per-arch `get_nx_core_type` LUT is the only correct bridge between them. Conflating the two — e.g. using engine index 3 (DVE) where a coretype is expected — selects the wrong ucode. The reconciliation of these (and the off-by-one TopSP cases) is the subject of [Coretype Numbering Reconciliation](coretype-numbering.md).

---

## The device / NeuronCore / vNC hierarchy

The runtime sees the hardware as a three-level hierarchy, addressed top-down:

```text
neuron device  (one PCI function; up to MAX_NEURON_DEVICE_COUNT=64 per host)
  └─ NeuronCore / TPB  (device_tpb_idx 0..num_tpb-1; 2 on V2, 8 on V3/V4)
        ├─ engines: PE · ACT · POOL · DVE · SP
        ├─ TopSP    (device-level sequencer; up to MAX_TS_PER_DEVICE=16)
        └─ GPSIMD / Q7  (POOL, + CCE on V3/V4)
```

- **Device** — one PCI function, modeled kernel-side by `struct neuron_device` (`neuron_device.h:70-129`) and runtime-side by `ndl_device_t` (632 B; `csr_base[0]` at `+0x48` is the BAR0 mmap base every region calc reads). The driver assumes a host holds **one chip type only** (`neuron_arch.c:6-8`) and caches the first device's arch first-wins.
- **NeuronCore (TPB)** — the runtime's `physical_core_t` (24 B): the only field the geometry/offset leaves read is `device_tpb_idx` at `+0x04`, guarded `< tdrv_arch_get_num_tpb()` before every offset computation (a `__assert_fail` on violation, e.g. `"pcore->device_tpb_idx < tdrv_arch_get_num_tpb()"`, sunda offset leaves). The index is the input to all per-core register math (`>>1` for seng-pair, `>>2` for quadrant, `/2` for HBM).
- **vNC (virtual NeuronCore)** — `physical_core_t+0x08` holds a `const virtual_core_t*`. The runtime threads a virtual-core abstraction over the physical TPB so a model can be placed on a subset of cores; the physical→virtual binding is owned by the [TDRV device bring-up](../runtime/tdrv-lifecycle.md) layer, not this map. **(MEDIUM** — the vNC layout itself is not byte-derived here; only the back-pointer slot is confirmed.**)**

---

## The memory planes (BAR layout)

The runtime addresses three physical memory planes through the kernel's three PCI BARs (`struct neuron_pci_device`, `neuron_device.h:45-55`):

| Plane | Kernel BAR | Role | Runtime view |
|---|---|---|---|
| **APB** | `bar0` (`bar0_pa`/`bar0`/`bar0_size`) | Control-register window (CSR, notific, TopSP regions) | `csr_base[0]` (`ndl_device_t+0x48`); all CSR region offsets are BAR0-relative |
| **AXI** | `bar2` (`bar2_pa`/`bar2`/`bar2_size`) | AXI fabric window | per-arch SDMA/sem/evt address math |
| **DRAM / HBM** | `bar4` (`bar4_pa`/`bar4`/`bar4_size`) | Device DRAM (HBM) aperture | `ndl_device_t.hbm_va[4]` (`+0x228`), `hbm_size` (`+0x248`) |

The per-arch register math is what projects a *core-relative* CSR offset onto an *absolute* BAR0 offset. The model is cleanest on V4/mariana, whose `tdrv_arch_csr_register_device_mariana` (@`0x30cd50`, 1182 B) builds the **entire BAR0 CSR atlas** at bring-up and registers each window into the global `csr_regions_v4` table (.bss @`0xc9f480`, 672 × 48 B = 32256 B, byte-confirmed). The mariana BAR0 region map:

```text
8× TPB        BAR0 +0xd0000000 .. +0xec000000   (stride 0x4000000)  size 0x2000000 (32 MiB)
8× TPB-PSUM   BAR0 +0xd2000000 .. +0xee000000   (interleaved)        size 0x2000000 (32 MiB)
4× preproc    BAR0 +0xc0/c4/c8/cc000000                              size 0x34C0000 (~52 MiB)
apb_io_0/1    BAR0 +0 / +0x40000000   dev-PA 0x8000000000 / 0x808000000000   size 0x20000000 (512 MiB)
apb_se_0..3   BAR0 +0x80/90/A0/B0000000   dev-PA {0x10,0x50,0x80100,0x80500}00000000   size 0xC800000 (200 MiB)
16× top_sp    BAR0 +0xF0000000+ (i&7)<<22, two halves   dev-PA (i<8) 0x8280000000+ / (i>=8) 0x808280000000+   size 0x400000 (4 MiB)
```

The four-quadrant device-PA scheme (`apb_se` bases `0x10..`/`0x50..`/`0x80100..`/`0x80500..`, from `mariana_apb_se_bases @0x9df1e0`) encodes the mesh/socket routing of Trn3 — the high bits `0x80...` select the far quadrant. The per-arch address translation (`tdrv_arch_get_dma_engine_bar_offset_adjustment_mariana` @`0x30ccb0`) maps an absolute `apb_se` PA back to a BAR0-relative offset via `mariana_apb_bar_offsets @0x9df1c0` = `{0x80000000, 0x90000000, 0xa0000000, 0xb0000000}`. The full atlas, the SRAM/state-buffer plane, and the V2/V3 counterparts are the subject of [Memory Hierarchy, BAR Layout and State Buffer](memory-hierarchy.md).

> **NOTE —** the kernel exposes a fourth, simulator-driven sizing path: when `narch_is_emu()`/`narch_is_qemu()` hold (PCI revision `0xF0`/`0xFF`, `neuron_arch.c:30-31`), the DRAM/bar4 window is sized dynamically (`dram_bar_size/4`) and completion-timing waits are skipped. On real silicon the BAR sizes come from PCI config. The reimplementer's BAR-probe must tolerate both. The emulation gate is detailed in [Generations, the V2/V3/V4 Enum](generations-enum.md).

---

## Dispatch: detect once, serve forever

Both layers follow the same shape — **detect the generation once, cache it, then serve a per-generation ops vtable** — but on different inputs and with a strict ordering constraint.

```text
KERNEL                                    RUNTIME
──────                                    ───────
PCI probe (neuron_pci.c:352)              al_hal_tpb_get_arch_type()
  └ device-ID switch → enum neuron_arch     └ returns 2/3/4
  └ narch_init(arch, rev)  [first-wins]   tdrv_arch_ops_init @0x308e80
      cache in arch_info (neuron_arch.c:33)   switch(arch):
  └ neuron_dhal_init(neuron_dhal.c:10)         case 2: tdrv_arch_register_sunda  @0x30b6a0
      arch = narch_get_arch()                  case 3: tdrv_arch_register_cayman @0x30c7d0
      switch(arch):                            case 4: tdrv_arch_register_mariana@0x30d900
        V2 → ndhal_register_funcs_v2()       → fills tdrv_arch_ops_1 (488 B, ~60 fn-ptrs)
        V3 → ndhal_register_funcs_v3()
        V4 → ndhal_register_funcs_v3()       generic getter (e.g. tdrv_arch_get_num_tpb @0x309050)
             + ndhal_register_funcs_v4()        → tdrv_arch_ops_1.<slot>()  → per-arch leaf
```

Two ordering invariants a reimplementer must preserve:

- **Kernel:** arch must be cached (`narch_init`) **before** DHAL init reads it back (`narch_get_arch`). The probe enforces this by call order (`neuron_pci.c:381-383`); the accessors `BUG_ON` if queried while still `INVALID` (`neuron_arch.c:44`). V4 is built as **V3-base + V4 overrides** (`neuron_dhal.c:44-45`) — register the V3 vtable first, then patch the V4 deltas.
- **Runtime:** because every geometry/offset leaf has **zero direct callers** and is reached *only* through its installed `tdrv_arch_ops_1` slot, `tdrv_arch_ops_init` must run before any `tdrv_arch_get_*`. A getter called before install hits the lazy-init path. The per-slot map (sunda) and the four reloc-installed slots are derived in [TDRV arch-ops Dispatch](../runtime/tdrv-arch-ops.md).

The runtime vtable `tdrv_arch_ops` (488 B) is **not** the 744-byte `soc_struct_t` referenced elsewhere in the binary — these are distinct types; the register functions provably write only the 488 B `tdrv_arch_ops_1`. **(MEDIUM** on the relationship between the two structs; not resolved at this map level.**)**

---

## Cross-References

**Canonical silicon detail (Part I siblings)**
- [Generations, the V2/V3/V4 Enum, and Cloud Naming](generations-enum.md) — the full `neuron_arch` / `al_hal_tpb_arch_type` derivation, marketing-name mapping, emulation gate, single-chip-type assumption
- [PCI Device-ID → Arch Map](pci-device-ids.md) — the device-ID switch (`0x7164/0x7264/0x7364/0x7564/0x7565`), vendor `0x1D0F`, PCI-revision EMU/QEMU markers
- [Per-Generation Hardware Geometry](hw-geometry.md) — the complete per-arch geometry-leaf table (num_tpb / seng / dma-queues / hbm), sunda vs cayman vs mariana offset math
- [Coretype Numbering Reconciliation](coretype-numbering.md) — the `al_hal_tpb_eng_type` ↔ `nrtucode_coretype_t` bridge, per-arch LUTs, TopSP/Q7/CCE off-by-ones
- [Memory Hierarchy, BAR Layout and State Buffer](memory-hierarchy.md) — the full BAR0 CSR atlas (`csr_regions_v4`), the 4-quadrant mesh PA scheme, HBM/SRAM/state-buffer planes

**Runtime and kernel consumers**
- [TDRV arch-ops Dispatch and Sync-Event Accessors](../runtime/tdrv-arch-ops.md) — `tdrv_arch_ops_init`, the 488 B vtable slot map, generic-getter dispatch
- [Per-Arch Device Layer: Geometry and Address Map](../runtime/arch-geometry.md) — the runtime-side per-arch leaves in full
- [DHAL Core (ndhal Vtable-of-Vtables)](../kernel/dhal-core.md) — the kernel's per-generation ops install (V4 = V3-base + V4-overrides)
- [PCI Probe and Device Detection](../kernel/pci-probe.md) — `neuron_pci_set_device_architecture`, `narch_init`, probe ordering
- [back to index](../index.md)
