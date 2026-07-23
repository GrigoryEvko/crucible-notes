# metaneff Protobuf + var/mem_ref Device I/O ABI

A NEFF on its own is half a model. The compiler emits the device program plus a
dense, device-side variable table (`mem_ref` keyed by `var_id` 0..N-1 — see
[container byte format](./container-byte-format.md)), but it carries no record of
*which host `at::Tensor` fills which slot*, what dtype/shape the framework must
present, or which outputs alias inputs. That host-side contract lives in a
separate, small protobuf — **`metaneff`** — serialized to a `std::string` and
handed to the native script objects `torch.classes.neuron.Model` /
`SPMDModel` alongside the NEFF bytes. This page recovers that contract from the
**native C++ runtime `libtorchneuron.so`**: the wire schema (decoded from the
generated `_InternalParse`/`_InternalSerialize` bodies, not a `.proto` file), the
in-RAM message struct layouts, and the exact `at::Tensor → nrt_tensor →
var_id → device mem_ref` binding performed at load and at execute.

> **Provenance.** Every proto field number, struct offset, error string, native
> import, and binding step on this page is **OBSERVED** in
> `libtorchneuron.so` (build-id `d7077365`, `torch_neuronx-2.9.0.2.13.24727`,
> not stripped, has RTTI). `.text`/`.rodata`/`.data` are all VMA==fileoffset in
> this binary, so every address below is simultaneously a file offset. Facts
> about the *device-side* var table (`kbin_mr_type`, DMA rings) are **CARRIED**
> from the libnrt analysis and tagged explicitly — they are not visible in
> `libtorchneuron.so`.

Binary path:
`neuronx-misc/extracted/torch_neuronx-2.9.0.2.13.24727+8e870898-py3-none-any/torch_neuronx/lib/libtorchneuron.so`.

---

## 1. Orientation — two key-rings, one shared index space

A compiled model is a **pair** of artifacts:

1. The **NEFF** — device program + device-side `mem_ref` table, keyed by the dense
   `var_id` 0..N-1 the kelf loader's `check_var_ids` enforces
   ([container byte format](./container-byte-format.md)).
2. The **metaneff** — this protobuf. It describes the *same* I/O boundary from the
   host/framework side: an ordered list of `MetaTensor` descriptors (name, shape,
   dtype, role), an `output → input` alias map, and graph-level `ModelConfig`.
   It lives **outside** the NEFF tar, serialized to its own byte string.

The contract that ties the two halves together is a single shared index:

```
metaneff.input_tensors[i] / output_tensors[i]      (host)
        ==  NEFF var_id i  (dense 0..N-1)           (device program)
        ==  nrt tensor-set ordinal i                (runtime handle)
        ==  device mem_ref[var_id i]                (DMA target)
```

The runtime binds `at::Tensor`s into device slots **by (ordinal, name)** where
`ordinal == metaneff index == NEFF var_id` and `name == "input{i}"/"output{i}"`
(the runtime lookup key). The per-tensor `MetaTensor.type`
`{USER_INPUT, INPUT_STATE, INPUT_WEIGHT}` partitions which device slots the host
fills from the user's argument vector versus from internal checkpoint state /
weights.

> **NOTE.** The schema is *shared*: the C++ runtime registers
> `descriptor_table_metaneff_2eproto` (`@0x56ed60`, getter
> `@0x481940`) from the same `FileDescriptorProto` bytes the Python producer ships
> in `proto/metaneff_pb2.py`. proto3, package `metaneff`. So the Python tracer and
> the native consumer cannot disagree on field numbers.

---

## 2. Wire schema — recovered from the native (de)serializers `[HIGH/OBSERVED]`

The tag→field map below is read directly from the switch in
`metaneff::MetaTensor::_InternalParse` (`@0x4847e0`),
`metaneff::MetaNeff::_InternalParse` (`@0x485dd0`), and
`metaneff::ModelConfig::_InternalParse` (`@0x484c40`). Each `case` compares the
varint tag byte (`cmp $0xNN,%sil`) and the handler `lea`/`mov`s the destination
struct offset — both are quoted per row. The serialize direction was
cross-checked against `MetaNeff::_InternalSerialize` (`@0x482f00`), where the tag
bytes appear as literal `movb $0xNN,(%rcx)` stores.

### 2.1 `message MetaTensor` — one tensor at the I/O boundary

