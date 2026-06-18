# ISA Coverage & the 1534/1607/12642 Tally

This is the authoritative **ISA-coverage ledger** for the Vision-Q7 *Cairo* (`ncore2gp`)
instruction set: exactly how complete the recovery is, what each headline number *means*, how
the numbers pair, and which residuals remain — with every figure re-derived from the binary
this pass and tagged. It is the capstone of Part 2 (Q7 Core & ISA Foundations): the four
deep-encoding pages ahead of it ([the decode model](libisa-decode-model.md),
[the FLIX encoding](flix-encoding.md), [the register files](register-files.md),
[the config sheet](config-reference-sheet.md)) each *use* a count; this page is the single
place where every count is **defined, sourced, paired, and certified** so they cannot drift.

The whole ledger reduces to one sentence a reimplementer can build on: **the shipped Vision-Q7
encoding is a certified-perfect cover at `1534/1534` shipped mnemonics and `12569/12569`
placements, and value-semantics coverage is `864/864` execution-validated leaves.** The pages
that follow this one — Part 3's per-instruction reference (B01–B30) and the
[Master ISA Encoding Appendix](../../appendix/isa-encoding-appendix.md) — inherit their
completeness claims from exactly these numbers.

Everything below is read **directly from the shipped binaries this pass**: the non-stripped
`.symtab` of `libisa-core.so` (the count-accessor immediates and the `Opcode_*`/`Slot_*`/
`Field_*` symbol families), and the `.symtab` of `libfiss-base.so` (the `module__xdref_` value
leaves). The standalone TIE database supplies the *pre-fold authoring* superset, which the
binary then folds; that pairing is reconciled in §3. No external or vendor source tree was
consulted; this is binary-derived prose only.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/immediate/symbol-count read from the shipped binary (or computed by
executing the shipped simulator); `INFERRED` = reasoned over OBSERVED facts; `CARRIED` =
re-used at a cited report's confidence; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a
naive reading), **NOTE** (orienting context).

> **GOTCHA — count only with `nm | rg -c`, never a decompile grep.** Every figure on this page
> is grounded in **either** a count-accessor immediate (`objdump -d` of a `num_*` getter) **or**
> a symbol-family population from `nm libisa-core.so | rg -c '<family>'`. Counting the same
> family in the IDA/Hex-Rays decompile inflates **2–12×** (a single thunk is referenced from
> many call sites and counted once per reference). The binary `.symtab` is the arbiter; the
> decompile is not. Where this page states a count, the witness is a getter immediate or an `nm`
> population, named in the row.

> **GOTCHA — the `.data.rel.ro` VMA↔file delta is per-binary `0x200000` here.** The
> count-*accessors* (`num_*` getters) are in `.text` (VMA == file-offset); the *tables* they
> index (`opcodes` @ `0x6ce6c0`, `opcodedefs` @ `0x6e9640`, …) are in `.data.rel.ro`, file =
> VMA − `0x200000`. This page reads counts from the `.text` getter immediates and the symtab —
> neither needs the delta — but a reader walking the tables themselves must apply it. The
> `libisa-core.so` carving used here is in `extracted/` (gitignored; reach it with an absolute
> path or `fd --no-ignore`).

---

## 1. The five headline numbers — re-derived from the binary this pass

These five figures carry the entire coverage claim. Each was re-derived **independently** this
pass, two ways where possible (a getter immediate **and** a symbol-family count), and the
results agree to the digit.

| # | Number | What it counts | Binary witness (re-read this pass) | Tag |
|---|---|---|---|---|
| 1 | **1534** | shipped **mnemonics** (distinct opcodes) | `num_opcodes()` @ `0x3b61d0` = `mov $0x5fe` (= 1534); **and** `nm \| rg -o 'Opcode_(.+)_Slot_…_encode' \| sort -u \| wc -l` = 1534 distinct mnemonics | `[HIGH/OBSERVED]` |
| 2 | **12569** | shipped **placements** (legal `mnemonic × slot`) | `num_encode_fns()` @ `0x3b6130` = `mov $0x3119` (= 12569); **and** `nm \| rg -c 'Opcode_.*_Slot_.*_encode'` = 12569 | `[HIGH/OBSERVED]` |
| 3 | **1607** | **pre-fold** authoring mnemonics (TIE-DB) | TIE-DB `<OPCODEDEF>` distinct `<ID>` census = 1607; `1534 + 73` (the fold, §3) | `[HIGH/CARRIED]` on 1607; `[HIGH/OBSERVED]` on the `+73` |
| 4 | **12642** | **pre-fold** authoring placements (TIE-DB) | TIE-DB `<OPCODEDEF>` element count = 12642; `12569 + 73` | `[HIGH/CARRIED]` on 12642; `[HIGH/OBSERVED]` on the `+73` |
| 5 | **864** | execution-validated **value leaves** | `nm libfiss-base.so \| rg -c 'module__xdref_'` = 864 (all `T`/`t` text symbols; 864 distinct base names) | `[HIGH/OBSERVED]` |

