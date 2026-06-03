# MXU Assignment Bin-Packer

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped — `nm -C` resolves every symbol below). `.text`/`.rodata` VMA == file offset; `.data.rel.ro` VMA − 0x200000 == file offset. The decompile source-line strings cite `platforms/xla/service/jellyfish/mxu_latency_balancing.cc`. Other versions differ.*

## Abstract

`AssignMxusForSequenceGroup` is the pass that decides **which physical MXU quadrant each matmul sequence-group runs on**. A jellyfish TensorCore has up to four physical MXU instances; a single fused HLO produces many [`MxuSequence`](mxu-sequence-struct.md) latch/matmul chains, and they must be spread across those four units so that no one unit becomes the throughput bottleneck. This pass is a **greedy min-makespan bin-packer**: it walks the sequences in order, scores each candidate MXU by how many extra busy cycles adding the sequence would cost that unit, assigns the sequence to the unit with the smallest resulting maximum-latency, and then runs a rebalance phase that hill-climbs by moving work off the most-loaded unit until the loads converge on the balanced target.

The familiar reference frame is LLVM's `MachineScheduler` resource-aware list scheduler, but specialised to one functional unit and run as a *placement* pass rather than an *ordering* pass. Where the list scheduler advances an issue cursor and tracks `ProcResource` counters, this pass keeps one `MxuStat` per physical MXU — a running occupancy timeline plus an ordered map of the sequences placed there — and prices each placement with the same interval-extension arithmetic a packetizer uses to test whether a new op fits a hazard slot. The cost the bin-packer minimises is *makespan*: the latency of the busiest MXU, summed over the structural stalls that [`MxuLatencyTable::GetLatencyBetween`](../cost/mxu-latency-overview.md) and [`XluConflictPenaltyTable`](../cost/xlu-conflict-penalty.md) price between adjacent ops.

The pass has two modes selected by one environment flag. The shipping default is the **flat** mode: all sequences are one group, balanced across all four MXUs at once. The opt-in **latency-balanced** mode sub-groups sequences by the matrix-result-buffer (MRB) address they write, building an `LloDependencyGraph` so the makespan accounts for the inter-sequence MRB-accumulate ordering, then balances each MRB group independently. Both modes converge on the same `AssignMxusForSequenceGroupInternal` bin-packer.

For reimplementation, the contract is:

- The public dispatcher `AssignMxusForSequenceGroup`: the env-flag gate (`MxuLatencyBalancingUseSequenceDependencies`), the flat path (copy span → one Internal call), and the MRB-keyed `linked_hash_map` grouping path (per-group Internal call).
- The bin-packer `AssignMxusForSequenceGroupInternal`: the `vector<MxuStat>` of `num_mxus` units, the greedy select loop (PASS 1), and the iterating rebalance loop (PASS 2).
- The two cost functions: `MxuStat::LatchLatencyChangeAfterAdding` (the `c + x − y2` interval-extension delta) and `MxuStat::LatencyChangeIfMoveTo` (the move-makespan delta).
- The interaction with the non-MXU hazard model: how the per-sequence base latency that the packer sums folds in the `XluConflictPenaltyTable` cross-lane stalls through the shared `LatencyBetween` edge model.

| | |
|---|---|
| **Public dispatcher** | `xla::jellyfish::AssignMxusForSequenceGroup` @ `0x10f753c0` |
| **Bin-packer (Internal)** | `xla::jellyfish::(anon)::AssignMxusForSequenceGroupInternal` @ `0x10f77ca0` |
| **Env gate** | `MxuLatencyBalancingUseSequenceDependencies` @ `0x1d6b9c80` (env field `+0xbe8`, `AutoOr<bool>`, default OFF) |
| **PASS-1 cost** | `MxuStat::LatchLatencyChangeAfterAdding(int, long, long)` @ `0x10f7f3e0` |
| **PASS-2 cost** | `MxuStat::LatencyChangeIfMoveTo(int, MxuStat const&, long)` @ `0x10f7fb40` |
| **Rebalance callback** | `…Internal::$_1::operator()(long)` @ `0x10f7db60` |
| **Per-seq base latency** | `xla::jellyfish::CycleTableInstruction(LloInstruction*)` @ `0x1c89ca80` |
| **MRB sub-group size** | `xla::jellyfish::ExpectedMatresesPerMatmul(LloInstruction*)` @ `0x145005e0` |
| **Result container** | `InlinedVector<MxuAssignment, 4>` (4 = physical MXU quadrants) |
| **Source file** | `platforms/xla/service/jellyfish/mxu_latency_balancing.cc` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Public Dispatcher

