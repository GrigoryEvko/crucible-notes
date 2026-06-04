# Evidence & Citation Conventions

> *Every fact in this book is recovered by static reverse engineering of `libtpu.so` from the `libtpu-0.0.40-cp314` wheel: a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d` (the unambiguous anchor; the wheel's package metadata reports version `0.0.40`, and the runtime-reported `0.103` is not a literal string in the binary, so pin to the build-id). All addresses are absolute virtual addresses in that one binary; another wheel will differ in every address.*

## Abstract

This is the calibration page for the rest of the book. Every other page makes claims about a closed-source binary — a function does X, a struct field means Y, a dispatch table has N axes — and the only way a reader can trust those claims is to know exactly how they were obtained. This page fixes that contract. It states the single source of all facts (static analysis of the un-stripped `libtpu.so` with IDA Pro 9.x), lists the callout markers and what each one signals, pins the citation style for addresses and symbols, and enumerates what the extraction could **not** recover, so a reader knows where the floor is.

The house rule for the whole book is simple: **all analysis is from static reverse engineering of the binary; no source code or any other restricted material was used.** Names that look like C++ identifiers throughout the book are demangled symbols read directly out of the binary's own symbol table — the object is *not* stripped — not guesses, and not anything pulled from an external tree. Where a name does not appear in the binary, the page either says the claim is inferred or does not make it.

Read this page once. After it, every `> **QUIRK —**` callout and every `sub_ADDR` citation elsewhere in the book carries a precise, pre-agreed meaning.

For using this book, the contract is:

- **One binary, one provenance.** Every address, offset, count, and symbol traces to the static analysis of the single ELF described in the version pin above. Nothing is from running the library, from a debugger, or from any source tree.
- **Claims are anchored.** Every claim points at something a reader with the same binary can independently find — an address, an offset, a symbol, a string, or a table entry.
- **Gaps are stated, not hidden.** The decompiler failed on some functions and the disassembler flagged thousands of analysis problems. Those limits are published here so trust is calibrated, not assumed.

| | |
|---|---|
| **Source binary** | `libtpu/libtpu.so` (in the `cp314` manylinux wheel), 781,691,048 bytes |
| **Build-id** | `89edbbe81c5b328a958fe628a9f2207d` (NT_GNU_BUILD_ID, md5/uuid form) |
| **Extraction tool** | IDA Pro 9.x — disassembler + Hex-Rays decompiler |
| **Stripped?** | **No** — full symbol table; 822,847 functions carry a demangled C++ name |
| **Recovered functions** | 884,832 (881,784 named / 3,048 anonymous `sub_`) |
| **Evidence form** | Decompiled C bodies + disassembly + a family of JSON sidecars |
| **Callout markers** | `QUIRK` · `GOTCHA` · `NOTE` |

---

## Sources of Evidence

There is exactly one source: the static analysis of the binary in the version pin. That analysis was performed once with IDA Pro 9.x, and its output is materialized as two things a page can cite — the **decompiled/disassembled bodies** and a **family of JSON sidecars** that index the binary's structure. No page cites a runtime trace, a log, a header, or a source file, because none was used.

### The decompiler and disassembler

The primary evidence is the per-function output of IDA's Hex-Rays decompiler (C-like pseudocode) backed by the raw x86-64 disassembly. The binary is large — 745 MiB, ~884.8 K functions — and overwhelmingly symbol-bearing, so the disassembler recovers real names for almost everything rather than `sub_`-only placeholders. A page's `### Algorithm` blocks are annotated rewrites of these decompiled bodies, not transcripts; the original `sub_ADDR` is kept in a comment so any claim can be cross-checked against the function it models.

A symbol name is treated as a hypothesis, not a fact: a function named `Validate…` whose body, on inspection, only logs and returns is documented by its **body**, not its name. When the name and the body disagree, the book follows the body and flags the discrepancy.

### The sidecar family

Alongside the bodies, the extraction emits a set of JSON sidecars, each indexing one structural facet of the binary. A page cites whichever sidecar carries the fact it needs. The ones that recur throughout the book:

| Sidecar | What it indexes |
|---|---|
| `functions` | Every recovered function: address range, size, callers/callees, demangled name, frame, stack vars, switch and try-block counts. |
| `names` | The symbol/name table: address ↔ symbol. |
| `strings` | Every string literal and its address; the anchor for most behavioral inference. |
| `segments` | ELF segment/section layout, permissions, ranges (cross-checks with `readelf`). |
| `data_tables` | Recovered constant/data tables (jump tables, descriptor arrays). |
| `switches` | Recovered `switch` dispatch structures and their jump targets. |
| `rtti` | C++ RTTI records: type names, class hierarchies, vtable bindings. |
| `fixups` | Relocations / pointer fixups across the image. |
| `xrefs` | Cross-references: who calls/reads/writes each address. |

The sidecars are not independent of the bodies — they are the same extraction viewed by facet — but agreement across several facets is what justifies a role claim that no single line states outright.

### Verified extraction scope

These headline figures are confirmed directly against the binary and its sidecars and are the canonical anchors the rest of the book builds on:

| Quantity | Value | Source |
|---|---|---|
| Recovered functions | 884,832 | `functions` sidecar (length) |
| ↳ named / anonymous | 881,784 / 3,048 | `functions` (name prefix split) |
| ↳ with demangled C++ name | 822,847 (~93 %) | `functions` (`demangled` non-null) |
| String literals | 1,249,324 | `strings` sidecar |
| RTTI records | 160,351 | `rtti` sidecar |
| Data tables | 40,313 | `data_tables` sidecar |
| Switch dispatches | 33,016 | `switches` sidecar |
| Pointer fixups | 1,069,659 | `fixups` sidecar |

That the object is **not stripped** is the single most consequential fact for trust: with ~99.66 % of functions named and ~93 % carrying a full demangled C++ signature, most role claims start from a real symbol rather than a `sub_` guess.

---

## Callout Vocabulary

The book uses three blockquote markers — bold text, never an emoji — to pull a reader's eye to something prose would bury. Each has a fixed meaning:

| Marker | Signals | One-line definition |
|---|---|---|
| `> **QUIRK —**` | A counter-intuitive fact. | Something true that contradicts the obvious assumption; a reimplementer who assumes the obvious gets it wrong. |
| `> **GOTCHA —**` | A trap. | A place where the naive implementation is *silently* wrong — it compiles, runs, and produces incorrect results. |
| `> **NOTE —**` | A clarification. | An important point that is not a trap and not counter-intuitive, but easy to miss. |

---

## Citation Style

Claims are anchored to the binary with a small, fixed citation grammar. Learn it once and every anchor elsewhere parses at a glance.

- **Addresses are absolute virtual addresses**, hex, in the image described by the version pin: `0xfe21da0`, `sub_E635524`. They are *not* file offsets and *not* RVAs relative to a section. A bare `sub_ADDR` is the IDA placeholder name for an anonymous function at that VA.
- **Symbols are cited mangled-and-demangled where the demangled form aids reading.** The binary stores the mangled C++ name (e.g. `_ZNSt3__u...`); pages present a readable demangled form and keep the mangled symbol available for exact lookup. When a page uses a friendly name in pseudocode, the real `sub_ADDR` it models sits in a comment on the same line.
- **Struct and object fields are cited as base+offset.** A field is `object+0x18` or `code_object+512` — the byte offset into the structure — because the binary has no field names; the offset *is* the field's identity. A struct-layout table uses `Field | Offset | Type | Meaning` columns.
- **Tables, switches, and data are cited by their anchor address and, where relevant, an index.** "the dispatch table at `0x…`, slot 7" or "the switch in `sub_…` (33,016 recovered switches total)."
- **Strings are cited by their literal text and address.** Behavioral inference that leans on a string names the string so a verifier can find it in the `strings` sidecar and judge the inference.

The rule behind all of it: **every claim points at something a reader with the same binary can independently find.** If a statement cannot be anchored to an address, offset, symbol, string, or flag bit, the page either says so or does not make it.

> **NOTE —** an absolute VA equals the raw file offset only in `.text`, `.rodata`, and `.lrodata`. For a struct or table resident in `.data` the file offset is VA − `0x400000`; in `.data.rel.ro` it is VA − `0x200000`. Seeking with `xxd`/`objdump` at the bare VA for data in those sections reads the wrong bytes; subtract the section delta first. The full section map is in [ELF Anatomy](../forensics/elf-anatomy.md).

> **NOTE —** a vtable slot index is measured from the vptr, which is the `_ZTV` symbol **+ 0x10** — past the two-word header (offset-to-top, then the `type_info` pointer). A `call *0xN(%rax)` therefore lands at slot `(0xN − 0x10)/8`; computing it from the bare `_ZTV` address overcounts by one header (`0x10`). Cross-check any slot number against a real call site. The vtable layout is decoded in [RTTI & Vtable Census](../forensics/rtti-vtable-census.md).

