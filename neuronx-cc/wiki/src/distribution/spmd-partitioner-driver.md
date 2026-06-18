# The SpmdPartitioner Driver and Options

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, binary `neuronxcc/starfish/bin/hlo-opt` (cp310 build). Other wheels differ; treat every address as version-pinned. Provenance: D-AB01.*

## Abstract

When a model is sharded across multiple NeuronCores, the HLO module that reaches `hlo-opt` is **annotated** — every instruction carries an `HloSharding` — but not yet **rewritten**. The pass that performs the rewrite, turning a single global program into per-partition local computations and inserting the collectives that stitch them back together, is `xla::spmd::SpmdPartitioner`. This page documents its *driver*: the top-level `SpmdPartitioner::Run` flow, the `SpmdPartitionerOptions` POD it consumes, the pipeline seat where it is constructed and run, and the hard entry/exit invariants it enforces. The per-operator rewrite logic (the `SpmdPartitioningVisitor` compute handlers) and the collective-emit handlers are owned by [13.5](spmd-compute-handlers.md)/[13.6](spmd-collective-emission.md); sharding propagation by [13.2](sharding-propagation.md)/[13.3](sharding-algebra.md).

The single most important provenance fact: **this driver is unmodified upstream XLA**. The namespace is `xla::spmd`, the source path string `xla/service/spmd/spmd_partitioner.cc` is embedded verbatim (`@0x37d1c8`), and the pass is constructed and run by the **stock** `xla::cpu::CpuCompiler` pipeline that Neuron reuses as its HLO front-end. Neuron does *not* fork `SpmdPartitioner::Run`. Its only contribution to the driver is the **seat and the wiring**: a config gate that turns the SPMD block on, the `num_partitions`/`replica_count` and sharding vectors it feeds in, and a family of `xla::hilo` collective passes that run *after* the partitioner to lower the emitted collectives for the Neuron device ([13.6](spmd-collective-emission.md), [13.9](lnc-sharding-constraint.md)). Everything between those two seams is XLA.

Read against the LLVM frame, `SpmdPartitioner` is a single `HloModulePass` (the analogue of an LLVM `ModulePass`) registered into a `HloPassPipeline` (an LLVM `PassManager`). The interesting structure is internal: `Run` is a six-phase sequence — preprocess, log, call-graph-prep, partition, relayout-and-verify, post — and the relayout/verify phase exists to enforce an invariant that distinguishes SPMD from ordinary HLO rewriting: **the global entry signature must survive partitioning byte-for-byte**, even though every internal computation is rewritten to local per-partition shapes.

For reimplementation, the contract is:

- The exact ordered call trace of `SpmdPartitioner::Run`, with the six phases and where each callee fires.
- The `SpmdPartitionerOptions` POD layout — the confirmed offsets, the windowed-einsum threshold gate, and the default values.
- The pipeline seat: `RunHloPassesThroughLayoutAssn` constructs `ShardingPropagation` (is_spmd=true) immediately followed by `SpmdPartitioner`, gated by `config.use_spmd_partitioning()`.
- The entry/exit invariants: every instruction pre-sharded on input; the global parameter/result signature preserved on output, enforced by three `RET_CHECK`s.

| | |
|---|---|
| **Driver entry** | `SpmdPartitioner::Run` @ `0x2ab76d0` (6349 B, 319 BB, 129 callees) |
| **Provenance** | Stock upstream XLA — `xla/service/spmd/spmd_partitioner.cc` (`@0x37d1c8`) |
| **Pass name** | `"spmd-partitioning"` (string `@0x2443fe`) |
| **Pipeline seat** | `xla::cpu::CpuCompiler::RunHloPassesThroughLayoutAssn` @ `0x2046400` |
| **Core callee** | `SpmdPartitioner::PartitionComputation` @ `0x2a944e0` |
| **Options POD** | `SpmdPartitionerOptions` default ctor @ `0x2038870` (181 B) |
| **Concrete pass** | `StatefulRngSpmdPartitioner` (RTTI `@0x483ca8`) — overrides `CreateVisitor` |
| **IR level** | HLO (`HloModule`), pre-layout-assignment |

---

## SpmdPartitioner::Run — the top-level driver

### Purpose

`Run` takes a fully-sharded `HloModule` and rewrites every computation into per-partition shapes, inserting collectives so the partitions cooperate. It is the `HloPassInterface::Run` entry point — it has no direct callers (CONFIRMED: `is_entry`), so it is reached only through the pass vtable by `HloPassPipeline::Run`. The demangled signature is the pass-interface one:

```c
// _ZN3xla4spmd15SpmdPartitioner3RunE... @ 0x2ab76d0
absl::StatusOr<bool> SpmdPartitioner::Run(
    HloModule* module,
    const absl::flat_hash_set<std::string_view>& execution_threads);
```

The `StatusOr<bool>` return packs a `StatusOrData<bool>` (callee `absl::...StatusOrData<bool>::StatusOrData @0x1ead0f0`); the `bool` is the `changed` flag every HLO pass returns. The `execution_threads` set scopes which computations participate (async/threaded computations are partitioned independently).

### Entry Point

```text
HloPassPipeline::Run                              ── stock XLA pass manager
  └─ (vtable dispatch on HloPassInterface)
      └─ SpmdPartitioner::Run (0x2ab76d0, 6349 B) ── THIS DRIVER
          ├─ PreprocessSharding   (0x2ab0560)     ── canonicalize + validate shardings
          ├─ PreprocessHlos       (0x2ab5c30)     ── rewrite HLOs partition-friendly
          ├─ RecordInputsOutputsSharding (0x2ab2dc0) ── snapshot io shardings
          ├─ FlattenCallGraph::Run                ── single-caller-context per computation
          ├─ CallGraph::Build / IsFlattened       ── RET_CHECK flattened
          ├─ PartitionComputation (0x2a944e0)     ── *** CORE: drives the visitor ***
          └─ (relayout + verify + set_config + post-log)
```

### Algorithm

The call trace below is reconstructed from the disassembly of `Run` in address order (cold/cleanup/dtor calls elided). Every call site address is CONFIRMED from the disasm of `0x2ab76d0`; the six phase labels (A–F) are INFERRED by matching the call sequence and the `RET_CHECK` strings to the embedded `spmd_partitioner.cc` source semantics.

```c
function SpmdPartitioner_Run(module, execution_threads):     // 0x2ab76d0
    changed = false

    // ---- PHASE A: PREPROCESS ----------------------------------------
    PreprocessSharding(module, execution_threads)            // 0x2ab0560 (see §entry-contract)
        // canonicalizes shardings, validates every instruction has one,
        // gates side-effecting ops. Detail owned by 13.4/13.5.
    PreprocessHlos(module, execution_threads)                // 0x2ab5c30 @ call 0x2ab77c2
        // rewrites individual HLOs into partition-friendly forms
        // (sink/replace, pad/slice canonicalization). 223 BB — the bulk
        // of pre-visitor HLO massaging.

    // ---- PHASE B: LOGGING / BOOKKEEPING -----------------------------
    report = SpmdLogger::ReportBeforePartition(*module, lvl) // 0x2ab7800
    LogLines(severity, report, file, line)                   // 0x2ab7820  dump
    RecordInputsOutputsSharding(module)                      // 0x2ab2dc0 @ 0x2ab7830
        // snapshots entry param + root output shardings into
        // spmd_parameters_sharding / spmd_output_sharding records.

    // ---- PHASE C: CALL-GRAPH PREP -----------------------------------
    FlattenCallGraph::Run(module, execution_threads)         // @ 0x2ab7884
        // clones called computations so each has ONE caller context —
        // required so per-call shardings are unambiguous.
    saved_prog_shape = entry->ComputeProgramShape(ids=b)     // 0x2ab7918  SAVED
    next_channel_id  = hlo_query::NextChannelId(*module)     // 0x2ab7920  seed counter
    root_sharding    = entry->root_instruction()->sharding() // 0x2ab7938
    root_sharding    = HloSharding(copy of root_sharding)    // 0x2ab7947
    call_graph = CallGraph::Build(module, execution_threads) // 0x2ab7989
    RET_CHECK(call_graph.IsFlattened())                      // 0x2ab79a5

    // ---- PHASE D: PARTITION (core) ----------------------------------
    PartitionComputation(entry, root_sharding,               // 0x2a944e0 @ 0x2ab79e9
                         &next_channel_id, &logger, call_graph)
        // *** drives SpmdPartitioningVisitor over the entry computation,
        //     recursively over callees via call_graph. Returns the
        //     rewritten (sharded) computation. Owned by 13.2/13.3.

    // ---- PHASE E: RELAYOUT + VERIFY (entry-signature contract) ------
    new_prog_shape = entry->ComputeProgramShape(b)           // 0x2ab7a26  NEW
    LayoutUtil::CopyLayoutBetweenShapes(saved, &new)         // 0x2ab7ada  params
    ShapeUtil::ForEachMutableSubshape(...)                   // 0x2ab7b35  relayout walk #1
    for each i in parameters:
        RET_CHECK( saved.param(i).Equal(new.param(i))        // 0x2ab7c1a
                       .MinorToMajorOnlyInLayout() )
            // FATAL "Parameter shape changed for the entry computation" @0x2ad7a8
    RET_CHECK( saved.result().Equal(new.result()) )          // 0x2ab7ce3
        // FATAL "Result shape changed for the entry computation" @0x297528
    // (param COUNT check FATAL "Parameter count changed..." @0x313020)
    LayoutUtil::CopyLayoutBetweenShapes(...)                 // 0x2ab7f0e  result
    ShapeUtil::ForEachMutableSubshape(...)                   // 0x2ab7f62  relayout walk #2
    cfg = HloModuleConfig(copy of module->config())          // 0x2ab7f90
    cfg.entry_layout = ComputationLayout(new_prog_shape,     // 0x2ab7fa1  ignore_layouts=b
                                         ignore_layouts)
    module->set_config(cfg)                                  // 0x2ab80ef

    // ---- PHASE F: POST ----------------------------------------------
    report = SpmdLogger::ReportAfterPartition(*module, lvl)  // 0x2ab812d
    LogLines(...)                                            // 0x2ab814d  dump
    for comp in module->computations(execution_threads):     // 0x2ab81aa
        cleanup_callback(comp)                               // 0x2ab8211 / 0x2ab828e (call rax)
            // per-computation final cleanup functor — in stock XLA the
            // dead-code / control-dependence cleanup + verify.
    return StatusOr<bool>(changed)
```

