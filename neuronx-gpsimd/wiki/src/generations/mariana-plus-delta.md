# MARIANA_PLUS (v4+) Generation Delta

This page answers one question with byte evidence: **what is MARIANA_PLUS
(`arch_id 28` / `coretype 29`) relative to MARIANA (`arch_id 20` / `coretype 21`)?**
The short answer, established byte-for-byte across the five committed
MARIANA_PLUS image pages and re-verified here against the shipped binaries:

> **MARIANA_PLUS is a RECOMPILE + a flag-refresh of MARIANA, not a silicon step
> and not a new architecture.** Every getter name, every dispatch handler, every
> opcode value, every PROF table, the reset/boot geometry, and the EXTISA Q7
> kernel containers are **byte-stable or byte-identical** v4 ↔ v4+. The *only*
> functional addition is **one firmware feature — the DGE reshape fast-path** —
> and it is **gen-wide** (linked into all five NX engine images, including SP,
> which hosts no DGE). The removed compile flag `NRTUCODE_MPLUS_ON_MARIANA`
> became the runtime debug env-var `NEURON_RT_DBG_V4_PLUS=0/1`. Everything else
> is a register-map refresh + relocated literals from a fresh build. `[HIGH/OBSERVED]`

Everything below reads directly from the shipped firmware container
`libnrtucode_internal.so` (sha256 `b7c67e898a116454…632fc329b`), its companion
archive `libnrtucode.a` (sha256 `158dadc5…d7bd6130`), the shipped C ISA / register
header trees under `…/custom_op/c10/include/`, and the IDA v3 sidecars — decoded
with stock `nm`/`objdump`/`readelf` and, for the device-side Xtensa blobs, the
shipped Cadence Vision-Q7 `ncore2gp` `xtensa-elf-objdump` (GNU Binutils
2.34.20200201, Xtensa Tools 14.09, `XTENSA_CORE=ncore2gp`, uarch Cairo). All counts
are grounded with `nm <lib> | rg -c` or `ar t | rg -ci`, never grepped from a
decompile. Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

This is the **generation-level synthesis** of the five committed per-engine v4+
image diffs — read those for the carve mechanics; this page records the *cross-gen
mechanism* and its lineage placement. Related pages:
[MARIANA_PLUS × ACT](../images/mariana-plus-act.md) ·
[× DVE](../images/mariana-plus-dve.md) ·
[× PE](../images/mariana-plus-pe.md) ·
[× POOL](../images/mariana-plus-pool.md) ·
[× SP](../images/mariana-plus-sp.md) ·
[DGE Reshape Engine](../firmware/dge/dge-reshape.md) ·
[Codename Cross-Walk](../reference/codename-crosswalk.md) ·
[Maverick (v5) Profile](./maverick-profile.md) ·
[Confidence & Walls Model](../reference/confidence-model.md).

---

## 1. The identity row — `arch_id 28` / `coretype 29`

The whole identity in one row, every cell anchored to a shipped artifact.
`[HIGH/OBSERVED unless tagged]`

| Field | MARIANA (v4) | MARIANA_PLUS (v4+) | Evidence |
|---|---|---|---|
| codename | `MARIANA` | `MARIANA_PLUS` | `NRTUCODE_CORE_MARIANA_NX_POOL` / `…_MARIANA_PLUS_NX_POOL` (string, container) |
| version tag | NeuronCore-v4 | NeuronCore-v4+ | shares the **v4** NC banner; no `v4_plus` ISA-version enumerator exists (§4) |
| `arch_id` | 20 (`0x14`) | **28 (`0x1c`)** | `get_image` selector ladder immediate (GEN-01, carried OBSERVED) |
| `coretype` | 21 (`0x15`) | **29 (`0x1d`)** | `coretype = arch_id + 1` across all four shipped gens (§1.1) |
| NCFW selector | sel `0x14` | sel `0x1c` | the libncfw `get_image` cmpl ladder tops out at `arch_id 0x1c` |
| silicon | v4 MARIANA die | **none new — same compute architecture** | shares ISA/dtypes/collectives/engines (§3, §4) **[CORRECTION, §6]** |
| shipped vs internal | shipped runtime gen | first-class **internal** firmware-core slot | `mariana_plus_libs @0x9b9010`; not in the 4 shipped ISA dirs |
| EXTISA blob identity | v4 Q7 kernel SOs | **byte-identical to v4** (`cmp -s` clean, §3.3) | POOL `EXTISA_{0,3}_SO` `9f2ce049`/`8477ff26` == MARIANA |

### 1.1 The `coretype = arch_id + 1` relation, from the dispatch

The identity numbers are not asserted from a table comment — they come from the
**coretype-keyed EXTISA selector** in the container. `nrtucode_get_ext_isa_internal`
(`@0x9b2b30`) dispatches the per-generation `*_libs` roster by `lea`-ing one of five
peer tables in lineage order: `[HIGH/OBSERVED — `objdump -d` this session]`

