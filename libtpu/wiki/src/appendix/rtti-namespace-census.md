# RTTI Namespace Census

> *All counts, addresses, and symbol names on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel: a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d` (the wheel/`METADATA`/`__init__` version is `0.0.40`; pin to the build-id). Other wheels will differ in every address.*

## Abstract

`libtpu.so` ships un-stripped with full Itanium-ABI RTTI: every polymorphic class left a `type_info` struct (`_ZTI`), a type-name string (`_ZTS`), and — if concrete — a vtable group (`_ZTV`). The [RTTI / Vtable Census](../forensics/rtti-vtable-census.md) establishes the headline counts (**160,351** records: `_ZTI` 60,457 · `_ZTV` 39,244 · `_ZTS` 60,650, summing exactly to 160,351) and ranks the dominant *hierarchies* by width and depth. This appendix asks a different question of the same 160,351 records: **which C++ namespace owns the type system?** It buckets the 60,457 distinct `type_info` structs by their leading namespace token and ranks the libraries by how many polymorphic classes each contributes.

The answer is a two-empire split. MLIR (`mlir::`, 13,091 typeinfos) and the TPU driver stack (`asic_sw::`, 11,379 typeinfos) together own **40%** of every polymorphic class in the binary — MLIR because every registered op, pattern, pass, and dialect interface is a distinct C++ type, and `asic_sw::` because the per-codename / per-lane-cluster hardware driver instantiates a separate class for every chip generation × functional block. Behind them sit the framework cores (`tensorflow::` 3,108, `xla::` 3,036, `llvm::` 2,940) and a long tail of vendored support libraries (`dnnl::` 1,888, `std::` 1,787, `grpc_core::` 1,502). The TPU *codename* namespaces a reader might expect to see at the top — `jellyfish`, `pufferfish`, `viperfish`, `ghostlite`, `sparse_core` — are **not** top-level namespaces at all; they are sub-namespaces nested inside `xla::`, `mlir::`, and `platforms_deepsea::`, and their classes are counted under those parents.

The census can be computed two ways, and the two disagree — the single most important caveat on this page. Counting by **leading typeinfo namespace** (the `_ZTIN<len><name>` prefix — "this class lives in namespace X") is the metric used here: it answers "how many polymorphic classes does X define." Counting by **top token of the demangled name** over-counts header-only template libraries — `absl::StatusOr<xla::Foo>` and `Eigen::Matrix<...>` appear as the *return type* or *template argument* of thousands of typeinfos whose owning class is in some other namespace. Where the two diverge by an order of magnitude (`absl`, `Eigen`, `xla`), a `> **NOTE —**` below records both numbers and explains the gap.

For reproduction — to rebuild this census from the binary — the contract is:

- **The bucketing rule:** a `type_info` struct's owning namespace is the leading `N<len><name>` token of its `_ZTI` mangled symbol; a `_ZTI` with no leading `N` is a global-scope or compound (pointer / function / template-substitution) type.
- **The denominator:** the 60,457 `_ZTI` structs, *not* the full 160,351 (which triple-counts each class as `_ZTI`+`_ZTV`+`_ZTS`). 46,078 of the 60,457 carry a leading namespace; 14,379 are global-scope or compound types.
- **The template-wrapper trap:** never bucket by the demangled top token, or `absl`/`Eigen`/`std` template wrappers inflate libraries that own almost no polymorphic classes of their own.

| | |
|---|---|
| **Denominator** | 60,457 `_ZTI` (typeinfo) structs |
| **Namespaced `_ZTI`** | 46,078 (leading `N` token) |
| **Global / compound `_ZTI`** | 14,379 (`_ZTIP…`, `_ZTIF…`, `_ZTI1X`, template substitutions) |
| **Top namespace** | `mlir::` — 13,091 typeinfos (~21.6% of all `_ZTI`) |
| **Two-empire share** | `mlir::` + `asic_sw::` = 24,470 = 40.5% of `_ZTI` |
| **Bucketing key** | leading `_ZTIN<len><name>` mangled prefix |

---

## The Census Table

The 60,457 `type_info` structs bucketed by leading namespace, ranked by typeinfo count. "Typeinfos" is the count of `_ZTI` structs whose mangled symbol begins `_ZTIN<len><namespace>`. "~Classes" is the same number read as a class population — a `_ZTI` struct *is* one polymorphic class identity, so the two are equal except where template instantiations of one logical class inflate the count (called out per row). "Dominant hierarchy" is the widest/deepest tree rooted in that namespace, with its root `_ZTI` struct VA. Counts are byte-exact greps over the RTTI sidecar; hierarchy widths/depths carry the parent census's confidence.

| Namespace | Typeinfos | ~Classes | Dominant hierarchy (width / depth, root `_ZTI`) | Confidence |
|---|---:|---:|---|---|
| `mlir` | 13,091 | ~13,000 | `mlir::Pattern` (6,142 / 9, `0x21cea698`); `OperationName::InterfaceConcept` (6,052 / 2, `0x217b1000`) | CERTAIN |
| `asic_sw` | 11,379 | ~11,400 | `…::profiler::EventControlInterface` (821 / 1, `0x2175c798`) | CERTAIN |
| `tensorflow` | 3,108 | ~3,100 | `tensorflow::OpKernel` (1,122 / 4, `0x218114c8`) | CERTAIN |
| `xla` | 3,036 | ~3,000 | `xla::HloInstruction` (68 / 4, `0x21d2ce88`); `HloPassInterface` (361 / 4) | CERTAIN |
| `llvm` | 2,940 | ~2,900 | `llvm::Pass` (628 / 5, `0x21ced3b8`) | CERTAIN |
| `(anonymous)` | 2,352 | ~2,350 | per-TU local classes (`_GLOBAL__N_…`) — no single tree | CERTAIN |
| `dnnl` | 1,888 | ~1,900 | `dnnl::impl::c_compatible` (2,069 / 6, `0x21b69258`) | HIGH |
| `std` | 1,787 | ~1,000 | `std::__u` container / iostream plumbing — many template insts | HIGH |
| `grpc_core` | 1,502 | ~1,500 | `grpc_core::PolymorphicRefCount` (442 / 6, `0x21ca0128`) | CERTAIN |
| `platforms_deepsea` | 576 | ~580 | `…::jellyfish::isa::Encoder` (19 / 3, `0x21cb6a20`) | CERTAIN |
| `operations_research` | 483 | ~610 | `…::math_opt::SolverInterface` tree (root `0x217fa708`) | HIGH |
| `grpc` | 430 | ~430 | `grpc::Service` (44 / 10, `0x216162d8`) — deepest chain in binary | CERTAIN |
| `tpu` | 315 | ~410 | `tpu::TpuCodec` (5 / 1, `0x21d35858`) | HIGH |
| `proto2` | 152 | ~8,000 | `proto2::MessageLite` (8,013 / 3, `0x22034138`) — see GOTCHA | CERTAIN |
| `riegeli` | 136 | ~140 | `riegeli::Object` (114 / 6, `0x220291a8`) | CERTAIN |
| `tsl` | 128 | ~130 | `tsl::core::RefCounted` (140 / 4, `0x215f9b18`) | CERTAIN |
| `stream_executor` | 58 | ~58 | `stream_executor::…` device/stream interfaces (`0x215fb6f0`) | HIGH |
| `absl` | 33 | ~33 | `absl::Duration` & status internals (`0x215fd610`) — see note below | CERTAIN |
| `Xbyak` | 4 | ~551 | `Xbyak::CodeArray` (551 / 5, `0x21b6d738`) — see GOTCHA | HIGH |
| `Eigen` | 4 | ~4 | `Eigen::ThreadPoolInterface` (`0x2163bd98`) — see note below | CERTAIN |

> **NOTE —** the table rows sum to ~43,500; with the long tail of single-digit namespaces (boringssl, re2, nsync, farmhash, snappy, zlibwrapper, …) the namespaced total is 46,078, and the remaining 14,379 `_ZTI` are global-scope classes and compound types (`_ZTIPF…` pointer-to-function, `_ZTIN…` template substitutions whose substitution resolves below the leading token). Together: 60,457.

> **GOTCHA — typeinfo count is not class-tree size.** Two rows show the trap in opposite directions. `proto2` owns only **152** typeinfo structs, but `proto2::MessageLite` roots an **8,013**-class tree — because the 8,000-odd generated message classes (`xla::HloProto`, `tensorflow::GraphDef`, …) live in *their own* namespaces and inherit *from* `proto2::Message`; they count under `xla`/`tensorflow`, not `proto2`. Conversely `Xbyak` owns 4 leading-namespace typeinfos but `Xbyak::CodeArray` roots 551 descendants — the oneDNN JIT emitters that derive from it. **Bucket-by-namespace counts where a class is *defined*; hierarchy width counts where it is *used*.** The two never coincide for a base class whose subclasses live elsewhere.

---

## mlir — the largest type empire (13,091)

### Why MLIR dominates

MLIR contributes more polymorphic classes than any other namespace because MLIR's extensibility model *is* C++ type proliferation. Every registered operation, every rewrite pattern, every pass, and every dialect interface materializes as a distinct concrete class with its own `type_info`. The two widest trees in the entire binary are both MLIR:

- **`mlir::Pattern`** (`_ZTI` `0x21cea698`, 6,142 descendants, depth 9) — the rewrite/conversion/lowering pattern forest. `Pattern → RewritePattern → ConversionPattern → ConvertToLLVMPattern → ConvertOpToLLVMPattern → …`, with the TPU SparseCore lowering chain (`SCConvertOpToLLVMPattern → StreamDmaOpLoweringBase → LinearStreamStartOpLowering`) as the deepest branch.
- **`mlir::OperationName::InterfaceConcept`** (`_ZTI` `0x217b1000`, 6,052 descendants, depth 2) — the type-erased op-interface dispatch: `InterfaceConcept → RegisteredOperationName → Model<Op>` per registered op. This is the [dispatch-table taxonomy](../forensics/dispatch-table-taxonomy.md)'s size-23 `RegisteredOperationName::Model<…>` fingerprint.

> **QUIRK — `mlir::Operation` is not in this census, and not because it was missed.** `mlir::Operation`, `mlir::Value`, and `mlir::Block` are **non-polymorphic** — they carry no vtable and no `type_info`, so they emit no `_ZTI` and are invisible to an RTTI walk. MLIR op behaviour is dispatched through the two trees above (the interface `Model<Op>` and the rewrite `Pattern`), not through virtual methods on `Operation`. A reimplementer who expects a polymorphic `Operation` base will find none.

The concrete `*Op` C++ classes that *do* carry typeinfo (283 total) are the dialects whose ops double as C++ value types: `mlir::hlo` (102), `mlir::stablehlo` (85), `mlir::TF` (47), `mlir::tfg` (20), `quant`/`linalg` (10 each). Pass and dialect plumbing fills the rest: `mlir::Pass` (606 / 7, `0x21c2c450`), `mlir::Dialect` (67 dialects, `0x21cea490`), `mlir::DialectInterface` (100, `0x21cea480`).

---

## asic_sw — the TPU hardware driver (11,379)

`asic_sw::` is the low-level TPU device driver, and it is the second-largest namespace for a structural reason: it instantiates a separate concrete class for every **chip generation × functional block × lane cluster**. The naming is a Cartesian product. The deepest nesting seen — `asic_sw::driver::deepsea::pxc::pfc::b0::TensorCoreCoreFactory` — encodes a chip family (`pxc`/`vxc`/`gxc`/`jxc`), a core type (`pfc`/`plc`/`vfc`/`vlc`/`gfc`/`glc`/`dfc`/`jfc`), and a silicon revision (`b0`), and there is one such class per combination.

The dominant tree is `asic_sw::driver::deepsea::profiler::EventControlInterface` (`_ZTI` `0x2175c798`, 821 descendants, depth 1 — all direct leaves), the per-lane-cluster performance-counter event-control hierarchy, partitioned exactly by lane cluster: `gxc/gfc` 320, `vxc/vfc` 264, `gxc/glc` 130, `pxc/pfc` 63, `vxc/vlc` 24, `pxc/plc` 20. A representative leaf typeinfo is `asic_sw::driver::DmaBuffer` (`_ZTI` `0x215ff0f8`).

> **NOTE — `asic_sw::` is the on-device runtime, distinct from `xla::`/`mlir::` which compile *for* the device.** The driver instantiates per-silicon classes; the compiler emits target-generic IR and selects the codename late. The two namespaces barely share base classes — the boundary between them is the `TpuHal`/`TpuCodec` interface family (`tpu::`, below).

---

## tensorflow / xla / llvm — the framework cores

These three namespaces are the compiler and runtime proper, in the ~3,000-typeinfo band.

`tensorflow` (3,108) is rooted in `tensorflow::OpKernel` (`_ZTI` `0x218114c8`, 1,122 descendants, depth 4) — the TF op-kernel base behind the TPU embedding and XLA bridge kernels. A representative leaf, `tensorflow::(anonymous)::TPUEmbeddingActivations` (`_ZTI` `0x215f81f0`), shows how much of the TF surface here is TPU-specific.

`xla` (3,036) splits across two well-known trees: `xla::HloInstruction` (`_ZTI` `0x21d2ce88`, 68 descendants, depth 4 — 37 direct, 9 internal, 59 leaf) for the IR node hierarchy, and `xla::HloPassInterface` (`0x217f4428`, 361 descendants, depth 4) for the compiler-pass interface. The TPU codegen emitter `xla::jellyfish::OpEmitter` (`0x219b0080`, 66) and the SparseCore offload factory `xla::tpu::sparse_core::collective::OffloadFactory` (`0x218fffd8`, 60 / 7) live under `xla::` as nested codename sub-namespaces.

`llvm` (2,940) is the embedded LLVM backend — and it is full backends, not a slice. `llvm::Pass` (`_ZTI` `0x21ced3b8`, 628 descendants, depth 5) is the deepest codegen structure; under it `FunctionPass` (506) → `MachineFunctionPass` (351) carries AMDGPU, PPC, ARM, AArch64, X86 *and* TPU MachineFunctionPasses. The Attributor's parallel CRTP trees (`llvm::AbstractState` 329 / 9, `llvm::IRPosition` 299 / 8, `llvm::AADepGraphNode` 299 / 8) are the deepest template chains after `grpc::Service`.

> **NOTE —** `xla` has two counts. Counting by **leading typeinfo namespace** (`_ZTIN3xla…`) gives **3,036** — the number of polymorphic classes `xla` actually *defines*, and the figure this page ranks on. Counting the top token of each *demangled* name gives **5,291**, but that credits `xla` for every `absl::StatusOr<xla::…>` and `std::unique_ptr<xla::…>` wrapper whose owning class is in `absl`/`std`. The 5,291 figure is "xla appears anywhere as the first token" — an upper bound, not class ownership.

---

## The vendored support tail

Below the framework cores sit the statically-linked third-party libraries. They contribute substantial typeinfo populations but root few central hierarchies.

| Namespace | Typeinfos | Role | Representative root |
|---|---:|---|---|
| `dnnl` | 1,888 | oneDNN primitive descriptors / JIT primitives | `dnnl::impl::c_compatible` `0x21b69258` (2,069 / 6) |
| `std` | 1,787 | libc++ `std::__u` containers, iostreams, exceptions | `std::exception`, allocator/iterator template insts |
| `grpc_core` | 1,502 | gRPC ref-counted core (channels, LB, credentials) | `grpc_core::PolymorphicRefCount` `0x21ca0128` (442 / 6) |
| `platforms_deepsea` | 576 | TPU ISA bundle encoders (per-family/per-core) | `…::jellyfish::isa::Encoder` `0x21cb6a20` (19 / 3) |
| `operations_research` | 483 | OR-Tools CP-SAT / math-opt solvers | `…::math_opt::SolverInterface` `0x217fa708` |
| `grpc` | 430 | gRPC generated services | `grpc::Service` `0x216162d8` (44 / **10** — deepest) |
| `tpu` | 315 | public TPU codec / API interface | `tpu::TpuCodec` `0x21d35858` (5 / 1) |
| `proto2` | 152 | protobuf message runtime base | `proto2::MessageLite` `0x22034138` (8,013 / 3) |
| `riegeli` | 136 | record-IO object base (reader/writer/codec) | `riegeli::Object` `0x220291a8` (114 / 6) |
| `tsl` | 128 | TSL/TF ref-counted base (devices, callbacks) | `tsl::core::RefCounted` `0x215f9b18` (140 / 4) |
| `stream_executor` | 58 | device/stream abstraction layer | `stream_executor::RuntimeAbiVersionManager` `0x215fb6f0` |
| `absl` | 33 | Abseil — almost no polymorphic classes | `absl::Duration` `0x215fd610` |
| `Eigen` | 4 | Eigen — header-only templates | `Eigen::ThreadPoolInterface` `0x2163bd98` |

`grpc::Service` deserves a note: at depth 10 it is the **single deepest inheritance chain in the binary**, bottoming out in the generated `tpu_debugger` service. The `operations_research` row is the most fragile count — its leading-prefix `_ZTIN19operations_research` greps to 483, but including local classes (`_ZTIZN19operations_research…$_0`) and the `math_opt` sub-namespace lambdas pushes the "anywhere" count to ~610.

> **NOTE —** `absl` and `Eigen` are overwhelmingly **header-only template** libraries: `absl::StatusOr<T>`, `absl::flat_hash_map<K,V>`, `Eigen::Matrix<…>` instantiate as the *outer* type of thousands of typeinfos, but the *owning polymorphic class* is almost always elsewhere. Counting by leading typeinfo namespace gives `absl` **33** and `Eigen` **4** — the genuinely polymorphic classes each defines (e.g. `Eigen::ThreadPoolInterface`). Demangled-top-token counting gives **509** and **406** respectively; those measure template-wrapper prevalence, a different and legitimate metric, but not class ownership.

---

## The TPU codename sub-namespaces

A reader hunting for `jellyfish`, `pufferfish`, `viperfish`, `ghostlite`, or `sparse_core` as top-level namespaces will not find them in the census table — and that absence is itself a finding. These are the TPU generation/subsystem codenames, and they appear **only as nested sub-namespaces** inside the framework and driver empires. Counting every `_ZTI` whose mangled name contains the token (regardless of nesting depth):

| Codename | `_ZTI` occurrences | Nesting parents (where it lives) | Confidence |
|---|---:|---|---|
| `jellyfish` | 2,996 | `xla::jellyfish` (667), `platforms_deepsea::jellyfish` (576), `asic_sw::…::jfc` | CERTAIN |
| `sparse_core` | 3,155 | `mlir::sparse_core` (1,689), `xla::tpu::sparse_core` (553), `platforms_performance_deepsea::sparse_core` | CERTAIN |
| `pufferfish` | 29 | `xla::pufferfish`, `asic_sw::…::pfc::Pufferfish*` | HIGH |
| `viperfish` | 19 | `xla::viperfish`, `xla::tpu::sparse_core::isa_emitter::viperfish` | HIGH |
| `ghostlite` | 17 | `xla::ghostlite`, `xla::tpu::sparse_core::isa_emitter::ghostlite` | HIGH |

> **NOTE —** the codename counts above are **substring (anywhere-in-symbol) matches**, not leading-namespace buckets, and they therefore **overlap** the `xla`/`mlir`/`platforms_deepsea` rows of the census table rather than adding to them. The `jellyfish` 667 inside `xla::` is already counted in the `xla` 3,036; the `sparse_core` 1,689 inside `mlir::` is already inside the `mlir` 13,091. Do **not** sum the codename rows into the namespace total — they are a cross-cut view, presented so a reimplementer can locate codename-specific code, not a partition. The asymmetry (`sparse_core` 3,155 vs `pufferfish` 29) reflects that SparseCore has a full MLIR dialect + lowering pipeline + ISA emitter, whereas the older chip codenames survive only as a handful of driver/emitter leaf classes.

> **QUIRK — the codename appears in two roles.** Lowercase (`jellyfish`, `viperfish`) is a *namespace* token; capitalized (`PufferfishDeviceScanner`, `ViperfishTensorCoreEmitter`, `GhostliteTensorCoreEmitter`) is a *class-name* token in a generation-agnostic namespace (`xla::`, `asic_sw::…::pfc`). Both forms encode the same target generation; a reimplementer mapping codenames to silicon must match both the namespace and the class-name spelling.

---

## Reproduction

The census is byte-exact and rebuildable from the RTTI sidecar with a dozen greps. The denominator is the 60,457 `_ZTI` structs (each `_ZTI` mangled record's `string_addr` *is* the typeinfo struct VA — verified against the parent census: `xla::HloInstruction` `_ZTI` resolves to `0x21d2ce88`, matching the hierarchy table).

```text
# total records (160,351) and flavor split (nm libtpu.so | rg -c '_ZTI' etc.):
count mangled ^_ZTI                 ->  60,457   (typeinfo structs = denominator)
count mangled ^_ZTV                 ->  39,244   (vtable groups)
count mangled ^_ZTS                 ->  60,650   (type-name strings)
# 60,457 + 39,244 + 60,650              = 160,351 (records)

