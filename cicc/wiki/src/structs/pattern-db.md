# Instruction Constraint Table (Pattern Database)

The instruction selection backend in cicc v13.0 uses a global constraint table to map target opcodes to their operand requirements. This table drives the `sub_B612D0` constraint emission function (38 KB native), which consults a packed 16-bit word array to determine register classes and constraint patterns for each machine instruction. The constraint table is the single authoritative source of truth for every NVPTX MachineInstr's register requirements -- any reimplementation of the backend codegen must reproduce it exactly.

## Global Table: `word_3F3E6C0`

The constraint table is a statically allocated array of 16-bit words in the `.data` section at address `0x3F3E6C0`, indexed by `(opcode - 1)`. Each entry packs two pieces of information into a single 16-bit word:

| Bits | Field | Meaning |
|------|-------|---------|
| Low byte (bits 0..7) | `constraint_class` | Index into the constraint switch (0x00..0xB2) |
| High byte (bits 8..15) | `register_class_id` | Target register class for the result |

The access pattern from `sub_B612D0`:

```c
// sub_B612D0(a1, a2)  where a2 = MachineInstr opcode
v4 = HIBYTE(word_3F3E6C0[a2 - 1]);    // register class for output
switch (LOBYTE(word_3F3E6C0[a2 - 1]))  // constraint class -> switch case
```

There are exactly **179 distinct constraint classes** (0x00 through 0xB2), each encoding a specific operand pattern for a category of instructions. Multiple opcodes can share the same constraint class if they have identical operand signatures.

## Constraint Descriptor Layout

Each constraint descriptor is a stack-allocated array of 16-byte entries built within `sub_B612D0`'s frame. The frame is approximately 0x160 bytes deep. Stack slots span `[rsp-0x158]` through `[rsp-0x20]`:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 4B | `constraint_kind` (int32) |
| +4 | 4B | (padding / alignment) |
| +8 | 8B | `value` (int64: register class ID or operand reference) |

Entry stride: **16 bytes** (8-byte aligned pairs of {int32 kind, int32 pad, int64 value}).

The `constraint_kind` values determine the role of each entry in the descriptor array:

| Kind | Meaning |
|------|---------|
| -1 | Output/result operand (always the **last** entry in the array) |
| 0 | Input operand at position 0 |
| 1 | Input operand at position 1 |
| 2 | Input operand at position 2 |
| 3..N | Input operands at higher positions |

The output entry (kind = -1) carries the result register class. Input entries carry the register class constraint for each source operand. The maximum observed operand count is **17** (constraint class 0xB0, corresponding to opcode 176 in the table), requiring 18 descriptor entries = 288 bytes of stack space.

## Register Class IDs

The `register_class_id` in the high byte maps to NVIDIA GPU register files. Values recovered from `sub_A778C0` (register class constraint creator), `sub_B5BA00` (register class set builder, 111 cases), and `sub_2163730` (PTX emission naming):

These IDs are specific to the pattern database constraint system and differ from the 4-bit class tags used in register encoding (see [Register Classes](../reference/register-classes.md) for vtable addresses, PTX types, prefixes, and encoded IDs).

| ID | Register Class | Width |
|----|---------------|-------|
| 14 | Int32Regs (`%r`) | 32 bits |
| 22 | Int16Regs (`%rs`) | 16 bits |
| 24 | Int16HalfRegs (`%h`) | 16 bits (f16/bf16) |
| 27 | Int32HalfRegs (`%hh`) | 32 bits (v2f16/v2bf16) |
| 29 | (unidentified) | -- |
| 32 | (unidentified) | -- |
| 36 | (unidentified) | -- |
| 39 | (unidentified) | -- |
| 40 | Float32Regs (`%f`) | 32 bits |
| 41 | (unidentified) | -- |
| 43 | Float16Regs (`%h`, alias of Int16HalfRegs) | 16 bits |
| 50 | Int64Regs (`%rd`) | 64 bits |
| 51 | Float64Regs (`%fd`) | 64 bits |
| 52 | Int128Regs (`%rq`) | 128 bits |
| 67 | (unidentified) | -- |
| 72 | (unidentified) | -- |
| 76 | (unidentified) | -- |
| 78 | Int1Regs (`%p`) | 1 bit |
| 86 | SpecialRegs (internal-only, `off_4A026E0`) | varies |

