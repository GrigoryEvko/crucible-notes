# Evidence-Anchor Index

> *All addresses on this page apply to `libtpu.so` build-id `89edbbe81c5b328a958fe628a9f2207d`, from the `libtpu-0.0.40-cp314` wheel. The binary is **not** stripped: every `0x…` here resolves to a demangled C++ symbol in the IDA function table, and `.text`/`.rodata`/`.lrodata`/`.data.rel.ro` map VMA == file offset. Other builds will differ.*

## Abstract

This is the wiki's **reverse-lookup index**: a flat, address-sorted table of the binary anchors that recur across the deep pages, each mapped back to the page that explains it. It exists for one workflow — you are in IDA, your cursor lands on `0x1c89fba0`, and you want the prose that documents it. Scan the master table, find the row, follow the link. It is the inverse of [`subsystem-map`](../subsystem-map.md) (which goes subsystem → address band) and of [`front/codename-cheatsheet`](../front/codename-cheatsheet.md) (which goes codename → facts): both of those collect anchors *forward*; this page is the *flat address → page* spine that ties them together.

The index is deliberately **not exhaustive**. The binary has 884,832 functions; this page carries only the ~60 genuinely cross-cutting anchors — the entry points, factory functions, cost tables, target accessors, and data sections that are cited on three or more pages and that a reader is realistically likely to hit cold. Single-page locals (the thousands of `_GLOBAL__N_` helpers, per-opcode handlers, ICF duplicates) are documented in place on their owning page and are not duplicated here. The selection bias is toward addresses that a reimplementer needs to recognise to orient inside the binary at all.

Every row was verified **both ways** before it was admitted: (a) the address resolves to the named symbol in the IDA function table (`…_function_addresses.json`) or, for data anchors, to a section/symbol the owning page pins; and (b) the named owning page exists and actually cites the address. Where a label in circulation does not survive that check, a `> **CORRECTION —**` callout records it instead of a silent fix. Confidence reflects the *binding* (address ↔ page), not the underlying RE claim, which the owning page grades on its own.

For reimplementation, the contract this page serves is:

- **Orientation** — given a raw VA, recover the symbol, its role, and the page that documents the algorithm behind it.
- **Provenance** — every binding is independently re-checkable: the symbol is in the function table, the citation is in the named `src/` page.
- **Scope honesty** — the index covers the cross-cutting spine, not the long tail; it tells you what it does *not* index.

| | |
|---|---|
| **Indexed anchors** | ~58 cross-cutting addresses (of 884,832 functions total) |
| **Master table sort key** | ascending VA |
| **Symbol source** | `…_function_addresses.json` (functions) · ELF section/symbol records (data) |
| **`.text` span** | `0xe63c000` – `0x21217484` (~360 MB) |
| **Singleton band** | `.lbss` from `0x227ba840` |
| **Sibling indices** | [`subsystem-map`](../subsystem-map.md) (band → subsystem) · [`codename-cheatsheet`](../front/codename-cheatsheet.md) (codename → facts) · [`cross-reference-graph`](cross-reference-graph.md) (page → page) |

---

## Master Anchor Index

One row per cross-cutting anchor, sorted by ascending VA. **What it is** is a one-line role; **Owning page(s)** links the page(s) that document the algorithm or layout; **Confidence** grades the address ↔ page binding. Subsystem-grouped detail follows in the sections below.

