# CoroSplit & CoroFrame: Coroutine Lowering on GPU

cicc v13.0 carries the complete LLVM coroutine lowering pipeline -- CoroEarly, CoroSplit, CoroElide, CoroAnnotationElide, and CoroCleanup -- largely unchanged from upstream LLVM 19. The pass infrastructure processes C++20 `co_await`/`co_yield`/`co_return` coroutines emitted by the EDG 6.6 frontend, splitting a single coroutine function into separate resume, destroy, and cleanup functions while computing a coroutine frame struct to carry live state across suspend points. NVIDIA adds one proprietary intrinsic (`llvm.nvvm.coro.create.suspend`) and emits a `.pragma "coroutine"` annotation in PTX, but the core splitting and frame layout algorithms are stock LLVM. The practical constraint is that coroutine frame allocation on GPU defaults to `malloc` in device heap -- extremely expensive on current architectures -- making CoroElide (which replaces heap allocation with a caller-stack alloca) the pass that determines whether GPU coroutines are viable or pathological.

## Key Facts

| Item | Value |
|------|-------|
| CoroSplit pass entry | `sub_24EF980` (71 KB, address range `0x24EF980`--`0x24F2300`) |
| CoroFrame layout computation | `sub_24F6730` (11,249 bytes, stack frame 5,624 bytes) |
| Core frame layout workhorse | `sub_24F5860` (called from CoroFrame) |
| createResumeFunction | `sub_2284030` |
| createDestroyFunction | `sub_2284040` |
| CoroEarly pass | `sub_24DCD10` (41 KB) |
| CoroElide pass | `sub_24DF350` (80 KB) |
| CoroAnnotationElide pass | `sub_24E2340` (33 KB) |
| Pass name / debug type | `"CoroSplit"` / `"coro-split"` (at `0x4388A37` / `0x4387AC3`) |
| Coroutine metadata table | `unk_4F8FAE8` |
| Pipeline parser ID | #156 (CGSCC pass, param: `reuse-storage`) |
| NVIDIA intrinsic | `llvm.nvvm.coro.create.suspend` (single constant integer argument) |
| PTX annotation | `.pragma "coroutine";` |

## The Coroutine Lowering Pipeline

Five passes run in a fixed sequence across the optimizer pipeline. The first and last are module-level bookends; the middle three do the real work inside the CGSCC (Call Graph SCC) pipeline where inlining decisions interact with coroutine splitting.

```
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

CoroSplit is registered as CGSCC pass #156 with an optional `reuse-storage` parameter. When `reuse-storage` is active, the pass attempts to reuse the storage of coroutine frames that are provably dead -- relevant for generators where the frame is allocated once and resumed many times. In the CGSCC context, CoroSplit runs alongside the inliner (`inline`) and `function-attrs`, allowing newly split resume/destroy functions to be immediately considered for inlining into callers within the same SCC.

## CoroSplit: Suspend Point Detection and Function Splitting

### Detection Phase

`sub_24EF980` iterates over every function in the module. For each function, it scans all instructions using a bitmask-based opcode test to identify coroutine suspension intrinsics:

```c
// Suspend point detection (at 0x24F00E6)
uint8_t opcode = inst->getOpcode();
unsigned normalized = opcode - 0x22;
if (normalized > 51) continue;  // not in range [0x22, 0x55]

