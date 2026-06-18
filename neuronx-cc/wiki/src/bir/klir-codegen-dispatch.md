# 7.21 KlirToBirCodegen: the dispatch core & the master routing table

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311/cp312 twins present). Subject binary: `neuronxcc/starfish/lib/libwalrus.so` — the **Strand-I** backend shared object. It retains its **full dynamic symbol table**: `nm -DC libwalrus.so | rg KlirToBirCodegen` yields **176** demangled `class → method → address` rows directly, the authoritative roster used throughout. `.text` (`0xf00000+`) and `.rodata` (`0x1c72000+`) have VA == file-offset, so jump tables are read by file offset. All evidence below is from this binary: `nm`, `objdump -d` disasm, `strings`, and a Python decode of the `.rodata` jump table. Every claim is tagged CONFIRMED (read directly off this binary) / STRONG (multi-evidence) / INFERRED / SPECULATIVE.*

## Abstract

`KlirToBirCodegen` is the **beta2** lowering core that turns a serialized-KLR NKI kernel — a tree of `klr::Stmt` / `klr::Operator` nodes — into a graph of `bir::Instruction` nodes (the L1 Backend IR the walrus allocator and scheduler consume). It is one of **two parallel front-ends onto the same BIR node model**, not a stage in a pipeline. The sibling, `BirCodeGenLoop` (the **beta3** driver, documented in [6.5.10 BirCodeGenLoop](../nki/bircodegenloop.md)), **builds** a `bir::Module` top-down from the Penguin tensoriser-IR tree; `KlirToBirCodegen` instead **mutates** an already-built `bir::Module` in place, patching the contents of a serialized-KLR `bir::InstNKIKLIRKernel` placeholder into it. They converge on the identical `bir::Inst` data model from two different inputs (Penguin IR vs. KLR). This is a *sibling* relationship — neither feeds the other.

> **NOTE — beta2 is the dormant path in this build.** The active default driver is beta3 (`BirCodeGenLoop`). The beta2 internal-retrace path that consumes serialized KLR through `KlirToBirCodegen` is present and complete in `libwalrus.so` but is **largely dormant** at this version: it is the lowering used for the legacy `nki_klr` retrace, not the default Penguin lowering. The code is shipped (every leaf, the full 77-case switch, the context struct are all present and citeable), but it is exercised by a non-default front-end. Treat this page as the specification of a complete-but-secondary lowering path, kept in parity with beta3 on the BIR side. [STRONG — beta3 is the documented default driver; both build identical `bir::Inst` nodes]

> **METHOD — where the dispatch lives.** `KlirToBirCodegen` lives **only** in `libwalrus.so`. The `nki_klr_sim` driver is stripped and links `libBIRSimulator`/`libpwp_sim`/`libBIR` — it contains **zero** `KlirToBirCodegen` symbols (`nm -DC` + `readelf -d` confirm). The `0x5e9020–0x62d650` band in `libwalrus` is `.plt` thunks (each tail-jumps to a real body `>0xf00000`); every address cited below is the **real body** (an `nm` `T`/`W` row), never a thunk alias. [CONFIRMED]

| | |
|---|---|
| **Class** | `neuronxcc::backend::KlirToBirCodegen` (TU `klir_to_bir_codegen.cpp`) |
| **Binary** | `libwalrus.so` (Strand-I, cp310; cp311/cp312 twins) |
| **Roster** | 176 dynsym methods: 60 wrappers + 82 `codegen*` leaves/sub-encoders + 3 dispatch-core + 31 ctor/dtor/getter/walk |
| **Driving pass** | `TranslateNKIASTToBIR::run(bir::Module&)` @ `0xf0dbc0` → `lowerKLIRToNKI(…, KlirToBirCodegen const&)` @ `0xf09c40` |
| **Sibling driver** | `BirCodeGenLoop` (beta3 Penguin→BIR) — *not* a pipeline stage; see [6.5.10](../nki/bircodegenloop.md) |
| **Dispatch core** | `codegenStmt` @ `0xf33520` → `codegenStmtOperWrapper` @ `0xf325a0` → `codegenOperator` @ `0xf30db0` |
| **Master switch** | jump table `jpt @ 0x1de95f8`, **77 cases** (kinds 0..76), 17 → default-throw, 60 → wrappers |
| **Context ctor** | `KlirToBirCodegen(bir::Function*)` @ `0xf0faa0` — IS-A `logging::Logger` |

---

## 1. The dispatch chain — four levels

A KLR kernel is a `std::list` of statements. The lowering is a depth-first walk in which **every** statement must be an operator-bearing statement, every operator is one switch case, and every case is a uniform refcount shim into a per-op leaf. The chain is exactly four call levels deep below the walk:

```text
codegenLncKernel / inlineNoReorderBlock           [LEVEL 0]  statement-list WALK
  └─ codegenStmt(shared_ptr<klr::Stmt>)            [LEVEL 1]  Stmt-kind guard (must == 1)
       └─ codegenStmtOperWrapper(...)              [LEVEL 2]  unwrap + debug-info + device-print
            └─ codegenOperator(...)                [LEVEL 3]  THE op-kind switch (77-case jump table)
                 └─ codegenOperator<Op>Wrapper(...) [LEVEL 4] refcount shim (60 of these)
                      └─ codegen<Op>(shared_ptr<klr::Op>)  [LEAF] builds bir::Instruction*
```

CONFIRMED — all four core addresses are `nm -DC` rows and the chain is read off the disasm (§§2–5).

### LEVEL 0 — the per-statement walk

```c
// codegenLncKernel(shared_ptr<klr::LncKernel>)  @0xf34140  [CONFIRMED]
bir::Instruction* codegenLncKernel(KlirToBirCodegen *this, klr::LncKernel *k) {
    // Pre-resolve per-statement names against the already-built BIR Function:
    //   bir::Function::getMemoryLocationByName / getBasicBlockByName  (name-keyed lookup)
    for (auto &node : k->statements) {              // std::list walk
        shared_ptr<klr::Stmt> stmt = node;          // refcount-bump
        try {
            codegenStmt(this, stmt);                // LEVEL 1 (decompiled @ line 728)
        } catch (std::runtime_error &e) {
            collectError(fmt("<...>': %s", e.what()));   // deferred error sink (line 708)
        }
        // refcount-drop
    }
    codegenDependencyEdges(this, k);                // @0xf1e450 — ONE post-walk pass (line 1064)
}
```

The walk pre-resolves BasicBlock and MemoryLocation names against the **already-built** `bir::Function` before the loop — the first concrete sign that this driver *patches into* an existing Module rather than constructing one. Each statement is lowered inside a `try`/`catch`; a thrown `std::runtime_error` is formatted and funneled into `collectError` (@ `0xf1f190`) for later retrieval via `getFormattedErrors` (@ `0xf14450`) — lowering does not abort on the first bad node. After the walk, **one** call to `codegenDependencyEdges` wires the BIR dependency edges across all emitted instructions. `inlineNoReorderBlock` (@ `0xf335a0`) is the secondary walker that re-enters `codegenStmt` for inlined no-reorder statement groups; per the callgraph these two are the **only** callers of `codegenStmt`. [CONFIRMED both callers]

### LEVEL 1 — `codegenStmt`: the Stmt-kind guard

```c
// codegenStmt(shared_ptr<klr::Stmt>)  @0xf33520  [CONFIRMED full body]
//   0xf3352a:  cmpl $0x1, (%rax)        ; *(int*)(*sp) == klr::Stmt kind @ offset 0
//   0xf3352d:  jne  6def3c              ; -> throw "Unsupported statement type encountered"
bir::Instruction* codegenStmt(KlirToBirCodegen *this, shared_ptr<klr::Stmt> stmt) {
    int kind = *(int*)stmt.get();                   // kind field @ offset 0
    if (kind != 1)
        throw std::runtime_error("Unsupported statement type encountered");
    // kind == 1  =>  the Stmt IS a klr::StmtOperWrapper; alias the same control block:
    return codegenStmtOperWrapper(this, reinterpret_cast<klr::StmtOperWrapper>(stmt));
}
```

Only `klr::Stmt` **kind == 1** is accepted — that one kind is a `klr::StmtOperWrapper`. The disasm shows `cmpl $0x1,(%rax)` immediately followed by `jne` to the throw site at `0x6def3c`. Control-flow and other non-operator statement kinds are rejected here: in this lowering path **every** statement is operator-bearing. The `shared_ptr` is reinterpreted onto the same control block (refcount bumped) and forwarded down. [STRONG — kind==1 == StmtOperWrapper, from the guard + the only downstream call target]

### LEVEL 2 — `codegenStmtOperWrapper`: unwrap, debug-info, device-print

This is the only level that does post-lowering bookkeeping; the wrapper level below (LEVEL 4) carries none. The `klr::StmtOperWrapper` object layout (offsets from `*sp`):

| Offset | Field | Meaning |
|---|---|---|
| `+8` | `shared_ptr<klr::Operator>` | the inner op (pulled into a local, refcount-bumped) |
| `+24`/`+32` | `std::string` op-name | debug name (ptr/len) |
| `+56` | `bool` | has op-name? (if 0 → default name `"nki-op"`) |
| `+64` | `klr::*` | node carrying the kernel-name string at `+176`/`+184` |