`AssignMxusForSequenceGroup(int num_mxus, Span<unique_ptr<MxuSequence>> sequences, CycleTable const&, optional<MapView<LloInstruction*, long>>)` @ `0x10f753c0` is the entry point. `num_mxus` arrives as `arg1` — the caller ([`MxuAssigner::LatchLhs`](mrb-fifo-msr-placement.md) / `AccumulateIntoMrb`) resolves it from the `Target` MXU-count getter (vtable `+0x300`, the per-gen physical quadrant count, four on jellyfish). The dispatcher front-loads two `CHECK`s, then branches on the env flag:

```c
double AssignMxusForSequenceGroup(int num_mxus, MxuSequence** seqs, size_t n, ...):  // sub_10F753C0
    CHECK(n > 0);                              // mxu_sequences.size() > 0      (line 987)
    CHECK(seqs[0]->matmuls.size() > 0);        // front()->matmuls.size() > 0   (line 988)
    if (!MxuLatencyBalancingUseSequenceDependencies(env)):     // DEFAULT path, flag OFF
        // flatten the Span<unique_ptr<MxuSequence>> into a mutable MxuSequence*[]
        MxuSequence** flat = new MxuSequence*[n];
        for (i = 0; i < n; ++i) flat[i] = seqs[i].get();
        AssignMxusForSequenceGroupInternal(num_mxus, flat, n, cycle_table, ...);   // ONE flat group
        free(flat);
    else:                                       // latency-balanced path, flag ON
        … build per-MRB-address sub-groups (below) …
```

> **NOTE —** the function's IDA return type is `double` and the makespan tracking uses XMM scratch, but the result the caller consumes is the per-sequence MXU id committed into each `MxuSequence` (via `set_mrb_address_unrestricted` and the `MxuStat::SequenceInfo` map), not the return register. The `double` is a decompiler artifact of the shared XMM spill slots; the pass is fundamentally `void`-with-side-effects.

### The env gate

`MxuLatencyBalancingUseSequenceDependencies` @ `0x1d6b9c80` reads the `AutoOr<bool>` at `TpuCompilationEnvironment + 0xbe8` (`+3048`), falling back to the global default proto when the per-compilation field is null, and resolves it with `AutoOr<bool>::FromProtoOrDie`:

```c
bool MxuLatencyBalancingUseSequenceDependencies(env):           // sub_1D6B9C80
    p = *(AutoProto**)(env + 3048);
    if (!p) p = &AutoProto_globals_;            // fall back to default proto
    v = AutoOr<bool>::FromProtoOrDie(p);
    return (~v & 0x101) == 0;                    // true only when both AUTO+VALUE bits agree
```

The `(~v & 0x101) == 0` test is the standard `AutoOr` "explicitly-true" idiom — the flag is an `AUTO`-defaulted tri-state, and on v0.0.40 it resolves OFF (see [TpuCompilationEnvironment](../config/tpu-compilation-environment.md) and [Environment Variables](../config/env-vars.md)). So the **shipping** path is the flat single-group bin-pack; the MRB sub-grouping is opt-in.

### Flat path — one group

