# TPB_0 POOL-Engine Address Subtree (8-core Q7)

This page is the byte-exact decode of the **`TPB_0` POOL sub-tree** — the
POOL-engine **8-core Xtensa Vision-Q7 ("GPSIMD") cluster** inside the Cayman
TPB (Tensor Processing Block). It is the **`NUM_POOL_CORES = 8` source of
truth**: the eight Q7 cores, their IRAM/DRAM windows, the per-core 1 MiB slot
pitch, every RESERVED pad, the `LOCAL_REG` control block, the
`q7_release_run_stall` boot doorbell, and the placement of the whole cluster
inside the 32.0625 GiB `TPB_0` decode window.

Every base/size on this page is taken from the RTL-generated address-map artifact
and derived numerically; nothing is carried from a sibling page.

**Primary artifact (binary-derived, citeable):**
`extracted/nested/cayman-arch-regs_tgz/output/address_map/address_map_flat.yaml`
(the flat leaf table; `name:base:size` per line). Corroborated by the CSR schema
`extracted/nested/cayman-arch-regs_tgz/csrs/tpb/tpb_xt_local_reg.json` (the
`LOCAL_REG` register file) and the node→schema map
`output/address_map/address_map_json_xref.yaml`.

> **Confidence convention.** Claims that depart from the page default
> `HIGH/OBSERVED` carry an explicit tag. Cayman = NC-v3 ("mariana"), and every value below
> is byte-grounded in the NC-v3 artifact (`OBSERVED`). NC-v5 ("maverick")
> equivalents are `INFERRED` (same IP family, same `tpb_xt_local_reg.json`
> shipped in `arch-headers/maverick/`, but per-arch population not re-walked
> here).

---

## 1. Where POOL lives in `TPB_0` — the 32.0625 GiB window

The `TPB_0` container and its POOL children are leaf rows in the flat YAML.
`TPB_0` itself is at `address_map_flat.yaml:866`:

```
L866: TPB_0   base 0x000002000000000   size 0x000000804000000
```

**Size derivation.** The YAML size field is
`0x804000000`, not `0x802000000`:

```
0x804000000 = 34,426,847,232 bytes
            = 34,426,847,232 / 2^30
            = 32.0625 GiB exactly
end = base + size = 0x2000000000 + 0x804000000 = 0x2804000000
```

