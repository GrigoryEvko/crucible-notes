# addrspacecast ISel

> *Every address, offset, intrinsic ID, and string on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ. Addresses are the binary's own VMA (text/rodata VMA == file offset).*

## Abstract

SparseCore needs sixteen distinct *address-space re-tag* operations, one per `(engine-scope, on-tile-pool)` combination its pointers can land in. The MLIR `sc_tpu` layer expresses each one as an `@llvm.tpu.addrspacecast.<tag>` intrinsic — a value-preserving cast whose only effect is to change the LLVM `addrspace(N)` of a pointer, leaving the bits identical. This page documents how those sixteen intrinsics behave at **instruction selection**: where (and whether) they convert to the generic `ISD::ADDRSPACECAST(0xf4)` SelectionDAG node, which `SelectCode` MatcherTable arm matches them, and the full per-cast from→to address-space map for all sixteen.

The single most important — and most counter-intuitive — finding is that **the SparseCore cast intrinsics are *not* converted to `ISD::ADDRSPACECAST(0xf4)`**. The `0xf4` node arises in this binary from exactly one source: a *real* IR `addrspacecast` *instruction*, lowered by the stock `SelectionDAGBuilder::visitAddrSpaceCast`. The sixteen SparseCore casts survive into LLVM-IR as honest intrinsic *calls* (`call ptr addrspace(dst) @llvm.tpu.addrspacecast.X(ptr addrspace(src) %p [, i32 %tileid])`) and reach the DAG as `ISD::INTRINSIC_WO_CHAIN` nodes keyed by their integer intrinsic ID. The `0xf4`/`ISD::ADDRSPACECAST` path (which routes through `TPUTargetLowering::LowerADDRSPACECAST` → the value-preserving `0xf3` register-copy node) serves the TensorCore / generic front-end's real `addrspacecast` instructions and is a **separate mechanism** from the SC intrinsic family. A reimplementer who wires the SC cast intrinsics into `LowerADDRSPACECAST` will produce a backend that never matches and traps with `CannotYetSelect`.

This page owns three things: the **IR→ISD conversion site** (the `getAddrSpaceCast` → `getNode(0xf4)` producer and its sole real-instruction caller), the **MatcherTable / `SelectCode` arm** that the cast `INTRINSIC_WO_CHAIN` nodes fall into, and the **16-intrinsic from→to AS map**. The 160/128/192-bit fat-pointer struct these casts re-tag lives on [Fat Pointers (AS7/8/9)](fat-pointers-as789.md); the two-operand `(base, tileid)` cast body lives on [Tile-ID Cast](tile-id-cast.md) — this page links, it does not re-derive either.

For reimplementation, the contract is:

- **The sixteen casts are LLVM intrinsics, not `addrspacecast` instructions.** Intrinsic IDs `0x33b0..0x33bf` (13232..13247), contiguous, alphabetically sorted, bracketed by `llvm.tpu.addcarry` (`0x33af`) and `llvm.tpu.alloca.dreg` (`0x33c0`). They survive translation as real intrinsic *calls*.
- **`ISD::ADDRSPACECAST` is opcode `244` (`0xf4`)**, produced only by `SelectionDAG::getAddrSpaceCast` (`0x192E2360`) — whose sole real caller is the IR-instruction handler `visitAddrSpaceCast` (`0x19333020`). No SC code emits an IR `addrspacecast` or a `0xf4` node.
- **The cast IDs hit the matcher's `INTRINSIC_WO_CHAIN` (opcode-48) arm but have no pattern there.** In `TPUDAGToDAGISel::Select` (`0x13B69640`) all sixteen fall through to `SelectCodeCommon` (MatcherTable size `0x37CAC`); the matcher has no cast-ID arm, so a cast node that reaches the matcher unfolded would `CannotYetSelect`.
- **The from→to map is encoded in the intrinsic *name suffix*.** Each `.<tag>` (and `.<src>.tile.<tag>` multi-segment form) names the destination engine-scope / tile pool; the cast re-tags the pointer's `addrspace(N)` to that pool's LLVM address space.
- **Operand arity is set by the destination *sequencer scope*, not by the `.tile.` infix.** Nine of the sixteen — the casts whose destination is a **tile-accessing SC engine (TEC or TAC)** — carry the `NOperands<2u>` trait and take a second `i32 tileid` operand: the seven TEC-scoped (`.smem`, `.spmem`, `.tec`, `.smem.tile.tec`, `.sflag.tile.tec`, `.sflag.tile.sflag.tec`, `.tec.sflag.tec`) plus the two TAC-scoped (`.tac`, `.sflag.tile.tac`). The other seven (the SCS/TC-scoped casts, including the `.tile.`-infixed `.sflag.tile.scs` / `.smem.tile.scs` / `.sflag.tile.sflag.scs`) carry `OneOperand` and are single-operand re-tags. The `.tile.` token does **not** add the operand — TEC and TAC scope do.

