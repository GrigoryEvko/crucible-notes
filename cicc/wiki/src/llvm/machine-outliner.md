# MachineOutliner for GPU

The MachineOutliner in CICC v13.0 is the stock LLVM `MachineOutliner` pass, compiled into the binary at two address ranges: a candidate-finder at `sub_3539E80` and a core outlining engine at `sub_3537010`, totaling approximately 136KB of combined code. A second instance at `sub_1E3D600` (62KB) appears in the MIR infrastructure region (0x1E20000--0x1E3FFFF) containing the same diagnostic strings ("NotOutliningCheaper", "OutliningBenefit", etc.) and `[MEDIUM confidence]` likely represents the `runOnModule` entry point that delegates to the two primary functions. The `runOnModule` identification is based on the function's address being in the MIR infrastructure region and its diagnostic string overlap with the primary outliner; it could alternatively be a separate pass-manager wrapper or a legacy code path. The pass extracts repeated MachineInstr sequences across all functions in a module, factors them into shared `OUTLINED_FUNCTION_*` stubs, and replaces the original sequences with calls. On GPU targets this is significant because code size directly affects the L1 instruction cache (L0/L1i) footprint per SM, and every instruction that survives into PTX also contributes to `ptxas` compilation time and register pressure during its own allocation pass.

CICC ships the pass as part of its standard LLVM codegen infrastructure, controlled by the `enable-machine-outliner` TargetPassConfig knob (tri-state: `disable`, `enable`, `guaranteed beneficial`). The binary does **not** override the upstream default -- meaning the outliner's activation depends on whether the NVPTX backend's `TargetPassConfig::addMachineOutliner()` enables it. The presence of full outliner infrastructure (pass registration at `sub_35320A0`, ~136KB of outliner code, the benefit-threshold knob, and the `"nooutline"` function-attribute check) confirms the pass is callable. The critical question is whether NVIDIA's default pipeline activates it. The evidence is ambiguous but leans toward **conditionally enabled**: the `TargetPassConfig` enum includes "guaranteed beneficial" mode, and the NVPTX-specific calling convention 95 (assigned to outlined functions when no special CC is required) would serve no purpose if the pass were dead code.

| | |
|---|---|
| **Pass name** | `"Machine Function Outliner"` / `"machine-outliner"` |
| **Registration** | `sub_35320A0` -- stores pass ID at `unk_503D78C` |
| **Core outlining engine** | `sub_3537010` (77KB, 2,185 decompiled lines) |
| **Candidate finder** | `sub_3539E80` (59KB) |
| **Second instance (MIR region)** | `sub_1E3D600` (62KB, 0x1E3D600) |
| **Pass factory** | `sub_3534A50` |
| **Benefit threshold knob** | `qword_503DAC8` = `outliner-benefit-threshold` (default: 1) |
| **Cost mode flag** | `qword_503DC88` (loaded into pass state at offset +184) |
| **Debug flag** | `qword_503D828` (verbose outliner output) |
| **Options constructor** | `ctor_675` at `0x5A2820` (10,602 bytes) |
| **NVPTX outlined-function CC** | Calling convention 95 (PTX `.func` linkage) |
| **Outlined function naming** | `OUTLINED_FUNCTION_{round}_{index}` |
| **Function attributes applied** | `nounwind` (47), `minsize` (18), internal linkage |

## Suffix Tree Algorithm

The outliner's core algorithm is Ukkonen's suffix tree construction, applied to a flattened sequence of MachineInstr encodings from every eligible basic block in the module. The process proceeds in three stages.

### Stage 1: Instruction Mapping

`sub_3508720` (`buildInstrLegalityMapping`) walks each MachineBasicBlock and encodes every instruction as a `uint16` alphabet symbol. The encoding incorporates both the opcode and a structurally significant operand pattern, so that two instruction sequences with different register names but identical structure map to the same suffix-tree substring. The helper `sub_35082F0` initializes from the MBB's scheduling info (offset +32), and `sub_35085F0` populates the actual mapping.

