# ISel Pattern Matching & Instruction Selection

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

The NVPTX instruction selector in cicc v13.0 translates legal SelectionDAG nodes into target MachineInstr opcodes through a three-level dispatch hierarchy totaling approximately 900KB of code. At the top sits `NVPTXDAGToDAGISel::Select` (`sub_3090F90`, 91KB), which builds a per-function cost table, manages a priority-queue-driven topological worklist, and calls the pattern matcher (`sub_308FEE0`) for every node. The pattern matcher fans out to a hand-written NVPTX-specific select switch (`sub_347A8D0`, 309KB) and a TableGen-generated `SelectCode` function (`sub_348D3E0`, 256KB). Surrounding this core are six NVPTX-specific sub-selectors covering memory operations, texture/surface fetches, complex addressing modes, vector patterns, and atomics. NVIDIA's key delta from upstream LLVM is (1) a compressed per-SM-variant legality table that gates which target opcodes exist on which GPU architecture, (2) a secondary 4-bit packed bitfield for fine-grained operand-class legality, and (3) the iteration budget that prevents the selector from looping indefinitely on pathological DAGs.

| | |
|---|---|
| **ISel driver** | `sub_3090F90` (91KB, 2,828 lines) |
| **Pattern matcher entry** | `sub_308FEE0` |
| **NVPTX Select switch** | `sub_347A8D0` (309KB -- largest ISel function) |
| **SelectCode (TableGen)** | `sub_348D3E0` (256KB -- auto-generated) |
| **Vector/SIMD patterns** | `sub_3475BB0` (89KB) |
| **Memory operation patterns** | `sub_306D850` (77KB) |
| **Complex addressing modes** | `sub_30811D0` (77KB) |
| **Addressing mode helper** | `sub_30783B0` (39KB) |
| **Texture/surface ISel** | `sub_306A930` (52KB) |
| **Atomic lowering** | `sub_3048C30` (86KB) |
| **Constraint table** | `word_3F3E6C0` (see [Pattern Database](../structs/pattern-db.md)) |
| **Compressed legality table** | Base + 6414, 500-byte stride per SM variant |
| **Secondary 4-bit bitfield** | Base + 521536 |
| **Legalize action table** | Object + 72760, 4-bit packed |
| **Knob registration** | `ctor_286` at `0x4FA0C0` (5KB) |
| **Upstream LLVM source** | `lib/CodeGen/SelectionDAG/SelectionDAGISel.cpp`, `lib/Target/NVPTX/NVPTXISelDAGToDAG.cpp` |

## ISel Driver: `sub_3090F90`

The top-level driver is *not* the pattern matcher itself; it is the orchestration loop that feeds nodes to the matcher in the right order and maintains shared state. It breaks into three phases.

### Phase 1: Function Argument Cost Table

Before selecting any instructions, the driver builds a DenseMap-style hash table at `this + 408` that maps function argument indices to their byte sizes. The hash table uses LLVM's standard integer-key hash function `key * 37`, open addressing with linear probing, and the tombstone sentinel `-2`. Growth triggers at 75% load factor (`4 * (count + 1) >= 3 * capacity`).

```
// Phase 1: build argument cost table
hash_table = this->arg_cost_map;  // at this + 408
for each argument A in function->args():
    byte_size = alignTo(getSizeInBits(A.type) / 8, A.alignment)
    key = A.index
    slot = (key * 37) & (capacity - 1)
    while hash_table[slot] is occupied and != key:
        slot = (slot + 1) & (capacity - 1)
    hash_table[slot] = { key, byte_size }
    if load_factor > 0.75: rehash()
```

The table layout:

| Field | Offset from `this` | Description |
|---|---|---|
| `data` | +416 | Pointer to hash bucket array |
| `count` | +424 | Number of live entries |
| `tombstone_count` | +428 | Number of tombstone slots |
| `capacity` | +432 | Total bucket count (power of 2) |

If the function has a non-void return type, the driver also inserts the return value sizes into the same table, computing `aligned_size = ((size + 7) >> 3 + (1 << align) - 1) >> align << align` for each return element. The return-type attribute check uses attribute kind 81 (likely `sret`).

### Phase 2: Return Value Processing

For non-void functions, the driver iterates each return value element via:

- `sub_A74710(attribute, 81)` -- checks for `sret` attribute
- `sub_A748A0(index)` -- gets return type at given index
- `sub_AE5020(dataLayout, type)` -- computes ABI alignment
- `sub_9208B0(dataLayout, type)` -- computes size in bits

