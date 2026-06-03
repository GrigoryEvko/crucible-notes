# Learned Cost-Model Client

> *Addresses apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). The binary is **not** stripped — every symbol below is a demangled C++ name. `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. Other versions differ.*

## Abstract

The TPU convolution lowering path contains a complete *client-side* hook for an ML-learned cost model, but **no server, no client class, and no predictor ship in this build**. The hook lives inside `SpatialMajorConvolution::ComputeWindowConfigInternal` (@ `0x13172c80`) — the function that searches the window-tiling space for the fastest convolution schedule. The hook reaches a borrowed `EmitterLearnedCostModelBase*` pointer stored on the emitter at `this+0x20d0`; when that pointer is non-null *and* the `enable_learned_cost_model` byte is set, the search consults the learned model through three virtual slots: a per-instruction enable check (`vtable+0x10`), a candidate-window registration call (`vtable+0x18`, `RegisterCandidateWindow`), and a fastest-window query (`vtable+0x30`) whose `absl::Status` result selects between the learned and the analytic answer. The pointer is borrowed all the way down from the 80-parameter `LoweringEmitter` ctor through `ConvolutionEmitter::Create`.

The familiar reference frame is XLA's pluggable cost-model interface — an abstract base with virtual `Predict`/`Register` methods, a concrete client that batches features into RPCs to an embedding/inference service, and an `absl::Status`-gated fallback to the analytic model on any RPC failure. libtpu-0.0.40 ships the *interface contract* (proto options, the four-enum mode state machine, the gflag wiring, the call sites, the `CHECK_OK` on the registration result, and the `Failed to get fastest window using learned cost model` failure-log fallback) but **not the implementation**: there is no `xla::jellyfish::EmitterLearnedCostModelBase` vtable, no concrete `LearnedCostModelClient`, no `LearnedCostModelService::Stub`, and no embedded model bytes. The `EmitterLearnedCostModelBase*` is therefore always null in the shipping binary, so every consult is short-circuited and the analytic [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) / window-search result is used unchanged.

This page documents what is *real and reimplementable* — the wire-level options proto (`LearnedCostModelClientOptions` with its `ServiceType` enum, RPC-endpoint fields, and recovered C++ struct layout), the four-enum mode/validation/DB-query state machine the interface implements, the recovered `EmitterLearnedCostModelBase` vtable shape (three exercised slots), the `MLCostModelWindowInfo` request payload the client receives, the status/error handling at each call site, and the precise null/flag double-gate that drops the whole layer back to the analytic model.

For reimplementation, the contract is:

- The `EmitterLearnedCostModelBase` vtable: `IsEnabled(HloInstruction*)` @ `+0x10` (returns `bool`), `RegisterCandidateWindow(LcmKey, MLCostModelWindowInfo)` @ `+0x18` (returns `absl::Status`, `CHECK_OK`'d), and the fastest-window query @ `+0x30` (returns `absl::StatusOr`, status-gated fallback).
- The `MLCostModelWindowInfo` request payload — the eight designated-initializer fields the client receives for every candidate window, including `estimated_cycles_classic` (the analytic answer, always supplied as the floor).
- The status handling: the `+0x30` query result `absl::Status` selects learned-vs-analytic; on non-OK the layer logs `spatial_major_convolution.cc:4006` and falls through to `SetupBestConfig` with the classic search result.
- The double-gate to the analytic model: the `this+0x20d0` null check **and** the `TpuCompEnv+0xed6` (`enable_learned_cost_model`) byte; either-false drops to analytic.
- The `LearnedCostModelClientOptions` proto wire shape, `ServiceType` (LOCAL/REMOTE) enum, and the recovered struct layout — what a client implementation would have to parse from the gflag.

| | |
|---|---|
| **Hook site** | `SpatialMajorConvolution::ComputeWindowConfigInternal` @ `0x13172c80` |
| **Registration lambda** | `…::ComputeWindowConfigInternal(…)::$_0` (policy_func) @ `0x1317fe00` |
| **Client pointer slot** | `this+0x20d0` (`SpatialMajorConvolution`), borrowed `EmitterLearnedCostModelBase*` |
| **Enable slot** | `vtable+0x10` — `bool IsEnabled(HloInstruction*)` (@ call `0x13173`…) |
| **Register slot** | `vtable+0x18` — `absl::Status RegisterCandidateWindow(LcmKey, MLCostModelWindowInfo)` |
| **Query slot** | `vtable+0x30` — `absl::StatusOr<…>` fastest-window prediction |
| **Enable flag byte** | `*(byte*)(GetTpuCompEnv(inst)+0xed6)` == `enable_learned_cost_model` |
| **Options proto / vtable** | `xla::jellyfish::LearnedCostModelClientOptions` @ `0x21cffc10` |
| **gflag** | `xla_tpu_emitter_learned_cost_model_options` = `AutoOr<EmitterLearnedCostModelOptions>` |
| **Failure-log fallback** | `spatial_major_convolution.cc:4006` → `SetupBestConfig` (analytic) |
| **Shipping default** | client pointer null everywhere → analytic model always used |

---

## What Ships vs What Does Not

The learned cost model is a textbook "future-extension hook": the schema, the gflag, the consumer call sites, and the failure-fallback all ship; the predictor does not. The split is exact and verifiable by symbol scan.

| Component | Present? | Status | Evidence |
|---|---|---|---|
| `EmitterLearnedCostModelOptions` proto | YES | Reachable | vtable @ `0x21cff958`; ctor `0x1db63f20` |
| `LearnedCostModelClientOptions` proto | YES | Reachable | vtable @ `0x21cffc10`; copy-ctor `0x1db653e0` |
| `FusionDataProtoGenerationOptions` proto | YES | Reachable | vtable @ `0x21cffbd0` |
| `EmbeddingCacheEntry` / `EmbeddingCacheDB` protos | YES | Reachable | typeinfo `0x21d00068` / `0x21d00080` |
| `LearnedCostModelMode` / `DbQueryType` / `MLOutputValidationStrategy` / `ServiceType` enums | YES | Decoded | value-name strings present (e.g. `SERVICE_TYPE_REMOTE`) |
| gflag `xla_tpu_emitter_learned_cost_model_options` | YES | Parsed | `AbslFlagDefaultGenForxla_tpu_emitter_learned_cost_model_options::Gen` @ `0x1d72ac00` |
| Consumer call sites (3 vtable slots) | YES | Code-present, runtime-dead | `ComputeWindowConfigInternal` @ `0x13172c80` + lambda `0x1317fe00` |
| `CHECK_OK` on `RegisterCandidateWindow` result | YES | Source `spatial_major_convolution.cc:4020` | literal @ `.rodata` |
| `Failed to get fastest window …` analytic fallback | YES | Source `spatial_major_convolution.cc:4006` | literal @ `.rodata` |
| `EmitterLearnedCostModelBase` vtable / typeinfo | **NO** | Type-only | appears solely in mangled ctor *parameter* names |
| concrete `LearnedCostModelClient` class | **NO** | Absent | no symbol beyond the `…ClientOptions` proto |
| `LearnedCostModelService::Stub` (gRPC) | **NO** | Absent | no `*::Stub` / `NewStub` (cf. `BarnaCoreInterWorkerCommunicationRpc::Stub` which *does* ship) |
| `Predict` / `Inference` / `Score` / `EstimateCycles` method | **NO** | Absent | no matching symbol |
| embedded model (SavedModel / ONNX / TFLite blob) | **NO** | Absent | only LLVM MLGO `RegAllocEvictModel`/`InlinerSizeModel`, unrelated |

> **NOTE —** the absence is positive evidence, not a gap in analysis. Scanning the (non-stripped) symbol table finds the `…ClientOptions` proto family and its ser/deser methods, but `EmitterLearnedCostModelBase` exists *only* as a function-parameter type in the mangled names of `ConvolutionEmitter::Create`, `SpatialMajorConvolution::SpatialMajorConvolution`, and `LoweringEmitter::LoweringEmitter`. A class used only as a borrowed pointer needs no emitted vtable or typeinfo in the consumer's translation unit — which is exactly the footprint of an interface whose only implementation lives out-of-tree.

---

## The Client Interface — `EmitterLearnedCostModelBase` vtable

### Purpose

`EmitterLearnedCostModelBase` is the abstract interface the convolution emitter calls. It is borrowed (never owned) by the emitter, so no destructor slot is exercised. Three virtual slots are reached from the decompiled code; their signatures are pinned by the call-site register usage and the `CHECK_OK` literal.

### Recovered vtable

| Slot | Method (inferred) | Returns | Call-site evidence |
|---|---|---|---|
| `+0x00` | `offset_to_top` | — | Itanium ABI |
| `+0x08` | typeinfo ptr | — | Itanium ABI |
| `+0x10` | `bool IsEnabled(const HloInstruction*)` | `bool` (in `%al`) | `(*(…)(*v44 + 16))(v44, hlo)` — arg is `*(HloInstruction**)(this+72)`; result drives the consult branch |
| `+0x18` | `absl::Status RegisterCandidateWindow(const LcmKey&, const MLCostModelWindowInfo&)` | `absl::Status` | `(*(…)(*v52 + 24))(v52, key.first, key.second, &status)` in lambda; `CHECK_OK`'d |
| `+0x30` | fastest-window query `→ absl::StatusOr<…>` | `absl::StatusOr` (stack-returned) | `(*(…)(**(this+0x20d0) + 48))(&out, this_lcm, fp.first, fp.second)`; status selects fallback |

### Call sequence inside the window search

```c
// SpatialMajorConvolution::ComputeWindowConfigInternal  @0x13172c80
bool consult = false;                                    // v172
void* lcm = *(void**)(this + 0x20d0);                    // borrowed EmitterLearnedCostModelBase*
if (lcm) {                                               // null → analytic (default ship)
    if (enable_learned_cost_model) {                     // bool param a9, see gate below
        const HloInstruction* hlo = *(const HloInstruction**)(this + 72);
        consult = (*(bool(**)(void*, const HloInstruction*))(*(void**)lcm + 0x10))(lcm, hlo);  // IsEnabled
        if (consult) {
            fp = xla::GetHloInstructionFingerprint(hlo);  // @0x13180b80 — the prediction key
        }
    } else {
        consult = false;
    }
}

