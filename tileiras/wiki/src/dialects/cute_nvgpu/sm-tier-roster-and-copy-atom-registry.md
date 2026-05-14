# SM Tier Roster and Copy Atom Registry

## Abstract

`cute_nvgpu` registers MMA, copy, prefetch, TMA, tensor-memory, and descriptor atom types per SM tier, then exposes them through common atom interfaces. The rest of the compiler asks uniform questions through those interfaces: what shape does this atom operate on, what element types are legal, where do operands live, what resources does the target need? This page describes the registry as a product contract rather than the binary registration table.

## Registry Model

The dialect uses interface-driven atom records:

| Interface | Implemented by | Purpose |
|---|---|---|
| MMA atom | Universal FMA, SM80, SM89, SM90, SM100, SM120 MMA families | Reports MMA shape, operand element types, accumulator type, atom class, and verifier hooks. |
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
| SM100/SM103 | `sm100.mma`, `sm100.mma_bs`, `sm100.mma_bs_sp` | `atom.tmem_load`, `atom.tmem_store`, `atom.s2t_copy`, TMA atoms | Datacenter Blackwell UMMA, block-scaled MMA, sparse block-scaled MMA, and tensor memory. |
| SM120/SM121 | `SM120.mma_bs` | Register-based copy and scale-factor paths | Consumer Blackwell block-scaled MMA with uppercase `SM120` spelling. |

The uppercase spelling in `SM120.mma_bs` is part of the textual contract. A parser that lowercases it cannot round-trip IR for this dialect.

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