| VA | Symbol | What it is | Owning page(s) | Confidence |
|---|---|---|---|---|
| `0x1c60480` | `xprof::kDeviceTypeInfo` | `DeviceTypeInfo[17]` `.lrodata` table | [profiling/kdevicetypeinfo-producer-readers](../profiling/kdevicetypeinfo-producer-readers.md) | CERTAIN |
| `0x3366d90` | `TPUMCCodeEmitter::…::InstBits` | Per-opcode TensorCore encoding bits | [isa/instbits-master-db](../isa/instbits-master-db.md) · [isa/record-format](../isa/record-format.md) | CERTAIN |
| `0x84a0000` | `.rodata` name pool | Merged NUL-terminated string pool (base) | [appendix/filewrapper-toc-catalog](filewrapper-toc-catalog.md) | HIGH |
| `0xb438aec` | `resLUT` (VF cost LUT) | Viperfish resource cycle lookup table | [cost/vf-cycletable](../cost/vf-cycletable.md) · [cost/iars-per-tensorcore](../cost/iars-per-tensorcore.md) | HIGH |
| `0xbe8af30` | `protodesc_cold` (section) | Serialized `FileDescriptorProto` blob pool | [appendix/protodesc-cold-catalog](protodesc-cold-catalog.md) | CERTAIN |
| `0xe635524` | `.init_proc` | ELF `DT_INIT`; runs the static-constructor storm | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) · [forensics/static-init](../forensics/static-init.md) | CERTAIN |
| `0xe63c000` | `__do_init` | Constructor driver; base of `.text` | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) | CERTAIN |
| `0xe6a83a0` | `GetPjrtApi` | Exported PJRT entry thunk | [lifecycle/get-pjrt-api-thunk](../lifecycle/get-pjrt-api-thunk.md) · [pjrt/overview](../pjrt/overview.md) | CERTAIN |
| `0xe6a9d00` | `pjrt::tpu_plugin::PJRT_Plugin_Initialize` | One-shot plugin-init slot of the `PJRT_Api` | [lifecycle/tftpu-initialize-bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) · [lifecycle/module-init-plugin-discovery](../lifecycle/module-init-plugin-discovery.md) | CERTAIN |
| `0xe6aa440` | `pjrt::tpu_plugin::GetTpuPjrtApi` | Lazy `PJRT_Api` builder + singleton cache | [lifecycle/get-pjrt-api-thunk](../lifecycle/get-pjrt-api-thunk.md) · [pjrt/overview](../pjrt/overview.md) | CERTAIN |
| `0xe6f54a0` | `TfTpu_Initialize` | C-shim runtime bootstrap | [lifecycle/tftpu-initialize-bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) | CERTAIN |
| `0xeaafba0` | `TpuExecutable_LoadProgramAndEnqueueToStream` | Exported C-shim runtime execute entry | [runtime/load-program-enqueue](../runtime/load-program-enqueue.md) · [shim/tpu-executable-roster](../shim/tpu-executable-roster.md) | CERTAIN |
| `0xf5a2900` | `xprof::tpu::GetTraceCodec` | Per-DeviceType trace-codec factory | [profiling/trace-entry-to-xevent](../profiling/trace-entry-to-xevent.md) · [profiling/overview](../profiling/overview.md) | CERTAIN |
| `0xf6993a0` | `xprof::tpu::DeviceTypeFromDeviceIdentifiers` | DeviceIdentifiers → DeviceType dispatcher | [forensics/per-gen-function-dispatcher](../forensics/per-gen-function-dispatcher.md) · [targets/pci-device-ids](../targets/pci-device-ids.md) | CERTAIN |
| `0xf795300` | `jellyfish::AutoOr<bool>::FromProtoOrDie` | AutoProto → AutoOr flag materializer | [config/registry-mediated-flags](../config/registry-mediated-flags.md) · [config/autoproto-autoor-resolution](../config/autoproto-autoor-resolution.md) | HIGH |
| `0xf849ec0` | `xla::TpuCompiler::RegisterAllPhases` | HLO pass-pipeline registration root | [runtime/internal-pass-names](../runtime/internal-pass-names.md) · [compiler/overview](../compiler/overview.md) | CERTAIN |
| `0xf874160` | `pjrt::CreatePjrtApi` | Fills 140 `PJRT_Api` slots + extension chain | [pjrt/api-vtable-reconstruction](../pjrt/api-vtable-reconstruction.md) · [pjrt/overview](../pjrt/overview.md) | CERTAIN |
| `0x1096fac0` | `jellyfish::(anon)::RunHloScheduler` | Scheduler driver behind LHS | [sched/lhs-ilp-variant](../sched/lhs-ilp-variant.md) · [config/registry-mediated-flags](../config/registry-mediated-flags.md) | HIGH |
| `0x109c6fa0` | `TensorCoreBarrierAssignment::DetermineBarrierConfigForKey` | TensorCore barrier-config solver | [barrier/tensorcore-barrier](../barrier/tensorcore-barrier.md) · [barrier/infer-barrier-config](../barrier/infer-barrier-config.md) | HIGH |
| `0x12fc3080` | `jellyfish::RunMemorySpaceAssignment` | HBM/VMEM memory-space assignment driver | [compiler/msa-overview](../compiler/msa-overview.md) · [compiler/compile-phases](../compiler/compile-phases.md) | CERTAIN |
| `0x130abfc0` | `jellyfish::CostModel::GetCollectiveCycles` | Collective-op cycle estimator | [cost/tpu-hlo-cost-analysis](../cost/tpu-hlo-cost-analysis.md) · [collectives/spmd-link-count-cost](../collectives/spmd-link-count-cost.md) | HIGH |
| `0x133c2dc0` | `ConstructConfigForCollectiveUniDirNDGroups` | UniDir-ND collective config builder | [collectives/tensor-split-ndplane](../collectives/tensor-split-ndplane.md) | HIGH |
| `0x13426260` | `jellyfish::DeepseaExecutable::LoadProgramAndEnqueueToStream` | Runtime program load + stream enqueue | [runtime/load-program-enqueue](../runtime/load-program-enqueue.md) · [runtime/overview](../runtime/overview.md) | CERTAIN |
| `0x136321a0` | `xla::LatencyHidingScheduler::RunImpl` | LHS core scheduling loop | [sched/lhs-post-layout](../sched/lhs-post-layout.md) · [front/compile-flow-walkthrough](../front/compile-flow-walkthrough.md) | CERTAIN |
| `0x137d3de0` | `TwistedTorusND::GetPhase1ReplicaGroups` | Phase-1 replica-group geometry | [routing/route-table-generation](../routing/route-table-generation.md) | HIGH |
| `0x13a33320` | `sparse_core::isa_emitter::GetVectorMask` | SparseCore vector-mask MCOperand emit | [sparsecore/scan-datapath](../sparsecore/scan-datapath.md) | HIGH |
| `0x1c6a75c0` | `jellyfish::net_util::BarrierCoresTree` | Tree-barrier core enumeration | [barrier/tree-barrier-vsync](../barrier/tree-barrier-vsync.md) · [barrier/replica-barrier](../barrier/replica-barrier.md) | HIGH |
| `0x1c89adc0` | `jellyfish::ResourceVector::Acc` | Resource-vector accumulate primitive | [cost/reduce-window-pooling-cost](../cost/reduce-window-pooling-cost.md) · [cost/convolution-cost-state](../cost/convolution-cost-state.md) | HIGH |
| `0x1c89ce20` | `jellyfish::CycleTable::GetResource` | Per-instruction resource lookup | [cost/resource-enum](../cost/resource-enum.md) · [cost/tpu-hlo-cost-analysis](../cost/tpu-hlo-cost-analysis.md) | HIGH |
| `0x1c89f820` | `jellyfish::LatencyTable::LatencyBetween` | Edge-latency accessor (scheduler) | [sched/mrb-chain-allocator](../sched/mrb-chain-allocator.md) · [cost/bundle-aware-cost](../cost/bundle-aware-cost.md) | HIGH |
| `0x1c89fba0` | `jellyfish::LatencyTable::Create` | Per-version latency-table factory | [cost/overview](../cost/overview.md) | CERTAIN |
| `0x1c8a0d60` | `jellyfish::LatencyTableJellyfish::LatencyBetweenInternal` | JF latency-edge implementation | [isa/slot-eup-transcendental](../isa/slot-eup-transcendental.md) · [cost/bundle-aware-cost](../cost/bundle-aware-cost.md) | HIGH |
| `0x1c8ae5c0` | `viperfish::MxuLatencyTable::GetResourceUsage` | MXU per-instruction resource usage | [cost/iars-per-tensorcore](../cost/iars-per-tensorcore.md) · [cost/vf-cycletable](../cost/vf-cycletable.md) | HIGH |
| `0x1d0b33e0` | `tpu::System::Execute` | Top-level async program execute | [runtime/load-program-enqueue](../runtime/load-program-enqueue.md) · [runtime/overview](../runtime/overview.md) | CERTAIN |
| `0x1d522f40` | `jellyfish::LloRegionBuilder::VsyncAddRemote` | Remote vsync sflag emitter | [barrier/remote-sflag-encoders](../barrier/remote-sflag-encoders.md) · [barrier/tree-barrier-vsync](../barrier/tree-barrier-vsync.md) | HIGH |
| `0x1d60f400` | `jellyfish::Target::LaneCount` | Per-target SIMD lane count | [targets/tpu-topology-struct](../targets/tpu-topology-struct.md) · [memory/tpu-buffer-layout](../memory/tpu-buffer-layout.md) | CERTAIN |
| `0x1d60f420` | `Target::GetGlobalBarrierSyncFlagNumber` | Reserved global-barrier sflag id | [barrier/infer-barrier-config](../barrier/infer-barrier-config.md) · [barrier/global-barrier-window](../barrier/global-barrier-window.md) | HIGH |
| `0x1d60f4e0` | `Target::GetMegacoreBarrierSyncFlagNumber` | Reserved megacore-barrier sflag id | [barrier/global-barrier-window](../barrier/global-barrier-window.md) · [barrier/barrier-to-sflag-binding](../barrier/barrier-to-sflag-binding.md) | HIGH |
| `0x1d60fc20` | `jellyfish::Target::Init` | Per-target topology initializer | [targets/tpu-topology-struct](../targets/tpu-topology-struct.md) · [barrier/infer-barrier-config](../barrier/infer-barrier-config.md) | CERTAIN |
| `0x1d615b40` | `jellyfish::Target::CoresPerChip` | Cores-per-chip by `TpuCoreType` | [barrier/tensorcore-barrier](../barrier/tensorcore-barrier.md) · [twist/megacore-even-odd](../twist/megacore-even-odd.md) | HIGH |
| `0x1d6ffae0` | `jellyfish::MemorySpaceToString` | `MemorySpace` enum → name | [isa/memory-space-enum](../isa/memory-space-enum.md) · [appendix/memory-space-table](memory-space-table.md) | CERTAIN |
| `0x1d73e640` | `jellyfish::OverrideTpuCompEnvByCmdLineFlags` | Flag → comp-env override | [appendix/flag-catalog-full](flag-catalog-full.md) · [config/registry-mediated-flags](../config/registry-mediated-flags.md) | HIGH |
| `0x1d73fcc0` | `jellyfish::SetFieldFromFlagString` | String-flag field setter | [config/overview](../config/overview.md) · [config/registry-mediated-flags](../config/registry-mediated-flags.md) | HIGH |
| `0x1e66a860` | `xla::DefaultDebugOptionsIgnoringFlags` | Hard-coded DebugOptions defaults | [config/default-debugoptions](../config/default-debugoptions.md) · [config/debugoptions-proto](../config/debugoptions-proto.md) | CERTAIN |
| `0x1e835fa0` | `tpu::TpuCodec::Create` | Per-version codec factory | [targets/tpuhal-class-hierarchy](../targets/tpuhal-class-hierarchy.md) · [targets/tpu-version-codename-matrix](../targets/tpu-version-codename-matrix.md) | CERTAIN |
| `0x1e86c7c0` | `EncoderJf::EncodeBundleInternal` | Jellyfish 32-byte bundle encoder | [isa/isa-emitter-registry](../isa/isa-emitter-registry.md) · [barnacore/bcs-32byte-bundle](../barnacore/bcs-32byte-bundle.md) | HIGH |
| `0x1fa0a900` | `BitCopy(void*, int, const void*, int, int)` | Generic bitfield copy primitive | [front/compile-flow-walkthrough](../front/compile-flow-walkthrough.md) · [barnacore/bcs-32byte-bundle](../barnacore/bcs-32byte-bundle.md) | HIGH |
| `0x204cecc0` | `tpu::driver::InitializeDriver` | Driver bring-up (bus probe, core enum) | [config/overview](../config/overview.md) · [lifecycle/tftpu-initialize-bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) | HIGH |
| `0x20ad3020` | `tpu::TpuTopology::LogicalDevicesPerChip` | Logical devices per chip by `TpuCoreType` | [twist/megacore-even-odd](../twist/megacore-even-odd.md) · [targets/tpu-topology-struct](../targets/tpu-topology-struct.md) | HIGH |
| `0x20b1b040` | `tpu::TpuChipParts::DefaultsForVersion` | Per-version chip-parts defaults | [targets/chip-parts-binarypb](../targets/chip-parts-binarypb.md) · [appendix/per-gen-comparison-matrix](per-gen-comparison-matrix.md) | CERTAIN |
| `0x20b3a480` | `tpu::TpuVersionToString` | `TpuVersion` enum → codename string | [forensics/per-gen-function-dispatcher](../forensics/per-gen-function-dispatcher.md) · [isa/isa-emitter-registry](../isa/isa-emitter-registry.md) | CERTAIN |
| `0x20ccca20` | `tensorflow::tpu::GetLibTpuInitArguments` | libtpu init-argument vector | [lifecycle/module-init-plugin-discovery](../lifecycle/module-init-plugin-discovery.md) · [appendix/flag-catalog-full](flag-catalog-full.md) | CERTAIN |
| `0x215f26f0` | `.init_array` (base) | Static-constructor pointer array | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) | CERTAIN |
| `0x215f8190` | `.init_array` (end) | End of constructor array (~2,900) | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) | CERTAIN |
| `0x21cfa9e0` | `AutoProto::_table_` neighbourhood | Adjacent AutoProto reflection tables | [config/autoproto-autoor-resolution](../config/autoproto-autoor-resolution.md) · [config/autoproto-message-arms](../config/autoproto-message-arms.md) | MEDIUM |
| `0x22048b30` | `.data.rel.ro` (end) | Relocated RO: vtables, RTTI graph | [appendix/binary-layout](binary-layout.md) | CERTAIN |
| `0x223a1320` | `opcode_info` (461 × `uint16`) | LLO opcode property/reg-file table | [isa/llo-opcode-enum](../isa/llo-opcode-enum.md) · [appendix/llo-opcode-table](llo-opcode-table.md) | CERTAIN |
| `0x224bf798` | `filewrapper_toc` (`_ZL7toc_ptr`) | Embedded-blob table of contents | [appendix/filewrapper-toc-catalog](filewrapper-toc-catalog.md) · [forensics/custom-sections](../forensics/custom-sections.md) | CERTAIN |
| `0x227ba840` | `GetTpuPjrtApi::pjrt_api` (singleton) | Cached `PJRT_Api`; first object in `.lbss` | [pjrt/api-vtable-reconstruction](../pjrt/api-vtable-reconstruction.md) · [forensics/custom-sections](../forensics/custom-sections.md) | CERTAIN |