### A worked anchor

A typical anchored sentence in this book carries every layer of the citation grammar at once. For instance, a page might write:

```text
The shape-inference dispatcher at `sub_FE21DA0`
(`_ZNSt3__u10__function13__policy_funcI…11__call_funcI…E`) reads the
op kind from `ctx+0x10` and switches on it (one of the 33,016 recovered
switches); the default arm returns `kInvalidArgument`.
```

Unpacked, that one sentence gives a verifier four independent handles: the **absolute VA** `sub_FE21DA0` to navigate to, the **mangled symbol** to confirm it is the right function, the **base+offset** `ctx+0x10` for the field being read, and the **switch** as the dispatch mechanism — each checkable against the binary and the matching sidecar.

---

## Known Extraction Limits

The extraction is broad but not total, and a reader calibrating trust needs the failure surface as much as the success surface. Three classes of gap matter.

### Functions the decompiler could not recover

The Hex-Rays decompiler failed to produce a C body for a small set of functions — the recorded error is `idaapi.decompile returned no cfunc`. Across the extraction, **516 functions** are flagged this way; they tend to be the gnarliest cases (deeply nested template instantiations, `__policy_func` call dispatchers, oversized bodies). For these, only the raw disassembly exists, so a behavioral claim leans on the disassembly and `xrefs` rather than a decompiled line. A page touching one of these says so.

### Disassembler analysis problems

IDA flagged **7,915 analysis problems** during recovery. These are not all "missing functions" — they are points where the analyzer was uncertain about code/data boundaries, stack frames, or instruction heads. The breakdown:

| Problem type | Count | What it means |
|---|---|---|
| `final` | 4,188 | Analysis reached a fixed point with a residual ambiguity. |
| `rolled` | 1,659 | A decision was rolled back during reanalysis. |
| `disasm_problem` | 942 | A spot the disassembler could not cleanly resolve to instructions. |
| `bad_stack` | 574 | Stack-pointer tracking was inconsistent — frame/stack-var claims here are weaker. |
| `head_problem` | 545 | Uncertain instruction-head alignment. |
| `illegal_addr` | 7 | A reference to an address outside any mapped region. |

None of these invalidates the headline counts (which were cross-checked against the raw binary), but a small fraction of per-function detail — especially stack-variable layouts near `bad_stack` sites — is less reliable than the bulk.

### What static analysis structurally cannot see

Some facts are simply not in a static image, and no amount of decompilation recovers them:

- **Runtime-only values** — actual buffer sizes negotiated at init, real device topologies, environment-driven knob values. The book documents the *code paths* that consume them, not the values.
- **Data behind indirection the analyzer cannot resolve** — a computed jump or vtable call whose target depends on runtime state appears as an indirect edge, not a concrete callee. The `xrefs`/`switches` sidecars resolve what they can; the rest is identified by shape.
- **Semantics of opaque blobs** — compressed or descriptor-pool regions are identified by shape and entry points, not fully decoded, unless a dedicated page does the decoding.

> **GOTCHA —** the demangled symbol names are a gift, but they describe what the *original author named* a function, not necessarily what it does in this build. A function named `Validate…` whose body, on inspection, only logs and returns is documented by its **body**, not its name. When the name and the body disagree, the book follows the body and flags the discrepancy — and so should any reader re-deriving a claim from a name alone.

---

## Cross-References

- [How to Read This Book](how-to-read.md) — reading paths and the dependency-flow rationale.
- [Codename Cheat-Sheet](codename-cheatsheet.md) — sibling front-matter page; the vocabulary glossary to this page's evidence grammar.
- [Methodology](../methodology.md) — the full process: acquisition, tooling, cross-validation discipline, and the legal basis.
- [Binary Forensics Overview](../forensics/overview.md) — the canonical headline structural counts, confirmed against the raw binary; the source of the version-pin numbers reused here.
- [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md) — how the 33,016 switches and 40,313 data tables are read.
- [RTTI & Vtable Census](../forensics/rtti-vtable-census.md) — how the 160,351 RTTI records become class-hierarchy claims.
- [ELF Anatomy](../forensics/elf-anatomy.md) — the segment/section layout behind every absolute address cited in this book.
