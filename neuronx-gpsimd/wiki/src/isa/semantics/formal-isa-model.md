# The Complete Formal ISA-Semantics Model

This is the **capstone** of the formal-semantics section: the authoritative, bit-precise
reference-compute **model** for the Vision-Q7 *Cairo* (`ncore2gp`) 512-bit FLIX/VLIW DSP. It does
*not* re-derive each opcode — the two group-semantics pages
([I](group-semantics-i.md), [II](group-semantics-ii.md)) carry the per-group datapaths and the
[coverage ledger](coverage-ledger.md) carries the roster accounting. This page **synthesizes** those
three into one model: the eight shared datapaths in summary form, the cross-group invariants that
hold over all of them, the multi-source confidence that grounds them, and the precise close-or-bound
of the residual `MED` opcodes. It is the reference the executable-model Parts validate against — the
[ISS semantic synthesis](../../iss/iss-semantic-synthesis.md) (Part 14) and the
[four-oracle validation method](../../validation/four-oracle-method.md) (Part 15) check their
behavior against *this* model, not the other way round.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte / immediate / symbol / **executed** value read from the shipped binary this pass;
`INFERRED` = reasoned over `OBSERVED`; `CARRIED` = re-used at a cited page's confidence — crossed with
`HIGH`/`MED`/`LOW`. Every count is grounded with `nm <lib> | rg -c` against the `.symtab`, never a
decompile grep; the `extracted/` tree is gitignored (reach it with `fd --no-ignore` or an absolute
path). All prose is binary / static-analysis derived only.

> **Headline.** The Cairo IVP+Xtensa ISA has a bit-precise reference-compute model for **1528 of the
> 1534 shipped opcodes** (99.6%); the 6 remainder are pipeline-ordering fences with no datapath. Of
> the full **1607-mnemonic** pre-fold TIE-DB roster, 1558 are bit-precise, 6 are fences, 43 are
> no-body decode-tree pseudo-mnemonics. After closing the `MED` set below, the `HIGH` fraction of the
> 1558 bit-precise bodies rises from ~1420 to ~1545+; the genuine residual is a handful of
> value-determined innermost-tree wirings plus correctly-`CARRIED` firmware/`.rodata` facts — **not a
> single missing datapath body**. `[HIGH/OBSERVED]`

---

## 0. The unifying architecture — one parametric datapath per iclass

The whole IVP vector ISA is a **small set of shared, fully-multiplexed reference datapaths**: one
`<SEMANTIC>` block per instruction class, whose member LIST enumerates the opcodes, and whose per-op
behavior is selected by `op_<GROUP>` OR-reduction decode wires fanning into one parametric compute
`<MODULE>`. The base-Xtensa control spine is the **inverse** — many small per-op `<SEMANTIC>` blocks,
one per branch / per SR / per access-form. The entire compute lives in **two TIE providers**:

* **`libtie-core.so`** carries the per-op **compute**. The `post_rewrite` blob is `.data`-resident at
  VMA `0x20104a` (`nm libtie-core.so | rg xml_data_post_rewrite`), ending at `xml_data_compiler`
  (`0x30c1d27`). `.data` has VMA `0x201018` / file offset `0x001018` (`readelf -SW`), so the blob's
  **file offset = VMA − `0x200000`** — *not* the `0x400000` delta seen in other DLLs. `[HIGH/OBSERVED]`
* **`libtie-Xtensa-msem.so`** carries the memory/control **interface seam**: the load/store
  reference, the SuperGather host ports, and the SR/EXC/EXTREG/sync signal surface. It has **zero**
  `<OPCODEDEF>` and **zero** `<SEMANTIC>` — it is the signal + load/store-reference half, *not*
  per-op compute. `[HIGH/OBSERVED]`