> **GOTCHA —** the five anchors that open the table (`0x1c60480`, `0x3366d90`, `0x84a0000`, `0xb438aec`, `0xbe8af30`) sort *below* `.text`'s base `0xe63c000` even though some are 8 hex digits and look "high". They are data: `0x1c60480`/`0x3366d90` in `.lrodata`, `0x84a0000` in `.rodata`, `0xb438aec`/`0xbe8af30` in `.rodata`/`protodesc_cold`. The large-code-model `l`-flagged sections (`.lrodata` `0x01884a00`–`0x084931d0`) live in a low band reached by `movabs`, not by RIP-relative `lea`. Do not assume a small-looking VA is interior to an early function; check the section first.

> **NOTE —** the master table is the authoritative spine. The subsystem sections below restate the same anchors grouped by role with extra context; they add no new bindings. When the two disagree, the master table wins.

### How to use the index

The intended workflow is single-VA lookup. Given an address in IDA:

1. **Round to the function head.** The table keys on the symbol's entry VA. If your cursor is at `0xf874223`, the owning function is the largest table key `≤` your VA — here `CreatePjrtApi` @ `0xf874160`. The index does not list interior addresses; use the disassembly's function boundary to find the head, then look up the head.
2. **Read the band, not just the row.** [`subsystem-map`](../subsystem-map.md) pins each address band to a subsystem; if your VA is between two indexed rows, the band still tells you which subsystem section to skim. PJRT/lifecycle is `0xe6…`–`0xf8…`; the cost-model/target/jellyfish core is `0x1c8…`–`0x1d7…`; ISA encoders and per-gen factories are `0x1e8…`–`0x20b…`; the data sections are below `0xc2…` and above `0x214…`.
3. **Follow the first owning link.** The first page in **Owning page(s)** is the canonical owner — the page whose subject *is* this anchor. The second is a high-value co-citing page (a caller, a sibling, or a catalog) for context.
4. **For a data VA, expect a range.** Data anchors (`.rodata`, `.lrodata`, `protodesc_cold`, `.init_array`, `.data.rel.ro`) are cited as `[start, end)` spans on their owning pages; a single VA inside the span resolves to the section, not a symbol. [`binary-layout`](binary-layout.md) is the authority for the exact bounds.

