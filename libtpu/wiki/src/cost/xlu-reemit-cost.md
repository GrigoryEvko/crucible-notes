# XLU Reemit Cost

> *Every opcode, address, offset, and field on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, 781,691,048 bytes, not stripped — full C++ symbols, `nm -C` resolves every method). `.text` and `.rodata` VMAs equal their file offsets; `.data.rel.ro` VMA − 0x200000 = file offset. Other libtpu builds will differ.*

## Abstract

The XLU optimizer (`LloXluGraphOptimizer::Optimize`, [`xlu-op-roster`](../isa/xlu-op-roster.md)) is a five-stage rewrite: combine adjacent identical cross-lane ops, balance them across the per-generation XLU units, reorder them to shorten the critical path, **re-emit** the fused/reordered ops as real LLO, then pack source buses. The first four stages are decision-making; the fifth (`AssignSourceBus`) is wiring. Every decision — which pair to fuse, which unit to load, which ready op to schedule next — keys on a single marginal-latency number, and that number is computed by one function: `CyclesAddedByXluOperation` (`@0x126d22a0`). This page is the authoritative reference for that cost function's closed form, for how `ReemitReorderedCombinedXluOperations` (`@0x126d5460`) charges the per-op added cycles as it re-emits, and for the `PerXluOperations` per-XLU scheduler-state struct (stride `0x60`) that carries the running cost between scheduling steps.

The cost is small and exact. For a new op `cur` chained after the XLU's last op `prev`, the cost is one `LatencyBetween(prev, cur)` edge on the optimizer's own `PreXluAssignmentLatencyTable` (the `ceil(base / xlu_count)` parallelism-discounted edge, [`xlu-op-roster`](../isa/xlu-op-roster.md)), **plus** — for an RPU `prev` — a `LatencyBetween` penalty for *each* of its two source operands that is not the cross-region boundary value (`from`/`to`); **or** — for a transpose `prev` — `(read_set_size − 1)` copies of the latency between the first two tile elements. An `XluControlOperation` is never priced (it is a hard `LogFatal` if it ever reaches the cost path). The same closed form is invoked verbatim by `AssignXlu`'s min-cost bin-pack, by the reorder list-scheduler's max-heap priority (`$_3`), and by the combine DP — pinning this one expression pins the entire XLU scheduling cost model.

This page treats the cost as it is *consumed* by the re-emission half of the pipeline. The reorder list scheduler accumulates per-op costs into a per-XLU `PerXluOperations` record, schedules the longest-marginal-cost ready op first, advances a per-XLU completion clock, and hands the ordered op vector plus the combinable-pair vector to `Reemit`, which materializes one fused LLO op per pair. The cost accounting lives in the scheduler state; `Reemit` consumes its output. Both halves are documented below.

For reimplementation, the contract is:

- The `CyclesAddedByXluOperation` closed form — the variant→anchor resolution, the main `LatencyBetween` edge, the two RPU per-source boundary-miss penalties, the transpose `(n−1)` multiplier, the control-op `LogFatal`.
- The variant operand layout the cost reads: an `RpuOperation` stores its two source `LloValue*` inline at `[+0x00]`/`[+0x08]`; a `TransposeTile` stores its read-set (ptr `[+0x00]`, size `[+0x08]`).
- The `PerXluOperations` `0x60` field map and the three writes that update it per scheduled op: the `[+0x38]` remaining-cycle decrement, the `[+0x58]` completion-clock max, the `[+0x40]`/`[+0x48]`/`[+0x50]` anchor/last-op record.
- The per-XLU max-heap composition: `pair<long,long> {marginal_cost, op_index}` under `less<>`, so the longest-cost ready op pops first, ties broken by higher op-index.

| | |
|---|---|
| **Cost function** | `CyclesAddedByXluOperation` @ `0x126d22a0` (size `0x1e0`) → `long` |
| **Anchor resolver** | `GetAnchorInstruction` @ `0x126cda00` (variant → anchor `LloValue`) |
| **Operand reader** | `LloValue::operands(int)` @ `0x10b8e040` (backward-stored source span) |
| **Edge table** | `LatencyTable::LatencyBetween` @ `0x1c89f820` (vtable `+0x18` = PreXlu `ceil(base/xlu_count)`) |
| **Re-emitter** | `ReemitReorderedCombinedXluOperations` @ `0x126d5460` (size `0x1c75`) → `absl::Status` |
| **Scheduler** | `ReorderToShortenCriticalPath` @ `0x126d3460`; `PerXluOperations` stride `0x60` |
| **Heap priority** | `$_3` @ `0x126d9240` — tail-calls `CyclesAddedByXluOperation` |
| **Commit** | `$_1` @ `0x126d8b60` — erases pending idx, advances clock, records anchors |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Cost Function — `CyclesAddedByXluOperation`