// ... the window-tiling search runs (IterateThroughWindowConfigs), invoking the
//     registration lambda once per candidate window when consult is true ...

if (consult != true) {                                   // learned model not used
    SetupBestConfig(/* classic search result */);        // analytic answer
} else {
    // fastest-window query @ vtable+0x30, keyed by the fingerprint
    status = (*(Status(**)(…))(*(void**)lcm + 0x30))(&out, lcm, fp.first, fp.second);
    if (!status.ok()) {                                  // analytic fallback
        LOG("Failed to get fastest window using learned cost model "
            "for instruction: ", hlo, " with status: ", status);   // .cc:4006
        SetupBestConfig(/* classic search result */);    // <-- fall through to analytic
    } else {
        SetupBestConfig(/* learned-selected window  */);
    }
}
```

> **GOTCHA —** the `vtable+0x10` slot takes the `HloInstruction*` (loaded from `this+72`) and returns a `bool`; do not mistake it for a no-arg `IsEnabled()`. The per-instruction argument is what lets a real client gate the learned path on op shape/type. In the shipping binary the slot is never reached because `lcm` is null.

### The registration call — `RegisterCandidateWindow`

The window-search iterator invokes a `std::function` (`…::$_0` policy_func @ `0x1317fe00`) once per candidate tiling. When the learned path is active, the lambda builds an `MLCostModelWindowInfo` on the stack from its parameters (the InlinedVectors are deep-copied via `inlined_vector_internal::Storage::InitFrom`) and calls `vtable+0x18`:

```c
// lambda body @0x1317fe00 — one call per candidate window
if (*(byte*)(GetTpuCompEnv(hlo) + 0xed6) == 1            // enable_learned_cost_model
    && capture_flag == 1 && a8 < threshold) {            // **(this+0x80)+0x10 byte + window bound
    void* lcm = *(void**)(this + 0x20d0);
    Status s = (*(Status(**)(void*, long, long, Status*))(*(void**)lcm + 0x18))(
                   lcm, lcm_key.first, lcm_key.second, &out);   // RegisterCandidateWindow
    // CHECK_OK — fatal on a non-OK registration:
    //   "learned_cost_model_->RegisterCandidateWindow( *lcm_key,
    //    MLCostModelWindowInfo( {.activations_window = …, .kernel_window = …,
    //    .output_window = …, .iteration_bounds = …, .window_info = …,
    //    .vmem_footprint_granules = estimated_granules, .bundles = estimated_bundles,
    //    .estimated_cycles_classic = estimated_cycles})) is OK"  // .cc:4020
}
```

> **NOTE —** `RegisterCandidateWindow` is `CHECK_OK`'d (fatal on failure), while the fastest-window query @ `+0x30` is *soft*-failed (logs and falls back). The asymmetry is deliberate: registration is a local bookkeeping push into the consideration set (must not fail), whereas the prediction query can fail transiently (RPC down) and must degrade to the analytic model rather than abort compilation.

---

## The Request Payload — `MLCostModelWindowInfo`

Reconstructed from the `CHECK_OK` designated-initializer literal and the lambda's stack-build sequence. This is the per-candidate-window feature record the client receives; the trailing `estimated_cycles_classic` is the analytic answer, supplied as a baseline/floor.

```c++
struct MLCostModelWindowInfo {
  absl::InlinedVector<int64_t, 6> activations_window;      // input  window dims (deep-copied)
  absl::InlinedVector<int64_t, 6> kernel_window;           // kernel window dims
  absl::InlinedVector<int64_t, 6> output_window;           // output window dims
  absl::InlinedVector<int64_t, 6> iteration_bounds;        // outer loop bounds
  WindowSizingInfo               window_info;              // sizing metadata (copied from a7)
  int64_t                        vmem_footprint_granules;  // estimated_granules
  int64_t                        bundles;                  // estimated_bundles
  int64_t                        estimated_cycles_classic; // analytic-model estimate (baseline)
};
```

The first argument to `RegisterCandidateWindow` is `*lcm_key` — an `LcmKey` whose two 8-byte halves (`v53 = *v50; v54 = v50[1]`) are passed by value. Its full shape is not recoverable beyond being a two-word key; it deduplicates candidates within one fusion search and most likely encodes operation type, MXU format, and a per-window hash. The fastest-window query @ `+0x30` is keyed instead by `xla::GetHloInstructionFingerprint(hlo)` (@ `0x13180b80`), also passed as a two-word value (`fp.first`, `fp.second`).

> **CONTRACT —** every candidate window the analytic search considers is reported to the client *with its classic cycle estimate attached*. A real client therefore never starts cold: it can return the classic number verbatim (validation strategy `ALWAYS_TRUST` off), clamp it (`NO_NEGATIVE_CYCLES`), or override it. This is the wire-level meaning of the `MLOutputValidationStrategy` enum below.

---

## The Mode State Machine — Four Enums

The options proto encodes a small state machine the (missing) client implements. All four enums are decoded from the embedded `FileDescriptorProto`; value-name strings are present in `.rodata`.

### `LearnedCostModelMode`

| Value | Name | Semantic |
|---|---|---|
| 0 | `LEARNED_COST_MODEL_MODE_INVALID` | default — treated as "no learned cost model" |
| 1 | `LEARNED_COST_MODEL_MODE_ONLY_DB` | look up cycles from a pre-built DB only |
| 2 | `LEARNED_COST_MODEL_MODE_ONLY_ML_PREDICTION` | always use the ML predictor |
| 3 | `LEARNED_COST_MODEL_MODE_DB_WITH_FALLBACK_TO_ML_PREDICTION` | DB first, ML on miss |
| 4 | `LEARNED_COST_MODEL_MODE_ONLY_DATA_COLLECTION` | dump `FusionData` protos for offline training; no scoring |

The mode names disclose the intended design: an offline DB of pre-measured `(window-config → cycles)` tuples, with an ML predictor filling gaps. The `+0x18` `RegisterCandidateWindow` slot is the data-collection / consideration-set push (used in all modes); the `+0x30` query slot is the DB-lookup-or-predict.

### `MLOutputValidationStrategy`

| Value | Name | Semantic |
|---|---|---|
| 0 | `ML_OUTPUT_VALIDATION_STRATEGY_NONE` | no validation |
| 1 | `ML_OUTPUT_VALIDATION_STRATEGY_NEVER_TRUST` | always fall back to the classic cost model |
| 2 | `ML_OUTPUT_VALIDATION_STRATEGY_ALWAYS_TRUST` | take ML output verbatim |
| 3 | `ML_OUTPUT_VALIDATION_STRATEGY_NO_NEGATIVE_CYCLES` | reject only negative-cycle predictions |

### `DbQueryType`

| Value | Name | Semantic |
|---|---|---|
| 0 | `DB_QUERY_TYPE_NONE` | no DB query |
| 1 | `DB_QUERY_TYPE_REPLAY_PREDICITIONS` | (sic — proto typo) replay stored ML cycles |
| 2 | `DB_QUERY_TYPE_GROUND_TRUTH` | look up measured ground-truth cycles |

### `LearnedCostModelClientOptions.ServiceType`

| Value | Name | Semantic |
|---|---|---|
| 0 | `SERVICE_TYPE_UNSPECIFIED` | invalid sentinel |
| 1 | `SERVICE_TYPE_LOCAL` | load model from `local_embedding_model_path`, run in-process |
| 2 | `SERVICE_TYPE_REMOTE` | issue RPCs to `remote_embedding_server_address` |

> **GOTCHA —** both `SERVICE_TYPE_LOCAL` and `SERVICE_TYPE_REMOTE` are unbuildable in this wheel. There is no in-process model loader for the LOCAL path and no `LearnedCostModelService::Stub` / `BlockingUnaryCall` for the REMOTE path. The binary *does* ship unrelated gRPC stubs (`BarnaCoreInterWorkerCommunicationRpc::Stub`, `RuntimeMetricService::Stub`, `MegaScaleTransport::Stub`), which is the proof that a learned-cost-model RPC stub would be visible if it existed.

---

## The Options Proto Wire Shape

The RPC/service configuration is carried entirely as a serialized proto inside one gflag — there is no dedicated boolean or endpoint flag. The two proto files are linked by a sub-message: `EmitterLearnedCostModelOptions.learned_cost_model_client_options` holds a `LearnedCostModelClientOptions`.

```protobuf
package xla.jellyfish;
import "third_party/tensorflow/core/framework/tensor.proto";

