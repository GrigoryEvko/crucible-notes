# Host-Side PCIe BAR Address Map

The host driver reaches the Cayman SoC through exactly **two PCIe BARs** on the
main data-plane function: **BAR0** (control / CSR aperture) and **BAR4** (HBM
aperture). Two further BARs belong to a separate PEB/DFT PCIe function. This page
is the host driver's view of the chip, joined to SoC-absolute addresses — i.e.
"given a `(bar, offset)` the driver MMIOs, which Cayman SoC address does the PCIe
inbound-address-translation decoder produce."

All facts here are recovered from the RTL-generated address-map artifacts shipped
in the `cayman-arch-regs` package (header macros + structured YAML + the
SystemVerilog header that *is* the host↔SoC translation table). Claims that depart
from the page default `HIGH/OBSERVED` carry an explicit tag. NC-v3 (Cayman)
byte-grounded; v5 (Maverick) class behavior is INFERRED from the same generator schema.

**Primary artifacts** (all under
`extracted/nested/cayman-arch-regs_tgz/output/address_map/`, gitignored):

| Artifact | Role |
|---|---|
| `pcie_bar_defines.h` | host BAR0/BAR4 (+ PEB) offsets and sizes as C macros |
| `pcie_bar_mapping.yaml` | same, structured; binds `cayman::<name>::base`; tags `type: host / amzn0 / amzn1` |
| `pcie_host_address_mapping.svh` | **THE JOIN** — per window: `host_bar[]`, `host_offset[]`, `cayman_address[]`, `cayman_address_size[]` |
| `pcie_amzn_address_mapping.svh` | PEB/amzn function BAR windows (function-level PCIe view) |
| `cayman_addr_decode.h` | 58-bit SoC address bit-field accessors (DIE / CAYMAN_ID / …) |

Related pages: [`soc-master-map.md`](soc-master-map.md) (SoC-absolute master map
this joins against), [`unified-soc-memory-map.md`](unified-soc-memory-map.md),
[`pkl-pcie-d2d-fabric.md`](pkl-pcie-d2d-fabric.md) (D2D / inter-die fabric),
[`../interrupt/pcie-hbm-tpb-d2d-triggers.md`](../interrupt/pcie-hbm-tpb-d2d-triggers.md)
(interrupt/trigger windows), and
[`../../runtime/aws-hal-q7.md`](../../runtime/aws-hal-q7.md) (host BAR0 CSR
programming via the HAL).

---

## 1. The two host BARs — aperture split

The main host PCIe function exposes the chip through two BARs only; the observed
`host_bar[]` values across all 68 host windows are `0` and `4`
(`pcie_host_address_mapping.svh`).

| BAR | Role | Span | Windows | Source |
|---|---|---|---|---|
| **BAR0** | CONTROL / CSR aperture — every engine and control register file, compacted into a flat sub-4 GiB region | 4 GiB (power-of-two; highest used byte = `INTC_1` end `0xF5204000`) | 64 | `pcie_bar_defines.h:2–101` |
| **BAR4** | HBM aperture — the four 64 GiB HBM stacks flattened into one contiguous window | 256 GiB (`4 × 0x1000000000`) | 4 | `pcie_bar_defines.h:102–109` |

**Why two apertures.** BAR0 is a small, dense, power-of-two control window a
driver maps with a single `ioremap` to reach every engine CSR; BAR4 is the large
prefetchable HBM window for bulk DMA/data. They are decoupled so the control
plane stays a tidy 4 GiB while DRAM gets a flat 256 GiB. **(HIGH/INFERRED — the
sizes/roles are OBSERVED; the "single ioremap" intent is INFERRED.)**

Two further BARs belong to the **PEB** (PCIe-Engine-Block / DFT) functions
`amzn0` / `amzn1`, *not* the main host data-plane function (see §3):
`BAR0` carries `PEB_HSIO2DFT_{0,1}` (4 MiB each), `BAR3` carries
`PEB_APB_IO_{0,1}` (512 MiB each). (`pcie_amzn_address_mapping.svh`)

> NOTE. Every `host_offset` value in `pcie_bar_defines.h` is
> byte-identical to the `host_offset[]` value in `pcie_host_address_mapping.svh`
> (64/64 BAR0 + 4/4 BAR4 match). The `.h`, the `.yaml`, and the `.svh` are three
> renders of one generator table.

---

