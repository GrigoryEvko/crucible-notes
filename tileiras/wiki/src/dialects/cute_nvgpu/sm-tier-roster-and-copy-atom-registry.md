# SM Tier Roster and Copy Atom Registry

## Abstract

`cute_nvgpu` registers MMA, copy, prefetch, TMA, tensor-memory, and descriptor atom types per SM tier, then exposes them through common atom interfaces. The rest of the compiler asks uniform questions through those interfaces: what shape does this atom operate on, what element types are legal, where do operands live, what resources does the target need? The registry below is the product contract — every atom mnemonic, the interfaces it implements, the SM tier that registers it, and the residencies its operands accept.

## Registry Model

The dialect uses interface-driven atom records:

| Interface | Implemented by | Purpose |
|---|---|---|
| MMA atom | Universal FMA, SM80, SM89, SM90, SM100/SM103, SM120/SM121 MMA families | Reports MMA shape, operand element types, accumulator type, atom class, and verifier hooks. |
| Copy atom | TMEM load/store, S2T copy, universal copy, async copy, LDSM/STSM, TMA atoms | Reports copy shape, value type, memory spaces, vector width, and legality. |
| Prefetch atom | TMA load, store, reduce, and non-executing tiled TMA atoms | Reports descriptor, prefetch tile, and cache-hint behavior. |
| Descriptor type | SMEM descriptor views and TMA descriptors | Carries hardware descriptor state as typed IR. |

The design point that matters: generic `cute` code dispatches through interfaces, not through string comparisons on target names.

```c
LogicalResult verify_atom_instance(Atom atom, Target target, Shape use_shape) {
    if (MmaAtomInterface mma = dyn_cast_mma_atom(atom.type)) {
        return mma.verify_instance(atom, target, use_shape);
    }

    if (CopyAtomInterface copy = dyn_cast_copy_atom(atom.type)) {
        return copy.verify_instance(atom, target, use_shape);
    }

    if (PrefetchAtomInterface prefetch = dyn_cast_prefetch_atom(atom.type)) {
        return prefetch.verify_prefetch(atom, target, use_shape);
    }

    return failure("atom type does not implement a known cute_nvgpu interface");
}
```

## Atom Surface by Tier

| Tier | MMA atoms | Copy and descriptor atoms | Notes |
|---|---|---|---|
| All tiers | `atom.universal_fma` | `atom.universal_copy` | Generic fallback atom vocabulary. |
| SM75+ | No dedicated MMA mnemonic | `atom.ldsm` | Turing introduces `ldmatrix`-style shared-memory matrix loads. |
| SM80 | `sm80.mma`, `sm80.sparse_mma` | `atom.simt_async_copy`, `atom.ldsm` | Ampere dense and sparse `mma.sync`, plus `cp.async`-style copy atoms. |
| SM89 | `sm89.mma` | SM80 copy atoms | Ada extends the dense register-MMA surface with FP8 inputs. |
| SM90 | `sm90.mma`, `smem_desc_view` | `atom.tma_load`, `atom.tma_store`, `atom.tma_reduce`, `atom.stsm`, non-exec TMA atoms | Hopper WGMMA, SMEM descriptors, and TMA descriptor traffic. |
| SM100/SM103 | `sm100.mma`, `sm100.mma_sp`, `sm100.mma_bs`, `sm100.mma_bs_sp` | `atom.tmem_load`, `atom.tmem_store`, `atom.s2t_copy`, TMA atoms | Datacenter Blackwell UMMA, sparse UMMA, block-scaled MMA, sparse block-scaled MMA, and tensor memory. |
| SM110 (Jetson Thor) | — | inherited universal atoms | SM110 is registered as a target tier (`sm_110` / `sm_110a` / `sm_110f`) but has no dedicated MMA mnemonic; see note below. |
| SM120/SM121 | `SM120.mma_bs` | Register-based copy and scale-factor paths | Consumer Blackwell block-scaled MMA with uppercase `SM120` spelling. |

The uppercase spelling in `SM120.mma_bs` is part of the textual contract. A parser that lowercases it cannot round-trip IR for this dialect.

