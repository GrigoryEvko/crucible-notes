# The Confidence & Walls Model

Every factual claim in this guide carries a two-part tag, and every honestly-unanswerable
question is tracked as a named **wall**. This page is the normative definition of that
tagging system: the rest of the wiki refers back here for what its tags *mean*. If you read
a sentence elsewhere ending in `[HIGH/OBSERVED]` or `[MED/INFERRED]`, or a paragraph that
says "this is a wall — closable-with-license", the precise meaning is fixed here and only
here.

The system exists because the entire reference is reconstructed from **static analysis of
shipped binaries** — ELF objects, XML/`.params`/golden-DB config files, TIE databases,
per-generation firmware images, and the libcas/libfiss instruction-set simulator driven
*in-process as a live oracle*. No design document, no symbol-rich debug build, and no
running silicon were available. That methodology produces facts of sharply different
strength: a byte read straight out of `.rodata`, a value computed by calling the shipped
simulator on a known input, a structure reasoned out from a stride pattern, and a fact
carried forward from an earlier report all deserve different trust. The tag makes that
difference explicit on every line so a reimplementer knows exactly how much weight to put on
each claim, and the wall taxonomy makes explicit the small set of questions that *cannot* be
answered from this corpus, why, and what would change that.

This page tags its own claims with the system it defines. `[HIGH/OBSERVED]` here means the
statement is read directly out of the corpus (a report verdict re-grepped, a binary
re-checked); `[HIGH/INFERRED]` means reasoned over those reads.

---

## 1. The two axes

A tag is **confidence × provenance**: one of `HIGH` / `MED` / `LOW`, then one of `OBSERVED`
/ `INFERRED` / `CARRIED`. The axes are orthogonal — provenance says *where the claim came
from*, confidence says *how much you should trust it*. A claim can be `OBSERVED` and only
`MED` (read from a region the disassembler partially desyncs); it can be `INFERRED` and
`HIGH` (a deduction so tightly bounded it is effectively certain). Always read both halves.

### 1.1 Provenance — where the fact came from

| Provenance | Criterion |
|---|---|
| **OBSERVED** | Read directly from a shipped artifact: a byte at a named symbol/offset, a string in a section, a config field in an XML/`.params`/golden-DB file, an instruction at a resolved address, or a value **computed by executing the shipped simulator** on a known input. The binary is the witness. |
| **INFERRED** | Reasoned *over* one or more OBSERVED facts. No single artifact states it; it follows from observed evidence by a stated deduction (a stride pattern, a structural mirror, a cross-reference, an absence-implies argument). The reasoning chain is named. |
| **CARRIED** | OBSERVED (or INFERRED) in a *cited* prior analysis report and reused here at that report's original confidence, **without re-reading the artifact this pass**. Carrying is legitimate provenance, but it is one inheritance step removed from the binary: the claim is only as good as the cited source, and a stale-copy hazard exists if that source was later corrected. |

The crucial distinction is OBSERVED-by-execution. Because the shipped value simulator
(`libfiss-base.so`, 864 `module__xdref_` value leaves) is **callable in-process via ctypes
with no license**, this reference treats "I ran the binary on input *A* and it returned *R*"
as OBSERVED, not inferred — the binary itself is the arbiter of its own value semantics.
This is what lets value coverage reach 100%.

### 1.2 Confidence — how much to trust it

| Confidence | Criterion |
|---|---|
| **HIGH** | Byte-exact and either directly read, multiply-corroborated, or proven-by-execution. A reimplementer can encode this as a hard requirement. The fact sits *above* any disassembler desync line, has independent witnesses, or has a differential-execution certificate. |
| **MED** | Sound but single-witness, partially tooling-bounded, or resting on one structural inference. Use it, but flag it; a contradicting later observation is plausible. Typical cause: a value read inside a FLIX-desynced region, a single-source carve with no sibling cross-check, or a one-step structural deduction. |
| **LOW** | Best-available but materially uncertain: a value behind a tooling wall, an absent-data substitute, or a label whose binding is not byte-proven. Do **not** hard-code a LOW fact; treat it as a hypothesis to confirm dynamically. |

### 1.3 The decision rule

