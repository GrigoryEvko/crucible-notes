# Codegen Sub-Encoders — Operand, Immediate, Register, and the Declared-Ordering Dependency Model

> *All symbols, addresses, and struct offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The codegen bodies live in `neuronxcc/starfish/lib/libwalrus.so` (`.text`/`.rodata` `VA == file offset`, base `0x62d660` / `0x1c72000`); the `bir::*` value/instruction templates they call resolve into `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`). The cp311/cp312 wheels share the ABI but drift every VA — treat each address as version-pinned. Provenance: static binary analysis, cross-checked against reports D-I21, D-I23, D-I01, D-I17, D-E14, D-E19, D-H21, D-H13.*

## Abstract

Every per-op KLR→BIR codegen leaf — `codegenNcMatMul`, `codegenTensorScalar`, `codegenReduce`, the 60-odd `codegen<Op>` members of `neuronxcc::backend::KlirToBirCodegen` — finishes the same way: it attaches operands, mints immediates, looks up or creates scalar registers, and (once, at the end of the whole kernel) wires dependency edges. None of that op-specific logic lives in the leaves. It lives in four shared *sub-encoders* the leaves call:

| Sub-encoder | Symbol @ VA (libwalrus) | What it produces |
|---|---|---|
| **`addOperandToInst`** | `0xf1be80` | one `bir::Argument` (Immediate **or** AP), appended to the instruction's **input** list at `Instruction+0xA0` |
| **`codegenImmediate`** | `0xf16980` | a packed `variant<float,int>` from a `klr::Immediate` — the literal-decoder feeding the above |
| **`codegenRegister`** | `0xf141e0` | one named `bir::Register` def (mint-or-reuse), single physical reg, on a chosen `EngineType` |
| **`codegenDependencyEdges`** | `0xf1e450` | the kernel's inter-instruction edges — a **replay** of a frontend-declared name adjacency list as `EdgeKind = Ordered(1)` edges |

This page reproduces each as annotated pseudocode keyed to real symbols, and pins the two invariants a reimplementer most needs: (1) immediates and AP-tensor operands are *always* plain inputs threaded at `+0xA0` — the role split (input vs output vs indirection) is decided by the leaf, never here; and (2) the dependency model at this boundary is **not** value-SSA and **not** memory aliasing — it is the frontend's declared program ordering, stamped as the weakest non-trivial edge kind, replayed verbatim, and left for the later analysis passes ([build_fdeps], the anti-dependency analyzer) to upgrade in place. The operand value-object shapes are owned by [the BIR Value Model](value-model.md) (7.4); the `Instruction+0xA0`/`+0xD0` layout and the `PointerIntPair` edge model by [the `bir::Instruction` Base Struct](instruction-base.md) (7.1); the symbolic AP these encoders consume by [BirCodeGenLoop Access-Pattern Builders](../nki/bircodegen-ap.md) (7.22).

## The two operand strands

A leaf binds an operand through exactly one of two entry points, chosen by the operand's **static type** in the `klr` op struct:

- **Strand A — positional tensor** (`out` / `in0` / `in1` / `index`): the field is a `klr::TensorRef`. The leaf calls `codegenTensorRef` (`0xf18b30`), gets back a resolved `{MemoryLocation*, SmallVector<APPair,4>, base-idx, Dtype}` bundle, and hands it to its own `InstBuilder::addXxx` as a positional `MemLoc + AP`. ~50 leaves take this path. `codegenTensorRef` is owned by the AP encoder page; this page touches it only where Strand B reuses it.
- **Strand B — scalar-or-tensor operand** (a `TensorScalar` scalar, a scan-init seed, a dropout RNG state, an activation bias): the field is a `klr::Operand` — a variant that may hold *either* an `Immediate` *or* a `TensorRef`. The leaf calls **`addOperandToInst`** (`0xf1be80`). 10 leaves take this path: `codegenTensorScalar`, `codegenTensorScalarReduce`, `codegenTensorScalarCumulative`, `codegenNcScalarTensorTensor`, `codegenTensorTensorScan`, `codegenActivate2`, `codegenExponential`, `codegenSelectReduce`, `codegenDropout`, `codegenRand2`. *(Caller set from the libwalrus callgraph; CONFIRMED.)*

