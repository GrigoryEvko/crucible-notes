# Penguin Target Abstraction

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp310/11/12 are byte-identical for these modules). The target layer lives entirely in Cython `.so` files under `neuronxcc/starfish/penguin/targets/`; several retain DWARF `debug_info` and a non-stripped symtab, so Python attribute names, source-line numbers, and docstrings are recoverable. The cost-model twin lives in `xla::hilo::*` inside the `hlo2penguin` ELF. Treat every address as version-pinned.*

## Abstract

Penguin's middle-end is written once and specialized at run time by a single object: the **target instance**. There is no `#ifdef SUNDA` and no per-generation copy of the codegen loop. Instead `targets/Target.py::parse_target` resolves an arch string to one of four Python classes — `Tonga`, `Sunda`, `Cayman`, `CoreV4` — arranged in a strict linear inheritance chain, and that instance is threaded through every pass, the cost model, and the BIR-emit loop as the one knob that says "which generation". The class you get back *is* the capability surface: which ISA ops can be emitted, which matmul operand dtypes are legal, which DMA-descriptor offload paths exist, and which latency model drives tiling.

This is the same design the C++ backend uses one level down — `CoreV2Gen → CoreV3Gen → CoreV4Gen` selective-override hierarchy in `libwalrus` (see [Part 8](../walrus/)) — and the two meet at the BIR-JSON handoff. The Python target decides *policy* (legal op set, legal dtype set, feature flags, cost model, which emit path BirCodeGenLoop takes); the C++ `CoreV*GenImpl` does the final 64-byte-bundle *encoding*, selected by the `ArchLevel` ordinal that the Python `_hierarchy_version` stamps into the BIR-JSON. A reimplementer who builds the Python layer right hands the backend exactly the right op variants; one who gets it wrong emits ops the backend has no encoder for.

Three things make this layer easy to misread, and each gets a callout below: the directory `tonga/` names the **abstract base class** as well as the gen1 Inferentia device (it is not a fifth silicon generation); `CoreV4` is **conditionally imported** behind a `try/except`, so a build without it must still parse `Sunda`/`Cayman`; and there are **three different numeric "generation" axes** in play — the Python `_hierarchy_version` `{1,2,3,4}`, the libBIR `ArchLevel` `{10,20,30,40}`, and the C++ `hilo::` cost-model enum `{0,1,2,3}` — which must not be conflated.

For reimplementation, the contract is:

- **Selection** — `parse_target(str, …) → instance` alias dispatch and its inverse `get_platform_target(instance) → {inf1,trn1,trn2,trn3}`, including the conditional-`CoreV4` guard.
- **The hierarchy** — `Tonga ← Sunda ← Cayman ← CoreV4` as a linear chain keyed by `@register_target(version=N)`, orderable so feature predicates can phrase `self.target < Cayman`.
- **The ISA / Gen / ISAInst three-layer stack** — the generated `<Op>Gen` encoder bases, the abstract `Inst` bases, and the per-target `*ISAInst` op classes whose mere presence is the coarse capability gate.
- **The capability gates** — ISA-class presence, matmul operand-dtype legality, the DGE (Descriptor-Generation-Engine) flag cluster, and the `max_*` sizing config.
- **Parameterization** — how the resolved target drives `BirCodeGenLoop`, `NeuronCodegen`/`KernelBuilder`, the cost model, and the `CodeGenFlow` pass roster.

| | |
|---|---|
| **Selection entry** | `Target.py::parse_target` @ `0x9720` (pf), `0xc1a0` (pw); py 47–76 |
| **Inverse** | `Target.py::get_platform_target` @ `0x8570`; py 99–131 |
| **Class chain** | `Tonga ← Sunda ← Cayman ← CoreV4` (linear; `tonga/Tonga.py`, `sunda/Sunda.py`, `cayman/Cayman.py`, `core_v4/CoreV4.py`) |
| **Version backbone** | `@register_target(version=N)` → `_hierarchy_version` ∈ {Tonga:1, Sunda:2, Cayman:3, CoreV4:4} |
| **Base ISA** | `tonga/TongaISAInst` — 58 op classes (common Neuron ISA) |
| **Extension ISA** | `sunda/SundaISAInst` — 33 op classes (collectives, MX, tiled kernels); Sunda+ only |
| **Emit loop** | `targets/codegen/BirCodeGenLoop.py::BirCodeGenLoop(BirCodeGenLoopGen)` — **105** `codegen<Op>` emitters |
| **Cost-model twin** | `xla::hilo::{Tonga,Sunda,Cayman,Mariana}` in `hlo2penguin`; `getType` ⇒ `{0,1,2,3}` |
| **Cross-ref** | [1.02 Codename Taxonomy](../arch/codename-taxonomy.md), [Part 8 `CoreV*Gen`](../walrus/) |

---

## Target Selection — `Target.py`

### Purpose

`Target.py` is pure selection glue: ~422 source lines, two public functions, no codegen. `parse_target` turns a user/arch string into a concrete target *instance*; `get_platform_target` is the inverse, projecting an instance back to the canonical platform name NKI and the runtime expect. This is the front-end Python twin of libBIR's `string2ArchLevel` / `ArchLevel2ExternalString` mappers ([1.02](../arch/codename-taxonomy.md)), but it accepts a wider alias set and constructs a live object rather than an ordinal.

### Entry Point

