# Collective Bucketing and Dynamic Instruction Estimation

> *All symbols, addresses, and strings on this page apply to `neuronx-cc 2.24.5133.0+58f8de22` (cp310 wheel). The three subjects are Cython-compiled extension modules under `neuronxcc/starfish/penguin/`: `targets/transforms/BucketizeCCOp.cpython-310-x86_64-linux-gnu.so` (536 KB), `transforms/DynamicInstEstimator.cpython-310-x86_64-linux-gnu.so` (325 KB), and `transforms/DetailDynInst.cpython-310-x86_64-linux-gnu.so` (676 KB). Evidence is the IDA decompile (`*_full.c`, `*_function_addresses.json`), the Cython string pools (`*_strings.json`), and the gating flag strings in the `Frontend` / `Penguin` / `sunda CodeGenFlow` modules. Function addresses are in-module `__pyx_pw_*` offsets; cp311/cp312 relocate every address. Other versions will differ.*

## Abstract

This page documents two unrelated backend mechanisms that share a tenuous surface similarity — both produce an *estimate* of how much work a region will do — but answer different questions and feed different consumers.

The first is **`BucketizeCCOp`**, a Neuron-authored `sunda` backend pass (base class `TargetLowering`) that **coalesces** runs of consecutive same-kind collectives. Its docstring is verbatim: *"Group small CCOp into bigger CCOp, the input and output of the 'bucketized' CCOp will be represented by tuple."* It walks a function's collective operations, accumulating consecutive `AllGatherOp` / `AllReduceOp` / `ReduceScatterOp` that share a `(group_id, factor)` key, and merges each maximal run into a single tuple-IO collective — concatenating all the operands into one operand tuple and all the results into one result tuple, then erasing the originals. The payoff is amortising the fixed per-collective launch/handshake cost over many small tensors. It is gated by the `ccop-bucketing` compiler flag (help text *"Bucketing ccop in compiler"*); the Python driver exposes this as the `--internal-ccop-*-bucketing` flag family, including per-kind byte-size caps that bound how large a bucket may grow. This is the real collective combiner the bucketing flags drive — confirmed, not a false hit.

The second is the **`DynamicInstEstimator`** / **`DetailDynInst`** pair, both `DotTransform` visitors. `DynamicInstEstimator` is the *coarse* estimator: per instruction it floor-divides the op's `num_dynamic_instances` (its dynamic element/iteration count) by a fixed power-of-two reciprocal — `2^16` for loads/stores, `2^23` for `TensorContractOp` — and accumulates the sum into a class-level `est_inst_count`, plus two shape flags `has_copy` / `has_reduce`. `DetailDynInst` is the *precise* counterpart: it computes the exact per-instruction dynamic element count honouring the full loop-nest domain and predicate masks, then multiplies by `compute_flops` (→ `Total_flops`) and `dtype_size_in_bytes` (→ `Total_bytes_moved`). Both are cost-model inputs for data-dependent (dynamic-trip-count) regions where an exact count is impossible.

