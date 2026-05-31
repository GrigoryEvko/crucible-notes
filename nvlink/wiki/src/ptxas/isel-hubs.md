# Instruction Selection Hubs

> **Note**: This page documents the embedded ptxas copy within nvlink v13.0.88. The standalone ptxas binary has its own comprehensive wiki -- see the [ptxas Reverse Engineering Reference](../../ptxas/index.html) for the full compiler reference. For the standalone ptxas instruction selection documentation, see [ptxas ISel](../../ptxas/codegen/isel.html).

The instruction selection (ISel) subsystem within the embedded ptxas backend occupies approximately 3 MB of `.text` across five architecture-specific backends. Each backend is organized around a single "mega-hub" dispatch function -- a monolithic function so large (160--280 KB) that Hex-Rays cannot decompile it. These mega-hubs implement a priority-based linear scan architecture: for every IR instruction to be lowered, the hub calls every pattern matcher in sequence, tracks the highest-priority match, then dispatches to the corresponding emitter. This page documents the complete ISel hub architecture as recovered from nvlink v13.0.88.

## The Five Mega-Hub Functions

| Address | Size | Target Arch | Matchers | Emitters | Description |
|---|---|---|---|---|---|
| `sub_5B1D80` | 204 KB | SM50-7x | 1,293 | ~79 | MercExpand engine for Maxwell/Pascal/Volta |
| `sub_FBB810` | 280 KB | SM75 (Turing) | 276 | 18+38 | Largest function in the binary |
| `sub_D5FD70` | 239 KB | SM80 (Ampere) | 259 | 137 | Three-phase pipeline with bitfield packing |
| `sub_126CA30` | 239 KB | SM86/87 (shared) | ~160 | varies | Shared PTX-level instruction selector |
| `sub_119BF40` | 231 KB | SM89/90 (Ada/Hopper) | ~160 | varies | Ada Lovelace / Hopper backend |

All five functions are too large for static decompilation. Their internal structure has been inferred from the pattern matchers they call, the emitters they dispatch to, and the protocol shared across all backends.

## ISel Protocol

Every mega-hub follows an identical protocol, regardless of target architecture:

```c
best_priority = 0
best_id = -1

for each pattern_matcher in pattern_table[arch]:
    matched = pattern_matcher(ctx, ir_node, &pattern_id, &priority)
    if matched && priority > best_priority:
        best_priority = priority
        best_id = pattern_id

emitter_table[best_id](ctx, ir_node)
```

This is a linear scan -- not a tree-pattern matcher or DAG-based selector. Every pattern is evaluated unconditionally, though each matcher contains an early-out check (`if (*a4 <= my_priority)`) that allows it to skip expensive operand validation when a higher-priority pattern has already been found.

### Pattern Matcher Signature

All pattern matchers across all five backends share a single uniform signature:

```c
char __fastcall pattern_matcher(
    __int64 ctx,           // attribute query context
    __int64 node,          // IR instruction node to match
    _DWORD *match_id,      // output: pattern ID (small integer)
    int    *priority       // output: match priority (higher wins)
);
```

The function returns nonzero if the pattern matches. On match, it writes the pattern ID to `*match_id` and the priority to `*priority`, but only if the new priority exceeds the current value in `*priority`. This allows the linear scan to accumulate the best match without external bookkeeping.

### Matching Algorithm

Each pattern matcher performs a strict sequence of checks. If any check fails, the function returns 0 immediately. The full check sequence:

1. **Attribute queries.** Call `sub_A49150(ctx, node, attribute_id)` to read instruction attributes. Each pattern checks 2--12 attributes against expected constant values. Attribute IDs are small integers (5, 69, 118, 144, 161, 162, 190, 200, 201, 211, 220, 228, 229, 247, 248, 268, 269, 287, 302, 304, 312, 338, 348, 385, 391, 394, 397, 480, etc.). Attribute 5 typically encodes the instruction class; attribute 480 the instruction format identifier.

2. **Operand count check.** Call `sub_530FD0(node)` to get the destination (explicit) operand count and `sub_530FC0(node)` to get the source (implicit) operand count. Each pattern expects specific counts.

3. **Operand iteration.** Call `sub_530FB0(node, idx)` to retrieve each operand (returns pointer to 32-byte operand structure at `base + 32 * idx`).

