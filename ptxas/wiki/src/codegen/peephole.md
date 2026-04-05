# Peephole Optimization

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

| Dispatch function | Binary size | Instructions | Pattern matchers | Total call sites | Entry trampoline | Context |
|-------------------|-------------|-------------|-----------------|-----------------|-----------------|---------|
| `sub_169B190` | 280 KB | 65,999 | 762 | 15,870 | `sub_B12930` | Generic (all SM) |
| `sub_143C440` | 233 KB | ~56,241 | 1,087 | 1,971 | `sub_B12940` | SM120-specific |
| `sub_198BCD0` | 233 KB | 54,043 | 1,336 | 13,391 | `sub_B12960` | Post-scheduling |

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

The secondary switches are embedded inside the giant function.
`sub_143C440` alone contains 85 secondary jump tables (sizes 7--190 cases),
totaling 1,971 switch cases.

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
if (queryModifier(ctx, instr, 0xDC) != 1206) return 0;  // data type != .f32
if (queryModifier(ctx, instr, 0x163) != 1943) return 0;  // rounding != .rn
if (queryModifier(ctx, instr, 0x7E) - 547 > 1) return 0; // saturation out of range
```

The slot indices (0x05, 0x7B, 0x7E, 0x88, 0x90, 0xA1, 0xBE, 0xD2, 0xD3, 0xDC,
0xF2, 0x101, 0x119, 0x126, 0x127, 0x142, 0x152, 0x155, 0x159, 0x15C, 0x163,
0x167, 0x178, 0x179, 0x18A, 0x18D, 0x196, 0x197, 0x199, 0x19D, 0x1A8, 0x1AD,
0x1AE, 0x1AF, 0x1B2, 0x1D1, 0x1D2, 0x1E0, 0x1E4, 0x1EC, 0x216, 0x253, etc.)
index into a per-instruction property table covering type, rounding mode,
saturation, negate, comparison type, and architecture-specific modifiers.

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

Pattern IDs range from 1 to approximately 244 in the generic and SM120
dispatchers.  Multiple matchers can target the same pattern ID with different
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