## 2. The JOIN table — `pcie_host_address_mapping.svh`

This is the page's spine: for every BAR window, the `.svh` carries the host view
(`host_bar`, `host_offset`, size) *and* the SoC-absolute `cayman_address`. The
SoC bases were cross-checked against the flat master map
([`soc-master-map.md`](soc-master-map.md)); all match.

The `cayman_address` field is a 58-bit SoC address. Its high bits decode via
`cayman_addr_decode.h`:

```c
LOCAL        = (addr >> 0)  & 0x7fffffffffff   /* bits[46:0]  */
DIE          = (addr >> 47) & 0x1              /* bit 47      */
CAYMAN_ID    = (addr >> 48) & 0x3f             /* bits[53:48] */
CAYMAN_ID_VALID = (addr >> 54) & 0x1           /* bit 54      */
PCIE_ATTR_RELAXED_ORDERING = (addr >> 56) & 0x1
OK_TO_FAIL   = (addr >> 57) & 0x1
```

For all main-host windows `CAYMAN_ID = 0`; `DIE` (bit 47) selects the second die
copy (`+0x800000000000`). The "die-bit second copy" is the structural pattern
behind every `*_1`/`*_2`/`*_4`/`*_10` window below.

### 2a. BAR4 — HBM aperture (4 windows)

`host_addr` = `host_offset` within BAR4. (`pcie_host_address_mapping.svh:201–216`)

| window | host_bar | host_addr (BAR4) | size | size (human) | cayman_address (SoC) | DIE | LOCAL |
|---|---|---|---|---|---|---|---|
| HBM_0 | 4 | `0x0000000000` | `0x1000000000` | 64 GiB | `0x000000000000` | 0 | `0x0` |
| HBM_1 | 4 | `0x1000000000` | `0x1000000000` | 64 GiB | `0x004000000000` | 0 | `0x4000000000` |
| HBM_2 | 4 | `0x2000000000` | `0x1000000000` | 64 GiB | `0x800000000000` | 1 | `0x0` |
| HBM_3 | 4 | `0x3000000000` | `0x1000000000` | 64 GiB | `0x804000000000` | 1 | `0x4000000000` |

Total BAR4 span = `4 × 0x1000000000 = 0x4000000000` = **256 GiB**, fully packed
(stride == size == `0x1000000000`, zero gaps).

### 2b. BAR0 — APB I/O and APB SE windows

`host_addr` = `host_offset` within BAR0. APB windows expose the **full** SoC
region (BAR size == SoC region size — *not* windowed).
(`pcie_host_address_mapping.svh:1–24`)

| window | host_bar | host_addr (BAR0) | size | size (human) | cayman_address (SoC) | DIE | note |
|---|---|---|---|---|---|---|---|
| APB_IO_0 | 0 | `0x00000000` | `0x20000000` | 512 MiB | `0x8000000000` | 0 | remapped (LOCAL ≠ offset, see §4) |
| APB_IO_1 | 0 | `0x40000000` | `0x20000000` | 512 MiB | `0x808000000000` | 1 | = APB_IO_0 \| bit47 |
| APB_SE_0 | 0 | `0x80000000` | `0xC800000` | 200 MiB | `0x1000000000` | 0 | SDMA/UDMA/INTC CSRs |
| APB_SE_1 | 0 | `0x90000000` | `0xC800000` | 200 MiB | `0x5000000000` | 0 | |
| APB_SE_2 | 0 | `0xA0000000` | `0xC800000` | 200 MiB | `0x801000000000` | 1 | = APB_SE_0 \| bit47 |
| APB_SE_3 | 0 | `0xB0000000` | `0xC800000` | 200 MiB | `0x805000000000` | 1 | = APB_SE_1 \| bit47 |

> QUIRK. **APB_SE stride ≠ size.** SE windows are placed on a
> `0x10000000` (256 MiB) host stride but each is only `0xC800000` (200 MiB),
> leaving a `0x3800000` (56 MiB) unmapped gap between adjacent SE windows in
> BAR0. A driver that walks SE windows by `size` will land mid-gap; walk by
> `host_offset[]` from the `.svh`, never by `prev + size`.

### 2c. BAR0 — PREPROC windows (WINDOWED)

`host_addr` = `host_offset` within BAR0. (`pcie_host_address_mapping.svh:25–40`)