When you author a claim, pick provenance first (where did this come from — a read, a
deduction, or a citation?), then confidence (how strong is the evidence behind it?). When you
*read* a claim, the provenance tells you what kind of follow-up could ever change it (a
re-read can only fix an OBSERVED; an INFERRED needs a better deduction or a new observation;
a CARRIED can be upgraded by re-reading the artifact in-checkout), and the confidence tells
you whether to build on it now.

---

## 2. The six useful combinations, each with a worked example

Of the nine cells, six carry the analytical load. Each example below is drawn from the real
corpus; the example's *own* tag is stated, and the reasoning that earns that tag is spelled
out so the criteria are unambiguous.

### HIGH / OBSERVED — "I read it, and it is byte-exact"

> The reset vector of the Q7 POOL core is the bundle `j 0x200` (bytes `06 7f 00`) on
> Cayman / Mariana / Mariana+, and `j 0x1e4` (`06 78 00`) on Maverick. `[HIGH/OBSERVED]`

Read straight out of each generation's SRAM image at the reset address with the native
disassembler. Three independent images agree on the first three generations; the fourth
differs by exactly `-0x1c`. Nothing is deduced — the bytes are on the page. This is the
default tag for an encoded instruction, a `.rodata` byte, a config field, or a string.

### HIGH / OBSERVED (by execution) — "I ran the binary and it returned this"

> The default rounding mode for the fp add/sub value leaves is **round-toward-zero**
> (truncation), not round-to-nearest-even: 2976/2976 results match an exact-`Fraction` RZ
> model and 0/2976 match RNE. `[HIGH/OBSERVED]`

Not read from any field — *computed* by calling the shipped `libfiss-base.so` add/sub leaves
in-process on 2976 inputs and comparing against two reference models. The binary is the
arbiter. This is the strongest provenance available short of silicon, and it is what
"PROVEN-BY-EXECUTION" means throughout the guide. ~95% of value-bearing leaves carry such a
differential certificate; zero firmware value bugs were found across ~2.09M comparisons.

### HIGH / INFERRED — "no single byte says it, but the deduction is airtight"

> The five silicon generations are one Vision-Q7 reimplementation parameterized by scaling
> axes; one physical core model covers Sunda/Cayman/Mariana/Mariana+/Maverick. `[HIGH/INFERRED]`

No artifact states "one core covers five generations." It is deduced from a large body of
OBSERVED facts — shared reset bodies, a single registered `ncore2gp` core config, identical
ABI-relevant register blocks, gen-keyed dispatch ladders — partitioned into invariant /
scaling / absent. The deduction is tight enough to be HIGH, but it remains INFERRED because
it is a synthesis, not a read. Flagging it INFERRED tells a reader the *form* of the claim
(a thesis over evidence) even though its strength is high.

### MED / OBSERVED — "read, but inside a region the tooling only partly resolves"

> Inside the NCFW (scalar Xtensa-LX management core) the `op0=e/f`-dense case-body interiors
> — the ring `0x3c..` / hierarchical-barrier `0x3e..` step schedules — are read, but the
> `e/f` leader ops cannot be named with certainty. `[MED/OBSERVED]`

The bytes *are* observed, but the shipped disassembler is configured for the Vision-Q7 FLIX
core, not the NCFW LX core; it greedily mis-frames the `e/f`-dense bytes as Vision bundles.
A 3-byte resync heuristic recovers the spine to HIGH but leaves the dense interiors at MED —
observed, not byte-resolved. OBSERVED earns the provenance (the bytes were read); the tooling
limit caps the confidence at MED.

### MED / INFERRED — "a sound one-step structural deduction, single-witness"

> Two core-kind gates in `libnrtucode_internal.so` appear to share one
> {Sunda,Cayman,Mariana,Mariana+,Maverick} enumeration with *different* id values — a raw
> core-kind `{2,9,21,29,37}` (a `_bittest64`) vs an ext-ISA-id `{6,13,21,29,37}` (a 32-case
> switch). `[MED/INFERRED]`

Both gate *bodies* are OBSERVED (the bittest mask and the switch are decoded). That they are
two encodings of the same generation axis, indexed differently, is a one-step inference over
those two reads, with no third witness to confirm the labeling. Sound, usable, but flagged —
exactly the MED/INFERRED contract.

