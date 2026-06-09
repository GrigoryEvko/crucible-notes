# CoroSplit & CoroFrame: Coroutine Lowering on GPU

cicc v13.0 carries the complete LLVM coroutine lowering pipeline — CoroEarly, CoroSplit, CoroElide, CoroAnnotationElide, and CoroCleanup — largely unchanged from upstream LLVM 19. The pass infrastructure processes C++20 `co_await`/`co_yield`/`co_return` coroutines emitted by the EDG 6.6 frontend, splitting a single coroutine function into separate resume, destroy, and cleanup functions while computing a coroutine frame struct to carry live state across suspend points. NVIDIA adds one proprietary intrinsic (`llvm.nvvm.coro.create.suspend`) and emits a `.pragma "coroutine"` annotation in PTX, but the core splitting and frame layout algorithms are stock LLVM. The practical constraint is that coroutine frame allocation on GPU defaults to `malloc` in device heap — extremely expensive on current architectures — making CoroElide (which replaces heap allocation with a caller-stack alloca) the pass that determines whether GPU coroutines are viable or pathological.

## Key Facts

| Property | Value |
|---|---|
| CoroSplit pass entry | `sub_24EF980` (11 KB native, address range `0x24EF980`--`0x24F2300`) |
| CoroFrame layout computation | `sub_24F6730` (11,249 bytes native, stack frame 5,624 bytes) |
| Core frame layout workhorse | `sub_24F5860` (called from CoroFrame) |
| createResumeFunction | `sub_2284030` |
| createDestroyFunction | `sub_2284040` |
| CoroEarly pass | `sub_24DCD10` (7 KB native) |
| CoroElide pass | `sub_24DF350` (11 KB native) |
| CoroAnnotationElide pass | `sub_24E2340` (6 KB native) |
| CoroSplit Cloner/Driver | `sub_25CA370` (11 KB native) |
| CoroFrame Materializer | `sub_25C5C80` (9 KB native, heap-to-stack frame layout) |
| CoroFrame Spill Analysis | `sub_25C1030` (5 KB native) |
| Pass name / debug type | `"CoroSplit"` / `"coro-split"` (at `0x4388A37` / `0x4387AC3`) |
| Coroutine metadata table | `unk_4F8FAE8` |
| Pipeline parser ID | #156 (CGSCC pass, param: `reuse-storage`) |
| CoroElide pipeline ID | #220 (Function pass) |
| CoroAnnotationElide pipeline ID | #155 (CGSCC pass) |
| CoroEarly pipeline ID | #29 (Module pass) |
| CoroCleanup pipeline ID | #28 (Module pass) |
| NVIDIA intrinsic | `llvm.nvvm.coro.create.suspend` (single constant integer argument) |
| PTX annotation | `.pragma "coroutine";` |

## The Coroutine Lowering Pipeline

Five passes run in a fixed sequence across the optimizer pipeline. The first and last are module-level bookends; the middle three do the real work inside the CGSCC (Call Graph SCC) pipeline where inlining decisions interact with coroutine splitting.

```text
CoroEarly (module)         Lowers coroutine setup intrinsics.
                           Materializes the NoopCoro.Frame global.
                           Replaces llvm.coro.resume, llvm.coro.destroy,
                           llvm.coro.promise, llvm.coro.free with
                           concrete operations on the frame pointer.
        |
        v
CoroSplit (CGSCC)          Identifies coroutine functions by scanning for
                           llvm.coro.suspend / llvm.coro.end intrinsics.
                           Invokes CoroFrame to compute the frame layout.
                           Clones the function into resume + destroy variants.
                           Builds the state machine dispatch switch.
        |
        v
CoroAnnotationElide (CGSCC) Annotation-driven elision: when the callee is
                           marked "elide_safe_attr" and the call site has
                           ".noalloc", converts heap alloc to alloca in the
                           caller's frame. New in LLVM 19 / cicc v13.0.
        |
        v
CoroElide (function)       Classic elision: proves the coroutine frame
                           lifetime is bounded by the caller, replaces
                           coro.alloc with alloca. Emits optimization
                           remarks "'<name>' elided in '<caller>'" or
                           "'<name>' not elided in '<caller>'".
        |
        v
CoroCleanup (module)       Removes remaining coroutine intrinsic stubs
                           that survived lowering (e.g., coro.subfn.addr).
                           Final cleanup pass -- no coroutine intrinsics
                           survive past this point.
```

The `coro-cond` module analysis (registered in the pipeline parser at `sub_2337E30`) gates whether the coroutine passes activate at all. If no function in the module contains `llvm.coro.id`, the entire pipeline is skipped. This zero-cost guard is important because the vast majority of CUDA kernels contain no coroutines.

### CoroSplit as a CGSCC Pass

CoroSplit is registered as CGSCC pass #156 with an optional `reuse-storage` parameter. When `reuse-storage` is active, the pass attempts to reuse the storage of coroutine frames that are provably dead — relevant for generators where the frame is allocated once and resumed many times. In the CGSCC context, CoroSplit runs alongside the inliner (`inline`) and `function-attrs`, allowing newly split resume/destroy functions to be immediately considered for inlining into callers within the same SCC.

