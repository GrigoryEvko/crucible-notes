# Validation & Lowering Passes

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

This page documents fourteen high-leverage phases that validate the initial Ori IR, lower target-unsupported and macro constructs into explicit instruction sequences, build the CFG, and instrument code. They span the pipeline from the index-0 consistency walk through post-schedule attribute back-propagation. Each is a 16-byte command object built by the factory `sub_C60D30`; the dispatch loop `sub_C64F70` calls vtable slot 0 (`Execute`), whose execute-thunk in the `0xC5E000`–`0xC60Cxx` band optionally calls the run-gate `sub_7DDB50` and then either tail-jumps to a direct worker or virtual-dispatches through the per-architecture profile (`Gb+0x630`) or shaderType object (`Gb+0x640`).

The full per-phase transform detail, with byte-level offsets and worker addresses, is reproduced as a [reference data page](../reference/data/passes-detail.md). The complete 159-phase registry is the [phase pipeline table](../reference/data/phase-pipeline.md).

| bin | phase | kind |
|----:|-------|------|
| 0 | `OriCheckInitialProgram` | Validation (debug-gated, read-only) |
| 2 | `PromoteFP16` | Lowering |
| 3 | `AnalyzeControlFlow` | Analysis (CFG build) |
| 9 | `OriCreateMacroInsts` | Lowering (macro packer) |
| 13 | `OriSanitize` | Instrumentation |
| 39 | `ExtractShaderConstsFirst` | Optimization |
| 45 | `ConvertVTGReadWrite` | Lowering (profile virtual) |
| 46 | `DoVirtualCTAExpansion` | Lowering (profile virtual) |
| 49 | `ForwardProgress` | Lowering (profile virtual) |
| 55 | `EnforceArgumentRestrictions` | Validation / legalization (profile virtual) |
| 59 | `ExtractShaderConstsFinal` | Optimization |
| 94 | `ExpandJmxComputation` | Lowering |
| 110 | `FinalInspectionPass` | Validation (shaderType virtual) |
| 116 | `BackPropagateVEC2D` | Analysis (post-schedule) |

> The `bin` index is the authoritative identity — it is the factory `sub_C60D30` switch case and the phase name-table index at `off_22BD0C0`. It is the number that `DUMPIR` / `NamedPhases` resolve to.

## The OptBudget Gate — `sub_7DDB50`

A single function backs the `OPT` / `NOOPT` predicates that appear in most execute-thunks. It is **not** a raw opt-level getter. It reads the OCG knob object (`Gb+0x1664`) and consults knob **499** (the optimization-budget knob) at the 72-byte knob-entry stride (`entry = knobs[9] + 72*499 = +0x8C58`): `byte+0` = active, `dword+8` = limit, `dword+12` = running count. While under budget it returns the configured effective opt level `*(Gb+0x838)` and increments the count; **once the budget is exhausted it returns the literal `1`** — a forced-O0 sentinel. The per-thunk comparison therefore reads:

| Thunk comparison | Meaning |
|---|---|
| `cmp $1; je`  → return `== 1` | **NOOPT** (opt level 0, or budget burned) |
| `cmp $1; jg`  → return `> 1`  | **optimizing** (OPT) |
| `cmp $1; jle` → skip phase unless OPT | run only when optimizing |

The default knob accessor is `sub_6614A0` (`byte[knobs[9] + 72*idx] != 0`); thunks compare the resolved profile slot against it before reading a knob byte directly. Because this gate sits inside each phase's own execute path, a "skipped" phase is one whose `execute()` runs and returns early — it is never removed from the 157-entry dispatch order.

## Profile-Virtual Dispatch

Several lowering phases do not have a fixed worker. Instead, their execute-thunk loads the per-architecture target profile (`Gb+0x630`) and tail-calls a fixed vtable slot on it. The base profile vtable lives at VMA `0x21CDCB8`, and most of its slots point at an **empty `repz ret` stub** — so on architectures that do not need the transform, the phase is a no-op. A profile that needs the transform installs a non-stub override at that slot.

| Phase | Profile slot | Base stub | Override behavior |
|---|---|---|---|
| `ConvertVTGReadWrite` (45) | `+0xA8` | `sub_7D6BB0` (empty) | VTG-capable profiles → `sub_8116B0`: rewrite vertex/tess/geometry varying I/O regs into memory READ/WRITE intrinsics |
| `DoVirtualCTAExpansion` (46) | `+0x1C0` | `sub_7D6DD0` (empty) | mesh/task (amplification) profiles install an override; compute/graphics-non-mesh keep the stub |
| `ForwardProgress` (49) | `+0xB8` | `sub_7D6BD0` (empty) | derived override `sub_BEE590`: insert YIELD/WARPSYNC for guaranteed forward progress |
| `EnforceArgumentRestrictions` (55) | `+0xD0` | `sub_7D6C00` (empty) | derived override `sub_13B5C80`: enforce per-arch operand legality |

