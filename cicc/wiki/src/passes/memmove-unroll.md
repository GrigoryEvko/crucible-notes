# Memmove Unrolling

CUDA GPUs have no hardware instruction for bulk memory copy. On a CPU, `memcpy` and `memmove` compile down to optimized microcode sequences (REP MOVSB, AVX-512 scatter/gather, or libc hand-tuned SIMD loops). On an SM, every byte of a copy must pass through explicit load and store instructions executed by individual threads. LLVM's standard `memcpy` lowering in SelectionDAG produces reasonable load/store sequences, but it operates late in the pipeline and cannot reason about NVVM IR semantics -- address spaces, alignment guarantees from the CUDA memory model, or the interaction between copy direction and overlapping shared-memory buffers. NVIDIA's memmove unrolling pass replaces `llvm.memmove` and `llvm.memcpy` intrinsic calls at the NVVM IR level with explicit element-wise copy loops, generating both forward and reverse copy paths to handle overlapping memory correctly.

The pass lives in the aggregate-lowering cluster at `0x1C80000`--`0x1CBFFFF`, adjacent to [struct splitting](struct-splitting.md) (`sub_1C86CA0`) and [FP128/I128 emulation](fp128-emulation.md) (`sub_1C8C170`). It is part of the `lower-aggr-copies` pipeline pass (pass index 417), which coordinates memmove unrolling, struct splitting, and aggregate store lowering as a single pipeline unit. Upstream LLVM has no equivalent IR-level memmove unroller -- this is entirely NVIDIA-proprietary.

## Key Facts

| Property | Value |
|---|---|
| Entry point | `sub_1C82A50` |
| Size | 39KB (~1,200 lines decompiled) |
| Binary cluster | `0x1C80000`--`0x1CBFFFF` (Aggregate Splitting + Memory Ops) |
| Pipeline pass | `lower-aggr-copies` (pass index 417, parameterized: `lower-aggr-func-args`) |
| Pass registration | `sub_233A3B0` (parameter parser for `LowerAggrCopiesPass`) |
| IR level | NVVM IR (pre-instruction-selection) |
| Unroll threshold global | `dword_4FBD560` |
| Knob constructor | `ctor_265` at `0x4F48E0` |
| LLVM upstream | No equivalent -- NVIDIA-proprietary |
| Neighbor passes | Struct splitting (`sub_1C86CA0`), FP128 emulation (`sub_1C8C170`) |

## Why This Pass Exists

On a CPU, `memmove(dst, src, n)` is a single function call that the runtime library implements with architecture-specific optimized loops, often using SIMD instructions that move 32 or 64 bytes per cycle. On a GPU:

1. **No bulk copy instruction.** PTX and SASS have `ld` and `st` but no `memcpy` or `rep movsb` equivalent. Every byte must be an explicit load followed by an explicit store.

2. **Per-thread execution model.** Each thread in a warp copies its own portion of data. A 128-byte struct copy in a kernel with 1024 threads means 1024 independent 128-byte copy sequences, all of which must resolve to individual load/store pairs.

3. **Address space semantics.** The source and destination may live in different address spaces (global, shared, local, constant). Generic-pointer memmove requires runtime address-space resolution, but if the compiler can resolve the spaces at IR time, it can emit space-qualified loads and stores that map directly to the correct PTX instructions.

4. **Overlap semantics.** `memmove` guarantees correct behavior when source and destination overlap. The pass must emit both a forward path (for `dst < src`) and a reverse path (for `dst >= src`) to preserve this guarantee. `memcpy` is also routed through this pass because the NVVM verifier enforces overlap-safety uniformly.

## Algorithm

The pass scans each function for `llvm.memmove` and `llvm.memcpy` intrinsic calls. For each call, it replaces the intrinsic with a 4-block CFG that implements element-wise copying. The generated code has two paths: one for when the element count is statically known and small enough to fully unroll, and one for dynamic or large counts that use a loop with a PHI induction variable.

### Step 1: Basic Block Structure Creation

The pass creates four new basic blocks, splitting the block containing the memmove call:

```text
              +-------+
              | split |   (direction comparison)
              +---+---+
             /         \
    +--------+--+   +--+----------+
    | forward.for|   | reverse.for |
    +--------+--+   +--+----------+
             \         /
            +----------+
            | nonzerotrip |   (exit / continuation)
            +----------+
```

| Block | Name string | Purpose |
|-------|-------------|---------|
| Entry | `"split"` | Compares `src` and `dst` addresses to choose copy direction |
| Forward | `"forward.for"` | Copies elements from index 0 upward |
| Reverse | `"reverse.for"` | Copies elements from index `count-1` downward |
| Exit | `"nonzerotrip"` | Continuation after the copy completes |

### Step 2: Forward vs. Reverse Decision

The `split` block determines copy direction by comparing the source and destination base addresses:

```llvm
; Pseudocode for the split block
%cmp = icmp ult ptr %dst, ptr %src     ; sub_12AA0C0, opcode 0x22 (34)
br i1 %cmp, label %forward.for, label %reverse.for   ; sub_15F83E0
```