`codegenOperand` (`0xf1bc80`) — the union folder Strand B uses — has **exactly one caller**: `addOperandToInst`. It is never used standalone. So the public Strand-B sub-encoder is `addOperandToInst`, which wraps `codegenOperand` and materializes the actual `bir::Argument`. *(Single-caller fact: CONFIRMED from callgraph.)*

> **NOTE — why immediates and scalar tensors share one helper.** A KLR scalar operand is a sum type. When it is a compile-time constant it lowers to an inline `ImmediateValue`; when it is a runtime tensor address it lowers to an AP-bound `PhysicalAccessPattern`. `addOperandToInst` is the single sink that handles both, which is why the per-op leaves never branch on "is this scalar constant?" themselves.

## `addOperandToInst` — append one operand as an input

`addOperandToInst(shared_ptr<klr::Operand>, bir::Instruction*)` @ `0xf1be80`. It runs in two steps: lower the operand to a tagged variant, then append by the tag.

```c
// neuronxcc::backend::KlirToBirCodegen::addOperandToInst  @ 0xf1be80
// a1=this (KlirToBirCodegen), op=&klr::Operand shared_ptr, inst=target bir::Instruction (=rbp)
void addOperandToInst(KlirToBirCodegen *this, shared_ptr<klr::Operand> op,
                      bir::Instruction *inst) {

  // STEP A — fold the klr::Operand to variant<float,int,InstArg>.
  //   codegenOperand @0xf1bc80 (call @0xf1bed6). The variant DISCRIMINANT is the
  //   byte at result+0x68 (loaded `movzx eax,[rsp+var_40]` @0xf1beea).
  Variant var = codegenOperand(op);
  uint8_t tag = var[0x68];                       // 0=float imm, 1=int imm, 2=tensor AP

  // STEP B — append by tag (switch @0xf1bef2). Every arm threads onto the INPUT list.
  switch (tag) {

    case 0: {  // FLOAT immediate  [loc_F1C000]
      bir::ImmediateValue *imm = (bir::ImmediateValue*)tc_new(0x50);   // 80-byte value obj
      bir::ImmediateValue::ImmediateValue(imm, inst, /*ArgumentKind=*/6);  // edx=6 @0xf1c00a
      //   ctor splices imm onto inst's INPUT arg-list head at inst+0xA0:
      //     mov rdx,[rbp+0A0h]; lea rcx,[rbp+0A0h]; …; mov [rbp+0A0h],rax  @0xf1c01d-4b
      bir::Argument::setType(imm, /*Dtype=*/16);    // movl $0x10,0x30(r14)  → float32
      *(uint32_t*)(imm + 0x48) = float_bits(var);   // value union @+0x48 (zero-extended)
      break;
    }

    case 1: {  // INT immediate  [loc_F1C108]
      bir::ImmediateValue *imm = (bir::ImmediateValue*)tc_new(0x50);
      bir::ImmediateValue::ImmediateValue(imm, inst, 6);                // edx=6 @0xf1c112
      bir::Argument::setType(imm, /*Dtype=*/15);    // movl $0xF,0x30(r14)  → int32
      *(int64_t*)(imm + 0x48) = sext_i32_to_i64(int_val(var));  // sign-extend @0xf1c17c-9d
      break;
    }

    case 2: {  // TENSOR (InstArg / AccessPattern)  [fall-through 0xf1bf0a]
      // unpack the InstArg bundle, then append a PhysicalAccessPattern INPUT bound
      // to the resolved MemoryLocation:
      bir::Instruction::addArgument<
          bir::PhysicalAccessPattern,        // ArgumentKind 1
          bir::MemoryLocation*,
          llvm::SmallVector<bir::APPair,4>,  // the stride list
          unsigned int,                      // base / elem index
          std::optional<bir::Dtype>          // per-operand dtype
        >(inst, var.memloc, var.ap, var.base_idx, var.dtype);   // call @0xf1bf96
      break;
    }

    default:  // tag not in {0,1,2} (incl. variant-valueless 0xFF reset state)
      throw std::runtime_error("Unsupported operand type encountered");  // loc_6D8CA6
  }
}
```

**The lowering inside `codegenOperand`** (`0xf1bc80`) folds the `klr::Operand` sum (kind `1 = Immediate`, kind `2 = TensorRef`; all others throw `"Unrecognized operand type encountered"`):