> **NOTE — `PreprocessSharding` runs first, even though `PreprocessHlos` is the first captured *direct* call.** In stock XLA the order is `PreprocessSharding` then `PreprocessHlos`. In the `hlo-opt` disassembly, `PreprocessSharding` (`0x2ab0560`) is reached through an early/inlined path while `PreprocessHlos` (`0x2ab5c30`) is the first call captured at `0x2ab77c2`. Both fire before the logger; the validation in `PreprocessSharding` is what the entry contract (below) rests on.

> **QUIRK — the relayout phase is not cosmetic.** Partitioning rewrites internal shapes to local per-partition sizes, which perturbs the layout bookkeeping on the *recomputed* entry program shape. Phase E re-copies the **original** layouts back onto the new shapes (`CopyLayoutBetweenShapes` + two `ForEachMutableSubshape` walks, one for params, one for the result) *before* the equality `RET_CHECK`s — otherwise an honest reimplementation would spuriously fail the shape check on a pure layout delta. The comparison is `Shape::Equal().MinorToMajorOnlyInLayout()`, i.e. dimensions-plus-minor-to-major, not full layout.

### Function Map

| Function | Addr | Size | Role | Confidence |
|---|---|---|---|---|
| `SpmdPartitioner::Run` | `0x2ab76d0` | 6349 B | Top-level driver (this page) | CERTAIN |
| `SpmdPartitioner::PreprocessSharding` | `0x2ab0560` | — | Canonicalize + validate shardings | CERTAIN |
| `SpmdPartitioner::PreprocessHlos` | `0x2ab5c30` | 5620 B | Rewrite HLOs partition-friendly (223 BB) | CERTAIN |
| `SpmdPartitioner::RecordInputsOutputsSharding` | `0x2ab2dc0` | 1191 B | Snapshot entry io shardings | CERTAIN |
| `SpmdPartitioner::PartitionComputation` | `0x2a944e0` | — | Core: drives the visitor (4 callers) | CERTAIN |
| `CanSideEffectingHaveReplicatedSharding` | `0x2a8e260` | 127 B | Side-effect-op sharding gate | CERTAIN |
| `CreateVisitor` (base) | `0x2aa4ff0` | — | Virtual visitor factory | HIGH |
| `StatefulRngSpmdPartitioner::CreateVisitor` | `0x2a10a20` | — | RNG-aware visitor override | CERTAIN |
| `SpmdPartitioningVisitor::DoPartition` | `0x2ad06b0` | — | Per-op partition (owned by 13.2) | HIGH |

---

## SpmdPartitioner construction and object layout

### Purpose

