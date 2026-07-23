# BIR-JSON Record Schema by Op-Family — the 110-Op Catalog

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the cp310 build of `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`, 9.5 MB). VA == file offset for `.text` (`0x1820c0…`) and `.rodata` (`0x708000…`); cp311/cp312 carry the identical ABI but the virtual addresses drift. Key strings and `this`-relative field offsets are version-stable.*

## Abstract

This is the authoritative per-op field reference for the BIR-on-disk JSON wire. Every `bir::Instruction` subclass serializes through three generated methods — a 49-byte `createFromJson` factory stub, a `readFieldsFromJson` reader, and a `toJson` writer — and the key set those two move is the op's **record schema**. The page catalogs all 110 `InstructionType` ordinals (0–109) grouped by op-family, and for every op gives its op-specific key set: the JSON key string, the C++ `this`-relative field offset it (de)serializes to, the deserializer type (`from_json<Enum>`, `MaybeAffine<T>`, `SerializableFloat`, `QuasiAffineExpr`, a vector, or a nested object), and the ctor default.

Two facts shape the whole catalog. **First, every record is two layers.** A shared `bir::Instruction` base header (`opcode`, `engine`, `ins`/`outs`/`indirection_ins`, dependencies, `sync_info`, `debug`, …) is emitted by `bir::Instruction::toJson` (`0x2e2ea0`) for *every* op and parsed by `bir::Instruction::createFromJsonHelper` (`0x2e01b0`); the per-op `readFieldsFromJson`/`toJson` add only the op-specific keys tabulated here. That base header is owned by [the `bir::Instruction` base-struct page](./instruction-base.md), not repeated per-op. **Second, the per-op layer is astonishingly thin.** Of the 110 ops, a large fraction carry *zero* op-specific keys — their `readFieldsFromJson` is a single ICF-folded `ret` and they emit a header-only record; their entire semantics rides on the operand tensors (`ins`/`outs`) and the opcode discriminator. The richest single op is `InstBIRKernel` with ~16 keys; the matmul base carries 13; `InstActivation` 11; most ops carry 0–4.

This is the JSON **record** layer. The L1≠L3 wire *encoding* (how the field values are packed for silicon, the comparison reorder, the runtime-byte form) is a separate concern owned by the [AluOpType + mode-enum](./aluop-modes.md) and front-end wire pages — cross-referenced here, never duplicated. The schema is recovered entirely from `libBIR.so`: `libBIRParserDumper.so` defines a *textual* pretty-printer (`bir::Dumper::visit*`) used here as a cross-check, but holds **zero** `readFieldsFromJson`/`toJson` — the JSON schema lives exclusively in `libBIR.so`.

For reimplementation, the contract is:

- The two-layer record model: shared base header (owned elsewhere) **plus** the op-specific key set per ordinal, tabulated below.
- For each op: the exact key spelling, field offset, type, default, and required-vs-optional emit/parse discipline.
- The non-obvious per-family structure: the **three** independent DMA class trees, the control-flow `EventSemaphore`-base thunk chain, the kernel-op key sets, and the operand-nested indirect-AP keys that are *not* per-op keys.

| | |
|---|---|
| **Defining binary** | `libBIR.so` (cp310; md5 `12bb979f7ca41248252abb0f16b2da98`) |
| **Base header writer / parser** | `bir::Instruction::toJson` `0x2e2ea0` · `createFromJsonHelper` `0x2e01b0` |
| **Factory stub (all ops)** | `Inst<X>::createFromJson` — 49 B: `insertElement<Inst<X>>` + `readFieldsFromJson` |
| **JSON library** | `nlohmann::json_abi_v3_11_3` (ordered map → stable key insertion order) |
| **Opcode roster** | `bir::InstructionType` 0–109 — see [the 110-opcode enum](./instruction-type.md) |
| **Op-specific base offset** | First own field of every subclass is at **`+0xF0`** (the base object is 0xF0 = 240 B) |
| **Textual cross-check (not the wire)** | `libBIRParserDumper.so` `bir::Dumper::visit*` |

---

## The record model & method dispatch

### Two key-sets per record

Every wire record is the union of two disjoint key sets:

```text
{ BIR JSON record for op X }
  = COMMON HEADER  (bir::Instruction::toJson @0x2e2ea0 — emitted for EVERY op)
      opcode · engine · engine_id · ins · outs · indirection_ins · predicates
      order · origin · dependencies · loop_carried_dependencies
      unroll_dependencies · optin_passes · loopnest · sync_info
      scheduled_start · scheduled_end · debug
      dma_trigger_debug_update_semaphore_id   (DMA-guarded)
  ⊎ OP-SPECIFIC keys  (Inst<X>::toJson — only the rows in this page's tables)
```

The common header is anchored verbatim to the `.rodata` refs inside `Instruction::toJson` (`0x2e2ea0`) and is the subject of [the base-struct page](./instruction-base.md); it is listed here once and never repeated. The op-specific keys are the catalog.

