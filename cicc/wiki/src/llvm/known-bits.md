# KnownBits & DemandedBits for GPU

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

NVIDIA's KnownBits and DemandedBits infrastructure in cicc v13.0 diverges from upstream LLVM in three structural ways. First, the two analyses are fused into a single 127 KB function (`sub_11A7600`) that simultaneously computes known-zero/known-one bitmasks and simplifies instructions whose demanded bits allow constant folding or narrowing — upstream LLVM separates `computeKnownBits` (in ValueTracking) from `SimplifyDemandedBits` (in InstCombine). Second, a dedicated GPU-specific known-bits oracle (`sub_F0C4B0`) provides range constraints for NVIDIA special registers (`%tid`, `%ntid`, `%ctaid`, `%nctaid`, `%warpsize`, `%laneid`) that have no CPU equivalent. Third, an early NVVM pipeline pass (`nvvm-intr-range` at `sub_216F4B0`) attaches `!range` metadata to every special-register read intrinsic, giving downstream analyses the same bounded-range information that CPU targets only get from profile data or programmer assertions. Together these form the primary dataflow backbone for address calculation optimization, type narrowing, and dead-bit elimination in GPU kernels.

| | |
|---|---|
| **Merged computeKnownBits + SimplifyDemandedBits** | `sub_11A7600` (`0x11A7600`, 127 KB, 4,156 lines) |
| **Secondary SimplifyDemandedBits helper** | `sub_11A1430` (`0x11A1430`, 6.3 KB, 6 opcodes) |
| **Per-operand demand propagation trampoline** | `sub_11AE940` (`0x11AE940`) |
| **Generic computeKnownBits (reference)** | `sub_9AC0E0` (fallback for unhandled opcodes) |
| **Debug-only reference computeKnownBits** | `sub_9AC330` (cross-validation oracle) |
| **computeKnownBitsFromOperator** | `sub_11A3F30` (`0x11A3F30`, 50 KB) |
| **computeKnownBitsFromAssume** | `sub_11A6910` (`0x11A6910`, 12.5 KB) |
| **computeKnownBitsFromRangeMetadata** | `sub_11A68C0` |
| **Post-analysis NVIDIA fixup** | `sub_99B5E0` (alignment + range refinement) |
| **NVIDIA intrinsic known-bits oracle** | `sub_F0C4B0` (special register ranges) |
| **Intrinsic return range analysis** | `sub_10CA790` + `sub_11A1390` |
| **NVVMIntrRange pass** | `sub_216F4B0` (`nvvm-intr-range`) |
| **SelectionDAG computeKnownBits** | `sub_33D4EF0` (`0x33D4EF0`, 114 KB, 3,286 lines) |
| **Pointer alignment known-bits** | `sub_BD5420` (getPointerAlignmentBits) |
| **Debug cross-validation flag** | `qword_4F90C28` (enables abort-on-mismatch) |
| **Max recursion depth** | 6 (checked in `sub_11AE940`) |

## GPU-Specific Known-Bits Sources

The key difference from CPU targets: GPU code has dozens of values with statically knowable ranges that never exist on a CPU. Every CUDA thread reads its identity from special registers whose values are bounded by hardware launch parameters. NVIDIA exploits this in two places: the `nvvm-intr-range` pass adds `!range` metadata at the IR level, and the target-specific known-bits oracle `sub_F0C4B0` provides bitmask information directly to `computeKnownBits`.

### Special Register Range Table

The following ranges apply to every NVVM intrinsic that reads a PTX special register. The `!range` metadata attached by `nvvm-intr-range` (`sub_216F4B0`) encodes `[lo, hi)` as an LLVM `MDNode`. The known-bits column shows which bits are guaranteed zero given the maximum value.

