# CSR — `tpb` (top-level cluster control)

> **Scope.** The `tpb` register file is the **engine-cluster top-level control
> surface** — the APB-mapped block that sits *above* the four compute-engine
> sequencers (**PE / POOL / ACT / DVE**) and owns two things the per-engine
> [`tpb_xt_local_reg`](tpb-xt-local-reg.md) aperture does **not**: the
> **shared arithmetic-datapath config** (rounding mode, FP8 bias, stochastic-
> rounding seeds, FP32 NaN/Inf special-value compares) and the **cluster
> event/notification routing** (`sw_queue_num*`, `queue_idx_ctrl`, the
> `events_semaphores` master enable). It is the bitfield-level companion to the
> address-side EVT_SEM view in
> [`evt-sem-regions.md`](../address/evt-sem-regions.md) and the queue model in
> [`notific-queue.md`](notific-queue.md).
>
> **Provenance.** Every offset / reset / bit-range / verbatim description below
> is read directly from the shipped register-description schema
> `csrs/tpb/tpb.json` (binary-derived CSR JSON, 123 778 bytes, **Cayman / NC-v3**
> arch-regs tree). The schema is the lawful, citeable artifact; all counts here
> are **re-derived from scratch** with `jq`/`python`, not carried from any
> upstream tally. Confidence tags: HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED.

---

## 1. The keystone: `tpb` vs `tpb_xt_local_reg`

This is the single most important distinction on the page. The two register
files have **disjoint jobs** and live at different addresses with different
aperture sizes: **[HIGH · OBSERVED]**

| | `tpb` (this page) | [`tpb_xt_local_reg`](tpb-xt-local-reg.md) |
|---|---|---|
| Scope | **cluster, above the engines** | **per-engine, inside one sequencer** |
| `AddrWidth` / `SizeInBytes` | `12` / `0x1000` (4 KiB) | `16` / `0x10000` (64 KiB) |
| `InterfaceType` | `APB` | `APB` |
| Owns | datapath config (rounding/bias/stochastic/NaN-Inf), cluster event routing, engine status/PC, perf counters, intc bypass | NX + 8×Q7 run-stall release / start, SoC↔Q7 translation windows, 4D tensor-replace, HW-decode breakpoints |
| `notific` surface | routes **all** classes (4 engines + SP + events + errors + HAM + **all 8 Q7**) to SW queues | maps a *local* subset (Q7_0..Q7_3) |
| Gating it provides | DATAPATH behavior, EVENT visibility, POOL/DVE PARALLELISM | run/clock/reset, halt, address translation |

> The per-engine **release / clock / reset / run-stall** machinery is **not in
> `tpb`** — it lives in [`tpb_xt_local_reg`](tpb-xt-local-reg.md)
> (`release_run_stall` / `start_ctrl`). `tpb` never gates power or clock; it
> gates *how arithmetic rounds*, *whether and where events surface*, and
> *whether POOL and DVE co-issue*. The two are complementary, not duplicate.
> **[HIGH · OBSERVED]**

---

## 2. Regfile metadata

Read from `.RegFile` of `tpb.json`. **[HIGH · OBSERVED]**

| Property | Value | Meaning |
|---|---|---|
| `UnitName` | `tpb` | regfile name |
| `Type` | `REGFILE` | flat register file, no sub-regfiles |
| `RegfileFlavor` | `POSEDGE` | posedge-clocked flops |
| `InterfaceType` | `APB` | APB-mapped control bus |
| `AddrWidth` | `12` | 12-bit byte address → **4 KiB** span |
| `DataWidth` | `32` | every register is 32 bits, word-addressed |
| `SizeInBytes` | `0x1000` | **4 KiB aperture** |
| `HalName` / `HalFilenameUnitName` / `Description` | `""` | all empty |
| `Memories` / `Parameters` / `Includes` | `[]` | all empty |

### Counts (re-derived, with CORRECTION vs `SX-CSR-04`)

`jq`/`python` over `tpb.json` gives **9 bundle-arrays, 146 registers, 185
bitfields**. The bundle/register/field totals match the backing survey, but the
**per-bundle field tallies** in `SX-CSR-04` were miscounted; the byte-exact
truth is below. **[HIGH · OBSERVED]**

