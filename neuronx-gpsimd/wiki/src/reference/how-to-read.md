# How to Read This Guide

This is a reimplementation reference, not a tutorial. The intended reader is a
senior C++ engineer — comfortable with ELF, LLVM, and DSP/VLIW microarchitecture
— who wants to **rebuild a Vision-Q7-compatible GPSIMD engine** (or a toolchain,
runtime, or simulator that targets one). Every page is written so that, after
reading it, you could write the C++ that reproduces the described behaviour.

Everything here was recovered by **static analysis of shipped, redistributable
binaries, headers, config files, and TIE artifacts** — 13 host x86-64 libraries,
29 embedded ELF32-Xtensa device blobs (`e_machine = 94`), and the cleartext
per-generation arch-ISA headers / core config / TIE database. No runtime trace,
no debugger session, no vendor source. That single fact shapes every convention
below: because the device firmware is symbol-stripped and compiled `-fno-rtti`
(verified: `_ZTS`/`_ZTI`/`_ZTV`/`__cxxabiv1` byte-count = 0 across all 29 device
ELFs), nothing in this guide rests on a recovered class hierarchy. It rests on
anchors you can re-derive yourself — section names, dispatch tables, enum bytes,
config tokens — and every claim is tagged with how strongly the bytes support it.

Read this page once. The four things it gives you — the **16-Part map**, the
**three reading paths**, the **page anatomy**, and the **in-text conventions** —
are the operating manual for every leaf page that follows (Part 0's
reference-apparatus pages plus the 401 subsystem pages of Parts 1–16).

---

## 1. The 16-Part structure

The body is organised in **journey order**: a reimplementer reads top to bottom
to rebuild the engine, each Part assuming the ones before it. The full tree lives
in [`SUMMARY.md`](../SUMMARY.md); the shape is:

| Part | Title | What it lets you build |
|------|-------|------------------------|
| **0** | Reference Apparatus | *(this Part)* — how to read, trust, and re-verify everything else |
| **1** | Orientation | The one-screen mental model: what GPSIMD is, the seven "faces" of the one core, the facts people get wrong |
| **2** | Q7 Core & ISA Foundations | The Cairo / `ncore2gp` core identity, the FLIX encoding, the eight register files, the libisa decode model |
| **3** | Per-Instruction ISA Reference | All 1534 Vision-Q7 opcodes in 30 batches + the formal operational semantics |
| **4** | Microarchitecture & Timing | Pipeline, co-issue matrix, register-file ports, boot/reset, the scalar-LX management core |
| **5** | Device Firmware & Kernel Catalog | The SEQ front-end, POOL dispatch, DGE, and the **140-opcode** kernel catalog |
| **6** | Firmware Images & Generations | The per-(generation × engine) device images and how they differ across Sunda → Maverick |
| **7** | Custom-Op ABI | The `at::Tensor` chain, the `customop_*` marshalling, the build/link/strip flow |
| **8** | Host Runtime | `libnrt` / `nrtucode` — bring-up, dispatch, the 8-core SPMD model, the prelinker |
| **9** | DMA / Descriptors / Memory | Descriptor model, gather/scatter, RDMA, SBUF/PSUM banks, the `al_udma` engine |
| **10** | Collectives & NCFW | The collective opcodes and the NX-CoreFirmware management core that orchestrates them |
| **11** | NEFF Container Format | The on-disk container, carved byte-by-byte, and its relationship to ELF |
| **12** | Compiler Seam | The `neuronx-cc` → opcode lowering, the BIR instruction roster, the MX path |
| **13** | Control Plane | CSR / address map / interrupt / security — the SoC plumbing around the core |
| **14** | ISS as Executable Oracle | `libcas` / `libfiss` — the cycle-accurate and fast simulators, used as a value oracle |
| **15** | Validation & Verification | The four-oracle bit-exact differential method and per-family pass/fail ledger |
| **16** | Appendices | Struct census, the master opcode↔kernel↔engine matrix, the coverage ledger, the checklist |

Two structural rules are worth internalising before you start:

- **`SUMMARY.md` is the contract.** Parts 1–16 are committed as a roadmap; a line
  becomes a live link the moment its page ships. If a path you expect is still in
  the roadmap comment block, the page is authored but not yet wired — check
  `SUMMARY.md` for the canonical path.
