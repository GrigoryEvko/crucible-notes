# LoopOptimization — Distribution, Interchange, and Tiling

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 verified byte-identical at the cited cl::opt store sites). The pass lives in `neuronxcc::backend::LoopOptimization`, compiled into `neuronxcc/starfish/lib/libwalrus.so`. That object retains its full C++ dynamic symbol table (`nm -DC`) and its `.text` is mapped at VA == file offset, so every body cited here is a demangled symbol at a hard address, disassembled with `objdump -d --start-address=0xVA`. The standalone `…/data/bin/loop_optimization` driver is stripped; it dynamically links `libwalrus`, so all class methods resolve to named symbols, not `sub_*`.*

## Abstract

`LoopOptimization` is one walrus-backend pass (`run(Module&)` @`0xb9bb30`) that, per basic block, applies LICM and a greedy fusion driver. Inside that driver sit three *restructuring* transforms that reshape the loop nest so that more fusion becomes legal and profitable: **loop distribution** (split one loop into several), **loop interchange** (swap two nest levels), and **loop tiling** (strip-mine a loop by a factor). The fusion driver, its profit model, and the priority queue are documented on the [LICM / fusion sibling page](loopopt-licm-fusion.md); this page documents the three transforms it calls and the two numeric rules that govern them.

The two headline numbers are a **positional candidate window** `distanceThreshold = 8` (CONFIRMED across all three ABIs) and a **GCD tile factor** `N = GCD(partitionExtent(l1), partitionExtent(l2))` (STRONG — the Euclidean GCD is inline at `0xb93b08`). The threshold is *not* a dependence distance — it is the maximum index-gap, in program order, between two loops that the profit-graph builder will even consider as a fusion pair. The tile factor is *not* a fixed constant and *not* a budget division — it is the greatest common divisor of the two candidate loops' partition-dimension trip counts, chosen so that after tiling both loops have the same inner trip count and can be legally interchanged or fused.

All three transforms ultimately defer their *legality* to the same dependence machinery: a `SymbolicAccessPattern` partition-dimension preflight (`cmpl $0x1, 0x110(AP)`) and a signed dependence distance from `bir::getDefUseDistance` (libBIR `0x20d4e0`), wrapped by `has_negative_distance` @`0xb8f100`. That dependence model has its own [page](../penguin/backend-dependence-distance.md); here it is the gate the transforms consult, not the subject.

For reimplementation, the contract is:

- **The positional window** `distanceThreshold = 8` — where it is read (`constructFusionProfitGraph` @`0xba1226`), what it bounds (the candidate-pair index gap), and that it is orthogonal to the dependence distance.
- **The GCD tile-factor rule** — the two partition-extent vtable calls, the inline Euclidean GCD, the `GCD > 1` guard, and the same-`N`-to-both-loops apply.
- **The distribution rule** — partition the body by def→use into independent statement groups, emit one loop per group, and *commit only when both resulting groups are non-empty*.
- **The interchange legality** — partition-dimension access on both loops plus a non-negative dependence-distance sign, decided pairwise, not via a stored direction matrix.

| | |
|---|---|
| **Pass class** | `neuronxcc::backend::LoopOptimization` |
| **Object** | `neuronxcc/starfish/lib/libwalrus.so` (`.text` VA == file offset) |
| **Pass driver** | `run(Module&)` @`0xb9bb30` |
| **Distribution decision** | `check_loop_distribution(InstLoop*,InstLoop*,vector<SAP*>&,pair&)` @`0xb95250` |
| **Distribution split** | `split_for_distribution(InstLoop*, vector<SAP*>&)` @`0xb8fd40` |
| **Distribution apply** | `loop_distribution(InstLoop*, vector<vector<Instruction*>>&, vector<InstLoop*>&)` @`0xb91f60` |
| **Interchange decision** | `check_loop_interchange(InstLoop*,InstLoop*,vector<SAP*>&,pair&)` @`0xb93430` |
| **Interchange apply** | `loop_interchange(InstLoop*, InstLoop*)` @`0xb92ef0` |
| **Tiling apply** | `loop_tiling(InstLoop*, int N)` @`0xb8d4d0` |
| **Dependence gate** | `has_negative_distance(InstLoop*, InstLoop*)` @`0xb8f100` |
| **`distanceThreshold`** | `uint` cl::opt @`0x3dff780`, value `8` (`movl $0x8` @`0x7cf85d`) |
| **`enable-loop-distribution`** | `bool` cl::opt @`0x3dff900`, default `true` (`movb $0x1` @`0x7cf703`) |
| **`InstLoop` opcode** | `0x69` (105) — `cmpl $0x69, 0x50(%reg)` |

