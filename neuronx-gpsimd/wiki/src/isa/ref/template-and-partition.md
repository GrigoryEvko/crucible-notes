# ISA Reference — Template & 30-Batch Partition

This is the **meta page of Part 3** (the per-instruction ISA reference). It owns three things the
thirty batch pages **B01–B30** all depend on and never re-derive:

1. **The canonical per-instruction page template** — the exact schema every B-page follows, specified
   precisely enough to follow mechanically (§3).
2. **The 30-batch partition** — the op-family → batch map over the **1534 shipped mnemonics**, grounded
   in the `opcodes[]` roster, the `opcodes[].package` census, and the `Opcode_<mn>_Slot_<slot>_encode`
   naming (§4).
3. **The per-mnemonic extraction methodology** a reimplementer runs to fill one roster row — read the
   encode-thunk immediate, map the mnemonic to its `module__xdref_*` value leaf, cross-check against
   `xtensa-elf-objdump` + the libisa decode tables — with a **live, ctypes-driven micro-example** (§5).

It closes with the **roll-up coverage model** (§6): how the 30 batch tallies must sum back to the
certified **`1534 ↔ 12569`** (shipped) / **`1607 ↔ 12642`** (pre-fold) denominators **without ever
cross-pairing them**, and where the `469 / 1065` scalar/vector split and the `1072`-op `xt_ivp32`
package fit.

This page does **not** emit a per-instruction roster of all 1534 mnemonics — that is the body of
B01–B30. It emits the **partition**, the **template**, and the **methodology**. Every count, address,
immediate, and symbol below was re-read or executed against the shipped binaries **this pass**.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED`
= a byte/immediate/symbol read from the shipped binary, or a value **computed by executing the shipped
simulator**; `INFERRED` = reasoned over OBSERVED facts; `CARRIED` = re-used at a cited report's
confidence; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE** (orienting
context).

> **NOTE — the two binaries and their address arithmetic.** The encoding side is
> `libisa-core.so` (`ncore2gp/config/`, sha256 `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451`,
> 9,690,712 B, ET_DYN x86-64, **not stripped**, no DWARF). The value side is `libfiss-base.so`
> (sha256 `260b110c…`, 12,330,016 B). In `libisa-core.so`, `.text` (`0x312c10`) and `.rodata`
> (`0x3b6e40`) are **VMA == file-offset**; `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) and
> `.data` (VMA `0x764040` ↔ file `0x564040`) carry a **per-binary delta of `0x200000`** (`readelf -SW`,
> re-read this pass). The count *accessors* (`num_*` getters) live in `.text` and need no delta; the
> *tables* they index (`opcodes` @ `0x6ce6c0`, `opcodedefs` @ `0x6e9640`, …) live in `.data.rel.ro`,
> file = VMA − `0x200000`. Both libraries are in `extracted/` (gitignored; reach with an absolute path
> or `fd --no-ignore`). **Do not** carry over the `0x400000` `.data` delta from `libtpu.so` — that is a
> different binary; here it is `0x200000`. `[HIGH/OBSERVED]`

---

## 1. What Part 3 is, and where this page sits

Part 2 certified **that** the Vision-Q7 (*Cairo* / `ncore2gp`) encoding is a perfect cover; Part 3
enumerates **what each instruction is**. The denominator is fixed by
[the coverage tally](../../isa/core/coverage-tally.md): **1534 shipped mnemonics**, **12569 placements**,
**864 value leaves**. The object model the reference walks is
[the canonical decode model](../../isa/core/libisa-decode-model.md): `ISA → format → slot → opcode →
iclass → operand → field`, plus the encode matrix `opcodedefs[]`. The byte-level decode of a bundle is
[the FLIX bundle-decoding methodology](../../reference/flix-decoding.md) (Part 0); the FLIX grid is
[the FLIX encoding page](../../isa/core/flix-encoding.md); the registers are
[the eight register files](../../isa/core/register-files.md).

Part 3 takes those foundations and produces **one roster row per shipped mnemonic**, organized into 30
batches. This page is the contract those 30 pages sign: a fixed **template** so the reference reads
uniformly, a fixed **partition** so no mnemonic is dropped or double-counted, and a fixed **extraction
recipe** so every row is binary-grounded the same way.

> **NOTE — the partition axis is the *libisa mnemonic*, not the firmware opcode.** There are **two
> distinct instruction rosters** in this corpus and Part 3 partitions the **larger** one. (a) The
> **libisa host-ISA roster** — the **1534** Vision-Q7 Xtensa+TIE mnemonics in `libisa-core.so`
> (`add`, `ivp_addnx16`, `wur.fsr`, …): the full FLIX-issuable instruction set the disassembler/ISS
> consume. (b) The **firmware kernel-lane opcode roster** — the ~**140** real `NEURON_ISA_TPB_OPCODE`
> values (`TENSOR_TENSOR_ARITH_OP = 0x41`, `POOL = 0x45`, …) the GPSIMD device firmware decodes via the
> 178-entry SEQ dispatch and the `kernel_info_table`. These are **different axes**: the 140 firmware
> opcodes are a coarse compute-instruction enum; the 1534 mnemonics are the fine machine ISA that
> *implements* the kernels. **B01–B30 partition the 1534**, with B30 reconciling the firmware-opcode
> view (Appendix-P pseudo/fence + the kernel-lane completeness ledger). Never sum a `140`-axis count
> into a `1534`-axis tally. `[HIGH/OBSERVED]`

---

## 2. The five denominators every batch inherits (re-grounded this pass)

The numbers a batch closes against, each re-read from the binary this pass — a getter immediate **and**
an `nm` symbol-family count where both exist:

| # | Number | Counts | Binary witness (this pass) | Tag |
|---|--:|---|---|---|
| 1 | **1534** | shipped **mnemonics** | `num_opcodes()` @ `0x3b61d0` = `mov $0x5fe` (=1534); `nm \| rg -o 'Opcode_(.+)_Slot_…_encode' \| sort -u \| wc -l` = 1534 | `[HIGH/OBSERVED]` |
| 2 | **12569** | shipped **placements** (`mnemonic × slot`) | `num_encode_fns()` @ `0x3b6130` = `mov $0x3119` (=12569); `nm \| rg -c 'Opcode_.*_Slot_.*_encode'` = 12569 | `[HIGH/OBSERVED]` |
| 3 | **1607 / 12642** | **pre-fold** authoring mnemonics / placements (TIE-DB) | `1534 + 73`, `12569 + 73`; the `+73` fold forms confirmed absent from the roster | `[HIGH/CARRIED]` on 1607/12642, `[HIGH/OBSERVED]` on `+73` |
| 4 | **864** | execution-validated **value leaves** | `nm libfiss-base.so \| rg -c 'module__xdref_'` = 864 | `[HIGH/OBSERVED]` |
| 5 | **469 / 1065** | scalar / vector mnemonic split (name prefix) | `rg -v '^ivp_'` = 469, `rg '^ivp_'` = 1065, sum 1534 | `[HIGH/OBSERVED]` |

Secondary dimensions a batch quotes (same transcript, this pass): `num_iclasses = 0x5a7 = 1447`,
`num_operands = 0xe8 = 232`, `num_fields = 0xca5 = 3237`, `num_formats = 0xe = 14`,
`num_slots = num_decode_fns = 0x2e = 46`, `num_regfiles = 8`, `num_regfile_views = 4`. `[HIGH/OBSERVED]`

> **GOTCHA — never cross-pair, and never count from the decompile.** The only valid pairings are
> **`1534 ↔ 12569`** (shipped) and **`1607 ↔ 12642`** (pre-fold). Pairing `1534 ↔ 12642` or
> `1607 ↔ 12569` manufactures a `±73` phantom. And every count must be grounded in **either** a `num_*`
> getter immediate **or** an `nm | rg -c` symbol-family population — **never** a grep of the IDA/Hex-Rays
> decompile, which inflates **2–12×** (one thunk is referenced from many call sites). A batch page that
> states a count names its witness. `[HIGH/OBSERVED]`

---

## 3. The canonical per-instruction page template (the B01–B30 schema)

Every batch page B01–B30 is the same document with a different roster. The schema, in order:

### 3.1 Required structure

1. **H1 + scope (1 paragraph).** `# ISA Batch NN — <Family Name>`. The scope paragraph names: the
   op-family the batch owns; the dominant FLIX format(s) and slot(s) those ops occupy; the operand
   regfile(s); and the batch's place in the 30-batch partition (a one-line back-reference to §4 here).
2. **The batch roster table** (the spine — one row per mnemonic the batch owns). Columns, fixed order:

   | column | content | source |
   |---|---|---|
   | `mnemonic` | the `opcodes[].name` string | `opcodes[]` row |
   | `FLIX format(s)·slot(s)` | the slot set the mnemonic is placed in (e.g. `F0·S3_ALU, F1·S3_ALU, N0·S3_ALU`) | `opcodedefs[]` rows for this opcode |
   | `opcode-sel imm` | the encode-template `word0` for a *representative* slot (the `movl $imm` operand) | the `Opcode_<mn>_Slot_<slot>_encode` thunk |
   | `operand regfiles + field bits` | each operand's regfile (AR/vec/vbool/valign/wvec/b32_pr/gvr/—) + its bit-window in that slot | `iclasses[]`→`operands[]`→`fields[]` |
   | `bundle byte-size` | the byte length of the format(s): one of `{2,3,8,16}` | `formats[].length` |
   | `semantics` | one line — what the op computes (dtype/lane-shape aware) | the `module__xdref_*` leaf body |
   | `[conf]` | per-row confidence tag (`HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`) | — |

3. **Per-mnemonic / per-sub-family deep dives.** For each non-trivial op (or each sub-family that shares
   an iclass), a block with:
   * **Encoding bitfield layout** — the selector bits in `word0`, the operand field windows, and
     (for wide slots) the `word1 = 0x00000000` invariant. State the *representative* slot's template and
     the per-format selector drift if the family spreads.
   * **Annotated C pseudocode** naming the **real symbol** it ports — the `module__xdref_<leaf>` value
     body (preferred) **or** the `Opcode_<mn>_Slot_<slot>_encode` thunk template. Pseudocode is the
     *disassembled* body, not invented; it cites the leaf address.
   * **dtype / lane behavior** — element width, lane count (`nx16` = 32×16b over a 512b `vec`, `2nx8` =
     64×8b, `2x32` = 16×32b, etc.), saturation/wrap, signed/unsigned, rounding mode.
   * **QUIRK / GOTCHA / NOTE callouts** — counter-intuitive encodings, reimplementation traps, NaN/edge
     behaviour, fold/alias relationships.