| # | field | type | wire | tag | parser store (this+off) | evidence |
|---|-------|------|------|-----|-------------------------|----------|
| 1 | `name` | bytes | LEN | `0x0a` | ArenaStringPtr @ `+0x30` (48) | `lea 0x30(%r12)` @0x484a36 |
| 2 | `shape` | repeated int64 | packed / varint | `0x12` / `0x10` | `RepeatedField<int64>` @ `+0x18` (24) | `lea 0x18(%r12)` @0x484a0d |
| 3 | `data_type` | enum `DataType` | varint | `0x18` | int32 @ `+0x50` (80) | `mov %eax,0x50(%r12)` @0x4849f1 |
| 4 | `content` | bytes | LEN | `0x22` | ArenaStringPtr @ `+0x38` (56) | `lea 0x38(%r12)` @0x4849b2 |
| 5 | `allow_dynamic_batch_size` | bool | varint | `0x28` | uint8 @ `+0x54` (84) | `setne 0x54(%r12)` @0x484999 |
| 6 | `checkpoint_key` | bytes (proto3-opt) | LEN | `0x32` | ArenaStringPtr @ `+0x40` (64); `has_bits\|=1` | `orl $0x1,0x10(%r12)` @0x48493f; `lea 0x40(%r12)` @0x484945 |
| 7 | `type` | enum `Type` | varint | `0x38` | int32 @ `+0x58` (88) | `mov %eax,0x58(%r12)` @0x4848e1 |
| 8 | `user_input_key` | bytes (proto3-opt) | LEN | `0x42` | ArenaStringPtr @ `+0x48` (72); `has_bits\|=2` | `orl $0x2,0x10(%r12)` @0x48485a; `lea 0x48(%r12)` @0x484860 |

Field 2 accepts both the packed form (`0x12 LEN <varints>`) and a single bare
varint element (`0x10 <varint>`) — both `case`s feed the same
`RepeatedField<int64>` @ `+0x18`.

> **GOTCHA.** Fields 6 and 8 are **proto3-optional** (synthetic-oneof) members.
> The descriptor encodes each with `48 00`/`48 01` (`oneof_index`) +
> `88 01 01` (`proto3_optional=true`); the C++ object tracks presence in the
> `_has_bits_[0]` dword @ `+0x10` (`|=1` for `checkpoint_key`, `|=2` for
> `user_input_key`). The synthetic oneof names `_checkpoint_key` / `_user_input_key`
> are present as descriptor strings. Do **not** treat them as plain string fields —
> a reimplementation must emit the has-bit framing or the Python side will read
> them as unset.

**`enum MetaTensor.DataType`** — recovered from the descriptor blob
(`@0x4d6390`+, framed `12 LEN 0a LEN <name> 10 <value>` per value). 16 distinct
value names; **value 11 (0x0b) is a reserved gap** (`INT64=10` at `100a`, the next
entry `FLOAT16` is `100c`=12):

```
UNDEFINED=0  FLOAT=1   INT32=2   BYTE=3   STRING=4  BOOL=5  UINT8=6  INT8=7
UINT16=8     INT16=9   INT64=10  <11 reserved>  FLOAT16=12  DOUBLE=13
BFLOAT16=14  F8E4M3FN=15  F8E5M2=16
```

(The matching C++ enum constants ship as RTTI symbols
`_ZN8metaneff10MetaTensor5FLOATE` … `_ZN8metaneff10MetaTensor6F8E5M2E` @ `0x4d3d…`.)

**`enum MetaTensor.Type`** (descriptor `@0x4d6464`):

```
UNDEFINED_TYPE=0   USER_INPUT=1   INPUT_STATE=2   INPUT_WEIGHT=3
```

### 2.2 `message ModelConfig` — graph-level execution metadata

Decoded from `ModelConfig::_InternalParse` (`@0x484c40`). The three bools land at
contiguous bytes `+0x28`/`+0x29`/`+0x2a`:

| # | field | type | tag | parser store | evidence |
|---|-------|------|-----|--------------|----------|
| 1 | `num_infer` | int64 | `0x08` | (int64) | `cmp $0x8,%sil` @0x484d68 |
| 2 | `timeout` | int64 | `0x10` | (int64) | `cmp $0x10,%sil` @0x484de8 |
| 3 | `optimal_ncg_size` | int64 | `0x18` | (int64) | `cmp $0x18,%sil` @0x484da8 |
| 4 | `async_load` | bool | `0x20` | uint8 @ `+0x28` (40) | `setne 0x28(%rbx)` @0x484e55 |
| 5 | `lazy_load` | bool | `0x28` | uint8 @ `+0x29` (41) | `setne 0x29(%rbx)` @0x484d59 |
| 6 | `return_aliases` | bool | `0x30` | uint8 @ `+0x2a` (42) | `setne 0x2a(%rbx)` @0x484ce1 |

> **CORRECTION.** The `ModelConfig::_InternalParse` body resolves all three bools
> precisely: `async_load=+0x28`, `lazy_load=+0x29`, **`return_aliases=+0x2a`
> `[OBSERVED]`**. Fields 1–3 are int64 (tags `0x08`/`0x10`/`0x18`) and parsed but
> have no observed *setter* in this binary (see §6).

### 2.3 `message MetaNeff` — the top-level artifact

Decoded from `MetaNeff::_InternalParse` (`@0x485dd0`). The repeated-message fields
go through `Arena::CreateMaybeMessage<MetaTensor>` + `RepeatedPtrFieldBase::
AddOutOfLineHelper` (so they are genuine repeated sub-messages, not packed):