> **CORRECTION (vs `SX-CSR-04`).** The report's per-bundle field column read
> `pool=11`, `misc=23`, `performance_counter=63`. The schema actually has
> `pool_sequencer=10` fields (2 of those on the single `stochastic_rnd`
> register), `misc=22` fields (the only multi-field misc reg is `chicken_ctrl`,
> 2 fields), and `performance_counter=66` fields — the three
> `*_perf_cntr_ctrl` registers each carry **two** fields (`en` + `reset`), so
> `60` data words `+ 3×2` = `66`, not `63`. The grand total is unaffected:
> `6+10+24+13+3+33+22+66+8 = 185`. **[HIGH · OBSERVED]**

| Bundle | Base | ArraySize | Regs | Fields | Abs span |
|---|---|---|---|---|---|
| `pe_sequencer` | `0x000` | 1 | 6 | 6 | `0x000..0x0FF` |
| `pool_sequencer` | `0x100` | 1 | 9 | 10 | `0x100..0x1FF` |
| `act_sequencer` | `0x200` | 1 | 18 | 24 | `0x200..0x2FF` |
| `dve_sequencer` | `0x300` | 1 | 11 | 13 | `0x300..0x3FF` |
| `events_semaphores` | `0x800` | 1 | 3 | 3 | `0x800..0x9FF` |
| `notific` | `0xA00` | 1 | 7 | 33 | `0xA00..0xAFF` |
| `misc` | `0xB00` | 1 | 21 | 22 | `0xB00..0xBFF` |
| `performance_counter` | `0xC00` | 1 | 63 | 66 | `0xC00..0xDFF` |
| `intc_bypass` | `0xE00` | 1 | 8 | 8 | `0xE00..0xE1F` |
| **totals** | | | **146** | **185** | |

All bundles have `ArraySize=1`, so **absolute offset = bundle base +
register `AddressOffset`**. Two address gaps exist inside the 4 KiB aperture:
`0x400..0x7FF` (1 KiB, between `dve_sequencer` and `events_semaphores`) and
`0xE20..0xFFF` (tail after `intc_bypass`). **[HIGH · OBSERVED]**

### Access / reset distribution

| Dimension | Distribution |
|---|---|
| Register `AccessType` | `RO:78`, `RW:64`, `WO:4` (no `Reserved`) |
| BitField `AccessType` | `RO:78`, `RW:100`, `WO:7` |
| `SpecialAccess` | `None:181`, `PulseOnW:4` |
| Non-zero field resets | `0x1`×5 (status ready + `chicken_ctrl.pool_dve_arb_en`), `0xffffffff`×11 (high thresh / Inf masks / spare2-3), `0x7f800000` (+Inf), `0xff800000` (−Inf), `0x7fc00000`×2 (qNaN val+mask) |

> Unlike the `xtensa_q7` / `xtensa_nx` sibling blocks, `tpb` genuinely uses
> **`WO`** (the 4 seed registers: `pool/act/dve stochastic_rnd` and `dve
> lfsr`) and a real **`PulseOnW`** class (4 fields). No `0x000000b1`
> generator-placeholder reset appears anywhere; every non-zero reset is a
> correct IEEE-754 FP32 constant, a clock-derived timestamp increment, a
> status=ready, or an all-ones ECO spare. Treat resets here as **verified**.
> **[HIGH · OBSERVED]**

---

## 3. Shared arithmetic-datapath config

The four sequencer bundles (`pe`/`pool`/`act`/`dve` @ `0x000`/`0x100`/`0x200`/
`0x300`) hold the per-engine slice of a **common datapath-config template**.
Every engine carries `instr_dbg_ctrl.dbg_level[3:0]` and `timestamp_inc_val
[23:0]`; the arithmetic-relevant fields are layered on top. PE is the spare
case — **no register at `0x000`, no `bias_adjust`, no `stochastic_rnd`** (its
systolic array does not run the output data-converter path). **[HIGH · OBSERVED]**

### 3.1 FP8 bias adjust

`bias_adjust.bias[5:0]` (RW, reset `0x0`) — *"FP8 bias adjust"* — present on
**POOL `0x100` / ACT `0x200` / DVE `0x300`**, absent on PE. A 6-bit signed-ish
exponent-bias offset applied in the FP8 output converter; firmware programs it
per dtype before issuing FP8-producing instructions. **[HIGH · OBSERVED]**