The ICMP instruction is created via `sub_12AA0C0` with opcode `0x22` (34 decimal, corresponding to an unsigned-less-than integer comparison). The conditional branch is created via `sub_15F83E0`. When `dst < src`, memory does not overlap in the forward direction, so the forward path is safe. When `dst >= src`, copying forward would overwrite source bytes before they are read, so the reverse path is required.

### Step 3: Copy Generation -- Small/Static Path

When the copy size is statically known and satisfies `size <= dword_4FBD560` (the compile-time unroll threshold), the pass generates fully unrolled element-by-element copies with no loop overhead.

**Reverse copy** (decompiled lines 606--690):

```llvm
; Fully unrolled reverse copy, count elements
; For i = count-1 downto 0:
%src.gep.N = getelementptr i8, ptr %src, i64 N     ; named "src.memmove.gep.unroll"
%val.N     = load i8, ptr %src.gep.N, align A       ; sub_15F9210 (InitLoadInstruction)
%dst.gep.N = getelementptr i8, ptr %dst, i64 N     ; named "dst.memmove.gep,unroll" [sic]
store i8 %val.N, ptr %dst.gep.N, align A            ; sub_15F9650 (InitStoreInstruction)
; ... repeated for each index from count-1 down to 0
```

**Forward copy** (decompiled lines 1036--1123):

```llvm
; Fully unrolled forward copy, count elements
; For i = 0 to count-1:
%src.gep.N = getelementptr i8, ptr %src, i64 N     ; "src.memmove.gep.unroll"
%val.N     = load i8, ptr %src.gep.N, align A
%dst.gep.N = getelementptr i8, ptr %dst, i64 N     ; "dst.memmove.gep,unroll" [sic]
store i8 %val.N, ptr %dst.gep.N, align A
; ... repeated for each index from 0 up to count-1
```

Each load is created via `sub_15F9210` (InitLoadInstruction, opcode 64 type 1) and each store via `sub_15F9650` (InitStoreInstruction, opcode 64 type 2). Alignment is set on both loads and stores via `sub_15F8F50` / `sub_15F9450`, preserving the alignment from the original memmove intrinsic call (passed as parameter `a15`). Memory attributes (volatile flags, etc.) are propagated through parameters `a16` and `a17`.

### Step 4: Copy Generation -- Large/Dynamic Path

When the copy size exceeds the threshold or is not statically known, the pass generates a single-iteration loop body with a PHI induction variable:

**Forward loop:**

```llvm
forward.for:
  %iv = phi i64 [ 0, %split ], [ %iv.next, %forward.for ]   ; sub_15F1EA0, opcode 53
  %src.gep = getelementptr i8, ptr %src, i64 %iv
  %val = load i8, ptr %src.gep, align A
  %dst.gep = getelementptr i8, ptr %dst, i64 %iv
  store i8 %val, ptr %dst.gep, align A
  %iv.next = add i64 %iv, 1        ; sub_15A0680 (constant 1) + sub_15FB440 (ADD, opcode 13)
  %done = icmp eq i64 %iv.next, %count
  br i1 %done, label %nonzerotrip, label %forward.for   ; sub_15F83E0
```

**Reverse loop:**

```llvm
reverse.for:
  %iv = phi i64 [ %count.minus1, %split ], [ %iv.next, %reverse.for ]
  %src.gep = getelementptr i8, ptr %src, i64 %iv
  %val = load i8, ptr %src.gep, align A
  %dst.gep = getelementptr i8, ptr %dst, i64 %iv
  store i8 %val, ptr %dst.gep, align A
  %iv.next = sub i64 %iv, 1
  %done = icmp eq i64 %iv.next, -1      ; or icmp slt i64 %iv.next, 0
  br i1 %done, label %nonzerotrip, label %reverse.for
```

The PHI node is created via `sub_15F1EA0` with opcode 53. The constant `1` for the increment is created via `sub_15A0680`. The addition/subtraction uses `sub_15A2B60` or `sub_15FB440` (the 5-argument node constructor, opcode 13 for ADD). The `nonzerotrip` block serves as the exit target for both loop directions.

### Step 5: Alignment Propagation

The pass preserves the alignment annotation from the original memmove/memcpy intrinsic call. The alignment value is passed through the internal parameter `a15` to the load/store alignment setter functions `sub_15F8F50` (SetLoadAlignment) and `sub_15F9450` (SetStoreAlignment). This matters because downstream PTX emission can generate wider loads (e.g., `ld.global.v4.b32` for 16-byte aligned accesses) if the alignment permits it.

### Step 6: Cleanup

After generating the replacement CFG, the original memmove/memcpy intrinsic call is erased. The pass uses `sub_164D160` (RAUW -- Replace All Uses With) to rewire any remaining references.

## Unroll Threshold

The global variable `dword_4FBD560` controls the boundary between full unrolling and loop generation. This value is registered at `ctor_265` (`0x4F48E0`) as part of the aggregate copy lowering knob group.