### Purpose

`CyclesAddedByXluOperation` answers one question: *if I place op `cur` immediately after op `prev` on the same XLU, how many extra cycles does that cost?* It is the marginal latency of chaining one cross-lane op after another, evaluated on the optimizer's `PreXluAssignmentLatencyTable`. It is the only cost function in the XLU pipeline; the combine DP, the unit bin-packer, and the reorder heap all call it. There is no separate "fuse cost" or "reorder cost" — those are the same number on the same table.

### Signature

```c
// xla::jellyfish::(anon)::CyclesAddedByXluOperation @ 0x126d22a0
long CyclesAddedByXluOperation(
    const variant* prev,    // a1 — the XLU's last-scheduled op (or null)
    const variant* cur,     // a2 — the op being chained after prev (or null)
    const LloValue* from,   // a3 — cross-region boundary value "from"
    const LloValue* to,     // a4 — cross-region boundary value "to"
    LatencyTable& tbl);     // a5 — the PreXluAssignmentLatencyTable
```

The variant is `std::variant<TransposeTile, RpuOperation, XluControlOperation>` with a discriminant byte at `[v+0x40]`: `0` = `TransposeTile`, `1` = `RpuOperation`, `2` = `XluControlOperation` (this layout is confirmed across `GetAnchorInstruction`, the `$_3` heap lambda, and the cost body). The `from`/`to` pair is the cross-region boundary that combining reads through: from `AssignXlu` they are the chosen XLU record's anchor pair; from the reorder `$_3` they are `PerXluOperations[xlu][+0x40]`/`[+0x48]` (the last-scheduled op's two source operands).

### Variant → Anchor Resolution

Both `prev` and `cur` are resolved to an anchor `LloValue` the same way (inline in the cost body, identical to `GetAnchorInstruction` @ `0x126cda00`):

```c
// resolve(v) → the variant's anchor LloValue*; op_data(v) = [resolve(v) + 0x10]
LloValue* resolve(const variant* v) {
    switch (v->discr /* [v+0x40] */) {
      case 1: /* RpuOperation        */ return *(LloValue**)(v + 0x10);
      case 2: /* XluControlOperation */ return *(LloValue**)(v);
      case 0: /* TransposeTile       */
        // last element of the read-set InlinedVector: ptr [v+0x00], size [v+0x08]
        if (size == 0) BUG();
        return *(LloValue**)( readset_ptr + 8*size - 8 );
    }
}
```

`op_data(v)` — the value the latency table is keyed on — is the *anchor instruction* block one indirection deeper: `op_data(v) = *(LloValue**)(resolve(v) + 0x10)`. The `XluControlOperation` arm in `GetAnchorInstruction` is a `LogFatal` (`"control != nullptr"`, line 229) — control ops resolve only their inline value, never an anchor.

### Algorithm (the closed form)

Byte-exact from the decompile. The return accumulator starts at the main edge and is incremented by the per-source / per-chunk penalties.

