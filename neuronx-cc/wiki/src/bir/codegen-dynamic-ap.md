# KLR→BIR Codegen of the Dynamic / Runtime Access Pattern

> *Every symbol, address, struct offset, and vtable VA on this page applies to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The two functions reconstructed here — `KlirToBirCodegen::assembleDynamicInfo` and `KlirToBirCodegen::extractDynamicApToStandAlone` — live in `neuronxcc/starfish/lib/libwalrus.so` (md5 `1d93972b81e619ce6d178a0e4b9003b3`, 64,973,024 bytes); the back-end codegen bodies are real here even where the `nki_klr_sim` driver carries only PLT stubs. `VA == file offset` for `.text` (verified at `0x62d660`) and `.rodata` (`0x1c72000`); the `bir::DynamicAPINFO` setters and `bir::Function::getRegisterByName` are imported `U` symbols satisfied by `libBIR.so`. cp311/cp312 share the ABI but drift the VAs.*

## Abstract

A KLR access is **dynamic** when its address is not a compile-time constant — when the
offset into SBUF/PSUM is a value the device only knows at execution time. There are
exactly two shapes of runtime offset, and they are mutually exclusive: a **scalar**
register-backed offset (a runtime integer read from a device register — a dynamic loop
base, a data-dependent slice start) and a **vector** gather index (a tensor of indices
that selects rows — the classic gather/scatter, and the MoE expert-routing path). This
page reconstructs the KLR→BIR codegen slice that turns each shape into the symbolic
address algebra the rest of the back-end materializes.

The center of the page is `assembleDynamicInfo` @`0xf20230`. Given a KLR operand whose
**static** geometry has already been encoded into an `InstArg` (the kind-1
`PhysicalAccessPattern` that [`codegenAccess`](codegen-accesspattern.md) mints), it builds
the `bir::DynamicAPINFO` companion that carries the runtime term. The decisive branch is
the **scalar-kind-7 vs vector-kind-3** split: the scalar offset becomes a
`bir::BirIntRuntimeValue` (pelican `Expr` kind **7**, the `IntRuntimeValueBase` with its
`regref` at `+0x40` documented in [Pelican Index/Runtime](pelican-index-runtime.md)); the
vector offset becomes a `pelican::IndirectArgExpr` (kind **3**, an `arg_id` handle). Both
are wrapped in a `QuasiAffineExpr` and paired with an accumulated-shape coefficient, so the
net address the device evaluates is `addr = c + Σ_axis coef_axis · expr_axis`.

The sibling function `extractDynamicApToStandAlone` @`0xf1e2c0` is the **index
pre-materialization**: for the gather case it hoists the index *tensor* out of the parent
AP into its own standalone `InstArg` and strips the vector-offset flag, so the index is
encoded exactly once and the parent AP is no longer dynamic at that slot. It is a
codegen-time rewrite — it does **not** build a `DynamicAPINFO` and does **not** emit
register-ALU ops. The two functions are siblings on the dynamic-DMA path, not nested:
`assemble…` packages the address algebra onto the data AP; `extract…` hoists the index
operand. Both bottom out in `codegenAccess` as the leaf encoder.

This is the KLR-codegen slice of the dynamic-shape thread. It mints the **symbolic**
(kind-2) form; the [Symbolic-AP → Register-ALU](../penguin/symbolic-ap-register-alu.md)
lowering (`LowerAP::convertSymAP`) later turns it into a kind-3 `RegisterAccessPattern`
with real `InstRegisterAlu` micro-ops; the [Dynamic-Shape Synthesis](../penguin/dynamic-shape-synthesis.md)
page is the end-to-end view. Front-end DMA bucketing is in [`front/pipeline`](../front/pipeline.md).

