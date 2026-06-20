# CSR — PE Array Sequencer (host-visible)

> **Scope.** This page documents `tpb_arr_seq_top_host_visible`, the APB-mapped
> host face of the TPB **PE (Processing Element) systolic-matmul array
> sequencer** on Cayman (NC-v3). It is the top-level *ARRay SEQuencer*
> (`arr_seq`) that drives the 128×128 PE array: it accepts the matmul micro-op
> stream (LdWeight / Matmul / PeRegwrite), feeds the array through an input FIFO
> (`ififo`), and exposes per-tile instruction telemetry plus a thin config and
> debug surface to the host.
>
> All offsets, names, bit positions, access types, and reset values on this page
> are byte-exact from the shipped Cayman register-description schema
> `csrs/tpb/tpb_arr_seq_top_host_visible.json` (`AddrWidth 12 → 4 KiB`,
> `SizeInBytes 0x1000`, `InterfaceType APB`, `RegfileFlavor POSEDGE`,
> `DataWidth 32`). Confidence is tagged **HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED**.
>
> **Per-gen applicability.** Byte-grounded for **Cayman / NC-v3** from this
> schema. The same `tpb_arr_seq_top_host_visible.json` ships under `sunda`
> (NC-v2), `mariana`/`mariana_plus`, and `maverick` arch-header trees; the
> 128×128 geometry and the three-bank perf model are stable across that family.
> **v5 / NC-v5 (Maverick-successor): INFERRED** — the host-visible control
> surface (config + telemetry + debug, *no* writable dimension/PSUM/precision
> register) is expected to carry forward unchanged, but no v5-specific schema was
> consulted; treat any v5 statement as INFERRED.

Related pages: [PE matmul firmware](../../firmware/kernels/pe-matmul.md) (the
micro-op decode and the `arr_seq` handshake) · [Cayman PE engine
images](../../images/cayman-pe.md) · [SIMD/MAC datapath](../../uarch/simd-datapath.md)
· [TPB CSR overview](./tpb.md).

---

## 1. Regfile metadata (HIGH · OBSERVED)

Top-level `RegFile` object, scalar fields verbatim from the schema:

| Field | Value | Note |
|---|---|---|
| `UnitName` | `tpb_arr_seq_top_host_visible` | the host-visible (APB-mapped) face |
| `Type` | `REGFILE` | |
| `RegfileFlavor` | `POSEDGE` | positive-edge-clocked register file |
| `InterfaceType` | `APB` | host access is APB, 32-bit |
| `AddrWidth` | `12` | 4 KiB address window |
| `DataWidth` | `32` | all registers are 32-bit |
| `SizeInBytes` | `0x1000` | 4096 B window |
| `Description` / `HalName` / `HalFilenameUnitName` | *(empty)* | blank in the JSON |
| `Includes` / `Parameters` / `Memories` | `[]` (all empty) | no sub-includes, no RAMs |

The block is built from **7 `RegistersBundleArrays`**. A "bundle" is a named,
optionally array-replicated group of registers at a base offset; `ArraySize`
replicates the bundle's register set `N` times at `BundleSizeInBytes` stride.

---

## 2. Register-count verification (HIGH · OBSERVED) — **CONFIRMED 305**

Re-derived from scratch by enumerating
`[.RegFile.RegistersBundleArrays[].Registers[]]`:

| idx | Bundle | base | `BundleSizeInBytes` | `ArraySize` | unique regs | fields | expanded |
|---|---|---|---|---|---|---|---|
| 0 | `arr_seq_cfg` | `0x000` | `0x40` | 1 | 6 | 9 | 6 |
| 1 | `arr_seq_ififo_perf_weight_load` | `0x100` | `0x200` | 1 | 98 | 98 | 98 |
| 2 | `arr_seq_ififo_perf_matmul` | `0x300` | `0x200` | 1 | 98 | 98 | 98 |
| 3 | `arr_seq_ififo_perf_pe_regwrite` | `0x500` | `0x200` | 1 | 98 | 98 | 98 |
| 4 | `arr_seq_queue_debug` | `0x700` | `0x0C` | 16 | 3 | 3 | 48 |
| 5 | `arr_seq_sbuf_rd_req_debug` | `0x800` | `0x04` | 9 | 1 | 1 | 9 |
| 6 | `arr_seq_ififo_debug` | `0x840` | `0x04` | 1 | 1 | 1 | 1 |
| | **SUM** | | | | **305** | **308** | **358** |

