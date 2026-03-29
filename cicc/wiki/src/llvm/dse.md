# DSE (Dead Store Elimination)

CICC v13.0 contains a heavily modified Dead Store Elimination pass totaling approximately 91 KB of decompiled code across three major functions: the core `DSE::runOnFunction` at `sub_19DA750` (33 KB), the overwrite detection engine at `sub_19DDCB0` (28 KB), and the partial overwrite tracking system at `sub_19DF5F0` (30 KB). This substantially exceeds the size of upstream LLVM DSE, primarily due to NVIDIA's additions for partial store forwarding with type conversion, cross-store dependency tracking, store-chain decomposition for aggregates, and native CUDA vector type awareness.

## Analysis Dependencies

DSE requires five analysis passes, resolved through the pass manager at registration time (`sub_19DD1D0`):

| Analysis | Global Address | Pass ID |
|----------|---------------|---------|
| MemorySSA | `unk_4F9E06C` | Memory SSA graph |
| DominatorTree | `unk_4F9A488` | Dominator tree |
| MemoryDependence | `unk_4F9B6E8` | Memory dependence queries |
| PostDominatorTree | `unk_4F9D764` | Post-dominator tree |
| AliasAnalysis | `unk_4F9D3C0` | NVVM-aware alias analysis |

## Core Algorithm

The main entry point `DSE::runOnFunction` (`sub_19DA750`) processes a function by iterating over store instructions and checking whether each store is dead (fully or partially overwritten by a later store to the same location before any intervening load).

### Early Exit and Setup

The pass begins with an early exit check via `sub_1636880()` to determine whether the function should be skipped entirely. It then retrieves `MemoryDependence` and `AliasAnalysis` from the pass manager and calls `sub_14A4050` / `sub_14A2F00` to verify the function contains stores worth analyzing. If no stores are present, the pass returns immediately.

### Store Instruction Identification

Store instructions are identified by checking byte +16 of the instruction structure for value 77. The operand count is read from offset +20 (masked with `0xFFFFFFF`), and the "has-operand-list-pointer" flag at byte +23, bit `0x40`, indicates indirect operand storage for instructions with many operands.

### Type Size Computation

DSE computes store sizes through a type-walker switch on byte +8 of the type structure. This logic is shared between the core pass and the overwrite detector:

| Type Code | Size | Notes |
|-----------|------|-------|
| 1 | 16 bits | Half-precision float |
| 2 | 32 bits | Float / int32 |
| 3, 9 | 64 bits | Double / int64 |
| 4 | 80 bits | x86 long double / PTX f80 |
| 5, 6 | 128 bits | Quad precision / int128 |
| 7 | pointer-sized | Resolved via `sub_15A9520` |
| 0xB | immediate | Size from upper bits of type word |
| 0xD | struct | Layout computed by `sub_15A9930` |
| 0xE | vector | `element_size * num_elements` with alignment |
| 0xF | integer | Arbitrary-width integer |
| 0x10 | array | Recurses into element type, multiplies by count |
| 0, 8, A, C | array-like | Follows pointer chain |

The vector type formula (case 0xE) accounts for element alignment: `8 * num_elements * element_alignment * ceil(element_alignment + ceil(element_bits/8) - 1) / element_alignment)`. This handles CUDA native vector types (`float2`, `float4`, `int4`).

## Overwrite Detection

The overwrite analysis engine at `sub_19DDCB0` (28 KB) determines whether one store completely or partially covers another. It receives the instruction, an operand index, alias analysis results, and address-space information.

### Alias Queries

The function calls `sub_14C2730` to perform alias queries with full parameters: `(target_ptr, data_layout, 0, instruction, store_address, alias_analysis)`. This returns whether two memory locations may alias. The alias analysis already incorporates CUDA address-space separation (shared=3, global=1, local=5, constant=4), so DSE itself does not need explicit address-space checks.

### Partial Store Forwarding

When store sizes do not match, NVIDIA's DSE creates truncation or extension casts to extract the relevant portion. This is a critical GPU-specific extension:

- If the source is smaller than the destination: creates an extension (opcode 36 = zext).
- If the source is larger than the destination: creates a truncation (opcode 38 = trunc).
- Alignment requirements are verified through `sub_16431D0`.
- Complex types use `sub_15FDBD0` for cast creation; simple types use `sub_15A46C0`.

Standard LLVM DSE bails on size mismatches. NVIDIA's version handles the common CUDA pattern of a `float4` store followed by a scalar `float` load by extracting the relevant component via GEP + load.

### Store Size Ratio Check

At labels `LABEL_25` / `LABEL_29` in the core function, DSE performs a ratio check:

1. Computes `v159` = aligned size of destination type.
2. Computes `v48` = aligned size of source type.
3. Calculates `v148 = v48 / v159` (how many destination elements fit in source).
4. If `v48 % v159 != 0`, bails (partial overlap that cannot be forwarded).
5. If sizes differ, creates a GEP + load to extract the relevant portion.

### Metadata Preservation

After creating a replacement instruction, the pass preserves metadata:

- Debug location via `sub_157E9D0`.
- Use-chain linkage by updating prev/next pointers at offsets +24/+32.
- Basic block insertion via `sub_164B780`.
- TBAA metadata propagation through `sub_1623A60` / `sub_1623210`.
- `nonnull` attribute copying via `sub_15FA300` / `sub_15FA2E0`.
- Use replacement via `sub_164B7C0`.

