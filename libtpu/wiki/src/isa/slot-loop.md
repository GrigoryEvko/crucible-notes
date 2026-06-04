# Hardware Loop-Counter

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel. Other versions differ.*

## Abstract

A TPU loop is counted in one of two places, and the choice is per-sequencer, not per-program. On **BarnaCore** (and the Jellyfish / Pufferfish AddressHandler) the loop is a **true hardware loop**: a dedicated silicon register holds the trip count, the hardware decrements and tests it, and the bundle stream contains explicit loop-setup / loop-start / loop-end ops but **no software induction variable, no compare, no branch**. The AddressHandler hardware loop is live only on v3/v4 — the `ViperfishTarget` override is a dead stub (below) and v5p/v6e have no override at all. On the **TensorCore** and **SparseCore** the loop is a **software back-edge** built from the scalar-ALU op set — `ADDri` IV update, a `CMPxx`, and a `BRcond` — with the hardware **Loop Counter (LCC)** register *mirroring* the iteration count so the body can read it (for iteration-dependent addressing) without keeping its own counter. The LCC drives nothing; it is a readable snapshot.

The loop-counter register is named **LCC** throughout the binary, a 64-bit value read as two 32-bit halves and unified with the global time counter (GTC) under one enum (`CycleCounterType { kLCC, kGTC }`). It is **not** an allocatable register — there is no LCC class in the register file alongside S/V/M/P0..P31; like GTC it is a special control register read into a scalar destination. The number of *addressable* LCC registers is the per-generation count this page pins: Jellyfish exposes none on its TensorCore (loops live only in the AddressHandler `Loop` slot); Pufferfish exposes **two** (LCC0, LCC1) through an indexed read-register enum; Viperfish, Ghostlite, and Trillium expose **one** implicit counter through a pair of dedicated dest-only opcodes.

This page documents the loop counter as a reimplementation target: the per-generation count and read mechanism, the encoding of the two loop mechanisms (hardware-counted vs software-counted-with-readback), the AddressHandler single-active-loop builder with its even-offset / minimum-length constraints, the two LLVM `PipelinerLoopInfo` subclasses that prove the hardware-vs-software split, and how the sequencer drives the back-edge. The op inventory and the lane it occupies are on the [Sequencer Slot](slot-sequencer.md) page; this page owns the counter itself.

For reimplementation, the contract is:

- **LCC is a 64-bit special control register**, read as low+high 32-bit halves into a 5-bit-selected scalar destination; max representable trip count `2^64 − 1`. It is not in the allocatable register file.
- **The count per generation**: JF TC = 0 (AddressHandler `Loop` slot only), PF = 2 (LCC0/LCC1 via the `Tcs`/`Bcs` read-register enum), V5+ = 1 implicit (via `ReadRegisterLcc{Low,High}`, a dest-only opcode with no index operand).
- **Two mechanisms**: hardware-counted (BarnaCore / AddressHandler — `getIVUpdate`/`getCmp` are NULL, `getTripCount` is `−1`) vs software-counted (TC / SparseCore — real IV/cmp/trip-count MIs), selected by `analyzeLoopForTPUPipelining` on a subtarget feature bit plus the terminator opcode.
- **The AddressHandler hardware loop is single-active** (one `loop_start_` field, not a stack), body length `≥ 2` instructions, with a mandatory non-loop preheader instruction.
- **Nesting is constrained**: one active hardware loop per sequencer; PF's two LCC registers permit reading a depth-2 nest; outer loops are software back-edges.

