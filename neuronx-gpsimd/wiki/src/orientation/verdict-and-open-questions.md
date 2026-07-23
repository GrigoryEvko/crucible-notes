# The Reimplementation Verdict & Open-Questions Map

> *This is the capstone of Part 1. It answers the one question a reader brings to a
> reimplementation reference — **"can a senior engineer rebuild a Vision-Q7-compatible GPSIMD
> engine from this guide, and at what confidence?"** — in two halves. First **the Verdict**: what
> is rebuildable today, at what confidence, with the headline coverage fractions and the
> generation scope they cover. Then **the Open-Questions Map**: every residual unknown that
> survives, categorized by what would close it, each pinned to the page that owns it. The honest
> bottom line up front, then the honest residual.*

The [front door](../index.md) carries the verdict table; the [Seven Faces](seven-faces.md) page
develops *Face 7 — the Evidence* into a promise that the value basis is execution-validated. This
page is where that promise is **cashed and bounded**: it states the verdict as a defensible
yes/at-what-confidence answer, and then maps the small, fully-named ledger of what is *not*
answerable from this shipped corpus — so a reimplementer knows both what to encode as a hard
requirement and exactly where the edges are.

Every figure on this page carries its provenance, and every wall its closability, per
[The Confidence & Walls Model](../reference/confidence-model.md). The numbers here are the
**reconciled** wiki figures (the later VAL-20 / ISS-20 execution census, not an earlier
snapshot); they match [What GPSIMD Is](what-gpsimd-is.md), the [front door](../index.md), and the
Part-0 reference pages exactly. This page is the **navigable** companion to the two exhaustive
appendix registers (`appendix/open-questions-register.md` and `appendix/coverage-ledger.md`);
those are the long form, this is the map.

---

# Part A — The Verdict

## A.1 The bottom line, in one paragraph

