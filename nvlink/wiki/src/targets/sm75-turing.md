# SM75 Turing

The SM75 (Turing, compute capability 7.5) instruction selection backend occupies 984 KB at `0xF16000`--`0x100C000` and is the largest single-architecture ISel backend in the nvlink v13.0.88 binary. It contains 1,737 functions organized into four functional layers — operand predicates, instruction emitters, pattern matchers, and post-ISel emit+encode functions — plus a 280 KB mega-hub dispatch function (`sub_FBB810`) that is the largest function in the entire binary at 65,999 instructions.

Turing is architecturally significant as the first SM generation to introduce the Uniform Register File (URF), which manifests throughout this backend as operand kind 10 (`UREG`). The ISel uses a priority-based linear scan: for each IR instruction, all 276 pattern matchers run in sequence, and the highest-priority match wins.

## Key Facts

| Property | Value |
|---|---|
| Address range | `0xF16000`--`0x100C000` (984 KB) |
| Total functions | ~1,737 |
| Mega-hub dispatch | `sub_FBB810` at `0xFBB810` (280 KB, 65,999 instructions, 1,733 callees) |
| Operand predicates | 15 functions at `0xF16030`--`0xF160F0` |
| Instruction emitters | 18 functions at `0xF10080`--`0xF15A50` |
| Pattern matchers | 276 functions at `0xF16150`--`0xFBB780` |
| Emit+encode functions | 38 functions at `0xFFFDF0`--`0x100BBF0` |
| Encoding context size | 576+ bytes |
| ISel architecture | Priority-based linear scan (not tree-pattern or DAG) |

## Address Map

| Range | Size | Subsystem | Count |
|---|---|---|---|
| `0xF16030`--`0xF160F0` | <1 KB | Operand predicate functions | 15 |
| `0xF10080`--`0xF15A50` | 22 KB | Instruction emitters | 18 |
| `0xF16150`--`0xFBB780` | 678 KB | ISel pattern matchers | 276 |
| `0xFBB810` | 280 KB | **Mega-hub dispatch** (`sub_FBB810`) | 1 |
| `0xFFFDF0`--`0x100BBF0` | 48 KB | Post-ISel emit+encode | 38 |

## Instruction Selection Flow

For each NVVM IR instruction to be lowered to SM75 machine code, `sub_FBB810` executes the following protocol:

```text
1. sub_FBB810 iterates through all 276+ pattern matchers
2. Each matcher calls sub_A49150(ctx, node, field_id) to read instruction attributes
3. Each matcher calls sub_530FD0(node) to check explicit operand count
4. Each matcher calls sub_530FB0(node, idx) to retrieve operand at index
5. Each matcher calls sub_530FC0(node) to check implicit operand count
6. Operand type checked via sub_F16040/F16070/etc predicates (kind tag)
7. Register class validated via sub_F16030: value 1023 = "any" (wildcard)
8. If all checks pass: *priority_out = priority, *pattern_id_out = id
9. After all matchers run, mega-hub picks highest-priority match
10. Corresponding emitter called to generate 128-bit encoding
```

The priority mechanism ensures specific patterns override general ones. Higher values win. If the current best priority already exceeds a matcher's threshold, that matcher early-outs (optimization to avoid redundant checks). Priority ranges across the 276 matchers:

| Priority Range | Meaning | Example |
|---|---|---|
| 2–4 | Fallback/default patterns (minimal constraints) | `sub_FBB780` (pattern 1, priority 2): matches any instruction with 0 explicit ops and 2 implicit uniform-register ops |
| 7–10 | Simple patterns (few attribute checks) | NOP/barrier variants, basic shifts |
| 14–19 | Standard patterns (moderate constraints) | IADD3, I2I, MUFU, ISETP, texture fetch, surface load |
| 22–24 | Complex patterns (many attribute + operand checks) | Memory indexed 3-op, branch with predication |
| 33–36 | Very specific patterns (maximum constraints) | SHFL/VOTE with 8 mixed operands, STG with 7 uniform-reg operands |
| 39 | Most specific (HMMA widest variants) | `sub_F77140` (9 implicit operands, R128 tensor core ops) |

## Operand Predicates

Fifteen trivial inline functions at `0xF16030`--`0xF160F0` classify operand types by a single-byte tag at operand offset +0. These are the leaves of every pattern-match tree.