`SpmdPartitioner` is a constructible `HloModulePass`. The pipeline builds it from `(num_partitions, num_replicas, options)`. The ctor stores those scalars and **moves** the options POD into the object, then builds a default collective-ops creator (the all-reduce / all-gather / all-to-all / collective-permute emitter set keyed by `num_partitions × num_replicas`).

```c
// _ZN3xla4spmd15SpmdPartitionerC2EllNS0_22SpmdPartitionerOptionsE @ 0x2a93280
SpmdPartitioner::SpmdPartitioner(long num_partitions, long num_replicas,
                                 SpmdPartitionerOptions options);   // options BY VALUE
```

ABI: `rdi=this, rsi=num_partitions, rdx=num_replicas, rcx=&options`. Options is passed by value as a caller-constructed temporary and **moved-from** here — the ctor steals its container members (`[opt+0x28]..[opt+0x50]`) and zeroes them.

### Object field map

From the ctor store offsets (`this=rbx`):

| Offset | Field | Source | Confidence |
|---|---|---|---|
| `+0x00` | vtable ptr (`off_485360`) | — | CERTAIN |
| `+0x08` | `num_partitions_` (long) | `rsi` arg | CERTAIN |
| `+0x10` | `num_replicas_` (long) | `rdx` arg | CERTAIN |
| `+0x18` | `options_` POD prefix begins (bool@+0) | moved from `[opt+0]` | CERTAIN |
| `+0x20`…`+0x70` | options scalars + first SSO string block | moved | HIGH |
| `+0x80`…`+0x160` | ~8 string/SSO blocks (std::string move idiom) | moved | HIGH |
| `+0x198` | `SPMDCollectiveOpsCreator` vtable/sentinel (`xmmword_D7D8D0`) | built in ctor | HIGH |
| `+0x1A0`…`+0x1B0` | collective-ops-creator inline state | zeroed | HIGH |

> **NOTE — the collective-ops creator is stock.** The ctor first calls `GetDefaultCollectiveOpsCreator(num_partitions, num_replicas)` (`@0x2a932b6`), stores it into the object tail, and frees the temp (`~SPMDCollectiveOpsCreator @0x2a936ac`). A `SpmdPartitioner` built this way uses the **stock** XLA collective emitters. Neuron does NOT swap this at the driver ctor; any Neuron-specific collective lowering happens later in the `xla::hilo` collective passes ([13.6](spmd-collective-emission.md)), not in the creator here.

### The concrete pass

`SpmdPartitioner` is the base; `CreateVisitor` is virtual (called via vtable slot `[visitor_factory+0x428]` inside `PartitionComputation @0x2a9470e`). The pass XLA actually registers is the subclass:

- **`StatefulRngSpmdPartitioner`** (RTTI `@0x483ca8`) overrides `CreateVisitor` (`@0x2a10a20`) to build a `StatefulRngSpmdPartitioningVisitor` that handles `HandleRngGetAndUpdateState` (`@0x2a11160`) — so RNG state is partitioned deterministically. (CONFIRMED RTTI; that this is the *registered* one is STRONG.)
- **`ShardBarrierPartitioner` / `ShardBarrierFromPartitioner` / `ShardBarrierToPartitioner`** (RTTI `@0x486928` / `@0x486970` / `@0x4869c8`) — auxiliary partitioners for the `SPMDFullToShardShape` / `SPMDShardToFullShape` shard-barrier custom-call boundaries.

---

## SpmdPartitionerOptions — field map and defaults

### Purpose

`SpmdPartitionerOptions` is a stock upstream XLA POD (`xla/service/spmd/spmd_partitioner.h`) carrying the knobs that tune partitioning — chiefly the windowed-einsum (collective-matmul) heuristics and the conv halo-exchange policy. The binary gives the **exact default-ctor stores**; the field *names* are the canonical upstream names, matched by type/default/use (STRONG).

### Default-ctor stores

From `SpmdPartitionerOptions::SpmdPartitionerOptions() @0x2038870` (181 B, `this=rdi`):

| Offset | Default written | Field (upstream name) | Confidence |
|---|---|---|---|
| `+0x00` | `1` (byte) | `conv_halo_exchange_always_on_lhs` = **true** | CONFIRMED |
| `+0x08` | `5` (qword) | report-level / count limit | HIGH (offset INFERRED) |
| `+0x10` | `0x100` (256) | a windowed-einsum chunk param | HIGH (offset INFERRED) |
| `+0x18` | `0x0100000001000000` | packed two int32 (=`0x01000000` each) / flags | INFERRED |
| `+0x20` | `1` (word) | `bidirectional_windowed_einsum` = **true** | CONFIRMED |
| `+0x22` | `0` (byte) | a bool flag | CONFIRMED |
| `+0x28` | `new(8)`+empty | std::vector / InlinedVector (id/skip list) | CONFIRMED |
| `+0x40` | `new(8)`+empty | second container | CONFIRMED |
| `+0x60` | `0` (byte) | windowed-einsum (a2a) enable = **false** | CONFIRMED |

