# AwsNeuronLNCShardingConstraint and the SPMD↔LNC Coupling

> *All addresses on this page apply to `neuronx-cc` 2.24.5133.0+58f8de22, the `hlo2penguin` front-end binary, cp310 build, unless tagged otherwise. The two-VA-frame convention holds: `.rodata` VA = file offset + 0x200000, `.text` VA = file offset + 0x201000 — VA is **not** the raw file offset. Other versions will differ. Evidence is static-binary-derived (`D-AB08`).*

## Abstract

neuronx-cc runs a **stock upstream-XLA SPMD partitioner** inside `hlo2penguin`. The driver, sharding propagation, and the compute handlers are all verbatim XLA — there is no Neuron subclass of `SpmdPartitioner`, no Neuron `ShardingPropagation`, no Neuron compute-handler set. Neuron customizes SPMD at exactly two seams, and this page documents the more interesting one: the **custom-call targets** that ride the stock partitioner. The companion seam — the front-end `--distribution-strategy` / `--spmd` / `--lnc` options that *seed* the mesh — is owned by [distribution-strategy-seeding](distribution-strategy-seeding.md); this page only touches it where the LNC count couples the two ends.

The namesake op is `AwsNeuronLNCShardingConstraint`: a side-effect-free HLO custom-call that **pins an `xla::HloSharding` onto an HLO value to bind it to the physical Logical NeuronCore (LNC) device mesh**. It is Neuron's analogue of XLA's stock `Sharding` custom-call / `sdy::ShardingConstraintOp`, but keyed to the physical LNC topology instead of an abstract logical mesh. The critical structural fact — verified by exhaustive negative xref — is that **it has no dedicated `CustomCallPartitioner` subclass**. It flows through the stock target-string-keyed `CustomCallPartitioner` registry in `xla/service/custom_call_sharding_helper.cc`, the same registry every other custom-call uses. This is "Neuron data riding stock machinery," not a Neuron-subclassed partitioner.

The page covers, in order: the LNC-constraint op and its penguin-IR printer (§1); the stock registry and the exact dispatch path that reshards an operand to the pinned sharding (§2); the sibling `AwsNeuronTransferWithStaticRing` collective-transfer custom-call and its parameter-load / rematerialization coupling (§3); and the front↔back contract — how the front-end `logical_nc_config` and the backend `lnc_size` are one quantity *N*, and how the LNC constraint makes the SPMD-tile↔physical-LNC-core mapping deterministic so the backend `lnc_splitter` produces SPMD-identical replicas (§4).

For reimplementation, the contract is:

- The op shape of `AwsNeuronLNCShardingConstraint`: an identity-on-data custom-call carrying `target_name=` + a `sharding=` `HloSharding` payload; emitted/validated/printed/cost-modeled only in the front-end.
- The stock `CustomCallPartitioner` registry mechanism: a process-global `flat_hash_map<target, CCP>` populated once via `absl::CallOnce` from `ShardingPropagation::Run`, consulted by target string at every SPMD touch-point.
- The dispatch-and-reshard path: how an unregistered (or sharding-constraint) target reaches `PartitionedHlo::Reshard` and forces the operand to the pinned sharding using stock collective machinery.
- The front↔back `logical_nc_config == lnc_size == N` contract and how the LNC constraint binds an SPMD tile to a specific LNC core.

