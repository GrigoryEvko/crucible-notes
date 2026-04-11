# Instruction Selection

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Instruction selection in ptxas is a two-phase process that converts PTX virtual ISA operations into concrete SASS machine opcodes. Unlike LLVM, which uses a single SelectionDAG or GlobalISel framework, ptxas distributes instruction selection across two distinct pipeline stages separated by the entire optimization pipeline: **Phase 1** converts PTX opcodes to Ori IR opcodes during initial lowering (phase 5, `ConvertUnsupportedOps`), and **Phase 2** converts Ori IR to final SASS binary forms during code generation (phases 112--122, ISel driver + Mercury encoder). The two phases serve fundamentally different purposes: Phase 1 legalizes the IR so the optimizer can reason about it, while Phase 2 selects the optimal machine encoding for the target architecture after register allocation and scheduling are complete.

| | |
|---|---|
| **Phase 1 location** | Phase 5: `ConvertUnsupportedOps` (PTX opcode to Ori opcode) |
| **Phase 2 location** | Phases 112+: ISel driver + Mercury encoder (Ori to SASS binary) |
| **MercConverter dispatch** | `sub_9ED2D0` (25 KB, master switch on `*(instr+72) & 0xCF` mask) |
| **ISel driver** | `sub_B285D0` (9 KB, 66 callees, vtable entry) |
| **ISel mega-selector** | `sub_C0EB10` (185 KB, 500+ locals, giant switch) |
| **DAG pattern matchers** | ~801 functions at `0xB28F60`--`0xB7D000` (~1.3 MB) |
| **Arch dispatch tables** | 5 thunks at `sub_B128E0`--`sub_B12920` (13 bytes each -> shared 15 KB handler) |
| **Mercury master encoder** | `sub_6D9690` (94 KB, instruction type switch) |
| **MercExpand** | `sub_C3CC60` (26 KB, pseudo-instruction expansion) |
| **SM120 pattern coordinator** | `sub_13AF3D0` (137 KB, 130-case switch, opcodes 2--352) |
| **Opcode variant selectors** | `sub_B0BE00` (19 KB, class 194), `sub_B0AA70` (5 KB, class 306) |

## Architecture

```
PTX source text
     |
     v
[Bison parser]  sub_4CE6B0 (48KB)
     |  Reduction actions build raw Ori nodes with PTX-derived opcodes
     v
+------------------------------------------------------------------+
| RAW ORI IR (PTX opcodes: add.f32, ld.global, mad.lo.s32, ...)    |
+------------------------------------------------------------------+
     |
     |  PHASE 1: PTX-to-Ori Opcode Legalization (phase 5)
     |
     |  sub_9F3340 (orchestrator, 7KB)
     |    -> sub_9F1A90 (MercConverter main, 35KB)
     |         -> sub_9ED2D0 (opcode dispatch, 25KB)
     |              Switch on (*(instr+72)) with BYTE1 & 0xCF mask
     |              ~120 case values -> ~60 handler functions
     |              + vtable dispatch for architecture-extensible ops
     |         -> sub_934630 (instruction creation, called N times)
     |    -> sub_9EF5E0 (post-conversion lowering, 27KB)
     |
     v
+------------------------------------------------------------------+
| OPTIMIZER-READY ORI IR (SASS opcodes: FADD, IMAD, LDG, STG, ...).|
| Every instruction has a valid SASS opcode for the target SM.      |
+------------------------------------------------------------------+
     |
     |  [Phases 14-111: Full optimization pipeline]
     |  Register allocation, scheduling, peephole, etc.
     |
     v
+------------------------------------------------------------------+
| OPTIMIZED ORI IR (register-allocated, scheduled)                  |
+------------------------------------------------------------------+
     |
     |  PHASE 2: Ori-to-SASS Selection & Encoding (phases 112+)
     |
     |  sub_B285D0 (ISel driver, 9KB)
     |    -> sub_C0EB10 (mega-selector, 185KB, default backend)
     |    -> sub_13AF3D0 (pattern coordinator, 137KB, SM120 backend)
     |    -> sub_B1FA20 / sub_B20E00 (builder variants)
     |    -> sub_B28F60..sub_B74C60 (~801 DAG pattern matchers)
     |    -> sub_B128E0..sub_B12920 (5 arch dispatch thunks -> shared handler)
     |
     |  sub_6D9690 (Mercury master encoder, 94KB)
     |    -> Switch on instruction type (*(instr+8))
     |    -> sub_C00BF0 (opcode lookup)
     |    -> sub_91D160 (register encoding)
     |    -> sub_7B9B80 (bitfield insert, 18,347 callers)
     |
     |  sub_C3CC60 (MercExpand, 26KB)
     |    -> sub_C37A10 (expand instruction, 16KB)
     |    -> sub_C39B40 (expand memory, 10KB)
     |    -> sub_C3BCD0 (expand control flow, 19KB)
     |
     v
+------------------------------------------------------------------+
| SASS binary (packed machine code in 64/128/256-bit words)         |
+------------------------------------------------------------------+
```

## Phase 1: PTX-to-Ori Opcode Conversion

Phase 1 runs as `ConvertUnsupportedOps` (pipeline phase 5), the most substantial bridge phase. Its job is to replace every PTX-derived opcode in the raw Ori IR with a valid SASS-level opcode for the target SM. After this phase completes, the optimizer sees only SASS-level instruction semantics.

The conversion is not a simple table lookup. Many PTX operations have no 1:1 SASS equivalent and must be expanded into multi-instruction sequences. The expansion depends on the target architecture, the operand types, and the available hardware functional units.

### MercConverter Dispatch -- `sub_9ED2D0` (25 KB)

The central dispatch function of Phase 1. Despite the sweep's initial identification as `PhaseRunner::executePhaseSequence`, the decompiled code reveals a classic opcode switch: it reads `*(instr+72)`, masks byte 1 with `0xCF` (stripping modifier bits 4--5), and dispatches to per-category handler functions. The switch covers approximately 120 distinct case values (opcode indices 1--352) routing to roughly 60 handler functions plus vtable-dispatched methods for architecture-extensible operations.

```c
// sub_9ED2D0 -- simplified dispatch logic
void MercConverter_Dispatch(context, instruction) {
    // Pre-dispatch: check predication eligibility
    bool can_predicate = sub_7E18A0(instruction, *(context+8));
    if (can_predicate)
        can_predicate = vtable[205](*(*(context+8)+1584), instruction);
    *(context+40) = can_predicate;

    // Read opcode, mask out modifier bits
    int opcode = *(DWORD*)(instruction + 72);
    BYTE1(opcode) &= 0xCF;

    // Special case: opcode 130 (HSET2 in ROT13; internal marker) with GPR operand -> clear predication
    if (opcode == 130) {
        int operand = *(DWORD*)(instruction + 84);
        if (((operand >> 28) & 7) == 1 && reg_type(operand) == 6)
            *(context+40) = 0;
    }

    // Main dispatch
    switch (opcode) {
    case 1:   sub_9DA5C0(context, instruction);  break;  // opcode class 1
    case 6:   sub_9DA100(context, instruction);  break;  // arithmetic
    case 8:   sub_9D2440(context, instruction);  break;  // specific class
    case 10: case 11: case 149: case 151: case 152: case 290: case 291:
              sub_9D80E0(context, instruction);  break;  // memory load/store
    case 16:  sub_9E8B20(context, instruction);  break;  // texture/surface
    case 61: case 63: case 80:
              sub_9E6600(context, instruction);  break;  // instruction expansion
    case 108: sub_9D76D0(context, instruction);  break;  // memory legalization
    // ... ~100 more cases ...
    default:  emit_noop(context, 0xFFFF);        break;  // unknown -> passthrough
    }

    // Post-dispatch: apply predication and operand adjustments
    vtable[107](context, instruction);
}
```

### MercConverter Opcode Dispatch Table

The complete switch covers opcodes 1--352. Cases route to three dispatch mechanisms: direct function calls (for common PTX categories), vtable-indirect calls (for architecture-extensible operations), and the `emit_noop` fallback for unrecognized opcodes. Below is the reconstructed routing table from the decompiled `sub_9ED2D0`.

**Direct handler dispatch (35 handlers):**

| Opcode(s) | Handler | Size | Category |
|---|---|---|---|
| 1 | `sub_9DA5C0` | 2 KB | Opcode class 1 (basic ALU) |
| 6 | `sub_9DA100` | 9 KB | Arithmetic operations |
| 8 | `sub_9D2440` | -- | Specific class |
| 10, 11, 149, 151, 152, 290, 291 | `sub_9D80E0` | 17 KB | Memory load/store |
| 15, 85 | `sub_9EC340` | 23 KB | Multi-operand legalization |
| 16 | `sub_9E8B20` | 17 KB | Texture/surface lowering |
| 17 | `sub_9E7FB0` | -- | Surface operations |
| 22 | `sub_9D6DB0` | -- | Specific lowering |
| 23 | `sub_9E58F0` | -- | Specific lowering |
| 24 | `sub_9D9F60` | -- | Specific lowering |
| 26 | `sub_9E54C0` | -- | Specific lowering |
| 27 | `sub_9E4BB0` | -- | Specific lowering |
| 28 | `sub_9D9E70` | -- | Specific lowering |
| 32, 271 | `sub_9E2440` | -- | Bitfield operations |
| 34 | `sub_9E55E0` | -- | Specific lowering |
| 38, 59, 106, 180, 182, 192, 194, 215, 221, 242 | `sub_9DA6B0` | -- | Generic ALU group |
| 41, 284 | `sub_9D1DA0` | -- | Specific lowering |
| 42, 53, 55, 66 | `sub_9D54B0` | -- | Grouped operations |
| 47 | `sub_9E74E0` | -- | Conditional (arch flag check) |
| 51 | `sub_9E2F60` | -- | Specific lowering |
| 52, 54, 72, 97 | `sub_9D09C0` | -- | Group with `v8=1` (deletion flag) |
| 57, 101 | `sub_9D6170` | -- | Paired operations |
| 60, 62, 78, 79 | `sub_9E5EE0` | -- | Comparison group |
| 61, 63, 80 | `sub_9E6600` | 25 KB | Instruction expansion (64-bit split) |
| 67 | `sub_9D9C30` | -- | Specific lowering |
| 70 | `sub_9E3490` | -- | Specific lowering |
| 75 | `sub_9E0C10` | -- | Specific lowering |
| 77 | `sub_9E4DF0` | -- | Specific lowering |
| 83 | `sub_9D6AB0` | -- | Specific lowering |
| 88, 89 | `sub_9D5990` | -- | Paired operations |
| 90 | `sub_9D2820` | -- | Specific lowering |
| 91 | `sub_9E7600` | -- | Specific lowering |
| 92 | `sub_9E7890` | -- | Specific lowering |
| 93, 95 | `sub_9E1D40` | -- | Comparison variants |
| 94 | `sub_9E1DF0` | -- | Specific lowering |
| 96 | `sub_9D41C0` | -- | Specific lowering |
| 98 | `sub_9D3230` | -- | Specific lowering |
| 100 | `sub_9D70E0` | -- | Specific lowering |
| 102 | `sub_9D9750` | -- | Specific lowering |
| 103, 104 | `sub_9E31D0` | -- | Paired operations |
| 108 | `sub_9D76D0` | 18 KB | Memory instruction legalization |
| 124 | `sub_9E18B0` | -- | Specific lowering |
| 135 | `sub_9D6560` | -- | Specific lowering |
| 139, 140, 141, 143 | `sub_9D4C10` | -- | Related operations group |
| 145 | `sub_9D3020` | -- | Specific lowering |
| 155, 268 | `sub_9E5260` | -- | Paired operations |
| 156 | `sub_9D94B0` | -- | Specific lowering |
| 158, 167 | `sub_9E4A00` | -- | Paired operations |
| 161 | `sub_9D21D0` | -- | Specific lowering |
| 162 | `sub_9D9660` | -- | Specific lowering |
| 166 | `sub_9E2100` | -- | Specific lowering |
| 170 | `sub_9E2DF0` | -- | Specific lowering |
| 173, 267 | `sub_9EB5C0` | -- | Paired operations |
| 174 | `sub_9D9300` | -- | Specific lowering |
| 184 | `sub_9D2E70` | -- | Specific lowering |
| 185 | `sub_9E32F0` | -- | Specific lowering |
| 188, 190 | `sub_9E2970` | -- | Paired operations |
| 195 | `sub_9D2AB0` | -- | Specific lowering |
| 196 | `sub_9D9080` | -- | Specific lowering |
| 198 | `sub_9D66F0` | -- | Specific lowering |
| 201, 202, 204, 285 | `sub_9EAC30` | -- | Async/bulk group |
| 203 | `sub_9D8E90` | -- | Specific lowering |
| 205 | `sub_9E1260` | -- | Specific lowering |
| 209 | `sub_9E5740` | -- | Specific lowering |
| 210, 213, 214 | `sub_9D8B30` | -- | Grouped operations |
| 240 | `sub_9D6280` | -- | Specific lowering |
| 241 | `sub_9E2CC0` | -- | Specific lowering |
| 247 | `sub_9D0F70` | -- | Specific lowering |
| 248 | `sub_9D0DF0` | -- | Specific lowering |
| 262 | `sub_9E7440` | -- | Specific lowering |
| 264 | `sub_9D73F0` | -- | Specific lowering |
| 276 | `sub_9D5EC0` | -- | Specific lowering |
| 292 | `sub_9D0E90` | -- | Specific lowering |

**Vtable-indirect dispatch (for architecture-extensible operations):**