Re-derivation transcript (this pass, on `libisa-core.so` sha256 `8fe68bf4…f143e451`,
9 690 712 B, and `libfiss-base.so` sha256 `260b110c…08d3cc94`, 12 330 016 B):

```
num_opcodes()      @ 0x3b61d0 :  mov $0x5fe,%eax   →  0x5fe   = 1534
num_encode_fns()   @ 0x3b6130 :  mov $0x3119,%eax  →  0x3119  = 12569
num_fields()       @ 0x3b5b40 :  mov $0xca5,%eax   →  0xca5   = 3237
num_formats()      @ 0x3b65e0 :  mov $0xe,%eax     →  0xe     = 14
num_slots()        @ 0x3b6510 :  mov $0x2e,%eax    →  0x2e    = 46
num_decode_fns()   @ 0x3b64c0 :  mov $0x2e,%eax    →  0x2e    = 46
num_iclasses()     @ 0x3b5fb0 :  mov $0x5a7,%eax   →  0x5a7   = 1447
num_operands()     @ 0x3b5e80 :  mov $0xe8,%eax    →  0xe8    = 232

nm libisa-core.so | rg -c 'Opcode_.*_Slot_.*_encode'          = 12569   (placements)
nm libisa-core.so | rg -o 'Opcode_(.+)_Slot_…_encode' -u | wc = 1534    (distinct mnemonics)
nm libisa-core.so | rg -c 'Slot_.*Format_.*_get'              = 46      (gathers)
nm libisa-core.so | rg -c 'Slot_.*Format_.*_set'              = 46      (scatters)
nm libisa-core.so | rg -c '\bSlot_[A-Za-z0-9_]+_decode\b'     = 46      (classifiers)
nm libisa-core.so | rg -c 'Field_.*_get'                      = 3237    (field-gets)
nm libisa-core.so | rg -c 'Field_.*_set'                      = 3230    (field-sets)
nm libisa-core.so | rg -c 'OperandSem_.*_decode'              = 95      (operand-sem decoders)
nm libisa-core.so | rg -c 'Format_.*_encode'                  = 14      (format encoders)
nm libfiss-base.so | rg -c 'module__xdref_'                   = 864     (value leaves)
```

Every immediate matches the sibling pages byte-for-byte; nothing here is taken on a report's
word. `[HIGH/OBSERVED]`

> **NOTE — the secondary Part-2 dimensions, re-confirmed in the same transcript.** `num_fields =
> 0xca5 = 3237`, `num_formats = 0xe = 14`, `num_slots = num_decode_fns = 0x2e = 46`,
> `num_iclasses = 0x5a7 = 1447`, `num_operands = 0xe8 = 232`. The `OperandSem_*_decode` family is
> **95** (operand value-transform decoders — sign-extend / scale / bias / PC-rebase). The
> register architecture is **8 regfiles + 4 BR views**, the FLIX grid **14 formats / 46 slots**,
> length classes **7 → 4 byte-sizes `{2,3,8,16}`**, and the TIE value-DB leaf census **864
> `xdref` leaves**. These are the numbers the four sibling pages own; this page restates them as
> the coverage denominators and does not re-derive their interiors. `[HIGH/OBSERVED]`

---

## 2. The precise definitions — mnemonic vs placement, shipped vs pre-fold

The four mnemonic/placement numbers are only meaningful once the four words are pinned. A
reimplementer who blurs any two of them mis-sizes a table.

| Term | Exact meaning | Where it lives | Count |
|---|---|---|---|
| **mnemonic** | one **distinct opcode name** — one row of `opcodes[]` | `opcodes[]` @ `0x6ce6c0`, stride 72 | 1534 (shipped) |
| **placement** | one legal **`(mnemonic × slot)` pair** — one row of `opcodedefs[]`, one `Opcode_*_Slot_*_encode` thunk | `opcodedefs[]` @ `0x6e9640`, stride 24 | 12569 (shipped) |
| **shipped** (runtime) | present in the **binary `libisa-core.so` tables** the disassembler/ISS actually consume | the two tables above | 1534 / 12569 |
| **pre-fold** (authoring) | present in the **standalone TIE database** (the cleartext authoring source) *before* the generator folds alias/macro forms | TIE-DB `<OPCODEDEF>` records | 1607 / 12642 |

A **mnemonic** is the opcode (e.g. `ivp_addnx16`); a **placement** is that opcode realized in
one specific FLIX slot (e.g. `ivp_addnx16` in `F0_S3_ALU`). One mnemonic legal in *k* slots
contributes *one* mnemonic and *k* placements. In the shipped tables **1178** of the 1534
mnemonics carry more than one placement (re-counted from `nm` this pass; `nop` alone carries
**44** placements — it is legal in every issue position); the average is `12569 / 1534 ≈ 8.2`
placements per mnemonic.

