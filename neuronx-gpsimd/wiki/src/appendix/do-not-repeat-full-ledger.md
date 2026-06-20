# The Full Do-Not-Repeat / Correction Ledger

This is the **exhaustive** companion to the curated
[Do-Not-Repeat / Correction Ledger](../reference/correction-ledger.md) (the Part-0 front
copy carries the sixteen highest-traffic rows; this appendix is canonical and carries
*all* of them plus the long tail). It is the consolidated register of every claim that, at
some point in the survey, was stated one way in an early analysis pass — or would be
written one way by a confident-but-naive first reading of the shipped binaries — and turned
out, under a direct re-read of the bytes, to be **wrong**. Each row pairs the **superseded
claim** with the **correct statement**, names the **byte/structural evidence** that settles
it, cites the **source page** that owns the correction, lists the **affected downstream
pages** a re-introduction would poison, and carries a **confidence/wall tag**.

Read it before you author anything that touches generations, opcodes, the activation
datapath, the dual-fetch front-end, the FLIX encoding, the collective config tape, or the
v5/MAVERICK identity surface — these are the traps a plausible first reading falls into, and
every one of them has already cost an analysis pass.

> **CORRECTION is the framing of this entire page.** Every row below overturns an earlier or
> naive reading; the callout is implicit per-row rather than repeated. Where a row records a
> *live* divergence between two committed pages (e.g. the `ct37` provenance, §1), it is
> tagged **LIVE DIVERGENCE** and names the page to fix in the Part-16 reconcile.

All prose is derived from **static analysis of the shipped binaries** — ELF objects, the
clean C arch-ISA headers shipped in the customop-lib (`nrtucode.h`,
`aws_neuron_isa_tpb_*.h`), the per-generation firmware images carved from
`libnrtucode_internal.so`, the `ncore2gp` config DLLs (`libisa-core.so` / `libfiss-base.so`),
the host `libnrt.so` DWARF, and `libncfw.so`. Recovered symbols, strings, and headers **are**
binary-derived facts and are cited as such; no vendor source tree is implied or referenced.

The confidence vocabulary (`HIGH/MED/LOW` × `OBSERVED/INFERRED/CARRIED`) and the **wall**
taxonomy are defined once, normatively, in
[The Confidence & Walls Model](../reference/confidence-model.md). The two standing v5 walls
this ledger keeps live — `arch_id 36` (**INFERRED**, no firmware byte) and `ct37`
(**OBSERVED**, three independent reads) — recur in several rows and are explained there in
full and in §1/§2 below.

---

## How to read a row

Each correction is one row of the master table. Columns:

- **#** — stable row id (used by sibling pages to cite a specific correction).
- **Superseded claim** — the wrong statement, phrased the way it was (or naively would be)
  written, so you recognize it when you meet it again.
- **Correct statement** — the binary-grounded truth.
- **Byte / structural evidence** — the symbol, offset, section, enum line, mask, or
  section-header read that settles it. This is what you re-run if you doubt the row.
- **Source** — the committed page that owns the correction (its CORRECTION callout is the
  primary citation).
- **Affected downstream pages** — where a re-introduction does damage.
- **Tag** — `[CONF/PROV]` plus any `WALL` / `LIVE DIVERGENCE` marker.

Escape a literal pipe inside a cell as `\|`.

