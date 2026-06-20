# The Unified GPSIMD / Cayman SoC Memory Map

This is the **capstone** of the Part‑13 address sub‑section. The preceding sixteen
pages each dissect one *view* of the address space — the byte‑grounded CAYMAN
(NC‑v3) flat‑YAML chip map, the forward‑looking MAVERICK (NC‑v5) pickled DB, the
host PCIe BAR view, the Q7‑local NX view, and the translation machinery that joins
them. This page **unifies** them into one canonical reference a reimplementer reads
*first*: one region table, the three coordinate systems and how to convert between
them, the generation delta, and the **gen‑invariance thesis** — what stays fixed in
the address layout across silicon generations (the offsets you hard‑code) versus
what moves (the absolute bases you must discover at runtime).

It is a **synthesis** page: it reconciles and presents, it does not re‑derive. Every
headline number below was nonetheless re‑verified this session against the primary
artifacts (`address_map_flat.yaml`, `al_address_map_db.pkl`, the nine LSP ldscripts,
`pcie_host_address_mapping.svh`, `tpb_nx.h`). Confidence is tagged
**HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED** per claim.

> **⚠ WALL — read this before trusting any number.**
> - **CAYMAN / NC‑v3** numbers are **byte‑grounded** in the RTL‑generated
>   `address_map_flat.yaml` (34,858 nodes; filename + banner say "Cayman"). `[HIGH · OBSERVED]`
> - **MAVERICK / NC‑v5** numbers come from the shipped `al_address_map_db.pkl`
>   (323,198 records, under `arch-headers/maverick/`). The DB **structure** (records,
>   roots, view partition, sizes of the top nodes) is **OBSERVED**; anything about
>   what a v5 address *does inside the silicon* is **INFERRED**. Every v5‑interior
>   claim is flagged.
> - The **Q7‑local** (NX 32‑bit) view is **CAYMAN/Sunda‑grounded** (the shipped LSPs,
>   `tpb_nx.h`, the disassembled `neuron_translate`). The register *widths* persist
>   to v5 (the v5 TCAM header is byte‑identical in structure); the v5 *behaviour*
>   beyond those widths is **INFERRED**.

**Primary artifacts** (all under the gitignored `extracted/` tree — use `--no-ignore`
/ absolute paths):

| artifact | view it grounds |
| --- | --- |
| `…/cayman-arch-regs_tgz/output/address_map/address_map_flat.yaml` | CAYMAN flat chip map (34,858 nodes) |
| `…/cayman-arch-regs_tgz/…/pcie_host_address_mapping.svh` | the host BAR → SoC JOIN table |
| `…/cayman-arch-regs_tgz/…/cayman_addr_decode.h` | the 58‑bit decode bitfield |
| `…/arch-headers/maverick/ext/al_address_map_db.pkl` | MAVERICK (v5) address DB (323,198 records) |
| `…/custom_op/lsp_fll_load_cpus/lsp_fll_load_cpu{0..7,_single}/ldscripts/elf32xtensa.x` | Q7 build‑time NX layout |
| `…/custom_op/c10/include/arch-headers/{sunda,cayman}/tpb_nx.h` | the SoC↔Q7 window registers |

---

## 0. Self‑verify — the five headline claims, re‑challenged this session

Before authoring, the five strongest claims were re‑challenged directly against the
artifacts (not carried from a sibling). All five pass.

| # | claim | re‑verification | verdict |
| --- | --- | --- | --- |
| 1 | The unified CAYMAN region table | every base re‑grepped from `address_map_flat.yaml` (HBM_0 L1, PREPROC_0 L845, TPB_0 L866, HBM_1 L1094, HBM_2 L4179, HBM_3 L5272, PCIE_A0_0 L4178, EVT_SEM L903, POOL CORE0 L947/949, LOCAL_REG L943) | ✅ byte‑exact |
| 2 | The 3‑coordinate conversion | host→SoC = per‑window `.svh` table (NOT global add); SoC→Q7 = TCAM `replace[63:20]`, 1 MiB granule; Q7 build = `0x84000000+cpu·0x200000` — all three independently sourced | ✅ round‑trips |
| 3 | CAYMAN↔MAVERICK delta | Cayman HBM `0x1000000000`=64 GiB / TPB `0x804000000`; pkl roots `0x1000000000000000` stride, HBM `0x2000000000`=128 GiB / TPB `0x4000000000`=256 GiB; SDMA token = 0 in pkl | ✅ |
| 4 | The gen‑invariance set | PREPROC CORE0 offset = POOL CORE0 offset = `+0x3100000` from cluster base; LSP `0x84000000`/2 MiB identical all 9; EVT_SEM windows fixed | ✅ |
| 5 | dual‑view totals 323,198 / 34,858 | `pickle.load` len = 323,198; `wc -l` flat = 34,858; by‑view 79,104+244,040+24+24+5+1 = 323,198; json bindings 19,012 | ✅ |

