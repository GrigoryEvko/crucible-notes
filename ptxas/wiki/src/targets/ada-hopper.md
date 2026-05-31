# Ada Lovelace & Hopper (SM 89--90a)

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

ptxas handles SM 89 (Ada Lovelace -- RTX 4090, L40S) and SM 90/90a (Hopper -- H100, H200) as adjacent but architecturally distinct targets. Ada shares the Ampere codegen factory (28673) and is stored internally as `"Ampere"`-family despite being a different microarchitecture. Hopper gets its own codegen factory (32768), its own family string `"Hopper"`, and introduces the largest single-generation feature addition in ptxas: WGMMA, thread-block clusters, TMA, setmaxnreg, and distributed shared memory.

| | SM 89 (Ada) | SM 90 (Hopper) | SM 90a (Hopper accel) |
|---|---|---|---|
| **Products** | RTX 4090, RTX 4080, L40S, L4 | H100, H200 | H100, H200 (arch-locked) |
| **Family string** | `"Ampere"` | `"Hopper"` | `"Hopper"` |
| **`__CUDA_ARCH__`** | 890 | 900 | 90a0 |
| **Codegen factory** | 28673 (7 << 12 \| 1) | 32768 (8 << 12) | 32768 |
| **Handler A** | `sub_609E10` | `sub_609DB0` | `sub_609DB0` (shared) |
| **Handler B** | `sub_609CF0` | `sub_609C00` | `sub_609C00` (shared) |
| **Intrinsic init** | `sub_60A810` | `sub_60A5F0` | `sub_60A5F0` (shared) |
| **HW latency table** | `sub_8E8280` (3.1KB) | `sub_8E8480` (5.2KB) | `sub_8E8780` (4.6KB) |
| **Suffix variants** | None | `a` only (no `f`) | -- |
| **Forward compat** | Full (runs on sm_90+) | Full (base variant) | **None** (locked to H100/H200) |

## The "a" Suffix -- Architecture-Accelerated

SM 90a is the first target to use the `a` suffix. It appears in the accelerated validation table (`unk_1D161C0`, 7 entries). The meaning is precise: `sm_90a` SASS executes **only** on the specific silicon it was compiled for (H100/H200) and will not run on any future architecture. This trades forward compatibility for access to features that may not survive to the next generation.

In ptxas, sm_90 and sm_90a share all 7 dispatch-table handler functions. The `a` suffix does not produce different handler code paths -- it produces different **compatibility metadata** in the output cubin. The ELF header records whether the binary is forward-compatible (base) or arch-locked (accelerated), and the CUDA driver enforces this at load time.

Compilation rules from ptxas help text:
- `sm_90a` PTX **must** be compiled to `sm_90a` SASS (no cross-arch)
- `sm_90` PTX can compile to `sm_90` or any later SASS target
- No `sm_90f` variant exists; the `f` suffix starts with Blackwell

## SM 89 -- Ada Lovelace

### Internal Classification

Ada is classified as Ampere-derived in the binary. The profile object constructed by `sub_6765E0` stores `"Ampere"` as the family name and uses codegen factory value 28673 -- identical to sm_80 through sm_88. The compiler distinguishes Ada from Ampere through per-SM capability accessor functions, not through the factory ID.

The encoded SM version for sm_89 is **28677** (7 << 12 | 5), placing it as the 5th variant in the Ampere generation:

| Encoded Value | SM | Variant |
|---|---|---|
| 28673 | sm_80 | 7 << 12 \| 1 (base Ampere) |
| 28674 | sm_86 | 7 << 12 \| 2 |
| 28675 | sm_87 | 7 << 12 \| 3 |
| 28676 | sm_88 | 7 << 12 \| 4 |
| 28677 | sm_89 | 7 << 12 \| 5 (Ada) |

### Ada-Specific Features

Ada introduces 4th-generation tensor cores with FP8 (E4M3, E5M2) support. In the PTX validator, `sub_4A6050` explicitly references `"%s on sm_89"` in its CVT instruction validation, confirming sm_89-specific conversion rules for new data types.

The intrinsic table initializer for sm_89 (`sub_60A810`) enables 39 additional MMA intrinsics:

| Intrinsic ID Range | Count | Category |
|---|---|---|
| 0x209--0x22F | 39 | `__cuda_sm_8x_mma_*` -- extended MMA shapes and types |

These intrinsics cover FP8 MMA operations, block-scale MMA, and additional type combinations beyond what sm_80--88 support. The MMA validator at `sub_49BBA0` checks for `"mma with FP8 floating point type"` and validates against the target SM version.

### Ada Scheduling Profile

The HW latency table for sm_89 (`sub_8E8280`, 3.1KB) is smaller than Hopper's (5.2KB), reflecting Ada's simpler pipeline structure -- no WGMMA async pipeline, no cluster operations. The register file geometry from `sub_8E4400`:

| Parameter | Value | Notes |
|---|---|---|
| Warps per scheduler | 8 | Threshold: encoded SM <= 36863 |
| Dispatch slots | 224 | Same as sm_80 class |
| Sub-architecture variant | 5 | From encoded value 28677 |

## SM 90 / SM 90a -- Hopper

### Profile Construction

Hopper is the first SM to get its own codegen factory value (32768 = 8 << 12) since the Turing/Ampere split. The profile object stores `"Hopper"` as the family name. Key profile fields:

| Field | sm_90 | sm_90a |
|---|---|---|
| SM name | `"sm_90"` | `"sm_90a"` |
| Compute name | `"compute_90"` | `"compute_90a"` |
| LTO name | `"lto_90"` | `"lto_90a"` |
| `CUDA_ARCH` | 900 | 90a0 |
| Family | `"Hopper"` | `"Hopper"` |
| Codegen factory | 32768 | 32768 |

### Hopper Scheduling Profile

The HW latency tables for Hopper are substantially larger than any previous architecture, reflecting the async pipeline and tensor core scheduling complexity:

| Function | Size | Target | Notes |
|---|---|---|---|
| `sub_8E8480` | 5.2KB | sm_90 | Base Hopper latency model |
| `sub_8E8780` | 4.6KB | sm_90a | Arch-accelerated variant |

sm_90a gets its own latency table (4.6KB) distinct from sm_90 (5.2KB), even though they share all dispatch handler functions. This is the only architecture where base and `a` variants have separate scheduling profiles -- all Blackwell variants share their tables within each base SM.

Register file geometry from `sub_8E4400`:

| Parameter | Value | Notes |
|---|---|---|
| Warps per scheduler | 16 | Threshold: encoded SM > 36863 (32768 qualifies) |
| Dispatch slots | 240 | Maximum -- 2x the sm_80 class |
| Sub-architecture variant | 0 | From encoded value 32768 (base variant) |
| Max threads/CTA | 240 | From code object builder `sub_A465F0` |

The jump from 8 warps / 224 slots (sm_89) to 16 warps / 240 slots (sm_90) is the largest warp geometry change in the binary. This doubling of warp capacity directly corresponds to Hopper's 4-warp warpgroup execution model.

### Hopper Intrinsics

The intrinsic initializer for sm_90 (`sub_60A5F0`) enables 38 sub-byte MMA intrinsics:

| Intrinsic ID Range | Count | Category |
|---|---|---|
| 0x23A--0x25F | 38 | `__cuda_sm_9x_mma_sub_byte_internal_*` |

These cover sparse sub-byte MMA operations: s4/u4 sparse variants for m16n8k32, m16n8k64, and m16n8k128 shapes. These are Hopper-specific and do not appear in the Ada (sm_89) intrinsic table.

## WGMMA -- Warpgroup Matrix Multiply-Accumulate

WGMMA is Hopper's signature instruction. It operates on **warpgroups** (4 consecutive warps) rather than single warps, and executes asynchronously -- the tensor core operates in parallel with the warp's regular instruction stream. ptxas handles WGMMA through four PTX instructions and a dedicated compiler pass infrastructure.

### PTX Instructions

Registered in the opcode dispatch table at `sub_5D4190`:

| PTX Instruction | Codegen Handler | Formatter | Formatter Size |
|---|---|---|---|
| `wgmma.mma_async` | `sub_50AC70` | `sub_4DA380` | 295B |
| `wgmma.fence` | `sub_4DA380` | `sub_4DA4B0` | 295B |
| `wgmma.commit_group` | `sub_4DA4B0` | `sub_4DA5E0` | 311B |
| `wgmma.wait_group` | `sub_4DA5E0` | `sub_505B00` | 1066B |

