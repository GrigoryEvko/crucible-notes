# The Do-Not-Repeat / Correction Ledger

This is the **curated front copy** of the do-not-repeat ledger: the short, high-traffic
list of facts that *look* plausible, were once stated one way in an early analysis pass,
and turned out — under a direct re-read of the shipped binaries — to be wrong. Each row
pairs the **superseded claim** with the **correct statement**, names the **binary
evidence** that settles it, and lists the **downstream pages** a re-introduction would
poison. Read it before you author anything that touches generations, opcodes, the
activation datapath, the dual fetch front-end, or the FLIX encoding — these are the traps
that a confident-but-naive reading falls into, and every one of them has already cost an
analysis pass.

This page is *not* exhaustive. It carries the corrections a working author meets most
often. The complete register — all sixty-seven cross-report corrections plus the carried
stale-copy hazards — is the
[Full Do-Not-Repeat / Correction Ledger](../appendix/do-not-repeat-full-ledger.md) in the
appendix. When the two disagree, the appendix is canonical and this page is wrong; file it.

The confidence vocabulary used in every row (`HIGH/MED/LOW` × `OBSERVED/INFERRED/CARRIED`)
and the *walls* a correction sometimes leaves behind are defined once, normatively, in
[The Confidence & Walls Model](confidence-model.md). The two standing walls this ledger
keeps live — `arch_id 36` (v5, **INFERRED**: no firmware byte) and `ct37` (v5 coretype,
**OBSERVED**: two resolver immediates) — recur in several rows below and are explained
there in full.

---

## How to read a row

Each correction is one block:

- **Superseded** — the wrong claim, phrased the way it was actually written, so you can
  recognize it if you meet it again in an early report or a naive first draft.
- **Correct** — the binary-grounded statement, tagged `[confidence/provenance]`.
- **Evidence** — the symbol, offset, section, enum line, or section-header read that
  settles it. This is what you re-run if you doubt the row.
- **Affected pages** — where a re-introduction does damage, so a reviewer knows where to
  look.

> **CORRECTION** is the wiki-wide callout for a fact that overturns an earlier or naive
> reading. Every row here is a CORRECTION; the framing is this page's entire job, so the
> callout is implicit rather than repeated on each block.

The two corrections at the very top — **FLIX byte-lengths** and **the `.data` delta** —
were confirmed straight from the section tables and the TIE config header this pass; they
are the freshest entries and the most load-prone, because both *feel* like settled
constants and are not.

---

## 1. FLIX length: 7 length-class outcomes map to 4 distinct instruction byte-sizes — two different levels

**Superseded.** "The FLIX VLIW encoding is **14 formats / 46 slots / 7 lengths**", written
as if **7** were the count of distinct *instruction byte-lengths*. The error is the
flattening of two different levels onto one "lengths" axis: the runtime decoder produces
**7 length-class outcomes**, but those collapse to only **4 distinct instruction
byte-sizes**. The number that belongs on the "how many bytes is an instruction" axis is 4,
not 7.

**Correct.** Both figures are real and neither supersedes the other — they live on
different levels:
- The runtime `length_decoder` (`@0x3b5a50`) + 256-entry `length_table` (`@0x3d4100`)
  yield **7 length-class outcomes** — the `op0==0xF` 8-vs-16 split keyed on `byte3`, the
  `{2, 3, 16}` direct lengths, and the illegal `-1`. (This is the figure validated 167/167
  vs the device objdump.)
- Those outcomes resolve to exactly **4 distinct positive instruction byte-sizes
  `{2, 3, 8, 16}`** — the *set* of the static `XCHAL_OP0_FORMAT_LENGTHS` vector.

State it as **"7 length-class/table outcomes → 4 distinct byte-sizes `{2,3,8,16}`"**, never
either number alone as "the number of lengths". `[HIGH/OBSERVED]`

**Evidence.** Both halves are read straight from the corpus this pass. The static byte-size
set comes from the shipped Cadence config header `tie.h`:

```
extracted/.../tools/ncore2gp/xtensa-elf/arch/include/xtensa/config/tie.h
  #define XCHAL_OP0_FORMAT_LENGTHS   3,3,3,3,3,3,3,3,2,2,2,2,2,2,16,8
```

