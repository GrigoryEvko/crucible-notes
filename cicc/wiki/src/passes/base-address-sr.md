# Base Address Strength Reduction

Address computation is a disproportionately expensive category of work on NVIDIA GPUs. The integer ALU units that compute memory addresses are a scarce resource relative to the FP/tensor throughput the hardware is designed to maximize. A typical unrolled loop body touching four arrays at `A[tid + i]`, `B[tid + i]`, `C[tid + i]`, `D[tid + i]` -- where `tid` is a function of `threadIdx.x`, `blockIdx.x`, and `blockDim.x` -- may emit four independent 64-bit multiply-add chains per iteration, each recomputing the same base expression `base_ptr + tid * element_size`. Reducing those four chains to one base computation plus three cheap constant-offset additions can halve the integer instruction count in the loop body and free address registers that would otherwise stay live across the entire loop.

Base Address Strength Reduction (BASR) is an NVIDIA-proprietary IR-level pass that performs exactly this transformation. It scans loop bodies for memory operations that share a common base pointer expression, finds the one with the minimum constant offset (the "anchor"), hoists the anchor computation, and rewrites all remaining addresses as `(anchor + relative_offset)`. The pass is confirmed by the string `"BaseAddressStrengthReduce"` at decompiled line 457 of `sub_1C67780`.

## Key Facts

| Field | Value |
|-------|-------|
| **Pass name** | `BaseAddressStrengthReduce` |
| **Entry point** | `sub_1C67780` (Legacy PM), `sub_2CA4A10` (New PM) |
| **Size** | 58 KB (~1,400 decompiled lines) |
| **Pass type** | NVIDIA-proprietary, IR-level, loop body transform |
| **Master knob** | `do-base-address-strength-reduce` (two levels: 1 = no conditions, 2 = with conditions) |
| **Chain variant** | `do-base-address-strength-reduce-chain` (separate boolean toggle) |
| **Negative offset control** | `dword_4FBCAE0` (aggressiveness for negative-offset patterns) |
| **IV limit** | `base-address-strength-reduce-iv-limit` (parametric) |
| **Max IV** | `base-address-strength-reduce-max-iv` (parametric) |
| **Debug dump** | `dump-base-address-strength-reduce` |
| **Required analyses** | LoopInfo (`sub_1632FA0`), DataLayout |
| **Option registration** | `ctor_263_0` at `0x4F36F0` (shared with SCEV-CGP, 44 strings total) |
| **Companion pass** | [Common Base Elimination](./common-base-elim.md) (`sub_1C5DFC0`) |
| **Helper** | Bitcast helper at `sub_1C637F0` (28 KB, strings `"baseValue"`, `"bitCastEnd"`) |

## Algorithm

The pass operates in six phases, executing once per function. It processes all loop bodies simultaneously using worklists seeded from LoopInfo.

### Phase 1 -- Initialization (lines 452-497)

The entry function retrieves LoopInfo via `sub_1632FA0` and extracts the module's `DataLayout` from the function object (path: `(a1+184)->field+24->field+40`). It then allocates bookkeeping state:

- **Eight hash maps** at stack offsets `v374`-`v399`, keyed by `Value*` (the base pointer). Each map entry holds a linked list of memory instructions that share that base.
- **Multiple worklists** for basic blocks containing loads vs. stores.
- **Threshold**: `v429 = 2` -- the minimum number of uses of the same base before the pass considers strength reduction worthwhile.
- **Pass counter**: `v438 = 1` -- the initial pass number (the pass may iterate).

### Phase 2 -- Address Pattern Collection (lines 518-600)

For each instruction in the target basic blocks (drawn from the `a4` worklist):

1. `sub_1C57390` classifies the address expression, extracting its structural form.
2. `sub_1CCB2B0` computes alignment information from the DataLayout.
3. `sub_1456040` extracts the base pointer from the address expression.

The base pointer is then categorized into one of two buckets:

| Category | Condition | Hash map | Worklist | Description |
|----------|-----------|----------|----------|-------------|
| Non-pointer-type base | `type_id != 15` | `v382` | `v363` | Integer/GEP-derived base addresses |
| Pointer-type base | `type_id == 15` | `v378` | `v360` | Bases that are raw pointers to globals |

For pointer-type bases, `sub_1CCDC20` further extracts the underlying global variable, allowing grouping of addresses to the same global even when accessed through different local pointer variables.

Hash map insertion uses `sub_1C50900`. If the base pointer is new (not yet in the map), the instruction list is initialized and the base is appended to the corresponding worklist. Otherwise, the instruction is appended to the existing list for that base.

```
for each instruction I in target BBs:
    addr_info = classify_address(I)          // sub_1C57390
    alignment = compute_alignment(addr_info)  // sub_1CCB2B0
    base_ptr  = extract_base(addr_info)       // sub_1456040

    if type_of(base_ptr) != POINTER_TYPE:
        map_insert(hash_map_v382, base_ptr, I)    // sub_1C50900
        if is_new_entry:
            worklist_v363.append(base_ptr)
    else:
        global = extract_global(base_ptr)         // sub_1CCDC20
        map_insert(hash_map_v378, global, I)
        if is_new_entry:
            worklist_v360.append(global)
```

### Phase 3 -- Anchor Finding (lines 430-470)

For each base pointer that has accumulated at least `v429` (2) uses, the pass determines the "anchor" -- the use with the minimum constant offset. This is the instruction whose address computation will be hoisted and shared.

For each candidate base:

1. `sub_1C53170` decomposes each address expression into a `(base, constant_offset)` pair.
2. The pass iterates over all uses and finds the one with the smallest constant offset:
   - For offsets that fit in 64 bits: direct integer comparison via sign-extended values.
   - For offsets wider than 64 bits: reads from extended-precision word arrays and compares word-by-word.
3. The minimum-offset use becomes the anchor.

```
function find_anchor(base_ptr, use_list):
    min_offset = +INF
    anchor = null

    for each use U in use_list:
        (base, offset) = decompose_address(U)  // sub_1C53170

        if bit_width(offset) <= 64:
            val = sign_extend_64(offset)
        else:
            val = read_extended_precision(offset)

        if val < min_offset:
            min_offset = val
            anchor = U

    return (anchor, min_offset)
```

### Phase 4 -- Address Rewriting (lines 578-600)

Once the anchor is identified:

1. `sub_13A5B00` creates a new base address instruction from the anchor's address computation. This instruction is placed at the loop preheader or the dominating point of all uses.
2. For every other instruction sharing the same base, the pass computes the relative offset: `relative_offset = original_offset - anchor_offset`.
3. `sub_14806B0` creates a new address expression `(new_base + relative_offset)` and replaces the original address operand.

```
function rewrite_addresses(anchor, anchor_offset, use_list):
    new_base = create_base_instruction(anchor)  // sub_13A5B00

    for each use U in use_list:
        if U == anchor:
            replace_address(U, new_base)
        else:
            (_, orig_offset) = decompose_address(U)
            rel_offset = orig_offset - anchor_offset
            new_addr = create_offset_add(new_base, rel_offset)  // sub_14806B0
            replace_address(U, new_addr)
```

After this transformation, a loop body that previously contained:

```
load (base + tid*stride + 0)    // original: full GEP chain
load (base + tid*stride + 16)   // original: full GEP chain
store (base + tid*stride + 32)  // original: full GEP chain
store (base + tid*stride + 48)  // original: full GEP chain
```

Becomes:

```
anchor = base + tid*stride + 0  // hoisted once
load anchor                     // offset 0: use anchor directly
load (anchor + 16)              // cheap add
store (anchor + 32)             // cheap add
store (anchor + 48)             // cheap add
```

The three 64-bit multiply-add chains are replaced by three 64-bit immediate additions.