| Opcode(s) | Vtable offset | Category (inferred) |
|---|---|---|
| 2, 3, 4, 5, 7 | `vtable[0]` (+0) | Generic fallback |
| 14, 39, 40, 105, 125, 299, 300, 321 | `vtable[7]` (+56) | Group A operations |
| 18 | `vtable[3]` (+24) | Specific class |
| 31 | `vtable[4]` (+32) | Specific class |
| 35 | `vtable[6]` (+48) | Specific class |
| 36 | `vtable[21]` (+168) | Specific class |
| 43 | `vtable[9]` (+72) | Specific class |
| 50 | `vtable[12]` (+96) | Specific class |
| 65 | `vtable[22]` (+176) | Specific class |
| 73 | `vtable[15]` (+120) | Specific class |
| 74 | `vtable[16]` (+128) | Specific class |
| 81 | `vtable[24]` (+192) | Specific class |
| 110, 111, 112, 114 | `vtable[25]` (+200) | Warp shuffle group |
| 118 | `vtable[10]` (+80) | Specific class |
| 119 | `vtable[28]` (+224) | Specific class |
| 120, 121, 126, 127, 128, 280, 281 | `vtable[27]` (+216) | Barrier/sync group |
| 122, 123, 310, 311, 312 | `vtable[26]` (+208) | Related group |
| 130 (`HSET2`), 169 | `vtable[29]` (+232) | Move/convert group (130 is MOV-like internally; actual SASS `MOV` = 19) |
| 157 | `vtable[84]` (+672) | Specific class |
| 176, 177 | `vtable[34]` (+272) | Paired operations |
| 183, 288 | `vtable[36]` (+288) | Paired operations |
| 186 | `vtable[35]` (+280) | Specific class |
| 211 | `vtable[39]` (+312) | Specific class |
| 220 | `vtable[40]` (+320) | Specific class |
| 223, 238 | `vtable[41]` (+328) | Paired operations |
| 228 | `vtable[42]` (+336) | Specific class |
| 243 | `vtable[43]` (+344) | Specific class |
| 245--253, 257 | `vtable[67--77]` (+536--+624) | SM 100+ operations |
| 265, 266 | `vtable[93]` (+744) | Paired operations |
| 270 | `vtable[77]` (+616) | Specific class |
| 277 | `vtable[65]` or `vtable[11]` (+520/+88) | Operand-type dependent |
| 279--351 | various high vtable offsets | SM 100+ / Blackwell operations |

The vtable mechanism allows architecture backends to override conversion behavior without modifying the core dispatch. The vtable factory at `sub_1CCEEE0` (17 KB, 244 callees) selects which overrides are active based on the SM version.

### Per-Category Handlers

The larger handlers implement non-trivial conversion logic:

| Handler | Size | Category | Key behavior |
|---|---|---|---|
| `sub_9E6600` | 25 KB | Instruction expansion | Splits 64-bit ops on 32-bit ALU into hi/lo pairs with carry chains. Calls `sub_9D4380` (instruction builder) ~10 times per expansion. |
| `sub_9EC340` | 23 KB | Multi-operand legalization | Operand type test: `(v >> 28) & 7 == 1` means register. Register class query via `sub_7BE7B0`. Creates new instructions via `sub_7DEAD0`. |
| `sub_9D76D0` | 18 KB | Memory legalization (load/store) | Register type dispatch: 6=GPR, 7=predicate, 3=address. Uses `sub_9D4380` (instruction builder) and `sub_9CD420` (predication). |
| `sub_9D80E0` | 17 KB | Memory legalization (variant) | Same opcode set as `sub_9D76D0`, alternate code path for different operand patterns. |
| `sub_9E8B20` | 17 KB | Texture/surface lowering | Register type 6 = GPR. Manipulates bitmask at register descriptor offset `+48`. |
| `sub_9DA100` | 9 KB | Arithmetic operations | Handles opcode case 6 -- standard ALU instruction legalization. |
| `sub_9DA6B0` | -- | Generic ALU group | Covers 10 opcode values (38, 59, 106, 180, 182, 192, 194, 215, 221, 242). |

### 1:1 vs 1:N Expansion

Most PTX operations map 1:1 to a single SASS opcode. When they do not, the handlers in `sub_9E6600` and related functions create multi-instruction sequences:

```
PTX                                    Ori IR (after Phase 1)
-----------------------------------    -----------------------------------
add.f32  %r1, %r2, %r3          -->   FADD  R1, R2, R3                [1:1]
add.s32  %r4, %r5, %r6          -->   IADD3 R4, R5, R6, RZ           [1:1, operand added]
mul.lo.s64 %rd1, %rd2, %rd3     -->   IMAD.LO  R1, R2, R6, RZ       [1:N split]
                                       IMAD.HI  R0, R2, R6, RZ
                                       IMAD      R0, R3, R6, R0
                                       IMAD      R0, R2, R7, R0
div.f32  %r7, %r8, %r9          -->   MUFU.RCP  R10, R9              [1:N, Newton-Raphson]
                                       FMUL      R7, R8, R10
                                       (+ correction iterations)
bar.sync 0                       -->   BAR                            [1:1]
```

The expansion creates new instruction nodes via `sub_934630` and links them into the doubly-linked instruction list. The original PTX-level instruction is replaced by the expanded sequence.

### Type-Dependent Opcode Selection

PTX's explicitly-typed opcodes (where the type is a qualifier like `.f32`, `.s64`) map to different SASS mnemonics based on the type:

| PTX type | SASS prefix | Example PTX | Example SASS |
|---|---|---|---|
| `.f16` / `.f16x2` | `H` | `add.f16` | `HADD2` |
| `.f32` | `F` | `add.f32` | `FADD` |
| `.f64` | `D` | `add.f64` | `DADD` |
| `.s32` / `.u32` | `I` | `add.s32` | `IADD3` |
| `.s64` / `.u64` | `I` (split) | `add.s64` | `IADD3` + `IADD3.X` (carry chain) |
| `.pred` | `P` | `setp.eq.f32` | `FSETP` |

The type qualifier disappears from the instruction syntax during conversion. It becomes encoded in the SASS mnemonic itself (the `F` in `FADD`, the `I` in `IADD3`) and in the register class of the operands.

### SM-Dependent Legalization

The MercConverter gates operations by SM version through the architecture vtable. An instruction available natively on one SM may require a multi-instruction lowering sequence on another:

- **64-bit integer arithmetic** on SM 50--75 (no native 64-bit ALU): splits into 32-bit hi/lo pairs
- **FP16 operations** on pre-SM 53 targets: promoted to FP32 (handled by Phase 2 `PromoteFP16`)
- **`bfe`/`bfi` variants**: some bit-field extract/insert modes not supported on all targets
- **Tensor core intrinsics**: SM 70 has HMMA v1, SM 75 has HMMA v2, SM 80+ has HMMA v3/DMMA, SM 100 has TCGen05

The architecture vtable factory at `sub_1CCEEE0` populates the vtable with SM-specific method overrides. The vtable has approximately 90 method slots (up to offset +720), with the highest-numbered slots (offset 624+) serving SM 100+ Blackwell operations.

## Phase 2: Ori-to-SASS Selection & Encoding

Phase 2 runs during code generation (phases 112+) after the optimizer, register allocator, and scheduler have completed. It operates on fully optimized, register-allocated Ori IR and produces final SASS machine code. Phase 2 has three major components: the ISel driver with DAG pattern matching, the Mercury master encoder, and MercExpand pseudo-instruction expansion.

### ISel Driver -- `sub_B285D0` (9 KB)

The top-level ISel coordinator is a vtable entry point with 66 callees. It selects the appropriate instruction builder variant based on the target architecture:

```c
// Simplified ISel driver
void ISel_LowerInstruction(context, instruction) {
    int sm = *(context + 184);          // SM version
    int opcode = instruction[18] & 0xFFFFCFFF;

    // Select architecture-variant builder
    if (sm == 14)
        Builder_VariantA(context, instruction);    // sub_B1FA20 (13 KB)
    else
        Builder_VariantB(context, instruction);    // sub_B20E00 (11 KB)

    // Apply post-ISel modifiers
    ApplyModifiers(context, instruction);           // sub_B1D670 (13 KB)
    SetProperties(context, instruction);            // sub_B241A0 (7 KB)
}
```

The two builder variants (`sub_B1FA20` and `sub_B20E00`) are structurally near-identical, with 50 callees each. Both call `sub_7E3EF0` (operand index helper) 6 times (3 source + 3 destination operands) and use `sub_A3B930` (operand register class resolver). The key difference is the validation function: variant A uses `sub_C49440`, variant B uses `sub_C49400`, reflecting different encoding constraints for different SM families.

### ISel Mega-Selector -- `sub_C0EB10` (185 KB)

The single largest function in the Phase 2 ISel range: 185 KB decompiled, 6,016 lines, 719+ local variables. It performs the final Ori-IR-to-SASS opcode and operand encoding for 169 distinct instruction types (SASS opcode indices 7--221). While the ~801 DAG pattern matchers handle template-based ISel through a priority contest, the mega-selector handles complex instructions that require procedural, multi-step encoding logic -- instructions where the operand marshalling depends on runtime state (calling conventions, symbol resolution, address space aliasing).

#### Dual-Switch SM-Generation Dispatch

The function contains **two copies** of the same 169-case switch statement, separated by a vtable-based opcode translation mechanism. This dual-switch structure is the SM-generation dispatch:

```c
// sub_C0EB10 -- simplified dispatch skeleton
void MegaSelector(context *a1, instruction *a2, isel_ctx *a3) {
    int64_t *vtable = *(a3->backend);
    int opcode = *(int *)(a2 + 8);           // SASS opcode type

    // Pre-dispatch: capability check via vtable[12]
    auto cap_check = vtable[12];              // offset +96
    if (cap_check != sub_BFEAA0)              // default stub?
        if (cap_check(a3, a2))
            ctx->flags[256] = 1;              // set encoding flag

    // Read opcode translator from vtable[2]
    auto translator = vtable[2];              // offset +16

    if (translator != sub_BFEBF0) {
        // PATH A: SM-specific translation
        int encoding_index = translator(a3, opcode);
        int isel_opcode = *(ctx + 8);         // post-translation opcode
        switch (isel_opcode) {                // PRIMARY SWITCH (169 cases)
            case 7: case 34: case 35: case 36:
                emit_simple(encoding_index, ...);
                break;
            case 8: case 38: case 46: ...
                /* already encoded */ break;
            // ... 169 cases total ...
            default: goto high_opcode_path;
        }
    } else {
        // PATH B: static table lookup (default backend)
        int encoding_index = 355;             // sentinel for extended opcodes
        if (opcode <= 0xDD)
            encoding_index = word_22B4B60[opcode];
        switch (opcode) {                     // FALLBACK SWITCH (same 169 cases)
            case 7: ...: goto handler_7;      // jumps into Path A handlers
            // ... identical case set ...
            default: return;
        }
    }

high_opcode_path:
    if (opcode > 0x199) return;
    // Try vtable[3] extension dispatch for SM 100+ / Blackwell
    auto extension = vtable[3];               // offset +24
    if (extension != sub_BFEA30)
        extension(a3, a2);                    // arch-extension handler
}
```

The dual-switch pattern is a code-generation artifact: the compiler emitted two copies because the vtable path and static-table path produce different values for the encoding index but need identical case routing. This doubles the binary size but avoids a conditional merge point at every case entry.

#### Three Vtable Dispatch Points

| Vtable slot | Offset | Default stub | Purpose |
|---|---|---|---|
| `vtable[2]` | +16 | `sub_BFEBF0` | Opcode-to-encoding-index translator. SM-specific override remaps opcodes to different encoding slots. Fallback: `word_22B4B60[]` static table. |
| `vtable[12]` | +96 | `sub_BFEAA0` | Pre-dispatch capability check. Returns boolean that sets `ctx[256]` encoding flag. |
| `vtable[3]` | +24 | `sub_BFEA30` | Extension opcode handler for opcodes outside the 169-case set (barrier/sync 61--63/221, opcodes > 0x199, SM 100+ extensions). |

The `word_22B4B60` static table is a `uint16[222]` array indexed by SASS opcode (0--0xDD = 221). Each entry is a SASS encoding slot index used to select a format descriptor from the encoder tables. Of the 222 entries: 117 carry a non-zero encoding slot, 95 are zero (opcode uses no static encoding or is handled purely by case logic), and 10 carry the sentinel value **355** (opcode requires SM-specific vtable override and has no default encoding). SM-specific vtable overrides can remap any entry, enabling per-architecture instruction variants without modifying the mega-selector logic.

**Complete `word_22B4B60` dump** (117 non-zero non-sentinel entries, sorted by opcode):

