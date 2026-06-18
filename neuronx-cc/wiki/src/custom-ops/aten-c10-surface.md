# The Bundled ATen/c10 Dispatch Surface

> *All symbols, RTTI names, source-path strings, and offsets on this page are recovered from the eight `libbuiltincustomop_cpuN.stripped.so` images shipped in `neuronxcc/data/custom_op/` of neuronx_cc **2.24.5133.0** (`+58f8de22`, cp310/311/312 wheels). These are statically-linked **Tensilica Xtensa** ELF32 executables — GPSIMD-core firmware, not host x86. Evidence tag: **D-AC05**.*

## Abstract

A Neuron custom op is C++ that runs on the GPSIMD engine's eight Xtensa cores, and it is written against `at::Tensor`. To make that possible, AWS statically links a **carved-down subset of PyTorch's c10/ATen core** into each per-core firmware image. This page documents that ABI surface: which c10 classes survive the carve, the two Neuron-specific subclasses that replace the host tensor representation, how storage is backed by an on-device allocator, and — the part that surprises everyone — that there is **no `c10::Dispatcher`** behind it. The author's `at::Tensor` is real; the operator-dispatch machinery that normally sits under `aten::` is gone, replaced by a flat ISA-argument table the SDK marshals by hand.

The reference frame is upstream PyTorch. Everything here is "PyTorch's c10, but…": the same `c10::TensorImpl`, `c10::StorageImpl`, `c10::Allocator`, `c10::ScalarType`, `c10::DispatchKey(Set)`, and `c10::intrusive_ptr` headers — recovered verbatim from `INTERNAL ASSERT FAILED at ".../c10/core/TensorImpl.cpp"`-style strings — but compiled for Xtensa, pinned to a 2.0-era source tree, with the `Dispatcher`/`FunctionSchema`/`OperatorEntry` layer **deleted**, `DeviceType` clamped to `CPU`, and `StorageImpl` pointed at a `NeuronAllocator`. The page's job is to let a reader who has the upstream c10 in their head know exactly which subset they may call and which they may not.

For reimplementation, the contract is:

- **The class census** — the c10 core classes that are present (and what is conspicuously absent), proven by RTTI typeinfo names and assert-string source paths.
- **The two Neuron subclasses** — `c10::NeuronTensorImpl : TensorImpl` and `c10::NeuronStorageImpl : StorageImpl`, their place in the vtable hierarchy, and what they override.
- **The storage/allocator path** — `NeuronStorageImpl` → `c10::Allocator` (`GetAllocator`) → `NeuronAllocator` → on-device DRAM windows.
- **The "no Dispatcher" fact** — the SDK's hand-written `arg_parser`/`arg_extractor` path that constructs `at::Tensor` directly from a flat ISA argument array, bypassing operator dispatch entirely.
- **The `ScalarType` ↔ BIR/ISA-dtype bridge** — the `isa_to_torch_dtype` / `dtype().toScalarType()` equivalence the SDK asserts at the boundary.

| | |
|---|---|
| **Images** | `libbuiltincustomop_cpu0..7.stripped.so` — Xtensa ELF32, statically linked, stripped, 579,380 B (≈566 KiB) each |
| **c10 namespace** | `N3c10…` RTTI: `TensorImpl`, `StorageImpl`, `NeuronTensorImpl`, `NeuronStorageImpl`, `UndefinedTensorImpl`, `intrusive_ptr_target`, `AutogradMetaInterface`, `VariableVersion::VersionCounter`, `Error` |
| **Custom classes** | `c10::NeuronTensorImpl` (`N3c1016NeuronTensorImplE`), `c10::NeuronStorageImpl` (`N3c1017NeuronStorageImplE`) |
| **Allocator** | `NeuronAllocator` (`/opt/amazon/custom_op/torch/include/neuron_torch_extension/NeuronAllocator.h`), reached via `c10::GetAllocator` |
| **Device** | `DeviceType::CPU` **only** — `Expected device_type() == DeviceType::CPU to be true` |
| **Dispatcher** | **None.** No `aten::`, `FunctionSchema`, `OperatorEntry`, `Dispatcher.cpp`, or `callBoxed` strings; only the `DispatchKey` *type* survives |
| **PyTorch pin** | ≈ **2.0** — c10 layout + `SymInt` present, `Float8` absent (2.1+), caffe2-era device-type strings |
| **SDK glue** | `/opt/workspace/SundaCustomOpLibrary/custom_op/library/{arg_parser,arg_extractor,utypes,allocator,data_transfer,start_exit}.{hpp,cpp}` |