### MED / CARRIED — "true in a cited report; reused, not re-read here"

> The host collective-compose pipeline (`libnccom` `findPath`/`EdgeRemoteMLA`; the
> SELECT/COMPOSE/EMIT machinery) behaves as described — but those binaries are **not in this
> checkout**; the facts are reused from prior reports over the host libraries. `[MED/CARRIED]`

The device half of the collective is re-OBSERVED in-checkout; the host half is CARRIED at its
source report's confidence because `libnccom.so` / `libnrt.so` 2.31.24.0 are absent here. A
reader is warned: this is one inheritance step from the binary, and re-OBSERVing it requires a
different checkout. (HIGH/CARRIED is common too — a re-verified prior verdict carried at its
proven strength; MED/CARRIED is the cautionary case where the carry crosses a checkout
boundary.)

> **Why never LOW/OBSERVED-as-fact:** if something is genuinely read byte-exact it is at least
> MED. LOW attaches to facts behind a wall — a value the tooling cannot resolve, an
> absent-data substitute, or an unproven label. A LOW claim is a hypothesis, not a
> requirement; do not encode it.

---

## 3. What a "wall" is

A **wall** is a genuine boundary of static analysis on *this* shipped corpus: a specific
question whose answer **cannot be produced by any amount of further reading or reasoning over
the binaries in hand.** Crossing it requires something the corpus does not contain — a
dynamic run on real hardware, a captured runtime payload, a different/fuller checkout, or a
license key that unlocks an already-present-but-gated capability.

A wall is **not** an unfinished analysis. "We have not yet decoded function X" is a task, not
a wall, if X is present and decodable. A wall is reached only when the *artifact that would
answer the question is provably not in the corpus*, or the answer is *gated behind a
runtime/license event no static read can trigger*. Every wall in this guide is named, its
exact nature stated, and its **closability** classified:

| Closability | Meaning — what would cross it |
|---|---|
| **closable-with-corpus** | A different or fuller checkout that *contains* the missing artifact (a later firmware image, an absent host library, an unshipped disassembler config). The corpus boundary, not a knowledge boundary. |
| **closable-with-license** | The capability is *present and runnable* in the shipped simulator but execution halts at a FlexNet license check (`AUTH::check_iss_licenses`). A node-locked key crosses it; nothing else needs to change. |
| **closable-with-hardware / runtime-capture** | A value that exists only at runtime — host-loaded per-model table content, a soft-float dispatch object populated by a device round-trip, a per-cycle ordering. A captured payload or a device run crosses it. |
| **closable-with-static** | A follow-on RE pass on the *binary already in hand* — the artifact is present and drivable; only the analysis work is unfinished. (Strictly a soft wall, listed for completeness.) |
| **fundamental** | No artifact in *any* static corpus of this subsystem can answer it, because the thing being asked for **does not exist in this subsystem** (a scope boundary) or the data is structurally absent and only a bound is recoverable. |

The honest headline of the whole wave: **no wall is a missing datapath body, a missing
opcode decode, or a missing value semantics.** `[HIGH/INFERRED]` Every named wall below is a
driver / checkout / key / capture / follow-on boundary — the *machine* is recovered; what is
walled off is a runtime input, an absent generation's image, an out-of-config core, or a
license-gated observable.

---

## 4. The named walls

Each row gives the wall's **exact nature**, its **provenance** (how we know the wall is real),
and its **closability**.

### 4.1 `arch_id 36` — INFERRED (Maverick v5 identity)

**Nature.** The v5 (MAVERICK) generation's firmware-internal `arch_id` is **36 (`0x24`)** —
but this number is *inferred from a stride*, not byte-read. The OBSERVED facts are: the
Maverick **coretype is 37** (read from the `movabs $0x2020202000` bitmask that sets bits
13/21/29/37, and from 62 Maverick getter symbols); and across the first four generations
`arch_id == coretype − 1`. Extending that stride gives `arch_id = 37 − 1 = 36`. There is **no
NCFW v5 image** to confirm it: `libncfw_get_image` has exactly four codename legs
(`0x05/0x0c/0x14/0x1c`) and `arch_id > 0x1C → return 2` routes `0x24` to the unsupported path
— there is no `cmp $0x24` anywhere.