4. **Per-batch coverage tally** (the batch's contribution to the roll-up). Three numbers, each with a
   binary witness: the batch's **mnemonic count** `m`; its **placement count** `p`
   (`Σ` over its mnemonics of `opcodedefs[]` rows); and its **value-leaf count** `v` (the
   `module__xdref_*` leaves its mnemonics resolve to). A one-line statement of how `m` rolls into the
   1534 and `p` into the 12569 (§6).

5. **Cross-links** — to this page, to
   [the coverage tally](../../isa/core/coverage-tally.md), to the relevant Part-2 deep pages
   ([decode model](../../isa/core/libisa-decode-model.md),
   [FLIX encoding](../../isa/core/flix-encoding.md),
   [register files](../../isa/core/register-files.md),
   [fp sub-ISA](../../isa/core/fp-sub-isa.md)), and to the adjacent batches.

### 3.2 The roster-row record format (the OBSERVED ground truth a row distills)

Each roster row distills the per-mnemonic record the slice survey reads from the binary. The canonical
record shape (one mnemonic, with its full placement list) is:

```
<mnemonic>  [opc#<row> iclass#<row>=<iclass-name> pkg=<package> flags=<hex> scalar|VECTOR] placements=<k>
    <mnemonic> : <format> : <Slot_name>       : <word0>[/<word1>] : <unit>   : iclass#<row>
    …                                                                          (one line per placement)
```

This is **exactly** the schema the gateway roster survey produces from `opcodes[]` + `opcodedefs[]` +
the encode thunks. A batch page's roster table is the **compression** of these records: the
`FLIX format(s)·slot(s)` column lists the placement slots, the `opcode-sel imm` column gives the
representative `word0`, the `bundle byte-size` is the format length. The deep dive expands one record in
full where the encoding is non-obvious.

> **NOTE — the encode template is `word0`; the upper lane is always cleared.** Every placement's encoder
> is the **movl-template thunk** `C7 07 <imm32> [ C7 47 04 <imm32> ] C3` — `*(u32*)(rdi+0) = TEMPLATE`,
> optionally `*(u32*)(rdi+4) = WORD1`, `ret`. **`WORD1 == 0x00000000` with zero exceptions across all
> 12569 thunks** (the upper lane carries no opcode-selector bits — it is merely cleared). A batch quotes
> only `word0`; if it shows a wide-slot template it states the `/0x00000000` explicitly. Operand fields
> are deposited *separately* by `field_set`/`operand_encode`, **not** by this thunk. Re-verified this
> pass: `Opcode_mov_n_Slot_f0_s3_alu_encode` @ `0x3386b0` = `movl $0x6498d800,(%rdi); movl $0x0,0x4(%rdi);
> ret`. `[HIGH/OBSERVED]`

> **GOTCHA — a slot's decoded operand width is NOT `next_offset − offset`.** The `_<bitoff>_` token in a
> slot/field symbol marks where the slot's *low byte* starts; the rest of the field scatters across high
> bits. A batch reads the operand bit-window from the `Field_<field>_Slot_<slot>_get` thunk body (the
> `and`/`shr`/`shl` chain), never by subtracting adjacent offsets. `[HIGH/OBSERVED]`

---

## 4. The 30-batch partition of the 1534 mnemonics

The partition is **op-family over the 1534-mnemonic roster**. It is anchored to three OBSERVED
structures, in priority order:

1. **The name prefix** — `ivp_*` (1065 vector) vs non-`ivp_` (469 scalar). This is the **top cut**:
   B01–B24 own the vector ISA, B25–B30 own the base-Xtensa scalar/system ISA. (`rg '^ivp_'` = 1065,
   `rg -v '^ivp_'` = 469, this pass.) `[HIGH/OBSERVED]`
2. **The `opcodes[].package`** — the 28-package census, **parsed directly from the binary this pass**
   (read `opcodes[i].package` at table offset `+0x08` for all 1534 rows). It sums to exactly 1534 and
   pins the base-Xtensa region: `xt_ivp32 = 1072`, `xt_ivpn_scalarfp = 102`, **base-Xtensa (all other 26
   packages) = 360**. `[HIGH/OBSERVED]`
3. **The verb/suffix sub-family** within `xt_ivp32` — the functional split (ALU-int / ALU-fp / MAC /
   load / store / reduce / shift / pack / convert / transcendental / FMA / gather-scatter /
   select-shuffle / divide / composite) the batch boundaries follow, read from the mnemonic root verb
   (`add`, `mul`, `lvn`, `svn`, `cvt`, `recip`, …), the dtype suffix (`nx16`/`2nx8`/`2x32`/`f16`/`f32`),
   and the dominant FLIX slot (`Mul` vs `ALU` vs `LdSt` vs `Ld`). `[HIGH/OBSERVED]` on the root/suffix
   reads; `[MED/INFERRED]` on the exact family boundary where two verbs blur (e.g. plain `ivp_mul*`
   straddles ALU and MAC).

### 4.1 The partition table — B01 → B30

The `family` column is the batch's owned op-family; `axis` is the roster axis (`ivp_` vector / base
scalar / pseudo); `package anchor` is the `opcodes[].package` region it draws from; `≈ mnemonics` is the
**partition target** the batch will pin exactly (see the CORRECTION below). `task` is the batch-page
task id.

