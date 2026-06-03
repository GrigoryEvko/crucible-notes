# Methodology

> *Every fact in this book is recovered by static reverse engineering of `libtpu.so` from the freely-redistributed `libtpu-0.0.40-cp314` PyPI wheel (`manylinux_2_31_x86_64`): a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d`. The build-id is the unambiguous pin; the wheel's package metadata reports version `0.0.40`. Another wheel will differ in every address.*

## Abstract

This page describes how the entire reconstruction was performed: where the binary came from, what tool read it, what the analysis emitted, how each claim in the rest of the book was cross-checked before it was written down, what could not be recovered and why, how a reader could repeat the work, and the legal basis for doing it. It is the process counterpart to [Evidence & Confidence Conventions](front/evidence-conventions.md), which defines the trust labels every other page applies; this page defines the pipeline those labels grade.

The whole book derives from one act repeated across nearly a million functions: load a single shared object into a disassembler, let it recover code and data, decompile each function to C, and serialize everything — disassembly, decompiled bodies, control-flow graphs, type information, cross-references — into machine-readable sidecars. No source tree, no debugger, no running TPU, and no Google-internal artifact entered the process. The `libtpu.so` shipped in this wheel is **not stripped**: its `.symtab` survives, so the C++-looking identifiers throughout the book are demangled symbols read out of the binary's own symbol table, not reconstructions. That single fact is what makes a 745 MB object tractable — every function arrives pre-named, and the analyst's job is to recover *behavior*, not *names*.

The method is adversarial by design. A symbol name is a hypothesis, not a fact; a function called `ValidateLength` is treated as un-validated until its decompiled body shows the length compare. Every headline claim is re-checked against the decompiled C or the raw bytes, and a claim earns a higher Confidence grade only when several independent indicators — body, callers, referenced strings, dispatch-table position — agree. The pipeline below is the machinery; the cross-validation discipline is what turns its output into a reference rather than a transcript.

For reproducing the methodology, the contract is:

- **The acquisition path** — the exact wheel, what it contains, and why redistribution makes it lawful to analyze.
- **The tool and its passes** — IDA Pro 9.x auto-analysis, the Hex-Rays decompiler, and FLIRT library-signature matching, and which of these actually fired on this binary.
- **The sidecar family** — the JSON artifacts that hold the recovered facts, with verified counts, so a reimplementer knows what evidence backs the book.
- **The cross-validation rule** — multiple independent indicators before a claim is trusted; the in-place correction discipline when one is overturned.
- **The published floor** — the decompilation failures, analysis problems, and firmware walls that bound what any page can assert.

| | |
|---|---|
| **Source artifact** | `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl` (PyPI, freely redistributable) |
| **Analyzed binary** | `libtpu/libtpu.so` — 781,691,048 bytes, ELF64 `DYN`, x86-64 |
| **Build-id** | `89edbbe81c5b328a958fe628a9f2207d` (NT_GNU_BUILD_ID, md5/uuid form) |
| **Stripped?** | **No** — `.symtab` present; most functions carry a demangled C++ name |
| **Tool** | IDA Pro 9.x — auto-analysis + Hex-Rays decompiler + FLIRT |
| **Recovered functions** | 884,832 (per binary metadata) |
| **Recovered strings** | 1,249,324 |
| **Switch tables** | 33,016 |
| **Per-function artifact files** | 884,843 each in `context/`, `decompiled/`, `disasm/`, `graphs/` |
| **Published limits** | 516 decompilation failures · 7,915 analysis problems |
| **Confidence semantics** | Defined once in [evidence-conventions](front/evidence-conventions.md) — not re-defined here |

---

## The Pipeline at a Glance

Five stages take the wheel to a wiki page. Each stage's output is the next stage's input; the discipline lives in the fourth.

| Stage | Input | Action | Output | Confidence |
|---|---|---|---|---|
| **Acquire** | PyPI wheel name | Download, unzip, locate `libtpu.so`, record build-id + hashes | A pinned 781,691,048-byte ELF | CERTAIN |
| **Analyze** | The ELF | IDA 9.x auto-analysis: code/data recovery, function boundaries, xrefs, type propagation | An IDB with 884,832 functions | CERTAIN |
| **Extract** | The IDB | Hex-Rays decompile + serialize every function and table to JSON sidecars and per-function files | The sidecar family + 884,843×4 per-function artifacts | CERTAIN |
| **Cross-validate** | Sidecars + bodies | For each claim, require the decompiled body or raw bytes to support it; demand multiple agreeing indicators for High | A graded claim with an address anchor | per-claim |
| **Write** | Graded claims | Synthesize into a reimplementation-grade page; mark gaps; file corrections in place | A wiki page | per-claim |

> **NOTE —** the first three stages are mechanical and reproduce byte-for-byte from the same wheel and the same IDA version. The last two are analytical judgment, and that is exactly where the Confidence labels exist to tell a reader how far to trust each sentence.

---

## Acquisition

The analyzed object is not a leaked or extracted internal build. It is the `libtpu.so` inside the published Python wheel `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl`, the same artifact `pip install libtpu` retrieves to provide the TPU PJRT plugin. A wheel is an ordinary ZIP archive; unzipping it yields a `libtpu/` package directory whose payload is the shared object this book describes.

The wheel directory contains exactly six payload files, all of which are part of the analysis surface:

| File | Size | Role |
|---|---|---|
| `libtpu.so` | 781,691,048 B | The PJRT plugin — the primary analysis target, the subject of this book |
| `sdk.so` | 22,541,240 B | A companion shared object, analyzed in parallel (sibling extraction) |
| `__init__.py` | 1,131 B | Python entry shim that locates and loads the plugin |
| `LICENSE` | 229 B | The wheel's license file |
| `THIRD_PARTY_NOTICES.txt` | 731,537 B | Bundled third-party attributions — confirms the open-source components statically linked in |
| `SDK_THIRD_PARTY_NOTICES.txt` | 103,306 B | The same for `sdk.so` |

The identity of the analyzed file is pinned three ways so any reader can confirm they hold the same bytes: the size (781,691,048), the GNU build-id `89edbbe81c5b328a958fe628a9f2207d` read from the `NT_GNU_BUILD_ID` note, and the file's SHA-256. `readelf -h` reports an ELF64 `DYN` (shared object) for `Advanced Micro Devices X86-64`, and `readelf -S` shows a live `.symtab` — the basis for the un-stripped claim.

> **GOTCHA —** the build-id is in the kernel's md5/uuid form, not the more common SHA-1 form; it is a 16-byte digest, and `file` reports it as `BuildID[md5/uuid]`. Do not expect a 40-hex-character SHA-1 build-id here. The 32-hex-character value above is correct.

> **CORRECTION (METH-01) —** the house README framing speaks of "stripped x86-64 ELF binaries." For *this* artifact that is imprecise: `libtpu.so` retains its full `.symtab` and is reported by `file` as **`not stripped`**. The provenance discipline (static analysis only, no source, no restricted material) holds exactly as stated; only the "stripped" adjective does not apply to this particular binary. Every named function in the book is a *demangled symbol read from the binary*, which is why naming was never the bottleneck.

---

## Tooling & Extraction

### The analysis tool

A single tool produced every primitive fact: **IDA Pro 9.x**, run in three capacities.

- **Auto-analysis** walks the ELF, recovers the section/segment layout, identifies function boundaries, follows the control flow, builds the global cross-reference database, and propagates types through the recovered code. On this binary it resolved **884,832** functions and **33,016** switch/jump tables.
- **The Hex-Rays decompiler** lifts each function from x86-64 to a C-like pseudocode body. These bodies — not the raw disassembly — are the primary evidence for behavioral claims, because a `switch` or a guard compare is legible in C in a way it is not in a screen of `mov`/`cmp`/`jne`.
- **FLIRT** (Fast Library Identification and Recognition Technology) matches byte-pattern signatures of known library routines, so a statically-linked `memcpy` or libstdc++ helper is labeled as such instead of re-analyzed from scratch.

> **NOTE —** FLIRT contributed *no* recognized matches on this binary's primary extraction pass (the metadata records `flirt_matches: 0`). That is expected, not a defect: the binary is already richly symbolized by its surviving `.symtab`, so library routines arrive pre-named and FLIRT has nothing left to add. The signature pass was run; it simply had no work to do here.

### The sidecar family

Auto-analysis and decompilation are not the deliverable — their serialized output is. Every function and structured table is exported to a family of JSON **sidecars**, plus four per-function artifact files (decompiled C, raw disassembly, a context bundle, and a control-flow graph). The book is written against these sidecars, never against a live IDA session, so that every page is reproducible from files on disk.

The sidecar family, with verified presence and the count or size each carries:

| Sidecar | Holds | Verified scope | Confidence |
|---|---|---|---|
| `functions` | One record per recovered function (address, size, name) | 884,832 records | CERTAIN |
| `names` | The name/symbol table surface | ~847 MB | CERTAIN |
| `strings` | Every recovered string literal | 1,249,324 strings | CERTAIN |
| `segments` | ELF segment / section layout | 55 segments | CERTAIN |
| `data_tables` | Recovered static data tables | ~114 MB | CERTAIN |
| `switches` | Jump/switch dispatch tables | 33,016 tables | CERTAIN |
| `rtti` | C++ RTTI: type-info, vtables, class hierarchy | ~65 MB | CERTAIN |
| `fixups` | Relocations / address fixups | ~120 MB | CERTAIN |
| `xrefs` | The global cross-reference graph (code + data edges) | ~41 GB | CERTAIN |
| `enums` | Recovered enumeration types | present (small) | HIGH |
| `structures` | Recovered struct/class layouts | present (~290 KB) | HIGH |
| `frames` | Per-function stack-frame layouts | ~745 MB | CERTAIN |
| `entries` | Exported / entry-point symbols | present | HIGH |
| `imports` | Imported / external symbols | present | HIGH |
| `prototypes` | Externally-supplied prototypes (empty here) | empty `[]` | CERTAIN |
| `metadata` | The extraction manifest (counts, hashes, mode) | present | CERTAIN |
| `problems` | IDA-flagged analysis problems | 7,915 records | CERTAIN |
| per-function | `decompiled/*.c`, `disasm/*`, `context/*`, `graphs/*` | 884,843 files each | CERTAIN |

> **NOTE —** the per-function artifact count (884,843) is slightly higher than the function-record count in the metadata (884,832). The directory count includes a small number of thunk/alias/data-stub entries that receive an artifact file without being counted as a full function record. When a page cites a function *count*, it cites 884,832; when it cites artifact *coverage*, the per-function directories hold one file per analyzed entry.

> **CORRECTION (METH-02) —** the extraction manifest for the primary pass records `decompiled: 0` and `ctree: 0`. That is an artifact of a two-phase extraction, **not** a claim that nothing was decompiled. The first pass ran in a *fast* mode (boundaries, names, tables, xrefs) and deliberately deferred Hex-Rays; the decompiled bodies and control-flow trees were produced by subsequent split passes, yielding the 884,843 per-function `decompiled/*.c` files and control-flow-tree coverage over 884,332 of them (a 511-function gap). A reader who only inspects the manifest's `decompiled` counter will draw the wrong conclusion — the bodies exist on disk.

---

## Cross-Validation Discipline

A symbolized binary is seductive: a name like `xla::tpu::sparse_core::lowering_util::GetPadValue` reads like documentation. It is not. The name records what the original author *called* the function, which is a strong lead but not a verified behavior, and demangled C++ names routinely outlive the code that justified them. Every claim in the book is therefore re-checked against evidence one level more direct than the name.

The rule has three tiers, mapped onto the Confidence scale defined in [evidence-conventions](front/evidence-conventions.md#the-four-confidence-levels) — that page owns the definitions; this section describes the *procedure* that earns each grade.

```text
To earn High:    The decompiled body (or a byte-exact table in the binary)
                 literally contains the claimed construct — the switch, the
                 guard compare, the constant, the vtable slot. A verifier
                 opens decompiled/<addr>.c and points at the line.

To earn Medium:  No single line states it, but >=3 independent indicators
                 agree: the function's callers, the strings it references,
                 its position in a dispatch table, its frame shape. The
                 conclusion is trusted; the exact detail is re-checked.

To earn Low:     One weak indicator, uncorroborated — a single suggestive
                 string, one xref, a name that implies unseen behavior.
                 Recorded as a lead, never a foundation.
```

The cross-checks draw on different sidecars on purpose, so that the indicators are genuinely independent:

- **Body vs. name** — the `decompiled/*.c` file is read; the name is confirmed or demoted.
- **Callers vs. role** — the `xrefs` graph shows who calls the function and with what; a "validator" with no length-shaped caller is suspect.
- **Strings vs. behavior** — a function that references `"request too large"` corroborates a rejection path; the `strings` and `data_tables` sidecars supply these.
- **Dispatch position vs. purpose** — the `switches` and `rtti` sidecars place a function in a table or a vtable, which constrains what it can be.
- **Raw bytes vs. decompiler** — where Hex-Rays is uncertain (it flags this with its own warnings), the `disasm/*` file and the raw bytes are the tiebreaker.

When a later cross-check overturns an earlier claim, the page is not silently edited. A `> **CORRECTION (tag) —**` note is filed in place, with a provenance tag, so the reasoning trail survives — exactly the two corrections above. This is the single most important honesty mechanism in the book: a reader can see not only the current claim but the claim it replaced and why.

> **QUIRK —** the heaviest cross-validation burden falls on the *most* symbolized functions, not the least. A 600-character demangled C++ template name (the `pxc::mnemonics::ProtoToEnvMiscGenerated...` family, for instance) is so specific that it *feels* authoritative, yet these template-heavy functions are precisely the ones where IDA's analysis stumbles (see below). The richest name and the weakest analysis often coincide; the discipline exists to catch exactly that trap.

---

## Limits — What Could Not Be Recovered

The credibility of every page rests on these limits being published, not hidden. Static analysis of one binary has a hard floor, and the floor has three distinct causes. [Evidence & Confidence Conventions](front/evidence-conventions.md#known-extraction-limits) states the floor as a trust contract; this section describes its mechanics.

### Decompilation failures

Hex-Rays returned no `cfunc` for **516** functions: the decompiler ran, refused, and the function exists only as disassembly. These are not random. They cluster in:

- **Template-explosion code** — deeply nested C++ templates (variant-of-strong-int dispatch in the mnemonics layer, `addOperations<...>` registering hundreds of MLIR ops in one call) whose recovered control flow exceeds what the decompiler will lift.
- **Imported PLT/GOT stubs** — entries like `strlen`, `getenv`, `__tls_get_addr`, `MallocExtension_Internal_*` that are import thunks, not local code, and have no body to decompile.
- **Hand-written assembly** — cryptographic and math routines from statically-linked libraries (`bn_sqr8x_mont`, `bn_power5_nohw`, `md5_sha1_final`) that are assembly with no C to recover.

For these 516, the book relies on the disassembly, the surrounding xrefs, and the name — and grades any behavioral claim accordingly (rarely above Low without independent corroboration).

### Analysis problems

IDA's auto-analysis flagged **7,915** problems during the disassembly pass, recorded in the `problems` sidecar. They fall into a few types: `disasm_problem` (a byte sequence the disassembler could not confidently decode), `bad_stack` (an unbalanced or unrecoverable stack frame), `head_problem` (an instruction-boundary ambiguity), and `final` (a problem persisting to the end of analysis). Like the decompilation failures, they concentrate in template-heavy compiler code (`primitive_util::*TypeSwitch`, `AlgebraicSimplifierVisitor`) and in statically-linked third-party assemblers (`X86AsmParser`, `AArch64AsmParser`, `PPCAsmParser::matchAndEmitInstruction`). A function carrying a `bad_stack` flag has un-trustworthy local-variable recovery, and any page touching it says so.

### What static analysis structurally cannot see

Beyond the per-function failures, three categories are invisible *in principle* to static analysis of this one file:

- **Firmware blobs** — payloads destined for the TPU hardware are embedded data, not x86 code. The disassembler sees bytes, not instructions; these are walls, documented as opaque regions rather than guessed at.
- **Runtime-only values** — anything computed at load or run time (resolved relocations after `dlopen`, environment-driven configuration, values arriving over ICI/network) has no static representation. The book documents the *code path* that consumes such a value, never the value.
- **The companion `sdk.so`** — analyzed in a sibling pass (94,732 functions, of which 94,292 decompiled), it is a smaller and largely independent object; this book's primary subject is `libtpu.so`, and `sdk.so` evidence is cited only where the two demonstrably interact.

> **NOTE —** the limits are bounded and small relative to the whole: 516 decompilation failures out of 884,832 functions is well under 0.1%, and 7,915 analysis problems span a binary with millions of instructions. The floor is published not because it is large but because a reverse-engineering reference that hid it would be untrustworthy on every page above it.

---

## Reproduction

The mechanical stages reproduce exactly; the analytical stages reproduce to the same evidence, with judgment grading the conclusions. A reader who wants to re-derive any claim follows this path:

```bash
# 1. Acquire the identical artifact from PyPI.
pip download libtpu==0.0.40 --no-deps --python-version 3.14 \
    --only-binary=:all: --platform manylinux_2_31_x86_64

# 2. A wheel is a ZIP. Unpack and locate the plugin.
unzip libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl -d libtpu_wheel
ls libtpu_wheel/libtpu/libtpu.so

# 3. Confirm you hold the same bytes (size + build-id must match the pin).
stat -c%s libtpu_wheel/libtpu/libtpu.so      # -> 781691048
readelf -n libtpu_wheel/libtpu/libtpu.so | grep -i 'build id'
                                             # -> 89edbbe81c5b328a958fe628a9f2207d
```

From there, the analysis is: load `libtpu.so` into IDA Pro 9.x, run full auto-analysis, and decompile. The per-function evidence the book cites is the decompiled C body at a given virtual address — open the address in Hex-Rays (or read the corresponding `decompiled/<addr>.c` artifact) and the claim graded `High` is verifiable line-for-line. Claims graded `Medium` or below require reconstructing the indicator chain this page describes; the address anchor in the citation is the entry point for that reconstruction.

> **GOTCHA —** reproduction fidelity depends on the IDA *version*. Auto-analysis heuristics, function-boundary recovery, and Hex-Rays output all shift between major IDA releases, so a different version may recover a slightly different function count or decompile a body this book lists as a failure (or vice-versa). Pin IDA 9.x to match the function and switch-table counts cited here. The binary's bytes are invariant; the analysis of them is not.

---

## Legal Basis

This work is reverse engineering of lawfully-obtained, publicly-distributed software for the purposes of interoperability and research. The binary was downloaded from PyPI under the terms that make it freely redistributable; no access control was circumvented, and no copyrighted source code, internal documentation, or other restricted material was used or consulted at any point. Every finding derives solely from analysis of the compiled binary that anyone may download.

The activity rests on well-established law on both sides of the Atlantic:

| Authority | Jurisdiction | What it establishes |
|---|---|---|
| **DMCA §1201(f)** | US | Reverse engineering for interoperability is a statutory exemption to the anti-circumvention rules. |
| **Sega v. Accolade** (9th Cir. 1992) | US | Disassembly to access unprotectable functional elements is fair use when it is the only means to that end. |
| **Sony v. Connectix** (9th Cir. 2000) | US | Intermediate copying during reverse engineering to build an interoperable product is fair use. |
| **EU Directive 2009/24/EC, Art. 6** | EU | Decompilation is permitted to achieve interoperability, without rightsholder authorization, within stated bounds. |

The book documents *what the binary does* — its functional behavior, data layouts, and decision logic — which are the unprotectable ideas and methods of operation, not the protected expression of any source text. The provenance discipline stated throughout (static analysis of the binary only) is precisely what keeps the work inside these boundaries: no protected source was copied, because none was ever in hand.

---

## Cross-References

- [Evidence & Confidence Conventions](front/evidence-conventions.md) — owns the four-level Confidence scale, callout vocabulary, and citation style this page grades against; read it before any other page.
- [Methodology (Deep)](appendix/methodology-deep.md) — the exhaustive sibling: sidecar-by-sidecar schema detail, per-pass logs, and the full extraction frontier.
- [Forensics Overview](forensics/overview.md) — the structural starting point for the binary itself: sections, sizes, and headline counts confirmed directly against the bytes.
- [ELF Anatomy](forensics/elf-anatomy.md) — the segment/section layout the `segments` sidecar records, read at the `readelf` level.
- [Dispatch-Table Taxonomy](forensics/dispatch-table-taxonomy.md) — how the 33,016 switch tables and the RTTI/vtable graph are read, the structured-evidence backbone of the behavioral pages.