> ⚡ **QUIRK — SM110 (Jetson Thor) registers a target tier but exposes no dedicated MMA surface (HIGH)**
> The compiler's SM-target roster enumerates `sm_110`, `sm_110a`, and `sm_110f` alongside the other Blackwell tiers, and lowering will accept those targets as legal architecture flags. The dialect does NOT register a `sm110.mma` atom mnemonic — every MMA mnemonic in the registry is `sm80.mma`, `sm89.mma`, `sm90.mma`, `sm100.mma{,_sp,_bs,_bs_sp}`, or `SM120.mma_bs`. Kernels targeting SM110 fall through to the universal-FMA atom or to whichever earlier-tier MMA atom the architecture-conditional gate accepts. No WGMMA, no `tcgen05.mma`, no block-scaled register MMA is dialect-side dispatched for SM110 in this compiler. A reimplementation that expects SM110 to carry its own MMA / TMEM / WGMMA atom family will find none here, and a kernel that wants tensor-core throughput on Thor must either route through a non-MMA path or accept that the dispatcher will not synthesise a Thor-specific machine form.

## Atom TypeID Registry

The dialect registers one MLIR `TypeID` per atom kind. Generic `cute` code never sees the per-atom C++ class; it sees a typed value whose `TypeID` resolves to an interface vtable, and that vtable carries the verifier, the asm printer, the bytecode round-trip, and the per-atom legality predicates. The registry below lists the contract per atom: which interfaces it implements, which residencies its operands accept, and which SM tier first registers it.

| Atom mnemonic | Min tier | Interfaces implemented | Residency contract |
|---|---|---|---|
| `atom.universal_fma` | all | `MmaAtom` | A, B, D in `rmem` |
| `atom.universal_copy` | all | `CopyAtom` | any source-destination pair the target supports |
| `atom.ldsm` | SM75 | `CopyAtom` | `src=smem`, `dst=rmem`, shape ∈ LDSM matrix |
| `atom.stsm` | SM90 | `CopyAtom` | `src=rmem`, `dst=smem`, shape ∈ STSM matrix |
| `atom.simt_async_copy` | SM80 | `CopyAtom`, `AsyncCopyAtom` | `cp.async` `gmem → smem` |
| `sm80.mma` | SM80 | `MmaAtom` | A, B, D in `rmem`; one element type per operand |
| `sm80.sparse_mma` | SM80 | `MmaAtom`, `SparseMetadataAtom` | A, B, D in `rmem`; metadata operand alongside A |
| `sm89.mma` | SM89 | `MmaAtom` | A, B, D in `rmem`; FP8 element types added |
| `sm90.mma` (WGMMA) | SM90 | `MmaAtom`, `SmemDescriptorAtom` | A in `rmem` or `smem_desc_view`; B in `smem_desc_view`; D in `rmem` |
| `smem_desc_view` | SM90 | `DescriptorType` | typed view over an SMEM descriptor |
| `atom.tma_load` | SM90 | `CopyAtom`, `TmaAtom`, `PrefetchAtom`, `AsyncCopyAtom` | descriptor-driven `gmem → smem` |
| `atom.tma_store` | SM90 | `CopyAtom`, `TmaAtom`, `AsyncCopyAtom` | descriptor-driven `smem → gmem` |
| `atom.tma_reduce` | SM90 | `CopyAtom`, `TmaAtom`, `AsyncCopyAtom` | descriptor-driven reduce into `gmem` |
| `atom.non_exec_tiled_tma_*` | SM90 | `TmaAtom` (non-exec) | partition-verified TMA atom waiting on mbarrier and cache binding |
| `sm100.mma` (UMMA) | SM100 | `MmaAtom`, `TmemAtom` | A in `memref`/`smem_desc_view`; B in `smem_desc_view`; D in `memref` (tmem) |
| `sm100.mma_sp` | SM100 | `MmaAtom`, `TmemAtom`, `SparseMetadataAtom` | UMMA contract + structurally-sparse A and metadata operand |
| `sm100.mma_bs` | SM100 | `MmaAtom`, `TmemAtom`, `BlockScaleAtom` | UMMA contract + scale-factor operand |
| `sm100.mma_bs_sp` | SM100 | `MmaAtom`, `TmemAtom`, `BlockScaleAtom`, `SparseMetadataAtom` | UMMA block-scale + sparsity |
| `atom.tmem_load` | SM100 | `CopyAtom` | `src=tmem`, `dst=rmem` |
| `atom.tmem_store` | SM100 | `CopyAtom` | `src=rmem`, `dst=tmem` |
| `atom.s2t_copy` | SM100 | `CopyAtom`, `AsyncCopyAtom` | `src=smem`, `dst=tmem` |
| `SM120.mma_bs` | SM120 | `MmaAtom`, `BlockScaleAtom` | A, B, D in `rmem`; two scale-factor operands (one per A, B) |

