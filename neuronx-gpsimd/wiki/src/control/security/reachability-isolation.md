# Custom-Op Reachability / Isolation Model

> **Scope.** What a GPSIMD custom op running on a compute **Vision-Q7** core *can*
> and *cannot* reach, end-to-end, and what enforces each boundary. This is the
> **capability/reachability keystone** of the Part-13 security sub-section: it joins
> four enforcement layers into one model — **(1)** the on-core address-space
> reachability (the `neuron_translate` 5-region software TLB, the 32-bit NX-local
> map, the SBUF AXI aperture, the per-core DRAM window, the PSUM exclusion); **(2)**
> the CSR / BAR0 surface gated at the fabric edge by the `amzn`/`user` remapper;
> **(3)** the MPU-enforced on-core code/data boundaries; and **(4)** the isolation
> guarantees between co-located custom ops, between the 8 SPMD cores, and between
> co-tenant fractional-NeuronCore workloads.

It is a **consolidation** page: it reconciles the byte-grounded siblings into one
capability table and gives the two algorithms a reimplementer most needs — the
`neuron_translate` 5-region lookup and the MPU boundary check — without
re-deriving what the siblings already pin. Confidence is tagged
`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` per claim.

Related pages (cross-linked, not duplicated):
the SoC↔Q7 translate machinery [`../address/soc-q7-translation-windows.md`](../address/soc-q7-translation-windows.md);
the unified SoC map [`../address/unified-soc-memory-map.md`](../address/unified-soc-memory-map.md);
the POOL 8-core subtree [`../address/tpb-pool.md`](../address/tpb-pool.md);
the build-time LSP map [`../address/lsp-sram-window-map.md`](../address/lsp-sram-window-map.md);
the fabric-edge firewall [`../csr/remapper.md`](../csr/remapper.md);
the perimeter framing [`soc-fabric-perimeter.md`](soc-fabric-perimeter.md);
the XEA3 core model [`../interrupt/xea3-interrupt-architecture.md`](../interrupt/xea3-interrupt-architecture.md);
the on-die window CSRs [`../csr/tpb-xt-local-reg.md`](../csr/tpb-xt-local-reg.md).
Forward: the leakage surface [`side-channel-leakage.md`](side-channel-leakage.md);
the SEC synthesis [`security-synthesis.md`](security-synthesis.md).

> **PROVENANCE.** Every reachability claim is grounded in a shipped binary-derived
> artifact: the disassembled `neuron_translate` (`translation.o`, native
> `ncore2gp` `xtensa-elf-objdump`), the RTL-generated CSR schemas
> (`csrs/sprot/*.json`, `csrs/tpb/tpb_xt_local_reg.json`), the flat address map
> (`address_map_flat.yaml`), the customop LSP scripts (`elf32xtensa.x`), the ISA
> config (`libisa-core.so`), and the SEQ boot firmware (`img_SUNDA_NX_POOL_*` in
> `libnrtucode.a`, disassembled with the shipped Cadence `ncore2gp` objdump). The
> MPU-and-PRID bring-up sequence in §4 was **re-disassembled this session**. v5 /
> MAVERICK is **header-OBSERVED only**; any v5-interior behaviour is flagged
> INFERRED.

---

## 0. The reachability thesis, in one paragraph

A custom op is **eight cooperating Q7 cores** (POOL engine) plus the **SEQ**
sequencer, each a Cadence **Vision-Q7** core (`ncore2gp`, **XEA3** exception
architecture, an MPU, no MMU) dereferencing a **32-bit NX-local** pointer. The
*only* way a core reaches anything outside its own 32-bit space — HBM tensors,
SBUF, EVT_SEM, a remote die — is through a small set of **hardware window
registers** programmed by the `neuron_translate` software TLB. Within the 32-bit
space, the **MPU** partitions IRAM (execute) from data and pins the window
apertures non-executable. *Below* the core, at the SoC fabric edge, the
**`amzn`/`user` remapper** is an address+master-ID firewall that decides whether
the core's resulting AXI transaction is even allowed onto the fabric. Three things
a custom op **cannot** reach: **PSUM** (no AXI path — it is the PE-array's private
accumulator), the **privileged "Pacific" management plane** (a different core
behind a fail-CLOSED remapper), and **another tenant's** SBUF/HBM partition (gated
by the upstream DMA-engine VMID and the fabric remapper). The capability table in
§1 is the headline; §2–§5 derive each row.

---

## 1. THE CONSOLIDATED REACHABILITY / CAPABILITY TABLE

`access` is from the point of view of **a custom-op Q7 core**. `R`/`W`/`X` =
read/write/execute reachable. The **enforcement mechanism** column names *what*
makes the access succeed or fail; the **route** column names *how* the address is
formed. `[HIGH · OBSERVED]` for every region/route unless tagged.