---

## The c10/ATen Class Census

### Purpose

Establish, against the binary, the exact subset of c10/ATen that a custom-op author may link against. This is the first thing a reimplementer needs: not the full PyTorch core, but the slice that fits in a GPSIMD firmware image and serves a single CPU-only tensor representation.

### What Survives — RTTI Evidence

Every class below is attested by its Itanium-ABI typeinfo name (`_ZTI…` / `_ZTS…` mangled string `N3c10…E`) recovered from `strings` over `libbuiltincustomop_cpu0.stripped.so`. The presence of a typeinfo name means a polymorphic instance of that class is constructed on-device.

| c10 class | Mangled RTTI | Role | Confidence |
|---|---|---|---|
| `c10::TensorImpl` | `N3c1010TensorImplE` | Base tensor representation | CONFIRMED |
| `c10::StorageImpl` | `N3c1011StorageImplE` | Refcounted byte buffer + allocator | CONFIRMED |
| `c10::NeuronTensorImpl` | `N3c1016NeuronTensorImplE` | **Neuron** `TensorImpl` subclass | CONFIRMED |
| `c10::NeuronStorageImpl` | `N3c1017NeuronStorageImplE` | **Neuron** `StorageImpl` subclass | CONFIRMED |
| `c10::UndefinedTensorImpl` | `N3c1019UndefinedTensorImplE` | The singleton "undefined tensor" | CONFIRMED |
| `c10::intrusive_ptr_target` | `N3c1020intrusive_ptr_targetE` | Refcount base for the above | CONFIRMED |
| `c10::AutogradMetaInterface` | `N3c1021AutogradMetaInterfaceE` | Autograd-meta vtable (present but inert, see below) | CONFIRMED |
| `c10::VariableVersion::VersionCounter` | `N3c1015VariableVersion14VersionCounterE` | Version-counter target | CONFIRMED |
| `c10::Error` | `N3c105ErrorE` | Exception type for `TORCH_CHECK`/asserts | CONFIRMED |
| `c10::EnforceFiniteError` | `N3c1018EnforceFiniteErrorE` | `CAFFE_ENFORCE_FINITE` error | CONFIRMED |
| `c10::MemoryReportingInfoBase`, `c10::DebugInfoBase`, `c10::WarningHandler` | resp. mangled | Debug/diagnostic plumbing | CONFIRMED |

Beyond the polymorphic classes, the **header-only** c10 utilities are linked as inline code, proven by their assert source-path strings:

```text
/opt/amazon/include/c10/core/TensorImpl.h           (also .../custom_op/c10/include/...)
/opt/amazon/include/c10/core/StorageImpl.h
/opt/amazon/include/c10/core/Device.h
/opt/amazon/include/c10/core/MemoryFormat.h
/opt/amazon/include/c10/core/ScalarType.h
/opt/amazon/include/c10/core/SymInt.h
/opt/amazon/include/c10/core/SymIntArrayRef.h
/opt/amazon/include/c10/core/impl/SizesAndStrides.h
/opt/amazon/include/c10/util/ArrayRef.h
/opt/amazon/include/c10/util/intrusive_ptr.h
/opt/amazon/custom_op/torch/include/ATen/core/TensorBase.h
/opt/amazon/custom_op/torch/include/ATen/Functions.h
/opt/amazon/custom_op/torch/include/neuron_torch_extension/NeuronAllocator.h
```

> **NOTE —** two include roots appear for the same headers: `/opt/amazon/include/c10/...` and `/opt/amazon/custom_op/c10/include/c10/...`, plus a third build-machine path `/workplace/auderian/custom-ops/dana-libc-cr/src/KaenaPytorchCustomOps/pytorch_source/c10/...`. The third is the *original PyTorch source tree* the c10 subset was carved from; the first two are the installed SDK include trees. Citations to line numbers (e.g. `TensorImpl.h:745`, `:2118`) are from this 2.0-era tree and will not match a current upstream checkout.

### What Is Absent — the Dispatcher

