# BIR Value Model — Argument, AccessPattern, Immediate, Register

> *All symbols, addresses, struct offsets, and vtable VAs on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310), library `neuronxcc/starfish/lib/libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`, ELF64, stripped `.symtab`/rich `.dynsym`). `VA == file offset` for `.text` (base `0x1820c0`) and `.rodata` (base `0x708000`); `.data.rel.ro` is `VMA − 0x1000`. The cp311/cp312 wheels share the ABI but drift the VAs.*

## Abstract

Every operand a BIR `Instruction` references — a tensor access, a scalar literal, a register lane — is a heap-allocated **`bir::Argument`** subclass object, not an inline field and not an index into a flat array. An instruction owns three intrusive circular doubly-linked lists keyed by *role* (inputs / indirection / outputs), and each node on those lists is a polymorphic `Argument` whose **C++ class encodes what it binds to**. The class is selected on the wire by a `"kind"` string (eight live spellings) and recorded in-memory as an `ArgumentKind` integer at `Argument+0x18`. This page is the in-memory **value model**: the `Argument` base header, the three concrete families that derive it (`AccessPattern`, the `ArgValue`/immediate family, and `RegisterArgument`), their byte layouts, and the single `createFromJson` dispatch that builds them.

The shape will be familiar to anyone who has read an LLVM `Value`/`Use` graph, with two deliberate departures. First, the **discriminator is doubled**: the wire carries a `"kind"` *string*, but the in-memory object carries an `ArgumentKind` *ordinal* set by the leaf constructor — there is no `string2ArgumentKind`, so the two are bridged only inside `createFromJson`. Second, the use-def edge is **not** a separate `Use` list: an `Argument` registers itself directly as a reader or writer on its bound `StorageBase` (a `MemoryLocation`, `MemoryLocationSet`, or `Register`) via `setLocation`, and the output-vs-input distinction that drives reader-vs-writer comes from the operand's *role list*, not from the operand class. Immediates bind to nothing and contribute no edge.

Three things a reimplementer must get right and that the naive reading gets wrong: (1) `AccessPattern` is **multiple-inheritance** — `bir::Argument` at offset 0 plus a `logging::SrcHandle` vptr at `+0x48` — so the AP's own data resumes at `+0x50`, not `+0x48`; (2) `ImmediateArray` derives `Argument` *directly*, skipping the `ArgValue` shim that its siblings use; (3) `RegisterSet` (kind 12) is a declared, RTTI-present class whose constructor is a hard `assert(false)` — the enumerator exists but no live `RegisterSet` operand can be built, and there is no `"register_set"` wire spelling. This page documents the AP family at the *value-object* level only; the on-wire silicon descriptors (`ADDR4` / `TENSOR4D` / `MXMEM_PATTERN1D`) are Part 2 and the codegen-side abstract AP builders are 7.22 — both cross-referenced, not duplicated.

For reimplementation, the contract is:

- The `Argument` base header (8 fields, `0x48` bytes) and the `ArgumentKind` discriminator `{1,2,3,6,7,8,11,12}` with its five reserved gaps.
- The `createFromJson` 8-way string dispatch, the per-leaf allocation sizes, and how role (`InstructionArgumentType`) threads the node onto one of three lists.
- The three value families' byte layouts: `AccessPattern` (MI base + two `SmallVector<APPair,4>`), the immediate family (`ImmediateValue`/`SymbolicImmediateValue`/`ImmediateArray`), and the register family (`RegisterAccess`, plus the `RegisterAccessPattern` dynamic AP).
- The use-def model: `setLocation` → `registerReader`/`registerWriter` on the bound `StorageBase`, and why immediates skip it.

| | |
|---|---|
| **Polymorphic root** | `bir::Argument` (introduces the vptr; RTTI `_ZTI` @ `0x8fbc88`, vtable `0x8fbcf0`) |
| **Base ctor** | `0x2320c0` — `Argument(this, ArgumentKind, Instruction*)`; throws on NULL parent |
| **Discriminator** | `ArgumentKind` @ `Argument+0x18` (int); live `{1,2,3,6,7,8,11,12}`; gaps `{0,4,5,9,10}` |
| **Dispatch** | `bir::Argument::createFromJson` `0x235500` (thunk `0x17e760`) — 8 `"kind"` spellings |
| **Role enum** | `bir::InstructionArgumentType` `{0 Argument, 1 IndirectionArgument, 2 Output}` |
| **Role lists** | `Instruction+0xA0` / `+0xB0` / `+0xC0` (intrusive circular, insertion-ordered) |
| **Concrete leaves** | 8: `{Physical,Symbolic,Register}AccessPattern`, `Immediate{Value,Array}`, `SymbolicImmediateValue`, `RegisterAccess` (+ `RegisterSet` stub) |