| Region (SoC or NX) | Access | Route from a Q7 core | Enforcement mechanism | conf |
| :--- | :--- | :--- | :--- | :--- |
| **NX IRAM** `[0x0, 0x1000)` (4 KiB used; 64 KiB region) | `R/W/X` boot, then `R/X` | direct NX deref; `_start` places vectors at NX `0x0` | LSP `iram0_0_seg`; **MPU** marks it execute | `HIGH·OBS` |
| **per-core dataram (NX)** `[0x80000, 0x90000)` (64 KiB) | `R/W` | direct NX deref — the pointer *is* the address; **PRID-rebased** so each core sees only its own | **MPU** data attr; PRID selects the physical bank | `HIGH·OBS` |
| **NX register block** `[0x100000, 0x100840)` | `R/W` | direct NX deref (Sunda `MEM_WINDOWn` / `SEQUENCER_WINDOW` CSRs) | mem-mapped CSR; `neuron_translate` *writes* here | `HIGH·OBS` |
| **Cayman window-TCAM** NX `0x2000`-rel (40 windows) | `R/W` | direct NX deref (`tpb_xt_local_reg` `q7`/`nx` window regs) | mem-mapped CSR; `window_valid` gate (§3) | `HIGH·OBS` |
| **SBUF** SoC `[0x2000000000, 0x2004000000)` (32 MiB live) | `R/W` | **pinned 64-MiB window** @ NX `0x80000000` (HIT, pure arith) | pinned TLB record 3; fabric remapper | `HIGH·OBS` |
| **hbm_scratch heap** (HBM-resident) | `R/W/X` | **pinned 64-MiB window** @ NX `0x84000000`; customop code is *linked here* | pinned TLB record 4; LSP `sram0_0_seg`; **MPU** code/data split | `HIGH·OBS` |
| **general HBM tensor** SoC `[0x0, 0x1000000000)` etc. | `R/W` | **dynamic 16-MiB window** @ NX `0x07/09/0a000000` (3-deep `%3` TLB) | dynamic TLB records 0–2 → `MEM_WINDOW3/5/6`; remapper | `HIGH·OBS` |
| **EVT_SEM** SoC `0x2802700000` (1 MiB) | `R/W` (inc/dec by delta) | **dynamic 16-MiB window** (no pinned slot) | dynamic TLB; write-only-by-delta semantics | `HIGH·OBS / MED route` |
| **remote die / mesh chip** | `R/W` | dynamic window with `DIE[47]`/`CAYMAN_ID[53:48]` set in the SoC tag | dynamic TLB; remapper compares full 58-bit addr | `HIGH·die-bit; MED·route` |
| **PSUM** SoC `0x2802000000` (4 MiB) | **NONE via AXI** | *no pinned window*; a dynamic window can *form* the address but PSUM has **no AXI slave port** to the Q7 | PE-array-private accumulator; §2.5 | `HIGH·OBS exclusion` |
| **BAR0 CSR aperture** (other engines' CSRs) | `R/W` **iff admitted** | dynamic window onto the engine's SoC CSR base | **`user_remapper` CAM** (fail-OPEN miss); AxPROT *not* forgeable | `HIGH·OBS gate` |
| **privileged firmware / "Pacific" plane** | **NONE** | — | **`amzn_remapper`** fail-CLOSED; separate management core | `HIGH·OBS` |
| **another tenant's SBUF/HBM** | **NONE** | a window *could* be programmed, but the txn is denied | upstream **DMA-engine VMID** + fabric remapper master-ID | `HIGH·layering; MED·per-tenant` |
| **another co-located op (same core, time-share)** | shared address space | same NX map, serially | **no HW isolation** between ops on one core — software contract | `HIGH·OBS (absence)` |

> **THE ONE-LINE MODEL.** *Inside* 32 bits: MPU. *Crossing* 32→58 bits:
> `neuron_translate` windows. *Crossing* the fabric edge: the remapper firewall.
> *Across tenants*: VMID upstream + remapper master-ID. **PSUM, Pacific, and other
> tenants are the three hard walls; everything else is a window away.**

---

## 2. On-core address-space reachability

### 2.1 The 32-bit NX-local map — the whole reachable-by-pointer space

`HIGH/OBSERVED` — from [`../address/soc-q7-translation-windows.md`](../address/soc-q7-translation-windows.md) §2
and the customop LSP. A Q7 core dereferences a **32-bit `void*`**. Two structural
classes: **direct** NX regions (the pointer *is* the address) and **windowed**
regions (an NX slice is a movable view of a 58-bit SoC address).

| NX range | class | what it is | reach |
| :--- | :--- | :--- | :--- |
| `[0x00000, 0x01000)` | direct | IRAM (vectors + early code), 4 KiB used | i-fetch |
| `[0x80000, 0x90000)` | direct | per-core dataram, 64 KiB, **PRID-rebased** | local deref |
| `[0x100000, 0x100840)` | direct | NX register block (window CSRs live here) | CSR deref |
| `0x07000000` | dyn 16 MiB | dynamic window 0 → HW `MEM_WINDOW3` | movable |
| `0x09000000` | dyn 16 MiB | dynamic window 1 → HW `MEM_WINDOW5` | movable |
| `0x0a000000` | dyn 16 MiB | dynamic window 2 → HW `MEM_WINDOW6` | movable |
| `0x80000000` | pin 64 MiB | SBUF window → SoC `[0x2000000000, 0x2004000000)` | resident |
| `0x84000000` | pin 64 MiB | hbm_scratch window (customop code is linked here) | resident |

> **NOTE — the "58-bit" SoC space.** Siblings say "57-bit"; the precise form
> ([`../address/unified-soc-memory-map.md`](../address/unified-soc-memory-map.md) §0)
> is **"58-bit decode field (`[57:0]`), `[54:0]` routed, 2⁵⁴ populated."** The
> remapper CAM compares all 58 bits; a window tag carries `DIE[47]`/`CAYMAN_ID[53:48]`.
> This page uses the precise form; "57-bit" in a sibling means the same thing.

### 2.2 The `neuron_translate` 5-region software TLB (the reach engine)

`HIGH/OBSERVED` — full machinery in
[`../address/soc-q7-translation-windows.md`](../address/soc-q7-translation-windows.md) §4
(re-disassembled byte-exact from `translation.o`). The reachability-relevant
contract: a **5-entry** software TLB (3 dynamic 16-MiB slots `%3` round-robin + 2
pinned 64-MiB slots), where a miss **reprograms the HW window register** and a 4th
distinct 16-MiB region **evicts** a slot (the transient-reference hazard). The
lookup is the gate that turns a 58-bit SoC address into a dereferenceable NX
pointer — and the place a reimplementer most often gets the masks wrong:

```c
/* neuron_translate(ctx, soc_ptr) -> NX 32-bit pointer.
 * 5-region software TLB; records 0..2 dynamic (16 MiB), 3..4 pinned (64 MiB).
 * Disasm @ translation.o 0x120. The SEARCH scans all 5; only 0..2 recycle.
 * [HIGH · OBSERVED, re-verified in the address keystone]                       */
typedef struct {
    uint64_t          ptr;      /* +0x00 matched SoC base (tag), full 64-bit    */
    uint32_t          window;   /* +0x08 NX base (0x07000000 / 0x80000000 / …)  */
    uint64_t          mask;     /* +0x10 64-bit AND mask (16MB=0xff000000,       */
                                /*       64MB=0xfc000000)                        */
    volatile uint32_t *reg_loc; /* +0x18 abs NX addr of MEM_WINDOWn_LO (dyn only)*/
} map_record_t;

typedef struct { map_record_t rec[5]; uint8_t next_alloc; /* 0..2 */ } xlate_ctx_t;

static uint32_t neuron_translate(xlate_ctx_t *ctx, uint64_t soc_ptr)
{
    /* SEARCH — 5-way linear scan, full 64-bit masked compare. */
    for (int i = 0; i < 5; i++)
        if ((soc_ptr & ctx->rec[i].mask) == ctx->rec[i].ptr)
            /* HIT @0x204 — pure arithmetic, no eviction, no HW write. */
            return ctx->rec[i].window + (uint32_t)(soc_ptr & ~ctx->rec[i].mask);

    /* MISS @0x215 — evict the round-robin victim and REPROGRAM its window reg.
     * Only the 3 dynamic slots are ever the victim (next_alloc in {0,1,2}).   */
    map_record_t *r = &ctx->rec[ctx->next_alloc];
    r->ptr = soc_ptr & 0xff000000ull;            /* install 16 MiB lo prefix     */
    volatile uint32_t *reg = r->reg_loc;          /* 0x100218/228/230            */
    __asm__ volatile("memw");
    reg[1] = (uint32_t)(soc_ptr >> 32);           /* MEM_WINDOWn_HI (carries DIE)*/
    reg[0] = (uint32_t)(soc_ptr & 0xff000000ull); /* MEM_WINDOWn_LO              */
    __asm__ volatile("memw");
    ctx->next_alloc = (uint8_t)((ctx->next_alloc + 1) % 3);
    return r->window + (uint32_t)(soc_ptr & 0x00FFFFFFull);
}
```

> **CORRECTION (carried) — there is NO bounds or null guard in this path.** The
> MISS path unconditionally programs the window for *any* 64-bit pointer; an
> out-of-range SoC address yields an out-of-range *window*, silently on-core. The
> guard is **off-core**: the SoC tag's high dword (with `DIE`/`CAYMAN_ID`) is what
> the **fabric remapper** (§3) then validates. A custom op cannot reach a forbidden
> SoC region *because the remapper denies the resulting AXI txn*, not because
> `neuron_translate` refused to form the pointer. `[HIGH · OBSERVED]`

### 2.3 The SBUF 32-MiB AXI aperture (pinned 64-MiB window)

`HIGH/OBSERVED` — [`../address/tpb-pool.md`](../address/tpb-pool.md) §1,
[`../address/unified-soc-memory-map.md`](../address/unified-soc-memory-map.md) §1.
SBUF (`TPB_0_STATE_BUF`) is **32 MiB** of on-chip state buffer at SoC
`0x2000000000`, organised **128 partitions × 256 KiB** (`STATE_BUF_SZ 0x2000000`;
`partOffset[25]` selects SBUF=0 / PSUM=1). A custom op reaches it through the
**pinned 64-MiB window** at NX `0x80000000` (`neuron_translate` record 3, tag =
`ENGINE_BASE_ADDR − 41 MiB`, mask `0xfc000000`). The window is a **HIT** — pure
arithmetic, never an eviction — so SBUF is *always resident* in the 32-bit space
and never thrashes the 3-deep dynamic set.

> **QUIRK — the 64-MiB *window* over a 32-MiB *buffer*.** The pinned window granule
> is 64 MiB (`0xfc000000` mask) but SBUF live data is 32 MiB; the upper half of the
> window maps `TPB_0_TPB_RESERVED10` pad
> ([`../address/tpb-pool.md`](../address/tpb-pool.md): `0x2002000000`, 32 MiB pad).
> So an SBUF access through NX `[0x80000000, 0x82000000)` is live; NX
> `[0x82000000, 0x84000000)` lands in pad. The reachable SBUF *byte range* is
> 32 MiB, not the full 64-MiB window. `[HIGH · OBSERVED]`

> **NOTE — "AXI aperture" vs "NX window" are two coordinate systems for one buffer.**
> The 32-MiB figure is the SoC-side **AXI decode aperture** (`STATE_BUF`); the
> 64-MiB figure is the NX-side **translation window** that fronts it. The custom op
> sees the NX window; the fabric sees the AXI aperture; `neuron_translate` is the
> bridge. Do not conflate the two granules. `[HIGH · OBSERVED]`

### 2.4 The per-core DRAM window — PRID-rebased `[0x80000, 0x90000)`

`HIGH/OBSERVED`. Each Q7 core has a private **on-core dataram** seen at NX
`[0x80000, 0x90000)` (64 KiB,
[`../address/soc-q7-translation-windows.md`](../address/soc-q7-translation-windows.md) §2);
the customop runtime carves its dataram + libc `xmem` heaps here. On the SoC side
the per-core DRAM is a distinct leaf per core
([`../address/tpb-pool.md`](../address/tpb-pool.md) §3):

```
TPB_0_POOL_Q7_CORE{i}_DRAM = 0x2803180000 + i*0x100000   (256 KiB each, 1 MiB pitch)
TPB_0_POOL_Q7_CORE{i}_IRAM = 0x2803100000 + i*0x100000   (128 KiB each)
```

The same NX pointer `[0x80000, 0x90000)` on **every** core resolves to **that
core's own** physical DRAM bank — the core's **PRID** selects the bank. Two
distinct PRID decodes (both `HIGH/OBSERVED`):

- **Direct deref** uses no rebase arithmetic — the per-core dataram is hardware-
  private; a core simply cannot name a sibling's bank by NX pointer.
- **SDMA staging** rebases explicitly: `dram_addr_to_soc_addr()`
  (`data_transfer.o @0x210`) re-reads PRID and maps it to a **per-core 64-KiB SoC
  aperture** via `window_index = 2·prid + 9`, then
  `soc = SoC_BASE + (window_index << 16) + (dram_addr − 0x80000)`. The
  `data_transfer.cpp:160` assert guards the `[0x80000,0x90000)` staging window;
  `data_transfer.cpp:171` fatals on `cpu_id ≥ 8`.

So a core *cannot* address a sibling core's dataram by NX pointer: the per-core
view is rebased by hardware identity (PRID), not by an address the op controls.

> **NOTE — two "per-core DRAM" sizes are not a contradiction.** The NX-local view is
> **64 KiB** (`[0x80000,0x90000)`); the SoC leaf is **256 KiB** (`CORE{i}_DRAM`).
> The 64 KiB is the slice the customop's direct-deref / SDMA-staging path uses; the
> larger SoC leaf is the full decode footprint. (`TPB_0_POOL_NX_DRAM` at
> `0x2803040000` is the *SEQ/NX* core's 64-KiB DRAM, a separate leaf from the 8
> compute cores'.) `[HIGH · OBSERVED]`

### 2.5 The PSUM exclusion — no AXI path

`HIGH/OBSERVED`. **PSUM** (`TPB_0_PSUM_BUF`, SoC `0x2802000000`, **4 MiB**, 128
part × 16 bank × 1 KiB, `PSUM_BUF_SZ 0x400000`; `PSUM_BUF_OFFSET 0x02000000`,
per-partition 32 KiB / 16 KiB active) is the **PE-array's private accumulator
buffer** ([`../address/tpb-pool.md`](../address/tpb-pool.md) §1 — *"PE-array PSUM
accumulator buffer"*). The reachability fact: **a Q7 custom op has no AXI path to
PSUM — PSUM has no AXI aperture and no NX window** (keystone-fact #600).

- The Q7 has **no dedicated SBUF/PSUM compute port** — it reaches on-chip memory
  only as memory-mapped AXI through the `axi2sram` bridge. SBUF has a 32-MiB AXI
  aperture (§2.3); **PSUM has none.**
- There is **no pinned window** for PSUM (the two pins are SBUF + hbm_scratch only),
  and no fixed NX window — PSUM is *structurally* outside the 32-bit reachable set.
- The architectural correlate: `partOffset[25]` selects SBUF (0) / PSUM (1) only
  inside the PE/ACT datapath; the SBUF test `(addr & 0x1fffffff) < 0x02000000`
  **fails** for any PSUM address (`≥ 0x02000000`). The compiler's int-op router
  (`SundaISel`) **falls back to the Vector engine if any operand lives in PSUM**,
  precisely because *"GpSimd Engine cannot access PSUM."*

> **CORRECTION — "PSUM rides a dynamic window" is a reach *of the address*, not a
> reach *of the data*.** Sibling §8 of the translate page lists PSUM under "dynamic
> 16-MiB window (Q7 sw access) `MED`", and its §9 even walks
> `neuron_translate(0x2802012345)` → NX `0x07012345`. That `MED` is the right
> caution and the two pages reconcile cleanly: a custom op can *program a window* at
> the PSUM SoC base (no on-core guard, §2.2), but keystone-fact #600 is that **PSUM
> has no AXI aperture / no NX window** — the data is unreachable by a Q7 AXI cycle
> regardless of the window the op programs. The `[HIGH·OBSERVED]` keystone is about
> the **AXI/compute datapath**; the dynamic-window note is a `[MED]` *address*-reach
> that does not terminate on a PSUM slave. Treat PSUM as **NONE via AXI** in the
> capability table; the PE array is the only writer. `[HIGH · OBSERVED]`

---

## 3. The CSR / BAR0 surface gated by the fabric remapper

`HIGH/OBSERVED` — full byte-exact register reference in
[`../csr/remapper.md`](../csr/remapper.md); perimeter framing in
[`soc-fabric-perimeter.md`](soc-fabric-perimeter.md). Below `neuron_translate`,
every AXI transaction a Q7 core issues crosses the **FIS `sprot` remapper** — a
per-master egress firewall that matches `{masked 58-bit address + optional 10-bit
AXI master-ID}`, allows/denies reads and writes independently, optionally remaps,
and (privileged variant only) stamps `AxPROT`. The asymmetry between the two
variants **is** the compute-vs-management isolation primitive:

| primitive | privileged `amzn_remapper` | guest `user_remapper` |
| :--- | :--- | :--- |
| CAM miss policy | **fail-CLOSED** `pass_on_miss = 0x0` | **fail-OPEN** `pass_on_miss = 0x1` |
| empty-entry ID match | strict (`id_cmp_dis = 0x0`) | lax (`id_cmp_dis = 0x1`) |
| `AxPROT` generation | **YES** (`arprot`/`awprot = 0x2`) | **NONE** — guest cannot forge it |
| management over peer CAM | wipe + bypass-ID | n/a |

> **WALL — the compute Q7 is the unprivileged plane.** A custom-op core's traffic
> is **guest-domain**: it crosses the `user_remapper` (fail-OPEN) for the regions
> firmware has left open, and is **denied** by the `amzn_remapper` (fail-CLOSED) for
> anything in the privileged firmware / **"Pacific"** management region that
> firmware has not whitelisted. The op **cannot acquire `AxPROT`** (the guest
> variant has no `master_prot` register) — it physically cannot claim
> privileged/secure attributes on the fabric. So even with an arbitrary
> `neuron_translate` window, a custom op reaches only what the remapper admits.
> `[HIGH · OBSERVED]`

> **CORRECTION (carried) — the gate is address-region + 10-bit AXI master-ID, NOT
> VMID.** A full-file scan of both schemas for `vmid|secure|privileged|domain`
> returns zero VMID gate. VMID is assigned **upstream** in the DMA engine
> (`udma_gen_ex` VMPR/VMADDR, per queue) before a transaction reaches the fabric
> edge. The two are disjoint, complementary virtualization layers — VMID = which
> guest VM owns the buffer; AXI master-ID = which hardware master issued the bus
> cycle. A reimplementer must keep them distinct. `[HIGH · OBSERVED]`

A denied transaction is *decided* by the remapper (latched into
`addr_denied_{lo,hi}`, counted, raises `fis_sprot_intr[0]`) and *answered* by the
QoS NTS responder with `SLVERR`/`DECERR` + `0xDEADBEEF` poison — so a custom op
that reaches past its allowed surface gets a poisoned read, not silent success.

---

## 4. MPU-enforced on-core code/data boundaries

`HIGH/OBSERVED` — **re-disassembled this session** from the SEQ boot image
(`img_SUNDA_NX_POOL_DEBUG_IRAM` in `libnrtucode.a`, shipped `ncore2gp`
`xtensa-elf-objdump`). The Vision-Q7 core is **XEA3** with **no MMU/TLB**
(`XCHAL_HAVE_TLBS = 0`, `XCHAL_HAVE_PTP_MMU = 0`) but a real Xtensa **MPU**
(region-based protection). The MPU config, byte-exact from `core-isa.h`:

| param | value | meaning |
| :--- | :--- | :--- |
| `XCHAL_HAVE_MPU` | `1` | the MPU exists |
| `XCHAL_MPU_ENTRIES` | **`16`** | foreground region descriptors |
| `XCHAL_MPU_BACKGROUND_ENTRIES` | **`2`** | background regions (default map) |
| `XCHAL_MPU_ALIGN` | `4096` | 4 KiB region alignment |
| `XCHAL_MPU_LOCK` | `0` | **no lock** — regions are reprogrammable |
| `XCHAL_HAVE_CACHEATTR` | **`0`** | **no legacy CACHEATTR — the MPU is the *only* attribute mechanism** |

The ISA config (`libisa-core.so`) carries the matching register/opcode set:

| symbol | role |
| :--- | :--- |
| `MPUCFG` / `wsr.mpucfg` / `rsr.mpucfg` | MPU global config SR |
| `MPUENB` / `wsr.mpuenb` / `rsr.mpuenb` | MPU **enable** SR (per-entry enable bitmap) |
| `WPTLB` / `RPTLB` / `PPTLB` | **write / read / probe** an MPU entry (the foreground map) |
| `MPUNUMENTRIES` | foreground-entry count (= `XCHAL_MPU_ENTRIES = 16`) |

### 4.1 The boot MPU bring-up (byte-exact)

The SEQ `_start` programs the MPU between `MEMCTL` and re-enable — matching the
XEA3 page's "`MEMCTL`/`WindowBase`/MPU bring-up" note. Disassembly (firmware-image
relative addresses):

```text
00ae  const16 a2, 0
00b1  wsr.vecbase a2            ; vector base <- 0  (vectors at NX 0x0)
00d4  const16 a2, 0xff08
00d7  wsr.memctl a2            ; MEMCTL (cache/region enables)
00fe  movi.n  a9, 0
0100  wsr.mpuenb a9            ; MPUENB <- 0  -- DISABLE MPU during reconfig
0103  movi.n  a10, 0          ; entry index = 0
0105  movi.n  a11, 15         ; entry count limit = 15
0110  memw
0113  wptlb   a10, a9         ; clear entry[a10]  (a9 = 0 attr)
0116  addi.n  a10, a10, 1
0118  bltu    a10, a11, 0x110 ; loop entries 0..14  (clear the foreground map)
 ...  second pass: const16 a2,0x4444 -> compute per-region {addr|attr} from a table
0181  wptlb   a5, a7          ; install a real region descriptor
0184  bnez    a6, 0x169       ; loop installing the live regions
0187  rsr.memctl a2
0193  wsr.memctl a2           ; OR-in bit 3  -- RE-ENABLE protection
```

```c
/* MPU bring-up — reconstructed from the byte-exact disasm above.
 * Xtensa MPU: a foreground array of {vstart, attr} region descriptors.
 * wptlb writes entry; the 'at' register encodes vStartAddress|valid, the
 * 'as' register the access rights/memtype.  [HIGH on opcodes/order; the
 * attr bit layout below is the Xtensa-MPU spec form · MED INFERRED]          */
#define SR_MPUENB  /* mpuenb */  0   /* enable bitmap; 0 = MPU off              */
#define MPU_N      16             /* XCHAL_MPU_ENTRIES (clear loop runs 0..14)  */

typedef struct { uint32_t vstart_valid; uint32_t attr_acc; } mpu_entry_t;

void mpu_bringup(const mpu_entry_t *map, unsigned n) {
    wsr_memctl(0xff08u);              /* cache/region master enables            */
    wsr_mpuenb(0);                    /* DISABLE before touching entries         */
    for (unsigned i = 0; i < MPU_N - 1; i++) {  /* clear the whole map first    */
        __asm__ volatile("memw");
        wptlb(/*at=*/i, /*as=*/0);    /* invalidate entry i                     */
    }
    for (unsigned i = 0; i < n; i++) {           /* install live regions        */
        __asm__ volatile("memw");
        wptlb(map[i].vstart_valid, map[i].attr_acc);
    }
    wsr_memctl(rsr_memctl() | 0x8u);  /* re-enable protection (OR bit 3)        */
}

/* The on-core boundary check the MPU enforces on every fetch/load/store:
 * find the foreground region covering the address; deny if the access class
 * (X for fetch, R/W for data) is not granted by that region's attr.          */
static bool mpu_permit(const mpu_entry_t *map, unsigned n,
                       uint32_t addr, int want /* X|R|W */) {
    for (unsigned i = 0; i < n; i++) {
        uint32_t base = map[i].vstart_valid & ~0x1Fu;     /* region start       */
        uint32_t end  = (i + 1 < n) ? (map[i+1].vstart_valid & ~0x1Fu)
                                     : 0xFFFFFFFFu;        /* up to next start   */
        if (addr >= base && addr < end)
            return (map[i].attr_acc & want) != 0;          /* class granted?     */
    }
    return false;   /* no covering region -> InstFetchProhibited/LoadStoreError */
}
```

### 4.2 What the MPU partitions on this core

`HIGH/OBSERVED` (the 16 regions exist and are programmed at reset) `· MED/INFERRED`
(the exact per-region attr bytes — the second-pass table is computed at boot, not a
static literal). Because `XCHAL_HAVE_CACHEATTR = 0`, the MPU is the **sole**
memory-attribute mechanism: per-region access rights (R/W/X) *and* cacheability are
both carried in the MPU descriptor. The boot path derives the regions either from a
linked `__xt_mpu_init_table` (`{attr, vaddr}` pairs) or — the customop default —
from the 8-nibble `_memmap_cacheattr_reset` word `0x44414444` (nibble `1` =
writeback-cached, `4` = bypass/uncached) mapped through the `_xtos_mpu_attribs`
lookup. The boundaries it draws
([`../address/lsp-sram-window-map.md`](../address/lsp-sram-window-map.md)):

- **IRAM / vectors** `[0x0, 0x1000)` (region `[0,0x80000000)` half) — **execute**,
  uncached (nibble `4`).
- **per-core dataram** `[0x80000, 0x90000)` — **read/write, no-execute** data.
- **device-trap holes** `[0x10000, 0x80000)` and `[0x90000, 0x100000)` —
  **MPU-walled** (no local RAM; a fetch/deref there faults). These bracket the
  64-KiB dataram and are the on-core correlate of "nothing reachable between the
  IRAM region and the register block."
- the **pinned/dynamic window apertures** (`0x80000000`, `0x84000000`,
  `0x07/09/0a000000`) — the hbm_scratch window is **writeback-cached** (cacheattr
  region `r4 = 1`) and holds the customop's *linked code+data*, so the MPU grants
  execute there; the SBUF and dynamic-tensor windows are **data-only**.

> **GOTCHA — the MPU is an on-core wall; the remapper is the off-core wall. They are
> orthogonal.** The MPU enforces *which class* (X vs R/W) and *which cacheability* is
> legal *at a 32-bit NX address on this core*; the remapper enforces *whether the
> resulting 58-bit AXI transaction is admitted to the fabric*. A reimplementer must
> build **both** — the MPU does not see SoC addresses, the remapper does not see NX
> class. `[HIGH · OBSERVED — two distinct enforcement points re-verified.]`

### 4.3 PRID — the per-core identity the SPMD model rides on

`HIGH/OBSERVED`. Core identity is **PRID** (`PRID = SR 235 = 0xEB`), with the
core-id field `PRID_ID_MASK = 0xF` / shift 0 / 4 bits (`PRID[3:0]`; 8 cores use 3
of the 4 field bits). Two observed read sites:

```text
;; device get_cpu_id() static ctor — raw PRID, NO mask/shift:
0021  rsr.prid a3              ; cpu_id = raw PRID value
0024  s32i.n   a3, a2, 0       ; cached; the dense core index IS PRID[3:0]

;; SEQ boot worker-branch — masks the id field explicitly:
1bb1  rsr.prid a5
1bb4  extui    a5, a5, 0, 4    ; core_id = PRID[3:0]  (0 = SEQ/primary)
1bb7  beqz.n   a5, 0x1bc1      ; branch the primary core away from the workers
```

> **CORRECTION (carried) — `get_cpu_id()` reads RAW PRID, not a MISC register.** The
> SPMD ABI page overturns prose that called core-id a "MISC-register read"
> (`MISC_REG_0/_1` = SR 244/245 are never read by the lib). `get_cpu_id()` returns
> the raw `rsr.prid` value with no `extui`/`and` — integration wiring pre-places the
> dense `[0..7]` index in `PRID[3:0]`. The firmware's `extui …,0,4` is the *same*
> field, masked where it must distinguish the primary core. `[HIGH · OBSERVED config;
> the dense-index pre-placement is INFERRED.]`

A 4-bit id field covers the SEQ + 8 compute cores. This hardware identity (a)
PRID-rebases the per-core dataram (§2.4), (b) selects the per-core
`run_state`/`intr_info` CSR slot
([`../address/tpb-pool.md`](../address/tpb-pool.md) §4), and (c) lets one SPMD image
branch per-core without an address it controls — the basis of §5.2's inter-core
isolation.

---

## 5. Isolation guarantees

### 5.1 Between co-located custom ops (same core, time-shared) — **software only**

`HIGH/OBSERVED (by absence)`. Two custom ops that run on the same Q7 core at
different times **share the same 32-bit NX map, the same MPU foreground regions,
and the same `neuron_translate` context**. There is **no hardware reset of the TLB
or MPU between ops** in the customop path; the dynamic windows persist their last
contents (the transient-reference hazard is per-op, not cleared at op boundary).
Isolation between co-located ops is therefore a **software contract** — the runtime
must scrub or re-stage state. A reimplementer who assumes hardware op-to-op
isolation on one core is wrong. `[HIGH · OBSERVED — no clearing instruction at the
op boundary in the disassembled path.]`

### 5.2 Between the 8 SPMD cores — **PRID-rebased private state**

`HIGH/OBSERVED`. The eight Q7 cores run the **same SPMD image** but are isolated by
construction at the address level:

- **Private IRAM**: each `CORE{i}_IRAM` is a distinct SoC leaf (`0x2803100000 +
  i*0x100000`); a core fetches only its own.
- **Private DRAM**: each `CORE{i}_DRAM` is a distinct leaf, and the shared NX
  pointer `[0x80000,0x90000)` is **PRID-rebased** to the issuing core's bank (§2.4)
  — core *i* cannot name core *j*'s dataram by pointer.
- **Shared reach**: cores deliberately *share* SBUF, HBM, and EVT_SEM (the pinned +
  dynamic windows reach the same SoC regions from every core) — that is the SPMD
  data plane, and cross-core coordination is **software** through EVT_SEM, not
  hardware-prevented.

> **NOTE — the 8 cores are fully independent within the customop library.** A sweep
> of all `libnrtucode.a` members finds **zero** `l32ex`/`s32ex` atomics and **zero**
> barrier/semaphore/spinlock device symbols (only no-op libc++ mutex shims). The
> hardware corroborates: `ncore2gp` is `numOfCores = 1`, `MPCoherencySupport = 0` —
> **no MP cache coherency**. Each core owns a private heap (`xmem` in dataram), so
> there is no shared structure to race. Cross-core coordination, where it exists,
> lives at a higher layer (the EVT_SEM array / the management core), not in the
> custom-op ABI. `[HIGH · OBSERVED — negative sweep + ISA config.]`

> **NOTE — SPMD isolation is "private code+private scratch, shared tensors."** The
> 1-MiB-pitch per-core IRAM/DRAM gives each core a private execution context; the
> windows give all cores a *common* view of SBUF/HBM for cooperative compute. There
> is no MPU/remapper wall *between* the 8 cores' shared-tensor accesses — the
> isolation is the private banks, not the shared windows. `[HIGH · OBSERVED]`

### 5.3 Between co-tenant fractional-NeuronCore workloads — **VMID + remapper**

`HIGH/OBSERVED (layering)` `· MED/INFERRED (per-tenant detail)`. When a NeuronCore
is fractionally shared between tenants, a custom op of tenant A must not reach
tenant B's SBUF/HBM partition. Two stacked enforcement layers, neither of which the
op controls:

1. **Upstream DMA-engine VMID** — `udma_gen_ex` assigns a VMID/VMADDR **per queue**
   before any transaction reaches the fabric edge; a tenant's queues carry the
   tenant's VMID, scoping the buffer it can name.
2. **Fabric remapper master-ID** — the `user_remapper` CAM matches the **10-bit AXI
   master-ID**; a tenant's master-ID is admitted only for its whitelisted regions,
   and the privileged `amzn_remapper` (fail-CLOSED) denies anything outside.

A custom op can *program a window* at another tenant's SoC base via
`neuron_translate` (no on-core guard, §2.2), but the resulting AXI transaction is
**denied at the fabric edge** by VMID + master-ID + region — answered with
`SLVERR`/`0xDEADBEEF`. So cross-tenant isolation is real but lives **below** the
custom op, not in the op's address space. `[HIGH · the two-layer model; MED · the
exact per-tenant VMID-to-partition binding is the runtime's, not register-traced
here.]`

---

## 6. Self-verification — the five strongest reachability claims

Re-challenged this session against the named artifacts (single-file, never a
folder grep). All five pass.

| # | claim | check against | result |
| :-- | :--- | :--- | :--- |
| 1 | PSUM has no pinned window; the two pins are SBUF + hbm_scratch only | `neuron_translate` rec3/rec4 tags (`../address/soc-q7-translation-windows.md` §4a); `tpb-pool.md` §1 PSUM = "PE-array accumulator" | ✅ no PSUM pin; PSUM is PE-private |
| 2 | The MPU is real (16 fg + 2 bg) and programmed at boot (`wsr.mpuenb` + `wptlb` loop) | `core-isa.h` `XCHAL_MPU_ENTRIES=16`; `img_SUNDA_NX_POOL_DEBUG_IRAM` disasm `@0x100/0x113…` (this session) | ✅ `wsr.mpuenb a9` @0x100, `wptlb` loop @0x110–0x118; 16+2 |
| 3 | Per-core dataram is PRID-rebased; core id = `PRID[3:0]` (`PRID = SR 0xEB`) | firmware `rsr.prid a5; extui a5,a5,0,4` @0x1bb1 (this session); `PRID_ID_MASK 0xF`; `tpb-pool.md` §3 per-core DRAM leaves | ✅ 4-bit core id; 8 private DRAM leaves |
| 4 | Compute Q7 is guest-domain: cannot forge `AxPROT`, denied in `amzn` region | `remapper.md` §4.2 (`user` has no `master_prot`); `soc-fabric-perimeter.md` §1 | ✅ guest has no AxPROT; `amzn` fail-CLOSED |
| 5 | `neuron_translate` has no on-core bounds/null guard | `soc-q7-translation-windows.md` §10; the disasm (no compare in MISS) | ✅ guard is the off-core remapper |

> **The strongest CORRECTION on this page** is row 1's consequence: the sibling
> translate page lists PSUM under the dynamic-window route with a `MED` caveat,
> which a reader can mistake for "PSUM is reachable through a dynamic window." The
> capability table here resolves it to **NONE via AXI** — a custom op can form a
> PSUM *address* but there is **no AXI slave path** to the PE-array's private
> accumulator; PSUM data is written only by the PE array. Reachability of the
> address ≠ reachability of the data.

---

## 7. Reimplementation checklist `[summary]`

- **Two coordinate walls, both required.** *On-core*: an **MPU** (no MMU) — clear
  the foreground map (`wsr.mpuenb 0`, `wptlb` loop), install IRAM=execute /
  dataram=R/W-noexec / windows=data (hbm_scratch=execute), re-enable
  (`memctl |= 8`). *Off-core*: the **`amzn`/`user` remapper** — never invert the
  fail-CLOSED/fail-OPEN split; only the privileged variant emits `AxPROT`.
- **`neuron_translate` is the reach engine, not a guard.** 5-region TLB (3 dyn 16
  MiB `%3` + 2 pin 64 MiB); a miss reprograms `MEM_WINDOW3/5/6`; a 4th distinct
  16-MiB region evicts. It forms any pointer — the remapper decides legality.
- **PSUM is unreachable by AXI** — PE-array-private, no slave port; no pinned
  window. **SBUF** is the pinned 64-MiB window over a 32-MiB AXI aperture (upper
  half is pad).
- **SPMD isolation = private IRAM/DRAM (PRID-rebased) + shared SBUF/HBM windows.**
  `core_id = PRID[3:0]`. No HW wall between the 8 cores' *shared-tensor* accesses;
  no HW reset of TLB/MPU between co-located ops on one core (software scrub).
- **Cross-tenant isolation lives below the op** — upstream DMA-engine **VMID** +
  fabric **10-bit master-ID** + region; a denied reach returns `SLVERR`/`0xDEADBEEF`.
- **v5 / MAVERICK** — the window-TCAM widths and the remapper reset core are
  header-OBSERVED; the v5-interior window/MPU *behaviour* beyond those widths is
  **INFERRED**. Confirm `MPUNUMENTRIES` and the PSUM port absence against the ISA
  config / PE-array datapath before hard-coding.