```c
// CyclesAddedByXluOperation @ 0x126d22a0 — closed form
long cost = 0;

if (cur == null) {
    // empty-XLU base case: only a TransposeTile prev contributes anything
    if (prev == null || prev->discr != 0 /*not TransposeTile*/) return 0;
    goto transpose_tail;                                    // prev->discr == 0
}
if (prev == null) CHECK_fail("control != nullptr", 229);    // resolve(prev) needs a non-null prev

// MAIN EDGE: latency between cur's anchor op-data and prev's anchor op-data
cost = (int) tbl.LatencyBetween( op_data(cur), op_data(prev) );   // @0x126d236b

if (prev->discr != 0 /*not TransposeTile*/) {
    if (cur->discr == 0 /*cur is TransposeTile*/) return cost;     // skip operand penalties
    CHECK(prev->discr == 1 /*RpuOperation*/, "rpu_op != nullptr", 1012);  // control prev ⇒ LogFatal

    // RPU prev: two inline SOURCE LloValue* at [prev+0x00], [prev+0x08]
    LloValue* s0 = *(LloValue**)(prev + 0x00);
    if (s0 != null && s0->operands(0) != from)                    // operand(0) @0x10b8e040
        cost += (int) tbl.LatencyBetween( GetAnchorInstruction(cur), s0 );   // @0x126d23b4

    LloValue* s1 = *(LloValue**)(prev + 0x08);
    if (s1 != null && s1->operands(0) != to)
        cost += (int) tbl.LatencyBetween( GetAnchorInstruction(cur), s1 );   // @0x126d23e6
    return cost;
}

transpose_tail:                                                   // prev is TransposeTile
{
    long n = *(long*)(prev + 0x08);                               // read-set InlinedVector size
    if (n >= 2) {
        LloValue* e0 = op_data( readset[0] );    // [ readset_ptr[0]    + 0x10 ]
        LloValue* e1 = op_data( readset[1] );    // [ readset_ptr[1]    + 0x10 ]
        cost += (n - 1) * (int) tbl.LatencyBetween( e0, e1 );     // imul @0x126d2418
    }
}
return cost;
```

> **QUIRK — the penalty's second argument is the raw source value, not `op_data`.** The main edge prices `LatencyBetween(op_data(cur), op_data(prev))` — both arguments resolved through the `+0x10` anchor-instruction indirection. The two RPU operand penalties price `LatencyBetween(GetAnchorInstruction(cur), s0)` where `s0 = *(LloValue**)(prev+0x00)` is the inline source operand pointer **directly** (no `op_data` indirection on the second arg). A reimplementation that runs the penalty term through `op_data` on the source operand will compute a different — and wrong — edge. The asymmetry is in the binary at `@0x126d23a6`/`@0x126d23b4` (the `GetAnchorInstruction` call resolves `cur`; the source pointer is passed raw).

### The Three Terms, Interpreted

| Term | When | Value |
|---|---|---|
| Main edge | always (`cur != null`) | `LatencyBetween(cur_anchor, prev_anchor)` on the PreXlu table |
| RPU source-0 miss | `prev` is RPU, `cur` not transpose, `s0->operands(0) != from` | `+ LatencyBetween(cur_anchor, s0)` |
| RPU source-1 miss | same, `s1->operands(0) != to` | `+ LatencyBetween(cur_anchor, s1)` |
| Transpose chain | `prev` is TransposeTile | `+ (read_set_size − 1) * LatencyBetween(elem0, elem1)` |

The dominant term is the per-`(op,op)` edge — the cost of issuing the new op behind the XLU's last op. For an RPU op, **each source operand that is not the cross-region boundary value** (`from`/`to`) adds one more edge: the fan-in hazard of materializing a non-boundary source on the XLU. Boundary operands — the values that cross the region edge — are free, because they are already resident at the boundary. For a transpose chain, the added cost is one inter-element latency per *extra* chunk beyond the first: a longer transpose sequence costs proportionally more.

> **NOTE — `XluControlOperation` is unpriced by construction.** A control op as `prev` falls into the `prev->discr == 1` CHECK and `LogFatal`s (`"rpu_op != nullptr"`, line 1012). This is consistent with control ops never being combinable ([`xlu-combine-sourcebus`](xlu-combine-sourcebus.md)): they carry no source operands the cost could price, so the optimizer guarantees one never reaches this branch.

### `LloValue::operands(int)` — the boundary-miss probe

The two RPU penalties compare `s->operands(0)` against the boundary value. `operands` reads the backward-stored source span (`@0x10b8e040`, byte-exact):

```c
// LloValue::operands(this, i) @ 0x10b8e040
int count = *((int*)this - 4);                       // [this - 0x10]
int n_src = count - (*(u16*)((char*)this + 0xb) & 3);// subtract the 2-bit predication/aux tag
CHECK(i < n_src, "index < operands_size()", 499);    // else LogFatal
return *((QWORD*)this + i - count - 2);              // [this + (i - count)*8 - 0x10] — stored backward
```

`operands(0)` is the first source operand of the producer value `s` — the producer-chain head the boundary test compares against. The operands are laid out *before* the value header (negative offsets), so `operands(0)` is the deepest-stored element.

