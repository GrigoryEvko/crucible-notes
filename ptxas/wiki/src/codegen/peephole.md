# Peephole Optimization

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

The peephole optimization pass in ptxas is the single largest subsystem by code volume
in the entire binary.  Three monolithic dispatch functions -- totaling approximately
750 KB of machine code -- implement a brute-force pattern-match-and-rewrite engine
that recognizes instruction idioms in the internal IR and replaces them with more
efficient SASS instruction forms.  Each dispatch function serves a different
compilation context (generic, SM120-specific, and post-scheduling), but all three
share the same architecture: a giant opcode-based switch dispatches to hundreds of
pattern matchers; the highest-priority match wins; the winning rewrite modifies the
instruction in-place.

None of the three mega-dispatchers can be decompiled by Hex-Rays due to their
extreme size (233--280 KB each).  All analysis in this page derives from
disassembly, call graphs, and the 3,185 pattern-matcher functions that they invoke.

## Scale Summary

| Dispatch function | Binary size | Instructions | Pattern matchers | Primary opcodes hit | Secondary switches | Total switch cases | Largest template table | Entry trampoline | Context |
|-------------------|-------------|-------------|-----------------|--------------------:|-------------------:|-------------------:|----------------------:|-----------------|---------|
| `sub_169B190` | 280 KB | 65,999 | 762 | 249 / 373 | 110 | 2,347 | 245 (cases 0..244) | `sub_B12930` | Generic (all SM) |
| `sub_143C440` | 233 KB | ~56,241 | 1,087 | 203 / 373 | 85 | 1,971 | 190 (cases 0..189) | `sub_B12940` | SM120-specific |
| `sub_198BCD0` | 233 KB | 54,043 | 1,336 | 203 / 373 | 85 | 1,966 | 190 (cases 0..189) | `sub_B12960` | Post-scheduling |

All three primary switches dispatch over the same 0..372 opcode space, but the
**generic dispatcher recognizes 249 distinct opcodes**, while the SM120 and
post-schedule dispatchers each handle only 203 -- the remaining opcodes fall
through to the shared default in each pass.  The generic pass also reaches
**pattern/template ID 244** (its largest secondary table is the 245-case rewrite
selector at `0x169DC25`), whereas SM120 and post-schedule top out at template
ID 189 (190-case tables at `0x144503C` and `0x199488C` respectively).  This is
the deepest structural asymmetry between the three passes: the generic peephole
both observes more opcode classes and rewrites them into a wider template
namespace.  All three share an identical 72-case rewrite-action table (at
`0x143FB8B`, `0x16A166C`, `0x198F41B`) plus 50--52-case secondary tables that
gate medium-frequency rewrites.

All three entry trampolines (`sub_B12930`, `sub_B12940`, `sub_B12960`) are 11-byte
thunks that strip or forward one argument and tail-call the corresponding giant.

## Pipeline Position

```
 IR instruction stream
       |
       v
 sub_B12930 -----> sub_169B190   (generic peephole)
       |
       v
 sub_B12940 -----> sub_143C440   (SM120 peephole, RTX 50-series / Pro)
       |
       v
 [instruction scheduling]
       |
       v
 sub_B12960 -----> sub_198BCD0   (post-schedule peephole)
       |
       v
 [instruction encoding via vtable]
```

The generic and SM120 dispatchers run before scheduling; the post-scheduling
dispatcher runs after.  The SM120 dispatcher (`sub_143C440`) appears to be
architecture-gated -- it is called only when compiling for SM 120 targets
(consumer RTX 50-series, enterprise Pro GPUs).

## Dispatch Architecture

All three mega-dispatchers follow the same algorithm.

### Entry and primary switch

```
push callee-saves
sub  rsp, 10h
mov  rbp, rdi            ; ctx
mov  rbx, rsi            ; instruction node
mov  [rsp+var_2C], -1    ; best_template_id = NONE
mov  [rsp+var_30], -1    ; best_priority    = NONE
movzx edi, word [rsi+0Ch]; read opcode field
call sub_13B9DC0          ; identity / normalization (returns opcode)
cmp  ax, 174h             ; 373 cases (opcodes 0..372)
ja   default
jmp  [jump_table + rax*8] ; PRIMARY SWITCH on opcode
```

The 16-bit opcode at instruction node offset `+0x0C` selects a primary case.
All three dispatchers use 373-case primary switches.

### Per-case pattern matching

Within each primary case, the dispatcher:

1. Calls a sequence of pattern-matcher functions, passing pointers to
   `best_template_id` and `best_priority` as out-parameters.
2. Each matcher may update these if it finds a match with higher priority
   than the current best.
3. After all matchers for the opcode have run, the dispatcher checks
   `best_template_id`.  If it is no longer -1, a secondary switch on the
   template ID selects the rewrite action.

The secondary switches are embedded inside the giant function.  `sub_143C440`
contains 85 secondary jump tables (sizes 6--190 cases) totaling 1,598 secondary
cases (1,971 including the primary 373-case opcode switch); `sub_169B190`
contains **110** secondaries totaling 1,974 secondary cases (2,347 with the
primary), and `sub_198BCD0` mirrors SM120 with 85 secondaries and 1,593
secondary cases (1,966 with the primary).  In every dispatcher the largest two
secondary tables are the template-rewrite selector (190 or 245 cases) and the
72-case action subtable; the remaining tables are 6--52-case per-opcode
priority gates.

### Rewrite action

When a rewrite is selected, the action block performs four operations:

```c
setRewrittenOpcode(instr, new_opcode);     // sub_B28F10: writes byte at instr+14
setRewrittenModifier(instr, new_modifier); // sub_B28F20: writes byte at instr+15
setOperandMapping(instr, slot, value);     // sub_BA9CF0: writes instr+72+4*slot
markRewritten(instr);                      // sub_BA9C30 or sub_BA9CB0
```

