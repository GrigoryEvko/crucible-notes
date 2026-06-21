# Validation & Lowering Pass Detail (14 phases)

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base 0x400000 (non-PIE).*

Deep-dive detail for 14 high-leverage validation/lowering/analysis phases. Each phase is a 16-byte command object built by the factory `sub_C60D30`; the dispatch loop `sub_C64F70` calls vtable slot 0 (Execute), whose execute-thunk (in the `0xC5E000`-`0xC60Cxx` band) optionally calls the OptBudget gate `sub_7DDB50` and then tail-jumps to the worker or virtual-dispatches through the per-architecture profile (`Gb+0x630`) or shaderType (`Gb+0x640`). The `bin_index` is the authoritative factory/name-table index. See [Validation & Lowering Passes](../../passes/validation-and-lowering.md) for the grouped narrative.

## Summary

| bin | phase | vtable_va | execute_thunk_va | worker / slot | dispatch_kind | role |
|---|---|---|---|---|---|---|
| 0 | `OriCheckInitialProgram` | 0x22BD5C8 | 0xC5F6D0 | `sub_7EF260` | direct tail-jmp (mov rsi,rdi; jmp 0x7EF260) | Structural well-formedness validation of the initial Ori IR right after PTX->Ori lowering |
| 2 | `PromoteFP16` | 0x22BD618 | 0xC5F6F0 | `sub_7EEB10` | direct call (sub 0x18; lea 8(rsp),rdi; mov rsi,8(rsp); call 0x7EEB10) | Promote native-FP16 ops to FP32 sequences on targets lacking HW FP16 |
| 3 | `AnalyzeControlFlow` | 0x22BD640 | 0xC60870 | `sub_781F80(ctx,0)` | gated direct tail-jmp (run-gate sub_7DDB50==1 + profile knob check + jmp) | Canonical CFG (re)builder: basic blocks, pred/succ edges, loop headers/depth/back-edges, reachability |
| 9 | `OriCreateMacroInsts` | 0x22BD730 | 0xC5F8D0 | `sub_19DFC20` | direct tail-jmp (mov rsi,rdi; jmp 0x19DFC20) | Expand macro / packed multi-result pseudo-instructions into explicit per-result def sequences |
| 13 | `OriSanitize` | 0x22BD7D0 | 0xC5F940 | `sub_1C64BF0` | direct tail-jmp (mov rsi,rdi; jmp 0x1C64BF0) | Compute-sanitizer (memcheck / threadsteer) instrumentation injector -- NOT a generic IR validator |
| 39 | `ExtractShaderConstsFirst` | 0x22BDBE0 | 0xC5FDA0 | `sub_1C72640(ctx,0)` | gated direct tail-jmp (run-gate sub_7DDB50>1; xor esi,esi; jmp) | First (early) extraction of shader constants into constant banks |
| 59 | `ExtractShaderConstsFinal` | 0x22BDF00 | 0xC5FDD0 | `sub_1C72640(ctx,1)` | gated direct tail-jmp (run-gate sub_7DDB50>1; mov esi,1; jmp) | Final shader-const extraction; finalizes/commits const banks |
| 45 | `ConvertVTGReadWrite` | 0x22BDCD0 | 0xC5F0A0 | `profile-vtable slot +0xA8` | pure virtual on target profile (mov 0x630(rsi),rdi; jmp *0xA8(*rdi)) | Convert VTG (vertex/geom/tess) shader input/output regs to explicit READ/WRITE memory ops |
| 46 | `DoVirtualCTAExpansion` | 0x22BDCF8 | 0xC60840 | `profile-vtable slot +0x1C0` | guarded virtual on target profile (slot+0x1C0; if==default 0x7D6DD0 repz ret else jmp) | Expand virtual-CTA constructs for mesh/task (amplification) shaders into physical CTA primitives |
| 49 | `ForwardProgress` | 0x22BDD70 | 0xC5F000 | `profile-vtable slot +0xB8` | pure virtual on target profile (mov 0x630(rsi),rdi; jmp *0xB8(*rdi)) | Guarantee per-thread forward progress: insert YIELD/WARPSYNC as needed |
| 55 | `EnforceArgumentRestrictions` | 0x22BDE60 | 0xC60510 | `profile-vtable slot +0xD0` | gated virtual on target profile (run-gate sub_7DDB50>1; jmp *0xD0(*profile)) | Enforce per-arch operand/argument legality (register classes, alignment, const-bank operand limits) |
| 94 | `ExpandJmxComputation` | 0x22BE478 | 0xC601A0 | `sub_7EE060` | direct tail-jmp (mov rsi,rdi; jmp 0x7EE060) | Lower JMX/BRX indexed multi-way branch from JMX_SRC_INDEX to concrete address computation, pre-legalization |
| 110 | `FinalInspectionPass` | 0x22BE6F8 | 0xC60B30 | `shaderType-vtable slots +0x118 then +0xC10` | chained guarded virtual on shaderType object | Final per-shaderType legality inspection before irreversible scheduling/RA |
| 116 | `BackPropagateVEC2D` | 0x22BE7E8 | 0xC60250 | `sub_90B6A0` | gated direct tail-jmp (run-gate sub_7DDB50>1; mov rdi,rbx; jmp 0x90B6A0) | Back-propagate VEC2 2D (paired-register) attributes after scheduling |

