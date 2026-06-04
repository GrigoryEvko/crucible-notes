# protodesc_cold Catalog

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`). Other builds renumber the section and rehash the descriptor symbols.*

## Abstract

`protodesc_cold` is a 3.2 MiB read-only ELF section (`0xbe8af30`–`0xc1bf0b0`) that holds the serialized `google.protobuf.FileDescriptorProto` blob for **every** protobuf schema statically linked into `libtpu.so`. There is exactly one blob per compiled `.proto` file. The protobuf C++ codegen emits, for each `foo.proto`, a `descriptor_table_protodef_<hash>` byte array (the serialized schema, parked here in the cold section) plus a `descriptor_table_<hash>` registration struct; a `_GLOBAL__sub_I_` constructor in `.init_array` walks these tables at process start and calls into the descriptor-pool builder so the reflection layer can later resolve message types by name. This page is the consolidated catalog of those blobs: the section facts, the per-entry record layout, and the full taxonomy of the schema set by source root and by domain.

The count is exact and byte-anchored. The symbol table carries **760** `descriptor_table_protodef_*` blob symbols and **760** matching `descriptor_table_*` registrar symbols — so there are 760 file descriptors in the pool. The section additionally contains **769** distinct `.proto` *path strings*; the nine-string excess over 760 is the set of `.proto` names that appear only inside some descriptor's `dependency` (import) list, i.e. imported files whose own descriptor is reached transitively. This is the schema surface a reimplementer would need to regenerate the entire reflection database the runtime expects: it spans the XLA/HLO compiler, the deepsea TPU ISA for five chip families, the TPU runtime topology and program format, the XPlane/xprof profiler, Megascale collectives, PJRT distributed coordination, and a long tail of Google-internal infrastructure pulled in by transitive imports.

The schema set is the reverse-engineer's index to the binary's *intent*. A flag that appears as a field in `tpu_compilation_environment.proto` is a flag the compiler implements; an `entry_point` oneof arm in `tpu_sequencer_program.proto` names a chip family the runtime can load; a `TransferType` enum value in `megascale_info.proto` names a collective the runtime can route. The catalog is therefore consulted alongside the [TpuCompilationEnvironment](../config/tpu-compilation-environment.md) and [tpu_telemetry.proto](../profiling/tpu-telemetry-proto.md) pages, which reconstruct individual high-value schemas in full.

This is an appendix reference page: it consolidates counts and representative names rather than reconstructing message graphs. For the section's place in the ELF layout and the registration mechanism, see [Custom Sections](../forensics/custom-sections.md), which owns the section header.

| | |
|---|---|
| **Section** | `protodesc_cold` — ELF section header `[12]`, type `PROGBITS`, flags `A` (alloc, read-only) |
| **VA range** | `0xbe8af30` → `0xc1bf0b0` |
| **Size** | `0x334180` = **3,359,104 bytes (3.20 MiB)** |
| **Alignment** | 16 bytes |
| **Entry count** | **760** file descriptors (one `descriptor_table_protodef_*` per `.proto`) |
| **Distinct `.proto` strings** | 769 (760 own names + 9 import-only paths) |
| **First blob** | `descriptor_table_protodef_zzRDQFgX_23` @ `0xbe8af80` — `pjrt_tpu_topo_desc_name_mapping.proto` |
| **Registration** | `descriptor_table_*` structs walked by a `_GLOBAL__sub_I_` ctor in `.init_array` at static-init |
| **Companion sections** | `linkarr_upb_AllExts` (37 upb extension registrars), `pb_defaults` (feature default-instance) |
| **Confidence** | CERTAIN (section + count byte-anchored to the symbol table) |

> **CORRECTION (PDC-01) —** earlier scratch analysis reported the section as "3.27 MiB". The section header size is `0x334180 = 3,359,104 bytes`, which is **3.20 MiB** (`3359104 / 1024 / 1024`). The page and [Custom Sections](../forensics/custom-sections.md) both use the correct 3.20 MiB figure.

---

## Entry / Record Layout

`protodesc_cold` is **not** a self-describing container — it carries no header, no count, and no per-entry index. It is a flat concatenation of independent byte arrays, each one a serialized `FileDescriptorProto`. The structure lives entirely in the symbol table and in the `.init_array` registrars, not in the section bytes.

### How an entry is reached

```text
.init_array
  └─ _GLOBAL__sub_I_<tu>.cc          ── one per translation unit
       └─ AddDescriptors(&descriptor_table_<hash>)   ── protobuf runtime call
            descriptor_table_<hash> {               ── in .data.rel.ro
              once_flag,                              ── std::once init guard
              size,                                   ── byte length of the blob
              const char* protodef  ───────────────┐ ── points INTO protodesc_cold
              const DescriptorTable** deps, ...      │
            }                                        │
  protodesc_cold:                                    │
    [ descriptor_table_protodef_<hash> ]  <──────────┘  serialized FileDescriptorProto