- **Unique register definitions = 305** (6+98+98+98+3+1+1). Matches the task's
  "~305" **exactly** — no CORRECTION needed on the headline count.
- **BitFields = 308** (each register here has one field except the three
  `arr_seq_cfg` multi-field registers).
- **Expanded (ArraySize applied) = 358**, occupying `0x000..0x843`; all 358
  absolute offsets are unique (no aliasing). Upper ~46% of the 4 KiB window
  (`0x844..0xFFF`) is unused/reserved.

**Access / reset census (whole file, HIGH · OBSERVED):**

| Census | Result |
|---|---|
| Register `AccessType` | **299 RO**, 6 RW |
| BitField `AccessType` | 299 RO, **9 RW** (the 6 RW regs carry 9 RW fields) |
| `SpecialAccess` | 307 `None`, **1 `PulseOnW`** |
| Field `ResetValue` | 306 × `0x0`, **2 × `0xffffffff`** |

The block is **96.4 % telemetry** (294/305 perf counters), ~2 % config
(6/305, all in bundle 0), ~3 % debug (5/305 unique). There is exactly one
write-pulse field (`perf_cntr_cfg.cntr_rst`) and no other side-effecting CSR.

> **Reset-integrity note (HIGH · OBSERVED).** Grepping every `ResetValue` for
> `b1` / `dead` / `bad` placeholder patterns returns nothing. The two
> `0xffffffff` resets are the deliberate "default-to-one" metal-ECO spares
> (`spare_register2/3`); all other resets are genuine `0x0`. No placeholder-reset
> artifact in this tpb-family block.

---

## 3. Bundle 0 — `arr_seq_cfg`: the entire host-writable control surface (HIGH · OBSERVED)

Base `0x000`, `BundleSizeInBytes 0x40`, `ArraySize 1`, 6 registers, 9 fields.
**All 6 registers are RW — this is the complete host-writable control of the
block.** Bytes `0x018..0x03F` inside the 0x40 bundle hold no registers
(reserved/padding).

| abs | Register | Field | `Position` | acc | reset | special | meaning |
|---|---|---|---|---|---|---|---|
| `0x000` | `queue_cfg` | `fifo_size_sel` | `[0]` | RW | `0x0` | None | FIFO-full policy for the queue instances. `0` = assert full only when the FIFO fills completely; `1` = assert full after a **single** entry is used (serialized / single-entry mode). |
| `0x000` | `queue_cfg` | `bypass_peregwrite_instr` | `[4]` | RW | `0x0` | None | If set, `PeRegwrite` instructions perform **no SBUF memory fetch** (register-immediate path; skip the state-buffer read). |
| `0x000` | `queue_cfg` | `disable_dependency_check` | `[8]` | RW | `0x0` | None | If set, the queue instances become **single-threaded** and inter-instruction dependency checking is disabled. |
| `0x004` | `perf_cntr_cfg` | `cntr_en` | `[0]` | RW | `0x0` | None | Master enable for **all** counters in the `arr_seq_ififo_perf` section (bundles 1/2/3). |
| `0x004` | `perf_cntr_cfg` | `cntr_rst` | `[4]` | RW | `0x0` | **PulseOnW** | Write-pulse: resets all perf counters in `arr_seq_ififo_perf`. The block's **only** `PulseOnW` field. |
| `0x008` | `spare_register0` | `spare` | `[31:0]` | RW | `0x0` | None | Metal-ECO spare; defaults 0. |
| `0x00C` | `spare_register1` | `spare` | `[31:0]` | RW | `0x0` | None | Metal-ECO spare; defaults 0. |
| `0x010` | `spare_register2` | `spare` | `[31:0]` | RW | `0xffffffff` | None | Metal-ECO spare; defaults all-ones. |
| `0x014` | `spare_register3` | `spare` | `[31:0]` | RW | `0xffffffff` | None | Metal-ECO spare; defaults all-ones. |