```text
9b2bfd:  lea 0x637c(%rip),%rcx   # SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get
9b2c0a:  lea 0x63bf(%rip),%rcx   # 9b8fd0 <mariana_libs>
9b2c17:  lea 0x6372(%rip),%rcx   # 9b8f90 <cayman_libs>
9b2c24:  lea 0x63e5(%rip),%rcx   # 9b9010 <mariana_plus_libs>    ← the v4+ peer table
9b2c31:  lea 0x6418(%rip),%rcx   # 9b9050 <maverick_libs>
```

`mariana_plus_libs @0x9b9010` sits between `cayman_libs` and `maverick_libs` as a
**peer**, not a sub-variant of `mariana_libs @0x9b8fd0` — confirmed by `nm`. The
companion `nrtucode_get_num_ext_isa_libs` (`@0x9b2c90`) bounds the coretype key
against `cmp $0x25,%edi` (`0x25 = 37 =` MAVERICK coretype, the family max). The
lineage relation, byte-grounded across all four shipped gens, is uniform:

```text
codename      arch_id (sel)   coretype     coretype − arch_id
SUNDA         5  (0x05)       6  (0x06)    +1
CAYMAN        12 (0x0c)       13 (0x0d)    +1
MARIANA       20 (0x14)       21 (0x15)    +1
MARIANA_PLUS  28 (0x1c)       29 (0x1d)    +1     ← THIS gen
MAVERICK      36 (0x24)*      37 (0x25)    +1     (arch_id INFERRED, coretype OBSERVED)
```

So MARIANA_PLUS is exactly **+8 coretype** over MARIANA (a full generation slot,
not a sub-stepping), keyed by `coretype 0x1d` in the EXTISA dispatch and `arch_id
0x1c` in the NCFW image selector. `[HIGH/OBSERVED for SUNDA/CAYMAN/MARIANA/MARIANA_PLUS;
MAVERICK arch_id INFERRED]`

> **GOTCHA — the prompt's `arch_id 29 / coretype 28` is the inverse of the bytes.**
> Two backing reports and the original task framing pair the numbers as
> "arch_id 29 / coretype 28". The shipped evidence is the **transpose**: the
> selector immediate is `arch_id = 0x1c = 28` and the coretype bitmask sets bit
> `29 = 0x1d`, with the uniform `coretype = arch_id + 1` shown above. This page
> carries **`arch_id 28 / coretype 29`**. `[HIGH/OBSERVED]`

---

## 2. The firmware images — MARIANA_PLUS's own NCFW + EXTISA set, no new silicon

MARIANA_PLUS ships a **complete, parallel firmware image set** for the same engines
as MARIANA — its own NCFW NX images and its own EXTISA Q7 kernels — but on the same
compute silicon. The roster parity is exact. `[HIGH/OBSERVED]`

### 2.1 The getter roster — 100 NX/Q7 image accessors per gen, identical shape

`nm libnrtucode_internal.so | rg -c '<GEN>_.*_get$'` returns **200** for both
MARIANA and MARIANA_PLUS — perfect parity. Broken out per engine (the lib is
not stripped; the getters are local `r` symbols, so plain `nm`, not `nm -D`):

| getter group | per engine | engines | subtotal |
|---|---|---|---|
| NX `{ACT, DVE, PE, POOL}` | 14 (12 base + 2 PROF `{CAM,TABLE}`) | 4 | 56 |
| NX `SP` | 12 (base only — **no PROF**) | 1 | 12 |
| Q7 `POOL` | 32 (compute + DKL + EXTISA) | 1 | 32 |
| **NX/Q7 total** | | | **100** |