IDs 29, 32, 36, 39, 41, 67, 72, 76 appear in the `sub_B612D0` table but have not been definitively mapped to named register classes. They likely correspond to sub-register classes, tied-operand classes, or WMMA accumulator classes that cicc defines beyond the 9 primary classes documented in [reference/register-classes.md](../reference/register-classes.md).

## Constraint Type Classification

A secondary classification table at `byte_3F252E0` categorizes constraint entries into four families (recovered from `sub_A7A6D0` constraint merge/intersection logic at `0xA78000`):

| Classification Byte | Family | Applies To |
|---------------------|--------|------------|
| 0x00 | Simple/scalar | Single-register operands; the vast majority of ALU constraints |
| 0x08 | Ordered | Operands with fixed positional requirements (tied operands) |
| 0x10 | Sized/ranged | Operands with explicit bit-width requirements (sub-register extracts) |
| 0x18 | Compound | Multi-register operands; types 86-97 in the classification table |

The merge function `sub_A7A6D0` (7KB) performs set intersection across constraint families when two constraint sets must be unified (e.g., during register coalescing or inline asm constraint resolution). The "compound" family (0x18) covers instructions that require register pairs or wider groupings -- tensor core MMA instructions fall into this category.

## Key Sub-Functions

The constraint emission pipeline involves these collaborating functions:

| Address | Size | Function | Purpose |
|---------|------|----------|---------|
| `sub_A778C0` | -- | Reg-class constraint builder | Build a register-class constraint entry; stores class ID in `value` field |
| `sub_A77AD0` | -- | Any-register constraint builder | Build an "any register" constraint (unconstrained operand) |
| `sub_A79C90` | -- | Constraint composer | Compose N descriptor entries into a single constraint record |
| `sub_A7A6D0` | 7KB | Constraint merger / intersector | Merge/intersect two constraint sets using `byte_3F252E0` classification |
| `sub_B5BA00` | 21KB | Output constraint builder | Build the output register constraint; 111-case switch on class ID |
| `sub_A78010` | -- | `emitConstraint(a1, &desc_array, N)` | Emit the final constraint with N entries to the instruction descriptor |
| `sub_B612D0` | 104KB | `emitInstrConstraint(a1, opcode)` | Top-level: lookup `word_3F3E6C0`, dispatch on constraint class, build and emit |

The `sub_B5BA00` function (10 KB native) is itself a 111-case switch that translates register class IDs into the internal constraint representation. It produces the `value` field for output constraint entries. Its size suggests that it handles not just the 9 primary register classes but also sub-register classes, paired classes, and special accumulator classes for tensor operations.

## Constraint Switch Structure

The 179-case switch in `sub_B612D0` is the heart of the pattern database. Each case constructs a fixed sequence of constraint descriptors on the stack, then calls `sub_A78010` to emit them. The cases can be organized into major families based on operand count and register class patterns.

### Family 1: Unary Instructions (1 input, 1 output)

These are the simplest constraints: one input operand and one result. Two descriptor entries (32 bytes on stack). Representative constraint classes:

```c
// Constraint class 0x01 — Unary ALU, same type in/out
// Example: MOV, NEG, NOT, ABS for Int32Regs
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x0E01  (class=0x01, regclass=14=Int32)
case 0x01:
    desc[0] = { kind=0, value=sub_A778C0(a1, v4, 0) }   // input[0]: same class as output
    desc[1] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output: regclass from high byte
    sub_A78010(a1, desc, 2)
```

Constraint classes in this family include 0x01 through approximately 0x08, covering unary operations across all scalar register classes. The register class `v4` (from the high byte) determines whether the instruction operates on Int32, Int64, Float32, Float64, Pred, or another class. The same constraint class is reused for multiple opcodes that share the same operand signature.

### Family 2: Binary ALU Instructions (2 inputs, 1 output)

The most common family. Three descriptor entries (48 bytes on stack). Covers all two-operand arithmetic and logic instructions:

```c
// Constraint class 0x09 — Binary ALU, all same type
// Example: ADD, SUB, MUL, AND, OR, XOR for Int32Regs
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x0E09  (class=0x09, regclass=14=Int32)
case 0x09:
    desc[0] = { kind=0, value=sub_A778C0(a1, v4, 0) }   // input[0]: Int32
    desc[1] = { kind=1, value=sub_A778C0(a1, v4, 0) }   // input[1]: Int32
    desc[2] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  Int32
    sub_A78010(a1, desc, 3)
```

