# Sinking2 (NVIDIA Code Sinking)

`sinking2` is an NVIDIA-proprietary instruction sinking pass that moves instructions closer to their uses, with specific awareness of GPU texture and surface memory operations. It is entirely distinct from LLVM's stock `sink` pass: while both perform code sinking, Sinking2 is tailored for NVIDIA's memory hierarchy and iterates to a fixed point rather than making a single pass. The primary motivation is reducing register pressure by deferring computation of values until just before they are consumed, which is especially impactful on GPUs where register files are shared across hundreds of concurrent threads.

The pass is particularly focused on sinking instructions into texture load blocks. Texture operations on NVIDIA GPUs have high latency but are served by a dedicated cache; by sinking the address computation and other operands into the block that performs the texture fetch, the compiler reduces the live range of those values and frees registers for other warps. This directly improves occupancy -- the number of warps that can execute simultaneously on an SM.

## Pipeline Position

| Field | Value |
|---|---|
| Pass name (pipeline) | `sinking2` |
| Pass ID | `sink2` |
| Display name | `Code sinking` |
| Pass type | FunctionPass (NVIDIA-custom) |
| Class | `llvm::Sinking2Pass` |
| Legacy PM entry | `sub_1CCA270` |
| New PM entry | `sub_2D1C160` (19KB) |
| Legacy PM registration | `sub_1CC7010` |
| New PM registration | `sub_2D1B410` |
| Knob constructor | `ctor_275` at `0x4F7750` |
| Vtable (Legacy) | `off_49F8BC0` |
| Vtable (New PM) | `off_4A260F0` |

## Relationship to Other Sink Passes

CICC contains **three** distinct sinking passes. Understanding which is which is essential when reading the pipeline:

| Pass ID | Class | Origin | Key Difference |
|---|---|---|---|
| `sink` | LLVM SinkingPass | Upstream LLVM | Stock single-pass sinking |
| `sink2` | `llvm::Sinking2Pass` | NVIDIA | Texture-aware, iterative, GPU-specific |
| `sink<rp-aware>` | Parameterized variant | LLVM + NVIDIA | Register-pressure-aware sinking |

Sinking2 differs from stock LLVM sink in four ways: (1) explicit texture/surface memory awareness with GPU address space checks, (2) configurable sinking levels (cross-block, intra-block, outside-only), (3) iteration to convergence rather than a single pass, and (4) GPU-specific profitability heuristics that account for the NVIDIA memory hierarchy.

## Algorithm

### Entry Point

The legacy PM entry `sub_1CCA270` performs these steps:

1. Fetches `DominatorTree` analysis (via `DominatorTreeWrapperPass` at `unk_4F9E06C`)
2. Fetches `LoopInfo` analysis (via `LoopInfoWrapperPass` at `unk_4F96DB4`)
3. Checks `sink-into-texture` knob -- must be non-zero (enabled)
4. Checks `sink-limit` knob -- must be greater than zero
5. Calls the main worklist driver `sub_1CC9110`

The pass does **not** require ScalarEvolution (SCEV), keeping it simpler and cheaper than loop-oriented passes.

### Main Worklist Driver (`sub_1CC9110`, 22KB)

The core algorithm proceeds as follows:

1. **Walk the dominator tree** starting from the function's entry block, visiting children in DFS order.
2. **For each basic block**, enumerate all instructions and check whether each has more than one use (via `sub_15F4D60`).
3. **Look up each instruction** in a hash-based "sinkable set" to determine if it is a candidate.
4. **For each use**, determine the target block where the value is consumed.
5. **Apply profitability heuristic**: check whether the target block contains texture or surface operations. Instructions feeding texture loads are strongly preferred for sinking.
6. **Move the instruction** to the target block if the transformation is profitable and safe.
7. **Repeat until convergence** -- the algorithm iterates until no further sinking opportunities remain.

### Instruction Processing (`sub_1CC7510`, 16KB)

For each candidate instruction, this function:

- Walks the use chain to find all consumers
- Looks up dominator tree entries to determine the lowest common dominator of all uses
- Builds a sink mapping (instruction to target block)
- Checks memory safety using `AliasAnalysis` (via `sub_13575E0`)
- Validates that sinking does not violate memory ordering constraints
- Respects PHI nodes (opcode 78) as sink boundaries

### Dominance Ordering (`sub_1CC8170`, 13KB)

Implements a hash-based ordering of basic blocks for comparing sink profitability. Uses DFS numbering from the dominator tree to determine which block comes "earlier" in the program, ensuring the pass sinks instructions in the correct direction (always toward uses, never away from them).

### Texture Awareness

The pass specifically recognizes instructions feeding texture operations:

- **Opcode 78** (`0x4E`): Texture intrinsic calls
- **Opcode 54** (`0x36`): Store instructions with specific address space checks

The `sink-into-texture` knob controls how aggressively the pass sinks toward texture blocks:

| Level | Behavior |
|---|---|
| 0 | Disabled -- no texture-aware sinking |
| 1 | Cross-block sinking only: move instructions across block boundaries into texture blocks |
| 2 | Cross-block + intra-block: also reorder instructions within a block to be closer to texture uses |
| 3 (default) | All of the above + consider instructions used only outside the current block |

### Memory Safety

Two helper functions enforce correctness:

- **`sub_1CC8920`** (4KB): Checks aliasing constraints -- ensures that moving an instruction does not reorder it past a conflicting memory access.
- **`sub_1CC8CA0`** (6KB): Checks memory dependency -- validates that the sink destination does not introduce new data dependencies.

## Configuration Knobs

| Knob | Type | Default | Description |
|---|---|---|---|
| `sink-into-texture` | int | 3 | Texture sinking aggressiveness (0=off, 1=cross-block, 2=+intra, 3=+outside-only) |
| `sink-limit` | int | 20 | Max instructions to sink per invocation (complexity limiter) |
| `dump-sink2` | bool | false | Dump debug information during sinking |

**Related knobs (stock LLVM `sink`, NOT Sinking2):**

| Knob | Type | Default | Description |
|---|---|---|---|
| `sink-check-sched` | bool | true | Check scheduling effects of sinking |
| `sink-single-only` | bool | true | Only sink single-use instructions |
| `rp-aware-sink` | bool | false | Consider register pressure (controls `sink<rp-aware>` variant) |

## Analysis Dependencies

| Legacy PM | New PM | Purpose |
|---|---|---|
| `DominatorTreeWrapperPass` (`unk_4F9E06C`) | `DominatorTreeAnalysis` (`sub_CF6DB0`) | Dominator tree for sink legality |
| `LoopInfoWrapperPass` (`unk_4F96DB4`) | `LoopAnalysis` (`sub_B1A2E0`) | Avoid sinking out of loops |

## Pass Object Layout

**Legacy PM** (160 bytes):

| Offset | Content |
|---|---|
| +0 | Vtable pointer (`off_49F8BC0`) |
| +8 | Pass link |
| +16 | Pass ID pointer (`&unk_4FBF0F4`) |
| +24 | Mode (int, value 3 = default `sink-into-texture` level) |
| +32--48 | Worklist data |
| +64 | List head 1 (self-referential sentinel) |
| +96 | Counter |
| +112 | List head 2 (self-referential sentinel) |
| +152 | Flag byte |

**New PM** (176 bytes): two embedded worklists and float thresholds at offsets +88 and +144 (value `1065353216` = `1.0f` IEEE 754).

## Diagnostic Strings

| String | Context |
|---|---|
| `"llvm::Sinking2Pass]"` | RTTI name at `sub_2315E20` |
| `"sink2"` | Pipeline parser ID |
| `"Code sinking"` | Display name (shared with stock LLVM sink) |
| `"sinking2"` | New PM pipeline string match |

## Function Map

| Address | Size | Role |
|---|---|---|
| `0x1CCA270` | — | Legacy PM `runOnFunction` entry |
| `0x2D1C160` | 19KB | New PM `run` entry |
| `0x1CC7010` | — | Legacy PM pass registration |
| `0x2D1B410` | — | New PM pass registration |
| `0x1CC7100` | — | Legacy PM factory |
| `0x2D1BC50` | — | New PM factory |
| `0x1CC7510` | 16KB | Process instruction (sink candidate evaluation) |
| `0x1CC8170` | 13KB | Dominance ordering (DFS numbering) |
| `0x1CC9110` | 22KB | Main worklist driver (iterates to fixpoint) |
| `0x1CC8920` | 4KB | Alias checking helper |
| `0x1CC8CA0` | 6KB | Memory dependency checking helper |
| `0x2D1CFB0` | 13KB | New PM core logic |
| `0x2D1D770` | 7KB | New PM helper |
| `0x2D1DCF0` | 7KB | New PM helper |

**Total code size:** ~80KB (Legacy PM) + ~65KB (New PM) = ~145KB

## GPU-Specific Motivation

Register pressure is the dominant performance constraint on NVIDIA GPUs. Each SM has a fixed register file (e.g., 65,536 registers on SM 8.x) shared among all active warps. Every register consumed by a warp reduces the number of warps that can be resident, directly reducing occupancy and the GPU's ability to hide memory latency through warp switching.

Sinking instructions closer to their uses shortens live ranges and reduces the peak number of simultaneously live registers. This is especially valuable for texture load sequences, which typically involve address computation (GEP chains, index arithmetic) that produces values consumed only at the texture fetch site. Without sinking, these intermediate values occupy registers across potentially many instructions, bloating register pressure unnecessarily.

The three-level `sink-into-texture` design reflects a graduated approach to this optimization: level 1 handles the common case (cross-block sinking), level 2 adds intra-block reordering for tighter packing, and level 3 (the default) handles the edge case where an instruction's only uses are in blocks other than where it is defined, enabling more aggressive motion.
