# BirCodeGenLoop Collective Codegens: AllGather / AllReduce / ReduceScatter / AlltoAll / Permute / SendRecv(CCE) / CoreBarrier / RankId

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The subject is `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (unstripped, with DWARF — a Cython goldmine). VAs are cp310 `__pyx_pw…` body offsets; the cp311/cp312 siblings live under the same path with different addresses, so treat every address as version-pinned. The auto-generated `generated/BirCodeGenLoopGen.cpython-3xx.so` is a **separate module** that supplies the `super()` bases — do not conflate the two.*

## Abstract

`BirCodeGenLoop` is **layer 2 of Strand-P** — it walks the Penguin *tensoriser IR* one op at a time and builds Backend IR (BIR) **directly** via the Python `birpy.Instruction` binding. The sibling page [§6.5.x BirCodeGen Compute Codegens](./bircodegen-compute.md) covers the matmul/activation/tensor subset. This page documents the **collective** subset: the ~21 `codegen<Op>` methods that lower a distributed-comm Penguin op into the BIR collective node family — `InstCollectiveCompute` (IT48), `InstCollectiveSend` (IT49), `InstCollectiveRecv` (IT50), `InstCoreBarrier` (IT87), and `InstGetGlobalRankId` (IT11).

The central structural finding is that the **non-tiled abstract group collectives build the IT48 node themselves** — `AllGather` / `AllReduce` / `ReduceScatter` / `AlltoAll` / `CollectivePermute` / `CollectivePermuteReduce` / `Broadcast` each call `self.addInstruction(CollectiveCompute(bb, id, debug_info))` directly, with **no `super()`**. The **Tiled** variants and a handful of others (`SendRecvCCEOp`, `CoreBarrierOp`, `GetGlobalRankId`, `BroadcastPartition`) are `super()` overrides whose IT-node allocation lives in the generated `BirCodeGenLoopGen` base; the impl override only *configures* the node. This page pins both halves: the canonical group-collective emit template, the reducer-vs-non-reducer `setop` split, the **implicit `cc_dim`** (the `CollectiveDimension` is carried in the access-pattern geometry, never as a top-level scalar), the FSDP-only `setCcTypeHint`, the multi-recv-and-reduce `SendRecvCCEOp`, and the SPMD control nodes `CoreBarrier`/`GetGlobalRankId`.

This is the **beta3 analog** of the libwalrus C++ `klr → BIR` collective leaf ([§Part 8 walrus lower-local-collectives]). The two front-ends are **twin emitters that converge on the same BIR node family** — not a pipeline. The forward Penguin-op *builders* (the upstream side that stamps `inst.kind`) are [§6.5.4 NeuronCodegen Collectives](./neuroncodegen-collectives.md); this page is the emission side that reads those attributes back out.

For reimplementation, the contract is:

- The **canonical group-collective emit order** (12 configured fields + the operand/result AP bind), recovered in program order from the `codegenAllGatherOp` disasm.
- The **direct-build vs `super()`-override split** — which methods allocate IT48 themselves and which delegate to the `Gen` base.
- The **reducer marshalling rule**: only `AllReduce`/`ReduceScatter`/`CollectivePermuteReduce`(+Tiled) wrap the op in `ALUOpcode`; everyone else passes `setop` through.
- The **implicit `CollectiveDimension`**: no `cc_dim`/`setDim` setter exists; the dimension is encoded by the AP builder choice (`add_sb_to_sb_cc_ap` vs `addSeqAccess` vs `addAP`).
- The **FSDP family discriminator**: `setCcTypeHint` (TP/FSDP) appears on exactly four methods; `addStreamId` on all seven group collectives.

| | |
|---|---|
| **Class** | `BirCodeGenLoop` (single public class) / base `BirCodeGenLoopGen` (generated) |
| **IR level** | Penguin tensoriser-IR collective `Op` **IN** → `birpy.Instruction` collective node **OUT** |
| **Direct-build template** | `codegenAllGatherOp` (idx 79, `0x155960`, `BirCodeGenLoop.py:1260`) |
| **BIR collective nodes** | `CollectiveCompute` IT48 · `CollectiveSend` IT49 · `CollectiveRecv` IT50 · `CoreBarrier` IT87 · `GetGlobalRankId` IT11 |
| **Kind enum** | `CollectiveKind` 0..10 (`setKind(inst.kind)`; numeric values owned by libBIR) |
| **`cc_dim` setter** | **none** — `CollectiveDimension` carried implicitly in the AP geometry |
| **Twin (klr/C++)** | libwalrus `KlirToBirCodegen` collective leaf — beta2/klr path, parallel, same BIR nodes |

---

## 1. Foundation: the collective node family and the two build idioms

### 1.1 Where this sits — beta3 ∥ beta2, both → BIR collective nodes

The collective codegens are the distributed-comm members of the `BirCodeGenLoop` per-instruction dispatch. Like the compute codegens, every method is `def codegenXxxOp(self, inst)`: `inst` is an **already-built Penguin collective op** (constructed upstream by `NeuronCodegen`, [§6.5.4](./neuroncodegen-collectives.md)), and the body reads attributes off it to build a `birpy.Instruction`.

```text
  L1  NeuronCodegen          : nl / nisa  → Penguin collective op (AllGatherOp, …)
                                            (stamps inst.kind, inst.op, replica_groups, …)
       │
       ├── L2  BirCodeGenLoop.codegen<Op>   : Penguin op → BIR  [THIS MODULE, beta3]
       │         via birpy.Instruction → InstCollectiveCompute(IT48)/Send(49)/Recv(50)/
       │         CoreBarrier(87)/GetGlobalRankId(11)
       │
       └── Lx  KlirToBirCodegen (C++)        : klr AST → BIR    [libwalrus, beta2]
                 via bir::InstBuilder
                                  ╲        ╱
                                   ╲      ╱
                            same  bir  collective  Inst  node family
