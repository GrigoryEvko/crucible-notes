# Memory Hierarchy and Data Flow

## Abstract

NVIDIA GPUs expose seven distinct memory spaces with order-of-magnitude differences in bandwidth, latency, capacity, and visibility scope. A Blackwell-class kernel routinely touches five of them in one mainloop: kernel parameters arrive in parameter memory, operands stage from global memory through shared memory into tensor memory, accumulators live in tensor memory or registers, and spills land in thread-local memory backed by global memory. Tileiras tracks every pointer's address space through the lowering cascade, validates address-space-aware operations at each layer, and emits PTX that names each space explicitly so ptxas can issue the right state-space-qualified memory instructions.

The compiler enforces a single invariant: every pointer-typed SSA value has a known address space by the time it reaches the backend, or it has provably failed to converge and emits an `assuming global memory space` diagnostic. The rest of the pipeline — operand staging, async-copy lowering, alias analysis, register allocation — assumes that invariant and breaks if a generic pointer slips past memory-space optimization without a concrete tag.

This page is the canonical reference for the spaces themselves, their representation at every compilation stage, and the data-flow patterns that move operands between them. [AddrSpace Vote Lattice](addrspace-vote-lattice.md) covers the inference algorithm in depth; [MemorySpaceOpt and process-restrict](../nvptx-passes/memory-space-opt-and-process-restrict.md) covers the propagation pass that runs the algorithm; this page provides the orientation that ties the two together.

## The Seven Memory Spaces

Every NVPTX address space corresponds to a distinct hardware structure with its own physical realisation. The PTX state-space modifier, the LLVM address-space number, and the MLIR address-space attribute must all agree on which structure a pointer names — the three encodings are isomorphic and disagreement is a verifier error.

| Space | PTX state space | LLVM AS | MLIR encoding | Capacity (H100/B100) | Bandwidth | Visibility |
|---|---|---:|---|---|---|---|
| Global | `.global` | 1 | `#gpu.address_space<global>` | tens of GB | ~3 TB/s | device-wide |
| Shared | `.shared` | 3 | `#gpu.address_space<workgroup>` | 228 KB / SM | ~20 TB/s | CTA |
| Distributed Shared | `.shared::cluster` | 7 | `#nvvm.shared_space<cluster>` | `cluster_size × 228 KB` | DSMEM network | cluster |
| Constant | `.const` | 4 | `#gpu.address_space<uniform_constant>` | 64 KB / module | broadcast | device |
| Local | `.local` | 5 | (LLVM stack) | bounded by spill budget | GMEM-backed | thread |
| Tensor (SM100+) | `.tmem` (proxy via `tcgen05`) | 6 | TMEM dialect handle | 256 cols × 128 rows / SM | per-SM | SM |
| Parameter | `.param` | 101 | (kernel arg, byval) | <= 4 KB / launch | broadcast | per-launch |
| Generic | (unqualified) | 0 | (unqualified pointer) | union of above | via `cvta` | (resolved at runtime) |
| Register | (named regs) | n/a | (LLVM virtual reg) | ~64 KB / SM | warp-private | warp |

Generic is the lattice's top element — a pointer that has not been refined to one concrete space. The hardware supports it through `cvta.to.X` instructions that decode the high-order address bits to recover the concrete space, but every generic-pointer dereference costs an extra cycle and prevents the backend from issuing a state-space-qualified load. Memory-space optimization exists to eliminate generic pointers wherever a concrete provenance can be proved.

Tensor memory is the SM100 newcomer. It is per-SM, addressed in a 128-row dense grid, and reachable only through the `tcgen05` instruction family — no `ldg`, no `cp.async`, no register-to-TMEM move outside that family. See [tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) for the allocator contract and operand-residency rules.

Distributed shared memory is shared memory addressed across cluster CTAs. The pointer is still `addrspace(3)` at the LLVM level, but a separate `addrspace(7)` exists for pointers that have already been translated through `nvvm.mapa` and name a peer CTA's shared region. The translation itself is a hardware instruction; the peer CTA's allocator must have placed the destination at the same SMEM offset for the translated pointer to be meaningful.

Register and parameter spaces sit at the extremes. Registers are warp-private, do not carry a pointer type at the LLVM level (they appear as virtual registers in MIR), and have no `addrspace` encoding because they cannot be the target of a load or store. Parameter memory is the kernel-argument buffer the driver fills before launch: in PTX it appears as `ld.param`, in LLVM IR as `addrspace(101)` pointers that the LowerArgs pass converts to direct loads.

