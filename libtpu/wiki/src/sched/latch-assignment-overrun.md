# Latch Assignment & Overrun

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d`, not stripped — full C++ symbols). Other versions differ.*

## Abstract

The MXU is weight-stationary: before a matmul step can clock, the stationary gain (weight) matrix must be *latched* into one of the array's per-quadrant weight banks, and one latch amortizes across many matmul steps. Stage 2 of the [scheduling pipeline](overview.md) — the MXU/MRB assignment pass — is where the compiler decides, for each accumulation chain, **which latch op gets which index in its `MxuSequence`** and **whether the first latch of a sequence needs an overrun-protection index at all**. That decision is `MxuAssigner::SetLatchIndices` (`sub_10F3B4C0`), and it is gated by a single per-generation virtual predicate, `Target::GainLatchModeHasOverrunChecks` (vtable `+0x358`).

The reason a "latch index in sequence" exists is a hazard the systolic array cannot express in cycles alone. A latch pushes a new weight matrix into a bank while a *previous* matmul may still be draining gains out of that bank through the array. If the new load lands too early it **overruns** the in-flight matmul's gains — the hardware corrupts the still-reading weights. Most generations sidestep this entirely (their first-latch index is never assigned and the scheduler relies on the bundle packer's slot legality), but Viperfish (TPU v5) carries an explicit MSR/first-latch overrun *handshake* at the gen level, and only for the wide non-bf16 weight formats whose wider reservation footprint makes the overrun reachable.

This page is the scheduling-side authority on three things. First, the `SetLatchIndices` walk and its first-latch gate — the algorithm. Second, the per-gen `GainLatchModeHasOverrunChecks` truth table and its coupled gen-level sibling `HasMsrOverrunChecks` — the policy. Third, the `CreateVectorLatchLsf` latch-op field layout, viewed as the data contract that `SetLatchIndices` reads (`latch_mode @+0x40`) and writes (`latch_index_in_sequence @+0x42`). The bit-level *encoding* of the latch op into the bundle word is the [Matprep / IAR / Latch ISA page](../isa/slot-matprep-iar-latch.md)'s job; this page treats the op fields only as far as the assignment pass touches them.

For reimplementation, the contract is:

- **The first latch of an `MxuSequence` is indexed only when its GLM "has overrun checks."** Every later latch is always indexed. The first-latch gate is `idx == 0 && !GainLatchModeHasOverrunChecks(glm)` → abandon the sequence (`break`).
- **Four of five gens are flat `FALSE`.** Jellyfish, Dragonfish, Pufferfish, Ghostlite never index their first latch. Only Viperfish's `+0x358` body is non-trivial: `!LatchModeIsTranspose(glm) && fmt ∈ {3,4,5,6,7,8}`, which resolves to GLM `{14,16,18,20,22,24}` — the six NO_XPOSE wide modes.
- **The gate predicate is keyed on the latch op's `GainLatchMode` (`BYTE[op+0x40]`)**, not on the matmul. `SetLatchIndices` reads it via `latch_mode(op)` and passes it to the vtable slot.
- **The latch index is a 16-bit field at `WORD[op+0x42]`**, bounded `≤ 65535`, written by `set_latch_index_in_sequence`. It is the same byte address as the load-LMR MSR for a disjoint opcode family — read it only after an opcode-family check.

| | |
|---|---|
| **Assignment driver** | `MxuAssigner::SetLatchIndices` `sub_10F3B4C0` (Stage 2 commit) |
| **First-latch gate** | `idx == 0 && !GainLatchModeHasOverrunChecks(latch_mode(op))` → `break` |
| **Per-gen predicate** | `Target::GainLatchModeHasOverrunChecks(glm)` vtable `+0x358` |
| **Gen-level sibling** | `Target::HasMsrOverrunChecks()` — `TRUE` only on Viperfish (`sub_1D49AAC0`) |
| **Viperfish body** | `sub_1D49AB20` = `!LatchModeIsTranspose && fmt∈{3..8}` → GLM `{14,16,18,20,22,24}` |
| **Index field** | `WORD[op+0x42]`, `set_latch_index_in_sequence` `sub_1D4E7960`, bound `≤ 0xFFFF` |
| **GLM field (gate key)** | `BYTE[op+0x40]`, `latch_mode` `sub_1D4E7500` / `set_latch_mode` `sub_1D4D7C20` |
| **Latch builder** | `LloInstruction::CreateVectorLatchLsf` `sub_1D4D7AA0`; general `CreateVectorLatchHelper` `sub_1D4D8360` |
| **Sequence record** | `MxuSequence` — latches list `@+0x18`, count `@+0x20` |
| **Confidence** | CONFIRMED (byte-anchored) unless a row or callout says otherwise |

---

## The Overrun Hazard and the Index

### What "overrun" means

The MXU is a systolic array clocked weight-stationary. A latch op writes the stationary gain matrix into a per-quadrant weight bank; a matmul op then streams the moving operand through the array, reading those latched weights at every systolic step. Because a matmul does not finish in one cycle — its result drains out of a matrix-result buffer many cycles after the op issues — the weights it reads stay *live* in the bank for the full drain. If the next latch op writes a fresh weight matrix into the same bank before the previous matmul has finished consuming the old one, the new write **overruns** the in-flight read.

A CPU/GPU backend never sees this hazard because register writes and the consuming instructions retire in a defined order the pipeline interlocks. The TPU MXU has no interlock for the weight bank: the schedule must statically guarantee a fresh latch does not land on a bank a still-draining matmul is reading. The compiler's tool for that guarantee is the **latch index in sequence** — a per-sequence ordinal that the downstream bundle packer and per-gen encoder use to keep latches and the matmuls that read them from colliding in time.

### Why the *first* latch is special

Within one `MxuSequence` (one accumulation chain), the matmuls are ordered and the latches that feed them are ordered the same way. Every latch *after the first* always gets an index — it is, by construction, a re-load that follows a matmul, so it always needs the ordering ordinal. The *first* latch of a sequence is the head of the chain: there is no prior matmul in *this* sequence for it to overrun. Whether the first latch nonetheless needs an index depends entirely on whether the hardware generation enforces an overrun handshake at the bank level — i.e. whether a matmul left over from a *previous* sequence could still be draining the bank the first latch targets.

On the four generations without the handshake, the answer is "no, never index the first latch": the bundle packer's slot legality plus the natural program order suffice, and `SetLatchIndices` abandons the sequence the moment it reaches the un-indexed first latch. On Viperfish, the answer is "yes, when the weight format is wide enough that its reservation footprint makes cross-sequence overrun reachable."

---

## SetLatchIndices — the Assignment Walk

`SetLatchIndices` (`sub_10F3B4C0`) is the Stage 2 commit step. It takes the span of `MxuSequence`s for a sequence group and, for each, walks the sequence's latch list assigning indices until it either runs out of latches or hits an un-indexed first latch.

```c
function MxuAssigner::SetLatchIndices(span<unique_ptr<MxuSequence>> seqs):  // sub_10F3B4C0
    for each seq in seqs:
        count = seq.latches.count                  // QWORD[seq+0x20]
        if (count == 0) continue
        idx = 0
        do:
            op  = seq.latches[idx]                  // ptr list @ QWORD[seq+0x18], 8*idx
            opc = WORD[op]                          // LloOpcode
            check((opc - 0x8d) < 0xa)               // LloOpcodeIsVectorLatch — else FATAL line 420
            tgt = op.region.module.target           // [[op+0x10]+0x38]+0x10
            glm = latch_mode(op)                    // BYTE[op+0x40]
            has_overrun = tgt.vtbl[+0x358](glm)     // GainLatchModeHasOverrunChecks
            if (idx == 0 && !has_overrun): break    // first latch, no overrun ⇒ abandon sequence
            set_latch_index_in_sequence(op, idx)    // WORD[op+0x42] = idx
            idx = (uint32)(idx + 1)
        while (idx < count)