```

> **NOTE —** the `CollectiveKind` numeric enum table (`SendRecv=0, SendRecvCCE=1, AllReduce=2, ReduceScatter=3, AllGather=4, AllToAll=5, AllToAllV=6, Permute=7, PermuteReduce=8, PermuteImplicit=9, PermuteReduceImplicit=10`) lives in the **BIR backend** (libBIR, plus the `bir_roundtrip` and `walrus_driver` images), **not** in `BirCodeGenLoop`. This module only writes `setKind(inst.kind)` — it never names the numeric value, and the kind-string in the emitted BIR JSON is produced downstream by libBIR's `CollectiveKind2string`. So the 0..10 values quoted on this page come from the BIR side; what is read *here* is the site that sets them.

### 1.2 The direct-build vs `super()`-override split

The single most important structural fact, recovered by counting `__pyx_n_s_addInstruction` vs `__pyx_builtin_super` in each method's disasm, is that the abstract collectives build the node **inline** while the Tiled/control variants **delegate** to the generated base:

```text
DIRECT BUILD  (addInstruction(<Inst>) in the impl, NO super() — impl is the primary builder):
  AllGatherOp · AllReduceOp · ReduceScatterOp · AlltoAllOp · CollectivePermuteOp ·
  CollectivePermuteReduceOp · BroadcastOp ·
  SendRecvOp · SendOp · RecvOp · CoreBarrierIntrinsic ·
  TiledCollectivePermuteReduceOp        ← the one Tiled collective with no Gen-base twin

SUPER() OVERRIDE  (BirCodeGenLoopGen base allocates the bir Inst; impl override configures):
  TiledAllReduceOp · TiledAllGatherOp · TiledReduceScatterOp · TiledAlltoAllOp ·
  TiledCollectivePermuteOp ·
  SendRecvCCEOp · CoreBarrierOp · GetGlobalRankId · BroadcastPartition
```

This split is observable per method: every DIRECT method has `addInstruction=1, super=0`; every OVERRIDE has `addInstruction=0, super=1`.

> **GOTCHA — `TiledCollectivePermuteReduceOp` looks like the other Tiled variants but is not one.** Idx 97 (`0x1d5ba0`, py:1444) has `super=0, addInstruction=1`: its body opens `addInstruction(CollectiveCompute(bb,id,debug_info))` → `setop`/`ALUOpcode` → `setReplicaGroups` → `setChannelId(channel_id)` → `setKind` → three `addAP(lhs/rhs/dst, isOutput=…)` tiles → `addStreamId`. It is a **direct build** that merely *uses* the per-tile `addAP` like its Tiled siblings — it has no `Gen`-base twin to delegate to.

### 1.3 The `BirCodeGenLoopGen` bases that the overrides call

The `super()` overrides delegate IT-node allocation to the generated base in `generated/BirCodeGenLoopGen.cpython-310…so`. Read from that module's disassembly, via the `addInstruction` argument in each base body:

| Override (impl) | GEN base idx | GEN base allocates |
|---|---|---|
| `codegenTiledAllReduceOp` (69) | `BirCodeGenLoopGen` #55 | `CollectiveCompute` (IT48) |
| `codegenTiledAllGatherOp` (75) | #59 | `CollectiveCompute` (IT48) |
| `codegenTiledReduceScatterOp` (81) | #63 | `CollectiveCompute` (IT48) |
| `codegenTiledAlltoAllOp` (83) | #57 | `CollectiveCompute` (IT48) |
| `codegenTiledCollectivePermuteOp` (93) | #61 | `CollectiveCompute` (IT48) |
| `codegenSendRecvCCEOp` (101) | #51 | `CollectiveCompute` (IT48) |
| `codegenCoreBarrierOp` (43) | #85 | `CoreBarrier` (IT87) |
| `codegenGetGlobalRankId` (47) | #7 | `GetGlobalRankId` (IT11) |
| `codegenBroadcastPartition` (165) | #45 | intra-core DVE inst (IT not pinned — see §7) |

> **NOTE —** the `Gen` base bodies do `addInstruction → CollectiveCompute → build_debuginfo → Opcode` and little else. The IT-field set and the `CollectiveCompute` ctor argument list live inside the `Gen` `.so` and were identified from the `addInstruction` argument alone, not byte-walked; the IT48/87/11 numbers themselves come from the BIR node-type catalog.

---

## 2. The canonical group-collective template

### Purpose

The seven non-tiled abstract group collectives (`AllGather`/`AllReduce`/`ReduceScatter`/`AlltoAll`/`CollectivePermute`/`CollectivePermuteReduce`/`Broadcast`) all build the IT48 `InstCollectiveCompute` directly and configure it with the same ordered sequence of setters. The only differences between them are (a) whether `setop` wraps `ALUOpcode`, (b) whether `setCcTypeHint` is set, (c) `setSplitCount` (AlltoAll only), and (d) the AP-builder choice. Everything else is shared.

### Entry Point

```text
dispatch_codegen (name-based: getattr(self, 'codegen'+type(op).__name__))
  └─ codegenAllGatherOp (idx 79, 0x155960)   ── the reference template, py:1260
       └─ self.addInstruction( CollectiveCompute(bb, id, build_debuginfo(inst)) )  → IT48