| Address | Function | Test | Operand Kind | Confidence |
|---|---|---|---|---|
| `0xF16030` | `sm75_get_regclass_id` | `return a1` | Identity (passthrough) | HIGH |
| `0xF16040` | `sm75_is_register_operand` | `a1 == 2` | REG — general register | HIGH |
| `0xF16050` | `sm75_is_immediate_operand` | `a1 == 1` | IMM — immediate/literal value | HIGH |
| `0xF16060` | `sm75_is_memory_operand` | `a1 == 6` | Memory/address operand | MEDIUM |
| `0xF16070` | `sm75_is_uniform_register` | `a1 == 10` | **UREG — uniform register (Turing+)** | MEDIUM |
| `0xF16080` | `sm75_is_predicate_operand` | `a1 == 9` | PRED — predicate register | MEDIUM |
| `0xF16090` | `sm75_is_cbuf_operand` | `a1 == 5` | Constant buffer reference | LOW |
| `0xF160A0` | `sm75_is_texture_operand` | `a1 == 4` | Texture/sampler reference | LOW |
| `0xF160B0` | `sm75_is_true_predicate` | `a1 == 3` | PT — always-true guard | MEDIUM |
| `0xF160C0` | `sm75_is_false_predicate` | `a1 == 15` | PN — always-false guard | MEDIUM |
| `0xF160D0` | `sm75_is_kind_13` | `a1 == 13` | Unknown | LOW |
| `0xF160E0` | `sm75_is_kind_14` | `a1 == 14` | Unknown | LOW |
| `0xF160F0` | `sm75_is_kind_16` | `a1 == 16` | Unknown | LOW |
| `0xF16100` | `sm75_is_kind_7` | `a1 == 7` | Unknown | LOW |
| `0xF16110` | `sm75_is_kind_11` | `a1 == 11` | Unknown | LOW |

The identity function at `0xF16030` is used as both a register-class accessor (return value compared against 1023/1/2/4/5) and a generic field value passthrough. A second identity function at `0xF16130` has a different type signature in the original source (both compile to identical machine code, but they occupy distinct vtable slots).

The predicate pair `sub_F160B0` / `sub_F160C0` is always called as `sub_F160B0(v) || sub_F160C0(v)` — accepting either PT (always true, kind 3) or PN (always false, kind 15), matching the SASS convention where a predicate guard can be either polarity.

### Operand Kind Tag Summary

| Type | Symbol | Meaning | Predicate Address |
|---|---|---|---|
| 1 | IMM | Immediate / constant value | `0xF16050` |
| 2 | REG | General register operand | `0xF16040` |
| 3 | PT | Predicate true (always-true guard) | `0xF160B0` |
| 4 | — | Texture / sampler reference | `0xF160A0` |
| 5 | — | Constant buffer reference | `0xF16090` |
| 6 | — | Memory / address operand | `0xF16060` |
| 9 | PRED | Predicate register operand | `0xF16080` |
| 10 | **UREG** | **Uniform register (Turing+)** | `0xF16070` |
| 15 | PN | Predicate false (always-false guard) | `0xF160C0` |

Kind 10 (`UREG`) is the defining Turing addition. It reflects the Uniform Register File introduced in SM75, which provides scalar registers shared across all threads in a warp. This operand kind appears pervasively in the pattern matchers, often alongside kind 2 (REG) as alternatives in the same operand slot.

### Register Class IDs

| ID | Symbol | Width | Usage |
|---|---|---|---|
| 1 | R32 / GPR32 | 32-bit | General purpose register |
| 2 | R64 / GPR64 | 64-bit | General purpose register pair |
| 4 | R128 / GPR128 | 128-bit | Register quad (HMMA tensor core) |
| 5 | P | 1-bit | Predicate register |
| 1023 (0x3FF) | ANY | wildcard | Matches any register class ("don't care") |

## Instruction Emitters

Eighteen functions at `0xF10080`--`0xF15A50` implement the "emit" phase of instruction selection. Each takes an emitter context (`a1`, 576+ bytes) and an instruction node (`a2`) and produces the SM75 128-bit instruction encoding. All share a common structure:

```text
Phase 1: Set instruction opcode at a2+12 (e.g., 126, 18, 104)
Phase 2: Load register class descriptor from rodata into a1+8 (SSE load)
Phase 3: Populate 10-slot operand descriptor arrays at a1+24..a1+140
         (register IDs, types, flags -- using SSE memcpy for speed)
Phase 4: Set explicit operand count at a1+144
Phase 5: Bind operands via sub_4C6380/sub_4C60F0/sub_4C6DC0
Phase 6: Encode bitfields into encoding words at a1+544 and a1+552
Phase 7: Set instruction class tag at a1+276
Phase 8: Write branch target / relocation info to instruction node
```

### Identified Emitters

