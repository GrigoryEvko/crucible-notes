# SM 90 Warp Specialization - Quick Reference

## The Concept
SM 90 automatically partitions thread blocks into **two specialized warp groups**:
- **cta_group::1** = Consumer warps (compute with MMA)
- **cta_group::2** = Producer warps (load data asynchronously)

This enables **concurrent data loading and computation** for higher performance.

## Group Assignment
**Compiler decides** based on MMA instruction attributes (encoded in result bitfield):

```
if (result & 0x2) {
    assign to cta_group::2  // Producer
} else {
    assign to cta_group::1  // Consumer
}
```

**Location**: `decompiled/sub_35F3330_0x35f3330.c:85-111`

---

## Producer (cta_group::2)

### Responsibilities
1. **Dispatch async copies**: `cp.async.bulk.tensor.*`
2. **Signal barrier**: `mbarrier.arrive.expect_tx [barrier], bytes`
3. **Flush operations**: `cp.async.bulk.commit_group`

### Constraints
- ❌ **CANNOT** use weight-stationary MMA
- ❌ **CANNOT** use with mxf8f6f4/fp4 types
- ⚠️ Must provide correct byte count to `expect_tx`

### Typical Code
```c
// Expected 4096 bytes of tensor data
mbarrier.arrive.expect_tx [barrier], 4096;
cp.async.bulk.tensor.g2s [shared_dst], [global_src];
cp.async.bulk.commit_group;
```

---

## Consumer (cta_group::1)

### Responsibilities
1. **Wait for data**: `mbarrier.wait [barrier]`
2. **Perform computation**: MMA operations
3. **Writeback results**: Store to global memory

### Capabilities
- ✅ **CAN** use weight-stationary MMA optimization
- ✅ **CAN** use all data types
- ✅ Guaranteed data safety after `mbarrier.wait`

### Typical Code
```c
// Wait until expected data arrives
mbarrier.wait [barrier];

// Now perform computation
mma.m16n8k32.sync.aligned [accum], [A], [B];
```

---

## Synchronization Primitives

### mbarrier Operations

| Operation | Code | Purpose |
|-----------|------|---------|
| `arrive::one` | 0x0 | Single thread arrives |
| `arrive_drop` | 0x1 | Arrive without waiting |
| `arrive_wait` | 0x2 | Arrive and block |
| `arrive_wait_drop` | 0x3 | Arrive, wait, cleanup |
| **`expect_tx`** | 0x4 | 🔑 Expect data (PRODUCER) |
| `complete_tx` | 0x5 | Data transmission done |

### Scopes
- `.cluster` = Up to 8 blocks
- `.cta` = Single block
- `.shared::*` = Visible in shared memory

---

## Async Copy Instructions

### Tensor-Accelerated Copies
```
cp.async.bulk.tensor.g2s.<type> [dst_shared], [src_global]
```

Types: `mxf4nvf4`, `f8f6f4`, `mxf8f6f4`, `f16`, `i8`, `tf32`, `mxf4`

### Cluster-Scope Copies
```
cp.async.bulk.global.to.shared.cluster [dst], [src]
```

### Generic Copies
```
cp.async.bulk.gmem.to.dsmem [dst], [src]
cp.async.bulk.tensor.gmem.to.smem.<type> [dst], [src]
```

### Finalization
```
cp.async.bulk.commit_group  // Must call after queueing copies
```

---

## Tensor Core (tcgen05.mma) Operations

### MMA Instructions
```
tcgen05.mma [accum], [operand_a], [operand_b]
tcgen05.mma.block_scale [...]
tcgen05.mma.tile.<type> [...]
```

### Data Types (from encoding)
```
kind::mxf4nvf4      // Mixed int4/float4
kind::f8f6f4        // FP8/FP6/FP4
kind::mxf8f6f4      // Mixed FP8/FP6/FP4
kind::f16           // Half precision
kind::i8            // 8-bit integer
kind::tf32          // TensorFloat-32
kind::mxf4          // Mixed FP4
```

### Weight Stationary ⚠️
**Only available in cta_group::1 (consumer warps)**

