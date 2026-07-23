# Codegen collectives / send-recv + the KLR↔BIR op-join table

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 twins present). Subject binaries: `neuronxcc/starfish/lib/libwalrus.so` (the **Strand-I** backend object — every `KlirToBirCodegen::codegen*` leaf body) and `neuronxcc/starfish/lib/libBIR.so` (the `InstBuilder::addLocalSendRecv` definition; the `libwalrus` copy is an extern `U`-row resolved through the PLT). Both retain their full dynamic symbol tables, so every leaf below is a byte-exact `nm -DC` row. `libwalrus` `.text` (`0xf00000+`) and `.rodata` (`0x1c72000+`) have VA == file-offset, so jump tables read by file offset. Evidence is `nm`, IDA decompiled/disasm sidecars under `ida/…__libwalrus.so/` and `…__libBIR.so/`, and `strings`. The **two-VA-frame artifact** applies: each leaf appears twice in the IDA sidecars — once at its PLT-thunk frame (`_0x6000xx`) and once at its real body frame (`_0xf2xxxx`); addresses below are always the real `nm` body. Every claim carries a confidence tag: CERTAIN / HIGH / MEDIUM.*

## Abstract

This page does two things. First, it documents the **collective / send-recv codegen leaves** — the corner of the `KlirToBirCodegen` lowering that turns KLR collective communication operators into `bir::InstCollectiveCompute` (IT `48`) nodes. There are **four** such leaves, and contrary to the naïve "all collectives share one encoder" reading, they split **three ways**: an explicit-permute leaf that builds the instruction itself, a thin leaf that delegates to the shared group-collective encoder, a dedicated fused permute-reduce leaf, and `SendRecv`, which is *not* a `CollectiveOp` at all and reaches a different `InstBuilder` entry point. Second, it assembles the **capstone of the codegen series**: the complete **klr→BIR op-join table** — every dispatched `codegen<Op>` leaf, its address, and the `bir::Inst*` / `InstructionType` opcode it emits — annotated with the **five 1→many fan-outs**, the **fan-ins** (several klr ops collapsing onto one opcode), and the **BIR-only opcodes** that no klr leaf ever produces. The join is the **architecture-independent waist** of the four-layer lowering: `klr::<Op>` → `bir::Inst*` (here) → per-arch `CoreV*` ISA descriptor → NEFF wire bytes.

This page is the leaf companion to the dispatch core ([7.21 KlirToBirCodegen dispatch](./klir-codegen-dispatch.md), the 77-case switch and refcount-shim mechanics) and the beta3 collective counterpart [6.5.13 BirCodeGenLoop collective lowering](../nki/bircodegen-collective.md). The `CollectiveKind` enum itself — the field every leaf here writes — is catalogued in [7.9 Op-family enums](./op-family-enums.md), which establishes that **one opcode (IT `48`) multiplexes all 11 `CollectiveKind`s**.

| | |
|---|---|
| **Class** | `neuronxcc::backend::KlirToBirCodegen` (TU `klir_to_bir_codegen.cpp`) |
| **Binary** | `libwalrus.so` (leaves) + `libBIR.so` (`addLocalSendRecv` @ `0x2b2780`) |
| **Collective leaves** | 4 KLR leaves + 4 group leaves over 1 shared encoder `codegenCollectiveOp` @ `0xf229a0` |
| **Target opcode** | `bir::InstCollectiveCompute` = `InstructionType` **48** (IT48) for all collectives + SendRecv |
| **Kind field** | `InstCollectiveCompute+0xF8` (= +248) = `bir::CollectiveKind` (0..10); IT48 `sameInst` keys on this dword |
| **Join coverage** | ~57 dispatched `codegen<Op>` leaves → ~50 distinct `bir::Inst*` opcodes |
| **Fan-outs** | 5 (NcCopy, NcDmaCopy, Transpose, MatchReplace8, CmpBranch) |
| **BIR-only opcodes** | ~60 of the 110 IT values never originate from a klr leaf |

---

## 1. The collective / send-recv codegen leaves

### 1.1 The three-way split (and why the "all-shared" reading is wrong)

A KLR kernel names collectives through six `klr::CollectiveOp`-typed operator arms plus a separate `klr::SendRecv` class. The dispatch switch (the `codegenOperator` jump table, [7.21](./klir-codegen-dispatch.md)) routes each to a `codegen<Op>` leaf. The temptation is to assume all collective arms are thin shims onto one shared encoder. They are **not**. Reading the four leaf bodies shows three distinct lowering styles:

| KLR arm | Operator-kind | Leaf @addr | Style | Builds IT48 how |
|---|---|---|---|---|
| `CollectivePermute` | k55 | `codegenCollectivePermute` @ `0xf22110` | **DEDICATED** (393 L) | inline; writes `src_target_pairs` |
| `CollectivePermuteImplicit` | k56 | `codegenCollectivePermuteImplicit` @ `0xf23a40` | **THIN** → shared | delegates to `codegenCollectiveOp(op,9)`, then sets `channel_id` |
| `CollectivePermuteImplicitReduce` | k57 | `codegenCollectivePermuteImplicitReduce` @ `0xf21850` | **DEDICATED** (392 L) | inline; 2-in/1-out + reduce-op + `channel_id` |
| `SendRecv` | k42 | `codegenSendRecv` @ `0xf1d420` | **separate path** | calls `bir::InstBuilder::addLocalSendRecv` (libBIR) |

The four *group* collectives — `AllReduce` (k52), `AllGather` (k53), `ReduceScatter` (k54), `AllToAll` (k59) — are the canonical thin pattern: each bumps the `shared_ptr` refcount, calls the shared `codegenCollectiveOp` @ `0xf229a0`, then patches one op-specific field. So of the seven `CollectiveOp` arms, **five are thin** (four group + `PermuteImplicit`) and **two are dedicated** (`Permute`, `PermuteImplicitReduce`). *Anchors: all eight addresses are `nm -DC` rows — `codegenSendRecv@0xf1d420`, `codegenCollectivePermute@0xf22110`, `codegenCollectiveOp@0xf229a0`, `codegenCollectivePermuteImplicit@0xf23a40`, `codegenCollectivePermuteImplicitReduce@0xf21850`, `codegenAllReduce@0xf23510`, `codegenAllGather@0xf23650`, `codegenReduceScatter@0xf23780`, `codegenAllToAll@0xf23910` all verified.*