| | |
|---|---|
| **Namesake op** | `AwsNeuronLNCShardingConstraint` (`.rodata` 0x360f50, len 30) — **hlo2penguin only** |
| **Penguin-IR printer** | `MhloToPythonPrinter::printLNCShardingConstraint` @0x20e37e0 (1281 B) |
| **Print dispatch** | `MhloToPythonPrinter::print<mhlo::CustomCallOp>` @0x20d1f10 (2762 B) |
| **Stock registry** | `xla::(anon)::GetPartitioners()` @0x2d24f30 — `flat_hash_map<string, CustomCallPartitioner>` |
| **Registry lookup** | `xla::GetCustomCallPartitioner(string const&)` @0x2d25140 (353 B) |
| **Registry insert** | `xla::RegisterCustomCallPartitioner(...)` @0x2d25830 (440 B) |
| **SPMD consumer** | `SpmdPartitioningVisitor::HandleCustomCall` @0x2c15e30 (4748 B) |
| **Reshard** | `PartitionedHlo::Reshard(HloSharding, optional<Literal>)` @0x2cad850 (2165 B) |
| **Registry source file** | `xla/service/custom_call_sharding_helper.cc` (string @0x3cf7d0) — **stock XLA** |
| **Static-ring transfer** | `AwsNeuronTransferWithStaticRing` (`.rodata` 0x2dfd28, len 31) — hlo2penguin **and** hlo-opt |
| **Mesh cardinality** | `logical_nc_config` (front) == `lnc_size` = `PassOptions+0x1A4` (back), default **1**, 2 on Trn2/`sunda` |

> **NOTE —** throughout, **stock-XLA** marks code that is verbatim upstream XLA (`xla::`, `xla::spmd::`, `xla::sdy::`), and **Neuron** marks Neuron-authored code (`xla::hilo::`, `xla::partition::`, `AwsNeuron*` strings, `PenguinizeFunctions`, `UnpackNestedAWSNTWSR`, the `mlir::*PythonPrinter` family). The whole point of the page is that the *mechanism* is stock and only the *data* is Neuron.

---

## 1. The LNC Sharding-Constraint Op

### Purpose

`AwsNeuronLNCShardingConstraint` is one of the real `AwsNeuron*` custom-call targets catalogued in the front-end's verifier whitelist. Semantically it is a **sharding-constraint op**: a no-op-on-data, side-effect-free custom-call whose output shape equals its input shape, existing solely to carry a required `HloSharding` into the SPMD partitioner. The sharding it carries is a tile assignment over the **LNC device mesh** — the *N* Logical-NeuronCore SPMD devices the module is being partitioned across. It is the one op that ties an abstract SPMD `HloSharding` to the *physical* LNC topology.

It is a **purely front-end construct**. The string `AwsNeuronLNCShardingConstraint` (`.rodata` 0x360f50, len 30) appears **only** in `hlo2penguin`; it is absent from the `hlo-opt` mid-level optimizer's string table, where the sibling `AwsNeuronTransferWithStaticRing` *is* present. The constraint is therefore consumed inside the HLO→penguin import path — which is where the stock SPMD partitioner runs — and stripped once partitioning has applied it, exactly as a sharding-constraint op should be.

> **QUIRK —** the op is "front-end only" not because Neuron hides it, but because it is a *transient annotation*. SPMD propagation reads it, reshards the operand to its sharding, and the op evaporates before the optimized HLO reaches `hlo-opt`. A reimplementer who looks for it in the mid-level IR will not find it; it lives only between import and the end of the front-end SPMD pass.

### Evidence It Has No Dedicated Pass

The string's complete referencing-function set (from the strings sidecar `referenced_by_functions`) is exactly four functions, and **none** is a rewrite/legalize/partition pass:

| Referencing function | Address | Role | Confidence |
|---|---|---|---|
| `xla::hilo::CustomCallOpChecker::CheckMisc` | 0x203e890 | validation whitelist | CERTAIN |
| `xla::hilo::NeuronHloCostAnalysis::HandleCustomCall` | 0x21b8b10 | cost model | CERTAIN |
| `mlir::MhloToPythonPrinter::print<mhlo::CustomCallOp>` | 0x20d1f10 | penguin-IR printer dispatch | CERTAIN |
| `mlir::StableHLOToPythonPrinter::print<stablehlo::CustomCallOp>` | (StableHLO twin) | penguin-IR printer dispatch | CERTAIN |

All four are import / validate / print / cost functions. There is **no** `xla::Legalize*` / `Partition*` / `Lower*` pass that special-cases this target string in either binary, and the op is given no `CustomCallPartitioner` subclass. That negative result — an exhaustive xref enumeration over both binaries — is what licenses the whole "rides stock machinery" thesis.