> **CORRECTION (vs the page brief's hypothesis).** The window is **`0x804000000`
> = 32.0625 GiB**, *not* `0x802000000`. `0x802000000` would be 32.03125 GiB and
> does not appear at `L866`. The literal `0x804000000` is byte-exact in the YAML
> and is reproduced verbatim by `TPB_1` (`L980`, identical size), so this is the
> true per-TPB stride, not a typo.
> **GOTCHA — the cluster pseudo-base is *not* the `TPB_0` base.** The POOL Q7
> cluster IP is laid out relative to **`0x2800000000`**, which is the child
> `TPB_0_TPB_RESERVED_SBUF` (`L873`, a 32 MiB SBUF decode aperture *inside*
> `TPB_0`), **not** the container base `0x2000000000`. That `0x800000000` gap
> (`0x2800000000 − 0x2000000000 = 0x800000000` = a 32 GiB SBUF-aperture window)
> is why the POOL offsets line up with PREPROC's `+0x3060000 / +0x3100000`
> exactly (§6). All absolute addresses here are anchored on the true
> `0x2000000000` `TPB_0` base; the `+0x3xxxxxx` offsets are quoted against the
> `0x2800000000` **cluster pseudo-base** where that aids the PREPROC comparison.

`TPB_0`'s direct children (all `TPB_0_*`) number **112**. Of these, **40** are the
POOL rows (`L938–L977`), all contiguous
leaves with no nesting. The top-level engine census inside the window, in
address order:

| absolute base | size | block | what it is |
|---|---|---|---|
| `0x2000000000` | `0x2000000` (32 MiB) | `TPB_0_STATE_BUF` | on-chip state buffer (SBUF) |
| `0x2002000000` | `0x2000000` (32 MiB) | `TPB_0_TPB_RESERVED10` | pad |
| `0x2004000000` | `0x2000000` (32 MiB) | `TPB_0_STATE_BUF_SCRATCH_RAM` | SBUF scratch RAM |
| `0x2006000000` | `0x3A000000` (928 MiB) | `TPB_0_TPB_RESERVED11` | pad |
| `0x2040000000` | `0x40000000` (1 GiB) | `TPB_0_DGE_MEMORY` | descriptor-gen-engine memory |
| `0x2080000000` | `0x780000000` (30 GiB) | `TPB_0_TPB_RESERVED12` | large decode pad |
| `0x2800000000` | `0x2000000` (32 MiB) | `TPB_0_TPB_RESERVED_SBUF` | SBUF aperture **(= cluster pseudo-base)** |
| `0x2802000000` | `0x400000` (4 MiB) | `TPB_0_PSUM_BUF` | PE-array PSUM accumulator buffer |
| `0x2802400000` | ~`0x200000` | `TPB_0_ACT_*` | ACT engine (Xtensa-NX control block) |
| `0x2802600000` | `0x100000` | `TPB_0_PE_*` | PE-array sequencer (matmul) |
| `0x2802700000` | `0x100000` | `TPB_0_EVT_SEM` | event/semaphore aperture |
| `0x2802800000` | `0x100000` | `TPB_0_SP_*` | SP engine (Xtensa-NX control block) |
| `0x2802900000` | `0x200000` (2 MiB) | `TPB_0_SUNDA_POOL_RSVD` | pre-POOL gap |
| `0x2802B00000` | `0x500000` | `TPB_0_DVE_*` | DVE engine (Xtensa-NX + bank RAMs) |
| **`0x2803000000`** | **`0x8C0000` (8.75 MiB)** | **`TPB_0_POOL_*`** | **POOL engine — THIS PAGE (8× Q7)** |
| `0x28038C0000` | `0x740000` (7.25 MiB) | `TPB_0_TPB_RESERVED0` | `TPB_0` tail pad to window end |

Exactly **five** engines carry an Xtensa `LOCAL_REG` bound to
`tpb_xt_local_reg.json`: **ACT, PE, SP, DVE, POOL**. Of these, **only POOL** has
a Q7 multi-core sub-array; ACT/PE/SP/DVE are single Xtensa-NX + small sequencer
blocks (no `Q7_CORE*` rows).
> **NOTE — SDMA is not under `TPB_0`.** There is no `TPB_0_SDMA*` row. The
> SDMA/UDMA units live under the `APB_SE_n` windows (e.g. `APB_SE_0_SDMA_0_*`),
> not under `TPB_0`. (Negative result.)

The 110 leaf children (112 minus the two nested-container nodes `EVT_SEM` and
`SP`) tile `[0x2000000000, 0x2804000000)` with **zero gaps and zero overlaps**:
first-leaf base == container base, last-leaf end == container end, and Σ(leaf
sizes) == `0x804000000` == container size.
See [`soc-master-map.md`](soc-master-map.md) and
[`pkl-tpb-subtree.md`](pkl-tpb-subtree.md) for the SoC- and TPB-level context.

---

## 2. The full POOL 40-row region table

Byte-exact from `address_map_flat.yaml:938–977` (the YAML zero-pads bases/sizes
to 15 hex digits; leading zeros are cosmetic). `off` = offset from the
`0x2800000000` cluster pseudo-base. Only `POOL_LOCAL_REG` carries a `json:`
binding; every Q7/NX IRAM·DRAM and every `RESERVED*` row is a pure memory/pad
leaf.
| YAML | name | off | absolute base | size | meaning |
|---|---|---|---|---|---|
| L938 | `TPB_0_POOL_IRAM` | `+0x3000000` | `0x2803000000` | `0x8000` (32 KiB) | POOL-engine sequencer IRAM |
| L939 | `TPB_0_POOL_RESERVED0` | `+0x3008000` | `0x2803008000` | `0x18000` (96 KiB) | pad |
| L940 | `TPB_0_POOL_NX_IRAM` | `+0x3020000` | `0x2803020000` | `0x20000` (128 KiB) | Xtensa-NX core IRAM |
| L941 | `TPB_0_POOL_NX_DRAM` | `+0x3040000` | `0x2803040000` | `0x10000` (64 KiB) | Xtensa-NX core DRAM |
| L942 | `TPB_0_POOL_RESERVED2` | `+0x3050000` | `0x2803050000` | `0x10000` (64 KiB) | pad |
| L943 | `TPB_0_POOL_LOCAL_REG` | `+0x3060000` | `0x2803060000` | `0x10000` (64 KiB) | **`csrs/tpb/tpb_xt_local_reg.json`** |
| L944 | `TPB_0_POOL_PROFILE_CAM` | `+0x3070000` | `0x2803070000` | `0x1000` (4 KiB) | profile CAM |
| L945 | `TPB_0_POOL_PROFILE_TABLE` | `+0x3071000` | `0x2803071000` | `0x2000` (8 KiB) | profile table |
| L946 | `TPB_0_POOL_RESERVED3` | `+0x3073000` | `0x2803073000` | `0x8D000` (564 KiB) | pad to 1st core slot |
| L947 | `TPB_0_POOL_Q7_CORE0_IRAM` | `+0x3100000` | `0x2803100000` | `0x20000` (128 KiB) | Q7 core 0 IRAM |
| L948 | `TPB_0_POOL_RESERVED4` | `+0x3120000` | `0x2803120000` | `0x60000` (384 KiB) | pad |
| L949 | `TPB_0_POOL_Q7_CORE0_DRAM` | `+0x3180000` | `0x2803180000` | `0x40000` (256 KiB) | Q7 core 0 DRAM |
| L950 | `TPB_0_POOL_RESERVED4_1` | `+0x31C0000` | `0x28031C0000` | `0x40000` (256 KiB) | pad (slot end `+0x100000`) |
| L951 | `TPB_0_POOL_Q7_CORE1_IRAM` | `+0x3200000` | `0x2803200000` | `0x20000` (128 KiB) | Q7 core 1 IRAM |
| L952 | `TPB_0_POOL_RESERVED5` | `+0x3220000` | `0x2803220000` | `0x60000` (384 KiB) | pad |
| L953 | `TPB_0_POOL_Q7_CORE1_DRAM` | `+0x3280000` | `0x2803280000` | `0x40000` (256 KiB) | Q7 core 1 DRAM |
| L954 | `TPB_0_POOL_RESERVED5_1` | `+0x32C0000` | `0x28032C0000` | `0x40000` (256 KiB) | pad |
| L955 | `TPB_0_POOL_Q7_CORE2_IRAM` | `+0x3300000` | `0x2803300000` | `0x20000` (128 KiB) | Q7 core 2 IRAM |
| L956 | `TPB_0_POOL_RESERVED6` | `+0x3320000` | `0x2803320000` | `0x60000` (384 KiB) | pad |
| L957 | `TPB_0_POOL_Q7_CORE2_DRAM` | `+0x3380000` | `0x2803380000` | `0x40000` (256 KiB) | Q7 core 2 DRAM |
| L958 | `TPB_0_POOL_RESERVED6_1` | `+0x33C0000` | `0x28033C0000` | `0x40000` (256 KiB) | pad |
| L959 | `TPB_0_POOL_Q7_CORE3_IRAM` | `+0x3400000` | `0x2803400000` | `0x20000` (128 KiB) | Q7 core 3 IRAM |
| L960 | `TPB_0_POOL_RESERVED7` | `+0x3420000` | `0x2803420000` | `0x60000` (384 KiB) | pad |
| L961 | `TPB_0_POOL_Q7_CORE3_DRAM` | `+0x3480000` | `0x2803480000` | `0x40000` (256 KiB) | Q7 core 3 DRAM |
| L962 | `TPB_0_POOL_RESERVED7_1` | `+0x34C0000` | `0x28034C0000` | `0x40000` (256 KiB) | pad |
| L963 | `TPB_0_POOL_Q7_CORE4_IRAM` | `+0x3500000` | `0x2803500000` | `0x20000` (128 KiB) | Q7 core 4 IRAM |
| L964 | `TPB_0_POOL_RESERVED8` | `+0x3520000` | `0x2803520000` | `0x60000` (384 KiB) | pad |
| L965 | `TPB_0_POOL_Q7_CORE4_DRAM` | `+0x3580000` | `0x2803580000` | `0x40000` (256 KiB) | Q7 core 4 DRAM |
| L966 | `TPB_0_POOL_RESERVED8_1` | `+0x35C0000` | `0x28035C0000` | `0x40000` (256 KiB) | pad |
| L967 | `TPB_0_POOL_Q7_CORE5_IRAM` | `+0x3600000` | `0x2803600000` | `0x20000` (128 KiB) | Q7 core 5 IRAM |
| L968 | `TPB_0_POOL_RESERVED9` | `+0x3620000` | `0x2803620000` | `0x60000` (384 KiB) | pad |
| L969 | `TPB_0_POOL_Q7_CORE5_DRAM` | `+0x3680000` | `0x2803680000` | `0x40000` (256 KiB) | Q7 core 5 DRAM |
| L970 | `TPB_0_POOL_RESERVED9_1` | `+0x36C0000` | `0x28036C0000` | `0x40000` (256 KiB) | pad |
| L971 | `TPB_0_POOL_Q7_CORE6_IRAM` | `+0x3700000` | `0x2803700000` | `0x20000` (128 KiB) | Q7 core 6 IRAM |
| L972 | `TPB_0_POOL_RESERVED10` | `+0x3720000` | `0x2803720000` | `0x60000` (384 KiB) | pad |
| L973 | `TPB_0_POOL_Q7_CORE6_DRAM` | `+0x3780000` | `0x2803780000` | `0x40000` (256 KiB) | Q7 core 6 DRAM |
| L974 | `TPB_0_POOL_RESERVED10_1` | `+0x37C0000` | `0x28037C0000` | `0x40000` (256 KiB) | pad |
| L975 | `TPB_0_POOL_Q7_CORE7_IRAM` | `+0x3800000` | `0x2803800000` | `0x20000` (128 KiB) | Q7 core 7 IRAM |
| L976 | `TPB_0_POOL_RESERVED11` | `+0x3820000` | `0x2803820000` | `0x60000` (384 KiB) | pad |
| L977 | `TPB_0_POOL_Q7_CORE7_DRAM` | `+0x3880000` | `0x2803880000` | `0x40000` (256 KiB) | Q7 core 7 DRAM |

The 40 POOL rows are fully contiguous: each base equals the previous row's
`base + size`, with no gap and no overlap from `0x2803000000` to `0x28038C0000`
(`span 0x8C0000` = **8.75 MiB**). The next sibling, `TPB_0_TPB_RESERVED0`
(`L978`, `0x740000` = 7.25 MiB), is the `TPB_0` tail pad to the window end — it
is *not* a `POOL_` row.
**RESERVED census (18 POOL pads):**

* **3 front-matter pads** — `RESERVED0` (96 KiB, after `POOL_IRAM`),
  `RESERVED2` (64 KiB, after `NX_DRAM`), `RESERVED3` (564 KiB, the long pad from
  the profile tables up to the first core slot at `+0x3100000`). There is no
  `RESERVED1` — the index skips it (`LOCAL_REG` + `PROFILE_*` occupy that span).
* **16 per-core pads** — for cores 0–6 a pair `RESERVEDk` (384 KiB) + `RESERVEDk_1`
  (256 KiB); core 7 has `RESERVED11` (384 KiB) but **no `_1`** because the
  trailing 256 KiB after `CORE7_DRAM` is absorbed by the `TPB_0` tail pad
  (`RESERVED0` sibling), not by a POOL leaf. Net: 7×2 + 1 + 1 = 16.

The alignment rationale is uniform: each 384 KiB pad lifts `IRAM` (`+0x20000`)
up to the `DRAM` window at `slot+0x80000`, and each 256 KiB pad lifts `DRAM`
(`+0x40000`) up to the next 1 MiB slot boundary — so every Q7 core occupies a
clean, identically-shaped **1 MiB slot** regardless of how much of it is live
RAM. The front-matter `RESERVED3` does the same job at coarse grain: it pads the
profile-table tail up to `+0x3100000` so `CORE0` starts on a 1 MiB boundary.

---

## 3. Per-core geometry — `q7_base_offset`, 1 MiB stride, intra-slot layout

**`q7_base_offset`.** Relative to the `0x2800000000` cluster
pseudo-base:

```
Q7_CORE0_IRAM(0x2803100000) − 0x2800000000 = 0x3100000   ← q7_base_offset
```

This is **identical to PREPROC** (§6). Relative to the true `TPB_0` container
base `0x2000000000` the offset is `0x803100000`
(`= 0x800000000` SBUF-aperture window `+ 0x3100000`).

**Per-core stride / slot pitch.** Across all 7 IRAM→IRAM and
all 7 DRAM→DRAM transitions the delta is constant:

```
CORE[i+1] − CORE[i] = 0x100000 = 1 MiB   (the only stride observed; 14/14 transitions)

core_i IRAM = 0x2803100000 + i*0x100000
core_i DRAM = 0x2803180000 + i*0x100000   (= core_i IRAM + 0x80000)
```

**The eight Q7 core windows (verified row-by-row against L947–977):**

| core | IRAM base | DRAM base | DRAM − IRAM |
|---|---|---|---|
| `CORE0` | `0x2803100000` | `0x2803180000` | `0x80000` |
| `CORE1` | `0x2803200000` | `0x2803280000` | `0x80000` |
| `CORE2` | `0x2803300000` | `0x2803380000` | `0x80000` |
| `CORE3` | `0x2803400000` | `0x2803480000` | `0x80000` |
| `CORE4` | `0x2803500000` | `0x2803580000` | `0x80000` |
| `CORE5` | `0x2803600000` | `0x2803680000` | `0x80000` |
| `CORE6` | `0x2803700000` | `0x2803780000` | `0x80000` |
| `CORE7` | `0x2803800000` | `0x2803880000` | `0x80000` |

**Intra-slot layout (constant for all 8 cores):**

```
slot+0x00000   IRAM     0x20000 (128 KiB)
slot+0x20000   RESERVED 0x60000 (384 KiB)
slot+0x80000   DRAM     0x40000 (256 KiB)
slot+0xC0000   RESERVED 0x40000 (256 KiB)   → slot ends at slot+0x100000 (next core)
```

Q7 IRAM = `0x20000` (128 KiB); Q7 DRAM = `0x40000` (256 KiB); `DRAM = IRAM +
0x80000` in every slot.
> **NOTE.** Live RAM per Q7 core is `128 KiB IRAM + 256 KiB DRAM = 384 KiB`,
> but the *decode footprint* per core is a full 1 MiB (the remaining 640 KiB is
> the two intra-slot RESERVED pads). Reimplementations must keep the 1 MiB pitch
> so core *i*'s windows land at the addresses above, even though more than half
> the slot is unmapped.

---

## 4. The `q7_release_run_stall` boot doorbell

The host kicks a Q7 core out of its power-on run-stall by writing the
**`release_run_stall`** register in the **`q7` bundle** of the POOL `LOCAL_REG`.

**Doorbell absolute address:**

```
POOL_LOCAL_REG(0x2803060000) + q7_bundle(0x3000) + release_run_stall(0x0)
  = 0x2803063000
```

This is corroborated **independently** by the CSR schema
`csrs/tpb/tpb_xt_local_reg.json` (RegFile `SizeInBytes 0x10000`, `AddrWidth 16`
— matching the 64 KiB `LOCAL_REG` window). The schema contains two register
bundles, `nx` at `AddressOffset 0x0` and `q7` at `AddressOffset 0x3000`, mirroring
the address-map's NX-vs-Q7 split. The full **`q7` bundle register map** (byte
offsets from `LOCAL_REG + 0x3000`, extracted directly from the JSON):