```c
// codegenStmtOperWrapper(shared_ptr<klr::StmtOperWrapper>)  @0xf325a0  [CONFIRMED]
bir::Instruction* codegenStmtOperWrapper(KlirToBirCodegen *this, klr::StmtOperWrapper *w) {
    // A — LOWER
    bir::Instruction *inst = codegenOperator(this, &w[+8]);   // LEVEL 3 (line 113)
    if (!inst) return nullptr;

    // B — DEBUG INFO bookkeeping on the produced instruction
    std::string label = w->hasName ? w->name : "nki-op";
    bir::OpDebugInfo dbg(/*id-tag=*/10, label);
    dbg.setKernelName( /* kernel-name from w[+64].+176 */ );
    inst->setDebugInfo(dbg);
    *(int*)(inst + 0x38) = 2;                                 // mark "lowered/codegen-origin" (line 304)
    if (w->hasName && !isCollective(inst))
        BasicBlock::setNameForElement(inst->container, w->name);

    // C — COMPILER-GENERATED DEVICE-PRINT injection (gated, lines 311-509)
    if (this->deviceDumpFlag) {                               // KlirToBirCodegen+0x1B0 (setDeviceDump)
        for (AccessPattern out : inst->outputs)
            if (out.memloc->dtype != /*fp32=*/32)
                injectDevicePrint("COMPILER-GENERATED-COREID-<id>-DevicePrint-",
                                  Module::getNeuronCoreId(getMod()), returnAndIncrementId());
    }
    return inst;
}
```