| window | host_bar | host_addr (BAR0) | size | size (human) | cayman_address (SoC) | DIE | leaf schema |
|---|---|---|---|---|---|---|---|
| PREPROC_0 | 0 | `0xC0000000` | `0x34C0000` | 52.75 MiB | `0x1200000000` | 0 | `csrs/tpb/tpb_xt_local_reg.json` |
| PREPROC_1 | 0 | `0xC4000000` | `0x34C0000` | 52.75 MiB | `0x5200000000` | 0 | |
| PREPROC_2 | 0 | `0xC8000000` | `0x34C0000` | 52.75 MiB | `0x801200000000` | 1 | = PP_0 \| bit47 |
| PREPROC_3 | 0 | `0xCC000000` | `0x34C0000` | 52.75 MiB | `0x805200000000` | 1 | = PP_1 \| bit47 |

> QUIRK. **PREPROC is WINDOWED.** The SoC PREPROC region is
> `0x40000000` (1 GiB; master map), but the BAR window exposes only `0x34C0000`
> (52.75 MiB, ≈1/19th). The BAR stride is `0x4000000` (64 MiB) with a `0xB40000`
> gap. The host sees only a CSR/control *slice* of each PREPROC engine, not its
> full SoC footprint.

### 2d. BAR0 — TPB windows (WINDOWED + REMAPPED)

`host_addr` = `host_offset` within BAR0. Each TPB engine contributes two BAR
windows: the `TPB_n` state-buffer slice and a `TPB_n_PSUM_BUF` reserved-SBUF
slice. (`pcie_host_address_mapping.svh:41–104`)

| window | host_bar | host_addr (BAR0) | size | size (human) | cayman_address (SoC) | DIE | SoC leaf it lands on |
|---|---|---|---|---|---|---|---|
| TPB_0 | 0 | `0xD0000000` | `0x2000000` | 32 MiB | `0x2000000000` | 0 | `TPB_0_STATE_BUF` |
| TPB_0_PSUM_BUF | 0 | `0xD2000000` | `0x2000000` | 32 MiB | `0x2800000000` | 0 | `TPB_0_TPB_RESERVED_SBUF` |
| TPB_1 | 0 | `0xD4000000` | `0x2000000` | 32 MiB | `0x3000000000` | 0 | `TPB_1_STATE_BUF` |
| TPB_1_PSUM_BUF | 0 | `0xD6000000` | `0x2000000` | 32 MiB | `0x3800000000` | 0 | |
| TPB_2 | 0 | `0xD8000000` | `0x2000000` | 32 MiB | `0x6000000000` | 0 | |
| TPB_2_PSUM_BUF | 0 | `0xDA000000` | `0x2000000` | 32 MiB | `0x6800000000` | 0 | |
| TPB_3 | 0 | `0xDC000000` | `0x2000000` | 32 MiB | `0x7000000000` | 0 | |
| TPB_3_PSUM_BUF | 0 | `0xDE000000` | `0x2000000` | 32 MiB | `0x7800000000` | 0 | |
| TPB_4 | 0 | `0xE0000000` | `0x2000000` | 32 MiB | `0x802000000000` | 1 | = TPB_0 \| bit47 |
| TPB_4_PSUM_BUF | 0 | `0xE2000000` | `0x2000000` | 32 MiB | `0x802800000000` | 1 | |
| TPB_5 | 0 | `0xE4000000` | `0x2000000` | 32 MiB | `0x803000000000` | 1 | |
| TPB_5_PSUM_BUF | 0 | `0xE6000000` | `0x2000000` | 32 MiB | `0x803800000000` | 1 | |
| TPB_6 | 0 | `0xE8000000` | `0x2000000` | 32 MiB | `0x806000000000` | 1 | |
| TPB_6_PSUM_BUF | 0 | `0xEA000000` | `0x2000000` | 32 MiB | `0x806800000000` | 1 | |
| TPB_7 | 0 | `0xEC000000` | `0x2000000` | 32 MiB | `0x807000000000` | 1 | |
| TPB_7_PSUM_BUF | 0 | `0xEE000000` | `0x2000000` | 32 MiB | `0x807800000000` | 1 | |