### The Edge — `LatencyBetween` and the PreXlu Table

The cost calls `LatencyTable::LatencyBetween` (`@0x1c89f820`), whose first act is a virtual dispatch through `vtable[+0x18]` — the `LatencyBetweenInternal` override. On the optimizer's `PreXluAssignmentLatencyTable` that override is the `ceil(base / xlu_count)` parallelism-discounted edge for XLU↔XLU pairs (documented in [`xlu-op-roster`](../isa/xlu-op-roster.md)); the public `LatencyBetween` then applies two adjustments:

```c
// LatencyTable::LatencyBetween @ 0x1c89f820 (after vtable[+0x18] base lookup → v4)
if (this[+0x10] /* random buffer */ != 0)
    v4 += UniformDistribution<int>(0, 101);          // stochastic perturbation (0..0x65)
if (cur.op == 0x84 && prev.op == 0x84)               // both kVectorWeird-band: floor at AutoOr<int> proto (def 16)
    v4 = max(v4, proto_floor);
else if (cur.op == 0x82 && (prev.op - 0x82) <= 2 && v4 < 3)   // 0x82-band edge: floor at 2
    v4 = 2;
return (unsigned) v4;
```

> **GOTCHA — the edge is not purely deterministic.** When `LatencyTable[+0x10]` (a lazily-allocated `BitGen` buffer) is non-null, every edge gets a `UniformDistribution(0, 0x65)` jitter added (`@0x1c89f84c`). Because `CyclesAddedByXluOperation` is the single cost the whole XLU schedule keys on, a non-null random buffer makes the entire schedule stochastic. The enable condition (debug fuzzing vs. always-off in production) is not isolated here — `LatencyTable[+0x10]` defaults to `0`, so the deterministic path is the default — but a reimplementer reproducing schedule order must account for it. **LOW** for the enable condition; the perturbation arithmetic is CONFIRMED.

The two opcode-band floors (`0x84`/`0x82` bands → minimum-latency clamps) are part of the public `LatencyBetween` and apply to every XLU edge the cost prices. They are CONFIRMED in the body but their per-opcode reach is documented with the [latency table](../isa/xlu-op-roster.md); here they are noted as the reason an XLU edge never drops below its band floor.

---

## Charging the Cost — `ReemitReorderedCombinedXluOperations`

### Purpose

`Reemit` is the IR-rewrite step that runs *after* the scheduler has chosen the order and the fusions. By the time it runs, every cost decision has already been made and committed into the `PerXluOperations` records; `Reemit` consumes the ordered op vector and the combinable-pair vector and materializes the LLO. It does **not** re-price anything — the cost accounting is entirely in the scheduler that precedes it. What `Reemit` *does* with the cost is honor it: a combinable pair that the scheduler priced as one fused op is emitted as exactly one cross-lane op, so the cycles the cost model charged (one main edge, one shared pattern setup) match the cycles the emitted op actually consumes.

### Signature and Boundary Threading

```c
// xla::jellyfish::(anon)::ReemitReorderedCombinedXluOperations @ 0x126d5460
absl::Status Reemit(
    LloDependencyGraph* graph,             // a1
    LloInstruction*     from_instr,        // a2 (WORD*) — cross-region boundary "from" instruction
    LloValue*           from_val,          // a3 (WORD*) — boundary "from" value
    LloInstruction*     to_instr,          // a4 — boundary "to" instruction
    LloValue*           to_val,            // a5 — boundary "to" value
    vector<variant>*    reordered,         // a6 — the ReorderToShortenCriticalPath output (stride 0x48)
    vector<pair<variant*,variant*>>* pairs // a7 — the combinable-pair vector (stride 0x10)
);
```

The boundary `from`/`to` threaded into `Reemit` is the *same* boundary pair `CyclesAddedByXluOperation` priced against: an operand that equals the boundary value was free in the cost and is reconnected (not re-emitted) at boundary fix-up time. The cost model and the emitter agree on what crosses the region edge.

### How the Per-Op Cost Maps to Emitted Cycles

