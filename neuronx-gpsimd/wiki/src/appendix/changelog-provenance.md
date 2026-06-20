# Provenance & Methodology Changelog

> *This is the **how-this-was-produced-and-how-to-audit-it** companion to
> [Methodology — How This Was Reverse-Engineered](../reference/methodology.md). The
> methodology page is the normative **technique** reference (the eight recovery
> techniques, the value oracle, the four verification gotchas, one worked
> end-to-end audit); this page is its **provenance** record — the production-wave
> history, the audit discipline restated as a contract, the concrete re-verify
> recipe, and the lawful-interop attestation. Where the two overlap, the
> methodology page is authoritative; this page points to it rather than
> re-deriving it.*

Every artifact pin on this page is relative to `neuronx-gpsimd/` and ties to
`aws-neuronx-gpsimd-customop-lib_0.21.2.0` + `aws-neuronx-gpsimd-tools_0.21.0.0-bc9b5fad5`.
The SHA-pinned witness list is [The Corpus, Tiers & Binary Inventory](../reference/corpus-inventory.md);
the toolchain pins are [Toolchain Inventory & Versions](../reference/toolchain-versions.md);
the confidence vocabulary is [The Confidence & Walls Model](../reference/confidence-model.md).

## Why this page exists

A reference reconstructed entirely from shipped binaries is only as trustworthy as
its **audit trail**. A reader who wants to bet a reimplementation on a claim here
needs three things this page supplies that a narrative subsystem page does not:

1. **How it was produced** — the production waves, in order, and how each later
   wave *corrected* the one before it (so a reader knows the text is the
   reconciled result of adversarial passes, not a single confident draft).
2. **How to re-verify any line** — a concrete, command-level recipe that takes an
   arbitrary claim back to the bytes (`nm` / `objdump` / `xtensa-elf-objdump` /
   `ctypes` / `jq`), with the four file-format gotchas that silently corrupt the
   answer if missed.
3. **Under what legal posture** — the clean-room, shipped-binary-only,
   DMCA §1201(f) interoperability footing.

The defining property of this corpus, restated once here because it shapes
everything below: **several witnesses are *executable oracles*, not just readable
data.** `libfiss-base.so` answers "what value does this opcode compute?" by
*running* — `nm -D libfiss-base.so | rg -c 'module__xdref_'` reports **864** value
leaves, each a pure C-ABI function callable in-process via `ctypes` with no license
key. That is why this reference can promote a static decode to
`OBSERVED-by-execution`. `[HIGH/OBSERVED]`

---

## 1. The production waves — a changelog of the RE process itself

> **NOTE — these are the *authoring methodology's own phases*, not external
> reports.** The wave names (SX / GX / W / DX) label the stages of this
> reverse-engineering effort: a deep survey, a refinement, a consolidation, and an
> execution-validation. They are the changelog of *how the binary analysis
> progressed*, in the same sense a compiler's pass pipeline is its own changelog.
> Every fact each wave produced still terminates at a shipped byte; the wave only
> says *when in the process* that byte was first read, re-read, or executed.

The reference was not written in one pass. It was produced in four waves, each with
a distinct job, and — critically — **each later wave was allowed to overturn the
earlier one** when a direct re-read of the bytes disagreed. That adversarial
discipline is the reason the published text is reconciled rather than first-draft;
the surviving record of those overturns is [The Full Do-Not-Repeat / Correction
Ledger](do-not-repeat-full-ledger.md).

### Wave SX — the deep survey (breadth-first census)

**Job.** Enumerate the corpus and lay down the first census of *everything*: carve
the 29 device ELF32-Xtensa images out of the `nrtucode` containers, mine the host
libraries' symbol and string pools, run the IDA export over the TIER-1 host
binaries, and pull the per-generation arch-ISA header trees, the `instruction_mapping.json`
struct↔opcode tables, the cleartext core config, and the TIE database. This is the
**breadth** wave — it establishes *what exists and roughly what it is*, and stamps a
first confidence tag on each finding.

**What it contributed.** The witness inventory; the ISA-source triangulation surface
(`libisa-core.so` tables + `core-isa.h` + the TIE DB + `xtensa-modules.c`); the
first opcode roster; the firmware-image geometry (`.text @ 0x01000000` IRAM /
`.rodata + kernel_info_table @ 0x02000000` DRAM); the structural skeleton of the host
firmware-loader from the IDA sidecars.

