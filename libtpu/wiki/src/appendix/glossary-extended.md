# Extended Glossary

> *All names and symbols on this page apply to `libtpu.so` v0.103 from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped: every C++ symbol survives as a demangled name, so a "term is grounded" claim here means the literal string or symbol is present in the binary's name/string tables. Other builds will differ.*

## Abstract

This is the **long-tail** companion to the root [Glossary](../glossary.md). The root page is the tight ~83-term quick reference — codenames, compute units, memory spaces, IR names, ABI terms — the vocabulary every page links back to. This page is the deep-reference index of *named things*: the concrete struct/class names, the enum families, the ISA-page abbreviations, the profiler/trace nouns, and the collective/network terms that a reimplementer meets only once they are inside the deep ISA, cost-model, scheduler, routing, and profiling pages. None of these warranted a slot in the quick reference, but every one of them is a symbol a reader will eventually grep for and need defined.

The split is deliberate and non-overlapping. The root glossary defines `MXU`, `SCS`, `LLO`, `PJRT`, `HBM`, `TpuVersion`. This page defines the *implementation surface* below those: the `tpu::Target` capability object the codename axis resolves to, the `CycleTable`/`MxuLatencyTable` pair the cost model reads, the `MatmulModeAttr`/`GainLatchModeAttr` MLIR enums the MXU slot encodes, the `Iar`/`Msr`/`Lmr` register abbreviations the matrix-push pipeline cycles through, the `XLineBuilder`/`GtcSpan` trace plumbing, and the `TwistedTorusND`/`ToroidalRouteCache`/`VirtualChannel` routing machinery. If the root glossary answers *"what does this acronym mean,"* this page answers *"what is the named C++ entity behind it, and which page owns it."*

Every entry that names a concrete binary entity was re-confirmed against the name table before being defined; the parenthetical hit counts are sidecar matches. A handful of terms that recur in TPU collective literature but are **not** present as TPU symbols in this binary (most notably `RDH`) are kept as flagged absences so a later page does not silently treat them as grounded. Definitions stay to one or two sentences plus a deep-page pointer; the deep page owns the algorithm, the layout, and the rationale.

For navigation, the contract is:

- **This page EXTENDS, never duplicates, the root [Glossary](../glossary.md)** — the 83 quick-reference terms live there; do not re-define `MXU`/`SCS`/`LLO`/`PJRT`/`HBM` here.
- **Every binary-grounded term cites its grounding** — a class symbol, an enum, an `Attr` storage, or the owning deep page.
- **Uncertain or external terms are flagged** with `(inferred)`, `(external)`, or `(not in binary)`; they are never given a fake anchor.
- **Each term ends in a pointer** to the deep page that owns its mechanism.

| | |
|---|---|
| **Parent reference** | [Glossary](../glossary.md) — the ~83-term quick reference this page extends |
| **Codename card** | [Codename Cheat-Sheet](../front/codename-cheatsheet.md) — the three-axis codename binding |
| **Term clusters** | Structs & Classes · Enum Families · ISA Abbreviations · Profiling Terms · Collective Terms · Misc |
| **Grounding** | All counts are name-table sidecar hits in the unstripped `libtpu.so` |
| **Deep-page roots** | [`isa/`](../isa/overview.md) · [`cost/`](../cost/overview.md) · [`sched/`](../sched/overview.md) · [`profiling/`](../profiling/overview.md) · [`collectives/`](../collectives/overview.md) · [`routing/`](../routing/overview.md) · [`twist/`](../twist/overview.md) |

---

## Structs & Classes

The named C++ types a reimplementer instantiates or models. These are the spine of the deep pages — the capability object the codename resolves to, the cost tables, the IR builders, and the allocators. Grounding is the demangled class symbol.

