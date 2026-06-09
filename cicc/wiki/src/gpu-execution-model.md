# GPU Execution Model

This page is the single authoritative reference for the GPU hardware properties that drive cicc's optimization decisions. Every other wiki page that mentions register pressure, occupancy cliffs, memory coalescing, warp divergence, or the `.param` calling convention should cross-reference this page rather than re-explaining the concepts inline. The page exists because these properties shape literally every pass in the compiler, from SROA (which exists to avoid `.local` memory) through register allocation (which trades register count for occupancy) to LTO inlining (which eliminates `.param` marshaling). Understanding the execution model is a prerequisite for understanding any cicc optimization decision that differs from upstream LLVM.

The material below describes the hardware model as cicc sees it — the properties that are visible in the binary through TTI hooks, threshold constants, cost model comparisons, and diagnostic strings. Where specific numbers vary by SM generation, the sm_70+ (Volta through Blackwell) values are given unless otherwise noted.


## SIMT Warp Execution

NVIDIA GPUs execute threads in groups of 32 called **warps**. All 32 threads in a warp share a single program counter under the SIMT (Single Instruction, Multiple Threads) model. The hardware issues one instruction per clock to all 32 threads simultaneously — there is no per-thread instruction decode, fetch, or issue overhead. Each thread has its own register state and can execute a different data path, but they all advance through the program in lockstep.

This is not SIMD in the CPU sense. On a CPU with AVX-512, the programmer (or compiler) explicitly packs 16 floats into a vector register and issues a single vector instruction. On a GPU, the programmer writes scalar code for one thread, and the hardware transparently replicates it across 32 threads. The distinction matters for cicc because **vectorization on GPU does not fill SIMD lanes** — it produces wide loads (`ld.v2`, `ld.v4`) within a single thread's scalar stream to improve memory transaction width and reduce instruction count. The VF returned by `TTI::getRegisterBitWidth(Vector)` is 32 bits (one scalar register), not 512 or 1024.

### Divergence

When a branch condition evaluates differently across threads in a warp, the hardware **serializes both paths**. First the "taken" subset executes while the others are masked off; then the "not-taken" subset executes. The warp reconverges at a point determined by the hardware's reconvergence stack (pre-Volta) or independent thread scheduling (Volta+). Both paths execute regardless of how many threads take each side, so a divergent branch in a hot loop can halve throughput even if only one thread disagrees.

Divergence is the primary reason cicc includes the [StructurizeCFG](llvm/structurizecfg.md) pass (which converts irreducible control flow to reducible form), the [CSSA](passes/cssa.md) pass (which repairs SSA across divergent join points), the [Loop Index Split](passes/loop-index-split.md) pass (which eliminates index-dependent branches that cause per-iteration divergence), and the [Branch Distribution](passes/branch-distribution.md) pass (which separates uniform from divergent computation).

The constant `warpSize = 32` is hardcoded in cicc's SCEV range analysis (intrinsic ID ~370, range `[32, 33)`) and is the architectural constant behind every power-of-two factor enforcement in the [loop unroller](llvm/loop-unroll.md) and [loop vectorizer](llvm/loop-vectorize.md).


## Register Pressure and Occupancy

The register file is the single most constrained resource on an NVIDIA GPU and the single most important factor in cicc's optimization heuristics. Understanding the relationship between register count, occupancy, and performance is essential to understanding why cicc makes the decisions it does.

### The Register Budget

Each Streaming Multiprocessor (SM) has a fixed 32-bit register file:

| SM Generation | Registers per SM | Max Registers per Thread |
|---|---|---|
| SM 70 (Volta) | 65,536 | 255 |
| SM 75 (Turing) | 65,536 | 255 |
| SM 80 (Ampere) | 65,536 | 255 |
| SM 86 (Ampere GA10x) | 65,536 | 255 |
| SM 89 (Ada) | 65,536 | 255 |
| SM 90 (Hopper) | 65,536 | 255 |
| SM 100 (Blackwell) | 65,536 | 255 |

These 65,536 registers are **shared among all resident threads**. The hardware partitions them at kernel launch time based on the per-thread register count reported by `ptxas`. The partition is coarse-grained — registers are allocated in units of warp groups, not individual threads.

### Occupancy Cliffs