> **CORRECTION carried forward (vs the "57‑bit SoC physical" shorthand).** SX‑ADDR‑18
> and [`soc-q7-translation-windows.md`](soc-q7-translation-windows.md) call the SoC
> space "57‑bit." That is loose. The decode **field is 58 bits** (`[57:0]`,
> `cayman_addr_decode.h`); the **routed** geometry is `[54:0]` (DIE[47] + CAYMAN_ID[53:48]
> + valid[54]); the **populated** map tops out at 2⁵⁴; bits `[57:55]` are
> attribute/valid flags, not address bits. State it as
> **"58‑bit decode field, `[54:0]` routed, 2⁵⁴‑populated."** This page uses that
> precise form throughout; "57‑bit" in the siblings means *the same thing* (bits
> `[57:0]` minus the reserved bit 55). `[HIGH · OBSERVED]` (`soc-master-map.md` §3 /
> `addr-decode.md`).

---

## 1. THE ONE CANONICAL UNIFIED REGION TABLE

This is the page's headline artifact: every major region with its **CAYMAN** (NC‑v3,
byte‑grounded) base/size and, where it differs, the **MAVERICK** (NC‑v5,
pkl‑observed) base/size side by side. Bases are **absolute SoC** addresses unless a
column says otherwise.

The MAVERICK column shows the v5 *root‑relative* base where the v5 partition differs
structurally (the v5 map is split into four 2⁶⁰‑wide access‑domain *views* —
`USER_INT`/`USER_PCIEA`/`SECURE_INT`/`SECURE_PCIEM` at top nibbles `0x0/0x1/0x2/0x3`,
plus `USER_POD` at `0x4`; a v5 leaf base = `view_base | local`). Where the v5 column
reads "*(SENG\_n)*" the block reparented under a Sub‑ENGine layer absent from Cayman.

| region | CAYMAN base | CAYMAN size | MAVERICK (v5) base/size | notes · sibling |
| --- | --- | --- | --- | --- |
| **HBM_0** | `0x0` | `0x1000000000` (64 GiB) | `USER_INT` HBM `0x2000000000` (**128 GiB**) | DRAM stack 0 · `soc-master-map.md` |
| **HBM_1** | `0x4000000000` | `0x1000000000` (64 GiB) | (v5 = 1 stack/die ×2 dies) | DRAM stack 1, die 0 |
| **HBM_2 / HBM_3** | `0x800000000000` / `0x804000000000` | `0x1000000000` ea | (die‑1 copies, bit 47) | DRAM stacks, die 1 |
| **APB_SE_0** | `0x1000000000` | `0xC800000` (200 MiB) | (SDMA/FIS/sprot family) | SDMA root · `pcie-bars.md` §2b |
| **PREPROC_0** | `0x1200000000` | `0x40000000` (1 GiB) | *(SENG\_n preprocessing leaf)* | 4×Q7 CC cluster · `preproc-cc.md` |
| **TPB_0** | `0x2000000000` | `0x804000000` (**32.0625 GiB**) | `USER_INT` TPB `0x4000000000` (**256 GiB**) | tensor block 0 · `tpb-pool.md` |
| ↳ `TPB_0_STATE_BUF` (SBUF) | `0x2000000000` | `0x2000000` (32 MiB) | *(SENG SBUF leaf)* | 128 part × 256 KiB |
| ↳ `TPB_0_TPB_RESERVED_SBUF` | `0x2800000000` | `0x2000000` (32 MiB) | — | the "PSUM_BUF" BAR aperture |
| ↳ `TPB_0_PSUM_BUF` | `0x2802000000` | `0x400000` (4 MiB) | — | 128 part × 16 bank × 1 KiB |
| ↳ `TPB_0_EVT_SEM` | `0x2802700000` | `0x100000` (1 MiB) | same window layout, view‑relative | 256 evt + 256 sem · `evt-sem-regions.md` |
| ↳ `TPB_0_POOL_LOCAL_REG` | `0x2803060000` | `0x10000` (64 KiB) | persists | run‑stall doorbell base · `tpb-pool.md` |
| ↳ `TPB_0_POOL_Q7_CORE0_IRAM` | `0x2803100000` | `0x20000` (128 KiB) | persists (NX‑DRAM 64→128 KiB) | 8 cores, 1 MiB pitch |
| ↳ `TPB_0_POOL_Q7_CORE0_DRAM` | `0x2803180000` | `0x40000` (256 KiB) | persists | per‑core on‑core DRAM |
| **PCIE_A0_0** | `0x400000000000` | `0x400000000000` (64 TiB) | `USER_PCIEA` root `0x1000000000000000` | PCIe outbound · `pkl-pcie-d2d-fabric.md` |
| **INTC_0** | `0x8580000000` | `0x2000` (8 KiB) | (per‑IP embedded `INTC_RDM`) | interrupt ctrl · `pkl-intc-sprot-security.md` |
| **TOP_SP_0** | `0x8280000000` | `0x400000` (4 MiB) | + cmd‑inject tables (v5‑new) | collective seq · `pkl-topsp-coverage.md` |
| **(far) PCIE_M0_0** | `0x20400000000000` | `0x10000000000` (1 TiB) | `SECURE_PCIEM` root `0x3000000000000000` | PCIe inbound |

