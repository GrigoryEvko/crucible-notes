# Performance and Cost Model

## Abstract

Tileiras turns "make this kernel fast" into three coordinated decisions. The modulo scheduler chooses a steady-state initiation interval and seats every op into a Resource Reservation Table. The layout selector picks SMEM swizzles, register fragments, and TMEM atoms for each pipelined value. The per-architecture atom catalog binds the dialect-level operation roster to concrete hardware costs — TMA bytes per transfer, WGMMA shape and accumulator stride, tcgen05 column count, transport-row occupancy. The three layers feed a layered cost model that ranks placements lexicographically and rejects illegal ones outright.

The cost model is not a black box. Every term in it traces back to a concrete hardware constraint: a row bit in the Blackwell 15-slot vocabulary, a capacity pool cap, an SMEM bank-conflict count, a TMEM cycles-per-row charge. A reader who understands those four sources understands why one schedule wins over another at the same `--opt-level`.

## Cost Vocabulary

The compiler tracks five categories of cost. Each enters the model at a different stage and serves a different downstream consumer.

| Category | Unit | Where it enters | Consumed by |
|---|---|---|---|
| latency | cycles | per-op latency table seeded by the resource constraint builder | modulo scheduler MII bound, structural distance closure |
| resource pressure | row bit + pool count | per-op footprint in the Resource Reservation Table | placement admission probe |
| register pressure | live virtual registers | layout selector cost layer 3 | layout choice and spill avoidance |
| SMEM bytes | bytes per CTA | buffer assignment and TMA descriptor builder | capacity pool index 5 (singleton SMEM lock), 227 KiB ceiling |
| TMEM columns | columns per allocation | tcgen05 atom verifier | capacity pool index 1 (TMEM bank pressure, cap = 4) |
| bank conflicts | 32-byte transactions | layout selector layer 3 SMEM term | tie-break in three-term layout score |
| code size | PTX bytes | NVPTX AsmPrinter | not modeled — observable only in the emitted text |

Latency is the most pervasive term because it feeds both the modulo scheduler's lower-bound calculation and the structural distance matrix that the cost-based fallback walks. The 23-entry per-op latency table at [Blackwell Pipeline 15-Slot Model — Per-Op Latency Table](../scheduler/blackwell-pipeline-15-slot-model.md#per-op-latency-table) is the primary source; dependence-edge latency adds to it through the Floyd-Warshall closure.

Resource pressure and register pressure are independent axes. A schedule can be resource-feasible (every modulo cycle clears its row probe) while still spilling registers to local memory because the assigned layouts demand more fragments than the SM offers. The two axes do not trade against each other inside the modulo scheduler — register pressure is decided one layer earlier by the layout selector and presented to the scheduler as a fixed input.

## Lexicographic Cost Vector

Tileiras uses lexicographic comparison, not weighted sums, at both the scheduler and the layout selector. A candidate that improves a softer term at the price of a harder one always loses.

### Scheduler: Four-Component Vector

The modulo scheduler's cost-based generator ranks candidates by a four-component vector. Component 1 is a hard gate — a candidate that fails it scores `(∞, *, *, *)` and is rejected before any later component matters.

| Position | Component | Source | Role |
|---:|---|---|---|
| 1 | resource legality | RRT row-OR test plus capacity-pool caps | hard admission gate |
| 2 | pipe-slot pressure | structural distance matrix produced by the SSE2-unrolled Floyd-Warshall | hard gate on dependence reach |
| 3 | bank-pressure pressure | dual-RRT probe of in-iteration vs cross-iteration occupancy | preference signal |
| 4 | structural distance | Kendall-tau inversion count vs original program order | tie-breaker |

The two evaluators that produce components 2 and 3 share an exponential-then-binary search driver — the driver doubles its cost threshold until the candidate first becomes feasible, then binary-searches for the smallest feasible threshold. The threshold is the candidate's score on that component; the cost reducer picks the candidate with the minimum across all candidates at the current cycle. [Schedule Solve and Cost Evaluators — Worked Scoring Example](../scheduler/schedule-solve-and-cost-evaluators.md#worked-scoring-example) walks the cost vector through a four-op loop body.

### Layout Selector: Three-Term Additive

The layout selector — D14 in the TileAS pipeline — uses a different cost shape because layouts are evaluated independently per pipeline alias group rather than against a shared modulo cycle. The structural filter at layer 2 enforces hard constraints (operand shape, memory space, alignment), so layer 3 sees only legal candidates and can sum cost terms with a fixed weight vector:

```text
score = w_smem · smem_transactions
      + w_tmem · tmem_cycles
      + w_reg  · live_registers
```