| | |
|---|---|
| **`assembleDynamicInfo`** | `0xf20230` (2683 B, 603 ins); mangled `_ZN9neuronxcc7backend16KlirToBirCodegen19assembleDynamicInfoE…` |
| **`extractDynamicApToStandAlone`** | `0xf1e2c0` (386 B, 104 ins) |
| **The dynamic predicate** | `isAccessDynamic(BAP)` @`0xf14030` = `bap[+0x40]` (scalar) `\|` `bap[+0x58]` (vector) |
| **The XOR validator** | `_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided` @`0xf140a0` — exactly one of the two |
| **Scalar offset →** | pelican `Expr` **kind 7** `bir::BirIntRuntimeValue` (vtable `0x3d8de40`), `regref` @`+0x40` |
| **Vector offset →** | pelican `Expr` **kind 3** `pelican::IndirectArgExpr` (vtable `0x3daa100`), `arg_id` @`+0x20` = 1 |
| **Per-axis coefficient** | `_computeAccumulatedShape` @`0xf152e0` — product of dim extents from `dim` inward (row-major stride) |
| **Companion struct** | `bir::DynamicAPINFO` — `actual_ap@+0`, `aff_expr@+0x50`, `c@+0x68`, `indirect_dim_max@+0x70`, `indirect_dim@+0x74` |
| **Net address** | `addr = c + Σ_axis coef_axis · expr_axis` |
| **Downstream lowering** | `LowerAP::convertSymAP` @`0x11b7f80` (kind-2→kind-3 register-ALU), `createDescForReadVarAddr` @`0x1198860` |

---

## 1. What "dynamic" means at the KLR level

### The predicate and the BAP payload

A KLR `Access` of `kind==4` carries a `klr::BirAccessPattern` (BAP). A BAP is **dynamic**
iff it holds a runtime offset, and the predicate is a one-line byte OR
(`isAccessDynamic(BAP)` @`0xf14030`):

```c
bool isAccessDynamic(klr::BirAccessPattern *bap) {  // 0xf14030
    return bap[+0x40]      // SCALAR  runtime offset present
         | bap[+0x58];     // VECTOR  runtime offset present
}
```

The two wrappers that reach it gate on the KLR object kind first:

- `isAccessDynamic(klr::Access)` @`0xf16540` — true only if `Access.kind == 4` (it *is* a BAP).
- `isAccessDynamic(klr::TensorRef)` @`0xf16760` — true only if `TensorRef.kind == 1` (an
  SBUF/PSUM ref); a DRAM `kind==4` ref can never be dynamic, then it delegates to the inner
  `Access`.

The two presence bits are **mutually exclusive**. A dynamic BAP carries *either* a scalar
runtime offset *or* a vector gather index, never both, and the invariant is enforced before
any work begins — `_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided` @`0xf140a0`
compares the two presence bytes and faults when they are equal:

```c
void _validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided(BAP *bap) {  // 0xf140a0
    assert(bap[+0x40] != bap[+0x58]);   // EXACTLY ONE of {scalar, vector}
}
```

> **QUIRK — the XOR is on raw presence bytes.** The check is a byte inequality, not a
> logical XOR over normalized booleans: `bap[+0x40] != bap[+0x58]`. If both were 1 (or both
> 0) it fires. Because each byte is written as a 0/1 flag this behaves as XOR, but a
> reimplementer must store the flags as canonical 0/1 — any other truthy value would make
> the inequality pass spuriously when both are "present."

### The `klr::BirAccessPattern` field map

Every slot below is read directly by `assembleDynamicInfo` or
`extractDynamicApToStandAlone`, and the table is reconstructed from those `[rsi+NN]` /
`[rax+NN]` loads. No separate strand maps the full BAP header, so slots neither function
touches carry no type here — this is the *used* subset of the struct, not its full layout.

| Off | Type | Meaning | Evidence |
|---|---|---|---|
| `+0x00` | i32 `kind` (=4) | `klr::Access` kind = `BirAccessPattern` | `castToBirAp` assert |
| `+0x10` | u32 | **static** offset residue (`c`) | `setStaticOffsetPortion(bap+0x10)` |
| `+0x28` | ptr | `klr::Shape*` (per-dim shape list) | `[*BAP+0x28]` → `_computeAccumulatedShape` |
| `+0x38` | `Access` (shared_ptr) | SCALAR-offset inner Access | scalar branch `codegenAccess` |
| `+0x40` | byte | SCALAR-dynamic-offset PRESENT | `cmp [rsi+0x40]` |
| `+0x48` | `Access` (shared_ptr) | VECTOR-offset inner Access (gather index AP) | `[bap+0x48]` (= `+72`) → `codegenAccess` |
| `+0x58` | byte | VECTOR-dynamic-offset PRESENT (gather) | `cmp [rsi+0x58]` |
| `+0x60` | i32 | `indirect_dim` (gathered axis ordinal) | `mov r8d,[rax+0x60]` |
| `+0x80` | ptr | gather-index shared_ptr control block (cleared on hoist) | `extract…` tail |

