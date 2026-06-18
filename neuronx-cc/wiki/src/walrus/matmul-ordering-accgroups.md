# Matmul Tile Ordering and Accumulation-Group Legalization

> *All addresses on this page apply to neuronx_cc 2.24.5133.0 (`libwalrus.so`, build `58f8de22`). VA == file offset for `.text` (base `0x62d660`) and `.rodata` (base `0x1c72000`); `.data` carries a +0x400000 VMA-vs-file-offset delta. Other versions will differ. All findings derive from static binary analysis.*

## Abstract

A matmul on the Trainium PE array is rarely one instruction. A large `C[M,N] = A[M,K]·B[K,N]` is split two ways: along the contraction `K` into a *chain* of partial-product matmuls that accumulate into one PSUM bank, and along the output column `N` into *column tiles* that each write a ≤128-wide slice of that bank. The PE array's accumulator is destructive — the **first** matmul of a chain zeroes-then-writes the bank (`start_tensor_calc`), interior matmuls add (`accumulate`), and the **last** drains it out (`stop_tensor_calc`). For that convention to be physically correct, the partial matmuls must execute in the right order, and exactly one of them must be marked the zeroing head. Two backend passes establish this.

`order_column_tiled_mms` (BackendPass order 91, class `OrderColumnTiledMMs`) is a pure scheduling-order pass: it walks each basic block, gathers consecutive column-tiled `InstMatmult`s that share one tile *shape*, and chains them with `EdgeKind::Ordered` dependencies so the post-RA list scheduler (order 92) cannot float a column tile out of its accumulate sequence. It touches no calc flag. `legalize_mm_accumulation_groups` (order 94, class `LegalizeMatmulAccumulationGroups`) is the finalizer: after the banks are physically allocated and scheduled, it re-groups matmuls by PSUM `MemoryLocation`, clusters physically-overlapping groups using **five geometric overlap predicates**, and stamps each chain's `START` / `STOP` / `ACCUMULATE` calc flags plus the **zero-region** — the power-of-2 bank window the START matmul must clear. A third routine, `set_first_matmults`, makes a provisional Start-flag pass during dependency reduction; it is *not* called by the ordering pass.

This page covers, in order: the column-tiled predicate and the ordering state machine (§ `order_column_tiled_mms`); the provisional flag marker `set_first_matmults`; the `MatmulAccGrp` geometry struct and the five overlap predicates (§ `legalize_mm_accumulation_groups`); the grouping, overlap clustering, and the START/STOP/ACCUMULATE legalization; and the zero-region width computation. The accumulate triad mirrors the simulator's PSUM Idle/Zero/Accumulate model documented in [Simulator Matmul and MX](../bir/sim-matmul-mx.md).

For reimplementation, the contract is:

- The column-tiled predicate: a plain `InstMatmult` occupying all 128 partitions but a *partial* output-column slice, grouped by tile shape, opened at tile-position `(0,0)`.
- The `Ordered`-edge chaining that freezes column-tile program order through the scheduler.
- The five overlap predicates (closed-interval intersection over five distinct geometry ranges) and the union-clustering rule that merges accumulation groups whose banks collide.
- The START/STOP/ACCUMULATE flag lowering, the `core_v4 + use_memset` all-accumulate variant, and the zero-region = `ceil(log2(bankspan))` window.

| | |
|---|---|
| **Ordering pass** | `OrderColumnTiledMMs::run` `@0x17011b0` (BackendPass order 91) |
| **Legalizer pass** | `LegalizeMatmulAccumulationGroups::run` `@0x16e4120` (order 94) |
| **Provisional marker** | `set_first_matmults(bir::Function&)` `@0x157d340` (called only by `dep_opt`) |
| **IR level** | BIR, post-schedule / post-physical-allocation (orders 91–94) |
| **Group struct** | `MatmulAccGrp` (ctor `@0x16e0ae0`, 0x100+ bytes) |
| **Cluster struct** | `OverlappedMatmulAccGrp` (ctor `@0x16de820`) |
| **Five predicates** | `@0x16da920`, `@0x16da950`, `@0x16da980`, `@0x16da9b0`, `@0x16da9d0` |
| **Calc flag byte / tag** | `inst+0x110` / `inst+0x130`; zero-region byte / tag `inst+0x118` / `inst+0x138` |
| **Arch gate** | `Hwm+4 == 4` (skip path); `ArchLevel @Module+0xac == 0x28` (core_v4) |

---

## order_column_tiled_mms (order 91)

### Purpose