- **The core is gen-invariant; the firmware is not.** For the four shipped
  generations (Sunda / Cayman / Mariana / Mariana+) there is exactly one Xtensa
  core config — one `core-isa.h`, one `ConfigName Xm_ncore2gp`, one `uarchName
  "Cairo"`. Parts 2–4 describe **that one core**. What changes per generation is
  the SoC, the NCFW firmware, and the per-engine *TPB* tensor-ISA headers — which
  is why Parts 5, 6, and 13 are generation-aware and Parts 2–4 are not. Maverick
  (v5) is the one exception and is flagged everywhere it matters (see Part 6 and
  the [Codename Cross-Walk](codename-crosswalk.md)).

---

## 2. Three reading paths

### Path A — the reimplementer (front to back)

Read in journey order. Part 0 → 1 → 2 → … → 16. Each Part hands the next its
vocabulary: Part 2's FLIX encoding is what Part 3's per-instruction pages decode;
Part 5's kernel catalog is what Part 7's ABI dispatches into; Part 12's compiler
seam is what produces the NEFF that Part 11 describes. If you read nothing else
first, read **[Part 1 · What GPSIMD Is](../orientation/what-gpsimd-is.md)** and
**[Part 1 · Keystone Facts Reimplementers Get Wrong](../orientation/keystone-facts.md)**
— they inoculate you against the mistakes the [Correction Ledger](correction-ledger.md)
catalogues.

### Path B — "I just need opcode X" (lookup)

You have an opcode byte (or a kernel name, or a `NEURON_ISA_TPB_*` enum) and want
its behaviour. Start at one of the two master indexes, then jump:

1. **[Appendix · Opcode ↔ Kernel ↔ Engine Matrix](../appendix/opcode-kernel-engine-matrix.md)**
   — the one-row-per-opcode table: byte → kernel page → engine → struct.
2. From the matrix row, jump to the **Part 5 kernel page** (e.g. opcode `0x68`
   `GATHER` → [`firmware/kernels/indirection-gather.md`](../firmware/kernels/indirection-gather.md))
   for the device handler, and to the matching **Part 3 batch page**
   (e.g. [`isa/ref/b19-scatter-gather.md`](../isa/ref/b19-scatter-gather.md)) for the
   underlying Vision-Q7 instructions.
3. For the *semantics* of the underlying instruction, the **Part 14 ISS pages**
   are the executable oracle — they are the ground truth a differential test runs
   against.
4. For *encoding*, the **[Master ISA Encoding Appendix](../appendix/isa-encoding-appendix.md)**
   and **[Part 2 · The FLIX VLIW Encoding](../isa/core/flix-encoding.md)**.

The opcode catalog ledger — **[`firmware/kernels/opcode-catalog-ledger.md`](../firmware/kernels/opcode-catalog-ledger.md)**
— is the authoritative list of the 140 real hardware opcodes (the 172-value enum
union minus 31 compiler-pseudo opcodes and 1 invalid).

### Path C — per-generation (silicon-targeted)

You are targeting one specific silicon and want only what differs. Anchor on the
**[Codename ↔ Generation Cross-Walk](codename-crosswalk.md)** to fix your
coordinates, then read the per-generation slice:

- the generation's image pages in **Part 6** (e.g. for Trn2 →
  [`images/cayman-pool.md`](../images/cayman-pool.md) and siblings),
- the **[Master Per-Generation Capability Matrix](../generations/master-capability-matrix.md)**
  and the **[Cross-Gen Opcode-Table Diff](../generations/cross-gen-opcode-diff.md)**,
- and, for the control plane, the gen-scaled CSR/address pages in **Part 13**.

The product coordinates, pinned to binary evidence, are: **Sunda** = NC-v2 =
Trn1 / Inf2; **Cayman** = NC-v3 = Trn2; **Mariana** = NC-v4 = Trn3; **Mariana+**
= NC-v4+ = Trn3-pre; **Maverick** = NC-v5 (firmware-internal-only, unshipped on
the public runtime path). Do not invent a Trn4 for Maverick — that binding is
open. The cross-walk page carries the full coretype/arch_id stride table and the
caveats.

---

