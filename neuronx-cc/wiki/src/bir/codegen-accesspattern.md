# Codegen AccessPattern Primitive — `codegenAccess` / `BirAccessPattern`

> *Every symbol, address, offset, and string on this page applies to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310), module `neuronxcc/starfish/lib/libwalrus.so` (ELF64, 64,973,024 bytes; `VA == file-offset` for `.text` @ `0x62d660` and `.rodata` @ `0x1c72000`). The same backend is statically linked into `nki_klr_sim`. Other wheels differ; treat every address as version-pinned. Evidence is the IDA decompiled/disasm sidecars under `neuronx-cc/ida/…libwalrus.so/`. Note the **two-VA-frame artifact**: each function has a 6-byte `.`-prefixed `jmp` thunk at low `.text` (`0x5f…`/`0x61…`) and the real body at `0xf1…`; this page cites the **real bodies**.*

## Abstract

This page documents the **shared klr→BIR access-pattern construction spine** of the **beta2 / klr** codegen path: the one descent that *every* per-op KLR→BIR leaf (the I-ops that lower `nisa`/`nki` tensor instructions) walks to turn a `klr::TensorRef` operand into the `(stride, size)`-pair descriptor a BIR `Instruction` carries. It is the beta2 counterpart of the beta3 Penguin builder family documented in [BirCodeGenLoop Access-Pattern Builders](../nki/bircodegen-ap.md); the two paths converge on the same abstract `(step, num)` AP model but split sharply on *where* the set-algebra runs — beta2's primitive is a **pure relay**, beta3's is not.

The spine is four C++ encoders plus a symbolic side-channel. `codegenTensorRef` → `codegenTensorRefImpl` dispatches on the `klr::TensorRef` kind: SBUF/PSUM accesses go to `codegenAccess`, DRAM/HBM tensors to `codegenHbm`. `codegenAccess` in turn switches the inner `klr::Access` kind — the **general strided** path `codegenBirAccessPattern` (slices, transposes, broadcasts) or the **contiguous fast** path `codegenAccessSimple` (a bare name, no explicit AP list). Both general encoders bottom out in `codegenAP`, the lowest-level materialiser that copies a `std::list<klr::APPair>` verbatim into a `SmallVector<APPair,4>`. All four encoders write the same transient **`InstArg`** operand descriptor that `InstBuilder::addArgument<PhysicalAccessPattern>` later mints into the real `bir::PhysicalAccessPattern` (kind 1).

The one decision that shapes the whole layer is the **static-vs-symbolic split**. The four encoders *always* build a **physical (kind-1)** operand. A **symbolic (kind-2)** AP is produced only on the dynamic path, gated by `isAccessDynamic` at the *call site* (not inside any encoder) and built by `assembleDynamicInfo`, which mints a `pelican::Expr` address-algebra expression — `IndirectArgExpr` (ExprKind 3) for a vector/gather offset, `BirIntRuntimeValue` (ExprKind 7) for a scalar/register offset. That branch is the QUIRK material on this page (§6).

| | |
|---|---|
| **Module** | `libwalrus.so` cp310 (class `neuronxcc::backend::KlirToBirCodegen`) |
| **Dispatch entry** | `codegenTensorRef` `0xf18b30` → `codegenTensorRefImpl` `0xf18960` |
| **`klr::Access` switch** | `codegenAccess` `0xf18820` — kind 4 → `codegenBirAccessPattern`, kind 1 → `codegenAccessSimple` |
| **General strided** | `codegenBirAccessPattern` `0xf18430` (664 B) — ML-by-name + list copy → `codegenAP` |
| **Contiguous fast** | `codegenAccessSimple` `0xf186d0` (331 B) — 1-pair identity AP, shape `{1,1,1}` |
| **List materialiser** | `codegenAP` `0xf13f50` (214 B) — `klr::APPair{i32,u32}` → `bir::APPair{i64,i64}`, pure relay |
| **DRAM path** | `codegenHbm` `0xf15060` (629 B) — 2-dim AP, byte-size ÷ dtype-bytes |
| **Dynamic predicate** | `isAccessDynamic(BirAccessPattern)` `0xf14030` — `[ap+0x40] \| [ap+0x58]` |
| **Symbolic builder** | `assembleDynamicInfo` `0xf20230` (2683 B) — `pelican::Expr` kind 3 / kind 7 |
| **Transient output** | `InstArg` — `{MemLoc*, SmallVector<APPair,4>, partition:i32, dtype:i32, valid:u8}` |
| **Wire consumers** | `addArgument<PhysicalAccessPattern>` `@plt 0x5fcd80` · `addOutput` `@plt 0x5fc5e0` |