| Term | Definition |
|---|---|
| **`tpu::Target`** | The per-generation **capability object** — a giant accessor bag (`Target::AccumulatorsPerTensorCore`, `Target::AllReduceScopedMemoryLimitBytes`, `Target::AllSublanesMask`, `Target::AccessesToSameWordIncurCrossSlotVmemBankConflicts`, …) that every cost/ISA/scheduler pass queries instead of switching on `TpuVersion` directly. `Target` appears 66,365 times. The codename axis ultimately resolves *to* a `Target`. Owned by [Target Capability Bitfield](../targets/target-capability-bitfield.md) and [Per-Codename HW Constants](../targets/per-codename-hw-constants.md). |
| **`SparseCoreTarget`** | The SparseCore-side sibling of `Target` (296 hits) — carries SparseCore-private layout constants (`SparseCoreSpmemStripeGranularityBytes`, `SparseCoreStartReservedSmemWordOffset`, `SparseCoreStartReservedTecSmemWordOffset`, `SparseCoreParamPtrLocationWordOffset`). Built per-module via `SparseCoreTargetForModule`. Owned by [SparseCore Target Descriptor](../targets/sparsecore-target-descriptor.md). |
| **`TpuCoreParts`** | The per-chip core-inventory struct (269 hits) describing how a physical chip decomposes into TensorCore + SparseCore parts (megacore pairing, core counts). Owned by [Sub-Core Taxonomy](../targets/sub-core-taxonomy.md). |
| **`TpuTopology`** | The pod/slice topology descriptor (3,568 hits) — the chip-grid dimensions, wrap, and core layout that routing and collective placement read. Owned by [TpuTopology Struct](../targets/tpu-topology-struct.md). |
| **`MxuLatencyTable`** | The MXU-issue latency lookup table (42 hits); `GetSharedMxuLatencyTable` returns a per-platform singleton consumed by the matmul cost path. Owned by [MXU Latency Overview](../cost/mxu-latency-overview.md). |
| **`CycleTable`** | The per-opcode cycle-cost table (463 hits); selected per platform via a `CycleTableCreator` registry — `"No CycleTable registered for platform"` is the miss path. The cost model's primary per-instruction timing source. Owned by [CycleTable Family](../cost/cycletable-family.md). |
| **`IsaEmitter`** | The bundle-encoding emitter (865 hits) that lowers scheduled LLO into per-generation instruction bits; one registered emitter per codec. Owned by [ISA Emitter Registry](../isa/isa-emitter-registry.md). |
| **`LloRegionBuilder`** | The LLO-region construction helper (15,021 hits) that assembles the per-bundle LLO IR regions ahead of scheduling/emission. Owned by [Bundle Model Overview](../isa/bundle-model-overview.md); see also [LLO Opcode Enum](../isa/llo-opcode-enum.md). |
| **`ConversionPatternRewriter`** | The MLIR dialect-conversion rewriter (30,571 hits, with its `ConversionPatternRewriterImpl`) that drives the MHLO→TPU→LLO legalization rewrites. Standard MLIR machinery, heavily instantiated here. Owned by [Conversion Pattern Rewriter](../compiler/conversion-pattern-rewriter.md) and [Dialect Conversion Legalizer](../compiler/dialect-conversion-legalizer.md). |
| **`MrbChainAllocator`** | The Matrix-Result-Buffer reservation allocator (38 hits) — a **time-ordered** reservation manager (`AdvanceTimeTo`, `ExtendMrbReservation`, `ReleaseMrbReservation`) that chains MRB lifetimes across the schedule. Owned by [MRB Chain Allocator](../sched/mrb-chain-allocator.md); placement in [MRB FIFO / MSR Placement](../sched/mrb-fifo-msr-placement.md). |
| **`TwistedTorusND`** | The N-dimensional twisted-torus topology model (87 hits; `TwistedTorus*` 353) used by the routing layer to describe pods whose ICI mesh is offset-wrapped rather than plainly toroidal. Owned by [Twist Overview](../twist/overview.md). |
| **`ToroidalRouteCache`** | The memoized route store for toroidal/twisted-torus paths (271 hits) — caches computed ICI routes so the route generator does not re-derive them per collective. Owned by [Toroidal Route Cache](../routing/toroidal-route-cache.md); codec in [Route Cache Codec](../routing/route-cache-codec.md). |
| **`ContinuationQueue`** | A SyncFlag-backed work queue (479 hits) whose available-count is tracked in a sync-flag word (`ContinuationQueueAvailableCount`; "All ContinuationQueues must be the same size"). The hardware queue the sequencer drains continuations from. Documented under [SFLAG Protocol](../memory/sflag-protocol.md). |
| **`StaticMapBase` / `StaticMap`** | The compile-time perfect-hash / frozen-map base (621 / 630 hits) used for the read-only string→id and id→handler tables (opcode names, metadata ids). A reverse-engineering landmark: a `StaticMapBase` instance flags a baked-in lookup table. See [Instr Name Data](../isa/instr-name-data.md). |
| **`AutoOr<T>`** | A status-or-value / flag wrapper template (3,322 hits; `AutoOr<bool>`, `AutoOr<double>`, `AutoOr<float>`) with an `AutoOrFromString` parser. Carries a parsed knob value *or* a default; the config layer's option-value container. Owned by [AutoOr Parse Grammar](../config/autoor-parse-grammar.md) and [AutoOr Unparse](../config/autoor-unparse.md). |
| **`AutoProto`** | The companion proto-backed auto-config container (1,130 hits) holding the structured (protobuf) form of an auto-tuned/auto-configured option. Paired with `AutoOr` in the config surface. Owned by [AutoProto/AutoOr Resolution](../config/autoproto-autoor-resolution.md) and [AutoProto Message Arms](../config/autoproto-message-arms.md). |