The `Interfaces implemented` column is the dispatch contract. A pass that walks every atom and asks "do you support prefetch?" calls `dyn_cast<PrefetchAtomInterface>` on each atom value; the SM90+ TMA load atom is the only positive hit, and the call collapses to a `TypeID` compare. The `Residency contract` column lists the legality bounds the per-atom verifier enforces; it is the same checklist a CUTLASS C++ user reads from `Copy_Traits<>` and `MMA_Traits<>` headers.

## Copy Atom Operand-Layout Contracts

Every copy atom carries an operand-layout contract that the verifier checks before lowering. The contract pins source and destination residency, the per-thread fragment shape, the natural shape one atom invocation transfers, and the PTX (or NVVM intrinsic) instruction the lowering emits. The table below is the per-tier catalog; each row is one atom mnemonic.

### SM70 and SM75 register copy atoms

| Atom | Source | Destination | Natural shape | Element width | Per-thread fragment | Lowering target |
|---|---|---|---|---:|---|---|
| `atom.universal_copy` | any | any (target-supported) | one element | any | one value | scalar `ld`/`st` of matching width |
| `atom.ldsm<m8n8>` (SM75) | `smem` | `rmem` | 8 x 8 matrix tile | 16 bits | 2 elements per lane | `ldmatrix.sync.aligned.m8n8.x1.shared.b16` |
| `atom.ldsm<m8n8.x2>` (SM75) | `smem` | `rmem` | 8 x 16 matrix tile | 16 bits | 4 elements per lane | `ldmatrix.sync.aligned.m8n8.x2.shared.b16` |
| `atom.ldsm<m8n8.x4>` (SM75) | `smem` | `rmem` | 8 x 32 matrix tile | 16 bits | 8 elements per lane | `ldmatrix.sync.aligned.m8n8.x4.shared.b16` |
| `atom.ldsm<m8n8.x4.trans>` (SM75) | `smem` | `rmem` | 8 x 32 transposed tile | 16 bits | 8 elements per lane | `ldmatrix.sync.aligned.m8n8.x4.trans.shared.b16` |

The `x1`/`x2`/`x4` suffix is the number of 8x8 sub-tiles the atom fetches in one instruction. The transposed variants swap the per-lane fragment layout so that two register-resident MMA operands meet at the same memory cell after the matrix multiply; the verifier checks the transpose flag against the consuming MMA atom's expected operand layout.

### SM80 and SM86 async-copy and matrix-load atoms

| Atom | Source | Destination | Natural shape | Element width | Per-thread fragment | Lowering target |
|---|---|---|---|---:|---|---|
| `atom.simt_async_copy<4>` | `gmem` | `smem` | 4-byte element | 32 bits | 1 i32 per lane | `cp.async.ca.shared.global` (4 bytes) |
| `atom.simt_async_copy<8>` | `gmem` | `smem` | 8-byte element | 64 bits | 1 i64 per lane | `cp.async.ca.shared.global` (8 bytes) |
| `atom.simt_async_copy<16>` | `gmem` | `smem` | 16-byte element | 128 bits | 1 i128-equivalent per lane | `cp.async.cg.shared.global` (16 bytes; bypass L1) |
| `atom.ldsm<m8n8.*>` | `smem` | `rmem` | inherited from SM75 | 16 bits | inherited | `ldmatrix.sync.aligned.*` |

