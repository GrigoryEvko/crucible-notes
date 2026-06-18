# The BIR-JSON Write Path — `Instruction::toJson`, skip-default, always-v2

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the cp310 build of `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`). The cp311/cp312 builds carry the identical ABI but their virtual addresses drift; offsets, key spellings, and emission order are version-stable. `.text`/`.rodata` are VA==fileoffset.*

## Abstract

This page documents how a `bir::Module` is **serialized to JSON** — the producer side, the exact inverse of the two-pass loader on the [BIR JSON Loader](json-loader.md) page. A serialized BIR module is a developer / round-trip artifact (the shipped NEFF carries the codegen-side JSONs instead); the wire form here is the authoritative L1 representation before lowering. The complete record schema — every per-op key set, every sub-record — lives on the [BIR-JSON Record Schema](json-schema.md) page; this page is about the **mechanism that emits it**.

There is **no `JsonWriter` class**. Serialization is raw `nlohmann::json` mutation: each emitter is an out-param mutator `void toJson(json& out)` that writes keys into `out` with the single primitive `out["key"] = value`. The top driver is `adl_serializer<bir::Module>::to_json` @ `0x48ab00`; per-instruction emission is `bir::Instruction::toJson` @ `0x2e2ea0` (the common header) plus a per-leaf `Inst<X>::toJson` that calls the base first, then appends op-specific keys. The producer and the loader are generated from one ISA spec by `brewer.py` ([brewer / *OpGen contract](brewer-generator.md)), which is why their key names, order, nesting, and enum spellings agree by construction.

Three properties dominate the write path and are this page's gotchas:

- **Skip-default emission.** A field whose value equals its constructor default is *not emitted*. Each field hardcodes its own inline gate (`cmp` against the default ordinal, a null-pointer test, or an empty-list test) immediately before its `operator[]`. There is no generic `is_default` helper. Because the loader restores an absent key to that same default, absence ⇄ default is a fixpoint — this is what makes skip-default round-trip-safe.
- **Always-v2.** `Module::to_json` hardcodes `root["version"] = 2`, and every pelican-expr emit is threaded with version 2 by the caller. The producer *always* writes schema v2, even when it loaded a v1 document. The loader still reads v1 (cross-ref [BIR-JSON Versioning](json-versioning.md)) — so the writer is a one-way v1→v2 normalizer.
- **ICF-merged header bodies.** Identical-code-folding collapses every "no-extra-field" opcode's `toJson` into one shared body: **28** distinct `Inst*::toJson` symbols resolve to the single address `0x434e30`. One address serves many ops.

### Reimplementation contract

To reproduce the write path a reimplementer must reproduce:

- The **`out["key"] = v` primitive** (`operator[]<char const>("key")` returning a `json&`, then `operator=`), threaded through an out-param — not a return-by-value writer.
- The **19-key common header** ([§2](#2-the-common-header--19-keys-0x2e2ea0)) in exact emission order, with each field's emit guard.
- The **per-leaf "base-first, then append" discipline** ([§3](#3-per-op-leaves--base-first-then-append)): every concrete `Inst<X>::toJson` calls `Instruction::toJson(out)` first.
- The **skip-default gate per field** ([§4](#4-skip-default--the-inline-per-field-gate)): null / empty / `== default-ordinal` tested inline before the emit.
- The **nested operand emit** ([§5](#5-nested-objects--argument--accesspattern)): `Argument::toJson` switches arg-kind, merges the chosen AP's flat keys via `update()`, and references tensors **by name**.
- The **module driver** ([§6](#6-the-module-driver-0x48ab00)): single-pass, name-keyed, `version = 2` hardcoded.

| | |
|---|---|
| **Defining binary** | `libBIR.so` (cp310), 115 distinct `Inst*::toJson` leaf symbols |
| **Top write driver** | `adl_serializer<bir::Module>::to_json(json&)` @ `0x48ab00` |
| **Common header emit** | `bir::Instruction::toJson(json&) const` @ `0x2e2ea0` |
| **Header-only ICF body** | `0x434e30` — **28** trivial leaves folded here; `InstCompareAndBranch` alone @ `0x434e40` |
| **Matmul shared body** | `InstMatmultBase::toJson` @ `0x4358b0` (13 op keys) |
| **Operand emit** | `bir::Argument::toJson(json&)` @ `0x2352f0` |
| **Pelican version gate** | `QuasiAffineExpr::toJson(json&, unsigned version)` @ `0x3bbda0` → `toJsonv2`/`toJsonv1` |
| **Round-trip harness** | `bir_roundtrip` @ `0x427dc0` — `from_json` → `to_json` → `dump(indent=2)` |
| **JSON library** | `nlohmann::json` `abi_v3_11_3` (`std::map`-ordered objects) |

> **NOTE —** `libBIR.so`'s `structures.json` carries no `bir::*` struct. Every offset, key spelling, and emission order below is anchored to a decompiled / disassembled `toJson` body, a `.rodata` key literal, or an `nm` symbol-address coincidence. The emission-order tables *are* the wire spec; there is no source struct to copy.

---

## 1. The emission primitive — `out["k"] = v`, no `JsonWriter`

Every emitter has the signature `void toJson(nlohmann::json& out) const` (or, for the `adl_serializer<T>` sub-records, `static void to_json(json& out, const T&)`). The `out` is the second argument — the emitter is an **out-param mutator**, not a builder that returns a value. The body writes each field with one idiom, witnessed ~25× in `Instruction::toJson` alone:

```c
// for each field "k" with value v:
json tmp;                              // construct the value (scalar assign, or adl<Enum>::to_json)
json& slot = out.operator[]<char const>("k");   // operator[] mints/returns the key slot
slot.operator=(tmp);                   // copy the value into the slot
// ~tmp
```

This is the exact primitive the loader inverts with `j.at("k")` / `j.find("k")`. There is no custom serializer object, no visitor, no stream — just `nlohmann::basic_json` mutation. `nlohmann`'s object is a `std::map`, so on `dump()` the keys come out **lexicographically sorted regardless of emission order** ([§7](#7-round-trip--toJson--from_json)); emission order determines *which keys exist*, not their on-wire position. [CONFIRMED — `operator[]<char const>` + `operator=` witnessed throughout `0x2e2ea0` / `0x4363d0`.]

---

## 2. The common header — 19 keys (`0x2e2ea0`)

`bir::Instruction::toJson` @ `0x2e2ea0` is the base serializer every concrete `Inst<X>::toJson` calls first (and, for the 28 ICF-merged trivial ops at `0x434e30`, the *only* thing emitted). It writes the shared header into the `out` param. The emission order below is read **line-by-line from the decompiled body** (the `operator[]("k")` call sites, in source order):

| # | Key | Source line | Wire form | Emit guard | Conf |
|---|---|---|---|---|---|
| 1 | `origin` | 335 | `NamedObjectOrigin` name `{Internal,Penguin,NKI}` | always | CONFIRMED |
| 2 | `opcode` | 353 | `InstructionType` name (`InstructionType2string`) | always | CONFIRMED |
| 3 | `engine` | 370 | `EngineType` name `{Unassigned,Pool,Activation,PE,DMA,DVE,SP,ALL}` | always | CONFIRMED |
| 4 | `engine_id` | 396 | `uint32` (`+0x94`) | always | CONFIRMED |
| 5 | `scheduled_start` | 423 | `int64` (sched block) | always | CONFIRMED |
| 6 | `scheduled_end` | 450 | `int64` (sched block) | always | CONFIRMED |
| 7 | `optin_passes` | 462 | `array<string>` (bit→pass name) | always | CONFIRMED |
| 8 | `dependencies` | 554 | `array<[depName, EdgeKind]>` | **dep list non-empty** | CONFIRMED |
| 9 | `loop_carried_dependencies` | 648 | `array<[name, EdgeKind]>` | **list non-empty** | CONFIRMED |
| 10 | `unroll_dependencies` | 673 | `array<[name, EdgeKind, [QuasiAffineExpr…], [AffinePredicate…]]>` | **vec non-empty** | CONFIRMED |
| 11 | `sync_info` | 1010 | `SyncInfo` object | **flag byte set** | CONFIRMED |
| 12 | `ins` | 1032 | `array<Argument>` | always (empty ok) | CONFIRMED |
| 13 | `indirection_ins` | 1055 | `array<Argument>` | **list non-empty** | CONFIRMED |
| 14 | `outs` | 1077 | `array<Argument>` | always | CONFIRMED |
| 15 | `debug` | 1095 | `OpDebugInfo` object | **present flag set** | CONFIRMED |
| 16 | `loopnest` | 1257 | `array<string>` (LoopAxis names, by reference) | **axis vec non-empty** | CONFIRMED |
| 17 | `predicates` | 1283 | `array<AffinePredicate>` | **vec non-empty** | CONFIRMED |
| 18 | `order` | 1409 | `array<QuasiAffineExpr>` (symbolic schedule) | **vec non-empty** | CONFIRMED |
| 19 | `dma_trigger_debug_update_semaphore_id` | 1454 | `uint` (semaphore id) | **opcode==67 (`InstDMATrigger`) AND sync present** | STRONG |

> *Source lines refer to the decompiled `Instruction::toJson_0x2e2ea0.c` `operator[]("k")` call sites.*

Nine keys are **always emitted**: `origin`, `opcode`, `engine`, `engine_id`, `scheduled_start`, `scheduled_end`, `optin_passes`, `ins`, `outs`. The other ten are **skip-default** ([§4](#4-skip-default--the-inline-per-field-gate)). The base struct layout that backs these offsets is owned by the [Instruction Base](instruction-base.md) page; the dependency-edge model (one shared predecessor list whose per-node 3-bit `EdgeKind` tag distinguishes Flow/Anti/Output/Ordered) is documented there.

Two emitter shapes recur in the header:

- **Enum → name.** `origin`/`opcode`/`engine` go through an `adl<Enum>::to_json` that emits the enum's *name string*, never its ordinal. The L1 ordinal and the L3 silicon byte differ for several enums (`InstructionType`, `Dtype`, `AluOpType`, `DMAQoSClass`); the wire **name** is the only stable representation — see [Op-Family Enums & the L1/L2/L3 Crosswalk](op-family-enums.md).
- **Edge list → tuple.** `dependencies` walks the intrusive predecessor list and emits each edge as a 2-tuple `[depName, EdgeKind2string(kind & 7)]` — the low 3 bits of the `PointerIntPair` carry the edge kind. The guard is the list-non-empty test: the decompiled body walks `v85 = *(_QWORD*)v85; if (!v85) break;` over the list before reaching `operator[]("dependencies")`. [CONFIRMED — `0x2e2ea0` decompile, dependencies block.]

---

## 3. Per-op leaves — base-first, then append

A concrete leaf `Inst<X>::toJson` **always calls `bir::Instruction::toJson(out)` first** (emits the §2 header into `out`), then appends its op-specific keys with the same `out["k"] = v` primitive. Field order on the wire is source-declaration order. `toJson` is a per-leaf vtable slot (the JSON factory `createFromJson` and `sameInst` are *not* vtable slots — only `toJson` is).

Witness — the head of `InstDMA::toJson` @ `0x4363d0`:

```
0x4363e0  call   bir::Instruction::toJson      ; <-- base header first
0x4363e5  mov    rsi, [rbx+0xF0]               ; the DMAQueue* (op field 1)
0x4363ec  test   rsi, rsi                       ; skip-if-null gate (§4)
0x4363ef  jz     skip_queue
0x4363f4  add    rsi, 0x68                       ; queue+0x68 = the queue NAME string
...
0x436400  lea    rsi, "queue"                   ; operator[]("queue") = name
```

The leaf calls the base, then for each op field tests a gate and (if it passes) does `operator[]("k")` followed by the value writer. The "by name" pattern — `queue+0x68` is the queue's name string, not a pointer or id — is universal on the wire ([§5](#5-nested-objects--argument--accesspattern)).

### 3a. The matmul body (`0x4358b0`) — 13 op keys

`InstMatmultBase::toJson` @ `0x4358b0` emits, in this exact order (read from the linear leaf disasm):

```
accumulation_flag · psum_zero_region · replication_resolution · replication_shift_amnt ·
replication_num_rows · is_transpose · is_fmap_onezero · is_weight_onezero ·
tile_size · tile_position · ifmap_quant_offset · weights_quant_offset · perf_mode
```

The first eight + the two quant-offsets are emitted by `MaybeAffine<T>::writeIntoJson` (the field-level writer for "constant-or-loop-affine" values, [§5d](#5d-maybeaffinet--the-constant-or-affine-writer)); `tile_size` / `tile_position` are 2-element `[int,int]` JSON arrays (an `initializer_list` `basic_json` + `MaybeAffine<int>` writes + two `push_back`s); `perf_mode` goes through `bir::to_json(json&, MatmultPerfMode)` and is **always emitted** (not skip-default). The 13-key roster and offsets are owned by the [BIR-JSON Record Schema §2.A](json-schema.md); this is the emission witness for the count.

### 3b. The Matmult family — thunks vs. wrapper (a refinement)

Four matmul opcodes share `InstMatmultBase`, but they do **not** all relate the same way:

| Op (IT) | `toJson` addr | Relationship to Base | Conf |
|---|---|---|---|
| `InstMatmult` (8) | `0x435e00` | **pure 5-byte tail-jmp** `e9 … jmp 0x4358b0` (zero own keys) | CONFIRMED |
| `InstMatmultMx` (95) | `0x435e50` | tail-jmp to Base (zero own keys) | CONFIRMED |
| `InstMatmultSparse` (9) | `0x435e10` | **`call` Base, then append** `compress_ratio` | CONFIRMED |

> **REFINES** the D-S06 phrasing that grouped Matmult/Sparse/Mx as "tail-jmp thunks." `InstMatmult`/`InstMatmultMx` are pure 5-byte `jmp` thunks. `InstMatmultSparse` is **not** a thunk — its body `call`s `InstMatmultBase::toJson`, then `lea rsi, "compress_ratio"` → `operator[]` → `MaybeAffine<int>::writeIntoJson` on the `this+0x328` field, before returning. This matches `compress_ratio` being a Sparse-only key at `+0x328`. [CONFIRMED — `0x435e10` disasm: `call 0x4358b0` at `0x435e1c`, `"compress_ratio"` at `0x435e24`, `MaybeAffine<int>::writeIntoJson` tail-jmp at `0x435e40`.]

### 3c. The ICF-merged header-only bodies (`0x434e30`)

Every opcode with **no extra fields** compiles to a `toJson` body that just calls `Instruction::toJson` and returns. These are byte-identical, so identical-code-folding collapses them: **28 distinct symbols** resolve to `0x434e30`. `nm -DC libBIR.so | grep '^…434e30 ' | grep -c toJson` returns 28. The members include `InstNoOp`, `InstHalt`, `InstMax`, `InstMaxIndex`, `InstSelect`, `InstGather`, `InstRng`, `InstRand2`, `InstTensorLoad`, `InstTensorSave`, `InstReciprocal`, `InstReadActivationAccumulator`, `InstActivationReadAccumulator`, `InstQuantizeMx`, `InstBNStatsAggregate`, `InstBNGradients`, `InstRegisterMove`, `InstEventSemaphore`, `InstAllEngineBarrier`, `InstGetRandState`, `InstRandGetState`, `InstRandSetState`, `InstGPSIMDSB2SB`, `InstStreamTranspose`, `InstNonzeroWithCount`, `InstGetSequenceBounds`, `InstGeneric`, `InstTerminator`. `InstCompareAndBranch` sits alone at `0x434e40`. [CONFIRMED — `nm -DC` address coincidence.]

> **GOTCHA —** when a reverse-engineer sees a `toJson` cross-reference resolve to `0x434e30`, the *symbol name* is not unique to one op: 28 opcodes share that body. This is the write-side counterpart of the loader's empty `readFieldsFromJson` returns for header-only ops. A header-only op at full default serializes to the §2 header and nothing else.

---

## 4. Skip-default — the inline per-field gate

The defining quirk. A field equal to its constructor default is **omitted**. There is no `is_default()` helper; each field tests its own condition immediately before `operator[]`. The canonical witness is `InstDMA::toJson` @ `0x4363d0`, which emits exactly three op fields, two of them skip-default:

```
; field "queue" — SKIP-IF-NULL
0x4363e5  mov   rsi, [rbx+0xF0]          ; DMAQueue*
0x4363ec  test  rsi, rsi
0x4363ef  jz    skip_queue               ; null → omit "queue"
          ...                            ; else queue+0x68 (name) → operator[]("queue")

; field "dge_type" — SKIP-IF-DEFAULT (==3 Unassigned)
0x43645b  cmp   dword [rbx+0xF8], 3      ; DGEType default = 3
0x436462  jnz   emit_dge_type            ; != 3 → emit; == 3 → omit
          ...                            ; bir::to_json(json&, DGEType)

; field "duplicate" — SKIP-IF-NO-VALUE (MaybeAffine present flag)
0x436464  cmp   byte [rbx+0x120], 0      ; queue-present sub-gate
0x436498  cmp   byte [rbx+0x100], 0      ; MaybeAffine<bool> present flag
          ...                            ; MaybeAffine<bool>::writeIntoJson
```

So an `InstDMA` at full default emits **only the §2 common header** — zero DMA keys. [CONFIRMED — `0x4363d0` disasm, all three gates byte-exact.]

The pattern generalizes to three gate shapes:

| Field kind | Default test | Examples |
|---|---|---|
| pointer / by-name ref | `test reg, reg; jz` (null) | `queue`, `memref_owner_func`, `reg_ap_offset` |
| enum scalar | `cmp [field], <default-ordinal>; jnz emit` | `dge_type` (==3 Unassigned), `perf_mode` (None=0), `cc_dim` (Free), `cc_type_hint` (None) |
| `MaybeAffine<T>` | present-flag byte == 0 | `duplicate`, `accumulation_flag`, `can_read_uninit` |
| vector / intrusive list | begin == end (empty) | `dependencies`, `loopnest`, `predicates`, `order`, `indirection_ins` |
| optional sub-object | present-flag byte clear | `sync_info`, `debug` |

This is symmetric with the loader, which reads each optional key with `j.find("k") != end()` / a value-with-default and restores the constructor default when the key is absent ([BIR JSON Loader](json-loader.md)). **Absence ⇄ default is therefore a fixpoint**, which is what makes skip-default safe for round-trip ([§7](#7-round-trip--toJson--from_json)). The corollary: an input that *redundantly spells* a default value is dropped on re-emit — a benign canonicalizing diff.

> **Skip-default is decisive, not cosmetic.** It is why a default-elided BIR-JSON is the canonical representative of its equivalence class, and why the loader's defaults are the exact inverse of the writer's omissions. A reimplementer who emits all keys unconditionally produces *semantically equal* but *non-canonical* output that will diff against the reference re-emit.

---

## 5. Nested objects — Argument / AccessPattern

### 5a. `Argument::toJson` (`0x2352f0`) — the operand switch

An operand in `ins` / `indirection_ins` / `outs` is a `bir::Argument` tagged union. `Argument::toJson` switches on the arg-kind byte at `+0x18` and emits the chosen access-pattern or immediate **inline** (flat keys merged into the argument object, not nested under a sub-object). The merge is `nlohmann::basic_json::update()`:

| kind | Dispatch | Merge | Conf |
|---|---|---|---|
| 1 | `PhysicalAccessPattern::to_json` @ `0x4869b0` | then `update()` | CONFIRMED |
| 2 | `SymbolicAccessPattern::toJson` @ `0x3d7a20` | **tail-jmp** | CONFIRMED |
| 3 | `RegisterAccessPattern::to_json` @ `0x487090` | then `update()` | CONFIRMED |
| 6 / 7 / 8 / 0xB | immediate — virtual dispatch via arg-obj vtable slot `[obj+0x28]` → `ImmediateValue` / `SymbolicImmediateValue` / `ImmediateArray::toJson` | — | CONFIRMED |
| else | FATAL `bir::reportError "Unhandled argument kind"` | — | CONFIRMED |

Witness — the `cmp eax,1 / cmp al,2 / cmp eax,3 / cmp eax,6 / 7 / 8 / 0xB` cascade and the `"Unhandled argument kind"` `lea` at `0x2354cb`. [CONFIRMED — `0x2352f0` disasm.] So an operand serializes to a **flat** object carrying the AP's `"kind"` discriminator (`"physical_ap"` / `"symbolic_ap"` / `"register_ap"`) plus that AP's own fields, **by value** — the AP is not referenced by id. The full operand / immediate / register value model is owned by [BIR Value Model](value-model.md).

### 5b. The access-pattern bodies — tensors referenced by name

| AP | `to_json` | Key roster (in order) |
|---|---|---|
| `PhysicalAccessPattern` | `0x4869b0` | `kind="physical_ap"` · `ap` (APPair `[step,num]` array) · `offset` · `dtype` · `memref` (MemoryLocation **name**) · `memsetref` (MemoryLocationSet **name**) · `access_shape` · `dynamic_ap_info` (skip-default) · `memref_owner_func` |
| `SymbolicAccessPattern` | `0x3d7a20` | `kind="symbolic_ap"` · `ap` · `dtype` · `memsetref` · `tensor_indirect_arg_id` (skip) · `num_tensor_indirect_indices` (skip) · `addrs` (`array<QuasiAffineExpr>`, version-gated) · `access_shape` |
| `RegisterAccessPattern` | `0x487090` | `kind="register_ap"` · `ap` · `dtype` · `memsetref` · `regref` (Register **name**) · `is_regloc_offset` · `reg_ap_offset` · `const_ap_offset` · `reg_sub_tensor_id` · `const_sub_tensor_id` |

The access pattern points at its tensor by **name** (`memref`/`memsetref` strings), the inverse of the loader's operand→location name resolution. No inline tensor copy. The `MemoryLocation` / `MemoryLocationSet` placement records — emitted inline in the function's `allocations` container, not by reference — are owned by [MemoryLocation / Storage](memory-location.md). [CONFIRMED — key rosters per the cited `to_json` addresses.]

### 5c. Pelican expr — version threaded by the caller (`0x3bbda0`)

`bir::QuasiAffineExpr::toJson(json&, unsigned version)` @ `0x3bbda0` takes the version as a **4th argument in `edx`** and dispatches on it:

```
0x3bbda5  cmp   edx, 1
0x3bbdaa  cmp   edx, 2
          ... edx==2 → call toJsonv2 (0x3bab90)
          ... edx==1 → call toJsonv1 (0x3b9f30)
          ... else   → FATAL "Check the version - should be 1 (old) or 2 (new)"
```

The version is supplied **by the caller**, *not* read from `Module::getVersion()` inside `toJson`. Because the module driver hardcodes 2 ([§6](#6-the-module-driver-0x48ab00)) and threads 2 into every pelican emit, **`toJsonv2` is the always-taken path on the write side**; `toJsonv1` is reachable only if a caller passes 1, and no such caller exists on the write path. `toJsonv1`/`toJsonv2` differ in the on-wire encoding of the pelican `RefPtr<Expr>` affine tree (the `addrs` / `address_expr` / `bank_expr` / `order` / unroll-predicate lists). This is the **sole** version-keyed divergence in the writer; the rest of the schema is version-agnostic. The full v1↔v2 split is owned by [BIR-JSON Versioning](json-versioning.md). [CONFIRMED — `0x3bbda0` disasm.]

### 5d. `MaybeAffine<T>::writeIntoJson` — the constant-or-affine writer

`MaybeAffine<T>::writeIntoJson` (`0x448900` for `u8`, and the `u32`/`int`/`bool` siblings) is the field writer for values that may be a compile-time constant **or** a loop-dependent affine expression (matmul flags, DMA `duplicate`, …). It inspects the `MaybeAffine` discriminant and writes either the scalar or a nested affine expression under the `operator[]`-returned slot. **Skip-default is the caller's job** — `writeIntoJson` assumes a present value; the `cmp present-flag, 0` gate lives in the leaf body before the call ([§4](#4-skip-default--the-inline-per-field-gate)). [CONFIRMED — `MaybeAffine<bool>::writeIntoJson` tail-jmp from `InstDMA::toJson` at `0x436490` gated by `0x436498`.]

---

## 6. The module driver (`0x48ab00`)

`adl_serializer<bir::Module>::to_json` @ `0x48ab00` is the top write driver — the inverse of the loader's two-pass design, but **single-pass**: the in-memory graph is already fully resolved, and everything serializes by name, so there is no construct/resolve split on emit. The emission sequence into the root object `a1` (read from the decompiled body):

| Step | Key / target | Decompiled line | Mechanism | Conf |
|---|---|---|---|---|
| W1 | `functions` container | 442 | `NamedObjectContainer<FunctionHolder,Function>::to_json` → **MOVE-assigned as the root object** (`sub_482080(a1, …)`) | CONFIRMED |
| W2 | DMA queues | 446–454 | gated `if (*((char**)a2+10) != a2+80)` (queue list non-empty) → `NamedObjectContainer<Module,DMAQueue>::to_json` → `update()`-merge into root | CONFIRMED |
| W3 | `nki_functions` | 462 | second `FunctionHolder` container emit, stored under literal key `"nki_functions"` | CONFIRMED |
| W4 | **`version` = 2** | 483–487 | `LOBYTE(tmp)=5` (json `number_unsigned`), `tmp body = 2`, `root["version"] = tmp` — **HARDCODED** | CONFIRMED |
| W5 | `arch` | 504 | `adl<ArchLevel>::to_json(*(a2+43))` | CONFIRMED |
| W6 | `revision` | 521 | `adl<ArchRevision>::to_json(*(a2+44))` | CONFIRMED |
| W7 | `artifact_path` | 536 | `Module::getArtifactAbsPath` (string) | CONFIRMED |
| W8 | `artifact_info` | — | `adl<ModuleArtifactInfo>::to_json` | STRONG |
| W9 | `attributes` | — | flat attribute bag, each member skip-default-gated (`previous_pass`, NEFF feature gates, `replica_groups`, `src_target_pairs`, …) | STRONG |
| W10 | `call_to_physical_memlocs` | 1698 | `adl<MapVector<InstCall*,SetVector<MemoryLocation*>>>::to_json` — inter-function `InstCall`→memlocs binding, by name | CONFIRMED |

The decisive witness — the v2 stamp:

```c
LOBYTE(v430) = 5;     // nlohmann value_t::number_unsigned
v431 = 2;             // the value: 2
...
v18 = a1.operator[]("version");
sub_482080(v18, &v430);   // root["version"] = 2
```

[CONFIRMED — `0x48ab00` decompile, lines 482–496.]

### 6a. Container structure and main-first

Every `NamedObjectContainer<…>::to_json` walks its element list, calls `<Element>::toJson()` per entry to build the element object, sets that object's `name` (the element's name string) and `origin`, and pushes it into the container array. The **element's own `toJson` does not re-emit its name** — the name is the container key/field. This is exactly why `Instruction::toJson`'s first key is `origin`, not the instruction name. The `"main"` function / block is matched by `std::string::compare(…, "main")` and emitted **first** (main-first ordering, mirrored by the loader). The function `Storage` array is emitted under the wire key **`allocations`** (a structural "storages" label maps to the literal JSON key `allocations`). [CONFIRMED — container `to_json` bodies + the loader's symmetric `json["allocations"]` iteration.]

### 6b. Name-keyed, not id-keyed

Every cross-object reference on the wire is a **name string**: operand→tensor (`memref`/`memsetref`), `dependencies` (`[instName, EdgeKind]`), `queue` (queue name), `InstCall` args (memloc names), phi `incoming_values` (block names). **No numeric handles are emitted.** The only numeric ids on the wire are intra-tensor (`memLocSetId`, `tensor_id`, `table_entry_id`) and the DMA-trigger debug semaphore id — internal, not cross-object handles. This is the exact inverse of the loader's `getXByName` resolution model and is what makes the two-pass loader necessary on read but unnecessary on write. [CONFIRMED.]

---

## 7. Round-trip — `toJson ∘ from_json`

The driver `bir_roundtrip` @ `0x427dc0` (links `libBIR` only) runs: read stdin JSON → `doc["module"]` → `adl<Module>::from_json` (the two-pass loader) → `adl<Module>::to_json` (this emitter) → `out.dump(indent=2)` → stdout. It is a serialize-determinism / schema-fidelity harness; it does not itself assert equality — an external fixture diffs input vs. re-emitted output.

**Is `toJson ∘ from_json` the identity?** Conditionally yes, **up to canonical form**:

- **Version normalization (v1→v2).** A v1 input is loaded via `fromJsonv1` but re-emitted as v2 (the writer hardcodes `version = 2` and threads 2 → `toJsonv2`). Round-tripping a v1 document **normalizes it to v2**: the `version` field and every pelican affine expr (`addrs` / `address_expr` / `bank_expr` / `order` / unroll predicates) change encoding. This is lossy-for-bytes but semantics-preserving — the affine tree is identical, only the wire form is canonicalized. A v2 input round-trips byte-stably (modulo key order).
- **Skip-default is round-trip-safe.** A field omitted at default on emit ([§4](#4-skip-default--the-inline-per-field-gate)) is restored to that same default by the loader, so absence ⇄ default is a fixpoint. An input that redundantly spells a default value is dropped on re-emit (canonicalization) — a benign diff.
- **Key order is irrelevant to the bytes.** `nlohmann`'s `std::map` sorts object keys lexicographically on `dump()`; the §2 / §6 emission order does **not** determine output position — only which keys exist. **Array** order (functions / instructions / operands) *is* preserved (vector order), with main-first for the function / block containers.
- **What is not byte-preserved:** the numeric `version` (v1→2), the pelican v1 wire encoding (→v2), redundant default-valued keys (dropped), and any transient/derived state never serialized (resolved pointer caches — only names are written and re-resolved on load). Everything name-addressable and every non-default field is preserved.

**Symmetry sketch.** For each key K with writer W and loader R: `R(W(x)) = x` on non-default `x`, and W omits K ⇔ `x == default` ⇔ R restores default. The pair {writer = §2/§3/§5, loader} is key-for-key symmetric (same spellings, same name-keyed refs, same nesting) **except** the version field, which is read-any / write-2. Hence `toJson ∘ from_json` is the identity on the **canonical** (v2, default-elided) representative of each equivalence class. [CONFIRMED — harness sequence; STRONG — the symmetry argument follows from the per-key write/read pairs.]

---

## 8. Adversarial self-verification

The five strongest claims, re-checked against the cp310 binary:

1. **19-key header order.** CONFIRMED. The `operator[]("k")` call sites in the decompiled `Instruction::toJson_0x2e2ea0.c` appear in source order: `origin`(335), `opcode`(353), `engine`(370), `engine_id`(396), `scheduled_start`(423), `scheduled_end`(450), `optin_passes`(462), `dependencies`(554), `loop_carried_dependencies`(648), `unroll_dependencies`(673), `sync_info`(1010), `ins`(1032), `indirection_ins`(1055), `outs`(1077), `debug`(1095), `loopnest`(1257), `predicates`(1283), `order`(1409), `dma_trigger_debug_update_semaphore_id`(1454). Exactly the 19 keys, in order. (Caveat: source order ≠ on-wire order; `nlohmann` sorts keys on `dump`, §7.)
2. **Skip-default mechanism.** CONFIRMED. `InstDMA::toJson` @ `0x4363d0`: `queue` gated by `test rsi,rsi; jz` (null), `dge_type` by `cmp dword [rbx+0xF8],3; jnz` (default ordinal 3), `duplicate` by `cmp byte [rbx+0x100],0` (MaybeAffine present flag). Three distinct inline gate shapes, no shared helper.
3. **Always-v2.** CONFIRMED. `Module::to_json` @ `0x48ab00`: `LOBYTE(v430)=5; v431=2; root["version"]=…` — literal 2, `number_unsigned`. Pelican gate `QuasiAffineExpr::toJson` @ `0x3bbda0` dispatches on a caller-supplied `edx`; the module path always supplies 2 → `toJsonv2`.
4. **ICF merging.** CONFIRMED. `nm -DC libBIR.so | grep '^…434e30 ' | grep -c toJson` = **28** distinct `Inst*::toJson` symbols at one address; `InstCompareAndBranch` alone at `0x434e40`. 115 distinct `Inst*::toJson` leaf symbols total.
5. **Round-trip identity-up-to-canonical.** CONFIRMED for the harness sequence (`bir_roundtrip` @ `0x427dc0`: `from_json`→`to_json`→`dump(indent=2)`); STRONG for the symmetry property (it follows from the per-key write/read pairing plus the v1→v2 write-only normalization, not from an in-binary equality assertion — the harness leaves equality to an external diff).

**Re-verify ceiling.** The header order, the three DMA gates, the v2 stamp, the pelican version gate, the Argument kind switch, the matmul 13-key order, and the ICF count are all byte-anchored (CONFIRMED). The exhaustive per-key skip-default gate set **beyond** matmul / DMA / Activation was not dumped op-by-op — the *pattern* (null / empty / `==default-ordinal` / MaybeAffine-no-value) is confirmed and generalizes, but a complete per-op gate table is loader-schema territory ([BIR-JSON Record Schema](json-schema.md)). The `BasicBlockHolder` container @ `0x254f70` and `MemoryLocationSet::toJson` @ `0x3481a0` failed Hex-Rays; their key rosters are taken from disasm string-xrefs (CONFIRMED for keys) but not all per-key gates were transcribed. The W8/W9 attribute-bag membership is STRONG (rosters cross-checked against the container model), not byte-dumped on this page.

## See also

- [BIR JSON Loader](json-loader.md) — the two-pass `from_json` reader this page inverts (7.12)
- [BIR-JSON Record Schema](json-schema.md) — the full per-op key sets and sub-records (7.14)
- [BIR-JSON Versioning](json-versioning.md) — the v1/v2 split and the pelican `toJsonv1`/`toJsonv2` divergence (7.15)
- [The bir::Instruction Base Struct](instruction-base.md) — the struct backing the 19 header offsets
- [BIR Value Model](value-model.md) — Argument / AccessPattern / Immediate / Register
- [MemoryLocation / Storage](memory-location.md) — the tensor placement records referenced by name
- [instabrew / brewer.py Generator](brewer-generator.md) — why writer and loader keys agree by construction
- [Op-Family Enums & the L1/L2/L3 Crosswalk](op-family-enums.md) — why enums serialize as names