```

The decompile (`sub_10F3B4C0`) is byte-exact: the latch list base is `QWORD[*v2 + 24]` (`+0x18`), the count is `QWORD[*v2 + 32]` (`+0x20`), the opcode bound is `(uint16)(opcode - 141) >= 0xA` → FATAL `"LloOpcodeIsVectorLatch(opcode)"` at `mxu_assigner.cc:420`, the target hop is `*(QWORD*)(*((QWORD*)op+2)+56)+16` (= `[[op+0x10]+0x38]+0x10`), the GLM read is `latch_mode(op)`, the gate is the `+856`-byte (`0x358`) virtual call, and the break is `if (!(DWORD)idx && !has_overrun) break`.

> **GOTCHA — the gate is `break`, not `continue`.** When the first latch of a sequence has no overrun checks, `SetLatchIndices` *abandons the whole sequence* — it does not skip just the first latch and index the rest. This is correct because on a flat-`FALSE` gen the first latch is always the un-indexed head and the inner loop has not advanced past it (`idx` is still 0), so there is nothing else to index yet. The break exits to the next sequence. A reimplementation that uses `continue` here would attempt to index later latches of a sequence whose head was never indexed — a malformed chain.

### The fields it reads and writes

`SetLatchIndices` touches exactly two latch-op fields, plus the sequence record:

| Access | Field | Offset | Accessor | Note |
|---|---|---|---|---|
| read | latches list base | `QWORD[seq+0x18]` | — | `LloInstruction*` array |
| read | latches count | `QWORD[seq+0x20]` | — | loop bound |
| read | `LloOpcode` | `WORD[op+0x00]` | — | `0x8d..0x96` check |
| read | `latch_mode` (GLM) | `BYTE[op+0x40]` | `latch_mode` `sub_1D4E7500` | gate key |
| write | `latch_index_in_sequence` | `WORD[op+0x42]` | `set_latch_index_in_sequence` `sub_1D4E7960` | the assignment |

`set_latch_index_in_sequence` (`sub_1D4E7960`) re-checks `LloOpcodeIsVectorLatch(opcode())` (`(uint16)(opcode-141) >= 0xA` → diagnostic at `llo_instruction.cc:3399`), bounds `index <= 65535` (`LloCheckOp 3`, `llo_instruction.cc:3400`), then stores `WORD[op + 33*2]` = `WORD[op+0x42]`. `latch_mode` (`sub_1D4E7500`) reads `BYTE[op+0x40]` for `(uint16)(opcode-141) <= 9` or a Matprep-subr opcode, else routes to a `Unsupported opcode` diagnostic.

> **NOTE — `WORD[op+0x42]` vs `BYTE[op+0x42]`.** The latch family (`0x8d..0x96`) stores its 16-bit `latch_index_in_sequence` at `+0x42`; the load-LMR family (`0xaa`/`0xab`) stores an 8-bit MSR at the same byte address (via the opcode-multiplexed `set_matrix_staging_register`, below). The two families are disjoint, so there is no aliasing within one op — but a reader that touches `+0x42` without first checking the opcode family will mis-decode one as the other.

---

## The Per-Gen Overrun Predicate

### GainLatchModeHasOverrunChecks (vtable +0x358)

The gate is a per-generation virtual on `Target`. The base body is an `Unimplemented` `LogFatal` stub (`target.h:2472`); every concrete generation overrides it. Four overrides return a flat `FALSE`; only Viperfish has a real body.

| Gen (TPU) | `GainLatchModeHasOverrunChecks` (`+0x358`) | body |
|---|---|---|
| Jellyfish (v2) | `sub_1D4925E0` | `return 0` ⇒ always FALSE |
| Dragonfish (v3) | `sub_1D4901C0` | `return 0` ⇒ always FALSE |
| Pufferfish (v4) | `sub_1D494880` | `return 0` ⇒ always FALSE |
| Viperfish (v5, v5e+v5p) | `sub_1D49AB20` | non-trivial (below) |
| Ghostlite (v6e) | `sub_1D497940` | `return 0` ⇒ always FALSE |
| base `Target` | `sub_1D61D8C0` | `LogFatal "Unimplemented"` (`target.h:2472`) |

The Viperfish body (`sub_1D49AB20`) is byte-exact:

```c
function ViperfishTarget::GainLatchModeHasOverrunChecks(glm):   // sub_1D49AB20
    if (LatchModeIsTranspose(glm)) return false;                // transpose ⇒ no overrun
    fmt = GainLatchModeToMatmulDataFormat(glm);                 // sub_1D629260
    return MatmulDataFormatIsIntegral(fmt) | ((uint8)(fmt - 3) < 2);
    //     ^ fmt ∈ {5,6,7,8}                  ^ fmt ∈ {3,4}     ⇒ TRUE ⇔ fmt ∈ {3,4,5,6,7,8}