The formatters are the smallest named formatters in ptxas (295 bytes), reflecting WGMMA's compact text representation. `wgmma.wait_group` is significantly larger (1066B) because it must encode the pipeline depth parameter.

### GMMA Pipeline Pass Infrastructure

The WGMMA pipeline optimizer is the largest single-architecture compiler subsystem in ptxas, spanning approximately 100KB of code across 15+ functions in the range 0xACE000--0xAE6000. It is active only for SM 90+ targets.

**Call chain:**

```text
sub_AE4F70  (coordinator -- outside primary range)
 +-- sub_ACE480   (22.7KB)  WGMMA serialization warning emitter
 +-- sub_ADEB40   (43.1KB)  warpgroup.arrive/wait fence insertion
 +-- sub_AE17C0   (37.9KB)  pipeline stage builder
 |    +-- sub_AE0D20  (16.8KB)  live range builder
 |    +-- sub_AE06F0           GMMA operand classifier
 +-- sub_ADDDF0   (20.6KB)  pass entry (vtable dispatch)
      +-- sub_ADCA60  (21.7KB)  scheduling coordinator
           +-- sub_ADBD30  (23.9KB)  register pressure estimator
           |    +-- sub_ADAD60  (8.4KB)  live range limiter
           |    +-- sub_AD9C20  (14.4KB) register class allocator
           +-- sub_AD70B0  (22.6KB)  operand register assignment
```

### Warpgroup Synchronization Injection

The fence insertion pass (`sub_ADEB40`, 43.1KB) scans for `wgmma.mma_async` operations and automatically injects `warpgroup.arrive` and `warpgroup.wait` instructions. These fences manage register ownership when asynchronous tensor core operations are in flight -- the hardware requires explicit handoff between the warpgroup's register file and the tensor core's accumulator registers.

Diagnostic messages emitted by the compiler:
- `"warpgroup.arrive is injected in around line %d by compiler to allow use of registers in GMMA in function '%s'"`
- `"warpgroup.wait is injected in around line %d by compiler to allow use of registers defined by GMMA in function '%s'"`

### WGMMA Serialization Warnings

When the compiler cannot pipeline WGMMA operations, `sub_ACE480` (22.7KB, 98% confidence) emits detailed performance advisories using codes 0x1D55--0x1D57 (7509--7511 decimal). Nine distinct serialization reasons are enumerated:

| Reason Code | Diagnostic Message |
|---|---|
| 1 | `"presence of Extern calls in the function"` |
| 2 | `"wgmma pipeline crossing function boundary"` |
| 3 | `"insufficient register resources for the wgmma pipeline"` |
| 4 | `"program dependence on compiler-inserted warpgroup"` |
| 5 | `"ill formed pipeline stage in the function"` |
| 6 | `"non wgmma instructions defining accumulator registers"` |
| 7 | `"non wgmma instructions reading accumulator registers"` |
| 8 | `"non wgmma instructions defining input registers"` |
| 9 | `"insufficient register resources for the function"` |

All messages are prefixed with `"Potential Performance Loss: wgmma.mma_async instructions are serialized due to ..."`. The pass reads its configuration from offsets +26280 (1-byte enable) and +26288 (dword threshold) on the compilation context.

### GMMA Live Range Management

The live range limiter (`sub_ADAD60`) enforces a maximum on simultaneously active live ranges within GMMA sequences:

```text
"GMMA sequence has too many active live ranges (%d), reduce it to bring it under (%d)"
```

When the threshold is exceeded, the system triggers register spilling or sequence splitting through `sub_ADBD30` (register pressure estimator). The live range builder uses FNV-1a hashing (constants 16777619 and 0x811C9DC5) for instruction deduplication.

## Thread-Block Clusters

Hopper introduces the concept of a **thread-block cluster** -- a group of cooperating CTAs that can access each other's shared memory (distributed shared memory). ptxas adds several PTX directives and special registers to support this.

### Cluster Directives

The directive validator (`sub_4CE6B0`, 48KB) enforces mutual exclusivity of cluster configuration:

```text
".reqnctapercluster and .maxclusterrank cannot both be specified"
```