Variants within this family differ in whether inputs are constrained to the same class as the output or to a different class. For instance, shift instructions constrain the shift amount (input[1]) to Int32 regardless of the data type of input[0]:

```c
// Constraint class 0x0C — Binary with mixed types (shift-like)
// Example: SHL.b64, SHR.b64  (data=Int64, shift_amount=Int32)
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x320C  (class=0x0C, regclass=50=Int64)
case 0x0C:
    desc[0] = { kind=0, value=sub_A778C0(a1, v4, 0) }     // input[0]: Int64 (data)
    desc[1] = { kind=1, value=sub_A778C0(a1, 14, 0) }     // input[1]: Int32 (shift amount)
    desc[2] = { kind=-1, value=sub_B5BA00(a1, v4) }        // output:  Int64
    sub_A78010(a1, desc, 3)
```

### Family 3: Comparison / Predicate-Producing Instructions (2 inputs, predicate output)

Comparison instructions produce a predicate register result regardless of the input type. Three descriptor entries:

```c
// Constraint class 0x10 — Compare, predicate output
// Example: SETP.EQ.s32, SETP.LT.f32
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x4E10  (class=0x10, regclass=78=Pred)
case 0x10:
    desc[0] = { kind=0, value=sub_A778C0(a1, <input_class>, 0) }  // input[0]: operand type
    desc[1] = { kind=1, value=sub_A778C0(a1, <input_class>, 0) }  // input[1]: operand type
    desc[2] = { kind=-1, value=sub_B5BA00(a1, 78) }               // output: Pred (%p)
    sub_A78010(a1, desc, 3)
```

The input register class is determined by the instruction variant (integer comparison vs. float comparison), while the output is always predicate register class 78.

### Family 4: Ternary / FMA Instructions (3 inputs, 1 output)

Fused multiply-add and select instructions require four descriptor entries (64 bytes on stack):

```c
// Constraint class 0x18 — Ternary FMA, all same float type
// Example: FMA.RN.f32 (a * b + c)
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x2818  (class=0x18, regclass=40=Float32)
case 0x18:
    desc[0] = { kind=0, value=sub_A778C0(a1, v4, 0) }   // input[0]: Float32 (a)
    desc[1] = { kind=1, value=sub_A778C0(a1, v4, 0) }   // input[1]: Float32 (b)
    desc[2] = { kind=2, value=sub_A778C0(a1, v4, 0) }   // input[2]: Float32 (c)
    desc[3] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  Float32 (result)
    sub_A78010(a1, desc, 4)
```

Select/conditional-move instructions also fall here, with one predicate input and two data inputs:

```c
// Constraint class 0x1A — Select (pred, trueval, falseval)
// Example: SELP.b32 (predicated select)
case 0x1A:
    desc[0] = { kind=0, value=sub_A778C0(a1, 78, 0) }   // input[0]: Pred (condition)
    desc[1] = { kind=1, value=sub_A778C0(a1, v4, 0) }   // input[1]: data (true value)
    desc[2] = { kind=2, value=sub_A778C0(a1, v4, 0) }   // input[2]: data (false value)
    desc[3] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  data (selected)
    sub_A78010(a1, desc, 4)
```

### Family 5: Memory Instructions (load/store with address operands)

Load instructions produce a data result from an address operand. Store instructions consume both data and address. These constraint classes handle the different address space qualifiers and vector widths:

```c
// Constraint class 0x20 — Scalar load from address
// Example: LD.GLOBAL.b32 (global memory load)
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x0E20  (class=0x20, regclass=14=Int32)
case 0x20:
    desc[0] = { kind=0, value=sub_A778C0(a1, 50, 0) }   // input[0]: Int64 (address pointer)
    desc[1] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  Int32 (loaded data)
    sub_A78010(a1, desc, 2)
```

Vector load variants (LoadV2, LoadV4) use additional output entries for each vector lane:

```c
// Constraint class 0x22 — Vector load V2 (two-element)
// Example: LD.GLOBAL.V2.b32 (load 2x Int32)
case 0x22:
    desc[0] = { kind=0, value=sub_A778C0(a1, 50, 0) }   // input[0]: Int64 (address)
    desc[1] = { kind=1, value=sub_A778C0(a1, v4, 0) }   // input[1]: (offset/predicate)
    desc[2] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  data element 0
    // Second output encoded separately via sub_A79C90 composition
    sub_A78010(a1, desc, 3)
```