```

Three small helpers fix the format and transpose classification, all byte-exact:

```c
function LatchModeIsTranspose(glm):           // sub_1D628EA0
    return bittest(0xAAAAAAAAAA6AA, glm);     // odd-GLM mask (+ bit 0xa)

function MatmulDataFormatIsIntegral(fmt):     // sub_1D629240
    return (uint8)(fmt - 5) < 4;              // fmt ∈ {5,6,7,8}

function GainLatchModeToMatmulDataFormat(glm):  // sub_1D629260  (switch; FATAL on a gap)
    0,1→1   10,11→2   14,15→3   16,17→4   18,19→5
    20,21→6  22,23→7   24,25→8   48,49→9   50,51→10   default→FATAL (matmul_data_format.cc:164)
```

### The Viperfish first-latch truth table

Running the predicate over the live GLMs and the `GainLatchModeToMatmulDataFormat` table, the first latch is indexed only for the six NO_XPOSE wide (non-bf16) modes:

| GLM | hex | fmt | transpose? | first-latch overrun | weight format (NO_XPOSE) |
|---|---|---|---|---|---|
| 0 | `0x00` | 1 | no | FALSE (bf16) | bf16 |
| 1 | `0x01` | 1 | yes | FALSE | bf16 XPOSE |
| 10 | `0x0a` | 2 | yes (in mask) | FALSE | bf16-alt / f32-pair |
| 11 | `0x0b` | 2 | no | FALSE (fmt2) | packed bf16 |
| 14 | `0x0e` | 3 | no | **TRUE** | F8E4M3FN |
| 16 | `0x10` | 4 | no | **TRUE** | F32 / F8E4M3B11 |
| 18 | `0x12` | 5 | no | **TRUE** | F8E5M2 |
| 20 | `0x14` | 6 | no | **TRUE** | S8 / int8 |
| 22 | `0x16` | 7 | no | **TRUE** | U4 nibble |
| 24 | `0x18` | 8 | no | **TRUE** | S4 nibble |
| 15/17/19/21/23/25 | odd | 3..8 | yes | FALSE | (transpose forms) |
| 48/50 | `0x30`/`0x32` | 9/10 | no | FALSE (fmt9/10) | fp8-conv / fp8-fnuz |

So Viperfish first-latch overrun fires for **GLM ∈ `{14,16,18,20,22,24}`**. The bf16 modes (fmt 1/2), the fp8-conversion modes (fmt 9/10), and *every* transpose GLM do not — transpose is excluded outright, and the bf16/fp8-conv formats are outside the `fmt ∈ {3..8}` window.

### HasMsrOverrunChecks — the gen-level coupling

The per-GLM predicate above is the refinement of a coarser, no-argument gen-level sibling, `Target::HasMsrOverrunChecks()`. It is `TRUE` only on Viperfish:

| Gen | `HasMsrOverrunChecks` | body |
|---|---|---|
| Jellyfish | `sub_1D4925C0` | `return 0` (FALSE) |
| Dragonfish | `sub_1D490160` | `return 0` (FALSE) |
| Pufferfish | `sub_1D494820` | `return 0` (FALSE) |
| Viperfish | `sub_1D49AAC0` | `return 1` (**TRUE**) |
| Ghostlite | `sub_1D4978E0` | `return 0` (FALSE) |
| base `Target` | `sub_1D61D800` | `LogFatal "Unimplemented"` |

> **NOTE — Viperfish (TPU v5) is the sole generation with the MSR/first-latch overrun handshake.** This is exactly why its per-GLM `+0x358` override is the only non-trivial body: the gen-level `HasMsrOverrunChecks` being `TRUE` is the *enabling* condition, and `GainLatchModeHasOverrunChecks` is the per-format *refinement* of when the handshake actually has to fire. The extra reservation cost of the indexed first latch is charged in the Viperfish cost model — see [MatmulMode and Modifiers](../cost/matmul-mode-modifiers.md), whose `AddOverrunCheckReservations` (`{Msr:2/6}`) lives in the Viperfish namespace and is the cost-side consumer of this same gate. The wide fmt-3..8 NO_XPOSE formats that fire here are precisely the formats whose matpush draws the widest reservation value-sets, making the overrun reachable.

---

## The Latch-Op Field Contract

`SetLatchIndices` operates on latch ops the LHS-partition step (`LatchLhs`, below) has already constructed. The constructor `CreateVectorLatchLsf` (`sub_1D4D7AA0`) is the canonical producer; this section pins the fields the assignment pass and its gate read. The bit-level *bundle encoding* of these fields is on the [Matprep / IAR / Latch ISA page](../isa/slot-matprep-iar-latch.md); here they are the LLO-IR data contract.

### The latch family

Ten `LloOpcode`s, `0x8d..0x96`, all routed through one of two constructors:

| `LloOpcode` | name | builder | constructor |
|---|---|---|---|
| `0x8d` | `kVectorLatchLsf` | `VlatchLsf` | `CreateVectorLatchLsf` (`sub_1D4D7AA0`) |
| `0x8e` | `kVectorLatchLsfMsk` | `VlatchLsfMsk` | `CreateVectorLatchLsfMasked` (`sub_1D4D8140`) |
| `0x8f` | `kVectorLatch` | `Vlatch` | `CreateVectorLatch` (`sub_1D4D8900`) |
| `0x90` | `kVectorLatchMsk` | `VlatchMsk` | `CreateVectorLatchMasked` (`sub_1D4D8C40`) |
| `0x91` | `kVectorLatch1` | `Vlatch1` | `CreateVectorLatch1` (`sub_1D4D8940`) |
| `0x92` | `kVectorLatch1Msk` | `Vlatch1Msk` | `CreateVectorLatch1Masked` (`sub_1D4D8C80`) |
| `0x93` | `kVectorLatch2` | `Vlatch2` | `CreateVectorLatch2` (`sub_1D4D8A80`) |
| `0x94` | `kVectorLatch2Msk` | `Vlatch2Msk` | `CreateVectorLatch2Masked` (`sub_1D4D8CC0`) |
| `0x95` | `kVectorLatch3` | `Vlatch3` | `CreateVectorLatch3` (`sub_1D4D8B60`) |
| `0x96` | `kVectorLatch3Msk` | `Vlatch3Msk` | `CreateVectorLatch3Masked` (`sub_1D4D8D00`) |

`VlatchI(value, long idx, glm)` (`sub_1D574580`) dispatches its `long idx` to `Vlatch1` (`0x91`) or `Vlatch2` (`0x93`) — the indexed latch sub-bank select. All ten opcodes satisfy `LloOpcodeIsVectorLatch` (`(opcode - 0x8d) < 0xa`), the predicate `SetLatchIndices` enforces.

### The LloInstruction field layout

The constructed latch op carries its operands in these fields, byte-exact from the setters and their symmetric readers:

| Offset | Field | Setter / reader | Meaning |
|---|---|---|---|
| `WORD[+0x00]` | `LloOpcode` | `New()` arg | `0x8d..0x96` |
| `BYTE[+0x0a]` | `register_number` | `set_register_number` / `sub_1D5A8E20` | gain-source VREG number |
| `WORD[+0x0b]` | control word | `set_unit_id` / `ValidateAndSetMxuAndSourceBus` | unit-id + source-bus (below) |
| `BYTE[+0x40]` | `latch_mode` (GLM) | `set_latch_mode` `sub_1D4D7C20` / `latch_mode` `sub_1D4E7500` | gate key |
| `WORD[+0x42]` | `latch_index_in_sequence` | `set_latch_index_in_sequence` `sub_1D4E7960` | assigned by `SetLatchIndices` |
| `BYTE[+0x44]` | `matrix_staging_register` (Msr) | `set_matrix_staging_register` `sub_1D4D7D40` | latch-bank / MSR destination |

The control word `WORD[+0x0b]` packs two bitfields (`set_unit_id` `sub_12698C00` for the unit-id; the source-bus inline in `ValidateAndSetMxuAndSourceBus`):

```c
// LloValue::set_unit_id (sub_12698C00) — the gain-matrix-register / MXU-quadrant pack
WORD[v+0x0b] = ((unit & 3) << 8) + (WORD[v+0x0b] & 0xF8FF) + 0x400;   // check unit <= 3
//   bits 8-9   : unit_id = which MXU quadrant (0..3) the gain matrix latches into
//   bit  10    : has-mxu flag (0x400)