Two shared-memory state spaces are distinguished:
- `.shared::cta` -- CTA-local shared memory (pre-Hopper behavior)
- `.shared::cluster` -- distributed shared memory accessible across CTAs in a cluster

### Cluster Special Registers

Registered in `sub_61B850` (special register table initializer):

| Special Register | Purpose |
|---|---|
| `%clusterid` | Cluster ID within the grid |
| `%nclusterid` | Number of clusters in the grid |
| `%cluster_ctaid` | CTA position within the cluster |
| `%cluster_nctaid` | Number of CTAs in the cluster |
| `%cluster_ctarank` | Linear rank of CTA within the cluster |
| `%cluster_nctarank` | Total CTAs in the cluster (linear) |
| `%is_explicit_cluster` | Whether this launch uses explicit clustering |
| `%aggr_smem_size` | Aggregate shared memory across cluster |

### Distributed Shared Memory Intrinsics

The intrinsic handler `OCG_DshmemHandler` at `sub_6C60B0` validates distributed shared memory operations:
- `"Cannot use both the selfcast and the broadcast modifier."`
- `"Either the selfcast or the broadcast modifier must be used."`

## TMA -- Tensor Memory Accelerator

The Tensor Memory Accelerator (TMA) provides hardware-accelerated bulk data movement between global and shared memory, using tensor descriptors to specify multi-dimensional copy patterns. In ptxas, TMA is exposed through `cp.async.bulk.tensor`.

### cp.async.bulk.tensor Codegen

The codegen handler (`sub_5AB460`, 45KB) is one of the largest single-instruction handlers in ptxas:

| Property | Value |
|---|---|
| Handler function | `sub_5AB460` |
| Size | 45KB |
| Buffer allocation | 50,000 bytes |
| Registered name | `"cp.async.bulk.tensor"` |
| Dimensionality | 1D through 5D |
| Modes | tile, im2col |
| Cast variants | unicast, multicast |

The TMA intrinsic handler (`OCG_CpAsyncTensorHandler` at `sub_6C8100`) validates operand counts per mode:
- `"Must have 1 input with a1t0 and no multicast"`
- `"Must have 2 inputs with a1t0 and multicast"`
- `"Must have 2 input with a0tx and no multicast"`
- `"Must have 3 inputs with a0tx and multicast"`

### Tensormap Instructions

The tensormap validator (`sub_4A73C0`, 10.8KB) handles tensor descriptor manipulation:
- `".tile"` mode validation
- `"Tensormap field with input value >= 13"` / `"with input value == 4"` bounds checking
- `".tensormap::generic"` addressing mode
- `"Interger Immediate for ordinal"` (sic -- typo preserved from binary)

### Related Bulk Copy Infrastructure

| Handler | Function | Size |
|---|---|---|
| `cp.async.bulk` | `sub_593210` | 5.1KB (formatter) |
| `cp.async.mbarrier.arrive` | `sub_4DC180` | -- |
| `OCG_CpAsyncBulkHandler` | `sub_6C3470` | 20KB |
| `OCG_CpAsyncHandler` | `sub_6C2AE0` | 10KB |

## setmaxnreg -- Dynamic Register Allocation

Hopper introduces `setmaxnreg` (PTX opcode 315) for dynamic register count adjustment. This allows kernels to change their register footprint at runtime, enabling techniques like CTA reconfiguration and warpgroup-level resource management.

### setmaxnreg Pass

The handler (`sub_97EC60`, ~3.5KB, 90% confidence) walks the instruction list looking for opcode 315 and processes or removes them. Five reasons for ignoring setmaxnreg are enumerated as `"Potential Performance Loss"` warnings:

| Reason | Message |
|---|---|
| 1 | `"unable to determine register count at entry"` |
| 2 | `"to maintain minimum register requirements"` |
| 3 | `"to allow debugging"` |
| 4 | `"to maintain compatibility across compilation units"` |
| 5 | `"to maintain compatibility into 'extern' call"` |

The setmaxnreg handling mode is controlled by **knob 653**.

### CTA Reconfig Pragmas

The pragma validator (`sub_97F540`, ~4KB) enforces ordering constraints on `setmaxreg.alloc` and `setmaxreg.dealloc`:
- `"Found an 'alloc' pragma after 'dealloc'"`
- `"Found incompatible thread count re-specification"`
- `"Found a 'dealloc' pragma after 'alloc'"`