## Address Space at Each Compiler Stage

The same memory space appears under different encodings at every stage of the lowering. Tileiras stage transitions preserve the address-space tag — a pointer that was global at the `cuda_tile` level is still global at the LLVM level — but the encoding changes from a dialect-specific attribute to a generic LLVM `addrspace(N)` numeric token to a PTX state-space modifier.

| Stage | How address spaces appear | Where the tag lives |
|---|---|---|
| `cuda_tile` | `!cuda_tile.ptr<f32>` and `!cuda_tile.partition_view<...>` | Type attribute, optional `addrspace` annotation |
| `nv_tileaa` | `!llvm.ptr` with `addrspace` attribute on memrefs | Operation-side memory-space attribute |
| `nv_tileas` | TMA descriptor types, async-copy ops with explicit space args | TMA descriptor + per-op `mem_space` enum |
| `cute_nvgpu` | Copy/MMA atoms tagged with operand residency | Atom metadata in the dialect attribute |
| LLVM IR | `ptr addrspace(N)` typed pointers | Pointer type, propagated through SSA |
| NVPTX MIR | Per-instruction state-space encoding | `MachineMemOperand::getAddrSpace()` |
| PTX | State-space modifier on every memory instruction | Lexical `.global` / `.shared` / `.tmem` etc. |

The boundary that matters most is between `tileas` and LLVM. Up to `tileas`, address spaces live on operation attributes — a `cp.async` op carries its source and destination spaces as enum operands, and the verifier rejects mismatched pairings. Below LLVM, address spaces live on the pointer type — a `ptr addrspace(1)` is structurally distinct from `ptr addrspace(3)`, and the LLVM verifier rejects assignments between them without an explicit `addrspacecast`. The cute-to-LLVM lowering is the pass that translates the operation-attribute encoding into the type encoding; see [cute and cute_nvgpu to LLVM](../lowering/cute-and-cute_nvgpu-to-llvm.md).

Two stages have no native address-space encoding and rely on context. The `cuda_tile` dialect carries address spaces only as optional annotations on memrefs — the public surface is shape-typed, not space-typed, and the inference cascade fills in the missing tags. The PTX layer has no encoding at all: state spaces are part of the instruction mnemonic, and ptxas sees them as lexical tokens that select the right hardware opcode at assembly time.

## The Address-Space Inference Algorithm

MemorySpaceOpt runs a finite-height-lattice forward data-flow analysis over the SSA graph. The lattice has one bottom element (BOTTOM, unknown), one top element (GENERIC, conflict), and six concrete address-space elements that form an antichain in between. The meet of two distinct concrete elements is GENERIC; the meet of BOTTOM with any element is the other element. Convergence is bounded by `2 × |pointer values|` because each pointer can be refined at most twice (BOTTOM → concrete → GENERIC) before reaching a fixed point.

```c
AddressSpace meet(AddressSpace a, AddressSpace b) {
    if (a == AS_BOTTOM) return b;
    if (b == AS_BOTTOM) return a;
    if (a == b)         return a;
    return AS_GENERIC;
}

void propagate(Lattice *lat, Function *fn) {
    seed_from_kernel_arguments(lat, fn);          /* global / constant / byval seeds */

    while (lat_changed(lat)) {
        for (Instruction *inst : fn->pointer_instructions) {
            switch (inst->opcode) {
            case GEP:
            case BITCAST:
                lat_set(lat, inst, lat_get(lat, inst->operand[0]));
                break;
            case PHI:
            case SELECT:
                lat_set(lat, inst, lat_meet_all_incoming(lat, inst));
                break;
            case ADDR_SPACE_CAST:
                lat_set(lat, inst, inst->target_as);   /* force, do not inherit */
                break;
            case CALL:
                propagate_call_args(lat, inst);        /* backward into caller */
                propagate_call_ret(lat, inst);         /* forward from callee */
                break;
            case WMMA:
            case CP_ASYNC_BULK:
                lat_refine_backward(lat, inst->pointer_operand, AS_GLOBAL);
                break;
            }
        }
    }
}
```

The seeds come from kernel-argument attributes: pointers tagged with the kernel-pointer attribute start at GLOBAL, grid-constant arguments start at CONSTANT, and byval struct arguments start at GENERIC because they cross a true generic boundary at the launch site. Backward refinement at WMMA and async-bulk sites adds the only non-monotone edge — WMMA forces GLOBAL on the operand chain because the hardware does not implement WMMA against any other space.