## Per-phase key transform

### OriCheckInitialProgram (bin 0)

**Dispatch:** direct tail-jmp (`mov rsi,rdi; jmp 0x7EF260`). **Worker:** `sub_7EF260`.

- **Gate:** `(ctx+0x566 & 0x401) != 0` — a two-bit verify/debug mask (bit 0 + bit 10); `repz ret` when clear, so a no-op in non-debug builds.
- **Scope record:** reads `theProfile` (`ctx+0x630`), builds a 16-byte `[ctx, theProfile]` record on the stack, runs an RAII enter/inspect/leave chain.
- **enter** (`sub_7EA6C0`): reads `ctx->[0x110]` first-inst / Gb-state, scans with packed mask `0x2080000010000001`.
- **inspect** (`sub_7EF090`): the actual checks — `sub $0x208` frame, derefs `ctx+8->[+0x848]`, compares a vtable slot vs `0x7D7960`, walks the program node graph via `call *0x8(rax)` / `call *0x460(rax)`.
- **leave** (`sub_7EA8B0`): pops the `ctx+8->[+0x290]` teardown record.
- **Nature:** read-only DAG/consistency validation — inspects, never transforms.
- **Confidence:** HIGH that it is debug-gated read-only validation; MEDIUM on the exact public-name identity.

> **Note:** this name appears nowhere else, and index-0 otherwise begins with FP16 promotion — the shipping binary prepends this double-flag-gated consistency walk ahead of FP16. Its inspect-walk semantics match a debug-DAG assert-on-failure validator. Binary wins.

### PromoteFP16 (bin 2)

**Dispatch:** direct call (`sub 0x18; lea 8(rsp),rdi; mov rsi,8(rsp); call 0x7EEB10`). **Worker:** `sub_7EEB10`.

- **Gate:** `ctx+1377 & 0x40` (HasHalfInst) **and** `ctx+1418 & 0x40` (EnableFP16PromotionPhase).
- **Flag reset:** unconditionally clears HasHalfInst (`ctx+1377 &= 0xBF`) — invalid from here until recomputed before MidExpansion (the else-branch also clears it).
- **Setup:** zeroes a per-vreg scratch dword at `vreg+84` over the vreg list at `ctx+104` (ClearFP16DefRefByNonPromotable); `sub_7EE4A0` collects def/ref for vregs.
- **Walk:** over the inst list at `ctx+272`, for nodes with type field `+76 == 7` (DT_HALF): `sub_7EE400` (IsFP16Promotable) → on success `sub_7EE8A0` (PromoteFP16Inst).
- **Confidence:** HIGH (full FP16-promotion flow recovered from the worker body).