Both gens carry exactly this 100-accessor shape (`14·4 + 12 + 32 = 100`; the `200`
`nm -c` figure double-counts each getter's `.text` stub + `.data` blob symbol). The
companion archive `libnrtucode.a` ships **124** members per gen — the same number as
CAYMAN — confirmed by `ar t libnrtucode.a | rg -ci`: `[HIGH/OBSERVED]`

```text
SUNDA         48
CAYMAN       124
MARIANA      124
MARIANA_PLUS 124        ← identical member count to MARIANA and CAYMAN
MAVERICK       0        (internal-only; its images are in the .so, not this .a)
shared .c.o   15        (nrtucode.c.o, prelink.c.o, nrtucode_images.c.o, … — gen-neutral)
              ───
total        435        (48 + 124 + 124 + 124 + 0 + 15)
```

> **NOTE — the `ar t | rg -ci mariana` substring trap.** A naïve
> `rg -ci 'mariana'` reports **248** because `MARIANA_PLUS` members contain the
> substring `MARIANA`. The mutually-exclusive count
> (`rg -i mariana | rg -vi mariana_plus | wc -l`) is **124** for MARIANA proper and
> **124** for MARIANA_PLUS. Always exclude the `_plus` superstring. `[HIGH/OBSERVED]`

### 2.2 The EXTISA Q7 compute blobs — byte-identical, NCFW DRAM byte-identical

The Q7 compute side is where "no new silicon" is most literal: the EXTISA kernel
SOs (the `kernel_info_table` containers that the Q7/POOL cores actually execute) are
**byte-for-byte identical** v4 ↔ v4+ — not merely relocated. The POOL page proves it
exhaustively: `EXTISA_0_SO` `9f2ce049` and `EXTISA_3_SO` `8477ff26` (and `1/2_SO` +
all four JSON) `cmp -s` clean against the MARIANA POOL EXTISA SOs, so even the
`funcVA`s match. The 17-entry `kernel_info_table` (`opcode→funcVA` map) is identical
key-for-key. The Q7 RNG body (Xorwow-TIE + LFSR) is unchanged. `[HIGH/OBSERVED —
[mariana-plus-pool §6](../images/mariana-plus-pool.md)]`

So the MARIANA_PLUS firmware image set is: **a recompiled NX SEQ core for every
engine** (the part that grew, §5) wrapped around a **byte-unchanged Q7 compute
core** (the EXTISA kernels) — exactly the profile of a software re-release against
unchanged compute silicon.

---

## 3. The ONE functional delta — the DGE reshape fast-path (gen-wide)

The single substantive functional contribution of v4+ is the **DGE
(Descriptor-Generation Engine) reshape fast-path**. It is not a new dispatch
handler, not a new opcode, not a new dtype — it is new firmware code inside the
existing DGE reshape subsystem. It is marked by exactly four strings, all
**absent on every MARIANA NX engine** (`count 0`) and **present (×1) on every
MARIANA_PLUS NX engine** (`count 1`), correlated with the **retirement** of the
legacy `push REGWRITE to DMA[%d]` path. `[HIGH/OBSERVED]`

| string | role | MARIANA | MARIANA_PLUS |
|---|---|:---:|:---:|
| `dge_decode_fast.cpp` | the fast-path TU `__FILE__` | 0 | 1 |
| `dge_reshape_memcopy_transpose_fast` | the fused `__func__` | 0 | 1 |
| `tensor_reshape_transpose_sb2sb` | a new SBUF→SBUF transpose reshape kind | 0 | 1 |
| `wait_for_credit` | explicit DMA descriptor-ring credit-wait | 0 | 1 |
| `push REGWRITE to DMA[%d]` | the legacy descriptor-emit path | 1 | **0 (retired)** |

Container-wide, each fast-path string appears **10×** (one per NX engine × five
engines, plus TEST/symbol-build copies) — `rg --no-ignore -a -c` this session:
`dge_decode_fast` = 10, `wait_for_credit` = 10, `tensor_reshape_transpose_sb2sb` =
13 (the extra hits are the Q7-side reshape kind). `BEGIN on mariana` = 5,
`BEGIN on mariana_plus` = 5 — exactly one self-name per engine per gen. `[HIGH/OBSERVED]`

### 3.1 Why it is GEN-WIDE, not DGE-engine-selective — the SP proof

> **CORRECTION (carried from [mariana-plus-pool §1](../images/mariana-plus-pool.md)).**
> An early hypothesis held the fast-path was DGE/POOL-selective (or compute-engine
> only). That is **REFUTED**. The fast-path is present on **all five** NX
> sequencers, including **SP** — the lean scalar sync/control core that hosts **no**
> DGE/reshape dispatch handler at all.

SP is the decisive case. Carving `MARIANA_PLUS_NX_SP_DEBUG_DRAM` (`@0x752f80`, size
`0x6660`, sha `2958154e`) and counting this session: all four fast-path strings
`= 1`, `push REGWRITE to DMA` `= 0` — versus `= 0`/`= 1` on MARIANA SP. SP has no
functional reason to carry reshape code; its carrying it proves the fast-path is a
**SEQ-infrastructure recompile payload baked into every NX image**, independent of
each engine's handler subset. POOL remains the architectural *home* (the SW-DGE
backend runs device-side on the Q7/POOL cores, per the shipped
`aws_neuron_isa_tpb_dma_gather_xpose.h` "uses the SW-DGE backend with Q7 processors
in the Gpsimd engine"), but the fast-path **translation unit is linked into every NX
label**. `[HIGH/OBSERVED — strings + absence + dual-build (DEBUG+TEST) presence;
functional reading INFERRED-HIGH]`

### 3.2 The fast-path, as annotated C

Reconstructed from the four `__FILE__`/`__func__` names, the shared DGE
descriptor/format-string corpus ([DGE Reshape §3–§6](../firmware/dge/dge-reshape.md)),
and the structural signature (the NX IRAM **grew** while `entry` prologues **fell** —
POOL DEBUG IRAM `697 → 675`, the fingerprint of inlining several functions into one).
The per-instruction bodies sit in FLIX-desynced IRAM spans and are not byte-recoverable,
so the control-flow *shape* is INFERRED-HIGH; the names and the present/absent counts
are OBSERVED.

```c
/* MARIANA_PLUS NX_* — dge_decode_fast.cpp : dge_reshape_memcopy_transpose_fast()
 * The v4+ functional delta. Linked into EVERY NX image (gen-wide, SP included);
 * the SW-DGE backend that this front-end feeds runs device-side on Q7/POOL.
 * NEW on MARIANA_PLUS; absent on MARIANA, which used the generic
 *   analyze_tensor_reshape -> pair-assess -> per-tile emit -> "push REGWRITE" path. */

bool dge_reshape_memcopy_transpose_fast(const dge_reshape_state *src,  /* {num[4], step[4], elem_size, is_src} */
                                        const dge_reshape_state *dst,
                                        dma_ring_t *ring)
{
    /* GUARD: only the contiguous/tiled SBUF->SBUF transpose case qualifies.
     * Anything else (cast, non-Y gather_dim, HBM round-trip, irregular stride)
     * returns false and the caller runs the SHARED slow path, present on BOTH gens. */
    if (!is_sb2sb_transpose(src, dst))         /* the new tensor_reshape_transpose_sb2sb kind */
        return false;                          /* -> caller: analyze_tensor_reshape()         */

    /* FUSE memcopy + transpose into ONE descriptor-gen pass. The generic path walked
     * analyze -> pair-assess -> per-tile GATHER_XPOSE emit; the fast path inlines the
     * spray (gen_spray_info) + gather-pattern (make_gather_pattern) + emit into one
     * body -- the "IRAM grew but fewer entry prologues" inline-consolidation signature. */
    for (int tile = 0; tile < num_xbar_tiles(src); tile++) {   /* 16x128 xbar tiles, 2B dtype */

        /* Explicit DMA flow control: block until the descriptor ring has CREDIT before
         * pushing more BDs. NEW primitive on v4+; the fused path's higher descriptor-issue
         * rate would otherwise overrun the ring depth. */
        wait_for_credit(ring);                 /* NEW on MARIANA_PLUS */

        /* Build + push the GATHER_XPOSE descriptor for this tile-group directly. The
         * DMA/xbar HARDWARE performs the 16x128-tile transpose during the transfer
         * (descriptor-level transpose -- reuses the existing DMA_GATHER_XPOSE ISA struct,
         * NO new descriptor type). */
        emit_gather_xpose_sb2sb(ring, src, dst, tile);

        /* NO separate "push REGWRITE to DMA[%d]" step -- the DMA trigger/register program
         * the generic emit did via REGWRITE is FOLDED into this fused emit. REGWRITE is
         * RETIRED on MARIANA_PLUS (present on MARIANA). */
    }
    return true;                               /* fast path consumed the request */
}
```

The fast-path adds **no new ISA descriptor** — it reuses the existing
`DMA_GATHER_XPOSE` / `DMA_DIRECT2D` 64-byte structs (the `gather_xpose.h` header is
byte-identical cayman↔mariana save the `NC-v3`/`NC-v4` comment; MARIANA_PLUS uses the
mariana copy). It is a *throughput* optimization, not a capability expansion.
`[HIGH/OBSERVED structs; INFERRED-HIGH fusion semantics]`

---

## 4. Shared ISA / equivalent collectives / register-map refresh

What is **byte-identical** vs **merely refreshed**:

### 4.1 Byte-identical — the ISA, dispatch surface, collectives

* **No `neuron_mariana_plus_arch_isa` dir exists.** `ls …/include` finds exactly
  **four** ISA dirs: `{cayman, mariana, maverick, sunda}`. MARIANA_PLUS **shares**
  `neuron_mariana_arch_isa` (the NC-v4 159-opcode table). There is no `V4_PLUS`
  enumerator in any `NEURON_ISA_TPB_NEURON_CORE_VERSION` enum — the runtime
  distinguishes v4 from v4+ by a **string selector** (`NEURON_RT_DBG_V4_PLUS`, §5),
  not an ISA version point. This is the structural reason MARIANA_PLUS *cannot* be a
  distinct ISA. `[HIGH/OBSERVED — DX-GEN-04 §1.1/§6.1, re-confirmed]`
* **The opcode table is identical, value-for-value.** All 159 MARIANA opcodes recur
  at the identical hex on MARIANA_PLUS (zero drift); the engine enum
  `{PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4, TOP_SP=5}`, the 108 `struct2opcode`
  bindings, and the dtype enum (16 base + `FP4_EXP2` + `CPTC1..7` + the MX surface)
  are all MARIANA's, unchanged. The MX firmware-visible surface (`PE_MANAGE_SEED 0x08`,
  `LDWEIGHTS_MX 0x09`, `MATMUL_MX 0x0a`, `QUANTIZE_MX 0xe3`, `ConvLutLoad 0xe4`) is
  **retained, not extended**. `[HIGH/OBSERVED]`