The single most important negative result on this page. A normal libtorch links `c10::Dispatcher`, `c10::OperatorHandle`, `c10::FunctionSchema`, `impl::OperatorEntry`, `KernelFunction`, and the boxed/unboxed `callBoxed`/`redispatch` machinery. **None of those strings exist in any of the eight images.** A sweep for `aten::`, `torch::jit`, `FunctionSchema`, `c10::OperatorName`, `findSchema`, `RegisterOperators`, `TORCH_LIBRARY`, `computeDispatchTable`, and `Dispatcher.cpp` returns empty.

What *does* survive of the dispatch namespace is the **type vocabulary**, not the runtime:

```text
present:   c10/core/DispatchKey.cpp     (enum-to-name table + parseDispatchKey)
present:   c10/core/DispatchKeySet.h    (DispatchKeySet, the bitset)
absent:    Dispatcher / OperatorEntry / KernelFunction / callBoxed / FunctionSchema
```

`DispatchKey.cpp` is linked only because `TensorImpl` carries a `DispatchKeySet` field and the assert/printing paths reference the enum (`parseDispatchKey`, `Autograd*` names, the `DispatchKeySet::299` backend-mask assert). The *table of operator implementations keyed by dispatch key* — the thing the dispatcher exists to consult — is not here.

> **GOTCHA —** the author writes `at::Tensor` and calls `at::` helper functions, so it *looks* like the full ATen dispatch surface is available. It is not. There is no operator registry, so you cannot call an arbitrary `aten::` op and have it route to a kernel — there are no kernels registered and no dispatcher to route to. The only `at::`/`ATen` code that runs is the **inline, header-only** part (`ArrayRef`, `TensorBase` accessors, the handful of `ATen/Functions.h` helpers up to ~line 285). Treat the surface as "`TensorImpl` + storage + the inline accessors," not "libtorch."

---

## The Two Neuron Subclasses

### Purpose

`NeuronTensorImpl` and `NeuronStorageImpl` are the entire reason a c10 subset is shipped at all: they make a host-style `at::Tensor` describe an on-device GPSIMD buffer. Everything else (the c10 base classes, the allocator) exists to support these two.

### Hierarchy and Vtables

Both derive from their upstream c10 base; the RTTI for both base and derived is present, so the inheritance edge is real (a single-inheritance typeinfo emits both `_ZTI` names).

```text
c10::intrusive_ptr_target
        ▲
        │
c10::StorageImpl                         c10::TensorImpl
        ▲                                       ▲
        │                                       │
c10::NeuronStorageImpl                  c10::NeuronTensorImpl
   (N3c1017…)                              (N3c1016…)
        │                                       │
   holds a NeuronAllocator-                 storage_ ─► NeuronStorageImpl
   backed data_ptr, byte size,             sizes_/strides_ (SizesAndStrides)
   refcount/weakcount                       dtype_ (TypeMeta), device_ = CPU
                                            key_set_ (DispatchKeySet, inert)
```

`TensorImpl` is the standard 2.0-era layout: an inline `SizesAndStrides` (`c10/core/impl/SizesAndStrides.h`, the small-vector that inlines up to 5 dims then heap-promotes — `!isInline()` assert at `:311`), a `Storage` holding the `intrusive_ptr<StorageImpl>`, a `TypeMeta dtype_`, an `optional<Device> device_opt_`, a `DispatchKeySet key_set_`, and a `VariableVersion` slot.

### What the Subclasses Override

The override set is inferred from which assert paths and override-only behaviors appear; the binary is stripped, so vtable slots cannot be read by symbol. Confidence is marked accordingly.

| Behavior | Mechanism | Confidence |
|---|---|---|
| Storage bytes live in on-device DRAM, not host heap | `NeuronStorageImpl` constructed with a `NeuronAllocator` data-ptr | CONFIRMED (allocator path attested) |
| `device_type()` is always `CPU` | `device_opt_` set to `CPU`; enforced by the `Expected device_type() == DeviceType::CPU` check | CONFIRMED |
| Autograd disabled | tensors created as inference tensors; `Cannot set version_counter for inference tensor`, `is_inference() && version_counter.enabled()` guard | CONFIRMED |
| `sym_sizes`/`sym_strides` are trivial | `noop_sym_sizes_fn`; `set_sizes_and_strides() called on tensor with symbolic shape` is an error path, i.e. symbolic shapes are rejected | CONFIRMED |
| Custom `release_resources` / dtor returning storage to the allocator | `NeuronStorageImpl` dtor → allocator free | INFERRED (no symbol; standard subclass pattern) |