Two **die** copies repeat the entire die‑0 layout OR'd with **bit 47**
(`0x800000000000`); a 2‑die Cayman thus exposes **4 × 64 GiB = 256 GiB** HBM SPARSE
in SoC space. `[HIGH · OBSERVED]` — the die‑1 copy is numerically exact for every
family (`soc-master-map.md` §2). The full 126‑root / 34,858‑node walk lives in
[`soc-master-map.md`](soc-master-map.md); this table is the *navigable subset* a Q7
reimplementer actually touches.

> **GOTCHA — "TPB_0 base is `0x2000000000`, not `0x2800000000`."** `0x2800000000`
> is the *child* `TPB_0_TPB_RESERVED_SBUF`, not the container. The POOL Q7 cluster
> uses `0x2800000000` only as a **pseudo‑base** for its `+0x3100000` core offset
> (so `POOL_Q7_CORE0_IRAM = 0x2800000000 + 0x3100000 = 0x2803100000`), which is
> *also* `TPB_0 + 0x803100000`. Both readings give the same absolute address; do not
> conflate the pseudo‑base with the container base. `[HIGH · OBSERVED]`
> (`soc-master-map.md` CORRECTION / `tpb-pool.md`).

---

## 2. THE THREE COORDINATE SYSTEMS — and how to convert between them

A GPSIMD reimplementer juggles **three** distinct address namespaces. Confusing them
is the single most common class of bug. Each has its own width, its own zero, and its
own translation step to the next.

| # | coordinate system | width | who uses it | the "zero" |
| --- | --- | --- | --- | --- |
| **(1)** | **SoC‑physical** | 58‑bit decode field (`[54:0]` routed, 2⁵⁴ populated) | the chip fabric; every CSR/DRAM byte | the chip's own address 0 (= HBM_0) |
| **(2)** | **PCIe‑host BAR offset** | 32‑bit within BAR0 / 40‑bit within BAR4 | the host driver's `ioremap`'d MMIO | `(bar, 0)` |
| **(3)** | **Q7‑local NX** | 32‑bit `void*` | a Q7 core dereferencing a pointer | NX `0x0` (reset vector) |

The conversion graph is a **chain**, host at one end, the Q7 core at the other, the
SoC‑physical space in the middle:

```
  (2) PCIe‑host BAR        (1) SoC‑physical 58‑bit          (3) Q7‑local NX 32‑bit
   (bar, offset)   ──►  cayman_address[54:0]   ──►   NX pointer 0x07012345
                  #898                        #910
        ▲  per‑window .svh table         ▲  TCAM {mask,match,replace}
        │  (NOT a global add)            │  (Sunda: {lo,hi} sw‑TLB)
        │                                │
        └─ host ioremap                  └─ Q7 build‑time: LSP places code at
                                            0x84000000+cpu·0x200000  (#911)
```