`sub_BA9C30` (markRewrittenSimple) sets bit 0 of the flags word at `instr+140`:

```c
*(uint32_t*)(instr + 140) |= 1;
```

`sub_BA9CB0` (markRewrittenComplex) applies priority-aware flag logic that
respects existing rewrites from earlier passes -- it sets bits to `0x8`
("superseded") when a higher-priority rewrite exists.

The symmetry of call frequencies in `sub_143C440` confirms this: `setRewrittenOpcode`
and `setRewrittenModifier` are each called exactly 1,759 times -- every rewrite
always sets both the opcode and modifier bytes.

### Rewrite action value space

The 1,759 rewrite actions in `sub_143C440` use 392 distinct `(new_opcode, new_modifier)`
pairs.  `new_opcode` is a small ordinal (0--193, with a gap at 100--159);
`new_modifier` selects the encoding variant.  Top pairs by frequency:

| new_opcode | new_modifier | count | likely SASS semantics                 |
|:----------:|:------------:|:-----:|:--------------------------------------|
|   0x00     |   0x05       |  64   | identity / NOP-fold                   |
|   0x01     |   0x03       |  58   | 2-src ALU, modifier 3                 |
|   0x01     |   0x05       |  47   | 2-src ALU, modifier 5                 |
|   0x02     |   0x05       |  42   | 3-src FMA-class, modifier 5           |
|   0x03     |   0x03       |  38   | shift/logic, modifier 3               |
|   0x01     |   0x0A       |  36   | 2-src ALU, modifier 10 (wider)        |
|   0x01     |   0x13       |  32   | 2-src ALU, modifier 19 (packed/FP16)  |
|   0x04     |   0x05       |  31   | convert/move, modifier 5              |
|   0x00     |   0x02       |  29   | identity / NOP-fold, modifier 2       |
|   0x02     |   0x13       |  22   | 3-src FMA-class, modifier 19 (FP16)   |

The modifier namespace (22 distinct values observed) is dominated by six
encodings: 0x05 (24% of rewrites), 0x03 (19%), 0x13 (15%), 0x0A (11%),
0x0D (9%), and 0x02 (6%).

### Concrete rewrite examples from the binary

A typical rewrite block for `template_id == 3` at `0x143C5DA`:

```c
// Matched pattern: 4-operand instruction foldable to 2-src ALU
setRewrittenOpcode(instr,  1);   // new_opcode = 0x01
setRewrittenModifier(instr, 2);  // new_modifier = 0x02
setOperandMapping(instr, 0, 1);  // dest slot 0 <- source operand 1
setOperandMapping(instr, 1, 2);  // dest slot 1 <- source operand 2
markRewrittenComplex(instr);     // priority-aware flag (existing rewrite may exist)
```

A simpler rewrite for `template_id == 1` at `0x143C63D`:

```c
// Matched pattern: single-operand trivial fold
setRewrittenOpcode(instr,  0);   // new_opcode = 0x00
setRewrittenModifier(instr, 2);  // new_modifier = 0x02
markRewrittenComplex(instr);     // no operand remapping needed
```

The operand mapping count per rewrite varies:

| operand mappings | count | share |
|:----------------:|:-----:|:-----:|
| 0                | 1,193 |  68%  |
| 1                |   181 |  10%  |
| 2                |   269 |  15%  |
| 3                |    94 |   5%  |
| 4--5             |    22 |   1%  |

68% of rewrites use zero operand mappings -- the instruction's existing
operands remain in place and only the opcode/modifier bytes change (e.g.,
folding a redundant modifier or selecting a cheaper encoding).  The remaining
32% physically remap operand slots, typically collapsing a multi-source
pattern into fewer sources or swapping operand order.

Of the 1,759 rewrites, 1,251 (71%) use `markRewrittenSimple` (unconditional
flag set), and 363 (21%) use `markRewrittenComplex` (priority-aware); 145
(8%) fall through to a shared exit without an explicit mark call.

## Pattern Matcher Signature

Every one of the 3,185 pattern matchers shares the same prototype:

```c
char __fastcall match(
    int64_t ctx,           // a1: peephole optimization context
    int64_t instr,         // a2: instruction node being examined
    int32_t *template_id,  // a3: output -- combined opcode / template ID
    int32_t *priority      // a4: input/output -- current best priority
);
```

The function returns a char (the last comparison result, used for early-exit
optimization in the caller), but the meaningful outputs are `*template_id` and
`*priority`.

### Matching algorithm

Every matcher performs a deeply-nested chain of checks:

**Step 1 -- Modifier/property checks.**
Call `queryModifier(ctx, instr, slot)` (`sub_10AE5C0`) repeatedly.  Each call
returns an enumerated value for a specific instruction property:

```c
if (queryModifier(ctx, instr, 0xDC) != 1206) return 0;  // field 220: encoding type != .f32
if (queryModifier(ctx, instr, 0x163) != 1943) return 0;  // field 355: rounding != .rn
if (queryModifier(ctx, instr, 0x7E) - 547 > 1) return 0; // field 126: data type not in {.f32, .f64}
```