**Its characteristic error class.** Breadth-first census produces *plausible but
single-witness* claims — exactly the `MED` tier the confidence model is built to
flag. A first reading mis-frames the NCFW scalar core's `op0=e/f` bytes as Vision
FLIX bundles (the spurious "~26–28% FLIX" artifact); a naive `^S:` string diff shows
handlers spuriously added/removed across generations; a count grepped from the
flattened decompile inflates 2–12× over the true symbol table. SX surfaces these;
it does not yet settle them.

### Wave GX — the refinement (depth-first re-read + reconciliation)

**Job.** Take the SX census and **re-read the bytes** for every claim that
mattered, replacing single-witness assertions with multi-witness or
execution-backed ones, and reconciling intra-survey divergences. This is the
**depth** wave — it converts `INFERRED`/`MED` into `OBSERVED`/`HIGH` where the bytes
allow, and writes down a `CORRECTION` where they don't.

**What it contributed (the SX corrections it landed).** The NCFW core is
re-identified as scalar **Xtensa-LX**, not FLIX — the "~26–28% FLIX" artifact is
traced to running the only-shipped `ncore2gp` config against an LX image, and the
correct scalar-LX length rule (`op0 ∈ {e,f}` ⇒ 3-byte, resync at `retw.n`) is
substituted. The cross-generation diff is re-grounded on `funcVA` / `kernel_info_table`
membership instead of string presence. The four ISA sources are reconciled to a
single roster (**1534 opcodes / 14 formats / 46 slots**, with the +73 TIE-DB
mnemonics resolved as the pre-fold authoring superset). The arch-ISA layouts are
promoted from `INFERRED` to `OBSERVED-by-compilation` via `offsetof`/`sizeof`. This
is where the reference's confidence *earns* its tags.

### Wave W — the consolidation (capstone synthesis)

**Job.** Fold the depth-first findings into **per-lane capstone syntheses** — one
owning page per reverse-engineering axis (ISA, ISS, VAL, HW, FW, IMG, GEN, ABI, RT,
DMA, CCL, NCFW, NEFF, CC, STRUCT, CSR, ADDR, INT, SEC) — and reconcile *across*
capstones so the same number does not appear two ways on two pages. This is the
**coherence** wave: it makes the reference one consistent document and produces the
[coverage ledger](coverage-ledger.md)'s per-lane accounting.

**What it contributed.** The nineteen-lane coverage partition; the cross-page
reconciliations (each Part's punch-list of two-pages-disagree divergences, resolved
to one truth and recorded); the one-core-covers-five-generations thesis stated as a
`HIGH/INFERRED` synthesis over OBSERVED facts; the generation-grounding policy
(v2–v4 byte-grounded; v5/v1 header-OBSERVED + bounded-INFERRED). W is where a count
like the RTTI census or the 864-leaf tally is *standardized* to its `nm`-grounded
value everywhere it appears.

### Wave DX — execution-validation (the oracle drive)

**Job.** Stop *reading* the value semantics and **run them.** Drive the shipped ISS
in-process as a differential oracle: for each value opcode, derive its semantics
statically, then call the live `libfiss-base.so` leaf on an input sweep and diff
against an independent reference model. A bit-exact match across the sweep upgrades
the claim from *decoded* to **proven-by-execution** — the binary itself is the
arbiter. This is the **certification** wave.

**What it contributed (the strongest provenance in the reference).** The value
oracle drive across **18 op families** and **~2.09M in-process differential
comparisons**, finding **0 firmware value bugs** — every apparent mismatch
root-caused to the reference model or the harness, never the firmware. It pinned
the IEEE-754-edge behaviors a naive model gets wrong (round-toward-zero default
rounding, NaN-asymmetric max/min, the quiet/signaling compare split, three-way pack
saturation) and drove execution-validation to **~95%** of value-bearing leaves. DX
is the wave that lets the confidence model treat "I ran the binary on input *A* and
it returned *R*" as `OBSERVED`. `[HIGH/OBSERVED — the 0 and the 2.09M; MED — the ~95% fraction]`

### Wave timeline at a glance