`Reemit` opens with a `RET_CHECK` (line 863) that `reordered` is non-empty iff `pairs` is non-empty, then builds a fresh emission `LloRegionBuilder` and a per-XLU `PerXluState` array (stride `0x20`) — the "currently-set pattern" cache (`[+0x00]`/`[+0x08]` permute source/result, `[+0x10]`/`[+0x18]` segment source/result; full mechanism in [`xlu-op-roster`](../isa/xlu-op-roster.md)). For each combinable pair it emits **one** fused op. The cost-accounting consequence:

| Pair kind | What the cost charged | What Reemit emits | Cycle correspondence |
|---|---|---|---|
| RPU reduce pair (idx 1) | one main edge + per-source boundary-miss penalties | one fused cross-lane reduce sharing a single `Vsetperm`/`Vsetspr` pattern prologue (cached per XLU) | the shared pattern op is the boundary value that was *free* in the cost; the fused body is the one priced main edge |
| Transpose pair (idx 0) | one main edge + `(n−1)` inter-chunk latencies | one fused `Vxpose` re-homing both tiles' instructions, `Vxposeres` per result chunk | `(n−1)` priced chunk latencies = the chunk count the fused transpose occupies (slot-fit gated, [`xpose-reservation-latency`](xpose-reservation-latency.md)) |

> **NOTE — the fusion economy is exactly the free boundary operand.** `CyclesAddedByXluOperation` charges nothing for a source operand that equals `from`/`to`. The `SetPermutePattern`/`SetSegmentPattern` op that two combinable reduces share *is* that boundary value: emitted once per XLU, cached in `PerXluState`, and reused. The cost model and the emitter encode the same economy from two sides — the cost does not pay for the shared pattern, and `Reemit` emits it once. `N` reduces sharing one pattern pay one setup; the cost reflects that by pricing only the per-op main edge plus the *non*-shared source.

After the per-pair emission and the boundary fix-up, `Reemit` deletes every superseded original op (`RemoveNode` over `reordered`, by variant kind) and returns `absl::Status`. The emission and re-home mechanism (factories, `ReplaceUsesOfInstruction`, `PopInstruction`/`AppendInstruction`) is the IR-rewrite concern of [`xlu-op-roster`](../isa/xlu-op-roster.md); the cost concern documented here is that the emitted instruction count and shared-pattern reuse match the cycles the scheduler charged.

---

## The Scheduler State — `PerXluOperations`

### Purpose

`ReorderToShortenCriticalPath` (`@0x126d3460`) is a latency-weighted list scheduler. It owns one `PerXluOperations` record per XLU unit (a `vector<PerXluOperations>` of `xlu_count` elements, allocated `operator new(96 * xlu_count)` at `@0x126d34d1` → stride `0x60`). The record carries the running cost between scheduling steps: the set of pending op-indices, the remaining cycles still to schedule on this XLU, the last-scheduled op (the `prev` the next `CyclesAddedByXluOperation` deltas against), its two source operands (the `from`/`to` the next cost prices against), and the per-XLU completion-time clock (the critical-path frontier the reorder shortens).

### Field Map (stride `0x60`)

Byte-exact from the per-element constructor (`@0x126d3530`/`@0x126d3590`), Phase A accumulation, and the `$_1`/`$_2`/`$_3` lambda accesses.

| Offset | Type | Field | Written by / Read by |
|---|---|---|---|
| `+0x00 .. +0x38` | `absl::btree_set<long, less, alloc, 256>` | per-XLU **pending op-index set** | ctor seeds two `EmptyNode` ptrs (`@0x2181d930`); Phase A inserts indices; `$_1` erases the scheduled index (`@0x126d8ccd`) |
| `+0x38` | `i64` | **remaining running-cycle accumulator** (Σ cost of not-yet-scheduled ops) | Phase A `+= cost[i]` (`@0x126d3942`, `[+56] += v28`); `$_1` `−= cycles[idx]` (`@0x126d8fdc`, `v20[7] -= …`) |
| `+0x40` | `LloValue*` | anchor **"from"** = last-scheduled op's source operand 0 | `$_1` writes `v65[8] = src0` (`@0x126d8f41`); read as `from` by next `$_3`/`CyclesAdded` |
| `+0x48` | `LloValue*` | anchor **"to"** = last-scheduled op's source operand 1 | `$_1` writes `v20[9] = src1` (`@0x126d8f73`); read as `to` by next cost |
| `+0x50` | `variant*` | **last-scheduled op** on this XLU (the `prev`) | `$_1` writes `v20[10] = *op` (`@0x126d8fe3`); read by Phase A and `$_3` (`@0x126d92c2`) |
| `+0x58` | `i64` | per-XLU **completion-time clock** (critical-path frontier) | `$_1` `[+0x58] = max([+0x58] + cost, finish[idx])` (`@0x126d8fad`, `v20[11]`); `$_2` critical-path test `setge` (`@0x126d91f0`) |