## CoroSplit: Suspend Point Detection and Function Splitting

### Detection Phase

`sub_24EF980` iterates over every function in the module. For each function, it scans all instructions using a bitmask-based opcode test to identify coroutine suspension intrinsics:

```c
// Suspend point detection (at 0x24F00E6)
// Stack frame: 0x860+ bytes, callee-saved: r15, r14, r13, r12, rbx
// Key locals:
//   [rbp-0x7F8] = outer iteration end pointer
//   [rbp-0x7E8] = current coroutine info
//   [rbp-0x7E0] = suspend point instruction
//   [rbp-0x740] = original coroutine function
//   [rbp-0x750] = resume function pointer
//   [rbp-0x748] = destroy function pointer

uint8_t opcode = inst->getOpcode();
unsigned normalized = opcode - 0x22;
if (normalized > 51) continue;  // not in range [0x22, 0x55]

uint64_t mask = 0x8000000000041ULL;
if (!((mask >> normalized) & 1)) continue;  // bit not set
```

The bitmask `0x8000000000041` encodes three intrinsic opcodes:

| Bit position | Opcode | Intrinsic |
|-------------|--------|-----------|
| 0 | `0x22` | `llvm.coro.suspend` — normal suspend point |
| 6 | `0x28` | `llvm.coro.suspend.retcon` — returned-continuation suspend |
| 51 | `0x55` | `llvm.coro.end` — coroutine termination |

This single 64-bit `bt` (bit-test) instruction replaces what would otherwise be a three-way comparison or switch, a pattern upstream LLVM uses in its `Intrinsic::ID` checking.

### Validation

After finding a suspend point, CoroSplit validates the coroutine structure (at `0x24F010E`):

```c
// Coroutine validation pseudocode (0x24F010E-0x24F0179)
Value *coro_id_inst = ...;
if (coro_id_inst->getOpcode() != 0x55)    // must be 'U' = coro.id
    goto skip;

Function *parent = coro_id_inst->getParent();  // [rax-20h]
if (!parent || parent->getOpcode() != 0)        // entry block check
    goto skip;

Value *promise = coro_id_inst->getOperand(4);   // [rcx+50h]
if (parent->getContext() != promise)             // [rax+18h] == promise
    goto skip;

if (!(parent->getFlags() & 0x20))               // "has personality" bit 5 of +0x21
    goto skip;

if (parent->getIntrinsicID() != 59)             // 0x3B = coro.id
    goto skip;
```

This is a thorough validation ensuring:

1. The instruction is indeed `llvm.coro.id` (opcode `0x55` = 'U', intrinsic ID 59 = `0x3B`)
2. It belongs to a valid function (parent pointer non-null, starts with opcode 0)
3. The promise alloca matches between `coro.id` and function context
4. The function has the correct personality (bit 5 of byte at offset `+0x21`)
5. The intrinsic ID equals 59 (`cmp dword [rax+24h], 0x3B`)

Nested coroutines receive additional validation (at `0x24F017F`): the pass checks that `coro.begin` (opcode range `0x1E`--`0x28`, ID 57 = `0x39`) references the correct parent function, preventing cross-coroutine confusion when one coroutine is nested inside another.

```c
// Nested coroutine check (0x24F017F-0x24F01D6)
unsigned operand_count = inst->getNumOperands() & 0x7FFFFFF;  // mask out type bits
Value *parent_ref = inst->getOperand(-operand_count);         // computed offset
if (parent_ref != current_function)
    goto skip;  // different coroutine -- do not cross wires

uint8_t begin_opcode = begin_inst->getOpcode();
if (begin_opcode - 0x1E > 0x0A)   // must be in [0x1E, 0x28]
    goto skip;  // not a coro.begin-related instruction

Value *frame_ptr = begin_inst->getOperand(2);  // [rdx+28h]
```

### Suspend Point Collection

Validated suspend points are collected into a deduplicated array. The dedup check at `0x24F02F9` scans existing entries, following def-use chains (`[rbx+10h]`) to avoid processing the same suspend point twice when multiple CFG paths reach it. For each suspend point, the pass extracts the value operand at instruction offset `+0x28`.

```c
// Suspend point collection with dedup (0x24F02F9-0x24F040A)
unsigned count = suspend_array_size;
for (unsigned i = 0; i < count; i++) {
    if (suspend_array[i] == new_suspend)
        goto already_collected;  // follow chain: [rbx+10h]
}
// Extract value operand:
Value *value_operand = suspend_inst->getOperand(2);  // [rdx+28h]
suspend_array[count++] = new_suspend;
```

### The Split Algorithm

After collecting all suspend points, the split proceeds in three phases:

**Phase 1: Frame layout computation.** CoroSplit invokes `sub_24F6730` (CoroFrame) to determine which SSA values are live across suspend points and must be stored in the frame struct (see the CoroFrame section below).

**Phase 2: Function cloning and specialization.** The split mode field at `[rbp-0x3F8]` controls which function variants are created:

```c
// Function splitting dispatch (at 0x24F0540)
int split_mode = frame_state->split_mode;  // [rbp-0x3F8]

if (split_mode == 0) {
    // Returned-continuation style: destroy function only
    Function *destroy = createDestroyFunction(state, orig_fn, suspends, ...);
} else if (split_mode >= 1 && split_mode <= 3) {
    // Standard C++20 coroutine: both resume and destroy
    Function *resume  = sub_2284030(state, orig_fn, suspends, coro_info,
                                     destroy_data, resume_data);
    Function *destroy = sub_2284040(state, orig_fn, suspends, coro_info,
                                     destroy_data, resume_data);
}
```

`sub_2284030` (createResumeFunction) and `sub_2284040` (createDestroyFunction) each:

1. Clone the original coroutine function via `sub_D2E510` (function cloner)
2. Replace the coroutine frame parameter with a typed pointer to the frame struct
3. Insert a switch statement at the entry block dispatching on the suspend index stored in the frame (`__coro_index`)
4. Replace each `llvm.coro.suspend` with a return instruction
5. Wire function pointers (`__resume_fn`, `__destroy_fn`) into the frame header at offsets `+0x00` and `+0x08`

**Phase 3: Metadata and remark emission.** After splitting, the pass registers the new functions in the coroutine metadata table at `unk_4F8FAE8` via `sub_BC1CD0`, then emits an optimization remark:

```c
// Remark emission (0x24F05D1-0x24F06E8)
sub_B17560(remark, "CoroSplit", "coro-split");  // create remark
sub_B18290(remark, "Split '");                  // prefix
sub_BD5D20(orig_fn, name_buf);                  // get function name
sub_B16430(remark, "function", name_buf);       // named attribute
sub_B18290(remark, "' (frame_size=");
sub_B16B10(remark, "frame_size", frame_size);   // integer attribute
sub_B18290(remark, ", align=");
unsigned align = 1u << alignment_log2;
sub_B16B10(remark, "align", align);
sub_B18290(remark, ")");
sub_1049740(remark);                            // publish to diagnostic handler
```

The format is: `Split '<function_name>' (frame_size=N, align=M)` where `N` is the computed frame size in bytes and `M` is `1 << alignment_log2`.

### The `.corodispatch` Trampoline

The CoroSplit dispatcher at `sub_3160A60` (9 KB native, second code cluster) generates a `.corodispatch` function — a lightweight trampoline that:

1. Loads `__coro_index` from the coroutine frame at offset `+0x10`
2. Switches on the index value to select the correct resume point
3. Uses `musttail` call semantics to jump to the target without growing the stack

The string `"MustTailCall.Before.CoroEnd"` confirms it enforces musttail on the final resume-to-end transition. Additional strings in this function include `".from."` (used to construct the dispatch label name), `"CoroEnd"`, `"CoroSave"`, and `"CoroSuspend"` (marking the IR structures being dispatched through).

For GPU targets, the musttail semantics are critical: stack space is per-thread local memory, and growing it across coroutine bounces would rapidly exhaust the limited local memory budget.

## CoroFrame: Frame Layout Computation

`sub_24F6730` is the largest and most complex function in the coroutine pipeline, with a 5,624-byte stack frame (`0x15F8`) — one of the largest in the entire cicc binary. Its job: determine which SSA values are live across suspend points and must be "spilled" into the coroutine frame struct.

### Algorithm Overview

The algorithm is a BFS-based cross-suspend-point liveness analysis:

1. **Initialize tracking structures.** Two hash tables with 16-byte entries, sentinel `0xFFFFFFFFF000`, hash function `(val >> 4) ^ (val >> 9)`. Initial capacity 8 entries each.

2. **Iterate all instructions.** Walk every basic block and instruction. A visitor callback (`[visitor+18h]`, virtual call) classifies each instruction as relevant or not to the frame computation.

3. **BFS traversal.** A deque with 512-byte blocks (64 pointer-sized entries per block) drives BFS over the CFG. The core computation at `sub_24F5860` determines which values cross which suspend points.

4. **Spill set computation.** Values that are defined before a suspend point and used after it must be stored in the frame. The result is a set of (value, suspend_point) pairs.

5. **Frame layout.** The frame type builder (at `sub_3169200` in the second code cluster) arranges spill slots into a struct.

### Frame Struct Layout

The coroutine frame is a flat C struct with a fixed header followed by computed spill slots:

```c
struct __coro_frame {                              // type name: ".coro_frame_ty"
    void (*__resume_fn)(struct __coro_frame *);    // +0x00  resume function pointer
    void (*__destroy_fn)(struct __coro_frame *);   // +0x08  destroy function pointer
    uint32_t __coro_index;                         // +0x10  suspend point state variable
    // --- header ends, spill slots begin ---
    // padding for alignment (computed per-coroutine)
    // spill slots ordered by descending alignment requirement
    // promise storage (if promise_type is non-trivial)
    // alloca copies (stack variables that survive suspend)
};
```