// source-bus pack (ValidateAndSetMxuAndSourceBus, sub_1D4D7E80) — Pufferfish-only, non-LSF only
WORD[v+0x0b] = ((bus & 3) << 11) + (WORD[v+0x0b] & 0xC7FF) + 0x2000;  // check bus <= 3
//   bits 11-12 : VEX source-bus (0..3)
//   bit  13    : has-source-bus flag (0x2000)
```

The `matrix_staging_register` setter is opcode-multiplexed — the same logical field lands at four offsets depending on the opcode family (byte-exact from `sub_1D4D7D40`, identical mux in the reader `sub_1D4E7B80`):

```c
function set_matrix_staging_register(op, msr):   // sub_1D4D7D40
    if ((uint16)(opcode - 0x9b) <= 0xa):  BYTE[op+0x46] = msr   // matmul  0x9b..0xa5
    elif ((uint16)(opcode - 0x8d) <= 9):  BYTE[op+0x44] = msr   // latch   0x8d..0x96  ← the latch family
    elif ((opcode & 0xfffe) == 0xaa):     BYTE[op+0x42] = msr   // load-LMR 0xaa/0xab
    elif (opcode == 0xa8):                BYTE[op+0x41] = msr   // done-with-gains
    else: FATAL "msr unsupported for opcode" (llo_instruction.cc:3414)