The full data-flow algorithm, the four red-black trees that hold per-block lattice state, the `"nvvm.as"` attribute that publishes results across passes, and the clone-budget that bounds inter-procedural specialization all live in [AddrSpace Vote Lattice](addrspace-vote-lattice.md).

## Data Flow Examples

The three examples below trace one pointer per pattern from the kernel-launch boundary to its operand-residency destination. Each example names the address-space transitions and identifies which `cvta` conversions survive into PTX versus which the optimizer eliminates.

### Example 1: Kernel Parameter to SMEM Stage

A typical Hopper TMA mainloop loads a GMEM tile into SMEM through `cp.async.bulk.tensor`. The kernel parameter starts in parameter memory and reaches SMEM through one address-space transition and one async copy:

```ptx
.param u64    A_ptr;                                     // PMEM (LLVM addrspace(101))
.shared.align 1024 .b8 A_smem[16384];                    // SMEM (LLVM addrspace(3))

ld.param.u64  %rd1, [A_ptr];                             // PMEM -> register (no AS change)
cvta.to.global.u64 %rd2, %rd1;                           // register -> GMEM-tagged pointer
mov.u64       %rd3, A_smem;                              // SMEM base address as a register

cp.async.bulk.tensor.2d.shared::cluster.global
              [%rd3], [%tma_desc, {%coord_m, %coord_n}], [%mbar];
```

Three transitions appear in the source IR but only one survives into PTX. The `ld.param` instruction is not an address-space cast — it is a load from `.param` into a register, and the register has no address space. The `cvta.to.global` is the real conversion: it tells the hardware that the address now refers to global memory, and ptxas emits it as a real instruction unless MemorySpaceOpt proved that the pointer was always global (in which case the cast folds to a no-op). The `cp.async.bulk.tensor` instruction names both source and destination spaces in its mnemonic; no further cast is needed.

The mbarrier object that completes the copy lives in SMEM and the TMA descriptor lives in GMEM. Both pointers are name-only operands to the `cp.async.bulk.tensor` instruction; the hardware tracks their spaces from the mnemonic, not from any pointer type carried into the instruction.

### Example 2: WGMMA Operand Staging

A Hopper WGMMA mainloop reads operand A from SMEM (or optionally from registers) and operand B from SMEM, and accumulates into a register-resident fragment. The end-to-end staging pattern is GMEM → SMEM → WGMMA → register → SMEM → GMEM:

```text
                    +---------+    cp.async.bulk.tensor    +---------+
A in GMEM (AS=1) ---|         |---------------------------->|         |
                    |  TMA    |                             | A SMEM  |
B in GMEM (AS=1) ---|  desc   |---------------------------->| B SMEM  |
                    +---------+                             | (AS=3)  |
                                                            +----+----+
                                                                 |
                                          SMEM descriptor (64-bit packed)
                                                                 |
                                                                 v
                                                       +---------+--------+
                                                       | wgmma.mma_async  |
                                                       |  (consumes B as  |
                                                       |   SMEM descrip-  |
                                                       |   tor, A as desc |
                                                       |   or RF)         |
                                                       +---------+--------+
                                                                 |
                                                       accumulator in RF
                                                                 |
                                                                 v
                                                       +---------+--------+
                                                       | wgmma.wait_group |
                                                       +---------+--------+
                                                                 |
                                                                 v
                                                          stmatrix to SMEM
                                                                 |
                                                                 v
                                                       st.global to GMEM
```

The accumulator lives in registers for the whole mainloop. SMEM operands are referenced through a 64-bit descriptor — a packed address-plus-layout immediate, not a pointer — that the operand-builder constructs once per tile and threads through the `mma_async` as an `l`-constraint i64. See [WGMMA Emission Protocol](wgmma-emission-protocol.md) for the descriptor bit layout and the fence/commit/wait sequence that orders the async MMA against subsequent reads.

The address-space transitions in this pattern are entirely between GMEM and SMEM. The descriptor construction is a pure arithmetic operation on a SMEM base offset — no `cvta` is involved. The `stmatrix` and `st.global` instructions at the epilogue name their spaces lexically, so the only generic pointer that could survive is the GMEM output base, which the kernel-pointer attribute pins to GLOBAL from the seed.

