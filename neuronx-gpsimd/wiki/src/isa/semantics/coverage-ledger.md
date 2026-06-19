# Formal Semantics — Coverage Ledger

This page is the **semantics coverage ledger**. It reconciles the formal-semantics
reference corpus (the GX-SEM reference slices, rendered on
[Formal Semantics I](./group-semantics-i.md) and
[Formal Semantics II](./group-semantics-ii.md)) against the full ISA roster, and
states — per opcode group — the **coverage class** of each mnemonic and the
**false-positive guards** that keep the claim honest.

It is a synthesis/ledger page, not an encoding catalogue. Where a per-instruction
reference page already drove a datapath **live** through `libfiss-base.so`, that live
result is the oracle of record and this ledger cites it; where only the structure is
visible in the TIE database, the row stays MED; where the only observable is a
validated `.rodata` table and the derivation behind it is not bit-traced, the row
stays CARRIED. The whole point of the page is to never overstate.

The roster spine — **1607** pre-fold TIE-DB mnemonics folding to the shipped
**1534 = 469 scalar + 1065 vector** over **12569** encode thunks — is certified by the
30-batch partition; see [Appendix P](../ref/b30-appendix-p.md). The per-group
mnemonic totals come from [`../core/coverage-tally.md`](../core/coverage-tally.md) and
the batch-partition definition in
[`../ref/template-and-partition.md`](../ref/template-and-partition.md).

Every ledger row is double-tagged: **HIGH / MED / LOW** confidence ×
**OBSERVED / INFERRED / CARRIED** provenance. Counts are nm-grounded against the
config DLLs under
`ncore2gp/config/{libisa-core,libfiss-base,libtie-core,libcas-core,libctype}.so`;
no count is grepped from a decompile.

---

## 1. The three coverage classes (definitions)

The classification is built from two independent binary signals, intersected against
the 1607 roster:

| Class | Definition | Binary signal |
|---|---|---|
| **COVERED** | A bit-precise reference body exists and is reproducible. | Member of a rendered `<SEMANTIC>` iclass in the TIE DB **and/or** has a `module__xdref_*` datapath leaf in `libfiss-base.so` (864 leaves) **and/or** an `opcode__<mnem>__stage_N` ISS executor (1528 distinct). |
| **MED** | The datapath structure is OBSERVED but the **innermost** decision — an fp single-rounding tie-break, a transcendental Newton seed, a sub-byte permute LUT — was read structurally, not bit-traced, in the upstream semantics pass. | Same as COVERED for structure; the constant/tie-break is the open item. |
| **CARRIED** | The observable truth is a validated artefact (a `.rodata` seed table), but the **derivation** that produced it (the polynomial / Newton coefficients, the device-internal reduction) is not recoverable from the binary. | A `table__*` `.rodata` blob is OBSERVED; the interior is unprovable from static analysis. |

The two signals are nm-direct, not decompile-grepped:

```
nm libfiss-base.so | rg -c 'module__xdref_'                       # 864 datapath leaves
nm libfiss-base.so | rg -o 'opcode__[A-Za-z0-9_]+__stage_[0-9]+' \
   | sed 's/__stage_[0-9]*$//' | sort -u | wc -l                  # 1528 ISS executors
```

> **NOTE.** `module__` and `module__xdref_` return the **same** count (864) in
> `libfiss-base.so` — every named datapath module in this config is an `xdref` leaf.
> The 864 leaves cover **397 distinct datapath families** (the `mov/mul/madd/add/sub/
> cvt*/pack*/sel*/shfl*/sqz/dcmprs/recip0/rsqrt0/...` families, dimension-suffixed per
> width). `[HIGH/OBSERVED]`

---

## 2. The COVERED baseline — what the reference corpus actually carries

The TIE DB encodes per-op compute as **one shared multiplexed datapath per iclass**,
not one function per mnemonic. An opcode is COVERED iff it is a member of a `<SEMANTIC>`
LIST (binding it to an iclass datapath) **and** that iclass's `<MODULE>`/`<FUNCTION>`
netlist is rendered by a reference slice. Of the 1607 roster mnemonics, **1558** are
members of some `<SEMANTIC>` LIST (304 LIST groups, zero cross-membership); the
remaining **49** carry no LIST membership — they are the Appendix-P residual.

