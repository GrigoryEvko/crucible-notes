# SP Engine — the TPB Control Processor

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel). The wire encoders live in `libwalrus.so` (build-id `92b4d331`; `CoreV2GenImpl`/`Generator` — the L3 64-byte-bundle emitters), the IR structs, enums and JSON serde in `libBIR.so` (build-id `a9b1ea38`; the `bir::` namespace), and the functional reference in `libBIRSimulator.so` (the `birsim::` model). Field offsets quoted as `+0xNN` are byte offsets re-derived from the simulator and dumper bodies this pass; see [Build & Version Provenance](../reference/versions.md). Other wheels differ — treat every address as version-pinned.*

## Abstract

The **SP (Sync/Sequencer) engine** is the TPB's per-NeuronCore **control processor**: the thing that owns the program counter, the scalar register file, branch / call / return control flow, the semaphore bank, and the DMA-queue trigger. It is `EngineType::SP` (= 6). It is *not* a datapath engine in the matmul sense — it computes only on scalars in its register file — but every datapath engine (PE, Pool, Activation, DVE) also carries an **SP-class sequencer + register file**, so the SP control ops run on whichever engine the schedule placed them on. SP is where a compiled kernel's *control flow* and *DMA orchestration* live, after the structured loops and barriers of the middle-end have been flattened.

A reimplementer needs to get four things right, and they are the four sections of this page:

1. **The scalar Register-ALU.** `RegisterAlu` (`InstSyncType=1`, opcode `0xA8`) is a 3-operand integer ALU on the named register file (`dst ← src1 <op> src2`). The BIR `op` key — a `bir::AluOpType` read from `InstRegisterAlu+0xF0`, cross-checked in two binaries — is wire-encoded by a **30-arm jump table**, not a flat byte LUT: the table identity-maps the arithmetic head (`0..18`) then *permutes* the comparison block and the transcendental tail, and **FATALs on three out-of-range variants** (`average`, `elemwise_mul`, and the `abs_max`/`abs_min`/`mod_int` tail). This is the headline.

2. **The fused compare-and-branch.** There is **no separate compare-then-branch pair**. `CompareAndBranch` and `UnconditionalBranch` *share* one silicon control-branch (opcode `0xA9`, `CTRL_BR`): the SP evaluates `lhs <pred> rhs` and jumps in **one** issue. The simulator proves this byte-exact — one `compareScalarArgs(lhs,rhs,op)` call selects between `on_true`/`on_false` and writes the chosen successor into the next-BB slot. Unconditional branch is the same op with the predicate cleared to "always."

3. **The sequencer model.** The functional reference realizes the SP sequencer with four runtime control-state structures, all re-derived byte-exact this pass: a **next-BB slot** (`InstVisitor+0xE10`) that *is* the program-counter advance (every terminator writes it; the CFG driver reads-and-nulls it after each block), an **affine-env DenseMap** (`+0xE18`) carrying the induction/loop-variable values, a **call-stack** of activation frames, and a **DMATrigger fireCount DenseMap** (`+0xE60`) — the queue round-robin cursor.

4. **The register file and its coloring allocator.** A `bir::Register` is a *named* scalar register spanning `numPhysicalRegs` physical slots (1 for 32-bit, 2 for 64-bit). The file size is a per-engine HW constant, `HwmCore->MaxRegNumPerEngine`. Allocation is a **graph-coloring allocator with spilling** — the dedicated `REG_Allocator` (`simplify`/`live_range` — the classic simplify/select phases), confirmed as both a `libwalrus.so` symbol and a standalone `coloring_allocator_with_loop` tool binary.

Everything structured — `for`, `while`, `do-while` — is **fully lowered before SP codegen**: the middle-end explodes a loop into a counter `RegisterAlu` op plus a back-edge `CompareAndBranch`, so on real silicon the SP sequencer only ever sees the *flat* scalar branch pair. This page covers the functional/architectural model — what the SP engine does and how its state moves. The bit-level field encoding of the 64-byte bundles is [ISA 2.19 (SP register-lane / TensorLoad-Save)](../isa/sp-register-encoding.md) and [ISA 2.20 (SP sync / branch / control encoding)](../isa/sp-sync-encoding.md); the simulator sequencer is [BIR Part 7](../bir/sim-control-sync-customop.md); the register allocator is [walrus Part 8](../walrus/alt-allocators-dma-opt.md).