> **QUIRK — the shared encoder *can* encode kinds 7/9/10, but only 9 reaches it.** The shared `codegenCollectiveOp` @ `0xf229a0` carries a name `switch(kind)` whose arms include `case 7 → "CollectivePermute"`, `case 9 → "CollectivePermuteImplicit"`, `case 10 → "CollectivePermuteImplicitReduce"`. So the encoder is *capable* of producing all three permute variants. But the KLR dispatch only ever reaches it for kind 9 (`PermuteImplicit`); kinds 7 and 10 have their own dedicated bodies that build IT48 directly and merely *reuse the kind-string* via `insertElement`, not the shared encoder. The capable-but-unreached arms are dead-on-this-path — a sign these leaves were forked off a common ancestor and the explicit/reduce variants grew their own operand-binding logic. *Anchors: the name strings `CollectivePermute`, `CollectivePermuteImplicit`, `CollectivePermuteImplicitReduce` present in `.rodata`; the two dedicated bodies do not `call codegenCollectiveOp`.*

> **CRITICAL DISTINCTION — Operator-kind ≠ `CollectiveKind`.** The Operator-kind (k52..k59) is a `libwalrus` jump-table index into the `codegenOperator` switch. `bir::CollectiveKind` (0..10) is the wire-level algorithm selector written into `InstCollectiveCompute+0xF8`. They are unrelated namespaces. Dispatch-kind 55 (`CollectivePermute`) binds `CollectiveKind` **7** (Permute); dispatch-kind 42 (`SendRecv`) binds `CollectiveKind` **0** (SendRecv). The leaf is what binds the `CollectiveKind`, and that binding is the single decisive choice — it selects the downstream on-chip-vs-remote fate (§4).

### 1.2 The `klr::CollectiveOp` / `klr::SendRecv` field layouts

The shared `klr::CollectiveOp` carries everything the seven arms need; each arm reads a different subset. Offsets are read from the per-field flag bytes and size reads that `klr::to_string(CollectiveOp&)` @ `0xf95120` walks:

```c
// klr::CollectiveOp  (to_string prints: dsts, srcs, op, replicaGroup,
//                     concatDim, sourceTargetPairs, channel_id, num_channels)
struct CollectiveOp {
    list<shared_ptr<TensorRef>> dsts;       // +0   sentinel = obj+0
    u64                         dsts_size;  // +16
    list<shared_ptr<TensorRef>> srcs;       // +24  sentinel = obj+24
    u64                         srcs_size;  // +40
    klr::AluOp                  op;         // +48  (reduce-op value)    flag @+52
    shared_ptr<ReplicaGroup>    replicaGroup;// +56  tag = **(+56); 3 = literal
    int                         concatDim;  // +72  flag @+76  (optional)
    optional<list<list<int>>>   srcTgtPairs;// +80  flag @+104, size @+96
    int                         channel_id; // +112 flag @+116 (optional)
    int                         num_channels;//+120 flag @+124 (optional)
};
```
*Anchors: `to_string` key order plus the per-field flag-byte/size reads.*

```c
// klr::SendRecv  (to_string @0xf8e390 prints: dst, src, sendToRank,
//                 recvFromRank, pipeId, useGpsimdDma)
struct SendRecv {
    shared_ptr<TensorRef>  dst;          // +0
    shared_ptr<TensorRef>  src;          // +16
    shared_ptr<Immediate>  sendToRank;   // +32
    shared_ptr<Immediate>  recvFromRank; // +48
    shared_ptr<Immediate>  pipeId;       // +64
    bool                   useGpsimdDma; // (printed via to_string(int)) — IGNORED at codegen (§1.6)
};
```
*Anchors: the `shared_ptr` fields are read at `a2[0],a2[2],a2[4],a2[6],a2[8]` = byte offsets 0/16/32/48/64 inside `codegenSendRecv`.*

### 1.3 `bir::InstCollectiveCompute` (IT48) — the fields the leaves write

All four lowerings populate one IT48 node. These are the offsets the codegen bodies touch, mapped to the wire schema:

| Offset | Field | Writer / value | Notes |
|---|---|---|---|
| `+0xF8` (248) | `cc_channel.kind` `CollectiveKind` | leaf stores the bound kind | ctor default `2`=AllReduce; IT48 `sameInst` @ `0x2f4a80` keys on this dword |
| `+0x100` (256) | `cc_channel.replica_groups` `vector<vector<u32>>` | `sub_F1F790` | group routing |
| `+0x118` (280) | `channel_id` (MaybeAffine, tag @+312) | reset-then-store | written by k9/k10 [INFERRED] key-name |
| `+0x140` (320) | `stream_id` MaybeAffine | reset to `-1` (unset) | every leaf clears this |
| `+0x168` (360) | `src_target_pairs` `vector<vector<u32>>` | `sub_F1F790` | explicit-permute routing |
| `+0x180` (384) | `cc_channel.op` `AluOpType` reduce-op | `codegenAluOp` | reducing collectives only |
| `+0x1B0` (432) | `is_local` MaybeAffine\<bool\> (tag @+464) | `addLocalSendRecv` sets `1` | "LocalSendRecv" marker |
| `+0x1D8` (472) | `pipe_id` scalar | `addLocalSendRecv` | SendRecv only |
| `+0x1E0/+0x1E8` | recv/send rank list bases | `addLocalSendRecv` | point-to-point |
| `+0x1F8` (504) | peer/source QuasiAffineExpr | `addLocalSendRecv` | value `1 − recvFromRank` [INFERRED] name |
| `+0x27C` (636) | `cc_dim` `CollectiveDimension` | `codegenAllGather` etc. | group dim, NOT permutes |

The `reset-then-store` idiom at `+280/+320/+432` is the standard `std::variant<value,QuasiAffineExpr>` (MaybeAffine) assignment: `_M_reset` only when the tag byte is neither `0` nor `-1`, then store scalar and clear tag. HIGH — the idiom is recognized across all four bodies; offsets cross [7.9](./op-family-enums.md) and the IT48 schema. `sameInst@0x2f4a80` keying on `+248` is exact.

### 1.4 The two dedicated permute leaves

```c
// (A) codegenCollectivePermute — CollectiveKind 7 — DEDICATED  @0xf22110
bir::Instruction* codegenCollectivePermute(KlirToBirCodegen *this, klr::CollectiveOp *cp) {
    assert(cp->srcs_size == 1);                 // "guaranteed by frontend API"  (line 0x77A)
    assert(cp->dsts_size == 1);                 //                               (0x77B)
    assert(has_value(cp->srcTgtPairs));         // flag @cp+104                  (0x77C)
    BasicBlock *bb = this->curBB;               // this+72
    auto name = "CollectivePermute-" + returnAndIncrementCCId();
    auto cc   = bb->insertElement<InstCollectiveCompute>(name);   // IT48
    register_inst(this, cc);                                      // sub_F20EC0
    cc->addArgument(codegenTensorRef(cp->srcs[0]));   // 1 input  (*(*(cp+24)+16))
    cc->addOutput  (codegenTensorRef(cp->dsts[0]));   // 1 output (*(*cp+16))
    cc[+248] = 7;                               // CollectiveKind = Permute            (*)
    // for each pair p in cp->srcTgtPairs: assert p.size()==2 (0x78E); emit {src,tgt}
    sub_F1F790(cc + 360, builtPairs);           // → src_target_pairs (+0x168)         (*)
    cc[+320] = -1;                              // stream_id ← unset
    return cc;
    // KEY: replica_groups (+256) left EMPTY; routing is BY src_target_pairs only.
    //      NO reduce-op (+384). NO channel_id. The simulator asserts exactly this:
    //      "Permute should not contain replica_groups".
}
```