### The Penguin-IR Printer

The op's only dedicated emitter is the penguin-IR printer:

```text
MhloToPythonPrinter::print<mhlo::CustomCallOp>   @0x20d1f10 (2762 B)   ── dispatch on call-target name
  └─ std::function<void(Operation*)> thunk                            ── one per AwsNeuron* target
       └─ MhloToPythonPrinter::printLNCShardingConstraint  @0x20e37e0 ── the LNC-constraint emitter
  (StableHLO twin: StableHLOToPythonPrinter::print<...> ─→ printLNCShardingConstraint @0x217fb00)
```

> **QUIRK —** dispatch from `print<CustomCallOp>` to `printLNCShardingConstraint` is **not** a direct call. The printer template builds a table of `std::function<void(mlir::Operation*)>` thunks keyed by target name; the only caller of `printLNCShardingConstraint` in the binary is the `std::_Function_handler<...print<CustomCallOp>...>::_M_invoke` thunk wrapper, not the template body. A reimplementer chasing a direct callee edge from the template will miss it — the routing goes through a function-pointer table.

`printLNCShardingConstraint` @0x20e37e0 emits the penguin op form. Its three referenced string literals and five callees pin the exact emission:

```c
// MhloToPythonPrinter::printLNCShardingConstraint(mlir::Operation* op)   // @0x20e37e0, 1281 B
//
// Emits:   <dsts> = <ctx>.LNCShardingConstraintOp( <srcs>,
//                                                  target_name=<getCallTargetName()>,
//                                                  sharding=<the op's HloSharding> )
void printLNCShardingConstraint(Operation* op):
    dsts = printDsts(op)                       // @callee printDsts
    srcs = printSrcs(op, /*bitset<20>*/)       // @callee printSrcs
    emit(dsts, " = ", ctx, ".LNCShardingConstraintOp(")     // str ".LNCShardingConstraintOp(" @0x26a802
    emit(srcs)
    emit(", target_name=", op.getCallTargetName())          // str "target_name="  @0x27aecd
                                                            //  callee CustomCallOp::getCallTargetName()
    emit(", sharding=",  sharding_attr_of(op))              // str "sharding="     @0x23a742
    // backend_config payload also read:
    bc = op.getBackendConfig()                              // callee CustomCallOp::getBackendConfig()
    printMeta(op)                                            // @callee printMeta
    emit(")")
```

The three literals (`.LNCShardingConstraintOp(`, `target_name=`, `sharding=`) and the five callees (`getCallTargetName`, `getBackendConfig`, `printSrcs`, `printDsts`, `printMeta`) all appear in the function's `strings_referenced` and `callees`. The `sharding=` value is the SPMD `HloSharding` attached to the custom-call op — the constraint payload, i.e. the tile-over-LNC assignment. The output shape equals the input shape; the op is identity on data and exists only to carry the sharding requirement into SPMD.

### Validation and Cost

Two of the four referencing functions consume the op outside the printer:

- **`CustomCallOpChecker::CheckMisc` @0x203e890** holds a `std::_Hashtable<string>` whitelist of legal `AwsNeuron*` target names (including `AwsNeuronLNCShardingConstraint`) and rejects unknown targets via `hilo::formatErrorMessage(ErrorCode, ...)`; it logs the `"[ERROR] ["` prefix on failure. The front-end verifier therefore *accepts* the LNC constraint as a known op.
- **`NeuronHloCostAnalysis::HandleCustomCall` @0x21b8b10** routes the same target set through the Neuron cost model. Being an identity annotation, `AwsNeuronLNCShardingConstraint` contributes ~zero compute — it is a metadata op — unlike `AwsNeuronCollectiveMatmul`, which carries dot dimensions (lhs/rhs contracting + batch dims) read from `backend_config`.

---

## 2. The Stock CustomCallPartitioner Seam

### Purpose