The `castToBirAp(TensorRef)` @`0xf168d0` → `castToBirAp(Access)` @`0xf166b0` chain is the
typed accessor that recovers a BAP from a ref: it asserts `ref.kind==1` ("Logic fault:
Attempt to cast a TensorRef that is not an BirAP"), takes the inner `Access` at `ref+0x08`,
asserts `Access.kind==4`, and returns the BAP at `Access+0x08`.

---

## 2. `assembleDynamicInfo` — the dynamic-info packager

### Signature and role

```c
// _ZN9neuronxcc7backend16KlirToBirCodegen19assembleDynamicInfoE…  @0xf20230
bir::InstArg KlirToBirCodegen::assembleDynamicInfo(
        std::unique_ptr<bir::DynamicAPINFO> &dyn,   // OUT: the companion to fill
        std::shared_ptr<klr::TensorRef>      ref,   // the dynamic operand
        bir::InstArg                        &arg);  // the ALREADY-built STATIC InstArg
```

The signature matches `nm -DC`. The caller has already encoded the
operand's **static** geometry into `arg` — an `InstArg` is `{MemLoc*, SmallVector<APPair,4>
pattern, partition, dtype, valid}` (the kind-1 form; see
[`codegen-accesspattern`](codegen-accesspattern.md)). `assembleDynamicInfo` builds the
`bir::DynamicAPINFO` companion that carries the **runtime** address algebra, then returns a
fresh `InstArg` by value — the operand the caller binds via `addArgument`. The companion
hangs off `PhysicalAccessPattern+0x1D8` and serializes to the `dynamic_ap_info` key.

### Prologue — common to every branch

```c
// 0xf20230..0xf20338
BAP *bap = castToBirAp(ref);                                   // 0xf20280
_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided(bap);// 0xf202b4

dyn->setActualPattern(arg.pattern @ arg+8);                   // 0xf202d7  ⭐
    // actual_ap = the STATIC-resolved SmallVector<APPair> already in `arg`:
    // the concrete (step,num) geometry codegenAccess produced. DynamicAPINFO+0x00.

PelicanContext *ctx = *(*(this+0x38) + 0xA0);                 // 0xf202e6
    // this+0x38 = Module/Function;  +0xA0 = its PelicanContext.

// Build ONE pelican IndirectArgExpr (kind 3) up front:
auto *idx = (Expr*) tc_new(0x28);                             // 0xf202f9
pelican::Expr::Expr(idx, /*kind=*/3, ctx);                    //   esi=3
idx->vptr = &_ZTVN7pelican15IndirectArgExprE + 0x10;          // 0xf20309 (off_3DAA110)
idx[+0x20] = 1;                                               // 0xf20310  arg_id = 1
inc_ref(idx);

switch (presence) {                                           // 0xf20334 / 0xf2033e
    case bap[+0x58]:  goto VECTOR;    // §2.2 gather index tensor
    case bap[+0x40]:  goto SCALAR;    // §2.3 register-backed offset
    default: throw "Logic fault, this branch should never be taken"; // 0x1d1d900
}
```

The `arg_id` slot is hard-stored as `1` @`0xf20310`. The
`IndirectArgExpr` is built unconditionally in the prologue and reused as the per-axis term
in both the vector path and the scalar `codegenAccess` sub-path; only the scalar
register-name path mints a *different* expr (kind 7). The `default` arm is a redundant
fail-safe — the XOR validator already excludes it.

### 2.2 Vector branch — a runtime gather index (kind 3)

```c
// loc_F20860..
VECTOR:
  Access *offAcc = bap[+0x48];                  // 0xf20860 the gather-index inner Access
  codegenAccess(offAcc);                        // 0xf20887 ENCODE the index AP (result
                                                //   dropped; resolves the index MemLoc)
  assert(bap[+0x60] /*indirect_dim*/ == 0);     // 0xf208a0
      // else throw "When using vector offset, the only supported indirectDim is 0"
      //   (0x1d1d8c0). A gather index addresses the W/partition axis only.
  dyn[+0x74] = 0;                               // 0xf208b2  indirect_dim = 0

  // indirect_dim_max_index = number of gatherable rows (the index range):
  if (MemLoc_kind(arg.MemLoc) == 1)             // 0xf208ce scalar-name MemLoc
      dyn[+0x70] = (*(ML+0x40))[0];             //   loc_F20B18
  else {
      dyn[+0x70]  = **(ML+0x100);               // 0xf208ec  ML+0x100 "dims"[0] = elem count
      bytes       = **(ML+0x108);               // 0xf208f9  ML byte size
      dt          = MemoryLocation::getDtype(ML);// 0xf20906 assert dt<=0x13 else "Unknown dtype"
      dyn[+0x70]  = bytes / dtype_bytes[dt];    // 0xf20922  ÷ per-dtype size
  }

  // per-axis term {IndirectArgExpr(idx), accumulated-shape coef}:
  coef = dyn[+0x70];
  dyn->aff_expr @+0x50 = { { QuasiAffineExpr(RefPtr(idx)), coef } };  // 0xf20940 / 0xf209a9
  dyn->setStaticOffsetPortion(bap[+0x10]);      // 0xf209b9  c = static residue
  populate_return_InstArg();