```c
// (C) codegenCollectivePermuteImplicitReduce — CollectiveKind 10 — DEDICATED  @0xf21850
bir::Instruction* codegenCollectivePermuteImplicitReduce(KlirToBirCodegen *this, klr::CollectiveOp *cpir) {
    assert(cpir->srcs_size == 2);               // permuted-in value + local accumulator (0x7AD)
    assert(cpir->dsts_size == 1);               //                                       (0x7AE)
    assert(has_value(cpir->op));                // flag @cpir+52                          (0x7AF)
    assert((**(cpir+56)) == /*literal*/3);      // replicaGroup->tag == literal          (0x7B0)
    assert(has_value(cpir->channel_id));        // flag @cpir+116                        (0x7B1)
    auto name = "CollectivePermuteImplicitReduce-" + returnAndIncrementCCId();
    auto cc   = this->curBB->insertElement<InstCollectiveCompute>(name);
    register_inst(this, cc);
    cc->addArgument(codegenTensorRef(cpir->srcs[0]));  // peer-routed value
    cc->addArgument(codegenTensorRef(cpir->srcs[1]));  // local accumulator  → 2 in / 1 out (*)
    cc->addOutput  (codegenTensorRef(cpir->dsts[0]));
    cc[+248] = 10;                              // CollectiveKind = PermuteReduceImplicit (*)
    cc[+384] = codegenAluOp(cpir->op);          // reduce-op (AluOpType) @+0x180          (*)
    sub_F1F790(cc + 256, replicaGroups);        // → replica_groups (+0x100)              (*)
    cc[+280] = cpir->channel_id;                // channel_id (MaybeAffine assign)        (*)
    cc[+320] = -1;                              // stream_id unset
    return cc;
    // KEY: the FUSED permute-then-reduce. 2 inputs combined by op. Routing by
    //      replica_groups (implicit). Sim @0x1ebd10: "must have 2 inputs and 1 output";
    //      "Unsupported OP type for Permute Reduce Implicit".
}
```
*Anchors: both bodies build IT48 inline; the assert line-ids are the `cpp:0x7xx` references. `codegenAluOp@0xf140c0`, `codegenTensorRef@0xf18b30`, `returnAndIncrementCCId@0xf13d50` are `nm` rows.*

### 1.5 The thin permute leaf and the shared group encoder

```c
// (B) codegenCollectivePermuteImplicit — CollectiveKind 9 — THIN → shared  @0xf23a40
bir::Instruction* codegenCollectivePermuteImplicit(KlirToBirCodegen *this, klr::CollectiveOp *cpi) {
    assert(cpi->srcs_size == 1);                              // (0x79F)
    assert(cpi->dsts_size == 1);                              // (0x7A0)
    assert(has_value(cpi->channel_id));                      // (0x7A1)
    auto cc = codegenCollectiveOp(this, cpi, /*CollectiveKind=*/9);   // SHARED ENCODER (*)
    cc[+280] = cpi->channel_id;                              // MaybeAffine assign       (*)
    return cc;
    // KEY: routing BY replica_groups (the implicit permutation = a rotate derived from
    //      replica order), NOT explicit pairs. channel_id REQUIRED. Sim:
    //      "PermuteImplicit should not contain src_target_pairs".
}

// shared codegenCollectiveOp  @0xf229a0  (444 L)  — the group/k9 encoder
bir::Instruction* codegenCollectiveOp(KlirToBirCodegen *this, klr::CollectiveOp *op, CollectiveKind k) {
    const char *kindStr;
    switch (k) {                                  // capable of 2,3,4,5,7,9,10
        case 2: kindStr = "AllReduce";        break;
        case 3: kindStr = "ReduceScatter";    break;
        case 4: kindStr = "AllGather";        break;
        case 5: kindStr = "AllToAll";         break;
        case 7: kindStr = "CollectivePermute";             break;  // reachable only from dedicated body's string reuse
        case 9: kindStr = "CollectivePermuteImplicit";     break;
        case 10:kindStr = "CollectivePermuteImplicitReduce";break;
        default: assert(false);
    }
    assert(!op->srcs.empty());                    // (0x719)
    assert(!op->dsts.empty());                    // (0x71A)
    assert(op->srcs_size == op->dsts_size);       // (0x71B)
    assert((**(op+56)) == /*literal*/3);          // replicaGroup->tag == literal  (0x71C)
    auto name = string(kindStr) + "-" + returnAndIncrementCCId();
    auto cc   = this->curBB->insertElement<InstCollectiveCompute>(name);
    for (auto [src,dst] : zip(op->srcs, op->dsts)) {     // N-in / N-out coalesced
        cc->append_arg(physAP_for(src));     // arg list @cc+160
        cc->append_out(physAP_for(dst));     // out list @cc+192
    }
    cc[+248] = k;                                 // CollectiveKind
    sub_F1F790(cc + 256, op->replicaGroup);       // → replica_groups (+0x100)
    cc[+320] = -1;                                // stream_id unset
    return cc;
}
```

The four group leaves each call this encoder then patch one field:

```c
codegenAllReduce     @0xf23510 (32 L): assert has_value(op); cc=codegenCollectiveOp(op,2);
                                       cc[+384]=codegenAluOp(op->op);              // reduce-op
codegenAllGather     @0xf23650 (40 L): assert has_value(concatDim); cc=codegenCollectiveOp(op,4);
                                       if(concatDim>1) sub_F12900(cc); cc[+636]=concatDim;  // cc_dim
codegenReduceScatter @0xf23780 (44 L): cc=codegenCollectiveOp(op,3) + reduce-op + cc_dim
codegenAllToAll      @0xf23910 (40 L): cc=codegenCollectiveOp(op,5)
```


**The routing-data crux** — this is the field-set the simulator keys on, and it is the entire reason the permute leaves diverge:

| `CollectiveKind` | Leaf | Routing data | replica_groups (+256) | src_target_pairs (+360) |
|---|---|---|---|---|
| group (2/3/4/5) | thin | `replica_groups` | written | empty |
| 7 Permute | dedicated | `src_target_pairs` | **empty** | **written** |
| 9 PermuteImplicit | thin | `replica_groups` | written | empty |
| 10 PermuteReduceImplicit | dedicated | `replica_groups` + reduce-op | written | empty |
| 0 SendRecv | addLocalSendRecv | send/recv rank + peer affine (+488/+480/+504) | — | — |