That is sixteen entries (eight at 3, six at 2, then 16 and 8); the *set* of values is
`{2, 3, 8, 16}` — **four** distinct byte-sizes, period. The byte-keyed companion
`XCHAL_BYTE0_FORMAT_LENGTHS` tiles the same sixteen-value pattern across all 256 first bytes
and adds no new size. The 7-outcome figure comes from the runtime `length_table` itself:
its 256 `int32` cells take exactly the value census `{-1:2, 2:96, 3:128, 8:8, 16:22}`
(re-dumped this pass), and the `op0==0xF` column splits on `byte3.low4` into 8 (even
`b3lo`), 16 (odd `b3lo ∈ {1,3,5,9,b,d}`), or `-1` (`b3lo ∈ {7,f}`) — the split that the
static byte-0-only macro cannot express. The format count `num_formats=0xe` (14) and slot
count `num_slots=0x2e` (46) are independently correct, read from `libisa-core.so`.

**Affected pages.** [Index](../index.md), [Master Glossary](../glossary.md),
[FLIX Bundle-Decoding Methodology](flix-decoding.md) (all already state the two-level
"7 outcomes → 4 byte-sizes" framing — keep it that way), and
[The FLIX VLIW Encoding](../isa/core/flix-encoding.md). When you write a length-resync
sweep, the *advance* table has **4** byte-size outcomes; a sweep that hard-codes seven
distinct byte-lengths has a dead arm and will mask a real desync — but the decoder it ports
genuinely has 7 length-classes, so do not delete the `op0==0xF` byte-3 branch.

---

## 2. The `.data` VMA↔file-offset delta is per-binary, not a constant `0x400000`

**Superseded.** "For every binary in the corpus, the `.data` section's virtual address
equals its file offset plus a fixed `0x400000`, so you can read a `.data`-resident struct
by adding `0x400000` to its file offset (or subtracting it the other way)." The constant
`0x400000` was over-generalized from one binary onto all of them.

**Correct.** Only **`.text` and `.rodata` have VMA == file-offset.** The `.data` (and
`.data.rel.ro`) delta is **per-binary and must be measured per file** with
`readelf -SW`. Measured this pass:

| Binary family | `.data` delta (VMA − file-off) |
|---|---|
| `libnrtucode_internal.so` (nrtucode) | **`0x3000`** |
| `ncore2gp` config libs (`libisa-core.so`, `libfiss-base.so`) | **`0x200000`** |
| `libncfw.so` (host runtime) | **`0x1000`** `[CARRIED]` |

`[HIGH/OBSERVED]` for the two families present in this extracted tree; the `libncfw.so`
figure is `[CARRIED]` from the runtime-lane analysis (that archive is not in this
checkout).

**Evidence.** Straight from the section tables:

```
readelf -SW .../c10/lib/libnrtucode_internal.so
  .rodata  Addr 00000000000046b0  Off 0046b0   (delta 0)
  .data    Addr 00000000009ba4a8  Off 9b74a8   (delta 0x3000)

readelf -SW .../ncore2gp/config/libisa-core.so
  .text    Addr 0000000000312c10  Off 312c10   (delta 0)
  .rodata  Addr 00000000003b6e40  Off 3b6e40   (delta 0)
  .data    Addr 0000000000764040  Off 564040   (delta 0x200000)

readelf -SW .../ncore2gp/config/libfiss-base.so
  .data    Addr 0000000000c8eb68  Off a8eb68   (delta 0x200000)
```

`0x3000` ≠ `0x200000` ≠ `0x1000`: the delta is not a constant, and the alleged `0x400000`
appears in none of them. The two config libs happen to *share* `0x200000`, but that is a
linker artifact of that toolchain, not a portable rule. Over-generalizing the delta makes
every `xxd`/`objdump` read of a `.data`-resident struct land on the wrong bytes — a
spurious "wrong struct" finding that has already happened in a sibling corpus.

**Companion correction — where the DWARF lives.** The same gotcha-cluster carried a second
wrong belief: that the device config libs carry DWARF. They do **not**. The `ncore2gp`
config libs (`libisa-core.so`, `libfiss-base.so`, `libcas-core.so`) carry a **full
`.symtab`** (e.g. 19,720 `FUNC` symbols in `libisa-core.so` — full names, so symbol-keyed
reads work) but **zero debug sections** (`readelf -S | rg debug` → empty). The DWARF that
lets you resolve struct field offsets lives in the **host `libnrt.so`**, not in the device
config libs. `[HIGH/OBSERVED]` Author accordingly: name a function from the config lib's
symtab, but reach for DWARF only in the host runtime library.

