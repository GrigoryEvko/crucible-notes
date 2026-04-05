# IR Node Infrastructure

The IR node subsystem is the central data structure layer of the embedded ptxas backend. Every instruction, operand, and modifier field in the compiler pipeline is represented as an IR node or a field within one. Two functions dominate the entire binary by call count: `sub_530FB0` (`IRNode_GetOperand`, 31,399 callers) returns a pointer to the Nth operand of any IR node, and `sub_A49150` (`NVInst::getOperandField`, 30,768 callers) reads any named field from any instruction. Together they form the universal accessor layer through which every optimization pass, instruction selector, register allocator, and encoder interacts with instructions. This page documents the IR node structure layout, the operand slot format, the NVInst instruction object (a ~1,550-byte structure initialized by an 11 KB constructor), and the four giant operand dispatch switches that total 453 KB of compiled code.

## Key Functions

### IR Node Primitives (0x530E80--0x530FD0)

Twenty-two leaf functions at the bottom of the call graph. Most are single-instruction predicates or field extractors.

| Address | Name | Size | Callers | Role |
|---|---|---|---|---|
| `sub_530E80` | `IRNode_GetRegClass` | 16 B | ~1,000 | Returns register class field (identity function on offset +4) |
| `sub_530E90` | `IROperand_IsRegister` | 16 B | ~1,000 | Returns `type_tag == 2` |
| `sub_530EA0` | `IROperand_IsImmediate` | 16 B | many | Returns `type_tag == 1` |
| `sub_530EB0` | `IROperand_IsMemRef` | 16 B | many | Returns `type_tag == 6` |
| `sub_530EC0` | `IROperand_IsAddress` | 16 B | many | Returns `type_tag == 10` |
| `sub_530ED0` | `IROperand_IsPredicate` | 16 B | many | Returns `type_tag == 9` |
| `sub_530EE0` | `IROperand_IsCondCode` | 16 B | many | Returns `type_tag == 5` |
| `sub_530EF0` | `IROperand_IsConstExpr` | 16 B | many | Returns `type_tag == 4` (constant expression) |
| `sub_530F00` | `IROperand_IsSymbol` | 16 B | many | Returns `type_tag == 3` |
| `sub_530F10` | `IROperand_IsSymExpr` | 16 B | few | Returns `type_tag == 15` (symbol expression) |
| `sub_530F20` | `IROperand_IsCbufRef` | 16 B | few | Returns `type_tag == 13` (constant buffer reference) |
| `sub_530F30` | `IROperand_IsCbufAddr` | 16 B | few | Returns `type_tag == 14` (constant buffer address) |
| `sub_530F40` | `IROperand_IsCbufImm` | 16 B | few | Returns `type_tag == 16` (constant buffer immediate) |
| `sub_530F50` | `IROperand_IsBarrier` | 16 B | few | Returns `type_tag == 7` |
| `sub_530F60` | `IROperand_IsUReg64` | 16 B | few | Returns `type_tag == 11` (64-bit uniform register) |
| `sub_530F70` | `IROperand_IsTexRef` | 16 B | few | Returns `type_tag == 8` (texture/surface reference) |
| `sub_530F80` | `IRNode_GetDataType` | 16 B | many | Returns data type field (identity function on offset +20) |
| `sub_530F90` | `IRNode_SetFlagA` | 16 B | many | Writes byte at offset +14 |
| `sub_530FA0` | `IRNode_SetFlagB` | 16 B | many | Writes byte at offset +15 |
| `sub_530FB0` | `IRNode_GetOperand` | 16 B | **31,399** | Returns `*(a1+32) + 32*a2` (pointer to operand slot) |
| `sub_530FC0` | `IRNode_GetNumSrcOperands` | 16 B | many | Returns `*(a1+40) + 1 - *(a1+92)` |
| `sub_530FD0` | `IRNode_GetNumDstOperands` | 16 B | many | Returns `*(a1+92)` |

### NVInst Accessor Methods (0xA49010--0xA49F80)

Higher-level accessors on the instruction object. These operate on the full NVInst structure (not the minimal IR node used by ISel patterns).