| Batch | file | family | axis | package anchor | ≈ mnemonics | task |
|---|---|---|---|---|--:|--:|
| **B01** | `b01-vec-alu-int.md` | Vector ALU — int add/sub/min/max/cmp/logic core | `ivp_` | `xt_ivp32` (int ALU) | ~115 | #616 |
| **B02** | `b02-vec-alu-fp.md` | Vector ALU — fp16/fp32 add/sub/min/max/cmp | `ivp_` | `xt_ivp32` (fp ALU) | ~45 | #617 |
| **B03** | `b03-vec-alu-rest.md` | Vector ALU — B-variant / flag / predicated / abs-diff | `ivp_` | `xt_ivp32` | ~70 | #618 |
| **B04** | `b04-mac-integer.md` | Integer MAC matrix — signed `mul*`/`mula*` | `ivp_` | `xt_ivp32` (Mul slot) | ~110 | #619 |
| **B05** | `b05-mac-mixed.md` | MAC — mixed-sign / complex / wide-acc `muls*`/`mulus*` | `ivp_` | `xt_ivp32` (Mul slot) | ~95 | #620 |
| **B06** | `b06-loads.md` | Vector loads (`lvn*`/`lsn*`/`lsrn*`) + `valign` priming | `ivp_` | `xt_ivp32` (Ld/LdSt) | ~90 | #621 |
| **B07** | `b07-stores.md` | Vector stores (`svn*`/`ssn*`/`sbn*`) | `ivp_` | `xt_ivp32` (LdSt) | ~90 | #622 |
| **B08** | `b08-reduce.md` | Cross-lane reduce (`radd*`/`rmax*`/`rmin*`/`rb*`) | `ivp_` | `xt_ivp32` (ALU) | ~56 | #623 |
| **B09** | `b09-vec-mov.md` | Vector move / regfile bridge (`mov*`/`cp*`) | `ivp_` | `xt_ivp32` | ~27 | #624 |
| **B10** | `b10-wvec-pack.md` | `wvec` pack — wide→narrow readout (`pack*`) | `ivp_` | `xt_ivp32` (`wvec`) | ~42 | #625 |
| **B11** | `b11-vbool-alu.md` | `vbool` ALU / predicate (`b*` boolean ops) | `ivp_` | `xt_ivp32`+`xt_booleans` | ~40 | #626 |
| **B12** | `b12-shift.md` | Vector shift / rotate / normalize (`sll*`/`srl*`/`sra*`/`nsa*`) | `ivp_` | `xt_ivp32` (ALU) | ~39 | #627 |
| **B13** | `b13-sp-cvt.md` | fp32 convert (`cvt*…32`) | `ivp_` | `xt_ivp32` | ~30 | #628 |
| **B14** | `b14-hp-lookup.md` | fp16 transcendental seeds (`recip0.h`/`rsqrt0.h`/`exp`) | `ivp_` | `xt_ivp32` (LUT) | ~18 | #629 |
| **B15** | `b15-sp-lookup.md` | fp32 transcendental seeds (`recip0`/`rsqrt0`/`nexp…32`) | `ivp_` | `xt_ivp32` (LUT) | ~18 | #630 |
| **B16** | `b16-vec-rep.md` | Vector replicate / extract / inject (`rep*`/`splat*`/`inj*`) | `ivp_` | `xt_ivp32` | ~30 | #631 |
| **B17** | `b17-spfma.md` | fp32 fused multiply-add (`fma…32`/`madd…32`) | `ivp_` | `xt_ivp32` (Mul) | ~14 | #632 |
| **B18** | `b18-hp-fma.md` | fp16 fused multiply-add (`fma…16`/`madd…16`) | `ivp_` | `xt_ivp32` (Mul) | ~14 | #633 |
| **B19** | `b19-scatter-gather.md` | SuperGather scatter / gather (`scatter*`/`gather*`) | `ivp_` | `xt_ivp32` (`gvr`/`b32_pr`) | ~24 | #634 |
| **B20** | `b20-hp-cvt.md` | fp16 convert (`cvt*…16`) | `ivp_` | `xt_ivp32` | ~30 | #635 |
| **B21** | `b21-select-shuffle.md` | Select / shuffle / compress (`sel*`/`shfl*`/`dsel*`) | `ivp_` | `xt_ivp32` | ~33 | #636 |
| **B22** | `b22-unpack-wvec-mov.md` | Unpack / `wvec` move (`unpack*`/`wv*` spill) | `ivp_` | `xt_ivp32` (`wvec`) | ~24 | #637 |
| **B23** | `b23-divide.md` | Vector integer divide (`divn*`/`div*` step macros) | `ivp_` | `xt_ivp32`+`xt_integerdivide` | ~22 | #638 |
| **B24** | `b24-composite.md` | Histogram / squeeze / QLI + **scalar-FP FCR/FSR** | mixed | `xt_ivpn_scalarfp`(102)+7 outliers+`xt_ivp32` resid | ~120 | #639 |
| **B25** | `b25-xt-core.md` | base-Xtensa scalar arith / logic / shift | base | `xt_core` (arith) | ~105 | #640 |
| **B26** | `b26-xt-ctrl.md` | base-Xtensa ld/st / branch / density / MUL32 / div | base | `xt_core`+`xt_density`+`xt_mul`+`xt_integerdivide` | ~100 | #641 |
| **B27** | `b27-xt-system.md` | base-Xtensa system / SR / reg-window / sync | base | `xt_core`(SR)+`xt_regwin`+`xt_sync`+`xt_externalregisters` | ~75 | #642 |
| **B28** | `b28-xt-exc.md` | base-Xtensa exc-dispatch / bool / loop / minmax | base | `xt_exception_dispatch`(37)+`xt_booleans`+`xt_wide_loop` | ~50 | #643 |
| **B29** | `b29-xt-system2.md` | base-Xtensa debug / timer / cache / MMU / atomic | base | `xt_debug`(33)+`xt_timer`+`xt_mmu`+`xt_instcache`+… | ~75 | #644 |
| **B30** | `b30-appendix-p.md` | Appendix-P pseudo / fence + kernel-lane reconciliation + final coverage | pseudo | `xt_virtualops`(10)+`xt_wide_branch`(24) base + the `+73` fold | ~35 | #645 |

> **CORRECTION — the `≈ mnemonics` are partition *targets*, not pre-counted truth; the batch pins the
> exact count.** The exact membership of each batch is determined by the **precise slice classifier the
> batch author runs over the roster** (the first-match-wins verb/suffix/package rule of §4.2). The `≈`
> figures here are derived from a deterministic first-pass classification of the 1534-row roster (run
> this pass) plus the package census, and are accurate to the family but **soft at the boundary** where
> two verbs blur (plain `ivp_mul*` between B04/B05; abs-diff between B01/B03; `move` between B09/B11).
> Each B-page is responsible for stating its **OBSERVED** `m` (from `nm`/roster) and proving the
> partition closes (§6). The hard, non-negotiable anchors the targets must respect: the vector axis
> totals **1065**, the scalar axis **469**, and the base-Xtensa packages **360**. `[HIGH/OBSERVED]` on
> the three hard anchors; `[MED/INFERRED]` on the per-batch `≈` split.

### 4.2 How each batch's membership is derived (the classifier)

A batch author reproduces the partition mechanically:

```
for each mnemonic m in roster (1534, C-locale sorted):
    if m starts "ivp_":                       # vector axis (1065)
        n = m without "ivp_"
        # priority order — first match wins:
        if "scatter" in n or "gather" in n:        -> B19
        elif n matches load-verb (lvn|lsn|lsrn|la|lvnx|…):   -> B06
        elif n matches store-verb (svn|ssn|sbn|sv|ss|…):     -> B07
        elif n matches (mula|mac|qmul):                       -> B04   # signed int MAC
        elif n matches (muls|mulus|mulsgn|mulq):              -> B05   # mixed/complex/wide MAC
        elif n matches (fma|madd|msub):     -> B18 if fp16 else B17
        elif n matches (recip|rsqrt|sqrt|nexp|exp): -> B14 if fp16 else B15
        elif n matches div:                                   -> B23
        elif n matches (cvt|round|trunc|float|fix): -> B20 if fp16 else B13
        elif n starts "pack":                                 -> B10
        elif n matches (radd|rmax|rmin|rb|rsum):              -> B08
        elif n matches (sll|srl|sra|nsa|rot|norm|sh):         -> B12
        elif n matches (sqz|qli|hist):                        -> B24
        elif n matches (rep|splat|bcast|inj):                 -> B16
        elif n matches (sel|shfl|dsel|extr|compr|zip):        -> B21
        elif n matches (move|mov|cp):                         -> B09
        elif n matches (unpack|wv|spill):                     -> B22
        elif n matches boolean (b<letter>):                   -> B11
        elif fp-typed (f16/f32/xf):  -> B02 (fp ALU)
        else:                        -> B01 (int ALU) / B03 (residual variant)
    else:                                     # base scalar axis (469)
        classify by opcodes[].package:
            xt_ivpn_scalarfp (102) + 7 xt_ivp32 outliers  -> B24  (scalar-FP)
            xt_core arith/logic/shift                      -> B25
            xt_core ld/st/branch + xt_density + xt_mul + xt_integerdivide -> B26
            xt_core SR + xt_regwin + xt_sync + xt_externalregisters       -> B27
            xt_exception_dispatch + xt_booleans + xt_wide_loop           -> B28
            xt_debug + xt_timer + xt_mmu + xt_instcache + xt_instram/dataram/prefetch/coprocessors/trace/interrupt -> B29
            xt_virtualops + xt_wide_branch (the pseudo/fold base forms)   -> B30
```

The vector batches (B01–B23) carve **`xt_ivp32`** by verb; B24 collects the **`xt_ivpn_scalarfp`**
scalar-FP region (the `abs.h`/`add.s`/`mulsone.h`/`recipqli.s` family — 102 + the 7 `xt_ivp32` non-`ivp_`
outliers `{rur/wur.fcr, rur/wur.fsr, recipqli.s, mulsone.s, mulsone.h}`) together with the `ivp_`
composites (`sqz`/`qli`/`hist`). B25–B29 carve the **360 base-Xtensa** mnemonics by package; B30 owns the
`xt_virtualops` + `xt_wide_branch` base forms and reconciles the firmware-opcode view. `[HIGH/OBSERVED]`
on the package carve; `[MED/INFERRED]` on the `xt_ivp32` verb carve boundaries.

> **GOTCHA — the scalar-FP `.h`/`.s` ops are package `xt_ivpn_scalarfp`, not `xt_ivp32`, and are
> *scalar* by name.** All **102** `xt_ivpn_scalarfp` ops are non-`ivp_`-prefixed (`abs.h`, `add.s`,
> `recip0.h`, …) — they count toward the **469 scalar** total, not the 1065 vector total, even though
> they are floating-point. Conversely, **7** ops *are* package `xt_ivp32` but lack the `ivp_` prefix
> (`rur.fcr`, `wur.fcr`, `rur.fsr`, `wur.fsr`, `recipqli.s`, `mulsone.s`, `mulsone.h`) — so
> `package == xt_ivp32` (1072) is **not** the same predicate as `name starts ivp_` (1065): they differ
> by exactly these 7. B24 owns both groups (scalar-FP), which is why its target is large. `[HIGH/OBSERVED]`

> **QUIRK — B30 reconciles the firmware-opcode axis but adds *no* 1534-axis mnemonics beyond its base
> forms.** Appendix-P pseudo-ops (`NEURON_ISA_TPB_OPCODE_PSEUDO_*`, 0xC1–0xDF, 31 of them) are
> **compiler-generated and NRT-translated to HW** — they have **no `opcodes[]` row, no encode thunk, no
> value leaf** in `libisa-core.so`, so they are **not** in the 1534. B30 documents them as a *firmware*
> reconciliation (the kernel-lane ledger: 172 union firmware-opcode values → 140 real HW after removing
> 31 pseudo + 1 `INVALID`), and its **1534-axis** membership is only the shipped `xt_virtualops` (10) +
> `xt_wide_branch` (24) base forms whose `.W18`/virtual *variants* fold (§6). The `SortMerge` op is a
> **phantom** — named only in a dead `// "SortMerge wip 0x97"` comment, with no `opcodes[]` row
> (`rg -ci sortmerge` over the roster = 0); B30 records it as a fabrication wall, never a row.
> `[HIGH/OBSERVED]`

---

## 5. The per-mnemonic extraction methodology

To fill **one** roster row, a reimplementer runs five binary reads, three of them cross-checks. The
recipe is identical for every mnemonic; only the symbol names change.

### 5.1 The five steps

1. **Find the placements.** Scan `opcodedefs[]` (`@0x6e9640`, stride 24, file `0x4e9640`) for rows whose
   `opcode` name (offset `+0x00`) equals the mnemonic; collect each row's `slot` name (`+0x08`) and
   `encode_fn` pointer (`+0x10`). Equivalently, `nm libisa-core.so | rg "Opcode_<mangled>_Slot_.*_encode"`
   (mangling: `'.'→'_'`, slot token lowercased — `wur.fsr` → `Opcode_wur_fsr_Slot_inst_encode`). The
   count of rows is the mnemonic's **placement count** `k`.
