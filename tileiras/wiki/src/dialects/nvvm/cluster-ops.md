# NVVM Cluster Ops

## Abstract

`nvvm.cluster.*` and the adjacent cluster-aware helpers cover Hopper's thread-block-cluster surface: a small group of CTAs running on neighbouring SMs that share a logical cluster-wide barrier and a `mapa`-addressable view of their peer CTAs' shared memory. The ops in this family handle cluster-wide arrival, wait, and rank queries; they pair with mbarrier ops in [`nvvm.mbarrier.*`](mbarrier-ops.md) for the data-side handshake. See [Cluster Sync and DSMEM Handshake](../../topics/cluster-sync-and-dsmem-handshake.md) for the cross-CTA protocol and [Cluster Sync Emission](../../codegen/tcgen05-wgmma-mbarrier-cluster.md#cluster-sync-emission) for the codegen side.

Blackwell (sm_100+) keeps the cluster surface; the same op set is the access path on every sm_90+ target.

## Op Roster

| Op | Role |
|---|---|
| `nvvm.cluster.arrive` | arrive at the cluster-wide barrier (acquire-release semantics) |
| `nvvm.cluster.arrive.relaxed` | relaxed-memory variant of `cluster.arrive` |
| `nvvm.cluster.wait` | wait for every CTA in the cluster to arrive |
| `nvvm.mapa` | translate a peer-CTA SMEM pointer to a cluster-mapped address |
| `nvvm.read.ptx.sreg.clusterid.x` / `.y` / `.z` | read cluster-rank index |
| `nvvm.read.ptx.sreg.nclusterid.x` / `.y` / `.z` | read cluster-rank dimension |
| `nvvm.read.ptx.sreg.cluster.ctarank` | per-CTA rank within the cluster |
| `nvvm.read.ptx.sreg.cluster.nctarank` | total CTAs in the cluster |
| `nvvm.barrier.cluster.arrive` / `.wait` (alias spellings used by `gpu.barrier` lowering) | same ops, different mnemonic |

The cluster rank reads sit alongside the special-register family; the dialect exposes them under both `nvvm.read.ptx.sreg.*` and the cluster-specific names so kernels written against either spelling round-trip.

## Operand Tables

### `nvvm.cluster.arrive` / `nvvm.cluster.arrive.relaxed` / `nvvm.cluster.wait`

No operands and no result. Each lowers to a single PTX `barrier.cluster.*;` instruction.

### `nvvm.mapa`

| Position | Name | Type | Notes |
|---|---|---|---|
| operand 0 | `addr` | `ptr addrspace(3)` | local-CTA SMEM pointer |
| operand 1 | `ctaRank` | `i32` | peer CTA index within the cluster |
| result 0 | `mapped` | `ptr addrspace(3)` | cluster-mapped address that aliases peer-CTA SMEM |

The mapped pointer is dereferenceable by ordinary `ld.shared` / `st.shared` instructions and behaves as a view into the peer CTA's slot.

### `nvvm.read.ptx.sreg.clusterid.{x,y,z}` and family

| Position | Name | Type | Notes |
|---|---|---|---|
| result 0 | `r` | `i32` | the requested cluster coordinate |

## LLVM Intrinsic Mapping

| Op | LLVM intrinsic |
|---|---|
| `nvvm.cluster.arrive` | `llvm.nvvm.barrier.cluster.arrive` |
| `nvvm.cluster.arrive.relaxed` | `llvm.nvvm.barrier.cluster.arrive.relaxed` |
| `nvvm.cluster.wait` | `llvm.nvvm.barrier.cluster.wait` |
| `nvvm.mapa` | `llvm.nvvm.mapa.shared.cluster.i64` |
| `nvvm.read.ptx.sreg.clusterid.x` | `llvm.nvvm.read.ptx.sreg.clusterid.x` |
| `nvvm.read.ptx.sreg.cluster.ctarank` | `llvm.nvvm.read.ptx.sreg.cluster.ctarank` |
| `nvvm.read.ptx.sreg.cluster.nctarank` | `llvm.nvvm.read.ptx.sreg.cluster.nctarank` |

## PTX Templates

```text
barrier.cluster.arrive;
barrier.cluster.arrive.relaxed;
barrier.cluster.wait;

mapa.shared::cluster.u64 %r, %addr, %cta_rank;

mov.u32 %r, %clusterid.x;
mov.u32 %r, %clusterid.y;
mov.u32 %r, %clusterid.z;
mov.u32 %r, %nclusterid.x;
mov.u32 %r, %nclusterid.y;
mov.u32 %r, %nclusterid.z;
mov.u32 %r, %cluster_ctarank;
mov.u32 %r, %cluster_nctarank;
```

`mapa` accepts a 64-bit shared-cluster address; the `u64` variant is the only one the dialect emits even when the result is a 32-bit pointer in source code — LLVM widens at type-conversion time.

## Per-Arch Availability

| Op family | SM floor | `ptx_min` |
|---|---|---|
| `cluster.arrive` / `wait` | sm_90 | 8.0 |
| `cluster.arrive.relaxed` | sm_90 | 8.1 |
| `mapa` | sm_90 | 8.0 |
| `clusterid` / `nclusterid` reads | sm_90 | 8.0 |
| `cluster.ctarank` / `nctarank` | sm_90 | 8.0 |

The relaxed-memory variant of `cluster.arrive` is the only op in the family that requires `ptx 8.1`; everything else is legal on 8.0.

## Verifier Invariants

- `mapa` requires the operand pointer in addr-space 3; generic pointers are rejected.
- `ctaRank` is a 32-bit unsigned value; values outside `[0, nctarank)` cause undefined behaviour at runtime but the verifier does not reject them.
- Cluster ops carry no operands and no result; verification rejects any attempt to attach attributes other than location info.
- `cluster.arrive` and `cluster.wait` must appear in pairs across cooperating CTAs; the verifier cannot prove pairing but rejects clearly-unpaired uses inside non-cluster kernels (no `cluster` attribute on the parent `gpu.module`).
