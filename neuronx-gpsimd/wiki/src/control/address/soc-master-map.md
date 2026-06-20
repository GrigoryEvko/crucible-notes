# [P13.1] Cayman SoC Top-Level Address Map (master)

This is the keystone address-map page for the Cayman SoC (Annapurna Trainium,
NC-v3). Every other page in Part 13 — the bitfield decoder, the per-subtree
maps, the CSR-schema xref, the pickled DB cross-walk — locates against the
absolute bases tabulated here. The numbers come from one RTL-generated artifact:

```
extracted/nested/cayman-arch-regs_tgz/output/address_map/address_map_flat.yaml
```

a 34,858-line flat YAML emitted by the arch-regs toolchain (the banner
`Generated for Cayman by the arch-regs toolchain` appears in the sibling
`address_map_json_xref.yaml` and `*.vh`). Every base, size, count, and line
number below was re-parsed and re-checked numerically by this page; nothing is
carried unverified. Confidence is tagged **HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED**
per claim. Cayman = NC-v3 is byte-grounded by the artifact's own filename and
banner; the one place a different arch (MAVERICK / NC-v5) intrudes is the
pickled DB, flagged below.

> **NOTE.** "Cayman" is the AWS Annapurna Trainium2-class host SoC. The
> GPSIMD/Vision-Q7 engine documented in the rest of this wiki is one leaf of the
> `TPB_n` subtree (`TPB_n_..._POOL`, the Q7 cluster). This page is the *whole
> chip* map; the Q7 view is a window inside `TPB_0` (see
> [tpb-pool.md](tpb-pool.md)).

---

## 1. Flat-YAML schema

`address_map_flat.yaml` is a *flat* YAML sequence — one node per line, each a
flow-mapping. There are exactly two key-sets and no others (verified by
re-parsing all 34,858 lines with a single regex; 0 non-matching lines):

```yaml
- { name: <STR>, base: 0x<HEX>, size: 0x<HEX> }                          # container / RAM / reserved
- { name: <STR>, base: 0x<HEX>, size: 0x<HEX>, json: csrs/<sub>/<u>.json }  # CSR leaf
```

| Field | Type | Meaning |
| ----- | ---- | ------- |
| `name` | str | Hierarchical node name, `_`-joined from the SoC tree path and UPPERCASED, with a numeric instance index appended per array element (e.g. `APB_SE_0_SDMA_0_UDMA_M2S`). Unique per entry. |
| `base` | int | **ABSOLUTE** SoC byte address (not a parent-relative offset). Hex, zero-padded to 15 digits in the file; the padding is cosmetic and `int(x,16)` parses it cleanly. |
| `size` | int | Window size in **bytes**. For a container, spans all children; for a leaf, the register/memory block size. |
| `json` | str | OPTIONAL. Present only on CSR leaves. Path relative to the package root, `csrs/<subsystem>/<unit>.json`. Absence == pure memory / reserved / pure container. |

**HIGH/OBSERVED.** `base` is absolute. There is no parent-relative offset field
in the flat view; the tree relationship survives only in the name prefix and in
the un-flattened sources (§4). Stream this file — do not slurp; it is 4.7 MB of
single-line entries.

---

## 2. Top-level region table (absolute bases + sizes)

These are the **partition roots** of the SoC map: nodes whose name has no parent
prefix among the 34,858 entries. There are **126** of them (re-derived by the
prefix test, not carried). The named roots tile each die's lower bands; the gaps
between named windows are explicitly filled by `RESERVED_n` roots (22 of them).
Bases/sizes below are byte-exact from the file, with the file line number cited.

### Die 0 — local band (bit 47 = 0)