| off (from `+0x3000`) | register | purpose |
|---|---|---|
| `+0x00` | `release_run_stall` | **the boot doorbell** — releases run-stall |
| `+0x04` | `start_ctrl` | start control |
| `+0x08` | `run_state_0` | core 0 run-state |
| `+0x0C` | `run_state_1` | core 1 run-state |
| `+0x10` | `run_state_2` | core 2 run-state |
| `+0x14` | `run_state_3` | core 3 run-state |
| `+0x18` | `run_state_4` | core 4 run-state |
| `+0x1C` | `run_state_5` | core 5 run-state |
| `+0x20` | `run_state_6` | core 6 run-state |
| `+0x24` | `run_state_7` | core 7 run-state |
| `+0x28` | `intr_ctrl` | interrupt control |
| `+0x2C` | `intr_info_0` | core 0 interrupt-info |
| `+0x30` | `intr_info_1` | core 1 interrupt-info |
| `+0x34` | `intr_info_2` | core 2 interrupt-info |
| `+0x38` | `intr_info_3` | core 3 interrupt-info |
| `+0x3C` | `intr_info_4` | core 4 interrupt-info |
| `+0x40` | `intr_info_5` | core 5 interrupt-info |
| `+0x44` | `intr_info_6` | core 6 interrupt-info |
| `+0x48` | `intr_info_7` | core 7 interrupt-info |