```

The per-dtype size table is `dtype_bytes[] = {1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}`
(20 entries, indices `0..0x13`), read at `qword_1DE98C0` and **byte-verified directly from
`.rodata`**. `indirect_dim_max_index` is recovered from the index tensor's own extent —
either its leading "dims" entry, or its byte size divided by the element size — and bounds
the gatherable rows.

> **QUIRK — the gather index can only address the partition axis.** `indirect_dim` is
> asserted `== 0` for the vector path and the `DynamicAPINFO` field is hard-written 0. The
> error string "the only supported indirectDim is 0" is the guard. A reimplementer who
> wants to gather along an inner free axis cannot express it through this path — the vector
> offset is structurally a W/partition-axis selector.

### 2.3 Scalar branch — a register-backed runtime offset (kind 7)

```c
// 0xf20348..
SCALAR:
  dyn[+0x74] = 0;                               // 0xf20365  indirect_dim = 0
  Access *offAcc = bap[+0x38];                  // the scalar-offset inner Access
  if (offAcc->kind == 1) goto REGISTER_NAME;    // 0xf20371  a bare register name
  else                   goto CODEGEN_ACCESS;   // a full Access
```

**§2.3a Register-name path** (`loc_F20580..0xf2075b`) — the offset is a bare
register name:

```c
REGISTER_NAME:
  std::string name = std::string(offAcc.bytes @ +0x8 .. +0x10);   // 0xf205bf
  Register *r = Function->getRegisterByName(name);                // 0xf205e4
  if (!r) throw "Cannot find register: " + name + " for scalar offset";  // 0x1c7e0a0/0x1c7e0b7

  auto *rv = (Expr*) tc_new(0x48);                                // 0xf20619
  pelican::Expr::Expr(rv, /*kind=*/7, ctx);                       //   esi=7
  rv->vptr = &_ZTVN3bir18BirIntRuntimeValueE + 0x10;             // 0xf2062d
  rv[+0x40] = r;                                                  // 0xf20634  regref = Register*  ⭐
  rv[+0x20] = (i64)-1;                                            // 0xf20638  no static index

  coef = _computeAccumulatedShape(*bap+0x28 /*Shape*/, bap[+0x60] /*dim*/);  // 0xf2068f
  dyn->aff_expr @+0x50 = { { QuasiAffineExpr(RefPtr(rv)), coef } };          // 0xf206b3
  dyn->setStaticOffsetPortion(bap[+0x10]);                        // 0xf20753  c
  populate_return_InstArg();