**Affected pages.** Any page that reads a `.data`-resident structure by offset — the
[Methodology](methodology.md) section-read recipes, the profiler-extension and CSR-map
pages. Always `readelf -SW` the *specific* file first.

---

## 3. NCFW codename↔arch binding: `0x0c = CAYMAN/v3`, `0x14 = MARIANA/v4`

**Superseded.** Five early NCFW reports printed the inverse codename text on the v3/v4
rows — "`0x0c → v3 mariana.c`, `0x14 → v4 cayman.c`" — swapping which codename owns which
arch_id.

**Correct.** **`0x05 = SUNDA/v2`, `0x0c = CAYMAN/v3`, `0x14 = MARIANA/v4`,
`0x1c = MARIANA_PLUS/v4+`.** `[HIGH/OBSERVED]` The arch_id→`v#` numbers were always right;
only the *codename text* on the v3/v4 rows was inverted, and every structural count
(mesh-event 50/108, the per-channel stride) is arch_id-keyed, so the counts are
unaffected — only the name you attach to them flips.

**Evidence.** The `libncfw` `get_image` selector ladder disassembles byte-exact: it
compares `{0x05, 0x0c, 0x14, 0x1c}` with `ja → default` for `arch_id > 0x1c`, loading
`v3_ncfw_*.bin` at `cmpl $0xc` and `v4_ncfw_*.bin` at `cmpl $0x14`. The `ctx_log` selector
agrees (`arch 12 → cayman_ncfw_ctx_log`). Key codenames off the `get_image` dispatch, never
off the stale row text.

**Affected pages.** [Codename ↔ Generation Cross-Walk](codename-crosswalk.md), every
per-generation firmware page. A synthesis that keyed images by the swapped codename text
would invert CAYMAN and MARIANA across the whole guide.

---

## 4. CAYMAN = v3 = Trn2 (not Trn1/Inf2); the "N2.5" row was a conflation