```

### Algorithm

Recovered from the `__pyx_n_s_` name-constant program order in `disasm/pyx_pw_*_79codegenAllGatherOp_0x155960.asm` (the constants appear in emit order; `mov rsi, cs:__pyx_n_s_<id>` per call). This is the template; per-method deltas follow in §2.1.

```c
function codegenAllGatherOp(self, inst):          // idx 79, 0x155960, BirCodeGenLoop.py:1260
    // ── 1. allocate the IT48 node directly (NO super()) ──
    cc = self.addInstruction(
            CollectiveCompute(inst.bb, inst.id, self.build_debuginfo(inst)) );   // InstCollectiveCompute (IT48)

    // ── 2. the reduce operator (bypass for AllGather; ALUOpcode-wrapped for reducers) ──
    cc.setop( inst.op );                          // AllGather: pass-through (NO ALUOpcode wrap)

    // ── 3. routing groups: vector<vector<u32>> read via the generic attr getter ──
    cc.setReplicaGroups( get_attr_default(inst, 'replica_groups') );   // n_u_replica_groups literal

    // ── 4. THE kind stamp: read straight off the Penguin op (set upstream by NeuronCodegen) ──
    cc.setKind( inst.kind );                       // CollectiveKind enum value; AllGather=4

    // ── 5. FSDP/TP parallelism hint — ONLY on AllGather & ReduceScatter (±Tiled) ──
    cc.setCcTypeHint( get_attr_default(inst, <tp|fsdp>) );   // n_u_tp / n_u_fsdp literals; two-site (default+explicit)

    // ── 6. pass-through scalars copied onto the bir inst ──
    cc.dma_qos             = inst.dma_qos;
    cc.permute_chain       = inst.permute_chain;   // per-step routing chain (Permute kinds consume it; wired even when empty)
    cc.unique_tensors_type = inst.unique_tensors_type;

    // ── 7. bind the comm/compute-overlap stream (TP=0 / FSDP=1) ──
    cc.addStreamId( <stream> );                    // the NeuronCollectiveStreamIdInjector realization

    // ── 8. operand/result access patterns; AllGather uses the dim-carrying SB↔SB CC AP ──
    for s in inst.operands:  cc.add_sb_to_sb_cc_ap(s); cc.addSeqAccess(s, isOutput=False);
    for d in inst.results :  cc.add_sb_to_sb_cc_ap(d); cc.addSeqAccess(d, isOutput=True );