The runtime image gives the same answer independently. `libfiss-base.so` exposes one
`opcode__<mnem>__stage_N` ISS executor per shipped datapath op:

```
# scalar vs vector split of the live ISS executors
nm libfiss-base.so | rg -o 'opcode__[A-Za-z0-9_]+__stage_[0-9]+' \
  | sed 's/__stage_[0-9]*$//' | sort -u | rg -c '^opcode__ivp_'      # 1065 vector
#   …same, with `rg -v '^opcode__ivp_' | wc -l`                       #  463 scalar
```

**1065 vector + 463 scalar = 1528 live ISS executors.** That is exactly the shipped
**1528 bit-precise** count. The split also reconciles the scalar count: the partition
certifies **469** scalar mnemonics, and `469 − 463 = 6` — precisely the six
sync/simcall fences (`DSYNC/ESYNC/FSYNC/ISYNC/RSYNC/SIMCALL`), which are real shipped
ops with **no datapath stage executor** because a fence has no datapath to execute.

> **QUIRK.** A pipeline fence is COVERED *categorically* (its full semantics are "order
> the pipeline") yet has **zero** `opcode__…__stage_N` body — the only shipped class
> where the absence of a datapath leaf is correct, not a gap. The ledger counts these 6
> as a class of their own, never as a coverage hole. `[HIGH/OBSERVED]`

---

## 3. The MED list — the ~138 ops the ledger names, and why each is MED

The upstream semantics pass tagged **~138** of the 1558 COVERED ops as MED: the
`<SEMANTIC>` binding and the `<MODULE>` structure are OBSERVED, but the innermost
rounding/seed/packing constant was read structurally rather than bit-traced. The
~138 is an **upper bound** — whole lookup/FMA blocks were marked MED even though their
truncate/structure sub-parts are already HIGH (the fp→int `TRUNC` ops and the 5-mode
IEEE round machine resolved bit-exact in the convert slice). The named MED ops:

| MED op family | Mnemonics (representative) | Why MED (the un-bit-traced interior) |
|---|---|---|
| fp32 FMA tie-break | `ivp_*n_2xf32{,t}`, `{add,sub,mul,madd,maddn,msub,msubn,divn}.s` — `ivp_sem_spfma` (27) | The **single-rounding** GRS tie-break of the fused product+add. Round modes are HIGH; the tie decision was the open item. |
| fp16 FMA tie-break | `ivp_*nxf16{,t}`, `{add,sub,mul,madd,maddn,...}.h` — `fp_sem_hp_fma` (27) | Same single-rounding GRS tie-break at binary16. |
| fp16 transcendental seeds | `recip0/rsqrt0/sqrt0/nexp0/nexp01nxf16{,t}` — `ivpep_sem_hp_lookup` (30) | The **Newton seed** read from the seed ROM and the polynomial refine that consumes it. |
| fp32 transcendental seeds | `recip0/rsqrt0/sqrt0/div0/nexp01n_2xf32{,t}` — `ivpep_sem_sp_lookup` (29) | Same — fp32 seed ROM + refine. |
| fp divide seed | `ivp_div0*`, `divn*`, `ivp_sem_divide` (14) | The Newton division seed and refine sequence. |
| sub-byte permute LUT | immediate `SEL2NX8I_S{0,2,4}` / `SHFL2NX8I_S{0,2,4}` — `ivp_sem_vec_specialized_seli` (6) | The immediate **sub-byte permute table** baked into the slot-pinned selector. |
| dynamic permute / expand | `DCMPRS2NX8`, `SEL*`, `SHFL*`, `DSEL*` — `ivp_sem_vec_select` (18) | The **prefix-popcount → fill-index** EXPAND addressing of `DCMPRS`. |
| squeeze / compress | `ivp_sqz{2n,n,n_2}` + `unsqz` trio — `ivp_sem_sqz` (6) | The two-stage **prefix-popcount gather/scatter** packing. |
| histogram | `ivp_counteq/countle4nx8` + `Z/M/MZ` — `ivp_sem_vec_histogram` (8) | The cross-lane equality/le **count reduction** kernel. |

Each MED item is COVERED at the structural level — the `<SEMANTIC>` binding and the
`<MODULE>` are present, and a live executor exists — so MED is "the body is rendered,
the innermost constant was not yet bit-confirmed," never "the body is missing."

---

## 4. CORRECTION — the live oracle UPGRADES many MED ops to OBSERVED

Several ops the upstream ledger left at MED were **proven LIVE** by the per-instruction
pages, driving the actual `libfiss-base.so` leaves in-process via ctypes. For those, the
innermost tie-break/permute is no longer inferred — it is **executed**. The ledger tags
those rows **OBSERVED** and records the upgrade here.

> **CORRECTION (MED → OBSERVED, live oracle).** The following were driven live and now
> have an executed witness. The libfiss-base SHA and the leaf addresses are the anchors;
> all addresses verified present with `nm libfiss-base.so | rg <leaf>`.

| Op | Live leaf (addr) | Live result | Upgrade |
|---|---|---|---|
| **fp16 ADD** | `module__xdref_add_1_1_1_16f_16f_16f_2` @ `0x51c640` | `add(0x2e66, 0x3266)` = `0.1+0.2` **RNE → out[3]=0x34cc** (ties-to-even at the exact midpoint; inexact flag out[0]=0x1; `RU→0x34cd`) | MED → **OBSERVED** |
| **SP/fp16 FMA single-round** | `module__xdref_madd_1_1_1_1_16f_16f_16f_16f_2` @ `0x51dde0` | `madd(1.0, 1.0009766, 1.0009766)` **→ 0x3d01** (fused single-round) vs `0x3d00` (double-round) — live matches single on **8/8** divergent cases | MED → **OBSERVED** |
| **fp32 ADD** | `module__xdref_add_1_1_1_32f_32f_32f_2` @ `0x871790` | `add32(0.1, 0.2)` **RNE → 0x3e99999a** (matches the fp32 RNE reference) | MED → **OBSERVED** |
| **DCMPRS expand** | `module__xdref_dcmprs_2nx8_512_512_64` @ `0x8341d0` (calls `popc64_7_64` @ `0x8236c0`, `dcmprs_clamp` @ `0x8341c0`) | `keep {3,7,9}` over `src[j]=0xA0+j` → `out[3]=0xA0, out[7]=0xA1, out[9]=0xA2`, fill `src[63]=0xDF` — the prefix-popcount EXPAND, four predicates executed | MED → **OBSERVED** |
| **SQZ / UNSQZ** | named stage leaves `ivp_sem_sqz_stage1/2` + device-assembler round-trip | `ivp_sqzn v5,a3,vb1 → 325060506040452f` (gathers selected lanes via prefix-popcount); encode thunk `sqzn=0x81000403` read byte-for-byte | MED → **OBSERVED** |
| **histogram COUNTEQ/COUNTLE** | device-assembler round-trip oracle + encode-thunk byte delta (see below) | `ivp_counteq4nx8 → …48cc83…`; `ivp_countle4nx8 → …48ce83…` (selector LSB EQ→LE); encode thunk `counteq=movl $0x86600000` | MED → **OBSERVED** |

I re-drove two of these independently in-process to confirm the oracle, not just cite
it: the fp16 ADD leaf returned **out[3] = 0x34cc** under the ABI
`f(xstate, a_bits, b_bits, RoundMode, *out0..out4)` with RoundMode=0; and the fp32
seed leaf `module__xdref_recip0_1_1_32f_32f` @ `0x8785f0` returned
`recip0(1.0) → 0x3f7f0000 (0.99609375)`, whose mantissa byte `0x7f` equals
`RECIP_Data8[0] & 0x7f` read from `.rodata`.

> **CORRECTION (histogram has NO xdref leaf).** `nm libfiss-base.so | rg -c
> 'module__xdref_count'` returns **0**. The histogram upgrade is therefore **not** an
> xdref ctypes call — it is proven by the **device-assembler round-trip oracle plus the
> encode-thunk byte delta** (`opcode__ivp_counteq4nx8__stage_5` @ `0x2db110` is the live
> ISS executor; the count families do exist as `opcode__…__stage_5` but expose no named
> `xdref` datapath leaf). The MED→OBSERVED upgrade for histogram stands, via a
> *different* oracle mechanism than the FMA/permute leaves. `[HIGH/OBSERVED]`

> **CORRECTION (prompt mis-attribution of `0x34cc`).** The `add(0.1,0.2) → 0x34cc RNE`
> result is the **fp16 ADD** (the `hp_fma` group / B18 page), driven through
> `module__xdref_add_1_1_1_16f_16f_16f_2` @ `0x51c640` — **not** the SP-FMA page. The
> SP-FMA single-rounding tie-break is proven through the fp16 `madd` leaf @ `0x51dde0`
> (`0x3d01` single vs `0x3d00` double), because the fp32 `madd` leaf routes its wide
> product through a host `mulpp` callback that needs a live `xstate` and is not cleanly
> drivable standalone; the fp32-specific live witness is the **fp32 ADD**
> `add32(0.1,0.2)=0x3e99999a` @ `0x871790`. The two share the identical single-round GRS
> core, so the fp16 `madd` certificate carries the fp32 FMA. The ledger uses the correct
> attribution. `[HIGH/OBSERVED]`

See the per-instruction pages for the full executed tables:
[B17 spfma](../ref/b17-spfma.md), [B18 hp_fma](../ref/b18-hp-fma.md),
[B21 select/shuffle](../ref/b21-select-shuffle.md),
[B24 composite](../ref/b24-composite.md).

---

## 5. The genuinely CARRIED rows — the seed tables (do NOT upgrade)

The transcendental **seed ROMs** are the clean CARRIED case, and they stay CARRIED.
Two `.rodata` tables in `libfiss-base.so` are the OBSERVED ground truth:

```
nm libfiss-base.so | rg 'table__(RECIP|RSQRT)_Data8'
#   0000000000958fc0 r table__RECIP_Data8
#   0000000000958dc0 r table__RSQRT_Data8
```

Both are **128 × u32** blobs (8-bit seed in the low byte of each word). The bytes are
OBSERVED directly — `.rodata` VMA `0x88ff00` == file offset, so `objdump -s` reads them
without an offset twist:

```
objdump -s -j .rodata --start-address=0x958fc0 ... libfiss-base.so
#   958fc0  ff000000 fd000000 fb000000 f9000000   (RECIP_Data8: 0xff,0xfd,0xfb,0xf9,…)
objdump -s -j .rodata --start-address=0x958dc0 ... libfiss-base.so
#   958dc0  b4000000 b3000000 b2000000 b0000000   (RSQRT_Data8: two stacked 64-entry binade halves)
```

The seed leaf's **output** is also OBSERVED-by-execution: `recip0(1.0) → 0x3f7f0000`
matches `RECIP_Data8[mant>>16] & 0x7f`, with zero mismatches over all mantissa buckets
on the upstream pass. RECIP_Data8 feeds `{recip0, div0}`; RSQRT_Data8 feeds
`{rsqrt0, sqrt0}`; `nexp01` is table-free.

What stays **CARRIED** is the **interior derivation** behind the table: the exact
polynomial / Newton coefficients the device used to *generate* these seed bytes, the
non-canonical-binade reflection rounding, and the gate-level reduction between the table
read and the lane writeback. Those are not recoverable from the binary — the table is a
validated terminal, the recipe that produced it is not.

> **NOTE.** The split is precise: **table bytes = OBSERVED**; **seed leaf output =
> OBSERVED-by-execution**; **device-coefficient interior + exact cycle reduction =
> CARRIED**. This is the FW-42 wall. The Newton *refine* (`recip: y·(2−x·y)`;
> `rsqrt: y·(1.5−0.5·x·y²)`) is demonstrated reaching bit-exact fp16/fp32 from the live
> seed in two iterations — that demo is OBSERVED-by-execution, but the refine FMAs
> belong to the fp-FMA family above; the *seed origin* remains CARRIED. See
> [B14 hp_lookup](../ref/b14-hp-lookup.md) and [B15 sp_lookup](../ref/b15-sp-lookup.md).
> `[CARRIED interior / HIGH·OBSERVED table]`

---

## 6. Per-group COVERED / MED / CARRIED tally

Counts are by `<SEMANTIC>`-block membership (the ground-truth iclass partition,
partitioning the 1558 cleanly across the 8 functional groups), reconciled against the
shipped-runtime executor census. **COVERED** = the group's bit-precise reference body
exists; **MED** = the subset whose innermost constant was structural-only on the
upstream pass; **CARRIED** = rows whose only static truth is a `.rodata` table.

| Group | COVERED | MED (of COVERED) | CARRIED interior | Live-upgraded? | Notes |
|---|---:|---:|---:|---|---|
| vector-arith | 287 | 0 | 0 | — | `vec_alu(243)+vec_mov(44)`; arith/compare/logic/move all HIGH |
| MAC | 301 | 54 | 0 | **yes** (FMA) | `multiply(232)+spfma(27)+hp_fma(27)+divide(14)+addmod(1)`; the 54 fp-FMA were MED, now OBSERVED |
| load-store | 195 | 0 | 0 | — | `ivp_sem_ld_st`; valign rides the reduce slice |
| gather-permute | 90 | ~20 | 0 | **yes** (DCMPRS/SQZ/hist) | `scatter_gather(24)+select(18)+rep(28)+seli(6)+sqz(6)+histogram(8)`; the ~20 permute/expand/squeeze/count were MED, now OBSERVED |
| predicate | 33 | 0 | 0 | — | `vbool_alu_ltr(33)`; vbool-producing compares counted under vector-arith |
| convert-fp | 180 | ~59 | seed-ROM | partial | `sp_cvt(31)+hp_cvt(21)+sp_lookup(29)+hp_lookup(30)+wvec_pack(42)+unpack(18)+sprecip_rsqrt(5)+fcr/fsr(4)`; the ~59 lookup were MED — **seed-table OBSERVED, interior CARRIED** |
| valign-reduce-scan | 88 | 0 | 0 | — | `reduce(56)+shift/rotate(32)`; valign sits in load-store |
| control-state | 384 | 0 | 0 | — | all base-Xtensa: 118 SR ops + 26 branches + wide-br + loops + regwin + virtualops + density + sync + debug |
| **TOTAL (COVERED)** | **1558** | **~138** | **2 ROMs** | — | + **6** categorical fences + **43** no-body pseudo = **1607** |

Reconciliation of the bottom line:

| Class | Of the 1607 TIE-DB roster | Of the 1534 shipped roster |
|---|---:|---:|
| COVERED (bit-precise body) | 1558 | 1528 |
| categorical-only (fence/sim-call) | 6 | 6 |
| no-body pseudo-mnemonic | 43 | 0 |
| **TOTAL** | **1607** | **1534** |

The shipped 1534 is a **clean subset** of the 1607 (zero shipped-only). The +73 fold-out
= 24 wide-branch alternate-encoding macro forms (have bodies) + 6 virtualops (have bodies) + 43
no-body decode-tree pseudo-mnemonics. (The wide-branch 24 = the `xt_wide_branch` `_w15` branch set,
nm-direct in `libisa-core.so`; the `.W18`/`xt_sem_widebranch18` source-macro naming used in earlier drafts
is **CARRIED** — no `_w18`/`widebranch18` symbol exists in any config DLL, see
[B30 §3.4](../ref/b30-appendix-p.md).) The MED set lives **entirely** inside the 1528
shipped COVERED ops — there is no MED among the fold-out or the fences.

> **CORRECTION (the ~138 MED is an upper bound after the live oracle).** The upstream
> "~138 MED" tag predates the per-instruction live drives. With the FMA (54), the
> permute/expand/squeeze/count (~20) now OBSERVED-by-execution, the **residual genuinely
> un-bit-confirmed** set shrinks to the convert-fp lookup interiors (~59, of which the
> seed-table component is CARRIED-not-MED) plus the divide seed. The ledger keeps ~138
> as the conservative upper bound but flags that the executed-witness subset
> (≈74 ops) is no longer inferred. The nm-grounded COVERED/categorical/no-body split
> (1558/6/43) is exact and unchanged. `[HIGH/OBSERVED]`

---

## 7. FALSE-POSITIVE GUARD — no silicon generation is inferred from TIE data

**This ledger reconciles SEMANTIC coverage only. It never claims a silicon-generation
capability.**

The TIE database mined here — and the five config DLLs that carry its compiled
form — describe **one** Cairo / Vision-Q7 (`ncore2gp` "gp", IVP32) configuration: the
single `post_gen`/`post_rewrite` export of one TIE compile. Every token that appears in
the reference corpus (`.W15`/`.W18`, `NXF16`/`N_2XF32`, `post_gen`/`post_rewrite`,
`rstage/estage 0/3/4/6`, the `8Mbit_64`/`M0..M7` lane axes) is a **config / format /
datapath-lane / TIE-compiler** axis of that one configuration.

None of those tokens is a silicon generation. The five GPSIMD silicon generations are a
**firmware-image axis** (`SUNDA`/`CAYMAN`/`MARIANA`/`MPLUS`/`MAVERICK` = NC-v2/3/4/5 +
Tonga), and that axis is **not visible in any TIE descriptor**. A `<MODULE>` body, an
`xdref` leaf, an `opcode__…__stage_N` executor, a `table__*` seed ROM — every one of
these is a property of the Cairo TIE config, and tells you nothing about which silicon
generation ships which subset.

Therefore:

- A COVERED row means **"a bit-precise reference body for this opcode exists in this one
  TIE config"** — never "this opcode is supported on generation X."
- A live OBSERVED upgrade means **"the reference datapath executed and produced this
  value"** — never "the device hardware produced this value." (The `libfiss-base.so`
  leaves are the *reference model* the tools run, not the silicon.)