The presence of **`run_state_0..7`** and **`intr_info_0..7`** — eight per-core
slots each — is an **independent CSR-level proof of an 8-core Q7 cluster**, fully
consistent with the eight `Q7_CORE*` rows in the address map.
> **QUIRK — one doorbell, eight cores.** `release_run_stall` is a *single*
> register at `+0x3000`, not one per core. The host releases the cluster as a
> group (the per-core bit assignment lives inside that register's bitfields; the
> per-core *state* is read back via the eight `run_state_*` registers).
> Contrast the `nx` bundle (`AddressOffset 0x0`): it has its **own** single
> `release_run_stall` and a single scalar `run_state` (no `_N` suffix) — that is
> the lone Xtensa-NX core behind `POOL_NX_IRAM/DRAM`, distinct from the 8-core
> Q7 array.
**Boot/reset flow** — see [`../../uarch/boot-reset.md`](../../uarch/boot-reset.md)
for the full sequence; in brief, host firmware (1) loads each core's program into
its `Q7_COREi_IRAM` window (§3), (2) seeds `Q7_COREi_DRAM`, then (3) writes
`release_run_stall` at `0x2803063000` to take the cores out of stall, and (4)
polls `run_state_0..7` for the running transition. The `LOCAL_REG` register
semantics are detailed in
[`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md).

---

## 5. CSR binding of `LOCAL_REG`

`POOL_LOCAL_REG` (`L943`, `0x2803060000`, `0x10000`) is the only POOL row with a
`json:` field, binding to `csrs/tpb/tpb_xt_local_reg.json` (on disk, 55,641 B).
The node→schema map confirms it at
`address_map_json_xref.yaml:504` →
`tpb_0_pool_local_reg: csrs/tpb/tpb_xt_local_reg.json`.
The **same schema** is bound by `TPB_0_ACT/PE/SP/DVE_LOCAL_REG`
(`json_xref:500–503`) and by `PREPROC_0_LOCAL_REG` (`json_xref:499`): all six
engines share **one** Xtensa control-block schema. The schema declares
`run_state_0..7` (room for 8 cores) regardless of how many a given instance
actually populates — which is why the 4-core PREPROC and the 8-core POOL can
reuse it unchanged. The register layout itself is documented in
[`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md).