> GOTCHA. **The `.svh` window label `TPB_n_PSUM_BUF` is a
> misnomer.** It maps to SoC base `0x2800000000` = `TPB_0_TPB_RESERVED_SBUF`
> (size `0x2000000`, 32 MiB), *not* the master-map leaf literally named
> `TPB_0_PSUM_BUF`, which is at SoC `0x2802000000`, size `0x400000` (a 4 MiB
> sub-block *inside* the reserved SBUF region). So the BAR window is the 32 MiB
> PSUM/reserved-SBUF **aperture**; the true `psum_buf` CSR is a 4 MiB interior of
> it. Likewise `TPB_n` (32 MiB) is only the `STATE_BUF` slice of the
> `0x804000000` (≈32.06 GiB) SoC `TPB_n` container — *not* the whole engine.
> TPB `(TPB + PSUM)` pairs are packed: per-pair stride = `0x4000000` (32+32 MiB),
> zero gap.

### 2e. BAR0 — TOP_SP windows (full aperture, packed)

20 windows, packed (stride == size == `0x400000`), BAR size == SoC region size
(not windowed). `TOP_SP_0..9` are die0, `TOP_SP_10..19` are die1 (bit47).
(`pcie_host_address_mapping.svh:105–184`)

| window | host_addr (BAR0) | cayman_address (SoC) | DIE | | window | host_addr (BAR0) | cayman_address (SoC) | DIE |
|---|---|---|---|---|---|---|---|---|
| TOP_SP_0 | `0xF0000000` | `0x8280000000` | 0 | | TOP_SP_10 | `0xF2800000` | `0x808280000000` | 1 |
| TOP_SP_1 | `0xF0400000` | `0x82C0000000` | 0 | | TOP_SP_11 | `0xF2C00000` | `0x8082C0000000` | 1 |
| TOP_SP_2 | `0xF0800000` | `0x8300000000` | 0 | | TOP_SP_12 | `0xF3000000` | `0x808300000000` | 1 |
| TOP_SP_3 | `0xF0C00000` | `0x8340000000` | 0 | | TOP_SP_13 | `0xF3400000` | `0x808340000000` | 1 |
| TOP_SP_4 | `0xF1000000` | `0x8380000000` | 0 | | TOP_SP_14 | `0xF3800000` | `0x808380000000` | 1 |
| TOP_SP_5 | `0xF1400000` | `0x83C0000000` | 0 | | TOP_SP_15 | `0xF3C00000` | `0x8083C0000000` | 1 |
| TOP_SP_6 | `0xF1800000` | `0x8400000000` | 0 | | TOP_SP_16 | `0xF4000000` | `0x808400000000` | 1 |
| TOP_SP_7 | `0xF1C00000` | `0x8440000000` | 0 | | TOP_SP_17 | `0xF4400000` | `0x808440000000` | 1 |
| TOP_SP_8 | `0xF2000000` | `0x8480000000` | 0 | | TOP_SP_18 | `0xF4800000` | `0x808480000000` | 1 |
| TOP_SP_9 | `0xF2400000` | `0x84C0000000` | 0 | | TOP_SP_19 | `0xF4C00000` | `0x8084C0000000` | 1 |

Each is `0x400000` (4 MiB). Schema: RAM config via
`APB_IO_0_USER_IO_TOP_SP_n_RAM_CONFIG` → `csrs/top_sp/top_sp_ram.json`;
local CSRs → `csrs/tpb/tpb_xt_local_reg.json`. **(MED/INFERRED — the schema
binding is the dominant leaf, not an explicit `.svh` field.)**

### 2f. BAR0 — RDM and INTC windows (top of BAR0)

Small control blocks; both pairs follow the die-bit47 second-copy pattern. The
`INTC_1` end (`0xF5204000`) is the highest byte used in BAR0.
(`pcie_host_address_mapping.svh:185–200`)

| window | host_bar | host_addr (BAR0) | size | size (human) | cayman_address (SoC) | DIE | schema |
|---|---|---|---|---|---|---|---|
| RDM_0 | 0 | `0xF5000000` | `0x100000` | 1 MiB | `0x8580200000` | 0 | `csrs/rdm/rdm_model.json` |
| RDM_1 | 0 | `0xF5100000` | `0x100000` | 1 MiB | `0x808580200000` | 1 | = RDM_0 \| bit47 |
| INTC_0 | 0 | `0xF5200000` | `0x2000` | 8 KiB | `0x8580000000` | 0 | `csrs/intc/intc_*grp_*_unit.json` |
| INTC_1 | 0 | `0xF5202000` | `0x2000` | 8 KiB | `0x808580000000` | 1 | = INTC_0 \| bit47 |