## 3. Page anatomy

Most subsystem pages follow the same skeleton, so you can skim predictably:

1. **Opening prose** — what the subsystem does, why it exists, where it sits in
   the pipeline.
2. **Key-facts table** — the addresses, sizes, struct/enum names, and knobs a
   reimplementer copies first.
3. **Algorithm** — mixed prose + C-style pseudocode for the core logic.
4. **Data-structure layouts** — byte offsets, field types, sizes (the field-exact
   struct census lives in Part 16).
5. **Configuration / knobs** — defaults and effects.
6. **Diagnostic strings** — what the binary prints, and when (these are recovery
   anchors as much as documentation).
7. **Function / address map** — identity table for the key routines.
8. **Cross-references** — to the related pages.

Not every page needs all eight; a 30-instruction batch page is mostly (2)+(3)+(4),
an image page is mostly (2)+(7)+(8). The constant is that **prose carries the
*why* and *reasoning*; tables and code carry the *what*.**

---

## 4. In-text conventions

These conventions are used on **every** page. Learn them once.

### 4.1 Confidence tags — `<level> / <evidence>`

Every non-obvious claim carries two orthogonal tags, written together as
`HIGH/OBSERVED`, `MED/INFERRED`, and so on.

**Confidence level** — how strongly the bytes support the claim:

| Level | Meaning |
|-------|---------|
| **HIGH** | Multi-source or byte-exact. Re-derivable from the shipped artifacts. |
| **MED** | Strong inference from solid evidence, but not byte-pinned end to end. |
| **LOW** | Plausible, explicitly flagged. Do not build on it without re-verifying. |

**Evidence kind** — *where* the claim comes from:

| Kind | Meaning |
|------|---------|
| **OBSERVED** | Read directly from a shipped artifact: a byte, a section, a struct compiled with `offsetof`/`sizeof`, a config token. |
| **INFERRED** | Reasoned over OBSERVED facts. |
| **CARRIED** | Re-used from an upstream finding **at its original confidence** — never inflated. |

The cardinal discipline: **a synthesis never raises a claim's confidence.** A
`MED/INFERRED` fact stays `MED/INFERRED` when a later page cites it. The full
rationale, the worked examples, and the "what counts as OBSERVED" rules are in
**[The Confidence & Walls Model](confidence-model.md)**.

### 4.2 Callout markers

Inline markers flag the four things you most need to not miss:

- **QUIRK** — a genuine hardware/firmware oddity you must reproduce, even if it
  looks like a bug (e.g. the Q7_POOL reset vector shifting from `j 0x200` to
  `j 0x1e4` on Maverick).
- **GOTCHA** — a recovery or reimplementation trap that has bitten before
  (e.g. *objdump's hex column is byte-reversed versus the memory stream — feed a
  FLIX decoder the real little-endian bytes*).
- **NOTE** — context that aids understanding but isn't a hazard.
- **CORRECTION** — a place where an earlier or naive reading was wrong, stating the
  **right** value and pointing at the [Correction Ledger](correction-ledger.md).
  The ledger holds 67 such corrections (12 of them HIGH-severity); the wiki always
  states the corrected value and never the superseded one.

### 4.3 How pseudocode names real symbols

C/pseudocode blocks describe **real recovered routines and structures**, not
illustrative inventions. The naming rules:

- A function the firmware actually contains is named by its recovered identity —
  a demangled `.xt.prop` section name (the device images ship per-function
  property tables whose *names are the C++ mangled symbols*, e.g.
  `.xt.prop._Z22cross_lane_reduce_implb` → `cross_lane_reduce_impl`), a
  `kernel_info_table` self-name, or a DEBUG-build `"S:<Name>"` / `"P%i:"` trace
  string. Where only an address is known, it appears as a virtual address
  (`sub_<vaddr>` or `func@0x…`) and is so marked.
- Structs and enums use their **header names verbatim** —
  `NEURON_ISA_TPB_DTYPE`, `S2D2_TS_AS_STRUCT`, `kernel_info_table` — because these
  ship as cleartext source and were compile-verified, not guessed.
- Pseudocode for a *desynced* span (one objdump rendered as `.byte`) is labelled as
  reconstructed and tagged at the confidence the FLIX-decode achieved — never
  presented as a clean disassembly it isn't.