* **Dispatch handlers are byte-for-name identical, `+0/−0` on every engine.** A broad
  `S:`-prefixed set-diff over all five DEBUG DRAMs returns `ADDED=[] REMOVED=[]`:
  ACT `29==29`, DVE `53==53` (60-strict), PE `29==29`, POOL `41==41`, SP `18==18`.
  `[HIGH/OBSERVED — the five committed image pages]`
* **Collectives are identical to MARIANA (`ADDED=0`).** The `SB2SB_COLLECTIVE`
  handler set, the collective pseudo-ops `{0xC7/C8/D9/DA/CB/D8/D5/C3/DB}`, the
  on-instruction `COLLECTIVE_TYPE`/`CCE_OP`/`LNC_SIZE_FMT`/`DGE_COMPUTE_OP` enums, and
  the 6-element CCE-reducible dtype set are byte-identical MARIANA→MARIANA_PLUS. The
  only collective-adjacent v4+ touch is the DGE `sb2sb` reshape *kind* (§3), a
  descriptor-generation optimization that **feeds** the SB2SB transport — not a new
  collective op or algorithm. `[HIGH/OBSERVED — SX-GEN-08, re-confirmed]`
* **PROF tables reused byte-identical.** The four PROF-bearing engines reuse
  MARIANA's per-engine `PROF_CAM`/`PROF_TABLE` **byte-for-byte** — re-carved and
  `cmp -s`-clean this session: ACT `326bc0dd`, DVE `ca588683`, PE `43475cec`,
  **POOL CAM `0951b326` / TABLE `534f2239`** (both `== True` byte-identical against
  the MARIANA POOL tables). SP has no PROF instance. A recompile that re-armed the
  HW-decode profiler would not preserve these byte-for-byte; v4+ left them untouched.
  `[HIGH/OBSERVED]`

