# SBUF Allocator — Spill-Cost, Simplify, Select & Spill-Code

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; the cp310/11/12 wheels are byte-identical for `libwalrus.so` but the **with-loop** allocator bodies sit at slightly different VAs in the cp311 `.dynsym` — see the note in [Address Frames](#address-frames). The codenamed addresses below are the cp310 `nm -DC` body frame). All bodies live in `neuronxcc/starfish/lib/libwalrus.so`; for `.text` (`0x62d660..`) and `.rodata` (`0x1c72000..`) the virtual address equals the file offset, and `.data` (VMA `0x3dec320`) is also delta-0 here. Treat every address as version-pinned.*

## Abstract

This page documents the **back half** of the loop-aware SBUF graph-coloring allocator — the part that runs after the interference graph and coalescing partners are built. Its job is to turn an interference graph plus a per-range spill cost into a concrete 2-D placement of every tensor inside the 128-partition State Buffer, or, when that is impossible, to insert the spill/reload DMAs that relieve pressure and re-run the whole colorer until a placement is found.

The allocator is the `ColoringAllocatorWithLoop::Rep::SB_Allocator` family (the `--allocator`/opt6 loop colorer), the variant the driver feeds a `LinearizedFunction`. It is a Chaitin–Briggs colorer with one decisive twist: **the "degree < K" trivial-colorability test is not a scalar count**. SBUF is a 2-D space (partition × byte), so "K colors" is "the number of legal partition×byte rectangles a tensor's shape admits", and a neighbor does not subtract `1` from that budget — it subtracts a *geometric* amount that depends on how the two tensors' partition-band shapes overlap. The simplify phase computes that geometric residual capacity, the select phase pops the resulting node stack and lays each range into a concrete rectangle by first-fit interval search, and `insert_spill_code` materializes the DRAM home plus the SB reload copy for anything that did not fit, then the driver loops to a fixpoint.

This page is the consumer of the front half — liveness, the interference adjacency (`Info+56`/`+64`), `find_costs` (which stamps `Info+0`), and the coalesce partner sets (`Info+48`) — and it hands its product (a placed `LinearizedFunction` or a spilled one) back to the allocator driver's spill loop. Four mechanisms are the deliverable, and each is grounded on the binary below:

- **The Hwm-latency spill cost with the `+inf` must-not-spill sentinel.** A range's spill cost is a pure sum of hardware *access latencies* read from the `bir::Hwm` cost oracle — there are zero hard-coded float weights in the cost function. A range that must never be spilled gets the literal IEEE-754 `+inf` (`0x7FF0000000000000`), and "infinite" is detected downstream as `> DBL_MAX`.
- **The geometric degree < K simplify.** `cap = possible_placements(node) − Σ impact(node, neighbor)`; nodes with `cap > 0` are trivially colorable and pushed; when none are, the colorer optimistically pushes the `argmin(cost/cap)` spill candidate and continues.
- **The 2-D rectangle select with partition-band reservation.** The node stack is popped in reverse, and each range is assigned a concrete `{partition-band × byte}` rectangle by `search_top_down`/`search_bottom_up` first-fit over a per-band free-interval list; each placement immediately reserves its rectangle for the next pop.
- **`insert_spill_code` + the spill fixpoint.** Un-placeable ranges get a DRAM home and an SB reload copy; a liveness pass eliminates redundant reloads; then the driver re-runs the *entire* colorer on the augmented function until select reports no spills (or the hard spill cap fires).

| | |
|---|---|
| **Allocator family** | `ColoringAllocatorWithLoop::Rep::SB_Allocator` (the `--allocator`/opt6 loop colorer) |
| **Spill cost** | `find_costs` @ `0xaa12c0` (loop) / `0x9e3ff0` (flat sibling) |
| **`+inf` sentinel** | `0x1DBCEB8` = bytes `00 00 00 00 00 00 F0 7F` = `0x7FF0000000000000` |
| **Geometric K** | `possible_placements` @ `0xab5040`; per-neighbor `impact` @ `0xab5000` |
| **Impact LUT** | `vertical_impact_with_loop` @ `0x3ded040` — 9×9 int32, values ∈ {0,1,2,4} |
| **Simplify** | `simplify(LinearizedFunction*, Info*, uint)→NodeStack*` @ `0xab58c0` |
| **Select** | `select(LinearizedFunction*, Info*, vector<int>*, double*)→Locations*` @ `0xaafa30` |
| **First-fit** | `search_top_down` @ `0xaaedf0`, `search_bottom_up` @ `0xaae920`, dispatch `search_intervals` @ `0xaaf700` |
| **Spill code** | `insert_spill_code` @ `0xac87d0`; reload elim `fast_liveness_analysis` @ `0xac2c60` → `find_redundant_reloads` @ `0xabeea0` |
| **Fixpoint driver** | `allocate(LinearizedFunction*)` @ `0xa95310` |
| **Spill cap** | `cmp DWORD PTR [Rep+0x2fc], 0x10000` @ `0xacc7fd` (65536 reloads → "turning optimizations off") |

---

## Where this sits in the colorer

The driver `allocate` @ `0xa95310` runs one Chaitin–Briggs round per spill iteration. Each round walks the front-half passes (liveness and interference — the `live_range`/`build` calls, documented in the front-half page **SBUF Liveness / Interference / Coalesce** — and coalesce partners), then this page's four passes, then the spill insertion that closes the loop:

```text
allocate(lf):                                   @0xa95310   (loop top = 0xa95b50)
  do {
    renumber_locations(lf)                       // re-densify ids after spill mutated the memloc set
    find_partners(lf, info)                      // coalesce candidates → Info+48 (front half)
    find_first_defs / find_last_uses / find_loads
    live_range(lf, info, …)                      // front half: liveness over instruction points
    build(lf, info, …)                           // front half: interference adjacency Info+56/+64
    find_costs(lf, info, …)         @0xaa12c0     // §1: cost@Info+0, +inf for un-spillable
    NodeStack* S = simplify(lf, info, 0)  @0xab58c0  // §2: geometric degree<K → node stack
    Locations* L = select(lf, info, S, &score) @0xaafa30 // §3: pop S, 2-D rectangle place
    if (*(int*)(L+8) == 0)  break;               // need-spill flag == 0 ⇒ fully colored, COMMIT
    create_eintervals(lf, info, …)  @0xac14a0    // placement intervals for the un-placeable set
    lf = insert_spill_code(lf, spillset, info, Homes) @0xac87d0  // §4: spill stores + reload copies
    ++iterationCounter                           // "Number of iterations in SB spills do loop:"
  } while (true);                                // back-edge re-colors the spilled lf'
```

The `select` return value is a `Locations*` (a `tc_new(24)` struct); the word at `Locations+8` is the **need-spill flag** that controls the loop — non-zero means some `Info+121` (the "allocated" bit) is still `0`, i.e. select could not place every range. (CONFIRMED — the `test edx,edx ; je 0xa97135` decision at `0xa95fb8` reads `*(Locations+8)`.)

### Address frames

The cp310 `nm -DC` table places these bodies at the addresses cited throughout (e.g. `simplify` @ `0xab58c0`, `select` @ `0xaafa30`, `find_costs` @ `0xaa12c0`, `insert_spill_code` @ `0xac87d0`). The cp311 `.dynsym` export reports the *with-loop* family 0x30 lower (`simplify` @ `0xab5890`, `select` @ `0xaafa00`, …) — the two-VA-frame artifact (a per-symbol prologue/frame offset, **not** a wheel-wide delta: the flat-sibling `SB_Allocator::select` @ `0xa07990` and `simplify` @ `0xa1b4a0` are identical in both frames). This page uses the cp310 body frame as canonical. (CONFIRMED — cross-checked cp310 `nm -DC` body addresses against the cp311 dynsym export.)

The flat (non-loop) `SB_Allocator` is the default opt1–3 colorer and the TBB-parallel sibling of the same algorithm; its addresses are noted where they differ but it is not the subject of this page.

---

## §1 — The Hwm-latency spill cost and the `+inf` sentinel

The spill cost answers "how many machine cycles do we pay if this range lives in DRAM instead of SBUF?". It is computed by `find_costs` and written to the cost slot of each node's `Info` record. The decisive design fact: **the cost is a pure sum of hardware access latencies, with no float multipliers baked into the allocator.** A disassembly of the flat `find_costs` @ `0x9e3ff0` contains **zero** `mulsd`/`divsd`-by-literal and zero `movsd`-from-`.rodata` weight constant — the only literal double it touches is the `+inf` sentinel. (CONFIRMED — `objdump` of `0x9e3ff0..0x9e6400` shows no float-weight literals; the latency numbers arrive through `bir::MemoryLocation::getLatency`, the per-arch `bir::Hwm` oracle, documented in the planned **Hwm / PerfSim Cost** page.)

### The latency tiers

`find_costs` accumulates one latency term per access point. The tier is selected by a `MemoryType` immediate handed to `getLatency`, and only two distinct tiers appear (the `MemoryType` enum is `DRAM = 8`, `SB = 16`):

```c
// flat SB_Allocator::find_costs @0x9e3ff0 — the two getLatency MemoryType sites:
cost(n) = Σ_{uses u}  L(u)  +  Σ_{defs d}  L_SB
  where  L(u) = getLatency(DRAM=8)  if u lives inside a loop body   // mov esi,0x8 @0x9e4713
              = getLatency(SB=16)   otherwise                       // mov esi,0x10 @0x9e4df9
  (+ one extra L_SB per rematable-load use when --gca-dma-optlevel is aggressive,
     gated by [Info+0x414] > 4)                                     // mov esi,0x10 @0x9e58d3
```

The "loop-depth multiplier" that earlier guesses posited does **not** exist as a power `K^depth`. The flat allocator's loop weighting is a **binary switch**: a use inside a loop body pays a full DRAM round-trip latency (`getLatency(8)`), a use outside pays the on-chip SBUF latency (`getLatency(16)`). The in-loop test is a `CFG::isLoopBody` membership query, not a depth integer. (CONFIRMED — the two `mov esi` immediates `0x8` and `0x10` at `0x9e4713`/`0x9e4df9` are byte-verified; this page documents the loop family, which feeds on the same cost slot.)

The loop-aware family `find_costs` @ `0xaa12c0` differs only in that every term is scaled by `getLiveN` (the live-element count from `bir::MemoryLocationSet::getLiveN`), so larger tensors carry proportionally larger spill cost — and it has no separate `isLoopBody` term because its input is already a linearized function in which loop bodies are laid out per their trip-count occurrence. A disassembly of `0xaa12c0` likewise shows no float-weight literal. (STRONG — corroborated by the PSUM sibling's `"need load, add load cost, add to needSave"` / `"add store cost, remove from needSave"` log strings; the exact `<<7`/`+0` cost-slot view onto the flat allocator's `<<6`/`+160` slot is a struct-view detail, the *value* is identical.)

### The `+inf` must-not-spill sentinel

The finalize step of `find_costs` overwrites the accumulated cost in three cases, the second and third of which write the **literal IEEE-754 `+inf`** — the un-spillable marker:

```c
// finalize, per node:
if   (Info+268 /*spill/output/load tensor*/)  cost(n) = (double)getLatency(SB);  // base reload
elif (Info+270 == 1 /*never touched by a real use*/)  cost(n) = +inf;            // spilling can't help
elif (pseudo_inf(n))                                  cost(n) = +inf;            // pinned / un-spillable
```

The constant is read directly off `.rodata`:

```text
qword @ 0x1DBCEB8 :  00 00 00 00 00 00 F0 7F   =  0x7FF0000000000000  =  +infinity
  loaded into the cost slot at finalize:  mov rax/rsi, [rip → 0x1dbceb8]  @0x9e612c / @0x9e61a7
```

(CONFIRMED — the eight bytes were read directly from the cp310 binary at file offset `0x1DBCEB8`; they decode to `+inf`, and `find_costs` loads that qword into the cost slot at the two finalize sites.)

The un-spillable predicate is tiny:

```c
// SB_Allocator::pseudo_inf(Info&) @0x9cfb50  (flat family — full body)
bool pseudo_inf(Info &n) { return n[+48] ? true : n[+49]; }   // pin#1 || pin#2
```

A node is un-spillable when **either** pin flag is set, **or** (flat family) when it was never touched by a real use (`Info+270` still `1` — spilling a never-used range relieves no pressure), **or** (loop family) when its live range collapses to a single program point (`writer_order − reader_order == 1`). (CONFIRMED — `pseudo_inf` body; the single-point live-range pin is from the loop `find_costs` finalize.)

Crucially, the *stored* value is `+inf`, but the downstream **eligibility test** is a comparison against `DBL_MAX`:

```c
//  |cost(n)| <= 1.7976931348623157e308 (DBL_MAX)  ⇒  FINITE ⇒ spillable
//  appears in simplify @0xa1b4a0 and select @0xa07990; the +inf value fails it ⇒ routed INFINITE
```

So `+inf` (`0x7FF0…`) is the value, `> DBL_MAX` is the predicate — the two must not be conflated. The pinned/infinite ranges are collected into a separate worklist that the colorer asserts empty before it gives up. (CONFIRMED — the `DBL_MAX` literal is the comparison bound in both phases.)

### The Chaitin spill-priority metric

When the graph is stuck (no trivially-colorable node), the colorer must pick which range to optimistically spill. The picker minimizes a Chaitin–Briggs ratio of cost over degree. The loop simplify carries three modes selected by its `uint` argument:

```c
// Rep::SB_Allocator::simplify @0xab58c0 — the spill-candidate metric:
mode 0 (the ONLY mode the driver uses):  argmin  cost(+0) / cap(+72.lo)      // cost / geometric residual
mode 1:                                  argmin  cost(+0) / cap²
mode 2:                                  argmin  cost(+0) / degreeMarker(+76)²
```

The degree-squared variants are real in the binary — the squaring `imul` (`imul edx,edx` ×3 plus one `imul edi,edi`, four total) appears in `simplify` and `divsd` (the `cost/deg²` division) six times — but the driver always passes `mode 0`, the classic `cost/degree` with the *geometric* residual capacity as the degree. (CONFIRMED — the `imul` and `divsd` counts are from a disassembly of `0xab58c0..0xab8400`; the driver call site passes the third argument `0` via `xor ecx,ecx` @ `0xa95eaa`.)

A range whose uses live inside loop bodies accrues higher cost (DRAM-tier or liveN-scaled terms), so its `cost/cap` ratio is high and it is *disfavoured* as a spill candidate — this is exactly the loop-bias the cost model injects, realized as a cost weight rather than a separate ordering rule. (STRONG.)

---

## §2 — The geometric degree < K simplify

The simplify phase is Chaitin's node-removal worklist, but with a geometric notion of degree. The output is a `NodeStack` (a `std::vector<int>` of node ordinals) that the select phase pops in reverse to assign colors.

### Why K is geometric

In scalar register allocation a node is trivially colorable when its interference degree is `< K` (the register count). Here a "color" is a legal placement of a tensor into the 2-D `{partition-band × byte}` SBUF space, and a tensor's *shape* determines how many placements it admits and how much budget each neighbor removes. Two functions encode that:

```c
// possible_placements(Info*, node) @0xab5040  =  the geometric K (legal placement count):
base = NumPartitions + 1 − height(node+80)        // a height-h band slides over P+1−h partition rows
switch (shapeClass(node+116)) {                    // 0..8 partition-band occupancy class
  case 0:        return 4*base;   // quarter-partition tensor → 4× as many placements
  case 5:        return 2*base;   // half-partition tensor    → 2× placements
  case 6,7,8:    return   base;   // full / wide classes      → 1× placements
  default:       return   base;   // (>8 logs "Unexpected value for info[xIndex].height")
}

// impact(Info*, node, neighbor) @0xab5000  =  how much of node's K-budget neighbor consumes:
return vertical_impact_with_loop[ 9*shapeClass(neighbor) + shapeClass(node) ]
       * ( bandSpan(node) + bandSpan(neighbor) − 1 );
```

The `9*sc_m + sc_n` index into `vertical_impact_with_loop` is the partition-axis conflict weight between two shape classes, and the `(h_n + h_m − 1)` factor is the combined byte-band overlap. The LUT's likely layout (9×9 int32 over the `{0,1,2,4}` value set) is:

```text
vertical_impact_with_loop @0x3ded040  (9×9 int32, 324 bytes, values ∈ {0,1,2,4}):
  row 0 (class-0 / quarter):  1 1 1 1 1 1 1 1 1
  row 1:                      1 1 0 0 0 1 1 0 1
  row 2:                      1 0 1 0 0 1 1 0 1
  row 3:                      1 0 0 1 0 1 0 1 1
  row 4:                      1 0 0 0 1 1 0 1 1
  row 5 (half):               2 1 1 1 1 1 1 1 1
  row 6:                      2 1 1 0 0 1 1 0 1
  row 7:                      2 0 0 1 1 1 0 1 1
  row 8 (full/tall):          4 1 1 1 1 2 1 1 1
```

The `0` entries are shape-class pairs that *do not* conflict in the partition dimension (they can coexist on different partition bands), the `2`s are half×half pairs, and the `4` in the class-0 axis is the quarter-block multiplier.

> **CORRECTION (#827 audit).** The 9×9 cell values above are **INFERRED**, not CONFIRMED — an earlier draft over-claimed they were "dumped byte-for-byte" / "read directly from the cp310 binary." `impact` @ `0xab5000` reaches the table through a relocated GOT pointer (`mov rcx, cs:vertical_impact_with_loop_ptr` @ `0xab5024`, then `imul eax,[rcx+rdx*4]` with `rdx = 9·Height`), so the symbol, the `.data` residence at `0x3ded040`, the `9·sc+sc` index arithmetic, and the `{0,1,2,4}` value range are CONFIRMED, but the individual integers live in `.data` behind the relocation and are **not byte-recoverable from this corpus** (absent from `rodata.bin` and `data_tables.json`). The sibling front-half page [SBUF Liveness / Interference](sbuf-liveness-interference.md) tags the same table INFERRED; this page is now consistent with it.

### The trivially-colorable test and the three worklists

```c
// simplify @0xab58c0 — geometric degree<K classification:
cap = possible_placements(node);                  // = geometric K
for each neighbor m in adj(node):                  // adj at Info+56, count Info+64
    cap    -= impact(node, m);                      // subtract geometric degree
    revAcc += impact(m, node);                      // reverse direction, bookkeeping
node[+72] = pack(cap, revAcc);
if      (cap > 0)                       → SAFE      // trivially colorable
else if (fabs(cost) <= DBL_MAX)         → UNSAFE    // finite cost ⇒ spillable
else                                    → INFINITE  // cost == +inf ⇒ un-spillable
```

`SAFE`, `UNSAFE`, and `INFINITE` are three `llvm::SparseSet` worklists. This is where the `+inf` sentinel pays off: a `+inf`-cost range fails the `<= DBL_MAX` test and is routed into `INFINITE`, never considered as a spill candidate. (CONFIRMED — the three-way classification with the `fabs(cost) <= DBL_MAX` test.)

### The drain loop and the optimistic spill

```c
NodeStack S;                                        // std::vector<int>
// phase 1: drain all trivially-colorable nodes
while (SAFE not empty) {
    n = SAFE.pop();  S.push_back(n);  node[n].removed(+76) = 1;
    for each live neighbor m of n:                   // decrement m's residual capacity
        m.cap -= impact(n, m);
        if (m.cap > 0 && !m.preallocated) {          // m just became trivially colorable
            UNSAFE.erase(m); INFINITE.erase(m); SAFE.insert(m);   // promote
        }
}
S.push_back(-1);                                     // sentinel: "definite" prefix ends here
// phase 2: when stuck, optimistically push the cheapest spill candidate
while (UNSAFE not empty) {
    c = argmin_{n in UNSAFE} cost(n) / cap(n);        // +123 spillable bit breaks ties
    c.potentialSpill(+126) = 1;                       // mark, but DON'T spill yet
    UNSAFE.erase(c); SAFE.insert(c);                  // feed it back as if colorable
    …re-run the drain loop (removing c may re-trivialize neighbors)…
}
// phase 3: flush INFINITE; force-push each remaining un-spillable node + its partner set
for each n still in INFINITE:  S.push_back(n);  follow Info+48 partner set, force-push each
assert(INFINITE.empty());  return S;
```

The push order is the *removal* order, which is the **reverse** of the select assignment order — a classic Chaitin optimistic ordering, so the last (most constrained) node removed is colored first. The `-1` sentinel separates the definite prefix from the optimistic spill-candidate suffix, and the select phase re-seeds its band watermarks when it pops it. The phase-3 partner force-push keeps coalesced groups adjacent on the stack so select assigns them together. (CONFIRMED — the LIFO push/pop relationship, the sentinel, and the partner force-push.)

---

## §3 — The 2-D rectangle select and band reservation

`select` pops the node stack and assigns each range a concrete `{partition-band × byte-offset}` rectangle. The geometric primitive is a 12-byte `Interval`:

```c
struct Interval {            // 12 bytes (vector stride 12)
    uint32 lower;   // +0   byte-offset START within a partition (the byte axis)
    uint32 upper;   // +4   byte-offset END (exclusive)
    uint16 tag;     // +8   partition-band tag: high nibble = band class
                    //      (0x80=full-128, 0x40=64-part, 0x20=32-part), low bits = band index
};
```

The partition axis is carried by the `tag` (the band a free byte-range belongs to); the byte axis is `[lower, upper)`. A rectangle is `{band from tag} × {byte [lower,upper)}`.

### The pop loop and band watermarks

```c
// select @0xaafa30:
for (k = 0; k < numNodes; ++k)  info[k].allocated(+121) = 0;   // un-allocate all
budget = *(uint32*)(LF+696);                                    // = SB_SIZE  (per-partition byte cap)
// seed seven rolling band watermarks LF[168..174] from aligned(budget · {1/48, 3/4, 1, …})
*heuristic = 0.0;
v = stack.end();
while (v != stack.begin()) {
    node = *(--v);                                              // POP from the BACK (reverse Chaitin order)
    if (node < 0) { re-seed watermarks to aligned(budget>>2 / >>1 / ·1); continue; }   // the -1 sentinel
    I = info + node;                                            // 128-byte stride
    assert(!I.allocated && !I.preallocated && "node should be unallocated when popped from stack");
    …place I (below)…
}
```

> **NOTE — the `LF+696` field.** Earlier reports flagged `LF+696` (= `allocator+0x2b8`) as ambiguous between "SB_SIZE bytes" and "NumPartitions". The [SBUF/PSUM geometry page](../arch/sbuf-psum-geometry.md) resolves it: `allocator+0x2b8` holds `SB_SIZE` **bytes** (read from `Statebuf+0` at allocator-ctor time), and the 128-partition count is the separate `Statebuf+0x8` field. So `select`/`search_*` read `LF+696` as the byte budget (consistent with the `"upper <= SB_SIZE"` asserts), while `possible_placements` reads `NumPartitions` from a different slot. The two are distinct fields of one geometry record. (STRONG — resolved cross-page.)

The seven `LF[168..174]` watermarks split SBUF into a low band (small tensors pack bottom-up) and a high band (large tensors pack top-down), which is the fragmentation-reduction strategy. Byte coordinates are 8-byte aligned via `aligned` @ `0xa929a0` (`round-up-to-multiple-of-8` — the SBUF access granule). (CONFIRMED — `"node should be unallocated when popped from stack"` is present in the binary; the watermark seeding and the alignment-to-8.)

### Per-node placement: the size-class cascade

Each popped node carries a height class (`Info+116`, 0..8) and a byte extent (`Info+80`, bytes-per-block). The running free space is decomposed into TALL / MEDIUM / SHORT free-`Interval` pools by partition-band class, and the cascade tries successively wider pools until a fit is found:

```c
switch (Info+116 /*height class*/) {
  case 0:      find_medium→find_short→search; else find_tall→find_short→search; else find_short→search
  case 1..4:   find_medium→find_short→search; else find_tall→find_short→search; else find_short→search
  case 5:      find_tall→find_medium→search; else find_medium→search
  case 6,7:    find_tall→find_medium→search; else find_medium→search
  case 8:      find_tall→search                       // tallest: full-height pool only
  default:     LOG "Unexpected value for info[node_index].height" → abort
}
```

The partition bands are the four 32-partition quadrants `[0,32) [32,64) [64,96) [96,128)` (pool tags `0x200000`/`0x200020`/`0x200040`/`0x200060`), the half-band split at 64 (`find_medium`, tag `0x400000`), and the full 128-partition band (`find_tall`, tag `0x800000`). This is the same 4-band partition geometry the front-half and geometry pages report. (CONFIRMED — the band tag math and the height-domain abort string.)

### First-fit interval search

`search_intervals` @ `0xaaf700` selects packing direction from the band watermarks, then delegates to one of two first-fit walks over a sorted `vector<Interval>` free-list:

```c
// search_top_down @0xaaedf0 — pack from the TOP (high byte offsets), place at upper−size:
for (it = free.end(); it != free.begin(); it -= 12) {
    if (!(window overlaps [it.lower, it.upper))) continue;
    assert(it.upper <= *(uint32*)(LF+696) && "upper <= SB_SIZE");   // byte-capacity bound
    hi = min(windowUpper, it.upper);  lo = max(windowLower, it.lower);
    if (hi - lo >= size) { out = { hi-size, hi, it.tag }; return true; }   // place at top of slot
}

// search_bottom_up @0xaae920 — pack from the BOTTOM (low offsets), place at lower:
for (it = free.begin(); it != free.end(); it += 12) {
    if (!(window overlaps [it.lower, it.upper))) continue;
    assert(it.upper <= *(uint32*)(LF+696) && "upper <= SB_SIZE");
    lo = max(windowLower, it.lower);  hi = min(windowUpper, it.upper);
    if (size <= hi - lo) { out = { lo, lo+size, it.tag }; return true; }   // place at bottom of slot
}
```

The collision test is purely the byte-axis overlap *inside* each candidate free interval; the partition axis is implicit in which pool (band) the interval came from. (CONFIRMED — both walks, the `"upper <= SB_SIZE"` byte-capacity asserts, and the place-at-`upper−size` vs place-at-`lower` direction.)

### Reservation: marking the rectangle occupied

On a fit, select records the placed rectangle per partition-block onto the node's two output arrays (`Info+96` byte-offsets, `Info+104` partition-tags), then **pushes the placed rectangle back into the running free-list and re-sorts it** — that re-sort *is* the loop colorer's partition-band reservation table:

```c
// on a fit:
record (out.lower, out.upper, tag=Info+114) into the per-node placed list
src.push_back(placed_rectangle);  std::sort(src.begin, src.end, compareStarts);  // RESERVE for next pop
// compareStarts: lower ascending, tie ⇒ tag descending (wider band first)
on the last block:  Info+121 = 1;   // node allocated
```

The loop colorer keeps **one** flat sorted free-list and re-derives the band pools on demand; the **flat/scalar** sibling instead materializes an explicit `std::array<std::vector<std::pair<int,int>>, 4>` — the literal `partitionBandReservations` table, one sorted reserved-byte-interval vector per 32-partition quadrant, in `selectNode` @ `0xa05250` (asserts `"boundIter != partitionBandReservations.end()"` / `"intersectionIter != reservations[otherPartitionBandIndex].end()"`). Both realize the same 4-band 2-D reservation model. Per the front-half **reservation-table disambiguation**, this table belongs to the *allocator*, not any scheduler. (CONFIRMED — the `src` re-sort-after-insert in the loop colorer; the `partitionBandReservations` asserts in the flat `selectNode`.)

After all blocks are placed, select runs a paranoid all-pairs check that no two committed rectangles intersect (byte-overlap ∧ partition-overlap → `"in sb_select, a bad allocation"` → abort) — the explicit statement of the geometric-coloring conflict model. (CONFIRMED — `"in sb_select, a bad allocation"` is present in the binary.)

### The heuristic score

`*heuristic` (the `double*` out-param) accumulates the spill cost of every range that *failed* to place:

```c
// on NO fit:  *heuristic += *(double*)(Info+0);   // the node's precomputed spill cost
//             if (cost == +inf)  LOG "infinite node <name>"
```

A run that placed everything leaves `*heuristic == 0.0`. The allocator driver's best-of-n loop perturbs the simplify/select order and keeps the run with the **lowest** `*heuristic` — the cheapest set of spills. (CONFIRMED — the per-unplaced-node `*heuristic += Info+0` accrual and the `"infinite node"` log.)

The epilogue asserts every node placed (`"info[i].allocated"`), sets the `MemoryLocationSet.allocated` flag, and commits each address to the IR via `bir::MemoryLocation::allocate()`. If select could not place every range, the un-placed set becomes the `spillset` the next phase consumes.

---

## §4 — `insert_spill_code` and the spill fixpoint

When select reports a non-zero need-spill flag, the un-placeable ranges (`spillset`) are handed to `insert_spill_code` @ `0xac87d0`. Its job is to give each spilled range a DRAM home, mint the SB working copy that will be reloaded at each use, eliminate redundant reloads, then return the augmented function for the driver to re-color.

```c
LinearizedFunction* insert_spill_code(
    LinearizedFunction* lf,
    DenseSet<MemoryLocationSet*>* spillset,             // the un-placeable nodes from select
    Info* info,
    DenseMap<MemoryLocationSet*, MemoryLocationSet*>& Homes);  // out: spilled-value → DRAM home
```

### Per-spill materialization

```c
find_split_first_defs_for_spills(info, spillset);       // split multi-def first-defs so each def spills
for each spilled MemoryLocationSet m in spillset:
    collect_def_use(inst, m, &defs, &uses);             // gather m's def(s) + all uses, linear order
    find_insertion_pos(m, use, /*is_reload=*/1, …);     // BB-local splice point; asserts MemoryType==SB(16)
    find_next_user(linst, m);                           // next downstream use (extend/coalesce reload)
    // --- spill STORE side (value SB → DRAM home) ---
    serial = ++Rep[0x2f8];
    copyInto(m, name="<m>_SpillSave<serial>", MemoryType=DRAM(8), …);   // mint DRAM home  (edx=0x8)
    try_emplace(Homes, m → new_dram_home);              // record the spill→home binding
    // --- reload COPY side (the SB working copy re-introduced at the use) ---
    serial = ++Rep[0x2fc];
    copyInto(m, name="<m>_ReloadStore<serial>", MemoryType=SB(16), …); // mint SB reload copy (edx=0x10)
    // insert InstSave (SB→home) ; setDebugInfo
```

The two `MemoryType` immediates are byte-verified: the `_SpillSave` DRAM home is minted with `mov edx,0x8`, the `_ReloadStore` SB copy with `mov edx,0x10`. The `_ReloadStore` SB copy is a short live range that **re-enters liveness/interference/select on the next iteration** — that is the engine of the fixpoint. (CONFIRMED — `mov edx,0x8` and `mov edx,0x10` at the copyInto call sites; the `_SpillSave`/`_ReloadStore` name strings are present in the binary.)

A spill is an `InstSave` and a reload is an `InstLoad` (asserts `"Reload/Spill instruction has to be of type Load or Save"`); per the front-half/DMA pages, Load = Save = DMACopy collapse to one wire-opcode. (CONFIRMED-name.)

> **Where the reload *load* is materialized.** `insert_spill_code` mints the DRAM home, the SB reload copy, and the spill *store* — but the per-use reload `InstLoad` itself (named `_Reload` / `_ReloadTensorCopy` / `_ReloadPartial`) is emitted **downstream** by `Rep::indice_legalization` @ `0xa8eb00`, once physical addresses are fixed, from the `Homes` bindings. (STRONG — those reload-name strings have no xref inside `insert_spill_code`; their only with-loop xrefs fall inside `indice_legalization`.) The PSUM and flat siblings do the reload load in-place instead.

### Redundant-reload elimination

After insertion, a liveness pass removes reloads whose value is still resident in SB:

```c
fast_liveness_analysis(lf, info, …)  @0xac2c60     // "compute liveness for each instruction point"
                                                    // "potential redundant reloads"
  └─ find_redundant_reloads(reload_needed, reload_groups, info, …)  @0xabeea0   (called ×2)
        // for each candidate reload: if the value is still SB-resident at this point
        //   (reload_needed[id]==false) erase it ("restores erase" / "no more remove");
        // group reloads of the same value at nearby uses and coalesce ("replace reload location");
        // repoint erased reloads' home pointer to the surviving reload's home.
        // getLoopnest() gates loop-nest cases: a reload inside a deeper loop nest than the value's
        //   residence cannot be removed (the back-edge re-kills the SB copy).
```

`find_next_user` @ `0xabdd80` (walks the instruction chain to find the next downstream use of `m`) feeds the coalescing by extending one reload's live range across several consecutive uses. `legalize_predicates` @ `0xac64c0` then runs `correct_predicates` per inserted DMA, inheriting the defining instruction's `AffinePredicate` so a value defined under a guard is only spilled/reloaded when the original def fired. (STRONG — the elimination strings and the `getLoopnest`/`find_redundant_reloads` ×2 call structure; `correct_predicates` is live via `legalize_predicates`. Note: the SB-with-loop `need_reload` @ `0xabdfe0` and `correct_spill_reload` @ `0xac6d10` are exported but **dead** in this build — the liveness-based path replaced them.)

### The spill cap

Both `copyInto` sites are gated by a hard ceiling on the reload-store serial counter:

```text
cmp DWORD PTR [Rep+0x2fc], 0x10000    @0xacc7fd   ; je → "Warning: too many spills required,
                                                  ;        turning optimizations off"
```

When the `_ReloadStore` serial reaches 65536, the colorer disables further optimization rather than looping forever — the reload-store counter doubles as a hard spill-count ceiling. (CONFIRMED — the `cmp …,0x10000` immediate appears twice in the `insert_spill_code` body, paired with the warning-string branch.)

### The fixpoint

`insert_spill_code` returns the augmented `LinearizedFunction`, and the driver loops back to the top:

```text
++iterationCounter            // "Number of iterations in SB spills do loop: <N>"  (str @0x1ccf008)
jmp loop-top (0xa95b50)       // re-run renumber → live_range → build → find_costs → simplify → select
                              //   on lf'; the fresh _ReloadStore SB ranges re-enter the colorer.
```

The loop terminates one of three ways:

1. **Fixpoint** — select reports need-spill `== 0` (everything placed). The driver commits, sets a per-`Function` attribute (`no_spill`/`ready_for_codegen`), logs `"no more spills"` plus the spilled-tensor count and total bytes/partition, and returns. (CONFIRMED — the `je 0xa97135` commit branch.)
2. **Spill cap** — the `Rep+0x2fc == 0x10000` ceiling fires inside `insert_spill_code`, turning optimizations off.
3. **Failure** — select fails *and* the spill set is empty or all-infinite-cost (nothing left to spill): `"couldn't allocate every tensor in SB and spilling can't help"`. (CONFIRMED-name — the failure string is present.)

DRAM home offsets for the `_SpillSave` homes are bumped downstream by the `DRAM_Allocator` (the `DramSpillSpace`/`DramAlignedSpillSpace` metrics), not in this driver.

---

## Function map

| Symbol (`ColoringAllocatorWithLoop::Rep::SB_Allocator::` unless noted) | Body (cp310) | Role |
|---|---|---|
| `allocate(LinearizedFunction*)` | `0xa95310` | the spill-fixpoint driver (best-of-n + do-loop) |
| `find_costs(LinearizedFunction*, Info*, …)` | `0xaa12c0` | spill cost; `+inf` sentinel; liveN-scaled (loop family) |
| `SB_Allocator::find_costs(Function*, …)` (flat) | `0x9e3ff0` | flat sibling; DRAM(8)/SB(16) latency tiers |
| `SB_Allocator::pseudo_inf(Info&)` (flat) | `0x9cfb50` | un-spillable predicate `pin#1 || pin#2` |
| `simplify(LinearizedFunction*, Info*, uint)→NodeStack*` | `0xab58c0` | geometric degree<K; 3 worklists; spill picker |
| `possible_placements(Info*, uint)` | `0xab5040` | geometric K = `(NumPartitions+1−height) × {4,2,1}` |
| `impact(Info*, uint, uint)` | `0xab5000` | per-neighbor budget = `LUT[9·sc_m+sc_n] · (h_n+h_m−1)` |
| `vertical_impact_with_loop` (data) | `0x3ded040` | 9×9 int32 conflict LUT, values {0,1,2,4} |
| `select(LinearizedFunction*, Info*, vector<int>*, double*)→Locations*` | `0xaafa30` | pop stack; 2-D rectangle place; reserve; heuristic |
| `search_intervals(Interval&, …, bool, bool)` | `0xaaf700` | first-fit direction dispatch (band watermarks) |
| `search_top_down(Interval&, …)` | `0xaaedf0` | pack from top, place at `upper−size` |
| `search_bottom_up(Interval&, …)` | `0xaae920` | pack from bottom, place at `lower` |
| `aligned(uint)` | `0xa929a0` | round byte offset up to multiple of 8 |
| `SB_Allocator::selectNode(…)` (flat) | `0xa05250` | flat scalar place; `partitionBandReservations` table |
| `insert_spill_code(LinearizedFunction*, DenseSet*, Info*, DenseMap& Homes)` | `0xac87d0` | mint DRAM home + SB reload copy; eliminate reloads |
| `fast_liveness_analysis(…)` | `0xac2c60` | redundant-reload liveness driver |
| `find_redundant_reloads(…)` | `0xabeea0` | erase still-resident reloads; coalesce groups |
| `find_next_user(LinearizedInstruction*, MemoryLocationSet*)` | `0xabdd80` | next downstream use (extend reload range) |
| `legalize_predicates / correct_predicates` | `0xac64c0` / `0xac5cb0` | inherit `AffinePredicate` onto spill/reload DMAs |
| `indice_legalization(LinearizedFunction*, Function*)` | `0xa8eb00` | downstream: materialize `_Reload` InstLoads from `Homes` |

## Diagnostic strings

| String (`.rodata`) | Phase / meaning |
|---|---|
| `spilling from SB cost about <X> cycles` | spill candidate chosen; cost in Hwm cycles |
| `Number of iterations in SB spills do loop: <N>` | the fixpoint iteration count |
| `<m>_SpillSave<N>` / `<m>_ReloadStore<N>` | DRAM home / SB reload copy names (counters `Rep+0x2f8` / `+0x2fc`) |
| `_Reload` / `_ReloadPartial` / `_ReloadTensorCopy` | per-use reload loads (minted in `indice_legalization`) |
| `Warning: too many spills required, turning optimizations off` | the `0x10000` spill-cap bail |
| `in sb_select, a bad allocation` | all-pairs rectangle-intersection check failed (abort) |
| `node should be unallocated when popped from stack` | select pop-loop sanity assert |
| `Unexpected value for info[node_index].height` | height class > 8 (abort) |
| `couldn't allocate every tensor in SB and spilling can't help` | failure exit (nothing left to spill) |

## Confidence and re-verification ceiling

The five strongest claims were re-verified against the cp310 binary first-hand this pass:

1. **`+inf` sentinel** — the eight bytes at file offset `0x1DBCEB8` were read directly: `00 00 00 00 00 00 F0 7F` = `0x7FF0000000000000` = `+inf`. **CONFIRMED.**
2. **Impact LUT** — the symbol, the `.data` residence at `0x3ded040`, the `9·sc+sc` indexing in `impact` (via the relocated `vertical_impact_with_loop_ptr`), and the `{0,1,2,4}` value range are **CONFIRMED**; the individual 81 int32 cell values are **INFERRED** (the table is `.data`-resident behind a relocation, absent from `rodata.bin`/`data_tables.json` — see the CORRECTION in §2).
3. **Spill cap** — `cmp DWORD PTR [Rep+0x2fc],0x10000` is present (twice) in the `insert_spill_code` body, paired with the warning string. **CONFIRMED.**
4. **Latency tiers / cost has no float weights** — the `mov esi,0x8` (DRAM) and `mov esi,0x10` (SB) immediates are byte-verified in `find_costs`, and the disassembly carries no `mulsd`/`movsd`-from-`.rodata` weight literal. **CONFIRMED.**
5. **Degree-squared metric exists but is off-path** — the squaring `imul` (×4: three `imul edx,edx` + one `imul edi,edi`) and `divsd` (×6) are present in `simplify`; the driver passes mode `0` (`cost/cap`, `xor ecx,ecx` @ `0xa95eaa`). **CONFIRMED** for presence; the mode-0 driver path is STRONG.

Remaining ceilings (tagged in-text): the exact `<<7`/`+0` (loop) vs `<<6`/`+160` (flat) cost-slot struct view is STRONG, not byte-traced; the redundant-reload coalescing *order* and the eintervals-driven choice of *which* candidate to store-vs-split are STRONG (string + call-graph), not line-by-line; the `_Reload` load materialization in `indice_legalization` is STRONG (string-xref ownership + `Homes` hand-off). The numeric `SB_SIZE` is a per-arch immediate in the geometry record, documented on the [geometry page](../arch/sbuf-psum-geometry.md), not re-derived here.

## Cross-references

- **SBUF Liveness / Interference / Coalesce** (the front half: liveness, the interference adjacency `Info+56`/`+64`, and the coalesce partner sets `Info+48` this page consumes) — sibling page, planned.
- **Allocator Drivers** (the best-of-n driver and the spill-fixpoint framing) — sibling page, planned.
- **Hwm / PerfSim Cost** (the `bir::Hwm` `getLatency` oracle whose cycle counts are this page's spill-cost weights) — planned.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the `SB_SIZE` byte budget, the 128-partition count, and the `allocator+0x2b8` field that `select`/`possible_placements` read.
- [The Reservation-Table Disambiguation — Scheduler vs Allocator](reservation-table-disambiguation.md) — confirms the `partitionBandReservations` table is the allocator's.
- [DMA Legalization](dma-legalization.md) — the `InstSave`/`InstLoad` spill/reload DMAs.