### 3.2 Stochastic-rounding seeds (the WO seed bank)

This is the heart of the shared datapath. Three engines expose a write-only
`stochastic_rnd` register feeding the output data-converter's stochastic-
rounding adder, plus a mode bit: **[HIGH · OBSERVED]**

| Abs | Engine | Register | Field | Bits | Acc | Verbatim description |
|---|---|---|---|---|---|---|
| `0x10C` | POOL | `stochastic_rnd` | `seed` | `[20:0]` | **WO** | Seed value for output data converter stochastic rounding |
| `0x10C` | POOL | `stochastic_rnd` | `dtype` | `[31:28]` | **WO** | Data type for which the seed value is used for in output data converter stochastic rounding |
| `0x110` | POOL | `stochastic_rnd_mode` | `mode` | `[0]` | RW | Rounding mode, 0=IEEE-RNE, 1=Stochastic Rounding |
| `0x20C` | ACT | `stochastic_rnd` | `seed` | `[20:0]` | **WO** | (same) |
| `0x20C` | ACT | `stochastic_rnd` | `dtype` | `[31:28]` | **WO** | (same) |
| `0x210` | ACT | `stochastic_rnd_mode` | `mode` | `[0]` | RW | (same) |
| `0x30C` | DVE | `stochastic_rnd` | `seed` | `[20:0]` | **WO** | (same) |
| `0x30C` | DVE | `stochastic_rnd` | `dtype` | `[31:28]` | **WO** | (same) |
| `0x310` | DVE | `stochastic_rnd_mode` | `mode` | `[0]` | RW | (same) |
| `0x314` | DVE | `lfsr` | `seed` | `[31:0]` | **WO** | Seed value for LFSR for RNG instruction |

Semantics: the **21-bit `seed`** initialises the per-dtype stochastic-rounding
PRNG inside the output converter; the **4-bit `dtype`** selects *which* output
data type that seed applies to (so a single 32-bit write both targets and seeds
one dtype slot). `stochastic_rnd_mode.mode` is the master switch — `0` =
IEEE round-to-nearest-even, `1` = stochastic rounding. Because the seed
register is **WO**, reads are meaningless: firmware must keep a shadow copy if
it needs to re-derive state. The DVE-only `lfsr.seed[31:0]` is a *separate*
write-only seed for the RNG **instruction** path (distinct from the output-
converter stochastic rounder above). **[HIGH · OBSERVED]**

> **Cross-link.** The firmware that manages these PRNG seeds — save/restore of
> RNG state, dtype-keyed seeding, and the LFSR generator dispatch — is
> documented in [`rng-seed-state-ops.md`](../../firmware/kernels/rng-seed-state-ops.md)
> (the `0x77`/`0x78` RandGet/SetState checkpoint pair) and
> [`rng-lfsr-dispatch.md`](../../firmware/kernels/rng-lfsr-dispatch.md) (the
> LFSR generator that `dve_sequencer.lfsr.seed` initialises). **[HIGH · OBSERVED]**

### 3.3 ACT special-value (NaN/Inf) compare bank

The ACT sequencer is the richest — it adds an 8-register **special-value
compare bank** at `0x240..0x25F`. Each special value has a `val`/`mask` pair;
the engine compares an incoming FP32 word `w` against `(w & mask) == val` to
classify it as zero / NaN / +Inf / −Inf during activation post-processing. The
reset values are the **correct IEEE-754 bit patterns**, which is strong
evidence they are intentional, not placeholders: **[HIGH · OBSERVED]**

| Abs | Register | Field | Reset | Meaning |
|---|---|---|---|---|
| `0x240` | `zero_val` | `val_NT_[31:0]` | `0x0` | FP32 Zero value to compare against |
| `0x244` | `zero_mask` | `mask_NT_[31:0]` | `0x0` | FP32 Zero mask (matches +0 and −0) |
| `0x248` | `nan_val` | `val_NT_[31:0]` | `0x7fc00000` | FP32 NaN value (quiet-NaN bits) |
| `0x24C` | `nan_mask` | `mask_NT_[31:0]` | `0x7fc00000` | FP32 NaN mask |
| `0x250` | `pos_inf_val` | `val_NT_[31:0]` | `0x7f800000` | FP32 +Inf value |
| `0x254` | `pos_inf_mask` | `mask_NT_[31:0]` | `0xffffffff` | FP32 +Inf mask |
| `0x258` | `neg_inf_val` | `val_NT_[31:0]` | `0xff800000` | FP32 −Inf value |
| `0x25C` | `neg_inf_mask` | `mask_NT_[31:0]` | `0xffffffff` | FP32 −Inf mask |

