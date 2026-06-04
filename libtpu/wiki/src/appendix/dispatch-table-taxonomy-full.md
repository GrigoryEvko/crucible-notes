# Dispatch-Table Taxonomy (Full Census)

> *All addresses, counts, and section names on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel, build-id `89edbbe81c5b328a958fe628a9f2207d` (the wheel/`METADATA`/`__init__` version is `0.0.40`; pin to the build-id, which is unambiguous). Other builds will differ. This is the exhaustive machine-style companion to the narrative [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md); for the "why" of each class, read the parent.*

## Abstract

`libtpu.so` is a 745 MB PJRT plugin built by statically linking XLA, MLIR, LLVM, TensorFlow, the TPU `asic_sw` ISA backend, oneDNN, abseil, protobuf, gRPC, and a long tail of host libraries into one position-independent shared object. Almost every C++ class in that union is polymorphic, so the binary carries an enormous population of dispatch structures: **40,313 function-pointer tables** (Itanium-ABI vtables, MLIR Op-Model arrays, type-erasure pools, PMU C tables) holding **516,323 function pointers**, plus a structurally separate **33,016 compiled switch jump tables** holding **4,673,757 case entries**. The parent page sorts the function-pointer tables into 19 structural classes and tells the story of each. This appendix is the full reference: per-class counts, the section a class lives in, its stride/entry-kind signature, the largest individual tables *with addresses*, and the switch jump-table size distribution.

The function-pointer tables are not filled in the file image. Of 1,069,603 relocations, 924,033 live in `.data.rel.ro`; each vtable slot is zero on disk and the loader writes the real target via an `R_X86_64_RELATIVE` reduce at load. IDA's table sidecar already resolved each slot through its relocation, so the per-table entry counts and first-symbol identities below are read off the resolved targets, not the file bytes. The single most important structural fact is the **39,244 / 40,313 ratio**: the binary carries 39,244 `_ZTV` vtable groups (nm-verified), so the dispatch population is overwhelmingly relocated vtables, with roughly 1,069 non-vtable dispatch structures (abseil policy thunks, libpfm4 C tables, member-pointer arrays) making up the rest.

This page is a census, not a reimplementation guide. It does not re-explain the Itanium vtable ABI, the RTTI→vtable binding chain, or the per-slot semantics of any hierarchy — those belong to [RTTI / Vtable Census](../forensics/rtti-vtable-census.md) and [Polymorphic Entry Points](../forensics/polymorphic-entry-points.md). What it provides is the complete enumeration a tool author needs to *index* the binary's dispatch surface: which class a table belongs to, how big it is, and where the outliers are.

| | |
|---|---|
| **Function-pointer tables** | 40,313 / 516,323 entries |
| **Switch jump tables** | 33,016 / 4,673,757 cases |
| **RTTI records** | 160,351 (`_ZTV` vtable groups = 39,244) |
| **Relocations** | 1,069,603 (924,033 in `.data.rel.ro`) |
| **Section split (tables)** | 38,664 `.data.rel.ro` / 1,442 `.data` / 207 `.rodata` |
| **Largest single table** | `0x223393a0` — 2,595 entries (`UniqueFunctionBase`, `.data`) |
| **Largest single switch** | `0x11cc4900` — 40,813 cases (`AMDGPUMCCodeEmitter::getBinaryCodeForInstr`) |
| **Classes** | 19 (function-pointer) + 13 (switch), 99.6% coverage |

### What this census fixes

A tool that walks `libtpu.so`'s dispatch surface must:

- **Distinguish the two populations.** The 40,313 function-pointer tables and the 33,016 switch tables are disjoint structural objects living in different sections. Conflating them — as an early pass that reported "40,313 dispatch tables" without splitting did — double-counts nothing but mislabels everything.
- **Key the MLIR Op-Model class on a symbol, not a size.** Size 23 is the Op-Model fingerprint, but 79 unrelated 23-method vtables coincide in arity. The classifier must key on the `RegisteredOperationName::Model<Op>` symbol.
- **Treat soft class boundaries as classifier-dependent.** The hard structural anchors (section split, size-23 count, largest tables, switch maximum) reproduce to the digit. The per-class *counts* depend on how thunk-prefixed (`_ZThn`/`_ZTv`), local-scope (`_ZZ`), and `std::` symbols are normalized; the figures below carry confidence labels accordingly.

