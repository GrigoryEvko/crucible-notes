# CSR — `tpb_xt_local_reg`

> **Scope.** The per-engine `*_LOCAL_REG` control aperture every TPB engine
> points at. This is the register file firmware drives to **release run-stall
> and start/stop the embedded NX (Tensilica) control core and the eight Q7
> sequencer cores**, to set up the **SoC↔Q7 address-translation windows**,
> program the **4D tensor-replace descriptor**, and drive the **HW-decode
> breakpoint/debug** machinery. This page opens the Part-13 CSR sub-lane and
> is the bitfield-level companion to the address-side view in
> [`tpb-pool`](../address/tpb-pool.md).
>
> **Provenance.** Every offset / reset / bit-range below is read directly from
> the shipped register-description schema
> `csrs/tpb/tpb_xt_local_reg.json` (binary-derived CSR JSON, 55 641 bytes,
> Cayman / NC-v3 arch-regs tree). The page default is **[HIGH · OBSERVED]**;
> claims that depart from it carry an explicit
> HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED tag.

---

## 1. Regfile metadata

Read from `.RegFile` of `tpb_xt_local_reg.json`.

| Property | Value | Meaning |
|---|---|---|
| `UnitName` | `tpb_xt_local_reg` | regfile name |
| `Type` | `REGFILE` | a flat register file (no sub-regfiles) |
| `RegfileFlavor` | `POSEDGE` | posedge-clocked flops |
| `InterfaceType` | `APB` | APB-mapped control bus |
| `AddrWidth` | `16` | 16-bit byte address → 64 KiB span |
| `DataWidth` | `32` | every register is 32 bits, word-addressed |
| `SizeInBytes` | `0x10000` | **64 KiB aperture** (matches `LOCAL_REG` slot, see [`tpb-pool`](../address/tpb-pool.md)) |
| `Memories` / `Parameters` / `Includes` | `[]` | all empty |

The 64 KiB `SizeInBytes` is the same `0x10000` that [`tpb-pool`](../address/tpb-pool.md)
reserves for `TPB_0_POOL_LOCAL_REG` at SoC-absolute `0x2803060000` (slot
`+0x3060000` off the `0x2800000000` cluster pseudo-base). For the POOL engine
the q7-bundle doorbell therefore lands at `0x2803060000 + 0x3000 + 0x0 =
0x2803063000`, which is exactly the boot doorbell cited by `#900`.

### Addressing model

The schema nests `RegFile → RegistersBundleArrays[] → Registers[] → BitFields[]`.
A register's `AddressOffset` is **relative to its bundle's** `AddressOffset`;
for an indexed bundle (`ArraySize > 1`) the per-instance base advances by the
bundle's `BundleSizeInBytes` stride:

```
abs_byte_off(reg, i) = bundle.AddressOffset
                     + i * bundle.BundleSizeInBytes      // i in 0 .. ArraySize-1
                     + reg.AddressOffset
```

`BitFields[].Position` is either a single bit `"N"` or an inclusive range
`"hi:lo"`. Reset values live in `BitFields[].ResetValue` (hex string), **not**
in any `Reset` key.

> **GOTCHA (extraction).** The reset key is literally `ResetValue`. A naive
> `.Reset` lookup returns `null` for every field and will make you think the
> whole file is reset-less — it is not. All non-zero resets quoted on this page
> (`0x1`, `0xFF`, `0x1FFF`, `0x10`, `0xb2924`, the CAM byte selectors) come from
> `ResetValue`.

---

## 2. Bundle map

The file declares **7 bundle-arrays, 55 registers, 84 bitfields**. Every
`AccessType` (register and bitfield) is `RW`; every
`SpecialAccess` is `None` — there are **no RO/WO fields** in this block, even
the field literally named `reserved`.

| bundle | base | `ArraySize` | stride (`BundleSizeInBytes`) | regs | fields | array span |
|---|---|---|---|---|---|---|
| `nx` | `0x0000` | 1 | `0x1000` | 8 | 8 | single instance |
| `general` | `0x1000` | 60 | `0x0020` | 1 | 1 | `0x1000`‥`0x1760` |
| `window` | `0x2000` | 40 | `0x001C` | 7 | 12 | `0x2000`‥`0x245C` |
| `q7` | `0x3000` | 1 | `0x1000` | 19 | 19 | single instance |
| `hw_decode` | `0x4000` | 1 | `0x1000` | 14 | 31 | single instance |
| `tensor_replace` | `0x5000` | 32 | `0x0020` | 4 | 8 | `0x5000`‥`0x53FF` |
| `notific` | `0x6000` | 1 | `0x0100` | 2 | 5 | single instance |

Each array span is verified to fit inside its `0x1000`-aligned reserved window
(`window` ends at `0x2000 + 40*0x1C = 0x245C`; `tensor_replace` ends at
`0x5000 + 32*0x20 = 0x5400`).