---

## The Argument Hierarchy

### Purpose

`bir::Argument` is the abstract base of the entire operand/value family — one of the four independent polymorphic roots of the non-`Instruction` BIR types (the others being `StorageBase`, `pelican::Expr`, and `sync::SyncRef`). It contributes the vtable (its sole RTTI base, `llvm::ilist_node<Argument>`, has no vtable of its own, so `Argument` *introduces* the polymorphism), the intrusive list links that thread an operand onto its instruction, the `ArgumentKind` discriminator, the back-pointer to the parent instruction, and the `StorageBase*` location that carries the use-def binding for the kinds that have one.

### Class Tree

Three intermediate bases sit under `Argument`, each opening a family; eight concrete leaves hang off them. Every edge below is an `_ZTI` base relocation:

```text
bir::Argument                       (ABSTRACT root — introduces vptr; vtable 0x8fbcf0)
 ├─ bir::AccessPattern              (ABSTRACT — 2 __cxa_pure_virtual; __vmi MI:
 │     │                             Argument@0 + logging::SrcHandle@0x48)
 │     ├─ PhysicalAccessPattern      kind 1   "physical_ap"           sizeof 0x1E0
 │     ├─ SymbolicAccessPattern      kind 2   "symbolic_ap"/"symbolic_pwap"  0x1B8
 │     └─ RegisterAccessPattern      kind 3   "register_ap"           sizeof 0x118
 ├─ bir::ArgValue                   (ABSTRACT shim — adds no field/virtual)
 │     └─ ImmediateValue             kind 6   "imm_value"             sizeof 0x50
 │          └─ SymbolicImmediateValue kind 7  "symbolic_imm_value"    sizeof 0xD0
 ├─ bir::ImmediateArray             kind 8   "imm_array"   sizeof 0x98  (⚠ derives Argument DIRECT)
 └─ bir::RegisterArgument           (ABSTRACT shim)
       ├─ RegisterAccess             kind 11  "register_access"       (no own field)
       └─ RegisterSet                kind 12  (STUB — ctor assert(false); no wire spelling)
```

> **QUIRK —** `createFromJson`/`toJson` are **vtable slots** in the `Argument` family (slots 4/5 on the concrete leaves), unlike the `Instruction` family where `createFromJson` is a static factory and not virtual. A reimplementer who models all BIR deserialisers uniformly as static factories will mis-shape the operand layer: an `Argument` round-trips through its *own* vtable, dispatched by the live object's class, which is exactly why the base `Argument::toJson` (`0x2352f0`) is a thin virtual thunk that lands in the leaf.

### The ArgumentKind Discriminator

`ArgumentKind` is an `int` at `Argument+0x18`, written by the base constructor from the ordinal the leaf passes up the chain. The `ArgumentKind2string` body (`0x233ae0`, `Argument.cpp:303`) handles exactly eight ordinals and asserts on the rest:

| Ord | Class | How the ordinal is set | Confidence |
|---|---|---|---|
| 0 | *(reserved)* | hits assert in 2string | CERTAIN (gap) |
| 1 | `PhysicalAccessPattern` | ctor `@0x3ae620` → `AccessPattern(this, 1, inst)` | CERTAIN |
| 2 | `SymbolicAccessPattern` | ctor `@0x3d4460` → `AccessPattern(this, 2, inst)` | CERTAIN |
| 3 | `RegisterAccessPattern` | ctor `@0x3c4fd0` → `AccessPattern(this, 3, inst)` | CERTAIN |
| 4 | *(reserved)* | hits assert | CERTAIN (gap) |
| 5 | *(reserved)* | hits assert | CERTAIN (gap) |
| 6 | `ImmediateValue` | ctor `@0x29ed00` → `ArgValue(this, 6, inst)` | CERTAIN |
| 7 | `SymbolicImmediateValue` | ctor `@0x29f010` → `ImmediateValue(this, inst, 7)` | CERTAIN |
| 8 | `ImmediateArray` | ctor `@0x29f890` → `Argument(this, 8, inst)` | CERTAIN |
| 9 | *(reserved)* | hits assert | CERTAIN (gap) |
| 10 | *(reserved)* | hits assert | CERTAIN (gap) |
| 11 | `RegisterAccess` | ctor `@0x3c7de0` → `RegisterArgument(this, 11, inst)` | CERTAIN |
| 12 | `RegisterSet` | ctor `@0x3c7f80` → `RegisterArgument(this, 12, inst)` (STUB) | CERTAIN |

> **NOTE —** there is no `string2ArgumentKind`. The ordinal is **never** serialised as an enum name; the wire discriminator is the `"kind"` *string* (§dispatch below). The ordinal exists only in-memory, set by the leaf ctor, read by the abstract-AP guards (e.g. `tryGetLocation` only calls the location vtable slot when `kind-1 <= 2`, i.e. for AP kinds 1/2/3; `0x232210`). The five gap ordinals `{0,4,5,9,10}` are reserved enumerators with no class and no string — historical or forward-reserved, unrecoverable from this build.