Each return value's aligned byte size is inserted into the argument cost table, so the pattern matcher can look up the cost of materializing any function parameter or return value during instruction selection.

### Phase 3: Topological Selection Loop

The main selection loop processes DAG nodes in topological order using a min-heap priority queue where priority equals topological order (lower number = earlier in the DAG, processed first). The iteration is bounded by an explicit budget.

```
// Phase 3: main ISel loop
sub_308B6F0(this);  // initialize worklist from DAG
budget = 4 * numInstructions * maxBlockSize
iteration = 0

while heap is not empty:
    node = heap.extractMin()         // sub_3089BD0: heap-sift-down
    sub_308FEE0(this, node, &tmp)    // pattern matcher dispatch

    if this->selectionChanged:       // byte at this + 400
        re-scan affected nodes

    iteration++
    if iteration > budget:
        break  // anti-infinite-loop guard

sub_308AB30(this)    // cleanup
sub_264E600(this)    // deallocate worklist
sub_308B100(this)    // destroy hash table
```

The min-heap stores `(SDNode*, priority)` pairs at 16-byte stride. The heap-sift-down operation (`sub_3089BD0`) maintains the heap invariant after extraction. The `selectionChanged` flag at `this + 400` is set by the pattern matcher when it replaces a node, signaling the driver to re-examine downstream users.

The iteration budget formula `4 * numInstructions * maxBlockSize` is an NVIDIA addition -- upstream LLVM's `SelectionDAGISel` does not have this guard. It prevents pathological DAGs (for example, from heavily-inlined device functions with thousands of parameters) from causing the selector to spin indefinitely when combine/legalize/select cycles interact.

## Pattern Matcher Dispatch: `sub_308FEE0`

The pattern matcher is called once per SDNode. It reads the node's opcode at `*(node + 24)` and dispatches through a multi-level decision tree:

1. **Quick-reject filter.** If the node is already selected (machine opcode bit set in flags), return immediately.
2. **NVPTX-specific hand-written patterns.** Calls `sub_347A8D0` for NVPTX custom opcodes (NVPTXISD range >= 499). This handles texture loads, MMA instructions, atomic operations, `.param`-space loads/stores, and other GPU-specific patterns.
3. **TableGen auto-generated matcher.** Calls `sub_348D3E0` (`SelectCode`) for standard ISD opcodes. This function is mechanically generated from the `.td` pattern files in the NVPTX backend and contains a massive switch table mapping DAG patterns to MachineInstr opcodes.
4. **Complex pattern matching.** For load/store addressing modes, calls `sub_30811D0` (77KB) and `sub_30783B0` (39KB), which match `base + offset`, `base + scaled_index`, and address-space-qualified patterns.
5. **Fallback.** If no pattern matches, the node is marked as "failed ISel" and the driver may retry after DAG combining.

### NVPTX Select Switch: `sub_347A8D0` (309KB)

This is the largest single ISel function, containing the hand-written pattern matching for all NVIDIA-specific DAG nodes. It calls `sub_969240` 263 times (SDNode accessor), is self-recursive 42 times, and dispatches to:

| Sub-selector | Size | Coverage |
|---|---|---|
| `sub_3447D70` | 32KB | Specific pattern sub-dispatch |
| `sub_3441190` | -- | Pattern helpers |
| `sub_343FD60` | -- | Type-aware matching |
| `sub_3475BB0` | 89KB | Vector/SIMD patterns (v2, v4 packed types) |

The function switches on the SDNode opcode to handle:

- **Load/store with address spaces** -- selects between `ld.global`, `ld.shared`, `ld.local`, `ld.param`, `ld.const`, and generic-space loads, each requiring different PTX instructions.
- **Texture/surface operations** -- dispatches to `sub_306A930` for `tex`, `suld`, `sust` instruction patterns.
- **MMA/WMMA/tensor ops** -- selects the correct `mma.sync`, `wmma.mma`, `wgmma` variant based on operand types and SM architecture.
- **Atomic operations** -- selects between `atom.global.add`, `atom.shared.cas`, `red.global.add`, etc., with scope qualifiers (`.cta`, `.gpu`, `.sys`).
- **Barrier/fence operations** -- selects `bar.sync`, `bar.warp.sync`, `membar.cta`, `membar.gl`, `membar.sys`.

### SelectCode (TableGen): `sub_348D3E0` (256KB)