### 1.6 `codegenSendRecv` — the non-collective point-to-point path

`klr::SendRecv` is a separate class, **not** a `CollectiveOp`. Its leaf does not touch `codegenCollectiveOp`; it validates the ranks, guards against self-send/self-recv, and calls `bir::InstBuilder::addLocalSendRecv` in `libBIR`:

```c
// codegenSendRecv  @0xf1d420  (disasm read)
bir::Instruction* codegenSendRecv(KlirToBirCodegen *this, klr::SendRecv *sr) {
    auto out = codegenTensorRef(sr->dst);                 // **a2   (offset 0)
    auto in  = codegenTensorRef(sr->src);                 // (*a2)[2] (offset 16)
    auto sendTo   = codegenImmediate(sr->sendToRank);     // (*a2)[4]
    assert(holds_alternative<int>(sendTo)   && "The sendToRank must be a integer");    // (0x6D1)
    auto recvFrom = codegenImmediate(sr->recvFromRank);   // (*a2)[6]
    assert(holds_alternative<int>(recvFrom) && "The recvFromRank must be a integer");  // (0x6D5)
    auto pipeId   = codegenImmediate(sr->pipeId);         // (*a2)[8]
    assert(holds_alternative<int>(pipeId)   && "The pipeId must be a integer");        // (0x6D9)

    // self-loop guard against THIS core's id:
    optional<int> myCore = bir::Module::getNeuronCoreId(this->module);   // @0xf1d60e call
    assert(myCore.has_value());
    if ((float)*myCore == (float)recvFrom)
        throw runtime_error("SendRecv instruction on core <N> is receiving from itself!");
    if ((float)*myCore == (float)sendTo)
        throw runtime_error("SendRecv instruction on core <N> is sending to itself!");

    auto name = "LocalSendRecv-" + returnAndIncrementCCId();
    auto cc = bir::InstBuilder::addLocalSendRecv(            // @0xf1d768 call
                  name, /*src*/in, /*dst*/out, inAP, outAP,
                  /*sendToRank&*/sendTo, /*recvFromRank&*/recvFrom,
                  /*pipeId*/pipeId, /*pipeId&*/…,
                  /*useGpsimdDma=*/false,                     // @0xf1d750  push 0   (*)
                  srcLoc);
    cc[+320] = -1;                                           // stream_id unset
    return cc;
}
```

> **GOTCHA — `useGpsimdDma` is hardcoded `false` at codegen.** The disasm shows `push 0` at `0xf1d750` immediately before the `call addLocalSendRecv` at `0xf1d768`; that `0` is the 11th argument, the `bool useGpsimdDma`. The `klr::SendRecv.useGpsimdDma` field (present in `to_string`) is **never read here**. The GPSIMD-vs-DMA engine decision is *deferred* to the on-chip lowering pass (`lowerSendRecv` `isCompatible` + bytes-per-partition gate, §4), not made at codegen. *Anchors: `0xf1d60e call getNeuronCoreId`, `0xf1d750 push 0`, `0xf1d768 call addLocalSendRecv` are byte-witnessed in the body-frame sidecar; the klr-field-ignore is HIGH.*

`addLocalSendRecv` itself (libBIR, body @ `0x2b2780`, decompiled cleanly there) is what actually mints the IT48 and stamps it as a local send-recv:

```c
// bir::InstBuilder::addLocalSendRecv  @libBIR 0x2b2780  (decompiled in the libBIR sidecar)
InstCollectiveCompute* addLocalSendRecv(name, srcML, dstML, inAP, outAP,
                                        sendToRank&, recvFromRank&, pipeId, pipeId&,
                                        useGpsimdDma, srcLoc) {
    auto v = addInstruction<InstCollectiveCompute>(bb, ip, name, inAP, outAP, …);   // IT48
    v[+248] = 0;                          // CollectiveKind = SendRecv(0)          (**)
    v[+432] = 1; /*tag +464 cleared*/     // is_local = TRUE  →  "LocalSendRecv"   (*)
    v[+472] = pipeId;                     // pipe_id
    v[+536] = 1; /*tag +568*/             // initial-corebarrier-class flag = true
    v[+504] = QuasiAffineExpr(module.name, 1 - recvFromRank);  // peer/source affine
    v[+576] = useGpsimdDma;               // = false here  → use_gpsimd_dma
    return v;                             // src/recv/send ranks woven into +480/+488
}
```

So an explicit `klr::SendRecv` **always** lowers to a `CollectiveKind`-0 (SendRecv), `is_local=true` IT48. There is **no** `codegenSendRecvCCE`: `SendRecvCCE` (`CollectiveKind` 1) originates upstream in the HLO collectives-to-CC path, never from a KLR leaf. `klr::SendRecvCompute`/`klr::Send`/`klr::Recv` have ser/des/to_string but **no codegen leaf** in the dispatch. No `codegenSendRecvCompute` or `codegenSendRecvCCE` symbol exists in `nm -DC`.

---

## 2. The master KLR↔BIR op-join table

This is the architecture-independent waist: every dispatched `codegen<Op>` leaf → its `bir::Inst*` / `InstructionType`. The Operator-kind column is the `codegenOperator` switch case (`jpt @0x1de95f8`, [7.21](./klir-codegen-dispatch.md)), **not** the KLR-ser wire type-id — the two are independent namespaces (e.g. `NcMatMul`: Operator-kind 22, ser type-id 183). The IT column uses [7.10 instruction-type](./instruction-type.md) ordinals. Brace `{…}` marks a 1→many fan-out (§3).

### COMPUTE — matmul / activation / scalar / tensor