### 4.4 How claims anchor

Every factual claim is anchored to something a reader can independently check.
Anchors come in these flavours, and you will see them inline throughout:

| Anchor | Example |
|--------|---------|
| **Address / offset** | `.text @ 0x01000000` (IRAM); `.rodata + kernel_info_table @ 0x02000000` (DRAM) |
| **Symbol / section** | `.xt.prop._Z24get_sequence_bounds_implj20NEURON_ISA_TPB_DTYPE` |
| **Enum value** | `NEURON_ISA_TPB_DTYPE` — 16 codes, byte-identical across Sunda and Cayman |
| **Opcode byte** | `0x72` = `COPY_PREDICATED`, DVE-native (the base of the predicated-op family) |
| **String / config token** | `XCHAL_VISION_TYPE = 7`, `vq7_isa = 1` in `core-isa.h` |

### 4.5 A worked example — what one anchored, tagged claim looks like

Here is a real claim from the kernel catalog, written the way pages write them:

> The `kernel_info_table` is a `PROGBITS` section present in every device image:
> an array of 8-byte entries, each `[ BE opcode-key : u32 ][ LE funcVA : u32 ]`.
> The `funcVA` is the **only** relocated field — one `R_XTENSA_RELATIVE` per
> entry, stride 8 — which independently proves the 8-byte stride, the funcVA
> position, and the entry count (**17** for Cayman). Every funcVA validates onto a
> real function prologue; several land directly on a named `.xt.prop` function.
> `[HIGH/OBSERVED]`

Read it as: the structural facts (stride, field layout, count) are **OBSERVED**
from the relocation table and **HIGH** because the relocation count *is* the entry
count — a self-checking anchor. Contrast a claim like *"Maverick targets HW rev
NX1.1.4"*, which is tagged `[INFERRED]` because Maverick's core config does not
ship — its `e_flags 0x300` merely *suggests* core-family continuity. The tag is
not decoration; it tells you exactly how far you can lean on the sentence.

---

## 5. The companion Part-0 pages

This page is the index to Part 0. The other front-matter pages each expand one
dimension of "how to trust and re-verify the body," and **every** subsystem page
links back here for its provenance framing:

- **[The Confidence & Walls Model](confidence-model.md)** — the `HIGH/MED/LOW ×
  OBSERVED/INFERRED/CARRIED` discipline in full, plus the *walls*: the true
  static-analysis boundaries (the FLIX-desync ceiling, the FLAT-NX interior, the
  out-of-corpus and host-supplied content) that no page may silently cross.
- **[Methodology — How This Was Reverse-Engineered](methodology.md)** — the eight
  recovery techniques: getter-driven ELF carving, the native `ncore2gp`
  disassembler, arch-ISA header compile-verify (`offsetof`/`sizeof` proofs),
  `kernel_info_table` / self-name anchoring, cross-generation byte-diffing,
  four-source ISA triangulation, the confidence-and-correction culture, and the
  codified corpus gotchas.
- **[FLIX Bundle-Decoding Methodology](flix-decoding.md)** — the Vision-Q7
  multi-slot decoder (a faithful port of the binutils libisa pipeline; 14 formats
  / 46 slots / 1534 opcodes; validated at 549,375 per-slot mnemonics, 100%
  agreement against the device-native oracle) — and the **scalar-LX vs FLIX rule**
  that prevents the phantom-FLIX mis-decode of the NCFW management core.
- **[The Corpus, Tiers & Binary Inventory](corpus-inventory.md)** — the substrate:
  13 host libraries, 29 embedded device ELFs, the shipped headers/config/TIE, with
  sha256 anchors and the per-image carve manifest.
- **[Toolchain Inventory & Versions](toolchain-versions.md)** — the three
  independent version axes that are routinely conflated: **toolchain** (14.09 /
  RI-2022.9 / clang-10 for the four shipped gens, 15.05 / clang-15 for Maverick),
  **package** (0.21.2.0 / ucode 1.21.1.0), and **HW revision** (NX1.1.4 = 281040 =
  RI-2020.4 = LX7.1.4, MIN == MAX).