---

## The Two Distances

> **GOTCHA —** there are **two** unrelated "distances," and conflating them is the single biggest trap on this page. `distanceThreshold = 8` is a *positional* index gap in program order; the *dependence* distance is a signed integer from `bir::getDefUseDistance`. The candidate window prunes which pairs are scored; the dependence sign decides whether a scored pair is legal. They are read in different functions, mean different things, and a reimplementation that uses one where the other belongs will either drop legal fusions or commit illegal ones. There is **no** function literally named `getLoopPairDistance` — that earlier narration name maps onto this two-level model. `[C]`

### The positional window — `distanceThreshold = 8`

`constructFusionProfitGraph` @`0xba1080` enumerates candidate loop pairs in basic-block program order. For loop `i`, only loops `j` with index gap `(j − i) ≤ distanceThreshold` are eligible partners; pairs farther apart than 8 loop positions are **never** entered into the profit graph. The value is read straight off the cl::opt global:

```asm
ba1226:  mov    0x3226c4b(%rip),%rax      ; &distanceThreshold (global @0x3dff780)
ba122d:  mov    0x78(%rax),%eax           ; eax = value cell (+0x78) = 8
ba1230:  mov    %rax,0x8(%rsp)            ; stash the window bound
...
ba1255:  cmp    %rax,%rbx                 ; rbx = window index, rax = #loops
ba1258:  jae    ba1329                    ; out of window -> stop enumerating this i
```

The cl::opt default is fixed in the static registrar (`__cxa_atexit` chain, `0x7cf500..0x7cf8a0`). Each `cl::opt<T>` stores its current value at `+0x78`, its declared default at `+0x88`, and a "has-default" byte at `+0x8c`:

```asm
7cf85d:  movl   $0x8,0x78(%rbp)           ; value  = 8   <-- distanceThreshold
7cf864:  movb   $0x1,0x8c(%rbp)           ; has-default flag
7cf86b:  movl   $0x8,0x88(%rbp)           ; default = 8
```

> **NOTE —** the store at `0x7cf85d` reads `movl $0x8,0x78` byte-identically in cp310, cp311, and cp312 (the same VA in all three wheels). The threshold is a stable, cross-ABI constant — not a per-build heuristic. The cl::opt name is `loop-opt-distance-threshold`, help string `"legitimate candidate loop pair distance threshold"`. `[C]`

### The dependence distance — sign and partition-dim legality

The dependence distance is computed by `bir::getDefUseDistance(SAP const&, SAP const&, int)` (libBIR `0x20d4e0`) and tested for sign by `has_negative_distance` @`0xb8f100`. The third `int` argument selects the axis along which the distance is measured (the partition axis; identity `[I]`). Before any distance is computed, both access patterns face a partition-dimension preflight — the field at `+0x110` of the `SymbolicAccessPattern` must equal `1`:

```asm
b8f1c9:  cmpl   $0x1,0x110(%rax)          ; SAP partition-dim descriptor count must be 1
```

The same `cmpl $0x1, 0x110` gate appears verbatim in `getDefUseDistance` (`0x20d4f9`/`0x20d522`) and twice more in `has_negative_distance` (`0xb8f1f6`/`0xb8f366`). A pair that fails it is rejected with `"[STOP FUSION] loop to be fused doesn't access partition dim"` (`0x1ce61b8`). When the distance *is* computed, `has_negative_distance` does `test %r14d,%r14d ; jle` — a distance `≤ 0` flags a backward (loop-carried) dependence and bails. The same dependence machinery underlies all three transforms below; see the [dependence-distance page](../penguin/backend-dependence-distance.md) for the polyhedral model itself.

---

## Loop Distribution

### Purpose

