# RTTI / Vtable Census

> *All addresses, counts, and symbol names on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel: a 781,691,048-byte ELF64 shared object, build-id `89edbbe81c5b328a958fe628a9f2207d` (the wheel/`METADATA`/`__init__` version is `0.0.40`; pin to the build-id, which is unambiguous). Other wheels will differ in every address.*

## Abstract

`libtpu.so` ships **un-stripped with full RTTI**. That single fact turns the binary's 745 MiB of statically-linked C++ into a self-describing object graph: every polymorphic class left an Itanium-ABI `type_info` record (`_ZTI`), a type-name string (`_ZTS`), and — if it is concrete — a vtable group (`_ZTV`). The RTTI sidecar holds **160,351** such records. Walking them reconstructs the entire C++ class forest of the compiler, runtime, and every vendored dependency without ever disassembling a function: the inheritance edges live in the `type_info` structs, and the typeinfo↔vtable binding ties each class to its dispatch table.

This page is the census. It establishes the headline counts (how the 160,351 records split into `_ZTI` / `_ZTV` / `_ZTS`, and how the typeinfos divide into the three Itanium `type_info` flavors), ranks the dominant polymorphic hierarchies by width and depth (the protobuf message universe, the two MLIR "Operation" structures, `llvm::Pass`, `xla::HloInstruction`, the TPU per-lane-cluster trees), and documents the cross-validation method that maps each `_ZTI` to its `_ZTV` and reads base-class chains out of `__class_type_info` / `__si_class_type_info` / `__vmi_class_type_info`. That binding is what makes the [dispatch-table taxonomy](dispatch-table-taxonomy.md) trustworthy: a recovered dispatch table is only as good as the class identity attached to it, and here every class identity is checkable against the type forest.

The frame to carry through the page: this is the **Itanium C++ ABI** as Clang/libstdc++ emit it. A reader who knows how `g++ -frtti` lays out a vtable group — `[offset-to-top][typeinfo-ptr][fn0][fn1]…]` with the address point at `+0x10` — already knows the data model. The only twist is that the binary was prelinked for a non-PIE-like image: the typeinfo pointers, base pointers, and function pointers are **zero in the file bytes** and are supplied by `R_X86_64_RELATIVE` relocation addends, so the inheritance graph is reconstructed from the relocation table, not from the raw `.data.rel.ro` image.

For reimplementation — i.e. to rebuild this census from the binary — the contract is:

- **The record taxonomy:** how 160,351 RTTI records partition into typeinfo structs, vtable groups, and name strings, and how a typeinfo's first word identifies its `type_info` flavor.
- **The edge-recovery rule:** how to read a single base out of `__si_class_type_info+0x10` and a base array out of `__vmi_class_type_info+0x18`, given that the base pointers are relocation addends.
- **The typeinfo↔vtable binding:** the `_ZTV+8 → _ZTI` invariant, how multiple-inheritance secondary sub-vtables repeat the own-class typeinfo, and what "abstract = has `_ZTI` but no `_ZTV`" means structurally.

| | |
|---|---|
| **RTTI sidecar records** | 160,351 (`_ZTI` 60,457 · `_ZTV` 39,244 · `_ZTS` 60,650 · 2 demangler-prefix strings) |
| **`type_info` flavors** | `__class_type_info` 16,754 · `__si_class_type_info` 42,447 · `__vmi_class_type_info` 680 |
| **Inheritance edges** | ~43,807 (42,440 single-base + 1,367 multi/virtual-base) |
| **Class roots / real hierarchies** | 16,761 roots · 2,094 with ≥2 descendants |
| **Widest hierarchy** | `proto2::MessageLite` — 8,013 descendants, depth 3 (`_ZTI` `0x22034138`) |
| **Deepest hierarchy** | `grpc::Service` — depth 10 (`_ZTI` `0x216162d8`, `_ZTV` `0x216162e8`) |
| **Vtable↔typeinfo binding** | 39,243 / 39,244 vtables bind to own-class `_ZTI`; 0 cross-class; 1 RTTI-less (`libunwind`) |
| **Relocation source** | `.rela.dyn` @ file `0x9170`, 1,069,006 `R_X86_64_RELATIVE` addends |

---

## Census Headline

### The record split

The RTTI sidecar is an array of 160,351 records, each a `(struct-address, mangled-symbol, name-string)` tuple harvested from `.rodata` / `.data.rel.ro`. Grouping by the three-letter Itanium prefix of the mangled symbol gives the top-level shape of the type system:

| Record kind | Prefix | Count | Meaning | Confidence |
|---|---|---:|---|---|
| Typeinfo struct | `_ZTI` | 60,457 | one per polymorphic *type* (class/si/vmi + pointer/pbase/fundamental) | CERTAIN |
| Vtable group | `_ZTV` | 39,244 | one per *concrete* polymorphic class (has at least one instantiable object) | CERTAIN |
| Typeinfo-name string | `_ZTS` | 60,650 | the human-readable mangled type name a `_ZTI` points at | CERTAIN |
| Demangler-prefix strings | (none) | 2 | the literals `"typeinfo for "` / `"typeinfo name for "` used by the in-binary demangler | CERTAIN |

The arithmetic closes exactly: `60,457 + 39,244 + 60,650 + 2 = 160,351`. The three counts are independent measurements — `_ZTI` and `_ZTV` are *struct* addresses in `.data.rel.ro`, `_ZTS` are *string* addresses in `.rodata` — and they are internally consistent: there are more typeinfos than vtables (60,457 vs 39,244) precisely because **20,638 of the 59,881 class-kind typeinfos are abstract** — pure-virtual interface bases that carry identity but own no vtable (see [§ Cross-Validation](#crossvalidation-rtti--vtable)). There are slightly more `_ZTS` strings than `_ZTI` structs (60,650 vs 60,457) because a handful of name strings are shared or were emitted for types whose `_ZTI` was folded.

> **NOTE —** the 160,351 figure is the *record* count, not the *distinct-class* count. A C++ template instantiated N times produces N distinct `_ZTI`/`_ZTS` pairs (e.g. every `std::__shared_count<...>` specialization, every `RegisteredOperationName::Model<Op>`), so the population is dominated by template explosion. The "distinct logical classes" number is far smaller; this census counts the instantiated types as the ABI emits them.

> **CORRECTION (RTTI-CENSUS-1) —** an earlier draft reported an inflated "structure-walk" census of 60,471 `_ZTI` / 39,246 `_ZTV` / 60,847 `_ZTS` (total 160,566), on the premise that a direct `.data.rel.ro` walk resolves records the symbol table missed. That is backwards: `libtpu.so` ships un-stripped, so every `_ZTI`/`_ZTV`/`_ZTS` carries a local `d` symbol and the symbol-table walk is the authoritative, exhaustive count. Direct `nm` over the binary gives **60,457** `_ZTI`, **39,244** `_ZTV`, **60,650** `_ZTS` (total **160,351**) — no structure-walk can find *more* records than there are symbols. The inflated figures are retracted; all counts on this page are the byte-exact `nm` symbol counts. (Reproduce: `nm libtpu.so | rg -c '_ZTI'`, etc.)

### Per-namespace population

Bucketing each typeinfo by the **leading namespace token of its mangled `_ZTIN<len><namespace>` symbol** gives the demographic of the type forest — and confirms that the compiler/runtime core, not the vendored support libraries, dominates the polymorphic surface. (These are leading-namespace buckets, *not* demangled top-token counts: the latter over-credit `xla`/`tensorflow` for every `absl::StatusOr<xla::…>` / `std::unique_ptr<…>` wrapper whose owning class lives elsewhere. The [namespace census appendix](../appendix/rtti-namespace-census.md) owns the full breakdown.)

| Namespace | Typeinfos | Role | Confidence |
|---|---:|---|---|
| `mlir` | 13,091 | MLIR dialects, ops, passes, patterns, interfaces | CERTAIN |
| `asic_sw` | 11,379 | TPU driver / profiler / ISA event-control trees | CERTAIN |
| `tensorflow` | 3,108 | TF op-kernels, graph, TFRT TPU | CERTAIN |
| `xla` | 3,036 | HLO, thunks, HLO passes, jellyfish codegen | CERTAIN |
| `llvm` | 2,940 | embedded LLVM backend (codegen, Attributor, VPlan) | CERTAIN |
| *(anon ns)* | 2,352 | translation-unit-local classes (`_GLOBAL__N_`) | CERTAIN |
| `dnnl` | 1,888 | oneDNN primitive descriptors / JIT kernels | CERTAIN |
| `std` | 1,787 | libstdc++ template plumbing | CERTAIN |
| `grpc_core` | 1,502 | gRPC ref-counted / orphanable object trees | CERTAIN |
| `platforms_deepsea` | 576 | TPU ISA encoders, debugger services | CERTAIN |

Of the 60,457 `_ZTI` structs, 46,078 carry a leading namespace and 14,379 are global-scope or compound types (pointer/function/substitution). The sum `mlir + xla + llvm + tensorflow + asic_sw + platforms_deepsea` is the compiler/runtime core; `std`, `absl`, `dnnl`, `Eigen`, `grpc`, OR-tools are vendored support. The single largest *tree*, however, lives under `proto2` (the protobuf message universe) — wide but shallow, and not the largest *namespace*.

---

## Top Hierarchies

A hierarchy's **width** is its count of transitive descendants; its **depth** is the longest downward chain from the root. The forest has 16,761 class roots, of which 2,094 are "real" hierarchies (≥ 2 descendants). The table below is the curated top tier — the structurally central trees, with pure template/plumbing trees (e.g. `std::__shared_count` at width 1,338, the four `mlir::spirv::Query*InterfaceTraits::Concept` at 376 each) excluded except where they are the binary's defining structure. Width/depth and every address are re-derived from the sidecar.

| Width | Depth | Root class | Root `_ZTI` | Root `_ZTV` | Role | Confidence |
|---:|---:|---|---|---|---|---|
| 8013 | 3 | `proto2::MessageLite` | `0x22034138` | abstract | protobuf message reflection (Message + 8,007 generated msgs) | HIGH |
| 6142 | 9 | `mlir::Pattern` | `0x21cea698` | abstract | MLIR rewrite / conversion / lowering pattern tree | HIGH |
| 6052 | 2 | `mlir::OperationName::InterfaceConcept` | `0x217b1000` | abstract | MLIR op-interface `Model<Op>` dispatch | HIGH |
| 2069 | 6 | `dnnl::impl::c_compatible` | `0x21b69258` | abstract | oneDNN primitive-descriptor / JIT base | HIGH |
| 1122 | 4 | `tensorflow::OpKernel` | `0x218114c8` | `0x218113d8` | TF op-kernel base (TPU embedding / XLA kernels) | HIGH |
| 821 | 1 | `asic_sw::…::profiler::EventControlInterface` | `0x2175c798` | abstract | per-lane-cluster TPU perf-counter event control | HIGH |
| 628 | 5 | `llvm::Pass` | `0x21ced3b8` | `0x21ced328` | LLVM legacy pass-manager hierarchy | HIGH |
| 606 | 7 | `mlir::Pass` | `0x21c2c450` | `0x21c2c3d8` | MLIR pass hierarchy (Op/Module/Function passes) | HIGH |
| 551 | 5 | `Xbyak::CodeArray` | `0x21b6d738` | `0x21b6d818` | Xbyak JIT code-generator base (oneDNN backend) | HIGH |
| 442 | 6 | `grpc_core::PolymorphicRefCount` | `0x21ca0128` | abstract | gRPC ref-counted object base | HIGH |
| 361 | 4 | `xla::HloPassInterface` | `0x217f4428` | abstract | XLA HLO compiler-pass interface | HIGH |
| 329 | 9 | `llvm::AbstractState` | `0x218540e8` | abstract | LLVM Attributor abstract-attribute state machine | HIGH |
| 299 | 8 | `llvm::IRPosition` | `0x21b345d8` | abstract | LLVM Attributor IR-position abstraction | HIGH |
| 299 | 8 | `llvm::AADepGraphNode` | `0x21854120` | `0x218540f8` | LLVM Attributor dependency-graph node | HIGH |
| 184 | 4 | `grpc_core::Orphanable` | `0x21ca01b8` | abstract | gRPC orphanable object base | HIGH |
| 166 | 3 | `llvm::cl::Option` | `0x21fe0cc0` | `0x21fe0c58` | LLVM command-line option base (feeds MLIR `PassOptions`) | HIGH |
| 140 | 4 | `tsl::core::RefCounted` | `0x215f9b18` | abstract | TSL/TF ref-counted base | HIGH |
| 114 | 6 | `riegeli::Object` | `0x220291a8` | `0x22029168` | riegeli record-IO object base | HIGH |
| 100 | 3 | `mlir::DialectInterface` | `0x21cea480` | abstract | MLIR dialect-interface base | HIGH |
| 68 | 4 | `xla::HloInstruction` | `0x21d2ce88` | `0x21d2cde8` | XLA HLO instruction tree | HIGH |
| 53 | 3 | `xla::cpu::Thunk` | `0x219b15a0` | `0x219b1550` | XLA:CPU runtime thunk tree | HIGH |
| 44 | 10 | `grpc::Service` | `0x216162d8` | `0x216162e8` | gRPC generated-service base (**deepest in the binary**) | HIGH |
| 23 | 3 | `llvm::MCStreamer` | `0x21b60b70` | `0x21b60630` | LLVM MC streamer tree (+ TPU SparseCore `SCStreamer`) | HIGH |

> **GOTCHA —** four of the widest trees are *abstract* — their `_ZTV` column reads "abstract" because the root carries `_ZTI` but **owns no vtable**. `proto2::MessageLite`, `mlir::Pattern`, `mlir::OperationName::InterfaceConcept`, `HloPassInterface`, `AbstractState`, `IRPosition`, `EventControlInterface` are all pure-virtual interface bases. A reimplementer hunting for "the MessageLite vtable" to hook will not find one: dispatch lives one level down, in each concrete subclass's own `_ZTV`. This is the recurring **per-generation-family** pattern — an interface root with a flat fan of concrete leaves that each fully override (see the TPU codec / cycle-table / encoder families, [Per-Generation Function Dispatcher](per-gen-function-dispatcher.md)).

Four structural facts about this ranking deserve emphasis, because they are what a reader should *learn* from it rather than memorize:

**The protobuf message universe is the single largest tree.** `proto2::MessageLite` → `proto2::Message` (8,007 descendants) → one concrete generated message per `.proto` type. It is wide (8,013) but shallow (depth 3): protobuf's generated classes are a flat fan, not a deep chain. This is the in-memory reflection backing the binary's embedded `FileDescriptorProto` records.

**MLIR has two distinct "Operation" structures, and neither is a polymorphic `mlir::Operation`.** `mlir::Operation` itself is **non-polymorphic** — it has no `_ZTI` and no vtable; MLIR ops carry no C++ virtual table at all. Op behavior is dispatched two ways, both visible as huge RTTI trees:

```text
(a) Type-erased op-interface dispatch  (the "op model"):
    mlir::OperationName::InterfaceConcept   (6,052 desc, depth 2)
      └─ mlir::RegisteredOperationName
           └─ Model<Op>   (one per registered op — verify/parse/print/fold)

(b) The rewrite / lowering pattern tree:
    mlir::Pattern   (6,142 desc, depth 9 — second-widest tree)
      └─ RewritePattern
           └─ ConversionPattern
                └─ ConvertToLLVMPattern
                     └─ ConvertOpToLLVMPattern
                          └─ ... (TPU SparseCore lowering is the deepest branch)
```

The `Model<Op>` arrays are exactly the MLIR-Op-Model class of the [dispatch-table taxonomy](dispatch-table-taxonomy.md); the `Pattern` tree is the second-widest tree in the entire binary.

**The LLVM backend is fully embedded and is the deepest codegen structure.** `llvm::Pass` (628 descendants) splits into the classic legacy-PM categories; the principal branch is `FunctionPass` (506) → `MachineFunctionPass` (351, of which 324 direct) → AMDGPU / PPC / TPU machine passes. `libtpu.so` embeds complete AMDGPU, PPC, ARM, AArch64, and X86 backends in addition to the TPU target — every `*TargetLowering` is present under `TargetLoweringBase`. The new-PM Attributor contributes two parallel 8–9-deep CRTP template chains (`AbstractState` depth 9, `IRPosition`/`AADepGraphNode` depth 8), the deepest CRTP chains in the binary after the gRPC service tree.

**The TPU-specific trees are wide-and-flat, partitioned by lane cluster.** `EventControlInterface` (821 descendants, **depth 1** — all direct leaves) is the per-lane-cluster performance-counter event-control hierarchy, split by cluster the same way the S-ISA event tables are. The four TPU HAL trees (`TpuCodec`, `isa::Encoder`, `CycleTable`, plus the runtime `TpuHal`/`TpuChip`/`TpuCore` trees) are pure-virtual interface roots whose per-generation leaves each fully override — the structural signature of per-gen vtable-family dispatch rather than a `switch`.

### `xla::HloInstruction` — a worked tree

The HLO instruction tree is the canonical mid-size hierarchy and the one most likely to be hooked, so it is worth its full structure. Root `_ZTI` `0x21d2ce88`, root `_ZTV` `0x21d2cde8`; 68 transitive descendants over 38 direct subclasses; 9 internal (multi-level) nodes, 59 concrete leaves; depth 4.

| Internal node | children | flavor | vtable | Confidence |
|---|---:|---|---|---|
| `HloDimensionsInstruction` | 7 | si | `0x21d2fb20` | HIGH |
| `HloCollectiveInstruction` | 5 | si | `0x21d2dd40` | HIGH |
| `HloSendRecvInstruction` | 4 | si | `0x21d2d9f8` | HIGH |
| `HloChannelInstruction` | 3 | si | `0x21d2d8b0` | HIGH |
| `HloCallableInstruction` | 3 | **vmi** | `0x21d2ea00` | HIGH |
| `HloBatchNormInstruction` | 3 | si | `0x21d2d1b0` | HIGH |
| `HloDynamicIndexInstruction` | 2 | si | abstract | HIGH |
| `HloAllReduceInstructionBase` | 2 | si | `0x21d2de90` | HIGH |
| `HloAsyncInstruction` | 1 | si | `0x21d2d4d0` | HIGH |

The deepest chain (depth 4) is `HloInstruction → HloChannelInstruction → HloCollectiveInstruction → HloAllReduceInstructionBase → HloAllReduceInstruction`. The paired `xla::DfsHloVisitorBase<HloInstruction const*>` (52 descendants, `_ZTI` `0x21d2c790`) is the double-dispatch visitor counterpart. `HloCallableInstruction` is the lone multiple-inheritance node in the tree — a `__vmi` class mixing `HloInstruction` (`0x21d2ce88`) with the empty `HloAliasible` mixin (`0x21d2ce98`); because the second base is empty it produces no secondary sub-vtable, so the `__vmi` group at `0x21d2ea00` still carries a single own-typeinfo pointer at `+8`.

---

## RTTI Record Kinds

Every `_ZTI` is one of the Itanium ABI's `type_info` flavors, and the flavor is encoded in the **first word** of the typeinfo struct: it is a pointer into the address point of the *metatype* vtable (the vtable of the `type_info` subclass itself), which is one of a tiny fixed set. Histogramming the word at `_ZTI+0` therefore classifies all 60,457 typeinfos with no parsing of names:

| Metatype vtable @`_ZTI+0` | Count | `type_info` flavor | Base info | Confidence |
|---|---:|---|---|---|
| `0x220485b0` | 16,754 | `__class_type_info` | none — a root with no base | CERTAIN |
| `0x22048600` | 42,447 | `__si_class_type_info` | single public base @ `+0x10` | CERTAIN |
| `0x22048668` | 680 | `__vmi_class_type_info` | base array @ `+0x18`, count @ `+0x14` | CERTAIN |
| `0x220486d0` | 282 | `__pointer_type_info` | — (not a class hierarchy) | CERTAIN |
| `0x22048528` | 261 | `__function_type_info` | — (not a class hierarchy) | CERTAIN |
| `0x22048560` | 22 | `__enum_type_info` | — | CERTAIN |
| *(tail)* | 11 | `__fundamental_type_info` (10, `0x220483d8`) + `__pointer_to_member_type_info` (1, `0x22048708`) | — | HIGH |

Class-kind typeinfos (`class` + `si` + `vmi`) total **59,881**; the 576 pointer/function/enum/fundamental records are not class hierarchies and are correctly left as forest roots.

> **QUIRK —** the histogram value at `_ZTI+0` is the metatype vtable's **address point**, which sits `0x10` *above* the vtable group base. The `__class_type_info` *symbol* (`'vtable for'__cxxabiv1::__class_type_info`) lives at `0x220485a0`, but the value written into every `__class_type_info` typeinfo's first word is `0x220485b0` = `0x220485a0 + 0x10`. Likewise `__si` is `0x220485f0 → 0x22048600` and `__vmi` is `0x22048658 → 0x22048668`. A reimplementer who matches the histogram value against the *symbol* address will find every classification "off by 16" unless they add the address-point offset. This is the same `+0x10` address-point convention that governs ordinary vtables (offset-to-top at `+0`, typeinfo at `+8`, first method at `+0x10`).

### Reading the base-class chain

The three class flavors differ only in how they encode their parents. The layout, read at each `_ZTI` (64-bit):

```text
common header (all type_info):
  +0x00  metatype vtable ptr (+16)   -> identifies the flavor (table above)
  +0x08  ptr to the _ZTS name string

__si_class_type_info  (single inheritance):
  +0x10  ptr to the single base _ZTI         -> exactly ONE edge

__vmi_class_type_info (multiple / virtual inheritance):
  +0x10  u32 flags
  +0x14  u32 base_count
  +0x18  base_count x { ptr base_ZTI ; long offset_flags }   -> base_count edges
            offset_flags: (val >> 8)  = sub-object byte offset
                          bit0        = base is virtual
                          bit1        = base is public
```

```c
// Edge recovery — re-derives the 43,807-edge inheritance forest.
// Crucial caveat: the base pointers are 0 in the file image; their real
// values are R_X86_64_RELATIVE relocation ADDENDS. Read the addend at the
// vaddr, not the file byte.
function recover_edges(zti_addr, flavor):
    switch flavor:
      case __class:                                  // 16,754 records
          return []                                  // root, no parent

      case __si:                                     // 42,447 records
          base = reloc_addend_at(zti_addr + 0x10)    // ONE base ZTI
          if base is a known _ZTI:
              emit edge  derived=zti_addr -> base    // 42,440 valid (+7 dangling)

      case __vmi:                                    // 680 records
          n = read_u32(zti_addr + 0x14)              // base_count (plain bytes)
          for i in 0 .. n-1:
              base    = reloc_addend_at(zti_addr + 0x18 + 16*i)
              off_flg = read_long  (zti_addr + 0x18 + 16*i + 8)
              emit edge  derived=zti_addr -> base
              record sub_object_offset = off_flg >> 8     // Table D values
              record is_virtual        = off_flg & 1
              record is_public         = off_flg & 2
          // base-count distribution: 1->121  2->503  3->34  4->4  7->18
```

> **GOTCHA —** `flags` and `base_count` at `_ZTI+0x10`/`+0x14` are *plain `.data.rel.ro` bytes* (no relocation), but the base *pointers* in the array are *relocation addends*. A reimplementer who reads the whole `__vmi` record as file bytes will get a correct `base_count` and a `0` for every base pointer. The `.data.rel.ro` mapping has a fixed vaddr→file-offset delta of `0x200000` (vaddr `0x215f81a0` ↔ file `0x213f81a0`); the actual edges come from the 1,069,006 `R_X86_64_RELATIVE` entries in `.rela.dyn`.

### Multiple inheritance in the core

The 680 `__vmi` classes are rare (1.1% of class typeinfos) but structurally important. Their base-count distribution is `1→121, 2→503, 3→34, 4→4, 7→18`. The 121 single-base `__vmi` records are `__vmi` only to carry a non-zero sub-object offset; the 18 seven-base records are abseil `raw_hash_set` policy CRTP plumbing, not compiler logic. The genuine compiler-core diamonds — with their decoded sub-object offsets — are:

| Derived class | bases | sub-object offsets (`offset_flags >> 8`) | Confidence |
|---|---:|---|---|
| `llvm::TPUScheduleDAGSlackModulo` | 4 | `ScheduleDAGInstrs` +0 · `TPUScheduleDAGModulo` +3424 · `TPUScheduleDAGComposeFifo` +4528 · `ScheduleDAGInstrsWrapper` +4672 | HIGH |
| `llvm::TPUScheduleDAGSwingModulo` | 3 | `TPUScheduleDAGSwing` +0 · `TPUScheduleDAGModulo` +3728 · `ScheduleDAGInstrsWrapper` +4832 | HIGH |
| `llvm::RABasic` | 3 | `MachineFunctionPass` +0 · `RegAllocBase` +56 · `LiveRangeEdit::Delegate` +784 (non-public) | HIGH |
| `llvm::VPRecipeBase` | 3 | `VPDef` +0 · `ilist_node_with_parent` +16 · `VPUser` +32 | HIGH |
| `llvm::legacy::FunctionPassManagerImpl` | 3 | `Pass` +0 · `PMDataManager` +32 · `PMTopLevelManager` +416 | HIGH |
| `xla::TpuHostTransferManager` | 3 | `TransferManager` +0 · `PjRtHostTransferManager` +8 · `enable_shared_from_this<…>` +72 | HIGH |

> **NOTE —** every genuine compiler-core diamond decodes to `offset_flags & 1 == 0` — **non-virtual inheritance throughout the core.** There are no virtual bases in the MLIR/XLA/LLVM/TPU code, which means construction vtables (`_ZTC`) and VTTs (`_ZTT`) are effectively absent for the core; only the abseil/std plumbing tail might carry them. The `bit1` public flag is set everywhere except `RABasic → LiveRangeEdit::Delegate` (the delegate mixin is non-public). The single most common `__vmi` pattern overall is `mlir::detail::PassOptions::Option = {llvm::cl::opt, PassOptions::OptionBase}` — the MLIR pass command-line-option adapter, appearing hundreds of times.

---

## Cross-Validation: RTTI ↔ Vtable

The census's credibility rests on a single ABI invariant that ties the two halves of the type system together: **a vtable group stores, at offset `+8`, a pointer to the typeinfo of the exact same class.** The group layout is

```text
Itanium vtable group (64-bit), read at each _ZTV symbol:
  +0x00  offset-to-top (long; 0 for the primary sub-vtable, signed for secondaries)
  +0x08  typeinfo pointer  -> the class's own _ZTI        <== THE BINDING
  +0x10  address point: virtual-function slot 0, slot 1, ...

For a multiple-inheritance class the group repeats:
  [ primary  sub-vtable: off-to-top=0,  _ZTI(own), fns... ]
  [ secondary sub-vtable per non-primary polymorphic base:
        off-to-top<0, _ZTI(own again), thunk-fns... ]
```

The cross-validation walks every `_ZTV`, reads the relocation addend at `_ZTV+8`, and confirms it lands on the `_ZTI` of the same class (join key = the mangled class suffix shared by the `_ZTV`/`_ZTI`/`_ZTS` triple). The result is decisive:

| Quantity | Count | Confidence |
|---|---:|---|
| Vtable groups (`_ZTV`) | 39,244 | CERTAIN |
| Vtable → own-class `_ZTI` at `+8` (direct) | 39,185 | HIGH |
| Vtable → own-class `_ZTI` via MI secondary structure | 58 | HIGH |
| Vtable → own-class `_ZTI` (total) | 39,243 | HIGH |
| Vtable → **different**-class `_ZTI` (mismatch) | **0** | HIGH |
| Vtable with **no** `_ZTI` anywhere (RTTI-less) | 1 (`libunwind`) | CERTAIN |
| Concrete class typeinfos (own a vtable) | 39,243 | HIGH |
| Abstract class typeinfos (no vtable) | 20,638 | HIGH |

39,243 of 39,244 vtables bind to their own-class typeinfo with **zero** cross-class mismatches; the binding is verified bidirectionally. The lone exception is fully explained:

> **QUIRK —** the one RTTI-less vtable is `libunwind::UnwindCursor<LocalAddressSpace, Registers_x86_64>` at `_ZTV` `0x22048990` (a 16-slot group). Its `+8` typeinfo slot is `NULL` and no `_ZTI` symbol exists for the class, because `libunwind` is compiled `-fno-rtti`. This is correct behaviour for a no-RTTI translation unit, not a defect in the binding — and it is the *only* such unit in the 745 MiB binary. Every other vtable in `libtpu.so` carries a clean typeinfo pointer.

### What the binding proves

Three structural checks turn the `+8` invariant into a hierarchy cross-check:

**Single-inheritance monotonicity.** For `__si` derived/base pairs that both own a vtable, the derived vtable must be at least as large as the base vtable — single inheritance *appends* new virtual slots to the base's prefix. On a 2,000-pair random sample, `derived_vtable_size ≥ base_vtable_size` held 2,000/2,000 (0 violations). The RTTI base edge and the vtable layout agree.

**Multiple-inheritance secondary sub-vtables.** Of the 509 `__vmi` classes that own a vtable, 508 contain at least one own-typeinfo pointer; the 7-base gRPC `EventMgrEventEngine` has exactly 7 own-typeinfo repetitions (one per sub-object) inside its 0x1d0-byte group. The lone "0-secondary" case is the empty-mixin pattern (`HloCallableInstruction`: bases `HloInstruction` + empty `HloAliasible` → one sub-vtable, one own-typeinfo at `+8`), which is correct ABI behaviour.

**Abstract-root confirmation.** All 16 abstract-interface roots the width ranking marks "abstract" — `MessageLite` (`0x22034138`), `mlir::Pattern` (`0x21cea698`), `OperationName::InterfaceConcept` (`0x217b1000`), `EventControlInterface` (`0x2175c798`), `PolymorphicRefCount` (`0x21ca0128`), `HloPassInterface` (`0x217f4428`), `AbstractState` (`0x218540e8`), `IRPosition` (`0x21b345d8`), `Orphanable` (`0x21ca01b8`), `tsl::core::RefCounted` (`0x215f9b18`), `DialectInterface` (`0x21cea480`), `OffloadFactory` (`0x218fffd8`), `ErrorInfoBase` (`0x21fe1a70`), `isa::Encoder` (`0x21cb6a20`), `TpuCodec` (`0x21d35858`), `CycleTable` (`0x21c20008`) — were independently confirmed to own no same-class vtable, validating the abstract-vs-concrete split.

### How the census cross-checks the dispatch taxonomy

The typeinfo↔vtable binding is the bridge between this page and the [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md): every dispatch-table the taxonomy recovers is a vtable group whose `+8` pointer now names its class with certainty. Each top hierarchy maps to a taxonomy class, and the per-generation TPU families resolve to concrete leaf vtables:

| TPU family root (abstract) | Concrete leaf vtables | Confidence |
|---|---|---|
| `tpu::TpuCodec` (`0x21d35858`) | Jellyfish `0x21d360f0` · Dragonfish `0x21d35800` · Pufferfish `0x21d36148` · Viperfish `0x21d361a0` · Ghostlite `0x21d35c00` | HIGH |
| `xla::jellyfish::CycleTable` (`0x21c20008`) | Jf `0x21c1ffb8` · Pf `0x21c20048` · Vf `0x21c200c8` · Glc `0x21c20148` · Gfc `0x21c201c8` | HIGH |
| `platforms_deepsea::…::isa::Encoder` (`0x21cb6a20`) | EncoderJf `0x21d36ca8` · EncoderDf `0x21d36be0` · per-family `EncoderGl*`/`EncoderVf*`/`EncoderPf*` leaves | HIGH |

Each codec leaf is a `__si` single-inheritance class with no intermediate; the consecutive `CycleTable` leaf addresses (`0x21c1ffb8`…`0x21c201c8`) are the group bases of the per-generation cost-model tables the dispatch taxonomy enumerates as the cycle-table family. The pattern is uniform: an abstract interface root with no own vtable, and a flat fan of per-codename concrete leaves that each fully override — exactly the per-generation vtable-family dispatch documented in the [Per-Generation Function Dispatcher](per-gen-function-dispatcher.md).

### Hierarchy depth distribution

Over the 2,094 real hierarchies, depth is heavily skewed toward shallow trees — most polymorphism in the binary is one or two levels of override, not deep inheritance:

```text
depth  1: 1,615    depth  6:   4
depth  2:   264    depth  7:  10
depth  3:   136    depth  8:   3
depth  4:    46    depth  9:   4
depth  5:    11    depth 10:   1
```

The single depth-10 hierarchy is `grpc::Service` → seven stacked `TpuDebugService::WithAsyncMethod_*` gRPC template wrappers → `TpuDebugServiceImpl`. 33 hierarchies have more than 100 transitive descendants. The shape confirms the binary's polymorphism is dominated by **wide-and-flat** interface fans (the per-generation families, the protobuf and MLIR-op fans), with deep chains confined to template-heavy CRTP code (the Attributor, gRPC service wrappers).

### What this census does not resolve

- **Per-slot virtual-method labels.** The binding proves each vtable's address point and typeinfo identity, but does not resolve each function-pointer slot (`_ZTV+0x10+8i` addend → symbol) into a method name. That slot-walk is the next layer; it is done only for the four TPU HAL trees elsewhere.
- **Template-instantiation collapse.** Each `*<...>` instantiation counts as a distinct type; the width ranking counts `std::__shared_count` (1,338) and the four `spirv::Query*::Concept` (376 each) as separate trees. The curated top tier deliberately picks non-template roots so this does not distort it.
- **Non-polymorphic classes.** `mlir::Operation`, `mlir::Value`, `TpuHalCommonStates`, and similar carry no typeinfo and are invisible to the RTTI walk; their layout must come from member-access analysis, not RTTI.
- **The 7 dangling `__si` base pointers** (base addend not a known `_ZTI`) and a 2-class drift in the `__vmi` 2-base count are untriaged; both are < 0.02% and move no top-hierarchy result.

---

## Cross-References

- [Binary Forensics Overview](overview.md) — the file-level frame: section table, symbol counts, the 745 MiB shape this census walks.
- [Dispatch-Table Taxonomy](dispatch-table-taxonomy.md) — owns the table-count taxonomy; this census supplies the verified class identity behind every recovered dispatch table.
- [Per-Generation Function Dispatcher](per-gen-function-dispatcher.md) — the per-codename leaf-vtable dispatch pattern the abstract TPU-family roots resolve to.
- [Polymorphic Dispatch Entry Points](polymorphic-entry-points.md) — the indirect-call sites and top-vtable classes that consume these bindings at the call site.