---

## Census Table — Function-Pointer Dispatch Classes

The 19 classes cover 99.6% of the 40,313 tables; 157 (0.4%) are IDA-auto-named (`sub_`/`nullsub_`) or pure-virtual-only tables with no recoverable owner. "Stride" is uniform 8 bytes (`void(*)()` slots) for every class — the differentiator is the **entry kind** (relocated code pointer, ICF-folded forwarder, ABI thunk, or C function pointer), given per class in the detail sections. "Top table" is the largest individual table whose first resolved symbol places it in the class.

| ID | Class | Count | % | Section | Top table (addr — entries) | Confidence |
|----|-------|------:|------:|---------|-----------------------------|------------|
| E | TPU ISA encoder/clone vtables (`asic_sw`) | 9,932 | 24.6% | `.data.rel.ro` | `0x21e0d0a0` — 674 | HIGH |
| A | MLIR Op `Model<>` arrays (size-23) | 6,085 | 15.1% | `.data.rel.ro` | size-23 fingerprint — 23 | HIGH |
| F | `mlir::` vtables (non-Op-Model) | 4,270 | 10.6% | `.data.rel.ro` | `0x21c2d030` — 108 | MEDIUM |
| I | `llvm::` vtables (TargetLowering, passes) | 2,611 | 6.5% | `.data.rel.ro` | `0x2186b0c0` — 336 | MEDIUM |
| D | oneDNN / Xbyak JIT vtables | 2,289 | 5.7% | `.data.rel.ro` | `0x21b6e048` — 29 | MEDIUM |
| G | `xla::` / `stablehlo::` vtables | 2,154 | 5.3% | `.data.rel.ro` | `0x21cc6358` — 266 | HIGH |
| H | `tensorflow::` / `tsl::` vtables | 2,153 | 5.3% | `.data.rel.ro` | `0xa304280` — 89 | MEDIUM |
| P | abseil hash-container policy thunks | 2,066 | 5.1% | `.data.rel.ro` | `0x21c1d590` — 447 | HIGH |
| O | long-tail named-namespace vtables | 1,866 | 4.6% | `.data.rel.ro` | `0x21865d20` — 256 | LOW |
| K | libc++ `std::` thunks | 1,802 | 4.5% | `.data.rel.ro` | `0x21c0c0c8` — 231 | MEDIUM |
| N | TPU runtime / profiler vtables | 1,130 | 2.8% | `.data.rel.ro` | `0x21ca92b0` — 61 | MEDIUM |
| M | gRPC / `grpc_core` vtables | 931 | 2.3% | `.data.rel.ro` | `0x21f874e0` — 30 | MEDIUM |
| C | libpfm4 PMU event tables | 833 | 2.1% | `.data` (mut.) | `0x222684d8` — 10 | HIGH |
| L | protobuf message/descriptor vtables | 712 | 1.8% | `.data.rel.ro` | `0x220387e8` — 117 | MEDIUM |
| Z1 | anonymous-namespace static helpers | 698 | 1.7% | `.data.rel.ro` | `0xa30af90` — 165 | LOW |
| B | LLVM `UniqueFunctionBase` pools | 591 | 1.5% | `.data` (mut.) | `0x223393a0` — 2,595 | HIGH |
| R | C-runtime / Rust I/O & codec tables | 33 | 0.1% | `.data.rel.ro` | `0x21fbfee8` — 30 | MEDIUM |
| Q | abseil `AnyInvocable` invoker thunks | 2 | 0.0% | `.rodata` | `0xa30c788` — 4 | HIGH |
| Z | unclassified (IDA auto-named) | 157 | 0.4% | `.data.rel.ro` | `0x21c3c558` — 345 | LOW |