Because the LNC constraint has no dedicated partitioner subclass, it must ride the **stock** `CustomCallPartitioner` registry. This section is the heart of the page: it shows the exact stock machinery that a Neuron custom-call target flows through, and how a sharding-constraint custom-call ends up reshard­ing its operand to the pinned sharding. Everything in this section is upstream XLA — the source-file string `xla/service/custom_call_sharding_helper.cc` (@0x3cf7d0) names it.

### The Registry

```c
// xla::(anon)::GetPartitioners()                          // @0x2d24f30, 143 B   (stock-XLA)
// Process-global, lazily-constructed, mutex-guarded:
absl::flat_hash_map<std::string, std::unique_ptr<CustomCallPartitioner>>&
GetPartitioners():
    static map  guarded by static absl::Mutex
    return map

// xla::RegisterCustomCallPartitioner(string_view target,                  // @0x2d25830, 440 B
//                                    unique_ptr<CustomCallPartitioner> p)
void RegisterCustomCallPartitioner(target, p):
    lock GetPartitioners() mutex
    if !GetPartitioners().insert({target, move(p)}):
        LOG "Failed to register custom call partitioner for "    // str present in fn
        //   source: "xla/service/custom_call_sharding_helper.cc"

// xla::GetCustomCallPartitioner(string const& target)         // @0x2d25140, 353 B
const CustomCallPartitioner* GetCustomCallPartitioner(target):
    lock GetPartitioners() mutex
    it = GetPartitioners().find(target)        // hash the string, mutex-locked find
    return it == end ? nullptr : it->second.get()
```

The registry is keyed by `custom_call_target`. It is populated **once** via `absl::CallOnce` — the only caller of `RegisterCustomCallPartitioner` @0x2d25830 is the `.constprop` `CallOnceImpl<...ShardingPropagation::Run...UlvE_>` thunk, so registration runs lazily from `ShardingPropagation::Run`.

### Who Consults the Registry

`GetCustomCallPartitioner` @0x2d25140 has exactly seven callers — the dispatch key (the target string) is consulted at *every* SPMD touch-point:

| Caller | SPMD phase | Confidence |
|---|---|---|
| `SpmdPartitioningVisitor::HandleCustomCall` | the actual partition | CERTAIN |
| `SpmdPartitioner::PreprocessSharding` | pre-partition sharding fixup | CERTAIN |
| `SpmdPartitioner::CanSideEffectingHaveReplicatedSharding` | side-effect validation | CERTAIN |
| `StatefulRngSpmdPartitioner::CanSideEffectingHaveReplicatedSharding` | side-effect validation (RNG flavor) | CERTAIN |
| `ShardingPropagation::InferShardingFromOperands` | forward propagation | CERTAIN |
| `ShardingPropagation::InferShardingFromUsers` | backward propagation | CERTAIN |
| `xla::(anon)::SupportSpatialPartitioning` | partitionability test | CERTAIN |

All seven are stock XLA; the list is the complete caller set of @0x2d25140.

### Dispatch and Reshard — the Algorithm

```c
// SpmdPartitioningVisitor::HandleCustomCall(HloInstruction* hlo)   // @0x2c15e30, 4748 B  (stock-XLA)
Status HandleCustomCall(hlo):
    target = hlo->custom_call_target()
    ccp = GetCustomCallPartitioner(target)              // @0x2d25140 — registry dispatch
    if ccp != nullptr:
        return ccp->Partition(this, hlo)                // bespoke partitioner, if one was registered

    // No registered partitioner -> fall to the stock pass-through / reshard paths:
    if is_elementwise_like(hlo):
        return HandleElementwise(hlo)                   // @callee HandleElementwise
    if forced_single_device(hlo):
        return HandleSingleDevice(hlo)                  // @callee HandleSingleDevice

    // A sharding-constraint custom-call resolves to: reshard the operand to the
    // requested HloSharding (the "sharding=" payload from §1):
    operand   = GetPartitionedHlo(hlo->operand(0))
    requested = hlo->sharding()                         // the pinned LNC tiling
    return operand.Reshard(requested, /*literal=*/nullopt)   // @0x2cad850
```

