# NVVM IR Verifier (Deep Dive)

The NVVM IR Verifier (`nvvm-verify`) is NVIDIA's three-layer correctness gate that runs between optimization passes throughout the CICC pipeline. Unlike LLVM's generic `Verifier` pass, which validates structural IR invariants, this pass enforces the complete NVVM IR contract: valid target triples, legal address space usage, architecture-gated intrinsic availability, MMA dimension/type constraints, function attribute restrictions, and atomic operation rules. It is the single largest verification subsystem in CICC at approximately 230KB across three cooperating functions. The verifier is inserted at roughly a dozen points in every optimization tier, guarded only by `NVVMPassOptions[600]` (disable). Every NVVM intrinsic call, every address space cast, and every unsupported CPU-oriented feature triggers a check here; failure produces a diagnostic message and sets the module error flag, but compilation continues to collect as many errors as possible in a single run.

## Key Facts

| Property | Value |
|---|---|
| Pass name | `nvvm-verify` |
| Pass class | `llvm::NVVMIRVerifierPass` |
| Registration | `sub_2342890` (New PM), `sub_12E54A0` (pipeline builder) |
| Entry point | `sub_12D4560` |
| Module verifier | `sub_2C80C90` (51KB, ~1671 lines) |
| Function verifier | `sub_2C771D0` (36KB, ~1165 lines) |
| Intrinsic verifier | `sub_2C7B6A0` (143KB, ~4139 lines) |
| Binary size | ~230KB decompiled |
| Pipeline slot | ~12 per tier (O1-O3), after GVN, after DSE, after LICM, etc. |
| Disable flag | `NVVMPassOptions[600]` (bool) |
| Primary knobs | `nvvm-verify-show-info` |
| Error model | Accumulate-and-continue (no early abort) |
| SM encoding | Internal SM * 10 (e.g., sm_90 = 900) at context offset +8 |
| Upstream equivalent | None -- fully proprietary |

## Three-Layer Verification Architecture

The pass operates as three nested verification functions. The module verifier is the entry point; it calls the function verifier once per function, and the function verifier dispatches to the intrinsic verifier for every intrinsic call instruction.

```
sub_2C80C90 (NVVMModuleVerifier)
  |
  +-- Validate data layout string
  +-- Validate target triple against whitelist
  +-- sub_2C797D0() for each global variable
  +-- sub_2C7A130() for each function declaration
  +-- sub_2C7AA20() for each named metadata node
  |
  +-- For each function:
  |     |
  |     +-- sub_2C771D0 (NVVMFunctionVerifier)
  |     |     +-- Cluster dimension validation (Hopper+ gate)
  |     |     +-- Parameter width validation (>=32-bit or sext/zext)
  |     |     +-- Function attribute rejection (17 attributes)
  |     |     +-- Entry/exit handler constraints
  |     |
  |     +-- For each instruction in each basic block:
  |           |
  |           +-- Switch on opcode 0x1E..0x60
  |           +-- Opcode 0x55 (intrinsic call) --> sub_2C7B6A0
  |                 (NVVMIntrinsicVerifier, 143KB)
  |                 +-- Switch on intrinsic ID
  |                 +-- SM version gate checks
  |                 +-- Type, address space, constant arg validation
  |                 +-- MMA shape/type cross-validation
```

### Context Object Layout

All three verifiers share a context object passed as the first argument:

| Offset | Type | Field |
|---|---|---|
| 0 | int32 | Mode (0 = standard, 1 = UnifiedNVVMIR) |
| 4 | int32 | Flags |
| 8 | int32 | SM version (SM * 10, e.g., 900 for sm_90) |
| 16 | ptr | Error flag pointer (set on any validation failure) |
| 24 | ptr | Error output stream |

## Target Triple Whitelist

The module verifier validates the module's target triple against two whitelists depending on mode.

### UnifiedNVVMIR Mode (mode == 1) -- Exact Match

Eight triples are accepted:

| Triple | Arch | API |
|---|---|---|
| `nvptx-nvidia-cuda` | 32-bit PTX | CUDA |
| `nvptx64-nvidia-cuda` | 64-bit PTX | CUDA |
| `nvptx-nvidia-nvcl` | 32-bit PTX | OpenCL |
| `nvptx64-nvidia-nvcl` | 64-bit PTX | OpenCL |
| `nvsass-nvidia-cuda` | SASS direct | CUDA |
| `nvsass-nvidia-nvcl` | SASS direct | OpenCL |
| `nvsass-nvidia-directx` | SASS direct | DirectX |
| `nvsass-nvidia-spirv` | SASS direct | SPIR-V |