```

### CreateVectorLatchLsf — the build sequence

`CreateVectorLatchLsf` (`sub_1D4D7AA0`) is the constructor `VlatchLsf` wraps. It guards the gain source and the GLM, then stamps the fields in order:

```c
function CreateVectorLatchLsf(gain_src, glm, unit_id, region):   // sub_1D4D7AA0
    if (gain_src.opcode >= 0x1cd) trap                           // opcode bound (ud1)
    if (opcode_produced_register_type[gain_src.opcode] != 4)     // gain source must produce reg-type 4
        UpdateStatus("chunk->ProducesVreg()")                    // slow diagnostic path (llo_instruction.cc:1073)
    if (glm > 0x33 || !bittest(0xf0000003c0c03, glm))            // LSF GLM-validity mask
        FATAL "LSF latch mode not expected." (llo_instruction.cc:1089)
    op = LloInstruction::New(0x8d /*kVectorLatchLsf*/, {gain_src}, region)
    set_latch_mode(op, glm)                                      // BYTE[op+0x40] = glm
    set_matrix_staging_register(op, 1)                           // BYTE[op+0x44] = 1 (LSF staging slot)
    ValidateAndSetMxuAndSourceBus(unit_id, op)                   // WORD[op+0x0b] unit-id (+ src-bus)
    return op