| | |
|---|---|
| **Cast intrinsic IDs** | `0x33b0..0x33bf` (13232..13247), 16 contiguous, alphabetical |
| **Intrinsic name range** | `llvm.tpu.addrspacecast` … `llvm.tpu.addrspacecast.tec.sflag.tec` |
| **MLIR cast source** | `MemorySpaceCastOpLowering::matchAndRewrite` (`0x135A5C20`) — elide-or-emit |
| **IR→intrinsic emit** | `convertOperationImpl` (`0x15140240`, IDA VMA) → `createIntrinsicCall` (`0x1683F440`) |
| **`ISD::ADDRSPACECAST`** | opcode `244` (`0xf4`); producer `SelectionDAG::getAddrSpaceCast` (`0x192E2360`) |
| **Sole `0xf4` caller** | `SelectionDAGBuilder::visitAddrSpaceCast` (`0x19333020`) — real IR instruction handler |
| **TPU Select** | `TPUDAGToDAGISel::Select` (`0x13B69640`); cast IDs → `SelectCodeCommon` default |
| **MatcherTable** | size `0x37CAC` (228 524 B); opcode-48 = `ISD::INTRINSIC_WO_CHAIN` (no cast arm) |
| **`0xf4` lowering** | `LowerOperation` (`0x13B70AA0`) opcode `244` → `LowerADDRSPACECAST` (`0x13B70480`) |
| **Confidence** | CONFIRMED (decompile-anchored) unless a row or callout says otherwise |

---

## The Two Address-Space-Cast Mechanisms

### Purpose

The defining fact of SparseCore addrspacecast ISel is that there are **two unrelated mechanisms** that both carry the words "address space cast", and conflating them is the single trap on this page. A reimplementer must keep them apart from the start, so this unit names both before any detail.

### The two paths

```text
MECHANISM A — the SparseCore cast-intrinsic family  (THIS PAGE owns the ISel)
─────────────────────────────────────────────────────────────────────────────
  sc_tpu.<op>                                        MLIR ScDialect op
    │  LowerToSparseCoreLlvm / LlvmTpu dialect
    ▼
  @llvm.tpu.addrspacecast.<tag>(ptr %p [, i32 %tid]) REAL IR intrinsic CALL
    │  (16 IDs 0x33b0..0x33bf — survives into LLVM-IR; NOT an IR addrspacecast)
    ▼
  ISD::INTRINSIC_WO_CHAIN  (DAG node keyed by intrinsic ID)
    │  TPUDAGToDAGISel::Select opcode-'0' (=48) arm
    ▼
  SelectCodeCommon  (no cast-ID matcher pattern → consumed/folded, see below)

MECHANISM B — a generic IR `addrspacecast` instruction  (SEPARATE; not the SC intrinsic)
─────────────────────────────────────────────────────────────────────────────
  addrspacecast ptr addrspace(s) %p to ptr addrspace(d)   real IR instruction
    │  SelectionDAGBuilder::visitAddrSpaceCast 0x19333020
    ▼
  SelectionDAG::getAddrSpaceCast 0x192E2360 → getNode( opcode 244 = 0xf4 )
    │  TPUTargetLowering::LowerOperation 0x13B70AA0  (else if v7 == 244)
    ▼
  TPUTargetLowering::LowerADDRSPACECAST 0x13B70480  → value-preserving 0xf3 reg-copy
```

> **GOTCHA —** the MLIR `MemorySpaceCastOpLowering` (`0x135A5C20`) is the *only* place a generic `llvm.addrspacecast` *instruction* can enter the SparseCore pipeline, and it does so only on its failure edge (§Conversion Site). The sixteen `@llvm.tpu.addrspacecast.*` *intrinsics* are emitted by a wholly different mechanism — the LlvmTpu dialect translation — and never become a `0xf4` node. A backend that lowers the cast intrinsics through `LowerADDRSPACECAST` is wired to mechanism B for an input that only ever travels mechanism A.

### Why two mechanisms