### Base Struct — `bir::Argument` (`0x48` bytes)

Byte-exact from the constructor at `0x2320c0` — every store is witnessed in the decompiled body. Subclass fields begin at `+0x48`.

| Field | Off | Type | Meaning | Conf |
|---|---|---|---|---|
| vtable | `+0x00` | qword | `off_8FBD00` (base) — overwritten per leaf | CERTAIN |
| list.next | `+0x08` | ptr | intrusive role-list next (`=0` at ctor, live once threaded) | CERTAIN |
| list.prev | `+0x10` | ptr | intrusive role-list prev (`=0` at ctor) | CERTAIN |
| kind | `+0x18` | int | `ArgumentKind` discriminator `{1,2,3,6,7,8,11,12}` | CERTAIN |
| parent | `+0x20` | `Instruction*` | owning instruction — ctor **throws** `runtime_error("NULL Parent Instruction for Argument!")` if NULL | CERTAIN |
| uniqueId | `+0x28` | int | `NamedObject<Instruction,BasicBlock>::getUniqueId(parent)` | CERTAIN |
| isOutput/isWriter | `+0x2C` | byte | a3 of `setLocation` — 1 ⇒ writes its storage | CERTAIN |
| dtype | `+0x30` | int | per-operand `Dtype` (set by `setType`, vtable slot +16) | CERTAIN |
| location | `+0x38` | `StorageBase*` | bound `MemoryLocation`/`Set`/`Register` (use-def); `=0` at ctor | CERTAIN |
| isActive/registered | `+0x40` | byte | a4 of `setLocation` — 1 ⇒ registered on storage | CERTAIN |

```c
// bir::Argument::Argument(this, kind, parent)            // 0x2320c0 — byte-exact
this[+0x08] = 0;  this[+0x10] = 0;        // list links empty
this[+0x00] = off_8FBD00;                 // base vtable
this[+0x18] = kind;                       // ArgumentKind ordinal (leaf-supplied)
this[+0x20] = parent;                     // owning Instruction
this[+0x2C] = 0; this[+0x38] = 0; this[+0x40] = 0;   // isOutput / location / active
if (parent == NULL)                       // NO operand may be parentless
    throw runtime_error("NULL Parent Instruction for Argument!");
this[+0x28] = parent->NamedObject::getUniqueId();      // stable per-instruction id
```

> **NOTE —** `dtype` at `+0x30` sits inside the base footprint (between `uniqueId` and `location`) but is *not* written by the base ctor — it is set later by `Argument::setType` (`0x20fde0`, `*(int*)(this+0x30) = dtype`), the vtable-slot-16 virtual. Value, register, and AP operands all carry a per-operand element `Dtype` here on the wire `"dtype"` key; the immediate family additionally uses it to width-select its payload (§immediates).

---

## The `createFromJson` Dispatch

### Purpose

`bir::Argument::createFromJson(Instruction*, InstructionArgumentType, const json&)` (`0x235500`, thunk `0x17e760`) is the single deserialiser entry point: it reads the `"kind"` string, builds the matching leaf, fills its fields, and threads it onto the role list selected by the `InstructionArgumentType` argument. The per-instruction `readFieldsFromJson` calls it once per operand record, passing `Argument` for inputs, `Output` for results, `IndirectionArgument` for index operands — this is the bridge from the JSON `args`/`outputs`/`indirection` arrays to the three in-memory lists.

### The Role Enum and the Three Lists

`bir::InstructionArgumentType` (`0x2e4a80`) has exactly three values; an `Instruction` owns one intrusive circular doubly-linked list head per value (initialised empty, `next==prev==&head`):

| Role | Ord | List head | qword idx | JSON section |
|---|---|---|---|---|
| `Argument` | 0 | `Instruction+0xA0` | `+20` | `args` (inputs) |
| `IndirectionArgument` | 1 | `Instruction+0xB0` | `+22` | indirection / gather-scatter index |
| `Output` | 2 | `Instruction+0xC0` | `+24` | `outputs` (results) |

The attach routine `addArgumentIndirectionArgumentOrOutput<T>(inst, role)` (template; `T=PhysicalAccessPattern` body `@0x237780`) is `new T(inst)` followed by a tail-append onto the head picked by `role` — so **operand order within a list is insertion order**, and inputs / outputs / indirection-args are physically *separate* lists, never one interleaved array.

### Algorithm