### 4.2 Merely refreshed — the register-map (HW-config) dir

MARIANA_PLUS carries its **own `arch-headers/mariana_plus/` register-map dir** (848
files) distinct from `arch-headers/mariana/` (832 files), a `+16`-file delta
(`fd --no-ignore -tf | wc -l` this session). The file-level delta:

* **ADDED** at v4+: `hbm_xbar_crc_hash{,_consts.hpp,.go,.h,.hpp,_reg_dump.cpp,_reg_dump.h}`
  (6 files — the HBM crossbar gains a CRC-hash addressing/integrity block);
  `xbar_ctrl{_consts.hpp,.go,.h,.hpp,.yaml}` (5 files — a new crossbar-control block);
  `intc_{cc,d2d,hbm,io_fabric,pcie,peb_intc,sdma,top_sp,tpb}_enums.svh` (9 files — the
  interrupt-controller enums broken out per fabric domain).
* **DROPPED** at v4+: `hbm_xbar_ctrl{_consts.hpp,.go,.h,.hpp}` (4 files — superseded by
  `hbm_xbar_crc_hash`).

`[HIGH/OBSERVED for the file delta; INFERRED-HIGH for the HW meaning]` This is a
**register-map refresh** — a relayout of the HBM-crossbar and interrupt-controller
register descriptions — that drives the firmware recompile (relocated literals). It is
*not* enumerated at the byte/CSR-offset level, and notably `sp.h` is sha256-**identical**
between the two dirs (DX-GEN-04 §6.3); the device-facing CSR schema is "mostly mariana
+ a refresh", with **zero confirmed new device-CSR bytes** at the firmware-image layer
(no NX engine carries any new dtype/handler/opcode that would require them).

---

## 5. The v4 ↔ v4+ essence — a flag-refresh recompile, with byte evidence

The decisive characterization. Three byte-level signatures, each re-derived this
session, prove **recompile + flag-refresh**, not a silicon step:

**(1) Block-similarity — high-prefix, then recompile-relocated.** The POOL NX DEBUG
IRAM pair (MARIANA `@0x44b540` size `0x1c080` sha `41b6c798`; MARIANA_PLUS `@0x714700`
size `0x1d340` sha `9b514bb6`) is **byte-identical through the reset + boot trampoline +
dispatch prefix** — the first byte divergence is at **`0xa2`** — but the **positional
16-byte-block similarity is only 5.0%** because the recompile inserted the DGE
fast-path and relocated every downstream literal, shifting alignment. This is the exact
signature of a *fresh build of the same source*, not a binary patch: a patch leaves
literals in place; a recompile shifts them. `[HIGH/OBSERVED — carve+diff this session]`