Container members occupy `0x28..0x60`. Past the POD prefix the struct continues with std::string / std::vector members (copied by the `SpmdPartitioner` ctor up to `~0x190` in the object), consistent with options holding several string members (dump tags) and vectors.

### The windowed-einsum threshold gate

The one consumer whose offsets are CONFIRMED is the threshold gate, from `should_enable_windowed_einsum_with_threshold(const SpmdPartitionerOptions&, const HloInstruction*, const HloInstruction*, long) @0x2a29c00`:

```c
// 0x2a29c00 — disasm
if ((*(byte*)(opt + 0x60)) == 0)        return false;  // !enable_windowed_einsum
if (operand_size <= *(long*)(opt + 0x58)) ...          // compare against threshold (long)
// log "Overhead outweighs benefit. Skipping windowed einsum" when below.
```

This pins two fields the **default** ctor leaves unset (they are written by the parameterized ctor, see the pipeline seat below):

| Offset | Field | Confidence |
|---|---|---|
| `+0x58` | `int64` windowed-einsum size threshold (`operand_bytes_threshold`) | CONFIRMED |
| `+0x60` | `bool` windowed-einsum enable | CONFIRMED |

### Related Knobs

The XLA debug-flag strings that *populate* these options are embedded verbatim in `hlo-opt`. They are stock XLA `xla_gpu_*` names; on Neuron the same options are reached through the Neuron HLO-pass config, not GPU flags.

| Knob (flag string) | Type | Default | Description |
|---|---|---|---|
| `xla_gpu_threshold_for_windowed_einsum_mib` | int64 | **100000** | Enable windowed einsum (collective matmul) when a partitioned operand exceeds this MiB |
| `xla_gpu_operand_bytes_threshold_for_windowed_einsum` | int64 | **-1** | If ≥0, used instead (sum of 2 operand sizes); overrides the MiB threshold |
| `xla_gpu_multi_streamed_windowed_einsum` | bool | — | Use multiple compute streams |
| `xla_gpu_experimental_enable_alltoall_windowed_einsum` | bool | — | Windowed-einsum rewrite for all-to-all+gemm (experimental) |
| `skip_checking_windowed_einsum_users` | bool | — | Skip the windowed-einsum user check |

> **GOTCHA — the default ctor and the pipeline-patched ctor disagree on the threshold.** The default ctor (`0x2038870`) leaves `+0x58` unset and `+0x60`=false. The pipeline that actually instantiates the partitioner *patches* the threshold to `0x186A0` (=100000) right before constructing it (`@0x2046809`, see below). A reimplementer who reads only the default ctor will see a disabled, unset windowed-einsum threshold and conclude the feature is off — it is the **pipeline** that turns it on. The middle-field offset assignment (`+0x08`/`+0x10`/`+0x18`) is INFERRED; the `+0x00`/`+0x20`/`+0x58`/`+0x60` offsets and the `5`/`256`/packed-int defaults are CONFIRMED.

---

## The pipeline seat

### Purpose

`SpmdPartitioner` is never invoked alone; it is one pass in the pre-layout-assignment `HloPassPipeline` that the **stock** `xla::cpu::CpuCompiler` assembles and Neuron reuses as its HLO front-end. The assembler is `RunHloPassesThroughLayoutAssn`.

```c
// xla::cpu::CpuCompiler::RunHloPassesThroughLayoutAssn @ 0x2046400
absl::Status CpuCompiler::RunHloPassesThroughLayoutAssn(
    HloModule* module, bool /*is_aot*/, TargetMachineFeatures* features);
// 201 BB, 41 callees — builds the pre-layout-assignment pass pipeline.
```

> **NOTE — the assembler is `xla::cpu::CpuCompiler`, i.e. stock XLA.** `RunHloPassesThroughLayoutAssn @0x2046400` is the XLA **CPU** compiler's pipeline method, not a Neuron-namespaced one (CONFIRMED by demangle). This is the clearest single piece of evidence that the SPMD seat itself is unmodified XLA: Neuron reuses the CPU compiler's HLO pipeline wholesale and feeds it a sharded module.