A guarded variant checks whether the resolved slot equals the base stub and skips the call if so (e.g. `DoVirtualCTAExpansion`); a pure variant tail-calls unconditionally and relies on the stub being a no-op (e.g. `ConvertVTGReadWrite`, `ForwardProgress`).

## Per-Phase Detail

### OriCheckInitialProgram (bin 0)

A debug-gated, read-only structural consistency walk over the initial Ori IR, sitting immediately ahead of the index-0 FP16 promotion work. The worker `sub_7EF260` gates on `(ctx+0x566 & 0x401) != 0` — a two-bit verify/debug mask (bit 0 + bit 10); when clear it is an immediate `repz ret`. When set it builds a 16-byte scope record `[ctx, theProfile]` and runs an RAII enter/inspect/leave chain (`sub_7EA6C0` → `sub_7EF090` → `sub_7EA8B0`) that walks the program node graph (`call *0x8(rax)` / `call *0x460(rax)`) and asserts on inconsistency. It inspects but never transforms; in normal (non-debug) builds it is a no-op.

### PromoteFP16 (bin 2)

Promotes native-FP16 operations to FP32 sequences on targets that lack hardware FP16. Worker `sub_7EEB10`, gated on `ctx+1377 & 0x40` (HasHalfInst) **and** `ctx+1418 & 0x40` (EnableFP16PromotionPhase). It unconditionally clears HasHalfInst (`ctx+1377 &= 0xBF`) — the flag is invalid from here until recomputed before MidExpansion — zeroes a per-vreg scratch dword, collects def/ref for vregs, then walks the instruction list; for nodes whose type field `+76 == 7` (DT_HALF) it calls `IsFP16Promotable` and, on success, `PromoteFP16Inst`.

### AnalyzeControlFlow (bin 3)

The canonical CFG (re)builder: basic blocks, predecessor/successor edges, loop headers/depth/back-edges, reachability. Its thunk gates on `sub_7DDB50(ctx) == 1` (NOOPT) and a knob check (`DISABLESOURCEORDER` at `oriKnobs + 0x4218`), then calls `sub_781F80(ctx, 0)`. The worker (~131 callers) with `a2 = 0` does a structural non-mutating build (default); `a2 != 0` adds RPO loop-rotation and branch-straightening. It builds the ordered block index array at `ctx+512`, per-block predecessor list at `block+0x80` and successor list at `block+0x88` (intrusive edge nodes), and packs loop-header/back-edge/reachable bits in the block flags dword `block+0x118`. It partitions instructions by normalizing the masked opcode at `node+72` (`BYTE1 & 0xCF`): opcode `0x61` = `OP_LABEL` starts a block; branch/exit opcodes terminate one. On completion it sets `ctx+1370` bit 2 (CFG-valid) and bit 3 (analysis-strength = `a2 & 1`).

### OriCreateMacroInsts (bin 9)

Expands packed multi-result macro pseudo-instructions into explicit per-result definition sequences. Worker `sub_19DFC20`, gated on `ctx+0x564` bit 7 (HasTTU early-out). It walks the instruction list matching the masked opcode class `(node+0x48 & 0xCF00) == 0xFC00` (OP_TTUOPEN, the sequence head), with sub-class handling for `0xFD00` and `0xFA00` operand legalization and an `OP_MACRO` dissolve path keyed on `0x6D00`. On a match it allocates and zero-inits a 0x40-byte macro record via `sub_7DD250` (sets `*rec = 0xFFFFFFFF`, `+0x18 = 3`), grows the per-function definition table through the `ctx+0x188/+0x190/+0x194` base/count/cap triple (geometric growth), and splices a new MACRO node into the doubly-linked list (writing `node+0x20/+0x28` = begin/end instruction, setting flag bits, recomputing the uniformity byte at `+0x14`), then emits the replacement via `sub_19DFBD0`. The body gates on HasTTU, scans for the open opcode, legalizes operands via inserted MOVs, sets the macro instruction range, splices the constituents out, and emits the replacement.

> The neighboring thunk `0xC5F8E0` belongs to a **different** phase (bin 11, `EarlyOriSimpleLiveDead`, worker `sub_A112C0`) and gates on `ctx+0x588` bit 7. The two must not be conflated — the canonical `OriCreateMacroInsts` thunk is `0xC5F8D0` → `sub_19DFC20`.

### OriSanitize (bin 13)