Store instructions have no result output (kind = -1 carries a sentinel value or void class):

```c
// Constraint class 0x28 — Scalar store
// Example: ST.GLOBAL.b32 (global memory store)
case 0x28:
    desc[0] = { kind=0, value=sub_A778C0(a1, v4, 0) }   // input[0]: data to store
    desc[1] = { kind=1, value=sub_A778C0(a1, 50, 0) }   // input[1]: Int64 (address)
    desc[2] = { kind=-1, value=sub_B5BA00(a1, 86) }      // output:  SpecialRegs (chain/token)
    sub_A78010(a1, desc, 3)
```

### Family 6: Type Conversion Instructions (input and output differ)

Conversion instructions have an input class that differs from the output class. The constraint class encodes the specific pair:

```c
// Constraint class 0x30 — CVT from Int32 to Float32
// Example: CVT.RN.f32.s32
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x2830  (class=0x30, regclass=40=Float32)
case 0x30:
    desc[0] = { kind=0, value=sub_A778C0(a1, 14, 0) }   // input[0]: Int32 (source)
    desc[1] = { kind=-1, value=sub_B5BA00(a1, 40) }      // output:  Float32 (result)
    sub_A78010(a1, desc, 2)
```

```c
// Constraint class 0x32 — CVT from Float64 to Int64
// Example: CVT.RTZ.s64.f64
// Opcode lookup: word_3F3E6C0[opcode - 1] = 0x3232  (class=0x32, regclass=50=Int64)
case 0x32:
    desc[0] = { kind=0, value=sub_A778C0(a1, 51, 0) }   // input[0]: Float64 (source)
    desc[1] = { kind=-1, value=sub_B5BA00(a1, 50) }      // output:  Int64 (result)
    sub_A78010(a1, desc, 2)
```

Widening/narrowing conversions between integer sizes and float-to-half conversions each have their own constraint class.

### Family 7: Copy / Move Instructions (register transfer)

The copy family (opcodes 440-503) maps to constraint classes that encode same-class and cross-class register transfers:

```c
// Constraint class 0x40 — Same-class copy
// Example: MOV.b32  (Int32 -> Int32)
// Used by opcodes 440-443 (type-preserving moves)
case 0x40:
    desc[0] = { kind=0, value=sub_A778C0(a1, v4, 0) }   // input[0]: same class
    desc[1] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  same class
    sub_A78010(a1, desc, 2)
```

```c
// Constraint class 0x42 — Cross-class copy (Int32 <-> Float32)
// Example: MOV from Int32Regs to Float32Regs (bitcast-level move)
// Used by opcodes 444+ (cross-class moves)
case 0x42:
    desc[0] = { kind=0, value=sub_A778C0(a1, <source_class>, 0) }   // input: source class
    desc[1] = { kind=-1, value=sub_B5BA00(a1, <dest_class>) }        // output: dest class
    sub_A78010(a1, desc, 2)
```

Cross-class copies are never coalesced by the register coalescer (they remain as explicit `mov` instructions in PTX output). The constraint table enforces this by assigning distinct source and destination classes.

### Family 8: Call ABI Instructions (parameter declaration and passing)

The NVPTX calling convention uses special opcodes for `.param` space management. These have unique constraint classes with no data register operands:

```c
// Constraint class 0x50 — DeclareParam (opcode 505)
// Declares a .param space allocation for function argument passing
case 0x50:
    desc[0] = { kind=0, value=sub_A77AD0(a1, 0) }       // input[0]: "any" (chain token)
    desc[1] = { kind=-1, value=sub_B5BA00(a1, 86) }      // output:  SpecialRegs (chain)
    sub_A78010(a1, desc, 2)
```

Call sequence opcodes (315=CallSeqBegin, 514=CallStart, 517=CallSeqEnd, 518=CallProto) all use constraint classes that operate on chain tokens rather than data registers. Their inputs and outputs are in the SpecialRegs class (ID 86).

### Family 9: Atomic Instructions (address + data + result)

Atomic operations require an address, a data operand, and produce a result of the same data type:

```c
// Constraint class 0x60 — Atomic RMW (read-modify-write)
// Example: ATOM.ADD.s32 (atomic add on Int32)
// Opcodes 294-297 (atom.add family)
case 0x60:
    desc[0] = { kind=0, value=sub_A778C0(a1, 50, 0) }   // input[0]: Int64 (address)
    desc[1] = { kind=1, value=sub_A778C0(a1, v4, 0) }   // input[1]: data (value to add)
    desc[2] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  data (old value)
    sub_A78010(a1, desc, 3)
```

Atomic compare-and-swap (opcode 462 = atom.cas) requires four operands (address, expected, desired, result):

```c
// Constraint class 0x62 — Atomic CAS
// Example: ATOM.CAS.b32 (compare-and-swap)
case 0x62:
    desc[0] = { kind=0, value=sub_A778C0(a1, 50, 0) }   // input[0]: Int64 (address)
    desc[1] = { kind=1, value=sub_A778C0(a1, v4, 0) }   // input[1]: data (expected)
    desc[2] = { kind=2, value=sub_A778C0(a1, v4, 0) }   // input[2]: data (desired)
    desc[3] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  data (old value)
    sub_A78010(a1, desc, 4)
```

### Family 10: Tensor Core / MMA Instructions (many inputs, many outputs)

The most complex constraint classes handle tensor core matrix operations. These instructions consume multiple register-pair or register-quad operands and produce multiple results. Constraint class 0xB0 is the extreme case with 17 input operands:

```c
// Constraint class 0xB0 — Complex MMA (17 inputs, 1+ outputs)
// Example: tcgen05.mma variants (Blackwell, opcodes 4905-4940)
// This is the maximum-operand constraint class.
case 0xB0:
    for (i = 0; i < 17; i++) {
        desc[i] = { kind=i, value=sub_A778C0(a1, <operand_class[i]>, 0) }
    }
    desc[17] = { kind=-1, value=sub_B5BA00(a1, v4) }
    sub_A78010(a1, desc, 18)
```

HMMA/IMMA/BMMA instructions (the SM70+ tensor core families at `sub_21E0360`-`sub_21E2280`) use constraint classes in the 0x90-0xAF range, typically with 4-8 register inputs (accumulator fragments) and 4-8 register outputs. The operand classes include Int32HalfRegs (ID 27) for packed f16 pairs and Int128Regs (ID 52) for wide accumulator state.

### Family 11: Predicated Instructions (extra predicate input)

Many NVPTX instructions support predication, where execution is conditional on a predicate register. Predicated variants append an extra Pred-class input:

```c
// Constraint class 0x70 — Predicated binary ALU
// Example: @%p0 ADD.s32 %r1, %r2, %r3  (conditional add)
case 0x70:
    desc[0] = { kind=0, value=sub_A778C0(a1, 78, 0) }   // input[0]: Pred (guard)
    desc[1] = { kind=1, value=sub_A778C0(a1, v4, 0) }   // input[1]: data (src0)
    desc[2] = { kind=2, value=sub_A778C0(a1, v4, 0) }   // input[2]: data (src1)
    desc[3] = { kind=-1, value=sub_B5BA00(a1, v4) }      // output:  data (result)
    sub_A78010(a1, desc, 4)
```

### Family 12: Special / Barrier Instructions (chain-only)

Barrier and synchronization instructions have no data operands. They operate purely on the chain token for ordering:

```c
// Constraint class 0x80 — Barrier/Fence (chain-only)
// Example: BAR.SYNC (opcodes 287-290)
case 0x80:
    desc[0] = { kind=0, value=sub_A77AD0(a1, 0) }       // input[0]: "any" (chain in)
    desc[1] = { kind=-1, value=sub_B5BA00(a1, 86) }      // output:  SpecialRegs (chain out)
    sub_A78010(a1, desc, 2)
```

## Pattern Matching Dispatch

The constraint table is consumed during instruction selection by the three-level dispatch hierarchy:

1. **Driver** (`sub_3090F90`, 91KB): Builds a cost table for function arguments via `hash(key*37)`, uses a min-heap priority queue for topological-order traversal, iterates with budget = `4 * numInstructions * maxBlockSize`.

2. **Matcher** (`sub_308FEE0`): Called per-SDNode from the driver. Dispatches to the hand-written selector or the TableGen-generated selector.