### 2a. Host BAR → SoC‑physical (the `#898` per‑window join)

There is **no closed‑form arithmetic** — each BAR window carries its own independent
SoC base. The recovered table (`pcie_host_address_mapping.svh`) *is* the translation:

```c
soc = window.cayman_address + (bar_offset - window.host_offset);   /* per‑window base */
```

Two BARs only: **BAR0** = 4 GiB control aperture, **64** windows (every engine CSR
compacted into a dense sub‑4 GiB region); **BAR4** = 256 GiB HBM aperture, **4**
windows (the four 64 GiB stacks flattened back‑to‑back). The host view *flattens* the
sparse SoC layout: `host_offset ≠ cayman_address` low bits for **every** window
except `HBM_0` (offset 0 → SoC 0). `[HIGH · OBSERVED]` (`pcie-bars.md`). Worked rows:

```
host_to_cayman(BAR0, 0xD0000000) -> 0x2000000000   ("TPB_0" → TPB_0_STATE_BUF)
host_to_cayman(BAR4, 0x2000000000) -> 0x800000000000 ("HBM_2", die1, LOCAL=0)
```

The **PEB/DFT** path (`amzn0`/`amzn1` functions, BAR0+BAR3) is a *separate* PCIe
function carrying `CAYMAN_ID = 32` (`0x20`) in bits `[53:48]` — that non‑zero chip‑id
is what routes it apart from the regular (`CAYMAN_ID = 0`) host function.

### 2b. SoC‑physical → Q7‑local NX (the `#910` window translate)

A Q7 core cannot dereference a 58‑bit SoC address directly — it dereferences a 32‑bit
NX pointer that a **hardware window register** maps onto the SoC. Two generations of
that register:

- **Sunda (NC‑v2):** a `{lo, hi}` base‑pair per window. The device caches it in a
  **5‑entry software TLB** (`neuron_translate`): on a HIT it returns
  `window_NX_base + (soc & ~mask)` (pure arithmetic); on a MISS it evicts a
  round‑robin victim (`%3` over the 3 dynamic slots) and **writes the `{lo,hi}` into
  the hardware `MEM_WINDOWn` register** in one act. The three dynamic 16 MiB slots map
  HW `MEM_WINDOW3/5/6` at NX `0x100218/228/230`. `[HIGH · OBSERVED]`
- **Cayman (NC‑v3) and after:** a **40‑window TCAM** at NX register offset `0x2000`,
  each a `{control, mask, match, replace}` struct. `match`/`mask` compare address bits
  `[39:20]` (20‑bit, **1 MiB** granule, fully resizeable); `replace[63:20]` is the SoC
  output base. The same `{lo,hi}` shape feeds the host‑side `program_window`.

```c
/* Cayman TCAM translate, NX 32‑bit -> SoC 58‑bit (one window). */
if ((nx[39:20] & care_mask) == (match & care_mask))          /* TCAM HIT */
    soc = (replace[63:20] << 20) | (nx & ((1<<20)-1));        /* 1 MiB passthrough */
```

The five fixed NX bases (byte‑exact from the `_init_translate_ctx` disasm):

| NX base | kind | SoC target | HW window |
| --- | --- | --- | --- |
| `0x07000000` | dynamic 16 MiB | any (round‑robin) | `MEM_WINDOW3` (`0x100218`) |
| `0x09000000` | dynamic 16 MiB | any | `MEM_WINDOW5` (`0x100228`) |
| `0x0a000000` | dynamic 16 MiB | any (HBM stack / PSUM / EVT_SEM / remote die) | `MEM_WINDOW6` (`0x100230`) |
| `0x80000000` | **pinned** 64 MiB | SBUF `[0x2000000000, 0x2004000000)` | tag = `ENGINE_BASE − 41 MiB` |
| `0x84000000` | **pinned** 64 MiB | HBM hbm_scratch heap | tag = `hbm_scratch & ~0x3FFFFFF` |

Full machinery in [`soc-q7-translation-windows.md`](soc-q7-translation-windows.md).

### 2c. Q7 build‑time placement (the `#911` LSP)

