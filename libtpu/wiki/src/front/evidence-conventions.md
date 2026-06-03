# Evidence & Confidence Conventions

> *Every fact in this book is recovered by static reverse engineering of `libtpu.so` from the `libtpu-0.0.40-cp314` wheel: a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d` (the unambiguous anchor — the reported `0.103` is package metadata, not a literal string in the binary). All addresses are absolute virtual addresses in that one binary; another wheel will differ in every address.*

## Abstract

This is the calibration page for the rest of the book. Every other page makes claims about a closed-source binary — a function does X, a struct field means Y, a dispatch table has N axes — and the only way a reader can trust those claims is to know exactly how they were obtained and how confident the author was. This page fixes that contract. It states the single source of all facts (static analysis of the un-stripped `libtpu.so` with IDA Pro 9.x), defines the four Confidence levels in terms of *evidence directness* rather than vibes, lists the callout markers and what each one signals, pins the citation style for addresses and symbols, and — most importantly — enumerates what the extraction could **not** recover, so a reader knows where the floor is.

The house rule for the whole book is simple: **all analysis is from static reverse engineering of the binary; no source code or any other restricted material was used.** Names that look like C++ identifiers throughout the book are demangled symbols read directly out of the binary's own symbol table — the object is *not* stripped — not guesses, and not anything pulled from an external tree. Where a name does not appear in the binary, the page either marks the claim inferred or does not make it.

Read this page once. After it, every Confidence label, every `> **QUIRK —**` callout, and every `sub_ADDR` citation elsewhere in the book carries a precise, pre-agreed meaning, and you can decide per-claim how much weight to put on it.

For using this book, the contract is:

- **One binary, one provenance.** Every address, offset, count, and symbol traces to the static analysis of the single ELF described in the version pin above. Nothing is from running the library, from a debugger, or from any source tree.
- **Confidence is about evidence directness.** A label is not how *plausible* a claim feels — it is how *directly* the binary supports it. The four-level scale below is the whole vocabulary.
- **Gaps are stated, not hidden.** The decompiler failed on some functions and the disassembler flagged thousands of analysis problems. Those limits are published here so trust is calibrated, not assumed.

| | |
|---|---|
| **Source binary** | `libtpu/libtpu.so` (in the `cp314` manylinux wheel), 781,691,048 bytes |
| **Build-id** | `89edbbe81c5b328a958fe628a9f2207d` (NT_GNU_BUILD_ID, md5/uuid form) |
| **Extraction tool** | IDA Pro 9.x — disassembler + Hex-Rays decompiler |
| **Stripped?** | **No** — full symbol table; 822,847 functions carry a demangled C++ name |
| **Recovered functions** | 884,832 (881,784 named / 3,048 anonymous `sub_`) |
| **Evidence form** | Decompiled C bodies + disassembly + a family of JSON sidecars |
| **Confidence scale** | High → Medium → Low → Inferred (defined below) |
| **Callout markers** | `QUIRK` · `GOTCHA` · `NOTE` · `CORRECTION` |

---

## The Four Confidence Levels

Every reverse-engineered claim in this book carries — or could carry — one of four Confidence labels. The labels grade **how directly the binary supports the claim**, nothing else. A label is *not* a probability and *not* a measure of how reasonable the conclusion sounds; it is a statement about the evidence chain. A wildly plausible inference with no byte behind it is still `Inferred`; a boring fact read straight out of a decompiled body is `High` even if it is dull.

| Level | What it means | Typical evidence | How to treat it |
|---|---|---|---|
| **High** | Read directly from a decompiled function body or a byte-exact table in the binary. | A `switch` in the decompiled C; a vtable layout from the RTTI sidecar; a constant compared in a guard. | Trust verbatim. Reproduce as written. |
| **Medium** | Not stated by any single byte, but consistent across several independent indirect indicators that all point the same way. | A function's role inferred from its callers, the strings it references, and its position in a dispatch table — all agreeing. | Trust the conclusion; re-verify the exact detail before depending on it. |
| **Low** | Supported by a single weak indicator with no corroboration. | One suggestive string near a function; one cross-reference; a name that *implies* a behavior not seen in the body. | Treat as a lead, not a fact. Re-derive before building on it. |
| **Inferred** | Reasoned from structure, convention, or analogy with **no direct byte** asserting it. | "This must be the cleanup path because every other arm returns and this one falls through." | A hypothesis. Useful for orientation; never a foundation. |

> **NOTE (EVID-01) —** the dividing line that matters most is **High vs. everything below it.** High means a verifier with the same binary can point at the exact decompiled line or table entry and see the claim. Medium, Low, and Inferred all require the verifier to *reconstruct a reasoning chain* — they differ only in how much corroboration that chain has. When a page omits a label, read it as the page's default grade for that section (stated in the section), not as "certain."

The forensics pages — the ones that report headline structural counts confirmed directly with `readelf`/sidecar queries — sometimes use a stronger `CERTAIN` tag for a count that was checked against the raw binary byte-for-byte. Treat `CERTAIN` as the top of the same ladder: directly measured, not inferred. The four-level scale above is the working vocabulary for *behavioral* claims about code, where exact certainty is rarely available.

### The same claim at each level

The line between the levels is easiest to see drawn on one hypothetical function. Suppose a function `sub_X` is documented as "validates a buffer length and rejects oversized requests." Here is what each grade would *require* of the evidence behind that one sentence:

```text
High      The decompiled body of sub_X contains, in plain pseudocode,
          `if (len > 0x40000) return kInvalidArgument;` — the comparison,
          the constant, and the error path are all literally present.