- **[Codename ↔ Generation Cross-Walk](codename-crosswalk.md)** — the definitive
  codename ↔ NC-version ↔ coretype ↔ arch_id ↔ product table, each cell pinned to
  a binary surface, with the v5-label-overload hazard called out.
- **[The Do-Not-Repeat / Correction Ledger](correction-ledger.md)** — the 67
  cross-report corrections (12 HIGH). State the corrected value, never the
  superseded one; the full ledger is also in
  [Appendix · Full Correction Ledger](../appendix/do-not-repeat-full-ledger.md).
- **[Master Glossary](../glossary.md)** — every term a reimplementer meets, each
  grounded in its anchor (Cairo, `ncore2gp`, NX1.1.4, Xtensa24, FLIX, FLIX-desync,
  scalar-LX, NCFW, TPB, the engine `engine_idx` numbering, RTTI, `.xt.prop`,
  `kernel_info_table`, the HW-Decode / Sunda-mode dispatch modes, TIE, ISS).

### Where to start

If you read three pages before the body, read these:

1. **[Part 1 · What GPSIMD Is — the one-screen map](../orientation/what-gpsimd-is.md)**
2. **[Part 1 · Keystone Facts Reimplementers Get Wrong](../orientation/keystone-facts.md)**
3. **[The Confidence & Walls Model](confidence-model.md)** — so every tag in every
   subsequent page reads correctly.

---

## 6. The compiler seam — cross-references to the `neuronx-cc` wiki

GPSIMD does not exist in isolation: a custom op is *compiled* by `neuronx-cc` into
a NEFF, then *dispatched* by the host runtime onto the eight Xtensa cores. This
wiki documents the **device + runtime** half; the **compiler** half — how the
opcodes documented here are *emitted* — lives in the sibling
[`neuronx-cc`](../../../../neuronx-cc/wiki/src/SUMMARY.md) wiki, whose **Part 11 —
Custom Ops & GPSIMD** is the matching seam. The cross-references, both directions:

| For… | This wiki | The `neuronx-cc` wiki |
|------|-----------|------------------------|
| The 8-core Xtensa ELF layout | [Part 7 · Custom-Op ABI](../abi/programming-model.md), [Part 8 · SPMD model](../runtime/spmd-teardown.md) | [`custom-ops/gpsimd-xtensa-layout.md`](../../../../neuronx-cc/wiki/src/custom-ops/gpsimd-xtensa-layout.md) |
| The custom-op CPU ABI | [Part 7 · Complete Custom-Op ABI](../abi/complete-customop-abi.md) | [`custom-ops/customop-cpu-abi.md`](../../../../neuronx-cc/wiki/src/custom-ops/customop-cpu-abi.md) |
| How opcodes are emitted | [Part 12 · Compiler Map (emit_*→opcode)](../compiler/compiler-map.md) | [`isa/collective-customop-encoding.md`](../../../../neuronx-cc/wiki/src/isa/collective-customop-encoding.md), [`nki/neuroncodegen-builtin-customop.md`](../../../../neuronx-cc/wiki/src/nki/neuroncodegen-builtin-customop.md) |
| The SORT / TOPK builtin | [Part 5 · Sort / DECODE_SORT](../firmware/kernels/sort.md) | [`custom-ops/bitonic-sort-topk.md`](../../../../neuronx-cc/wiki/src/custom-ops/bitonic-sort-topk.md) |

The dedicated bridge page is **[Part 12 · Compiler Cross-Reference to the
`neuronx-cc` Wiki](../compiler/crossref-neuronxcc.md)**. One discipline to carry
across the seam: the **NKI/compiler engine enum** (`tensor=1, scalar=2, gpsimd=3,
dma=4, vector=5, sync=6`) is a *different integer space* from the firmware
`engine_idx` (`PE 0, ACT 1, POOL 2, DVE 3, SP 4, TOP_SP 5`) used throughout this
wiki — do not equate the two. `[HIGH/OBSERVED]`

---

> **Provenance.** Every fact in this guide derives solely from static analysis of
> shipped, redistributable binaries, headers, config files, and TIE artifacts —
> lawful interoperability reverse engineering under DMCA 17 U.S.C. § 1201(f). No
> vendor source tree was referenced, consulted, or quoted; everything reads as
> derived from binary, header, config, and TIE analysis alone.
