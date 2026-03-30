# PrologEpilogInserter & Frame Layout

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

NVIDIA GPUs have no hardware stack pointer. There is no `push`, no `pop`, no `%rsp` — the entire concept of a "stack frame" is a compiler fiction. When a CUDA kernel needs local storage (spill slots, `alloca`, local arrays), cicc allocates a byte array called `__local_depot` in PTX `.local` address space and computes all offsets at compile time. The PrologEpilogInserter (PEI) pass is responsible for this: it takes abstract `MachineFrameInfo` frame indices produced by register allocation and earlier lowering, assigns concrete byte offsets within the depot, emits the two-instruction prologue that sets up the `%SP`/`%SPL` pseudo-registers, and rewrites every frame-index operand in the MachineFunction to `[%SP + offset]` form. At 68 KB and ~2,400 decompiled lines, cicc's PEI is a heavily modified monolith — the upstream open-source NVPTX backend replaces LLVM's standard PEI with a stripped-down 280-line `NVPTXPrologEpilogPass` that handles only offset calculation and frame-index elimination. cicc restores and extends nearly all of the standard PEI's functionality: callee-saved register handling, register scavenging, bitmap-based frame packing, categorized layout ordering, and a stack-size diagnostic system.

| Property | Value |
|---|---|
| Binary address | `sub_35B1110` (0x35B1110) |
| Binary size | 68,332 bytes (~2,388 decompiled lines) |
| Pass identity | `PrologEpilogInserter::runOnMachineFunction` |
| Pass position | Post-register-allocation, before NVPTXPeephole |
| Stack frame | 0x490 bytes of local state (~400 variables) |
| Upstream equivalent | `NVPTXPrologEpilogPass` (280 lines) + `NVPTXFrameLowering` (101 lines) |
| Key strings | `"warn-stack-size"`, `"stack frame size"` |
| Knobs | `warn-stack-size` (function attribute), `nvptx-short-ptr`, `nv-disable-mem2reg` |

## The GPU "Stack" Model

### __local_depot: The Frame Array

Every PTX function that needs local storage declares a `.local` byte array:

```
.local .align 16 .b8  __local_depot0[256];
```

This is the entire "stack frame." The alignment value is the maximum alignment of any object in the frame. The size is the total frame size computed by PEI. The suffix number (`0`, `1`, ...) is the function index within the module.

There is no call stack in the CPU sense. GPU threads have a fixed local memory allocation (typically 512 KB per thread on modern architectures). The `.local` directive reserves a region within this per-thread memory. Recursive functions and dynamic allocations are legal in PTX but the driver/ptxas resolves their addresses — cicc only needs to produce a statically-sized depot for each function's fixed-size locals.

### %SP and %SPL: The Two Frame Pseudo-Registers

PTX declares two pseudo-register pairs for frame access:

```
.reg .b64  %SP;     // generic address space pointer to the frame
.reg .b64  %SPL;    // local address space (AS 5) pointer to the frame
```

In 32-bit mode these are `.reg .b32`. The distinction exists because NVIDIA GPUs use address space qualification:

- **`%SPL`** (Stack Pointer Local) — points directly into the `.local` address space (PTX address space 5). Loads/stores using `%SPL` emit `ld.local`/`st.local` instructions, which ptxas can optimize for the L1 cache local-memory path. This is the efficient pointer.

