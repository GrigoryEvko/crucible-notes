# SM80-88 Ampere

The SM80 ISel backend occupies a contiguous 1 MB region at `0xCA0000`--`0xDA0000` within nvlink's `.text` section. It contains the complete instruction selection, operand emission, bitfield packing, and binary encoding pipeline for Ampere-class GPUs (GA100, GA102, GA104, etc.) covering sm_80, sm_86, sm_87, and sm_88. The sm_88 variant is new in CUDA 13.0 (nvlink v13.0.88).

This page documents the internal structure of the backend, the three-phase compilation pipeline it implements, and the full function catalog derived from the sweep of the address range.

## Address Map

| Range | Size | Subsystem | Functions |
|---|---|---|---|
| `0xCA0000`--`0xCDC000` | 240 KB | Operand emission + bitfield packing | 137 |
| `0xCDD5F0`--`0xCDD690` | <1 KB | Operand type predicates | 15 |
| `0xCE2000`--`0xD5FD70` | 510 KB | ISel pattern matchers | 259 |
| **`0xD5FD70`** | **239 KB** | **ISel mega-hub dispatcher** | **1** |
| `0xD9A400`--`0xDA0000` | 23 KB | Binary instruction encoding | 17 |

Total analyzed functions (>3 KB): 413. The mega-hub at `sub_D5FD70` is 239 KB (55,985 instructions, 1,340 callees) and too large for Hex-Rays to decompile. It is the third-largest function in the binary after the SM75 mega-hub (`sub_FBB810`, 280 KB) and the `cuda_builtin_prototype_generator` (`sub_15B86A0`, 345 KB).

## Three-Phase Pipeline

Every IR instruction processed by this backend passes through three phases in sequence: instruction selection, operand emission, and binary encoding. The output is a 128-bit SASS instruction word ready for insertion into the device ELF `.text` section.

### Phase 1: Instruction Selection

The mega-hub `sub_D5FD70` iterates all 259 pattern matcher functions for the current IR instruction. Each pattern matcher receives `(ctx, ir_node, &pattern_id, &priority)` and tests a single candidate encoding against the IR instruction's attributes and operand types. If all constraints pass and the pattern's priority exceeds the current best, the matcher writes its pattern ID and priority into the output slots. After all matchers run, the mega-hub dispatches to the emitter for the winning pattern.

The ISel protocol:

```
for each pattern_matcher in sm80_pattern_table[0..258]:
    pattern_matcher(ctx, ir_node, &pattern_id, &priority)
    if priority > best_priority:
        best_priority = priority
        best_id = pattern_id
sm80_emitter_table[best_id](ctx, ir_node)
```

Each pattern matcher queries:
- Instruction attributes via `sub_A49150(ctx, ir_node, slot_id)` -- up to 12 attribute checks per pattern
- Definition (output) operand count via `sub_530FD0(ir_node)`
- Use (input) operand count via `sub_530FC0(ir_node)`
- Individual operands via `sub_530FB0(ir_node, index)`
- Operand type predicates: `isGPR` (`sub_CDD600`), `isPredicate` (`sub_CDD610`), `isUniformReg` (`sub_CDD630`), `isImmediate` (`sub_CDD670`), `isConstBuf` (`sub_CDD680`)
- Register file via `sub_CDD5F0(reg_id)`

Priority values range from 14 (least specific, fallback patterns) to 34 (most specific, highly constrained patterns). Higher priority means more attribute checks and narrower operand constraints.

### Phase 2: Operand Emission

The winning pattern dispatches to an operand emission function from Zone 1 (`0xCA0000`--`0xCDC000`). Each emitter handles one (opcode, format) combination and populates a structured instruction descriptor:

1. Store opcode ID at `*(a2+12)` (WORD)
2. Store encoding format at `*(a2+14)` (BYTE)
3. Store maximum operand slot count at `*(a2+15)` (BYTE)
4. Set instruction modifiers via setter functions (rounding mode, data type, negation, saturation, etc.)
5. Decode register operands from the IR instruction's 128-bit packed word at `*(a1+16)` using shift/mask operations
6. Call `emitRegOperand` / `emitPredicateOperand` / `emitAddrOperand` for each operand slot
7. Apply special-case fixups for specific operand ID combinations