```

### The shared fields, in detail

- **`setKind(inst.kind)` — the kind is read, not derived.** `__pyx_n_s_setKind` is immediately followed by `__pyx_n_s_kind` in every group-collective disasm. The `CollectiveKind` is an attribute of the Penguin op, stamped when `NeuronCodegen` built the `<Kind>Op` ([§6.5.4](./neuroncodegen-collectives.md)).

  > **GOTCHA — the kind is stamped at *different* layers on the two paths.** On the beta2/klr path it is written later, during `klr → BIR`. On the beta3 path it is written **here**, by `setKind(inst.kind)`, reading the value the Penguin op constructor already set. Both funnel into the same IT48 `"kind"` JSON field, so "where is the kind set" has two correct answers depending on the front end.

- **`setop` — the reducer/non-reducer split.** An `ALUOpcode` reference count over each method's disasm shows the three reducers wrap the op (`ALUOpcode` appears **2×** each — get + construct) while the four non-reducers do not (count **0**), yet all seven still call `setop`:

  | Method | idx | `ALUOpcode` count | `setop` |
  |---|---|---|---|
  | `codegenAllReduceOp` | 71 | **2** | `ALUOpcode(inst.op)` |
  | `codegenReduceScatterOp` | 85 | **2** | `ALUOpcode(inst.op)` |
  | `codegenCollectivePermuteReduceOp` | 95 | **2** | `ALUOpcode(inst.op)` |
  | `codegenAllGatherOp` | 79 | 0 | pass-through |
  | `codegenAlltoAllOp` | 89 | 0 | pass-through |
  | `codegenCollectivePermuteOp` | 91 | 0 | pass-through |
  | `codegenBroadcastOp` | 87 | 0 | pass-through |

  The wrapped value is the BIR reduce operator (`AluOpType` add=4 / max=8 / min=9 / mult=6 / average=24). The exact numeric per reducer is carried from `inst.op`, set upstream — [INFERRED] here, since this layer only re-wraps it.

- **`setReplicaGroups(get_attr_default(inst,'replica_groups'))`** — the `n_u_replica_groups` literal plus a `get_attr_default` call. The `vector<vector<u32>>` routing groups become the BIR `replica_groups` channel field.

- **`addStreamId` — universal**, present once on each of the seven group collectives. The chosen stream id binds the IT48 node to a comm stream — the per-collective realization of the `NeuronCollectiveStreamIdInjector`.

> **GOTCHA — there is no `cc_dim` / `setDim` / `setCcDim` setter anywhere in the beta3 collective codegens.** A full setter sweep across all 816 disasm files returns **zero** matches for any dimension setter. The `CollectiveDimension` (Partition/Free) is **not** marshalled as a top-level scalar at beta3; it is carried **implicitly in the access-pattern geometry** built by `add_sb_to_sb_cc_ap` (for AllGather/AllReduce/ReduceScatter) or `addSeqAccess` (for AlltoAll/Permute/Broadcast). This **diverges** from the klr leaf, which writes `cc_dim` explicitly at `InstCollectiveCompute+0x27C`. A reimplementer porting from the klr path must move the dimension into the AP, not a field. (The internal encoding inside `add_sb_to_sb_cc_ap` itself was not byte-walked — gap G1 in §9.)

### 2.1 Per-method specialization

Every group collective is the template above plus a small delta. py: lines from the `_Pyx_AddTraceback` first-line-number in each decompiled body.

| Method | idx · py | kind | `setop` | `setCcTypeHint` | AP builder | Extra / guard string |
|---|---|---|---|---|---|---|
| `codegenAllGatherOp` | 79 · 1260 | AllGather(4) | bypass | **SET** (FSDP/TP) | `add_sb_to_sb_cc_ap` + `addSeqAccess` | — |
| `codegenAllReduceOp` | 71 · 1183 | AllReduce(2) | `ALUOpcode` | — | `add_sb_to_sb_cc_ap` + `addSeqAccess` | reduce required |
| `codegenReduceScatterOp` | 85 · 1319 | ReduceScatter(3) | `ALUOpcode` | **SET** (FSDP/TP) | `add_sb_to_sb_cc_ap` + `addSeqAccess` | — |
| `codegenAlltoAllOp` | 89 · 1366 | AllToAll(5) | bypass | — | `addSeqAccess` | **`setSplitCount`**; `"Expect single alltoall buffer"` |
| `codegenCollectivePermuteOp` | 91 · 1389 | PermuteImplicit(9) | bypass | — | `addSeqAccess` | `"Expect single CollectivePermute"`; no channel |
| `codegenCollectivePermuteReduceOp` | 95 · 1423 | PermuteReduceImplicit(10) | `ALUOpcode` | — | `addSeqAccess` (×3) | `"Expect 2 input buffers and 1 out…"` (2-in/1-out) |
| `codegenBroadcastOp` | 87 · 1348 | `inst.kind` | bypass | — | `addSeqAccess` | `"Expect single broadcast buffer"` |

> **NOTE — the AP-builder choice *is* the implicit `cc_dim`.** A per-method count of `add_sb_to_sb_cc_ap` vs `addSeqAccess` shows the dim-carrying families (AllGather/AllReduce/ReduceScatter) call `add_sb_to_sb_cc_ap` **once** (which internally drives `addSeqAccess`, present 2×), while AlltoAll/Permute/Broadcast call `addSeqAccess` **directly** (no `add_sb_to_sb_cc_ap`). The presence of `add_sb_to_sb_cc_ap` is exactly the marker for "this collective's dimension is folded into the SB↔SB CC AP."

> **QUIRK — `setSplitCount` is the entire AlltoAll split/concat marshalling at this layer.** `__pyx_n_s_setSplitCount` appears **only** in `codegenAlltoAllOp` (plus the string-table init). It is a *single count*, not two separate dims; the concat-dimension resolution happens downstream in the AP, not here (gap G2).

> **NOTE on Broadcast (idx 87).** It builds an IT48 `CollectiveCompute` carrying `inst.kind` (`setKind`/`setop`/`setReplicaGroups`/`addSeqAccess`×2, guard `"Expect single broadcast buffer"`). The 0..10 `CollectiveKind` enum has **no dedicated "Broadcast" value**, so the cross-core broadcast is realized as a Permute/AllGather-class CC carrying the broadcast geometry (consistent with [§6.5.4](./neuroncodegen-collectives.md) `BroadcastOp`/`broadcast_sizes`). Which kind value `inst.kind` actually holds is [INFERRED] — there is no Broadcast enum slot to pin it to.

---

## 3. The CcTypeHint (TP/FSDP) discriminator

### Purpose

`setCcTypeHint` writes the `CollectiveComputeTypeHint` (TP(0)/FSDP(1)/None(2)) — the parallelism-strategy hint that distinguishes the two FSDP collective families (`fsdp_all_gather`, `fsdp_reduce_scatter`) for comm/compute overlap and stream selection.

### Where it appears

A full setter sweep (`rg -l '__pyx_n_s_setCcTypeHint'` over every `codegen*` disasm) returns exactly four methods:

```text
codegenAllGatherOp · codegenReduceScatterOp · codegenTiledAllGatherOp · codegenTiledReduceScatterOp
```

`AllReduce` / `AlltoAll` / `CollectivePermute(Reduce)` / `Broadcast` / `SendRecv(CCE)` do **not** set it — it defaults downstream (`CollectiveComputeTypeHint` ctor-default None(2), or TP via the stream injector).

### Algorithm

```c
// inside codegenAllGatherOp, between setKind and the dma_qos block:
hint = get_attr_default(inst, 'tp');              // n_u_tp literal — present ONLY in AllGather
if (hint is set):
    cc.setCcTypeHint( TP );                        // explicit branch (site 1)