The `nvsass` triples confirm that CICC can compile directly to native GPU assembly (SASS) without the PTX intermediate step, and can do so for DirectX shader and SPIR-V/Vulkan shader pipelines. This reveals CICC's role in NVIDIA's shader compiler toolchain beyond CUDA.

Failure message: `"Invalid target triple"`.

### Standard Mode (mode != 1) -- Prefix + Suffix Match

The triple must begin with `"nvptx-"` or `"nvptx64-"` and end with `"-cuda"`. The middle component is wildcarded.

Failure message: `"Invalid target triple (<actual>), must be one of:"` followed by `"nvptx-*-cuda"` and `"nvptx64-*-cuda"`.

### Data Layout Validation

If the module's data layout string is empty: `"Empty target data layout, must exist"`.

Otherwise, `sub_2C74F70` parses and validates the layout. On failure, the verifier prints `"Example valid data layout:"` with reference strings from:

| Global | Description |
|---|---|
| `off_4C5D0A0` | 32-bit layout example |
| `off_4C5D0A8` | 64-bit layout example |
| `off_4C5D070` | 64-bit with mixed pointer widths (p3:32:32:32) |

## Per-Instruction Validation (Module Verifier)

After calling `sub_2C771D0` for function-level checks, the module verifier iterates every instruction in every basic block and dispatches on the LLVM IR opcode. The opcode range is 0x1E through 0x60:

| Opcode | IR Instruction | Validation |
|---|---|---|
| 0x1F | `call` (non-intrinsic) | Calls `sub_2C795F0`. Checks for `"pragma"` metadata; rejects `"unroll"` pragma with: `"pragma unroll is not supported. Please use llvm.loop.unroll.count instead"`. Validates branch pragma operand count. |
| 0x21 | `indirectbr` | Rejected via `sub_2C76F10(ctx, "indirectbr", instr)` |
| 0x22 | `invoke` | Rejected via `sub_2C76F10(ctx, "invoke", instr)` |
| 0x23 | `resume` | Rejected via `sub_2C76F10(ctx, "resume", instr)` |
| 0x3C | `alloca` | Alignment must be <= 2^23. Address space must be Generic (AS 0): `"Allocas are not supported on address spaces except Generic"` |
| 0x3D | `load` | Rejects atomic loads: `"Atomic loads/stores are not supported"`. Rejects tensor memory (AS 6): `"Tensor Memory loads/stores are not supported"` |
| 0x3E | `store` | Same atomic and tensor memory checks as load |
| 0x40 | `fence` | In UnifiedNVVMIR mode: only `acq_rel` and `seq_cst` allowed. Otherwise: rejected entirely via `sub_2C76F10` |
| 0x41 | `cmpxchg` | Only i32/i64/i128 types. Pointer must be in generic, global, or shared AS |
| 0x42 | *(GEP/addrspacecast helper)* | Calls `sub_2C7AF00` |
| 0x4F | `addrspacecast` | Validates source and target AS are in range. `"Cannot cast non-generic pointer to different non-generic pointer"` -- at least one side must be AS 0 (generic) |
| 0x55 | `call` (intrinsic) | Dispatches to `sub_2C7B6A0` (NVVMIntrinsicVerifier) |
| 0x5F | `landingpad` | Rejected: `"landingpad"` unsupported |

The unsupported instructions -- `indirectbr`, `invoke`, `resume`, `landingpad` -- are CPU exception-handling features with no GPU equivalent. Their rejection at the IR level prevents downstream passes from encountering them.

### Address Space Casting Rules

The `addrspacecast` validation enforces NVIDIA's GPU address space model:

```
Rule: At least one operand of addrspacecast must be AS 0 (generic).
      Non-generic-to-non-generic casts are illegal.

Legal:   addrspacecast i32* addrspace(0) to i32* addrspace(1)  ; generic -> global
Legal:   addrspacecast i32* addrspace(3) to i32* addrspace(0)  ; shared -> generic
Illegal: addrspacecast i32* addrspace(3) to i32* addrspace(1)  ; shared -> global
```

The valid address space range check uses the expression `(AS + ~2) & 0xFFFFFF) > 2`, which means AS values 0 (generic), 1 (global), and 3 (shared) are always valid for atomic and cast operations. AS 2 (constant) and higher values have restricted usage contexts.

## Function Attribute Rejection

The function verifier (`sub_2C771D0`) rejects 17 LLVM function attributes that have no GPU meaning. Each is identified by its LLVM attribute kind ID:

| Attr ID | Attribute Name | Error Message |
|---|---|---|
| 4 | `builtin` | `"builtin function attribute is not supported."` |
| 17 | `jumptable` | `"jumptable function attribute is not supported."` |
| 20 | `naked` | `"naked function attribute is not supported."` |
| 23 | `nobuiltin` | `"nobuiltin function attribute is not supported."` |
| 30 | `noimplicitfloat` | `"noimplicitfloat function attribute is not supported."` |
| 35 | `noredzone` | `"noredzone function attribute is not supported."` |
| 42 | `nonlazybind` | `"nonlazybind function attribute is not supported."` |
| 53 | `returns_twice` | `"returns_twice function attribute is not supported."` |
| 55 | `safestack` | `"safestack function attribute is not supported."` |
| 56 | `sanitize_address` | `"sanitize_address function attribute is not supported."` |
| 59 | `sanitize_memory` | `"sanitize_memory function attribute is not supported."` |
| 63 | `sanitize_thread` | `"sanitize_thread function attribute is not supported."` |
| 69 | `ssp` | `"ssp function attribute is not supported."` |
| 70 | `sspreq` | `"sspreq function attribute is not supported."` |
| 71 | `sspstrong` | `"sspstrong function attribute is not supported."` |
| 86 | `alignstack` | `"alignstack function attribute is not supported."` |
| 95 | `uwtable` | `"uwtable function attribute is not supported."` |

These attributes fall into four categories: (1) CPU ABI (`naked`, `alignstack`, `noredzone`), (2) security hardening (`ssp`/`sspreq`/`sspstrong`, `safestack`, sanitizers), (3) EH-related (`uwtable`, `returns_twice`, `personality`), and (4) linker features (`jumptable`, `nonlazybind`, `builtin`, `nobuiltin`). None have GPU equivalents.

### Additional Function-Level Checks

| Check | Error Message | Notes |
|---|---|---|
| Cluster dimensions on pre-Hopper | `"Cluster dimensions and cluster maximum blocks are not supported on pre-Hopper Architectures"` | SM version <= 899 (i.e., before sm_90) |
| Cluster dims on non-kernel | `"Cluster dimensions and cluster maximum blocks are only allowed for kernel functions"` | Checked via `sub_CE9220` |
| Partial zero cluster dims | `"If any cluster dimension is specified as 0 then all other dimensions must be specified as 0"` | |
| Zero max cluster blocks | `"Cluster maximum blocks must be non-zero"` | |
| Narrow int param without sign attr | `"Integer parameter less than 32-bits without sext/zext flag"` | PTX requires >=32-bit params |
| Narrow int return without sign attr | `"Integer return less than 32-bits without sext/zext flag"` | |
| InReg attribute | `"InReg attribute on parameter will be ignored"` | Warning only |
| Nest attribute | `"Nest attribute on parameter will be ignored"` | Warning only |
| Explicit section | `"Explicit section marker <name> is not allowed."` | |
| Explicit alignment | `"Explicit alignment is not allowed."` | |
| Prefix data | `"Prefix data is not allowed."` | CPU feature |
| Prologue data | `"Prologue data is not allowed."` | CPU feature |
| Personality function | `"Personality function is not allowed."` | EH feature |
| GC names | `"GC names are not supported."` | |
| Non-void kernel/entry | `"non-void entry function."` | Return type must be void |
| Entry with params | `"entry function with parameters."` | Non-kernel entries only |
| Non-void exit handler | `"non-void exit handler function."` | |
| Exit handler with params | `"exit handler function with parameters."` | |

## Architecture Gates (SM-Gated Features)

The intrinsic verifier (`sub_2C7B6A0`) uses the SM version stored at context offset +8 (encoded as SM*10) to gate feature availability. The threshold checks use `<=`, so e.g. `<= 899` means "below sm_90".

| SM Gate | Threshold | Intrinsics / Features | Error Message |
|---|---|---|---|
| sm_70 (Volta) | <= 699 | `llvm.nvvm.branch.if.all.convergent` (ID 0x205A) | `"...not supported on pre-Volta Architectures"` |
| sm_72 (Volta+) | <= 719 | `llvm.nvvm.cvt` base conversion (ID 0x2106) | `"this instrinsic is only supported for Volta (sm_72)+"` |
| sm_75 (Turing) | <= 749 | `cvt` extended types -- BF16, TF32 conversions (within ID 0x2106) | `"conversion type only supported for Turing (sm_75)+"` |
| sm_80 (Ampere) | <= 799 | `llvm.nvvm.branch.if.convergent` (ID 0x205B) | `"...not supported on pre-Ampere Architectures"` |
| sm_89 (Ada) | <= 889 | Extended type conversion intrinsic (ID 0x2107) | `"this instrinsic is only supported for Ada (sm_89)+"` |
| sm_90 (Hopper) | <= 899 | TMA, async copy (IDs 0x2279, 0x232D), cluster dims, bulk async (IDs 0x244D-0x2459, 0x2487-0x2489) | `"this intrinsic is only supported for Hopper+"` |
| sm_90 (Hopper) | <= 899 | 64-bit pointer requirement for TMA | `"this intrinsic is only supported when pointer size is >= 64 bits"` |
| sm_100+ (Blackwell) | <= 1199 | `.offset.bindless` intrinsics (checked via `sub_CEA320`) | `".offset.bindless intrinsics are not supported on pre-Blackwell architectures"` |