2. **Read the opcode-selector template.** For each placement's `encode_fn` (`.text`, VMA==file), read the
   thunk bytes `C7 07 <imm32> [ C7 47 04 <imm32> ] C3`: `word0` = the `movl` operand = the
   opcode-selector for `(mnemonic, slot)`; `word1` = `0x00000000` (invariant). The roster row's
   `opcode-sel imm` is a representative `word0`.
3. **Resolve the operand model.** `opcodes[].iclass` (offset `+0x10`) → `iclasses[]` row → its
   `operands[]` sub-array → each `operands[].field` → the `Field_<field>_Slot_<slot>_get` thunk body =
   the operand's bit-window in that slot, and `operands[].regfile` = its file (AR/vec/vbool/valign/wvec/
   b32_pr/gvr, or `""` for an immediate). `OperandSem_<x>_decode` gives the value transform
   (sign-extend/scale/bias/PC-rebase).
4. **Map mnemonic → value leaf.** Find the `module__xdref_<leaf>` symbol in `libfiss-base.so` whose root
   matches the mnemonic and whose `_<outbits>_<inbits…>` suffix matches the dtype. The leaf **is** the
   per-element value function; its disassembled body is the row's `semantics`.
5. **Cross-check three ways.** (a) **Decode inverse:** confirm the slot's `Slot_<slot>_decode` classifier
   (`decodes[]`, 46 of them) maps `word0` back to the mnemonic. (b) **Device objdump:** assemble/disassemble
   the bundle with `xtensa-elf-objdump`/`xtensa-elf-as` under `XTENSA_SYSTEM=…/XtensaTools/config
   XTENSA_CORE=ncore2gp` and confirm byte/mnemonic agreement. (c) **Value execution:** call the
   `module__xdref_*` leaf via ctypes on an input sweep and diff against an independent model — a bit-exact
   match is an OBSERVED-by-execution certificate.

> **NOTE — `instruction_mapping.json` cross-checks the *firmware* axis, not the libisa mnemonic.** The
> per-gen `instruction_mapping.json` (in the customop-lib headers) is a `struct ↔ NEURON_ISA_TPB_OPCODE`
> map (e.g. `NEURON_ISA_TPB_CTRL_BR_STRUCT → NEURON_ISA_TPB_OPCODE_COMPARE_BRANCH`) over the ~140
> firmware opcodes — it is the right cross-check for the **B30 kernel-lane reconciliation** and for any
> firmware-facing semantics, but it does **not** index the 1534 libisa mnemonics. For a B01–B24 vector
> row, the value cross-check is the `module__xdref_*` leaf; for a firmware-opcode mapping, it is
> `instruction_mapping.json`. Keep the two axes separate. `[HIGH/OBSERVED]`

### 5.2 Worked micro-example — `abs` (8-bit), driven live through `libfiss-base`

Take the absolute-value op at 8-bit element width. Step 4 finds the value leaf
`module__xdref_abs_8_8` (`@0x5c0b60` in `libfiss-base.so`). Its disassembled body, re-read this pass:

```asm
00000000005c0b60 <module__xdref_abs_8_8>:
  5c0b60: 40 f6 c6 80     test  $0x80,%sil          ; if (in & 0x80)         — sign bit set?
  5c0b64: 74 06           je    5c0b6c              ;   skip
  5c0b66: f7 de           neg   %esi                ;   in = -in
  5c0b68: 40 0f b6 f6     movzbl %sil,%esi          ;   in &= 0xff           — re-narrow to 8b
  5c0b6c: 89 32           mov   %esi,(%rdx)         ; *out = in
  5c0b6e: c3              ret
```

The ABI is **`void leaf(int lane /*ignored here*/, int in, int *out)`** — `%esi` = input, `*%rdx` =
output (3-input ops add a `%edx` and shift the out-pointer to `%rcx`, as `module__xdref_add_8_8_8`
@`0x5bc380` shows: `add %esi,%edx ; and $0xff,%edx ; mov %edx,(%rcx)`). The leaf is **license-free,
callable in-process** — so it can be *proven by execution*, not merely decoded. Driven live via ctypes
this pass:

```python
import ctypes
lib = ctypes.CDLL("libfiss-base.so")
f = lib.module__xdref_abs_8_8
f.restype = None
f.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
def abs8(x):
    out = ctypes.c_int(0)
    f(0, x & 0xFF, ctypes.byref(out))   # arg0=lane(ignored), arg1=in, arg2=*out
    return out.value
```

Sweep (OBSERVED-by-execution): `abs8(0x01)=0x01`, `abs8(0x7f)=0x7f`, `abs8(0x80)=0x80`,
`abs8(0x81)=0x7f`, `abs8(0xff)=0x01`, `abs8(0xc0)=0x40`. This is **signed-8 magnitude with the
`0x80 → 0x80` two's-complement edge** (`−128` has no positive 8-bit form, so it returns itself — a real
GPSIMD edge a reimplementer must replicate). A **full 256-input differential** against the disassembled
model (`((-x)&0xff) if (x&0x80) else x`) returned **0 mismatches**; the companion
`module__xdref_add_8_8_8` over the **full 65,536-pair space** matched `(a+b)&0xff` with **0 mismatches**.
Both are OBSERVED-by-execution certificates — the value column of two roster rows, proven not inferred.
`[HIGH/OBSERVED by execution]`