**Yes — for v2–v4, a senior engineer with LLVM/Tensilica experience can rebuild a
Vision-Q7-compatible GPSIMD engine from this guide as a hard specification.** The instruction set
is closed on three independent axes — **encoding** (a certified-perfect non-overlapping cover),
**value semantics** (every value opcode's per-element function resolved), and **execution-proof**
(the shipped value oracle driven live as arbiter) — and the firmware mechanism, the custom-op
ABI, the host runtime spine, and the device dispatch are field-exact for the v2–v4 silicon. The
single most powerful technique behind that strength is that the shipped value simulator
(`libfiss-base.so`) is **callable in-process with no license**, so the binary itself is the
arbiter of value semantics rather than a static guess. `[HIGH/INFERRED over the named denominators]`

The generation scope is precise and must be carried on every per-generation claim:

- **v2–v4 (SUNDA / CAYMAN / MARIANA / MARIANA_PLUS): byte-grounded, execution-validated.** Their
  firmware images, config, and ABI are present and read directly; their value semantics are closed
  by execution. Target these as a hard specification. `[HIGH/OBSERVED]`
- **v5 (MAVERICK): header-OBSERVED + bounded-INFERRED.** The identity/header surface is read
  (`coretype 37`, the gen-selection bitmask arm, the Maverick getter symbols, the clang-15
  firmware `.comment`), but the v5 firmware *interiors* — the `arch_id`, the collective firmware
  image, the v5-specific dispatch bodies — are inferred or file-absent and **flagged on every
  use**. Publishable only as header-OBSERVED + bounded-INFERRED; never fabricate a v5 part-binding.
- **v1 (TONGA): pre-unified outlier.** A register-block ISA that predates the unified opcode
  namespace; treated like v5 — header-OBSERVED where present, interiors flagged. R(Q7) covers
  exactly v2..v5; TONGA is out of that domain by design.

## A.2 The five headline coverage figures

These are the numbers a reimplementer plans against. Each is `nm`-grounded or proven-by-execution
on the shipped binary, and each carries its provenance. They are the *reconciled* wiki figures and
are identical to the verdict tables in [What GPSIMD Is](what-gpsimd-is.md) §5 and the
[front door](../index.md).

| Axis | Result | Provenance |
|---|---|---|
| **Value semantics** | **864 / 864 = 100%** — every `module__xdref_` value leaf in `libfiss-base.so` resolved to a root with per-element semantics; the element function of every GPSIMD value opcode is known | `[HIGH/OBSERVED]` — `nm libfiss-base.so \| rg -c module__xdref_` = 864 |
| **Execution-validated** | **~95%** of value-bearing leaves proven-by-execution against the **live** shipped simulator, across **18 op families** / **~2.09M** in-process comparisons; **zero** firmware value bugs found | `[HIGH/OBSERVED]`; **MED** for the exact `~95%` percentage |
| **Encoding** | **1,534 / 1,534** shipped Vision-Q7 mnemonics, folded from the **1,607 / 1,607** pre-fold TIE-DB roster (**12,642** placements across that 1,607-mnemonic superset) — a certified-perfect, non-overlapping cover; canonical encoder `libisa-core.so` bit-exact | `[HIGH/OBSERVED]` |
| **Struct census** | **~169 / ~171** domain structs field-exact (**~99%**); every struct on the path a custom op actually travels is recovered | `[HIGH/OBSERVED]` |
| **Firmware value bugs** | **ZERO** — across the ~2.09M differential comparisons, every apparent divergence root-caused to the reference *model* or a *tool*, never the firmware | `[HIGH/OBSERVED]` |

> **NOTE — why `~95%` is `MED` on the percentage but `HIGH` on the fact.** The *fact* that
> ~2.09M live comparisons over 18 op families produced zero firmware value mismatches is
> `[HIGH/OBSERVED]` — it is what the binary returned when executed. The exact *fraction* of
> value-bearing leaves that carry a differential certificate (`~95%`) is `MED`: it is a census
> over a denominator that the later VAL passes widened (from ~85–90% over 9 families to ~95% over
> 18). Encode "the value lane is the arbiter and found no firmware bug"; treat the precise
> percentage as a bounded estimate, not a constant.

> **GOTCHA — pair the placement count with the right roster.** The **12,642** placement total is
> the count across the **1,607-mnemonic pre-fold TIE-DB superset**, not the 1,534 shipped roster
> (whose own placement total is 12,569). Cite the matched pairs — **1,607 / 12,642** (pre-fold) or
> **1,534 / 12,569** (shipped) — never the cross-pair "1,534 shipped / 12,642 placements." The
> 73-mnemonic fold-out (24 `.W18` wide branches + 6 virtualops + 43 no-body pseudo) is what
> separates 1,607 from 1,534.

The net consolidated grade: a **byte-grounded, execution-validated reimplementation reference at
≥97% coverage for v2–v4**, and a **header-OBSERVED + bounded-INFERRED reference for v5 and v1.**
`[HIGH/INFERRED over the named denominators]`

## A.3 What is rebuildable today — the closed subsystems

Concretely, with the deep pages a reimplementer can build each of these from the guide as a
specification, at the confidence stated:

- **The FLIX decoder** — reproduces the device `xtensa-elf-objdump` byte-for-byte: one
  `ncore2gp` Cairo config, 14 formats / 46 slots over 8 register files, the 7 length-class
  outcomes resolving to 4 byte-sizes `{2,3,8,16}`. The encoding cover is certified perfect and
  non-overlapping. `[HIGH/OBSERVED]`
- **A value simulator** — matching the shipped oracle bit-exact: 864 per-element leaves, each with
  a derived reference model, ~95% carrying a live differential certificate. This is the
  highest-leverage deliverable, because the binary itself validates it. `[HIGH/OBSERVED]`
- **The v2–v4 firmware mechanism** — the SEQ front-end, the POOL `0xF0` dispatch over the 8-byte
  `kernel_info_table`, the per-(gen × engine) embedded ELF32-Xtensa image set, selected by the
  `coretype` switch. v2–v4 images are present and decoded. `[HIGH/OBSERVED]`
- **The custom-op ABI & the runtime spine** — `Q7PtrType`, the `at::Tensor` marshalling chain,
  the `customop_*` device contract, the host `libnrt` load → install → execute → reap path, and
  the 8-core SPMD-by-PRID fan-out, field-exact for v2–v4. `[HIGH/OBSERVED]`
- **The collective opcode family** — the host trigger tier (`0xC7`/`0xC8`/`0xD9`), the one real
  device hop (`SB2SB 0xBF`), and the CCE in-transfer reduce in the SDMA fabric. The device half is
  re-OBSERVED in-checkout; the host *compose* pipeline is CARRIED (see Q11). `[HIGH device · CARRIED host]`
- **The gen-invariant core + scaling envelope** — one R(Q7) parameterized by a small scaling
  vector covering SUNDA through the header surface of MAVERICK. `[HIGH/OBSERVED for v2–v4; v5 envelope INFERRED]`

> **QUIRK — no signing key, ever.** The GPSIMD device load path contains *no* signature
> verification anywhere. Admission of a Q7 image is an **unkeyed** integrity hash + structural
> (ELF / reloc / core-count) + version (the ucode semver gate); the trust root is the **host OS /
> process boundary**, not a hardware root of trust, and the install seam `nrt_set_pool_eng_ucode`
> is a silent, unauthenticated override. A reimplementation that *adds* a signing requirement
> mis-models the security boundary — this is a deliberate single-tenant trusted-compiler design.
> `[HIGH/OBSERVED]`

## A.4 The one value caveat — the recipqli soft-float leg

There is exactly **one** value-leaf residual, and it is worth naming up front so the verdict is
honest: the **3 recipqli soft-float QLI refine leaves** (`recipqli_..._32f_32f` and an fp64 pair)
— **3 of 864 leaves, ~0.35%**. Everything *under* the wall is already proven: the 6-bit segment
index, the four QLI coefficient tables (read bit-exact and config-invariant, `base == ref`), the
two-substage split, and the device round-trip. Only the **value-producing soft-float FMA on top**
is gated — it routes through a host soft-float dispatch object (`call *0x38(%rax)`) that a bare
ctypes drive cannot populate; a fork-isolated probe SIGSEGVs on the NULL dispatch slot. This is a
**driver gap, not a knowledge gap** — the structure and tables are proven; only the live
end-to-end value of three leaves is deferred. It is registered as **Q1** below.
`[MED/OBSERVED — end-to-end value; the wall itself HIGH/OBSERVED, empirically proven]`

---

# Part B — The Open-Questions Map

The residual is a **small, fully-named, fully-categorized ledger of twelve open questions** (with
three carried secondaries, fourteen rows in total). The single honest headline governs the whole
map:

> **NONE of the open questions is a missing datapath body, a missing opcode decode, or a missing
> value semantics.** `[HIGH/INFERRED]` Every one is a true static-analysis *boundary* on this
> shipped corpus — a driver, a checkout, a license key, a runtime capture, or a follow-on pass
> away. The machine is recovered; what is walled off is a runtime input, an absent generation's
> image, an out-of-config core, or a license-gated observable.

Each question is tagged by **closability** — the thing that would cross it — per the
[walls taxonomy](../reference/confidence-model.md#3-what-a-wall-is): `closable-with-corpus`
(a fuller checkout), `closable-with-license` (a FlexNet key on the already-runnable simulator),
`closable-with-hardware / runtime-capture` (a captured payload or a device run),
`closable-with-static` (a follow-on RE pass on the binary in hand), or `fundamental` (the thing
asked for does not exist in this subsystem, or only a bound is recoverable). The owning page in the
final column is where each is developed in full.

## B.1 The closability map

| ID | Open question | Closability | Owning page |
|---|---|---|---|
| **Q1** | The 3 recipqli soft-float QLI refine leaves (the sole value-leaf wall, 3/864 ≈ 0.35%) — the value-producing FMA leg routes through a host soft-float dispatch | **hardware / runtime-capture** (device round-trip / FW-42 driver), or **license** (populated full ISS) | `validation/four-oracle-method.md` · `iss/iss-oracle-synthesis.md` |
| **Q2** | The 7 uncited `nrtucode` prelink/loader functions — present and partly decoded; only a *citation* residual (IDA name-coverage 98.5% → 100%) | **corpus** (in-corpus; a citation micro-pass) | `runtime/runtime-synthesis.md` |
| **Q3** | The DVE read-back ops `0x9b` / `0xe9` — output is engine *state* a prior producer left, outside the `f(A,B)→R` value-leaf model; no leaf to drive | **license** (the full-ISS cycle-model producer→state→read-back differential) | `firmware/kernels/dve-read-state.md` |
| **Q4** | The license-gated cycle / fault / observable-trace oracle — `libcas-core.so` loads, but instruction *retirement* hits `AUTH::check_iss_licenses` | **license** (a FlexNet node-locked key; the harness is already reconstructed) | `iss/iss-oracle-synthesis.md` |
| **Q5** | Stochastic rounding — there is **no** RNG-seed value leaf in the 864-leaf oracle; stochastic rounding lives *outside* the GPSIMD value datapath | **fundamental** (a scope boundary — the feature is not in this subsystem) | `validation/four-oracle-method.md` |
| **Q6** | The host-loaded PWP activation coefficients — the format is byte-exact; the per-function `{d0..d3,x0}` cubic *content* is host-loaded via the `0x23` table-load DMA | **hardware / runtime-capture** (a captured `0x23` payload), secondarily **corpus** (the host PWP generator) | `firmware/kernels/dve-read-state.md` · the ACT-engine table page |
| **Q7** | The NCFW dense case-body interiors — the scalar Xtensa-LX core's `op0=e/f`-dense ring/barrier bodies; its own (unshipped) Tensilica config never ships | **corpus** (NCFW's own LX disassembler config — a corpus item absent here) | `uarch/microarch-synthesis.md` (the NCFW LX section) |
| **Q8** | The MAVERICK (v5) NCFW / `Q7_CC_TOP` collective firmware image — file-absent; `libncfw_get_image` tops out at MARIANA_PLUS | **corpus** (a `libncfw` checkout shipping the coretype-37 image) | `generations/codename-generation-map.md` |
| **Q9** | The raw-kind vs ext-isa-id indirection — two core-kind gates appear to share one generation axis with different id values; mechanism OBSERVED, labeling open | **corpus** (in-corpus; a struct-lane cross-reference) | `generations/codename-generation-map.md` |
| **Q10** | The C16 FMA `_2` multi-output reassembly — the leaf executes live and its components are observed; only mapping the 5-output decomposition back to one bit-exact value is deferred | **static** (a follow-on drive pass on the shipped binary) | `validation/four-oracle-method.md` |
| **Q11** | The host collective-compose binaries (`libnccom` / host `libnrt`) — the device half is re-OBSERVED in-checkout; the host SELECT/COMPOSE/EMIT machinery is CARRIED from prior reports, not in this checkout | **corpus** (the host collective-compose libraries in-checkout) | `collectives/ops/architecture-synthesis.md` |
| **Q12a** | FW-42 transcendental **seed coefficient bytes** + exact Newton/QLI iteration counts — the `.rodata` seed *table* is validated truth; only the literal source coefficients in the out-of-carve FW-42 driver are CARRIED | **corpus** (the FW-42 firmware / a fuller carve) | `validation/four-oracle-method.md` |
| **Q12c** | The empty `MODULE_SCHEDULE` per-port reservation matrices — 1994/1994 empty in the shipped XML; the 1+1 co-issue ceiling is recovered, only the fine per-port reservation below it is absent | **fundamental** (the XML bodies are absent; the FLIX-slot model is the sound bound) | `uarch/microarch-synthesis.md` |
| **Q12e** | The scatter-add per-cycle RMW interleave under collision — the *value* is bit-exact (a commutative histogram sum); only the silicon's exact per-cycle ordering is untraced, and it is **value-immaterial** | **hardware** (a device run; observational, not corrective) | `dma/descriptor-model.md` |

**Roll-up by closability** (primary tag per row):

| Closability | Count | IDs |
|---|---|---|
| **closable-with-corpus** | **6** | Q2, Q7, Q8, Q9, Q11, Q12a (Q7 a special NCFW-config case) |
| **closable-with-license** | **2** (+1 secondary route on Q1) | Q3, Q4 |
| **closable-with-hardware / runtime-capture** | **2** (+1 value-immaterial Q12e) | Q1, Q6 |
| **closable-with-static** | **1** | Q10 |
| **fundamental** | **2** | Q5, Q12c |

The two `fundamental` rows are *not* unrecovered mechanisms: **Q5** is a scope boundary
(stochastic rounding is not a GPSIMD value-datapath feature at all) and **Q12c** is an
absent-XML-body residual whose practical bound — the FLIX-slot + per-format mul-capable-slot
model — is already sound. Every other row is a checkout, a key, a capture, or a follow-on pass
away. `[HIGH/INFERRED over the cited closers]`

## B.2 The standing walls a reader meets most often

Of the residual set, a handful are carried on the page wherever they apply, because a reimplementer
trips over them repeatedly. These are the *defining subset* of the closability map; the
[Confidence & Walls Model §4](../reference/confidence-model.md#4-the-named-walls) gives each its
full nature/provenance/closability.

### The v5 firmware-internal walls

MAVERICK (v5) is **header-OBSERVED only**, and three distinct interiors are walled — flag each on
every v5 claim, never fabricate across them:

- **`arch_id 36` — INFERRED.** The v5 firmware-internal `arch_id` is `36 (0x24)`, but it is *not*
  byte-read — it is `coretype − 1` extrapolated from the four observed generations. The OBSERVED
  anchor is `coretype 37` (`ct37`); the `arch_id` is the inference. There is no `cmp $0x24`
  anywhere and no v5 NCFW image to confirm it. `[ct37 HIGH/OBSERVED; arch_id 36 MED/INFERRED]`
  **closable-with-corpus.**
- **v5 Q7 geometry — INFERRED.** The v5 SoC-envelope scaling (the IRAM/DRAM geometry, the CSR
  bundle count, the cross-die transport) is the bounded extrapolation of the monotone v2–v4 scaling
  axes, not a read of v5 firmware. `[INFERRED]` **closable-with-corpus.**
- **v5 `Q7_CC_TOP` collective firmware — FILE-ABSENT.** The Maverick collective firmware image is
  not in this corpus — a genuine gap, not an unread region. The v5-specific dispatch bodies and the
  native-UCIe D2D transport live in firmware images this checkout does not carry; the v4/v5-*shared*
  kernels *are* decoded via the Mariana images. `[absence HIGH/OBSERVED]` **closable-with-corpus.**
- **v5-body decode (the FLIX-desync overlap).** Where a flat v5 NX region *is* present, the
  per-instruction bodies inside a FLIX-desynced span are read but tooling-bounded — table bases and
  string-anchored structures stay HIGH, the interior bindings sit at MED. `[wall HIGH/OBSERVED; bodies MED]`
  **closable-with-corpus** (a FLIX-aware config).

> **CORRECTION — the `coretype` stride is irregular; the `arch_id` relation is uniform.** Do not
> assert a flat `+8` `coretype` stride. The observed `coretype` set is `{6, 13, 21, 29, 37}` —
> that is **`+7` then `+8`, `+8`, `+8`** (6→13 is +7; 13→21→29→37 is +8 each). What *is* uniform is
> the `arch_id = coretype − 1` relation (`{0x05, 0x0c, 0x14, 0x1c, 0x24}`), and it is that `−1`
> relation — not any `coretype` stride — that extends the v5 `arch_id` to 36. A reimplementer who
> reads "+8 stride" off the `coretype` axis mis-derives the floor.

### FW-42 seed coefficients — CARRIED (the table is the truth)

The transcendental **seed lookup tables** (`RECIP_Data8` / `RSQRT_Data8` and the recipqli QLI
coefficient LUTs) are byte-read at their `nm` symbol addresses *and* execution-validated — the
reciprocal/rsqrt mantissa seeds reproduce `128/128` against the live simulator with
`FISS == SEM == TAB` agreement. **The shipped `.rodata` table is therefore the validated ground
truth**: a reimplementer copies those bytes and is correct by construction. What is *not*
recoverable is the literal **source coefficients** the firmware author started from — the
closed-form derivation, the `2^x` kernel on the NEXP0/NEXP01 primitives, and the exact
Newton/QLI iteration counts — which live in the out-of-carve FW-42 driver and are **CARRIED** from
the seed-validation report. **This wall is materially harmless:** the validated table is what a
reimplementer needs; the un-recovered item is provenance lineage, not behavior.
`[table HIGH/OBSERVED + validated; source-coefficient lineage MED/CARRIED]` **closable-with-corpus.**
Registered as **Q12a**.

### Empty `MODULE_SCHEDULE` matrices — fundamental (the 1+1 ceiling is the bound)

The pipeline-timing XML ships `1994/1994` `<MODULE_SCHEDULE>` reservation matrices that are
**structurally empty** in the dump. The class-level co-issue ceiling **is** recovered — the **1+1
FLIX co-issue bound** from the 1564-record `INSTR_SCHEDULE` table — so only the fine per-port
single-issue reservation *below* that ceiling is unrecoverable, because the matrix bodies are
simply not present in the shipped file. This is **fundamental** for the per-port claim (no read of
this XML produces bodies it does not contain), but the practical bound is **sound**: the FLIX-slot
+ per-format mul-capable-slot model is the correct substitute for a reimplementer's scheduler. Do
not fabricate a per-port matrix; use the 1+1 ceiling. `[empty HIGH/OBSERVED; per-port LOW]`
Registered as **Q12c**.

### The recipqli full-context leg — CARRIED end-to-end value

Q1's recipqli soft-float FMA is the sole value-leaf wall (A.4). Its **full-context leg** — the
end-to-end value through a fully-populated ISS soft-float dispatch — is `closable-with-license`
(route a: the licensed full ISS executing the leaf in cycle/turbo mode) or
`closable-with-hardware` (route b: the FW-42 full-iteration driver / a device run that exercises
the dispatch object). The structure, tables, and `base == ref` identity are all proven; only the
soft-float FMA on top is gated. `[MED/OBSERVED]`

### The SortMerge phantom — a wall that *isn't*

A companion "merge two sorted subtensors" instruction, **SortMerge**, is *named* in the firmware
only as a dead comment — `// SortMerge wip 0x97` — on the adjacent `0x98 = TENSOR_SCALAR_SELECT`
line, with opcode slot `0x97` commented out and never shipped (`0x97` is actually the update-mode
field `UPDATE_MODE_SEM_SUB_REG_COMPLETE`, not an opcode). There is **no SortMerge struct, no
opcode body, and no debug string** anywhere in the corpus; plain `SORT` (`0x96`) is real and
decoded. This is documented here precisely so no downstream page invents a SortMerge opcode from
the leftover comment. The honest statement is **"named-but-never-shipped."** `[HIGH/OBSERVED — the absence is a positive finding]`
Not a behavioral wall — an anti-fabrication marker.

## B.3 What the map is *not*

It bears repeating, because it is the meta-finding of the whole effort: **the open-questions map
contains no missing decode, no missing datapath, and no missing value semantics.** Across ~2.09M
differential comparisons, every apparent value mismatch root-caused to the reference model or a
tool — never the firmware. The residual is honestly-flagged boundaries:

- a **driver** away (Q1 recipqli soft-float dispatch),
- a **checkout** away (Q2, Q7, Q8, Q9, Q11, Q12a — corpus items absent here),
- a **license key** away (Q3, Q4 — present-but-gated cycle/fault observables),
- a **runtime capture** away (Q6 host-loaded PWP content; Q12e value-immaterial ordering),
- a **follow-on static pass** away (Q10 the C16 reassembly),
- or **fundamentally out of scope** (Q5 stochastic rounding; Q12c per-port reservation).

That is the precise boundary between what this reference proves today and what one key, one
checkout, or one capture would unlock tomorrow.

---

## The exhaustive registers

This page is the navigable orientation map. The two appendix registers are the exhaustive,
fully-cross-referenced versions — read them when you need the complete *why-unreachable* and
*what-would-close-it* for every row, not just the defining subset:

- **The Open-Questions Register** — `appendix/open-questions-register.md` — every residual unknown,
  fully partitioned by closability, each with its complete boundary citation and closing artifact.
- **The Coverage Ledger** — `appendix/coverage-ledger.md` — the per-axis, per-lane coverage
  accounting behind the five headline fractions on this page.

(Both are full appendix pages in the sidebar; this map carries the defining subset they expand.)

---

## See also

- [The Confidence & Walls Model](../reference/confidence-model.md) — the `[CONF/PROV]` tag system
  every figure here uses, and the seven named walls (§4) this map's standing-walls subset draws
  from.
- [The Corpus, Tiers & Binary Inventory](../reference/corpus-inventory.md) — what is and is not in
  the checkout; the file-absent walls (Q8, Q11) are corpus facts catalogued there.
- [What GPSIMD Is — the one-screen map](what-gpsimd-is.md) — the verdict table this page develops
  into the named-wall ledger.
- [The Seven Faces of the One Machine](seven-faces.md) — *Face 7 — the Evidence* is the
  execution-validated basis this verdict cashes.
- [Keystone Facts Reimplementers Get Wrong](keystone-facts.md) — the traps (the recipqli leg, the
  `coretype` stride, the SortMerge phantom, the empty `MODULE_SCHEDULE` matrices) that this map's
  walls also guard against.
- [The Gen-Invariance Thesis](gen-invariance.md) — the R(Q7) invariant/scaling/absent partition
  that backs the v2–v4 byte-grounded vs v5 bounded-INFERRED scope.