| name | absolute base | size | size (human) | line | schema |
| ---- | ------------- | ---- | ------------ | ---- | ------ |
| `HBM_0` | `0x000000000000000` | `0x1000000000` | 64 GiB | L1 | (none — pure DRAM) |
| `APB_SE_0` | `0x000001000000000` | `0xC800000` | 200 MiB | L2 | (container — SDMA/FIS/sprot leaves) |
| `PREPROC_0` | `0x000001200000000` | `0x40000000` | 1 GiB | L845 | (container)\* |
| `RESERVED_0` | `0x000001240000000` | `0xDC0000000` | 55 GiB | — | (reserved fill) |
| `TPB_0` | `0x000002000000000` | `0x804000000` | 32.0625 GiB | L866 | (container)\*\* |
| `RESERVED_1` | `0x000002804000000` | `0x7FC000000` | 31.9375 GiB | — | (reserved fill) |
| `TPB_1` | `0x000003000000000` | `0x804000000` | 32.0625 GiB | — | (container)\*\* |
| `RESERVED_2` | `0x000003804000000` | `0x7FC000000` | 31.9375 GiB | — | (reserved fill) |
| `HBM_1` | `0x000004000000000` | `0x1000000000` | 64 GiB | L1094 | (none — pure DRAM) |
| `APB_SE_1` | `0x000005000000000` | `0xC800000` | 200 MiB | — | (container) |
| `PREPROC_1` | `0x000005200000000` | `0x40000000` | 1 GiB | — | (container)\* |
| `TPB_2` / `TPB_3` | `0x...6000000000` / `0x...7000000000` | `0x804000000` | 32.0625 GiB | — | (container)\*\* |
| `APB_IO_0` | `0x000008000000000` | `0x20000000` | 512 MiB | L2187 | (container — D2D/PCIe/io-fabric leaves) |
| `TOP_SP_0`..`TOP_SP_9` | `0x000008280000000` + n·`0x40000000` | `0x400000` | 4 MiB each | L3950 | (container)\*\*\* |
| `INTC_0` | `0x000008580000000` | `0x2000` | 8 KiB | L4160 | (container — interrupt controller) |
| `HOST_MSIX_DOORBELL_0` | `0x000008580100000` | `0x100000` | 1 MiB | L4172 | (doorbell window) |
| `RDM_0` | `0x000008580200000` | `0x100000` | 1 MiB | L4173 | (container — `csrs/rdm/rdm_model.json`) |
| `RESERVED_6` | `0x000008580300000` | `0xA7FD00000` | ≈42 GiB | — | (reserved fill) |
| `EVT_ACCEL_CLR_0` | `0x000009000000000` | `0x4` | 4 B | L4175 | (event-accelerator clear MMIO) |
| `EVT_ACCEL_SET_0` | `0x000009000000004` | `0x4` | 4 B | L4176 | (event-accelerator set MMIO) |
| `RESERVED_7` | `0x000009000000008` | `0x3F6FFFFFFFF8` | ≈63 TiB | — | (reserved fill to PCIe band) |
| `PCIE_A0_0` | `0x000400000000000` | `0x400000000000` | 64 TiB | L4178 | (PCIe outbound aperture, die 0) |

\* `PREPROC_n` leaf CSR example: `PREPROC_0_LOCAL_REG → csrs/tpb/tpb_xt_local_reg.json`.
See [preproc-cc.md](preproc-cc.md).
\*\* `TPB_n` contains `STATE_BUF`, `PSUM_BUF`, the Q7/POOL cluster, `DGE_MEMORY`,
and `TPB_n_TPB_RESERVED_SBUF` (see CORRECTION below). Leaf CSR example
`TPB_0_ACT_LOCAL_REG → csrs/tpb/tpb_xt_local_reg.json` at `0x2802460000` size
`0x10000`. See [tpb-pool.md](tpb-pool.md).
\*\*\* `TOP_SP_n` is a 4 MiB control window; its RAM config sits under
`APB_IO_0_USER_IO_TOP_SP_n_RAM_CONFIG → csrs/top_sp/top_sp_ram.json`. 10 per die
(20 total).

### Die 1 — same band OR'd with bit 47 (`0x800000000000`)

The entire die-0 layout repeats at `die0_base | 0x800000000000`. Verified
numerically for every family: e.g. `APB_IO_1 = APB_IO_0 | bit47 = 0x808000000000`
(exact). `HBM_2 = 0x800000000000`, `HBM_3 = HBM_1 | bit47 = 0x804000000000`,
`TPB_4..7`, `APB_SE_2/3`, `PREPROC_2/3`, `TOP_SP_10..19`, `INTC_1`, `RDM_1`,
`EVT_ACCEL_*_1`, `PCIE_A0_1 = 0xC00000000000`.

So a 2-die Cayman exposes **4 × 64 GiB HBM = 256 GiB DRAM** (2 stacks per die),
with the four stacks SPARSE in SoC space (`0x0`, `0x40_0000_0000`,
`0x800_0000_0000`, `0x804_0000_0000`).

### Far apertures (above the local/die bands)