```text
CLI --target / --arch  +  --internal-num-neuroncores-per-sengine
  └─ driver/jobs/Frontend, HLOToTensorizer, penguin/Compile.py
       └─ Target.parse_target(target_str, num_neuroncores_per_sengine=1, revision=…)
            ├─ "tonga"/"inf1"           → Tonga(...)    platform "inf1"
            ├─ "sunda"/"trn1"           → Sunda(...)    platform "trn1"
            ├─ "cayman"/"trn3pre"/"trn2"→ Cayman(...)   platform "trn2"
            └─ "corev4"/"mariana"/"trn3"→ CoreV4(...)   platform "trn3"   (try-guarded)
```

### Algorithm

```c
// __pyx_pf_…parse_target @0x9720   (Target.py:47–76)
function parse_target(target_str, num_neuroncores_per_sengine=1, revision=…):
    target_lower = target_str.lower()                 // py 63 — n_s_lower

    // Each branch is `target_lower in (<alias tuple>)`, lowered to
    // memcmp@plt / PyObject_RichCompare equality chains.
    if target_lower in ("tonga","inf1"):              // py 65 — memcmp @0x994e
        return Tonga (num_neuroncores_per_sengine=…, revision=…)        // py 66
    if target_lower in ("sunda","trn1"):              // py 67 — memcmp @0x9a68
        return Sunda (...)                                              // py 68
    if target_lower in ("cayman","trn2","trn3pre"):   // py 69 — memcmp @0x9bc4
        return Cayman(...)                                             // py 70
    if target_lower in ("corev4","mariana","trn3"):   // py 71 — memcmp @0x9ce4
        if CoreV4 is <unavailable sentinel>:           // import-time try/except
            raise ImportError("If CoreV4 target is requested but not available")
        return CoreV4(...)                                            // py 72

    raise ValueError("Unknown target")                 // py 75 — kp_u_Unknown_target
```

The constructor forwards two axes that the selection therefore fixes at construction time: `num_neuroncores_per_sengine` (the LNC fan-out, default 1) and `revision` (the `r0`/`r1` ArchRevision stepping, [1.02 §ArchRevision](../arch/codename-taxonomy.md)). So `parse_target` does not just pick a generation — it pins generation × LNC-count × silicon revision in one call.

### The alias dispatch table

The alias tokens are `__pyx_kp_u_*` literals byte-present in `Target.so` `.rodata`; the token→class binding matches libBIR's `ArchLevel2ExternalString` ([1.02](../arch/codename-taxonomy.md)). The **standalone** interned tokens are `tonga`, `sunda`, `cayman`, `corev4`, `mariana`, `trn3pre`; the `inf1`/`trn1`/`trn2`/`trn3` spellings appear in `Target.so` only inside the `parse_target`/`get_platform_target` **docstrings** (e.g. `"Canonical platform target name ('trn1', 'trn2', 'trn3', or 'inf1')"`), so they are documented aliases whose standalone-key presence is not independently byte-confirmed.

| Accepted alias tokens (case-insensitive via `.lower()`) | → Class | Platform | Confidence |
|---|---|---|---|
| `tonga`, (`inf1`) | `Tonga` | `inf1` | `tonga` interned standalone; `inf1` docstring-only |
| `sunda`, (`trn1`) | `Sunda` | `trn1` | `sunda` interned standalone; `trn1` docstring-only |
| `cayman`, `trn3pre`, (`trn2`) | `Cayman` | `trn2` | `cayman`/`trn3pre` interned standalone; `trn2` docstring-only |
| `corev4`, `mariana`, (`trn3`) | `CoreV4` | `trn3` | `corev4`/`mariana` interned standalone; `trn3` docstring-only |

> **GOTCHA — `trn3pre` resolves to Cayman (gen3), not CoreV4.** The marketed Trainium index trails the silicon generation by one: `Trn2` = gen3 = Cayman, `Trn3` = gen4 = CoreV4 ([1.02 §off-by-one](../arch/codename-taxonomy.md)). `trn3pre` is a pre-silicon / early-Trn3 tag — an interned standalone string — that maps to the **gen3 Cayman** class; only bare `trn3` (or `mariana`/`corev4`) reaches CoreV4. A reimplementer who routes `trn3pre` to CoreV4 will select the wrong matmul-dtype policy and the wrong encoder.

> **GOTCHA — `v1`/`v2` are not aliases on this path.** The CoreVN numbering invites a
> `v1 → Tonga` / `v2 → Sunda` shorthand, but a standalone-string sweep of `Target.so`
> finds no `v1` or `v2` token anywhere in the module — they are absent, not merely
> undocumented. Do not accept bare `v1`/`v2` in `parse_target`: the CoreVN version lives
> as the `_hierarchy_version` integer on the class, not as a parse alias.

### `get_platform_target` — the inverse

```c
// __pyx_pw_…get_platform_target @0x8570   (Target.py:99–131)
function get_platform_target(target):
    // reads the class-discriminator attr off the instance
    disc = getattr(target, <name/target_type>)        // PyObject_GetAttr @0x8e30
    if disc is Sunda-class:  return "trn1"
    if disc is Cayman-class: return "trn2"
    if disc is CoreV4-class: return "trn3"
    if disc is Tonga-class:  return "inf1"
    raise ValueError("Unknown target type")            // PyErr_Format @0x8873
```

This is the bridge to NKI: `NeuronCodegen`/`KernelBuilder` and `nki/_torch_xla` call `get_platform_target` to obtain the canonical `{trn1,trn2,trn3,inf1}` string NKI keys its lowering on (see [Part 6](../nki/)). The reverse map is the docstring-`Returns` order, cross-checked against libBIR `ArchLevel2ExternalString {10→Inf1,20→Trn1,30→Trn2,40→Trn3}` — they agree.

> **NOTE — three name spaces, three mappers.** `get_platform_target` emits the *marketed* name (`Trn1`…). libBIR additionally carries an *internal* name (`sunda`/`gen3`/`core_v4`) and a *runtime codename* (`mariana` for gen4). Keep them separate; [1.02](../arch/codename-taxonomy.md) is the authority on which mapper produces which spelling.