uint64_t mask = 0x8000000000041ULL;
if (!((mask >> normalized) & 1)) continue;  // bit not set
```

The bitmask `0x8000000000041` encodes three intrinsic opcodes:

| Bit position | Opcode | Intrinsic |
|-------------|--------|-----------|
| 0 | `0x22` | `llvm.coro.suspend` -- normal suspend point |
| 6 | `0x28` | `llvm.coro.suspend.retcon` -- returned-continuation suspend |
| 51 | `0x55` | `llvm.coro.end` -- coroutine termination |

This single 64-bit `bt` (bit-test) instruction replaces what would otherwise be a three-way comparison or switch, a pattern upstream LLVM uses in its `Intrinsic::ID` checking.

### Validation

After finding a suspend point, CoroSplit validates the coroutine structure (at `0x24F010E`):

1. Locates the `llvm.coro.id` intrinsic (opcode `0x55` = 'U', intrinsic ID 59 = `0x3B`)
2. Verifies the parent function pointer is non-null and starts with opcode 0 (entry block)
3. Confirms the promise alloca matches between `coro.id` and function context
4. Checks the "has personality" flag (`bit 5 of byte at offset +0x21`)
5. Validates intrinsic ID equals 59 (`cmp dword [rax+24h], 0x3B`)

Nested coroutines receive additional validation: the pass checks that `coro.begin` (opcode range `0x1E`--`0x28`, ID 57 = `0x39`) references the correct parent function, preventing cross-coroutine confusion when one coroutine is nested inside another.

### Suspend Point Collection

Validated suspend points are collected into a deduplicated array. The dedup check at `0x24F02F9` scans existing entries, following def-use chains (`[rbx+10h]`) to avoid processing the same suspend point twice when multiple CFG paths reach it. For each suspend point, the pass extracts the value operand at instruction offset `+0x28`.

### Function Splitting

The split mode field at `[rbp-0x3F8]` controls which function variants are created:

```c
// At 0x24F0540
int split_mode = frame_state->split_mode;
if (split_mode == 0) {
    // Create destroy function only (returned-continuation style)
    createDestroyFunction(state, orig_fn, suspends, coro_info, ...);
} else if (split_mode <= 3) {
    // Create both resume and destroy functions (standard coroutine)
    Function *resume = createResumeFunction(state, orig_fn, suspends, ...);
    Function *destroy = createDestroyFunction(state, orig_fn, suspends, ...);
}
```

`sub_2284030` (createResumeFunction) and `sub_2284040` (createDestroyFunction) each:

1. Clone the original coroutine function via `sub_D2E510`
2. Replace the coroutine frame parameter with a typed pointer to the frame struct
3. Insert a switch statement at the entry block dispatching on the suspend index stored in the frame
4. Replace each `llvm.coro.suspend` with a return instruction
5. Wire function pointers (`__resume_fn`, `__destroy_fn`) into the frame header

After splitting, the pass emits an optimization remark:

```
Split '<function_name>' (frame_size=N, align=M)
```

where `N` is the computed frame size in bytes and `M` is the frame alignment (computed as `1 << alignment_log2`). The remark is published through the standard LLVM diagnostic handler at `sub_1049740`.

## CoroFrame: Frame Layout Computation

`sub_24F6730` is the largest and most complex function in the coroutine pipeline, with a 5,624-byte stack frame -- one of the largest in the entire cicc binary. Its job: determine which SSA values are live across suspend points and must be "spilled" into the coroutine frame struct.

### Algorithm Overview

The algorithm is a BFS-based cross-suspend-point liveness analysis:

1. **Initialize tracking structures.** Two hash tables with 16-byte entries, sentinel `0xFFFFFFFFF000`, hash function `(val >> 4) ^ (val >> 9)`. Initial capacity 8 entries each.

2. **Iterate all instructions.** Walk every basic block and instruction. A visitor callback (`[visitor+18h]`, virtual call) classifies each instruction as relevant or not to the frame computation.

3. **BFS traversal.** A deque with 512-byte blocks (64 pointer-sized entries per block) drives BFS over the CFG. The core computation at `sub_24F5860` determines which values cross which suspend points.

4. **Spill set computation.** Values that are defined before a suspend point and used after it must be stored in the frame. The result is a set of (value, suspend_point) pairs.

5. **Frame layout.** The frame type builder (at `0x3169200` in the second code cluster) arranges spill slots into a struct:

```c
struct __coro_frame {
    void (*__resume_fn)(struct __coro_frame *);   // +0x00
    void (*__destroy_fn)(struct __coro_frame *);  // +0x08
    uint32_t __coro_index;                        // +0x10 (suspend point index)
    // ... spill slots, ordered by alignment ...
    // ... promise storage ...
    // ... alloca copies ...
};
```

The frame type name is `".coro_frame_ty"` and the frame variable is `"__coro_frame"`. The suspend point index field `"__coro_index"` is the state variable for the resume switch dispatch.

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

### Hash Table Policy

Both hash tables in CoroFrame share identical parameters:

- **Hash function:** `(val >> 4) ^ (val >> 9)` -- same hash used throughout cicc (e.g., WPD pass)
- **Entry size:** 16 bytes (8-byte key + 8-byte metadata)
- **Empty sentinel:** `0xFFFFFFFFF000`
- **Load factor threshold:** 75% (triggers growth when `count * 4 >= capacity * 3`)
- **Tombstone cleanup:** 12.5% (rehash when `tombstones > capacity >> 3`)
- **Growth factor:** 2x (capacity doubles on each growth)
- **Collision resolution:** linear probing

## GPU-Specific Constraints

### Frame Allocation: The Heap Problem

Standard LLVM coroutines allocate the frame on the heap via `operator new` (or a custom allocator returned by `get_return_object_on_allocation_failure`). On GPU, this calls into the device-side `malloc`, which:

- Operates on a fixed-size heap (`cudaLimitMallocHeapSize`, default 8 MB)
- Serializes across threads within a warp (implementation-defined, but typically slow)
- Can fragment badly under concurrent allocation from thousands of threads
- Has latency orders of magnitude higher than register or local memory access

This makes **CoroElide the most critical coroutine optimization for GPU targets**. When CoroElide proves the coroutine frame lifetime is bounded by the caller, it replaces the heap allocation with a stack alloca, which maps to per-thread local memory (address space 5 in NVPTX). Local memory accesses go through the L1 cache and are dramatically faster than device `malloc`.

### CoroElide on GPU

`sub_24DF350` (80 KB -- the largest coroutine pass) implements the elision analysis. It examines every `llvm.coro.id` call site and attempts to prove that:

1. The coroutine handle does not escape the caller
2. No alias of the handle is stored to memory visible to other threads
3. The coroutine is fully consumed (all suspend/resume/destroy calls are within the caller)

When elision succeeds, the pass emits: `'<coroutine>' elided in '<caller>'`. When it fails: `'<coroutine>' not elided in '<caller>'`. These diagnostics (via `-Rpass=coro-elide` or `-Rpass-missed=coro-elide`) are essential for GPU developers diagnosing coroutine performance.

### CoroAnnotationElide

`sub_24E2340` (33 KB) is the newer annotation-driven elision from LLVM 19. It looks for the `"elide_safe_attr"` function attribute and `".noalloc"` suffix on coroutine function names. When both are present, elision proceeds without the full escape analysis -- the developer has asserted safety. This is particularly useful for GPU code where the developer knows the coroutine is single-thread-scoped but the compiler cannot prove it due to pointer-to-generic-address-space casts.

### The `llvm.nvvm.coro.create.suspend` Intrinsic

This is the sole NVIDIA-proprietary coroutine intrinsic. The NVVM verifier enforces:

```
llvm.nvvm.coro.create.suspend must have exactly one argument,
which must be a constant integer
```

The constant integer argument likely encodes a suspend-point identifier or mode. This intrinsic appears in the NVVM intrinsic table alongside `llvm.nvvm.stacksave` and `llvm.nvvm.stackrestore`, suggesting it interacts with the local memory stack for frame placement. Its exact lowering is handled by the NVVM-specific intrinsic lowering pass rather than the standard CoroSplit pipeline.

### PTX `.pragma "coroutine"`

The AsmPrinter (documented in [asmprinter.md](../infra/asmprinter.md)) optionally emits `.pragma "coroutine";` in the function header. This is triggered by metadata nodes with type byte `'N'` (0x4E) linked to the current function via the list at `this+792`. The pragma signals to `ptxas` that the function uses coroutine semantics, potentially affecting register allocation and scheduling decisions in the assembler.

### Warp Divergence at Suspend Points

A fundamental tension exists between SIMT execution and coroutine suspend. When one thread in a warp suspends while others do not, the warp diverges. The resume dispatch switch (the `__coro_index`-based state machine) creates a divergence point: threads may be at different suspend indices, requiring the hardware to serialize execution paths. This is identical to how any data-dependent branch causes divergence, but the impact is amplified because coroutine state machines typically have many switch cases (one per suspend point).

The StructurizeCFG pass (see [structurizecfg.md](../llvm/structurizecfg.md)) runs after coroutine lowering and will structurize the resume switch, potentially introducing additional control flow to manage reconvergence.

## The Second Code Cluster (0x3150000 Region)

The binary contains a second, independent cluster of coroutine functions, likely from a different compilation unit or LTO merge:

| Address | Size | Identity |
|---------|------|----------|
| `0x3171DA0` | 55 KB | CoroFrame layout computation |
| `0x316D160` | 49 KB | CoroSplit splitting logic |
| `0x3160A60` | 48 KB | CoroSplit dispatcher (`.corodispatch`, `MustTailCall.Before.CoroEnd`) |
| `0x31650D0` | 47 KB | Spill/reload generation (`AllocaSpillBB`, `PostSpill`, `.reload`, `.spill.addr`) |
| `0x3169200` | 46 KB | Frame type builder (`__coro_frame`, `.coro_frame_ty`, `__coro_index`) |
| `0x315A7B0` | 41 KB | CoroElide heap allocation elision |
| `0x3150D70` | 43 KB | Attributor analysis helper |
| `0x314DBB0` | 40 KB | Attributor analysis helper |

These functions reference the same string literals and implement the same algorithms as the primary cluster. The spill/reload generator at `0x31650D0` creates basic blocks named `"AllocaSpillBB"` and `"PostSpill"`, inserting `".spill.addr"` stores and `".reload"` loads to shuttle values between the coroutine frame and SSA registers. The frame type builder at `0x3169200` constructs the `StructType` with fields `__resume_fn`, `__destroy_fn`, and `__coro_index`, followed by the computed spill slots.

The CoroSplit dispatcher at `0x3160A60` generates the `.corodispatch` function -- a trampoline that loads `__coro_index` from the frame, switches on it, and tail-calls the appropriate resume point. The string `"MustTailCall.Before.CoroEnd"` indicates it enforces musttail semantics on the final resume-to-end transition, ensuring no stack growth across coroutine bounces.

## EDG Frontend Support

The EDG 6.6 frontend fully implements C++20 coroutine semantics in two key functions:

- **`sub_87AFA0`** (14 KB) -- Coroutine body processor. Resolves `promise_type` methods: `initial_suspend`, `final_suspend`, `unhandled_exception`, `get_return_object`, `get_return_object_on_allocation_failure`. Generates the coroutine body scaffolding including the implicit try-catch around user code.

- **`sub_87BD00`** (6 KB) -- Coroutine trait resolver. Looks up `std::coroutine_traits<R, Args...>::promise_type`, `std::coroutine_handle`, `return_value`, `return_void`. The EDG IL walker maps these as IL node type 64 (`il_coroutine`).

The frontend does **not** restrict coroutines to host-side code. `__device__` functions can be coroutines, and the full coroutine IR (with `llvm.coro.id`, `llvm.coro.begin`, `llvm.coro.suspend`, etc.) flows into the NVVM optimizer pipeline.

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

## Function Map

| Address | Size | Identity |
|---------|------|----------|
| `sub_24DCD10` | 41 KB | CoroEarly pass entry |
| `sub_24DF350` | 80 KB | CoroElide pass entry |
| `sub_24E2340` | 33 KB | CoroAnnotationElide pass entry |
| `sub_24EF980` | 71 KB | CoroSplit pass entry |
| `sub_24F5860` | -- | Core frame layout computation |
| `sub_24F6730` | 11 KB | CoroFrame layout entry |
| `sub_2284030` | -- | createResumeFunction |
| `sub_2284040` | -- | createDestroyFunction |
| `sub_D2E510` | -- | Function cloner (used for resume/destroy) |
| `sub_B2D610` | -- | Frame-already-computed check |
| `sub_BD5D20` | -- | Get function name string |
| `sub_B17560` | -- | Create optimization remark |
| `sub_1049740` | -- | Publish remark to diagnostic handler |
| `sub_22077B0` | -- | Allocator (frame info, spill entries, BFS deque) |
| `sub_2337E30` | 15 KB | `coro-cond` module analysis checker |
| `sub_314DBB0` | 40 KB | Attributor helper (coroutine attributes) |
| `sub_3150D70` | 43 KB | Attributor helper (coroutine attributes) |
| `sub_315A7B0` | 41 KB | CoroElide (second cluster) |
| `sub_3160A60` | 48 KB | CoroSplit dispatcher (`.corodispatch`) |
| `sub_31650D0` | 47 KB | Spill/reload generation |
| `sub_3169200` | 46 KB | Frame type builder |
| `sub_316D160` | 49 KB | CoroSplit splitting logic (second cluster) |
| `sub_3171DA0` | 55 KB | CoroFrame layout (second cluster) |

## Cross-References

- [Pipeline & Ordering](../llvm/pipeline.md) -- where coroutine passes sit in the optimization sequence
- [SROA, EarlyCSE & JumpThreading](../llvm/scalar-passes.md) -- SROA interacts with coroutine frame allocas
- [AsmPrinter & PTX Body Emission](../infra/asmprinter.md) -- `.pragma "coroutine"` emission
- [Inliner Cost Model](../lto/inliner-cost.md) -- inlining decisions for split resume/destroy functions
- [StructurizeCFG](../llvm/structurizecfg.md) -- structurizes the resume dispatch switch