Despite the generic name, the worker `sub_1C64BF0` is the **compute-sanitizer device-instrumentation injector**, not a generic IR validator. This identity is established entirely from the disassembly and byte-verified `.rodata` strings. It gates on `ctx+0x584` bit 7, with an architecture gate (`theProfile + 0x174 > 0x4FFF`, an sm-version threshold) and a sanitizer-mode name string that must be non-null. It string-compares the mode against `"memcheck"` (metadata-init path) and `"threadsteer"` (instrumentation-walk path), and emits symbols prefixed `"__cuda_sanitizer"`:

- **threadsteer path:** walks the instruction list keying on masked opcode `(node+0x48 & 0xCF00)` in `{0x10, 0x7D, 0xB7, 0x120}` (load/store/atomic class), queries operand-type and node-attribute records (`flags & 8` = needs-instrument), and materializes a runtime-call sequence (opcode `0x91`, descriptor `0x60000001`).
- **memcheck path:** builds a 6-entry table of `__cuda_sanitizer_memcheck_{generic,global,local,malloc,readmetadata}` ids and runs the metadata finalizer.

All four mode/symbol strings are byte-verified in `.rodata` (`"memcheck"`, `"threadsteer"`, `"__cuda_sanitizer"`, and the packed `__cuda_sanitizer_memcheck_*` name table at `0x1D17740`).

### ExtractShaderConsts — First (bin 39) and Final (bin 59)

Both share worker `sub_1C72640`; the boolean `a2` selects extract (First, `a2=0`) vs finalize (Final, `a2=1`). Both gate on `sub_7DDB50 > 1` (OPT).

- **First (`a2=0`):** computes `remainingConstBytes = (block-range query) − *(ctx+928)` and takes an early-out unless that budget `> 3`. It allocates def/ref bookkeeping bitmaps sized `(numVregs+64)>>6`, per-const-bank slot maps, and worklists, then runs `MarkShaderConstDefs → UnmarkLeastProfitableConstDefs → ExtractShaderConstDefs(false)`: reachable const-def expression trees are replicated into the priming/setup-shader region and main-shader references rewritten to const-bank reads. It **skips** the finalize commit so it may run again. Register ids extracted as `operand & 0xFFFFFF` (24-bit).
- **Final (`a2=1`):** bypasses the `remainingConstBytes ≤ 3` early-out (it must run even with no const space, to commit consts a prior pass already extracted) and runs the finalize commit `sub_1C68760` (FinalizeSetupShader): it allocates the setup function if needed, materializes the const-bank setup instructions, and re-runs the CFG builder (`sub_781F80`) when constants are present. A post-rewrite consistency check walks every setup-function block, reads each operand register id (`& 0xFFFFFF`), and asserts the rewritten const-bank reference's loop-id (`*(v+66)`) matches the target block field (`+164`); a mismatch (or an unreached operand) raises a `BUG()`. This guards that const extraction never moved a reference across a loop or block boundary.

### ConvertVTGReadWrite (bin 45)

Converts vertex/geometry/tessellation shader input/output registers into explicit READ/WRITE memory operations. Pure profile-virtual dispatch on `theProfile` slot `+0xA8`, no run-gate (always runs). The base impl `sub_7D6BB0` is an empty `repz ret`, so compute and non-VTG profiles no-op. VTG-capable profiles override `+0xA8` → `sub_8116B0`, which walks the instruction list, classifies operands by register class, and rewrites varying input/output regs into memory READ (profile slot `+0x998`) / WRITE (slot `+0x9A0`) intrinsics plus opcode-`0xB7` emission, clearing the output-register map.

### DoVirtualCTAExpansion (bin 46)

Expands virtual-CTA constructs for mesh/task (amplification) shaders into physical CTA primitives. Guarded profile-virtual dispatch on slot `+0x1C0`: if the slot resolves to the base impl `0x7D6DD0` (a confirmed `repz ret`), the phase is skipped; otherwise it tail-calls. Both inspected non-graphics profiles keep the stub, so virtual-CTA expansion runs only on mesh/task profiles, which install a non-stub override gated on `IsMeshProfile() || IsTaskProfile()`. No run-gate.

### ForwardProgress (bin 49)

Guarantees per-thread forward progress by inserting YIELD/WARPSYNC instructions. Pure profile-virtual dispatch on slot `+0xB8`, taking no `Gb` argument; no run-gate. The base impl `sub_7D6BD0` is empty. The derived override `sub_BEE590` reads per-function flags at `profile+0x584/+0x587`; on the active path it scans for opcode 145 (a barrier/branch-class op) whose last-operand bit 2 is clear and rewrites each, then sets a forward-progress-applied marker (`ctx+0x55c = 1`). In dispatch order it runs after `ExpandMbarrier` and before `OptimizeUniformAtomic` (which can cause data divergence).

### EnforceArgumentRestrictions (bin 55) and its late pair (bin 103)