4. **Operand type validation.** Each operand's type tag at offset +0 is checked against the expected kind. The predicate functions differ by backend but test for the same set of 16 operand kinds:

   | Tag | Type | SM50-7x Predicate | SM75 Predicate | SM80 Predicate |
   |---|---|---|---|---|
   | 1 | Immediate | `sub_530EA0` | `sub_F16050` | `sub_CDD670` |
   | 2 | Register (GPR) | `sub_530E90` | `sub_F16040` | `sub_CDD600` |
   | 3 | Symbol/label | `sub_530F00` | `sub_F160B0` | -- |
   | 4 | Constant | `sub_530EF0` | `sub_F160A0` | -- |
   | 5 | Condition code | `sub_530EE0` | `sub_F16090` | `sub_CDD680` (const buf) |
   | 6 | Memory reference | `sub_530EB0` | `sub_F16060` | -- |
   | 7 | Barrier/sync | `sub_530F50` | `sub_F16100` | -- |
   | 9 | Predicate | `sub_530ED0` | `sub_F16080` | `sub_CDD610` |
   | 10 | Address/surface | `sub_530EC0` | `sub_F16070` | `sub_CDD630` (uniform) |
   | 15 | False predicate | `sub_530F10` | `sub_F160C0` | -- |

5. **Register class validation.** The register class field at operand offset +4 is read via an identity function (`sub_530E80` / `sub_F16030` / `sub_CDD5F0`). The special value 1023 (`0x3FF`) means "any/wildcard" -- the operand's register class is unconstrained. Concrete register class values observed: 1 (R32/GPR32), 2 (R64/GPR64), 4 (R128/GPR128), 5 (predicate).

6. **Data type validation.** The data type field at operand offset +20 is checked. Some patterns use bitvector tricks for type set membership: the masks `0x5555555555555554` and `0x1111111111111111` encode allowed type sets. Special data type value 128 represents `.f64`.

7. **Priority assignment.** If all checks pass, the matcher writes `*match_id = N` and `*priority = P`. Priority values range from 1 to 39 across the observed corpus. The pattern with the highest priority wins.

## The Attribute Query Function

`sub_A49150` is the universal instruction attribute accessor, called 30,768 times across the binary. It takes three arguments: context, IR node, and attribute slot ID. It returns a 32-bit integer representing the attribute value. This single function underlies all pattern matching decisions across all five ISel backends.

Key attribute IDs and their semantic roles (inferred from usage patterns):

| Attribute ID | Semantic Role | Typical Values |
|---|---|---|
| 5 | Instruction class | 12 = memory operation |
| 69 | Subclass modifier | 317--318 (texture format) |
| 118 | Control flow tag | 519 (return/exit) |
| 190 | MOV identifier | 815 |
| 200 | Special handling flag | 1107 (triggers MercExpand MOV path) |
| 397 | Operand encoding mode | 2115 |
| 480 | Instruction format ID | 2481, 2483 |

## Per-Backend Details

### SM50-7x: MercExpand Engine (`sub_5B1D80`, 204 KB)

The oldest ISel backend covers Maxwell, Pascal, and Volta architectures (SM50 through SM7x). Unlike the newer backends, this one is organized around the "MercExpand" instruction expansion engine -- confirmed by the string `"After MercExpand"` at `0x5FF15E`.

**Address layout:**

| Range | Size | Contents |
|---|---|---|
| `0x530FE0`--`0x5B1AB0` | 523 KB | 1,293 pattern matchers |
| `0x5B1D80`--`0x5E4470` | 204 KB | MercExpand mega-hub (not decompilable) |
| `0x5E4470`--`0x600260` | 114 KB | MercExpand engine (bitvectors, hash maps, CFG) |
| `0x603F60`--`0x61FA60` | 112 KB | 79 SM50 instruction encoders |

**Pattern matcher statistics:**

- 1,293 auto-generated pattern matching functions
- 152 distinct target opcodes (machine instruction types)
- 36 distinct priority levels (range 1--39)
- Most-matched opcode: opcode 1 (123 patterns), opcode 2 (100 patterns)
- Most common priority: 11 (used by 136 patterns)