| klr::\<Op\> | k | leaf @addr | → bir::Inst\* (IT) | conf |
|---|---|---|---|---|
| `NcMatMul` | 22 | `codegenNcMatMul` @`0xf19590` | `InstMatmult` IT8 | CERTAIN |
| `MatMulMX` | 50 | `codegenNcMatMulMX` @`0xf25320` | `InstMatmultMx` IT95 | CERTAIN |
| `QuantizeMX` | 49 | `codegenQuantizeMX` @`0xf25ad0` | `InstQuantizeMx` IT96 | CERTAIN |
| `NcActivate` | 2 | `codegenNcActivate` @`0xf18ba0` | `InstActivation` IT4 | CERTAIN |
| `Activate` | 3 | `codegenNcActivate` @`0xf18ba0` | `InstActivation` IT4 | CERTAIN †shared leaf (reduce-fold arm) |
| `Activate2` | 76 | `codegenActivate2` @`0xf2f3a0` | `InstActivation` IT4 | CERTAIN ‡`is_activate2=1` |
| `Exponential` | 75 | `codegenExponential` @`0xf23fb0` | `InstExponential` IT103 | CERTAIN |
| `Reciprocal` | 29 | `codegenReciprocal` @`0xf1a5a0` | `InstReciprocal` IT21 | CERTAIN |
| `TensorScalar` | 34 | `codegenTensorScalar` @`0xf2bf80` | `InstTensorScalarPtr` IT29 | CERTAIN |
| `TensorScalarReduce` | 38 | `codegenTensorScalarReduce` @`0xf2b9f0` | `InstTensorScalarPtr` IT29 | CERTAIN |
| `TensorScalarCumulative` | 71 | `codegenTensorScalarCumulative` @`0xf2aff0` | `InstTensorScalarCache` IT30 | CERTAIN |
| `NcScalarTensorTensor` | 31 | `codegenNcScalarTensorTensor` @`0xf2c450` | `InstTensorScalarPtr` IT29 | CERTAIN |
| `TensorTensor` | 35 | `codegenTensorTensor` @`0xf1b820` | `InstTensorTensor` IT31 | CERTAIN |
| `TensorTensorScan` | 36 | `codegenTensorTensorScan` @`0xf2b450` | `InstTensorScalarPtr` IT29 | CERTAIN ⚠scan reuses TSP |
| `TensorReduce` | 33 | `codegenTensorReduce` @`0xf1a140` | `InstTensorReduce` IT27 | CERTAIN |
| `TensorPartitionReduce` | 37 | `codegenTensorPartitionReduce` @`0xf1b3d0` | `InstTensorReduce` IT27 | CERTAIN †axis=C(5) |
| `BatchNormStats` | 7 | `codegenBatchNormStats` @`0xf1add0` | `InstBNStats` IT34 | CERTAIN |
| `BatchNormAggregate` | 6 | `codegenBatchNormAggregate` @`0xf1b0d0` | `InstBNStatsAggregate` IT35 | CERTAIN |

### SELECT / DVE / IOTA / RNG

| klr::\<Op\> | k | leaf @addr | → bir::Inst\* (IT) | conf |
|---|---|---|---|---|
| `NcAffineSelect` | 5 | `codegenNcAffineSelect` @`0xf29690` | `InstTensorScalarAffineSelect` IT62 | CERTAIN |
| `NcRangeSelect` | 28 | `codegenNcRangeSelect` @`0xf1c210` | `InstRangeSelect` IT63 | CERTAIN |
| `SelectReduce` | 40 | `codegenSelectReduce` @`0xf270a0` | `InstCopyPredicated` IT52 | CERTAIN ⚠NOT RangeSelect |
| `CopyPredicated` | 10 | `codegenCopyPredicated` @`0xf1a9c0` | `InstCopyPredicated` IT52 | CERTAIN |
| `Max8` | 25 | `codegenMax8` @`0xf1c970` | `InstMax` IT88 | CERTAIN |
| `FindIndex8` | 15 | `codegenFindIndex8` @`0xf1cc80` | `InstMaxIndex` IT89 | CERTAIN |
| `MatchReplace8` | 23 | `codegenMatchReplace8` @`0xf29e10` | `{InstMatchReplace IT90 \| InstMaxIndexAndMatchReplace IT91}` | CERTAIN ⭐1→2 |
| `NonzeroWithCount` | 73 | `codegenNonzeroWithCount` @`0xf2c8f0` | `InstNonzeroWithCount` IT104 | CERTAIN |
| `Iota` | 16 | `codegenIota` @`0xf1d0c0` | `InstIota` IT61 | CERTAIN |
| `MemSet` | 26 | `codegenMemSet` @`0xf19d60` | `InstMemset` IT10 | CERTAIN |
| `Dropout` | 14 | `codegenDropout` @`0xf27d30` | `InstDropout` IT65 | CERTAIN |
| `Rng` | 65 | `codegenRng` @`0xf245c0` | `InstMemset(mode=Random)` IT10 | CERTAIN ⚠NOT IT100 |
| `Rand2` | 66 | `codegenRand2` @`0xf24ff0` | `InstRand2` IT97 | CERTAIN |
| `SetRngSeed` | 68 | `codegenSetRngSeed` @`0xf24860` | `InstSetRandState` IT59 | CERTAIN |
| `RandGetState` | 67 | `codegenRandGetState` @`0xf24d60` | `InstRandGetState` IT99 | CERTAIN |
| `RandSetState` | 69 | `codegenRandSetState` @`0xf24ad0` | `InstRandSetState` IT98 | CERTAIN |
| `NcLocalGather` | 20 | `codegenNcLocalGather` @`0xf2a9c0` | `InstIndirectCopy` IT26 | CERTAIN |
| `NcNGather` | 72 | `codegenNcNGather` @`0xf2a590` | `InstGather` IT92 | CERTAIN |

### DATA-MOVE / collective / send-recv / rank / control

| klr::\<Op\> | k | leaf @addr | → bir::Inst\* (IT) | conf |
|---|---|---|---|---|
| `NcCopy` | 9 | `codegenNcCopy` @`0xf2d120` | `{InstTensorCopy IT23 \| …DynamicSrc IT24 \| …DynamicDst IT25}` | CERTAIN ⭐1→3 |
| `NcDmaCopy` | 12 | `codegenNcDmaCopy` @`0xf2fa40` | `{InstDMACopy IT32 \| InstGenericIndirectSave IT44}` | CERTAIN ⭐1→2 |
| `DmaCompute` | 51 | `codegenDmaCompute` @`0xf1d9e0` | `InstDMACopy(addDmaCopyFma)` IT32 | CERTAIN |
| `DmaTranspose` | 13 | `codegenDmaTranspose` @`0xf2dd10` | `InstDMACopy(Transpose)` IT32 | CERTAIN |
| `Transpose` | 39 | `codegenTranspose` @`0xf28fb0` | `{InstStreamTranspose IT40 \| InstMatmult IT8 vs identity}` | CERTAIN ⭐1→2 |
| `Shuffle` | 32 | `codegenShuffle` @`0xf28130` | `InstStreamShuffle` IT39 | CERTAIN |
| `TensorLoad` | 44 | `codegenTensorLoad` @`0xf26a60` | `InstTensorLoad` IT75 | CERTAIN |
| `TensorStore` | 45 | `codegenTensorStore` @`0xf266c0` | `InstTensorSave` IT76 | CERTAIN |
| `CollectiveOp[AllReduce]` | 52 | `codegenAllReduce` @`0xf23510` | `InstCollectiveCompute` IT48 | CERTAIN |
| `CollectiveOp[AllGather]` | 53 | `codegenAllGather` @`0xf23650` | `InstCollectiveCompute` IT48 | CERTAIN |
| `CollectiveOp[ReduceScatter]` | 54 | `codegenReduceScatter` @`0xf23780` | `InstCollectiveCompute` IT48 | CERTAIN |
| `CollectiveOp[Permute]` | 55 | `codegenCollectivePermute` @`0xf22110` | `InstCollectiveCompute` IT48 | CERTAIN |
| `CollectiveOp[PermuteImplicit]` | 56 | `codegenCollectivePermuteImplicit` @`0xf23a40` | `InstCollectiveCompute` IT48 | CERTAIN |
| `CollectiveOp[PermuteImplicitReduce]` | 57 | `codegenCollectivePermuteImplicitReduce` @`0xf21850` | `InstCollectiveCompute` IT48 | CERTAIN |
| `CollectiveOp[AllToAll]` | 59 | `codegenAllToAll` @`0xf23910` | `InstCollectiveCompute` IT48 | CERTAIN |
| `SendRecv` | 42 | `codegenSendRecv` @`0xf1d420` | `InstCollectiveCompute` IT48 (`addLocalSendRecv`, kind=0, is_local) | CERTAIN |
| `RankId` | 60 | `codegenRankId` @`0xf26e00` | `InstGetGlobalRankId` IT11 | CERTAIN |
| `CurrentProcessingRankId` | 61 | `codegenCurrentProcessingRankId` @`0xf211b0` | `InstGetCurProcessingRankID` IT66 | CERTAIN |
| `CoreBarrier` | 64 | `codegenCoreBarrier` @`0xf23c10` | `InstCoreBarrier` IT87 | CERTAIN |
| `CmpBranch` | 47 | `codegenCmpBranch` @`0xf33a70` | `{InstUnconditionalBranch IT79 \| InstCompareAndBranch IT78}` | CERTAIN ⭐1→2 |
| `RegisterAluOp` | 48 | `codegenRegisterAluOp` @`0xf25f10` | `InstRegisterAlu` IT73 | CERTAIN |
| `RegisterMove` | 46 | `codegenRegisterMove` @`0xf26370` | `InstRegisterMove` IT74 | CERTAIN |
| `SequenceBounds` | 41 | `codegenSequenceBounds` @`0xf276c0` | `InstGetSequenceBounds` IT64 | CERTAIN |
| `DevicePrint` | 74 | `codegenDevicePrint` @`0xf2cde0` | `InstDevicePrint` IT57 | CERTAIN |
| `ExtendedInst` | — | (no `codegenOperator` case) | `InstCustomOp` IT53 (via libwalrus NX-126 bridge) | HIGH §5 |