### AnalyzeControlFlow (bin 3)

**Dispatch:** gated direct tail-jmp (run-gate `sub_7DDB50==1` + profile knob check + jmp). **Worker:** `sub_781F80(ctx, 0)`.

- **Thunk gate:** `sub_7DDB50(ctx) == 1` (NOOPT) + knob check (`DISABLESOURCEORDER` at `oriKnobs+0x4218`), then `sub_781F80(ctx, 0)`. Selection predicate = `NOOPT && !IsKnobActive(DISABLESOURCEORDER)`.
- **Gate primitive (`sub_7DDB50` = OptBudget oracle, not raw OptLevel):** consults knob 499 (ORI_KNOB_OPTBUDGET) at the 72-byte stride (`knobs[9] + 72*499 = +35928`: byte+0 active, dword+8 limit @+35936, dword+12 count @+35940). Returns the effective opt level `*(ctx+0x838)` while under budget (incrementing the count), or the literal `1` (forced-O0 sentinel) once exhausted. Hence `ret==1` = NOOPT, `ret>1` = optimizing.
- **Worker modes (~131 callers):** `a2=0` = structural non-mutating build (default); `a2!=0` adds RPO loop-rotation / branch-straightening + sourceBidxMap snapshot, with self-recursion.
- **Re-entry guard:** bail if `(ctx+1370 & 4)` set and (`a2==0` or `ctx+1370 & 8`).
- **Structures built:** ordered block-index array at `ctx+512` (count `ctx+520`, written at the OP_LABEL/`0x61` case); per-block predecessor list `block+0x80`, successor list `block+0x88` (intrusive edge nodes); loop-header/back-edge/reachable bits packed in block flags `block+0x118` (per-block reset `&= 0xFF78F98F`).
- **Partition:** walks `node+8`, normalizes opcode `node+72` (`BYTE1 & 0xCF`): `0x61` (OP_LABEL) starts a block; branch/exit opcodes terminate.
- **Completion:** `ctx+1370 = (8*(a2&1)) | (old & 0xB3) | 4` — bit 2 (CFG-valid) always set, bit 3 = analysis strength (`a2&1`).
- **Confidence:** HIGH (gate/offsets/bits confirmed against the decompiled worker; loop-depth interval machinery MEDIUM).

### OriCreateMacroInsts (bin 9)

**Dispatch:** direct tail-jmp (`mov rsi,rdi; jmp 0x19DFC20`). **Worker:** `sub_19DFC20`.

- **Identity caution:** the worker is `sub_19DFC20` (canonical thunk `0xC5F8D0`, unconditional jump) — **not** `sub_A112C0`, which is idx 11 `EarlyOriSimpleLiveDead` reached via the neighboring thunk `0xC5F8E0` (gate `ctx+0x588` bit 7). Do not conflate.
- **Gate:** head `cmpb $0,0x564(ctx); js` — `ctx+0x564` bit 7 = HasTTU early-out (`repz ret` if clear).
- **Walk / match:** inst list at `ctx+0x110`; matches opcode class `(node+0x48 & 0xCF00) == 0xFC00` (OP_TTUOPEN, sequence head); sub-classes `0xFD00` (TTUST) / `0xFA00` (TTUMFUSE) operand legalization; OP_MACRO dissolve path (worker `0x19DFE80`) keyed `== 0x6D00`.
- **Macro record (`sub_7DD250`):** allocates + zero-inits a 0x40-byte Ori def record (`*rec=0xFFFFFFFF`, `+0x18=3`, `+0x6 &= 0xF8`), grows the per-function def table via the `ctx+0x188/+0x190/+0x194` base/count/cap triple (`(cap+1)>>1` geometric growth), returns a macro-index; caller loads slot `ctx+0x188 + id*8`.
- **Splice + emit:** new MACRO node into the doubly-linked list (`node+0x20/+0x28` = begin/end inst; `orb 0x9c@+4`, `0x20@+5`; `orl 0x4000@+8/+0xc`; recompute uniformity byte `+0x14`); emit via `sub_19DFBD0` → `sub_9314F0(...,0x6D,...)`.
- **Identity:** a TTU ray-tracing macro packer — gates on HasTTU, scans OP_TTUOPEN, allocates/grows the macro record, legalizes TTUST operands via inserted MOVs, sets the macro instruction range, splices constituents out, emits the replacement.
- **Confidence:** HIGH (gate `ctx+0x564` bit 7, opcode `0xFC00`=TTUOPEN, and TTU identity all confirmed).