`INTC_0` SoC base `0x8580000000` holds the `TRIGGER_MB` / `MSIX_MB` /
`NOTIFIC_MB_*` mailboxes (8 KiB) used by the host↔engine interrupt/trigger path —
see [`../interrupt/pcie-hbm-tpb-d2d-triggers.md`](../interrupt/pcie-hbm-tpb-d2d-triggers.md).

---

## 3. PEB / amzn (DFT) function BARs

`pcie_amzn_address_mapping.svh` defines a **second** set of windows on the
`amzn0` / `amzn1` PCIe functions (the PEB = PCIe-Engine-Block / DFT path),
distinct from the main host data-plane function. `pcie_bar_mapping.yaml` tags
these under separate documents `type: amzn0` and `type: amzn1` (vs `type: host`),
confirming distinct PCIe functions, each with its own `BAR0` and `BAR3`.

| window | function | host_bar | host_addr | size | size (human) | cayman_address (SoC) | DIE | CAYMAN_ID |
|---|---|---|---|---|---|---|---|---|
| PEB_HSIO2DFT_0 | amzn0 | 0 | `0x0` | `0x400000` | 4 MiB | `0x20008580400000` | 0 | 32 (`0x20`) |
| PEB_APB_IO_0 | amzn0 | 3 | `0x0` | `0x20000000` | 512 MiB | `0x20008000000000` | 0 | 32 (`0x20`) |
| PEB_HSIO2DFT_1 | amzn1 | 0 | `0x0` | `0x400000` | 4 MiB | `0x20808580400000` | 1 | 32 (`0x20`) |
| PEB_APB_IO_1 | amzn1 | 3 | `0x0` | `0x20000000` | 512 MiB | `0x20808000000000` | 1 | 32 (`0x20`) |

SoC decode of the PEB high bits (via `cayman_addr_decode.h`):

- `PEB_APB_IO_0` `0x20008000000000` → `LOCAL = 0x8000000000` (== `APB_IO_0` local
  base), `DIE = 0`, `CAYMAN_ID = 32` (`0x20`, bits[53:48]).
- The `amzn0`/`amzn1` split toggles `DIE` (bit 47); both functions carry the same
  `CAYMAN_ID = 32`. The non-zero `CAYMAN_ID` is what routes the PEB/DFT path apart
  from the regular (`CAYMAN_ID = 0`) host function.

> NOTE (MED/INFERRED). The `CAYMAN_ID = 32` decode is exact and unambiguous from
> the header, but its *semantic* role as a DFT-path selector is inferred, not
> stated in the artifacts. `HSIO2DFT` = high-speed-IO-to-DFT bridge
> (design-for-test); `PEB_APB_IO` is the same 512 MiB APB I/O fabric reached via
> the engine-block function (schema leaves bind the same `csrs/*` as host
> `APB_IO`). Each PEB window starts at `host_offset = 0` of its own BAR — there is
> no compaction here, one window per BAR.

---

## 4. Aperture-vs-windowed taxonomy

The decisive distinction for a reimplementor is whether a BAR window exposes the
*whole* SoC region or only a control slice, and whether `host_offset` equals the
SoC `LOCAL` low bits.

- **FULL aperture** (BAR window size == SoC region size; whole region exposed):
  `APB_IO_{0,1}`, `APB_SE_{0..3}`, `TOP_SP_{0..19}`, `RDM_{0,1}`, `INTC_{0,1}`.
- **WINDOWED subset** (BAR window size ≪ SoC region size; only a control slice):
  - `PREPROC_{0..3}`: `0x34C0000` (52.75 MiB) of a `0x40000000` (1 GiB) SoC region.
  - `TPB_{0..7}`: `0x2000000` (32 MiB) `STATE_BUF` slice of a `0x804000000`
    (≈32.06 GiB) SoC region, plus a separate `0x2000000` PSUM/reserved-SBUF window.
- **COMPACTED + REMAPPED** (`host_offset` ≠ SoC base low bits; host view flattens
  sparse SoC): *all* of BAR0 (control windows repacked into a dense sub-4 GiB
  region from SoC bases scattered across `0x10_0000_0000 .. 0x80_8580_0000`) and
  BAR4 `HBM_{0..3}` (four 64 GiB stacks flattened from die-split SoC bases into one
  contiguous `0 .. 256 GiB` host region).