```

`rv` is a `bir::BirIntRuntimeValue` — the bir leaf grafted onto pelican kind 7
(`IntRuntimeValueBase`). Its `regref` at `+0x40` is the same field the
[Pelican Index/Runtime](pelican-index-runtime.md) page documents as the dynamic-shape
spine: a `Register*` parked inside an `Expr` node, treated downstream as an opaque
loop-invariant scalar. The `Register*` lands in that slot @`0xf20634`, immediately after
`getRegisterByName` returns, and the vtable VA `0x3d8de40` matches `nm -DC`
(`vtable for bir::BirIntRuntimeValue`).

**§2.3b Codegen-Access path** (`0xf2037a..0xf204d6`) — the scalar offset is a
full `Access` rather than a register name:

```c
CODEGEN_ACCESS:
  codegenAccess(offAcc);                                         // 0xf203b8 resolve its MemLoc
  coef = _computeAccumulatedShape(*bap+0x28, bap[+0x60]);        // 0xf2040f
  dyn->aff_expr @+0x50 = { { QuasiAffineExpr(RefPtr(idx)), coef } };  // 0xf2043b/0xf204c1
      // reuses the prologue IndirectArgExpr (kind 3) — NOT a fresh kind-7 node.
  dyn->setStaticOffsetPortion(bap[+0x10]);                       // 0xf204d1  c
  populate_return_InstArg();
```

> **QUIRK — "scalar" does not always mean kind 7.** The scalar branch splits on
> `offAcc->kind == 1`. Only the *register-name* sub-path mints the kind-7
> `BirIntRuntimeValue`; the *full-Access* sub-path reuses the prologue's kind-3
> `IndirectArgExpr`. The kind-7-vs-kind-3 distinction is therefore not "scalar vs vector"
> at the top level — it is "register-name scalar" (kind 7) vs "everything else" (kind 3,
> minted once and shared by the vector path and the scalar full-Access path).

### 2.4 The `DynamicAPINFO` it builds

Field-by-field, every offset is written by `assembleDynamicInfo` and matches the
independently-mapped `DynamicAPINFO` layout byte-for-byte:

| `DynamicAPINFO` offset | Wire key | Source | Set by |
|---|---|---|---|
| `+0x00` | `actual_ap` | the STATIC AP from `arg` | `setActualPattern(arg.pattern)` |
| `+0x50` | `aff_expr` + `coef` | `vector<pair<QuasiAffineExpr, long>>` | `sub_F13410` / `sub_F1FB60` |
| (within `+0x50`) | `offset_expr` | the per-axis runtime `Expr` (kind 3 / kind 7) | `QuasiAffineExpr(RefPtr<Expr>)` |
| `+0x68` | `c` | compile-time residue | `setStaticOffsetPortion(bap+0x10)` |
| `+0x70` | `indirect_dim_max_index` | gatherable-row count (vector only) | `[dyn+0x70]` |
| `+0x74` | `indirect_dim` | always `0` (W axis) | `[dyn+0x74] = 0` |

`tensor_indirect_arg_id` (`+0xF0`) and `num_tensor_indirect_indices` (`+0xF4`) are **not**
written here — they belong to the `SymbolicAccessPattern` companion produced downstream.

The net runtime address algebra (the pelican semantics) is:

```text
addr = c  +  Σ_axis  coef_axis · expr_axis
```

where `expr_axis` is a runtime value (`IndirectArgExpr` index or `BirIntRuntimeValue`
register), `coef_axis = _computeAccumulatedShape` is the per-axis element stride, and `c` is
the static residue. This is the offset-expression the device evaluates per access.

---

## 3. `_computeAccumulatedShape` — the per-axis coefficient

```c
// 0xf152e0
long _computeAccumulatedShape(std::shared_ptr<klr::Shape> shape, int dim) {
    assert(0 <= dim && dim <= shape->numDims + 1);   // else range fault sub_6D6FDE
    node = shape.dims_head;                          // shape+0x08 intrusive dim list
    for (k = 0; k < dim; k++) node = node->next;     // walk to `dim`
    long coef = 1;
    for (; node != list_sentinel; node = node->next)
        coef *= node->extent;                        // node+0x10 (u32) dim extent
    return coef;
}
```

The accumulated stride is the **product of all dim extents from `dim` inward** — the
row-major stride of axis `dim` in element units. It is paired as the `long` half of each
`aff_expr` entry, so `addr += coef · expr`. The `dim` argument is the BAP's `indirect_dim`
(`bap+0x60`).

### The offset-expr vector assigners

`sub_F13410` @`0xf13410` and `sub_F1FB60` @`0xf1fb60` are both
`std::vector<std::pair<bir::QuasiAffineExpr, long>>::operator=` — a deep copy-assignment
with a **40-byte element stride** (a `QuasiAffineExpr` is 32 B = `{RefPtr<Expr>@+0,
SmallVector<LoopAxis*>@+0x8}` plus the `long coef@+0x20`, padded to 5 qwords). They
refcount-bump each `RefPtr<Expr>` via the nanobind intrusive counter. `sub_F1FB60` builds
the stack working vector; `sub_F13410` installs it into the companion's persistent
`aff_expr` field at `DynamicAPINFO+0x50`. The 40-byte loop stride and the
`QuasiAffineExpr` destructor run over the displaced elements are what identify the element
type.

---

## 4. `extractDynamicApToStandAlone` — the index pre-materializer

### Signature and role

```c
// _ZN9neuronxcc7backend16KlirToBirCodegen28extractDynamicApToStandAloneE…  @0xf1e2c0
bir::InstArg KlirToBirCodegen::extractDynamicApToStandAlone(
        std::shared_ptr<klr::TensorRef> ref);
