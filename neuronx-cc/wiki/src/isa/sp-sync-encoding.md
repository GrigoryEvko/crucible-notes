# SP Sync / Branch / Control Encoding

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core logic). The `CoreV2GenImpl::visitInst*` encoders, the wire-mode look-up tables and the `core_v{2,3,4}::dbg_is_valid_*` ISA validators live in `libwalrus.so` (`.text`/`.rodata`, VA == file offset; build-id `92b4d331…`); the `bir::Inst*` kinds and `bir::BranchCompareOp` enum live in `libBIR.so` (`a9b1ea38`); the simulator bodies that cross-confirm the BIR struct offsets live in `libBIRSimulator.so`. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of the **SP control band** — every sequencer control-flow and synchronization instruction the SP-class engine emits onto the wire. These are the bundles that carry no tensor datapath: they wait on semaphores, branch the program counter, barrier across engines, drain queues, and call/return/exit. The functional model — what the SP sequencer *does* with these — is [1.12 the SP engine](../arch/sp-engine.md); the hardware semaphore/event model they manipulate is [1.14 the execution sync model](../arch/execution-sync-model.md). This page is the *encoding*: for each op, the 64-byte bundle field map, the value source, and the disassembly store-site that pins it.

The control band is **eleven distinct BIR instruction kinds**, each with a `CoreV2GenImpl::visitInst*` emitter that produces exactly one 64-byte bundle:

| BIR `Inst*` kind | opcode | word | ISA struct / validator | engine |
|---|---|---|---|---|
| `EventSemaphore` (the task's "Semaphore") | `0xA0` | `0x10A0` | `ctrl_es` / `dbg_is_valid_ctrl_es` | datapath/SP |
| `CompareAndBranch` | `0xA9` | `0x10A9` | `ctrl_br` / `dbg_is_valid_ctrl_br` | SP |
| `UnconditionalBranch` | `0xA9` | `0x10A9` | `ctrl_br` (shared) | SP |
| `Halt` | `0xA1` | `0x10A1` | `dbg_is_valid_halt` | SP |
| `Drain` | `0xA2` | `0x10A2` | `dbg_is_valid_drain` | SP |
| `NoOp` | `0xA4` | `0x10A4` | `dbg_is_valid_nop` | any |
| `GroupResetSemaphores` | `0xB0` | `0x10B0` | `ctrl_er` / `ev_sem_range_clr` | datapath |
| `AllEngineBarrier` | `0xD5` | `0x10D5` | `PSEUDO_SYNC_BARRIER` | ALL → per-engine |
| `Call` | `0xD3` | `0x10D3` | `PSEUDO_FUNCTION_CALL` | broadcast |
| `Return` | `0xD2` | `0x10D2` | `PSEUDO_FUNCTION_RETURN` | broadcast |
| `Exit` | `0xDF` | `0x10DF` | `PSEUDO_EXIT_EXECUTION` | datapath |

Four further names in this area — **PollSem, Notify, EventSemaphoreRangeClear, CoreBarrier** — are *not* distinct BIR kinds in this build; their `bir::Inst*`/`visitInst*` symbols are absent. PollSem, Notify, and EventSemaphoreRangeClear are **ISA-decode subtypes** of the `0xA0`/`0xB0` sync family, each with its own `core_v{2,3,4}::dbg_is_valid_*` validator; CoreBarrier is a **gen3-only** op with no CoreV2 emitter (§6d). §1 resolves the three name-classes precisely, and [§9](#9-name-resolution--three-names-that-do-not-mean-what-they-look-like) collects them.

Every bundle is a `std::array<unsigned char, 64>`: zero-filled (so any byte the encoder does not write is a hard `0x00`), header-stamped by `setupHeader`, field-filled, validated by the ISA checker, and `fwrite(buf, 1, 0x40, bin)`-ed. The bar for this page: a reader can **byte-encode every SP sync/branch/control instruction by hand**, knowing for each control byte its offset, width, semantic, value source, the recovered field/struct name that *names* it, and the store-site that pins it. Every field row carries a confidence cell: CERTAIN means the exact store is disassembled or the rodata read directly, HIGH means a LUT or symbol cross-check backs it with one link not exhaustively walked, MEDIUM means zero-init is implied with no direct store. No ordinal, offset, or field name is fabricated.

## At a glance — the shared 64-byte skeleton

Every control bundle shares the standard TPB header ([2.1 the bundle](instruction-bundle.md)): byte `+0x00` is the opcode, byte `+0x01` is the constant `0x10` = `inst_word_len` (16 dwords = 64 bytes), bytes `+0x02..+0x03` are reserved. Read little-endian, `bundle[0:2]` is `(0x10<<8) | opcode` — which is why each op appears both as "opcode `0xA0`" and "word `0x10A0`".

```
 byte +0x00  opcode        (low byte of the opcode word)
 byte +0x01  inst_word_len = 0x10   (16 dwords = 64 bytes)
 byte +0x02..+0x03  reserved = 0x0000
```

`setupHeader` (`@0x1172120`, a 6-instr body) stamps `bundle[0]=opcode; bundle[1]=0x10; bundle[2:3]=0`. The encoder runs in one of three `CodeGenMode`s read from `Generator+0x9C`: `1` GENERATE_ISACODE (emit + `fwrite`), `2` RUN_ISA_CHECKS (stack bundle → virtual ISA validator, no `fwrite`), `0` COLLECT_OPCODES (record opcode + branch-hint target PC); anything else is a `"Wrong CodeGenMode"` fatal.

Two control bands carry payload past the header; the rest carry only an **inline semaphore predicate** at `+0x04..+0x08` (§8). The two payload bands are:

| Band | Used by | Layout |
|---|---|---|
| **Branch band** | `CompareAndBranch`, `UnconditionalBranch` | `comp_op@+0x0C`, dtype`@+0x0D`, AP-kind`@+0x0E`, RHS imm`@+0x10` / LHS reg`@+0x20` / RHS reg`@+0x21`, target PC qword`@+0x30` |
| **Events band** | `EventSemaphore` (`0xA0`) | inline 2-wait + 1-update: wait0 `@+0x04/+0x05/+0x08`, wait1 `@+0x20/+0x21/+0x24`, update `@+0x22/+0x23/+0x28` |

> **NOTE — `EventSemaphore` is the only op that does NOT use the shared `+0x04..+0x08` predicate template.** It has its own inline `events` / `events_extended` walk (§2). Every *other* control op (`Halt`, `Drain`, `NoOp`, `AllEngineBarrier`, `GroupResetSemaphores`, `Call`, `Return`, `Exit`, both branches) stamps the *same* 5-byte predicate via the `setupSyncWait`/`setupSyncUpdate` template (§8). This is the central design fact: most cross-engine syncs need no separate semaphore op because the synchronizer attaches the wait/update to a convenient control op and the encoder stamps it inline.

---

## 1. Name resolution — the 15 task names vs the actual ISA taxonomy

The task lists fifteen names. They split into three classes, and conflating them is the single biggest reimplementation trap:

**(A) Distinct BIR `Inst*` kinds, each with a `CoreV2GenImpl::visitInst*` emitter** (one 64-byte bundle each): `EventSemaphore` (`0xA0`, IT13), `CompareAndBranch` (`0xA9`, IT78), `UnconditionalBranch` (`0xA9`, IT79), `Halt` (`0xA1`, IT17), `Drain` (`0xA2`, IT16), `NoOp` (`0xA4`), `GroupResetSemaphores` (`0xB0`, IT14), `AllEngineBarrier` (`0xD5`, IT15), `Call` (`0xD3`, IT84), `Return` (`0xD2`), `Exit` (`0xDF`). All eleven `bir::Inst*` symbols are present in `libwalrus.so`: `InstEventSemaphore`, `InstCompareAndBranch`, `InstUnconditionalBranch`, `InstHalt`, `InstDrain`, `InstCall`, `InstReturn`, `InstExit`, `InstGroupResetSemaphores`, `InstAllEngineBarrier`.

**(B) ISA-decode subtypes of the `0xA0`/`0xB0` sync family — NO separate BIR kind in this build.** Each is realized on the wire as a variant of `EventSemaphore` (`0xA0`) or `GroupResetSemaphores` (`0xB0`), and each has its own decode-side validator:

| Subtype | Validator | Wire realization |
|---|---|---|
| `PollSem` | `core_v3::dbg_is_valid_ctrl_poll_sem` `@0x1296220` | `0xA0` events word — a **non-blocking** poll (test, no stall) |
| `Notify` | `core_v4::dbg_is_valid_notify` `@0x1291130` | `0xA0` events word with `UpdateMode 0` (evt-set `0x11`) — a one-shot runtime event |
| `EventSemaphoreRangeClear` | `core_v4::dbg_is_valid_ev_sem_range_clr` `@0x1283ff0` | `0xB0` `GroupResetSemaphores` range form + events predicate |

The `bir::Inst{PollSem,Notify,EventSemaphoreRangeClear}` and `CoreV2GenImpl::visitInst{PollSem,Notify,…}` symbols are **absent** from `libwalrus.so`, while the three `dbg_is_valid_*` validators are present as exported `core_v{3,4}` symbols. Each validator loads `mov $0xa0,%ecx` (or `0xB0`) and calls `core_v2::dbg_has_valid_neuron_events` — it validates an opcode-`0xA0`/`0xB0` word whose events sub-block carries the poll, set, or range. The per-poll wire field of `PollSem` is not byte-walked (§10, G1).

**(C) gen3-only op, no CoreV2 emitter.** `CoreBarrier` (`0xD8`, IT87, `PSEUDO_CORE_BARRIER`) is emitted only by `CoreV3GenImpl::visitInstCoreBarrier` `@0x1356550`. There is **no** `CoreV2GenImpl::visitInstCoreBarrier` (nm-confirmed); the cross-core barrier is a Trainium-gen3 addition, and CoreV2/V4 do not emit it. (Do not confuse it with `AllEngineBarrier` `0xD5`, which is the all-*engine* same-core fence, §6a.)

---

## 2. EventSemaphore (`0xA0`, IT13) — the fused 2-wait + 1-update word

`CoreV2GenImpl::visitInstEventSemaphore` `@0x1217df0`. The richest control word: a single 64-byte bundle that fuses **up to two semaphore WAITs and one semaphore UPDATE** — the hardware wait-and-update rendezvous. It does *not* use the shared `+0x04` predicate template; it has its own inline `events` (wait0) + `events_extended` (wait1 + update) walk.

GENERATE_ISACODE bundle base = `%r14` (the heap `array<u8,64>`). Offsets re-read off the encoder body at the cited VAs:

| off | width | field name (`instr.*` / ISA) | enum / wire | evidence | conf |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode | `0xA0` | `movb $0xa0` `@0x1217f78` → `setupHeader` | CERTAIN |
| `+0x01` | u8 | `inst_word_len` | `0x10` | `setupHeader` `@0x1172120` | CERTAIN |
| `+0x02` | u16 | reserved | `0x0000` | `setupHeader` | CERTAIN |
| `+0x04` | u8 | `events.wait_mode` (WAIT0) | `_WAIT_MODE` wire `07/05/85` | `sub_120be70`; `mov %al,0x4(%r14)` `@0x1218245` | CERTAIN |
| `+0x05` | u8 | `events.wait_idx` (WAIT0) | sema-bank index `<256` | `lea 0x5(%r14)`; `sub_12173A0` `@0x121826c` | CERTAIN |
| `+0x06` | u8 | reserved | `0` | not written in ev-sem path | HIGH |
| `+0x07` | u8 | reserved | `0` | — | HIGH |
| `+0x08` | u32 | WAIT0 value/reg | `WaitMode 2` → `Wait::getReg(RegId)`; else `Wait::getValue(imm)` | `mov %eax,0x8(%r14)` `@0x1218330` | CERTAIN |
| `+0x20` | u8 | `events_extended.wait_mode` (WAIT1) | `_WAIT_MODE` wire (optional) | `sub_120be70`; `mov %al,0x20(%r14)` `@0x121836f` | CERTAIN |
| `+0x21` | u8 | `events_extended.wait_idx` (WAIT1) | sema-bank index | `lea 0x21(%r14)`; `sub_12173A0` `@0x1218396` | CERTAIN |
| `+0x22` | u8 | `events_extended.update_mode` | `_UPDATE_MODE` wire `11/15/17/13/14/19` | `sub_120c430`; `mov %al,0x22(%r14)` `@0x12180f9` | CERTAIN |
| `+0x23` | u8 | `events_extended.update_idx` | UPDATE sema-bank index | `lea 0x23(%r14)`; `sub_12173A0` `@0x121811c` | CERTAIN |
| `+0x24` | u32 | WAIT1 value/reg | `WaitMode 2` → reg; else imm | `mov %eax,0x24(%r14)` `@0x121844f` | CERTAIN |
| `+0x28` | u32 | UPDATE value | `Update::getValue`; **SKIPPED** if `updMode ∈ {3 sem-inc, 4 sem-dec}` | `mov %eax,0x28(%r14)` `@0x1218129` | CERTAIN |

Bytes `+0x09..+0x1F`, `+0x25..+0x27`, `+0x29..+0x3F` are left `0` = reserved.

### The wire-mode look-up tables (rodata, xxd-confirmed)

The encoder never stores the BIR enum ordinal directly — it indexes a small rodata LUT and stores the *wire byte*. Two LUTs drive `EventSemaphore` (and, identically, the shared template §8):

```
_WAIT_MODE   byte_1DF577A = 07 05 85          (sub_120be70, bound `cmp $2; ja`)
  ord 0  evt-set-then-clear   → 0x07   (REJECTED for an ev-sem wait — event bank only)
  ord 1  SEM_GE_IMM           → 0x05   (wait until sema >= imm@+0x08)
  ord 2  SEM_GE_REG           → 0x85   (= 0x05 | 0x80, wait until sema >= reg@+0x08;
                                        the high bit 0x80 = "threshold is a register")

_UPDATE_MODE byte_1DF5774 = 11 15 17 13 14 19  (sub_120c430, bound a1<=5)
  ord 0  evt-set     → 0x11   (routes to the 256-bit EVENT bank, not the counter bank)
  ord 1  sem-add-imm → 0x15   (counter += imm@+0x28)
  ord 2  sem-sub-imm → 0x17   (counter -= imm@+0x28)
  ord 3  sem-inc     → 0x13   (counter += 1; val@+0x28 SKIPPED, implicit)
  ord 4  sem-dec     → 0x14   (counter -= 1; val@+0x28 SKIPPED, implicit)
  ord 5  sem-wr-imm  → 0x19   (counter  = imm@+0x28)
```

So the task's "wait / set / cmp fields" map to: **WAIT** = comparator byte `@+0x04` (`0x05`/`0x85`) + idx `@+0x05` + threshold `@+0x08`; **SET/inc/dec** = update byte `@+0x22` (`0x11`/`0x13`/`0x14`/`0x15`/`0x17`/`0x19`) + idx `@+0x23` + value `@+0x28`.

> **GOTCHA — `EventSemaphore` accepts ONLY `SEM_GE_IMM` (`0x05`) or `SEM_GE_REG` (`0x85`) per wait.** The encoder's assert string is verbatim `"only two wait modes supported now: SEM_GE_IMM and SEM_GE_REG"` (`@0x1d66598`). `EQ` (`0x01`) and `evt` (`0x07`) waits are *not* reachable from the compile-time `EventSemaphore` encoder — they appear only in runtime barrier/clear builders. A reimplementer that emits an `EQ` wait from a compile-side `EventSemaphore` produces a word the ISA checker rejects.

The other asserts: `"EventSemaphore must have sync info"` (`@0x1d66508`) and `"EventSemaphore must have <= 2 wait commands, and <= 1 update command…"` (`@0x1d66530`).

> **NOTE — there is no `clear-mode` field on IT13.** A semaphore *clear* is `WaitMode 0` / wire `0x07` on the EVENT bank, not a dedicated field. `EventSemaphoreClearMode` is a `GroupResetSemaphores` field (§6b), **not** an `EventSemaphore` field.

> **CONSISTENCY — agrees byte-for-byte with [1.14 the sync model](../arch/execution-sync-model.md).** 1.14 pins the comparator set `{0x01 EQ, 0x04 default, 0x05 GE-imm, 0x85 GE-reg, 0x07 evt}` and subtype set `{0x13 sem-inc, 0x14 sem-dec, 0x15 add-imm, 0x17 sub-imm, 0x19 wr-imm, 0x11 evt-set}` — identical to `byte_1DF577A`/`byte_1DF5774` above. 1.14 documents the **runtime** record's lane-2 (`cmp@+0x20`, `idx@+0x21`, `subtype@+0x22`, `idx@+0x23`, wait-val@+0x24, act-val@+0x28); this page documents the **compile-time** `EventSemaphore`, where lane-0 (`+0x04/+0x05/+0x08`) is wait0, lane-1 (`+0x20/+0x21/+0x24`) is wait1, and the update rides `+0x22/+0x23/+0x28`. The two encoders place the *same* cmp/subtype bytes; they differ only in which logical slot lane-0 holds (runtime: inline-events predicate; compile: wait0). No discrepancy.

### 2a. Notify — `dbg_is_valid_notify` `@0x1291130`

Not a distinct kind. `Notify` is the `EventSemaphore` word with the **EVENT-set update** (`UpdateMode 0` → wire `0x11` at `+0x22`) used to set a one-shot event bit so the runtime can observe completion (the documented use: notify the runtime when the last write to an output tensor occurs). EVIDENCE: `dbg_is_valid_notify` loads `mov $0xa0,%ecx` (`@0x12911e6`) and calls `core_v2::dbg_has_valid_neuron_events` — it validates an opcode-`0xA0` word whose events sub-block carries the event-set. Notify therefore rides the same `+0x04..+0x28` `events`/`events_extended` layout as §2, distinguished by `UpdateMode 0`. The validator's opcode immediate and its neuron-events call are witnessed; no separate emitter exists to byte-walk.

### 2b. PollSem — `dbg_is_valid_ctrl_poll_sem` `@0x1296220`

Not a distinct kind. `PollSem` is the ISA `CTRL_POLL_SEM` struct subtype — a **non-blocking** semaphore poll (test/read a semaphore without stalling the sequencer), as opposed to the blocking `SEM_GE` wait. `dbg_is_valid_ctrl_poll_sem` loads `mov $0xa0` (`@0x12962f8`) and validates via `dbg_has_valid_neuron_events`, so PollSem is a decode-side subtype of the same `0xA0` events word. No `bir::InstPollSem` / `visitInstPollSem` exists (nm-confirmed) — there is no separate compile-side emitter. The validator immediate and neuron-events call are witnessed; the poll-result wire field is not byte-walked (§10, G1).

---

## 3. CompareAndBranch (`0xA9`, IT78, `CTRL_BR`) — the fused conditional branch

`CoreV2GenImpl::visitInstCompareAndBranch` `@0x123ebd0`. The SP control branch is **one silicon instruction** — there is no compare-then-branch pair; the SP evaluates `lhs <pred> rhs` and jumps in one issue (the architectural model is [1.12 §the fused compare-and-branch](../arch/sp-engine.md)).

**BIR source struct (`bir::InstCompareAndBranch`)** — confirmed byte-exact by the simulator body `visitInstCompareAndBranch` `@0x1bffb0` in `libBIRSimulator.so` (a 99-byte function):

```asm
0x1bffcf:  mov  ecx, [rbx+0F0h]   ; comp_op = BranchCompareOp  @ InstCompareAndBranch+0xF0
0x1bffde:  call compareScalarArgs(lhs, rhs, comp_op)   ; ← the compare, fused into the branch
0x1bffe5:  jz   .on_false
   .on_true:  mov rax, [rbx+0F8h]  ; on_true  BasicBlock*  @ +0xF8   (taken when pred true)
   .on_false: mov rax, [rbx+100h]  ; on_false BasicBlock*  @ +0x100  (taken when pred false)
              mov [rbp+0E10h], rax ; → write chosen successor into the next-BB slot
```

So the source reads are: `Inst+0xF0` = `BranchCompareOp` (`getCompOp`, `<=0xB`; `0xC` = Unsupported sentinel); `Inst+0xF8` = `on_true` BB*; `Inst+0x100` = `on_false` BB*; `arg0` = LHS `RegisterAccess`; `arg1` = RHS `RegisterAccess` (REG form) or `ImmediateValue` (IMM form). The simulator body and the dumper's JSON keys `comp_op`/`on_true`/`on_false` agree at the same offsets.

**Wire bundle** (GENERATE_ISACODE base = `%r12`):

| off | width | field (`instr.*` / `CTRL_BR`) | enum / wire | evidence | conf |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode | `0xA9` | `movb $0xa9` `@0x123f4b0` → `setupHeader` | CERTAIN |
| `+0x01` | u8 | `inst_word_len` | `0x10` | `setupHeader` | CERTAIN |
| `+0x02` | u16 | reserved | `0` | — | CERTAIN |
| `+0x0C` | u8 | `instr.compare_op` (comp_op) | `_BRANCH_COMPARE_OP` wire (`byte_1DF5780`) | `sub_1203580`; `mov %al,0xc(%r12)` `@0x123f501` | CERTAIN |
| `+0x0D` | u8 | LHS dtype tag | `byte_1DF5760[dtype]` | `sub_120E650(arg0[12])`; `mov %al,0xd(%r12)` `@0x123f518` | CERTAIN |
| `+0x0E` | u8 | operand-AP kind | const `3` (register-AP/scalar) | `movb $0x3,0xe(%r12)` `@0x123f50f` | CERTAIN |
| `+0x10` | u32 | `instr.immediate` (RHS imm) **[IMM form]** | `ImmediateValue::getValue<u32>` | `mov %eax,0x10(%r12)` `@0x123fba9` (cond: `dyn_cast<ImmediateValue>`) | CERTAIN |
| `+0x20` | u8 | LHS register id | `Register::getRegId(arg0.loc,0)` | `mov %al,0x20(%r12)` `@0x123f52f` | CERTAIN |
| `+0x21` | u8 | RHS register id **[REG form]** | `Register::getRegId(rhs.loc,0)` | `mov %al,0x21(%r12)` `@0x123f554` (cond: `dyn_cast<RegisterAccess>`) | CERTAIN |
| `+0x30` | u64 | `instr.branch_target` (PC) | `getBranchTargetId(I, takenBB)` = SP instr-stream PC of the taken BB | `mov %rax,0x30(%r12)` `@0x123f582` | CERTAIN |
| `+0x04..+0x08` | 5 B | inline Wait/Update | `setupSyncWait<CTRL_BR>` `sub_121E4B0` / `setupSyncUpdate<CTRL_BR>` `sub_121E1F0` (§8) | calls `@0x123f6e3`/`@0x123f6ee` | CERTAIN |

`+0x30..+0x3F` is `movups`-zeroed first (`@0x123f4a7`), then the `+0x30` qword overwritten.

### The comp_op wire LUT — `byte_1DF5780` (12 bytes)

```
byte_1DF5780 = 01 02 03 04 05 06 09 0a 0b 0c 0d 0e
  BIR ord 0..5  IMM family {LT, LE, EQ, NE, GE, GT}  → wire 1..6
  BIR ord 6..11 REG family {LT, LE, EQ, NE, GE, GT}  → wire 9..14   (gap at wire 7,8)
  BIR ord 0xC = Unsupported (sentinel) → range-guard `>0xB` FATALs
       "Invalid enum variant for enum BranchCompareOp"
```

`sub_1203580` range-guards `>0xB`. The REG family selects RHS-is-register; the IMM family selects RHS-is-immediate (`@+0x10`). The compare runs at the **LHS dtype width** (`+0x0D`), so `is_lt` on `s32` and on `u8` are different comparisons — the simulator's `compareScalarArgs` (`@0x1bce50`) dispatches a per-dtype comparator table. The LUT is read directly, and the simulator's `compareScalarArgs(…, bir::BranchCompareOp)` symbol shows the enum is live.

### Pre-emit canonicalization — only the non-fall-through edge is encoded

A `CTRL_BR` bundle encodes **exactly one** successor: the TAKEN edge. The other edge must be the physical next basic block (`getNextNode()`):

```c
// CompareAndBranch encode (CoreV2GenImpl::visitInstCompareAndBranch @0x123ebd0)
BasicBlock *fall = I.getParent()->getNextNode();
if (I.on_false == fall) {                  // on_true is the taken edge — encode as-is
    encode_comp_op = wire(I.comp_op);                       // byte_1DF5780[I.comp_op]
    encode_target  = getBranchTargetId(I, I.on_true);
} else if (I.on_true == fall) {            // on_false is the taken edge — NEGATE the predicate
    encode_comp_op = wire(dword_1DF5720[I.comp_op]);        // LT<->GE, LE<->GT, EQ<->NE (+REG mirror)
    encode_target  = getBranchTargetId(I, I.on_false);
} else {
    assert_fail("onFalse == I.getParent()->getNextNode()"); // neither edge falls through
}
assert(I.getEngine() != ALL /*7*/);
// RHS discrimination:
if (auto *r = dyn_cast<RegisterAccess>(arg1))   bundle[0x21] = getRegId(r);   // REG form
else if (auto *im = dyn_cast<ImmediateValue>(arg1)) *(u32*)(bundle+0x10) = im->getValue<u32>(); // IMM
else assert_fail(/* RHS must be reg or imm */);
bundle[0x0E] = 3;                          // operand-AP kind = scalar/register
bundle[0x20] = getRegId(arg0);             // LHS register
```

The predicate-negate LUT:

```
dword_1DF5720 = 04 05 03 02 00 01 | 0a 0b 09 08 06 07
  ord 0 LT → 4 GE ; 1 LE → 5 GT ; 2 EQ → 3 NE ; 3 NE → 2 EQ ; 4 GE → 0 LT ; 5 GT → 1 LE
  (+ the REG-family mirror in the second half)
```

The LUT bytes are read directly. The fall-through/negate branch structure comes from the encoder body, but the per-arm conditional store chain is matched against the simulator's `on_true`/`on_false` selection rather than walked instruction by instruction in the V2 encoder.

---

## 4. UnconditionalBranch (`0xA9`, IT79, `CTRL_BR`) — the unconditional jump

`CoreV2GenImpl::visitInstUnconditionalBranch` `@0x121e850`. **Same opcode `0xA9`, same `CTRL_BR` struct** as the conditional branch — one silicon control-branch instruction. The `comp_op` byte `@+0x0C = 0` ("always") distinguishes it. BIR source: `Inst+0xF0` = target BB*.

| off | width | field | value / source | evidence | conf |
|---|---|---|---|---|---|
| `+0x00` | u8 | opcode | `0xA9` | `movb $0xa9` `@0x121e975` | CERTAIN |
| `+0x0C` | u8 | `instr.compare_op` | `0` ("always" — const) | `movb $0x0,0xc(%r13)` `@0x121e995` | CERTAIN |
| `+0x0E` | u8 | operand-AP kind | `3` (const) | `movb $0x3,0xe(%r13)` `@0x121e9a0` | CERTAIN |
| `+0x30` | u64 | `instr.branch_target` (PC) | `getBranchTargetId(I, target)` | `mov %rax,0x30(%r13)` `@0x121e9b6` | CERTAIN |
| `+0x04..+0x08` | 5 B | inline Wait/Update | `sub_121E4B0` / `sub_121E1F0` (§8) | — | CERTAIN |

> **QUIRK — an unconditional branch to the next block emits NO bundle.** If `target == parent->getNextNode()`, the whole encoder body is skipped — a fall-through `Uncond` is silently elided. There are no LHS/RHS operand bytes. The engine is asserted `!= ALL(7)`.

---

## 5. Halt (`0xA1`) / Drain (`0xA2`) / NoOp (`0xA4`) — `CTRL_NO` sequencer ops

These three share the `CTRL_NO_STRUCT` template: header + the shared `+0x04..+0x08` sync block (§8), nothing else (except Drain's count).

### 5a. Halt — `CoreV2GenImpl::visitInstHalt` `@0x122d620`, opcode `0xA1`, IT17 (sequencer STOP)

| off | width | field | source | conf |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0xA1` | `movb $0xa1` `@0x122d76f` → `setupHeader` | CERTAIN |
| `+0x04` | u8 | wait mode wire | `setupSyncWait` `sub_122D280` → `sub_120be70` | CERTAIN |
| `+0x05` | u8 | wait sema-idx | `sub_12173A0` | CERTAIN |
| `+0x06` | u8 | update mode wire | `setupSyncUpdate` `sub_122CFC0` → `sub_120c430` | CERTAIN |
| `+0x07` | u8 | update sema-idx | `sub_12173A0` | CERTAIN |
| `+0x08` | u32 | wait/update value | `getValue` (skip if `updMode ∈ {3,4}`) | CERTAIN |

Asserts `"Too many sync wait commands"` / `"Too many sync update commands"` if more than one of either. Validator `core_v2::dbg_is_valid_halt`.

### 5b. Drain — `CoreV2GenImpl::visitInstDrain` `@0x122d8d0`, opcode `0xA2`, IT16 (queue retire)

Identical `CTRL_NO` sync block as Halt, **plus a drain count**:

| off | width | field | source | conf |
|---|---|---|---|---|
| `+0x28` | u8 | DRAIN COUNT | `Inst+0xF0`; **GUARD** `Inst+0x110 == 0` (variant-tag must be `0` = compile-const) | `mov %al,0x28(%r12)` `@0x122da5b` | CERTAIN |

> **GOTCHA — the drain count MUST be a compile-time constant.** The guard `Inst+0x110 == 0` asserts the count is the constant arm of a `QuasiAffineExpr` (variant tag `0`); a register-valued count is rejected. `lower_control` mints the fence Drain with `Inst+0xF0 = 1` (full drain). Validator: `dbg_is_valid_drain`.

### 5c. NoOp — `CoreV2GenImpl::visitInstNoOp` `@0x122dbb0`, opcode `0xA4`, `CTRL_NO`

The minimal control bundle: header + the shared `CTRL_NO` Wait/Update only, no payload. Used as a pure sequencer sync slot / semaphore vehicle — a control op that carries an inline wait/update predicate so a separate `EventSemaphore` op is not needed (§8). `movb $0xa4` @ `0x122dcff`. Validator: `dbg_is_valid_nop`.

---

## 6. Barrier / bulk-semaphore fences — `AllEngineBarrier`, `GroupResetSemaphores`, `EventSemaphoreRangeClear`, `CoreBarrier`

### 6a. AllEngineBarrier — `CoreV2GenImpl::visitInstAllEngineBarrier` `@0x122aa10`, opcode `0xD5`, IT15, `PSEUDO_SYNC_BARRIER`

The all-engine fence. Header + the shared `+0x04..+0x08` sync block (§8) only:

| off | width | field | source | conf |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0xD5` | `movb $0xd5` `@0x122ab93` → `setupHeader` | CERTAIN |
| `+0x04..+0x08` | 5 B | shared sync block | `setupSyncWait` `sub_122A670` / `setupSyncUpdate` `sub_122A3B0` | CERTAIN |

`assert isDataPathEngine(I.getEngine())` (plt `@0x617a50`, call `@0x122abc0`). There is **no** engine-mask byte in the word beyond the sync block — the engine is implicit in *which* per-engine clone this is.

> **NOTE — `AllEngineBarrier` is a composite: one logical op becomes N per-engine bundles.** `lower_control` mints ONE on engine `ALL(7)`; `ExpandAllEngineFinalPreCodegenImpl::expandInstruction` `@0xcee080` `fullClone()`s it per datapath engine (`cmpl $0x7,0x90; fullClone; mov %eax,0x90`), so the encoder above never sees engine `ALL` — it sees each per-engine clone. The verifier proves the abstract barrier topology *before* expansion; the expander preserves the proof by duplicating without reordering ([1.14](../arch/execution-sync-model.md)).

### 6b. GroupResetSemaphores — `CoreV2GenImpl::visitInstGroupResetSemaphores` `@0x122ba40`, opcode `0xB0`, IT14, `CTRL_ER`

The bulk semaphore re-arm. Whole 64 B `movups`-zeroed `@0x122bbb0` first.

| off | width | field (`instr.*` / `CTRL_ER`) | source / wire | conf |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0xB0` | `movb $0xb0` `@0x122bbc2` → `setupHeader` | CERTAIN |
| `+0x04..+0x08` | 5 B | shared `CTRL_ER` sync block | `setupSyncWait` `sub_122B6A0` / `setupSyncUpdate` `sub_122B3E0` (§8) | CERTAIN |
| `+0x0C` | u8 | CLEAR-MODE (wire) | `sub_120DEC0(Inst+0xF0)`: `0` SemaphoreZero → `0x01`; `1` SemaphoreZeroBitmask → `0x02` | `mov %al,0xc(%r12)` `@0x122bc2b` | CERTAIN |
| `+0x0D` | u8 | `instr.range_first` **[ClearMode 0]** | `sub_12173A0` (`lea 0xd(%r12)`); MIN of SemaGroup ids | rodata `"instr.range_first"` `@0x1c84b77` | CERTAIN |
| `+0x0E` | u8 | `instr.range_last` **[ClearMode 0]** | `sub_12173A0` (`lea 0xe(%r12)`); MAX of SemaGroup ids | rodata `"instr.range_last"` `@0x1c84b89` | CERTAIN |
| `+0x20` | 256b | sema bitmask (8 × u32) **[ClearMode 1]** | `for id in SemaGroup: mask[id>>5] |= 1<<(id&0x1F)` | `movups %xmm7,0x20(%r12)` `@0x122ca44` + per-word stores | CERTAIN |

```c
// ClearMode-1 bitmask build
u32 mask[8] = {0};
for (SemaId id : I.getSemaGroup())  mask[id >> 5] |= (1u << (id & 0x1F));   // 256-bit
memcpy(bundle + 0x20, mask, 32);
```

Asserts `isDataPathEngine` + `!getSemaGroup().empty()`. The verifier `checkGroupResetSemaphores` `@0xfeceb0` forbids duplicates, so the range `[first,last]` is exact. Composite like §6a: `ExpandAllEngineFinalPreCodegenImpl::visitInstGroupResetSemaphores` `@0xceebc0` `fullClone`s per datapath engine.

### 6c. EventSemaphoreRangeClear — the `0xB0`/`0xA0` name reconciliation

There is an apparent opcode conflict: the ISA opcode-enum lists slot 176 (`0xB0`) = `EventSemaphoreRangeClear`, but the BIR-level emitter at opcode `0xB0` is `visitInstGroupResetSemaphores` (§6b). The resolution:

- The **range form** of `GroupResetSemaphores` (ClearMode 0, `instr.range_first`/`range_last` at `+0x0D`/`+0x0E`) **is** the `EventSemaphoreRangeClear` semantic: zero every semaphore id in `[first,last]`.
- `dbg_is_valid_ev_sem_range_clr` `@0x1283ff0` validates against `mov $0xa0,%ecx` / `dbg_has_valid_neuron_events` — the decode-side validator treats the range-clear as an EVENTS-bearing word (the events sub-block at `+0x04` carries an optional wait/update alongside the range).

So `EventSemaphoreRangeClear` = `GroupResetSemaphores` range form (`0xB0`) **with** the events predicate; `GroupReset`/`EventSemaphore` are the compiler `Inst*` names, and `PollSem`/`Notify`/`RangeClear` are the ISA-decode subtype names. The two name-spaces overlap on the same `0xB0`/`0xA0` wire words. The opcode enum `0xB0`, the emitter's `movb 0xB0`, the `ev_sem_range_clr` `$0xA0`-events validator, and the range-field strings are all witnessed.

### 6d. CoreBarrier — `CoreV3GenImpl::visitInstCoreBarrier` `@0x1356550` (gen3 ONLY), opcode `0xD8`, IT87, `PSEUDO_CORE_BARRIER`

The cross-core HW barrier. **No CoreV2 emitter exists** (nm-confirmed). This is a CoreV3 (Cayman, arch 30) addition.

| off | width | field | source | conf |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0xD8` | `setupHeader` (CoreV3 `@0x1369280`, `movb $0xd8` `@0x135669f`) | CERTAIN |
| `+0x0C` | u32 | barrier HW reg id | `getNextCoreBarrierId` `@0x61c2a0` (a separate HW-barrier-register namespace, NOT a semaphore) | `mov %eax,0xc(%r12)` `@0x13566e7` | CERTAIN |
| `+0x10` | u32 | static index | per-barrier static index | `mov %eax,0x10(%r12)` `@0x13566d6` | CERTAIN |

`CoreBarrier` emits a **companion descriptor sub-word** (`assignAccess<TENSOR4D>`, fields `@+0x20`/`+0x22`, a 2nd `fwrite`) — the cross-core handshake buffer. The full gen3 companion field map is gen3-ISA territory; here it is enumerated as id `@+0xC` / index `@+0x10` plus a second `fwrite`. The id and index are exact; the companion sub-word's existence rests on the `assignAccess` call and the second write.

---

## 7. Call (`0xD3`) / Return (`0xD2`) / Exit (`0xDF`) — the function control-transfer family

### 7a. Call — `CoreV2GenImpl::visitInstCall` `@0x12633e0`, opcode `0xD3`, IT84, `PSEUDO_FUNCTION_CALL`

**Per-engine broadcast**: loops `Module::listEnginesUsed()` (`@0x1263501`), sets `Inst+144` = `EngineInfo`, emits one `0xD3` bundle per used engine. Whole 64 B `movaps`-zeroed `@0x126374d` first.

| off | width | field (`instr.*` / ISA) | source | conf |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0xD3` | `setupHeader` | CERTAIN |
| `+0x0C` | char[35] | call target NAME (`<=0x23`=35 B) | `strncpy(bundle+0xC, targetName, 0x23)`; name from `target+0xB0/+0xB8` via `sub_1202790`; `ASSERT len<=0x23` | `lea 0xc(%r13)`; `mov $0x23`; `strncpy` `@0x126383f` | CERTAIN |
| `+0x30` | u32 | `instr.args_table_var_id` | `sub_124BD70(bundle+0x30, "instr.args_table_var_id", &argId, I)`; `argId = Inst+288`; only when `argc>0` AND module is args-table flavor | `lea 0x30(%r13)`; call `sub_124BD70` `@0x12637c6` | CERTAIN |
| `+0x04..+0x08` | 5 B | inline Wait/Update | `sub_121CBD0` / `sub_121C910` (§8) | — | CERTAIN |

> **QUIRK — the callee is referenced by a 35-byte NAME string, not a PC.** `Call` writes the target *name* (`strncpy`, capped at 35 bytes) at `+0x0C`, where `CompareAndBranch` writes a resolved PC qword at `+0x30`. One bundle is emitted **per used engine** (the broadcast).

### 7b. Return — `CoreV2GenImpl::visitInstReturn` `@0x121c2d0`, opcode `0xD2`, `PSEUDO_FUNCTION_RETURN`

Per-engine broadcast (loops `listEnginesUsed`, sets `Inst+144` per engine, zeroes `Inst+0x90` at end). Header + shared Wait/Update only, no payload.

| off | width | field | source | conf |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0xD2` | `movb $0xd2` `@0x121c651` → `setupHeader` | CERTAIN |
| `+0x04..+0x08` | 5 B | inline Wait/Update | `sub_121BF30` / `sub_121BC70` (§8) | — | CERTAIN |

A bare per-engine return barrier (one per used engine).

### 7c. Exit — `CoreV2GenImpl::visitInstExit` `@0x121b290`, opcode `0xDF`, `PSEUDO_EXIT_EXECUTION`

**Single** bundle (no engine loop). `ASSERT isDataPathEngine` (`@0x121b440`).

| off | width | field | source | conf |
|---|---|---|---|---|
| `+0x00` | u8 | opcode `0xDF` | `movb $0xdf` `@0x121b413` → `setupHeader` | CERTAIN |
| `+0x0C` | u8 | datapath flag = `1` | const `1` (set after `isDataPathEngine`) | `movb $0x1,0xc(%r13)` `@0x121b5b0` | CERTAIN |
| `+0x04..+0x08` | 5 B | inline Wait/Update | `sub_121AEF0` / `sub_121AC30` (§8) | — | CERTAIN |

The simulator's `visitInstExit` throws `InstExitException` (single-core) or joins all engines (LNC).

---

## 8. The shared embedded-predicate template — `setupSyncWait` / `setupSyncUpdate`

Every control/sync op above **except `EventSemaphore`** (which has its own inline 2-wait/1-update walk, §2) stamps an inline semaphore predicate via two template helpers. They are instantiated per ISA-struct but write the *same* bytes:

```c
// setupSyncWait<ISA_STRUCT> — the WAIT predicate (the +0x04..+0x08 lane)
bundle[0x04] = wire_of_waitmode(w.getMode());  // sub_120be70 → byte_1DF577A {07,05,85}
bundle[0x05] = w.getId() & 0xff;               // SyncRef::getId → sub_12173A0  (errors if >0xff)
*(u32*)(bundle+0x08) = (w.getMode()==2 /*GE_REG*/) ? w.getReg() : w.getValue();   // reg or imm

// setupSyncUpdate<ISA_STRUCT> — the UPDATE predicate
bundle[0x06] = wire_of_updmode(u.getMode());   // sub_120c430 → byte_1DF5774 {11,15,17,13,14,19}
bundle[0x07] = u.getId() & 0xff;               // SyncRef::getId → sub_12173A0
if (u.getMode() != 3 /*sem-inc*/ && u.getMode() != 4 /*sem-dec*/)
    *(u32*)(bundle+0x08) = u.getValue();       // value SKIPPED for inc/dec (implicit ±1)
```

The idx strings are `"instr.events.wait_idx"` (`@0x1c849bc`) and `"instr.events.update_idx"` (`@0x1c849f0`). Per-struct wait/update sub instances:

| ISA struct | op(s) | wait / update sub |
|---|---|---|
| `CTRL_BR` | CompareAndBranch / UncondBranch | `sub_121E4B0` / `sub_121E1F0` |
| `PSEUDO_BR_HINT` | BranchHint | `sub_121D230` / `sub_121CF70` |
| `CTRL_NO_STRUCT` | Halt / Drain / NoOp | `sub_122D280` / `sub_122CFC0` |
| `PSEUDO_SYNC_BARRIER` | AllEngineBarrier | `sub_122A670` / `sub_122A3B0` |
| `CTRL_ER` | GroupResetSemaphores | `sub_122B6A0` / `sub_122B3E0` |
| `PSEUDO_FUNCTION_CALL` | Call | `sub_121CBD0` / `sub_121C910` |
| `PSEUDO_FUNCTION_RETURN` | Return | `sub_121BF30` / `sub_121BC70` |
| `PSEUDO_EXIT_EXECUTION` | Exit | `sub_121AEF0` / `sub_121AC30` |

> **NOTE — why most syncs need no separate semaphore op.** The data-path COMPUTE ops (matmul/pool/memset/activation) make zero `getSyncInfo` calls — their cross-engine deps ride a standalone `EventSemaphore` (IT13). But the control/seq ops (Exit, Return, BranchHint, UnconditionalBranch, NoOp, DMATrigger, …) *do* carry `SyncInfo`, so the synchronizer attaches the Wait/Update to a convenient control op and the encoder stamps it inline at `+0x04..+0x08` — no dedicated semaphore instruction is emitted. This rests on the `getSyncInfo` census, cross-checked against [1.14](../arch/execution-sync-model.md).

> **GOTCHA — the sema index is NOT computed by the encoder; it is masked to 8 bits.** The `+0x05`/`+0x07`/`+0x23` idx bytes are read straight off `SyncRef::getId()` (assigned upstream by the `allocSema 0..255` / `assignEngineSemaphore` / `assignDMASemaphore` passes) and written through `sub_12173A0`, which masks `&0xff` and **errors if the id exceeds 8 bits** (the 256-sema-per-NeuronCore bank limit). This idx writer and its 256-bank limit are arch-invariant across CoreV2/V3/V4.

---

## 9. Name resolution — three names that do not mean what they look like

Three of the names in this area are ISA-decode labels rather than compiler-side instruction kinds, and reading them as distinct opcodes is the most common way to get this family wrong.

**"Semaphore" is `EventSemaphore` (IT13), opcode `0xA0`.** There is no separate op called simply "Semaphore".

**`CompareAndBranch` and `UnconditionalBranch` share opcode `0xA9` and the `CTRL_BR` struct.** They are distinguished only by the `comp_op` byte at `+0x0C`, where `0` means "always" — i.e. unconditional. There is no separate unconditional-branch opcode to emit.

**`PollSem`, `Notify`, and `EventSemaphoreRangeClear` are not BIR kinds in this build.** No `bir::Inst{PollSem,Notify,EventSemaphoreRangeClear}` and no matching `CoreV2GenImpl::visitInst*` symbols exist. They are ISA-decode subtype names, each with its own `dbg_is_valid_*` validator, realized on the wire as variants of `EventSemaphore` (`0xA0`) and `GroupResetSemaphores` (`0xB0`).

**`CoreBarrier` (`0xD8`) is CoreV3-only** and has no CoreV2 emitter. Do not read the `0xD5` composite as its CoreV2 equivalent: `0xD5` is `AllEngineBarrier`, the all-*engine* same-core fence, a different op entirely (§6a versus §6d).

Every offset on this page agrees with [1.14](../arch/execution-sync-model.md) and [1.12](../arch/sp-engine.md): the comparator set (`0x01/0x04/0x05/0x85/0x07`), the subtype set (`0x11/0x13/0x14/0x15/0x17/0x19`), the `CTRL_BR` BIR offsets (`+0xF0/+0xF8/+0x100`), and the shared `+0x04..+0x08` predicate lane.

---

## 10. What pins each claim, and the gaps

**Byte-exact in this build:** all eleven BIR-emitter opcodes (from the `movb` immediates: EventSemaphore `0xA0`, both branches `0xA9`, Halt `0xA1`, Drain `0xA2`, NoOp `0xA4`, GroupReset `0xB0`, AllEngineBarrier `0xD5`, Call `0xD3`, Return `0xD2`, Exit `0xDF`; CoreBarrier `0xD8` gen3); the `EventSemaphore` field map (`+0x04/+0x05/+0x08`, `+0x20/+0x21/+0x24`, `+0x22/+0x23/+0x28`); the `CompareAndBranch` field map (`comp_op@+0xC`, dtype`@+0xD`, kind3`@+0xE`, imm`@+0x10`, LHS`@+0x20`, RHS`@+0x21`, target qword`@+0x30`) and the byte-exact BIR struct offsets via the simulator body; the Drain count`@+0x28`; GroupReset ClearMode`@+0xC` / range`@+0xD,+0xE` / bitmask`@+0x20`; Exit flag`@+0xC`; Call name `strncpy`+0xC / `args_table_var_id@+0x30`; CoreBarrier id`@+0xC` / index`@+0x10`; all five wire LUTs (`byte_1DF5780`/`dword_1DF5720`/`byte_1DF577A`/`byte_1DF5774`/`byte_1DF5760`); the field-name rodata strings.

**Cross-checked rather than byte-walked:** the branch fall-through/negate canonicalization arm by arm; the three ISA-decode subtypes' wire realizations, where validator immediates and neuron-events calls are witnessed but no separate emitter exists to disassemble; and the existence of the CoreBarrier TENSOR4D companion sub-word.

**Gaps:**
- **(G1)** `PollSem`'s poll-result wire field is not byte-walked — there is no distinct emitter to disassemble, only the ISA validator.
- **(G2)** `CoreBarrier`'s TENSOR4D companion field map is enumerated only as id`@+0xC`/index`@+0x10` + "2nd `fwrite`"; the full gen3 companion descriptor is out of scope for this CoreV2-centred page.
- **(G3)** The runtime (post-NEFF) 64-byte record layout reorganizes the `events_extended` half-block; the **compile-side** bundle layout here is authoritative for the wire word. The runtime lane mapping is in [1.14](../arch/execution-sync-model.md).

---

## Cross-References

- [1.14 The Execution Sync Model](../arch/execution-sync-model.md) — the 256-entry semaphore bank, the 64-byte sync record, the comparator/subtype sets this page encodes; the runtime-side lane mapping.
- [1.12 The SP Engine](../arch/sp-engine.md) — the functional model: the fused single-issue compare-and-branch, the next-BB-slot PC mechanism, what the sequencer does with these bundles.
- [2.19 SP Register-Lane Encoding](sp-register-encoding.md) — the sibling SP-band page: register-move / TensorLoad-Save encodings (the data-carrying SP ops).
- [2.1 The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the shared header (`opcode`, `inst_word_len`, reserved) every bundle on this page starts with.
- [2.10 PE Matmul Encoding](pe-matmul-encoding.md) — the CoreV2/V3/V4 generator dispatch and `setupHeader` convention shared with these control ops.
- [walrus Part 8 — `lower_control` / `lower_branch`](../walrus/register-allocation.md) — the passes (H35/H38) that mint the fence `Drain`, explode structured loops into a counter `RegisterAlu` + flat `CmpBranch`, and re-home branches onto SP before codegen.
- [BIR Part 7 — Sim Sequencer](../bir/sim-sequencer.md) — the barrier-expansion (I11) and the simulator bodies that byte-confirm the `CTRL_BR` struct offsets.