> **QUIRK.** Bundle strides do not pack tightly to the register footprint.
> `general` reserves `0x20` per instance for a single 4-byte register;
> `window` reserves `0x1C` for 7 registers (a perfect fit); `tensor_replace`
> reserves `0x20` for only `0x10` of registers, leaving `0x10` dead per slot.
> Always compute instance bases from `BundleSizeInBytes`, never from the
> register count.

---

## 3. Bundle `nx` — embedded NX control core (base `0x0000`, 1 instance)

The single Tensilica NX core that runs the engine's control firmware. This is
the boot path: at reset the core is held in run-stall and FW releases it.

| off | register | field | bits | reset | purpose |
|---|---|---|---|---|---|
| `0x0000` | `release_run_stall` | `run_stall` | `[0]` | `0x1` | write **0** → release `RunStallOnReset` to NX. **Reset 1 ⇒ held stalled.** |
| `0x0004` | `start_ctrl` | `ctrl` | `[0]` | `0x0` | 1: `start_addr` valid, exit Halt, begin executing |
| `0x0008` | `run_state` | `state` | `[31:0]` | `0x0` | opaque run-state status word |
| `0x000C` | `dma_rx_base` | `base` | `[31:0]` | `0x0` | DMA RX base address |
| `0x0010` | `dma_tx_base` | `base` | `[31:0]` | `0x0` | DMA TX base address |
| `0x0014` | `instr_halt_ctrl` | `halt_req` | `[0]` | `0x0` | request entry to ISA HALT state |
| `0x0018` | `intr_ctrl` | `en` | `[3:0]` | `0x0` | interrupt enable, 4 sources |
| `0x001C` | `intr_info` | `metadata` | `[31:0]` | `0x0` | interrupt metadata |

---

## 4. Bundle `q7` — eight sequencer cores (base `0x3000`, 1 instance)

The eight Q7 sequencer cores live behind a **single** bundle instance whose
`release_run_stall` carries **one bit per core** (`[7:0]`, reset `0xFF` ⇒ all 8
stalled). This is the CSR-level confirmation of `NUM_POOL_CORES = 8`. The base
`0x3000` is the anchor every sibling page cites.

| off | register | field | bits | reset | purpose |
|---|---|---|---|---|---|
| `0x3000` | `release_run_stall` | `run_stall` | `[7:0]` | `0xFF` | write 0 per-bit → release `RunStallOnReset` to Q7[bit]. **Reset `0xFF` ⇒ all 8 held stalled.** |
| `0x3004` | `start_ctrl` | `ctrl` | `[0]` | `0x0` | 1: `start_addr` valid, exit Halt, start executing |
| `0x3008` | `run_state_0` | `run_state` | `[31:0]` | `0x0` | Q7 #0 run-state |
| `0x300C` | `run_state_1` | `run_state` | `[31:0]` | `0x0` | Q7 #1 run-state |
| `0x3010` | `run_state_2` | `run_state` | `[31:0]` | `0x0` | Q7 #2 run-state |
| `0x3014` | `run_state_3` | `run_state` | `[31:0]` | `0x0` | Q7 #3 run-state |
| `0x3018` | `run_state_4` | `run_state` | `[31:0]` | `0x0` | Q7 #4 run-state |
| `0x301C` | `run_state_5` | `run_state` | `[31:0]` | `0x0` | Q7 #5 run-state |
| `0x3020` | `run_state_6` | `run_state` | `[31:0]` | `0x0` | Q7 #6 run-state |
| `0x3024` | `run_state_7` | `run_state` | `[31:0]` | `0x0` | Q7 #7 run-state |
| `0x3028` | `intr_ctrl` | `en` | `[31:0]` | `0x0` | interrupt enable, **4 bits per Q7** (8 × 4 = 32) |
| `0x302C` | `intr_info_0` | `metadata` | `[31:0]` | `0x0` | Q7 #0 interrupt metadata |
| `0x3030` | `intr_info_1` | `metadata` | `[31:0]` | `0x0` | Q7 #1 interrupt metadata |
| `0x3034` | `intr_info_2` | `metadata` | `[31:0]` | `0x0` | Q7 #2 interrupt metadata |
| `0x3038` | `intr_info_3` | `metadata` | `[31:0]` | `0x0` | Q7 #3 interrupt metadata |
| `0x303C` | `intr_info_4` | `metadata` | `[31:0]` | `0x0` | Q7 #4 interrupt metadata |
| `0x3040` | `intr_info_5` | `metadata` | `[31:0]` | `0x0` | Q7 #5 interrupt metadata |
| `0x3044` | `intr_info_6` | `metadata` | `[31:0]` | `0x0` | Q7 #6 interrupt metadata |
| `0x3048` | `intr_info_7` | `metadata` | `[31:0]` | `0x0` | Q7 #7 interrupt metadata |

### 4.1 Run-stall / start-stop sequence

