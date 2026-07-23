# ptxas Ori IR — recovered facts

Binary-derived documentation of ptxas v13.0.88's internal instruction representation
(project name "Ori"). Every fact recovered from the stripped ptxas binary
(`ptxas/ptxas`, 37.7 MB x86-64 ELF) and its decompilation.

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
- MEDIUM: modifier field exact width (only bits 20-23 observed extracted).
- Packed-word and instruction-object field bindings:
  - operand bit31 = OPD_DEF **is-destination** flag (mask 0x80000000), not sign/negate
    — HIGH.
  - operand type_tag bits28-30 = 3-bit **DAG-IR kind** (0=Unknown,1=VReg,2=Imm32,3=Imm64,
    4=Lab,5=Sym,6=Info,7=Null/sentinel) — HIGH for the enum; exact 13.0.88 lowered labels MED.
  - +32 = reserved/dead u32 (zero-init only, no reader); the SASS control word is not here
    — HIGH.
  - +40 sched_slot = pointer to a lazily-allocated polymorphic (vtable@0) per-inst
    scheduling/latency/barrier record holding the SASS control word — HIGH.
  - 32-byte ISel descriptor kinds (`operand_kind_enum.tsv`): 3=Imm, 9=cond-pred, 10=reg-alt-src
    HIGH; 13/14/15/16 MED; 4/5/7/8/11 = match-only variants (never emitted) LOW; the 1<->2
    register/predicate naming anchor is open (structure solid, name binding unresolved).
- The +128/+136 scheduler list is an OVERLAY over the operand region, used by
  operand-less scheduling pseudo-instructions; not a second always-present linkage.