| Opc | Mnemonic | Slot | | Opc | Mnemonic | Slot | | Opc | Mnemonic | Slot |
|----:|----------|-----:|-|----:|----------|-----:|-|----:|----------|-----:|
|   5 | SGXT     |  160 | |  68 | BRX      |  283 | | 141 | UISETP   |  120 |
|   6 | LOP3     |  241 | |  69 | JMP      |  164 | | 142 | ULDC     |  126 |
|   7 | ISETP    |  148 | |  70 | JMX      |  168 | | 143 | ULEA     |  129 |
|   8 | IABS     |   97 | |  71 | CALL     |  130 | | 144 | UP2UR    |  139 |
|  10 | SHF      |   93 | |  74 | BREAK    |   12 | | 145 | ULOP3    |  143 |
|  11 | FFMA     |   94 | |  75 | BPT      |   13 | | 146 | UPLOP3   |  151 |
|  13 | FMUL     |   95 | |  77 | EXIT     |  172 | | 147 | USEL     |  163 |
|  20 | SEL      |   29 | |  78 | RTT      |   30 | | 149 | UFLO     |  200 |
|  22 | R2P      |   37 | |  84 | TLD      |   86 | | 150 | UIADD3   |  201 |
|  24 | PRMT     |  188 | |  85 | TLD4     |   88 | | 151 | UIMAD    |  206 |
|  25 | NOP      |  190 | |  86 | TMML     |   89 | | 152 | UMOV     |  207 |
|  29 | PMTRIG   |   32 | |  88 | TXQ      |  187 | | 153 | UPRMT    |  208 |
|  30 | CSMTEST  |  271 | |  99 | STL      |    1 | | 154 | VOTEU    |  213 |
|  31 | VABSDIFF |  159 | | 100 | LD       |   25 | | 156 | USHF     |  214 |
|  32 | VABSDIFF4|   72 | | 101 | ST       |   33 | | 158 | F2FP     |  216 |
|  34 | IDE      |   55 | | 102 | ATOM     |   38 | | 159 | HMMA_1688|  217 |
|  35 | I2I      |   42 | | 104 | RED      |   44 | | 160 | HMMA_16816| 219 |
|  36 | I2IP     |   53 | | 105 | ATOMS    |   45 | | 161 | BMMA     |  227 |
|  47 | I2F      |  224 | | 106 | QSPC     |   59 | | 162 | TTUCCTL  |  229 |
|  48 | I2F_X    |  235 | | 108 | CCTL     |   60 | | 163 | TTUMACRO |  290 |
|  50 | FRND_X   |  150 | | 109 | CCTLL    |   62 | | 164 | R2UR     |    7 |
|  51 | AL2P     |   87 | | 110 | CCTLT    |   68 | | 168 | FOOTPRINT|   36 |
|  55 | BMOV_R   |   16 | | 111 | MEMBAR   |   71 | | 172 | SM82_FIRST| 110 |
|  57 | S2R      |   98 | | 112 | SULD     |   78 | | 173 | GATHER   |  115 |
|  58 | B2R      |  153 | | 113 | SUST     |   79 | | 174 | GENMETADATA| 114|
|  59 | R2B      |  183 | | 114 | SUATOM   |  106 | | 175 | SPMETADATA| 117 |
|  60 | LEPC     |  288 | | 118 | ISBEWR   |  147 | | 176 | BMMA_88128| 196 |
|  61 | BAR      |   18 | | 119 | SHFL     |  149 | | 177 | BMMA_168128| 254|
|  62 | BAR_IDX  |  302 | | 122 | DFMA     |  179 | | 178 | BMMA_168256| 255|
|  63 | SETCTAID |  303 | | 123 | DADD     |  180 | | 179 | CLMAD    |  256 |
|  65 | GETLMEMBASE| 248|| 124 | DMUL     |  192 | | 180 | DMMA     |  257 |
|     |          |      | | 125 | DSETP    |  191 | | 181 | HMMA_SP_1688| 258|
|     |          |      | | 126 | HADD2    |  199 | | 182 | HFMA2_MMA| 259 |
|     |          |      | | 127 | HADD2_F32|  215 | | 183 | HMNMX2   |  260 |
|     |          |      | | 129 | HMUL2    |  221 | | 184 | IMMA_88  |  261 |
|     |          |      | | 130 | HSET2    |  225 | | 187 | IMMA_16832| 262 |
|     |          |      | | 131 | HSETP2   |    2 | | 188 | IMMA_SP_16832|243|
|     |          |      | | 132 | HMMA_16  |   10 | | 202 | QMMA_16832|  96|
|     |          |      | | 133 | HMMA_32  |   48 | | 204 | QMMA_SP_12864|169|
|     |          |      | |     |          |      | | 205 | SM89_LAST|  178 |
|     |          |      | |     |          |      | | 206 | SM90_FIRST| 197 |
|     |          |      | |     |          |      | | 207 | ACQBLK   |  239 |
|     |          |      | |     |          |      | | 208 | CGABAR_ARV| 154 |
|     |          |      | |     |          |      | | 210 | CGABAR_SET| 195 |
|     |          |      | |     |          |      | | 211 | CGABAR_WAIT|175 |
|     |          |      | |     |          |      | | 217 | ENDCOLLECTIVE|309|
|     |          |      | |     |          |      | | 218 | FENCE_G  |  170 |

**Sentinel entries** (slot = 355, no default encoding -- require SM-override):

| Opc | Mnemonic | Notes |
|----:|----------|-------|
|  40 | FCHK | FP range check -- encoding varies by arch |
|  49 | FRND | FP round -- superseded by FRND_X (opc 50) in default table |
| 169 | S2UR | Special-reg to uniform-reg -- uniform pipe is SM-specific |
| 212 | CGAERRBAR | CGA error barrier -- SM 90+ only |
| 213 | CREATEPOLICY | Cache policy creation -- SM 90+ only |
| 214 | CVTA | Generic address conversion -- encoding varies by arch |
| 215 | DMMA | Double MMA (duplicate; opc 180 has default slot 257) |
| 216 | ELECT | Warp elect -- SM 90+ only |
| 219 | FENCE_S | Shared fence -- SM 90+ only |
| 220 | FMNMX | FP min/max (duplicate; separate from FMNMX at opc 14) |

#### Opcode Case Routing

The 169 distinct opcode cases (338 total case labels across both switches) group into approximately 70 handler blocks. The groupings reveal SASS ISA families:

| Group | Opcodes | Handler pattern | Instruction family |
|---|---|---|---|
| No-op passthrough | 8, 38, 46, 87, 89, 90, 93, 97, 98, 208 | `goto LABEL_33` (already encoded) | Pre-encoded by upstream ISel |
| Simple emission | 7, 34, 35, 36 | `sub_9314F0(encoding_index, 1 operand)` | Basic ALU / simple 1-op |
| Branch/call | 9, 10, 11, 12, 13, 22 | `sub_926370` / vtable[17] / linked-list walk | Control flow, call frames |
| Memory load/store | 15, 16, 18, 19, 20, 23, 24, 25, 26, 30 | `sub_C01840` + address helpers | LDG, STG, LDS, etc. |
| Control flow | 31, 32, 33 | SSA phi nodes, branch tables | Phi, switch, call return |
| Generic ALU | 39, 41, 42, 50, 51, 52, 53 | `sub_9314F0` passthrough | Standard arithmetic |
| Special register | 43, 44, 45 | `sub_C06E90` symbol lookup | SR access, shared memory alias |
| Constant/predicate | 47, 54, 55, 56 | Direct operand copy / `sub_BFFD60` | Constant bank, predicate ops |
| Address compute | 57 | 200-line handler, `"__nv_reservedSMEM_offset_0_alias"` | Complex addressing with SMEM |
| Immediate ops | 59, 60 | `sub_C05CC0` / `sub_C07690` | Immediate-operand variants |
| Barrier/sync | 61, 62, 63, 221 | Forward to vtable[3] extension | BAR, MEMBAR, SYNC |
| Conversion/move | 65 | Operand loop with per-element `sub_9314F0` | MOV, CVT |
| Texture/surface | 67, 68, 69, 70 | Multi-operand type-qualified encoding | TEX, TLD, TXQ |
| Intrinsics | 71, 74, 75 | Loop-based operand emission | Hardware intrinsics |
| Tensor core | 84, 88, 91, 92 | Wide-operand encoding (case 92 = 354 lines) | HMMA, DMMA, IMMA, TCGen05 |
| Predication ext | 94, 95 | Predicate-dependent path selection | Extended predication |
| Memory extended | 99--130 (19 opcodes) | `sub_C0B2C0` or `sub_BFFD60` + encoding lookup | Extended memory ops |
| Warp intrinsics | 131--189 (50+ opcodes) | Mixed handlers, vtable[198]+632 dispatch | SHFL, VOTE, MATCH, REDUX |
| Async/bulk | 192--218 (15 opcodes) | `sub_C0B2C0` / individual handlers | TMA, async copy, bulk ops |

The largest case handlers:
- Cases 141/142: ~503 lines (warp shuffle/vote extended operations)
- Case 92: ~354 lines (tensor core instructions -- widest operand format)
- Cases 45, 57, 95: ~200 lines each (shared memory, address compute, predication)

#### Representative Per-Case Handler Pseudocode

Six handlers spanning the five marshalling patterns. All write tagged operands into `buf[32]` (256 bytes) and converge at `LABEL_33` (clears `ctx[256..257]` encoding flags).

**1. Simple emission -- cases 7/34/35/36 (ISETP, IDE, I2I, I2IP)**

```c
instr->output = ctx->reg_base;                        // instr[16] = ctx[35]
BuildOperandRecord(buf, ctx, encoding_index, 1, 0, NULL);  // sub_9314F0
```

**2. Control flow -- cases 15/16 (BRA/RET with call frame)**

```c
int frame_id = ++isel_ctx->call_frame_counter;
GrowCallFrameIfNeeded(isel_ctx, frame_id);               // sub_C00C30
MarshalSrcOperands(isel_ctx, ctx, instr->src_list, buf, 32);
ResolveRegisterIndex(&tmp, ctx, instr->data_type);        // sub_91C150
EmitBranchRecord(&tmp, ctx, 0x82, data_type, tmp, buf);   // sub_92E800
record = isel_ctx->frame_base + (frame_id << 6);          // 64-byte frame record
record->encoding = tmp;  record->opcode = instr->opcode;
if (opcode == 16/*RET*/ && IsUniformPipe(ctx->backend))
    MarkReturnRecord(ctx, record, 4);
// Trailing BRA stub to callee address via encoding_index 0x5F
EmitBranchRecord(&tmp, ctx, 0xC7, data_type, 0xFFFFFF, buf);
buf[0] = callee_addr & 0xFFFFFF | TAG_CONST_BANK;  buf[1] = tmp;
buf[2] = 0x60000003;  // modifier: count=3
BuildOperandRecord(&result, ctx, 0x5F, 1, 3, buf);
```

**3. Multi-operand ALU -- cases 102/106/114/122--124/127 (ATOM, DFMA, DADD, DMUL, ...)**

```c
int n_dst = MarshalDstOperands(isel_ctx, ctx, instr, buf);     // sub_BFFD60
int n_src = MarshalSrcOperands(..., &buf[n_dst], 32 - n_dst);
int enc = (vtable[2] != default) ? vtable[2](isel_ctx, opcode)
                                 : word_22B4B60[opcode];
if (data_type == 19 && vtable[14](isel_ctx, instr))           // shared-mem override
    enc = (enc == 180) ? 181 : (enc == 192) ? 193 : enc;
Emit2OpRecord(&result, ctx, enc, data_type, buf, &buf[n_dst]); // sub_92E720
instr->encoding = RegisterResult(isel_ctx->output, &result);
for (int j = 1; j < n_dst; j++)
    ChainExtraDest(isel_ctx->output, &result);                 // sub_A64220
```

**4. Tensor core -- case 92 (HMMA/IMMA/DMMA wide-operand)**

```c
int n_dst = MarshalDstOperands(isel_ctx, ctx, instr, buf);
MarshalSrcOperands(..., &buf[n_dst], 32 - n_dst);
int sched_class = GetSchedClass(ctx->backend, data_type);     // vtable[198]+904
if (sched_class == 2) {                                       // HMMA path
    int frag = 0;  instr->encoding = 0;
    for (int k = 0; k < 4; k++) {
        if (!instr->fragment_mask[k]) continue;                // bytes +48..+51
        BuildFragOperand(&imm, 8, 8*k);                       // sub_91E8E0
        int reg = EncodeRegIndex(ctx, imm);                    // sub_91D160
        Emit4OpRecord(&rec, ctx, 0x14, 0xC, &buf[frag++], &buf[n_dst],
                      &reg, &TAG_ALU);                         // sub_92FF10
        int r = RegisterResult(isel_ctx->output, &rec);
        if (!instr->encoding) instr->encoding = r;
    }
} else if (sched_class == 4) { /* DMMA: flag-OR per fragment via sub_92E720 */ }
```

**5. Surface/texture -- case 150 (3-phase operand collection + per-dest loop)**

```c
bool is_float = IsFloatType(instr->data_type);                // sub_7D6860
int n_dst   = MarshalDstSpecial(isel_ctx, ctx, instr, buf, 32, is_float);
int n_src   = MarshalSrcOperands(..., &buf[n_dst], 32 - n_dst);
int n_coord = MarshalSrcOperands(..., &buf[n_dst+n_src], ...);
if ((instr->subop - 28) & ~4) {                               // non-texture path
    int modifier = (instr->modifier & 0x3F) | TAG_CONTROL;
    for (int i = 0; i < n_dst; i++) {                          // one SASS per dest
        Emit4OpRecord(&rec, ctx, enc_idx, data_type,
                      &buf[i], &buf[n_dst+...], &buf[n_dst+n_src+...], &modifier);
        int r = RegisterResult(isel_ctx->output, &rec);
        if (i == 0) instr->encoding = r;
    }
} else { /* texture-qualified: vtable[29] dispatch */ }
```

**6. MMA/warp extended -- cases 141/142 (shape-descriptor construction)**

```c
int shape = instr->modifier & 7;
if (!shape) goto generic_path;                                // LABEL_417
int enc_idx = (opcode == 141) ? 121 : 127;                   // UISETP / ULDC
int desc_mod = (vtable[21] != default) ? vtable[21](isel_ctx, instr) << 16 : 0;
int descriptor = desc_mod + instr->mma_m + (instr->mma_n << 8);
buf[0] = 0xF0000000;  buf[1] = descriptor;                   // TAG_SPECIAL
AllocUniformReg(&tmp, ctx, 6);                                // sub_91BF30
buf[2] = (tmp & 0xFFFFFF) | 0x90000000;  buf[3] = descriptor;
goto marshal_and_emit;                                        // -> LABEL_418
```


#### Operand Encoding Protocol

The mega-selector encodes operands into a stack-allocated 256-byte output buffer using a tagged-pointer word format. Each operand occupies 8 bytes (a DWORD pair):

| Bits | Field | Description |
|---|---|---|
| `[31:28]` of word 0 | Type tag | `0x1`=register, `0x4`=constant bank, `0x5`=immediate, `0x6`=control/modifier, `0x9`=special register |
| `[23:0]` of word 0 | Value | Register index, immediate value, or bank offset |
| word 1 | Flags | Modifier bits, encoding-format flags |

The marshalling pipeline for a typical case:

```
1. sub_C01840(ctx, instr, operand_list, output_buf, max_count, ...)
   -> Iterates source operands, writes tagged words to output_buf
   -> Returns: number of operand words written

2. sub_C01F50(ctx, instr, dest_list, output_buf, max_count, ...)
   -> Same for destination operands

3. Encoding-index lookup:
   if (vtable[2] != default)
     index = vtable[2](ctx, opcode);
   else
     index = word_22B4B60[opcode];

4. sub_9314F0(output, ctx, encoding_index, count, n_words, buf, ...)
   -> Emits the instruction record to the output stream
```

| Helper | Calls | Purpose |
|---|---|---|
| `sub_C01840` | 52 | Marshal source operands into tagged-word buffer |
| `sub_9314F0` | 31 | Emit instruction with encoding index + operand buffer |
| `sub_C00EA0` | 8 | Extract single operand as tagged word |
| `sub_91D160` | 8 | Encode register index to encoding bits |
| `sub_934630` | 6 | Build new instruction node in IR (for multi-instruction expansion) |
| `sub_91D150` | 5 | Decode register index from operand word |
| `sub_926370` | 4 | Emit simple instruction (branch/jump) |
| `sub_C01F50` | 3 | Marshal destination operands |
| `sub_7D6860` | 3 | Encode data type qualifier (FP32/FP64/INT) |
| `sub_BFEF10` | 3 | Register bank capacity check / grow |
| `sub_92E1B0` | 2 | Emit instruction with constant-bank operand |