The host boot path for a Q7 core is: **set the entry PC, clear the stall bit,
assert start**. The reset state is "all 8 cores held in `RunStallOnReset`,
neither started"; FW clears one stall bit at a time so individual cores can be
brought up independently. Cross-link [`boot-reset`](../../uarch/boot-reset.md).
**[HIGH · INFERRED — register semantics OBSERVED, ordering INFERRED from field descriptions]**

```c
/* tpb_xt_local_reg q7 bundle, base = LOCAL_REG + 0x3000.
 * Release one Q7 sequencer core out of RunStallOnReset and start it.
 * `base` is the SoC-absolute LOCAL_REG of the engine (e.g. POOL = 0x2803060000),
 * so the q7 bundle is at base + 0x3000.
 * release_run_stall.run_stall is [7:0], reset 0xFF (all 8 stalled). */
static void q7_release_and_start(volatile uint32_t *base, unsigned core /*0..7*/)
{
    volatile uint32_t *q7 = base + (0x3000u / 4);   /* word-indexed */

    /* 1. (caller has already loaded the Q7 entry PC via its IRAM/start_addr.) */

    /* 2. Clear THIS core's stall bit: read-modify-write 0xFF -> bit cleared.
     *    Each bit gates one core's RunStallOnReset; writing 0 releases it. */
    uint32_t stall = q7[0x0u / 4];          /* release_run_stall @ +0x0 */
    stall &= ~(1u << core);                  /* drop bit `core` */
    q7[0x0u / 4] = stall;

    /* 3. Assert start_ctrl.ctrl = 1: declares start_addr valid, exits Halt,
     *    begins execution. start_ctrl is a single [0] bit @ +0x4. */
    q7[0x4u / 4] = 1u;                        /* start_ctrl.ctrl */

    /* 4. (optional) poll run_state_<core> @ +0x8 + core*4 for liveness. */
    /*    volatile uint32_t st = q7[(0x8u + core*4u) / 4]; */
}
```

> **GOTCHA.** `release_run_stall` is a *bitmask* — a blind `*reg = 0` releases
> **all eight** cores at once. To bring up one core, read-modify-write so the
> other seven stay stalled. The NX bundle's `release_run_stall` (`§3`) is a
> single `[0]` bit, so for NX `write 0` is unambiguous.

> **NOTE.** `run_state_<n>` is `[31:0]` and the schema describes it only as a
> status word — its encoding is not enumerated here (no enum in the JSON).
> Treat it as opaque; poll for change, not for a specific value. **[MED · OBSERVED]**

---

## 5. Bundle `window` — SoC↔Q7 address-translation windows (base `0x2000`, 40 instances, stride `0x1C`)

40 address-remap windows. Each window is `{control, mask, match, replace}` and
selects a requester class (any / NX-only / Q7-only / a single selected Q7) and
a 40-bit `match`/`mask` page key that, on hit, substitutes a relocated address.
This is the `LOCAL_REG` side of the windows documented end-to-end in
[`soc-q7-translation-windows`](../address/soc-q7-translation-windows.md).

Per-window register layout (relative offsets; instance `i` base = `0x2000 + i*0x1C`):

| rel | register | field | bits | reset | purpose |
|---|---|---|---|---|---|
| `0x00` | `control` | `window_valid` | `[0]` | `0x0` | enable window. **Clear before reconfig; set only after full state is valid.** |
| `0x00` | `control` | `nx_dedicated` | `[1]` | `0x0` | only NX-initiated requests match |
| `0x00` | `control` | `q7_dedicated` | `[2]` | `0x0` | only Q7-initiated requests match |
| `0x00` | `control` | `single_q7_enable` | `[3]` | `0x0` | only the Q7 selected by `[6:4]` matches |
| `0x00` | `control` | `single_q7_select` | `[6:4]` | `0x0` | which Q7 (0‥7); ignored if `[3]==0` |
| `0x00` | `control` | `reserved` | `[31:7]` | `0x0` | reserved (AccessType still `RW`) |
| `0x04` | `mask_lo` | `value` | `[31:20]` | `0x0` | `mask_value[31:20]` |
| `0x08` | `mask_hi` | `value` | `[7:0]` | `0x0` | `mask_value[39:32]` |
| `0x0C` | `match_lo` | `value` | `[31:20]` | `0x0` | `match_value[31:20]` |
| `0x10` | `match_hi` | `value` | `[7:0]` | `0x0` | `match_value[39:32]` |
| `0x14` | `replace_lo` | `value` | `[31:20]` | `0x0` | `replace_value[31:20]` |
| `0x18` | `replace_hi` | `value` | `[25:0]` | `0x0` | `replace_value[63:32]` |

> **QUIRK (split fields, page-aligned).** `mask`/`match` are **40-bit** keys
> presented as bits `[39:20]` only: `*_lo` holds `[31:20]` and `*_hi` holds
> `[39:32]`. The low 20 bits are **implicitly 0** — windows match on
> 1 MiB-aligned (`2^20`) pages, so there is no `[19:0]` field. `replace` is the
> 64-bit substitute split as `replace_lo[31:20]` + `replace_hi` = `[63:32]`.
> The bit ranges are taken verbatim from `Position`; do not assume contiguous
> 32-bit lo/hi halves.