3. **Hand-written selector** (`sub_347A8D0`, 309KB): Giant switch on ISD/NVPTXISD opcodes. Calls `sub_969240` (SDNode accessor) 263 times. Recursive with 42 self-calls. Handles tex/surf, wmma, atomics, barriers.

4. **TableGen-generated selector** (`sub_348D3E0`, 256KB): Auto-generated from NVPTX `.td` instruction pattern definitions. Calls `sub_969240` 45 times, `sub_32889F0` 38 times.

5. **Complex addressing mode selector** (`sub_33D4EF0`, 114KB): Handles NVPTX load/store addressing with address space qualifiers. Calls `sub_969240` 399 times -- the single function with the most SDNode accesses in the entire binary.

After pattern matching selects a MachineInstr opcode, the constraint table is queried via `sub_B612D0` to determine register requirements. The selected opcode is the index into `word_3F3E6C0`.

## Operand Binding

When the constraint emission function `sub_B612D0` builds the descriptor array, operand binding follows this protocol:

1. **Lookup**: Read `word_3F3E6C0[opcode - 1]`. Extract `constraint_class` (low byte) and `register_class_id` (high byte, stored as `v4`).

2. **Switch dispatch**: Branch to the case for `constraint_class`.

3. **Input construction**: For each input operand position `i`:
   - Call `sub_A778C0(a1, class_id, flags)` to create a register-class constraint entry.
   - The `class_id` is either `v4` (same class as output) or a hardcoded value (different class for mixed-type instructions).
   - The `flags` parameter encodes operand modifiers (tied, early-clobber, etc.).
   - Store the result in `desc[i]` with `kind = i`.

4. **Output construction**: Call `sub_B5BA00(a1, v4)` to create the output constraint.
   - `sub_B5BA00` is a 21KB function with 111 switch cases that translates the register class ID into the internal output representation.
   - Store in `desc[N]` with `kind = -1`.

5. **Emission**: Call `sub_A78010(a1, desc, N+1)` to finalize. This function walks the descriptor array, validates constraint consistency, and writes the constraint record into the instruction's operand descriptor table.

For instructions that use `sub_A77AD0` ("any register" constraint), the operand accepts any register class. This is used for chain tokens, inline asm operands with unconstrained registers, and certain special-purpose slots.

For composition of multi-output instructions, `sub_A79C90` merges multiple descriptor sub-arrays into a single compound constraint. This is needed for vector loads (LoadV2, LoadV4) and MMA instructions that produce multiple result registers.

## Allocation

The global table `word_3F3E6C0` is in the `.data` section, allocated at link time. It is read-only after cicc process startup. Constraint descriptors are purely stack-allocated within `sub_B612D0`'s frame (approximately 0x160 bytes deep). No heap allocation occurs during constraint emission. This makes the constraint emission path allocation-free and safe for use in concurrent compilation (the function is reentrant as long as each thread has its own stack frame).

## Cross-References

- [reference/register-classes.md](../reference/register-classes.md) -- Authoritative register class table with encoding scheme
- [reference/nvptx-opcodes.md](../reference/nvptx-opcodes.md) -- NVPTX MachineInstr opcode inventory (consumers of this constraint table)
- [llvm/isel-patterns.md](../llvm/isel-patterns.md) -- ISel pattern matching that feeds opcodes into this table
- [llvm/selectiondag.md](../llvm/selectiondag.md) -- SelectionDAG construction that precedes constraint emission
- [llvm/register-allocation.md](../llvm/register-allocation.md) -- Greedy RA that consumes the emitted constraints
- [llvm/register-coalescing.md](../llvm/register-coalescing.md) -- Copy family opcodes 440-503 and coalescing constraints

## Function Map

| Address | Size | Role |
|---|---|---|
| `sub_A778C0` | -- | Build register-class input constraint entry |
| `sub_A77AD0` | -- | Build unconstrained ("any") input constraint |
| `sub_A79C90` | -- | Merge N descriptor entries into compound constraint |
| `sub_A7A6D0` | 7KB | Set-intersection of constraints using `byte_3F252E0` |
| `sub_A78010` | -- | Finalize and emit constraint record |
| `sub_B5BA00` | 21KB | Output constraint builder: 111-case switch (class ID to output representation) |
| `sub_B612D0` | 104KB | Top-level constraint emitter: 179-case constraint class dispatch |
| `sub_B6B200` | 44KB | Operand type decoder from bytecode stream (101-case) |
