# XLU Combine and Source-Bus Assignment

> *Every opcode, address, offset, bit position, and immediate on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped — full C++ symbols, `nm -C` resolves every method). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA − 0x200000 = file offset. Other libtpu builds will differ.*

## Abstract

This page is the **cost half** of `xla::jellyfish::LloXluGraphOptimizer` — the four passes that *decide* placement and the latency model they all price against. The companion [XLU Op Roster](../isa/xlu-op-roster.md) catalogs the opcodes and walks the full five-stage pipeline at a higher altitude; this page drills into the three scheduling decisions that read the cost — `ComputeCombinablePairs`, `AssignXlu`/`AssignSourceBus`, `ReorderToShortenCriticalPath` — plus the edge-weight model (`LatencyTable::Create` → `PreXluAssignmentLatencyTable`) that turns a per-generation latency table into the XLU op-graph's dependency-edge weights.

The XLU (Cross-Lane Unit) is a scarce multi-cycle TensorCore engine issued from the same `VectorExtended` bundle slot as the MXU. Before lowering the cross-lane ops into a bundle, the optimizer rewrites their dependency graph: it **fuses** identical adjacent ops (`ComputeCombinablePairs`), **bin-packs** the survivors across the per-generation XLU units by least cumulative cost (`AssignXlu`), **reorders** them with a latency-weighted list scheduler to shorten each unit's critical path (`ReorderToShortenCriticalPath`), and on Pufferfish (v4) **routes** their operands onto the V0/V1/V2/V3 source buses (`AssignSourceBus`). If you know LLVM's modulo scheduler and the SelectionDAG critical-path heuristics, this is a small, special-purpose analog over a single resource type: the "priority" is marginal latency, the "machine model" is a per-(op,op) `LatencyBetween` table, and the central trick is that XLU↔XLU edge latency is **divided by the XLU count** — the parallelism discount that makes a chain of cross-lane reduces cheaper the more XLUs the generation has.

The page documents, in order: the latency edge model (`LatencyTable::Create` registry dispatch + the `ceil(base / xlu_count)` `PreXluAssignmentLatencyTable` wrapper) because every other pass reads it; the combining predicate; the greedy unit assignment; the reorder list scheduler; and the source-bus allocator with its shared-bus serialization hazard. The closed-form marginal-cost function `CyclesAddedByXluOperation` and the per-XLU `PerXluOperations` state struct are documented in [XLU Reemit Cost](xlu-reemit-cost.md) and referenced here.

For reimplementation, the contract is:

- The XLU edge weight: `IsXluOp(from) && IsXluOp(to) ? ceil(base_LatencyBetween / xlu_count) : base_LatencyBetween`, and the `IsXluOp` opcode set.
- The combining predicate: equal metadata key + tracker-readiness + critical-path-bounded; control ops never fuse.
- `AssignXlu`: greedy least-loaded bin-pack by accumulated `CyclesAddedByXluOperation`; the unit index committed into `WORD[instr+0xb]` bits 8-10.
- `ReorderToShortenCriticalPath`: per-XLU max-heap keyed on marginal cost, `XluOperationIsReady` gate, per-XLU completion-clock critical-path test.
- `AssignSourceBus`: the `HasVexSourceBuses` gate, `SourceBusesForXlu(i) = {i, i+2}`, the greedy/explicit bus binding, and the shared-bus `UpdateEdge` serialization edge.