### Phase 5 -- Negative Offset Handling (lines 512-520)

When `dword_4FBCAE0 > 1` (the aggressiveness knob is set above default), the pass also considers address groups where the maximum offset has a negative sign bit. These represent patterns like:

```
load (base + tid*stride - 32)
load (base + tid*stride + 0)
load (base + tid*stride + 32)
```

Without this phase, the anchor would be the instruction at offset `-32`, producing negative relative offsets for the first use. Some hardware addressing modes handle negative offsets less efficiently, so this phase is gated separately.

For negative-offset candidates, the pass:

1. Checks whether the base is loop-invariant via `sub_1C51340`.
2. If loop-invariant, creates a separate common base via `sub_1C55CE0` that absorbs the negative component.

### Phase 6 -- Red-Black Tree Tracking

The pass uses a red-black tree infrastructure (`sub_220F040` for insertion, `sub_220EF80` for lookup) shared with other NVIDIA passes. This provides O(log n) sorted-set operations for maintaining collections of instruction pointers and efficiently checking membership during the rewriting phase.

## Hash Map Implementation

The address pattern hash maps use the standard DenseMap growth policy (75% load factor, 12.5% tombstone compaction) with NVVM-layer sentinels (-8 / -16). The resize/rehash logic lives in `sub_1C54050` -- the same function used by [Common Base Elimination](./common-base-elim.md). Hash keys are `Value*` pointers with linear probing. See [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md) for the hash function and probing strategy.

## Relationship with Common Base Elimination

BASR and [Common Base Elimination](./common-base-elim.md) (`sub_1C5DFC0`) attack the same problem -- redundant address computation -- but at different scopes and with different strategies:

| Dimension | Base Address Strength Reduction | Common Base Elimination |
|-----------|--------------------------------|------------------------|
| **Scope** | Intra-loop: operates within a single loop body | Inter-block: operates across the CFG using dominance |
| **Grouping** | Groups addresses by shared induction-variable-based base | Groups addresses by shared base pointer to the same global |
| **Placement** | Anchor placed at loop preheader | Anchor placed at common dominator of all uses |
| **Offset model** | Constant offsets relative to IV-derived base | Constant offsets relative to global-derived base |
| **Entry point** | `sub_1C67780` | `sub_1C5DFC0` |
| **Size** | 58 KB | 38 KB |

The two-pass approach is deliberate. Common Base Elimination runs first at the IR level, hoisting shared base expressions across control flow boundaries. BASR then runs within loop bodies, strength-reducing the IV-dependent address chains that CBE cannot handle because the IV changes each iteration.

Both passes share the same address decomposition helper (`sub_1C53170`), the same hash map infrastructure (`sub_1C50900`, `sub_1C54050`), and the same instruction creation routines (`sub_13A5B00`, `sub_14806B0`).

## Relationship with SCEV-CGP

The BASR knobs are registered together with SCEV-CGP (Scalar-Evolution-based CodeGenPrepare) in `ctor_263_0` at `0x4F36F0`. This constructor registers 44 option strings total, covering both SCEV-CGP and BASR. The `do-base-address-strength-reduce` and `do-scev-cgp` knobs are stored in the same `ctor_526_0` option block.

SCEV-CGP is a broader pass that performs SCEV-based address optimization using thread ID as an induction variable (`scev-cgp-tid-max-value` controls the maximum thread ID value for analysis). BASR is a sub-transformation within this address optimization framework -- it handles the specific case of multiple memory operations sharing a base, while SCEV-CGP handles the broader case of rewriting address expressions using scalar evolution.

Related SCEV-CGP knobs that interact with BASR:

| Knob | Purpose |
|------|---------|
| `scev-cgp-old-base` | Controls whether SCEV-CGP creates new base expressions |
| `ignore-bad-base` | Bypasses validity checks on base pointer classification |
| `ignore-32-bit-overflow` | Skips 32-bit overflow checks in address arithmetic |
| `ignore-signed-32-bit-overflow` | Skips signed 32-bit overflow checks |
| `topo-sort-begin` | Controls topological sort start point for address chains |
| `special-reassociate-for-threadid` | Prevents reassociation from moving threadId-dependent expressions |