else:
    fsdp = get_attr_default(inst, 'fsdp');         // n_u_fsdp literal
    cc.setCcTypeHint( FSDP or default );           // default branch (site 2)
// → then addStreamId binds stream "0"=TP / "1"=FSDP
```

The `n_u_tp` and `n_u_fsdp` string literals appear **only** in `codegenAllGatherOp`'s disasm, and `setCcTypeHint` appears **twice** in it — a default branch and an explicit branch — selecting TP(0) versus FSDP(1). The hint then splits the two FSDP families onto distinct streams via the immediately-following `addStreamId`.

---

## 4. The Tiled variants — `super()` override + per-tile AP

### Purpose

The Tiled collectives lower a *tiled* Penguin collective: the `BirCodeGenLoopGen` base allocates the IT48 node, and the impl override configures it and binds **per-tile** access patterns (`addAP`) instead of the single SB↔SB CC AP of the abstract form.

### Algorithm

```c
function codegenTiledAllGatherOp(self, inst, bb):     // idx 75, 0x98500, py:1219; super() override
    cc = super().codegenTiledAllGatherOp(inst, bb);    // BirCodeGenLoopGen #59 → CollectiveCompute (IT48)
    cc.setop( [ALUOpcode] inst.op );                   // ALUOpcode-wrapped only for reducers (TiledAllReduce/RS/CPR)
    cc.setReplicaGroups( inst.replica_groups );
    // setChannelId( inst.channel_id )   ← ONLY the Tiled permutes (idx 93)
    cc.setKind( inst.kind );
    cc.setCcTypeHint( ... );                           // ONLY TiledAllGather / TiledReduceScatter
    for tile in inst.operands: cc.addAP(tile, isOutput=False);   // ⭐ per-TILE AP, not add_sb_to_sb_cc_ap
    for tile in inst.results : cc.addAP(tile, isOutput=True );
    cc.addStreamId( ... );
```

| Method | idx · py | kind | reducer? | `setCcTypeHint` | `setChannelId` |
|---|---|---|---|---|---|
| `codegenTiledAllReduceOp` | 69 · 1171 | AllReduce(2) | yes (`ALUOpcode`) | — | — |
| `codegenTiledAllGatherOp` | 75 · 1219 | AllGather(4) | no | **SET** | — |
| `codegenTiledReduceScatterOp` | 81 · 1292 | ReduceScatter(3) | yes | **SET** | — |
| `codegenTiledAlltoAllOp` | 83 · 1308 | AllToAll(5) | no | — | — (`setSplitCount`) |
| `codegenTiledCollectivePermuteOp` | 93 · 1407 | Permute(7)/PermuteImplicit(9) | no | — | **SET** |
| `codegenTiledCollectivePermuteReduceOp` | 97 · 1444 | PermuteReduceImplicit(10) | yes | — | **SET** (direct build — see §1.2) |

> **QUIRK — `setChannelId(channel_id)` is the Tiled-permute multi-channel ring marker.** `__pyx_n_s_setChannelId` and `__pyx_n_s_channel_id` appear **only** on the two Tiled permutes (idx 93 and 97) and are **absent** from the non-tiled permutes (idx 91, 95). The channel id is the double-buffered ring channel (`CHANNEL_N` 1/2/4, see [§6.5.4](./neuroncodegen-collectives.md)). The non-tiled `CollectivePermute`/`CollectivePermuteReduce` carry no channel — they are single-channel implicit-routed.

The only structural difference between a Tiled variant and its abstract twin: Tiled uses **`addAP`** (the per-tile chunk access pattern) where the abstract form uses `add_sb_to_sb_cc_ap`/`addSeqAccess`, and Tiled permutes additionally carry `setChannelId`.

---

## 5. Point-to-point: SendRecv / Send / Recv

### `codegenSendRecvOp` (idx 99, `0xc8ec0`, py:1464) — fused local send+recv

A **direct build** (no `super()`). Emit order recovered from disasm:

```c
function codegenSendRecvOp(self, inst, bb):          // idx 99, 0xc8ec0, py:1464
    cc = self.addInstruction(CollectiveCompute(inst.bb, inst.id, debug_info));   // IT48
    cc.setReplicaGroups( ... );
    cc.setPipeId( inst.pipe_id );
    cc.setDynPipeId( inst.dynamic_pipe_id );
    cc.setRecvFromRank( inst.recv_from_rank, lnc_id );
    cc.setSendToRank( inst.send_to_rank, lnc_id );
    cc.setKind( ... );                                // SendRecv(0)
    cc.setLocal( ... );                               // is_local=true → BIR "LocalSendRecv"
    cc.setInitialCoreBarrier( inst.initial_corebarrier );    // the cross-core fence
    cc.dma_qos        = inst.dma_qos;
    cc.use_gpsimd_dma = inst.use_gpsimd_dma;          // ⭐ CAPTURED HERE (get+set, twice in body)
    cc.permute_chain  = inst.permute_chain;
    codegenNdDMAAP( cc, inst, strip_fp32r );          // the actual buffer transfer = a DMA access pattern