The frame variable is named `"__coro_frame"` and the type is `".coro_frame_ty"`. The suspend point index field `"__coro_index"` is the state variable for the resume switch dispatch: value 0 means "initial entry", value N means "resumed at suspend point N", and a poison/unreachable value means "coroutine has returned".

The frame type builder at `sub_3169200` (10 KB native) constructs the `StructType` using these rules:

1. The two function pointers (`__resume_fn`, `__destroy_fn`) always occupy the first 16 bytes
2. `__coro_index` occupies bytes 16–19 (i32)
3. Remaining spill slots are sorted by alignment (largest first) to minimize padding
4. The promise alloca (if present) is placed at a known offset so `llvm.coro.promise` can compute it
5. Total frame size and alignment are recorded for the split remark

### Spill/Reload Code Generation

The spill/reload generator at `sub_31650D0` (9 KB native) creates the actual load/store instructions that move values between SSA registers and the coroutine frame:

- A basic block named `"AllocaSpillBB"` is inserted at the function entry. All alloca instructions that need to survive across suspend points are moved here and replaced with GEP+store into the frame.
- A basic block named `"PostSpill"` follows, branching to the original entry logic.
- At each suspend point, `".spill.addr"` store instructions write live SSA values into their frame slots.
- After each resume point, `".reload"` load instructions fetch values back from frame slots into fresh SSA values.

The naming convention (`.spill.addr`, `.reload`) is important for debugging: these instructions appear in `-print-after-all` dumps and identify coroutine frame traffic distinctly from normal loads/stores.

### Detailed BFS Liveness Algorithm

```c
// Pseudocode for sub_24F5860 core frame computation

void computeFrameLayout(Function *F, SmallVector<SuspendPoint> &suspends) {
    // Step 1: Build definition map
    DenseMap<Value*, uint32_t> def_map;     // sentinel 0xFFFFFFFFF000
    DenseMap<Value*, uint32_t> cross_map;   // sentinel 0xFFFFFFFFF000

    // Step 2: Walk all basic blocks, identify definitions
    for (BasicBlock &BB : *F) {
        for (Instruction &I : BB) {
            if (visitor->isRelevant(&I))    // virtual call [visitor+18h]
                def_map.insert(&I, generation++);
        }
    }

    // Step 3: For each suspend point, BFS forward to find uses
    Deque<BasicBlock*> worklist;  // 512-byte blocks, 64 entries each
    for (SuspendPoint &SP : suspends) {
        worklist.clear();
        worklist.push_back(SP.getParent());

        while (!worklist.empty()) {
            BasicBlock *BB = worklist.pop_front();
            for (Instruction &I : *BB) {
                for (Value *Op : I.operands()) {
                    if (def_map.count(Op) && def_before_suspend(Op, SP)) {
                        // This value is defined before SP and used after it
                        cross_map.insert({Op, SP.getIndex()});
                        spill_set.add(Op);
                    }
                }
            }
            for (BasicBlock *Succ : successors(BB))
                worklist.push_back(Succ);
        }
    }

    // Step 4: Build frame struct from spill set
    // Sort spill slots by alignment (descending) then by size
    // Compute offsets, padding, total frame size
}
```

The complexity is O(instructions * suspend_points) per coroutine for the liveness phase, O(V+E) for each BFS where V = basic blocks and E = CFG edges.

### Data Structures

**Frame info** (0x138 = 312 bytes, allocated via `sub_22077B0`):

| Offset | Size | Description |
|--------|------|-------------|
| `+0x00` | 8 | Spill array pointer |
| `+0x08` | 8 | Reserved (initially 0) |
| `+0x10` | 8 | Reference count (initially 1) |
| `+0x18`--`+0x98` | 128 | Embedded hash table for spill tracking (16-byte stride, sentinel-filled) |
| `+0x98` | 8 | Pointer to inner table (self-referential) |
| `+0xA0` | 8 | Capacity encoding (`0x800000000`) |
| `+0x128` | 8 | Back-reference to visitor context |
| `+0x130` | 8 | Back-reference to suspend point array |

**Spill entry** (0x48 = 72 bytes):

| Offset | Size | Description |
|--------|------|-------------|
| `+0x00` | 8 | Coroutine function pointer |
| `+0x08` | 8 | Buffer pointer (inline or heap) |
| `+0x10` | 8 | Capacity encoding (6 entries inline) |
| `+0x18`--`+0x48` | 48 | Inline buffer for small spill sets |

The inline buffer holds up to 6 spill entries without heap allocation. When exceeded, the buffer externalizes to the heap; cleanup at `0x24F6CB0` checks `[entry+8]` against `[entry+18h]` to determine if `free()` is needed.

**BFS deque:**

| Parameter | Value |
|-----------|-------|
| Block map allocation | 0x40 bytes (8 pointers) |
| Data block allocation | 0x200 bytes (512 bytes = 64 pointer entries) |
| Block pointers | `[rbp-0x340]`=front, `[rbp-0x338]`=count(8), `[rbp-0x330]`=begin |

### Hash Table Policy

Both hash tables in CoroFrame share identical parameters (see [hash-infrastructure.md](../infra/hash-infrastructure.md) for the universal pattern):