#### Cross-Reference: Arch Dispatch Tables

The 5 arch dispatch thunks (`sub_B128E0`--`sub_B12920`, each 13 bytes setting `esi` to an SM-family code: `0x5004`, `0x5003`, `0x5001`, `0x5000`, `0x4000`) are **not** called from the mega-selector. They tail-jump to a shared handler at `loc_1C38C00` and operate at the Mercury encoder level:

```
Mega-selector (sub_C0EB10)
  -> Produces (encoding_index, operand_buffer) pairs
  -> Calls sub_9314F0 to package into instruction nodes

Mercury encoder (sub_6D9690)
  -> Reads instruction type field from instruction node
  -> Arch dispatch tables (sub_B128E0 etc.) resolve type to encoding format
  -> Encoder emits binary SASS using format + operand data
```

The mega-selector and arch dispatch tables thus operate at different abstraction levels: the mega-selector decides **what** to encode (opcode selection, operand marshalling), while the arch tables decide **how** to encode it (encoding format, bit layout). The arch tables' per-SM variants handle encoding-level differences (field widths, modifier positions) that are invisible to the mega-selector's opcode-level logic.

### Post-ISel Modifiers -- `sub_B1D670` (13 KB)

After the main ISel selection, this pass applies architecture-specific instruction modifications:

- **Opcode 13**: sets instruction field `[79] = 3`
- **Opcode 14**: sets instruction field `[79] = 2`
- **Opcode 11**: separate modifier path

The function has 51 callees including `sub_AAD690` (field accessor, called multiple times), `sub_AADF40`, and `sub_C49400` (encoding validator). It handles encoding mode bits, register class adjustments, and predicate attachment.

### Instruction Properties -- `sub_B241A0` (7 KB)

Sets scheduling-relevant properties on the selected instruction:

- `inst[74] = 7` -- scheduling class
- `inst[75] = (opcode == 325)` -- special flag for specific opcode
- `inst[77] = sub_A3B930(...)` -- operand class from register resolver
- `inst[79]` -- derived from `a2[19]`, architecture-dependent

Contains a switch on `*(context+46)` (target architecture selector), confirming per-SM property assignment.

### DAG Pattern Matchers -- ~800 Functions at `0xB28F60`--`0xB7D000`

Every pattern matcher follows an identical prototype and a strict check-and-report protocol. These are the ptxas equivalent of LLVM's TableGen-generated ISel patterns, but handwritten in C++. Binary analysis confirms 801 functions with the matching `*a4 <=` priority-comparison idiom, with the bulk (750+) residing in the `0xB30000`--`0xB7D000` range and a handful of smaller matchers in the `0xB28F60`--`0xB30000` preamble zone.

#### Pattern Matcher Architecture

The pattern matching system implements a **priority-based best-match selection** protocol. For each instruction being lowered, the ISel infrastructure invokes all applicable matchers (dispatched through vtable function pointers, not direct calls). Each matcher independently tests whether the instruction matches its pattern; if it does, it writes a `(template_id, priority)` pair to the output parameters. The dispatcher selects the match with the highest priority value.

**Function signature** (all 801+ matchers):

```c
char __fastcall match(
    int64_t  ctx,           // a1: ISel context (passed through to field reader)
    int64_t  dag_node,      // a2: pointer to the Ori IR instruction node
    int32_t *template_id,   // a3: OUT: encoding template index [1..152]
    int32_t *priority       // a4: IN/OUT: current best priority; written only if better
);
```

The `priority` parameter is read-then-conditionally-written: the matcher checks `if (*a4 <= threshold)` before overwriting. This means the dispatcher initializes `*a4 = 0` and calls matchers in sequence; each matcher only upgrades the result if its specificity exceeds the current best. After all matchers complete, `*a3` holds the template index of the winning pattern.

**Matching pipeline** (invariant across all 801 matchers):

```
 1. OPCODE PROPERTY CHECKS      sub_10AE5C0(ctx, node, field_id)
    Check 1-12 instruction properties against expected values.
    Any mismatch -> return immediately (early exit).

 2. SOURCE OPERAND COUNT         sub_B28F50(node) -> source_count
    Verify the instruction has the expected number of source operands.

 3. SOURCE OPERAND VALIDATION    sub_B28F30(node, i) -> operand_record
    For each source operand:
      a. Type predicate: isImmediate / isGPR / isPredicate / isUniformReg / ...
      b. Register class: class == 1023 (wildcard) OR class == specific_value

 4. RESULT OPERAND COUNT         sub_B28F40(node) -> result_count
    Verify the expected number of result (destination) operands.

 5. RESULT OPERAND VALIDATION    sub_B28F30(node, first_result + j)
    Same type + register-class checks as for source operands.
    First-result index = sub_B28E00(*(node + 92)).

 6. PRIORITY WRITE               if (*a4 <= N) { *a4 = N+1; *a3 = template; }
    Conditional update: only overwrite if this pattern is more specific
    than whatever was already matched.
```

#### Match-Score Priority System

The priority values range from 2 (least specific) to 34 (most specific), with the distribution heavily concentrated in the 8--19 range. The priority correlates directly with pattern specificity: matchers with more constraints (more `sub_10AE5C0` checks, more operand type checks, tighter register class requirements) assign higher priority values.

| Priority range | Count | Interpretation |
|---|---|---|
| 2--5 | 31 | Fallback / generic patterns (few constraints) |
| 6--10 | 253 | Common patterns (3--6 constraints) |
| 11--15 | 293 | Standard patterns (5--8 constraints) |
| 16--20 | 168 | Specific patterns (6--10 constraints) |
| 21--34 | 56 | Highly specific patterns (8--12+ constraints) |

Template IDs range from 1 to 152. Multiple matchers can target the same template ID at different priority levels, forming a specificity ladder: a generic matcher might match `FADD` at priority 8 while a specialized matcher matches `FADD.FTZ.SAT` with specific register classes at priority 17. Both write the same template ID but the specialized matcher wins when its constraints are satisfied.

#### Dispatcher Mechanism

The matchers are **not** called directly from a single dispatch function. Instead, they are registered as virtual methods on per-instruction-class descriptor objects. The dispatch chain is:

```
sub_B285D0 (ISel driver, 9 KB)
  -> opcode switch on (instruction[18] & 0xFFFFCFFF)
     -> selects builder variant (sub_B1FA20 / sub_B20E00 / sub_B1EC10 / ...)
        -> builder invokes vtable method on instruction descriptor
           -> vtable slot contains pointer to one of the 801 pattern matchers
              -> matcher writes (template_id, priority) if pattern matches
```

For a given instruction, the dispatcher may invoke multiple matchers (one per applicable template variant). Each matcher independently checks its constraints and conditionally updates the priority/template pair. After all candidates have been tried, the dispatcher reads the final `template_id` and uses it to select the SASS encoding template.

#### Vtable Registration and Dispatch Table Layout

All ~801 matchers are registered through a single C++ vtable rooted at `off_22AD230` (installed by the descriptor constructor `sub_9CE190`). The vtable contains 244 general-purpose virtual method slots followed by a contiguous **dispatch table** of 1884 function pointers starting at vtable byte offset `+1952` (address `0x22AD9D0`). Every pointer in this dispatch table targets a location inside a single 200 KB monolithic function `sub_BA9CF0` (spanning `0xBA9CF0`--`0xBDBA60`), which the compiler emitted as an enormous switch-case body. Of the 1884 entries, 1611 point to distinct case handlers (the actual matcher logic) and 273 point to a shared sentinel at `0xBA9E23` (a no-op return indicating "no matcher for this slot").

The coordinator function `sub_13AF3D0` (137 KB) invokes matchers through indirect calls at specific vtable byte offsets. Each offset maps to a dispatch table index:

| Vtable offset | Dispatch index | Role |
|---|---|---|
| `+2328` | 47 | Guard check (compared against `sub_13A6110`) |
| `+2600` | 81 | Primary matcher -- operand-with-offset form |
| `+2616` | 83 | Secondary matcher -- multi-operand ALU (case 102) |
| `+2624` | 84 | Tertiary matcher -- extended ALU (case 108) |
| `+2656` | 88 | Quaternary matcher -- opcode 78 fallthrough |
| `+2896` | 118 | Predicated-store matcher (LD/ST `.STRONG`) |
| `+3232` | 160 | Architecture gate (compared against `sub_868720`) |

The coordinator uses a **guard-then-dispatch** invocation pattern:

```c
// sub_13AF3D0 -- primary matcher invocation (vtable +2600)
vtable = *(int64_t*)descriptor;
guard_fn = *(fn_ptr*)(vtable + 2328);
if (guard_fn == sub_13A6110) {                    // default guard -> proceed
    offset_ptr = &operand_array[operand_index];
    (*(fn_ptr*)(vtable + 2600))(descriptor, node, offset_ptr, out);
    if (!(descriptor[1039] & 2) && (offset_ptr[1] & 0x18000000))
        sub_13A5ED0(ctx, node, operand_index, out, ...);  // post-match fixup
} else {
    guard_fn(descriptor, node, operand_index, out, ...);   // SM-override path
}

// vtable +2896 -- sentinel-guarded dispatch for predicated stores
store_fn = *(fn_ptr*)(vtable + 2896);
if (store_fn != nullsub_239)                      // sentinel = no-op stub
    store_fn(descriptor, node, out);
```

The 1884 dispatch table entries partition into **144 non-sentinel groups** separated by sentinel runs. Group sizes range from 1 to 152 matchers, reflecting per-instruction-class matcher fan-out:

| Group size | Count | Example instruction classes |
|---|---|---|
| 1 | 27 | Single-encoding (NOP, EXIT, BAR) |
| 2--5 | 37 | Simple ALU (IADD, IMUL) |
| 6--10 | 44 | Common FP (FADD, FMUL, FSETP) |
| 11--25 | 16 | Complex LD/ST (LDG, STG, ATOM) |
| 26--50 | 11 | Wide-encoding (HMMA, conversion) |
| 51--152 | 9 | Max fan-out (opcode 0x135 arith) |

The matchers have no static callers -- they appear exclusively through indirect function pointer invocation via this vtable, which is why cross-reference tools report them as "no callers in function DB."

#### DAG Node Property Accessor -- `sub_10AE5C0`

The field reader is the most-called function in the matcher range (typically 2--12 calls per matcher, so 3,000--8,000 total invocations across all 801 matchers):

```c
// sub_10AE5C0 -- Read instruction property by field_id
int64_t DAGNode_ReadField(int64_t ctx, int64_t node, uint32_t field_id) {
    if (sub_10E32E0(node, field_id))        // field exists in descriptor?
        return sub_10D5E60(node, field_id); // read value from property table
    else
        return 0xFFFFFFFF;                  // sentinel: field not present
}
```

The `field_id` values form a large flat namespace (observed range: 5--595). These are **not** byte offsets into the instruction record; they are logical property identifiers resolved through a descriptor table. The backing store (managed by `sub_10E32E0` / `sub_10D5E60`) implements a sparse property bag that maps field IDs to integer values.

The companion write functions follow the same field-ID namespace:

```c
// sub_10AE590 -- Write single field
void DAGNode_WriteField(int64_t ctx, int64_t node, uint32_t field_id, uint32_t value);

// sub_10AE640 -- Write two fields atomically (multi-field update)
void DAGNode_WriteFields(int64_t ctx, int64_t node, uint32_t f1, uint32_t v1, uint32_t v2);
```

Semantic mapping of the 43 most frequently referenced field IDs, ranked by total read+write frequency across the 801 matchers ("W" = write-count in lowering functions, "R" = read-count in guard predicates):