| Address | Name | Size | Callers | Role |
|---|---|---|---|---|
| `sub_A49060` | `NVOperand::isIdentity` | 80 B | many | Checks if operand is a no-op via bitmask `0xFF6` and lookup table at `0x1E31200` |
| `sub_A490B0` | `NVInst::getPredicatePtr` | 16 B | many | Returns `*(a1+32) + 16` (predicate field pointer) |
| `sub_A490E0` | `NVInstrFormat::markInvalid` | 32 B | many | Sets flag bit 2, opcode = `0xFFFF` |
| `sub_A49100` | `NVInstrFormat::markUnimplemented` | 32 B | many | Sets flag bit 8, opcode = `0xFFFF` |
| `sub_A49120` | `NVInst::setOperandField` | 16 B | many | Thunk to `sub_A5B6B0` (180 KB dispatch) |
| `sub_A49130` | `NVInst::getDefaultOperandValue` | 16 B | many | Thunk to `sub_A67910` (141 KB dispatch) |
| `sub_A49140` | `NVInst::getOperandFieldRaw` | 16 B | many | Thunk to `sub_A709F0` (value getter) |
| `sub_A49150` | `NVInst::getOperandField` | 60 B | **30,768** | Calls `hasOperand` then `getValue`; returns `0xFFFFFFFF` if absent |
| `sub_A49190` | `NVInst::hasOperandField` | 16 B | many | Thunk to `sub_A7DE70` (existence check) |
| `sub_A491A0` | `NVInst::copyOperandField` | 48 B | many | `getValue` from src, `setOperandField` on dst |
| `sub_A491D0` | `NVInst::setOperandImm` | 16 B | many | Thunk to `sub_A62220` (65 KB dispatch) |
| `sub_A491E0` | `NVInst::getOperandFieldForSlot` | 16 B | many | Thunk to `sub_A65900` (67 KB dispatch) |
| `sub_A491F0` | `NVInst::copyOperandFieldForSlot` | 48 B | many | Get from src slot, set on dst slot |
| `sub_A49220` | `NVInstrFormat::lookupOpcodeDesc` | 96 B | many | FNV-1a hash lookup in opcode-to-descriptor table |
| `sub_A492B0` | `NVInstrFormat::lookupSubopDesc` | 96 B | many | Same hash lookup for sub-opcode descriptors |
| `sub_A49390` | `NVInst::addOperandSlot` | 256 B | many | Append 40-byte entry to operand vector (1.5x growth) |
| `sub_A49B50` | `NVInst::analyzeInstructionFlags` | 672 B | many | Sets classification bits in flags at offset +1136 |
| `sub_A49DF0` | `NVInst::decodeAndAnalyze` | 96 B | many | Decode via vtable then call `analyzeInstructionFlags` |

### Giant Operand Dispatch Switches (0xA5B6B0--0xA67910)

Four massive switch-on-opcode functions that implement per-instruction read/write of operand fields. Each switch has hundreds of cases (one per opcode ID), and each case contains a nested switch on field ID.

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_A5B6B0` | `setOperandField` dispatch | **180 KB** | Write path: sets named field to value |
| `sub_A62220` | `setOperandImm` dispatch | **65 KB** | Write path: sets immediate value for operand slot |
| `sub_A65900` | `getOperandField` dispatch | **67 KB** | Read path: extracts field value from encoding bits |
| `sub_A67910` | `getDefaultOperandValue` dispatch | **141 KB** | Returns default (reset) value for each field per opcode |

### NVInst Constructor

| Address | Name | Size | Role |
|---|---|---|---|
| `sub_A4AB10` | `NVInst::NVInst` | **11 KB** | 31-parameter constructor, allocates ~1,550-byte object |

## IR Node Structure Layout

The minimal IR node structure is accessed by the 22 leaf functions at `0x530E80`--`0x530FD0`. This is the structure passed to ISel pattern matchers and MercExpand handlers -- a lightweight view of any instruction or operand.

```
IRNode (minimum 96 bytes, exact total size unknown)
============================================================
Offset  Size  Field                  Description
------------------------------------------------------------
  0      1    type_tag               Operand type (1-16, see Operand Types below)
  4      4    reg_class              Register class ID; 1023 = wildcard "any"
 14      1    flag_a                 General-purpose flag byte A
 15      1    flag_b                 General-purpose flag byte B
 20      4    data_type              Data type / secondary encoding field
 28      2    opcode                 IR opcode (0xFFFF = invalid/terminator)
 32      8    operand_array_ptr      Pointer to operand array (each entry = 32 bytes)
 40      4    total_operand_count    Total number of operands (sources + destinations)
 92      4    first_src_index        Index of first source operand
```

Operand counts are derived:

- **Source operands** = `total_operand_count + 1 - first_src_index` (via `sub_530FC0`)
- **Destination operands** = `first_src_index` (via `sub_530FD0`)
- **Get operand N** = `operand_array_ptr + 32 * N` (via `sub_530FB0`)

The `+1` in the source operand formula means `total_operand_count` stores the index of the last operand (inclusive upper bound), not the count. If an instruction has 4 operands numbered 0-3, `total_operand_count` = 3 and `first_src_index` = 1 (one destination, three sources).

## Operand Slot Layout

Each operand occupies exactly 32 bytes within the operand array. The slot format is used both by the lightweight IR node accessors and by the giant dispatch switches.

```
OperandSlot (32 bytes)
============================================================
Offset  Size  Field                  Description
------------------------------------------------------------
  0      1    type_tag               Operand kind (see Operand Types)
  4      4    value                  Register number, immediate value, or symbol ID
  8      8    (pointer/extended)     Pointer to symbol, constant buffer, etc.
 16      1    modifier_a             Modifier byte 1 (negation, absolute value, etc.)
 17      1    modifier_b             Modifier byte 2 (type coercion, reuse flag, etc.)
 20      4    reg_class              Register class for this operand
 24      4    packed_encoding        Packed bitfield for instruction encoding
 28      4    constraint             Operand constraint ID