| # | field | type | tag | parser store (this+off) | evidence |
|---|-------|------|-----|-------------------------|----------|
| 1 | `input_tensors` | repeated `MetaTensor` | `0x0a` | `RepeatedPtrField` @ `+0x10` (16) | `lea 0x10(%r12)` @0x4860df → `AddOutOfLineHelper` |
| 2 | `output_tensors` | repeated `MetaTensor` | `0x12` | `RepeatedPtrField` @ `+0x28` (40) | `lea 0x28(%r12)` @0x486073 |
| 3 | `model_config` | `ModelConfig` | `0x1a` | message* @ `+0xC8` (200) | `mov 0xc8(%r12),%rsi` @0x48600a |
| 4 | `serialized_graph_def` | bytes (HLO) | `0x22` | ArenaStringPtr @ `+0xB8` (184) | `lea 0xb8(%r12)` @0x485fdf |
| 5 | `name` | bytes | `0x2a` | ArenaStringPtr @ `+0xC0` (192) | `lea 0xc0(%r12)` @0x485f8f |
| 6 | `output_aliases_to` | `map<int64,int64>` | `0x32` | MapField @ `+0x40` (64) | `lea 0x40(%r12)` @0x485f4e |
| 7 | `num_user_inputs` | int64 | `0x38` | int64 @ `+0xD0` (208) | `mov %rax,0xd0(%r12)` @0x485f31 |
| 8 | `num_states` | int64 | `0x40` | int64 @ `+0xD8` (216) | `mov %rax,0xd8(%r12)` @0x485ef1 |
| 9 | `num_weights` | int64 | `0x48` | int64 @ `+0xE0` (224) | `mov %rax,0xe0(%r12)` @0x485e71 |

The nested map entry `MetaNeff.OutputAliasesToEntry` is a
`MapEntryImpl<…, long, long, WireFormatLite::FieldType 3, 3>` (parse
`@0x486bd0`, serialize `@0x486920`) — i.e. `map<int64,int64>` with
**field 1 = key (`output_index`)**, **field 2 = value (`input_index`)**,
describing a donated/in-place output buffer.

> **NOTE — `RepeatedPtrField` walk recipe.** Layout is
> `{ Arena* @+0, int current_size @+8, int total_size @+12, void** rep @+16 }`. For
> `input_tensors` (field base `+0x10`), the element pointer is
> `input_tensors[i] = *(*(MetaNeff + 0x20) + 8*i + 8)` and the count is
> `*(int*)(MetaNeff + 0x18)`. This exact address expression appears verbatim in
> the `SPMDRankedTask` and `TensorSet` consumers below — it is the canonical way
> the native code iterates the boundary tensors.

The on-wire field names are corroborated by descriptor-string fragments shipped in
`.rodata`: `"+\n\rinput_tensors"` (the `\r`=0x0d is `strlen("input_tensors")`),
`"\n\nnum_states"` (`0a 0a` = name-tag + len 10), `"\n\x0bnum_weights"` (len 11),
`output_tensors`, `serialized_graph_def`, `output_aliases_to`, `num_user_inputs`.

---

## 3. Native message struct layouts `[HIGH/OBSERVED]`

These are the in-RAM protobuf `Message` objects, with the standard libprotobuf
header (`vptr` @+0, `InternalMetadata` @+8, `_has_bits_`/`_cached_size_` @+0x10).
Offsets below are cross-confirmed between the `_InternalParse` *writers* (§2) and
the `neuron::` *readers* (§4–5).