The customop library is **linked** into the low slice of the `0x84000000` hbm_scratch
pinned window, per‑core: `sram0_0_seg org = 0x84000000 + cpu_id·0x200000` (2 MiB
stride; re‑verified all 9 LSPs this session — cpu0 `0x84000000`, cpu3 `0x84600000`,
cpu7 `0x84E00000`, single `0x84000000`/32 MiB). The four XEA3 vector sections go in
`iram0_0_seg org = 0x0, len = 0x1000` (4 KiB). **No stack, no heap in the LSP** —
both are runtime‑carved. `[HIGH · OBSERVED]` ([`lsp-sram-window-map.md`](lsp-sram-window-map.md)).

### 2d. A worked **round‑trip** — host driver to Q7 pointer to SoC byte

Suppose the host wants a Q7 core to read **PSUM byte `0x2802012345`** (PSUM base
`0x2802000000` + `0x12345`), and wants to confirm the host BAR that reaches the same
byte. Trace all three systems:

```
SYSTEM (1) SoC‑physical: target = 0x2802012345
                         decode: LOCAL[46:0]=0x2802012345, DIE[47]=0, CAYMAN_ID=0

(2) host side — which BAR0 window?  (cayman_to_host)
    0x2802012345 ∈ TPB_0_PSUM_BUF window? PSUM lives inside TPB_0 (STATE_BUF aperture
    is 32 MiB @0xD0000000→0x2000000000); the 4 MiB psum_buf is interior. The host
    reaches it through BAR0 0xD0000000 + (0x2802012345 − 0x2000000000) — but note the
    32 MiB STATE_BUF aperture does NOT cover 0x2802xxxxx; PSUM proper is the
    TPB_0_PSUM_BUF/RESERVED_SBUF window @0xD2000000→0x2800000000. Host MMIO =
    0xD2000000 + (0x2802012345 − 0x2800000000) = 0xD2000000 + 0x2012345 = 0xD4012345.
       (per‑window base remap — NEVER a global add)

(3) Q7 side — neuron_translate(ctx, 0x2802012345), cold cache, victim slot 0:
    rec0.ptr  = 0x2802012345 & 0xff000000 = 0x02000000        (lo prefix install)
    *(MEM_WINDOW3_HI 0x10021C) = 0x2802012345 >> 32 = 0x28
    *(MEM_WINDOW3_LO 0x100218) = 0x02000000
    return 0x07000000 + (0x2802012345 & 0x00FFFFFF) = 0x07012345   (NX pointer)

  Q7 then dereferences NX 0x07012345; MEM_WINDOW3 (now holding SoC 0x2802000000)
  re‑expands it to SoC 0x2802012345.  ✓  back to SYSTEM (1).
```

Round‑trip closes: SoC `0x2802012345` ⇄ host `(BAR0, 0xD4012345)` ⇄ Q7 NX
`0x07012345`. `[HIGH · OBSERVED]` for the per‑step arithmetic; the host‑window choice
is the `pcie-bars.md` §2d table (the BAR label `TPB_n_PSUM_BUF` maps to the 32 MiB
RESERVED_SBUF aperture, the true 4 MiB psum CSR being interior).

---

## 3. THE CAYMAN‑vs‑MAVERICK GENERATION DELTA

The v3→v5 address‑space changes a reimplementer must account for. CAYMAN values are
byte‑grounded; MAVERICK values are pkl‑observed for **structure**, the *interior*
behaviour **INFERRED**.