Medium    sub_X has no such explicit compare, but it is named
          *ValidateLength*, every caller passes a length-shaped argument,
          and it references the string "request too large" — three
          independent indicators that agree on "length validation."

Low       sub_X references one string, "too large", and nothing else
          corroborates a validation role. A single weak hint.

Inferred  sub_X is the only arm of a dispatcher with no body claim at all,
          and it is *assumed* to be the validation step purely because the
          other arms are accounted for. No byte asserts it.
```

A page that grades this claim `High` is telling the reader: open `sub_X`, find that line. A page that grades it `Inferred` is telling the reader: this is scaffolding to orient you, go re-derive it before you depend on it.

---

## Sources of Evidence

There is exactly one source: the static analysis of the binary in the version pin. That analysis was performed once with IDA Pro 9.x, and its output is materialized as two things a page can cite — the **decompiled/disassembled bodies** and a **family of JSON sidecars** that index the binary's structure. No page cites a runtime trace, a log, a header, or a source file, because none was used.

### The decompiler and disassembler

The primary evidence is the per-function output of IDA's Hex-Rays decompiler (C-like pseudocode) backed by the raw x86-64 disassembly. The binary is large — 745 MiB, ~884.8 K functions — and overwhelmingly symbol-bearing, so the disassembler recovers real names for almost everything rather than `sub_`-only placeholders. A page's `### Algorithm` blocks are annotated rewrites of these decompiled bodies, not transcripts; the original `sub_ADDR` is kept in a comment so any claim can be cross-checked against the function it models.

### The sidecar family

Alongside the bodies, the extraction emits a set of JSON sidecars, each indexing one structural facet of the binary. A page cites whichever sidecar carries the fact it needs. The ones that recur throughout the book:

| Sidecar | What it indexes | Confidence of its data |
|---|---|---|
| `functions` | Every recovered function: address range, size, callers/callees, demangled name, frame, stack vars, switch and try-block counts. | High — structural, measured per function. |
| `names` | The symbol/name table: address ↔ symbol. | High — read from the binary's own tables. |
| `strings` | Every string literal and its address; the anchor for most behavioral inference. | High — bytes; *interpretation* of a string is Medium/Low. |
| `segments` | ELF segment/section layout, permissions, ranges. | High (CERTAIN) — cross-checks with `readelf`. |
| `data_tables` | Recovered constant/data tables (jump tables, descriptor arrays). | High for shape; Medium for *meaning*. |
| `switches` | Recovered `switch` dispatch structures and their jump targets. | High — the selector logic is in the body. |
| `rtti` | C++ RTTI records: type names, class hierarchies, vtable bindings. | High — the basis of the vtable census. |
| `fixups` | Relocations / pointer fixups across the image. | High — relocation entries. |
| `xrefs` | Cross-references: who calls/reads/writes each address. | High — used heavily for Medium-grade role inference. |

> **NOTE (EVID-02) —** when a page says "this function classifies X," the *body* (decompiler) is the High-confidence part; the *name* "X" usually comes from `names`/`strings`/`xrefs` and is what pushes the claim up from Medium toward High. The sidecars are not independent of the bodies — they are the same extraction viewed by facet — but agreement across several facets is exactly what justifies a Medium label.

### Verified extraction scope

These headline figures are confirmed directly against the binary and its sidecars and are the canonical anchors the rest of the book builds on:

| Quantity | Value | Source | Confidence |
|---|---|---|---|
| Recovered functions | 884,832 | `functions` sidecar (length) | CERTAIN |
| ↳ named / anonymous | 881,784 / 3,048 | `functions` (name prefix split) | CERTAIN |
| ↳ with demangled C++ name | 822,847 (~93 %) | `functions` (`demangled` non-null) | CERTAIN |
| String literals | 1,249,324 | `strings` sidecar | CERTAIN |
| RTTI records | 160,566 | `rtti` sidecar | CERTAIN |
| Data tables | 40,313 | `data_tables` sidecar | CERTAIN |
| Switch dispatches | 33,016 | `switches` sidecar | CERTAIN |
| Pointer fixups | 1,069,603 | `fixups` sidecar | CERTAIN |

That the object is **not stripped** is the single most consequential fact for trust: with ~99.66 % of functions named and ~93 % carrying a full demangled C++ signature, most role claims start from a real symbol rather than a `sub_` guess, which is what makes so many claims reachable at High rather than Inferred.

---

## Callout Vocabulary

The book uses four blockquote markers — bold text, never an emoji — to pull a reader's eye to something prose would bury. Each has a fixed meaning:

| Marker | Signals | One-line definition |
|---|---|---|
| `> **QUIRK —**` | A counter-intuitive fact. | Something true that contradicts the obvious assumption; a reimplementer who assumes the obvious gets it wrong. |
| `> **GOTCHA —**` | A trap. | A place where the naive implementation is *silently* wrong — it compiles, runs, and produces incorrect results. |
| `> **NOTE —**` | A clarification. | An important point that is not a trap and not counter-intuitive, but easy to miss. |
| `> **CORRECTION (tag) —**` | An overturned claim. | A prior assertion that later analysis disproved, recorded in place — usually with a provenance tag — rather than silently edited out. |

The first three carry no tag; `CORRECTION` normally carries a short provenance tag (e.g. `EVID-03`, `FOR-01`) so a specific reversal can be referenced and audited, and the deep pages tag the overwhelming majority of theirs. A few self-contained, page-local corrections — ones that merely overturn an earlier reading of the same passage and need no cross-page handle — appear as a bare `> **CORRECTION —**` instead; the tag is the norm, not an inviolable requirement. Either way a correction is never a silent edit — when analysis changes a conclusion, the old conclusion stays visible with the correction beside it, so a reader who memorized the old claim is actively warned.

> **QUIRK —** a `CORRECTION` block is *evidence of trustworthiness*, not a defect. A reverse-engineering book with zero corrections has either analyzed nothing hard or is hiding its mistakes. Treat the presence of in-place corrections as a signal that the surrounding claims were genuinely re-examined.

---

## Citation Style

Claims are anchored to the binary with a small, fixed citation grammar. Learn it once and every anchor elsewhere parses at a glance.

- **Addresses are absolute virtual addresses**, hex, in the image described by the version pin: `0xfe21da0`, `sub_E635524`. The version pin at the top of each page makes them unambiguous; they are *not* file offsets and *not* RVAs relative to a section. A bare `sub_ADDR` is the IDA placeholder name for an anonymous function at that VA.
- **Symbols are cited mangled-and-demangled where the demangled form aids reading.** The binary stores the mangled C++ name (e.g. `_ZNSt3__u...`); pages present a readable demangled form and keep the mangled symbol available for exact lookup. When a page uses a friendly name in pseudocode, the real `sub_ADDR` it models sits in a comment on the same line.
- **Struct and object fields are cited as base+offset.** A field is `object+0x18` or `code_object+512` — the byte offset into the structure — because the binary has no field names; the offset *is* the field's identity. A struct-layout table uses `Field | Offset | Type | Meaning` columns.
- **Tables, switches, and data are cited by their anchor address and, where relevant, an index.** "the dispatch table at `0x…`, slot 7" or "the switch in `sub_…` (33,016 recovered switches total)."
- **Strings are cited by their literal text and address.** Behavioral inference that leans on a string names the string so a verifier can find it in the `strings` sidecar and judge the inference for themselves.

The rule behind all of it: **every claim points at something a reader with the same binary can independently find.** If a statement cannot be anchored to an address, offset, symbol, string, or flag bit, the page either marks it Inferred or does not make it.

### A worked anchor

A typical anchored sentence in this book carries every layer of the citation grammar at once. For instance, a page might write:

```text
The shape-inference dispatcher at `sub_FE21DA0`
(`_ZNSt3__u10__function13__policy_funcI…11__call_funcI…E`) reads the
op kind from `ctx+0x10` and switches on it (one of the 33,016 recovered
switches); the default arm returns `kInvalidArgument`.
```