**Provenance.** Coretype 37: `[HIGH/OBSERVED]`. The `arch_id = 36` value: `[MED/INFERRED]` —
bounded tightly (the +1 stride is invariant across four observed generations) but
unconfirmed for want of a v5 image. The audit disposition is *split*: upgrade coretype to
OBSERVED, keep `arch_id 36` flagged INFERRED.

**Closability — closable-with-corpus.** A fuller `libncfw` checkout that carries the
coretype-37 image would byte-read the v5 `arch_id` and either confirm 36 or correct it. **Do
not fabricate a v5 part-binding** on the strength of the stride.

### 4.2 `ct37` — OBSERVED (the Maverick coretype anchor)

**Nature.** In contrast to its `arch_id`, the Maverick **coretype 37** is directly OBSERVED:
the gen-selection bitmask `0x2020202000` sets bit 37, the ext-ISA dispatch switch has a
`case 37 → maverick_libs` arm, and 62 Maverick-keyed getter symbols ship in
`libnrtucode_internal.so`. The v5 *device-side bytes are present in the customop twin* even
though the v5 *collective firmware image* is not.

**Provenance.** `[HIGH/OBSERVED]` — multiple independent witnesses (bitmask, switch arm,
symbol census), re-verified against the binary.

**Closability — not a wall (anchor).** `ct37` is the OBSERVED counterpart that bounds §4.1's
inference. It is listed here precisely to mark the boundary line: *coretype crossed it
(OBSERVED); arch_id did not (INFERRED)*. Treat any claim that conflates the two as a defect.

### 4.3 FW-42 seed coefficient bytes — CARRIED (table is validated truth; source coefficients not recoverable)

**Nature.** The transcendental seed lookup tables (`RECIP_Data8` / `RSQRT_Data8` and the
recipqli QLI coefficient LUTs) are **byte-read at their `nm` symbol addresses and
execution-validated**: the fp16/fp32 reciprocal/rsqrt mantissa seeds reproduce `128/128`
against the live simulator, with `FISS == SEM == TAB` agreement. **The shipped `.rodata`
table is therefore the validated ground truth** — a reimplementer copies those bytes and is
correct by construction. What is *not* recoverable is the **literal source coefficients** the
firmware author started from: the closed-form derivation, the `2^x` kernel built on top of
the NEXP0/NEXP01 primitives, and the exact Newton/QLI iteration counts live in the FW-42
firmware driver, which is *out of the customop carve*. Those upstream numbers are CARRIED
from the seed-validation report, not byte-read here.

**Provenance.** The `.rodata` table content and its `128/128` validation: `[HIGH/OBSERVED]`
(read + proven-by-execution). The literal source-coefficient lineage and iteration counts:
`[MED/CARRIED]` — narrowed and table-anchored, but the originating bytes are in an
out-of-corpus driver.

**Closability — closable-with-corpus.** The FW-42 firmware / a fuller carve would expose the
source coefficients. **This wall is materially harmless:** the *validated table* is the truth
a reimplementer needs; the un-recovered item is provenance lineage, not behavior.

### 4.4 FLIX-desync device interiors — the corpus-wide MED ceiling

**Nature.** The shipped device disassembler config has `IsaMaxInstructionSize = 32` and
desyncs on the 512-bit FLIX/VLIW bundle stream: once mis-aligned, the byte cursor produces
plausible-but-wrong per-instruction decodes until it resynchronizes. **Everything *above* the
desync line is HIGH** — table bases, `kernel_info` entries, dispatch-table addresses, reset
vectors, string-anchored structures, opcode-enum membership — because those are read at known
addresses by cursor reads that do not depend on linear sweep. The per-*instruction* body
bindings *inside* a desynced region are not byte-resolved.

**Provenance.** `[HIGH/OBSERVED]` that the wall exists and where it sits (it has *caught real
errors*: two cases where a desync literal was mis-read as a branch target were later
corrected, which is the flag doing its job). The interior bodies: `[MED/OBSERVED]` — read but
tooling-bounded.