Count: ~57 dispatched leaves collapse onto **~50 distinct opcodes** — the fan-ins of §3 (seven collectives → IT48, three activations → IT4, four scalar ops → IT29, two reduces → IT27) account for the gap. [Leaf addresses spot-verified byte-exact against `nm -DC libwalrus.so`; the `bir::Inst*` targets carried from the per-op Strand-I bodies.]

---

## 3. The five fan-outs (and the fan-ins)

A fan-out is a single klr leaf that emits **one of several distinct opcodes** based on a routing predicate read off the klr node. There are exactly **five**; each routing condition is transcribed from the codegen body.

**(F1) `NcCopy` → {IT23 `InstTensorCopy` | IT24 `…DynamicSrc` | IT25 `…DynamicDst`}** — `codegenNcCopy` @ `0xf2d120` reads `isDynamicAccess(src)`/`isDynamicAccess(dst)`:

```c
if (!dynSrc && !dynDst) emit InstTensorCopy;           // both STATIC (addTensorCopyCustom)
else if (dynSrc)        emit InstTensorCopyDynamicSrc;  // IT24
else /* dynDst */       emit InstTensorCopyDynamicDst;  // IT25
// guard: "Only one of src and dst can have dynamic access" (cpp:836)
```
`NcCopy` **never** emits `GenericCopy` (IT1) — that is an internal `AbstractCopy` node.

**(F2) `NcDmaCopy` → {IT32 `InstDMACopy` | IT44 `InstGenericIndirectSave`}** — `codegenNcDmaCopy` @ `0xf2fa40` falls through to `codegenGenericIndirectSave` @ `0xf2eb10` (which, confirming the fan-out, *takes a `klr::NcDmaCopy`*):

```c
if (plainCopy || uniqueIndices) emit InstDMACopy;                 // IT32  (static=addUnary; dynamic=insertElement+assembleDynamicInfo)
else /* scatter w/ index vector */ codegenGenericIndirectSave();  // IT44  ("IndirectSaveAccumulate-" prefix; compute_op add → RMW field +320=4)
// IT44 then expands further at the H06 LowerGenericIndirect pass into a 5-op sequence.
```
*Anchors: `nm` shows `codegenGenericIndirectSave(std::shared_ptr<klr::NcDmaCopy>)` @ `0xf2eb10`, i.e. the same klr type as `codegenNcDmaCopy`.*

**(F3) `Transpose` → {IT40 `InstStreamTranspose` | IT8 `InstMatmult` (vs identity)}** — `codegenTranspose` @ `0xf28fb0` throws if `engine==5` (PE), then forks on dst dtype:

```c
if (dstDtype == fp32) emit InstStreamTranspose;        // IT40  DVE 32×32 tile
else { getIdentityMatrix(128×128 SBUF);                // matmul-against-identity
       addMatmult("matmult_TP_", isTranspose@+440=1); } // IT8
// (a THIRD transpose form is the DMA-descriptor one: klr::DmaTranspose → IT32.)
```

**(F4) `MatchReplace8` → {IT90 `InstMatchReplace` | IT91 `InstMaxIndexAndMatchReplace`}** — `codegenMatchReplace8` @ `0xf29e10` branches on the index-output `Option<TensorRef>` engaged byte at `klr+0x50`:

```c
if (byte[klr+0x50] == 0) addMatchReplace();            // IT90  (index output ABSENT)
else { read 4th TensorRef; addInstruction<InstMaxIndexAndMatchReplace>(); }  // IT91 (sentinel→+0xF0)
```

**(F5) `CmpBranch` → {IT79 `InstUnconditionalBranch` | IT78 `InstCompareAndBranch`}** — `codegenCmpBranch` @ `0xf33a70` reads the `BrCmpOp` at `klr+0x44`:

```c
if (byte[klr+0x44] == 1) emit InstUnconditionalBranch; // IT79  "always" arm (inlineNoReorderBlock shortcut)
else emit InstCompareAndBranch;                        // IT78  BranchCompareOp@+0xF0 = BrCmpOp − 2
```

**Fan-ins (the inverse — many klr ops → one opcode).** These are mode-discriminated by flag fields, not opcode choice:

- **7 collective arms + SendRecv → IT48** `InstCollectiveCompute`, per-arm `CollectiveKind` + routing fields (§1). This is a 7-arm fan-in, *not* a multi-opcode fan-out: every arm produces the same opcode.
- **3 activations → IT4** `InstActivation` (`NcActivate`/`Activate` share the leaf; `Activate2` sets `is_activate2=1`).
- **4 scalar ops → IT29** `InstTensorScalarPtr` (`TensorScalar`; `TensorScalarReduce` `acc@+0x1F0=3`; `NcScalarTensorTensor` `is_scalar_tensor_tensor@+0x1A0=1`; `TensorTensorScan` `is_tensor_tensor_scan@+0x1C8=1`). `TensorScalarCumulative` → IT30 separately.
- **2 reduces → IT27** `InstTensorReduce` (`TensorReduce`; `TensorPartitionReduce` hard-codes axis=C=5).

