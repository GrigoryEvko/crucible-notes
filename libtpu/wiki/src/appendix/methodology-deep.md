# Methodology (Deep)

> *All counts, sizes, and addresses on this page apply to the `libtpu.so` inside `libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl`: a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d`, reported runtime version `0.103`. A different wheel will differ in every figure below.*

## Abstract

This is the audit-grade companion to the curated [Methodology](../methodology.md). That page tells a reader *how* the book was made and *what* its trust labels mean; this page hands an auditor the raw inventory those labels grade — every sidecar with its exact byte size and record count, the precise coverage and demangle figures, the full failure taxonomy down to the seven `illegal_addr` records, the cross-validation cross-checks named by which sidecar feeds each, and a reproduction recipe complete enough to re-derive the numbers in the tables here. Where the parent page rounds ("well under 0.1%"), this page gives the count. Where the parent page summarizes the gaps in a paragraph, this page tabulates them by type.

The discipline is unchanged and is not re-argued here: every primitive fact comes from one IDA Pro 9.x pass over one un-stripped ELF, serialized to JSON sidecars and per-function artifact files, and no source tree, debugger, or restricted material entered the process. What this page adds is the *ledger*. A claim in the book is only as trustworthy as the sidecar that backs it, and a reader auditing a claim needs to know that the `functions` sidecar is 3.9 GB of 884,832 records, that 822,847 of them carry a demangled C++ name, and that exactly 516 functions have no decompiled body. Those are the numbers below, each confirmed against the file on disk.

This page does not re-define the four Confidence levels — [Evidence & Confidence Conventions](../front/evidence-conventions.md) owns those — and it does not repeat the acquisition path or legal basis the parent already states. It deepens three things the parent only sketches: the **full sidecar inventory** (kind → contents → size → record count), the **coverage dashboard** (what fraction decompiled, the demangle rate, the per-name-class distribution), and the **gap audit** (the exact 516 + 7,915 failure taxonomy and where the un-decompilable spots cluster).

For an auditor, the contract is:

- **The sidecar ledger** — every JSON kind, what it holds, its byte size, its record count, so any cited evidence can be located and its scale understood.
- **The coverage figures** — function count, demangle rate, per-function artifact coverage, and the fast-pass counter trap that makes the manifest under-report.
- **The failure taxonomy** — the 516 decompilation failures and the 7,915 analysis problems broken down by type, with the address clusters where they concentrate.
- **The cross-validation map** — which sidecar supplies each independent indicator, and the four multi-sidecar agreement patterns (RTTI↔vtable, descriptor-pool↔symbol, fixup↔table, multi-indicator) that earn a claim its grade.
- **The reproduction recipe** — the exact commands that regenerate every number in this page's tables from the sidecars on disk.

| | |
|---|---|
| **Analyzed binary** | `libtpu/libtpu.so` — 781,691,048 B, ELF64 `DYN`, x86-64 |
| **Build-id / SHA-256** | `89edbbe81c5b328a958fe628a9f2207d` · `456e7d6e…784ae033` |
| **Tool** | IDA Pro 9.x — auto-analysis + Hex-Rays + FLIRT (0 matches) |
| **Function records** | 884,832 (`functions` sidecar) |
| **Demangle rate** | 822,847 / 884,832 = **93.0%** carry a demangled C++ name |
| **Per-function artifacts** | 884,843 each in `context/`, `decompiled/`, `disasm/`, `graphs/` |
| **Decompilation failures** | **516** (no `cfunc` returned) |
| **Analysis problems** | **7,915** (`problems` sidecar, 805,426 B) |
| **Switch tables / segments / RTTI rows** | 33,016 · 55 · 160,566 |
| **Confidence semantics** | Defined once in [evidence-conventions](../front/evidence-conventions.md) |

---

## Sidecar Inventory

The deliverable of extraction is the sidecar family: one JSON file per kind of recovered fact, written once and never re-read from a live IDA session. The book is written against these files, so an auditor can reproduce any cited fact by opening the named sidecar. Every size below is the byte length on disk; every record count is `length` over the file's top-level array (or the documented sub-key). The two-target extraction also produced a sibling set for `sdk.so` — out of scope here; all figures are the primary `libtpu.so` pass.

> **NOTE —** sidecar file names are prefixed with the full target stem `libtpu__libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64__libtpu__libtpu.so_`. The **Kind** column drops that prefix for readability; the on-disk file is `<stem>_<kind>.json`.

| Kind | Holds | Size | Records | Confidence |
|---|---|---|---|---|
| `functions` | One record per recovered function — addr, size, name, `demangled`, callers, callees, strings, switches, frame, flags | 4,131,147,732 B (3.9 GB) | 884,832 | CERTAIN |
| `callgraph` | Whole-binary caller→callee edge graph | 1,886,610,229 B (1.8 GB) | (edge graph) | CERTAIN |
| `names` | Name/symbol-table surface (every named address) | 847,352,845 B (809 MB) | — | CERTAIN |
| `frames` | Per-function stack-frame layouts | 744,718,196 B (711 MB) | per function | CERTAIN |
| `strings` | Every recovered string literal | 618,740,538 B (591 MB) | 1,249,324 | CERTAIN |
| `comments` | IDA auto- and analysis-comments per address | 495,648,733 B (473 MB) | — | CERTAIN |
| `switches` | Jump/switch dispatch tables | 339,618,667 B (324 MB) | 33,016 | CERTAIN |
| `function_addresses` | Flat address→name index (fast lookup) | 187,509,657 B (179 MB) | 884,832 | CERTAIN |
| `fixups` | Relocations / address fixups | 119,612,914 B (115 MB) | — | CERTAIN |
| `data_tables` | Recovered static data tables | 114,277,117 B (109 MB) | — | CERTAIN |
| `rtti` | C++ RTTI: type-info, vtables, class hierarchy | 64,877,616 B (62 MB) | 160,566 | CERTAIN |
| `problems` | IDA-flagged analysis problems (`addr`/`func`/`type`) | 805,426 B (788 KB) | 7,915 | CERTAIN |
| `structures` | Recovered struct/class layouts | 289,703 B (284 KB) | — | HIGH |
| `native_imports` | Dynamic-import (PLT/GOT) symbol surface | 221,835 B (217 KB) | — | HIGH |
| `native_exports` | Exported dynamic symbols | 116,039 B (113 KB) | — | HIGH |
| `imports` | Imported / external symbols | 97,483 B (95 KB) | — | HIGH |
| `entries` | Exported / entry-point symbols | 28,761 B (28 KB) | — | HIGH |
| `segments` | ELF segment / section layout | 10,940 B (11 KB) | 55 | CERTAIN |
| `enums` | Recovered enumeration types | 1,584 B | (small) | HIGH |
| `metadata` | Extraction manifest (counts, hashes, mode) | 616 B | 1 | CERTAIN |
| `prototypes` | Externally-supplied prototypes (empty here) | 2 B (`[]`) | 0 | CERTAIN |
| `ctree` (split) | Hex-Rays control-flow-tree dumps, sharded by address window | 97 shard files (~10 KB–56 MB each) | see coverage | CERTAIN |
| `decompilation_failures` (split) | Per-address Hex-Rays refusals, sharded | 19 shard files | 516 (summed) | CERTAIN |

> **GOTCHA —** the `xrefs` sidecar (~39 GB) is the global code+data cross-reference graph and is the single largest artifact in the family — larger than the binary itself by ~50×. It is not in the table above because at that scale it is streamed, not loaded; an auditor inspecting a specific cross-reference slices it by address rather than reading it whole. The `callgraph` (1.8 GB) is the function-level projection of it and is the practical entry point for "who calls X."

> **NOTE —** record counts are blank for sidecars whose top-level shape is a map keyed by address (`names`, `frames`, `comments`, `fixups`, `data_tables`) rather than a flat array. For those, the *size* is the meaningful scale signal; the per-address content is reached by lookup, not by enumeration.

---

## Coverage Dashboard

Coverage is measured three ways, and they do not all read off the same field. The function *count* is authoritative from the `functions` sidecar; the *demangle rate* is the fraction of those records carrying a non-empty demangled name; the *per-function artifact coverage* is the count of files in the four per-function directories. A correct dashboard cites the right field for each, because the extraction manifest's own counters under-report two of the three.

### Headline figures

```text
Function records (functions sidecar) .......... 884,832
  carry a demangled C++ name .................. 822,847   (93.0%)
  raw symbol but no distinct demangling ....... ~58,937   (plain-C symbols, thunks)
  sub_ADDR stubs (no symbol at all) ...........   3,048   (0.34%)

