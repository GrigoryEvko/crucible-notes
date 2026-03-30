# InstCombine

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.
>
> **Upstream source:** `llvm/lib/Transforms/InstCombine/InstructionCombining.cpp`, `llvm/lib/Transforms/InstCombine/InstCombine*.cpp` (LLVM 20.0.0). The upstream is split across ~15 files by instruction category; cicc inlines them into a single monolithic visitor.

NVIDIA's InstCombine in CICC v13.0 is approximately twice the size of upstream LLVM's, weighing in at roughly 405 KB for the main visitor alone. The monolithic visitor function at `sub_10EE7A0` dispatches across 80 unique opcode cases through a three-level switch structure, handling standard LLVM instructions, NVIDIA-extended vector and FMA operations, and three high-opcode NVVM intrinsic dead-code elimination patterns. A separate 87 KB intrinsic folding function (`sub_1169C30`) handles NVVM-specific canonicalization, and a 127 KB `computeKnownBits` implementation (`sub_11A7600`) provides the dataflow backbone. This page covers the visitor architecture, the per-instruction-type visitors recovered from the binary, and the NVIDIA-specific extensions that distinguish this implementation from upstream.

| | |
|---|---|
| **Main visitor** | `sub_10EE7A0` (`0x10EE7A0`, ~405 KB, 9,258 lines) |
| **Intrinsic folding** | `sub_1169C30` (`0x1169C30`, ~87 KB, 2,268 lines) |
| **computeKnownBits** | `sub_11A7600` (`0x11A7600`, ~127 KB, 4,156 lines) |
| **SimplifyDemandedBits** | `sub_11AE870` / `sub_11AE3E0` (wrapper + hash table) |
| **Opcode cases** | 80 unique case labels across 3 switch blocks |
| **NVIDIA extra size** | ~200 KB beyond upstream (~87 KB intrinsic fold + ~113 KB expanded cases) |

## Visitor Architecture

The main visitor `sub_10EE7A0` receives an NVVM IR node pointer (`__m128i* a2`) and attempts to simplify it. A persistent local `v1612` aliases the instruction being visited. The function has four structural regions:

**Preamble** (lines ~1760--2000) performs pre-dispatch checks: validating call-site attributes (opcode 41 for bitwise-assert), handling ternary FMA instructions (opcodes 238--245), checking for constant-foldable select patterns, canonicalizing operand ordering (constant to RHS), and running SimplifyDemandedBits via `sub_11A3F30` on the result type.

**Opcode dispatch** reads the NVVM opcode via `sub_987FE0` (getOpcode) and uses a three-level switch:

| Switch Level | Opcode Range | Description |
|---|---|---|
| Level 1 | `0x99`--`0x2A5` (main) | Standard LLVM instructions (GEP, select, stores, casts, compares, calls, vectors) |
| Level 2 | `0x01`--`0x42` (low) | Binary operations, casts, early comparisons |
| Level 3 | `> 0x13CF` (high) | NVIDIA proprietary intrinsic IDs (9549, 9553, 9567) |

Additional if-else chains handle intermediate ranges: opcodes `0xC7E` (3198), `0x2E2` (738), `0x827` (2087), `0x2CC` (716), `0xE07`--`0xE08`, `0xE4F`--`0xE51`, `0x13C6`--`0x13C7`, and `0x13CD`--`0x13CE`.

The **fallback** path at LABEL_95 calls `sub_F0C430` for generic simplification. The **no-change** return path at LABEL_155 is referenced 101 times throughout the function.

## Per-Instruction Visitors

Each major instruction type is handled by a dedicated visitor function called from the main dispatch. The following table summarizes the recovered visitors with their sizes and key characteristics.

### visitBinaryOperator -- `sub_10D8BB0`

| | |
|---|---|
| **Address** | `0x10D8BB0` (102 KB, 2,078 lines) |
| **Dispatch case** | `0x3A` in the master dispatcher |
| **Sibling cases** | `0x39` (NSW/NUW-focused), `0x3B` (associative/commutative) |

This is the second-largest visitor. It implements approximately 25 cascading simplification phases for all binary arithmetic (Add, Sub, Mul, Div, Rem, Shl, LShr, AShr, And, Or, Xor, and their floating-point counterparts). The phases execute in a strict try-and-return order:

Phase 0 runs quick exits: pattern-matched constant fold (`sub_101E960`), `SimplifyBinOp` (`sub_F29CA0`), algebraic identities (`sub_F0F270`), NSW/NUW simplification (`sub_F11DB0`), and critically the **NVIDIA-specific intrinsic handler** `sub_11AE870` which runs before any standard LLVM folds.

Phases 1--9 handle associative/commutative factoring, cross-operand Mul-of-Add matching, delegated simplification, overflow detection, and multiply-shift strength reduction. Phase 5 detects multiply-by-power-of-2 and converts to shift; `sub_10BA120` builds the full strength reduction for patterns like `x * (2^n + 1)` into `(x << n) + x`.

Phases 10--25 cover Add-of-Mul factoring, shift chains, linear expression folding, subtraction of multiplied constants, demanded-bits masking, reciprocal elimination, overflow intrinsic decomposition, and division/remainder folding. The division constant folder uses `sub_C46BD0` (APInt::udiv), `sub_C499A0` (APInt::urem), `sub_C45F70` (APInt::sdiv), and `sub_C49AB0` (APInt::srem).

Four template-instantiated helpers at `sub_10D2680`--`sub_10D2D70` (2,767 bytes each, identical structure) implement `matchBinOpReduction` parameterized by NVVM intrinsic ID (329, 330, 365, 366) and acceptable opcode range. These detect NVVM horizontal reduction intrinsics (e.g., horizontal add/mul across vector lanes) and simplify them to scalar binary operations.

### visitICmpInst -- `sub_1136650` + `sub_113CA70`

| | |
|---|---|
| **Comprehensive folder** | `sub_1136650` (`0x1136650`, 149 KB, 3,697 lines) |
| **Per-opcode dispatch** | `sub_113CA70` (`0x113CA70`) -- 12 case labels |

The ICmp folder is the single largest function in InstCombine. It runs before the per-opcode dispatch table and handles 15 major fold categories: all-ones/sign-bit constant folds, Mul-with-constant strength reduction (NUW-gated), nested Mul decomposition, common sub-operand cancellation, NUW/NSW flag-gated predicate conversion, known-nonnegativity folds, ConstantRange intersection, shared sub-operand elimination, Sub sign-bit analysis, min/max pattern recognition, computeKnownBits sign-bit analysis, power-of-2 optimizations, remainder pattern matching, XOR/shift decomposition, and Or/And decomposition with type width folding.

NVVM uses a custom predicate encoding stored at `ICmpInst+2` as a 6-bit field (`*(_WORD*)(inst+2) & 0x3F`):

| Value | Predicate | Value | Predicate |
|---|---|---|---|
| 32 | EQ | 33 | NE |
| 34 | UGT | 35 | UGE |
| 36 | ULT | 37 | ULE |
| 38 | SGT | 39 | SGE |
| 40 | SLT | 41 | SLE |

The per-opcode dispatch at `sub_113CA70` routes based on the non-constant operand's opcode tag:

| Tag | Instruction | Handler | Size |
|---|---|---|---|
| `*` (42) | Mul | `sub_1128290` | 1,178 lines |
| `,` (44) | Add | `sub_1119FB0` | 413 lines |
| `.` (46) | Trunc | `sub_1115510` | -- |
| `0` (48) | SExt | `sub_11164F0` | -- |
| `1` (49) | ZExt | `sub_1122A30` | -- |
| `4` (52) | Select | `sub_1115C10` | 428 lines |
| `6` (54) | And | `sub_1120680` | 911 lines |
| `7` (55) | Or | `sub_1126B10` | 786 lines |
| `8` (56) | Xor | `sub_1126B10` | shared with Or |
| `9` (57) | Shl | `sub_112C930` | 664 lines |
| `:` (58) | LShr | `sub_1133500` | -- |
| `;` (59) | Sub | `sub_111CED0` + `sub_113BFE0` | 519 lines |

### visitCastInst -- `sub_110CA10`

| | |
|---|---|
| **Address** | `0x110CA10` (93 KB, 2,411 lines) |
| **Cast chain helper** | `sub_110B960` (22 KB, 833 lines) |

Handles all cast simplification: same-type identity elimination, bool-to-float chains, integer-to-integer narrowing/widening, FP-to-int special cases, FP narrowing, cast-through-select/PHI, and the major cast-of-cast chain folding. The helper `sub_110B960` implements deep cast chain folding for aggregate types using a worklist with a `DenseMap` for O(1) deduplication, preventing exponential blowup on diamond-shaped use-def graphs. The function is conservative about side effects: `sub_B46500` (isVolatile) is called before every fold.

