# Symbol Namespace Index

> *All counts on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d`; the wheel/`METADATA`/`__init__` version is `0.0.40` — pin to the build-id). The byte footprints are the summed `size` of resolved functions, not section sizes; other wheels and other extraction passes will differ.*

## Abstract

`libtpu.so` is a 745 MB statically-linked PJRT plugin, and — unusually for a shipped product binary — it carries a full local symbol table (`.symtab`), not just the 226 defined dynamic exports in its 741-entry `.dynsym`. The IDA name sidecar resolves **1,893,205 named addresses** and **884,832 functions**, of which 881,784 carry a name and 3,048 fall back to a synthetic `sub_` label. Because the names are real Itanium-mangled C++ symbols, the binary can be partitioned by its **top-level namespace** — the namespace token that immediately follows `_ZN` in each mangled symbol. That partition is what this page is: a census of *who owns how much of the function population*, by symbol count and by code byte footprint.

The reference frame is the rest of the XLA/TPU stack a reader already knows. `libtpu.so` is the whole compiler-plus-runtime collapsed into one shared object: the MLIR/LLVM compiler core, the XLA HLO middle-end, the TensorFlow op bridge, the Eigen kernel library, the Abseil/protobuf/gRPC support layer, and — uniquely — the closed `asic_sw` driver stack that talks to the silicon. The ranking below makes the proportions concrete: MLIR alone is 31% of all named functions, the four compiler namespaces (mlir, xla, llvm, tensorflow) plus the driver (asic_sw) are over 70%, and everything Google-internal-but-generic (absl, tsl, protobuf, gRPC, tcmalloc) is a single-digit-percent tail.

This page is the **symbol/function counterpart** to [the RTTI namespace census](rtti-namespace-census.md), which counts *type records* (`typeinfo`/vtable entries). The two disagree on purpose and by a wide margin: a namespace with thousands of polymorphic classes (asic_sw) ranks high in the RTTI census, while a namespace that is mostly free functions and monomorphic templates (mlir, llvm) ranks high here. The methodology section below makes the counting rule explicit so the numbers are reproducible.

For a reimplementer, the contract this page satisfies is:

- **The counting rule** — what "owns a symbol" means (the `_ZN<quals><len><ns>` top-level anchor), and why prefix-anywhere and raw-substring counts give different, larger answers.
- **The ranking** — the per-namespace function count, byte footprint, and role for the largest top-level owners, with a *(other)* catch-all so the listed rows plus the residual reach the 884,832-function total.
- **The surfaces** — why library-family substring tallies (absl, Eigen, re2) overshoot the owner counts, and which surface each number describes.

| | |
|---|---|
| **Binary** | `libtpu.so` (wheel `0.0.40`), build-id `89edbbe81c5b328a958fe628a9f2207d` |
| **Symbol table** | `.symtab` present (full local symbols) — not stripped |
| **Named addresses** | 1,893,205 (881,784 function · 880,052 data · 108,629 other · 22,740 code-label) |
| **Functions** | 884,832 total — 881,784 named, 3,048 anonymous (`sub_`) |
| **Resolved function bytes** | 299,035,160 (sum of function `size`) |
| **Main `.text`** | 314,422,404 bytes at `0xe63c000`; all CODE segments 342,157,540 bytes |
| **Dynamic exports / imports** | 226 / 515 (`.dynsym`, 741 entries total incl. null) |

---

## How a Symbol Is Attributed

The whole index turns on one decision: which namespace a mangled symbol *belongs to*. There are three defensible answers, they give three different counts, and conflating them is the single largest source of error in any symbol census.

### The three counting surfaces

```text
mangled symbol:  _ZN4mlir3xla9SomethingINS_4abslE...E   (illustrative)
                  └─┬─┘                  └──┬──┘
            top-level owner            absl appears here as
            = mlir                     a template argument

