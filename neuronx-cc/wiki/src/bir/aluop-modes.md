# AluOpType and the BIR Mode Enums

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, the cp310 build of `libBIR.so` (md5 `12bb979f7ca41248252abb0f16b2da98`). The `0x142e030` CoreV4 wire-byte converter is in the sibling `libwalrus.so` (cp310, GNU build-id `92b4d331…`); for both libraries, `.text`/`.rodata` virtual address equals file offset. The cp311/cp312 builds carry the identical ABI but their addresses drift; the ordinals, names, and the L1≠L3 mapping are version-stable.*

## Abstract

Five small `bir::*` enums select the arithmetic, copy, and fill semantics of the BIR instruction set: `AluOpType` (the 33-value ALU opcode), and four mode flags — `TSCMode`, `CopyMode`, `MemsetMode`, and the dormant `ReduceCmdType`. Each is an **L1** field stored as an `int` inside a `bir::Inst*` object; the BIR JSON wire form (**L2**) serializes it by *name* through a `<Enum>2string` forward map and parses it by *name* through a `string2<Enum>` inverse map. Only `AluOpType` carries an **L3** silicon wire byte — the CoreV4 `ALU_OP` field — and that byte is produced not in `libBIR` but downstream in `libwalrus`'s `CoreV4Convert.cpp` (`sub_142E030`).

The one finding a reimplementer must not miss is that **the L1 enum and the L3 wire byte are not the same integer for the comparison family.** `bir::AluOpType` numbers the predicates `is_equal=18, not_equal=19, is_gt=20, is_ge=21, is_lt=22, is_le=23`, but the CoreV4 encoder renumbers them on the wire so that an operand swap maps `gt↔lt` and `ge↔le` onto adjacent codes with no re-encode. Two L1 ordinals (`average=24`, `elemwise_mul=25`) have *no* generic ALU wire byte at all — they fall through the converter's switch to a hard `reportError`, because silicon handles them with dedicated opcodes. And `mod_int=32` does not map into the 1-byte `0x00..0x21` ALU band but into the int32 extended band, emitting `0xFFFFFFC8` (low byte `0xC8`).

This page reconstructs all five enums from both directions of their (de)serializers, names the JSON key and struct offset each field deserializes into, reproduces the CoreV4 comparison-reorder switch as annotated pseudocode, and proves that `ReduceCmdType` — though it has a complete enum API — is referenced by no instruction in this build.

For reimplementation, the contract is:

- The 33 `AluOpType` ordinals and their exact spellings (ordinal 6 is `mult`, not `multiply`; 25 is the distinct `elemwise_mul`).
- The L1→L3 CoreV4 wire map for `AluOpType`, including the comparison reorder, the two `reportError` holes, and the `mod_int` extended-band escape.
- The value sets of `TSCMode` / `CopyMode` / `MemsetMode`, the JSON key + struct offset each deserializes into, and the fact that none of the four mode enums has an L3 silicon byte.
- That `ReduceCmdType` is a dormant enum: defined and serializable, but consumed by no shipped instruction.

| | |
|---|---|
| **Enum source** | InstaBrew `brewer.py` codegen — every default arm cites `…/neuronxcc/instabrew/brewer.py` |
| **L1→L2 (serialize)** | `Inst::toJson` → `bir::to_json(json, Enum&)` → `<Enum>2string(int)` → `json["<key>"]` |
| **L2→L1 (parse)** | `Inst::readFieldsFromJson` → `json.at("<key>")` → `bir::from_json(json, Enum&)` → `string2<Enum>(str)` → field at `Inst+0xNN` |
| **L3 (AluOpType only)** | `libwalrus` `sub_142E030` (`CoreV4Convert.cpp`), jump table base `0x1dfb5e8`, 33 cases |
| **AluOpType** | `2string @0x400600` / `string2 @0x40db60` / `from_json @0x416fc0` — 33 ops, 0..32 |
| **ReduceCmdType** | `2string @0x402480` / `string2 @0x40f420` / `from_json @0x417aa0` — 4 values, **dormant** |
| **TSCMode** | `2string @0x4012e0` / `string2 @0x40e720` / `from_json @0x41b200` — 3 values |
| **CopyMode** | `2string @0x4013f0` / `string2 @0x40e7b0` / `from_json @0x416ea0` — 4 values |
| **MemsetMode** | `2string @0x400cf0` / `string2 @0x40e220` / `from_json @0x416ce0` — 2 values |

