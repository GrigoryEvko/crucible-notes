# MARIANA_PLUS × POOL image (dual-core)

This page diffs the **MARIANA_PLUS (v4+) × POOL** firmware image against the
committed, byte-true [MARIANA × POOL baseline](./mariana-pool.md). POOL is the
**only** one of the five NX engines that ships **two distinct firmware cores**
(`engine_idx = 2`): an NX-class SEQ sequencer (`NX_POOL`, `'S:'` dialect) and a
per-pool-core `kernel_info_table` compute engine (`Q7_POOL`, `'P%i:'` dialect),
bridged by the `0xF0` `ExtendedInst` escape. This is a **DIFF page** — it does
**not** re-derive the dual-core model, the 177-entry SEQ table, the
`kernel_info_table` back-end, or the `0xF0` reconciliation; those are owned by the
[MARIANA baseline](./mariana-pool.md) (and beneath it [CAYMAN × POOL](./cayman-pool.md)).
Read the MARIANA page for the *model*; this page records **only what changed**
across v4 → v4+, for **both** cores.

Every size, sha256, opcode, reset byte and string below reads directly from
`libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) via its 14
`MARIANA_PLUS_NX_POOL_*_get` + 32 `MARIANA_PLUS_Q7_POOL_*_get` accessors, carved
from identity-mapped `.rodata`, with the shipped Cadence Vision-Q7 `ncore2gp`
`xtensa-elf-objdump` decoding the flat blobs and `xtensa-elf-readelf` the EXTISA
ELFs.

> **The headline, up front — and POOL is the engine that PROVES it.**
> **MARIANA_PLUS POOL is functionally identical to MARIANA POOL** — the NX_POOL
> handler roster is `41 == 41` (`+0/−0`, byte-for-name), the opcode space is
> gen-stable at **177** (no growth), the Q7_POOL `kernel_info_table` is
> **byte-for-key identical** (17/1/2/9 entries, *and* the EXTISA SOs are byte-for-byte
> identical so even the `funcVA`s match), the `0xF0` bridge is intact on both cores,
> the RNG body is the unchanged MARIANA TIE+LFSR, and the per-engine PROF tables are
> **byte-identical**. The v4+-vs-v4 delta is a **recompile + the DGE reshape
> FAST-PATH** — and **POOL is the engine that hosts the fast-path**, because the
> SW-DGE backend runs device-side on the Q7/POOL cores. So this is the one v4+ page
> where the fast-path is not a string footprint *reflected in* from elsewhere — it is
> the **home** of `dge_decode_fast`, with real new code in the NX SEQ core's IRAM.
> [HIGH/OBSERVED]

This is the *expected* result. MARIANA_PLUS shares the MARIANA ISA — there is no
`neuron_mariana_plus_arch_isa` dir (the four ISA dirs are
`cayman`/`mariana`/`maverick`/`sunda`); MARIANA_PLUS carries only its own
`arch-headers/mariana_plus/` *register-map* dir. The "v4+" label is a register-map
refresh + a recompile + the DGE optimization, **not** a model or ISA change, and
POOL absorbs it exactly as [ACT](./mariana-plus-act.md), [DVE](./mariana-plus-dve.md)
and [PE](./mariana-plus-pe.md) do — with the fast-path *concentrated* on the NX SEQ
core where DGE decode lives. [HIGH/OBSERVED]

Confidence/evidence tags follow the project
[Confidence & Walls Model](../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

Related pages: [MARIANA × POOL (the diff base)](./mariana-pool.md) ·
[MARIANA_PLUS × ACT (the v4+ template)](./mariana-plus-act.md) ·
[MARIANA+ generation delta](../generations/mariana-plus-delta.md) ·
[DGE Reshape Engine](../firmware/dge/dge-reshape.md) ·
[Legacy DMA / iDMA](../firmware/dge/idma-legacy-dma.md) ·
[kernel_info_table Layout](../firmware/pool/kernel-info-table.md) ·
[Firmware-Image Accessor Index](./image-catalog-index.md).

---

## 1. The cross-gen delta table (MARIANA → MARIANA_PLUS, per core)

The whole page in one table. `(==)` marks invariant rows; the **bold** rows are
the *only* real changes — both of them live on the NX SEQ core, and both are the
DGE fast-path. Read the [MARIANA page](./mariana-pool.md) for the dual-dispatch
*model*; this table leads with the null functional delta.

| AXIS | `NX_POOL` (SEQ) | `Q7_POOL` (compute) | Δ |
|---|---|---|---|
| getters (`nm` / IDA sidecar) | 14 / 14 | 32 / 32 | `(==)` shape |
| real / empty images | (NX share of) 28 real + 18 zero-size cursors | (Q7 share of) same | `(==)` |
| reset vector | `06 7d 00 00` → `j 0x1f8` | `06 7f 00 00` → `j 0x200` | **`(==)` SAME +0x1c (NX) / UNCHANGED (Q7)** |
| boot path | `const16 a0,0x90 ; jx a0` → `enter_run@0x90` | same → `enter_run@0x90` | `(==)` byte-identical |
| dispatch form | `addi a2,a2,-65` + `movi a3,177` (base `0x41`) | `kernel_info_table` linear scan | `(==)` |
| opcode space | 177 entries (54 real / 123 default) | 17/1/2/9 KIT entries | **`(==)` NO growth** |
| **HANDLER / KEY SET** | **41 handlers, `+0/−0` byte-for-name** | **`(opcode,spec)` keys byte-for-key + funcVA byte-identical** | **`(==)`** |
| `0xF0` bridge | slot[175] `0x306d`→`0x30e2` (real, ≠ default; `+0x75`) | five `0xf0+spec{0,1,2,4,3}` rows | `(==)` intact |
| RNG body | (no RNG kernel; SEQ handlers only) | `Xorwow(TIE)` + `LFSR` — **RETAINED from v4** | `(==)` carried |
| **KERNEL CODE** | **`+DGE fast-path` (`dge_decode_fast` +3 helpers, REGWRITE retired)** | **`+tensor_reshape_transpose_sb2sb`** (the one Q7 ripple) | **new code** |
| **PROF** | **`0951b326`/`534f2239` — byte-IDENTICAL, disarmed** | (no PROF) | **`(==)`** |
| dtype strings | `{UINT32,INT32,FP32}` only | MX dequant (`proc_4bit_mx`/`cptc`) — byte-identical | `(==)` |
| DKL | (n/a) | `DKL_PERF == DKL_TEST == MARIANA DKL_PERF`; "Cayman" string kept | `(==)` |
| source tree | `…/cayman/seq/src/…` `+ dge_decode_fast.cpp` | `dge_reshape_apply_impl.hpp` etc. | `(==)` dir name |
| engine self-name | `S: BEGIN on mariana` → `S: BEGIN on mariana_plus` | `Starting pooling engine` | **rename (NX)** |
| **IRAM size** | **GREW** (PERF `+0x36a0`, TEST `+0x3460`, DEBUG `+0x12c0`) | release UNCHANGED; DEBUG/DKL_DEBUG `+0x1c0` | **NX grew** |

**The two cores absorb the generation asymmetrically.** The NX SEQ core got the DGE
fast-path + recompile in **every** build variant; the Q7 compute core's *release*
path (PERF / TEST / DKL_PERF + the EXTISA kernel ELFs + the RNG body) is **byte-for-byte
identical to MARIANA**, with a single small ripple — the `tensor_reshape_transpose_sb2sb`
reshape *kind* — visible only in the DEBUG/symbol-bearing builds. A
MARIANA ↔ MARIANA_PLUS POOL swap is **a NX-core recompile + the DGE fast-path
(+ REGWRITE retire) + the Q7 `sb2sb` kind + the `mariana_plus` self-name**, not a
model change. [HIGH/OBSERVED]

> **NOTE — the catalog's "byte-identical POOL ucode" claim, qualified.** The
> [image catalog](./image-catalog-index.md) records MARIANA and MARIANA_PLUS POOL
> ucode as byte-identical. This diff shows that holds **only for the Q7 release path**
> (PERF/TEST + DKL_PERF + the EXTISA SOs + the PROF tables): 10 of the 20 carved image
> pairs are byte-identical, 10 differ. The NX SEQ core differs in every variant (the
> fast-path), and the DEBUG-class builds differ on Q7 (the `sb2sb` kind). The
> [MARIANA × POOL §9 footnote](./mariana-pool.md) that forward-referenced this page as
> "reported byte-identical ucode" is therefore *refined*, not contradicted: the Q7
> *compute kernels* are byte-identical; the NX *sequencer* is not. [HIGH/OBSERVED]

---

## 2. THE HEADLINE — the DGE reshape FAST-PATH (the v4+ functional delta)

POOL is the architecturally correct home for the fast-path. The shipped ISA header
`aws_neuron_isa_tpb_dma_gather_xpose.h` states that `DmaGatherTranspose` "uses the
SW-DGE backend with **Q7 processors in the Gpsimd engine**" — so the descriptor
generator that lowers and executes a reshape/transpose runs **on the Q7/POOL cores**,
and the DGE *decode front-end* lives on the NX SEQ core. That is exactly where the
fast-path landed. (See [DGE Reshape Engine](../firmware/dge/dge-reshape.md) for the
descriptor structs and the analyze→emit pipeline this fast-path short-circuits, and
[Legacy DMA / iDMA](../firmware/dge/idma-legacy-dma.md) for the descriptor-ring credit
flow `wait_for_credit` throttles against.)

### 2.1 The presence map — 4 strings present-vs-absent (byte-grounded)

`comm -13` over both DEBUG DRAMs, then re-grepped `rg -a -c` this session:
[HIGH/OBSERVED]

| String (NX_POOL DEBUG DRAM) | MARIANA | MARIANA_PLUS | kind |
|---|:---:|:---:|---|
| `dge_decode_fast` (`.cpp`) | **0** | **1** | new fast-path TU `__FILE__` |
| `dge_reshape_memcopy_transpose_fast` | **0** | **1** | fused memcopy+transpose `__func__` |
| `tensor_reshape_transpose_sb2sb` | **0** | **1** | new SB→SB transpose reshape kind |
| `wait_for_credit` | **0** | **1** | DMA descriptor-ring credit wait |
| `push REGWRITE` | **1** | **0** | **RETIRED** (folded into the fast-path emit) |
| `dge_reshape.cpp` / `analyze_tensor_reshape` / `Setting up DGE` | 1 | 1 | the pre-existing DGE machinery (both gens) |

The four fast-path names are **real compiled symbols, not stray text** — they also
surface in the symbol-bearing MARIANA_PLUS NX_POOL **TEST** DRAM (`dge_decode_fast`
× 1, re-confirmed this session), so they correspond to compiled code, not debug-log
artifacts. The Q7 side carries exactly one ripple: `tensor_reshape_transpose_sb2sb`
(MPLUS = 1, MARIANA = 0); `gen_spray_info` (× 6) and `make_gather_pattern` (× 2) are
unchanged on both — the DGE *decode* front-end is NX-only, the Q7 side just gains the
SB-to-SB reshape *kind*. This is byte-for-byte the per-gen presence map of
[DGE Reshape §6.1](../firmware/dge/dge-reshape.md). [HIGH/OBSERVED]

### 2.2 The fast-path, as annotated C (the new NX_POOL code body)

The fast-path strings are byte-adjacent in the MARIANA_PLUS NX DRAM
(`dge_reshape_memcopy_transpose_fast\0dge_decode_fast.cpp\0`), so
`dge_reshape_memcopy_transpose_fast` is the `__func__` and `dge_decode_fast.cpp` its
`__FILE__` — the fast path is integrated into the DGE decode/setup translation unit.
The C below models the *new* control flow it adds, reconstructed from the names + the
shared DGE struct/format-string corpus ([DGE Reshape §3–§6](../firmware/dge/dge-reshape.md))
and corroborated by the structural signature in §2.3. The general
analyze→pair-assess→per-tile-emit path (the slow path, present on both gens) is
unchanged; the fast path *replaces* it for the contiguous/tiled SB-to-SB case.

```c
/* MARIANA_PLUS NX_POOL — dge_decode_fast.cpp : dge_reshape_memcopy_transpose_fast()
 * The v4+ functional delta. POOL is the fast-path's home engine: the SW-DGE backend
 * runs on the Q7/POOL cores (gather_xpose.h), and this NX SEQ decode front-end emits
 * its descriptors. NEW on MARIANA_PLUS; absent on MARIANA (which used the generic
 * analyze_tensor_reshape -> pair-assess -> per-tile-emit -> "push REGWRITE" path).   */