### The ordered construction

From the disassembly of `0x2046400`, in address order (CONFIRMED):

```c
function RunHloPassesThroughLayoutAssn(module, is_aot, features):   // 0x2046400
    pipeline = HloPassPipeline(name, stats)              // 0x20464ab
    AddHloVerifier(pipeline, opts, ...)                  // 0x2046528  verifier first
    ...
    if config.use_spmd_partitioning():                   // var_2B8 gate (jnz skip @0x2046796)
        sp = ShardingPropagation(                        // 0x2046764
                 is_spmd = 1,                            //   <-- SPMD propagation
                 ..., allow_module_signature_change,
                 unique_ptr<CustomCallShardingHelper>())
            // args from HloModuleConfig: [+0x718] propagate flags,
            // [+0x720]/[+0x730]/[+0x738] the
            // allow_spmd_sharding_propagation_to_output/_to_parameters vectors.
        pipeline.emplace_back(sp)                        // 0x20467b4
        options = SpmdPartitionerOptions()               // 0x20467ea  default-construct
        options.threshold = 0x186A0  (=100000)           // 0x2046809  PATCH MiB threshold
        options.<+2 bool> = 1; <+0 bool> = 0; <+6 bool> = 0   // 0x2046802/14/1b
        rsi = num_partitions                             // 0x20467ff (r14)
        rdx = [config+0x170] = num_replicas              // 0x20467d1
        partitioner = SpmdPartitioner(num_partitions,    // 0x2046831
                                      num_replicas, options)
        pipeline.emplace_back(partitioner)
    ...  // later in the SAME pipeline, post-SPMD:
    AddPass<ComparisonExpander>                          // 0x204715f
    AddPass<AllReducePromotion>(span<pair<PrimitiveType,...>>)   // 0x20475df
    AddPass<FloatNormalization>(...) x several           // float legalization
    AddPass<DynamicPadder>(DynamicPadderOptions&)        // 0x2047dc6
    AddPass<TupleSimplifier> / FlattenCallGraph / HloDCE // 0x2048332 / 0x204883a / 0x204888d
    AddPass<ChangeOpDataType>(...)                       // 0x2048b1e
```

The confirmed pipeline order within `RunHloPassesThroughLayoutAssn`:

```text
... HloVerifier ...
ShardingPropagation     (is_spmd = true)        ── 13.4/13.5 run propagation
SpmdPartitioner         (num_partitions, num_replicas, options)   ── THIS DRIVER
ComparisonExpander
AllReducePromotion
FloatNormalization (xN)
DynamicPadder
TupleSimplifier / FlattenCallGraph / HloDCE
...
```

This matches upstream XLA's "sharding-propagate → spmd-partition → collective/float normalization" ordering. `ShardingPropagation` annotates the module; `SpmdPartitioner` consumes the now-fully-sharded module; the post-SPMD passes legalize the emitted collectives and floats.

### Gating

The entire SPMD block — both the `ShardingPropagation` and the `SpmdPartitioner` emplacements — is guarded by a single config predicate held in `var_2B8` and tested as `cmp [rbp+var_2B8], 0 ; jnz <skip>` throughout the construction region (e.g. `@0x20465f6`, `@0x2046796`). Upstream this is `module->config().use_spmd_partitioning()` (STRONG). When false, **neither pass is emplaced** and the module is left unpartitioned.

The flags that flip the gate are **Neuron front-end** flags, set in the driver/walrus layer, *not* in `hlo-opt` itself:

- `--enable-experimental-spmd` → sets `HloModuleConfig::use_spmd_partitioning = true`.
- `--distribution-strategy` → selects the sharding strategy; populates the module's sharding annotations + `num_partitions`/`replica_count` in `HloModuleConfig`.

> **GOTCHA — the Neuron CLI flags do not appear inside `hlo-opt`.** There is no `--distribution-strategy` string in the `hlo-opt` strings table (CONFIRMED absent). `hlo-opt` sees only the *resulting* config plus the `mhlo.*` attributes the front-end stamped onto the module: `mhlo.use_auto_spmd_partitioning` (`@0x2fdb60`/`0x487240`/`0xbb8000`), `mhlo.num_partitions` (`@0xbb8080`), and `mhlo.spmd_output_sharding` (`@0x487280`/`0xbb8040`) — all CONFIRMED. The `num_partitions`/`replica_count` are read from config ("`Initial num_partitions from config = `" / "`Initial replica_count from config = `" strings, emitted by the Neuron hilo layer that feeds the config). A reimplementer driving the gate off a CLI string in `hlo-opt` will find nothing; the gate is a config bool.