The `[+0x08..+0x38)` region was previously read structurally as opaque; it is the `absl::btree_set` internals (root + rightmost `EmptyNode` pointers at `+0x00`/`+0x08`, then size/height/internal). The per-element destructor clears only the btree_set over `[+0x00..+0x38)`; the four scheduler fields `[+0x38..+0x60)` are trivial `i64`/`ptr`.

### How the Cost Flows Through the Record

```c
// ReorderToShortenCriticalPath — Phase A (cost pre-pass, @0x126d3460 body)
for each op i assigned to xlu:
    cost[i] = CyclesAddedByXluOperation(prev=PerXlu[xlu].last_op, cur=op_i,
                                        from=PerXlu[xlu][+0x40], to=PerXlu[xlu][+0x48], tbl);  // @0x126d38f8
    PerXlu[xlu][+0x38] += cost[i];                                      // remaining-cycles accumulator
    PerXlu[xlu].pending.insert(i);                                      // btree_set

// Phase B — per-XLU max-heap priority_queue<pair<long,long>, …, less<>>
heap.emplace({ cost = $_3(op), op_index });                            // @0x126d8980 / push @0x126d3a2c
top = heap.pop();                                                      // longest cost first, ties → higher idx
if ($_2.ready_and_worth(top))  // XluOperationIsReady && [+0x58]+cost >= finish[idx]   (setge @0x126d91f0)
    $_1.commit(top);           // erase pending, advance clock, record anchors+last-op   (@0x126d8b60)
```

The `$_3` marginal-cost lambda (`@0x126d9240`) is a *direct tail-call* into `CyclesAddedByXluOperation` against the XLU's last-scheduled op:

```c
// $_3 @ 0x126d9240 — heap priority for candidate op `cur`
v8 = PerXlu[xlu][+0x50];            // last-scheduled op (prev)
if (v8) {
    if (cur.discr == 1 && prev.discr == 1)      // both RPU: price with boundary from/to
        return CyclesAddedByXluOperation(cur, prev, PerXlu[xlu][+0x40], PerXlu[xlu][+0x48], tbl);  // @0x126d92f4
    else
        return CyclesAddedByXluOperation(prev, cur, 0, 0, tbl);          // @0x126d930a
} else {                            // empty XLU: only a transpose cur contributes
    if (cur.discr != 0) return 0;
    n = cur.read_set_size;          // [cur+0x08]
    return (n < 2) ? 0 : (n-1) * LatencyBetween(readset[0], readset[1]);
}
```

This is the decisive confirmation that the **reorder heap priority, the `AssignXlu` bin-pack key, and the combine DP weight are the same function on the same table**. There is exactly one XLU cost expression, and `$_3` reaches it by tail-call. The `$_1` commit then advances the completion clock by exactly that priced cost (`[+0x58] = max([+0x58]+cost, finish[idx])`) and decrements the remaining-cycle accumulator by the per-op `cycles[idx]`, keeping the two cost views (frontier and remaining) consistent across every scheduled op.

### The Per-XLU Heap Composition

The candidate heap is `priority_queue<pair<long,long>, vector<…>, less<pair<long,long>>>` — a **max-heap**. The pushed pair (`@0x126d3a2c`) is `{first = marginal_cost (from $_3), second = op_index}`. Under `less<pair<long,long>>`, `pop` returns the lexicographically greatest: highest marginal cost first; among equal-cost ops, the higher op-index first. The list scheduler is therefore **longest-marginal-cost-first with a higher-index tie-break** — it schedules the op that adds the most cycles soonest, so its successors' costs are exposed earliest and the critical path is shortened. A pre-test (`cmp heap_top_cost, [PerXlu+0x38]; jl skip`, `@0x126d494b`) only attempts a reorder when the top marginal cost can still shorten that XLU's remaining path — the critical-path-shortening gate the function name describes.

---

## Worked Example — Two `kVectorAddReduceF32` (Pufferfish v4, xlu_count = 2)

