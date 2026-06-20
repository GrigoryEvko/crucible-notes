# Instruction Legality Matrix

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

Whether a given `(opcode, modifier-combination)` is legal on the selected target is
decided by a layered gate. The central data structure is a large sparse table in
`.rodata`; it is one axis of a three-layer model.

## The legality table

A flat `u32` array spanning VMA `0x22FEE00 – 0x2339E00` (241,664 B = 60,416 entries),
68.4% zero (19,086 non-zero — stored sparse). The lookup key is

```text
key = (format_id << 8) | minor_opcode
```

— the **same key** the per-SM handler-dispatch tables use, so legality and encoder
selection share one address space. The non-zero `u32` values fall into classes:

| Value class | Count | Meaning |
|---|---|---|
| record key `(fmt<<8)\|minor` | 9,721 | the entry's own key |
| `.text` handler ptr (`0x401000`–`0x1CE0000`) | 8,087 | a dedicated validator/encoder for that `(opcode, modifier)` |
| `.rodata` sub-table ptr (`0x1CE0000`–`0x2400000`) | 918 | pointer to a nested dispatch sub-table |
| **`0x08000000` sentinel** | 259 | legal, but no dedicated validator → take the **generic validation path** |
| other | 101 | small chained-alias keys |

The dominant record stride is 24 B (`{key:u32, key_hi:u32, handler:u64, reserved:u64}`),
but the stride is **not uniform** across the whole region — the reliable facts are the
value classes and the key encoding, not a single rigid struct.

## The three-layer gate

1. **Per-SM encoder dispatch — the hard gate.** Five generation tables (sm50-7x / sm75 /
   sm80-8x / sm86-89 / sm100+), each `{(fmt<<8)|minor, handler_ptr, pad}`. An instruction
   is encodable on a target **iff** its key resolves to a handler in that generation's
   table. Per-gen opcode counts grow monotonically (newer arches add opcodes); 492 opcodes
   are common to all five.
2. **Global legality table** (above) — the STANDARD validation layer applied before per-SM
   dispatch.
3. **PTX-ISA-version gate** — a second axis independent of the target SM. Diagnostics gate
   instructions/modifiers by both `.target sm_NN` *and* PTX ISA version (e.g. *"Instruction
   '%s' without '.sync' is not supported on .target sm_70 and higher from PTX ISA version
   6.4"*).

## STANDARD vs EXTENDED

The instruction registry tags each of its 1,410 forms STANDARD or EXTENDED — **1,141
STANDARD + 269 EXTENDED**. EXTENDED forms are the extended-ISA / fused / approximate
variants (`mad.fused.hi`, `tanh`, `ex2`, …) that are arch- or option-gated above the
standard set. (See [Operand-Type Signatures & Attributes](operand-types-attributes.md).)

## Gating diagnostics

The messages emitted on a failed `(SM, ISA, instruction, modifier)` tuple include:
*"Instruction '%s' not supported on .target '%s'"*, *"Feature '%s' not supported on
.target '%s'"*, *"Unsupported instruction '%s' used when compiling for '%s' target
architecture"*, and *"Modifier '%s' on instruction '%s' … not supported starting %s and
later architectures"*. (Full set in [Diagnostics & Messages](diagnostics.md).)

The flat legality table and the gating-diagnostic strings are in the repo at
`decoded/ptxas-targets/` (`instruction_legality.tsv`, `gating_diagnostics.tsv`).

## Cross-References

- [SM Version Codes](../targets/version-codes.md) — the target-identity tables this gate keys on.
- [SASS Instruction Encoding](../codegen/encoding.md) — the encoder the per-SM dispatch feeds.
- [Operand-Type Signatures & Attributes](operand-types-attributes.md) — the STANDARD/EXTENDED registry.