## Configuration

### Boolean Knobs

| Knob | Default | Description |
|------|---------|-------------|
| `do-base-address-strength-reduce` | Enabled (level 2) | Master enable. Level 1 = unconditional; level 2 = with conditions (default). 0 = disabled. |
| `do-base-address-strength-reduce-chain` | Enabled | Enables the chain variant, which strength-reduces chains of dependent address computations |
| `dump-base-address-strength-reduce` | false | Prints diagnostic output when set |

### Parametric Knobs

| Knob | Description |
|------|-------------|
| `base-address-strength-reduce-iv-limit` | Maximum number of induction variables to consider per loop |
| `base-address-strength-reduce-max-iv` | Maximum IV value for strength reduction eligibility |

### Global Variables

| Global | Purpose |
|--------|---------|
| `dword_4FBCAE0` | Negative offset aggressiveness. When > 1, enables strength reduction of address groups with negative offsets. Also used as a special minimum-selection mode in MemorySpaceOpt. |

## Diagnostic Strings

```
"BaseAddressStrengthReduce"   -- Pass identification (line 457)
"baseValue"                   -- Bitcast helper: base value operand name (sub_1C637F0)
"bitCastEnd"                  -- Bitcast helper: end-of-chain marker (sub_1C637F0)
```

When `dump-base-address-strength-reduce` is enabled, the pass emits additional diagnostic output showing which base pointers were grouped, which anchor was selected, and which addresses were rewritten.

## Key Functions

| Function | Address (Legacy) | Size | Role |
|----------|---------|------|------|
| Main entry | `sub_1C67780` | 58 KB | Pass driver: initialization, collection, anchor finding, rewriting |
| Main entry (New PM) | `sub_2CA4A10` | 62 KB | New Pass Manager variant |
| Address classifier | `sub_1C57390` | -- | Classifies address expression structure |
| Address decomposer | `sub_1C53170` | -- | Decomposes address into `(base, constant_offset)` pairs |
| Hash map insert | `sub_1C50900` | -- | Inserts base pointer into pattern hash map |
| Hash map resize | `sub_1C54050` | -- | Load-factor-based resize/rehash |
| Loop invariance check | `sub_1C51340` | -- | Tests whether a value is loop-invariant |
| Negative offset handler | `sub_1C55CE0` | -- | Creates common base for negative-offset patterns |
| Base instruction creation | `sub_13A5B00` | -- | Creates the hoisted anchor address instruction |
| Offset rewriting | `sub_14806B0` | -- | Creates `(base + relative_offset)` replacement |
| Base extraction | `sub_1456040` | -- | Extracts base pointer from address expression |
| Global extraction | `sub_1CCDC20` | -- | Extracts underlying global variable from pointer chains |
| Alignment computation | `sub_1CCB2B0` | -- | Computes alignment from DataLayout |
| Bitcast helper | `sub_1C637F0` | 28 KB | Handles bitcast chains in base address expressions |
| RB-tree insert | `sub_220F040` | -- | Red-black tree insertion (shared infrastructure) |
| RB-tree lookup | `sub_220EF80` | -- | Red-black tree membership check |
| LoopInfo retrieval | `sub_1632FA0` | -- | Gets LoopInfo analysis for the function |

## Cross-References

- [Common Base Elimination](./common-base-elim.md) -- the complementary inter-block pass
- [Pass Overview & Inventory](./index.md) -- master pass listing
- [Optimizer Pipeline](../pipeline/optimizer.md) -- pipeline position and option registration
- [Rematerialization](./rematerialization.md) -- another pass trading computation for register pressure
- [SCEV](../llvm/scev.md) -- the scalar evolution analysis that SCEV-CGP (and indirectly BASR) depends on