A scheduling-order pass that serializes the column tiles of a tiled matmul so their shared PSUM bank is reused in a deterministic accumulate sequence. It installs `EdgeKind::Ordered` dependencies — pure ordering constraints with no data-hazard semantics — between consecutive same-shape column tiles, and it touches no calc flag. Its output is the precondition the downstream legalizer relies on: that the first column tile of a chain is physically first in the instruction stream, so "the first matmul to this bank is the zeroing head" is true.

### Entry Point

```text
register_generator_order_column_tiled_mms__   factory _M_invoke @0x17050b0
  └─ OrderColumnTiledMMs::OrderColumnTiledMMs   ctor @0x1704050 (0x2c0-byte object)
       (BackendPass name literal "order_column_tiled_mms", order 91)

OrderColumnTiledMMs::run @0x17011b0          ── per-Function / per-BasicBlock walk
  ├─ isStartOfColumnTiledGroup @0x17006a0    ── open a group at tile-pos (0,0)
  │    └─ isColumnTiledMatmul   @0x1700620    ── the column-tiled predicate
  ├─ isColumnTiledMatmul        @0x1700620    ── extend / break the group
  └─ processAndResetGroup       @0x1701170
       └─ createOrderedDependencies @0x1700710 ── chain group[i] → group[i-1]
```

### The column-tiled predicate

A "column-tiled matmul" is a plain `bir::InstMatmult` (opcode 8 *only* — `MatmultMx`/95 and Sparse/9 take a different path) that saturates the partition axis but writes a partial output-column slice. The tile fields are read off the `bir::Instruction*` view at the offsets recovered from `getLegalTileSize`/`getLegalTilePosition` (`@0x1094e70` / `@0x1094330`); each has a boost-optional "present" tag byte that throws `std::out_of_range` when invalid, so both accessors assert `tag == 0` first.

```c
bool isColumnTiledMatmul(Instruction *I):     // @0x1700620 [CONFIRMED]
    if (I == NULL)            return false;
    if (I->opcode /*+0x58*/ != 8)  return false;   // ONLY plain InstMatmult (IT8)
    if (I[+0x278] != 0) throw;     // legalTileSize[1] present-tag must be valid
    if (I[+0x250] != 0) throw;     // legalTileSize[0] present-tag must be valid
    colExtent  = I->legalTileSize[1];   // +0x258  free / output-column extent
    partExtent = I->legalTileSize[0];   // +0x230  partition / row extent
    return (colExtent - 1) <= 0x7e      // colExtent in [1,127]  → PARTIAL column tile
        && partExtent == 0x80;          // partExtent == 128      → FULL partition column
```

The binary computes the column test as `cmp $0x7e,%eax` on the raw `+0x258` extent, i.e. the C-level `(colExtent - 1) <= 0x7e` is fused to the unsigned `colExtent <= 0x7f` boundary with the partition test `cmpl $0x80,0x230` (`@0x170066d`); both feed a `sete` into the boolean result. [CONFIRMED — opcode `cmpl $0x8,0x58(%rsi)` `@0x170062b`; `cmp $0x7e` `@0x1700667`; `cmpl $0x80,0x230` `@0x170066d`.]

> **NOTE —** the group is keyed purely by tile *shape* `(legalTileSize[0], legalTileSize[1])`, never by PSUM bank id. Two tiles of the same large matmul necessarily share the shape, and a shape change — a ragged final column tile, or a different matmul entirely — breaks the run. A value of `0xffffffff` (-1) in a tile field means "not cached" and forces a fallback to `getTilePositionEvalIfNeeded` (affine evaluation over the schedule).

A group is *opened* only by the tile at the origin, position `(0,0)` — the leftmost column slice that owns column-0 of the output region. That tile becomes the chain head:

```c
bool isStartOfColumnTiledGroup(Instruction *I):   // @0x17006a0 [CONFIRMED]
    if (!isColumnTiledMatmul(I))  return false;
    cast<InstMatmultBase>(I);                 // assert opcode in {7,8,9,95}
    if (I[+0x2c8] != 0) throw;                 // legalTilePosition[1] present-tag
    if (I[+0x2a0] != 0) throw;                 // legalTilePosition[0] present-tag
    return (I->legalTilePosition[0] /*+0x280*/
          | I->legalTilePosition[1] /*+0x2a8*/) == 0;   // BOTH origins == 0
```

### Algorithm — the grouping state machine

`run` installs the `module_name` log attribute, emits `"Running OrderColumnTiledMMs pass"`, then walks every function's basic blocks maintaining one running group. Groups never cross a basic-block boundary.