### 5.1 Program one translation window

```c
/* tpb_xt_local_reg window bundle, base = LOCAL_REG + 0x2000, stride 0x1C.
 * Program window `w` to remap a Q7-issued page (mask/match) to `replace`.
 * mask/match cover bits [39:20]; replace covers [63:20] in the same split.
 * Low 20 bits are page-implicit-zero (1 MiB granularity). */
static void window_program(volatile uint32_t *base, unsigned w /*0..39*/,
                           uint64_t match40, uint64_t mask40, uint64_t replace64,
                           int q7_only, int single_q7 /* -1 = any, else 0..7 */)
{
    volatile uint32_t *win = base + ((0x2000u + w * 0x1Cu) / 4);

    /* 1. Disable the window first (window_valid must be 0 during reconfig). */
    win[0x00u / 4] = 0u;                                 /* control = 0 */

    /* 2. mask  : lo = bits[31:20], hi = bits[39:32]. */
    win[0x04u / 4] = (uint32_t)( mask40 & 0xFFF00000ull);            /* mask_lo  [31:20] */
    win[0x08u / 4] = (uint32_t)((mask40 >> 32) & 0xFFull);           /* mask_hi  [39:32] */

    /* 3. match : same split. */
    win[0x0Cu / 4] = (uint32_t)( match40 & 0xFFF00000ull);           /* match_lo [31:20] */
    win[0x10u / 4] = (uint32_t)((match40 >> 32) & 0xFFull);          /* match_hi [39:32] */

    /* 4. replace: lo = bits[31:20], hi = bits[63:32] (26-bit field). */
    win[0x14u / 4] = (uint32_t)( replace64 & 0xFFF00000ull);         /* replace_lo [31:20] */
    win[0x18u / 4] = (uint32_t)((replace64 >> 32) & 0x03FFFFFFull);  /* replace_hi [63:32] */

    /* 5. Finally build control and set window_valid LAST. */
    uint32_t ctrl = 1u;                                  /* window_valid = 1 */
    if (q7_only)        ctrl |= (1u << 2);               /* q7_dedicated */
    if (single_q7 >= 0) ctrl |= (1u << 3)                /* single_q7_enable */
                              | ((uint32_t)(single_q7 & 0x7) << 4); /* single_q7_select */
    win[0x00u / 4] = ctrl;
}
```

> **GOTCHA (valid-bit ordering).** The `window_valid` description is explicit:
> clear it before touching any other window state, set it **only after** mask /
> match / replace are fully written. Writing `control` first (or never clearing
> it) lets the remap fire on a half-programmed key.

---

## 6. Bundle `tensor_replace` — 4D strided-replace descriptor (base `0x5000`, 32 instances, stride `0x20`)

32 instances of a 4D tensor descriptor. Each descriptor packs **two 16-bit
subfields per 32-bit register** across four registers: per-axis step size
(X/Y/Z/W) and per-axis element count (X/Y/Z/W).

Per-instance layout (relative offsets; instance `i` base = `0x5000 + i*0x20`):

| rel | register | field | bits | reset | purpose |
|---|---|---|---|---|---|
| `0x0` | `tensor_4d_dim_0` | `step_size_x` | `[15:0]` | `0x0` | X step size |
| `0x0` | `tensor_4d_dim_0` | `step_size_y` | `[31:16]` | `0x0` | Y step size |
| `0x4` | `tensor_4d_dim_1` | `step_size_z` | `[15:0]` | `0x0` | Z step size |
| `0x4` | `tensor_4d_dim_1` | `step_size_w` | `[31:16]` | `0x0` | W step size |
| `0x8` | `tensor_4d_dim_2` | `num_elem_x` | `[15:0]` | `0x0` | X element count |
| `0x8` | `tensor_4d_dim_2` | `num_elem_y` | `[31:16]` | `0x0` | Y element count |
| `0xC` | `tensor_4d_dim_3` | `num_elem_z` | `[15:0]` | `0x0` | Z element count |
| `0xC` | `tensor_4d_dim_3` | `num_elem_w` | `[31:16]` | `0x0` | W element count |

> **NOTE.** The eight logical subfields (`step_size_*`, `num_elem_*`) are
> **packed into four registers**, not eight; the `0x10`‥`0x1F` tail of each
> `0x20` slot is unused. Steps and counts are `uint16`.

### 6.1 Program one tensor-replace descriptor