---

## 6. The 8-vs-4 diff: TPB POOL = 8 cores, PREPROC/CC = 4 cores

**This page is the `NUM_POOL_CORES = 8` source of truth.** Q7 cores exist in
exactly two cluster families in the whole SoC map:

| family | cores | instances | LOCAL_REG | CORE0_IRAM |
|---|---|---|---|---|
| **`TPB_n` POOL** | `Q7_CORE0..7` (**8**) | 8 (`n=0..7`) | cluster_base `+0x3060000` | cluster_base `+0x3100000` |
| **`PREPROC_n`** | `Q7_CORE0..3` (**4**) | 4 (`n=0..3`) | cluster_base `+0x3060000` | cluster_base `+0x3100000` |

Per-instance: **every** `TPB_n` POOL carries `Q7_CORE7_IRAM`
(**8/8** — all eight TPBs have eight Q7
cores), and PREPROC's max core index is **3** (`PREPROC_0_Q7_CORE0..3` present,
`CORE4` absent). So the POOL cluster is unambiguously **8 cores**, PREPROC is
**4**.
> **CORRECTION — `NUM_POOL_CORES = 8`.** Any prelink or ABI logic that assumes 4
> POOL cores (a PREPROC-shaped count) is wrong for the TPB POOL engine. The
> address map, the CSR `run_state_0..7`, and all eight TPB instances agree on
> **8**. The `NUM_POOL_CORES` prelink check is
> [`../../firmware/pool/prelink-validation.md`](../../firmware/pool/prelink-validation.md);
> the multicore SPMD ABI is [`../../abi/multicore-spmd.md`](../../abi/multicore-spmd.md).