| name | absolute base | size | size (human) | role |
| ---- | ------------- | ---- | ------------ | ---- |
| `PCIE_B0_0`..`PCIE_B0_3` | `0x10000000000000` + n·`0x4000000000000` | `0x4000000000000` | 1 PiB each | PCIe BAR-region apertures |
| `PEB_APB_IO_0/1` + 16× `PEB_APB_IO_BCAST_*` | `0x20008000000000` … | `0x20000000` | 512 MiB each | PCIe-Engine-Block control mirror |
| `PEB_SP_0`..`PEB_SP_19` | `0x20008280000000` + … | `0x400000` | 4 MiB each | PEB TOP_SP mirror |
| `PEB_HSIO2DFT_0/1` | `0x20008580400000` / `0x20808580400000` | `0x400000` | 4 MiB | HSIO-to-DFT engine block |
| `PCIE_M0_0/1` | `0x20400000000000` / `0x20C00000000000` | `0x10000000000` | 1 TiB each | PCIe inbound (memory) aperture |
| `RESERVED_16/21` | … | … | up to ≈8 EiB | top-of-space reserved fill |

The highest end across all 126 roots is `0x40000000000000` = **2⁵⁴**, so the
*populated* top map spans bits [54:0]; the per-die local sub-band is bits [46:0]
(see §3 and [addr-decode.md](addr-decode.md)).

> **CORRECTION (vs an earlier reading of TPB).** `0x2800000000` is **not** the
> `TPB_0` container base. The authoritative `TPB_0` top-level window is
> `base 0x2000000000, size 0x804000000` (L866). `0x2800000000` is the *child*
> `TPB_0_TPB_RESERVED_SBUF` (`base 0x000002800000000, size 0x2000000`) at L873,
> sitting inside `TPB_0`. Use `0x2000000000` as the `TPB_0` base.
> **HIGH/OBSERVED** (grepped unique).

> **GOTCHA — "sprot / D2D / SDMA / INTC" are families, not top-level roots.**
> The task brief lists these as windows the master map "locates," and it does —
> but only `INTC_n` (and its PEB mirror) is an actual top-level root. The other
> three are deep sub-trees, reachable only through `APB_SE_n` / `APB_IO_n`:
> - **SDMA** → `APB_SE_n_SDMA_m_...` (first leaf `APB_SE_0_SDMA_0_UDMA_M2S` at
>   `0x1002000000`, `csrs/sdma/udma_m2s.json`); 10,508 entries contain `SDMA`.
> - **sprot** → `APB_SE_n_USER_FIS_SDMA_k_FIS_j_SPROT_...` (first
>   `APB_SE_0_USER_FIS_SDMA_0_FIS_0_SPROT` at `0x100C005000`); 5,304 entries.
> - **D2D** (die-to-die) → `APB_IO_n_USER_FIS_IO_D2D_...` (first
>   `APB_IO_0_USER_FIS_IO_D2D` at `0x800D960000`, size `0x200000`); 7,158 entries.
>
> If you are looking for a single absolute base for "the sprot block," there
> isn't one — sprot is replicated per FIS instance. The master map's job is to
> give you the *enclosing* `APB_SE_n` / `APB_IO_n` base; you walk in from there.
> **HIGH/OBSERVED.**

---

## 3. SoC address-space structure (the die bit explains the layout)

The 58-bit decode field from `cayman_addr_decode.h` (full bitfield treatment on
[addr-decode.md](addr-decode.md)):

| bits | field | mask | meaning |
| ---- | ----- | ---- | ------- |
| `[46:0]` | LOCAL | `0x7FFFFFFFFFFF` | 47-bit local address (128 TiB span) |
| `[47]` | DIE | `0x1` | die select (0 = die0, 1 = die1) |
| `[53:48]` | CAYMAN_ID | `0x3F` | 6-bit multi-chip / chip-to-chip id |
| `[54]` | CAYMAN_ID_VALID | `0x1` | id field valid |
| `[55]` | RESERVED | `0x1` | reserved |
| `[56]` | PCIE_ATTR_RELAXED_ORDERING | `0x1` | PCIe relaxed-ordering attribute |
| `[57]` | OK_TO_FAIL | `0x1` | access may fail silently |

This decode *explains* the table in §2. The die bit (`0x800000000000`) generates
the entire die-1 copy; the `CAYMAN_ID` field [53:48] is the band the multi-chip
PCIE_A0 (`0x400000000000`) / PCIE_B0 / PCIE_M0 apertures live in. The
`OK_TO_FAIL` and `RELAXED_ORDERING` bits [57:56] are address *attributes*, not
locations — they ride above any concrete window.