Note the typo `"instrinsic"` in the Volta and Ada messages -- this is present in the binary. The Blackwell gate threshold of 1199 means the `.offset.bindless` intrinsics are available on sm_120 (value 1200) and above, covering all Blackwell-generation architectures including consumer (sm_120/121) and datacenter (sm_100/103).

## Intrinsic Verification Categories

The intrinsic verifier is a single monolithic switch on the NVVM internal intrinsic ID (stored at function value offset +36). The 143KB function covers 26+ validation categories. The sections below group the major ones; reduction (`nvvm.red`), fence, `setmaxnreg`, `tcgen05.cp`, and arch-gated MMA variants each have dedicated check blocks documented in subsections G.1-G.3, O.1, P, and Q.

### A. Constant Argument Validation

Many NVVM intrinsics require one or more arguments to be compile-time constants (typically mode selectors, masks, or task IDs):

- `"arg0 of intrinsic not constant"`
- `"op0 of intrinsic not constant"` / `"op1 of intrinsic not constant"`
- `"Flag argument must be an immediate."`
- `"the task_id parameter must be constant"`
- `"the mask parameter must be constant"`
- `"Mode operand must be constant"`

### B. Rounding Mode Validation

```
Rounding mode encoding: bits[2:0] of the mode word
Valid range: 1..4 (round-to-nearest-even, round-down, round-up, round-to-zero)
Reject: value == 0 or value > 4
Message: "rounding mode not a valid value"
```

### C. Subword Mode Validation

For conversion intrinsics that operate on sub-word portions:

```
Source subword mode:  bits[9:7], valid range 0..2
Dest subword mode:    bits[12:10], valid range 0..2
Messages: "src subword mode not a valid value"
          "dest subword mode not a valid value"
```

### D. Reserved Bits Checking

Multiple locations verify that high/reserved bits in mode words are zero:

- `"reserved flag bits used"`

This prevents future-proofing conflicts if NVIDIA later assigns meaning to currently reserved fields.

### E. Address Space Validation

Intrinsics that access memory enforce specific address space requirements:

| Check | Message |
|---|---|
| Global pointer required | `"pointer address space not global"` |
| Invalid arg1 address space | `"arg1 invalid addrspace"` |
| Arg0 must be pointer | `"arg0 of intrinsic not pointer"` |
| Constant AS required | `"Operand must be in constant address space"` |
| Memcpy/memmove targets constant AS | `"memmove/memcpy cannot target constant address space"` |
| Memset targets constant AS | `"memset cannot point to constant address space"` |
| Stack ops require local AS (5) | `"llvm.nvvm.stackrestore is only supported with local address space pointers"` |
| Stack ops require local AS (5) | `"llvm.nvvm.stacksave is only supported with local address space pointers"` |

### F. Type Validation

| Check | Message |
|---|---|
| bswap operand | `"Invalid type for bswap, need i16, i32, or i64"` |
| ctpop/ctlz/cttz operand | `"Invalid type for ctpop/ctlz/cttz, need i8, i16, i32, ..."` (i64) |
| Arithmetic overflow | `"Invalid type for arithmetic overflow intrinsic, need i16, i32, or i64"` |
| Inline asm type | `"Invalid type in inline assembly, must be i1, i8, i16, i32, i64, float, or double"` |
| MMA element | `"op1 of intrinsic not containing f32 or i32 element"` |

Inline assembly type validation uses a bitmask check: valid bit widths are 1, 8, 16, 32, 64 (encoded as `0x1000000010001` for fast lookup).

### G. Atomic Intrinsic Validation