surface 1  TOP-LEVEL OWNER     token right after _ZN[KVrOR]   -> mlir
surface 2  PREFIX-ANYWHERE     token "4mlir" / "4absl" anywhere in the symbol
surface 3  RAW SUBSTRING       literal "mlir" / "absl" anywhere, mangled or not
```

The Itanium ABI encodes a nested name as `_ZN`, an optional run of CV/ref qualifiers (`K`, `V`, `r`, `O`, `R`), then a length-prefixed component (`3xla`, `4mlir`, `15stream_executor`). The **top-level owner** is that first component. It is the only surface that partitions the population — every named function lands in exactly one bucket, and the buckets sum to the total. That is the surface this page ranks on.

> **GOTCHA —** prefix-anywhere and raw-substring counts do **not** partition. A single `xla` function whose signature mentions `absl::Status`, `std::vector`, and an `Eigen::Tensor` increments the anywhere-count of four namespaces at once. Summing those columns produces a number several times larger than 884,832. They are *participation* metrics — useful for "how pervasive is this vocabulary", useless for "who owns the binary".

### The regex and where it runs

The top-level owner is extracted from the `name` field of the function sidecar with the anchored pattern `^_ZN[KVrOR]?<len><ns>[A-Z0-9]`. The trailing `[A-Z0-9]` rejects accidental prefix collisions (so `3tslSomething` is not confused with a longer token). Functions whose name is not a nested mangled symbol — C symbols, operator/`extern "C"` exports, libc++ `std::__u` wrappers, and the 3,048 `sub_` anonymous functions — fall to dedicated buckets or to `other`. The byte footprint of a bucket is the sum of the `size` field over its members.

> **NOTE —** libc++ symbols mangle as `_ZNSt3__u…` (the `St` substitution for `std`, then the inlined ABI namespace `__u`), so they never collide with a real top-level project namespace and get their own row. The codename driver namespaces (`deepsea`, `jxc`, `pxc`, `vxc`, `gxc`, and their `b0`/`lc`/`fc` sub-variants) are **nested under `asic_sw`**, not top-level; they appear in the `asic_sw` total and are broken out in the detail section, not as separate rows.

---

## The Namespace Census

Top-level-owner function counts and byte footprints, sorted by symbol count. These rows rank the **largest** top-level owners; they do not exhaust the population. The whole-binary totals are **884,832** functions and **299,035,160** bytes; the listed namespaces plus the *(other)* catch-all account for those totals, with the catch-all absorbing every owner that ranks below the cut. Several real mangled `_ZN` owners fall into that residual — notably `dnnl` (oneDNN, ~19.5k functions), `proto2` (the protobuf runtime, ~12k), `grpc_core` (~8.4k), `operations_research` (~6.8k), and `platforms_deepsea` (~6k, the codename-rooted device layer) — each larger than several of the listed lower rows. They are folded into *(other)* here and broken out by the sibling [RTTI namespace census](rtti-namespace-census.md).

| Namespace | Functions | Function Bytes | % of funcs | Role in `libtpu.so` |
|---|---:|---:|---:|---|
| `mlir` | 270,983 | 54,560,105 | 30.6% | MLIR compiler framework — dialects, passes, IR/op infrastructure |
| `asic_sw` | 194,445 | 26,811,387 | 22.0% | Closed TPU driver stack — chip-generation register/queue/DMA layers |
| *(other)* | 138,313 | 85,667,960 | 15.6% | Below-cut mangled owners (`dnnl`, `proto2`, `grpc_core`, …), C symbols, non-`_ZN` exports, vtable thunks |
| `llvm` | 91,060 | 30,705,459 | 10.3% | LLVM core — IR, codegen, target backends used by the JIT |
| `xla` | 62,221 | 42,138,764 | 7.0% | XLA HLO middle-end — passes, layout, runtime, `megascale` collectives |
| `std` (libc++) | 57,643 | 18,571,901 | 6.5% | libc++ `std::__u` containers/algorithms instantiated into the binary |
| `absl` | 27,777 | 8,080,990 | 3.1% | Abseil — `Status`, containers, strings, synchronization, time |
| `tensorflow` | 17,721 | 10,915,236 | 2.0% | TF op bridge — XLA kernels, device compiler, SparseCore ops |
| `Eigen` | 10,419 | 15,400,879 | 1.2% | Eigen tensor/matrix kernels — note the high bytes-per-function |
| `tpu` | 3,438 | 1,339,121 | 0.4% | TPU driver glue — `Tpu*Driver`, chip/core handles, HAL bridge |
| `anon` (`sub_`) | 3,048 | 1,807,907 | 0.3% | Functions with no symbol — synthetic `sub_<addr>` labels |
| `xprof` | 2,605 | 1,606,724 | 0.3% | TPU profiler — trace conversion to XPlane, counter controls |
| `grpc` | 2,265 | 499,369 | 0.3% | gRPC — RPC transport for distributed/multi-host execution |
| `tsl` | 1,855 | 678,318 | 0.2% | TSL (TensorFlow Support Lib) — `AsyncValue`, monitoring, platform |
| `stream_executor` | 542 | 99,857 | 0.1% | StreamExecutor device-abstraction interfaces |
| `re2` | 226 | 109,063 | <0.1% | RE2 regular-expression engine |
| `google::protobuf` | 201 | 25,173 | <0.1% | protobuf C++ runtime under `google::` (the bulk of the runtime is the separate `proto2` owner, in *(other)*; generated messages mangle under their own namespace) |
| `tcmalloc` | 70 | 16,947 | <0.1% | tcmalloc allocator core |

> **QUIRK —** `google::protobuf`, `tcmalloc`, and `re2` rank at the bottom by *owned function count* yet are unmistakably present and heavily used. The owner count understates them because their work shows up elsewhere: the protobuf runtime's bulk is the separate `proto2` owner (~12k functions, folded into *(other)* above) plus generated message code (mangled under each message's own namespace and in the `protodesc_cold` / `.lrodata` data segments), tcmalloc's hot path is in the `google_malloc` / `malloc_hook` code sections by *function* not by namespace, and RE2's value is in a few engine functions invoked from everywhere. Owner count measures authored surface, not runtime weight.

### Reading the ranking

The shape is a compiler with a driver bolted on. The top five owned-function rows — `mlir`, `asic_sw`, `llvm`, `xla`, plus the `other` catch-all — are 85% of all functions. The two genuinely TPU-specific namespaces (`asic_sw`, `tpu`) together own ~198k functions, second only to MLIR; everything else is the standard open-source XLA/TF substrate that the same code would carry on any backend.

The byte column tells a second story that the count column hides. `mlir` owns the most functions but `other` owns the most bytes (85.7 MB) — the catch-all is full of large vendored-library and template-expansion bodies. And `Eigen` is the clearest outlier: 10,419 functions but 15.4 MB, ≈1,478 bytes/function, roughly five times the binary-wide average of ≈338 bytes/function. Eigen's fully-unrolled, vectorized expression templates compile to very large individual functions — a reimplementer budgeting code size for a kernel library should expect this density, not the count.

---

## Full Symbol Surface (All Named Addresses)

The census above counts functions. The name sidecar also resolves data symbols, vtables, typeinfo records, and code labels — 1,893,205 named addresses in total. Partitioned by the same top-level-owner rule, the *full* symbol surface re-ranks the namespaces, because data-heavy and RTTI-heavy namespaces pick up entries that the function-only view misses.

| Namespace | Named addresses (all kinds) | vs function count |
|---|---:|---|
| `mlir` | 286,192 | +15,209 data/typeinfo/labels |
| `asic_sw` | 210,325 | +15,880 — driver RTTI and register tables |
| `llvm` | 120,884 | +29,824 — large static tables |
| `xla` | 65,339 | +3,118 |
| `std` (libc++) | 57,726 | ≈ same (mostly functions) |
| `absl` | 28,189 | +412 |
| `tensorflow` | 20,923 | +3,202 |
| `Eigen` | 10,419 | ≈ same |
| `xprof` | 2,853 | +248 |
| `tpu` | 3,989 | +551 |
| `grpc` | 2,563 | +298 |
| `tsl` | 1,985 | +130 |
| `stream_executor` | 656 | +114 |
| `re2` | 499 | +273 |
| `google::protobuf` | 276 | +75 |
| `tcmalloc` | 84 | +14 |

The kind breakdown of the full surface — 881,784 functions, 880,052 data, 108,629 `other`, 22,740 code-labels — explains the deltas. `llvm` gains the most absolute entries (~30k) because its target tables, instruction-info arrays, and intrinsic descriptors are large static data objects; `asic_sw` gains its second-largest delta from per-register and per-queue data tables plus its dense RTTI.

> **NOTE —** the data-symbol count (880,052) nearly equals the function count (881,784). This is characteristic of a heavily-RTTI'd C++ binary built with full local symbols: roughly one named data object (vtable, typeinfo, static, string-literal label) per function. The [RTTI vtable census](../forensics/rtti-vtable-census.md) drills into that data half.

---

## The Driver Codename Sub-Namespaces

`asic_sw` is the only top-level namespace that is entirely TPU-specific, and internally it is organized by **chip generation codename** rather than by component. These are nested namespaces (`asic_sw::driver::deepsea::<codename>::…`), so they do not appear as top-level rows; their participation counts (mangled token appearing anywhere in a symbol) are listed here to size the per-generation driver surface. Participation over-counts — a `pxc` symbol referenced as a template argument inside a `gxc` function increments both — so treat these as relative magnitudes, not disjoint totals.

| Sub-namespace token | Participation count | Role |
|---|---:|---|
| `deepsea` | 271,973 | The umbrella driver family for all TPU generations |
| `gxc` | 156,011 | A chip-generation core (largest by symbol participation) |
| `glc` | 77,591 | `gxc` sub-core / lane-control variant |
| `vxc` | 68,986 | A chip-generation core |
| `pxc` | 33,363 | A chip-generation core |
| `vfc` | 21,860 | `vxc` sub-core variant |
| `pfc` | 10,647 | `pxc` sub-core variant (with `b0` stepping sub-namespace) |
| `jxc` | 5,183 | A chip-generation core |
| `vlc` | 3,008 | `vxc` sub-core variant |
| `plc` | 1,379 | `pxc` sub-core variant |

The pattern is a `<x>xc` core with `<x>lc` / `<x>fc` lane/fabric sub-cores and, for `pfc`, an explicit silicon-stepping namespace (`b0`). The driver replicates the same factory/queue/register interface family (`*Factory`, `*Interface`, `QueueAllocator`, `IndirectStateFactory`) per generation, which is why the participation counts are so large relative to the asic_sw owned-function total — most of these symbols are template instantiations of a shared interface, re-stamped once per codename. The [RTTI namespace census](rtti-namespace-census.md) resolves these codenames to concrete generations from the typeinfo records.

> **QUIRK —** `deepsea` also exists as a top-level namespace (822 owned symbols), separate from its dominant role nested under `asic_sw` (208,549 `asic_sw…deepsea…` participations). Do not double-count: the 822 are a small standalone surface; the 208k are the driver-family instantiations the asic_sw bucket already contains.

---

## Library-Family Tallies Across the Three Surfaces

The substring tallies a casual `nm | grep` produces for embedded library families overshoot the owner counts this page ranks on, because each measures a different surface. The three surfaces for the three largest vendored libraries:

| Namespace | Top-level owner | Mangled token anywhere | Raw substring (mangled-or-not) |
|---|---:|---:|---:|
| `absl` | 28,189 | 117,015 | 117,804 |
| `Eigen` | 10,419 | 27,577 | 28,818 |
| `re2` | 226 (496 strict) | 591 | 18,572 |

> **NOTE —** for `re2`, the raw-substring count (18,572) catches RE2's API surface *plus* every symbol that merely mentions a regex type. RE2's actual owned function surface is two orders of magnitude smaller — **226** functions, 496 top-level-owner names. The ~18.5k is a participation/substring metric and is not comparable to the per-namespace owner ranking; the page ranks re2 by ownership.

> **NOTE —** `absl` owns **27,777** functions / 28,189 names but participates in ~117k symbols (substring), and `Eigen` owns **10,419** functions / 10,419 names but participates in ~28.6k. Use the owner counts for the "who owns the binary" question; a raw substring count inflates both by roughly 3–4×.

The lesson generalizes: any single number for "how much absl is in the binary" is ambiguous until the surface is named. Abseil's vocabulary (`absl::Status`, `absl::StatusOr`, the flat-hash containers, `absl::Span`) is in the *signature* of a large fraction of all functions, so its participation count (~117k, ~13% of names) dwarfs its owned-function count (~28k, ~3%). Both are true; they answer different questions.

---

## Why This Differs From the RTTI Census

The [RTTI namespace census](rtti-namespace-census.md) and this index count the same binary and rank it differently on purpose. The RTTI census counts **type records** — one entry per polymorphic class with a `typeinfo`/vtable. This index counts **all functions** — every method, free function, template instantiation, and lambda body.

A namespace's two ranks diverge by its *style*:

- **`asic_sw`** ranks very high in RTTI (a deep interface/factory hierarchy = many polymorphic classes) and high here too, but its function-to-type ratio is modest because most classes are thin interfaces.
- **`mlir` / `llvm`** rank top here (enormous free-function and template-method surface) but lower per-type in RTTI, because much of the compiler is monomorphic templates and free functions with no vtable.
- **`Eigen`** is almost invisible in the RTTI census (expression templates are non-polymorphic) yet owns 10k+ functions and 15 MB here.

A reimplementer sizing a component should consult both: this page for *code volume and function count*, the RTTI census for *class/interface surface*. Neither alone is the whole picture.

---

## Cross-References

- [RTTI Namespace Census](rtti-namespace-census.md) — the type-record counterpart; counts polymorphic classes per namespace, not functions
- [RTTI / Vtable Census](../forensics/rtti-vtable-census.md) — the data half of the symbol surface: vtables and typeinfo records
- [LLVM / MLIR Manifest](../forensics/llvm-mlir-manifest.md) — version evidence for the embedded LLVM and MLIR (the two largest compiler namespaces here)
- [Embedded Library Atlas](../forensics/embedded-library-atlas.md) — per-library identification; this page resolves the absl/Eigen/re2 owner-vs-substring surfaces those families span
- [Forensics Overview](../forensics/overview.md) — binary-level facts: build-id, segments, the `.symtab` presence this index depends on
