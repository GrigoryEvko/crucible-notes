# Buffer Donation & Aliasing

> *All addresses, struct offsets, source file/line citations, and symbol names on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names and the embedded `CHECK`-string source paths are quoted verbatim. Other versions will differ.*

## Abstract

A TPU computation that updates a tensor in place — a weight after an optimizer step, a KV cache after a decode token — must not pay for a fresh HBM allocation plus a copy on every call. XLA's mechanism for this is **input/output aliasing**: at compile time the program declares that output *o* will reuse the HBM of input *p*, and at run time the runtime, instead of allocating a new buffer for *o*, hands the executable input *p*'s buffer and lets the program overwrite it. For that to be safe the caller must promise it will not read input *p* after the call — that promise is **buffer donation**, expressed per-execute through `ExecuteOptions::non_donatable_input_indices` (the *absence* of an index from that set is the donation flag). This page documents the three-part contract: the compile-time `xla::HloInputOutputAliasConfig` (which output reuses which input, and whether the reuse is mandatory or optional), the run-time donation handshake on the PJRT buffer (`ScopedHold` → `AcquireDonation` → `ConfirmDonation`), and the run-time honor site `tfrt::tpu::AllocateOutputBuffersWithInputReuse` (`0xf7ba9a0`) that walks the output tuple and, per leaf, either reuses the donated input's `tpu::TpuBuffer` or freshly allocates.

The reader who knows XLA on GPU should hold one analogy and immediately complicate it. On GPU, donation feeds the same `HloInputOutputAliasConfig`; the difference on TPU is *what gets reused*. The reused object is a `tpu::TpuBuffer` at a `TpuSharedMemoryLocation` — an HBM offset the [allocator](hbm-allocator.md) handed out, holding bytes in the [tiled on-device layout](tpu-buffer-layout.md). Reuse therefore means the output's *padded device shape* (§[buffer layout §2](tpu-buffer-layout.md#2-host-shape--device-shape)) must be byte-compatible with the donated input's, and the donated buffer must not be one the [compactor](on-device-compaction.md) is free to relocate. The alias config is a sibling of the `ComputationLayout` and `CompilerMetadata` carried in the compiled-program payload ([serialization](../compiler/tpu-program-serialization.md)), so a reloaded program replays the same aliasing without recompilation.

This page owns the *donation mechanism + the `input_output_alias` config + the run-time aliasing honor*. It does **not** reproduce the HBM free-list allocator that backs both the donated and the freshly-allocated buffer (that is [hbm-allocator.md](hbm-allocator.md)), the tiled byte layout that the two shapes must agree on (that is [tpu-buffer-layout.md](tpu-buffer-layout.md)), the executable-serialization container the config rides in (that is [../compiler/tpu-program-serialization.md](../compiler/tpu-program-serialization.md)), or the `PJRT_Buffer` external-refcount ABI above the `ScopedHold` (that is [../pjrt/buffer-and-memory.md](../pjrt/buffer-and-memory.md)).

For reimplementation, the contract is:

- **The config** — `xla::HloInputOutputAliasConfig`: an output-shape-indexed map of `Alias{parameter_number, parameter_index, AliasKind}`; `AliasKind ∈ {kMayAlias, kMustAlias}` (proto `MAY_ALIAS`/`MUST_ALIAS`).
- **The compile-time build** — `OptimizeInputOutputBufferAlias::Build` (`0x164d9b00`) synthesizes the config + `HloBufferDonorConfig` from parameter/result shape compatibility; front ends declare explicit aliases via `XlaBuilder::SetUpAlias`.
- **The donation flag** — per-execute `ExecuteOptions::non_donatable_input_indices`; an input is donated iff it is *absent* from that set. A `kMustAlias` input absent from the donation set is a run-time fatal.
- **The run-time handshake** — `ScopedHold(kDonation)` → `AcquireDonation` → `ConfirmDonation`: ownership of the input's `TrackedTpuDeviceBuffer` transfers out of the donor `PjRtBuffer` into the execute, marking the donor invalidated.
- **The run-time honor** — `tfrt::tpu::AllocateOutputBuffersWithInputReuse` (`0xf7ba9a0`): per output leaf, `GetAliasedParameter(output_index)` → if aliased, reuse the donated input `TpuBuffer`; else `AllocateTpuBufferWithRetry`.

