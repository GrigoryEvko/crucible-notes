# [P13.6] TPB Event/Semaphore Regions (EVT_SEM)

The `EVT_SEM` unit is the on-chip hardware synchronization block every Cayman
tensor block and standalone sync-processor exposes: **256 single-bit events** plus
**256 32-bit counted semaphores**, projected into the SoC address space as a 1 MiB
aperture. It is the array the collective barriers, cross-engine waits, DMA-completion
syncs and the device→host notifications all operate on. Reads are atomic snapshots;
inc/dec writes are hardware read-modify-write, so multiple posting engines never race.

This page documents the **data plane** (the `tpb_events_semaphores_axi` register file
that backs the EVT_SEM aperture) and reconciles it with the **control plane** (the
`events_semaphores` bundle in `tpb.json`) and the NCFW counted-semaphore barrier model.
Every base/size/offset/count below is re-verified byte-exact against the shipped
RTL-generated address map and the shipped CSR schema this session.

> **Generation scope.** Everything here is Cayman / NC-v3 byte-grounded. §9 shows the
> unit is byte-identical across `cayman`, `mariana`, and `mariana_plus`; no v5 artifact
> was consulted, so any v5 extrapolation would be INFERRED. **[NOTE]**

Primary artifacts (all binary-derived, citeable):

| artifact | role |
| --- | --- |
| `output/address_map/address_map_flat.yaml` | the EVT_SEM decode apertures (bases/sizes) |
| `output/address_map/address_map.vh` | RTL `` `define `` cross-check (58-bit literals) |
| `csrs/tpb/tpb_events_semaphores_axi.json` | **the EVT_SEM CSR schema** (the register model) |
| `csrs/tpb/tpb.json` → `events_semaphores` | the control-plane threshold/enable bundle |

(All under the gitignored `extracted/nested/cayman-arch-regs_tgz/`.)

---

## 1. The EVT_SEM container — byte-exact address-map geometry

From `address_map_flat.yaml` (the file zero-pads bases to 15 hex digits; leading zeros
are cosmetic). The `TPB_0` copy is shown; the layout is identical for all 48 instances
(§3). Cross-checked against the RTL `address_map.vh` `` `define `` macros at L21650+.

| node (`address_map_flat.yaml`) | SoC base | size | off-in-container | window |
| --- | --- | --- | --- | --- |
| `TPB_0_EVT_SEM` | `0x2802700000` | `0x100000` (1 MiB) | — | *(container)* |
| `TPB_0_EVT_SEM_EVENT` | `0x2802700000` | `0x400` (1 KiB) | `0x0000` | events |
| `TPB_0_EVT_SEM_EVENT_RESERVED0` | `0x2802700400` | `0xC00` (3 KiB) | `0x0400` | pad |
| `TPB_0_EVT_SEM_SEMAPHORE_READ` | `0x2802701000` | `0x400` (1 KiB) | `0x1000` | sem read |
| `TPB_0_EVT_SEM_SEMAPHORE_SET` | `0x2802701400` | `0x400` (1 KiB) | `0x1400` | sem set |
| `TPB_0_EVT_SEM_SEMAPHORE_INC` | `0x2802701800` | `0x400` (1 KiB) | `0x1800` | sem inc |
| `TPB_0_EVT_SEM_SEMAPHORE_DEC` | `0x2802701C00` | `0x400` (1 KiB) | `0x1C00` | sem dec |
| `TPB_0_EVT_SEM_EVENT_RESERVED1` | `0x2802702000` | `0xFE000` (1016 KiB) | `0x2000` | pad to 1 MiB |

`[HIGH/OBSERVED — address_map_flat.yaml L903–L910, byte-exact.]`