The split exists because the two casts mean different things. Mechanism B re-tags a pointer between LLVM address spaces that *change the pointer representation* (e.g. a fat-pointer struct vs a flat integer), so it needs a lowering that materialises the representation change — the `0xf3` value-preserving register copy with an `MVT::i32` carrier. Mechanism A's sixteen casts are *type-system bookkeeping*: they re-tag a pointer between SparseCore engine-scope / tile pools whose LLVM representation is identical, so the cast is a no-op the consuming load/store absorbs. Keeping them as opaque intrinsics (rather than `addrspacecast` instructions) is what lets the SC backend carry the engine-scope tag through optimization without the generic `InferAddressSpaces` / `addrspacecast` machinery folding it away prematurely.

---

## Conversion Site — MLIR Op → Intrinsic, and the `0xf4` Severance

### Purpose

"The IR→`ISD::ADDRSPACECAST` conversion site" has two halves a reimplementer must trace: (1) where the MLIR cast op becomes the surviving `@llvm.tpu.addrspacecast.*` intrinsic call, and (2) the proof that this call is *never* converted to `ISD::ADDRSPACECAST(0xf4)`. Both are byte-anchored below.

### The MLIR cast op — elide or fall through

The ScDialect cast op (`memref.memory_space_cast`, as the SparseCore type system sees it) is rewritten by `MemorySpaceCastOpLowering::matchAndRewrite` (`0x135A5C20`). Its body is a three-line elide-or-emit decision, byte-confirmed:

```c
function MemorySpaceCastOpLowering_matchAndRewrite(op, adaptor, rewriter):  // 0x135A5C20
    src_llvm = TypeConverter::convertType(srcPointerType)   // 0x135A5C20:22
    dst_llvm = TypeConverter::convertType(resultType)        // 0x135A5C20:23
    if src_llvm != dst_llvm:
        return failure                                       // → generic pattern emits a REAL llvm.addrspacecast
    rewriter.replaceOp(op, adaptor.operand)                  // ELIDE: identical LLVM ptr type → drop the cast
    return success
```

> **NOTE —** this MLIR pattern is mechanism B's gate, not mechanism A's. When `convertType(src) == convertType(dst)` (the two SC spaces collapse to the same `!llvm.ptr`) the cast is elided outright. When they differ, the pattern *fails*, and the generic `ConvertOpToLLVMPattern` emits a real `llvm.addrspacecast` *instruction* — which then travels mechanism B to `0xf4`. The sixteen `@llvm.tpu.addrspacecast.*` *intrinsics* do **not** originate here; they are emitted by the LlvmTpu dialect translation that lowers each `sc_tpu` op to its registered intrinsic. The per-ID `convertType` collapse map (which of the 21 SC address-space IDs share an LLVM addrspace) is owned by [Tile-ID Cast](tile-id-cast.md).

### The intrinsic-emission site (links to the per-op dispatcher)

The sixteen SparseCore casts become real IR intrinsic *calls* in the LlvmTpu dialect's op→IR translation. `convertOperationImpl` (`0x15140240` in the IDA-rebased dump) is a ~1349-arm op-identity dispatcher; each cast arm tails into the shared trampoline that calls `mlir::LLVM::detail::createIntrinsicCall` (`0x1683F440`) with the cast's intrinsic ID. `createIntrinsicCall` is the uniform op→intrinsic-call emitter, byte-confirmed to materialise a genuine declaration + call:

```c
function createIntrinsicCall(builder, modTrans, op, intrinsicID, numResults, ...):  // 0x1683F440
    operands = ModuleTranslation::lookupValues(op.operands)     // 0x1683F440:348 / :422 (base [, i32 tileid])
    decl     = Intrinsic::getOrInsertDeclaration(Module, intrinsicID, overloadTypes)  // 0x1683F440:656
    call     = IRBuilderBase::CreateCall(builder, decl, operands)                      // 0x1683F440:669
    return call    // %r = call ptr addrspace(dst) @llvm.tpu.addrspacecast.X(ptr addrspace(src) %base [, i32 %tid])
```

> **NOTE —** the op→intrinsic-ID dispatcher mechanism (all 1349 arms, the trampoline, the operand/result mapping) is a dialect-translation concern, not an ISel concern; this page cites it only to establish that the cast reaches the DAG as an `INTRINSIC_WO_CHAIN` call rather than an IR `addrspacecast`. The two-operand `(base, i32 tileid)` calls — the nine TEC- and TAC-scoped casts — are detailed on [Tile-ID Cast](tile-id-cast.md).

### The `0xf4` producer and its sole caller

`ISD::ADDRSPACECAST` is SelectionDAG opcode **244** (`0xf4`). The only producer is `SelectionDAG::getAddrSpaceCast` (`0x192E2360`), byte-confirmed building a node with that opcode:

```c
function SelectionDAG_getAddrSpaceCast(loc, vt, ptr, srcAS, dstAS):   // 0x192E2360
    AddNodeIDNode(&id, 244 /* ISD::ADDRSPACECAST */, vtlist, &ops, 1)  // 0x192E2360:30
    ... // CSE lookup
    node->opcode = 244                                                 // 0x192E2360:70
    return node
```

Its sole meaningful caller is `SelectionDAGBuilder::visitAddrSpaceCast` (`0x19333020`) — the handler for a *real IR `addrspacecast` instruction*. No TPU/SparseCore code path calls `getAddrSpaceCast`, constructs an `AddrSpaceCastInst`, or emits a `0xf4` node. Therefore a SparseCore cast intrinsic can only become a `0xf4` node if some pass first rewrote it into an IR `addrspacecast` instruction — and none does.

### The `0xf4` lowering arm (the mechanism-B destination)

When a real `addrspacecast` instruction *does* reach the DAG, `TPUTargetLowering::LowerOperation` (`0x13B70AA0`) catches opcode `244` and forwards it, byte-confirmed:

```c
function TPUTargetLowering_LowerOperation(op, dag):   // 0x13B70AA0
    v7 = op.getOpcode()
    ...
    else if (v7 == 244)                                // 0x13B70AA0:73  ISD::ADDRSPACECAST
        return TPUTargetLowering::LowerADDRSPACECAST(op, dag)   // 0x13B70480
```

`LowerADDRSPACECAST` (`0x13B70480`) rewrites the `0xf4` node into the SparseCore value-preserving `0xf3` register-copy node (the legality matrix and the `0xf3` register-copy matcher arm are TensorCore-front concerns, out of scope here). The arm exists; it is simply never reached by the cast-intrinsic family.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `MemorySpaceCastOpLowering::matchAndRewrite` | `0x135A5C20` | MLIR cast: elide if `convertType` equal, else fail → generic `llvm.addrspacecast` | CONFIRMED |
| `convertOperationImpl` | `0x15140240` (IDA VMA) | LlvmTpu op→IR dispatcher; cast arms → `createIntrinsicCall` | CONFIRMED |
| `mlir::LLVM::detail::createIntrinsicCall` | `0x1683F440` | emit real intrinsic call (`getOrInsertDeclaration` + `CreateCall`) | CONFIRMED |
| `SelectionDAG::getAddrSpaceCast` | `0x192E2360` | the only `getNode(244)` / `0xf4` producer | CONFIRMED |
| `SelectionDAGBuilder::visitAddrSpaceCast` | `0x19333020` | sole caller of `getAddrSpaceCast` — real IR-instruction handler | CONFIRMED |
| `TPUTargetLowering::LowerOperation` | `0x13B70AA0` | opcode `244` → `LowerADDRSPACECAST`; opcode 48 NOT lowered | CONFIRMED |
| `TPUTargetLowering::LowerADDRSPACECAST` | `0x13B70480` | `0xf4` → value-preserving `0xf3` register-copy node | CONFIRMED |

---

## The MatcherTable / `SelectCode` Arm

### Purpose

A SparseCore cast intrinsic reaches the DAG as an `ISD::INTRINSIC_WO_CHAIN` node whose child-0 is the integer intrinsic ID. This unit documents which `TPUDAGToDAGISel::Select` arm the node enters and what happens to it — the answer being "the `SelectCodeCommon` default, where no matcher pattern claims it".

### Entry Point

```text
TPUDAGToDAGISel::Select (0x13B69640)            ── per-node ISel entry
  └─ switch on node opcode (low byte)
       case '0' (= 48 = ISD::INTRINSIC_WO_CHAIN)
         └─ switch on intrinsic ID v68 (= node->op0 constant)
              ├─ 13216..13222  ── ReplaceAllUses / cross-lane reg-class (special handlers)
              ├─ 13223..13465  ── (the big default group) ── goto LABEL_118
              │     ▲ the 16 cast IDs 13232..13247 live HERE
              ├─ 13227/13229/13293/13297/...  ── selectCrossLane / selectCMask / selectErf (special)
              └─ default       ── selectCrossLaneIntrinsic / selectErfIntrinsic
       LABEL_118: SelectCodeCommon(this, node, MatcherTable, 0x37CAC, OperandLists)
```

### Algorithm — the cast IDs fall to the SelectCode default