The descriptor serves as the intermediate representation between pattern selection and binary encoding. Format IDs observed: 0 (RR), 1 (RI), 2 (RC), 3 (RR.ALT), 4 (RR.P), 5 (RI.P), 6 (RC.P), 7 (SHFL), 8 (RR.3SRC), 9 (RI.P2), 10 (RR.WIDE), 11 (RR.ADD), 13 (TCA), 14 (TCB), 15 (TCC), 17 (TCD), 18 (TCE), 19 (RR.MEM), 23 (TC.ALT), 24 (TC.ALT2), 42 (TC.WIDE1), 43 (TC.WIDE2), 44 (TC.WIDE3), 45 (TC.WIDE4).

### Phase 3: Binary Encoding

Zone 3 functions (`0xD9A400`--`0xDA0000`, 17 functions) take the fully populated instruction descriptor and produce the final 128-bit SASS binary instruction word:

1. Call `setBitfield` (`sub_4C28B0`) to lay down opcode base bits
2. Load instruction template from static tables via SIMD `_mm_loadu_si128`
3. Initialize instruction buffer via `initInsnFromTemplate` (`sub_4C2A90`)
4. Encode register operand slots via `encodeOperandSlot0` (`sub_4C4D60`) and `encodeOperandSlot1` (`sub_4C5C30`)
5. Pack modifier flags into the binary instruction
6. Set scheduling information and dependency barriers

The encoding functions use SSE2 intrinsics (`_mm_or_si128`) for efficient 128-bit bulk operations on the instruction word.

## Instruction Set Coverage

The SM80 backend handles 19 distinct SASS opcodes with a total of 80 (opcode, format) emission variants:

| Opcode ID | Mnemonic | Variants | Description | Max Operands |
|---|---|---|---|---|
| 34 | HMMA | 11 | Tensor Core half-precision matrix multiply-accumulate | 25 |
| 39 | S2R | 2 | Move special register to GPR | 10 |
| 40 | CS2R | 2 | Move control/status special register to GPR | 10 |
| 90 | IMAD | 4 | Integer multiply-add (32-bit) | 19 |
| 127 | FFMA | 12 | FP32 fused multiply-add | 25 |
| 195 | DSETP | 2 | FP64 set predicate (comparison) | 10 |
| 205 | LEA | 1 | Load effective address computation | 19 |
| 230 | IMAD.WIDE | 9 | Integer multiply-add with 64-bit result | 25 |
| 284 | DADD | 1 | FP64 addition | 25 |
| 285 | LDG | 9 | Global memory load | 25 |
| 289 | ISETP | 4 | Integer set predicate | 19/37 |
| 290 | IMNMX | 4 | Integer min/max selection | 19 |
| 292 | FSETP | 2 | FP32 set predicate | 19 |
| 293 | SEL | 4 | Select / conditional move | 19 |
| 294 | SHFL | 1 | Warp shuffle (inter-lane communication) | 3 |
| 295 | FADD | 4 | FP32 addition | 19 |
| 296 | FMUL | 4 | FP32 multiplication | 19 |
| 297 | MUFU | 4 | Multi-function unit (sin/cos/sqrt/rsq/rcp/lg2/ex2) | 19 |
| 299 | HADD2 | 2 | FP16x2 packed addition | 19 |

These 19 opcodes represent the core compute-intensive instructions that the linker's embedded ptxas must emit during LTO compilation. The full Ampere ISA is substantially larger; instructions that never appear in LTO-generated code (control flow, barriers, texture, surface, etc.) are handled by separate codec tables at `0xC00070`--`0xC50970`.

## ISel Pattern Classes

The 259 ISel pattern matchers divide into 9 functional classes, each covering a specific instruction category:

| Class | Patterns | Address Range | Target Opcodes |
|---|---|---|---|
| Integer/comparison | 60 | `0xCE20F0`--`0xCF0040` | ISETP, IMNMX, IMAD |
| Floating-point | 30 | `0xCF0040`--`0xCFA770` | FADD, FMUL, FSETP, DSETP, DADD |
| Memory/load-store | 31 | `0xCFA770`--`0xD07000` | LDG, S2R, CS2R |
| Conversion/special | 17 | `0xD07000`--`0xD0E000` | MUFU, HADD2, SHFL |
| Wide multiply | 14 | `0xD0E000`--`0xD13000` | IMAD.WIDE |
| Fused multiply-add | 40 | `0xD13000`--`0xD39000` | FFMA |
| Complex ALU | 14 | `0xD39000`--`0xD3E000` | LEA, SEL (complex forms) |
| Tensor core | 27 | `0xD3E000`--`0xD52000` | HMMA (all TC formats) |
| Predicate/select | 26 | `0xD52000`--`0xD5FD70` | SEL, predicate combinations |

