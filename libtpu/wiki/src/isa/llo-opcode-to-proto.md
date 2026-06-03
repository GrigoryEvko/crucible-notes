# LloOpcode to Proto

> *Every offset, value, and address on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`). Other versions differ.*

## Abstract

LLO has two opcode index spaces that must be kept apart. The in-memory [`LloOpcode`](llo-opcode-enum.md) enum is the dense, zero-based set of 461 values (`0x000`..`0x1CC`) the compiler manipulates. The `LloOpcodeProto` enum is the *wire* form serialized into the `LloInstructionProto` message inside a `TpuProgram`/`LloModuleProto`: it is **1-based**, its values run up to **499**, and it has **38 reserved/removed gaps**. A pair of pure converter functions bridge the two — `LloOpcodeToProto` on serialize, `ProtoToLloOpcode` on decode — and they are not identity, not a constant offset, and not monotonic.

The asymmetry exists for the usual protobuf reason: wire compatibility. New opcodes are *inserted* into the in-memory `LloOpcode` at their family's natural position (so families stay contiguous and range tests keep working), but *appended* to the end of the `LloOpcodeProto` enum and never renumbered (so an old serialized program still decodes). Over many silicon generations this produces a scrambled, gapped mapping. A reimplementer who assumes `proto_value == llo_value` or `proto_value == llo_value + 1` will mis-decode every program that contains a recently-added opcode.

This page documents the two converters byte-exactly: the forward direction is a flat lookup table; the reverse is a 499-arm `switch` that traps on reserved values and fatals on out-of-range. It then shows where the converters are invoked (the `LloSerializer::LloInstructionToProto` write at proto field offset +52, and the `xprof` cost-estimation read), and tabulates the structural facts a reimplementer needs.

| | |
|---|---|
| **Forward** | `LloOpcodeToProto(LloOpcode)` @ `0x14420020` (26 B body) — table lookup |
| **Forward table** | `int32[]` @ VMA `0x344cb4c` (GOT-relative; 461 live entries) |
| **Reverse** | `ProtoToLloOpcode(LloOpcodeProto)` @ `0x14420040` — 499-arm `switch` |
| **Reverse out-of-range** | `absl::LogMessageFatal` "Invalid LloOpcodeProto: " (line 1953) |
| **Reverse reserved arms** | 38 proto values → `__debugbreak()` (UD2) |
| **Serializer** | `LloSerializer::LloInstructionToProto` @ `0x1441c000` (opcode → proto field +52) |
| **Proto message** | `xla::jellyfish::LloInstructionProto` (`GetClassData` @ `0x1d1a0d40`) |
| **Value spaces** | `LloOpcode` ∈ `0`..`460` (dense); `LloOpcodeProto` ∈ `1`..`499` (38 gaps) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## The Forward Map — `LloOpcodeToProto`

Serialization converts the in-memory opcode to its wire value with a single table read. The decompiled body is one statement — a `lea` of `_GLOBAL_OFFSET_TABLE_`, the table base added in, then one indexed `mov` and `ret` (26 B of code, padded with `int3` to the next function at `0x14420040`):

```c
// xla::jellyfish::LloOpcodeToProto @ 0x14420020 (decompiled, exact)
uint32_t LloOpcodeToProto(LloOpcode op) {
    return forward_table[op];        // *((uint32_t*)&_GLOBAL_OFFSET_TABLE_ + op - 130144141)
}
```

The decompiler renders the table address GOT-relatively: `(uint32_t*)&_GLOBAL_OFFSET_TABLE_ + op - 130144141`. With `_GLOBAL_OFFSET_TABLE_` at VMA `0x224c2980`, that resolves to a base of `0x224c2980 + 4*(-130144141) = 0x344cb4c`, i.e. the forward table is a flat `int32` array at VMA `0x344cb4c` indexed directly by the `LloOpcode` value. There is **no bounds check** here — the caller is trusted to pass a valid `0`..`460` value, which it always does because the value came from the compiler's own opcode field.

Reading all 461 entries confirms the structure: each maps to a distinct proto value, the set of produced values is `{1, 2, …, 499} \ {38 gaps}` (exactly 461 distinct values), and the map is **not monotonic at the tail**. The early run is the clean `+1` shift (in-memory 0 → proto 1, in-memory 1 → proto 2, …), but late-added opcodes break it:

| In-memory `LloOpcode` | Name | → Proto value | Confidence |
|---|---|---:|---|
| `0x000` (0) `kEvent` | base | 1 | CONFIRMED |
| `0x001` (1) `kVectorReadIar` | base | 2 | CONFIRMED |
| `0x086` (134) `kScalarAddressCalculation` | base | 158 | CONFIRMED |
| `0x14E` (334) `kVectorEupResult` | base | 371 | CONFIRMED |
| `0x151` (337) `kVectorCmemResult` | base | 374 | CONFIRMED |
| `0x17B` (379) `kScalarMove` | base | 417 | CONFIRMED |
| `0x084` (132) `kVectorTraceArg` | **late** | 498 | CONFIRMED |
| `0x197` (407) `kVectorMaskPackCompressedEven` | **late** | 499 | CONFIRMED |
| `0x1CC` (460) `kBarnaCoreVectorStore` | base | 497 | CONFIRMED |

> **QUIRK — proto 498 and 499 are the newest wire slots and map to *low* in-memory opcodes.** `kVectorTraceArg` (in-memory `0x084`) and `kVectorMaskPackCompressedEven` (in-memory `0x197`) were inserted mid-enum in `LloOpcode` but appended at the end of `LloOpcodeProto`. So the highest two wire values decode to opcodes that sit deep inside the in-memory enum. The map is a permutation-with-gaps, not a shift — port the actual table, not a formula.

---

## The Reverse Map — `ProtoToLloOpcode`

Decode is the inverse, but implemented as an explicit `switch` rather than a second table, because it also has to reject the reserved/removed wire values:

```c
// xla::jellyfish::ProtoToLloOpcode @ 0x14420040 (decompiled, structure)
LloOpcode ProtoToLloOpcode(int proto_value) {
    switch (proto_value) {
        case 1:   return (LloOpcode)0;        // dense head: proto N -> llo N-1
        case 2:   return (LloOpcode)1;
        // ...
        case 51:  return (LloOpcode)50;
        case 52: case 53: case 90: /* ...38 values... */ case 376:
            __builtin_trap();                  // UD2 — reserved/removed wire value
        case 54:  return (LloOpcode)51;        // numbering resumes, shifted past the gap
        // ...
        case 498: return (LloOpcode)132;       // late slots map back to low in-memory opcodes
        case 499: return (LloOpcode)407;
        default:                               // proto value never assigned at all
            LogMessageFatal("Invalid LloOpcodeProto: ", proto_value);   // line 1953
    }
}
```

Three distinct outcomes, and a reimplementer must reproduce all three:

1. **Live value** → the corresponding dense `LloOpcode`. The head (proto 1..51) is a clean `proto − 1`; after each gap the output index continues without skipping, so the shift grows.
2. **Reserved/removed value** → `__builtin_trap()` (UD2). These are wire values that *were* assigned to opcodes since deleted, or are explicitly reserved. They are valid integers in the proto enum's range but must never appear in a well-formed program; hitting one is a hard abort, not a recoverable error.
3. **Out-of-range value** → `absl::log_internal::LogMessageFatal` with the message `"Invalid LloOpcodeProto: <value>"` at source line 1953. This catches a proto value outside the enum entirely (negative, zero, or above the declared max).

### The 38 reserved proto values

These wire values are the ones the reverse `switch` routes to `__builtin_trap()`. They are exactly the values *absent* from the forward table's output, which is the cross-check that the two converters agree:

```text
52  53  90  91  92  93  94  95  96  97  98  99 100 101 102 103 104
109 110 111 132 134 140 141 163 176 177 179 183 184
301 312 313 314 315 316 317 376
```

The clustering is informative: the `90`..`104` block and the `109`..`111` block correspond to a generation of conversion/EUP opcodes that were reworked, and `312`..`317` to a removed transcendental band. A reimplementation that maps these to *anything* — even a no-op opcode — silently accepts malformed programs the real decoder rejects.

> **GOTCHA — `__builtin_trap()` on a reserved value is a feature, not a missing case.** The decompiler shows 38 `case` labels falling into a single `__debugbreak()`. It is tempting to read that as "unhandled" and add a fallthrough; doing so removes the integrity check. The proto enum reserves these slots precisely so a stale or corrupted program is caught at decode rather than mis-executed. Keep the trap.

---

## Where The Converters Are Called

### Serialize: `LloInstructionToProto`

The opcode field is written near the top of `LloSerializer::LloInstructionToProto` (@ `0x1441c000`), which builds one `LloInstructionProto` from an in-memory `LloInstruction`:

```c
// LloSerializer::LloInstructionToProto @ 0x1441c000 (decompiled, opcode path)
LloInstructionProto LloInstructionToProto(const LloInstruction *inst) {
    LloInstructionProto proto;                       // proto2::Arena-allocated
    proto.field_at(+0) = inst->result_index;         // line ~108
    proto.set_has_bit(0x04);                          // field-presence bit for the index
    proto.field_at(+52) = LloOpcodeToProto(inst->opcode);   // <- forward convert, line 110
    proto.set_has_bit(0x08);                          // field-presence bit for the opcode
    // ... operands (LloInstructionProto_LloOperand), predication, FIFO ids ...
    return proto;
}
```

The opcode occupies a 4-byte field at byte offset **+52** in the `LloInstructionProto` layout (the `_impl_` block), and it is set to the *proto* value, not the in-memory value. The matching field-presence (`has`) bit is `0x08` in the presence word. The rest of the function lowers operands into `LloInstructionProto_LloOperand` sub-messages (`GetClassData` @ `0x1d1a0800`), the predication, and the runtime result-FIFO ids — but the opcode is the one field that goes through a value translation.

### Decode: read field +52, reverse-convert

The reverse converter is reached from the cost-estimation path, which consumes a *serialized* `LloInstructionProto` directly:

```c
// xprof::AddInstructionCost(LloInstructionProto, LloModuleProto, ...) @ 0xf23c340
LloOpcode op = (uint16_t)ProtoToLloOpcode(*(uint32_t*)(proto + 52));   // line 67
// ...later, compares the wire opcode against a known constant by re-encoding:
if (wire_opcode == LloOpcodeToProto(219 /* kScalarConstantU32 */)) ...  // line 148
```

This is the canonical decode pattern: read the 4-byte opcode field at proto offset +52, run it through `ProtoToLloOpcode` to recover the in-memory `LloOpcode`, then dispatch. The second call (`LloOpcodeToProto(219)`) is the dual trick — rather than decode every instruction, a hot path re-encodes a known in-memory opcode to its wire value and compares against the raw proto field, avoiding the per-instruction `switch`.

> **NOTE — the proto opcode field is a plain `int32`, validated only at conversion time.** Nothing in the proto message guards the opcode field; the integrity check lives entirely in `ProtoToLloOpcode` (the 38 traps + the fatal default). A decoder that reads field +52 and uses it as an array index *without* going through `ProtoToLloOpcode` skips both the reserved-value trap and the out-of-range fatal, and will index its `LloOpcode`-keyed tables (`opcode_info`, the cost grid) with an unvalidated, differently-numbered value.

---

## Structural Summary

The facts a reimplementer needs in one place:

| Property | `LloOpcode` (in-memory) | `LloOpcodeProto` (wire) |
|---|---|---|
| Base | 0-based | 1-based |
| Dense? | yes (`0`..`460`, no gaps) | no (38 reserved gaps in `1`..`499`) |
| Live value count | 461 | 461 |
| Max value | 460 (`0x1CC`) | 499 |
| Numbering policy | insert-in-family (range tests stay valid) | append-only (wire-stable) |
| Bound check | `LloOpcodeName` `>= 0x1CD` → `ud1`; metadata tables `< 0x1CE` | reverse converter: trap (reserved) / fatal (out-of-range) |
| Storage | C++ scoped enum | proto `int32` field at `LloInstructionProto` +52 |

> **QUIRK — both converters are *total* over their valid domains and abort otherwise.** `LloOpcodeToProto` has no out-of-range arm because its input always comes from the compiler's own opcode field; `ProtoToLloOpcode` is exhaustive over `1`..`499` with explicit reserved-traps and a fatal default. There is no "unknown opcode" return path — an invalid value on either side terminates the process. A reimplementation that returns an error code instead changes the failure mode from a crash at the boundary to silent corruption downstream.

---

## Cross-References

- [LloOpcode Enum](llo-opcode-enum.md) — the in-memory 461-value enum these converters map to and from, grouped by family; its append-and-insert numbering is why proto 498/499 decode to low in-memory opcodes.
- [LLO Opcode Table (appendix)](../appendix/llo-opcode-table.md) — the exhaustive 461-row value→name→slot dump the forward table indexes.
- [ISA Overview](overview.md) — places `LloOpcodeProto` in the two-level LLO-IR/VLIW-bundle encoding split; note the "462" figure there counts the wire enum's declared symbols (value-0 sentinel included), versus the 461 live mappable values here.
- [TpuProgram Serialization](../compiler/tpu-program-serialization.md) — the surrounding `LloModuleProto` / `TpuProgram` wire format the `LloInstructionProto` opcode field lives inside.