> **CORRECTION (DTT-FULL-1) —** the parent taxonomy reports Class B (`UniqueFunctionBase`) at 589 tables / 11,530 entries. Re-deriving directly from the table sidecar, the strict "first resolved symbol is `UniqueFunctionBase`" criterion yields 586 tables / 11,516 entries; broadening to include the `unique_function` template spelling yields 591 / 11,591. The figure is therefore **586–591 depending on the symbol-prefix criterion**; the 2,595-entry top table at `0x223393a0` and the `.data` residency are unchanged. The discrepancy is a classifier boundary (two `.data.rel.ro` and one `.rodata` table whose first slot resolves to a non-`UniqueFunctionBase` base before the pool body), not a count error in the source data.

> **NOTE —** the Op-Model size-23 fingerprint reproduces exactly: 6,129 tables have 23 entries; 6,050 contain a `RegisteredOperationName::Model` slot; 79 are 23-method vtables that merely coincide in arity (e.g. `xla::MegaScalePjRtDevice`, the 23-slot `PjRtDevice` family). Class A is keyed on the Model symbol, so it does not mis-bucket the 79; its 6,085 count is the Model population across all sizes, not just size-23.

---

## E — TPU ISA Encoder / Clone Vtables (`asic_sw`)

### Signature

The largest class by table count (9,932; 24.6%) and the structural heart of the TPU code generator. Each table is a vtable for an `asic_sw::deepsea::<cluster>::isa::*` instruction encoder, where `<cluster>` partitions by silicon generation and lane cluster. Entry kind: relocated code pointers into the per-cluster encode/clone bodies; medium stride-6 (the typical encoder declares ~6 virtual methods), with a heavy tail of wide `TensorCoreVectorAluCompact`-family tables.

The population partitions by lane cluster. The two `gxc` clusters (`gfc` ≈ 2,290, `glc` ≈ 2,270) dominate and are near-symmetric, which suggests a paired encode/clone vtable per ISA opcode; `vxc` and `pxc` follow, with `jxc` a vestigial pair. The near-symmetry is the central observation for a reimplementer: the encoder count is roughly 2× the opcode count per generation, not 1×.

### Largest Tables

```text
addr          entries  first symbol (TensorCoreVectorAluCompact family)
0x21e0d0a0    674      asic_sw::deepsea::gxc::gfc::isa::TensorCoreVectorAluCompact
0x21dc35a8    623      asic_sw::deepsea::gxc::glc::isa::TensorCoreVectorAluCompact
0x21e03370    620      asic_sw::deepsea::gxc::gfc::isa::TensorCoreVectorAluCompact
0x21df9ce0    620      asic_sw::deepsea::gxc::gfc::isa::TensorCoreVectorAluCompact
0x21df04c0    620      asic_sw::deepsea::gxc::gfc::isa::TensorCoreVectorAluCompact
0x21dba8a8    566      asic_sw::deepsea::gxc::glc::isa::TensorCoreVectorAluCompact
0x21db1f30    566      asic_sw::deepsea::gxc::glc::isa::TensorCoreVectorAluCompact
0x21da9788    566      asic_sw::deepsea::gxc::glc::isa::TensorCoreVectorAluCompact
0x21d816c8    403      asic_sw::deepsea::vxc::isa::TensorCoreVectorAluCompact
0x21d7b5b0    383      asic_sw::deepsea::vxc::isa::TensorCoreVectorAluCompact
```

The 620/623/674 cluster of wide tables is the `TensorCoreVectorAluCompact` encoder — the vector-ALU instruction class with the largest opcode-variant fan-out. These are the concrete per-generation fills of the `IsaEmitter` 152-slot pure interface documented in [Polymorphic Entry Points](../forensics/polymorphic-entry-points.md).

---

## A — MLIR Op `Model<>` Arrays

### Signature

6,085 tables (15.1%). Each is a `mlir::RegisteredOperationName::Model<Op>` array — the type-erased op-interface dispatch that MLIR materializes once per registered op. The canonical instance is 23 slots (`OperationName::Impl::~Impl`, `Model::~Model`, then 21 interface methods: `foldHook`, `getCanonicalizationPatterns`, `hasTrait`, parse/print/verify, inherent-attribute accessors, and 9 property-management slots). Entry kind: heavily ICF-folded — most interface-method slots point at a shared canonical body via a 5-byte `jmp` forwarder, so thousands of distinct ops share one physical `isCompatibleReturnTypes` body.