```

The two constructors admit different GLM sets (byte-exact `movabs` masks):

| Constructor | GLM-validity mask | Accepts GLM |
|---|---|---|
| `CreateVectorLatchLsf` | `0xf0000003c0c03` | `{0,1,10,11,18,19,20,21,48,49,50,51}` (bf16, F8E5M2, S8, fp8-conv) |
| `CreateVectorLatchHelper` | `0xf000003fffc3f` | `{0-5,10-25,48-51}` (full set incl. F8E4M3FN/F32 and nibble fmt7/8) |

`ValidateAndSetMxuAndSourceBus` (`sub_1D4D7E80`) bounds the MXU id (`mxu_id >= 0`, `mxu_id < MxusPerTensorCore()` = `DWORD[Target+0x4ac]`, the same `+0x4ac` index `LatchLhs` uses), stamps the unit-id, and — only if `HasVexSourceBuses()` (vtable `+0x408`, **`TRUE` only on Pufferfish**, `sub_1D494B40`) and `LloOpcodeUsesSourceBus(op)` — stamps the source bus.

> **QUIRK — `LloOpcodeUsesSourceBus` is FALSE for the LSF ops.** `LloOpcodeUsesSourceBus` (`sub_10C0D420`) returns `TRUE` for `0x8f..0x96` (the plain/indexed `Vlatch` forms) but not for `0x8d`/`0x8e` (the LSF forms). So `VlatchLsf` *never* writes the source-bus field, even on Pufferfish; the VEX source-bus latch field is populated only on Pufferfish and only for the non-LSF latch ops `0x8f..0x96`. A reimplementation that stamps a source bus on an LSF latch diverges from the binary. The opcode also gates which ops reach the MXU at all: `LloOpcodeUsesMxu` (`sub_10A433E0`) is `TRUE` iff `opcode ∈ [0x8d,0xa5] ∪ [0xa8,0xab] ∪ [0x152,0x153]`.

> **GAIN-SOURCE GUARD (INFERRED) —** the entry guard `opcode_produced_register_type[gain_src.opcode] != 4` selects a fast path when the *gain source* op produces register-type 4 (and a slow `LloModule::UpdateStatus("chunk->ProducesVreg()")` path otherwise). The `opcode_produced_register_type` table (`@0x223a16c0`, `.data.rel.ro`) is indexed by the producer opcode, not the latch opcode; the latch family's own entries are 0 because latches are consumers. Which producer opcodes yield type-4 — the precise gain/matrix register class the LSF latch consumes — was not enumerated cell-by-cell; the class identity is INFERRED as the MXU gain/matrix register class from context. **LOW.**

---

## LatchLhs — Where the First Latch Comes From

`SetLatchIndices` indexes latches that `LatchLhs` (`sub_10F3B5E0`) has already produced. `LatchLhs` is the gain-matrix partition step: it groups the LHS by transpose op, runs a per-MXU capacity guard, then rebuilds the sequence with each emitted latch+matmul+matres op tagged by its MXU quadrant.

```c
function LatchLhs(target, lhs_span, sequences):   // sub_10F3B5E0
    // per-sequence capacity guard
    acc = Σ over matreses of MatmulDataFormatPackingFactor(matmul_data_format(op))
    check( ChunksPerTile() * num_mxus >= acc )    // num_mxus = DWORD[Target+0x4ac]
    check( acc % ChunksPerTile() == 0 )           // tile-aligned
    for each matmul:
        q   = program_order & 3                   // the MXU quadrant (0..3)
        glm = byte_AC0913E[matmul_op - 0x9b]      // GLM byte table @0xac0913e
        emit = VlatchLsf(value, glm, /*unit_id=*/0)               // kVectorLatchLsf
        WORD[emit+0x0b] = ((q << 8) + 0x400) | (WORD[emit+0x0b] & 0xF8FF)   // overwrite unit-id with q
        repeat Vmatmul / Vmatres PackingFactor(fmt)×, each unit_id-stamped  // K-tile split