Loop distribution splits one loop over a body that contains independent statement groups into several loops, one per group. It is the inverse of fusion: where fusion merges two compatible bodies, distribution separates one body whose halves do not depend on each other, freeing each half to be tiled, interchanged, or re-fused with a different partner. It is gated by `enable-loop-distribution` (cl::opt @`0x3dff900`, default `true` — `movb $0x1` @`0x7cf703`) and decided by `check_loop_distribution` @`0xb95250`, reachable from both the greedy and classic fusion paths.

### Entry Point

```text
check_loop_distribution(l1, l2, vector<SAP*>&, pair&)   @0xb95250
  └─ is_perfect_loopnest / is_perfect_loop                ── precondition (perfect nest only)
  └─ split_for_distribution(loop, vector<SAP*>&)          @0xb8fd40  ── partition body by def->use
  └─ loop_distribution(loop, partitions, &out_loops)      @0xb91f60  ── emit one InstLoop per group
  └─ assert l1_loops_after_distribution.size() > 0        @0xb953db  ── both halves non-empty
  └─ assert l2_loops_after_distribution.size() > 0        @0xb9554b
```

### Algorithm

```c
function check_loop_distribution(InstLoop *l1, InstLoop *l2,         // 0xb95250
                                 vector<SAP*> &commonAPs, pair &out):
    // Precondition: only perfectly-nested loops may be distributed.
    if !is_perfect_loopnest(l1) or !is_perfect_loop(l1):            // PLT helpers
        return;                                                     // not distributable

    // 1. Partition the body by DEPENDENCE into independent statement groups.
    vector<vector<Instruction*>> partitions = {};
    split_for_distribution(l1, commonAPs);                          // 0xb8fd40

    // 2. Emit one InstLoop per partition (clones header / iteration space).
    vector<InstLoop*> l1_after, l2_after;
    loop_distribution(l1, partitions, l1_after);                   // 0xb91f60
    loop_distribution(l2, partitions, l2_after);

    // 3. NONEMPTY-HALVES RULE: commit only if BOTH groups are non-empty.
    assert(l1_after.size() > 0,                                    // 0xb953db, str 0x1ce60c0
           "l1_loops_after_distribution.size() > 0");
    assert(l2_after.size() > 0,                                    // 0xb9554b, str 0x1ce60e8
           "l2_loops_after_distribution.size() > 0");
    // a split that leaves either side empty is a no-op and is rejected.
```

```c
function split_for_distribution(InstLoop *loop, vector<SAP*> &commonAPs):  // 0xb8fd40
    // Build per-tensor def/last-use info, then group instructions so that
    // no group's def precedes another group's use of the same tensor.
    DenseMap<StorageBase*, mem_loc_info> info = {};
    for inst in loop.body:
        for each tensor t touched by inst:
            info[t].def      = find_inst(t);          // producer        (PLT find_inst)
            info[t].last_use = findLastUseInBB(t);    // last consumer    (PLT findLastUse*)
    // Partition into vector<vector<Instruction*>> such that each partition
    // can live in its own loop without violating any def -> use order.
    return partitions;                                // consumed by loop_distribution
```

### The distribution rule, stated precisely