This auto-generated function implements the standard LLVM TableGen pattern matching algorithm. It is a giant switch-table compiled from the `.td` instruction pattern files in `lib/Target/NVPTX/*.td`. The function:

- Calls `sub_969240` 45 times and `sub_32889F0` 38 times (opcode/type checkers).
- Contains no string literals (purely mechanical code).
- Works in tandem with `sub_347A8D0`: the hand-written selector handles NVPTX custom nodes first, and anything that falls through goes to `SelectCode`.

The auto-generated matcher encodes patterns as a sequence of opcode checks, type checks, and operand recursive matches. When a full pattern matches, it calls `MorphNodeTo` to convert the SDNode into a MachineSDNode with the target opcode and register operands.

## Compressed Instruction Legality Table

NVIDIA's instruction selector uses a per-SM-variant legality table to determine whether a given target opcode is legal on the current GPU architecture. This table is checked during instruction selection to gate SM-specific instructions (for example, `wgmma` instructions are illegal on SM 70 but legal on SM 90+).

The table lives at a fixed offset from the base of the ISel object, accessed by `sub_376DE90`:

```
legality = *(uint8_t*)(base + 500 * arch_variant + opcode + 6414)
```

| Field | Encoding |
|---|---|
| **Base offset** | 6414 bytes from object base |
| **Row stride** | 500 bytes per architecture variant |
| **Index** | `500 * arch_variant + opcode` |
| **Value 0** | Illegal -- this opcode does not exist on this SM |
| **Value 1** | Custom -- requires custom lowering before emission |
| **Value 2** | Legal -- can be emitted directly |

The `arch_variant` value selects which row of the table to consult. Each row contains 500 entries, one per target opcode. The table is read-only after initialization and occupies approximately `num_variants * 500` bytes in the `.data` section.

### Secondary 4-bit Packed Bitfield

A second legality table at `base + 521536` provides fine-grained operand-class legality using 4-bit packed nibbles:

```
byte_offset = (opcode_class >> 3) + 36 * arch_id - arch_id
nibble      = (*(uint8_t*)(base + 521536 + byte_offset) >> (4 * (opcode_class & 7))) & 0xF
```

The offset simplification `36 * arch_id - arch_id` equals `35 * arch_id`, giving a 35-byte stride per architecture variant. Each byte packs two 4-bit legality fields, and the low/high nibble is selected by bit 0 of `opcode_class`. The 4-bit values encode a richer set of actions than the primary table's 3-value encoding.

## Legalize Action Table

The operation legalization subsystem (separate from the ISel legality table above) uses a 4-bit packed action table at object offset 72760 to determine how to legalize each `(opcode, type)` pair:

```
index  = type_bits + 15 * opcode + 18112
action = (*(uint32_t*)(object + 4 * index + 72760) >> (4 * (type & 7))) & 0xF
```

| Action | Value | Behavior |
|---|---|---|
| Legal | 0 | Node is natively supported |
| Promote | 1 | Widen to a larger legal type |
| Custom | 5 | Call `NVPTXTargetLowering::LowerOperation` via vtable slot 164 |
| ExpandInteger | 9 | Split wide integers into halves |
| ExpandFloat | 13 | Emulate unsupported FP via libcalls |
| SplitVector | 14 | Decompose illegal vector into legal sub-vectors |

This table is distinct from the type-legality table at `TLI + 2422` (described in [SelectionDAG](./selectiondag.md)), which uses a 259-byte stride and encodes the simpler 5-action set (Legal/Custom/Expand/LibCall/Promote). The table at +72760 is the operation-level action table used during the `LegalizeOp` phase, while the +2422 table is the type-level action table used during `LegalizeTypes`.

## NVPTX-Specific Pattern Categories

### Memory Operations: `sub_306D850` (77KB)

Selects PTX load/store instructions with the correct address space qualifier, vector width, and volatility. The function handles the full matrix of `{ld,st} x {.global,.shared,.local,.param,.const,.gen} x {.b8,.b16,.b32,.b64,.b128} x {.v1,.v2,.v4} x {.volatile,.relaxed,.acquire,.release}` instruction variants. Address space is determined by querying the pointer operand's address space attribute through the DAG.