### OriSanitize (bin 13)

**Dispatch:** direct tail-jmp (`mov rsi,rdi; jmp 0x1C64BF0`). **Worker:** `sub_1C64BF0`.

- **Correction:** despite the public name, `sub_1C64BF0` is the **compute-sanitizer instrumentation pass** — a binary-only discovery, established from the disassembly and byte-verified `.rodata` strings.
- **Gate:** `ctx+0x584` bit 7 (`cmpb $0,0x584; js`). Arch gate: `theProfile+0x174 > 0x4FFF` (sm-version threshold); the sanitizer-mode string `(*ctx)+0x420` must be non-null.
- **Mode dispatch:** string-compares the mode (`repz cmpsb`) against `"memcheck"` (@`0x21ED5FC`, metadata-init path) and `"threadsteer"` (@`0x1CE3D43`, instrumentation-walk path); symbol names vs `"__cuda_sanitizer"` (@`0x1CE716A`); name table `__cuda_sanitizer_memcheck_{generic,global,local,malloc,readmetadata}` @`0x1D17740`.
- **threadsteer path:** walks insts (base `ctx+0x170`, count `ctx+0x178`) keying on masked opcode `(node+0x48 & 0xCF00)` in `{0x10, 0x7D, 0xB7, 0x120}` (load/store/atomic); queries `sub_7DFFC0` (operand-type classify) and `sub_7DF3A0` (node-attribute, `flags & 8` = needs-instrument); for `0xB7/0x120` derefs an operand symbol (`node+0x50 & 0xFFFFF`) via `ctx+0x98` and materializes a runtime-call sequence (`sub_7DAFF0` + builder `0x934630`, opcode `0x91`, descriptor `0x60000001`); stashes `node[0]→ctx+0xE8`, `node+0x14→ctx+0x108`.
- **memcheck path:** builds the 6-entry stack table of the `__cuda_sanitizer_memcheck_*` ids and runs the metadata finalizer (record dtor `sub_1C64830`).
- **Strings:** all four byte-verified in `.rodata`.
- **Confidence:** HIGH (binary).

### ExtractShaderConstsFirst (bin 39)

**Dispatch:** gated direct tail-jmp (run-gate `sub_7DDB50>1`; `xor esi,esi`; jmp). **Worker:** `sub_1C72640(ctx, 0)`.

- **Gate:** `sub_7DDB50 > 1` (OPT, the OptBudget oracle), then `sub_1C72640(ctx, a2=0=extract)`.
- **Budget early-out:** `remainingConstBytes = (block-range query) − *(ctx+928)`; with `a2=0`, returns unless `remainingConstBytes > 3` (equivalently: returns when `(!FinalCall || setupShader empty) && remainingConstBytes < 4`).
- **Allocations:** seven stack/arena tables (dtors `sub_1C68B60/1C690B0/1C68BE0/1C68C60/1C68CE0/1C68D60/1C69030`) + two vreg-count bitmaps sized `(numVregs+64)>>6` (`sub_6E6650`) backing def/ref bookkeeping, a per-const-bank slot map, and worklists.
- **Transform chain:** `MarkShaderConstDefs → UnmarkLeastProfitableConstDefs → ExtractShaderConstDefs(false)` (`sub_1C6F590→1C6DD40→1C6A230→1C72370`, each gated on budget + candidate-set non-empty): reachable const-def expr trees are replicated into the priming/setup-shader region, main-shader refs rewritten to const-bank reads.
- **a2=0:** skips the finalize commit `sub_1C68760` (may run again). Reg ids extracted as `operand & 0xFFFFFF` (24-bit).
- **Confidence:** HIGH.