### Example 3: tcgen05 With TMEM Allocation

The Blackwell pattern adds tensor memory between SMEM and the MMA. The accumulator moves from registers to TMEM; operand A optionally moves from registers to TMEM for weight-stationary chains; operand B stays in SMEM (the WGMMA descriptor format is preserved). The full staging pattern is GMEM → SMEM → TMEM → tcgen05.mma → TMEM → SMEM → GMEM:

```text
                    +---------+    cp.async.bulk.tensor    +---------+
A in GMEM (AS=1) ---|  TMA    |---------------------------->|  SMEM   |
B in GMEM (AS=1) ---|  desc   |---------------------------->| (AS=3)  |
                    +---------+                             +----+----+
                                                                 |
                                              tcgen05.cp (SMEM -> TMEM)
                                                                 v
                              +-----------------------+    +----------+
                              | TMEM accumulator      |    |  TMEM A  |
                              | (allocated via        |    | (AS=6,   |
                              |  tcgen05.alloc.shared)|    | weight-  |
                              | (AS=6, 128-row grid)  |    | stationary)|
                              +-----+-----------------+    +-----+----+
                                    |                            |
                                    |       B SMEM descriptor    |
                                    |                            |
                                    +----- tcgen05.mma ----------+
                                                |
                                                v
                                         TMEM accumulator
                                                |
                                          tcgen05.st to RF
                                                |
                                                v
                                          stmatrix to SMEM
                                                |
                                                v
                                          st.global to GMEM
```

The TMEM allocator op `nvvm.tcgen05.alloc` returns an `addrspace(6)` handle; every subsequent `tcgen05.mma` op consumes the handle as a 32-bit base address plus row/column descriptor. The handle lifetime is scoped to the enclosing dialect operation — there is no way to pass a TMEM handle out of the function it was allocated in, and the allocator op must dominate every MMA op that uses the handle. See [tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) for the allocator contract and the variant taxonomy.

The cooperative 2-CTA MMA variant shares TMEM across two CTAs in a cluster: CTA 0 holds rows `[0..M/2)` and CTA 1 holds rows `[M/2..M)`. The two halves never exchange data through TMEM directly — the only inter-CTA path on the data side is through DSMEM (distributed shared memory, `addrspace(7)`) via the `nvvm.mapa` address translation. See [Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) for the rendezvous protocol.

## Address-Space Transition Table

The hardware implements a fixed set of address-space conversions through the `cvta` instruction family. The legal conversions form an asymmetric matrix: every concrete space can be converted to generic, but the reverse direction is conditional on the runtime address actually naming the target space. A `cvta.to.global` on a pointer that points into shared memory is undefined behaviour at runtime; the compiler issues it only when the lattice has proved the pointer is global, or when the user has explicitly requested it through an intrinsic.

| From | To | Instruction | Always legal? | Notes |
|---|---|---|---|---|
| GMEM | Generic | `cvta.global` | yes | Identity in PTX; the high bits already name global |
| SMEM | Generic | `cvta.shared` | yes | Sets the SMEM marker in the high bits of the address |
| CMEM | Generic | `cvta.const` | yes | Same as SMEM, with the constant marker |
| LMEM | Generic | `cvta.local` | yes | Per-thread stack window |
| PMEM | Generic | implicit via `ld.param` | yes | The PMEM-to-generic conversion happens inside `ld.param` |
| TMEM | Generic | (no such cast) | no | TMEM has no generic representation; only `tcgen05` reads it |
| DSMEM | Generic | `cvta.shared` after `nvvm.mapa` | yes | The mapa translation produces an SMEM-tagged pointer first |
| Generic | GMEM | `cvta.to.global` | conditional | UB if the pointer does not name global memory |
| Generic | SMEM | `cvta.to.shared` | conditional | UB if not shared |
| Generic | CMEM | `cvta.to.const` | conditional | UB if not constant |
| Generic | LMEM | `cvta.to.local` | conditional | UB if not local |
| Generic | TMEM | (no such cast) | no | TMEM is unreachable from generic |
| Generic | PMEM | (no such cast) | no | PMEM is only readable through `ld.param` |
| GMEM | SMEM | (none) | no | Must go through generic; usually a bug if it appears |
| SMEM | GMEM | (none) | no | Same as above |
| SMEM | DSMEM | `nvvm.mapa` | conditional | Requires a multi-CTA cluster and a valid peer rank |