# per-namespace bucket (leading prefix; <len> is the namespace name length):
count mangled ^_ZTIN3xla            ->   3,036
count mangled ^_ZTIN4mlir           ->  13,091
count mangled ^_ZTIN7asic_sw        ->  11,379
count mangled ^_ZTIN10tensorflow    ->   3,108
count mangled ^_ZTIN4llvm           ->   2,940
count mangled ^_ZTIN4dnnl           ->   1,888
count mangled ^_ZTIN9grpc_core      ->   1,502
count mangled ^_ZTIN6proto2         ->     152

# total namespaced vs global/compound:
count mangled ^_ZTIN                 ->  46,078   (leading-namespace)
60,457 - 46,078                      =  14,379   (global / pointer / function / substitution)
```

> **GOTCHA — get the `<len>` prefix right.** Itanium mangling encodes each namespace component as its byte length followed by the name: `xla` is `3xla`, `mlir` is `4mlir`, `operations_research` is `19operations_research` (19, not 20 — counting the underscore but not a leading digit). A grep for `_ZTIN20operations_research` returns **zero**; the correct length-19 prefix returns 483. An off-by-one in the length token silently drops an entire namespace from the census.

## Cross-References

- [RTTI / Vtable Census](../forensics/rtti-vtable-census.md) — the curated parent: record taxonomy, `type_info` flavors, the top-30 hierarchies by width/depth, edge-recovery method. This appendix is the namespace-axis slice of that data.
- [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md) — each dominant hierarchy here maps to a dispatch class there (MLIR `Model<Op>`, llvm vtables, proto2, grpc), tying namespace ownership to vtable-slot fingerprints.
- [Symbol Namespace Index](symbol-namespace-index.md) — the broader symbol-population map (all symbols, not just RTTI); complementary axis, parallel ranking.
- [Dispatch-Table Taxonomy (full)](dispatch-table-taxonomy-full.md) — the exhaustive per-class dispatch-table listing behind the taxonomy summary.