| Check | Message |
|---|---|
| CAS opcode mismatch | `"the opcode of atomic_cas must be CAS"` |
| RMW opcode error | `"the opcode of atomic_rmw must not be CAS, CAST or CAST_SPIN"` |
| CAST opcode error | `"the opcode of atomic_cast must be CAST or CAST_SPIN"` |
| CAST type restriction | `"atomic.cast only overloads on i32 and i64"` |
| CAST pointer restriction | `"atomic.cast is only allowed on shared pointers"` |
| CAST ordering restriction | `"atomic.cast works on shared memory, so cannot be ordered"` |
| Vector RMW opcode | `"the opcode of atomic_rmw_v2f32 and atomic_rmw_v4f32 must be FADD"` |
| Global ordering scope | `"Global ordering on atomics is only allowed on generic/global pointers"` |
| Ordering mode | `"ordering mode not a valid value"` |
| Scope mode | `"scope mode not a valid value"` |
| Unsupported RMW ordering | `"unsupported ordering for nvvm.atomic.rmw"` |
| Cache hint | `"Cache operation hint not a valid value"` |
| Operation mode | `"operation mode not a valid value"` |
| 128-bit gate | `"128b atomics not supported on this architecture!"` (pre-sm_90) |
| Vector atomic gate | `"vector atomics not supported on this architecture!"` (pre-sm_90) |

### G.1. NVVM Reduction (`nvvm.red`) Validation

The `nvvm.red` family (warp/CTA-wide reduction operators) has its own dedicated validation block, separate from generic atomics. The pointer operand identifies the destination memory; the operator selects the reduction kernel; the type-and-vector-length pair must match the hardware-supported reduction operator set.