| | |
|---|---|
| **Optimizer** | `xla::jellyfish::LloXluGraphOptimizer::Optimize` @ `0x126cdb80` |
| **Combine** | `ComputeCombinablePairs` @ `0x126d2480` (sret `vector<pair<variant*,variant*>>`) |
| **Unit assign** | `AssignXlu` @ `0x126d3100` (greedy least-loaded; requires `xlu_count > 1`) |
| **Reorder** | `ReorderToShortenCriticalPath` @ `0x126d3460` (per-XLU max-heap list scheduler) |
| **Source bus** | `AssignSourceBus` @ `0x126d70e0` (Pufferfish-only in this build) |
| **Edge model** | `PreXluAssignmentLatencyTable::LatencyBetweenInternal` @ `0x126e0e40` — `ceil(base / xlu_count)` |
| **Base table** | `LatencyTable::Create(TpuVersion)` @ `0x1c89fba0` — registry `@0x225799f8` dispatch |
| **Cost fn** | `CyclesAddedByXluOperation` @ `0x126d22a0` (see [XLU Reemit Cost](xlu-reemit-cost.md)) |
| **XLU count** | `VectorIsa.xlu_count` = `DWORD[Target+0x4b0]` |
| **Bit-field** | `WORD[LloInstruction+0xb]`: unit `bits 8-9` (+valid `10`), bus `bits 11-12` (+valid `13`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Edge-Weight Model

### Purpose

Every placement decision in the four passes below resolves to a single question: *how many cycles does putting op B after op A on the same XLU cost?* That number is a dependency-graph edge weight, and the XLU optimizer does not read the per-generation latency table directly — it reads a thin wrapper, `PreXluAssignmentLatencyTable`, that applies the XLU parallelism discount. Document this first because it is the cost the combine DP, the unit-assignment min-cost pick, the reorder heap priority, and the source-bus critical-path depth all consume.

The cost model is two layers, mirroring the [CycleTable / Performance split](cycletable-family.md): a per-generation **base** `LatencyTable` chosen by registry dispatch, and the XLU optimizer's **wrapper** around it. The base answers "raw per-(op,op) latency"; the wrapper rewrites only the XLU↔XLU edges.

### The Base Table — LatencyTable::Create

`LatencyTable::Create(TpuVersion)` (`@0x1c89fba0`) is a **registry dispatch**, not an `if`-chain over versions. Byte-exact from the decompile:

```c
// LatencyTable* LatencyTable::Create(TpuVersion version)   // @0x1c89fba0
LatencyTable* Create(int version) {
    VLOG(2) << "Creating latency table for deepsea version: " << version; // latency_table.cc:119
    CHECK(registry != nullptr);                  // @0x225799f8 ; "no latency tables registered" :120
    CHECK(version >= 0);                          // :122
    CHECK(version < registry->size());            // :123  (size = registry_word >> 1)
    factory = (*registry)[version].factory;       // slot stride 0x20, factory at [slot+0x18]
    CHECK(factory != nullptr);                    // :124  "no latency table for " << version
    return factory(&(*registry)[version]);        // call rax — builds the per-gen subclass
}
```

The registry is the global `registry` `@0x225799f8`, a flat map keyed by `TpuVersion`; each slot is `0x20` bytes and holds its factory function pointer at `+0x18`. The returned object is a per-generation subclass — `LatencyTableJellyfish` for v2/v3 (the 15-field copy model), the heap `MxuLatencyTable` form for VF/GL. The `CrossXluOperationsDataDependencyTracker` stores this base table at `[tracker+0xf8]` and passes it (the `r8` argument) to its internal `LloDependencyGraph` constructor as the graph's edge-weight source.

> **NOTE —** `Create` performs **no** XLU-specific override. The tracker's raw graph edges are the standard per-generation latency model. The XLU discount is applied *on top*, by the wrapper below — so a reimplementer must keep the two tables distinct: the tracker holds the base, the optimizer holds the wrapper.

### The Wrapper — PreXluAssignmentLatencyTable

`LloXluGraphOptimizer::Optimize` builds a `PreXluAssignmentLatencyTable` on the heap (`new`, sizeof `0x28`) right after `AdjustEdgesBeforeXluAssignment`. Its layout, byte-exact:

| offset | field | meaning |
|---|---|---|
| `+0x00` | vptr | `PreXluAssignmentLatencyTable` vtable (`@0x218e0950`) |
| `+0x08` | `i32` | `TpuVersion` (the base `LatencyTable` field) |
| `+0x10` | ptr | `0` (the base lazy buffer; freed in dtor) |
| `+0x18` | `LatencyTable*` | the **delegate** — the optimizer's previous table (the per-gen base) |
| `+0x20` | `i32` | the **divisor** = `xlu_count` = `DWORD[Target+0x4b0]` |

Its `LatencyBetweenInternal(from, to)` (`@0x126e0e40`) is the XLU edge weight. Byte-exact from the decompile (which checks `from.op` in a switch, then `to.op`):

```c
// PreXluAssignmentLatencyTable::LatencyBetweenInternal(LloValue* from, LloValue* to)  @0x126e0e40
long LatencyBetweenInternal(LloValue* from, LloValue* to) {
    if (IsXluOp(*(u16*)from) && IsXluOp(*(u16*)to)) {
        int raw = delegate->LatencyBetween(from, to);   // [this+0x18] base @0x1c89f820
        int div = xlu_count;                             // [this+0x20]
        return ceil_div(raw, div);                       // see ceil arithmetic below
    }
    return delegate->LatencyBetween(from, to);           // pass-through, all non-XLU edges
}
```

`IsXluOp(op)` is the union of two opcode bands, both directly visible as the `case` labels:

```c
IsXluOp(op) =
    op ∈ {0x8b, 0x8c, 0xa6, 0xa7, 0xf5..0x101, 0x14f, 0x150, 0x154, 0x155}   // 21 XLU opcodes (switch cases)
  | (op <= 0x3b && _bittest64(0x0C40000000000000, op))                        // = {0x32, 0x36, 0x37, 0x3a, 0x3b}
```

The 21 opcodes are the cross-lane reduce / permute / transpose family; the bit-mask band adds `kVectorPermute`/`kVectorRotate`/`kVectorBroadcastLane` and two more. Any other opcode (the entire matmul/push/EUP band `{0x8d..0x153}` minus the XLU cases, and everything outside `[0x32, 0x155]`) takes the pass-through arm.

The `ceil` is integer arithmetic with sign correction (byte-exact `@0x126e0e8b..0x126e0ed0`):

```c
long ceil_div(int raw, int div) {        // div > 0 in practice (xlu_count >= 1)
    int q = raw / div;                    // truncating cdq/idiv
    if (q < 0) return q;                  // negative quotient: no round-up
    int bump = 1;
    if (raw <= div*q || div <= 0)         // remainder is zero (or div<=0 guard)
        bump = (div < 0 && raw < div*q);
    return q + bump;                      // = ceil(raw/div) for div>0
}
```

Verified against worked cases in the decompile semantics: `ceil(8/3)=3`, `ceil(88/4)=22`, `ceil(92/8)=12`, `ceil(105/2)=53`, `ceil(7/2)=4`.

> The XLU↔XLU edge is the per-generation base latency **divided across the available cross-lane units**. With `xlu_count = 2`, a reduce-chain edge costs `ceil(B/2)` — roughly half the serial latency, because the two XLUs run in parallel. This is the single discount the whole optimizer prices against; everything below reads `LatencyBetween` through this wrapper.

`VectorRawHazardCycles` (`@0x126e0e20`) tail-delegates to the base table unchanged — the wrapper scales the dependency edge, **not** the raw-hazard cycles. `xlu_count` itself comes from `VectorIsa.xlu_count` (proto field #6), copied into `DWORD[Target+0x4b0]` by `Target::Init` (`@0x1d60fc20`); the neighboring `[Target+0x4ac]` = `mxu_count`, `[Target+0x4a8]` = `iar_count`.

---

## ComputeCombinablePairs — The Fusion Analysis

### Purpose

Two adjacent XLU ops that do the *same* cross-lane operation on the *same* operands (e.g. two sum-reduces feeding the same permute pattern) can collapse into one cross-lane pass that pays for the pattern setup once. `ComputeCombinablePairs` finds those fusions and returns the candidate pairs; the actual rewrite is done later by `ReemitReorderedCombinedXluOperations`.

### Entry Point

```text
LloXluGraphOptimizer::Optimize @0x126cdb80
  └─ CrossXlu Create (tracker #1, reverse=0)        ; data-dependency tracker over the XLU ops
  └─ ComputeCombinablePairs @0x126d2480  ── this pass
       ├─ CyclesAddedByXluOperation @0x126d22a0     ; per-op marginal cost (PreXlu table)
       ├─ GetRpuTransposeOperationKeyFrom @0x126d8520 ; RpuOperationMetadata extractor
       ├─ value_visitor __fmatrix @0x218e0898       ; variant-dispatched grouping
       │    ├─ $_0 TransposeTile @0x126dce40        ; TransposeTileMetadata bucket
       │    ├─ $_1 RpuOperation  @0x126dd0a0        ; RpuOperationMetadata bucket
       │    └─ $_2 XluControl    @0x2139b1c0        ; LogFatal — UNREACHABLE
       └─ XluOperationIsReady @0x126cd920           ; tracker readiness gate
```

### Algorithm

`ComputeCombinablePairs` (`@0x126d2480`) takes the XLU-op list (`vector<variant<TransposeTile, RpuOperation, XluControlOperation>*>`), the cross-region `from`/`to` boundary `LloValue` pair, and the dependency tracker, returning (sret) a `vector<pair<variant*,variant*>>`.

```c
// ComputeCombinablePairs(const vector<variant*>& xlu_ops, LloValue* from, LloValue* to,
//                        CrossXluOperationsDataDependencyTracker* tracker) -> vector<pair<variant*,variant*>>
function ComputeCombinablePairs(xlu_ops, from, to, tracker):
    N = xlu_ops.size()
    cycles[N], values[N], cummax[N] = new int64[N] each, memset 0   // @0x126d251f/255b/257f

    // PHASE A — per-op cost + value arrays (@0x126d24b9..)
    prev_op = null; prev_value = null
    for i in 0..N:
        op = xlu_ops[i]
        cur_value  = resolve_value(op)              // by variant idx @[op+0x40]
        values[i]  = cur_value.anchor               // [value+0x28]->[+0x10]
        cycles[i]  = CyclesAddedByXluOperation(prev_op, op, prev_value, cur_value, PreXlu_table)
        if op contributes: prev_op = op; prev_value = cur_value   // cmovne-advance

    // critical-path DP (triangular loop @0x126d2b30): cumulative-max of per-op cycles over windows
    for i in 0..N:
        for j from i downward:
            cummax[j] = max(cummax[j], cycles[j] + window_cost)

    // PHASE B — metadata grouping + pair emission (@0x126d2bf9..)
    rpu_map  : FlatHashMap<RpuOperationMetadata,      btree_set<long,256>>   // op INDICES per key
    xpo_map  : FlatHashMap<TransposeTileMetadata,     btree_set<long,256>>
    for i in 0..N:
        op = xlu_ops[i]
        visit(op):                                  // value_visitor __fmatrix @0x218e0898
          valueless (idx 0xff): skip
          TransposeTile  ($_0): key = {height, anchor, vxpose_mode, ...}    // bucket = xpo_map
          RpuOperation   ($_1): key = GetRpuTransposeOperationKeyFrom(op)   // bucket = rpu_map
          XluControl     ($_2): LogFatal "unexpected XluOperation type"     // :1750 — UNREACHABLE
        for prior_index in bucket[key]:
            if XluOperationIsReady(tracker, op) and cost_compatible(cummax):   // @0x126cd920
                emit pair {xlu_ops[prior_index], op}     // push_back @0x126d8880
                RemoveScheduledXluOperation(tracker, ...)  // @0x126ccfa0 — erase scheduled
        bucket[key].insert(i)
    return pairs
```

### The Fusion Predicate

Two adjacent XLU ops fuse into one cross-lane operation **iff** all three hold:

1. **Same variant kind and same metadata key.** For an `RpuOperation`, the key is `{opcode, source-operand-0, source-operand-1}`; for a `TransposeTile`, `{height, anchor, vxpose-mode, …}`. Equal keys collide in the same hash bucket.
2. **The later op is tracker-ready.** `XluOperationIsReady(tracker, op)` (`@0x126cd920`) returns true when the op's in-edge count is zero — all its predecessor XLU ops are already scheduled (list-scheduling readiness).
3. **Combining keeps the critical-path cost bounded** — the `CyclesAddedByXluOperation` DP arrays (`cycles[]`/`cummax[]`).

`XluControlOperation` ops **never** fuse: the `$_2` visitor arm (`@0x2139b1c0`) is a `__noreturn` `LogMessageFatal("unexpected XluOperation type", llo_xlu_graph_optimizer.cc:1750)`.

### The Two Fusion Keys

| variant (idx) | key struct (byte-exact) | extractor | Confidence |
|---|---|---|---|
| `RpuOperation` (1) | `RpuOperationMetadata {u16 opcode@0, LloValue* op0@8, LloValue* op1@0x10 (gated u8@0x18==1)}` | `GetRpuTransposeOperationKeyFrom` @ `0x126d8520` | CONFIRMED |
| `TransposeTile` (0) | `TransposeTileMetadata {i32 height@0, i64@8, u16@0x10, u8 vxpose_mode@0x12, u8@0x13}` | inline in `$_0` @ `0x126dce40` | CONFIRMED |

`GetRpuTransposeOperationKeyFrom` (`@0x126d8520`) reads `value = [[RpuOp+0x10]+0x10]`, sets `opcode = WORD[value]`, and fills the two operand identities:

```c
// RpuOperationMetadata GetRpuTransposeOperationKeyFrom(RpuOperation& op)   @0x126d8520
key.opcode = WORD[value];
if (key.opcode == 0x3a) {                  // kVectorRotate — single from-end operand
    key.op0 = operand_at(value, count-1);  // [value-0x10]=count; [value+0xb]&3 = operand-from-end mask
    key.op1 = 0; key.has1 = 0;             // [out+0x18] = 0
} else {
    key.op0 = operands[0];                 // first source LloValue*
    key.op1 = operands[1]; key.has1 = 1;   // two source operands
}
```

The hashers confirm the field layout: `RpuOperationMetadata` hash (`@0x126e1140`) is a crc32 of `{u16@0, i64@8, then i64@0x10 only if u8@0x18==1}`; `TransposeTileMetadata` hash (`@0x126e1640`) is a crc32 of `{i32@0, i64@8, u16@0x10, u8@0x12 (xor 0x1 = vxpose bool), u8@0x13}`.

> **GOTCHA —** the `btree_set<long>` buckets hold op **indices**, not op pointers, and have node capacity 256 (`btree_set<long, less, alloc, 256>`). A naive reimplementation keying directly on pointers will work, but the readiness gate fires *and erases* scheduled ops from the tracker mid-walk (`RemoveScheduledXluOperation` @ `0x126ccfa0`) — the predicate is stateful, so two passes over the same list are not idempotent.

---

## AssignXlu — Greedy Least-Loaded Unit Assignment

### Purpose

After fusion, each surviving (combinable) op is placed on one of the per-generation XLU units. `AssignXlu` is a greedy bin-packer: it puts each op on whichever XLU currently has the smallest accumulated cost, balancing the per-unit critical path. The unit choice is committed into the instruction word immediately, so the reorder and the final emission see it.

### Algorithm

`AssignXlu(Span<pair<variant*,variant*> const> pairs)` (`@0x126d3100`) is meaningful only for `xlu_count > 1`: it `CHECK`s `xlu_count >= 2` and `LogFatal`s otherwise (line 0xa03 in the optimizer source). It reaches the `Target` via `[[optimizer][0]+0x168]` and reads `xlu_count = DWORD[Target+0x4b0]`.

```c
// LloXluGraphOptimizer::AssignXlu(Span<pair<variant*,variant*> const> pairs)   @0x126d3100
function AssignXlu(pairs):
    CHECK(xlu_count >= 2)                              // line 0xa03 LogFatal otherwise
    rec[xlu_count] = new(xlu_count << 5), memset 0     // 0x20 B each; [rec+0]=running cost
    for p in pairs:                                    // stride 0x10
        // min-cost XLU pick (unrolled x4 @0x126d320d + remainder @0x126d32a0)
        chosen = argmin over u in 0..xlu_count of rec[u].cost      // cmovl keeps min + index
        $_0(p.first,  chosen)                          // write unit index into backing instrs
        if p.second: $_0(p.second, chosen)
        rec[chosen].cost += CyclesAddedByXluOperation(rec[chosen].prev, p.first,
                                                       anchor_from, anchor_to, PreXlu_table)
        rec[chosen].prev = p.first                     // [rec+0x18] = new prev op
```

The `$_0` lambda (`@0x126db0a0`) writes the chosen unit index into the unit-selector field of **every** backing `LloInstruction`:

- `RpuOperation` (idx 1): record `[op+0x20] = xlu`, write the field on the op's two instructions (`[op+0x10]`, `[op+0x18]`).
- `TransposeTile` (idx 0): record `[op+0x34] = xlu`, write the field on every element of the read-set `InlinedVector` (`[op+0x00]`, count `[op+0x08]`) **and** the written-set (`[op+0x18]`, count `[op+0x20]`).

The field write is byte-identical to the LLO-emission validator `ValidateAndSetXluAndSourceBus`:

```c
// unit selector (XLU or MXU instance) — AssignXlu $_0 and ValidateAndSet*…
WORD[instr+0xb] = ((xlu & 3) << 8) | (WORD[instr+0xb] & 0xF8FF) | 0x400;   // bits 8-9 + valid bit 10
```

`AssignXlu` is the **scheduler-side producer** of this field; `ValidateAndSetXluAndSourceBus` (XLU ops) / `ValidateAndSetMxuAndSourceBus` (matmul-push ops) are the emission-side validators that re-assert it, with the matching `CHECK(xlu < xlu_count)` range check. `AssignXlu` is gated on `optimizer+0x28 == 1` in `Optimize`.

---

## ReorderToShortenCriticalPath — The List Scheduler

### Purpose

With each op assigned to an XLU, `ReorderToShortenCriticalPath` reorders the combinable-pair list so each XLU's critical path is as short as possible. It is a textbook latency-weighted list scheduler: ready ops are scheduled longest-marginal-cost-first, so the chain that dominates an XLU's path is committed earliest.

### Entry Point

```text
Optimize @0x126cdb80
  └─ CrossXlu Create (tracker #2, reverse=1)         ; rebuilt on the unit-assigned graph
  └─ ReorderToShortenCriticalPath @0x126d3460  ── this pass
       ├─ $_1 COMMIT          @0x126d8b60   ; erase from pending set, advance clock, write output
       ├─ $_2 READY + CP-test @0x126d90e0   ; XluOperationIsReady + completion-clock compare
       ├─ $_3 MARGINAL-COST   @0x126d9240   ; CyclesAddedByXluOperation on the op's XLU
       ├─ $_4 GROUP-RECORD    @0x126d93a0   ; (from,to)-keyed combinable-group map
       └─ pop_heap @0x126e1b40 / emplace @0x126d8980  ; the per-XLU max-heap
```

### Data Structures

`ReorderToShortenCriticalPath(vector<pair<variant*,variant*>>* pairs, CrossXluOperationsDataDependencyTracker* tracker)` (`@0x126d3460`) allocates one `PerXluOperations` state struct per XLU unit (stride `0x60`, `alloc = 3*(xlu_count<<5)`). The field layout is documented in [XLU Reemit Cost](xlu-reemit-cost.md); the offsets this pass reads:

| offset | field | role |
|---|---|---|
| `+0x00` | `btree_set<long, less, alloc, 256>` | per-XLU pending op-index set |
| `+0x38` | `i64` | remaining running-cycle accumulator (Σ cost of pending ops) |
| `+0x50` | `variant*` | last-scheduled op on this XLU (the `prev` for the next `CyclesAdded`) |
| `+0x58` | `i64` | per-XLU completion-time clock (the critical-path frontier) |

Plus a per-op `cost[]` and `anchor[]` array, a `FlatHashMap<pair<LloValue*,LloValue*>, btree_set<long,256>>` combinable-group map, the per-XLU `priority_queue<pair<long,long>, vector<…>, less<>>` candidate **max-heap**, and the reordered output vector (written back to `*pairs` via `__assign_with_size`).

### Algorithm

```c
// LloXluGraphOptimizer::ReorderToShortenCriticalPath(vector<pair*>* pairs, tracker)   @0x126d3460
function ReorderToShortenCriticalPath(pairs, tracker):
    if xlu_count == 0 or pairs.empty(): return       // short-out
    per_xlu[xlu_count] = PerXluOperations (stride 0x60), each btree EmptyNode + zeroed

    // PHASE A — per-op preprocessing
    for i in 0..N:
        xlu = assigned_xlu(op_i)                      // WORD[instr+0xb]: (>>8)&3 gated by &0x400
        cost[i] = CyclesAddedByXluOperation(per_xlu[xlu].prev, op_i, anc_from, anc_to, PreXlu_table)
        per_xlu[xlu][+0x38] += cost[i]                // running cycles
        anchor[i] = op_i.anchor; per_xlu[xlu].pending.insert(i); group_map[(from,to)].insert(i)

    // PHASE B — list-scheduling loop
    while candidates remain:
        idx = top of the per-XLU max-heap          // less<(cost,index)> → longest marginal cost first
        // pre-test: only attempt a reorder when it can shorten this XLU's remaining path
        if heap_top_cost < per_xlu[xlu][+0x38]:      // cmp @0x126d494b ; jl skip
            ...
        {ready, frontier} = $_2(idx):                // @0x126d90e0
            ready = XluOperationIsReady(tracker, op_a) &&
                    (op_b ? XluOperationIsReady(tracker, op_b) : true)   // in-edge==0, @0x126cd920
            if not ready: return {false, 0}
            cp_cost = $_3(op_a)                       // marginal cost on its XLU @0x126d9240
            frontier = (per_xlu[xlu][+0x58] + cp_cost >= finish[idx])    // setge — the CP test
        if ready:
            $_1(idx):                                 // COMMIT @0x126d8b60
                write {op_a, op_b} into the output vector
                per_xlu[xlu].pending.erase(idx)       // btree erase @0x126ba360
                advance per_xlu[xlu][+0x58] clock; set per_xlu[xlu][+0x50] = op
                pop_heap                              // @0x126e1b40
        else:
            cost = $_3(op);  $_4(op)                  // re-price + group-record @0x126d93a0
            heap.emplace({cost, idx})                 // @0x126d8980
    *pairs = reordered output                          // __assign_with_size
```

> **NOTE —** the heap is `less<pair<long,long>>` (a max-heap) keyed `{marginal_cost, op_index}`, so the **highest** marginal-cost ready op is popped first — the longest-latency-first heuristic. Ties break by the second pair element (read structurally as the op-index; LOW that it is op-index ascending rather than another priority). The `$_2` lambda's `LogFatal` on a control-op variant (`control != nullptr`, line 2532) confirms control ops are never reordered either — consistent with their never being combinable.

### The Two Trackers

`Optimize` builds the `CrossXluOperationsDataDependencyTracker` **twice**: once (`reverse=0`) before `ComputeCombinablePairs`/`AssignXlu`, once (`reverse=1`) before the reorder, so the reorder's readiness is computed against the **post-combine, post-unit-assign** dependency graph. Both consumers share the `XluOperationIsReady` predicate (in-edge count == 0).

---

## AssignSourceBus — The VEX Source-Bus Allocator

### Purpose

On generations with VEX source buses (Pufferfish v4 only in this build), each XLU op's operands must be routed onto one of the four read ports V0/V1/V2/V3. `AssignSourceBus` walks the dependency graph in topological order and binds each cross-lane op to a bus, inserting a serialization edge whenever two ops would collide on the same bus.

### Entry Point

```text
Optimize @0x126cdb80
  └─ [gate optimizer+0x28==1] AssignSourceBus @0x126d70e0  ── this pass
       ├─ HasVexSourceBuses()  vtable[+0x408]   ; gate — Pufferfish-only true
       ├─ NodesInTopologicalOrder(false) @0x1442b8c0
       ├─ SourceBusesForXlu(i) vtable[+0x518]    ; {i, i+2} on Pufferfish
       ├─ LloOpcodeUsesSourceBus @0x10c0d420     ; the 29-opcode gate
       ├─ LloOpcodeUsesMxu       @0x10a433e0     ; MXU vs pure-XLU split
       ├─ $_0 critical-path depth @0x126db4e0    ; latency-weighted level
       ├─ $_1 greedy free-bus     @0x126db6c0    ; non-MXU XLU ops
       └─ $_2 explicit bus bind   @0x126dbb80    ; MXU-indexed + shared-bus edge
```

### The HasVexSourceBuses Gate

The entire pass is gated on `Target::HasVexSourceBuses()` — vtable slot `+0x408` (`+1032` in the decompile). Per generation (byte-exact from the `Target` vtables):

| Target (gen) | `HasVexSourceBuses()` | source-bus pass | Confidence |
|---|---|---|---|
| `JellyfishTarget` (v2/v3) | `false` (`xor eax,eax`) @ `0x1d4904a0` | no-op | CONFIRMED |
| `PufferfishTarget` (v4) | `true` (`mov al,1`) @ `0x1d494b40` | **active** | CONFIRMED |
| `ViperfishTarget` (v5p) | `false` @ `0x1d49ae40` | no-op | CONFIRMED |
| `GhostliteTarget` (v6e) | `false` @ `0x1d497d00` | no-op | CONFIRMED |

So in libtpu 0.0.40 the VEX source-bus assignment is exercised for Pufferfish only.

### The Bus Pool — SourceBusesForXlu

When active, the pass first sizes the bus pool by iterating the `xlu_count` XLU units and accumulating each unit's bus count:

```c
// @0x126d7aa9 — bus-pool sizing
total_buses = 0
for xlu in 0 .. DWORD[Target+0x4b0]:                 // xlu_count
    pool = Target->SourceBusesForXlu(xlu)             // vtable[+0x518]
    total_buses += pool.count >> 1                    // count-tag >> 1
```

`SourceBusesForXlu(int)` is abstract — `LogFatal "not implemented"` on the base/JF/VF targets; only `PufferfishTarget` (`@0x1d494f20`) has a concrete body, byte-exact: `[ret+0x00]=4` (size-tag ⇒ 2 buses), `[ret+0x08]=i`, `[ret+0x0c]=i+2`. So **XLU unit *i* owns source buses `{i, i+2}`**; for the typical 2-XLU config, XLU 0 owns `{0, 2}` and XLU 1 owns `{1, 3}` — the V0/V1/V2/V3 read ports paired `(V0,V2)` and `(V1,V3)`, a `2*xlu_count`-deep pool.

> **QUIRK —** the `{i, i+2}` port-pair map is **Pufferfish-specific**. The `AssignSourceBus` *mechanism* is generation-generic (it calls the virtual `SourceBusesForXlu`), but the only live concrete implementation in this binary is Pufferfish's. A hypothetical future gen reporting `HasVexSourceBuses() == true` would supply a different map; that map is not in this build (LOW for any other gen).

### Per-Node Bus Binding

For each topological node whose opcode satisfies `LloOpcodeUsesSourceBus`, the pass splits on `LloOpcodeUsesMxu`:

```c
// @0x126d7e60.. per-node binding
for node in NodesInTopologicalOrder():
    if not HasVexSourceBuses(): continue
    op = WORD[value+0x10]
    if not LloOpcodeUsesSourceBus(op): continue        // 29-opcode gate
    if LloOpcodeUsesMxu(op):                            // matmul-push 0x8f..0x96
        mxu = (WORD[value+0xb] >> 8) & 3                // gated by &0x400 → | 0x1_0000_0000
        $_2(mxu)                                        // bind explicit MXU-indexed bus
    else:                                               // pure XLU op
        extract operands (op-0x8b dispatch)
        $_1()                                           // greedily bind next free bus
    node_to_bus_map[node] = bus                         // find_or_prepare_insert @0x126c7b80
```

`LloOpcodeUsesSourceBus` (`@0x10c0d420`) is `true` for exactly **29** opcodes (confirmed as the literal `case` set):

```text
{0x36, 0x3a, 0x3b}        permute / rotate / broadcast-lane
{0x8b, 0x8c}              set-permute-pattern / set-segment-pattern
{0x8f .. 0x96}            8 matmul-push ops (the MXU operand path, UsesMxu)
{0xa6, 0xa7}              transpose / transpose-binary
{0xf5 .. 0x101}           13 cross-lane reduce / index / segment-reduce ops
{0x155}                   transpose-clear
```

This is the `IsXluOp` 21-op set ∪ `{0x36, 0x3a, 0x3b}` ∪ the 8 matmul-push ops; the matmul-push ops route operands through the source bus *into* the MXU, the rest through the XLU/RPU read ports.

### Critical-Path Depth — $_0

The `$_0` lambda (`@0x126db4e0`) computes each node's latency-weighted depth, used to prioritize bus binding. It walks the node's predecessor edges via the `LloDependencyGraphEdgeMap` external-chunk form (the `&3 == 3` indirect-edge form, `RemoveChunk` @ `0x1443b460`), reads each predecessor's depth (`[pred+0x10]`) and the **edge latency** packed in the edge-map entry's high bits (`entry >> 33`, the `sar entry, 0x21`), and sets:

```c
node.depth[+0x10] = max over preds of (pred.depth + edge_latency);
```

The edge latencies are the `PreXluAssignmentLatencyTable` (`ceil/xlu_count`) values written into the graph — so the depth is priced through the same XLU edge model as everything else.

### The Shared-Bus Hazard — $_2 / $_1

The `$_2(int slot)` lambda (`@0x126dbb80`) binds an op to an explicit (MXU/XLU-indexed) bus and inserts the structural hazard. Byte-exact:

```c
// AssignSourceBus::$_2(int slot)   @0x126dbb80
function $_2(slot):
    CHECK(slot < bus_slots.count)                  // BUG()/ud2 otherwise
    s = bus_slots[slot]                             // 16-byte: [s+0]=value, [s+8]=node
    if (s.node != 0):                               // slot already holds a prior op
        lat = LatencyTable.LatencyBetween(s.value, this_value)   // @0x1c89f820 (optimizer table)
        CHECK(lat > 0)                              // line 2753 LogFatal w/ both ToString'd ops
        LloDependencyGraph::UpdateEdge(prev_node, this_node, lat)  // @0x14428b00 — SERIALIZE
    $_0(this_node)                                  // recompute depths
    s.value = this_value; s.node = this_node        // record the new occupant
```

`$_1()` (`@0x126db6c0`) is the greedy free-bus allocator for non-MXU XLU ops: it pushes the node onto the worklist, records the node→bus assignment in the `FlatHashMap<LloDependencyGraphNode*, int>` (`find_or_prepare_insert_large`), and re-runs `$_0`. Both paths use the same shared-bus edge mechanism.

> **GOTCHA —** two XLU ops assigned the **same** source bus get a brand-new latency-weighted dependency edge (`UpdateEdge`, weight = `LatencyBetween`) that serializes them on that V-port. A reimplementer who treats the source bus as a pure register-renaming hint will under-count this hazard: the bus is a structural resource, and sharing it costs a real edge in the critical path. The `CHECK(lat > 0)` is a hard invariant — a zero-latency shared-bus edge is treated as a bug, not a free fusion.

---

## The LLO-Instruction Bit-Field

`AssignXlu` (unit) and `AssignSourceBus` (bus) both write `WORD[LloInstruction + 0xb]`; the LLO-emission validators `ValidateAndSetXluAndSourceBus` / `ValidateAndSetMxuAndSourceBus` re-assert the same field. Byte-exact:

```text
WORD[LloInstruction + 0xb] (16-bit; byte 0xb low, byte 0xc high):
  bit  8-9   : XLU/MXU UNIT INDEX   (2-bit, {0..3})         ← AssignXlu $_0 / ValidateAndSet*…
  bit  10    : UNIT-ASSIGNED valid flag (the +0x400)        ← set together with the index
  bit  11-12 : SOURCE-BUS INDEX     (2-bit, {0..3})         ← ValidateAndSet*…SourceBus (PF only)
  bit  13    : SOURCE-BUS-ASSIGNED valid flag (the +0x2000) ← set together with the bus index
```

```c
// source bus (Pufferfish only) — the second 2-bit field
WORD[instr+0xb] = ((bus & 3) << 11) | (WORD[instr+0xb] & 0xC7FF) | 0x2000;  // bits 11-12 + valid bit 13
```

The source-bus field holds a raw 2-bit index `{0..3}` — it is **not** the SparseCore `VexSourcePortEncoding` proto enum (an 8-value, 3-bit encoding on a different datapath produced by an identity `VregReadPort → encoding` map). The `{i, i+2}` `SourceBusesForXlu` pool indexes this 2-bit TensorCore field; the V0..V3/X-Y sub-port `VexSourcePortEncoding` is the SparseCore EUP mux — distinct concerns. The `ValidateAndSetMxuAndSourceBus` twin sets the same two fields for matmul-push ops, with `source_bus = mxu_index` (the explicit-MXU-indexed bus, the `$_2` path above) and the analogous `CHECK(mxu < mxu_count)`.

---

## Worked Example — Two `kVectorAddReduceF32` (0xf7), Pufferfish v4, xlu_count = 2

1. **Combine.** `ComputeCombinablePairs` keys both reduces with `RpuOperationMetadata{0xf7, src0, src1}` (`0xf7 ≠ 0x3a`, so two source operands). If the second is tracker-ready (`XluOperationIsReady` — its source producer scheduled) and within the `cummax` DP budget, they collide in the RPU bucket → `push_back {&R_a, &R_b}` (combinable into one cross-lane reduce pass).
2. **Edge cost.** `CyclesAddedByXluOperation` prices the reduce-result edge through the `PreXluAssignmentLatencyTable`. If the per-gen base reduce-edge latency is `B`, the XLU↔XLU edge costs `ceil(B / 2)` — half the serial latency, because the two XLUs run in parallel.
3. **AssignXlu.** `xlu_count == 2` (≥ 2 OK). The min-cost pick puts the pair on XLU 0 (tie → index 0). `$_0` writes `unit = 0 | valid` into both instruction words (`WORD[+0xb] |= (0<<8) + 0x400`, `R_a.[+0x20] = 0`). `rec[0].cost = ceil(B/2)`; a third op lands on XLU 1 (now least-loaded) → load-balanced.
4. **Reorder.** The tracker is rebuilt (`reverse=1`) on the unit-assigned graph. `ReorderToShortenCriticalPath` queues `R_a`/`R_b` keyed by `$_3 = ceil(B/2)`; when `XluOperationIsReady(R_a)` and the completion-clock test passes, `$_1` commits: erase from `per_xlu[0]` pending, advance `[+0x58] += ceil(B/2)`, append `{R_a,R_b}` to the output. The max-heap pops the longest-cost ready op first, so the reduce chain that dominates the XLU path schedules earliest.
5. **AssignSourceBus.** `HasVexSourceBuses == true`. `LloOpcodeUsesSourceBus(0xf7) == true`, not `UsesMxu` → `$_1()` greedy-binds the fused op to a free bus from `SourceBusesForXlu(0) = {0,2}`. If both reduces landed on the same bus, the `$_2`/`$_1` shared-bus path inserts `UpdateEdge(R_a→R_b, LatencyBetween)` to serialize them on that V-port; otherwise they use disjoint V-ports and stay parallel. The 2-bit bus index is written into `WORD[+0xb]` bits 11-13.

Contrast a matmul-push (`0x90`): `LloOpcodeUsesSourceBus(0x90) == true` **and** `LloOpcodeUsesMxu(0x90) == true` → the MXU instance is read from `WORD[value+0xb]` and `$_2(mxu_index)` binds the explicit MXU-indexed source bus.

---

## What Is Not Pinned

- The exact `cummax` DP acceptance threshold in `ComputeCombinablePairs` (the triangular loop `@0x126d2b30`): decoded structurally as a cumulative-max of per-op cycles over windows fed into the visitor, not isolated as a single closed-form accept/reject inequality. MEDIUM.
- The reorder heap's tie-break composition: confirmed `less<pair<long,long>>` keyed `{cost, index}`, but whether equal-cost ops break by op-index ascending vs another second-field priority is read structurally. LOW.
- `SourceBusesForXlu` for any gen other than Pufferfish: base/JF/VF are `LogFatal "not implemented"`. Only Pufferfish reports `HasVexSourceBuses == true` in this build, so `{i, i+2}` is the only live map. LOW for any hypothetical future gen.
- The int-bus-index → `VexSourcePortEncoding` ordinal: `AssignSourceBus` works with opaque bus ints `{0..3}`; the proto enum is a separate SparseCore datapath. The TensorCore 2-bit field is the only one written by these passes.
- The `PreXluAssignmentLatencyTable` delegate (`[+0x18]`) provenance across the full optimizer pipeline: it is the optimizer's previous `[optimizer+0x8]` table (chained wrapping); the first table in the chain is read as "the previous +0x8", not traced to its construction in `Run`. MEDIUM.

---

## Cross-References

- [XLU Op Roster](../isa/xlu-op-roster.md) — the opcode catalog and the full five-stage pipeline at higher altitude; the `IsXluOp` / classifier ranges and the reemit/slot-fit geometry.
- [XLU Reemit Cost](xlu-reemit-cost.md) — the closed-form `CyclesAddedByXluOperation` and the `PerXluOperations` struct this page's passes consume.
- [XLU Conflict-Penalty Table](xlu-conflict-penalty.md) — the non-MXU hazard cost the latency model accounts for.
- [Transpose-Reservation Latency](xpose-reservation-latency.md) — the `XposeXLUReservationLatency` / `VxposeMode` geometry behind the transpose fusion key.
- [CycleTable Family](cycletable-family.md) — the throughput half of the cost model; the same `Create`-by-registry, `Performance`-grid framing as `LatencyTable::Create`.
- [ResultFifo and ArchRegister](../isa/resultfifo-archregister.md) — `ComputeXluOperations`, which emits the `variant<TransposeTile, RpuOperation, XluControlOperation>` list this optimizer consumes.