> **NOTE —** every `0x40xxxx` body cited here has a `0x17xxxx` twin in the IDA listing (e.g. `TSCMode2string` also at `0x17ec90`). The twins are the lazy-binding PLT stubs / second VA frame for the same symbol; the real switch bodies are the `0x40xxxx` addresses. Cite the `0x40xxxx` frame.

---

## AluOpType — the 33-value ALU opcode

### Purpose

`AluOpType` is the operation selector for every elementwise/reduction ALU instruction in BIR. A single integer field (`int`, 0..32) names which of 33 operations the engine applies: arithmetic, bitwise, logical, shift, comparison, and a handful of reductions and unary functions. It is the most widely consumed enum in the IR — fifteen distinct `Inst*` classes carry one or more `AluOpType` fields.

### Forward and Inverse Maps

The two maps are exact inverses, walked in the same ordinal order, so the BIR-JSON round-trip is name-stable.

```c
// bir::AluOpType2string  @0x400600  — dense switch(a2), 33 cases
function AluOpType2string(out, ord):     // sub_400600
    switch (ord):
        case 0:  return mkstr(out, "bypass")           // unary identity
        case 6:  return mkstr(out, "mult")             // NOT "multiply"
        case 25: return mkstr(out, "elemwise_mul")     // distinct from mult
        case 32: return mkstr(out, "mod_int")
        ...                                            // all 33 in the table below
        default: fatal("Unknown AluOpType", brewer.py) // sub_679F20

// bir::string2AluOpType  @0x40db60  — 33-arm string::compare if-chain
function string2AluOpType(s):            // sub_40db60
    if s=="bypass":      return 0
    ... if s=="mod_int": return 32       // last arm
    fatal("Unknown AluOpType string: " + s, brewer.py)
```

The 33 comparand strings in `string2AluOpType`'s disasm (`lea rsi, a<Name>`) appear in the *same order* as the forward switch cases — verified by reading the inverse-map disasm at `0x40db60` and matching `bypass, bitwise_not, arith_shift_left, arith_shift_right, add, subtract, …, is_equal, not_equal, is_gt, is_ge, …` to the forward arms one-for-one.

### Ordinal Table and the L3 Wire Map

The L1 ordinal (column 1) is what `libBIR` stores and what the JSON name decodes to. The L3 wire byte (column 4) is what `libwalrus` `sub_142E030` emits into the CoreV4 `ALU_OP` field. Where they differ, the row is flagged.

| L1 | Name | Class | L3 `ALU_OP` byte | Δ |
|---|---|---|---|---|
| 0 | `bypass` | unary | `0x00` | = |
| 1 | `bitwise_not` | unary | `0x01` | = |
| 2 | `arith_shift_left` | shift | `0x02` | = |
| 3 | `arith_shift_right` | shift | `0x03` | = |
| 4 | `add` | arith | `0x04` | = |
| 5 | `subtract` | arith | `0x05` | = |
| 6 | `mult` | arith | `0x06` | = |
| 7 | `divide` | arith | `0x07` | = |
| 8 | `max` | arith | `0x08` | = |
| 9 | `min` | arith | `0x09` | = |
| 10 | `bitwise_and` | bitwise | `0x0a` | = |
| 11 | `bitwise_or` | bitwise | `0x0b` | = |
| 12 | `bitwise_xor` | bitwise | `0x0c` | = |
| 13 | `logical_and` | logical | `0x0d` | = |
| 14 | `logical_or` | logical | `0x0e` | = |
| 15 | `logical_xor` | logical | `0x0f` | = |
| 16 | `logical_shift_left` | shift | `0x10` | = |
| 17 | `logical_shift_right` | shift | `0x11` | = |
| 18 | `is_equal` | compare | `0x12` | = |
| 19 | `not_equal` | compare | `0x18` | ✗ **reorder** |
| 20 | `is_gt` | compare | `0x13` | ✗ **reorder** |
| 21 | `is_ge` | compare | `0x14` | ✗ **reorder** |
| 22 | `is_lt` | compare | `0x16` | ✗ **reorder** |
| 23 | `is_le` | compare | `0x15` | ✗ **reorder** |
| 24 | `average` | reduce | **`reportError`** | ✗ no wire byte |
| 25 | `elemwise_mul` | arith | **`reportError`** | ✗ no wire byte |
| 26 | `pow` | arith | `0x1a` | = |
| 27 | `mod` | arith | `0x1b` | = |
| 28 | `rsqrt` | unary | `0x1d` | ✗ reorder |
| 29 | `abs` | unary | `0x19` | ✗ **reorder** |
| 30 | `abs_max` | reduce | `0x20` | ✗ shift |
| 31 | `abs_min` | reduce | `0x21` | ✗ shift |
| 32 | `mod_int` | arith | `0xC8` | ✗ **ext band** |