`TPUDAGToDAGISel::Select` (`0x13B69640`) dispatches first on the node opcode (the decompiler renders the `INTRINSIC_WO_CHAIN` arm as `case '0'`, the ASCII for the low byte of opcode 48). Inside that arm it reads the intrinsic ID and switches on it. The sixteen cast IDs `13232..13247` (`0x33b0..0x33bf`) sit inside one large contiguous fall-through block (cases `13225/13226/13231/13232 … 13465`) whose terminator at `0x13B69640:422` is `goto LABEL_118` — the `SelectCodeCommon` tail-call:

```c
function TPUDAGToDAGISel_Select(node):                    // 0x13B69640
    switch (node.opcode_low_byte):
      case 48 /* ISD::INTRINSIC_WO_CHAIN */:              // 0x13B69640:160 ('0')
          intNo = node.op0_constant                        // :167  v68
          switch (intNo):
            case 13216..13220: ReplaceAllUsesOfValueWith(...); return   // :171  special
            case 13221/13222:  reg_class = (cond ? XRFPR2 : XRFPR0);
                               goto selectCrossLane                       // :178
            // --- the big default group, INCLUDING all 16 cast IDs ---
            case 13225/13226/13231/13232/.../13247/.../13465:            // :221..:421
                goto LABEL_118                                            // :422
            case 13227/13229: ... selectCrossLane                         // :423  special
            case 13293:       selectCMask(node); return                  // :431  special
            ...
    LABEL_118:
        SelectCodeCommon(this, node,
                         MatcherTable /* &GOT - 521848896 */, 0x37CAC,    // :509
                         TPUDAGToDAGISel::SelectCode::OperandLists)
```

So every cast intrinsic node that survives to `Select` is handed to the generic `SelectCodeCommon` MatcherTable interpreter — the same arm that handles every TPU intrinsic without a dedicated `Select` C++ handler. The cast IDs get **no** special-case C++ handling (unlike, e.g., `13221/13222` cross-lane, `13293` `selectCMask`, or `13227/13229`).

### The matcher has no cast-ID pattern

`SelectCodeCommon` walks the MatcherTable (size `0x37CAC` = 228 524 bytes). Its top-level `OPC_SwitchOpcode` has an arm for opcode 48 (`ISD::INTRINSIC_WO_CHAIN`) that matches ~150 TPU load/store/vector/scalar/sync intrinsics by child-0 integer ID — but the sixteen cast IDs `0x33b0..0x33bf` are **absent** from it (and from every other arm). A cast `INTRINSIC_WO_CHAIN` node reaching the matcher unfolded would therefore `CannotYetSelect`.

> **QUIRK —** opcode 48 in the matcher *is* `ISD::INTRINSIC_WO_CHAIN`, and it *is* present (a multi-kilobyte arm matching ~150 intrinsics) — it simply does not list the cast IDs. The earlier reading that "`INTRINSIC_WO_CHAIN` is absent from the matcher" was wrong; the absence is specific to the cast IDs, not the opcode. A reimplementer who adds a TableGen pattern for the cast intrinsics expecting them to match like loads will find no slot reserved — by design, because the cast is meant to be consumed before it reaches the matcher.

### How the cast is discharged before the matcher

The sixteen casts are value-preserving: the result pointer equals the operand pointer with a different `addrspace` tag. The intended discharge (HIGH confidence, not a single byte-trace) is that the **consuming** SparseCore load/store ISel pattern reads *through* the cast's pointer operand (plus, for the TEC/TAC-scoped two-operand casts, the separate `i32 tileid` operand), so the cast node is dead and DCE'd before `Select` ever sees it — or it is folded by the generic DAGCombiner's `visitINTRINSIC_WO_CHAIN` pointer pass-through. Either way the cast never needs a matcher pattern. The exact discharge stage (consumer-pattern fold vs generic combiner DCE) is the one open machine-level link; the negatives — no `0xf4` conversion, no matcher arm, no special `Select` handler — are instruction-grade firm.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `TPUDAGToDAGISel::Select` | `0x13B69640` | per-node ISel; opcode-48 intrinsic dispatch | CONFIRMED |
| `SelectionDAGISel::SelectCodeCommon` | (tail-called) | MatcherTable interpreter; `0x37CAC`-byte table | CONFIRMED |
| MatcherTable opcode-48 arm | (in table) | `ISD::INTRINSIC_WO_CHAIN`; ~150 IDs, **no** cast IDs | HIGH |
| `selectCrossLaneIntrinsic` / `selectCMask` / `selectErfIntrinsic` | `0x13B6B940` / `0x13B6C6E0` / `0x13B6B480` | the special-handler intrinsics (NOT the casts) | CONFIRMED |