> **QUIRK — the `_<outbits>_<inbits>` suffix is the dtype contract, and the first argument is the
> lane/context slot.** `abs_8_8` is `out8(in8)`; `abs_16_16`/`abs_32_32` are the 16/32-bit siblings;
> `abs_16f_16f`/`abs_32f_32f` are the fp16/fp32 forms (different bodies — the fp32 form is the branchless
> `shr$0x1f ; neg ; xor ; add` idiom @`0x5c0b70`). A batch row reads the **leaf whose suffix matches the
> placement's dtype**, not a single "abs". The leading integer argument is a lane/context selector the
> simple element leaves ignore but the lane-dependent leaves consume — pass `0` for the scalar
> element-function check. `[HIGH/OBSERVED]`

---

## 6. The roll-up coverage model

The 30 batches partition the 1534, and their tallies must close back onto the certified denominators
**without cross-pairing**. The model a reimplementer audits the reference against:

### 6.1 The two axes sum independently

```
   B01 … B23  (vector verb families)   ┐
   B24 (vector composite + scalar-FP)  ├─ vector axis = 1065 (ivp_)
   B25 … B29  (base-Xtensa by package) ┘   scalar axis =  469  (= 109 scalar-FP + 360 base-Xtensa)
   B30 (pseudo/fold base forms)        ───────────────────────────────────────────────────────────
                                                              TOTAL = 1534 shipped mnemonics
```

The exact bookkeeping, by hard anchor:

* **Vector axis = 1065.** B01–B23 plus the `ivp_` composites in B24 partition the **1065** `ivp_`-prefix
  mnemonics. (Equivalently, `xt_ivp32` = 1072 minus the 7 non-`ivp_` outliers that B24 takes as
  scalar-FP.)
