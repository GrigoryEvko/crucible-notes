# Instruction Selection

Instruction selection in ptxas is a two-phase process that converts PTX virtual ISA operations into concrete SASS machine opcodes. Unlike LLVM, which uses a single SelectionDAG or GlobalISel framework, ptxas distributes instruction selection across two distinct pipeline stages separated by the entire optimization pipeline: **Phase 1** converts PTX opcodes to Ori IR opcodes during initial lowering (phase 5, `ConvertUnsupportedOps`), and **Phase 2** converts Ori IR to final SASS binary forms during code generation (phases 112--122, ISel driver + Mercury encoder). The two phases serve fundamentally different purposes: Phase 1 legalizes the IR so the optimizer can reason about it, while Phase 2 selects the optimal machine encoding for the target architecture after register allocation and scheduling are complete.

| | |
|---|---|
| **Phase 1 location** | Phase 5: `ConvertUnsupportedOps` (PTX opcode to Ori opcode) |
| **Phase 2 location** | Phases 112+: ISel driver + Mercury encoder (Ori to SASS binary) |
| **MercConverter dispatch** | `sub_9ED2D0` (25 KB, master switch on `*(instr+72) & 0xCF` mask) |
| **ISel driver** | `sub_B285D0` (9 KB, 66 callees, vtable entry) |
| **ISel mega-selector** | `sub_C0EB10` (185 KB, 500+ locals, giant switch) |
| **DAG pattern matchers** | ~801 functions at `0xB28F60`--`0xB7D000` (~1.3 MB) |
| **Arch dispatch tables** | 4 copies at `sub_B128E0`--`sub_B12920` (15,049 bytes each) |
| **Mercury master encoder** | `sub_6D9690` (94 KB, instruction type switch) |
| **MercExpand** | `sub_C3CC60` (26 KB, pseudo-instruction expansion) |
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
     |    -> sub_C0EB10 (mega-selector, 185KB)
     |    -> sub_B1FA20 / sub_B20E00 (builder variants)
     |    -> sub_B28F60..sub_B74C60 (~801 DAG pattern matchers)
     |    -> sub_B128E0..sub_B12920 (4 arch dispatch tables)
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

    // Special case: opcode 130 with GPR operand -> clear predication
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
| 130, 169 | `vtable[29]` (+232) | Move/convert group |
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

The single largest function in the ISel range, with 500+ local variables and a giant switch/case over instruction opcodes. It handles the complete IR-to-SASS mapping for complex instructions that cannot be handled by the simpler builder variants. Key evidence:

- String reference: `"__nv_reservedSMEM_offset_0_alias"` -- handles shared memory alias resolution
- Uses SSE2 operations for bulk operand manipulation
- For each instruction type: reads operands, checks encoding constraints, and emits the corresponding SASS instruction or instruction sequence
- Calls into register allocation queries to verify that selected encodings are compatible with the allocated registers

The mega-selector is too large for Hex-Rays to produce clean decompilation. Analysis relies on disassembly, call graph structure, and the smaller functions it invokes.

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

The vtable dispatch occurs at various offsets including `+2600`, `+2616`, `+2656`, and `+2896` (observed in `sub_13AF3D0`, the 137 KB ISel pattern coordinator). The matchers have no static callers -- they appear exclusively through indirect function pointer invocation, which is why the sweep reports them as "no callers in function DB."

For a given instruction, the dispatcher may invoke multiple matchers (one per applicable template variant). Each matcher independently checks its constraints and conditionally updates the priority/template pair. After all candidates have been tried, the dispatcher reads the final `template_id` and uses it to select the SASS encoding template.

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

Inferred semantic groupings for field IDs (from cross-referencing matcher patterns):

| Field range | Likely semantics |
|---|---|
| 5--7 | Opcode class / major instruction group |
| 88 | Sub-operation modifier |
| 105 | Operation variant selector |
| 126 | Data type qualifier (e.g., field 126 in {547,548}) |
| 163 | Addressing mode / operand encoding class |
| 190--211 | Encoding format selectors |
| 220 | Specific encoding property |
| 242 | Width/size qualifier |
| 294 | Generic constraint field |
| 327 | Register format descriptor |
| 345 | Rounding / saturation mode |
| 348 | Precision qualifier |
| 355--429 | Extended instruction properties |
| 397 | Instruction validity flag (value 2115 appears as a near-universal gate) |
| 480 | High opcode range (Blackwell/SM 100+ instructions) |
| 595 | Highest observed field ID |