bool dge_reshape_memcopy_transpose_fast(const dge_reshape_state *src,   /* {num[4],step[4],elem_size} */
                                        const dge_reshape_state *dst,
                                        dma_ring_t *ring)
{
    /* Fast-path GUARD: only the contiguous/tiled SB->SB transpose case qualifies.
     * Anything outside it (cast, non-Y gather_dim, HBM round-trip, irregular stride)
     * falls back to the SHARED slow path (analyze_tensor_reshape, both gens).         */
    if (!is_sb2sb_transpose(src, dst))            /* the new tensor_reshape_transpose_sb2sb kind */
        return false;                             /* -> caller runs analyze_tensor_reshape()      */

    /* FUSE the memcopy + transpose into ONE descriptor-gen pass. The generic path
     * walked analyze -> pair-assess -> per-tile GATHER_XPOSE emit; the fast path
     * inlines the spray (gen_spray_info) + gather-pattern (make_gather_pattern) +
     * emit into one body — the inline consolidation seen as IRAM-grew/fewer-entries. */
    for (int tile = 0; tile < num_xbar_tiles(src); tile++) {   /* 16x128 xbar tiles, 2B dtype */

        /* Explicit DMA flow control: block until the descriptor ring has CREDIT
         * before pushing more BDs. NEW primitive on v4+; the higher descriptor-issue
         * rate of the fused path would otherwise overrun the ring depth.              */
        wait_for_credit(ring);                    /* NEW on MARIANA_PLUS */

        /* Build + push the GATHER_XPOSE descriptor for this tile-group directly.
         * The DMA/xbar hardware performs the actual 16x128-tile transpose during the
         * transfer (descriptor-level transpose, not a DVE StreamTranspose).           */
        emit_gather_xpose_sb2sb(ring, src, dst, tile);

        /* NOTE: NO separate "push REGWRITE to DMA[%d]" step here — the DMA trigger /
         * register program the generic emit did via REGWRITE is folded into this
         * fused emit. push REGWRITE is RETIRED on MARIANA_PLUS (present on MARIANA).   */
    }
    return true;                                  /* fast path consumed the request */
}
```

The fusion, the SB-to-SB kind, the explicit credit-wait, and the REGWRITE retirement
are all OBSERVED at the string/symbol level; the per-instruction bodies sit in
FLIX-desynced IRAM spans and are not byte-recoverable, so the control-flow *shape* is
INFERRED-HIGH from the names + the shared DGE corpus. [strings OBSERVED; fusion +
credit-wait semantics INFERRED-HIGH — see [DGE Reshape §6.4](../firmware/dge/dge-reshape.md)]

### 2.3 Structural corroboration — IRAM grew, function entries fell

Re-disassembled this session with the native `ncore2gp` `xtensa-elf-objdump` (exit 0),
counting `entry` prologues (`rg -c '\tentry\t'`): [HIGH/OBSERVED]

| NX_POOL DEBUG IRAM | size (B) | sha256[:16] | `entry` prologues |
|---|---:|---|---:|
| MARIANA (v4) | 114 816 (`0x1c080`) | `41b6c798bff3cd9f` | **697** |
| MARIANA_PLUS (v4+) | 119 616 (`0x1d340`, **`+0x12c0`**) | `9b514bb6d45a7363` | **675** |

A **larger IRAM yet fewer function entries** is the signature of inlining the spray +
gather-pattern + emit into one larger function — exactly the consolidation §2.2 models
and the REGWRITE retirement implies. This reproduces the
[DGE Reshape §6.6](../firmware/dge/dge-reshape.md) finding on the same image. The first
MARIANA_PLUS-vs-MARIANA NX DEBUG IRAM divergence is at byte `0xa3` — the reset + boot
trampoline + dispatch region is byte-identical, and the code only diverges at the
first relocated literal, the same recompile signature the
[ACT page §3](./mariana-plus-act.md) found. [HIGH/OBSERVED counts; inline-consolidation
reading INFERRED-HIGH]

---

## 3. The dual-core inventory + carve (46 getters, the SAME shape as MARIANA)

`nm libnrtucode_internal.so | rg -c 'MARIANA_PLUS_(NX|Q7)_POOL_.*_get$'` = **46**:
**14 `NX_POOL`** + **32 `Q7_POOL`**, the identical split to MARIANA POOL (28 real + 18
zero-size boundary cursors). Each getter is the canonical 4-instruction `(img-ptr,
size)` stub: `lea <blob>(%rip),%rax ; mov %rax,(%rdi) ; movq $<size>,(%rsi) ; ret`;
all 46 `(img-ptr, size)` pairs parse instruction-exact and match the
[image-catalog index](./image-catalog-index.md). [HIGH/OBSERVED]

### 3a. `NX_POOL` (CLS = NX) — 14 getters (the SEQ sequencer)

| VARIANT | REGION | IMG-PTR (.rodata = file off) | SIZE | sha256[:16] / STATUS |
|---|---|---|---|---|
| PERF | IRAM | `0x5f0fe0` | `0x17bc0` | `582c246b197c193b` (SEQ + **DGE fast-path**) |
| PERF | DRAM | `0x608ba0` | `0x032a0` | REAL (SEQ data) |
| TEST | IRAM | `0x676ee0` | `0x176a0` | `bf84aacb…` (+ fast-path symbols) |
| TEST | DRAM | `0x68e580` | `0x03620` | `9c605daf269edc2e` (fast-path syms) |
| **DEBUG** | **IRAM** | `0x714700` | `0x1d340` | `9b514bb6d45a7363` (code + `S:` logs + fast-path) |
| **DEBUG** | **DRAM** | `0x731a40` | `0x07160` | `d2e1552a13f1efe3` (data + `S:` logs) |
| **PROF** | **CAM** | `0x86ef00` | `0x00400` | `0951b326f4a40ccd` (**disarmed; == MARIANA**) |
| **PROF** | **TABLE** | `0x86f300` | `0x02000` | `534f2239b9e76d1c` (**== MARIANA**) |

(The 6 SRAM/EXTRAM rows are zero-size boundary cursors — POOL runs from IRAM+DRAM.)

### 3b. `Q7_POOL` (CLS = Q7) — 32 getters (compute + DKL + EXTISA)

| VARIANT | REGION | IMG-PTR | SIZE | sha256[:16] / STATUS |
|---|---|---|---|---|
| PERF | IRAM | `0x7595e0` | `0x164e0` | `0c761dba81477508` (**== MARIANA**) |
| PERF | DRAM | `0x76fac0` | `0x13180` | `c448c5ff…` (**== MARIANA**) |
| TEST | IRAM/DRAM | `0x782c40` / `0x79aac0` | `0x17e80`/`0x13480` | `e8a32b3f`/`6271b2d0` (**== MARIANA**) |
| **DEBUG** | **IRAM** | `0x7adf40` | `0x1ef00` | `1c9c15bcc91b381b` (code; `P%i:`; **+`sb2sb`**) |
| **DEBUG** | **DRAM** | `0x7cce40` | `0x15d80` | `295fae9c1cdb07f8` (data; `P%i:`; **+`sb2sb`**) |
| DKL_PERF | IRAM/DRAM | `0x7e2bc0` / `0x7f2d80` | `0x101c0`/`0x13c80` | `8a8c927a`/`0aaa01a7` (**== MARIANA**) |
| **DKL_DEBUG** | **IRAM** | `0x806a00` | `0x14300` | `47332835…` (**+`sb2sb`**) |
| **DKL_DEBUG** | **DRAM** | `0x81ad00` | `0x16700` | `fb6ff81e799d2798` (**+`sb2sb`**) |
| DKL_TEST | IRAM/DRAM | `0x831400` / `0x8415c0` | `0x101c0`/`0x13c80` | **== DKL_PERF** (§8) |
| PERF_EXTISA_0 | SO | `0x855240` | `0x0a260` | `9f2ce049608c0a88` (17-entry KIT; **== MARIANA**) |
| PERF_EXTISA_1 | SO | `0x85f4c0` | `0x00f5c` | REAL (1-entry KIT) |
| PERF_EXTISA_2 | SO | `0x860440` | `0x01500` | REAL (2-entry KIT) |
| PERF_EXTISA_3 | SO | `0x861960` | `0x06974` | `8477ff2690f30cc3` (9-entry; cptc; **== MARIANA**) |
| PERF_EXTISA_{0–3} | JSON | (4 blobs) | `0x20` | REAL (dummy; all 4 identical) |

28 real carves; 12/12 spot-reconciled byte-identical (sha256 + `cmp -s`) to the
matching `libnrtucode.a` member `.rodata` (the archive ships exactly 46 MARIANA_PLUS
POOL members: 2 `hwdecode_` PROF + 44 `img_`). [HIGH/OBSERVED]

> **GOTCHA — anchor PROF_TABLE on the getter-resolved offset.** The `hwdecode_*_PROF_TABLE`
> archive member's `.rodata` carries 2 `R_X86_64` relocations (the wrapper's
> `.text`/`.eh_frame` fixups); a hand-typed offset (`0x871300` instead of the
> getter-resolved `0x86f300`) produces a spurious boundary mismatch. Carve at the
> `lea`-decoded `0x86f300` (`movq $0x2000`) and the `cmp -s` is clean and the hash is
> the byte-identical-to-MARIANA `534f2239`. All other 27 carves were correct on first
> read. [HIGH/OBSERVED]

> **NOTE — engine ordering confirms POOL is LAST.** The MARIANA_PLUS block is laid out
> `ACT → DVE → PE → POOL` (variant-major, engine-minor). PE PERF_DRAM ends at
> `0x5ee120 + 0x2ec0 = 0x5f0fe0` == MARIANA_PLUS `NX_POOL` PERF_IRAM start — exactly the
> contiguity cursor the [MARIANA_PLUS × PE](./mariana-plus-pe.md) carve predicted. The NX
> real blobs are contiguous up to the NX_POOL PROF tables `@0x86ef00`. [HIGH/OBSERVED]

---

## 4. The reset/boot diff — SAME +0x1c (NX-only), Q7 UNCHANGED

The byte-level signature that `NX_POOL` and `Q7_POOL` are **two separate cores**, and
that v4+ added **no further shift** to either. Both reset regions are byte-identical to
their MARIANA counterparts (`od -An -tx1 -N16`, re-read this session). [HIGH/OBSERVED]

**(A) `NX_POOL` — flat, reset SAME +0x1c (no further shift):**

```text
MARIANA      IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
MARIANA_PLUS IRAM:  06 7d 00 00 | 00 00 | 86 7e 00 00 | 00 00 | a0 71 69 80
                    └ j 0x1f8 ┘         └ j 0x204 ┘            └ shared boot stub ┘
  0x1f8: const16 a0,0 ; const16 a0,0x90 ; jx a0   -> enter_run @0x90
  0x204: halt 0