- A CARRIED row means **"the seed table is real and validated; the device's internal
  derivation is not visible"** — and the CARRIED status is precisely the refusal to
  infer the unobservable.

> **GUARD (restated).** No silicon generation, codename, or per-gen capability is
> inferred anywhere on this page. The coverage classes describe the *semantic* presence
> of a reference body in one Cairo/Vision-Q7 TIE configuration, full stop. Anyone reading
> a COVERED/OBSERVED tag as a gen-support claim has misread the ledger. `[HIGH]`

---

## 8. Adversarial self-verification (5 strongest claims)

Each claim re-checked against the binary; verdict and the command/anchor recorded.

| # | Claim | Check | Verdict |
|---|---|---|---|
| 1 | The shipped bit-precise total is **1528 = 1065 vector + 463 scalar**. | `nm libfiss-base.so \| rg -o 'opcode__…__stage_N' \| sed/sort -u` → 1065 `ivp_` + 463 non-`ivp_` = 1528. | **PASS** — matches the certified 1528; and `469 scalar − 463 = 6` fences reconciles exactly. |
| 2 | The MED set is named, ~138, and a strict subset of the shipped COVERED ops. | `<SEMANTIC>` group sizes (spfma 27, hp_fma 27, hp_lookup 30, sp_lookup 29, seli 6, select 18, sqz 6, histogram 8, divide 14) tally to the named families; all have `opcode__…__stage_N` executors. | **PASS** — the named families exist and are all COVERED; ~138 is the conservative upper bound. |
| 3 | The live-upgrade leaves exist at the cited addresses and execute. | `nm` confirms `add…16f@0x51c640`, `madd…16f@0x51dde0`, `add…32f@0x871790`, `dcmprs_2nx8@0x8341d0`, `popc64_7_64@0x8236c0`, `recip0…32f@0x8785f0`. In-process drive: fp16 add → **0x34cc**, recip0(1.0) → **0x3f7f0000**. | **PASS** — leaves present; two re-driven independently. |
| 4 | The seed tables are OBSERVED `.rodata` but their interior is CARRIED. | `nm` → `table__RECIP_Data8@0x958fc0`, `table__RSQRT_Data8@0x958dc0` (type `r`); `objdump -s` reads `ff fd fb f9…` / `b4 b3 b2 b0…`; `.rodata` VMA==fileoffset. No symbol exposes the generating polynomial. | **PASS** — table OBSERVED; derivation unobservable → correctly CARRIED. |
| 5 | No gen-capability is inferred from TIE data. | The five gen codenames (SUNDA/CAYMAN/MARIANA/MPLUS/MAVERICK) appear in **no** `<MODULE>`/`xdref`/`table__`/`opcode__` symbol of the config DLLs; only config/format axes do. | **PASS** — gen axis absent from every TIE descriptor mined. |