---

## 4. Where the bound `CollectiveKind` decides on-chip vs remote

The four collective leaves all produce an abstract IT48; the *consumer* is the `LowerLocalCollectives` pass (real worker @ `0x1624850`), which groups IT48 nodes and dispatches on `CollectiveKind` at `CC+0xF8` (= our `+248`):

| `CollectiveKind` | lowering | engine fate |
|---|---|---|
| 0 SendRecv | `lowerSendRecv` @`0x161cc70` | **on-chip** SBUF↔SBUF: `InstGPSIMDSB2SB` (IT33) iff `isCompatible` ∧ bytes/partition ≤ `cl::opt sendrecv-to-gpsimd-max-bpp` (libBIR ≤1024 B/partition); else `InstDMACopy` |
| 1 SendRecvCCE | `lowerSendRecvCCE` @`0x161f130` | DMA-only (no GPSIMD probe) |
| 2 AllReduce | `lowerAllReduce` @`0x1621a80` | DMA reduce-scatter → all-gather |
| ≥3 (RS/AG/A2A/Permute 7/9/10) | — | **fence-only**, left INTACT = remote / ICI cross-node path |

So the codegen-time choice of `CollectiveKind` directly selects the on-chip-vs-remote fate. Of the four leaves on this page, **only `SendRecv` (kind 0) is on-chip-eligible**; `Permute`(7), `PermuteImplicit`(9), `PermuteReduceImplicit`(10) are all `≥3` and stay as IT48 for the remote collective path (fence-only on-chip). And because `codegenSendRecv` hardcoded `use_gpsimd_dma=false` (§1.6), the actual GPSIMD-vs-DMA engine for a local SendRecv is decided *here*, by `isCompatible` plus the bytes-per-partition gate — not at codegen.

> **GOTCHA — the local/remote cut runs the other way from the permute kinds.** It is kinds **0/1/2** that get a local lowering; the permute kinds `{7, 9, 10}` are all `≥3` and stay remote. Reading it as "the permute kinds get local GPSIMD" inverts the whole table.

*The `LowerLocalCollectives` dispatch keys on `CC+0xF8`; the kind-0/1/2-local versus ≥3-fence cut is HIGH.*

---

## 5. BIR-only opcodes — the beta2↔beta3 coverage asymmetry

Of the 110 `InstructionType` opcodes ([7.10](./instruction-type.md)), the KLR (beta2) codegen surface emits roughly **50**. The remaining ~60 are **never** produced by a `codegenOperator` leaf: they originate either (a) downstream in `libwalrus` lowering passes (Strand-H), (b) the Penguin / `BirCodeGenLoop` beta3 path, or (c) the HLO frontend (Strand-B). This is the **beta2/KLR ↔ beta3/Penguin coverage asymmetry** — the KLR surface is the leaf ISA a NKI kernel can *name*; the rest are structural/lowering opcodes minted by later passes.

BIR-only families, grouped by origin:

| Family | IT values | Origin (no klr leaf) |
|---|---|---|
| Generic / abstract copy | 0,1,2,3 | lowering / abstract (NcCopy emits `AbstractCopy` internally, never `GenericCopy`) |
| Matmul base / sparse | 7,9 | abstract (KLR emits IT8/IT95 only) |
| Activation accumulator drains | 5,101 | H07 peephole (IT102 `DveReadAccumulator` *is* reachable via `Exponential` accumulate) |
| Semaphore / barrier / engine ctrl | 6,12,13,14,15,16,17 | Strand-H lower_control / scheduler |
| Pool | 20 | HLO / Penguin (no `klr::Pool` leaf) |
| GPSIMD / stream variants | 33,41 | lowering (IT33 minted by `lowerSendRecv`, §4) |
| BN gradients / backprop | 36,37,38 | HLO training graph |
| Indirect load/save | 42,43,45,46 | H06 `LowerGenericIndirect` expands IT44 → these |
| Static Load/Save | 19,22 | DMA descriptor lowering (klr uses IT75/76) |
| Collective base / send / recv leaves | 47,49,50 | KLR emits IT48 only |
| Select base | 51 | lower_select splits to IT52 |
| Kernel carriers | 54,55,56,84 | frontend, not an op |
| Rand / Rng legacy | 58,60,100 | ⭐KLR rides IT10-Random + IT97; **never** emits IT60/IT100 |
| Queue / completion / inline-asm | 85,86,93,94 | scheduler + custom-op emission |
| DMA trigger + descriptor | 67,68,69,70,71,72 | Strand-N descriptor expansion |
| Terminators / structured control | 77,80,81,82,83,105,106,107,108,109 | CFG legalization + Penguin loop carriers |
| CustomOp | 53 | libwalrus NX-126 bridge from `klr::ExtendedInst` (indirect, not a leaf) |

The clearest "beta3 / downstream only" families are the `DMADescriptor*` (IT68–72), `DMATrigger` (IT67), `Loop`/`DMABlock` (IT105–109), and the `IndirectLoad/Save` split (IT42–46).

> **GOTCHA — klr names that are not a bir opcode.** Several roster names are *not* independently codegen'd: the **abstract parents** (`MatMul`/`Copy`/`DmaCopy`/`ScalarTensorTensor`/`AffineSelect`/`RangeSelect`/`LocalGather`) whose concrete `Nc*` child is the dispatched leaf; the **sub-components** (`LoadStationary` folded into the matmul stationary operand, `MatchValueLoad` inside the DVE Max/MatchReplace sequence, `Send`/`Recv` folded into the SendRecv path, `SendRecvCompute` originating upstream); and the **structural** ones (`ExtendedInst` → `InstCustomOp` via the NX-126 bridge; `LncKernel` is the per-LNC-core kernel-body driver `codegenLncKernel` @ `0xf34140`, not an op — it emits *many* insts). The single cleanest "name ≠ opcode" remap is **`klr::Rng → InstMemset(mode=Random)` (IT10)**, *not* `InstRng` (IT100) — so the bir Rng/Rand opcodes are BIR-only and the klr Rng op is klr-only-named.

---

## 6. The four-layer join — where this table sits

The klr→BIR map here is **layer 1→2** of the full lowering. Each `bir::Inst*` continues through two more layers to reach NEFF wire bytes:

```text
LAYER 1  klr::<Op>          → LAYER 2  bir::Inst* (IT)         [THIS PAGE, §2]
LAYER 2  bir::Inst* (IT)    → LAYER 3  CoreV{2,3,4}GenImpl::visitInst<X>   (per-ARCH ISA descriptor)
LAYER 3  ISA descriptor     → LAYER 4  InstGen.serialize → NEFF wire bytes  (enum marshalling per the wire crosswalk)
```