```

The `J` opcode byte-1 is `0x7d` on **both** generations — MARIANA_PLUS inherits
MARIANA's relocated boot entry (the `+0x1c` forward shift from CAYMAN's `0x76`) with
**no further shift**. DRAM head `34 cb 99 60` (header word `0x6099cb34`, the
`.globstruct` dispatcher-state magic) is byte-identical too. [HIGH/OBSERVED]

**(B) `Q7_POOL` (incl. DKL) — flat, reset UNCHANGED:**

```text
IRAM head (all variants): 06 7f 00 00 | 00 00 | 86 80 00 00 | 00 00 | a0 71 69 80
  0x000: 06 7f 00   j 0x200    ; primary reset -> boot   (== MARIANA, byte-identical)
  0x006: 86 80 00   j 0x20c    ; secondary    -> halt
  0x200: const16 a0,0 ; const16 a0,0x90 ; jx a0   -> enter_run @0x90
```

The Q7 compute core's reset vector is byte-identical across MARIANA and MARIANA_PLUS —
the `+0x1c` shift was always **NX-only**, and v4+ adds nothing. Both trampolines still
converge on `enter_run @0x90`. The first cross-gen Q7 DEBUG IRAM divergence is at byte
`0x21b` (post-boot, the `sb2sb`-kind code). [HIGH/OBSERVED]

**(C) `EXTISA_0..3` SO — real `EM_XTENSA` ELFs**, section geometry **byte-identical** to
MARIANA: `[0] .text 0x01000000 size 0x6f66`, `[6] kernel_info_table 0x02000380 size 0x88`
(17 entries), `[7] .globstruct 0x02000408`, `[8] .bss 0x02000450` — and the *whole* SO is
byte-identical cross-gen (§6). [HIGH/OBSERVED]

> **GOTCHA — the FLIX desync is a disassembler limit, not a finding.** The flat IRAM
> blobs carry no `.xt.prop` FLIX property table, so densely-scheduled vector bundles
> desync under the linear sweep. The `entry`/`retw` counts, reset-vector reads, dispatch
> sites, and string corpus are robust; per-bundle micro-op recovery inside the fast-path
> and the Q7 `sb2sb` body is MED and so flagged. [HIGH/OBSERVED for the limit]

---

## 5. The NX_POOL handler + opcode diff — STABLE (no growth)

MARIANA_PLUS `NX_POOL` uses the **same** `addi`-normalization SEQ dispatch (the DVE/POOL
form, not PE's raw-compare chain). Decoded instruction-exact at dispatch SITE A this
session (`ncore2gp`, exit 0):

```text
2d3f:  l32i     a2, a4, 212
2d42:  addi     a2, a2, -65       ; normalization base 0x41 ('A') — UNCHANGED
2d45:  movi     a3, 177           ; 177-entry bound — UNCHANGED, NO growth
2d48:  bgeu     a3, a2, 0x2d4e
2d4b:  j        0x30ea            ; default trampoline (was MARIANA 0x3075; +0x75 reloc)
2d4e:  const16  a3, 8
2d51:  const16  a3, 0x800         ; table base @DRAM 0x800 — UNCHANGED
2d54:  addx4    a2, a2, a3
2d57:  l32i.n   a2, a2, 0
2d59:  jx       a2
```

- **Normalization base `0x41` (`'A'`) — UNCHANGED** (`addi a2,a2,-65` both gens). POOL
  keeps `'A'`-based normalization; the opcode space did **not** extend downward.
- **Bound `movi a3,177` — UNCHANGED** → 177-entry table. **NO growth** — contrast PE
  (`25→29`) and DVE (`170→187`) on other engines.
- **Table base `@DRAM 0x800` — UNCHANGED.** Only the default trampoline relocated,
  `0x3075`→`0x30ea` (`+0x75`, a clean recompile shift). [HIGH/OBSERVED]

**Real-vs-default count: 54 real / 123 default on BOTH gens** (parsed from the 177-word
table this session; MPLUS default `0x30ea` × 123, MARIANA default `0x3075` × 123). Every
real slot relocated `+0x75`, but the count and pattern are invariant. [HIGH/OBSERVED]

**The handler diff (the structural claim).** Method (carried from the
[MARIANA baseline §4](./mariana-pool.md)): extract every strict end-anchored single-token
`S: <OpName>` (`rg -oP 'S: \K[A-Za-z][A-Za-z0-9_/-]*$'`), `sort -u`, `comm`-diff both
DEBUG DRAMs. The string-pool glue trap is auto-handled by the end-anchor.

> **RESULT: MARIANA_PLUS `NX_POOL` = 41 handlers; MARIANA `NX_POOL` = 41 handlers;
> ADDED = 0; REMOVED = 0; the 41-handler set is byte-for-name IDENTICAL** — and the
> reproduced roster equals the [MARIANA baseline](./mariana-pool.md)'s published 41-name
> set exactly (`comm` empty both directions, this session). [HIGH/OBSERVED]

```text
AluOp BRANCH BranchPrefetchHint ConvLutLoad CrossLaneReduce EXT_BREAK EmbeddingUpdate
EngineNop Event_Semaphore ExtendedInst GetSequenceBounds Halt INS_BREAK INS_FL Iota
LoadPoolArgument MEMSET/RNG MOVE ModifyPoolConfig NOP NOTIFY POLL_SEM Pool RandGetState
RandSetState Redirect SB2SB_Collective SET_OM STRONG_ORDER Sort Tensor-Reduce
Tensor-Scalar Tensor-Scalar-PTR Tensor-Tensor TensorDequantize TensorGather TensorLoad
TensorScalarAddr TensorScalarAffineSelect TensorStore WRITE
```

> **NOTE — the absolute count is method-dependent; the cross-gen diff is not.** A
> stricter `strings` minimum length counts 40 single-token names on both gens (one
> two-token / glue-trapped form falls outside the end-anchor *equally* on both). Either
> count gives the **load-bearing** result — the cross-gen diff is clean `+0/−0`. The
> 41-name set above was reproduced with `strings -n2` this session and matches the
> committed baseline name-for-name. [HIGH/OBSERVED for the diff; the absolute count is
> method-dependent, MED]

POOL's SEQ handler set is the **richest of the five engines** and already shipped on
CAYMAN/MARIANA with **all** general-compute + RNG (`RandGetState`/`RandSetState`) +
`ExtendedInst` handlers. There was nothing to add at the SEQ layer this generation —
the v4+ delta is **not** a handler add but the DGE fast-path *code* (§2) reached through
the **existing** handlers. Both gens also carry the dual-mode SEQ strings
(`S: NX in HW Decode mode` / `S: NX in Sunda mode`), the ErrorHandler arms
(`cayman/seq/src/handlers/exception_handler.hpp`), and no `mariana-4062`/`mariana_plus`
errata. The only self-name change is `S: BEGIN on mariana` → `S: BEGIN on mariana_plus`.
[HIGH/OBSERVED]

---

## 6. The Q7_POOL kernel_info_table + EXTISA — BYTE-IDENTICAL cross-gen

### 6a. The EXTISA SOs are byte-identical v4 ↔ v4+

`EXTISA_0_SO` `9f2ce049` and `EXTISA_3_SO` `8477ff26` (and `EXTISA_1/2_SO` + all 4 JSON)
are byte-for-byte identical (`cmp -s` clean this session) to the MARIANA POOL EXTISA SOs.
So the **entire** `kernel_info_table` — keys **and** `funcVA`s — is identical, **not
merely relocated**: the Q7 compute kernel containers did not change at all between
MARIANA and MARIANA_PLUS (contrast the `+0x0..+0x44` monotonic relocation MARIANA showed
going CAYMAN→MARIANA). [HIGH/OBSERVED]

### 6b. The 17-entry KIT (EXTISA_0), parsed byte-exact this session

`kernel_info_table @ vaddr 0x02000380 / file off 0x7400, size 0x88 = 17 entries`; record
format `{ u8 0; u8 0; u8 spec(+2); u8 opcode(+3); u32_le funcVA(+4) }`; the KIT bytes are
identical to MARIANA (`cmp` clean). [HIGH/OBSERVED]

| idx | opcode | spec | funcVA | routing (== MARIANA) |
|---|---|---|---|---|
| 0 | `0x7e` | 0 | `0x01000080` | `pool_iota` |
| 1 | `0x7c` | 0 | `0x010003f8` | `pool_cross_lane_reduce_arith` |
| 2 | `0x7d` | 0 | `0x01000410` | `pool_cross_lane_reduce_bitvec` |
| 3 | `0x45` | 0 | `0x01000b90` | `decode_pool` [Pool] |
| 4 | `0x51` | 0 | `0x01001068` | (tensor primitive) |
| 5 | `0x41` | 0 | `0x01000f1c` | `decode_tensor_tensor_arith` |
| **6** | **`0xf0`** | **0** | `0x01003390` | `ExtendedInst` spec0 (EngineNop) |
| **7** | **`0xf0`** | **1** | `0x010033a0` | `ExtendedInstCopy` |
| **8** | **`0xf0`** | **2** | `0x010034a4` | `decode_extended_inst_tensor_tensor_arith` |
| **9** | **`0xf0`** | **4** | `0x010037d8` | ExtendedInst spec4 → Rand band |
| **10** | **`0xf0`** | **3** | `0x01003a90` | ExtendedInst spec3 → Rand band |
| 11 | `0x52` | 0 | `0x01003b80` | (tensor primitive) |
| 12 | `0x46` | 0 | `0x01004100` | `pool_copy` |
| 13 | `0x47` | 0 | `0x010041a0` | (tensor primitive) |
| 14 | `0xbe` | 0 | `0x01004244` | (tensor primitive) |
| 15 | `0xf2` | 0 | `0x01004890` | `get_sequence_bounds` / `NonzeroWithCount` |
| 16 | `0x7b` | 0 | `0x01004e04` | `decode_tensor_dequantize` [TensorDequantize] |

`EXTISA_1` = 1 entry (`0x7e`), `EXTISA_2` = 2 (`0x7c`, `0x7d`), `EXTISA_3` = 9 (cptc/MX
family) — all key + funcVA identical to MARIANA. **No new `opcode→funcVA` rows.** See
[kernel_info_table Layout](../firmware/pool/kernel-info-table.md). [HIGH/OBSERVED]

### 6c. The Q7 RNG body — UNCHANGED v4 ↔ v4+ (TIE+LFSR retained)

The `Q7_POOL DEBUG DRAM` `'P%i:'` RNG token set is identical across gens (re-grepped
this session): `Xorwow(TIE)` × 1, `XorwowRng(TIE)` × 1, `LfsrGetSeeds` × 1,
`LfsrSetSeeds` × 1, `Xorwow(SW)` × **0**, `rand_algo` × 2, `RandGetState` × 3,
`RandSetState` × 3 — on **both** MARIANA and MARIANA_PLUS. The `Xorwow(SW)` → TIE+LFSR
boundary was the **MARIANA (v4) arrival** (the
[MARIANA baseline §5](./mariana-pool.md) headline); MARIANA_PLUS (v4+) **retains** it
byte-for-name. The RNG is **not** a v4+ POOL delta — it landed at v4 and is unchanged at
v4+ (the same "v4 feature, retained at v4+" pattern the ACT/PE pages show). See
[RNG — LFSR + `rand_algo` Dispatch](../firmware/kernels/rng-lfsr-dispatch.md). [HIGH/OBSERVED]

### 6d. The one Q7-side v4+ delta — `tensor_reshape_transpose_sb2sb`

The only Q7 image-level difference is the new SB-to-SB reshape **kind**:
`tensor_reshape_transpose_sb2sb` is present on MARIANA_PLUS Q7 (× 1) and absent on
MARIANA (× 0); `gen_spray_info` (× 6) and `make_gather_pattern` (× 2) are unchanged. This
small kind body is what makes Q7 DEBUG IRAM `+0x1c0` and DKL_DEBUG IRAM `+0x1c0` (+3 entry
prologues); the Q7 *release* path strips the debug strings and the kernel ELFs did not
change, so PERF/TEST/DKL_PERF + the EXTISA SOs are byte-identical (§3b). This is the Q7
leg of the fast-path's NX-vs-Q7 split ([DGE Reshape §6.1](../firmware/dge/dge-reshape.md)).
[HIGH/OBSERVED]

---

## 7. The 0xF0 ExtendedInst bridge — INTACT (both cores)

The `0xF0` two-level escape registers across **both** cores on MARIANA_PLUS, unchanged.
[HIGH/OBSERVED]

- **SEQ side (`NX_POOL`):** index `0xf0 − 0x41 = 0xaf = 175`; slot `@table 0x800 + 175·4`
  reads **`0x30e2`** (MARIANA_PLUS) / `0x306d` (MARIANA) — **both REAL** handlers, each
  distinct from its default trampoline (MPLUS `0x30ea`, MAR `0x3075`), relocated `+0x75`
  (parsed from the 177-word table this session). `S: ExtendedInst` present in `NX_POOL`
  DRAM on both gens — **POOL-exclusive** (the only engine with the bridge).
- **Q7 side (`EXTISA_0`):** the five `0xf0+spec` rows (specs `0,1,2,4,3`, KIT idx 6–10)
  are byte-identical cross-gen — the EXTISA SO is byte-for-byte identical, so the keys
  **and** the `funcVA`s match. The two-level `(opcode<<24)|(spec<<16)` escape is
  structurally invariant; the spec byte sub-selects exactly one of the five rows.

POOL remains the **only** engine with **both** the SEQ `0xf0` bridge **and** a Q7 compute
core — which is exactly why only POOL has the dual-dispatch. See
[POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md). [HIGH/OBSERVED]

**dtype / MX footprint.** `NX_POOL`: only `NEURON_ISA_TPB_DTYPE_{UINT32,INT32,FP32}`
(`move.cpp`), no `FP4`/`CPTC`/`MXTENSOR`/`proc_4bit` strings (0 NX hits) — byte-identical
to MARIANA; new dtype codes are numeric in the decode path, not named strings (same
negative as ACT/DVE/PE). `Q7_POOL`: the MX dequant footprint (`proc_4bit_mx` × 1,
`cptc_decode` × 2, `proc_6bit_non_mx` × 1) **is** present — but the EXTISA SOs are
byte-identical cross-gen, so this MX path is byte-for-byte the MARIANA one, **pre-existing
since CAYMAN, not a v4+ add**. [HIGH/OBSERVED]

---

## 8. DKL, PROF, and the size deltas

**DKL — structurally invariant.** `cmp` confirms MARIANA_PLUS `DKL_PERF_IRAM ==
DKL_TEST_IRAM` (`8a8c927a`) and `DKL_PERF_DRAM == DKL_TEST_DRAM` (`0aaa01a7`) — the same
"DKL has only a DEBUG-vs-release split" property as MARIANA/CAYMAN — and `DKL_PERF` is
**byte-identical to MARIANA `DKL_PERF`**. `DKL_DEBUG` differs (`47332835`/`fb6ff81e` vs
MARIANA `22790dbe`/`a01f5d43`; `+0x1c0` IRAM = the `sb2sb` kind + recompile). See
[External-Library / Prelink Loader](../firmware/pool/external-lib-loader.md). [HIGH/OBSERVED]

> **QUIRK — `"CustomOps not supported on Cayman"` survived into MARIANA_PLUS.** The
> CAYMAN-named source string is **still** unchanged on v4+ Q7 DKL — a build-string
> artifact, exactly as MARIANA; the dynamic custom-op path remains gated off.
> [HIGH/OBSERVED]

**PROF — byte-identical to MARIANA, disarmed.** Both NX_POOL profiling blobs are `cmp -s`
clean against the MARIANA POOL tables this session: [HIGH/OBSERVED]

- **PROF_CAM** `0951b326f4a40ccd` (`0x400`) — the **same disarmed CAM**: a strict
  `enable==1` count of **1** (a single stray opcode-0 / mask-0 sentinel record), and **0**
  effective armed opcode-capture records (`mask≠0`). POOL's per-engine HW-decode profiler
  is disarmed on **both** v4 and v4+.
- **PROF_TABLE** `534f2239b9e76d1c` (`0x2000`) — re-preallocated, zeroed header + 1 nonzero
  word; byte-identical to MARIANA.

This is the gen-wide per-engine PROF reuse the v4+ matrix shows: MARIANA_PLUS reuses
MARIANA's per-engine PROF_CAM byte-identical on all four PROF-bearing engines (ACT
`326bc0dd`, DVE `ca588683`, PE `43475cec`, POOL `0951b326`). `Q7_POOL` ships **no** PROF.
See [PROF CAM/TABLE Formats](./prof-cam-table-formats.md). [HIGH/OBSERVED]

**Size / sha — 10/20 byte-identical, 10/20 differ.** The directional split is the diff's
signature: **NX_POOL IRAM GREW in every variant** (the v4+ DGE fast-path code — the
*opposite* of the v4 shrink vs CAYMAN); the Q7 *release* path is byte-identical; Q7
DEBUG/DKL_DEBUG `+0x1c0` (the `sb2sb` kind). [HIGH/OBSERVED]

| IMAGE | MAR sz / sha | MPLUS sz / sha | ΔSize | identical? |
|---|---|---|---|---|
| NX PERF_IRAM | `0x14520` / `22991181` | `0x17bc0` / `582c246b` | **`+0x36a0`** | NO (recompile + DGE) |
| NX TEST_IRAM | `0x14240` / `a505cbaa` | `0x176a0` / `bf84aacb` | **`+0x3460`** | NO |
| NX DEBUG_IRAM | `0x1c080` / `41b6c798` | `0x1d340` / `9b514bb6` | **`+0x12c0`** | NO |
| NX DEBUG_DRAM | `0x7000` / `ec067304` | `0x7160` / `d2e1552a` | `+0x160` | NO |
| **NX PROF_CAM** | `0x400` / `0951b326` | `0x400` / `0951b326` | `+0x0` | **YES** |
| **NX PROF_TABLE** | `0x2000` / `534f2239` | `0x2000` / `534f2239` | `+0x0` | **YES** |
| **Q7 PERF_IRAM** | `0x164e0` / `0c761dba` | `0x164e0` / `0c761dba` | `+0x0` | **YES** |
| **Q7 PERF_DRAM** | `0x13180` / `c448c5ff` | `0x13180` / `c448c5ff` | `+0x0` | **YES** |
| **Q7 TEST_IRAM/DRAM** | `e8a32b3f`/`6271b2d0` | `e8a32b3f`/`6271b2d0` | `+0x0` | **YES** |
| Q7 DEBUG_IRAM | `0x1ed40` / `47f76629` | `0x1ef00` / `1c9c15bc` | `+0x1c0` | NO (`sb2sb`) |
| Q7 DEBUG_DRAM | `0x15d80` / `02cacff0` | `0x15d80` / `295fae9c` | `+0x0` | NO (`sb2sb` string) |
| **Q7 DKL_PERF_IRAM/DRAM** | `8a8c927a`/`0aaa01a7` | `8a8c927a`/`0aaa01a7` | `+0x0` | **YES** |
| Q7 DKL_DEBUG_IRAM | `0x14140` / `22790dbe` | `0x14300` / `47332835` | `+0x1c0` | NO (`sb2sb`) |
| **EXTISA_0_SO** | `0xa260` / `9f2ce049` | `0xa260` / `9f2ce049` | `+0x0` | **YES** |
| **EXTISA_3_SO** | `0x6974` / `8477ff26` | `0x6974` / `8477ff26` | `+0x0` | **YES** |

The dispatch mechanism, reset-vector form, `0xF0` bridge, KIT (key + funcVA), RNG body,
ErrorHandler arms, source trees, PROF tables, and the dual-core split are all
**invariant**. [HIGH/OBSERVED]

---

## 9. Adversarial self-verification

Five strongest claims, re-challenged against the binary this session:

1. **Per-core byte-for-name / byte-for-key stability.** *Challenge:* could the NX
   `41==41` be a glue-trap artifact, or the Q7 KIT have silently re-routed? *Re-verify:*
   strict end-anchored `S:` diff → `41 == 41`, `comm` EMPTY both directions, and the
   reproduced roster equals the committed baseline name-for-name; the EXTISA_0 KIT bytes
   are `cmp`-identical to MARIANA (17 entries, opcodes/specs/funcVAs all match) and the
   whole EXTISA SO is byte-identical (`9f2ce049`/`8477ff26`). **HOLDS.** [HIGH/OBSERVED]
2. **The DGE fast-path code body (the headline).** *Challenge:* are the 4 strings stray
   text, or present on MARIANA too? *Re-verify:* `dge_decode_fast` /
   `dge_reshape_memcopy_transpose_fast` / `tensor_reshape_transpose_sb2sb` /
   `wait_for_credit` → MPLUS = 1, MARIANA = 0 (all four); `push REGWRITE` → MPLUS = 0,
   MARIANA = 1 (retired); the shared `dge_reshape.cpp`/`analyze_tensor_reshape`/`Setting up
   DGE` on both; `dge_decode_fast` recurs in the symbol-bearing TEST DRAM; and the NX DEBUG
   IRAM grew `+0x12c0` while `entry` prologues fell `697 → 675` (inline consolidation).
   **HOLDS** (strings + counts OBSERVED; fusion/credit semantics INFERRED-HIGH).
3. **SAME +0x1c, no further shift.** *Challenge:* could a later variant hide a second
   relocation? *Re-verify:* NX IRAM head `06 7d 00 00 … 86 7e … a0 71 69 80` byte-identical
   to MARIANA across variants (`j 0x1f8`/`j 0x204`); Q7 head `06 7f 00 …` (`j 0x200`)
   unchanged; both → `enter_run @0x90`; first NX divergence at byte `0xa3` (a relocated
   literal, not a vector move). **HOLDS.** [HIGH/OBSERVED]
4. **The `0xF0` bridge across both cores.** *Challenge:* does the SEQ slot still point to a
   REAL handler, and do the five Q7 rows survive? *Re-verify:* SEQ slot[175] = `0x30e2`
   (MPLUS) / `0x306d` (MARIANA), both ≠ default; `S: ExtendedInst` present; Q7 five
   `0xf0+spec{0,1,2,4,3}` rows byte-for-key (KIT idx 6–10). **HOLDS.** [HIGH/OBSERVED]
5. **PROF byte-identity.** *Challenge:* same size could mask different content.
   *Re-verify:* `cmp -s` clean for both CAM (`0951b326`) and TABLE (`534f2239`); the CAM
   decodes to the same 1 stray-sentinel / 0-armed disarmed shape. **HOLDS.** [HIGH/OBSERVED]

---

## 10. Honesty ledger

**HIGH / OBSERVED (reproduced this session):** container sha `b7c67e89…632fc329b`; 46
getters (14 NX + 32 Q7); all carve hashes match (NX DEBUG_IRAM `9b514bb6`, Q7 DEBUG_IRAM
`1c9c15bc`, EXTISA_0 `9f2ce049`); 12/12 spot-reconciled to `libnrtucode.a`; MARIANA
baseline re-carved (NX DEBUG_IRAM `41b6c798`, PROF `0951b326`/`534f2239`) → diff vs
authentic MARIANA. Reset NX `06 7d 00` (== MARIANA, no further shift) / Q7 `06 7f 00`
(unchanged), both → `enter_run @0x90`. Dispatch `addi a2,a2,-65` + `movi a3,177` + table
`@0x800`, default `0x3075→0x30ea`; 54 real / 123 default both gens. Handler diff `41==41`
(`+0/−0`, roster == baseline). 0xF0 slot[175] `0x30e2`(MPLUS)/`0x306d`(MAR) both real;
KIT 17 entries byte-identical (key + funcVA). RNG TIE+LFSR × 1 each, `Xorwow(SW)` × 0,
both gens. DGE fast-path 4 strings MPLUS=1/MARIANA=0, REGWRITE retired, symbol in TEST
DRAM; NX IRAM grew `+0x12c0`, entries `697→675`. PROF `cmp -s` clean both blobs; disarmed.
DKL `DKL_PERF==DKL_TEST==MARIANA DKL_PERF`; "Cayman" string kept. Size 10/20 identical /
10/20 differ; self-name `BEGIN on mariana_plus`.

**MED / INFERRED:** the absolute NX handler *count* (41 vs a stricter-`strings` 40) is a
regex/min-length method artifact — the cross-gen diff (`+0/−0`) is HIGH; the fast-path
*fusion / inline-consolidation* reading (the IRAM-grew-but-fewer-entries correlate is
HIGH; the optimization *semantics* INFERRED-HIGH from the names + DGE corpus); the exact
`sb2sb` kind body on Q7 (FLIX desync; +3 entries OBSERVED, per-instruction body MED);
`engine_idx = 2` computed at boot (string + boot path + ISA enum POOL=2); PROF "disarmed"
record-shape INFERRED-HIGH.

**LOW / NOT CLAIMED:** which silicon/runtime selects MARIANA vs MARIANA_PLUS or
DEBUG/PERF/TEST/DKL; the exhaustive per-opcode SEQ table rows + the Q7 per-entry compare
loop body (FLIX desync); the exact `wait_for_credit` credit-counter mechanics (string +
NX presence only).

---

## 11. Cross-references

- [MARIANA × POOL](./mariana-pool.md) — **the diff baseline** (the dual-core model, the
  177-entry SEQ hub, the 41-handler derivation, the KIT format, the `0xF0`
  reconciliation, the v4 RNG arrival).
- [MARIANA_PLUS × ACT](./mariana-plus-act.md) — the v4+ template (the null-functional-delta
  + DGE fast-path pattern, established first).
- [MARIANA_PLUS × DVE](./mariana-plus-dve.md) · [MARIANA_PLUS × PE](./mariana-plus-pe.md) —
  the sibling v4+ engines (the contiguity cursor that places POOL last).
- [MARIANA+ generation delta](../generations/mariana-plus-delta.md) — the broader v4+ diff.
- [DGE Reshape Engine](../firmware/dge/dge-reshape.md) — **the fast-path's home page**
  (descriptor structs, analyze→emit pipeline, the per-gen presence map, the
  IRAM-grew/entries-fell correlate).
- [Legacy DMA / iDMA](../firmware/dge/idma-legacy-dma.md) — the descriptor-ring credit flow
  `wait_for_credit` throttles against.
- [kernel_info_table Layout](../firmware/pool/kernel-info-table.md) — the 8-byte record /
  packed-key format.
- [RNG — LFSR + `rand_algo` Dispatch](../firmware/kernels/rng-lfsr-dispatch.md) — the
  retained TIE+LFSR RNG body.
- [POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md) — the spec
  sub-dispatch.
- [External-Library / Prelink Loader](../firmware/pool/external-lib-loader.md) — the DKL layer.
- [PROF CAM/TABLE Formats](./prof-cam-table-formats.md) — the byte-identical disarmed profiler.
- [Image Catalog Index](./image-catalog-index.md) — the getter map.