```c
function Argument::createFromJson(inst, role, j):          // 0x235500
    kind = j["kind"];                                       // wire discriminator (string)
    assert(kind in {                                        // Argument.cpp:101 — 8 spellings
        "physical_ap","symbolic_ap","symbolic_pwap","register_ap",
        "imm_value","symbolic_imm_value","imm_array","register_access"
    }) || fail("Unsupported argument kind");

    switch (kind):
      // ---- AccessPattern family: honour role, set use-def edge ----
      case "physical_ap":                                   // kind 1
          ap = add<PhysicalAccessPattern>(inst, role);
          loc = Function/Module.getMemoryLocationByName(j["memref"]);
          ap.setLocation(loc, /*isOutput=*/ role==Output, /*active=*/1);
      case "symbolic_ap": case "symbolic_pwap":             // kind 2 (two spellings, one class)
          ap = add<SymbolicAccessPattern>(inst, role);      // new(0x1B8)
          ap.setLocation(getMemoryLocationSetByName(j["memsetref"]), role==Output, 1);
      case "register_ap":                                   // kind 3
          ap = add<RegisterAccessPattern>(inst, role);      // new(0x118)
          ap.setLocation(getMemoryLocationSetByName(j["memsetref"]));
          ap.setRegLocation(getRegisterByName(j["regref"]));         //  +0xE8
          if (j has "reg_ap_offset")   ap.setRegAPOffset(...);       //  +0xF0 (Argument.cpp:165)
          if (j has "reg_sub_tensor_id") ap[+0xF8] = getRegisterByName(...);  // Argument.cpp:171

      // ---- Immediate family: IGNORE role, always plain input (+0xA0) ----
      case "imm_value":                                     // kind 6
          v = operator new(0x50);  ImmediateValue::createFromJson(v, inst, j);
          thread_onto(inst+0xA0, v);                        // inline, never Output/Indirection
      case "symbolic_imm_value":                            // kind 7
          v = operator new(0xD0);  SymbolicImmediateValue::createFromJson(v, inst, j);
          thread_onto(inst+0xA0, v);
      case "imm_array":                                     // kind 8
          v = operator new(0x98);  ImmediateArray::createFromJson(v, inst, j);
          thread_onto(inst+0xA0, v);

      // ---- Register family ----
      case "register_access":                               // kind 11
          ra = add<RegisterAccess>(inst, role);
          ra.from_json(j);                                  // reads "dtype" only
          ra.setLocation(getRegisterByName(j["regref"]), role==Output, 1);   // isa<Register>
    return ref;
```

> **GOTCHA —** the three immediate kinds (6/7/8) **ignore** the `role` argument entirely. They thread directly onto the input list at `Instruction+0xA0` regardless of the JSON section they appeared in — an immediate is always a plain `Argument`, never an `Output` or `IndirectionArgument`. Only the AP and register kinds forward `role==Output` into `setLocation` to register as a *writer*. A reimplementer who routes every operand through the role-selected list will silently mis-place an immediate that the producer emitted in an `outputs` array.

The dispatch-string assert and the four `operator new` sizes are byte-witnessed in the decompiled body: line 235 carries the verbatim 8-spelling assert string, and the `new(0x50)` / `new(0xD0)` / `new(0x98)` / `new(0x1B8)` allocations appear at lines 748 / 758 / 768 / 881.

---

## The AccessPattern Family

### Purpose

An `AccessPattern` is the operand that binds an instruction to a *tensor access* — a `(step, num)`-pair stride descriptor over a `MemoryLocation` (physical) or `MemoryLocationSet` (symbolic / register-offset), plus a logical shape. It is the abstract base of the three AP leaves; it is hard-**abstract**, carrying two `__cxa_pure_virtual` slots (`getLocation`, `getMemoryLocationSet`) that each concrete leaf fills.

### Multiple Inheritance and the `+0x50` Data Start

`AccessPattern` is a `__vmi` (multiple-inheritance) class with **two** public bases (`__vmi_class_type_info` @ `0x8fb348`, base_count 2):

```text
bir::AccessPattern : public bir::Argument        (primary base, offset 0)
                     public logging::SrcHandle    (secondary base, offset +0x48 → public)
```

The `logging::SrcHandle` subobject is an 8-byte vptr at `+0x48` (a source-location/diagnostic handle, no further data mapped here). Consequently the AP's *own* data resumes at **`+0x50`**, not `+0x48`. The `_ZThn72_` (`thn72 = 0x48`) non-virtual dtor thunk confirms the `0x48` subobject offset.

> **GOTCHA — the AP data start is `+0x50`, not `+0x48`.** For the immediate and register families, which are single-inheritance with no `SrcHandle`, subclass fields do begin at `+0x48` immediately after the `Argument` header. The AP family breaks that pattern: its `+0x48` slot is the `logging::SrcHandle` secondary-base vptr (pinned by the typeinfo relocs), and the AP's first own field, `Pattern.data`, sits at `+0x50`. Reading an AP as a flat `0x48`-byte header shifts every subsequent field by eight bytes.