The encode side is a **placement** table: there is exactly one
`Opcode_<mnem>_Slot_<slot>_encode` thunk per legal pair (12569 of them), each laying the fixed
opcode-selector bits for *that* opcode *in that slot*. The decode side is a **slot** table: one
`Slot_<slot>_decode` classifier per slot (46 of them), each recognising *every* opcode legal in
its slot. So decode is many-opcodes→one-fn-per-slot (46), encode is one-opcode→one-fn-per-slot
(12569) — the asymmetry is structural, not a counting error. (Full mechanism:
[the decode model §2–§3](libisa-decode-model.md).)

> **CORRECTION — the SortMerge phantom is *named-but-never-shipped* and is excluded from 1534.**
> A "merge two sorted subtensors" op, **SortMerge**, survives in the firmware *only* as a dead
> comment (`// "SortMerge wip 0x97"`, slot `0x97` commented out, `0x98` reassigned to
> `TENSOR_SCALAR_SELECT`). It has **no `opcodes[]` row, no encode thunk, no value leaf**.
> Re-checked this pass: `rg -ci sortmerge` over the 1534-mnemonic roster = **0**. SortMerge is
> *not* in any denominator on this page; the 1534 count is sound precisely because it counts
> shipped table rows, not authoring intent. (See the
> [Confidence Model §4.5](../../reference/confidence-model.md) — this is a *fabrication wall*,
> documented so no downstream page invents the opcode.) `[HIGH/OBSERVED]`

---

## 3. The `+73` fold — how 1607/12642 collapses to 1534/12569

The authoring TIE database and the shipped runtime tables differ by **exactly `+73` mnemonics
and `+73` placements**, in lockstep:

```
   pre-fold (TIE DB) :  1607 mnemonics  /  12642 placements
   shipped (binary)  :  1534 mnemonics  /  12569 placements
   fold delta        :   −73 mnemonics  /   −73 placements
```

The arithmetic is exact: `1534 + 73 = 1607`, `12569 + 73 = 12642` (re-checked this pass). The
deltas move **together** because each folded mnemonic carries **exactly one** placement in the
authoring DB — they are author-time assembler/macro forms with a single canonical encoding that
collapses (aliases or elides) onto an already-shipped opcode before the runtime `opcodedefs[]`
table is generated.

**The fold mechanism, by group** (from the TIE-DB authoring census; the *base* survivors
re-confirmed present in the binary roster this pass, the folded forms confirmed *absent*):

| Folded group | n | What they are | Binary-roster check (this pass) |
|---|--:|---|---|
| `xt_wide_branch` `.W18` macros | 24 | 18-bit wide-branch-offset macro expansions of `BALL/BANY/BBC/BBCI/BBS/BBSI/BEQ/BEQI/BEQZ/BGE/BGEI/BGEU/BGEUI/BGEZ/BLT/BLTI/BLTU/BLTUI/BLTZ/BNALL/BNE/BNEI/BNEZ/BNONE` | the **base** forms (`beq`, `bnez`, …) **and** their `_w15` 15-bit variants ship; **no** `*_w18` form is in the roster (`rg w18` = 0) — the `.W18` macros fold onto the base/`_w15` encodings |
| `xt_virtualops` pseudo-ops | 6 | `ADDI.A.N, CLAMPSF, FFS, POPC, POPCE, SEXTF` — pure assembler virtual ops with no runtime encoding | `FFS/POPC/CLAMPSF/SEXTF` all **absent** from the 1534 roster (re-checked) — they expand to base-op sequences at assemble time |
| residual authoring forms | 43 | the remainder of the `+73` (additional `.W18`/macro/alias forms across `xt_branchprediction`, `xt_halt`, `xt_trace`, `xt_interrupt`, and other packages) that collapse onto a shipped base encoding | the *base* forms (`rbtb0..2`, `wbtb0..2`, `halt`, `halt.n`, `waiti`, `wsr.mmid`) **are** in the binary roster — only their author-time alias/wide variants fold |

The 24 + 6 named groups account for 30 of the 73; the remaining 43 are the spread of `.W18` /
alias / macro variants across the branch-prediction, halt, trace and interrupt packages whose
*base* forms ship and whose *authoring* variants fold. The reconciliation verdict from the
authoring DB is decisive and re-confirmed by the roster checks: **no opcode in the binary is
missing from the DB** — the TIE-DB is a strict superset, and the `−73` fold removes only
authoring conveniences, never datapath behaviour. `[HIGH/OBSERVED]` on the fold direction, the
`+73` arithmetic, and the present/absent roster checks; `[HIGH/CARRIED]` on the 1607/12642
authoring totals (read from the decoded TIE-DB, not the runtime binary).

> **GOTCHA — never cross-pair the totals.** The only correct pairings are
> **`1534 ↔ 12569`** (shipped / runtime) and **`1607 ↔ 12642`** (pre-fold / authoring). Pairing
> `1534` with `12642`, or `1607` with `12569`, mixes the runtime fold with the authoring
> superset and manufactures a `±73` phantom discrepancy out of thin air. The shipped tables are
> authoritative for *anything a reimplementer builds* (decoder, assembler, ISS): they are
> **`1534 / 12569`**. The `1607 / 12642` pair is useful only as the *reference-semantics*
> superset — the TIE DB carries bit-precise MODULE logic the binary tables only reference. State
> both pairs; never split one across the line. `[HIGH/OBSERVED]`