```c
/* tpb_xt_local_reg tensor_replace bundle, base = LOCAL_REG + 0x5000, stride 0x20.
 * Pack a 4D (X,Y,Z,W) strided descriptor `d` (0..31): per-axis 16-bit step + count. */
static inline uint32_t pack16(uint16_t lo, uint16_t hi)
{
    return (uint32_t)lo | ((uint32_t)hi << 16);
}

static void tensor_replace_program(volatile uint32_t *base, unsigned d /*0..31*/,
                                   const uint16_t step[4] /*x,y,z,w*/,
                                   const uint16_t num[4]  /*x,y,z,w*/)
{
    volatile uint32_t *t = base + ((0x5000u + d * 0x20u) / 4);
    t[0x0u / 4] = pack16(step[0], step[1]);  /* dim_0: step_x | step_y */
    t[0x4u / 4] = pack16(step[2], step[3]);  /* dim_1: step_z | step_w */
    t[0x8u / 4] = pack16(num[0],  num[1]);   /* dim_2: num_x  | num_y  */
    t[0xCu / 4] = pack16(num[2],  num[3]);   /* dim_3: num_z  | num_w  */
}
```

---

## 7. Bundle `hw_decode` — HW instruction-decode + breakpoint/KDB debug (base `0x4000`, 1 instance)

Configures the HW instruction-decode path (vs the instruction FIFO) and the
full KDB breakpoint surface: instruction breakpoints, two opcode-instance
counters (IC0/IC1), two address breakpoints, single-step, and immediate pause.
Cross-link the CAM programming side in
[`hw-decode-cam-programming`](../../runtime/hw-decode-cam-programming.md) and the
debugger driver in [`uarch-debugger`](../../firmware/seq/uarch-debugger.md).

| off | register | field | bits | reset | purpose |
|---|---|---|---|---|---|
| `0x4000` | `control` | `disable_hw_decode` | `[0]` | `0x0` | select Instr-FIFO vs HW-decode path. Cannot change while traffic flows / not in halt. SP uses in production; chicken bit elsewhere. |
| `0x4000` | `control` | `instr_ordering_mode` | `[1]` | `0x0` | strong/relaxed ordering (NX sets per `SetOrderingMode` / `ctrl_om`). Strong ⇒ pause at sync_stage, release one-by-one on `engine_drain_done`. |
| `0x4000` | `control` | `iram_block_size_mask` | `[18:2]` | `0x1FFF` | mask cache-block index off IRAM addr → PC lower bits (rtl builds full PC from PC_upper + IRAM_offset) |
| `0x4000` | `control` | `iram_ctrl_flush_en` | `[20]` | `0x0` | enable IRAM-ctrl flush (**SP/TOP instance only**) |
| `0x4004` | `breakpoint_ctrl` | `breakpoint_instr_enable` | `[0]` | `0x1` | enable instruction breakpoint |
| `0x4004` | `breakpoint_ctrl` | `breakpoint_profile_table_enable` | `[1]` | `0x1` | enable bp sourced from profile table |
| `0x4004` | `breakpoint_ctrl` | `ucode_force_pause_enable` | `[2]` | `0x1` | enable `force_pause` output of NX |
| `0x4004` | `breakpoint_ctrl` | `breakpoint_step_valid` | `[3]` | `0x0` | `num_instr` valid (step mode); uCode toggles 0→1 then `resume_issue` |
| `0x4004` | `breakpoint_ctrl` | `breakpoint_addr0_valid` | `[4]` | `0x0` | enable bp address-0 check |
| `0x4004` | `breakpoint_ctrl` | `breakpoint_addr1_valid` | `[5]` | `0x0` | enable bp address-1 check |
| `0x4004` | `breakpoint_ctrl` | `ic0_valid` | `[6]` | `0x0` | clear-on-invalid / start-on-valid HW IC0 counter |
| `0x4004` | `breakpoint_ctrl` | `ic1_valid` | `[7]` | `0x0` | clear-on-invalid / start-on-valid HW IC1 counter |
| `0x4004` | `breakpoint_ctrl` | `immediate_pause` | `[8]` | `0x0` | immediate pause after current instr (host debug) |
| `0x4008` | `breakpoint_step` | `num_instr` | `[31:0]` | `0x0` | KDB "step N instr" count; rtl decrements to 0 then pauses at sync_stage |
| `0x400C` | `breakpoint_match` | `ic0_mask` | `[7:0]` | `0x0` | mask for IC0 opcode counter |
| `0x400C` | `breakpoint_match` | `ic0_opcode` | `[15:8]` | `0x0` | opcode for IC0 (`instr.opcode & ic0_mask`) |
| `0x400C` | `breakpoint_match` | `ic1_mask` | `[23:16]` | `0x0` | mask for IC1 opcode counter |
| `0x400C` | `breakpoint_match` | `ic1_opcode` | `[31:24]` | `0x0` | opcode for IC1 (`instr.opcode & ic1_mask`) |
| `0x4010` | `breakpoint_ic0` | `value` | `[31:0]` | `0x0` | instr-count threshold to match IC0 |
| `0x4014` | `breakpoint_ic1` | `value` | `[31:0]` | `0x0` | instr-count threshold to match IC1 |
| `0x4018` | `breakpoint_addr0_lo` | `value` | `[31:0]` | `0x0` | bp addr0 low — HW-decode pauses after this address |
| `0x401C` | `breakpoint_addr0_hi` | `value` | `[31:0]` | `0x0` | bp addr0 high |
| `0x4020` | `breakpoint_addr1_lo` | `value` | `[31:0]` | `0x0` | bp addr1 low |
| `0x4024` | `breakpoint_addr1_hi` | `value` | `[31:0]` | `0x0` | bp addr1 high |
| `0x4028` | `profile_cam_search_vector` | `profile_cam_search_byte_0` | `[5:0]` | `0x0` | CAM-search byte0 select (0‥63) |
| `0x4028` | `profile_cam_search_vector` | `profile_cam_search_byte_1` | `[13:8]` | `0x1` | CAM-search byte1 select |
| `0x4028` | `profile_cam_search_vector` | `profile_cam_search_byte_2` | `[21:16]` | `0x2` | CAM-search byte2 select |
| `0x4028` | `profile_cam_search_vector` | `profile_cam_search_byte_3` | `[29:24]` | `0x3` | CAM-search byte3 select |
| `0x402C` | `hw_decode_flush_cntr` | `value` | `[7:0]` | `0x10` | cycles to wait before issuing flush-done after the HW instr queue empties |
| `0x4030` | `spare0` | `value` | `[31:0]` | `0x0` | spare register |
| `0x4034` | `spare1` | `value` | `[31:0]` | `0x0` | spare register |