1. **Combine.** `ComputeCombinablePairs` emits `{&R_a, &R_b}` — equal `RpuOperationMetadata{0xf7, src0, src1}` ([`xlu-combine-sourcebus`](xlu-combine-sourcebus.md)).
2. **AssignXlu.** `CyclesAddedByXluOperation(prev=null, cur=R_a, …) = 0` (empty-XLU base, `cur != null` but `prev == null` ⇒ main edge needs `prev`; the empty-XLU first op is the base `0`). `R_b` after `R_a` on XLU 0 prices `LatencyBetween(R_b_anchor, R_a_anchor) + (src0 != from ? LatencyBetween(R_b_anchor, src0) : 0) + (src1 != to ? … : 0)`. With `xlu_count = 2`, the main edge is the `ceil(base/2)` PreXlu value — half the serial reduce latency.
3. **Reorder.** `PerXluOperations[0]` initializes: pending btree `{R_a_idx, R_b_idx}`, `[+0x38] = Σcost`, `[+0x58] = 0`. The heap keys `{$_3(R_a), R_a_idx}` and `{$_3(R_b), R_b_idx}`; longest-cost ready op pops first. `$_1` commits `R_a`: erase `R_a_idx`, `[+0x58] = max(0+0, finish)`, `[+0x40]/[+0x48] = R_a.src0/src1`, `[+0x50] = R_a`. `R_b` then prices `$_3 = CyclesAdded(R_a, R_b, R_a.src0, R_a.src1)` — the chained reduce edge.
4. **Reemit.** Pair `{R_a, R_b}`, both RPU, `xlu = 0`. The shared `Vsetperm` pattern op is emitted **once** (the boundary value that was free in the cost); the two reduces collapse into one fused cross-lane reduce body. The emitted-cycle count matches the priced cost: one main edge + the non-shared source penalty.

> **NOTE — segment vs. permute changes the shared pattern, not the cost shape.** A segment-reduce pair (`0xfc`, `LloOpcodeIsSegmentedReduction(0xfc) = true`) shares a `Vsetspr` prologue (cached at `PerXluState[+0x18]`) instead of `Vsetperm`. The cost is identical in shape — one main edge + per-source boundary-miss penalties — because `CyclesAddedByXluOperation` does not branch on segment vs. permute; the distinction is purely in which pattern op `Reemit` shares.

---

## What Is Not Pinned

- The `LatencyBetween` stochastic perturbation enable condition (`LatencyTable[+0x10]` non-null ⇒ `UniformDistribution(0,0x65)` per edge): the arithmetic is CONFIRMED, the default is `0` (deterministic), but the env/flag that toggles the `BitGen` buffer is not isolated. **LOW** for the enable condition.
- The exact `0x84`/`0x82`-band minimum-latency floors inside the public `LatencyBetween`: present and applied to XLU edges, but the per-opcode reach of each band is documented with the latency table, not enumerated here.
- The `AssignXlu` empty-XLU base-case semantics when `cur != null, prev == null`: the body resolves `prev` only when `cur != null` and reaches the `"control != nullptr"` CHECK if `prev == null` at the main-edge point; the empty-XLU price is observed as `0` from the call site, read structurally rather than proven for every variant kind. **MEDIUM** for the empty-XLU base value across all three variant kinds.

---

## Cross-References

- [XLU Op Roster](../isa/xlu-op-roster.md) — the full XLU pipeline, the op-factory set, the `PreXluAssignmentLatencyTable` `ceil(base/xlu_count)` edge, and the `Reemit` IR-rewrite mechanism this page's cost feeds.
- [XLU Combine / Source-Bus](xlu-combine-sourcebus.md) — `ComputeCombinablePairs` (the DP that consumes this cost) and `AssignSourceBus` (the post-emit bus pack).
- [XLU Conflict-Penalty Table](xlu-conflict-penalty.md) — the non-MXU hazard table the shared-bus serialization edge adds to.
- [Transpose-Reservation Latency](xpose-reservation-latency.md) — the `VxposeMode`/`ElementCount` geometry and slot-fit predicate gating the transpose-chain `(n−1)` cost.
- [CycleTable Family](cycletable-family.md) — the throughput half of the cost model the latency axis sits beside.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VII — Cost & Latency Model / XLU cost — [back to index](../index.md)