> QUIRK — the non-identity windows are the interesting ones.
> Even `HBM_0` and the "first" APB window are not a trivial identity: only
> `HBM_0` (offset 0 → SoC 0) coincides; **for every other window
> `host_offset ≠ cayman_address` low bits.** Examples from the `.svh`:
> - `APB_IO_0`: `host_offset = 0x0` but `cayman_address = 0x8000000000`
>   (`LOCAL = 0x8000000000`). Even the "first" BAR0 window is fully remapped.
> - `TPB_0`: `host_offset = 0xD0000000` → SoC `0x2000000000`.
> - `INTC_1`: `host_offset = 0xF5202000` → SoC `0x808580000000` (die1).
> - `HBM_2`: `host_offset = 0x2000000000` → SoC `0x800000000000` (die1, `LOCAL = 0`).
>
> The translation is *not* a single global add — each window has its own base.
> Never assume `cayman_address = bar_base + host_offset`; always look the window up.

> CORRECTION — the BAR0 highest byte in GiB. The BAR0 highest byte (`INTC_1` end
> `0xF5204000`) is **`0xF5204000 / 2^30 = 3.830 GiB`**, not "~3.92 GiB" — that
> figure divides by `10^9` (→ ≈4.11 GB), or is a rounding estimate. The byte value
> `0xF5204000` itself is correct and < 4 GiB, so BAR0 rounding up to a 4 GiB
> power-of-two is unaffected.

### HBM compaction detail

BAR4 erases the SoC sparsity. In SoC space the two HBM stacks per die are
`0x4000000000` (256 GiB) apart and the two dies are `0x800000000000` (128 TiB)
apart (bit 47) — a deliberately sparse layout. BAR4 lays the four 64 GiB stacks
back-to-back at `0 / 64 / 128 / 192 GiB`. Sizes are byte-identical
(`0x1000000000` each), so the flattening is a pure base remap, not a resize. The
PCIe inbound-address-translation hardware re-expands a BAR4 offset into the
correct sparse SoC HBM address (die-bit + intra-die stride). This confirms
[`soc-master-map.md`](soc-master-map.md) and
[`unified-soc-memory-map.md`](unified-soc-memory-map.md).

---

## 5. Host ↔ Cayman address translation (C pseudocode)

The `.svh` tables (`host_bar[]` / `host_offset[]` / `cayman_address[]` /
`cayman_address_size[]`) *are* the translation table in source form. A reimplementor
loads them into a small array and does a window lookup — there is no closed-form
arithmetic because each window has an independent SoC base.

```c
/* Recovered window descriptor, one per entry of pcie_host_address_mapping.svh.
 * Field names mirror the .svh associative arrays exactly. */
typedef struct {
    const char *name;          /* e.g. "TPB_0", "HBM_2", "INTC_1"             */
    uint8_t     host_bar;      /* host_bar[name]      : 0 (control) or 4 (HBM) */
    uint64_t    host_offset;   /* host_offset[name]   : offset within the BAR  */
    uint64_t    cayman_address;/* cayman_address[name]: 58-bit SoC-absolute base */
    uint64_t    size;          /* cayman_address_size[name]                    */
} cayman_bar_window_t;

/* Forward: host MMIO (bar, off) -> SoC-absolute cayman_address.
 * Returns 0 on success, -1 if (bar, off) falls outside every known window. */
int host_to_cayman(const cayman_bar_window_t *tbl, size_t n,
                   uint8_t bar, uint64_t off, uint64_t *soc_out)
{
    for (size_t i = 0; i < n; i++) {
        const cayman_bar_window_t *w = &tbl[i];
        if (w->host_bar != bar)               continue;
        if (off <  w->host_offset)            continue;
        if (off >= w->host_offset + w->size)  continue;   /* half-open [base,base+size) */
        /* Per-window base remap: NOT a global add. Re-insert this window's SoC base. */
        *soc_out = w->cayman_address + (off - w->host_offset);
        return 0;
    }
    return -1;   /* unmapped: a gap (e.g. the 56 MiB APB_SE inter-window pad) */
}

/* Inverse: SoC-absolute cayman_address -> host (bar, off).
 * The table base already carries DIE/CAYMAN_ID, so a direct range test suffices. */
int cayman_to_host(const cayman_bar_window_t *tbl, size_t n,
                   uint64_t soc, uint8_t *bar_out, uint64_t *off_out)
{
    for (size_t i = 0; i < n; i++) {
        const cayman_bar_window_t *w = &tbl[i];
        if (soc <  w->cayman_address)               continue;
        if (soc >= w->cayman_address + w->size)     continue;
        *bar_out = w->host_bar;
        *off_out = w->host_offset + (soc - w->cayman_address);
        return 0;
    }
    return -1;   /* SoC address not reachable through any BAR window */
}

/* Worked examples grounded in pcie_host_address_mapping.svh:
 *   host_to_cayman(BAR0, 0xD0000000)        -> 0x2000000000      ("TPB_0")
 *   host_to_cayman(BAR0, 0xD0000000+0x10)   -> 0x2000000010      (interior, STATE_BUF)
 *   host_to_cayman(BAR0, 0xD2000000)        -> 0x2800000000      ("TPB_0_PSUM_BUF" = RESERVED_SBUF)
 *   host_to_cayman(BAR4, 0x2000000000)      -> 0x800000000000    ("HBM_2", die1, LOCAL=0)
 *   host_to_cayman(BAR0, 0x40000000)        -> 0x808000000000    ("APB_IO_1", die1)
 *   cayman_to_host(0x808580000000)          -> (BAR0, 0xF5202000)("INTC_1")
 *
 * Gap example (returns -1):
 *   host_to_cayman(BAR0, 0x8C800000) -> -1   (APB_SE_0 ends at 0x80000000+0xC800000
 *                                             = 0x8C800000; next SE starts 0x90000000)
 */
```