### Scope and selection

An anchor earns a row only if it is *cross-cutting*: cited on three or more `src/` pages, or an exported entry point, or a data section that multiple subsystems reference. That rule deliberately excludes three large categories a reader will still meet in IDA — and the index says so rather than pretending to be complete:

- **Per-page locals.** The `_GLOBAL__N_` helpers, lambda bodies, and single-use rewriters that one page documents in place. They are findable from that page; duplicating them here would bury the spine.
- **ICF duplicates.** Identical-code-folding produces many byte-identical copies of small functions and data tables (the `kDeviceTypeInfo` story is the canonical example: one main copy at `0x1c60480`, 13 data duplicates). The index lists only the canonical copy.
- **Per-opcode / per-arm leaves.** The 461 LLO opcodes, the AutoProto message arms, the InstBits rows — these are *tables*, indexed once by the table anchor (`opcode_info` @ `0x223a1320`, `InstBits` @ `0x3366d90`), not row by row.

A VA that misses the index is therefore not undocumented; it is either interior to an indexed function, a per-page local on its owning page, or a member of an indexed table. Round to the head and re-look-up before concluding a gap.

---

## PJRT and Lifecycle Anchors

These are the addresses a reader hits first: the loader entry points, the static-constructor machinery, and the lazily-built API singleton. They cluster low (`0xe6…`–`0xf8…`) with the static-init data parked high in `.init_array`/`.lbss`.