Per-function artifact files (each directory) .. 884,843
  context/  decompiled/  disasm/  graphs/ ...... 884,843 each
  ctree control-flow trees ..................... 884,332   (coverage gap: 511)

Strings ....................................... 1,249,324
Switch / jump tables .......................... 33,016
ELF segments .................................. 55
RTTI rows (type-info + vtable entries) ........ 160,566

Decompilation failures (no cfunc) .............     516   (0.058% of functions)
Analysis problems (problems sidecar) ..........   7,915
FLIRT library matches .........................       0   (binary self-symbolized)
```

### The demangle rate, precisely

The "93%" headline is the fraction of function records whose `demangled` field is non-empty and differs from the raw `name` — i.e. the symbol was a mangled C++ identifier that demangled to a distinct human-readable signature. That is 822,847 of 884,832. The remaining ~62,000 split into plain-C symbols that have no mangling to demangle (`bn_sqr8x_mont`, `__tls_get_addr`) and 3,048 `sub_ADDR` stubs that carry no symbol at all.

> **CORRECTION (METH-D1) —** a naive demangle estimate by counting `_Z`-prefixed names overshoots: 871,370 records (98%) begin with `_Z`, but ~48,500 of those either fail to fully demangle or demangle to a string identical to the raw name and so add no signature. The honest, field-backed demangle rate is the `demangled`-field count: **822,847 / 884,832 = 93.0%**. Cite that figure, not the `_Z`-prefix count, when a page states the demangle rate.

> **GOTCHA —** the extraction `metadata` records `decompiled: 0` and `ctree: 0`. Taken literally this would say *nothing was decompiled*. It is a fast-pass artifact: the manifest counts only what the first (boundary/name/table) pass produced, and that pass deliberately deferred Hex-Rays. The decompiled bodies and control-flow trees were produced by subsequent split passes — 884,843 `decompiled/*.c` files and 884,332 ctree entries exist on disk. An auditor who trusts the manifest's `decompiled` counter will conclude the opposite of the truth. Trust the directory counts, not the manifest counter, for decompilation coverage.

> **NOTE —** the per-function artifact count (884,843) exceeds the function-record count (884,832) by 11. The directories include a handful of thunk/alias/data-stub entries that get an artifact file without being booked as a full function record. A page citing a function *count* uses 884,832; a page citing artifact *coverage* uses 884,843.

### Function-class distribution

The `functions` sidecar tags each record with structural flags (`is_thunk`, `is_leaf`, `is_entry`, `is_library`), so the 884,832 partition along axes that matter for reading the call graph. The shape is dominated by leaves:

```text
Leaf functions (call nothing) ................. 326,941   (37.0%)
Non-leaf (have outgoing call edges) ........... 557,891   (63.0%)
Thunks (single-jump trampolines) .............. 750
Entry-point functions (is_entry) ............. 222
FLIRT library matches (is_library) ............ 0         (binary self-symbolized)
```

> **NOTE —** `is_library` is zero across all 884,832 records, consistent with `flirt_matches: 0` in the manifest. The binary's surviving `.symtab` named the statically-linked library code directly, so FLIRT had nothing to label and IDA marked no function as a library match. A reimplementer should not expect FLIRT to contribute here; the symbol table already did its job.

---

## Gap Audit / Failure Taxonomy

The credibility of every page above rests on this section being exact rather than rounded. There are two published floors — decompilation failures and analysis problems — and they are distinct phenomena with distinct causes. Neither is random; both cluster in identifiable code, and an auditor can predict where a new failure will appear from the taxonomy below.

### Decompilation failures — the 516

These are functions for which Hex-Rays returned no `cfunc`: the decompiler ran and declined, leaving only disassembly. The 516 records live across 19 sharded `decompilation_failures` sidecars (summing to 516 exactly), and they fall into three structural causes:

| Cause | What it is | Representative addresses / names | Confidence |
|---|---|---|---|
| **Template-explosion** | Deeply nested C++ templates whose recovered control flow exceeds the decompiler's lift budget — variant-of-strong-int dispatch in the mnemonics layer, `addOperations<...hundreds of MLIR ops...>` in one call, `jellyfish::ReduceEmitter::EmitReduction` | `0xfedc180` (`mlir::Dialect::addOperations<…>`), `0x13e16240` (`ReduceEmitter::EmitReduction`) | HIGH |
| **Import / data stubs** | PLT/GOT thunks and `__imp_*` slots that are not local code and have no body to lift — `strlen`, `getenv`, `abort`, `__tls_get_addr`, `MallocExtension_Internal_*` | `0x22860108`–`0x22861110` (40+ import slots, one shard) | CERTAIN |
| **Hand-written assembly** | Cryptographic and math routines from statically-linked libraries that are assembly with no C source to recover — `bn_sqr8x_mont`, `bn_power5_nohw`, `md5_sha1_final`, the dnnl JIT convolution kernels | `0x206ee040`–`0x2071e720` (BoringSSL bignum/MD5), `0x1b012c00` (dnnl AVX-512) | CERTAIN |

For these 516, the book relies on disassembly, surrounding xrefs, and the name, and grades any behavioral claim accordingly — rarely above Low without independent corroboration. The largest single shard holds 476 of the 516 (a dense run of import slots and template instantiations swept in one address window); the remainder are scattered ones and twos across the other 18 shards.

### Analysis problems — the 7,915

Separate from decompilation, IDA's auto-analysis flags positions where its disassembly was uncertain. The `problems` sidecar (805,426 B) holds 7,915 records, each an `{addr, func, type}` triple. The type distribution is exact:

| Type | Count | Meaning | Where it clusters | Confidence |
|---|---|---|---|---|
| `final` | 4,188 | A problem persisting to the end of analysis — the function was analyzed but a residual uncertainty was never resolved | Compiler-backend drivers (`GenerateIsaProgram`), protobuf descriptor machinery | CERTAIN |
| `rolled` | 1,659 | Analysis was rolled back / re-attempted at this address (a boundary the analyzer revised) | Template-heavy emitters, type-switch dispatch | CERTAIN |
| `disasm_problem` | 942 | A byte sequence the disassembler could not confidently decode into an instruction | `pxc::mnemonics::ProtoToEnvMiscGenerated…`, the statically-linked `*AsmParser::matchAndEmitInstruction` family (PPC/X86/AArch64) | CERTAIN |
| `bad_stack` | 574 | An unbalanced or unrecoverable stack frame — local-variable recovery is untrustworthy here | `primitive_util::*TypeSwitch`, `AlgebraicSimplifierVisitor`, sparse-core lowering | CERTAIN |
| `head_problem` | 545 | An instruction-boundary (head) ambiguity — IDA could not pin where an instruction starts | Adjacent to `disasm_problem` sites in the same mnemonics functions | CERTAIN |
| `illegal_addr` | 7 | A reference to an address outside any defined segment | Rare; 7 isolated sites | CERTAIN |
| **Total** | **7,915** | | | CERTAIN |

> **QUIRK —** the heaviest problem density falls on the *most* richly named functions, not the least. The `pxc::mnemonics::ProtoToEnvMiscGenerated…` function carries a 600-character demangled template signature *and* simultaneously holds `disasm_problem`, `head_problem`, and `bad_stack` flags at multiple addresses. A long, specific symbol name feels authoritative precisely where IDA's analysis is shakiest; the cross-validation discipline exists to catch that exact coincidence. A `bad_stack` flag is the loudest single warning — it means the stack-variable names in the decompiled body may be fictional.

> **NOTE —** the two floors are independent. A function can decompile cleanly yet carry a `final` problem (a residual data-flow uncertainty that did not block the lift), and a function can be problem-free yet fail to decompile (an import stub). The 516 and the 7,915 are not subsets of one another; an auditor counts them separately. Together they bound what any page may assert: 516 functions have no body to cite, and ~5,700 functions (the `bad_stack` + `disasm_problem` + `head_problem` + `illegal_addr` sites) have a body whose local detail is suspect.

### What static analysis structurally cannot see

Beyond the per-function floors, three categories are invisible in principle and are documented as walls, never guessed:

- **Firmware blobs** — payloads bound for TPU hardware are embedded data, not x86 code; the disassembler sees bytes. These appear in the `data_tables` and segment maps as opaque regions.
- **Runtime-only values** — anything resolved at `dlopen` or run time (post-relocation pointers, environment-driven config, ICI/network-arriving values) has no static form; the book documents the consuming code path, never the value.
- **The companion `sdk.so`** — a sibling 94,732-function pass, cited only where the two objects demonstrably interact.

---

## Cross-Validation Techniques

A symbolized binary is seductive — a name reads like documentation — so every claim is re-checked against evidence one level more direct than the name, and a high grade requires *multiple independent indicators to agree*. The independence is real because the indicators come from *different sidecars*. The parent page states the three-tier grading procedure; this section names the four concrete agreement patterns the book uses and the sidecars each consumes.

### The four agreement patterns

```text
RTTI ↔ vtable
  Source: rtti sidecar (160,566 rows) + the function whose address sits in a
  vtable slot. A type-info record names a class; the vtable lists its virtual
  method addresses; each address resolves (via functions sidecar) to a named
  member. Agreement of all three pins a method's identity and its dispatch
  position simultaneously. A name that claims to be a virtual override but
  appears in no vtable slot is demoted.

descriptor-pool ↔ symbol
  Source: strings + data_tables (the protobuf descriptor pool: message names,
  field names, type tags) cross-checked against the demangled symbol. A function
  named …MessageLite…SerializeWithCachedSizes whose body references the matching
  descriptor strings is corroborated; a serialization name with no descriptor
  reference is suspect.

fixup-relocation ↔ table
  Source: fixups + switches/data_tables. A jump-table or vtable is only trusted
  as such when the fixup records show the relocations that populate its slots
  land inside the table's address range. A "table" with no fixups pointing into
  it is treated as raw data, not dispatch.

multi-indicator confidence
  Source: callgraph (callers) + strings (referenced literals) + switches
  (dispatch position) + frames (stack shape). The Medium grade is earned when
  >=3 of these agree on a role even though no single decompiled line states it
  outright. Each is a distinct sidecar, so the agreement is genuinely
  independent, not one fact counted three times.
```

### Per-indicator sidecar map

| Indicator | Sidecar(s) | What it confirms |
|---|---|---|
| Body vs. name | `decompiled/*.c` (per-function) | The claimed construct (switch, guard, constant) is literally present |
| Callers vs. role | `callgraph`, `xrefs` | A "validator" has length-shaped callers; a leaf has none |
| Strings vs. behavior | `strings`, `data_tables` | A rejection path references `"request too large"`; a parser references format literals |
| Dispatch position vs. purpose | `switches`, `rtti` | The function sits in a jump table or vtable slot that constrains its role |
| Relocation vs. table | `fixups` | The table's slots are populated by recorded relocations, not coincidental data |
| Raw bytes vs. decompiler | `disasm/*`, `problems` | Where `bad_stack`/`disasm_problem` is flagged, the disassembly is the tiebreaker |

When a later cross-check overturns an earlier claim, the page is corrected in place with a `> **CORRECTION (tag) —**` note, never silently edited — the reasoning trail is part of the evidence.

---

## Full Reproduction Recipe

Every number in this page's tables is regenerable from the sidecars on disk with `jq`. The first block reproduces acquisition and identity; the second regenerates the coverage and failure figures so an auditor can confirm them independently.

```bash
# --- 1. Acquire and pin the identical artifact (see methodology.md for detail) ---
pip download libtpu==0.0.40 --no-deps --python-version 3.14 \
    --only-binary=:all: --platform manylinux_2_31_x86_64
unzip libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64.whl -d libtpu_wheel
stat -c%s libtpu_wheel/libtpu/libtpu.so          # -> 781691048
readelf -n libtpu_wheel/libtpu/libtpu.so | grep -i 'build id'
                                                 # -> 89edbbe81c5b328a958fe628a9f2207d
```

```bash
# --- 2. Regenerate the dashboard from the sidecars. STEM is the target prefix. ---
STEM=libtpu__libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64__libtpu__libtpu.so
cd "$STEM/"

# Function count and demangle rate (the authoritative 884,832 / 822,847).
jq -r '(if type=="array" then . else .functions end) as $f
       | ($f|length) as $n
       | ([$f[]|select(.demangled!=null and .demangled!="" and .demangled!=.name)]|length) as $dm
       | "functions="+($n|tostring)+"  demangled="+($dm|tostring)
         +"  ("+((100*$dm/$n)|floor|tostring)+"%)"' "${STEM}_functions.json"

# Analysis-problem taxonomy (the 7,915 split by type).
jq -r '(if type=="array" then . else .problems end)
       | group_by(.type)
       | map({type:.[0].type, n:length}) | sort_by(-.n)
       | (.[]|"\(.n)\t\(.type)"), ("TOTAL\t"+([.[].n]|add|tostring))' \
      "${STEM}_problems.json"

# Decompilation-failure count (the 516, summed across the 19 shards).
for f in ${STEM}_decompilation_failures_*.json; do
  jq 'if type=="array" then length else (.failures // .records // [])|length end' "$f"
done | paste -sd+ | bc          # -> 516

# Per-kind sidecar sizes (the inventory table).
for k in functions callgraph names frames strings comments switches \
         function_addresses fixups data_tables rtti problems structures \
         segments enums metadata prototypes; do
  printf '%-20s %s\n' "$k" "$(stat -c%s "${STEM}_${k}.json" 2>/dev/null)"
done
```

> **GOTCHA —** reproduction fidelity depends on the IDA *version*. Auto-analysis heuristics, function-boundary recovery, and Hex-Rays output shift between major IDA releases, so a different version may report a slightly different function count or move a function between the decompiled and failed sets. Pin IDA 9.x to match the 884,832 / 516 / 7,915 figures cited here. The binary's bytes are invariant; the analysis of them is not.

> **NOTE —** the `decompilation_failures` and `ctree` sidecars are sharded by address window (19 and 97 files respectively); the recipe sums across all shards. Do not read a single shard and assume it is the whole — the 516 figure only emerges from the full sum, and any per-shard count (the largest holds 476) is a fragment.

### Why the extraction is sharded

The decompilation pass did not run as one monolithic export. It was driven in ~98 windowed re-export passes — each lifting a 10,000-function address window (`off…_lim10000`) and writing its own `ctree`, `decompilation_failures`, and log shard — on top of one base auto-analysis pass and one address-snapshot pass. This is a memory- and crash-isolation measure: a single Hex-Rays refusal or out-of-memory in one window does not abort the whole 884,832-function lift, and a window can be re-run in isolation. The consequence for an auditor is that *every* per-function aggregate (decompiled bodies, ctree coverage, the 516 failures) is a sum over shards, and the `metadata` manifest — written by the first, fast pass — predates all of them. This is the same reason the manifest's `decompiled: 0` counter is stale: the windowed passes that produced the bodies ran after it was written.

---

## Cross-References

- [Methodology](../methodology.md) — the curated parent: acquisition path, tooling overview, the cross-validation procedure narrated, and the legal basis. Start there; this page is its audit-grade ledger.
- [Evidence & Confidence Conventions](../front/evidence-conventions.md) — owns the four-level Confidence scale and the known-extraction-limits contract that this page's failure taxonomy quantifies.
- [Forensics Overview](../forensics/overview.md) — the structural starting point: sections, sizes, and headline counts confirmed directly against the bytes.
- [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md) — how the 33,016 `switches` and the RTTI graph are read; the `fixup↔table` cross-validation pattern in practice.
- [RTTI / Vtable Census](../forensics/rtti-vtable-census.md) — the 160,566-row `rtti` sidecar in depth; the `RTTI↔vtable` agreement pattern's home page.
- [Symbol Namespace Index](symbol-namespace-index.md) — what the 822,847 demangled names partition into; the namespace surface the coverage dashboard summarizes.