The dealloc validator (`sub_98D100`, ~4.8KB) enforces register count bounds:
- `"setmaxreg.dealloc/release has register count (%d) less than launch min target (%d)"`
- `"setmaxnreg.dec has register count (%d) which is larger than the largest temporal register count"`

## Async Pipeline Features

Hopper extends the async copy pipeline introduced in Ampere with mbarrier (memory barrier) objects. ptxas implements mbarrier handling through a cluster of detector, classifier, and emitter functions:

| Function | Address | Identity |
|---|---|---|
| `MBarrierDetector::isNonTrivialMBarrier` | `sub_A94440` | Checks `"%mbarrier_"` prefix |
| `MBarrierDetector::classifyMBarrier` | `sub_A9A5F0` | Returns packed `(type << 32) \| is_mbarrier` |
| `MBarrierDetector::resolveMBarrierBaseName` | `sub_A9A920` | Extracts base name from symbol |
| `MBarrierEmitter::rewriteMBarrierOperand` | `sub_AA33C0` | Constructs `"%%mbarrier_%s_%s"` |

The mbarrier type classifier returns an enumeration of 0--12 for different operation kinds, covering arrive, arrive_drop, expect_tx, and their counted variants.

## Warpgroup Attribute Management

The warpgroup attribute processor (`sub_64BAF0`, 30KB) handles kernel-level attributes introduced with Hopper:

| Attribute String | Purpose |
|---|---|
| `"warpgroup-commit_batch"` | WGMMA commit batch configuration |
| `"warpgroup-wait"` | WGMMA wait configuration |
| `"warpgroup-arrive"` | WGMMA arrive configuration |
| `"setsmemsize-state"` | Shared memory size pragma state |
| `"setmaxreg-state"` | Register limit pragma state |
| `"func_begin"` | Function entry marker |
| `"CC-temp"` | Calling convention temporary |

## Architecture Version Threshold Checks

The binary uses the encoded SM version (codegen factory value) for feature gating throughout the compiler. Key thresholds observed:

| Check Pattern | Threshold | Meaning |
|---|---|---|
| `profile[+372] > 28673` | > sm_80 base | Post-Ampere features |
| `a2 <= 32767` | <= sm_89 class | Pre-Hopper geometry (7 warps, 208 slots) |
| `a2 <= 36863` | <= sm_89 extended | Ampere/Ada geometry (8 warps, 224 slots) |
| `a2 > 36863` | >= sm_90 | Hopper+ geometry (16 warps, 240 slots) |
| Codegen factory == 32768 | sm_90 exactly | Hopper-specific code paths |
| Codegen factory >= 32768 | sm_90+ | WGMMA, cluster, TMA enabled |

The register file descriptor at `sub_8E4400` uses the encoded value to select warp geometry. The full cascade:

```text
encoded <= 20479  ->  4 warps,  96 slots   (pre-Maxwell)
encoded <= 24575  ->  6 warps, 176 slots   (Pascal)
encoded <= 28672  ->  7 warps, 192 slots   (Volta)
encoded <= 32767  ->  7 warps, 208 slots   (Turing/Ampere/Ada)
encoded <= 36863  ->  8 warps, 224 slots   (Ampere extended)
encoded >  36863  -> 16 warps, 240 slots   (Hopper+)
```

Note: sm_89 (encoded 28677) falls in the `<= 32767` range, giving it 7 warps / 208 slots. But the separate warp geometry lookup uses a different cascade where sm_89 (as Ampere class) gets 8 warps / 224 slots. The dual-cascade structure reflects the fact that different subsystems query different profile fields.

## Hardware Resource Geometry