Priority levels range from 14 to 34. The priority system ensures more specific patterns (e.g., a pattern requiring GPR+Pred+UReg+Imm with 11 attribute checks at priority 31) win over generic fallbacks (e.g., GPR+Imm with 3 attribute checks at priority 14). Within each class, patterns form a lattice from general to specific:

| Priority Range | Attribute Checks | Operand Constraint Level | Example |
|---|---|---|---|
| 14--16 | 2--3 | Lightly constrained | GPR+Imm fallback |
| 17--19 | 4--5 | Standard | GPR+Pred+UReg+Imm |
| 20--23 | 6--7 | Moderately constrained | Multi-operand with specific attributes |
| 24--27 | 8--9 | Heavily constrained | UReg-only with 9 attribute checks |
| 28--34 | 10--12 | Highly constrained | FMA with 12 attribute checks, priority 34 |

## Bitfield Packing Functions

Zone 1 contains 75 bitfield packing functions (embedded within the `0xCA0000`--`0xCDC000` range alongside operand emitters). These translate the instruction descriptor's register IDs, opcode fields, and modifiers into bit positions within the 128-bit SASS instruction word. Each function follows the pattern:

1. Merge fixed opcode template bits via `_mm_or_si128`
2. Translate virtual register IDs to SASS encoding via `encodeRegId` (`sub_A50D10`)
3. Pack register fields at specific bit offsets (11--20 shift-pack operations per function)
4. Handle the zero-register sentinel: register ID 1023 maps to RZ, predicate ID 31 maps to PT (true predicate)
5. Encode source operands (registers, immediates, constant buffer references)
6. Pack modifier fields (rounding, data type, flags) into instruction bits

Packing functions are grouped by the instruction class they serve:

| Class | Functions | Shift-Pack Ops | Notes |
|---|---|---|---|
| FADD/FMUL/MUFU/HADD2/SHFL | 26 | 11--16 | Arithmetic + special function encoding |
| IMAD/FFMA/LEA | 3 | 13--15 | Multiply-add class encoding |
| FFMA | 4 | 15 | Fused multiply-add specific |
| FFMA/DSETP | 12 | 13--16 | FMA and FP64 comparison encoding |
| HMMA (Tensor Core) | 4 | 16 | Tensor core with fixed 16-bitfield layout |
| HMMA/IMAD.WIDE | 10 | 14--20 | Wide operand encoding (most complex) |

The HMMA/IMAD.WIDE class has the most complex packing with up to 20 shift-pack operations per function, reflecting the wide operand formats required by tensor core and 64-bit multiply-add instructions.

## Operand Type Predicates

Fifteen small predicate functions at `0xCDD5F0`--`0xCDD690` classify operands by type. These are called thousands of times by the ISel pattern matchers:

| Address | Identity | Check |
|---|---|---|
| `sub_CDD5F0` | `getRegFile` | Extract register file from register ID |
| `sub_CDD600` | `isGPR` | Operand is a general-purpose register |
| `sub_CDD610` | `isPredicate` | Operand is a predicate register |
| `sub_CDD630` | `isUniformReg` | Operand is a uniform register |
| `sub_CDD670` | `isImmediate` | Operand is an immediate value |
| `sub_CDD680` | `isConstBuf` | Operand is a constant buffer reference |

Operand type combinations observed in ISel patterns: GPR, GPR+Imm, GPR+Pred, GPR+Pred+Imm, GPR+Pred+UReg, GPR+Pred+UReg+Imm, GPR+UReg+Imm, UReg, UReg+Imm. The most common combination is GPR+Pred+UReg+Imm (used by 89 of the 259 patterns).

## Instruction Modifier Setters

The operand emission functions call external setter functions to configure instruction modifiers on the descriptor. These setters are shared across all SM backends:

| Address | Identity | Modifier |
|---|---|---|
| `sub_509100` | `setDnzMode` | Denormalized-number-as-zero mode |
| `sub_509160` | `setRoundingMode` | IEEE rounding mode (RN, RZ, RP, RM) |
| `sub_509760` | `setAbsolute` | Absolute value modifier |
| `sub_509890` | `setEvictFirst` | Cache eviction hint |
| `sub_509950` | `setCacheLevel` | Cache level targeting |
| `sub_509B00` | `setScope` | Memory scope (CTA, GPU, SYS) |
| `sub_509B20` | `setEviction` | Eviction policy |
| `sub_50AC80` | `setCacheOp` | Cache operation type |
| `sub_50ACD0` | `setMemoryType` | Memory type qualifier |
| `sub_50B160` | `setComparison` | Comparison predicate (LT, GT, EQ, NE, ...) |
| `sub_50B300` | `setNegation` | Source operand negation |
| `sub_50B500` | `setDataType` | Data type (F32, F16, S32, U32, ...) |
| `sub_50B900` | `setStrongOrder` | Strong memory ordering |
| `sub_50BD00` | `setSaturation` | Output saturation clamp |
| `sub_50BDA0` | `setAddrSpace` | Address space (global, shared, local) |
| `sub_50C060` | `setFtzMode` | Flush-to-zero for denormals |

Not every instruction uses every modifier. The modifier set per instruction family:

| Instruction | Key Modifiers |
|---|---|
| FFMA | rounding, evictFirst, cacheOp, memoryType |
| FADD | rounding, absolute, negation, dataType, saturation |
| FMUL | dnzMode, rounding, absolute, negation, dataType |
| MUFU | rounding, absolute, comparison, negation, dataType |
| LDG | rounding, evictFirst, cacheOp, memoryType, strongOrder |
| IMAD.WIDE | rounding, evictFirst, cacheOp, memoryType, addrSpace |
| ISETP/IMNMX | rounding, evictFirst, scope, eviction, cacheOp |
| HADD2 | rounding, absolute, negation, dataType, saturation |

## Emission Function Catalog

The following table lists all 80 operand emission functions identified in Zone 1, organized by opcode. Each row is one (opcode, format) combination.

### HMMA (Tensor Core) -- 11 variants

| Address | Identity | Format | Size | Notes |
|---|---|---|---|---|
| `sub_CCE930` | sm80_emit_HMMA_TC.ALT | 23 | 3,185 B | 3-operand compact form |
| `sub_CCD8E0` | sm80_emit_HMMA_TC.ALT2 | 24 | 3,134 B | 3-operand compact form |
| `sub_CCECD0` | sm80_emit_HMMA_TCB | 14 | 3,151 B | 25-operand |
| `sub_CCF070` | sm80_emit_HMMA_TCA | 13 | 3,202 B | 25-operand |
| `sub_CD12D0` | sm80_emit_HMMA_TCC | 15 | 3,227 B | 25-operand |
| `sub_CD0E40` | sm80_emit_HMMA_TCD | 17 | 3,211 B | 25-operand |
| `sub_CD0230` | sm80_emit_HMMA_TCE | 18 | 3,160 B | 25-operand |
| `sub_CD6740` | sm80_emit_HMMA_TC.WIDE2 | 43 | 11,127 B | Complex multi-variant with fixup tables |
| `sub_CD7310` | sm80_emit_HMMA_TC.WIDE4 | 45 | 11,127 B | Complex multi-variant with fixup tables |
| `sub_CD7EE0` | sm80_emit_HMMA_TC.WIDE1 | 42 | 11,188 B | Complex multi-variant with fixup tables |
| `sub_CD8AC0` | sm80_emit_HMMA_TC.WIDE3 | 44 | 11,204 B | Complex multi-variant with fixup tables |

