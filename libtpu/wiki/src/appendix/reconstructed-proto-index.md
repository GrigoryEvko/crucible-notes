# Reconstructed-Proto Index

> *Every message name, field count, and descriptor VA on this page was read from the `protodesc_cold` descriptor pool of `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, version 0.103, not stripped). Field numbers are stable wire identifiers; other versions may add fields but will not renumber these. Other versions differ.*

## Abstract

`libtpu.so` embeds its entire protobuf type universe as serialized `google.protobuf.FileDescriptorProto` records in the `protodesc_cold` section (VA `0xbe8af30`, size `0x334180` = 3.27 MiB). The runtime rebuilds this **descriptor pool** at startup, calling `DescriptorPool::BuildFile()` for each generated `_pbz_…` symbol; the reflection layer then serves every message by name. The full enumeration — 760 file records, ~8 021 messages, ~2 031 enums — is catalogued on the sibling [protodesc-cold-catalog](protodesc-cold-catalog.md) page. This page is the narrower, higher-value index: of those ~760 descriptors, **which messages has this wiki reconstructed field-by-field**, and on **which page** does each reconstruction live.

A "reconstruction" here means the message schema was decoded from the descriptor bytes to reimplementation grade — field number → name → label → wire type → nested-type structure → oneof grouping — sufficient that a reader could regenerate the `.proto` and round-trip the wire format. The index distinguishes that from mere *cataloguing* (the sibling page lists all 760 by name/size/syntax but does not field-dump most of them). Every row carries the descriptor VA so a verifier can re-parse the source bytes with `FileDescriptorProto.ParseFromString`, and a Confidence label so a reimplementer knows which schemas to trust verbatim.

The reconstructed set clusters into five families, each owned by a deep page: the **XLA HLO serialization core** (the front-end ↔ TPU-backend contract: `HloModuleProto` / `HloInstructionProto` / `HloComputationProto` and the `xla_data.proto` type universe they ride on); the **compiler config protos** (`DebugOptions`, 290 fields; `TpuCompilationEnvironment`, 1121 fields); the **TPU executable + topology protos** (`TpuCoreProgramProto`, `TpuChipPartsProto`, the shared-memory locators); the **runtime / collective protos** (the Megascale graph, `tpu_telemetry`, the `ContinuationQueue` config, the toroidal route-cache codec); and the **profiler protos** (the XPlane hierarchy, the xprof `Task` record). The grouped detail sections below give per-family field counts, descriptor addresses, and owning pages.

For a reader using this index, the contract is:

- **Message identity** — the fully-qualified proto message name as it appears as a descriptor string in the pool (verifiable by `rg` over the binary's strings).
- **Field count** — the number of declared fields in *this build's* descriptor (not an upstream `.proto` you might find elsewhere; the count is binary-truth and can disagree with public XLA).
- **Owning page** — the deep page that carries the field-by-field reconstruction; this index never duplicates the field roster, it points to it.
- **Descriptor VA** — the `protodesc_cold` address of the enclosing `FileDescriptorProto`, so the schema can be re-derived from source bytes.
- **Confidence** — `CERTAIN` (descriptor fully decoded + cross-checked against an owning page and the binary), `HIGH` (descriptor decoded, owning page authored), `MEDIUM` (decoded but partial field-dump on the owning page).

| | |
|---|---|
| **Descriptor pool** | `protodesc_cold` @ VA `0xbe8af30`, size `0x334180` (3.27 MiB) |
| **Records in pool** | 760 `FileDescriptorProto`; ~8 021 messages, ~2 031 enums |
| **Reconstructed (this index)** | ~30 messages across 5 families, ~14 owning pages |
| **Full enumeration** | [protodesc-cold-catalog](protodesc-cold-catalog.md) — all 760 by name/size/syntax |
| **Headline counts** | `DebugOptions` 290 fields · `TpuCompilationEnvironment` 1121 fields |
| **Re-parse path** | `FileDescriptorProto.ParseFromString(bytes_at_VA)` |

---

## The Reconstructed-Proto Index

One row per message this wiki has reconstructed field-by-field. The **Fields** column is this build's descriptor count; the **Owning page** is where the field roster lives; the **Descriptor VA** is the enclosing `FileDescriptorProto` in `protodesc_cold`. Descriptor VAs for the HLO core (`hlo.proto`, `xla_data.proto`, `xla.proto`) are from the file-record map; the per-message VA is the VA of the file that declares it.

| Message | Fields | Owning page | Descriptor VA (file) | Confidence |
|---|---:|---|---|---|
| `xla.HloModuleProto` | 25 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc189a60` (hlo.proto) | HIGH |
| `xla.HloComputationProto` | 8 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc189a60` (hlo.proto) | HIGH |
| `xla.HloInstructionProto` | 70 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc189a60` (hlo.proto) | HIGH |
| `xla.HloScheduleProto` | 1 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc189a60` (hlo.proto) | HIGH |
| `xla.HloProto` | 2 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc189a60` (hlo.proto) | HIGH |
| `xla.ShapeProto` | 5 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc1b7e20` (xla_data.proto) | HIGH |
| `xla.OpSharding` | 14 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc1b7e20` (xla_data.proto) | HIGH |
| `xla.OpMetadata` | 16 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc1b7e20` (xla_data.proto) | HIGH |
| `xla.HloModuleConfigProto` | 42 | [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) | `0xc021470` (xla.proto) | HIGH |
| `xla.DebugOptions` | **290** | [config/debugoptions-proto](../config/debugoptions-proto.md) | `0xc021470` (xla.proto) | CERTAIN |
| `xla::jellyfish.TpuCompilationEnvironment` | **1121** | [config/tpu-compilation-environment](../config/tpu-compilation-environment.md) | (tpu_compilation_environment.proto) | CERTAIN |
| `tpu.TpuCoreProgramProto` | 11 | [compiler/tpu-program-serialization](../compiler/tpu-program-serialization.md) | (tpu_core_program.proto) | HIGH |
| `tpu.TpuSequencerProgramProto` | 5 | [compiler/tpu-program-serialization](../compiler/tpu-program-serialization.md) | (tpu_sequencer_program.proto) | HIGH |
| `tpu.TpuChipPartsProto` | 9 | [targets/chip-parts-binarypb](../targets/chip-parts-binarypb.md) | (tpu_chip_parts.proto) | HIGH |
| `tpu.TpuSharedMemoryOnChipProto` | 2 | [dma/continuation-queue](../dma/continuation-queue.md) | (tpu_chip_parts_locators.proto) | HIGH |
| `TpuChipConfigProto.ContinuationQueue` | 7+ | [dma/continuation-queue](../dma/continuation-queue.md) | (tpu_chip_config.proto) | HIGH |
| `…tpu_telemetry.CurrentCoreStateSummary` | 7 | [profiling/tpu-telemetry-proto](../profiling/tpu-telemetry-proto.md) | (tpu_telemetry.proto) | HIGH |
| `…tpu_telemetry.AllCoreStateSummaries` | 1 | [profiling/tpu-telemetry-proto](../profiling/tpu-telemetry-proto.md) | (tpu_telemetry.proto) | HIGH |
| `xla.megascale.runtime.MegaScaleInfoProto` | 8 | [megascale/overview](../megascale/overview.md) | (megascale_info.proto) | HIGH |
| `xla.megascale.runtime.MegaScaleRuntimeError` | 13 | [megascale/error-aggregator](../megascale/error-aggregator.md) | (megascale_status.proto) | HIGH |
| `xla.megascale.runtime.RapidEyeErrorDigestProto` | 17 | [megascale/error-aggregator](../megascale/error-aggregator.md) | `0xc169340` (rapideye_logging.proto) | CERTAIN |
| `CompressedToroidalRouteCache` | — | [routing/toroidal-route-cache](../routing/toroidal-route-cache.md) | (route_cache.proto) | MEDIUM |
| `tensorflow.profiler.Task` | 18 | [profiling/task-proto](../profiling/task-proto.md) | `0xbe999a0` (task.proto) | CERTAIN |
| `tensorflow.profiler.XPlane` | 6 | [profiling/xplane-xstat-traceme](../profiling/xplane-xstat-traceme.md) | (xplane.proto) | HIGH |
| `tensorflow.profiler.XEvent` | 4 | [profiling/trace-entry-to-xevent](../profiling/trace-entry-to-xevent.md) | (xplane.proto) | HIGH |

> **NOTE —** the **Fields** column is the count of *top-level declared fields on that one message*, not the file's total. `HloModuleProto` declares 25 fields directly; `HloInstructionProto` is the wide "union of all op attributes" at 70 fields; the file `hlo.proto` as a whole carries 22 messages. The file totals are on the [protodesc-cold-catalog](protodesc-cold-catalog.md) sibling.

> **GOTCHA —** these counts are **binary-truth for this build**, and several disagree with the public XLA `.proto` you would find upstream. `DebugOptions` here has 290 live fields with a max field number of 501 (211 numbering gaps for retired GPU/CPU flags); an earlier partial recovery in this wiki quoted "111 wire-fields", which was a sample, not the schema — see the correction below. Drive a reimplementation off the descriptor count, never off an upstream `.proto`.

---

## Family 1 — XLA HLO Serialization Core

The stable contract between the JAX/TF/PyTorch front-ends and the TPU JellyfishCompiler. Three `FileDescriptorProto` records carry the entire schema, with two supporting records closing the type graph. The dependency chain is `xla_data.proto` → `hlo.proto` → `xla.proto`, so the universe is self-contained inside these records.

| File | Descriptor VA | Size | Msgs | Enums | Role |
|---|---|---:|---:|---:|---|
| `…/service/hlo.proto` | `0xc189a60` | 10 242 | 22 | 5 | graph spine: module / computation / instruction |
| `…/xla_data.proto` | `0xc1b7e20` | 11 143 | 50 | 12 | type universe: shapes, layouts, dtypes, sharding |
| `…/xla.proto` | `0xc021470` | 39 523 | 13 | 0 | config layer: `HloModuleConfigProto`, `DebugOptions` |
| `…/service/metrics.proto` | — | — | — | — | `HloPassMetadata.kv_metrics` source |
| `…/hlo_profile_printer_data.proto` | — | — | — | — | per-instruction profile catalog |

The graph spine is an **id-edge DAG**, not a tree: `HloModuleProto` → repeated `HloComputationProto` → repeated `HloInstructionProto`, where every data edge is an `int64 operand_ids` reference into the sibling instruction list and every call edge is an `int64 called_computation_ids` reference into the module's computation list. `root_id` per computation names the output. This index representation survives serialization without pointer fixups — the reason the proto is a clean DAG-by-index.

`HloInstructionProto` is the widest message in the family: **70 fields**, a deliberately sparse "union of all op attributes" where the `string opcode = 2` selects which subset is meaningful (a `dot` reads `dot_dimension_numbers` + `precision_config`; a `custom-call` reads `custom_call_target` + `backend_config` + `custom_call_api_version`). The field-number space runs to 99 with gaps for retired attributes.

> **QUIRK —** `HloOpcode` is **not** a proto enum. The opcode is `string opcode = 2`; an exhaustive scan of all 760 descriptors finds no `xla.HloOpcode` enum anywhere in the pool. Opcodes serialize as lowercase text mnemonics ("add", "dot", "convolution", "fusion", "all-reduce") via the C++ `HloOpcodeString()` ↔ `StringToHloOpcode()` pair. This is why the proto is forward/backward-compatible across XLA versions that add opcodes — a new opcode is just a new string, no enum-number coordination. A reimplementer building a numeric opcode dispatch off the descriptor pool will find nothing to dispatch on; the mnemonic vocabulary is a C++-side table, not a descriptor.

> **NOTE —** the HLO core reconstruction lives on [hlo-ingestion](../compiler/hlo-ingestion.md), the page that owns the StableHLO→HLO conversion and the HLO proto schema the front-end hands in. The id-graph edge model, the 70-field attribute union, the sharding dialects (classic `OpSharding` + Shardy `NamedShardingProto`), and the two-tier source provenance (`OpMetadata` + `StackFrameIndexProto`) are reconstructed there from the descriptor pool. The `xla_data.proto` type universe (`ShapeProto`, `OpSharding`, `OpMetadata`, the dimension-numbers messages) and the `xla.proto` config layer (`HloModuleConfigProto`) ride the same descriptor records and are described alongside it.

---

## Family 2 — Compiler Config Protos

Two master flag messages gate the entire compilation pipeline, and they are the two highest-field-count reconstructions in the index. They are siblings: `DebugOptions` is the backend-shared (GPU/CPU/TPU) flag set that travels with an HLO module across the PJRT boundary; `TpuCompilationEnvironment` is the TPU-specific master switch table the Jellyfish backend actually consumes.

| Message | Fields | Max field # | Type breakdown | Owning page |
|---|---:|---:|---|---|
| `xla.DebugOptions` | 290 | 501 | 290 live, 211 numbering gaps, 17 nested enums, 2 map-entry msgs | [config/debugoptions-proto](../config/debugoptions-proto.md) |
| `…TpuCompilationEnvironment` | 1121 | 1218 | 418 bool, 349 msg, 148 int64, 74 enum, 37 string, 34 float, 32 int32, 14 double, 11 uint32, 4 uint64 | [config/tpu-compilation-environment](../config/tpu-compilation-environment.md) |

`DebugOptions` lives in `xla.proto` (VA `0xc021470`). Every scalar is a proto3 `optional` wrapped in a synthetic single-member oneof, giving explicit has-bit presence — a `DebugOptions` on the wire records exactly which knobs the front-end touched. The HLO-dump and HLO-pass-control flags are the part that governs serialization of the rest of the family: `xla_dump_hlo_as_proto` (113) → `HloProto`, `xla_dump_hlo_snapshots` (118) → `HloSnapshot`, `xla_dump_module_metadata` (144) → `HloModuleMetadataProto`, `xla_dump_latency_hiding_schedule` (182) → `ScheduleProto`, `xla_dump_full_hlo_config` (381) → `HloModuleConfigProto`, `xla_dump_hlo_unoptimized_snapshots` (405) → `HloUnoptimizedSnapshot`.

`TpuCompilationEnvironment` (TCE) is the largest single proto in the catalog (file 137 692 bytes). Its **1121 fields** are numbered 1…1218 with gaps for retired flags, and at least 80% are wrapped in `AutoProto` so each can be in state AUTO / DISABLED / ENABLED or carry a typed value. The field naming is the subsystem map: `xla_…`, `xla_tpu_…`, `xla_jf_…`, `megascale_…`, `xla_sc_…`, `xla_tpu_sdc_checker_…`, `xla_tpu_autofdo_…`. This single message is the surface area for the entire TPU compiler config; its field roster is split across the field-dictionary pages, the offsets/defaults page, and the AutoOr resolver page (all cross-linked from the owning page).

> **CORRECTION (PROTO-IDX-01) —** earlier flag-catalog work in this wiki (and the configuration overview) once quoted "**111 wire-fields**" for `DebugOptions`. That was a *partial* tag recovery from a 111-entry sample, not the schema. The full descriptor decode at the `xla.proto` record carries **290 live fields** (290 field names cross-matched against the binary). This index and the [debugoptions-proto](../config/debugoptions-proto.md) page use 290 as the authoritative count; the 111 figure is superseded.

> **QUIRK —** the TCE schema and the flag surface are the **same 1121 names, 1:1**: every TCE field is also a registered `absl::Flag`. A reimplementer can therefore treat the proto field roster and the command-line/env flag set as one namespace, not two. The TCE is an `editions`-syntax proto (proto2/proto3's successor); `DebugOptions` is proto3. Both reach `RunHloPasses` via the `CompilationEnvironmentsProto.environments` Any-packed list.

---

## Family 3 — TPU Executable + Topology Protos

The *output* side of compilation and the hardware description it targets. `TpuCoreProgramProto` is the compiled TPU executable (the JellyfishCompiler's output); `TpuChipPartsProto` is the per-generation hardware-constant description the cost model and ISA emitter read.

| Message | Fields | Role | Owning page |
|---|---:|---|---|
| `tpu.TpuCoreProgramProto` | 11 | compiled executable; `oneof core` = TensorCore / BarnaCore / SparseCore | [compiler/tpu-program-serialization](../compiler/tpu-program-serialization.md) |
| `tpu.TpuSequencerProgramProto` | 5 | per-sequencer program; `oneof entry_point` = 16 per-chip-family variants | [compiler/tpu-program-serialization](../compiler/tpu-program-serialization.md) |
| `tpu.TpuChipPartsProto` | 9 | per-codename HW constants (HBM/VMEM/SMEM, MXU geometry, clocks, DMA granules) | [targets/chip-parts-binarypb](../targets/chip-parts-binarypb.md) |
| `tpu.TpuSharedMemoryOnChipProto` | 2 | shared-memory locator (type + index), used by ContinuationQueue mapping | [dma/continuation-queue](../dma/continuation-queue.md) |

`TpuCoreProgramProto.entry_point` confirms the supported chip families are exactly {jellyfish, pufferfish (pxc), viperfish (vxc), ghostlite (gxc/glc), 6acc60406 (gxc/gfc)} with their per-core sub-programs (SCS = SparseCore Sequencer, TAC = Tile Access Core, TEC = Tile Execute Core). The `tpu::l` serializer dispatches the `oneof core` on a discriminant `*((_DWORD*)this + 22)` with `(unsigned)(v20 - 5) <= 2`, so tags 5/6/7 are mutually exclusive arms — see the owning page's reconstruction.

`TpuChipPartsProto` is reconstructed on [chip-parts-binarypb](../targets/chip-parts-binarypb.md) as the schema for the embedded `<codename>_chip_parts.binarypb` blobs that the runtime parses into the `xla::jellyfish::Target` object — a data-driven HAL where one C++ `Target` class is specialized only by the bytes it loads. Its companion locator file (`tpu_chip_parts_locators.proto`) declares `TpuCoreOnChipProto`, `TpuSharedMemoryOnChipProto`, `TpuSyncFlagRangeOnChipProto`, and `TpuSegmentMemoryOnChipProto`, referenced from both the chip-parts schema and the ContinuationQueue config.

---

## Family 4 — Runtime / Collective Protos

The wire formats that drive the multi-chip / multi-host collective fabric, the device-state snapshot, the async continuation queue, and the toroidal route cache.

| Message | Fields | Role | Owning page |
|---|---:|---|---|
| `…MegaScaleInfoProto` | 8 | per-collective-op metadata: `TransferType`, reduce op, ragged + FP-compression params | [megascale/overview](../megascale/overview.md) |
| `…MegaScaleRuntimeError` | 13 | runtime error record: `ErrorType`, hostname, task id, embedded runtime state | [megascale/error-aggregator](../megascale/error-aggregator.md) |
| `…RapidEyeErrorDigestProto` | 17 | fleet-wide root-cause digest; 9-value `Cause` enum, embeds first error verbatim | [megascale/error-aggregator](../megascale/error-aggregator.md) |
| `…tpu_telemetry.CurrentCoreStateSummary` | 7 | per-core live state: sequencer info, fingerprint, launch id, queued programs | [profiling/tpu-telemetry-proto](../profiling/tpu-telemetry-proto.md) |
| `…tpu_telemetry.AllCoreStateSummaries` | 1 | map `int32 global_core_id → CurrentCoreStateSummary` | [profiling/tpu-telemetry-proto](../profiling/tpu-telemetry-proto.md) |
| `TpuChipConfigProto.ContinuationQueue` | 7+ | async continuation queue config: producer sflags, per-core ring window | [dma/continuation-queue](../dma/continuation-queue.md) |
| `CompressedToroidalRouteCache` | — | baked route-cache blob, decompressed into the per-link route map | [routing/toroidal-route-cache](../routing/toroidal-route-cache.md) |

`MegaScaleInfoProto` types the metadata for every cross-slice collective: a `TransferType` ∈ {ALL_TO_ALL, ONE_TO_ONE, REDUCE_SCATTER, ALL_GATHER, ALL_REDUCE, BROADCAST, RAGGED_ALL_TO_ALL}, an `OperationType` reduce op, optional ragged parameters, and optional FP-compression knobs. It binds to HLO at `HloInstructionProto.channel_id` + `replica_group_list` for the collectives the TPU backend lowers across the DCN.

`RapidEyeErrorDigestProto` is the most thoroughly reconstructed runtime proto: 17 live fields, 13 nested message types, a 9-value `Cause` enum, recovered from the `rapideye_logging.proto` record at VA `0xc169340` (4 460 bytes). It embeds the inbound `MegaScaleRuntimeError` verbatim (field 11, `first_recorded_error`), so the digest is self-describing.

The `ContinuationQueue` config is reconstructed on [continuation-queue](../dma/continuation-queue.md) as the static record (`TpuChipConfigProto.ContinuationQueue`, one per core) that tells both host runtime and device producer where the async descriptor ring lives — the producer sync-flag offsets on the message head, the ring region and consumer flag in the `per_core` vector. The toroidal route cache (`CompressedToroidalRouteCache`) is reconstructed at MEDIUM on [toroidal-route-cache](../routing/toroidal-route-cache.md): the baked `(src,dst) → route` blob, its decompress codec, and its dedup form, consumed by `GetDistanceFromCache` with a per-pair cache-miss fall-through to the live torus metric.

> **GOTCHA —** `tpu_telemetry` is a **point-in-time state snapshot**, structurally the *opposite* of the XPlane event stream — the two never share a wire blob. `AllCoreStateSummaries` is a map of one `CurrentCoreStateSummary` per core captured at one instant; the profiler's `XSpace`/`XPlane`/`XLine`/`XEvent`/`XStat` is an append-only event log. A reimplementer must not unify them; their only meeting point is the PC→HLO metadata both read.

---

## Family 5 — Profiler Protos

The XPlane trace hierarchy and the xprof per-worker metadata record. These are XPlane-compatible: a 7-message hierarchy with interned metadata dictionaries.

| Message | Fields | Role | Owning page |
|---|---:|---|---|
| `tensorflow.profiler.Task` | 18 | per-worker device metadata: provenance, host id, clock rates, profile window | [profiling/task-proto](../profiling/task-proto.md) |
| `tensorflow.profiler.XPlane` | 6 | trace plane: lines + interned event/stat metadata dictionaries | [profiling/xplane-xstat-traceme](../profiling/xplane-xstat-traceme.md) |
| `tensorflow.profiler.XEvent` | 4 | one timeline event: metadata id, duration, stats, offset/occurrence oneof | [profiling/trace-entry-to-xevent](../profiling/trace-entry-to-xevent.md) |

`Task` is the most precisely-anchored profiler reconstruction: a flat proto3 message of **18 singular scalar fields**, carved byte-exact from the descriptor record at VA `0xbe999a0` (path `…/tensorboard_plugin_profile/protobuf/task.proto`). At runtime it is the *value* of a `Map<uint32 task_id, Task>` — one record per worker ordinal — read by `xprof::XlaJfProfileCheapOps(Task const&)` @ `0xf2ca280`. The XPlane hierarchy (`XSpace` → `XPlane` → `XLine` → `XEvent` → `XStat`, with interned `XEventMetadata` / `XStatMetadata`) is reconstructed across the xplane/traceme and trace-entry pages; the per-chip `trace_entries.proto` families that feed it are catalogued separately on the [tracepoints-master-registry](../profiling/tracepoints-master-registry.md).

---

## Verification Provenance

Every message name and the two headline field counts were re-checked against the binary before this index shipped:

- **Message-name presence** — each fully-qualified name (`HloModuleProto`, `HloInstructionProto`, `HloComputationProto`, `TpuCompilationEnvironment`, `DebugOptions`, `TpuChipPartsProto`, `TpuCoreProgramProto`, `TpuSequencerProgramProto`, `TpuSharedMemoryOnChipProto`, `MegaScaleInfoProto`, `MegaScaleRuntimeError`, `ContinuationQueue`, `RouteCache`, `tpu_telemetry`, `CurrentCoreStateSummary`, `AllCoreStateSummaries`) was confirmed to occur as a descriptor string in the binary's extracted strings.
- **File-path presence** — the source paths (`service/hlo.proto`, `xla_data.proto`, `tpu_chip_parts.proto`, `tpu_core_program.proto`, `tpu_sequencer_program.proto`, `megascale_info.proto`, `tpu_telemetry.proto`) were confirmed present, anchoring each `FileDescriptorProto` record.
- **Headline counts** — `DebugOptions` = 290 and `TpuCompilationEnvironment` = 1121 were corroborated against both the descriptor decode and the two owning pages, which independently state the same figures (the 290 supersedes a prior 111-field partial sample, per the correction above).
- **Owning-page existence** — every page linked in the index table was confirmed to exist under `src/`. The HLO-core family rows point at [hlo-ingestion](../compiler/hlo-ingestion.md), the page that owns the front-end HLO proto schema; the config rows point at the `config/` field-dictionary pages; the rest point at the deep page that field-dumps each message.

What this index does **not** do: it does not reproduce field rosters (those live on the owning pages), and it does not claim a per-message descriptor VA for messages whose enclosing file VA was not separately recovered — those rows carry the file name in the VA column rather than a fabricated address. A blank VA is an honest gap, never a guessed one.

---

## Cross-References

- [protodesc-cold-catalog](protodesc-cold-catalog.md) — the full enumeration sibling: all 760 `FileDescriptorProto` records by name, size, syntax, and category
- [compiler/hlo-ingestion](../compiler/hlo-ingestion.md) — the HLO core family: `HloModuleProto` / `HloInstructionProto` / `HloComputationProto` id-graph and the StableHLO→HLO conversion
- [config/debugoptions-proto](../config/debugoptions-proto.md) — the 290-field `DebugOptions` field dictionary
- [config/tpu-compilation-environment](../config/tpu-compilation-environment.md) — the 1121-field TCE structure/taxonomy (field rosters on the dictionary pages it links)
- [config/tce-field-dictionary-a](../config/tce-field-dictionary-a.md) / [config/tce-field-dictionary-b](../config/tce-field-dictionary-b.md) — the full TCE field#→name roster
- [compiler/tpu-program-serialization](../compiler/tpu-program-serialization.md) — `TpuCoreProgramProto` / `TpuSequencerProgramProto` reconstruction
- [targets/chip-parts-binarypb](../targets/chip-parts-binarypb.md) — `TpuChipPartsProto` and the embedded per-codename blob schema
- [dma/continuation-queue](../dma/continuation-queue.md) — `ContinuationQueue` config + `TpuSharedMemoryOnChipProto` locator
- [profiling/tpu-telemetry-proto](../profiling/tpu-telemetry-proto.md) — `CurrentCoreStateSummary` / `AllCoreStateSummaries` device-state snapshot
- [profiling/task-proto](../profiling/task-proto.md) — the 18-field xprof `Task` per-worker record
- [profiling/xplane-xstat-traceme](../profiling/xplane-xstat-traceme.md) — the `XPlane` / `XStat` trace hierarchy
- [megascale/overview](../megascale/overview.md) — `MegaScaleInfoProto` collective metadata
- [megascale/error-aggregator](../megascale/error-aggregator.md) — `MegaScaleRuntimeError` + `RapidEyeErrorDigestProto`
- [routing/toroidal-route-cache](../routing/toroidal-route-cache.md) — the toroidal route-cache codec