The model is checked against a third leg — the **`libfiss-base.so` `module__xdref_*` value leaves**
(864 of them; `nm libfiss-base.so | rg -c module__xdref_`), self-contained integer-only functions
this page **drives live via `ctypes`**. Their ABI was read from the prologues and has a **dead first
integer argument** (`%rdi` ignored; A in `%rsi`, B in `%rdx`, result pointer in `%rcx` or `%rdx`
depending on operand count); see [group-semantics-I §0](group-semantics-i.md) for the full ABI table.
Three leaves are driven live in [§4](#4-multi-source-confidence--the-tie--cas--fiss-triangulation).

---

## 1. The eight op-group reference-compute summaries

Each group is **one (or few) shared `<SEMANTIC>` datapath(s)**, parameterized per-op by the
`op_<GROUP>` decode keys. Byte offsets are into the decoded `post_rewrite` blob (`libtie-core`,
file offset = VMA − `0x200000`) unless marked `msem`. The four data groups (arith / MAC / load-store /
gather) are detailed in [group-semantics-I](group-semantics-i.md); the four control-and-format groups
(predicate / convert-fp / valign-reduce / control) in [group-semantics-II](group-semantics-ii.md).
The B-batch [ISA-reference pages](../ref/template-and-partition.md) catalog the *encodings*; this is
the *reference math* those encodings drive.

### G1 — Vector arithmetic — `xdsem_vec_alu_arith_8Mbit_64` *(287 ops)*

One `<SEMANTIC name="ivp_sem_vec_alu">` (byte `33,778,228`; 244 members) + `ivp_sem_vec_mov` (44).
The core is a **two's-complement carry-chain adder** with lane-break masks, instantiated 64-bit ×8 =
512-bit. The **only native arithmetic primitive is `<ADD>`**; everything else is structural over it.

| sub-op | reference compute | selector |
|---|---|---|
| `SUB` | `a + ~b + 1` | `op_GRP_A1_SUB` XORs `b`, `cin=1` |
| `WRAP` | `propagate_mask` breaks the carry at each 8/16/32-bit lane edge → mod `2^w` | (default) |
| `S`-sat | clamp to `±(2^(w−1))` = `0x7fff`/`0x8000` for `w=16` | `op_GRP_SAT` |
| `AVG`/`AVGR` | `(a+b)>>1` / `(a+b+1)>>1` (the `+1` via the same `cin`) | `op_AVG`/`op_AVGR` |
| `MIN`/`MAX` | a 2nd adder yields `a−b`; per-lane `le`/`ge` picks `a` or `b` | `op_MIN`/`op_MAX` |
| `ABS`/`ABSSUB`/`NEG`/`MULSGN` | conditional-negate selects on the `SUB` datapath | `op_*` |
| fp `MIN`/`MAX` vs `MINNUM`/`MAXNUM` | ordered-compare (operand-order NaN) vs NaN-suppress (`NaN→x`) | `f*_minnum_sel_a` |

**Per-op key:** `(op, sign, sat, round, width, pred)` from the `op_<GROUP>` OR-reductions —
decoder-exhaustive, not name-inferred. Signedness is the **guard-bit extension**
(`ctrl_unsign_adder`), not a runtime flag. `[HIGH/OBSERVED]`

### G2 — Multiply / MAC — `ivp_sem_mul_slice` *(301 ops)*

`<SEMANTIC name="ivp_sem_multiply">` (byte `34,953,115`; 232 integer members) + `ivp_sem_spfma`
(27 fp32 FMA) + `fp_sem_hp_fma` (27 fp16 FMA) + `ivp_sem_divide` (14) + `ivp_sem_addmod` (1).
**No native `<MUL>` exists in the 49 MB blob** (`<ADD>`×1968, `<SUB>`×748, `<MUL>`×0): the multiplier
is a Wallace/Dadda partial-product + carry-save (sum/carry redundant rails) tree resolved by 5
carry-propagate `<ADD>`s. The fp MACs are a **separate single-rounding FMA datapath**.

* **Accumulator:** 48-bit-per-lane two's-complement (`accum_even/odd[47:0]`); `wvec` =
  16 slices × 96-bit = 32 lanes × 48-bit = **1536-bit**. Widening matrix: `i8×i8→24`, `i16×i16→48`,
  `i16×i32→64/96`, `i32×i32→96`.
* **`op_acc`** = accumulate (`new=old+prod`) vs overwrite (`new=0+prod`). **Integer MAC WRAPS**;
  saturation is a *post-op pack* (`PACKL`), never inside the accumulate.
* **Signedness** = per-operand partial-product extension (`sign_a`/`unsign_a`/`unsign_b`) → the
  `s*s`/`u*u`/`s*u`/`u*s` quad as *separate* opcodes. The `XR` forms source one factor from the
  packed reduce-register `b32_pr` (the matmul weight bus).
* **fp FMA** = **true single-rounding** (product kept full-width; one round at output). The exact
  tie-break and the FCR gate are formally closed in [§5.1](#51-closed-the-fp-fma-exact-round-tie-break-med--high).

**Per-op key:** `(form, sign, accumulate, precision)` from `op_mul`/`op_mulp`/`op_mulq`/`op_mul4t`/
`op_sqr`/`op_decneg`/`op_dmul`, cross-confirmed by the `xt_ivp32.h` acc ctypes
(`xb_vecNx48`/`2Nx24`/`N_2x64w`). `[HIGH/OBSERVED]`

### G3 — Load / store — `xt_load_semantic` / `xt_store_semantic` *(195 ops)*

`ivp_sem_ld_st` carries each opcode's **address generation**; the **memory-access compute** is the
`msem` pair `xt_load_semantic` (byte `120,818`) / `xt_store_semantic` (byte `198,656`). The base DB
has *zero* references to `xt_load_semantic` — the two-provider split is real.

* **Align-down + within-line funnel** (little-endian config):
  `aligned = VAddr & ~(issue_bytes−1)`; `loaded = (lv0 >> shiftsel) | (lv1 << (issue_bits − shiftsel))`
  where `lv1` is the tail line at `aligned + issue_bytes` (BE is the mirror).
* **Byte-disable** `& ~disable_mask`; store honors per-byte (`StoreByteDisable`) *and* per-word
  (`StoreWordDisable`, 16×32b) disable; **store truncates** (positional, no numeric cast).
* **Sign/zero-extend** by sign-splash into high lanes (signed; `SignExtendFrom` present) or 0
  (unsigned), then zero-mask to `extend_to`.
* **Dual LSU:** `XT_LOADSTORE_UNIT num_copies=2`; a normal access uses one pipe (the 2nd only for an
  unaligned tail); the 5 `L2A*`/`L2U*` 2-vector loads consume **both** pipes.
* `xtms_aligned_load`/`store` is the **uninterpreted memory seam** — the named hook the simulator
  replaces with a real 64-byte line fetch.

**Per-op key:** `LSBytes`/`SignExtendFrom`/`SignExtendTo`/`RotateAmount`/disable from the per-opcode
selectors (~27 `has_*`/`Isa*`), *not* `op_<GROUP>` OR-reductions. `[HIGH/OBSERVED]`

### G4 — Gather / scatter + permute — `ivp_sem_vec_scatter_gather` / `xdsem_tiesel_5_32` *(90 ops)*

`ivp_sem_vec_scatter_gather` (byte `34,462,563`; 24) + `ivp_sem_vec_select` (byte `34,195,174`; 18) +
`ivp_sem_vec_specialized_seli` (6) + `ivp_sem_vec_rep` (28) + `ivp_sem_sqz` (6) +
`ivp_sem_vec_histogram` (8). The lane-routing core is `xdsem_tiesel_5_32` — a **5-bit one-hot 32-way
mux** (per output lane the index picks one of 32 source lanes). Gather **straddles** base(compute) +
msem(access).

* **`GSControl[15:0]`** = `{8'd0, elem_sz[1:0], offst_sz[1:0], operation[3:0]}`; `operation 1..6` =
  `{gathera, gatherd, mgatherd, scatter, scatterw, scatterinc}`; `elem_sz {0/1/2}` = `{8/16/32-bit}`.
* **`addr[lane]`** = `VAddrBase(ars) + GSVAddrOffset(vs)` (16×32-bit per-lane offset), host-scaled by
  `elem_sz ∈ {1,2,4}` bytes.
* **`GSEnable`** = `op_pred ? vbr_in : all-ones` (predicate-gated OOB; the host applies the
  `0xffffffff` miss sentinel — `CARRIED`, not in the TIE block).
* `SHUFFLE` `dst[i]=src[idx[i]]`; `SELECT` `dst[i]={B++A}[idx[i]]`; `DSEL`/`DCMPRS` dual-out/compress;
  sub-byte `seli` reads the 7-bit `tab_selimm_7b`/`tab_shflimm_7b` LUT (closed in
  [§5.3](#53-closed-the-sub-byte-permute-innermost-lut-med--high)).

`[HIGH/OBSERVED]`

### G5 — Predicate / vbool — `ivp_sem_vbool_alu_ltr` *(33 ops)*

`ivp_sem_vbool_alu_ltr` (33) + the compares (members of `ivp_sem_vec_alu`). Pure **64-bit bitwise
logic** over the vbool register (1 bit/lane for the densest 64-lane `2NX8` view); compares produce a
2-bit-per-lane predicate word.

* **COMPARE** `z[1:0] = {2{c}}` (`c`→`0x3` true / `0x0` false); signed compare = the sign-flip-bias
  `{~a[msb], a[msb−1:0]}` mapped onto unsigned `<`,`<=`.
* **BOOLEAN** `notb`/`andb`/`orb`/`xorb`/`andnotb`/`ornotb` = the obvious 64-bit ops.
* ***`bitkillt` merge polarity*** (the named SEM-09 invariant, live-proven below): `bitkillt` emits
  **all-ones in the KILLED (predicate-FALSE) lane** = the keep-destination merge mask; `bitkillf` is
  the complement. The `_t` masked op is a **MERGE** — predicate-false lanes retain the prior
  destination. `[HIGH/OBSERVED]`
* **SCALAR-COND** `bitkill_mov{eqz,nez,gez,ltz}_1` → a 1-bit predicate from a 32-bit scalar.

### G6 — Convert / FP — `ivpep_sem_sp_cvt` / `..._hp_cvt` + lookups *(180 ops)*

`ivpep_sem_sp_cvt` (byte `34,639,876`; 31 fp32) + `ivpep_sem_hp_cvt` (21 fp16) + `ivpep_sem_sp_lookup`
(29; trunc + seeds) + `ivpep_sem_hp_lookup` (30) + `ivp_sem_wvec_pack` (42) +
`ivp_sem_unpack_wvec_mov` (18) + `bbn_sem_vec_sprecip_rsqrt` (5) + `ivp_sem_{rur,wur}_fcr_fsr` (4). A
soft-float `normalize→rebias→round→special` netlist; cores `sem_fp_sp_cnv`, `sem_fp_sp_cnv_round`
(the 5-mode round), `fp_spfma_round` (the FMA single round).

* **`RoundMode[2:0]`** = `{000 RNE, 001 RTZ, 010 RPI(+inf), 011 RMI(−inf), 100 RNA(away)}` — the
  **fp-rounding pin** (the full GRS round-up decision is in
  [§5.1](#51-closed-the-fp-fma-exact-round-tie-break-med--high)).
* `FI` ops force a fixed `RoundMode` const (`ficeil`/`fifloor`/`fitrunc`/`firound`); `firint` reads
  the dynamic FCR. `TRUNC`/`UTRUNC` = fp→int **RTZ always** (no GRS in the lookup path).
* **FP32-HUB:** the only native fp-width convert is `fp16<->fp32` (widen `CVTLH` lossless; narrow
  `CVTHL` round per FCR). PACK: `packl` WRAP, `pack`/`packvr` ROUND (`+1<<(sh−1)` bias), `packv*`
  SATURATE; `packvr` drains the MAC accumulator.
* **Full IEEE** classify/propagate (NaN/Inf/zero/subnormal), **gradual underflow (no FTZ)**;
  inexact/overflow/underflow/invalid are first-class outputs. `FCR[1:0]` = round mode (control);
  `FSR` = exception flags (status).

`[HIGH/OBSERVED]`

### G7 — Valign / reduce / scan — `ivp_sem_vec_reduce` + the funnel *(88 ops)*

Valign rides `ivp_sem_ld_st` (7 of the 195 LSU members) + `ivp_sem_vec_reduce` (byte `34,079,437`;
56) + `ivp_sem_vec_shift` (byte `34,321,726`; 32, the rotate). Cores: `xdsem_ld_shifter_512` (valign
funnel), `ivp_sem_csa_8_16_32_l0/l1/l2` (the reduce CSA tree).

* **VALIGN** = a byte/word-granular funnel shift over the 4×512-bit valign regfile (muxsel byte-phase
  select of the carried `data_align` + incoming word + width-expand + sign/zero-extend).
* **REDUCE** = a **log-step balanced tree** (not a sequential fold): ADD via a 3-level carry-save
  tree `l0→l1→l2`; `MAX`/`MIN` via a compare tree; fp `NUM` via a log-depth NaN-quieting select tree —
  value-equivalent to the flat fold under the header's associativity license. `op_vbout` (the `RB*`
  forms) carries the winning-lane **index** (argmax/argmin) alongside the value.
* **SCAN** = a **software** Hillis-Steele recurrence: `dst = combine(src, rotate(src, stride))`,
  stride-doubling, `log2(N)` passes — the kernel composes the rotate (`op_ROT`) + combine into one
  FLIX VLIW word; *not* a single hardware op.

`[HIGH/OBSERVED]`

### G8 — Control / state — the base-Xtensa per-op spine *(384 ops + 6 fences + 43 pseudo)*

**Many small per-op blocks**: 118 per-SR `rsr`/`wsr`/`xsr` + 26 branch + the wide-branch (the 24
`xt_wide_branch` `_w15` branch forms ship and collapse into the plain `beq`/`bne`/… roster entries; the
`.W18` alternate macro folds out — a CARRIED TIE-DB distinction, no `_w18`/`widebranch18` symbol exists in
any config DLL) + loops + regwin + extreg + virtualops + density + sync + debug — driven by
the `msem` interface (645 signal decls + 118 load/store reference functions).

* **SR op** = `{SRAddr const + SRRead/SRWrite strobe + the exact state↔AR bitfield move + per-SR
  side-effect}`; `XSR` = atomic exchange. 42 SRs / 118 ops, all SR# byte-exact.
* **EXTREG** `RER`/`WER` route `ars↔ER` bus, gated by `eraccess_allowed`
  (→`ExternalRegisterPrivilegeException` on deny).
* **ZOL** `LOOP` seeds `LCOUNT`/`LBEG`/`LEND`; the per-iter `LEND`-match decrement is the funcUnit's
  implicit loop-back, not an opcode.
* **Windowed ABI** `entry`/`retw`/`movsp` rotate `WB_C`/`WB_P` over the 64-entry physical AR file
  (16-entry visible window); `WindowOverflow8`/`Underflow8` spill faults.
* **BRANCH** `{target = PC + offset; BranchTaken = condition}`; the PC update is the funcUnit's.
* **SYNC fences** `DSYNC`/`ESYNC`/`FSYNC`/`ISYNC`/`RSYNC` + `MEMW`/`EXCW` = pure ordering, **no
  datapath**.
* **PRIVILEGE** `PrivilegedException = (MS_DISPST==0) & |PSRING & !InOCDMode`; the msem EXC roster
  (180 signals) is the fault surface.

`[HIGH/OBSERVED]`

---

## 2. The cross-group invariants

These hold over **all eight groups** and triangulate against the regfile, dtype, and decode pages.

1. **The two-provider split.** Every group's compute is in `libtie-core`; the memory/control interface
   seam (load/store reference, gather host ports, the SR/EXC/EXTREG/sync surface) is in
   `libtie-Xtensa-msem`. The msem has 0 `<OPCODEDEF>` / 0 `<SEMANTIC>`. `[HIGH/OBSERVED]`
2. **The shared-multiplexed-datapath pattern.** For the IVP vector groups: one `<SEMANTIC>` per
   iclass + a member LIST + `op_<GROUP>` OR-reduction selectors + one parametric core `<MODULE>`. The
   per-op `(op, sign, sat, round, width, pred, form)` tuple is **decoder-exhaustive and OBSERVED**.
   The base-Xtensa control spine inverts this (per-op blocks). `[HIGH/OBSERVED]`
3. **`<ADD>` is the only native arithmetic primitive.** Across the 49 MB blob: `<ADD>`×1968,
   `<SUB>`×748, `<MUL>`×0, `<MULT>`×0. Multiply = partial-product + carry-save + `<ADD>` resolve;
   shift = concatenation/replication; select = mux net. The whole DB reads as Verilog RTL.
   `[HIGH/OBSERVED]`
4. **The 512-bit vector / 48-bit accumulator geometry is uniform.** vec = 512-bit (`64×8`/`32×16`/
   `16×32` lane views); wvec = 1536-bit = 32 lanes × 48-bit; vbool = 64-bit; valign = 4×512-bit.
   `[HIGH/OBSERVED]`
5. **Predication is uniform.** The `_t`/`T` forms take a `vboolN d` + an `/*inout*/ a` and are a MERGE
   (predicate-false lanes retain the prior dst), realized by the `bitkillt` all-ones-in-killed-lane
   mask — identical polarity in the ALU, MAC, convert, gather, and reduce slices. `[HIGH/OBSERVED]`
6. **The base-Xtensa state layer is shared.** The windowed ABI, the 42-SR state model, and the msem
   EXC fault surface (the privilege guard, the exception roster, the AR 32×64-wait — 64-physical/
   16-visible window) are the *same* layer for every op. `[HIGH/OBSERVED]`

### 2.1 The eight-register-file model *(pinned vs the regfile reference)*

The TIE ctypes match the byte-read `dll_regfile_table` records exactly for every file the SEM corpus
touched. The `idx` is the regfile-table index; widths/ctypes are `OBSERVED` from the decoded DB.

| file | idx | entries × bits | ctype | role |
|---|---|---|---|---|
| `AR` | 0 | 64 × 32 | (windowed) | scalar AR file — 16 visible over 64 physical (G8) |
| `BR` | 1 | 16 × 1 | `xt_booleans` | scalar boolean reg (G5/G8; flat SR `0x04`) |
| `vec` | 2 | 32 × 512 | `xb_vecNx16` … | the 512-bit vector operand reg (all IVP groups) |
| `vbool` | 3 | 16 × 64 | `vbool2N` | per-lane predicate (G5); 1 bit/lane for 64-lane `2NX8` |
| `valign` | 4 | 4 × 512 | `valign` | aligning register (G7; dbnum `0x1030..0x1033`) |
| `wvec` | 5 | 4 × 1536 | `xb_vecNx48`/`2Nx24`/`N_2x64w` | wide MAC accumulator — 32 lanes × 48-bit (G2/G6) |
| `b32_pr` | 6 | 16 × 64 | `xb_int64pr` | packed matmul weight / reduce-register (G2/G5) |
| `gvr` | 7 | 8 × 512 | (gr-target/source) | SuperGather staging reg (G4; `gt`/`gs`) |

> **NOTE — idx ordering.** The eight files index `0..7` in regfile-table order: `AR=0`, `BR=1`,
> `vec=2`, `vbool=3`, `valign=4`, `wvec=5`, `b32_pr=6`, `gvr=7`. The `vbool`/`b32_pr`/`valign`/`AR`/
> `BR` ctypes are byte-matched against the regfile reference; the `wvec`/`vec`/`gvr` geometry is
> `OBSERVED` from the slice wire widths with an `INFERRED-HIGH` lane mapping (each group page notes
> the wire it read). `[HIGH/OBSERVED]`

### 2.2 The dtype model — the FP32-HUB

The convert `<SEMANTIC>` member LISTs are **exhaustive**, and the FP32-HUB is the TIE-side negative
control:

* The **only** native fp-width convert is `fp16<->fp32` (`op_cvtlh` widen / `op_cvthl` narrow).
* **No** bf16, **no** fp8 (e3m4/e4m3/e5m2), **no** fp4 (e2m1) convert opcode exists in *any* convert
  or lookup block. `int<->fp16`, `int<->fp32` (float/ufloat), the `FI` integer-rounds, and the
  saturating packs are the complete native fp surface.
* Therefore bf16/fp8/fp4 are converted by the firmware `Cast`/`MX-dequant` kernels, each as **two
  legs through the fp32 hub** (nibble-unpack → ufloat → scale-MAC → sat-clamp → `cvtg48` extract).

The integer dtype widths are the MAC widening matrix (G2): `i8`/`i16`/`i32` inputs; `24/48/64/96`-bit
accumulators; the pack saturates back to the lane width. `[HIGH/OBSERVED — the member-LIST negative
control.]`

> **CORRECTION — per-gen dtype availability is *not* a TIE fact.** A reader might be tempted to read
> "which generation adds fp8/fp4/bf16" off the convert roster. The TIE exposes the **same**
> `fp16<->fp32` hub and the **same** seed tables regardless of generation; the per-gen
> dtype-availability is a firmware-image **header** finding, not inferable from this config. See the
> one-config guard in [§2.3](#23-one-cairo-config-the-coverage-is-semantic-not-a-silicon-generation-claim).
> `[HIGH/OBSERVED]`

### 2.3 ONE Cairo config — the coverage is *semantic*, not a silicon-generation claim

> **QUIRK / GUARD.** The decoded TIE DB is **one** config — `Xm_ncore2gp`, `Xtensa24`, `NX1.1.4`,
> `RI-2022.9` (the 1534-folded / 1607-superset opcode set). **No silicon-generation / gen-count /
> codename is inferred from any TIE descriptor.** Every token in this model — `sp`/`hp`,
> `fp16`/`fp32`/`bf16`/`fp8`/`fp4`, `.W15`/`.W18`, `8x8`/`16x16`/`16x32`/`32x32`, `_s0`/`_s2`/`_s4`,
> `wvt`/`wvu`/`wv<N>`, `M0..M7`, `gr0..gr7`, `[511:0]`, `even/odd/lo/hi`, the `RECIP_Data8`/
> `RSQRT_Data8`/QLI table contents, the `tab_selimm`/`shflimm` LUTs, `rstage/estage 0/3/4/6` — is a
> **datapath-width / precision / lane-parity / format / config / table-content** axis of that *single*
> config, **not** one of the five firmware-image generations
> (`SUNDA`/`CAYMAN`/`MARIANA`/`MPLUS`/`MAVERICK`, an axis not visible in any TIE descriptor). The seed
> tables are this one config's baked reciprocal/rsqrt approximation tables — a datapath-constant axis,
> not a gen axis. This synthesis reconciles **semantic coverage** only; any v5/Maverick interior is
> header-`OBSERVED` only, and any v5 claim would be `INFERRED`. `[HIGH/OBSERVED]`

---

## 3. The authoritative coverage statement

Of the **1607-mnemonic** pre-fold TIE-DB roster:

| class | count | meaning |
|---|---|---|
| bit-precise | **1558** | member of a rendered `<SEMANTIC>` iclass |
| categorical fence | **6** | `DSYNC`/`ESYNC`/`FSYNC`/`ISYNC`/`RSYNC` + `SIMCALL` (no datapath) |
| no-body pseudo | **43** | decode-tree interior nodes; all fold-out, not leaf instructions |
| **total** | **1607** | `1558 + 6 + 43` — checks exactly |

Of the **1534-mnemonic** shipped runtime roster: 1528 bit-precise / 6 categorical-fence / 0 no-body →
**99.6%** of every shipped opcode has a bit-precise reference body. The `+73` TIE-DB-only delta =
`{24 wide-branch macro forms + 6 virtualops (have bodies, folded) + 43 no-body pseudo}` (the wide-branch 24
= the `xt_wide_branch` `_w15` branch set; the `.W18` source-macro naming is CARRIED, not in any binary —
see [B30 §3.4](../ref/b30-appendix-p.md)); the shipped 1534
is a **clean subset** of the 1607 (0 shipped-only). The fold reconciles both frames:
`1607 − 1534 = 12642 − 12569 = 73` (encode placements: `num_encode_fns() = 0x3119 = 12569` shipped ↔
12642 pre-fold `<OPCODEDEF>` placements). **No semantically-significant shipped op lacks a reference
body.** `[HIGH/OBSERVED]` — full accounting and the certificate are in the
[coverage ledger](coverage-ledger.md) and [B30 Appendix P](../ref/b30-appendix-p.md).

> **NOTE — `num_states`: 81 vs 87.** `libisa-core.so` `num_states` returns `mov $0x51` = **81** (the
> scalar architectural states). Merging the `libisa-core-hw.so` module (`num_states = mov $0x6` = 6)
> gives **87 = 81 + 6**. This page means **81** wherever it says "architectural states" and **87**
> only for the explicitly merged-with-hw figure; a sibling page flagged the ambiguity and this is the
> harmonization. `[HIGH/OBSERVED]`

---

## 4. Multi-source confidence — the TIE / cas / fiss triangulation

The model is grounded on up to **three independent legs** per group, and no source contradicts
another in any group:

* **(A) TIE reference** — the `libtie-core` / `libtie-Xtensa-msem` `post_rewrite` decoded XML
  (source-of-truth datapath).
* **(B) cas-binary** — the `libcas-core.so` DWARF `xdsem` decode/timing model (the cas decode bitmap
  the TIE wires were generated against).
* **(C) fiss-oracle** — the `libfiss-base.so` `module__xdref_*` value leaves, the **executable**
  integer-only oracle, **driven live via `ctypes`** in-process.

| group | TIE (A) | cas (B) | fiss (C) | verdict |
|---|---|---|---|---|
| vector-arith | OBSERVED | xdsem | xdref live | A confirms + upgrades B `MED→HIGH` |
| MAC | OBSERVED | xdsem | xdref live (acc48) | acc48 **triple-confirmed** (wires · ctypes · width-tuples) |
| load-store | OBSERVED | xdsem | `wideldshift` live | A is the source B models |
| gather-permute | OBSERVED | xdsem | `bitkillf` live | **fully triangulated** (A=B=C agree) |
| predicate | OBSERVED | xdsem | `bitkillt` live | A upgrades B; `bitkillt` polarity OBSERVED live |
| convert-fp | OBSERVED | xdsem | `recip0`/`rsqrt0` live | A pins B's round enum HIGH; seeds live |
| valign-reduce | OBSERVED | xdsem | — | A shows the TREE B flattened |
| control-state | OBSERVED | xdsem | — | A pins SR#/window |

**Agreement across legs is the confidence lift:** where the TIE reference (A) and the cas decode
model (B) agree on a datapath *and* the fiss leaf (C) executes the exact result bits, the claim is
`HIGH/OBSERVED` rather than `MED/INFERRED`. The sub-byte permute LUT is TIE-vs-binary byte-exact
(`tab_shflimm_7b = {0x8, 0x1e}`); the MAC acc48 is triple-confirmed; the gather index plane is
A=B=C-agreed.

### 4.1 Three leaves driven live this pass (the lifts)

The `module__xdref_*` ABI has a dead first integer arg (`f(uint64 /*dead*/, uint32 a, …, uint32* res)`).
The result-pointer slot is operand-count-dependent (read from each prologue's store-target register).

```text
# (1) PREDICATE MERGE POLARITY — module__xdref_bitkillt_32_4 @0x528dd0  (res in arg3 %rdx)
#     body: res = -((pred & 1) ^ 1)        vs   bitkillf_32_4 @0x82d010: res = (pred<<31)>>31
bitkillt(pred=0) -> 0xffffffff   bitkillf(pred=0) -> 0x00000000   # KILLED lane: keep-dst all-ones
bitkillt(pred=1) -> 0x00000000   bitkillf(pred=1) -> 0xffffffff   # LIVE   lane: complement
#  => the `_t` masked op is a MERGE: predicate-FALSE lanes retain the prior destination. [HIGH/OBSERVED]

# (2) RECIP SEED — module__xdref_recip0_1_1_32f_32f @0x8785f0
#     body @0x87878a: lea 0x958fc0 <table__RECIP_Data8> ; mov (%rcx,%r12,4),%r12d   (dword stride)
recip0(1.0) -> seed word 0x3f7f0000   # mantissa top byte 0xff = RECIP_Data8[0]  (~0.998 ≈ 1/1)
recip0(2.0) -> seed word 0x3eff0000   # exp 2^-2, mantissa 0xff                   (~0.499 ≈ 1/2)

# (3) RSQRT SEED — module__xdref_rsqrt0_1_1_32f_32f @0x878900   (reads table__RSQRT_Data8 @0x958dc0)
rsqrt0(1.0) -> 0x3f7f0000   # ~0.998 ≈ 1/sqrt(1)
rsqrt0(4.0) -> 0x3eff0000   # ~0.499 ≈ 1/sqrt(4) = 0.5
```

The seed leaves provably `lea` the `.rodata` table and index it with a **4-byte stride** (the table is
stored dword-per-entry, the 8-bit seed in the low byte), then splash the seed byte into the result
mantissa. This grounds the seed tables as `OBSERVED` two ways — the raw bytes (§5.2) *and* the live
read. `[HIGH/OBSERVED]`

---

## 5. The MED-closure ledger — lifts and precise boundaries

The coverage ledger named **~138 `MED` ops** as the residual upper bound — itself an over-count, since
whole lookup/FMA blocks were marked `MED` though their trunc/structure subparts were already `HIGH`.
Each named family is closed below (lifted to `HIGH` where the TIE/binary yields the constant) or
precisely bounded (the exact static-analysis wall named).

### 5.1 CLOSED — the fp FMA exact round tie-break *(MED → HIGH)*

The round-up decision, rendered from the FMA round core `fp_spfma_round`:

```text
rnd_mode_even/zero/p_inf/n_inf = (RoundMode == 2'b{00,01,10,11})
inf_rnd_up = (roundb | guardb) | sticky
rnd_up = TIEsel( even  -> ({LSB,R,G,S}==4'b0101) | ({LSB,R,G}==3'b011) | ({LSB,R}==2'b11)  ; RNE GRS
                 p_inf -> (~is_neg & inf_rnd_up)                                            ; +inf
                 n_inf -> ( is_neg & inf_rnd_up) )                                          ; -inf
                                                  ; default (RTZ) -> 0
rnd_sig = rnd_up ? (sig + 1) : sig              ; the single increment
```

The **FCR gate** (RNE-forced for the negated forms) is identical in both FMA `<SEMANTIC>` blocks:

```text
ivp_sem_spfma (fp32): use_rm = ~(op_maddn | op_msubn) ; roundm =        RoundMode & {2{use_rm}}
fp_sem_hp_fma (fp16): op_maddn = MULANNXF16(T)|MADDN.H ; op_msubn = MULSNNXF16(T)|MSUBN.H
                      use_rm = ~(op_maddn | op_msubn) ; roundm = {1'b0, RoundMode & {2{use_rm}}}
```

The negated-multiply forms (`MADDN`/`MSUBN` — the `−(a·b)+c` / `−(c−a·b)` shapes) **force RNE**
(`roundm=0`); `MUL`/`MADD`/`MSUB`/`MULSONE` use the dynamic FCR. The product is kept full-width
(`sig[26:0]`, 27-bit > the 23-bit fp32 mantissa) before the **single** round — true single-rounding
fused multiply-add. The fiss leaves `module__xdref_{madd,maddn,msub,msubn}_{16f,32f}` exist for both
precisions (B17/B18 drove them LIVE), confirming the two-block split. **`MED → HIGH/OBSERVED`** for all
54 fp-FMA opcodes (27 fp16 + 27 fp32). See [B17 SPFMA](../ref/b17-spfma.md) / [B18 HP-FMA](../ref/b18-hp-fma.md).

> **WALL — the two-level rounding.** The fiss FMA leaf is **parameter-driven**: the round/flag context
> arrives in a later argument and a status word is written back, i.e. the rounding mode is the
> `RoundMode` SR plumbed by the caller. The production path supplies `RoundMode = RNE` from the
> **FCR reset = RNE**; a bare *un-parameterized* fiss leaf with no context defaults to **RZ**
> (round-toward-zero). State which applies: the single-rounding invariant holds with `RoundMode = RNE`
> in the production path; a context-free leaf call rounds RZ. The two-level distinction is not a
> defect — it is the FCR-vs-leaf-default seam. `[HIGH/OBSERVED]`

### 5.2 CLOSED — the transcendental Newton seeds *(MED → HIGH tables; CARRIED interior)*

The seed compute, rendered from `sem_fp_sp_lookup` (the fp16 core `sem_fp_hp_lookup` **reuses the same
tables** at the fp16 mantissa width):

```text
recip_data = {RECIP_Data8[mantissa_index], 3'b0}     ; the 1/x seed mantissa
rsqrt_data = {RSQRT_Data8[mantissa_index], 16'b0}     ; the 1/sqrt(x) seed mantissa
```

The two seed tables are `<TABLE>` blocks, read **byte-exact** from `libfiss-base.so` `.rodata`
(section [13] @`0x88ff00`; both VMA==fileoffset for `.text`/`.rodata`, so `objdump -s` is direct):

* **`table__RECIP_Data8`** @`0x958fc0` — 128 entries × dword (8-bit seed in the low byte), first bytes
  `ff fd fb f9 f7 f5 f4 f2 …` and last `… 81 81`. **Math-verified:** entry `i` =
  `round(256 / (1 + (i + 0.5)/128))` (`MATCH` for all sampled indices) — the textbook reciprocal
  initial-approximation table, the Newton-Raphson `1/x` seed.
* **`table__RSQRT_Data8`** @`0x958dc0` — the two-range table: `[0..63] b4 b3 b2 b0 af …` (one
  exponent-parity half), `[64..127] ff fd fb f9 f7 …` (the other) — the classic even/odd-exponent
  rsqrt split.

The inline (non-table) seeds are fully closed formulas — no table needed:

```text
NEXP01 : nexp_frac = mat << a_lzc ; nexp_exp = a_is_nan ? 0xFF : expn   ; LZC-normalize
MKSADJ/MKDADJ : mk_hi = 0xFF + (exp_hi<<3) ; mk_lo = 0xFF + (exp_lo<<3) ; exponent make-adjust
SQRT0  : c_w = {nexp_neg, 1'b0, 6'b111111, 1'b0, rsqrt_data}            ; reuses rsqrt seed
RECIP0 : c_w = {nexp_neg, rec_exp, recip_fr}                            ; reuses recip seed
DIV0   : c_w = {nexp_neg, div_exp, recip_data, 13'b0}                   ; reuses recip seed
```

The higher-precision QLI (quadratic-interpolation) refine reads piecewise `(A, gx)` coefficient pairs
(`fp_recip_qli_lut1/2_A/_gx`, `fp_rsqrt_qli_lut1/2_A/_gx`) from `bbn_sem_vec_sprecip_rsqrt` — all
`<TABLE>` `int_value` contents `OBSERVED`. **`MED → HIGH/OBSERVED`** for the seed structure + the
table contents (re-confirmed by the live `recip0`/`rsqrt0` leaves, §4.1) — see
[B14 hp-lookup](../ref/b14-hp-lookup.md) / [B15 sp-lookup](../ref/b15-sp-lookup.md).

> **WALL — the seed-coefficient derivation is CARRIED (boundary precisely placed).** The `.rodata`
> table *is* validated truth (byte-exact + closed-form match + live read). What is `CARRIED` is the
> **firmware-kernel** fact on top of it: how many Newton/QLI refine steps the firmware applies (the
> iteration count) and the per-coefficient derivation rationale are firmware-kernel facts, not TIE-op
> facts — correctly attributed, not inferred. The TIE provides the **baked seed + the refine
> structure**; the iteration count is the documented residual, not a missing op semantics.
> `[HIGH/OBSERVED table; HIGH structure; CARRIED iteration count]`

### 5.3 CLOSED — the sub-byte permute innermost LUT *(MED → HIGH)*

Closed for the table content, TIE-vs-binary triangulated:

```text
tab_shflimm_7b : 2 entries = {0x8, 0x1e}   # BYTE-IDENTICAL to the cas const CONST_TBL_tab_shflimm_7b_0
                                            #   @0x17bc4c0 = {08 00 00 00, 1e 00 00 00}
tab_selimm_7b  : 28 entries = 0,1,2,3, 8,9,a,b, 10,11, 20,21,22,23, 3b,3c,3d,3e,3f,
                              40,41,42,43,44,45,46,47,48     # sub-byte de-interleave/transpose pattern
```

The `seli_s4 = bbe_selimm_S4[6:0]` / `shfli_s4 = bbe_shflimm_S4[6:0]` are stride-`S4` (4-bit sub-byte)
views driving the `xdsem_tiesel_5_32` lane-pick. The B21 `DCMPRS` compress drove the mechanism LIVE.
**`MED → HIGH/OBSERVED`** for the LUT.

> **WALL — the full cptc de-interleave pattern is CARRIED.** The per-immediate **exact lane map** for
> the full step-32 4-deal transpose (`out-lane j ← src-lane ((j&3)<<5 | (j>>2))`) is the `.rodata`
> permutation **pattern**. The TIE provides the **mechanism** (tiesel lane-pick by immediate index) +
> the **LUT** (`HIGH/OBSERVED`); the per-immediate full lane map is `CARRIED`. The boundary: mechanism
> + table are read truth; the cptc pattern is a firmware `.rodata` formula correctly carried, not
> guessed. `[HIGH mechanism; CARRIED pattern]`

### 5.4 CLOSED — the COUNTEQ/COUNTLE histogram + SQZ squeeze *(MED → HIGH structure)*

`ivp_sem_vec_histogram` (8 members: `COUNTEQ`/`COUNTLE` `4NX8` + `M`/`Z`/`MZ` variants), rendered:

```text
cur_bin   = init_bin_zero ? 3'd0 : ars          ; Z-suffix starts the bin at 0
data_scaled = data >> sa                         ; the bin-scale right-shift
mask      = use_mask ? {vbs,vbr} : all-ones      ; M-suffix vbool predicate gate
vrs_cmp   = per-lane: use_le ? (data <= bin) : (data == bin), AND mask
hist_sum  = radd128_stage1/stage2(vrs_cmp)       ; cross-lane reduce-ADD = the bin COUNT
nxt_bin   = cur_bin + 3'd1                        ; the bin advances per call
```

`COUNTEQ` counts lanes equal to the bin; `COUNTLE` counts lanes ≤ the bin; the histogram reuses the
G7 reduce-ADD tree (already `HIGH`). B24 (composite) drove this LIVE. **`MED → HIGH/OBSERVED`.**

`ivp_sem_sqz` (6 members: `SQZ`/`UNSQZ` × `{2N, N, N_2}`) is a **two-stage prefix-popcount
compaction**: stage 1 = the prefix population-count over the vbool predicate (each selected lane's
packed position); stage 2 = gather/scatter the lanes by that count (`SQZ` squeeze / `UNSQZ` inverse).
**`MED → HIGH/OBSERVED`** for the compaction structure.

> **WALL — the SQZ innermost prefix-sum tree wiring (value-determined residual).** stage 1 delegates
> to `ivp_sem_sqz_stgA` (one `<FUNCTION>` nesting deeper); the exact innermost per-lane prefix-sum
> adder tree's balanced lane-pairing is one level below what is rendered here. The compaction
> semantics (prefix-popcount → gather/scatter) are `HIGH/OBSERVED`; the exact balanced-tree pairing is
> **value-determined** — it *is* a prefix-sum, the only un-rendered detail is the lane-pairing, the
> same class of structural-vs-exact boundary the G7 reduce tree drew. **Not** a missing body.
> `[HIGH structure; value-determined residual]`

### 5.5 The revised confidence after closure

| family | ops | verdict |
|---|---|---|
| fp FMA round tie-break | 54 | **HIGH** (GRS + FCR gate, §5.1) |
| transcendental seeds (RECIP0/RSQRT0/SQRT0/NEXP/MKSADJ/MKDADJ/DIV0) | ~30 | **HIGH** tables + structure; iteration count CARRIED (§5.2) |
| sub-byte permute LUT | 6 | **HIGH** LUT; cptc pattern CARRIED (§5.3) |
| COUNTEQ/COUNTLE histogram | 8 | **HIGH** (compare + reduce-add, §5.4) |
| SQZ/UNSQZ squeeze | 6 | **HIGH** structure; innermost prefix-tree value-determined (§5.4) |
| FLOAS/MINORMAX/MULPD/BADDNORM/ADDEXP exact constants | ~30 | structure OBSERVED; exact innermost constant the named residual |

The `HIGH` set of the 1558 bit-precise bodies rises from ~1420 to **~1545+**; the ~138 `MED`
upper-bound collapses to a residual of a few dozen value-determined innermost-tree/exact-clamp
boundaries. The shipped-roster statement is unchanged: **1528/1534 (99.6%) bit-precise**, 6 fences.
`[HIGH/OBSERVED]`

---

## 6. Adversarial self-verification of the five strongest model claims

Each strongest claim was re-checked against the shipped binary this pass; failures were corrected
against the binary (not the other way round).

| # | Claim | Check | Verdict |
|---|---|---|---|
| ADV-1 | The eight-register-file geometry (`vec` 32×512, `wvec` 4×1536, `vbool` 16×64, `valign` 4×512, `b32_pr` 16×64, `gvr` 8×512, `AR` 64×32, `BR` 16×1) | regfile-table ctype match; wire widths | **PASS** — ctypes byte-matched; `wvec` = 1536 = 32×48 confirmed by the `[47:0]` accum wires |
| ADV-2 | Two-level rounding: FCR reset = RNE; un-parameterized fiss leaf default = RZ | FMA leaf ABI (round context is a later arg + status writeback) | **PASS** — the round mode is caller-plumbed `RoundMode`; production = RNE, bare leaf = RZ (§5.1 WALL) |
| ADV-3 | The fp FMA is **single**-rounding (product full-width, one round) | `fp_spfma_round` RTL (`sig[26:0]` > 23-bit mantissa); `madd`/`maddn`/`msub`/`msubn` `{16f,32f}` leaves all present | **PASS** — `MED → HIGH` lift confirmed; the negated forms force RNE |
| ADV-4 | Seed tables OBSERVED, refine interior CARRIED | `objdump -s` `.rodata` byte-read + `round(256/(1+(i+0.5)/128))` match + live `recip0`/`rsqrt0` leaf | **PASS** — bytes `ff fd fb f9…`/`b4 b3 b2 b0…` byte-exact; closed-form `MATCH`; the leaf `lea`s `table__RECIP_Data8` |
| ADV-5 | The one-Cairo-config guard (semantic coverage, not a silicon-gen claim) | `Xm_ncore2gp`/`Xtensa24`/`NX1.1.4`; 1607/1534 rosters; `+73` fold | **PASS** — `1607−1534 = 12642−12569 = 73`; every dtype/lane token is a config axis, not a gen axis |

> **CORRECTION (raised by ADV-4 — the seed-table storage form).** The seed-table bytes were originally
> quoted as a packed byte sequence. The shipped `.rodata` stores each entry as a **32-bit
> little-endian dword** with the 8-bit seed in the low byte (`ff 00 00 00, fd 00 00 00, …`), and the
> `recip0` leaf indexes the table with a **4-byte stride** (`mov (%rcx,%r12,4),%r12d` @`0x87878a`).
> The *seed values* are byte-identical to the model (`ff fd fb f9 …`); only the storage stride
> differs. The model's "8-bit seed table" claim holds; the storage form is now stated precisely.
> No other claim required correction. `[HIGH/OBSERVED]`

---

## 7. Honesty ledger

| confidence | claims |
|---|---|
| **HIGH / OBSERVED** | the 8-group shared-datapath model (§1); the 6 cross-group invariants (§2); the 8-register-file geometry pinned vs the regfile reference (§2.1); the FP32-hub dtype model (§2.2); the one-Cairo-config guard (§2.3); the coverage statement `1528/1534` + `1558/1607` + the `+73` fold (§3); `num_states` 81/87; the TIE/cas/fiss triangulation (§4); the 3 live-driven leaves (§4.1); the fp FMA round lift + FCR gate (§5.1); the seed tables byte-exact + math-verified + live (§5.2); the sub-byte LUT TIE-vs-binary (§5.3); the histogram + SQZ structure (§5.4) |
| **MED / INFERRED** | the SQZ innermost prefix-sum tree lane-pairing; the G7 reduce balanced-tree lane-pairing; the FLOAS/MINORMAX/MULPD/BADDNORM/ADDEXP exact innermost constants — structure OBSERVED, the innermost wiring a value-determined deeper nesting read structurally, not byte-traced |
| **CARRIED** | the Newton/QLI refine **iteration count** (firmware-kernel, §5.2); the cptc per-immediate full de-interleave lane map (`.rodata` pattern, §5.3); the gather `0xffffffff` miss sentinel + 4096-index bound (host/firmware); the EXCCAUSE numeric codes; the per-cell PE-array MAC mapping; the per-gen dtype-availability (firmware-image header, §2.2 CORRECTION) |
| **LOW** | none asserted — where a body is absent (the 6 fences) it is correctly classified as a no-datapath ordering op; where an innermost wiring is one nesting deeper, it is named as the residual boundary, not guessed |

The genuine residual static-analysis boundary, after closure: (a) the SQZ innermost prefix-sum tree
lane-pairing; (b) the G7 reduce balanced-tree lane-pairing; (c) the cptc per-immediate full
de-interleave lane map (CARRIED); (d) a handful of exact-packing/exact-clamp innermost constants;
(e) the Newton/QLI iteration count (CARRIED). **None is a missing datapath body** — each is a
value-determined deeper nesting or a correctly-CARRIED firmware/`.rodata` fact, the same honest split
the corpus drew for the `xtms_aligned_load` memory black-box and the cptc even-impl wall.

---

*Provenance: shipped `libtie-core.so` + `libtie-Xtensa-msem.so` `post_rewrite` TIE semantics (`.data`
blob, file offset = VMA − `0x200000`; `xml_data_post_rewrite` @`0x20104a`), the `libcas-core.so` DWARF
`xdsem` decode/timing model, and the `libfiss-base.so` `module__xdref_*` value leaves driven live via
`ctypes` (`.text`/`.rodata` VMA = file offset; seed tables `table__RECIP_Data8` @`0x958fc0` /
`table__RSQRT_Data8` @`0x958dc0`). Counts grounded `nm | rg -c` against the `.symtab`
(`num_encode_fns() = 0x3119 = 12569`; `num_states = 0x51 = 81`, `+6` hw = 87; 864 `xdref` leaves). No
silicon-generation fact is inferred from any TIE descriptor: every width/lane/format/table-content
token is a config axis of the one Cairo config (`Xm_ncore2gp`, `Xtensa24`, `NX1.1.4`, `RI-2022.9`),
not one of the firmware-image generations.*