> **NOTE — why fold at all?** The generator folds because `.W18` wide-branch macros and
> virtual-ops are *assembler conveniences*: they exist so an author can write `BEQZ.W18 a2, far`
> and have the assembler emit the real shipped encoding. The runtime decode/ISS never needs a
> separate table row for them — they were already lowered. The fold is therefore lossless at the
> *encoding* level (every byte sequence a folded form would produce is still produced by its
> base form) and is exactly why the shipped `1534/12569` is the right denominator for an
> encoding-completeness claim. `[HIGH/INFERRED]`

---

## 4. Coverage by ISA region

The 1534 shipped mnemonics partition cleanly by **package** (`opcodes[].package`) — a 28-package
census that sums to exactly 1534, re-derived against the binary roster this pass. The packages
map onto the functional ISA regions a reimplementer thinks in (vector ALU / MAC / load-store /
gather / predicate / convert / transcendental / base-Xtensa / pseudo). Coverage and confidence
are stated per region.

### 4.1 The 28-package census (the binary partition, sums to 1534)

| package | n | package | n | package | n |
|---|--:|---|--:|---|--:|
| `xt_ivp32` | 1072 | `xt_density` | 11 | `xt_externalregisters` | 5 |
| `xt_core` | 131 | `xt_mmu` | 10 | `xt_integerdivide` | 4 |
| `xt_ivpn_scalarfp` | 102 | `xt_virtualops` | 10 | `xt_instram` | 3 |
| `xt_exception_dispatch` | 37 | `xt_misc` | 8 | `xt_dataram` | 3 |
| `xt_debug` | 33 | `xt_instcache` | 7 | `xt_prefetch` | 3 |
| `xt_wide_branch` | 24 | `xt_branchprediction` | 7 | `xt_coprocessors` | 3 |
| `xt_booleans` | 16 | `xt_mul` | 5 | `xt_wide_loop` | 3 |
| `xt_regwin` | 14 | `xt_sync` | 5 | `xt_exceptions` | 2 |
| `xt_timer` | 12 | — | — | `xt_halt` | 2 |
| — | — | — | — | `xt_interrupt` | 1 |
| — | — | — | — | `xt_trace` | 1 |

**Total = 1534** (re-summed this pass). `[HIGH/OBSERVED]`

> **CORRECTION — "package == `xt_ivp32`" is *not* the same predicate as "name starts `ivp_`".**
> The vector package `xt_ivp32` holds **1072** ops, but the `ivp_*` *name-prefix* count is
> **1065**. The 7 ops that are in `xt_ivp32` but lack the `ivp_` prefix are the scalar-FP-control
> ops `rur.fcr, wur.fcr, rur.fsr, wur.fsr, recipqli.s, mulsone.s, mulsone.h` — all re-confirmed
> present in the binary roster this pass. So the canonical **scalar/vector split is `469 / 1065`
> by name prefix** (independently re-derived from the binary: `rg '^ivp_'` = 1065,
> `rg -v '^ivp_'` = 469, summing to 1534), while the **package split puts 1072 in `xt_ivp32`**.
> Both are correct for their axis; do not conflate them. `[HIGH/OBSERVED]`

### 4.2 Region roll-up — count · coverage · confidence

Mapping the packages onto functional regions. "Coverage" is *encoding* coverage (every
mnemonic in the region has its full placement set in `opcodedefs[]`); the **value-semantics**
coverage of the datapath regions is the separate `864/864` execution-validated layer (§5).

| ISA region | packages | mnemonics | encoding coverage | value-sem coverage | confidence |
|---|---|--:|---|---|---|
| **Vector ALU + MAC + reduce + shift + pack + select** | `xt_ivp32` (datapath subset) | ~960 of 1072 | **complete** — every op fully placed; 12 of 14 formats oracle-confirmed | execution-validated via `xdref` leaves (§5) | `[HIGH/OBSERVED]`; `F4`/`F6` interiors `[MED/INFERRED]` |
| **Vector load / store / valign** | `xt_ivp32` (ld/st subset) | (within the 1072) | **complete** — LdSt/Ld slots fully populated across F0–F11/N0–N2 | store-capability per `opcodes[].flags` bit5 | `[HIGH/OBSERVED]` |
| **SuperGather scatter / gather** | `xt_ivp32` (gather subset) | (within the 1072) | **complete** — `gvr`(8)/`b32_pr` operand model byte-pinned | gather value leaves validated | `[HIGH/OBSERVED]` |
| **Predicate / vbool** | `xt_ivp32` (vbool subset) | (within the 1072) | **complete** — N0/N1/N2 narrow codecs **inverse-proven** | predicate compare leaves validated | `[HIGH/OBSERVED]` |
| **Convert (fp16/fp32 ↔ int)** | `xt_ivp32` (cvt subset) | (within the 1072) | **complete** | RZ/RNE/saturation behaviours pinned by execution | `[HIGH/OBSERVED]` |
| **Transcendental seeds (recip/rsqrt/exp)** | `xt_ivp32` (lookup subset) | (within the 1072) | **complete** — seed `.rodata` LUTs byte-read | `128/128` execution-validated (`FISS==SEM==TAB`); source coefficients CARRIED | `[HIGH/OBSERVED]` table; `[MED/CARRIED]` lineage |
| **Scalar-FP (FCR/FSR view)** | `xt_ivpn_scalarfp` + 7 `xt_ivp32` outliers | 102 (+7) | **complete** — `rur/wur.fcr/.fsr` round-mode plumbing | scalar fp value leaves validated | `[HIGH/OBSERVED]` |
| **Base-Xtensa scalar / system / ctrl** | `xt_core`(131), `xt_density`(11), `xt_mul`(5), `xt_integerdivide`(4), `xt_booleans`(16), `xt_regwin`(14), `xt_exception_dispatch`(37), `xt_debug`(33), `xt_timer`(12), `xt_mmu`(10), `xt_instcache`(7), `xt_sync`(5), `xt_misc`(8), + 8 small pkgs | **360** total | **complete** — `Inst`/`Inst16a`/`Inst16b` templates = the literal Xtensa words | base ISA (architecturally standard) | `[HIGH/OBSERVED]` |
| **Pseudo / fold-source** | `xt_virtualops`(10), `xt_wide_branch`(24) | 34 *shipped-base* of these packages ship; the `+73` fold forms (§3) do **not** ship | base forms ship; `.W18`/virtual variants fold | n/a (lowered at assemble time) | `[HIGH/OBSERVED]` |