```c
// metaneff::MetaTensor  (RTTI _ZTSN8metaneff10MetaTensorE @0x4d3d80)
struct MetaTensor {                       // offsets OBSERVED
  /* +0x00 */ void*    _vptr;             // generated Message vtable
  /* +0x08 */ uint64_t _internal_metadata;
  /* +0x10 */ uint32_t _has_bits_;        // bit0=checkpoint_key, bit1=user_input_key
  /* +0x14 */ uint32_t _cached_size_;
  /* +0x18 */ RepeatedField<int64> shape; // { int current_size@+0x18, int total@+0x1c,
                                           //   Arena*, int64* data@+0x20 }  -> field 2
  /* +0x30 */ ArenaStringPtr name;        // field 1   ("input{i}"/"output{i}")
  /* +0x38 */ ArenaStringPtr content;     // field 4   (inline const bytes)
  /* +0x40 */ ArenaStringPtr checkpoint_key; // field 6 (state buffers)
  /* +0x48 */ ArenaStringPtr user_input_key; // field 8
  /* +0x50 */ int32_t  data_type;         // field 3   (-> MetaTensor.DataType)
  /* +0x54 */ uint8_t  allow_dynamic_batch_size; // field 5
  /* +0x58 */ int32_t  type;              // field 7   (-> MetaTensor.Type; ==1 USER_INPUT is the hot test)
};

// metaneff::MetaNeff   (RTTI _ZTSN8metaneff8MetaNeffE @0x4d4010)
struct MetaNeff {                          // offsets OBSERVED
  /* +0x00 */ void*    _vptr;
  /* +0x08 */ uint64_t _internal_metadata;
  /* +0x10 */ RepeatedPtrField<MetaTensor> input_tensors;  // count *(int*)(+0x18), rep *(+0x20)
  /* +0x28 */ RepeatedPtrField<MetaTensor> output_tensors; // count *(int*)(+0x30), rep *(+0x38)
  /* +0x40 */ MapField<int64,int64> output_aliases_to;
  /* +0xB8 */ ArenaStringPtr serialized_graph_def; // field 4 (HLO module)
  /* +0xC0 */ ArenaStringPtr name;                 // field 5
  /* +0xC8 */ ModelConfig*   model_config;         // field 3
  /* +0xD0 */ int64_t  num_user_inputs;            // field 7
  /* +0xD8 */ int64_t  num_states;                 // field 8
  /* +0xE0 */ int64_t  num_weights;                // field 9
};

// metaneff::ModelConfig (RTTI _ZTSN8metaneff11ModelConfigE @0x4d3da0)
struct ModelConfig {
  /* +0x00..0x17 */ /* Message header + int64 fields 1..3 (num_infer/timeout/optimal_ncg_size) */
  /* +0x28 */ uint8_t async_load;       // field 4  (OBSERVED: set_async_load store)
  /* +0x29 */ uint8_t lazy_load;        // field 5  (OBSERVED: set_lazy_load store)
  /* +0x2a */ uint8_t return_aliases;   // field 6  (OBSERVED: parser setne +0x2a)
};
```

These host message objects are the framework-side twins of the device `mem_ref`
(field-exact layout is in [host-runtime struct
layouts](../appendix/struct-host-runtime-layouts.md)).

---

## 4. The `at::Tensor → var_id` binding `[HIGH/OBSERVED]`

This is the core deliverable: the path from a host `at::Tensor` to a device
`mem_ref`. It splits cleanly into a **load-time** step (allocate one device
`nrt_tensor` per `MetaTensor`, build the tensor-set) and an **execute-time** step
(stage host bytes in, launch, collect outputs).

### 4.1 dtype + byte-size — `allocate_tensor` / `empty_tensor`

`neuron::allocate_tensor(MetaTensor&, device_id)` (`@0x388f40`) and
`neuron::empty_tensor(MetaTensor&, batch, c10::Device)` (`@0x36d650`) materialize
the host-visible buffer:

- **dtype.** `data_type` (`MetaTensor+0x50`) is looked up in the process-global
  hash map **`neuron::PROTO_AT_DTYPE_MAP`** — an
  `unordered_map<metaneff::MetaTensor_DataType, caffe2::TypeMeta>` (the insert node
  type is the literal symbol seen at the `TensorSet` ctor call site `@0x189e60`).
  The resulting `caffe2::TypeMeta` index is asserted `<= 0x2e` (46) — `empty_tensor`
  does `cmp $0x2e,%ax` `@0x36d86a` — else
  `caffe2::TypeMeta::error_unsupported_typemeta()`.
- **shape.** The `int64[]` at `MetaTensor+0x20` (count `MetaTensor+0x18`) is copied
  into the new tensor. `empty_tensor` reads exactly those offsets
  (`mov 0x20(%rsi),%r8` `@0x36d667`, `movslq 0x18(%rsi),%rax` `@0x36d66b`).
- **dynamic batch.** When `batch >= 0` **and**
  `allow_dynamic_batch_size` (`MetaTensor+0x54`, `cmpb $0x0,0x54(%r15)`
  `@0x36d79c`) is set, `shape[0]` is overridden to `batch` — dynamic batching
  rewrites the leading dim per launch.
- The buffer is produced via `at::zeros` / `at::empty_memory_format` on the target
  device; this is what `nrt` later DMAs.

### 4.2 DataType → caffe2 ScalarType map `[HIGH/OBSERVED]`

`PROTO_AT_DTYPE_MAP` is built once at static init by
`_GLOBAL__sub_I_tensor_util.cpp` (`@0x15e790`), which stages a contiguous array of
`{int32 key, int16 value}` pairs on the stack (keys `0,1,…,16` via
`movl $0x1..$0x10`) then bulk-inserts. The recovered key→ScalarType-index map:

| `MetaTensor.DataType` | caffe2 `ScalarType` | index |
|-----------------------|---------------------|-------|
| `FLOAT(1)`            | `Float`             | 6 |
| `INT32(2)`            | `Int`               | 3 |
| `BYTE(3)`             | `Byte`              | 0 |
| `STRING(4)`           | `Byte`              | 0 |
| `BOOL(5)`             | `Bool`              | 11 |
| `UINT8(6)`            | `Byte`              | 0 |
| `INT8(7)`             | `Char`              | 1 |
| `UINT16(8)`           | `UInt16`*           | 27 |
| `INT16(9)`            | `Short`             | 2 |
| `INT64(10)`           | `Long`              | 4 |
| `FLOAT16(12)`         | `Half`              | 5 |
| `DOUBLE(13)`          | `Double`            | 7 |
| `BFLOAT16(14)`        | `BFloat16`          | 15 |
| `F8E4M3FN(15)`        | `Float8_e4m3fn`     | 24 |
| `F8E5M2(16)`          | `Float8_e5m2`       | 23 |