```c
function OrderColumnTiledMMs_run(Module M):        // @0x17011b0 [CONFIRMED]
    for each Function F in M:
      have_group = false; group_vec.clear(); group_key = {0,0};
      for each BasicBlock B in F:                  // groups are per-BB
        for each Instruction I in B (program order):
          ts0 = I->legalTileSize[0] (+0x230);  ts1 = I->legalTileSize[1] (+0x258);
          if (!have_group):
              if (isStartOfColumnTiledGroup(I)):       // tile at position (0,0)
                  group_key = (ts0, ts1);              // freeze the tile SHAPE
                  have_group = true;
                  group_vec.push_back(I);
              // else ignore non-tiled / non-origin instruction
          else:                                        // a group is open
              if (isColumnTiledMatmul(I) && (ts0,ts1) == group_key):
                  group_vec.push_back(I);             // another column slice
              else:
                  // boundary: shape changed, non-column matmul, or non-matmul
                  processAndResetGroup(group_vec, have_group, group_key);
                  // re-examine I as a possible NEW group start (loop continues)
      // function end: flush the final open group
      processAndResetGroup(group_vec, have_group, group_key);
```

The shape key is compared field-by-field: `cmp [rsp+0xa8],ts0` `@0x1701653` and `cmp [rsp+0xac],ts1` `@0x1703031`; the `isColumnTiledMatmul` extend-test is `@0x1701642`, `isStartOfColumnTiledGroup` `@0x1701d68`, `processAndResetGroup` `@0x1701d54`. The pass is logging-heavy — DEBUG strings (`"Found start of column tiled group: "` `@0x1d8b558`, `"Adding to column tiled group: "` `@0x1d8b580`, `"End of column tiled group …"`, `"Processing final column tiled group with "`) interleave the data path but the field reads and branch targets are exact. [CONFIRMED.]

### The ordered chain

`createOrderedDependencies` is the only non-logging work. It is a single `addDependency` call site that linearizes the group head-to-tail:

```c
function createOrderedDependencies(vector<Instruction*> &group):  // @0x1700710
    for (i = 1; i < group.size(); ++i):                            // i starts at 1
        added = group[i]->addDependency(group[i-1],
                                        EdgeKind::Ordered /*=1*/, /*flag=*/false);
        if (added):  log "Added ordered dependency from <group[i-1]> to <group[i]>";
```

The loop init `movq $0x1,0x20(%rsp)` (`@0x170072d`), the `mov $0x1,%edx` setting `EdgeKind::Ordered` (`@0x1700798`), and the `call 5eff00 <addDependency>` (`@0x170079d`) are all confirmed in the body. `EdgeKind` is `{0 Invalid, 1 Ordered, 2 Anti, 3 Output, 4 Flow}`; `Ordered(1)` is a pure scheduling-order constraint — it pins relative order *without* implying a memory hazard. The resulting chain is linear:

```text
tile(pos 0) → tile(pos 1) → tile(pos 2) → … → tile(last)
   = HEAD          interior tiles            = TAIL
```

`processAndResetGroup` (`@0x1701170`) calls `createOrderedDependencies`, then clears the vector and resets `have_group=false`, `group_key={0,0}`.

### Considerations

The point of the `Ordered` edges is that the post-RA list scheduler (order 92, `post_sched`) *respects* them, so it cannot reorder a column tile out of its accumulate sequence. Without this, a non-head tile scheduled before the head would add into an uninitialized (un-zeroed) bank, or the head's later START would wipe a partial it should have kept. The linear chain also keeps DoubleColumn perf-mode (2 output columns/cycle) aligned, since even-width packing requires the tiles execute in order. No pass-specific `PassOptions` are parsed; the pass is unconditional once registered at order 91.

> **CORRECTION (H28) —** an earlier hypothesis held that `OrderColumnTiledMMs` calls `set_first_matmults`. A call-site sweep shows it does not: `set_first_matmults` has exactly one caller in the whole library, `reduce_dependencies::dep_opt` `@0xc464e0`. Order 91 and `set_first_matmults` are independent contributors to the same accumulate machinery, not a caller/callee pair.

---

## set_first_matmults (the provisional Start marker)

### Purpose

A flag-marking routine that makes a *first* pass at the Start flag, keyed by PSUM destination access-pattern. The first matmul to write a given PSUM-dst AP gets `setCalcStart` (zero-then-write); every later matmul to the same AP gets `setCalcContinue` (clears both start and stop → interior). It chooses only the head; the matching tail-Stop is assigned later by the order-94 legalizer.

### Algorithm