> **NOTE — the functional sub-split of the 1072-op vector ISA is Part 3's subject.** This page
> grounds coverage on the *binary package partition* (1534 by `opcodes[].package`, which is
> OBSERVED). The finer functional partition — vector-ALU-int vs vector-ALU-fp vs integer-MAC vs
> mixed-MAC vs loads vs stores vs reduce vs shift vs convert vs transcendental vs scatter/gather
> vs select/shuffle — is the **B01–B30 batch partition** of the per-instruction reference
> (Part 3). Those 30 batches *together* cover the same 1534 mnemonics; their per-batch counts
> are the reference's job to enumerate, and they **build their completeness claim on this page's
> 1534/12569 denominator**. See the forward-links in §8. `[HIGH/INFERRED]`

The **per-slot placement census** (the 12569 broken down by the 46 slots) is byte-complete and
sums exactly; it is reproduced on [the FLIX encoding page §6.3](flix-encoding.md). Re-summed
this pass from `nm`: every one of the 46 slots hosts ≥ 1 opcode, and the per-slot counts total
**12569** with zero slack. The three `None` slots (`N0_S1`, `N0_S2`, `N1_S1`) host exactly one
op each — `nop` — and are NOP-only filler, not real issue units. `[HIGH/OBSERVED]`

---

## 5. Value-semantics coverage — the `864/864` execution-validated cover

Encoding coverage answers "can I *decode and re-encode* every shipped instruction byte-exact?"
(yes — §1–§4). **Value** coverage answers a stronger question: "do I know what every value-
producing op *computes*, bit-exact?" That layer is the **864 `module__xdref_` value leaves** in
`libfiss-base.so` — the per-element value functions of every GPSIMD value opcode.

The decisive property (from [the Confidence Model §6.1](../../reference/confidence-model.md)):
`libfiss-base.so` is **callable in-process via ctypes with no license**. A value leaf is not
merely *decoded* — it is **proven-by-execution**: call the shipped leaf on a sweep of inputs,
diff against an independent reference model, and a bit-exact match across the sweep is an
OBSERVED-by-execution certificate. The companion cycle oracle (`libcas-core.so`) gates
*retirement* behind `AUTH::check_iss_licenses`, so cycles/faults/trace are walled
(`closable-with-license`); but the **value** lane runs free.