The four most load-prone rows — the two v5 identity walls (§1, §2), the SortMerge phantom
(§3 #14), and the FLIX length two-level (§4 #1) — are broken out into their own sections
*before* the master table because each carries a structural argument longer than a table
cell, and each is the precise trap a re-read catches on the third look.

---

## 1. `ct37` is OBSERVED — the do-not-repeat distinction (a live divergence to settle here)

This is the single most important row on the page, and it carries a **live divergence**
between two committed appendix pages that this ledger resolves authoritatively.

**Correct statement.** **coretype 37 (`ct37`) is OBSERVED** — confirmed **three independent
ways** in the binary:

1. **The resolver bitmasks.** In `libnrtucode_internal.so` (INT), the two ext-ISA-count
   resolvers gate on `cmp $0x25,%edi` (`0x25 = 37`) and load a `movabs` whose **bit 37 is
   set**: `nrtucode_get_num_ext_isa_libs` (`.text 0x9b2c90`) uses `movabs $0x2020202000`
   (`@0x9b18b2`), and the sibling `opset_get_library_index` uses `movabs $0x2020202040`
   (`@0x9b1a1f`). Bit-decoded this pass: `0x2020202000 → {13,21,29,37}`,
   `0x2020202040 → {6,13,21,29,37}` — bit 37 is **set in both**. `[HIGH/OBSERVED]`
2. **`nrtucode.h:56`.** `NRTUCODE_CORE_MAVERICK_Q7_POOL` is the enum member at ordinal
   **37** (`nrtucode.h:56`, read this pass). `[HIGH/OBSERVED]`
3. **The `maverick_libs` resolver target.** `get_ext_isa` case `idx 31` (`coretype 37`) →
   `lea … 0x9b9050 <maverick_libs>` — the fifth and last `*_libs` jump-table target, present
   at `nm`-confirmed `0x9b9050`. `[HIGH/OBSERVED]`

**The do-not-repeat error** — the trap that recurs — is **conflating two distinct claims**:

- **"the v5/MAVERICK NCFW firmware *image* is file-absent"** — this is **TRUE**. The host
  `libncfw get_image` selector ladder closes at `arch_id 0x1c`; there is **no `cmp $0x24`
  (36) arm** and no v5 NCFW blob symbol. The orchestration *image* genuinely does not ship.
- **"the coretype *value* 37 is not observed"** — this is **FALSE**. The three reads above
  are device-side / header-side firmware bytes, entirely independent of the absent NCFW
  *image*. `ct37` is realised in the clang-15 INT twin's dispatch tables and the shipped
  arch-ISA headers.

State both halves explicitly: **the v5 NCFW image is absent (OBSERVED-negative); the
coretype value 37 is present (OBSERVED-positive).** They are different artifacts.

> **LIVE DIVERGENCE — fix #986.** The sibling appendix page
> [Device-Firmware Global Structs](struct-device-firmware-globals.md) (§1.5i, the NCFW carry
> wall) currently carries a CORRECTION stating *"coretype 37 (`ct37`, `arch_id 0x24`) is
> **INFERRED**, not OBSERVED"*. **That verdict is wrong** — it folds the (true) NCFW-image
> absence into the (false) coretype-value absence, the exact conflation this row forbids. The
> correct verdict, recorded here authoritatively and corroborated by
> [the MAVERICK Profile §1](../generations/maverick-profile.md), the
> [Codename Cross-Walk §3](../reference/codename-crosswalk.md), and the
> [Codename Cross-Walk card §3](codename-crosswalk-table.md), is **`ct37` OBSERVED**.
> **Part-16 reconcile: rewrite #986's §1.5i CORRECTION to OBSERVED, keeping its (correct)
> point that the *NCFW image* is file-absent.** `[ct37: HIGH/OBSERVED]` `[WALL — anchor]`

**Source.** [maverick-profile §1/§10](../generations/maverick-profile.md),
[codename-crosswalk-table §3](codename-crosswalk-table.md),
[codename-crosswalk §3](../reference/codename-crosswalk.md),
[confidence-model §4.2](../reference/confidence-model.md).
**Affected:** every v5/MAVERICK page; the confidence model; #986 (to fix).

---

## 2. `arch_id 36` is *doubly* INFERRED — never "the +1 of the coretype" alone

**Correct statement.** **`arch_id 36` (`0x24`) is INFERRED, and doubly so.** It is *not*
merely "INFERRED from `coretype = arch_id + 1`"; that understates it. Two independent reasons
keep it off OBSERVED:

1. **No `arch_id` byte for MAVERICK exists.** The only `arch_id`-keyed firmware structures
   are the NCFW selectors (`get_image`, `ctx_log`); both compare *exactly* `{0x05,0x0c,0x14,
   0x1c}` with a `ja → default` for `arch_id > 0x1c`. There is **no `0x24` (36) arm** and
   zero MAVERICK strings in `libncfw`. `[HIGH/OBSERVED-negative]`
2. **The `NX_TOPSP = arch_id` structure that grounds the other four gens *fails* for
   MAVERICK.** For SUNDA…MARIANA_PLUS, `arch_id` is the `<GEN>_NX_TOPSP` enum ordinal
   (5/12/20/28). For MAVERICK the index-36 enum slot is **`NRTUCODE_CORE_MAVERICK_NX__REMOVED__`**
   (`nrtucode.h:55`, a removed/reserved placeholder), and the *real* `MAVERICK_NX_TOPSP` is
   at index **54** (`nrtucode.h:54`) — not 36. The MAVERICK block also starts at `NX_DVE`
   (`nrtucode.h:50`, **no `NX_ACT`** — the ACT→DVE fold, §3 #6/#7), which is why its
   `NX_TOPSP` lands at 54 instead of a +8-stride 36. So `arch_id 36 = coretype(37) − 1` lands
   on a `__REMOVED__` name and *contradicts* the very structure that anchors the other rows.
   `[MED/INFERRED]`

Mark it **`36*`** on every appearance; never present it as binary-observed.

**Evidence.** `nrtucode.h:50,54,55,56` (read this pass); the NCFW `get_image` ladder
`{0x05,0x0c,0x14,0x1c}` with `ja` default (no `0x24`). **Source.**
[codename-crosswalk-table §3](codename-crosswalk-table.md),
[codename-generation-map §4](../generations/codename-generation-map.md),
[confidence-model §4.1](../reference/confidence-model.md). **Affected:** every v5 page; the
two cross-walk pages; the confidence model. `[MED/INFERRED]` `[WALL — closable-with-corpus]`

---

## 3. The SortMerge phantom — `0x97` is not, and was never, a real opcode

**Correct statement.** **There is no SortMerge hardware opcode.** A task plan once targeted
"SortMerge" as a byte to document; it survives **only** as a dead `wip` comment.

**Evidence (byte-exact, re-read this pass).** The maverick `common.h:239` reads
`NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_SELECT = 0x98, // SortMerge wip 0x97 // Y` — the `0x97`
appears *only* inside that parenthetical comment, never as an enumerator. The byte `0x97` is
**never assigned to any opcode** in any of the four shipped enums; the single `= 0x97` in the
header is `NEURON_ISA_TPB_UPDATE_MODE_SEM_SUB_REG_COMPLETE = 0x97` (`common.h:382`) — a value
in a *different* (WAIT/UPDATE-MODE) enum, not the opcode space. The real `0x98` is
`TENSOR_SCALAR_SELECT`. There is **no** SortMerge struct, **no** SortMerge opcode value, and
**no** SortMerge DEBUG string in any carved image. The cross-block / cross-partition merge
SortMerge would do is host-side or future work; in v0.21.2.0 only the single-block Sort
(`0x96`) ships.

Do **not** document a SortMerge hardware opcode, and do not "discover" `0x97`. **Source.**
[sort §1/§6](../firmware/kernels/sort.md),
[opcode-kernel-engine-matrix §5.1](opcode-kernel-engine-matrix.md),
[confidence-model §4.5](../reference/confidence-model.md). **Affected:** the ISA opcode-roster
pages, the search/sort-op pages, the opcode matrix. `[HIGH/OBSERVED — negative]`

---

## 4. FLIX length: 7 length-class outcomes → 4 distinct byte-sizes (two different levels)

**Correct statement.** The "7" and the "4" live on **different levels** and neither
supersedes the other; the trap is flattening them onto one "lengths" axis. The runtime
`length_decoder` (`@0x3b5a50`) + 256-entry `length_table` (`@0x3d4100`) yield **7
length-class outcomes** (the `op0==0xF` 8-vs-16 split keyed on `byte3.low4`, the `{2,3,16}`
direct lengths, and the illegal `-1`); those resolve to exactly **4 distinct positive
instruction byte-sizes `{2,3,8,16}`** — the *set* of the static `XCHAL_OP0_FORMAT_LENGTHS`
vector. State it as **"7 length-class/table outcomes → 4 distinct byte-sizes `{2,3,8,16}`"**,
never either number alone as "the number of lengths".

**Evidence.** The static byte-size set is read from the shipped Cadence config header
`tie.h`: `XCHAL_OP0_FORMAT_LENGTHS = 3,3,3,3,3,3,3,3,2,2,2,2,2,2,16,8` (sixteen entries; the
*set* is `{2,3,8,16}` = four). The 7-outcome figure is the runtime `length_table` value
census `{-1:2, 2:96, 3:128, 8:8, 16:22}` with the `op0==0xF` byte-3 split. `num_formats=0xe`
(14) and `num_slots=0x2e` (46) are independently correct, read from `libisa-core.so`. A
length-resync sweep that hard-codes seven distinct byte-lengths has a dead arm and masks a
real desync; one that deletes the `op0==0xF` byte-3 branch loses a real length-class.
**Source.** [correction-ledger §1](../reference/correction-ledger.md). **Affected:** the FLIX
encoding page, the FLIX-decoding methodology, the index/glossary. `[HIGH/OBSERVED]`

---

## 5. The master correction table

Every other do-not-repeat row, in one table. Rows whose argument is given above (#1–#4 / the
ct37/arch_id walls, the SortMerge phantom, the FLIX two-level) are summarized here for
completeness and cross-link back. The table is grouped by subsystem.

### 5a. Generation identity / codename bindings

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| G1 | `0x0c → v3 mariana.c`, `0x14 → v4 cayman.c` (CAYMAN/MARIANA codename text crossed on the v3/v4 rows) | `0x05 = SUNDA/v2`, **`0x0c = CAYMAN/v3`**, **`0x14 = MARIANA/v4`**, `0x1c = MARIANA_PLUS/v4+`; the arch_id→`v#` numbers were always right, only the *codename text* was inverted | `libncfw get_image` ladder `{0x05,0x0c,0x14,0x1c}` w/ `ja` default; `je` targets resolve in blob address order `v2<v3<v4<v4+`; `.c` strings `sunda.c<cayman.c<mariana.c<mariana_plus.c`; `ctx_log` `arch 12 → cayman_ncfw_ctx_log` | [codename-crosswalk §2](../reference/codename-crosswalk.md) | every per-gen firmware page; both cross-walk pages | `[HIGH/OBSERVED]` |
| G2 | `cayman = Trn1/Inf2` (the "N2.5" / v2-row conflation) | **CAYMAN = v3 = Trn2.** SUNDA is the v2 that serves *both* Trn1 and Inf2; the cayman codename was mis-attached to that v2 row | collectives topology builders: `topo_neuron_cayman.o` builds **Trn2** topologies, `topo_neuron_sunda.o` builds the **Trn1+Inf2** set; consistent w/ NCFW `0x0c → v3/CAYMAN` (G1) | [codename-crosswalk §1/§6](../reference/codename-crosswalk.md) | the collectives + platform pages; both cross-walk pages | `[HIGH/OBSERVED on the GPSIMD bind; the product family CARRIED]` |
| G3 | The platform compiler's `CoreV5` ArchLevel = a MAVERICK instantiation (the two "v5"s equated) | **Two distinct "v5" axes.** Platform `CoreV5 (trn3pre variant)` = **Trn3-pre / MARIANA_PLUS** (`coretype 29 / arch 0x1c`, NCFW-*present*); GPSIMD `ct37` / NC-v5 = **MAVERICK** (firmware-internal-only). Never equate | platform index crosswalk places `CoreV5` *under* Mariana/Trn3 as the explicit "trn3pre variant"; NCFW binds `0x1c = v4+/MARIANA_PLUS` w/ no `0x24` leg | [codename-crosswalk-table §6](codename-crosswalk-table.md), [codename-crosswalk §6](../reference/codename-crosswalk.md) | the codename/generation pages; any page citing a platform "CoreV5" label | `[HIGH/OBSERVED axes; "CoreV5 = Trn3-pre" reading MED/INFERRED-strong]` |
| G4 | `CoreV5`, `coretype 37`, and `NeuronCoreVersion::V5` are one "v5" token | **Three distinct tokens on three axes**, none derives the other: `CoreV5`/`core_v5` = compiler ArchLevel (= MARIANA_PLUS-region); `coretype 37` = device dispatch key (= MAVERICK); `NeuronCoreVersion::V5 = 5` = codegen-target ISA version | `NEURON_ISA_TPB_NEURON_CORE_VERSION_V5 = 5` (maverick `common.h:136`); **absent** from the mariana header (caps `V4 = 4`, `:135`); the `coretype` and ArchLevel axes proven separately (G3) | [codename-crosswalk-table §6 NOTE](codename-crosswalk-table.md) | the codename/generation pages; the v5 pages | `[HIGH/OBSERVED — V5 token read this pass]` |
| G5 | TONGA is a sixth GPSIMD coretype | **TONGA is the pre-unified NC-v1 outlier, outside the `coretype = arch_id + 1` family** — no coretype, no arch_id, no `NRTUCODE_CORE_TONGA`, no `tonga_libs`, no NCFW image, no EXTISA blob | SPIS Product-ID `"Tonga - 0x01"` (`spis_model.json:107`); `strings INT \| rg -i tonga` = 0; distinct 8-code `TONGA_ISA_TPB_DTYPE_*` family (subset of the 16-code `NEURON_ISA_TPB_DTYPE_*`); only in legacy `arch-isa/`, never `neuron_tonga_arch_isa` | [codename-crosswalk-table §4](codename-crosswalk-table.md), [codename-crosswalk §5](../reference/codename-crosswalk.md) | the codename pages; the cross-gen opcode diff | `[HIGH/OBSERVED; the V1/arch_id provenance INFERRED]` |
| G6 | The `coretype`/`arch_id` axis steps a uniform **+8** | The stride is **`+7, +8, +8, +8`** (`{6,13,21,29,37}` / `{5,12,20,28,36*}`); the +7 first step is because the SUNDA enum block has a different member count. The **only** uniform relation is `coretype = arch_id + 1` | `*_libs` symbol addresses keyed by a `coretype − 6` jump table; the `NX_TOPSP`/`Q7_POOL` enum ordinals; the resolver bitmasks (§1) | [codename-crosswalk-table §2.1](codename-crosswalk-table.md), [codename-crosswalk CORRECTION](../reference/codename-crosswalk.md) | any page deriving a value off a "+8 stride"; the v5 arch_id extrapolation | `[HIGH/OBSERVED]` |
| G7 | The MAVERICK string tally is "internal = 187" | **189 strings-occurrences / 125 symtab-symbols / 0 in SO.** The `187` was a `rg -ac` raw-byte count under-counting vs `strings`-tokenised occurrences | `strings INT \| rg -oi maverick \| wc -l` = 189; `nm INT \| rg -ci maverick` = 125; `strings SO \| rg -oi maverick \| wc -l` = 0 (re-counted this pass) | [codename-crosswalk-table §5 CORRECTION](codename-crosswalk-table.md) | the 4/5-split pages; any "MAVERICK string count" claim | `[HIGH/OBSERVED]` |

### 5b. The activation / DVE datapath + the profiler CAM

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| A1 | `PROF_CAM` holds "47 ACT opcodes" / *is* the activation PWL lookup (so `0x30` Exponential "routes through the PWL via PROF_CAM") | **`PROF_CAM`/`PROF_TABLE` is a generic, cross-engine HW instruction-decode *profiler* CAM** present on every NX engine, keyed on opcode w/ a 16-byte `{opcode(32), mask, enable, rsvd}` record. The activation PWL is a separate, ACT-only datapath | all four CAYMAN per-engine PROF_CAM blobs byte-identical (sha `8fd7e422`); the one 9-bit record `{opcode 0x1e3, mask 0x1ff}` (a func-CAM has no 9-bit-opcode concept); `.a` ships 24 `hwdecode_*_PROF_*` members, **0** activation members; device CSRs `ic0_opcode` "increment an ICn counter" | [prof-cam-table-formats §3/§5](../images/prof-cam-table-formats.md) | the activation-engine + profiler pages; every ISA page touching `0x30` | `[HIGH/OBSERVED]` |
| A2 | The activation table is a linear PWL storing `{intercept, slope, breakpoint}` | Each bucket entry is a **degree-≤3 polynomial** `{float d0,d1,d2,d3, x0}` evaluated in `t = (x − x0)` — **piecewise-*cubic* (PWP)**, not linear | `tpb_activation_entries.h` `aws_hal_stpb_act_bucket_entry_t` = four floats `d0@0/d1@4/d2@8/d3@12` + breakpoint `x0@16` (32 B); the `d2`/`d3` cubic terms are physically present | [prof-cam-table-formats §5a](../images/prof-cam-table-formats.md) | the activation-engine + transcendental-op pages | `[HIGH/OBSERVED format; per-function coeffs LOW/not-in-corpus]` |
| A3 | The MAVERICK ACT→DVE move is "merely a schedule/profile-arm fold" | The ACT→DVE fold is a **real hardware-region migration + a datapath rename**: MAVERICK ships **no `NX_ACT` image**; the activation PWL SRAM moves *into* the TPB_DVE block; ACT opcodes execute/profile on DVE | `nm \| rg -c 'MAVERICK.*NX_ACT'` = 0 (four independent zeros); `…DVE_0_0_ACT_CONTROL_TABLE`/`PWP_*` namespaced under the DVE node; `0x23`/`0x25` armed on the MAVERICK DVE PROF CAM (absent on MARIANA DVE) | [maverick-profile §3](../generations/maverick-profile.md), [prof-cam §5a](../images/prof-cam-table-formats.md) | the activation/DVE/v5 pages | `[roster HIGH/OBSERVED; causal fold INFERRED-HIGH]` |
| A4 | MAVERICK grew its ACT opcode count (159→165) → there must be a new ACT handler image | **MAVERICK ships no NX_ACT image; it is a PROF-table re-arm + a datapath rename.** The "+10 PROF-armed" ACT opcodes are *pre-existing* MARIANA enum entries, not v5 additions | MAVERICK DVE DRAM carries **0** DGE strings (vs 13 on MARIANA_PLUS); the 5 ACT handlers (`Activate`/`ActivateQuantize`/`ActivationTableLoad`/`ActivationReadAccumulator`/`Activate2`) = **0** each firmware-wide; read-accum survives as `DveReadAccumulator 0x9b` | [maverick-profile §3](../generations/maverick-profile.md) | the activation/DVE/v5 pages | `[HIGH/INFERRED — absence OBSERVED]` |
| A5 | The `tpb_activation_entries.h` quad is byte-identical across **all five** gens | cayman/mariana/mariana_plus/maverick share `8f6f5f49…`, but **SUNDA differs** (`dbdca26b…`) — a field-order swap of `opcode_mask`/`func_id_mask` (+3 reserved-bit reallocations) at the **same** 32/128/32/32 B sizes + same `(opcode, func_id)` key | re-hash of the shipped headers this pass; the PROF-vs-PWL distinction is unaffected | [prof-cam-table-formats §5d CORRECTION](../images/prof-cam-table-formats.md) | the activation header / transcendental pages | `[HIGH/OBSERVED]` |
| A6 | `0x72 COPY_PREDICATED` is a POOL op | **`0x72` is DVE-native** — the base of the predicated-op family `{0x72 copy / 0x99 cast / 0xe8 scalar / 0xea copy+reduce}` (struct `S3S3D3_TT`, src0-integer predicate) | `// Y` on all four gens (sunda 203 / cayman 206 / mariana 211 / maverick 214); `CopyPredicated` co-resident w/ `CastPredicated` in the NX_DVE debug blobs; absent from both carved POOL kernel-info tables | [correction-ledger §16](../reference/correction-ledger.md) | the opcode-roster + DVE predicated-op pages | `[HIGH/OBSERVED]` |

### 5c. The dual-fetch front-end (SEQ) + Sunda-mode

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| S1 | "Sunda-mode" is the SUNDA (v2) generation's firmware (a per-generation label) | **"Sunda-mode" is a runtime software-fetch *fallback*** present on CAYMAN-and-up images, selected by the host chicken bit; nothing to do with the SUNDA generation | string `"NX in Sunda mode: HW decode disabled"` present in CAYMAN POOL DRAM (`@0xef5`) + MARIANA_PLUS (`@0xeeb`), **absent** from SUNDA POOL DRAM; SUNDA SEQ has no dispatch table, names its fetch `fast_fetch`/`handle_surprises`; host selector `disable_hw_decode = CSR 0x4000[0]` | [dual-fetch §7](../firmware/seq/dual-fetch.md), [correction-ledger §8](../reference/correction-ledger.md) | the dual-fetch + firmware-mode pages; any v2 page | `[HIGH/OBSERVED]` |
| S2 | The lower-address dispatch table is "HW-Decode", the higher is "Sunda" (by IRAM position) | **HIGHER table `@0x80adc` = HW-Decode**; **LOWER `@0x80814` = Sunda-mode.** The "lower = HW-Decode" label was a positional authorship artifact (O1, the last live RED contradiction), now refuted | whole-IRAM `const16` uniqueness census: iter-log `@0x31d5` + `RTL_PC_check_delta` `@0x326e` each **exactly 1 site**, both inside the FSM that builds `0x80adc` (`const16 a3,0x0adc @0x36ce`); the Sunda FSM `@0x2d81` builds `0x80814` and has neither marker | [dual-fetch §6](../firmware/seq/dual-fetch.md), [correction-ledger §9](../reference/correction-ledger.md) | the dual-fetch page; the MARIANA carve pages | `[HIGH/OBSERVED]` |
| S3 | The two modes route to two different handler implementations | The two modes share **one** set of per-opcode `Handler::execute()` bodies; only the front-end FSM + the trampoline back-edge differ (`0x31a3` Sunda / `0x3a0f` HW-decode) | both `table[0]` trampolines (`0x3074` Sunda / `0x38dd` HW-decode) call the **same** impl `0x2124` (`'A'`), differing only in the back-edge | [dual-fetch §5c CORRECTION](../firmware/seq/dual-fetch.md) | the dual-fetch + dispatch-hub pages | `[HIGH/OBSERVED]` |
| S4 | SUNDA(v2) *is* "the Sunda-mode firmware" | **SUNDA(v2) has neither mode** — it is monolithic, predating the dual front-end; if "Sunda-mode" were the v2 gen, the v2 image would *be* the Sunda-mode firmware (it is not) | SUNDA carve (iram 59,600 B): 0 mode strings, 0 `addi-65`/`addi-48` dispatch sites, 0 `const16 *,0xadc`, 0 `RTL_PC_check`; uses `fast_fetch`(1)/`handle_surprises`(1) | [dual-fetch §7a CORRECTION](../firmware/seq/dual-fetch.md) | the dual-fetch + per-gen presence pages | `[HIGH/OBSERVED]` |
| S5 | The `0x80814` table holds the `Handler` vptrs (it is/contains a vtable) | The table holds **trampoline** addresses; the `Handler` vptr lives in a **boot-built** object the impl's FLIX bundle loads — a full scan finds **zero** static occurrences of any handler-fn address as a data word | the trampoline `call8 <impl>; j 0x31a3` bytes; the double-indirect `execute()` thunk (vtable slot 0 at `vptr+0`, **no `+0x10` header skip**); zero static-table count | [struct-device-firmware-globals §1.4 CORRECTION](struct-device-firmware-globals.md) | the dispatch-hub + device-globals pages | `[trampoline bytes HIGH/OBSERVED; object materialisation MED/INFERRED]` |

### 5d. PE engine, RNG, and PeManageSeed

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| P1 | PeManageSeed first appears at v4+ (MARIANA_PLUS) / it manages "a PE-array per-cell PRNG" with no struct | **PeManageSeed (`0x08`) first appears at v4 = MARIANA** (CAYMAN has none); it manages the **PSUM `fp32→bf16` stochastic-rounding RNG seeds** (2048 seeds/PSUM on v4, 32-bit each) via its own 64-byte `S2S1D2_PE_SEED` struct | the "v4+" boundary was a carve-coverage artifact (only CAYMAN + MARIANA_PLUS carved, never MARIANA-between); MARIANA PE image self-names `PeManageSeed` at DRAM offsets byte-identical to MARIANA_PLUS; CAYMAN PE has none; the struct compiles to 64 B; `0x08` present `--YY` | [opcode-kernel-engine-matrix §2.1](opcode-kernel-engine-matrix.md) (`0x08 PE_MANAGE_SEED`, `--YY`); [correction-ledger §12](../reference/correction-ledger.md) | the PE-engine + RNG/stochastic-rounding pages | `[HIGH/OBSERVED]` |
| P2 | `0x05 = Matmul (MultiplyMoving)`; no `0x02` row (the "MultiplyMoving" *name* mapped to the wrong byte) | **`0x02 = MATMUL`, `0x05 = WEIGHT_SHIFT`** (`// n, tonga stuff, deprecated`); `0x04 = WEIGHT_MASK` (also tonga-retired) | maverick `common.h`: `0x02 = MATMUL // Y`, `0x05 = WEIGHT_SHIFT // n …`, `0x04 = WEIGHT_MASK // n …` (read this pass) | [opcode-kernel-engine-matrix §2.1 CORRECTION](opcode-kernel-engine-matrix.md) | the PE-matmul page; **#775 §4h (to fix in Part-16)** | `[HIGH/OBSERVED]` |
| P3 | `LDWEIGHTS_MX`/`MATMUL_MX` (`0x09`/`0x0A`) keep dedicated v5 handlers | On v5 the MX pair **folds into `Matmul`/`Ldweights`** via the `MXTENSOR_V2` ADDR4 marker (0x01) + `MX_PERF_MODE`; `0x09`/`0x0A` are **retained + deprecated** opcodes | maverick PE PROF arms `0x09`/`0x0A`; the `MXTENSOR_V2` marker + `MX_PERF_MODE QUAD/OCT` fields in the PE header; `0x08`/`0x09`/`0x0A` first ship at MARIANA (CAYMAN absent) | [maverick-profile §4](../generations/maverick-profile.md) | the PE-matmul + v5 pages | `[HIGH/OBSERVED]` |

### 5e. Opcode classification + MX

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| O1 | `0xb4`/`0xb6` are part of the `0xb8..0xbd` DMA/transpose cluster | **`0xb4`/`0xb6` are NX control-spine, not DMA.** `0xb4 TEST_EVENT_SEM → CTRL_TEST_ES_STRUCT`; `0xb6 COMPACT_CONTROL_INST → CTRL_CCI_STRUCT` (a 15-micro-op/64-B control bundle). The genuine DMA/transpose cluster is `{0xb8,0xb9,0xba,0xbd,0xf1}` | `0xb4` mariana line 261 / maverick 264; `0xb6` maverick line 266 — both `// Y`, absent on lower gens; `ctrl_cci.h` read field-by-field | [correction-ledger §16](../reference/correction-ledger.md), [maverick-profile §5](../generations/maverick-profile.md) | the opcode-roster + DMA/transpose + control pages | `[HIGH/OBSERVED]` |
| O2 | The MAVERICK `0x26`/`0xf3`/`0xf4` bytes are "not corroborated" / "not byte-pinned" | All six v5 additions are **header-pinned `[HIGH/OBSERVED]`** (byte = HIGH); only the device *body* is MED: `0x26 ACTIVATE_MULTIPASS`, `0xb6 COMPACT_CONTROL_INST`, `0xb9 DMA_MEMCPY2`, `0xba DMA_IMMEDIATE`, `0xf3 TENSOR_TENSOR_INT_WIDE`, `0xf4 TENSOR_SCALAR_INT_WIDE` | maverick `common.h`: `0x26 // Y :173`, `0xb6 // Y :266`, `0xb9 // Y :268`, `0xba // Y :269`, `0xf3 // Y :320`, `0xf4 // Y :321` (read this pass) | [opcode-kernel-engine-matrix §2.4/§5.2 CORRECTION](opcode-kernel-engine-matrix.md) | the v5/MAVERICK opcode-roster pages; **#775 §9 + ledger §5 (narrow MED to body in Part-16)** | `[HIGH/OBSERVED byte; MED body]` |
| O3 | `0xe3 QUANTIZE_MX` is a POOL `kernel_info_table` row / the `QuantizeMx` handler "migrates to the Q7 POOL MX path" | **`0xe3` binds the DVE engine** (forward MX quantize, gate `nc==V5`); it is **absent from all four POOL KITs**; POOL's only MX surface is `0x7b TENSOR_DEQUANTIZE`; the `QuantizeMx` named handler is **DROPPED** (60→59), not migrated | the union of all four MAVERICK POOL KIT opcodes = `{0x41,0x45,0x46,0x47,0x51,0x52,0x7b,0x7c,0x7d,0x7e,0xbe,0xe4,0xf0,0xf2}` (no `0xe3`/`0x09`/`0x0A`); `0xe3` armed only on the MAVERICK DVE PROF CAM; `QuantizeMx` = 0 hits in the `0x871300+` region; `common.h:309 // Y` gate `nc==V5` | [maverick-profile §6 CORRECTION](../generations/maverick-profile.md) | the DVE/POOL/v5 + MX-dequant pages | `[HIGH/OBSERVED]` |
| O4 | `0xbe`/`0xf2` are one opcode (a "`0xf2 = GetSequenceBounds`" conflation) | **Distinct opcodes:** `0xBE = GET_SEQUENCE_BOUNDS` (POOL, KIT idx14), `0xF2 = NONZERO_WITH_COUNT` (POOL, KIT idx15). The `0xf2` trampoline routes through a *shared* sequence-bounds/dequant region, hence the loose conflation | maverick enum pins `GET_SEQUENCE_BOUNDS = 0xbe`, `NONZERO_WITH_COUNT = 0xf2`; KIT idx14/idx15 carves | [opcode-kernel-engine-matrix §3 CORRECTION](opcode-kernel-engine-matrix.md) | the opcode-roster + nonzero/get-sequence-bounds pages | `[HIGH/OBSERVED]` |
| O5 | The `search-cluster` ops `0x6c–0x6f` are MAVERICK additions | They are **pre-existing MARIANA DVE opcodes** (`MAX8`/`MATCH_VALUE_LOAD`/`FIND_INDEX8`/`MATCH_REPLACE8`, all `YYYY`); at MAVERICK they are merely *PROF-armed* onto DVE — a profile-table change, not opcode growth. Do not double-count as v5-new | all four `YYYY` in the enum; the DVE PROF re-arm `+10/−3` vs MARIANA includes them as re-armed pre-existing ops, distinct from the 159→165 enum growth | [opcode-kernel-engine-matrix §5.2 GOTCHA](opcode-kernel-engine-matrix.md), [maverick-profile §4 NOTE](../generations/maverick-profile.md) | the v5 opcode pages; the DVE PROF page | `[HIGH/OBSERVED]` |
| O6 | The SUNDA-only BF16 ops `0x8A–0x8F` are "six" ops / the same thing as dtype `BFLOAT16` | **Five** dedicated bf16 *opcodes* (`TENSOR_TENSOR_{ADD,MULT,SUB}_BF16`, `TENSOR_REDUCE_{ADD,MAX}_BF16`), all `Y---`, dropped CAYMAN+ — `0x8E` is **not** in the cluster (it is `BATCH_NORM_PARAM_LOAD2`, DVE, `YYYY`). The dtype code `BFLOAT16 = 0x6` is present in **all** gens; only the *opcodes* were retired | SUNDA header `0x8A/0x8B/0x8C/0x8D/0x8F // Y`, absent CAYMAN/MARIANA/MAVERICK; `0x8E = BATCH_NORM_PARAM_LOAD2 YYYY`; dtype model `BFLOAT16 = 0x6` all gens | [opcode-kernel-engine-matrix §5.3](opcode-kernel-engine-matrix.md) | the SUNDA-baseline + dtype-model pages | `[HIGH/OBSERVED]` |

### 5f. The `0xF0` extended-instruction dispatch + kernel_info_table

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| F1 | The `0xF0` two-level dispatch is an in-loop `if (opcode==0xF0) dispatch_extended(spec)` branch | There is **no in-loop `0xF0` branch.** The two-level dispatch is realized entirely by registering opcode `0xF0` **five times** in the `kernel_info_table` (one row per spec byte `0/1/2/4/3`); one linear key-scan lands a `(0xF0,spec)` on exactly one row | the 5 `0xF0` KIT rows w/ distinct spec bytes at key `+0x02`; each handler owns its own `.bss` state slot (`0x468`/`0x46c`/`0x470`) — proof they are independent kernels | [struct-device-firmware-globals §1.2 CORRECTION](struct-device-firmware-globals.md), [opcode-kernel-engine-matrix §4](opcode-kernel-engine-matrix.md) | the POOL `0xF0` + kernel-info-table pages | `[HIGH/OBSERVED]` |
| F2 | The `0xF0` spec table is sorted by `(opcode,spec)` (binary-searchable) | Registration order is **`0,1,2,4,3`** — spec 4 precedes spec 3. A linear key-scan is order-independent; a binary search mis-locates specs 3/4 | the KIT key column `7e 7c 7d 45 51 41 f0 f0 f0 f0 f0 …` in registration (not sorted) order | [struct-device-firmware-globals §1.2 QUIRK](struct-device-firmware-globals.md) | the POOL `0xF0` page | `[HIGH/OBSERVED]` |
| F3 | The `cptc_decode_impl<1..6>` template arg maps one-to-one onto the POOL spec byte (`0/3/4/7`) | The `<N>` is an `(unsigned char)` **dtype** selector chosen *inside* the cptc handler (reached via `0xE4` or `0xF0`-spec7), not the POOL spec byte | the demangled `cptc_decode_impl<N>(unsigned char)` signature + the `"unsupported in_dtype/out_dtype for cptc_decode"` error strings | [opcode-kernel-engine-matrix §4 GOTCHA](opcode-kernel-engine-matrix.md) | the cptc-codec + `0xF0` pages | `[HIGH/OBSERVED]` |

### 5g. Gather / scatter (the NKI 2:1 split)

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| N1 | One combined row "(gather / IndirectCopy) → GATHER `0x68` / INDIRECT_COPY `0xe7`" that does not pin which NKI name maps to which opcode | **2:1 and exact:** `nki.isa.nc_n_gather → emit_gather → GATHER 0x68` (`S4D4_GT`, within-partition flat gather, u32 index); `nki.isa.local_gather → emit_indirect_copy → INDIRECT_COPY 0xe7` (`S4D4_IC`, 8-core/16-partition software per-index loop, u16 index, ≤4096) | `GATHER = 0x68` (maverick `common.h:204 // Y`), `INDIRECT_COPY = 0xe7` (`:313 // Y`) — distinct opcodes; the routing is the verbatim `isa.py`/lowering comment in the NKI frontend; both are POOL kernels (`pool_gather` / `pool_indirect_copy`) | [validation/gather-scatter §7](../validation/gather-scatter.md), [correction-ledger §13](../reference/correction-ledger.md) | the NKI-Rosetta / gather-indirect-copy ISA pages | `[HIGH/OBSERVED]` |
| N2 | `local_gather` routes to `GATHER` (a name-trap: "gather" → GATHER) | **`local_gather` does NOT route to `GATHER`** — it routes to `INDIRECT_COPY 0xe7`. The shared per-lane `addr = base + offset·elem_sz` + `OOB → 0` value function grounds *both* `0x68` and `0xe7`; the routing/index-width is an ISel attribute (`can_lower_generic_load_to_gather`), not a value difference | the 4-oracle gather value (`OOB → 0`) bit-exact across SEM/nki/LIVE-`libfiss`; the lowering split documented in `indirection-gather` | [validation/gather-scatter §7](../validation/gather-scatter.md) | the NKI-Rosetta + gather pages | `[HIGH/OBSERVED value; routing documented]` |

### 5h. The `.data` delta, DWARF location, and count-grep grounding

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| D1 | Every binary's `.data` VMA = file-offset + a fixed `0x400000` | Only `.text`/`.rodata` have VMA == file-offset; the `.data`/`.data.rel.ro` delta is **per-binary**: `0x3000` (`libnrtucode_internal`) / `0x2000` (the INT `.data.rel.ro` where the `*_libs` tables live) / `0x200000` (the `ncore2gp` config DLLs) / `0x1000` (`libncfw`, CARRIED). The alleged `0x400000` appears in none | `readelf -SW` per-file: INT `.data.rel.ro` Δ `0x2000`; `libisa-core.so`/`libfiss-base.so` `.data` Δ `0x200000`; over-generalizing the delta lands a `.data`-resident struct read on the wrong bytes | [correction-ledger §2](../reference/correction-ledger.md), [codename-crosswalk-table §0 NOTE](codename-crosswalk-table.md) | any page reading a `.data`-resident struct by offset; the methodology page | `[HIGH/OBSERVED]` |
| D2 | The `ncore2gp` device config libs carry DWARF / the `_ZTV+0x10` vtable rule applies to INT | The config libs carry a **full `.symtab`** (19,720 `FUNC` in `libisa-core.so`) but **zero debug sections** — DWARF lives in the host `libnrt.so`. **INT has zero C++ vtables**; its `*_libs` entries are plain C `{SO_get, JSON_get}` 16-byte fn-ptr pairs (the `_ZTV+0x10` rule does not apply) | `readelf -S … \| rg debug` → empty on the config libs; `nm INT` shows fn-ptr-pair tables, no `_ZTV` | [correction-ledger §2](../reference/correction-ledger.md), [codename-crosswalk-table §0 NOTE](codename-crosswalk-table.md) | the methodology + struct pages; any vtable-slot computation on INT | `[HIGH/OBSERVED]` |
| D3 | A "symbol hit count" grepped from the decompile tree is the binary's count | Re-ground every count claim to `nm <binary> \| rg -c` on the *binary* nm table — decompile-tree greps inflate counts (raw-byte `rg -ac` vs `strings`-tokenised occurrences; cf. G7's 187→189) | the 187→189 / 125-symbol recount (G7); the count-discipline note repeated across the count-audit rows | [codename-crosswalk-table §5/§8](codename-crosswalk-table.md) | any page asserting a symbol/string count | `[HIGH/OBSERVED]` |

### 5i. The collectives config tape (NCFW / libnrt DWARF)

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| C1 | The op-list block carries "three u32 counters" (`op_num`, `function_n`, `tpb_compl_addr_num`) | Only `op_num@+3584` is u32; **`function_n` is u16 @+3588**, **`tpb_compl_addr_num` is u8 @+3590**, and `slot_spad_base@+2144` is an **array of 6** `addr_t`, not a scalar | the host DWARF DIE `<14f9029>` (`neff_configs`, 3592 B) field types read per-offset | [struct-device-firmware-globals §1.5a CORRECTION](struct-device-firmware-globals.md) | the collectives ring/protocol-config pages | `[HIGH/OBSERVED]` |
| C2 | The per-channel ring config stride is **149** (`0x95`) | The stride is **148** (`0x94`): the last stored field is `kangaring_num_peers @+0x90` (1 B), so a channel spans `+0x00..+0x90` = 145 used bytes + 3 pad inside a 148-byte stride | the byte-exact `lea`/`shl` chain in `ncfw_log_algo_ring_configs @0x8544` resolves to `cfg + 148·i` | [struct-device-firmware-globals §1.5d CORRECTION](struct-device-firmware-globals.md) | the ring/kangaring + mesh-collective pages | `[HIGH/OBSERVED]` |
| C3 | The libncfw printer's flag set is the whole on-wire `cc_op` record | The printer emits only **3 of the byte1 flags**; the on-wire record also carries `safe_mode` (byte1 bit3) + `unique_tensors` (byte1 bit4) + a reserved byte at entry+3. Size the entry at **8 B total**; the reduce op is **not** in this word (`SDMA_CCETYPE` rides the CCE descriptor) | the `spad_ctrl_cc_op_entry_t` bitfield layout + the packer `create_spad_ctrl_entry @0x232cd0` (libnrt) | [struct-device-firmware-globals §1.5a GOTCHA](struct-device-firmware-globals.md) | the collectives op-list / enums pages | `[HIGH/OBSERVED]` |

### 5j. The v5 reset geometry

| # | Superseded claim | Correct statement | Byte / structural evidence | Source | Affected | Tag |
|---|---|---|---|---|---|---|
| R1 | The MAVERICK reset shift is a uniform `−0x20` across all engines | `−0x20` is true **only** for DVE/PE/NX_POOL (`06 75 → j 0x1d8`). **SP is `−0x14`** (Top-Sync stub, `06 78 → j 0x1e4`, `+0xc`); **Q7_POOL is `−0x1c`** (`0x200 → j 0x1e4`, the Q7 core moving for the first time on any gen). The shared invariant is **`enter_run @0x94`** + the unchanged `.globstruct` magic `0x6099cb34` | every reset byte decoded with `ncore2gp` (exit 0); the per-engine head bytes `75`/`78` and `j` targets | [maverick-profile §8 CORRECTION](../generations/maverick-profile.md) | the v5 image pages; the reset/boot pages | `[HIGH/OBSERVED]` |

---

## 6. Carried stale-copy hazards

These are not fresh corrections but **stale-copy hazards**: early report text that was never
re-edited after a fix landed elsewhere. The disposition is uniform — **cite the fixer, not
the stale source.** Listed so a reviewer recognizes a re-introduction.

- **The CAYMAN/MARIANA codename crossing (G1)** appears in five early NCFW reports' row text;
  several were never re-edited. Key the codename off the `get_image` dispatch (G1), never the
  stale row text.
- **The "v4+ = first PeManageSeed" boundary (P1)** persists in any report that carved only
  CAYMAN + MARIANA_PLUS; the fixer is the MARIANA-between carve. Cite MARIANA-first.
- **The "187 MAVERICK strings" tally (G7)** and any count grepped from the decompile tree
  (D3) — re-ground to `nm`/`strings` on the binary.
- **The "+8 stride" shorthand (G6)** survives in prose on the cross-walk pages as deliberate
  shorthand for the `+7/+8/+8/+8` progression; read it as `coretype = arch_id + 1`, never as
  a derivable stride.
- **The "−0x20 v5 reset" shorthand (R1)** is correct only for DVE/PE/NX_POOL; use the
  per-engine table for SP/Q7.
- **The `ct37` = INFERRED note in #986 (§1)** is the live stale verdict this page overturns;
  it is scheduled for the Part-16 reconcile.

---

## 7. The long tail of single-number refinements

Low-traffic but real; each is a one-number tightening already landed on its owning page and
recorded here so it is not silently un-fixed:

- ring per-channel stride **148** not 149 (C2); op-list `function_n` **u16**/`tpb_compl_addr_num`
  **u8** not u32 (C1); MAVERICK string tally **189**/125/0 not 187 (G7); the `.data` deltas
  `0x3000`/`0x2000`/`0x200000`/`0x1000` not a flat `0x400000` (D1); FLIX **7 outcomes → 4
  byte-sizes** not "7 lengths" (§4); the SUNDA BF16 cluster is **five** ops not six (O6); the
  v5 reset shift **per-engine** (`−0x20`/`−0x14`/`−0x1c`) not uniform (R1).

---

## What this page is to its curated front copy

The [Part-0 ledger](../reference/correction-ledger.md) carries the sixteen highest-traffic
rows a working author meets most often; this appendix is **canonical** and carries those plus
the full register (the generation-binding rows, the profiler/PWP rows, the dual-fetch O1
resolution, the collectives config-tape refinements, the `.data`/DWARF grounding rows, the v5
reset geometry, and the live `ct37` divergence). When the two disagree, **this appendix wins**
and the front copy is wrong — file it. If you are about to assert a fact that *feels* like a
settled constant — a generation binding, an opcode's engine, the activation table's degree,
the FLIX length set, a section delta, or a v5 identity byte — check it here first, then
against the binary.

---

## See also

- [The Do-Not-Repeat / Correction Ledger](../reference/correction-ledger.md) — the curated
  Part-0 front copy (sixteen highest-traffic rows); this appendix is its exhaustive companion.
- [The Confidence & Walls Model](../reference/confidence-model.md) — what `[HIGH/OBSERVED]`,
  `[MED/INFERRED]`, `[CARRIED]`, and "wall" mean; the `arch_id 36` and `ct37` walls in full.
- [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) and
  [the Cross-Walk card](codename-crosswalk-table.md) — the rows G1–G7 / §1 / §2 feed.
- [The MAVERICK (v5) Profile](../generations/maverick-profile.md) — the v5 walls, the ACT→DVE
  fold, the `0xe3` resolution (rows A3/A4/O2/O3/R1).
- [PROF_CAM / PROF_TABLE Blob Formats](../images/prof-cam-table-formats.md) — rows A1/A2/A5.
- [HW-Decode vs Sunda Dual Fetch](../firmware/seq/dual-fetch.md) — rows S1–S4 (the O1
  resolution and the Sunda-mode distinction).
- [Sort / DECODE_SORT](../firmware/kernels/sort.md) and
  [The Opcode ↔ Kernel ↔ Engine Matrix](opcode-kernel-engine-matrix.md) — the SortMerge
  phantom (§3) and rows P2/O2/O4/O5/O6/F1/F3.
- [VAL — Gather / Scatter](../validation/gather-scatter.md) — rows N1/N2.
- [Device-Firmware Global Structs](struct-device-firmware-globals.md) — rows S5/C1/C2/C3/F1/F2,
  and the #986 `ct37` verdict this page corrects (§1).