```

Each blob is a standard length-delimited protobuf message. Because every `FileDescriptorProto` begins with field 1 (`name`, a `string`), the first thing in nearly every blob is the proto's own path — the `.proto` filename — which is why scanning the section for `.proto`-suffixed strings recovers the catalog directly. Subsequent strings in the same blob are the `package`, the `dependency` import paths, message and field names, and the `syntax` marker (`"proto2"`, `"proto3"`, or absent/`"editions"`).

### Record-layout table

| Element | Where | Meaning | Confidence |
|---|---|---|---|
| Blob bytes | `protodesc_cold` | Serialized `FileDescriptorProto` (no framing) | CERTAIN |
| `descriptor_table_protodef_<hash>` | symbol → blob start VA | The byte array; `<hash>` is an 8–11 char base62 tag | CERTAIN |
| Blob length | `descriptor_table_<hash>.size` field | Byte count; not stored in the section | CERTAIN |
| `descriptor_table_<hash>` | `.data.rel.ro` | Registrar struct (once-flag, size, ptr, deps) | CERTAIN |
| `_GLOBAL__sub_I_*` | `.text.startup` / `.init_array` | Calls `AddDescriptors` for the TU | HIGH |
| `name` (field 1) | first string in blob | The `.proto` path — the catalog key | CERTAIN |
| `package` (field 2) | second string in blob | Dotted package; drives C++ namespace | CERTAIN |
| `dependency` (field 3, repeated) | strings in blob | Import paths — source of the 9 extra `.proto` strings | HIGH |

> **NOTE —** the `<hash>` in `descriptor_table_protodef_<hash>` is content-derived, not a source path. It is stable for a given `.proto` content + protobuf version but is **not** human-meaningful; to map a blob to its file you read field 1 of the blob, not the symbol name. The first blob's symbol `..._zzRDQFgX_23` resolves to `learning/45eac/research/pjrt/pjrt_tpu_topo_desc_name_mapping.proto`.

> **GOTCHA —** counting `.proto` strings in the section overcounts by exactly the number of import-only paths (769 vs 760). The authoritative entry count is the number of `descriptor_table_protodef_*` symbols (760), each of which is a *file*; a path that appears only as another file's `dependency` has a string in the section but no blob of its own here. Drive the count off the symbol table, not the string scan.

### Syntax dialects

The pool deliberately mixes three proto dialects — each `FileDescriptorProto` carries its own `syntax` field, so the builder tolerates the mix. Newer TPU-runtime additions (chip-parts topology, memory reservation, sequencer program, persistent compilation cache) use **editions**; `xla`, `megascale`, and PJRT use **proto3**; the deepsea ISA protos remain on **proto2**. The split is roughly even (≈285 proto2 / ≈278 proto3 / ≈197 editions) and is a fingerprint of how recently each subsystem's schema was last touched.

---

## Catalog by Source Root

Every `.proto` path begins with a Google source-tree root. Grouping the 760 entries by that first path segment gives the coarse map of what is compiled into the binary. Counts are over the 769 distinct path strings in the section (which includes import-only paths); the dominant three roots account for ~77% of the pool.

| Source root | Files | Domain | Confidence |
|---|---:|---|---|
| `platforms/` | 306 | deepsea TPU ISA (5 chip families), driver/profiler, XLA jellyfish compiler + Megascale | CERTAIN |
| `third_party/` | 240 | TensorFlow, TF2XLA, XLA/HLO, xprof, gRPC, protobuf, ortools | CERTAIN |
| `learning/45eac/` | 41 | PJRT + tfrt TPU runtime, topology, compilation cache | CERTAIN |
| `util/` | 29 | operations_research (CP-SAT, MathOpt), task/time utilities | CERTAIN |
| `security/` | 28 | LOAS L2 internal auth, IAM credentials, ALTS policies | CERTAIN |
| `perftools/` | 19 | xprof accelerator profiler convert/service | CERTAIN |
| `google/` | 19 | protobuf well-known types + `api/expr` (CEL) | CERTAIN |
| `monitoring/` | 18 | Streamz timeseries (collection/query/aggregation) | CERTAIN |
| `production/` | 11 | coroner crash-analysis annotations | CERTAIN |
| `net/` | 10 | gRPC core types, DNS, congestion-control | HIGH |
| `file/` | 5 | file/base options, blob types | HIGH |
| `webutil/` | 4 | HTTP content-type (135-value MIME enum) | HIGH |
| `stats/` | 4 | streamz stat annotations | MEDIUM |
| `storage/` | 3 | datapol semantic annotations | HIGH |
| `tech/` | 3 | colossus file blob types | MEDIUM |
| `wireless/`, `privacy/`, `identity/`, `logs/`, `cloud/`, `src/`, `research/`, `hardware/`, `frameworks/`, `borg/`, `n2a/` | ≤2 each | transitive-import tail | MEDIUM |

> **QUIRK —** `platforms/` outweighs `third_party/` even though TensorFlow/XLA are the "headline" frameworks, because each of the five deepsea chip families ships a *parallel* ISA proto tree (TensorCore + SparseCore + BarnaCore + DMA + memory subsystems). The chip ISA schemas, not the framework schemas, are the bulk of the descriptor pool.

---

## Catalog by Domain

The source-root view is coarse; the domain view is what a reimplementer actually navigates. The 760 entries fall into the following functional groups. Per-group counts are byte-anchored (matched against the section's path strings); representative file names are verbatim.

### TPU deepsea ISA — per-chip-family schemas

The largest single domain. Five silicon codenames each carry a parallel proto tree under `platforms/asic_sw/lib/deepsea/<codename>/.../isa/` plus driver/config trees under `platforms/asic_sw/driver/deepsea/<codename>/`. The ISA protos serialize the per-core compiled program AST (TensorCore, SparseCore SCS/TAC/TEC, BarnaCore).

| Chip dir | ISA-tree files | Representative `.proto` | Confidence |
|---|---:|---|---|
| `deepsea/vxc/` (viperfish) | 37 | `tensorcore_vector_extended.proto`, `sparsecore_tec_vector_alu.proto` | CERTAIN |
| `deepsea/pxc/` (pufferfish) | 37 | `tensorcore_vector_extended_0.proto`, `sparsecore_scs.proto` | CERTAIN |
| `deepsea/gxc/glc/` (ghostlite) | 35 | `tensorcore_vector_extended.proto`, `sparsecore_dma_program.proto` | CERTAIN |
| `deepsea/gxc/gfc/` (6acc60406) | 34 | `sparsecore_tec_vector_alu.proto`, `tensorcore_scalar_alu.proto` | CERTAIN |
| `deepsea/jellyfish/` | 23 | `tensorcore_isa.proto`, `barnacore_isa.proto`, `payload.proto` | CERTAIN |
| `deepsea/jxc/` | 22 | `chip_config.proto`, `coredump.proto`, `link_stack.proto` | CERTAIN |

The shared resource enum `architectural_resource_ids.proto` (`asic_sw.deepsea.ResourceId`, 232 values) names every hardware resource a sequencer can allocate (VMEM banks, sync flags, IAR slots, DMA channels, ICI ports); it is the spine of the compiler's resource-allocation pass. `common_encodings.proto` per chip defines the 64-value operand-source enums (`VectorSource`, `ScalarY`, `CbregMetadata`, `SparsecoreVmask`).

### TPU runtime — topology, program format, compilation cache

`learning/45eac/tpu/runtime/` and `tensorflow/core/tpu/`. These are the editions-dialect schemas that describe a physical TPU and the binary a core executes.

| Schema | Role | Confidence |
|---|---|---|
| `tpu_chip_parts.proto` | `TpuChipPartsProto` — per-chip core/shared-memory/DMA topology | CERTAIN |
| `tpu_chip_parts_locators.proto` | `TpuCoreOnChipProto`, `TpuSyncFlagRangeOnChipProto` locator types | CERTAIN |
| `tpu_core_parts.proto`, `tpu_memory_parts.proto`, `tpu_sequencer_parts.proto`, `tpu_shared_memory_parts.proto` | per-subsystem part descriptors | CERTAIN |
| `tpu_core_program.proto` | `TpuCoreProgramProto` — `oneof core {TensorCore, BarnaCore, SparseCore}` | CERTAIN |
| `tpu_sequencer_program.proto` | `TpuSequencerProgramProto` — `oneof entry_point` over 16 per-chip program variants | CERTAIN |
| `tpu_memory_reservation.proto` | `TpuMemoryReservationTypeProto` (51 values) — program-descriptor slot map | CERTAIN |
| `tpu_compilation_cache*.proto` | cache entry/group/common; persistent cache | CERTAIN |
| `tpu_chip_enums.proto`, `tpu_version.proto`, `tpu_platform_type.proto` | chip/version/platform enums | CERTAIN |
| `tpu_executable.proto`, `tpu_executable_info.proto`, `compile_metadata.proto`, `topology.proto` | executable + compile metadata | CERTAIN |

> **NOTE —** the `entry_point` oneof in `tpu_sequencer_program.proto` enumerates exactly `{jellyfish, pufferfish, viperfish, ghostlite, 6acc60406}` with their per-core sub-programs (SCS = SparseCore Sequencer, TAC = Tile Access Core, TEC = Tile Execute Core). This oneof is the authoritative list of chip families the runtime can load and is corroborated by the per-chip ISA trees above.

### XLA / HLO compiler

133 files span `third_party/tensorflow/compiler/xla/` and `platforms/xla/`. The headline schemas:

| Schema | Notes | Confidence |
|---|---|---|
| `tpu_compilation_environment.proto` | `TpuCompilationEnvironment` — the master flag message (largest blob in the pool); see [config page](../config/tpu-compilation-environment.md) | CERTAIN |
| `xla.proto` | HLO config: precision, debug options, dump flags | CERTAIN |
| `xla_data.proto` | `PrimitiveType` (35 values), sharding, `ShapeProto`, op-metadata | CERTAIN |
| `service/hlo.proto` | `HloInstructionProto`, `HloModuleProto`, schedule/alias/donor | CERTAIN |
| `jellyfish/proto/llo_opcode.proto` | `LloOpcodeProto` enum (462 values) — internal LLO IR opcodes | CERTAIN |
| `jellyfish/metadata/llo_proto_opcode.proto` | `Opcode` enum (461 values) — serialized LLO metadata form | CERTAIN |
| `jellyfish/lowering/backend_configs.proto` | per-op lowering configs (barna-core, sparse-core, ICI, megacore) | CERTAIN |
| `backends/gpu/runtime/thunk.proto` | 49 GPU `Thunk` messages — present because xla/gpu shares the `xla.gpu` namespace | HIGH |

The `xla.DebugOptions` message (reconstructed on [its own page](../config/debugoptions-proto.md)) lives in this domain. Note `debug_options.proto` is present in the section as a distinct path string.

### Profiler — XPlane + per-chip trace_entries

54 files under `third_party/tensorflow/core/profiler/`, `third_party/xprof/`, and `perftools/accelerators/xprof/`, plus per-chip trace schemas under the deepsea driver tree.

| Schema | Role | Confidence |
|---|---|---|
| `xplane.proto` | `XSpace → XPlane → XLine → XEvent → XStat` hierarchy with interned metadata | CERTAIN |
| `op_stats.proto`, `op_metrics.proto`, `steps_db.proto`, `kernel_stats.proto` | aggregated profile views | CERTAIN |
| `trace_events.proto`, `trace_events_raw.proto` | trace-viewer event streams | CERTAIN |
| `profiler/trace_entries.proto` (×5) | per-chip hardware tracepoint catalogs (gfc, glc, vfc, vlc, pxc/common) | CERTAIN |
| `xprof_service.proto` | `XprofService` (6 RPCs) | CERTAIN |
| `topology.proto`, `dcn_collective_info.proto`, `power_metrics.proto` | hardware/topology/power telemetry | CERTAIN |

The five `trace_entries.proto` carry per-chip `TracePointId` enums (up to ~135 values for ghostlite) cataloging memory-controller, ICI/DCN, OCI-descriptor, and throttle/power events. The `tpu_telemetry.proto` runtime-state snapshot is reconstructed on the [profiling page](../profiling/tpu-telemetry-proto.md).

### Megascale — cross-slice collectives runtime

Exactly 15 files under the `platforms/xla/megascale/` tree (most under `runtime/`, with `megascale_info.proto`/`runtime.proto` under `common/` and `addresses.proto`/`dcn_topology.proto` under the `third_party/.../xla/megascale/` mirror). The complete control plane above ICI/DCN.

| Schema | Role | Confidence |
|---|---|---|
| `transport.proto` | `MegaScaleTransport` (6 RPCs: Send, GetMultiSliceTopology, Barrier, ReportError, TriggerError, SendHeartBeat) | CERTAIN |
| `debug_service.proto` | `MegascaleDebugService` (6 RPCs incl. `SetImpairments`) | CERTAIN |
| `megascale_info.proto` | `MegaScaleInfoProto` — `TransferType` (ALL_TO_ALL, REDUCE_SCATTER, ALL_GATHER, ALL_REDUCE, BROADCAST, RAGGED_ALL_TO_ALL, …) + fp-compression + ragged params | CERTAIN |
| `megascale_status.proto` | `MegaScaleRuntimeError` with `ErrorType` / `UnrecoverableErrorType` | CERTAIN |
| `dcn_topology.proto` | `DCNTopology` oneof (SymmetricTree vs explicit TreeNode) for multi-slice ring/tree reductions | CERTAIN |
| `rapideye_logging.proto` | on-device always-on log buffer (post-mortem hang analysis) | CERTAIN |
| `actions.proto`, `host_command.proto`, `host_command_scheduler.proto`, `impairments.proto`, `addresses.proto`, `core_location.proto`, `runtime.proto`, `runtime_config.proto`, `debug_logging.proto` | action queue, host protocol, fault injection, addressing | CERTAIN |

### PJRT — plugin C-API + distributed coordination

16 files spanning `learning/45eac/research/pjrt/` and `third_party/tensorflow/compiler/xla/pjrt/`.

| Schema | Role | Confidence |
|---|---|---|
| `coordination_service.proto` | `CoordinationService` (14 RPCs: register/heartbeat/barrier/KV-store/watch-tasks) | CERTAIN |
| `compile_options.proto` | `PJRT_CompileOptions` wire format (carries the serialized `TpuCompilationEnvironment`) | CERTAIN |
| `tpu_executable.proto`, `tpu_pjrt_topology_description.proto` | TPU-specific PjRtExecutable + topology | CERTAIN |
| `pjrt_tpu_topo_desc_name_mapping.proto` | the first blob in the section (`@0xbe8af80`) | CERTAIN |
| `distributed/protocol.proto`, `stream_executor_executable.proto` | distributed protocol + SE executable | HIGH |
| `pjrt_partial_program.proto`, `pjrt_abi_version.proto`, `pjrt_value_type.proto`, `pjrt_device_dimensions.proto`, `execute_options.proto`, `executable_metadata.proto`, `topology_description.proto` | C-API ABI + value/option wire types | HIGH |

> **NOTE —** there are **two** `CoordinationService` definitions in the pool — the public gRPC surface (`xla/pjrt`, 14 RPCs) and a lower-level state machine (`tsl/protobuf`, 19 RPCs). The overlap is intentional; both provide register/heartbeat/barrier/KV-store, but the tsl one is the implementation the xla one wraps.

### TensorFlow / TF2XLA base

84 files of TF/TF2 graph and saved-model schemas (`config.proto`, `rewriter_config.proto`, `saved_object_graph.proto`, `meta_graph.proto`, `journal.proto`) plus the TF→XLA bridge (`tf2xla.proto`, `xla_argument.proto`, `host_compute_metadata.proto`, `xla_activity.proto`). These are present because the PJRT plugin links the TF2XLA legacy bridge and tf.data service replication.

### Operations Research (ortools)

28 files under `util/operations_research/`. CP-SAT + MathOpt solver schemas: `sat_parameters.proto` (hundreds of CP-SAT tuning knobs), `cp_model.proto`, `math_opt/{rpc,model,model_update,model_parameters,callback}.proto` (`SolveService`, 3 RPCs), plus seven solver-adapter files (glpk, gscip, gurobi, highs, mosek, osqp, xpress). Pulled in because XLA's autosharding cost model uses MIP solving.

### Infrastructure tail

| Group | Files | Representative `.proto` | Confidence |
|---|---:|---|---|
| Security (LOAS L2, IAM, ALTS) | 28 | `principal.proto`, `authenticator.proto`, `end_user_credentials.proto` | CERTAIN |
| Monitoring (Streamz) | 18 | `service.proto`, `query.proto`, `aggregation.proto` (3 services) | CERTAIN |
| gRPC core | 9 | `channelz/v1` + `v2/service.proto`, `reflection/v1`, `latent_see.proto` | CERTAIN |
| Crash analysis (coroner) | 10 | `field_id.proto` (124-value FieldID), `crash_reason.proto`, `asan_error_type.proto` | CERTAIN |
| Protobuf well-known types | 10 | `any.proto`, `duration.proto`, `timestamp.proto`, `struct.proto`, `wrappers.proto`, `field_mask.proto`, `empty.proto`, `type.proto`, `source_context.proto`, `java_features.proto` | CERTAIN |
| CEL (Common Expression Language) | 5 | `google/api/expr/*` (syntax/checked/value/eval/explain) | CERTAIN |
| MLIR / StableHLO | 9 | TF-Lite converter (`model_flags`, `converter_flags`) + PTQ + `compiler_trace.proto` | HIGH |
| File / storage | 8 | `file/base/options.proto`, `tech/file/proto/types.proto`, `semantic_annotations.proto` (166-value SemanticType) | HIGH |

> **NOTE —** the CEL schemas (`google/api/expr`) are present because some compilation-environment switch expressions are CEL-evaluated. The infrastructure tail (security, monitoring, crash-analysis, file/storage) is not TPU-specific; it is dragged in by transitive imports from the framework and runtime libraries and is mostly inert in the PJRT plugin path.

---

## gRPC Service Inventory

The pool defines 27 gRPC services across 184 RPCs. The largest surfaces are the on-device debug/control planes, not the public API. Full list in the per-domain tables above; the standouts:

| Service | RPCs | File | Confidence |
|---|---:|---|---|
| `DebuggerService` | 33 | `platforms/deepsea/jellyfish/xdb/debugger.proto` | CERTAIN |
| `SliceBuilderWorkerService` | 18 | `platforms/accel_ssw/deepsea/slice_builder/slice_builder.proto` | CERTAIN |
| `CoordinationService` (tsl) | 19 | `xla/tsl/protobuf/coordination_service.proto` | CERTAIN |
| `CoordinationService` (pjrt) | 14 | `xla/pjrt/distributed/coordination/coordination_service.proto` | CERTAIN |
| `TpuDebugService` | 8 | `xdb/tpu_debugger/tpu_debugger.proto` | CERTAIN |
| `VBARControl` | 8 | `learning/45eac/tfrc/tpunetd/proto/vbar_control.proto` | CERTAIN |
| `MegaScaleTransport` / `MegascaleDebugService` | 6 / 6 | `megascale/runtime/...` | CERTAIN |
| `XprofService` | 6 | `perftools/accelerators/xprof/xprof_service.proto` | CERTAIN |
| `SessionControl` / `NetworkConfig` / `NetworkBuild` | 7 / 3 / 3 | `superpod/routing/tpunetd/proto/tpunetd.proto` | HIGH |

> **QUIRK —** the biggest service surface is the post-mortem TPU debugger (`DebuggerService`, 33 RPCs), not any compilation or execution API. The schema pool is shaped as much by the debug/observability tooling baked into the runtime as by the compiler itself.

---

## Architectural Enum Highlights

Several enums in the pool are large enough to be the spine of a subsystem. Re-derived from the descriptor blobs:

| Values | Enum | File | Confidence |
|---:|---|---|---|
| 462 | `xla.jellyfish.LloOpcodeProto` | `jellyfish/proto/llo_opcode.proto` | CERTAIN |
| 461 | `xla.jellyfish.Opcode` | `jellyfish/metadata/llo_proto_opcode.proto` | CERTAIN |
| 232 | `asic_sw.deepsea.ResourceId` | `deepsea/common/architectural_resource_ids.proto` | CERTAIN |
| 166 | `SemanticType` | `storage/datapol/.../semantic_annotations.proto` | HIGH |
| ~135 | `TraceEntries.TracePointId` (per chip) | each `trace_entries.proto` | CERTAIN |
| 135 | `ContentType` | `webutil/http/content-type.proto` | HIGH |
| 124 | `FieldID` | `crash_analysis/reporting/ipc/field_id.proto` | CERTAIN |
| 67 | `DataType` | `tensorflow/core/framework/types.proto` | CERTAIN |
| 53 | `xla.gpu.ThunkKindProto` | `backends/gpu/runtime/thunk_kind.proto` | HIGH |
| 51 | `TpuMemoryReservationTypeProto` | `tpu/runtime/topology/tpu_memory_reservation.proto` | CERTAIN |
| 35 | `xla.PrimitiveType` | `xla_data.proto` | CERTAIN |

The two LLO opcode enums (462 / 461 values) enumerate the full deepsea ISA at the LLO abstraction level — the same opcode universe documented in the [LLO opcode table](llo-opcode-table.md). `ResourceId` (232) is the resource-allocation spine; `TpuMemoryReservationTypeProto` (51) is the program-descriptor slot dictionary that PJRT reads.

---

## Verification

- **Section facts** confirmed against the ELF section header `[12]` and the IDA segment record: `protodesc_cold` start `0xbe8af30`, end `0xc1bf0b0`, `size = 3,359,104 (0x334180)`, flags `A`.
- **Entry count 760** confirmed three ways: 760 distinct `descriptor_table_protodef_*` blob symbols, 760 matching `descriptor_table_*` registrar symbols, and 769 distinct `.proto` path strings in the section (760 own + 9 import-only).
- **First entry** confirmed: `descriptor_table_protodef_zzRDQFgX_23` @ `0xbe8af80` → `learning/45eac/research/pjrt/pjrt_tpu_topo_desc_name_mapping.proto`.
- **Per-root / per-domain counts** derived by scanning the section's path strings; chip-family ISA dirs, the 15 Megascale files, 16 PJRT files, and the 5 `trace_entries.proto` were each enumerated against the binary.
- **Registration mechanism** anchored to the `_ZL37descriptor_table_protodef_*` mangled symbols and the companion `linkarr_upb_AllExts` / `pb_defaults` sections, both documented in [Custom Sections](../forensics/custom-sections.md).

---

## Cross-References

- [Custom Sections](../forensics/custom-sections.md) — owns the `protodesc_cold` section header and the `linkarr_upb_AllExts` / `pb_defaults` companions
- [Static-Init Surface](../forensics/static-init.md) — the `.init_array` constructors that call `AddDescriptors` to build the pool
- [Trailing zstd Blob](../forensics/trailing-zstd-blob.md) — sibling forensic catalog; `records_metadata.proto` from this pool corroborates the riegeli hypothesis
- [Binary Layout Reference](binary-layout.md) — the full ELF section map this section sits in
- [filewrapper_toc Catalog](filewrapper-toc-catalog.md) — sibling appendix; the embedded runtime-resource table (binary configs, bootloaders)
- [Reconstructed-Proto Index](reconstructed-proto-index.md) — the index of schemas reconstructed in full from these blobs
- [LLO Opcode Table](llo-opcode-table.md) — the 462/461-value opcode enums catalogued here, reconstructed
- [TpuCompilationEnvironment](../config/tpu-compilation-environment.md) — the largest single schema in the pool, reconstructed field-by-field
- [xla.DebugOptions Proto](../config/debugoptions-proto.md) — a reconstructed HLO-config schema from the xla domain
- [tpu_telemetry.proto](../profiling/tpu-telemetry-proto.md) — a reconstructed TPU runtime-state schema from the profiler/debug domain