> **QUIRK —** the size-23 fingerprint is necessary but not sufficient. 6,129 tables have exactly 23 entries; 79 of them are ordinary 23-method vtables, not Op-Models. A census that buckets on size alone over-counts Class A by 79 and silently mis-files the `PjRtDevice` family. Key on the `RegisteredOperationName::Model` symbol; size 23 is a hint, not the test.

### Largest Tables

```text
addr          entries  note
0xa305350     113      tensorflow::OpOrArgNameMapper (a 23-fingerprint-adjacent .rodata case)
size-23 ×6050          the canonical Model<Op> ABI — the dominant arity
```

Class A is unusual in being a near-uniform population: its median size is 23 and its variance is small, because every registered op materializes the same ABI. The class is large by *count* (one per op) but cheap in distinct code (ICF collapses the interface bodies). See [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md) for how ICF populates this class.

---

## F — `mlir::` Vtables (non-Op-Model)

### Signature

4,270 tables (10.6%): every `mlir::` polymorphic object that is *not* an Op-Model array — passes, dialects, interface concepts, attribute/type storage, rewrite patterns. Entry kind: relocated code pointers, with a significant `_ZThn`/`_ZTv` thunk fraction in the multiply-inheriting pass and pattern classes. Median stride-8.

### Largest Tables

```text
addr          entries  note
0x21c2d030    108      mlir:: interface/pass object (widest non-Model mlir vtable)
```

The two structurally central F-class bases are `mlir::Pass` (13 slots, a CRTP contract where the concrete pass fills only `runOnOperation`) and `mlir::Pattern` (the second-widest inheritance tree in the binary). Their per-slot ABIs are walked in [RTTI / Vtable Census](../forensics/rtti-vtable-census.md).

> **NOTE —** F is a MEDIUM-confidence count because the `mlir::` namespace prefix attracts thunk-prefixed symbols whose owning namespace must be normalized before bucketing. The parent taxonomy folded ~310 such thunks into F that an earlier pass had left in the unclassified bucket.

---

## I — `llvm::` Vtables

### Signature

2,611 tables (6.5%): LLVM codegen objects — `TargetLowering`, SelectionDAG nodes, the legacy `Pass`/`FunctionPass`/`MachineFunctionPass` ladder, `MCStreamer`, diagnostics. Entry kind: relocated code pointers; this class holds the widest non-pool vtables in the binary.

### Largest Tables

```text
addr          entries  first symbol
0x2186b0c0    336      llvm::TargetLowering
0x21866ed8    336      llvm::TargetLowering
0x218567b0    336      llvm::TargetLowering
```

`llvm::TargetLowering` (336 slots, 3 instances) is the widest genuine vtable in Class I — the target-independent lowering interface that every backend target overrides. `llvm::MCStreamer` (164 raw slots) is the largest single-target streamer family. The legacy pass ladder (`Pass` 16 → `FunctionPass`/`ModulePass` 17 → `MachineFunctionPass` 21) is the inheritance backbone of this class.

---

## D — oneDNN / Xbyak JIT Vtables

### Signature