> **NOTE —** the common header does **not** carry `target` / `on_true` / `queue` / `arguments`. Those are *op-emitted* (a `BasicBlock*` or operand reference serialized as the block/object name string), even though the corresponding object is *linked* later by the common `createFromJsonHelper` successor/operand resolver, not by the op's own reader. So a control-flow op's reader key set can omit `target` while its writer emits it — see [§ Control-flow family](#control-flow-family).

### Read vs write mechanics

The (de)serializers are generated against `nlohmann::json_abi_v3_11_3`. The reader/writer idiom is uniform across all 110 ops:

```c
// reader: Inst<X>::readFieldsFromJson(self, j)
//   per op-specific key:
if (REQUIRED)                                  // json.at<char const(&)[N]>("key")
    from_json(j.at("key"),  self->field);      //   throws type_error if absent
else /* OPTIONAL */                            // find-guard: j.find("key") != j.end()
    if (j.find("key") != j.end())              //   std::string::compare lookup loop
        from_json(j["key"], self->field);      //   else field keeps its ctor default

// writer: Inst<X>::toJson(j) const
//   j["key"] = field;   via MaybeAffine<T>::writeIntoJson / bir::to_json(enum)
//                       / adl_serializer<T>::to_json
//   MaybeAffine + skip-default fields are emitted ONLY when non-default
```

Two discriminators a reimplementer must get right:

- **Required vs optional.** A key read with `json.at<char[N]>` is **required** (throws if absent). A key reached through a `find(key) != end()` guard is **optional** — a missing key leaves the field at its ctor default. The `[N]` template argument equals `strlen(key)+1` and is itself a spelling check (e.g. `"op"`→`at[3]`, `"reduce_cmd"`→`at[11]`, `"is_first_reduce"`→`at[16]`).
- **Skip-default emission.** `MaybeAffine<T>` fields and most enum scalars are emitted by `toJson` only when the value is non-default (the guard is `value != 0` or `affine-flag set`), so an omitted key on the wire means "default". `SerializableFloat` and the unconditional-`at()` enums are always emitted. The writer's skip-default discipline is the subject of [the JSON-writer page](./json-writer.md); the **default** column in every table below is what the field reads as when the key is absent.

> **QUIRK —** `MaybeAffine<T>` (0x28 B) is a tagged union `{value : T, affine-flag, QuasiAffineExpr}` — the wrapper that lets a field be a compile-time constant *or* a runtime dynamic-shape affine expression. On the wire a `MaybeAffine` value is a scalar **or** an affine-expr object under the same key. The value lives at `field+0`; the active-tag byte sits at a class-specific offset (`field+0x20` within the 0x28-B slot, e.g. `IndirectLoad` value `+0x128` / flag `+0x148`). Reader → `MaybeAffine<T>::readFromJson` / `QuasiAffineExpr::createFromJson`; writer → `MaybeAffine<T>::writeIntoJson`. Full byte-layout is owned by [the value model page](./value-model.md).

### The two-VA-frame artifact

Every (de)serializer symbol appears **twice** in the IDA sidecars: a real body in the `0x40xxxx`–`0x43xxxx` (or `0x2dxxxx`/`0x25xxxx`) text band, and a 5–6-byte alias/PLT thunk in the `0x17xxxx`–`0x18xxxx` band that tail-jumps to it (verified e.g. `InstActivation` body `0x417f00` vs thunk `0x1816f0`; `InstDMA::toJson` body `0x4363d0` vs thunk frame `0x17ffb0`). **Every address on this page is a real body.** Do not read a thunk frame as the function — it is the same code reached via a different VA frame.

### The factory stub

`createFromJson` is a 49-byte stub identical in shape for all ops:

```c
Inst<X>* Inst<X>::createFromJson(string& name, BasicBlock* bb, const json& j):   // 49 B
    inst = NamedObjectContainer<BasicBlock,Instruction>::insertElement<Inst<X>>(bb, name);
    Inst<X>::readFieldsFromJson(*inst, j);     // the op-specific keys
    return inst;
```

Dispatch is purely static per opcode: a subclass's `createFromJson` calls its **own** `readFieldsFromJson`, so there is no virtual fan-out inside the factory. The opcode→`createFromJson` routing is done once by `createFromJsonHelper` keyed on the `opcode` string (`InstructionType2string`, `0x2d5bf0`).

---

## Matmul / MX family (6 ops)

`InstMatmultBase` is an abstract base that owns the entire matmul/MX op-specific payload in one shared 13-key body. `Matmult` and `MatmultMx` are pure JMP thunks to it (zero own keys); `MatmultSparse` adds exactly one key; `QuantizeMx` and `ReadActivationAccumulator` carry zero op-specific keys.

```text
Instruction
 └─ InstMatmultBase   (abstract; the shared 13-key reader/writer)
     ├─ InstMatmult         (no own keys; readFields/toJson = 5-B jmp → base)
     ├─ InstMatmultMx       (no own keys; readFields/toJson = 5-B jmp → base)
     └─ InstMatmultSparse   (base keys + compress_ratio)
 ├─ InstQuantizeMx                 (empty readFields; header+operands only)
 └─ InstReadActivationAccumulator  (empty readFields; header+operands only)
```

### InstMatmultBase — the shared 13-key body

`readFieldsFromJson` `0x41f600` (4373 B) / `toJson` `0x4358b0` (1359 B). Verified: the writer references exactly these 13 key literals and no others. Offsets are from the `Instruction` `this`. `MaybeAffine<T>` codes: `h`=u8, `b`=bool, `i`=i32, `j`=u32.

| Key | Offset | Type | Default | Conf |
|---|---|---|---|---|
| `accumulation_flag` | `+0x0F0` | MaybeAffine\<u8\> | ctor | CERTAIN |
| `psum_zero_region` | `+0x118` | MaybeAffine\<u8\> | ctor | CERTAIN |
| `replication_resolution` | `+0x140` | MaybeAffine\<u32\> | ctor | CERTAIN |
| `replication_shift_amnt` | `+0x168` | MaybeAffine\<u32\> | ctor | CERTAIN |
| `replication_num_rows` | `+0x190` | MaybeAffine\<u32\> | ctor | CERTAIN |
| `is_transpose` | `+0x1B8` | MaybeAffine\<bool\> | ctor | CERTAIN |
| `is_fmap_onezero` | `+0x1E0` | MaybeAffine\<bool\> | ctor | CERTAIN |
| `is_weight_onezero` | `+0x208` | MaybeAffine\<bool\> | ctor | CERTAIN |
| `tile_size` | `+0x230,+0x258` | MaybeAffine\<int\>[2] (JSON array `[0]`/`[1]`) | ctor | CERTAIN |
| `tile_position` | `+0x280,+0x2A8` | MaybeAffine\<int\>[2] (JSON array `[0]`/`[1]`) | ctor | CERTAIN |
| `perf_mode` | `+0x2D0` | `MatmultPerfMode` (enum) | `0` None | CERTAIN |
| `ifmap_quant_offset` | `+0x2D8` | MaybeAffine\<u8\> | ctor | CERTAIN |
| `weights_quant_offset` | `+0x300` | MaybeAffine\<u8\> | ctor | CERTAIN |

The 13 fields lie on a regular 0x28-byte stride from `+0xF0` (counting `tile_size`/`tile_position` as two slots each = 13 `MaybeAffine` slots), then `perf_mode` (4-B enum + 4-B pad) at `+0x2D0`, then the two quant offsets; the base-specific region ends at `+0x328`. `perf_mode` → `MatmultPerfMode {0 None, 1 DoubleRow, 2 DoubleColumn, 3 DoublePixel, 4 DoubleRowSwInterleave}`; ctor default `0`/None is set by `0x402fa4` (`mov dword[rbx+2D0h], 0`).

> **QUIRK —** `MaybeAffine` fields are **always emitted** in the matmul body (no skip-default gate on `toJson` `0x4358b0`), so all 13 base keys are present in any valid Matmult record. This differs from the tensor-op family, whose `MaybeAffine` keys *are* skip-default. The catalog's default column is the ctor value, not "absent".

### The other five

| Op (IT) | readFields | toJson | Op-specific keys |
|---|---|---|---|
| `InstMatmult` (8) | `0x420720` (5-B jmp→base) | `0x435e00` (5-B jmp→base) | — exactly the 13 base keys |
| `InstMatmultMx` (95) | `0x4208f0` (5-B jmp→base) | `0x435e50` (5-B jmp→base) | — exactly the 13 base keys |
| `InstMatmultSparse` (9) | `0x420730` (439 B; base + 1) | `0x435e10` (53 B; base + 1) | `compress_ratio` |
| `InstQuantizeMx` (96) | `0x40d090` (1-B `ret`) | (inherit) | **none** |
| `InstReadActivationAccumulator` (5) | `0x402da0` (1-B `ret`) | (inherit) | **none** |

`MatmultSparse`'s one extra key: `compress_ratio` → `std::variant<int, QuasiAffineExpr>` (payload `+0x328`, variant tag `+0x348`; tag `0`=int arm, `1`=affine arm). The remaining schema = the 13 base keys.

> **GOTCHA —** the "MX-ness" of `MatmultMx`/`QuantizeMx` (FP8/E8M0 packed weights, block scaling) is **not** in any JSON scalar field. It is carried by (a) the operand tensors' `dtype` (the FP4/FP8/E8M0 `Dtype` ordinals — see [the dtype tables](./dtype-tables.md)) and (b) an extra scale-tensor operand in `ins`. The MX `block_size`=32 is an HLO `backend_config` baked at codegen — it is **absent from BIR-JSON**. A reimplementer searching for a `block_size`/`scale`/`scale_method` key will find none; they do not exist in `libBIR`.

> **NOTE —** `quant_kernel` is not a matmul/MX field despite the name. It belongs to `InstBIRKernel` (`readFields` `0x433d40`, key at `+0x250`; see [§ Kernel family](#kernel--custom-op-family)).

---

## Activation / reduce / pool family (16 ops)

A mixed family: a handful of rich ops (`Activation`, `TensorReduce`, `Pool`, `RangeSelect`, `MatchReplace`) and many header-only ops. The wire key `func` is **overloaded**: on `Activation` it is `ActivationFunctionType` at `+0x120`; on `Pool` it is `PoolFunctionType` at `+0xF0` — same string, different enum and offset, different struct.

### InstActivation (IT 4) — 11 keys

`readFields` `0x417f00` / `toJson` `0x435450`. Engine = Activation ("Scalar"). Verified: the writer references exactly these 11 keys (`op0`/`op1` are the `aCompareOp0+8`/`aCompareOp1+8` rodata substrings — confirmed at the `lea` sites `0x43571f`/`0x4356df`). This op's 11-key count is the reference shared with [the cross-language parity page](./cross-language-wirekeys.md).

| Key | Offset | Type | Default | Req | Conf |
|---|---|---|---|---|---|
| `scale` | `+0x0F0` | float | — | req | CERTAIN |
| `alpha` | `+0x0F4` | float | — | req | CERTAIN |
| `can_read_uninit` | `+0x0F8` | MaybeAffine\<bool\> | unset | opt | CERTAIN |
| `func` | `+0x120` | `ActivationFunctionType` (31 vals) | Identity(0) | req | CERTAIN |
| `acc` | `+0x124` | `EngineAccumulationType` | Idle(0) | req | CERTAIN |
| `is_activate2` | `+0x128` | MaybeAffine\<bool\> | unset | opt | CERTAIN |
| `op0` | `+0x150` | `AluOpType` | bypass(0) | req | CERTAIN |
| `op1` | `+0x154` | `AluOpType` | bypass(0) | req | CERTAIN |
| `reverse0` | `+0x158` | MaybeAffine\<bool\> | unset | opt | CERTAIN |
| `reverse1` | `+0x180` | MaybeAffine\<bool\> | unset | opt | CERTAIN |
| `reduce_op` | `+0x1A8` | `AluOpType` | bypass(0) | req | CERTAIN |

### The rest of the family

| Op (IT) | readFields / toJson | Op-specific keys (offset · type · default) |
|---|---|---|
| `InstTensorReduce` (27) | `0x41a7c0` / `0x4379f0` | `op` `+0xF0` AluOpType bypass(0) req · `axis` `+0xF4` `AxisListType` X(0) req · `apply_transpose` `+0xF8` MA\<bool\> opt · `negate` `+0x120` MA\<bool\> opt |
| `InstPool` (20) | `0x41adf0` / `0x4375b0` | `func` `+0xF0` `PoolFunctionType {Max0,Avg1}` Max(0) req |
| `InstExponential` (103) | `0x417b80` / `0x43dcf0` | `reduce_cmd` `+0xF0` `EngineAccumulationType` Idle(0) req |
| `InstRangeSelect` (63) | `0x419b00` / `0x43bef0` | `base` `+0xF0` MA\<bool\> opt · `compare_op0` `+0x118` AluOpType bypass(0) · `compare_op1` `+0x11C` AluOpType bypass(0) · `reduce_op` `+0x120` AluOpType bypass(0) · `reduce_cmd` `+0x124` `EngineAccumulationType` Idle(0) · `fill_value` `+0x128` SerializableFloat · `num_active_channels` `+0x130` MA\<int\> opt |
| `InstMatchReplace` (90) | `0x415220` / `0x43d500` | `imm_value` `+0xF0` SerializableFloat req |
| `InstMaxIndexAndMatchReplace` (91) | `0x415250` (jmp→MatchReplace) / `0x43d6d0` (jmp→MatchReplace) | `imm_value` `+0xF0` SerializableFloat (inherited) |
| `InstLoadActFuncSet` (6) | `0x4155c0` / `0x435850` | `act_func_set_id` `+0xF0` MA\<int\> opt |
| `InstDveReadAccumulator` (102) | `0x415410` / `0x43dbf0` | `negated` `+0xF0` MaybeAffine\<u8\>(=bool) opt |
| `InstTongaReduceMacroSymbolic` (109) | `createFromJson` `0x2d08b0` / `0x2d15d0` | `op` `+0xF0` AluOpType bypass(0) · `is_first_reduce` bool (HIGH) · `reduce_axes` array `vector<axis>` · `debug` OpDebugInfo |
| `InstActivationReadAccumulator` (101) | `0x40d340` (`ret`) | **none** |
| `InstReciprocal` (21) | `0x405e10` (`ret`) | **none** |
| `InstGenericRelu` (2) | (no real body; PLT thunk `0x1756a0`) | **none** (header-only) |
| `InstMax` (88) | `0x40c880` (`ret`) | **none** |
| `InstMaxIndex` (89) | `0x40c970` (`ret`) | **none** |
| `InstNonzeroWithCount` (104) | `0x40d700` (`ret`) | **none** |

> **GOTCHA —** the wire key `reduce_cmd` deserializes to `bir::EngineAccumulationType` (`{0 Idle, 1 Zero, 2 Accumulate, 3 ZeroAccumulate, 4 AddAccumulate, 5 LoadAccumulate}`), **not** `bir::ReduceCmdType`. `ReduceCmdType` is dormant — no JSON consumer references it. `InstTensorReduce` has *no* `reduce_cmd` key at all; its reduce selector is the single enum `axis` (`AxisListType`, value+1 = innermost dims collapsed). See [the AluOpType + mode-enum page](./aluop-modes.md).

> **NOTE —** `reduce_axes` (Tonga, a JSON **array** of axis indices) and `axis` (TensorReduce, an `AxisListType` **enum**) are two distinct keys with different wire shapes. Do not conflate them.

---

## Copy / tensor-scalar / tensor-tensor family (18 ops)

The common base object is 240 bytes (`0xF0`); every subclass's first own field is at `+0xF0`. The tensor-scalar group shares a *layout* (op0 `+0xF0`, reverse0 `+0xF8`, op1 `+0x120`, reverse1 `+0x128`) but each op re-implements its own reader — layout-inheritance by convention, not a shared reader function.

### Copy ops

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstTensorCopy` (23) | `0x416400` / `0x437780` | `can_read_uninit` `+0xF0` MA\<bool\> opt |
| `InstGenericCopy` (1) | `0x4160a0` / `0x435260` | `can_read_uninit` `+0xF0` MA\<bool\> opt |
| `InstAbstractCopy` (3) | `0x416b30` / `0x4352d0` | `dma_qos` `+0xF0` `DMAQoSClass` Unassigned(0) opt |
| `InstTensorCopyDynamicSrc` (24) | `0x41e8a0` / `0x4377e0` | `scale` `+0xF0` MA\<uint\> opt · `can_read_uninit` `+0x118` MA\<bool\> opt |
| `InstTensorCopyDynamicDst` (25) | `0x41ec00` (jmp→Src) / `0x437870` | `scale` `+0xF0`, `can_read_uninit` `+0x118` (byte-identical to Src) |
| `InstIndirectCopy` (26) | `0x415260` / `0x437900` | `num_valid_indices` `+0xF0` MaybeAffine\<u16\> (=`variant<u16,QuasiAffineExpr>`) **req** |
| `InstCopyPredicated` (52) | `0x419470` / `0x439a00` | `can_read_uninit` `+0xF0` MA\<bool\> opt · `reverse_pred` `+0x118` MA\<bool\> opt · `reduce_op` `+0x140` AluOpType bypass(0) · `accumulator_cmd` `+0x144` `EngineAccumulationType` Idle(0) |

> **NOTE —** `InstAbstractCopy` (IT 3) appears in this family *and* in the DMA family because it is a copy-like op whose only key is `dma_qos`. Its parent is `InstEventSemaphore`, **not** `InstDMA` — see [§ DMA family](#dma-family-three-class-trees). Its `dma_qos` at `+0xF0` is in the `InstGeneric` tree, offset-coincident-but-semantically-distinct from `InstDMA+0xF0` (a `DMAQueue*` pointer).

### Tensor-scalar group

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstTensorScalar` (28) | `0x417030` / `0x437d80` | `op0` `+0xF0` AluOpType req · `const0` `+0xF4` SerFloat req · `reverse0` `+0xF8` MA\<bool\> req · `op1` `+0x120` AluOpType req · `const1` `+0x124` SerFloat req · `reverse1` `+0x128` MA\<bool\> req |
| `InstTensorScalarCache` (30) | `0x41b270` / `0x4384d0` | `op0` `+0xF0` AluOpType req · `reverse0` `+0xF8` MA\<bool\> opt · `op1` `+0x120` AluOpType req · `reverse1` `+0x128` MA\<bool\> opt · `mode` `+0x150` `TSCMode` Reduce(0) opt · `acc` `+0x154` `EngineAccumulationType` Idle(0) opt |
| `InstTensorScalarPtr` (29) | `0x418b80` / `0x438120` | `op0` `+0xF0` · `reverse0` `+0xF8` · `op1` `+0x120` · `reverse1` `+0x128` (all req); `apply_transpose` `+0x150` · `is_tensor_scalar_addr` `+0x178` · `is_scalar_tensor_tensor` `+0x1A0` · `is_tensor_tensor_scan` `+0x1C8` · `negate_second_output` `+0x1F8` (MA\<bool\> opt) · `acc` `+0x1F0` `EngineAccumulationType` opt |
| `InstTensorScalarAffineSelect` (62) | `0x421020` / `0x43bb80` | `base` `+0xF0` MA\<int\> req · `pattern` `+0x118` `SmallVector<APPair,4>` req · `channel_multiplier` `+0x168` MA\<int\> opt · `compare_op` `+0x190` AluOpType req · `fill_value` `+0x194` SerFloat opt |

> **QUIRK —** `InstTensorScalarPtr` (the "Ptr" variant) carries **no** `const0`/`const1` — the scalar is a pointer/tensor operand, not an immediate. Its `is_tensor_scalar_addr` / `is_scalar_tensor_tensor` / `is_tensor_tensor_scan` flag triple selects which datapath variant a single op maps to.

### Tensor-tensor / select / address-gen / fill / shuffle

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstTensorTensor` (31) | `0x417d30` / `0x438880` | `op` `+0xF0` AluOpType req · `acc` `+0xF4` `EngineAccumulationType` Idle(0) opt |
| `InstSelect` (51) | `0x409a70` (empty) / (inherit) | **none** (on_true/on_false/preds/dst are base operands) |
| `InstIota` (61) | `0x420ca0` / `0x43b8c0` | `base` `+0xF0` MA\<int\> req · `pattern` `+0x118` `SmallVector<APPair,4>` req · `channel_multiplier` `+0x168` MA\<int\> opt |
| `InstMemset` (10) | `0x41e840` / `0x435e60` | `mode` `+0xF0` `MemsetMode {Const0,Random1}` req · `constant` `+0xF8` MA\<uint\> req |
| `InstDropout` (65) | `0x416760` / `0x43c2a0` | `is_keep_threshold` `+0xF0` MA\<bool\> opt |
| `InstStreamShuffle` (39) | `0x415920` / `0x438c70` | `mask` `+0xF0` `SmallVector<MaybeAffine<uint>>` (array) req |
| `InstStreamTranspose` (40) | `0x408500` (empty) / (inherit) | **none** (transpose carried by base AP) |
| `InstTensorLoad` (75) | `0x40c160` (`ret`) | **none** (identity/operand-only) |
| `InstTensorSave` (76) | `0x40c190` (`ret`) | **none** (identity/operand-only) |

> **NOTE —** `Iota` and `TensorScalarAffineSelect` share the `{base, pattern, channel_multiplier}` affine-address-generator sub-layout at identical offsets (`+0xF0`/`+0x118`/`+0x168`). The Memset fill key is `constant` (MA\<uint\>), **not** `fill_value` — `fill_value` is the `TensorScalarAffineSelect` SerFloat at `+0x194`. The seed helper keys `predicates`/`value_array`/`value_list`/`update_mode` are base-header / sub-object helper keys, not members of any of these op records.

---

## DMA family — three class trees

The decisive structural result: the "DMA family" is **not** one inheritance tree. Three independent roots descend from `Instruction`, and they do **not** share the DMA base fields. This is verified at the binary: `InstDMA::toJson` (`0x4363d0`) emits `queue` and chains `Instruction::toJson`; `InstDMADescriptor::toJson` (`0x43c3e0`) emits `num_tiling_dimensions` and **also chains `Instruction::toJson` directly** — proving the descriptor subtree is *not* under `InstDMA`.

```text
TREE 1  Instruction → InstDMA → { DMACopy, Load(→Save), DMATrigger, TensorLoad, TensorSave }
         InstDMA owns the shared DMA fields: queue / dge_type / duplicate.

TREE 2  Instruction → InstDMADescriptor → { Copy, CCE, Transpose, Replicate }
         InstDMADescriptor ctor calls Instruction DIRECTLY (NOT InstDMA).
         No dge_type/queue/dma_qos — only num_tiling_dimensions.

TREE 3  Instruction → InstGeneric → InstEventSemaphore → InstAbstractCopy
         AbstractCopy (IT3): copy-like Generic op, NOT an InstDMA. Only key = dma_qos.
```

### Tree 1 — InstDMA base and its subclasses

`InstDMA` base: `readFields` `0x4173c0` / `toJson` `0x4363d0` / ctor `0x405260`. The base object's own additions on top of the header are `queue`, `dge_type`, `duplicate`:

| Key | Offset | Type | Default | Conf |
|---|---|---|---|---|
| `queue` | `+0xF0` | `DMAQueue*` → name string | `0` (null) | HIGH |
| `dge_type` | `+0xF8` | `DGEType` (from_json) | `3` Unassigned | CERTAIN |
| `duplicate` | `+0x100` | MaybeAffine\<bool\> | `0` (none) | CERTAIN |

> **GOTCHA —** `InstDMA+0xF0` is a `DMAQueue*` **pointer**, not a scalar QoS source. `toJson` (`0x4363e5`) dereferences it and emits the queue *name* (the string at `queue+0x68`) under key `queue`. `readFields` reads only `dge_type` + `duplicate`; the `queue` field is written null and bound later by `createFromJsonRecursivelyPass2` (queue name → `DMAQueue*`; "Queue does not exist!" assert). The InstDMA scalar QoS lives on the **subclasses** (`Load`/`Save` `+0x150`, `DMACopy` `+0x1D8`, `AbstractCopy` `+0xF0`), per `getDMAQoSClass` (`0x30fc60`).

The DMACopy subclass starts its own block at `+0x128` (the InstDMA base block ends at the `+0x120` queue-present gate):

| Op (IT) | readFields / toJson | Op-specific keys (beyond inherited queue/dge_type/duplicate) |
|---|---|---|
| `InstDMACopy` (32) | `0x4311a0` / `0x4367e0` | `mode` `+0x128` `CopyMode` 0 · `oob_is_err` `+0x130` MA\<bool\> · `transpose_op` `+0x158` `TransposeOps` · `constants` `+0x160` `SmallVector<float,1>` · `cce_op` `+0x178` AluOpType · `replica_groups` `+0x180` `vector<vector<uint>>` · `remote_notification` `+0x198` `vector<uint>` · `can_read_uninit` `+0x1B0` MA\<bool\> · `dma_qos` `+0x1D8` `DMAQoSClass` 0 · `use_gpsimd_dma` `+0x1DC` bool |
| `InstLoad` (19) | `0x417720` / `0x436600` | `can_read_uninit` `+0x128` MA\<bool\> · `dma_qos` `+0x150` `DMAQoSClass` Unassigned(0) |
| `InstSave` (22) | `0x417a80` (jmp→Load) / `0x4366f0` | identical to Load: `dma_qos` `+0x150`, `can_read_uninit` `+0x128` |
| `InstDMATrigger` (67) | `0x417a90` (jmp→InstDMA) / `0x4372f0` | inherits queue/dge_type/duplicate; emits `dma_blocks` (block-list container) |
| `InstTensorLoad` (75)* | `0x40c160` (`ret`) | **none** |
| `InstTensorSave` (76)* | `0x40c190` (`ret`) | **none** |

\* TensorLoad/TensorSave are catalogued under both the copy family (their natural home) and Tree 1 by name/RTTI; they carry zero DMA additions.

### Tree 2 — InstDMADescriptor and its subclasses

`InstDMADescriptor` base: `readFields` `0x41f0c0` / `toJson` `0x43c3e0` / ctor `0x40b7e0` (calls `Instruction` directly). Sole base key:

| Key | Offset | Type | Default | Conf |
|---|---|---|---|---|
| `num_tiling_dimensions` | `+0xF0` | MaybeAffine\<uint\> | `0` (none) | CERTAIN |

| Op (IT) | readFields / toJson | Op-specific keys (beyond num_tiling_dimensions) |
|---|---|---|
| `InstDMADescriptorCopy` (69) | `0x41f270` / `0x43c440` | `can_read_uninit` `+0x118` MA\<bool\> |
| `InstDMADescriptorCCE` (70) | `0x420a50` / `0x43c4a0` | `constants` `+0x118` `SmallVector<float,1>` · `op` `+0x130` AluOpType (reduce op) |
| `InstDMADescriptorTranspose` (71) | `0x41f420` / `0x43c6c0` | `transpose_op` `+0x118` `TransposeOps` · `can_read_uninit` `+0x120` MA\<bool\> |
| `InstDMADescriptorReplicate` (72) | `0x41f5f0` (jmp→base) / `0x43c8d0` (jmp→base) | **none** — pure base record |

### Tree 3 + structured-control DMA

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstAbstractCopy` (3) | `0x416b30` / `0x4352d0` | `dma_qos` `+0xF0` `DMAQoSClass` Unassigned(0) (parent = `InstEventSemaphore`) |
| `InstDMABlock` (107) | (no flat reader) `createFromJson` `0x252300` / `toJson` `0x251c40` | `dma_trigger` (sub-block) · `block_id` uint · `section_start` bool · `priority_class` · `sync_info` · `remote_notifications` (plural) · `on_wait` · `on_update` |

### Where each DMA-ish key actually lives

The DMA-flavoured key names are spread across four different record levels, and only one of them is an `InstDMA` field:

| Key | Owner |
|---|---|
| `dge_type` | `InstDMA` base (the only DMA-wide instruction field) |
| `dma_qos` | per-subclass (`Load`/`Save` `+0x150`, `DMACopy` `+0x1D8`, `AbstractCopy` `+0xF0`) |
| `use_gpsimd_dma` | `InstDMACopy` `+0x1DC` |
| `dma_blocks` | BasicBlock level, resolved by `createFromJsonRecursivelyPass2` |
| `dma_trigger` | an `InstDMABlock` sub-block |
| `num_dma_engines` | the `DMAQueue` record (`DMAQueue::toJson` `0x257160`) |
| `eng_dma` | `ModuleArtifactInfo` |

The last four are not instruction fields at all, so a per-op reader that tries to parse them will never see them.

> **NOTE —** `InstDMACopy` uses `remote_notification` (singular); `InstDMABlock` uses `remote_notifications` (plural). Two distinct keys — do not conflate.

---

## Collective / BatchNorm / RNG family (18 ops)

Three structural classes here. **Rich** ops parse named keys; **degenerate** ops have a bare-`ret` reader and no `toJson` override (header-only); and `InstCollective` (IT 47) is an **abstract base** whose `readFields` is `ret` but whose `toJson` (`0x438ea0`) emits one inherited collective key `queue` that Send/Recv/Compute all chain.

### Collective ops

`InstCollectiveCompute` (IT 48) multiplexes all 11 `CollectiveKind`s; it is the only collective carrying `cc_dim`/`cc_type_hint`. `readFields` `0x432110` (6466 B) / `toJson` `0x438fe0`.

| Key | Offset | Type | Default | Conf |
|---|---|---|---|---|
| `cc_channel` | `+0x118` (sub-obj) | nested `{kind, replica_groups, op}` | — | CERTAIN |
| ` ├ kind` | `+0xF8` | `CollectiveKind` | AllReduce(2) | CERTAIN |
| ` ├ replica_groups` | `+0x100/+0x108` | `vector<vector<u32>>` | empty | CERTAIN |
| ` └ op` | `+0x180` | `AluOpType` (reduce op) | bypass/unused | CERTAIN |
| `cc_dim` | `+0x27C` | `CollectiveDimension` | Free(1) (suppressed) | CERTAIN |
| `cc_type_hint` | `+0x244` | `CollectiveComputeTypeHint` | None(2) (suppressed) | CERTAIN |
| `stream_id` | `+0x140` | MaybeAffine (scalar) | optional | HIGH |
| `src_target_pairs` | `+0x168/+0x170` | vector (pair list) | empty | CERTAIN |
| `can_read_uninit` | `+0x188` (tag `+0x1A8`) | MaybeAffine\<bool\> | false | CERTAIN |
| `is_local` | `+0x1B0` (tag `+0x1D0`) | MaybeAffine\<bool\> | false | CERTAIN |
| `pipe_id` / `recv_from_rank` / `send_to_rank` | `+0x1D8`/`+0x1E0`/`+0x1E8` | scalar | — | HIGH |
| `initial_corebarrier` | `+0x218` (flag `+0x238`) | flag-gated | optional | HIGH |
| `permute_chain` | `+0x250` (flag `+0x270`) | vector | empty | HIGH |
| `src1_bitmap` | `+0x280` (flag `+0x2A0`) | vector/bitmap | empty | HIGH |
| `unique_tensors` / `use_gpsimd_dma` / `dma_qos` | (disc/bool/enum) | — | — | MED |

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstCollective` (47) | `0x409100` (`ret`) / `0x438ea0` | `queue` `+0xF0` `QuasiAffineExpr` — **write-only** (no readFields parses it; set at codegen). Inherited by Compute/Send/Recv toJson. |
| `InstCollectiveSend` (49) | `0x421560` / `0x4397d0` | `peer_id` `+0xF8` `QuasiAffineExpr` **req** · `can_read_uninit` `+0x120` MA\<bool\> opt |
| `InstCollectiveRecv` (50) | `0x4218a0` / `0x439910` | `peer_id` `+0xF8` `variant<u64,QuasiAffineExpr>` **req** (no can_read_uninit) |
| `InstGetGlobalRankId` (11) | `0x420900` / `0x436050` | `world_size` `+0xF0` MaybeAffine\<uint\> (tag `+0x110`) **req** |
| `InstGetCurProcessingRankID` (66) | `0x431d30` / `0x43c300` | `iter_id` `+0xF0` MA\<uint\> req · `channel_id` `+0x118` MA\<uint\> req · `stream_id` `+0x140` MA\<int\> opt · `replica_groups` `+0x168` `vector<vector<u32>>` req |

> **NOTE —** the literal top-level `CollectiveCompute` key set is exactly the table above plus the inherited `queue`. The offset-bearing keys are `recv_from_rank` / `send_to_rank` / `src_target_pairs`; there is no `neff_collective_has_offset` key in `libBIR`.

### BatchNorm ops

Only three BN JSON fields exist across the whole family; `BNStatsAggregate` and `BNGradients` are header-only (operand-only) records.

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstBNStats` (34) | `0x4165b0` / `0x438ad0` | `apply_transpose` `+0xF0` (tag `+0x110`) MA\<bool\> false req |
| `InstBNBackprop` (37) | `0x416040` / `0x438b30` | `total_elements` `+0xF0` float req |
| `InstBNBackprop2` (38) | `0x416070` / `0x438bd0` | `total_elements` `+0xF0` float req |
| `InstBNStatsAggregate` (35) | `0x407e00` (`ret`) | **none** (operands only) |
| `InstBNGradients` (36) | `0x407f70` (`ret`) | **none** (operands only) |

### RNG ops

Only `Rand` and `SetRandState` carry keys; the other five RNG opcodes are header-only.

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstRand` (60) | `0x420ab0` / `0x43b5a0` | `random_algorithm` `+0xF0` `RandomAlgorithmKind {LFSR0,PCG32_1,PHILOX_2}` req · `rand_num_steps` `+0xF8` (tag `+0x118`) `variant<u8,QuasiAffineExpr>` req · `distribution` `+0x120` `RandomDistributionKind {Raw0,Uniform1,Normal2,Binomial3}` req · `params` `+0x128` `SmallVector<float,4>` req |
| `InstSetRandState` (59) | `0x4151f0` / `0x43b3d0` | `rng_engine` `+0xF0` `bir::EngineType` req |
| `InstRand2` (97) | `0x40d180` (`ret`) | **none** |
| `InstRng` (100) | `0x40d2d0` (`ret`) | **none** |
| `InstGetRandState` (58) | `0x40a830` (`ret`) | **none** |
| `InstRandGetState` (99) | `0x40d260` (`ret`) | **none** |
| `InstRandSetState` (98) | `0x40d1f0` (`ret`) | **none** |

> **NOTE —** `SetRandState`'s `rng_engine` is `bir::EngineType` (`{0 Unassigned, 1 Pool, 2 Activation, 3 PE, 4 DMA, 5 DVE, 6 SP, 7 ALL}`) — it names the engine whose RNG state is restored — **not** a Random enum. The Gen-2 (V4) RNG ops `Rand2`/`RandGetState`/`RandSetState` carry no keys; only the Gen-1 `Rand` and `SetRandState` are keyed.

---

## Indirect / gather family (8 ops)

The fine-grained indirect access-pattern descriptors are **operand-object** keys, nested under the common-header `ins`/`indirection_ins` arrays — **not** per-op top-level keys.

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstIndirectLoad` (43) | `0x41ec10` / `0x436e30` | `runtime_semaphore_wait_value` `+0x128` MaybeAffine\<u32\> (flag `+0x148`) ctor 0 |
| `InstIndirectSave` (45) | `0x41eec0` (jmp→Load) / `0x4370c0` | `runtime_semaphore_wait_value` `+0x128` (inherited, identical) |
| `InstIndirectSaveAccumulate` (46) | `0x41eed0` / `0x437120` | `num_indices` `+0x128` MA\<u32\> · `entry_step_elements` `+0x150` MA\<u32\> · `op` `+0x178` AluOpType |
| `InstIndirectCopy` (26) | `0x415260` / `0x437900` | `num_valid_indices` `+0xF0` `QuasiAffineExpr` (no InstDMA base) |
| `InstGenericIndirectLoad` (42) | `0x4309f0` / `0x436c90` | base = InstDMA; then `indirect_dims` `+0x128` `vector<IndirectDim>` empty |
| `InstGenericIndirectSave` (44) | `0x430be0` / `0x436e90` | `indirect_dims` `+0x128` `vector<IndirectDim>` · `op` `+0x140` AluOpType |
| `InstGather` (92) | `0x40cca0` (`ret`) | **none** |
| `InstReadVarAddr` (41) | `0x417710` (jmp→InstDMA) / `0x436c80` (jmp→InstDMA) | **none** (DMA base only) |

`IndirectDim` wire enum: `{W=0, Z1, Y2, X3}` (`IndirectDim2string` `0x4021a0`).

The fine-grained indirect keys — `indirect_dim`, `indirect_dim_max_index`, `num_tensor_indirect_indices`, `offset_expr`, `tensor_indirect_arg_id`, `reg_ap_offset`, `num_indirect_args` — all live one level down, on the **operand objects** nested under `ins`/`indirection_ins`. They are owned by `bir::DynamicAPINFO::toJson` (`0x268c00`), `bir::SymbolicAccessPattern::toJson`, `bir::Argument::createFromJson`, and `getIndirectArgumentById`.

> **GOTCHA —** the only per-op *top-level* indirect key in the whole catalog is `indirect_dims`, on `GenericIndirectLoad`/`GenericIndirectSave`. Everything else indirect is an operand key. (`indirect_loadsave`, `is_regloc_offset`, and `const_ap_offset` are not keys anywhere in `libBIR`.)

---

## Control-flow family (13 ops)

The control hierarchy is recovered from the `toJson` tail-call chains. `InstEventSemaphore` (IT 13) is the concrete base whose `toJson` (a thunk → `Instruction::toJson`) the five structural terminators inherit.

```text
Instruction
 └─ InstEventSemaphore           (toJson = thunk → Instruction::toJson; NO own keys)
      ├─ Return / Exit / Break    (thunk → EventSemaphore::toJson; NO keys)
      ├─ UnconditionalBranch      (+ target)
      ├─ CompareAndBranch         (+ comp_op, on_true, on_false)
      └─ BranchHint               (+ hint, associate_branch)   [excluded from the thunk chain]
```

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstCompareAndBranch` (78) | `0x416f80` / `0x434e40` | `comp_op` `+0xF0` `BranchCompareOp` · `on_true` `+0xF8` BasicBlock*→name · `on_false` `+0x100` BasicBlock*→name |
| `InstUnconditionalBranch` (79) | `0x40c250` (jmp→Term) / `0x4350f0` | `target` `+0xF0` BasicBlock*→name (base = EventSemaphore::toJson) |
| `InstBranchHint` (80) | `0x41af70` / `0x43caf0` | `hint` `+0xF0` `BranchOutcomeHint` · `associate_branch` `+0xF8` BasicBlock*→name |
| `InstCall` (84) | `0x415770` / `0x43cce0` | `table_id` `+0x120` MA\<i32\> (only readFields key); `target` `+0xF0` callee-name (toJson, HIGH) · `arguments` `+0xF8` `FunctionArgumentMap` (toJson, HIGH) |
| `InstLoop` (105) | (no readFields; parsed in `createFromJson`) / `0x323d90` | `isBasicBlock` · `isParallel` (byte) · `LoopAxis` `+0x150` (object) · `unroll` · `registers` `+0x168/+0x178` (vector) |
| `InstDynamicForLoop` (106) | (createFromJson) / `0x324190` | `name` (string) · `DynamicForLoopAxis` (object) · `lb` / `ub` / `stride` (bound exprs) |
| `InstDoWhile` (108) | (createFromJson) / `0x25e6e0` | `cond` (block/expr ref) |
| `InstTerminator` (77) | `0x40c1e0` (`ret`) | **none** (base) |
| `InstReturn` (81) | `0x40c2c0` (jmp→Term) / `0x435230` (thunk→EventSema) | **none** |
| `InstExit` (82) | `0x40c2f0` (jmp→Term) / `0x435240` (thunk→EventSema) | **none** |
| `InstBreak` (83) | `0x40c320` (jmp→Term) / `0x435250` (thunk→EventSema) | **none** |
| `InstNoOp` (12) | `0x405060` (`ret`) | **none** |
| `InstGeneric` (0) | (inherit) | **none** (base Generic) |

Three structural notes on this family. The loop bounds are spelled `lb`/`ub`/`stride` alongside `name` — there is no `loop_count`, `trip_count`, or `branch_target` key anywhere in `libBIR`. `BranchHint` (80) carries its own keys but sits **outside** the `EventSemaphore` thunk chain (consistent with `byte_7858CE[3]=0`), which is why it is drawn detached in the tree above. And the `Loop`/`DynamicForLoop`/`DoWhile` container fields are parsed inline inside `createFromJson` (`0x3243b0`/`0x325380`/`0x25e8e0`) rather than by a flat `readFieldsFromJson`, so their offsets here are read off `toJson` alone and carry HIGH rather than CERTAIN confidence.

---

## Queue / sync family (9 ops)

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstSwitchQueueInstance` (85) | `0x416910` / `0x43cf70` | `no_rearm` `+0x100` MA\<bool\> (only readFields key); `queue` `+0xF0`, `instance` `+0xF8` (toJson-emitted refs) |
| `InstResetQueueInstance` (86) | `0x40c5e0` (`ret`) / `0x43d170` | `queue` `+0xF0`, `instance` `+0xF8` (toJson-emitted; readFields is `ret`) |
| `InstGroupResetSemaphores` (14) | `0x4307e0` / `0x436090` | `mode` `+0xF0` `EventSemaphoreClearMode` · `sema_group` `+0xF8` vector |
| `InstDrain` (16) | `0x416250` / `0x436370` | `is_reset_sema` `+0xF0` MA\<bool\> |
| `InstCoreBarrier` (87) | `0x430f70` / `0x43d330` | `runtime_semaphore` `+0xF0` MA\<u32\> · `id` `+0x118` MA\<u32\> · `cores` `+0x140` vector |
| `InstEventSemaphore` (13) | `0x405090` (`ret`) / `0x434e30` (thunk→Instruction) | **none** (concrete Terminator base) |
| `InstAllEngineBarrier` (15) | `0x405100` (`ret`) | **none** |
| `InstHalt` (17) | `0x405250` (`ret`) | **none** |
| `InstGPSIMDSB2SB` (33) | `0x407bb0` (`ret`) | **none** (AP-legality is in `verify()` `0x294e00`, not JSON) |

`EventSemaphoreClearMode` is carried by exactly one op (`GroupResetSemaphores`).

> **GOTCHA —** `runtime_semaphore_wait_value` (an `Indirect{Load,Save}` key) and `runtime_semaphore` (the `CoreBarrier` key) are two different fields on two different ops. Also: `num_semaphores`, `user_event`, and `wait_mode` exist as rodata literals but are wired to no (de)serializer — the per-op sync key set is exactly the table above.

---

## Kernel / custom-op family (4 ops)

The kernel ops are the richest records in BIR-JSON. Non-`MaybeAffine` vector/string offsets are marked HIGH (their field↔key pairing is by `lea`/`operator[]` adjacency); the `MaybeAffine` members are CERTAIN.

| Op (IT) | readFields / toJson | Op-specific keys (key · ~offset · type) |
|---|---|---|
| `InstCustomOp` (53) | `0x433aa0` / `0x439b60` | `opFunctionName` `+0xF0` string · `opLibFile` `+0x110` string · `ulib_to_ucode_version` `+0x130` · `ulib_to_isa_version` `+0x150` · `is_builtin` `+0x170` MA\<bool\> · `srcsShape` `+0x198` vector (HIGH) · `dstsShape` `+0x1B0` vector (HIGH) |
| `InstBIRKernel` (54) | `0x433d40` / `0x439ed0` | `kernel_name` `+0xF0` string · `srcs_shape` `+0x110` · `dsts_shape` `+0x128` · `kernel_attrs` `+0x140` (HIGH) · `sb_buf_shape` `+0x150` · `psum_buf_shape` `+0x158` · `auto_cast` `+0x168` MA\<bool\> · `is_causal` `+0x188` MA\<bool\> · `fused_rmsnorm` `+0x1A8` MA\<bool\> · `norm_type` `+0x1D0` MA\<i32\> · `lnc_size` `+0x1F8` MA\<i32\> · `store_add` `+0x220` MA\<bool\> · `quant_kernel` `+0x250` MA\<bool\> · `output_layout` `+0x2A0` MA\<i32\> · `lower_bound` (last key, HIGH) |
| `InstNKIKernel` (55) | `0x430410` / `0x43a4f0` | `func` `+0xF8` `FunctionArgumentMap` · `func_args` `+0x120` `FunctionArgumentMap` · `func_outs` `+0x148` vector (HIGH) · `sb_buf_shape` `+0x150` · `psum_buf_shape` `+0x158` · `address_rotation_scope_val` `+0x15C` `AddressRotationScope` · `kernel_source_val` `+0x160` `KernelSource` |
| `InstNKIKLIRKernel` (56) | `0x434870` / `0x43a990` | `klir_binary` `+0xF0` string/blob · `kernel_format` `+0x110` string · `nki_binary_version_identifier` `+0x130` string · `enable_device_dump` `+0x150` MA\<bool\> · `func_args` `+0x1B0` (HIGH) · `func_outs` `+0x1DC` · `sb_buf_shape` `+0x1E0` · `psum_buf_shape` `+0x1EC` · `address_rotation_scope_val` (HIGH) |

> **GOTCHA —** the kernel ops use `opFunctionName`/`opLibFile` (CustomOp) and `func`/`klir_binary` (the NKI trio). There is no `function_name`, `lib_file_name`, `kernel_text`, or `opaque` key. Separately: `NKIKernel`, `NKIKLIRKernel`, and `Call` assert "Not Implemented" in `sameInst` (structural equality) only — their JSON (de)serializers are complete.

---

## Misc family (6 ops)

| Op (IT) | readFields / toJson | Op-specific keys |
|---|---|---|
| `InstDevicePrint` (57) | `0x41a240` / `0x43b2b0` | `prefix` `+0xF0` string · `output_buffer` `+0x110` `DebugDeviceBuffer` |
| `InstRegisterAlu` (73) | `0x417110` / `0x43c8e0` | `op` `+0xF0` AluOpType · `is_64bit` `+0xF8` MA\<bool\> |
| `InstInlineASMBytes` (93) | `0x425210` / `0x43d6e0` | `asm_bytes` `+0xF0` byte-vector · `sync_type` `+0x110` `InstSyncType` · `latency_estimate` `+0x118` `QuasiAffineExpr` |
| `InstTensorCompletion` (94) | `0x41abd0` / `0x43da60` | `mode` `+0xF0` `TensorCompletionMode` |
| `InstGetSequenceBounds` (64) | `0x40b2e0` (`ret`) | **none** |
| `InstRegisterMove` (74) | `0x40c130` (`ret`) | **none** |

---

## The 110-op coverage tally

Every `InstructionType` ordinal 0–109 is covered exactly once by one of the families above. The partition is cross-checked against the 110-case `InstructionType2string` (`0x2d5bf0`) — see [the 110-opcode enum](./instruction-type.md), which also reconciles the 104/105 "concrete-leaf" count denominators. The catalog has no gaps and no overlaps.

| Family | Count | IT ordinals |
|---|---|---|
| Matmul / MX | 6 | 5, 7, 8, 9, 95, 96 |
| Activation / reduce / pool | 16 | 2, 4, 6, 20, 21, 27, 63, 88, 89, 90, 91, 101, 102, 103, 104, 109 |
| Copy / tensor-scalar / tensor-tensor | 18 | 1, 3, 10, 23, 24, 25, 26, 28, 29, 30, 31, 39, 40, 51, 52, 61, 62, 65 |
| DMA (three trees) | 11 | 18, 19, 22, 32, 67, 68, 69, 70, 71, 72, 107 |
| Collective / BN / RNG | 18 | 11, 34, 35, 36, 37, 38, 47, 48, 49, 50, 58, 59, 60, 66, 97, 98, 99, 100 |
| Indirect / gather | 8 | 26, 41, 42, 43, 44, 45, 46, 92 |
| Control-flow | 13 | 0, 12, 77, 78, 79, 80, 81, 82, 83, 84, 105, 106, 108 |
| Queue / sync | 9 | 13, 14, 15, 16, 17, 33, 85, 86, 87 |
| Kernel / custom-op | 4 | 53, 54, 55, 56 |
| Misc | 6 | 57, 64, 73, 74, 93, 94 |

> **NOTE —** the family rows total more than 110 because a few ops are legitimately catalogued under two families (e.g. `AbstractCopy` IT 3 appears under both copy and DMA Tree 3; `IndirectCopy` IT 26 under both copy and indirect; `TensorLoad`/`TensorSave` IT 75/76 under both copy and DMA Tree 1; `DMA` IT 18 is the InstDMA abstract base). The **distinct** ordinal set is exactly 0–109, 110 ops, dense and gap-free, matching the `InstructionType` enum.

The dominant shape of the catalog: **a large fraction of ops carry zero op-specific keys**. Across the indirect/control/queue/sync/kernel/misc set alone, 10 ops have a `ret`-only reader (`NoOp`, `EventSemaphore`, `AllEngineBarrier`, `Halt`, `GPSIMDSB2SB`, `GetSequenceBounds`, `RegisterMove`, `Terminator`, `Gather`, `Generic`) and 6 are 5-byte JMP thunks to a base reader. A reimplementer should expect most records to be header-only and reserve the rich path for matmul, activation, the tensor-scalar group, the collectives, and the kernel ops.

---

## Evidence summary

The strongest claims on this page and their anchors in `libBIR.so` cp310:

- **`InstMatmultBase::toJson` (`0x4358b0`)** references exactly the 13 key literals tabulated and no more, by enumeration of its `lea …, a<Key>` sites. **CERTAIN.**
- **`InstActivation::toJson` (`0x435450`)** references exactly 11 keys; `op0`/`op1` resolve via the rodata substrings `aCompareOp0+8`/`aCompareOp1+8` at `0x43571f`/`0x4356df`. **CERTAIN.**
- **The DMA tree split**: `InstDMADescriptor::toJson` (`0x43c3e0`) chains `Instruction::toJson` and emits `num_tiling_dimensions`, while `InstDMA::toJson` (`0x4363d0`) emits `queue` and chains `Instruction::toJson` — the descriptor subtree is provably **not** under `InstDMA`. **CERTAIN.**
- **`InstAbstractCopy::readFieldsFromJson` (`0x416b30`)** reads `dma_qos` into `[r12+0F0h]` via `from_json<DMAQoSClass>` (jmp `0x416ca8`). **CERTAIN.**
- **The 110-op partition** is cross-checked against the 110-case `InstructionType2string` (`0x2d5bf0`), verified in [the opcode-enum page](./instruction-type.md). **CERTAIN.**

Ceiling on the rest: families fully covered at CERTAIN for their primary keys are matmul/MX, activation/reduce/pool, copy/tensor-scalar/tensor-tensor, the three DMA trees, BN, RNG, and the control-flow/queue/sync core. Sampled at **HIGH** (offset-precise but not every member byte-pinned): the deep `CollectiveCompute` field block below `cc_type_hint` (`pipe_id`/`recv_from_rank`/`permute_chain`/`src1_bitmap`/`unique_tensors`), the kernel-op non-`MaybeAffine` vector/string offsets (`srcsShape`/`sb_buf_shape`/`func_outs`/`kernel_attrs`/`lower_bound`), and the structured-control container offsets (`Loop`/`DynamicForLoop`/`DoWhile`, read from `toJson` only). The `MaybeAffine<T>` per-field ctor defaults (beyond `perf_mode`=None and the bool-false sentinels) are the uniform 0x28-B zero/identity init and are marked "ctor", not byte-traced per field. No NEFF/BIR-JSON fixture round-trip byte-diff was performed.

---

## Cross-References

- [The `bir::Instruction` Base Struct](./instruction-base.md) — the shared base header keys (`opcode`/`engine`/`ins`/`outs`/…) that every record carries; the single-spec `*OpGen` contract
- [InstructionType: the 110-Opcode Enum](./instruction-type.md) — the ordinal→name roster and the 104/105 concrete-leaf count reconciliation this catalog partitions
- [Cross-Language Wire-Key Consistency & the `*Gen` Bases](./cross-language-wirekeys.md) — the Python↔C++ parity proof for the same `InstActivation` (11) / `InstMatmultBase` (13) key sets; the instruction-count denominators
- [Argument / AccessPattern / Immediate / Register Value Model](./value-model.md) — the `MaybeAffine<T>` / `QuasiAffineExpr` / `SerializableFloat` / `APPair` wrappers and the operand-nested indirect-AP keys (`DynamicAPINFO`)
- [AluOpType + the Mode Enums & the L1≠L3 Comparison Reorder](./aluop-modes.md) — `AluOpType`, `TSCMode`, `MemsetMode`, and why `reduce_cmd` is `EngineAccumulationType` not `ReduceCmdType`
- [Engine / Arch / Accumulation / Axis / Indirect / Transpose Enums](./structural-enums.md) — `EngineType`, `AxisListType`, `IndirectDim`, `TransposeOps`, `EngineAccumulationType`
- [Op-Family Enums & the L1/L2/L3 Crosswalk](./op-family-enums.md) — `CollectiveKind`/`CollectiveDimension`, `MatmultPerfMode`, `PoolFunctionType`, the kernel/branch structural enums
- [Dtype Tables & Wire-Tag / Stride / Alignment LUTs](./dtype-tables.md) — the FP4/FP8/E8M0 `Dtype` ordinals that carry "MX-ness" in operand tensors, absent from the matmul scalar fields
- [JSON Writer — `toJson`, Skip-Default Emission & Always-v2](./json-writer.md) — the writer side: skip-default gates, ICF-merged bodies, the always-v2 schema version *(planned)*
- [JSON Loader — `createFromJson` & the Recursive Pass-2 Linker](./json-loader.md) — the reader side: the opcode→factory dispatch and the queue/successor resolution this catalog defers to *(planned)*