### visitSelectInst -- `sub_1012FB0`

| | |
|---|---|
| **Address** | `0x1012FB0` (74 KB, 1,801 lines) |
| **Local variables** | 190 total |

Implements 18 prioritized select simplifications: constant fold, undef arm elimination, both-same identity, PHI-through-select, KnownBits sign analysis, ConstantRange analysis, full-range analysis, KnownBits cross-validation, ICmpInst arm synthesis, ExtractValue decomposition, implied condition, canonicalization (delegated to `sub_1015760`, 27 KB), min/max pattern detection (smin/smax/umin/umax/abs/nabs via four helpers), select-in-comparison chains, PHI-select worklist scan (DenseMap with hash `(ptr >> 9) ^ (ptr >> 4)`), ValueTracking classification, pointer-null folding, and load/trunc delegation.

### visitPHINode -- `sub_1175E90`

| | |
|---|---|
| **Address** | `0x1175E90` (~57 KB, ~2,130 lines) |

Implements 16 PHI optimization strategies tried in sequence: SimplifyInstruction constant fold, foldPHIArgOpIntoPHI (binary/cast with one varying operand), foldPHIArgConstantOp, typed opcode dispatch (GEP via `sub_1172510`, InsertValue, ExtractValue, CmpInst, BinOp/Cast), GEP incoming deduplication with loop back-edge analysis, single-use PHI user check, GEP-of-PHI transform (`sub_1174BB0`, 1,033 lines), phi-cycle escape detection, trivial PHI elimination (all-same non-PHI value), recursive PHI cycle resolution (`sub_116D410`), operand reordering canonicalization, identical-PHI-in-block deduplication, pointer-type struct GEP optimization, all-undef incoming check, and dominator-tree GEP index hoisting using two DenseMaps.

### visitCallInst -- `sub_1162F40`

| | |
|---|---|
| **Address** | `0x1162F40` (50 KB, 1,647 lines) |

Processes calls through a 15-step cascade: LibCall simplification (`sub_100A740`), standard intrinsic folding (`sub_F0F270`), return attribute analysis (`sub_F11DB0`), overflow/saturating arithmetic (`sub_115C220`), inline mul-by-constant folding, generic call combining (`sub_115A080`), FMA/fneg/fsub canonicalization (the largest block, requiring all of nnan+ninf+nsz+arcp+reassoc on both call and function), constant-argument intrinsic folding, unary intrinsic constant folding, exp/log pair detection (IDs 325 and 63), sqrt/rsqrt folding (IDs 284, 285), min/max folding (IDs 88, 90), nested intrinsic composition, division-to-reciprocal-multiply, and finally the NVIDIA-specific `sub_115A4C0` which dispatches to the 87 KB intrinsic folding table.

### visitLoadInst -- `sub_1152CF0`

| | |
|---|---|
| **Address** | `0x1152CF0` (~68 KB, ~1,680 lines) |
| **Stack frame** | `0x4F0` bytes (1,264 bytes) |