Register-class resolution happens in a second pass via `sub_3508F10` (`buildRegClassMapping`): `sub_3508B80` builds register-class bitmask information, and `sub_3508890` computes the final mapping. This two-layer encoding is critical because NVPTX has typed register classes (i32, i64, f32, f64, pred, etc.) and an outlined sequence must be valid across all call sites regardless of which specific virtual register names appear.

Instructions that cannot participate in outlining receive a special encoding: unique negative integers starting at -3 (matching upstream's `IllegalInstrNumber`). Each illegal instruction gets a distinct value so it acts as a suffix-tree terminator, preventing matches from spanning across them. The sentinel value `0xFFFFFFFF` (-1 as `uint32`) in the cost array explicitly marks these.

### Stage 2: Suffix Tree Construction and Candidate Extraction

`sub_35364E0` (`insertIntoSuffixTree`) inserts each MBB's encoded instruction sequence into the suffix tree working set. The suffix tree identifies all repeated substrings of length >= 2. For each repeated substring with at least 2 occurrences, the pass creates a candidate group.

Function filtering happens before insertion. `sub_3539E80` iterates all MachineFunctions in the module's linked list and applies three gates:

1. **`nooutline` attribute check** -- `sub_B2D620` tests whether the function has the `"nooutline"` string attribute. If present, all MBBs in that function are skipped.

2. **`shouldOutlineFrom`** -- vtable dispatch at offset +1440 on the TargetInstrInfo. The NVPTX backend's implementation of this hook determines whether a given function is eligible based on target constraints.

3. **`isFunctionSafeToOutlineFrom`** -- vtable dispatch at offset +1432, receiving the outliner cost mode byte from `qword_503DC88`. This is where target-specific safety checks (e.g., functions with special register constraints or inline assembly) can reject outlining.

Additional per-block filters: a block must contain more than one instruction, must not already be marked as outlined (byte at MBB offset +217), and must have no special flag (qword at MBB offset +224 must be zero).

### Stage 3: Sorting and Pruning

After suffix-tree extraction, the candidate list is sorted using a hybrid merge sort:

- `sub_3534120` -- parallel merge sort for large arrays (recursive, splits at midpoint)
- `sub_3533600` -- in-place merge sort for small arrays (fallback when size < 14 pointers = 112 bytes)
- `sub_3533450` -- insertion sort for very small partitions (<= 14 elements)

The sorted suffix array is then scanned by `sub_3532120` (`findIllegalInRange`), which performs a 4-way unrolled linear scan searching for the sentinel value `0xFFFFFFFF` in the integer cost array. Any candidate whose instruction range contains an illegal sentinel is pruned. The compaction loop copies valid entries forward in place and frees discarded entries' internal string buffers via `_libc_free`.

## Benefit/Cost Model

The outliner accepts a candidate only if the net benefit exceeds the threshold. The formula:

```
Benefit = NumOccurrences * PerOccurrenceCost - FrameOverheadCost
```

Where:

- **NumOccurrences** = number of identical sequences found (vtable dispatch at slot 0 on the candidate)
- **PerOccurrenceCost** = bytes saved per replacement (effectively the cost of the call instruction that replaces the inlined sequence, dispatched via vtable slot 0 multiplied by the `repeat_count` at candidate offset +40)
- **FrameOverheadCost** = cost of the outlined function itself: the function entry/exit, the return instruction, and any callee-saved register saves (vtable dispatch at slot 8)

The decision rule:

```c
int benefit = num_occurrences * per_call_cost - frame_overhead;
if (benefit < 0) benefit = 0;
if (benefit < outliner_benefit_threshold) continue;  // skip candidate
```

The threshold `qword_503DAC8` defaults to 1, meaning any candidate that saves at least one byte is accepted. This is identical to upstream LLVM's default and is intentionally aggressive -- the outliner relies on the cost model's accuracy rather than a conservative threshold to filter bad candidates.

### NVPTX Cost Model Considerations

The cost model is dispatched through the TargetInstrInfo vtable, meaning the NVPTX backend supplies its own `getOutliningCandidateInfo`, `buildOutlinedFrame`, and `insertOutlinedCall` implementations. Several factors make the GPU cost model structurally different from CPU targets:

**Call overhead in PTX is expensive.** A PTX `.func` call requires `.param` space declaration, parameter marshaling (each argument is copied to `.param` memory), the `call` instruction itself, and result retrieval from `.param` space. On CPU targets, a `call` instruction is a single opcode plus a return address push. On NVPTX, the overhead is proportional to the number of live values that must be passed to the outlined function. This means the FrameOverheadCost for NVPTX candidates is significantly higher than on CPU, and only sequences with many occurrences or substantial length achieve positive benefit.

**No hardware call stack.** PTX function calls are lowered by `ptxas` into something closer to inlined code with register renaming. The actual "call" may or may not involve a hardware subroutine mechanism depending on the SM architecture and `ptxas` optimization level. This makes the cost model somewhat speculative from CICC's perspective -- the outlined function may be re-inlined by `ptxas`.

**Calling convention 95.** When no candidate entry in a group requires a special calling convention, the outlined function is assigned CC 95 -- an NVPTX-specific calling convention not present in upstream LLVM. CC 95 maps to PTX `.func` linkage with internal visibility, meaning the function is private to the compilation unit and `ptxas` has full freedom to inline or optimize it. See [Calling Convention 95](#calling-convention-95-the-nvptx-outlined-function-cc) below for the complete assignment algorithm and CC comparison table.

## Outlined Function Creation

When a candidate group passes the benefit threshold, `sub_3537010` creates the outlined function through these steps:

**Name generation.** The name follows the pattern `OUTLINED_FUNCTION_{round}_{index}`. The round number (pass counter at state offset +188) is omitted in round 0, producing `OUTLINED_FUNCTION_0`, `OUTLINED_FUNCTION_1`, etc. for the first pass and `OUTLINED_FUNCTION_2_0`, `OUTLINED_FUNCTION_2_1`, etc. for subsequent reruns. The integer-to-string conversion uses a standard two-digit lookup table (`"00010203...9899"`) for fast decimal formatting.

**LLVM Function creation.** `sub_BCB120` (getOrInsertFunction) creates or retrieves the Function in the LLVM Module. `sub_BCF640` creates the function type (void return, no arguments by default). `sub_B2C660` creates the corresponding MachineFunction.

**Function flags.** The flag word at function offset +32 is set to `(existing & 0xBC00) | 0x4087`. The bit pattern `0x4087` encodes internal linkage, norecurse, and nounwind. The mask `0xBC00` preserves target-dependent alignment and visibility bits. Two explicit attributes are added: `nounwind` (attribute ID 47) and `minsize` (attribute ID 18).

**Register liveness.** A `calloc`-allocated byte array (one byte per physical register, count from `TargetRegisterInfo::getNumRegs()` at TRI offset +16) tracks which registers are live-through versus defined-inside the outlined region. `sub_35095B0` (`populateOutlinedFunctionBody`) walks the outlined MBB's instruction stream, checking the TargetRegisterInfo live-in bitmap (offset +48 in the subtarget). Registers not in the live-in set are inserted as phantom definitions. Super-register chains are walked via delta tables at TRI offset +56, following standard LLVM MCRegisterInfo encoding.

**Outlined body.** The TargetInstrInfo hook `buildOutlinedFrame` (vtable offset +1408) constructs the actual machine instructions in the outlined function by copying from the candidate entries. The `isOutlined` flag is set at MachineFunction offset +582.

## Call-Site Rewriting

After creating the outlined function, the pass rewrites each call site:

1. For each candidate entry, `insertOutlinedCall` (vtable offset +1416) is invoked with the caller's MBB, an insertion point, the outlined Function, and the candidate metadata. This returns the new call MachineInstr.

2. If the outlined function has callee-saved register information (flag at candidate offset 344), the pass builds live-in/live-out register sets using red-black trees (`sub_3536E40` for classification). Registers are classified as defs (implicit-def, flag `0x30000000`), uses (implicit-use, flag `0x20000000`), or implicitly-defined. These operands are attached to the call instruction via `sub_2E8F270`.

3. The original instruction range in the cost array is memset to `0xFF`, marking it with illegal sentinels. This prevents future outlining passes (reruns) from attempting to re-outline already-outlined code.

## Candidate Entry Structure

Each candidate is a 224-byte structure (56 x uint32 stride):

| Offset | Size | Field |
|---|---|---|
| `+0x00` | 4 | `start_index` -- index into module instruction array |
| `+0x04` | 4 | `length` -- number of instructions in sequence |
| `+0x08` | 8 | `call_info_ptr` -- pointer to MBB or instruction range |
| `+0x10` | 8 | `metadata_0` |
| `+0x18` | 8 | `metadata_1` |
| `+0x20` | 4 | `num_occurrences_field` |
| `+0x28` | 4 | `cost_field` |
| `+0x2C` | 48 | SSO string data (via `sub_3532560`) |
| `+0x70` | 4 | `benefit_or_flags` |
| `+0x78` | 40 | Second SSO string field |
| `+0xA0` | 1 | `flag_byte_0` |
| `+0xA1` | 1 | `flag_byte_1` |
| `+0xA8` | 4 | `field_A8` |
| `+0xAC` | 4 | `field_AC` |
| `+0xB0` | 4 | `field_B0` |
| `+0xB4` | 4 | `field_B4` |

The two string fields use LLVM's small-string optimization (SSO): strings shorter than the inline buffer are stored directly in the struct; longer strings allocate on the heap. The copy function `sub_3532560` handles both cases.

## Calling Convention 95: The NVPTX Outlined-Function CC

CICC defines calling convention 95 (0x5F) as an NVPTX-specific calling convention that does not exist in upstream LLVM. It is assigned exclusively to outlined functions and signals to both the AsmPrinter and `ptxas` that the function is a module-internal device helper with PTX `.func` linkage.

### CC Assignment Algorithm

The CC assignment happens in Phase 5 of `sub_3537010` (lines 838--877 of the decompilation), after the outlined MachineFunction is created and before its body is populated. The algorithm:

```
fn assign_outlined_cc(candidate_group, outlined_fn):
    max_cc = 0
    for entry in candidate_group:
        cc = sub_A746B0(entry)          // extract caller's CC from candidate
        max_cc = max(max_cc, cc)

    if max_cc > 0:
        // At least one call site has a non-default CC.
        // Inherit the highest CC and create a callee-saved register mask.
        sub_B2BE50(outlined_fn, max_cc)         // setCallingConv
        sub_A77AA0(outlined_fn, max_cc)         // create callee-saved mask
    else:
        // All call sites have default CC (0) -- typical case for
        // device functions compiled from __device__ code.
        // Assign the NVPTX-specific outlined-function CC.
        outlined_fn.setCallingConv(95)
```

`sub_A746B0` extracts the calling convention from each candidate entry's source MachineFunction. The "max" selection rule means that if candidates come from functions with different CCs, the outlined function inherits the most restrictive one. In practice, since the outliner only groups structurally identical MachineInstr sequences, all entries in a group typically come from functions with the same CC.

### CC 95 vs Other NVPTX Calling Conventions

| CC | Decimal | PTX Linkage | Meaning |
|---|---|---|---|
| 0 | 0 | `.func` | Default C calling convention (non-kernel device function) |
| 42 | 0x2A | `.entry` | PTX kernel entry (one of two kernel CCs; used in SCEV budget bypass) |
| 43 | 0x2B | `.entry` | PTX kernel entry (variant; also bypasses SCEV budget) |
| 71 | 0x47 | `.entry` | Primary CUDA kernel CC (`isKernel` returns true when `linkage == 0x47`) |
| 95 | 0x5F | `.func` | **NVPTX outlined-function CC** -- internal, never a kernel |

CC 95 functions are emitted as `.func` by the AsmPrinter (`sub_215A3C0`). The `.entry` vs `.func` branch at line 30--33 of the PTX header emission calls `sub_1C2F070` (`isKernelFunction`), which checks whether the CC is one of the kernel CCs (42, 43, 71) or the `nvvm.kernel` metadata flag. CC 95 fails all kernel tests, so the function is always emitted as `.func`.

### What CC 95 Communicates

The CC carries three semantic signals:

1. **Internal linkage.** CC 95 functions are never externally visible. The flag word `0x4087` applied at function offset +32 encodes internal linkage. Combined with the `nounwind` (47) and `minsize` (18) attributes, this tells the backend and `ptxas` that the function is private to the compilation unit.

2. **No `.param`-space calling convention overhead.** Unlike CC 0 device functions, which must declare `.param` space for every argument and marshal values through `st.param`/`ld.param` sequences (the full `sub_3040BF0` `LowerCall` path with `DeclareParam`/`DeclareScalarParam` nodes), CC 95 functions use a simplified call interface. The outlined function takes no explicit arguments -- live values are passed implicitly through the register state, and the `TargetInstrInfo::insertOutlinedCall` hook (vtable +1416) handles the call-site ABI.

3. **`ptxas` is free to inline.** Because CC 95 functions are internal `.func` with no special ABI constraints, `ptxas` can and frequently does inline them back at the call site during its own optimization passes. This makes the outlining decision partially speculative from CICC's perspective -- the code size reduction measured by the benefit model may be undone by `ptxas`.

### Callee-Saved Register Mask Interaction

When `max_cc > 0` (the non-default path), `sub_A77AA0` creates a callee-saved register mask for the outlined function. This mask determines which registers the outlined function must preserve across its body. For CC 95 (the `max_cc == 0` path), no callee-saved mask is created. Instead, the call-site rewriting logic at Phase 11 of `sub_3537010` (lines 1469--1968) builds explicit `implicit-def` (flag `0x30000000`) and `implicit-use` (flag `0x20000000`) operands on the call instruction using the RB-tree-based register classifier at `sub_3536E40`. This makes the register interface fully explicit rather than relying on a convention-defined preserved set.

## launch_bounds Interaction and Cross-Kernel Outlining

The MachineOutliner operates at module scope -- it considers all functions in the module simultaneously. On NVPTX, this raises the question of whether sequences can be outlined across functions with different `__launch_bounds__` annotations.

### How launch_bounds Metadata Flows

The `__launch_bounds__` attribute on a `__global__` function flows through CICC as follows:

1. **EDG frontend** (`sub_826060`): Validates `__launch_bounds__` arguments. Rejects `__launch_bounds__` on non-`__global__` functions. Detects conflicts with `__maxnreg__`.

2. **Post-parse fixup** (`sub_5D0FF0`): Converts `__launch_bounds__` values into structured metadata.

3. **Kernel metadata emission** (`sub_B05_kernel_metadata`): Stores as LLVM named metadata under `nvvm.annotations`:
   - `nvvm.maxntid` -- max threads per block (from first `__launch_bounds__` argument)
   - `nvvm.minctasm` -- minimum CTAs per SM (from second argument, if present)
   - `nvvm.maxnreg` -- max registers per thread (from `__maxnreg__` or third argument)

4. **PTX emission** (`sub_214DA90`): Reads the metadata back and emits `.maxntid`, `.minnctapersm`, `.maxnreg` directives. These are emitted **only for `.entry` functions** -- the guard at step (g) of `sub_215A3C0` ensures `.func` functions never receive these directives.

### The Outlined Function Inherits Nothing

Because outlined functions are created with internal linkage, void return type, and CC 95 (`.func`), they are device functions -- never kernels. The function creation code in Phase 5 of `sub_3537010` does not copy any metadata from source functions. Specifically:

- No `nvvm.kernel` flag is set.
- No `nvvm.maxntid` metadata is attached.
- No `nvvm.maxnreg` metadata is attached.
- No `nvvm.minctasm` metadata is attached.
- No `nvvm.cluster_dim` or `nvvm.maxclusterrank` metadata is attached.
- The `isKernel` check (`sub_CE9220`) returns false: the CC is not 0x47, there is no `nvvm.kernel` metadata, and there is no `"kernel"` entry in `nvvm.annotations`.

The only function-level metadata the outlined function receives is the `isOutlined` flag at MachineFunction offset +582 and the two attributes `nounwind` (47) and `minsize` (18).

### Function Eligibility Gating

The candidate finder (`sub_3539E80`) applies three gates before considering a function's basic blocks for outlining:

```
fn is_eligible(func, cost_mode):
    // Gate 1: explicit opt-out
    if sub_B2D620(func, "nooutline"):       // has "nooutline" attribute?
        return false

    // Gate 2: target hook -- "should we outline FROM this function?"
    tii = get_target_instr_info(func)
    if !tii.vtable[1440](func):             // shouldOutlineFrom
        return false

    // Gate 3: target hook -- "is it SAFE to outline from this function?"
    if !tii.vtable[1432](func, cost_mode):  // isFunctionSafeToOutlineFrom
        return false

    return true
```

The NVPTX backend's implementation of `shouldOutlineFrom` (vtable +1440) and `isFunctionSafeToOutlineFrom` (vtable +1432) determines whether kernel functions and launch_bounds-constrained functions participate. The evidence does not contain the NVPTX-specific implementation of these hooks, so we cannot state definitively whether kernels with `nvvm.maxnreg` are rejected. However, the architectural implications are clear:

**If the hooks permit outlining from constrained kernels**, the outliner may extract a sequence shared between a `maxnreg=32` kernel and a `maxnreg=64` kernel into a single CC 95 `.func`. That `.func` has no register budget. When `ptxas` processes the `maxnreg=32` kernel's call to this `.func`, it must either:

1. **Inline the call** -- absorbing the outlined function's register usage into the kernel's allocation. If the outlined body fits within 32 registers, this is transparent.
2. **Keep the call** -- allocating the outlined function's registers within the kernel's 32-register budget. If the outlined function needs more registers than available after the kernel's own allocation, `ptxas` will spill to local memory.

Both outcomes preserve correctness. The performance risk is that spilling may occur in a kernel that would not have spilled without outlining, because the CICC-side cost model has no visibility into `ptxas`'s register allocation decisions.

**If the hooks reject constrained kernels**, the outliner only operates on unconstrained device functions (CC 0) and kernels without `__launch_bounds__`. This is the conservative and likely behavior, given that NVIDIA is aware of the register-pressure implications.

### Per-Block Eligibility

Even within an eligible function, individual basic blocks are filtered:

| Condition | Check | Effect |
|---|---|---|
| Block has <= 1 instruction | `MBB.size() <= 1` | Skipped -- too small to outline |
| Block already outlined | byte at MBB offset +217 | Skipped -- prevents re-outlining |
| Block has special flag | qword at MBB offset +224 != 0 | Skipped -- target-specific block exclusion |

The "already outlined" flag at MBB offset +217 is set by the call-site rewriting phase (Phase 11) after replacing a sequence with a call to the outlined function. Combined with the cost-array sentinel memset (`0xFF` fill), this provides a two-layer defense against re-outlining.

## Outlining vs. Inlining Tension

The MachineOutliner and the LLVM inliner operate in opposite directions: the inliner copies callee bodies into call sites (increasing code size, reducing call overhead), while the outliner extracts common sequences out of function bodies (decreasing code size, adding call overhead). In CICC, the two passes do not directly coordinate -- the inliner runs during the IR optimization pipeline (CGSCC pass manager), while the MachineOutliner runs late in the machine codegen pipeline after register allocation and scheduling.

The tension manifests in two ways:

1. **The inliner may create outlining opportunities.** Aggressive inlining of small device functions can produce multiple copies of the same instruction sequence in different callers, which the outliner then detects and re-extracts. This round-trip (inline then outline) is wasteful but not incorrect. The net result depends on whether the outliner's shared function is more cache-friendly than the inlined copies.

2. **The outliner may undo inlining benefits.** If the inliner carefully decided that inlining a hot function improves performance by eliminating call overhead and enabling cross-function optimization, the outliner may later extract the inlined sequence back out if it appears in multiple callers. The `minsize` attribute on outlined functions does not prevent this -- it only signals that the outlined function should be optimized for size rather than speed.

The `enable-machine-outliner` knob's "guaranteed beneficial" mode addresses this partially by only outlining sequences where the cost model is confident the savings are worthwhile, but it cannot reason about the inliner's original intent.

## Configuration Knobs

All knobs are LLVM `cl::opt` command-line options, passable via `-Xllc` in CICC:

| Knob | Type | Default | Effect |
|---|---|---|---|
| `outliner-benefit-threshold` | `unsigned` | 1 | Minimum net byte savings for a candidate to be accepted. Higher values make outlining more conservative. |
| `enable-machine-outliner` | enum | target-dependent | Tri-state: `disable`, `enable`, `guaranteed beneficial`. Controls whether the pass runs at all. |
| `enable-linkonceodr-outlining` | `bool` | `false` | Whether to outline from `linkonce_odr` functions. Off by default because the linker can deduplicate these. Should be enabled under LTO. |
| `machine-outliner-reruns` | `unsigned` | 0 | Number of additional outliner passes after the initial run. Each rerun can find new candidates from code modified by previous outlining. |
| `outliner-leaf-descendants` | `bool` | `true` | Consider all leaf descendants of internal suffix-tree nodes as candidates (not just direct leaf children). |
| `disable-global-outlining` | `bool` | `false` | Disable global (cross-module) outlining, ignoring codegen data generation/use. |

The options constructor at `ctor_675` (0x5A2820, 10,602 bytes) registers the outliner-specific options including the linkonce-odr and rerun knobs. The benefit threshold is registered separately in the same constructor.

## Diagnostic Strings

The outliner emits LLVM optimization remarks under the `"machine-outliner"` pass name:

| Remark key | Meaning |
|---|---|
| `"OutlinedFunction"` | A new outlined function was created |
| `"NotOutliningCheaper"` | Candidate rejected because outlining would not save bytes |
| `"Did not outline"` | Candidate rejected for other reasons (illegal instructions, safety checks) |
| `"OutliningBenefit"` | Named integer: net byte savings |
| `"OutliningCost"` | Named integer: cost of the outlined call sequence |
| `"NotOutliningCost"` | Named integer: cost of keeping the sequence inline |
| `"NumOccurrences"` | Named integer: how many times the sequence was found |
| `"Length"` | Named integer: number of instructions in the sequence |
| `"StartLoc"` / `"OtherStartLoc"` | Source locations of the outlined regions |

The remark message format: `"Saved {N} bytes by outlining {M} instructions from {K} locations. (Found at: {loc1}, {loc2}, ...)"`.

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| Pass registration (name, ID, factory) | `sub_35320A0` | -- | -- |
| Pass factory function | `sub_3534A50` | -- | -- |
| Core outlining engine (`outline + rewrite`) | `sub_3537010` | 77KB | -- |
| Candidate finder / suffix-tree builder | `sub_3539E80` | 59KB | -- |
| MachineOutliner `runOnModule` entry (MIR region) | `sub_1E3D600` | 62KB | -- |
| `insertIntoSuffixTree` -- adds MBB instruction hashes | `sub_35364E0` | -- | -- |
| `SuffixArray::allocateWorkBuffer` | `sub_3535DB0` | -- | -- |
| `SuffixArray::parallelMergeSort` | `sub_3534120` | -- | -- |
| `SuffixArray::inPlaceMergeSort` (fallback for small arrays) | `sub_3533600` | -- | -- |
| Insertion sort for <= 14 elements | `sub_3533450` | -- | -- |
| `findIllegalInRange` (4-way unrolled sentinel scan) | `sub_3532120` | -- | -- |
| `buildInstrLegalityMapping` -- MBB to suffix alphabet | `sub_3508720` | -- | -- |
| `buildRegClassMapping` -- register-class constraint resolution | `sub_3508F10` | -- | -- |
| `populateOutlinedFunctionBody` -- instruction insertion | `sub_35095B0` | -- | -- |
| `classifyOperandRegisters` -- RB-tree register tracking | `sub_3536E40` | -- | -- |
| `RBTree::destroyAll` -- recursive tree deallocation | `sub_3532B90` | -- | -- |
| `std::string` constructor (for name generation) | `sub_35323D0` | -- | -- |
| SmallString SSO-aware deep copy | `sub_3532560` | -- | -- |
| `RemarkBuilder::appendField` | `sub_3534BB0` | -- | -- |
| `RemarkBuilder::emitOutlinedFunctionRemark` | `sub_35341F0` | -- | -- |
| Extract calling convention from candidate entry's source function | `sub_A746B0` | -- | -- |
| Create callee-saved register mask for non-default CC | `sub_A77AA0` | -- | -- |
| `hasAttribute("nooutline")` -- function attribute check | `sub_B2D620` | -- | -- |
| `isKernel(func)` -- returns true for CC 0x47 or `nvvm.kernel` metadata | `sub_CE9220` | -- | -- |
| `isKernelFunction` -- `.entry` vs `.func` emission branch | `sub_1C2F070` | -- | -- |
| Kernel attribute emission (`.maxntid`, `.maxnreg`, `.minnctapersm`) | `sub_214DA90` | -- | -- |
| PTX function header orchestrator (`.entry` / `.func` branch + params) | `sub_215A3C0` | -- | -- |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM | CICC v13.0 |
|--------|---------------|------------|
| **Activation** | Default off for most targets; explicit `-enable-machine-outliner` required | Conditionally enabled via `TargetPassConfig::addMachineOutliner()`; evidence of "guaranteed beneficial" mode for NVPTX |
| **Calling convention** | Uses target default CC for outlined functions | Assigns CC 95 to outlined functions -- a dedicated NVPTX convention that bypasses `.param`-space ABI overhead |
| **Kernel interaction** | No kernel concept; all functions treated equally | `isKernel(func)` check (`sub_CE9220`) for CC 0x47 / `nvvm.kernel` metadata; kernel attributes (`.maxntid`, `.maxnreg`, `.minnctapersm`) may constrain outlining profitability |
| **`nooutline` attribute** | Standard function attribute check | Same check (`sub_B2D620` / `hasAttribute("nooutline")`); kernels with tight `__launch_bounds__` may implicitly disable outlining |
| **Code size motivation** | Reduce instruction cache footprint and binary size | Primary motivation is L0/L1i instruction cache pressure per SM partition; every surviving PTX instruction also costs `ptxas` compilation time |
| **Suffix tree/array** | Standard suffix array construction | Same algorithm; parallel merge sort (`sub_3534120`) with fallback insertion sort for <= 14 elements |

## Cross-References

- [Inliner Cost Model](../lto/inliner-cost.md) -- the opposing force: inlining decisions that the outliner may partially reverse
- [AsmPrinter & PTX Body Emission](../infra/asmprinter.md) -- how outlined `.func` functions are emitted as PTX
- [Register Allocation](./register-allocation.md) -- the outliner runs after RA; outlined functions affect register pressure
- [Register Coalescing](./register-coalescing.md) -- coalescing happens before outlining; the outliner operates on already-coalesced code
- [Block Placement](./block-placement.md) -- block layout interacts with code size; the outliner reduces the instruction footprint that placement must arrange
- [Pipeline & Ordering](./pipeline.md) -- where the outliner sits in the overall pass sequence
- [NVPTX Call ABI](./selectiondag.md) -- the `.param`-space calling convention that CC 0 device functions use; CC 95 outlined functions bypass this
- [SCEV Analysis](./scev.md) -- SCEV budget bypass for CC 42/43 kernel functions; illustrates CC-based dispatch in CICC