Enforces per-architecture operand/argument legality — register classes, alignment, const-bank operand limits, operand-count restrictions. Gated on `sub_7DDB50 > 1` (OPT), then profile-virtual dispatch on slot `+0xD0` with `FinalCall = false`. The base impl `sub_7D6C00` is empty; the derived override `sub_13B5C80` iterates operands and emits opcode-`0xB7` fixups plus reorder/spill instructions.

`LateEnforceArgumentRestrictions` (bin 103, execute thunk `0xC5E880`) calls the **same** profile slot `+0xD0` but with `FinalCall = true` and **no** opt-gate (always runs). The two are a binary-proven pair: bin 55 runs `FinalCall = false` under the OPT gate; bin 103 runs `FinalCall = true` unconditionally.

### ExpandJmxComputation (bin 94)

Lowers the JMX/BRX indexed multi-way branch from a `JMX_SRC_INDEX` form into a concrete address computation, before scheduling. Worker `sub_7EE060`, gated on `ctx+0x560 & 0x40` (HasJmx). It computes the per-block address shift from the profile's instruction-size slots (`MAX(+0x820, +0x828)`, then `floor(log2)` via a shift loop), walks the blocks, and for a terminating `OP_JMX` (opcode `0x5E`, `node+0x48 & 0xCF00 == 0x5E00`) whose variant word `node+0x5c` has the `JMX_SRC_INDEX` bit set, selects a strategy by `node+0x54` bit 2:

- **JMX_TAB_INCBANK:** reserves a const-bank jump table via profile slot `+0x260`, emits `OP_SHL #2` (index→byte-addr), and sets the const-bank symbol-operand encode bits.
- **JMX_TAB_INCODE** (const bank unavailable): emits `OP_SHL` by the computed `shift` (profile slot `+0x248`) and sets the impl-code encode bits; on the failure subpath it also sets `ctx+0x560 |= 0x80` (HasJmxImplCode).

Both strategies finish by clearing the `JMX_SRC_INDEX` bit (`node+0x5c &= ~1`), converting `JMX_SRC_INDEX → JMX_SRC_LABEL`. The actual in-code table layout is deferred to a later post-fixup.

### FinalInspectionPass (bin 110)

A final per-shaderType legality inspection before irreversible scheduling and register allocation. It reads the shaderType object at `ctx+0x640` and applies a default-impl guard: if its vtable slot `+0x60` equals the base `sub_661310`, it runs the canonical two-stage inspection inline — load the inner pipeline object at `shaderType+0x10`, call its slot `+0x118` (pre-check), reload, and tail its slot `+0xC10` (main inspection). Otherwise a specialized shaderType has overridden `+0x60`, and a single direct dispatch on the shaderType itself is taken. The base `sub_661310` is exactly `(*(*(a1+16)+280))(*(a1+16)); return (*(*(a1+16)+3088))(*(a1+16));`. No run-gate.

### BackPropagateVEC2D (bin 116)

Back-propagates the VEC2 2D (paired-register) attribute after scheduling. Gated on `sub_7DDB50 > 1` (OPT); the worker `sub_90B6A0` gates again on `ctx+0x559 & 0x40` (VEC2D back-prop enabled). It allocates a 24-byte worklist control node from the per-context arena, seeds it via `sub_90AB90`, then drains the worklist in reverse-CFG order until empty. The seed first re-derives CFG/region structure, then iterates instructions: for each whose masked opcode (`node+72`, `BYTE1 & 0xCF`) `== 272` (`OP_VEC2D`) with a clean modifier mask, it resolves the destination vreg id (`*(node+84) & 0xFFFFFF`), checks the vreg descriptor's paired/64-bit flag, queries the profile's VEC2D-legality gate, and on success pushes a propagation node so the worklist walks predecessor edges backward. The propagated attribute is the even-aligned register-pair constraint: a VEC2D consumer forces its two source vregs into an aligned consecutive pair, pushed back to the defining instructions. It runs post-`ScheduleInstructions` and before `AllocateRegisters` because instruction order and pairing are only fixed after scheduling, so the pairing constraint must be re-pushed to defs before regalloc.

## Cross-References

- [Pass Inventory & Ordering](index.md) — the full 159-phase registry
- [Phase Manager Infrastructure](phase-manager.md) — factory, dispatch loop, 157-vs-159
- [AnalyzeControlFlow (CFG Rebuild)](analyze-control-flow.md) — the full CFG builder
- [Shader Constant Extraction](shader-const-extraction.md) — `ExtractShaderConsts` First/Final
- [Optimization Levels](../config/opt-levels.md) — the opt-level model and the OptBudget knob
- [Validation & Lowering Pass Detail (data)](../reference/data/passes-detail.md) — byte-level per-phase transforms