- **Hash function:** `(val >> 4) ^ (val >> 9)` — same hash used throughout cicc
- **Entry size:** 16 bytes (8-byte key + 8-byte metadata)
- **Empty sentinel:** `0xFFFFFFFFF000`
- **Load factor threshold:** 75% (triggers growth when `count * 4 >= capacity * 3`)
- **Tombstone cleanup:** 12.5% (rehash when `tombstones > capacity >> 3`)
- **Growth factor:** 2x (capacity doubles on each growth)
- **Collision resolution:** linear probing

## GPU-Specific Constraints: The Heap Allocation Problem

### Why Device Malloc Is Pathological

Standard LLVM coroutines allocate the frame on the heap via `operator new` (or a custom allocator returned by `get_return_object_on_allocation_failure`). On GPU, this calls into the device-side `malloc`, which has severe limitations:

**Fixed-size heap.** The device heap is controlled by `cudaLimitMallocHeapSize` (default 8 MB across the entire GPU). A kernel launching 65,536 threads, each with a 256-byte coroutine frame, requires 16 MB of heap — already exceeding the default. Increasing the limit helps, but the heap must be pre-allocated before kernel launch, wasting memory for non-coroutine workloads.

**Serialized allocation.** Device `malloc` implementation uses a global free list protected by atomics. Within a warp, threads attempting simultaneous allocation serialize on this atomic. Across warps on the same SM, L2 cache line bouncing on the free-list head pointer creates further contention. Under heavy allocation pressure (hundreds of concurrent warps), the effective throughput of device `malloc` can drop to single-digit allocations per microsecond — three orders of magnitude slower than a register read.

**Fragmentation under concurrency.** Thousands of threads allocating and freeing small frames (64–512 bytes) rapidly fragment the device heap. The device allocator does not perform compaction. Once fragmented, even a heap with sufficient total free space may fail individual allocations, causing `malloc` to return `nullptr` and triggering coroutine allocation failure paths (if the user provided `get_return_object_on_allocation_failure`) or program termination.

**Memory latency hierarchy.** The cost difference between frame locations is dramatic:

| Location | Latency | Bandwidth per SM | Notes |
|----------|---------|------------------|-------|
| Registers | 0 cycles | N/A (direct) | Best case — values that don't cross suspends |
| Local memory (L1 hit) | ~28 cycles | ~12 TB/s | Stack alloca destination after CoroElide |
| Local memory (L1 miss, L2 hit) | ~200 cycles | ~3 TB/s | Large frames that spill L1 |
| Global memory (device heap) | ~400-800 cycles | ~1 TB/s | Default without CoroElide |
| Device malloc overhead | ~2000+ cycles | N/A | Free-list atomic contention |

The combined overhead of malloc latency + global memory access latency makes un-elided coroutines 50–100x slower than elided ones on GPU. This is the fundamental reason CoroElide is the most performance-critical coroutine optimization for GPU targets.

### CoroElide: The GPU Escape Analysis