> **NOTE — `tpu::Target` is the real "codename" payload.** The root glossary's three integer axes (`TpuVersion` / `DeviceType` / `TpuVersionProto`) are just *selectors*; the object they select is a `tpu::Target` (or `SparseCoreTarget`). A reimplementer who treats the codename as an `enum` and stops there misses that every per-generation constant — accumulator count, sublane mask, scoped-memory limit, bank-conflict rule — lives as a `Target` accessor, not in a flat table.

---

## Enum Families

The enum families the ISA pages encode. In this binary the per-bundle ISA enums are realized as **MLIR enum attributes** — each is a `…Attr` with a `…AttrStorage` (e.g. `MatmulModeAttr`, `MatmulModeAttrStorage`), so a grep for the bare name plus `Attr` confirms it is an op-attribute enum rather than a plain C `enum`. Grounding is the attribute symbol.

| Term | Definition |
|---|---|
| **`MatmulMode`** | The MXU matmul operating-mode enum (457 hits; `MatmulModeAttr`) — selects the contraction/accumulation mode the matrix slot runs in. Owned by [MXU Slot](../isa/slot-mxu.md); cost modifiers in [Matmul Mode Modifiers](../cost/matmul-mode-modifiers.md). |
| **`MatmulDataFormat`** | The MXU operand data-format enum (954 hits; `MatmulDataFormatAttr`, with a `MatmulDataFormatAndScalingFactor` pairing) — the element type / packing the matmul feeds (bf16, int8, fp8, …). Owned by [MXU Slot](../isa/slot-mxu.md); precision packing in [Pack/Unpack Precision](../isa/pack-unpack-precision.md). |
| **`MatpushModifier`** | The matrix-push modifier enum (80 hits) — per-`matpush` flags that tune how operands are latched into the MXU front-end. Owned by [MatPrep / IAR Latch Slot](../isa/slot-matprep-iar-latch.md). |
| **`GainLatchMode`** | The MXU gain/scale latch-mode enum (535 hits; `GainLatchModeAttr`, `GainLatchModeAndScalingFactor`) — controls how the per-row gain/scaling factor is latched alongside the data format. Owned by [MXU Slot](../isa/slot-mxu.md). |
| **`ResultFifo`** | The MXU result-FIFO selector enum (408 hits) — which output FIFO a matmul result drains into before write-back. Owned by [ResultFifo / ArchRegister](../isa/resultfifo-archregister.md). |
| **`ArchRegister`** | The architectural-register identity enum (164 hits) — the named hardware register slots the encoder references (paired with `ResultFifo` on the output side). Owned by [ResultFifo / ArchRegister](../isa/resultfifo-archregister.md); numbering in [ArchRegNo Numbering](../isa/archregno-numbering.md). |
| **`VxposeMode`** | The vector-transpose mode enum (204 hits; `VxposeModeAttr`, with variants `VxposeBinary`/`VxposeClear`/`VxposeCont`/`VxposeEnd`) — the XLU transpose op's phase/mode selector. Owned by [XLU Op Roster](../isa/xlu-op-roster.md). |
| **`MemorySpace`** | The LLO memory-space enum (9,213 hits) — the address-space tag (HBM/VMEM/SMEM/CMEM/Spmem/…) carried on every memory op. The deep enumeration of the root glossary's *Memory Spaces* group. Owned by [Memory-Space Enum](../isa/memory-space-enum.md). |
| **`TpuSequencerType`** | The sequencer-type enum (1,017 hits) — TensorCore vs the SparseCore SCS/TAC/TEC sequencers; the axis the codec template instantiates per sequencer. **Two off-by-one numberings** (codec-template vs proto/runtime), see the root glossary's SCS/TAC/TEC trap. Owned by [Sequencer Ops Per Gen](../isa/sequencer-ops-per-gen.md). |