### ExtractShaderConstsFinal (bin 59)

**Dispatch:** gated direct tail-jmp (run-gate `sub_7DDB50>1`; `mov esi,1`; jmp). **Worker:** `sub_1C72640(ctx, 1)`.

- **Gate:** `sub_7DDB50 > 1` (OPT), then `sub_1C72640(ctx, a2=1=finalize)` — same worker as First.
- **What a2=1 flips:** (1) bypasses the `remainingConstBytes ≤ 3` early-out — Final must run even with no const space, to commit consts a prior pass already extracted to the setup shader; (2) runs the finalize commit `sub_1C68760` (FinalizeSetupShader).
- **FinalizeSetupShader:** when setup-func id `*(ctx+932) < 0`, allocates the setup function (`sub_1C67570/sub_1C67750`); else materializes the const-bank setup insts via repeated `sub_934630` binary-emit of opcodes 283/67/195/201, wiring the const-bank entry chain.
- **CFG rebuild + emit:** with constants present (`(*(ctx+1664))+72`: byte+59040==1 **and** dword+59048!=0) and last-block terminator `!= opcode 188`: re-runs `sub_781F80` (CFG rebuild @`0x1C732C1`), then emits via `sub_7E6090` (@`0x1C732D2`) and `sub_A13890` (@`0x1C732EE`, const-bank loads); operand rewrites collected into a `sub_1C69460`-grown pair list.
- **Consistency check:** walks every setup-func block, reads each operand reg id (`& 0xFFFFFF`), asserts the rewritten const-bank ref's loop-id `*(v+66)` == target block `+164`; a mismatch (or unreached operand) → `BUG()`. Guards that extraction never moved a ref across a loop/block boundary. Finalized via `sub_8E3A20`.
- **Confidence:** HIGH.

### ConvertVTGReadWrite (bin 45)

**Dispatch:** pure virtual on target profile (`mov 0x630(rsi),rdi; jmp *0xA8(*rdi)`). **Slot:** profile-vtable `+0xA8`.

- **Dispatch:** pure tail vtable dispatch on `theProfile` (`ctx+0x630`) slot `+0xA8`; no run-gate (always runs).
- **Base stub:** `sub_7D6BB0` (empty `repz ret`, base vtable `0x21CDCB8`) — compute / non-VTG profiles no-op.
- **VTG override (`sub_8116B0`):** walks the inst list, classifies operands by register class (`sub_7DEB10`), and rewrites vertex/tess/geometry varying input/output regs into memory READ (profile slot `+0x998`) / WRITE (slot `+0x9A0`) intrinsics plus opcode-`0xB7` emission (`sub_930910`); clears OutputRegistersMaybeRead / outputRegMap.
- **Confidence:** HIGH (base-stub `0x7D6BB0` + override `0x8116B0` confirmed).

### DoVirtualCTAExpansion (bin 46)

**Dispatch:** guarded virtual on target profile (slot `+0x1C0`; if == default `0x7D6DD0` `repz ret` else jmp). **Slot:** profile-vtable `+0x1C0`.

- **Dispatch:** tail vtable dispatch on `theProfile` slot `+0x1C0` with a default-impl guard — if the slot resolves to the empty base impl `0x7D6DD0` (confirmed `repz ret`), skip; else tail-call. No run-gate (always).
- **Coverage:** base vtable `0x21CDCB8` slot `+0x1C0` = `0x7D6DD0`; both inspected non-graphics profiles also keep the stub, so virtual-CTA expansion is skipped on them. Only mesh/task (amplification) profiles install a non-stub override, gated on `IsMeshProfile() || IsTaskProfile()` (`|| IsVertexProfile` in one path), then DoVirtualCTATransformation.
- **Confidence:** HIGH.