### FFMA -- 12 variants

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CC7380` | sm80_emit_FFMA_RR | 0 | 4,102 B |
| `sub_CC4F80` | sm80_emit_FFMA_RI | 1 | 3,866 B |
| `sub_CC7880` | sm80_emit_FFMA_RC | 2 | 4,384 B |
| `sub_CC58D0` | sm80_emit_FFMA_RR.ALT | 3 | 4,148 B |
| `sub_CAAFE0` | sm80_emit_FFMA_RR.P | 4 | 4,603 B |
| `sub_CC7D20` | sm80_emit_FFMA_RI.P | 5 | 4,397 B |
| `sub_CC8990` | sm80_emit_FFMA_RC.P | 6 | 4,413 B |
| `sub_CC4230` | sm80_emit_FFMA_SHFL | 7 | 3,669 B |
| `sub_CC3500` | sm80_emit_FFMA_RR.3SRC | 8 | 3,433 B |
| `sub_CC6B60` | sm80_emit_FFMA_RI.P2 | 9 | 3,888 B |
| `sub_CC4AF0` | sm80_emit_FFMA_RR.WIDE | 10 | 3,685 B |
| `sub_CC5440` | sm80_emit_FFMA_RR.ADD | 11 | 3,701 B |

### LDG (Global Memory Load) -- 9 variants

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CD5BE0` | sm80_emit_LDG_RR | 0 | 11,464 B |
| `sub_CD4520` | sm80_emit_LDG_RI | 1 | 11,399 B |
| `sub_CD5080` | sm80_emit_LDG_RC | 2 | 11,448 B |
| `sub_CD39E0` | sm80_emit_LDG_RR.ALT | 3 | 11,356 B |
| `sub_CBA5D0` | sm80_emit_LDG_RR.P | 4 | 3,480 B |
| `sub_CBA210` | sm80_emit_LDG_RI.P | 5 | 3,245 B |
| `sub_CBADD0` | sm80_emit_LDG_RC.P | 6 | 3,492 B |
| `sub_CBC350` | sm80_emit_LDG_SHFL | 7 | 3,680 B |
| `sub_CBA9D0` | sm80_emit_LDG_RR.3SRC | 8 | 3,476 B |

The RR/RI/RC/RR.ALT base forms are substantially larger (~11 KB each) than the predicated (.P) and shuffle forms (~3.5 KB), reflecting the complex cache hierarchy modifiers (`strongOrder`, `eviction`, `scope`, `cacheOp`, `memoryType`) that the base forms must encode.