---

## The 16-Intrinsic from→to AS Map

### Purpose

Each cast intrinsic's behaviour is fully determined by its name suffix, which names the destination engine-scope and (for the `.tile.`-infixed forms) the per-tile pool re-tag. This unit gives the complete from→to address-space map for all sixteen, anchored to the SparseCore address-space ID table.

### The naming grammar

The suffix after `llvm.tpu.addrspacecast` is the **destination tag**. Three shapes occur, all confirmed from the sixteen name strings in the binary:

```text
llvm.tpu.addrspacecast                       no suffix  →  generic/default re-tag
llvm.tpu.addrspacecast.<engine|pool>         1 segment  →  re-tag to that engine-scope / pool
llvm.tpu.addrspacecast.<srcpool>.tile.<eng>  3 segment  →  per-tile-pool re-tag, named src + dst scope
```

The engine tags are `scs` / `tac` / `tec` (the SparseCore sub-engine whose scope the pointer is being viewed in) and `tc` (the TensorCore). The pool tags are `smem` / `spmem` / `sflag`. The `.tile.` infix marks a **per-tile-pool** re-tag (the pointer is re-tagged into a tile-private pool, viewed in the named destination engine scope). The `.tile.` token does *not* by itself imply a second operand — operand arity is governed by the destination sequencer scope: the tile-accessing engines TEC and TAC take a `tileid`, SCS and TC do not (next callout).

> **QUIRK — operand arity follows the destination sequencer, not the name's `.tile.` infix.** A cast takes a second `i32 tileid` operand iff its destination scope is a tile-accessing SC engine — **TEC or TAC** (the nine casts carrying the `NOperands<2u>` trait, byte-confirmed from each op class's trait list in the binary). SCS/TC-scoped casts carry `OneOperand` and are single-operand even when they carry `.tile.` — e.g. `.sflag.tile.scs` (`0x33b3`) and `.smem.tile.scs` (`0x33b9`) are single-operand, while `.smem` (`0x33b8`), `.spmem` (`0x33bb`), `.tec` (`0x33be`), and the two TAC casts `.tac` (`0x33bc`) / `.sflag.tile.tac` (`0x33b6`) are two-operand. The two-operand casts' lowering body is owned by [Tile-ID Cast](tile-id-cast.md).

> **QUIRK —** the suffix names the *destination* scope, not the source. The cast is value-preserving, so the source address space is whatever the operand pointer carries; the intrinsic only asserts the new tag. The ID is purely alphabetical (it is the Intrinsic name-table position), so adjacent IDs are *not* semantically adjacent — `0x33b6` (`.sflag.tile.tac`) and `0x33b7` (`.sflag.tile.tec`) differ only by destination engine, while `0x33bb` (`.spmem`) and `0x33bc` (`.tac`) are unrelated.

### The complete map

The destination address space is read from the SparseCore address-space ID table (owned by the address-space catalog; reproduced here only for the IDs these casts target). "AS" columns are LLVM address-space numbers; engine-scope tags re-tag to the per-scope alias of a pool rather than a single fixed number, so those rows give the scope rather than one ID.

The **Operands** column is byte-confirmed from each op class's operand trait (`NOperands<2u>` ⇒ two, `OneOperand` ⇒ one). The nine two-operand casts are exactly the TEC- and TAC-scoped set.