**MercExpand dispatch (`sub_5FDDB0`, 25.5 KB).** This is the secondary dispatcher called from within the mega-hub. It iterates IR nodes in basic block order and dispatches on the IR opcode type field at node offset +28:

| Opcode Type | Handler |
|---|---|
| 0 | Generic expansion via vtable +48 |
| 5, 8, 9 | Register width clamping (max width = 15) |
| 11 | Complex handler: texture (`sub_5F80E0`), shared memory (`sub_5FAC90`), surface (`sub_5FC1B0`), or generic (vtable +88) |
| 12 | vtable +136 |
| -1 | Terminator: checks predication flags |
| 120 | Special node: skip |

Before dispatching, MercExpand checks attribute 200 against value 1107 to intercept MOV instructions for special-case expansion (`sub_5FC6B0`).

**MercExpand infrastructure.** The engine includes:

- SSE-optimized bitvector operations (`sub_5E4470` AND, `sub_5E4670` OR, `sub_5E4810` ANDNOT, `sub_5E4AE0` XOR) for liveness analysis
- FNV-1a hash maps (prime 16777619, offset basis `0x811C9DC5`) for IR node lookup tables, with auto-resize at load factor > 1 and 4x capacity growth
- CFG analysis: RPO computation, backedge detection, graphviz DOT dump (`sub_5EA250` outputs `digraph f { ... }`)
- Register state caching with generation-counter invalidation across 13+ register slots mapping to NVIDIA's physical register partitions (R-regs, predicates, CC)
- Resource constraint propagation: `sub_5F8B60` (16 KB) classifies 52 register types via `byte_1DFE340` lookup table and applies constraints through predicate modes (read/write/readwrite/clobber)

**SM50 instruction encoders.** The 79 encoders at `0x603F60`--`0x61FA60` each write a specific SM50 machine instruction via the core bitfield primitive `sub_4C28B0(buf, bit_offset, width, value)`. Encoding format distribution: format 1 (single 64-bit) = 17 encoders, format 2 (double 128-bit) = 14 encoders, format 3 (triple 192-bit) = 48 encoders. Observed SM50 opcodes include: `0x1C` (IADD), `0x08` (FMUL), `0x10` (IMAD), `0x11` (FFMA), `0x13` (MOV), `0x1D` (ISETP), `0x27` (TEX), `0x42` (LDG), `0x43` (STG).

### SM75: Turing ISel Backend (`sub_FBB810`, 280 KB)

The Turing (SM75) backend is the largest single-architecture ISel backend in the binary. `sub_FBB810` at 280 KB is the largest function overall.

**Address layout:**

| Range | Size | Contents |
|---|---|---|
| `0xF16030`--`0xF160F0` | <1 KB | 15 operand predicates |
| `0xF10080`--`0xF15A50` | 22 KB | 18 instruction emitters |
| `0xF16150`--`0xFBB780` | 678 KB | 276 pattern matchers |
| `0xFBB810` | 280 KB | SM75 ISel mega-hub (not decompilable) |
| `0xFFFDF0`--`0x100BBF0` | 48 KB | 38 post-ISel emit+encode functions |

**Operand predicates.** 15 trivial functions classify operand types by single-byte tag comparison. Each returns `a1 == N`. The Turing backend introduced operand kind 10 (uniform register) reflecting Turing's uniform register file (URF) for scalar operations. Branch predicates use a paired check: `sub_F160B0(v) || sub_F160C0(v)` accepts both "true predicate" (kind 3, PT) and "false predicate" (kind 15, !PT).

**Pattern matcher categories.** The 276 matchers organize into functional groups:

| Range | Count | Instruction Class |
|---|---|---|
| `0xF1C3F0`--`0xF20D10` | ~20 | Tensor core (HMMA) -- f16/f32/f64, wide registers |
| `0xF20D10`--`0xF2B2A0` | ~30 | ALU/arithmetic (IADD3, IMAD, shifts, logic) |
| `0xF307E0`--`0xF36A20` | ~25 | Memory/load-store (LDG, STG, SHFL) |
| `0xF3C0F0`--`0xF437C0` | ~20 | Conversion/cast (I2I, F2F, MUFU) |
| `0xF4AA30`--`0xF4FB70` | ~15 | Predicated operations (ISETP, texture fetch) |
| `0xF58BB0`--`0xF5C120` | ~10 | Store with predication |
| `0xF6DC60`--`0xF71B60` | ~15 | Surface/texture operations |
| `0xF76170`--`0xF77DF0` | 8 | Complex HMMA (largest matchers, 6--8 KB each) |
| `0xF82CF0`--`0xF96B40` | ~50 | ALU patterns (IMAD, LEA, SHF, BFE, BFI, LOP3, PRMT) |
| `0xF97CE0`--`0xF9CD30` | ~15 | Comparison/SETP (DSETP, FSETP, ISETP) |
| `0xFA0310`--`0xFAA4E0` | ~20 | Branch/call/return (BRA, CALL, RET) |
| `0xFB7A90`--`0xFBB780` | ~5 | Final/fallback matchers |