The 4/8/16 vector widths are the only legal `cp.async` granularities; the verifier rejects any other width with `"unsupported cp.async vector width"`. The 16-byte variant uses the `cg` cache-policy (bypass L1) because the L1 cache cannot satisfy a 128-bit single-instruction store; the 4- and 8-byte variants use `ca` (cache-all). Lowering chooses the cache policy from the atom's width alone — there is no per-op cache hint.

### SM90 TMA atom family

TMA atoms are descriptor-driven; the per-lane fragment layout is implicit in the descriptor word rather than in the atom's MLIR operand types.

| Atom | Source | Destination | Descriptor kind | Natural shape | Lowering target |
|---|---|---|---|---|---|
| `atom.tma_load` | `gmem` (descriptor) | `smem` | TMA tile descriptor | rank-1..rank-5 box | `cp.async.bulk.tensor.NDIM.shared::cluster.global` |
| `atom.tma_load_multicast` | `gmem` (descriptor) | `smem` (multi-CTA) | TMA tile descriptor + CTA mask | rank-1..rank-5 box | `cp.async.bulk.tensor.NDIM.shared::cluster.global.multicast::cluster` |
| `atom.tma_load_im2col` | `gmem` (descriptor) | `smem` | TMA im2col descriptor | rank-3..rank-5 spatial | `cp.async.bulk.tensor.NDIM.shared::cluster.global.im2col` |
| `atom.tma_store` | `smem` | `gmem` (descriptor) | TMA tile descriptor | rank-1..rank-5 box | `cp.async.bulk.tensor.NDIM.global.shared` |
| `atom.tma_reduce<op>` | `smem` | `gmem` (descriptor) | TMA tile + reduce kind | rank-1..rank-5 box | `cp.reduce.async.bulk.tensor.NDIM.global.shared.OP` |
| `atom.stsm<m8n8.*>` | `rmem` | `smem` | none (register copy) | 8 x 8 matrix tile per sub-tile | `stmatrix.sync.aligned.m8n8.x[1,2,4].shared.b16` |

The TMA atoms accept rank-1 through rank-5 boxes; the descriptor word encodes the per-dimension extents, strides, and box edges (see the dedicated TMA atom page). The multicast variant adds a 16-bit CTA mask that names which CTAs in the cluster receive the loaded data, enabling one-to-many fanout from a single GMEM read. The im2col variant rewrites the descriptor's box coordinates through a convolution-style spatial reshape so a single load presents the data already in NCHW-to-window form for convolution kernels.

`atom.stsm` mirrors `atom.ldsm` from SM75 in reverse — `rmem -> smem` rather than `smem -> rmem` — and shares the same sub-tile multiplicity convention.

### SM100 and SM103 TMEM copy atoms

Datacenter Blackwell adds tensor memory as a fourth memory class alongside register, shared, and global. The copy atom family covers every legal direction between TMEM and the other three classes.

| Atom | Source | Destination | Natural shape | Element width | Lowering target |
|---|---|---|---|---:|---|
| `atom.tmem_load` | `tmem` | `rmem` | one TMEM column tile per atom | 32/16/8 bits | `tcgen05.ld.sync.aligned.shape.b32` |
| `atom.tmem_store` | `rmem` | `tmem` | one TMEM column tile per atom | 32/16/8 bits | `tcgen05.st.sync.aligned.shape.b32` |
| `atom.s2t_copy` | `smem` | `tmem` | TMA-box-shaped SMEM slice | 8/16/32/64 bits | `tcgen05.cp.shared::cta.async` |
| `atom.tmem_to_smem_copy` | `tmem` | `smem` | TMEM column tile | 32/16/8 bits | `tcgen05.cp.async.shared::cta` (reverse direction) |
| `atom.tcgen05.cp` | `tmem` | `tmem` (cross-CTA) | column tile inside one cluster | 32 bits | `tcgen05.cp.async` (cluster-scope) |

TMEM is column-organised: an atom transfers one or more TMEM columns at a time. The verifier checks that the operand layout addresses TMEM columns in a contiguous range matching the natural shape, and that the column count matches the destination tile. SM100 splits the `tcgen05` family into `tcgen05.ld` / `tcgen05.st` (register-mediated, synchronous-looking) and `tcgen05.cp` (cluster-scope async); the atom mnemonics make that distinction explicit.