**Same Q7-cluster IP, instantiated with 8 cores instead of 4.**
Both families place `LOCAL_REG` at `cluster_base + 0x3060000` (`0x10000`),
`Q7_CORE0_IRAM` at `cluster_base + 0x3100000`, use the identical 1 MiB slot
pitch and identical intra-slot layout (IRAM `0x20000` / RES `0x60000` / DRAM
`0x40000` / RES `0x40000`), and bind the identical `tpb_xt_local_reg.json`.
Cluster bases: PREPROC `0x1200000000` (so `PREPROC_0_LOCAL_REG = 0x1203060000`,
`PREPROC_0_Q7_CORE0_IRAM = 0x1203100000`); POOL pseudo-base `0x2800000000`.

**What POOL has that PREPROC lacks** — the pre-core front-matter:

| POOL-only block | size | absent in PREPROC |
|---|---|---|
| `POOL_IRAM` | `0x8000` (32 KiB) | POOL-engine sequencer IRAM |
| `POOL_NX_IRAM` | `0x20000` (128 KiB) | Xtensa-NX core IRAM |
| `POOL_NX_DRAM` | `0x10000` (64 KiB) | Xtensa-NX core DRAM |
| `POOL_PROFILE_CAM` / `PROFILE_TABLE` | `0x1000` / `0x2000` | profiling |