Unpacked, that one sentence gives a verifier four independent handles: the **absolute VA** `sub_FE21DA0` to navigate to, the **mangled symbol** to confirm it is the right function, the **base+offset** `ctx+0x10` for the field being read, and the **switch** as the dispatch mechanism — each checkable against the binary and the matching sidecar. The reader never has to take the conclusion on faith; the anchors *are* the proof.

> **NOTE (EVID-03) —** `sub_FE21DA0` is exactly the kind of function that lives near the extraction's limits: a `__policy_func`/`__call_func` template dispatcher. Some functions of this family are among the 516 the decompiler could not body (see below), in which case the same sentence would be graded `Low`/`Inferred` and would lean on the disassembly and `xrefs` rather than a decompiled line. The anchor format does not change; the Confidence does.

---

## Known Extraction Limits

A trust page that only listed strengths would be dishonest. The extraction is broad but not total, and a reader calibrating trust needs the failure surface as much as the success surface. Three classes of gap matter.

### Functions the decompiler could not recover

The Hex-Rays decompiler failed to produce a C body for a small set of functions — the recorded error is `idaapi.decompile returned no cfunc`. Across the extraction, **516 functions** are flagged this way; they tend to be the gnarliest cases (deeply nested template instantiations, `__policy_func` call dispatchers, oversized bodies). For these, only the raw disassembly exists, so any claim about their behavior is `Low` or `Inferred` at best — there is no decompiled line to point at. A page touching one of these will say so.

### Disassembler analysis problems

IDA flagged **7,915 analysis problems** during recovery. These are not all "missing functions" — they are points where the analyzer was uncertain about code/data boundaries, stack frames, or instruction heads. The breakdown:

| Problem type | Count | What it means for trust |
|---|---|---|
| `final` | 4,188 | Analysis reached a fixed point with a residual ambiguity. |
| `rolled` | 1,659 | A decision was rolled back during reanalysis. |
| `disasm_problem` | 942 | A spot the disassembler could not cleanly resolve to instructions. |
| `bad_stack` | 574 | Stack-pointer tracking was inconsistent — frame/stack-var claims here are weaker. |
| `head_problem` | 545 | Uncertain instruction-head alignment. |
| `illegal_addr` | 7 | A reference to an address outside any mapped region. |

None of these invalidates the headline counts (which were cross-checked against the raw binary), but they mean a small fraction of per-function detail — especially stack-variable layouts near `bad_stack` sites — is less reliable than the bulk. Where a page's claim sits on top of one of these, it inherits a lower Confidence.

### What static analysis structurally cannot see

Some facts are simply not in a static image, and no amount of decompilation recovers them:

- **Runtime-only values** — actual buffer sizes negotiated at init, real device topologies, environment-driven knob values. The book documents the *code paths* that consume them, not the values.
- **Data behind indirection the analyzer cannot resolve** — a computed jump or vtable call whose target depends on runtime state appears as an indirect edge, not a concrete callee. The `xrefs`/`switches` sidecars resolve what they can; the rest is marked Inferred.
- **Semantics of opaque blobs** — compressed or descriptor-pool regions are identified by shape and entry points, not fully decoded, unless a dedicated page does the decoding.

> **GOTCHA —** the demangled symbol names are a gift, but they describe what the *original author named* a function, not necessarily what it does in this build. A function named `Validate…` whose body, on inspection, only logs and returns is documented by its **body**, not its name. When the name and the body disagree, the book follows the body and flags the discrepancy — and so should any reader re-deriving a claim from a name alone. Name-only claims never rise above `Low`.

Taken together: trust the headline counts and the named, cleanly-decompiled bodies at High; treat behavioral roles as Medium unless the page shows the body; treat anything resting on a single name, a single string, a `bad_stack` frame, or a failed decompilation as `Low`/`Inferred` and re-derive it before building on it.

---

## Cross-References

- [How to Read This Book](how-to-read.md) — reading paths and the dependency-flow rationale; this page is its companion on *trust*.
- [Codename Cheat-Sheet](codename-cheatsheet.md) — sibling front-matter page; the vocabulary glossary to this page's evidence grammar.
- [Binary Forensics Overview](../forensics/overview.md) — the canonical headline structural counts, confirmed against the raw binary; the source of the version-pin numbers reused here.
- [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md) — how the 33,016 switches and 40,313 data tables are read; a worked example of Medium-confidence structural inference.
- [RTTI & Vtable Census](../forensics/rtti-vtable-census.md) — how the 160,566 RTTI records become High-confidence class-hierarchy claims.
- [ELF Anatomy](../forensics/elf-anatomy.md) — the segment/section layout behind every absolute address cited in this book.