### 3.4 ACT 6-lane rounding-mode register

`act_sequencer.rnd_mode @ 0x2C0` packs six **independent 2-bit** rounding-mode
selectors, one per FMA/subtractor lane of the activation datapath. Each lane is
byte-aligned with a 2-bit gap (bits `[3:2]`, `[7:6]`, … are unused):
**[HIGH · OBSERVED]**

| Field | Bits | Verbatim |
|---|---|---|
| `dequant_bias_fma_NT_` | `[1:0]` | Rounding mode for dequant bias FMA in Activation engine |
| `fma0_NT_` | `[5:4]` | Rounding mode for FMA0 in Activation engine |
| `fma1_NT_` | `[9:8]` | Rounding mode for FMA1 in Activation engine |
| `fma2_NT_` | `[13:12]` | Rounding mode for FMA2 in Activation engine |
| `base_subtractor_NT_` | `[17:16]` | Rounding mode for base subtractor in Activation engine |
| `sym_point_subtractor_NT_` | `[21:20]` | Rounding mode for Symmetry point subtractor in Activation engine |

### 3.5 DVE pipeline-ordering chicken + FP-error disable

`dve_sequencer.misc_ctrl.safe_instruction_ready[0]` (RW, reset `0x0`) makes
instruction-ready come from the **end** of the DVE pipeline (an ordering/safety
chicken bit). `dve_sequencer.spare_register0` is the *only* "spare" register in
the file carrying a functional named field:
`disable_dve_int_fp_error[0]` (RW), which suppresses DVE internal FP errors.
The DVE `timestamp_inc_val` resets to **`0xdf36e`** (914 286) versus
**`0xb2924`** (730 404) on PE/POOL/ACT — a ~1.25× ratio that indicates DVE runs
on a distinct clock. **[HIGH · OBSERVED for values; MED · INFERRED for the
clock-ratio interpretation]**

---

## 4. Cluster event / notification routing

The `notific` bundle @ `0xA00` is the **cluster-level event-routing table**: it
maps every notification source to a 4-bit SW-queue number and selects where the
`queue_idx` comes from. The master on/off is one bit in `events_semaphores`.

### 4.1 `events_semaphores` bundle @ `0x800` (incl. `notific_ctrl @ 0x808`)

| Abs | Register | Field | Bits | Acc | Reset | Verbatim |
|---|---|---|---|---|---|---|
| `0x800` | `sem_threshold_ctrl0` | `low_NT_` | `[31:0]` | RW | `0x0` | Semaphore value low threshold |
| `0x804` | `sem_threshold_ctrl1` | `high_NT_` | `[31:0]` | RW | `0xffffffff` | Semaphore value high threshold |
| `0x808` | `notific_ctrl` | `notifications_en` | `[0]` | RW | `0x0` | Enable events/semaphores notifications |

> **Anchor (consistent with `#901`/`#902`).** The cluster master notification
> enable `notific_ctrl.notifications_en` lives **here in `tpb.json`'s
> `events_semaphores` bundle at abs `0x808`** — *not* in
> [`tpb_xt_local_reg`](tpb-xt-local-reg.md). The `sem_threshold_ctrl{0,1}`
> pair brackets the semaphore value `[low,high]` that triggers an
> event/semaphore notification; the EVT_SEM register windows those events
> target (256 events + 256 semaphores, container `TPB_0_EVT_SEM @
> 0x2802700000`, read/set/inc/dec windows at `+0x1000`/`+0x1400`/`+0x1800`/
> `+0x1C00`) are decoded in
> [`evt-sem-regions.md`](../address/evt-sem-regions.md). **[HIGH · OBSERVED]**