When the flag is OFF, the dispatcher copies the `Span<unique_ptr<MxuSequence>>` into a heap `MxuSequence*[]` (a growing-vector copy in the decompile, `operator new` / `memcpy` / `free`), calls the bin-packer once over the whole list, and frees the temporary. Every sequence competes for every MXU; the packer balances all of them simultaneously across the four units.

### Latency-balanced path — per-MRB sub-groups

When the flag is ON, the dispatcher groups sequences by the **matrix-result-buffer address** they write, so that sequences accumulating into the same MRB chunk are balanced together (and end up co-located on one MXU, preserving their accumulate ordering). The grouping is an `absl::linked_hash_map<LloInstruction*, linked_hash_set<MxuSequence*>>` built by `lazy_emplace` keyed on `LloInstruction::mrb_address()`:

```c
// balanced path (flag ON)
linked_hash_map<LloInstruction*, ...> groups;
mrb = 0;
for (seq : sequences):
    for (matmul : seq->matmuls):
        addr = matmul->mrb_address();
        groups.lazy_emplace(addr);                       // sub_10F755E0
        matmul->set_mrb_address_unrestricted(mrb);
        // size the MRB chunk this matmul consumes:
        k = LloInstructionPushesToResultFifo(matmul);
        if (num_result_fifos() > 0):
            mpm = matreses_per_matmul();                 // vtable +0x5e0
            k = mpm * ceil_div(k, mpm);                  // round up to a full matres group
        mrb += k;
    … for each matres of the seq, re-key by mrb_address and bump mrb by ExpectedMatresesPerMatmul …
// then, per MRB-address group:
AssignMxusForSequenceGroupInternal(num_mxus, group_seqs, n, cycle_table, dep_view);
```