A TMEM-resident MMA accumulator does not move out of TMEM until a `atom.tmem_load` retires its column range into registers. Lowering must keep that retire op alive across any consumer that reads the accumulator from registers — eliding it produces undefined values.

## MMA Records

MMA records carry:

- architecture tier;
- operand A, B, and accumulator element types;
- tile shape, usually expressed as `(M, N, K)`;
- operand residency, such as register memory, shared-memory descriptor, or
  tensor memory;
- sparse or block-scaled metadata, when present;
- verifier and lowering hooks.

```c
typedef struct {
    SmTier min_tier;
    Shape mnk;
    ElementType a_type;
    ElementType b_type;
    ElementType c_type;
    Residency a_residency;
    Residency b_residency;
    Residency d_residency;
    bool supports_sparse;
    bool supports_block_scale;
} MmaAtomContract;
```

## Copy Records

Copy atoms carry copy width, source and destination residency, optional async behaviour, and any descriptor or prefetch behaviour. TMA atoms add a descriptor flavour and a prefetch interface on top.

```c
typedef struct {
    SmTier min_tier;
    Residency source;
    Residency destination;
    int value_bits;
    bool is_async;
    bool uses_tma_descriptor;
    bool supports_prefetch;
} CopyAtomContract;
```

## Per-Tier Semantics

### SM70 and SM75

Volta and Turing mostly use generic atoms. SM75 introduces the shared-memory matrix-load family, where `ldsm` becomes tier-gated. Older MMA forms route through universal or backend intrinsic paths — there is no dedicated `cute_nvgpu.sm70.mma` spelling.

### SM80

Ampere is the first full register-MMA tier. `sm80.mma` covers dense `mma.sync` forms; `sm80.sparse_mma` covers the structured-sparse forms with metadata operands. `simt_async_copy` models Ampere asynchronous copies. The verifier's anchors here are register-resident MMA operands, supported integer and floating input types, valid sparse metadata, and legal copy vector widths.

### SM89

Ada keeps the SM80 register-resident model but adds FP8 input combinations.
Sparse FP8 is not part of this tier's atom surface.

### SM90

Hopper introduces WGMMA and TMA. `sm90.mma` accepts shared-memory descriptor operands; B is always descriptor-backed, A is either register- or descriptor-backed depending on mode. TMA load/store/reduce atoms are descriptor-driven and often start as non-executing tiled atoms, then bind to mbarrier and cache state to form executable atoms.

### SM100 and SM103

Datacenter Blackwell introduces UMMA and tensor memory. `sm100.mma` is the plain tensor-memory MMA family; `sm100.mma_bs` and `sm100.mma_bs_sp` carry block-scale and sparse block-scale metadata. TMEM load/store and shared-to-tmem copy atoms move values between register, shared, global, and tensor-memory domains. SM103 reuses the same dialect surface — the distinction is a target flag, not a new atom family.

### SM120 and SM121

Consumer Blackwell block-scaled MMA has no TMEM dependency. It carries two scale-factor operands — one for A, one for B — and keeps the accumulator in register memory. SM121 shares the same surface.

## MMA Atom Verifier Diagnostics

Every MMA atom registers one verifier through the dialect. The verifier emits verbatim diagnostics so test suites can match by string. The strings below are the user-visible contract.

### Layout-shape verifier (all UMMA / SM90 / SM100 variants)

- `"expects Mma atom layout of {0}"` — strict equality between the op's declared per-operand layout and the canonical layout the atom's traits table reconstructs. The `{0}` slot prints the canonical reference layout.
- `"expects static and no scaled basis layout for {0}"` — the stride basis must be static integer; scaled-basis layouts are rejected because the descriptor packer cannot encode them.

### Element-type ladder (UMMA family)

- `"expects operand a with element type {0}, but get {1}."`
- `"expects operand b with element type {0}, but get {1}."`
- `"expects operand c with element type {0}, but get {1}."` (the verifier uses `c` for both C and D operand slots)

The element-type check happens before the residency check. The `{0}` slot prints the expected element type from the atom's traits; `{1}` prints the actual operand element type.