### Abstract Base Struct — `bir::AccessPattern` (`0xD0` bytes)

Constructor `0x2019b0` (`AccessPattern(this, ArgumentKind, Instruction*)`). Two `llvm::SmallVector<…,4>` with inline storage, both initialised `{size=0, cap=4}` (the ctor stores `0x400000000` = `cap 4 << 32 | size 0`):

| Field | Off | Type | Meaning | Conf |
|---|---|---|---|---|
| *(Argument base)* | `+0x00` | `0x48` | §base — `kind`/`parent`/`dtype`@`+0x30`/`location`@`+0x38` | CERTAIN |
| `SrcHandle.vptr` | `+0x48` | qword | `logging::SrcHandle` secondary-base vptr | CERTAIN |
| `Pattern.data` | `+0x50` | `APPair*` | the `(step,num)` stride vector data ptr (inline `→ +0x60`) | CERTAIN |
| `Pattern.size` | `+0x58` | uint32 | num_dims (1..4) | CERTAIN |
| `Pattern.cap` | `+0x5C` | uint32 | SmallVector cap (init 4) | CERTAIN |
| `Pattern.inline[4]` | `+0x60` | `APPair[4]` (`0x40`) | inline `(step,num)` buffer, canonical `[W,Z,Y,X]` | CERTAIN |
| `access_shape.data` | `+0xA0` | `uint64*` | logical per-dim shape vector (inline `→ +0xB0`) | CERTAIN |
| `access_shape.size`/`cap` | `+0xA8` | uint32×2 | shape length / cap 4 | CERTAIN |
| `access_shape.inline[4]` | `+0xB0` | `uint64[4]` (`0x20`) | inline shape buffer | CERTAIN |
| *(base end)* | `+0xD0` | — | leaves resume here | CERTAIN |

**`APPair`** is a 16-byte POD (no RTTI): `step` @ `+0x00` (signed int64), `num` @ `+0x08` (int64). The canonical axis order is `[W,Z,Y,X] = Pattern[0..3]`, with `Pattern[0]` the outermost/partition dim — `getStepBytesPerHighestDim` (`0x203170`) reads `Pattern[0].step` and asserts `>= 0`. `setPattern` (`0x211d40`) copies an external `SmallVector<APPair>` in, striding 16 bytes per entry.

### The Three Leaves

```text
PhysicalAccessPattern  kind 1  "physical_ap"   0x1E0  → MemoryLocation (compile-time AP)
SymbolicAccessPattern  kind 2  "symbolic_ap"   0x1B8  → MemoryLocationSet (pelican::Expr step/num)
RegisterAccessPattern  kind 3  "register_ap"   0x118  → MemoryLocationSet + Register (dynamic AP)
```

**`PhysicalAccessPattern` (`0x1E0`, ctor `0x3ae620`)** appends to the `0xD0` base: `offset` @ `+0xD0` (uint64, the start byte-offset → wire `"offset"`; setter `0x3a2aa0`), a `+0xD8..+0x1D0` cached-address-interval region (derived state, memset 0 in ctor, guarded by the `+0x1D0` spinlock), and a `DynamicAPINFO*` @ `+0x1D8` (`=0` unless the AP is an indirect/gather access → wire `"dynamic_ap_info"`). Its `to_json` (`0x4869b0`) emits `kind`/`ap`/`offset`/`dtype`/`memref`/`memsetref`/`access_shape`/`dynamic_ap_info`/`memref_owner_func`.

**`SymbolicAccessPattern` (`0x1B8`, ctor `0x3d4460`)** carries the symbolic step/num as `pelican::QuasiAffineExpr` trees (32-byte wrappers: `RefPtr<pelican::Expr>` @ `+0x00`, `SmallVector<LoopAxis*>` @ `+0x08`) rather than integers. Its persistent fields: `tensor_indirect_arg_id` @ `+0xD0` (int, init `0xFFFFFFFF`), `num_tensor_indirect_indices` @ `+0xD4`, and the `addrs` vector of `QuasiAffineExpr` @ `+0xE0` (appended by `addAffineExpr` `0x3d6640`). Fields `+0x100`/`+0x130`/`+0x178` are evaluation caches (predicates / shard-in APs / evaluated concrete APs — derived, not serialised). Two wire spellings (`"symbolic_ap"`, `"symbolic_pwap"`) map to this one class.

**`RegisterAccessPattern` (`0x118`, ctor `0x3c4fd0`)** is a *dynamic* AP: the start address or a per-dim offset is supplied at runtime by a `Register`, not a compile-time constant. Concrete fields and wire keys, read from `to_json` @ `0x487090`:

| Field | Off | Type | Wire key | Conf |
|---|---|---|---|---|
| `regLoc` | `+0xE8` | `Register*` | `"regref"` (setter `setRegLocation` `0x3c5040`) | CERTAIN |
| `RegAPOffset_` | `+0xF0` | `Register*` | `"reg_ap_offset"` (if non-0; setter `0x3c6520`, asserts `numPhysicalRegs` match) | CERTAIN |
| `reg_sub_tensor_id` | `+0xF8` | `Register*` | `"reg_sub_tensor_id"` (if non-0) | CERTAIN |
| `const_ap_offset` | `+0x100` | qword | `"const_ap_offset"` (static fall-through; init 0) | CERTAIN |
| `const_sub_tensor_id` | `+0x108` | int | `"const_sub_tensor_id"` (init -1) | CERTAIN |
| `is_regloc_offset` | `+0x114` | byte | `"is_regloc_offset"` — register holds offset directly | CERTAIN |

> **NOTE —** this page maps the AP value-object only. The *abstract* `(stride,size)` AP that the Penguin→BIR codegen builds before it becomes one of these C++ objects is [BirCodeGenLoop Access-Pattern Builders](../nki/bircodegen-ap.md) (7.22); the *on-wire* silicon descriptors the encoder lowers these to are [ADDR4](../isa/addr4.md), [Tensor4D / MemPattern4D](../isa/tensor4d-mempattern4d.md), and [Indirect Descriptors](../isa/indirect-descriptors.md) (Part 2). The `pelican::QuasiAffineExpr`/`Expr` internals held by symbolic APs are [Affine Expression Algebra](../penguin/affine-expr-algebra.md).

---

## The Immediate / Value Family

### Purpose

The immediate family carries **inline** literals and symbolic expressions — operands that bind to nothing external (`location` @ `+0x38` stays null, no use-def edge). `ArgValue` is an abstract shim (a 4-slot vtable that adds no field and no virtual) whose only purpose is to group `ImmediateValue` and its symbolic subclass; `ImmediateArray` deliberately bypasses it.

### `ImmediateValue` (kind 6, `0x50`, ctor `0x29ed00`)

A scalar literal: `dtype` @ `+0x30` (Argument base slot), an 8-byte value union @ `+0x48`. Both fields are pinned by the symmetric `createFromJson` (`0x2a7000`, read) / `dataToJson` (`0x2a2c10`, write), which share one `Dtype` switch. The wire `"value"` width is keyed by the `Dtype` ordinal:

```text
Dtype ordinal → storage width @ +0x48
  0  uint8  / 1  int8                    → byte
  0xA uint16 / 0xC bf16 / 0xD fp16       → ushort      (0xB int16 → short)
  0xE uint32 / 0xF int32                 → dword
  0x10 float32                           → dword via SerializableFloat
  0x12 uint64 / 0x13 int64               → qword
  DEFAULT (fp8/fp4 variants 3–9, 0x11 float32r) → ASSERT "unsupported dtype"
                                          (ImmediateValue.cpp:188)
```

> **GOTCHA —** immediates carry **only** standard int/float scalars on the wire. The fp8/fp4 variants (Dtype 3–9) and `float32r` (`0x11`) hit a hard assert in both `createFromJson` and `dataToJson` (`ImmediateValue.cpp:188`/`:226`). `ImmediateValue::castTo` (`0x2a0ac0`) *can* internally convert to fp8 dtypes, but the result is never wire-deserialised — a reimplementer must not emit an `imm_value` with one of those dtypes.

### `SymbolicImmediateValue` (kind 7, `0xD0`, ctor `0x29f010`)

A 3-level chain (`→ ImmediateValue → ArgValue → Argument`). It carries a `pelican::QuasiAffineExpr` (a symbolic affine expression) at `+0x68`, built from `Instruction::getPelicanContext`, alongside the folded scalar inherited from `ImmediateValue`. The expr's coefficient set heads are self-referential (`+0xB8`/`+0xC0 = this+168`, an empty intrusive `AffineIdx→coeff` map at ctor). The kind-7 ordinal is byte-witnessed: the ctor calls `ImmediateValue::ImmediateValue(this, inst, 7)`.

### `ImmediateArray` (kind 8, `0x98`, ctor `0x29f890`)

> **QUIRK —** `ImmediateArray` derives `bir::Argument` **directly**, not `ArgValue` — confirmed both by the ctor (`Argument::Argument(this, 8, inst)`) and by the `_ZTI` reloc (`__si → Argument 0x8fbc88`, not `ArgValue`). Its siblings `ImmediateValue`/`SymbolicImmediateValue` go through the `ArgValue` shim; `ImmediateArray` skips it. A reimplementer modeling the family as a clean `ArgValue ⊃ {scalar, array, symbolic}` will get the vtable shape wrong.