```

This is the **gather-index pre-materialization**. For a dynamic *vector-offset* access it
pulls the index tensor out of the parent AP and encodes it as a self-contained `Access`
whose address is the runtime offset, returning the `InstArg` the consuming op binds. It
then **strips the vector-offset flag** so the parent AP is no longer dynamic at that slot
and the index is encoded exactly once.

```c
// full 104-ins body
bir::InstArg extractDynamicApToStandAlone(std::shared_ptr<klr::TensorRef> ref) {
  assert(isAccessDynamic(ref));                       // 0xf1e303
      // else throw "Compiler Logic Fault: cannot extract dynamic ap from a static access!"
      //   (0x1d1d768)
  BAP *bap = castToBirAp(ref);                         // 0xf1e352

  if (bap[+0x40] /*scalar flag*/)                      // 0xf1e36b — must be VECTOR, not scalar
      throw "When unique_indices=True, it's expected that the vector_offset is set. "
            "Check the nisa.dma_copy call";            // 0x1d1d7b0
  if (!bap[+0x58] /*vector flag*/)                     // 0xf1e3c8
      throw "Logic Fault: dynamic access should have vector offset.";  // 0x1d1d818

  Access *offAcc = bap[+0x48];                         // the gather-index inner Access (= bap+72)
  InstArg result = codegenAccess(offAcc);              // 0xf1e3ad ⭐ ENCODE as standalone InstArg

  if (bap[+0x58]) {                                    // 0xf1e3c8 re-test vector flag
      ctrl = bap[+0x80];                               // old gather-index shared_ptr ctrl
      bap[+0x58] = 0;                                  // STRIP the vector-offset flag
      if (ctrl) release(ctrl);                         // drop its old reference
  }
  return result;
}
```

Each error string sits at its cited `.rodata` address. The branch structure is exactly as
transcribed: the scalar guard fires the `unique_indices=True` string, the missing-vector
guard fires "should have vector offset," `codegenAccess` reads the vector-offset Access at
`bap+72` (`0x48`), and the tail clears `bap+0x58` after releasing `bap+0x80`.

> **GOTCHA — the two error strings are inverted guards on the same gather contract.** The
> *scalar* flag present here is an error ("unique_indices=True … vector_offset is set");
> the *vector* flag absent is also an error ("should have vector offset"). Together they
> assert: a unique-indices `nisa.dma_copy` gather must arrive with the vector offset set and
> the scalar offset clear. A reimplementer must not treat `extractDynamicApToStandAlone` as
> a general dynamic-AP extractor — it is the gather-index hoist specifically, and it rejects
> the scalar/register path.

### Relation to `assembleDynamicInfo`

The two are **siblings on the dynamic-DMA path, not nested** — their bodies are disjoint and
neither calls the other; they share only the `codegenAccess` leaf:

- `assembleDynamicInfo` packages the runtime offset **into** a symbolic companion
  (`DynamicAPINFO`, attached to the *data* AP).
- `extractDynamicApToStandAlone` hoists the *index tensor* out as a separate operand and
  strips the vector flag from the parent.

A unique-indices gather DMA uses `extract…` to materialize the index operand, and the data
side uses `assemble…` (or the symbolic AP) for its address algebra. `extract…` does **not**
build a `DynamicAPINFO` and does **not** emit register-ALU ops.

---

## 5. The kind-1 / kind-2 / kind-3 lifecycle

`assembleDynamicInfo` mints the **symbolic** (kind-2) form. The register-computed address
is produced *downstream*. The full lifecycle, KLR codegen → lowering → encoder:

| Stage | Who | Produces / does |
|---|---|---|
| **codegen** (per operand) | `codegenAccess` / `codegenBirAccessPattern` + `addArgument` | **kind-1** `PhysicalAccessPattern` — the static geometry, the common case |
| ↳ dynamic operand | `assembleDynamicInfo` (§2) | **kind-2** symbolic: `DynamicAPINFO` with pelican `offset_expr` (`IndirectArgExpr` 3 / `BirIntRuntimeValue` 7) + `coef` + `c` + `actual_ap`, attached @`AP+0x1D8` → wire `dynamic_ap_info` |
| ↳ gather index | `extractDynamicApToStandAlone` (§4) | hoists the index AP to its own `InstArg`, clears the vector flag (codegen-time) |
| **lower_ap** | `LowerAP::convertSymAP` @`0x11b7f80` | **kind-2 → kind-3** `RegisterAccessPattern`: mints `InstRegisterAlu` micro-ops that compute the address into a register (`reg_ap_offset`/`regref`); runs last before reg-alloc |
| **lower_dynamic_dma** | `createDescForReadVarAddr` @`0x1198860` | a runtime-variable-address DMA → an `InstDMABlock` whose descriptor references a runtime-resolved memloc |
| **expand_inst_late** | `ExpandDynamicAPInfo` @`0xcd4dc0` + `rewireDynamicAPRegisters` @`0xef91d0` | expands a residual `DynamicAPINFO` into per-engine register wiring (late cleanup) |
| **encoder** | `CoreV*GenImpl` | kind-1 / resolved actual_ap → `ADDR4` bit-31 register-mode + reg id; `indirect_dim` → `ADDR4` bit-29 + `INDIRECT16B` / `MXINDIRECT16B` |

So the division of labor is sharp: `assembleDynamicInfo` (codegen) **mints** the symbolic
`offset_expr`; [`LowerAP::convertSymAP`](../penguin/symbolic-ap-register-alu.md) (L32)
**materializes** it into register-ALU ops; the encoder **stamps** the kind-3 register-mode
descriptor. `extractDynamicApToStandAlone` runs *alongside* `assembleDynamicInfo` at
codegen, hoisting the index so the symbolic AP it feeds references a clean standalone
operand. `extract…` is therefore a codegen-time pre-materialization of the *operand*, not
of the *address compute*.

---

## 6. The BIR-JSON wire form

The `DynamicAPINFO` is **not** a per-op top-level JSON key. It is nested in the
access-pattern object under the common-header `ins` / `indirection_ins` arrays.

The `DynamicAPINFO` class has no serializer of its own. Its full exported method set is
`getCanonicalPattern`, `setActualPattern`, `isPartitionContiguous`,
`setStaticOffsetPortion`, and `getNumElementsPerPartition` — there is no `toJson` member,
and the code that walks these fields into the wire object is not pinned to a named symbol.
The field→key mapping below is therefore read off the member layout: the offsets each
setter writes.

> **GOTCHA — `0x268c00` is not a `DynamicAPINFO` serializer.** That VA (body frame
> `0x1268c00`) is
> `neuronxcc::backend::CoreV2GenImpl::generateIndirectLoadSave(bir::InstDMA&, bool)`, a
> CoreV2 *encoder*. Its indirect-DMA subject matter makes it an easy misattribution when
> hunting for where the dynamic AP reaches the wire.

The field→key mapping (from the member layout):

```text
"actual_ap"                  ← DynamicAPINFO+0x00   (setActualPattern; the static AP)
"aff_expr" + "coef"          ← DynamicAPINFO+0x50   (the pair<QuasiAffineExpr,long> vector)
"offset_expr"  (QuasiAffineExpr) ← the per-axis runtime expr (kind 3 / kind 7)
"c"                          ← DynamicAPINFO+0x68   (setStaticOffsetPortion)
"indirect_dim"               ← DynamicAPINFO+0x74   (= 0 here; the W axis)
"indirect_dim_max_index"     ← DynamicAPINFO+0x70   (gatherable-row count)
"num_tensor_indirect_indices"← +0xF4 ; "tensor_indirect_arg_id" ← +0xF0  (set downstream)
```

The register half (`reg_ap_offset` / `const_ap_offset` / `is_regloc_offset`) appears
**later**, on the kind-3 `RegisterAccessPattern` that `lower_ap` mints — not in
`assembleDynamicInfo`. The `regref` key is the `BirIntRuntimeValue`'s backing register.

---

## 7. Where the two flavours fire

The scalar/vector split maps onto two distinct front-end constructs:

- **Scalar (kind 7, `BirIntRuntimeValue`)** — a runtime *scalar* offset: a dynamic loop
  bound or a per-iteration base index read from a device register (the `nl.*` dynamic-slice
  / dynamic-shape access). The address is `c + coef·reg`; the register is resolved by name
  (`getRegisterByName`) and lowered to register-ALU ops by `lower_ap`. This is the
  **dynamic-shape / data-dependent addressing** path.
- **Vector (kind 3, `IndirectArgExpr`)** — a runtime *index tensor*: the classic
  gather/scatter. The index addresses the W/partition axis (`indirect_dim` forced 0);
  `indirect_dim_max_index` bounds the gatherable rows. This is the **MoE expert-routing**
  path — the per-token expert id (a vector of indices) selects which expert's weight rows
  to gather, exactly the `nisa.dma_copy` "unique_indices" gather that
  `extractDynamicApToStandAlone`'s "Check the nisa.dma_copy call" guard names.

The codegen leaves that produce these are `codegenNcDmaCopy` (dynamic case calls
`assembleDynamicInfo`), `codegenNcLocalGather` / `codegenNcNGather`
(`InstIndirectCopy` / `InstGather`), and the GenericIndirectLoad/Save family. See
[`front/pipeline`](../front/pipeline.md) for the DMA-bucketing front-end and
[Dynamic-Shape Synthesis](../penguin/dynamic-shape-synthesis.md) for the whole thread.

---

## 8. Evidence summary and limits of this reading

Both bodies are transcribed in full — `assembleDynamicInfo` 603 instructions,
`extractDynamicApToStandAlone` 104 — at their `nm -DC` VAs and with the signatures those
symbols carry. Every offset, immediate, vtable VA, and error string quoted on this page is
read out of the cp310 `libwalrus.so`.

The central structural claims and where they come from:

| Claim | Anchors | Confidence |
|---|---|---|
| scalar→kind-7 / vector→kind-3 split | `mov esi,3` @`0xf202f9` + `IndirectArgExpr` vtable LEA @`0xf20309`; `mov esi,7` @`0xf20619` + `BirIntRuntimeValue` vtable LEA @`0xf2062d`; both vtable VAs match `nm -DC` | CERTAIN |
| `regref` lives at `+0x40` | the `Register*` store @`0xf20634` right after `getRegisterByName`; matches the independently-mapped `IntRuntimeValueBase` `+0x40` | CERTAIN |
| index pre-materialization | `extract…` calls `codegenAccess(bap+0x48)`, clears `bap+0x58`, releases `bap+0x80`; guard strings at `0x1d1d7b0` / `0x1d1d818` | CERTAIN |
| the XOR validator and the dynamic predicate | `0xf140a0` (byte inequality) and `0xf14030` (`bap+0x40 \| bap+0x58`) | CERTAIN |
| `dtype_bytes[]` = `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}` | `qword_1DE98C0` in `.rodata` | CERTAIN |
| `arg_id` is always `1` | hard store @`0xf20310` | HIGH |
| the hoist clears the flag at `+0x58` and releases the ctrl block at `+0x80` | the test/store/release tail of `extract…` | HIGH |
| §7 use-site mapping (MoE routing, dynamic shape) | the error strings `nisa.dma_copy` / `unique_indices` and the codegen-leaf names | MEDIUM |

Three things a reader should keep a reservation about. The `klr::BirAccessPattern` header is
mapped only where these two functions read it — the slots at `+0x10`, `+0x28`, `+0x38`,
`+0x40`, `+0x48`, `+0x58`, `+0x60`, `+0x80` are pinned, the rest are unknown. Whether
`arg_id` ever exceeds 1 is not exercised anywhere in this body (one index per dynamic AP is
all these paths construct), and no NEFF/BIR-JSON fixture was diffed against the serialized
form. And the front-end attribution in §7 is read off error-string wording rather than a
traced front-end fixture.