The relationship between per-thread register count and achievable occupancy is a step function with sharp discontinuities:

```text
Registers/thread    Max warps/SM    Max threads/SM    Occupancy
      32                64              2048            100%
      33-40             48              1536             75%
      41-48             32              1024             50%   <-- cliff
      49-64             32              1024             50%
      65-80             24               768            37.5%  <-- cliff
      81-96             20               640            31.3%
      97-128            16               512             25%   <-- cliff
     129-168            12               384            18.8%
     169-255             8               256            12.5%  <-- cliff
```

(Exact thresholds vary by SM generation and block size; these are representative for sm_70+ with standard block configurations.)

Adding a single register — from 32 to 33 registers per thread — drops maximum occupancy from 64 warps to 48 warps, a 25% reduction. These are the **occupancy cliffs** that cicc's heuristics are designed to avoid. The cost is asymmetric: the 33rd register provides trivial benefit (one fewer spill), but the occupancy loss costs 25% of the SM's latency-hiding capacity.

This is why:
- The [loop unroller](llvm/loop-unroll.md) uses conservative thresholds that balance ILP against register growth
- The [loop vectorizer](llvm/loop-vectorize.md) limits VF to 2 or 4 even though wider vectors are legal
- [LSR](llvm/lsr.md) has an `lsr-rp-limit` knob that hard-rejects formulae exceeding a register pressure ceiling
- [LICM](llvm/licm-real.md) runs twice — once to hoist, once to sink back values whose extended live ranges hurt occupancy
- The [rematerialization](passes/rematerialization.md) pass recomputes values rather than keeping them live across long ranges
- The [register allocator](llvm/register-allocation.md) uses `-maxreg` (default 70) as a pressure cap rather than a physical assignment constraint