> NOTE. The 58-bit `cayman_address` can additionally carry
> `PCIE_ATTR_RELAXED_ORDERING` (bit 56) and `OK_TO_FAIL` (bit 57) from
> `cayman_addr_decode.h`. The `.svh` base values in this page have those bits
> clear; a driver that ORs them in for posted-write tuning must mask them off
> before the range test in `cayman_to_host()`. See
> [`pkl-pcie-d2d-fabric.md`](pkl-pcie-d2d-fabric.md) for the inter-die/D2D
> attribute path and [`../../runtime/aws-hal-q7.md`](../../runtime/aws-hal-q7.md)
> for how the HAL programs BAR0 CSRs through these windows.

---

## 6. Confidence summary

| Claim | Confidence | Basis |
|---|---|---|
| BAR0 = control (4 GiB, 64 windows), BAR4 = HBM (256 GiB, 4 windows) | HIGH/OBSERVED | `pcie_bar_defines.h`, `.svh` host_bar ∈ {0,4} |
| Every BAR0/BAR4 `host_offset` + size byte-exact; `.h` == `.svh` (64/64 + 4/4) | HIGH/OBSERVED | header vs `.svh` cross-check |
| SoC-absolute base per window | HIGH/OBSERVED | `.svh` `cayman_address[]` vs master map |
| BAR4 HBM compaction (host 0/64/128/192 GiB ↔ sparse die-split SoC) | HIGH/OBSERVED | numeric decode via `cayman_addr_decode.h` |
| 256 GiB BAR4 fully packed; BAR0 highest byte `0xF5204000` (3.830 GiB) | HIGH/OBSERVED | arithmetic re-check |
| WINDOWED: PREPROC `0x34C0000`≪`0x40000000`; TPB `0x2000000`≪`0x804000000` | HIGH/OBSERVED | header + master map |
| TPB remap: `TPB_n`→STATE_BUF; `TPB_n_PSUM_BUF`→RESERVED_SBUF | HIGH/OBSERVED | master-map grep at those bases |
| Die-bit (47) second copy for `*_1/_2/_4/_10`, HBM_2/3 | HIGH/OBSERVED | decode |
| PEB function BARs (amzn0/amzn1, BAR0+BAR3, CAYMAN_ID=32) | HIGH/OBSERVED | `pcie_amzn_address_mapping.svh`, `.yaml` `type:` |
| PEB `CAYMAN_ID=32` as DFT-path selector (functional meaning) | MED/INFERRED | exact decode, role inferred |
| Per-window representative CSR schema (dominant leaf, not an explicit field) | MED/INFERRED | master-map / json_xref grep |
| Maverick (NC-v5) follows the same generator schema | LOW/INFERRED (CARRIED) | class behavior; Cayman is the byte-grounded reference |