```c
// ERROR: "cta_group::2 is not supported with weight stationary"
// Compiler forbids this combination
```

---

## Scale Vector Constraints

| Type | 1X | 2X | 4X |
|------|:--:|:--:|:--:|
| mxf4nvf4 | ❌ | ✅ | ✅ |
| mxf8f6f4 | ✅ | ❌ | ❌ |
| mxf4 | ❌ | ✅ | ❌ |

---

## Typical Pipeline Execution

```
Time:       Iteration 0       Iteration 1       Iteration 2
           ┌─────────┐      ┌─────────┐      ┌─────────┐
Producer:  │Load[0]  │──→  │Load[1]  │──→  │Load[2]  │
           └─────────┘      └─────────┘      └─────────┘
                │ Signal       │ Signal        │ Signal
                ↓              ↓               ↓
           ┌─────────┐      ┌─────────┐      ┌─────────┐
Consumer:  │Wait (1) │──→  │Compute  │──→  │Compute  │
           │         │     │[0]      │     │[1]      │
           └─────────┘      └─────────┘      └─────────┘

Result: Compute and load overlap → higher throughput
```

---

## Compiler Flags

```bash
# Compile for SM 90
-opt-arch=sm_90 -mcpu=sm_90

# Or SM 90a
-opt-arch=sm_90a -mcpu=sm_90a
```

---

## Evidence in Binary

### Key Code Locations
| Function | Lines | Finding |
|----------|-------|---------|
| `sub_35F3330` | 85-111 | Warp group assignment |
| `sub_35F4080` | 138-144 | `expect_tx` opcode |
| `sub_35F4E30` | 46-61 | Barrier operations |
| `sub_A8E250` | 1019+ | cp.async patterns |
| `sub_36E9630` | 169-175 | Weight stationary restriction |

### Key Strings
```
".cta_group::1"
".cta_group::2"
".mbarrier::expect_tx"
"cp.async.bulk.tensor.g2s"
"cp.async.bulk.commit_group"
```

---

## SM 90 vs Earlier Architectures

| Aspect | SM 80 | SM 90 |
|--------|-------|-------|
| Explicit groups | ❌ | ✅ |
| Async copies | Limited | TMA-accelerated |
| Barriers | `__syncthreads` | Specialized mbarrier |
| Compiler support | Manual | Automatic |
| Cluster sync | No | Yes |

---

## Common Errors

### Error 1: Weight Stationary in Producer
```
ERROR: "cta_group::2 is not supported with weight stationary"
FIX: Only use weight-stationary in cta_group::1 (consumer)
```

### Error 2: Wrong expect_tx Byte Count
```
mbarrier.arrive.expect_tx [barrier], 4096;  // Wrong if transfer is 8192 bytes!
cp.async.bulk.tensor ... // Transfers 8192 bytes
FIX: expect_tx(barrier, 8192) must match actual bytes
```

### Error 3: Forgetting commit_group
```
cp.async.bulk.tensor [dst], [src];
// Missing: cp.async.bulk.commit_group
// Result: Transfers may not execute!
FIX: Always call cp.async.bulk.commit_group after queueing
```

---

## Performance Tips

1. **Size producer appropriately**: 1 warp usually sufficient for TMA bandwidth
2. **Match barrier byte count**: Accurate `expect_tx` helps scheduler
3. **Pipeline 2-3 iterations**: Overlap maximizes throughput
4. **Use weight-stationary in consumer**: Free optimization for group::1
5. **Leverage TMA formats**: `tensor.g2s` is faster than generic copies

---

## Quick Facts

- **How many groups?** Always 2 (producer + consumer)
- **How many producers?** Typically 1 warp
- **How many consumers?** Typically 3 warps
- **How many barriers?** Depends on pipeline depth (typically 2-4)
- **Cluster support?** Yes, up to 8 blocks coordinating

---

**For Full Details**: See `WARP_SPECIALIZATION_SM90_ANALYSIS.md`
**For JSON Data**: See `warp_specialization_sm90.json`