| Wave | Mode | Contributed | Corrected the prior wave by… |
|---|---|---|---|
| **SX** | breadth-first census | witness inventory, first rosters, image geometry, IDA skeleton | — (establishes the baseline) |
| **GX** | depth-first re-read | multi-witness promotion, ISA roster reconcile, `offsetof`/`sizeof` proofs | re-reading SX's single-witness `MED` claims; NCFW-is-LX; diff-on-funcVA |
| **W** | capstone synthesis | nineteen-lane coverage, cross-page reconcile, generation policy | standardizing GX's counts/labels across pages; killing two-pages-disagree |
| **DX** | execution-validation | ~2.09M differential comparisons, RZ/NaN/compare/pack edges, 0 firmware bugs | promoting W's `OBSERVED-by-naming` to `OBSERVED-by-execution` |

> **QUIRK — later waves *win*, and the loss is recorded, not hidden.** When DX's
> live drive contradicts a GX-era inferred value, or a W reconcile overturns an
> SX-era count, the superseded claim is written into the
> [do-not-repeat ledger](do-not-repeat-full-ledger.md) with the byte evidence that
> settles it — never quietly conformed to. The ledger's existence *is* the proof the
> wave discipline works: it is the empirical record of `MED` claims that later passes
> refined.

---

## 2. The methodology spine — the eight techniques, restated as an audit index