`HandleCustomCall` @0x2c15e30 calls **both** `GetCustomCallPartitioner` and `PartitionedHlo::Reshard` @0x2cad850, plus `HandleElementwise` / `HandleSingleDevice` as the pass-through paths. The base `CustomCallPartitioner::Partition` @0x2d250a0 is an unimplemented stub (it logs `"Implement sharding for %s"`), and the base `CanSideEffectingHaveReplicatedSharding` @0x2bfc590 is a 3-byte `return false` (`xor eax,eax; ret`). So an *unregistered* target falls to the default reshard/elementwise/replicate handling — which is precisely the reshard a sharding-constraint forces.

`Reshard` @0x2cad850 is the stock collective machinery: all-to-all / collective-permute / all-gather / dynamic-slice, selected per the stock `GetReshardAllToAllSourceTargetDims` / `CanReshardWithCollectivePermute` predicates (all present in `hlo2penguin`). The same function also handles the stock `SPMDFullToShardShape` / `SPMDShardToFullShape` custom-calls.

```text
                       Neuron data  ──────────────────────────────────►  stock-XLA machinery
  AwsNeuronLNCShardingConstraint
     (target string + sharding=)
            │
            ▼
   HandleCustomCall ── GetCustomCallPartitioner(target) ── registry miss (no Neuron subclass)
            │                                                        │
            │                                                        ▼
            └──────────────────────────────────────────────►  PartitionedHlo::Reshard(sharding)
                                                                     │
                                          all-to-all / collective-permute / all-gather / dynamic-slice
```

One question stays open. The registrant set of `GetPartitioners()` is populated indirectly, through the `CallOnce` from `ShardingPropagation::Run`, and that import is a skipped decompile — so the map's contents cannot be enumerated. No symbol literally named something like `LNCShardingConstraintPartitioner` exists, and the §1 negative xref rules out any Neuron pass touching the target string, which is what places the constraint on the stock path. Whether a *thin* Neuron `CustomCallPartitioner` is nevertheless registered for the target, or whether the target simply misses the map and falls to the default reshard, is not separable from the outside. The observable result is the same either way: the operand is resharded to the pinned LNC tiling.

> **NOTE —** the LNC constraint is side-effect-**free** (a pure annotation), so it is never gated by the side-effecting-sharding `RET_CHECK` path that fires only for side-effecting custom-calls. This is why it can be freely resharded and then stripped without a replication-legality check.

---

## 3. AwsNeuronTransferWithStaticRing

### Purpose