---

## The Class Hierarchy and `register_target`

### Purpose

The four target classes form a **single linear inheritance chain**. Each concrete generation adds a thin delta over its parent; the abstract `Tonga` base defines the complete API. This is the structural backbone of the whole abstraction — every capability query bottoms out in either an override down the chain or an inherited base method.

```text
Tonga                            (tonga/Tonga.py    — ABSTRACT base, gen1 Inferentia codegen)
  └── Sunda(Tonga)               (sunda/Sunda.py    — FIRST CONCRETE arch; full profile + ISA table)
        └── Cayman(Sunda)        (cayman/Cayman.py  — gen3 deltas)
              └── CoreV4(Cayman)  (core_v4/CoreV4.py — gen4 deltas; conditionally imported)
```

> **QUIRK — `Tonga` is the abstract base *and* the gen1 device.** The directory `targets/tonga/` is the Inferentia / gen1 / CoreV1 / Inf1 codegen base, and the class name `Tonga` doubles as both the abstract base class *and* the gen1 device. It is **not** a fifth silicon generation. The cost-model getters on `Tonga` raise `NotImplementedError` (property-wrapped); `Sunda` is the first class that fills them in. So "abstract base" is literal: `Tonga` defines the shape, `Sunda` is the first instantiable concrete profile.

### Evidence for each base link

Read from the bare-class-name base references and import literals in each module:

| Link | Evidence | Confidence |
|---|---|---|
| `Sunda(Tonga)` | `Sunda.so` references bare `Tonga` and imports `…tonga.Tonga` | CERTAIN |
| `Cayman(Sunda)` | `Cayman.so` bare `Sunda` count 1, `Tonga` count 0 | CERTAIN |
| `CoreV4(Cayman)` | `CoreV4.so` bare `Cayman` count 1, `Sunda` count 0 | CERTAIN |

CoreV4 inherits `Sunda` *transitively* via `Cayman` — a strict linear chain, not a diamond. (This refines an earlier reading that had `Cayman` and `CoreV4` both inheriting `Sunda` directly.)

### `register_target` — the version-keyed registration decorator

`register_target` is a decorator **factory** defined in `tonga/Tonga.py` (`__pyx_pw_…Tonga_1register_target @0x288e0`): `register_target(version=N) → decorator(cls)`. It stamps the class with `_hierarchy_version` (interned `__pyx_n_s_hierarchy_version`). The version kwarg is mandatory — omitting it raises `"Version number is required for @register_target decorator on class "`.

```c
// docstring (.rodata, verbatim):
//   "Decorator to register target classes in the hierarchy.
//    Usage:
//      @register_target(version=5)
//      class CoreV5(CoreV4):
//        ...
//    Args:
//      version: Required version number for the target hierarchy.
//               Higher numbers represent newer hardware versions."
function register_target(version):           // @0x288e0 — decorator factory
    if version is None: raise ValueError("Version number is required …")
    def decorator(cls):
        cls._hierarchy_version = version
        return cls
    return decorator
```

Each concrete target is decorated with its generation number, byte-confirmed by the integer immediate feeding `PyLong_FromLong` in each module-exec:

| Class | Decorator | `_hierarchy_version` | Evidence | Confidence |
|---|---|---|---|---|
| `Tonga` | (base) | 1 (implicit) | abstract base, no `register_target` of its own | MEDIUM |
| `Sunda` | `@register_target(version=2)` | 2 | `mov $0x2,%edi` → `PyLong_FromLong` | CERTAIN |
| `Cayman` | `@register_target(version=3)` | 3 | `mov $0x3,%edi @0x76bc` | CERTAIN |
| `CoreV4` | `@register_target(version=4)` | 4 | `mov $0x4,%edi @0x5147` (version/freq kwargs block) | CERTAIN |
| `CoreV5` | `@register_target(version=5)` | 5 | docstring forward stub only — no `core_v5` `.so` in this wheel | LOW / forward-stub |

`_hierarchy_version` = the CoreVN generation = libBIR `ArchLevel / 10`. This single ordinal is the backbone the whole abstraction orders itself by.

### Targets are orderable