| Register | PTX | NVVM Intrinsic ID Range | Value Range | i32 Known Zero (upper bits) |
|---|---|---|---|---|
| `%tid.x/y/z` | `%tid.x` | 350--352 | `[0, maxntid-1]` | bits `[ceil(log2(maxntid)), 31]` |
| `%ntid.x/y/z` | `%ntid.x` | 353--355 | `[1, 1024]` | bits `[11, 31]` (at most 1024) |
| `%ctaid.x/y/z` | `%ctaid.x` | 356--358 | `[0, gridDim-1]` | bits `[ceil(log2(gridDim)), 31]` |
| `%nctaid.x/y/z` | `%nctaid.x` | 359--361 | `[1, 2^31-1]` | bit 31 (always non-negative) |
| `%warpsize` | `%WARP_SZ` | ~370 | `{32}` (constant) | bits `[0,4]` = `00000`, bit 5 = 1, bits `[6,31]` = 0 |
| `%laneid` | `%laneid` | ~371 | `[0, 31]` | bits `[5, 31]` |
| `%warpid` | `%warpid` | ~372 | `[0, maxWarpsPerSM-1]` | SM-dependent upper bits |
| `%smid` | `%smid` | ~375 | `[0, numSMs-1]` | architecture-dependent |
| `%nsmid` | `%nsmid` | ~376 | `[1, numSMs]` | architecture-dependent |
| `%gridid` | `%gridid` | ~378 | `[0, 2^32-1]` | none (full range) |
| `%clock` | `%clock` | ~380 | `[0, 2^32-1]` | none |
| `%lanemask_eq/lt/le/gt/ge` | `%lanemask_*` | ~382--386 | `[0, 2^32-1]` | none |

When `__launch_bounds__(maxThreadsPerBlock, minBlocksPerMP)` is present on a kernel, `nvvm-intr-range` tightens the `%tid` ranges to `[0, maxThreadsPerBlock-1]` and `%ntid` to `[1, maxThreadsPerBlock]`. Similarly, `nvvm.reqntid` metadata (from `__launch_bounds__` with exact dimensions or `reqntid` pragmas) can constrain each dimension independently to an exact value.

The knob `nvvm-intr-range-sm` (constructor `ctor_359`) selects the SM variant used to determine architectural limits for registers like `%warpid`, `%smid`, and `%nsmid`.

### Address Space Known Bits

CUDA uses separate address spaces with distinct pointer bit-widths and alignment properties. These feed directly into `sub_BD5420` (getPointerAlignmentBits), which OR's known-zero low bits into the KnownBits result for any pointer-typed value:

| Address Space | PTX | Pointer Width | Known Alignment | Known Bits Effect |
|---|---|---|---|---|
| 0 (generic) | default | 64 bits | none guaranteed | pointer alignment only |
| 1 (global) | `.global` | 64 bits | >= 16 bytes (typical) | low 4 bits often known-zero |
| 3 (shared) | `.shared` | 32 bits | >= 4 bytes (minimum) | low 2 bits known-zero, bits `[32,63]` irrelevant |
| 4 (constant) | `.const` | 64 bits | >= 4 bytes | low 2 bits known-zero |
| 5 (local) | `.local` | 32 bits | >= 4 bytes (stack) | low 2 bits known-zero, bits `[32,63]` irrelevant |

The 32-bit address spaces (shared and local) are critical: any value known to be a shared-memory pointer has bits `[32, 63]` entirely dead. The DemandedBits analysis exploits this to eliminate zero-extensions and truncations around shared-memory address calculations, keeping everything in 32-bit arithmetic.

### Launch Parameter Integration

The `__launch_bounds__` attribute, `__maxnreg__` pragma, and `nvvm.reqntid` / `nvvm.maxntid` metadata all flow into the known-bits infrastructure:

1. **nvvm-intr-range pass** (`sub_216F4B0`): Runs early in the pipeline. Reads kernel metadata (`nvvm.reqntid`, `nvvm.maxntid`) via `sub_93AE30`. Attaches `!range` metadata to every `llvm.nvvm.read.ptx.sreg.*` intrinsic call. The metadata format is `!{i32 lo, i32 hi}` where `hi` is exclusive.

2. **computeKnownBitsFromRangeMetadata** (`sub_11A68C0`): Called during standard `computeKnownBits` traversal. Reads `!range` metadata from any value and derives known-zero/known-one masks. For a range `[0, 1024)`, this yields `knownZero = 0xFFFFFC00` (bits 10--31 known zero).