### IMAD.WIDE (64-bit Multiply-Add) -- 9 variants

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CDBBD0` | sm80_emit_IMAD.WIDE_RR | 0 | 12,692 B |
| `sub_CDA300` | sm80_emit_IMAD.WIDE_RI | 1 | 12,627 B |
| `sub_CDAF60` | sm80_emit_IMAD.WIDE_RC | 2 | 12,676 B |
| `sub_CD96B0` | sm80_emit_IMAD.WIDE_RR.ALT | 3 | 12,577 B |
| `sub_CD1C80` | sm80_emit_IMAD.WIDE_RR.P | 4 | 4,874 B |
| `sub_CD1770` | sm80_emit_IMAD.WIDE_RI.P | 5 | 4,638 B |
| `sub_CD2730` | sm80_emit_IMAD.WIDE_RC.P | 6 | 4,889 B |
| `sub_CD2C90` | sm80_emit_IMAD.WIDE_SHFL | 7 | 5,078 B |
| `sub_CD21D0` | sm80_emit_IMAD.WIDE_RR.3SRC | 8 | 4,873 B |

IMAD.WIDE has the largest base-form emitters at ~12.7 KB each, exceeding even LDG. The wide result format requires handling both the low and high 32-bit halves of the 64-bit product, with separate register pair allocation for the destination.

### Remaining Opcodes

| Address | Identity | Format | Size |
|---|---|---|---|
| `sub_CCA5D0` | sm80_emit_IMAD_RR | 0 | 4,835 B |
| `sub_CC2A20` | sm80_emit_IMAD_RC | 2 | 3,583 B |
| `sub_CBF4A0` | sm80_emit_IMAD_RR.P | 4 | 3,798 B |
| `sub_CC3D20` | sm80_emit_IMAD_RR.3SRC | 8 | 4,215 B |
| `sub_CABB10` | sm80_emit_S2R_TCC | 15 | 3,213 B |
| `sub_CABEA0` | sm80_emit_S2R_RR.MEM | 19 | 3,216 B |
| `sub_CAC230` | sm80_emit_CS2R_TCC | 15 | 3,314 B |
| `sub_CAB750` | sm80_emit_CS2R_RR.MEM | 19 | 3,317 B |
| `sub_CC9C30` | sm80_emit_DSETP_RI | 1 | 5,292 B |
| `sub_CC5D30` | sm80_emit_DSETP_RR.ALT | 3 | 4,903 B |
| `sub_CAE310` | sm80_emit_LEA_RR | 0 | 5,246 B |
| `sub_CBED30` | sm80_emit_DADD_RR.ADD | 11 | 3,153 B |
| `sub_CC46A0` | sm80_emit_ISETP_RR | 0 | 3,741 B |
| `sub_CC6260` | sm80_emit_ISETP_RI | 1 | 3,785 B |
| `sub_CC66E0` | sm80_emit_ISETP_RC | 2 | 3,917 B |
| `sub_CC84F0` | sm80_emit_ISETP_RR.ALT | 3 | 3,961 B |
| `sub_CB96B0` | sm80_emit_IMNMX_RR | 0 | 3,570 B |
| `sub_CB9AD0` | sm80_emit_IMNMX_RI | 1 | 3,614 B |
| `sub_CB8E80` | sm80_emit_IMNMX_RC | 2 | 3,467 B |
| `sub_CB9280` | sm80_emit_IMNMX_RR.ALT | 3 | 3,511 B |
| `sub_CC2660` | sm80_emit_FSETP_RR | 0 | 3,331 B |
| `sub_CC3930` | sm80_emit_FSETP_RI | 1 | 3,376 B |
| `sub_CC22A0` | sm80_emit_SEL_RR | 0 | 3,316 B |
| `sub_CC2EA0` | sm80_emit_SEL_RI | 1 | 3,361 B |
| `sub_CBF0F0` | sm80_emit_SEL_RC | 2 | 3,213 B |
| `sub_CC1ED0` | sm80_emit_SEL_RR.ALT | 3 | 3,258 B |
| `sub_CA02C0` | sm80_emit_SHFL_SHFL | 7 | 4,076 B |
| `sub_CB7B20` | sm80_emit_FADD_RR | 0 | 7,415 B |
| `sub_CBF900` | sm80_emit_FADD_RI | 1 | 27,061 B |
| `sub_CA08C0` | sm80_emit_FADD_RC | 2 | 44,490 B |
| `sub_CB71C0` | sm80_emit_FADD_RR.ALT | 3 | 6,705 B |
| `sub_CB60E0` | sm80_emit_FMUL_RR | 0 | 5,715 B |
| `sub_CBB1E0` | sm80_emit_FMUL_RI | 1 | 11,561 B |
| `sub_CBC940` | sm80_emit_FMUL_RC | 2 | 14,951 B |
| `sub_CB4BE0` | sm80_emit_FMUL_RR.ALT | 3 | 5,164 B |
| `sub_CB4390` | sm80_emit_MUFU_RR | 0 | 4,743 B |
| `sub_CB5A10` | sm80_emit_MUFU_RI | 1 | 5,377 B |
| `sub_CB8410` | sm80_emit_MUFU_RC | 2 | 8,046 B |
| `sub_CB3E20` | sm80_emit_MUFU_RR.ALT | 3 | 4,529 B |
| `sub_CB5380` | sm80_emit_HADD2_RR | 0 | 5,434 B |
| `sub_CB67F0` | sm80_emit_HADD2_RI | 1 | 5,648 B |

Notable size outliers: `sm80_emit_FADD_RC` at 44,490 bytes (1,415 lines) is the single largest emission function, followed by `sm80_emit_FADD_RI` at 27,061 bytes (865 lines). Both require extensive fixup tables for the constant-buffer and immediate operand forms of FP32 addition with negation, absolute value, saturation, and data type modifiers.

## External Dependencies

The SM80 backend calls into shared infrastructure functions across the binary:

### IR Accessors

| Address | Identity | Callers | Role |
|---|---|---|---|
| `sub_530FB0` | `getOperand(idx)` | 31,399 | Universal operand accessor |
| `sub_530FC0` | `getNumUses()` | -- | Count use (input) operands |
| `sub_530FD0` | `getNumDefs()` | -- | Count definition (output) operands |
| `sub_A49150` | `getInsnAttribute` | 30,768 | Query instruction attribute by slot ID |
| `sub_A49720` | `hasSpecialAttribute` | -- | Check for special encoding flag |

### Operand Emitters

| Address | Identity | Role |
|---|---|---|
| `sub_4FF010` | `emitRegOperand` | Emit register operand to descriptor |
| `sub_4FF150` | `emitPredicateOperand` | Emit predicate operand to descriptor |
| `sub_4FF280` | `emitAddrOperand` | Emit address/memory operand to descriptor |
| `sub_50C790` | `getReuse` | Get register reuse flag for scheduling |

### Encoding Primitives

| Address | Identity | Role |
|---|---|---|
| `sub_4C28B0` | `setBitfield` | Core primitive: pack value into 128-bit instruction at bit offset |
| `sub_4C2A90` | `initInsnFromTemplate` | Initialize instruction buffer from static template |
| `sub_4C4D60` | `encodeOperandSlot0` | Encode register into operand slot 0 |
| `sub_4C5C30` | `encodeOperandSlot1` | Encode register into operand slot 1 |
| `sub_A50D10` | `encodeRegId` | Translate virtual register ID to SASS encoding |
| `sub_4FEFF0` | `resolveAddress` | Resolve address operand from raw bits |

### Lookup Tables

| Address | Identity | Role |
|---|---|---|
| `sub_403BDB` | `bitmaskLookup_fuzzy` | Fuzzy bitmask table lookup |
| `sub_403C5C` | `bitmaskLookup_exact` | Exact bitmask table lookup |

## SM Variant Support

The SM80 backend covers four Ampere-class compute capabilities:

| SM Version | Architecture | GPUs | Notes |
|---|---|---|---|
| sm_80 | GA100 | A100, A30 | Data center, first Ampere |
| sm_86 | GA102/GA104/GA106/GA107 | RTX 3090/3080/3070/3060, A40, A16 | Consumer and enterprise |
| sm_87 | GA10B | Jetson Orin (AGX, NX, Nano) | Embedded/automotive |
| sm_88 | -- | -- | New in CUDA 13.0 (nvlink v13.0.88) |

All four variants share the same ISel backend, emission functions, and encoding pipeline. Variant-specific behavior is handled upstream in the SM dispatch tables (`sub_15C0CE0`) via per-SM callback registration, not within this backend's code. The backend's pattern matchers and emitters are variant-agnostic -- they produce identical SASS encoding for all sm_8x targets, with differences (if any) resolved at the callback level before ISel runs.

The sm_88 addition in CUDA 13.0 is notable because it was not present in earlier toolkit releases. Its callback registration follows the same pattern as sm_86/87, suggesting minimal ISA divergence from the base sm_80 encoding.

## Relationship to Other Backends

The SM80 backend at 1 MB is smaller than the SM75 (Turing) backend at 984 KB and substantially smaller than the SM89/90 (Ada/Hopper) backend at 1.9 MB. Size comparison of mega-hubs:

| Backend | Mega-Hub | Size | Pattern Matchers |
|---|---|---|---|
| SM50-7x (shared) | `sub_126CA30` | 239 KB | ~160 |
| SM75 (Turing) | `sub_FBB810` | 280 KB | 276 |
| **SM80 (Ampere)** | **`sub_D5FD70`** | **239 KB** | **259** |
| SM89/90 (Ada/Hopper) | `sub_119BF40` | 231 KB | ~160 |

Despite having fewer ISel patterns than SM75, the SM80 mega-hub matches the SM50-7x hub at 239 KB. The larger pattern count in SM75 (276 vs 259) correlates with its larger hub size (280 KB), confirming the linear relationship between pattern count and hub size.

## Encoding Format Summary

The format field at `*(a2+14)` selects the operand encoding layout. Formats observed across all 19 opcodes:

| Format ID | Name | Description |
|---|---|---|
| 0 | RR | Register-Register (both sources in GPRs) |
| 1 | RI | Register-Immediate (one immediate source) |
| 2 | RC | Register-ConstantBuffer (one source from constant memory) |
| 3 | RR.ALT | Register-Register alternate encoding |
| 4 | RR.P | Register-Register with predicate output |
| 5 | RI.P | Register-Immediate with predicate output |
| 6 | RC.P | Register-ConstantBuffer with predicate output |
| 7 | SHFL | Warp shuffle encoding |
| 8 | RR.3SRC | Register-Register with 3 register sources |
| 9 | RI.P2 | Register-Immediate with dual predicate output |
| 10 | RR.WIDE | Register-Register with wide (64-bit) result |
| 11 | RR.ADD | Register-Register addition-specific encoding |
| 13--18 | TCA--TCE | Tensor Core formats A through E |
| 19 | RR.MEM | Register-Register memory-mapped encoding |
| 23--24 | TC.ALT/TC.ALT2 | Tensor Core alternate compact formats |
| 42--45 | TC.WIDE1--4 | Tensor Core wide formats 1 through 4 |