This page builds the **transient codegen output**; the **value-object** that consumes it — `bir::PhysicalAccessPattern` and the `Argument` hierarchy — is [BIR Value Model](value-model.md) (note the offset distinction in §5), the on-wire `ADDR4` / `TENSOR1-4D` silicon descriptors are Part 2, and the kind-2→kind-3 lowering is `lower_ap` (cross-ref §7). The dynamic-AP companion `DynamicAPINFO` is detailed in [Dynamic Access Patterns](dynamic-ap.md).

---

## 1. The dispatch spine — the path every I-op walks

A per-op codegen never builds an access pattern inline. It binds each tensor operand through exactly one entry, `codegenTensorRef`, and lets the spine resolve the operand class. The descent is fixed:

```text
codegenTensorRef(ref)                         §1.1   shared_ptr<klr::TensorRef>
  → codegenTensorRefImpl(ref, allow_dynamic)  §1.2   isAccessDynamic gate
      ├ TensorRef.kind == 1 → codegenAccess(ref+0x08)        §1.3   SBUF / PSUM
      │     ├ Access.kind == 4 → codegenBirAccessPattern     §3   general strided
      │     └ Access.kind == 1 → codegenAccessSimple         §2   contiguous fast
      └ TensorRef.kind == 4 → codegenHbm(...)                §4   DRAM / HBM
```

Every leaf produces one `InstArg` (§5); the call site then hands it to `addArgument`/`addOutput<PhysicalAccessPattern>` to mint the real BIR operand. This is the **single funnel**: there is no per-op AP code, so a reimplementation that gets this descent right gets every op's operand binding right for free.

### 1.1 `codegenTensorRef` `0xf18b30` — the entry [CONFIRMED]

Shallow-copies the 16-byte `shared_ptr<klr::TensorRef>` into a stack temp, bumps the strong-count, and tail-calls `codegenTensorRefImpl(this, BB, &temp, /*allow_dynamic=*/0)`. The hard-coded `allow_dynamic = false` is the default contract: **most ops forbid dynamic access.** The dynamic-capable ops (e.g. `nisa.dma_copy` with a runtime offset) bypass this wrapper and call `codegenTensorRefImpl` directly with `allow_dynamic = true`.

### 1.2 `codegenTensorRefImpl` `0xf18960` — the gate + kind switch [CONFIRMED]

```c
// Address 0xf18960 (real body; thunk 0x61bb20)
InstArg codegenTensorRefImpl(shared_ptr<klr::TensorRef> ref, bool allow_dynamic) {
    // --- the static/symbolic GATE (decompiled v10) ---
    bool v10 = isAccessDynamic(ref) & (allow_dynamic ^ 1);   // dynamic AND not-allowed
    if (v10)
        throw std::runtime_error("Dynamic Access is not allowed in the instruction.");

    switch (*(i32*)*ref) {                 // klr::TensorRef kind @ +0x00
      case 1: return codegenAccess(*(ref + 0x08));            // inner klr::Access @ +0x08
      case 4: return codegenHbm(...);                         // DRAM / HBM tensor
      default:
        __assert_fail("false && \"Unsupported tensorRef type encountered!\"",
                      ".../klir_to_bir_codegen.cpp", 0x135, ...);
    }
}
```

The gate is `isAccessDynamic(ref) & (allow_dynamic ^ 1)` — i.e. *dynamic AND not-allowed* throws. The assert string and the `klir_to_bir_codegen.cpp:0x135` source location are present verbatim in the binary; the kind-1 branch loads the inner `klr::Access` shared_ptr at `TensorRef+0x08` and forwards it to `codegenAccess`. [evidence: decompiled body 0xf18960 — `v10 = isAccessDynamic(...) & (a4 ^ 1)`; `v12 == 1` → `codegenAccess`, `v12 != 4` → `__assert_fail`.]

### 1.3 `codegenAccess` `0xf18820` — the `klr::Access` kind switch [CONFIRMED]

```c
// Address 0xf18820 (real body; thunk 0x60bb60)
InstArg codegenAccess(shared_ptr<klr::Access> access) {
    switch (*(i32*)*access) {              // klr::Access kind @ +0x00
      case 4:  return codegenBirAccessPattern(this, BB, access);
                                           //   FULL strided AP (slice / transpose / broadcast)
      case 1:  return codegenAccessSimple(this, BB, /*klr::TensorName=*/ access + 0x08);
                                           //   contiguous fast path; forwards inner TensorName
      default: throw std::runtime_error("Unsupported access type encountered " + to_string(kind));
    }
}
```