| VA | Symbol | Role | Owning page | Confidence |
|---|---|---|---|---|
| `0xe635524` | `.init_proc` | `DT_INIT`; jumps into `__do_init` | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) | CERTAIN |
| `0xe63c000` | `__do_init` | Walks `.init_array`, runs constructors | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) | CERTAIN |
| `0xe6a83a0` | `GetPjrtApi` | Thin exported thunk → `GetTpuPjrtApi` | [lifecycle/get-pjrt-api-thunk](../lifecycle/get-pjrt-api-thunk.md) | CERTAIN |
| `0xe6a9d00` | `PJRT_Plugin_Initialize` | One-shot plugin-init `PJRT_Api` slot | [lifecycle/tftpu-initialize-bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) | CERTAIN |
| `0xe6aa440` | `GetTpuPjrtApi` | Builds + caches the `PJRT_Api` once | [lifecycle/get-pjrt-api-thunk](../lifecycle/get-pjrt-api-thunk.md) | CERTAIN |
| `0xe6f54a0` | `TfTpu_Initialize` | C-shim init path (non-PJRT consumers) | [lifecycle/tftpu-initialize-bootstrap](../lifecycle/tftpu-initialize-bootstrap.md) | CERTAIN |
| `0xeaafba0` | `TpuExecutable_LoadProgramAndEnqueueToStream` | Exported C-shim execute entry | [runtime/load-program-enqueue](../runtime/load-program-enqueue.md) | CERTAIN |
| `0xf874160` | `CreatePjrtApi` | Fills 140 slots (`struct_size 1120`) + ext chain | [pjrt/api-vtable-reconstruction](../pjrt/api-vtable-reconstruction.md) | CERTAIN |
| `0x204cecc0` | `driver::InitializeDriver` | Driver bring-up (bus probe, core enum) | [config/overview](../config/overview.md) | HIGH |
| `0x20ccca20` | `GetLibTpuInitArguments` | Returns the libtpu init-arg vector | [lifecycle/module-init-plugin-discovery](../lifecycle/module-init-plugin-discovery.md) | CERTAIN |
| `0x215f26f0` | `.init_array` base | Start of the constructor pointer array | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) | CERTAIN |
| `0x215f8190` | `.init_array` end | ~2,900 constructors (`DT_INIT_ARRAYSZ` 23,200 B) | [lifecycle/elf-entry-and-init-proc](../lifecycle/elf-entry-and-init-proc.md) | CERTAIN |
| `0x227ba840` | `pjrt_api` singleton | First object in `.lbss`; cached `PJRT_Api` | [pjrt/api-vtable-reconstruction](../pjrt/api-vtable-reconstruction.md) | CERTAIN |