`AwsNeuronTransferWithStaticRing` (`.rodata` 0x2dfd28, len 31) is the Neuron collective-**transfer** custom-call: it moves a tensor between LNC cores along a **static ring** — a fixed, compile-time-determined ring of Logical NeuronCores — rather than a dynamically-routed collective. "Static" means the cross-core transfer path and order are baked at compile time (fixed by the LNC mesh), so the backend can emit a deterministic DMA schedule instead of a runtime-negotiated route. Unlike the LNC constraint, this op is present in **both** `hlo2penguin` and `hlo-opt` (count 1 in the optimizer's string table) — it survives into the mid-level optimizer because it is a real data-movement op, not a transient annotation.

### Who Touches It

The string's full referencing set (seven functions) splits into validate / cost / cleanup / partition-boundary / penguinize:

| Function | Address | Role | Confidence |
|---|---|---|---|
| `xla::UnpackNestedAWSNTWSR::Run` | 0x20537f0 | flatten illegally-nested transfers | CERTAIN |
| `xla::partition::isCustomCallLoadingParam` | 0x1f3d6b0 | recognize as parameter-load | CERTAIN |
| `xla::partition::Rematter::isCustomCallLoadingParam` | 0x1f16550 | recognize as parameter-load (remat) | CERTAIN |
| `xla::hilo::NeuronHloCostAnalysis::HandleCustomCall` | 0x21b8b10 | transfer cost | CERTAIN |
| `xla::hilo::CustomCallOpChecker::CheckMisc` | 0x203e890 | validation whitelist | CERTAIN |
| `PenguinizeFunctions::runOnOperation()::lambda#2` | 0x2087ee0 | MHLO→penguin matcher | CERTAIN |
| `StableHLOPenguinizeFunctions::...::lambda#2` | 0x2127230 | StableHLO→penguin matcher | CERTAIN |

### Flattening Nested Transfers

```c
// xla::UnpackNestedAWSNTWSR::Run(HloModule* m, flat_hash_set<string_view> exec)   // @0x20537f0
//   1897 B, 79 basic blocks, 68 callees.  ("AWSNTWSR" = AWS-Neuron-Transfer-With-Static-Ring)
Status UnpackNestedAWSNTWSR::Run(m, exec):
    RET_CHECK(m->entry_computation() != nullptr)                  // "nullptr != entry_computation_"
    for comp in m->computations():
        for inst in comp->MakeInstructionPostOrder():            // @callee MakeInstructionPostOrder
            if inst->custom_call_target() != "AwsNeuronTransferWithStaticRing":
                continue
            source = inst->mutable_operand(0)                    // @callee mutable_operand
            if source->custom_call_target() == "AwsNeuronTransferWithStaticRing":   // nested!
                NeuronLogger << "found illegally nested AwsNeuronTransferWithStaticRing, "
                                "replacing <tail> with <source>"  // str @0x354968, len 66
                comp->ReplaceInstruction(/*tail=*/inst, source)   // @callee HloComputation::ReplaceInstruction
    return OK
```

The pass collapses `transfer(transfer(x)) → transfer(x)`: two consecutive static-ring transfers on the same value are illegal (a value already in transit on the ring must not be re-wrapped), so the inner is removed and the outer's operand is rebound to the source, keeping the static-ring schedule single-hop. The function's callee set is exactly `MakeInstructionPostOrder`, `mutable_operand`, `ReplaceInstruction`, plus the `NeuronLogger` diagnostic chain (`getInstance` / `setSourceFile` / `setSourceLine` / `shouldLogToFile` / `shouldLogToConsole`).

### Static-Ring Transfer as a Parameter-Load

Both `isCustomCallLoadingParam` predicates are tiny (`@0x1f3d6b0`, 221 B; `@0x1f16550`, 237 B) and return true iff `custom_call_target() == "AwsNeuronTransferWithStaticRing"`. Their callers locate them in the Neuron **graph-partition** layer (`xla::partition` — the module/LNC splitter, distinct from `xla::spmd`):

```text
partition::isCustomCallLoadingParam @0x1f3d6b0
   ├─ partition::SplitFinder::findInitSplits    ── seeds the init-split set (input boundary)
   └─ partition::replicateTrivial               ── trivial replicate
Rematter::isCustomCallLoadingParam @0x1f16550
   └─ partition::Rematter::trivialRemats        ── trivial rematerialization
```

A static-ring transfer that brings a parameter onto a core is treated like a **parameter load**: (i) `SplitFinder::findInitSplits` seeds it as an *input boundary* of a partition — it is where data enters the per-core subgraph — and (ii) `Rematter::trivialRemats` marks it **trivially rematerializable**, so the loaded value can be re-fetched via the ring rather than spilled/recomputed, and `replicateTrivial` may replicate it cheaply. The caller set is pinned; the *why* is read off the caller names plus the LNC split model.

### Penguinization

`PenguinizeFunctions` (MHLO) @0x2087ee0 and `StableHLOPenguinizeFunctions` @0x2127230 each carry a per-`CustomCallOp` lambda that `memcmp`-matches the static-ring target name and lowers the op to the penguin transfer/collective representation. The target-name match is pinned, but the exact penguin op kind is not: the lambda body is a skipped-decompile export. That it is a transfer/collective op rather than a compute op rests on the cost model accounting its bytes-moved as a transfer cost.

---

## 4. The Front↔Back LNC Contract

### The LNC Count Is the Device-Mesh Cardinality

A single number — the **LNC count**, `logical_nc_config` = NeuronCores-per-Logical-NeuronCore — is what the SPMD mesh, the front-end sharding, and the backend split all key off:

- **Front-end** (`CompileCommand`, owned by [distribution-strategy-seeding](distribution-strategy-seeding.md)): `self.logical_nc_config` (`--lnc` / `--logical-nc-config`), default **2 on Trn2** (codename `sunda`), forced 1 on other archs (constraint string `args.arch != "sunda" or args.logical_nc_config == 1`). It gates `enable_internal_spmd_opt` and is surfaced to `hlo2penguin` as the `HloPassOptions` core-count knob; see the seeding page.
- **Backend** (`lnc_splitter`, [walrus/lnc-splitter](../walrus/lnc-splitter.md)): `lnc_size` = `PassOptions+0x1A4` (dword index 420), cached to `LncSplitter+0x60`, **default 1** — the per-module replication factor. `LncSplitter::run` @0x16d5ca0 (pipeline **order 8**) clones the one symbolic BIR module `lnc_size` times and concretizes per-core shard-ids; the cross-core collective lowering computes `getRemoteCores={0..lnc-1}\{self}` and `getAllCores={0..lnc-1}` from the same field.

> **GOTCHA —** the raw backend field `lnc_size` (`PassOptions+0x1A4`) defaults to **1**, not 2. The "2 on Trn2" is an *arch-conditioned front-end default* that plumbs down into that field; do not state "`lnc_size` defaults to 2." A single-core compile leaves the field at 1 and `lnc_splitter`'s `if (lnc != 1)` fast path skips the clone loop entirely.

Front-end `logical_nc_config` and backend `lnc_size` are the **same logical quantity** *N* (the LNC mesh size): the front-end shards an *N*-way mesh, the backend realizes it as *N* per-core BIR modules. Two independent sightings of *N* carry the same semantic, but the equality is not fully nailed down: the plumbing from the CLI attribute to `PassOptions+0x1A4` spans the driver→penguin boundary and is not traced through a single symbol.

### LNCShardingConstraint Is the Contract

```text
(1) front-end SPMD (stock XLA, inside hlo2penguin)
        propagates an HloSharding over the N-way LNC mesh;
        AwsNeuronLNCShardingConstraint PINS specific values to specific LNC tiles  (§1 "sharding=")
            │
(2) after SPMD partitioning, each HLO value carries its per-LNC tile assignment
        → these become BIR shard-id symbols (QuasiAffineExpr shard-id) in ONE symbolic bir::Module
            │
(3) ExpandReplication::run  @0x1553500  (order 7)  ── makes replication explicit on the symbolic module
        LncSplitter::run    @0x16d5ca0  (order 8)  ── clone ×lnc_size, concretize each clone's
                                                       shard-id to its core index, PRUNE off-core
                                                       (evalMask()==0) instructions
            │
(4) per-core BIR modules, SPMD-identical replicas (the LncVerifier invariant)
```

The sharding the front-end pins via `AwsNeuronLNCShardingConstraint` is exactly the shard-id assignment the backend splitter concretizes. The constraint is the contract that makes the SPMD-tile↔physical-LNC-core mapping deterministic, so `lnc_splitter`'s per-core prune produces SPMD-identical replicas. The ordering 7-before-8 is mandatory: replication is made explicit on the single module first; running it after the split would break SPMD identity.

### Static-Ring Transfer at the Split Boundary

`AwsNeuronTransferWithStaticRing` (§3) is the cross-LNC data movement the split materializes: `getRemoteCores` / `getAllCores` feed the cross-core DMA / remote-target lowering. Because `partition::isCustomCallLoadingParam` treats a static-ring transfer as a parameter load (§3), the splitter sees it as a per-core *input boundary* — where the ring delivers this core's shard — rather than recomputable compute, so each cloned core reads its slice off the static ring.

---

## Confidence Ledger

| Claim | Tag |
|---|---|
| `AwsNeuronLNCShardingConstraint` string is hlo2penguin-only (0 in hlo-opt); 4 referencing fns; no dedicated pass | CERTAIN (negative xref) |
| `printLNCShardingConstraint` @0x20e37e0 emits `.LNCShardingConstraintOp(` / `target_name=` / `sharding=` | CERTAIN |
| dispatch from `print<CustomCallOp>` to the printer is via a `std::function` thunk, not a direct call | CERTAIN |
| stock registry: `GetPartitioners` / `Register` / `GetCustomCallPartitioner`, registered once from `ShardingPropagation::Run` | CERTAIN |
| 7-caller consult list; `HandleCustomCall` @0x2c15e30 calls `GetCustomCallPartitioner` + `Reshard` | CERTAIN (call edges) |
| base `Partition` stub `"Implement sharding for %s"`; base `CanSideEffecting...` = 3-byte `return false` | CERTAIN |
| LNC constraint rides the stock reshard (no bespoke pass); reshard forces operand to pinned sharding | HIGH |
| `sharding=` payload is the constraint's tile-over-LNC `HloSharding` | HIGH |
| `UnpackNestedAWSNTWSR::Run` flattens nested transfers (`ReplaceInstruction(tail, source)`) | CERTAIN |
| `isCustomCallLoadingParam` (+ Rematter) gate on the static-ring target; callers = `findInitSplits`/`replicateTrivial`/`trivialRemats` | CERTAIN |
| static-ring lowers to a penguin transfer/collective op (penguinize lambda body skipped) | HIGH |
| LNC constraint pin == backend shard-id concretization (front↔back contract) | HIGH |
| front-end `logical_nc_config` == backend `lnc_size` as one *N* (CLI→`+0x1A4` not traced in one symbol) | MEDIUM–HIGH |
| no symbol literally named `LNCShardingConstraintPartitioner` in `GetPartitioners()`; registrant set not enumerable | NOT FOUND / OPEN |

---

## Related Components

| Component | Relationship |
|---|---|
| `SpmdPartitioner` driver | stock-XLA driver the LNC constraint rides; no Neuron subclass |
| `ShardingPropagation` | stock-XLA; registers the partitioner table once + consults it on infer-from-operands/users |
| `xla::sdy` mesh pipeline | stock-XLA Shardy mesh (`Sharding`→`ShardingConstraintOp`→`ReshardOp`) that the LLM-training seed sets up; the LNC constraint is the Neuron variant that additionally pins to a physical LNC |
| `lnc_splitter` (order 8) | backend pass that realizes the *N*-way LNC sharding as *N* per-core BIR modules |
| `ExpandReplication` (order 7) | backend pass that makes replication explicit before the split |

## Cross-References

- [SPMD Partitioner Driver](spmd-partitioner-driver.md) — the stock driver/visitor the custom-call targets flow through (13.1)
- [Distribution-Strategy Seeding](distribution-strategy-seeding.md) — the companion seam: `--distribution-strategy` / `--spmd` / `--lnc` that seed the mesh before SPMD (13.10)
- [Sharding Propagation](sharding-propagation.md) — registers and consults the `CustomCallPartitioner` table (13.2)
- [SPMD Compute Handlers](spmd-compute-handlers.md) — the stock per-op handlers `HandleCustomCall` shares the visitor with (13.5)
- [Shardy ↔ HloSharding Bridge](shardy-hlosharding-bridge.md) — the sdy mesh / `HloSharding` conversion that produces the sharding pinned here (13.4)
- [LNC Splitter](../walrus/lnc-splitter.md) — backend `lnc_size` clone-×N + concretize-shard-id that realizes the front-end pin (Part 8)
- [LNC Memory Model](../arch/lnc-memory-model.md) — what a Logical NeuronCore is; `lnc_size` = `PassOptions+0x1A4`, default 1 / 2 on Trn2 (1.07)