> **CORRECTION / NOTE on "58-bit space."** The decode *struct* is 58 bits wide
> (`[57:0]`), but the *populated* address map tops out at 2⁵⁴
> (`0x40000000000000`). Calling the SoC space "58-bit" describes the decoder
> field width; the reachable map is bits [54:0] with [57:55] as attribute/valid
> flags rather than address bits. State it as "58-bit decode field, 2⁵⁴-spanning
> populated map" to avoid the implication of a 2⁵⁸ flat range. **HIGH/OBSERVED.**

---

## 4. Hierarchical RTL source view → flat map

The flat YAML is *generated* from a nested source tree, shipped two ways in the
same directory:

- **`address_map.yaml`** — the un-flattened tree. Each node carries
  `address` (absolute), an optional `child:` array, `name`, `size`, `host-bar`,
  optional `json` (relative path), and `file:`/`line:` pointing back to the
  per-block source YAML it was expanded from (e.g. `apb/sdma/udma_user.yaml`,
  `APB_DMA.yaml`). Array/repeat groups carry `count: N`.
- **`address_map.vh`** — a 191,860-line CSV-in-comment dump of the same tree:
  `//<depth>, <name>, <repeat_count>, <local_offset>, -1, <child_aggregate_size>`.

### Worked example: a `count: 32` repeat group

In `address_map.yaml`, the node

```yaml
name: user_fis_sdma
count: 32
size: 131072            # 0x20000 per instance
file: APB_DMA.yaml
line: 69
```

(matched in `address_map.vh` by `//1, USER_FIS_SDMA, 32, 0, -1, 0`) expands into
32 flat entries `APB_SE_n_USER_FIS_SDMA_0 .. _31`, each stride = its own size
(`0x20000`), names UPPERCASED with the index suffix. The generator walks the
tree depth-first, accumulates the parent `address`, multiplies out every `count`
group, UPPERCASEs-and-`_`-joins the path, and emits one flat line per
leaf-or-container reached.

### Depth, naming, stride/repeat groups

- **Depth** runs 0–6 (`//<depth>` column histogram: depth0 = 174 nodes, depth1 =
  1534, depth2 = 3168, depth3 = 4324, depth4 = 6778, depth5 = 2010, depth6 = 108).
- **Naming**: child names are concatenated to the parent path with `_`; array
  instances append `_<index>`. The flat `name` is the full root-to-node path.
- **Stride/repeat** observed strides (re-derived from the flat map):
  `TOP_SP` ×10/die, stride `0x40000000`, size `0x400000`;
  `TPB` ×4/die (TPB_0..3 then 4..7), stride `0x1000000000`;
  `USER_FIS_SDMA` ×32, stride `0x20000`; `TRIG` ×2, stride `0x1000`.

This `count`-expansion is exactly why the flat file has 34,858 lines from a tree
of far fewer *distinct* source nodes — the arrays multiply out. See
[unified-soc-memory-map.md](unified-soc-memory-map.md) for the consolidated
multi-view picture.

---

## 5. Node → CSR-schema xref

19,012 of the 34,858 flat nodes (15,846 do not) bind to one of **76 distinct**
CSR register-file schemas via the optional `json:` field. The sibling
`address_map_json_xref.yaml` is the independent binding index: 10 banner lines +
19,012 mappings of the form

```
<lowercased_flat_name>: csrs/<subsystem>/<unit>.json
```

### Mechanism

1. Take a flat node with a `json:` field — e.g.
   `TPB_0_ACT_LOCAL_REG` (`base 0x2802460000, size 0x10000,
   json: csrs/tpb/tpb_xt_local_reg.json`).
2. Lowercase the name → `tpb_0_act_local_reg`.
3. Look it up in `address_map_json_xref.yaml` →
   `csrs/tpb/tpb_xt_local_reg.json`.
4. The two agree. **Verified for all 19,012 bindings: 0 mismatches**, and
   `len(xref) == len(flat-with-json) == 19,012` exactly. The xref is thus a
   redundant, lowercase-keyed cross-check of the flat `json:` column.

Each `csrs/<sub>/<unit>.json` is a full register-file definition: top object
`RegFile` with `UnitName`, `DataWidth`, `AddrWidth`, `SizeInBytes`,
`InterfaceType`, `RegfileFlavor`, `Memories`, `RegistersBundleArrays`,
`Includes`, … See [block-schema-xref.md](block-schema-xref.md) for the schema
walk.