The kind-1 branch does **not** forward `access` itself — it loads the inner `klr::TensorName` shared_ptr at `klr::Access+0x08` (the decompiler's `_mm_loadu_si128((__m128i*)(v7 + 2))` = ptr+ctrl at +0x10 byte offset, i.e. +0x08 qword) and passes that to `codegenAccessSimple`. [evidence: decompiled 0xf18820 — `v8 = *(i32*)*a3`, `v8 == 4` → `codegenBirAccessPattern`, `v8 == 1` → loads `a3[1]`/`(v7+2)` → `codegenAccessSimple`, else `"Unsupported access type encountered "` + `to_string`.]

---

## 2. `codegenAccessSimple` `0xf186d0` — the contiguous fast path

Used when `klr::Access.kind == 1`: a bare `klr::TensorName` with no explicit AP list. The encoder synthesises a **single-dim identity AP** spanning the whole tensor along the free axis and never reads an AP list.

```c
// Address 0xf186d0 (real body; thunk 0x61e870)
InstArg codegenAccessSimple(shared_ptr<klr::TensorName> klr) {
    MemoryLocation* ML = Function::getMemoryLocationByName(klr.name);   // BB.Func @ +0x40
    if (!ML) ML = codegenTensorName(klr);          // fallback: mint a fresh MemLoc

    u64 num  = **(u32**)(ML + 0x100);              // ML+0x100 → dims ptr → [0] = elem count
    u32 step = *(u32*)(klrTensorName + 0x48);      // TensorName stride

    InstArg a;
    a.Pattern = SmallVector<APPair,4>{ {step, num} };   // ONE dim; header 0x400000000 (size 0, cap 4)
    a.shape   = SmallVector<u64>{ 1, 1, 1 };            // {1,1,1}; header 0x400000003 (size 3, cap 4)
    a.MemLoc    = ML;                              // InstArg+0x00
    a.partition = 0;                               // InstArg+0x58 = 0 (no partition offset)
    a.dtype     = MemoryLocation::getDtype(ML);    // InstArg+0x5C native dtype (no per-access cast)
    a.valid     = 1;                               // InstArg+0x60
    return a;
}
```

This is the access an op produces when the front-end handed it a name with no sliced AP: one canonical free dim `{step = TensorName.stride, num = ML.elem_count}`, the shape defaulted to `1×1×1`, the partition field zero, and the dtype taken straight from the `MemoryLocation` — **no per-access cast** on this path. [evidence: decompiled 0xf186d0 — `v5 = *MemoryLocationByName[32]` (ML+0x100 deref, `[0]` = num); `v6 = *(u32*)(*a3 + 72)` = `TensorName+0x48` (step); `v10[0]=step, v10[1]=num, v10[2..4]=1,1,1`; `v9 = 0x400000003` (shape header); `a1[5].i32[2] = 0` (partition); `a1[5].i32[3] = getDtype(ML)`; `a1[6].i8[0] = 1` (valid).]

> **QUIRK — Simple reads the element count directly; Hbm divides.** `codegenAccessSimple` lifts `num` straight out of the `MemoryLocation` dims pointer at `ML+0x100`. `codegenHbm` (§4) cannot — a DRAM `MemoryLocation` stores its size *in bytes*, so it divides `ML+0x108` by the per-dtype byte size to recover the element extent. Same abstract AP, two different extent derivations.

---

## 3. `codegenBirAccessPattern` `0xf18430` — the general strided path

Used when `klr::Access.kind == 4`: an explicit `klr::BirAccessPattern` carrying a list of `{step, num}` pairs. This is where **slices, transposes, and broadcasts** live.

### 3.1 `klr::BirAccessPattern` field map [STRONG]

| off | type | meaning |
|---|---|---|
| `+0x00` | `i32` | kind (`== 4` as seen by `codegenAccess`) |
| `+0x10` | `i32` | partition / base-index field → `InstArg.partition` |
| `+0x18` | `ListHead` | `std::list<shared_ptr<klr::APPair>>` sentinel; nodes walked via `[node]` |
| `+0x38` | name | tensor name (resolved via `getMemoryLocationByName`) |
| `+0x40` | `u8` | scalar-dynamic-offset present (register / unique-indices) → ExprKind 7 |
| `+0x58` | `u8` | vector-dynamic-offset present (gather index) → ExprKind 3 |
| `+0x64` | `i32` | dtype-override index (`klr::Dtype`, fed to `codegenDtype`) |
| `+0x68` | `u8` | dtype-override-PRESENT flag |

The offsets are confirmed from the disasm of the four encoders and the validator; the `klr` struct header itself was not separately mapped, so the *field roles* are STRONG (inferred from use) while the *byte offsets* are CONFIRMED.

`klr::APPair` (the 8-byte list payload): `+0x00 i32 step` (signed), `+0x04 u32 num`. The list node is 24 B — `_List_node_base{next,prev}` at `+0x00`/`+0x08`, then `shared_ptr<APPair>{ptr @+0x10, ctrl @+0x18}`; `codegenAP` reads `node+0x10` (the `.get()` pointer).

### 3.2 The body [CONFIRMED]

```c
// Address 0xf18430 (real body; thunk 0x60be00)
InstArg codegenBirAccessPattern(shared_ptr<klr::BirAccessPattern> klr) {
    MemoryLocation* ML = Function::getMemoryLocationByName(klr.name);   // klr+0x38 = name
    if (!ML) ML = codegenTensorName(klr);

    // --- COPY the klr APPair list into a private working list (two passes) ---
    // pass 1: dup klr.list@+0x18 head segment into stack list v18 (tc_new(32) per node,
    //         APPair shared_ptr strong-count bumped); pass 2: dup v18 into list `i`.
    std::list<shared_ptr<klr::APPair>> work;
    for (node in klr.list @ +0x18) {
        n = tc_new(32);  n.sp = node.sp(+0x10/+0x18);  refbump;  work.hook(n);
    }

    SmallVector<APPair,4> ap;  codegenAP(&ap, this, &work);   // §5; header 0x400000000
    work.clear();

    InstArg a;
    a.MemLoc    = ML;                                  // InstArg+0x00
    a.Pattern   = ap;                                  // SmallVector copy via sub_F12B40
    a.partition = *(i32*)(klr + 0x10);                 // InstArg+0x58  partition / base idx
    a.dtype     = klr[+0x68]                            // InstArg+0x5C
                    ? codegenDtype(klr[+0x64])         //   explicit per-access cast
                    : MemoryLocation::getDtype(ML);    //   else native MemLoc dtype
    a.valid     = 1;                                   // InstArg+0x60
    return a;
}
```

[evidence: decompiled 0xf18430 — `getMemoryLocationByName()` ?: `codegenTensorName()`; two `tc_new(32)` hook loops (v18 head segment, then `i`); `codegenAP(&v22, a2, &i)`; `a1[1] = 0x400000000`; `a1[5].i32[2] = *((i32*)*a3 + 4)` = `klr+0x10` (partition); dtype branch `if *((u8*)*a3 + 104)` = `klr+0x68` → `codegenDtype(*((i32*)*a3 + 25))` = `klr+0x64`, else `getDtype(ML)`; `a1[6].i8[0] = 1`.]

### 3.3 What the general path does *not* do

- **No start address.** `codegenBirAccessPattern` carries no byte offset of its own. The base is the `MemoryLocation` (resolved by *name*); the per-dim start is the partition field (`klr+0x10`) plus the AP geometry. The eventual byte offset (the wire `ADDR4` start) is computed downstream by `InstBuilder`/`lower_ap`, not here. [CONFIRMED — no offset store in the body.]
- **No stride scaling.** Each `{step, num}` passes through `codegenAP` verbatim; the dtype→bytes scaling (`qword_1DE98C0[dtype]` = `[1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8]`) happens at the wire encoder, not in codegen.
- **No partition-axis search.** The canonical `[W,Z,Y,X]` axis order means `Pattern[0]` *is* the partition/W axis by construction. The codegen relays the front-end list order plus the scalar partition/base index; there is no "which dim is the 128-partition axis" search. The KLR front-end already ordered the list so `Pattern[0] = partition`.
- **No max-of-inputs dtype promotion.** Dtype is per-operand: either the explicit per-access cast (`klr+0x68` set) or the native `MemoryLocation` dtype. Promotion, if any, is an upstream KLR cast.

---

## 4. `codegenAP` `0xf13f50` — list → `SmallVector<APPair,4>`, the pure relay

The lowest-level materialiser. **No transformation, no broadcast logic, no partition logic** — it copies a `std::list<shared_ptr<klr::APPair>>` into a `SmallVector<APPair,4>`, widening each `{i32, u32}` pair to `{i64, i64}`.

```c
// Address 0xf13f50 (real body; thunk 0x5f51f0)
SmallVector<APPair,4> codegenAP(std::list<shared_ptr<klr::APPair>> list) {
    out.data   = &out.inline_buf;
    out.header = 0x400000000;                  // size = 0, cap = 4  (qword_1DBCE40)
    for (node in list) {
        klr::APPair* p = node[+0x10];          // shared_ptr .get()
        i64 step = (i64)(i32)p[0x00];          // SIGN-extend the 32-bit step  (movsxd)
        i64 num  = (u64)(u32)p[0x04];          // zero-extend the 32-bit num
        if (out.size + 1 > out.cap)
            llvm::SmallVectorBase<u32>::grow_pod();    // spill past 4 dims
        out[out.size] = APPair{step, num};     // 16-byte write {step @+0, num @+8}
        // re-asserts N <= capacity()  ("SmallVector.h":0x5A)
        out.size++;
    }
    return out;
}
```

The **sign-extension of `step` is decisive** and confirmed at the byte level: `0xf13f8f: movsxd r13, dword ptr [rdx]` (step, signed → i64) versus `0xf13f8c: mov ebp, [rdx+4]` (num, 32→64 zero-extend). Negative strides — reverse / descending access — therefore survive into the BIR AP; `num` (an extent) cannot be negative and is zero-extended. [evidence: disasm 0xf13f50 — header from `qword_1DBCE40 = 0x400000000`; `mov rdx,[rbx+10h]` (node+0x10); `movsxd r13,[rdx]`; `mov ebp,[rdx+4]`; stores `[rax]=r13`, `[rax+8]=rbp`; `grow_pod` at 0xf13ff3; `__assert_fail("N <= capacity()", ".../SmallVector.h", 0x5A, ...)`.]

> **QUIRK — a broadcast dim is just `step == 0`, and codegen never inspects it.** `codegenAP` relays a stride-0 entry verbatim. Partition-broadcast (`Pattern[0]`/W) versus free-broadcast (Y/X) is *not* distinguished here — both are zero-step entries, told apart only by *which slot* carries the zero. Codegen neither creates nor validates the broadcast; the KLR front-end mints it. A reimplementer must not add a "broadcast detector" to this layer — there isn't one.

---

## 5. The transient `InstArg` operand descriptor

All four encoders write the **same ~0x68-byte stack struct** — the "PhysicalAccessPattern temp." It is **not** a `bir::AccessPattern`; it is the *recipe* that `InstBuilder::addArgument`/`addOutput<PhysicalAccessPattern>` consume to mint the real `bir::PhysicalAccessPattern`.

| off | field | source |
|---|---|---|
| `+0x00` | `MemoryLocation*` | `getMemoryLocationByName` (or `codegenTensorName` fallback) |
| `+0x08` | `SmallVector<APPair,4>.data` | self-ptr = `&InstArg+0x18` inline buffer |
| `+0x10` | SmallVector header | `size:u32 @+0x10`, `cap:u32 @+0x14`; init `0 / 4` |
| `+0x18` | `APPair[4]` inline buf | 4 × 16 B `{step:i64, num:i64}` (+ `access_shape` SmallVector on the Simple/Hbm variants) |
| `+0x58` | `i32` partition | `klr+0x10`; `0` for Simple / Hbm |
| `+0x5C` | `i32` dtype | `bir::Dtype` — per-access cast or native MemLoc dtype |
| `+0x60` | `u8` valid | set `0` at entry, `1` on success |

[evidence: r13-relative writes in `codegenBirAccessPattern` 0xf185c3..0xf18613 — `a1[5].i32[2]` = +0x58, `a1[5].i32[3]` = +0x5C, `a1[6].i8[0]` = +0x60.]

> **GOTCHA — the `InstArg` SmallVector at `+0x18` is *not* the `bir::AccessPattern` data at `+0x50`.** This page's transient `InstArg` is a stack struct local to codegen; its Pattern SmallVector lives at `InstArg+0x18`. The **minted** value-object, `bir::PhysicalAccessPattern`, is a different thing entirely: it is multiple-inheritance — `bir::Argument` at offset 0 plus a `logging::SrcHandle` vptr at `+0x48`, so the AP's *own* data (the two `SmallVector<APPair,4>`s) resumes at **`+0x50`** ([BIR Value Model §3](value-model.md)). Do not conflate the two `+0x18`/`+0x50` layouts: the `InstArg` is consumed and discarded; the `+0x50` layout is what `addArgument` constructs from it. This page is consistent with 7.4 precisely because it never claims the `InstArg` *is* the AP object.

**How `InstArg` becomes a BIR AccessPattern.** `addArgument<PhysicalAccessPattern>(InstArg)` allocates a `PhysicalAccessPattern` (kind 1) and copies the recipe: `setLocation(InstArg.MemLoc)`, `setPattern(InstArg.Pattern)` (`@libBIR 0x211d40`, APPair stride 16), `setType(InstArg.dtype)`, `setOffset(...)` (computed downstream), partition from `InstArg+0x58`. If the op allowed dynamic access *and* the access was dynamic, the call site first runs `assembleDynamicInfo` (§6) to attach a `DynamicAPINFO` — that is what promotes the operand to the **symbolic (kind-2)** family.

---

## 6. The static-vs-symbolic split — where kind-2 is minted

The four encoders in §2–§4 **always** build a physical (kind-1) operand. A symbolic (kind-2) AP is produced *only* on the dynamic path, and the decision is **not inside any encoder** — it is the `isAccessDynamic` predicate evaluated at the call site (§1.2), where the op decides `allow_dynamic`.

### 6.1 `isAccessDynamic` — the predicate [CONFIRMED]

```c
// Address 0xf14030 — isAccessDynamic(shared_ptr<klr::BirAccessPattern>), 11 bytes
u8 isAccessDynamic(shared_ptr<klr::BirAccessPattern> ap) {
    return *(u8*)(*ap + 0x40)        // scalar-dynamic-offset present
         | *(u8*)(*ap + 0x58);       // vector-dynamic-offset present
}
```

`isAccessDynamic(Access)` `0xf16540` returns false unless `Access.kind == 4`, then extracts the inner `klr::BirAccessPattern` at `Access+0x10` and delegates to the above. So: an access is **dynamic ⇔ it carries a scalar OR vector runtime offset** (an indexed / gather address). Static accesses → physical (kind-1). Dynamic accesses → symbolic (kind-2), evaluated to a `DynamicAPINFO`. [evidence: decompiled 0xf14030 — `result = *(u8*)(*a1 + 64)`, `LOBYTE(result) = *(u8*)(*a1 + 88) | result`; 0xf16540 — `if (*(i32*)*a1 != 4) return 0`, else loads inner AP at `(v2+2)`.]

> **QUIRK — the static/symbolic decision lives at the call site, not in the encoder.** A reimplementer reading `codegenBirAccessPattern` will find no kind-2 branch in it at all. The four encoders are *unconditionally* physical. The static-vs-symbolic fork is the `isAccessDynamic(...) & (allow_dynamic ^ 1)` gate in `codegenTensorRefImpl` (which only ever *throws* on the forbidden case) plus the separate `assembleDynamicInfo` call the dynamic-capable op makes *around* `addArgument`. This mirrors the symbolic-AP→register-ALU split elsewhere in the backend: the symbolic form is built on a side-channel, never folded into the physical builder.

### 6.2 The scalar-XOR-vector invariant [CONFIRMED]

```c
// Address 0xf140a0 — _validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided
void _validate...(shared_ptr<klr::BirAccessPattern> ap) {
    if (*(u8*)(*ap + 0x58) == *(u8*)(*ap + 0x40))   // scalar == vector → fail
        assert_fail(...);
}
```

On the dynamic path, **exactly one** of scalar-offset (`+0x40`) / vector-offset (`+0x58`) is present — never both, never neither. [evidence: decompiled 0xf140a0 — `if (*(u8*)(*a1 + 88) == *(u8*)(*a1 + 64)) sub_6D6825()`.]

### 6.3 `assembleDynamicInfo` `0xf20230` — the symbolic-AP builder [CONFIRMED]

Builds a `bir::DynamicAPINFO` carrying the `pelican::Expr` symbolic address algebra. Two branches, selected by which offset flag is set:

**(A) Vector offset (`klr+0x58` set — a gather index tensor):**
- `pelican::Expr::Expr(/*ExprKind=*/3, ctx)` → `pelican::IndirectArgExpr` (its vtable `_ZTVN7pelican15IndirectArgExprE` is loaded right after the ctor).
- Constraint: `indirect_dim` **must be 0** — string *"When using vector offset, the only supported indirectDim is 0"* is present in the binary. (A gather indexes the W/partition axis.)
- The index AP itself is recursed via `codegenAccess` (through `extractDynamicApToStandAlone`).

**(B) Scalar offset (`klr+0x40` set — a register-backed runtime integer):**
- `Register* r = Function::getRegisterByName(name)`.
- `pelican::Expr::Expr(/*ExprKind=*/7, ctx)` → `bir::BirIntRuntimeValue` (vtable `_ZTVN3bir18BirIntRuntimeValueE` loaded after the ctor); the register backs the offset.

Both branches then build per-axis `bir::QuasiAffineExpr(RefPtr<pelican::Expr>)`, accumulate a `std::vector<pair<QuasiAffineExpr,long>>` (the affine-expr + coefficient list), call `DynamicAPINFO::setActualPattern(SmallVectorImpl<APPair>)` (the resolved concrete AP) and `DynamicAPINFO::setStaticOffsetPortion(long)` (the constant term).

[evidence: disasm 0xf20230 — vector branch `mov esi, 3` @0xf202f9 → `pelican::Expr::Expr(ExprKind, ctx)` @0xf20304, then `mov rax, cs:_ZTVN7pelican15IndirectArgExprE_ptr`; scalar branch `getRegisterByName` @0xf205e4, `mov esi, 7` @0xf20619 → `Expr::Expr` @0xf20624, then `mov rax, cs:_ZTVN3bir18BirIntRuntimeValueE_ptr`; `setActualPattern` @0xf202d7, `setStaticOffsetPortion` @0xf204d1/0xf20753, `QuasiAffineExpr` ctors @0xf2043b/0xf206b3.]

The `ExprKind` immediates — `3` for the vector/`IndirectArgExpr` branch and `7` for the scalar/`BirIntRuntimeValue` branch — are loaded into `esi` directly before each `pelican::Expr::Expr(ExprKind, ctx)` call; this is the binary-level proof of the kind-3 / kind-7 minting.

`extractDynamicApToStandAlone` `0xf1e2c0` asserts `isAccessDynamic` first (*"Compiler Logic Fault: cannot extract dynamic ap from a static access!"*), checks the `unique_indices` (`+0x40`) / `vector_offset` (`+0x58`) coupling (*"When unique_indices=True, it's expected that the vector_offset is set. Check the nisa.dma_copy call"*), then recurses `codegenAccess` on the inner offset AP at `klr+0x48`.

> **NET.** Codegen mints **kind-1** (physical, static) directly from the four encoders, and **kind-2** (symbolic, dynamic) via `assembleDynamicInfo`'s `pelican::Expr` (kind 3 IndirectArg / kind 7 IntRuntime). It **never** mints kind-3 (register) — that is `lower_ap`'s job (§7).

---

## 7. The kind-1 / kind-2 / kind-3 lifecycle

This primitive sits at the *top* of the AP lifecycle that runs codegen → `lower_ap` → wire encoder.

| stage | who | produces |
|---|---|---|
| codegen (per-op) | `codegenAccess[Simple]` / `codegenBirAccessPattern` + `addArgument` | **kind-1** `PhysicalAccessPattern` (static AP) — the common case, every I-op |
| codegen (dynamic) | `assembleDynamicInfo` `0xf20230` | **kind-2** `SymbolicAccessPattern` — `pelican::Expr` kind 3 / kind 7 addrs |
| `lower_ap` (pre-reg-alloc) | `LowerAP::convertSymAP` and friends | **kind-2 → kind-3** `RegisterAccessPattern` (mints virtual registers from the symbolic exprs) |
| encoder / wire | `CoreV*GenImpl` | kind-1 (or the resolved `actual_ap` of a kind-2/3) → `ADDR4` / `TENSOR1-4D` / `MXMEM_PATTERN1D` / `INDIRECT16B` |

`Pattern.size` (1..4) selects `TENSOR1/2/3/4D`; `Pattern[0].step` is the partition step; `Pattern[1..3].step` are the Z/Y/X free strides; the offset becomes the `ADDR4` start address; and the `DynamicAPINFO.indirect_dim` drives the bit-29 indirect descriptors. The codegen primitive on this page is the source of every kind-1/kind-2 operand that flows down this chain.

---

## 8. The master algorithm — one screen

What every I-op invokes per tensor operand:

```c
codegen<Op>(klr::Op op):
  for each tensor slot S (out, in0, in1, …):
    InstArg a = codegenTensorRef(op.S):                         // §1
      ref = op.S;                                               // shared_ptr<klr::TensorRef>
      if (isAccessDynamic(ref) && !op_allows_dynamic) throw;    // §6.1 gate
      switch (ref.kind):                                        // TensorRef+0x00
        1:  acc = ref.inner_access (ref+0x08);                  // klr::Access
            switch (acc.kind):                                  // Access+0x00
              4:  a = codegenBirAccessPattern(acc):             // §3  GENERAL
                     ML        = getMemoryLocationByName(acc.name) ?: codegenTensorName
                     a.Pattern = codegenAP(copy(acc.list@+0x18))  // §4  {i32,u32}→{i64,i64}
                     a.partition = acc[+0x10]
                     a.dtype     = acc[+0x68] ? codegenDtype(acc[+0x64]) : ML.dtype
              1:  a = codegenAccessSimple(acc+0x08):            // §2  CONTIGUOUS
                     ML        = getMemoryLocationByName(name) ?: codegenTensorName
                     a.Pattern = { {step=TensorName[+0x48], num=ML[+0x100][0]} }
                     a.shape   = {1,1,1};  a.partition = 0;  a.dtype = ML.dtype
              else: throw "Unsupported access type"
        4:  a = codegenHbm():                                   // §4 DRAM
                     ML = F.addMemoryLocation(); tag DRAM; num = ML.bytes / dtype_size
        else: assert "Unsupported tensorRef type"
      // dynamic-capable ops (e.g. dma_copy): if dynamic, build DynamicAPINFO via
      // assembleDynamicInfo → pelican::Expr kind3/kind7 → SYMBOLIC kind-2 AP    §6
    addArgument<PhysicalAccessPattern>(a)   // or addOutput — MINTS the bir AP (kind-1)
  // … set AluOp / engine / flags / scalars … addInstruction<bir::Inst…>
```

---

## 9. Cross-checks and confidence

All cross-strand checks are consistent:

- **Value model** ([BIR Value Model](value-model.md)): kind-1 `PhysicalAccessPattern` / kind-2 `SymbolicAccessPattern` / kind-3 `RegisterAccessPattern`; `APPair` 16 B `{step,num}`; the AP value-object's own data at `+0x50` after the MI base. Codegen produces exactly the kind-1 strides + kind-2 `pelican` exprs this struct map describes. The `InstArg+0x18` layout and the AP-object `+0x50` layout are *distinct* (§5 GOTCHA). **[OK]**
- **MemoryLocation**: `+0x100` dims/elem-count ptr (used by Simple); `+0x108` byte size (used by Hbm ÷ dtype). **[OK]**
- **pelican Expr**: ExprKind 3 = `IndirectArgExpr`, ExprKind 7 = `BirIntRuntimeValue` — `assembleDynamicInfo` mints exactly those (`mov esi,3`/`mov esi,7` before each ctor). **[OK]**
- **`lower_ap`**: codegen mints kind-1/kind-2; `lower_ap` converts kind-2→kind-3 before reg-alloc. **[OK]**
- **beta3 twin** ([BirCodeGenLoop AP Builders](../nki/bircodegen-ap.md)): same abstract `(step, num)` AP model, broadcast = stride-0; but beta3 runs the set-algebra *inside* its builders whereas this beta2 `codegenAP` is a pure relay. The two are deliberately parallel, not contradictory. **[OK]**

### Gaps / confidence ledger

| id | conf | claim |
|---|---|---|
| G1 | STRONG | `klr::BirAccessPattern` field *roles* (+0x10 partition, +0x18 list, +0x40 scalar-offset, +0x58 vector-offset, +0x64/+0x68 dtype) from encoder + validator disasm; the byte *offsets* are CONFIRMED, the `klr`-struct header was not separately mapped. |
| G2 | STRONG / INFERRED | `codegenAccessSimple` `num = ML[+0x100][0]` value source is CONFIRMED; whether the degenerate 1-dim extent is the partition vs free extent is INFERRED from the `{1,1,1}` shape default. |
| G3 | CONFIRMED | `codegenAP` `step` is sign-extended (`movsxd`), `num` zero-extended — negative (reverse) strides survive. |
| G4 | CONFIRMED | the kind-1-vs-kind-2 decision is *not* inside `codegenBirAccessPattern`; it is the `isAccessDynamic` gate at the call site + `assembleDynamicInfo`. The four encoders always emit physical. |
| G5 | CONFIRMED | no byte offset is set in codegen; the `ADDR4` byte offset is computed downstream. |

---

## Evidence anchors

```text
codegenTensorRef         0xf18b30  (thunk 0x5ff170)
codegenTensorRefImpl     0xf18960  (thunk 0x61bb20)  isAccessDynamic & (a4^1) gate;
                                   kind1→codegenAccess, kind4→codegenHbm;
                                   assert klir_to_bir_codegen.cpp:0x135
codegenAccess            0xf18820  (thunk 0x60bb60)  kind4→BirAP / kind1→Simple;
                                   "Unsupported access type encountered "
codegenAccessSimple      0xf186d0  (thunk 0x61e870)  ML+0x100 num, TensorName+0x48 step,
                                   shape {1,1,1} hdr 0x400000003, part=0, getDtype(ML)
codegenBirAccessPattern  0xf18430  (thunk 0x60be00)  name klr+0x38, list klr+0x18,
                                   codegenAP @0xf1859e, part klr+0x10, dtype klr+0x68?+0x64
codegenAP                0xf13f50  (thunk 0x5f51f0)  node+0x10, movsxd step @0xf13f8f,
                                   num @0xf13f8c, grow_pod, "N <= capacity()" SmallVector.h:0x5A
codegenHbm               0xf15060  (thunk 0x621ed0)  bytes ÷ qword_1DE98C0[dtype]
isAccessDynamic(BAP)     0xf14030  [ap+0x40] | [ap+0x58]
isAccessDynamic(Access)  0xf16540  Access.kind==4 → inner BAP @ Access+0x10
_validate...XOR          0xf140a0  scalar==vector → assert
extractDynamicApToStandAlone 0xf1e2c0
assembleDynamicInfo      0xf20230  vector: mov esi,3 → IndirectArgExpr; scalar:
                                   getRegisterByName + mov esi,7 → BirIntRuntimeValue;
                                   setActualPattern, setStaticOffsetPortion; "indirectDim is 0"
Constants  qword_1DBCE40 = 0x400000000 (SV size0/cap4); qword_1DBCF28 = 0x400000003
           (shape size3/cap4); qword_1DE98C0 dtype→bytes = [1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8]
libBIR consumers  addArgument<PhysicalAccessPattern> @plt 0x5fcd80;
                  addOutput @plt 0x5fc5e0; PhysicalAccessPattern::setPattern 0x211d40 (libBIR)
```

**Cross-references:** [BIR Value Model](value-model.md) (7.4 — the minted `Argument`/`AccessPattern` value-objects, `+0x50` MI layout) · [BirCodeGenLoop AP Builders](../nki/bircodegen-ap.md) (6.5.14 — the beta3 Penguin twin of this primitive) · [Dynamic Access Patterns](dynamic-ap.md) (7.23 — the `DynamicAPINFO` companion built by `assembleDynamicInfo`).