Three things happen here. **(A)** It calls down into the op switch and bails on a null result. **(B)** It stamps the produced `bir::Instruction` with an `OpDebugInfo` (op label = the wrapper's name string or the literal `"nki-op"`; kernel name pulled from the `+64.+176` node), writes the lowered-origin marker `*(int*)(inst+0x38) = 2`, and — when the wrapper carries a name and the instruction is not a collective — names the instruction via `BasicBlock::setNameForElement`. **(C)** When the device-dump flag (`this+0x1B0`, set by `setDeviceDump`) is on, it injects an `InstDevicePrint` for every output AccessPattern whose `MemoryLocation` dtype is not fp32 (`!= 32`), named `"COMPILER-GENERATED-COREID-<id>-DevicePrint-"`. This is the source of the `COREID` device-print spam in device-dump builds. The literals `"nki-op"`, `"dummy_dbg_info"`, and `"COMPILER-GENERATED-COREID-"` are all present in `.rodata` (verified via `strings`). [STRONG]

### LEVEL 3 — `codegenOperator`: the master switch

```c
// codegenOperator(shared_ptr<klr::Operator>)  @0xf30db0  [CONFIRMED full body + switch]
//   0xf310df:  mov  (%rax),%rax           ; *(*a2)
//   0xf310e2:  cmpl $0x4c, (%rax)         ; kind > 76 ?  (0x4c == 76)
//   0xf310e5:  ja   6de4be                ; -> default throw
//   0xf310eb:  mov  (%rax),%edx           ; kind
//   0xf310ed:  lea  0xeb8504(%rip),%rcx   ; %rcx = 0x1de95f8  (jump-table base, .rodata)
//   0xf310f4:  movslq (%rcx,%rdx,4),%rdx  ; 4-byte signed self-relative offset
//   0xf310f8:  add  %rcx,%rdx
//   0xf310fb:  jmp  *%rdx                 ; INDIRECT dispatch
bir::Instruction* codegenOperator(KlirToBirCodegen *this, shared_ptr<klr::Operator> *op) {
    log_debug("<kind>");                              // boost::log per-op trace (decimal kind)
    int kind = *(int*)(*op).get();                    // kind @ offset 0
    if ((unsigned)kind > 76)
        throw std::runtime_error("Unsupported operator " + to_string(kind));
    switch (kind) {                                   // jpt @ 0x1de95f8, 77 entries
        case 22: { aliasBump(op); auto r = codegenOperatorNcMatMulWrapper(this, op); drop(op); return r; }
        /* ... 59 more uniform cases ... */
        default: throw std::runtime_error("Unsupported operator " + to_string(kind));
    }
}
```

The discriminant is the `klr::Operator` kind field at **offset 0**. The bound check `cmpl $0x4c` (76) with `ja` to the default site confirms the case domain is exactly kinds **0..76 — 77 cases**. The jump table base `0x1de95f8` is materialized by `lea 0xeb8504(%rip)`, entries are 4-byte **signed self-relative** offsets, and dispatch is the indirect `jmp *%rdx` at `0xf310fb`. A `boost::log` debug record carrying the decimal kind is emitted before the switch — a per-op trace log. Each valid case is uniform: alias the inner `shared_ptr` (refcount-bump), call the matching `codegenOperator<Op>Wrapper`, refcount-drop, return the instruction. [CONFIRMED — `switches.json` analogue reproduced by directly decoding the 77 `.rodata` entries; see §3]

### LEVEL 4 — `codegenOperator<Op>Wrapper`: the templated refcount shim

```c
// codegenOperatorNcMatMulWrapper(shared_ptr<klr::OperatorNcMatMulWrapper>)  @0xf19cf0  [CONFIRMED]
//   0xf19cf6:  mov    (%rsi),%rax
//   0xf19cf9:  movdqu 0x8(%rax),%xmm0     ; concrete klr::NcMatMul shared_ptr @ +8
//   0xf19d1a:  addl   $0x1,0x8(%rax)      ; refcount-bump
//   0xf19d21:  call   codegenNcMatMul     ; tail into the per-op LEAF (real body @0xf19590)
//   0xf19d33:  call   ~shared_ptr         ; refcount-drop
//   ... returns the bir::Instruction*
bir::Instruction* codegenOperatorNcMatMulWrapper(KlirToBirCodegen *this, klr::OperatorNcMatMulWrapper *w) {
    shared_ptr<klr::NcMatMul> inner = w->node_at_plus8;   // ptr@+0, ctrl-block ref
    inner.ctrl->refcount++;
    bir::Instruction *r = codegenNcMatMul(this, &inner);  // the leaf
    inner.~shared_ptr();
    return r;
}
```

`klr` emits a uniform `klr::Operator` base whose concrete subclass is `klr::Operator<Op>Wrapper` — a thin envelope around the bare `klr::<Op>` node, holding the concrete node's `shared_ptr` at `+8`. The wrapper's only job is to **strip that envelope**: load `+8`, own a ref, forward to the same-named leaf, drop the ref. No naming, debug, or device-print logic lives here — all of that is in LEVEL 2. This is the instantiation pattern for **all 60 wrappers**; the disasm above (NcMatMul) is the canonical exemplar. [STRONG — pattern confirmed against the NcMatMul shim; 60 wrappers share the layout]

---

## 2. Why a wrapper layer at all

The two intermediate layers (`StmtOperWrapper`, `Operator<Op>Wrapper`) exist because KLR serializes each operator twice-boxed: a `klr::Stmt` envelope (kind==1) around a `klr::StmtOperWrapper`, which holds a `klr::Operator` base, whose runtime subtype is `klr::Operator<Op>Wrapper` around the bare `klr::<Op>`. Unwrapping is split so that **bookkeeping happens once, at the statement level** (LEVEL 2), while **type-recovery happens per-op** (LEVEL 4). That separation is what lets the 60 per-op shims stay trivial and identical — the only per-op state that ever differs is *which leaf is called*, which is precisely the switch's job. The cost is two extra `shared_ptr` aliasings per instruction; the benefit is that adding an operator is one switch case + one wrapper + one leaf, with zero touch to the bookkeeping path.

---

## 3. The master routing table — `klr::Operator` kind → wrapper → leaf

The table below is the I-strand index: each row is one valid switch case in `codegenOperator` (@ `0xf30db0`). Wrapper and leaf addresses are `nm -DC` bodies. The 77-entry jump table at `0x1de95f8` was decoded directly from `.rodata`: **17 entries point at the default-throw target `0x6de4be`** (kinds `{0,1,4,8,11,17,18,19,21,24,27,30,43,58,62,63,70}`), and the remaining **60 entries** point at 60 distinct wrappers — yielding **61 distinct jump targets** (60 wrappers + 1 default). [CONFIRMED — Python decode of the 77 `.rodata` int32 entries reproduced the exact reserved-kind set]

| kind | Wrapper `@addr` | Leaf `codegen…@addr` | klr node | notes |
|----|----|----|----|----|
| 2 | NcActivate `@0xf194b0` | codegenNcActivate `@0xf18ba0` | `klr::NcActivate` | shares leaf w/ 3 |
| 3 | ActivationReduce `@0xf19520` | codegenNcActivate `@0xf18ba0` | `klr::NcActivate` | reduce-fold of kind 2 |
| 5 | NcAffineSelect `@0xf29da0` | codegenNcAffineSelect `@0xf29690` | `klr::NcAffineSelect` | |
| 6 | BatchNormAggregate `@0xf1b360` | codegenBatchNormAggregate `@0xf1b0d0` | `klr::BatchNormAggregate` | |
| 7 | BatchNormStats `@0xf1b060` | codegenBatchNormStats `@0xf1add0` | `klr::BatchNormStats` | |
| 9 | NcCopy `@0xf2dca0` | codegenNcCopy `@0xf2d120` | `klr::NcCopy` | |
| 10 | CopyPredicated `@0xf1ad60` | codegenCopyPredicated `@0xf1a9c0` | `klr::CopyPredicated` | |
| 12 | NcDmaCopy `@0xf30d40` | codegenNcDmaCopy `@0xf2fa40` | `klr::NcDmaCopy` | |
| 13 | DmaTranspose `@0xf2eaa0` | codegenDmaTranspose `@0xf2dd10` | `klr::DmaTranspose` | |
| 14 | Dropout `@0xf280c0` | codegenDropout `@0xf27d30` | `klr::Dropout` | |
| 15 | FindIndex8 `@0xf1d050` | codegenFindIndex8 `@0xf1cc80` | `klr::FindIndex8` | spec8 |
| 16 | Iota `@0xf1d3b0` | codegenIota `@0xf1d0c0` | `klr::Iota` | |
| 20 | NcLocalGather `@0xf2af80` | codegenNcLocalGather `@0xf2a9c0` | `klr::NcLocalGather` | |
| 22 | NcMatMul `@0xf19cf0` | codegenNcMatMul `@0xf19590` | `klr::NcMatMul` | → InstMatmult |
| 23 | MatchReplace8 `@0xf2a520` | codegenMatchReplace8 `@0xf29e10` | `klr::MatchReplace8` | spec8 |
| 25 | Max8 `@0xf1cc10` | codegenMax8 `@0xf1c970` | `klr::Max8` | spec8 |
| 26 | MemSet `@0xf1a0d0` | codegenMemSet `@0xf19d60` | `klr::MemSet` | |
| 28 | NcRangeSelect `@0xf1c900` | codegenNcRangeSelect `@0xf1c210` | `klr::NcRangeSelect` | |
| 29 | Reciprocal `@0xf1a950` | codegenReciprocal `@0xf1a5a0` | `klr::Reciprocal` | |
| 31 | NcScalarTensorTensor `@0xf2c880` | codegenNcScalarTensorTensor `@0xf2c450` | `klr::NcScalarTensorTensor` | |
| 32 | Shuffle `@0xf288a0` | codegenShuffle `@0xf28130` | `klr::Shuffle` | |
| 33 | TensorReduce `@0xf1a530` | codegenTensorReduce `@0xf1a140` | `klr::TensorReduce` | |
| 34 | TensorScalar `@0xf2c3e0` | codegenTensorScalar `@0xf2bf80` | `klr::TensorScalar` | |
| 35 | TensorTensor `@0xf1bc10` | codegenTensorTensor `@0xf1b820` | `klr::TensorTensor` | |
| 36 | TensorTensorScan `@0xf2b980` | codegenTensorTensorScan `@0xf2b450` | `klr::TensorTensorScan` | |
| 37 | TensorPartitionReduce `@0xf1b7b0` | codegenTensorPartitionReduce `@0xf1b3d0` | `klr::TensorPartitionReduce` | |
| 38 | TensorScalarReduce `@0xf2bf10` | codegenTensorScalarReduce `@0xf2b9f0` | `klr::TensorScalarReduce` | |
| 39 | Transpose `@0xf29620` | codegenTranspose `@0xf28fb0` | `klr::Transpose` | |
| 40 | SelectReduce `@0xf27650` | codegenSelectReduce `@0xf270a0` | `klr::SelectReduce` | |
| 41 | SequenceBounds `@0xf27cc0` | codegenSequenceBounds `@0xf276c0` | `klr::SequenceBounds` | |
| 42 | SendRecv `@0xf1d970` | codegenSendRecv `@0xf1d420` | `klr::SendRecv` | |
| 44 | TensorLoad `@0xf26d90` | codegenTensorLoad `@0xf26a60` | `klr::TensorLoad` | |
| 45 | TensorStore `@0xf269f0` | codegenTensorStore `@0xf266c0` | `klr::TensorStore` | |
| 46 | RegisterMove `@0xf26650` | codegenRegisterMove `@0xf26370` | `klr::RegisterMove` | |
| 47 | CmpBranch `@0xf340d0` | codegenCmpBranch `@0xf33a70` | `klr::CmpBranch` | control |
| 48 | RegisterAluOp `@0xf26300` | codegenRegisterAluOp `@0xf25f10` | `klr::RegisterAluOp` | |
| 49 | QuantizeMX `@0xf25ea0` | codegenQuantizeMX `@0xf25ad0` | `klr::QuantizeMX` | → InstQuantizeMx |
| 50 | NcMatMulMX `@0xf25a60` | codegenNcMatMulMX `@0xf25320` | `klr::MatMulMX` | → InstMatmultMx |
| 51 | DmaCompute `@0xf1e250` | codegenDmaCompute `@0xf1d9e0` | `klr::DmaCompute` | |
| 52 | AllReduce `@0xf235e0` | codegenAllReduce `@0xf23510` † | `klr::CollectiveOp` | coll |
| 53 | AllGather `@0xf23710` | codegenAllGather `@0xf23650` † | `klr::CollectiveOp` | coll |
| 54 | ReduceScatter `@0xf238a0` | codegenReduceScatter `@0xf23780` † | `klr::CollectiveOp` | coll |
| 55 | CollectivePermute `@0xf22930` | codegenCollectivePermute `@0xf22110` † | `klr::CollectiveOp` | coll |
| 56 | CollectivePermuteImplicit `@0xf23ba0` | codegenCollectivePermuteImplicit `@0xf23a40` † | `klr::CollectiveOp` | coll |
| 57 | CollectivePermuteImplicitReduce `@0xf220a0` | codegenCollectivePermuteImplicitReduce `@0xf21850` † | `klr::CollectiveOp` | coll |
| 59 | AllToAll `@0xf239d0` | codegenAllToAll `@0xf23910` † | `klr::CollectiveOp` | coll |
| 60 | RankId `@0xf27030` | codegenRankId `@0xf26e00` | `klr::RankId` | |
| 61 | CurrentProcessingRankId `@0xf217e0` | codegenCurrentProcessingRankId `@0xf211b0` | `klr::CurrentProcessingRankId` | |
| 64 | CoreBarrier `@0xf23f40` | codegenCoreBarrier `@0xf23c10` | `klr::CoreBarrier` | |
| 65 | Rng `@0xf247f0` | codegenRng `@0xf245c0` | `klr::Rng` | |
| 66 | Rand2 `@0xf252b0` | codegenRand2 `@0xf24ff0` | `klr::Rand2` | |
| 67 | RandGetState `@0xf24f80` | codegenRandGetState `@0xf24d60` | `klr::RandGetState` | |
| 68 | SetRngSeed `@0xf24a60` | codegenSetRngSeed `@0xf24860` | `klr::SetRngSeed` | |
| 69 | RandSetState `@0xf24cf0` | codegenRandSetState `@0xf24ad0` | `klr::RandSetState` | |
| 71 | TensorScalarCumulative `@0xf2b3e0` | codegenTensorScalarCumulative `@0xf2aff0` | `klr::TensorScalarCumulative` | |
| 72 | NcNGather `@0xf2a950` | codegenNcNGather `@0xf2a590` | `klr::NcNGather` | |
| 73 | NonzeroWithCount `@0xf2cd70` | codegenNonzeroWithCount `@0xf2c8f0` | `klr::NonzeroWithCount` | |
| 74 | DevicePrint `@0xf2d0b0` | codegenDevicePrint `@0xf2cde0` | `klr::DevicePrint` | explicit print |
| 75 | Exponential `@0xf24550` | codegenExponential `@0xf23fb0` | `klr::Exponential` | |
| 76 | Activate2 `@0xf2f9d0` | codegenActivate2 `@0xf2f3a0` | `klr::Activate2` | act |

**Reserved / default-throw kinds** (jump-table entry → `0x6de4be`): `0, 1, 4, 8, 11, 17, 18, 19, 21, 24, 27, 30, 43, 58, 62, 63, 70`. These 17 kinds raise `std::runtime_error("Unsupported operator <kind>")`. [CONFIRMED]

> **† Collectives share one body.** Kinds 52–57 and 59 are seven distinct wrappers and seven thin leaves, but every leaf delegates to the shared helper `codegenCollectiveOp(shared_ptr<klr::CollectiveOp>, bir::CollectiveKind)` @ `0xf229a0`. The per-op leaf does nothing but pin the `bir::CollectiveKind` argument (AllReduce / AllGather / ReduceScatter / AllToAll / CollectivePermute[Implicit][Reduce]) and tail into the shared body. [CONFIRMED]

> **NcActivate shared by two kinds.** Kinds 2 (`NcActivate`) and 3 (`ActivationReduce`) both route to leaf `codegenNcActivate` @ `0xf18ba0` (two wrappers, one leaf): they share the activation-engine codegen and differ only by reduce-fold flags. [CONFIRMED — `nm` shows a single `codegenNcActivate`, two `…Wrapper` shims]

**Routing arithmetic.** 60 valid kinds → 60 wrappers → **57 distinct leaves**: the activation engine is shared by 2 kinds (−1 leaf), and the 7 collective kinds reach 7 thin leaves all over the *one* shared `codegenCollectiveOp` body (so the seven thin leaves exist as symbols but route to a single substantive codegen). [STRONG]

---

## 4. Sub-encoders — the operand grammar shared by the leaves

The leaf bodies are not self-contained: they share a set of value-codegen / sub-encoder helpers that translate KLR operand sub-trees into BIR arguments and access patterns. These are **not** op-dispatched — they are called directly by the leaves. The full `codegen*` roster in `libwalrus` is **82** `codegen*` methods that are neither the 60 wrappers nor the 3 dispatch-core entries nor the LncKernel walk; of these, ~57 are the per-op leaves of §3 and the remainder are the sub-encoders below plus a few variant leaves (`codegenGenericIndirectSave`, `codegenImmediateIntWrapper`, `codegenImmediateFloatWrapper`). [CONFIRMED `nm` enumeration — see §6 GOTCHA on the 81-vs-82 count]

| Sub-encoder `@addr` | Role |
|---|---|
| `codegenOperand` @ `0xf1bc80` | operand → BIR arg (entry) |
| `addOperandToInst` @ `0xf1be80` | `codegenOperand` + append to inst operand list (ImmediateValue / `bir::Argument` fallback) |
| `codegenTensorRef` @ `0xf18b30` / `codegenTensorRefImpl(.., bool)` @ `0xf18960` | tensor ref → BIR access (impl carries the dynamic flag) |
| `codegenAccess` @ `0xf18820` / `codegenAccessSimple` @ `0xf186d0` | access node → BIR access |
| `codegenBirAccessPattern` @ `0xf18430` | `klr::BirAccessPattern` → BIR access pattern |
| `codegenAP` @ `0xf13f50` | access-pattern pair list (`list<APPair>`) |
| `codegenTensorName` @ `0xf17660` / `codegenIOTensorName(.., bool)` @ `0xf14b80` | `klr::TensorName` → BIR MemoryLocation |
| `codegenHbm` @ `0xf15060` | HBM tensor |
| `codegenImmediate` @ `0xf16980` | immediate → `bir::ImmediateValue` |
| `codegenDtype` @ `0xf14060` / `codegenEngine` @ `0xf140f0` / `codegenAluOp` @ `0xf140c0` | enum-map KLR → BIR enums |
| `codegenActivationFunc` @ `0xf14040` / `codegenAccumCmd` @ `0xf14110` / `codegenTensorSubDim` @ `0xf140e0` | enum-map KLR → BIR enums |
| `codegenRegister(string, bir::EngineType)` @ `0xf141e0` | named scalar register |
| `assembleDynamicInfo` @ `0xf20230` / `extractDynamicApToStandAlone` @ `0xf1e2c0` | dynamic access-pattern info |
| `codegenDependencyEdges` @ `0xf1e450` | post-walk: wire BIR dependency edges |
| `getIdentityMatrix(bir::Dtype)` @ `0xf28910` / `getActBiasTensor` @ `0xf16bc0` | const tensors for transpose/matmul/activation |
| `legalizeIdentityMatrices` @ `0xf14320` / `legalizeActBiasTensorAddress` @ `0xf16a90` | late address fixups |
| `getBankId(TensorName, uint, uint)` @ `0xf14ac0` | PSUM/SBUF bank-id resolve |

These sub-encoders are documented per-family in the leaf pages: access/indexing in [7.22 codegenAccess](codegen-access.md) *(planned)*, and the per-op leaves in [7.26–7.30](codegen-leaves.md) *(planned)*.

---

## 5. The codegen context — `KlirToBirCodegen` object state

```c
// ctor: KlirToBirCodegen(bir::Function*)  @0xf0faa0  [CONFIRMED ctor + getters]
//  - BASE at offset 0: logging::Logger (constructed from the demangled type-name
//    "neuronxcc::backend::KlirToBirCodegen" via __cxa_demangle). The object IS-A Logger,
//    which is why the boost::log open_record calls in codegenOperator/StmtOperWrapper log through `this`.
//  - Asserts parent module: FunctionHolder::getModule() != null
//      else fatal: 'curModule != nullptr && "ERROR: parent module of FunctionHolder is not set!"'
```

The codegen object is a stateful sink: it IS-A `logging::Logger` (its base subobject at offset 0), holds the current Module/Function, monotonic ID counters, and the resolved arch-model SBUF/PSUM budgets. Offsets recovered via the getter bodies (word index = byte/8):

| Offset | Accessor `@addr` | State |
|---|---|---|
| `+0` | — | `logging::Logger` base subobject |
| `+0x38` | `getMod` @ `0xf142f0` | `bir::Module*` (current module); ctor also stores the `bir::Function*` here-region |
| `+0xF0` | `returnAndIncrementId` @ `0xf13b50` | monotonic instruction-ID counter (read + increment) |
| `+? (CCId)` | `returnAndIncrementCCId` @ `0xf13d50` | parallel collective-comm ID counter |
| `+0x110` | `getIsAllocated` @ `0xf14310` | `bool` — allocation-done flag |
| `+0x118` | `getSbufMaxBytes` @ `0xf14420` | SBUF byte budget |
| `+0x154` | (divisor) | PSUM bank size |
| `+0x158` | `getPsumMaxBankId` @ `0xf14430` | PSUM total; `getPsumMaxBankId = (psumTotal-1)/bankSize` (0 if empty) |
| `+0x1B0` | `setDeviceDump` @ `0xf109a0` | `bool` device-dump flag (gates the COREID DevicePrint injection in LEVEL 2) |

Supporting state: a name-uniquing hashtable (`Hashtable<string,string>` near `+0x1E8`/`+496`) used in `codegenLncKernel` to de-duplicate instruction names; an `OpDebugInfo` scratch (`"dummy_dbg_info"` template, set in ctor); and the deferred error log behind `collectError` @ `0xf1f190` / `getFormattedErrors` @ `0xf14450`. `setParentTensorizerId(boost::optional<unsigned long>)` @ `0xf14300` threads a nested-tensorizer id; `isBasicBlockUseful` @ `0xf14a10` / `locateSharedConstants` @ `0xf14240` support block pruning.

**What the driver reads.** `TranslateNKIASTToBIR::lowerKLIRToNKI` (@ `0xf09c40`) takes `KlirToBirCodegen const&` purely to read the alloc budgets — `getSbufMaxBytes()`, `getPsumMaxBankId()`, `getIsAllocated()`. The codegen object is the carrier that brings the resolved arch-model SBUF/PSUM limits into the NKI-function lowering. [STRONG — `const&` parameter + the three getters are the only `const` reads exposed]

**KLR-value → BIR-argument binding is name-keyed, not a map.** There is no single `std::map<klr-id, bir-arg>` field. `codegenTensorName` / `codegenHbm` resolve a `klr::TensorName` to a BIR MemoryLocation via `bir::Function::getMemoryLocationByName` — a name-keyed lookup into the already-built `bir::Function`'s named-element container, keyed by the KLR node's name string (the same container `setNameForElement` writes into at LEVEL 2). Operand identity therefore flows through the BIR Function's named-element table, not a KLR-id translation table. This is the structural consequence of beta2 being a *patch-into-existing-Module* driver. [STRONG — `getMemoryLocationByName` is an undefined import resolved against `libBIR`; the binding is by-name]

---

## 6. Adversarial self-verification

The five strongest claims, re-checked against the binary:

1. **77-case switch.** CONFIRMED. `cmpl $0x4c` (76) + `ja` to default at `codegenOperator+0x..`; jump-table base `0x1de95f8` materialized by `lea 0xeb8504(%rip)`; 4-byte signed self-relative entries; indirect `jmp *%rdx` @ `0xf310fb`. A Python decode of the 77 `.rodata` int32s gave **17** entries → default `0x6de4be` (exact reserved-kind set) and **60** → wrappers, i.e. 61 distinct targets.

2. **4-level dispatch.** CONFIRMED. `codegenStmt@0xf33520` → `codegenStmtOperWrapper@0xf325a0` → `codegenOperator@0xf30db0` → `codegenOperator<Op>Wrapper` → leaf — all four are `nm -DC` bodies and the chain is read off disasm (NcMatMul wrapper @ `0xf19cf0` tail-calls `codegenNcMatMul` @ `0xf19590`).

3. **60 wrappers + leaf split.** Wrappers: CONFIRMED **60** (`nm -DC | rg 'codegenOperator\w+Wrapper' | wc -l` = 60). Leaves: the precise binary count of non-wrapper, non-dispatch, non-walk `codegen*` members is **82**, not the 81 cited in earlier I-strand notes. The 1-row delta is the variant leaves `codegenGenericIndirectSave` / `codegenImmediateIntWrapper` / `codegenImmediateFloatWrapper` being grouped differently than `codegenDependencyEdges` (a post-walk pass, not a leaf). **82 is the binary truth**; "81" is a grouping artifact. The §3 routing table is unaffected: 60 valid kinds → 57 distinct *substantive* leaves.

   > **GOTCHA — the leaf count is a definitional choice, not a discrepancy.** "How many leaves" depends on whether you count the seven thin collective leaves (each a 1-line shim over `codegenCollectiveOp`), the enum-map sub-encoders, and `codegenDependencyEdges`. The unambiguous binary facts are: **176** total `KlirToBirCodegen` dynsym methods; **60** wrappers; **3** dispatch-core (`codegenStmt`/`codegenStmtOperWrapper`/`codegenOperator`); **82** other `codegen*` methods. Cite those, not a derived "81 leaves."

4. **Parallel, not pipeline (vs. BirCodeGenLoop).** CONFIRMED structurally. `TranslateNKIASTToBIR::run(bir::Module&)` @ `0xf0dbc0` → `lowerKLIRToNKI(bir::InstNKIKLIRKernel&, bir::Function&, …, KlirToBirCodegen const&)` @ `0xf09c40` *mutates* an existing `bir::Module` (it takes a `bir::Module&` and an `InstNKIKLIRKernel&` placeholder). `BirCodeGenLoop` (beta3) constructs a `bir::Module` from scratch from Penguin IR. Neither consumes the other's output; both emit identical `bir::Inst` nodes. The beta3 page ([6.5.10](../nki/bircodegenloop.md)) states the same from its side.

5. **Context struct.** CONFIRMED for every cited getter address (`getMod@0xf142f0`, `getSbufMaxBytes@0xf14420`, `getPsumMaxBankId@0xf14430`, `getIsAllocated@0xf14310`, `returnAndIncrementId@0xf13b50`, `setDeviceDump@0xf109a0`, ctor `@0xf0faa0`). The exact byte-offsets within the object (`+0x38`, `+0xF0`, `+0x118`, `+0x1B0`, etc.) are read from the getter bodies; the `+0x154`/`+0x158` PSUM split is INFERRED from the `getPsumMaxBankId` division and is the one offset that would most reward a second disasm pass.

**Honest re-verify ceiling.** The dispatch core, the 77-case switch, the 60-wrapper roster, the collective/NcActivate sharing, the diagnostic strings, and the driver relationship are all CONFIRMED off this binary. The per-row klr-node *names* in §3 are STRONG (derived from the wrapper/leaf demangled symbol names, which embed the op name) rather than independently re-derived from each switch case's literal log string. The exact PSUM-offset split (`+0x154`/`+0x158`) and the "beta2 dormant in this build" claim are the two STRONG-not-CONFIRMED items: dormancy is an architectural inference (beta3 is the documented default), not a runtime trace.

---

## Cross-references

- [6.5.10 BirCodeGenLoop: the beta3 Penguin→BIR driver](../nki/bircodegenloop.md) — the **sibling** driver onto the same BIR node model (the active default; this page is the beta2 sibling).
- [7.22 codegenAccess & the operand grammar](codegen-access.md) *(planned)* — the §4 sub-encoders in depth (`codegenTensorRef`, `codegenBirAccessPattern`, `codegenAP`).
- [7.26–7.30 the per-op leaves](codegen-leaves.md) *(planned)* — the substantive `codegen<Op>` bodies (matmul, activation, collectives, DMA, RNG) reached by the §3 table.