- **`%SP`** (Stack Pointer) — a generic address space pointer obtained by converting `%SPL` via `cvta.local`. Loads/stores using `%SP` go through generic address resolution, which adds a TLB lookup to determine the address space at runtime. This is slower but required when the address escapes to code that expects generic pointers (e.g., passing a local variable's address to a called function).

The prologue sequence is:

```
mov.u64   %SPL, __local_depot0;     // MOV_DEPOT_ADDR_64
cvta.local.u64  %SP, %SPL;          // cvta_local_64
```

The `cvta.local` (Convert Address) instruction is the key: it takes a `.local` pointer and produces the equivalent generic-space pointer. When `nvptx-short-ptr` is enabled, `%SPL` is 32 bits (sufficient for the per-thread local memory window, always < 4 GB) while `%SP` may still be 64 bits on 64-bit targets.

Upstream's `NVPTXFrameLowering::emitPrologue` implements this directly. It checks `MachineRegisterInfo::use_empty` for each register — if `%SP` has no uses, it skips the `cvta.local`; if `%SPL` has no uses, it skips the `mov.depot`. The `NVPTXPeephole` pass runs immediately after PEI and rewrites `LEA_ADDRi64 %VRFrame64, offset` followed by `cvta_to_local_64` into `LEA_ADDRi64 %VRFrameLocal64, offset`, eliminating the generic-to-local conversion when the address stays in local space.

### Frame Index Resolution

During instruction selection and register allocation, local memory references use abstract frame indices: `%stack.0`, `%stack.1`, etc. Each maps to a `MachineFrameInfo` frame object with a size, alignment, and (after PEI) a byte offset.

Frame-index elimination in upstream is simple — `NVPTXRegisterInfo::eliminateFrameIndex` replaces the frame-index operand with `VRFrame` (which prints as `%SP`) and sets the immediate offset:

```cpp
MI.getOperand(FIOperandNum).ChangeToRegister(getFrameRegister(MF), false);
MI.getOperand(FIOperandNum + 1).ChangeToImmediate(Offset);
```

The `VRDepot` physical register (prints as `%Depot` internally) serves as the canonical frame base in `getFrameIndexReference`. For debug info, `%Depot` is remapped to `%SP` since cuda-gdb resolves stack frames via the generic pointer.

## Frame Layout Algorithm

cicc's PEI executes in ten sequential phases within a single monolithic function. The algorithm is significantly more sophisticated than upstream's linear scan.

### Phase 1–2: Setup and Callee-Saved Registers (lines 443–566)

Retrieves the `TargetFrameLowering` and `TargetRegisterInfo` from the MachineFunction's subtarget. If callee-saved registers exist (determined by `vtable(FrameLowering, +480)`), allocates a 0xA8-byte callee-save info structure at PEI state offset +200 containing two inline SmallVectors for register indices.

On GPU targets, callee-saved registers are unusual — PTX functions use a fully virtual register file, so there is no hardware register saving in the CPU sense. However, cicc models device-function calling conventions that may require preserving certain virtual registers across calls, and this mechanism handles that.

### Phase 3: Fixed Object Collection (lines 567–730)

Initializes a chunk table (deque-like structure) with -4096 sentinel values. Collects prolog/epilog insertion points from the PEI state arrays at offsets +216 (prolog points, count at +224) and +264 (epilog points, count at +272).

When callee-saves exist and optimization level is not 20 (a special threshold), manually inserts save/restore instructions:
- Simple saves: `storeRegToStackSlot(MBB, MI, reg, kill=1, FI, RC, TRI)`
- Compound saves: handles sub-register decomposition via `sub_2F26260` when `byte+9 == 1` in the callee-save info.

### Phase 4: Offset Assignment — The Core Layout Engine (lines 733–1070)

This is the heart of PEI. It assigns byte offsets within `__local_depot` to every frame object.

```
MachineFrameInfo layout:
  StackDirection:    1 = grows-negative (toward lower addresses)
                     0 = grows-positive (toward higher addresses)
  LocalFrameSize:    initial offset base
  NumFixedObjects:   count of pre-positioned objects
  MaxAlignment:      tracks largest alignment seen
```

**Fixed objects** are laid out first. Each frame object is a 40-byte record:

| Offset | Type | Field |
|---|---|---|
| +0 | i64 | Byte offset (written by PEI) |
| +8 | i64 | Object size in bytes |
| +16 | u8 | Alignment (log2) |
| +20 | u8 | isDead flag |
| +32 | u8 | isSpillSlot flag |
| +36 | u8 | Category (0–3) |

The alignment formula appears ~20 times throughout the pass:

```c
// Round up 'value' to next multiple of (1 << align_log2):
aligned = -(1 << align_log2) & (value + (1 << align_log2) - 1);
// Equivalent to: aligned = (value + mask) & ~mask  where mask = (1<<n) - 1
```

For grows-negative direction, offsets are stored as negative values; for grows-positive, they accumulate upward.

**Callee-saved region** is laid out next, iterating frame indices in range `[PEI+208 .. PEI+212]`. Each CSR object gets an aligned offset using the same formula.

**Separate stack area**: if `MachineFrameInfo+665` flag is set, NVIDIA supports a physically separate stack region with its own alignment at +664 and total size at +656. This likely corresponds to a distinct `.local` segment for shared-memory scratch or ABI-reserved zones.

### Phase 5: Categorized Local Variable Layout (lines 1060–1600)

This is cicc's most significant divergence from upstream PEI. Objects are classified into three priority buckets by a category byte at frame-object offset +36:

| Category | Bucket | Typical contents | Layout order |
|---|---|---|---|
| 3 | `v427` | Vector/tensor spills (high alignment) | First |
| 2 | `v419` | Medium-aligned objects | Second |
| 1 | `v412` | General locals | Third |
| 0 | — | Skip (already placed or dead) | — |

Each bucket is processed by `sub_35B0830` which assigns aligned offsets. The ordering minimizes alignment waste: laying out large-alignment objects first avoids padding gaps.

Objects are skipped if:
- They are spill slots in a separate stack area
- They fall within the callee-saved index range
- Their size is -1 (sentinel for dynamic-size objects)
- They are the frame-pointer object
- They are dead

**Bitmap-Based Packing** — When register count is nonzero and `canUseStackBitmap` returns true (frame size <= 0x7FFFFFFF), cicc builds a bitset representing every byte of the frame:

```c
// Bitmap size in qwords:
bitmap_size = (frame_size + 63) >> 6;

// Mark all bytes as free (bits set to 1)
// Then clear bits for fixed objects and CSR objects
for each placed_object:
    clear bits [offset .. offset + size)
```

For each unassigned general object, the algorithm scans the bitmap using `tzcnt` (trailing zero count) to find contiguous runs of set bits that match the object's size and alignment:

```c
for each unassigned_obj in v412:
    candidate = tzcnt_scan(bitmap, obj.size);
    if (candidate != NOT_FOUND):
        // Verify alignment
        if aligned(candidate, obj.alignment):
            // Verify all bits available (inner loop)
            if all_bits_set(bitmap, candidate, candidate + obj.size):
                assign_offset(obj, candidate);
                clear_bits(bitmap, candidate, candidate + obj.size);
                continue;
    // Fallback: linear allocation at end of frame
    offset = align(running_offset);
    assign_offset(obj, offset);
    running_offset += obj.size;
```

This is substantially more aggressive than both upstream LLVM PEI (which does a single linear pass) and the upstream NVPTX PrologEpilogPass (which has no packing at all). It enables reuse of "holes" left by fixed objects, callee-saves, and dead objects.

### Phase 6: Final Alignment and Frame Size (lines 1688–1795)

After all objects are laid out:

1. If `targetHandlesStackFrameRounding` returns true, skip to finalization.
2. Add `MaxCallFrameSize` to the running offset if the function adjusts the stack.
3. Choose alignment: `StackAlign` (from `TFI.getStackAlign()`) for functions with calls or alloca, or `TransientStackAlign` for leaf functions. The subtarget stores these at `FrameLowering[12]` and `[13]` respectively.
4. Round up: `final = align(running_offset, max(StackAlign, MaxAlignment))`.
5. If alignment changed the total and direction is grows-negative, shift all callee-save offsets by the delta to maintain correct relative positions.
6. Write `FrameInfo.StackSize = final_offset - initial_offset`.

This value becomes the `SIZE` in `.local .align ALIGN .b8 __local_depotN[SIZE]`.

### Phase 7: Prologue/Epilogue Insertion (lines 1803–1872)

Executed when optimization level is not at threshold 20. For each prolog insertion point, calls `emitPrologue(MF, MBB)` via `RegisterInfo` vtable at +96. For each epilog point, calls `emitEpilogue(MF, MBB)` at +104.

Post-fixup via `sub_35AC7B0`, then a second pass over prolog points for `insertPrologueSaveCode` (vtable +152, if not a null stub).

Architecture-specific extension: checks `(*(Module+2) >> 4) & 0x3FF == 0xB` (SM arch code 11). When matched, calls an additional prolog handler at vtable +176. This likely targets an early or internal SM variant.

### Phase 8–9: Frame Index Elimination (lines 1873–2268)

Two strategies selected by `vtable(FrameLowering, +616)`:

**Forward elimination (Path A)**: walks each MBB's instruction list forward. For each instruction, checks the opcode against `FRAME_SETUP` and `FRAME_DESTROY` pseudos — these adjust the SP offset tracker. For other instructions, scans operands for type-5 (FrameIndex), then calls `sub_35ABF20` to attempt elimination or falls back to the target-specific handler.

**Backward elimination (Path B)**: same logic but iterates instructions in reverse order. Handles `FRAME_SETUP`/`FRAME_DESTROY` with different SP adjustment accumulation.

This dual-path approach is unique to cicc — upstream NVPTX PrologEpilogPass only does a single backward walk. The forward path may be needed for instructions where the SP adjustment at a given point depends on preceding pseudo-ops.

### Phase 10: Diagnostics and Cleanup (lines 2270–2388)

**Stack size warning**: default threshold is 0xFFFFFFFF (4 GB, effectively disabled). If the function has a `"warn-stack-size"` attribute, it parses the value via `strtoul(str, &end, 10)`. When the total frame size (plus optional regspill area at `MF+86*wordsize` if opt-level flag 55 is set) exceeds the threshold, emits a `"stack frame size"` diagnostic.

**Stack annotation**: if annotation output is enabled (checked via `sub_B6EA50`/`sub_B6F970`), formats and writes stack-size metadata to the analysis output for the NVVM container.

Cleanup frees the 0xA8 callee-save info structure, resets prolog/epilog point counts, resets frame metadata, and walks the chunk table to free non-inline instruction arrays.

## Dynamic Stack Allocation (alloca)

PTX supports `alloca` semantics at the LLVM IR level — the `alloca` instruction lowers to a local memory reservation. However, truly dynamic-sized allocations (variable-length arrays, runtime `alloca(N)`) are constrained:

- `MachineFrameInfo.hasVarSizedObjects` (flag at +36) tracks whether the function contains VLA-style allocations.
- When present, PEI selects `StackAlign` (the full stack alignment) rather than `TransientStackAlign` for final frame rounding.
- ptxas ultimately resolves dynamic allocations at JIT time, not cicc. cicc's role is to set up the frame pointer correctly so that dynamic objects can be addressed relative to it.
- The `FramePointerIndex` (at `MachineFrameInfo+68`) is laid out last among general objects, ensuring the frame pointer anchors the top of the fixed frame with dynamic objects growing beyond it.

For fixed-size allocas, SROA (Scalar Replacement of Aggregates) typically promotes them to SSA registers before PEI ever runs. When SROA succeeds for all allocas, `MachineFrameInfo` has no stack objects and PEI emits no `__local_depot` at all — the function runs entirely in registers.

## Spill Slots

Register spills are the primary consumer of `__local_depot` space. When the register allocator cannot fit a virtual register's live range into the available physical registers, it creates a spill slot — a frame object marked with `isSpillSlot = 1` (byte at frame-object +32).

Spill-slot frame objects are created during register allocation. PEI does not create them; it only assigns their offsets. In cicc, spill slots interact with the categorized layout:

- Spill slots in a separate stack area (when `hasSeparateStackArea` is set) are excluded from the general layout and handled in Phase 4's separate-area processing.
- Remaining spill slots are classified into categories 1–3 based on their alignment requirements and register class — vector register spills (e.g., 128-bit `%rq` registers) end up in category 3, scalar spills in category 1.

After PEI assigns offsets, the spill loads/stores reference `[%SP + offset]` or `[%SPL + offset]` directly. The post-PEI `NVPTXPeephole` pass optimizes these: when a `LEA_ADDRi64 %VRFrame64, offset` feeds directly into `cvta_to_local_64`, the peephole collapses this to `LEA_ADDRi64 %VRFrameLocal64, offset`, saving the generic address conversion.

## Interaction with SROA

SROA runs early in the optimization pipeline (see [SROA](./sroa.md)) and aggressively promotes `alloca` instructions to SSA values. For many GPU kernels — especially those that avoid taking addresses of locals — SROA eliminates all allocas, resulting in an empty `MachineFrameInfo`. In this case:

1. PEI's frame size computes to 0.
2. The PTX emitter (`sub_2158E80`) checks `FrameInfo.StackSize`; if zero, it emits no `.local` directive and no `%SP`/`%SPL` declarations.
3. The function runs entirely in the virtual register file — the ideal case for GPU performance.

When SROA cannot promote (address-taken locals, aggregates too large for SROA's threshold controlled by `sroa-size-limit`, or when `sroa-skip-mem2reg` is set), PEI becomes essential. Additionally, cicc has a custom **MI Mem2Reg** pass (`nv-disable-mem2reg` controls it) that runs post-register-allocation and promotes MachineIR local-memory accesses back to registers — effectively a second chance at eliminating `__local_depot` usage after regalloc.

## Comparison with Upstream

| Aspect | Upstream `NVPTXPrologEpilogPass` | cicc `sub_35B1110` |
|---|---|---|
| Size | 280 lines | ~2,400 lines |
| Callee-saved regs | Not handled | Full save/restore infrastructure |
| Register scavenging | Not used | Both forward and backward paths |
| Layout algorithm | Single linear pass over all objects | Categorized 3-bucket layout + bitmap packing |
| Frame packing | None — objects placed sequentially | tzcnt-accelerated bitmap hole-finding |
| Stack direction | Supports both, simple | Supports both, with per-direction callee-save adjustment |
| Diagnostics | None | `warn-stack-size` attribute + annotation output |
| Separate stack area | Not supported | Full support (flag at MFI+665) |
| Arch-specific prolog | None | SM arch code 0xB extension |
| Optimization gating | None | opt-level 20 skips prolog/epilog emission |
| Frame-index elimination | Single backward walk | Dual forward/backward strategies |

The upstream pass explicitly disables LLVM's standard `PrologEpilogCodeInserterID` and replaces it. cicc's version is closer to the full standard LLVM PEI but with GPU-specific extensions — it re-enables callee-saved handling, register scavenging, and the frame-rounding logic that upstream strips out.

## Configuration

| Knob | Type | Default | Effect |
|---|---|---|---|
| `warn-stack-size` | Function attribute (string→int) | 0xFFFFFFFF (disabled) | Emit diagnostic when frame size exceeds threshold |
| `nvptx-short-ptr` | `cl::opt<bool>` | false | Use 32-bit pointers for local/const/shared address spaces; affects `%SPL` width |
| `nv-disable-mem2reg` | `cl::opt<bool>` | false | Disable post-regalloc MI Mem2Reg pass (more objects remain for PEI to lay out) |
| `sroa-size-limit` | `cl::opt<int>` | (varies) | Max aggregate size SROA will promote; larger values reduce PEI workload |
| Opt-level flag 20 | Internal | — | Skips prolog/epilog instruction emission and callee-save handling |
| Opt-level flag 55 | Internal | — | Includes regspill area in stack-size diagnostic total |
| `FrameLowering[12]` | Subtarget | arch-dependent | Stack alignment for functions with calls/alloca |
| `FrameLowering[13]` | Subtarget | arch-dependent | Stack alignment for leaf functions (TransientStackAlign) |

## Key Data Structures

### MachineFrameInfo (at MachineFunction+48)

```
Offset  Type   Field
+8      ptr    Objects array base pointer (40-byte records)
+16     ptr    Objects array end pointer
+32     i32    NumFixedObjects
+36     u8     hasVarSizedObjects
+48     i64    StackSize  ← WRITTEN by PEI
+64     u8     MaxAlignment (log2)
+65     u8     hasCalls / needsStackAlignment
+68     i32    FramePointerIndex (-1 if none)
+80     i64    MaxCallFrameSize (-1 if unknown)
+96     ptr    Separate-area array base
+104    ptr    Separate-area array end
+120    u8     hasCalleeSaves  ← SET by PEI
+128    ptr    Extra-area array pointer
+136    i64    Extra-area count
+656    i64    Separate area total size
+664    u8     Separate area alignment
+665    u8     hasSeparateStackArea flag
```

### PEI State (pass object, offset from `a1`)

```
Offset  Type   Field
+8      ptr    Analysis list (tagged analysis pointers)
+200    ptr    Callee-save info (0xA8-byte struct, or null)
+208    u32    First CSR frame index
+212    u32    Last CSR frame index
+216    ptr    Prolog insertion points array
+224    u32    Prolog point count
+264    ptr    Epilog insertion points array
+272    u32    Epilog point count
+312    u8     hasReservedCallFrame flag
+313    u8     requiresRegisterScavenging flag
+320    ptr    Stack-size annotation analysis pointer
```

### Frame Object Record (40 bytes each)

```
Offset  Type   Field
+0      i64    Byte offset in __local_depot (assigned by PEI)
+8      i64    Object size in bytes
+16     u8     Alignment (log2 encoding)
+20     u8     isDead flag
+32     u8     isSpillSlot flag
+36     u8     Category: 0=skip, 1=general, 2=medium, 3=large
```

## Diagnostic Strings

| String | When emitted |
|---|---|
| `"warn-stack-size"` | Function attribute name — read and parsed as an integer threshold |
| `"stack frame size"` | Diagnostic message when total frame size exceeds the `warn-stack-size` threshold |

## Function Map

| Function | Address | Size | Role |
|---|---|---|---|
| `PrologEpilogInserter::runOnMachineFunction` — main entry (68 KB) | `sub_35B1110` | -- | -- |
| PEI pre-setup: initialize frame object tracking | `sub_35AC440` | -- | -- |
| Record frame object into chunk table | `sub_35AFAD0` | -- | -- |
| Determine CSR frame index range (writes PEI+208, +212) | `sub_35AEEB0` | -- | -- |
| Post-save fixup | `sub_35AE230` | -- | -- |
| Insert restore instructions at epilog points | `sub_35ADBC0` | -- | -- |
| Assign offsets to categorized frame object bucket | `sub_35B0830` | -- | -- |
| Push frame object index into categorized bucket | `sub_35B0B10` | -- | -- |
| Post-prolog/epilog fixup | `sub_35AC7B0` | -- | -- |
| Try to eliminate a single frame index operand | `sub_35ABF20` | -- | -- |
| Initialize register scavenger for a MBB | `sub_35C5BD0` | -- | -- |
| Advance register scavenger | `sub_35C5C00` | -- | -- |
| Post-scavenging callee-save cleanup | `sub_35C6D20` | -- | -- |
| Format stack-size annotation | `sub_35AE7D0` | -- | -- |
| PTX emitter: `emitFunctionFrameSetup()` (__local_depot + %SP/%SPL) | `sub_2158E80` | -- | -- |
| Local depot helper | `sub_214C040` | -- | -- |
| Local depot helper | `sub_2154370` | -- | -- |
| Collect callee-saved registers | `sub_2E77EA0` | -- | -- |
| Get register class for physical register | `sub_2FF6500` | -- | -- |
| Build sub-register decomposition list | `sub_2F26260` | -- | -- |
| Insert compound save instruction | `sub_2E8EAD0` | -- | -- |
| Check optimization level flag | `sub_B2D610` | -- | -- |
| Check function attribute existence | `sub_B2D620` | -- | -- |
| Get function attribute value | `sub_B2D7E0` | -- | -- |
| Build stack-size diagnostic message | `sub_B15960` | -- | -- |

## Differences from Upstream LLVM

| Aspect | Upstream LLVM (NVPTX open-source) | CICC v13.0 |
|--------|-----------------------------------|------------|
| **Implementation** | Stripped-down `NVPTXPrologEpilogPass` (~280 lines); handles only offset calculation and frame-index elimination | Full 68 KB PEI monolith with callee-saved register handling, register scavenging, bitmap-based frame packing, categorized layout ordering |
| **Stack concept** | No hardware stack; minimal `__local_depot` offset assignment | Same `__local_depot` model but with full-featured offset assignment: categorized frame objects, alignment-based bucketing, dead frame object elimination |
| **Callee-saved registers** | Skipped entirely (no function calls in typical kernels) | Restored: full callee-saved register scan, compound save/restore instruction insertion for non-inlined device function calls |
| **Register scavenging** | Absent | Included: `sub_35C5BD0`/`sub_35C5C00` initialize and advance a register scavenger per MBB for emergency spill resolution |
| **Frame packing** | Sequential offset assignment | Bitmap-based packing with categorized buckets; objects sorted by alignment to minimize padding waste |
| **Stack-size diagnostics** | No diagnostic system | Annotation system (`sub_35AE7D0`) formats stack-size remarks; integrates with `-Rpass-analysis` for occupancy tuning |
| **Prologue emission** | Two-instruction `%SP`/`%SPL` setup | Same two-instruction prologue (`sub_2158E80`) but with additional `__local_depot` sizing logic for complex frame layouts |

## Cross-References

- **[Register Allocation](./register-allocation.md)** — creates spill slots that PEI lays out; the number and alignment of spills directly determines frame size.
- **[Register Coalescing](./register-coalescing.md)** — reduces register pressure, which reduces spills, which reduces frame size.
- **[SROA](./sroa.md)** — SROA eliminates allocas before they reach MachineIR; when fully successful, PEI has nothing to do.
- **[AsmPrinter & PTX Body Emission](../infra/asmprinter.md)** — `sub_2158E80` emits the `.local` directive and `%SP`/`%SPL` declarations that PEI computed.
- **[Instruction Scheduling](./scheduling.md)** — runs before PEI; scheduling decisions affect register pressure and thus spill count.
- **[Pipeline & Ordering](./pipeline.md)** — PEI runs post-regalloc, followed immediately by NVPTXPeephole for `%VRFrame` to `%VRFrameLocal` optimization.