It is a `SmallVector<int64,8>` where **every** element is widened to 8 bytes regardless of `dtype`:

| Field | Off | Type | Wire key | Conf |
|---|---|---|---|---|
| *(Argument base)* | `+0x00` | `0x48` | `kind="imm_array"` | CERTAIN |
| `dtype` | `+0x30` | int | `"dtype"` | CERTAIN |
| `vec.data` | `+0x48` | `int64*` | `"value_array"` elems (inline `→ +0x58`) | CERTAIN |
| `vec.size` | `+0x50` | uint32 | element count | CERTAIN |
| `vec.cap` | `+0x54` | uint32 | inline cap = 8 | CERTAIN |
| `vec.inline[8]` | `+0x58` | `int64[8]` (`0x40`) | inline buffer | CERTAIN |

`createFromJson` (`0x2a6080`) reads `"dtype"` then a required `"value_array"` (missing → `ImmediateArray.cpp` error), typing each element by `dtype` and storing it widened to a qword; growth via `sub_67B350(…, 8)` (elem size 8). It shares the same int/float-only dtype gate as `ImmediateValue`.

---

## The Register Family

### Purpose

The register family references a `bir::Register` — a `StorageBase` subclass (a register *definition*, scoped to the `Function` and referenced by name, **not** itself an `Argument`). `RegisterArgument` is an abstract shim; `RegisterAccess` is the only live leaf.

### `RegisterAccess` (kind 11, ctor `0x3c7de0`)

A scalar register reference with **no** new fields beyond the `Argument` base: `dtype` @ `+0x30`, the bound `Register*` @ `+0x38` (the base `location`). Its own `createFromJson` (`0x3c96f0`) reads only `"dtype"`; the outer `Argument::createFromJson` "register_access" branch does the name resolution — `getRegisterByName(j["regref"])` then `setLocation(reg, isOut=role==Output, 1)`. `setLocation` (`0x3c78d0`) asserts `isa<Register>` (`StorageBase` class-tag @ `+0x110 == 2`); `getLocation` (`0x3c7e10`) re-asserts it.

| Field | Off | Type | Meaning | Conf |
|---|---|---|---|---|
| *(Argument base)* | `+0x00` | `0x48` | `kind=11`, vtable `off_8FFFC8` | CERTAIN |
| `dtype` | `+0x30` | int | wire `"dtype"` | CERTAIN |
| `regref` | `+0x38` | `Register*` | bound `Register` (the base `location`) | CERTAIN |

### `RegisterSet` (kind 12, ctor `0x3c7f80`) — STUB

> **GOTCHA —** `RegisterSet` is a declared, RTTI-present class (`_ZTI 0x8fff70`, vtable `0x900000`) whose constructor is a hard `__assert_fail("false", RegisterArg.cpp:0x4D)`. No live `RegisterSet` operand can be built, its vtable has **no** `createFromJson`/`toJson` slot, and `createFromJson` has **no** `"register_set"` wire spelling among its eight. The `ArgumentKind 12` enumerator exists, but the kind is dormant — treat it as RESERVED, like the gap ordinals `{0,4,5,9,10}`. A reimplementer should declare the class for ABI completeness but never instantiate it.

### The `Register` Definition (binding target)

`bir::Register` is a `StorageBase ⊃ Storage` leaf, created at function level (`Register::createFromJson` `0x3c4860`, keyed by name into the `Function`) and referenced by operands via `getRegisterByName`. Two fields matter to the value model: the `StorageBase` class-tag @ `+0x110` (`== 2` for `Register`, vs `1` for `MemoryLocationSet`) is the `isa<>` discriminator every register-operand guard checks; `numPhysicalRegs` @ `+0x18C` (setter `0x287010`) is read by `RegisterAccessPattern::setRegAPOffset`'s physical-reg-count match assert. The full `StorageBase`/`Register` layout is the storage strand, out of scope here.

---

## The Operand-Binding Model

The five rules that connect an instruction's operands to the values they reference:

1. **Storage / ownership.** Operand objects are heap-allocated `Argument` subclasses owned by the `Instruction` via three role-keyed intrusive circular lists (`+0xA0` input · `+0xB0` indirection · `+0xC0` output). `list.next`/`prev` live at `Argument+0x08`/`+0x10`; order within a list is insertion order.

2. **Value-class by `ArgumentKind`.** The class says what it binds to. AP leaves → a `MemoryLocation` (physical) or `MemoryLocationSet` (symbolic/register) via `location` @ `+0x38` plus a stride `Pattern` vector. `RegisterAccess`/`RegisterAccessPattern` → a `Register` (`+0x38`, or `+0xE8` for the reg-AP). The immediate kinds → an inline literal/expr, with `location` null.