```

The GLM byte table at `0xac0913e` reads `{0,0,0,0,0,0,0,0,0xb,0xb}` (10 entries indexed by `matmul_op - 0x9b`): the plain matmul opcodes map to GLM 0 (bf16 NO_XPOSE), the last two (`0xa3`/`0xa4`) to GLM `0xb` (packed bf16). So the latches `SetLatchIndices` walks carry GLM 0 or `0xb` — both fmt 1/2, both *outside* the Viperfish `fmt ∈ {3..8}` overrun window. The `LatchLhs`-emitted bf16/packed-bf16 first latch therefore never trips the overrun gate; the gate fires only when a wider-format latch (GLM `{14,16,18,20,22,24}`) heads a Viperfish sequence by another path.

`MatmulDataFormatPackingFactor` (`sub_1D629300`, table `@0xb53c6bc` = `{1,2,4,4,4,4,8,8,4,4}` for fmt 1..10) is the column-pack factor that drives the K-tile loop count; `ChunksPerTile` and `num_mxus` (`Target+0x4ac`) bound the per-sequence capacity. The full `MxuSequence` record and the `LatchLhs` partition are on [MxuSequence / SequenceInfo](mxu-sequence-struct.md).

---

## Confidence Summary

| Claim | Evidence |
|---|---|
| `SetLatchIndices` walks each sequence's latch list and indexes until break | `sub_10F3B4C0` — list `@+0x18`, count `@+0x20`, opcode check `(op-141)<0xa` |
| First-latch gate is `idx==0 && !GainLatchModeHasOverrunChecks(glm)` → `break` | `sub_10F3B4C0` — `if (!(DWORD)idx && !has_overrun) break` after `vtbl[+0x358]` call |
| Gate keyed on `latch_mode(op)` = `BYTE[op+0x40]` | `latch_mode` `sub_1D4E7500` called at `+0x358` arg |
| Jellyfish/Dragonfish/Pufferfish/Ghostlite `+0x358` flat `FALSE` | `sub_1D4925E0`/`1D4901C0`/`1D494880`/`1D497940` = `return 0` |
| Viperfish `+0x358` = `!transpose && fmt∈{3..8}` → GLM `{14,16,18,20,22,24}` | `sub_1D49AB20` body + `GainLatchModeToMatmulDataFormat` `sub_1D629260` switch |
| `LatchModeIsTranspose` mask `0xAAAAAAAAAA6AA`; `IsIntegral` = `(fmt-5)<4` | `sub_1D628EA0` / `sub_1D629240` |
| `HasMsrOverrunChecks` `TRUE` only on Viperfish; base `LogFatal` | `sub_1D49AAC0` = `return 1`; others `return 0`; `sub_1D61D800` LogFatal |
| `latch_index_in_sequence` at `WORD[op+0x42]`, bound `≤ 0xFFFF` | `set_latch_index_in_sequence` `sub_1D4E7960` (`index <= 65535`, `WORD[op+33*2]`) |
| MSR opcode-mux: latch→`+0x44`, matmul→`+0x46`, load-LMR→`+0x42`, dwg→`+0x41` | `set_matrix_staging_register` `sub_1D4D7D40` |
| `CreateVectorLatchLsf` GLM mask `0xf0000003c0c03`; helper `0xf000003fffc3f` | `sub_1D4D7AA0` / `sub_1D4D8360` `movabs` + `_bittest64` |
| `CreateVectorLatchLsf` stamps `New(0x8d)`, GLM`@+0x40`, Msr=1`@+0x44`, unit-id`@+0x0b` | `sub_1D4D7AA0` build sequence |
| unit-id pack `bits 8-9 + 0x400`; source-bus pack `bits 11-12 + 0x2000` | `set_unit_id` `sub_12698C00`; `ValidateAndSetMxuAndSourceBus` `sub_1D4D7E80` |
| Source bus stamped only when `HasVexSourceBuses` (Pufferfish-only) AND `UsesSourceBus` (`0x8f..0x96`) | `sub_1D494B40`=1, others 0; `LloOpcodeUsesSourceBus` `sub_10C0D420` |
| `num_mxus` = `DWORD[Target+0x4ac]` (index 299) | `ValidateAndSetMxuAndSourceBus` `v6[299]`; `LatchLhs` `*((int*)v32+299)` |
| `LatchLhs` GLM byte table `@0xac0913e` = `{0×8, 0xb, 0xb}` | binary read at file off `0xac0913e` |
| Gain-source reg-type-4 producer class identity | `opcode_produced_register_type[…]` table indexed by producer; class not isolated |

---

## Cross-References

- [TPU Scheduling Pipeline](overview.md) — the four-stage stack; `SetLatchIndices` is the Stage 2 commit step, a precondition of Stage 3 bundle packing.
- [MxuSequence / SequenceInfo](mxu-sequence-struct.md) — the byte-exact `MxuSequence` record (latches list `@+0x18`, count `@+0x20`) and the `LatchLhs` partition that produces the latches indexed here.
- [MRB Chain Allocator](mrb-chain-allocator.md) — the accumulation-chain reservation timeline that runs in the same Stage 2 pass; the output-side analogue of latch assignment.
- [Matprep, IAR, and Latch Sub-Slots](../isa/slot-matprep-iar-latch.md) — the latch-op family and its `LloInstruction` field offsets from the ISA / encoding side; this page is the scheduling-side reader/writer of those same fields.
- [MXU Slot](../isa/slot-mxu.md) — the systolic-array matmul op family the latched gains feed; the consumer whose in-flight drain the overrun gate protects.
- [MatmulMode and Modifiers](../cost/matmul-mode-modifiers.md) — the Viperfish `AddOverrunCheckReservations` (`{Msr:2/6}`) cost charged when an indexed first latch fires; the cost-side consumer of `HasMsrOverrunChecks`.
- [MXU Latency Overview](../cost/mxu-latency-overview.md) — the per-gen reservation matrices behind the overrun-check cost.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part VIII — Instruction Scheduling & Bundle Packing — [back to index](../index.md)