> **QUIRK —** the only required PJRT export is `GetPjrtApi` @ `0xe6a83a0`, but it does almost nothing: it tail-calls `GetTpuPjrtApi` @ `0xe6aa440`, which lazily runs `CreatePjrtApi` @ `0xf874160` exactly once and parks the result in the static-local `pjrt_api` @ `0x227ba840`. A reimplementer chasing "where is the API table built" will bounce through three addresses in three subsystems before reaching the assembly site.

---

## Codec and Per-Generation Anchors

Per-version factories and the ISA encoders. These resolve a `TpuVersion`/`DeviceType` to a codec, a codename string, or a 32-byte encoded bundle, and sit in the upper `.text` band (`0x1e8…`–`0x20b…`) alongside the instruction-bit and opcode-property data tables.

| VA | Symbol | Role | Owning page | Confidence |
|---|---|---|---|---|
| `0xf6993a0` | `DeviceTypeFromDeviceIdentifiers` | Identifiers → DeviceType dispatcher | [forensics/per-gen-function-dispatcher](../forensics/per-gen-function-dispatcher.md) | CERTAIN |
| `0x1e835fa0` | `TpuCodec::Create` | `TpuVersion` → codec instance | [targets/tpuhal-class-hierarchy](../targets/tpuhal-class-hierarchy.md) | CERTAIN |
| `0x1e86c7c0` | `EncoderJf::EncodeBundleInternal` | Jellyfish bundle → 32 bytes | [isa/isa-emitter-registry](../isa/isa-emitter-registry.md) | HIGH |
| `0x1fa0a900` | `BitCopy` | Generic bitfield blit used by encoders | [barnacore/bcs-32byte-bundle](../barnacore/bcs-32byte-bundle.md) | HIGH |
| `0x20b1b040` | `TpuChipParts::DefaultsForVersion` | Per-version chip-parts defaults | [targets/chip-parts-binarypb](../targets/chip-parts-binarypb.md) | CERTAIN |
| `0x20ad3020` | `TpuTopology::LogicalDevicesPerChip` | Logical devices per chip by core type | [twist/megacore-even-odd](../twist/megacore-even-odd.md) | HIGH |
| `0x20b3a480` | `TpuVersionToString` | `TpuVersion` → codename string | [forensics/per-gen-function-dispatcher](../forensics/per-gen-function-dispatcher.md) | CERTAIN |
| `0x223a1320` | `opcode_info` (data) | 461 × `uint16` LLO opcode property words | [isa/llo-opcode-enum](../isa/llo-opcode-enum.md) | CERTAIN |
| `0x3366d90` | `InstBits` (data) | Per-opcode TensorCore encoding bits | [isa/instbits-master-db](../isa/instbits-master-db.md) | CERTAIN |

> **GOTCHA —** the seed shorthand "`DefaultsForVersion@0x20b1b040`" resolves to `tpu::TpuChipParts::DefaultsForVersion`, not a free function. A reimplementer must enter it through the `TpuChipParts` aggregate; there is no standalone `DefaultsForVersion` symbol at that VA.

---

## Cost-Model Anchors

The cost-model spine: the latency/cycle/resource tables and accumulators the scheduler queries. They live in a tight `jellyfish`/`viperfish` band (`0x1c89…`–`0x1c8a…`) with the priced LUT data down in `.rodata` near `0xb438…`.

| VA | Symbol | Role | Owning page | Confidence |
|---|---|---|---|---|
| `0xb438aec` | `resLUT` (data) | VF resource cycle LUT (priced cells) | [cost/vf-cycletable](../cost/vf-cycletable.md) | HIGH |
| `0x130abfc0` | `CostModel::GetCollectiveCycles` | Collective-op cycle cost | [cost/tpu-hlo-cost-analysis](../cost/tpu-hlo-cost-analysis.md) | HIGH |
| `0x1c89adc0` | `ResourceVector::Acc` | Resource-vector accumulate | [cost/reduce-window-pooling-cost](../cost/reduce-window-pooling-cost.md) | HIGH |
| `0x1c89ce20` | `CycleTable::GetResource` | Per-instruction resource lookup | [cost/resource-enum](../cost/resource-enum.md) | HIGH |
| `0x1c89f820` | `LatencyTable::LatencyBetween` | Edge latency for scheduler | [sched/mrb-chain-allocator](../sched/mrb-chain-allocator.md) | HIGH |
| `0x1c89fba0` | `LatencyTable::Create` | Per-version latency-table factory | [cost/overview](../cost/overview.md) | CERTAIN |
| `0x1c8a0d60` | `LatencyTableJellyfish::LatencyBetweenInternal` | JF latency-edge implementation | [isa/slot-eup-transcendental](../isa/slot-eup-transcendental.md) | HIGH |
| `0x1c8ae5c0` | `MxuLatencyTable::GetResourceUsage` | MXU per-instruction resource usage | [cost/iars-per-tensorcore](../cost/iars-per-tensorcore.md) | HIGH |