Per-SM hardware resource limits used by ptxas for register allocation, occupancy calculations, and scheduling decisions. Extracted from `sub_8688F0` (universal baseline), `sub_8E4400` (scheduler partition geometry), and `sub_ABF250` (occupancy calculator). See [targets/index.md -- Per-SM Resource Geometry Table](index.md#per-sm-resource-geometry-table) for the complete table across all architectures.

| SM | Regs/SM | Max Regs/Thread | Max Threads/CTA | Warps/SM | Max CTAs/SM | Sched Partitions | Dispatch Slots | Configurable Shared Memory | Conf |
|---|---|---|---|---|---|---|---|---|---|
| `sm_89` | 65,536 | 255 | 1,536 | 48 | 16 | 7 / 208 | 208 | 48 / 100 KB | 90% |
| `sm_90` | 65,536 | 255 | 1,024 | 64 | 32 | 8 / 224 | 224 | 48 / 100 / 132 / 164 / 228 KB | 90% |

**Column definitions:**
- **Regs/SM**: Total 32-bit registers per streaming multiprocessor. 65,536 universally for sm\_75+.
- **Max Regs/Thread**: Maximum registers a single thread can use. 255 universally (`sub_8688F0` offset +612).
- **Max Threads/CTA**: Maximum threads per cooperative thread array (block).
- **Warps/SM**: Total concurrent warps per SM. Determines peak occupancy.
- **Max CTAs/SM**: Maximum concurrent CTAs per SM.
- **Sched Partitions / Dispatch Slots**: From `sub_8E4400` offset +18 (packed DWORD) and offset +22 (WORD).
- **Configurable Shared Memory**: Valid shared memory sizes per CTA, selected by `cudaFuncSetAttribute`.

sm\_90a shares sm\_90's geometry -- the `a` suffix affects only compatibility metadata, not hardware resource limits. The jump from sm\_89 (7 partitions, 208 slots, 48 warps) to sm\_90 (8 partitions, 224 slots, 64 warps) is the largest single-generation scheduling capacity increase in the binary, directly supporting Hopper's 4-warp warpgroup execution model.

## MMA Instruction Validators

Several validator functions gate MMA features by SM version:

| Validator | Address | Size | SM Strings Referenced |
|---|---|---|---|
| WMMA/MMA validator | `sub_4C2FD0` | 12.2KB | `"sm_90"`, `"sm_80"`, `"sm_75"` |
| MMA scale/block validator | `sub_49BBA0` | 11.4KB | `"sm_%d"`, `"mma with FP8 floating point type"` |
| WMMA shape validator | `sub_4BFED0` | 10.3KB | `"sm_80"`, `"sm_75"` |
| CVT arch-specific | `sub_4A6050` | 5.0KB | `"%s on sm_89"` |
| Special register validator | `sub_49A5A0` | 3.5KB | `"sm_90"`, `"%laneid"`, `"%warpid"` |
| Instruction fusion validator | `sub_4AA3E0` | 7.1KB | `"sm_90"` |
| Float instruction validator | `sub_4A2CA0` | 3.7KB | `"sm_90"` |
| Function address validator | `sub_4B1630` | 4.6KB | `"sm_90"`, `"sm_30"`, `"sm_20"` |

The WMMA/MMA validator at `sub_4C2FD0` performs three-way version checks: sm_75 for base WMMA, sm_80 for extended types (BF16/TF32), sm_90 for WGMMA features. FP8 MMA is gated by both sm_89 (Ada) for the data types and sm_90 (Hopper) for the warpgroup shapes.

## Post-Scheduling Statistics

Eight architecture-variant statistics printers (clones at 0x700-byte intervals from `sub_ABBA50`) emit DUMPIR statistics. The metrics cover Hopper-specific counters:

| Metric | Format String |
|---|---|
| MMA counts | `"# [hmma1688=%d]"` (and variants for imma, sparse, dmma) |
| Occupancy | `"# [Occupancy = %f]"` |
| Per-unit throughput | `"# [est adu=%d] [est alu=%d] [est cbu=%d] [est fma2x=%d] ..."` |
| Issue throughput | `"# [issue thru=%f] [adu thru=%f] [alu thru=%f] ..."` |
| WGMMA serialization | `"Potential Performance Loss: wgmma.mma_async ..."` (9 variants) |
| Shared memory | `"# [SharedMem Alloc thru=%f]"` |

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_4A6050` | 5.0KB | CVT validator (sm_89 special cases) | 85% |
| `sub_4C2FD0` | 12.2KB | WMMA/MMA validator (sm_75/80/90 version checks) | 90% |
| `sub_4DA380` | 295B | `wgmma.mma_async` formatter | 99% |
| `sub_4DA4B0` | 295B | `wgmma.fence` formatter | 99% |
| `sub_4DA5E0` | 311B | `wgmma.commit_group` formatter | 99% |
| `sub_505B00` | 1066B | `wgmma.wait_group` formatter | 99% |
| `sub_50AC70` | -- | `wgmma.mma_async` codegen handler | 99% |
| `sub_5AB460` | 45KB | `cp.async.bulk.tensor` codegen (TMA) | 95% |
| `sub_609CF0` | ~1.2KB | sm_89 handler B (capability accessor) | 90% |
| `sub_609DB0` | ~1.2KB | sm_90 handler A (capability accessor) | 90% |
| `sub_609E10` | ~1.2KB | sm_89 handler A (capability accessor) | 90% |
| `sub_60A5F0` | ~1KB | sm_90 intrinsic table initializer | 85% |
| `sub_60A810` | ~1KB | sm_89 intrinsic table initializer | 85% |
| `sub_61B850` | 10KB | Special register table (cluster regs) | 99% |
| `sub_64BAF0` | 30KB | Warpgroup/kernel attribute processor | 80% |
| `sub_6C60B0` | -- | Distributed shared memory intrinsic handler | 65% |
| `sub_6C8100` | -- | TMA (`cp.async.tensor`) intrinsic handler | 85% |
| `sub_8E4400` | 3.3KB | Register file geometry initializer | 90% |
| `sub_8E8280` | 3.1KB | sm_89 (Ada) HW latency table | 85% |
| `sub_8E8480` | 5.2KB | sm_90 (Hopper) HW latency table | 85% |
| `sub_8E8780` | 4.6KB | sm_90a HW latency table | 85% |
| `sub_97EC60` | ~3.5KB | setmaxnreg handler (opcode 315) | 90% |
| `sub_97F540` | ~4KB | CTA reconfig pragma validator | 90% |
| `sub_98D100` | ~4.8KB | setmaxreg.dealloc validator | 90% |
| `sub_A94440` | -- | `MBarrierDetector::isNonTrivialMBarrier` | 85% |
| `sub_A9A5F0` | -- | `MBarrierDetector::classifyMBarrier` | 85% |
| `sub_AA33C0` | -- | `MBarrierEmitter::rewriteMBarrierOperand` | 85% |
| `sub_ACE480` | 22.7KB | WGMMA serialization warning emitter | 98% |
| `sub_AD70B0` | 22.6KB | GMMA operand register assignment | 75% |
| `sub_AD9C20` | 14.4KB | GMMA register class allocator | 75% |
| `sub_ADAD60` | 8.4KB | GMMA live range limiter | 90% |
| `sub_ADBD30` | 23.9KB | GMMA register pressure estimator | 80% |
| `sub_ADCA60` | 21.7KB | GMMA scheduling coordinator | 85% |
| `sub_ADDDF0` | 20.6KB | GMMA pass entry (vtable) | 80% |
| `sub_ADEB40` | 43.1KB | WGMMA sync fence insertion | 95% |
| `sub_AE0D20` | 16.8KB | GMMA live range builder | 80% |
| `sub_AE17C0` | 37.9KB | GMMA pipeline stage builder | 85% |
| `sub_AE4F70` | -- | GMMA pass coordinator (outside range) | 90% |
| `sub_AE5030` | 15.5KB | GMMA scheduling wrapper (alt entry) | 75% |

## Cross-References

- [SM Architecture Map](index.md) -- Validation tables, codegen factory values, suffix semantics
- [Turing & Ampere (SM 75--88)](turing-ampere.md) -- Ampere baseline that Ada inherits
- [Blackwell (SM 100--121)](blackwell.md) -- Next-generation features (tcgen05, expanded `a`/`f` variants)
- [Intrinsic Table (608 Entries)](../intrinsics/index.md) -- Full intrinsic catalog with sm_8x and sm_9x ranges
- [Pass Inventory](../passes/index.md) -- GMMA/WGMMA pipeline pass placement in 159-phase schedule
- [Scheduling Overview](../scheduling/overview.md) -- HW latency table architecture
- [CLI Options](../config/cli-options.md) -- `--gpu-name sm_90a` parsing
- [Knobs System](../config/knobs.md) -- Knob 653 (setmaxnreg mode)