| dimension | CAYMAN (NC‑v3) | MAVERICK (NC‑v5) | confidence |
| --- | --- | --- | --- |
| **HBM per stack** | 64 GiB (`0x1000000000`) | **128 GiB** (`0x2000000000`) | HIGH · OBS (size) / INF (interior) |
| **TPB container** | 32.0625 GiB (`0x804000000`) | **256 GiB** (`0x4000000000`) | HIGH · OBS |
| **top‑level split** | flat — 126 partition roots, one address space | **4 access‑domain views** (`USER/SECURE × INT/PCIE`) + `USER_POD`, 2⁶⁰ strides | HIGH · OBS |
| **DMA naming** | `SDMA` (`APB_SE_n_SDMA_m`) | **`DDMA` / `CDMA` / `UDMA`** (SDMA token count = **0** in pkl) | HIGH · OBS |
| **inter‑chip fabric** | D2D die‑to‑die (`APB_IO_n_USER_FIS_IO_D2D`) | **native UCIE** links (`pkl-pcie-d2d-fabric.md`) | HIGH · OBS (presence) |
| **record/node count** | 34,858 flat‑YAML nodes | **323,198** pkl records (5 views) | HIGH · OBS |
| **CSR coverage** | 19,012 json‑bound / 76 schemas | 23‑field records, 6‑class `type` taxonomy | HIGH · OBS |
| **TOP_SP** | NX seq + RAM + EVT_SEM, no cmd tables | **+ DGE stream‑to‑AXI cmd‑inject tables**, COLLECTIVE_SYNC, SP_SHARED_RAM | HIGH · OBS (`pkl-topsp-coverage.md`) |
| **EVT_SEM** | 6 windows + 7‑leaf table, no counter‑inc bank | **+ `SEMAPHORE_CNTR_INC` @+0x2000** (`0x4000`) | MED · OBS |
| **security domain** | single | `TOP_SP_MISC_USER` **removed** → secure‑only misc (fail‑closed) | HIGH · OBS |

The **64‑die mesh** addressing (CAYMAN_ID `[53:48]` = 2⁶ chips) is shared by both
generations; only the *intra‑chip* layout grows. The v5 HBM/TPB sizes and the 5‑view
partition were re‑verified this session against the pkl directly (roots on a 2⁶⁰
stride; `pickle.load` len = 323,198). `[HIGH · OBSERVED]`

> **WALL — the "39 UCIE links" and the v5 HBM/TPB GiB sizes are sourced to the
> right sibling.** They are **not** in [`pkl-db.md`](pkl-db.md). The v5 HBM 128 GiB /
> TPB 256 GiB sizes are read off the pkl root nodes (also tabulated in
> [`soc-master-map.md`](soc-master-map.md) §8); the UCIE link count lives in
> [`pkl-pcie-d2d-fabric.md`](pkl-pcie-d2d-fabric.md) and the HBM detail in
> `pkl-hbm-subtree.md`. Cite those, not `pkl-db.md`. `[HIGH · OBSERVED]`

---

## 4. THE GEN‑INVARIANCE THESIS FOR ADDRESSES — the reimplementer's keystone

This is the single most useful generalization on the page. A reimplementer can
**hard‑code** the gen‑invariant set and need only **discover at runtime** the small
gen‑variant set. The dividing line is *relative offset (fixed) vs absolute base (moves)*.

### 4a. WHAT STAYS FIXED across generations (hard‑code these)

| invariant | value | why it is fixed | confidence |
| --- | --- | --- | --- |
| Q7 cluster **engine‑relative core offset** | `+0x3100000` to CORE0 IRAM, `+0x3180000` to DRAM, `+0x3060000` to LOCAL_REG | **identical PREPROC vs POOL vs gen** — same Q7 IP block, re‑verified this session (PREPROC CORE0 = POOL CORE0 = `+0x3100000`) | HIGH · OBS |
| per‑core **slot pitch** | 1 MiB (`0x100000`) | the Q7 cluster IP tiles cores on a fixed pitch | HIGH · OBS |
| **run‑stall doorbell pattern** | `q7_release_run_stall = LOCAL_REG + 0x3000` (Cayman abs `0x2803063000`) | the release register's offset within the LOCAL_REG CSR file does not move | HIGH · OBS (`tpb-pool.md`) |
| **EVT_SEM window offsets** | EVENT `+0x0`, SEM_READ `+0x1000`, SET `+0x1400`, INC `+0x1800`, DEC `+0x1C00` | the four‑window aliasing of 256 evt / 256 sem is the data‑plane contract | HIGH · OBS (`evt-sem-regions.md`) |
| **Q7 NX‑local map** | iram0 `0x0`/4 KiB, dataram `[0x80000,0x90000)`, MEM REG `0x100000`, dyn windows `0x07/09/0a000000`, pins `0x80000000`/`0x84000000` | the 32‑bit NX view is per‑core, per‑cluster, AND per‑gen invariant — only the SoC backing moves | HIGH · OBS (SX‑ADDR‑18 §8) |
| **customop link base / stride** | `0x84000000 + cpu·0x200000` (2 MiB) | the hbm_scratch pinned‑window NX base is gen‑stable | HIGH · OBS (`lsp-sram-window-map.md`) |
| **firmware 3‑band VADDR** | `.text 0x01000000` / data `0x02000000` / `.dynamic 0x03000000` | stable across POOL EXTISA_0..3 and CAYMAN/MARIANA/MARIANA_PLUS/SUNDA | HIGH · OBS |
| **window‑register TCAM struct** | `WINDOW_COUNT 40`, `WINDOW_SIZE 0x1c`, base `0x2000`, `replace` mask `0x3ffffff` | byte‑identical Cayman→Mariana→Mariana_plus→Maverick `tpb_nx.h` | HIGH · OBS |
| **decode bitfield** | LOCAL `[46:0]` / DIE `[47]` / CAYMAN_ID `[53:48]` / attrs `[57:55]` | the routing geometry is gen‑stable | HIGH · OBS |