> **NOTE —** `KernelMetrics` (`penguin/KernelMetrics.so`) is **not** part of this story and must not be conflated with the estimators. It is a singleton NKI-kernel *telemetry* collector (`KernelMetricsCollector`) that tallies how many kernels of each frontend category were compiled and exports `kernel_info.json`. It is build-time diagnostics, not a dynamic-instruction cost model. See [§ KernelMetrics — Not an Estimator](#kernelmetrics--not-an-estimator).

For reimplementation, the contract is:

- **The bucketing grouping key** — only consecutive collectives of the *same kind, same `group_id`, same `factor`* coalesce; any change flushes the current run.
- **The tuple-IO merge** — clone each member, extend a shared operand list and a shared result list, build one new collective with those tuples, erase the members, bump the per-kind coalesced counter.
- **The two coarse divisors** — `2^16` (load/store/contract) and `2^23` (`TensorContract`), and the global class-attribute accumulation.
- **The precise path** — `masked_dynamic_instances = is_predicated ? size(predicated_domain) : domain_size`, then `flops = compute_flops × n` and `bytes = dtype_size × n`.

| | |
|---|---|
| **`BucketizeCCOp` base class** | `TargetLowering` (`sunda` backend lowering visitor) |
| **`bucketizeCCOp` (merge)** | `0x10bd0` |
| **`transformCCOp` (grouping)** | `0x134a0` |
| **Gating flag** | `ccop-bucketing` ("Bucketing ccop in compiler") — in `Frontend`, `Penguin`, `sunda CodeGenFlow` |
| **`DynamicInstEstimator.transformStmts`** | `0x92e0` |
| **Coarse divisors** | `__pyx_int_65536` (`2^16`), `__pyx_int_8388608` (`2^23`) |
| **`DetailDynInst` precise sinks** | `transformInstruction` `0x12270`, `transformLoadStore` `0x161b0` |
| **Stat backbone** | `neuronxcc.starfish.penguin.Statistics.register_stats` (all three passes) |
| **Origin** | Neuron-authored (`sunda` backend / penguin transforms) — *not* stock XLA |

---

## BucketizeCCOp — Collective Coalescing

### Purpose

`BucketizeCCOp` reduces the *number* of collective instructions in a function by batching many small same-kind collectives into one large collective whose operands and results are tuples. The verbatim module docstring (string pool, `*_strings.json`):

```text
Copyright (C) 2023, Amazon.com. ...
BucketizeCCOp - Group small CCOp into bigger CCOp, the input and output of the
'bucketized' CCOp will be represented by tuple
```

This is the canonical collective-bucketing optimisation: a collective's fixed launch and handshake cost (rendezvous across the process group) is paid once per *call*, not per *tensor*, so issuing N small tensors as one batched collective amortises that cost N-fold. The pass is Neuron-authored — it lives in the `sunda` target's transform set, has no stock-XLA analogue, and is gated by the Neuron-private `ccop-bucketing` flag.

The pass registers three diagnostic counters through `register_stats`, with these printed descriptions (verbatim from the string pool):

| Counter | Description string |
|---|---|
| `num_all_gathers_coalesced` | `"Number of AllGathers coalesced"` |
| `num_all_reduces_coalesced` | `"Number of AllReduces coalesced"` |
| `num_reduce_scatters_coalesced` | `"Number of ReduceScatters coalesced"` |

### Entry Point

`BucketizeCCOp` is a `TargetLowering` visitor: the framework calls `beforeStmtTransform` at the start of each function, dispatches each collective op to the matching `transform*Op` hook, and calls `afterStmtTransform` at the end. The three per-kind hooks are thin shims onto the shared `transformCCOp` dispatcher; `afterStmtTransform` flushes the three pending runs.

```text
beforeStmtTransform(f)                 ── clear pending_allgathers / _reducescatters / _allreduces
  transformAllGatherOp(op)             ── transformCCOp(op, self.pending_allgathers)
  transformReduceScatterOp(op)         ── transformCCOp(op, self.pending_reducescatters)
  transformAllReduceOp(op)             ── transformCCOp(op, self.pending_allreduces)
    └─ transformCCOp(op, pending)      ── 0x134a0  group: match (group_id, factor) or flush
afterStmtTransform(f)                  ── bucketizeCCOp(each pending list)  [final flush]
       └─ bucketizeCCOp(ccops)         ── 0x10bd0  merge a run into one tuple-IO CCOp
```

Three separate pending lists (`pending_allgathers`, `pending_reducescatters`, `pending_allreduces` — all three names verbatim in the pool) keep the kinds apart: an all-gather can never coalesce with an all-reduce, so each kind accumulates independently and is flushed independently.

### Algorithm — the grouping key

`transformCCOp` (`0x134a0`) accumulates the current op into its pending list, but first checks whether the op breaks the run that is currently being built. The run boundary is a change in `(group_id, factor)`; the kind is already fixed by *which* pending list the op was routed to.

```c
function transformCCOp(self, ccop, pending):     // 0x134a0
    gid = ccop.group_id                          // GetAttr group_id  (str tab[46] "group_id")
    if gid is None or len(gid) == 0:             // PyObject_Size on group_id
        return                                   // no process group → not bucketizable

    if pending:                                  // a run is already accumulating
        last = pending[-1]                       // the op the run was last extended with
        // bucket boundary = ANY change in factor or group_id:
        if last.factor   != ccop.factor   or     // GetAttr factor (str tab[44]); RichCompare
           last.group_id != ccop.group_id:       // GetAttr group_id; RichCompare
            self.bucketizeCCOp(pending)           // flush the finished run into one CCOp
            pending.clear()                       // start a fresh run
            // ("different factor" diag string, str tab[38], tags the factor-mismatch branch)

    pending.append(ccop)                          // extend the current run (str tab[26] "append")
    return
```

> **GOTCHA —** the merge key is `(kind, group_id, factor)`, *not* tensor size. `factor` is the collective's sharding/replication factor (process-group size class) and `group_id` names the communicator/process group. Two collectives may be combined only when they run over the *same communicator with a compatible factor* — combining across different groups would be semantically wrong (different rank sets). A reimplementation that buckets by byte size alone, ignoring `group_id`/`factor`, miscompiles. The byte-size *cap* exists, but it is applied upstream by the driver (see [§ Related Knobs](#related-knobs)), not by this match.

> **QUIRK —** the run is built by *consecutive* adjacency, not by a global grouping pass. `transformCCOp` only ever compares against `pending[-1]`; a same-key collective separated from the run by an intervening different-key collective lands in a *new* bucket. This keeps the rewrite order-preserving (no reordering of collectives across each other) at the cost of missing non-adjacent coalescing opportunities — that reordering is the job of `AGOrderingAnalysis`, a separate pass ([§ Related Components](#related-components)).

### Algorithm — the tuple-IO merge

`bucketizeCCOp` (`0x10bd0`) turns one accumulated run into a single collective whose operands and results are the concatenation of every member's operands and results.

```c
function bucketizeCCOp(self, ccops):             // 0x10bd0
    if len(ccops) <= 1:                          // a run of one is nothing to coalesce
        return

    combined_operands = []                       // PyList_New
    combined_results  = []                       // PyList_New
    for ccop in ccops:                           // iterate the run
        combined_operands.extend(ccop.clone().operands)   // clone (tab[36]); .operands (tab[72]); _PyList_Extend
        combined_results.extend (ccop.clone().results)    // clone; .results (tab[84]); _PyList_Extend

    first = ccops[0]
    new_ccop = type(first)(                      // construct ONE new collective of the same kind
        operands = combined_operands,            // PyDict kwargs (PyDict_SetItem ×3)
        results  = combined_results,             //   → tuple-typed I/O
        id       = first.id)                     // inherit the first member's id

    insert new_ccop at the run's position
    for ccop in ccops:                           // delete every original
        ccop.eraseFromParent()                   // eraseFromParent (tab[41])

    n = len(ccops)                               // bump the matching coalesced counter:
    if   isinstance(first, AllGatherOp):     num_all_gathers_coalesced     += n   // InPlaceAdd
    elif isinstance(first, ReduceScatterOp): num_reduce_scatters_coalesced += n
    elif isinstance(first, AllReduceOp):     num_all_reduces_coalesced     += n
```

The cloned-then-extended operand/result lists are the heart of the rewrite: the docstring's *"input and output ... represented by tuple"* is exactly `combined_operands` / `combined_results` becoming the new op's I/O. The grounding for every step is in `*_full.c`: `clone` (str tab[36]), `operands` (tab[72]), `results` (tab[84]), `_PyList_Extend`, `eraseFromParent` (tab[41]), and the three `isinstance` + `PyNumber_InPlaceAdd` arms onto the coalesced counters. (CONFIRMED — bodies and arithmetic seen in the decompile.)

> **NOTE —** there is **no byte-size threshold inside this `.so`**. The pass enforces only the `(group_id, factor, kind)` compatibility key and the `len > 1` guard; the size/count cap that decides *how big a bucket may grow* is applied by the upstream driver that segments the collectives before they reach `transformCCOp`. The grouping here is pure adjacency + key match.

### Related Knobs

The `ccop-bucketing` flag gates the whole pass (its help string *"Bucketing ccop in compiler"* lives in `sunda CodeGenFlow`, `Penguin`, and `Frontend`). The Python driver (`neuronxcc/driver/jobs/Frontend`) exposes the full `--internal-ccop-*-bucketing` family — verbatim `.rodata` strings, listed below. The per-kind enable knobs drive *which* collective kinds get bucketed; the `*-size-in-bytes` knobs supply the byte cap the upstream driver uses to bound a bucket.

| Knob (verbatim flag string) | Role |
|---|---|
| `--internal-ccop-bucketing` | Master enable (the `ccop-bucketing` attr) |
| `--internal-ccop-all-gather-bucketing` | Enable bucketing for AllGathers |
| `--internal-ccop-all-reduce-bucketing` | Enable bucketing for AllReduces |
| `--internal-ccop-reduce-scatter-bucketing` | Enable bucketing for ReduceScatters |
| `--internal-ccop-bucketing-allgather-size-in-bytes` | Per-bucket byte cap for AllGathers |
| `--internal-ccop-bucketing-allreduce-size-in-bytes` | Per-bucket byte cap for AllReduces |
| `--internal-ccop-bucketing-reducescatter-size-in-bytes` | Per-bucket byte cap for ReduceScatters |

> **CORRECTION (Z01) —** an earlier hypothesis treated `BucketizeCCOp` as a possible false hit / unrelated "bucket" (histogram or domain-tiling). The decompiled body and docstring **refute** that: it is the real collective combiner, and the `--internal-ccop-*-bucketing` family drives *this* pass. The divisor-like thresholds seen earlier are the `*-size-in-bytes` byte caps that bound a bucket, applied upstream — not part of the merge.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `bucketizeCCOp` | `0x10bd0` | Merge a run → one tuple-IO CCOp; bump counter | CERTAIN |
| `transformCCOp` | `0x134a0` | Grouping dispatcher; `(group_id, factor)` boundary | CERTAIN |
| `transformAllGatherOp` | (per-kind shim) | `transformCCOp(op, pending_allgathers)` | HIGH |
| `transformReduceScatterOp` | (per-kind shim) | `transformCCOp(op, pending_reducescatters)` | HIGH |
| `transformAllReduceOp` | (per-kind shim) | `transformCCOp(op, pending_allreduces)` | HIGH |
| `beforeStmtTransform` | (visitor hook) | Clear all three pending lists | HIGH |
| `afterStmtTransform` | (visitor hook) | Flush all three pending lists | HIGH |

---

## DynamicInstEstimator — Coarse Instruction Count

### Purpose

`DynamicInstEstimator` is a `DotTransform` visitor whose docstring is *"DynamicInstEstimator - Estimate the dynamic number of instructions"*. It produces a **cheap, coarse** estimate of how many hardware instructions a function's data-dependent region will execute, by converting each op's dynamic *element* count into an instruction count via a fixed elements-per-instruction reciprocal. The single registered stat carries the description *"Estimated instruction count."* and accumulates into the class attribute `est_inst_count`. It is Neuron-authored (penguin transform; imports `DotTransform`, `Statistics.register_stats`, and the penguin IR op types).

### Algorithm

`transformStmts` (`0x92e0`) is the DotTransform visitor hook, invoked per function `f`. It walks `f.insts`, classifies each instruction, and floor-divides its `num_dynamic_instances` by one of two constants. It also OR-accumulates two boolean shape flags.

```c
function transformStmts(self, f):                          // 0x92e0
    has_copy = False; has_reduce = False
    for inst in f.insts:
        if isinstance(inst, TensorContractOp):             // heavy matmul-class op
            DynamicInstEstimator.est_inst_count +=
                inst.num_dynamic_instances // 8388608      // __pyx_int_8388608 = 0x800000 = 2^23
                                                           //   (FloorDivideObjC @ full.c:6182)
        elif isinstance(inst, (GenericLoadStore, AffineLoadStore)):
            DynamicInstEstimator.est_inst_count +=
                inst.num_dynamic_instances // 65536        // __pyx_int_65536  = 0x10000  = 2^16
                                                           //   (FloorDivideObjC @ full.c:5859, &off_10000)

        if isinstance(inst, (ReduceLikeOp, RmsNormOp)):    // reduce-bearing op
            has_reduce = True
        if isinstance(inst, AffineStore) and isinstance(inst.src, AffineLoad):
            has_copy = True                                // store-of-load = a copy pattern

    if has_copy:   f.set_attr("has_copy",   True)          // ("has_copy",)  set on the function
    if has_reduce: f.set_attr("has_reduce", True)          // ("has_reduce",)
    return False                                           // Py_False — no IR mutation
```

Both `FloorDivide` calls and both constants are CONFIRMED in `*_full.c`: line 5859 divides by `__pyx_int_65536` (auxiliary literal `&off_10000` = `0x10000`), line 6182 divides by `__pyx_int_8388608` (`0x800000`). Both results are `PyNumber_InPlaceAdd`-ed into the class attribute `est_inst_count` via `tp_setattro` on the *class* object, so the estimate accumulates **globally across every function visited**, not per-function.

> **QUIRK —** the accumulator is a *class* attribute, not an instance field. The estimate is one running total over the whole compilation, reset between compilations rather than between functions. A reimplementation that puts `est_inst_count` on `self` produces per-function totals and breaks the cost model's whole-program comparison.

### Why two divisors

`num_dynamic_instances` is the op's dynamic element/iteration count — how many scalar/vector lanes it will execute. Dividing by a fixed *elements-per-instruction* reciprocal converts an element count into an instruction count:

- **`2^16` (65536)** elements per emitted load/store/contract instruction — one DMA or PE pass moves on the order of 64 K elements.
- **`2^23` (8388608)** elements per emitted `TensorContract` instruction — a full matmul tile does far more work per issued instruction, so the same element count maps to far fewer instructions (a 128× larger divisor than the load/store case).

Floor division makes the result deliberately coarse: this is the *estimate*, not the exact count, for a region whose true trip count is data-dependent. `has_copy` and `has_reduce` are coarse function-shape flags consumed downstream — a pure-copy or reduce-dominated function can take a cheaper schedule.

### Op-type coverage

`DynamicInstEstimator` references exactly these IR op types (string pool): `TensorContractOp`, `GenericLoadStore`, `AffineLoadStore`, `AffineStore`, `AffineLoad`, `ReduceLikeOp`, `RmsNormOp`. Its only metric names are `est_inst_count` and `num_dynamic_instances`. It has only two methods — `__init__` and `transformStmts`. It is the coarse estimator; the precise one is a separate module.

---

## DetailDynInst — Precise FLOP and Byte Profiler

### Purpose

`DetailDynInst` is the precise cost model that `DynamicInstEstimator` is the cheap approximation of. Its docstring (string pool): *"DetailDynInst - Print the total dynamic instance of a function, consider predicates"*. Where the coarse estimator floor-divides a raw element count, `DetailDynInst` computes the **exact** dynamic element count per instruction — honouring (a) the full loop-nest iteration domain and (b) predication (masked-off lanes do not count) — then turns it into FLOPs and bytes moved. It is older than the coarse estimator (Copyright 2019 vs 2021) and is also a `DotTransform` visitor, importing `numpy`, `LogContext`, and the penguin IR. Two class-level `register_stats` accumulators, `Total_flops` and `Total_bytes_moved`, are printed by `print_profile` as *"Total flops:"* / *"Total bytes moved:"*.

### Algorithm

The core is `masked_dynamic_instances` — the exact dynamic element count of one instruction — which `transformInstruction` turns into FLOPs and `transformLoadStore` turns into bytes.

```c
function masked_dynamic_instances(self, inst):   // 0x17360
    if inst.is_predicated:                        // GetAttr is_predicated; RichCompare (disasm)
        return size(self.predicated_domain(inst)) //   domain ∩ active masks (masked lanes excluded)
    else:
        return self.domain_size(inst)             //   product over domain_shape (all lanes)

function transformInstruction(self, inst):       // 0x12270  (main dispatch)
    n     = self.masked_dynamic_instances(inst)
    flops = inst.compute_flops * n               // GetAttr compute_flops; PyNumber_Multiply
    DetailDynInst.Total_flops += flops           // PyNumber_InPlaceAdd (class attr)

function transformLoadStore(self, inst):         // 0x161b0
    n     = self.masked_dynamic_instances(inst)
    bytes = n * dtype_size_in_bytes(inst.dtype)  // datamove_bytes; PyNumber_Multiply
    DetailDynInst.Total_bytes_moved += bytes     // PyNumber_InPlaceAdd (class attr)

function print_profile(self):                    // 0x14ef0
    print("Total flops:",       Total_flops)
    print("Total bytes moved:", Total_bytes_moved)
```

The iteration domain is supplied by three generators — `domain` (yields the loop-nest indices), `domain_size` (product over `domain_shape`), and `predicated_domain` (intersects the domain with the active masks, using `generateDomains` / `evalMasks`). The two metrics are `compute_flops × n` and `dtype_size × n`. All names — `masked_dynamic_instances`, `predicated_domain`, `domain_size`, `domain_shape`, `is_predicated`, `compute_flops`, `datamove_bytes`, `evalMasks`, `generateDomains` — are CONFIRMED in the string pool; the four method addresses resolve to the named `__pyx_pw_*DetailDynInst*` symbols. The PLT-call shapes (GetAttr + Multiply + InPlaceAdd) are confirmed in disasm; the exact per-line algebra is STRONG (reconstructed from call profiles + line anchors, no full decompile of this `.so`).

> **NOTE —** the loop trip count that sizes the domain comes from data-dependent control flow. For a dynamic `for` region (see [InstDynamicForLoop](../penguin/dynamic-for-loop.md)), the trip count is itself an estimate, so the FLOP/byte total `DetailDynInst` derives from it is necessarily an estimate — the same "estimate, not exact" property the coarse path has, but with predication and full-domain precision rather than a power-of-two reciprocal.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `masked_dynamic_instances` | `0x17360` | Exact dynamic element count (predicate-aware) | HIGH |
| `transformInstruction` | `0x12270` | `flops = compute_flops × n` → `Total_flops` | HIGH |
| `transformLoadStore` | `0x161b0` | `bytes = dtype_size × n` → `Total_bytes_moved` | HIGH |
| `print_profile` | `0x14ef0` | Print the two accumulators | HIGH |
| `predicated_domain` | (generator) | Domain ∩ masks (`evalMasks` / `generateDomains`) | HIGH |
| `domain` / `domain_size` / `domain_shape` | (generators) | Loop-nest iteration domain and its size | HIGH |

---

## KernelMetrics — Not an Estimator

`penguin/KernelMetrics.cpython-310-...so` is a **separate subsystem** and must not be conflated with the dynamic-instruction estimators. Its docstring (string pool, verbatim) describes a *"Singleton collector for NKI kernel metrics (both internal and external)"* that *"accumulates metrics from multiple passes during compilation"* — `LowerIntrinsics` records IRBuilder ops, `InlineNativeKernels` records old NKI-FE kernels, `BirCodeGenLoop` records new NKI-FE kernels. At end of compilation `export_to_kernel_info_json()` writes `kernel_info_sg{id}.json`, later aggregated into a global `kernel_info.json`.

The class is `KernelMetricsCollector` (singleton: `get_instance` / `record_kernel` / `to_dict`). It tallies *how many kernels of each frontend category were compiled* — pure build-time telemetry. It is **not** consumed by the cost model or scheduler and has nothing to do with `num_dynamic_instances`, `est_inst_count`, `Total_flops`, or `Total_bytes_moved`. The name overlap ("metrics") is the only connection.

---

## Where These Feed

All three passes register through `neuronxcc.starfish.penguin.Statistics.register_stats` — the compiler's metrics sink. The estimators and the combiner counters land in the same statistics channel but serve different consumers.

```text
DynamicInstEstimator.est_inst_count   ─┐
DetailDynInst.Total_flops              ├─→ Statistics (register_stats) ─→ cost model / scheduler
DetailDynInst.Total_bytes_moved        │       heuristics: compare candidate lowerings of
has_copy / has_reduce ─────────────────┘       dynamic-trip-count regions; pick cheaper schedules
                                               for pure-copy / reduce-dominated functions

BucketizeCCOp.num_*_coalesced ─────────→ Statistics (diagnostics) — quantifies the collective
                                          reduction; the merge itself shrinks the schedule the
                                          SPMD collective emitter must lower (fewer, larger CCs)
```

`est_inst_count` (coarse) and `Total_flops` / `Total_bytes_moved` (precise) are cost-model inputs: a per-function instruction / FLOP / byte estimate used by scheduling and tiling heuristics where an exact count is impossible. The values are emitted as stats (CONFIRMED); the specific downstream module that reads them is not in these `.so` files, so "cost model / scheduler" is the grounded scope and the exact autotuner hook is INFERRED.

`BucketizeCCOp`'s coalescing directly reduces the *number* of collective instructions the [SPMD collective emitter](spmd-collective-emission.md) must schedule — fewer, larger collectives mean lower aggregate launch/handshake overhead and a smaller schedule. The `num_*_coalesced` counters are diagnostics measuring that reduction.

---

## Related Components

These are distinct collective optimisations. `BucketizeCCOp` does **not** call any of them and none call it — their relationship is pipeline-ordering, not call-graph (`BucketizeCCOp.so` references only `ir.ir` / `IRCloner` / `Statistics` / `TargetLowering`).

| Component | Relationship |
|---|---|
| `AGOrderingAnalysis` | An *analysis* (not a rewrite): builds an all-gather **ordering graph** (`AGGlobalOrderingEdge` with cost/priority/forced edges) deciding the relative order/tiling of all-gathers. Orthogonal to bucketing's "which to batch". Consumed later by the layout/tiling pipeline. |
| `CCOpFusion` / `FineGrainedCCOpFusion` | A rewrite that **fuses one collective into surrounding compute/loop** to overlap it with adjacent work — one CC at a time, vs bucketing's many-CC batch. Caveat string: *"BirKernel has static IO which cannot have CCop Fusion."* |
| `CoalesceCCOp` | A related but distinct coalescer that merges by **factorising rank/shape into a shared buffer** (`FactorizeRankDim`), where `BucketizeCCOp` tuple-batches whole CCs by `(group_id, factor)`. |

**Pipeline order** (STRONG, from `sunda CodeGenFlow` co-resident strings): `HoistFSDPCollectives → BucketizeCCOp (ccop-bucketing) → CoalesceCCOp → CCOpFusion / FineGrainedCCOpFusion → LegalizeCCOpLayout`. The exact `BucketizeCCOp`-vs-`CoalesceCCOp` ordering is INFERRED (both present; bucketing is the coarse tuple-batch ahead of the rank-factorising coalesce).

---

## Cross-References

- [AllReduce / ReduceScatter / AllGather Combiners & Threshold Model](../hlo-opt/collective-combiners.md) — the HLO-level combiner and its byte-threshold model; `BucketizeCCOp` is the backend-IR analogue applied during `sunda` lowering.
- [SPMD Collective / Communication Emission](spmd-collective-emission.md) — the emitter whose schedule `BucketizeCCOp` shrinks by coalescing collectives into tuple-IO calls.
- [Data-Dependent Control Flow: InstDynamicForLoop](../penguin/dynamic-for-loop.md) — supplies the dynamic loop trip count that sizes the domain `DetailDynInst` costs and that makes the instruction estimate an estimate.