**(2) funcVA shift with byte-stable names.** Where the IRAM blobs differ, the change is
**relocation, not re-routing**: the POOL SEQ default-trampoline relocated `0x3075 →
0x30ea` (`+0x75`) with every real handler slot shifting the same `+0x75`; the SP default
trampoline `0x2975 → 0x298b` (`+0x16`) with the **real handler-slot trampolines
byte-identical**; the ACT DRAM dispatch trampolines a uniform `MARIANA = MARIANA_PLUS +
0x20`. The handler *names*, the table base, and the real-slot pattern are gen-stable —
only the funcVAs moved, as a recompile moves them. `[HIGH/OBSERVED]`

**(3) PROF byte-identity + reset identity.** §4.1 showed the four PROF CAM/TABLE pairs
`cmp -s`-clean. The reset geometry (§6) is byte-identical. The EXTISA Q7 SOs are
byte-identical (§2.2). A silicon step would have re-armed PROF and/or moved the reset;
v4+ did neither.

**The flag essence.** The build carries the **removed compile flag**
`NRTUCODE_MPLUS_ON_MARIANA` and the **runtime debug env-var**
`NEURON_RT_DBG_V4_PLUS=0/1` — both verified present in `libnrtucode_internal.so` this
session (`rg --no-ignore -a -b -o`):

```text
NRTUCODE_MPLUS_ON_MARIANA   @ file offset 18575 (0x488f)  and  20398 (0x4fae)
NEURON_RT_DBG_V4_PLUS=0/1   @ file offset 18671 (0x48ef)
```

(`libnrt.so` is not part of this shipped package — only `libnrtucode_internal.so` and
`libnrtucode.a` ship under `…/custom_op/c10/lib/`; both flag strings live in the
container.) The `=0/1` suffix confirms `NEURON_RT_DBG_V4_PLUS` is the env-var that
toggles the v4-vs-v4+ runtime path — the runtime replacement for the compile-time
`NRTUCODE_MPLUS_ON_MARIANA` that "turned MARIANA_PLUS firmware on over a MARIANA core".
`[HIGH/OBSERVED]`

> **CORRECTION — `[SX-GEN-03 §6]` and `[DX-GEN-04 §6.5]` call v4+ a "silicon
> stepping / hardware-revision (Trn3-pre)".** Against the binary and the five
> committed image pages, that framing is **over-stated**. The only HW-flavored
> evidence is the file-name-level register-map *refresh* (§4.2) — and DX-GEN-04 §6.3
> itself records **zero new device-CSR bytes** and a sha-identical `sp.h`. Everything
> the firmware images show — byte-identical PROF, byte-identical EXTISA Q7 kernels,
> byte-identical reset, identical ISA/opcodes/collectives/handlers, a recompile that
> only relocates literals — is consistent with a **software re-release + register-map
> refresh on the SAME compute silicon**. This page therefore characterizes v4+ as a
> **recompile / flag-refresh**, with the register-map refresh as its only HW-config
> footprint, and does **not** assert a silicon step. The `mariana_plus_libs` peer
> table, the `+8` coretype slot, and the own register-map dir make it a **distinct
> runtime codename** — but a distinct codename is not the same as a distinct die.
> `[HIGH/OBSERVED — reconciles SX-GEN-03/DX-GEN-04 with the committed image matrix]`

**PeManageSeed is NOT a v4+ delta.** `PE_MANAGE_SEED (0x08)` / `LDWEIGHTS_MX (0x09)` /
`MATMUL_MX (0x0a)` / `ConvLutLoad (0xe4)` all **first ship at MARIANA (v4)** and are
**retained byte-for-name at v4+** (the four PeManageSeed strings co-locate with
`BEGIN on mariana` in the MARIANA PE DRAM at byte-identical offsets). The earlier
"PeManageSeed is v4+-only" label was a carve-coverage artifact (only the CAYMAN and
MARIANA_PLUS PE images had been carved). Do **not** attribute PeManageSeed, the MX
matmul trio, or the TIE-Xorwow/LFSR RNG to MARIANA_PLUS — they are v4 arrivals.
`[HIGH/OBSERVED — [mariana-plus-pe](../images/mariana-plus-pe.md), DX-GEN-04 §3.1]`

---

## 6. Reset geometry across the lineage — the byte fingerprint

The reset vector is the single cleanest cross-gen discriminator, decoded this session
with the native `ncore2gp` `xtensa-elf-objdump` (exit 0) off the carved POOL NX IRAM
blobs of each gen. `[HIGH/OBSERVED for v4/v4+; v5 OBSERVED here, lineage CARRIED]`

```text
gen            IRAM head (16 B)                       primary    secondary   geometry
-------------  -------------------------------------  ---------  ----------  -----------------
v4   MARIANA   06 7d 00 00 00 00 86 7e 00 00 … a0 71  j 0x1f8    j 0x204     +0x1c (vs CAYMAN 0x76→0x1dc)
v4+  M_PLUS    06 7d 00 00 00 00 86 7e 00 00 … a0 71  j 0x1f8    j 0x204     +0x1c  (BYTE-IDENTICAL to v4)
v5   MAVERICK  06 75 00 00 00 00 86 76 00 00 … a0 71  j 0x1d8    j 0x1e4     −0x20 from v4 (0x1f8→0x1d8)
```