> **QUIRK —** `c10::AutogradMetaInterface` RTTI is present, yet autograd is dead on-device. The interface vtable is linked because `TensorImpl` references it structurally (the `autograd_meta_` slot type), but the tensors are constructed as **inference tensors** (`InferenceMode`-equivalent), so the version counter is disabled and no `AutogradMeta` is ever attached. Reimplementers should not try to call `.backward()` or `.grad()` — the plumbing is type-present but value-absent.

---

## Storage and the NeuronAllocator

### Purpose

Trace the path from `at::Tensor::data_ptr()` down to a physical DRAM address, which is what a custom op ultimately reads and writes. This is where the upstream c10 `Allocator` abstraction is reconnected to GPSIMD memory.

### The Allocator Chain

`StorageImpl` holds a `c10::Allocator*` (the `allocator_` field; the `:51` assert in `StorageImpl.h` checks it non-null). Upstream, an allocator is registered per `DeviceType` and fetched with `c10::GetAllocator(deviceType)` — and `GetAllocator` is present in the binary. The registered allocator for the Neuron path is `NeuronAllocator`.

```c
// Reconstructed storage-allocation path (Neuron subset of c10::StorageImpl + NeuronAllocator)
Storage make_neuron_storage(size_t nbytes):                 // NeuronStorageImpl ctor
    Allocator* a = c10::GetAllocator(DeviceType::CPU);       // returns the registered NeuronAllocator
    DataPtr dp   = a->allocate(nbytes);                      // NeuronAllocator::allocate
    // dp wraps an on-device DRAM address; NeuronAllocator.h:18 asserts ret == 0
    return new NeuronStorageImpl(/*size=*/nbytes,
                                 /*data=*/std::move(dp),
                                 /*allocator=*/a,
                                 /*resizable=*/false);
```

The `NeuronAllocator` itself is implemented in the SDK's `allocator.cpp`, which translates a c10 allocation request into the GPSIMD DRAM window scheme. The DRAM-window evidence lives in the sibling `data_transfer.cpp`:

```text
data_transfer.cpp:160  dram_addr >= 0x80000 && dram_addr < 0x90000   (a 64 KiB DRAM window)
data_transfer.cpp:171  cpu_id < 8                                    (eight GPSIMD cores)
data_transfer.cpp:240  READ_LOCAL_UREG64(MEM_WINDOW0_LO) == SUNDA_APB_BASE
allocator.cpp:45       mgr != nullptr                                (a memory-manager singleton)
```

> **NOTE —** the `cpu_id < 8` assert and the eight `libbuiltincustomop_cpuN.stripped.so` images are the same fact seen from two sides: one firmware image per GPSIMD Xtensa core, each carrying its own copy of the c10 subset. The eight are **not byte-identical** across cores — they are one program *re-linked* at each core's `0x84000000 + id·0x200000` base (distinct per-core md5s, all 579,380 B), so they differ only by the rebase of absolute addresses. The tensor ABI is identical across all eight; only the link base (and the core index it encodes) differs. See [GPSIMD Xtensa Layout](gpsimd-xtensa-layout.md).

### Sharing an External Pointer

ATen's "from a raw external pointer" path (`at::from_blob`-style) is reachable: the string `To share with a raw external pointer you need to pass in an initialized data_type(TypeMeta).` and the `ATen/Functions.h` helpers (`:197 tensor.is_non_overlapping_and_dense()`, `:240`/`:245`/`:271` `ret == 0`) show the SDK wrapping an existing on-device buffer as a tensor without a fresh allocation — this is how an input/output tensor handed in by the ISA argument array becomes an `at::Tensor`.

---

## Constructing `at::Tensor` Without a Dispatcher

### Purpose

Show the actual code path by which a custom op receives its tensors. Because there is no dispatcher, the SDK marshals a flat ISA argument array into `at::Tensor` objects by hand. This is the function a reimplementer must reproduce to be ABI-compatible.

### The ISA Argument Surface