- kind 1 → `codegenImmediate(op.+8)` (§ below) returns a `variant<float,int>`; the scalar is stored at `result+0` and `result+0x68` is set to `0` (float-arm) or `1` (int-arm).
- kind 2 → `codegenTensorRef(op.+8)` (allow_dynamic = 0) returns the AP bundle; `result+0x68 = 2`.

*(Operand kind ordinals `{1=Immediate, 2=TensorRef}` are CONFIRMED from `klr::Operand_ser` @ `0xf46bc0`; only `klr::OperandImmWrapper` / `klr::OperandTileWrapper` exist in RTTI.)*

> **GOTCHA — the role is hardcoded INPUT here; the leaf owns outputs.** `addOperandToInst` *always* threads its operand onto the `Instruction+0xA0` input list. Immediates can never be outputs or indirection args; AP operands appended here are inputs by construction. The output AP (`Instruction+0xC0`) and the DMA indirection list (`Instruction+0xB0`) are written by the leaf's own `InstBuilder::addXxx(role=Output)` and indirection paths, not by this helper. A reimplementer who routes every operand through a role-selected list will silently mis-place a scalar the producer emitted in an `outputs` section. This matches the [value-model](value-model.md#the-createfromjson-dispatch)'s rule that the three immediate kinds (6/7/8) ignore the `role` argument entirely.