### ForwardProgress (bin 49)

**Dispatch:** pure virtual on target profile (`mov 0x630(rsi),rdi; jmp *0xB8(*rdi)`). **Slot:** profile-vtable `+0xB8`.

- **Dispatch:** pure tail vtable dispatch on `theProfile` slot `+0xB8`; no run-gate (always runs); the virtual takes no Gb arg (`ForwardProgress(void)`).
- **Base stub:** `sub_7D6BD0` (empty `repz ret`).
- **Override (`sub_BEE590`):** reads per-function flags at `profile+0x584/+0x587`; on the `+0x584` path scans the inst list for opcode 145 (a barrier/branch-class op) whose last-operand bit 2 is clear and rewrites each via `sub_9253C0(...,1)`, then sets a forward-progress-applied marker (`ctx+0x55c=1`, clears a bit at `ctx+0x55a`). This is YIELD/WARPSYNC insertion (the GuaranteeForwardProgress feature).
- **Order:** after ExpandMbarrier, before OptimizeUniformAtomic (which can cause data divergence).
- **Confidence:** HIGH (base-stub `0x7D6BD0` + override `0xBEE590` confirmed).

### EnforceArgumentRestrictions (bin 55)

**Dispatch:** gated virtual on target profile (run-gate `sub_7DDB50>1`; `jmp *0xD0(*profile)`). **Slot:** profile-vtable `+0xD0`.

- **Gate:** `sub_7DDB50 > 1` (OPT, the OptBudget oracle), then vtable dispatch on `theProfile` slot `+0xD0` with `(rdi=profile, rsi=Gb, edx=0=FinalCall false)`.
- **Base stub:** `sub_7D6C00` (empty `repz ret`).
- **Override (`sub_13B5C80`):** keys on the FinalCall arg, re-consults the OptBudget gate internally, iterates operands and enforces operand legality — register-class / const-bank operand limits / operand-count restrictions — emitting opcode-`0xB7` fixups (`sub_930910`) and reorder/spill via `sub_9253C0`.
- **Late pair (bin 103, `LateEnforceArgumentRestrictions`):** vtable `0x22BE5E0`, execute thunk `0xC5E880` — calls the **same** slot `+0xD0` with `edx=1=FinalCall TRUE` and **no** opt-gate (always); binary-proven by disasm of `0xC5E880` (`mov 0x630(rsi),rdi; mov $1,edx; mov 0xD0(rax),rax; jmp`). So bin 55 runs FinalCall=false under the OPT gate; bin 103 runs FinalCall=true unconditionally.
- **Confidence:** HIGH (base-stub `0x7D6C00`, override `0x13B5C80`, both thunks confirmed).

### ExpandJmxComputation (bin 94)

**Dispatch:** direct tail-jmp (`mov rsi,rdi; jmp 0x7EE060`). **Worker:** `sub_7EE060`.

- **Gate:** `ctx+0x560 & 0x40` (HasJmx; `testb; jne`).
- **Block shift:** `blockSize = MAX(profile vtable +0x820 GetInstructionSize, +0x828 GetInstOpexSize)`; `shift = floor(log2(blockSize))` via a shift loop (GetPowerOf2 — a shift loop, not `bsr`).
- **Match:** walks BBs (`ctx+0x110` FirstBB / `+0x128` block array); a terminating OP_JMX (opcode `0x5E`, `node+0x48 & 0xCF00 == 0x5E00`) whose variant word `node+0x5c` has the JMX_SRC_INDEX bit (bit 0); strategy selected by `node+0x54` byte0 bit 2 (`0x4`).
- **JMX_TAB_INCBANK:** reserves a const-bank jump table via profile slot `+0x260` (ReserveConstantBankSlots), emits `OP_SHL #2` (`lea (,r,4)`, index→byte-addr), sets `node+0x58:=0` then `node+0x54 |= 0x50000000` (sym-opd const-bank encode); the packed-by-vreg variant ORs `node+0x54 |= 0x10000000` / `node+0x58 |= 0x1000000` (MakeIndexed encoding).
- **JMX_TAB_INCODE** (const bank unavailable): emits `OP_SHL` by variable `shift` (profile slot `+0x248`), sets `node+0x5c |= 4`, `node+0x6c := tab|0x60000000`, `node+0x70 := 0`; on the impl-code failure subpath also sets `ctx+0x560 |= 0x80` (HasJmxImplCode) and `node+0x5c |= 2`.
- **Finish:** both strategies clear `node+0x5c &= ~1` (JMX_SRC_INDEX → JMX_SRC_LABEL). Node fields = opd[1] (info word `node+0x54`) and opd[3] (cbank index / start-label `node+0x6c`). Pre-scheduling so 32→64b addr legalization can follow; the in-code table layout is deferred to ImplJumpTablesInCode post-fixup.
- **Confidence:** HIGH (full JMX-lowering flow recovered step-for-step).