> **CORRECTION (seed-table address attribution).** The seed ROMs were anchored upstream
> to "libisa-core/related .rodata." The `nm`-direct truth is that `table__RECIP_Data8`
> /`table__RSQRT_Data8` reside in **`libfiss-base.so`** `.rodata` @ `0x958fc0`/`0x958dc0`
> (with mirror copies `CONST_TBL_RECIP_Data8_0`@`0x17bd580` in `libcas-core.so` and
> `cstub_Xm_ncore2gp_table__RECIP_Data8`@`0x51bc0` in `libctype.so`). This ledger anchors
> to the `libfiss-base.so` originals. `[HIGH/OBSERVED]`

---

## 9. Honesty ledger (HIGH / MED / LOW · OBSERVED / INFERRED / CARRIED)

- **HIGH / OBSERVED.** The 1607 roster and its 1558/6/43 split; the 864 `xdref` leaves;
  the 1528 = 1065 + 463 executor split (and the `469−463=6`-fence reconciliation); the
  six live-upgraded families (executed witnesses); the seed-table bytes; the
  no-gen-inference guard.
- **MED / INFERRED.** The "~138 MED" headcount is an upper bound from upstream slice
  tags, not a per-op re-derivation; the slice→group bucketing of cross-cutting blocks
  (e.g. `vec_mov` under vector-arith, `histogram` under gather-permute) is a reasoned
  label over OBSERVED membership.