| Condition | Code generation |
|-----------|----------------|
| `count` statically known AND `count <= dword_4FBD560` | Fully unrolled: N load/store pairs with no loop overhead |
| `count` statically known AND `count > dword_4FBD560` | Dynamic loop with PHI induction variable |
| `count` not statically known | Dynamic loop with PHI induction variable |

The tradeoff is straightforward: full unrolling eliminates loop overhead (branch, PHI, compare) but increases code size linearly. For GPU kernels where instruction cache pressure is rarely the bottleneck, unrolling small copies is almost always profitable. The threshold prevents pathological code size explosion for large static copies (e.g., a 4KB struct assignment would generate 4,096 load/store pairs without the limit).

The related knob `lower-aggr-unrolled-stores-limit` provides an additional limit on the number of stores generated in unrolled mode, and `large-aggr-store-limit` controls when aggregate stores transition from unrolled sequences to loops.

## Naming Conventions

The pass names its generated GEP instructions with distinctive prefixes that are visible in IR dumps and useful for debugging:

| Instruction | Name string | Notes |
|-------------|-------------|-------|
| Source GEP | `"src.memmove.gep.unroll"` | Period-separated |
| Destination GEP | `"dst.memmove.gep,unroll"` | Comma before `unroll` -- a typo in the binary [sic] |

The comma in `"dst.memmove.gep,unroll"` (where a period would be expected by analogy with the source GEP name) is a benign naming inconsistency baked into the binary string table. It has no semantic effect -- LLVM IR value names are arbitrary strings -- but it serves as a reliable fingerprint for identifying output from this specific pass. A reimplementation should preserve this exact string if binary-identical IR output is desired, or normalize it to `"dst.memmove.gep.unroll"` if not.

## Configuration

Knobs registered at `ctor_265` (`0x4F48E0`), applicable to the `lower-aggr-copies` pass cluster:

| Knob | Global | Description |
|------|--------|-------------|
| `lower-aggr-unrolled-stores-limit` | -- | Maximum number of stores in unrolled mode |
| `large-aggr-store-limit` | -- | Element count above which aggregate stores use a loop |
| `max-aggr-copy-size` | -- | Maximum aggregate copy size the pass will handle |
| `skiploweraggcopysafechk` | -- | Skip safety check in aggregate copy lowering |
| `devicefn-param-always-local` | -- | Treat device function parameter space as local |

The pass can be invoked via the pipeline text interface:

```bash
-Xcicc "-passes=lower-aggr-copies"
-Xcicc "-passes=lower-aggr-copies<lower-aggr-func-args>"
```

Related aggregate lowering knobs from `ctor_089` (`0x4A0D60`):

| Knob | Default | Description |
|------|---------|-------------|
| `max-aggr-lower-size` | 128 | Threshold size (bytes) below which aggregates are lowered |
| `aggressive-max-aggr-lower-size` | 256 | Aggressive threshold for aggregate lowering |

## Diagnostic Strings

```text
"split"
"forward.for"
"reverse.for"
"nonzerotrip"
"src.memmove.gep.unroll"
"dst.memmove.gep,unroll"
"memmove/memcpy cannot target constant address space"   (from nvvm-verify)
```

## Function Map

| Function | Address | Size | Role |
|----------|---------|------|------|
| Memmove unroller | `sub_1C82A50` | 39KB | Main pass: CFG construction, copy generation |
| ICMP creation | `sub_12AA0C0` | -- | Creates integer comparison (opcode 0x22) |
| Conditional branch | `sub_15F83E0` | -- | Creates `br i1` |
| InitLoadInstruction | `sub_15F9210` | -- | Creates load instruction (opcode 64, type 1) |
| InitStoreInstruction | `sub_15F9650` | -- | Creates store instruction (opcode 64, type 2) |
| SetLoadAlignment | `sub_15F8F50` | -- | Sets alignment on load |
| SetStoreAlignment | `sub_15F9450` | -- | Sets alignment on store |
| InitInstruction (PHI) | `sub_15F1EA0` | -- | Creates PHI node (opcode 53) |
| CreateConstant | `sub_15A0680` | -- | Creates integer constant (e.g., 1 for increment) |
| CreateBinaryOp | `sub_15FB440` | -- | Creates binary operation node (5-arg constructor) |
| CreateBinaryOp (variant) | `sub_15A2B60` | -- | Alternative binary op constructor |
| RAUW | `sub_164D160` | -- | Replace All Uses With |
| Pipeline param parser | `sub_233A3B0` | -- | Parses `lower-aggr-func-args` parameter |

## Cross-References

- [Struct/Aggregate Splitting](struct-splitting.md) -- sibling pass in the same `lower-aggr-copies` pipeline unit; decomposes struct-typed operations into scalar field operations
- [FP128/I128 Emulation](fp128-emulation.md) -- neighbor in the `0x1C80000` cluster; replaces wide arithmetic with runtime library calls
- [NVVM Verifier](nvvm-verify-deep.md) -- validates that memmove/memcpy targets are not in constant address space
- [NVIDIA Custom Passes](index.md) -- master index of all proprietary passes
- [SROA](../llvm/sroa.md) -- upstream LLVM pass that splits alloca-based aggregates; handles memcpy/memmove during alloca rewriting