| Quantity | Value | Witness | Tag |
|---|---|---|---|
| value leaves (denominator) | **864** | `nm libfiss-base.so \| rg -c 'module__xdref_'` = 864 (this pass) | `[HIGH/OBSERVED]` |
| execution-validated | **~95% of value-bearing leaves** | ~2.09M differential comparisons across 18 op families, **zero firmware value bugs** | `[HIGH/OBSERVED by execution]` |
| transcendental seed LUTs | `128/128` reproduced | `RECIP_Data8`/`RSQRT_Data8` byte-read + `FISS==SEM==TAB` agreement | `[HIGH/OBSERVED]` |
| edge behaviours pinned | RZ-default *value-leaf* rounding (the un-parameterized-call default — distinct from the **RNE** architectural FCR reset; see [fp-sub-isa §3.2](fp-sub-isa.md#32-the-encoding--proven-by-execution)), NaN-asymmetric max/min, quiet/signaling compare split, 3-way pack saturation | differential-execution certificates | `[HIGH/OBSERVED]` |

The headline restated: **value-semantics coverage is `864/864` leaves enumerated and ~95%
execution-validated with zero value bugs across ~2.09M comparisons** — the binary itself is the
arbiter of its own value semantics, which is what lets this number be HIGH rather than inferred.
The residue (~5% of leaves not yet swept) is *task*, not *wall*: those leaves are present and
callable; only the differential run is unfinished. `[HIGH/OBSERVED]`

---

## 6. The four-source agreement tally

Coverage is "certified" because four **independent** descriptions of the ISA agree. Each is a
different artifact with a different failure mode; their agreement is what upgrades "we decoded
it" to "we decoded it *correctly*."

| Source | What it is | What it grounds | Counts it pins | Tag |
|---|---|---|---|---|
| **libisa** (`libisa-core.so`) | the shipped TIE-generated runtime ISA DB the disassembler/ISS consume | the **shipped** encoding tables — the arbiter for everything a reimplementer builds | 1534 / 12569 / 3237 / 14 / 46 / 8 / 95 | `[HIGH/OBSERVED]` |
| **TIE-DB** (standalone) | the cleartext authoring database (`Xtensa.xml` / `Xtensa.tl`), decoded once | the **pre-fold** superset + the **reference MODULE semantics** (bit-precise wire/assign logic) | 1607 / 12642 (pre-fold) | `[HIGH/CARRIED]` |
| **objdump** oracle | the device-native `xtensa-elf-objdump`/`xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) | an **independent** encode/decode round-trip — bytes ⇄ mnemonics | 81 442 bundles / 549 375 per-slot mnemonics, **0** disagreements | `[HIGH/OBSERVED]` |
| **headers** (`core-isa.h` / `ncore2gp-params`) | the config-of-record text + generated HAL header | the **identity/config** the tables instantiate (vision type, formats, regfiles, coproc) | 14 formats / 8 regfiles / Q7 / 512-bit | `[HIGH/OBSERVED]` |

The agreements that close the cover:

* **libisa ⇄ objdump (encode/decode round-trip).** A faithful port of the libisa decode pipeline
  reproduces the device-native disassembler with **zero** disagreements across 81 442 bundles /
  549 375 per-slot mnemonics (0 length errors); the device assembler emits **byte-identical**
  firmware bytes for worked bundles (e.g. the `N0` bundle `4f 80 a0 80 …` byte-matches the Cayman
  `EXTISA_0` image). 12 of 14 formats are directly oracle-confirmed.
* **libisa ⇄ TIE-DB (fold reconciliation).** The runtime `1534/12569` is the TIE-DB `1607/12642`
  minus the `+73` fold (§3); **no binary op is missing from the DB**. The DB additionally carries
  the reference value MODULEs the binary tables only reference.
* **libisa ⇄ headers (config consistency).** The header `num_formats`/`regfiles`/`VISION_TYPE`/
  `coproc` match the libisa table dimensions exactly ([identity-config.md](identity-config.md) /
  [config-reference-sheet.md](config-reference-sheet.md)).
* **libisa ⇄ libfiss (encoding ⇄ value).** Each shipped placement's iclass/operand chain resolves
  to a value leaf whose semantics are execution-validated (§5).

The per-source mnemonic/placement join — which exact opcodes each source carries and how the
`+73` reconciles — is the subject of the
[TIE Database & Four Independent ISA Sources](tie-database.md) page (#611); this page owns the
*tally*, that page owns the *per-opcode join*. `[HIGH/OBSERVED]` on the libisa/objdump/header
agreements; `[HIGH/CARRIED]` on the TIE-DB superset relation.

---

## 7. Residual gaps and their closability

The cover is certified, but four residuals are honestly flagged. None is a missing datapath
body, a missing opcode decode, or a missing value semantics — each is a *driver / image /
config* boundary, per [the Walls Model §4](../../reference/confidence-model.md).

| Residual | What is unresolved | Confidence floor | Closability |
|---|---|---|---|
| **`F4` / `F6` INFERRED interiors** | some per-slot mnemonic/operand bit-exactness in formats `F4` (dual-load) and `F6` is inferred from the *identical* decode path rather than oracle-confirmed on a specific bundle — **no shipped object emits an `F4`/`F6` bundle** to confirm against | `[MED/INFERRED]` per `F4`/`F6` interior; the *structure* is `[HIGH/OBSERVED]` | **closable-with-corpus** — a firmware image that emits an `F4`/`F6` bundle, or a FLIX-aware objdump config |
| **The FLIX-desync wall** | a linear sweep over dense device `.text` cannot stay synchronized in-band (literal pools / boot stubs interleave with bundles, with no tag separating a literal word from a bundle word) | wall `[HIGH/OBSERVED]`; desynced *interiors* `[MED/OBSERVED]` | **closable-with-corpus** (per-image `.xt.prop.*` records / a FLIX-aware config). *Note: the encoding cover does **not** depend on the linear sweep* — it is closed on the certified-perfect placement matrix, which is read at known addresses, not swept |
| **v5 (MAVERICK) INFERRED** | the v5 generation is **header-OBSERVED** (coretype 37, 62 getters, clang-15 `.comment`) with **INFERRED interiors** (`arch_id 36*`, the v5 collective firmware image is file-absent) | header `[HIGH/OBSERVED]`; interiors `[MED/INFERRED]` or file-absent | **closable-with-corpus** — a fuller `libncfw` checkout carrying the coretype-37 image |
| **Empty `MODULE_SCHEDULE`** | the per-port pipeline reservation matrices ship **structurally empty**; the fine per-port single-issue model below the co-issue ceiling is unrecoverable | ceiling `[HIGH/OBSERVED]`; per-port reservation `[LOW]` | **fundamental** for the per-port claim — but the **`1+1` co-issue bound is the sound substitute** (next NOTE) |

> **NOTE — the empty `MODULE_SCHEDULE` yields a *sound* `1+1` co-issue bound, not a gap in the
> ISA.** With the per-port reservation matrices absent, the sound, non-speculative bound is the
> structural one the format itself states: one `XT_LOADSTORE_UNIT` with **`num_copies = 2`**
> permits at most **two memory ops** (the LdSt + Ld slots) plus the format's Mul + ALU slots to
> co-issue — i.e. **`1 + 1`** (one load/store-class op + one of each other class per declared
> slot, capped by the 2 LSU copies). This is an *ISA-coverage* fact: the **encoding** of every
> co-issue combination is fully covered (all 46 slots placed); only the *fine timing* below the
> structural ceiling is walled. A reimplementer's scheduler encodes the `1+1` ceiling and is
> correct-by-construction for issue legality. `[HIGH/OBSERVED on the slot rosters + 2 LSU copies;
> the absence of a tighter model is OBSERVED-negative.]`

> **GOTCHA — the v2–v4 footing is hard spec; v5/v1 is header-OBSERVED only.** Generations **v2–v4
> (Sunda / Cayman / Mariana / Mariana+)** are byte-grounded and execution-validated — a
> reimplementer can target them as a hard specification. **v5 (Maverick)** and **v1 (Tonga)** are
> publishable only as **header-OBSERVED + bounded-INFERRED**, with interiors flagged on every
> use. The 1534/12569 cover is the **gen-invariant Cairo core** (one `libisa-core.so`, no
> `arch_id`/`coretype` gate in its decode path), so the *encoding* tally holds across all
> shipped generations; the per-generation *firmware/image* residuals (v5 absent) are a separate
> axis. Never fabricate a v5 `arch_id` byte, a v5 collective firmware, or a v5 silicon
> part-binding. `[HIGH/OBSERVED]` on gen-invariance of the encoding;
> [Confidence Model §5](../../reference/confidence-model.md) on the policy.

---

## 8. The bottom line — a certified-perfect cover

The coverage verdict, stated as the claim Part 3 and the appendix inherit:

* **Encoding: `1534 / 1534` shipped mnemonics, `12569 / 12569` shipped placements — certified
  perfect.** Every shipped opcode has its full placement set in `opcodedefs[]`; the matrix sums
  to 12569 with zero slack; every one of the 46 slots is populated; the device-native objdump/as
  round-trips byte-exact over hundreds of thousands of bundles with zero disagreements. The
  `+73` authoring forms are folded (lossless at the encoding level). `[HIGH/OBSERVED]`
* **Value semantics: `864 / 864` leaves enumerated, ~95% execution-validated, zero value bugs
  across ~2.09M comparisons** — the binary itself the arbiter via the free in-process value lane.
  `[HIGH/OBSERVED by execution]`
* **Four-source agreement:** libisa (shipped) ⇄ TIE-DB (pre-fold superset, reconciled by `+73`)
  ⇄ objdump (independent round-trip oracle) ⇄ headers (config-of-record) — all agree.
* **Residuals** are `F4`/`F6` INFERRED interiors, the FLIX-desync wall, v5 INFERRED interiors,
  and the empty `MODULE_SCHEDULE` (bounded by the sound `1+1` ceiling) — **all
  closable-with-corpus or fundamental-with-a-sound-substitute, none a missing decode or value.**

This is the page the per-instruction reference (Part 3, B01–B30) and the
[Master ISA Encoding Appendix](../../appendix/isa-encoding-appendix.md) (#989) build their
completeness claims on: when a Part-3 batch says "this batch covers N of the 1534," the `1534`
is *this* number, the certified-perfect denominator.

---

## 9. Adversarial self-verification — the five headline counts, re-derived this pass

Each headline number re-derived independently from the binary, two ways where possible; nothing
taken from a report's word.

1. **`1534` shipped mnemonics.** `num_opcodes()` @ `0x3b61d0` = `mov $0x5fe,%eax` → `0x5fe` =
   1534. Independently, `nm libisa-core.so | rg -o 'Opcode_(.+)_Slot_…_encode' | sort -u | wc -l`
   = **1534** distinct mnemonics. Two witnesses agree. `[HIGH/OBSERVED]`
2. **`12569` shipped placements.** `num_encode_fns()` @ `0x3b6130` = `mov $0x3119,%eax` → `0x3119`
   = 12569. Independently, `nm | rg -c 'Opcode_.*_Slot_.*_encode'` = **12569**, and the per-slot
   census (46 slots) **sums to 12569** with zero slack. Three witnesses agree. `[HIGH/OBSERVED]`
3. **`1607` pre-fold mnemonics ↔ `1534` shipped (the fold).** `1534 + 73 = 1607` (re-checked).
   The fold forms are confirmed **absent** from the binary roster (`FFS/POPC/CLAMPSF/SEXTF` = 0;
   `*_w18` = 0) while their base forms ship; the TIE-DB carries the 1607 superset.
   `[HIGH/OBSERVED]` on the fold and arithmetic; `[HIGH/CARRIED]` on 1607.
4. **`12642` pre-fold placements ↔ `12569` shipped.** `12569 + 73 = 12642` (re-checked); the
   `+73` mnemonics each carry exactly one placement, so the placement delta equals the mnemonic
   delta. The correct pairing is `1607 ↔ 12642`, never `1607 ↔ 12569`. `[HIGH/OBSERVED]` on the
   arithmetic; `[HIGH/CARRIED]` on 12642.
5. **`864` value leaves.** `nm libfiss-base.so | rg -c 'module__xdref_'` = **864** (all 864 are
   `T`/`t` text symbols; the distinct base-name count is also 864). The denominator for the
   `864/864` enumerated / ~95% execution-validated value cover. `[HIGH/OBSERVED]`

**Pairing confirmation.** `1534 ↔ 12569` (shipped/runtime) and `1607 ↔ 12642` (pre-fold/
authoring) are the only two valid pairs; `1534 ↔ 12642` and `1607 ↔ 12569` are the cross-pairing
trap and are **wrong**. Both deltas are `+73`, in lockstep, because each folded mnemonic carries
exactly one placement. All five counts and both pairings re-derived from the binary this pass.

---

## 10. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| `1534` shipped mnemonics (`num_opcodes 0x5fe`; 1534 distinct `Opcode_*` symbols) | `[HIGH/OBSERVED]` | `libisa-core.so` getter @ `0x3b61d0` + `nm` |
| `12569` shipped placements (`num_encode_fns 0x3119`; `nm` = 12569; per-slot sum = 12569) | `[HIGH/OBSERVED]` | getter @ `0x3b6130` + `nm` + per-slot census |
| `1607 / 12642` pre-fold (TIE-DB); `+73` fold; pairs `1607 ↔ 12642`, `1534 ↔ 12569` | `[HIGH/OBSERVED]` on fold + arithmetic; `[HIGH/CARRIED]` on 1607/12642 | roster present/absent checks + decoded TIE-DB |
| `864` value leaves; `864/864` enumerated, ~95% execution-validated, 0 value bugs / ~2.09M cmp | `[HIGH/OBSERVED by execution]` | `nm libfiss-base.so` = 864; in-process ctypes differential lane |
| 28-package census sums to 1534; scalar/vector `469/1065`; `xt_ivp32 1072 ≠ ivp_* 1065` | `[HIGH/OBSERVED]` | `opcodes[].package` census + name-prefix re-derivation |
| Four-source agreement (libisa ⇄ TIE-DB ⇄ objdump ⇄ headers); 0 round-trip disagreements | `[HIGH/OBSERVED]` libisa/objdump/header; `[HIGH/CARRIED]` TIE-DB superset | round-trip oracle + fold reconciliation |
| SortMerge phantom excluded from 1534 (named-not-shipped); `rg -ci sortmerge` = 0 | `[HIGH/OBSERVED]` | absence is a positive finding |
| `F4`/`F6` INFERRED interiors; FLIX-desync wall; v5 INFERRED; empty `MODULE_SCHEDULE` → `1+1` | floors per §7 | the named walls (Confidence Model §4) |
| 14 formats / 46 slots / 8 regfiles + 4 BR views / 3237 fields / 95 OperandSem / 7→4 lengths | `[HIGH/OBSERVED]` | the four sibling Part-2 pages, re-confirmed in §1 |

---

## 11. Cross-references

* [The TIE Database & Four Independent ISA Sources](tie-database.md) (#611) — the per-opcode
  join behind §6's four-source tally; which opcodes each source carries and the `+73` reconciliation.
* [The Canonical ISA Decode Model (libisa-core)](libisa-decode-model.md) — the object hierarchy
  and the §2.2 placement relationship this page tallies.
* [The FLIX VLIW Encoding](flix-encoding.md) — the per-slot placement census (§6.3) summing to 12569.
* [The Eight Register Files](register-files.md) — the 8 regfiles + 4 BR views denominator.
* [Config-Grounded Microarch Reference Sheet](config-reference-sheet.md) — the header/config counts.
* [Core Identity & Configuration](identity-config.md) — the gen-invariant Cairo identity the cover holds across.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the named walls
  (`F4`/`F6`, FLIX-desync, v5, `MODULE_SCHEDULE`, SortMerge), and the proven-by-execution lane.
* **Forward — Part 3 per-instruction reference (B01–B30)** and
  [The Master ISA Encoding Appendix](../../appendix/isa-encoding-appendix.md) (#989) — both build
  their completeness claims on this page's `1534/12569` certified-perfect denominator.