> **QUIRK — the int arm sign-extends; the float arm zero-extends.** Both write the payload at `ImmediateValue+0x48`, but the int case emits the canonical `movsxd; sar 63; and; or` 32→64 sign-extension (`@0xf1c17c–9d`) while the float case writes the 32-bit pattern with an implicit zero-extend (`mov (dword)`). The wire `Dtype` is therefore the discriminator a reader uses to re-narrow the payload: `int32(15)` ⇒ signed dword, `float32(16)` ⇒ float bits. The immediate value-object only ever carries those two dtypes from this path (fp8/fp4 immediates are wire-illegal — see [value-model](value-model.md#the-immediate--value-family)).

### Adversarial verification — the operand-append offset and immediate minting

Five claims above were re-checked byte-for-byte against `libwalrus.so`:

```
# input-list splice at Instruction+0xA0 (addOperandToInst, float arm)
f1c01d: 48 8b 95 a0 00 00 00   mov  0xa0(%rbp),%rdx          # load old head
f1c024: 48 8d 8d a0 00 00 00   lea  0xa0(%rbp),%rcx          # &inst->arg_head
f1c04b: 48 89 85 a0 00 00 00   mov  %rax,0xa0(%rbp)          # store new tail   ✓ +0xA0

# ImmediateValue ctor ArgumentKind=6, then setType float32=16 / int32=15
f1c00a: ba 06 00 00 00         mov  $0x6,%edx               # ArgumentKind 6   ✓
f1c05f: 41 c7 46 30 10 00 00   movl $0x10,0x30(%r14)        # Dtype 16=float32 ✓
f1c163: 41 c7 46 30 0f 00 00   movl $0xf,0x30(%r14)         # Dtype 15=int32   ✓

# tensor arm: addArgument<PhysicalAccessPattern, MemoryLocation*, SmallVector<APPair,4>, …>
f1bf96: e8 e5 0d 6e ff         call …Instruction::addArgument<PhysicalAccessPattern,…>  ✓
```

All four — the `+0xA0` splice, `ArgumentKind 6`, the `0x10`/`0x0F` dtype writes, and the `addArgument<PhysicalAccessPattern>` instantiation — are **CONFIRMED byte-exact**. The `+0xA0` offset agrees with [instruction-base](instruction-base.md)'s arg-list head; the `ImmediateValue` ctor (kind 6, `0x50` bytes, value@`+0x48`, dtype@`+0x30`) agrees with [value-model §ImmediateValue](value-model.md#the-immediate--value-family).

## `codegenImmediate` — literal → `variant<float,int>`

`codegenImmediate(shared_ptr<klr::Immediate>)` @ `0xf16980`. The decoder feeding `codegenOperand`'s immediate arm. It returns a `variant<float,int>` packed into one 64-bit register: the low dword is the value bits, byte 4 is the discriminator (`0`=float-arm, `1`=int-arm).

```c
// neuronxcc::backend::KlirToBirCodegen::codegenImmediate  @ 0xf16980
variant<float,int> codegenImmediate(shared_ptr<klr::Immediate> imm) {
  uint32_t kind = **imm;                          // klr::Immediate kind @ offset 0
  if (kind == 4) {                                // ── FLOAT immediate
    auto inner = (*imm)[+8];                       // ImmediateFloatWrapper (refcount bump)
    float f = codegenImmediateFloatWrapper(inner); // = *(float*)(*inner + 4)   @0xf14080
    return float_arm(f);                           // value=f, discriminator=0
  }
  if (kind == 3) {                                // ── INT immediate
    auto inner = (*imm)[+8];                       // ImmediateIntWrapper (refcount bump)
    int  i = codegenImmediateIntWrapper(inner);    // = *(uint32_t*)(*inner + 4)  @0xf14090
    return int_arm(i);                             // value=i, discriminator=1
  }
  __assert_fail("false && \"Unsupported immediate type encountered\"",
                "klir_to_bir_codegen.cpp", /*line=*/425, "...codegenImmediate(...)");
}
```

The two value-readers are trivial field-loads — the raw 4-byte scalar lives at `klr-inner + 4`, i.e. the wrapper is an 8-byte `{tag:u32, value:u32/f32}` header. There is **no** separate non-wrapper `codegenImmediateFloat`/`codegenImmediateInt` symbol; `…FloatWrapper` (`0xf14080`) and `…IntWrapper` (`0xf14090`) are the readers. *(CONFIRMED from `nm -DC`.)*

> **CORRECTION — `klr::Immediate` has four arms; codegen accepts only two.** `klr::Immediate_ser` (`0xf457a0`) pins the kind enum: `1 = Nat`, `2 = Bool` (tagless arm), `3 = Int`, `4 = Float`. `codegenImmediate` lowers **only** Int(3) and Float(4) and *asserts* on Nat(1)/Bool(2). The KLR front-end must therefore pre-fold a `Nat` literal to `Int` before codegen; a Nat or Bool reaching this point is a hard `__assert_fail` at `klir_to_bir_codegen.cpp:425`. *(Four-arm enum CONFIRMED from the serializer; Bool name for arm 2 is INFERRED from the Nat/Bool/Int/Float pattern — the serializer reads no payload for it.)*

## `codegenRegister` — mint or reuse a named scalar register

`codegenRegister(std::string name, bir::EngineType engine)` @ `0xf141e0`. A 14-byte tail-thunk: it forwards `name` and `engine` unchanged, injects a hardcoded physical-register count of 1, and tail-jumps into `libBIR`.

```
# neuronxcc::backend::KlirToBirCodegen::codegenRegister  @ 0xf141e0  (CONFIRMED, 14 bytes)
f141e0: 48 8b 7f 40            mov  0x40(%rdi),%rdi          # Function* = *(KlirToBirCodegen + 0x40)
f141e4: b9 01 00 00 00         mov  $0x1,%ecx               # 4th arg (ulong count) = 1  ← HARDCODED
f141e9: e9 c2 9b 70 ff         jmp  bir::Function::addRegister(string const&, EngineType, ulong)
```

So `codegenRegister(name, engine) ≡ Function::addRegister(name, engine, /*numPhysicalRegs=*/1)`. The `Function*` lives at `KlirToBirCodegen+0x40` — the live `bir::Function` the codegen is filling (the ctor sets `+0x38 = Module`, `+0x40 = Function`).

Down in `libBIR`:

```c
// bir::Function::addRegister(name, EngineType, count)  @ libBIR 0x276530
Register* addRegister(Function *f, string const& name, EngineType eng, ulong count) {
  Register *r = Function::addRegister(f, name, count);          // 2-arg overload @0x2764f0
  *(uint32_t*)(r + 0x180) = eng;                                // EngineType @ Register+0x180
  return r;
}
// bir::Function::addRegister(name, count)  @ libBIR 0x2764f0
Register* addRegister(Function *f, string const& name, ulong count) {
  Register *r =
    NamedObjectContainer<Function,Storage>::insertElement<Register>(f+88, f+88, name); // NAME-KEYED
  Register::setNumPhysicalRegs(r, count);                       // +0x18C = count (=1)
  return r;
}
```

`insertElement<Register>` over the `NamedObjectContainer<Storage>` at `Function+0x58` is the mint-or-reuse-by-name point; the companion lookup `getRegisterByName` (`0x26ce10`) is a `_Hash_bytes(name, 0xC70F6907)`-keyed bucket search over `Function+104/+112` that returns the `Storage` only if its class-tag (`+0x110 == 2`) is `Register`. The two share one name-keyed namespace.

> **NOTE — a codegen register is one physical reg on `ALL`, untyped at the def.** `codegenRegister` writes no dtype onto the `Register`; the def carries only `EngineType` (`+0x180`) and `numPhysicalRegs = 1` (`+0x18C`). The per-use element `Dtype` (`int32 = 15` in every register-op leaf) lives on the `RegisterAccess` argument, not the def. The callers — `codegenRegisterAluOp` / `codegenRegisterMove` and the compare-branch LHS/RHS binds — pass `EngineType = ALL(7)` (byte-witnessed `mov edx,7`), **not** `SP(6)`; `lower_branch` re-homes the register file onto `SP(6)` downstream. The codegen-time engine tag is semantic-`ALL`. Leaves use a "look-up-or-create" pattern: try `getRegisterByName(name)` first, and on NULL call `codegenRegister(name, ALL)` to mint — `codegenRegister` is the lazy mint-half. *(The 14-byte thunk and `mov $0x1,%ecx` are CONFIRMED byte-exact; engine=ALL(7) is CONFIRMED from the D-I17 callers.)*

## `codegenDependencyEdges` — replay the frontend's declared ordering

`codegenDependencyEdges(shared_ptr<klr::LncKernel>)` @ `0xf1e450`. This is the **batch dependency-wiring post-pass**. `codegenLncKernel` (`0xf34140`) runs the per-statement walk (`codegenStmt` @ `0xf34ac3` in the loop), then calls `codegenDependencyEdges` **exactly once** after the walk (`@0xf352ca`). Edges are never wired per-op. *(CONFIRMED — single post-walk call site.)*

### What it reads

The `klr::Edges` list at `klr::LncKernel + 0x98`. `klr::LncKernel_ser` (`0xf49b70`) pins the field (`List_Edges_ser(*a2 + 152)`), and `klr::Edges_ser` (`0xf46460`) pins each record as a two-field tag:

```
klr::Edges = { from:   String        @ +0x00     // the producer instruction NAME
               to:     List<String>  @ +0x20 }   // the consumer instruction NAMES
```

That is a pure **name-keyed adjacency record**. The list of them at `LncKernel+0x98` is the frontend-declared program dependency graph. *(Both layouts CONFIRMED from the two serializers.)*

### The body

```c
// neuronxcc::backend::KlirToBirCodegen::codegenDependencyEdges  @ 0xf1e450
// (boost::log stream machinery elided; only the control flow shown)
void codegenDependencyEdges(KlirToBirCodegen *this, shared_ptr<klr::LncKernel> k) {
  void *head = k + 0x98;                          // List<klr::Edges> head  @0xf1e608-0f
  for (node = *head; node != head; node = node->next) {       // std::list walk
    Edges *rec      = *(node + 0x10);             // the klr::Edges record (node payload)
    string fromName = string{*(rec+0), *(rec+8)}; // record[0] = "from" String  @0xf1e648

    Instruction *fromInst =
        bir::Function::getInstructionByName(this->Function /*+0x40*/, fromName);  // @0xf1e683
    if (!fromInst)
      throw std::runtime_error(fromName +
          " is not found, unable to complete dependency edge insertion");

    for (toNode in *(rec + 0x20)) {               // record+0x20 = List<String> "to"
      string toName = string{*(toNode + 0x10), …};
      Instruction *toInst =
          bir::Function::getInstructionByName(this->Function, toName);            // @0xf1e6de
      if (!toInst)
        throw std::runtime_error(toName +
            " is not found, unable to complete dependency edge insertion");

      // THE EDGE — disasm @0xf1e6ef-fb, byte-exact:
      //   rdi = toInst (consumer)   rsi = fromInst (producer)   edx = 1   ecx = 0
      bir::Instruction::addDependency(toInst, fromInst, /*EdgeKind=*/1, /*loop_carried=*/0);
    }
  }
}
```

### Edge semantics

`bir::Instruction::addDependency(this = toInst, former = fromInst, EdgeKind, bool loop_carried)` @ `libBIR 0x2e7640`. The direction is **consumer waits for producer**: `this` (the `"to"` name, the reader) gets a predecessor edge to `former` (the `"from"` name, the writer). The edge is packed as an `llvm::PointerIntPair<Instruction*, 3>` — `(former | EdgeKind&7)` — into the consumer's `dependencies` predecessor set (`Block+0x08` count, `Block+0x20` iter-list) on its `+0xD0` scheduling block, with `loop_carried = 0` selecting the normal `dependencies` head over the `loop_carried_dependencies` head at `+0x4C0`. This is exactly the [instruction-base](instruction-base.md#dependency-edges) edge model: the 3-bit `EdgeKind` tag, `dependencies@+0x20` vs `loop_carried@+0x4C0` as orthogonal heads.

The codegen edge is stamped **`EdgeKind = Ordered(1)`** — the weakest non-trivial kind (`Invalid 0 < Ordered 1 < Anti 2 < Output 3 < Flow 4`), **not** Flow(4), **not** Anti(2), **not** Output(3). Because `addDependency` MAX-merges on the `(toInst→fromInst)` slot, a later pass that proves a stronger kind on the same pair upgrades it in place.

> **GOTCHA — this is declared ordering, not dependency analysis.** There is **no** last-writer-of-`MemoryLocation` map, **no** klr SSA value→producer map, **no** `AccessPattern` overlap test, and **no** Flow/Anti/Output classification anywhere in this body. The producer/consumer relation is entirely name-declared by the frontend in `klr::Edges` and replayed verbatim as `Ordered(1)`. The dependency model at the KLR-codegen boundary is therefore "frontend-declared program-ordering edges," carrying the NKI author's / klr-builder's intended execution order (RNG-state chains, explicit barriers, hand-declared ordering) into BIR before any analysis runs. A dangling name (a `from`/`to` not matching an emitted instruction) is a hard `runtime_error` — the frontend graph must reference only emitted insts.

### Adversarial verification — the EdgeKind packing and the once-per-kernel replay

```
# codegenDependencyEdges edge-emit site  @ 0xf1e6ef
f1e6de: e8 fd 2c 6f ff   call …Function::getInstructionByName   # rdi=Function+0x40, rsi=toName
f1e6e3: 48 89 c7         mov  %rax,%rdi                          # rdi = toInst (consumer)
f1e6ef: 48 8b 74 24 30   mov  0x30(%rsp),%rsi                    # rsi = fromInst (producer)
f1e6f4: 31 c9            xor  %ecx,%ecx                          # ecx = 0   → loop_carried=0
f1e6f6: ba 01 00 00 00   mov  $0x1,%edx                          # edx = 1   → EdgeKind Ordered
f1e6fb: e8 00 18 6d ff   call …Instruction::addDependency        ✓
```

The edge kind (`edx=1`), the non-loop-carried flag (`ecx=0`), and the consumer→producer direction (`rdi=to`, `rsi=from`) are **CONFIRMED byte-exact**. `Ordered(1)` is consistent with [instruction-base](instruction-base.md)'s `EdgeKind = {Invalid 0, Ordered 1, Anti 2, Output 3, Flow 4}` recovery and with `addDependency`'s `PointerIntPair` packing into the `dependencies` set. The single post-walk call (`codegenLncKernel @0xf352ca`) is the once-per-kernel invariant.

## The dependency lifecycle: where the Ordered edges go

`codegenDependencyEdges` is the **first and weakest** of four dependency-construction events that all write the same edge sets at different pipeline stages:

| Stage | Pass | Kind emitted | What it adds |
|---|---|---|---|
| 0 | **`codegenDependencyEdges`** (KLR→BIR) | `Ordered(1)` | replay of frontend `klr::Edges` — the only inter-instruction edges that exist right after lowering |
| 1 | PreSched (`@33`, [build_fdeps] lifecycle) | (consumes kind-agnostically) | masks `EdgePtr & ~7` and keeps topological reachability — so the Stage-0 Ordered edges **do** constrain the initial list-schedule |
| 2 | `build_fdeps` (`@74`) | `Flow(4)` | rebuilds the real RAW value-SSA chain on materialized `PhysicalAP`s (reader→last-writer per `MemoryLocation`), reduction/accumulation sequences; MAX-merge **upgrades** any Ordered pair it proves is a true flow |
| 3 | anti-dependency analyzer (`@76`) | `Anti(2)` / `Output(3)` | post-coloring physical-aliasing WAR/WAW edges, invisible to codegen (no physical addresses yet) |
| 4 | post-sched / synchronizer / `lower_sync` | (consumes merged set) | chooses cross-engine edges needing a hardware semaphore |

> **NOTE — why codegen declares only Ordered, never Flow.** At lowering time the BIR is still pre-unroll and pre-allocation: APs are symbolic, there are no physical addresses, and no materialized writer/reader use-def lists exist. The only dependency information available is what the frontend explicitly declared, and the safe encoding for "must run in this order, reason TBD" is `Ordered(1)`. `build_fdeps` later computes the real value-flow on materialized `PhysicalAP`s and the anti analyzer adds post-coloring aliasing edges; the codegen Ordered edges persist as program-order scaffolding, upgraded in place wherever a stronger kind is later proven on the same `(consumer→producer)` slot. *(Lifecycle STRONG — synthesized from the PreSched / build_fdeps / anti-analyzer reports; the Stage-0 `Ordered(1)` emission and the MAX-merge are CONFIRMED.)*

## The master operand → argument contract

Putting the sub-encoders together, every KLR codegen leaf binds operands through exactly one of two static-type-selected paths, and wires dependencies through one post-pass:

```
POSITIONAL TENSOR slot (klr::TensorRef field)
  leaf → codegenTensorRef(ref)                              [allow_dynamic = 0]
         → {MemoryLocation*, SmallVector<APPair,4>, base, Dtype}     (the AP encoder, 7.22)
  leaf → InstBuilder::addXxx(... as positional MemLoc + AP, role chosen by the leaf)

SCALAR-OR-TENSOR operand (klr::Operand field — a variant)
  leaf → addOperandToInst(operand, inst)
         → codegenOperand(operand)
              kind 1 Immediate → codegenImmediate → variant{float-arm(0) | int-arm(1)}
                                                    (Nat/Bool → assert cpp:425)
              kind 2 TensorRef → codegenTensorRef → variant tensor-arm(2)
         → tag 0 → new ImmediateValue(kind 6), setType(16=float32), value@+0x48, INPUT @+0xA0
           tag 1 → new ImmediateValue(kind 6), setType(15=int32),  sext value@+0x48, INPUT @+0xA0
           tag 2 → addArgument<PhysicalAccessPattern(kind 1), MemLoc*, AP, base, optional<Dtype>>

NAMED SCALAR REGISTER (register-op leaves)
  leaf → getRegisterByName(name)  ?? :  codegenRegister(name, ALL)   // Function::addRegister(…,1)

KERNEL DEPENDENCY EDGES (once, after the statement walk)
  codegenLncKernel → codegenDependencyEdges(kernel)
       for each klr::Edges {from, to[]} at LncKernel+0x98:
         toInst.addDependency(fromInst, EdgeKind=Ordered(1), loop_carried=0)
```

Invariants a reimplementer must preserve: (1) the klr→bir `MemoryLocation`/`Register`/`Instruction` binding is uniformly **name-keyed** (`getMemoryLocationByName` / `getRegisterByName` / `getInstructionByName`, with mint-on-miss for the first two) — there is no klr-id→object map; (2) immediates are **always plain inputs** with no bound storage; (3) the codegen dependency edges are **all `Ordered(1)`** and the true data/anti/output dependence is deferred to the later analysis passes.

## Cross-references

- [The `bir::Instruction` Base Struct](instruction-base.md) (7.1) — the `+0xA0` input list, the `+0xD0` sched/dep block, and the `PointerIntPair<Instruction*,3>` edge model `addDependency` packs into.
- [BIR Value Model](value-model.md) (7.4) — `Argument` / `ImmediateValue` (kind 6, `0x50`, value@`+0x48`) / `PhysicalAccessPattern` (kind 1) / `Register` value objects these sub-encoders mint.
- [MemoryLocation and Storage](memory-location.md) (7.5) — the name-keyed `MemoryLocation` the tensor arm binds via `setLocation`.
- [BirCodeGenLoop Access-Pattern Builders](../nki/bircodegen-ap.md) (7.22) — `codegenTensorRef` / `codegenAccess` / `codegenAP`, the symbolic-AP encoder Strand A and the tensor arm of Strand B delegate to.

[build_fdeps]: ../walrus/