### 4.2 `sw_queue_num0..5` + `queue_idx_ctrl` @ `0xA00..0xA18`

Each notification source gets a **4-bit SW-queue selector**. Sources are
grouped three classes per engine — **NX-generated implicit** (notifs for NX-
executed instrs), **explicit** (instruction-emitted), and **engine-generated
implicit** (notifs for engine instrs): **[HIGH · OBSERVED]**

| Abs | Register | Fields (`[bits]`) |
|---|---|---|
| `0xA00` | `sw_queue_num0` | `pe_nx_NT_[3:0]`, `pe_explicit_NT_[7:4]`, `pe_engine_NT_[11:8]`, `pool_nx_NT_[19:16]`, `pool_explicit_NT_[23:20]`, `pool_engine_NT_[27:24]` |
| `0xA04` | `sw_queue_num1` | `act_nx_NT_[3:0]`, `act_explicit_NT_[7:4]`, `act_engine_NT_[11:8]`, `dve_nx_NT_[19:16]`, `dve_explicit_NT_[23:20]`, `dve_engine_NT_[27:24]` |
| `0xA08` | `sw_queue_num2` | `sp_nx_NT_[3:0]`, `sp_explicit_NT_[7:4]`, `events_semaphores_NT_[19:16]` |
| `0xA0C` | `sw_queue_num3` | `errors_NT_[3:0]` |
| `0xA10` | `sw_queue_num4` | `ham_periodic[3:0]`, `ham_throttle[7:4]`, `ham_periodic_en[16]`, `ham_throttle_en[20]` |
| `0xA14` | `sw_queue_num5` | `Q7_0[3:0]` … `Q7_7[31:28]` (all **eight** Q7 cores) |
| `0xA18` | `queue_idx_ctrl` | `pe_instr_queue_idx[0]`, `pool_…[1]`, `act_…[2]`, `dve_…[3]`, `sp_instr_queue_idx[4]` |

Semantics: writing a 4-bit value selects the destination SW notification queue
for that source class. `queue_idx_ctrl.<engine>_instr_queue_idx` is a 1-bit
override — when set, the **queue index** for that engine's notifications is
taken from the **instruction** instead of the static register. The destination
queues themselves (depth, doorbell, head/tail) are modelled in
[`notific-queue.md`](notific-queue.md). **[HIGH · OBSERVED]**

> **NOTE.** SP is routed here (`sw_queue_num2[7:0]` + `queue_idx_ctrl[4]`)
> even though `tpb.json` has **no `sp_sequencer` config bundle** — SP's datapath
> config lives in another register file; only its NX-SPC snapshot
> (`misc.sp_nx_spc_*`) and notif routing appear in `tpb`. And `sw_queue_num5`
> covers **all eight** Q7 cores, whereas the `tpb_xt_local_reg` notif surface
> maps only `Q7_0..Q7_3` — they are distinct cluster-level vs local-level
> surfaces. **[HIGH · OBSERVED]**

### 4.3 Programming an engine's event route — pseudocode

```c
/* tpb 4 KiB APB aperture base (engine-cluster, NC-v3). The SoC-absolute
 * cluster pseudo-base is 0x2800000000; tpb is the 0x1000 control window. */
#define TPB              ((volatile uint32_t *)TPB_BASE)
#define TPB_R(off)       (TPB[(off) >> 2])

/* events_semaphores @ 0x800 */
#define NOTIFIC_CTRL          0x808u   /* notifications_en[0]                 */
/* notific @ 0xA00 */
#define SW_QUEUE_NUM1         0xA04u   /* act_{nx,explicit,engine}, dve_{...} */
#define QUEUE_IDX_CTRL        0xA18u   /* <engine>_instr_queue_idx[0..4]      */

/* Route the ACT engine's three notification classes to one SW queue and arm
 * the cluster.  ACT lives in sw_queue_num1: nx[3:0], explicit[7:4],
 * engine[11:8].  queue_idx_ctrl bit 2 = act_instr_queue_idx. */
static void route_act_notifs(unsigned sw_queue /* 0..15 */,
                             bool      idx_from_instr)
{
    uint32_t q = TPB_R(SW_QUEUE_NUM1);
    q &= ~0x00000FFFu;                              /* clear ACT nx/expl/eng */
    q |=  (sw_queue & 0xF) << 0;                    /* act_nx_NT_      [3:0]  */
    q |=  (sw_queue & 0xF) << 4;                    /* act_explicit_NT_[7:4]  */
    q |=  (sw_queue & 0xF) << 8;                    /* act_engine_NT_  [11:8] */
    TPB_R(SW_QUEUE_NUM1) = q;

    uint32_t idx = TPB_R(QUEUE_IDX_CTRL);
    if (idx_from_instr) idx |=  (1u << 2);          /* act_instr_queue_idx[2] */
    else                idx &= ~(1u << 2);
    TPB_R(QUEUE_IDX_CTRL) = idx;

    TPB_R(NOTIFIC_CTRL) |= 1u;                       /* notifications_en[0]   */
}
```