```

The identifier set backing this: `setRecvFromRank`, `setSendToRank`, `setKind`, `setLocal`, `setInitialCoreBarrier`, `setPipeId`/`setDynPipeId`, `codegenNdDMAAP`, and `__pyx_n_s_use_gpsimd_dma` appearing **twice** (a get and a set).

> **GOTCHA — the two front ends disagree on `use_gpsimd_dma`.** The klr→BIR leaf **hardcodes** `useGpsimdDma=false`, but this beta3 codegen carries the flag through onto the bir inst. So the same source-level kernel can reach BIR with a different `use_gpsimd_dma` depending on which front end compiled it. Note also that the local send/recv is realized as a DMA access pattern via `codegenNdDMAAP`, not as a pure CC descriptor — which is what makes the GPSIMDSB2SB-vs-DMACopy lowering choice downstream meaningful.

### `codegenSendOp` / `codegenRecvOp` (idx 103/105, py:1518/1529) — raw P2P

Both are direct builds of the **dedicated** send/recv nodes (not IT48):

```c
function codegenSendOp(self, inst, bb):    // idx 103, 0x14c7b0, py:1518
    si = self.addInstruction(CollectiveSend(inst.bb, inst.id, debug_info));   // IT49
    si.setPeerId( inst.peer_id );
    si.addSeqAccess( ... );                 // peer_id ONLY — no kind/op/cc_dim

function codegenRecvOp(self, inst, bb):    // idx 105, 0x153730, py:1529
    ri = self.addInstruction(CollectiveRecv(inst.bb, inst.id, debug_info));   // IT50
    ri.setPeerId( inst.peer_id );
    ri.addSeqAccess( ... );                 // peer_id ONLY
```

Both carry only `peer_id`, matching the BIR node catalog: IT49 and IT50 carry a peer id and nothing else.

---

## 6. `codegenSendRecvCCEOp` — the kind-1 multi-recv + reduce

### Purpose

`SendRecvCCE` is the collective-comm-engine fused **multi-receive-and-reduce**: one core receives from *several* peers and reduces them into a single destination, with a rendezvous barrier. It is the second origin (alongside the NKI front-end) of `CollectiveKind=SendRecvCCE(1)`.

### Entry Point

```text
codegenSendRecvCCEOp (idx 101, 0x129c40, py:1485)   ── super() override
  └─ super().codegenSendRecvCCEOp  ── BirCodeGenLoopGen #51 → CollectiveCompute (IT48)
  └─ 3 nested generators over the recv sources:
       _generator7  (0xebdf0) · _generator9 (0x51e70) · _genexpr12/_generator8 (0xec690)
```

### Algorithm

```c
function codegenSendRecvCCEOp(self, inst, bb):       // idx 101, super() override
    cc = super().codegenSendRecvCCEOp(inst, bb);      // GEN #51 → IT48 base
    cc.setReplicaGroups( ... );
    cc.setPipeId( inst.pipe_id );
    cc.setDynPipeId( inst.dynamic_pipe_id );
    cc.setRecvFromRank( inst.recv_from_rank, self, lnc_id );   // iterated over multiple peers
    cc.setSendToRank( inst.send_to_rank, lnc_id );
    cc.setop( ALUOpcode( inst.op.opcode ) );          // ⭐ the multi-recv REDUCE operator
    cc.setKind( ... );                                // SendRecvCCE(1)
    cc.setLocal( ... );
    cc.setInitialCoreBarrier( inst.initial_corebarrier );      // ⭐ initiate_barrier (rendezvous)
    cc.dma_qos = inst.dma_qos;
    // per-peer recv access patterns via the 3 generators (one recv-AP per peer):
    for r in <recv sources>:
        cc.addAP(r, isOutput=…);
        addComplicatedDMAAP(dst, isOutput=…);         // N strided DMA APs into one dst
    // guard: "Cannot legalize strided load/store!"
```

The evidence: the three generator/genexpr disasm bodies (`_generator7`, `_generator9`, `_genexpr12`/`_generator8`); `setRecvFromRank` + `setSendToRank`; `setop` + `ALUOpcode` (×2, the reduce); `setKind`; `setInitialCoreBarrier`; `addComplicatedDMAAP` (×2). The multi-peer reduce is realized as N strided DMA access patterns into one destination, through the complicated-DMA machinery.

> **GOTCHA — the attribute is singular `recv_from_rank`, even though the op is multi-peer.** The binary name constant is `__pyx_n_s_recv_from_rank`, with no trailing `s`, in the method and in all three generators. The plurality is real but lives in the **iteration**: the generators walk multiple recv sources through one singular-named attribute.

> **NOTE — this is the second origin of `CollectiveKind=1`.** There is no klr→BIR `SendRecvCCE` leaf at all; the libwalrus beta2 path has none. Both the NKI front-end ([§6.5.4](./neuroncodegen-collectives.md) `SendRecvCCEOp`) and this beta3 codegen mint kind 1. `setInitialCoreBarrier` is the `initiate_barrier` rendezvous.

---

## 7. SPMD control: CoreBarrier (IT87) · GetGlobalRankId (IT11) · BroadcastPartition (DVE)

### `codegenCoreBarrierOp` (idx 43, `0x58a70`, py:972) — `super()` override

```c
function codegenCoreBarrierOp(self, inst, bb):       // idx 43; super() override
    cb = super().codegenCoreBarrierOp(inst, bb);      // GEN #85 → InstCoreBarrier (IT87)
    for o in inst.operands:
        cb.addAP(o, isOutput=False);                  // ⭐ SBUF semaphore as input  ┐ RMW
        cb.addAP(o, isOutput=True );                  //    and as output            ┘ (two addAP)
    cb.setCores( get_attr_default(inst, 'cores') );   // active-core id list
    cb.setEngine( engineTrans(inst.engine) );         // codegenEngine
    cb.set_cb_id( inst.pipe_id );                     // the barrier id
