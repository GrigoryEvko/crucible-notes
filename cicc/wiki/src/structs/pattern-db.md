# Instruction Constraint Table (Pattern Database)

The instruction selection backend in cicc v13.0 uses a global constraint table to map target opcodes to their operand requirements. This table drives the `sub_B612D0` constraint emission function, which consults a packed 16-bit word array to determine register classes and constraint patterns for each machine instruction.

## Global Table: `word_3F3E6C0`

The constraint table is a statically allocated array of 16-bit words at address `0x3F3E6C0`, indexed by `(opcode - 1)`. Each entry packs two pieces of information:

| Bits | Field | Meaning |
|------|-------|---------|
| Low byte (bits 0..7) | `constraint_class` | Index into the constraint switch (0x00..0xB2) |
| High byte (bits 8..15) | `register_class_id` | Target register class for the result |

The access pattern from `sub_B612D0`:

```
v4 = HIBYTE(word_3F3E6C0[a2 - 1]);    // register class
switch (LOBYTE(word_3F3E6C0[a2 - 1]))  // constraint pattern
```

There are at least **179 distinct constraint classes** (0x00 through 0xB2), each encoding a specific operand pattern for a category of instructions.

## Constraint Descriptor Layout

Each constraint descriptor is a stack-allocated array of entries built by `sub_B612D0`. Entries have a 16-byte stride, stored as aligned pairs on the stack frame at `[rsp-0x158]` through `[rsp-0x20]`:

| Offset | Size | Field |
|--------|------|-------|
| +0 | 4B | `constraint_kind` (int32) |
| +4 | 4B | (padding) |
| +8 | 8B | `value` (int64, register class or operand ref) |

The `constraint_kind` values:

| Kind | Meaning |
|------|---------|
| -1 | Output/result operand (always the last entry) |
| 0 | Input operand at position 0 |
| 1 | Input operand at position 1 |
| 2 | Input operand at position 2 |
| 3..N | Input operands at higher positions |

The maximum observed operand count is **17** (constraint class 0xB0, opcode 176), requiring 272 bytes of stack space for descriptors.

## Register Class IDs

The `register_class_id` in the high byte of the table entry maps to NVIDIA GPU register files. The following values were recovered from `sub_A778C0` (register class constraint creator):

| ID | Register Class | Description |
|----|---------------|-------------|
| 14 | Int32 | 32-bit integer registers |
| 22 | Int16 | 16-bit integer registers |
| 40 | Float32 | 32-bit floating point |
| 43 | Float16 | 16-bit floating point |
| 50 | Int64 | 64-bit integer registers |
| 51 | Float64 | 64-bit floating point |
| 52 | Int128 | 128-bit integer (pair) |
| 78 | Pred | Predicate registers |
| 86 | Special | Special-purpose registers |

## Key Sub-Functions

The constraint emission pipeline involves several helper functions:

| Address | Function | Purpose |
|---------|----------|---------|
| `sub_A778C0` | `createRegClassConstraint(a1, regclass, flags)` | Build a register class constraint entry |
| `sub_A77AD0` | `createAnyRegConstraint(a1, flags)` | Build an "any register" constraint |
| `sub_A79C90` | `composeConstraints(a1, &desc, N)` | Compose N descriptor entries into one |
| `sub_B5BA00` | `createOutputConstraint(a1, regclass_id)` | Build the output register constraint |
| `sub_A78010` | `emitConstraint(a1, &desc_array, N)` | Emit the final constraint with N entries |
| `sub_B612D0` | `emitInstrConstraint(a1, opcode)` | Top-level: lookup table, build, emit |

## Constraint Switch Structure

The switch statement in `sub_B612D0` has 179 cases. Each case constructs a fixed sequence of constraint descriptors and calls `sub_A78010` to emit them. Representative patterns:

- **Simple ALU** (2 inputs, 1 output): 3 descriptor entries, 48 bytes on stack.
- **Ternary FMA** (3 inputs, 1 output): 4 entries, 64 bytes.
- **Complex intrinsic** (up to 17 inputs): 18 entries, 288 bytes.

The constraint table is read-only after initialization. It encodes the complete operand specification for every NVPTX machine instruction that cicc can emit, making it the central artifact for understanding the target instruction set's register requirements.

## Allocation

The global table `word_3F3E6C0` is in the `.data` section, allocated at link time. Constraint descriptors are purely stack-allocated within `sub_B612D0`'s frame, which is approximately 0x160 bytes deep. No heap allocation occurs during constraint emission.