The slot indices are **field IDs** from the same flat property namespace used by
the ISel matchers (see [isel.md field-ID table](isel.md#dag-node-property-accessor----sub_10ae5c0)).
`queryModifier` is literally `DAGNode_ReadField` (`sub_10AE5C0`); peephole and
ISel share the same property-bag abstraction.  The 42 slot IDs observed in the
peephole matchers, with their decoded semantics, guard values, and value-to-PTX
modifier mappings:

| Slot | Dec | Semantic name | Observed guard values | Decoded meaning |
|------|-----|---------------|----------------------|-----------------|
| 0x05 |   5 | Ori opcode ID | 12 | Internal opcode number (e.g., 12 = tensor-class) |
| 0x7B | 123 | operation class | 536 | Major instruction family tag |
| 0x7E | 126 | data type qualifier | 547, 548 | 547 = `.f32`, 548 = `.f64`; range check `- 547 <= 1` |
| 0x88 | 136 | sub-operation modifier | 406--408, 598, 599 | Instruction sub-variant (e.g., ADD vs FADD vs FMUL) |
| 0x90 | 144 | FP precision class | 628, 629 | Range `- 628 <= 1`; selects FP16 vs FP32 operand path |
| 0xA1 | 161 | addressing mode variant | 700 | Memory addressing mode (register-indirect vs offset) |
| 0xBE | 190 | operation subtype | 815 | Zero-operand instruction subtype (NOP/barrier class) |
| 0xD2 | 210 | memory scope | 1177, 1181 | 1177 = `.gpu` (device), 1181 = `.sys` (system) scope |
| 0xD3 | 211 | memory ordering | 1181 | Acquire/release/relaxed semantics |
| 0xDC | 220 | encoding property (type) | 1206 | 1206 = `.f32` encoding tag for SASS bitfield selection |
| 0xF2 | 242 | width / size qualifier | 1281, 1282 | 1281 = 32-bit, 1282 = 64-bit operand width |
| 0x101 | 257 | async copy routing | 1332 | Async memory operation routing value |
| 0x119 | 281 | address space qualifier | 1435--1440 | `.global` / `.shared` / `.local` / `.const` (6 spaces) |
| 0x126 | 294 | constraint field | 1493 | Encoding constraint check |
| 0x127 | 295 | extended constraint | 1499 | Secondary encoding constraint |
| 0x142 | 322 | instruction form | 1800 | Instruction encoding form class |
| 0x152 | 338 | operand layout tag | 1871, 1873, 1874 | Source/dest operand slot arrangement variant |
| 0x155 | 341 | source modifier | 1881, 1882 | Range `- 1881 <= 1`; source operand modifier (abs/neg) |
| 0x159 | 345 | rounding mode selector | 1899--1903 | 1900 = `.rn`, 1901 = `.rz`, 1902 = `.rm`, 1903 = `.rp` |
| 0x15C | 348 | precision qualifier | 1912, 1915 | FP precision tag (`.tf32`, `.bf16`, etc.) |
| 0x163 | 355 | extended property (rnd) | 1943 | 1943 = `.rn` extended rounding mode confirmation |
| 0x167 | 359 | operand negation mask | 1957, 1961 | Source-operand sign-flip for FP instructions |
| 0x178 | 376 | extended property A | 2035 | Extended instruction property (tensor/MMA class) |
| 0x179 | 377 | extended property B | 2037--2041 | 5-value range; tensor layout variant |
| 0x18A | 394 | dual-issue / sched hint | -- | Scheduling hint for dual-issue eligibility |
| 0x18D | 397 | encoding validity stamp | 2115 | Post-ISel seal: `0x843` = bits {0,1,6} set in SASS dword 0 |
| 0x196 | 406 | MMA type A | 2146 | Matrix multiply source type A (FP16/BF16/TF32/INT8) |
| 0x197 | 407 | MMA type B | -- | Matrix multiply source type B |
| 0x199 | 409 | MMA accumulator type | -- | Accumulator precision for tensor ops |
| 0x19D | 413 | extended qualifier | 2167, 2168 | 2-value discriminator for tensor instruction shape |
| 0x1A8 | 424 | uniform register hint | 2214--2225 | Bitmask 739; uniform register allocation eligibility |
| 0x1AD | 429 | extended qualifier B | 2253--2257 | 5-value range; tensor instruction extended modifier |
| 0x1AE | 430 | warp shuffle mode A | -- | SHFL sub-operation variant |
| 0x1AF | 431 | warp shuffle mode B | -- | SHFL companion modifier |
| 0x1B2 | 434 | FP composition | 2274 | FP instruction composition (fused vs separate) |
| 0x1D1 | 465 | codegen control A | -- | Code generation control property |
| 0x1D2 | 466 | codegen control B | -- | Code generation control property |
| 0x1E0 | 480 | encoding format class | 2478--2481 | Selects SASS encoding format (3-src vs imm vs reg-reg) |
| 0x1E4 | 484 | extended modifier C | -- | Blackwell-era extended property |
| 0x1EC | 492 | extended modifier D | -- | Blackwell-era extended property |
| 0x216 | 534 | MMA layout descriptor | 2717 | HMMA/DMMA matrix layout and tiling descriptor |
| 0x253 | 595 | extended field (max) | 2937, 2938 | Highest field ID observed; tensor instruction tag |

Values are drawn from a global enumeration (~2,850 entries, max 2829) that is
**disjoint** from the field-ID namespace.  The encoding bitfield LUT at VA
`0x23F2E00` maps value IDs to bit positions and masks in the SASS instruction
word.  The most frequently read slots in the peephole matchers are 0x159
(rounding, ~400 reads), 0x7E (data type, ~300), 0x88 (sub-op, ~200), and
0x18D (validity stamp, ~150).

**Step 2 -- Operand count.**
Check the number of explicit/fixed operands and the total operand slot count:

```c
int fixed = getExplicitOperandCount(instr);  // sub_B28F50: returns *(instr+92)
int total = getTotalOperandSlots(instr);     // sub_B28F40: returns *(instr+40)+1 - *(instr+92)
```

**Step 3 -- Operand type and register class validation.**
For each operand slot, retrieve the operand pointer and check its kind:

```c
void *op = getOperand(instr, idx);   // sub_B28F30: returns *(instr+32) + 32*idx
byte kind = *(byte*)op;
if (!isRegister(kind))   return 0;   // sub_13B9CD0: kind == 2
if (!isImmediate(kind))  return 0;   // sub_13B9CE0: kind == 1 (alt check)
```

Register class is checked against expected values:

```c
int regclass = getRegisterClass(*(uint32_t*)(op + 4)); // sub_13B9CC0
if (regclass != 1023 && regclass != 1) return 0;       // 1023 = wildcard
```

**Step 4 -- Priority gate.**
If all checks pass and the current priority allows it:

```c
if (*priority <= threshold) {
    *priority = threshold + 1;
    *template_id = combined_opcode_id;
}
```

Since matchers are called sequentially and each checks the running maximum,
the highest-priority match always wins.

## Operand Type Discriminators

Three families of trivial single-instruction functions serve as operand type
predicates, one family per dispatch context:

### SM120 matchers (Zone A of `sub_143C440`)

| Function | Test | Semantic |
|----------|------|----------|
| `sub_13B9CD0` | `kind == 2` | isRegister |
| `sub_13B9CE0` | `kind == 1` | isImmediate |
| `sub_13B9D00` | `kind == 2 \|\| kind == 1` | isRegOrImm |
| `sub_13B9D10` | `kind == ?` | isConstantBuffer |
| `sub_13B9D40` | `kind == ?` | isPredicate |
| `sub_13B9D50` | `kind == ?` | isUniformRegister |
| `sub_13B9CC0` | extracts class | getRegisterClass (1023 = wildcard) |

### Generic matchers (Zone A of `sub_169B190`)

| Function | Test | Semantic |
|----------|------|----------|
| `sub_15F59C0` | `a1 == 2` | isRegister |
| `sub_15F59D0` | `a1 == 1` | isImmediate |
| `sub_15F59E0` | `a1 == 0` | isNone |
| `sub_15F59F0` | `a1 == 10` | isConstantMemory |
| `sub_15F5A00` | `a1 == 9` | isTexRef |
| `sub_15F5A30` | `a1 == 3` | isPredicate / isConstImm |
| `sub_15F5A40` | `a1 == 15` | isUniformRegister / isTrueConst |
| `sub_15F5A80` | `a1 == 6` | isLabel |
| `sub_15F5A90` | `a1 == 11` | isTexture |
| `sub_15F5AB0` | identity | getOperandValue |

### Post-schedule matchers (Zone A of `sub_198BCD0`)

| Function | Test | Semantic | Call count |
|----------|------|----------|------------|
| `sub_1820170` | identity | getOpcodeRaw | 9,278 |
| `sub_1820180` | `a1 == 2` | isRegOperand | 2,743 |
| `sub_1820190` | `a1 == 1` | isImmOperand | 677 |
| `sub_18201A0` | `a1 == 8` | isUniform | 7 |
| `sub_18201B0` | `a1 == 10` | isPredicateReg | 1,228 |
| `sub_18201C0` | `a1 == 9` | isTexRef | 211 |
| `sub_18201D0` | `a1 == 5` | isConstBuf | 14 |
| `sub_18201E0` | `a1 == 4` | isAddress | 9 |
| `sub_18201F0` | `a1 == 3` | isConstImm | 1,044 |
| `sub_1820200` | `a1 == 15` | isTrueConst | 1,044 |
| `sub_1820210` | `a1 == 7` | isBarrier | 9 |
| `sub_1820220` | `a1 == 12` | isSurface | 12 |
| `sub_1820230` | `a1 == 11` | isTexture | 12 |
| `sub_1820240` | `a1 == 6` | isLabel | 2 |
| `sub_1820250` | `a1 == 14` | isSpecialReg | 2 |
| `sub_1820260` | `a1 == 13` | isUnknown | 6 |

## Priority System

Matchers use a strict numeric priority to resolve conflicts when multiple
patterns match the same instruction.  Higher priority means more specific
and/or more profitable transformation.

| Priority range | Description | Example |
|---------------|-------------|---------|
| 1--2 | Trivial matches (simple mov, basic arithmetic) | Single-operand passthrough |
| 5--11 | Common 2--3 operand combining patterns | Standard FMA combines |
| 14--20 | Complex 4-operand patterns with constraints | Multi-source ALU combines |
| 22--31 | Highly specific multi-operand patterns | Wide register + predicated ops |
| 33--36 | Maximum specificity (8--9 operands + all modifiers) | Full tensor instruction forms |

Pattern IDs occupy disjoint ranges per dispatcher: the generic pass reaches
template ID **244** (245-case rewrite table at `0x169DC25`), while SM120 and
post-schedule cap at template ID **189** (190-case tables at `0x144503C` and
`0x199488C`).  Multiple matchers can target the same pattern ID with different
priorities, creating a priority cascade.

## Instruction Node Layout

The peephole subsystem reveals the following fields of the instruction IR node:

| Offset | Size | Field | Accessor |
|--------|------|-------|----------|
| `+0x00` | 1 B | Operand type tag | `isRegister`, `isImmediate`, etc. |
| `+0x04` | 4 B | Primary value (register number / immediate) | `getRegisterClass` / `getOperandValue` |
| `+0x0C` | 2 B | Opcode number (16-bit) | Direct read in dispatch entry |
| `+0x0E` | 1 B | Rewritten opcode | `sub_B28F10` (setRewrittenOpcode) |
| `+0x0F` | 1 B | Rewritten modifier | `sub_B28F20` (setRewrittenModifier) |
| `+0x14` | 4 B | Secondary register field | Direct read |
| `+0x20` | 8 B | Operand array base pointer | `sub_B28F30` base address |
| `+0x28` | 4 B | Total operand count | Part of `sub_B28F40` computation |
| `+0x48` | var | Operand mapping table (4 B per slot) | `sub_BA9CF0` writes here |
| `+0x5C` | 4 B | Explicit operand count | `sub_B28F50` returns this |
| `+0x8C` | 4 B | Flags word | Bit 0 = rewritten (set by `sub_BA9C30`) |

Each operand is a 32-byte record at `base + 32 * index`:

| Operand offset | Size | Content |
|---------------|------|---------|
| `+0` | 1 B | Type tag (1=imm, 2=reg, 3=constImm, 10=pred, 15=trueConst, ...) |
| `+4` | 4 B | Primary value (register ID; 1023 = wildcard / any-reg) |
| `+20` | 4 B | Secondary value (modifier / sub-register) |

## Code Duplication

The pattern matchers exhibit extreme structural duplication.  Groups of 2--10
functions are near-identical clones differing only in numeric constants (the
specific opcode/modifier values they check, the template ID they assign, and
the priority level).

Observed clone clusters in `sub_169B190`'s matchers:

| Cluster size | Count | Byte size each | Address range example |
|-------------|-------|----------------|----------------------|
| ~5,560 B | 5 functions | 5,560 | `0x167CBB0`--`0x16E7D20` |
| ~5,282 B | 10 functions | 5,282 | `0x167E3A0`--`0x16807E0` |
| ~5,298 B | 4 functions | 5,298 | `0x16EA5F0`--`0x16ECA30` |
| ~5,846 B | 3 functions | 5,846 | `0x16EDC00`--`0x16EE8B0` |
| ~2,718 B | 7 functions | 2,718 | `0x166F260`--`0x1692B60` |
| ~2,604 B | 6 functions | 2,604 | `0x166AC30`--`0x166E170` |

Similarly, in `sub_198BCD0`'s matchers, eight functions of exactly 5,282 bytes
each (`sub_1982810`, `sub_1982AE0`, `sub_1982DB0`, `sub_1983080`,
`sub_1984B40`, `sub_1984E10`, `sub_19850E0`, `sub_19853B0`) share identical
structure, varying only in the opcode/modifier constants passed to
`sub_10AE5C0`.

This strongly suggests compiler-generated code from C++ templates or macros
that instantiate one matcher function per instruction variant from ISA
specification tables -- a pattern consistent with NVIDIA's internal build
tooling.

## Size Distribution of Matchers

### SM120 matchers (1,087 functions, 429 KB)

| Size range | Count | Description |
|-----------|-------|-------------|
| < 200 B | 37 | Simple 1--2 modifier checks |
| 200--400 B | 520 | Typical 4--8 modifier checks |
| 400--600 B | 455 | 6--12 modifier checks + operand validation |
| 600--800 B | 66 | Complex multi-operand patterns |
| > 800 B | 9 | Deepest nesting, most constrained patterns |

### Generic matchers (762 functions, ~310 KB)

| Size range | Count | Description |
|-----------|-------|-------------|
| ~2,200 B | most common | 2--4 instruction field checks |
| ~2,800 B | moderate | Patterns with operand constraints |
| ~3,500--4,000 B | fewer | Complex multi-operand patterns |
| ~5,500--8,500 B | rare | 12+ modifier checks, 8--9 operands |

### Post-schedule matchers (~1,336 functions)

| Size range | Count | Description |
|-----------|-------|-------------|
| ~2,200 B | most common | Simple 2-instruction patterns |
| ~2,500 B | common | 3-instruction patterns |
| ~3,100 B | moderate | Patterns with predicate checks |
| ~5,300 B | few | Multi-instruction sequences (8+ operands) |
| ~6,800 B | 1 | Largest matcher (`sub_1980D10`) |

## Representative Matcher Examples

### Simplest: `sub_143C3B0` (132 bytes, priority 2, template 1)

Checks: no explicit operands, 2 total slots, first operand is register-or-immediate
with register class 1023 or 1.  Matches a trivial `mov`-type instruction for
passthrough combining.

### Moderate: `sub_13CF0C0` (426 bytes, priority 15, template 28)

Checks 5 modifiers: slot 0xD3 == 1181, slot 0xD2 == 1177, slot 0x0C == 59,
slot 0xB3 == 772, slot 0xC8 == 1107.  Then validates 1 explicit register operand
plus 4 additional operands (register, register, immediate, predicate).

### Complex: `sub_1615980` (priority 36, template 25 -- highest observed priority)

Checks 12 modifier slots: 0x05 == 12, 0xDC == 1206, 0x253 in {2937,2938},
0x126 == 1493, 0xF2 in {1281,1282}, 0x163 == 1943, 0x178 == 2035,
0x179 in {2037..2041}, 0x1AD in {2253..2257}, 0x7E in {547,548},
0x19D in {2167,2168}, 0x18D == 2115.  No fixed operands, 7 variable operands,
each of type 10 (constant memory) with register class 1023 or specific flag
constraints.  This is the most constrained pattern observed -- likely a fully
specified tensor instruction variant.

### Post-schedule: `sub_1834600` (pattern 17, priority 16)

Checks modifier slots 0xD3 == 1181, 0xD2 == 1177, 0x0C in {60,61}, 0xB3 == 772,
0xC8 == 1107.  Then: first operand offset == 1, that operand is immediate, total
operand count == 5, followed by register pattern checks.

## Infrastructure Helper Functions

### Core accessor (`sub_10AE5C0`, 60 bytes)

The single most-called function in the peephole subsystem (30,768 callers
across the full binary).  Queries a property of an instruction node by slot ID:

```c
int queryModifier(int64_t ctx, int64_t instr, int slot) {
    if (hasProperty(instr, slot))        // sub_10E32E0
        return getPropertyValue(instr, slot); // sub_10D5E60
    return 0xFFFFFFFF;                   // property not present
}
```

### Node accessors

| Function | Size | Semantics | Call frequency |
|----------|------|-----------|---------------|
| `sub_B28F30` | 12 B | `getOperand(instr, idx)` -- returns `*(instr+32) + 32*idx` | 31,399 |
| `sub_B28F40` | 10 B | `getTotalOperandSlots(instr)` -- returns `*(instr+40)+1 - *(instr+92)` | ~2,500 |
| `sub_B28F50` | 4 B | `getExplicitOperandCount(instr)` -- returns `*(instr+92)` | ~2,100 |

### Rewrite helpers

| Function | Semantics | Call frequency in `sub_143C440` |
|----------|-----------|-------------------------------|
| `sub_B28F10` | `setRewrittenOpcode(instr, byte)` -- writes `instr[14]` | 1,759 |
| `sub_B28F20` | `setRewrittenModifier(instr, byte)` -- writes `instr[15]` | 1,759 |
| `sub_BA9CF0` | `setOperandMapping(instr, slot, val)` -- writes `instr[72+4*slot]` | 993 |
| `sub_BA9C30` | `markRewrittenSimple(instr)` -- `instr[140] \|= 1` | 1,222 |
| `sub_BA9CB0` | `markRewrittenComplex(instr)` -- priority-aware flag update | 361 |

The ratio of `markRewrittenSimple` (1,222) to `markRewrittenComplex` (361)
shows that approximately 77% of rewrites are straightforward replacements,
while 23% involve priority negotiation with competing rewrites.

## Call Frequency in `sub_169B190` (Generic Dispatcher)

| Callee | Count | Role |
|--------|-------|------|
| `sub_B28F10` (setRewrittenOpcode) | 2,142 | Write new opcode byte |
| `sub_B28F20` (setRewrittenModifier) | 2,142 | Write new modifier byte |
| `sub_15F59B0` (getOperandValue) | 1,736 | Extract register number |
| `sub_10AE5C0` (queryModifier) | 1,303 | Read instruction property |
| `sub_B28F30` (getOperand) | 1,281 | Get operand pointer |
| `sub_BA9C30` (markRewrittenSimple) | 1,261 | Simple rewrite commit |
| `sub_BA9CF0` (setOperandMapping) | 855 | Map operand slots |
| `sub_BA9CB0` (markRewrittenComplex) | 589 | Priority-aware commit |

## Relationship to Instruction Encoding

Each dispatch function's address range is adjacent to a zone of SASS instruction
encoders that consume the rewritten instructions:

- `sub_143C440` (SM120) sits before 123 SM120 encoders at `0x14771E0`--`0x14A3C80`
  (180 KB), covering 82 unique SASS opcodes with up to 42 encoding variants per
  opcode.
- `sub_169B190` (generic) sits before 100 encoding table entries at
  `0x16DF750`--`0x16FFFF0` and 36 template expanders at `0x1700000`--`0x1722D60`.
- `sub_198BCD0` (post-schedule) operates on already-scheduled instructions,
  performing strength reduction and idiom recognition on the final instruction stream.

The encoders are called via vtable dispatch, not directly from the peephole
functions.  Each encoder packs a 128-bit SASS instruction word using
`sub_7B9B80(state, bit_offset, bit_width, value)` for bit-field insertion.

## Function Map

| Address | Size | Identity | Confidence |
|---------|------|----------|---|
| `sub_B12930` | 11 B | Entry trampoline for generic peephole | CERTAIN |
| `sub_B12940` | 11 B | Entry trampoline for SM120 peephole | CERTAIN |
| `sub_B12960` | 11 B | Entry trampoline for post-schedule peephole | CERTAIN |
| `sub_169B190` | 280 KB | Generic peephole mega-dispatcher | HIGH |
| `sub_143C440` | 233 KB | SM120 peephole mega-dispatcher | HIGH |
| `sub_198BCD0` | 233 KB | Post-schedule peephole mega-dispatcher | HIGH |
| `sub_10AE5C0` | 60 B | queryModifier(ctx, instr, slot) | HIGH |
| `sub_B28F10` | small | setRewrittenOpcode(instr, byte) | HIGH |
| `sub_B28F20` | small | setRewrittenModifier(instr, byte) | HIGH |
| `sub_B28F30` | 12 B | getOperand(instr, idx) | CERTAIN |
| `sub_B28F40` | 10 B | getTotalOperandSlots(instr) | CERTAIN |
| `sub_B28F50` | 4 B | getExplicitOperandCount(instr) | CERTAIN |
| `sub_BA9C30` | small | markRewrittenSimple(instr) | HIGH |
| `sub_BA9CB0` | small | markRewrittenComplex(instr) | HIGH |
| `sub_BA9CF0` | small | setOperandMapping(instr, slot, value) | HIGH |
| `sub_13B9CC0` | small | getRegisterClass(field) | HIGH |
| `sub_13B9CD0` | small | isRegister(byte) | HIGH |
| `sub_13B9CE0` | small | isImmediate(byte) | HIGH |
| `sub_13B9D00` | small | isRegisterOrImmediate(byte) | HIGH |
| `sub_13B9D10` | small | isConstantBuffer(byte) | HIGH |
| `sub_13B9D40` | small | isPredicate(byte) | HIGH |
| `sub_13B9D50` | small | isUniformRegister(byte) | HIGH |
| `sub_13B9DC0` | small | opcodeIdentity(uint) -- passthrough | CERTAIN |
| `sub_1909030` | small | opcodePassthrough (post-schedule context) | HIGH |

## Macro Instruction Expansion (`sub_8127C0`)

Separate from the three pattern-match-and-rewrite mega-dispatchers, ptxas contains
a dedicated macro instruction expansion pass at `sub_8127C0` (10,720 bytes).  This
pass resolves register-file constraints for composite instructions -- cases where
source or destination operands span register files or where multi-word results need
splitting into narrower instruction sequences.

It is called from the master lowering dispatcher `sub_8380A0` and runs before
instruction scheduling.

### Two-phase algorithm

**Phase 1 -- Operand scanning and constraint annotation.**
The pass iterates every instruction in the function's linked list (traversing via
`instr+8`).  For each instruction, it reads the opcode at `instr+72` and dispatches
through a 15-family if-else cascade.  For each opcode, it calls
`sub_812550` (`getOperandConstraint`) on each source operand to determine
register-file affinity:

| Return value | Meaning |
|-------------|---------|
| 0 | Unconstrained |
| -2 | Constrained to register file B (e.g., even-aligned pair) |
| -3 | Constrained to register file A (e.g., odd-aligned pair) |
| -1 | Conflict / unresolvable |

The pass annotates register descriptor entries (indexed via `ctx+88`) at `reg+76`
(constraint code) and `reg+80` (target width code), and builds a linked list of
instructions requiring expansion (linked via `instr+56`).  Registers consumed by
expansion are marked dead (`reg+64 = 5`).

**Phase 2 -- Instruction rewriting.**
If `v187 == 0` (no expansion needed), phase 2 is skipped.  Otherwise a cleanup
loop purges the worklist: it reads both operand constraint codes, applies modifier-
bit adjustments (bits 25-31 can transform -2 to -3/-1), and removes entries where
both resolve to -1.  The loop repeats until no further removals (fixed-point).
The rewrite loop then dispatches by opcode:

```
for each instr on expansion_worklist (linked via instr+56):
  opc = instr[72]; dst = &instr[84]; src0 = &instr[92]
  src1 = &instr[100]; src2 = &instr[108]; reg = regArray[dst & 0xFFFFFF]
  switch opc:

  case 36 (I2IP), 201 (QMMA_16816), 202 (QMMA_16832):
    cA = getOperandConstraint(ctx, src0)           // sub_812550
    if cA not in {-2,-3}: skip
    w = (cA==-2) ? dword_21D5EE0[src2 & 0xFFFFFF] : dword_21D5F60[src2 & 0xFFFFFF]
    resolved = resolveOperandType(ctx, src0, w)    // sub_800360
    if opc==201: instr[108] = resolved | 0x60000000
    else:        patch slot at nOps + ~((opc>>11) & 2)
    recomputeProperties(ctx, instr)                // sub_92C0D0
    if reg.file != 5:                              // dest still live
      sched = sm_backend->getSchedClass(instr[76]) // vtable[113]
      if opc==202: emit_barrier(ctx, dword_21D6390[(pen>>9)&0xF], 20, ...)
      emit_4op(ctx, 36, sched, dst, src0, RZ, resolved)  // sub_930040
      deleteInstruction(ctx, instr)                // sub_9253C0

  case 18 (FSETP):                                 // nOps==6, modifier filter
    finalizeOperand(ctx, &instr[116])              // sub_800400

  case 29 (PMTRIG), 95/96 (STS/LDG), 190 (LDGDEPBAR), bit-12 set:
    if !sub_747EE0(instr): skip
    idx = nOps + ~((opc>>11) & 2)                  // last-operand index
    pen = &dst[2*idx]; prev = pen-2
    pen[0] = resolveOperandType(ctx, prev, pen[0] & 0xFFFFFF) | 0x60000000
    pen[1] = 0

  case 130 (HSET2), 137 (SM73_FIRST):
    cA = getOperandConstraint(ctx, src0)
    if cA not in {-2,-3}: finalizeOperand(ctx, src0); skip
    w = lookup(cA, src2)                           // same two tables
    if canExpandStoreChain(ctx, instr):            // sub_8125E0
      instr[108] = resolved | 0x60000000           // in-place patch
    else:
      emit_MOV(ctx, 130, 20, dst, src0)            // sub_92E720
      emit_store(ctx, 201, instr[76], dst, src0)   // sub_92FF10
      deleteInstruction(ctx, instr)

  case 149 (UFLO):
    if getOperandConstraint(ctx, dst) in {-2,-3}:
      reg.file = 5; if both srcs dead: instr[76] = 20
    elif sub_7D6780(regArray[src0].width):          // vectorize path
      emit_I2IP(ctx, 36, instr[76], dst, src0, neg, RZ)
      deleteInstruction(ctx, instr)
    else: finalizeOperand(ctx, src0)

  case 10 (SHF), 151 (UIMAD), 290 (MOV sm_104):   // three-source
    cS0 = getOperandConstraint(ctx, src0)
    cS1 = getOperandConstraint(ctx, src1)
    if both dead: finalizeOperand both; instr[76] = 20
    elif one live, one file==5:
      emit_I2IP(ctx, 36, w, dst, resolved, RZ, const)
      emit_replacement(ctx, opc, w, dst, src0, src1)
      deleteInstruction(ctx, instr)
    else: finalizeOperand(ctx, src0); finalizeOperand(ctx, src1)

  case 283 (UVIADD):                               // simple extraction
    pen = &dst[2*(nOps-2)]; prev = pen-2
    pen[0] = resolveOperandType(ctx, prev, pen[0] & 0xFFFFFF) | 0x60000000
    pen[1] = 0
```

Register-file mapping uses `dword_21D5EE0[26]` (constraint -2, width codes 0--25)
and `dword_21D5F60[16]` (constraint -3, width codes 0--15).  `dword_21D6390[]`
(indexed by `(operand >> 9) & 0xF`) selects the barrier variant for QMMA_16832.

### Opcodes handled

| Opcode | Mnemonic | Expansion pattern |
|--------|----------|-------------------|
| 10 | `SHF` | Three-source constraint check; emits `I2IP` (36) + new `SHF` when sources span register files |
| 18 | `FSETP` | Predicate operand finalization when operand count == 6 and modifier bits match |
| 29 | `PMTRIG` | Last-operand extraction and finalization |
| 36 | `I2IP` | Destination register marking and two-source constraint checking |
| 60 | `LEPC` | Store/load legalization: validates flags, checks register file == 6, recursive chain validation via `sub_812480` |
| 62, 78, 79 | `BAR_INDEXED`, `RTT`, `BSYNC` | Same legalization path as `LEPC` |
| 95, 96 | `STS`, `LDG` | Last-operand extraction for stores; two-source vector-width constraint checking for loads |
| 97 | `STG` | Source registration for expansion tracking |
| 130 | `HSET2` | Validates single-def destination, recursive source constraint chains; inserts `HSET2` rewrites or converts to opcode-201 stores |
| 137 | `SM73_FIRST` | Same path as `HSET2` |
| 149 | `UFLO` | Two-source validation; marks destination with width code 20; vectorization combining |
| 151 | `UIMAD` | Shared three-source path with `SHF` |
| 190 | `LDGDEPBAR` | Shared last-operand path with `PMTRIG` |
| 201, 202 | `QMMA_16816`, `QMMA_16832` | Full multi-operand legalization; inserts barrier instructions for QMMA |
| 283 | `UVIADD` | Penultimate operand extraction and type resolution |
| 290 | `MOV` (sm_104) | Same constraint path as `SHF`/`UIMAD` |
| bit 12 set | (arch-specific) | Last-operand extraction for architecture-extended instructions |

### `sub_812550` -- `getOperandConstraint`

The single most-called helper (32 call sites), this 40-byte function reads the
constraint code from the register descriptor for a given operand reference:

```c
int getOperandConstraint(int64_t ctx, uint32_t *operand_ref) {
    int modifier_bits = operand_ref[1];
    int constraint = reg_array[*operand_ref & 0xFFFFFF].constraint;  // reg+76
    if ((modifier_bits & 0xFE000000) == 0)
        return constraint;      // no sub-register modifier => raw value
    // Apply modifier-aware transformations:
    //   constraint -2 + certain modifier combos => -3 or -1
    //   constraint -3 + modifier bit 0x3C000000 => -1; + sign bit => -2
    ...
}
```

### `sub_812480` -- `validateOperandChain`

Recursively walks use-def chains through `HSET2` (130) and `SM73_FIRST` (137)
instructions to verify that an entire operand chain is compatible with a target
register file.  Uses `sub_A9BD00` to resolve the register file for a width code,
then checks `reg+76` and `reg+80` agreement.

### Knob gate

Option 183 (target profile offset 13176) controls the expansion distance threshold.
When enabled, a secondary value at profile+13184 sets the maximum distance between
a register definition and its use before the constraint is considered violated.
Default threshold: 7.

### Function map

| Address | Size | Identity | Confidence |
|---------|------|----------|------------|
| `sub_8127C0` | 10,720 B | ExpandMacroInstructions (main pass) | HIGH |
| `sub_812550` | 40 B | getOperandConstraint | HIGH |
| `sub_812480` | ~170 B | validateOperandChain | HIGH |
| `sub_8125E0` | ~450 B | canExpandStoreChain | MEDIUM |
| `sub_800470` | small | isLegalizable | MEDIUM |
| `sub_800360` | small | resolveOperandType | MEDIUM |
| `sub_800400` | small | finalizeOperand | MEDIUM |

## Cross-References

- [Instruction Selection](./isel.md) -- the isel pass that precedes peephole optimization
- [SASS Instruction Encoding](./encoding.md) -- the encoder vtable entries that consume peephole output
- [Newton-Raphson Templates](./templates.md) -- multi-instruction template expansion (DDIV, DRCP, DSQRT) in the same address neighborhood as `sub_169B190`
- [Scheduling Algorithm](../scheduling/algorithm.md) -- the scheduler that runs between pre- and post-schedule peephole
- [Blackwell (SM 100--121)](../targets/blackwell.md) -- SM120-specific context for `sub_143C440`

## Evidence Index

| Claim | Source |
|-------|--------|
| `sub_143C440` structure, 1,087 matchers, 373-case switch | `p1.20-sweep-0x13CF000-0x14A4000.txt` lines 1--486 |
| SM120 encoder zone (123 functions, 180 KB) | `p1.20` lines 269--329 |
| `sub_169B190` structure, 762 matchers, 280 KB | `p1.22` lines 1--460, `p1.23` lines 1--588 |
| Generic operand discriminators (`sub_15F59C0` family) | `p1.22` lines 181--201 |
| Clone clusters in generic matchers | `p1.23` lines 156--174 |
| Post-schedule discriminators (`sub_1820170` family) | `p1.25` lines 271--289 |
| `sub_198BCD0` structure, 1,336 callees, 373-case switch | `p1.26` lines 355--398 |
| Post-schedule 5,282-byte clone group | `p1.26` lines 401--424 |
| Rewrite helper call frequencies | `p1.20` lines 216--227, `p1.23` lines 228--237 |
| Priority 36 as highest observed | `p1.22` lines 316--327 |
| Instruction node layout | `p1.20` lines 406--420, `p1.22` lines 367--409 |
| Secondary switch inventory per dispatcher (85 / 110 / 85; template caps 244 vs 189) | `ptxas_switches.json` filtered by `func_addr` |
| Primary 373-case opcode coverage (249 vs 203 distinct targets) | `ptxas_switches.json`, switches at `0x143C478`, `0x169B1C8`, `0x198BD08` |