```c
function set_first_matmults(Function &F):           // @0x157d340 [CONFIRMED]
    collect_all_blocks(F, &blocks);                 // @0x5ee960
    DenseSet<key> seen;                             // open-addressed; sentinels
                                                    //   empty 0xffff…f000 / tombstone …e000
    for each BasicBlock B, each Instruction I:
        if (I->opcode /*+0x50, ilist-node view*/ in {7,8,9} || == 95):  // matmul family
            cast<InstMatmultBase>(I);
            key = I.output(0)->vtable[+0x20]();      // PSUM-DST AP identity key
            inserted = seen.insert(key);             // probe/insert helper @0x157d190
            if (inserted):  I->setCalcStart();        // FIRST writer of this PSUM dst
            else:           I->setCalcContinue();      // subsequent → clear start+stop
```

The `setCalcStart` (`call 600990` `@0x157d431`) and `setCalcContinue` (`call 5f91b0` `@0x157d4b0`) edges are confirmed. The DenseSet helper `@0x157d190` hashes `(k>>9 ^ k>>4) & (cap-1)`, linear-probes, and returns `inserted=false` when the key already exists, growing the table past 3/4 load.

> **NOTE —** group identity here is the **PSUM destination access-pattern** (`output(0)` AP), not the bank id or output tile directly. This is a provisional, pre-physical-allocation partition. The single caller `dep_opt` runs it as the third step of a re-legalization block — `sync_tiled_matmult_matmultmx` (`@0x1579430`) → `sanitize_matmult_stc_bits` (`@0xc32e50`, clears stale `start_tensor_calc` bits) → `set_first_matmults` — to re-stamp the Start flag from scratch after dependency reduction perturbs the order. The order-94 legalizer below is what produces the *final, physically consistent* flags.

---

## legalize_mm_accumulation_groups (order 94)

### Purpose

The finalizer. It runs after `post_sched` (92) and `arch_legalize` (93), so PSUM banks are physically allocated and scheduled and the pass can read real bank ids, partitions, and byte intervals off each matmul's `PhysicalAccessPattern`. It groups matmuls by PSUM `MemoryLocation`, detects which groups physically overlap, and assigns the final consistent `START`/`STOP`/`ACCUMULATE` calc flags plus zero-region widths across every matmul that shares a region.

### Entry Point

```text
LegalizeMatmulAccumulationGroups::run @0x16e4120
  ├─ index_instructions(F)                          @0x619d70   ── stamp inst+0x4c ordinals
  ├─ collect_acc_grps(F, grps)                       @0x16e14b0
  │    └─ find_accumulation_groups_writing_to_same_memloc @0x1592800
  │    └─ MatmulAccGrp::MatmulAccGrp(loc, matmuls)   @0x16e0ae0  ── fold geometry ranges
  ├─ find_overlapped_psum_zero_region_groups(grps, ovl) @0x16dee50
  │    └─ is_zero_region_bank_overlap && is_partition_dim_overlap → union
  └─ for each OverlappedMatmulAccGrp og:
       ├─ og.legalize_psum_accumulate_flag(spillCtr, Logger) @0x16e3130
       ├─ og.set_psum_accumulate_flag()              @0x16e1410  ── HEAD→START, TAIL→STOP
       └─ og.set_psum_zero_region()                  @0x16daff0  ── pow2 bank window
```

### The MatmulAccGrp geometry struct

The ctor takes `(MemoryLocation *psumLoc, vector<InstMatmultBase*> &matmuls)` and folds min/max over every matmul's write-AP to build the group's geometry ranges. The matmul vector is insertion-sorted ascending by `inst[+0x4c]` (the program ordinal stamped by `index_instructions`). All offsets CONFIRMED from the ctor disasm `0x16e0ae0–0x16e1248`.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `psumLoc` | +0x08 | `MemoryLocation*` | destination PSUM bank set |
| `matmuls` | +0x10 / +0x18 | `vector<InstMatmultBase*>` | ordered group members (begin/end) |
| `head` | +0x28 | `InstMatmultBase*` | `matmuls[0]` — min program-index |
| `tail` | +0x30 | `InstMatmultBase*` | `matmuls[last]` — max program-index |
| `quadrant[lo,hi]` | +0x38 / +0x3c | `uint` | partition>>5 PE-quadrant range |
| `col_grp[lo,hi]` | +0x40 / +0x44 | `uint` | free-axis (output-column) tile range |
| `bank[lo,hi]` | +0x48 / +0x4c | `uint` | PSUM bank range |
| `zr_bank[lo,hi]` | +0x50 / +0x54 | `uint` | pow2-aligned **zero-region** bank window |
| `index[lo,hi]` | +0x58 / +0x60 | `int64` | program-ordinal range (`inst+0x4c`) |
| `partition_dim_mode` | +0xf8 | byte | col-grp(≠0) vs quadrant(0) selector, init 1 |
| byte-interval set | +0xc8 | `boost::icl::interval_set` | union of PSUM byte-address intervals |

