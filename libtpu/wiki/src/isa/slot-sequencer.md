# Sequencer Slot

> *Every offset, address, bit position, and constant on this page was read byte-exactly from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped, 1,233,709 symbols). Other versions differ.*

## Abstract

Every TPU VLIW bundle carries exactly one **sequencer slot**: the single scalar-ALU lane that owns program-counter mutation. It is the lane that encodes branch / jump, call, halt, the pipeline-balancing delay op, the hardware-loop-counter read, and (on the SparseCore engines) the sync-flag and barrier ops. The matrix unit, the vector lanes, and the memory ports are *issued* by the bundle, but only the sequencer slot can change what executes next cycle. Functionally this is the on-chip control CPU of the TensorCore reduced to one slot in the issue word.

The slot's identity is byte-anchored across the silicon line. On Jellyfish (TPU v2) and Pufferfish (TPU v4) it is named `SLOT_SCALAR_0`; from Viperfish (v5e) onward it is `SLOT_SCALAR_ALU_0`. In both naming schemes the PC-mutating opcodes are legal **only in lane 0** — lane 1 (`SLOT_SCALAR_1` / `SLOT_SCALAR_ALU_1`) carries halt / fence / delay and a mirror of the scalar ALU, but never a branch or a call. On Jellyfish this rule is a literal bitmask in `ProtoUtils::ScalarOpAllowedInSlot` (`0x1e875a20`); on V5+ it is the proto-message structure `bundle.scalar_alu().scalar_alu_0().branch_relative()`. This page documents the slot as a reimplementation target: which lane it is per generation, the three-layer encode path that turns a control-flow intent into bundle bytes, the branch / call / halt / delay / loop-read field layout, and how the predication field doubles as the conditional-branch condition. The per-(generation × sequencer-type) op inventory lives on the [companion page](sequencer-ops-per-gen.md); the hardware-loop-counter detail on [Hardware Loop-Counter](slot-loop.md).

For reimplementation, the contract is:

- One sequencer slot per bundle, always lane 0 of the scalar-ALU sub-bundle; PC mutation is lane-0-only and the compiler must enforce it.
- The branch / call **target is a signed 20-bit field** (`−0x80000 .. +0x7FFFF`) that lands in **immediate slot 0** of the bundle, not inside the sequencer slot bytes; absolute-vs-relative is purely an opcode discriminator over the same field.
- A **call writes its return address into a `dest` scalar register** (link register `sreg #5` on the SparseCore path); there is no dedicated return opcode — a return is a `BranchSreg` that reads the link register.
- Every branch/call carries a **0..5 delay-slot count** (3-bit); on V5+ the delay slots are bundle-packer padding, not an encoded slot bit.
- The **predication field is the branch condition**: `PREDICATION_ALWAYS` = unconditional, a predicate-register index = conditional, `PREDICATION_NEVER` = gated off (the empty-slot encoding).
- Two loop models: Jellyfish uses a software bundle-index backward branch (`BeginLoop`/`EndLoop`); V5+ uses a hardware loop counter read via `ReadRegisterLccLow/High`.