| Address | Size | Identity | Opcode | Operand Bindings | Instruction Class |
|---|---|---|---|---|---|
| `0xF10080` | 4,975 B | `sm75_emit_memop_5src` | 126 (memory) | 5: src reg, gen x3, pred | `0xE000000004` (load) |
| `0xF10620` | 4,969 B | `sm75_emit_memop_6src` | 126 (memory) | 6: as above + extra src | `0xE000000003` (store) |
| `0xF10BE0` | 4,857 B | `sm75_emit_alu_2src_uniform` | 18 (int ALU) | 2: gen(rc=10) + pred | `0x7000000001` |
| `0xF11090` | ~4.8 KB | `sm75_emit_alu_2src_uniform_B` | 18 | 2 | `0x7000000001` |
| `0xF11540` | ~4.8 KB | `sm75_emit_alu_2src_uniform_C` | 18 | 2 | `0x7000000001` |
| `0xF119F0` | ~4.8 KB | `sm75_emit_alu_variant_D` | 18 | — | — |
| `0xF11EE0` | ~4.8 KB | `sm75_emit_alu_variant_E` | 18 | — | — |
| `0xF123D0` | ~4.8 KB | `sm75_emit_alu_variant_F` | 18 | — | — |
| `0xF128E0` | ~4.8 KB | `sm75_emit_memop_variant_G` | 126 | — | — |
| `0xF12DF0` | ~4.8 KB | `sm75_emit_memop_variant_H` | 126 | — | — |
| `0xF13310` | ~4.8 KB | `sm75_emit_variant_I` | — | — | — |
| `0xF13830` | ~4.8 KB | `sm75_emit_variant_J` | — | — | — |
| `0xF13D50` | ~4.8 KB | `sm75_emit_variant_K` | — | — | — |
| `0xF14310` | ~4.8 KB | `sm75_emit_variant_L` | — | — | — |
| `0xF148D0` | ~4.8 KB | `sm75_emit_variant_M` | — | — | — |
| `0xF14E90` | ~4.8 KB | `sm75_emit_variant_N` | — | — | — |
| `0xF15470` | ~4.8 KB | `sm75_emit_variant_O` | — | — | — |
| `0xF15A50` | ~4.8 KB | `sm75_emit_variant_P` | — | — | — |

### Opcode Families

Three instruction opcode numbers are confirmed from `a2+12` assignments:

| Opcode | Family | SASS Instructions |
|---|---|---|
| 18 | Integer ALU | IADD, IMAD, ISCADD, LEA, SHF, BFE, BFI, LOP3, PRMT |
| 104 | FP32 operations | FADD, FMUL, FMAD, FFMA |
| 126 | Memory / load-store | LDG, STG, LDS, STS, LDL, STL |

### Instruction Class Tags

The 5-byte value written at context offset +276 encodes both the instruction family (high nibble) and operand configuration (low word):

| Tag | Meaning |
|---|---|
| `0x7000000001` | Integer ALU, 2-operand form |
| `0xE000000003` | Memory store (opcode 126, 6 sources) |
| `0xE000000004` | Memory load (opcode 126, 5 sources) |

## Pattern Matchers

All 276 pattern matchers at `0xF16150`--`0xFBB780` share the same signature:

```c
char __fastcall sm75_match_XXX(
    void*     ctx,          // a1: ISel context
    void*     instr_node,   // a2: IR instruction node
    uint32_t* pattern_id,   // a3: output -- matched pattern ID
    uint32_t* priority      // a4: in/out -- current best priority
);
```

Each performs a deeply-nested sequence of checks:

1. Check instruction attributes via `sub_A49150(ctx, node, field_id)` — see [field ID dictionary](#field-id-dictionary) below
2. Check explicit operand count via `sub_530FD0(node)`
3. For each explicit operand: validate kind tag and register class
4. Check implicit operand count via `sub_530FC0(node)`
5. For each implicit operand: validate kind tag and register class
6. If all checks pass and `*a4 <= threshold`: set `*a4 = new_priority`, `*a3 = pattern_id`

### Pattern Matcher Categories

The 276 matchers group into 12 functional categories by the instruction families they match:

| Type | Address Range | Count | Representative Patterns |
|---|---|---|---|
| NOP / barrier | `0xF16150`--`0xF163A0` | ~5 | NOP variant (pattern 33, priority 4), control flow simple (42, 8) |
| HMMA (tensor core) | `0xF1C3F0`--`0xF20D10` | ~10 | HMMA f16/f32 (4, 15), HMMA f64 (13, 15), HMMA with UR (9, 15) |
| ALU / arithmetic | `0xF20D10`--`0xF2B2A0` | ~30 | IADD3 reg+imm (2, 17), ALU 2-op variants |
| Memory / load-store | `0xF307E0`--`0xF36A20` | ~15 | Memory indexed 3-op (12, 24), STG indexed 6-op (25, 36) |
| Conversion / cast | `0xF3C0F0`--`0xF437C0` | ~20 | I2I 3-op (57, 17), MUFU/F2F (82, 19), TEX 3-op (121, 19) |
| Predicated ops | `0xF4AA30`--`0xF4FB70` | ~10 | ISETP 3-op (209, 19), texture fetch 3-op (218, 19) |
| Store variants | `0xF58BB0`--`0xF5C120` | ~10 | STG with predicate (10, 19), store predicated variants |
| Surface / texture | `0xF6DC60`--`0xF71B60` | ~15 | SULD 4-op predicated (5, 19) with side-effect check |
| Complex HMMA | `0xF76170`--`0xF77DF0` | ~8 | HMMA wide R128 (7, 34), HMMA widest 9-op (8, 39) — largest matchers |
| ALU extended | `0xF82CF0`--`0xF96B40` | ~50 | IMAD predicated 6-op (1, 19), IADD/IMUL/SHF/BFE/BFI/LOP3/PRMT |
| Comparison / SETP | `0xF97CE0`--`0xF9CD30` | ~15 | DSETP 8-op (22, 34), DSETP 9-op+pred (23, 36) |
| Branch / call | `0xFA0310`--`0xFAA4E0` | ~20 | BRA complex predicated (1, 24), call/return variants |
| Final / fallback | `0xFB7A90`--`0xFBB780` | ~5 | SHF 3-op imm (2, 10), **fallback simplest (1, 2)** |

### Complex HMMA Matchers (Largest)

The most complex matchers target Half-precision Matrix Multiply-Accumulate (HMMA) instructions for Turing's tensor cores. These are the largest individual matcher functions (6–8 KB each) because HMMA has the most operands and encoding options:

**`sub_F77140` — HMMA widest variant A** (8,408 bytes, 179 lines):
- Checks field `0x216 == 2717` (HMMA opcode variant)
- Additional checks: fields `0xA1 == 700`, `0xA2` in range 702–703
- 1 explicit operand: register R128
- 9 implicit operands: R128 x3, predicate, R64, R32, UREG R32, PT/PN check
- Sets pattern_id=8, priority=39 (maximum observed)

**`sub_F77DF0` — HMMA widest variant B** (8,401 bytes, 179 lines):
- Checks field `0x21A == 2729` (different HMMA subtype)
- Same 9-implicit-operand structure
- Sets pattern_id=12, priority=39

**`sub_F76DD0` — HMMA wide operand A** (7,226 bytes, 164 lines):
- Checks field `0x216 == 2716`
- 1 explicit R128 + 8 implicit (R128 x3, predicate, R32, UREG x2)
- Sets pattern_id=7, priority=34

### Fallback Matcher

**`sub_FBB780`** (1,108 bytes, 34 lines) is the fallback pattern that matches when nothing else does:
- Zero instruction attribute checks
- Requires: explicit operand count == 0, implicit count == 2, first implicit = uniform register R32
- Sets pattern_id=1, priority=2 (lowest observed)
- Any other matching pattern will override this due to priority 2

## Post-ISel Emit+Encode Functions

Thirty-eight functions at `0xFFFDF0`--`0x100BBF0` combine pattern matching with instruction encoding for complex instructions requiring immediate bitfield packing. They share the emitter signature `__int64 (ctx, instr_node)` and use `sub_4C28B0(ctx, bit_offset, width, value)` extensively to pack individual fields into the 128-bit SASS instruction word.

### Encoding Protocol

```text
1. Pack opcode bits:     sub_4C28B0(a1, 0, 4, 2)   -- 4 bits at offset 0
2. Pack sub-opcode:      sub_4C28B0(a1, 4, 3, 0)   -- 3 bits at offset 4
3. Pack encoding fields from rodata tables
4. Initialize operand binding via sub_4C2A60/sub_4C2A90
5. Extract instruction-specific modifiers via sub_A551C0..sub_A55470
6. Encode modifiers into bit positions via sub_A4xxxx/sub_A50xxx
7. Pack into encoding words at ctx+544 and ctx+552 (shift+OR)
8. Set relocation metadata at ctx+148..ctx+160
```

### Identified Emit+Encode Functions

| Address | Size | Identity | Opcode | Sources |
|---|---|---|---|---|
| `0xFFFDF0` | 6,810 B | `sm75_emit_encode_memop_complex_4src` | 126 | 4 src via `sub_4C4D60` + `sub_4C5C30` |
| `0x1000460` | 6,959 B | `sm75_emit_encode_memop_complex_6src` | 126 | 6 src, two relocation entries |
| `0x1000CD0` | 5,492 B | `sm75_emit_encode_alu_3src_A` | 18 | 3 src |
| `0x10012B0` | 5,493 B | `sm75_emit_encode_alu_3src_B` | 18 | 3 src |
| `0x1001890` | 5,407 B | `sm75_emit_encode_alu_3src_C` | 18 | 3 src |
| `0x1001E10` | 5,460 B | `sm75_emit_encode_alu_3src_D` | 18 | 3 src |
| `0x1002340` | 5,687 B | `sm75_emit_encode_alu_4src_A` | 18 | 4 src |
| `0x10028F0` | 5,688 B | `sm75_emit_encode_alu_4src_B` | 18 | 4 src |
| `0x10088D0` | 5,190 B | `sm75_emit_encode_fp32_4op` | 104 | 4 src, `sub_A51DD0(node) == 1875` check |
| `0x1008DD0` | 5,329 B | `sm75_emit_encode_fp32_4op_B` | 104 | 4 src |
| `0x10092F0` | 5,191 B | `sm75_emit_encode_fp32_4op_C` | 104 | 4 src |
| `0x10097F0` | 5,330 B | `sm75_emit_encode_fp32_4op_D` | 104 | 4 src |
| `0x1009D10` | 5,138 B | `sm75_emit_encode_fp32_4op_E` | 104 | 4 src |
| `0x100A210` | 5,296 B | `sm75_emit_encode_fp32_4op_F` | 104 | 4 src |
| `0x100A730` | 5,435 B | `sm75_emit_encode_fp32_4op_G` | 104 | 4 src |
| `0x100AC70` | 5,297 B | `sm75_emit_encode_fp32_4op_H` | 104 | 4 src |
| `0x100B190` | 5,436 B | `sm75_emit_encode_fp32_4op_I` | 104 | 4 src |
| `0x100B6D0` | 5,297 B | `sm75_emit_encode_fp32_4op_J` | 104 | 4 src |
| `0x100BBF0` | 5,296 B | `sm75_emit_encode_fp32_4op_K` | 104 | 4 src |

The remaining 20 emit+encode functions (`0x1002EA0`--`0x10083A0`) follow the same structure with varying field encoding positions and are labeled as generic variants (I through AA).

Opcode distribution among emit+encode functions: 11 for FP32 (opcode 104), 8 for integer ALU (opcode 18), 2 for memory (opcode 126), 17 for undetermined variants.

## Encoding Context Structure

The 576-byte emitter context is the central data structure threading through all emitter and emit+encode functions. It accumulates the operand bindings and bitfield encodings for one SM75 SASS instruction.

```text
Offset  Size   Field
+0      8      Reserved / vtable pointer
+8      16     XMM register class descriptor (SSE-loaded from rodata)
+12     2      Instruction opcode number (18, 104, or 126)
+16     4      Base bit position for predicate encoding
+24     40     Operand register numbers: 10 x 4-byte slots (indices 0–9)
+64     40     Operand types / constraints: 10 x 4-byte slots
+104    40     Operand flags: 10 x 4-byte slots (0=def, 1-5=use, -1=unused)
+144    4      Explicit operand count
+148    4      Relocation type (first)
+152    4      Relocation bit offset (first)
+156    4      Relocation type (second)
+160    4      Relocation bit offset (second)
+276    8      Instruction class tag (e.g., 0xE000000004)
+404    32     Match/emit dispatch table pointer
+536    8      Pointer to instruction descriptor table
+544    8      Encoding word 0 (64-bit bitfield, low half of 128-bit)
+552    8      Encoding word 1 (64-bit bitfield, high half of 128-bit)
+558    2      Immediate value (16-bit)
+572    4      Branch/offset target
```

The operand descriptor arrays at offsets +24, +64, and +104 are populated with optimized SIMD memcpy (aligned SSE loads/stores copying 4 elements at a time from rodata descriptor tables).

## Rodata Register Class Descriptors

Each instruction family has a 16-byte register class descriptor loaded from rodata into context offset +8 via SSE:

| Rodata Address | Instruction Family | Used By |
|---|---|---|
| `xmmword_1F46E28` | Memory operations (opcode 126) | `sub_F10080`, `sub_F10620`, `sub_FFFDF0`, `sub_1000460` |
| `xmmword_1F466B8` | Integer ALU (opcode 18) | `sub_F10BE0`, `sub_F11090`, `sub_F11540` |
| `xmmword_1F46630` | FP32 operations (opcode 104) | `sub_10088D0`--`sub_100BBF0` |
| `xmmword_1F47268` | Complex memory (emit+encode) | Post-ISel emit+encode functions |

Each descriptor has three parallel arrays of 10 DWORDs defining per-slot operand register IDs, type/constraint descriptors, and flag words. Example for memory operations: `dword_1F46E38[0..9]` (register IDs), `dword_1F46E60[0..9]` (types), `dword_1F46E88[0..9]` (flags).

## External Dependencies

The SM75 backend relies on shared infrastructure functions used across all ISel backends:

### IR Node Accessors

| Function | Signature | Description | Callers |
|---|---|---|---|
| `sub_A49150` | `(ctx, node, field_id) -> value` | Read instruction attribute by field ID | 30,768 (binary-wide) |
| `sub_530FD0` | `(node) -> count` | Get explicit operand count | Universal |
| `sub_530FB0` | `(node, idx) -> operand*` | Get operand at index | 31,399 (binary-wide) |
| `sub_530FC0` | `(node) -> count` | Get implicit operand count | Universal |
| `sub_A49720` | `(node) -> bool` | Check instruction has side effects | Surface load matchers |
| `sub_A51DD0` | `(node) -> class` | Get instruction class / post-condition | FP32 emit+encode |

### Operand Binding Functions

| Function | Description |
|---|---|
| `sub_4C6380(ctx, node, op, off, rc)` | Bind source register operand |
| `sub_4C60F0(ctx, node, op, off, rc)` | Bind general register operand |
| `sub_4C6DC0(ctx, node, op, off, rc)` | Bind predicate register operand |
| `sub_4C5F90(ctx, node)` | Finalize operand binding |
| `sub_4C28B0(ctx, bit, width, val)` | Pack value into encoding bitfield |
| `sub_4C2A60(ctx)` | Initialize encoding |
| `sub_4C2A90(ctx, node, flag)` | Bind primary result |
| `sub_4C4D60(ctx, node, op, off)` | Bind source operand (complex) |
| `sub_4C5C30(ctx, node, op, off)` | Bind special operand |

### Modifier Extraction and Encoding

Modifier fields are extracted from the IR node via `sub_A55xxx` functions and encoded into bit positions via `sub_A4xxxx`/`sub_50xxxx` functions:

| Extractor | Field | Encoder | Width |
|---|---|---|---|
| `sub_A551C0` | Modifier 1 | `sub_A4F970` | 3-bit |
| `sub_A55220` | Modifier 2 | `sub_A4D940` | 2-bit |
| `sub_A55280` | Modifier 3 | `sub_A4DC60` | 2-bit (alt) |
| `sub_A55320` | Modifier 4 | `sub_A4FDE0` | 4-bit |
| `sub_A55340` | Modifier 5 | `sub_A50260` | 2-bit |
| `sub_A55400` | Modifier 6 | `sub_A500E0` | 2-bit |
| `sub_A55450` | Modifier 7 | `sub_A500F0` | 5-bit |
| `sub_A55470` | Modifier 8 | `sub_A4FBC0` | 4-bit |

Additional encoding functions handle specific operand attributes: `sub_509D90` (register source A), `sub_509DB0` (register source B), `sub_509F20` (comparison mode), `sub_509160` (data type/precision), `sub_509290` (rounding mode), `sub_509890` (saturation/clamp), `sub_50AC80` (source negate/abs), `sub_50ACD0` (source modifier composite), `sub_509800` (address mode), `sub_509930` (thread scope), `sub_509A90` (memory order), `sub_50C820` (cache policy), `sub_50B570` (texture mode).

## Field ID Dictionary

Field IDs passed to `sub_A49150` to query instruction attributes. These are the keys used by every pattern matcher to classify instructions:

| Field ID | Hex | Semantic Name | Known Values |
|---|---|---|---|
| 5 | `0x05` | Instruction major class | 12 = memory/special |
| 28 | `0x1C` | Branch/jump type subfield | 123–124 |
| 46 | `0x2E` | Integer comparison mode | 213 |
| 59 | `0x3B` | Warp operation mode | 273–274 |
| 88 | `0x58` | Data type / precision code | 406–408 |
| 89 | `0x59` | Store type | 410–416 |
| 91 | `0x5B` | Address space qualifier | 425–427 |
| 92 | `0x5C` | Memory ordering | 429–430 |
| 105 | `0x69` | ALU function select | 477 |
| 116 | `0x74` | Texture/surface function | 512–513 |
| 123 | `0x7B` | Special function unit selector | 536 = texture/surface |
| 126 | `0x7E` | Cache coherence / eviction policy | 547–548 |
| 136 | `0x88` | Source negate/absolute modifier | 598–599 |
| 161 | `0xA1` | HMMA input precision A | 700 |
| 162 | `0xA2` | HMMA input precision B | 702–703 |
| 190 | `0xBE` | NOP/barrier subtype | 815 |
| 201 | `0xC9` | Control flow subtype | 1109 |
| 203 | `0xCB` | Integer multiply mode | 1113–1119 |
| 207 | `0xCF` | Integer multiply variant | 1150–1158 |
| 211 | `0xD3` | Conversion subtype | 1182 |
| 220 | `0xDC` | Load/store address mode | 1206 |
| 226 | `0xE2` | Matrix layout | 1229 |
| 229 | `0xE5` | Special instruction code | 1238 |
| 242 | `0xF2` | Addressing mode detail | 1281–1282 |
| 253 | `0xFD` | MUFU function select | 1321 |
| 254 | `0xFE` | I2I conversion mode | 1324 |
| 255 | `0xFF` | Texture fetch type | 1327–1328 |
| 265 | `0x109` | Texture opcode variant | 1363/1366 |
| 281 | `0x119` | Warp shuffle type | 1435–1440 |
| 285 | `0x11D` | Warp shuffle mode | 1454–1457 |
| 287 | `0x11F` | Special indexed operation | 1464 |
| 294 | `0x126` | Memory bank selector | 1493 |
| 295 | `0x127` | Surface load type | 1495 |
| 302 | `0x12E` | Set-predicate class | 1525 |
| 329 | `0x149` | Integer addressing mode | 1833–1837 |
| 338 | `0x152` | Source predication mode A | 1871/1873–1874 |
| 339 | `0x153` | HMMA accumulator type | 1877 |
| 341 | `0x155` | Source predication mode B | 1881–1882 |
| 345 | `0x159` | Memory scope / synchronization | 1899–1903 |
| 348 | `0x15C` | Execution model qualifier | 1912–1915 |
| 355 | `0x163` | Data size / vector width | 1943/1947 |
| 356 | `0x164` | Texture data type | 1949 |
| 359 | `0x167` | Surface data type | 1960 |
| 376 | `0x178` | Memory persistence | 2035 |
| 377 | `0x179` | Memory eviction priority | 2037–2041 |
| 379 | `0x17B` | Branch condition type | 2046 |
| 380 | `0x17C` | Branch target type A | 2048–2049 |
| 381 | `0x17D` | Branch target type B | 2052–2053 |
| 382 | `0x17E` | Branch modifier | 2055–2060 |
| 394 | `0x18A` | Convert source type | 2107–2108 |
| 397 | `0x18D` | Destination predication | 2115 |
| 399 | `0x18F` | HMMA sub-operation | 2121 |
| 404 | `0x194` | Comparison extension | 2140–2141 |
| 406 | `0x196` | HMMA configuration | 2146 |
| 407 | `0x197` | Comparison precision | 2148–2151 |
| 409 | `0x199` | Set-predicate comparison | 2155 |
| 413 | `0x19D` | Memory segment | 2167–2168 |
| 423 | `0x1A7` | Source data type | bitmask test |
| 424 | `0x1A8` | Function lookup | bitmask test (739) |
| 429 | `0x1AD` | Memory ordering qualifier | 2253–2257 |
| 430 | `0x1AE` | Source A comparison | 2259–2260 |
| 431 | `0x1AF` | Source A comparison ext | 2262–2263 |
| 465 | `0x1D1` | Source B comparison | 2420–2421 |
| 466 | `0x1D2` | Source B comparison ext | 2423–2424 |
| 468 | `0x1D4` | HMMA step select | 2429–2430 |
| 480 | `0x1E0` | Matrix multiply type | 2480/2482/2485 |
| 484 | `0x1E4` | Set-predicate subclass | 2502 |
| 492 | `0x1EC` | Comparison boolean combine | 2524–2525 |
| 494 | `0x1EE` | Branch target form | 2529–2530 |
| 505 | `0x1F9` | HMMA operand layout A | 2569 |
| 506 | `0x1FA` | HMMA operand layout B | 2571 |
| 508 | `0x1FC` | Shift/funnel type | 2576–2577 |
| 524 | `0x20C` | Branch distance | 2678–2679 |
| 534 | `0x216` | HMMA opcode variant A | 2716–2717 |
| 535 | `0x217` | HMMA source C layout | 2719–2720 |
| 536 | `0x218` | HMMA source D layout | 2722–2723 |
| 538 | `0x21A` | HMMA opcode variant B | 2729 |
| 539 | `0x21B` | HMMA mode X | 2731–2736 |
| 540 | `0x21C` | HMMA mode Y | 2738–2743 |
| 547 | `0x223` | HMMA step A | 2767–2768 |
| 548 | `0x224` | HMMA step B | 2770 |
| 549 | `0x225` | HMMA step C | 2772 |
| 569 | `0x239` | Integer set-predicate type | 2850–2851 |
| 575 | `0x23F` | Memory base addressing | 2870 |
| 576 | `0x240` | Memory indexed addressing | 2872 |
| 583 | `0x247` | Conversion class | 2892 |
| 595 | `0x253` | Store qualifier | 2937–2938 |

## Turing-Specific Design Observations

**Uniform Register File (URF).** SM75 introduced the uniform register file — scalar registers whose value is identical across all threads in a warp. This eliminates redundant per-lane computation for warp-uniform values. In the ISel backend, UREG (kind 10) appears as a first-class operand type alongside REG (kind 2). Many pattern matchers accept either kind in the same operand slot, reflecting that SASS instructions can take operands from either the general or uniform register file.

**HMMA complexity.** The tensor core (HMMA) instruction family drives the most complex patterns in this backend. The R128 register class (ID 4) exists specifically for HMMA, representing four consecutive 32-bit registers that hold matrix fragments. The highest-priority matchers (priority 39) are all HMMA variants, and the largest individual matcher functions (8+ KB) target HMMA.

**Linear scan architecture.** Unlike tree-pattern matchers (as used in LLVM's TableGen-generated ISel), this backend evaluates all patterns sequentially. The 280 KB mega-hub calls each of the 276 matchers in order, collects the highest-priority match, then dispatches to the winning emitter. This is computationally expensive (O(patterns) per instruction) but simple to extend: adding a new pattern requires only inserting a new matcher function into the sequence.

**128-bit instruction encoding.** SM75 uses 128-bit SASS instructions (two 64-bit words at context offsets +544 and +552). The `sub_4C28B0` primitive packs arbitrary-width bit fields at arbitrary positions within these two words. Modifier fields are extracted from the IR node and encoded at precise bit positions, with different emit+encode variants differing only in which bits they set within the 128-bit word.

## Confidence Assessment

| Claim | Confidence | Verification |
|---|---|---|
| ISA class string "Turing" for sm_75 | CONFIRMED | Decompiled `sub_484F50` line 251: `"Turing"`; string in `nvlink_strings.json` at `0x1d409dc` |
| SM75 backend at `0xF16000`--`0x100C000` (984 KB) | HIGH | Address range consistent with decompiled function addresses in the catalog; mega-hub at `sub_FBB810` falls within range |
| Mega-hub `sub_FBB810` at 280 KB, 65,999 instructions | HIGH | Size claim derived from binary analysis; too large for Hex-Rays decompilation consistent with other mega-hubs |
| 276 pattern matchers at `0xF16150`--`0xFBB780` | HIGH | Pattern addresses verified against decompiled function catalog; representative patterns like `sub_FBB780` (fallback) confirmed |
| 15 operand predicates at `0xF16030`--`0xF160F0` | HIGH | Address range and trivial predicate structure consistent with ISel infrastructure |
| Operand kind tags: 1=IMM, 2=REG, 10=UREG, etc. | HIGH | Consistent with shared infrastructure used across SM75/80/89/90 backends |
| Priority-based linear scan ISel architecture | HIGH | Protocol described matches mega-hub structure: iterate all matchers, pick highest priority |
| 18 emitter functions at `0xF10080`--`0xF15A50` | HIGH | Addresses consistent with function catalog |
| Opcode families: 18 (int ALU), 104 (FP32), 126 (memory) | HIGH | Opcode numbers from decompiled emit+encode functions at `*(a2+12)` assignments |
| Register class 1023 = wildcard | HIGH | Consistent across all ISel backends; sentinel value used in operand matching |
| Dispatch table: sm_75 encoding table = `sub_15C3210` | CONFIRMED | Decompiled `sub_15C0CE0` shows sm_75 registration (earlier in file) |
| `__CUDA_ARCH__=750` | CONFIRMED | String at `0x1d409c8`; decompiled `sub_484F50` line 252 |
| 128-bit instruction encoding at ctx+544/+552 | HIGH | Consistent across all SM75+ backends; encoding word offsets documented |
| Field ID dictionary (500+ field IDs) | MEDIUM | Field IDs from pattern matcher analysis; individual values not independently verified but consistent with `sub_A49150` usage |

For general SM75 architecture details, see the [ptxas wiki: Turing/Ampere](../../ptxas/targets/turing-ampere.html) and [cicc wiki: SM70-89](../../cicc/targets/sm70-89.html).

## Cross-References

### nvlink Internal
- [Embedded ptxas: Architecture Overview](../ptxas/overview.md) — full address map including SM75 backend position
- [Instruction Selection Hubs](../ptxas/isel-hubs.md) — the five mega-hub dispatch functions
- [IR Nodes](../ptxas/ir-nodes.md) — IR node structure and accessor functions
- [Architecture Dispatch](../ptxas/arch-dispatch.md) — SM75 vtable registration and callbacks
- [Architecture Profiles](arch-profiles.md) — SM75 profile in the linker database
- [SM80 Ampere](sm80-ampere.md) — successor ISel backend

### Sibling Wikis
- [ptxas: Turing/Ampere](../../ptxas/targets/turing-ampere.html) — standalone ptxas SM75/SM80 target documentation
- [ptxas: ISel](../../ptxas/codegen/isel.html) — standalone ptxas instruction selection
- [cicc: SM70-89](../../cicc/targets/sm70-89.html) — cicc compiler SM75 through SM89 targets