### 4b. WHAT MOVES across generations (discover at runtime)

| variant | CAYMAN | MAVERICK | how to discover |
| --- | --- | --- | --- |
| **cluster SoC base** | `TPB_0 = 0x2000000000` (+ DIE[47] + CAYMAN_ID) | reparented under SENG views | the arch‑regs / address DB for the target gen |
| **HBM / TPB sizes** | 64 GiB / 32.06 GiB | 128 GiB / 256 GiB | the gen's address DB |
| **DMA block names** | `SDMA` | `DDMA`/`CDMA`/`UDMA` | the gen's CSR schema |
| **top‑level partition** | flat 126 roots | 5 access‑domain views | the gen's DB roots |
| **`ENGINE_BASE_ADDR` / `hbm_scratch` tags** | runtime register values | runtime | read `ENGINE_BASE_ADDR_{LO,HI}` at `0x100028/0x10002c`; SBUF tag = that − 41 MiB |
| **POOL NX‑DRAM size** | 64 KiB | 128 KiB | the gen's address map |
| **HBM‑stack arena cap** | 24 GiB | (per‑gen: mariana 36, sunda 16) | `common.h` `HBM_STACK_CAPACITY_GIB` |

**The keystone reading.** A reimplementer who hard‑codes §4a's *offsets* and reads
§4b's *bases* from one DB at init has a forward‑portable engine: the Q7 firmware
references everything **engine‑relative** (its own `ENGINE_BASE_ADDR` + fixed offsets
+ the NX window map), so it runs unchanged when the SoC moves the cluster's absolute
base. The fragile code is anything that *bakes an absolute SoC base*; the robust code
computes every SoC address as `runtime_engine_base + fixed_offset`. `[HIGH · OBSERVED]`
for the invariants; the "forward‑portable" reading is **INFERRED** (architectural,
grounded in the byte‑exact offset equality across PREPROC/POOL and the gen‑stable NX
map).

---

## 5. WHERE DO I FIND X — the region → sibling index

| if you need… | go to | key number |
| --- | --- | --- |
| the **whole chip** map (all 126 roots, 34,858 nodes) | [`soc-master-map.md`](soc-master-map.md) | HBM_0@0x0, TPB_0@0x2000000000 |
| the **decode bitfield** (DIE / CAYMAN_ID / attrs / neighbor route) | [`addr-decode.md`](addr-decode.md) | LOCAL[46:0], EXIT_DIE[51], NBR_ROUTE[52] |
| the **host PCIe BAR** view (driver MMIO → SoC) | [`pcie-bars.md`](pcie-bars.md) | BAR0 4 GiB/64 win, BAR4 256 GiB/4 win |
| the **PREPROC** 4×Q7 collective cluster | [`preproc-cc.md`](preproc-cc.md) | base 0x1200000000, +0x3100000 |
| the **TPB POOL** 8×Q7 cluster + run‑stall | [`tpb-pool.md`](tpb-pool.md) | CORE0 IRAM 0x2803100000, run‑stall 0x2803063000 |
| the **EVT_SEM** 256‑event / 256‑semaphore plane | [`evt-sem-regions.md`](evt-sem-regions.md) | container 0x2802700000, windows +0x1000.. |
| the **MAVERICK pkl** DB (5 views, 23 fields) | [`pkl-db.md`](pkl-db.md) | 323,198 records, user_int 79,104 |
| the **grand coverage tally** (proof residual 0) | [`pkl-topsp-coverage.md`](pkl-topsp-coverage.md) | 323,198 = both axes |
| the **SoC↔Q7 translation** windows (TCAM / sw‑TLB) | [`soc-q7-translation-windows.md`](soc-q7-translation-windows.md) | 40 TCAM windows, 3 dyn + 2 pin |
| the **Q7 build‑time** NX layout (LSP) | [`lsp-sram-window-map.md`](lsp-sram-window-map.md) | iram0 0x0/4 KiB, sram0 0x84000000+cpu·2 MiB |