```

The `packed_encoding` field at offset +24 is the key data structure for the `getOperandField`/`setOperandField` dispatch functions. It stores multiple sub-fields as packed bit-fields. The 2-bit and 3-bit extractors (`sub_A4D1E0` and `sub_A4D150`) read sub-fields from this word (and from DWORD arrays for wider instructions) by bit position.

## Operand Types

The `type_tag` byte at offset +0 in both the IR node and each operand slot encodes the operand kind. All 16 values have been identified from the 22 leaf predicate functions at `0x530E80`--`0x530FD0`, the 10 operand initializer functions at `0x4C5F90`--`0x4C6DC0`, the two NVOperand constructors at `0xA49020`/`0xA49040`, and the ISel pattern matcher call sites.

| Tag | Name | Predicate | Initializer | Usage |
|---|---|---|---|---|
| 1 | **Immediate** | `sub_530EA0` | `sub_4C6380` | Literal integer or float value. 5-bit value field decoded from bitstream at offset +11 bits. |
| 2 | **Register** | `sub_530E90` | `sub_4C60F0` | General-purpose register (R0-R255). 4-bit register class from `dword_1D49260[0..11]`, 10-bit register number. |
| 3 | **Symbol** | `sub_530F00` | `sub_4C6DC0` | Label or function symbol; used in branch/call targets. Stores 2-bit mode field at `modifier_b` (+17), 3-bit sub-field at `modifier_a` (+16), and 8-bit value at `extended` (+8). |
| 4 | **ConstExpr** | `sub_530EF0` | `sub_4C6940` | Constant expression or address computation. 8-bit value at offset +4. Used by address-forming instructions. |
| 5 | **CondCode** | `sub_530EE0` | `sub_4C67B0` | Condition code operand (LT, EQ, GT, etc.). 8-bit encoded value at offset +4. |
| 6 | **MemRef** | `sub_530EB0` | `sub_4C6C60` | Memory reference (base + offset). 12-bit value at offset +4 encoding the addressing mode. |
| 7 | **Barrier** | `sub_530F50` | (inline) | Synchronization barrier operand. Used by BAR/DEPBAR instructions. |
| 8 | **TexRef** | `sub_530F70` | `sub_4C6AD0` | Texture or surface resource reference. 8-bit resource index at offset +4. Initializer identical to type 4 and type 5; distinguished solely by the type tag. Used by TEX/TLD/TXQ/SULD/SUST instructions (opcode families near 270). |
| 9 | **Predicate** | `sub_530ED0` | `sub_4C5F90` | Predicate register (P0-P6, PT). Created conditionally: bit test at position `8*param+1` in the instruction bitstream selects type 9 (predicate) vs type 1 (immediate). 5-bit register number from bitstream at offset `8*param+3`. |
| 10 | **Uniform** | `sub_530EC0` | `sub_4C60F0` | Uniform register (UR0-UR63). Uses the same initializer as type 2 but passed as `a5=10`. The initializer decodes 4-bit register class and 10-bit register number from distinct bitstream positions. Register value 1023 is the wildcard "any" class. |
| 11 | **UReg64** | `sub_530F60` | `sub_4C6500` | 64-bit uniform register pair. Simple operand created via `sub_4C6500` with `a5=11`. No register decoding -- the initializer only checks the destination/source bitmask. Appears as destination operand in 64-bit uniform load/store patterns (opcode 11). |
| 12 | **CbufReg** | (none) | `sub_4C60F0` | Constant-buffer-indexed register. Descriptor-init-only type: when `sub_4C60F0` receives `a5=12`, it overwrites `type_tag` to 2 (Register) and decodes a 6-bit register class from `dword_1D492A0[]` and a 9-bit doubled register number (511 maps to wildcard 1023). Never survives past init. |
| 13 | **CbufRef** | `sub_530F20` | `sub_4C6640` / `sub_A49020` | Constant buffer reference (`c[bank][offset]`). Created two ways: (1) `sub_4C6640` stores a 12-bit constant-buffer address in `extended` (+8); (2) `sub_A49020` stores a bank index in `extended` (+8) and zeros `modifier_b`. In ISel patterns, checked first -- the operand at position `first_src_index` is tested for type 13 before checking subsequent source operands. |
| 14 | **CbufAddr** | `sub_530F30` | `sub_A49040` | Constant buffer address with offset. `sub_A49040` sets `type_tag=14`, stores address value in `extended` (+8), and sets `modifier_b=2`. In ISel pattern matchers, typically checked in an OR pattern with type 16: `isType14(op) \|\| isType16(op)`. Appears as the last constant-buffer source operand in multi-source patterns. |
| 15 | **SymExpr** | `sub_530F10` | (operand conversion) | Symbol expression or resolved reference. In ISel pattern matchers, always checked in an OR pattern with type 3: `isSymbol(op) \|\| isType15(op)`. Semantically equivalent to Symbol but marks a resolved or elaborated form. Used in branch/call patterns where the target may be either a raw label (type 3) or a computed symbol expression (type 15). |
| 16 | **CbufImm** | `sub_530F40` | (operand conversion) | Constant buffer immediate offset. In ISel pattern matchers, always checked in an OR pattern with type 14: `isType14(op) \|\| isType16(op)`. Semantically similar to CbufAddr but represents an immediate (literal) constant-buffer offset rather than an address-register-based one. |

### Operand Type Tag Architecture Invariance

The type tag encoding is identical across all supported architectures. Three sets of per-arch clones of the predicate functions exist in the binary:

| Arch Group | Base Address | Tag Checks |
|---|---|---|
| Generic (ISel core) | `sub_530E90`--`sub_530F70` | All 15 tag predicates (types 1--11, 13--16) |
| SM75 (Turing) | `sub_F16040`--`sub_F160F0` | Identical tag values; same predicate logic |
| SM80 (Ampere) | `sub_CDD5F0`+ | Identical tag values (only `GetRegClass` clone present) |
| SM86 (Ada) | `sub_11E9CA0`--`sub_11E9CF0` | Identical tag values; 7 predicates cloned |

All clones test the same integer constants. No architecture adds, removes, or renumbers any tag.

### Identity Operand Table

The `NVOperand::isIdentity` function (`sub_A49060`) determines whether an operand holds its type's default (no-op) value. The logic:

```c
bool isIdentity(char *operand) {
    uint8_t tag = operand[0];
    if (tag > 11) return false;           // tags 12-16 are never identity
    if (((1LL << tag) & 0xFF6) == 0)      // bitmask: types 1,2,4,5,6,7,8,9,10,11
        return false;                      // excludes type 0 and type 3 (Symbol)
    uint8_t idx = tag - 2;
    int identity = (idx <= 9) ? dword_1E31200[idx] : 31;
    return operand[4] == identity;         // value at offset +4
}
```

The identity lookup table at `0x1E31200` (10 entries, indexed by `type_tag - 2`):

| Index | Tag | Identity Value | Meaning |
|---|---|---|---|
| 0 | 2 (Register) | 255 | RZ (zero register) |
| 1 | 3 (Symbol) | (excluded by bitmask) | -- |
| 2 | 4 (ConstExpr) | 0 | Null constant |
| 3 | 5 (CondCode) | 15 | Always-true condition |
| 4 | 6 (MemRef) | 0 | Null memory reference |
| 5 | 7 (Barrier) | 0 | No barrier |
| 6 | 8 (TexRef) | 0 | Null texture reference |
| 7 | 9 (Predicate) | 7 | PT (always-true predicate) |
| 8 | 10 (Uniform) | 63 | URZ (uniform zero register) |
| 9 | 11 (UReg64) | 31 | Default 64-bit uniform sentinel |

The identity values match the PTX ISA conventions: RZ is register 255, PT is predicate 7, URZ is uniform register 63. The bitmask `0xFF6` = `0b111111110110` excludes type 0 (unused) and type 3 (Symbol -- symbols have no identity value since every symbol is unique).

The companion function `sub_A48FE0` (`NVOperand::setToIdentity`) initializes an operand to its identity state:

```c
void setToIdentity(char *operand, uint8_t tag) {
    operand[0] = tag;                     // set type
    uint8_t idx = tag - 2;
    operand[4] = (idx <= 9) ? dword_1E31200[idx] : 31;
    operand[20] = 1;                      // reg_class = 1 (default)
}
```

### Operand Initializer Functions

The instruction descriptor init table uses 10 specialized initializer functions. Each allocates a 32-byte operand slot, sets the type tag, decodes register or value fields from the instruction bitstream at `inst+544`, and optionally updates the `first_src_index` counter at `a2+92`.

| Address | Name | Type Tag | Bitstream Fields |
|---|---|---|---|
| `sub_4C5F90` | `initPredicateOp` | 1 or 9 | Conditional on bit at `8*param+1`. If clear: type 1 (immediate), value from 5 bits at `8*param+3`. If set: type 9 (predicate), same value bits. |
| `sub_4C60F0` | `initRegisterOp` | 2, 10, or 12 | Type from `a5` param. For type 12 (CbufReg): overwritten to 2, 6-bit class from `dword_1D492A0[]`, 9-bit doubled reg number. For types 2/10: 4-bit class from `dword_1D49260[]`, 10-bit reg number. |
| `sub_4C6380` | `initImmediateOp` | `a5` | 5-bit value from bitstream at offset `a4+11`. Type passed directly as parameter. |
| `sub_4C6500` | `initSimpleOp` | `a5` | No value decode. Only checks destination bitmask for `first_src_index` update. Used for types 1, 11, and others. |
| `sub_4C6640` | `initCbufRefOp` | `a5` | 12-bit value at `extended` (+8) decoded from bitstream at position `a4`. Sets `modifier_a=0`, `modifier_b=0` on the companion slot. Always called with `a5=13`. |
| `sub_4C67B0` | `initCondCodeOp` | `a5` | 8-bit value at offset +4 from bitstream at `a4+1`. Always called with `a5=5`. |
| `sub_4C6940` | `initConstExprOp` | `a5` | 8-bit value at offset +4 from bitstream at `a4+1`. Always called with `a5=4`. Identical structure to `initCondCodeOp`. |
| `sub_4C6AD0` | `initTexRefOp` | `a5` | 8-bit value at offset +4 from bitstream at `a4+1`. Always called with `a5=8`. Identical structure to `initCondCodeOp` and `initConstExprOp`. |
| `sub_4C6C60` | `initMemRefOp` | `a5` | 12-bit value at offset +4 from bitstream at position `a4`. Sets `modifier_a=0`, `modifier_b=0` on companion slot. Always called with `a5=6`. Identical structure to `initCbufRefOp`. |
| `sub_4C6DC0` | `initSymbolOp` | `a5` | 2-bit mode at `modifier_b` (+17) from bitstream at `a4`. If mode==0: 3-bit field at `modifier_a` (+16) from `a4+3`, 8-bit value at `extended` (+8) from `a4+8`. If mode!=0: 3-bit field at `modifier_a` from `a4+3` only. Always called with `a5=3`. |

## NVInst Object Layout

The full NVInst instruction object is a ~1,550-byte structure initialized by the 11 KB constructor at `sub_A4AB10`. This constructor takes 31 parameters including an allocator, architecture ID, format ID, name string, mnemonic string, and various configuration values. The object is organized as ~97 x 16-byte (m128i) slots, many holding ref-counted pointers to sub-lists.

```
NVInst (~1550 bytes, 16-byte aligned)
============================================================
Offset   Size   Field                  Description
------------------------------------------------------------
   0       8    allocator              Pointer to arena allocator
  16      48    instr_list_node        Instruction linked-list (prev/next/head)
  64      32    bb_membership          Basic block membership node
  96      32    sched_graph_node       Scheduling dependency graph node
 128      32    operand_list           Ref-counted pointer to operand list
 132       4    flags                  Instruction flags (see Flag Bits below)
 160      32    output_operands        Ref-counted output operand list
 192       8    modifier_flags         Packed modifier bitfield
 200       4    arch_class_id          Architecture class identifier
 204       4    format_id              Instruction format identifier
 208       4    arch_id                SM architecture version
 224       8    opt_level              Optimization level (from dword_1E311D0 table)
 232       8    sched_mode             Scheduling mode (from dword_1D4AED0 table)
 240      32    name_str               Heap-allocated instruction name string
 272      48    mnemonic_str           Heap-allocated mnemonic string
 320      48    output_operand_list    Ref-counted list for output operands
 368      32    input_operand_list     Ref-counted list for input operands
 400       8    isa_vtable             ISA-specific virtual dispatch table pointer
 408       8    isa_backend            Pointer to ISA-specific backend object
 416      48    isa_state              ISA-specific state (initialized by vtable[7])
 464      48    predicate_chain        Ref-counted predicate chain list
 512      16    mem_dep_flags          Memory dependency classification flags
 528      32    sched_dep_list         Scheduling dependency ref-counted list
 560      ...   reg_file_state         Register file state, use-def chains, etc.
 976       8    operand_vec_data       Pointer to dynamic operand vector
 984       4    operand_vec_size       Current number of 40-byte operand entries
 988       4    operand_vec_capacity   Allocated capacity