### Binding count by subsystem (re-tallied from the flat map; sum = 19,012)

| subsystem | bindings | subsystem | bindings | subsystem | bindings |
| --------- | -------- | --------- | -------- | --------- | -------- |
| sprot | 4696 | sdma | 1780 | pcie | 1280 |
| erg | 2700 | ela500 | 1318 | fis | 1046 |
| intc | 1932 | notific | 1294 | urb | 400 |
| d2d | 1872 | tpb | 228 | hbm | 204 |
| xtensa_q7 | 80 | xtensa_nx | 60 | top_sp | 40 |
| gpio | 16 | apbblk | 12 | pll | 10 |
| dfx | 8 | misc | 6 | ring | 6 |
| rdm | 4 | sfabric | 4 | i2c | 4 |
| spis | 4 | pvt | 4 | iofabric | 2 |
| pmdtu | 2 | | | | |

The Q7 GPSIMD cluster surfaces here as `xtensa_q7` (80 bindings,
`csrs/xtensa_q7/xtensa_q7.json`); the management LX core is `xtensa_nx` (60,
`csrs/xtensa_nx/xtensa_nx.json`).

---

## 6. Coverage tally & tiling check

- **Total nodes:** 34,858 (re-counted: `rg -c '^- { name:'` = `wc -l` = 34,858,
  100 % well-formed, 0 stray lines). **HIGH/OBSERVED.**
- **With `json:`** 19,012; **without** 15,846; sum = 34,858. **HIGH/OBSERVED.**
- **Distinct schemas:** 76, all present on disk. **HIGH/OBSERVED.**
- **Top-level partition roots:** 126 (22 of them `RESERVED_n`). **HIGH/OBSERVED.**

**Tiling.** Walking the 126 roots in base order: **0 overlaps**. There are 55
*gaps* between named windows — these are the intentional holes between
fixed-stride control windows (e.g. the 4 MiB `TOP_SP_n` on a `0x40000000`
stride leaves a `0x3FC00000` hole after each), and between bands they are filled
by the `RESERVED_n` roots (`RESERVED_0` = `0x1240000000` size `0xDC0000000`
fills the PREPROC→TPB gap, etc.). So the map does **not** densely tile inside the
control bands — it is a sparse, stride-addressed map with explicit RESERVED fill
between major bands. **Flagging the gaps as by-design**, not as missing data.

> **QUIRK.** Do not assume `addr ∈ [base, base+size)` for *some* root implies a
> live target. The 55 inter-window gaps and the `RESERVED_n` fills are real
> address space that decodes to nothing live; an access there is governed by the
> `OK_TO_FAIL` attribute bit, not by a backing window. **HIGH/INFERRED.**

---

## 7. Address → region resolution (C pseudocode)

Build a sorted base table once, binary-search at lookup. Real node names from
the flat map:

```c
/* One row per top-level partition root from address_map_flat.yaml.
 * Sorted ascending by base. Sizes are byte counts. */
typedef struct { uint64_t base, size; const char *name; const char *json; } region_t;

static const region_t soc_regions[] = {
    { 0x000000000000000ull, 0x1000000000ull, "HBM_0",      0 /* pure DRAM */ },
    { 0x000001000000000ull, 0x00C800000ull,  "APB_SE_0",   0 /* container */ },
    { 0x000001200000000ull, 0x040000000ull,  "PREPROC_0",  0 },
    { 0x000001240000000ull, 0xDC0000000ull,  "RESERVED_0", 0 },
    { 0x000002000000000ull, 0x804000000ull,  "TPB_0",      0 },
    /* ... TPB_1..3, HBM_1, APB_SE_1, PREPROC_1, APB_IO_0,            */
    /*     TOP_SP_0..9, INTC_0, RDM_0, EVT_ACCEL_*_0, PCIE_A0_0 ...   */
    /*     then the bit-47 die-1 copies (HBM_2/3, TPB_4..7, ...),     */
    /*     then PCIE_B0_*, PEB_*, PCIE_M0_* far apertures.            */
    /* 126 rows total. */
};
enum { N_REGIONS = sizeof(soc_regions)/sizeof(soc_regions[0]) };

/* Resolve an absolute 58-bit SoC address to its enclosing top-level region.
 * Returns 0 if addr lands in an unmapped inter-window gap. O(log N). */
static const region_t *soc_resolve(uint64_t addr)
{
    /* Strip the non-address attribute bits [57:55] before lookup; keep
     * DIE[47] and CAYMAN_ID[53:48] — they ARE part of the absolute base. */
    addr &= ~((0x7ull) << 55);                  /* clear OK_TO_FAIL/RO/RESERVED */

    size_t lo = 0, hi = N_REGIONS;              /* upper-bound binary search   */
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;        /* overflow-safe midpoint      */
        if (soc_regions[mid].base <= addr) lo = mid + 1;
        else                               hi = mid;
    }
    if (lo == 0) return 0;                       /* below the first base         */
    const region_t *r = &soc_regions[lo - 1];    /* greatest base <= addr        */
    if (addr - r->base < r->size) return r;      /* inside the window            */
    return 0;                                     /* fell into an inter-window gap */
}

/* Within a hit region, the LOCAL offset for the per-subtree decoders is: */
static inline uint64_t soc_local_offset(const region_t *r, uint64_t addr)
{ return (addr & ~((0x7ull) << 55)) - r->base; }
```