| Field | Hex | W | R | Semantic name | Evidence / observed values |
|---|---|---|---|---|---|
| 345 | 0x159 | -- | 417 | **rounding mode selector** | Most-read field.  Guard value 1900 in 135 matchers; 1901--1903 select alternatives.  Drives field 300 (1900->1513, 1901/02->1515, 1903->1516). |
| 300 | 0x12C | 302 | -- | **rounding mode encoding** | Highest write count.  Values: 1513 (RN), 1515 (RZ/RM), 1516 (RP).  Always paired with field 301. |
| 301 | 0x12D | 222 | -- | **FTZ / rounding qualifier** | Values: 1521 (default/FTZ), 1520 (non-FTZ).  Co-occurs with field 300 in every rounding-sensitive matcher. |
| 480 | 0x1E0 | 163 | -- | **encoding format class** | Value 2481 dominant (126 writes); also 2480, 2478.  Selects top-level SASS encoding format (3-source vs immediate). |
| 212 | 0x0D4 | 140 | -- | **operand source layout** | Single value 1184.  Part of a 5-field "instruction shape descriptor" group with {456, 336, 316, 318}. |
| 456 | 0x1C8 | 140 | -- | **instruction shape tag** | Value 2373 (120 writes), 2375 (20).  Always precedes the shape descriptor group. |
| 336 | 0x150 | 120 | -- | **operand count / layout** | Values 1863, 1865.  Distinguishes 2-source from 3-source operand formats. |
| 318 | 0x13E | 120 | -- | **source B encoding class** | Values 1784, 1785, 1789.  Selects reg-imm vs reg-reg for second source operand. |
| 316 | 0x13C | 120 | -- | **source A encoding class** | Values 1776, 1777.  Paired with field 318 to specify the source operand encoding pair. |
| 150 | 0x096 | 101 | -- | **comparison mode** | Values 650, 651.  Used in ISETP/FSETP matchers for comparison operator. |
| 64  | 0x040 | 101 | -- | **destination data type** | Single value 297.  Sets the output data-type tag in the encoding descriptor. |
| 29  | 0x01D | 101 | -- | **predicate / condition code** | Single value 126.  Controls whether the instruction writes a predicate register. |
| 270 | 0x10E | 101 | -- | **accumulation mode** | Single value 1385.  Controls FMA-style accumulation (fused vs non-fused). |
| 88  | 0x058 | 90 | 2 | **sub-operation modifier** | Value 408 dominant.  Selects instruction sub-variant (e.g., ADD vs FADD within same opcode). |
| 359 | 0x167 | 90 | -- | **operand negation mask** | Single value 1957.  Encodes source-operand sign-flip for FP instructions. |
| 283 | 0x11B | -- | 80 | **source type A** | Guard values 1446--1449.  Read with field 284 to encode source type for HMMA and similar. |
| 284 | 0x11C | -- | 80 | **source type B** | Guard values 1451, 1452.  Paired with 283 (e.g., {1449,1452} -> FP16xFP16). |
| 210 | 0x0D2 | 65 | -- | **memory scope** | Values 1175--1177.  CTA / GPU / SYS scope for ld/st/atom. |
| 234 | 0x0EA | 60 | -- | **cache control modifier** | Single value 1258.  Encodes cache hints (L1 bypass, etc.). |
| 33  | 0x021 | 47 | -- | **data width specifier** | Values 147, 148.  Selects 32-bit vs 64-bit operation width. |
| 22  | 0x016 | 47 | -- | **integer sign qualifier** | Values 99 (unsigned), 101 (signed). |
| 154 | 0x09A | 41 | -- | **texture/surface sampler** | Values 664--669 (6 variants).  Selects among texture access modes. |
| 209 | 0x0D1 | 40 | -- | **memory ordering** | Single value 1172.  Sets acquire/release semantics. |
| 89  | 0x059 | -- | 36 | **operand class guard** | Read-only pre-condition filter; matchers exit early if absent or wrong. |
| 500 | 0x1F4 | 36 | -- | **extended modifier A** | Values 2547--2549.  Blackwell (sm_100+) matchers only. |
| 330 | 0x14A | -- | 32 | **precision / type tag** | Read-then-copied: propagates data-type attribute from source to output node. |
| 208 | 0x0D0 | 30 | -- | **atomic operation type** | Values 1163--1167 (ADD, MIN, MAX, CAS, EXCH). |
| 327 | 0x147 | -- | 30 | **register format descriptor A** | Read-only register class constraint.  Always checked with field 328. |
| 328 | 0x148 | -- | 30 | **register format descriptor B** | Read-only companion to field 327. |
| 281 | 0x119 | 30 | -- | **address space qualifier** | Single value 1436.  GLOBAL/SHARED/LOCAL address space tag. |
| 31  | 0x01F | 27 | -- | **logical operation selector** | Single value 133.  AND/OR/XOR for LOP3-class instructions. |
| 20  | 0x014 | 27 | -- | **immediate format** | Single value 94.  Immediate field encoding (sign extension, shift). |
| 293 | 0x125 | 27 | -- | **shift amount** | Single value 1489.  Shift distance for SHL/SHR. |
| 418 | 0x1A2 | 24 | -- | **uniform register hint** | Single value 2180.  Marks instruction eligible for uniform register allocation. |
| 344 | 0x158 | 24 | 12 | **output saturation** | Values 1896, 1897.  Bidirectional: read in guards, written in lowering.  [0,1] clamping. |
| 479 | 0x1DF | 22 | -- | **extended modifier B** | Values 2467, 2468.  Blackwell-era extended instruction property. |
| 432 | 0x1B0 | 22 | -- | **warp shuffle mode** | Values 2269, 2270.  SHFL sub-op (UP, DOWN, BFLY, IDX). |
| 452 | 0x1C4 | -- | 22 | **architecture guard** | Read-only.  Guards SM-generation-specific matchers. |
| 12  | 0x00C | 20 | 20 | **operand encoding class** | Value 59 (write).  Bidirectional; feeds LUT-based encoder selection. |
| 393 | 0x189 | 20 | -- | **dual-issue hint** | Values 2104, 2105.  Marks pairs eligible for dual-issue scheduling. |
| **397** | **0x18D** | **2** | **--** | **encoding validity stamp** | **Always value 2115 (0x843).**  Written by lowering; encoder `sub_1C4F470` ORs `0x843` into dword 0.  `0x843 = 0b100_0100_0011` (bits 0,1,6).  Post-ISel seal, not a pre-condition guard. |
| 326 | 0x146 | 2 | -- | **SASS major opcode** | Value 1810 (0x712).  Written once at node creation; identifies top-level SASS instruction class. |
| 5   | 0x005 | 2 | -- | **Ori opcode ID** | Value 12 (0x0C).  Copies internal Ori opcode number into the encoding descriptor. |

The value namespace is disjoint from the field-ID namespace -- each field's values are drawn from a global enumeration (~2,850 entries, max value 2829).  For any given field only 1--6 distinct values are ever written.  The encoding bitfield lookup table at VA `0x23F2E00` (4,096 entries, 98% fill) maps value IDs to concrete bit positions and masks in the SASS instruction word.

**Field 397 / value 2115 deep analysis.**  Contrary to the initial hypothesis that 397 acts as a pre-ISel validity guard, decompilation shows it is *write-only*: lowering functions stamp `field[397] = 2115` onto newly created nodes, and the encoder `sub_1C4F470` ORs the literal `0x843` into instruction-word dword 0.  `0x843 = 0b100_0100_0011`: bit 0 = opcode valid, bit 1 = encoding template assigned, bit 6 = ISel generation stamp.  This is a post-ISel seal confirming the instruction passed pattern matching and has a valid binary encoding template.

#### Operand Record Layout

Each operand is a 32-byte record accessed by index via `sub_B28F30`:

```c
// sub_B28F30 -- Get operand record by index
int64_t GetOperand(int64_t node, int index) {
    return *(int64_t*)(node + 32) + 32LL * index;
}
```

The 32-byte operand record:

| Offset | Size | Field | Description |
|---|---|---|---|
| +0 | 1 | `type_tag` | Operand kind (see predicate table below) |
| +4 | 4 | `primary_class` | Register class ID; 1023 = wildcard (any class) |
| +14 | 1 | `modifier_a` | Written by `sub_B28F10` |
| +15 | 1 | `modifier_b` | Written by `sub_B28F20` |
| +20 | 4 | `secondary_class` | Fallback register class constraint |

Source operand count is stored at `node + 92` and doubles as the first-result-operand index:

```c
uint32_t source_count = *(uint32_t*)(node + 92);   // sub_B28F50
uint32_t result_count = *(node + 40) + 1 - source_count; // sub_B28F40
```

#### Operand Type Predicates

Fifteen predicate functions classify operand type tags. Each is a single comparison returning `bool`:

| Address | Name | Test | Semantics |
|---|---|---|---|
| `sub_B28E20` | `isImmediate` | `tag == 1` | Constant / immediate literal |
| `sub_B28E10` | `isGPR` | `tag == 2` | General-purpose register |
| `sub_B28E80` | `isPredicate` | `tag == 3` | Predicate register |
| `sub_B28E70` | `isType4` | `tag == 4` | (specific operand class) |
| `sub_B28E60` | `isType5` | `tag == 5` | (specific operand class) |
| `sub_B28E30` | `isSpecialReg` | `tag == 6` | Special register |
| `sub_B28ED0` | `isType7` | `tag == 7` | (specific operand class) |
| `sub_B28EF0` | `isType8` | `tag == 8` | (specific operand class) |
| `sub_B28E50` | `isType9` | `tag == 9` | (specific operand class) |
| `sub_B28E40` | `isValidReg` | `tag == 10` | Generic valid register |
| `sub_B28EE0` | `isType11` | `tag == 11` | (specific operand class) |
| `sub_B28EA0` | `isType13` | `tag == 13` | (specific operand class) |
| `sub_B28EB0` | `isType14` | `tag == 14` | (specific operand class) |
| `sub_B28E90` | `isUniformReg` | `tag == 15` | Uniform register (SM 75+) |
| `sub_B28EC0` | `isType16` | `tag == 16` | (specific operand class) |

Register class 1023 serves as a wildcard: `if (class == 1023 || class == expected)`. This allows matchers to accept both unconstrained operands and operands already assigned to a specific register file.

#### Register Class Constraint Protocol

Operand records carry two register class fields: `primary_class` at offset +4 and `secondary_class` at offset +20. The matching protocol checks them with a cascading OR:

```c
// Typical register class check (from sub_B33F00, sub_B390A0, etc.)
uint32_t primary   = *(uint32_t*)(operand + 4);
uint32_t secondary = *(uint32_t*)(operand + 20);

if (sub_B28E00(primary) == 1023) {
    // Wildcard -- operand is unconstrained, accept it
} else {
    uint32_t cls = sub_B28E00(secondary);
    if (cls != expected_class) return;  // mismatch
}
```

`sub_B28E00` and `sub_B28F00` are identity functions -- the register class is stored as a plain integer, not packed. The two-field scheme allows the matcher to accept an operand where either the allocation constraint (primary) is wildcard or the resolved register file (secondary) matches.

Observed register class values in matchers:

| Class | Frequency | Likely meaning |
|---|---|---|
| 1023 | ubiquitous | Wildcard (any register class) |
| 1 | very common | 32-bit GPR (R0..R255) |
| 2 | common | 64-bit GPR pair |
| 3 | occasional | 128-bit GPR quad |
| 4 | occasional | Predicate or special register file |
| 5 | rare | Extended register class |

#### Representative Matcher Walkthroughs

**`sub_B30160`** -- simple 2-source, 4-result pattern (68 lines, priority 9, template 12):

```
1. field 480 == 2481                    -> opcode/subclass check
2. source_count == 2                    -> expects 2 source operands
3. operand[0].type == 1 (immediate)     -> first source is a constant
4. operand[1].type == 2 (GPR)           -> second source is a register
5. operand[1].class == 1023 OR sec == 1 -> 32-bit GPR or unconstrained
6. result_count == 4                    -> expects 4 result operands
7. result[0].type == 2 (GPR)            -> first result is GPR
   result[0].class == 1023 OR sec == 1
8. result[1].type == 3 OR 15            -> predicate or uniform register
9. result[2].type == 2 (GPR)            -> third result is GPR
   result[2].class == 1023 OR sec == 1
10. if (*a4 <= 8) -> *a4 = 9, *a3 = 12
```

**`sub_B33F00`** -- medium 2-source, 5-result pattern (4,166 bytes, priority 21, template 22):

```
1. field 7 == 21                            -> major opcode class
2. field 163 in {705, 706}                  -> addressing mode variant
3. field 203 in {1113..1117}                -> encoding format (5 values)
4. field 105 == 477                         -> operation variant
5. field 88 == 408                          -> sub-operation modifier
6. field 345 == 1903                        -> rounding/saturation mode
7. source_count == 2                        -> 2 sources
8. operand[0].type == 1 (immediate)         -> constant source
9. operand[1].type == 2 (GPR)              -> register source
   operand[1].class: primary wildcard or secondary in {1,2}
10. result_count == 5                       -> 5 results
11. result[0].type == 2 (GPR), class != 1023, secondary == 2 (64-bit)
12. result[1].type == 3 OR 15 (pred/uniform)
13. result[2].type == 2 (GPR), class: wildcard or secondary in {1,2}
14. result[3].type == 2 (GPR), class: wildcard or secondary in {1,2}
15. if (*a4 <= 20) -> *a4 = 21, *a3 = 22
```

**`sub_B44CA0`** -- complex 0-source, 7-result pattern (6,214 bytes, priority 11, template varies):

```
1.  field 5 == 12                           -> opcode class 12
2.  field 220 == 1206                       -> encoding property
3.  field 595 in {2937, 2938}               -> extended field (high range)
4.  field 294 == 1493                       -> constraint
5.  field 242 in {1281, 1282}               -> width qualifier
6.  field 355 == 1943                       -> extended property
7.  field 376 == 2035                       -> extended property
8.  field 377 in {2037..2041}               -> extended property (5 values)
9.  field 429 in {2252, 2253}               -> extended qualifier
10. field 126 in {547, 548}                 -> data type
11. field 397 == 2115                       -> encoding validity stamp (post-ISel seal)
12. source_count == 0                       -> no source operands
13. result_count == 7                       -> 7 result operands
14. All 7 results checked: type == 10 (valid register), various class constraints
15. if (*a4 <= 10) -> *a4 = 11, *a3 = (template)
```

This pattern has the most field checks (12) of the representative examples, validating properties deep into the extended field namespace (field 595). Its zero-source, seven-result shape suggests a hardware intrinsic or complex output instruction like a tensor-core operation.

**`sub_B28FE0`** -- minimal matcher in the preamble zone (31 lines, priority 8, template 42):

```
1. field 211 == 1182
2. field 201 == 1109
3. field 348 in {1912, 1915}   -> precision qualifier
4. field 397 == 2115           -> encoding validity stamp (post-ISel seal)
5. source_count == 0           -> no sources
6. if (*a4 <= 7) -> *a4 = 8, *a3 = 42
```

The simplest matchers skip operand validation entirely and rely solely on opcode-property checks. These are for instructions with fixed operand formats where the operand shape is fully determined by the opcode.

#### Helper Function Summary

| Address | Name | Signature | Purpose |
|---|---|---|---|
| `sub_10AE5C0` | `DAGNode_ReadField` | `(ctx, node, field_id) -> value` | Read instruction property by ID; returns `0xFFFFFFFF` if absent |
| `sub_10AE590` | `DAGNode_WriteField` | `(ctx, node, field_id, value)` | Write single instruction property |
| `sub_10AE640` | `DAGNode_WriteFields` | `(ctx, node, f1, v1, v2)` | Multi-field atomic update |
| `sub_B28F30` | `GetOperand` | `(node, index) -> operand_ptr` | Index into operand array (32-byte records at `*(node+32)`) |
| `sub_B28F40` | `GetResultCount` | `(node) -> count` | Number of result operands: `node[40] + 1 - node[92]` |
| `sub_B28F50` | `GetSourceCount` | `(node) -> count` | Number of source operands: `*(node+92)` |
| `sub_B28E00` | `DecodeRegClass` | `(packed) -> class_id` | Identity function (class stored as plain int) |
| `sub_B28F00` | `DecodeRegClass2` | `(packed) -> class_id` | Second identity accessor (same semantics) |
| `sub_B28F10` | `SetModifierA` | `(operand, value)` | Write operand modifier at offset +14 |
| `sub_B28F20` | `SetModifierB` | `(operand, value)` | Write operand modifier at offset +15 |
| `sub_B28E10` | `isGPR` | `(tag) -> bool` | `tag == 2` |
| `sub_B28E20` | `isImmediate` | `(tag) -> bool` | `tag == 1` |
| `sub_B28E30` | `isSpecialReg` | `(tag) -> bool` | `tag == 6` |
| `sub_B28E40` | `isValidReg` | `(tag) -> bool` | `tag == 10` |
| `sub_B28E50` | `isType9` | `(tag) -> bool` | `tag == 9` |
| `sub_B28E60` | `isType5` | `(tag) -> bool` | `tag == 5` |
| `sub_B28E70` | `isType4` | `(tag) -> bool` | `tag == 4` |
| `sub_B28E80` | `isPredicate` | `(tag) -> bool` | `tag == 3` |
| `sub_B28E90` | `isUniformReg` | `(tag) -> bool` | `tag == 15` |
| `sub_B28EA0` | `isType13` | `(tag) -> bool` | `tag == 13` |
| `sub_B28EB0` | `isType14` | `(tag) -> bool` | `tag == 14` |
| `sub_B28EC0` | `isType16` | `(tag) -> bool` | `tag == 16` |
| `sub_B28ED0` | `isType7` | `(tag) -> bool` | `tag == 7` |
| `sub_B28EE0` | `isType11` | `(tag) -> bool` | `tag == 11` |
| `sub_B28EF0` | `isType8` | `(tag) -> bool` | `tag == 8` |