A custom op is invoked by the GPSIMD runtime with an `isa_args` array — a flat list of typed arguments described by the `NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_*` and `…_TENSOR_SHAPE_TYPE_*` enums. The SDK's `arg_parser` walks it positionally.

```c
// Reconstructed marshalling path — SundaCustomOpLibrary/custom_op/library/{arg_parser,utypes}
function bind_next_tensor(ArgParser& p, IsaArgs& isa_args):     // arg_parser.hpp:66-67
    assert(p.curr_arg_ < isa_args.get_num());
    assert(isa_args.get_types()[p.curr_arg_]
             == NEURON_ISA_TPB_CUSTOM_OP_ARG_TYPE_TENSOR);       // must be a tensor slot

    IsaTensor t_ = isa_args.get_tensor(p.curr_arg_++);
    assert(t_.framework_shape_type
             != NEURON_ISA_TPB_..._SHAPE_TYPE_OUT_OF_LINE_SHAPE);// utypes.hpp:94 — inline shape only
    assert(t_.num_dim != 0);                                     // utypes.hpp:139 — rank >= 1

    ScalarType st = isa_to_torch_dtype(t_.dtype);                // utypes.hpp:65 bridge (see below)
    DataPtr   dp = wrap_device_address(t_.xt_ptr_);              // utypes.hpp:64 — Xtensa-visible ptr
    size_t    nbytes = t_.buffer_bytes;                          // utypes.hpp:74 buffer_bytes > 0

    // Build the tensor directly — no operator dispatch, no schema lookup:
    Storage   s = make_neuron_storage_from_blob(dp, nbytes);     // NeuronStorageImpl over t_.xt_ptr_
    Tensor    aten_t = at::empty({0}, st);                       // or from_blob; sets dtype + CPU device
    aten_t.unsafeGetTensorImpl()->set_storage(s);
    aten_t.unsafeGetTensorImpl()->set_sizes_and_strides(t_.shape, t_.strides);
    assert(aten_t.dtype().toScalarType() == isa_to_torch_dtype(t_.dtype)); // utypes.hpp:65
    return aten_t;
```

A scalar argument (rank-collapsed) is turned into an `at::Scalar` on the same walk: `utypes.hpp:172/178` assert `nelem == 1` with the message `Cannot create an at::Scalar. Argument has more than 1 element`.

> **GOTCHA —** the construction is *direct*: `set_storage` / `set_sizes_and_strides` / `set_storage_offset` are called on a freshly-made `TensorImpl`, never through an `aten::empty` operator routed by a dispatcher. The asserts `set_sizes_and_strides() called on tensor with symbolic shape` and `set_storage_offset() called on an undefined Tensor` are the guard rails on this hand path. A reimplementation that tries to go through `c10::Dispatcher::call(...)` has no registry to call into and will not link.

### The ScalarType ↔ ISA/BIR Dtype Bridge

The boundary between PyTorch's `c10::ScalarType` and the Neuron ISA/BIR dtype enum is a single function pair the SDK asserts on every tensor: `isa_to_torch_dtype(t_.dtype)` must equal `aten_t.dtype().toScalarType()` (`utypes.hpp:65`). The c10 side is the standard `ScalarType` enum (`ScalarType.h` linked; `c10::Half`, `c10::BFloat16`, `c10::complex<…>` typeinfo present); the ISA side is the BIR dtype enum documented in [BIR Dtype Tables](../bir/dtype-tables.md).

| c10 `ScalarType` | RTTI / string evidence | BIR/ISA side | Confidence |
|---|---|---|---|
| `Float` (`float`) | `fully_qualified_type_name… [T = float]` | BIR `float32` | CONFIRMED (c10 type) / INFERRED (mapping) |
| `Double` (`double`) | `[T = double]` | BIR `float64` | CONFIRMED / INFERRED |
| `Half` (`c10::Half`) | `at::Half`, `N…HalfE` type-name | BIR `float16` | CONFIRMED / INFERRED |
| `BFloat16` (`c10::BFloat16`) | `at::BFloat16` | BIR `bfloat16` | CONFIRMED / INFERRED |
| `Int` / `Long` / `Short` / `Char` / `Byte` / `Bool` | `[T = int/long/short/char/...]`, `[T = bool]` | BIR `int32/int64/int16/int8/uint8/uint8` | CONFIRMED / INFERRED |
| `ComplexFloat` / `ComplexDouble` / `ComplexHalf` | `complex<float/double/c10::Half>` | (no BIR analogue) | CONFIRMED type / N/A |