3. **Use-def edge.** `Argument::setLocation` (`0x2318f0`) wires the operand into the storage's reader/writer set: if `isOutput`(`+0x2C`) → `StorageBase::registerWriter`, else `registerReader` (only when `active`, `+0x40`). This is the SSA-like def-use graph between operands and `MemoryLocation`s/`Register`s; rebind deregisters first. The output role (`role==2` → list `+0xC0`) drives `isOutput=true` → writer. **Immediates never call `setLocation`** → no def-use edge.

4. **Dtype.** Per-operand element type @ `Argument+0x30`, set via `setType` (vtable slot 16), carried on the wire `"dtype"` key by value, register, and AP operands.

5. **Wire ↔ C++.** The per-instruction `readFieldsFromJson` iterates the JSON `args`/`indirection`/`outputs` arrays, calling `Argument::createFromJson(inst, role, record)` once per operand; the `"kind"` string selects the leaf, the leaf fills its fields, the role threads it onto the matching list and (for AP/register) sets the use-def edge. Emit is the symmetric vtable-dispatched `toJson` per leaf (8 spellings).

---

## Evidence Summary

Byte-witnessed in the decompiled/disasm sidecars of `libBIR.so`:

- The `Argument` base ctor's eight field stores at `0x2320c0` (offsets `+0x08`/`+0x10`/`+0x18`/`+0x20`/`+0x28`/`+0x2C`/`+0x38`/`+0x40`) and the NULL-parent throw.
- The eight-spelling `"kind"` assert string verbatim and the four `operator new` sizes (`0x50`/`0xD0`/`0x98`/`0x1B8`) inside `createFromJson` (`0x235500`).
- The `ArgumentKind` discriminator ordinals and their leaf-ctor witnesses, including kind 7 via `ImmediateValue::ImmediateValue(this, inst, 7)`.
- The full RTTI inheritance graph — 12 typeinfos, the `AccessPattern` `__vmi` multiple inheritance, `ImmediateArray` deriving `Argument` directly, and `RegisterSet` having no factory — from the base relocations.

### Limits of this reading

The `RegisterAccessPattern` / `PhysicalAccessPattern` `to_json` wire-key→offset mappings come from the real serialiser bodies, but in this corpus the serialiser sidecars resolve to their thunk frame (`0x181570`), which tail-calls the un-named real bodies at `0x487090` / `0x4869b0` — the two-VA-frame artifact. Those keys are cross-checked by ctor⇄setter⇄serialiser agreement rather than re-read line by line.

Four things are inferred rather than pinned. The `APPair` internal field *names* are mapped as `{step:i64, num:i64}` from the stride and the `getStepBytesPerHighestDim` read, not name-witnessed. The `SymbolicImmediateValue` `QuasiAffineExpr` coefficient encoding is described structurally, not element by element. The `SymbolicAccessPattern` cache regions at `+0x100`/`+0x130`/`+0x178` are identified by role, with their element types [INFERRED]. The `logging::SrcHandle` subobject has no data mapped beyond its vptr. No NEFF or BIR-JSON fixture was available to round-trip any of this against a real serialised operand.

---

## Related Components

| Name | Relationship |
|---|---|
| `bir::Instruction` | Owns the three role-keyed operand lists; `createFromJson` is the caller of `Argument::createFromJson` |
| `bir::StorageBase` / `MemoryLocation` / `Register` | The binding targets; `setLocation` registers the use-def edge on them |
| `pelican::QuasiAffineExpr` / `Expr` | Held by `SymbolicAccessPattern.addrs` and `SymbolicImmediateValue`; the symbolic step/num/value |
| `DynamicAPINFO` | `PhysicalAccessPattern+0x1D8` indirection/gather companion (`"dynamic_ap_info"`) |

## Cross-References

- [BIR Instruction Base](instruction-base.md) — the `Instruction` that owns the operand lists; `InstructionArgumentType` role enum and the `+0xA0`/`+0xB0`/`+0xC0` heads
- [BirCodeGenLoop Access-Pattern Builders](../nki/bircodegen-ap.md) — 7.22, the abstract `(stride,size)` AP the codegen builds *before* it becomes one of these `bir::AccessPattern` objects
- [Affine Expression Algebra](../penguin/affine-expr-algebra.md) — the `pelican::Expr` / `QuasiAffineExpr` trees the symbolic AP and symbolic immediate carry
- [ADDR4](../isa/addr4.md) — the on-wire address descriptor a physical/register AP lowers to (Part 2)
- [Tensor4D / MemPattern4D Encoding](../isa/tensor4d-mempattern4d.md) — the on-wire stride/num silicon descriptor the AP feeds
- [Indirect Descriptors](../isa/indirect-descriptors.md) — the gather/scatter wire form `DynamicAPINFO` drives