---

## Target and Memory Anchors

The `jellyfish::Target` accessors that a reimplementer hits constantly — lane count, cores-per-chip, reserved sync-flag numbers — plus the memory-space stringifier. All in the `0x1d6…` band.

| VA | Symbol | Role | Owning page | Confidence |
|---|---|---|---|---|
| `0x1d60f400` | `Target::LaneCount` | SIMD lanes for the target | [targets/tpu-topology-struct](../targets/tpu-topology-struct.md) | CERTAIN |
| `0x1d60f420` | `Target::GetGlobalBarrierSyncFlagNumber` | Reserved global-barrier sflag | [barrier/infer-barrier-config](../barrier/infer-barrier-config.md) | HIGH |
| `0x1d60f4e0` | `Target::GetMegacoreBarrierSyncFlagNumber` | Reserved megacore-barrier sflag | [barrier/global-barrier-window](../barrier/global-barrier-window.md) | HIGH |
| `0x1d60fc20` | `Target::Init` | Per-target topology initializer | [targets/tpu-topology-struct](../targets/tpu-topology-struct.md) | CERTAIN |
| `0x1d615b40` | `Target::CoresPerChip` | Cores-per-chip by `TpuCoreType` | [barrier/tensorcore-barrier](../barrier/tensorcore-barrier.md) | HIGH |
| `0x1d6ffae0` | `MemorySpaceToString` | `MemorySpace` enum → name | [isa/memory-space-enum](../isa/memory-space-enum.md) | CERTAIN |

> **NOTE —** `Target::Init` @ `0x1d60fc20` is the constructor that populates the object every other `Target::` accessor reads from; the accessors at `0x1d60f4xx` are trivial getters over fields set there. Trace data-flow from `Init`, not from the getters.

---

## Barrier, Collective, and Routing Anchors

Synchronisation and collective-geometry machinery: barrier-config solvers, the tree-barrier core enumerator, the remote vsync encoder, and the twisted-torus replica-group geometry. Spread across `0x109…`, `0x133…`–`0x137…`, and `0x1c6…`–`0x1d5…`.

| VA | Symbol | Role | Owning page | Confidence |
|---|---|---|---|---|
| `0x109c6fa0` | `TensorCoreBarrierAssignment::DetermineBarrierConfigForKey` | Barrier-config solver | [barrier/tensorcore-barrier](../barrier/tensorcore-barrier.md) | HIGH |
| `0x133c2dc0` | `ConstructConfigForCollectiveUniDirNDGroups` | UniDir-ND collective config | [collectives/tensor-split-ndplane](../collectives/tensor-split-ndplane.md) | HIGH |
| `0x137d3de0` | `TwistedTorusND::GetPhase1ReplicaGroups` | Phase-1 replica-group geometry | [routing/route-table-generation](../routing/route-table-generation.md) | HIGH |
| `0x13a33320` | `isa_emitter::GetVectorMask` | SparseCore vector-mask emit | [sparsecore/scan-datapath](../sparsecore/scan-datapath.md) | HIGH |
| `0x1c6a75c0` | `net_util::BarrierCoresTree` | Tree-barrier core enumeration | [barrier/tree-barrier-vsync](../barrier/tree-barrier-vsync.md) | HIGH |
| `0x1d522f40` | `LloRegionBuilder::VsyncAddRemote` | Remote vsync sflag emitter | [barrier/remote-sflag-encoders](../barrier/remote-sflag-encoders.md) | HIGH |

---

## Compiler, Scheduler, and Runtime Anchors

Pipeline registration, the latency-hiding scheduler core, flag/comp-env plumbing, and the runtime execute path. The compiler/config code sits in the `0xf8…`/`0x1d7…`/`0x1e6…` bands; the scheduler and runtime in `0x10…`–`0x13…`.

| VA | Symbol | Role | Owning page | Confidence |
|---|---|---|---|---|
| `0xf795300` | `AutoOr<bool>::FromProtoOrDie` | AutoProto → flag materializer | [config/autoproto-autoor-resolution](../config/autoproto-autoor-resolution.md) | HIGH |
| `0xf849ec0` | `TpuCompiler::RegisterAllPhases` | HLO pass-pipeline registration | [runtime/internal-pass-names](../runtime/internal-pass-names.md) | CERTAIN |
| `0x1096fac0` | `(anon)::RunHloScheduler` | Scheduler driver | [sched/lhs-ilp-variant](../sched/lhs-ilp-variant.md) | HIGH |
| `0x12fc3080` | `RunMemorySpaceAssignment` | HBM/VMEM memory-space assignment | [compiler/msa-overview](../compiler/msa-overview.md) | CERTAIN |
| `0x136321a0` | `LatencyHidingScheduler::RunImpl` | LHS core scheduling loop | [sched/lhs-post-layout](../sched/lhs-post-layout.md) | CERTAIN |
| `0x13426260` | `DeepseaExecutable::LoadProgramAndEnqueueToStream` | Program load + enqueue | [runtime/load-program-enqueue](../runtime/load-program-enqueue.md) | CERTAIN |
| `0x1d0b33e0` | `System::Execute` | Top-level async execute | [runtime/load-program-enqueue](../runtime/load-program-enqueue.md) | CERTAIN |
| `0x1d73e640` | `OverrideTpuCompEnvByCmdLineFlags` | Flag → comp-env override | [appendix/flag-catalog-full](flag-catalog-full.md) | HIGH |
| `0x1d73fcc0` | `SetFieldFromFlagString` | String-flag field setter | [config/overview](../config/overview.md) | HIGH |
| `0x1e66a860` | `DefaultDebugOptionsIgnoringFlags` | Hard-coded DebugOptions defaults | [config/default-debugoptions](../config/default-debugoptions.md) | CERTAIN |