> **QUIRK —** the c10 build is from a **PyTorch 2.0-era** tree, so the `ScalarType` enum here has **no `Float8` variants** (E4M3/E5M2 were added in 2.1). A reimplementer targeting this ABI must not assume the 2.1+ scalar-type set: a sweep for `Float8`/`e4m3`/`e5m2` over all eight images returns empty, while `SymInt` and the caffe2-era `DeviceTypeName()` plumbing are present — together these bracket the pin at 2.0.x.

---

## Adversarial Self-Verification

The five strongest claims, re-challenged against the binary:

| Claim | Tag | Re-challenge result |
|---|---|---|
| No `c10::Dispatcher` — direct function/arg table instead | **CONFIRMED** | Zero `Dispatcher`/`OperatorEntry`/`FunctionSchema`/`callBoxed`/`aten::` strings in any image; only `DispatchKey.cpp`+`DispatchKeySet.h` (the *type*) survive. The hand-marshalling path is positively attested by `arg_parser.hpp`/`utypes.hpp` asserts. |
| `DeviceType::CPU` only | **CONFIRMED** | `Expected device_type() == DeviceType::CPU to be true, but got false` is a live runtime check; `DeviceType.cpp`'s `DeviceTypeName()` table is linked only for diagnostics (it still lists CUDA/XLA names, but no CUDA/XLA tensor is ever made). |
| Two custom classes `NeuronTensorImpl` / `NeuronStorageImpl` | **CONFIRMED** | `N3c1016NeuronTensorImplE` and `N3c1017NeuronStorageImplE` typeinfo present in all eight images; base-class RTTI (`TensorImpl`/`StorageImpl`) co-present, confirming the inheritance edge. |
| `NeuronAllocator`-backed storage | **CONFIRMED** | `NeuronAllocator.h`, `c10::GetAllocator`, `StorageImpl.h:51 allocator_` assert, and `allocator.cpp:45 mgr != nullptr` together trace the chain; DRAM windows in `data_transfer.cpp`. |
| PyTorch ≈ 2.0 pin | **STRONG** | c10 `SymInt`/`SizesAndStrides`/`DispatchKeySet` layout = 2.0-era; **no** `Float8` (would imply ≥2.1); caffe2 `DeviceTypeName` strings present. Exact patch level not recoverable (no `TORCH_VERSION` string), hence STRONG not CONFIRMED. |

No claim failed re-challenge. Items marked INFERRED in the tables above (specific vtable-slot overrides, the exact `ScalarType`→BIR row mapping) remain inferred because the images are stripped of symbols and the dtype LUT was not disassembled byte-for-byte — the *equivalence* is attested (`utypes.hpp:65`), the *enumerated rows* are reconstructed from the c10 side plus the BIR dtype catalog.

---

## Related Components

| Name | Relationship |
|---|---|
| Custom-op CPU ABI / SDK (11.3) | The author-facing SDK headers and entry-point contract that sit on top of this c10 surface |
| GPSIMD Xtensa Layout (11.1) | The eight-core Xtensa ELF32 image these classes are linked into |
| BIR Dtype Tables (7.6) | The ISA/BIR dtype enum that `isa_to_torch_dtype` bridges to `c10::ScalarType` |
| Bitonic SORT / TOPK Builtin | A concrete builtin custom op that consumes this `at::Tensor` ABI |

## Cross-References

- [GPSIMD Xtensa Layout](gpsimd-xtensa-layout.md) — the 8-core Xtensa firmware images that statically link this c10 subset
- [Bitonic SORT / TOPK Builtin](bitonic-sort-topk.md) — a builtin op built against this `at::Tensor` surface
- [BIR Dtype Tables](../bir/dtype-tables.md) — the BIR/ISA dtype enum on the other side of the `isa_to_torch_dtype` bridge
- [The Custom-Op CPU ABI: `extended_isa::sdk`](customop-cpu-abi.md) — Part 11.3, the author-facing entry-point and SDK glue (`SundaCustomOpLibrary`) layered over this surface
