# How to Read This Book

> *Version pin (every artifact this book derives from): **`libnrt.so` 2.31.24.0** (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`, real file `libnrt.so.2.31.24.0` → SONAME `libnrt.so.1`, BuildID[sha1] `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, ELF64 x86-64 DYN, **not stripped, DWARF v4**), the **DKMS GPL-2.0 kernel** (`aws-neuronx-dkms 2.27.4.0`, `neuron.ko` shipped as C source), the two firmware/microcode carriers **`libncfw.so`** (symtab-only) and **`libnrtucode_extisa.so`** (stripped), the forked NCCL **`libnccom.so`** (`aws-neuronx-collectives 2.31.24.0-1a31ba186`, DWARF), and the static archive **`libnds.a`** folded into `libnrt.so`.*
>
> *Part 0 — Reference Apparatus / NARRATIVE on-ramp · **Evidence grade:** this page documents the book's reader-facing conventions; its own claims are facts about those conventions and about the named artifacts (section geometry, RTTI counts). · [back to index](../index.md)*

## Abstract

This book reverse-engineers the AWS Neuron **runtime** stack at reimplementation grade — the layer that loads a compiled model onto Trainium/Inferentia silicon, runs inference, and orchestrates collectives. It is written for a senior systems engineer who knows compilers, ELF, and ISA encoding but has never opened these binaries, and who wants to **rebuild** a subsystem from the page alone. That single reader sets the bar for every page: enough mechanism and algorithm to reconstruct the subsystem, not a transcript of decompiler output.

Every claim rests on one of two evidence sources and nothing else. The first is **static analysis of the shipped binaries** — `libnrt.so` and `libnccom.so` carry DWARF v4, so functions, struct fields, and enum values survive; `libncfw.so` and `libnrtucode_extisa.so` degrade to symtab-only and stripped, where attribution falls back to symbols and embedded strings. The second is **direct reading of the GPL-2.0 kernel C source** that ships inside the DKMS package, which is *read*, not reverse-engineered, and cited `file:line`. The producer-facing companion to this page — how that evidence is actually gathered, the toolchain, the source-tree attribution method — is [Methodology](../methodology.md); this page is the reader-facing side: how to *read* what the method produces.

The conventions below are fixed across the whole book, so a reader learns them once and navigates every page by reflex. They are: the **reimplementation bar** every page aims at, the **fixed page grammar** that makes sibling pages share a shape, the **confidence model** and the anchoring rule that keep reverse-engineering honest, the **VMA == file-offset** convention that makes every address usable, and the **callout taxonomy** — including how the book corrects a wrong claim *in place* rather than editing it away. The first page to read after this one is the end-to-end walkthrough; the close of this page points there.

## At a glance