| | |
|---|---|
| **Counter register** | LCC ("Loop Counter") — 64-bit, read as lo+hi 32-bit halves |
| **Counter enum** | `LloInstruction::CycleCounterType { kLCC, kGTC }` (shares the read datapath with GTC) |
| **PF read mechanism** | indexed `TcsReadRegister` / `BcsReadRegister` enum (`LCC0`, `LCC1`, …) |
| **V5+ read mechanism** | `ReadRegisterLccLow` / `ReadRegisterLccHigh` — dest-only opcodes, no index |
| **V5+ dest encoder** | `TensorCoreScalarAlu0Compact_ReadRegisterLccLow::set_dest` @ `0x1f62ff60` → `BitCopy(buf,467,&dest,0,5)` |
| **HW-vs-SW selector** | `TPUInstrInfo::analyzeLoopForTPUPipelining` @ `0x13b804c0` (feature bit `[subtarget+0x158]&1` + terminator opcode) |
| **HW loop-info class** | `(anon)::TPUBarnaCorePipelinerLoopInfo` — `getIVUpdate`=NULL @ `0x13b86560`, `getTripCount`=−1 @ `0x13b865a0` |
| **SW loop-info class** | `(anon)::TPUSparseCorePipelinerLoopInfo` — `getTripCount`=`[this+0x28]` @ `0x13b86260` |
| **AddressHandler builder** | `AddressHandlerProgramBuilder::BeginLoop` @ `0xfa90d40` / `EndLoop` @ `0xfa91300` |
| **AddressHandler per-gen insert** | `JellyfishTarget::InsertAddressHandlerLoop` @ `0x1d490e00` (live) / `PufferfishTarget` @ `0x1d495340` (live) / `ViperfishTarget` @ `0x1d49b980` (`__noreturn` stub) |
| **BarnaCore HW opcodes** | `bcLOOP_SETUP` (0x194), `bcLOOP_START` (0xf8a), `bcLOOP_END` (0x193) |
| **LLO loop kinds** | `LloLoopKindProto { LOOP_KIND_NONE, LOOP_KIND_WHILE, LOOP_KIND_DOWHILE }` |

---

## The LCC Register and Its Width

The hardware loop counter is named **LCC** ("Loop Counter") everywhere in the binary and is a **64-bit** value. It is never read whole: the program reads it as two 32-bit halves (`rdreg.lcc.lo` + `rdreg.lcc.hi` on the TensorCore; `ReadRegisterLccLow` + `ReadRegisterLccHigh` on V5+), exactly mirroring the global time counter (GTC), which is read the same lo+hi way. The LLO IR unifies the two under a single enum — `LloInstruction::CycleCounterType { kLCC, kGTC }` — and they share the read-register datapath. LCC is the per-loop iteration counter; GTC is the global time counter.

The crucial reimplementation fact: **LCC is not an allocatable register.** The TPU register file has S / V / M / P0..P31 / XRF / DRF / ERF / SFRF / V2SF / CB / VAGG classes and no LCC class. LCC, like GTC, is a special control register that the sequencer reads into a scalar destination `S0..S31`. Each half lands in one 32-bit scalar register; the destination is a 5-bit-selected scalar index, confirmed byte-exact from the V5+ encoder:

```c
// gfc::isa::TensorCoreScalarAlu0Compact_ReadRegisterLccLow::set_dest(unsigned)  @ 0x1f62ff60
int dest = a2;                               // the scalar destination index
return BitCopy(buf, /*dst_bit=*/467, &dest, /*src_bit=*/0, /*width=*/5);   // 5-bit dest, abs bit 467
```