> **NOTE (reset asymmetry).** Three `breakpoint_ctrl` enables reset to **1**
> (`breakpoint_instr_enable`, `breakpoint_profile_table_enable`,
> `ucode_force_pause_enable`) — the breakpoint engine is **armed at reset**; the
> *triggers* (`*_valid`, `immediate_pause`) reset to 0. The CAM-search byte
> selectors default to the identity sequence `{0,1,2,3}`, and
> `hw_decode_flush_cntr` resets to `0x10` cycles.

> **QUIRK.** `iram_block_size_mask` is **17 bits** (`[18:2]`) — not a clean
> power-of-two field — and resets to `0x1FFF` (13 set bits). It masks the cache
> block index out of the IRAM address so rtl can reconstruct the PC. Do not
> assume it spans the whole register.

### 7.1 Arm an address breakpoint

```c
/* hw_decode bundle, base = LOCAL_REG + 0x4000.
 * Arm breakpoint slot 0 to pause HW-decode after a 64-bit instruction address. */
static void hw_decode_break_at(volatile uint32_t *base, uint64_t addr)
{
    volatile uint32_t *hd = base + (0x4000u / 4);

    hd[0x18u / 4] = (uint32_t)(addr & 0xFFFFFFFFu);          /* breakpoint_addr0_lo */
    hd[0x1Cu / 4] = (uint32_t)(addr >> 32);                  /* breakpoint_addr0_hi */

    uint32_t ctl = hd[0x4u / 4];                             /* breakpoint_ctrl */
    ctl |= (1u << 0)        /* breakpoint_instr_enable (already 1 at reset) */
         | (1u << 4);       /* breakpoint_addr0_valid: enable addr-0 check  */
    hd[0x4u / 4] = ctl;
    /* On hit, HW-decode pauses at the sync_stage; resume via the debugger. */
}
```

---

## 8. Bundle `notific` — SW-queue mapping + timestamp (base `0x6000`, 1 instance)

Maps each Q7 sequencer's generated notifications to a SW queue number and sets
the timestamp increment.

| off | register | field | bits | reset | purpose |
|---|---|---|---|---|---|
| `0x6000` | `sw_queue_num0` | `Q7_0` | `[3:0]` | `0x0` | SW queue num for Q7_0 notifications |
| `0x6000` | `sw_queue_num0` | `Q7_1` | `[7:4]` | `0x0` | SW queue num for Q7_1 notifications |
| `0x6000` | `sw_queue_num0` | `Q7_2` | `[11:8]` | `0x0` | SW queue num for Q7_2 notifications |
| `0x6000` | `sw_queue_num0` | `Q7_3` | `[15:12]` | `0x0` | SW queue num for Q7_3 notifications |
| `0x6004` | `cfg_timestamp_inc` | `val` | `[23:0]` | `0xb2924` | timestamp increment, clock-frequency derived |

> **GOTCHA.** `sw_queue_num0` maps **only Q7_0‥Q7_3** (four 4-bit nibbles).
> There is **no** `sw_queue_num1` register in this block — the upper four cores
> Q7_4‥Q7_7 are not mapped here (handled elsewhere, or a schema omission).
> Do not assume an 8-core nibble array. **[MED · OBSERVED]**

---

## 9. Bundle `general` — scratch local registers (base `0x1000`, 60 instances, stride `0x20`)