```

The body carries `addAP` ×2 (the SBUF-semaphore read-modify-write), `setCores`, `setEngine` + `engineTrans`, and `set_cb_id` + `pipe_id`. This is the beta3 twin of the klr-side `codegenCoreBarrier → InstCoreBarrier(IT87)`. CoreBarrier is gen3+/Trn2 (LNC).

### `codegenCoreBarrierIntrinsic` (idx 53, `0xa3020`, py:1030) — direct build

```c
function codegenCoreBarrierIntrinsic(self, inst, bb): // idx 53; DIRECT build (NO super)
    cb = self.addInstruction(CoreBarrier(inst.bb, inst.id, debug_info));   // IT87 inline
    for o in inst.operands: cb.addSeqAccess(o, isOutput=False);            // addSeqAccess, not addAP
    cb.addSeqAccess(inst.dst, isOutput=True);
    cb.setCores( get_attr_default(inst, 'cores') );
    cb.set_cb_id( inst.pipe_id );                     // NO setEngine — engine defaulted
```

Same IT87 node as `codegenCoreBarrierOp`, but the intrinsic entry (the `nki.isa`-level `core_barrier`) builds it inline via `addSeqAccess` and does not set engine (`addInstruction(CoreBarrier)` present, `super=0`).

### `codegenGetGlobalRankId` (idx 47, `0x16cfe0`, py:1005) — `super()` override

```c
function codegenGetGlobalRankId(self, inst, bb):     // idx 47; super() override
    gr = super().codegenGetGlobalRankId(inst, bb);    // GEN #7 → InstGetGlobalRankId (IT11)
    gr.setWorldSize( inst.world_size );               // ⭐ world_size attr
    // dst register bind (uint32 named register "reg"):
    gr.addArgumentOrOutput(dst_register, result_index, isOutput=True);
    gr.addRegister( RegisterAccess(...), EngineType.ALL );
    gr.setEngine( EngineType.ALL );
```

The body carries `setWorldSize` + `world_size`, `addRegister` + `RegisterAccess`, `addArgumentOrOutput`, `EngineType.ALL` (×2), and the `n_u_int32` / `n_u_reg` literals (the uint32 named register). This is the beta3 twin of the klr-side `codegenRankId → InstGetGlobalRankId(IT11, opcode 220)`; the rank resolves at runtime as `core / numCoresPerLNC`. The in-group TP rank resolver (`GetCurProcessingRankID`, IT66) is **not** a `BirCodeGenLoop` method (nccl-side, [§6.5.4](./neuroncodegen-collectives.md)).

> **NOTE — convergence with the klr path.** Both `CoreBarrier`(IT87) and `GetGlobalRankId`(IT11) are emitted field-for-field identically by the beta3 (`BirCodeGenLoop`) and beta2 (libwalrus `codegenCoreBarrier`/`codegenRankId`) front-ends. The two front-ends **converge** on the same IT87/IT11 nodes — the same dual-frontend convergence the collectives show on IT48.

### `codegenBroadcastPartition` (idx 165, `0x11dd60`, py:2052) — intra-core DVE broadcast

```c
function codegenBroadcastPartition(self, inst, bb):  // idx 165; super() override (GEN #45)
    bp = super().codegenBroadcastPartition(inst, bb);
    if npartitions > target.dve_channels_per_bank:
        error("too large broadcast in <npartitions> vs <dve_channels_per_bank>");
    bp.setmask( ... );                                // the partition broadcast mask
    if src.dtype != dst.dtype: error("Unsupported cast");
    bp.addAP(inst.dst, isOutput=True);                // via NeuronAP, bounded by dve_channels_per_bank