### 4.4 Programming a stochastic-rounding seed — pseudocode

```c
/* pool/act/dve stochastic_rnd is WRITE-ONLY: seed[20:0], dtype[31:28].
 * One 32-bit write both seeds the PRNG and selects the target dtype slot.
 * Keep a shadow copy — reads return undefined. */
#define ACT_STOCHASTIC_RND       0x20Cu   /* WO: seed[20:0] | dtype[31:28]    */
#define ACT_STOCHASTIC_RND_MODE  0x210u   /* mode[0]: 0=RNE, 1=stochastic     */

static uint32_t g_act_stoch_shadow;       /* WO register needs a shadow       */

static void act_program_stochastic(uint32_t seed21 /* 21-bit */,
                                    uint32_t dtype4 /* 4-bit  */)
{
    uint32_t v = (seed21 & 0x001FFFFFu)            /* seed[20:0]   */
               | ((dtype4 & 0xFu) << 28);          /* dtype[31:28] */
    g_act_stoch_shadow = v;                         /* shadow the WO write */
    TPB_R(ACT_STOCHASTIC_RND) = v;
    TPB_R(ACT_STOCHASTIC_RND_MODE) = 1u;            /* enable stochastic round */
}
```

---

## 5. `misc` bundle @ `0xB00` — status, PC, error inject, arbitration

The `misc` bundle is the cluster observability + arbitration surface.
**[HIGH · OBSERVED]**

| Abs | Register | Field(s) | Acc | Reset | Purpose |
|---|---|---|---|---|---|
| `0xB00` | `fake_error_ctrl` | `en[0]` | RW (**PulseOnW**) | `0x0` | SW interrupt/error inject; one-cycle pulse on write |
| `0xB04` | `fake_error_data` | `data_NT_[31:0]` | RW | `0x0` | metadata for the fake-error notification |
| `0xB08`–`0xB2C` | `{pe,pool,act,dve,sp}_nx_spc_{lsb,msb}` | `spc[31:0]` | RO | `0x0` | per-engine NX 64-bit SPC snapshot (lsb/msb halves) — **5 engines incl. SP** |
| `0xB30`–`0xB3C` | `{pe,pool,act,dve}_status` | `status[9:0]` | RO | `0x1` | per-engine status (reset `0x1` = ready) — **4 engines, no SP** |
| `0xB40` | `chicken_ctrl` | `pool_dve_arb_en[0]`, `pool_dve_arb_single_lane[1]` | RW | `0x1`, `0x0` | the **sole** top-level arbitration register |
| `0xB44`–`0xB50` | `{pe,pool,act,dve}_eng_pc` | `pc[23:0]` | RO | `0x0` | per-engine 24-bit PC — **4 engines, no SP** |

`chicken_ctrl` is the only co-scheduling control in the file:
`pool_dve_arb_en[0]` (reset **enabled** `0x1`) gates whether **POOL and DVE
execute in parallel**, and `pool_dve_arb_single_lane[1]` controls `instr_id`
usage when they do. **[HIGH · OBSERVED]**

> **NOTE (polarity verbatim).** The schema description for
> `pool_dve_arb_single_lane` reads *"... (0 - enable)"*, the inverse of what the
> name implies. Recorded verbatim; do **not** assume `1`=single-lane. The
> NX-SPC bank covers five engines (incl. SP) but `status` and `eng_pc` cover
> only the four sequencer engines. **[HIGH · OBSERVED for text; MED · INFERRED
> for the operational polarity]**