The eight recovery techniques are defined in full, each with its mechanism, its
concrete artifact, and its confidence ceiling, in [Methodology
§2](../reference/methodology.md#2-the-eight-recovery-techniques). This page restates
them only as a **one-line audit index** — what each recovers and the single command
or file that reproduces it — so a reader auditing a claim can jump straight to the
technique that backs it. Do not treat this table as the definition; the methodology
page is.

| # | Technique | Recovers | Reproduce it with |
|---|---|---|---|
| (a) | **ELF / firmware carving** | the 29 embedded device ELF32-Xtensa images (`e_machine=94`) | parse each `*_EXTISA_<n>_SO_get` accessor for `(data_va, size)`; slice `.rodata` (VMA==offset); `sha256` dedup proves the carve |
| (b) | **Native `ncore2gp` device disasm** | Vision-Q7 FLIX/VLIW + IVP vector mnemonics off device `.text` | `XTENSA_CORE=ncore2gp xtensa-elf-objdump -d <image>` (the only registered core) |
| (c) | **Arch-ISA header compile-verify** | operand-struct field offsets + sizes, `OBSERVED-by-compilation` | `gcc` the shipped `aws_neuron_isa_tpb_*.h`; read `offsetof`/`sizeof`; confirm the header's own `ISA_STATIC_ASSERT` |
| (d) | **`kernel_info_table` / DEBUG self-name anchoring** | which image *ships* a handler (vs an ISA-surface opcode name) | read the 8-byte `[BE key][LE funcVA]` entries (one `R_XTENSA_RELATIVE` per entry); cross the `"S:<Name>"`/`"P%i:"` strings |
| (e) | **Cross-generation byte-diffing** | per-`(engine,variant,region)` invariance + the localized gen delta | `sha256` each carved image; diff; ground on `funcVA`/`kernel_info` membership, never `^S:` strings |
| (f) | **Cleartext-config cross-validation** | the ISA roster from four independent sources converging | reconcile `libisa-core.so` tables + `core-isa.h`/`ncore2gp-params` + the TIE DB + `xtensa-modules.c` → **1534/14/46** |
| (g) | **Confidence discipline + premise-correction** | a per-claim trust tag + an explicit `CORRECTION` when bytes disagree | apply [the Confidence & Walls Model](../reference/confidence-model.md); record the overturn in the [ledger](do-not-repeat-full-ledger.md) |
| (h) | **IDA v3 sidecar JSON + `context/*.md`** | host-loader struct layouts, callgraph, enum values, jump tables | `jq` the `ida/<dir>/*_{functions,structures,enums,strings,xrefs}.json`; counts still grounded in `nm`, not the sidecar |

Two cross-cutting tools sit on top of this spine and are the reason it reaches
reimplementation grade:

- **The FLIX bundle-decode tool.** Technique (b)'s native objdump desyncs on dense
  512-bit FLIX/VLIW spans (it has `IsaMaxInstructionSize = 32` and a linear sweep).
  The byte-exact format/length/slot decode that *lifts* that desync — the
  `format_decoder` mask-ladder, the 256-cell `length_table`, the 46 `Slot_*_get`
  gather thunks — is read directly out of `libisa-core.so` and documented end to
  end in [FLIX Bundle-Decoding Methodology](../reference/flix-decoding.md). A
  from-scratch port reproduces the device `xtensa-elf-objdump` framing with **zero**
  disagreements over the 167 bundles of Cayman `EXTISA_0`. `[HIGH/OBSERVED]`

- **The libfiss live-drive value oracle.** The single most powerful step is not
  static: `libfiss-base.so`'s 864 `module__xdref_*` leaves are pure C-ABI functions
  driven standalone by `dlopen`-ing the real shipped `.so` under `ctypes` and
  calling a leaf by its exported symbol — the binary computes the result. This is
  the executable arbiter of the value semantics and the mechanism behind the DX
  wave's ~2.09M comparisons. The worked four-step audit (`nm` → `objdump` → live
  `ctypes` drive) is [Methodology §4](../reference/methodology.md#4-a-worked-verification-end-to-end);
  the differential-lane detail is [Confidence Model §6.1](../reference/confidence-model.md#61-the-differential-iss-val-lane-how-observed-becomes-proven-by-execution).
  `[HIGH/OBSERVED]`

- **The struct compile-verify.** Technique (c) plus the `static_assert`/`sizeof`
  cross-checks promote a layout from a stride-inferred guess to a compiler-proven
  fact: the shipped CAYMAN header carries
  `ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_CTRL_MV_STRUCT) == 64, ...)`, and
  compiling it and reading `sizeof` confirms the assert holds. `[HIGH/OBSERVED]`

---

## 3. The confidence & audit discipline — the reader's contract

### 3.1 The tagging contract (restated, normative source elsewhere)

Every factual claim in this reference carries a two-part tag — **confidence ×
provenance** — defined normatively in [The Confidence & Walls
Model](../reference/confidence-model.md) and applied unchanged here:

- **Confidence** ∈ `{HIGH, MED, LOW}` — how much to trust it: `HIGH` = byte-exact
  and either directly read, multiply-corroborated, or proven-by-execution (encode it
  as a hard requirement); `MED` = sound but single-witness or tooling-bounded (use,
  but flag); `LOW` = behind a wall or unproven-label (a hypothesis — do not
  hard-code).
- **Provenance** ∈ `{OBSERVED, INFERRED, CARRIED}` — where it came from: `OBSERVED` =
  read from a shipped byte/string/config field, *or computed by executing the
  shipped simulator*; `INFERRED` = reasoned over OBSERVED facts by a named
  deduction; `CARRIED` = re-used at a cited analysis's confidence without re-reading
  the artifact this pass (one inheritance step from the binary).

The axes are orthogonal. The contract for the reimplementer: `HIGH/OBSERVED` →
encode it; `HIGH/INFERRED` → encode it but know it is a synthesis; `MED/*` → use,
flag, plan to confirm; `*/CARRIED` → trust at the source's strength and check the
source was not later corrected; `LOW/*` → do not hard-code.

### 3.2 The wall discipline — every wall is a true static-analysis boundary

A **wall** is a question whose answer **cannot be produced by any amount of further
reading or reasoning over the binaries in hand** — not an unfinished task. "We have
not yet decoded function X" is a *task* if X is present and decodable; it becomes a
*wall* only when the artifact that would answer it is provably not in the corpus, or
the answer is gated behind a runtime/license event no static read can trigger. Each
named wall states its exact nature, its provenance, and its **closability**
(corpus / license / hardware / static / fundamental).

> **The headline of the whole effort, restated verbatim from the confidence model:**
> **no wall is a missing datapath body, a missing opcode decode, or a missing value
> semantics.** `[HIGH/INFERRED]` Every named wall is a driver / checkout / key /
> capture / follow-on boundary — the *machine* is recovered; what is walled off is a
> runtime input (a host-loaded per-model table), an absent generation's image
> (the v5 `Q7_CC_TOP` collective firmware, file-absent), an out-of-config core
> (the NCFW scalar-LX, no shipped LX config), or a license-gated observable (cycle
> counts behind `AUTH::check_iss_licenses`). The full, closability-partitioned
> residual list is [The Open-Questions Register](open-questions-register.md); its
> defining subset is in [Confidence Model §4](../reference/confidence-model.md#4-the-named-walls).

### 3.3 The audit recipe — take any claim back to the bytes

This is the concrete procedure to re-verify an arbitrary claim in this wiki against
the shipped binary. The cited claim carries a tag and (for an `OBSERVED` one) a
symbol/offset/leaf; the recipe re-grounds it.

**Step 0 — reach the artifact.** Everything under `extracted/` is `.gitignored`.
`fd`/`rg` skip it by default. Use `--no-ignore` or an absolute path, and locate the
*one* file with `fd` before reading it — never run a folder-wide `rg`/`nm` over the
multi-GB `extracted/`/`ida/` trees.

```sh
FISS=$(fd -t f 'libfiss-base.so' extracted --no-ignore | head -1)
```

**Step 1 — re-ground a count or a symbol address in `nm` (never a decompile grep).**
A "symbol hit count" grepped from the flattened decompile inflates **2–12×** versus
the binary's true symbol table.

```sh
nm -D --defined-only "$FISS" | rg -c 'module__xdref_'        # -> 864 (the value-leaf count)
nm -D --defined-only "$FISS" | rg 'fs0ltu|xdref_ltu_1_8_8'   # -> the audit-anchor addresses
```

**Step 2 — confirm the body with the right disassembler.** Host x86 libraries use
stock `objdump`/`nm -DC`/`c++filt`/`readelf`. Device Xtensa images use the shipped
`xtensa-elf-objdump` with `XTENSA_CORE=ncore2gp` (the *only* registered core — bare
invocation errors with "no Xtensa core registered as the default"). Route NCFW
scalar-LX code through the scalar-LX length rule, **not** `ncore2gp`.

```sh
objdump -d --start-address=0x8328d0 --stop-address=0x8328ee "$FISS"          # host x86 wrapper body
XTENSA_CORE=ncore2gp xtensa-elf-objdump -d <device-image>                    # device FLIX
```

**Step 3 — for a value claim, drive the leaf live and diff.** A leaf that matches a
reference model bit-exact across an input sweep is `OBSERVED-by-execution`.

```python
import ctypes
lib = ctypes.CDLL(FISS, mode=ctypes.RTLD_GLOBAL)
SIG = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
leaf = lib.module__xdref_ltu_1_8_8; leaf.restype = ctypes.c_int; leaf.argtypes = SIG
out = ctypes.c_uint(0)
for A, B in [(0,0),(0,1),(1,0),(127,128),(128,127),(5,250),(250,5)]:
    leaf(None, A, B, ctypes.byref(out))
    assert out.value == (1 if (A & 0xff) < (B & 0xff) else 0)   # *result = (unsigned)(A<B)
```

**Step 4 — for a sidecar/structure claim, `jq` the IDA JSON, then cross-check `nm`.**
The sidecar offsets/edges are binary-derived (read off the ELF by the disassembler),
so a `structures.json` member offset is as citeable as an `objdump` line — but any
*count* still re-grounds in `nm`, not in the sidecar.

```sh
jq '.[] | select(.name=="<struct>") | .members[] | {name,offset,size}' ida/<dir>/*_structures.json
```

> **GOTCHA — the four file-format traps, each of which caused a real regression.**
> Codified in [Methodology §5](../reference/methodology.md#5-the-verification-gotchas-each-caused-a-real-regression);
> reproduced here as the audit checklist because each silently corrupts an offset or
> a count:
> 1. **`.data` is NOT VMA==file-offset.** `.text`/`.rodata` are equal, so `xxd` by
>    vaddr works there. `.data` carries a **binary-specific** delta — `0x200000` for
>    the `ncore2gp` config libs (`readelf -SW libfiss-base.so` shows `.data Addr
>    0xc8eb68 Off 0xa8eb68`), `0x3000` for the `nrtucode` pair, `0x1000` for the
>    ncfw/extisa pair. Read the real `Addr`/`Off` columns before addressing a
>    `.data`-resident struct. (Over-generalising the libtpu wave's `0x400000` *is*
>    the regression.)
> 2. **A vtable slot is measured from `vptr = _ZTV + 0x10`, not the `_ZTV` symbol.**
>    A slot index in `call *0xN(%rax)` is `N` relative to `_ZTV + 0x10`; `reloc −
>    symbol` overcounts by `0x10`. (Moot on the RTTI-empty device firmware; applies
>    only to the host RTTI surface.)
> 3. **Count via `nm`, never the decompile.** Restated above; it is the single most
>    common count error.
> 4. **`extracted/` is `.gitignored`.** Restated as Step 0; it bites every lane.

---

## 4. Lawful-interoperability attestation

This reference is a **clean-room interoperability reverse-engineering** product. The
posture, stated factually:

- **Shipped-binary analysis only.** Every fact derives from static analysis of
  shipped, redistributable artifacts — ELF objects, static archives, the per-generation
  arch-ISA headers shipped *in cleartext* in the customop-lib, the `ncore2gp` config
  DLLs and TIE database, the JSON/pickle config, and the device firmware images
  carved from the `nrtucode` containers — plus the lawful **in-process execution of
  shipped bytes** (the `libfiss-base.so` value oracle driven via `ctypes`). Recovered
  symbols, strings, and headers **are** binary-derived facts and are cited as such.
- **No vendor source tree.** No proprietary source repository, design document,
  internal specification, debug-symbol-rich build, runtime silicon trace, or
  debugger session against a real NeuronCore was referenced at any point. The device
  firmware is RTTI-empty (`-fno-rtti`, the Cadence/Tensilica default) and ships with
  stripped or empty symbol tables; the recovery rests on the eight techniques in §2,
  not on any privileged artifact.
- **Interoperability purpose, §1201(f).** The analysis is performed for the sole
  purpose of understanding and documenting the GPSIMD / Vision-Q7 "Cairo" interface
  so an independent, interoperable implementation can be written — squarely within
  the **DMCA 17 U.S.C. §1201(f)** reverse-engineering-for-interoperability provision.
  Where the shipped instruction-set simulator's *timing/retirement* path halts at a
  FlexNet license check (`AUTH::check_iss_licenses`), that gate is **respected, not
  circumvented** — the cycle-lane facts behind it are marked walled
  (`closable-with-license`), never extracted by defeating the check. Only the
  free, license-unencumbered *value* lane is executed.

*Provenance: lawful interoperability reverse engineering under DMCA 17 U.S.C.
§1201(f). Every fact in this reference derives from shipped-binary / shipped-header /
shipped-config analysis and the lawful in-process execution of shipped, unlicensed
value leaves; no vendor source tree was referenced and no license gate was defeated.*

---

## 5. Cross-references

- **[Methodology — How This Was Reverse-Engineered](../reference/methodology.md)** —
  the normative technique reference this changelog is the provenance companion to:
  the eight techniques in full, the value oracle, the worked end-to-end audit, the
  four gotchas.
- **[The Confidence & Walls Model](../reference/confidence-model.md)** — the
  normative definition of the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tag and the
  wall taxonomy that §3 applies.
- **[The Corpus, Tiers & Binary Inventory](../reference/corpus-inventory.md)** — the
  SHA-pinned witness catalogue every audit-recipe path resolves against.
- **[Toolchain Inventory & Versions](../reference/toolchain-versions.md)** — the
  `xtensa-elf-objdump` / `ncore2gp` and build-toolchain pins Step 2 of the recipe
  depends on.
- **[FLIX Bundle-Decoding Methodology](../reference/flix-decoding.md)** — the
  byte-exact format/length/slot decode that lifts technique (b)'s desync wall.
- **[The Coverage Ledger](coverage-ledger.md)** — the per-lane accounting the W wave
  produced; where the 864/864 value cover and the ~2.09M / 0-firmware-bug DX figures
  roll up.
- **[The Full Do-Not-Repeat / Correction Ledger](do-not-repeat-full-ledger.md)** —
  the surviving record of every claim a later wave overturned; the empirical proof
  the wave discipline works.
- **[Bibliography of Source Binaries](bibliography-source-binaries.md)** — the cited
  source-binary list backing the attestation in §4.
- **[The Open-Questions Register](open-questions-register.md)** — the exhaustive,
  closability-partitioned wall list §3.2 summarizes.