| Check | Message |
|---|---|
| Pointer AS (top-level intrinsic AS field) | `"Invalid address space for nvvm.red"` |
| Pointer operand AS (the actual pointer's AS) | `"Invalid address space for pointer operand in nvvm.red"` |
| Reduction opcode field | `"Invalid reduction op for nvvm.red"` |
| Reduction element type field | `"Invalid reduction type for nvvm.red"` |
| Op/type cross-check | `"Invalid op and type combination for nvvm.red"` |
| Vector length vs type | `"Invalid type and vector length for nvvm.red"` |
| Element IR type | `"Invalid Type for nvvm.red"` |
| Memory ordering | `"Invalid memory model ordering for nvvm.red"` |

### G.2. Fence Intrinsic Validation

In addition to the IR-level `fence` instruction (opcode 0x40, see [Per-Instruction Validation](#per-instruction-validation-module-verifier)), there is a dedicated `llvm.nvvm.fence` intrinsic whose opcode/scope/ordering tuple is validated independently.

| Check | Message |
|---|---|
| Opcode field | `"Invalid opcode for nvvm_fence_opcode intrinsic"` |
| Ordering whitelist | `"Invalid ordering for fence, only acq_rel and seq_cst are supported."` |
| Scope/ordering pair | `"Unsupported "{}" ordering and "{}" scope for fence."` |
| Acquire/release/acq_rel scope | `"Unsupported scope "{}" for acquire/release/acq_rel fence."` |
| seq_cst scope | `"Unsupported scope "{}" for seq_cst fence."` |
| Wait operation ordering | `"relaxed memory ordering is not allowed on 'wait' operation"` |

### G.3. Register Count (`nvvm.setmaxnreg`) Validation

The Hopper register-reallocation intrinsic accepts only specific count values:

| Check | Message |
|---|---|
| Range | `"reg_count argument to nvvm.setmaxnreg must be within [24, 256]"` |
| Granularity | `"reg_count argument to nvvm.setmaxnreg must be in multiples of 8"` |

Combined, the legal `reg_count` set is `{24, 32, 40, ..., 248, 256}` -- 30 distinct values.

### H. Texture/Surface Validation

| Check | Message |
|---|---|
| Texture dimensionality | `"dimensionality not a valid value"` |
| LOD adjust | `"LOD Adjust mode not a valid value"` |
| Binding mode | `"Binding Mode is not a valid value"` |
| Border mode | `"border mode not a valid value"` |
| Address mode | `"address mode not a valid value"` |
| Scope | `"scope not a valid value"` |
| Semantic mode | `"semantic mode not a valid value"` |
| Query mode | `"query mode is not a valid value"` |
| Handle source | `"Op0 of nvvm.texsurf.handle must be a metadata wrapper around a tex/surf GlobalVariable"` |
| Handle (short form) | `"nvvm_texsurf_handle op0 must be metadata wrapping a GlobalVariable"` |
| Global declaration AS | `"Texture/surface variables must be global address space"` |
| Deprecated desc | `"Desc parameter is deprecated and should be undef."` (IDs 8937, 9549) |

### H.1. Barrier Pointer Validation

| Check | Message |
|---|---|
| Barrier intrinsic pointer AS | `"Barrier pointer must be in shared memory space"` |

Mbarrier / arrive-wait synchronization objects live exclusively in `addrspace(3)` (shared); a generic or global pointer is rejected.

### I. SATF (Saturate-to-Float) Validation

For math intrinsics with saturation control (IDs 0x2281-0x229C, covering fma/mul/add variants):

```
Message: "satf operand must be a constant zero"
```

The `satf` parameter was deprecated but the intrinsic signatures retain it for ABI compatibility. The verifier enforces it must be zero.

### J. Constant Load Validation

For ID 0x2310 (constant bank load):

| Check | Message |
|---|---|
| Load kind | `"Invalid constant load kind"` |
| Bound bank type | `"Bound bank must be i32"` |
| Bindless bank type | `"Bindless bank must be i64"` |

### K. TMA/Shared Memory Validation

For IDs 0x2319-0x231B:

| Check | Message |
|---|---|
| Column-major restriction | `"ColMajor is not supported for this size"` |
| Size encoding | `"Invalid size"` (bits[3:1] > 4) |

### L. Load Bounds Check

For ID 0x231C:

```
Validation: (value & 7) must be <= 2
Message: "invalid load bounds check type"
Also: "pointer address space not global"
```

### M. Convergent Branch Result Validation

For IDs 8282 (`llvm.nvvm.branch.if.all.convergent`) and 8283 (`llvm.nvvm.branch.if.convergent`):

```
Message: "result of llvm.nvvm.branch.if.convergent and
          llvm.nvvm.branch.if.all.convergent can only be
          used by exactly one branch instruction"
```

This enforces that the convergent branch intrinsic's boolean result flows directly to a single terminator branch, preventing misuse that would break convergence guarantees.

### N. MMA (Matrix Multiply-Accumulate) Validation

The most complex validation category (ID 0x2366 = 9062). Validates WMMA/MMA intrinsics against a multidimensional constraint space:

**Opcode byte encoding:**

| Byte | Bits | Field |
|---|---|---|
| byte0 | [2:0] | Rounding mode |
| byte0 | [7:4] | MMA opcode |
| byte1 | all | A matrix element type (1-13, lookup via `dword_43A2620`) |
| byte2 | all | B matrix element type |
| byte4 | all | MNK dimension encoding (cases 1-0x19) |
| byte5 | all | Additional type info |

**MNK dimension decoding (selected cases):**

| Encoding | M | N | K | Notes |
|---|---|---|---|---|
| 1 | 8 | 8 | 8 | Legacy HMMA |
| 0x10 | 16 | 8 | 8 | |
| 0x17 | 16 | 8 | 16 | |
| 0x18 | 32 | 8 | 8 | |
| 0x19 | 16 | 8 | 16 | |

**Validation checks:**

| Check | Message |
|---|---|
| MNK dimensions | `"Invalid MMA MNK"` |
| A element type | `"Invalid MMA AType"` |
| Fragment A bit width | `"Invalid MMA FragASize"` |
| Fragment B bit width | `"Invalid MMA FragBSize"` |
| Fragment C bit width | `"Invalid MMA FragCSize"` |
| Fragment A IR type | `"Invalid fragA type"` |
| Rounding mode | `"Invalid MMA Rounding Mode"` |
| MMA opcode | `"Invalid MMA Opcode"` |
| A/B type match | `"Mismatched MMA A B Type"` |
| Fragment element consistency | `"Mismatched fragA, fragB and fragC element type"` |

### O. Type Conversion Validation

For IDs 0x2106 and 0x2107:

```
Conversion type: bits[3:1], must be 1..4
Messages: "conversion type not a valid value"
          "Invalid dst type" / "Invalid src type"
          "Src and dst type must be different types"
          "Src and dst type must be different bit widths"
```

### O.1. Pack-Float Conversion (`nvvm.cvt.packfloat`) Validation

| Check | Message |
|---|---|
| Src/dst pair | `"Invalid Src/Dst Type in cvt_packfloat Intrinsic."` |

The pack-float family converts pairs of FP values to packed sub-word formats; the type pair must be one of a small whitelist (e.g., `{f32, f32} -> v2bf16`).

### P. tcgen05 (Blackwell Tensor Core Gen-5) Validation

The Blackwell-introduced `tcgen05.cp` / `tcgen05.mma` intrinsics carry an encoded shape and multicast flag word; the verifier validates the tuple as a unit:

| Check | Message |
|---|---|
| Copy shape encoding | `"Unexpected tcgen05.cp shape"` |
| Copy destination format | `"Unsupported tcgen05.cp destination format"` |
| Copy shape + multicast cross-check | `"Unsupported tcgen05.cp shape and multicast flags"` |
| Block-scale ashift gate | `"ashift is not supported with tcgen05.mma.block_scale variants"` |

These checks fire only on sm_100+ targets; on earlier architectures the intrinsic is rejected by the SM gate first.

### Q. Architecture-Gated MMA / Vector Intrinsic Variants

Beyond the SM-version gates in the [Architecture Gates](#architecture-gates-sm-gated-features) table, the intrinsic verifier rejects specific MMA, atomic, and vector variants whose hardware support landed in scattered SM versions. These messages follow a unified pattern -- `"<name> is not supported on this architecture"` -- and are checked against per-feature SM tables internal to the intrinsic verifier.

| Family | Variant | Message |
|---|---|---|
| HMMA (FP16 tensor) | mma | `"hmmamma is not supported on this architecture"` |
| HMMA | load-A/B | `"hmmaldab is not supported on this architecture"` |
| HMMA | load-C | `"hmmaldc is not supported on this architecture"` |
| HMMA | store-C | `"hmmastc is not supported on this architecture"` |
| IMMA (int8 tensor) | mma | `"immamma is not supported on this architecture"` |
| IMMA | load-A/B | `"immaldab is not supported on this architecture"` |
| IMMA | load-C | `"immaldc is not supported on this architecture"` |
| IMMA | store-C | `"imma stc not supported on this architecture"` (sic -- missing dash) |
| BMMA (binary tensor) | mma | `"bmmamma is not supported on this architecture"` |
| F32x2 | -- | `"F32x2 intrinsics are not supported on this architecture"` |
| Vector atomics | -- | `"vector atomics not supported on this architecture!"` |
| 128-bit atomics | -- | `"128b atomics not supported on this architecture!"` |
| TMA G2S | 2CTA mode | `"2CTA Mode for CpAsyncBulkTensorG2S not supported on this architecture"` |
| WGMMA (Hopper) | shape check | `"The shape %s is not supported for __wgmma_mma_async builtin"` |
| WGMMA | operand overflow | `"unexpected constant overflow in __wgmma_mma_async operand"` |

Several "unexpected"-prefixed messages indicate the verifier detected an MMA variant that should have been lowered earlier in the pipeline; reaching the verifier in this state implies an upstream pass bug rather than user IR error: `"unexpected imma_ld intrinsic!"`, `"unexpected imma_mma intrinsic call!"`, `"unexpected overloaded mma intrinsic call!"`, `"unexpected overloaded mma load intrinsic call!"`, `"unexpected overloaded mma store intrinsic call!"`, `"unexpected WMMA intrinsic!"`.

### R. Other Validation Categories

| Category | IDs | Key Messages |
|---|---|---|
| Coroutine | -- | `"llvm.nvvm.coro.create.suspend must have exactly one argument, which must be a constant integer"` |
| Subop mode | 9383-9384 | `"Invalid subop mode"` (bits[3:1] > 5) |
| Geometry output | -- | `"geometry out mode not a valid value"`, `"op1 of GeometryOut intrinsic must be constant when CUT mode"`, `"op1 of GeometryOut intrinsic must be 0 when CUT mode"` |
| Syncwarp | -- | `"syncwarp mode not a valid value"` |
| Cache operations | -- | `"invalid cache type"`, `"invalid cache op"` |
| Wait intrinsic | -- | `"Invalid wait mode"` |
| ISBE | 0x2BC1 (11201) | `"Only writes to MAP or ATTR are supported"`, `"Cannot write to input ISBE"` |
| `llvm.nvvm.sub` | -- | `"First argument of 'llvm.nvvm.sub' must be a constant."` |
| Load/store first arg | -- | `"The first argument of load/store intrinsic must be a constant."` |
| Address-space cvt deprecation | -- | `"nvvm address space conversion intrinsics are not supported. Please use addrspacecast instruction for address space conversions"` |
| `read.sreg` overload | -- | `"Unsupported overloaded declaration of llvm.nvvm.read.sreg intrinsic"` |
| `read.sreg` SM gate | -- | Specific sreg IDs gate on SM version (clock64 needs sm_30+, etc.) |
| Unsupported fallback | -- | `"Unsupported intrinsic: <name>"` |

## Cmpxchg Restrictions

The module verifier enforces strict constraints on `cmpxchg`:

```
Allowed types:  i32, i64, i128
Allowed spaces: generic (AS 0), global (AS 1), shared (AS 3)

Messages:
  "Atomic operations on non-i32/i64/i128 types are not supported"
  "cmpxchg pointer operand must point to generic, global, or shared address space"
```

This rules out i8/i16 atomics (hardware does not support sub-word CAS) and atomics on constant/local address spaces.

## Tensor Memory Restrictions

Load and store instructions targeting address space 6 (tensor memory) are rejected at the IR level:

```
Message: "Tensor Memory loads/stores are not supported"
```

Tensor memory access is handled through dedicated intrinsics (TMA/cp.async) rather than generic load/store instructions. The verifier enforces this indirection.

## Pipeline Placement

The NVVMVerifier is inserted repeatedly throughout the optimization pipeline, not just once. In the pipeline assembler (`sub_12E54A0`), it appears after nearly every major optimization pass, gated by `!NVVMPassOptions[600]`:

| Position | After Pass | Notes |
|---|---|---|
| 10 (O1 tier) | GVN | Verify IR after value numbering |
| After DSE | Dead Store Elimination | Verify after store removal |
| After EarlyCSE | Early CSE | O2+ only |
| After LoopIndexSplit | Loop Index Split | O2+ only |
| After NVVMReflect | NVVM Reflect | Common tail |
| After LICM | Loop-Invariant Code Motion | Common tail |
| After LowerSwitch | Switch lowering | Final position in common tail |

This aggressive re-verification catches bugs introduced by any optimization pass. In debug/development builds, this is the primary mechanism for detecting optimizer-introduced IR invalidity.

## Configuration

| Knob | Storage | Type | Default | Description |
|---|---|---|---|---|
| `NVVMPassOptions[600]` | opts array | bool | false | When true, disables ALL NVVMVerifier insertions in the pipeline |
| `nvvm-verify-show-info` | `ctor_257` | bool | false | Enables informational messages (e.g., `"IR Kind is UnifiedNVVMIR"`) |

## Diagnostic Infrastructure

Error messages are produced through a chain of helper functions:

| Function | Role |
|---|---|
| `sub_2C764C0` | Create diagnostic message with severity level |
| `sub_2C76A00` | Create error diagnostic for a specific instruction |
| `sub_2C76240` | Flush diagnostic to error stream |
| `sub_2C76F10` | Report an unsupported instruction by name (takes a string literal like `"indirectbr"`) |
| `sub_904010` | Append string to diagnostic buffer |
| `sub_CB6200` | Write raw bytes to output buffer |
| `sub_CB5AE0` | Flush buffer |

The error model is accumulate-and-continue: the verifier sets the error flag at context offset +16 and writes the diagnostic, but does not abort. This allows a single verification run to report all errors in the module.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| NVVMModuleVerifier | `sub_2C80C90` | 51KB | Module entry: triples, data layout, per-instruction dispatch |
| NVVMFunctionVerifier | `sub_2C771D0` | 36KB | Function-level: attributes, params, cluster dims, entry funcs |
| NVVMIntrinsicVerifier | `sub_2C7B6A0` | 143KB | Intrinsic-level: SM gates, types, MMA, atomics, tex/surf |
| NVVMVerifier pass wrapper | `sub_12D4560` | small | Pipeline entry point, creates context, invokes module verifier |
| Verify global variable | `sub_2C797D0` | -- | Per-global validation |
| Verify function declaration | `sub_2C7A130` | -- | Checks function declarations (not definitions) |
| Verify named metadata | `sub_2C7AA20` | -- | Named metadata validation |
| Verify address space cast | `sub_2C7AF00` | -- | addrspacecast / GEP rule checker |
| Verify generic call | `sub_2C795F0` | -- | Non-intrinsic call validation, pragma check |
| Report unsupported instruction | `sub_2C76F10` | -- | Produces `"<name> is not supported"` diagnostics |
| Is kernel function? | `sub_CE9220` | -- | Checks kernel calling convention |
| Extract cluster dimensions | `sub_CE8EA0` | -- | Reads cluster dims from function metadata |
| Extract cluster max blocks | `sub_CE9030` | -- | Reads max cluster blocks from metadata |
| Check function attribute | `sub_A73ED0` | -- | Tests presence of attribute by ID |
| Is .offset.bindless? | `sub_CEA320` | -- | Blackwell gate predicate |
| Get intrinsic name string | `sub_BD5D20` | -- | Returns intrinsic name for error messages |
| Get integer bit width | `sub_BCAE30` | -- | Type query helper |
| Compute total bit width | `sub_CA1930` | -- | Aggregate/vector width computation |

## Cross-References

- [GPU Target Architecture](../targets/index.md) -- SM table and architecture gating
- [Hopper (sm_90)](../targets/sm90-hopper.md) -- TMA, cluster operations, WGMMA
- [Blackwell (sm_100)](../targets/sm100-blackwell.md) -- tcgen05, .offset.bindless
- [Memory Space Optimization](./memory-space-opt.md) -- address space enforcement and resolution
- [NVIDIA Custom Passes index](./index.md) -- pass inventory
- [IP Memory Space Propagation](./ipmsp.md) -- inter-procedural address space analysis