The v4 and v4+ heads are **byte-identical** (`06 7d … 86 7e`, `j 0x1f8`/`j 0x204`,
both → `enter_run @0x90` via the shared `const16 a0,0x90 ; jx a0` boot stub): the
recompile **preserves** the +0x1c MARIANA shift with **no further shift**. MAVERICK is
**distinct**: `06 75 … 86 76`, `j 0x1d8`/`j 0x1e4` — a **−0x20** reset shift
(`0x1f8 → 0x1d8`). The DRAM `.globstruct` magic `0x6099cb34` + init block
(`4×0x1000 + 4×0xffffff`) is byte-identical v4 ↔ v4+. `[HIGH/OBSERVED]`

So at the reset/boot level: **v4+ == v4, byte-for-byte; v5 = v4 − 0x20.** The recompile
left the boot geometry untouched; the real gen-step (v4+ → v5) moved it.

---

## 7. Lineage placement — v4+ is the LAST byte-grounded tier before the v5 wall

```text
SUNDA(v2, ct6)  <  CAYMAN(v3, ct13)  <  MARIANA(v4, ct21)  <  MARIANA_PLUS(v4+, ct29)  <  MAVERICK(v5, ct37*)
   baseline         full DGE +          MX trio + FP4/CPTC     recompile + DGE FAST-PATH    re-architecture
   topology         SB2SB collective    + RNG + PeManageSeed    + register-map refresh        (the v5 wall)
```

* **v3 CAYMAN** establishes the full DGE + transpose engine, the `SB2SB_COLLECTIVE`,
  the `gather_xpose` transpose ISA, and the 16-code+FP8 dtype base.
* **v4 MARIANA** adds the MX/microscaled matmul trio (`MATMUL_MX`/`LDWEIGHTS_MX`/
  `QUANTIZE_MX`), `FP4_EXP2`/`CPTC` dtypes, the TIE-Xorwow + LFSR RNG, and
  `PE_MANAGE_SEED`. The big capability jump.
* **v4+ MARIANA_PLUS (this page)** is the **narrowest step in the line**: same
  ISA/dtypes/collectives/RNG/PeManageSeed (all retained byte-for-name), **plus the one
  DGE reshape fast-path + a register-map refresh.** No ISA change, no model change. The
  NX IRAM *grew* gen-wide (the fast-path bulk — the *opposite* of the CAYMAN→MARIANA
  shrink), itself a v4+ fingerprint.
* **v5 MAVERICK** is a genuine **re-architecture** — and it is the contrast that shows
  why v4+ is the last byte-grounded tier. Per the committed
  [Maverick (v5) Profile](./maverick-profile.md) `[CARRIED]`: a new **−0x20 reset
  geometry** (§6), `enter_run @0x94` (vs v4/v4+ `@0x90`), the **ACT datapath folded
  into DVE**, the DGE reshape **fast-path DROPPED**, a `~60%`-smaller independent build
  on a newer toolchain (clang-15 / XtensaTools-15.05, `ET_DYN`), 4× SBUF / 16× SP / 4×
  PE-DVE, a UCIE chiplet interconnect, +6 opcodes, and a re-modeled sync. MAVERICK is
  internal-only (`0` `.a` members; images live only in the container) — **its
  interiors are CARRIED, not OBSERVED here**, and must not be stated as fact.

The shape of the line: MARIANA_PLUS is the **small refresh between two large events**
— the v4 capability jump behind it, the v5 re-architecture ahead of it. It is the only
generation that adds **no ISA and no model change** — which is precisely why it is
fully byte-grounded, while everything past it is the v5 inference wall. This synthesis
feeds the sibling [Codename→Generation Map](./codename-generation-map.md) and the
[Master Capability Matrix](./master-capability-matrix.md) (authored in parallel).

---

## 8. Adversarial self-verification

The five strongest claims, re-challenged against the binary this session:

1. **`arch_id 28 / coretype 29`.** *Challenge:* the prompt and two reports say the
   inverse. *Re-verify:* `nrtucode_get_ext_isa_internal @0x9b2b30` `lea`s
   `mariana_plus_libs @0x9b9010` as a peer in lineage order; `coretype = arch_id + 1`
   holds for all four shipped gens; NCFW `get_image` selector immediate `0x1c`.
   **HOLDS** — page carries 28/29, prompt pairing flagged transposed. `[HIGH/OBSERVED]`
2. **124-member `.a` count.** *Challenge:* `rg -ci mariana` returns 248. *Re-verify:*
   the substring trap inflates it; `rg -i mariana | rg -vi mariana_plus | wc -l` = 124,
   `rg -ci mariana_plus` = 124, `= ` CAYMAN's 124; total 435 = 48+124+124+124+0+15.
   **HOLDS.** `[HIGH/OBSERVED]`