| Convention | What it means | Where to learn more |
|---|---|---|
| Reimplementation bar | Every page lets a senior engineer rebuild the subsystem — exact mechanism + algorithm as C pseudocode, not a dependency graph | [§1](#1-the-reimplementation-bar) |
| Page grammar | Fixed section order: version-pin → Abstract → contract → at-a-glance → deep sections → Cross-References | [§2](#2-the-page-grammar) |
| Confidence model | `CERTAIN` / `HIGH` / `MEDIUM` / `LOW` on every reverse-engineered claim; absence of a tag means CERTAIN-or-HIGH | [§3](#3-the-confidence-model--anchoring-rule) |
| Anchoring rule | Every claim cites an address/offset/symbol/enum/string/`file:line`, **or** carries a confidence tag — never both absent | [§3](#3-the-confidence-model--anchoring-rule) |
| VMA == file offset | For `libnrt.so` every loadable section's virtual address equals its file offset — **delta zero** | [§4](#4-the-vma--file-offset-convention) |
| Callout taxonomy | `QUIRK` / `GOTCHA` / `NOTE` / `CORRECTION`, text markers only; corrections are in-place | [§5](#5-the-callout-taxonomy) |
| Five-binary scope | One runtime, one GPL driver, two firmware carriers, one forked NCCL, one static archive | [§6](#6-the-five-binary-scope) |

---

## 1. The reimplementation bar

The bar is uniform across every crucible-notes wiki and it is the single test a page must pass: **a competent engineer could rebuild this subsystem from the page alone**. Not "understand it", not "navigate to the right function" — *rebuild* it: the algorithm, the data layout, the wire format, and the decision logic at each branch. If a paragraph or table is present only because it was easy to extract, it is cut.

What the bar implies, concretely, is the difference between a *dependency graph* and a *mechanism*. A dependency graph says "`kmgr_load_nn_nc` calls `neff_parse`, then `kbl_model_add`, then `dlr_kelf_stage`". That is a call list; a reimplementer cannot rebuild from it. A mechanism says what `neff_parse` *does* — preflights the NEFF header, rejects `version_major > 2`, verifies the package hash (SHA-256 for packager 1, MD5 for 2), drives `libarchive` over the gzip-pax-tar payload, and inserts each member into a red-black tree keyed by path — and gives the core of it as annotated C pseudocode. The `### Algorithm` block is where that mechanism lives, and it is the heart of every reimplementable page.

So a good page carries: the data model that must be reproduced (the 7744-byte `model_t`, the 16-byte notification entry, the assert-call-site shape); the algorithm as C pseudocode that names the real symbol it models and the *why* of each branch; and the rationale — why the LICM-equivalents run where they run, why completion is notification-polled rather than kernel-signalled, why the per-arch HAL tables are `.bss` and empty in the file. Detail is welcome when it is digested; a 31-column resource grid is fine once the page first names its axes and which cells matter. Undigested detail — a thousand-row byte dump, raw `v224`/`a3` decompiler names, a "meaning" cell that restates a field's name — is the anti-pattern the bar exists to exclude.

## 2. The page grammar

Sibling pages share a fixed grammar so the reader learns the shape once. The opening is a contract: by the end of the at-a-glance table the reader knows what the subsystem is, what version it pins to, and where the hard anchors live. The order is invariant:

```text
# Title                          ── a plain noun phrase, exactly one per page
> version-pin blockquote         ── fixes the binary/build so every address is unambiguous
                                    + the Part / evidence-grade / back-to-index line
## Abstract                      ── 2–3 paragraphs: what it does, how it relates to the
                                    familiar frame (LLVM / NCCL / known theory), page preview
reimplementation-contract        ── a bullet list: the data model, the algorithm, the
                                    target-specific behaviour that must be reproduced
## At a glance                   ── a borderless | | key/value table of the hard anchors
## deep sections                 ── the body: Purpose / Algorithm / Function Map per unit
## Cross-References               ── links out, each with a one-line why
```

Inside the body, peer units (phases, engines, layers) share the same H3 vocabulary in the same order — `### Purpose`, `### Entry Point` (a `text` call-tree), `### Algorithm` (annotated `c` pseudocode), `### Function Map` (a table with a Confidence column), then knobs/data and considerations. A reader scanning for "how is the decision made" always finds it under `### Algorithm`, never buried in prose. Not every unit needs every section, but the order never moves. A tiny skeleton of a body unit:

```c
function neff_parse(neff):            // @0x4ca3f0 — Stage 1 container parse
    if neff.version_major > 2:        // reject unknown container revision
        return NRT_INVALID
    if !verify_hash(neff):            // SHA-256 (packager 1) / MD5 (packager 2)
        return NRT_FAILURE
    for member in untar(neff):        // libarchive over gzip-pax-tar
        rbtree_insert(neff.files, member.path, member)   // keyed by path
    return NRT_SUCCESS
```

The version-pin blockquote is mandatory on any page that cites addresses: a page of `0x…` addresses with no pinned build is unverifiable. Pin once, at the top — never repeat the version on every address. The full grammar, the table column grammars, and the pre-publish checklist are the producer's concern; the reader needs only to know that the shape is the same everywhere.

## 3. The confidence model + anchoring rule

Reverse-engineering is only as useful as its honesty about certainty. The book grades every reconstructed claim on a four-level scale, and the scale is uniform across the book so it is read once:

| Level | What it asserts | Typical evidence |
|---|---|---|
| **CERTAIN** | Read directly, not inferred — a constant from a `mov` immediate, a byte-decoded wire field, a line of GPL kernel C, an export in `.dynsym`. | GPL `file:line`; verbatim DWARF; a literal read from `.rodata`; a register-loaded immediate. |
| **HIGH** | Recovered from solid binary evidence with a sound but non-trivial inference step. | DWARF `DW_AT_name`; an `nm` symbol; a reloc-walked vtable slot; an assert triple cross-checked against the call site. |
| **MEDIUM** | A reasonable inference where one link is judgement — a regex-classified bucket, a basename-only attribution that could collide, an inherited library body mapped by delta. | basename-only TU cite; classifier bucket; negative-evidence claim. |
| **LOW** | A plausible reading offered explicitly as tentative, flagged inline so it is never mistaken for fact. | a sized-but-not-walked struct interior; a guessed field role marked `(LOW confidence)`. |

The model is enforced by one rule that governs the whole book — **the anchoring rule**:

> **Every claim is anchored to an address, offset, symbol, enum, string, or `file:line` — OR it carries a confidence tag.** A bare assertion with neither is forbidden. The Confidence column appears in every function map and every table of reverse-engineered claims; in prose, an inferred statement carries an inline `(LOW confidence)` / `(MED)` tag. The corollary a reader must internalize: **the absence of a tag means CERTAIN-or-HIGH by construction** — anything weaker is always marked, so an untagged sentence is one the book stands behind.

A worked example makes the grading concrete. The RTTI map of `libnrt.so` recovers its C++ class hierarchy from the typeinfo objects. The kind of each `_ZTI` typeinfo — root class, single-inheritance, or multiple/virtual inheritance — is read from the relocation at `_ZTI+0`, which points at one of the three `__cxxabiv1` type_info vtables. That read is direct, so the result is **HIGH**: 53 `__class_type_info` (root) + 191 `__si_class_type_info` (single-base) + **0** `__vmi_class_type_info`, totalling 244 — meaning *no multiple or virtual inheritance anywhere in `libnrt.so`*. The single-inheritance base edges follow from the `R_X86_64_RELATIVE` reloc at `_ZTI+16`; 183 of the 191 resolve inside `.symtab` (**HIGH**), and the remaining **8** point at libstdc++-external typeinfo not in this symbol table — those 8 edges are graded **LOW** in place, flagged as std/abi parents immaterial to the first-party tree. Same subsystem, three confidence levels, each tied to exactly what the bytes support: the reloc-derived kind split is HIGH, the unresolved external bases are LOW, and the reader can trust the HIGH rows verbatim while treating the LOW ones as re-derivable.

## 4. The VMA == file-offset convention

Every address on every page is usable as both a virtual address and a file offset because of one structural fact about `libnrt.so`: the image is **identity-mapped**. `readelf -lW` shows the writable `LOAD` segment as `Offset == VirtAddr == PhysAddr == 0xbeeaa0` — and all four `PT_LOAD` segments share that property, so `.text`, `.rodata`, `.data.rel.ro`, `.got`, and `.data` all read the identical bytes whether an address is treated as a VMA or a file offset. The book calls this **delta zero**, and it is what lets a page write "`kaena_khal` @`0xcaeb80`" and have the reader `xxd` that offset directly. (`.bss` is `NOBITS` — it occupies no file bytes at all — so its addresses are runtime RAM, never a file location.)

> **CORRECTION (HOW-TO-READ vs source notes) —** a `+0x400000` `.data` VMA→file-offset delta appears in some early scratch notes and must **not** be carried into any `libnrt.so` page. That figure is a fact about a *different* image (the libtpu / Kaena-profiler binary, where `.data` is not identity-mapped); for `libnrt.so.2.31.24.0` the delta is **zero** for every loadable section, proven by the `readelf -lW` RW-segment line above. Read `libnrt.so`'s `.data` / `.data.rel.ro` globals at their VMA directly. The four sibling binaries are *not* delta-zero — `libncfw.so`, `libnrtucode_extisa.so`, and `libnccom.so` carry a small page-aligned `+0x1000` shift on their writable data, so `.data` reads in those images must use the section-header `Off` column. The per-binary geometry, the proof, and this correction in full are owned by [reference/binary-layout](../reference/binary-layout.md).

The practical reading rule, then: on a `libnrt.so` page, treat any `0x…` as a file offset and the bytes are right; on a firmware/microcode/collectives page, trust the page's own offset cite (it has already applied the `+0x1000`); on a kernel page, there is no binary at all — every cite is `file:line` into the GPL C source.

## 5. The callout taxonomy

Callouts are blockquotes opened with a bold **text marker** — never an emoji glyph — that pull the reader's eye to something the surrounding prose would bury. There are exactly four:

| Marker | Use for |
|---|---|
| `> **QUIRK —**` | A counter-intuitive fact that bites a reimplementer who assumes the obvious (the `libnccom` arrow points out of `libnrt` at runtime, but its `DT_NEEDED` points back in). |
| `> **GOTCHA —**` | A trap where the naive implementation is silently wrong (the device-HAL dispatch tables are `.bss` and read as zeros from a static file dump). |
| `> **NOTE —**` | An important clarification that is not itself a trap. |
| `> **CORRECTION (tag) —**` | An earlier claim overturned by later analysis, stated **in place** with its provenance tag. |

The CORRECTION mechanism is the one worth dwelling on, because it is what keeps a long-lived reverse-engineering book trustworthy. **The book never silently edits a wrong claim out of existence.** When later analysis overturns an earlier reading, the page states the overturn *where the claim lived*, names the new evidence, and tags the correction with a provenance label so a reader who remembers the old claim sees exactly why it changed. The `+0x400000`-delta note in [§4](#4-the-vma--file-offset-convention) is itself a worked CORRECTION: the wrong figure is shown, the right one is given, and the evidence (`readelf -lW`) is cited — rather than the wrong figure being deleted as if it had never been written. The arrows in callouts and diagrams are house style (`→` forward, `←` back, `×` for a removed or invalid edge); the markers carry the meaning, the glyphs carry the geometry.

> **NOTE —** an honest gap is part of the apparatus, not a failure of it. Where a function was sized but not line-walked, the page says what was done (the range swept, the bodies sized) and what remains, and routes the remainder to the deep-dive backlog — a marked unknown is correct where a plausible fiction is a landmine for the reimplementer.

## 6. The five-binary scope

The whole book is the expansion of one stack. A Neuron host runs **one** userspace runtime, `libnrt.so`, and every other binary hangs off it: the **GPL kernel driver** (`neuron.ko`, reached by `ioctl` magic `'N'` + `mmap`), two **firmware carriers** that ship device code in their `.rodata` for `libnrt` to DMA onto silicon — `libncfw.so` (Xtensa collective-sequencer images) and `libnrtucode_extisa.so` (GPSIMD/Q7 microcode), both bound by `dlopen` — and the forked NCCL **`libnccom.so`** for multi-node collective transport (`dlopen`-forward, hard `DT_NEEDED` in reverse). The sixth artifact, the static archive `libnds.a`, is not a runtime object at all: it is compile-time linked into `libnrt.so`. The four edges out of `libnrt` use four different bind mechanisms, and confusing them breaks any reimplementation — the binary identities, the layered-stack diagram, and the bind model on every arrow are owned by [front/five-binaries](five-binaries.md).

## Start here

With the conventions in hand, the first technical read is **[An Inference, End to End](inference-walkthrough.md)** — it follows one inference from a NEFF file on disk to a completed output tensor, naming the symbol at each stage and forward-linking to the page that derives it. It is the map; the deep pages are the territory. Read it next, then follow its stage links into whichever subsystem you intend to rebuild.

## Cross-References

- [Methodology](../methodology.md) — the producer-facing companion: how the evidence is gathered (toolchain, evidence hierarchy, source-TU attribution) that this page teaches readers to consume.
- [front/inference-walkthrough](inference-walkthrough.md) — the first technical read: one inference end to end, the map these conventions are applied across.
- [front/five-binaries](five-binaries.md) — the five-binary scope ([§6](#6-the-five-binary-scope)) in full: identities, the layered stack, and the four bind models.
- [reference/binary-layout](../reference/binary-layout.md) — the delta-zero proof ([§4](#4-the-vma--file-offset-convention)), the section geometry, and the `+0x400000`-is-not-libnrt correction.
- [reference/extraction-status](../reference/extraction-status.md) — the per-page coverage map the confidence model produces, and the no-DWARF ceiling named per binary.