These three `queue_cfg` bits are the **only** behavioral control the host has
over how the sequencer issues work; everything else (what to compute, with which
weights, in which tile shape) arrives **in the instruction stream**, not in a
CSR (see [§6](#6-arming-the-pe-array--the-host-sequence-med--inferred)).

> **Micro-op binding (HIGH for names · MED for the exact op set).** The three
> instruction classes that program this sequencer — **LdWeight** (weight load),
> **Matmul**, and **PeRegwrite** — appear by name as the three perf-counter banks
> (§4) and in `queue_cfg.bypass_peregwrite_instr`'s description. This is the same
> micro-op set decoded by the [PE matmul firmware](../../firmware/kernels/pe-matmul.md);
> `bypass_peregwrite_instr` directly ties the host-visible `PeRegwrite` class to
> its SBUF-fetch behavior.

---

## 4. Bundles 1/2/3 — `arr_seq_ififo_perf_{weight_load,matmul,pe_regwrite}`: per-tile instruction telemetry (HIGH · OBSERVED)

The three perf bundles are **structurally byte-identical**: same 98 register
names, same offsets, same field layout. A `diff` of each bank's `(offset, name)`
list (with the bank-implied prefix stripped) against `weight_load` is empty
(0 differing lines for both `matmul` and `pe_regwrite`). One bank counts
**LdWeight** ops, one **Matmul** ops, one **PeRegwrite** ops — mirroring the
firmware micro-op set. The `.json.mako` generator confirms
`types = ['weight_load','matmul','pe_regwrite']`.

Each bank = **98 registers = 49 distinct counters × 2 halves**:

- `<tile>_instr_cnt_lsb` → field `cntr_lsb` `[31:0]`, RO, reset `0x0` — low 32 bits.
- `<tile>_instr_cnt_msb` → field `cntr_msb` **`[15:0]`**, RO, reset `0x0` — high 16 bits.

> **CORRECTION vs SX-CSR-05 §4 (HIGH · OBSERVED).** The backing report calls
> these "64-bit free-running counters" (`cntr_msb [31:0]`). The Cayman schema is
> unambiguous: **every one of the 147 MSB fields is `cntr_msb [15:0]`** (census:
> 147 × `[15:0]` MSB, 147 × `[31:0]` LSB across the three banks). Each counter is
> therefore **48-bit** (32 LSB + 16 MSB), not 64-bit. Reconstruct a counter as
> `((u64)(msb & 0xFFFF) << 32) | lsb`. The 64-bit claim is wrong for this schema.

### 4.1 Why 49 counters — the array geometry (HIGH for 128×128 + 9 shapes · OBSERVED)

The 49 counters are **one per tile**, where a tile is a sub-partition of the
**128×128** PE array. The sequencer can partition the array into 9 tile shapes
(rows ∈ {128,64,32} × cols ∈ {128,64,32}). Tile count per shape =
`(128/R)·(128/C)`:

```
128x128 -> 1     64x128 -> 2     32x128 -> 4
128x64  -> 2     64x64  -> 4     32x64  -> 8
128x32  -> 4     64x32  -> 8     32x32  -> 16
            SUM = 1+2+4 + 2+4+8 + 4+8+16 = 49
```

49 counters × 2 halves = 98 registers/bank; 98 × 3 banks = **294 of 305 unique
registers**. The 128×128 array geometry and the 9 legal tile shapes are exposed
**only descriptively** through these counter names — there is **no writable
dimension or partition-select register** in this host-visible window. The actual
partition the array runs is selected by the Matmul/LdWeight micro-op, not a CSR.

Tile **index** numbering (re-derived, MED · INFERRED): indices come from a 4×4
grid of 32×32 quadrants, linearized row-major (index = `quadrant_row·4 +
quadrant_col`). A tile of shape R×C occupies `(R/32)×(C/32)` quadrants and is
named by its top-left quadrant's linear index. Verified index sets:
`128x128:{0}`, `128x64:{0,2}`, `128x32:{0,1,2,3}`, `64x128:{0,8}`,
`64x64:{0,2,8,10}`, `64x32:{0,1,2,3,8,9,10,11}`, `32x128:{0,4,8,12}`,
`32x64:{0,2,4,6,8,10,12,14}`, `32x32:{0..15}`.

### 4.2 Full 49-counter offset map (HIGH · OBSERVED)

Relative offsets within each bank; the `_lsb` register sits at the listed
offset, `_msb` at `+0x4`. Bank bases: `weight_load 0x100`, `matmul 0x300`,
`pe_regwrite 0x500` → absolute `_lsb` = `base + rel`.

| # | rel `_lsb` | shape | tile | abs `_lsb` (wl / mm / pe) |
|---|---|---|---|---|
| 1 | `0x000` | 128×128 | 0 | `0x100` / `0x300` / `0x500` |
| 2 | `0x010` | 128×64 | 0 | `0x110` / `0x310` / `0x510` |
| 3 | `0x018` | 128×64 | 2 | `0x118` / `0x318` / `0x518` |
| 4 | `0x028` | 128×32 | 0 | `0x128` / `0x328` / `0x528` |
| 5 | `0x030` | 128×32 | 1 | `0x130` / `0x330` / `0x530` |
| 6 | `0x038` | 128×32 | 2 | `0x138` / `0x338` / `0x538` |
| 7 | `0x040` | 128×32 | 3 | `0x140` / `0x340` / `0x540` |
| 8 | `0x050` | 64×128 | 0 | `0x150` / `0x350` / `0x550` |
| 9 | `0x058` | 64×128 | 8 | `0x158` / `0x358` / `0x558` |
| 10 | `0x068` | 64×64 | 0 | `0x168` / `0x368` / `0x568` |
| 11 | `0x070` | 64×64 | 2 | `0x170` / `0x370` / `0x570` |
| 12 | `0x078` | 64×64 | 8 | `0x178` / `0x378` / `0x578` |
| 13 | `0x080` | 64×64 | 10 | `0x180` / `0x380` / `0x580` |
| 14 | `0x090` | 64×32 | 0 | `0x190` / `0x390` / `0x590` |
| 15 | `0x098` | 64×32 | 1 | `0x198` / `0x398` / `0x598` |
| 16 | `0x0a0` | 64×32 | 2 | `0x1a0` / `0x3a0` / `0x5a0` |
| 17 | `0x0a8` | 64×32 | 3 | `0x1a8` / `0x3a8` / `0x5a8` |
| 18 | `0x0b0` | 64×32 | 8 | `0x1b0` / `0x3b0` / `0x5b0` |
| 19 | `0x0b8` | 64×32 | 9 | `0x1b8` / `0x3b8` / `0x5b8` |
| 20 | `0x0c0` | 64×32 | 10 | `0x1c0` / `0x3c0` / `0x5c0` |
| 21 | `0x0c8` | 64×32 | 11 | `0x1c8` / `0x3c8` / `0x5c8` |
| 22 | `0x0d8` | 32×128 | 0 | `0x1d8` / `0x3d8` / `0x5d8` |
| 23 | `0x0e0` | 32×128 | 4 | `0x1e0` / `0x3e0` / `0x5e0` |
| 24 | `0x0e8` | 32×128 | 8 | `0x1e8` / `0x3e8` / `0x5e8` |
| 25 | `0x0f0` | 32×128 | 12 | `0x1f0` / `0x3f0` / `0x5f0` |
| 26 | `0x100` | 32×64 | 0 | `0x200` / `0x400` / `0x600` |
| 27 | `0x108` | 32×64 | 2 | `0x208` / `0x408` / `0x608` |
| 28 | `0x110` | 32×64 | 4 | `0x210` / `0x410` / `0x610` |
| 29 | `0x118` | 32×64 | 6 | `0x218` / `0x418` / `0x618` |
| 30 | `0x120` | 32×64 | 8 | `0x220` / `0x420` / `0x620` |
| 31 | `0x128` | 32×64 | 10 | `0x228` / `0x428` / `0x628` |
| 32 | `0x130` | 32×64 | 12 | `0x230` / `0x430` / `0x630` |
| 33 | `0x138` | 32×64 | 14 | `0x238` / `0x438` / `0x638` |
| 34 | `0x148` | 32×32 | 0 | `0x248` / `0x448` / `0x648` |
| 35 | `0x150` | 32×32 | 1 | `0x250` / `0x450` / `0x650` |
| 36 | `0x158` | 32×32 | 2 | `0x258` / `0x458` / `0x658` |
| 37 | `0x160` | 32×32 | 3 | `0x260` / `0x460` / `0x660` |
| 38 | `0x168` | 32×32 | 4 | `0x268` / `0x468` / `0x668` |
| 39 | `0x170` | 32×32 | 5 | `0x270` / `0x470` / `0x670` |
| 40 | `0x178` | 32×32 | 6 | `0x278` / `0x478` / `0x678` |
| 41 | `0x180` | 32×32 | 7 | `0x280` / `0x480` / `0x680` |
| 42 | `0x188` | 32×32 | 8 | `0x288` / `0x488` / `0x688` |
| 43 | `0x190` | 32×32 | 9 | `0x290` / `0x490` / `0x690` |
| 44 | `0x198` | 32×32 | 10 | `0x298` / `0x498` / `0x698` |
| 45 | `0x1a0` | 32×32 | 11 | `0x2a0` / `0x4a0` / `0x6a0` |
| 46 | `0x1a8` | 32×32 | 12 | `0x2a8` / `0x4a8` / `0x6a8` |
| 47 | `0x1b0` | 32×32 | 13 | `0x2b0` / `0x4b0` / `0x6b0` |
| 48 | `0x1b8` | 32×32 | 14 | `0x2b8` / `0x4b8` / `0x6b8` |
| 49 | `0x1c0` | 32×32 | 15 | `0x2c0` / `0x4c0` / `0x6c0` |

Gaps between shape-groups (`0x008..0x00F`, `0x048..0x04F`, `0x060..0x067`,
`0x088..0x08F`, `0x0d0..0x0d7`, `0x140..0x147`, `0x1c8..0x1FF`) are alignment
padding so each group starts on an 8-byte boundary; the bank rounds up to
`0x200`.

> **Doc anomaly A1 (HIGH · OBSERVED).** Three counters' *field descriptions*
> carry the wrong tile index (generator-text bug; the register NAME and OFFSET
> are correct): `tile_128x32_tile2_instr_cnt_{lsb,msb}` describe themselves as
> "tile 3"; `tile_64x32_tile10_instr_cnt_msb` describes "tile 11" while its
> `_lsb` correctly says "tile 10". The same generator drives all three banks, so
> up to 9 strings are affected. **Trust the register name and offset, not the
> description text.**
>
> **Doc anomaly A2 (HIGH · OBSERVED).** The distinguishing instruction class
> (weight_load vs matmul vs pe_regwrite) lives **only in the bundle name** — the
> field descriptions are identical across banks ("…tile config … instruction
> count LSB/MSB"). A consumer must use the bundle name, not the field text, to
> know which op a counter counts. (Per-bank register descriptions are empty;
> the prose lives on the bitfield.)

---

## 5. Bundles 4/5/6 — debug-vector taps (HIGH · OBSERVED)

All RO, reset `0x0`, single `debug_vector_N` field `[31:0]` each — raw RTL probe
taps ("brings out internal signals"); there are **no decoded status bits**
(no `busy`/`idle`/`error` named field anywhere in this window).

| Bundle | base | `ArraySize` | stride | per-element fields | element bases |
|---|---|---|---|---|---|
| `arr_seq_queue_debug` | `0x700` | 16 | `0x0C` | `debug_vector_{0,1,2}` @ `+0x0/+0x4/+0x8` (96 debug bits/queue) | `0x700,0x70c,0x718,0x724,0x730,0x73c,0x748,0x754,0x760,0x76c,0x778,0x784,0x790,0x79c,0x7a8,0x7b4` |
| `arr_seq_sbuf_rd_req_debug` | `0x800` | 9 | `0x04` | `debug_vector_0` `[31:0]` | `0x800,0x804,0x808,0x80c,0x810,0x814,0x818,0x81c,0x820` |
| `arr_seq_ififo_debug` | `0x840` | 1 | `0x04` | `debug_vector_0` `[31:0]` (single IFIFO probe) | `0x840` |

- **16 queue instances** → the sequencer exposes **16 host-visible queue/thread
  slots** (HIGH count · MED that 16 == #threads). With
  `queue_cfg.disable_dependency_check`, those 16 slots collapse to single-threaded
  in-order issue.
- **9 sbuf_rd_req instances** → 9 SBUF read-request debug taps; their exact
  physical width meaning (ports/partitions) is not stated in the JSON (HIGH
  count · LOW on the "= 9 ports" meaning).

---

## 6. Arming the PE array — the host sequence (MED · INFERRED)

The critical reimplementation insight from the schema: **this host-visible block
has no "go" / "start" CSR.** The only side-effecting bit is the counter-reset
pulse. The array is armed by *configuration* + *streaming micro-ops*, not by a
trigger register:

1. **Configure issue policy** (one-time, bundle 0): set `queue_cfg` for the
   desired mode — FIFO-full policy (`fifo_size_sel`), PeRegwrite SBUF-fetch
   bypass (`bypass_peregwrite_instr`), and single-thread vs dependency-checked
   issue (`disable_dependency_check`).
2. **Enable telemetry** (optional): pulse `perf_cntr_cfg.cntr_rst`, then set
   `perf_cntr_cfg.cntr_en = 1` to start the per-tile counters.
3. **Stream the matmul program** into the sequencer's IFIFO as
   **LdWeight → Matmul → (PeRegwrite)** micro-ops. The tile shape, weight-load
   target column / last-active-column, accumulation/`done` semantics, and the
   data type are carried by the *instruction descriptors* (decoded by the
   [PE matmul firmware](../../firmware/kernels/pe-matmul.md)), **not** by these
   CSRs.
4. **Observe** via the perf counters (per-tile op counts) and the RO
   `debug_vector` taps; there is no decoded completion register in this window —
   completion/`done` is signaled on the response bus (cluster sibling, §7).

```c
/* Cayman PE array-sequencer host arm-sequence (host-visible window only).
 * Base = APB base of tpb_arr_seq_top_host_visible (4 KiB). 32-bit accesses.
 * NOTE: no CSR launches the matmul -- work enters via the micro-op IFIFO. */

#define ARRSEQ_QUEUE_CFG      0x000u   /* RW */
#define ARRSEQ_PERF_CNTR_CFG  0x004u   /* RW; cntr_rst is PulseOnW            */

/* queue_cfg bit positions (from schema Position fields) */
#define QCFG_FIFO_SIZE_SEL    (1u << 0)   /* 1 = single-entry/serialized FIFO  */
#define QCFG_BYPASS_PEREGW    (1u << 4)   /* 1 = PeRegwrite skips SBUF fetch    */
#define QCFG_DISABLE_DEPCHK   (1u << 8)   /* 1 = single-threaded, no dep-check  */

/* perf_cntr_cfg bit positions */
#define PCFG_CNTR_EN          (1u << 0)
#define PCFG_CNTR_RST         (1u << 4)   /* write-pulse: clears all perf cntrs */

static inline void wr32(volatile uint32_t *base, uint32_t off, uint32_t v) {
    base[off >> 2] = v;
}

void arrseq_arm(volatile uint32_t *base, bool serialized, bool reg_immediate) {
    /* 1. issue policy: in-order serialized vs dependency-checked 16-way */
    uint32_t qcfg = 0;
    if (serialized)      qcfg |= QCFG_FIFO_SIZE_SEL | QCFG_DISABLE_DEPCHK;
    if (reg_immediate)   qcfg |= QCFG_BYPASS_PEREGW;   /* PeRegwrite: no SBUF rd */
    wr32(base, ARRSEQ_QUEUE_CFG, qcfg);

    /* 2. telemetry: reset (write-pulse) then enable per-tile counters */
    wr32(base, ARRSEQ_PERF_CNTR_CFG, PCFG_CNTR_RST);   /* self-clearing pulse   */
    wr32(base, ARRSEQ_PERF_CNTR_CFG, PCFG_CNTR_EN);

    /* 3. THE MATMUL ITSELF IS NOT A CSR WRITE.
     *    Push LdWeight / Matmul / PeRegwrite micro-ops into the sequencer
     *    IFIFO (see firmware/kernels/pe-matmul.md). Tile shape, weight-load
     *    last-active-column, accumulate/done, and dtype live in the descriptor,
     *    not here. */
}

/* read a 48-bit per-tile instruction counter (msb is only [15:0]) */
uint64_t arrseq_read_tile_counter(volatile uint32_t *base, uint32_t lsb_off) {
    uint32_t lo = base[ lsb_off       >> 2];
    uint32_t hi = base[(lsb_off + 4)  >> 2] & 0xFFFFu;   /* cntr_msb [15:0] */
    return ((uint64_t)hi << 32) | lo;
}
```

---

## 7. Host-visible vs protected vs cluster split (HIGH for membership · MED for rationale)

The `arr_seq` sequencer is split across **three sibling APB regfiles** in
`csrs/tpb/`, each its own 4 KiB window. The split is the security/privilege
boundary: untrusted host software touches the *host-visible* face; the *protected*
face is reachable only at a higher privilege; the *cluster* face holds the
matmul-sequencing knobs that gate the array's response behavior.

| Schema | `UnitName` | bundles | regs | what it holds |
|---|---|---|---|---|
| **host-visible** *(this page)* | `tpb_arr_seq_top_host_visible` | 7 | **305** | issue-policy config (6 RW) + per-tile telemetry (294 RO) + debug taps (5 RO) |
| **protected** | `tpb_arr_seq_top_protected` | 1 | **1** | a single `throttle_cfg` register |
| **cluster** | `tpb_arr_seq_cluster_host_visible` | 3 | **17** | `arr_cluster_cfg` matmul-sequencing bits + array-stagger idle-timers + 36-way queue perf + rd-rsp debug |

> **CORRECTION / refinement vs SX-CSR-05 (HIGH · OBSERVED).** SX-CSR-05 names the
> protected sibling only generically. The Cayman `tpb_arr_seq_top_protected.json`
> is **not** a config-mirror of `arr_seq_cfg` — it is a single register
> `throttle_cfg @ 0x0` with two RW fields: `disable_throttle [8:0]`
> (reset `0x1ff`, "Set to 1 to disable throttle feature; per XBUS") and
> `hw_only_en_transfer_cnt_upd [12:9]` (reset `0x0`, "hw controlled throttle
> feature; per XBUS"). The protected face exists to gate the **XBUS throttle**
> (bandwidth / back-pressure control) — a privilege-sensitive knob that untrusted
> host code must not be able to flip — hence its separation from the host-visible
> window. (`SizeInBytes 0x1000`, `InterfaceType APB`, same 4 KiB layout.)

**The real matmul-sequencing control lives in the cluster sibling, not here.**
`tpb_arr_seq_cluster_host_visible.json → arr_cluster_cfg @ 0x0`:

| Field | `Position` | reset | meaning |
|---|---|---|---|
| `enable_wl_last_active_col` | `[0]` | `0x0` | set the last-active-column for the target column of a **weight-load** instruction |
| `flush_p2f_fifos` | `[4]` | `0x0` | flush all p2f FIFOs in the response-bus block before the next read response |
| `matmul_done_last` | `[8]` | `0x0` | generate Matmul **done** on the *last* SBUF response (default: on the *first* SBUF read response) |
| `en_inter_instr_dly` | `[12]` | `0x0` | enable inter-instruction response delay at the bottom of the response pipe |
| `inter_instr_dly_cnt` | `[21:16]` | `0x8` | delay count (valid when `en_inter_instr_dly=1`) |
| `p2fifo_af` | `[26:24]` | `0x2` | p2f FIFO almost-full threshold |

The cluster file also carries `array_stagger_rg0..3_ctrl1` (each `rgN_ctrl1`
field `[15:0]` = "max limit for the idle timer count" — the systolic-array
launch-stagger / idle-timer config) plus its own `perf_cntr_cfg` and
spare-register set. So:

- **Weight-load column control** (last-active-column) → **cluster** sibling.
- **Matmul `done`-timing and response-bus flow** (done-on-last, p2f flush,
  inter-instruction delay, almost-full threshold) → **cluster** sibling.
- **Array launch-stagger / idle timers** → **cluster** sibling.
- **Issue policy + per-tile telemetry + debug** → **host-visible** (this file).
- **XBUS throttle** → **protected** sibling.

---

## 8. Negative results — what is NOT in this window (HIGH · OBSERVED)

Verified by exhaustive keyword scan
(`fp32|fp16|bf16|int8|fp8|mxfp|psum|accumul|precision|dtype|quant`) over the
entire JSON — it matches **only** the bundle tokens `matmul`/`weight_load` and
`ififo`:

- **No precision / dtype register.** The matmul data type is set by the
  Matmul/LdWeight micro-op, not by a CSR here. (See
  [SIMD/MAC datapath](../../uarch/simd-datapath.md) for the datapath formats.)
- **No PSUM accumulation register** (no init/accumulate/read/precision). PSUM
  read/init/accumulate lives in a separate state-buffer / PSUM register file;
  even the cluster sibling has no explicit PSUM register. This block only
  **sequences** the array.
- **No writable array-dimension / partition-select register.** The 128×128
  geometry and 9 tile shapes are descriptive (counter names) only.
- **No decoded array-status register** (no `busy`/`idle`/`error`). Status is
  inferred from the RO `debug_vector` taps and the response bus.

This block's host face is, by construction, **config + telemetry + debug only.**

---

## 9. Provenance

Every offset, name, bit position, access type, and reset value above is a
byte-exact reading of `csrs/tpb/tpb_arr_seq_top_host_visible.json` (the shipped
Cayman register-description schema, itself a binary-derived artifact). Counts
(305 / 308 / 358; 49 counters; 9 tile shapes; 16/9 debug instances; the
299 RO / 6 RW, 1 PulseOnW, 306×0 / 2×0xffffffff census) were re-derived from
scratch via `jq` enumeration plus Python/bash arithmetic and cross-checked
against the `.json.mako` generator (`types = ['weight_load','matmul',
'pe_regwrite']`). Protected and cluster sibling data in §7 are byte-exact from
`tpb_arr_seq_top_protected.json` and `tpb_arr_seq_cluster_host_visible.json`,
used only to locate the matmul/throttle control that is absent from the target
file. The **48-bit counter** width and the **`throttle_cfg`** protected-register
contents are CORRECTIONS to SX-CSR-05, grounded directly in the Cayman schema.
