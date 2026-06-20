# ptxas Ori IR — recovered facts

Binary-derived documentation of ptxas v13.0.88's internal instruction representation
(project name "Ori"). Every fact recovered from the stripped ptxas binary
(`ptxas/ptxas`, 37.7 MB x86-64 ELF) and its decompilation; on any source/binary
disagreement the binary wins.

## Files

| File | Contents |
|---|---|
| `ir_node_layout.tsv` | 296-byte instruction object field map (offset, size, init evidence) |
| `opcode_enum.tsv` | 322 primary Ori opcodes (id 0-321), ROT13, sm_gen, ctor-verified flag |
| `opcode_enum_mercury.tsv` | 385 Mercury (SM103 tensor) extended opcode names |
| `operand_packed_word.tsv` | Packed inline operand lo/hi-word bitfield + dest/src split rule |
| `operand_kind_enum.tsv` | 32-byte ISel operand-descriptor kind enum (helper bank 0xB28E00-0xB28EF0) |

## Key recovery sources (binary)

- `sub_7DD010` (0x7dd010): instruction allocator — allocates exactly **296 bytes**,
  zero-init pattern gives the field map directly.
- `sub_BE7390` (0xbe7390): `InstructionInfo` constructor — populates the ROT13 opcode
  name table at object+4184, 16 bytes/entry. 321 of 322 entries extracted directly
  (index 94=LDS inlined by decompiler, confirmed via .rodata string scan at 0x1c395d1).
- `sub_BEBAC0` (0xbebac0): getName — `table + 4184 + 16*opcode`, proving opcode id ==
  name-table index (single numbering system, no remap).
- `sub_7E0030` / `sub_7E0650` / `sub_7E6090`: opcode mask (0xFFFFCFFF), operand type
  bits 28-30, dest/src split via `(opcode>>11)&2`, guard predicate = last operand type 6.
- `sub_7D62D0` / `sub_7D6320`: extended-operand store (+168 count = 2*N, data +172/+176).
- `sub_B28E00`-`sub_B28EF0`: operand-kind predicate helper bank (the kind enum).
- `sub_7EB4B0` / `sub_7EB830`: scheduler per-BB list at +128(next)/+136(prev) overlay.

## Confidence summary

- HIGH: 296-byte size, opcode field +72 & 0xFFFFCFFF mask, opcode id==table index,
  322 primary + 385 Mercury opcodes, operand array +84 (6x8B), operand type bits 28-30,
  guard=type-6-last-operand, bit-11 dest adjustment, ext-operand store, kind-enum membership.
- MEDIUM: meaning of individual operand kinds 3,4,5,7,8,9,10,11,13,14,15,16; modifier
  field exact width (only bits 20-23 observed extracted).
- LOW: +32 control_word and +40 sched_slot semantics on the instruction object
  (offsets heavily aliased by other context objects; not isolated to the instr in this pass).
- The +128/+136 scheduler list is an OVERLAY over the operand region, used by
  operand-less scheduling pseudo-instructions; not a second always-present linkage.