> **QUIRK —** the `quadrant` range is `partition >> 5`, i.e. *which* of the four 32-partition PE quadrants the write lands in, not a partition index. Two groups quadrant-overlap iff they share a physical PE quadrant. Confusing the two (treating +0x38 as a raw partition) over-narrows the overlap test and lets two colliding chains pass as disjoint.

### The five overlap predicates

All five are tiny leaf functions in the `0x16da920`–`0x16da9d0` band. Four are the *same* closed-interval intersection compiled over a different field pair; the fifth is a dispatcher. The shared skeleton is the standard "`[a.lo,a.hi]` and `[b.lo,b.hi]` overlap": `if a.lo < b.lo → b.lo <= a.hi; else if a.lo > b.lo → a.lo <= b.hi; else true` — equivalently `a.lo <= b.hi && b.lo <= a.hi`. [CONFIRMED — identical `cmp / jae / setbe` skeleton in all four interval tests.]

| # | Predicate | Address | Fields | Range meaning |
|---|---|---|---|---|
| 1 | `is_zero_region_bank_overlap` | `@0x16da920` | +0x50 / +0x54 | pow2-aligned zero-region **bank window** — the PRIMARY clustering test |
| 2 | `is_quadrant_overlap` | `@0x16da950` | +0x38 / +0x3c | 32-partition PE quadrant range |
| 3 | `is_col_grp_overlap` | `@0x16da980` | +0x40 / +0x44 | free-axis output-column tile range |
| 4 | `is_partition_dim_overlap` | `@0x16da9b0` | dispatcher | selects #3 or #2 by mode byte +0xf8 |
| 5 | `is_index_overlap` | `@0x16da9d0` | +0x58 / +0x60 | 64-bit SIGNED program-ordinal range |

Predicate **1** (zero-region bank) is the decisive one: if two groups' zero-region windows intersect, a single START-zeroes cannot serve both, so they must be merged. Predicate **5** (index) is the schedule-interleave test: signed 64-bit (`jge`/`setle` `@0x16da9db`/`@0x16da9e1`) over the `inst+0x4c` ordinals — two groups index-overlap iff one's matmuls are scheduled *between* another's, so the flag assignment of one must account for the other's intervening writes.

Predicate **4** is not an interval test; it tail-calls one of the two preceding tests by the per-group mode byte:

```c
bool is_partition_dim_overlap(MatmulAccGrp *other):    // @0x16da9b0 [CONFIRMED]
    if (this[+0xf8] == 0)  return is_quadrant_overlap(other);   // je → quadrant
    else                   return is_col_grp_overlap(other);    // jmp → col-grp
```

The binary is `cmpb $0x0,0xf8(%rdi); je → is_quadrant_overlap@plt; jmp → is_col_grp_overlap@plt` (`@0x16da9b7`/`@0x16da9b9`/`@0x16da9c0`). The ctor inits the byte to 1, so the default partition-axis reasoning is in column-group units; a group whose writes are quadrant-tiled rather than col-tiled has the byte cleared and reasons in 32-partition quadrant units instead. [Branch targets CONFIRMED; the col-tiled-vs-quadrant-tiled semantics of the byte is STRONG (inferred from the two targets + ctor init).]

Two reduction predicates gate whether the pass re-derives flags at all:

```c
bool all_auto_psum_accummutate_flag():       // @0x16daa10 — TRUE iff every
    for each matmul mm in group:             //   matmul has getCalcAuto()==true
        if (!mm->getCalcAuto()) return false;
    return true;
bool all_non_auto_psum_accummutate_flag():   // @0x16dab20 — TRUE iff every == false
```

`all_auto` ⇒ honour existing flags; `all_non_auto` ⇒ this pass owns assignment; mixed ⇒ the inconsistent case the legalizer exists to fix. [Both bodies CONFIRMED; the gate semantics STRONG.]

### Grouping — collect_acc_grps and the group-identity rule

`collect_acc_grps` (`@0x16e14b0`) walks `Function::allocs()` and keeps only PSUM `MemoryLocation`s — the test is `*(int*)(loc+0xd8) == 0x20` (MemoryType bit 32 = PSUM, `cmp [rax+0xd8],0x20` `@0x16e1715`). For each PSUM loc it collects the writer `PhysicalAccessPattern`s and calls `find_accumulation_groups_writing_to_same_memloc` (`@0x1592800`) to split them into accumulation subgroups, building one `MatmulAccGrp` per subgroup.