---

## Entry and exit contract

The partitioner's value depends on two invariants the driver enforces at its boundaries: the input must be fully sharded, and the global entry signature must survive.

### Entry contract — what Run requires on input

Enforced by `PreprocessSharding(module, execution_threads) @0x2ab0560` (called first in `Run`). The exact CHECK/FATAL strings (CONFIRMED from its context):

```c
function PreprocessSharding(module, execution_threads):     // 0x2ab0560
    for hlo in every partitioned computation:
        CHECK(hlo->has_sharding())                          // every inst annotated
        if HasReplicatedSharding(hlo->sharding())
           && !CanSideEffectingHaveReplicatedSharding(hlo): // 0x2a8e260
            FATAL "side-effect HLO cannot have a replicated sharding:"
        // side-effecting ops also need "Side-effect HLO must have sharding:"
    for param in entry parameters:
        CHECK(param->has_sharding())
        if !(param->sharding().IsReplicated()
             || param->sharding().UniqueDevice().has_value()):
            FATAL "Unsupported entry parameter sharding:"    // @0x3b8c00
    CHECK(entry->root_instruction()->has_sharding())
    rs = root sharding
    ok = rs.IsReplicated() || rs.IsManual()                 // tuple: c_all_of elements
    if !ok: FATAL "Unsupported entry root sharding: "        // @0x3656d8 (trailing space)
```

The two `Unsupported entry …` rules are the global-boundary invariant in *input* form: **entry parameters may only be Replicated or pinned to a UniqueDevice; the entry root must be Replicated or Manual.** Entry params/results are never tile-sharded at the global boundary — the partitioned module still presents the original program signature to its caller; sharding inputs/outputs across devices is the caller's responsibility.

The side-effect gate is small and exact:

```c
function CanSideEffectingHaveReplicatedSharding(hlo):       // 0x2a8e260 (127 B)
    op = *(byte*)(hlo + 0x14)                                // HloOpcode (alphabetized enum)
    if op == 0x2B:                                           // kCustomCall
        cp = GetCustomCallPartitioner(hlo->custom_call_target())
        // allowed ONLY if cp's vtable slot +0x40 is the BASE
        // CustomCallPartitioner::CanSideEffectingHaveReplicatedSharding
        return cp && cp->vt[+0x40] == base_impl
    if op == 0x3C || op == 0x4A:                             // two side-effecting opcodes
        return true
    return false
```

`PreprocessHlos @0x2ab5c30` then rewrites HLOs into partition-friendly forms before the visitor runs — creating Pad / Iota / Compare / Broadcast / Constant nodes, rewriting slice/pad/dynamic-slice patterns, and re-applying `set_sharding` + `OpMetadata::CopyFrom` so the rewritten ops keep correct shardings. Per-op detail is owned by [13.5](spmd-compute-handlers.md).

### Exit contract — what Run guarantees on output

After `PartitionComputation` rewrites every computation to per-partition shapes, `Run` re-derives the entry program shape and enforces three `RET_CHECK`s (CONFIRMED strings):

| Invariant | FATAL string | Addr |
|---|---|---|
| Parameter count unchanged | `Parameter count changed for the entry computation` | `0x313020` |
| Each parameter global shape equal | `Parameter shape changed for the entry computation` | `0x2ad7a8` |
| Result global shape equal | `Result shape changed for the entry computation` | `0x297528` |

The comparison is `Shape::Equal().MinorToMajorOnlyInLayout()` on `saved.parameters(i)` vs `new.parameters(i)` and `saved.result()` vs `new.result()`, after the original layouts are restored onto the post-partition shapes (Phase E). The module config is then rewritten with a fresh `ComputationLayout` (`module->set_config`). **The global, un-sharded entry signature is preserved byte-for-byte; only the internal computation bodies are rewritten to local per-partition shapes.**

### Sharded-HLO shape convention