message LearnedCostModelClientOptions {
  enum ServiceType { SERVICE_TYPE_UNSPECIFIED = 0; SERVICE_TYPE_LOCAL = 1; SERVICE_TYPE_REMOTE = 2; }

  optional ServiceType embedding_service_type                        = 1;
  optional string      remote_embedding_server_address               = 2;  // REMOTE RPC endpoint
  optional string      remote_embedding_model_name                   = 3;  // REMOTE model selector
  optional int64       inflight_rpc_monitoring_interval_milliseconds = 4;  // RPC liveness poll
  optional string      local_embedding_model_path                    = 5;  // LOCAL model file
  optional string      embedding_cache_path                          = 6;  // EmbeddingCacheDB on disk
  optional FusionDataProtoGenerationOptions fusion_data_proto_generation_options = 7;
  optional int64       max_batch_size                                = 8;  // RPC batch size
}

message EmbeddingCacheEntry { optional bytes fingerprint = 1; optional tensorflow.TensorProto embedding = 2; }
message EmbeddingCacheDB    { repeated EmbeddingCacheEntry entries = 1; }
message FusionDataProtoGenerationOptions {
  optional bool include_standalone_fusion_module   = 1;
  optional bool include_expert_and_gating_features = 2;
}
```

C++ struct layout, recovered byte-exact from the copy-ctor `LearnedCostModelClientOptions(Arena*, const&)` @ `0x1db653e0` (vtable stored is `off_21CFFC20`):

| Offset | Field | Size | Notes |
|---:|---|---:|---|
| `+0x00` | vtable ptr | 8 | `0x21cffc10`+0x10 |
| `+0x08` | `internal::InternalMetadata` | 8 | arena / unknown-field tag |
| `+0x10` | `uint32_t _has_bits_` | 4 | presence bitmap |
| `+0x14` | cached_size | 4 | |
| `+0x18` | `TaggedStringPtr remote_embedding_server_address` | 8 | `ForceCopy` if tag bits set |
| `+0x20` | `TaggedStringPtr remote_embedding_model_name` | 8 | |
| `+0x28` | `TaggedStringPtr local_embedding_model_path` | 8 | |
| `+0x30` | `TaggedStringPtr embedding_cache_path` | 8 | |
| `+0x38` | `FusionDataProtoGenerationOptions*` | 8 | copied iff `_has_bits_ & 0x10` |
| `+0x40` | `int64_t` (RPC interval **or** `max_batch_size`) | 8 | |
| `+0x48` | `int32_t embedding_service_type` (enum) | 4 | tail integer |

> **NOTE —** the copy-ctor guards the sub-message with `(*(byte*)(this+0x10) & 0x10)` — `_has_bits_` bit 4 governs `fusion_data_proto_generation_options`. The four string fields are `proto2::internal::TaggedStringPtr` and are `ForceCopy`'d when their low tag bits are set (arena-owned vs inline). MEDIUM confidence on the exact assignment of the two `int64` fields to `+0x40`; the proto declares both `inflight_rpc_monitoring_interval_milliseconds` and `max_batch_size`, and the copy-ctor copies one int64 at `+0x40` plus the enum at `+0x48` — the second int64 is presence-packed and not separately visible in the copy path.

The owning `EmitterLearnedCostModelOptions` adds the top-level switches: `enable_learned_cost_model` (tag 1, the gate byte), `cost_model_mode`, `db_query_type`, `ml_output_validation_strategy`, `db_path`, `max_num_considered_windows`, `dump_fusion_data_proto[_dir]`. Of these, only `enable_learned_cost_model` has a runtime consumer (the `+0xed6` gate); the rest are deserialized but dead because the client they configure is absent.

---

## The Analytic Fallback — Double Gate

Two independent gates drop the entire learned layer back to the analytic window search. **Either** being false is sufficient.

```c
// Gate 1 — pointer null check (ComputeWindowConfigInternal @0x13172c80)
void* lcm = *(void**)(this + 0x20d0);
if (!lcm) consult = false;            // DEFAULT in shipping libtpu-0.0.40 — always taken