```c
function find_accumulation_groups_writing_to_same_memloc(vector<PhysicalAP*> &writers):
    // @0x1592800 — THE group-identity rule [CONFIRMED]
    map<Instruction*,int> ord;                  // distinct writer → program ordinal
    for each AP in writers:
        inst   = AP[+0x20];                     // writer instruction
        opcode = inst[+0x58];
        is_matmul = (opcode - 7) <= 2           // opcode in {7,8,9}
                 || opcode == 0x5f;             // 95 = MatmultMx
        if (is_matmul):  bucket into the matmul-writer chain
        else:            NON-matmul writer (memset / copy) — splits the chain
    // A maximal program-ordered RUN of matmul writers into the same memloc,
    // with identical AP (compared via PhysicalAccessPattern::toString + memcmp),
    // not split by an intervening non-matmul superset write → one MatmulAccGrp.
```

The opcode classify is `lea edx,[rax-7]; cmp edx,2; jbe` plus `cmp eax,0x5f; je` (`@0x1592993`). **Group identity** = (same PSUM `MemoryLocation`) ∧ (a maximal program-ordered run of matmul writers, not split by an intervening non-matmul superset write). A non-matmul writer (typically a `memset` that explicitly zeroes the bank) sitting in front of or between matmuls terminates / re-seeds the chain — that explicit zero replaces the implicit first-matmul START. [CONFIRMED.]

### Overlap clustering — find_overlapped_psum_zero_region_groups

Connected-components clustering over the simple groups. For each ordered pair `(g_i, g_j)`:

```c
function find_overlapped_psum_zero_region_groups(grps, &out):   // @0x16dee50
    for each pair (g_i, g_j):
        if (g_i.is_zero_region_bank_overlap(g_j)        // predicate 1
         && g_i.is_partition_dim_overlap(g_j)):         // predicate 4
            union(g_i, g_j);                            // std::set Rb-tree insert
    for each connected component C:
        out.push_back( OverlappedMatmulAccGrp(C) );     // ctor @0x16de820
        // installs EdgeKind::Ordered deps (addDependency @0x5eff00, 3×)
        //   between constituent matmuls → chain stays contiguous through scheduling
```

Two groups merge iff they share a zero-region bank window **and** a partition-axis tile. Because their banks overlap, their start/stop/zero-region flags cannot be assigned independently — a START on one would clobber the other's partial sums. A non-overlapping group still becomes a singleton `OverlappedMatmulAccGrp` so `run` treats every group uniformly. The `OverlappedMatmulAccGrp` re-folds head (global min `inst+0x4c`), tail (global max), the merged quadrant range (+0x38/+0x3c), and the union byte-interval set (+0xc8). [CONFIRMED.]

### Algorithm — the START/STOP/ACCUMULATE legalization

`run` first checks an arch gate: `mov 0xa0(Module),%rax; cmpl $0x4,0x4(%rax)` (`@0x16e413f`/`@0x16e414b`) — the `Hwm+4 == 4` generation does its own implicit accumulate, so the pass only clears stale flags via `reset_psum_accumulate_flag_and_zero_region` and returns. Otherwise it runs the mainline per function (`index_instructions` is called twice — once before grouping, once after `legalize_psum_accumulate_flag` may have inserted memsets), and stamps `FunctionAttribute(key=19)` as the completion marker.

The flag lowering itself:

```c
function set_psum_accumulate_flag():                  // @0x16e1410 [CONFIRMED]
    M = this->head /*+0x18*/ -> getModule();
    if (M[+0xac] == 0x28 /*ArchLevel core_v4 == 40*/ && use_memset()):
        // bank pre-cleared by an explicit memset → NO first-matmul-zero
        for each subgroup g, for each matmul mm in g:
            mm->setCalcAccu();                        // every member pure-accumulate
    else:
        this->head /*+0x18*/ -> setCalcStart();        // FIRST matmul zeroes the bank
    this->tail /*+0x28*/ -> setCalcStop();             // LAST matmul drains / reads out
```

The arch gate is `cmpl $0x28,0xac(%rax)` (`@0x16e1424`), `use_memset` is `call 61cc30` (`@0x16e1453`), the head's `setCalcStart` is `call 600990` (`@0x16e1432`), the `setCalcAccu` loop is `call 5f24c0` (`@0x16e148f`), and the tail's `setCalcStop` is `call 5f41e0`. This is the canonical PE convention: HEAD = `start_tensor_calc` (zero-on-first-write), interior matmuls accumulate, TAIL = `stop_tensor_calc` (drain). The codegen lowers these to the ISA accumulate byte at `isa+43` with `bit0=START / bit1=STOP / bit2=ACCUMULATE` (`CoreV3GenImpl::generateMatMulAccumulateFlag` `@0x1428630`, skipped on `ArchLevel > 40` / core_v5). [Flag bodies CONFIRMED; the ISA-byte encoding is the cross-referenced codegen contract.]