Four major paths: constant-address fold (loads from known constant pointers with types <= 64 bits are replaced via symbol table lookup using `sub_BCD420`), address-space-based elimination (loads from non-AS(32) pointers are replaced with constants, exploiting CUDA's read-only address spaces), the main store-to-load forwarding worklist (BFS over the def-use graph following GEPs, PHIs, and bitcasts, depth-limited by global `qword_4F90528`), and dominator-based forwarding for non-pointer loads. Alignment is propagated as the maximum of source and destination, with the volatile bit carefully preserved through the `*(node+2)` 16-bit field (bits [5:0] = log2(alignment), bit [6] = volatile flag).

## NVIDIA-Specific Extensions

### NVVM Intrinsic Folding -- `sub_1169C30`

This 87 KB function is the core of NVIDIA's additions to InstCombine. Called from the main visitor when the instruction is an NVIDIA intrinsic, it uses a two-layer dispatch:

**Layer 1** (primary switch, entered when the uses-list is empty or the "fast" flag at `a1+336` is set) dispatches on the node's byte-tag:

| Tag | Char | Fold Type |
|---|---|---|
| 42 | `*` | FNeg/negation -- pushes negation through arithmetic via the "Negator" chain |
| 55 | `7` | Vector extract from intrinsic result (full-width extract becomes identity) |
| 56 | `8` | Vector insert into intrinsic result (full-width insert becomes And mask) |
| 59 | `;` | Multiply-like symmetric intrinsic (folds when one operand is known non-negative) |
| 68 | `D` | ZExt of i1 intrinsic result (bypasses intrinsic wrapper) |
| 69 | `E` | SExt of i1 intrinsic result (bypasses intrinsic wrapper) |
| 85 | `U` | Call-site fold for `llvm.nvvm.*` with specific IDs (313, 362) |
| 86 | `V` | Select-like intrinsic fold (dead select elimination) |

**Layer 2** (depth-gated by `qword_4F908A8` = `instcombine-negator-max-depth`) adds aggressive cases:

| Tag | Char | Fold Type |
|---|---|---|
| 46 | `.` | Dot product fold |
| 54 | `6` | Indexed access / extract with fold |
| 58 | `:` | Comparison intrinsic fold |
| 67 | `C` | Type conversion intrinsic fold |
| 84 | `T` | Tensor / multi-operand intrinsic fold |
| 90 | `Z` | Zero-extend intrinsic fold |
| 91 | `[` | Three-operand fold (e.g., fma) |
| 92 | `\` | Four-operand fold (e.g., dp4a) |
| 96 | `` ` `` | Unary special intrinsic fold |

The FNeg case (tag 42) is the most complex. It first attempts constant folding: if the operand is all-ones (-1), it creates `sub(0, operand)` via CSE lookup with opcode 30. When the simple fold fails, it falls through to the **Negator chain** at LABEL_163: `sub_1168D40` collects all negatable sub-expressions, `sub_1169800` attempts to fold negation into each operand, and the results are combined with `sub_929C50` or `sub_929DE0`. This pushes negation through chains of arithmetic to find a cheaper representation, depth-gated to prevent exponential blowup. Created replacement instructions carry `.neg` modifier metadata for PTX emission.

### Three High-Opcode NVIDIA Intrinsics

Opcodes 0x254D (9549), 0x2551 (9553), and 0x255F (9567) are NVIDIA-proprietary intrinsic IDs handled directly in the main visitor. All three share the same pattern: extract the commuted-operand index via `v1612->m128i_i32[1] & 0x7FFFFFF`, verify the other operand has byte-tag 12 or 13 (ConstantInt/ConstantFP), query metadata via `sub_10E0080` with mask `0xFFFFFFFFFFFFFFFF`, and test specific bit patterns:

| Opcode | Test | Fold Condition |
|---|---|---|
| 0x2551 (9553) | `((result >> 40) & 0x1E) == 0x10` | Fold when bit pattern mismatches |
| 0x255F (9567) | `(result & 0x10) != 0` | Fold when bit 4 is clear |
| 0x254D (9549) | `(result & 0x200) != 0` | Fold when bit 9 is clear |

When the filter passes, the shared epilogue calls `sub_F207A0(v6, v1612->m128i_i64)` (eraseInstFromFunction), deleting the instruction entirely. These implement dead-code elimination for NVIDIA intrinsics with constant arguments matching known-safe-to-remove criteria.

### Separate Storage Assume Bundles

At lines 6557--6567 of the main visitor, the code iterates over operand bundles on `llvm.assume` calls (opcode `0x0B`). For each bundle with a tag of exactly 16 bytes matching `"separate_storage"` (verified by `memcmp`), it calls `sub_10EA360` on both bundle operands. This implements NVIDIA's `separate_storage` alias analysis hint, allowing InstCombine to exploit non-aliasing assumptions for pairs of pointers declared to reside in separate memory spaces.

### Expanded GEP Handling

The GEP case (opcode `0x99` = 153) is significantly expanded compared to upstream. The global `dword_4F901A8` controls a depth-limited chain walk for nested GEP simplification:

```
v729 = getOperand(0) of GEP
if (dword_4F901A8) {
    v730 = 0;
    do {
        if (!isConstantGEP(v729)) break;
        ++v730;
        v729 = getOperand(0, v729);  // walk up
    } while (v730 < dword_4F901A8);
}
if (*(_BYTE*)v729 != 85)  // 85 = CallInst
    goto LABEL_155;        // bail
```

This walks backward through constant-index GEP chains up to `dword_4F901A8` steps, looking for a CallInst base pointer. The knob controls how many GEP levels to look through when simplifying `GEP(GEP(GEP(..., call_result)))`.

### Ternary/FMA Support

The preamble handles 3-operand instructions (opcodes 238--245) representing fused multiply-add variants. This includes checking whether the third operand is a zero-constant, converting between FMA opcode variants (238 vs. 242), and handling address space mismatches on FMA operand types -- entirely NVIDIA-specific for CUDA's FMA intrinsics.

## computeKnownBits -- `sub_11A7600`

The 127 KB `computeKnownBits` implementation dispatches on the first byte of the NVVM IR node (the type tag):

| Tag | Char | Node Type |
|---|---|---|
| 42 | `*` | Truncation (extracts low bits) |
| 44 | `,` | GEP (computes known bits through pointer arithmetic) |
| 46 | `.` | Comparison (known result bits) |
| 48 | `0` | Select (intersection of known bits from both arms) |
| 52 | `4` | Branch-related |
| 54 | `6` | Vector shuffle |
| 55 | `7` | Vector extract |
| 56 | `8` | Vector insert |
| 57 | `9` | PHI node (intersection across incoming values) |
| 58 | `:` | Comparison variant |
| 59 | `;` | Invoke / call |
| 67 | `C` | Cast chain |
| 68 | `D` | Binary op path 1 |
| 69 | `E` | Binary op path 2 |
| 85 | `U` | CallInst (sub-dispatch: `0x0F`=abs, `0x42`=ctpop, `0x01`=bitreverse) |
| 86 | `V` | LoadInst |

A debug assertion at lines 2204--2212 fires when `computeKnownBits` and `SimplifyDemandedBits` produce inconsistent results, printing both APInt values and calling `abort()`. This invariant check (`known_zero & known_one == 0`, plus consistency with the demanded mask) is compiled in for debug/checked builds.

## SimplifyDemandedBits -- `sub_11AE870`

The wrapper `sub_11AE870` gets the bit-width via `sub_BCB060` (or `sub_AE43A0` for non-integer types), allocates two APInts sized to the width, delegates to `sub_11AE3E0`, and frees any heap-allocated storage. The core implementation at `sub_11AE3E0` (235 lines) calls `computeKnownBits`, then if the instruction was simplified, walks the use-chain and inserts each user into a hash table (open-addressing with quadratic probing, hash = `(ptr >> 9) ^ (ptr >> 4)`) at offset +2064 from the InstCombiner context. This "seen instructions" set prevents infinite recursion during demanded-bits propagation.

## Configuration Knobs

| Global | CLI Flag | Default | Used In |
|---|---|---|---|
| `dword_4F901A8` | (GEP chain look-through depth) | unknown | GEP handler (case 0x99) |
| `qword_4F908A8` | `instcombine-negator-max-depth` | -1 | `sub_1169C30` (depth gate) |
| `qword_4F90988` | `instcombine-negator-enabled` | 1 | ctor_090 |
| `qword_4F8B4C0` | `instcombine-split-gep-chain` | -- | ctor_068 |
| `qword_4F8B340` | `instcombine-canonicalize-geps-i8` | -- | ctor_068 |
| `qword_4F909E0` | `instcombine-max-num-phis` | -- | ctor_091 |
| `qword_4F90120` | `instcombine-guard-widening-window` | 3 | ctor_087 |
| `qword_4F90528` | (load forwarding search depth) | -- | `sub_1152CF0` |

## Key Helper Functions

| Address | Recovered Name | Purpose |
|---|---|---|
| `sub_987FE0` | `getOpcode()` | Reads NVVM opcode from IR node |
| `sub_B46B10` | `getOperand(idx)` | Operand access |
| `sub_B44E20` | `eraseFromParent()` | Unlink instruction |
| `sub_F207A0` | `eraseInstFromFunction()` | Delete instruction from worklist |
| `sub_F162A0` | `replaceInstUsesWith()` | RAUW and return replacement |
| `sub_F20660` | `setOperand(i, val)` | Replace operand in-place |
| `sub_B33BC0` | `CreateBinOp()` | IRBuilder binary op creation |
| `sub_B504D0` | `CreateBinOp(no-flags)` | Binary op without flags |
| `sub_B51D30` | `CreateCast()` | Cast instruction creation |
| `sub_AD8D80` | `ConstantInt::get(type, APInt)` | Constant integer factory |
| `sub_AD64C0` | `ConstantInt::get(type, val, signed)` | Constant integer factory (scalar) |
| `sub_BCB060` | `getScalarSizeInBits()` | Type bit-width query |
| `sub_10E0080` | `getKnownBitsProperty()` | Metadata property query |
| `sub_B43CB0` | `getFunction()` | Get parent function |
| `sub_B43CA0` | `getParent()` | Get parent basic block |
| `sub_10A0170` | `extractFlags()` | Read fast-math, exact, etc. |
| `sub_B44900` | `isCommutative()` | Check commutativity |
| `sub_C444A0` | `APInt::countLeadingZeros()` | Bit analysis |
| `sub_986760` | `APInt::isZero()` | Zero test |
| `sub_10EA360` | `recordSeparateStorageOperand()` | Separate storage alias hint |

## Size Contribution Estimate

| Component | Size | Description |
|---|---|---|
| Upstream visitor baseline | ~200 KB | Standard LLVM visiting ~50 instruction types |
| `sub_1169C30` intrinsic folding | ~87 KB | NVVM-specific intrinsic canonicalization |
| NVVM GEP/FMA/vector cases | ~40 KB | Expanded GEP chains, ternary FMA, vector width-changing |
| `separate_storage` + assume | ~10 KB | Operand bundle handling for alias hints |
| High-opcode NVIDIA intrinsics | ~15 KB | DCE for opcodes 0x254D/0x2551/0x255F |
| Expanded comparator/cast | ~50 KB | Extended ICmp, cast chain, select handling |
| **NVIDIA total addition** | **~200 KB** | Roughly doubles upstream InstCombine |

## Optimization Level Behavior

| Level | Scheduled | Instances | Notes |
|-------|-----------|-----------|-------|
| **O0** | Not run | 0 | No optimization passes |
| **Ofcmax** | Runs | 1 | Single instance in fast-compile pipeline |
| **Ofcmid** | Runs | 2 | Early + post-GVN cleanup |
| **O1** | Runs | 3-4 | Early, post-SROA, post-GVN, late cleanup |
| **O2** | Runs | 4-5 | Same as O1 + additional Tier 2 instance after loop passes |
| **O3** | Runs | 5-6 | Same as O2 + Tier 3 instance; benefits from more aggressive inlining/unrolling |

InstCombine is the most frequently scheduled pass in the CICC pipeline. Each instance runs the full 405KB visitor but benefits from different preceding transformations: the post-SROA instance cleans up cast chains from aggregate decomposition, the post-GVN instance simplifies expressions exposed by redundancy elimination, and the late instance performs final canonicalization before codegen. The `instcombine-negator-max-depth` and `instcombine-negator-enabled` knobs apply uniformly across all instances. Even at Ofcmax, at least one InstCombine run is considered essential for basic IR canonicalization. See [Optimization Levels](../config/optimization-levels.md) for pipeline tier details.

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Binary size** | ~200 KB main visitor | ~405 KB main visitor + 87 KB intrinsic folding (~2x upstream) |
| **NVVM intrinsic folding** | No NVVM-specific intrinsic canonicalization | Dedicated 87 KB function (`sub_1169C30`) with two-layer dispatch for negation, vector extract/insert, FMA, tensor, dot product, and 15+ fold types |
| **High-opcode DCE** | Not present | Three NVIDIA proprietary intrinsic IDs (9549, 9553, 9567) with constant-argument dead-code elimination |
| **`separate_storage` bundles** | No `separate_storage` operand bundle handling | Iterates `llvm.assume` bundles, extracting `"separate_storage"` hints for alias-based optimization |
| **Ternary FMA opcodes** | Standard `llvm.fma` / `llvm.fmuladd` folding | Extended preamble handles opcodes 238--245 for CUDA FMA variants with address-space mismatch handling |
| **GEP chain look-through** | Single-level GEP simplification | Depth-limited chain walk (`dword_4F901A8` steps) backward through constant-index GEP chains to find CallInst base pointers |
| **Horizontal reduction** | Standard intrinsic-based reduction fold | Four template-instantiated `matchBinOpReduction` helpers for NVVM horizontal reduction intrinsics (IDs 329, 330, 365, 366) |
| **KnownBits integration** | Separate `computeKnownBits` in ValueTracking | Fused 127 KB `computeKnownBits` + `SimplifyDemandedBits` with GPU special-register range oracle |