| | |
|---|---|
| **Config object** | `xla::HloInputOutputAliasConfig` (output-shape-tree of `Alias`); `GetAliasedParameter` @ `0x1e580200`, `GetAliasedOutput` @ `0x1e5800a0`, `SetUpAlias` @ `0x1e57e140` |
| **Alias kinds** | `AliasKind::kMayAlias` / `kMustAlias`; proto tokens `MAY_ALIAS` @ `0xc18c23a`, `MUST_ALIAS` @ `0xc18c249` |
| **Front-end declaration** | `xla::XlaBuilder::SetUpAlias` @ `0xfb21220`; `PopulateInputOutputAliasAndBufferDonor` (emits `HloModuleProto`) |
| **Compile-time pass** | `xla::OptimizeInputOutputBufferAlias::Build` @ `0x164d9b00` (builds config + `HloBufferDonorConfig`) |
| **Must-donate set** | `xla::ComputeParametersThatMustBeDonated(HloInputOutputAliasConfig&, int, bool)` @ `0x1d7f4700` (`xla/pjrt/utils.cc:824`) |
| **Donation flag** | `ExecuteOptions::non_donatable_input_indices` (diagnostic @ `0x87a48c6`; proto field-name @ `0xbf85686`) |
| **Donation hold** | `CommonPjRtBuffer::ScopedHold::AcquireDonation` @ `0xf93d600`; `ConfirmDonation` @ `0xf93dca0`; `DropDonationHold` @ `0xf93d900` |
| **Tracked-buffer confirm** | `xla::TrackedTpuDeviceBuffer::ConfirmDonation` @ `0xf840660` (`tracked_tpu_device_buffer.cc:88`) |
| **Run-time honor (TPU)** | `tfrt::tpu::AllocateOutputBuffersWithInputReuse` @ `0xf7ba9a0` |
| **Run-time honor (generic)** | `xla::CommonPjRtClient::AllocateOutputBuffersWithInputReuse` @ `0xf91ec20` |
| **Dispatch planner** | `xla::InferDispatchInfo(CommonPjRtClient*, ComputationLayout&, HloInputOutputAliasConfig&, …)` @ `0xf90cb40` |
| **Config restore** | `HloInputOutputAliasConfig::CreateFromProto(Shape, HloInputOutputAliasProto&)` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## 1. The `HloInputOutputAliasConfig`

### Purpose

The whole subsystem is keyed on one object: `xla::HloInputOutputAliasConfig`. It answers exactly one query the runtime needs — *"for this output leaf, which input parameter (if any) does it alias, and is that alias mandatory?"* — via `GetAliasedParameter`. A reimplementer must reproduce this object because it is the value carried in the compiled-program payload and consumed verbatim at every execute; the runtime never recomputes aliasing, it replays the frozen config.

### Data model

The config wraps the program's **result shape** and stamps each aliased leaf with an `Alias` record. Internally it is an `xla::internal::IndexTable` over the output shape tree (confirmed: `GetAliasedParameter` reaches `xla::internal::IndexTable::GetEntry((char*)a2 + 80, …)` and CHECKs `ShapeUtil::IndexIsValid(alias_.shape(), output_index)` at `hlo_input_output_alias_config.cc:175`). Each entry is:

```c
struct xla::HloInputOutputAliasConfig::Alias {   // one per aliased output leaf
    int64_t   parameter_number;   // which execute input (flat parameter index)
    ShapeIndex parameter_index;   // leaf within that input's tuple tree
    AliasKind kind;               // kMayAlias | kMustAlias
};

enum AliasKind {                   // proto HloInputOutputAliasProto.Kind
    kMayAlias,   // MAY_ALIAS  — reuse if donated; otherwise allocate + copy
    kMustAlias,  // MUST_ALIAS — input MUST be donated; not donating is fatal
};
```

The config is queried two ways, both `const`:

| Query | Symbol | Address | Returns | Confidence |
|---|---|---|---|---|
| output → input | `GetAliasedParameter(output_index)` | `0x1e580200` | `optional<Alias>` (empty ⇒ output is freshly allocated) | CONFIRMED |
| input → output | `GetAliasedOutput(param_number, param_index)` | `0x1e5800a0` | `optional<ShapeIndex>` | CONFIRMED |
| add an alias | `SetUpAlias(output_index, param_number, param_index, kind)` | `0x1e57e140` | mutates the table | CONFIRMED |