// Gate 2 — enable flag (lambda @0x1317fe00, and the IsEnabled branch)
if (*(byte*)(GetTpuCompEnv(hlo) + 0xed6) != 1) /* skip */ ;   // enable_learned_cost_model

// Gate 3 (defence-in-depth) — captured byte flag inside the lambda
if (**(byte**)(capture + 0x80 ... + 0x10) != 1) /* skip */ ;
```

The `this+0x20d0` pointer is set verbatim from the `EmitterLearnedCostModelBase*` constructor parameter (`SpatialMajorConvolution` C2 @ `0x130dd180` stores it with a raw `mov`, no allocation), which is propagated null from `ConvolutionEmitter::Create` (@ `0x130d86c0`) and ultimately from `LoweringEmitter::LoweringEmitter` (@ `0x10c309c0`). Because no caller ever supplies a non-null pointer in this build, Gate 1 always fires and Gates 2–3 are unreachable — the analytic [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) flop model and the classic window search (`SetupBestConfig`) drive every convolution-lowering decision.

When a client *is* present and a query fails, the soft fallback at `spatial_major_convolution.cc:4006` logs the instruction and the failing `absl::Status`, then calls `SetupBestConfig` with the classic search result — the same code path Gate 1 reaches, so a learned-model RPC outage is functionally identical to having no client at all.

| Gate | Location | Field | Shipping value | Effect when false |
|---|---|---|---|---|
| 1 — pointer | `this+0x20d0` | borrowed `EmitterLearnedCostModelBase*` | **null** | skip consult; analytic |
| 2 — enable | `GetTpuCompEnv(hlo)+0xed6` | `enable_learned_cost_model` | 0 (proto default) | skip register/query |
| 3 — capture | lambda capture `+0x10` byte | propagated enable | 0 | skip per-candidate register |
| soft — query | `+0x30` result `absl::Status` | n/a (unreachable) | OK | log `.cc:4006`, analytic |

---

## Function & Symbol Map

| Symbol | Address | Role | Confidence |
|---|---|---|---|
| `SpatialMajorConvolution::ComputeWindowConfigInternal` | `0x13172c80` | hook site: enable check, fingerprint, query, fallback | CERTAIN |
| `…::ComputeWindowConfigInternal(…)::$_0` (policy_func) | `0x1317fe00` | per-candidate `RegisterCandidateWindow` call + `CHECK_OK` | CERTAIN |
| `SpatialMajorConvolution::SpatialMajorConvolution` (C2) | `0x130dd180` | stores `EmitterLearnedCostModelBase*` at `this+0x20d0` | CERTAIN |
| `ConvolutionEmitter::Create` | `0x130d86c0` | forwards the (null) client pointer | CERTAIN |
| `LoweringEmitter::LoweringEmitter` (C1) | `0x10c309c0` | originates the borrowed client pointer | HIGH |
| `xla::GetHloInstructionFingerprint` | `0x13180b80` | builds the `+0x30` query key | CERTAIN |
| `LearnedCostModelClientOptions(Arena*, const&)` | `0x1db653e0` | copy-ctor → struct layout | CERTAIN |
| `LearnedCostModelClientOptions::_InternalSerialize` | `0x1db65920` | proto wire encode | HIGH |
| `EmitterLearnedCostModelOptions(Arena*)` | `0x1db63f20` | owning options proto ctor | CERTAIN |
| `AutoOr<EmitterLearnedCostModelOptions>::ParseFlag` | `0x1d745680` | gflag → proto parse | HIGH |
| `LearnedCostModelClientOptions` vtable | `0x21cffc10` | proto vtable | CERTAIN |
| `EmitterLearnedCostModelBase` vtable / typeinfo | — | **does not exist** (interface only) | CERTAIN |
| `LearnedCostModelClient` concrete class | — | **does not exist** | CERTAIN |
| `LearnedCostModelService::Stub` (gRPC) | — | **does not exist** | CERTAIN |

> **QUIRK —** the failure-log call site reads `spatial_major_convolution.cc:4006` and the `RegisterCandidateWindow` `CHECK_OK` reads `:4020` in this build (`0.0.40`). Source line numbers are build-version-specific; the surrounding VAs and the wire contract are the stable anchors.

---

## Cross-References

- [Cost Model Overview](overview.md) — why the shipped model is data-table driven, not ML; this page is the detail behind that claim
- [TpuHloCostAnalysis](tpu-hlo-cost-analysis.md) — the analytic flop/byte model the fallback uses, and the `estimated_cycles_classic` source
- [Normalized Computation Cost](normalized-computation-cost.md) — the convolution-cycle cache the classic window search feeds
- [Cost-Model Logging](cost-model-logging.md) — the sibling `AutoOr<…Options>` gflag consumer pattern (impure logging options)
- [Per-Opcode Cycle Constants](per-opcode-cycle-constants.md) — the per-gen cycle table the analytic estimate draws from