The cicc binary contains no explicit occupancy table — it delegates final register assignment and occupancy computation to `ptxas`. But the thresholds in the optimization passes (LSR's `lsr-rp-limit`, the unroller's `PartialThreshold`, the vectorizer's register-pressure-bounded interleave count) are all calibrated to stay below known cliff boundaries.

### PTX Virtual Registers

PTX has no fixed physical register file from the compiler's perspective. cicc emits virtual registers in nine typed classes (`%p`, `%rs`, `%r`, `%rd`, `%f`, `%fd`, `%h`, `%hh`, `%rq` — see [Register Classes](reference/register-classes.md)). The `ptxas` assembler performs the actual register allocation from virtual to physical registers, using the SM's register file as the constraint. cicc's job is to minimize the number of simultaneously live virtual registers so that `ptxas` can produce a low register-count assignment.

The typed register model means that a 32-bit integer (`%r`) and a 32-bit float (`%f`) occupy separate register namespaces — they never alias. A 64-bit value (`%rd`, `%fd`) occupies two 32-bit register slots. An `Int128Regs` value (`%rq`) occupies four. This is why the [type legalization](llvm/type-legalization.md) pass aggressively scalarizes vector types and the [IV demotion](passes/iv-demotion.md) pass narrows 64-bit induction variables to 32-bit: every bit of width reduction directly saves register pressure.


## Memory Hierarchy

GPU memory is organized into physically disjoint address spaces with radically different performance characteristics. On a CPU, the entire address space is a flat virtual memory with uniform-latency cache hierarchy. On a GPU, choosing the wrong address space for an access can cost 100x in latency. This section summarizes the performance-relevant properties; for complete address space encoding, aliasing rules, and data layout strings, see [Address Spaces](reference/address-spaces.md).

### Latency Table

| Memory | LLVM AS | PTX Qualifier | Latency (cycles) | Scope | Capacity |
|---|---|---|---|---|---|
| Registers | — | `%r`, `%f`, etc. | 0 | Per-thread | 255 per thread (SM 70+) |
| Shared | 3 | `.shared` | 20-30 | Per-CTA (block) | 48-228 KB per SM |
| Constant cache | 4 | `.const` | 4-8 (hit) | Read-only, device-wide | 64 KB per SM |
| Parameter | 101 | `.param` | 4-8 | Per-kernel launch | Mapped to constant bank |
| Local (L1 hit) | 5 | `.local` | ~30 | Per-thread stack | L1 partition |
| Local (L2 hit) | 5 | `.local` | ~200 | Per-thread stack | L2 partition |
| Global (L2 hit) | 1 | `.global` | 32-128 | Device-wide | L2 cache |
| Global (DRAM) | 1 | `.global` | 200-800 | Device-wide | Device DRAM |
| Generic | 0 | `.generic` | +4-8 over resolved | Virtual | Runtime-resolved |
| Shared cluster | 7 | `.shared::cluster` | 30-50 | Cross-CTA (SM 90+) | Cluster shared pool |

The 200-800 cycle range for global DRAM access is the defining constraint of GPU performance. It means that a single cache-missing load stalls the executing warp for hundreds of cycles. The hardware hides this latency through **warp-level multithreading** (see next section), but only if enough warps are resident — which brings us back to register pressure and occupancy.

### Why Each Memory Matters for cicc

**Registers vs. `.local`:** Every alloca that [SROA](llvm/sroa.md) fails to promote becomes a `.local` allocation backed by DRAM. A `.local` access that misses L1 costs 200-400 cycles versus zero for a register. This is why SROA runs twice in the pipeline and why cicc's inline budget (20,000 vs upstream 225) is so aggressive — inlining eliminates allocas from byval parameter copies.

**Shared memory (AS 3):** On-chip SRAM with 20-30 cycle latency, shared across all threads in a CTA (thread block). Uses 32-bit pointers (when `+sharedmem32bitptr` is active), saving one register per pointer compared to 64-bit global pointers. This is why [LSR](llvm/lsr.md) has `disable-lsr-for-sharedmem32-ptr` — strength-reducing a 32-bit shared pointer can produce 64-bit intermediates that defeat the optimization.

**Constant memory (AS 4):** Hardware-cached read-only memory with 4-8 cycle latency on cache hit. The NVVM AA marks AS 4 as `NoModRef`, enabling LICM to hoist constant loads without checking for intervening stores.

**`.param` space (AS 101):** Used for function argument passing (see the calling convention section below). Read-only from device code. Mapped to the constant cache path, so reads are 4-8 cycles.

**Generic (AS 0):** The performance killer. A generic pointer forces a runtime address-space lookup (+4-8 cycles per access) and destroys alias analysis precision (every generic pointer `MayAlias` with everything). This is why [MemorySpaceOpt](passes/memory-space-opt.md) exists — resolving generic pointers to specific address spaces is one of the highest-impact optimizations in cicc.


## Memory Coalescing

The GPU memory subsystem services warp-wide requests in **128-byte transactions** (or 32-byte sectors on some architectures). When 32 threads in a warp access 32 consecutive 4-byte values (128 bytes total), the hardware coalesces the 32 individual requests into a single transaction. This is the **stride-1 access pattern** — the ideal case.

```text
Thread 0  loads addr+0    ┐
Thread 1  loads addr+4    │
Thread 2  loads addr+8    │  One 128-byte transaction
...                       │
Thread 31 loads addr+124  ┘
```

When threads access non-consecutive addresses (stride > 1, scattered, or misaligned), the hardware must issue multiple transactions to satisfy the warp's requests. In the worst case (32 threads accessing 32 different cache lines), a single warp load generates 32 separate transactions, reducing effective bandwidth by 32x.

Coalescing is why the [loop vectorizer](llvm/loop-vectorize.md) targets VF=2 or VF=4 on GPU: vectorizing a per-thread loop with `ld.v4.f32` loads four consecutive elements per thread in a single wide transaction, improving bytes-per-transaction. It is also why the loop unroller enforces [power-of-two factors](llvm/loop-unroll.md) — non-power-of-two unroll factors create asymmetric access patterns that interact poorly with the 128-byte transaction boundary.

The memory coalescing model also explains why cicc's [SLP vectorizer](llvm/slp-vectorizer.md) pairs adjacent scalar loads into `ld.v2` / `ld.v4` instructions — not for SIMD parallelism (there is none) but for transaction width optimization.


## No Out-of-Order Execution

GPU warps execute instructions **strictly in program order**. There is no out-of-order execution, no speculative execution, no branch prediction, and no reorder buffer. A warp that encounters a long-latency operation (global memory load, texture fetch) simply stalls until the result is available.

The sole latency-hiding mechanism is **warp-level multithreading**. Each SM maintains multiple warps in flight simultaneously. When one warp stalls on a memory access, the hardware switches to another ready warp in the same clock cycle (zero-cost context switch, because each warp has its own register state). This is why occupancy matters — more resident warps means more opportunities to hide latency through interleaving.

The absence of OOO execution has profound implications for cicc:

**ILP must be compiler-created.** On a CPU, the hardware reorder buffer discovers and exploits instruction-level parallelism dynamically. On a GPU, the compiler (cicc + ptxas) must explicitly schedule independent instructions adjacent to each other so the hardware can overlap them. This is why [loop unrolling](llvm/loop-unroll.md) is so valuable on GPU — it creates independent instructions from different iterations that the scheduler can interleave — and why the interleave count in the [loop vectorizer](llvm/loop-vectorize.md) exists (it replicates the vectorized body to expose more ILP).

**Every stall is a stall.** There is no store buffer to absorb write latency, no load queue to speculatively issue reads. The scheduling passes ([instruction scheduling](llvm/scheduling.md), [block placement](llvm/block-placement.md)) must model this accurately.

**Instruction issue width bounds throughput.** Each SM has a fixed number of instruction schedulers (typically 4 per SM on sm_70+), each issuing one instruction per clock to one warp. The total instruction throughput of an SM is `schedulers * clock_rate`. The TTI scheduling info at `TTI+56` (issue width at +32, latency at +36 within the sub-structure) encodes this model and feeds the [vectorizer's interleave count cap](llvm/loop-vectorize.md).


## The `.param` Calling Convention

Function calls on NVIDIA GPUs are expensive in a way that has no CPU equivalent. On x86, a function call pushes arguments to registers or the stack (a cached memory region), executes `CALL`, and the callee reads them back. Total overhead: 5-20 cycles. On GPU, there is no hardware call stack for registers. The PTX calling convention works through the `.param` address space:

### Call Sequence

```ptx
// Caller side:
.param .align 8 .b8 param0[16];           // DeclareParam
st.param.b64 [param0+0], %rd1;            // Store arg 0, field 0
st.param.b64 [param0+8], %rd2;            // Store arg 0, field 1
.param .b32 param1;                        // DeclareScalarParam
st.param.b32 [param1+0], %r5;             // Store arg 1
call.uni (retval0), callee, (param0, param1);  // The actual call

// Callee side:
ld.param.b64 %rd10, [param0+0];           // Load arg 0, field 0
ld.param.b64 %rd11, [param0+8];           // Load arg 0, field 1
ld.param.b32 %r20,  [param1+0];           // Load arg 1
// ... function body ...
st.param.b32 [retval0+0], %r30;           // Store return value
ret;

// Back in caller:
ld.param.b32 %r6, [retval0+0];            // Load return value
```

Each function call generates **O(n) `st.param` + O(n) `ld.param` instructions** where n is the total number of argument fields (not just argument count — structs are marshaled field-by-field). A function with 8 struct arguments containing 4 fields each generates 32 stores + 32 loads + the call instruction itself. At shared/constant-cache latency (4-8 cycles per access), this is 256-512 cycles of pure marshaling overhead.

Additionally:
- **Call boundaries destroy scheduling freedom.** The hardware cannot overlap instructions across a call/return boundary.
- **Call boundaries force register save/restore.** If the callee needs more registers than are available in the caller's allocation, the hardware spills to `.local` memory (DRAM, 200-800 cycles).
- **Indirect calls are catastrophic.** An indirect call (`call.uni` through a register) prevents all of the above from being optimized statically. No inlining, no cross-function register allocation, no dead argument elimination.

This is why:
- cicc's [custom inliner](lto/inliner-cost.md) uses a 20,000-unit budget (89x upstream LLVM's 225) — the `.param` marshaling cost for a typical function easily exceeds the 225-unit threshold
- [LTO](lto/index.md) is dramatically more valuable on GPU than on CPU — cross-module inlining eliminates `.param` overhead for functions in separate translation units
- [Whole-program devirtualization](lto/devirtualization.md) is critical — converting indirect calls to direct calls enables inlining and eliminates the worst-case register spill scenario
- 60% of the NVIDIA custom inliner's code computes type-size comparisons for argument coercion cost, because the `.param` marshaling cost dominates the inlining decision

### The SelectionDAG Encoding

The SelectionDAG backend uses opcodes `DeclareParam` (505), `DeclareScalarParam` (506), `StoreV1/V2/V4` (571-573), and `LoadRetParam` / `LoadV1/V2/V4` (515-516, 568-570) for the param passing convention. The `.param` space is encoded as SelectionDAG code 5 in `sub_33B0210`. For complete opcode details, see [NVPTX Machine Opcodes](reference/nvptx-opcodes.md).


## Address Space Semantics

GPU memory is partitioned into physically disjoint hardware regions. Pointers in different non-generic address spaces can never reference the same byte — a property that [NVVM AA](infra/alias-analysis.md) exploits for O(1) NoAlias determination. The generic address space (AS 0) is a virtual overlay resolved at runtime by the hardware's address translation unit, which tests whether the address falls in the shared, local, or global window.

The following properties have direct optimization impact:

| Property | Global (AS 1) | Shared (AS 3) | Local (AS 5) | Constant (AS 4) |
|---|---|---|---|---|
| Pointer width | 64-bit | 32-bit* | 32-bit (effective) | 64-bit |
| Read-only | No | No | No | **Yes** |
| Cross-CTA visible | Yes | No | No | Yes |
| Hardware addressing modes | Base + offset | Base + offset, banked | Frame pointer + offset | Indexed constant cache |
| Coalescing | 128-byte transactions | 32 banks, 4-byte stride | Per-thread (no coalescing) | Broadcast to warp |

\* 32-bit when `+sharedmem32bitptr` target feature is active (the default for sm_70+).

The 32-bit pointer optimization for shared memory saves one register per shared-memory pointer and reduces all address arithmetic from 64-bit to 32-bit operations. This is encoded in the NVPTX data layout string as `p3:32:32:32` and is the reason the [IV Demotion](passes/iv-demotion.md) pass exists — it narrows 64-bit induction variables to 32-bit when the loop operates entirely in shared memory.

For the complete address space reference — including aliasing rules, the MemorySpaceOpt bitmask encoding, `cvta` intrinsic mapping, `isspacep` folding, and per-SM shared memory sizes — see [Address Spaces](reference/address-spaces.md).


## Compiler Implications Summary

Every major cicc optimization decision traces back to one or more of the properties above. The following table maps each hardware property to the compiler passes it shapes:

| Hardware Property | Compiler Impact | Key Passes |
|---|---|---|
| Warp divergence serializes both paths | Minimize control flow in hot loops | [StructurizeCFG](llvm/structurizecfg.md), [CSSA](passes/cssa.md), [Loop Index Split](passes/loop-index-split.md), [Branch Distribution](passes/branch-distribution.md) |
| Register count determines occupancy | All transforms must minimize live values | [Register Allocation](llvm/register-allocation.md), [LSR](llvm/lsr.md), [LICM](llvm/licm-real.md), [Rematerialization](passes/rematerialization.md), [IV Demotion](passes/iv-demotion.md) |
| Occupancy cliffs are discrete | Threshold-driven heuristics with cliff awareness | [Loop Unroll](llvm/loop-unroll.md), [Loop Vectorize](llvm/loop-vectorize.md), LSR `lsr-rp-limit` |
| No OOO execution | Compiler must create ILP | [Loop Unroll](llvm/loop-unroll.md) (ILP via body replication), [Scheduling](llvm/scheduling.md), vectorizer interleave count |
| `.local` spill costs 200-800 cycles | Aggressively promote allocas | [SROA](llvm/sroa.md) (runs twice), [Inliner](lto/inliner-cost.md) (20K budget eliminates byval copies) |
| `.param` marshaling is O(n) per call | Aggressively inline | [Inliner](lto/inliner-cost.md), [LTO](lto/index.md), [Devirtualization](lto/devirtualization.md) |
| 128-byte coalescing transactions | Optimize memory access stride | [Loop Vectorize](llvm/loop-vectorize.md) (VF=2/4 for `ld.v2`/`ld.v4`), [SLP Vectorizer](llvm/slp-vectorizer.md) |
| Address spaces are disjoint | NoAlias for cross-space pairs | [NVVM AA](infra/alias-analysis.md), [MemorySpaceOpt](passes/memory-space-opt.md) |
| Generic pointers destroy alias precision | Resolve to specific space | [MemorySpaceOpt](passes/memory-space-opt.md), [IPMSP](passes/ipmsp.md) |
| Shared memory uses 32-bit pointers | Narrow IV and address width | [IV Demotion](passes/iv-demotion.md), [LSR](llvm/lsr.md) `disable-lsr-for-sharedmem32-ptr` |
| Closed-world compilation model | Full-program visibility | [LTO](lto/index.md), [Dead Kernel Elimination](lto/index.md), [Devirtualization](lto/devirtualization.md) |
| Constant cache is 4-8 cycles | Hoist constant loads freely | [LICM](llvm/licm-real.md), NVVM AA `NoModRef` for AS 4 |


## What Upstream LLVM Gets Wrong

Upstream LLVM's NVPTX backend correctly implements the PTX virtual register model and the basic address space numbering. But the optimization passes assume CPU-like economics:

1. **Inline threshold of 225** assumes function calls cost 5-20 cycles. GPU calls cost hundreds of cycles due to `.param` marshaling. NVIDIA overrides to 20,000.

2. **LSR cost model** compares formulae by counting registers and instructions with equal weight. On GPU, one extra register can cost 25% occupancy; one extra instruction costs nearly nothing. NVIDIA replaces the formula solver entirely.

3. **LICM assumes hoisting is always profitable.** On CPU, moving an operation from loop body to preheader is strictly beneficial. On GPU, it extends the live range of the hoisted value across the entire loop, consuming a register for all iterations. NVIDIA runs LICM twice (hoist then sink) and relies on rematerialization to undo unprofitable hoists.

4. **Vectorization targets SIMD lane width.** `TTI::getRegisterBitWidth(Vector)` returns 256 (AVX2) or 512 (AVX-512) on CPU. NVPTX returns 32 — there are no SIMD lanes. Vectorization targets memory transaction width, not ALU parallelism.

5. **No occupancy model exists in upstream.** CPU register allocation minimizes spill cost. GPU register allocation must minimize total register count to maximize occupancy. These are different objective functions.

6. **Address spaces are an afterthought.** Upstream LLVM treats address spaces as metadata annotations. On GPU, they are physically disjoint hardware memory partitions with different pointer widths, latencies, and aliasing properties. Every pass that touches pointers must be address-space-aware.


## Cross-References

- [Address Spaces](reference/address-spaces.md) — complete encoding, aliasing rules, MemorySpaceOpt bitmask, data layout strings
- [Register Classes](reference/register-classes.md) — nine typed register classes, encoding scheme, coalescing rules
- [Register Allocation](llvm/register-allocation.md) — greedy RA, `-maxreg` constraint, pressure tracking
- [Loop Vectorize](llvm/loop-vectorize.md) — VF selection, memory coalescing motivation, register-pressure-bounded IC
- [Loop Unroll](llvm/loop-unroll.md) — ILP vs register pressure tradeoff, power-of-two enforcement
- [LSR (NVIDIA Custom)](llvm/lsr.md) — occupancy-aware formula solver, register pressure gating
- [LICM](llvm/licm-real.md) — hoist/sink dual invocation, register pressure tension
- [SROA](llvm/sroa.md) — `.local` elimination, dual-invocation pipeline position
- [Inliner Cost Model](lto/inliner-cost.md) — 20K budget, `.param` marshaling cost, four parallel models
- [LTO & Module Optimization](lto/index.md) — closed-world model, dead kernel elimination
- [MemorySpaceOpt](passes/memory-space-opt.md) — generic-to-specific address space resolution
- [StructurizeCFG](llvm/structurizecfg.md) — divergence-safe control flow restructuring
- [CSSA](passes/cssa.md) — conventional SSA for SIMT divergence correctness
- [Rematerialization](passes/rematerialization.md) — register pressure reduction via recomputation
- [IV Demotion](passes/iv-demotion.md) — 64-bit to 32-bit IV narrowing for shared memory
- [Instruction Scheduling](llvm/scheduling.md) — in-order scheduling, MRPA pressure tracking
- [NVPTX Target Infrastructure](infra/nvptx-target.md) — TTI hooks, data layout, target features