### Residency ladder (UMMA family)

- `"expects memref/smem_desc_view for operand A, but gets A:{0}."`
- `"expects smem_desc_view for operand B, but gets B: {0}."`
- `"expects memref for operand D, but gets D: {0}."`

UMMA B is always SMEM-descriptor; A is either an RMEM `memref` or an SMEM-descriptor (note: per `verify_sm100_umma` an RMEM A is rejected — A must be either an SMEM descriptor or a TMEM `memref` — see [MMA Atoms SM70-120 — SM100 and SM103 UMMA](mma-atoms-sm70-120.md#sm100-and-sm103-umma)); D is a `memref` in the tensor-memory address space (`result.residency == TENSOR_MEMORY`) — the verifier diagnostic spells the SSA type "memref", but the residency contract pins the accumulator to TMEM, not register memory. The verifier emits the first mismatched operand and stops.

### Layout composability (UMMA family)

- `"invalid layout of A/B/D. A: {0}, B: {1}, D:{2}"` — emitted when one of the three per-operand canonical-layout checks fails before the composability step runs.
- `"layoutA, layoutB and layoutD fail to form a gemm. A: {0}, B: {1}, D: {2}"` — emitted when the three layouts pass individually but their composition does not encode a valid `(M, N, K)` triple.

### Non-UMMA shared verifier (SM70 / 75 / 80 / 89)

- `"expects all mma operands to have element type"`
- `"expects rmem for input operands, but got A: {0}, B: {1}, D: {2}"`
- `"expects operand a with element type "` (followed by expected and actual types)

The non-UMMA path enforces the simpler rule that A, B, and D all share one element type and all live in register memory. This is the only path SM70-89 use.

### Composed-layout rejection (tiled-copy / tiled-mma builders)

- `"doesn't support composed layout for "`
- `"A/B/D, but got: A: {0}, B: {1}, D:{2}"`
- `"expects A, B to have the same rank, but got A: "`
- `"expects C, D to have the same rank, but got C: "`
- `"expects C to have rank 1 or 2, but got C: "`
- `"expects C to have rank 2 or 3, but got C: "`
- `"expects C to have rank 3, but got C: "`

## Registry Invariants

- Atom names encode the minimum architecture tier or intentionally remain
  tier-generic.
- Generic tiling code dispatches through interfaces, not mnemonic switches.
- Sparse and block-scaled atoms expose their metadata through typed operands or
  attributes.
- TMA atoms that prefetch descriptors implement the prefetch interface.
- Descriptor view types remain explicit until the backend has emitted the
  corresponding WGMMA, TMA, or TCGEN instruction sequence.
- Target verification rejects atoms whose tier exceeds the selected target.

## Cross-References

[Mode Pattern Verifiers — LDSM and STSM Matrix](mode-pattern-verifiers.md#ldsm-and-stsm-matrix) documents the LDSM/STSM, [UMMA Canonical Layout Verifier](mode-pattern-verifiers.md#umma-canonical-layout-verifier), [tcgen05.mma Kind-Word Verifier](mode-pattern-verifiers.md#tcgen05mma-kind-word-verifier), and [SM120 Block-Scaled Lattice](mode-pattern-verifiers.md#sm120-block-scaled-lattice) verifiers each atom registers. [TMA Atoms — Atom Family](tma-atoms.md#atom-family) covers the descriptor-driven TMA family in depth. [MMA Atoms SM70-120 — Per-Arch MMA Shape Lattice](mma-atoms-sm70-120.md#per-arch-mma-shape-lattice) covers the per-tier MMA shape lattice. [MMA Atoms SM70-SM120 — Operand Contract by Tier](mma-atoms-sm70-120.md#operand-contract-by-tier) cross-references the consumer side of every copy atom in the table above. [Layout Algebra and Descriptor Grammar — Swizzle Operator](../cute/layout-algebra-and-descriptor-grammar.md#swizzle-operator) covers the bit-manipulation formula the SMEM-resident atoms (`atom.ldsm`, `atom.stsm`, TMA descriptors, `atom.s2t_copy`) rely on for bank-conflict-free placement.