The `addr - base < size` form (rather than `base + size`) avoids overflow at the
top apertures (`PCIE_B0_*` sizes are 2⁵⁰; `RESERVED_16` is near 2⁵²). The
attribute-bit mask matches `cayman_addr_decode.h` exactly: bits [57:55] are
cleared, bits [54:0] (incl. DIE and CAYMAN_ID) are preserved because they
distinguish the die-1 copy and the multi-chip apertures.

---

## 8. Cross-check vs the pickled DB — divergence flagged

The shipped `al_address_map_db.pkl` lives under the **maverick** (NC-v5) header
tree:

```
extracted/.../arch-headers/maverick/ext/al_address_map_db.pkl
```

It loads as a Python `list` of 323,198 dicts (fields `name`, `short_name`,
`size`, `offset`, `parent_names`, `base`, `type`, `count`, …). Loading it and
walking its roots (`parent_names == ['ADDRESS_MAP']`) gives **5** top-level
nodes on a 2⁶⁰ stride:

```
ADDRESS_MAP_USER_INT     base 0x0000000000000000  size 0x1000000000000000
ADDRESS_MAP_USER_PCIEA   base 0x1000000000000000  size 0x1000000000000000
ADDRESS_MAP_SECURE_INT   base 0x2000000000000000  size 0x1000000000000000
ADDRESS_MAP_SECURE_PCIEM base 0x3000000000000000  size 0x1000000000000000
ADDRESS_MAP_USER_POD     base 0x4000000000000000  size 0x4000000000000000
```

with HBM at `size 0x2000000000` (128 GiB) and TPB at `size 0x4000000000`
(256 GiB) under a `USER_INT_SENG_n_*` (Sub-ENGine) hierarchy.

> **CORRECTION / GOTCHA.** This pickle is **NOT** the Cayman map and **cannot**
> validate the Cayman flat YAML. It is a *different SoC generation* (MAVERICK /
> NC-v5): different root names, a USER/SECURE × INT/PCIE/POD top split, 2⁶⁰
> strides, 128 GiB HBM, 256 GiB TPB, and a SENG sub-engine layer absent from
> Cayman. Treat any MAVERICK-interior base/size as **INFERRED for v5 only** —
> Cayman (NC-v3) numbers come exclusively from `address_map_flat.yaml`. The pkl
> is useful as a *forward-looking* cross-walk, not as a Cayman cross-check. See
> [pkl-db.md](pkl-db.md) for the v5 DB walk. **HIGH/OBSERVED.**

---

## Cross-links

- [addr-decode.md](addr-decode.md) — the 58-bit bitfield decoder (DIE / CAYMAN_ID / attrs).
- [tpb-pool.md](tpb-pool.md) — the `TPB_n` subtree (STATE_BUF / PSUM_BUF / Q7 POOL cluster).
- [preproc-cc.md](preproc-cc.md) — the `PREPROC_n` (preprocessing / collective) subtree.
- [block-schema-xref.md](block-schema-xref.md) — the 76 CSR `RegFile` schemas.
- [pkl-db.md](pkl-db.md) — the MAVERICK/NC-v5 pickled DB walk (forward cross-walk).
- [unified-soc-memory-map.md](unified-soc-memory-map.md) — consolidated multi-view map.