Field 397 with value 2115 appears in the majority of matchers as a mandatory check, suggesting it encodes a "this instruction is encoding-compatible" or "instruction is valid for ISel" flag.

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
11. field 397 == 2115                       -> validity gate
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
4. field 397 == 2115           -> validity gate
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

### Architecture Dispatch Tables -- 4 Copies at `sub_B128E0`--`sub_B12920`

Four nearly identical functions (15,049 bytes each) provide architecture-variant opcode dispatch. Despite being only 13 binary bytes each (3 instructions -- a thunk into shared code at `0x1C39xxx`), the decompiled output is 79,562 bytes due to the massive shared switch statement they jump into.

Each table contains a switch on `*(a3+12)` (the opcode word field) with 50+ cases, and secondary switches on `*(a3+14)` (opcode sub-field) within certain cases. The return values are SASS encoding slot indices (e.g., 197, 691, 526, 697, 772, 21). The four copies serve different SM architecture families, mapping the same logical opcode to different encoding slots depending on the target.

### Opcode Variant Selectors

Two specialized variant selectors handle the final opcode-to-encoding mapping for specific instruction families:

**`sub_B0BE00`** (19 KB) -- opcode class 194:
- Massive switch on `a2` (100+ cases)
- All cases call `sub_10AE590(ctx, inst, 194, N)` with sequential N values starting from 827
- Pattern: `case K -> sub_10AE590(ctx, inst, 194, 826+K)`
- Maps sub-variant indices to SASS encoding slots for one PTX opcode family

**`sub_B0AA70`** (5 KB) -- opcode class 306:
- Same pattern but with opcode class 306
- Variants numbered 1680--1726 with non-sequential case indices (2, 3, 8, 9, 14, 15, 20, 21, 26, 27, 30, 31, 36, 37, 40, 41, ...)
- The alternating-pair pattern at stride 6 suggests type-width combinations (e.g., F32/pair, F64/pair, S32/pair, ...)

### Instruction Modifier Dispatchers

Two modifier-application functions run after the main ISel selection to set type modifiers, rounding modes, and register width:

**`sub_B13E10`** (5,792 B) -- basic modifier dispatcher:
- All 21 callees are `sub_10AE640` (DAG node modifier)
- Switch on `BYTE1(a7) & 0x1F` for modifier type
- Maps modifier values 1--6 to internal codes 31--35
- Secondary dispatch on `(a7 >> 3)` for register width encoding

**`sub_B157E0`** (11,815 B) -- extended modifier dispatcher:
- All 37 callees are `sub_10AE640`
- Handles texture/surface operations specially (opcode type 18)
- Maps sub-opcodes `(BYTE5(a7) & 0x3F)` to encoding values 54--60

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
| `sub_C0EB10` | 185 KB | ISel mega-selector (500+ locals, giant switch) | HIGH |
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
| `sub_B128E0`--`sub_B12920` | 15 KB x4 | Architecture dispatch tables (4 SM families) | HIGH |
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
| `sub_13AF3D0` | 137 KB | ISel pattern coordinator (vtable dispatch to matchers) | HIGH |
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

## Cross-References

- [PTX-to-Ori Lowering](../pipeline/ptx-to-ori.md) -- Phase 1 context: bridge phases, MercConverter call chain
- [Code Generation Overview](./overview.md) -- ISel within the codegen pipeline
- [SASS Instruction Encoding](./encoding.md) -- bit-level encoding format, operand encoders
- [Mercury Encoder Pipeline](./mercury.md) -- Mercury master encoder, MercExpand
- [Peephole Optimization](./peephole.md) -- post-ISel pattern rewrites (3 mega-dispatchers)
- [Newton-Raphson Templates](./templates.md) -- DDIV/DRCP/DSQRT expansion sequences
- [Ori IR](../ir/overview.md) -- instruction format, opcode field layout
- [SASS Opcodes](../reference/sass-opcodes.md) -- target instruction set