| rel | register | field | bits | reset | purpose |
|---|---|---|---|---|---|
| `0x0` | `lr` | `value` | `[31:0]` | `0x0` | general local register (`glr`) for NX/Q7 |

60 instances at `0x1000 + i*0x20` (`lr[0]`@`0x1000` ‥ `lr[59]`@`0x1760`); one
register per `0x20` slot, the remaining `0x1C` is unused.

---

## 10. Interrupt control summary

Two separate interrupt apertures, one per core domain.

| domain | enable reg | width | metadata regs | semantics |
|---|---|---|---|---|
| NX | `nx.intr_ctrl.en` | `[3:0]` | `nx.intr_info` `[31:0]` | 4 sources for the single NX core |
| Q7 | `q7.intr_ctrl.en` | `[31:0]` | `q7.intr_info_0..7` `[31:0]` | **4 bits per Q7** (8 × 4 = 32); per-core metadata word |

The Q7 `intr_ctrl` packs all 8 cores' enables into one 32-bit register
(`core c` → bits `[4c+3 : 4c]`); each core then has its own `intr_info_<c>`
metadata word. **[HIGH · OBSERVED for the 4-bit-per-core layout; the exact 4
source meanings are not enumerated in the JSON — MED · INFERRED]**

```c
/* Enable all 4 interrupt sources for Q7 core `c` (0..7). */
static void q7_enable_intr(volatile uint32_t *base, unsigned c)
{
    volatile uint32_t *q7 = base + (0x3000u / 4);
    uint32_t en = q7[0x28u / 4];             /* q7.intr_ctrl @ +0x28 */
    en |= (0xFu << (4u * c));                 /* 4 bits for core c */
    q7[0x28u / 4] = en;
}
```

---

## 11. Per-generation applicability

Compared against the sibling arch-regs trees shipped in the customop library.
**[HIGH · OBSERVED for cayman/mariana/mariana_plus/maverick; v2/V1 INFERRED]**

| arch | NC gen | regs / fields | bundles vs Cayman | `SizeInBytes` | `q7.release_run_stall` |
|---|---|---|---|---|---|
| `cayman` | NC-v3 | **55 / 84** | base set (this page) | `0x10000` | `[7:0]` reset `0xFF` |
| `mariana` | NC-v4 | 59 / 88 | **+4 atomic bundles** | `0x10000` | `[7:0]` reset `0xFF` |
| `mariana_plus` | NC-v4 | 59 / 88 | **+4 atomic bundles** | `0x10000` | `[7:0]` reset `0xFF` |
| `maverick` | NC-v5 | 59 / 88 | **+4 atomic bundles** | `0x10000` | `[7:0]` reset `0xFF` |
| `sunda` | NC-v2 | — | no `tpb_xt_local_reg.json` shipped | — | INFERRED present |
| `tonga` | V1 | — | no `tpb_xt_local_reg.json` shipped | — | INFERRED present |

The four added bundles on v4/v5 are **atomic-op apertures** appended after
`notific`, each `ArraySize=64` with a single `value[31:0]` register:

| bundle | base | `ArraySize` | stride | purpose |
|---|---|---|---|---|
| `atomic_rdwr` | `0x8000` | 64 | `0x10` | atomic read-write slots |
| `atomic_inc` | `0x8400` | 64 | `0x10` | atomic increment slots |
| `atomic_dec` | `0x8800` | 64 | `0x10` | atomic decrement slots |
| `atomic_addiff0` | `0x8C00` | 64 | `0x10` | atomic add-if-zero slots |

> **NOTE (per-gen).** All five core control surfaces this page documents — the
> NX/Q7 run-stall release, the 40 translation windows, the 4D tensor-replace
> descriptor, the HW-decode/breakpoint block, and the notification mapping —
> are **byte-identical** across cayman/mariana/mariana_plus/maverick at the
> same offsets (`nx`@`0x0000`, `window`@`0x2000`, `q7`@`0x3000`,
> `hw_decode`@`0x4000`, `tensor_replace`@`0x5000`, `notific`@`0x6000`). The
> v4/v5 delta is **additive only** (the `0x8000`-region atomic apertures inside
> the previously-RESERVED upper half of the same `0x10000` aperture). A Cayman
> reimplementation that ignores the atomic region is forward-compatible for the
> control path. **[HIGH · OBSERVED for v3/v4/v5; v2/V1 INFERRED — not shipped]**

> **CORRECTION — "7 bundles / 55 regs / 84 fields" is Cayman-only.** That count
> is often quoted as *the* layout. It is exact **for Cayman**, but the v4/v5
> arch-regs trees ship **11 bundles / 59 regs / 88 fields** — the four `atomic_*`
> apertures at `0x8000`‥`0x8FFF` are missed by any survey that does not
> cross-check the maverick/mariana trees. All offsets, resets, and field positions
> outside those four bundles are unchanged.

---

## 12. Cross-references