- **Internal ops** carry the per-partition (local) shape. A dim sharded over `k` devices has local size `ceil(global_dim / k)`; halo/padding for windowed/convolution cases is handled by the visitor (`conv_halo_exchange_always_on_lhs` default true, see Options). (STRONG)
- **Entry boundary** keeps params and root at global shape (Replicated/Manual only). `RecordInputsOutputsSharding @0x2ab2dc0` walks `parameter_instruction(i)->sharding()` and builds the `spmd_parameters_sharding` / `spmd_output_sharding` records (cf. the `mhlo.spmd_*_sharding` strings) so the caller knows how to scatter/gather. (CONFIRMED)
- **Channel ids**: every emitted collective gets a fresh id from the `&next_channel_id` counter seeded by `hlo_query::NextChannelId`. Neuron later re-stamps these via `NeuronUniqueChannelIdEnforcer` / `NeuronCollectiveStreamIdInjector` ([13.6](spmd-collective-emission.md)). (CONFIRMED)

---

## Neuron context — stock XLA vs Neuron specialization

The SPMD partitioner **driver is unmodified upstream XLA** (namespace `xla::spmd`, source `xla/service/spmd/spmd_partitioner.cc @0x37d1c8`, run by `xla::cpu::CpuCompiler`). Neuron does not fork `SpmdPartitioner::Run`. Its three contributions are all at the *edges*:

1. **It drives the partitioner** from the reused XLA `CpuCompiler` HLO pipeline (the seat above).
2. **It feeds shardings** via `mhlo.*` attributes produced by the Neuron front-end (`--distribution-strategy` / `--enable-experimental-spmd` → config + annotations).
3. **It wraps the partitioner** with a family of `xla::hilo` HLO passes that run *after* partitioning to lower and optimize the emitted collectives for the Neuron device.

The Neuron `xla::hilo` collective-adjacent passes (CONFIRMED present), all post-partition:

| Neuron pass (`xla::hilo`) | Register fn / RTTI | Role |
|---|---|---|
| `NeuronAllReduceCombiner` | `0x1e6fdc0` (dtor `0x1f8f940`) | Combine all-reduces by threshold |
| `NeuronAllGatherCombiner` / `NeuronReduceScatterCombiner` | — | Combine all-gather / reduce-scatter |
| `NeuronCollectivePermuteToAllGather` | `0x1e6fd40` (`0x1f8ffa0`) | Lower collective-permute → all-gather |
| `NeuronUniqueChannelIdEnforcer` | `0x1e6f210` (`0x1fec6f0`) | Re-stamp unique channel ids |
| `NeuronCollectiveStreamIdInjector` | — | Inject stream ids |
| `DeviceAssignmentLegalization` / `LegalizeCCOpsForTensorizer` | — | Device-assign + CC-op legalize |
| `RematerializeLargeAllGather` / `NeuronMoveAllGatherWhileLoop` | — | All-gather rematerialization / loop motion |

These are documented in [13.6](spmd-collective-emission.md) and the [4.1 pass registry](../hlo-opt/pass-registry.md); the LNC (logical-NeuronCore) seam where partition count meets the Neuron device geometry is [13.9](lnc-sharding-constraint.md). **The partition algorithm itself is stock; the only Neuron surface touching the driver is the gate, the config, and the downstream `hilo` passes.**

---

## Related Components

| Name | Relationship |
|---|---|
| `ShardingPropagation` (is_spmd=true) | Runs immediately before `SpmdPartitioner` in the same pipeline; annotates every instruction with the sharding `Run` then requires |
| `SpmdPartitioningVisitor` | The per-op rewriter `PartitionComputation` drives; emits the local shapes + collectives |
| `xla::cpu::CpuCompiler` | The stock XLA compiler whose HLO pipeline Neuron reuses; constructs and runs this pass |
| `xla::hilo` collective passes | Post-partition Neuron passes that lower the emitted collectives for the device |

## Cross-References

- [13.5 SPMD Compute-Op Partition Handlers](spmd-compute-handlers.md) — the visitor `PartitionComputation` drives; owns the per-operator rewrite logic
- [13.6 SPMD collective emission](spmd-collective-emission.md) — the all-reduce / all-gather / all-to-all / collective-permute emitters keyed by `num_partitions × num_replicas`, plus the `xla::hilo` collective passes that run after partitioning
- [13.2 ShardingPropagation Engine](sharding-propagation.md) — the pass that annotates the module the driver's entry contract requires
- [13.3 Sharding Algebra](sharding-algebra.md) — the factor algebra the propagation pass operates on
- [13.9 AwsNeuronLNCShardingConstraint and the SPMD↔LNC Coupling](lnc-sharding-constraint.md) — how `num_partitions` maps onto the Neuron logical-NeuronCore device geometry
- [4.1 The hlo-opt Pass Registry](../hlo-opt/pass-registry.md) — the `--passes` table that places these passes in the `hlo-opt` invocation