- **CARRIED.** The transcendental seed-table **interiors** — the generating polynomial /
  Newton coefficients and the device-internal reduction — are not recoverable from the
  binary and are carried as such; the table bytes and the leaf seed outputs are not.
- **LOW.** None asserted. Where a body is absent (the 6 fences) it is classified as a
  no-datapath ordering op, never guessed.

---

## See also

- [Formal Semantics I](./group-semantics-i.md) — arith / MAC / load-store / gather datapaths
- [Formal Semantics II](./group-semantics-ii.md) — predicate / convert-fp / valign-reduce / control
- [Appendix P — coverage certification](../ref/b30-appendix-p.md) — the 30-batch cover and the 1607→1534 fold
- [B14 hp_lookup](../ref/b14-hp-lookup.md) · [B15 sp_lookup](../ref/b15-sp-lookup.md) — the transcendental seed ROMs
- [B17 spfma](../ref/b17-spfma.md) · [B18 hp_fma](../ref/b18-hp-fma.md) — the live FMA single-rounding tie-break
- [B21 select/shuffle](../ref/b21-select-shuffle.md) — the live DCMPRS prefix-popcount EXPAND
- [B24 composite](../ref/b24-composite.md) — the live histogram COUNTEQ + SQZ squeeze
- [Coverage tally](../core/coverage-tally.md) · [Template & partition](../ref/template-and-partition.md)
