# DSE (Dead Store Elimination)

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/Transforms/Scalar/DeadStoreElimination.cpp` (LLVM 20.0.0)

CICC v13.0 contains a heavily modified Dead Store Elimination pass totaling roughly 14 KB of binary text (~91 KB of decompiled source) across three major functions: the core `DSE::runOnFunction` at `sub_19DA750` (5.2 KB binary), the overwrite detection engine at `sub_19DDCB0` (4.4 KB binary), and the partial overwrite tracking system at `sub_19DF5F0` (4.8 KB binary). This substantially exceeds the size of upstream LLVM DSE, primarily due to NVIDIA's additions for partial store forwarding with type conversion, cross-store dependency tracking, store-chain decomposition for aggregates, and native CUDA vector type awareness.

## IR Before/After Example

DSE removes stores that are overwritten before any load reads them. The NVIDIA extension handles partial overwrites common in CUDA vector code.

**Before** (dead store followed by overwrite):
```llvm
define void @f(ptr addrspace(1) %p, float %x, float %y) {
  store float %x, ptr addrspace(1) %p, align 4          ; dead: overwritten below before any load
  %other = fadd float %x, %y
  store float %other, ptr addrspace(1) %p, align 4      ; overwrites the first store completely
  ret void
}
```

**After:**
```llvm
define void @f(ptr addrspace(1) %p, float %x, float %y) {
  ; first store removed -- overwritten by second store, no intervening load
  %other = fadd float %x, %y
  store float %other, ptr addrspace(1) %p, align 4
  ret void
}
```

NVIDIA's DSE also handles partial overwrite patterns with CUDA vector types. When a `float4` store partially overwrites a previous `float4` store, the pass decomposes via GEP to determine which elements are dead. This is a key GPU extension that upstream LLVM DSE does not handle.

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

The overwrite analysis engine at `sub_19DDCB0` (4.4 KB binary) determines whether one store completely or partially covers another. It receives the instruction, an operand index, alias analysis results, and address-space information.

### Alias Queries

The function calls `sub_14C2730` to perform alias queries with full parameters: `(target_ptr, data_layout, 0, instruction, store_address, alias_analysis)`. This returns whether two memory locations may alias. The alias analysis already incorporates CUDA address-space separation (shared=3, global=1, local=5, constant=4), so DSE itself does not need explicit address-space checks.

### Partial Store Forwarding

When store sizes do not match, NVIDIA's DSE creates truncation or extension casts to extract the relevant portion. This is a critical GPU-specific extension:

- If the source is smaller than the destination: creates an extension (opcode 36 = zext).
- If the source is larger than the destination: creates a truncation (opcode 38 = trunc).
- Alignment requirements are verified through `sub_16431D0`.
- Complex types use `sub_15FDBD0` for cast creation; simple types use `sub_15A46C0`.

Standard LLVM DSE bails on size mismatches. NVIDIA's version handles the common CUDA pattern of a `float4` store followed by a scalar `float` load by extracting the relevant component via GEP + load.

```c
/* Partial store forwarding inside sub_19DDCB0 (overwrite engine, 4.4 KB binary).
 * src_store = earlier store whose value we want to forward
 * dst_load  = later load whose location overlaps with src_store
 * Returns the value to feed into RAUW, or NULL on bail. */
