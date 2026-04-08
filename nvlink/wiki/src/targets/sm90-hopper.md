# SM90 Hopper

SM90 (Hopper, H100/H200) shares its backend implementation with SM89 (Ada Lovelace) inside nvlink v13.0.88. The two architectures use the same instruction selector mega-hub (`sub_119BF40`, 231 KB), the same ~750 shared instruction encoder templates, the same compilation driver, and the same scheduling pipeline. Although all six dispatch table callback slots have distinct function addresses between sm_89 and sm_90, only the backend init slot (A8) produces different behavior: Hopper uses a shared memory resource limit of `0x8000` (32768) vs Ada's `0x7005` (28677), and Hopper's init includes a `--blocks-are-clusters` conditional that Ada lacks entirely. The remaining five slots are functionally identical duplicates. The `sm_90a` variant uses the same function pointers as base sm_90 in all slots -- the "a" suffix enables architecture-specific features at runtime through the feature flag configurator, not through separate code paths. See [SM89 Ada: Ada vs Hopper Differences](sm89-ada.md#ada-vs-hopper-concrete-binary-differences) for the complete catalog. This page documents the SM90-specific instruction codec at `0xA70000`--`0xB80000`, the shared SM89/90 backend driver at `0x1100000`--`0x11EA000`, and the Hopper-specific tensor core (HMMA/WMMA) encoding and decoding infrastructure.

## Architecture Identity

SM90 is identified as ISA class "Hopper" in the dispatch table registered by `sub_15C0CE0`. The seven callback slots for sm_90 and sm_90a point to the same functions:

| Slot | Callback | Role |
|---|---|---|
| 0 | nv.info emitter | Per-kernel EIATTR record generation |
| 1 | Resource usage table | Register/shared-memory accounting |
| 2 | Instruction encoding table | SASS binary encoding initializers |
| 3 | Compute capability array | CC version constants |
| 4 | Perf-stats handler | Performance statistics emission |
| 5 | cpf_optx handler | Compiler pass framework integration |
| 6 | Codegen options | SM-specific optimization knobs |

The `sm_90a` variant is not distinguished at the dispatch-table level. Instead, `sub_1100E50` (the feature flag configurator) tests the parsed SM version number and sets per-feature booleans. For sm_89/90, feature 33 is enabled when the SM internal version equals 29 or 30 and flag 618 (suppress-debug-info) is not set. This corresponds to the HMMA/WMMA tensor core extensions.

## Instruction Format

SM90 uses 128-bit (16-byte) instruction words, consistent with the format introduced in SM80 (Ampere). Every instruction occupies exactly two 64-bit words written to the output buffer at `*(a1+40)`:

```
Bit layout (128-bit SASS instruction word):

Word 0 (bits 0-63):
  [15:18]   Destination register number (8-bit, encoded via sub_A50D10)
  [12:14]   Register bank / sign bit (3-bit, via sub_A50CF0)
  [varies]  First and second source register fields
  [varies]  Opcode template bits (OR'd from xmmword constant)

Word 1 (bits 64-127):
  [varies]  Additional source operands
  [varies]  Modifier fields (rounding mode, saturation, FTZ, data type)
  [varies]  Predicate destination and source fields
```

The opcode template is loaded as a 128-bit SSE constant (`xmmword_1E5B2F0`, `xmmword_1E5B2C0`, etc.) and OR'd into the output buffer via `_mm_or_si128`. Each encoder function uses a unique template constant that establishes the base opcode bits, with operand fields packed on top.

## Register Encoding

SM90 uses the same register sentinel scheme as SM80+:

| Field | Width | Valid Range | Sentinel | Internal Mapping |
|---|---|---|---|---|
| GPR (general purpose) | 8 bits | 0--254 | 255 | Maps to 1023 (= RZ, zero register) |
| Predicate register | 5 bits | 0--6 | 7 | Maps to 31 (= PT, true predicate) |
| Uniform register | 8 bits | 0--62 | 63 | Maps to URZ |

The encoding helper `sub_A50D10(arch, value)` packs a register number into the destination field. `sub_A50CF0(arch, value)` encodes the bank select bit. `sub_A50CD0(arch, value)` encodes a flag bit (negate or absolute-value modifier). The sentinel value 1023 at operand offsets +36, +68, +100, or +132 in the operand array triggers substitution from the architecture context at `a1+8` / `a1+12`, which provides architecture-specific default register values.

## Operand Structure

Each operand is a 32-byte record within an operand array at `*(a2+32)`, indexed by `*(a2+40)`:

```
struct operand {             // 32 bytes
    uint32_t  kind;          // [+0]  operand type (register, immediate, const bank, ...)
    uint32_t  reg_num;       // [+4]  register number (1023 = unused sentinel)
    uint32_t  field_a;       // [+8]  secondary field (modifier, bank index)
    uint32_t  field_b;       // [+12] tertiary field
    uint32_t  field_c;       // [+16] type / class
    uint32_t  stride;        // [+20] register pair stride (1=single, 2=pair, 3=triple, 4=quad)
    uint64_t  imm_or_ptr;    // [+24] immediate value or pointer to constant
};
```

Source operands are accessed at base + 32*index, so operand 0 is at offset +0, operand 1 at +32, operand 2 at +64, and so on. The stride field at offset +20 (also found at operand+52, +84, +116, +148 in the full instruction descriptor) is critical for register allocation: a stride of 2 means the instruction requires consecutive register pairs (R0:R1, R2:R3, ...), stride 3 means triples, and stride 4 means quads. The WMMA/HMMA decoders set these extensively.

## Instruction Codec (0xA70000 -- 0xB80000)

The 1.1 MB region from `0xA70000` to `0xB80000` implements the complete instruction codec for SM90 -- the paired encoder/decoder functions that translate between the high-level IR representation and 128-bit binary machine words. This region contains no register allocation, no scheduling, and no peephole optimization code.

### Component Breakdown

| Range | Size | Count | Identity |
|---|---|---|---|
| `0xA709F0` | 54 KB | 1 | Field offset query (`sub_A709F0`, 6,491 lines) |
| `0xA7DE70` | 50 KB | 1 | Field presence query (`sub_A7DE70`, 6,240 lines) |
| `0xA853F0` | 3 KB | 1 | Operand type compatibility checker |
| `0xA87CE0`--`0xB25D50` | ~630 KB | ~164 | Per-opcode encoders |
| `0xACECF0`--`0xB77B60` | ~700 KB | ~139 | Per-opcode decoders |

### Field Query Functions

`sub_A709F0` and `sub_A7DE70` are the two largest functions in the codec. They implement giant switch tables mapping `(opcode_class, field_id)` pairs to either bit offsets or presence booleans.

`sub_A709F0` (InstrFieldOffset_Query): takes an instruction pointer and a field ID, switches on the opcode class at `*(a1+12)` (covering opcode classes 0x00 through 0x171, approximately 370 instruction classes), and for each valid `(class, field_id)` combination, returns the bit offset within the 128-bit instruction word where that field is encoded. The offset is computed as `sub_A4D370(a1+48, bitfield_index) + base_constant`, where the base constants (e.g., 790, 1278, 1942, 2476) represent bit positions. Returns `0xFFFFFFFF` (-1) when the field does not exist for the given opcode class.

`sub_A7DE70` (InstrFieldPresent_Query): identical switch structure, but every case body returns `sub_A4Dxxx(a1+48, idx) != 0` -- a boolean "does this field have a non-zero value" test. This is the `hasField` companion to `sub_A709F0`'s `getFieldOffset`.

Four bitfield extraction helpers are used by both functions, corresponding to different field widths:
- `sub_A4D270`: extract narrow bitfield
- `sub_A4D2F0`: extract medium bitfield (type B)
- `sub_A4D370`: extract medium bitfield (type A)
- `sub_A4D3F0`: extract wide bitfield
- `sub_A4D470`: extract extra-wide bitfield

### Operand Type Compatibility

`sub_A853F0` (259 lines) implements a pure type algebra function that determines valid register type combinations for paired operands. It takes `(type_a, type_b, query_mode)` and returns a compatibility code:

| Return | Meaning |
|---|---|
| 0 | Compatible |
| 4, 5, 6, 7, 8 | Specific incompatibility type |
| 10, 12 | Required conversion |

The type values 1--5 correspond to GPR, predicate, uniform, special register, and constant bank reference (inferred from the dispatch logic and register file size constants at each branch). The `query_mode` parameter (`a3`) selects between two interpretation modes.

### Encoder Functions (0xA87CE0 -- 0xB25D50)

The ~164 encoder functions follow a uniform pattern:

1. Load a 128-bit opcode template constant via `_mm_or_si128` (or scalar `|=` for some variants).
2. Extract operands from the 32-byte-stride operand array at `*(a2+32)`.
3. Pack register numbers, modifiers, and immediate values into specific bit positions in the 128-bit output word.
4. Handle register sentinel substitution (1023 -> architecture default).
5. Encode modifier bits (rounding mode, saturation, FTZ, data type, comparison predicate, memory ordering) via shared modifier-setter functions.

Size distribution of encoders:

| Line Count | Typical Instructions | Operand Count |
|---|---|---|
| 106--114 | Simple ALU, shifts, moves | 2--3 source operands |
| 118--136 | FP operations with rounding | 3--4 operands + modifiers |
| 143--170 | FMA, MAD, predicated ops | 5--7 operands |
| 216--335 | DMMA, paired-register ops | 6+ operands + pairing logic |

The encoder clusters are organized by instruction family:

| Range | Functions | Family |
|---|---|---|
| `0xA87CE0`--`0xA9E770` | ~25 | Core ALU / register-register |
| `0xAA0000`--`0xAAF000` | ~60 | Dense ALU cluster (integer, shift, logical) |
| `0xAB0000`--`0xABFF00` | ~52 | Memory operations (load, store, atomic) |
| `0xAC0000`--`0xACF000` | ~32 | Special / miscellaneous |
| `0xB00000`--`0xB0CC00` | ~36 | Complex multi-operand (texture, surface) |
| `0xB25000`--`0xB26300` | ~4 | Atomic shared-memory operations |

**Example: `sub_A87CE0` (Encode_3OpRRR_TypeA)**. This encoder handles a 3-operand register-register-register instruction. It OR's the 128-bit constant `xmmword_1E5B2F0` into the output, encodes the destination register at bits [15:18] via `sub_A50D10`, encodes the bank select at bits [12:14] via `<< 12 & 0x7000`, and processes three source operands at offsets +32, +64, and +96 from the operand array base. Helper functions `sub_A59C60`, `sub_A51200`, and `sub_A51220` extract operand values.

**Example: `sub_B0AA80` (Encode_DMMA_PairedReg, 335 lines)**. The largest encoder in this range handles double-precision MMA with paired register encoding. It contains a 40-entry if-else chain mapping register pairs: `if (result==1 && v59==0)`, `if (result==3 && v59==2)`, up to `(result==79 && v59==78)`. Each branch encodes an even:odd register pair (R0:R1, R2:R3, ..., R78:R79) as a single compact field. A 3-level modifier combination logic (`v48`, `v52`, `v54`) selects cache control bits.

### Decoder Functions (0xACECF0 -- 0xB77B60)

The ~139 decoder functions reverse the encoding process: they extract bit fields from a 128-bit instruction word and populate the IR instruction descriptor. The common decoder helpers are:

| Function | Role |
|---|---|
| `sub_4FF010` | Set up register operand (operand_idx, reg_class, is_dst, operand_type, reg_num) |
| `sub_4FF150` | Set up predicate operand (operand_idx, reg_class, is_dst, type, pred_num) |
| `sub_4FF280` | Set up immediate/constant operand (operand_idx, class, is_dst, type, imm_val) |
| `sub_4FF390` | Set up 5-bit immediate field |
| `sub_4FF480` | Set up 17-bit immediate field |
| `sub_50C790` | Decode predicate condition |

Modifier decoder functions configure instruction modifiers:

| Function | Modifier |
|---|---|
| `sub_5096E0` | Flush-to-zero (FTZ) |
| `sub_5095F0` | Negate |
| `sub_50A670` | Rounding mode |
| `sub_50C0F0` | Data type |
| `sub_509760` | Saturation |
| `sub_509200` | Saturation (variant) |
| `sub_50BD20` | Rounding (variant) |
| `sub_50C000` | Comparison mode |
| `sub_50C4F0` | Flush-to-zero (variant) |
| `sub_50B500` | Data type (variant) |

### Decoder Clusters

| Range | Functions | Identity |
|---|---|---|
| `0xACECF0` | 1 | **HMMA** (tensor core MMA, class 35) |
| `0xAF6000`--`0xB00000` | ~20 | FADD/FMUL/FP decoders (class 180) |
| `0xB00000`--`0xB0CC00` | ~10 | LDS/STS shared memory (classes 232, 191) |
| `0xB2A000`--`0xB2F000` | ~15 | ALU / LDGSTS (async copy, class 205) |
| `0xB30000`--`0xB39000` | ~12 | IMMA / tensor op decoders (classes 296, 297) |
| `0xB3A000`--`0xB40000` | ~15 | DFMA / DSET / HMMA_Large (class 295, 297) |
| `0xB40000`--`0xB4B000` | ~25 | SFU / TEX / TLD4 decoders |
| `0xB4C000`--`0xB54000` | ~22 | Miscellaneous ALU decoders |
| `0xB53000`--`0xB63000` | 3 | **WMMA monster decoders** (class 296, 2490--2842 lines each) |
| `0xB6B000`--`0xB7C000` | ~18 | Uniform register decoders (UIMAD, UFMA, UMOV) |

## Hopper Tensor Core Support (HMMA/WMMA)

The SM90 codec dedicates substantial code to tensor core instruction encoding and decoding, reflecting Hopper's enhanced tensor operations. Three categories of tensor instructions are present:

### HMMA (Hopper Matrix Multiply-Accumulate)

`sub_ACECF0` (128 lines) decodes the HMMA instruction (opcode class 35). It sets format bytes `*(_BYTE*)(a2+14) = 18` and `*(_BYTE*)(a2+15) = 19`, then calls MMA-specific modifier decoders (`sub_50F2B0`, `sub_50F2D0`, `sub_50C630`, `sub_50F570`, `sub_50F550`). The instruction has 6 register operands (operands 0--5), with operand class 10 indicating shared memory / matrix register type. Post-decode fixups set `operand[n].reg+1` for paired register allocation constraints. Opcodes 2038--2041 trigger variant-specific register dependency fixups.

### WMMA (Warp Matrix Multiply-Accumulate)

The three largest functions in the entire codec region are WMMA decoders, all for opcode class 296:

| Function | Lines | Format | Identity |
|---|---|---|---|
| `sub_B53830` | 2,490 | format 3 | WMMA (warp MMA) |
| `sub_B5AB00` | 2,837 | format variant | WMMA Extended |
| `sub_B62DE0` | 2,842 | format variant | WMMA Maximum |

Each decoder processes 7 register operands plus 1 predicate output and contains **hundreds** of post-decode register pairing fixup checks. Each check is a 5-way conjunction:

```
if (sub_A58D30() == X &&    // instruction variant
    sub_A58D50() == Y &&    // data type
    sub_A58C90() == Z &&    // precision
    sub_A58BC0() == W &&    // matrix layout
    sub_A58CD0() == V)      // accumulation mode
{
    operand[n].stride = 2;  // or 3, or 4
}
```

The five query functions retrieve the instruction variant, data type, precision mode, matrix layout, and accumulation mode respectively. When a combination matches, the stride field at operand offset +116 is set to 2, 3, or 4, constraining the register allocator to assign consecutive register pairs, triples, or quads. Referenced opcode variants include 2129--2134, 2532--2534, 2669, 2681--2683, and 2840--2841.

The combinatorial explosion in these decoders reflects the number of WMMA variants in the Hopper ISA: every combination of data type (FP16, BF16, TF32, FP64, INT8, INT4), matrix layout (row-major, column-major), precision (full, reduced), and accumulation mode generates a distinct register pairing constraint.

### IMMA (Integer Matrix Multiply-Accumulate)

Opcode class 297 handles integer MMA variants. Decoders at `0xB30940` (188 lines) and `0xB30FF0` (201 lines) handle the basic variants. The extended MMA decoder at `0xB40C30` (517 lines, the largest single decoder) handles all MMA modifiers including warp group configuration, data format, layout, and an extensive register pairing fixup section.

## Uniform Register Decoders (0xB6B000 -- 0xB7C000)

Fourteen decoders in this range handle uniform-register instructions (UIMAD, UFMA, UIADD, UMOV). These use a distinctive bitmap-based register class membership test: each decoder loads 24 x 128-bit constants (384 bytes of bitmap data) from read-only data and tests register numbers against the bitmap using bit-shift operations:

```c
// Bitmap membership test for register class
bool is_class_member = (0x1668166816681660ull >> reg_num) & 1;
```

Functions `sub_403941` and `sub_4038C0` implement the bitmap membership test on packed 128-bit bitset arrays. The bitmap determines the required operand stride:

| Stride | Register Width | Use Case |
|---|---|---|
| 2 | 64-bit (paired) | Standard double-width operations |
| 3 | 96-bit (triple) | Triple-wide uniform registers |
| 4 | 128-bit (quad) | Quad-wide uniform registers (256-bit) |

### Uniform Instruction Classes

| Opcode Class | Mnemonic | Decoders | Line Range |
|---|---|---|---|
| 211 | UIMAD | `sub_B6B0F0`, `sub_B6B9F0`, `sub_B6C310` | 229--248 |
| 230 | UFMA | `sub_B6CC70`, `sub_B6EE10`, `sub_B71020`, `sub_B75640`--`sub_B77B60` | 324--389 |
| 285 | UIADD | `sub_B6D790`, `sub_B6E2D0`, `sub_B6F960`, `sub_B704C0`, `sub_B71B70` | 324--331 |
| 34 | UMOV | `sub_B726D0`, `sub_B732A0`, `sub_B73E70`, `sub_B74A50` | 320--326 |

## Instruction Class Reference

The following instruction classes have been identified in the SM90 codec through decoder analysis:

| Class ID | Mnemonic | Type | Notes |
|---|---|---|---|
| 2 | MOV | Data movement | Register-to-register move |
| 34 | UMOV | Uniform data movement | Uniform register move |
| 35 | HMMA | Tensor core | Half-precision matrix multiply-accumulate |
| 90 | PRMT | Bit manipulation | Byte permute |
| 121 | BRA | Control flow | Branch |
| 126 | BAR | Synchronization | Barrier |
| 143 | NOP | Control | No-operation |
| 173 | RET | Control flow | Return |
| 180 | FADD/FMUL | Floating point | FP add / multiply |
| 191 | STS | Memory | Store to shared memory |
| 195 | DEPBAR | Scheduling | Dependency barrier |
| 205 | LDGSTS | Memory | Load-global-store-shared (async copy) |
| 211 | UIMAD | Uniform integer | Uniform integer multiply-add |
| 227 | VOTE | Warp | Warp vote |
| 230 | UFMA | Uniform FP | Uniform FP multiply-add |
| 232 | LDS | Memory | Load from shared memory |
| 280 | EXIT | Control flow | Kernel exit |
| 285 | IADD/UIADD | Integer | Integer add / uniform integer add |
| 289 | HMMA_ALU | Tensor core | Hopper matrix ALU |
| 290 | DFMA_DP | Floating point | Double-precision FMA |
| 292 | MUFU | Special function | Multi-function unit (sin, cos, rsq, ...) |
| 293 | I2F/F2I | Conversion | Integer-float conversion |
| 295 | DFMA | Floating point | Double-precision FMA (extended) |
| 296 | WMMA | Tensor core | Warp matrix multiply-accumulate |
| 297 | IMMA | Tensor core | Integer matrix multiply-accumulate |
| 298 | QSPC | Special | Quasispecific operation |
| 299 | DP4A | Tensor core | Dot-product 4-element accumulate |
| 300 | HADD2 | Floating point | Half-precision add x2 |
| 301 | TEX | Texture | Texture fetch |
| 303 | TLD | Texture | Texture load |
| 315 | YIELD | Control | Thread yield |
| 316 | SSY | Control flow | Set synchronization point |
| 319 | CAL | Control flow | Call |
| 325 | PBK | Control flow | Push breakpoint |
| 327 | PCNT | Control flow | Push counter |
| 368 | BSSY | Synchronization | Barrier set synchronization |

## Shared SM89/90 Backend (0x100C000 -- 0x11EA000)

The 1.9 MB region at `0x100C000`--`0x11EA000` contains the complete backend for both SM89 and SM90 architectures. It decomposes into five functional layers:

### Backend Address Map

| Range | Size | Functions | Identity |
|---|---|---|---|
| `0x100C000`--`0x10FFFFF` | ~1.0 MB | ~750 | Shared instruction encoder templates |
| `0x1100000`--`0x1120000` | ~128 KB | ~30 | Backend driver (option parsing, codegen orchestration, ELF output) |
| `0x1120000`--`0x119BF40` | ~496 KB | ~160 | ISel pattern matchers |
| `0x119BF40` | ~231 KB | 1 | **ISel mega-hub** (too large for Hex-Rays) |
| `0x11D4680`--`0x11EA000` | ~90 KB | ~16 | Instruction scheduling + emission |

### Instruction Encoder Templates (0x100C000 -- 0x10FFFFF)

Approximately 750 functions, each 4--8.5 KB, implement instruction encoding table initializers. Every function follows the same template:

1. `sub_4C28B0(a1, offset, fieldlen, value)` -- set bitfield parameters (5--8 calls per function).
2. SSE load from global constant table (`xmmword_1F46xxx`) -- instruction signature.
3. Copy loop: 3 parallel arrays (10 entries each) from read-only data into the instruction descriptor at `a1+24` through `a1+140`.
4. `sub_4C60F0(a1, a2, slot, offset, type)` -- configure control code slots.
5. `sub_4C5F90(a1, a2)` -- finalize the descriptor.
6. `sub_50xxxx` family calls -- set modifier bits (predicate via `sub_50C790`, rounding via `sub_50E300`, FTZ via `sub_50E320`, etc.).

Size clusters by instruction complexity:

| Size Range | Instruction Type | Count |
|---|---|---|
| 4,700--6,200 bytes | Simple (moves, branches, simple math) | ~100 |
| 7,400--7,700 bytes | Standard 3-source ALU | ~400 |
| 7,800--8,100 bytes | ALU with extra modifiers (rounding, saturate) | ~150 |
| 8,300--8,500 bytes | Complex (texture, surface, atomics) | ~100 |

The constant tables reside in `.rodata` at `0x1F460E0`--`0x1F47400`. Each table contains 10 source-register-class entries (40-byte stride), 10 destination-register-class entries, 10 control-code entries, and a 16-byte SSE header with the instruction signature.

### Backend Driver (0x1100000 -- 0x1120000)

The ~30 functions in this range implement the compilation pipeline controller for SM89/90 targets:

**`sub_1112F30` (65,018 bytes, 2,164 lines) -- Main Compilation Driver.** This is the top-level per-module compilation entry point. It reads command-line options (`def-load-cache`, `force-load-cache`, `position-independent-code`), writes PTX headers (`.version`, `.target`, `.entry __cuda_dummy_entry__ { ret; }`), validates SM version compatibility (`--legacy-bar-warp-wide-behavior` requires sm_70+, tensor-memory-access-check is gated by target arch), and dispatches per-function codegen. The function selects codegen callbacks based on mode flags: `sub_110CD20` for compile-only, `sub_110D110` for multi-function, and `sub_110CBA0` / `sub_110D0B0` for standard mode. Multi-threaded compilation is supported via `sub_464AE0` (thread pool) and `sub_464C30`.

**`sub_1116890` (59,847 bytes, 1,998 lines) -- ELF Output and Metadata Generator.** Handles CUBIN output, builds JSON metadata trees (`version`, `metadata`, `type`, `min`, `max`), and integrates with `sub_1CFA200` / `sub_1CFA220` / `sub_1CFA2D0` for JSON object creation. Uses `setjmp`/`longjmp` for error recovery.

**`sub_1104950` (37,578 bytes, 1,208 lines) -- Option Parser.** Registers approximately 60 ptxas options via `sub_42E390`: `warn-on-double-precision-use`, `maxrregcount`, `opt-level`, `fast-compile`, `device-stack-protector`, `sanitize`, `position-independent-code`, `g-tensor-memory-access-check`, `query-controls`, `apply-controls`, and others. Validates option compatibility with target architecture and computes SM architecture family from `dword_1EED2E0` lookup table.

**`sub_110AA30` (18,774 bytes, 661 lines) -- Per-Function Codegen Init.** Sets up virtual table pointers (5 callback slots at offsets 24, 1544, 1568, 1584, 1600), the `"NVIDIA"` vendor string (offset 1200), and `"ptxocg.0.0"` producer string (offset 64). Magic value `38156003` at offset 48 serves as a tool ID. Feature flags are enabled by SM version: SM >= 14 enables extended features, SM >= 17 enables SM100-specific paths.

**`sub_1100E50` (13,759 bytes, 451 lines) -- Feature Flag Configurator.** Reads the SM version via `sub_15C3DD0(gpu_name)` and configures approximately 30 boolean feature flags. SM version 29 or 30 (corresponding to sm_89/sm_90) enables feature 33 (tensor core extensions) when debug suppression is not active. Uses `sub_16E3AA0` to set flags in the feature table at `a1+1096`.

**`sub_110BC90` (18,111 bytes, 763 lines) -- Register Allocation and Launch Configuration.** Reads thread-block dimensions (`blockDim.x/y/z` from `v9[6..8]`), computes total threads, handles `maxntid` and `minnctapersm` overrides, and performs complex register budget computation with multiple fallback paths. SM version range checks gate architecture-specific features.

### ISel Pattern Matchers (0x1120000 -- 0x119BF40)

Approximately 160 small functions implement pattern-matching rules for the SM89/90 instruction selector. Each function:

1. Takes `(match_context, ir_node, result_opcode*, result_priority*)` parameters.
2. Calls `sub_A49150(a1, a2, field_id)` to extract IR node properties.
3. Compares extracted values against known SASS opcode requirements through nested if-chains.
4. If all constraints match, writes the selected SASS opcode to `*a3` and sets the priority in `*a4`.

The field IDs map to IR node attributes:

| Field ID | Decimal | Attribute |
|---|---|---|
| `0x1DF` | 479 | Primary opcode class |
| `0x1DE` | 478 | Secondary opcode |
| `0x1C4` | 452 | Operation variant |
| `0x1A3` | 419 | Data type |
| `0x20` | 32 | Addressing mode |
| `0xD0` | 208 | Memory space |
| `0x80` | 128 | Result type |
| `0x31` | 49 | Comparison operator |
| `0xDF` | 223 | Modifier flags |
| `0x7B` | 123 | Texture operation type |

Register class 1023 acts as a wildcard ("any class"). Priority values (e.g., 16) determine preference when multiple patterns match the same IR node. Operand type predicates are checked via `sub_1119410` (register class), `sub_1119420` (is register), `sub_1119430` (is predicate), `sub_1119450` (is general register), `sub_1119490` (is immediate), and `sub_11194A0` (is constant bank reference).

### ISel Mega-Hub (0x119BF40)

`sub_119BF40` (225,792 bytes, estimated 7,500+ lines) is the master instruction selection dispatch function for SM89/90 targets. It is too large for Hex-Rays to decompile. Located immediately after the ISel helper functions, it contains a massive switch/jump-table on the IR opcode that calls the ~160 pattern matchers and selects the highest-priority match for each IR node. The protocol is uniform across all ISel backends:

```
for each pattern_matcher in sm89_90_table:
    matched = pattern_matcher(ctx, ir_node, &pattern_id, &priority)
    if matched && priority > best_priority:
        best_priority = priority
        best_id = pattern_id
emitter_table[best_id](ctx, ir_node)
```

### Instruction Scheduling (0x11D4680 -- 0x11EA000)

The final 16 functions form a cohesive instruction scheduling and emission subsystem. Five functions exceed 7 KB and share an identical data-structure pattern:

| Function | Size | Identity |
|---|---|---|
| `sub_11D6890` | 13,175 bytes | Main basic-block scheduler |
| `sub_11D6080` | 11,782 bytes | Scheduling dependency check |
| `sub_11D5940` | 10,364 bytes | Per-block scheduling initialization |
| `sub_11D52B0` | 9,111 bytes | Scheduling state query (checks for value 711) |
| `sub_11D4AF0` | 10,679 bytes | Scheduling state update |

All five use:
- 184-byte per-basic-block records stored in a growable array at offset +832.
- Capacity tracking at offset +840.
- Overflow entries in a hash-table / linked-list at offset +864.
- 192-byte scheduling contexts allocated from an arena at offset +848.
- Virtual-dispatch cleanup via a vtable at offset +32.

This is a classic list-scheduling implementation that tracks data dependencies (RAW/WAR/WAW hazards), models instruction latencies, manages register pressure, and inserts control-flow barriers.

## Compilation Call Graph

The key call paths for the SM89/90 backend:

```
sub_1116890 (ELF output entry)
  -> sub_1104950 (parse options)
  -> sub_1112F30 (main compilation driver)
     -> sub_110AA30 (per-function codegen init)
        -> sub_1100E50 (feature flag setup)
        -> sub_110BC90 (register/launch config)
     -> sub_110D2A0 (per-function output finalization)
        -> sub_14075D0 / sub_1407FC0 / sub_14091C0 (encoding passes)
     -> sub_1109180 / sub_1107F10 (function list processing)
        -> sub_11078F0 (register analysis)
     -> sub_1111DB0 (symbol table building)
        -> sub_110FA30 (symbol resolution)
           -> sub_110EF30 / sub_110E7E0 (expression walkers)
  -> sub_1109EB0 (trace JSON integration)
  -> sub_1103030 (option table builder)

sub_119BF40 (ISel mega-hub)
  -> sub_1190050 .. sub_119B8F0 (pattern matchers)
  -> sub_100C110 .. sub_10FFFFF (instruction encoder templates)
     -> sub_4C28B0, sub_4C60F0, sub_4C5F90 (field setup)
     -> sub_50xxxx (modifier setup)

sub_11D6890 (block scheduler)
  -> sub_11D6080 (dependency query)
  -> sub_11D5940 (init)
  -> sub_11D4AF0 (update)
  -> sub_11D52B0 (query)
```

## Key Global Data

| Address | Type | Identity |
|---|---|---|
| `xmmword_1E5B2C0`--`xmmword_1E5C1xx` | 128-bit constants | Opcode template constants for encoders |
| `dword_1E3CBD0`, `dword_1E3CBE0` | Lookup tables | Modifier value encoding tables |
| `xmmword_1F460E0`--`0x1F47400` | Constant tables | Instruction encoding parameter tables |
| `dword_1EED2E0` | Lookup table | SM version -> architecture family mapping |
| `off_1EEEFA0` | Descriptor table | ELF metadata field descriptors |