This path additionally constructs an `LloDependencyGraph` (`LloDependencyGraph::Create` over the region's flattened LLO, `AddNode` per non-trivial op) and a `LatencyTable` so the per-group balance can price the inter-sequence dependency edges; the `MapView<LloInstruction*, long>` optional argument carries that dependency-cycle view into the bin-packer. The `ExpectedMatresesPerMatmul` @ `0x145005e0` getter (matmul data format + `Target[+0x5f8]` matreses-per-matmul) sizes each MRB sub-group, and a `CHECK(data->sequence == incoming_data->sequence)` ("vdwg blocking two sequences, which is not expected", `mxu_latency_balancing.cc:550`) guards the `dwg` (double-weight-gate) cross-sequence aliasing.

> **GOTCHA —** distinct MRB-address groups are balanced **independently**, each in its own `Internal` call. Two sequences in *different* MRB groups can both land on MXU 0 even when that over-subscribes it relative to MXU 1, because no single balance sees both groups. The flat path does not have this blind spot — which is part of why it is the default. A reimplementation choosing the ON path must accept per-group-local optimality, not global.

---

## The Bin-Packer — `AssignMxusForSequenceGroupInternal`

`AssignMxusForSequenceGroupInternal(int num_mxus, Span<MxuSequence*>, CycleTable const&, optional<MapView<…>>)` @ `0x10f77ca0` (anonymous namespace) is the core. Both dispatcher paths funnel here.

### State — `vector<MxuStat>`

The packer allocates a `vector<MxuStat>` of `num_mxus` entries. Each `MxuStat` (the decompile shows a 40-byte stride per entry in the select loop, `5` qwords) tracks two things:

- a **running per-MXU latency** (the accumulated makespan of that unit — the `long` the select loop reads at the entry's head and the rebalance loop mutates), and
- an ordered **`btree_map<int, MxuStat::SequenceInfo>`** of the sequences placed on that MXU, keyed by latch/sequence index (the `absl::container_internal::btree<map_params_impl<int, MxuStat::SequenceInfo>>` that appears throughout the cost functions and the merge/rebalance node ops).

A `+0xb` "valid" byte and a `+0xa` "node count" byte gate the btree-node walk in the cost functions. The final placement is materialised as an `InlinedVector<MxuAssignment, 4>` — the inline capacity **4** is the physical MXU-quadrant count baked into the type (`…MxuAssignment, 4ul, …` in the `EmplaceBackSlow` instantiation), matching the `std::array<MxuState, 4>` used by `CollectAndTransformSequencesPerMxu`.

> **NOTE —** the `MxuStat` field roles (which `long` is the running latency vs the accumulated busy interval, the exact `SequenceInfo` body) are reconstructed FUNCTIONALLY from the access pattern in the two cost functions and the btree node math, not from a source layout. The cost arithmetic below is byte-exact; the field naming is INFERRED. (Confidence: HIGH for the arithmetic, INFERRED for the field labels.)

### PASS 1 — greedy assignment

The packer walks sequences in order. The per-sequence base cost is the [CycleTable](../cost/cycletable-family.md) instruction latency `CycleTableInstruction(seq)` @ `0x1c89ca80`. For each sequence it scores every MXU and keeps the one that yields the smallest resulting makespan. The select loop (`@0x10f784d0` region, decompile lines ~929–954) is byte-exact:

```c
// PASS 1: for each MxuSequence S (dependency order), base = CycleTableInstruction(S)
best_max  = INT64_MAX;        // v78 — running minimum of the candidate makespans
best_mxu  = -1;               // v76/v84 — chosen MXU index
found     = 0;               // v75  — set when a strictly-better MXU is seen
for (idx = 0; idx < num_mxus; ++idx):
    delta = mxus[idx].LatchLatencyChangeAfterAdding(idx, S.latch_ptr, S.latch_latency, base);  // sub_10F7F3E0
    cand  = running_latency_base(idx) + delta + mxus[idx].head_latency;   // v82 + *(entry-3)
    if (best_max <= cand) best_mxu = idx;        // cmovle — tie keeps the lower idx
    if (best_max >  cand) found = 1;             // a strictly smaller candidate exists
    if (best_max >= cand) best_max = cand;       // cmovge — track the minimum makespan
assign S → best_mxu;
mxus[best_mxu].insert(S);     // SequenceInfo into the btree, bump running latency
```

The key is that the candidate `cand` is the makespan *if* `S` were added to MXU `idx`: the unit's current running latency plus the marginal busy-cycle increase `delta`. The picker minimises that, so the sequence lands on the unit that grows the least — the textbook greedy min-makespan placement (LPT/list-scheduling variant). The `cmovle`/`cmovge` pair tracks the minimum and the chosen index simultaneously; ties keep the first (lower-index) MXU.

### PASS 1 cost — `LatchLatencyChangeAfterAdding`

`MxuStat::LatchLatencyChangeAfterAdding(int idx, long latch_ptr, int latch_latency, long base)` @ `0x10f7f3e0` returns the marginal busy-cycle increase of inserting the new latch/matmul into this MXU's time-sorted sequence map. It locates the insertion neighbour in the `btree<int, SequenceInfo>` (the `a3 < *v7` walk over the 18-`DWORD`/72-byte node stride), reads the neighbour's end/free/busy timestamps, and computes a clamped interval extension. The tail (decompile lines 167–177) is byte-exact:

```c
long LatchLatencyChangeAfterAdding(int idx, long latch_ptr, int new_start, long latch_latency):  // sub_10F7F3E0
    // locate the sorted-by-time neighbour of `new_start` in sequences_ (btree walk);
    // CHECK(latch_latency == prev_it->second.latch_latency)   if exact key match  (line 236)
    //   free  = neighbour.free_time     (v15)
    //   busy  = neighbour.busy_until    (v10)
    //   start = base / latch_latency arg (a4)
    c  = max(0, a4  - free);             // a4 − v15, clamp at 0   → v21
    x  = max(0, busy - start);           // v10 − a5, clamp at 0   → v22
    y2 = max(0, busy - free);            // v10 − v15, clamp at 0  → v23
    return c + x - y2;                   // interval extension
```

`c + x − y2` is the number of extra cycles the MXU's occupied interval grows when the new op is spliced into its slot: `c` is how far the new op pushes past the prior free point, `x` is the overlap of the next-busy interval with the new start, and `−y2` removes the interval that was already counted. This is the increase-in-makespan-delta the greedy picker adds to the unit's running latency to form `cand`.

### PASS 2 — iterating rebalance

After the greedy pass, an outer loop (decompile `while(1)` @ line 1133) hill-climbs toward a balanced load. Each iteration:

1. computes the **balance target** `ceil(total_latency / num_mxus)` (lines 1136–1160),
2. scans all `num_mxus` units to find the **least-loaded** (`min`) and **most-loaded** (`max`) MXU (lines 1174–1196),
3. if the most-loaded unit is already at the target, **stops** (`LABEL_378`, line 1203),
4. otherwise picks a sequence to move off the max unit onto the min unit, scores the move, and commits it if it lowers the makespan.

The move score uses `MxuStat::LatencyChangeIfMoveTo`; the commit mutates both units' running latency (decompile line 1530–1531: `*max_mxu -= LatencyChangeIfMoveTo(...)` then `*min_mxu += delta`) and re-keys the moved sequence in both btrees. A VLOG at verbosity ≥2 traces every commit:

```text
mxu_latency_balancing.cc:686   "max mxu latency: <N> on mxu<M>"
mxu_latency_balancing.cc:758   "DECISION: moving <N> matmuls on sequence <S> from <A> to <B>"
```

> **CORRECTION (MXU-BINPACK-1) —** an earlier synthesis described PASS 2 as a *single swap pass* with "iterate-to-fixpoint INFERRED". The decompile is unambiguous: PASS 2 is an **outer loop that recomputes the min/max MXU and the `ceil(total/num_mxus)` target each iteration** and runs until the most-loaded unit reaches the balance target (the `LABEL_378` exit). It is a real iterating rebalance, not a single pass. (Confidence: CONFIRMED.)

### PASS 2 cost — `LatencyChangeIfMoveTo`

`MxuStat::LatencyChangeIfMoveTo(int seq_key, MxuStat const& src, long n_matmuls)` @ `0x10f7fb40` returns the makespan delta of moving a sequence to *this* (target) MXU. It locates the sequence in the target's `sequences_` btree (`CHECK(it != sequences_.end())`, `mxu_latency_balancing.cc:267`), re-prices the moved matmuls by summing their per-instruction CycleTable latency through a vtable call (`*(_QWORD*)(reg+16)`, the CycleTable instruction lookup, line 218), and folds that into the same clamped interval-extension arithmetic as PASS 1, finally calling `LatchLatencyChangeAfterAdding` on the source unit (line 254) to account for the vacated slot. The two-branch tail (lines 232–253):

```c
long LatencyChangeIfMoveTo(int seq_key, MxuStat const& target, long n):   // sub_10F7FB40
    // gather the moved matmuls' CycleTable latencies → sum `v29`
    // free=v45, busy=v46, prev_free=v48, next_busy_after=v47 from the btree neighbours
    g = max(0, next_busy_after - free);                  // v35
    if (sum == free):                                    // v29 == v45
        a = max(0, busy - prev_free);                    // v37
        b = max(0, next_busy_after - prev_free);         // v38
        delta = free + g + a - b;                        // v39
    else:
        delta = (sum + g) - max(0, sum + next_busy_after - free);   // v40 − v41
    LatchLatencyChangeAfterAdding(target.head, target.head_ptr, seq_key, busy, sum);  // re-price source
    return delta;
```

The `max(0, …)` clamps mirror PASS 1's `c + x − y2` shape; the per-matmul re-summation is what lets a *multi-matmul* sequence be moved as a unit and re-priced against the destination's timeline rather than just translated.

---

## What the Bin-Packer Produces and Consumes

The output is one physical MXU id per `MxuSequence`, recorded in the `MxuStat::SequenceInfo` btrees and committed into the sequences (the `set_mrb_address_unrestricted` writes on the balanced path; the `MxuAssignment` inlined-vector on the flat path). This id is the input to the downstream [MRB Chain Allocator](mrb-chain-allocator.md) and [MRB FIFO / MSR Placement](mrb-fifo-msr-placement.md), which turn the per-MXU sequence list into concrete matrix-result-buffer chunks and MSR latch addresses; the [LLO Bundle Packing](llo-bundle-packing.md) pass then schedules the latch/matpush/matres ops whose ordering depends on this placement, and [Latch Assignment / Overrun](latch-assignment-overrun.md) resolves the per-MXU latch slot conflicts the placement implies.

The latency the packer sums — both the per-sequence `CycleTableInstruction` base and, on the ON path, the dependency-edge weights from the `MapView` — is produced by the shared `LatencyBetween` edge model. That model is a three-way dispatch: a true-dependency edge charges the base op latency; an MXU∧MXU edge charges the [`MxuLatencyTable::GetLatencyBetween`](../cost/mxu-latency-overview.md) reservation recurrence; and a non-MXU cross-lane edge charges the [`XluConflictPenaltyTable`](../cost/xlu-conflict-penalty.md) structural hazard.

### How the XLU conflict penalty interacts with the placement

The bin-packer does not read the `XluConflictPenaltyTable` directly. The interaction is **indirect but real**: when two cross-lane (reduce / transpose / permute) ops issue back-to-back on the **same MXU instance** without a true data dependency, `XluConflictPenaltyTable` prices the cross-lane FIFO-drain stall as a `MAX`-reduced edge latency inside `LatencyBetweenInternal`. That stall inflates the per-sequence latency the packer sums, and — critically — the penalty is **per-MXU-instance** (the table's third index is `mxu_id & 3`, and the value reader `CHECK`s both ops are on the same MXU). So the placement the packer chooses determines *whether two conflicting cross-lane ops even share an MXU*:

- If the packer puts two XLU-conflicting sequences on the **same** MXU, their `LatencyBetween` edge carries the full `XluConflictPenaltyTable` stall (e.g. on Viperfish a `kTransposeB32 → kReduceB32` conflict is up to 105 cycles — a full XLU drain), inflating that unit's makespan.
- If it spreads them across **different** MXUs, the same-MXU `CHECK` short-circuits — there is no cross-instance XLU conflict in this model — and neither sequence pays the penalty.

The min-makespan objective therefore implicitly *avoids co-locating XLU-conflicting sequences*: putting them together raises the candidate makespan via the inflated `LatencyBetween` weight, so the greedy picker and the rebalance pass both prefer to separate them. This is the same effect the MXU reservation matrix has for matmul-on-matmul structural hazards — the bin-packer respects both hazard tables by virtue of summing the latencies they price, never by querying them itself.

> **NOTE —** the bin-packer's awareness of the XLU (and MXU) hazard tables is only as good as the latency view it is handed. On the **flat** (default) path the per-sequence cost is the bare `CycleTableInstruction` base — the inter-sequence `LatencyBetween` edges (including XLU conflicts) are **not** summed into the placement, because the flat path passes no dependency `MapView`. Only the **ON** (MRB-grouped) path builds the `LloDependencyGraph` whose edges carry the `XluConflictPenaltyTable` / `MxuLatencyTable` stalls into the makespan. So the hazard-aware placement described above is the ON-path behaviour; the default path balances on raw per-sequence latency and relies on the downstream bundle packer to absorb the residual conflicts. (Confidence: CONFIRMED — the flat path's single `Internal` call passes the optional `MapView` empty.)

---

## Worked Example

Six independent bf16 matmul sequences, four MXUs, each sequence base ≈ 212 cycles (the per-opcode matmul latency from the [per-opcode cycle constants](../cost/per-opcode-cycle-constants.md)).

**PASS 1 (greedy).** Sequences are placed one at a time. The first four go to MXUs 0–3 (each empty unit ties at `cand = 212`; ties keep the lowest index, but each subsequent empty unit is strictly smaller, so they fill 0,1,2,3). The fifth scores all four units at `cand ≈ 2·212 = 424` and lands on MXU 0 (first minimum). The sixth lands on MXU 1. Final loads: `{2, 2, 1, 1}` sequences → makespan ≈ 424 cycles on the two loaded units.

**PASS 2 (rebalance).** The balance target is `ceil(6·212 / 4) = 318`. The most-loaded unit (MXU 0 at 424) exceeds it, but moving one of its two sequences to a one-sequence unit (212 → 424) does not lower the *max* (it would just swap which unit is at 424), so `LatencyChangeIfMoveTo` reports no improvement and the loop reaches the `LABEL_378` "already at target / no improving move" exit. Final placement `{2, 2, 1, 1}`.

**XLU-conflict variant.** Suppose two of the six sequences each end with a transpose-B32 op feeding a cross-lane reduce-B32 op (a `kTransposeB32 → kReduceB32` XLU conflict). On the **ON** path the `LloDependencyGraph` edge between them carries the `XluConflictPenaltyTable` Viperfish penalty (88–105 cycles depending on the MXU instance). If the packer were to place both on MXU 0, that unit's makespan would jump by ~100 cycles; scoring instead places each on a different MXU, where the same-MXU `CHECK` short-circuits and neither pays the stall. The min-makespan objective separates them automatically.

---

## Cross-References

- [MxuSequence / SequenceInfo](mxu-sequence-struct.md) — the `MxuSequence` record (latch + matmuls + matreses) the bin-packer places, and the `set_mxu` commit that records the chosen MXU id.
- [MRB Chain Allocator](mrb-chain-allocator.md) — the next pass down: turns the per-MXU sequence lists into matrix-result-buffer accumulation chains using the MXU ids this pass assigns.
- [MRB FIFO / MSR Placement](mrb-fifo-msr-placement.md) — `MxuAssigner::LatchLhs` / `AccumulateIntoMrb`, the callers that resolve `num_mxus` from the `Target` and consume the assignment into concrete FIFO/MSR addresses.
- [LLO Bundle Packing](llo-bundle-packing.md) — the forward bundle scheduler whose latch ordering depends on this placement.
- [Latch Assignment / Overrun](latch-assignment-overrun.md) — the per-MXU latch-slot conflict resolution downstream of the assignment.
- [XLU Conflict-Penalty Table](../cost/xlu-conflict-penalty.md) — the per-(XluInstrType, XluInstrType, vxpose) cross-lane structural-hazard matrix whose stalls inflate the makespan this pass minimises; the non-MXU hazard the placement implicitly respects.
- [MXU Latency Overview](../cost/mxu-latency-overview.md) — `MxuLatencyTable::GetLatencyBetween`, the MXU-on-MXU reservation recurrence that prices the structural stalls between matmul pushes the packer sums.
- [Transpose-Reservation Latency](../cost/xpose-reservation-latency.md) — `XposeXLUReservationLatency`, the dynamic height/width transpose-reservation path the XLU conflict reader can route to.
- [CycleTable Family](../cost/cycletable-family.md) — `CycleTableInstruction`, the per-sequence base latency the bin-packer sums.
- [MXU Slot](../isa/slot-mxu.md) — the latch / matpush / matmul / matres instruction family that makes up an `MxuSequence`.
- [MXU Op Hold-Issues Stall](../cost/mxu-opholdissues-stall.md) — how the per-MXU reservation occupancy is consumed by the issue-stall logic.
- [TPU Scheduling Pipeline](overview.md) — where this MXU/MRB placement pass sits between the HLO latency-hiding scheduler and the LLO bundle packer.
- [TpuCompilationEnvironment](../config/tpu-compilation-environment.md) · [Environment Variables](../config/env-vars.md) — the `MxuLatencyBalancing` env field (`+0xbe8`) that gates flat vs MRB-grouped balancing.