The conditional conversions are the source of every `assuming global memory space` warning the compiler emits. When the lattice fails to prove a generic pointer's concrete space, the rewriter assumes GLOBAL and emits a warning; the backend then issues a `cvta.to.global` that is correct only if the pointer was in fact global at runtime. The warning is the user's signal to either add a kernel-pointer attribute, replace a pointer-of-pointer indirection with a direct argument, or restructure the kernel to avoid the generic boundary entirely.

## What the Compiler Enforces

Address-space rules are enforced at three levels: the dialect verifier, the LLVM verifier, and the PTX assembler.

Pointer arithmetic stays within one address space. A GEP, bitcast, or PHI cannot change a pointer's address space; only `addrspacecast` (LLVM) or `cvta` (PTX) can. The lattice propagator relies on this: every non-cast pointer operation inherits its address-space tag from its operand, so a single seed propagates to the whole def-use tree. A kernel that mixes a GEP with a different-space operand fails the LLVM verifier with a type mismatch.

TMA descriptor pointers must be GMEM. The descriptor itself is a packed 128-byte structure that names the multi-dimensional tile layout, and the hardware reads it through a global-memory load before issuing the async copy. The verifier rejects any `cp.async.bulk.tensor` op whose descriptor operand is not GMEM-tagged.

SMEM allocations are statically sized. The kernel declares its shared-memory footprint at compile time through a `.shared` directive in PTX; the launch site passes the size as a kernel-launch parameter. There is no dynamic allocation, no `malloc` in SMEM, no growth at runtime. A kernel that needs to grow its SMEM footprint must be relaunched with a larger size, and the launch is constrained by the SM's total SMEM capacity of 228 KB.

TMEM allocations are scope-bound to a specific dialect operation. The allocator returns a handle that is an SSA value; the matching deallocator must dominate every use of the handle and be dominated by the allocator. The dialect does not allow TMEM regions to outlive their enclosing scope, and the kernel cannot pass a TMEM handle through a function call. The constraint is structural — TMEM does not survive the SM reset that occurs between CTAs scheduled on the same SM, so a function-call-crossing handle would dangle.

Local-memory atomics are rejected. The PTX architecture does not support atomic operations on `.local`, and the lattice walker turns a backward-inferred LOCAL tag at an atomic site into a hard error rather than letting the backend emit an unsupported instruction. The diagnostic is `Cannot do atomic on local memory`; the fix is almost always to move the atomic target to `.shared` or `.global`.

Param-memory pointers do not escape. The compiler turns every `addrspace(101)` load into a direct `ld.param` instruction at the function entry, and the resulting register has no address space. A pointer that survives as `addrspace(101)` past the LowerArgs pass is a bug — the parameter memory is only readable inside the kernel, and any function call that takes a parameter pointer must already have been inlined.

## Cross-References

[AddrSpace Vote Lattice](addrspace-vote-lattice.md) is the canonical reference for the inter-procedural inference algorithm: the lattice, the four red-black trees, the clone-budget, and the `"nvvm.as"` attribute that publishes results across passes.

[MemorySpaceOpt and process-restrict](../nvptx-passes/memory-space-opt-and-process-restrict.md) is the LLVM-IR pass that runs the inference and rewrites generic pointers. It covers the walker, the addrspacecast folder, the WMMA backward constraint, and the diagnostic catalog.

[tcgen05 Tensor Memory Model](tcgen05-tensor-memory-model.md) is the canonical reference for tensor memory: the allocator, the operand-residency table, the variant taxonomy, and the collector cache.

[Cluster Sync and DSMEM Handshake](cluster-sync-and-dsmem-handshake.md) covers distributed shared memory: the `nvvm.mapa` translation, the cluster-barrier rendezvous, and the transaction-byte handshake that pairs an inter-CTA copy with its consumer.

[WGMMA Emission Protocol](wgmma-emission-protocol.md) covers the Hopper async MMA: the fence/commit/wait protocol, the 64-bit SMEM descriptor, and the accumulator-lifetime contract.

[Lower-Args, Aggr, Struct](../nvptx-passes/lower-args-and-aggr-and-struct.md) covers parameter-memory lowering: how byval struct arguments become `addrspace(101)` pointers, how the LowerArgs pass converts them to direct loads, and the launch-argument check that validates the parameter footprint.

[NVPTX Backend Passes Overview](../nvptx-passes/overview.md) places the memory-space passes in the wider backend cluster.