2,289 tables (5.7%): oneDNN JIT primitive vtables (≈1,692) plus Xbyak code-generator vtables (≈546), reported jointly because they form one physical inheritance tree — every JIT kernel inherits `jit_generator` (an Xbyak `CodeGenerator`) plus a kernel interface, so the class is dominated by multiple-inheritance secondary-base thunks. Entry kind: relocated code pointers, with the heaviest `_ZThn` this-adjust thunk concentration of any class (≈1,376 of the binary's 3,016 non-virtual ABI thunks are `dnnl::impl::cpu::x64::*`).

### Largest Tables

```text
addr          entries  note
0x21b6e048    29       dnnl JIT primitive vtable (widest in class)
```

> **GOTCHA —** D-class vtable slots frequently hold a `_ZThn` this-adjust thunk, not the method body directly. Calling such a slot lands in an 8–12-byte stub that adjusts `this` by a (negative) byte offset and tail-jumps to the shared base body. A navigator that assumes a vtable slot points at the final method will mis-resolve every secondary-base slot in this class. The single largest thunk fan-in target in the binary, `jit_generator::~D2`, collects 281 destructor thunks.

---

## G — `xla::` / `stablehlo::` Vtables

### Signature

2,154 tables (5.3%): the XLA compiler and jellyfish backend — `HloInstruction` (18-slot, 67 derived vtables), `xla::cpu::Thunk` (8-slot, 24 derived), the HLO pass system, `OpEmitter`, and the per-generation `Target` descriptors. Entry kind: relocated code pointers.

### Largest Tables

```text
addr          entries  first symbol
0x21cc6358    266      xla::jellyfish::JellyfishTarget
0x21cc6bd0    266      xla::jellyfish::JellyfishTarget
0x21cc74e8    266      xla::jellyfish::Target
0x21cc7da8    266      xla::jellyfish::Target
0x21cc8728    266      xla::jellyfish::Target
0x21cc90a8    266      xla::jellyfish::Target
0x21cce6b0    266      xla::jellyfish::Target
```

> **CORRECTION (DTT-FULL-2) —** an early pass reported "9 vtables at size 266". The table sidecar contains **exactly 7** 266-slot tables (2 `JellyfishTarget` + 5 `Target`), at `0x21cc6358`–`0x21cce6b0`. These are the per-generation target descriptors that install the per-gen cost model. The 7-not-9 count is confirmed by enumerating every 266-entry record. See [Per-Generation Function Dispatcher](../forensics/per-gen-function-dispatcher.md) for how these are reached through hand-written `GoogleInitializer` forwarders.

---

## H — `tensorflow::` / `tsl::` Vtables

### Signature

2,153 tables (5.3%): TensorFlow grappler transposers, op kernels, runtime objects, and the `tsl` support library. Entry kind: relocated code pointers; the `OpKernel` 7-slot base (`Compute` pure at slot 2) is the structural root.

### Largest Tables

```text
addr          entries  first symbol
0xa304280     89       tensorflow::grappler::Conv2DBackpropFilterTransposer
0xa304e30     70       tensorflow::grappler::BiasAddGradTransposer
0xa304bb0     70       tensorflow::grappler::BiasAddGradTransposer
```

The grappler transposers (`.rodata`-resident) are the widest H-class tables — each transposer carries the full layout-transformation interface.

---

## P — Abseil Hash-Container Policy Thunks

### Signature

2,066 tables (5.1%): the type-erased policy dispatch for `flat_hash_set`/`flat_hash_map`/`node_hash_*`. Each hashmap instantiation routes through a `container_internal` policy table; the class is dominated by a single global thunk table that fans out to every hashmap instance. Entry kind: member-pointer / policy thunks (not classic vtables).

### Largest Tables

```text
addr          entries  first symbol
0x21c1d590    447      absl::container_internal::GetRefForEmptyClass
```

> **NOTE —** the 447-entry `GetRefForEmptyClass` table at `0x21c1d590` is the global flat-hash policy thunk — one table fanning out to 447 distinct hashmap instantiations. An earlier pass under-counted Class P at 70, having lumped most `raw_hash_set` policy thunks into the long-tail; the correct figure is 2,066, the largest single reclassification in the taxonomy.

---

## O — Long-Tail Named-Namespace Vtables

### Signature

1,866 tables (4.6%): roughly 150 small named namespaces — Eigen, OR-tools (`operations_research`), RE2, riegeli, ANTLR, ICU, and dozens more — each contributing a handful of vtables. Entry kind: relocated code pointers. This is the residual of the named-namespace classification: anything with a demangled namespace that is not one of the big buckets. LOW confidence because the boundary with K (libc++) and Z1 (anonymous) is classifier-dependent.

### Largest Tables

```text
addr          entries  note
0x21865d20    256      long-tail named-namespace vtable
0xa303ec0     111      Eigen::internal::general_matrix_vector_product
```

---

## K — libc++ `std::` Thunks

### Signature

1,802 tables (4.5%): `std::__u::` (libc++ inline-namespace) dispatch — `shared_ptr` emplace bodies, `__function::__policy_func` type-erasure, sort/visitation dispatchers. Entry kind: relocated code pointers and ICF-folded forwarders.

### Largest Tables

```text
addr          entries  first symbol
0x21c0c0c8    231      std::__u::__variant_detail::__visitation::__base::__dispatcher
```

> **NOTE —** the 231-slot `__variant_detail::__dispatcher` is a `std::variant` visitation table — one slot per alternative type. K was the biggest beneficiary of `_ZNSt` symbol-routing fixes: an earlier pass mis-parsed the `St` mangling prefix and routed ~422 std tables into the unclassified bucket.

---

## N — TPU Runtime / Profiler Vtables

### Signature

1,130 tables (2.8%): the TPU HAL runtime — `TpuHal`/`TpuCore`/`TpuChip`/`TpuCodec` hardware abstraction, xprof profiler, superpod, and stream_executor objects. Entry kind: relocated code pointers; the per-generation HAL families live here.

### Largest Tables

```text
addr          entries  note
0x21ca92b0    61       TPU runtime / HAL object (widest in class)
```

The structurally central N-class hierarchies are `TpuHal<F>HardwareImpl` (23-slot, per-generation `{Jxc,Pxc,Vxc}` leaves), `TpuCodec` (6-slot, 5 named codecs sharing a base destructor), and `CycleTable` (5-slot cost model, fully overridden per generation). The `CycleTable` family lives at consecutive addresses (`0x21c1ffc8` `JfCycleTable` … `0x21c201d8` `GfcCycleTable`, 5 slots each); their per-slot ABI and override matrix are in [Per-Generation Function Dispatcher](../forensics/per-gen-function-dispatcher.md).

---

## C — libpfm4 PMU Event Tables

### Signature

833 tables (2.1%), **all in `.data`** (runtime-mutable, not vtables): per-microarchitecture PMU event lookup tables for the host CPU performance-counter sampling path. Entry kind: C function pointers / struct tables, not C++ vtables. This is one of two classes that lives entirely in mutable `.data`.

### Largest Tables

```text
addr          entries  note
0x222684d8    10       pfm_* per-microarch PMU event table
```

The 833 count reproduces exactly, and the section residency (`.data`, all 833) is the cleanest single-class anchor in the census: a table whose first symbol is `pfm_*` is in `.data` with probability 1.

---

## L — Protobuf Message / Descriptor Vtables

### Signature

712 tables (1.8%): `proto2` message reflection, descriptor, and `MapEntry` vtables, plus the `TcParser` fast-path tables. Entry kind: relocated code pointers. These feed the in-memory dispatch of the protobuf reflection layer behind the recovered FileDescriptorProto pool.

### Largest Tables

```text
addr          entries  first symbol
0x220387e8    117      proto2::internal::TcParser::FastV8S1 (parse fast-path)
```

---

## Z1 — Anonymous-Namespace Static Helpers

### Signature

698 tables (1.7%): translation-unit-local `_GLOBAL__N_` helpers, passes, and lambda dispatch — anything in an anonymous namespace. Entry kind: relocated code pointers. Split out from the long-tail (O) because anonymous-namespace symbols have no recoverable owning namespace and must be classified by their `_GLOBAL__N_` / `(anonymous)` prefix. LOW confidence.

### Largest Tables

```text
addr          entries  note
0xa30af90     165      (anonymous)::NVVMReflect / ::MCAsmStreamer-class helper
```

---

## B — LLVM `UniqueFunctionBase` Pools

### Signature

≈591 tables (1.5%), **runtime-mutable in `.data`**: `llvm::detail::UniqueFunctionBase` type-erasure pools — *not* vtables. Each is a pool of type-erased callables (move-only `std::function` analogues). Entry kind: runtime-mutable callable slots. This class holds the single largest table in the entire binary.

### Largest Tables

```text
addr          entries  first symbol
0x223393a0    2595     UniqueFunctionBase<LogicalResult(Operation*, ArrayRef<Attribute>, ...)>
0x22337a90    360      UniqueFunctionBase pool
0x2233e500    291      UniqueFunctionBase pool
0x22354c00    201      UniqueFunctionBase pool
0x22303670    168      UniqueFunctionBase pool
0x22338760    162      UniqueFunctionBase pool
```

> **QUIRK —** the 2,595-entry table at `0x223393a0` is the unified MLIR Op verify/parse/print type-erasure dispatch pool — a single `UniqueFunctionBase` holding 2,595 callables, more than 4× the next-largest table. It lives in `.data`, not `.data.rel.ro`, because the pool is mutated at runtime (callables are installed during dialect registration), so it is not a load-time-relocated constant. A census that filters on `.data.rel.ro` (where 95.9% of tables live) misses this and the libpfm4 class entirely.

---

## R, Q, Z — Tail Classes

Three small classes complete the 99.6% coverage.

**R — C-runtime / Rust I/O & codec handler tables (33).** cURL (`Curl_nghttp2_*`), BoringSSL connection-filter (`ssl_cf_*`), zstd (`ZSTD_*`), hwloc, and Rust v0-mangled (`_RNv*`) handler tables. Real dispatch structures, not the trampoline false-positives an earlier pass labeled them. Top table `0x21fbfee8` — 30 entries.

**Q — abseil `AnyInvocable` invoker thunks (2).** `absl::functional_internal::InvokeObject<...>` type-erasure invoker thunks. Top table `0xa30c788` — 4 entries, `.rodata`-resident.

**Z — unclassified (157).** Pure-virtual-only tables and IDA-auto-named (`sub_`/`nullsub_`) tables with no recoverable symbol owner. The largest is `0x21c3c558` (345 entries, first slot `sub_1CDA77A0`) — a wide table whose owner could be recovered by matching slot targets to `.text` function ranges, not from symbols.

> **NOTE —** the Z residual at 157 (0.4%) is the floor of the current symbol-based classifier. Resolving it further requires address-band → `.text`-owner matching rather than symbol demangling. The single 345-entry `sub_`-named table inflates Z's apparent max far beyond its typical 6-entry median.

---

## Switch Jump-Table Distribution

The 33,016 compiled switch jump tables are a structurally separate population from the 40,313 function-pointer tables: they are LLVM-lowered `switch` statements that indirect through a `.lrodata` offset table, not arrays of relocated function pointers. They hold 4,673,757 case entries total, with a median of 18 cases per switch.

### Size Distribution

Re-derived directly by bucketing every switch's case count:

| Case-count bucket | Switches | Cumulative | Confidence |
|-------------------|---------:|-----------:|------------|
| 1–4 | 2,681 | 2,681 | HIGH |
| 5–8 | 10,148 | 12,829 | HIGH |
| 9–16 | 2,857 | 15,686 | HIGH |
| 17–32 | 4,213 | 19,899 | HIGH |
| 33–64 | 4,063 | 23,962 | HIGH |
| 65–128 | 3,150 | 27,112 | HIGH |
| 129–256 | 3,070 | 30,182 | HIGH |
| 257–512 | 1,301 | 31,483 | HIGH |
| 513–1024 | 625 | 32,108 | HIGH |
| 1025–4096 | 720 | 32,828 | HIGH |
| 4097+ | 188 | 33,016 | HIGH |

The distribution is sharply front-loaded: 39% of all switches (12,829) have 8 or fewer cases — these are small enum/state dispatches. Only 188 switches (0.6%) exceed 4,096 cases, but those few hold a disproportionate share of the 4.67M total cases. The 5–8 bucket alone (10,148 switches) is the single mode, reflecting the binary's pervasive small enum dispatch.

### Largest Switches

```text
cases    addr          function
40813    0x11cc4900    (anon)::AMDGPUMCCodeEmitter::getBinaryCodeForInstr
 7529    0x1fe5edc0    asic_sw::driver::deepsea::gxc::glc::profiler::PerformanceCounterNameToString
 7529    0x1fe57d40    asic_sw::...::PerformanceCounterNameToString
 7529    0x1fe50f80    asic_sw::...::PerformanceCounterNameToString
 7529    0x1fe4a0c0    asic_sw::...::PerformanceCounterNameToString
 7529    0x1fe432e0    asic_sw::...::PerformanceCounterNameToString
```

> **NOTE —** the maximum switch — 40,813 cases in `AMDGPUMCCodeEmitter::getBinaryCodeForInstr` at `0x11cc4900` — is a TableGen-generated instruction-encoder dispatch, not a hand-written switch. It is larger than the entire function-pointer table population's biggest table (2,595) by an order of magnitude, which is why the two populations must be censused separately. The `PerformanceCounterNameToString` cluster (multiple 7,529-case switches, one per generation) is the per-gen performance-counter-enum → string dispatch.

The switch classes break down by owning namespace: the `asic_sw` ISA encode/decode opcode switches dominate (≈11,746 tables, ≈3.94M cases — 84% of all switch cases), followed by other named-namespace switches, LLVM IR/codegen, and XLA HLO-opcode switches. Only ≈12 literal `TpuVersion` enum switches exist — per-generation dispatch is carried by parallel vtable families (Class E/G/N above), not by switching on the generation enum. See [Per-Generation Function Dispatcher](../forensics/per-gen-function-dispatcher.md).

---

## Verification Anchors

Every figure on this page was re-derived from the table, switch, RTTI, and fixup sidecars. The hard anchors, confirmed exact:

```text
function-pointer tables  = 40,313 ; entries sum = 516,323
section split            = 38,664 .data.rel.ro / 1,442 .data / 207 .rodata
size-23 tables           = 6,129 ; with Model entry = 6,050 ; without = 79
largest table            = 0x223393a0 = 2,595 entries (UniqueFunctionBase, .data)
.data population          = 1,442 = 833 libpfm4 + ~586 UniqueFunctionBase + ~24 other
abseil policy             = 0x21c1d590 = 447 entries (GetRefForEmptyClass)
asic_sw widest            = 0x21e0d0a0 = 674 (TensorCoreVectorAluCompact, gxc/gfc)
266-slot Target tables    = exactly 7 @ 0x21cc6358 … 0x21cce6b0
llvm widest               = 0x2186b0c0 = 336 (TargetLowering, 3 instances)
RTTI records              = 160,351 (nm) ; _ZTV vtable groups = 39,244
fixups                    = 1,069,603 (924,033 in .data.rel.ro)
switch jump tables        = 33,016 ; cases sum = 4,673,757 ; median 18
largest switch            = 0x11cc4900 = 40,813 cases (AMDGPUMCCodeEmitter)
```

The soft per-class counts (E, F, I, D, H, K, M, N, O, Z1) are classifier-dependent at the ±100s level, as they hinge on how thunk-prefixed and local-scope symbols are normalized into their owning namespace; they carry MEDIUM/LOW confidence accordingly. The structural anchors above carry HIGH confidence and reproduce to the digit.

## Cross-References

- [Dispatch-Table Taxonomy](../forensics/dispatch-table-taxonomy.md) — the curated narrative parent; the "why" of each of the 19 classes and the ICF mechanism that populates them
- [RTTI / Vtable Census](../forensics/rtti-vtable-census.md) — the per-slot ABI walks of the central hierarchies that populate Classes A/F/G/I/N
- [Per-Generation Function Dispatcher](../forensics/per-gen-function-dispatcher.md) — how the per-gen vtable families (CycleTable, TpuCodec, the 7 Target descriptors) replace a `TpuVersion` switch
- [Polymorphic Entry Points](../forensics/polymorphic-entry-points.md) — the thunk/ICF forwarding layer; how 17,002 relocated slots point at a forwarding stub instead of the method body
- [RTTI Namespace Census](rtti-namespace-census.md) — the parallel namespace-level RTTI breakdown (sibling appendix)
- [Symbol Namespace Index](symbol-namespace-index.md) — the namespace prefix index underlying the per-class first-symbol classification (sibling appendix)