Names are byte-exact from the `0x400600` switch and the `0x40db60` inverse arms. The L3 bytes come from the `sub_142E030` disassembly — each `case N: mov eax, IMM; retn`, and the `mov eax, 0FFFFFFC8h` for case 32, was read directly (see below).

> **QUIRK — the comparison family is reordered on the wire because the predicates are anti-symmetric.** The L1 enum numbers them `is_equal(18), not_equal(19), is_gt(20), is_ge(21), is_lt(22), is_le(23)`; the CoreV4 wire band groups them so `is_gt(20)→0x13` and `is_lt(22)→0x16`, `is_ge(21)→0x14` and `is_le(23)→0x15`, while `not_equal(19)→0x18`. Swapping the two operands of a comparison flips `gt↔lt` and `ge↔le`; the wire layout is chosen so the swapped predicate is one entry away and the codegen never has to re-derive the opcode. A reimplementer who assumes `wire = L1_ordinal` will silently emit the wrong predicate for every `is_gt`/`is_ge`/`is_lt`/`is_le`/`not_equal`. The same anti-symmetry is why these ops must never be operand-canon-swapped by a verifier (see Commutativity below).

> **GOTCHA — `average(24)` and `elemwise_mul(25)` are valid L1 ops with no generic ALU wire byte.** The CoreV4 converter's `switch` has *no* case for 24 or 25; both fall to the `default` arm, which calls `bir::reportError("Invalid enum variant for enum AluOpType")` then `llvm_unreachable_internal(…, "CoreV4Convert.cpp")`. This is not a bug — silicon implements `average` with a dedicated 1/N reduction op and `elemwise_mul` with a dedicated multiply opcode, neither of which is the generic ALU path. A reimplementer must route these two L1 ordinals to their dedicated emitters *before* reaching the ALU-byte converter, or the converter will fatal.

### The CoreV4 Wire Converter

`sub_142E030` in `libwalrus.so` is the L1→L3 map. It is a jump-table switch (`cmp edi, 20h; ja default; … jpt_142E05F` at base `0x1dfb5e8`) with 33 reachable index slots; cases 24 and 25 are absent from the table and fall to the default fatal.

```c
// libwalrus  sub_142E030  (CoreV4Convert.cpp)  — L1 bir::AluOpType → L3 ALU_OP byte
function CoreV4_AluOp_toWire(ord):       // 0x142e030, jumptable @0x1dfb5e8
    switch (ord):
        case 0..18: return ord           // identity for bypass..is_equal
        case 19: return 0x18             // not_equal   — reorder  (@0x142e110: mov eax,18h)
        case 20: return 0x13             // is_gt       — reorder  (@0x142e100: mov eax,13h)
        case 21: return 0x14             // is_ge       — reorder  (@0x142e0f0: mov eax,14h)
        case 22: return 0x16             // is_lt       — reorder  (@0x142e0e0: mov eax,16h)
        case 23: return 0x15             // is_le       — reorder  (@0x142e0d0: mov eax,15h)
        // cases 24 (average), 25 (elemwise_mul): NOT in table → default
        case 26: return 0x1a             // pow         (@0x142e0c0)
        case 27: return 0x1b             // mod         (@0x142e0b0)
        case 28: return 0x1d             // rsqrt       (@0x142e0a0)
        case 29: return 0x19             // abs         — reorder  (@0x142e090)
        case 30: return 0x20             // abs_max     (@0x142e080)
        case 31: return 0x21             // abs_min     (@0x142e070)
        case 32: return 0xFFFFFFC8       // mod_int → int32 ext band, low byte 0xC8 (@0x142e068)
        default:                          // includes 24, 25
            reportError("Invalid enum variant for enum AluOpType")  // sub_142DE80 + bir::reportError
            llvm_unreachable("CoreV4Convert.cpp")
```