Value *forwardPartialStore(StoreInst *src_store, LoadInst *dst_load,
                           const DataLayout *DL, AAResults *AA) {
    /* Address-space disjointness already filtered by NVVM AA via
     * sub_14C2730 -- shared/global/local pairs never reach this point. */
    Value *src_val = src_store->getValueOperand();
    Type  *src_ty  = src_val->getType();
    Type  *dst_ty  = dst_load->getType();

    uint64_t src_bits = computeTypeBits(src_ty, DL);    /* type-tag walk      */
    uint64_t dst_bits = computeTypeBits(dst_ty, DL);    /* opcodes 1..0x10    */

    if (src_bits == dst_bits)                           /* full overwrite     */
        return src_val;                                 /* trivial forward    */

    /* Ratio check (decompile labels LABEL_25 / LABEL_29). */
    if (src_bits == 0 || dst_bits == 0)        return NULL;
    if (src_bits % dst_bits != 0)              return NULL; /* misaligned    */

    /* Compute byte offset of dst inside src. Bails if dst is not naturally
     * contained -- e.g., partially-overlapping scalar inside float4. */
    uint64_t off_bits = pointerDeltaBits(src_store->getPointerOperand(),
                                         dst_load ->getPointerOperand(), DL);
    if (off_bits == UINT64_MAX || off_bits + dst_bits > src_bits) return NULL;

    /* Build trunc/zext or GEP+extractelement to slice src_val.
     * Simple scalar slice: trunc (opcode 38) or zext (opcode 36). */
    if (isScalarType(src_ty) && isScalarType(dst_ty)) {
        Value *shifted = (off_bits != 0)
            ? createLShr(src_val, off_bits)             /* sub_15A46C0       */
            : src_val;
        return (src_bits > dst_bits)
            ? createTrunc(shifted, dst_ty)              /* opcode 38         */
            : createZExt (shifted, dst_ty);             /* opcode 36         */
    }
    /* Vector/struct slice goes through sub_15FDBD0. */
    return createGepExtract(src_val, off_bits / dst_bits, dst_ty);
}
```

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

The function-level partial overwrite pass at `sub_19DF5F0` (4.8 KB binary) maintains a hash table of all stores in a function and tracks which stores partially overwrite each other.

### Hash Table Structure

Each hash table entry is 72 bytes:

| Offset | Content |
|--------|---------|
| +0 | Key (store instruction pointer; -8 = empty, -16 = tombstone) |
| +8 | Operand list pointer |
| +16 | Operand count |
| +24 | Inline storage (when count <= small threshold) |
| +48 | Additional metadata |

The hash function, probing strategy, and growth/compaction thresholds follow the standard DenseMap infrastructure; see [Hash Table and Collection Infrastructure](../infra/hash-infrastructure.md). This instance uses NVVM-layer sentinels (-8 / -16) and a minimum table size of 64 entries.

### Cross-Store Dependency Records

When a new store aliases an existing entry, DSE records both stores in a 6-element record: `{store1, store2, operand1, operand2, ptr1, ptr2}`. This enables tracking stores that partially overwrite each other even when the overwritten value has been modified between stores. Reference counting is managed through `sub_1649AC0` / `sub_1649B30`, and per-entry operand lists grow via `sub_170B450`.

### Store-Chain Decomposition

In the `LABEL_47` region of the core function, DSE walks store chains through struct/array GEPs and decomposes aggregate stores into element-level dead store checks. `sub_19D94E0` handles chain-level elimination, while `sub_19D91E0` builds the comparison set for overlap detection.

## Address-Space Handling

DSE does not contain explicit CUDA address-space comparisons. Address-space separation is handled entirely by the underlying NVVM alias analysis (`unk_4F9D3C0`), which knows that different address spaces cannot alias. The alias query function `sub_14C2730` receives the full instruction context including address space, so query results already incorporate this constraint.

## Store Forwarding to Loads

The function `sub_19DBD20` (3.6 KB binary) attempts store-to-load forwarding. When `sub_19DD7C0` finds a store feeding into a load, it constructs a replacement using `sub_12815B0`. Sign/zero extension matching uses type byte 15 (float types) and type byte 11 (integer types), with opcodes 45 (float-to-int truncation), 46 (int-to-float), and 47 (generic cast).

## Related Passes

Two related passes are registered alongside DSE in the same code region:

- **MergedLoadStoreMotion** (`sub_19DCD20`, pass name `mldst-motion`): Shares the same alias infrastructure and is registered with the same analysis dependencies.
- **NaryReassociate** (`sub_19DD420` / `sub_19DD530`): N-ary reassociation pass factory, registered at `sub_19DD1D0` with its own analysis set.

## Key Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| DSE main entry | `sub_19DA750` | 5.2 KB | Main dead store elimination driver |
| Overwrite detection engine | `sub_19DDCB0` | 4.4 KB | Complete/partial overwrite classification |
| Function-level partial-overwrite pass | `sub_19DF5F0` | 4.8 KB | Tracks partial overwrites across the function |
| Store-to-load forwarding | `sub_19DBD20` | 3.6 KB | Forwards stored values into dominated loads |
| Overlap record construction | `sub_19D8AF0` | -- | Builds 72-byte overlap record entries |
| Compare-set assembly | `sub_19D91E0` | -- | Gathers store-set to compare against current store |
| Chain-level elimination | `sub_19D94E0` | -- | Drops a chain of dead stores in one pass |
| Loop-level DSE worker | `sub_19DCB70` | -- | Handles dead stores inside individual loops |
| Block-level entry | `sub_19DCC90` | -- | Per-basic-block DSE driver |
| Store-operand extraction | `sub_19DD690` | -- | Reads base pointer + stored value from a store inst |
| Dead-store candidate lookup | `sub_19DD7C0` | -- | Hash-table probe for known overlapping stores |
| GEP-based store decomposition | `sub_19DD950` | -- | Breaks aggregate stores into per-element pieces |
| Partial-overwrite operand collection | `sub_19DEFC0` | -- | Gathers operands feeding partial overwrites |
| Per-pair partial-overwrite check | `sub_19DEE70` | -- | Checks one partial overlap relationship |
| Single-store elimination attempt | `sub_19DF200` | -- | Tries to delete one identified dead store |
| Store-table rehash | `sub_19DF220` | -- | Grows the partial-overwrite tracking table |

## Differences from Upstream LLVM

1. **Partial store forwarding with type conversion.** Standard LLVM DSE bails when store and load sizes differ. NVIDIA's version creates GEP + load sequences to extract relevant portions, handling `float4` -> `float` patterns.
2. **72-byte hash table entries with cross-store tracking.** Upstream uses simpler data structures. NVIDIA tracks which stores partially overwrite each other through 6-element dependency records.
3. **Store-chain decomposition.** Aggregate stores are decomposed through struct/array GEPs into element-level checks, enabling elimination of stores that are collectively dead.
4. **Vector type awareness.** The type walker includes a dedicated case for CUDA vector types with proper alignment computation.
5. **Total code size.** At ~14 KB of binary text across three functions (or ~91 KB of decompiled source), NVIDIA's DSE is roughly 3x the size of upstream LLVM's equivalent.