The memory pattern matching also covers:
- **Vector loads/stores** -- `ld.global.v2.b32`, `ld.global.v4.b32`, and their 64-bit variants, selected based on the vector element count (1, 2, or 4).
- **Parameter loads** -- `ld.param.b32` and `st.param.b32` for call ABI (see [SelectionDAG: .param ABI](./selectiondag.md#the-param-space-calling-convention)).
- **Generic-space loads with addrspacecast** -- when the address space is generic (AS 0), the selector checks whether the source can be proven to be in a specific space and emits a non-generic load if so.

### Texture/Surface Instructions: `sub_306A930` (52KB)

Selects `tex`, `suld`, and `sust` instructions from DAG nodes produced by the intrinsic lowering mega-switch. The selector dispatches through helper functions:

| Helper | Purpose |
|---|---|
| `sub_2FE5F00` | Texture fetch type selection |
| `sub_2FE5F30` | Surface read type selection |
| `sub_2FE5F60` | Surface write type selection |
| `sub_2FE69A0` | Texture sampler mode selection |
| `sub_2FE6CC0` | Unified texture/surface dispatch |

Texture instructions have complex operand requirements: sampler reference, texture reference, coordinate type (1D/2D/3D/cube), data type (f32/i32/f16), and optional LOD/gradient parameters. The selector maps each combination to a specific PTX `tex.1d.v4.f32.f32` (or similar) opcode.

### Complex Addressing Modes: `sub_30811D0` (77KB)

Matches addressing patterns for load/store operands. NVPTX supports a limited set of addressing modes compared to x86:

- **Register + immediate offset** -- `[%r1 + 16]`, the most common PTX addressing mode.
- **Register** -- `[%r1]`, zero-offset variant.
- **Immediate** -- `[0x1000]`, absolute address (rare on GPU).
- **Register + register** -- not directly supported in PTX; decomposed into add + register addressing.

The complex pattern matcher at `sub_30811D0` calls seven helper functions (`sub_307B990` through `sub_307FEF0`) to decompose DAG address expressions into base-register + offset pairs. When the offset is a constant that fits in the PTX immediate field, it folds into the instruction encoding. When the offset is too large or non-constant, it generates a separate `add` instruction and uses register addressing.

### MMA / Tensor Core Instructions

Tensor core instruction selection is split across the intrinsic lowering stage (which generates NVPTXISD nodes from `wmma.load`, `wmma.mma`, `mma.sync`, `wgmma` intrinsics) and the ISel stage (which selects the specific PTX opcode). The ISel switch in `sub_347A8D0` handles these by checking:

1. **SM architecture** -- `wmma` requires SM 70+, `mma.sync` requires SM 75+, `wgmma` requires SM 90+.
2. **Matrix dimensions** -- m16n16k16, m8n8k4, m16n8k8, etc.
3. **Data types** -- f16, bf16, tf32, f64, i8, i4, b1, fp8 (SM 90+), fp4 (SM 100+).
4. **Accumulator type** -- f16 or f32 for half-precision MMA.

The architecture check consults the compressed legality table to determine whether a given MMA variant is legal on the target SM.

### Atomic Operations: `sub_3048C30` (86KB)

Atomic instruction selection generates `atom.{scope}.{op}.{type}` instructions. The selector handles:

| Operation | PTX | NVPTXISD opcodes |
|---|---|---|
| Compare-and-swap | `atom.cas` | 462 |
| Add (int) | `atom.add` | 294--297 |
| Min (signed) | `atom.min` | 302--305 |
| Max (signed) | `atom.max` | 314--317 |
| Exchange | `atom.exch` | (via generic path) |
| AND/OR/XOR | `atom.and` / `atom.or` / `atom.xor` | (via generic path) |

The selector checks `"vector atomics not supported on this architecture!"` for vector-width atomics and gates them behind an SM version check (likely SM 90+). Scope qualifiers (`.cta`, `.gpu`, `.sys`) are determined from the memory ordering of the LLVM atomic instruction.

### Vector / SIMD Patterns: `sub_3475BB0` (89KB)

Handles vector-type instruction selection for NVPTX's limited vector support (v2 and v4 packed types). The function calls `sub_969240` 121 times and is self-recursive 28 times. It selects between:

- **Packed register operations** -- `add.v2.f32`, `mul.v2.f32` when the SM supports native vector operations.
- **Scalarized fallback** -- decomposes vector operations into per-element scalar operations when the vector type is not natively supported.
- **mov.v2 / mov.v4** -- register-to-register vector moves for shuffles and extracts.

## Knobs

The ISel subsystem registers its knobs at `ctor_286` (`0x4FA0C0`, 5KB):

| Knob | Type | Description |
|---|---|---|
| `fast-isel-abort` | int | Abort mode for FastISel failures (0=silent, 1=warn, 2=abort) |
| `fast-isel-report-on-fallback` | bool | Report when FastISel falls back to SelectionDAG |
| `use-mbpi` | bool | Use Machine Branch Probability Info during ISel |
| `dag-disable-combine` | bool | Disable DAG combining entirely |
| `pre-RA-sched` | enum | Pre-RA scheduler variant: `"default"`, `"list-burr"`, `"source"`, `"list-hybrid"`, `"list-ilp"` |

Note that cicc does **not** use FastISel for GPU code generation. The `fast-isel-*` knobs exist because the upstream LLVM `SelectionDAGISel` framework registers them unconditionally, but the NVPTX backend always takes the full SelectionDAG path. The `dag-disable-combine` flag is the only ISel-phase knob that has a meaningful effect on NVPTX code generation; setting it skips the DAG combiner entirely, which produces worse code but can be useful for debugging.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM 20.0 | NVIDIA cicc v13.0 |
|---|---|---|
| **Iteration budget** | No explicit budget; relies on DAG invariants to terminate | Budget = `4 * numInstructions * maxBlockSize` |
| **Argument cost table** | Not present in `SelectionDAGISel` | Hash table with `key * 37` hash for argument byte sizes |
| **Legality table** | Simple `isLegal()` callback per target | Compressed 500-stride table + 4-bit packed secondary table |
| **FastISel** | Used for -O0 on most targets | Never used; always full SelectionDAG |
| **ISel function size** | Typical NVPTX `Select()` is ~50KB upstream | 309KB hand-written + 256KB TableGen = 565KB total |
| **Memory patterns** | Standard load/store | 5 address spaces, each with distinct PTX encoding |
| **Texture/surface** | Not present in upstream NVPTX (handled by intrinsics only) | 52KB dedicated sub-selector for tex/suld/sust |
| **Atomic patterns** | Standard expansion via AtomicExpandPass | 86KB custom selector with scope qualifiers and architecture gating |

## Function Map

| Address | Size | Function |
|---|---|---|
| `sub_3090F90` | 91KB | `NVPTXDAGToDAGISel::Select` -- ISel driver |
| `sub_308FEE0` | -- | Pattern matcher entry (dispatches to Select switch and SelectCode) |
| `sub_347A8D0` | 309KB | NVPTX hand-written Select switch |
| `sub_348D3E0` | 256KB | TableGen-generated SelectCode |
| `sub_3475BB0` | 89KB | Vector/SIMD pattern selection |
| `sub_306D850` | 77KB | Memory operation patterns (ld/st with address spaces) |
| `sub_30811D0` | 77KB | Complex addressing mode matching |
| `sub_30783B0` | 39KB | Addressing mode helper (base + offset extraction) |
| `sub_306A930` | 52KB | Texture/surface instruction selection |
| `sub_3048C30` | 86KB | Atomic operation selection |
| `sub_3447D70` | 32KB | Sub-selector for specific NVPTX patterns |
| `sub_3472970` | 36KB | Pattern matching helpers |
| `sub_343A2E0` | 49KB | Operand matching |
| `sub_376DE90` | -- | Compressed legality table lookup |
| `sub_308B6F0` | -- | Initialize topological worklist |
| `sub_3089BD0` | -- | Min-heap sift-down (priority queue) |
| `sub_308AB30` | -- | ISel cleanup |
| `sub_308B100` | -- | Hash table destruction |

## Cross-References

- [SelectionDAG & Instruction Selection](./selectiondag.md) -- parent page covering the full SelectionDAG pipeline (type legalization, operation legalization, DAG combining, and the ISel overview)
- [Pattern Database / Constraint Table](../structs/pattern-db.md) -- the per-instruction operand constraint table at `word_3F3E6C0`
- [DAG Node Layout](../structs/dag-node.md) -- SDNode structure definition
- [NVPTX Target Infrastructure](../infra/nvptx-target.md) -- target machine, subtarget features, and register classes
- [Hash Infrastructure](../infra/hash-infrastructure.md) -- the `key * 37` integer hash used throughout cicc
- [Tensor / MMA Builtins](../builtins/tensor-mma.md) -- intrinsic lowering for MMA operations that feed into ISel
- [Surface & Texture Builtins](../builtins/surface-texture.md) -- intrinsic lowering for texture/surface operations
- [Atomics Builtins](../builtins/atomics.md) -- intrinsic lowering for atomic operations