- **Boot / reset sequencing** (when FW clears run-stall): [`boot-reset`](../../uarch/boot-reset.md)
- **SoC↔Q7 translation windows** (the `window` bundle, full address view): [`soc-q7-translation-windows`](../address/soc-q7-translation-windows.md)
- **HW-decode CAM programming** (the `profile_cam_search_vector` / breakpoint CAM): [`hw-decode-cam-programming`](../../runtime/hw-decode-cam-programming.md)
- **SEQ uarch-debugger** (drives `breakpoint_*` / `breakpoint_step`): [`uarch-debugger`](../../firmware/seq/uarch-debugger.md)
- **Address-side view** (`LOCAL_REG` placement, the `0x2803063000` doorbell): [`tpb-pool`](../address/tpb-pool.md)

---

## Appendix A — full register map (offset / name / width / access)

All 55 Cayman registers, absolute byte offsets (instance 0 for indexed
bundles). Every register is 32-bit `RW`.

| abs off | bundle\[i] | register | width | access |
|---|---|---|---|---|
| `0x0000` | `nx` | `release_run_stall` | 32 | RW |
| `0x0004` | `nx` | `start_ctrl` | 32 | RW |
| `0x0008` | `nx` | `run_state` | 32 | RW |
| `0x000C` | `nx` | `dma_rx_base` | 32 | RW |
| `0x0010` | `nx` | `dma_tx_base` | 32 | RW |
| `0x0014` | `nx` | `instr_halt_ctrl` | 32 | RW |
| `0x0018` | `nx` | `intr_ctrl` | 32 | RW |
| `0x001C` | `nx` | `intr_info` | 32 | RW |
| `0x1000`+`i*0x20` | `general[0..59]` | `lr` | 32 | RW |
| `0x2000`+`i*0x1C`+`0x00` | `window[0..39]` | `control` | 32 | RW |
| `0x2000`+`i*0x1C`+`0x04` | `window[0..39]` | `mask_lo` | 32 | RW |
| `0x2000`+`i*0x1C`+`0x08` | `window[0..39]` | `mask_hi` | 32 | RW |
| `0x2000`+`i*0x1C`+`0x0C` | `window[0..39]` | `match_lo` | 32 | RW |
| `0x2000`+`i*0x1C`+`0x10` | `window[0..39]` | `match_hi` | 32 | RW |
| `0x2000`+`i*0x1C`+`0x14` | `window[0..39]` | `replace_lo` | 32 | RW |
| `0x2000`+`i*0x1C`+`0x18` | `window[0..39]` | `replace_hi` | 32 | RW |
| `0x3000` | `q7` | `release_run_stall` | 32 | RW |
| `0x3004` | `q7` | `start_ctrl` | 32 | RW |
| `0x3008`‥`0x3024` | `q7` | `run_state_0..7` | 32 | RW |
| `0x3028` | `q7` | `intr_ctrl` | 32 | RW |
| `0x302C`‥`0x3048` | `q7` | `intr_info_0..7` | 32 | RW |
| `0x4000` | `hw_decode` | `control` | 32 | RW |
| `0x4004` | `hw_decode` | `breakpoint_ctrl` | 32 | RW |
| `0x4008` | `hw_decode` | `breakpoint_step` | 32 | RW |
| `0x400C` | `hw_decode` | `breakpoint_match` | 32 | RW |
| `0x4010` | `hw_decode` | `breakpoint_ic0` | 32 | RW |
| `0x4014` | `hw_decode` | `breakpoint_ic1` | 32 | RW |
| `0x4018`‥`0x4024` | `hw_decode` | `breakpoint_addr0/1_lo/hi` | 32 | RW |
| `0x4028` | `hw_decode` | `profile_cam_search_vector` | 32 | RW |
| `0x402C` | `hw_decode` | `hw_decode_flush_cntr` | 32 | RW |
| `0x4030` | `hw_decode` | `spare0` | 32 | RW |
| `0x4034` | `hw_decode` | `spare1` | 32 | RW |
| `0x5000`+`i*0x20`+`0x0` | `tensor_replace[0..31]` | `tensor_4d_dim_0` | 32 | RW |
| `0x5000`+`i*0x20`+`0x4` | `tensor_replace[0..31]` | `tensor_4d_dim_1` | 32 | RW |
| `0x5000`+`i*0x20`+`0x8` | `tensor_replace[0..31]` | `tensor_4d_dim_2` | 32 | RW |
| `0x5000`+`i*0x20`+`0xC` | `tensor_replace[0..31]` | `tensor_4d_dim_3` | 32 | RW |
| `0x6000` | `notific` | `sw_queue_num0` | 32 | RW |
| `0x6004` | `notific` | `cfg_timestamp_inc` | 32 | RW |

> **Schema cross-check.** 8 (`nx`) + 1 (`general`) + 7 (`window`) + 19 (`q7`) +
> 14 (`hw_decode`) + 4 (`tensor_replace`) + 2 (`notific`) = **55 registers**,
> **84 bitfields** — matches the JSON exactly.