---

## 6. Confidence ledger

**HIGH · OBSERVED** (re‑verified this session against the artifacts):

- The unified CAYMAN region table — every base re‑grepped from `address_map_flat.yaml`
  (HBM_0/PREPROC_0/TPB_0/HBM_1/2/3/PCIE_A0_0/EVT_SEM/POOL CORE0/LOCAL_REG, line‑cited).
- 34,858 flat nodes (`wc -l`), 19,012 json bindings; pkl 323,198 records, 5 roots on
  2⁶⁰ stride, by‑view 79,104 + 244,040 + 24 + 24 + 5 + 1 = 323,198 (both re‑loaded).
- All 9 LSP origins (iram0 `0x0`/`0x1000`, sram0 `0x84000000 + cpu·0x200000` / 2 MiB,
  single 32 MiB) — re‑grepped byte‑exact.
- PREPROC CORE0 offset = POOL CORE0 offset = `+0x3100000`; LOCAL_REG `+0x3060000`;
  EVT_SEM container `0x2802700000` + windows `+0x1000/1400/1800/1C00` — direct from YAML.
- The 3‑coordinate conversion chain (host per‑window join / TCAM `replace[63:20]` /
  LSP build base) and the worked round‑trip — each step grounded in its sibling.
- The CAYMAN↔MAVERICK delta sizes (64→128 GiB HBM, 32.06→256 GiB TPB, SDMA→DDMA/CDMA/UDMA).
- The gen‑invariant offset set (§4a) — the offset equality across PREPROC/POOL is the
  byte‑exact anchor.

**MED:**

- The `0x2802012345` worked round‑trip's host‑window choice (the BAR `TPB_n_PSUM_BUF`
  label vs the interior 4 MiB psum CSR — the aperture mapping is OBSERVED, the exact
  interior offset is the `pcie-bars.md` §2d reading).
- EVT_SEM `SEMAPHORE_CNTR_INC` v5 window (observed in pkl; per‑field semantics not
  separately schema‑verified).

**INFERRED / WALL:**

- All **MAVERICK / NC‑v5 interior** behaviour — the DB structure is OBSERVED, what a
  v5 address *does in silicon* is INFERRED. The byte‑grounded behavioral reference is
  the CAYMAN flat YAML.
- The "forward‑portable engine" reading of the gen‑invariance thesis (§4b) — grounded
  in the offset equality + the gen‑stable NX map, but a synthesis, not a traced fact.
- The run‑stall register **offset within LOCAL_REG** (`+0x3000`) is carried from
  `tpb-pool.md` #900; the register name is obscured to `release_n` in the shipped
  schema JSON, so the offset is sibling‑anchored, not re‑derived this session.

---

## See also

- [`soc-master-map.md`](soc-master-map.md) — the full 126‑root / 34,858‑node CAYMAN chip map
- [`addr-decode.md`](addr-decode.md) — the 58‑bit decode bitfield (DIE / CAYMAN_ID / neighbor route)
- [`pcie-bars.md`](pcie-bars.md) — the host PCIe BAR0/BAR4 → SoC join
- [`preproc-cc.md`](preproc-cc.md) · [`tpb-pool.md`](tpb-pool.md) — the two Q7 clusters
- [`evt-sem-regions.md`](evt-sem-regions.md) — the EVT_SEM 256/256 data plane
- [`pkl-db.md`](pkl-db.md) · [`pkl-topsp-coverage.md`](pkl-topsp-coverage.md) — the MAVERICK DB + the coverage proof
- [`soc-q7-translation-windows.md`](soc-q7-translation-windows.md) — the SoC↔Q7 window machinery
- [`lsp-sram-window-map.md`](lsp-sram-window-map.md) — the Q7 build‑time NX layout