> **CORRECTION (H10→H31) —** the "core_v4" gate was earlier taken from a `> 40` codegen test on the `isa+43` byte. The actual constant read *here* is exactly `0x28 == 40` off `Module+0xac` (the `ArchLevel` enum: `inf=10, sunda=20, gen3=30, core_v4=40, core_v5=50`). The `Hwm+4 == 4` skip gate at the top of `run` is a *different* field (a Hwm generation sub-field), not the `ArchLevel`.

`use_memset` (`@0x16e1300`) returns true iff *every* subgroup has a preceding non-matmul writer whose dst AP is a superset of the group's footprint:

```c
bool use_memset():                                    // @0x16e1300
    for each subgroup g:
        if (!g.has_preceding_non_matmul_with_superset_dst_ap())   // @0x16e07e0
            return false;
    return true;
// has_preceding…: for the group head's dst AP, walk the bank's prior writers;
//   TRUE iff some NON-matmul write covers a superset (bir::supersetAP @0x5f8940)
//   → the bank is guaranteed cleared ahead of the chain, so the implicit zero is dropped.
```

When the implicit "first matmul zeroes" convention is unsafe — a prior write already touched the bank, or two chains interleave on one bank — `legalize_psum_accumulate_flag` (`@0x16e3130`) materializes an explicit `InstMemset` via `insert_mm_memset` (`@0x615100`) and converts the matmuls to pure accumulators. It uses `boost::icl::intersects` on the +0xc8 byte-interval sets (`@0x5e95d0`, 3×) for exact-footprint intersection between subgroups. [STRONG — full body read; the exact branch-by-branch merge condition is partially inferred.]

### The zero-region width

The zero-region tells silicon how many banks the START matmul must clear, as a bit-width. The overlapped group's value comes from its merged quadrant span; each non-head member gets its own from its subgroup's bank span.

```c
function set_psum_zero_region():                      // @0x16daff0 [CONFIRMED]
    span = (this->quadrant_hi /*+0x3c*/ + 1) - this->quadrant_lo /*+0x38*/;
    zr   = (span <= 1) ? 0 : bit_width(span - 1);     // bsr(span-1) via xor 0x3f
    if (HEAD[+0x138] /*zero-region tag*/ == 0):
        HEAD[+0x118] = (byte)zr;                       // psum_zero_region byte
    for each subgroup g, for each matmul mm != HEAD:
        bspan = (g.bank_hi /*+0x4c*/ + 1) - g.bank_lo /*+0x48*/;
        zr2   = bit_width(bspan - 1);
        if (mm[+0x138] == 0):  mm[+0x118] = (byte)zr2;
```

The bit-width is `bsr %rax,%rax; xor $0x3f,%rax` (`@0x16db01f`/`@0x16db028`) over `(+0x3c)+1-(+0x38)` (`@0x16db006`/`@0x16db010`), written as a byte to `inst+0x118` with tag check at `inst+0x138` (`mov %bl,0x118(%r12)` `@0x16db05c`). The per-subgroup loop folds `(+0x4c)+1-(+0x48)` identically (`@0x16db0be`/`@0x16db0ca`/`@0x16db0da`). So `zero_region = ceil(log2(bankspan))`. [CONFIRMED.]

The actual bank window is computed once in the ctor and stored at +0x50/+0x54:

```c
function get_psum_zero_region_bank_range(uint startBank, uint endBank):  // @0x16da8a0
    for (k = 0; k < 4; ++k):                  // window = pow(2,k) ∈ {1,2,4,8}
        win = 1 << k;
        lo  = alignDown(startBank, win);
        hi  = lo + win - 1;
        if (hi >= endBank):  return (lo | (uint64)hi << 32);   // covered
    return (0, 0);                            // k==4 (window 16) → NOT coverable (assert)
```

The PSUM zero-region is always a power-of-2-aligned window of 1/2/4/8 banks; a span requiring 16 banks is the failure boundary. [CONFIRMED — `pow@0x614740`, `cmp ebx,4`.]

> **GOTCHA —** the zero-region width and the zero-region *bank range* are two different things written at two different times. The byte `inst+0x118` (`ceil(log2(span))`) is set in `set_psum_zero_region` from the *quadrant* span for the head and the *bank* span for non-heads; the `zr_bank[lo,hi]` window at struct +0x50/+0x54 is computed in the ctor from `get_psum_zero_region_bank_range` and is what predicate 1 intersects. A reimplementation that conflates them will clear the wrong number of banks.

### Idempotency