> **A loop may be distributed at a point iff no dependence cycle crosses that point, and the split is committed only when *both* resulting loop groups are non-empty.** The first half is the def→use partitioning in `split_for_distribution` (a tensor's producer and all its consumers must stay in the same group, so a group is a maximal set with no outward def→use edge). The second half is the pair of asserts at `0xb953db`/`0xb9554b` — a "split" that empties one side is a no-op and is rejected. `[C]` for both asserts (strings `0x1ce60c0`/`0x1ce60e8` confirmed present); `[S]` for the cycle-free phrasing (the binary partitions by def→last-use, which is the operational form of "no dependence cycle crosses the split").

> **NOTE —** the string `"Const Load Distribution: nConsts:"` (`0x1d06148`) belongs to a **separate** constant-hoist path @`0xd27584`, *not* to core loop distribution. A reimplementer scanning for distribution strings should not fold it into this transform. `[C]`

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `check_loop_distribution` | `0xb95250` | Distribution decision driver | CERTAIN |
| `split_for_distribution` | `0xb8fd40` | Partition body by def→use | CERTAIN |
| `loop_distribution` | `0xb91f60` | Emit one `InstLoop` per group | CERTAIN |
| `is_perfect_loopnest` / `is_perfect_loop` | PLT thunks (`…f94f0`/`…eac60`) | Perfect-nest precondition | HIGH |

---

## Loop Interchange

### Purpose

Loop interchange swaps two levels of a loop nest. The backend uses it to make a candidate fusion pair commensurate — reordering the nest so the two loops iterate the same dimension in the same position — and it is the function that *also* drives tiling (see below). The decision is `check_loop_interchange` @`0xb93430`; the mechanical swap is `loop_interchange` @`0xb92ef0`.

### Entry Point

```text
check_loop_interchange(l1, l2, vector<SAP*>&, pair&)   @0xb93430
  └─ collect_interchangeable_loops(l1, DenseSet&)       @0xb906f0  ── permutable set for l1
  └─ collect_interchangeable_loops(l2, DenseSet&)       @0xb906f0  ── permutable set for l2
  └─ [GCD tile-factor computation + loop_tiling x2]      @0xb93ad3  ── make loops commensurate
  └─ partition-dim + dependence-direction legality test  @0xb94cb9
  └─ loop_interchange(l1, l2)                            @0xb92ef0  ── mechanical swap
       └─ update_all_loopnest(Instruction*)              PLT        ── rebuild nest metadata
```

### Algorithm

```c
function check_loop_interchange(InstLoop *l1, InstLoop *l2,         // 0xb93430
                                vector<SAP*> &commonAPs, pair &out):
    // 1. Gather the set of loops in each nest that may be permuted.
    DenseSet<InstLoop*> permutable_l1, permutable_l2;
    collect_interchangeable_loops(l1, permutable_l1);              // 0xb934b9
    collect_interchangeable_loops(l2, permutable_l2);              // 0xb934d1

    // 2. Make the two candidate loops commensurate by tiling (see §Loop Tiling).
    //    N = GCD(partitionExtent(l1), partitionExtent(l2)); tile both by N if N > 1.
    int N = gcd_partition_extents(l1, l2);                         // 0xb93ad3..0xb93b1c
    if N > 1:
        loop_tiling(l1, N);                                        // 0xb94f53
        loop_tiling(l2, N);                                        // 0xb94f63   same N

    // 3. LEGALITY: partition-dim access on both + valid dependence direction.
    bool partdim_ok = accesses_partition_dim(l1) && accesses_partition_dim(l2);
    bool dir_ok     = !has_negative_distance(l1, l2);             // sign on partition axis
    // legality flags live in r14b + stack booleans at 0x30(rsp)/0x40(rsp)  (0xb94cb9..ccf)
    if !(partdim_ok && dir_ok):
        log("invalid b/c not on partition dim");                   // 0x1ce5fd8, @0xb94d34
        return;                                                    // interchange illegal

    // 4. APPLY the swap.
    loop_interchange(l1, l2);                                      // 0xb92ef0
```

```c
function loop_interchange(InstLoop *l1, InstLoop *l2):              // 0xb92ef0
    // Mechanically permute nest order: splice the inner body out,
    // insert InstNoOp / InstGenericCopy markers, remove & re-insert
    // the loop instructions (bir splice / removeInstruction), then:
    update_all_loopnest(l1);          // PLT — rebuild the nesting metadata
```

### Interchange legality, stated precisely

> **Interchange of `l1` and `l2` is legal iff both loops access the partition dimension (`SAP +0x110 == 1`) and reordering them preserves the dependence-direction sign on the partition axis (a non-negative `getDefUseDistance`).** The legality is decided **per pair**, not from a materialized dependence-direction matrix. The two stack booleans at `0x30(%rsp)`/`0x40(%rsp)` plus `r14b` (`0xb94cb9..0xb94ccf`) are the partition-dim flags; the direction sign reuses the same `has_negative_distance` test as fusion. On failure the pass logs `"invalid b/c not on partition dim"` (`0x1ce5fd8`). `[C]` for the strings, the GCD/tiling dataflow, and the partition-dim gate; `[S]` for the "direction sign preserved" phrasing.

> **CORRECTION —** an earlier narration described interchange as consulting a "dependence-direction matrix." No such matrix object is materialized in the binary. Legality is a per-pair *test*: the `SAP +0x110` partition-dim check plus the `getDefUseDistance` sign. For nests deeper than two levels only pairwise tests were observed — whether a multi-level direction vector is consulted is a `[GAP]`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `check_loop_interchange` | `0xb93430` | Interchange decision + tiling driver | CERTAIN |
| `loop_interchange` | `0xb92ef0` | Mechanical nest swap | CERTAIN |
| `collect_interchangeable_loops` | `0xb906f0` | Permutable-loop set (free fn) | CERTAIN |
| `update_all_loopnest` | PLT (`…f9ba0`) | Rebuild nesting metadata | HIGH |

---

## Loop Tiling

### Purpose

Loop tiling (strip-mining) splits a loop's iteration range into outer "tiles" of a fixed inner extent `N`. In this backend, tiling is **not** a standalone transform — it runs *inside* `check_loop_interchange`, before the swap, to give the two candidate loops a matching inner trip count so they can be legally interchanged or fused. The apply primitive is `loop_tiling(InstLoop*, int N)` @`0xb8d4d0`.

### The GCD tile-factor rule — the headline

The factor `N` is computed at `0xb93ad3..0xb93b1c`. Two vtable calls fetch the partition-dimension extent of each loop's tensor, then an inline Euclidean GCD reduces them:

```asm
b93ad3:  mov    0x150(%rax),%rdi          ; l1.tensor (member +0x150)
b93add:  call   *0x140(%rax)              ; ebp = partitionExtent(l1)   (vtable slot +0x140)
b93ae9:  mov    0x150(%rax),%rdi          ; l2.tensor (member +0x150)
b93af3:  call   *0x140(%rax)              ; edx = partitionExtent(l2)
b93b03:  mov    %ebp,%eax                 ; eax = extent(l1)
b93b08:  mov    %edx,%ecx                 ; --- Euclidean GCD(eax, edx) ---
b93b0a:  cltd
b93b0b:  idiv   %ecx
b93b0d:  mov    %ecx,%eax
b93b0f:  test   %edx,%edx
b93b11:  jne    b93b08                    ; loop until remainder == 0; ecx = gcd
b93b13:  mov    %ecx,0x44(%rsp)           ; tile factor N = GCD
b93b17:  cmpl   $0x1,0x44(%rsp)           ; if GCD == 1
b93b1c:  jne    b93b68                    ;   -> skip tiling (jump past the tile apply)
```

Both loops are then tiled by the **same** `N`:

```asm
b94f40:  mov    0x44(%rsp),%r14d          ; r14d = N (the GCD)
b94f4d:  mov    %r14d,%edx                ; arg N
b94f53:  call   loop_tiling@plt           ; loop_tiling(l1, N)
b94f60:  mov    %r14d,%edx                ; arg N (same value)
b94f63:  call   loop_tiling@plt           ; loop_tiling(l2, N)
```

And the chosen factor is logged via `"[LOOP TILING] by "` (`0x1c76bb6`, `lea` @`0xb94ebb`) followed by `operator<<(int)` on `0x44(%rsp)` (@`0xb94f14`).

> **The tile size is the greatest common divisor of the two loops' partition-dimension trip counts, applied iff `GCD > 1`.** It is therefore neither a fixed constant nor an SB/PSUM-budget division. Choosing the GCD guarantees both loops divide evenly into tiles of the *same* inner extent `N`, which is precisely the precondition the following interchange/fusion needs. `[C]` for the GCD instruction sequence, the `GCD > 1` guard, the same-`N` apply, and the `"[LOOP TILING] by N"` log; `[S]` for the rule as a whole; `[I]` for the exact identity of vtable slot `+0x140` (almost certainly `Argument::getNumPartitionsAccessed`, libBIR `0x234170`, the partition-extent getter — its *role* as the value fed to GCD is CONFIRMED by the dataflow even though the slot is called indirectly).

```c
function loop_tiling(InstLoop *loop, int N):                        // 0xb8d4d0
    // Strip-mine `loop` by factor N: re-walk the body access patterns
    // (find_inst, isa/cast<Argument>, getMemoryLocationSet) and repoint
    // every AP onto the tiled iteration space (outer tile loop + inner N).
    for inst in loop.body:
        for ap in memoryLocationSet(inst):
            ap.retarget_to_tiled_space(N);
```

> **GOTCHA —** do **not** confuse this with the `tilingThreshold` cl::opt @`0x3e03e20`. That global is `"DMA-tiling-threshold"` (help `"DMA tiling threshold for Copy descriptor"`, `0x1d61ab0`); its consumers are `CoreV*GenImpl::hasTilingConfigChanged` and `DescGenHelper::determineTilingDimForDMADesc` — DMA *descriptor* tiling in codegen, unrelated to `loop_tiling`. The struct it controls inits to `{2,1,0}` (a DMA-dim mode array). The loop tile factor is the GCD above, not any cl::opt. `[C]`

> **CORRECTION —** the loop tile factor does **not** reuse `full_unroll`'s `getLargestFactorWithThreshold` (MaxFactor=16). That primitive is a storage/engine split granularity (largest divisor with cofactor ≤ 16); this tile `N` is the GCD of two partition extents. Two distinct "factor" primitives — do not conflate. `[I]` (cross-pass distinction, from the separate symbol roster).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `loop_tiling` | `0xb8d4d0` | Strip-mine one loop by `N` | CERTAIN |
| GCD computation | inline `0xb93ad3..0xb93b1c` | `N = GCD(extent l1, extent l2)` | CERTAIN (seq) / STRONG (rule) |
| `Argument::getNumPartitionsAccessed` | libBIR `0x234170` | Candidate for vtable slot `+0x140` | INFERRED |

---

## Pass Order and Gating

`run(Module&)` @`0xb9bb30` resolves the two memory budgets, then drives the per-block transforms. The transforms above are reached *inside* the fusion driver, not as separate top-level phases:

```text
run(Module&)  @0xb9bb30
  1. getArchModel(archLevel)                               @0xb9bb77
  2. resolve SBSize/PSUMSize (0 -> arch-derived)           @0xb9bbd5/@0xb9bbf7 -> +0x104/+0x108
  3. printParams()  (emits distanceThreshold=, enableLoopDistribution=, ...)  @0xb9bc19
  4. mmpk_lower_symbolic(Module&)                          @0xb9bc29
  5. update_liveN(Module&)                                 @0xb9bc39
  6. per BasicBlock:                                        @0xb9bccb..@0xb9bd55
       enableProfitableLoopFusion ? checkLoopFusionGreedy(bb) : check_loop_fusion(bb)
         (internally, per accepted pair: distribution -> tiling -> interchange -> fuse, gated)
  7. licm(Module&)                                         @0xb9c094
```

The two `loop_opt_sb_size` / `loop_opt_psum_size` cl::opts (both default `0` → derive from the arch model, cached in members `+0x104`/`+0x108`) bound the *working set* of a tiled/fused loop body in the fusion profit/legality check; they do **not** set the tile size. The checkpoint dump is `"__loop_opt_chkpt.txt"`.

### Gating Knobs

| Knob Name | Type | Default | Description |
|---|---|---|---|
| `loop-opt-distance-threshold` | `uint` | **8** | Positional candidate-pair window in `constructFusionProfitGraph`; pairs > 8 loop-positions apart are never scored. Global @`0x3dff780`; `movl $0x8` @`0x7cf85d`. |
| `enable-loop-distribution` | `bool` | **true** | Enables the distribution transform. Global @`0x3dff900`; `movb $0x1` @`0x7cf703`. |
| `enable-profitable-loop-fusion` | `bool` | **true** | Selects the greedy driver that hosts these transforms. Global @`0x3dff840`. |
| `loop_opt_sb_size` | `uint` | **0** | SB bytes per partition; `0` → arch-derived. Bounds working set, not tile size. Global @`0x3dffa80`; member `+0x104`. |
| `loop_opt_psum_size` | `uint` | **0** | PSUM bytes per partition; `0` → arch-derived. Global @`0x3dff9c0`; member `+0x108`. |
| `disable-loop-opt` | `bool` | **false** | Master disable for the whole pass. Global @`0x3dfc700`. |

---

## Adversarial Self-Verification

Five strongest claims re-checked against the binary:

1. **`distanceThreshold` default = 8, positional window.** `[C]` — `movl $0x8,0x78` and `movl $0x8,0x88` at the cl::opt registrar (`0x7cf85d`/`0x7cf86b`, both value and default cells), and the window read `mov 0x78(%rax),%eax` @`0xba122d` with `jae` @`0xba1258` in `constructFusionProfitGraph`. Cross-ABI: the `0x7cf85d` store is byte-identical in cp311/cp312.
2. **Tile factor `N = GCD(partitionExtent l1, partitionExtent l2)`, applied iff `> 1`.** `[C]` for the sequence — two `call *0x140(%rax)` extent getters (`0xb93add`/`0xb93af3`), the Euclidean GCD loop `idiv`/`test`/`jne` @`0xb93b08`, `mov %ecx,0x44(%rsp)` @`0xb93b13`, `cmpl $0x1` skip-if-one @`0xb93b17`, and the same-`N` apply `call loop_tiling` @`0xb94f53`/`0xb94f63`. `[S]` for the rule; `[I]` for the `+0x140` getter identity (`getNumPartitionsAccessed`, libBIR `0x234170`).
3. **Distribution commits only when both halves are non-empty.** `[C]` — the assert strings `l1_loops_after_distribution.size() > 0` and `l2_loops_after_distribution.size() > 0` are present in the binary; the asserts fire at `0xb953db`/`0xb9554b`. `[S]` for the cycle-free partition phrasing (operationally a def→last-use grouping in `split_for_distribution`).
4. **Interchange legality = partition-dim access + non-negative dependence sign, per pair.** `[C]` — the `cmpl $0x1,0x110` partition-dim gate (`0xb8f1c9`), the `"invalid b/c not on partition dim"` string (`0x1ce5fd8`), and the `collect_interchangeable_loops` calls @`0xb934b9`/`0xb934d1`. `[S]` for "direction sign preserved"; `[I]` for deeper-than-2 nests (only pairwise tests observed).
5. **`bir::getDefUseDistance` is the dependence-distance primitive.** `[C]` — symbol confirmed in libBIR at `0x20d4e0` (`nm -DC`), called from `has_negative_distance` with the `test %r14d; jle` sign reject.

**Honest re-verify ceiling.** The `distanceThreshold = 8` constant, the GCD instruction sequence, the same-`N` tiling apply, the distribution non-empty asserts, the partition-dim gate, and the interchange/distribution/tiling symbol addresses are all CONFIRMED at the instruction or symbol level — bedrock. What remains below CONFIRMED: (a) the identity of vtable slot `+0x140` as the partition-extent getter (`[I]`, but its *role* in the GCD dataflow is confirmed); (b) the third `int` axis argument of `getDefUseDistance` as the partition axis index (`[I]`); (c) whether interchange consults a multi-level direction vector for nests deeper than two (`[GAP]` — only pairwise tests seen); (d) the exact `mem_loc_info` layout used by `split_for_distribution` (`[GAP]`). No address, default, or field meaning on this page is fabricated; every inference carries its tag.

---

## Related Components

| Name | Relationship |
|---|---|
| `checkLoopFusionGreedy` @`0xba9210` | Greedy driver that hosts these three transforms; owns the profit graph and the fuse apply |
| `has_negative_distance` @`0xb8f100` | Shared dependence-sign gate consulted by interchange and fusion |
| `licm` @`0xb90a70` | Runs after all per-block restructuring, hoisting invariants the transforms exposed |
| DMA descriptor tiling (`tilingThreshold` @`0x3e03e20`) | Unrelated codegen tiling; not `loop_tiling` |
| `full_unroll` (`getLargestFactorWithThreshold`, MaxFactor=16) | A different "factor" primitive; not the GCD tile factor |

---

## Cross-References

- [LoopOptimization — LICM and Greedy Profit-Based Fusion](loopopt-licm-fusion.md) — the fusion driver and profit model that calls distribution, interchange, and tiling
- [Backend Polyhedral Dependence-Distance](../penguin/backend-dependence-distance.md) — `bir::getDefUseDistance` and the polyhedral distance model these transforms gate on
- [Penguin Loop-Transform Clients](../penguin/loop-transform-clients.md) — the Penguin-side loop-transform clients that feed this backend pass
- [The walrus Pass Pipeline & Optlevel Planes](pass-pipeline-optlevels.md) — where `loop_optimization` sits in the walrus pass order