---

## 6. `performance_counter` @ `0xC00` and `intc_bypass` @ `0xE00`

**Perf counters** exist for **POOL / ACT / DVE only** — PE has *no* perf group.
Each engine has a 0x60-byte block: one `*_perf_cntr_ctrl` (RW; `en[0]` +
`reset[4]`, the latter **PulseOnW**) at the block base, then **ten 64-bit
counters** as `cntr_N_lsb`/`cntr_N_msb` RO word pairs starting at `base+0x10`.
That is `3 × (1 ctrl + 10×2 data) = 63` registers and `3×2 + 60×1 = 66`
fields. Block bases: POOL `0xC00`, ACT `0xC60`, DVE `0xCC0`. **[HIGH · OBSERVED]**

**`intc_bypass`** @ `0xE00` is eight 32-bit RW words (`intc_bypass_0..7`, field
`bypass[31:0]`, reset `0x0`) = **256 interrupt-controller trigger bypass bits**;
a set bit bypasses that trigger. The per-word `[hi:lo]` trigger-index mapping
(word *k* → triggers `[32k+31 : 32k]`) is the natural linear ordering — the JSON
labels every word only *"bypass config for intc triggers"*. **[HIGH · OBSERVED
for bits/access; MED · INFERRED for the per-word index mapping]**

---

## 7. What is NOT in this block

`tpb` does **not** expose PSUM addressing or state-buffer (SB) partition
control — a `grep` over `tpb.json` finds no `psum_*` / `sbuf_*` / `state_buffer`
register. The closest datapath influence on values written toward PSUM is the
ACT special-value compare bank (§3.3) and the rounding/stochastic config
(§3.2–3.4); actual PSUM/SB control is owned by a separate register file. It
also contains **no** per-engine clock/reset/run-stall — that is
[`tpb_xt_local_reg`](tpb-xt-local-reg.md)'s job (§1). **[HIGH · OBSERVED]**

---

## 8. Per-generation applicability

`tpb.json` is **gen-specific**, and the shape changes across the NC family —
re-derived from the per-arch schema trees: **[HIGH · OBSERVED for NC-v2/v3/v4;
INFERRED for v5]**

| Gen | Arch codename | `tpb.json` shape | Source |
|---|---|---|---|
| NC-v2 | `sunda` | 8 bundles / 136 regs | `arch-headers/sunda/csrs/tpb/tpb.json` |
| **NC-v3** | **Cayman** | **9 bundles / 146 regs / 185 fields** | `cayman-arch-regs_tgz/csrs/tpb/tpb.json` *(this page)* |
| NC-v4 | `mariana` | 12 bundles / 168 regs | `arch-headers/mariana/csrs/tpb/tpb.json` |

> **WALL.** Everything on this page is **byte-grounded to the Cayman / NC-v3
> `tpb.json`** unless tagged otherwise. The growth `sunda(8)→cayman(9)→
> mariana(12)` bundle-arrays confirms the cluster surface is re-spun per gen;
> the bias/stochastic/NaN-Inf/notific *template* is the stable core, but exact
> offsets, the `intc_bypass` width, and perf-counter membership must be
> re-read per gen. **The NC-v5 (`maverick`) `tpb` layout is INFERRED** to keep
> the same template (datapath config + cluster notification routing) but is not
> byte-verified here — no NC-v5 `tpb.json` was read. **[MED · INFERRED]**

---

## See also

- [`tpb_xt_local_reg`](tpb-xt-local-reg.md) — the per-engine LOCAL_REG complement (run-stall / start / translation windows).
- [`notific-queue.md`](notific-queue.md) — the SW notification queues `sw_queue_num*` routes into.
- [`evt-sem-regions.md`](../address/evt-sem-regions.md) — the EVT_SEM event/semaphore register windows the `events_semaphores` bundle thresholds drive.
- [`rng-seed-state-ops.md`](../../firmware/kernels/rng-seed-state-ops.md) — firmware save/restore of the datapath RNG / stochastic-rounding seed state.
- [`rng-lfsr-dispatch.md`](../../firmware/kernels/rng-lfsr-dispatch.md) — the LFSR generator that `dve_sequencer.lfsr.seed` initialises.