**Superseded.** A platform note carried a tension reading "v2_ncfw cayman = Trn1/Inf2"
(the so-called N2.5 / platform-#1044 row), attaching the *cayman* codename to the v2 row
that serves Trn1+Inf2.

**Correct.** **CAYMAN = v3 = Trn2.** SUNDA is the v2 that serves *both* Trn1 and Inf2; the
cayman codename was mis-attached to that v2 row. `[HIGH/OBSERVED]`

**Evidence.** The collectives topology builders bind it: `topo_neuron_cayman.o` builds
Trn2 topologies, while `topo_neuron_sunda.o` builds the Trn1+Inf2 set. This is consistent
with the NCFW selector leg `0x0c → v3/CAYMAN` (§3). Key off `topo_neuron_cayman.o = Trn2`;
never the N2.5 row.

**Affected pages.** [Codename ↔ Generation Cross-Walk](codename-crosswalk.md), the
collectives and platform pages.

---

## 5. PROF_CAM is a per-engine instruction *profiler* CAM, not the activation PWL lookup

**Superseded.** Two readings: that the 47-record `PROF_CAM` holds "47 ACT opcodes," and
that it *is* the activation/transcendental piecewise-lookup table (so `0x30` Exponential
"routes through the PWL via PROF_CAM").

**Correct.** **`PROF_CAM`/`PROF_TABLE` is a generic, cross-engine hardware-decode
*instruction profiler* CAM present on every NX engine** — keyed on opcode, with a 16-byte
`{opcode, mask, enable, rsvd}` record. The activation PWL is a **separate, ACT-only**
datapath (see §6). `[HIGH/OBSERVED]`

**Evidence.** All four CAYMAN per-engine PROF_CAM blobs (ACT/DVE/PE/POOL) are
byte-identical (sha `8fd7e422`) — a generic profiler, not 47 activation-specific entries.
The MARIANA DVE PROF_CAM *arms* opcode `0x30` (Exponential, slot 45) and `0xe2` (Rand2,
slot 41) with `enable=1`; CAYMAN DVE arms neither (a clean negative control). A profiler
that can arm `0x30` for *profiling* is plainly not the table that *computes* `0x30`. The
60-symbol PROF census is exactly 30 getters + 30 blobs over 15 (gen,engine) pairs ×
{CAM, TABLE}.

**Affected pages.** The activation-engine and profiler pages, every ISA page that touches
`0x30`. Do not describe PROF_CAM as the activation lookup.

---

## 6. The activation table is piecewise-*cubic* (PWP), and on v5 it lives in the DVE block

**Superseded.** "The activation table is a linear piecewise-linear (PWL) interpolation
storing `{intercept, slope, breakpoint}` per segment," and "the MAVERICK ACT→DVE move is
merely a schedule/profile-arm fold."

**Correct.** Each bucket entry is a **degree-≤3 polynomial** `{float d0, d1, d2, d3, x0}`
evaluated in `t = (x − x0)` — **piecewise-cubic (PWP)**, not linear. The activation quad is
`CAM(opcode,func_id)` + `PROFILE(128 B)` + `CONTROL(act_tbl_base:11 / extract_lsb:5 /
extract_size:4)` + `BUCKET(32 B cubic)`. And the MAVERICK **ACT→DVE fold is a real
hardware-region migration**: the activation PWL SRAM (`ACT_CONTROL` / `PWP_CONTROL` /
`PWP_BUCKETS`) moves *into* the TPB_DVE block on v5. `[HIGH/OBSERVED]`

**Evidence.** `tpb_activation_entries.h` (cayman) gives the bucket struct as four floats
`d0@0 / d1@4 / d2@8 / d3@12` plus the breakpoint `x0@16` (32 B). A linear PWL would carry
`{intercept, slope, breakpoint}` only — the `d2`/`d3` cubic terms are physically present.
The CONTROL entry carries `act_tbl_base:11 / extract_lsb:5 / extract_size:4`. On MAVERICK
the PWL region is resident in the TPB_DVE block. This *deepens* the ACT→DVE correction
(§7) — it is a region migration, not a contradiction.

**Companion (still a wall).** The PWP *format* is OBSERVED; the per-function cubic
*coefficients* (relu/gelu/sigmoid/…) are host-supplied at runtime and never appear in the
image — `[LOW / not-in-corpus]`. State the format; do not invent coefficients.

**Affected pages.** The activation-engine page, the transcendental-op pages.

---

## 7. The MAVERICK ACT→DVE fold renames the datapath; it is not an opcode-count growth

**Superseded.** "MAVERICK grew its ACT opcode count (159 → 165); there must be a new ACT
handler image."

**Correct.** **MAVERICK ships no NX_ACT image.** The "+10 PROF-armed" ACT opcodes
(`0x23/0x25/0x58/0x61/0x62/0x6c/0x6d/0x6e/0x6f/0x99`) already exist in the MARIANA ISA
enum — it is a **PROF-table re-arm plus a datapath rename**, with ACT opcodes executing and
profiling on the DVE. `[HIGH/INFERRED]` (the absence is OBSERVED; the causal reading is
inferred-high) Combined with §6, the fold is also a hardware-region migration of the PWL
SRAM into the DVE block.

**Evidence.** MAVERICK DVE DRAM carries **0** DGE strings (vs 13 on MARIANA_PLUS), and the
MAVERICK image has no NX_ACT region — the opcodes are pre-existing MARIANA enum entries,
not v5 additions.

**Affected pages.** The activation-engine, DVE, and v5/MAVERICK pages.

---

## 8. "Sunda-mode" is a runtime SW-fetch fallback, not the SUNDA generation

**Superseded.** Reading "Sunda-mode" as *the SUNDA (v2) generation's* firmware — i.e.
treating the string as a per-generation label.

**Correct.** **"Sunda-mode" is a runtime software-fetch fallback present on CAYMAN-and-up
images**, selected by the host chicken bit; it has nothing to do with the SUNDA
generation. `[HIGH/OBSERVED]`

**Evidence.** The string `"NX in Sunda mode: HW decode disabled"` is present in the CAYMAN
POOL DRAM (`@0xef5`) and MARIANA_PLUS (`@0xeeb`) but **absent** from the SUNDA-generation
POOL DRAM (which carries only `/…/sunda/…` *source-path* strings). The decisive disproof:
if "Sunda-mode" were the SUNDA generation, the SUNDA image would itself *be* the Sunda-mode
firmware — it is not. The host selector is `disable_hw_decode = CSR 0x4000[0]`.

**Affected pages.** The dual-fetch front-end page, the firmware-mode pages, any v2 page.

---

## 9. The O1 polarity: HIGHER table = HW-Decode, LOWER = Sunda (the IMG label was inverted)

**Superseded.** The carve labels in three MARIANA reports tagged the **lower**-address
dispatch table "HW-Decode" and the **higher** "Sunda" — by IRAM position, with no FSM
proof. This was the last live RED contradiction in the corpus (O1).

**Correct.** The polarity **inverts** the positional label:
**HIGHER table `@0x80adc` = HW-Decode** (the hardware-FIFO-assisted fetch, default on
v3/v4); **LOWER table `@0x80814` = Sunda-mode** (the SW-fetch fallback). SUNDA (v2) has no
dual front-end and is mode-unsupported. The "lower = HW-Decode" label is a **positional
authorship artifact**, now refuted. `[HIGH/OBSERVED]`

**Evidence.** A FLIX-aware decode of the `0x31ac` FSM span (which scalar `objdump`
desyncs) byte-binds each dispatch site to its FSM. The HW-decode FSM is the *only* body
carrying the `RTL_PC_check_delta` HW-FIFO↔SW-cache coherence telemetry and the iteration
counter — a whole-IRAM `const16` uniqueness census finds exactly **one** site each
(`@0x31d5` iter-log, `@0x326e` RTL_PC_check), both inside the FSM that builds DRAM
`0x80adc` via `const16 a3,0x0adc @0x36ce`. The Sunda FSM (`@0x2d81`, SW cursor, no
coherence telemetry) builds `0x80814`. CAYMAN DVE corroborates the same lower=Sunda /
higher=HW-decode geometry on a second engine.

**Residual (a wall, not a reopen).** The single boot-flag bit → mode-gate-predicate
*register* sits in one FLIX `<undef>` slot past the decoder's slot-table coverage
`[BOUNDED]`. It does not affect the binding: each FSM hard-codes its own `const16` table
base in scalar code, so the un-decoded gate cannot re-route an FSM to the other table.

**Affected pages.** The dual-fetch front-end page (present the resolved binding GREEN, with
the IMG "lower=HW-Decode" flagged as a corrected positional artifact and the gate-slot
register footnoted as a FLIX boundary).

---

## 10. `ct37` is OBSERVED; `arch_id 36` stays INFERRED (the two v5 walls)

**Superseded.** Treating the MAVERICK (v5) `arch_id` as observed firmware, or treating the
v5 coretype as a pure extrapolation.

**Correct.** **coretype 37 (`ct37`) is OBSERVED** — two ways; **`arch_id 36` is INFERRED**
— no firmware byte. `[ct37: HIGH/OBSERVED]` `[arch_id 36: HIGH-bounded/INFERRED]`

**Evidence.** `ct37`: the `nrtucode.h` coretype enum places `MAVERICK_Q7_POOL` at ordinal
37, *and* the two resolvers gate on `cmp $0x25` (= 37) plus a `movabs` immediate whose
bit 37 is set (`0x2020202000` → bits `{13,21,29,37}`; `0x2020202040` → `{6,13,21,29,37}`).
`arch_id 36`: the only `arch_id`-keyed firmware (the `libncfw get_image`/`ctx_log`
selectors) compares exactly `{0x05,0x0c,0x14,0x1c}` with `ja → default` for
`arch_id > 0x1c` — there is **no `0x24` (36) leg**, and zero MAVERICK strings in
`libncfw`. So `arch_id 36 = ct37 − 1 + the +8 stride` is a correctly-bounded *inference*,
not a read.

**Affected pages.** Every v5/MAVERICK page carries both walls wherever a v5 claim appears.
State `ct37` as observed; star `arch_id 36` as inferred.

---

## 11. The platform "CoreV5" label ≠ the GPSIMD `ct37`/NC-v5 MAVERICK

**Superseded.** Reading the platform compiler's ArchLevel "CoreV5 (trn3pre variant)" as a
MAVERICK instantiation — i.e. equating the two "v5"s.

**Correct.** There are **two distinct "v5" axes.** Platform "CoreV5 (trn3pre variant)" =
**Trn3-pre / MARIANA_PLUS** (coretype 29 / `arch 0x1c`, an NCFW-*present* generation). The
GPSIMD `ct37` / NC-v5 **MAVERICK** is firmware-internal-only (zero sibling references).
Never equate them. `[HIGH/OBSERVED axes; the "CoreV5 = Trn3-pre" reading INFERRED-strong]`

**Evidence.** The NCFW selector binds `0x1c = v4+/MARIANA_PLUS` and has no `0x24 =
MAVERICK` leg (§10). The "trn3pre variant" label is explicit on the platform ArchLevel
string.

**Affected pages.** The codename/generation pages, any page citing a platform "CoreV5"
label.

---

## 12. PeManageSeed first appears at v4 (MARIANA), and it manages PSUM SR-RNG seeds

**Superseded.** Two readings: that PeManageSeed first appears at v4+ (MARIANA_PLUS), and
that it manages "a PE-array per-cell PRNG" with no struct of its own.

**Correct.** **PeManageSeed (`0x08`) first appears at v4 = MARIANA** (CAYMAN has none), and
it manages the **PSUM `fp32→bf16` stochastic-rounding RNG seeds** — 2048 seeds per PSUM on
v4, 32-bit each — via its own 64-byte `S2S1D2_PE_SEED` struct. `[HIGH/OBSERVED]`

**Evidence.** The first-appearance boundary was a carve-coverage artifact: the report that
said "v4+" had only carved CAYMAN and MARIANA_PLUS, never MARIANA-in-between. The MARIANA
PE image self-names `PeManageSeed` at DRAM offsets byte-identical to MARIANA_PLUS; CAYMAN
PE has none — so the boundary is CAYMAN(no) → MARIANA(first) → MARIANA_PLUS(retained). The
struct compiles to 64 B; the header text is explicit about the PSUM SR seeds. It still
drives LdWeight/Matmul micro-ops at the firmware level, but the *managed state* is the
PSUM SR-RNG, not a per-cell PRNG.

**Affected pages.** The PE-engine page, the RNG/stochastic-rounding pages.

---

## 13. The NKI gather split is 2:1 — two names, two opcodes, pinned

**Superseded.** One ambiguous combined row "(gather / IndirectCopy) → GATHER `0x68` /
INDIRECT_COPY `0xe7`" that did not pin which NKI name maps to which opcode.

**Correct.** The mapping is **2:1 and exact**:
`nki.isa.local_gather → emit_indirect_copy → INDIRECT_COPY 0xe7` (`S4D4_IC`, an 8-core /
16-partition software per-index loop); `nki.isa.nc_n_gather → emit_gather → GATHER 0x68`
(`S4D4_GT`, a within-partition flat gather). `[HIGH/OBSERVED]`

**Evidence.** `GATHER 0x68` (maverick enum line 204, `// Y`) and `INDIRECT_COPY 0xe7`
(line 313, `// Y`) are distinct, genuinely-different opcodes; the routing is the verbatim
`isa.py` comment in the NKI frontend. Present the 2:1 binding, never the combined row.

**Affected pages.** The NKI-Rosetta/opcode-mapping page, the gather/indirect-copy ISA
pages.

---

## 14. SortMerge is a phantom — named in a task plan, never a shipped opcode

**Superseded.** A task plan targeted "SortMerge" as a hardware opcode to document.

**Correct.** **There is no SortMerge HW opcode.** The targeted byte `0x97` is **commented
out** in the enum, and `0x98` is `TENSOR_SCALAR_SELECT` (a different, real op). SortMerge
is named-but-never-shipped — do **not** document a SortMerge hardware opcode.
`[HIGH/OBSERVED — negative]`

**Evidence.** The enum read: `0x97` carries a comment-out marker; `0x98` resolves to
`TENSOR_SCALAR_SELECT`. The TSPtr siblings the same plan touched are already covered
elsewhere — do not duplicate them either.

**Affected pages.** The ISA opcode-roster pages, the search/sort-op pages. A SortMerge row
is a fabricated datapath; reject it on sight.

---

## 15. The MAVERICK "+6" enum growth is byte-named

**Superseded.** A caveat that the prompt's `0x26/0xf3/0xf4` were "not corroborated" as the
MAVERICK new opcodes.

**Correct.** All six v5 additions are now OBSERVED in the maverick `common.h` enum
(`// Y`): **`0x26 ACTIVATE_MULTIPASS`** (spec-present but image-dormant), **`0xb6
COMPACT_CONTROL_INST`**, **`0xb9 DMA_MEMCPY2`**, **`0xba DMA_IMMEDIATE`**, **`0xf3
TENSOR_TENSOR_INT_WIDE`**, **`0xf4 TENSOR_SCALAR_INT_WIDE`**. The `0x26/0xf3/0xf4` bytes
that could not be corroborated *are* the genuine v5 additions. `[HIGH/OBSERVED]`

**Evidence.** Direct enum reads on the maverick header; `0xb6` additionally re-verified as
`COMPACT_CONTROL_INST` at maverick line 266.

**Affected pages.** The v5/MAVERICK opcode-roster pages.

---

## 16. Two opcode-classification traps: `0xb4`/`0xb6` and `0x72`

These two are smaller but recur, because both look like something they are not from enum
adjacency or a single-engine first read.

**`0xb4` / `0xb6` are NX control-spine, not DMA/transpose.** `0xb4 TEST_EVENT_SEM` and
`0xb6 COMPACT_CONTROL_INST` sit by enum-byte adjacency next to the `0xb8..0xbd` DMA band,
so a reader of a "DMA/transpose cluster" heading can mis-file them as data movers. They are
not. `0xb4 → CTRL_TEST_ES_STRUCT` (a sem/event batch test-read-update control op); `0xb6 →
CTRL_CCI_STRUCT` (a packed scalar-ALU control bundle). The genuine DMA/transpose cluster is
the five `{0xb8, 0xb9, 0xba, 0xbd, 0xf1}`. `[HIGH/OBSERVED]` (`0xb4`: mariana line 261 /
maverick line 264; `0xb6`: maverick line 266 — both `// Y`, absent on lower gens.)

**`0x72 COPY_PREDICATED` is DVE-native, not POOL.** An early ledger engine-tagged `0x72` as
POOL. It is the **DVE-native** base of the predicated-op family
`{0x72 copy / 0x99 cast / 0xe8 scalar / 0xea copy+reduce}` (struct `S3S3D3_TT`,
src0-integer predicate). `[HIGH/OBSERVED]` The enum carries it `// Y` on all four gens
(sunda 203 / cayman 206 / mariana 211 / maverick 214), and `CopyPredicated` is co-resident
with `CastPredicated` in the NX_DVE debug blobs — absent from both carved POOL kernel-info
tables.

**Affected pages.** The opcode-roster and engine-assignment pages, the DMA/transpose page,
the DVE predicated-op page.

---

## What this page deliberately omits

This curated copy carries the corrections an author hits most often. It does **not** carry:

- the long tail of single-number refinements (a stride `148` not `149`, a struct field at
  `@12` not `@16`, a placement headline off by 14) — those are real but low-traffic and
  live in the [full appendix ledger](../appendix/do-not-repeat-full-ledger.md);
- the carried *stale-copy hazards* (early report text never re-edited after a fix landed) —
  also in the appendix, with the "cite the fixer, not the stale source" disposition;
- the multi-engine RNG `0x77`/`0x78` re-tag, the search-select `0x6c..0x6f` SUNDA-floor
  correction, the `impl<4>` mixed-sign revision, and the compiler-side descriptor-struct
  gap closure — folded into the appendix.

If you are about to assert a fact that *feels* like a settled constant — a generation
binding, an opcode's engine, the activation table's degree, the FLIX length set, or a
section delta — check it here first, then against the appendix, then against the binary.
The corrections above are precisely the ones that survived a first plausible reading and a
second look, and were only caught on the third.

---

## See also

- [The Confidence & Walls Model](confidence-model.md) — what `[HIGH/OBSERVED]`,
  `[MED/INFERRED]`, `[CARRIED]`, and "wall" mean; the `arch_id 36` and `ct37` walls in full.
- [Full Do-Not-Repeat / Correction Ledger](../appendix/do-not-repeat-full-ledger.md) — the
  exhaustive register: all sixty-seven corrections plus carried stale-copy hazards.
- [Codename ↔ Generation Cross-Walk](codename-crosswalk.md) — the authoritative
  arch_id↔codename↔platform table the §3/§4/§10/§11 rows feed.
- [FLIX Bundle-Decoding Methodology](flix-decoding.md) — the 7-outcome length decoder →
  4-byte-size advance (§1) in use.
- [The FLIX VLIW Encoding](../isa/core/flix-encoding.md) — 14 formats / 46 slots /
  7 length-class outcomes → 4 distinct byte-sizes `{2,3,8,16}`.