The default weight vector is `(1, 4, 0.25)` on `(SMEM, TMEM, registers)`. Ties break on register pressure first, then on SMEM bank-conflict count — see [TileAS Layout and Buffer Family — Three-Layer Cost Model](../passes/tileas/layout-and-buffer-family.md#three-layer-cost-model).

The two cost shapes coexist because they answer different questions. The scheduler's lexicographic vector ranks placements against a fixed `II` where any single resource overcommit kills the candidate. The layout selector's additive score ranks layouts whose legality has already been proven, so summing costs across orthogonal axes is sound.

## Roofline Reasoning

The number of pipeline stages a software-pipelined loop needs is fundamentally a roofline calculation. The compiler computes it as:

```text
stage_count = ceil(memory_cycles / compute_cycles)
```

where `memory_cycles` is the wall-clock cost of the loop's heaviest async load and `compute_cycles` is the throughput of the surrounding compute. The modulo scheduler's `II` is the floor — it sets how often a new iteration starts — and the stage count is the ceiling — how many in-flight iterations the steady state holds.

### Worked Example 1: TMA Load + WGMMA on Hopper

A typical SM90 mainloop loads a `(128, 64)` tile through TMA and consumes it through `wgmma.mma_async.m64n128k16`:

| Op | Slot | Cycles | Role |
|---|---|---:|---|
| `cp.async.bulk.tensor` (TMA) | `tma` + `tp_smem_wr` | 8 | descriptor + SMEM write transport |
| `wgmma.mma_async` | `tc_and_mma` + `tp_mma` | 8 | TC issue + MMA transport |

Both ops occupy 8 cycles on Hopper. The roofline ratio is `8 / 8 = 1`, so `stage_count = ceil(1) = 1`. A single in-flight iteration is enough to keep the WGMMA pipe fed — the compiler does not need to overlap multiple loads with multiple computes, because the load finishes in exactly the time the compute takes.

In practice the compiler still schedules two stages, because the TMA descriptor commit (`cp.async.bulk.commit_group`) and the WGMMA fence (`wgmma.fence`) introduce a half-cycle of synchronisation overhead. The modulo scheduler models this by promoting the producer-consumer pair to stage `(0, 1)` rather than collapsing them into stage `(0, 0)`.

### Worked Example 2: TMA Load + tcgen05 on Blackwell

The same mainloop on Blackwell uses tcgen05 instead of WGMMA, and the cost picture changes. The accumulator now lives in tensor memory, not in registers:

| Op | Slot | Cycles | Role |
|---|---|---:|---|
| `cp.async.bulk.tensor` (TMA) | `tma` + `tp_smem_wr` | 8 | descriptor + SMEM write transport |
| `cp.async.tcgen05` (SMEM→TMEM) | `tp_tmem_wr` | 7 | tensor-memory write transport |
| `tcgen05.mma` | `tc_and_mma` + `tp_mma` | 8 | TC issue + MMA transport |
| `tcgen05.ld` (TMEM→reg) | `tp_tmem_rd` | 7 | tensor-memory read transport (epilogue only) |

The load-to-compute path is now a chain of three transports — SMEM write, TMEM write, MMA issue — each consuming a different singleton transport row. The roofline ratio is `(8 + 7) / 8 = 1.875`, so `stage_count = ceil(1.875) = 2`. The compiler must keep at least two iterations in flight to hide the SMEM→TMEM staging.

The capacity pool at index 1 caps in-iteration TMEM bank pressure at 4 (see [Blackwell Pipeline 15-Slot Model — Pool Capacity Vector](../scheduler/blackwell-pipeline-15-slot-model.md#pool-capacity-vector)). Two in-flight iterations consume two banks each on average, leaving headroom; a kernel that tried for four stages would saturate the TMEM bank cap and the cost-based generator would reject the placement at component 1.

The 5000-cycle HBM3e ceiling at [Blackwell Pipeline 15-Slot Model — Cycle Anchor Table](../scheduler/blackwell-pipeline-15-slot-model.md#cycle-anchor-table) is the absolute round-trip budget the scheduler attributes to a worst-case far-memory dependence. If the loop body's accumulated latency exceeds 5000 cycles before the dependence closes, the candidate is rejected before any RRT probe runs — the Big-M term acts as a hard ceiling on pipeline depth.

## Opt Level Table

Each `--opt-level=N` selects a different pass pipeline; see [Pass List by Optimization Level](../pipeline/full-pass-list-by-opt-level.md) for the full per-level pass roster. The performance-relevant compile-time and runtime trade-offs:

| Level | Compile-time cost | Runtime quality | Used for |
|---|---|---|---|
| `O0` | minimal (verify-only) | not runnable — IR stays in `cuda_tile` | bytecode round-trip validation |
| `O1` | low (one dialect hop) | not runnable — IR stays in TileAA | front-end debugging |
| `O2` | moderate (full scheduler) | production-grade with default placement | default production builds |
| `O3` | high (full conversion stack) | production-grade with full kernel-ABI legalisation | non-debug production builds, the only level that exercises every NVVM target attachment |

The modulo scheduler is the dominant pass at both `O2` and `O3` — the difference between the two is not scheduling quality but the breadth of dialects converted to LLVM. The cost-based generator runs at both levels; its cost vector is identical. A kernel that scheduled well at `O2` will schedule identically at `O3` because the schedule analysis is computed once and reused.

Warp-specialised scheduling is a layered adder, not a level. When `pipeline-strategy=warp-specialize` is set, the adder replaces the modulo-schedule stage with a warp-specialisation pipeline that partitions the loop body across agents. The light variant (when `rrt-size-threshold=0`) inserts boundaries and barriers without scheduling; the heavy variant runs the full modulo scheduler against agent-partitioned RRTs. The choice is independent of opt level above `O1`.

## Performance-Critical Tunables

A handful of environment variables and `cl::opt` flags directly shift the cost model's behaviour. The complete inventory is in [Environment Variable and Runtime Gate Catalog](../reference/env-var-and-runtime-gate-catalog.md); the performance-relevant subset is:

| Tunable | Default | Effect on cost model |
|---|---|---|
| `TILE_AS_DEBUG_UNLIMITED_SMEM` | unset | raises the 227 KiB SMEM ceiling at capacity pool index 5 to `INT_MAX`; used to isolate whether a placement failed on SMEM pressure or on a different resource |
| `TILEIR_PREFER_TMA_FOR_LOAD_STORE` | `"false"` | when `"true"`, biases the layout selector toward `cp.async.bulk.tensor` atoms whose register-pressure cost is zero |
| `TILEIR_ALWAYS_SWIZZLE` | unset | forces the swizzled layout regardless of the layer-3 cost score — diagnostic only |
| `TILEIR_DELAY_TMA_STORE_WAIT` | unset | defers the `cp.async.bulk.wait_group` barrier after a TMA store, raising effective bandwidth at the cost of correctness margin |
| `--max-chain-length` (default 64) | 64 | caps the IDPA chain length — longer chains let the LLVM-tier optimizer fuse more FMA-style operations but raise compile-time cost |
| `--do-base-address-strength-reduce` (default 4) | 4 | BASR master, 0..4; higher levels enable more aggressive base-address strength reduction at the LLVM tier |
| `--scev-cgp-inst-limit` (default 500) | 500 | caps the SCEV-CGP instruction budget — a higher limit lets the SCEV-driven code-generation prepare more aggressively but extends compile time linearly |
| `--rrt-size-threshold` (warp-spec) | varies | switches between light and heavy warp-specialisation variants based on RRT size |

`TILE_AS_DEBUG_UNLIMITED_SMEM` is the most surgical diagnostic switch — it isolates whether a placement failed because of SMEM byte pressure (pool 5) or because of a different resource. Setting it temporarily and rerunning the same kernel will produce identical schedules if SMEM was not the binding constraint, and a different schedule if it was.

## Performance Gotchas

Five anti-patterns appear repeatedly in kernels that schedule worse than expected.

**Register pressure spilling to LMEM.** The layout selector's layer-3 register-pressure term scores live registers across the atom's window, but it does not see the whole-kernel register budget. A kernel that picks low-cost `ldmatrix.sync` atoms throughout (each pays 32–64 register fragments) can accumulate live ranges that exceed the SM's 64 KiB register file, forcing spills to local memory. The spills emit `st.local` and `ld.local` instructions that the NVPTX backend cannot eliminate. The fix is to bias the layout selector with `TILEIR_PREFER_TMA_FOR_LOAD_STORE=true` so memory-resident atoms (zero register cost) win the cost score for the largest tiles.

**SMEM bank conflicts.** The layer-3 SMEM cost term counts conflict-free transactions for the chosen swizzle, but the scorer cannot see across pipeline alias groups. Two independent groups can each pick a low-conflict swizzle internally and still collide at the cycle level if their footprints overlap on the same banks. The modulo scheduler's component-3 bank-pressure term catches the collision after the fact, but the only fix is to rerun the layout selector with a different swizzle hint or to raise the alignment on the offending operand.

**Sync overhead.** Named-barrier slots are capped at 3 across iterations (pool index 6, cross-iteration carry). A kernel that uses `bar.sync.named` for every producer-consumer handoff exhausts the cap at two-stage pipelines and the cost-based generator drops back to the trivial fallback. The fix is to coalesce barriers — multiple producer-consumer pairs can share one named barrier when their handoff cycles align.

**Pipeline staging miscount.** The roofline formula `stage_count = ceil(memory_cycles / compute_cycles)` assumes both numerator and denominator are dominated by the heaviest op. A loop body with a long-latency `cvta.to.global` or an unfolded address computation can extend the compute denominator without contributing usefully to throughput, lowering the apparent ratio and the chosen stage count. The fix is to inspect the per-op latency table charges at [Blackwell Pipeline 15-Slot Model — Per-Op Latency Table](../scheduler/blackwell-pipeline-15-slot-model.md#per-op-latency-table) and check whether non-load ops are inflating the compute side.

**TMEM column over-allocation.** The tcgen05 allocator partitions 256 columns × 128 rows of tensor memory per SM. A kernel that allocates wide accumulators (e.g., `tensor<256x256xf32>` split across two CTAs) can consume more columns than the per-SM budget when the CTAs co-reside, and the modulo scheduler rejects the placement at capacity pool index 1 (TMEM bank pressure, cap = 4). The fix is either to lower the accumulator precision (FP16 instead of FP32) or to split the kernel into separate CTAs that do not co-reside.

## Performance-Analysis Workflow

Performance debugging in Tileiras follows a four-step trace from the highest-level scheduler decisions down to the assembled PTX.

**Step 1: dump the schedule.** Setting `--schedule-trace-file=<path>` writes the per-op stage and order assignments, the chosen `II`, and the placement-arm sequence that produced each placement. The trace also records the cost vector for the cost-based fallback when it runs, which surfaces the dominant cost component. A kernel that fails to schedule emits a diagnostic naming the binding constraint — the slot, the pool, or the cycle ceiling.

**Step 2: inspect the snapshot IR.** Both `O2` and `O3` provide an optional snapshot printer between the heaviest lowering and CSE. The snapshot is the natural inspection window for layout decisions — every `tiled_load` and `tiled_store` carries its assigned `nv_tileas.layout` attribute, and a layout-induced bank conflict is visible as a swizzle that does not match the surrounding pattern. The snapshot also shows which pipe values the scheduler emitted; a missing `Pipe_` between an expected producer-consumer pair indicates that `Schedule::solve` fell back to the trivial zero-producer path.

**Step 3: read the PTX.** The NVPTX backend writes state-space-qualified memory instructions (`ld.global`, `ld.shared`, `ld.tmem` via `tcgen05.ld`). A kernel that schedules well at the MLIR level can still emit suboptimal PTX if the AsmPrinter chose a wide instruction where a narrow one would suffice, or if address-space promotion left a `cvta.to.global` that better refinement would have eliminated. The PTX text is also where the launch-bound directives surface — `.maxntid` and `.reqntid` collisions, parameter-space size, register count.

**Step 4: profile with `nvprof` or Nsight Compute.** Once the PTX is in cubin form, runtime profiling closes the loop. The metrics that map directly back to Tileiras's cost terms are `smsp__inst_executed_pipe_tensor_op_hmma.sum` (TC issue throughput, component 2 in the scheduler's vector), `l1tex__data_bank_conflicts_pipe_lsu.sum` (SMEM bank conflicts, component 3), `smsp__inst_executed_pipe_lsu.sum` divided by `smsp__cycles_active.avg` (transport pressure, component 1's RRT row-OR test), and `sm__warps_active.avg.pct_of_peak_sustained_active` (occupancy, the ratio that drives the stage-count ceiling). A mismatch between predicted and measured throughput points to the cost-model term that was wrong — usually a latency the per-op table charged but the hardware did not, or a bank conflict the layer-3 scorer missed.

## Cross-References

[Pass List by Optimization Level](../pipeline/full-pass-list-by-opt-level.md) documents which passes each level runs and the IR shape at every stage boundary. [Schedule Solve and Cost Evaluators](../scheduler/schedule-solve-and-cost-evaluators.md) walks the four-component cost vector through a concrete scoring example. [Blackwell Pipeline 15-Slot Model](../scheduler/blackwell-pipeline-15-slot-model.md) documents the slot vocabulary, the latency families, and the capacity pools the cost model reads. [TileAS Layout and Buffer Family](../passes/tileas/layout-and-buffer-family.md) documents the three-term layout selector cost. [Environment Variable and Runtime Gate Catalog](../reference/env-var-and-runtime-gate-catalog.md) lists every tunable that shifts the model.