| | |
|---|---|
| **Slot identity (JF/PF)** | `SLOT_SCALAR_0` / `SLOT_SCALAR_1` |
| **Slot identity (V5+)** | `SLOT_SCALAR_ALU_0` / `SLOT_SCALAR_ALU_1` |
| **PC-mutating lane** | lane 0 only (branch/call); lane 1 = halt/fence/delay mirror |
| **JF slot-legality rule** | `ProtoUtils::ScalarOpAllowedInSlot` @ `0x1e875a20` (slot-0 mask `0x18000000f00`) |
| **JF branch classifier** | `ProtoUtils::IsBranch` @ `0x1e876120` (`op & ~3 == 8` → 8..11) |
| **JF call classifier** | `ProtoUtils::IsCall` @ `0x1e876140` (`op & ~3 == 12` → 12..15) |
| **V5+ SCS branch encoder** | `isa_emitter::EmitBranchOp<…BranchRelative>` @ `0x13a5d3e0` |
| **V5+ SCS call encoder** | `isa_emitter::EmitCallOp<…CallAbsolute>` @ `0x13a5d4c0` |
| **`gfc` TC seq dispatch** | `gfc::isa::TensorCoreScalarAlu0Encoder::Encode` @ `0x1f87b420` |
| **`gfc` TC opcode-HIGH / family** | bit 483, width 6 (= `0` for branch/call-immediate) |
| **`gfc` TC opcode-LOW / discriminator** | bit 478, width 5 (`BranchAbsolute=4`/`Rel=5`/`CallAbs=6`/`Rel=7`) |
| **`gfc` TC seq predicate selector** | bit 489, width 2 (`*(scalar_alu+28)`) |
| **Branch/call target** | signed 20-bit, immediate slot 0 (`EmitImmediate<SparseCoreImmediates>`) |
| **Call link register** | `sreg #5` written into `dest` (SCS path) |
| **Delay-slot field** | 0..5 (3-bit); verifier `delay_slots_op.getImm() >= 0 && <= 5` |
| **Predication = condition** | `PREDICATION_ALWAYS/NEVER/OR_NEVER/OR_INVERTED_NEVER` |
| **V5+ predication encode** | `TPUMCCodeEmitter::encodePredicateOperand` @ `0x13c77c40` (7-bit) |
| **Empty-slot mark** | `kNeverExecute = 31` (`0xb834cfc`) |
| **Confidence** | CONFIRMED (byte-anchored) unless a row says otherwise |

---

## What the Sequencer Slot Is

A bundle is one VLIW issue word; the [bundle model](bundle-model-overview.md) covers the no-scoreboard contract. Within that word, the sequencer slot is the only lane whose op can alter the program counter. Issue a `Halt` and the core stops; issue a `BranchRelative` and the next bundle fetched is the branch target rather than the fall-through; issue a `CallAbsolute` and the fall-through address is captured into a scalar register so the callee can return to it. Everything else in the bundle — matrix push, vector ALU, loads, stores — executes "in place" and falls through to the next sequential bundle.

The hardware enforces a single sequencer per bundle by giving the scalar-ALU sub-bundle exactly two lanes and permitting PC mutation in only one of them. The constraint is concrete in the binary. `ProtoUtils::ScalarOpAllowedInSlot` (`0x1e875a20`) takes a `ScalarOpcode` and a slot index, and for slot index `≤ 1` it tests the opcode against two 64-bit bitmasks:

```c
// ProtoUtils::ScalarOpAllowedInSlot(ScalarOpcode op, int slot)  @ 0x1e875a20
// (decoded byte-exactly from objdump)
if (slot <= 1) {
    // either lane (lane-0 OR lane-1):
    if (bt(0x6000060070, op)) return true;     // movabs $0x6000060070; bt %rdx
    // lane-0 ONLY (PC-mutating + a couple of others):
    if (bt(0x18000000f00, op)) return (slot == 0);  // movabs $0x18000000f00; bt %rdx
}
```

The slot-0-only mask `0x18000000f00` has bits set at opcodes `{8, 9, 10, 11}` (the four branch opcodes) and `{39, 40}` — these are the lane-0-exclusive ops. The either-lane mask `0x6000060070` (bits `{4, 5, 6, 17, 18, 37, 38}`) covers the scalar ops legal in both lanes. A reimplementation that lets the packer place a branch in lane 1 produces a bundle the hardware rejects.

> **GOTCHA — the sequencer slot is one lane of a two-lane sub-bundle, not a whole bundle field.** The scalar-ALU sub-bundle has `scalar_alu_0` and `scalar_alu_1`; the sequencer ops are a `oneof` member of `scalar_alu_0` only. Lane 1 exists and carries ALU compute, halt, fence, and delay, but a branch/call in lane 1 is illegal. Modeling "the sequencer" as a single per-bundle field, rather than as lane 0 of a two-lane scalar sub-bundle, loses the lane-1 ALU capacity and mis-checks slot legality.