RTL `` `define `` cross-check (`address_map.vh` L21650–L21669, 58-bit literals,
byte-identical to the YAML):

```text
TPB_0_EVT_SEM_BASE                58'h000002802700000   _SIZE 58'h000000000100000
TPB_0_EVT_SEM_EVENT_BASE          58'h000002802700000   _SIZE 58'h000000000000400
TPB_0_EVT_SEM_SEMAPHORE_READ_BASE 58'h000002802701000   _SIZE 58'h000000000000400
TPB_0_EVT_SEM_SEMAPHORE_SET_BASE  58'h000002802701400   _SIZE 58'h000000000000400
TPB_0_EVT_SEM_SEMAPHORE_INC_BASE  58'h000002802701800   _SIZE 58'h000000000000400
TPB_0_EVT_SEM_SEMAPHORE_DEC_BASE  58'h000002802701C00   _SIZE 58'h000000000000400
```

**Contiguity** (Python verifier this session): the 7 leaves tile
`[0x2802700000, 0x2802800000)` with zero gaps and zero overlaps; the sum of leaf sizes
`0x400 + 0xC00 + 0x400 + 0x400 + 0x400 + 0x400 + 0xFE000 = 0x100000` exactly.
`[HIGH/OBSERVED — arithmetic.]`

**Placement.** The EVT_SEM container sits immediately *before* the TPB's embedded
Sync-Processor block (`TPB_0_SP` @ `0x2802800000`). EVT_SEM and SP are the only two
nested-container nodes among the TPB's children — i.e. the per-TPB EVT_SEM is the
hardware sync unit the TPB's embedded SP (and the PE/ACT/POOL/DVE sequencers) post and
poll. See [`soc-master-map.md`](soc-master-map.md) for the enclosing TPB region table.
`[HIGH/OBSERVED location.]`

> **GOTCHA — no `json:` binding on the address-map leaves.** None of the EVT_SEM
> address-map rows carry a `json:` field; they are pure decode apertures. The
> register-level schema lives in a *separate* file, `tpb_events_semaphores_axi.json`
> (§2), keyed to the aperture by its `SizeInBytes 0x100000` / `AddrWidth 20`
> (= exactly the 1 MiB window). Do not look for the bitfields in the YAML.

---

## 2. The EVT_SEM CSR schema — the 256-semaphore register model

From `csrs/tpb/tpb_events_semaphores_axi.json`. Register-file facts (re-decoded with
`jq` this session):

| field (`RegFile.*`) | value |
| --- | --- |
| `UnitName` | `tpb_events_semaphores_axi` |
| `DataWidth` | `32` bits |
| `AddrWidth` | `20` bits (`2^20 = 0x100000 = 1 MiB`, == the EVT_SEM aperture) |
| `SizeInBytes` | `0x100000` (byte-identical to the address-map container) |
| `InterfaceType` | `APB` (control-plane access path) |
| `RegfileFlavor` | `POSEDGE` |
| `RegistersBundleArrays` | **5** |

The five bundle arrays (each `ArraySize=256`, `BundleSizeInBytes=0x4`):

| bundle (`RegistersBundleArrays[*]`) | `AddressOffset` | `ArraySize` | stride | field `AccessType` | field `Description` |
| --- | --- | --- | --- | --- | --- |
| `tpb_events` | `0x0000` | 256 | 4 B | `RW` (bit `[0]`) | "event clear - 0 , event set - 1" |
| `tpb_semaphores_read` | `0x1000` | 256 | 4 B | `RO` (`[31:0]`) | "Semaphore value" |
| `tpb_semaphores_set` | `0x1400` | 256 | 4 B | `WO` (`[31:0]`) | "Semaphore value" |
| `tpb_semaphores_inc` | `0x1800` | 256 | 4 B | `WO` (`[31:0]`) | "value to increment semaphore by" |
| `tpb_semaphores_dec` | `0x1C00` | 256 | 4 B | `WO` (`[31:0]`) | "value to decrement semaphore by" |

`[ALL HIGH/OBSERVED — read straight from the JSON this session: each bundle's`
`ArraySize=256, BundleSizeInBytes=0x4, the AddressOffsets, the BitFields[0].Position/`
`AccessType/Description.]`

> **CONFIRMATION — the four windows are now OBSERVED, not CARRIED.** The window offsets
> **read @ `+0x1000` / set @ `+0x1400` / inc @ `+0x1800` / dec @ `+0x1C00`** entered this
> wiki as a CARRIED fact from the collective/sync analysis. This page re-confirms them
> *directly* against `tpb_events_semaphores_axi.json`
> (`RegistersBundleArrays[1..4].AddressOffset` = `0x1000`/`0x1400`/`0x1800`/`0x1C00`).
> The schema **confirms** the carried offsets verbatim — they are hereby promoted to
> **[HIGH/OBSERVED]**.

### 2.1 The 256-semaphore array model

The geometry is **OBSERVED, not inferred**:

- **256 hardware events** — `tpb_events` `ArraySize=256`, one useful bit (`value[0]`,
  "event clear - 0, event set - 1") on a 4-byte stride.
- **256 hardware semaphores** — 32-bit counters (`DataWidth=32`, field `[31:0]`), each
  reachable through 4 op windows.
- Per-window byte span `256 × 4 = 0x400` = exactly the 1 KiB the address map assigns to
  each of `EVENT` / `SEMAPHORE_{READ,SET,INC,DEC}`. The CSR `ArraySize × stride` fills
  the address-map leaf size exactly. `[HIGH/OBSERVED.]`

The 256 count is independently corroborated by **three 8-bit ISA fields** (`2^8 = 256`):
the notification record's `event_semaphore_id:8` (see
[`device-host-notification.md`](../interrupt/device-host-notification.md)), and the
`TRIGGER_COLLECTIVE` events block `wait_idx:8` / `update_idx:8` (§4). All three index a
`0..255` space — exactly the 256 EVT_SEM array entries.
`[HIGH/OBSERVED each; the three-way tie is INFERRED-STRONG but mutually consistent.]`