The value immediates observed in the initializer are
`{0x06,0x03,0x00,0x0b,0x00,0x01,0x1b,0x02,0x04,0x05,0x07,0x0f,0x18,0x17,0x10}`
(`0x1b`=27 is the modern `UInt16` ScalarType slot — marked `*` as the
disambiguation between the two `0x00`=`Byte` entries; `0x1b`'s key assignment is
INFERRED from slot order, the rest are OBSERVED). The signed/unsigned collapse on
the Python producer side (U32/S32→`INT32`, etc.) funnels several XLA types into
one metaneff `DataType`, then this map collapses again into one `ScalarType`. The
per-element byte size used downstream comes from `caffe2::scalarTypeItemSizes`
(`@0x4b66e0`, bytes `01 01 02 04 08 02 04 08 04 08 10 01 …` = Byte1 Char1 Short2
Int4 Long8 Half2 Float4 Double8 …). Full direction map: see [ScalarType↔dtype
rosetta](../abi/scalartype-dtype-rosetta.md).

### 4.3 Load-time — `TensorSet(MetaNeff&, is_input_set, device)` `@0x189b20`

The constructor builds one device tensor-set from one side (inputs *or* outputs)
of the boundary. Observed call sequence:

```c
// neuron::TensorSet::TensorSet(const metaneff::MetaNeff& mn, bool is_input, int dev)
RepeatedPtrField<MetaTensor>& v = is_input ? mn.input_tensors   // +0x10
                                           : mn.output_tensors; // +0x28
int n = *(int*)((char*)&v + 0x08);                 // current_size

nrt_allocate_tensor_set(&set);                     // @plt 0x140d20
// status 1003 (NRT pending/async) tolerated; other nonzero ->
//   torchCheckFail "Failed to allocate tensor set"

for (int i = 0; i < n; ++i) {                      // i == metaneff order == NEFF var_id
    const MetaTensor& t = *(*(rep) + 8*i + 8);
    ScalarType st  = PROTO_AT_DTYPE_MAP[t.data_type];     // @0x189e60 hashtable
    size_t   isz   = caffe2::scalarTypeItemSizes[st];     // @0x4b66e0
    size_t   bytes = isz;
    for (d in t.shape) bytes *= d;     // OBSERVED: imul (%rsi),%rdx chain @0x189cf9..

    nrt_tensor_allocate(&dev_tensor, bytes, ...);  // @plt 0x13f8c0
    //   status 1003 tolerated; else "Failed to allocate neuron runtime tensor"
    nrt_add_tensor_to_tensor_set(set, name, dev_tensor); // @plt 0x142230
    //   else "Failed to add neuron tensor to the input set"
}
```

This realizes `MetaTensor[i] → nrt_tensor[i] → (via nrt_load's var table)
mem_ref[var_id i]`. **The tensor-set ordinal IS the `var_id`.**
`neuron::TensorSetPool` (`@0x189780`) is the multi-instance pooled variant: it
allocates one `TensorSet` per model replica/NCG.

> **GOTCHA — NRT status 1003 is success-ish.** Both `nrt_allocate_tensor_set` and
> `nrt_tensor_allocate` treat status **1003** (pending/async) as non-fatal; only
> *other* non-zero values throw. A reimplementation that treats any non-zero NRT
> status as an error will spuriously fail on async allocation.

### 4.4 Per-rank task — `SPMDRankedTask(nrt_model*, …, MetaNeff&, rank)` `@0x3895f0`

The SPMD constructor builds one task per rank. It scans `input_tensors`
(`count *(int)(mn+0x18)`), counts `USER_INPUT` members (`MetaTensor.type==1`) into
`n_user_inputs`, and pre-allocates per-instance input tensors via
`allocate_tensor` for the instance count. It scans `output_tensors`
(`count *(int)(mn+0x30)`) joined against `output_aliases_to` (`MetaNeff+0x40`)
through a `SyncMapWithRepeatedField`: an **output aliased to an input is a donated
buffer** — the task skips materializing a fresh tensor and binds the donor's
`nrt_tensor` instead. The task holds the input/output `nrt_tensor*[]` vectors and
the `at::Tensor` staging copies.

### 4.5 Execute-time — `SPMDRankedTask::run()` `@0x3880c0`

The full host→device launch. Observed call order (`objdump` of `run`):