**Closability — closable-with-corpus** (a FLIX-aware disassembler config). Note that the
*reference methodology* mitigates this without crossing it: encoding is closed independently
on the certified-perfect ISA cover, and value semantics are closed by *executing* the
simulator — neither path walks the desynced linear stream. The desync limits *narrative
body-reading*, not the encoding or value closures. The FLIX-decoding methodology page details
the resync discipline.

### 4.5 The SortMerge phantom — a wall that *isn't*, named to forestall fabrication

**Nature.** A companion "merge two sorted subtensors" instruction, **SortMerge**, is *named*
in the firmware only as a dead comment — `// "SortMerge wip 0x97"` — with opcode slot `0x97`
**commented out** and never shipped (`0x98` was reassigned to `TENSOR_SCALAR_SELECT`). There
is **no SortMerge struct, no opcode body, and no debug string** anywhere in the corpus. Sort
itself (`0x96`) is real and decoded; cross-partition merge is host-side / future work.

**Provenance.** `[HIGH/OBSERVED]` — the absence is a positive, byte-exact finding: the only
trace of SortMerge is the disabled comment.

**Closability — not a behavioral wall; a *fabrication* wall.** It is documented here so no
downstream page invents a SortMerge opcode from the leftover comment. The honest statement is
"named-but-never-shipped." A future firmware revision that ships `0x97` would *add* the
behavior; nothing in *this* corpus is missing.

### 4.6 Empty `MODULE_SCHEDULE` reservation matrices — fundamental (bound is the substitute)

**Nature.** The pipeline-timing XML ships `1994/1994` `<MODULE_SCHEDULE>` reservation
matrices that are **structurally empty** in the dump. The class-level co-issue ceiling **is**
recovered (the 1+1 FLIX co-issue bound, from the 1564-record `INSTR_SCHEDULE` table); only the
*fine per-port single-issue reservation below that ceiling* is unrecoverable, because the
matrix bodies simply are not present in the shipped file.

**Provenance.** `[HIGH/OBSERVED]` that the matrices are empty and that the 1+1 class ceiling
is recovered. The per-port reservation: `[LOW]` — absent data.

**Closability — fundamental** (for the per-port claim). No read of *this* XML can produce
bodies it does not contain. This is honest absence, not unfinished work — and the practical
bound is *sound*: the FLIX-slot + per-format mul-capable-slot model is the correct substitute
for a reimplementer's scheduler. The Maverick FLIX-desync interiors (§4.4) overlap this as a
secondary, image-gated sub-case.

### 4.7 v5 `Q7_CC_TOP` collective firmware — FILE-ABSENT (closable-with-corpus)

**Nature.** The Maverick (v5) `Q7_CC_TOP` collective firmware image is **not in this
corpus** — file-absent, a genuine gap rather than an unread region. Maverick ships no NCFW
image and no `Q7_CC_TOP` firmware in the customop artifacts; the v5 D2D transport (a native
UCIe re-IP whose PHY is not named in the customop artifacts) and the v5-specific dispatch
sites live in firmware images this checkout does not carry. The v4/v5-*shared* kernels **are**
decoded via the Mariana images; only the v5-*specific* bodies are absent.

**Provenance.** `[HIGH/OBSERVED]` on the absence (the `get_image` ladder tops at Mariana+;
exactly four codename `ctx_log` symbols, zero `maverick`/`v5`). The interiors that *would* be
in the missing image: not observable.

**Closability — closable-with-corpus.** A fuller/later `libncfw` checkout that ships the
coretype-37 image crosses it. The absence is itself a definitive OBSERVED fact; the closure is
a different corpus. **Do not fabricate a v5 collective firmware.**

### 4.8 Wall summary