3. **PROF byte-identity v4 ↔ v4+.** *Challenge:* equal size could mask different bytes.
   *Re-verify:* POOL `PROF_CAM` (`@0x5a3080` v4 / `@0x86ef00` v4+) and `PROF_TABLE`
   (`@0x5a3480` / `@0x86f300`) compared in full → `== True` (sha `0951b326` /
   `534f2239` on both). **HOLDS.** `[HIGH/OBSERVED]`
4. **+0x1c reset identity (v4 == v4+, distinct from v5).** *Challenge:* a later variant
   could hide a second shift. *Re-verify:* `ncore2gp` objdump → v4 `j 0x1f8` == v4+
   `j 0x1f8` (heads byte-identical); v5 MAVERICK `j 0x1d8` (−0x20). **HOLDS.**
   `[HIGH/OBSERVED]`
5. **DGE fast-path gen-wide (incl. SP) + the two flag strings.** *Challenge:* the
   strings could be POOL/DGE-only, or stray text. *Re-verify:* all four present (×1) on
   MARIANA_PLUS SP DEBUG DRAM (`@0x752f80`, sha `2958154e`) — the no-DGE engine —
   `push REGWRITE` retired; `NRTUCODE_MPLUS_ON_MARIANA` @18575/20398 and
   `NEURON_RT_DBG_V4_PLUS=0/1` @18671. **HOLDS.** `[HIGH/OBSERVED]`

All five survive. The residual frontiers (carried OPEN): the byte-level v4+ register/CSR
offset deltas (§4.2 is file-name-level); the exact `wait_for_credit` credit-counter
mechanics (string + presence only); the per-opcode SEQ table row binding (the FLIX
desync frontier); MAVERICK's interiors (CARRIED, §7).

---

## 9. Honesty ledger

**HIGH / OBSERVED (reproduced this session):** container sha `b7c67e89…632fc329b`;
`.a` sha `158dadc5…d7bd6130`; 200 `_get` symbols each gen / 100-accessor shape
(14·4+12+32); 124 `.a` members each gen (435 total); 4 ISA dirs (no
`mariana_plus_arch_isa`); arch-headers 6 dirs, mariana 832 / mariana_plus 848 (+16);
`mariana_plus_libs @0x9b9010` as a peer in the `get_ext_isa` `lea` ladder; the four
PROF CAM/TABLE pairs byte-identical (POOL `cmp == True`); reset v4 `j 0x1f8` == v4+,
v5 `j 0x1d8` (−0x20) via `ncore2gp`; the four DGE fast-path strings present×1 / absent
on every NX engine incl. SP, `push REGWRITE` retired; both flag strings at the cited
offsets; POOL NX DEBUG IRAM first divergence `@0xa2`, positional 16B similarity 5.0%.

**HIGH / INFERRED (naming/structural, no contradicting evidence):** the DGE fast-path
*fusion*/credit-wait/`sb2sb`-no-HBM semantics; "v4+ = recompile + flag-refresh"
essence; the HW meaning of the register-map refresh (HBM-xbar routing/integrity +
per-fabric INTC).

**CARRIED:** `arch_id 28/coretype 29` from the GEN-01 `get_image` ladder; the
collective equivalence (SX-GEN-08); MAVERICK (v5) interiors — `−0x20` reset (here
re-OBSERVED), `enter_run @0x94`, ACT→DVE fold, DGE-dropped, ~60%-smaller build — from
the committed [maverick-profile](./maverick-profile.md).

**LOW / NOT CLAIMED:** the byte-level v4+ register/CSR offset deltas; the silicon
part/SKU MARIANA_PLUS maps to; the exact `wait_for_credit` mechanics; which runtime
selects v4 vs v4+ beyond the env-var name.

---

## 10. Cross-references

- [MARIANA_PLUS × ACT](../images/mariana-plus-act.md) — the v4+ template (null
  functional delta + DGE fast-path, established first).
- [MARIANA_PLUS × DVE](../images/mariana-plus-dve.md) ·
  [× PE](../images/mariana-plus-pe.md) ·
  [× POOL](../images/mariana-plus-pool.md) (the fast-path's home) ·
  [× SP](../images/mariana-plus-sp.md) (the gen-wide proof).
- [DGE Reshape Engine](../firmware/dge/dge-reshape.md) — the fast-path's home subsystem
  (descriptor structs, analyze→emit pipeline, per-gen presence map).
- [Codename Cross-Walk](../reference/codename-crosswalk.md) — the codename ↔ coretype ↔
  arch_id table.
- [Maverick (v5) Profile](./maverick-profile.md) — the v5 re-architecture (the contrast
  that bounds v4+).
- [Codename→Generation Map](./codename-generation-map.md) ·
  [Master Capability Matrix](./master-capability-matrix.md) — the sibling
  generation-level pages.
- [Confidence & Walls Model](../reference/confidence-model.md) — the tag taxonomy.