> **GOTCHA — these are op-attributes, not flat C enums.** Each `Matmul*`/`GainLatchMode`/`Vxpose` enum exists as an MLIR `…Attr` with a generated `…AttrStorage` and a `…AndScalingFactor` companion. A reimplementer encoding the bundle from a plain integer enum will miss the *paired* scaling-factor/storage that the attribute carries — the data format and the gain mode are latched together (`MatmulDataFormatAndScalingFactor`, `GainLatchModeAndScalingFactor`), not independently.

---

## ISA Abbreviations

The short register/latch abbreviations that the ISA-slot pages use without expansion. Each is the matrix-push / encoder pipeline's name for a specific hardware register class or latch state. Grounding is the camel-cased symbol family — **note the binary uses CamelCase** (`Iar`, `Msr`), so an uppercase grep (`IAR`) returns zero.

| Abbr | Expansion / Definition |
|---|---|
| **IAR** (`Iar`) | **I**nput **A**ctivation **R**egister — the MXU front-end operand-latch register the matrix-push pipeline fills (17,415 `Iar` hits; `IarsPerTensorcore`, `IarHasValue`, even/odd `LoadIar`/`StoreIar` initialization). The count of IARs per TensorCore is a `Target` constant. Owned by [MatPrep / IAR Latch Slot](../isa/slot-matprep-iar-latch.md); capacity in [IARs Per TensorCore](../cost/iars-per-tensorcore.md). |
| **MRB** | **M**atrix-**R**esult **B**uffer — the buffer holding MXU outputs before write-back (`Mrb`, 1,151 hits). Allocated by `MrbChainAllocator` (see Structs above). Owned by [MRB Chain Allocator](../sched/mrb-chain-allocator.md). |
| **MSR** (`Msr`) | **M**atrix **S**taging **R**egister (42,358 `Msr` hits) — the staging register the matrix pipeline inserts/reserves (`Matrix Staging Register`, `reservedforlgmr`); convertible to/from `Xmr`. Distinct from the `RDH*`/`SMRD` LLVM hits, which are unrelated statically-linked code. Owned by [MRB FIFO / MSR Placement](../sched/mrb-fifo-msr-placement.md). |
| **VEX** (`Vex`) | **V**ector-**EX**tended unit — the SparseCore vector datapath (3,340 `Vex` hits). Already in the root glossary's *Compute Units*; listed here as the abbreviation a SparseCore-ISA reader resolves. Owned by [VectorExtended (vex)](../sparsecore/vectorextended-vex.md). |
| **VREG** (`Vreg`) | **V**ector **REG**ister — the VPU's lane-vectorized register file (73,463 hits). The operand class VPU/XLU slots read and write. Owned by [VPU Slot](../isa/slot-vpu.md). |
| **SREG** (`Sreg`) | **S**calar **REG**ister — the SPU's scalar register file (60,431 hits). Owned by [SPU Scalar Slot](../isa/slot-spu-scalar.md). |
| **LMR** (`Lmr`) | **L**oad-**M**atrix **R**egister width — the matrix-load register width selector on `matpush`/`loadmatrix` (5,534 hits; `LoadmatrixLmrWidth`, `MatrixMultiplyLmrWidth`, "Invalid LMR width for platform"). A per-platform-validated field. Owned by [MatPrep / IAR Latch Slot](../isa/slot-matprep-iar-latch.md). |
| **CBREG** (`Cbreg`) | **C**onstant/**B**ase **REG**ister — a base-plus-offset register class (1,586 hits; `CbregBase`, `CbregOffset`, `CbregSize`) used for addressing/constant operands. Owned by [Slot Immediate](../isa/slot-immediate.md). |
| **LSF** (`Lsf`) | A **L**atch-**S**tate **F**lag on the vector matrix-push (42 hits; `VmatpushLsf`, `LsfLatchMode`, "Instruction Vmatpush Lsf not supported on this platform") — a per-platform-gated latch modifier. Owned by [Encoder Latch Serialization](../sched/encoder-latch-serialization.md). |
| **RPU** (`Rpu`) | The vector-**R**otate / cross-lane op kind (571 hits; `RpuOp`, `FirstRpuInstOpcode Lloopcode KVectorRotate`, "should have only TransposeTiles and Rpu operations") — an XLU-family rotate operation. Owned by [XLU Op Roster](../isa/xlu-op-roster.md). |

> **CORRECTION (GLOSS-X1) — `IAR` is `Iar`, not `IAR`.** A literal `IAR` (all-caps) has **zero** hits in the name table; the grounded symbol is the CamelCase `Iar` (17,415 hits). Likewise `MatPush` is absent — the binary spells it `Matpush` (292). Grep with the binary's casing or conclude the feature is missing when it is not.

---

## Profiling Terms

The named nouns in the trace/profiling plumbing below the root glossary's XProf-schema entries (`XPlane`/`XEvent`/`XStat`/`XSpace`/`TraceMe`). These are the *builders* and *spans* a profiler-page reader meets. Grounding is the class symbol.

| Term | Definition |
|---|---|
| **`TraceEntry`** | The per-core raw trace record (23,704 hits) the hardware/sequencer emits — the pre-XEvent form a `CoreDispatcher` decodes per codec family. Owned by [Trace Entries Coder](../profiling/trace-entries-coder.md); conversion in [Trace Entry to XEvent](../profiling/trace-entry-to-xevent.md). |
| **`XLine` / `XLineBuilder`** | A single timeline row within an `XPlane` (358 `XLine` hits); `XLineBuilder::AddEvent` appends the converted `XEvent`s onto it. The row a `TraceEntry` stream lands on. Owned by [XPlane / XStat / TraceMe](../profiling/xplane-xstat-traceme.md). |
| **`GtcSpan`** | A **G**lobal **T**ime **C**ounter span (469 hits; `GetEntriesGtcSpan`, `GetTraceDroppingGtcSpan`) — the hardware-timestamp window a batch of trace entries covers, used to align per-core clocks and to report dropped-trace intervals. Owned by [Trace Entries Coder](../profiling/trace-entries-coder.md). |
| **riegeli** | The record-container format (14,622 `riegeli` hits) the trace blobs are serialized into — a framed, optionally-compressed record stream. The on-disk envelope around the XProf payload. Owned by [Riegeli Trace Container](../profiling/riegeli-trace-container.md). |
| **`StaticMap` (metadata ids)** | The frozen lookup the profiler uses for `XEvent`/`XStat` metadata-id resolution (see Structs); a profiler-side `StaticMapBase` maps stat names to integer ids. Owned by [XEvent Metadata IDs](../profiling/xevent-metadata-ids.md) and [XStat Metadata IDs](../profiling/xstat-metadata-ids.md). |

---

## Collective Terms

The fabric/topology nouns a collectives or routing page uses. Several are realized as the routing classes already listed under *Structs* (`TwistedTorusND`, `ToroidalRouteCache`); the entries here are the descriptive terms and the `VirtualChannel` machinery. Grounding is noted per row.

| Term | Definition |
|---|---|
| **VC / `VirtualChannel`** | **V**irtual **C**hannel — an ICI link's logical sub-channel (366 `VirtualChannel` hits; `VirtualChannelConfiguration`). Balanced across links by the VC allocator (`VcBalanceThreshold`, `VcBalanceUsage`) to avoid head-of-line blocking on a shared physical link. Owned by [VC Balance Allocation](../ici/vc-balance-allocation.md). |
| **twisted torus** | A torus topology whose wrap-around is offset (sheared) rather than aligned (18 string hits "twisted torus"; class `TwistedTorusND`). Lets a pod present a balanced-diameter mesh that a plain torus cannot; the routing layer models it explicitly. Owned by [Twist Overview](../twist/overview.md). |
| **n-hop** | The multi-hop ICI routing mode (grounded as `NHopRoutingTableGenerator`, `EnableNHopRouting`, `IsNHopRouting`, and the `tpu_slice_builder_ici_route_force_n_hop` flag) — a route that traverses several ICI links rather than a single direct hop, used when topology or link faults preclude a 1-hop path. Owned by [Route Table Generation](../routing/route-table-generation.md) and [Get Static Path](../routing/get-static-path.md). |
| **megacore** | The two-TensorCore-per-chip execution mode (`Megacore`, 1,562 hits; "megacore" 442 string hits) — the pairing that lets a collective fuse across both cores of a chip. Owned by [Megacore Fusion](../collectives/megacore-fusion.md); the even/odd core split in [Megacore Even/Odd](../twist/megacore-even-odd.md). |
| **RDH** | **R**edundant-**D**ata **H**andling — a TPU-pod collective-resiliency concept in external literature. `(not in binary as a TPU term)`: the only `RDH`/`Rdh*` symbols in this binary are statically-linked LLVM hits (`checkSMRDHazards`, `Rdhi`/`Rdlo` register ops), unrelated to collectives. Do not anchor a resiliency claim to `RDH`; use the grounded `n-hop` / degraded-axis machinery instead. See [Degraded Axis](../collectives/degraded-axis.md). |

> **NOTE — twisted torus, n-hop, and megacore are the resiliency triad here.** When an ICI link or a sub-cube fails, the routing layer reaches a working path by combining the twisted-torus shear, n-hop multi-link routing, and (for compute) the megacore even/odd split — all three grounded above. The conventional "RDH" framing is **not** how this binary names that mechanism.

---

## Misc

Terms that span clusters or name reverse-engineering artifacts a deep-page reader meets.

| Term | Definition |
|---|---|
| **`…AttrStorage`** | The MLIR generated storage struct behind every op-attribute enum (`MatmulModeAttrStorage`, `GainLatchModeAttrStorage`, …). Seeing a `…AttrStorage` symbol confirms the adjacent `…Attr` is a dialect-registered enum attribute, not a plain integer. Reverse-engineering landmark for the *Enum Families* cluster above. |
| **`…AndScalingFactor`** | The paired-encoding suffix (`MatmulDataFormatAndScalingFactor`, `GainLatchModeAndScalingFactor`) marking enums that latch a scaling factor *together* with the mode in one attribute. The signal that an MXU mode is not independent of its scale. See [MXU Slot](../isa/slot-mxu.md). |
| **`Matpush` / `Vmatpush`** | The matrix-push instruction family (`Matpush` 292 hits) that latches operands into the MXU IARs; the vector form `Vmatpush` carries the `Lsf` latch flag. Spelled CamelCase, not `MatPush`. Owned by [MatPrep / IAR Latch Slot](../isa/slot-matprep-iar-latch.md). |
| **`CycleTableCreator`** | The per-platform factory that builds the right `CycleTable`; its miss diagnostic ("No CycleTable registered for platform") is the hook that proves the cost model is platform-dispatched. See [CycleTable Family](../cost/cycletable-family.md). |
| **`GetSharedMxuLatencyTable`** | The accessor returning the process-wide `MxuLatencyTable` singleton; the entry point the matmul cost path calls. See [MXU Latency Overview](../cost/mxu-latency-overview.md). |
| **`SparseCoreTargetForModule`** | The factory that builds a `SparseCoreTarget` for a given module — the SparseCore analogue of resolving a `tpu::Target`. See [SparseCore Target Descriptor](../targets/sparsecore-target-descriptor.md). |

---

## Cross-References

- [Glossary](../glossary.md) — the **parent** quick reference; the ~83 core terms (codenames, compute units, memory spaces, IR names, ABI terms) live there, and this page extends them. Resolve `MXU`/`SCS`/`LLO`/`PJRT`/`HBM` there, the named structs/enums/abbreviations here.
- [Codename Cheat-Sheet](../front/codename-cheatsheet.md) — the canonical three-axis codename card; what `tpu::Target` is ultimately selected by.
- [ISA Overview](../isa/overview.md) · [MXU Slot](../isa/slot-mxu.md) · [Memory-Space Enum](../isa/memory-space-enum.md) · [ResultFifo / ArchRegister](../isa/resultfifo-archregister.md) · [MatPrep / IAR Latch Slot](../isa/slot-matprep-iar-latch.md) — owns the enum families and the IAR/MRB/MSR/LMR/CBREG/RPU abbreviations.
- [Cost Overview](../cost/overview.md) · [CycleTable Family](../cost/cycletable-family.md) · [MXU Latency Overview](../cost/mxu-latency-overview.md) · [Matmul Mode Modifiers](../cost/matmul-mode-modifiers.md) — owns `CycleTable`, `MxuLatencyTable`, and the matmul-mode cost modifiers.
- [Scheduler Overview](../sched/overview.md) · [MRB Chain Allocator](../sched/mrb-chain-allocator.md) · [MRB FIFO / MSR Placement](../sched/mrb-fifo-msr-placement.md) · [Encoder Latch Serialization](../sched/encoder-latch-serialization.md) — owns `MrbChainAllocator`, MRB/MSR placement, and the LSF latch.
- [Profiling Overview](../profiling/overview.md) · [Trace Entries Coder](../profiling/trace-entries-coder.md) · [Riegeli Trace Container](../profiling/riegeli-trace-container.md) · [XPlane / XStat / TraceMe](../profiling/xplane-xstat-traceme.md) — owns `TraceEntry`, `XLine`/`XLineBuilder`, `GtcSpan`, and riegeli.
- [Collectives Overview](../collectives/overview.md) · [Twist Overview](../twist/overview.md) · [Routing Overview](../routing/overview.md) · [VC Balance Allocation](../ici/vc-balance-allocation.md) — owns `TwistedTorusND`, `ToroidalRouteCache`, `VirtualChannel`, megacore, and n-hop routing.
- [Targets Overview](../targets/overview.md) · [Target Capability Bitfield](../targets/target-capability-bitfield.md) · [SparseCore Target Descriptor](../targets/sparsecore-target-descriptor.md) · [TpuTopology Struct](../targets/tpu-topology-struct.md) — owns `tpu::Target`, `SparseCoreTarget`, `TpuCoreParts`, and `TpuTopology`.