| ID | hex | Intrinsic name | Operands | from (src AS) | to (dst scope / AS) | Confidence |
|---|---|---|---|---|---|---|
| 13232 | `0x33b0` | `llvm.tpu.addrspacecast` | `ptr` | any | generic / default pointer re-tag | CONFIRMED |
| 13233 | `0x33b1` | `…​.scs` | `ptr` | any | SCS engine scope (scalar-sequencer view) | CONFIRMED (name/arity) / HIGH (scope) |
| 13234 | `0x33b2` | `…​.scs.sflag.scs` | `ptr` | sflag | SFLAG, SCS scope (`sflag_scs`, AS 223) | CONFIRMED |
| 13235 | `0x33b3` | `…​.sflag.tile.scs` | `ptr` | sflag-tile | SFLAG tile pool, SCS view (`sflag_tile` AS 217) | CONFIRMED |
| 13236 | `0x33b4` | `…​.sflag.tile.sflag.scs` | `ptr` | sflag-tile | SFLAG tile → `sflag_scs` (AS 223), SCS scope | CONFIRMED |
| 13237 | `0x33b5` | `…​.sflag.tile.sflag.tec` | `ptr, i32 tid` | sflag-tile | SFLAG tile → sflag, TEC scope | CONFIRMED |
| 13238 | `0x33b6` | `…​.sflag.tile.tac` | `ptr, i32 tid` | sflag-tile | SFLAG tile pool, TAC view (`sflag_tile` AS 217) | CONFIRMED |
| 13239 | `0x33b7` | `…​.sflag.tile.tec` | `ptr, i32 tid` | sflag-tile | SFLAG tile pool, TEC view (`sflag_tile` AS 217) | CONFIRMED |
| 13240 | `0x33b8` | `…​.smem` | `ptr, i32 tid` | any | SMEM (AS 0 / `smem`), TEC scope | CONFIRMED |
| 13241 | `0x33b9` | `…​.smem.tile.scs` | `ptr` | smem-tile | per-tile SMEM (`smem_tile`/`TileSmem` AS 219), SCS scope | CONFIRMED |
| 13242 | `0x33ba` | `…​.smem.tile.tec` | `ptr, i32 tid` | smem-tile | per-tile SMEM (`smem_tile`/`TileSmem` AS 219), TEC scope | CONFIRMED |
| 13243 | `0x33bb` | `…​.spmem` | `ptr, i32 tid` | any | SPMEM (chip-shared SC SRAM, AS 202), TEC scope | CONFIRMED |
| 13244 | `0x33bc` | `…​.tac` | `ptr, i32 tid` | any | TAC engine scope (tile-access-core view) | CONFIRMED (name/arity) / HIGH (scope) |
| 13245 | `0x33bd` | `…​.tc` | `ptr` | any | TensorCore scope (cross-engine handoff) | CONFIRMED (name/arity) / HIGH (scope) |
| 13246 | `0x33be` | `…​.tec` | `ptr, i32 tid` | any | TEC engine scope (tile-execute view) | CONFIRMED |
| 13247 | `0x33bf` | `…​.tec.sflag.tec` | `ptr, i32 tid` | sflag | SFLAG, TEC scope (`.tec` engine + sflag pool) | CONFIRMED |

> **NOTE —** the intrinsic *names* and their contiguous ID assignment (`0x33b0..0x33bf`, bracketed by `0x33af llvm.tpu.addcarry` and `0x33c0 llvm.tpu.alloca.dreg`) are CONFIRMED byte-exactly from the binary's Intrinsic name table. The **operand arity** is also CONFIRMED — read from each op class's operand trait (the nine `NOperands<2u>` two-operand casts are precisely the TEC- and TAC-scoped set; the seven `OneOperand` casts are SCS/TC-scoped). The pool address-space numbers `217` (`sflag_tile`), `219` (`smem_tile`/`TileSmem`), `223` (`sflag_scs`), `202` (`spmem`), `0` (`smem`) are CONFIRMED from the cast-lowering drivers (`CastSflagPointerToSflagAny` `0x135b8a00`, `CastTileSmemPointerToSmem` `0x135b86e0`). The remaining HIGH items are the per-suffix *scope* meaning for the bare engine tags (`.scs`/`.tac`/`.tc`): the exact LLVM address-space *number* a given engine-scope tag resolves to is the per-scope alias selection owned by [Fat Pointers (AS7/8/9)](fat-pointers-as789.md) and the address-space catalog; this page resolves the *pool*, not the per-scope numeric alias.

### The two-operand (TEC/TAC-scoped) subset

Nine of the sixteen take a second `i32 tileid` operand — the casts whose destination is a tile-accessing SC engine. Seven are TEC-scoped: `0x33b5` (`.sflag.tile.sflag.tec`), `0x33b7` (`.sflag.tile.tec`), `0x33b8` (`.smem`), `0x33ba` (`.smem.tile.tec`), `0x33bb` (`.spmem`), `0x33be` (`.tec`), `0x33bf` (`.tec.sflag.tec`); two are TAC-scoped: `0x33b6` (`.sflag.tile.tac`), `0x33bc` (`.tac`). All nine carry the `NOperands<2u>` op trait; the other seven carry `OneOperand`. The remaining seven are single-operand re-tags, including the three SCS-scoped `.tile.`-infixed forms (`.sflag.tile.scs`, `.sflag.tile.sflag.scs`, `.smem.tile.scs`) and the bare `.scs` / `.scs.sflag.scs` / `.tc` casts. The two-operand cast *body* — how `(base, tileid)` resolves to a tile-local physical address — is documented in full on [Tile-ID Cast](tile-id-cast.md); this page records only that a **TEC or TAC** destination scope is the arity discriminator and that the second operand is the tile selector.