### FinalInspectionPass (bin 110)

**Dispatch:** chained guarded virtual on the shaderType object. **Slots:** shaderType-vtable `+0x118` then `+0xC10`.

- **Dispatch:** reads the shaderType obj at `ctx+0x640`; no run-gate (always).
- **Default-impl guard:** if its vtable slot `+0x60` == base `sub_661310`, take the equal branch (the inline two-stage inspection); otherwise (derived shaderType overrides `+0x60`) a single direct dispatch on the shaderType itself.
- **Two-stage (`sub_661310`):** loads the inner pipeline obj at `shaderType+0x10`, calls its vtable slot `+0x118` (=280, pre-check), reloads `+0x10`, tails slot `+0xC10` (=3088, main inspection) — exactly `(*(*(a1+16)+280))(*(a1+16)); return (*(*(a1+16)+3088))(*(a1+16));`.
- **Installed:** base `sub_661310` at slot `+0x60` of several shaderType vtables (`0x20274A8, 0x2027EF8, 0x202FEB8, 0x21B5010…`); specialized shaderTypes override `+0x60` to bypass it.
- **Confidence:** HIGH.

### BackPropagateVEC2D (bin 116)

**Dispatch:** gated direct tail-jmp (run-gate `sub_7DDB50>1`; `mov rdi,rbx`; jmp 0x90B6A0). **Worker:** `sub_90B6A0`.

- **Gate:** `sub_7DDB50 > 1` (OPT); the worker gates again on `ctx+0x559 & 0x40` (=ctx+1369) — the VEC2D back-prop feature bit.
- **Worklist:** allocates a 24-byte control node from the per-context arena (`*(ctx+16)` → vtable slot `+0x18`, 24 bytes), stores the arena back-ptr at `node+16`, type tag `*node=2` (intrusive prev/next/parent node kind); seeds via `sub_90AB90`, drains via `sub_69DD70` (pop in reverse-CFG order via `+8/+16` parent links) until empty; frees both nodes via `sub_661750` (×2).
- **Seed (`sub_90AB90`):** re-derives CFG/region structure (`sub_781F80/sub_763070`), then for each inst with masked opcode (`node+72`, `BYTE1 & 0xCF`) `== 272` (OP_VEC2D) and clean modifier mask (`&0x603FFFF == 0`): resolves dest vreg id `*(node+84) & 0xFFFFFF`, checks the vreg descriptor flag `+48` (must be the paired/64-bit form), queries profile gate-fn 57 (VEC2D-legality knob), and on success (`sub_8FC870` lo/hi half identification) pushes a propagation node so the worklist walks predecessor edges backward.
- **Attribute:** the even-aligned register-pair (vec2) constraint — a VEC2D consumer forces its two source vregs into an aligned consecutive pair, pushed back to the defining insts.
- **Order:** post-ScheduleInstructions, before AllocateRegisters (pairing is fixed only after scheduling, so the constraint must reach defs before regalloc).
- **Confidence:** HIGH on structure/gate/worklist; MEDIUM on the exact sub-attribute flag.