| Wall | Exact nature | Provenance of the wall | Closability |
|---|---|---|---|
| `arch_id 36` (§4.1) | v5 `arch_id` inferred from `coretype−1` stride, no v5 image to confirm | coretype OBSERVED; value INFERRED | closable-with-corpus |
| `ct37` (§4.2) | v5 coretype directly read (bitmask + switch + 62 getters) | `[HIGH/OBSERVED]` | (anchor — not a wall) |
| FW-42 seeds (§4.3) | `.rodata` table is validated truth; literal source coefficients out-of-carve | table OBSERVED+validated; lineage CARRIED | closable-with-corpus |
| FLIX-desync interiors (§4.4) | per-instruction bodies inside desynced FLIX regions | wall HIGH/OBSERVED; bodies MED | closable-with-corpus (FLIX-aware config) |
| SortMerge phantom (§4.5) | named-but-never-shipped (dead `0x97` comment) | `[HIGH/OBSERVED]` (absence is positive) | not behavioral — anti-fabrication |
| empty `MODULE_SCHEDULE` (§4.6) | per-port reservation matrices empty in shipped XML | empty OBSERVED; per-port LOW | fundamental (1+1 ceiling is the bound) |
| v5 `Q7_CC_TOP` (§4.7) | Maverick collective firmware image file-absent | absence HIGH/OBSERVED | closable-with-corpus |

For the complete, categorized residual ledger — twelve open questions partitioned by
closability, each with its full *why-unreachable* and *what-would-close-it* — see the
**Open-Questions Register** (`appendix/open-questions-register.md`) and the **Coverage
Ledger** (`appendix/coverage-ledger.md`). The named walls above are the *defining* subset a
reader meets most often; the appendix register is exhaustive.

---

## 5. The generation-grounding policy

Confidence is not uniform across the five silicon generations, and the wiki applies a fixed
policy so a reader always knows which footing a per-generation claim stands on. `[HIGH/INFERRED]`

**v2 – v4 (Sunda / Cayman / Mariana / Mariana+): byte-grounded.** Their firmware images,
config, and ABI are present and read directly; their value semantics are closed by execution
against the live oracle. Per-generation claims for these are `OBSERVED` (often
proven-by-execution) and default to HIGH. A reimplementer can target v2–v4 as a hard
specification.

**v5 (Maverick) and the MAVERICK header surface: header-OBSERVED-only, interiors INFERRED.**
What *is* OBSERVED for v5 is the header/identity surface and the customop-twin device bytes:
coretype 37 (§4.2), the 62 Maverick getters, the gen-selection bitmask arm, the
clang-15/15.05 firmware `.comment`, and the empirical *absence* of a v5 NCFW image. Everything
*behind* that surface — the v5 `arch_id` (§4.1), the v5 collective firmware (§4.7), the
v5-specific dispatch interiors — is INFERRED or file-absent and **flagged on every use**.
The policy: a v5 claim is publishable **only** as header-OBSERVED + bounded-INFERRED, with the
interior explicitly marked. Never fabricate a v5 silicon part-binding, a v5 collective
firmware, or a v5 `arch_id` byte that was not read.

**v1 (Tonga): pre-unified outlier.** Characterized as an engine-scoped outlier to the
one-core-covers-all thesis; treated like v5 — header-OBSERVED where present, interiors flagged.

The net effect, restated as the wiki's standing claim: this is a *byte-grounded,
execution-validated reimplementation reference for v2–v4*, and a *header-OBSERVED +
bounded-INFERRED reference for v5 and v1*. The generations page carries the formal
invariant / scaling / absent partition that backs this policy.

---

## 6. How to trust each tag (the reader's contract)

The tag is an instruction to the reimplementer. Read it as follows:

- **`HIGH/OBSERVED`** (incl. proven-by-execution) — **encode it as a hard requirement.** It is
  byte-read or computed by the binary itself. If a divergence appears during your
  bring-up, suspect your *reference model or tool*, not this fact — that is precisely the
  meta-finding of ~2.09M differential comparisons: every apparent mismatch root-caused to the
  model or the tool, never the firmware.
- **`HIGH/INFERRED`** — **encode it, but know it is a synthesis.** The deduction is tight, but
  it is a thesis over evidence. If you can later *observe* it directly, do — an OBSERVED
  confirmation is strictly stronger.
- **`MED/*`** — **use it, flag it, plan to confirm it.** Single-witness, tooling-bounded, or
  one-step-inferred. Build on it provisionally; a later observation may refine it. The
  Correction Ledger exists because MED claims are exactly where later passes have refined
  earlier ones (a desync literal corrected, a stride boundary tightened).
- **`*/CARRIED`** — **trust it at its cited source's strength, and check that source was not
  later corrected.** A carry is one inheritance step from the binary. HIGH/CARRIED is a
  re-verified verdict; MED/CARRIED usually crosses a checkout boundary and warrants
  re-OBSERVing if you can.