**Fallback pattern.** `sub_FBB780` (1,108 bytes) is the lowest-priority pattern: it checks only that operand count is 0, implicit count is 2, and the first implicit operand is a uniform register with class R32. It sets `pattern_id=1, priority=2`. Any other matching pattern overrides it.

**Most complex patterns.** `sub_F77140` and `sub_F77DF0` (each ~8.4 KB) match HMMA variants with 9 implicit operands, checking 10+ attributes and using R128 register classes for tensor core operations. They carry priority 39 -- the highest observed across any backend.

**Instruction emitters.** The 18 emitters at `0xF10080`--`0xF15A50` follow an 8-phase structure:

1. Set instruction opcode at `a2+12` (18=integer ALU, 104=FP32, 126=memory)
2. Load register class descriptor from `.rodata` into `a1+8`
3. Populate 10-slot operand descriptor arrays at `a1+24`--`a1+140`
4. Set explicit operand count at `a1+144`
5. Bind operands via `sub_4C6380`/`sub_4C60F0`/`sub_4C6DC0`
6. Encode bitfields into 128-bit encoding words at `a1+544` and `a1+552`
7. Set instruction class tag at `a1+276` (e.g., `0xE000000004` for load/store)
8. Write branch target / relocation info

**Post-ISel emit+encode.** The 38 functions at `0xFFFDF0`--`0x100BBF0` handle complex instructions that require immediate bitfield packing. They use `sub_4C28B0(ctx, bit_offset, width, value)` extensively -- packing opcode bits, sub-opcodes, register encoding classes, and modifier fields into 128-bit instruction words. Each function extracts 6--8 modifier fields via `sub_A551C0`--`sub_A55470` and encodes them at precise bit positions.

### SM80: Ampere ISel Backend (`sub_D5FD70`, 239 KB)

The Ampere backend implements a clean three-phase pipeline for each instruction: pattern match, operand emission, and binary encoding.

**Address layout:**

| Range | Size | Contents |
|---|---|---|
| `0xCA0000`--`0xCDC000` | 240 KB | Operand emission + bitfield packing (137 functions) |
| `0xCDD5F0`--`0xCDD690` | <1 KB | 15 operand predicates |
| `0xCE2000`--`0xD5FD70` | 510 KB | 259 ISel pattern matchers |
| `0xD5FD70` | 239 KB | SM80 ISel mega-hub (not decompilable) |
| `0xD9A400`--`0xDA0000` | 23 KB | 17 binary encoding functions |

**SM80 operand predicates.** Similar to SM75 but with architecture-specific naming:

| Function | Test | Operand Kind |
|---|---|---|
| `sub_CDD600` | isGPR | General-purpose register |
| `sub_CDD610` | isPredicate | Predicate register |
| `sub_CDD630` | isUniformReg | Uniform register (Ampere+) |
| `sub_CDD670` | isImmediate | Immediate value |
| `sub_CDD680` | isConstBuf | Constant buffer reference |
| `sub_CDD5F0` | getRegFile | Register file extraction |

**Instruction coverage.** The SM80 backend handles 19 distinct SASS instructions with 259 pattern variants:

| Opcode | Mnemonic | Variants | Description |
|---|---|---|---|
| 34 | HMMA | 11 | Tensor core half-precision matrix multiply-accumulate |
| 39 | S2R | 2 | Special register to GPR |
| 40 | CS2R | 2 | Control/status special register to GPR |
| 90 | IMAD | 4 | Integer multiply-add (32-bit) |
| 127 | FFMA | 12 | FP32 fused multiply-add |
| 195 | DSETP | 2 | FP64 set predicate |
| 205 | LEA | 1 | Load effective address |
| 230 | IMAD.WIDE | 9 | Integer multiply-add with 64-bit result |
| 284 | DADD | 1 | FP64 addition |
| 285 | LDG | 9 | Global memory load |
| 289 | ISETP | 4 | Integer set predicate |
| 290 | IMNMX | 4 | Integer min/max |
| 292 | FSETP | 2 | FP32 set predicate |
| 293 | SEL | 4 | Conditional move/select |
| 294 | SHFL | 1 | Warp shuffle |
| 295 | FADD | 4 | FP32 addition |
| 296 | FMUL | 4 | FP32 multiplication |
| 297 | MUFU | 4 | Multi-function unit (sin/cos/sqrt/rcp/lg2/ex2) |
| 299 | HADD2 | 2 | FP16x2 packed addition |

**Operand emission phase.** The 137 emission functions at `0xCA0000`--`0xCDC000` each handle one `(opcode, format)` combination. Format codes denote operand encoding variants:

| Format | Name | Description |
|---|---|---|
| 0 | RR | Register-register |
| 1 | RI | Register-immediate |
| 2 | RC | Register-constant buffer |
| 3 | RR.ALT | Register-register alternate encoding |
| 4 | RR.P | Register-register with predicate |
| 5 | RI.P | Register-immediate with predicate |
| 6 | RC.P | Register-constant buffer with predicate |
| 7 | SHFL | Warp shuffle encoding |
| 8 | RR.3SRC | Three-source register |
| 9-11 | RI.P2, RR.WIDE, RR.ADD | Extended variants |
| 13-18 | TCA-TCE | Tensor core encoding variants |
| 23-24 | TC.ALT, TC.ALT2 | Tensor core alternate |
| 42-45 | TC.WIDE1-4 | Wide tensor core (11 KB each) |

Each emitter follows a fixed protocol:

```c
*(WORD*)(a2+12) = opcode_id;       // e.g., 127 for FFMA
*(BYTE*)(a2+14) = format_id;       // e.g., 0 for RR
*(BYTE*)(a2+15) = max_operand_slots; // e.g., 25
// Decode operands from 128-bit packed IR at *(a1+16)
// Call emitRegOperand/emitPredicateOperand/emitAddrOperand per slot
// Set modifiers: rounding mode, data type, saturation, negation, etc.
```

**Binary encoding phase.** The 17 encoding functions at `0xD9A400`--`0xDA0000` and the ~50 bitfield packing functions at `0xCA4760`--`0xCB3500` produce the final 128-bit SASS instruction word. Each uses SSE2 intrinsics (`_mm_or_si128`) to merge fixed opcode template bits and calls `sub_A50D10` to translate virtual register IDs to SASS encoding. Register ID 1023 is the sentinel for RZ (zero register); predicate ID 31 maps to PT (true predicate).

### SM86/87: Shared PTX ISel (`sub_126CA30`, 239 KB)

This ISel hub serves the shared PTX-level instruction selector, covering the instruction set common across SM86/87 targets.

**Address layout:**

| Range | Size | Contents |
|---|---|---|
| `0x11EA000`--`0x126C000` | 520 KB | ~160 pattern-match predicates |
| `0x126CA30` | 239 KB | ISel mega-hub (not decompilable) |

**Pattern matcher characteristics.** The ~160 matchers follow the same protocol as other backends but use a distinct set of operand predicate functions:

| Function | Test |
|---|---|
| `sub_11E9C90` | Operand type discriminator (returns 1=32-bit, 2=64-bit, 4=128-bit, 1023=any) |
| `sub_11E9CA0` | Integer register (32-bit capable) |
| `sub_11E9CB0` | Immediate/constant |
| `sub_11E9CD0` | General register (any bitwidth) |
| `sub_11E9CE0` | FP register |
| `sub_11E9D10` | Predicate register |
| `sub_11E9D20` | Uniform predicate |

Attribute queries use hex-format slot IDs: `0x1E0` (major opcode), `0x18F` (sub-opcode), `0x240` (address mode), `0x247` (texture class), etc. Pattern IDs range 1--57+, with priority values from 10 to 36.