### SM120 Pattern Coordinator -- `sub_13AF3D0` (137 KB)

The largest ISel function in the binary (137 KB, 4,225 lines, 570+ locals). It is an architecture-specific operand-emission coordinator that runs in Phase 2 as a **parallel backend** to the mega-selector `sub_C0EB10`. The two do not call each other -- they are mutually exclusive implementations of the same ISel protocol, selected per-SM by the vtable in the ISel driver. The mega-selector covers opcodes 7--221 for the default backend; the coordinator covers opcodes 2--352 for the SM120 (consumer RTX 50xx / enterprise Pro) backend.

#### Position in the ISel Pipeline

```
sub_B285D0 (ISel driver, 9 KB)
  -> selects builder variant by SM version
     -> Builder variant vtable dispatch
        |
        +-- DEFAULT BACKEND: sub_C0EB10 (mega-selector, 185 KB)
        |     opcodes 7..221, dual-switch, word_22B4B60 encoding table
        |
        +-- SM120 BACKEND: sub_A29220 (instruction iterator, 435 lines)
              -> sub_13AF3D0 (pattern coordinator, 137 KB)
                   opcodes 2..352, single switch, inline operand emission
```

The coordinator is called once per instruction by `sub_A29220`, which walks the instruction list. Before entering the main switch, the coordinator performs a predication pre-test: if bit `0x1000` is set in the opcode word and the opcode is not 169, it queries `vtable[3232/8]` and optionally emits the last source operand via `sub_13A6AE0`.

#### Dispatch Structure