- **`LOW/*`** — **do not hard-code.** It is a hypothesis behind a wall, an absent-data
  substitute, or an unproven label. Confirm it dynamically before you depend on it.

### 6.1 The differential ISS/VAL lane: how OBSERVED becomes proven-by-execution

The single mechanism that upgrades a static read into the strongest possible static fact is
the **differential execution lane**, and it is worth understanding because it is what makes
the value claims HIGH. The shipped instruction-set simulator splits into two libraries:

- **`libfiss-base.so`** — the **value** oracle. Its 864 `module__xdref_` leaves are the
  per-element value functions of every GPSIMD value opcode. They are **callable in-process via
  ctypes with no license.** This is the lane that runs.
- **`libcas-core.so`** — the **cycle** oracle (latency, stall, issue, fault). Instruction
  *retirement* calls `AUTH::check_iss_licenses`; without a FlexNet key it halts at "Unable to
  get license."

The upgrade procedure: take a value opcode, derive its semantics statically (decode → leaf →
reference model), then **call the shipped leaf live on a sweep of inputs and diff the result
against the reference model.** A leaf that matches bit-exact across the sweep is no longer
merely *decoded* — it is **proven-by-execution**, the binary itself acting as arbiter. Across
18 op families and ~2.09M comparisons this drove execution-validation to ~95% of
value-bearing leaves with zero firmware value bugs, and pinned precise IEEE-754-edge behaviors
(RZ-default rounding, NaN-asymmetric max/min, the quiet/signaling compare split, three-way
pack saturation) that a naive model gets wrong.

What the *value* lane cannot upgrade — and where the license wall (§3) sits — is anything that
needs **retirement, cycles, faults, or observable trace**: the cycle counts, the 124-slot
fault machine, single-stepping, and the DVE engine-state read-back ops (`0x9b`/`0xe9`) that
read hidden per-lane flops a *prior* producer left. Those are runnable in the *same*
simulator but gated behind the license — `closable-with-license`, not a knowledge gap. The
value lane is free and runs; the cycle lane is present and gated. That split is the precise
boundary between what this reference proves by execution today and what a single key would
unlock tomorrow.

---

## 7. Applying the system when you author or read a page

1. **Every factual claim carries a `[CONF/PROV]` tag.** Pick provenance by *where it came
   from* (a read incl. execution → OBSERVED; a deduction → INFERRED; a citation → CARRIED),
   then confidence by *evidence strength*.
2. **Name the source.** OBSERVED → the symbol/offset/section/config-field, or the leaf you
   executed. INFERRED → the OBSERVED facts and the deduction. CARRIED → the cited report.
3. **If it cannot be answered from the corpus, it is a wall** — state its exact nature, its
   provenance, and its closability (corpus / license / hardware / static / fundamental). Do
   not let a wall masquerade as a fact; do not let an unfinished task masquerade as a wall.
4. **Flag every v5/Maverick and v1/Tonga interior** per §5. Header-OBSERVED is publishable;
   interiors are INFERRED or absent and must say so.
5. **Never fabricate across a wall** — no invented v5 part-binding, no SortMerge opcode body,
   no `MODULE_SCHEDULE` per-port matrix, no FW-42 source coefficient that was not read. The
   validated *table* or the sound *bound* is the honest deliverable.

---

## Cross-references

- **[Methodology — How This Was Reverse-Engineered](methodology.md)** — the static-only
  toolchain (native disassembler, in-process ctypes oracle, config-file grounding) that
  produces the OBSERVED facts this page tags.
- **[FLIX Bundle-Decoding Methodology](flix-decoding.md)** — the desync mechanism behind
  §4.4 and the resync discipline that lifts the spine to HIGH.
- **[The Corpus, Tiers & Binary Inventory](corpus-inventory.md)** — what is and is not in the
  checkout; the file-absent walls (§4.7) are corpus facts.
- **The Open-Questions Register** (`appendix/open-questions-register.md`) and **The Coverage
  Ledger** (`appendix/coverage-ledger.md`) — the exhaustive, closability-partitioned residual
  ledger that this page's named walls are the defining subset of.