`reset_psum_accumulate_flag` (`@0x16daef0`) and `reset_psum_zero_region` (`@0x16daf50`) per-matmul clear `CalcStart/Stop/Accu` (→ `setCalcContinue`) and the zero-region byte+tag, run before re-deriving. The free function `reset_psum_accumulate_flag_and_zero_region` (`@0x16dadd0`) is the variant used on the `Hwm+4 == 4` skip path. The calc-flag byte/tag live at `inst+0x110` / `inst+0x130`, the zero-region byte/tag at `inst+0x118` / `inst+0x138`.

---

## Adversarial self-verification

Five strongest claims re-checked directly against `libwalrus.so` (not the report):

| Claim | Binary evidence | Verdict |
|---|---|---|
| `isColumnTiledMatmul` = opcode 8 ∧ colExtent≤127 ∧ partExtent==128 | `cmpl $0x8,0x58` `@0x170062b`; `cmp $0x7e,%eax` on +0x258 `@0x1700667`; `cmpl $0x80,0x230` `@0x170066d` | CONFIRMED |
| Five overlap predicates over +0x50/54, +0x38/3c, +0x40/44, dispatcher, +0x58/60 | `cmp 0x54`/`0x3c`/`0x44`; `cmpb 0xf8` + `je quadrant`/`jmp col_grp`; signed `cmp 0x60` + `setle` | CONFIRMED |
| `set_psum_accumulate_flag`: gate `0xac==0x28`, HEAD+0x18→Start, TAIL+0x28→Stop, memset→Accu | `cmpl $0x28,0xac` `@0x16e1424`; `setCalcStart` `@0x16e1432`; `setCalcAccu` loop `@0x16e148f` | CONFIRMED |
| zero-region = `ceil(log2(span))` via `bsr;xor 0x3f`, byte→+0x118 tag +0x138 | `bsr;xor $0x3f` `@0x16db01f`; `mov %bl,0x118(%r12)` `@0x16db05c`; span `(+0x3c)+1-(+0x38)` | CONFIRMED |
| `createOrderedDependencies` chains with `EdgeKind::Ordered(1)`, i from 1 | `movq $0x1,0x20(%rsp)` `@0x170072d`; `mov $0x1,%edx` `@0x1700798`; `call addDependency` `@0x170079d` | CONFIRMED |

**Re-verify ceiling.** Five predicates, the column-tiled predicate, the START/STOP/ACCUMULATE lowering, the zero-region computation, and the ordering chain are CONFIRMED at the disassembly level. The `is_partition_dim_overlap` mode-byte *semantics* (col-tiled vs quadrant-tiled) is STRONG — the two branch targets and ctor init are confirmed, but the high-level meaning of the byte is inferred. `legalize_psum_accumulate_flag`'s internal merge condition is STRONG — the body, the `icl::intersects` calls, the `insert_mm_memset` materialization, and the arch checks are confirmed, but the exact branch-by-branch condition under which a memset is forced is partially inferred. The `isa+43 START/STOP/ACCUMULATE` codegen byte and the simulator triad live outside this pass and are cited as cross-references, not re-derived here.

---

## Related Passes

| Order | Pass | Relationship |
|---|---|---|
| 27 | `psum_legalization` | widens PSUM dst to fp32 and aligns bank columns — makes the banks legal accumulators before any chaining |
| 89–90 | `separate_load_and_compute` | software-pipelines load/compute; runs just before the ordering pass |
| 91 | `order_column_tiled_mms` | this page — chains same-shape column tiles with `Ordered` edges |
| 92 | `post_sched` | post-RA list scheduler that *respects* the order-91 `Ordered` edges |
| 93 | `arch_legalize` | architectural legalization, runs before the accumulation-group finalizer |
| 94 | `legalize_mm_accumulation_groups` | this page — final START/STOP/ACCUMULATE + zero-region assignment |
| 104/130 | `dep_opt` (`reduce_dependencies`) | the sole caller of `set_first_matmults`; re-stamps the Start flag after dependency reduction |

## Cross-References

- [PE Engine — the Systolic Matmul Array](../arch/pe-engine.md) — the 128×128 PE array and its destructive PSUM accumulator model that this ordering/legalization serves
- [Layout-Tiling Pipeline](../penguin/layout-tiling-pipeline.md) — the tile transform that produces the column-tiled matmuls this pass orders
- [Simulator Matmul and MX](../bir/sim-matmul-mx.md) — the PSUM Idle/Zero/Accumulate triad these calc flags decode into at simulation time
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the PSUM bank layout the zero-region windows are aligned to
- *Mixed-precision accumulation (numerics, planned)* — the accumulator dtype (fp32) the legalized chains write into