---

## Per-Generation Slot Identity and Position

The slot lives at lane 0 of the scalar-ALU sub-bundle in every generation, but the surrounding sub-core taxonomy changes: each TPU generation hosts several **sequencer types** (`TpuSequencerType`), and each sequencer type has its own bundle with its own scalar-ALU sub-bundle. The companion page enumerates the [TpuSequencerType enum](sequencer-ops-per-gen.md#the-tpusequencertype-enum); the table below pins the slot position per (generation × sequencer-type).

| Gen | Sequencer type | Bundle B | Sequencer lane | Lane 1 (no PC mutation) | Confidence |
|---|---|---:|---|---|---|
| Jellyfish (v2) | TensorCore | 41 | `SLOT_SCALAR_0` | `SLOT_SCALAR_1` (halt/fence/delay) | CONFIRMED |
| Jellyfish (v2) | BarnaCoreAddressHandler | 16 | dedicated BCAH `Branch` ScalarSlot | n/a | HIGH |
| Dragonfish (v3) | TC / BCAH | 41 / 16 | alias of Jellyfish codec | (as Jellyfish) | HIGH |
| Pufferfish (v4) | TensorCore | 51 | `Scalar0` (`TensorCoreScalar0_*`) | `Scalar1` (halt/fence/delay) | CONFIRMED |
| Pufferfish (v4) | BarnaCoreSequencer | 32 | `BarnaCoreSequencerScalar0` | `Scalar1` (halt/fence/delay + sync) | CONFIRMED |
| Viperfish (v5e) | TensorCore | 64 | `ScalarAlu0` (`vxc::isa`) | `ScalarAlu1` | CONFIRMED |
| Viperfish (v5e) | SCS / TAC / TEC | 32 / 64 / 64 | `ScalarAlu0` (`vxc::vfc::isa`) | `ScalarAlu1` | CONFIRMED |
| Ghostlite (v6e) | TC / SCS / TAC / TEC | 64 / 32 / 64 / 64 | `ScalarAlu0` (`gxc::glc::isa`) | `ScalarAlu1` | CONFIRMED |
| 6acc60406 (TPU7x) | TC / SCS / TEC | 64 / 32 / 64 | `ScalarAlu0` (`gxc::gfc::isa`) | `ScalarAlu1` | CONFIRMED |

> **NOTE — 6acc60406 (TPU7x) drops the TileAccess sequencer.** `gxc::gfc::isa::SparseCoreTac*` symbols are absent from the binary while `SparseCoreTec*` and `SparseCoreScs*` are present (`nm -C`). Viperfish and Ghostlite carry all three SparseCore sequencer engines (SCS + TAC + TEC); 6acc60406 carries SCS + TEC only. The TensorCore sequencer is present on every generation.

On V5+ the lane is reached through a fixed proto path, visible verbatim in the assertion strings the binary embeds: `bundle.scalar_alu().scalar_alu_0().branch_relative()`, `…scalar_alu_0().call_absolute()`, `…scalar_alu_0().halt()`. The `scalar_alu_0` message is a `oneof` whose members are the sequencer ops (`branch_absolute`, `branch_relative`, `branch_sreg`, `call_absolute`, `call_relative`, `call_sreg`, `halt`, `delay`, `scalar_fence`, `read_register_lcc_low`, `read_register_lcc_high`, plus the ALU compute ops). The Viperfish TC scalar-ALU `oneof` enumerated from the symbol table (`vxc::isa::TensorCoreScalarAlu_*`) holds the full control-flow set alongside ~60 ALU compute ops — the sequencer slot and the scalar-ALU lane are the same physical lane, distinguished only by which `oneof` member is set.

---

## The Three-Layer Encode Model

A control-flow intent becomes bundle bytes through three structures, layered:

1. **Per-gen proto message types** — the structured form. Each control-flow op is a distinct protobuf message: `vxc::isa::TensorCoreScalarAlu_BranchRelative`, `gxc::glc::isa::SparseCoreScalarAlu_CallAbsolute`, `pxc::isa::TensorCoreScalar0_ScalarBranchAbsolute`, and so on. On Jellyfish the form is a flat enum, `platforms_deepsea::jellyfish::isa::ScalarOpcode` (62 values), with a `ScalarOpcode_descriptor()` at `0x1fa1fc00`.

2. **Per-gen compact ref wrappers** — the read-side accessor form. Each op has a `*Compact_<Op>{,ConstRef,Ptr}` family whose accessors are virtual thunks over a per-gen `BitfieldsRefImpl` (the actual bit holder). The accessors confirm the field set: `…Compact_BranchSregConstRef::x()` is the branch-target register, `…Compact_CallSregConstRef::dest()` is the return-address register, and `…Compact_BranchAbsoluteConstRef::has_delay_slots()` / `delay_slots()` expose the per-branch delay count. These bits live behind a vtable, not inline — e.g. `gfc BranchSregConstRef::has_delay_slots()` is the vtable `+0x18` thunk, `delay_slots()` is `+0x20`, `x()` is `+0x40`.

3. **The byte-emission path** — turns the proto/MCInst into raw bytes. The SparseCore (SCS/TAC/TEC) lane uses the templated `xla::tpu::sparse_core::isa_emitter::EmitBranchOp<Bundle, Op>` / `EmitCallOp<Bundle, Op>` populators; the TensorCore (Jellyfish) lane uses `JellyfishEmitter::EmitScalar*`; the BarnaCore-AH lane uses `BarnaCoreAddressHandlerEmitter::EmitScalar*`. Final emission is per-gen: `EncoderJf` / `EncoderPf::EncodeBundleInternal` (dynamic byte packing) for JF/PF, and on V5+ the per-slot `<Slot>Encoder::Encode` calls (e.g. `gfc::isa::TensorCoreScalarAlu0Encoder::Encode` @ `0x1f87b420`) writing into the 64-byte buffer via the universal bit-packer `BitCopy` (`0x1fa0a900`).

> **GOTCHA — the V5+ LLVM-MC emitter contributes nothing to a branch's bits.** The MC opcodes `BRabs` (505), `BRind` (507), `BRrel` (508), `BRrelrot` (509), `CALLabs` (514), `CALLrel` (515), `HALT` (571) reach `TPUMCCodeEmitter::getBinaryCodeForInstr` (`0x13c74da0`), index its jump table, and route to the **zero-base default** — their `InstBits` record is all zero. The real offset, dest/x registers, and predication are written by the proto-bundle `EmitBranchOp` / `EmitCallOp` / `EmitImmediate` / `EmitPredicationToSlot` path. See [MC-Emitter](mc-emitter.md) and [Instruction Name Data](instr-name-data.md).

---

## Branch / Jump / Call Encoding

The branch and call target is a **signed 20-bit field** that lands in **immediate slot 0** of the bundle, decoded byte-exactly from the Ghostlite SCS branch and call emitters. `EmitBranchOp<…BranchRelative>` (`0x13a5d3e0`) and `EmitBranchOp<…BranchAbsolute>` (`0x13a5d220`) are field-identical — the abs/rel distinction is only the opcode discriminator:

```c
// isa_emitter::EmitBranchOp<SparseCoreScalarAlu_BranchRelative> @ 0x13a5d3e0
// (decoded byte-exactly from objdump)
rsi = MCInst.getOperand(0).Imm;        // the PC offset / target
if ((uint64_t)(rsi + 0x80000) >= 0x100000)   // lea 0x80000(%rsi); cmp 0x100000; jae fail
    return RetCheckFail();             // reject if NOT in [-0x80000, +0x7FFFF]
bundle.byte[0x10] |= 0x04;             // orb $0x4, 0x10 — set scalar-alu PRESENT bit (bit 2)
rsi &= 0xFFFFF;                        // and $0xfffff — mask to 20 bits
EmitImmediate<SparseCoreImmediates>(slot=0, rsi);  // 20-bit value → immediate slot 0
```

The range check `(value + 0x80000) < 0x100000` is exactly the signed-20-bit test: `−524288 .. +524287`. Both `BranchAbsolute` and `BranchRelative` write the same field; the opcode decides whether the 20-bit value is a PC-relative delta or a bundle-index absolute target.

A **call** adds the return-address mechanism. `EmitCallOp<…CallAbsolute>` (`0x13a5d4c0`):

```c
// isa_emitter::EmitCallOp<SparseCoreScalarAlu_CallAbsolute> @ 0x13a5d4c0
// (decoded byte-exactly from objdump)
assert(operand.kind == 5);             // cmpb $0x5 — MCImmExpr
offset = GetValueFromSubExpr(GetTPUMCImmExpr(...));
if ((uint64_t)(offset + 0x80000) >= 0x100000) return RetCheckFail();  // SAME 20-bit check
bundle.byte[0x10] |= 0x04;             // scalar-alu present bit
EmitImmediate<SparseCoreImmediates>(slot=0, offset);   // 20-bit target → imm slot 0
link = GetSregno(MCOperand{kind=1, reg=5});  // movq $0x5; GetSregno — LINK REG = sreg #5
bundle.dword[0x18] = link;             // mov %eax, 0x18 — write dest (return-addr) sreg
bundle.byte[0x10] |= 0x01;             // orb $0x1 — set dest-present bit (bit 0)
```

So a call is `{20-bit target in imm slot 0} + {dest = return-address scalar register}`. On the SCS path the link register is hardcoded to `sreg #5`. The callee returns by branching to the value in `dest` — a `BranchSreg` reading the link register. **There is no dedicated return opcode in any generation.**

The indirect forms read a computed target from a register: `BranchSreg` has an `x()` field (the target sreg); `CallSreg` has both `x()` (target) and `dest()` (return address). On Jellyfish the indirect branch instead reads a Branch-Target-Register set by a scalar `SET_BRANCH_TARGET_REGISTER` op or a TTU `set_btr` op — the binary embeds the conflict assertion *"Cannot have a scalar SET_BRANCH_TARGET_REGISTER instruction and a TTU set_btr instruction in the same bundle."*

| Field | Where | Width | Source | Confidence |
|---|---|---:|---|---|
| branch/call target | immediate slot 0 (bit 423 on GF) | 20 (signed) | `EmitBranchOp` @ `0x13a5d3e0`; `EmitImmediate` | CONFIRMED |
| abs vs rel vs indirect | opcode discriminator | — | distinct proto message / `ScalarOpcode` | CONFIRMED |
| call dest (return-addr sreg) | `dest` field, bundle `+0x18` (SCS); GF bit 467 | 5 | `EmitCallOp` @ `0x13a5d4c0` | CONFIRMED |
| call link register (SCS) | `sreg #5` | — | `movq $0x5; GetSregno` @ `0x13a5d560` | CONFIRMED |
| indirect target (`x()`) | `BranchSreg`/`CallSreg` reg field; GF bit 472 | 6 (GF) | compact ref `x()` accessors | CONFIRMED |

> **GOTCHA — the branch target does not live in the sequencer slot bytes.** Both abs and rel write a 20-bit value into a *shared immediate slot* (slot 0) of the bundle, and only an opcode bit in the sequencer slot says how to interpret it. A decoder that searches the sequencer-slot byte window for a 20-bit offset finds nothing; the offset is in the bundle's immediate region. This is the same immediate slot the sync ops reuse for the sflag id/threshold.

On the V5+ TensorCore lane the abs/rel/call discriminator is written **inside** the sequencer slot — the SCS *emitter* path above is the SparseCore engines; the TensorCore engine has its own per-op `BitCopy` populators. The `gfc` TensorCore slot is decoded byte-exactly from the per-op encoders and the dispatching `TensorCoreScalarAlu0Encoder::Encode` (`0x1f87b420`). Every absolute bit position below is **LSB-first** — bit 0 is the least-significant bit of byte 0, matching the universal `BitCopy(dst, dst_bit, src, src_bit, nbits)` packer (`0x1fa0a900`) the [bundle model](bundle-model-overview.md) documents. Every TC sequencer op begins by writing a **6-bit opcode-HIGH "family" field at bit 483** and a **5-bit opcode-LOW "discriminator" at bit 478**; the branch/call-immediate family pins opcode-HIGH to `0` and selects the op via the LOW field:

```c
// EncodeTensorCoreScalarAlu0BranchAbsolute @ 0x1f87f5c0 (decoded byte-exactly)
BitCopy(slot, 483, 0, 6);              // opcode-HIGH "family" = 0 (branch/call-immediate)
BitCopy(slot, 478, 4, 5);              // opcode-LOW discriminator = 4 → BranchAbsolute
// …if x() present:
BitCopy(slot, 472, x_reg, 6);          // 6-bit operand / 2nd-source field
```

The four immediate branch/call discriminators are field-identical except for the LOW value; the register-indirect forms (`BranchSreg`/`CallSreg`) instead carry their opcode in the **HIGH** field. A call's return-address sreg lands in a **5-bit field at bit 467** and the indirect target / link-source sreg in the **6-bit field at bit 472**:

| Op | `oneof` case | opcode-HIGH @483 (w6) | opcode-LOW @478 (w5) | Encoder | Confidence |
|---|---:|---:|---:|---|---|
| `BranchAbsolute` | 62 | 0 | **4** | `0x1f87f5c0` | CONFIRMED |
| `BranchRelative` | 63 | 0 | **5** | `0x1f87f660` | CONFIRMED |
| `CallAbsolute` | 65 | 0 | **6** | `0x1f87f7e0` | CONFIRMED |
| `CallRelative` | 66 | 0 | **7** | `0x1f87f8e0` | CONFIRMED |
| `BranchSreg` | 64 | **4** | (x at 472, dest n/a) | `0x1f87f700` | CONFIRMED |
| `CallSreg` | 67 | **5** | (x at 472, dest at 467) | `0x1f87f9e0` | CONFIRMED |

`CallAbsolute`/`CallRelative` additionally write the return-address sreg into a **5-bit field at bit 467** (`BitCopy(slot, 467, dest, 5)`) and the call-target / link-source sreg into the 6-bit field at bit 472 — confirming the SCS-path return-address mechanism is present byte-for-byte on the TensorCore lane too. The opcode-HIGH `0` family is shared by the non-control sequencer ops as well (`ScalarFence` LOW=0, `Delay` LOW=3, `SetTag` LOW=8, `ReadRegisterLccLow` LOW=10), so the LOW discriminator alone identifies a branch/call only within the HIGH=0 family. This map matches [Bundle GF §Sequencer Slot](bundle-gf.md#sequencer-slot-and-the-20-bit-branch-offset).

> **NOTE — the SCS and TC lanes share the *encoding contract* but not the *code path*.** The SparseCore (SCS/TAC/TEC) sequencer reaches its bytes through the LLVM-MC-driven `isa_emitter::EmitBranchOp` / `EmitCallOp` templates; the TensorCore sequencer reaches its bytes through the proto-bundle `gfc::isa::Encode*` populators dispatched by `TensorCoreScalarAlu0Encoder::Encode`. Both land a 20-bit signed target in immediate slot 0 and an abs/rel/call discriminator in the sequencer slot, but a reimplementation must not assume one function emits both — they are separate populator families keyed by `TpuSequencerType`.

The Jellyfish branch/call discriminators are the contiguous `ScalarOpcode` ranges decoded byte-exactly:

```c
ProtoUtils::IsBranch(op): (op & ~3) == 8    // and $0xfffffffc; cmp $0x8  → {8,9,10,11}
ProtoUtils::IsCall(op):   (op & ~3) == 0xc  // and $0xfffffffc; cmp $0xc  → {12,13,14,15}
```

Four branch opcodes (`ScalarBranchRelative` / `ScalarBranchAbsolute` / `ScalarBranchIndirect` + one) and four call opcodes (`ScalarCallRelative` / `ScalarCallAbsolute` / `ScalarCallIndirect` + one). The first three names of each range are confirmed from `.rodata` strings; the exact integer↔name binding inside `8..11` / `12..15` is inferred from contiguity (MEDIUM).

---

## Delay-Slot Field

Every branch and call carries a delay-slot count — the number of bundles after the branch that issue before the PC change takes effect. The accessors are present on every branch/call op (`scalar_alu_0.branch_absolute().has_delay_slots()`, `…call_sreg().has_delay_slots()`, etc.), and the LLVM-MC verifier bounds the value:

```text
delay_slots_op.getImm() >= 0 && delay_slots_op.getImm() <= 5
```

So the delay-slot count is a **0..5 field (3 bits)**. The packer appends that many empty bundles after the branch bundle; the per-gen base count lives at `TpuSubtarget +0x914` (its exact per-gen value is not extracted — MEDIUM). The standalone `Delay` op (`…Compact_Delay::delay_count()`) is a distinct pipeline-balancing NOP-with-count, not the per-branch delay. On V5+ there is no in-bundle delay-slot bit — the count is purely a packer pad-count (the GF sequencer slot has no delay-slot field). `MarkBranchDelaySlot(bool)` emitter methods exist for JF / JF-BCAH / PF-TC / PF-BCS / PF-BCChan / VF-TC / GL-TC; no such symbol exists for 6acc60406 (gfc), consistent with the V5+ no-encoded-delay model.

---

## Condition Encoding — Predication *is* the Condition

There is no separate branch-condition field. The sequencer slot's **predication field doubles as the conditional-branch condition**. The binary embeds the assertion `!scalar_alu_0.has_predication() || scalar_alu_0.predication() == PREDICATION_ALWAYS` — an unconditional branch must have predication `ALWAYS` or none — and the query `IsConditional(scalar_alu_0)` (`ProtoUtils::IsConditional`) tests the slot's predication to decide whether the branch is conditional.

The predication enum values, from `.rodata` strings:

| Value | Meaning |
|---|---|
| `PREDICATION_ALWAYS` | unconditional (predicate true) |
| `PREDICATION_NEVER` | slot gated off (NOP / never-execute) |
| `PREDICATION_OR_NEVER` | predicate-OR variant |
| `PREDICATION_OR_INVERTED_NEVER` | predicate-OR with inversion |
| `PREDICATION_SLOT_NEVER` | slot-level never (V5+ dual-predicate) |

A conditional branch encodes a predicate-register index in the predication field; the branch is taken iff that predicate is true (or, for the OR/inverted variants, the combined predicate). On Jellyfish the predication field is **5 bits** per slot, with the constants byte-exact:

```text
HardwareBundleBits::kPredicateRegisterCount = 15   (0xb834cf4: 0x0f)
HardwareBundleBits::kAlwaysExecute          = 15   (0xb834cf8: 0x0f)
HardwareBundleBits::kNeverExecute           = 31   (0xb834cfc: 0x1f)
```

So values `0..14` are predicate registers P0..P14, `15` is always-execute, `31` is never-execute. An empty (unfilled) slot is left at `kNeverExecute = 31`, which is the canonical empty-slot encoding — see [Bundle Model §Empty-Slot Convention](bundle-model-overview.md#empty-slot-and-nop-convention).

On V5+ the predication is a **7-bit field**, encoded byte-exactly by `TPUMCCodeEmitter::encodePredicateOperand` (`0x13c77c40`):

```c
// encodePredicateOperand @ 0x13c77c40 (decoded byte-exactly)
reg = reg_encoding_table[op.reg];      // movzwl (%rdi,%rcx,2)
APInt::insertBits(out, reg, /*pos=*/0, /*width=*/4);   // bits [0:3] = predicate-reg index
if (flag_byte & 1)                     // test $0x1, %r14b
    out.flags |= 0x10;                 //   bit [4] = predicate sense / present
mode = (flag_byte >> 5) & 3;           // shr $0x5; and $0x3
APInt::insertBits(out, mode, /*pos=*/5, /*width=*/2);  // bits [5:6] = predication MODE
```

That is `{4-bit reg, 1-bit sense, 2-bit mode}` = a 7-bit field, a superset of Jellyfish's 5-bit field. The four modes correspond to `ALWAYS` / `NEVER` / `OR_NEVER` / `OR_INVERTED_NEVER`. When the branch target is a yet-unresolved label rather than an inline immediate, the operand takes the LLVM-MC fixup path (`getMachineOpValue` @ `0x13c777e0`, 24-bit operand class for label relocations).

> **NOTE — 6acc60406 (GF) adds dual predication.** 6acc60406 (gfc) replaces the per-slot 4-bit register index with a dedicated dual-predicate slot (`TensorCorePredicates`, a two-entry `(reg, invert)` pool at bits 496..505) plus a per-slot 2-bit *selector* (sequencer slot @ bit 489). A GF conditional branch can be guarded by either of the two per-bundle predicates; the GF SparseCore adds a rotating-predicate branch (`BranchRelativeRotatingPreg`) that reads the rotating-predicate ring. See [Bundle GF §Dual-Predicate Slot](bundle-gf.md#the-dedicated-dual-predicate-slot-gf-vs-ghostlite).

---

## Hardware Loop and Sync — Where They Live

The two loop models split cleanly across generations. **Jellyfish / Dragonfish** have no loop-counter register; a hardware loop is a *software bundle-index backward branch* built by `AddressHandlerProgramBuilder::BeginLoop` (`0xfa90d40`) / `EndLoop` (`0xfa91300`): `BeginLoop` records the current bundle index (asserting no nested loop is active), and `EndLoop` emits a backward branch to that index. **Viperfish / Ghostlite / 6acc60406** have a 64-bit hardware loop counter (LCC) read by the sequencer ops `ReadRegisterLccLow` / `ReadRegisterLccHigh` (low + high 32-bit halves) into a scalar register; the loop body uses a conditional `BranchRelative` guarded by a predicate computed from the LCC value. The LCC read ops are present on TC and SCS for all of VF/GL/GF and absent on JF/PF (`nm -C`). The full loop detail is on [Hardware Loop-Counter](slot-loop.md).

Sync ops do **not** live in the sequencer scalar slot on most generations. On Jellyfish, sync-flag work is issued from the *vector* path (`JellyfishEmitter::EmitVectorSyncFlagSet/Add/...`); on Pufferfish BCS, dedicated sync ops sit in both `Scalar0` and `Scalar1`; on V5+ a dedicated `ScalarMisc` lane (separate from the `ScalarAlu0` sequencer lane) owns the sync family. The barrier op encoder `EmitBarrierSync<…ScalarMisc>` (`0x13a5f100`) sets the `ScalarMisc` present bit and writes the sflag id/threshold through a `ScalarY` operand whose immediate form reuses the same 20-bit immediate slot 0 the branch offset uses. The per-gen sync-slot location and op roster are tabulated on the [companion page](sequencer-ops-per-gen.md#sync-ops-and-where-they-live).

---

## Cross-References

- [Bundle Model](bundle-model-overview.md) — the VLIW bundle, the codec keyed by `(TpuVersion, TpuSequencerType)`, and the empty-slot `kNeverExecute` convention.
- [Sequencer Ops Per Gen](sequencer-ops-per-gen.md) — the per-(generation × sequencer-type) control-flow op inventory and the `TpuSequencerType` enum.
- [Hardware Loop-Counter](slot-loop.md) — the JF software bundle-index loop vs the V5+ LCC counter read.
- [SPU / Scalar Slot](slot-spu-scalar.md) — the scalar-ALU compute ops sharing the lane with the sequencer.
- [MC-Emitter](mc-emitter.md) — why V5+ branch/call/halt opcodes route to the zero-base default.
- [Bundle GF](bundle-gf.md) — the byte-exact GF sequencer slot (selector @ 489, opcode-HIGH @ 483) and dual-predicate slot.