---

## Profiling and Data-Section Anchors

The trace pipeline factory, the per-DeviceType info table, and the large read-only data sections a reader will scroll into without a function under the cursor. These are the anchors most likely to be cited *as ranges* rather than single VAs.

| VA | Symbol / section | Role | Owning page | Confidence |
|---|---|---|---|---|
| `0x84a0000` | `.rodata` name pool (base) | Merged NUL-terminated string pool | [appendix/filewrapper-toc-catalog](filewrapper-toc-catalog.md) | HIGH |
| `0xbe8af30` | `protodesc_cold` (section, → `0xc1bf0b0`) | Serialized `FileDescriptorProto` blobs | [appendix/protodesc-cold-catalog](protodesc-cold-catalog.md) | CERTAIN |
| `0xf5a2900` | `GetTraceCodec` | Per-DeviceType trace-codec factory | [profiling/trace-entry-to-xevent](../profiling/trace-entry-to-xevent.md) | CERTAIN |
| `0x1c60480` | `kDeviceTypeInfo` (`.lrodata`, → `0x1c64d48`) | `DeviceTypeInfo[17]` frozen table | [profiling/kdevicetypeinfo-producer-readers](../profiling/kdevicetypeinfo-producer-readers.md) | CERTAIN |
| `0x21cfa9e0` | `AutoProto::_table_` neighbourhood (`.data.rel.ro`) | Adjacent AutoProto reflection tables | [config/autoproto-autoor-resolution](../config/autoproto-autoor-resolution.md) | MEDIUM |
| `0x22048b30` | `.data.rel.ro` (end) | Relocated RO: vtables + RTTI graph | [appendix/binary-layout](binary-layout.md) | CERTAIN |
| `0x224bf798` | `filewrapper_toc` (`_ZL7toc_ptr`, 488 B) | Embedded-blob table of contents | [appendix/filewrapper-toc-catalog](filewrapper-toc-catalog.md) | CERTAIN |

> **QUIRK —** `kDeviceTypeInfo` @ `0x1c60480` is `.lrodata` (`perm 4`, read-only-no-write) with **zero** `.rela.dyn` entries across `[0x1c60480, 0x1c64d48)` — it is frozen at link time and has no runtime producer. The 13 byte-identical copies elsewhere in `.lrodata` are ICF data duplicates, not independent tables; index only the main copy.

> **CORRECTION (ANCHOR-1) —** the data anchors `0x84a0000` (name pool), `0xb438aec` (resLUT), `0x223a1320` (opcode_info), `0x3366d90` (InstBits), `0x224bf798` (filewrapper_toc), `0x227ba840` (pjrt_api), `0x215f26f0`/`0x215f8190` (`.init_array`), and `0x22048b30` (`.data.rel.ro` end) do **not** resolve to symbols in `…_function_addresses.json` — that file lists code functions only. They are data/section anchors and were verified instead against the owning page's section/symbol citation (and, for `kDeviceTypeInfo` and `pjrt_api`, against named symbols `_ZN5xprofL15kDeviceTypeInfoE` and `_ZL7toc_ptr`). An address that fails the function-table lookup is not therefore invalid; check whether the owning page pins it as data before treating a miss as a defect.

---

## Cross-References

- [Subsystem Map](../subsystem-map.md) — the forward index: address band → subsystem; pairs with this flat reverse index
- [Codename Cheatsheet](../front/codename-cheatsheet.md) — codename → per-gen facts; many anchors here are per-gen factory functions cited there
- [Cross-Reference Graph](cross-reference-graph.md) — page → page link graph; use it to walk from an owning page to its neighbours
- [Symbol Namespace Index](symbol-namespace-index.md) — namespace census (`xla::jellyfish`, `xprof::tpu`, …) behind the symbols indexed here
- [Binary Layout](binary-layout.md) — full segment/section table; the authority for every data-section range cited above
- [Per-Gen Comparison Matrix](per-gen-comparison-matrix.md) — collects the per-version factory anchors (`TpuCodec::Create`, `DefaultsForVersion`, `TpuVersionToString`) into one table
- [Get-PjrtApi Thunk](../lifecycle/get-pjrt-api-thunk.md) — the three-hop PJRT entry path the lifecycle anchors trace
- [PJRT API-Vtable Reconstruction](../pjrt/api-vtable-reconstruction.md) — the 140-slot table `CreatePjrtApi` fills and the `pjrt_api` singleton caches