`Tonga` defines the full comparison suite — `__lt__`, `__le__`, `__gt__`, `__ge__`, `__eq__`, `__ne__`, `__hash__`, `__str__` (qualnames present, e.g. `Tonga.Tonga.__lt__`). Comparison is on the `arch` / `_hierarchy_version` ordinal (interned `n_s_arch`), so target objects support `target >= Sunda`-style generation gates in Python — the exact mirror of the libwalrus string gates `arch > Sunda` / `arch > Tonga` ([1.02](../arch/codename-taxonomy.md)). Per-arch feature predicates phrase themselves as `if self >= some_gen: enable X`, and — crucially — `BirCodeGenLoop` uses `self.target < Cayman` to gate the gen3+ matmul path (see [§Parameterization](#parameterization)).

### Per-target file footprint

| Module | ~src lines | What it overrides | Confidence |
|---|---|---|---|
| `Tonga.py` | ~1036 | The complete API: cost-model getters (raise `NotImplementedError`), PE geometry `getPECols`/`getPERows`/`getPEModel`, `partition_size*`, `getMatmultOperand*Type`, `get_native_alu_dtype`, the feature-flag properties, `register_target`, HW-spec attrs proxied from `self.tpb` (the `neuronxcc.hwm` model), `_use_inferentia_hwm` | CERTAIN (DWARF) |
| `Sunda.py` | (first concrete) | Fills in **every** cost-model getter with literal `LatencyModel`/`DMALatencyModel`/`ProfileBasedDMALatencyModel` instances, the four `*_ENGINE_FREQ` globals (PE=280, ACT=140, DVE=112, POOL=140 — `Sunda` is the **only** class that defines `POOL_ENGINE_FREQ`), the DGE gating flags, `max_*` limits, `getMatmultOperandType`/`getMatmultOperandsType`, the tile-size choosers, `PoolOnlyInt32Ops`/`PoolOnlyInt64Ops`, `required_pe_rhs_align_in_bytes` | CERTAIN (DWARF) |
| `Cayman.py` | (small) | DELTAS only: `register_target(3)`, engine-freq overrides (PE=240, ACT=120, DVE=96 — `PE_/ACT_/DVE_ENGINE_FREQ`; **no `POOL_ENGINE_FREQ` override**, inherits Sunda's 140), `DMALatencyModel.bandwidth_per_engine=23`, `disable_auto_cast` override, `double_row_stride_alignment`, `max_topk_elements` / `max_sequence_bounds_elements` deltas | CERTAIN (DWARF) |
| `CoreV4.py` | ~423 | `register_target(4)`, overrides `__init__` and `getMatmultOperandType` (gen4 FP8/MX operand path), engine-freq (PE=240, ACT=120, DVE=120 — `PE_/ACT_/DVE_ENGINE_FREQ`; **no `POOL_ENGINE_FREQ`**), `DMALatencyModel.bandwidth_per_engine=57.5`; module-exec int block @`0x5147` confirms version=4 + freq deltas {0x78=120, 0xf0=240} + DMA `start_latency` 0x514=1300 | CERTAIN (DWARF + immediates) |

---

## The ISA / Gen / ISAInst Three-Layer Stack

### Purpose

Penguin-IR instruction classes are organized in three layers. A reimplementer needs all three to understand *which op a target can emit* and *how it serializes to BIR*. The bottom layer is machine-generated encoder glue; the middle is hand-written abstract bases; the top is the per-target op set whose **presence** is the coarse capability gate.

```text
LAYER A  targets/generated/<Op>Gen   — MACHINE-GENERATED encoder base (104 modules).
         Carries the op's STRUCTURE: operand slots, BIR encode glue, default attrs,
         the .serialize() that produces the BIR-JSON producer record. The "Gen" suffix
         marks auto-emission from an op spec.

LAYER B  targets/tonga/TongaInst     — ABSTRACT Inst bases shared by ALL arches:
         NeuronInst, NeuronUnaryInst, NeuronNaryInst, NeuronIndirectAP,
         NeuronNordsetAP/IndirectAP, plus the most-fundamental ops every arch has
         (DMACopyOp, InlineASMInst, InlineASMBytesInst, TensorCopyBase, TensorCopyOp).
         Imports from penguin.ir (Access, AffineExpr, Axis, DMAQoS, IRCloner,
         TileAccess) — it sits ON TOP of the Penguin IR (Part 5.1–5.4).

LAYER C  the per-target *ISAInst modules — hand-written subclasses of the Layer-A
         *Gen bases that add verify()/semantics/legalization:
           • tonga/TongaISAInst  — the BASE ISA op set    (58 op classes)
           • sunda/SundaISAInst  — the SUNDA EXTENSION set (33 op classes)
```

### `TongaISAInst` — the base ISA (58 ops)

The common Neuron ISA every modern arch (Sunda+) inherits. Each `<Op>` subclasses `targets/generated/<Op>Gen` (import literals confirm `ActivationOpGen`, `MatMulOpGen`, `TensorTensorOpGen`, …). Rather than dump 58 rows, here are the families:

| Family | Representative ops |
|---|---|
| Activation | `ActivationOp`, `ActivationAccumulationOp`, `AffSelTensorScalarOp`, `ReciprocalOp` |
| MatMul | `MatMulOp`, `MatMulOpBase`, `MatMulSparseOp` |
| TensorScalar | `TensorScalarPtrOp`, `TensorScalarGEPOp`, `TensorScalarCacheReduce`, `TensorScalarCacheCumulative` |
| TensorTensor | `TensorTensorOp` |
| Reduce / Pool | `TensorReduceOp`, `PartitionReduceOp`, `TransposeTensorReduceOp`, `SelectReduce`, `RangeSelectReduce`, `ParReduceBNMeanVar` |
| Select | `TensorSelect`, `RangeSelect` |
| BatchNorm | `SundaBNStats`, `SundaBNAggr`, `SundaBNGradient`, `SundaBNBackprop`, `TransposeBatchnormStats2` |
| Transpose | `TransposeOp`, `TransposeOpBase`, `ShuffleTInst` |
| DMA-transpose | `DMATranspose`, `DMATransposeLoad/Store/Copy`, `DMAIndirectTranspose` |
| Memory | `MemsetOp`, `SBAtomLoad`, `SBAtomStore`, `LoadTensorToRegister`, `NeuronReadTensorPtr`, `NeuronReplicateAP` |
| Indirect DMA | `NeuronIndirectLoad/Save/RMW/LoadStore` |
| DRAM | `NeuronDDRInst`, `NeuronDirectDDRInst` |
| Copy | `TensorCopyPredicated`, `TensorCopyDynamicBase/Dst/Src` |
| Broadcast | `BroadcastPartition`, `SimpleBroadcastPartition`, `ComplexBroadcastPartition` |
| Macro / misc | `NeuronReduceMacro`, `DropoutMaskInst`, `IndexValueInst`, `GetGlobalRankId`, `NeuronPrintInst`, `DebugDeviceBuffer` |

> **NOTE — the `Sunda*` BatchNorm ops live in the *Tonga* base.** `SundaBNStats`/`SundaBNAggr`/`SundaBNGradient`/`SundaBNBackprop` are named "Sunda" for historical reasons but are defined in `TongaISAInst` — they are the shared BN family, not Sunda-exclusive. Do not infer arch-exclusivity from an op's name prefix; infer it from which `*ISAInst` module defines the class.

### `SundaISAInst` — the extension ISA (33 ops; Sunda+ only)

The gen2+ additions over the Tonga base — the ops newer silicon **gained**. Each subclasses a Layer-A `*Gen` (imports confirm `CoreBarrierOpGen`, `MatMulMXOpGen`, `TiledAllGatherOpGen`, …).

| Family | Representative ops |
|---|---|
| Collectives | `LocalCollectiveOp`, `LocalReduceOp`, `SendRecvOp`, `SendRecvCCEOp`, `TiledCollectiveOp`, `TiledAllGatherOp`, `TiledReduceScatterOp`, `TiledAlltoAllOp`, `TiledCollectivePermuteOp`, `TiledCollectivePermuteReduceOp` (+ generated `TiledAllReduceOpGen`) |
| Sync | `CoreBarrierOp`, `ExitOp` |
| MX block-float | `MatMulMXOp`, `QuantizeMXOp` (the microscaling MX path) |
| Tiled kernels | `TiledNativeKernel(Attention/MLP/QKV/RMSNormQuant)`, `TiledRmsNormOp`, `TiledSoftmaxOp`, `TiledSoftmaxDxOp`, `TiledSoftmaxRSumOp`, `TiledOffloadedMemCpy` |
| Shuffle / Gather | `LNCShuffleOp`, `StreamShuffleInst`, `PoolGather` |
| Reduce / Scan | `TensorTensorScanOp` |
| Sequence | `GetSequenceBounds` (+ `max_sequence_bounds_elements` gate) |
| Random | `SundaSetRandState`, `SundaMax8` (+ generated `SundaMax8Gen`, `SundaMaxIndex8Gen`, `SundaMatchReplace8Gen`, `SundaCustomOpGen`) |
| MaxIndex | `MaxIndexAndMatchReplace`, `IndirectCopy` |

> **QUIRK — capability is gated by ISA-class *presence*, not a feature bit.** There is no `TongaISAInst`-resident collective, MX, or tiled-native-kernel op. `Tonga` (gen1) imports only the 58-op `TongaISAInst` base; `Sunda`/`Cayman`/`CoreV4` import base **+** `SundaISAInst`. The set of op classes a target can instantiate *is* its capability surface — a target literally cannot emit an op whose `*ISAInst` class its arch does not import. `Cayman`/`CoreV4` reuse `SundaISAInst` directly; there is no `cayman`/`core_v4` `*ISAInst.so`.

---

## Capability Gating

The target object gates capability along four axes. ISA-class presence (above) is the coarse gate; the next three are fine.

### Matmul operand-dtype legality

`getMatmultOperandType` / `getMatmultOperandsType` / `legal_matmult_operand_type` / `matmult_result_dtype` decide which dtypes the PE array accepts. The legal set widens monotonically up the hierarchy (from the dtype tokens each `.so` references; all pull dtype objects from `neuronxcc.starfish.support.dtype`):

| Gen | Class | Legal matmul operand dtypes (delta over parent) | Confidence |
|---|---|---|---|
| gen1 | `Tonga` | `bf16`, `fp16`, `fp32`, `fp32r`, `float8_e4m3`, `float8_e5m2`, `int8`/`uint8` — **no** `float8_e3m4` | CERTAIN (tokens) |
| gen2 | `Sunda` | **+ `float8_e3m4`** (the E3M4 FP8 variant) | CERTAIN (token) |
| gen3 | `Cayman` | overrides `legal_matmult_operand_type` + `matmult_result_dtype`; adds `double_row_stride_alignment` — the gen3 "double_row" packed-matmul mode | CERTAIN (override + token) |
| gen4 | `CoreV4` | overrides `getMatmultOperandType`; adds **packed ×4 FP8**: `float8_e4m3fn_x4`, `float8_e5m2_x4` (4-elt-packed PE operands) | CERTAIN (override + tokens) |

The CLI matmul-precision flags (`matmult-bf16`, `matmult-fp16`, `matmult-fp32r`, `matmult-fp8e4`, `all-fp32r`, `experimental-all-fp16`, `mm-transpose-type`) are shared option *names*; the per-arch legality is enforced by the methods above, not by the flags ([3.x precision marshalling](../frontend/precision-flag-marshalling.md)).

### The DGE flag cluster (Sunda-and-later)

`Sunda` adds the **DGE (Descriptor-Generation-Engine)** gating cluster — the gen2+ hardware DMA-descriptor offload. Each is a separate `enable_*` property (paired `_enable_x` backing attr + getter) and a CLI option with per-arch default and help text. Enumerated:

| Flag | Docstring fragment | Confidence |
|---|---|---|
| `enable_dge_on_io_dma` | "whether to enble dge on IO dma" | CERTAIN |
| `enable_dge_on_indirect_dma` | "whether to enble dge on indirect…" | CERTAIN |
| `enable_dge_on_dma_transpose` | — | CERTAIN |
| `enable_dge_on_dst_reduce` | — | CERTAIN |
| `enable_dge_on_spill_reload_dma` | "whether to enable dge on spill r…" | CERTAIN |
| `enable_dge_on_vector_indirect_dma` | "whether to enable dge on vector…" | CERTAIN |
| `enable_scalar_dge_vectorization` | — | CERTAIN |
| `dge_par_min_size` | "if true use dge for eligible xba…" | CERTAIN |

Adjacent Sunda additions: `enable_8bit_tensorcopy_cast`, `enable_dmacopy_transpose`, `has_cstart`/`has_cstop` (collective start/stop markers). The DGE cluster is **absent from the Tonga base** — a clean example of the feature surface growing down the hierarchy. `BirCodeGenLoop` reads `enable_dge_on_dst_reduce` / `enable_dge_on_vector_indirect_dma` to choose the DGE descriptor-gen DMA encoding versus the legacy path.

### The Tonga-base feature flags

The base `Tonga` defines a broad property set every arch inherits (byte-present attrs):

```text
enable_dma_transpose, enable_dma_cast, enable_dram_to_dram_transpose,
enable_stream_shuffle, enable_stream_transpose(_small),
enable_trivial_dma_transpose, enable_transpose_reduce,
enable_transpose_batchnormstats2 (+ force_), enable_new_scatter,
enable_replication ("Enable automatic replication inference"),
enable_fp32_mm_transpose, enable_softmax_kernel ("Use high performance softmax kernel"),
enable_softmax_division_delay, enable_ccop_compute_overlap (+ _fine_grained_),
enable_smt_allocator ("enable smt based flow for spill identification …"),
enable_tensorized_spiller,
enable_spill_free_kernels ("Disable all memory allocator and depend on allocation from NKI kernels"),
enable_shard_axis_verifier, enable_experimental_addr_calc;
disable_auto_cast ("Enable FP32 to Bfloat16 auto-conversion"),
disable_8bit_dma_cast, disable_partition_vectorization,
disable_global_redundant_load_elimination,
disable_tiling_for_non_overlapping_mem_access;
can_merge_type, can_lower_generic_to_tensor_copy(_for_new_tiling),
can_lower_generic_load_to_gather; force_tensortensor_psum_sb;
allow_direct_dims_on_p, allow_shuffle;
_use_inferentia_hwm ("Use the Inferentia hardware model only").
```

### Per-target codegen-config constants

`Sunda` carries the full `max_*` sizing set; `Cayman`/`CoreV4` delta a few. Values are `PyLong` immediates in module-exec:

```text
max_computation_tile_size(_in_bytes), max_statebuffer_tile_size_in_bytes,
max_local_tensor_tile_size_in_bytes, max_prefetch_size_in_bytes(_value),
max_prefetch_buffer_size_in_bytes, max_indirect_dma_prefetch_size_in_bytes,
max_dma_access_free_depth, max_batch_norm_reduction_size,
max_dve_bn_stats_partition_elements, max_inflight_allreduces, max_inflight_iterations,
max_sequence_bounds_elements, max_topk_elements, num_engines,
required_pe_rhs_align_in_bytes, dge_par_min_size,
double_row_stride_alignment (Cayman), partitions_per_bank, pool_buffer_size.
```

Sampled Sunda immediates: `0x514=1300` (DMA `start_latency`), `0x190=400`, `0x200=512`, `0x400=1024`, `0x800=2048`, `0x1000=4096`, `0x2000=8192`, `0x4000=16384`, `0x8000=32768`, `0x30000=196608`, `0x800000=8388608`, `0x2aaaa=174762` — the tile/prefetch/free-depth byte limits. Per-arch codegen *methods* chosen at the target: `chooseReduceTileSize`, `chooseTopKTileSize`, `chooseCustomOpDMATileSize`, `chooseDMATileSize` (Tonga base), `lower_broadcast_par_to_stream_shuffle`, `find_layout_producer_insts`, `is_replication_candidate`, `PoolOnlyInt32Ops`/`PoolOnlyInt64Ops`, `get_native_alu_dtype`, `getPECols`/`getPERows`/`getPEModel`, `partition_size`/`partition_size_per_bank` (proxied from `self.tpb`, the `neuronxcc.hwm` HW model — see [Part 1](../arch/hardware-constant-matrix.md)).

---

## Parameterization

### Purpose

The target **instance** is the single per-arch parameter threaded through the whole Penguin backend. Nothing else carries generation. This section traces the four consumers and shows the three granularities at which `BirCodeGenLoop` reads it.

### Selection → instance → everywhere

```text
--target / --arch  +  --internal-num-neuroncores-per-sengine
  └─ driver/jobs/Frontend · HLOToTensorizer · penguin/Compile.py
       └─ Target.parse_target(target_str, num_neuroncores_per_sengine=…, revision=…)
            └─ concrete target instance  ──┬─► every pass (target kwarg, via PassConstructorBuilder)
                                           ├─► BirCodeGenLoop        (arch-gate × dtype × feature-flag)
                                           ├─► NeuronCodegen/KernelBuilder (imports the *ISAInst classes)
                                           └─► cost model            (LatencyModel/DMA attrs)
```

`parse_target` consumers are byte-confirmed across modules: `Compile.so`, `Frontend.so`, `HLOToTensorizer.so`, `ir/Function.so`, the `targets/transforms` passes (`TargetLowering`, `LegalizeSundaAccess`, `RemoveShardedPartitionAxes`), and `codegen/BirCodeGenLoop.so`. `Compile.so` references `Sunda` + `SundaISAInst` + `num_neuroncores_per_sengine` — it holds the constructed target.

### `BirCodeGenLoop` — the Penguin-IR → BIR emit loop

`class BirCodeGenLoop(BirCodeGenLoopGen)` in `targets/codegen/BirCodeGenLoop.py`. `BirCodeGenLoopGen` is the auto-generated Layer-A base carrying the **105** `codegen<Op>()` per-op emitters — confirmed by counting distinct `codegen*` methods in the Cython symtab (e.g. `codegenActivationOp`, `codegenMatMulOp`, `codegenMatMulSparseOp`, `codegenMatMulMXOp`, `codegenDMACopyOp`, `codegenAllReduceOp`, `codegenAllGatherOp`, `codegenCoreBarrierOp`, `codegenGetSequenceBounds`). The module docstring states the IR level explicitly:

> `"BirCodeGen - Generate Backend IR from tensoriser IR at the TongaISAInst level."`

So the loop operates at the **Tonga-base ISA level**; per-arch specialism is injected by reading the target object. The three confirmed query granularities:

```c
// BirCodeGenLoop matmul emit (schematic)
function codegenMatMulOp(self, op):              // BirCodeGenLoop.codegenMatMulOp
    // (1) ARCH-ORDINAL GATE via target.__lt__ on _hierarchy_version
    if self.target < Cayman:                     // gen1/gen2: no double-row encoding
        ap = standard_matmul_ap(op)
    else:                                        // gen3+: packed double-row
        ap = self.addDoubleRowAP(op)             // __pyx_pw_…_285 addDoubleRowAP @0x1415a0
        //   ↳ self.cayman_matmul_double_row_ap  __pyx_pw_…_287 @0xecf30
    if op.is_sparse:
        self.addSparseMatmulAP(op)               // __pyx_pw_…_281 addSparseMatmulAP @0xf2430

    // (2) DTYPE-LEGALITY via target method (per-arch legal set, §Capability)
    operand_dtype = self.target.getMatmultOperandType(op)   // CoreV4 ⇒ float8_*_x4

    // (3) FEATURE-FLAG via target property (DGE vs legacy DMA path; gen2+ only)
    if self.target.enable_dge_on_dst_reduce: emit_dge_descriptor(...)
    else:                                        emit_legacy_dma(...)
```

So `BirCodeGenLoop` is one codegen body parameterized at three granularities: **(arch-ordinal gate) × (dtype-legality method) × (feature-flag property)**. It holds `arch` + `target` as instance attrs and an internal NKI-kernel registry (`_build_internal_kernel_registry` @ pw `0x957e0`/`0xad3e0`; `get_internal_kernel_registry` @ `0x87d70`) keyed off the target.

> **NOTE — `self.target < Cayman` is the gen-gate idiom.** The string `self.target < Cayman` is byte-present (verbatim) in `BirCodeGenLoop.so` alongside the bare `Cayman` reference: because targets are orderable on `_hierarchy_version` (§Hierarchy), the gen3+ double-row PE encoding is gated by a single comparison, not a generation switch. `addDoubleRowAP` (`0x1415a0`), `cayman_matmul_double_row_ap` (`0xecf30`), and `addSparseMatmulAP` (`0xf2430`) are confirmed `BirCodeGenLoop` methods (Cython symtab). A reimplementer should make targets comparable on the version ordinal first; the rest of the gating falls out.

### `NeuronCodegen` / `KernelBuilder` (NKI front)

`NeuronCodegen`/`KernelBuilder` imports the per-target ISAInst classes directly: `…sunda.Sunda` + `…sunda.SundaISAInst` + `…tonga.TongaISAInst` + `…core_v4.CoreV4` + `…cayman.Cayman` (see [Part 6](../nki/)). The NKI front instantiates op nodes from the per-target ISAInst layer — i.e. the set of emittable op classes is the per-arch ISA surface (§ISA stack). It also consumes `get_platform_target(target)` for the canonical `{trn1,trn2,trn3,inf1}` string (byte-confirmed in `nki/_torch_xla` and `nki/compiler/backends/neuron/TraceKernel`). The matmul-family emitters branch on the target (`double_row_gen3` / `perf_mode … not supported on <target>` validation literals are target-keyed).

### The cost model

The cost-model passes (`CycleBasedLayoutCostModel`, `InstructionLatencyModel`) read `target.get_matmult_latency` / `get_*_latency` / `profile_based_dma_latency_model` via `getattr` and call `.get_latency()` on the `LatencyModel` instances the target carries (§footprint). The target is therefore also the per-arch cost-model parameter for tiling/layout/scheduling and the `targets/transforms/autotune` autotuner.

### The `CodeGenFlow` driver

`targets/sunda/CodeGenFlow` + `NKICodeGenFlow` + `SharedCodeGenFlow` sequence the pass roster with the target as the `target` kwarg of `PassConstructorBuilder` (guard literal: `"PassConstructorBuilder does not have target but %s requires target"`). Passes that need arch info declare a target requirement and receive the instance. The driver imports the `Sunda` class + the `sunda`/`tonga` pass packages; `Cayman`/`CoreV4` reuse the Sunda `CodeGenFlow` and Sunda passes — there is no `cayman`/`core_v4` `CodeGenFlow.so`, only `__init__` in `cayman/core_v4/passes/`.

---

## The Boundary — Python Target vs C++ `CoreV*GenImpl`

The Python target abstraction and the C++ `libwalrus` per-arch backend ([Part 8](../walrus/)) are two **parallel** per-arch hierarchies that meet at the BIR-JSON handoff. They mirror each other and split responsibility.

| Python (this page) | C++ `libwalrus` |
|---|---|
| `Tonga` (abstract gen1) | Inferentia legacy — `CoreV1`, no `Gen` class |
| `Sunda` (gen2, full concrete) | `CoreV2Gen → CoreV2GenImpl` (full base) |
| `Cayman` (gen3, deltas) | `CoreV3Gen → CoreV3GenImpl` (selective overrides) |
| `CoreV4` (gen4, deltas) | `CoreV4Gen → CoreV4GenImpl` (selective overrides) |
| selected by `parse_target(str)` | selected by `initCodegen(ArchLevel)` ([1.02](../arch/codename-taxonomy.md)) |
| `_hierarchy_version` {2,3,4} | `bir::Module` ArchLevel {20,30,40} |
| selective override down the chain | selective `visitInst<Op>` override (same pattern) |

> **QUIRK — three "generation" integers, do not conflate.** The Python `_hierarchy_version` is `{Tonga:1, Sunda:2, Cayman:3, CoreV4:4}`. The libBIR `ArchLevel` is `{10,20,30,40,50}` = version×10. The C++ cost-model `hilo::` enum is **0-indexed**: `xla::hilo::Tonga::getType` ⇒ `0` (`xor eax,eax` @ `0x1ec0f60`), `Sunda` ⇒ `1` (`0x1ec0f00`), `Cayman` ⇒ `2` (`0x1ec0ea0`), `Mariana` (= CoreV4's runtime codename) ⇒ `3` (`0x1ec0e40`). Three axes, three offsets; a reimplementer must translate, not assume identity.

### What each side decides

**Python (front) decides policy:** which `*ISAInst` op classes exist for the arch; which matmul operand dtypes are legal (`getMatmultOperandType` / `legal_matmult_operand_type` / `matmult_result_dtype`); the feature flags (`enable_dge_*`, `enable_stream_*`, `disable_auto_cast`…) and `max_*` sizing limits — i.e. which codegen *strategy*; the per-arch cost model; and the arch-gated emit paths in `BirCodeGenLoop` (`self.target < Cayman` → double-row; DGE-vs-legacy DMA). Its output is the BIR-JSON producer record, via the Layer-A `targets/generated/*OpGen .serialize()`.

**C++ `CoreV*GenImpl` (back) decides encoding:** each `visitInst<Op>` emplaces a 64-byte TPB bundle, filling `NEURON_ISA_TPB_*` fields per the `core_v{2,3,4}` ISA-struct namespace, stamps the 16-bit opcode, and `fwrite`s per-engine `.bin`. It also runs the ISA-legality dry-run (`flushISAChecks`), opcode collection, and NEFF packaging. The Python side never touches bundle bytes.

> **NOTE — the override rosters align across the boundary.** `CoreV4GenImpl` overrides `{MatmultMx, QuantizeMx, Activation, Exponential, Rand2}`; the Python `CoreV4` overrides `getMatmultOperandType` (feeding `MatmultMx`) + the MX/quantize path. `CoreV3GenImpl` overrides `{Matmult, MatmultSparse, TensorTensor}`; the Python `Cayman` overrides `legal_matmult_operand_type` + `double_row`. The same selective-delta design appears on both sides — the dtype/feature *policy* the Python layer sets is exactly what the C++ micro-encoding overrides *implement*.

### The handoff

```text
Python BirCodeGenLoop  (BIR-JSON at the TongaISAInst level)
   │  ArchLevel = _hierarchy_version × 10  stamped into bir::Module
   ▼
BIR-JSON ─► libwalrus BIRParserDumper ─► backend::Codegen pass
                                            └─ Generator picks CoreV{2,3,4}GenImpl
                                               by bir::Module ArchLevel
                                                  └─ 64-byte bundles ─► NEFF
```

So `parse_target`'s class choice ultimately selects the C++ `GenImpl`: the front picks the generation, encodes it as `ArchLevel` in the BIR-JSON, and the backend re-derives the same generation to choose its encoder.

---

## Related Components

| Name | Relationship |
|---|---|
| `Target.parse_target` / `get_platform_target` | the selection entry and its NKI-facing inverse |
| `tonga/TongaISAInst`, `sunda/SundaISAInst` | the per-target op-class sets (Layer C) gated by ISA presence |
| `targets/generated/*OpGen` (104) | Layer-A encoder bases; `.serialize()` produces the BIR-JSON record |
| `BirCodeGenLoop(BirCodeGenLoopGen)` | the 105-emitter Penguin-IR → BIR loop, parameterized by the target |
| `sunda/CodeGenFlow` + `NKICodeGenFlow` + `SharedCodeGenFlow` | the per-target pass roster driver (reused by Cayman/CoreV4) |
| `xla::hilo::{Tonga,Sunda,Cayman,Mariana}` | the C++ cost-model twin (`getType` ⇒ 0/1/2/3) |
| `CoreV{2,3,4}GenImpl` (libwalrus) | the backend encoder selected by `ArchLevel = _hierarchy_version × 10` |

## Cross-References

- [Codename ↔ Device ↔ Generation Taxonomy](../arch/codename-taxonomy.md) — the five-rung ArchLevel ladder, the off-by-one Trainium index, the three name-space mappers; the authority for every codename↔arch↔CoreV binding this page leans on (1.02)
- [Vestigial Generations — CoreV1 & CoreV5](../arch/vestigial-generations.md) — why `Tonga`/gen1 is modelled-but-abstract and `CoreV5` is a forward stub (1.03)
- [Per-Generation Hardware-Constant Matrix](../arch/hardware-constant-matrix.md) — the `self.tpb` HW model the target proxies geometry from
- [Penguin IR Node Model](ir-node-model.md) — the `penguin.ir` layer the `Inst` bases (Layer B) sit on (5.1)
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where `parse_target` is called and how the target threads through the job pipeline
- [Frontend Precision-Flag Marshalling](../frontend/precision-flag-marshalling.md) — the CLI matmul-precision flags whose per-arch legality the target methods enforce
- [Part 6 — NKI Kernel DSL](../nki/) — `NeuronCodegen`/`KernelBuilder` importing the `*ISAInst` classes and consuming `get_platform_target`
- [Part 8 — The libwalrus Backend](../walrus/) — the C++ `CoreV*GenImpl` encoder hierarchy this layer hands off to