```c
// neuron::SPMDRankedTask::run()
neuron::validate(inputs, metaneff, /*is_input*/1);          // @0x17aef0 (§5)

for each input i:
    name = input_tensors[i].name;        // *(*(mn+0x20)+8*i+8) + 0x30
    if (tensor.device() != Neuron(20))
        nrt = NeuronTensorImpl::CreateSlice(tensor);  // dynamic-batch slice
        nrt_tensor_write(nrt, host_bytes);   // @plt 0x13fc50; fail -> status 3
    set_input(nrt, name, i);             // @0x387fe0  -> slot[i], name = lookup key

nrt_execute(model, in_set, out_set);     // @plt 0x1412f0; status 0/1003 ok, else 1
//   on any error path: nrt_tensor_free() the freshly-allocated device tensors

for each output i (when n_outputs set):
    nrt = get_output_tensor(i); CreateSlice; name = output_tensors[i].name;
    set_output(nrt, name, i);            // @0x388010
```

`set_input` / `set_output` are six-instruction slot binders — the cleanest
evidence on this page that **ordinal == array slot** and **name == device lookup
key**:

```asm
; neuron::SPMDRankedTask::set_input(nrt_tensor* t, const char* name, int ordinal)
387fe6:  mov    0x60(%rdi),%rdx        ; input slot array base = this+0x60
387fed:  lea    (%rdx,%rcx,8),%rdx     ; slot = base + 8*ordinal
387ff1:  cmp    %rax,(%rdx)            ; already bound to this tensor?
387ff4:  jne    388000
387ff8:  ret                           ; -> no-op if pointer unchanged
388000:  mov    %rax,(%rdx)            ; store nrt_tensor* into slot[ordinal]
388003:  mov    0x10(%rdi),%rdi        ; tensor-set handle (this+0x10)
38800a:  jmp    nrt_add_tensor_to_tensor_set ; (set, NAME, tensor)  -- name still in %rsi
```

`set_output` (`@0x388010`) is identical with the **output** slot array at
`this+0x78` and the output tensor-set handle at `this+0x18`. Both **only re-bind**
when the slot pointer actually changes — a launch that reuses the same staging
buffer skips the `nrt_add_tensor_to_tensor_set` round-trip.

> **NOTE — why the name must be `"input{i}"`.** The native binder passes the
> `MetaTensor.name` string straight into `nrt_add_tensor_to_tensor_set` as the
> device-side tensor key. The ordinal is the dense `var_id` slot. So a metaneff
> whose tensor names are not exactly `"input0"`, `"input1"`, … (resp.
> `"output{i}"`) will bind the buffer to a key the NEFF var table never registered
> — the dense `var_id 0..N-1` invariant the kelf loader checks
> ([container byte format](./container-byte-format.md)) is mirrored host-side by
> this naming requirement.

### 4.6 Output aliasing — `create_output_tensors(...)` `@0x1994c0`

`neuron::create_output_tensors(inputs, &outputs, metaneff, device)` iterates
`output_tensors`; for each output index it does a `Map` lookup in
`output_aliases_to` (`"key not found"` / `"CHECK failed: it != end()"`). If the
output **is** aliased to input *j*, the returned `at::Tensor` reuses input *j*'s
buffer (donated / in-place) rather than allocating; non-aliased outputs get a
fresh `allocate_tensor`. This realizes the proto3 `map<int64,int64>
output_aliases_to = output_idx → input_idx` as tensor-layer donation — the host
mirror of the compiler's planned device-side buffer reuse.

---

## 5. Input validation — `neuron::validate(inputs, MetaNeff&, is_input)` `@0x17aef0`

The gate run before every execute. Checks, in order:

- **Count.** When `is_input != 1`, `metaneff.input_tensors.current_size`
  (`*(int)(mn+0x18)`) must equal `inputs.size()` — else `torchCheckFail`
  (`"Expected npts->impl()->size() == tensors.size()"`). The `is_input==1`
  fast-path used by `SPMDRankedTask::run` skips the strict count check because
  states/weights are bound internally, not from the user vector.
- **Per-tensor (only `MetaTensor.type==1` USER_INPUT** — states/weights are
  runtime-internal and skipped):
  - **dtype.** `PROTO_AT_DTYPE_MAP[data_type]` vs the `at::Tensor`'s `TypeMeta` →
    `"Expected tensor.dtype() == dtype"`.
  - **shape.** `tensor.sizes()` vs `MetaTensor.shape` element-wise (modulo the
    dynamic-batch leading dim) → `"Expected tensor.sizes() == sizes"`.
  - **dynamic-batch sanity.** A 0-dim (scalar) tensor with
    `allow_dynamic_batch_size=True` is rejected:
    `"Scalar tensor cannot have allow_dynamic_batch_size=True"`.
- An out-of-range ordinal trips `"Expected ordinal < nrt_tensors_.size()"`.

`neuron::get_slices(inputs, RepeatedPtrField<MetaTensor>&, long batch, bool)`
(`@0x197b80`) computes the per-launch batch slices (via `TensorIndexing`) honoring
each tensor's `allow_dynamic_batch_size` — the dynamic-batch fan-out that feeds
`CreateSlice`.