**Instruction categories identified from attribute patterns:**

- FMA variants (opcode `0x1E0`=2480, sub-op 2121): patterns 4--13 at priority 15
- Atomic CAS (attr 5=12, `0xDC`=1206, `0x240`=2872): pattern 12 at priority 24
- Load from parameter space (complex multi-attribute): pattern 4 at priority 29
- Texture unified (attr `0x247`=2892): pattern 57 at priority 17
- Complex ALU with 9 operands: pattern 21 at priority 36

### SM89/90: Ada Lovelace / Hopper Backend (`sub_119BF40`, 231 KB)

The SM89/90 backend shares its mega-hub region with the main ptxas compilation driver, option parser, and ELF output generator.

**Address layout:**

| Range | Size | Contents |
|---|---|---|
| `0x100C000`--`0x10FFFFF` | 1.0 MB | ~750 shared instruction encoders (4--8.5 KB each) |
| `0x1100000`--`0x1120000` | 128 KB | Backend driver (option parser, codegen init, ELF output) |
| `0x1120000`--`0x119BF40` | 496 KB | ~160 ISel pattern matchers |
| `0x119BF40`--`0x11D4680` | 231 KB | SM89/90 ISel mega-hub (not decompilable) |
| `0x11D4680`--`0x11EA000` | 90 KB | Instruction scheduler + emission helpers |

**Backend driver.** `sub_1112F30` (65 KB) is the top-level per-module compilation driver. It writes PTX headers, validates SM version compatibility, selects codegen callbacks based on `--compile-as-tools-patch`, `--extensible-whole-program`, and `--compile-only` mode flags, and dispatches to per-function codegen via `sub_110AA30`. Multi-threaded compilation is supported via `sub_464AE0` (thread pool creation).

**Instruction scheduling.** The range `0x11D4680`--`0x11EA000` contains the per-basic-block instruction scheduler. It uses 184-byte scheduling entries organized in hash tables with arena allocation. Key functions:

- `sub_11D6890` (13 KB): Per-basic-block scheduling state builder
- `sub_11D6080` (12 KB): Scheduling predicate query (checks entry value 711)
- `sub_11D4AF0` (11 KB): Scheduling state update with rehashing
- `sub_11D5940` (10 KB): Per-block scheduling initialization

## Priority Scoring System

The priority system ensures that more specific patterns always defeat less specific ones. Analysis of all backends reveals a consistent scoring philosophy:

| Priority Range | Specificity | Typical Pattern Characteristics |
|---|---|---|
| 1--4 | Lowest / fallback | 0--2 attribute checks, minimal operand validation |
| 8--11 | Low | 2--4 attribute checks, basic operand count match |
| 13--15 | Medium | 2--4 attributes + operand type + register class checks |
| 17--19 | Standard | 5+ attributes + full operand validation + register class constraints |
| 24--29 | High | Complex addressing modes, memory operations with many constraints |
| 33--36 | Very high | Multi-attribute + multi-operand + register file + data type + special flags |
| 37--39 | Maximum | Surface/texture operations or tensor core with maximal specificity |

The highest observed priority is 39, used by HMMA tensor core patterns with 9+ operands and 10+ attribute checks (SM75 patterns `sub_F77140` and `sub_F77DF0`). The lowest observed priority is 2, used by fallback patterns that match when no instruction-specific pattern applies.

## Emitter Dispatch

After the linear scan selects the best-matching pattern, the mega-hub dispatches to the corresponding emitter via a function pointer table indexed by pattern ID. The emitter phase varies by backend:

**SM50-7x (MercExpand).** The MercExpand engine performs instruction expansion rather than direct emission. It creates new IR nodes (`sub_A4CA70`), sets attributes (`sub_A5B6B0`), configures operands (`sub_A48F80`), and inserts them into the instruction stream (`sub_A49DF0`). The 79 SM50 instruction encoders then handle final binary emission.

**SM75 (Turing).** Emitters populate a 576+ byte encoding context structure with register class descriptors, operand arrays, encoding words, and relocation metadata. The context structure layout:

| Offset | Size | Field |
|---|---|---|
| +8 | 16B | Register class descriptor (from `.rodata`) |
| +12 | 2B | Instruction opcode number |
| +24--140 | 10x4B x3 | Operand register numbers, types, flags (10 slots) |
| +144 | 4B | Explicit operand count |
| +148--160 | 4x4B | Relocation type and bit offset pairs |
| +276 | 8B | Instruction class tag |
| +544 | 8B | Encoding word 0 (64-bit bitfield) |
| +552 | 8B | Encoding word 1 (64-bit bitfield) |
| +558 | 2B | 16-bit immediate value |
| +572 | 4B | Branch/offset target |

**SM80 (Ampere).** Three-phase pipeline: operand emission decodes 128-bit packed IR into structured descriptors, then modifier setters configure rounding mode, data type, saturation, negation, absolute value, cache policy, memory scope, and eviction mode. Finally, binary encoding packs everything into 128-bit SASS via SSE2.

## Why Hex-Rays Cannot Decompile the Mega-Hubs

The five mega-hub functions range from 204 KB to 280 KB. Hex-Rays fails on them for several reasons:

1. **Function call count.** Each hub calls 160--1,293 pattern matchers. The resulting call graph and stack analysis exceeds Hex-Rays' internal working set limits.

2. **Linear control flow depth.** The hub is essentially a 200+ element sequential `if` chain with no early termination (every pattern must be tried). This produces an extremely deep control flow graph that the microcode optimizer cannot simplify.

3. **Variable aliasing.** The `match_id` and `priority` output variables are modified by every pattern matcher call, creating a long chain of potential aliases that the decompiler must track.

4. **Jump table size.** The emitter dispatch after pattern matching uses a large jump table or function pointer array indexed by pattern ID. Combined with the preceding linear scan, this creates a control flow structure that is tractable at the machine code level but exceeds decompiler limits.

Despite this, the hub functions are structurally simple: a linear sequence of pattern matcher calls followed by an indexed dispatch. All complexity lives in the pattern matchers and emitters, which decompile individually without difficulty.

## Key External Functions

Functions called from within the ISel subsystem that serve as the universal accessor layer:

| Function | Callers | Identity | Description |
|---|---|---|---|
| `sub_530FB0` | 31,399 | `IRNode_GetOperand` | Return pointer to operand at index: `*(a1+32) + 32*a2` |
| `sub_A49150` | 30,768 | `IRInstr_GetAttribute` | Query instruction attribute by slot ID |
| `sub_530FD0` | ~5,000 | `IRNode_GetNumDstOperands` | Return `*(a1+92)` (destination operand count) |
| `sub_530FC0` | ~5,000 | `IRNode_GetNumSrcOperands` | Return `*(a1+40) + 1 - *(a1+92)` |
| `sub_530E80` | ~2,000 | `IRNode_GetRegClass` | Identity function (returns argument unchanged) |
| `sub_4C28B0` | ~3,000 | `setBitfield` | Core SASS encoding: pack value into bit position |
| `sub_A50D10` | ~1,500 | `encodeRegId` | Translate virtual register ID to SASS encoding |

## Cross-References

### nvlink Internal
- [Embedded ptxas Overview](overview.md) -- full address map and architecture summary
- [IR Nodes](ir-nodes.md) -- IR node structure and accessor functions
- [Register Allocation](register-allocation.md) -- post-ISel register allocation pipeline
- [Scheduling](scheduling.md) -- instruction scheduling after ISel
- [Architecture Dispatch](arch-dispatch.md) -- how SM version selects which ISel backend to use
- [SM75 Turing](../targets/sm75-turing.md) -- SM75 ISel backend with 276 pattern matchers
- [SM80 Ampere](../targets/sm80-ampere.md) -- SM80 ISel backend with 259 pattern matchers
- [SM89 Ada](../targets/sm89-ada.md) -- SM89/90 shared ISel backend
- [Mercury Overview](../mercury/overview.md) -- MercExpand mega-hub at `sub_5B1D80`

### Sibling Wikis
- [ptxas: Instruction Selection](../../ptxas/codegen/isel.html) -- standalone ptxas ISel (two-phase: PTX-to-Ori + Ori-to-SASS)
- [ptxas: Mercury Encoder](../../ptxas/codegen/mercury.html) -- Mercury encoder pipeline (phases 113--122)
- [ptxas: SASS Encoding](../../ptxas/codegen/encoding.html) -- SASS instruction encoding format