Layer 2→3 is where the **per-arch fan-out** happens: one IT can map to many ISA opcodes (gen2/3/4 differ). For example `visitInstTensorReduce` @ `0x12383a0` forks IT27 into 6 distinct ISA reduce opcodes by axis/transpose, and `visitInstMaxIndex` emits **two** co-issued DVE bundles from one IT89 — a 1→2 split that happens at the *encoder*, distinct from the codegen-time `MatchReplace8` split (§3 F4). So §2 is the **arch-independent waist**; the ISA fan-out and the int↔wire↔JSON enum marshalling are downstream legs. See [7.9 op-family enums](./op-family-enums.md) for the L1/L2/L3 enum crosswalk that the layer boundaries marshal across.

---

## 7. Diagnostic strings (collective / send-recv path)

| String | Emitted by | Meaning |
|---|---|---|
| ` is receiving from itself!` | `codegenSendRecv` | self-recv guard (`myCore == recvFromRank`) |
| ` is sending to itself!` | `codegenSendRecv` | self-send guard (`myCore == sendToRank`) |
| `The sendToRank must be a integer` | `codegenSendRecv` (assert) | `holds_alternative<int>(sendTo)` |
| `The recvFromRank must be a integer` | `codegenSendRecv` (assert) | `holds_alternative<int>(recvFrom)` |
| `The pipeId must be a integer` | `codegenSendRecv` (assert) | `holds_alternative<int>(pipeId)` |
| `CollectivePermute` / `CollectivePermuteImplicit` / `CollectivePermuteImplicitReduce` | `codegenCollectiveOp` switch + dedicated bodies | IT48 name prefix (+ `-<CCId>` suffix) |
| `Only one of src and dst can have dynamic access` | `codegenNcCopy` (cpp:836) | F1 fan-out guard |

All present in `libwalrus.so` `.rodata`. *Anchors: `strings -a` hits for every row above.*

---

## 8. Function map

| Symbol | @addr | Binary | Role |
|---|---|---|---|
| `codegenSendRecv` | `0xf1d420` | libwalrus | klr::SendRecv → addLocalSendRecv (kind 0, is_local) |
| `codegenCollectivePermute` | `0xf22110` | libwalrus | dedicated, src_target_pairs (kind 7) |
| `codegenCollectivePermuteImplicit` | `0xf23a40` | libwalrus | thin → codegenCollectiveOp(9) + channel_id |
| `codegenCollectivePermuteImplicitReduce` | `0xf21850` | libwalrus | dedicated, 2-in/1-out + reduce-op (kind 10) |
| `codegenCollectiveOp` | `0xf229a0` | libwalrus | shared group/k9 encoder (replica_groups) |
| `codegenAllReduce / AllGather / ReduceScatter / AllToAll` | `0xf23510 / 0xf23650 / 0xf23780 / 0xf23910` | libwalrus | thin group leaves (kinds 2/4/3/5) |
| `codegenOperatorSendRecvWrapper` | `0xf1d970` | libwalrus | refcount shim (kind 42) |
| `codegenOperatorCollectivePermuteWrapper` | `0xf22930` | libwalrus | refcount shim (kind 55) |
| `bir::InstBuilder::addLocalSendRecv` | `0x2b2780` | libBIR | mints the SendRecv IT48 |
| `codegenAluOp` | `0xf140c0` | libwalrus | klr::AluOp → bir::AluOpType (reduce-op) |
| `codegenTensorRef` | `0xf18b30` | libwalrus | klr::TensorRef → bir ML\*+AP |
| `returnAndIncrementCCId` | `0xf13d50` | libwalrus | per-CC name suffix |
| `sub_F1F790` | — | libwalrus | copy `vector<vector<u32>>` → replica_groups / src_target_pairs |

---

## 9. Grounding & limits

The five claims this page turns on, and how far each is pinned:

1. **The three-way collective split.** All eight leaf addresses are `nm -DC` rows. The two dedicated bodies (`0xf22110`, `0xf21850`) do not `call codegenCollectiveOp`, while `codegenCollectivePermuteImplicit` @ `0xf23a40` does. The addresses are byte-exact; "does not call the shared encoder" is a body read.
2. **The five fan-outs.** The *existence* of each branch is pinned. F2 is byte-anchored by `codegenGenericIndirectSave(std::shared_ptr<klr::NcDmaCopy>)` @ `0xf2eb10` taking the same klr type. The exact branch predicates (`klr+0x50`, `klr+0x44`, the dtype fork) are carried from the per-op codegen bodies, so those offsets are HIGH.
3. **The BIR-only ~60 opcodes.** HIGH. Grounded in the 110-opcode `InstructionType` census minus the ~50 leaf-reachable opcodes; the per-family origin is read from naming plus the pass roster rather than a per-opcode trace.
4. **~57 leaves → ~50 opcodes.** HIGH. The "~57" is the dispatched-leaf count and "~50" the distinct-opcode count after the §3 fan-ins; both are approximate by construction, since the abstract-parent versus sub-component boundary is a judgment call. The fan-in arithmetic is exact for the four families named.
5. **`CollectiveKind` multiplexing — one opcode, IT48, for all kinds.** `InstCollectiveCompute` is IT48; the kind is the orthogonal `+0xF8` sub-field; `sameInst` @ `0x2f4a80` keys on it ([7.9](./op-family-enums.md)). `codegenSendRecv`'s hardcoded `false` and the `getNeuronCoreId` / `addLocalSendRecv` call sequence are byte-witnessed in the body-frame disasm (`0xf1d60e`, `0xf1d750`, `0xf1d768`).

Two things stay [INFERRED] and are not raised above: the exact JSON key-name of the `+280` channel_id field, and that of the `+504` peer-affine field. The `op-family-enums` cross-reference resolves the kind ordering but not those two sub-field names.

---

## Cross-references

- [7.21 KlirToBirCodegen dispatch](./klir-codegen-dispatch.md) — the 77-case switch, refcount shims, the dispatch chain feeding every leaf here.
- [6.5.13 BirCodeGenLoop collective lowering](../nki/bircodegen-collective.md) — the **beta3** collective counterpart (same IT48 model, Penguin input).
- [7.9 Op-family enums](./op-family-enums.md) — `CollectiveKind`/`CollectiveDimension`/`CollectiveComputeTypeHint`; the "one opcode multiplexes 11 kinds" quirk; the L1/L2/L3 crosswalk.
- [7.10 InstructionType](./instruction-type.md) — the 110 L1 opcodes referenced by the IT column.
- *Part 13 (distribution layer — planned)* — the SPMD / replica-group / ICI cross-node path that consumes the fence-only (kind ≥3) collectives.