3. **Intrinsic return range analysis** (`sub_10CA790` + `sub_11A1390`): A separate path used when the merged `computeKnownBits+SimplifyDemandedBits` processes ZExt/SExt of intrinsic calls. Computes `[lo, hi]` bounds for the intrinsic's return value and checks whether the extension can be eliminated because the return range fits within the demanded bits.

## The Merged Analysis: Algorithm and Pseudocode

Unlike upstream LLVM where `InstCombiner::SimplifyDemandedBits` calls `computeKnownBits` as a subroutine, cicc fuses them. The entry point `sub_11AE870` wraps `sub_11AE3E0`, which calls the core `sub_11A7600`. A hash table at `InstCombiner + 2064` tracks visited instructions to prevent infinite recursion.

### Core Algorithm

```c
// sub_11A7600 — merged computeKnownBits + SimplifyDemandedBits
// Returns: replacement instruction pointer, or NULL if no simplification
Instruction* computeKnownBitsAndSimplify(
    AnalysisCtx    *ctx,        // a1 — holds IR module, pass info
    IRNode         *inst,       // a2 — instruction to analyze
    APInt          *demanded,   // a3 — which output bits the consumer needs
    KnownBits      *result,     // a4 — output {knownZero, knownOne}
    unsigned        depth,      // a5 — recursion depth (checked in caller)
    QueryState     *state       // a6 — worklist context
) {
    uint8_t opcode = inst->opcode_tag;   // single-byte opcode at offset 0
    unsigned width = demanded->getBitWidth();

    // Stack-allocate 4 APInt accumulators for operand known bits
    APInt kz0(width, 0), ko0(width, 0);  // operand 0
    APInt kz1(width, 0), ko1(width, 0);  // operand 1

    switch (opcode) {
    case '*': // Mul — lines 654-1037
        // Pattern: if one operand is known power-of-2 from intrinsic call,
        //          replace Mul with Shl (critical for threadIdx * stride)
        if (auto *rhs = matchConstantPow2Call(inst->getOperand(1))) {
            if (inst->getOperand(0)->hasOneUse())
                return createShl(inst->getOperand(0), log2(rhs));
        }
        // Generic: narrow demanded mask by leading zeros, propagate to operands
        unsigned effectiveBits = width - demanded->countLeadingZeros();
        APInt narrowDemand = APInt::getLowBitsSet(width, effectiveBits);
        propagateDemandToOperand(ctx, inst, 0, narrowDemand, &kz0, &ko0, depth+1, state);
        propagateDemandToOperand(ctx, inst, 1, narrowDemand, &kz1, &ko1, depth+1, state);
        KnownBits::computeForMul(result, {kz0,ko0}, {kz1,ko1}, inst->hasNUW(), inst->hasNSW());
        break;

    case '6': // ZExt — lines 1677-1919
        // Check if source is intrinsic call with known return range
        if (auto range = getIntrinsicReturnRange(inst->getOperand(0))) {
            if (range.fitsBitWidth(demanded->getActiveBits()))
                return inst->getOperand(0);  // eliminate extension
        }
        // Standard: shift demanded bits down, propagate to source, zext result
        propagateDemandToOperand(ctx, inst, 0, demanded->trunc(srcWidth), ...);
        KnownBits::zext(result, srcWidth);
        break;

    case 'U': // NVIDIA Intrinsic — lines 3521-4085
        unsigned intrinsicID = getIntrinsicID(inst);
        switch (intrinsicID) {
        case 0x0F: handleBFE_BFI(inst, demanded, result); break;
        case 0x42: handlePopcount(inst, demanded, result); break;
        case 0x01: handleAbs(inst, demanded, result);      break;
        case 0xB4: handleFSHL(inst, demanded, result);     break;
        case 0xB5: handleFSHR(inst, demanded, result);     break;
        case 0x12B: handleBswap(inst, demanded, result);   break;
        default:
            // Fall through to NVIDIA intrinsic known-bits oracle
            sub_F0C4B0(inst, result, depth, state);
            break;
        }
        break;

    // ... 13 more opcode cases (Add, Sub, Xor, PHI, Trunc, SExt, etc.)

    default:
        sub_9AC0E0(inst, result, depth, state);  // generic fallback
        break;
    }

    // POST-ANALYSIS REFINEMENT (lines 2134-2281)
    // 1. Pointer alignment: if type is pointer, OR alignment bits into knownZero
    if (inst->getType()->isPointerTy()) {
        unsigned alignBits = getPointerAlignmentBits(inst);  // sub_BD5420
        result->knownZero |= APInt::getLowBitsSet(width, alignBits);
    }

    // 2. Debug cross-validation (when qword_4F90C28 is set)
    if (DEBUG_FLAG) {
        KnownBits reference;
        sub_9AC330(inst, &reference, depth, state);  // independent computation
        if (reference != *result) {
            print("computeKnownBits(): ", reference);
            print("SimplifyDemandedBits(): ", *result);
            abort();
        }
    }

    // 3. Demand-covers-known check: can we replace with constant?
    if (demanded->isSubsetOf(result->knownZero | result->knownOne))
        return ConstantInt::get(inst->getType(), result->knownOne);

    return nullptr;
}
```