1064       8    side_effect_cache      Cached side-effect analysis result
1080      72    instr_type_hashtable   FNV-1a hash table for instruction type lookups
1136       1    analysis_flags_lo      Classification bits (see Analysis Flags)
1137       1    analysis_flags_hi      Extended classification bits
```

### Construction Sequence

The constructor at `sub_A4AB10` follows this sequence:

1. Store the allocator pointer at slot 0.
2. Allocate 8+ ref-counted list objects for: instruction list, basic block list, operand list, use-def chains, modifier lists, register mappings. Each allocation calls a release helper (`sub_A4A050`--`sub_A4A3D0`) for exception safety.
3. Store architecture/format IDs at offsets 200--208.
4. Copy the name string via `strlen` + `memcpy` into offsets 240--271.
5. Copy the mnemonic string into offsets 272--319.
6. Allocate additional ref-counted lists for output operands, input operands, predicate operands, scheduling dependencies, and memory dependencies.
7. Call `sub_52DAB0(arch_id)` to obtain the ISA-specific vtable.
8. Store the vtable at offset 400 and call `vtable[7]` for ISA-specific initialization.
9. Initialize flag bytes, counters, and sentinel values (`0xFFFFFFFF`).
10. Compute optimization level from parameter `a5` via the `dword_1E311D0` lookup table (5 entries).
11. Compute scheduling mode from parameter `a7` via the `dword_1D4AED0` lookup table (4 entries).

### Instruction Flag Bits (offset +132)

| Bit | Meaning |
|---|---|
| 0 | `has_special_flag` -- instruction has a special processing flag |
| 1 | `invalid_encoding` -- set by `markInvalid` (`sub_A490E0`) |
| 3 | `unimplemented_opcode` -- set by `markUnimplemented` (`sub_A49100`) |
| 8 | `unimplemented_mark` -- checked by `needsRegisterAllocation` |
| 10 | `post_decode_validated` -- set after decode + analysis pass |

### Analysis Flag Bits (offset +1136)

Set by `sub_A49B50` (`analyzeInstructionFlags`) after instruction decoding:

| Bit | +1136 | Meaning | Trigger opcodes |
|---|---|---|---|
| 0 | low | `has_convergence_point` | Opcode 10 (SYNC) |
| 1 | low | `has_texture_op` | Opcode 35 |
| 2 | low | `is_barrier` | Opcode 246 |
| 3 | low | `is_branch` | Opcodes 60-61 |
| 4 | low | `is_memory_fence` | Opcodes 608-636 |
| 5 | low | `has_fp64` | DADD/DMUL/DFMA/DSETP (opcodes 155, 118, 90, 205) |
| 6 | low | `is_predicate_producer` | Opcodes 71, 130, 99 |
| 7 | low | `is_control_flow` | Opcodes 27, 294 |
| 0 | high | `tex_query_type_2504` | Field 485 present |
| 1 | high | `tex_query_type_2506` | Field 486 present |
| 2 | high | `custom_side_effect` | vtable at +416, offset 920 |

## Operand Dispatch Architecture

The four giant switch functions at `0xA5B6B0`--`0xA67910` are the read/write interface between the abstract IR operand model and the physical instruction encoding. They total 453 KB of compiled code. All four share the same architectural pattern: a top-level switch on `*(inst+12)` (the opcode ID, hundreds of cases), with each case containing a nested switch on the field ID.

### setOperandField (sub_A5B6B0, 180 KB)

```
void setOperandField(NVInst *inst, uint32_t field_id, uint32_t value);
```

Outer switch on opcode ID dispatches to ~200 per-opcode `sub_50Cxxx`/`sub_50Dxxx` setter functions. Each setter writes bit-fields into the instruction encoding bitstream (starting at offset +536 in the NVInst object). The bit extraction formula:

```c
// Writing a field at bit position pos with width w:
uint64_t *word = (uint64_t *)(inst + 8 * (pos >> 6) + 544);
uint64_t mask  = ((1ULL << w) - 1) << (pos & 0x3F);
*word = (*word & ~mask) | ((uint64_t)value << (pos & 0x3F));
```

Some fields are set via a normalized subtraction pattern: `setter(inst, value - base_value)` where `base_value` is the minimum legal value for that field (shared across many goto labels like `LABEL_3923` and `LABEL_3935`).

### setOperandImm (sub_A62220, 65 KB)

```
void setOperandImm(NVInst *inst, uint32_t slot_idx, uint32_t field_id, uint64_t value);
```

Accesses the operand slot at `*(inst+32) + 32*slot_idx`. Writes the immediate value into the `packed_encoding` word at offset +24 within the operand slot using XOR-swap:

```c
*(operand + 24) ^= (old_bits ^ new_bits) & mask;
```

Dispatches to per-opcode `sub_5195xx`--`sub_519Axx` setter functions.

### getOperandField (sub_A65900, 67 KB)

```
uint32_t getOperandField(NVInst *inst, uint32_t slot_idx, uint32_t field_id);
```

Reads from the `packed_encoding` word. Uses the 2-bit and 3-bit extractors:

- `sub_A4D1E0` (`extractBitField2`): extracts 2 bits at a given position, handles cross-DWORD boundary
- `sub_A4D150` (`extractBitField3`): extracts 3 bits, same cross-boundary handling

Typical return pattern: `extractBitField2(operand + 24, bit_offset) + base_value`, where `base_value` maps the extracted bits back to an enumeration value (e.g., register class ID).

Returns `0xFFFFFFFF` if the field is not present for the given opcode.

### getDefaultOperandValue (sub_A67910, 141 KB)

```
uint32_t getDefaultOperandValue(NVInst *inst, uint32_t field_id);
```

Structurally identical to `setOperandField` but calls setters with hardcoded constant values rather than user-supplied values. Used during instruction creation to initialize all fields to their default (identity/NOP) values. Example: `sub_50C9B0(inst, 1278)` sets a modifier field to its default encoding.

## The getOperandField Wrapper (sub_A49150)

This 60-byte function is the single most important accessor in the binary. With 30,768 callers, it is called from virtually every optimization pass, instruction selector, register allocator, and code generator:

```c
uint32_t NVInst_getOperandField(void *this, NVInst *inst, uint32_t field_id) {
    // sub_A7DE70: giant switch -- does the instruction have this field?
    if (hasOperand(inst, field_id)) {
        // sub_A709F0: giant switch -- extract the field value
        return getOperandValue(inst, field_id);
    }
    return 0xFFFFFFFF;  // field not present
}
```

The two-phase lookup (existence check then value extraction) means every field access performs two opcode switch dispatches. This is the cost of supporting a fully generic instruction representation that must handle 365+ distinct opcodes with varying operand schemas.

## Instruction Descriptor Init Table (0x920240--0xA48290)

943 template-instantiated functions, each initializing one instruction variant's descriptor. Every function follows this template:

1. **Opcode assignment**: `*(desc+12) = opcode_id` (range: 15--365+).
2. **Register class descriptor**: Load 16-byte mask from `.rodata` via SSE into `*(inst+8)`.
3. **Operand format tables**: Three parallel arrays of 10 DWORDs each:
   - `inst+24..+60`: operand register class IDs
   - `inst+64..+100`: operand constraint IDs
   - `inst+104..+140`: operand modifier IDs
4. **Operand count**: `*(inst+144) = N` (range: 1--16).
5. **Per-operand init calls**: N calls to type-specific initializers:

| Initializer | Operand type |
|---|---|
| `sub_4C5F90` | `initPredicateOp` -- Predicate register (type 9) or immediate (type 1); always last operand |
| `sub_4C60F0` | `initRegisterOp` -- Register (type 2), uniform (type 10), or cbuf-register (type 12, remapped to 2) |
| `sub_4C6380` | `initImmediateOp` -- Immediate value (type 1); 5-bit from bitstream |
| `sub_4C6500` | `initSimpleOp` -- Simple tag-only operand; used for UReg64 (type 11) and others |
| `sub_4C6640` | `initCbufRefOp` -- Constant buffer reference (type 13); 12-bit cbuf address |
| `sub_4C67B0` | `initCondCodeOp` -- Condition code (type 5); 8-bit value |
| `sub_4C6940` | `initConstExprOp` -- Constant expression (type 4); 8-bit value |
| `sub_4C6AD0` | `initTexRefOp` -- Texture/surface reference (type 8); 8-bit resource index |
| `sub_4C6C60` | `initMemRefOp` -- Memory reference (type 6); 12-bit addressing mode |
| `sub_4C6DC0` | `initSymbolOp` -- Symbol (type 3); 2-bit mode + 3-bit + 8-bit fields |

6. **Modifier field decoders**: N calls to `sub_50xxxx`/`sub_51xxxx` functions that extract modifier fields from the instruction bitstream at `inst+536..+560+`.

### Opcode Frequency

The most common opcodes, indicating which instruction families have the most encoding variants:

| Opcode | Variants | Likely mnemonic |
|---|---|---|
| 18 | 74 | IMAD (integer multiply-add) |
| 16 | 43 | IADD3 (integer add) |
| 56 | 39 | FFMA (float fused multiply-add) |
| 89 | 30 | LDG (global memory load) |
| 94 | 26 | MOV (register move) |
| 130 | 26 | HMMA (tensor core) |
| 200 | 25 | DADD (double-precision add) |
| 77 | 24 | (memory operation) |
| 27 | 24 | (control flow) |
| 41 | 23 | (ALU variant) |

Function size correlates directly with operand count: 2--4 operand instructions produce ~4 KB functions, 3--6 operands produce ~5 KB, 6--10 operands produce ~6 KB, and the most complex instructions (10--16 operands, e.g., LDG wide-register variants) produce ~7 KB functions.

## FNV-1a Hash Table Infrastructure

The instruction descriptor layer uses FNV-1a hash tables for opcode-to-descriptor lookups. The hash parameters:

- **Offset basis**: `0x811C9DC5` (standard FNV-1a 32-bit)
- **Prime**: `16777619` (`0x01000193`)
- **Application**: byte-by-byte on the key, XOR-then-multiply

Two specializations exist:

### FNVHash\<uint64_t\> (sub_A4B770)

- Key: 8 bytes (instruction ID or pointer)
- Node size: 168 bytes. Layout: `{next[0], key[8], value_data[16..160], hash[160]}`
- Value: 280 bytes copied via SSE (instruction metadata)
- Rehash trigger: `collision_count > entry_count AND entry_count > capacity/2`
- Growth: 4x current capacity

### FNVHash\<uint32_t\> (sub_A4C360)

- Key: 4 bytes (opcode ID)
- Node size: 32 bytes. Layout: `{next[0], key[8], value_ptr[16], hash[24]}`
- Value: pointer to 160-byte descriptor allocated via `sub_4FDC30`
- Same rehash strategy via `sub_A4C1D0`

Both implementations use bucket arrays with 24-byte buckets (`{head_ptr, tail_ptr, count}`) and singly-linked chains within each bucket.

## Bit-Field Extraction Helpers

Two small functions used extensively by the `getOperandField` dispatch:

- **`sub_A4D1E0` (extractBitField2)**: Extracts a 2-bit field from a packed DWORD array at a given bit position. Handles cross-DWORD boundary by combining shifted values from adjacent DWORDs.
- **`sub_A4D150` (extractBitField3)**: Same pattern for 3-bit fields, mask `& 7`.

Both return 0 if the field position exceeds the array bounds.

## Instruction Query Methods

The NVInst object provides a set of classification methods that other passes query:

| Function | Role | Logic |
|---|---|---|
| `sub_A49350` | `isCompactEncoding` | `*(inst+208) <= 0x3FFF` |
| `sub_A49360` | `isValidOpcode` | `(opcode - 122) <= 0xFF84` (not in invalid range) |
| `sub_A495B0` | `isMemoryBarrier` | Opcode 182 or bitmask `0x1140D` for opcodes 242--258 |
| `sub_A495E0` | `hasFlags` | `(flags & 1) \|\| (flags & 0xE)` at offset +132 |
| `sub_A496C0` | `isNOP` | No flags, not opcode 120, last operand type == 1, value == 31 + field 14 == 53 |
| `sub_A49720` | `isPredicated` | Last operand slot type == 9 (predicate) |
| `sub_A497F0` | `isCompareOp` | Opcode 194, 213, or `(opcode & ~4) == 131` (ISETP/DSETP/FSETP) |
| `sub_A49820` | `hasFP64Operands` | Opcodes 155, 118, or 90/205 with field 241 present |
| `sub_A49890` | `isFloatingPoint` | Opcodes 114-115, 216, 110, 195, 133, plus `hasFP64Operands` |
| `sub_A49930` | `isArithmetic` | All FP conditions plus opcodes 30-31, 13, 200, 264 |

## Use-Def Chain Infrastructure

The use-def chain structure (`sub_A49F80`, constructor at `0xA49F80`) is allocated as part of the NVInst's register file state. It initializes:

```
NVUseDefChain (104+ bytes)
============================================================
Offset  Size  Field
  0      4    def_id         Initially -1 (0xFFFFFFFF)
  4      4    flags          Initially 1
  8      8    parent_ptr     Pointer to owning NVInst
 16      4    sentinel       Initially 0xFFFFFFFF
 24     32    use_list       Ref-counted list of uses
 64     32    def_list       Ref-counted list of definitions
```

Use-def chains are the fundamental data structure for SSA-based optimizations in the ptxas backend.

## Read-Only Data Tables

| Address | Content | Size |
|---|---|---|
| `0x1F460E0` | Base register class descriptor table | Shared by all instructions |
| `0x1F461F0+` | Per-opcode register class descriptors | 16 bytes each |
| `0x1F46200+` | Per-opcode operand format arrays | Three 40-byte arrays |
| `0x1D492A0` | Constant buffer register class mapping | ~48 bytes |
| `0x1D49260` | Register operand class mapping | 12 entries |
| `0x1E31200` | Identity operand value table | 10 entries, indexed by type |
| `0x1E31240` | Register class size table | Indexed by format byte |
| `0x1E311D0` | Optimization level mapping | 5 entries |
| `0x1D4AED0` | Scheduling mode mapping | 4 entries |