| | |
|---|---|
| **Engine** | `EngineType::SP` (= 6); `EngineType2string` (`libBIR 0x47fa80`) → 8 values `{0 Unassigned, 1 Pool, 2 Activation, 3 PE, 4 DMA, 5 DVE, 6 SP, 7 ALL}` (see [the EngineType ordinal map](arch-object-model.md#the-enginetype-ordinal-map)) |
| **Family signature** | `InstSyncType = 1` (`sequencer`) for *every* SP op — vs `0` datapath (compute), `2` dma (transfer) |
| **Bundle size** | 64 bytes (`inst_word_len = 16` dwords); `setupHeader` stamps `byte[0]=opcode`, `byte[1]=0x10`, `bytes[2:3]=0` → opcode word `0x10<op>` LE |
| **Scalar ALU** | `RegisterAlu` (op 73, opcode `0xA8`); `op` field @ `InstRegisterAlu+0xF0` = `bir::AluOpType` (sim + dumper); 30-arm wire jump table |
| **Scalar move** | `RegisterMove` (op 74, opcode `0xA7`); `mode@+0x0C` (1=32b / 2=64b), `dtype@+0x0D`, `dst reg-lo@+0x18` / `hi@+0x19` |
| **Branch** | `CompareAndBranch` (op 78) + `UnconditionalBranch` (op 79) share opcode `0xA9` / `CTRL_BR`; `comp_op@+0xF0` = `bir::BranchCompareOp`, `on_true@+0xF8`, `on_false@+0x100` (sim + dumper) |
| **DMA trigger** | `DMATrigger` (op 67, opcode `0xC1`); `getDefaultEngine` → 6 (SP); fires the Nth `DMABlock` then advances the fireCount cursor |
| **Queue ops** | `SwitchQueueInstance` (op 85, `0xCF`), `ResetQueueInstance` (op 86, `0xC2`) — rewind the round-robin fireCount cursor |
| **Semaphore bank** | 256-entry counting-sema + 256-bit event bitset; one wait + one update per bundle at `+0x04..+0x08` |
| **PC mechanism** | next-BB slot `InstVisitor+0xE10`; every terminator writes it, `visitBBHolderInControlFlow` @ `0x20f820` reads-and-nulls it |
| **Affine env** | `InstVisitor+0xE18` `DenseMap<AffineIdx,long>` — induction/loop-variable values (`lea` @ `0x20fbf4`) |
| **DMA cursor** | `InstVisitor+0xE60` `DenseMap` — queue round-robin fireCount (`lea` @ `0x2124a4`) |
| **Register file** | named `bir::Register`, `numPhysicalRegs` ∈ {1,2}; `getRegId(i)`/`setNumPhysicalRegs(i)` (libBIR symbols); size = `core.MaxRegNumPerEngine` = **62** (`Core+0x3c`, uniform across all four arch models) |
| **Allocator** | `REG_Allocator` (`simplify`, `live_range`) — graph-coloring + spill; `ColoringAllocatorWithLoop`; standalone `coloring_allocator_with_loop` binary |

---

## 1. The SP Instruction Family

The SP-engine ISA partitions into five facets. Every one of them is `InstSyncType = sequencer` (= 1) — that shared sync class is the signature that proves they ride the same control path, distinct from `datapath` (0, compute engines) and `dma` (2, transfer):

| Facet | Instructions (BIR opcode, wire opcode) |
|---|---|
| **(a) Sequencer control** (PC + call stack) | `CompareAndBranch` (78, `0xA9`), `UnconditionalBranch` (79, `0xA9`), `BranchHint` (80, `0xDD`), `Call` (84, `0xD3`), `Return` (`0xD2`), `Exit` (`0xDF`), `NoOp` (`0xA4`), `Break` (lowered away) |
| **(b) Semaphore / sync** | `EventSemaphore` (13, `0xA0`), `GroupResetSemaphores` (14, `0xB0`), `AllEngineBarrier` (15, `0xD5`), `Drain` (16, `0xA2`), `Halt` (17, `0xA1`) |
| **(c) DMA-trigger + queue** | `DMATrigger` (67, `0xC1`), `SwitchQueueInstance` (85, `0xCF`), `ResetQueueInstance` (86, `0xC2`) |
| **(d) Scalar register file** | `RegisterAlu` (73, `0xA8`), `RegisterMove` (74, `0xA7`) |
| **(e) Rank / completion** (collective context) | `GetCurProcessingRankID` (66, `0xDB`), `GetGlobalRankId` (11, `0xDC`), `TensorCompletion` (94, `0xDE`), `LoadActFuncSet` (6, `0x23`) |

> **NOTE — what "SP" means physically.** SP is a member of the datapath mask `isDataPathEngine = (1<<e) & 0x6E = {1,2,3,5,6}` (Pool, Activation, PE, DVE, SP), so it counts as datapath for barrier/fence purposes, but it has no tensor datapath of its own — it computes only on scalars. Each compute engine carries its own SP-class sequencer and register file. When the scheduler places a `RegisterAlu` or a `CompareAndBranch`, it homes it onto one engine's sequencer; `lower_branch` re-homes any branch left on `Unassigned`/`ALL` onto SP before codegen.

### The Shared 64-Byte ABI

Every SP encoder is a `Generator` method that runs in one of three `CodeGenMode`s (`Generator+624` selects):

```c
// CodeGenMode dispatch shared by every SP encoder (Generator+624)
switch (codeGenMode) {
  case 0: /* COLLECT_OPCODES  */ opcodeSet.insert(opcode);
                                 updateBranchHintTargetPC(inst);   // build the PC map
                                 break;
  case 1: /* GENERATE_ISACODE */ setupHeader(bundle, opcode);      // stamp opcode word
                                 fieldWalk(bundle, inst);          // per-op field writes
                                 fwrite(bundle, 1, 0x40, out);     // exactly 64 bytes
                                 /* + 3 accounting maps */         break;
  case 2: /* RUN_ISA_CHECKS   */ setupHeader(stackBundle, opcode); // same layout, no fwrite
                                 fieldWalk(stackBundle, inst);
                                 validate(stackBundle);            break;  // vtable validator
}
```

`setupHeader` is a six-instruction body: `bundle[1] = 16; bundle[0] = opcode; *(u16*)(bundle+2) = 0`. So the opcode word at `+0x00` is always `0x10<op>` little-endian, where `0x10` is the instruction-word length in dwords (16 × 4 = 64 bytes). The wire layout is byte-identical in modes 1 and 2; only mode 1 actually writes the bundle out.

> **GOTCHA.** The single biggest field-layout pitfall: the BIR-side "key" offset (where the IR struct stores a value, e.g. `op` at `InstRegisterAlu+0xF0`) is **not** the wire-bundle offset (where the encoder writes the encoded byte, e.g. the ALU wire byte at `bundle+0x0C`). The `+0xF0`/`+0xF8`/`+0x100` family are *struct* offsets read from the BIR instruction; the `+0x0C`/`+0x10`/… family are *bundle* offsets written to the 64-byte word. Confusing the two is the error corrected in §6.

---

## 2. The Scalar Register-ALU — the Headline

`RegisterAlu` (op 73, opcode `0xA8`) is the SP's arithmetic unit on the scalar register file. The BIR `InstRegisterAlu` carries the op selector in a `bir::AluOpType` field at struct offset `+0xF0` (BIR JSON key `"op"`). This is confirmed **byte-exact in two independent binaries**:

```text
libBIRSimulator  birsim::InstVisitor::visitInstRegisterAlu @ 0x1bee20:
    0x1bee7a:  mov  r15d, [r15+0F0h]          ; load op = AluOpType from InstRegisterAlu+0xF0
    0x1bee54:  call getArgument<RegisterAccess>(0)   ; src operand
    0x1bee61:  call getOutput<RegisterAccess>(0)     ; dst operand
    0x1bee6f:  call RegState::read(Argument&)        ; read src register value

libBIRParserDumper  bir::Dumper::visitInstRegisterAlu @ 0xa2610:
    0xa271e:   lea  rsi, [rbp+0F0h]                  ; &inst.op
    0xa2752:   call bir::to_json(json&, AluOpType const&)
    0xa2783:   lea  r15, "op"                        ; JSON key
    0xa2a2d:   lea  rsi, "is_64bit"                  ; sibling key
```

The BIR keys on `InstRegisterAlu` are `op`, `is_64bit`, `dge_type`, `reduce_cmd`, `acc`; `op` and `is_64bit` are read directly in the dumper, the rest are the `klr::RegisterAluOp` serde fields). The lowering origin is `klr::RegisterAluOp` (`klr::RegisterAluOp_des`/`_ser`) → `bir::InstRegisterAlu`.

### 2.1 The 30-Arm Wire Jump Table

`bir::AluOpType` has **33 values** (`AluOpType2string` bounds `esi ≤ 0x20` = 32 = max index). The SP encoder accepts only `0..29` (it bounds the wire input `edi ≤ 0x1D`); it **rejects** `average`(24) and `elemwise_mul`(25), and it never sees the `abs_max`/`abs_min`/`mod_int` tail (30–32), which are out of bound. The op→wire-byte mapping is a **30-arm jump table** (a computed `jmp`, *not* a flat byte LUT), and it is *not* an identity map: it identity-maps the arithmetic head `0..18`, then permutes the comparison block, then scatters the transcendental tail.

```c
// AluOpType → wire byte, the 30-arm jump table behind RegisterAlu
// bundle+0x0C receives this wire byte. arms 24,25 + the (30..32) tail FATAL.
static uint8_t aluop_to_wire(bir::AluOpType op) {
  switch (op) {                       // BIR name             wire byte
    case  0: return 0x00;             // bypass               0x00
    case  1: return 0x01;             // bitwise_not          0x01
    case  2: return 0x02;             // arith_shift_left     0x02
    case  3: return 0x03;             // arith_shift_right    0x03
    case  4: return 0x04;             // add                  0x04
    case  5: return 0x05;             // subtract             0x05
    case  6: return 0x06;             // mult                 0x06
    case  7: return 0x07;             // divide               0x07
    case  8: return 0x08;             // max                  0x08
    case  9: return 0x09;             // min                  0x09
    case 10: return 0x0a;             // bitwise_and          0x0a
    case 11: return 0x0b;             // bitwise_or           0x0b
    case 12: return 0x0c;             // bitwise_xor          0x0c
    case 13: return 0x0d;             // logical_and          0x0d
    case 14: return 0x0e;             // logical_or           0x0e
    case 15: return 0x0f;             // logical_xor          0x0f
    case 16: return 0x10;             // logical_shift_left   0x10
    case 17: return 0x11;             // logical_shift_right  0x11
    case 18: return 0x12;             // is_equal             0x12   ← identity ends here
    // --- the comparison block PERMUTES ---
    case 19: return 0x18;             // not_equal            0x18
    case 20: return 0x13;             // is_gt                0x13
    case 21: return 0x14;             // is_ge                0x14
    case 22: return 0x16;             // is_lt                0x16
    case 23: return 0x15;             // is_le                0x15
    case 24: fatal("Invalid enum variant for enum AluOpType");  // average     — REJECTED
    case 25: fatal("Invalid enum variant for enum AluOpType");  // elemwise_mul— REJECTED
    // --- the transcendental tail SCATTERS ---
    case 26: return 0x1a;             // pow                  0x1a
    case 27: return 0x1b;             // mod                  0x1b
    case 28: return 0x1d;             // rsqrt                0x1d
    case 29: return 0x19;             // abs                  0x19
    default: fatal("Invalid enum variant for enum AluOpType");  // 30..32 out of bound
  }
}
```

> **NOTE — provenance of the 30-arm count and the wire bytes.** The op-field read at `InstRegisterAlu+0xF0` and the JSON serde are read directly here (sim `0x1bee7a` + dumper `0xa2752`). The wire-byte map and the 30-arm dispatch come from the `libwalrus.so` encoder's byte reads, but that encoder body is not present as a non-thunk in this disassembly slice, so the *table* is one step less direct than the field offsets. Its structure — identity head, permuted comparisons, scattered transcendentals, two FATAL arms — is internally consistent with the enum (`AluOpType` ordering `…is_equal, not_equal, is_gt, is_ge, is_lt, is_le, average, elemwise_mul, pow, mod, rsqrt, abs`).

> **QUIRK — why the permutation.** The wire encoding is the *silicon ISA* opcode space, not the BIR enum order. The compiler enum was extended over time (`average`, `elemwise_mul`, the `abs_*`/`mod_int` tail are newer BIR ops that the SP scalar ALU does not implement), so the comparison block and the transcendentals keep their *original* silicon opcodes (`0x13`–`0x1d`) while the BIR enum slots renumbered around them. A reimplementer must encode through this table — feeding the BIR index straight to the wire produces wrong comparisons (`not_equal` would become `0x13` = `is_gt`).

### 2.2 The Bundle Layout

```text
RegisterAlu 64-byte bundle (r12 in the encoder)
  +0x00  opcode 0xA8 / word 0x10A8        setupHeader
  +0x0C  ALU-op wire byte                  aluop_to_wire(inst+0xF0)   ← the §2.1 table
  +0x0D  op-present = 1                     constant
  +0x0E  secondary mode = 0                 constant (32-bit / non-accumulate)
  +0x10  dst reg-lo                         getOutput<RegisterAccess>(0).getRegId(0)
  +0x11  src1 reg id                        getArgument<RegisterAccess>(.).getRegId
  +0x12  src2 reg id                        getArgument<RegisterAccess>(.).getRegId
  +0x13  src reg id (3rd operand)           getArgument<RegisterAccess>(.).getRegId
  +0x14  dst reg-lo / +0x15 dst reg-hi      getRegId(0) lo ; getRegId(1) hi (64-bit pair)
```

So `RegisterAlu` is a **3-operand register ALU**: `dst ← src1 <op> src2` (the simulator's `doRegisterAlu<int>`/`doRegisterAlu<long>` confirm the two-source form, with multiple `getArgument<RegisterAccess>` reads in `visitInstRegisterAlu`). A 64-bit op pairs two consecutive physical registers (`+0x14` lo, `+0x15` hi). dtype guards from `.rodata`: the 32-bit ALU accepts `uint32`/`int32`/`uint64`/`int64` as `src1`; the 64-bit ALU accepts `uint64` only; `"offset register cannot be applied to 32bit Addressing in ISA"`.

> **NOTE.** The simulator reads the op at `+0xF0` and then dispatches on the *signed-int vs long* width to choose `doRegisterAlu<int>` or `doRegisterAlu<long>` — width comes from `is_64bit`, which on the bundle is the `+0x0E` secondary-mode byte (0 = 32-bit, the byte-proven path). The full mapping of `acc`/`reduce_cmd` to bundle bits beyond the 32-bit non-accumulate path is **INFERRED** (the BIR keys exist; the accumulate bundle bytes were not exhaustively enumerated).

### 2.3 RegisterMove

`RegisterMove` (op 74, opcode `0xA7`) is the scalar register copy — a degenerate ALU (no op selector):

```text
RegisterMove 64-byte bundle (r15)
  +0x0C  mode = 1 (32-bit) / 2 (64-bit)     constant (two encoder arms)
  +0x0D  dtype wire                          dtype_to_wire(...)
  +0x18  dst reg-lo                          getLocation.getRegId(0)
  +0x19  dst reg-hi (64-bit path only)       getRegId(1)
```

Guards: `"only support fp32/uint32/int32 for REG MOVE instr"`; the 64-bit path requires an immediate source (`"64bit REG MOVE instr must have ImmediateValue src"`).

Note that `RegisterMove`'s destination bytes sit at `+0x18`/`+0x19`, well past the mode/dtype pair at `+0x0C`/`+0x0D` — the bundle is not densely packed from the header, and the gap is real.

---

## 3. The Fused Compare-and-Branch

The SP control branch is **one silicon instruction**. `CompareAndBranch` and `UnconditionalBranch` *share* opcode `0xA9` and the `CTRL_BR` bundle class — the SP evaluates `lhs <pred> rhs` and jumps in a single issue. There is no compare-then-branch pair. The simulator proves the fusion byte-exact (the whole body is 99 bytes):

```text
birsim::InstVisitor::visitInstCompareAndBranch @ 0x1bffb0
    getArgument(0)               ; lhs
    sub_1BE610(inst)             ; rhs (RegisterAccess or ImmediateValue)
    mov  ecx, [rbx+0F0h]         ; comp_op = BranchCompareOp from InstCompareAndBranch+0xF0
    call compareScalarArgs(lhs, rhs, comp_op)    ; ← the compare, fused into the branch
    test al, al
    jz   .on_false
  .on_true:
    mov  rax, [rbx+0F8h]         ; on_true  BasicBlock* @ +0xF8
    mov  [rbp+0E10h], rax        ; write next-BB slot ( = take the branch )
    ret
  .on_false:
    mov  rax, [rbx+100h]         ; on_false BasicBlock* @ +0x100
    mov  [rbp+0E10h], rax        ; write next-BB slot ( = fall through )
    ret
```

This single fragment pins five claims at once: `comp_op@+0xF0`, `on_true@+0xF8`, `on_false@+0x100`, the one-call compare-and-branch fusion, and the **next-BB slot at `InstVisitor+0xE10`** as the PC-advance mechanism (§4). The dumper independently confirms the same struct layout — JSON keys `comp_op` (with `to_json(…, BranchCompareOp const&)`), `on_true`, `on_false` at exactly `+0xF0`/`+0xF8`/`+0x100`.

### 3.1 The Predicate — `BranchCompareOp`

`comp_op` is a `bir::BranchCompareOp` with **13 values**: an IMM family `0..5 {LT, LE, EQ, NE, GE, GT}`, a REG family `6..11 {LT, LE, EQ, NE, GE, GT}` (the RHS is a register rather than an immediate), and `12 = Unsupported` (a sentinel). The comparison itself runs at the **LHS dtype width** — `compareScalarArgs` (`0x1bce50`) is a 17-case switch that selects a per-dtype comparator table (`sq_cmp_ops_int<h/s/a/i/t/j/f>` — `u8`/`s16`/`s8`/`s32`/`u16`/`u32`/`f32`), so an `is_lt` on `int32` and on `uint8` are *different* comparisons; the switch and its per-width comparator tables are read directly.

The encoder maps `comp_op` to a wire byte through a 12-entry LUT:

```c
// comp_op wire LUT (bundle+0x0C)
// note the gap at wire 7/8 between the IMM and REG halves.
static const uint8_t branch_cmp_wire[12] = {
  /* IMM LT,LE,EQ,NE,GE,GT */  0x01, 0x02, 0x03, 0x04, 0x05, 0x06,
  /* REG LT,LE,EQ,NE,GE,GT */  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e
};  // op 12 (Unsupported) → FATAL
```

### 3.2 Canonicalization — Encode Only the Taken Edge

A `CTRL_BR` bundle encodes **exactly one** successor: the *non-fall-through* edge (the TAKEN target). The other edge **must** be the physical next basic block — the encoder asserts `on_false == getNextNode()`. When the IR has `on_true` as the fall-through instead, the encoder cannot just swap the BBs; it **negates the predicate** so the encoded edge is again the taken one:

```c
// predicate negation when on_true is the fall-through edge
// LT↔GE, LE↔GT, EQ↔NE, with the same mirror for the REG family.
static const uint8_t negate_cmp[12] = { 4,5,3,2,0,1,  10,11,9,8,6,7 };
//                                       LT GE EQ NE GE LT  (REG mirror)
```

`UnconditionalBranch` is the same op with `comp_op` cleared to `0` ("always"). The simulator's `UnconditionalBranch` is a **15-byte** function that proves this — it is `CompareAndBranch` with the compare elided:

```text
birsim::InstVisitor::visitInstUnconditionalBranch @ 0x1aa550
    mov  rax, [rsi+0F0h]         ; target BasicBlock* @ inst+0xF0
    mov  [rdi+0E10h], rax        ; write next-BB slot — unconditional take
    ret
```

> **GOTCHA — fall-through elision.** A branch (conditional or not) whose taken edge *is* the next BB emits **nothing** — zero bytes on the wire. The PC just advances linearly. This means a reimplementer reconstructing control flow from the wire cannot assume one bundle per CFG edge; the fall-through edge is implicit in instruction order.

### 3.3 The Branch Target — PC-Relative Resolution

The branch target on the wire (`bundle+0x30`, a qword) is `Generator::getBranchTargetId(I, targetBB)` — the **absolute SP-instruction PC of the target BB**, resolved at emit time from a per-(BB, engine) PC map. Because every engine has its own SP sequencer, the same logical branch resolves to a *different* PC per engine; the target id is keyed jointly by `(Instruction*, EngineType)`. `BranchHint` (op 80, `0xDD`) carries a target PC the same way but binds to a target *instruction* (not a BB) and is a pure no-op at runtime — a static-prediction annotation (`BranchOutcomeHint {0 LikelyTaken, 1 LikelyNotTaken}`).

---

## 4. The SP Sequencer Model

The functional reference realizes the SP sequencer with four runtime control-state structures, all hung off the `birsim::InstVisitor`. Three of the four offsets are re-derived byte-exact this pass:

| State | Offset | Type | Role | Confidence |
|---|---|---|---|---|
| **Next-BB slot** | `InstVisitor+0xE10` | `BasicBlock*` | the pending successor = the "PC target" | CERTAIN |
| **Affine env** | `InstVisitor+0xE18` | `DenseMap<AffineIdx,long>` | induction / loop-variable values | CERTAIN |
| **Call stack** | (two `std::deque`s) | activation frames | call / return frames | HIGH |
| **DMATrigger cursor** | `InstVisitor+0xE60` | `DenseMap` | per-queue round-robin fireCount | CERTAIN |

### 4.1 The Next-BB Slot *is* the Program Counter

The PC advance is the next-BB slot. **Every terminator writes it** — `CompareAndBranch` (§3, writes `on_true`/`on_false`), `UnconditionalBranch` (writes its target), `Return`/`Break` (write null). The CFG driver `visitBBHolderInControlFlow` (`0x20f820`, 5726 bytes) reads it after each block, follows the edge, and **nulls it** so the next block falls through if no terminator fired:

```text
birsim::InstVisitor::visitBBHolderInControlFlow @ 0x20f820
    0x20f8d4:  mov  rcx, [rbx+0E10h]          ; read pending successor (the PC)
    ...        (walk this BB's instructions in semaphore-ready order)
    0x20fa37:  mov  rax, [rbx+0E10h]          ; re-read the slot a terminator may have set
    0x20fa3e:  mov  qword [rbx+0E10h], 0      ; null it — consume the edge
    0x20fbf4:  lea  r14, [rbx+0E18h]          ; the affine-env DenseMap
    0x20fd9f:  mov  [rbx+0E10h], rdx          ; follow / set the next successor
```

The read-follow-null sequence *is* the fetch loop: `BB = next; if (!next) BB = linearFallThrough(BB)`. The "fetch" of the next SP instruction is the next-BB-slot follow; the PC is the target-BB inst-PC resolved by `getBranchTargetId` at emit.

### 4.2 The Call Stack — `Call` / `Return` / `Exit`

`Call` (`0xD3`) is a **per-engine broadcast** — the encoder loops `Module::listEnginesUsed` and emits one bundle *per engine*. The callee is referenced **by name** (`≤35`-byte `strncpy` at `bundle+0x0C`), not by PC — it is symbolic, resolved later. If `argc > 0`, an `args_table_var_id` goes at `bundle+0x30`. The simulator pushes the callsite and the caller onto **two `std::deque`s**, sets the callsite memloc context at `inst+0xF8`, recurses the callee region, then pops.

`Return` (`0xD2`) is a per-engine broadcast carrying only header + sync (no payload). It zeroes `inst+0x90` (an engine-reset). The simulator pops both deques, clears all semaphores, and nulls the next-BB slot:

```text
birsim::InstVisitor::visitInstReturn @ 0x1aa560
    call SyncState::clearAllSemaphores()
    mov  qword [rbx+0E10h], 0          ; null the PC — fall out of the region
```

`Exit` (`0xDF`, `PSEUDO_EXIT_EXECUTION`) is a single datapath bundle (asserts `isDataPathEngine`, datapath flag `= 1` at `bundle+0x0C`); the simulator throws `InstExitException` (single-core) or returns into an all-engine join (LNC). `NoOp` (`0xA4`, `CTRL_NO`) is a bare sync-only bundle — a pure sequencer sync slot carrying only the wait/update regions, used as a barrier/semaphore vehicle. `Break` is a one-byte `c3 retn` stub — lowered to `Uncond`/`CmpBranch` before codegen; the simulator's `Break` just nulls the next-BB slot (exit the enclosing region).

> **NOTE — structured loops are gone by codegen.** The simulator *does* have structured-loop kernels (ops 105/106/108) that recurse the visitor over a region with static `lb`/`ub`/`stride` (or a dynamic `ub = const + coef * RegState.read(reg)`, or a do-while that post-tests a register `!= 0`). But those model the **pre-lowering** form. On real silicon the loop has already been exploded (by `lower_branch`, §6) into a counter `RegisterAlu` op plus a back-edge `CompareAndBranch` — `LoopAxis`/`DynamicForLoopAxis` is consumed into the counter ALU ops and is **never a wire field**. The SP sequencer the codegen emits for sees only the flat scalar branch pair.

### 4.3 The DMATrigger Cursor

`DMATrigger` (op 67, opcode `0xC1`) is the SP op that **kicks a DMA queue**. `InstDMATrigger::getDefaultEngine` → `6` (SP), and `InstSyncType = 1` (sequencer): the SP *triggers* the queue then sequences past it — the descriptor transfers themselves sync on the DMA path, not the SP. The encoder writes a 31-byte queue/instance **name** at `bundle+0x0C` (`strncpy`, `edx = 0x1F`) and a dword at `bundle+0x30` = the trigger/fire descriptor (the count of queue entries to fire). The simulator fires the Nth `DMABlock` of the queue then advances a per-queue **round-robin fireCount cursor**, a `DenseMap` at `InstVisitor+0xE60`:

```text
birsim::InstVisitor::visitInstDMATrigger @ 0x2124a0
    0x2124a4:  lea  r13, [rdi+0E60h]          ; &fireCount DenseMap
    0x2124bf:  mov  rcx, [rdi+0E60h]          ; map storage base
    0x2124d1-e7: (shr 9 / shr 4 / xor / and)  ; DenseMap hash probe
    0x212502:  cmp  rbx, 0FFF…F000h           ; empty-key sentinel
    0x21250f:  cmp  rbx, 0FFF…E000h           ; tombstone sentinel
    0x212523:  add  eax, 1                     ; fireCount++  (advance the cursor)
    0x212526:  mov  [rdx], eax
```

### 4.4 The Queue Ops

`SwitchQueueInstance` (op 85, `0xCF`, `DMA_SWAP_QUEUE_SET`) selects a new queue instance: `queue@inst+0xF0`, `instance@inst+0xF8`, `no_rearm@inst+0x100`. The bundle carries the instance name (31 B, `strncpy`, `checkQueueNameLen ≤ 31`) at `+0x0C`, `no_rearm@+0x3B`, marker `1@+0x3C`. The encoder maintains a local `DenseMap<DMAQueue*, BasicBlock*>` at `this+0x1B0`. The simulator clears the queue's two semaphore groups and — unless `no_rearm` — rewinds **every owning DMATrigger's fireCount cursor to block 0**.

`ResetQueueInstance` (op 86, `0xC2`, `DMA_REARM`) re-arms a queue instance (name only at `+0x0C`). It is **hard-gated**: it asserts the module feature `neff_feature_SQI_no_rearm` (FATAL if absent) and then clears it. The simulator unconditionally rewinds the owning triggers' cursors — the cursor-only subset of `Switch`'s rearm, with no semaphore clear.

---

## 5. The Scalar Register File and its Coloring Allocator

### 5.1 The Register Struct

A `bir::Register` is a `StorageBase` subclass (class-tag `+0x110 == 2`) — a **named** scalar register created at function level via `Register::createFromJson` and referenced by `getRegisterByName`. It spans `numPhysicalRegs` (`Register+0x18C`, getter/setter `setNumPhysicalRegs`) physical slots: **1 for a 32-bit value, 2 for a 64-bit value** (the encoder asserts `Reg.getNumPhysicalRegs() == 2` on the 64-bit path). `getRegId(i)` bounds-checks `i < numPhysicalRegs` then returns `base(Reg+0x188) + i` (the physical id):

```c
// bir::Register::getRegId(i) — libBIR 0x17aaa0
uint8_t Register::getRegId(int i) const {
  assert(i < this->numPhysicalRegs);        // Register+0x18C
  return this->basePhysicalId + i;          // Register+0x188 + i
}
```

All wire register-id fields are **8-bit** (`< 256`). The register-file *size* is a per-engine HW constant, `HwmCore->MaxRegNumPerEngine`, asserted both at select and at coloring-allocation time:

```text
"addr < this->HwmCore->MaxRegNumPerEngine && \"bad Register ID during select\""
"info[i].address < ...MaxRegNumPerEngine && \"bad Register ID allocation\""
```

The literal value is **`MaxRegNumPerEngine = 62`** (`0x3e`), and it is the **same for all four arch models** (Inferentia, Sunda/Trainium, Cayman, CoreV4). The value is reachable through the arch-model tables. `bir::Register::getRegId` (libBIR `0x3c3800`) bounds `RegId` against `getArchModel(arch).device.core.MaxRegNumPerEngine` — in the disassembly, `mov rax,[Board+0x8]` (the `Device`), `mov rax,[Device+0x10]` (the `Core`), `cmp [Core+0x3c],RegId` (`0x3c38f5..0x3c38ff`), so the field lives at `Core+0x3c`. `Core::Core(CoreParamSet const&)` (libBIR `0x478cf0`) fills it from the parameter set — `mov ecx,[ParamSet+0x74]; mov [Core+0x3c],ecx` (`0x478d95`). Each per-arch `Core` constructor in `neuronxcc/hwm/ctm.cpython-310-*.so` writes that parameter as an immediate `0x3e`: `mov DWORD PTR [rsp+0x74],0x3e` in `InferentiaCore` (`0x393a0`), `SundaCore` (`0x39790`), `CaymanCore` (`0x39b80`), and `CoreV4Core` (`0x39f80`). So the SP register file is 62 physical slots per engine, spillable, uniform across generations.

### 5.2 The Graph-Coloring Allocator with Spilling

Register allocation is a **graph-coloring allocator with spilling**, confirmed at the symbol level in three places: the dedicated `REG_Allocator` (`neuronxcc::backend::REG_Allocator::simplify` @ `0x5edb60`, `::live_range` @ `0x5edd00` — the classic simplify/select coloring phases over a `vector<Info>` and `ArrayRef<bir::Register*>` interference graph), the templated `ColoringAllocatorWithLoop` (with `SB_Allocator`/`PSUM_Allocator` siblings sharing the `simplify`/`find_first_defs` machinery), and a **standalone `coloring_allocator_with_loop` tool binary**, which carries its own `bir::Instruction::getInstSyncType` body. The spill path surfaces the strings:

```text
"info[i].allocated && register not allocated"
"cannot allocate registers since spilling candidates are not enough"
"Number of registers spilled = "
```

So the allocation flow is the textbook one: build the interference graph over scalar `bir::Register` live ranges (`live_range`), `simplify` (push low-degree nodes, spill candidates when stuck), `select` (pop and color, asserting `address < MaxRegNumPerEngine`), spill to memory if a color cannot be found. Each colored `Register` gets a physical id `< MaxRegNumPerEngine`, which the SP encoders write as the 8-bit `reg-lo`/`reg-hi` bundle fields (§2).

---

## 6. Sequencer Lifecycle — from BIR to SP Wire

The SP sequencer is the *terminal* consumer of a long middle-end pipeline. The flatness it sees (a counter `RegisterAlu` + a back-edge `CmpBranch`, never a structured loop) is manufactured upstream:

```text
build_fdeps / order  →  dep_reduction  →  SYNCHRONIZER (allocates 256-bank sema ids,
   attaches sem-inc Updates + sem-ge-imm/-ge-reg Waits into each Inst's SyncInfo)
 → alloc_semaphores (bank-bind)
 → lower_sync       (materializes EventSemaphore(13) + GroupReset(14))
 → lower_branch     (Loop/DoWhile → SP counter RegisterAlu ops + flat CmpBranch/Uncond
                     + per-engine barriers; re-homes branches Unassigned/ALL → SP)
 → lower_control    (brackets control-transfer BBs with
                     [AllEngBar · GroupReset · AllEngBar · Drain (· CoreBarrier)] on ALL)
 → ExpandAllEngineFinalPreCodegen (clones each ALL fence per datapath engine —
                     because the barrier/reset encoders assert isDataPathEngine)
 → register-allocation (REG_Allocator coloring + spill; physical ids < MaxRegNumPerEngine)
 → CoreV2GenImpl (this L3 layer): setupHeader + field-walk + fwrite 0x40 per inst.
```

The simulator (`birsim`) replays the result: the **next-BB slot** (PC) follow + the **call-stack deques** + the **affine env** + the **DMATrigger fireCount cursor** + the **256-sema bank**. The wire mode/op LUTs are the bridge between the two sides: the synchronizer's `sem-inc`/`sem-ge` ↔ wire bytes `0x13`/`0x05`/`0x85`; `bir::AluOpType` ↔ the §2.1 jump table; `bir::BranchCompareOp` ↔ the §3.1 LUT `{1..6, 9..14}`.

> **NOTE — the all-engine fence fan-out.** `lower_control` mints `AllEngineBarrier` + `GroupReset` on engine `ALL` (7), but both encoders assert `isDataPathEngine`, so they can never emit on `ALL`. `ExpandAllEngineFinalPreCodegenImpl` resolves this by `fullClone`-ing each `ALL` fence into **one bundle per datapath engine** (stamping `clone+144 = engine`) *before* codegen. One logical all-engine fence therefore becomes N per-engine 64-byte bundles. `CoreBarrier` (op 87, `0xD8`) is gen3-only (`CoreV3`, no `CoreV2` encoder).

---

## 7. Confidence and Gaps

**Read directly from `libBIRSimulator.so` / `libBIRParserDumper.so` bodies:** `comp_op@+0xF0`, `on_true@+0xF8`, `on_false@+0x100` and the **fused** compare-and-branch (`visitInstCompareAndBranch` @ `0x1bffb0` — one `compareScalarArgs` call); the next-BB-slot PC mechanism at `InstVisitor+0xE10` (written by `CompareAndBranch`, `UnconditionalBranch` @ `0x1aa550`, `Return` @ `0x1aa560`; read-and-nulled by `visitBBHolderInControlFlow` @ `0x20f820`); the affine-env DenseMap at `+0xE18`; the DMATrigger fireCount round-robin cursor at `+0xE60` (`visitInstDMATrigger` @ `0x2124a0`); `RegisterAlu` op field at `InstRegisterAlu+0xF0` (sim `0x1bee7a` + dumper `0xa2752`, JSON key `op`, `to_json(…,AluOpType const&)`); the per-dtype comparator dispatch in `compareScalarArgs`; the register-file symbols `Register::getRegId`/`setNumPhysicalRegs` and the allocator symbols `REG_Allocator::simplify`/`live_range` + the standalone `coloring_allocator_with_loop` binary.

**One step less direct** (pinned from the `libwalrus.so` encoder byte-reads, whose bodies are not present as non-thunks in this disassembly slice): the 30-arm `AluOpType` wire jump table and its byte map; the `RegisterAlu`/`RegisterMove` bundle offsets (`+0x0C`/`+0x0D`/`+0x0E`/`+0x10..+0x15`, `+0x18`/`+0x19`); the `BranchCompareOp` wire LUT `{1..6, 9..14}` and predicate-negation LUT `{4,5,3,2,0,1,10,11,9,8,6,7}`; `DMATrigger` opcode `0xC1` + `getDefaultEngine = 6`; `EngineType SP = 6` and `InstSyncType = 1` for the whole family; the semaphore wire-mode LUTs.

**Gaps:**

- **G1 — `MaxRegNumPerEngine` = 62 (pinned).** The bound is `Core+0x3c`, read by `getRegId` (`0x3c38ff`) and set by every per-arch `Core` ctor to the immediate `0x3e` = 62 (`hwm/ctm.so`: `InferentiaCore`/`SundaCore`/`CaymanCore`/`CoreV4Core`, `mov DWORD [rsp+0x74],0x3e`). Uniform across all four arch models; §5.1 carries the full chain. The register file is 62 physical slots per engine, spillable.
- **G2 — `RegisterAlu` accumulate/reduce bits.** The BIR keys `is_64bit`/`dge_type`/`reduce_cmd`/`acc` exist and the bundle has the `+0x0D`/`+0x0E` mode bytes, but the full mapping of `acc`/`reduce_cmd` to bundle bytes is not exhaustively enumerated — only the 32-bit non-accumulate path is pinned. **INFERRED.**
- **G3 — the 2-vs-3-operand form.** The encoder writes up to three `getArgument` reg-ids (`+0x11`/`+0x12`/`+0x13`); whether every op consumes 3 operands or some are 2-operand (`src2 = imm`) is not fully enumerated.
- **G4 — runtime semaphore decode.** The runtime-side (libnrt) decode of the wait/inc/dec/set wire bytes is a separate strand and is not landed in this corpus; the compile-side encoding here is authoritative.

---

## Cross-References

- [ISA 2.19 — SP register-lane / TensorLoad-Save encoding](../isa/sp-register-encoding.md) — the bit-level field encoding of the scalar register-access lane.
- [ISA 2.20 — SP sync / branch / control encoding](../isa/sp-sync-encoding.md) — the bit-level `CTRL_BR`/`CTRL_NO` bundle and the semaphore wait/update block.
- [BIR Part 7 — the simulator sequencer](../bir/sim-control-sync-customop.md) — the `birsim::InstVisitor` next-BB / call-stack / affine-env model in full.
- [walrus Part 8 — register allocation](../walrus/alt-allocators-dma-opt.md) — the `REG_Allocator` / `ColoringAllocatorWithLoop` graph-coloring + spill path.
- [PE Engine — the Systolic Matmul Array](pe-engine.md) (1.08) — a datapath engine whose own SP-class sequencer the control ops home onto.
- [Pool Engine — Windowed Pooling and the Reduce Leg](pool-engine.md) (1.09) — another datapath engine carrying an SP sequencer + register file.