### Demand Propagation Per Operand

The trampoline `sub_11AE940` is the per-operand demand propagation entry point. It increments depth, checks the depth limit (`depth > 6` returns all-unknown), and dispatches between the big handler (`sub_11A7600`) and the binary-arithmetic-specific helper (`sub_11A1430`) based on opcode class:

```c
// sub_11AE940 — per-operand demand propagation trampoline
Instruction* propagateDemandToOperand(
    AnalysisCtx *ctx, IRNode *parent, unsigned opIdx,
    APInt *demand, KnownBits *out, unsigned depth, QueryState *state
) {
    if (depth > 6)
        return nullptr;  // MaxAnalysisRecursionDepth reached

    IRNode *operand = parent->getOperand(opIdx);
    uint8_t opcode = operand->opcode_tag;

    // Binary arithmetic subset goes to the helper
    if (opcode == '*' || opcode == '9' || opcode == ':' ||
        opcode == ';' || opcode == ',' || opcode == '8')
        return sub_11A1430(ctx, operand, demand, out, depth, state);

    // Everything else goes to the big merged handler
    return sub_11A7600(ctx, operand, demand, out, depth, state);
}
```

The secondary helper `sub_11A1430` handles Add/Sub/Xor/Mul/BitCast/ExtractElement with a tighter structure: it uses a four-accumulator cascade with three successive `isSubsetOf` checks per operation, which is more aggressive than upstream LLVM's single post-merge check.

### The Four-Accumulator Cascade

For binary operators (Add, Sub, Xor), cicc maintains four APInt accumulators (two per operand) and performs a three-tier check:

```c
// Three-tier demand satisfaction check (sub_11A1430 pattern)
// More aggressive than upstream single-check approach
KnownBits kb0, kb1;
computeKnownBits(op0, &kb0, depth+1, state);
computeKnownBits(op1, &kb1, depth+1, state);
KnownBits merged = mergeForOpcode(kb0, kb1, opcode);
sub_99B5E0(inst, &merged, depth, state);  // NVIDIA post-fixup

// Check 1: merged result covers demand?
if (demanded.isSubsetOf(merged.knownZero | merged.knownOne))
    return ConstantInt::get(merged.knownOne);

// Check 2: union of operand known-bits covers demand?
if (demanded.isSubsetOf((kb0.knownZero | kb1.knownZero) |
                        (kb0.knownOne  | kb1.knownOne)))
    return ConstantInt::get(...);

// Check 3: all accumulated zero|one covers demand?
if (demanded.isSubsetOf(allAccumulatedZero | allAccumulatedOne))
    return followUseDef(...);
```

The post-analysis fixup `sub_99B5E0` is NVIDIA-specific and does not exist in upstream LLVM. It applies additional refinements from thread index range constraints, warp-level uniformity, and shared memory alignment guarantees.

## DemandedBits for GPU: Narrowing Optimizations

The DemandedBits analysis is the backward complement to KnownBits' forward analysis. When a consumer only needs the low N bits of a value, the producer can be narrowed or eliminated. On GPU, this interaction is dramatically more productive than on CPU because of three factors:

1. **32-bit address spaces**: Shared memory (AS 3) and local memory (AS 5) use 32-bit pointers. When address calculations are performed in i64 (as the generic address space requires), the upper 32 bits are entirely undemanded for shared/local accesses. DemandedBits proves this and enables truncation to i32.

2. **Bounded thread indices**: `threadIdx.x * stride + offset` patterns produce values that fit in far fewer bits than i32. If `threadIdx.x < 256` (from `__launch_bounds__`) and `stride < 4096`, the product fits in 20 bits. DemandedBits propagates this, enabling downstream shifts and masks to operate on narrower types.

3. **Type demotion to i16/fp16**: When DemandedBits proves only the low 16 bits of an i32 computation matter, cicc can demote to 16-bit operations. The function at `sub_1185740` (InstCombine's `visitTrunc`) inserts narrowing truncations. This is particularly valuable for texture coordinate calculations and index arithmetic in tensor core operations.

### Dead Bit Elimination

The core optimization check appears approximately 15 times across the analysis functions:

```c
// Inline version (width <= 64):
uint64_t unknown = ~(knownZero | knownOne);
if ((demanded & unknown) == 0) {
    // All demanded bits are determined -> replace with constant
    return ConstantInt::get(type, knownOne);
}

// Wide version (width > 64):
if (demanded.isSubsetOf(knownZero | knownOne)) {
    return ConstantInt::get(type, knownOne);  // sub_AD6220
}
```

This is the heart of the analysis: backward-propagated demand meets forward-propagated known-bits. When they cover every bit the consumer needs, the entire instruction is dead and can be replaced with a compile-time constant.

### GPU Patterns Enabled by Known Bits

The following simplifications are GPU-specific and do not have CPU equivalents:

**Mul to Shl for threadIdx arithmetic** (lines 714--861): When both operands of a multiply originate from intrinsic calls with known power-of-2 returns (e.g., `threadIdx.x * blockDim.x` where blockDim is a power-of-2 from `__launch_bounds__`), the multiply is replaced with a left shift. The pattern matcher checks `sub_BCAC40` (hasOneUse) and `sub_10A0620` (createShl replacement).

**Bswap + BFE fusion** (lines 3959--4007): Detects a byte-swap feeding into a bit-field extract and replaces with a direct byte read at the swapped offset. Common in endianness conversion code for shared memory operations.

**ZExt/SExt elimination via intrinsic return range** (sub_10CA790 path): When a ZExt or SExt extends the result of an NVVM intrinsic call, and the intrinsic's annotated return range fits entirely within the demanded bits, the extension is eliminated. This fires frequently for `threadIdx.x` reads extended to i64 for address calculations.

**BitCast-through-ZExt folding** (`sub_11A1430` at `0x11A2360`): When a BitCast's source is a ZExt and the demanded bits fit within the original narrow type, the bitcast+zext chain collapses to the original value. Common in CUDA address calculations involving zero-extension followed by pointer reinterpretation.

## SelectionDAG computeKnownBits

The DAG-level known-bits analysis at `sub_33D4EF0` (114 KB, 3,286 lines) mirrors the IR-level analysis but operates on `SDNode` opcodes. It handles 112 opcode cases organized into 14 groups.

### NVPTX Target Node Known Bits

For NVPTX-specific DAG opcodes (above `ISD::BUILTIN_OP_END` = 499), the function delegates to `NVPTXTargetLowering::computeKnownBitsForTargetNode` via vtable slot 254 at offset 2032. The key NVPTX-specific cases:

| Opcode Range | NVPTX DAG Node | Known-Bits Behavior |
|---|---|---|
| 0x152--0x161 (338--353) | TEX, SULD, surface ops | Result width known: bits above element size set to zero |
| 0x12A (298) | LoadV2, LoadParam | Extension mode from flags byte bits[2:3]: zext/sext/none |
| 0x16A, 0x16C (362, 364) | StoreParam, StoreRetval | When flags bits[2:3] == 0b11: element type width known |
| 0x175 (373) | ConstantPool | Uses ConstantRange::fromKnownBits intersection |
| 0xCA (202) | INTRINSIC_WO_CHAIN | Boolean-like: bit 0 unknown, bits [1..width] known zero |
| >= 499 | All target-specific | Delegates to vtable[254] computeKnownBitsForTargetNode |

The DAG-level analysis uses the same recursion depth cap of 6 (`a6 > 5` returns all-unknown), matching LLVM's `MaxRecursionDepth`.

### Texture/Surface Fetch Result Width

Cases 0x152--0x161 encode the known bit-width of texture and surface fetch results. For an 8-bit texture fetch zero-extended to i32, the analysis sets bits [8, 31] as known-zero in the result. This enables downstream shift and mask elimination in texture sampling code.

## KnownBits Data Structure Layout

Both the IR-level and DAG-level implementations use the same 32-byte struct:

```c
struct KnownBits {                      // 32 bytes total
    union {
        uint64_t  val;                  // +0x00: inline storage (width <= 64)
        uint64_t *ptr;                  // +0x00: heap pointer  (width > 64)
    } knownZero;
    uint32_t knownZero_width;           // +0x08: bit-width
    uint32_t _pad0;                     // +0x0C: padding
    union {
        uint64_t  val;                  // +0x10: inline storage (width <= 64)
        uint64_t *ptr;                  // +0x10: heap pointer  (width > 64)
    } knownOne;
    uint32_t knownOne_width;            // +0x18: bit-width
    uint32_t _pad1;                     // +0x1C: padding
};

// Invariant: (knownZero & knownOne) == 0   (no bit both 0 and 1)
// Threshold: width > 64 triggers heap allocation via sub_C43690
```

Roughly 43% of `sub_11A1430`'s binary size consists of APInt destructor sequences (`cmp [rbp+var], 0x40; jbe skip; call free`) for the width > 64 cleanup paths.

## Configuration

| Knob | Source | Default | Effect |
|---|---|---|---|
| `nvvm-intr-range-sm` | `ctor_359` | Current target SM | SM variant used to compute special register ranges for `nvvm-intr-range` pass |
| `scev-cgp-tid-max-value` | `ctor_XXX` | Architecture limit | Maximum value of thread ID used in SCEV-based CodeGenPrep address calculations |
| `nv-remat-threshold-for-spec-reg` | `unk_4FD3860` | 20 | Threshold controlling when special register reads are rematerialized instead of spilled (interacts with known-bits because remat preserves range metadata) |
| `qword_4F90C28` | internal debug flag | 0 (disabled) | Enables cross-validation abort: runs independent reference `computeKnownBits` (`sub_9AC330`) and aborts if results disagree with merged analysis |
| Max recursion depth | hardcoded | 6 | Matches LLVM's `MaxAnalysisRecursionDepth`; checked in `sub_11AE940` |
| APInt inline threshold | hardcoded | 64 bits | Values <= 64 bits use inline uint64 storage; wider values heap-allocate |

## Diagnostic Strings

The merged analysis emits the following diagnostics (only in debug/assert builds when `qword_4F90C28` is set):

| String | Location | Trigger |
|---|---|---|
| `"computeKnownBits(): "` | `sub_11A7600` line ~2204 | Cross-validation mismatch: prints the reference implementation's result |
| `"SimplifyDemandedBits(): "` | `sub_11A7600` line ~2208 | Cross-validation mismatch: prints the merged analysis result |
| `"Mismatched known bits for <inst> in <func>"` | `sub_11A7600` line ~2200 | Precedes the two values above; followed by `abort()` |

The `nvvm-intr-range` pass emits:

| String | Location |
|---|---|
| `"Add !range metadata to NVVM intrinsics."` | `sub_216F4B0` (pass registration) |

## NVVM IR Node Layout

The KnownBits analysis traverses IR nodes using cicc's internal representation. Each node is 32 bytes:

```c
struct IRNode {             // 32 bytes (0x20)
    uint8_t  opcode;        // +0x00: single-byte opcode tag (ASCII-based)
    uint8_t  flags;         // +0x01: bit 1, bit 2 = nsw/nuw flags
    uint16_t _reserved;     // +0x02
    uint32_t operand_idx;   // +0x04: 27-bit operand index + 5-bit flags
                            //        byte 7 bit 6 (0x40) = use-list vs indexed
    // ... remaining 24 bytes: use-list pointers, type info, metadata
};

// Operand resolution:
// If byte[7] & 0x40 (use-list flag set):
//     operand = *(node - 8) -> *(ptr + 0x20)
// If byte[7] & 0x40 == 0 (indexed):
//     idx = (node[4..7] & 0x7FFFFFF)
//     operand = node - (idx << 5)    // 27-bit index * 32 bytes
```

The 27-bit index allows up to 134 million nodes (4 GB theoretical IR size).

## Function Map

### IR-Level Known-Bits

| Function | Address | Size |
|---|---|---|
| `computeKnownBitsAndSimplify` — merged main analysis | `sub_11A7600` | 127 KB |
| `SimplifyDemandedBitsHelper` — binary arithmetic subset | `sub_11A1430` | 6.3 KB |
| Per-operand demand propagation trampoline (depth check) | `sub_11AE940` | varies |
| SimplifyDemandedBits entry wrapper (allocates APInts) | `sub_11AE870` | thin |
| SimplifyDemandedBits result caching (hash table at IC+2064) | `sub_11AE3E0` | 235 lines |
| `computeKnownBitsFromOperator` / PHI merge | `sub_11A3F30` | 50 KB |
| `computeKnownBitsFromAssume` (processes `@llvm.assume`) | `sub_11A6910` | 12.5 KB |
| `computeKnownBitsFromRangeMetadata` (reads `!range`) | `sub_11A68C0` | varies |
| Generic `computeKnownBits` (fallback, no simplification) | `sub_9AC0E0` | varies |
| Reference `computeKnownBits` (debug cross-validation only) | `sub_9AC330` | varies |
| NVIDIA post-analysis fixup (alignment + range refinement) | `sub_99B5E0` | varies |
| NVIDIA intrinsic known-bits oracle (special registers) | `sub_F0C4B0` | varies |
| `isNVVMFunction` check (NVIDIA-specific flag) | `sub_F0C3D0` | varies |
| Intrinsic return range analysis (computes `[lo, hi]`) | `sub_10CA790` | 11.2 KB |
| Extract return range bounds from range analysis result | `sub_11A1390` | varies |
| `getPointerAlignmentBits` (alignment-derived known zeros) | `sub_BD5420` | varies |
| `isDemandedBitsFullyKnown` (demand subset-of known) | `sub_10024C0` | varies |
| `NVVMIntrRange` pass — attaches `!range` metadata | `sub_216F4B0` | varies |

### SelectionDAG-Level Known-Bits

| Function | Address | Size |
|---|---|---|
| `SelectionDAG::computeKnownBits` (recursive, 112 opcode cases) | `sub_33D4EF0` | 114 KB |
| Creates all-demanded mask, delegates to `sub_33D4EF0` | `sub_33DD090` | wrapper |
| `computeMinLeadingZeros` (calls `sub_33D25A0` + returns) | `sub_33D4D80` | wrapper |
| `computeNumSignBits` (parallel switch structure) | `sub_33D25A0` | 49 KB |
| `computeOverflowForAdd` / `computeOverflowForSub` | `sub_33DCF10` | varies |

### KnownBits Arithmetic Helpers

| Function | Address |
|---|---|
| `KnownBits::computeForMul(result, nuw, nsw, kb0, kb1)` | `sub_C70430` |
| `KnownBits::add(a, b, nsw, nuw, carry)` | `sub_C74E10` |
| `KnownBits::sub(a, b, nsw, nuw)` | `sub_C75B70` |
| `KnownBits::computeForAddSub(isSub, nsw, nuw, a, b)` | `sub_C76560` |
| `KnownBits::shl(a, shamt)` | `sub_C73220` |
| `KnownBits::lshr(a, b)` | `sub_C738B0` |
| `KnownBits::ashr(a, b)` | `sub_C73E40` |
| `KnownBits::and(a, b, commutative)` | `sub_C787D0` |
| `KnownBits::or(a, b)` | `sub_C78F20` |
| `KnownBits::xor(a, b)` | `sub_C790F0` |
| `KnownBits::mergeForPHI` / `smax(a, b)` | `sub_C79480` |
| `KnownBits::truncate` / `smulh(a, b)` | `sub_C7B4D0` |
| `KnownBits::cttz(a, shift)` | `sub_C7BCF0` |
| `KnownBits::ctpop(a)` | `sub_C7BD50` |
| `KnownBits::bswap(a)` | `sub_C7BDB0` |
| `KnownBits::abs(a, known_shift)` | `sub_C746C0` |
| `KnownBits::umin(a, b)` | `sub_C740A0` |
| `KnownBits::umax(a, b)` | `sub_C74180` |
| `KnownBits::ctlz(a, poisonAtZero)` | `sub_C778B0` |

### APInt Utilities

| Function | Address |
|---|---|
| `APInt(width, 0)` — zero-init constructor (heap for width > 64) | `sub_C43690` |
| `APInt` copy constructor | `sub_C43780` |
| `APInt::operator&=` | `sub_C43B90` |
| `APInt::operator\ | `sub_C43BD0` |
| `APInt::setBits(lo, hi)` | `sub_C43C90` |
| `APInt::flipAllBits` | `sub_C43D10` |
| `APInt::trunc(width)` | `sub_C44740` |
| `APInt::zext(width)` | `sub_C449B0` |
| `APInt::sext(width)` | `sub_C44830` |
| `APInt::countTrailingZeros` | `sub_C44590` |
| `APInt::countLeadingZeros` | `sub_C444A0` |
| `APInt::countPopulation` | `sub_C44630` |
| `APInt::isSubsetOf(other)` | `sub_C446F0` |
| `APInt::reverseBits` / `byteSwap` | `sub_C44AB0` |
| `ConstantInt::get(type, APInt)` — creates constant replacement | `sub_AD6220` |
| `ConstantInt::get(type, value, isSigned)` | `sub_AD64C0` |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Analysis architecture** | Separate `computeKnownBits` (ValueTracking) and `SimplifyDemandedBits` (InstCombine) | Fused into single 127 KB function (`sub_11A7600`) that simultaneously computes bitmasks and simplifies instructions |
| **GPU register ranges** | No special register concept; all values have full-width range | Dedicated oracle (`sub_F0C4B0`) provides known-zero bits for `%tid`, `%ntid`, `%ctaid`, `%warpsize`, `%laneid`, and 10+ PTX special registers |
| **Range metadata injection** | No equivalent pass; range info comes from profile data or programmer annotations | `nvvm-intr-range` pass (`sub_216F4B0`) attaches `!range` metadata to every special-register read; tightened by `__launch_bounds__` |
| **Warp size** | Not a concept; no constant is known | `%warpsize` is statically known to be exactly 32 (known-zero bits `[0,4]` and `[6,31]`, bit 5 = 1) |
| **Cross-validation** | No cross-validation in release builds | Debug flag `qword_4F90C28` enables abort-on-mismatch between `computeKnownBits` and `SimplifyDemandedBits` results |
| **SelectionDAG integration** | Separate DAG-level `computeKnownBits` (~60 KB) | Extended DAG-level version at `sub_33D4EF0` (114 KB, 3,286 lines) with GPU-specific value tracking |
| **Max recursion depth** | 6 (configurable) | Same default 6, checked in `sub_11AE940` with identical semantics |

## Cross-References

- [InstCombine](instcombine.md) — The primary consumer of KnownBits analysis; `sub_11AE870` is called from the binary operator visitor's Phase 0
- [SelectionDAG](selectiondag.md) — DAG-level known-bits at `sub_33D4EF0` feeds into DAGCombine and instruction selection pattern matching
- [Loop Strength Reduction](lsr.md) — LSR interacts with shared-memory known-bits through the `lsr-no-ptr-address-space3` knob that disables LSR for 32-bit shared memory pointers
- GVN — `sub_9AC330` (reference computeKnownBits) is also called from GVN to validate value numbering decisions
- LICM — Loop-invariant code motion uses known-bits to prove that hoisted expressions are safe (no integer overflow when known-bits constrain the range)