PREPROC collapses that entire pre-core region into a single `RESERVED_CC0` pad
(`0x3060000` worth) and has none of `POOL_IRAM/NX_IRAM/NX_DRAM/PROFILE_*`. In
short: **PREPROC = the POOL Q7 cluster minus the POOL/NX/sequencer/profile
front-matter, and with 4 cores instead of 8.** Also, POOL is one of nine
sub-blocks tiling the 32.0625 GiB `TPB_0` window (alongside
STATE_BUF/DGE/PSUM/ACT/PE/EVT_SEM/SP/DVE), whereas PREPROC's Q7 cluster *is* the
whole 1 GiB PREPROC window. The 4-core sibling is documented at
[`preproc-cc.md`](preproc-cc.md).

---

## 7. Reimplementation pseudocode

Real YAML node paths and CSR offsets are named inline so the constants are
traceable to source.

```c
/* ---- POOL Q7 cluster geometry (from address_map_flat.yaml, TPB_0_POOL_*) ---- */

#define TPB0_BASE              0x2000000000ULL  /* flat YAML L866 TPB_0.base       */
#define TPB0_SIZE              0x0804000000ULL  /* flat YAML L866 TPB_0.size = 32.0625 GiB */

/* Cluster pseudo-base = TPB_0_TPB_RESERVED_SBUF (L873). The POOL IP is laid out
 * relative to THIS, not to TPB0_BASE. (TPB0_BASE + 0x800000000 == POOL_PSEUDO_BASE.) */
#define POOL_PSEUDO_BASE       0x2800000000ULL  /* flat YAML L873                  */

#define POOL_Q7_BASE_OFFSET    0x3100000ULL     /* CORE0_IRAM − pseudo (L947)      */
#define POOL_Q7_SLOT_PITCH     0x100000ULL      /* 1 MiB per-core stride           */
#define POOL_Q7_DRAM_DELTA     0x80000ULL       /* DRAM = IRAM + 0x80000           */
#define POOL_Q7_IRAM_SIZE      0x20000ULL       /* 128 KiB (TPB_0_POOL_Q7_COREi_IRAM) */
#define POOL_Q7_DRAM_SIZE      0x40000ULL       /* 256 KiB (TPB_0_POOL_Q7_COREi_DRAM) */

#define NUM_POOL_CORES         8                /* TPB POOL: Q7_CORE0..7 (vs 4 PREPROC) */

/* Per-core IRAM base. core_index in [0, NUM_POOL_CORES).
 * Returns flat-YAML row TPB_0_POOL_Q7_CORE<core_index>_IRAM (L947 + 3*core_index). */
static inline uint64_t pool_q7_core_iram(unsigned core_index) {
    /* core 0 IRAM = 0x2803100000 (L947); +1 MiB per core */
    return POOL_PSEUDO_BASE + POOL_Q7_BASE_OFFSET
         + (uint64_t)core_index * POOL_Q7_SLOT_PITCH;
}

/* Per-core DRAM base = IRAM + 0x80000. Row TPB_0_POOL_Q7_CORE<core_index>_DRAM. */
static inline uint64_t pool_q7_core_dram(unsigned core_index) {
    return pool_q7_core_iram(core_index) + POOL_Q7_DRAM_DELTA;
}
/* pool_q7_core_iram(0) == 0x2803100000 ; pool_q7_core_dram(7) == 0x2803880000 */

/* ---- LOCAL_REG control block (flat YAML L943; csrs/tpb/tpb_xt_local_reg.json) ---- */

#define POOL_LOCAL_REG_BASE    0x2803060000ULL  /* flat YAML L943 (= pseudo + 0x3060000) */
#define LOCAL_REG_Q7_BUNDLE    0x3000           /* tpb_xt_local_reg.json: q7.AddressOffset */
#define Q7_RELEASE_RUN_STALL   0x0              /* q7.release_run_stall.AddressOffset      */
#define Q7_RUN_STATE_0         0x8              /* q7.run_state_0 (run_state_i = +0x8+4*i)  */

/* Boot doorbell: take the 8-core Q7 cluster out of power-on run-stall. */
static inline volatile uint32_t *pool_q7_run_stall_doorbell(void) {
    /* 0x2803060000 + 0x3000 + 0x0 = 0x2803063000 */
    return (volatile uint32_t *)
           (POOL_LOCAL_REG_BASE + LOCAL_REG_Q7_BUNDLE + Q7_RELEASE_RUN_STALL);
}

static inline volatile uint32_t *pool_q7_run_state(unsigned core_index) {
    /* run_state_<core_index> within the q7 bundle */
    return (volatile uint32_t *)
           (POOL_LOCAL_REG_BASE + LOCAL_REG_Q7_BUNDLE
            + Q7_RUN_STATE_0 + 4u * core_index);
}

void pool_boot_release(uint32_t release_mask /* per-core release bits */) {
    /* After all 8 cores' IRAM/DRAM are loaded (pool_q7_core_iram/dram above),
     * one write to 0x2803063000 releases the cluster from run-stall. */
    *pool_q7_run_stall_doorbell() = release_mask;
    for (unsigned i = 0; i < NUM_POOL_CORES; ++i)
        while (((*pool_q7_run_state(i)) & RUN_STATE_RUNNING) == 0)
            ;  /* spin until core i reports running */
}
```

