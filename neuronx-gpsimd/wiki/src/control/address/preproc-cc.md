# PREPROC / Compute-Cluster (CC) Address Subtree

The `PREPROC_0` window is the **Compute-Cluster (CC)** of the Cayman SoC: a
four-core Cadence Tensilica **Vision-Q7** ("GPSIMD") DSP cluster with one shared
control aperture. This page is the byte-exact decomposition of that 1 GiB
window — the `LOCAL_REG` control block, the four Q7 cores (each an IRAM + a DRAM
window), the ten `RESERVED_CC` padding regions, and the arithmetic that derives
every per-core base from a single offset and stride.

Every base/size/stride below is copied byte-exact from the shipped,
RTL-generated address-map artifact, with the tiling, stride and core-count
proofs worked below. All node paths are recovered from the flat address map and
are citeable as binary-derived geometry.

**Primary artifact** (flat address map, RTL-generated):

```
extracted/nested/cayman-arch-regs_tgz/output/address_map/address_map_flat.yaml
```

PREPROC_0 container at **L845**; its 19 children are contiguous **L846–L864**.
Corroborating RTL `\`define` view: `output/address_map/address_map.vh`
(L21476+). LOCAL_REG CSR schema: `csrs/tpb/tpb_xt_local_reg.json` (55,641 bytes
on disk; bound via `address_map_json_xref.yaml:499`
`preproc_0_local_reg: csrs/tpb/tpb_xt_local_reg.json`).