`sub_24DF350` (11 KB native — the largest coroutine pass) implements the classic heap allocation elision. It runs as a function-level pass (#220 in the pipeline parser), meaning it analyzes each caller individually after CoroSplit has already split the coroutine.

#### Elision Preconditions

For each `llvm.coro.id` call site in the caller, CoroElide attempts to prove that:

1. **No handle escape.** The coroutine handle (pointer to `__coro_frame`) does not escape the caller's scope. Specifically, the handle is not stored to memory visible to other threads, not passed to functions that might store it, and not returned from the caller. On GPU, the "visible to other threads" criterion is complicated by shared memory (`addrspace(3)`) and generic address space (`addrspace(0)`) casts — a handle stored through a generic pointer could be visible to any thread.

2. **No external aliases.** No alias of the handle is created that could outlive the caller. This includes GEPs into the frame, bitcasts, and pointer arithmetic. The alias analysis at this stage uses the results from the function-level AA pipeline.

3. **Full consumption.** All suspend/resume/destroy calls on this coroutine handle are within the caller function. If the handle is passed to a helper function that calls `coroutine_handle::resume()`, the coroutine is not fully consumed from CoroElide's perspective (unless that helper was inlined first by the CGSCC inliner running in the same SCC iteration).

4. **Callee identity known.** The coroutine callee must be identifiable (not an indirect call through a function pointer). CoroElide needs to read the callee's frame size and alignment from the split remark metadata to size the alloca correctly.

#### The Elision Transformation

When all preconditions are satisfied, CoroElide performs this rewrite:

```c
// BEFORE elision (caller code):
%id    = call token @llvm.coro.id(i32 0, ptr null, ptr null, ptr null)
%need  = call i1 @llvm.coro.alloc(token %id)
br i1 %need, label %alloc, label %begin

alloc:
  %mem = call ptr @operator_new(i64 FRAME_SIZE)   ; <-- heap allocation
  br label %begin

begin:
  %phi = phi ptr [ %mem, %alloc ], [ null, %entry ]
  %hdl = call ptr @llvm.coro.begin(token %id, ptr %phi)
  ; ... use coroutine ...
  call void @llvm.coro.resume(ptr %hdl)
  call void @llvm.coro.destroy(ptr %hdl)

// AFTER elision:
%frame = alloca [FRAME_SIZE x i8], align FRAME_ALIGN  ; <-- stack allocation
%hdl   = call ptr @llvm.coro.begin(token %id, ptr %frame)
; ... use coroutine ...
call void @llvm.coro.resume(ptr %hdl)
; destroy is elided (frame on stack, automatically freed)
```

The key changes:

- `llvm.coro.alloc` is replaced with `false` (allocation not needed)
- The `operator new` call is deleted
- An `alloca` of the correct size and alignment is inserted in the caller's entry block
- The `coro.begin` now points at the stack alloca
- `llvm.coro.free` is replaced with a no-op (stack memory does not need explicit deallocation)
- The destroy function call may be simplified since stack deallocation is automatic

On NVPTX, the `alloca` maps to per-thread local memory (address space 5). Local memory accesses go through the L1 cache and are dramatically faster than device `malloc` followed by global memory access.

#### Elision Failure Modes on GPU

Several GPU-specific patterns defeat CoroElide:

1. **Generic address space cast.** If the coroutine handle is cast to `addrspace(0)` (generic), the compiler cannot prove it stays in local memory. Generic pointers are indistinguishable from shared or global pointers at the IR level, so the escape analysis conservatively assumes the handle escapes.

2. **Coroutine handle in shared memory.** Storing the handle to `addrspace(3)` (shared memory) makes it visible to all threads in the CTA. Even if the programmer knows only one thread uses it, CoroElide cannot prove this.

3. **Cross-function resume.** A common pattern where the coroutine is created in one device function and resumed in another (e.g., a scheduler loop calling resume on handles from a queue). The handle passed as a function argument escapes the creator.

4. **Opaque allocator.** If the coroutine uses a custom allocator (via `promise_type::operator new`), CoroElide may not recognize the allocation/deallocation pattern.

#### Diagnostic Output

CoroElide emits remarks through the standard optimization remark infrastructure:

- **Success:** `'<coroutine_name>' elided in '<caller_name>'` (via `-Rpass=coro-elide`)
- **Failure:** `'<coroutine_name>' not elided in '<caller_name>'` (via `-Rpass-missed=coro-elide`)

For GPU developers, the failure remark is the most important diagnostic. An un-elided coroutine on GPU is a performance disaster. The recommended debugging workflow:

```bash
nvcc -Xptxas -v --compiler-options="-Rpass-missed=coro-elide" foo.cu
```

### CoroAnnotationElide: Developer-Asserted Elision

`sub_24E2340` (6 KB native) is the newer annotation-driven elision from LLVM 19. It looks for the `"elide_safe_attr"` function attribute and `".noalloc"` suffix on coroutine function names. When both are present, elision proceeds without the full escape analysis — the developer has asserted safety.

This is particularly useful for GPU code where the developer knows the coroutine is single-thread-scoped but the compiler cannot prove it due to pointer-to-generic-address-space casts. The `"caller_presplit"` attribute marks the caller as needing coroutine lowering, enabling the annotation elide pass to fire during the CGSCC iteration before the caller itself is split.

CoroAnnotationElide runs as CGSCC pass #155, meaning it fires before CoroSplit (#156) in the same CGSCC iteration. This ordering allows the annotation-based elision to rewrite allocation sites before CoroSplit performs the split, avoiding the need for a second pass.

### The `llvm.nvvm.coro.create.suspend` Intrinsic

This is the sole NVIDIA-proprietary coroutine intrinsic. The NVVM verifier enforces:

```text
llvm.nvvm.coro.create.suspend must have exactly one argument,
which must be a constant integer
```

The constant integer argument likely encodes a suspend-point identifier or mode. This intrinsic appears in the NVVM intrinsic table alongside `llvm.nvvm.stacksave` and `llvm.nvvm.stackrestore`, suggesting it interacts with the local memory stack for frame placement. Its exact lowering is handled by the NVVM-specific intrinsic lowering pass rather than the standard CoroSplit pipeline.

### PTX `.pragma "coroutine"`

The AsmPrinter (documented in [asmprinter.md](../infra/asmprinter.md)) optionally emits `.pragma "coroutine";` in the function header. This is triggered by metadata nodes with type byte `'N'` (0x4E) linked to the current function via the list at `this+792`. The pragma is the first thing emitted in the function prologue (step (a) in the PTX header emission sequence at `sub_215A3C0`), before even the `.entry`/`.func` keyword.

The pragma signals to `ptxas` that the function uses coroutine semantics, potentially affecting register allocation and scheduling decisions in the assembler. The exact `ptxas` behavior triggered by this pragma is not documented publicly, but it likely increases the local memory budget and adjusts the register allocation heuristics for the state-machine dispatch pattern.

### Warp Divergence at Suspend Points

A fundamental tension exists between SIMT execution and coroutine suspend. When one thread in a warp suspends while others do not, the warp diverges. The resume dispatch switch (the `__coro_index`-based state machine) creates a divergence point: threads may be at different suspend indices, requiring the hardware to serialize execution paths. This is identical to how any data-dependent branch causes divergence, but the impact is amplified because coroutine state machines typically have many switch cases (one per suspend point).

The StructurizeCFG pass (see [structurizecfg.md](../llvm/structurizecfg.md)) runs after coroutine lowering and will structurize the resume switch, potentially introducing additional control flow to manage reconvergence. On SM 70+ architectures with Independent Thread Scheduling, diverged threads can reconverge at any point, but the switch still introduces warp-level serialization proportional to the number of distinct `__coro_index` values active within the warp.

## The Second Code Cluster (0x3150000 Region)

The binary contains a second, independent cluster of coroutine functions, likely from a different compilation unit or LTO merge:

| Function | Address | Size |
|---|---|---|
| CoroFrame layout computation | `0x3171DA0` | 55 KB |
| CoroSplit splitting logic | `0x316D160` | 49 KB |
| CoroSplit dispatcher (`.corodispatch`, `MustTailCall.Before.CoroEnd`) | `0x3160A60` | 48 KB |
| Spill/reload generation (`AllocaSpillBB`, `PostSpill`, `.reload`, `.spill.addr`) | `0x31650D0` | 47 KB |
| Frame type builder (`__coro_frame`, `.coro_frame_ty`, `__coro_index`) | `0x3169200` | 46 KB |
| CoroElide heap allocation elision | `0x315A7B0` | 41 KB |
| Attributor analysis helper | `0x3150D70` | 43 KB |
| Attributor analysis helper | `0x314DBB0` | 40 KB |

These functions reference the same string literals and implement the same algorithms as the primary cluster. The primary cluster at `0x24D`--`0x25C` and this cluster at `0x314`--`0x317` are structurally identical — they differ only in binary address due to compilation unit or LTO merge ordering.

Additionally, three helper functions in the primary cluster's vicinity handle specialized aspects:

| Function | Address | Size |
|---|---|---|
| CoroSplit Cloner/Driver (calls CoroFrame helpers) | `sub_25CA370` | 55 KB |
| CoroFrame Materializer (heap-to-stack frame layout) | `sub_25C5C80` | 49 KB |
| CoroFrame Spill Analysis helper | `sub_25C1030` | 37 KB |

`sub_25C5C80` (CoroFrame Materializer) is particularly relevant: this is the function that actually rewrites the IR to replace heap allocation with stack-based frame placement after CoroElide has proven safety. It materializes the frame struct type, inserts the `alloca`, and rewires all frame access GEPs.

### Error Conditions in the Second Cluster

The CoroSplit implementation at `0x316D160` emits two diagnostic errors:

- `"Coroutines cannot handle non static allocas yet"` — triggered when a coroutine body contains a VLA (variable-length array) or `alloca()` with a dynamic size. The frame layout computation requires compile-time-known sizes for all frame slots. Dynamic allocas would require a separate heap allocation per suspend-resume cycle.

- `"alignment requirement of frame variables"` — triggered when a spill slot requires alignment exceeding the frame's maximum supported alignment. This can occur with over-aligned types (e.g., `alignas(256)` variables that must survive across suspends).

The CoroFrame at `0x3171DA0` emits:

- `"token definition separated from use by suspend point"` — a fatal error when an LLVM token value (which cannot be stored to memory) crosses a suspend boundary. Tokens are used for exception handling state and musttail call tracking; they are inherently non-materializable.

- `"Unable to handle alias with unknown offset before CoroBegin"` — triggered when a GEP with a non-constant offset operates on a value computed before `coro.begin`. The frame layout computation needs constant offsets to compute spill slot positions.

## EDG Frontend Support

The EDG 6.6 frontend fully implements C++20 coroutine semantics in two key functions:

- **`sub_87AFA0`** (3 KB native) — Coroutine body processor. Resolves `promise_type` methods: `initial_suspend`, `final_suspend`, `unhandled_exception`, `get_return_object`, `get_return_object_on_allocation_failure`. Generates the coroutine body scaffolding including the implicit try-catch around user code.

- **`sub_87BD00`** (1.4 KB native) — Coroutine trait resolver. Looks up `std::coroutine_traits<R, Args...>::promise_type`, `std::coroutine_handle`, `return_value`, `return_void`. The EDG IL walker maps these as IL node type 64 (`il_coroutine`), with expression sub-type `0x21` (`coroutine_expr`). The IL copier handles coroutine handles as entity type 72 (`coroutine_handle`).

The frontend does **not** restrict coroutines to host-side code. The EDG configuration sets `COROUTINE_ENABLING_POSSIBLE = 1` globally, meaning `__device__` functions can be coroutines. The full coroutine IR (with `llvm.coro.id`, `llvm.coro.begin`, `llvm.coro.suspend`, etc.) flows into the NVVM optimizer pipeline regardless of the function's execution space.

## Diagnostic Strings

| String | Location | Meaning |
|--------|----------|---------|
| `"Split '<name>' (frame_size=N, align=M)"` | CoroSplit remark | Successful coroutine split |
| `"' elided in '"` | CoroElide | Frame allocation replaced with alloca |
| `"' not elided in '"` | CoroElide | Elision failed, heap allocation remains |
| `"Coroutines cannot handle non static allocas yet"` | `0x316D160` | VLA or dynamic alloca inside coroutine body |
| `"alignment requirement of frame variables"` | `0x316D160` | Frame alignment constraint exceeded |
| `"token definition separated from use by suspend point"` | `0x3171DA0` | Token value crosses suspend boundary (error) |
| `"Unable to handle alias with unknown offset before CoroBegin"` | `0x3171DA0` | GEP with non-constant offset on pre-begin alias |
| `"llvm.nvvm.coro.create.suspend must have exactly one argument, which must be a constant integer"` | NVVM verifier | Malformed NVIDIA coroutine intrinsic |
| `"AllocaSpillBB"` | `0x31650D0` | Entry block for spill alloca instructions |
| `"PostSpill"` | `0x31650D0` | Block following spill setup |
| `".spill.addr"` | `0x31650D0` | Store to coroutine frame slot |
| `".reload"` | `0x31650D0` | Load from coroutine frame slot after resume |
| `".corodispatch"` | `0x3160A60` | Dispatch trampoline function name |
| `"MustTailCall.Before.CoroEnd"` | `0x3160A60` | Musttail semantics on final transition |
| `".from."` | `0x3160A60` | Dispatch label name construction |
| `"NoopCoro.Frame"` | `0x24DCD10` | Global no-op coroutine frame (CoroEarly) |
| `"caller_presplit"` | `0x24E2340` | Attribute marking pre-split caller |
| `"elide_safe_attr"` | `0x24E2340` | Attribute asserting elision safety |
| `".noalloc"` | `0x24E2340` | Function name suffix for annotation elide |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| CoroEarly pass entry | `sub_24DCD10` | 41 KB | — |
| CoroElide pass entry | `sub_24DF350` | 80 KB | — |
| CoroAnnotationElide pass entry | `sub_24E2340` | 33 KB | — |
| CoroSplit pass entry | `sub_24EF980` | 71 KB | — |
| Core frame layout computation | `sub_24F5860` | — | — |
| CoroFrame layout entry | `sub_24F6730` | 11 KB | — |
| CoroFrame Spill Analysis helper | `sub_25C1030` | 37 KB | — |
| CoroFrame Materializer (heap-to-stack) | `sub_25C5C80` | 49 KB | — |
| CoroSplit Cloner/Driver | `sub_25CA370` | 55 KB | — |
| createResumeFunction | `sub_2284030` | — | — |
| createDestroyFunction | `sub_2284040` | — | — |
| Function cloner (used for resume/destroy) | `sub_D2E510` | — | — |
| Frame-already-computed check | `sub_B2D610` | — | — |
| Get function name string | `sub_BD5D20` | — | — |
| Register in coroutine metadata table | `sub_BC1CD0` | — | — |
| Create optimization remark | `sub_B17560` | — | — |
| Publish remark to diagnostic handler | `sub_1049740` | — | — |
| Allocator (frame info, spill entries, BFS deque) | `sub_22077B0` | — | — |
| `coro-cond` module analysis checker | `sub_2337E30` | 15 KB | — |
| Attributor helper (coroutine attributes) | `sub_314DBB0` | 40 KB | — |
| Attributor helper (coroutine attributes) | `sub_3150D70` | 43 KB | — |
| CoroElide (second cluster) | `sub_315A7B0` | 41 KB | — |
| CoroSplit dispatcher (`.corodispatch`) | `sub_3160A60` | 48 KB | — |
| Spill/reload generation | `sub_31650D0` | 47 KB | — |
| Frame type builder | `sub_3169200` | 46 KB | — |
| CoroSplit splitting logic (second cluster) | `sub_316D160` | 49 KB | — |
| CoroFrame layout (second cluster) | `sub_3171DA0` | 55 KB | — |
| EDG coroutine body processor | `sub_87AFA0` | 14 KB | — |
| EDG coroutine trait resolver | `sub_87BD00` | 6 KB | — |

## Cross-References

- [Pipeline & Ordering](../llvm/pipeline.md) — where coroutine passes sit in the optimization sequence
- [SROA](../llvm/sroa.md) — SROA interacts with coroutine frame allocas; decomposes aggregate allocas into scalar SSA values
- [AsmPrinter & PTX Body Emission](../infra/asmprinter.md) — `.pragma "coroutine"` emission
- [Inliner Cost Model](../lto/inliner-cost.md) — inlining decisions for split resume/destroy functions
- [StructurizeCFG](../llvm/structurizecfg.md) — structurizes the resume dispatch switch
- [Hash Infrastructure](../infra/hash-infrastructure.md) — universal DenseMap pattern used by CoroFrame
- [Diagnostics & Optimization Remarks](../infra/diagnostics.md) — remark emission protocol
- [Address Spaces](../reference/address-spaces.md) — local (5), shared (3), generic (0) spaces relevant to elision