---

## 8. Source anchors

| claim | anchor | confidence |
|---|---|---|
| `TPB_0` base/size `0x2000000000` / `0x804000000` (32.0625 GiB) | `address_map_flat.yaml:866` | `HIGH/OBSERVED` |
| cluster pseudo-base `0x2800000000` (`TPB_0_TPB_RESERVED_SBUF`) | `address_map_flat.yaml:873` | `HIGH/OBSERVED` |
| POOL 40-row table (`TPB_0_POOL_*`) | `address_map_flat.yaml:938–977` | `HIGH/OBSERVED` |
| POOL span `[0x2803000000, 0x28038C0000)` = 8.75 MiB, contiguous | derived from L938–977 | `HIGH/OBSERVED` |
| `q7_base_offset 0x3100000`, 1 MiB stride, DRAM = IRAM+`0x80000` | `address_map_flat.yaml:947–977` | `HIGH/OBSERVED` |
| `POOL_LOCAL_REG 0x2803060000` / `0x10000` | `address_map_flat.yaml:943` | `HIGH/OBSERVED` |
| `LOCAL_REG → tpb_xt_local_reg.json` binding | `address_map_json_xref.yaml:504` | `HIGH/OBSERVED` |
| `q7` bundle `@0x3000`, `release_run_stall@0x0` → doorbell `0x2803063000` | `csrs/tpb/tpb_xt_local_reg.json` | `HIGH/OBSERVED` |
| `run_state_0..7` + `intr_info_0..7` (8-core CSR proof) | `csrs/tpb/tpb_xt_local_reg.json` | `HIGH/OBSERVED` |
| `NUM_POOL_CORES = 8` (TPB POOL) vs 4 (PREPROC); 8/8 TPBs have CORE7 | `address_map_flat.yaml` (POOL/PREPROC rows) | `HIGH/OBSERVED` |
| NC-v5 (maverick) equivalents | `arch-headers/maverick/` ships same schema; not re-walked | `MED/INFERRED` |

**Cross-links:** [`preproc-cc.md`](preproc-cc.md) (4-core sibling) ·
[`../../firmware/pool/prelink-validation.md`](../../firmware/pool/prelink-validation.md)
(`NUM_POOL_CORES` check) ·
[`../../abi/multicore-spmd.md`](../../abi/multicore-spmd.md) (multicore ABI) ·
[`../../uarch/boot-reset.md`](../../uarch/boot-reset.md) (run-stall release) ·
[`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md) (`LOCAL_REG` CSR).