```

The body carries `setmask`, `npartitions` (×3), `dve_channels_per_bank`, `addAP`, and the verbatim guard strings `"too large broadcast in"` / `"Unsupported cast"`. This is the **cross-partition (within-core, DVE)** broadcast — the intra-core analog of the cross-core `codegenBroadcastOp` (§2). It is a **local DVE op**, *not* an `InstCollectiveCompute`, and carries **no `CollectiveKind`**.

> **NOTE (gap G4) —** the exact BIR Inst type that `BirCodeGenLoopGen` #45 allocates for `BroadcastPartition` is not pinned to an IT number: the `Gen` base's `addInstruction` argument is not a named `n_s` constant (likely a numeric IT), so the DVE-op IT is left unconfirmed. It is definitively **not** IT48.

---

## 8. Cross-check against the klr/C++ twins and the enum catalog

| Subject | Beta2/klr (libwalrus) | Beta3 (this page) | Relationship |
|---|---|---|---|
| group-collective fields | `cc_kind`@+0xF8 · `reduce_op`@+0x180 · `replica_groups`@+0x100 | `setKind` · `setop(ALUOpcode)` · `setReplicaGroups` | same IT48 fields, different access |
| collective dimension | `cc_dim`@+0x27C (explicit field) | **none** — carried in the AP | **DIVERGENCE** (§2 GOTCHA) |
| SendRecvCCE | **no klr leaf** | `codegenSendRecvCCEOp` kind 1 | beta3 is the 2nd origin (§6) |
| `use_gpsimd_dma` | hardcoded `false` | `cc.use_gpsimd_dma = inst.…` | **DIVERGENCE** — beta3 preserves (§5) |
| barrier / rank | `codegenCoreBarrier`(IT87) / `codegenRankId`(IT11) | `codegenCoreBarrierOp` / `codegenGetGlobalRankId` | field-for-field CONVERGENCE (§7) |
| enum ownership | `CollectiveKind` 0..10 · `AluOpType` · `CollectiveComputeTypeHint` | `setKind`/`setop`/`setCcTypeHint` sites | beta3 stamp sites for enums libBIR owns |
| BIR JSON | IT48/49/50/87/11 keys | the fields set here | the JSON keys this page populates |

The fields this page writes — `kind` / `op` / `replica_groups` / `cc_type_hint` / `stream_id` / `permute_chain` / `channel_id` / `peer_id` / `cores` / `world_size` — are exactly the IT48/49/50/87/11 BIR-JSON keys.

---

## 9. Limits of this reading

Read directly off `BirCodeGenLoop.so`: the full method roster with indices, VAs and `BirCodeGenLoop.py` line numbers; the canonical group-collective emit order (from the `n_s` name-constant program order); every `setKind` / `setop` / `setReplicaGroups` / `setCcTypeHint` site; the reducer split (`ALUOpcode` count 2 vs 0); the total absence of any `cc_dim`/`setDim` setter across all 816 disassembled bodies; the direct-build vs `super()`-override split; `setChannelId` only on the Tiled permutes; `setSplitCount` only on AlltoAll; `setCcTypeHint` only on the four FSDP methods; `addStreamId` on all seven; the `SendRecvCCEOp` generators, reduce and rendezvous barrier; the `CoreBarrier`/`GetGlobalRankId` field binds; `SendOp`/`RecvOp` carrying only `peer_id`; the `BroadcastPartition` DVE guards; and every guard string verbatim.

Weaker, in decreasing order of confidence:

- **`BroadcastOp`'s actual kind value.** [INFERRED] It carries `inst.kind`, but there is no dedicated "Broadcast" slot in the 0..10 enum to pin it to.
- **The `CollectiveDimension → AP-geometry` encoding.** [INFERRED] There is no explicit setter; the mapping is deduced from which methods call `add_sb_to_sb_cc_ap`.
- **The `ALUOpcode` numeric per reducer.** [INFERRED] Carried in from `inst.op`, which is set upstream; this layer only re-wraps it.
- **The `Gen`-base `CollectiveCompute` ctor argument list.** Only the `addInstruction` and `Opcode` references were walked.

Values this module never names — the `CollectiveKind` 0..10 numerics, the IT48/49/50/87/11 node-type numbers, and the `AluOpType` / `CollectiveComputeTypeHint` values — all belong to libBIR. This module sets them by attribute.

**Open gaps.** **G1** — the internal body of `add_sb_to_sb_cc_ap` was not byte-walked, so the exact Partition/Free encoding into the SB↔SB AP remains its private logic. **G2** — `setSplitCount` sets a single count; the concat-dim resolution happens downstream. **G3** — the `BirCodeGenLoopGen` base constructors (and their IT-field set) live in the `Gen` `.so`, identified only by the `addInstruction` argument. **G4** — `BroadcastPartition`'s exact bir Inst type is not pinned to an IT number, only known not to be IT48.

---

## Related Components

| Name | Relationship |
|---|---|
| [§6.5.4 NeuronCodegen Collectives](./neuroncodegen-collectives.md) | the **forward** Penguin-op builder — stamps `inst.kind`/`replica_groups`/etc. that this page reads back |
| [§6.5.x BirCodeGen Compute Codegens](./bircodegen-compute.md) | the **compute** sibling subset of the same `BirCodeGenLoop` module |
| [§6.5.10 BirCodeGenLoop driver](./bircodegenloop.md) | `dispatch_codegen` name-based dispatch into these per-inst methods |
| libwalrus `KlirToBirCodegen` (Part 8) | the **parallel** beta2/klr C++ collective leaf — same BIR nodes, divergent `cc_dim`/`use_gpsimd_dma` |

## Cross-References

- [NeuronCodegen Collectives](./neuroncodegen-collectives.md) — §6.5.4, the upstream forward builder that mints the Penguin collective ops
- [BirCodeGen Compute Codegens](./bircodegen-compute.md) — the matmul/activation/tensor codegens of the same module; the dual-path beta3∥beta2 model
- [BirCodeGenLoop driver](./bircodegenloop.md) — the `dispatch_codegen` mechanism and the 3-layer KernelBuilder stack