`BitCopy(buf, 467, &dest, 0, 5)` writes the 5-bit destination at absolute bundle bit 467 (`0x1d3`) — and it is the **only** field this op writes. All bit positions on this page are **LSB-first** (bit 0 = least-significant bit of byte 0), matching the universal `BitCopy(dst, dst_bit, src, src_bit, nbits)` packer (`0x1fa0a900`) and the convention pinned in [Bundle Model §bit-numbering](bundle-model-overview.md). This is the same 5-bit dest at bit 467 that the [Sequencer Slot](slot-sequencer.md#branch--jump--call-encoding) call/return-address encoder writes. There is no loop-counter-index operand, which is the structural proof that V5+ exposes a single implicit counter (see [Per-Generation Count](#per-generation-count-and-read-mechanism)). The maximum trip count the counter can represent is `2^64 − 1`; the 64-bit readback (lo+hi) is fixed, while whether the silicon down-counter is the full 64 bits or narrower is not separable from the binary.

> **NOTE —** the LLVM-generic `hardware-loop-counter-bitwidth` `cl::opt` ("Set the loop counter bitwidth") belongs to the target-independent `HardwareLoops` pass, not to the silicon LCC. The TPU LCC is fixed at 64-bit per the lo/hi read structure; do not conflate the two.

---

## Per-Generation Count and Read Mechanism

The "count" is the number of *addressable* loop-counter registers a program can read. Two distinct register-naming conventions exist in the binary, and they reveal the count directly.

**Pufferfish (v4) — TWO loop counters, explicitly indexed.** The Pufferfish read-register set is an enum, `TcsReadRegister` (TensorCore Sequencer) and `BcsReadRegister` (BarnaCore Sequencer), whose values name two distinct loop counters and two distinct time counters:

```text
 TcsReadRegister:                  BcsReadRegister:
   TCS_READ_REGISTER_LCC0            BCS_READ_REGISTER_LCC0
   TCS_READ_REGISTER_LCC1            BCS_READ_REGISTER_LCC1
   TCS_READ_REGISTER_GTC0            BCS_READ_REGISTER_GTC0
   TCS_READ_REGISTER_GTC1            BCS_READ_REGISTER_GTC1
   TCS_READ_REGISTER_TAG_REGISTER    BCS_READ_REGISTER_TAG_REGISTER
   TCS_READ_REGISTER_TRACEMARK_REG   BCS_READ_REGISTER_TRACEMARK_REG / FSR / HDR
```

The active counter is selected by the `reg` enum field of the scalar read-register op, not by a dedicated opcode — which is why no `pxc::isa::*ReadRegisterLcc*` function exists in the binary (the `nm` set has none). A reader of LCC0 vs LCC1 is choosing an enum value, not a different instruction.

**Viperfish (v5p) / Ghostlite (v6e) / 6acc60406 (TPU7x) — ONE loop counter, implicit.** V5+ replaced the indexed read with two dedicated opcodes per engine: `ReadRegisterLccLow` reads `LCC[31:0]`, `ReadRegisterLccHigh` reads `LCC[63:32]`, each into a scalar dest. The encoders exist for `vxc`, `gxc::glc`, and `gxc::gfc` on both `ScalarAlu0` and `ScalarAlu1` and on the SparseCore. As shown above, the op's only operand is the 5-bit destination — there is no index field, so the program can address exactly one (implicit) loop counter per sequencer.

**Jellyfish (v2) / Dragonfish (v3) — NO LCC read on the TensorCore.** No `jellyfish::isa::*ReadRegisterLcc*` symbol exists and no Jellyfish read-register enum names LCC; the JF TensorCore has only the cycle-counter reads. Jellyfish exposes a hardware loop **only** through the AddressHandler `Loop` slot (below), never via an LCC read on the TensorCore.

| Gen | TC LCC regs | SC/BCS LCC regs | Read mechanism |
|---|---:|---|---|
| Jellyfish (v2) | 0 | n/a (BCAH `Loop` slot) | cycle-counter only on TC |
| Dragonfish (v3) | 0 (alias JF) | n/a | inherits Jellyfish |
| Pufferfish (v4) | 2 (LCC0/LCC1) | 2 (BCS LCC0/LCC1) | `Tcs`/`Bcs` read-register enum |
| Viperfish (v5p) | 1 (implicit) | 1 (implicit) | `ReadRegisterLcc{Low,High}` (dest-only) |
| Ghostlite (v6e) | 1 (implicit) | 1 (implicit) | `ReadRegisterLcc{Low,High}` (dest-only) |
| 6acc60406 (TPU7x) | 1 (implicit) | 1 (implicit) | `ReadRegisterLcc{Low,High}` (dest-only) |

> **GOTCHA —** Pufferfish genuinely lacks the V5+ `ReadRegisterLcc{Low,High}` opcode form, but it still has **two** LCC registers: it reads them through the indexed `TcsReadRegister` / `BcsReadRegister` enum instead. A reimplementation that drives off the V5+ opcode shape will miss the PF loop counters entirely.

---

## The Two Loop Mechanisms

Which mechanism a loop uses is decided by the sequencer, and the split is proven by the two — and only two — LLVM `PipelinerLoopInfo` subclasses. There is **no** `TensorCorePipelinerLoopInfo`; the TensorCore software loop uses the SparseCore (software) info.

```text
 llvm::TPUPipelinerLoopInfo (base)
   ├─ (anon)::TPUBarnaCorePipelinerLoopInfo   — hardware-counted (counter is in silicon)
   └─ (anon)::TPUSparseCorePipelinerLoopInfo  — software-counted (IV in the scalar register file)
```

The BarnaCore subclass returns nulls for every software-loop component — the clearest possible evidence that the hardware does the counting:

```c
// (anon)::TPUBarnaCorePipelinerLoopInfo  — decoded byte-exactly
getIVUpdate()    @ 0x13b86560  ->  return 0;     // NULL — no software induction variable
getCmp()         @ 0x13b86580  ->  return 0;     // NULL — no software compare
getTripCount()   @ 0x13b865a0  ->  return -1;    // -1 = "no software trip count"
adjustTripCount  @ 0x13b86500  ->  ret;          // no-op
```

The SparseCore subclass returns real machine instructions for each:

```c
// (anon)::TPUSparseCorePipelinerLoopInfo  — decoded byte-exactly
getIVUpdate()    @ 0x13b86220  ->  return *((void**)this + 3);   // [this+0x18] = ADDri MI
getCmp()         @ 0x13b86240  ->  return *((void**)this + 2);   // [this+0x10] = CMP/BRcond MI
getTripCount()   @ 0x13b86260  ->  return *((void**)this + 5);   // [this+0x28] = trip-count MI
```

The selection happens in `analyzeLoopForTPUPipelining` (`0x13b804c0`). It first tests a subtarget feature bit (`[*subtarget + 0x158] & 1` — byte offset `0x158` = 344; the BarnaCore hardware-loop mode); then it walks the loop's terminators looking for one of **three** loop-terminator opcodes (`325`, `403`, `328`). On a match it reads a per-loop HW-mode marker byte at `[loop_header + 0x122]` (= 290); when that byte is `1` it allocates the BarnaCore info via the `operator new(8u)` / `vtable = off_2192D4E8` arm (mid-function, reached only after the terminator match); otherwise it detects an IV and compare and builds the 48-byte SparseCore info (`operator new(0x30u)`):

```c
// llvm::TPUInstrInfo::analyzeLoopForTPUPipelining  @ 0x13b804c0  (decoded byte-exactly)
subtarget = loop_header->parent_subtarget;             // [a3+4]→[+32]
if ((subtarget->vtable_field[0x158] & 1) == 0) return 0;   // not pipelineable on this target
for (term in loop terminators) {                       // walk the terminator chain
    if (term.opcode == 325 || term.opcode == 403 || term.opcode == 328) {
        if (loop_header.flags[+0x122] == 1)             // BarnaCore HW-mode marker byte == 1
            return new TPUBarnaCorePipelinerLoopInfo;   // operator new(8); no IV/cmp/trip
        // else: find predicate operand, detect IV update + compare (analyzeIVUpdateforPipelining)
        ...
        return new TPUSparseCorePipelinerLoopInfo(iv, cmp, ...);  // operator new(0x30)
    }
}
```

> **NOTE —** the three terminator opcodes (`325`, `403`, `328`) and the predicate-operand opcode `540` are the raw MC opcode integers `analyzeLoopForTPUPipelining` matches; their symbolic names were not resolved (the integers are byte-exact from the decompile — the names are not). The function additionally special-cases opcode `540` when extracting the predicate operand index.

So a BarnaCore loop body is: `bcLOOP_SETUP` (load the count) → `bcLOOP_START` (mark the body head; one value operand = the bound) → body → `bcLOOP_END` (the hardware decrement+test+branch-back terminator). A TC/SC loop body is: preheader (`MOV`/`ADDri` to init the IV) → body → `ADDri` (IV += stride) → `CMPxx` → `BRcond` back to the head, with the LCC register mirroring the count for in-body reads.

> **QUIRK —** the BarnaCore loop index is itself a readable value, distinct from the LCC snapshot. The intrinsics `bc.extractvalue.loopindex` (read the current index) and `bc.insertvalue.loopindex` (seed it) expose the live counter that `bcLOOP_END` decrements — a different datapath from the `ReadRegisterLcc` mirror used on the software-counted engines. A reimplementer must not assume "read the loop count" maps to the same op on both mechanisms.

---

## How the Sequencer Drives the Loop

The loop-control op always occupies the scalar **sequencer slot** — lane 0 of the scalar-ALU sub-bundle, the only lane that can mutate the program counter (see [Sequencer Slot](slot-sequencer.md)). The two mechanisms place different ops there.

**Software loop (TC / SparseCore).** The back-edge is a `BRcond` (the conditional branch); the IV update (`ADDri`) and the compare (`CMPxx`) are scalar-ALU ops in the same lane family. The branch target is a signed 20-bit field in [immediate slot 0](slot-immediate.md), not inside the sequencer slot bytes; the absolute-vs-relative distinction is purely an opcode discriminator over that one field. The `CMPxx` family spans signed and unsigned comparisons in register-immediate (`CMPxxri`) and register-register (`CMPxxrr`) forms — the immediate form encodes a compile-time-constant trip bound, the register form a dynamic / SPU-computed bound. Because the back-edge is a branch, the loop body's last bundle is a branch-terminator bundle, which interacts with the branch-delay-slot packing.

**Hardware loop (BarnaCore).** `bcLOOP_SETUP` (opcode 0x194) loads the trip count into the loop register in the preamble; `bcLOOP_START` (0xf8a) marks the body head and carries one value operand (the bound — a register-materialized value that may originate from an immediate via SETUP); `bcLOOP_END` (0x193) is the back-edge terminator the hardware uses to decrement, test, and branch back. A BarnaCore channel-scalar slot carries a `loop` bit and a `branch` bit, and setting both is an encoding error — the diagnostic *"Invalid Barnacore Channel Instruction with both Loop and Branch bits set"* enforces that a slot is either a loop control or a branch, never both.

The trip count enters the LLO loop as a canonical index space: `LloLoopProto.LoopIndexSpaceProto = { start, limit, step }`. The `limit` may be a constant or a dynamic value; the binary tracks `num_dynamic_loop_bounds_` (each dynamic bound occupying two slots, `offset < num_dynamic_loop_bounds_ * 2`), and a loop with `num_dynamic_loop_bounds_ == 0` is fully static. The XLA-side `WhileLoopBackendConfig { KnownTripCount, KnownInitStep, KnownInductionVariable }` propagates the trip count down into this index space. The LLO pass `(anon)::UpdateLoopCounter(LloRegion*)` walks the region, finds the loop, and materializes the trip counter into the loop's carried tuple so the hardware LCC or the software IV can be seeded; it gives up with `UnknownLoopCountComplexCFG` when the CFG is too tangled to identify a single trip counter.

Zero-trip is handled at the loop-kind level, not by the counter: `LloLoopKindProto = { LOOP_KIND_NONE, LOOP_KIND_WHILE, LOOP_KIND_DOWHILE }`. `WHILE` tests before the body and can execute zero times; `DOWHILE` tests after and runs at least once. The MLIR property `verify_non_zero_trip` asserts a loop runs `≥ 1` iteration so it can lower as `DOWHILE` / a hardware-counted loop without a guard. Small constant-trip loops bypass the counter entirely — *"Loops with a constant trip count smaller than this value will not use the count register"* — and are unrolled or peeled instead.

---

## The AddressHandler Hardware Loop (Jellyfish / Pufferfish)

Jellyfish has no TensorCore LCC, so its hardware loop lives entirely in the BarnaCore AddressHandler sequencer, built by `AddressHandlerProgramBuilder::BeginLoop` (`0xfa90d40`) / `EndLoop` (`0xfa91300`). The builder tracks a **single** loop-region field, `loop_start_` (at `this+0x18`, the 7th int), with a `kNoLoopActive = −1` sentinel — **not a stack** — which is the structural proof that AddressHandler loops cannot software-nest. The CHECKs are byte-exact:

```c
// AddressHandlerProgramBuilder::BeginLoop  @ 0xfa90d40  (decoded)
CHECK(loop_start_ == kNoLoopActive);          // -1; fails if a loop is already active  (line 903)
loop_start_ = instructions_.size();           // record the body head
CHECK(loop_start_ >= 1);                       // "Code must start with one non-loop instruction"  (line 905)

// AddressHandlerProgramBuilder::EndLoop  @ 0xfa91300  (decoded)
CHECK(loop_start_ != kNoLoopActive);          // (line 910)
CHECK(loop_start_ >= 1);                       // "Code must start with one non-loop instruction"  (line 911)
CHECK(loop_start_ < instructions_.size());    // (line 912)
loop_length = instructions_.size() - loop_start_;
CHECK(loop_length >= 2);                       // "Jellyfish spec requires that loop must have at least two instructions"  (line 915)
insn = instructions_[loop_start_ - 1];        // the preheader instruction
CHECK(!insn.scalar.loop_start);               // (line 917)
insn.scalar.loop_start = 1;
insn.scalar.loop_count = loop_length - 1;      // body length minus the preheader slot
loop_start_ = kNoLoopActive;                   // reset; loop is closed
```

So an AddressHandler loop requires a mandatory non-loop preheader instruction (`loop_start_ >= 1`) and a body of at least two instructions (`loop_length >= 2`). `EndLoop` stamps the loop-start flag and the loop-count into the preheader instruction record, then resets `loop_start_`. The identical Pufferfish check string — *"Pufferfish spec requires that loop must have at least two instructions"* — confirms the same minimum-length rule carries to v4.

The loop body is **not** an offset field — the per-generation `Target::InsertAddressHandlerLoop` overrides write a **count** into a dedicated `BarnaCoreAddressHandlerScalarSlot_Loop` proto sub-message. `JellyfishTarget::InsertAddressHandlerLoop` (`0x1d490e00`) re-checks `program_in_loop.bundles_size() >= 2` (the same *"Jellyfish spec requires that loop must have at least two instructions"* string, `target_jellyfish.h:90`), then default-constructs the `Loop` sub-message and stores `loop_count = bundles − 1` (`*(loop + 24) = v30 − 1`) — the body-bundle count minus the preheader, identical to the `loop_count = loop_length − 1` that `EndLoop` stamps. `PufferfishTarget::InsertAddressHandlerLoop` (`0x1d495340`) is the same shape with the *"Pufferfish spec requires that loop must have at least two instructions"* string and the same `bundles − 1` count write.

> **GOTCHA —** the diagnostics *"loop end is out of range or not a positive multiple of 2"* / *"loop start is out of range or not a negative multiple of 2"* belong to LLVM's bundled **ARM** backend (`(anon)::ARMAsmParser::matchAndEmitInstruction` @ `0x15185a20`, the Armv8.1-M low-overhead-loop `WLS`/`LE` validation), **not** to any TPU `InsertAddressHandlerLoop` path. The TPU AddressHandler loop carries an iteration **count** (`bundles − 1`), not a signed even byte-offset; there is no even-multiple constraint in the TPU encode path.

The AddressHandler loop persists across **v3/v4 only** (`JellyfishTarget` / `PufferfishTarget` overrides, both live with real proto construction). The `ViperfishTarget::InsertAddressHandlerLoop` override (`0x1d49b980`) exists but is a `__noreturn` stub that fatals *"Deepsea version not supported"* (`target_viperfish.h:320`) — so the AddressHandler-style hardware loop is **dropped at Viperfish (v5)**; there is no Ghostlite or 6acc60406 override at all.

| Element | BarnaCore / AddressHandler (HW loop) | TC / SparseCore (SW loop) |
|---|---|---|
| Loop begin | `bcLOOP_SETUP` (load count) + `bcLOOP_START` (1 bound operand); BCAH `BeginLoop` sets `loop_start_` | preheader: init scalar IV (`MOV` / `ADDri`) |
| Body length | `loop_count = bundles − 1` in the `ScalarSlot_Loop` proto; body `≥ 2` instr | implicit (basic-block span) |
| Loop end | `bcLOOP_END` — HW decrement+test+branch-back | `ADDri` + `CMPxx` + `BRcond` back-edge |
| Counter | dedicated HW loop register (1; LCC0/LCC1 on PF) | scalar-reg IV; HW LCC mirrors count (readable) |
| Trip source | bound operand (reg / immediate via SETUP) | `CMPxxri` (imm) or `CMPxxrr` (reg, dynamic) |
| Live index read | `bc.extractvalue.loopindex` | `ReadRegisterLcc{Low,High}` / indexed `Tcs`/`Bcs` enum |
| Nesting | single active loop (`loop_start_ != kNoLoopActive` CHECK) | software IVs nest freely; only innermost HW-counted |

---

## Nesting Model

Hardware-loop nesting is intentionally bounded:

- **AddressHandler / BarnaCore**: one active hardware loop at a time, enforced by the single `loop_start_` field and the `BeginLoop` CHECK above. There is no loop-counter stack at this level — no nested AddressHandler / BarnaCore hardware loops.
- The LLVM-generic option strings *"force-nested-hardware-loop"* / *"nested hardware-loops not supported"* confirm the default TPU `HardwareLoops` path does not support nested hardware loops; the common case is one hardware-counted innermost loop with outer loops handled as software back-edges.
- **Pufferfish's two LCC registers (LCC0, LCC1)** allow reading two distinct loop counters — supporting at most a depth-2 nest where inner and outer each have a distinct counter, selected by the read-register enum value. Whether LCC0/LCC1 correspond to (outer, inner) nest levels or (TC-issued, BC-issued) loops is not traced here.
- **V5+ has a single implicit LCC**, so only the innermost hardware-counted loop's count is directly readable; outer loops use software IVs.
- The LLO loop region (`LloRegionMember::kLoop`) can nest in the IR — a `kLoop` member's sub-region may contain another `kLoop` — but only the innermost gets the hardware counter; the compiler flattens, unrolls, or software-counts the rest.

---

## What Is Not Yet Pinned

- **The bcLOOP field bit positions.** The trip-count immediate field of `bcLOOP_SETUP` and the body-offset fields of `bcLOOP_START` / `bcLOOP_END` within the BarnaCore bundle are routed by the LLVM MC encoder (`TPUMCCodeEmitter`); the per-opcode `InstBits` records are not byte-decoded here. The ops and their roles are pinned; the exact field widths are not.
- **The silicon counter width.** The 64-bit readback (lo+hi) is fixed; whether the down-counter is the full 64 bits or narrower is not separable from the binary.
- **PF LCC0 vs LCC1 assignment policy.** Two counters exist; which compiler pass picks LCC0 vs LCC1, and whether the choice tracks nest level or issuing engine, is not traced here.
- **The three loop-terminator MC opcodes (`325`, `403`, `328`) and the predicate opcode `540`.** The integers `analyzeLoopForTPUPipelining` matches are byte-exact; their symbolic LLVM-MC names (which of `bcLOOP_END` / branch terminators they correspond to) are not individually resolved here.

---

## Cross-References

- [Sequencer Slot](slot-sequencer.md) — the lane-0 scalar slot the loop-control ops (`BRcond`, `bcLOOP_*`, `ReadRegisterLcc`) occupy, and the JF software-bundle-index loop vs the V5+ LCC read.
- [Immediate Slot](slot-immediate.md) — immediate slot 0, where the signed-20-bit branch / back-edge target and the loop bound land.
- [Bundle Model](bundle-model-overview.md) — the per-generation bundle widths and the codec keyed by `(TpuVersion, TpuSequencerType)` that hosts these slots.