All eight error strings above are present in `.rodata`
(`@0x4a5158`, `0x4a5188`, `0x4a80d0`, `0x4a8170`, `0x4b63c0`, `0x4a8678`,
`0x4a8020`, `0x4b1f0e`).

---

## 6. Native writers of the "Python-unset" fields `[HIGH/OBSERVED]`

The `torch.classes.neuron.Model` setters mutate the **embedded serialized
metaneff**. The Model object stores the metaneff bytes as a `std::string` at
`neuron::Model+0x18`; each setter **parses → mutates → re-serializes**:

```c
// neuron::Model::set_async_load(bool a)  @0x283820
metaneff::MetaNeff mn;                                 // @0x28383f ctor
mn.ParseFromString(this+0x18);                         // @0x28384a
ModelConfig* cfg = mn.model_config ?:                  // CreateMaybeMessage<ModelConfig>
                   Arena::CreateMaybeMessage<ModelConfig>(arena);  // @0x283890
*(uint8_t*)(cfg + 0x28) = a;                           // @0x28385c  field 4 async_load
mn.SerializeToString(this+0x18);                       // @0x283866
```

- `set_async_load` → `ModelConfig+0x28` (#4). `[OBSERVED @0x28385c]`
- `set_lazy_load` (`@0x2838b0`) → `ModelConfig+0x29` (#5), same shape.
- `set_dynamic_batching(bool)` (`@0x2834b0`): parse, then for every
  `input_tensors[i]` and `output_tensors[i]` set `MetaTensor+0x54 = a`
  (`allow_dynamic_batch_size`, #5), re-serialize, then force a reload via
  `Model::model()`/`unload()`.
- `set_dynamic_batching_mixed(vector<int64>, vector<int64>)` (`@0x2835b0`) is the
  per-tensor heterogeneous variant (different dims per slot).

So `allow_dynamic_batch_size` (MetaTensor #5) and `ModelConfig.async_load`/
`lazy_load` (#4/#5) **are written natively**, toggled per setter call — they are
not left at proto3 defaults.

> **Open (INFERRED).** `num_user_inputs`/`num_states`/`num_weights` (#7–9) and
> `MetaTensor.content` (#4) have **no** observed native writer in
> `libtorchneuron.so` — the parser reads them (fast counts / inline-const slot)
> but nothing here sets them. They are most likely producer/compiler-filled or
> left 0. The `ModelConfig` int64s (#1–3) are parsed but have no setter in this
> binary either (likely consumed by NCG sizing inside libnrt).

---

## 7. Load / object graph — `Model` & `SPMDModel` script classes `[HIGH]`

`torch.classes.neuron.Model` and `SPMDModel` are c10 `intrusive_ptr` custom
classes (RTTI `_ZTIN6neuron5ModelE @0x5523a8`, `_ZTIN6neuron9SPMDModelE
@0x554c90`; vtables `@0x552498` / `@0x554dd0`).

- `SPMDModel(const std::string& neff, const std::string& metaneff, int64
  local_ranks, int64 world_size)` (`@0x36f580`) stores the metaneff bytes,
  `nrt_load`s the NEFF (a native import) to an `nrt_model*`, then builds one
  `SPMDRankedTask` per rank (§4.4) bound to that `MetaNeff`.
- `Model` lifecycle: `load`/`blocking_load`/`unload`/`load_collectives`/
  `instance_count`/`model`/`set_neuron_devices`. `load()` lazily `nrt_load`s;
  `set_lazy_load`/`set_async_load` gate that (and mutate the embedded
  `ModelConfig`, §6).
- The boxed entry is `neuron::forward_v2(vector<at::Tensor>,
  intrusive_ptr<Model>)` (`@0x2893d0`), which funnels to `neuron::forward_batch`
  (`@0x178f80`) — that runs `validate` + the `SPMDRankedTask::run` binding loop
  (§4.5) and re-collects outputs via `create_output_tensors` (§4.6). The
  `forward_v2_tuple<N>` arity-N wrappers handle fixed-output-count graphs.

> **CORRECTION.** The address `0x13f920` is the boxing/PLT thunk. The real
> `neuron::forward_v2(vector<at::Tensor>, intrusive_ptr<Model>)` implementation is
> at **`0x2893d0`** (`@0x178f80` is `forward_batch`). Use `0x2893d0` when
> navigating the de-boxed call graph.

The full set of `nrt_*` calls this binding leans on are all UND imports versioned
`@NRT_2.0.0`: `nrt_load`, `nrt_execute`, `nrt_allocate_tensor_set`,
`nrt_tensor_allocate`, `nrt_add_tensor_to_tensor_set`, `nrt_tensor_write`,
`nrt_tensor_free`, `nrt_get_tensor_from_tensor_set` (see
[public API table](../runtime/public-api-table.md) and
[host↔device descriptor handoff](../runtime/host-device-descriptor-handoff.md)).

---

## 8. Join to the device var table `[HIGH; device side CARRIED]`

The host key-ring (this page, OBSERVED in `libtorchneuron.so`) lines up with the
device key-ring (the NEFF var table, `parse_one_variable` in libnrt — **CARRIED**
from the libnrt analysis, not visible here):

| metaneff (host, OBSERVED) | NEFF var table (device, CARRIED) |
|---------------------------|----------------------------------|
| `MetaTensor` index *i* (input then output) | `var_id i` (dense 0..N-1; `check_var_ids`) |
| `MetaTensor.name "input{i}"/"output{i}"` | var `name` key (`parse_one_variable`) |
| `MetaTensor.type USER_INPUT(1)` (input) | `#transfer-type "input"` → `MR_INPUT(5)` |
| `MetaTensor` (output_tensors) | `#transfer-type "output"` → `MR_OUTPUT(6)` |
| `MetaTensor.type INPUT_STATE(2)` (+`checkpoint_key`) | `"state-buffer"` → `MR_SB(1)` |
| `MetaTensor.type INPUT_WEIGHT(3)` | weight bin / `"buffer"` → `MR_BUFFER(3)` |
| `MetaTensor.shape` / `data_type` | `mem_ref.shape[8]` / `mem_ref.dtype` |
| `output_aliases_to[out]=in` | backing-buf / referenced-`var_id` aliasing |
| `ModelConfig` (host-only) | *(no device analog — load/exec policy)* |
| `serialized_graph_def` (HLO) | the source the compiler lowered to the NEFF |

> **SHARED ANCHOR — `kbin_mr_type` `[CARRIED, libnrt]`.** The device-side
> `mem_ref.type` enum (recovered from `parse_one_variable` in libnrt, **not** in
> `libtorchneuron.so`) is:
> `MR_INVALID=0, MR_SB=1, MR_BUFFER_STAGED=2, MR_BUFFER=3, MR_TMP_BUF=4,
> MR_INPUT=5, MR_OUTPUT=6, MR_PTR=7, MR_VIRTUAL_TMP_BUF=8, MR_LIST=9,
> MR_PTR_TABLE=10, MR_REMOTE=11`. These values must match the
> [relocation-weights](./relocation-weights.md) page and the
> [host-runtime struct layouts appendix](../appendix/struct-host-runtime-layouts.md).

The runtime realizes the join as: host `at::Tensor` → `nrt_tensor` (tensor-set
ordinal == `var_id`) → `nrt_load`'s device `mem_ref` → the DMA ring the compiler
assigned for that `var_id` (for a GPSIMD custom op that is the custom-op ring
class). `nrt_execute()` triggers the NEFF; completion is signalled back via the
Notification Queue. The chain from the framework tensor object down to the c10
impl is in [tensor object chain](../abi/tensor-object-chain.md).

---

## 9. Worked example — serializing a 1-input / 1-output MetaNeff

The bytes `MetaNeff::_InternalSerialize` (`@0x482f00`) emits for an input
(`var_id 0`, fp32 `[1,1,1,32]`) and one output (`var_id 2`, fp32 `[1,1,1,32]`),
top level only:

```
0A LEN <MetaTensor input>          ; field 1 input_tensors
   0A 06 "input0"                  ;   field 1 name
   12 04 01 01 01 20               ;   field 2 shape=[1,1,1,32] (packed int64 varints)
   18 01                           ;   field 3 data_type=FLOAT(1)
   38 01                           ;   field 7 type=USER_INPUT(1)
12 LEN <MetaTensor output>         ; field 2 output_tensors
   0A 07 "output0"                 ;   name
   12 04 01 01 01 20               ;   shape
   18 01                           ;   data_type=FLOAT(1)
   38 00                           ;   type=UNDEFINED_TYPE(0)
1A LEN <ModelConfig>               ; field 3 model_config  (e.g. 30 01 -> return_aliases=true)
22 LEN <hlo bytes>                 ; field 4 serialized_graph_def
                                   ; (an alias entry would add: 32 04 08 <out> 10 <in>)
```

This literal byte string is what gets handed to `SPMDModel(neff, THIS, ranks,
world)`. Note `38 01` (`type` tag `0x38`, value `USER_INPUT=1`) and the
`return_aliases` bool emitted by `ModelConfig` serialize as `30 01` — both tags
match the parser switch in §2.

---

## 10. Open items `[LOW]`

- No native writer observed for `num_user_inputs`/`num_states`/`num_weights`
  (#7–9) or `MetaTensor.content` (#4) — producer/compiler-set or default-0 (§6).
- `ModelConfig.num_infer`/`timeout`/`optimal_ncg_size` (#1–3) parse but have no
  setter in this binary; likely consumed by NCG sizing in libnrt.
- The exact `NeuronTensorImpl::CreateSlice` dynamic-batch slicing arithmetic
  (TensorIndexing offsets) — body present, math not fully traced.
- The device-side confirmation that `nrt_tensor` for `var_id i` lands on the
  `mem_ref`/DMA ring the compiler emitted is device-disasm scope (Xtensa
  `ncore2gp`), out of this host-side page.