> **CORRECTION check vs SX-ADDR-08.** The backing report asserts the 256-count from the
> schema. Re-decode this session agrees: all 5 bundles `ArraySize=256`. **No correction**
> — 256 is confirmed.

### 2.2 The four-window operation model ("same array, op-per-address")

The four semaphore windows are **four address aliases of the same 256 physical
counters**; the address you touch selects the *operation* on counter `N`:

| operation | window base offset | access | semantics |
| --- | --- | --- | --- |
| **read** `sem[N]` | `+0x1000 + 4·N` | `RO` | returns the current 32-bit count |
| **set** `sem[N] := V` | `+0x1400 + 4·N` | `WO` | overwrites the count with `V` |
| **inc** `sem[N] += D` | `+0x1800 + 4·N` | `WO` | hardware atomic add of `D` |
| **dec** `sem[N] -= D` | `+0x1C00 + 4·N` | `WO` | hardware atomic subtract of `D` |
| **event** `evt[N]` | `+0x0000 + 4·N` | `RW` (bit 0) | write 1=set / 0=clear; read=state |

`[window/access OBSERVED HIGH; the "same physical array" reading is INFERRED-STRONG from`
`the identical 256×4 geometry + the op-named windows + the inc/dec being WO-by-delta.]`

The inc/dec windows are **write-only by delta** (the field description is "value to
increment/decrement semaphore by", not an absolute value), so the read-modify-write is
performed in hardware. Two engines incrementing the same semaphore from different
sequencers therefore cannot lose an update — the post is a single atomic delta-write,
not a host-side read-add-write. `[inc/dec=WO-by-delta OBSERVED HIGH; HW-atomic RMW`
`INFERRED-STRONG.]`

> **QUIRK — the read window is "untestable at reset".** `tpb_semaphores_read` carries the
> tag `TESTSPEC_DO_NOT_CHECK_READ_VALUE` (`RegistersBundleArrays[1].Registers[0].Tag[0]`).
> The counter is live state, so production-test cannot assert a fixed read-back value.
> A re-implementer must treat the read window as volatile: never cache `sem[N]`; always
> re-read inside a poll loop.

---

## 3. The three EVT_SEM families — full census + bases

48 EVT_SEM containers ship (grep-verified `EVT_SEM,` size-`0x100000` rows = **48**, split
8/20/20 by name):

| family | count | what it is |
| --- | --- | --- |
| `TPB_n_EVT_SEM` | 8 | **embedded**, one per tensor block `TPB_0..7` |
| `TOP_SP_n_TPB_EVT_SEM` | 20 | **standalone**, one per Top-Sync-Processor `TOP_SP_0..19` |
| `PEB_SP_n_TPB_EVT_SEM` | 20 | the **PEB-route alias** of the 20 TOP_SP (bit 53) |

Each of the 48 carries the identical 7-leaf internal layout of §1/§2.
`[HIGH/OBSERVED — census + per-row layout.]`

**TPB_n_EVT_SEM bases** (the per-tensor-block sync unit):

| TPB | EVT_SEM base | die |
| --- | --- | --- |
| 0 | `0x2802700000` | 0 |
| 1 | `0x3802700000` | 0 |
| 2 | `0x6802700000` | 0 |
| 3 | `0x7802700000` | 0 |
| 4 | `0x802802700000` | 1 (= TPB_0 \| bit47) |
| 5 | `0x803802700000` | 1 (= TPB_1 \| bit47) |
| 6 | `0x806802700000` | 1 (= TPB_2 \| bit47) |
| 7 | `0x807802700000` | 1 (= TPB_3 \| bit47) |

Die mapping (Python-verified this session): `TPB_{4..7}_EVT_SEM = TPB_{0..3}_EVT_SEM XOR
0x800000000000` exactly — the `DIE[47]` bit. TPBs 0..3 on die0, 4..7 on die1.
`[HIGH/OBSERVED.]`

**TOP_SP_n container** — each is a 4 MiB container of four 1-MiB sub-blocks (TOP_SP_0;
contiguity verified, sum `0x400000`):

```text
TOP_SP_0_TPB_EVT_SEM   0x8280000000  0x100000   (the EVT_SEM unit, §1/§2 layout)
TOP_SP_0_RAM           0x8280100000  0x100000   (scratch RAM)
TOP_SP_0_TPB_SP        0x8280200000  0x100000   (the SP Xtensa-NX core + LOCAL_REG)
TOP_SP_0_RESERVED      0x8280300000  0x100000
```

A TOP_SP is a self-contained sync processor: its own EVT_SEM array + RAM + an Xtensa-NX
core (the same SP IP embedded in each TPB) + a `LOCAL_REG` block. The 20 standalone
TOP_SPs are the cross-engine / collective glue processors (TOP_SP = NEURON_ENGINE #5 in
the lowered collective loop; see
[`spad-ccop-tsync.md`](../../collectives/ncfw/spad-ccop-tsync.md)). Within a die the
TOP_SP slot stride is `0x40000000` (1 GiB); `TOP_SP_{10..19} = TOP_SP_{0..9} XOR bit47`
(die1). `[HIGH/OBSERVED.]`

**PEB_SP_n** — `PEB_SP_{0..19}_TPB_EVT_SEM = TOP_SP_{0..19}_TPB_EVT_SEM XOR
0x20000000000000` exactly (bit 53). Per the SoC neighbor/staged decoder, bit 53 = the
PEB (PCIe-Engine-Block / staged-neighbor) route, so PEB_SP_n is the same 20 sync
processors reached through the PEB BAR path rather than the direct TOP path — the address
the host/PCIe side uses to poke a TOP_SP's semaphores. Example:
`TOP_SP_0 0x8280000000 → PEB_SP_0 0x20008280000000`. `[bases OBSERVED HIGH; bit53=PEB-route`
`MED — the bit is documented in the neighbor decoder and the alias is clean in the map,`
`but no node-type field confirms "same SRAM".]`

---

## 4. The semaphore operation primitives → EVT_SEM windows

The firmware/host sync primitives map one-to-one onto the §2 op windows. EVT_SEM is TPB
opcode `0x10A0` (decimal 4256), sub-opcoded per operation:

| primitive | op / subop | → EVT_SEM window |
| --- | --- | --- |
| `add_semaphore_inc` | `0x10A0` / 21 | **write** `tpb_semaphores_inc[i] = delta` (atomic `sem[i] += delta`) |
| `add_semaphore_wait_ge_and_dec` | `0x10A0` / 20 | **poll** `tpb_semaphores_read[i]` until `>= target`, then **write** `tpb_semaphores_dec[i] = amount` |
| *(set)* | `0x10A0` | **write** `tpb_semaphores_set[i] = value` |
| *(event set/clear)* | — | **write** `tpb_events[i] = 1 / 0` |

`[op/subop OBSERVED HIGH (P-3-65, NCFW-08); the window-binding (subop21→+0x1800,`
`subop20→read +0x1000 + dec +0x1C00) is INFERRED-STRONG — the op NAMES match the WINDOW`
`names + field descriptions exactly, but the firmware does not print the literal +offset`
`in any shipped header.]`

The `add_semaphore_inc` primitive is also reachable from the host as the
`ndl_nc_semaphore_increment` IOCTL (`NEURON_IOCTL_SEMAPHORE_INC`, `0x80084E29`).

**`TRIGGER_COLLECTIVE` inline events block** (8 bytes, compile-verified) — every
collective instruction carries an inline EVT_SEM wait+update:

```text
wait_mode (1B) | wait_idx (1B) | update_mode (1B) | update_idx (1B) | value (4B)
```

Before executing: WAIT on `semaphore[wait_idx]` per `wait_mode`. After: UPDATE
`semaphore[update_idx]` per `update_mode` by `value`. `wait_idx` / `update_idx` are `u8`
= the `0..255` EVT_SEM index. The collective barrier pseudo-ops
(`0xD8 PSEUDO_CORE_BARRIER`, `0xD5 PSEUDO_SYNC_BARRIER`, `0xC3 PSEUDO_DMABARRIER`) lower
to concrete `inc` / `wait_ge_and_dec` sequences parameterised by the NCFW barrier struct
(§5). See [`core-barrier.md`](../../collectives/ops/core-barrier.md) and
[`sync-barrier.md`](../../collectives/ops/sync-barrier.md).
`[events-block layout OBSERVED HIGH; idx→EVT_SEM[i] binding INFERRED-STRONG; pseudo-op`
`opcodes OBSERVED HIGH.]`

### 4.1 C pseudocode — the four operations + the barrier wait loop

```c
/* EVT_SEM data plane, one TPB/TOP_SP instance.
 *
 * Bases & window offsets are byte-exact from:
 *   address_map_flat.yaml          : TPB_0_EVT_SEM_SEMAPHORE_{READ,SET,INC,DEC}
 *   tpb_events_semaphores_axi.json : RegistersBundleArrays[*].AddressOffset
 *
 * The four semaphore windows alias the SAME 256 physical counters; the
 * address selects the operation.  inc/dec are hardware-atomic read-modify-write.
 */
#define EVTSEM_EVENT_OFF   0x0000u   /* tpb_events          arr=256 stride=4 RW bit0   */
#define EVTSEM_SEMREAD_OFF 0x1000u   /* tpb_semaphores_read arr=256 stride=4 RO [31:0] */
#define EVTSEM_SEMSET_OFF  0x1400u   /* tpb_semaphores_set  arr=256 stride=4 WO [31:0] */
#define EVTSEM_SEMINC_OFF  0x1800u   /* tpb_semaphores_inc  arr=256 stride=4 WO [31:0] */
#define EVTSEM_SEMDEC_OFF  0x1C00u   /* tpb_semaphores_dec  arr=256 stride=4 WO [31:0] */
#define EVTSEM_NSEM        256u      /* ArraySize for every bundle                     */
#define EVTSEM_STRIDE      4u        /* BundleSizeInBytes                              */

/* `base` is the SoC base of one EVT_SEM container, e.g. TPB_0 = 0x2802700000.
 * For a cross-die target, OR in DIE[47]; for a remote chip, set CAYMAN_ID[53:48]
 * + CAYMAN_ID_VALID[54] (see §8). */
static inline volatile uint32_t *evtsem_win(uint64_t base, uint32_t win_off, uint32_t i)
{
    /* i must be < EVTSEM_NSEM (256); out-of-range aliases into RESERVED pad. */
    return (volatile uint32_t *)(uintptr_t)(base + win_off + (uint64_t)i * EVTSEM_STRIDE);
}

/* read window: returns sem[i]'s current 32-bit count (RO). */
static inline uint32_t sem_read(uint64_t base, uint32_t i)
{
    return *evtsem_win(base, EVTSEM_SEMREAD_OFF, i);     /* +0x1000 + 4*i */
}

/* set window: sem[i] := value (WO overwrite). */
static inline void sem_set(uint64_t base, uint32_t i, uint32_t value)
{
    *evtsem_win(base, EVTSEM_SEMSET_OFF, i) = value;     /* +0x1400 + 4*i */
}

/* inc window: sem[i] += delta, hardware-atomic (WO by delta). */
static inline void sem_inc(uint64_t base, uint32_t i, uint32_t delta)
{
    *evtsem_win(base, EVTSEM_SEMINC_OFF, i) = delta;     /* +0x1800 + 4*i */
}

/* dec window: sem[i] -= delta, hardware-atomic (WO by delta). */
static inline void sem_dec(uint64_t base, uint32_t i, uint32_t delta)
{
    *evtsem_win(base, EVTSEM_SEMDEC_OFF, i) = delta;     /* +0x1C00 + 4*i */
}

/* event window: evt[i] := state (bit0), RW. */
static inline void evt_set(uint64_t base, uint32_t i, uint32_t state)
{
    *evtsem_win(base, EVTSEM_EVENT_OFF, i) = (state & 1u);  /* +0x0000 + 4*i */
}

/* The barrier primitive: add_semaphore_wait_ge_and_dec (op 0x10A0 / subop 20).
 * Poll the read window until the counter reaches `target`, then atomically
 * subtract `amount` via the dec window.  This is the consumer side of a
 * counted-semaphore barrier: each arriving peer does sem_inc(...,1) into this
 * semaphore; the waiter releases once `target` peers have arrived. */
static void sem_wait_ge_and_dec(uint64_t base, uint32_t i,
                                uint32_t target, uint32_t amount)
{
    while (sem_read(base, i) < target)   /* RO poll; counter is volatile (§2.1) */
        cpu_relax();                     /* core spin hint                       */
    sem_dec(base, i, amount);            /* GE reached -> consume `amount`       */
}
```

The `cpu_relax()` is a placeholder for the core's spin hint; the loop is a pure
read-poll on a `RO` window, so it never mutates state until the GE-compare passes.

---

## 5. Reconciling the NCFW barrier semaphores into EVT_SEM

The NCFW device/host barrier structs (decoded from `libncfw.so`) carry their semaphores
as 64-bit `soc_addr` pointers. Those pointers are **runtime soc_addr values into these
EVT_SEM windows**:

| NCFW barrier field | count | role | → EVT_SEM window |
| --- | --- | --- | --- |
| host `barrier_start` | 1 | host→device release | a `SEMAPHORE_SET`/`INC[i]` |
| host `barrier_done` | 1 | device→host completion | `SEMAPHORE_READ[i]` (host polls) |
| `barrier_sema[k][0..3]` | 4 / step | per-step cross-engine fan-in | remote `SEMAPHORE_INC[i]` (arrive) + local `SEMAPHORE_READ[i]` (wait) |
| `target_sema_val[k][0..3]` | 4 / step | the GE-compare targets | the `>=` threshold for `tpb_semaphores_read[i]` |
| `dma_sync_sema[0..3]` | 4 | DMA-completion sync | `SEMAPHORE_READ[i]` poll |
| `start_network_proxy` | 1 | inter-node leg kick | a `SEMAPHORE_INC[i]` |

`[struct layout/counts OBSERVED HIGH in NCFW-08; the "→ window" binding INFERRED-STRONG`
`— see the caveat below.]`

> **GOTCHA — INFERRED-STRONG, not byte-equated (the honest caveat).** The NCFW barrier
> `soc_addr` integers live in the firmware's DRAM image and are **runtime-populated** —
> they are *not* in `libncfw.so`'s static bytes and *not* in the address-map YAML. So a
> literal `0x2802701800`-class value cannot be grepped out of a shipped file to prove
> `barrier_sema[0][0] == TPB_3_EVT_SEM_SEMAPHORE_INC + 4·i`. What *is* byte-exact and
> closes the loop:
> 1. the barrier ops are `add_semaphore_inc` / `add_semaphore_wait_ge_and_dec`
>    (= EVT_SEM op `0x10A0` subop 21/20, OBSERVED);
> 2. those subops write/read exactly the `SEMAPHORE_INC`/`READ`/`DEC` windows whose field
>    descriptions are "value to increment/decrement semaphore by" / "Used for reading
>    Semaphores" (§2, OBSERVED);
> 3. `target_sema_val` is a `u32` GE-compare against a semaphore whose value field is
>    `u32 [31:0]` (§2, OBSERVED) — widths match;
> 4. a per-step 4×4 fan-in (4 semaphores × 4 targets) fits trivially in the 256-array.
>
> Therefore the NCFW barrier semaphores **are** entries of these EVT_SEM arrays, selected
> at runtime by firmware; the operation model and the 32-bit width are byte-exact — only
> the concrete index `i` per barrier slot is runtime-bound. `[HIGH that they target the`
> `EVT_SEM op model / widths match; MED for which specific instance + index.]`

**Which instance a barrier semaphore lives in.** A cross-engine barrier *within one TPB*
uses that TPB's embedded `TPB_n_EVT_SEM`. A cross-die / cross-rank barrier uses the
`TOP_SP` EVT_SEM of the participating sync processors and addresses a peer die's semaphore
by setting `DIE[47]` (other die of the package) or `CAYMAN_ID[53:48]` + `VALID[54]`
(a remote chip) in the soc_addr — the `dma_apb_bcast` peer-fanout writes all masked peers'
`SEMAPHORE_INC` windows with one APB-broadcast DMA. `[die-select bits OBSERVED HIGH; the`
`barrier USING them = INFERRED MED, runtime-bound.]` See
[`spad-ccop-tsync.md`](../../collectives/ncfw/spad-ccop-tsync.md).

---

## 6. Per-engine vs global semaphore split

EVT_SEM is a **per-TPB / per-SP shared** unit, *not* a per-engine private array:

- The `TPB_n_EVT_SEM` (8) is shared by all engines inside that tensor block — the PE
  array sequencer, ACT, POOL, DVE and the embedded SP all post/poll the **same** 256
  semaphores. There is **one** EVT_SEM per TPB, not one per engine. `[OBSERVED HIGH.]`
- *Who posts* rides the **notification type**, not a separate array. The notification
  record has a distinct `notific_type` per engine for the EVT_SEM event:
  `0x07` PE · `0x0b` ACT · `0x0f` POOL · `0x13` DVE · `0x17` SP · `0x1b` AXI. So the
  256-semaphore array is global within the TPB; the engine identity is in the
  notification, and each engine's evt/sem notif routes to a SW queue via
  `tpb.notific.sw_queue_num2.events_semaphores_NT_`. `[OBSERVED HIGH — six EVT_SEM`
  `notific_types, one shared array.]`
- The 20 `TOP_SP` EVT_SEM units are the cross-engine / cross-die global sync arrays the
  collective glue (TOP_SP engine #5) runs on (its `PollSem` reads them).
- **Per-Q7-core private state is separate** and is *not* in EVT_SEM: each of the 8 Q7
  cores has `run_state_0..7` + `intr_info_0..7` in the SP `LOCAL_REG`
  (`tpb_xt_local_reg.json`), distinct from the shared EVT_SEM array. `[OBSERVED HIGH.]`

`AXI_EVT_SEM` (`0x1b`) is the AXI-master's semaphore-write path into the *same* array —
matching the regfile name `tpb_events_semaphores_AXI`. `[structure OBSERVED HIGH;`
`AXI-master == SDMA/DGE INFERRED MED.]`

---

## 7. Relation to the notification queue (control plane)

A semaphore/event update can surface as a **notification-queue entry** instead of forcing
a busy-poll. The control plane is the `events_semaphores` bundle in `tpb.json` at bundle
offset `0x800` — a *separate* register file from the EVT_SEM data plane (§2), living in
the host-visible `TOP_TPB_TOP_CSR` control aperture, **not** in the EVT_SEM data aperture:

| register (`tpb.json` `events_semaphores`) | abs offset | reset | meaning |
| --- | --- | --- | --- |
| `sem_threshold_ctrl0.low_NT_` | `0x800` | `0x0` | semaphore LOW threshold |
| `sem_threshold_ctrl1.high_NT_` | `0x804` | `0xffffffff` | semaphore HIGH threshold |
| `notific_ctrl.notifications_en` | `0x808` (bit 0) | `0x0` | master enable for evt/sem notifs |

`[OBSERVED HIGH — tpb.json events_semaphores bundle @ bundle-offset 0x800; the three`
`registers at intra-bundle 0x0/0x4/0x8 = abs 0x800/0x804/0x808.]`

When a semaphore value crosses a configured threshold (or an event is set), the NOTIFIC
block builds a 16-byte record with `notific_type` in
`{0x07,0x0b,0x0f,0x13,0x17,0x1b}` and an EVT_SEM body:

```text
event_semaphore_id : 8   /* which of the 256 events/semaphores = the §2 index */
is_semaphore       : 1   /* 1 => semaphore update, 0 => event */
double_set_or_clear: 1
update_mode        : 8
value              : 32  /* new semaphore value / event state */
```

The `event_semaphore_id:8` is the same `0..255` space as §2 — a direct tie. The record
egresses over AXI to the host RAM ring, where the host reads it via the phase-bit fast
path; an NCFW device barrier's completion can therefore surface as a notification rather
than a busy-poll. See [`device-host-notification.md`](../interrupt/device-host-notification.md)
and [`tpb-subblocks.md`](../csr/tpb-subblocks.md) for the control bundle in context.
`[record format + routing OBSERVED HIGH; "barrier completion surfaces here" MED.]`

> **CORRECTION (in-place, vs an earlier NCFW note).** An earlier NCFW write-up placed
> `notific_ctrl` at `xt_local_reg+0x808`. The correct enclosing register file is
> `tpb.json` `events_semaphores` `notific_ctrl @ 0x808` (in the TPB `TOP_CSR` block), not
> `xt_local_reg`. The `0x808` offset is the same; only the enclosing regfile differs.

---

## 8. Cross-die / cross-chip addressing of a semaphore

A semaphore on die N (or chip M) is reached from die/chip P by composing the EVT_SEM SoC
offset with the SoC routing fields — `[54:0]` of the 58-bit decode field (see
[`soc-master-map.md`](soc-master-map.md)):

| bit field | meaning | how a remote sema is addressed |
| --- | --- | --- |
| `LOCAL[46:0]` | intra-die byte address | the EVT_SEM window offset (`…802701800 + 4·i` for `INC[i]`) |
| `DIE[47]` | which die of the 2-die package | set to target the OTHER die (TPB_4..7 / TOP_SP_10..19 are exactly `base \| bit47`) |
| `CAYMAN_ID[53:48]` | chip id in the mesh | set to a peer chip's id for an inter-chip barrier write |
| `CAYMAN_ID_VALID[54]` | route-by-chip-id enable | 1 = target a remote chip; 0 = stay on local chip |
| `PEB[53]` | (neighbor decoder) PEB route | the `PEB_SP_n` alias (`base \| bit53`) is the PEB-routed view of a TOP_SP's EVT_SEM |

Observed exact XORs (Python this session):

```text
TPB_{4..7}_EVT_SEM    = TPB_{0..3}_EVT_SEM    XOR 0x800000000000    (bit47 DIE)
TOP_SP_{10..19}_EVTSEM= TOP_SP_{0..9}_EVTSEM  XOR 0x800000000000    (bit47 DIE)
PEB_SP_{0..19}_EVTSEM = TOP_SP_{0..19}_EVTSEM XOR 0x20000000000000  (bit53 PEB)
```

The die-1 EVT_SEM addresses are *literally* the die-0 addresses with bit 47 set, so a
cross-die barrier-semaphore write is just the local EVT_SEM offset with `DIE[47]` flipped
— the mechanism by which an NCFW `barrier_sema` soc_addr "high bits select the die".
`[bit relationships HIGH/OBSERVED; the barrier USING them INFERRED MED, runtime-bound.]`

---

## 9. Cross-generation: byte-identical (cayman / mariana / mariana_plus)

The EVT_SEM event/semaphore unit is byte-identical across all three shipped
Trainium-class generations (grep-/jq-verified):

- **Address map** — 48 EVT_SEM containers (8 TPB + 20 TOP_SP + 20 PEB_SP) in all three;
  `TPB_0_EVT_SEM = 0x2802700000 / 0x100000` with the identical 7-leaf internal layout;
  die-1 / PEB aliases identical.
- **CSR schema** — `tpb_events_semaphores_axi.json` ships in `cayman` and in the
  `mariana` / `mariana_plus` arch-headers with identical `UnitName`, `AddrWidth 20`,
  `SizeInBytes 0x100000`, and the same 5 bundles (`tpb_events`, `tpb_semaphores_read/
  set/inc/dec`), each `ArraySize=256` stride-4.

The 256-event / 256-semaphore / read-set-inc-dec model is **stable across these three
generations** — no cross-gen EVT_SEM geometry difference. `[HIGH/OBSERVED.]` Any NC-v5
claim would be INFERRED (no v5 artifact consulted). **[NOTE]**

---

## 10. Confidence ledger

**HIGH / OBSERVED** (grepped/decoded byte-exact this session):

- EVT_SEM container 1 MiB @ `TPB_0 0x2802700000`; 7-leaf layout EVENT `0x400` + RSVD0
  `0xC00` + READ/SET/INC/DEC each `0x400` + RSVD1 `0xFE000`; gap-free, sum `0x100000`.
  YAML + `.vh` `` `define `` agree byte-identical.
- CSR schema: 5 bundles, each `ArraySize=256` stride-4 at offsets
  `0x0/0x1000/0x1400/0x1800/0x1C00`; access RW/RO/WO/WO/WO; 256 events + 256 32-bit
  semaphores. **The four window offsets are confirmed by the schema — promoted from
  CARRIED to OBSERVED.**
- 48 EVT_SEM containers: 8 TPB + 20 TOP_SP + 20 PEB_SP; identical layout each.
- `TPB`/`TOP_SP` die1 = die0 `| bit47`; `PEB_SP` = `TOP_SP | bit53` (Python XOR-verified).
- Control plane = `tpb.json` `events_semaphores` {`sem_threshold_ctrl0/1`, `notific_ctrl`
  @ `0x800/4/8`}, a SEPARATE regfile from the EVT_SEM data plane.

**MED:** `PEB_SP_n` == same-SRAM PEB alias (bit53 from the neighbor decoder; no node-type
field); cross-die barrier = local offset with `DIE[47]`/`CAYMAN_ID` set (bits OBSERVED,
barrier-usage runtime); barrier completion surfacing via the NQ; `AXI_EVT_SEM` ==
SDMA/DGE master; inc/dec windows are HW-atomic RMW.

**INFERRED-STRONG:** the NCFW `barrier_sema` / `dma_sync_sema` / `target_sema_val`
soc_addrs are entries of these EVT_SEM arrays (ops `0x10A0/21,/20`, window names, 32-bit
widths all match byte-exact; only the runtime index `i` is firmware-populated);
`wait_idx`/`update_idx`/`event_semaphore_id` (`u8`) → EVT_SEM[i] (three independent 8-bit
ISA fields index the same 256-space).

---

## See also

- [`core-barrier.md`](../../collectives/ops/core-barrier.md) — the collective core barrier
- [`sync-barrier.md`](../../collectives/ops/sync-barrier.md) — the collective sync barrier
- [`spad-ccop-tsync.md`](../../collectives/ncfw/spad-ccop-tsync.md) — NCFW counted-semaphore barrier
- [`device-host-notification.md`](../interrupt/device-host-notification.md) — the notification egress path
- [`tpb-subblocks.md`](../csr/tpb-subblocks.md) — the `events_semaphores` control bundle in context
- [`soc-master-map.md`](soc-master-map.md) — the enclosing SoC region map + 58-bit decode field (`[54:0]` routed)
