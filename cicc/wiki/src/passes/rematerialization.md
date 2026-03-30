# Rematerialization

NVIDIA's rematerialization infrastructure in CICC operates at two levels: an IR-level pass (`nvvmrematerialize` / `"Legacy IR Remat"`) that reduces register pressure before instruction selection, and a machine-level pass (`nv-remat-block` / `"Do Remat Machine Block"`) that performs the same transformation on MachineIR after register allocation decisions have been made. Both passes share the same fundamental strategy -- recompute cheap values at their use sites rather than keeping them live across long spans -- but they differ significantly in their cost models, candidate selection criteria, and interaction with the surrounding pipeline.

On NVIDIA GPUs, register pressure directly determines [occupancy](../gpu-execution-model.md#register-pressure-and-occupancy) -- the number of concurrent warps per SM -- with discrete cliff boundaries where a single additional register can drop an entire warp group. Rematerialization trades extra ALU work for reduced register count, a tradeoff that is almost always profitable on GPUs where compute throughput vastly exceeds register file bandwidth.

## Key Facts

| Property | Value |
|---|---|
| Pass name (New PM) | `remat` |
| Pass name (Legacy PM) | `nvvmrematerialize` / `"Legacy IR Remat"` |
| Class | `RematerializationPass` |
| Registration | New PM #385, line 2257 in `sub_2342890` |
| Runtime positions | Tier 0 #34 (NVVMRematerialization via `sub_1A13320`); Tier 1/2/3 #55 (gated by `!opts[2320]`); see [Pipeline](../llvm/pipeline.md) |
| Pass factory | `sub_1A13320` |
| Machine-level companion | `nv-remat-block` / `"Do Remat Machine Block"` at `sub_2186D90` |
| Upstream equivalent | None -- entirely NVIDIA-proprietary |

## IR-Level Rematerialization (`nvvmrematerialize`)

### Registration and Dependencies

The pass is registered at `sub_1CD0BE0` with pass ID `"nvvmrematerialize"` and entry point `sub_1CD0CE0`. Before running, it initializes five analysis passes:

| Analysis | Function | Purpose |
|----------|----------|---------|
| Dominator tree | `sub_15CD350` | Dominance queries for instruction placement |
| Loop info | `sub_1440EE0` | Loop nest structure for cost scaling |
| Unknown | `sub_13FBE20` | Possibly alias analysis |
| Live variable analysis | `sub_1BFC830` | Builds live-in/live-out bitvector sets |
| Unknown | `sub_1BFB430` | Possibly register pressure estimation |

### Main Algorithm (`sub_1CE7DD0`, 67KB)

**Complexity.** Let B = number of basic blocks, I = total instructions, and L = number of live-in values. The live-in analysis uses hardware `popcnt` on bitvectors of size ceil(I / 64) per block, giving O(B * I / 64) per iteration. The intersection of live-in sets (bitwise AND) is O(B * I / 64). The rematizability check for each candidate walks its def chain: O(D) where D is the def-chain depth (bounded by `max-recurse-depth`). The pull-in cost model (`sub_1CE3AF0`) scores each candidate in O(U * D) where U = uses per candidate. Candidate sorting is O(K^2) via selection sort where K = candidates selected. The block executor clones instructions in O(K * B). The outer loop runs at most 5 iterations. Overall IR-level: O(5 * (B * I / 64 + K * U * D + K * B)). For the machine-level pass (`sub_2186D90`): max-live computation is O(I) per block (reverse walk), giving O(I) total. Candidate classification is O(I) for the initial scan, plus O(K * 50) for recursive pullability checks (depth bounded at 50). The second-chance heuristic iterates until convergence -- bounded by the candidate count K. The outer loop runs at most `nv-remat-max-times` (default 10) iterations. Overall machine-level: O(10 * (I + K^2)).

The driver implements an iterative register pressure reduction loop with up to 5 iterations. The high-level flow:

1. **Function exclusion check**: The `no-remat` knob stores a comma-separated list of function names. If the current function matches, the pass prints `"Skip rematerialization on <funcname>"` and bails.

2. **Master gate**: If all three sub-passes are disabled (`do-remat`, `remat-iv`, `remat-load` all zero), return immediately.

3. **Live-in/live-out analysis**: For each basic block, the pass looks up the block's live-in bitvector from the analysis (`sub_1BFDF20`), counts live-in values via hardware `popcnt` (`sub_39FAC40`), and stores per-block counts in a hash map. The maximum live-in across all blocks becomes the pressure target baseline. At `dump-remat >= 2`, the pass prints `"Block %s: live-in = %d"`.

4. **Register target computation**: The algorithm computes how many registers it wants to reduce to:
   - If `remat-maxreg-ceiling` is set and lower than the actual register count, cap at that value.
   - If `remat-for-occ` is non-zero (default 120): call `sub_1BFBA30` for register usage, then `sub_1C01730` for an occupancy-based target. Apply heuristic adjustments based on occupancy level.
   - Otherwise: target = 80% of the current register count.

5. **Iterative loop** (up to 5 iterations):
   - If max live-in is already at or below the target, skip to the IV/load phases.
   - Compute the intersection of live-in bitvectors across blocks (bitwise AND). Values that are live-in everywhere are the best rematerialization candidates because pulling them in at each use site eliminates a register everywhere.
   - Walk the intersection bitvector. For each candidate, check rematerizability via `sub_1CD06C0`. Partition into rematizable and non-rematizable sets.
   - Call `sub_1CE3AF0` (pull-in cost analysis) to rank candidates by cost.
   - Build a per-block rematerialization plan and execute via `sub_1CE67D0`.
   - Recompute max live-in. If it decreased, continue iterating.

6. **Post-remat phases**: After the main loop, run IV demotion (`sub_1CD74B0`) if `remat-iv` is enabled, then load rematerialization (`sub_1CDE4D0`) if `remat-load` is enabled, then cleanup (`sub_1CD2540`).

7. **Expression factoring**: When `remat-add` is non-zero, the pass also performs strength reduction on chains of `add`/`mul`/`GEP` instructions, factoring common sub-expressions into `"factor"` named values. This is a mini-pass embedded within rematerialization.

### Block-Level Executor (`sub_1CE67D0`, 32KB)

This function processes one basic block at a time, creating two kinds of instruction clones distinguished by their name prefixes:

**`remat_` prefix**: The value was live-in to the block and is being recomputed from scratch. The defining instruction is duplicated via `sub_15F4880`, named with the `"remat_"` prefix via `sub_164B780`, and inserted at the use site. This is full rematerialization.

**`uclone_` prefix**: The value already has a definition in the block's dominance chain, but a local copy is needed to shorten the live range. The instruction is cloned and named `"uclone_"`. This is a use-level clone for live range splitting, not pure rematerialization.

After cloning, both variants update use-def chains via `sub_1648780` and set debug locations via `sub_15F22F0`.

### Pull-In Cost Model (`sub_1CE3AF0`, 56KB)

The cost model evaluates each candidate for rematerialization by computing:

```
pull_in_cost = base_cost * use_factor
```

Where `base_cost` is the sum of per-instruction costs along the value's def chain (`sub_1CD0460`), and `use_factor` is accumulated from per-use costs (`sub_1CD3A10`), with different cost tables for uses in different loop nests.

Candidates are filtered by three thresholds:

| Filter | Condition | Default |
|--------|-----------|---------|
| Use limit | `use_count > remat-use-limit` AND `use_factor >= remat-loop-trip` | 10 uses, 20 trips |
| GEP cost | `cost > remat-gep-cost` AND opcode is GEP | 6000 |
| Single cost | `cost > remat-single-cost-limit` (unless `remat-ignore-single-cost`) | 6000 |

After scoring, candidates are sorted by cost (cheapest first via selection sort), and the cheapest N are selected where N is the target reduction count. At `dump-remat >= 4`, the pass prints `"Total pull-in cost = %d"`.

### NLO -- Simplify Live Output (`sub_1CE10B0` + `sub_1CDC1F0`)

The NLO sub-pass normalizes live-out values at block boundaries to reduce register pressure. Controlled by `simplify-live-out` (default 2):

- **Level 1**: Basic normalization only.
- **Level 2** (default): Full normalization. Walks each block's live-out set and replaces values with simpler expressions.
- **Level 3+**: Extended patterns.

NLO creates two kinds of synthetic instructions:
- **`nloNewBit`**: A bit-level operation (AND, extract, truncation) to reduce a live-out value to its actually-used bit width.
- **`nloNewAdd`**: A local add instruction to recompute an address/offset that was previously live-out, replacing it with a local computation.

### IV Demotion (`sub_1CD74B0`, 75KB)

The induction variable demotion sub-pass reduces register pressure by narrowing wide IVs (typically 64-bit to 32-bit). Controlled by `remat-iv` (default 4, meaning full demotion):

| Level | Behavior |
|-------|----------|
| 0 | Disabled |
| 1-2 | Basic IV demotion |
| 3 | Extended IV demotion |
| 4 | Full demotion including complex patterns (default) |
| 5+ | Aggressive mode |

The algorithm identifies PHI nodes at loop headers, checks whether the IV's value range fits in a smaller type (for 64-bit IVs: `(val + 0x80000000) <= 0xFFFFFFFF`), and creates narrower replacements:

- **`demoteIV`**: A truncation of the original IV to a narrower type.
- **`newBaseIV`**: A new narrow PHI node to replace the wide loop IV.
- **`iv_base_clone_`**: A clone of the IV's base value for use in comparisons that need the original width.
- **`substIV`**: Replaces uses of the old IV with the demoted version.

## Multi-Pass Data Flow: Rematerialization / IV Demotion / NLO

The IR-level rematerialization pass (`nvvmrematerialize`) contains three cooperating sub-passes that execute in a fixed sequence within a single pass invocation. The following diagram shows the data each sub-pass produces and consumes, and the feedback loop that drives iterative pressure reduction.

```
               Live Variable Analysis (prerequisite)
               +------------------------------------+
               | Builds per-block live-in/live-out   |
               | bitvector sets via sub_1BFDF20     |
               | Produces:                           |
               |  - live-in bitvector per BB         |
               |  - live-out bitvector per BB        |
               |  - max live-in count (pressure)     |
               +------------------+-----------------+
                                  |
                                  v
  +===============================================================+
  |  MAIN REMATERIALIZATION LOOP (sub_1CE7DD0, up to 5 iterations)|
  |                                                               |
  |  Inputs:                                                      |
  |   - live-in bitvectors (from analysis above)                  |
  |   - register target (from occupancy model or 80% heuristic)  |
  |   - remat cost thresholds (knobs)                             |
  |                                                               |
  |  +----------------------------------------------------------+ |
  |  | Step 1: Compute intersection of live-in sets              | |
  |  | (bitwise AND across all blocks)                           | |
  |  | --> Values live everywhere = best candidates              | |
  |  +---------------------------+------------------------------+ |
  |                              |                                |
  |                              | candidate value set            |
  |                              v                                |
  |  +---------------------------+------------------------------+ |
  |  | Step 2: Pull-In Cost Analysis (sub_1CE3AF0)              | |
  |  | For each candidate:                                       | |
  |  |   cost = base_cost(def chain) * use_factor(loop nesting) | |
  |  | Filter by: remat-use-limit, remat-gep-cost,              | |
  |  |            remat-single-cost-limit                        | |
  |  | Sort by cost (cheapest first)                             | |
  |  | Produces: ranked list of N cheapest candidates            | |
  |  +---------------------------+------------------------------+ |
  |                              |                                |
  |                              | remat plan per block           |
  |                              v                                |
  |  +---------------------------+------------------------------+ |
  |  | Step 3: Block Executor (sub_1CE67D0)                     | |
  |  | For each selected candidate in each block:                | |
  |  |   "remat_" clone: full rematerialization at use site     | |
  |  |   "uclone_" clone: live range split within dom chain     | |
  |  | Produces:                                                 | |
  |  |   - cloned instructions at use sites                      | |
  |  |   - reduced live-in counts per block                      | |
  |  +---------------------------+------------------------------+ |
  |                              |                                |
  |                              | updated IR                     |
  |                              v                                |
  |  Recompute max live-in. If decreased and < 5 iters, loop.   |
  +=======================+=====================================+
                          |
                          | IR with reduced register pressure
                          v
  +=======================+=====================================+
  | IV DEMOTION (sub_1CD74B0, controlled by remat-iv)           |
  |                                                              |
  | Consumes:                                                    |
  |  - Loop header PHI nodes (from LoopInfo)                     |
  |  - Type widths (from DataLayout)                             |
  |  - post-remat IR (live ranges already shortened)             |
  |                                                              |
  | Algorithm:                                                   |
  |  for each loop L:                                            |
  |    for each 64-bit PHI in L.header:                          |
  |      if (val + 0x80000000) <= 0xFFFFFFFF:                    |
  |        create "demoteIV" (trunc to i32)                      |
  |        create "newBaseIV" (narrow PHI replacement)            |
  |        rewrite uses with "substIV"                            |
  |                                                              |
  | Produces:                                                    |
  |  - narrowed IVs (64->32 bit, halving register cost)          |
  |  - "iv_base_clone_" values for comparisons needing           |
  |    original width                                            |
  |  - updated loop exit conditions                              |
  +=======================+=====================================+
                          |
                          | IR with narrowed IVs
                          v
  +=======================+=====================================+
  | NLO -- SIMPLIFY LIVE OUTPUT (sub_1CE10B0, simplify-live-out)|
  |                                                              |
  | Consumes:                                                    |
  |  - per-block live-out bitvector sets                          |
  |  - post-IV-demotion IR                                       |
  |                                                              |
  | For each block's live-out set:                               |
  |  - If a value is live-out but only its low bits are used     |
  |    downstream: create "nloNewBit" (AND/extract/trunc)        |
  |  - If a value is an address live-out that can be recomputed  |
  |    locally in successors: create "nloNewAdd" (local add)     |
  |                                                              |
  | Produces:                                                    |
  |  - "nloNewBit" bit-narrowing instructions                    |
  |  - "nloNewAdd" local recomputation instructions              |
  |  - reduced live-out register count at block boundaries       |
  +=======================+=====================================+
                          |
                          | Final IR: pressure-reduced,
                          | IVs narrowed, live-outs simplified
                          v
  +-------------------------------------------------------+
  | Downstream consumers:                                  |
  |  - Instruction selection (register model now concrete) |
  |  - Machine-level remat (nv-remat-block, second pass)  |
  |  - Register allocation (lower pressure = higher occ.) |
  +-------------------------------------------------------+
```

**Data flow summary:**

| Producer | Data | Consumer |
|----------|------|----------|
| Live Variable Analysis | Per-block live-in/live-out bitvectors | Main remat loop |
| Occupancy model (`sub_1C01730`) | Register pressure target | Main remat loop |
| Main remat loop | `remat_`/`uclone_` cloned instructions | Updated IR for IV demotion |
| IV Demotion | `demoteIV`, `newBaseIV`, `substIV` narrowed values | NLO and downstream |
| NLO | `nloNewBit`, `nloNewAdd` local recomputations | Final IR for instruction selection |
| All three sub-passes | Cumulative register pressure reduction | Machine-level remat (`nv-remat-block`) |

The sequencing is important: the main loop reduces cross-block live-in pressure first (the broadest and cheapest wins), IV demotion then halves the cost of loop induction variables (converting two registers to one), and NLO cleans up block-boundary live-out values that survived both earlier phases. The machine-level `nv-remat-block` pass runs much later in the pipeline (after instruction selection and register allocation) as a final safety net, operating on concrete register assignments rather than abstract SSA values.

## Machine-Level Block Rematerialization (`nv-remat-block`)

### Registration

Registered at `ctor_361_0` (address `0x5108E0`) with pass name `"nv-remat-block"` and description `"Do Remat Machine Block"`. Main entry point: `sub_2186D90` (47KB, ~1742 lines).

### Algorithm Overview

The machine-level pass implements a sophisticated iterative pull-in algorithm operating on MachineIR after instruction selection:

1. **Measure**: Compute max-live register pressure across all blocks via `sub_2186590`. Prints `"Max-Live-Function(<num_blocks>) = <max_live>"`.

2. **Identify**: For each block where pressure exceeds the target, enumerate live-out registers.

3. **Classify**: For each live-out register, determine pullability:
   - **MULTIDEF check** (`sub_217E810`): The register must have exactly one non-dead, non-debug definition. Registers with multiple definitions print `"MULTIDEF"` and are rejected.
   - **Opcode exclusion**: A large switch/comparison tree excludes memory ops, atomics, barriers, texture ops, surface ops, and other side-effecting instructions. Specific exclusions exist for sm_62 (opcodes 380-396).
   - **Operand safety**: Instructions that define additional tied registers beyond the target are rejected.
   - **Recursive verification** (`sub_2181550`): All operands of the defining instruction must themselves be pullable, checked recursively up to depth 50.

4. **Second-chance heuristic** (`sub_2181870`): Registers initially rejected because one of their operands was non-pullable are re-evaluated when those operands become pullable. This iterates until convergence, using a visit-count mechanism to prevent infinite loops. The hash function throughout is `h(regID) = 37 * regID`. Debug: `"After pre-check, <N> good candidates, <N> given second-chance"`, `"ADD <N> candidates from second-chance"`.

5. **Cost analysis** (`sub_2183E30`): Each candidate receives a clone cost. Candidates with cost 0 are non-rematerializable.

6. **Selection**: Sort candidates by cost (ascending). Greedily select the cheapest candidates until pressure is reduced to target. Double-wide register classes (size > 32) count as 2 for pressure purposes and have their cost doubled. Debug: `"Really Final Pull-in: <count> (<total_cost>)"`.

7. **Execute**: For each selected register:
   - Clear from live-out bitmap via `sub_217F620`.
   - Propagate backward through predecessors via `sub_2185250`.
   - Clone the defining instruction at use sites via `sub_217E1F0`.
   - Replace register references via `sub_21810D0`.
   - Remove now-dead original definitions.

8. **Iterate**: Repeat up to `nv-remat-max-times` (default 10) iterations until max pressure is at or below target, or no further progress is made.

### Instruction Replacement (`sub_21810D0`)

When replacing a rematerialized register:
1. Create a new virtual register of the same class via `sub_1E6B9A0`.
2. Call the target's `replaceRegWith` method (vtable offset 152).
3. Walk all uses of the original register ID and rewrite operands via `sub_1E310D0`.
4. Handle special cases: `DBG_VALUE` (opcode 45) and NOP/PHI (opcode 0) instructions use stride-2 operand scanning.

### Register Pressure Computation (`sub_2186590`)

Per-block pressure is computed by starting with the live-out set size, walking instructions in reverse, tracking register births (defs) and deaths (last uses), and recording the peak pressure point. The maximum across all blocks is returned.

## Key Functions

### IR-Level

| Function | Address | Size | Role |
|----------|---------|------|------|
| Pass registration | `sub_1CD0BE0` | -- | Registers `"nvvmrematerialize"` |
| Main driver | `sub_1CE7DD0` | 67KB | Iterative live-in reduction loop |
| Block executor | `sub_1CE67D0` | 32KB | `"remat_"` / `"uclone_"` creation |
| Pull-in cost | `sub_1CE3AF0` | 56KB | Cost model and candidate selection |
| NLO main | `sub_1CE10B0` | 48KB | Live-out normalization |
| NLO helper | `sub_1CDC1F0` | 35KB | Inter-block NLO propagation |
| IV demotion | `sub_1CD74B0` | 75KB | Induction variable narrowing |
| Load remat | `sub_1CDE4D0` | -- | Load rematerialization sub-pass |
| Per-function init | `sub_1CDA600` | -- | Data structure initialization |
| Rematizability check | `sub_1CD06C0` | -- | Determines if a value can be recomputed |

### Machine-Level

| Function | Address | Size | Role |
|----------|---------|------|------|
| Main engine | `sub_2186D90` | 47KB | Iterative pull-in algorithm |
| Max-live computation | `sub_2186590` | -- | Per-block pressure analysis |
| MULTIDEF check | `sub_217E810` | ~230 lines | Single-definition verification |
| Recursive pullability | `sub_2181550` | ~110 lines | Operand chain verification (depth 50) |
| Second-chance | `sub_2181870` | ~800 lines | Re-evaluation of rejected candidates |
| Cost evaluator | `sub_2183E30` | -- | Clone cost computation |
| Liveness propagation | `sub_2185250` | ~650 lines | Backward propagation + cloning |
| Instruction replacement | `sub_21810D0` | ~290 lines | Register use rewriting |
| Remat allocation helper | `sub_2184890` | ~477 lines | Pressure simulation |

## Configuration Knobs

### IR-Level Knobs (ctor_277_0 at `0x4F7BE0`)

| Knob | Global | Default | Description |
|------|--------|---------|-------------|
| `do-remat` | `dword_4FC05C0` | 3 | Master control. 0=off, 1=conservative, 2=normal, 3=full. |
| `no-remat` | `qword_4FC0440` | (empty) | Comma-separated function exclusion list |
| `remat-iv` | `dword_4FBFB40` | 4 | IV demotion level. 0=off, 4=full. |
| `remat-load` | `dword_4FBFA60` | 1 | Load rematerialization. 0=off, 1=on. |
| `remat-add` | `dword_4FBF980` | 0 | Add/GEP factoring. 0=off. |
| `remat-single-cost-limit` | `dword_4FC0080` | 6000 | Max cost per single live-in reduction |
| `remat-loop-trip` | `dword_4FBFFA0` | 20 | Default assumed loop trip count |
| `remat-gep-cost` | `dword_4FBFEC0` | 6000 | Max cost for GEP rematerialization |
| `remat-use-limit` | `dword_4FBFDE0` | 10 | Max number of uses for a candidate |
| `remat-max-live-limit` | `dword_4FBFD00` | 10 | Max live-in limit for rematerialization |
| `remat-maxreg-ceiling` | `dword_4FBF600` | 0 | Register ceiling (0 = uncapped) |
| `remat-for-occ` | `dword_4FBF8A0` | 120 | Occupancy-driven rematerialization target |
| `remat-lli-factor` | `dword_4FC0320` | 10 | Long-latency instruction cost factor |
| `remat-ignore-single-cost` | `byte_4FBFC20` | false | Bypass per-value cost filter |
| `remat-move` | `byte_4FC0400` | false | Remat move instructions |
| `simplify-live-out` | `dword_4FBF520` | 2 | NLO level. 0=off, 2=full. |
| `dump-remat` | `dword_4FC0240` | 0 | Debug dump level (0-4+) |
| `dump-remat-iv` | `dword_4FC0160` | 0 | IV remat debug dump |
| `dump-remat-load` | `dword_4FBF720` | 0 | Load remat debug dump |
| `dump-remat-add` | `dword_4FBF640` | 0 | Add remat debug dump |
| `dump-simplify-live-out` | `byte_4FBF400` | false | NLO debug dump |

### Machine-Level Knobs (ctor_361_0 at `0x5108E0`)

| Knob | Global | Default | Description |
|------|--------|---------|-------------|
| `nv-remat-block` | `dword_4FD3820` | 14 | Bitmask controlling remat modes (bits 0-3) |
| `nv-remat-max-times` | `dword_4FD3740` | 10 | Max outer loop iterations |
| `nv-remat-block-single-cost` | `dword_4FD3660` | 10 | Max cost per single live value pull-in |
| `nv-remat-block-map-size-limit` | `dword_4FD3580` | 6 | Map size limit for single pull-in |
| `nv-remat-block-max-cost` | `dword_4FD3040` | 100 | Max total clone cost per live value reduction |
| `nv-remat-block-liveout-min-percentage` | `dword_4FD3120` | 70 | Min liveout % for special consideration |
| `nv-remat-block-loop-cost-factor` | `unk_4FD3400` | 20 | Loop cost multiplier |
| `nv-remat-default-max-reg` | `unk_4FD3320` | 70 | Default max register pressure target |
| `nv-remat-block-load-cost` | `unk_4FD2EC0` | 10 | Cost assigned to load instructions |
| `nv-remat-threshold-for-spec-reg` | `unk_4FD3860` | 20 | Threshold for special register remat |
| `nv-dump-remat-block` | `byte_4FD2E80` | false | Debug dump toggle |
| `nv-remat-check-internal-live` | `byte_4FD2DA0` | false | Check internal liveness during MaxLive |
| `max-reg-kind` | `qword_4FD2C20` | 0 | Kind of max register pressure info |
| `no-mi-remat` | `qword_4FD2BE0` | (empty) | Skip remat for named functions |
| `load-remat` | `word_4FD32F0` | true | Enable load rematerialization |
| `vasp-fix1` | `word_4FD3210` | false | VASP fix for volatile/addsp |

### Complementary ptxas-side Knobs

The assembler (ptxas) has its own rematerialization controls that complement the CICC passes:

- `RegAllocRematEnable=1`
- `RegAllocEnableOptimizedRemat=1`
- `RematEnable=1`
- `SinkRematEnable=1`
- `RematBackOffRegTargetFactor=N`

## Optimization Level Behavior

| Level | IR-Level Remat (`nvvmrematerialize`) | Machine-Level Remat (`nv-remat-block`) |
|-------|--------------------------------------|---------------------------------------|
| **O0** | Not run | Not run |
| **Ofcmax** | Not run | Not run |
| **Ofcmid** | Runs with `do-remat=3` (full) | Not run |
| **O1** | Runs with `do-remat=3`, `remat-iv=4`, `remat-load=1` | Runs with `nv-remat-block=14` (default bitmask) |
| **O2** | Same as O1 | Same as O1 |
| **O3** | Same as O1; may see more candidates due to additional inlining/unrolling | Same as O1; operates on more aggressively optimized MIR |

The `do-remat` master control (default 3) enables all rematerialization sub-phases at O1+. The machine-level pass is gated by its own NVVMPassOptions slot and runs only when the codegen pipeline includes the full register allocation sequence. At Ofcmax, neither pass runs because the fast-compile pipeline skips the full optimization and codegen stack. See [Optimization Levels](../config/optimization-levels.md) for the complete pipeline tier structure.

## Diagnostic Strings

```
"Skip rematerialization on <funcname>"
"Block %s: live-in = %d"
"Total pull-in cost = %d"
"remat_"
"uclone_"
"nloNewBit"
"nloNewAdd"
"demoteIV"
"newBaseIV"
"iv_base_clone_"
"substIV"
"factor"
"Max-Live-Function(<num_blocks>) = <max_live>"
"Really Final Pull-in: <count> (<total_cost>)"
"MULTIDEF"
"Skip machine-instruction rematerialization on <name>"
"After pre-check, <N> good candidates, <N> given second-chance"
"ADD <N> candidates from second-chance"
"Pullable: <count>"
"live-out = <count>"
"Total Pullable before considering cost: <count>"
```

## Reimplementation Checklist

1. **Live-in/live-out bitvector analysis.** Build per-basic-block bitvector sets tracking which values are live-in and live-out, compute max live-in via hardware `popcnt`, and maintain a hash map of per-block counts.
2. **Occupancy-driven register target.** Query the occupancy model to compute a target register count (default: `remat-for-occ=120`), apply heuristic adjustments based on occupancy cliff boundaries, and cap at `remat-maxreg-ceiling` when set.
3. **Candidate selection and cost model.** Compute the live-in intersection across all blocks (bitwise AND), check rematerizability of each candidate via def-chain walking (bounded by `max-recurse-depth`), score candidates as `base_cost * use_factor` with loop-nesting scaling, filter by `remat-use-limit`/`remat-gep-cost`/`remat-single-cost-limit`, and sort cheapest-first.
4. **Block-level instruction cloning.** Implement two clone types: `remat_` prefix clones (full rematerialization of live-in values at use sites) and `uclone_` prefix clones (use-level copies for live range splitting within the dominance chain), with proper use-def chain and debug location updates.
5. **IV demotion sub-pass.** Identify 64-bit loop-header PHI nodes whose value range fits in 32 bits (`(val + 0x80000000) <= 0xFFFFFFFF`), create narrowed PHI replacements (`demoteIV`/`newBaseIV`/`substIV`), and rewrite loop exit conditions.
6. **NLO live-out simplification.** Walk each block's live-out set, create `nloNewBit` instructions (AND/extract/trunc to actual used bit-width) and `nloNewAdd` instructions (local address recomputations) to reduce live-out register count at block boundaries.
7. **Machine-level pull-in algorithm (`nv-remat-block`).** Implement the iterative MachineIR rematerialization engine: max-live computation via reverse instruction walk, MULTIDEF verification, recursive pullability checking (depth 50), second-chance heuristic for re-evaluating rejected candidates, cost-sorted greedy selection, and liveness propagation with instruction cloning at use sites.
8. **Iterative convergence loop.** Wrap the IR-level pass in an up-to-5-iteration loop (recompute max live-in after each round, stop when target is met) and the machine-level pass in an up-to-`nv-remat-max-times` loop.

## Architecture-Specific Behavior

The machine-level MULTIDEF checker (`sub_217E810`) contains architecture-specific opcode exclusions: opcodes 380-396 are rejected only when the target SM is sm_62 (GP106, mid-range Pascal), suggesting these instructions have rematerialization hazards specific to that microarchitecture. All other opcode exclusions apply uniformly across SM targets.

## Test This

The following kernel creates high register pressure by keeping many independent values alive simultaneously. Compile with `nvcc -ptx -arch=sm_90 -maxrregcount=32` to force a low register cap and observe rematerialization in action.

```cuda
__global__ void remat_test(const float* __restrict__ in, float* __restrict__ out, int n) {
    int tid = blockIdx.x * blockDim.x + threadIdx.x;
    if (tid >= n) return;

    float a = in[tid];
    float b = in[tid + n];
    float c = in[tid + 2*n];
    float d = in[tid + 3*n];
    float e = in[tid + 4*n];
    float f = in[tid + 5*n];
    float g = in[tid + 6*n];
    float h = in[tid + 7*n];

    float r0 = a * b + c;
    float r1 = d * e + f;
    float r2 = g * h + a;
    float r3 = b * c + d;
    float r4 = e * f + g;

    out[tid]       = r0 + r1;
    out[tid + n]   = r2 + r3;
    out[tid + 2*n] = r4 + r0;
}
```

**What to look for in PTX:**
- Address recomputation: the expressions `tid + k*n` are cheap to recompute. With `-maxrregcount=32`, the pass should rematerialize these address calculations at use sites rather than keeping them in registers. Look for repeated `mad.lo.s32` or `add.s32` instructions computing the same offset near each `ld.global` instead of a single computation early on.
- Compare the `.nreg` directive value between `-maxrregcount=32` and the default. The rematerialization pass trades extra ALU instructions for fewer registers to hit the lower target.
- With `-Xcicc -dump-remat=4`, cicc prints `"Total pull-in cost = %d"` for each candidate, showing the cost/benefit analysis.
- The `remat_` prefix on SSA names in LLVM IR dumps identifies rematerialized values.

## Pipeline Interaction

The IR-level pass runs after live variable analysis has been computed and before instruction selection. Its register pressure reduction directly influences the occupancy achievable by the final kernel. The machine-level pass runs later, after instruction selection and register allocation, providing a second opportunity to reduce pressure on MachineIR where the register model is concrete rather than abstract. Together, the two passes form a layered rematerialization strategy: the IR pass makes broad, cost-effective reductions early, and the machine pass performs precise, targeted reductions late. Both passes interact with the register pressure analysis (`rpa` / `machine-rpa`) that feeds pressure estimates into scheduling and allocation decisions throughout the pipeline.