## Partial Overwrite Tracking

The function-level partial overwrite pass at `sub_19DF5F0` (30 KB) maintains a hash table of all stores in a function and tracks which stores partially overwrite each other.

### Hash Table Structure

Each hash table entry is 72 bytes:

| Offset | Content |
|--------|---------|
| +0 | Key (store instruction pointer; -8 = empty, -16 = tombstone) |
| +8 | Operand list pointer |
| +16 | Operand count |
| +24 | Inline storage (when count <= small threshold) |
| +48 | Additional metadata |

The hash function is `(value >> 9) ^ (value >> 4)`, using linear probing with tombstone handling. Growth triggers when `4*(used+1) >= 3*capacity` or when too many tombstones accumulate (`capacity - tombstones - used <= capacity/8`). Minimum table size is 64 entries with power-of-2 sizing.

### Cross-Store Dependency Records

When a new store aliases an existing entry, DSE records both stores in a 6-element record: `{store1, store2, operand1, operand2, ptr1, ptr2}`. This enables tracking stores that partially overwrite each other even when the overwritten value has been modified between stores. Reference counting is managed through `sub_1649AC0` / `sub_1649B30`, and per-entry operand lists grow via `sub_170B450`.

### Store-Chain Decomposition

In the `LABEL_47` region of the core function, DSE walks store chains through struct/array GEPs and decomposes aggregate stores into element-level dead store checks. `sub_19D94E0` handles chain-level elimination, while `sub_19D91E0` builds the comparison set for overlap detection.

## Address-Space Handling

DSE does not contain explicit CUDA address-space comparisons. Address-space separation is handled entirely by the underlying NVVM alias analysis (`unk_4F9D3C0`), which knows that different address spaces cannot alias. The alias query function `sub_14C2730` receives the full instruction context including address space, so query results already incorporate this constraint.

## Store Forwarding to Loads

The function `sub_19DBD20` (20 KB) attempts store-to-load forwarding. When `sub_19DD7C0` finds a store feeding into a load, it constructs a replacement using `sub_12815B0`. Sign/zero extension matching uses type byte 15 (float types) and type byte 11 (integer types), with opcodes 45 (float-to-int truncation), 46 (int-to-float), and 47 (generic cast).

## Related Passes

Two related passes are registered alongside DSE in the same code region:

- **MergedLoadStoreMotion** (`sub_19DCD20`, pass name `mldst-motion`): Shares the same alias infrastructure and is registered with the same analysis dependencies.
- **NaryReassociate** (`sub_19DD420` / `sub_19DD530`): N-ary reassociation pass factory, registered at `sub_19DD1D0` with its own analysis set.

## Key Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| `DSE::runOnFunction` | `0x19DA750` | 33 KB | Main dead store elimination |
| `DSE::analyzeOverwrite` | `0x19DDCB0` | 28 KB | Complete/partial overwrite detection |
| `DSE::runPartialOverwritePass` | `0x19DF5F0` | 30 KB | Function-level partial tracking |
| `DSE::tryForwardStoresToLoad` | `0x19DBD20` | 20 KB | Store-to-load forwarding |
| `DSE::buildOverwriteRecord` | `0x19D8AF0` | -- | Overlap record construction |
| `DSE::buildComparisonSet` | `0x19D91E0` | -- | Set of stores to compare |
| `DSE::eliminateStoreChain` | `0x19D94E0` | -- | Chain-level elimination |
| `DSE::scanLoopForDeadStores` | `0x19DCB70` | -- | Loop-level DSE |
| `DSE::runOnBasicBlock` | `0x19DCC90` | -- | Block-level entry point |
| `DSE::extractStoreOperands` | `0x19DD690` | -- | Get base pointer and stored value |
| `DSE::lookupDeadStoreCandidate` | `0x19DD7C0` | -- | Hash table lookup |
| `DSE::decomposeGEPStore` | `0x19DD950` | -- | GEP-based store decomposition |
| `DSE::collectPartialOperands` | `0x19DEFC0` | -- | Partial overwrite operand collection |
| `DSE::checkPartialOverwrite` | `0x19DEE70` | -- | Individual partial overwrite check |
| `DSE::tryEliminateStore` | `0x19DF200` | -- | Attempt store elimination |
| `DSE::rehashStoreTable` | `0x19DF220` | -- | Hash table resize |

## Differences from Upstream LLVM

1. **Partial store forwarding with type conversion.** Standard LLVM DSE bails when store and load sizes differ. NVIDIA's version creates GEP + load sequences to extract relevant portions, handling `float4` -> `float` patterns.
2. **72-byte hash table entries with cross-store tracking.** Upstream uses simpler data structures. NVIDIA tracks which stores partially overwrite each other through 6-element dependency records.
3. **Store-chain decomposition.** Aggregate stores are decomposed through struct/array GEPs into element-level checks, enabling elimination of stores that are collectively dead.
4. **Vector type awareness.** The type walker includes a dedicated case for CUDA vector types with proper alignment computation.
5. **Total code size.** At ~91 KB across three functions, NVIDIA's DSE is roughly 3x the size of upstream LLVM's equivalent.