> **GOTCHA —** do not infer the operand count from the `.tile.` token. `…​.sflag.tile.scs` (`0x33b3`, **1 operand**) and `…​.sflag.tile.tec` (`0x33b7`, **2 operands**) both carry `.tile.` and both target an SFLAG-tile pool, yet only the TEC-scoped one takes a `tileid`. The `.sflag.tile.tac` (`0x33b6`) and bare `.tac` (`0x33bc`) casts are **also 2-operand** despite TAC's `.tile.`-less bare form. Conversely `…​.smem` (`0x33b8`) and `…​.spmem` (`0x33bb`) carry no `.tile.` at all and are 2-operand. The arity discriminator is whether the destination **sequencer scope** is a tile-accessing engine — TEC or TAC ⇒ `NOperands<2u>` ⇒ `(base, i32 tileid)`; SCS/TC ⇒ `OneOperand` — not the literal `.tile.` token.

---

## Considerations

A reimplementer building the SparseCore addrspacecast ISel must do these things, in order, and avoid the trap baked into the name:

- **Declare sixteen intrinsics, not one `addrspacecast` instruction.** Reserve contiguous Intrinsic IDs in alphabetical position (between `addcarry` and `alloca.dreg` in this build), and emit them as ordinary intrinsic calls from the dialect translation — never as IR `addrspacecast` instructions. Mark them value-preserving (`IntrNoMem`, the cast's result equals its pointer operand) so the combiner can fold them.
- **Do not wire them to `LowerADDRSPACECAST`.** That arm exists for the generic IR `addrspacecast` instruction only (mechanism B). Routing the cast intrinsics there is the single most likely mistake and produces a backend that never selects the casts.
- **Give them no matcher pattern; rely on consumer absorption.** The casts are meant to be folded into the consuming load/store (which reads through the pointer and the separate `tileid`). If a cast somehow survives to the matcher, the correct behaviour is `CannotYetSelect` — that signals a missing consumer fold, not a missing cast pattern.
- **Drive the from→to map off the name suffix, and the arity off the destination scope.** The `.scs`/`.tac`/`.tec`/`.tc` engine tags select the per-scope alias of the destination pool; the pool tags (`smem`/`spmem`/`sflag`) name the physical tier. The second `i32 tileid` operand is present iff the destination scope is a tile-accessing engine — **TEC or TAC** (the nine `NOperands<2u>` casts), and routes through the tile address resolver — *not* keyed off the `.tile.` token, which appears on single-operand SCS-scoped casts too.

The discharge stage of a surviving cast (consumer-pattern fold vs generic `visitINTRINSIC_WO_CHAIN` DCE) is not byte-traced here and is the one open link; it does not affect the ISel contract above, because in both cases the cast never needs a matcher pattern and never becomes a `0xf4` node.

---

## Related Components

| Name | Relationship |
|---|---|
| `MemorySpaceCastOpLowering::matchAndRewrite` (`0x135A5C20`) | the MLIR cast op: elide or fall through to a generic `llvm.addrspacecast` (mechanism B's gate) |
| `createIntrinsicCall` (`0x1683F440`) | emits the sixteen casts as surviving intrinsic calls (mechanism A's emit) |
| `SelectionDAG::getAddrSpaceCast` (`0x192E2360`) | the sole `ISD::ADDRSPACECAST(0xf4)` producer — never reached by the SC casts |
| `TPUDAGToDAGISel::Select` (`0x13B69640`) | routes the cast `INTRINSIC_WO_CHAIN` nodes to the `SelectCodeCommon` default |
| `TPUTargetLowering::LowerADDRSPACECAST` (`0x13B70480`) | the `0xf4` → `0xf3` lowering for generic IR `addrspacecast` (mechanism B's body) |

## Cross-References

- [Fat Pointers (AS7/8/9)](fat-pointers-as789.md) — the 160/128/192-bit structured pointer the casts re-tag, and the per-scope address-space alias numbers.
- [Tile-ID Cast](tile-id-cast.md) — the two-operand `(base, i32 tileid)` cast body (the nine TEC- and TAC-scoped casts among these sixteen).
- [SparseCore Hardware Architecture](architecture.md) — the four-tier memory model and the SCS/TAC/TEC engine scopes the cast tags name.
- [SparseCore Overview](overview.md) — the navigational entry for Part IX; engine names, per-gen presence, the data path.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the indirect-DMA consumers that read through the re-tagged SparseCore pointers.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore pointers & DMA — [back to index](../index.md)