> **NOTE — KEYSTONE: this CC is 4 cores; the TPB POOL cluster is 8.**
> Cayman instantiates the *same* Q7-cluster IP block in two configurations.
> `PREPROC_n` carries **4** Q7 cores (`Q7_CORE0..3`); the
> [TPB POOL cluster](tpb-pool.md) carries **8** (`Q7_CORE0..7`). Both place
> `LOCAL_REG` at `cluster_base + 0x3060000` and `Q7_CORE0_IRAM` at
> `cluster_base + 0x3100000`, both use the identical 1 MiB per-core slot pitch
> and the identical IRAM 0x20000 / DRAM 0x40000 intra-slot layout, and both
> bind `LOCAL_REG` to the same `tpb_xt_local_reg.json`. Do not assume 8 cores
> when targeting PREPROC — the address decode aliases only 4. See
> [§5](#5-4-core-preproc--cc-vs-8-core-tpb-pool) and
> [tpb-pool.md](tpb-pool.md).
---

## 1. PREPROC_0 window: base, size, child census

`PREPROC_0` is a 1 GiB top-level window in the 58-bit SoC address space. It is
located under the SoC master map's PREPROC-CC region; see
[soc-master-map.md](soc-master-map.md) for the parent context (PREPROC_0 SoC
base `0x1200000000`, size `0x40000000`).

| field | value | source |
| ----- | ----- | ------ |
| container base | `0x1200000000` | `address_map_flat.yaml:845` |
| container size | `0x40000000` (1 GiB) | `address_map_flat.yaml:845` |
| container end | `0x1240000000` | computed (`base + size`) |
| child count | **19** (contiguous, L846–L864) | `PREPROC_0_*` row count = 19 |

RTL `\`define` cross-check (`address_map.vh:21476–21477`) — the `58'h` literal
width confirms the 58-bit SoC space:

```verilog
`define PREPROC_0_BASE   58'h000001200000000
`define PREPROC_0_SIZE   58'h000000040000000
```

> **GOTCHA — the flat YAML zero-pads bases to 15 hex digits.** `0x000001200000000`
> is `0x1200000000`; the leading zeros are cosmetic 58-bit-field padding, not
> part of the value. All tables on this page strip them.

The 19 children, byte-exact from `address_map_flat.yaml:846–864`. `off` is the
offset from the container base `0x1200000000`. The window tiles with **zero
gaps and zero overlaps** (proven in [§4](#4-tiling-proof-no-gaps-no-overlaps)).

| L | node | off | absolute base | size | human | CSR schema |
| --- | ---- | --- | ------------- | ---- | ----- | ---------- |
| 846 | `PREPROC_0_RESERVED_CC0` | `+0x0000000` | `0x1200000000` | `0x3060000` | 48.375 MiB | — (pad; == container base) |
| 847 | `PREPROC_0_LOCAL_REG`    | `+0x3060000` | `0x1203060000` | `0x0010000` | 64 KiB | `csrs/tpb/tpb_xt_local_reg.json` |
| 848 | `PREPROC_0_RESERVED_CC1` | `+0x3070000` | `0x1203070000` | `0x0090000` | 576 KiB | — (pad before core array) |
| 849 | `PREPROC_0_Q7_CORE0_IRAM`| `+0x3100000` | `0x1203100000` | `0x0020000` | 128 KiB | — (core0 IRAM) |
| 850 | `PREPROC_0_RESERVED_CC2` | `+0x3120000` | `0x1203120000` | `0x0060000` | 384 KiB | — (pad) |
| 851 | `PREPROC_0_Q7_CORE0_DRAM`| `+0x3180000` | `0x1203180000` | `0x0040000` | 256 KiB | — (core0 DRAM) |
| 852 | `PREPROC_0_RESERVED_CC3` | `+0x31C0000` | `0x12031C0000` | `0x0040000` | 256 KiB | — (pad → core1 slot) |
| 853 | `PREPROC_0_Q7_CORE1_IRAM`| `+0x3200000` | `0x1203200000` | `0x0020000` | 128 KiB | — (core1 IRAM) |
| 854 | `PREPROC_0_RESERVED_CC4` | `+0x3220000` | `0x1203220000` | `0x0060000` | 384 KiB | — (pad) |
| 855 | `PREPROC_0_Q7_CORE1_DRAM`| `+0x3280000` | `0x1203280000` | `0x0040000` | 256 KiB | — (core1 DRAM) |
| 856 | `PREPROC_0_RESERVED_CC5` | `+0x32C0000` | `0x12032C0000` | `0x0040000` | 256 KiB | — (pad) |
| 857 | `PREPROC_0_Q7_CORE2_IRAM`| `+0x3300000` | `0x1203300000` | `0x0020000` | 128 KiB | — (core2 IRAM) |
| 858 | `PREPROC_0_RESERVED_CC6` | `+0x3320000` | `0x1203320000` | `0x0060000` | 384 KiB | — (pad) |
| 859 | `PREPROC_0_Q7_CORE2_DRAM`| `+0x3380000` | `0x1203380000` | `0x0040000` | 256 KiB | — (core2 DRAM) |
| 860 | `PREPROC_0_RESERVED_CC7` | `+0x33C0000` | `0x12033C0000` | `0x0040000` | 256 KiB | — (pad) |
| 861 | `PREPROC_0_Q7_CORE3_IRAM`| `+0x3400000` | `0x1203400000` | `0x0020000` | 128 KiB | — (core3 IRAM) |
| 862 | `PREPROC_0_RESERVED_CC8` | `+0x3420000` | `0x1203420000` | `0x0060000` | 384 KiB | — (pad) |
| 863 | `PREPROC_0_Q7_CORE3_DRAM`| `+0x3480000` | `0x1203480000` | `0x0040000` | 256 KiB | — (core3 DRAM) |
| 864 | `PREPROC_0_RESERVED_CC9` | `+0x34C0000` | `0x12034C0000` | `0x3CB40000`| 972.25 MiB | — (tail pad to window end) |

Census of leaf kinds inside the 1 GiB window: **1× LOCAL_REG**, **4× Q7 cores**
(IRAM + DRAM each = 8 memory leaves), **10× RESERVED_CC** padding regions
(`CC0..CC9`). That is the *entire* contents of PREPROC — see
[§6](#6-engine-sub-block-census-negative-result) for the negative result on
engine sub-blocks.
---

## 2. LOCAL_REG: the cluster control aperture

`PREPROC_0_LOCAL_REG` (`address_map_flat.yaml:847`) is the only child carrying a
`json:` binding. It is the CC's shared control/CSR window — the single aperture
through which firmware programs the four Q7 cores.

| field | value |
| ----- | ----- |
| base | `0x1203060000` (off `+0x3060000`) |
| size | `0x10000` (64 KiB) |
| CSR schema | `csrs/tpb/tpb_xt_local_reg.json` (55,641 B on disk) |
| xref node | `preproc_0_local_reg` (`address_map_json_xref.yaml:499`) |

The schema carries `sw_queue_num` fields for `Q7_0..Q7_3` — i.e. exactly four
sequencer cores — independently corroborating the 4-core nature of this cluster
from the CSR side rather than the geometry side. The schema is shared
byte-for-byte with the TPB POOL cluster's `LOCAL_REG` (same `json:` path), which
is the strongest evidence that PREPROC and POOL are one IP block with a
different core count.

> **NOTE — LOCAL_REG is the only addressable control surface in the CC.** All
> per-core start/stop/queue programming flows through this 64 KiB window; the
> IRAM/DRAM windows are pure memory leaves with no CSR semantics. Interrupt
> trigger routing for the CC is documented separately in
> [../interrupt/peb-cc-topsp-triggers.md](../interrupt/peb-cc-topsp-triggers.md).

> **QUIRK — RTL .vh exposes a *second*, unrelated `PREPROC` aperture.**
> `address_map.vh` also defines `APB_IO_0_USER_PREPROC_0_BASE 32'h06E02000`
> (size `0x80000`). That is a 32-bit APB-side alias in the I/O block, **not**
> the 58-bit CC window described here. Do not conflate the two: the CC is the
> `0x1200000000`-based window; the APB alias is a register-file shadow reachable
> over APB. *(MED / OBSERVED)*

---

## 3. q7_base_offset and the per-core 1 MiB stride

The four Q7 cores live in a regular array. Two numbers fully describe their
placement: the **base offset** of `CORE0_IRAM` within the window, and the
per-core **stride** (slot pitch).

### q7_base_offset

```
Q7_CORE0_IRAM (0x1203100000)  −  PREPROC_0 (0x1200000000)  =  0x3100000
```

→ `q7_base_offset = 0x3100000`.
### Per-core stride (IRAM→IRAM and DRAM→DRAM)

```
CORE1_IRAM − CORE0_IRAM = 0x1203200000 − 0x1203100000 = 0x100000
CORE2_IRAM − CORE1_IRAM = 0x1203300000 − 0x1203200000 = 0x100000
CORE3_IRAM − CORE2_IRAM = 0x1203400000 − 0x1203300000 = 0x100000
```

→ stride = `0x100000` = **1 MiB exactly**, constant across all three
transitions (DRAM-to-DRAM gives the identical 1 MiB). Each Q7 core occupies a
fixed 1 MiB slot.
### Closed-form per-core bases

```
core_i  IRAM_base = 0x1203100000 + i*0x100000          (i ∈ [0,4))
core_i  DRAM_base = 0x1203180000 + i*0x100000   = IRAM_base + 0x80000
```

| i | IRAM base | DRAM base | DRAM − IRAM |
| - | --------- | --------- | ----------- |
| 0 | `0x1203100000` | `0x1203180000` | `0x80000` |
| 1 | `0x1203200000` | `0x1203280000` | `0x80000` |
| 2 | `0x1203300000` | `0x1203380000` | `0x80000` |
| 3 | `0x1203400000` | `0x1203480000` | `0x80000` |

### Intra-slot layout (identical for all 4 cores)

```
+0x00000  IRAM      0x20000 (128 KiB)
+0x20000  RESERVED  0x60000 (384 KiB)   <- RESERVED_CC{2,4,6,8}
+0x80000  DRAM      0x40000 (256 KiB)
+0xC0000  RESERVED  0x40000 (256 KiB)   <- RESERVED_CC{3,5,7,9-prefix}
                    --------
          slot      0x100000 (1 MiB)    -> next core at +0x100000
```

So `DRAM_base − IRAM_base = 0x80000` in every slot (= IRAM `0x20000` + the
following RESERVED `0x60000`). **IRAM = 128 KiB, DRAM = 256 KiB** per core.

> **CORRECTION vs naïve "IRAM and DRAM are adjacent".** They are **not**
> contiguous: a 384 KiB `RESERVED_CC` pad sits between a core's IRAM and its
> DRAM. Computing `DRAM = IRAM + IRAM_size` (`+0x20000`) is wrong; the correct
> delta is `+0x80000`.
---

## 4. Tiling proof (no gaps, no overlaps)

The 19 children exactly tile the 1 GiB window, against
`address_map_flat.yaml:845–864`:

- First child (`RESERVED_CC0`) base `0x1200000000` **== container base**.
- Each child base **== previous child end** (all 18 transitions checked, OK).
- Last child (`RESERVED_CC9`) end `0x12034C0000 + 0x3CB40000 = 0x1240000000`
  **== container end** (`0x1200000000 + 0x40000000`).
- `Σ(child sizes) = 0x40000000` **== container size**.

All four invariants hold. The window is fully decoded with no holes.

---

## 5. 4-core (PREPROC / CC) vs 8-core (TPB POOL)

Q7 cores appear in exactly two cluster families in the whole SoC map:

| family | Q7 cores | instances | LOCAL_REG off | CORE0_IRAM off | slot pitch |
| ------ | -------- | --------- | ------------- | -------------- | ---------- |
| `PREPROC_n` (CC) | **4** (`CORE0..3`) | n=0..3 | `+0x3060000` | `+0x3100000` | 1 MiB |
| `TPB_n_POOL` | **8** (`CORE0..7`) | n=0..7 | `+0x3060000` | `+0x3100000` | 1 MiB |

`PREPROC_0` has 4 `*_IRAM` entries (max core index 3);
`TPB_0_POOL` has 8 (`Q7_CORE0_IRAM` @ `0x2803100000` … `Q7_CORE7_IRAM` @
`0x2803800000` = `+7*0x100000`, `address_map_flat.yaml:947–975`).

**Same IP, different core count.** Both clusters share the LOCAL_REG offset,
CORE0_IRAM offset, 1 MiB slot pitch, IRAM/DRAM intra-slot layout, and the
LOCAL_REG `json:` binding. PREPROC is the POOL Q7-cluster IP instantiated with
4 cores instead of 8.

**What TPB POOL has that PREPROC does NOT.** In the `0x0 … 0x3100000` pre-core
region, POOL places real sub-blocks where PREPROC has only the `RESERVED_CC0`
pad (`address_map_flat.yaml:938–945`):

| TPB POOL front-matter | size | PREPROC equivalent |
| --------------------- | ---- | ------------------ |
| `TPB_0_POOL_IRAM` | `0x8000` | folded into `RESERVED_CC0` |
| `TPB_0_POOL_NX_IRAM` | `0x20000` | folded into `RESERVED_CC0` |
| `TPB_0_POOL_NX_DRAM` | `0x10000` | folded into `RESERVED_CC0` |
| `TPB_0_POOL_PROFILE_CAM` | `0x1000` | folded into `RESERVED_CC0` |
| `TPB_0_POOL_PROFILE_TABLE` | `0x2000` | folded into `RESERVED_CC0` |

PREPROC collapses all of that front-matter into the single `RESERVED_CC0`
(`0x3060000`) pad. So:

> PREPROC = "POOL Q7-cluster minus the pool-engine / Xtensa-NX / profile
> front-matter, minus 4 cores."

See [tpb-pool.md](tpb-pool.md) for the full 8-core sibling breakdown, and
[../../abi/q7-elf-vaddr.md](../../abi/q7-elf-vaddr.md) for the per-core IRAM/DRAM
virtual-address memory model that an ELF must link against.
---

## 6. Engine sub-block census (negative result)

A common expectation is that PREPROC contains sequencer / SDMA / DVE / ACT / PE
sub-engine windows. **It does not.** Grepping the `PREPROC_0_*` children for
`seq | sdma | dve | act | _pe | dma | pool | ring | intc` returns **zero hits**.
The entire `0x40000000` window decomposes only into:

- **1×** `LOCAL_REG` (the CC control CSR aperture),
- **4×** Q7 cores (IRAM + DRAM each), and
- **10×** `RESERVED_CC` padding regions (`CC0..CC9`).

The CC is purely a 4-core Vision-Q7 scalar/SIMD compute cluster with one control
block. Any sequencer / DMA / PE engines live in **other** top-level windows
(`TPB_n`, `APB_SE_n`, `APB_IO_0`), not under PREPROC. *(negative result)*

> **NOTE — RESERVED_CC9 is decode-reserved, not an engine.** The 972.25 MiB tail
> pad (`0x3CB40000`) is simply the unused remainder of the 1 GiB allocation slot
> for the cluster. It is address-decode-reserved; it is not a hidden sub-engine.

---

## 7. RESERVED_CC padding: why, and to what alignment

The ten `RESERVED_CC` regions are pure decode-reserved pads with no `json:`
binding. They exist to align the addressable blocks onto power-of-two
boundaries so the decode logic can use cheap masked-compare matching:

| region | off | size | purpose |
| ------ | --- | ---- | ------- |
| `RESERVED_CC0` | `+0x0000000` | `0x3060000` (48.375 MiB) | front-matter pad → places LOCAL_REG at `+0x3060000` (where POOL's pool/NX/profile blocks live) |
| `RESERVED_CC1` | `+0x3070000` | `0x0090000` (576 KiB)   | pads LOCAL_REG end (`+0x3070000`) up to the core array start `+0x3100000` |
| `RESERVED_CC2` | `+0x3120000` | `0x0060000` (384 KiB)   | between core0 IRAM end and core0 DRAM start (`IRAM 0x20000 → DRAM at +0x80000`) |
| `RESERVED_CC3` | `+0x31C0000` | `0x0040000` (256 KiB)   | from core0 DRAM end to the 1 MiB-aligned core1 slot |
| `RESERVED_CC4` | `+0x3220000` | `0x0060000` (384 KiB)   | core1 IRAM→DRAM intra-slot pad |
| `RESERVED_CC5` | `+0x32C0000` | `0x0040000` (256 KiB)   | core1 DRAM → core2 slot |
| `RESERVED_CC6` | `+0x3320000` | `0x0060000` (384 KiB)   | core2 IRAM→DRAM |
| `RESERVED_CC7` | `+0x33C0000` | `0x0040000` (256 KiB)   | core2 DRAM → core3 slot |
| `RESERVED_CC8` | `+0x3420000` | `0x0060000` (384 KiB)   | core3 IRAM→DRAM |
| `RESERVED_CC9` | `+0x34C0000` | `0x3CB40000` (972.25 MiB)| tail pad: core3 DRAM end → window end `0x1240000000` |

The repeating intra-slot pattern is `RESERVED_CC{2,4,6,8}` = 384 KiB (IRAM→DRAM)
and `RESERVED_CC{3,5,7}` = 256 KiB (DRAM→next slot); `RESERVED_CC9` is the
"DRAM→next slot" pad of core3 extended to absorb the entire unused remainder of
the 1 GiB window.
---

## 8. Reimplementation: per-core address derivation

Closed-form C derivation for the per-core IRAM/DRAM bases. The node names below
are the real flat-YAML paths (`PREPROC_<n>_Q7_CORE<i>_IRAM` /
`..._DRAM`); the constants are the geometry from §3.

```c
/* Cayman PREPROC / Compute-Cluster (CC) per-core address derivation.
 *
 * Source of truth (byte-exact):
 *   address_map_flat.yaml:845   PREPROC_0                base 0x1200000000 size 0x40000000
 *   address_map_flat.yaml:847   PREPROC_0_LOCAL_REG      base 0x1203060000 size 0x00010000
 *   address_map_flat.yaml:849   PREPROC_0_Q7_CORE0_IRAM  base 0x1203100000 size 0x00020000
 *   address_map_flat.yaml:851   PREPROC_0_Q7_CORE0_DRAM  base 0x1203180000 size 0x00040000
 *
 * This CC has exactly 4 cores (NOT 8 — that is the TPB POOL sibling, tpb-pool.md).
 */

#include <stdint.h>
#include <assert.h>

#define PREPROC_0_BASE        0x1200000000ULL  /* L845: container base            */
#define PREPROC_WINDOW_SIZE   0x0040000000ULL  /* L845: 1 GiB                      */

#define CC_LOCAL_REG_OFF      0x03060000ULL    /* L847: control aperture offset    */
#define CC_LOCAL_REG_SIZE     0x00010000ULL    /* L847: 64 KiB                     */

#define CC_Q7_BASE_OFFSET     0x03100000ULL    /* §3: CORE0_IRAM - container        */
#define CC_Q7_CORE_STRIDE     0x00100000ULL    /* §3: 1 MiB slot pitch              */
#define CC_Q7_IRAM_SIZE       0x00020000ULL    /* L849: 128 KiB                     */
#define CC_Q7_DRAM_REL_IRAM   0x00080000ULL    /* §3: DRAM = IRAM + 0x80000          */
#define CC_Q7_DRAM_SIZE       0x00040000ULL    /* L851: 256 KiB                     */

#define CC_Q7_CORE_COUNT      4u               /* §5: PREPROC = 4 (POOL = 8)        */

/* SoC instance bases — die-bit47 / SE-stride (address_map_flat.yaml L845/1938/5023/6116). */
static const uint64_t kPreprocInstanceBase[4] = {
    0x1200000000ULL,   /* PREPROC_0 (die0, SE0)                         */
    0x5200000000ULL,   /* PREPROC_1 (die0, SE1) = +0x4000000000         */
    0x801200000000ULL, /* PREPROC_2 (die1, SE0) = PREPROC_0 | bit47     */
    0x805200000000ULL  /* PREPROC_3 (die1, SE1) = PREPROC_1 | bit47     */
};

/* PREPROC_<n>_Q7_CORE<i>_IRAM base. */
static uint64_t cc_q7_iram_base(unsigned n, unsigned i) {
    assert(n < 4 && i < CC_Q7_CORE_COUNT);
    return kPreprocInstanceBase[n] + CC_Q7_BASE_OFFSET + (uint64_t)i * CC_Q7_CORE_STRIDE;
}

/* PREPROC_<n>_Q7_CORE<i>_DRAM base (= IRAM base + 0x80000). */
static uint64_t cc_q7_dram_base(unsigned n, unsigned i) {
    return cc_q7_iram_base(n, i) + CC_Q7_DRAM_REL_IRAM;
}

/* PREPROC_<n>_LOCAL_REG base. */
static uint64_t cc_local_reg_base(unsigned n) {
    assert(n < 4);
    return kPreprocInstanceBase[n] + CC_LOCAL_REG_OFF;
}
```

Self-check for `n=0` against the YAML:

| call | result | matches |
| ---- | ------ | ------- |
| `cc_q7_iram_base(0,0)` | `0x1203100000` | L849 ✓ |
| `cc_q7_dram_base(0,0)` | `0x1203180000` | L851 ✓ |
| `cc_q7_iram_base(0,3)` | `0x1203400000` | L861 ✓ |
| `cc_q7_dram_base(0,3)` | `0x1203480000` | L863 ✓ |
| `cc_local_reg_base(0)` | `0x1203060000` | L847 ✓ |

---

## 9. The four PREPROC instances (die / quadrant copies)

All four `PREPROC_n` instances share a **byte-identical** 19-child internal
structure (child offsets and sizes match PREPROC_0 exactly; only the container
base differs). Each is a separate SoC-fabric instance:

| instance | base | derivation | location |
| -------- | ---- | ---------- | -------- |
| `PREPROC_0` | `0x1200000000` | — | die0, SE0 (`L845`) |
| `PREPROC_1` | `0x5200000000` | `PREPROC_0 + 0x4000000000` (SE-stride, bit 38) | die0, SE1 (`L1938`) |
| `PREPROC_2` | `0x801200000000` | `PREPROC_0 \| 0x800000000000` (die-bit 47) | die1, SE0 (`L5023`) |
| `PREPROC_3` | `0x805200000000` | `PREPROC_1 \| 0x800000000000` (die-bit 47) | die1, SE1 (`L6116`) |

`PREPROC_0/1` have the die-bit (47) clear; `PREPROC_2/3` have it set. The
within-die SE stride (`PREPROC_1 − PREPROC_0`) is `0x4000000000` (bit 38), the
same SE-instance stride used across the HBM / APB_SE windows in the SoC master
map. See [soc-master-map.md](soc-master-map.md) for the die/SE decode bits.

---

## 10. Out-of-scope: the "engine-index = 5" claim

A firmware/ISA-level "engine index" of 5 is sometimes attributed to PREPROC.
That value is **not present in and not derivable from** any address-map artifact
(`address_map_flat.yaml`, `address_map.vh`, `address_map_json_xref.yaml`,
`cayman_addr_decode*.h/.vh` — searched, no hit). The address map encodes
geometry (bases/sizes), not an engine-ordering enum. An engine index is a
firmware engine-type table entry (the TPB engine-type enumeration), which lives
outside the shipped RTL address map. The `LOCAL_REG` schema corroborates the
4-core nature (it carries `sw_queue_num` for `Q7_0..Q7_3`) but assigns no
numeric engine index either.

> **NOTE — engine-index=5 is plausible but UNCORROBORATED here.** Do not treat
> it as established from the address-map artifact. *(LOW / CARRIED)*

---

## Confidence summary

| claim | confidence | basis |
| ----- | ---------- | ----- |
| PREPROC_0 base `0x1200000000`, size `0x40000000` (1 GiB) | HIGH / OBSERVED | YAML L845 + .vh L21476-77 + tiling proof |
| 19 children tile the window, no gaps/overlaps | HIGH / OBSERVED | numeric tiling check (§4) |
| LOCAL_REG `0x1203060000` / `0x10000` → `tpb_xt_local_reg.json` | HIGH / OBSERVED | YAML L847 + json on disk (55,641 B) + xref L499 |
| q7_base_offset `0x3100000`, stride `0x100000` (1 MiB) | HIGH / OBSERVED | computed from L849/853/857/861 |
| IRAM 128 KiB / DRAM 256 KiB, DRAM = IRAM + `0x80000` | HIGH / OBSERVED | YAML L849/851 + intra-slot arithmetic |
| **exactly 4 Q7 cores** (PREPROC) vs 8 (TPB POOL) | HIGH / OBSERVED | 4 IRAM entries in PREPROC_0; 8 in TPB_0_POOL (L947-975) |
| 10× RESERVED_CC pads, power-of-two alignment | HIGH / OBSERVED | YAML L846/848/850/852/854/856/858/860/862/864 |
| no seq/SDMA/DVE/ACT/PE sub-engine in PREPROC | HIGH / OBSERVED | grep negative result (§6) |
| PREPROC_1/2/3 die/quadrant copies, die-bit 47 | HIGH / OBSERVED | YAML L1938/5023/6116 + bit arithmetic |
| engine-index = 5 | LOW / CARRIED | not in any address-map artifact (§10) |

*Geometry byte-grounded against the NC-v3 (`mariana`/Cayman) shipped address-map
artifact. Provenance: `address_map_flat.yaml` (RTL-generated flat map),
`address_map.vh` (RTL `\`define` view), `address_map_json_xref.yaml` (CSR-schema
binding). NC-v5 placement is INFERRED (not present in this NC-v3 artifact).*

---

### Cross-links

- [tpb-pool.md](tpb-pool.md) — the 8-core Q7 cluster sibling (same IP, +pool/NX/profile front-matter)
- [soc-master-map.md](soc-master-map.md) — top-level SoC windows, die-bit 47 / SE-stride decode
- [../interrupt/peb-cc-topsp-triggers.md](../interrupt/peb-cc-topsp-triggers.md) — CC interrupt trigger routing
- [../../abi/q7-elf-vaddr.md](../../abi/q7-elf-vaddr.md) — per-core IRAM/DRAM virtual-address memory model