The coordinator reads the opcode from `*(instr+72)` with the standard `BYTE1 & 0xCF` mask (identical to Phase 1's MercConverter) and enters a single 130-case switch. Unlike the mega-selector's dual-switch encoding-slot translation, the coordinator emits operands inline -- each case directly calls `sub_13A6280` (the operand emitter) with explicit operand indices.

```c
// sub_13AF3D0 -- simplified dispatch skeleton
char PatternCoordinator(context *a1, instruction *a2, output *a3,
                        pattern_table *a4, flags a5, int a6) {
    int opcode = *(DWORD*)(a2 + 72);
    BYTE1(opcode) &= 0xCF;

    // Pre-dispatch: predication check when bit 0x1000 is set
    if ((*(a2+72) & 0x1000) && opcode != 169) {
        if (vtable[3232/8] == sub_868720 || vtable[3232/8]())
            EmitLastSource(a1[1], a2, operand_count - 2*flag, a3);
    }

    // Setup output context
    vtable[104/8](output_ctx, a1, &context_ref);

    switch (opcode) {
    case 2: case 4: case 7:     // FMA/MAD 2-source
        operand_span = 16; src_count = 2;
        goto SHARED_FMA_HANDLER;
    case 3: case 5:             // FMA/MAD 3-source
        operand_span = 24; src_count = 3;
        goto SHARED_FMA_HANDLER;
    case 6:                     // IMAD/IADD3 with 3+ sources
        EmitOperand(ctx, instr, 3, out);
        EmitOperand(ctx, instr, 4, out);
        EmitOperand(ctx, instr, 5, out);
        break;
    case 8:                     // Pure vtable dispatch (vtable+2328)
        vtable[2328/8](a1, a2, operand_count, a3, a5, 0);
        break;
    case 10: case 11: case 151: case 152: case 290: case 291:
        vtable[2768/8](a1, a2, a3, a4, a5);   // Memory load/store
        break;
    case 16:                    // Texture/surface (163-line handler)
        for (i = first_src; i < last_src; i++)
            EmitOperand(ctx, instr, i, out);   // loop up to 15 operands
        break;
    // ... 120 more cases ...
    case 329:                   // Variable-count loop + vtable+2328
        for (i = 0; i < src_count; i++)
            EmitOperand(ctx, instr, i, out);
        vtable[2328/8](a1, a2, remaining, a3, a5, 0, 0, 0);
        break;
    default:
        break;                  // no-op passthrough
    }
}
```

#### Opcode Case Routing

The 130 distinct case labels (spanning 82 distinct handler blocks) cover the full SASS opcode range including SM100+/SM120 extensions:

| Opcodes | Handler pattern | Instruction family |
|---|---|---|
| 2, 3, 4, 5, 7 | Shared FMA handler with operand-span parametrization | FMA/MAD variants (32/64-bit) |
| 6 | Inline 3-source emission + optional operands 6/7 | IMAD/IADD3 wide |
| 8 | Pure vtable+2328 delegation | Builder-only instructions |
| 10, 11, 151, 152, 290, 291 | vtable+2768 delegation | Memory load/store |
| 16 | 163-line operand loop (up to 15 sources) | Texture/surface |
| 20, 21 | vtable+2680/2688 with stub check | Memory/store alternates |
| 22, 77, 83, 297, 352 | vtable+2744 with `nullsub_463` check | Control flow |
| 24, 34, 209, 213, 214 | Passthrough: emit src 1 + dst 2 | Simple 2-operand ALU |
| 29, 95, 96, 190 | Conditional operand-6 check | Predicate-source instructions |
| 38, 59, 106, 180, 182, 192, 194, 215, 221 | Single `EmitOperand(1)` at high SM | Generic ALU |
| 42, 53, 55 | `EmitOperand(1)` | Paired ALU |
| 60, 61, 62, 63, 64 | Comparison / inner sub-opcode switch (case 61: 5 sub-cases) | Compare / set-predicate |
| 88, 89 | Variable source count (2 or 3) with sign-dependent offsets | Extended FMA |
| 110, 111, 114, 115, 117 | Warp operand emission | Warp shuffle / vote |
| 120, 121, 126, 127 | Barrier handler with operand loop at LABEL_53 | Barrier / sync |
| 139, 140, 141, 143 | `sub_13A4DA0` for commutative operand selection | Commutative ALU |
| 183 | Extended memory with register-class-6 check | Wide memory |
| 201, 202, 204 | vtable+2328 delegation | Async / bulk operations |
| 270, 279, 282, 285, 325--328 | Goto LABEL_53 (barrier/sync shared handler) | Extended memory / warp |
| 280, 281 | vtable+2896 with `nullsub_239` check, then LABEL_53 | Sync instructions |
| 329 | Variable-count operand loop + vtable+2328 | Variable-width encoding |

#### Three Competing-Match Selection Mechanisms

The coordinator selects among competing pattern matchers through three mechanisms:

**1. LABEL_750 -- vtable alternate-match dispatch.** Six opcode paths (cases 6, 36, 130, 137, plus opcodes reaching LABEL_119 when `sub_7D6850` confirms a double-precision operand) jump to LABEL_750:

```c
LABEL_750:
    replacement = vtable[16/8](output_ctx, instruction);
    *output = replacement;
    return;
```

This is the "try architecture-specific alternate" escape hatch. The vtable slot at offset +16 on the ISel context object points to an SM-specific matcher. If it succeeds, the coordinator's inline emission is entirely bypassed and the replacement instruction is written to the output.

**2. `sub_13A4DA0` -- commutative operand position selector.** Called 12 times for commutative instructions (FMA, IADD3, comparison) where source operands can be swapped for better encoding. The function holds up to 4 pattern entries at offsets +12/+16 through +36/+40, each a `(lo_word, hi_word_mask)` pair. It tests operand properties via `sub_13A48E0` against each entry; the first match returns a preferred operand index. The coordinator then calls `sub_13A6280` with the returned index instead of the default.

```c
// sub_13A4DA0 -- simplified
int SelectOperandSlot(pattern_table, instruction, default_slot, alt_slot, out_match) {
    if (!pattern_table->active) return default_slot;
    uint64_t operand_desc = GetOperandDescriptor(instruction, default_slot);
    for (i = 0; i < pattern_table->count; i++) {  // up to 4 entries
        if (operand_desc matches pattern_table->entry[i])
            { *out_match = entry[i].preferred; return default_slot; }
    }
    // Repeat with alt_slot if no match on default_slot
    operand_desc = GetOperandDescriptor(instruction, alt_slot);
    for (i = 0; i < pattern_table->count; i++) {
        if (operand_desc matches pattern_table->entry[i])
            { *out_match = entry[i].preferred; return alt_slot; }
    }
    return default_slot;
}
```

**3. Inline vtable override checks.** Many cases test whether a vtable function pointer equals a known null-stub before calling it. The stub addresses serve as sentinel values -- when the vtable slot has been overridden by an SM-specific implementation, the coordinator calls the override:

| Vtable offset | Default stub | Purpose |
|---|---|---|
| +2680 | `sub_A8CBE0` | Memory operation alternate matcher |
| +2688 | `sub_A8CBF0` | Store operation alternate matcher |
| +2744 | `nullsub_463` | Control flow alternate |
| +2632 | `nullsub_233` | Move/convert alternate |
| +2760 | `nullsub_235` | Atomic/barrier alternate |
| +2896 | `nullsub_239` | Sync instruction alternate |
| +3232 | `sub_868720` | Pre-dispatch predication alternate |
| +3112 | `sub_A8CCA0` | MADC alternate (case 36) |

When the vtable slot holds the stub, the coordinator skips the call and proceeds with its inline emission logic.

#### Primary Callee: `sub_13A6280` (239 lines)

The operand emitter, called 83 times. It reads the operand at `instruction[operand_index + 10]` (each operand is 8 bytes starting at `instruction + 84`), checks the type tag at bits `[31:28]`, and emits:

- **Tag 1 (register):** fast-path returns if register class == 6 (UB/dead register). Otherwise reads the register descriptor from `*(context+88)[reg_index]`, checks register class at descriptor offset +64.
- **Tags 2/3 (constant/immediate):** calls `sub_7DBC80` to validate constant-bank availability, then `sub_A9A290` for type-5 immediate expansion. Delegates to vtable methods at `*(*(context+1584) + 1504)` and `*(*(context+1584) + 3248)`.
- **Other types:** pass through to the vtable dispatch chain.

The third parameter (operand index) ranges from 0 to 7 across the coordinator's call sites, with 0/1/2/3 being the most common (corresponding to the first 4 source operands in the Ori IR instruction layout).

#### Function Map Additions

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_13AF3D0` | 137 KB | SM120 ISel pattern coordinator (130-case switch, 83x operand emission) | HIGH |
| `sub_A29220` | 435 lines | Instruction iterator / coordinator caller (per-instruction walk) | HIGH |
| `sub_13A6280` | 239 lines | Operand emitter (type-tag dispatch, register class 6 fast-path) | HIGH |
| `sub_13A7410` | -- | Destination operand emitter (with register class 6 check) | MEDIUM |
| `sub_13A6AE0` | -- | Pre-dispatch source emitter (predicated instruction operands) | MEDIUM |
| `sub_13A4DA0` | 180 lines | Commutative operand position selector (4-entry pattern table) | HIGH |
| `sub_13A6F90` | -- | Extended destination emitter (3rd variant, class 6 check) | MEDIUM |
| `sub_13A6790` | -- | Fenced memory operand emitter | MEDIUM |
| `sub_13A45E0` | -- | Extra operand emitter (operands 6/7 for wide instructions) | MEDIUM |
| `sub_13A5ED0` | -- | Modifier flag emitter (operands with 0x18000000 bits) | MEDIUM |
| `sub_13A75D0` | -- | Register class 6 (UB) operand substitution handler | MEDIUM |
| `sub_13A48E0` | -- | Operand property extractor (for sub_13A4DA0 matching) | MEDIUM |

### Architecture Dispatch Tables -- 5 Thunks into Shared Handler at `loc_1C38C00`

Five 13-byte thunks each set an SM-family selector in `esi` and tail-jump to a single shared handler:

| Thunk | Address | `esi` value | SM family (inferred) |
|---|---|---|---|
| `sub_B12920` | `0xB12920` | `0x4000` | Turing / Ampere (sm_75--86) |
| `sub_B12910` | `0xB12910` | `0x5000` | Ada (sm_89) |
| `sub_B12900` | `0xB12900` | `0x5001` | Hopper (sm_90) |
| `sub_B128F0` | `0xB128F0` | `0x5003` | Blackwell datacenter (sm_100/103) |
| `sub_B128E0` | `0xB128E0` | `0x5004` | Blackwell consumer (sm_120/121) |

Each thunk is identical in structure (`mov esi, <imm32>; mov rdi, rdx; jmp loc_1C38C00`). The shared handler contains a switch on `*(a3+12)` (instruction type field, 36+ top-level cases) with secondary switches on `*(a3+14)` (type sub-field) in 15 of those cases. Return value is a SASS encoding slot index; 772 is the universal "invalid / unhandled" sentinel.

**Top-level case return values** (from `sub_B12920` / `esi=0x4000`, the only variant where the decompiler resolved concrete return values instead of JUMPOUTs):

| Case | Sub-switch? | Slot index / range | Notes |
|---|---|---|---|
| 0 | yes, `*(a3+14)` | 197, 526, 691, 697, 772 | 5 sub-variants by data width |
| 1 | yes | 21, 647, 772 | Narrow (<=2) vs wide (3--4) vs invalid |
| 2 | -- | 636 | Single slot |
| 3 | -- | 22 | Single slot |
| 4 | yes | 25, 26, 179, 180, 772 | Even/odd sub-field pairs |
| 5 | -- | 27 | Single slot |
| 6 | yes | 28, 646, 772 | Same narrow/wide pattern as case 1 |
| 7 | yes | 29, 30, 181, 182, 772 | 4-way by sub-field |
| 8--0xA | -- | 31, 32, 33 | Three consecutive single-slot cases |
| 0xB | yes, 15 sub-cases | 526, 527, 549, 571, 572, 697--699, 704, 764 | Texture/surface types |
| 0xC | yes, 56 sub-cases | 52, 72, 73, 82, 103, 132, 140, 557, 666, 668, 683, 684, 752, 758 | Memory load/store variants |
| 0xD | yes, 52 sub-cases | 55, 79, 80, 84, 85, 106, 107, 558, 667, 669 | Conversion operations |
| 0xE | yes, 37 sub-cases | 34, 42, 638, 639, 643, 744, 746, 748 | Predicate/comparison ops |
| 0xF | yes, 41 sub-cases | 60, 86, 90, 91, 121, 148, 149, 166, 568, 672, 685 | FP math variants |
| 0x10 | yes, 67 sub-cases | 61, 98, 124, 175, 176, 556, 676, 688, 689, 703 | Integer math variants |
| 0x11 | -- | 65 | Single slot (MOV/CVT) |
| 0x12 | yes, 249 sub-cases | 66--71, 94, 95, 104, 125--129, 662--665, 673, 678--681 | Largest: ALU permutations |
| 0x13 | -- | 685 | Single slot |
| 0x14 | -- | 75 | Single slot |
| 0x15 | yes | 87, 670, 772 | Narrow/wide pattern |
| 0x16--0x17 | yes, identical | 88, 120, 671, 753, 760 | Duplicated: shift variants |
| 0x18 | -- | 8 | Single slot |
| 0x19 | yes, 17 sub-cases | 96, 586, 587, 674, 707, 708 | Shared memory ops |
| 0x1A | yes, 16 sub-cases | 97, 123, 675, 762 | Extended shared memory |
| 0x1B | yes, 113 sub-cases | 595, 612, 613, 615, 619--621, 715, 718, 719, 721, 723 | Tensor/MMA (sparse sub-field) |
| 0x1C | yes | 130, 682, 772 | Narrow/wide pattern |
| 0x1D | yes, 14 sub-cases | 140, 683, 684 | Memory fence variants |
| 0x1E--0x1F | -- | 143, 144 | Two single-slot cases |
| 0x20 | yes, 40 sub-cases | 147--149, 685 | FP extended variants |
| 0x21 | -- | 709 | Single slot |
| 0x22 | yes, 46 sub-cases | 184--187, 190--192, 690 | Warp intrinsic variants |
| 0x23 | yes, 58 sub-cases | 5, 188, 190, 637, 640, 745, 747, 749, 750 | Async/bulk ops |

Total unique encoding slots referenced across all cases: ~120 distinct values out of a 772-entry namespace. The four `0x50xx` thunks share all JUMPOUT targets with `sub_B128E0`, confirming the shared handler selects per-SM slot variants internally based on the `esi` family code rather than at the thunk level.

### Opcode Variant Selectors

Two specialized variant selectors handle the final opcode-to-encoding mapping for specific instruction families:

**`sub_B0BE00`** (19 KB) -- opcode class 194:

Full switch on `a2` with 255 sequential cases plus default. The function unpacks `ctx = *(a1+8)`, `inst = *(a1+16)`, then dispatches:

```
default  -> sub_10AE590(ctx, inst, 194,  826)
case   1 -> sub_10AE590(ctx, inst, 194,  827)
case   2 -> sub_10AE590(ctx, inst, 194,  828)
  ...        (strictly linear: slot = 826 + case_value)
case 254 -> sub_10AE590(ctx, inst, 194, 1080)
case 255 -> sub_10AE590(ctx, inst, 194, 1081)
```

Closed-form mapping: **`slot_index = 826 + a2`** for `a2` in `[1, 255]`; slot 826 is the fallback for any value outside that range (including 0 and values > 255). The 256 encoding slots (826--1081) are contiguous within the ISel dispatch table at `0x22AD9D0`, occupying pointer entries `[826]` through `[1081]` of the 1884-entry `isel_dispatch_tables` array. This is the largest single opcode family in the table -- opcode class 194 alone accounts for 13.6% of all ISel dispatch entries.

The perfect linearity (no gaps, no reordering, no secondary switch) distinguishes this selector from `sub_B0AA70` (class 306) which uses sparse non-sequential case indices. The identity mapping `case K -> slot 826+K` means the switch is logically a bounds-checked offset addition -- the compiler emitted it as a jump table.

**`sub_B0AA70`** (5 KB) -- opcode class 306:
- Same pattern but with opcode class 306
- Variants numbered 1680--1726 with non-sequential case indices (2, 3, 8, 9, 14, 15, 20, 21, 26, 27, 30, 31, 36, 37, 40, 41, ...)
- The alternating-pair pattern at stride 6 suggests type-width combinations (e.g., F32/pair, F64/pair, S32/pair, ...)

### Instruction Modifier Dispatchers

Two modifier-application functions run after the main ISel selection to set type modifiers, rounding modes, and register width:

**`sub_B13E10`** (5,792 B) -- basic modifier dispatcher (21 calls to `sub_10AE640`):

| Stage | Bitfield extract | Field ID | Switch mapping |
|-------|-----------------|----------|----------------|
| Register width | `(a7 >> 3) & 0x3` | 19 | 0/1 -> 71, 2 -> 72, 3 -> 73 (type 13: <=1 -> 71, else 72/73) |
| Modifier type | `BYTE1(a7) & 0x1F` | 8 | 1 -> 31, 2 -> 33, 3 -> 34, 4 -> 32, 5 -> 35; 6--13 -> skip |
| Saturation | `BYTE4(a7) >> 3` | 16 / 3 | 1 -> 62 (type 18: fld 16; else fld 3 val 13), 2 -> 63 (type 18: fld 16; else fld 3 val 14), 3 -> 65, 4 -> 64 |
| Rounding (hi) | `(HIDWORD(a7) >> 14) & 0x1F` | 3 | 1 -> 13, 2 -> 14 |
| Saturate flag | `a8 & 0x10` | 20 | set -> 76, clear -> 75 |
| Negate flag | `a8 & 0x20` | 21 | set -> 79, clear -> 78 |
| Abs flag | `(HIBYTE(a7) >> 1) & 0xF` | 22 | 2 -> 82, else -> 81 |
| Complement flag | `a8 & 0xF` | 23 | 2 -> 85, else -> 84 |
| Carry enable | `a8 >> 6` | 18 | 1 -> 69 |
| Widen flag | `HIBYTE(a8) & 3` | 17 | 1 -> 67 |
| Integer type | `(HIBYTE(a8) >> 2) & 7` | 4 | 1 -> 16, 2 -> 17 |
| Cache hint A | `(WORD1(a7) >> 7) & 0xF` | 15 | 1 -> 55, 2 -> 57, 3 -> 59, 4 -> 60 |
| Cache hint B | `BYTE4(a7) & 7` | 15 | 1 -> 56, 2 -> 58 |

**`sub_B157E0`** (11,815 B) -- extended modifier dispatcher (37 calls to `sub_10AE640`):

Texture/surface fast path (opcode type == 18, runs before anything else):

| `BYTE5(a7) & 0x3F` | Field ID 15 value |
|---------------------|-------------------|
| 0 (default) | 54 |
| 1 | 55 |
| 2 | 56 |
| 3 | 57 |
| 4 | 58 |
| 5 | 59 |
| 6 | 60 |

Extended stages for types 12/13/15 (load/store/atomic) and type 18 at geometry:

| Stage | Bitfield extract | Field ID | Switch mapping |
|-------|-----------------|----------|----------------|
| LD/ST qualifier | `BYTE1(a7) >> 5` | 26 | 1 -> 99, 2 -> 100, 3 -> 101 |
| Address mode | `BYTE2(a7) & 7` | 29 | 1 -> 116, 2 -> 117 |
| Memory ordering | `BYTE4(a7) >> 3` (non-tex) | 24 | 1 -> 87, 2 -> 88 |
| Reg size | `(v15 + 26) & 0x1F` | 25 | 0--7 -> 90--97 |
| Type size | `(v51 + 26) & 0x1F` | 25 | 0--7 -> 90--97 (same table) |
| Geometry (non-tex) | `(v17 + 13) & 0xF` | 27 | 0 -> 107, 1 -> 106, 2 -> 105, 3 -> 104, 4 -> 103 |
| Tex/surf dim (type 18) | `v17` | 28 | 0 -> 108, 1 -> 109, 2 -> 110, 3 -> 111, 4 -> 112, 5 -> 113, 6 -> 114 |

After these type-specific stages, `sub_B157E0` runs the identical tail as `sub_B13E10` (register width through cache hint B) with matching field IDs and values.

## Mercury Master Encoder -- `sub_6D9690` (94 KB)

The Mercury master encoder is the single largest backend function and the final instruction selection point before binary emission. It contains a massive switch on the instruction type field (read from `instruction+8`) covering all SASS instruction formats. While its primary role is encoding (documented in [Mercury Encoder Pipeline](./mercury.md) and [SASS Instruction Encoding](./encoding.md)), the switch itself performs the final opcode-to-encoding-format selection:

```c
// Simplified encoding flow
void EncodeInstruction(context, instruction) {
    int type = *(int*)(instruction + 8);
    uint64_t base = 0x2000000000LL;     // encoding base constant

    switch (type) {
    case 61:    // FFMA with literal operand
        sub_6D9580(ctx, operand);       // encode literal
        break;
    case 455:   // complex multi-operand format
        // bit-field extraction and assembly
        break;
    // ... hundreds of cases ...
    }

    // Common tail: append operand words, commit
    sub_6D2750(ctx, word);              // append 8-byte operand word
    sub_6D28C0(ctx);                    // commit instruction record
}
```

Key encoding dispatch details:
- Operand word type prefix in bits `[31:28]`: `0x1` = register, `0x5` = immediate/constant, `0x6` = control/modifier, `0x7` = literal, `0x9` = special
- `sub_7D6860` handles data type encoding (FP32/FP64/INT)
- `sub_C00BF0` provides opcode lookup from the encoding tables
- Architecture-specific bits accumulated via SM 100+ extensions controlled by knob 4176

## MercExpand -- Pseudo-Instruction Expansion

`sub_C3CC60` (26 KB) runs as phase 118 (`MercExpandInstructions`) and expands Mercury pseudo-instructions into concrete SASS sequences. This is the third and final instruction selection point -- where abstract instruction forms that survived through ISel and Mercury encoding are replaced by their concrete multi-instruction implementations.

| Handler | Size | Instruction class |
|---|---|---|
| `sub_C37A10` | 16 KB | General instruction expansion (jump table, 4+ cases) |
| `def_C37B2E` | 13 KB | Complex expansion cases (default handler, string `"EXPANDING"`) |
| `sub_C39B40` | 10 KB | Memory operations (LDG, STG, LDS, etc.) |
| `sub_C3A460` | 6 KB | Atomic operations |
| `sub_C3B560` | 8 KB | Texture operations |
| `sub_C3BCD0` | 19 KB | Control flow (branches, jumps, calls) |
| `sub_C3E030` | 18 KB | Finalization and cleanup |

The expansion creates new instruction nodes, links them into the doubly-linked list, and deletes the original pseudo-instruction. After all expansions, `sub_C3E030` performs post-expansion verification. The expansion engine also uses `sub_719D00` (50 KB), which builds output for expanded instructions across different operand widths (32/64/128-bit, predicate) -- four near-identical code blocks corresponding to template instantiations over operand width types.

## OCG Encoding Template Lookup -- `sub_C3F490`

The OCG (Optimized Code Generation) intrinsic pipeline on SM100+ does not use the ISel mega-selector or DAG pattern matchers. Instead, the OCG router (`sub_6CC690`, documented in [Intrinsics](../intrinsics/index.md#ocg-intrinsic-lowering-pipeline----sub_6a97b0--sub_6cc690)) assigns each instruction one of 7 **internal routing values** and passes it to the SASS instruction emitter `sub_6CB8A0`. These routing values are **not** Ori IR opcodes, **not** binary SASS opcodes, and **not** encoding slot indices from `word_22B4B60`. They are a small, closed set of keys that exist solely to select an operand gathering template inside `sub_C3F490`.

### Routing values assigned by the OCG router

| Value | Hex | Instruction class | Assigned when |
|---|---|---|---|
| 70 | `0x46` | Memory-ordered load/store/atomic (with barrier) | Barrier register present (`v108 != 0` in conditional paths) |
| 243 | `0xF3` | Default memory operation | Fallback for general memory ops without barrier or special fence |
| 245 | `0xF5` | Load variant (LD/LDG/LDS) | Load-type operations (from OCG load/store handler) |
| 246 | `0xF6` | Reduction/atomic default | Atomic operations and reductions |
| 247 | `0xF7` | Fenced memory operation (LDGSTS) | Operations requiring memory fence semantics |
| 257 | `0x101` | Async copy without memory order | Bulk copy ops when no barrier: `v108 == 0` selects 257, else 70 |
| 261 | `0x105` | Atomic with pre-existing value read | Atomic exchange / compare-and-swap returning old value |

### How `sub_C3F490` maps routing values to encoding templates

`sub_C3F490` is a pure lookup function (184 bytes) that takes a routing value plus 7 boolean modifier flags and returns a pointer to an operand gathering template in `.data` at `0x22B8960`--`0x22BB460`. The function is a nested if-else tree: the first-level switch selects on the routing value, then inner branches refine the template based on the modifier flags.

```
sub_C3F490(routing_value, a2..a8) -> template_ptr
    a2: has pre-existing-value operand (used only by value 257)
    a3: SM generation > sm_7x (SM80+)
    a4: has predicate attachment
    a5: has scope/fence operand (SM generation > sm_8x && memory_order == 4)
    a6: (always 0 from OCG emitter, used by MercExpand callers)
    a7: (always 0 from OCG emitter, used by MercExpand callers)
    a8: (always 0 from OCG emitter, used by MercExpand callers)
```

The OCG emitter (`sub_6CB8A0`) always passes `a6=a7=a8=0`, which means the OCG path only reaches a subset of template leaves. The MercExpand callers (`sub_C41100`, `sub_C40420`, `sub_C40B90`, `sub_C42330`) pass all 7 flags and can reach the full template space. The returned template is a packed array: `template[0]` is the operand count, followed by operand slot indices that reference positions in the 39-QWORD operand buffer (v134[]). The emitter iterates over these indices, gathers the tagged operand words, builds control words from bitfields, and calls `sub_9314F0` to commit the encoded instruction.

Two additional routing values (254, 262) are handled by `sub_C3F490` but are **never assigned by the OCG router** -- they originate exclusively from the MercExpand memory instruction handlers, where the routing value is read from the instruction's opcode field (`instr[18]` masked with `& 0xCFFF`).

| Value | Hex | Origin | Instruction class |
|---|---|---|---|
| 254 | `0xFE` | MercExpand only | Extended memory format (operand gather mode 3) |
| 262 | `0x106` | MercExpand only | Wide memory format (operand gather mode 0, with scope/fence branches) |

### Template address space

The 40+ distinct templates returned by `sub_C3F490` occupy a contiguous `.data` region:

| Address range | Routing values served |
|---|---|
| `0x22B8960`--`0x22B8E60` | 257 (async copy variants) |
| `0x22B8E60`--`0x22B9360` | 70 (barrier memory variants) |
| `0x22B9360`--`0x22B9860` | 262 (MercExpand wide memory) |
| `0x22B9860`--`0x22B9E60` | 247, 245 (fenced / load variants) |
| `0x22B9E60`--`0x22BA960` | 243, 246, 70 (default / reduction / barrier sub-variants) |
| `0x22BA960`--`0x22BB460` | Leaf templates for bare operand forms (no modifiers) |

Each template is 256 bytes (0x100). For a given routing value, the modifier flags select progressively simpler templates as flags are cleared: the most complex template (all modifiers active) is reached first in the if-chain, and the simplest (no modifiers) is the final fallback.

## Addressing Mode Selection

Addressing mode selection is distributed across Phases 1 and 2. During Phase 1, the operand processing function `sub_6273E0` (44 KB) classifies PTX operand forms into internal categories. During Phase 2, the ISel driver and Mercury encoder select the optimal SASS addressing mode based on the register-allocated operand forms.

PTX addressing modes and their SASS encodings:

| PTX syntax | Addressing mode | SASS instruction | Encoding |
|---|---|---|---|
| `[%rd1]` | Register indirect | `LDG.E R0, [R2]` | Register + zero offset |
| `[%rd1+16]` | Register + offset | `LDG.E R0, [R2+0x10]` | Register + immediate offset |
| `c[2][0x100]` | Constant bank | `LDC R0, c[0x2][0x100]` | Bank index + offset |
| `[%rd1], %r2` | Base + index | `STG.E [R2], R4` | Separate base/data registers |

Special string references in `sub_6273E0` confirm complex addressing:
- `".nv.reservedSmem.offset0"` -- reserved shared memory region
- `"COARSEOFFSET"` -- coarse-grained offset computation for large address spaces
- `"__$endLabel$__%s"` -- label generation for structured control flow

The ISel mega-selector (`sub_C0EB10`) references `"__nv_reservedSMEM_offset_0_alias"` for shared memory alias resolution during final encoding.

## Vtable Dispatcher Zone -- `0xAF0000`--`0xB10000`

The range `0xAF0000`--`0xB10000` contains approximately 2,735 tiny vtable method implementations (average 160 bytes) that form the instruction encoding hierarchy. These implement polymorphic instruction property queries:

```c
// Typical vtable method (sub_AFXXXX, ~160 bytes)
int64_t get_property(int64_t a1, unsigned int a2) {
    if (a2 <= N)
        return (unsigned int)dword_XXXXXXX[a2];  // table lookup
    return default_value;
}
```

Each function maps a small integer index to an encoding constant, answering questions like "what is the register class for operand N of this instruction?" The 0xAF0000--0xB00000 sub-range has 1,269 functions (all under 200 bytes), while 0xB00000--0xB10000 has 1,466 with slightly more complex logic (13 exceeding 1 KB).

## Comparison with LLVM ISel

| Aspect | LLVM | ptxas |
|---|---|---|
| **ISel framework** | SelectionDAG or GlobalISel (single pass) | Two-phase: MercConverter (phase 5) + ISel driver (phase 112+) |
| **Pattern specification** | TableGen `.td` files, machine-generated | Handwritten C++ (~750 functions) |
| **Pattern count** | Target-dependent (thousands for x86) | ~801 DAG matchers + 185 KB mega-selector |
| **Architecture dispatch** | Subtarget feature bits | 4 architecture dispatch tables + vtable overrides |
| **Intermediate form** | MachineInstr (already selected) | Ori IR (SASS opcodes after phase 5, not yet encoded) |
| **Encoding** | MCInst emission (separate pass) | Integrated: ISel + Mercury encode in same pipeline |
| **Expansion** | Pseudo-instruction expansion in AsmPrinter | MercExpand (phase 118, post-ISel) |
| **Optimization post-ISel** | MachineFunction passes | Phases 14--111 (full optimizer runs between Phase 1 and Phase 2) |

The key architectural difference: LLVM performs instruction selection once, then optimization happens on already-selected machine instructions. ptxas selects SASS opcodes early (phase 5) so the optimizer can reason about SASS-level semantics, then performs a second selection/encoding pass after optimization is complete. This two-phase design gives the optimizer accurate cost models (it sees real SASS opcodes, not abstract PTX operations) at the cost of architectural complexity.

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_C0EB10` | 185 KB | ISel mega-selector (719 locals, dual 169-case switch, SM-generation dispatch) | HIGH |
| `sub_6D9690` | 94 KB | Mercury master encoder (instruction type switch) | VERY HIGH |
| `sub_9F1A90` | 35 KB | MercConverter main instruction conversion pass | HIGH |
| `sub_9EF5E0` | 27 KB | Post-MercConverter lowering (`"CONVERTING"`) | HIGH |
| `sub_C3CC60` | 26 KB | MercExpand::run (pseudo-instruction expansion) | HIGH |
| `sub_9ED2D0` | 25 KB | MercConverter opcode dispatch (master switch, `& 0xCF` mask) | HIGH |
| `sub_9E6600` | 25 KB | Instruction expansion (64-bit split) | HIGH |
| `sub_9EC340` | 23 KB | Multi-operand instruction legalization | MEDIUM |
| `sub_B0BE00` | 19 KB | Opcode variant selector (class 194, 100+ cases) | HIGH |
| `sub_C3BCD0` | 19 KB | MercExpand::expandControlFlow | MEDIUM |
| `sub_9D76D0` | 18 KB | Memory instruction legalization (load/store) | HIGH |
| `sub_C3E030` | 18 KB | MercExpand::finalizeExpansion | MEDIUM |
| `sub_9D80E0` | 17 KB | Memory instruction legalization (variant) | HIGH |
| `sub_9E8B20` | 17 KB | Texture/surface lowering | MEDIUM |
| `sub_C37A10` | 16 KB | MercExpand::expandInstruction (jump table) | HIGH |
| `sub_B128E0`--`sub_B12920` | 13 B x5 + 15 KB shared | Architecture dispatch thunks (5 SM families) | HIGH |
| `sub_B1FA20` | 13 KB | SASS 3-operand builder (variant A) | HIGH |
| `sub_B1D670` | 13 KB | Post-ISel instruction modifier | HIGH |
| `def_C37B2E` | 13 KB | MercExpand complex cases (`"EXPANDING"`) | HIGH |
| `sub_B157E0` | 12 KB | Extended modifier dispatcher (37 callees) | HIGH |
| `sub_B20E00` | 11 KB | SASS 3-operand builder (variant B) | HIGH |
| `sub_C39B40` | 10 KB | MercExpand::expandMemoryOp | MEDIUM |
| `sub_9DA100` | 9 KB | Arithmetic operation handler (case 6) | HIGH |
| `sub_B285D0` | 9 KB | ISel lowering driver (66 callees) | HIGH |
| `sub_B241A0` | 7 KB | SASS instruction property setter | HIGH |
| `sub_9F3340` | 7 KB | MercConverter orchestrator (`"After MercConverter"`) | HIGH |
| `sub_C3A460` | 6 KB | MercExpand::expandAtomicOp | MEDIUM |
| `sub_B13E10` | 6 KB | Basic modifier dispatcher (21 callees) | HIGH |
| `sub_B0AA70` | 5 KB | Opcode variant selector (class 306) | HIGH |
| `sub_9DA5C0` | 2 KB | Opcode class 1 handler | MEDIUM |
| `sub_13AF3D0` | 137 KB | SM120 ISel pattern coordinator (130-case switch, 83x `sub_13A6280`, opcodes 2--352) | HIGH |
| `sub_A29220` | ~17 KB | SM120 instruction iterator (calls `sub_13AF3D0` per instruction) | HIGH |
| `sub_13A6280` | ~10 KB | Operand emitter (type-tag dispatch, register class 6 fast-path) | HIGH |
| `sub_13A4DA0` | ~7 KB | Commutative operand position selector (4-entry pattern table) | HIGH |
| `sub_13A7410` | -- | Destination operand emitter (with register class 6 check) | MEDIUM |
| `sub_13A6AE0` | -- | Pre-dispatch source emitter (predicated instruction operands) | MEDIUM |
| `sub_13A6F90` | -- | Extended destination emitter (3rd variant, class 6 check) | MEDIUM |
| `sub_13A6790` | -- | Fenced memory operand emitter | MEDIUM |
| `sub_13A45E0` | -- | Extra operand emitter (wide instruction operands 6/7) | MEDIUM |
| `sub_13A5ED0` | -- | Modifier flag emitter (operands with 0x18000000 bits) | MEDIUM |
| `sub_13A48E0` | -- | Operand property extractor (for `sub_13A4DA0` matching) | MEDIUM |
| `sub_10AE5C0` | tiny | DAGNode_ReadField (field_id to value, delegates to `sub_10D5E60`) | VERY HIGH |
| `sub_10AE590` | tiny | DAGNode_WriteField (single field write) | VERY HIGH |
| `sub_10AE640` | tiny | DAGNode_WriteFields (multi-field update) | VERY HIGH |
| `sub_B28F30` | tiny | GetOperand (index into 32-byte operand array at `*(node+32)`) | VERY HIGH |
| `sub_B28F40` | tiny | GetResultCount (`node[40] + 1 - node[92]`) | VERY HIGH |
| `sub_B28F50` | tiny | GetSourceCount (`*(node+92)`) | VERY HIGH |
| `sub_B28E00` | tiny | DecodeRegClass (identity function, class is plain int) | VERY HIGH |
| `sub_B28E10` | tiny | isGPR operand predicate (`tag == 2`) | VERY HIGH |
| `sub_B28E20` | tiny | isImmediate operand predicate (`tag == 1`) | VERY HIGH |
| `sub_B28E40` | tiny | isValidReg operand predicate (`tag == 10`) | VERY HIGH |
| `sub_B28E80` | tiny | isPredicate operand predicate (`tag == 3`) | VERY HIGH |
| `sub_B28E90` | tiny | isUniformReg operand predicate (`tag == 15`) | VERY HIGH |
| `sub_B28F60`--`sub_B74C60` | ~1.3 MB | ~801 DAG pattern matchers (priority 2--34, template 1--152) | HIGH |
| `sub_C01840` | -- | Mega-selector source operand marshaller (52 calls from mega-selector) | HIGH |
| `sub_C01F50` | -- | Mega-selector destination operand marshaller | HIGH |
| `sub_C00EA0` | -- | Single operand extractor (returns tagged operand word) | HIGH |
| `sub_BFFD60` | -- | Operand reference resolver (register ref to encoding word) | HIGH |
| `sub_C06E90` | -- | Symbol/special-register lookup for shared memory | HIGH |
| `sub_C07690` | -- | Immediate-operand encoding helper | MEDIUM |
| `sub_C0B2C0` | -- | Extended memory/warp operation encoder | HIGH |
| `sub_C05CC0` | -- | Immediate operation encoder (flag-dependent path) | MEDIUM |
| `sub_BFEBF0` | tiny | Default vtable[2] stub (opcode translator, no-op identity) | VERY HIGH |
| `sub_BFEAA0` | tiny | Default vtable[12] stub (capability check, always false) | VERY HIGH |
| `sub_BFEA30` | tiny | Default vtable[3] stub (extension handler, no-op) | VERY HIGH |
| `sub_BFEF10` | -- | Register bank capacity check / grow | MEDIUM |
| `word_22B4B60` | -- | Static opcode-to-encoding-index table (`uint16[222]`, default backend) | VERY HIGH |
| `sub_C3F490` | 184 B | OCG encoding template lookup (routing value + 7 flags -> template ptr) | VERY HIGH |
| `sub_6CB8A0` | -- | OCG SASS instruction emitter (calls `sub_C3F490` then `sub_9314F0`) | HIGH |
| `sub_C41100` | -- | MercExpand memory encoder (calls `sub_C3F490` with full flag set) | HIGH |
| `sub_C40420` | -- | MercExpand memory encoder variant (calls `sub_C3F490`) | HIGH |
| `sub_C40B90` | -- | MercExpand memory encoder variant (calls `sub_C3F490`) | HIGH |
| `sub_C42330` | -- | MercExpand memory encoder variant (calls `sub_C3F490`) | HIGH |
| `unk_22B8960`--`unk_22BB460` | ~11 KB | Operand gathering templates (40+ entries, 256 B each) | HIGH |

## Cross-References

- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) -- Phase 1 context: bridge phases, MercConverter call chain
- [Code Generation Overview](./overview.md) -- ISel within the codegen pipeline
- [SASS Instruction Encoding](./encoding.md) -- bit-level encoding format, operand encoders
- [Mercury Encoder Pipeline](./mercury.md) -- Mercury master encoder, MercExpand
- [Peephole Optimization](./peephole.md) -- post-ISel pattern rewrites (3 mega-dispatchers)
- [Newton-Raphson Templates](./templates.md) -- DDIV/DRCP/DSQRT expansion sequences
- [Intrinsics: OCG Lowering Pipeline](../intrinsics/index.md#ocg-intrinsic-lowering-pipeline----sub_6a97b0--sub_6cc690) -- OCG router that assigns routing values, operand buffer layout
- [Ori IR](../ir/overview.md) -- instruction format, opcode field layout
- [SASS Opcodes](../reference/sass-opcodes.md) -- target instruction set