* **Scalar-FP = 109** (B24's scalar half) = **102** `xt_ivpn_scalarfp` + **7** `xt_ivp32` non-`ivp_`
  outliers.
* **Base-Xtensa = 360** (the 26 non-vector, non-scalar-FP packages) = B25 ∪ B26 ∪ B27 ∪ B28 ∪ B29 ∪
  (B30's `xt_virtualops` 10 + `xt_wide_branch` 24 = 34). Each B-page pins its package slice exactly; the
  sum is the OBSERVED **360**.
* **Scalar axis = 469** = **109** scalar-FP (B24) + **360** base-Xtensa (B25–B30). `109 + 360 = 469`. ✓
* **Grand total** = **1065** (vector) + **469** (scalar) = **1534**. ✓

### 6.2 The placement roll-up pairs `↔ 12569`, never `12642`

Each batch also tallies **placements** `p_i = Σ` (over its mnemonics) of `opcodedefs[]` rows. The 30
placement tallies sum to **12569** (`num_encode_fns = 0x3119`), the **shipped** total. The pre-fold
**12642** is the TIE-DB authoring superset (`12569 + 73`); a batch that touches a fold-source package
(`xt_wide_branch`, `xt_virtualops` — both in B30) notes the `+73` authoring forms **do not ship** and so
do **not** enter its `p`. The valid pairings stay **`1534 ↔ 12569`** (the batches' running total) and
**`1607 ↔ 12642`** (the authoring superset); the reference never carries `1534 ↔ 12642`. `[HIGH/OBSERVED]`

### 6.3 The value-leaf roll-up

Each batch's value-leaf tally `v_i` (the `module__xdref_*` leaves its mnemonics resolve to) rolls into
the **864** value-leaf denominator. Value leaves are *fewer* than mnemonics (one leaf serves a whole
dtype-family of mnemonics, e.g. `abs_8_8` underlies every 8-bit `abs` placement), so `Σ v_i ≤ 864`; the
base-Xtensa batches (B25–B30) contribute few or no value leaves (their semantics are the architecturally
standard Xtensa core, validated by the objdump round-trip, not by `xdref` execution). The reference's
value-coverage claim is `864/864` enumerated, ~95% execution-validated, **0 value bugs across ~2.09M
comparisons** — inherited from [the coverage tally §5](../../isa/core/coverage-tally.md). `[HIGH/OBSERVED]`

### 6.4 The completeness invariant each batch proves

A batch page is complete when: (a) every mnemonic in its partition slice has a roster row; (b) `Σ`
placements over its rows equals its `nm`-counted placement total; (c) every value-bearing row names a
resolved `module__xdref_*` leaf or is flagged as a base-ISA op with no leaf; and (d) the running
**`Σ m`** across B01..B30 reaches **1534** and **`Σ p`** reaches **12569** with zero slack. B30 carries
the final audit line. `[HIGH/INFERRED]` (the invariant is a derived contract over the OBSERVED denominators).

---

## 7. Adversarial self-verification — the five strongest claims, re-checked this pass

1. **`1534 = 469 scalar + 1065 vector`, `12569` placements.** `num_opcodes()` @ `0x3b61d0` =
   `mov $0x5fe` (1534); `num_encode_fns()` @ `0x3b6130` = `mov $0x3119` (12569);
   `nm | rg -c 'Opcode_.*_Slot_.*_encode'` = **12569**; `nm | rg -o 'Opcode_(.+)_Slot_…_encode' | sort -u
   | wc -l` = **1534**; `rg '^ivp_'` = **1065**, `rg -v '^ivp_'` = **469** over the roster. Five witnesses
   agree. **Confirmed.** `[HIGH/OBSERVED]`
2. **The 28-package census sums to 1534; `xt_ivp32 = 1072`, `xt_ivpn_scalarfp = 102`, base-Xtensa = 360,
   7 outliers.** Parsed `opcodes[i].package` (`+0x08`) for all 1534 rows directly from the binary this
   pass: `xt_ivp32 = 1072`, `xt_ivpn_scalarfp = 102`, sum-of-all = **1534**, 28 distinct packages,
   base-Xtensa (all but the two vector packages) = **360**; the 7 `xt_ivp32` non-`ivp_` outliers are
   exactly `{mulsone.h, mulsone.s, recipqli.s, rur.fcr, rur.fsr, wur.fcr, wur.fsr}`. **Confirmed.**
   `[HIGH/OBSERVED]`
3. **The encode-thunk ABI is `C7 07 imm32 [C7 47 04 imm32] C3` with `word1 == 0`.** Re-disassembled this
   pass: `Opcode_wur_fsr_Slot_inst_encode` @ `0x341110` = `movl $0xf3e900,(%rdi); ret` (= `WUR | FSR<<8`,
   UR=0xe9); the 2-lane `Opcode_mov_n_Slot_f0_s3_alu_encode` @ `0x3386b0` = `movl $0x6498d800,(%rdi);
   movl $0x0,0x4(%rdi); ret` — `word1 = 0`. **Confirmed.** `[HIGH/OBSERVED]`
4. **The value leaf is proven by execution, bit-exact.** `module__xdref_abs_8_8` driven live via ctypes
   over all 256 inputs matched its disassembled model with **0 mismatches**, including the `0x80 → 0x80`
   two's-complement edge; `module__xdref_add_8_8_8` matched `(a+b)&0xff` over all 65,536 pairs with **0
   mismatches**. `nm libfiss-base.so | rg -c 'module__xdref_'` = **864**. **Confirmed.**
   `[HIGH/OBSERVED by execution]`
5. **The `.data.rel.ro` delta is `0x200000`, not `0x400000`.** `readelf -SW` this pass: `.text` VMA ==
   file `0x312c10`; `.rodata` VMA == file `0x3b6e40`; `.data.rel.ro` VMA `0x67bb00` ↔ file `0x47bb00`;
   `.data` VMA `0x764040` ↔ file `0x564040` — delta `0x200000`. The `0x400000` figure belongs to
   `libtpu.so`, a different binary. **Confirmed.** `[HIGH/OBSERVED]`

**What I could not ground to OBSERVED at the row level.** The per-batch `≈ mnemonics` in §4.1 are
partition **targets** derived from a first-pass verb/suffix/package classifier plus the package census —
`[MED/INFERRED]` at the family boundary, **not** exact `nm`-counted memberships. The three *hard* anchors
(vector 1065, scalar 469, base-Xtensa 360) and the package census are `[HIGH/OBSERVED]`; the exact split
of `xt_ivp32`'s 1072 ops across B01–B24 is the **batch authors' job to pin** from `nm`. No claim here
asserts a precise per-batch count as OBSERVED.

---

## 8. Function & symbol map

All in `libisa-core.so` unless noted. `.text`/`.rodata`: VMA == file. `.data.rel.ro`/`.data`: file =
VMA − `0x200000`.

| Symbol / table | Addr (VMA) | Role |
|---|---|---|
| `num_opcodes` | `0x3b61d0` | `mov $0x5fe` → 1534 mnemonics |
| `num_encode_fns` | `0x3b6130` | `mov $0x3119` → 12569 placements |
| `num_regfiles` / `num_regfile_views` | `0x3b5c20` / `0x3b5d50` | 8 / 4 |
| `num_iclasses`/`num_operands`/`num_fields` | `0x3b5fb0`/`0x3b5e80`/`0x3b5b40` | 1447 / 232 / 3237 |
| `num_formats`/`num_slots`/`num_decode_fns` | `0x3b65e0`/`0x3b6510`/`0x3b64c0` | 14 / 46 / 46 |
| `opcodes` | `0x6ce6c0` (.data.rel.ro) | 1534 × `{name,package,iclass,flags,…}`, stride 72 |
| `opcodedefs` | `0x6e9640` (.data.rel.ro) | 12569 × `{opcode,slot,encode_fn}`, stride 24 |
| `Opcode_wur_fsr_Slot_inst_encode` | `0x341110` | template `0x00f3e900` (`WUR\|FSR<<8`) |
| `Opcode_mov_n_Slot_f0_s3_alu_encode` | `0x3386b0` | 2-lane thunk, `word1 = 0` |
| `module__xdref_abs_8_8` | `0x5c0b60` (`libfiss-base.so`) | signed-8 magnitude value leaf (§5.2) |
| `module__xdref_add_8_8_8` | `0x5bc380` (`libfiss-base.so`) | mod-256 add value leaf |
| `xtensa-elf-objdump` | `tools/XtensaTools/bin/` | device round-trip oracle (`XTENSA_CORE=ncore2gp`) |

---

## 9. Cross-references

* [ISA Coverage & the 1534/1607/12642 Tally](../../isa/core/coverage-tally.md) — the certified
  denominators (1534/12569/864), the 28-package census, the `+73` fold, the no-cross-pair law.
* [The Canonical ISA Decode Model (libisa-core)](../../isa/core/libisa-decode-model.md) — the
  `formats → slots → opcodes → iclasses → operands → fields` object model + `opcodedefs[]` encode matrix
  this template walks.
* [The libisa Table Schema & Codec ABI](../../isa/core/libisa-table-schema.md) — the struct layouts /
  strides / encode-template ABI the extraction recipe reads.
* [The FLIX VLIW Encoding](../../isa/core/flix-encoding.md) — the 14-format / 46-slot grid and the
  per-slot placement census the roster's `format·slot` column draws from.
* [The Eight Register Files](../../isa/core/register-files.md) — the AR/vec/vbool/valign/wvec/b32_pr/gvr
  files the roster's operand column names.
* [The FP Sub-ISA (FCR/FSR, RNE/RZ)](../../isa/core/fp-sub-isa.md) — the scalar-FP rounding state B24's
  `xt_ivpn_scalarfp` family plumbs.
* [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) (Part 0) — the byte-level decode
  the device-objdump cross-check leg validates against.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags and the named walls
  (`F4`/`F6` interiors, FLIX-desync, v5, `MODULE_SCHEDULE`, `SortMerge` phantom).
* **Forward — the thirty batch pages B01–B30** (`b01-vec-alu-int.md` … `b30-appendix-p.md`): each follows
  the §3 template, owns the §4 partition slice, and closes onto the §6 roll-up.