> **NOTE —** the two `AliasKind` values are the spine of the safety model. `kMayAlias` is a *performance hint*: if the caller donates the input, reuse it; if not, the runtime allocates a fresh output and copies. `kMustAlias` is a *requirement*: the program was lowered assuming in-place update (it has no copy-in fallback), so the runtime fatals if the matching input is not donated. The literal proto enum strings `MAY_ALIAS` (`0xc18c23a`) and `MUST_ALIAS` (`0xc18c249`) sit adjacent in `.rodata` (in the serialized `HloInputOutputAliasProto` enum descriptor), and the diagnostic `print-must-aliases` (`0x855e010`) dumps only the mandatory set.

### Where the config comes from

Two paths populate the config, both feeding the same object:

1. **Explicit, front-end declared.** A frontend (JAX `donate_argnums`, TF `XlaCallModule` aliasing) calls `xla::XlaBuilder::SetUpAlias(output_index, param, param_index, kind)` (`0xfb21220`). At HLO-module build, `XlaBuilder::PopulateInputOutputAliasAndBufferDonor` serializes these into the `HloModuleProto` (and the parallel `HloBufferDonorConfig` for donate-but-don't-alias parameters). MLIR ingestion mirrors this via `xla::ImportInputOutputAlias` / `ConvertInputOutputAlias`.
2. **Compiler-synthesized.** `xla::OptimizeInputOutputBufferAlias::Build(Span<const Shape> param_shapes, Shape& result_shape, HloInputOutputAliasConfig* out, HloBufferDonorConfig* donors)` (`0x164d9b00`) discovers aliasing opportunities by matching donatable parameter leaves to result leaves of identical byte size, greedily (the decompile shows `DonorEntry`/`DoneeEntry` vectors sorted via `__stable_sort` and matched). This is the pass that turns "this parameter is donatable" into "this output reuses it."

> **GOTCHA —** the config is shape-indexed by the **output** tuple, not the input. `GetAliasedParameter(output_index)` is the run-time hot query (the honor loop calls it once per output leaf). The inverse `GetAliasedOutput` exists for the donation-planning side (given a donated input, does anything reuse it?). A reimplementer who keys the table on the input parameter will make the honor loop O(inputs) per output instead of O(1).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `HloInputOutputAliasConfig::GetAliasedParameter` | `0x1e580200` | output leaf → `optional<Alias>` (the run-time query) | CONFIRMED |
| `HloInputOutputAliasConfig::GetAliasedOutput` | `0x1e5800a0` | input leaf → `optional<ShapeIndex>` | CONFIRMED |
| `HloInputOutputAliasConfig::SetUpAlias` | `0x1e57e140` | record an alias entry | CONFIRMED |
| `HloInputOutputAliasConfig::ForEachAliasWithStatus` | (used by `0x1d7f4700`) | iterate all entries, fallible | CONFIRMED |
| `HloInputOutputAliasConfig::CreateFromProto` | (`xla` core) | rehydrate from `HloInputOutputAliasProto` | HIGH |
| `XlaBuilder::SetUpAlias` | `0xfb21220` | front-end alias declaration | CONFIRMED |
| `XlaBuilder::PopulateInputOutputAliasAndBufferDonor` | (`xla` core) | serialize aliases + donors into `HloModuleProto` | CONFIRMED |
| `OptimizeInputOutputBufferAlias::Build` | `0x164d9b00` | compiler-synthesized alias/donor discovery | CONFIRMED |
| `ImportInputOutputAlias` / `ConvertInputOutputAlias` | (`xla` core) | MLIR ↔ config conversion | HIGH |

---

## 2. The Donation Flag and the Must-Donate Set

### Purpose

Aliasing is a compile-time *plan*; donation is a run-time *permission*. The plan says "output *o* can reuse input *p*"; the permission says "for this particular call, the caller will not touch input *p* afterward, so you may overwrite it." This section documents how the permission is expressed and how it is reconciled against the plan — the reconciliation is where a mismatch becomes a fatal error.

### The donation flag — `non_donatable_input_indices`

PJRT does not carry a per-buffer "donate me" bit on the execute call. It carries the *inverse*: `xla::ExecuteOptions::non_donatable_input_indices`, the set of input indices the caller wants to **keep**. The contract is:

> An input index is **donated** iff it is *absent* from `ExecuteOptions::non_donatable_input_indices`.

Evidence: the run-time diagnostic at `0x87a48c6` — *"pinned_host buffers do not support donation denial at runtime via `ExecuteOptions::non_donatable_input_indices`"* — and the proto field-name string `non_donatable_input_indices` at `0xbf85686`. Default-constructed `ExecuteOptions` has an empty set, so by default every input that the config marks aliasable is donated.

> **GOTCHA —** the polarity is opt-*out*, not opt-*in*. A caller that wants to preserve an input it accidentally marked donatable must add that index to `non_donatable_input_indices`; doing nothing donates it. This is the source of the most common in-place-update bug class: a buffer is silently consumed (invalidated) by an execute because the caller relied on a default-empty deny-list. The donor `PjRtBuffer` becomes unusable after the call (its tracked device buffer is moved out — §4).

> **QUIRK —** `kPinnedHbm` buffers (the runtime-locked tier, see [overview §2](overview.md#2-the-memoryspace-enum)) **cannot** be deny-listed at run time (string `0x87a48c6`). A pinned-host input that the config marks aliasable is donated unconditionally; the deny-list mechanism is silently inert for that memory space. A reimplementer must either reject `non_donatable_input_indices` for pinned buffers or document that the flag does nothing there.

### The must-donate set — `ComputeParametersThatMustBeDonated`

Before dispatch, the runtime computes the set of parameter indices that the program *requires* to be donated — the `kMustAlias` parameters. `xla::ComputeParametersThatMustBeDonated(const HloInputOutputAliasConfig&, int param_count, bool)` (`0x1d7f4700`, source `xla/pjrt/utils.cc:824`) walks the config and returns a sorted `vector<int>`:

```c
// xla::ComputeParametersThatMustBeDonated(config, param_count, kind_filter)  0x1d7f4700
function ComputeParametersThatMustBeDonated(config, param_count, filter):
    donated = vector<int>()                 // reserve param_count
    status = config.ForEachAliasWithStatus(
        [&](output_index, Alias alias) -> Status {
            if (alias.kind matches filter)   // e.g. kMustAlias only
                donated.push_back(alias.parameter_number)
            return Ok
        })
    CHECK(status.ok())                       // else StatusBuilder @ utils.cc:824
    std::sort(donated.begin(), donated.end())
    return donated                           // sorted, possibly with duplicates
```

The runtime cross-checks this set against the effective donation set (inputs absent from `non_donatable_input_indices`). A `kMustAlias` parameter that is *not* donated trips the fatal:

> *"An input was configured to be must-alias at compile time but not donated at runtime: %s"* — strings `0x8588a34` (format) and `0xa27831b` (prefix).

> **NOTE —** `TestBufferDonationClashes` (`0x1d7f4be0`) is the companion validator: it detects when two parameters resolve to the *same* underlying device buffer (e.g. the same `PjRtBuffer` passed as two arguments) and one of them is donated — donating a shared buffer would corrupt the other alias. A reimplementer must run this check before honoring donation, or two aliased outputs can stomp one buffer.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ComputeParametersThatMustBeDonated(config, int, bool)` | `0x1d7f4700` | sorted `vector<int>` of must-donate params | CONFIRMED |
| `ComputeParametersThatMustBeDonated(HloModule, bool)` | `0x1d7f4580` | same, from a module (extracts config first) | CONFIRMED |
| `TestBufferDonationClashes` | `0x1d7f4be0` | detect two args resolving to one donated buffer | CONFIRMED |
| `InferDispatchInfo(client, ComputationLayout&, config&, …)` | `0xf90cb40` | plan dispatch (layouts + aliasing) per launch | CONFIRMED |
| (deny-list field) `ExecuteOptions::non_donatable_input_indices` | string `0xbf85686` | per-execute opt-out donation flag | CONFIRMED |

---

## 3. The Run-Time Honor — `AllocateOutputBuffersWithInputReuse`

### Purpose

This is where the compile-time plan and the run-time permission become a physical decision: for each output leaf, allocate a fresh HBM buffer, or reuse a donated input's buffer. The function is the run-time half of the entire page; everything before it is setup. There are two instantiations — the generic `xla::CommonPjRtClient::AllocateOutputBuffersWithInputReuse` (`0xf91ec20`, in terms of `ScopedHold`s) and the TPU-concrete `tfrt::tpu::AllocateOutputBuffersWithInputReuse` (`0xf7ba9a0`, in terms of `tpu::TpuBuffer`/`TpuCoreLocation`). The TPU one is documented here because it is the silicon-facing path.

### Algorithm — the per-leaf reuse-or-allocate loop

`tfrt::tpu::AllocateOutputBuffersWithInputReuse` (`0xf7ba9a0`) takes the alias config, the output shapes (`ArrayRef<AsyncValueRef<Shape>>`), the `TpuCoreLocation`, the donated input `TpuBuffer`s, and a `System`. It iterates the output tuple's leaves (`*(_QWORD*)(out_shape + 16)` is the tuple element count; CHECKs `Expected a tuple shape` at `xla/shape.h:803` if the result is not a tuple), and per leaf:

```c
// tfrt::tpu::AllocateOutputBuffersWithInputReuse  0xf7ba9a0  (decompiled control flow)
function AllocateOutputBuffersWithInputReuse(config, out_shapes, core_loc,
                                             donated_inputs, out_buffers, system, rm):
    shared_mem = TpuCoreLocation::LocalSharedMemory(core_loc,
                     TpuChipParts::PreferredSharedMemoryType(chip_parts))
    result_shape = config.shape()                       // the program result shape
    CHECK(result_shape.IsTuple())                       // xla/shape.h:803
    for i in [0 .. result_shape.tuple_shapes_size()):
        output_index = {i}                              // top-level leaf index
        alias = config.GetAliasedParameter(output_index)   // 0x1e580200  -> optional<Alias>
        if alias.has_value():                           // v77 == 1 branch
            // ---- REUSE: hand the donated input's buffer straight through ----
            buf = donated_inputs[ alias.parameter_number ]   // $_0::operator()
            out_buffers.push_back(buf)                       // no allocation, no copy
        else:
            // ---- ALLOCATE: size the padded device shape, alloc fresh HBM ----
            n_bytes = TransferSizeUtil::ShapeSizeCompact(topology, out_shapes[i])  // see buffer-layout §4
            buf = AllocateTpuBufferWithRetry(system, shared_mem, n_bytes, …)       // OOM -> defrag -> retry
            out_buffers.push_back(buf)
    return
```

The decompiled branch is unambiguous: a per-leaf flag (`v77`) set from the `GetAliasedParameter` result selects between the *reuse* arm (a lambda `$_0::operator()` that moves the donated `TpuBuffer` into the output vector) and the *allocate* arm (`ShapeSizeCompact` to size the leaf, then `AllocateTpuBufferWithRetry`). The reuse arm performs **no** HBM allocation and **no** byte copy — the output simply *is* the input's buffer, and the program writes through it.

> **GOTCHA —** the reuse arm trusts that the donated input's buffer is byte-compatible with the output leaf's *padded device shape*. The compile-time `OptimizeInputOutputBufferAlias::Build` enforces equal byte size, and [layout assignment](../compiler/layout-assignment.md) ensures both leaves get the same `(SublaneCount, LaneCount)` tile, so the donated bytes are already in the layout the output expects ([tpu-buffer-layout §3](tpu-buffer-layout.md#3-the-tile-and-the-padding-rules)). A reimplementation that aliases two leaves with different tiling or `memory_space` color will reuse a buffer the program addresses incorrectly — the bug is silent (no allocation error) and corrupts the output.

> **NOTE —** the allocate arm calls `AllocateTpuBufferWithRetry`, which wraps `tpu::AllocateBuffer` with the OOM→defragment→retry loop. Crucially, the *donated* (reused) buffer is **not** subject to that defrag relocation during this allocation — it was pinned by the donation hold (§4) before the honor loop ran, so the [compactor](on-device-compaction.md) cannot move it out from under the program. This is the link to `kPinnedHbm`: a donated buffer behaves like a pinned one for the duration of the execute.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `tfrt::tpu::AllocateOutputBuffersWithInputReuse` | `0xf7ba9a0` | per-leaf reuse-or-allocate over donated `TpuBuffer`s | CONFIRMED |
| (reuse lambda) `…::$_0::operator()` | `0xf7bb1c0` | move a donated input buffer into the output slot | CONFIRMED |
| `CommonPjRtClient::AllocateOutputBuffersWithInputReuse` | `0xf91ec20` | generic (ScopedHold-based) sibling | CONFIRMED |
| `tfrt::tpu::AllocateTpuBufferWithRetry` | (in `0xf7ba9a0`) | fresh HBM alloc w/ OOM→defrag→retry | CONFIRMED |
| `TransferSizeUtil::ShapeSizeCompact` | `0x1d6ae8a0` | size the output leaf's padded device shape | CONFIRMED |
| `HloInputOutputAliasConfig::GetAliasedParameter` | `0x1e580200` | the per-leaf alias query | CONFIRMED |

---

## 4. The Donation Handshake on the PJRT Buffer

### Purpose

Before the honor loop can reuse a donated input, the runtime must take ownership of that input's device buffer away from the caller's `PjRtBuffer`, atomically and exactly once, and mark the `PjRtBuffer` as consumed so any later use errors instead of racing the in-place write. That is the `ScopedHold` donation handshake. A reimplementer must get the state machine exactly right — the binary `CHECK`-fatals on every illegal transition.

### The `ScopedHold` state machine

`xla::CommonPjRtBuffer::ScopedHold` is an RAII guard taken on a `PjRtBuffer` for one of several intents; the donation intent is `kDonation`. The execute-prepare stage (`ExecutePrepare` → `PrepareArguments`, see [the adapter execute path](../pjrt/stream-executor-pjrt-adapter.md)) pins each input as a `ScopedHold` and, for donated inputs, drives:

```c
// the donation handshake (decompiled, abstract_tracked_device_buffer.cc + tracked_tpu_device_buffer.cc)

// 1. Acquire: move the tracked device buffer OUT of the PjRtBuffer into the hold.
ScopedHold::AcquireDonation()                       // 0xf93d600
    REQUIRE(state == kUsable)                        // CHECK "!ok()" @ abstract_tracked_device_buffer.cc:180
    buffer_ptr_ = take(buffer_)                       // ownership transfer
    REQUIRE(buffer_ptr_ != nullptr)                   // CHECK @ :192
    state = kDonated

// 2. On successful dispatch: confirm — the donor PjRtBuffer is now permanently empty.
ScopedHold::ConfirmDonation()                       // 0xf93dca0
    CommonPjRtBuffer::ConfirmDonation(buffer_ptr_)    // 0xf93dd40
        TrackedTpuDeviceBuffer::ConfirmDonation()     // 0xf840660
            REQUIRE(in_use_)                          // CHECK "in_use_" @ tracked_tpu_device_buffer.cc:88
            in_use_ = false                           // release the in-use marker
            release all AsyncValueRef usage/definition events  // refcount decrements

// 3. On failure / not-actually-donated: give the buffer back to the PjRtBuffer.
ScopedHold::DropDonationHold(unique_ptr<AbstractTrackedDeviceBuffer>)   // 0xf93d900
    return ownership to buffer_  (PjRtBuffer becomes usable again)
```

The decompile of `AcquireDonation` (`0xf93d600`) shows the ownership move (`*(_QWORD*)(a1 + 32) = take(*a2)`), the post-move `CHECK(buffer_ptr_ != nullptr)` at `abstract_tracked_device_buffer.cc:192`, and the guard `CHECK(!ok())` at `:180` that forbids acquiring a donation from a hold already in an error/used state. `TrackedTpuDeviceBuffer::ConfirmDonation` (`0xf840660`) asserts `in_use_` at `tracked_tpu_device_buffer.cc:88`, clears it, and walks the buffer's usage/definition `AsyncValue` vectors decrementing refcounts under the buffer's `absl::Mutex`.

> **GOTCHA —** the three-state lifecycle (`Acquire` → either `Confirm` or `Drop`, never both, never neither) is enforced by fatals, not graceful errors. `AcquireDonation` moves the buffer out *before* dispatch is guaranteed to succeed; if dispatch then fails, the runtime **must** call `DropDonationHold` to return the buffer, or the donor `PjRtBuffer` is leaked-empty (usable-looking but with no backing storage). A reimplementation that forgets the drop path turns a transient dispatch failure into a permanently-dead buffer.

> **NOTE —** `PjRtBuffer::DonateWithControlDependency(Future<void>)` (`0xe6eb260`, TPU impl `CommonPjRtBufferImpl::DonateWithControlDependency` @ `0xf92a740`, PJRT C shim `PJRT_Buffer_DonateWithControlDependency` @ `0xf86f2e0`) is the *standalone* donation API: it lets a caller donate a buffer gated on a future, independent of an execute call (e.g. donate after a transfer completes). It funnels into the same `ConfirmDonation` machinery once the control dependency resolves.

### How the config and the handshake meet at dispatch

`xla::InferDispatchInfo` (`0xf90cb40`) is the planner that ties the two together. It takes the `ComputationLayout` (input/output device shapes, [serialized with the executable](../compiler/tpu-program-serialization.md)) and the `HloInputOutputAliasConfig`, plus the device assignment, and produces a `DispatchInfo` describing which inputs to donate and how outputs map. The execute path then:

```text
PJRT_LoadedExecutable_Execute
  └─ CommonPjRtLoadedExecutable::Execute
       └─ ExecutePrepare → PrepareArguments
            ├─ pin each input as ScopedHold
            ├─ for inputs absent from ExecuteOptions::non_donatable_input_indices
            │      and aliased by the config:  AcquireDonation               (§4)
            ├─ AllocateOutputBuffersWithInputReuse(config, out_shapes, …)    (§3)
            │      → reuse donated TpuBuffer  |  AllocateTpuBufferWithRetry
            └─ verify ComputeParametersThatMustBeDonated ⊆ donated set       (§2)
                 (else FATAL "must-alias at compile time but not donated at runtime")
       └─ ExecuteLaunch → … → tpu::System::Execute   (program overwrites the reused buffer in place)
       └─ on success: ConfirmDonation for each donated input   (donor PjRtBuffer permanently consumed)
```

### Function Map

| Function | Address | Source anchor | Role | Confidence |
|---|---|---|---|---|
| `ScopedHold::AcquireDonation` | `0xf93d600` | `abstract_tracked_device_buffer.cc:180/192` | move tracked buffer out of PjRtBuffer | CONFIRMED |
| `ScopedHold::ConfirmDonation` | `0xf93dca0` | — | finalize: donor permanently empty | CONFIRMED |
| `CommonPjRtBuffer::ConfirmDonation` | `0xf93dd40` | — | dispatch to tracked-buffer confirm | CONFIRMED |
| `TrackedTpuDeviceBuffer::ConfirmDonation` | `0xf840660` | `tracked_tpu_device_buffer.cc:88` | clear `in_use_`, release events | CONFIRMED |
| `TrackedCpuDeviceBuffer::ConfirmDonation` | `0xf916b20` | — | CPU sibling | CONFIRMED |
| `CommonPjRtBuffer::DropDonationHold` | `0xf93d900` | — | return buffer on failure / no-donate | CONFIRMED |
| `CommonPjRtBuffer::GetBufferForDonationHoldLocked` | `0xf93d3a0` | — | fetch buffer under lock for the hold | CONFIRMED |
| `PjRtBuffer::DonateWithControlDependency` | `0xe6eb260` | — | standalone future-gated donation | CONFIRMED |
| `CommonPjRtBufferImpl::DonateWithControlDependency` | `0xf92a740` | — | TPU impl of the above | CONFIRMED |
| `PJRT_Buffer_DonateWithControlDependency` | `0xf86f2e0` | — | PJRT C-ABI shim | CONFIRMED |
| `InferDispatchInfo(…, ComputationLayout&, config&, …)` | `0xf90cb40` | — | plan donation + output mapping | CONFIRMED |

---

## 5. How the Config Rides the Executable

### Purpose

A donated, aliased program is reused across many executes and may be loaded from a compile cache without recompilation. For that to work, the alias config must travel inside the compiled-program payload. A reimplementer must serialize it as a first-class sibling of the program, not regenerate it.

### The carried config

The compiled-program payload is a tuple constructed once and carried for the program's whole lifetime. The `HloInputOutputAliasConfig` is a member of every payload variant, sitting beside the `ComputationLayout` (or `vector<Shape>`) and the `xdb::CompilerMetadata`:

| Payload type | Constructor | Members (relevant) | Confidence |
|---|---|---|---|
| `xla::jellyfish::TpuJitResult::Program` | `0xf8b7720` / `0xf8b7500` | `unique_ptr<TpuCoreProgram>`, `ComputationLayout`, **`HloInputOutputAliasConfig`**, `CompilerMetadata`, `HostTransferProto[]`, `HostExecutionProto[]` | CONFIRMED |
| `tfrt::tpu::TpuCompilationCacheEntry::Program` | `0xf7bc500` / `0xf7bbd60` | `int`, `AsyncValueRef<TpuCoreProgram>`, `vector<Shape>×2`, **`HloInputOutputAliasConfig`**, `CompilerMetadata`, `HostTransferProto[]` | CONFIRMED |
| `tfrt::tpu::TpuJitResultTF::Program` | `0xf7c8d60` | `AsyncValueRef<TpuCoreProgram>`, **`const HloInputOutputAliasConfig`**, `CompilerMetadata`, `HostTransferProto[]` | CONFIRMED |

The on-disk form is `xla::HloInputOutputAliasProto` (with nested `HloInputOutputAliasProto_AliasEntryProto`; `CopyFrom`/`MergeImpl`/`InternalSwap`/`Clear` all present), and `HloInputOutputAliasConfig::CreateFromProto(result_shape, proto)` rehydrates the in-memory config at load time. This is why a cache-hit reload of a program preserves its aliasing exactly — the config is data, not recomputed.

> **NOTE —** the alias config and the `ComputationLayout` are complementary: the `ComputationLayout` fixes the *padded device shapes* of inputs and outputs ([tpu-buffer-layout §2](tpu-buffer-layout.md#2-host-shape--device-shape)), and the alias config fixes *which output device-shape reuses which input device-shape*. The byte-compatibility the honor loop (§3) relies on is the conjunction of the two: same layout (from `ComputationLayout`) plus an alias edge (from the config). See [../compiler/tpu-program-serialization.md](../compiler/tpu-program-serialization.md) for the full payload container.

---

## Related Components

| Component | Relationship |
|---|---|
| `xla::HloInputOutputAliasConfig` | The output-indexed alias map this page is built around |
| `xla::CommonPjRtBuffer::ScopedHold` | The RAII donation hold that transfers buffer ownership at execute |
| `tfrt::tpu::AllocateOutputBuffersWithInputReuse` | The run-time honor loop that reuses or allocates per output leaf |
| `xla::OptimizeInputOutputBufferAlias` | The compile-time pass that synthesizes the alias + donor config |
| `xla::InferDispatchInfo` | The per-launch planner that reconciles config + `ComputationLayout` + donation flag |
| `tpu::TpuBuffer` / `TpuSharedMemoryLocation` | The reused HBM object; allocated by [hbm-allocator.md](hbm-allocator.md) |

## Cross-References

- [tpu-buffer-layout.md](tpu-buffer-layout.md) — the padded device shape and `(SublaneCount, LaneCount)` tiling the aliased input/output leaves must agree on for reuse to be byte-correct
- [hbm-allocator.md](hbm-allocator.md) — the `BestFitAllocator` behind both the reused (donated) buffer and the fresh `AllocateTpuBufferWithRetry` allocation
- [overview.md](overview.md) — the `memory_space` tiers; `kPinnedHbm` and why donated buffers behave as pinned for the execute
- [on-device-compaction.md](on-device-compaction.md) — the compactor that may relocate free HBM, but not a buffer held by a donation `ScopedHold`
- [../compiler/tpu-program-serialization.md](../compiler/tpu-program-serialization.md) — the compiled-program payload that carries the `HloInputOutputAliasConfig` beside the `ComputationLayout` and `CompilerMetadata`
- [../compiler/layout-assignment.md](../compiler/layout-assignment.md) — fixes the per-leaf tile so two aliased leaves are layout-compatible
- [../pjrt/stream-executor-pjrt-adapter.md](../pjrt/stream-executor-pjrt-adapter.md) — the `ExecutePrepare` → `PrepareArguments` → `ExecuteLaunch` path that drives the donation handshake
- [../pjrt/buffer-and-memory.md](../pjrt/buffer-and-memory.md) — the `PJRT_Buffer` ABI and external refcounting above the `ScopedHold`
- [../pjrt/executable-execution.md](../pjrt/executable-execution.md) — the executable-execution surface that issues the execute call
- [back to index](../index.md) — Part X — On-Chip Memory & DMA