The `case 32` arm returns the 32-bit value `0xFFFFFFC8` (`mov eax, 0FFFFFFC8h` at `0x142e068`), not a 1-byte ALU code. Masked to the ALU field this is `0xC8`, the `mod_int` slot of the int32-both-operand extended band (`Add 0xC4 / Sub 0xC5 / Mul 0xC6 / … / mod_int 0xC8`). The full membership of that `0xC4..0xE1` band is a `libwalrus`/`CoreV4` concern documented on the ISA enum-ordinals page; only the `mod_int=0xC8` escape is pinned from this converter (the rest is INFERRED from the cross-binary band, not re-disassembled here).

> **NOTE —** the same `sub_142E030` byte values are independently documented on [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md#enum-2--_alu_op-biraluoptype), where they were read from the L3 encoder side. The two derivations — `libBIR` 2string names joined to `libwalrus` converter bytes — agree byte-for-byte, which is the cross-check for this map. A CoreV2/V3 converter (`sub_12039C0`, 30 cases `0..29`, base `0x1df4bb8`) emits the same identity-plus-reorder pattern but tops out before `abs_max`/`abs_min`/`mod_int`; those three are CoreV4-only.

### Field Consumers

Fifteen `Inst*` classes invoke `from_json(json, AluOpType&)` from their `readFieldsFromJson` body (confirmed by listing every `readFieldsFromJson` disasm that calls the `…RNS_9AluOpTypeE` overload). The JSON key differs per instruction:

| JSON key(s) | Instruction(s) |
|---|---|
| `op` | `InstTensorTensor`, `InstRegisterAlu` |
| `op0`, `op1` | `InstTensorScalar`, `InstTensorScalarCache` |
| `op0`, `op1`, `reduce_op` | `InstActivation` |
| `op` (reduce) | `InstTensorReduce` |
| `reduce_op`, `compare_op0`, `compare_op1` | `InstRangeSelect` |
| DGE reduce op (tail field) | `InstDMACopy`, `InstDMADescriptorCCE` |
| op field | `InstCopyPredicated`, `InstCollectiveCompute`, `InstGenericIndirectSave`, `InstIndirectSaveAccumulate`, `InstTensorScalarAffineSelect`, `InstTensorScalarPtr` |

> **NOTE —** `InstTensorReduce::readFieldsFromJson` (`0x41a7c0`) reads `{axis, op→AluOpType, negate, apply_transpose}` and has **no** `reduce_cmd` field — relevant to the `ReduceCmdType` dormancy finding below. A single `InstDMACopy` carries three independent enums at once: `CopyMode` at `+0x128`, a `DGEType` at `+0xF8`, and an `AluOpType` DGE-reduce field (`from_json(AluOpType&)` tail-call at `0x4316e9`).

### Commutativity

`libBIR.so` has **no** `isCommutative` symbol; operand-order sensitivity is implicit in each operator's semantics. The authoritative behavioral evidence is the simulator's reduce/RMW functor bodies (`libBIRSimulator`), each `redFn(acc, x)` with `acc` the running LHS. The classification below is derived from those functor bodies; there is no named commutativity flag to read:

- **Commutative** `f(a,b)==f(b,a)`: `add(4)`, `mult(6)`, `max(8)`, `min(9)`, `is_equal(18)`, `not_equal(19)`, `bitwise_and/or/xor(10/11/12)`, `logical_and/or/xor(13/14/15)`, `abs_max(30)`, `abs_min(31)`, `average(24)`, `elemwise_mul(25)`.
- **Order-sensitive** (`acc` op `x`): `subtract(5)=acc−x`, `divide(7)=acc/x`, shifts `2/3/16/17 = acc<<x` / `acc>>x`, `pow(26)=powf(acc,x)`, `mod(27)`/`mod_int(32)=acc mod x`, and the comparisons `is_gt(20)=acc>x`, `is_ge(21)`, `is_lt(22)`, `is_le(23)`.
- **Unary** (`x` ignored): `bypass(0)`, `bitwise_not(1)=~acc`, `rsqrt(28)=1/√acc`, `abs(29)=|acc|`.

The comparison family's anti-symmetry — swapping operands flips `gt↔lt`, `ge↔le` — is exactly the property the CoreV4 wire reorder exploits. A verifier may treat the commutative ops as order-insensitive in a `sameInst` structural comparison, but must never operand-canon-swap an order-sensitive op.

---

## ReduceCmdType — a dormant enum

### Purpose

`ReduceCmdType` names the four phases of a read-modify-write reduce sequence: `Idle, Reset, Reduce, ResetReduce`. Semantically it is the simulator-side reduce-command phase state (reset-then-accumulate). In *this* `libBIR` build it has a complete enum API but is wired to no instruction.

### Forward and Inverse Maps

```c
// bir::ReduceCmdType2string  @0x402480
function ReduceCmdType2string(out, ord):
    switch (ord):
        case 0: strcpy(out, "Idle")        // inline default arm
        case 1: return mkstr(out, "Reset")
        case 2: return mkstr(out, "Reduce")
        case 3: return mkstr(out, "ResetReduce")
        default: fatal("Unknown ReduceCmdType", brewer.py)

// bir::string2ReduceCmdType  @0x40f420  — inverse, "Idle"→0 … "ResetReduce"→3
```

Both directions are byte-exact (`Idle=0, Reset=1, Reduce=2, ResetReduce=3`); round-trip is name-stable.

### Dormancy Proof

> **GOTCHA — `ReduceCmdType` is defined, serializable, and consumed by nothing.** The symbol `ReduceCmdType` appears in exactly six functions across the entire decompiled + disasm tree, and they are *all* its own (de)serializer / stream operators: `2string @0x402480`, `string2 @0x40f420`, `from_json @0x417aa0`, `to_json @0x41e650`, `operator<< @0x402520` (plus their second-VA-frame twins). It appears in **no** `Inst::readFieldsFromJson`, no builder, and no setter. The enum is dormant in this build — `rg 'ReduceCmdType'` over both trees returns only those serializer bodies.

The live `reduce_cmd` JSON key, despite the name, deserializes to a *different* enum. In `InstExponential::readFieldsFromJson` (`0x417b80`), the disasm reads:

```asm
0x417ccb: lea  rsi, aReduceCmd            ; "reduce_cmd"
0x417cdb: lea  rsi, [r12+0F0h]            ; field at +0xF0
0x417cf0: jmp  bir::from_json(… bir::EngineAccumulationType &)
```

So the `reduce_cmd` wire field is an `EngineAccumulationType` (`{Idle, Zero, Accumulate, ZeroAccumulate, AddAccumulate, LoadAccumulate}`), parsed into `+0xF0`, **not** a `ReduceCmdType`. `InstRangeSelect` (`0x419b00`) carries the same `reduce_cmd`→`EngineAccumulationType` binding; `InstTensorReduce` has no such key at all.

> **GOTCHA — the `reduce_cmd` key is not a `ReduceCmdType`.** The name invites the wrong binding. `InstTensorReduce` has no `reduce_cmd` field at all, and on `InstExponential` / `InstRangeSelect` the key deserializes to `EngineAccumulationType` at `+0xF0`. `bir::ReduceCmdType` is a sim/runtime RMW-phase enum that never surfaces in the BIR JSON of any shipped instruction in this build.

Where the compiler *sets* a `ReduceCmdType` value at runtime is [UNRESOLVED]; the likeliest home is an internal scheduler / RMW-sequencing path that constructs it programmatically without a JSON round-trip.

---

## TSCMode — TensorScalarCache accumulation mode

### Purpose

`TSCMode` selects the accumulation semantics of the cached scalar-tensor path: a one-shot reduce, a running cumulative, or a tensor scan. Its sole consumer is `InstTensorScalarCache`.

### Maps and Driver

```c
// bir::TSCMode2string  @0x4012e0
//   0 → "Reduce"   (default arm)   1 → "Cumulative"   2 → "TensorScan"
// bir::string2TSCMode  @0x40e720   — inverse, round-trip exact
```

| L1 | Name | L3 wire | Confidence |
|---|---|---|---|
| 0 | `Reduce` | none | CERTAIN |
| 1 | `Cumulative` | none | CERTAIN |
| 2 | `TensorScan` | none | CERTAIN |

`InstTensorScalarCache::readFieldsFromJson` (`0x41b270`) reads `{op0→AluOpType@+0xF0, op1→AluOpType@+0x120, mode→TSCMode@+0x150, acc→EngineAccumulationType@+0x154, reverse0, reverse1, …}`. The `mode` field sits at `+0x150` (336), per the disasm:

```asm
0x41b3c2: lea  rsi, aUpdateMode+7         ; "mode"
0x41b3d1: lea  rsi, [r12+150h]            ; +0x150
0x41b3dc: call bir::from_json(… bir::TSCMode &)
```

`TSCMode` has no L3 silicon byte — it gates a `libwalrus` emitter codepath (reduce / running-cumulative / scan accumulation), not a wire opcode field.

> **GOTCHA — the three `InstTensorScalarCache` enum fields are at non-adjacent offsets that are easy to confuse.** `+0xF0`/`+0x120` are the two `AluOpType` ops; `+0x150` is the `TSCMode`; `+0x154` is the `EngineAccumulationType`. The JSON key `mode` is the `TSCMode` at `+0x150`, not the op fields at `+0xF0`.

---

## CopyMode — InstDMACopy transfer mode

### Purpose

`CopyMode` selects how an `InstDMACopy` moves data: a straight copy, a transpose, a collective-comm-engine DMA, or a broadcast/replicate. Sole consumer is `InstDMACopy`.

### Maps and Driver

```c
// bir::CopyMode2string  @0x4013f0
//   0 → "Copy" (default arm)   1 → "Transpose"   2 → "CCE"   3 → "Replicate"
// bir::string2CopyMode  @0x40e7b0  — inverse, round-trip exact
```

| L1 | Name | L3 wire | Confidence |
|---|---|---|---|
| 0 | `Copy` | none | CERTAIN |
| 1 | `Transpose` | none | CERTAIN |
| 2 | `CCE` | none | CERTAIN |
| 3 | `Replicate` | none | CERTAIN |

`InstDMACopy::readFieldsFromJson` (`0x4311a0`) reads the `mode` key into `+0x128` (296), per the disasm:

```asm
0x4311c3: lea  rsi, aUpdateMode+7         ; "mode"
0x4311d7: lea  rsi, [rbx+128h]            ; +0x128
0x4311e1: call bir::from_json(… bir::CopyMode &)
```

Like the other mode enums, `CopyMode` carries no silicon byte; it selects an emitter branch (e.g. `Transpose` → stream-transpose emitter, `CCE` → collective-DMA path). The exact `CoreVN` emitter branch each value picks is a `libwalrus` concern and is not pinned here.

---

## MemsetMode — InstMemset fill mode

### Purpose

`MemsetMode` selects whether an `InstMemset` writes a constant value or a random pattern. Sole consumer is `InstMemset`.

### Maps and Driver

```c
// bir::MemsetMode2string  @0x400cf0
//   0 → "Const"   1 → "Random"
function MemsetMode2string(out, ord):     // sub_400cf0
    if ord == 0:
        // value 0 builds the string inline, NOT from a .rodata literal:
        *(DWORD*)(out+16) = 0x736E6F43     // 'C','o','n','s'
        *(BYTE*)(out+20)  = 0x74           // 't'
        len = 5                            // "Const"
    elif ord == 1: return mkstr(out, "Random")
    else: fatal("Unknown MemsetMode", brewer.py)
// bir::string2MemsetMode  @0x40e220  — "Const"→0, "Random"→1, round-trip exact
```

| L1 | Name | L3 wire | Confidence |
|---|---|---|---|
| 0 | `Const` | none | CERTAIN |
| 1 | `Random` | none | CERTAIN |

> **QUIRK — the `Const` name is assembled inline, not loaded from a string literal.** `MemsetMode2string` value 0 stores `0x736E6F43` ('Cons', little-endian) as a DWORD and `'t'` as a trailing byte, length 5. There is no `aConst` pointer in this arm, so a decompiler that only follows `lea`-string references can miss the name entirely — it must be reconstructed from the immediate. (The round-trip is still exact: `string2MemsetMode` compares against the `.rodata` `"Const"` literal at `0x40e222` → 0.)

`InstMemset::readFieldsFromJson` (`0x41e840`) reads `mode` into `+0xF0` (240), then a second key `constant` (the fill value, a `MaybeAffine`):

```asm
0x41e849: lea  rsi, aUpdateMode+7         ; "mode"
0x41e85c: lea  rsi, [r12+0F0h]            ; +0xF0
0x41e867: call bir::from_json(… bir::MemsetMode &)
0x41e86f: lea  rsi, aConstant             ; "constant"  (fill value)
```

Round-trip serialization is confirmed: `InstMemset::toJson` (`0x435e60`) references the `"Const"` literal and calls `to_json(MemsetMode)`.

---

## Provenance

All five enums are emitted by the InstaBrew `brewer.py` codegen: every `2string`/`string2` default-error arm cites the source path `…/neuronxcc/instabrew/brewer.py`, consistent with the generated `readFieldsFromJson`/`toJson`/`from_json`/`to_json` family that consumes them. This is why the maps are dense, ordinal-ordered switches with a uniform fatal arm rather than hand-written tables. The `brewer.py` path string is present in all ten map bodies.

### Limits of this reading

- `ReduceCmdType`'s live runtime setter is unresolved; the enum is dormant in `libBIR` JSON and its symbol does not surface in the simulator strings either.
- The four mode enums (`ReduceCmdType`/`TSCMode`/`CopyMode`/`MemsetMode`) have no L3 silicon byte; the exact `libwalrus` emitter branch each selects is not pinned here.
- The int32 extended ALU band (`0xC4..0xE1`) beyond `mod_int=0xC8` is taken from the cross-binary CoreV4 converter, not re-disassembled in this scope.
- The dedicated CoreV4 opcodes that `average(24)`/`elemwise_mul(25)` route to — and the simulator's inline 1/N average functor — are sim/walrus-side and not pinned here.

---

## Related Components

| Name | Relationship |
|---|---|
| `sub_142E030` (libwalrus, `CoreV4Convert.cpp`) | The L1→L3 `AluOpType` wire converter — the comparison reorder lives here |
| `bir::EngineAccumulationType` | The enum the live `reduce_cmd` key actually deserializes to (not `ReduceCmdType`) |
| `Inst*::readFieldsFromJson` family | The 15 AluOpType consumers + the 3 mode-enum drivers |

## Cross-References

- [ISA Numeric Enum-Ordinal Tables](../isa/isa-enum-ordinals.md) — 2.23, the cross-ISA L1/L2/L3 ordinal crosswalk; documents the same AluOpType reorder from the L3 encoder side (byte-for-byte agreement is the cross-check)
- [TensorScalar / Cumulative / ScalarTensorTensor / Exp Encoding](../isa/tensorscalar-encoding.md) — 2.14, how the AluOpType `op0`/`op1` fields drive the tensor-scalar wire encoding
- [The `bir::Instruction` Base Struct](instruction-base.md) — the polymorphic root whose `Inst*` subclasses carry these enum fields
- [Build & Version Provenance](../reference/versions.md) — md5/build-id pins for `libBIR.so` and `libwalrus.so`
