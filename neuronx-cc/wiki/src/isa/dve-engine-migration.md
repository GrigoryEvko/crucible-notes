# DVE `0xF1`/`0xF2` — Engine-Migration Reconcile

> *Every byte, slot value, and symbol address on this page is read from one build: `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; the `cp310`/`cp311`/`cp312` wheels ship **byte-identical** `dve/` blobs — a property of the toolchain version, not the Python ABI). The blobs are `neuronxcc/dve/dve_bin_gen{2,3,4}/default_opcode_table.bin`; the symbols are `nm -DC` of `neuronxcc/starfish/lib/libwalrus.so` (`92b4d331a42d7e80bb839e03218d2b9b0c23c346`). See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the **two-layer resolution** of a tension that surfaces the moment you read both the [2.25 DVE opcode_table](dve-opcode-table.md) (the on-device **decode** table) and the [2.1 instruction-bundle](instruction-bundle.md) opcode word (the compiler **encode** word) for opcodes `0xF1` (241) and `0xF2` (242). The decode table **carries** `0xF1`/`0xF2` rows on gen2 but **not** on gen3/gen4; the compiler ISA **names and encodes** them on gen3/gen4 but **not** on gen2. The two facts point in opposite directions and look contradictory.

They are not. The DVE `opcode_table.bin` is the **DVE engine's** firmware decode table; the compiler op layer (stringifier + validator + visitor + JSON roster) is a separate, gen-versioned artifact — and for `0xF1`/`0xF2` the two layers move oppositely because **the op migrates off the DVE engine at gen3**. On gen2 the DVE silicon holds live, unique microcode for `0xF1`/`0xF2` that **no gen2 compiler path can emit** (decode-capable, never-emitted). At gen3 the ops become real compiler ops — `0xF1` = `DmaGatherTranspose` on the DMA/gather path, `0xF2` = `NonzeroWithCount` on the **Pool** engine ([1.09](../arch/pool-engine.md)) — and therefore vanish from the **DVE** decode table.

The bar for this page: a reader sees a `0xF1` or `0xF2` entry — in a decode-table dump, a wire word `0x10F1`/`0x10F2`, or a symbol roster — and **correctly assigns it to a generation and a layer** without mistaking a firmware decode-table row for an encodable compiler op. Every claim carries a confidence tag (CONFIRMED = exact from the shipped bytes / disassembled symbol; STRONG = cross-checked across ≥2 artifacts; INFERRED = read off layout + invariants, no single decisive store; SPECULATIVE).

## At a glance

| | 0xF0 = 240 `ExtendedInst` | 0xF1 = 241 `DmaGatherTranspose` | 0xF2 = 242 `NonzeroWithCount` |
|---|---|---|---|
| gen2 DVE `opcode_table` slot | `0x0369` → row 105, **resident** | `0x0074` → row 116, **resident** | `0x0077` → row 119, **resident** |
| gen2 JSON `ops[]` roster | absent (resident-only) | **absent** | **absent** |
| gen2 ISA name (`core_v2`) | `"ExtendedInst"` | *(empty no-case)* | *(empty no-case)* |
| gen2 encoder / validator | escape op (klr ser/des) | **none** | **none** |
| gen3/gen4 DVE `opcode_table` slot | nonzero → **roster** | **`0x00000000`** (gone) | **`0x00000000`** (gone) |
| gen3/gen4 ISA name | `"ExtendedInst"` | `"DmaGatherTranspose"` | `"NonzeroWithCount"` |
| gen3/gen4 engine | DVE | DMA / gather | **Pool** ([1.09](../arch/pool-engine.md)) |
| First real compiler op | all gens | **gen3** (Cayman) | **gen3** (Cayman) |

```
The two layers, and which way each op moves:

  layer A  DVE opcode_table.bin     (firmware DECODE table — DVE engine only)
  layer B  compiler ISA            (name + validator + visitor + JSON ops[])

           gen2            gen3 / gen4
  0xF1  A: resident   →    A: ABSENT      (op now on DMA/gather, not DVE)
        B: nameless   →    B: real op     DmaGatherTranspose
  0xF2  A: resident   →    A: ABSENT      (op now on Pool, not DVE)
        B: nameless   →    B: real op     NonzeroWithCount
```

> **GOTCHA — a decode-table slot is *not* an encodable op.** A nonzero `opcode_table.bin` slot proves only that the **DVE sequencer can decode** that opcode integer into a control/datapath program. It does **not** mean any compiler path emits that opcode. For gen2 `0xF1`/`0xF2`, the slot is live and unique (§2) yet there is no gen2 name, no gen2 validator, no gen2 visitor, no roster entry (§3): **decode-capable, compiler-never-emitted.** Read a decode-table entry as a firmware capability, an `ops[]` / visitor / stringifier entry as a compiler capability. They share only the opcode integer as a hinge.

---

## 1. The per-generation residency table

Notation: **decode-resident** = nonzero slot in `dve_bin_genN/default_opcode_table.bin` ([2.25](dve-opcode-table.md) decodes the slot format: gen2 stride 8 `u64`-LE, gen3/4 stride 4 `u32`-LE, slot `k` at byte `k·stride`); **roster** = membership in `dve_info.json` `ops[]`; **ISA name** = a non-empty `core_vN::enum_variant_string_opcode` case; **encoder/validator** = a `CoreVNGenImpl::visitInst*` / `core_vN::dbg_is_valid_*`.

| opcode | gen | decode-resident? | roster? | ISA name | encoder/validator | engine | encodable? |
|---|---|---|---|---|---|---|---|
| **0xF0** 240 `ExtendedInst` | gen2 | **yes** (row 105) | no | `"ExtendedInst"` | escape op | DVE | yes (escape) |
| | gen3 | yes (promoted) | **yes** | `"ExtendedInst"` | escape op | DVE | yes |
| | gen4 | yes (promoted) | **yes** | `"ExtendedInst"` | escape op | DVE | yes |
| **0xF1** 241 `DmaGatherTranspose` | gen2 | **yes** (row 116) | no | *(empty)* | **none** | — | **no** |
| | gen3 | **no** (slot `0x0`) | no | `"DmaGatherTranspose"` | `core_v3` validator | DMA/gather | **yes** |
| | gen4 | no (slot `0x0`) | no | `"DmaGatherTranspose"` | `core_v4` validator | DMA/gather | yes |
| **0xF2** 242 `NonzeroWithCount` | gen2 | **yes** (row 119) | no | *(empty)* | **none** | — | **no** |
| | gen3 | **no** (slot `0x0`) | no | `"NonzeroWithCount"` | `CoreV3` visitor + `core_v3` validator | **Pool** | **yes** |
| | gen4 | no (slot `0x0`) | no | `"NonzeroWithCount"` | `CoreV4` ISA-check + `core_v4` validator | Pool | yes |

Read across each row and the inversion is plain: `0xF1`/`0xF2` are **decode-resident only on gen2** and **encodable only on gen3+** — never both in the same generation. `0xF0` is the control case: a real op on every gen (gen2 names + decodes it; gen3/4 also roster it). The blob values are CONFIRMED below (§2); the encoder/validator residency is CONFIRMED from `nm` (§3).

> **NOTE — `0xF1`/`0xF2` are gen3+ ops, full stop, at every layer that ships in this wheel.** The compiler is the only artifact in the wheel. Its name table, validators, and visitors expose `0xF1`/`0xF2` only at gen3/gen4, and the gen3 validator carries a `CORE_VERSION > 2` arch gate (§3) that **rejects gen2**. The gen2 decode-table residency is a *firmware* fact about the silicon, not a compiler-emittable op.

## 2. The bytes — decode-table residency, per gen [CONFIRMED]

`struct.unpack` of `default_opcode_table.bin` at slots 147 / 240 / 241 / 242, with each `dve_info.json` `ops[]` roster cross-checked (re-derived this turn):

```
gen2 (stride 8, u64-LE)                 in blob   value      → row      in ops[]?
  idx 147 (0x93): 0201000000000000      yes       0x0102     row 2       NO  → resident
  idx 240 (0xF0): 6903000000000000      yes       0x0369     row 105     NO  → resident
  idx 241 (0xF1): 7400000000000000      yes       0x0074     row 116     NO  → resident
  idx 242 (0xF2): 7700000000000000      yes       0x0077     row 119     NO  → resident
  (gen2 default ops[]: 46 ops, max opcode 234 — 147/240/241/242 all above the roster)

gen3 (stride 4, u32-LE)                 in blob   value      in ops[]?
  idx 147 (0x93): 10040000              yes       0x0410     YES → PROMOTED to roster
  idx 240 (0xF0): 680c0000              yes       0x0C68     YES → PROMOTED to roster
  idx 241 (0xF1): 00000000              NO        0x0        no  → GONE from DVE table
  idx 242 (0xF2): 00000000              NO        0x0        no  → GONE from DVE table

gen4 (stride 4, u32-LE)  — identical pattern to gen3:
  idx 147 (0x93): 20040000  (roster) ;  idx 240 (0xF0): 6f0c0000  (roster)
  idx 241 (0xF1): 00000000  (gone)   ;  idx 242 (0xF2): 00000000  (gone)
```

Two facts fall out, both byte-exact:

**(a) gen2 carries `0xF1`/`0xF2` rows; gen3/gen4 zero them.** Slots 241/242 are nonzero on gen2 (`0x74`, `0x77`) and exactly `0x00000000` on gen3/gen4. The op left the DVE decode table at gen3. CONFIRMED.

**(b) The slot *value* `0x074` is a control-**row**, not opcode `0x74`.** The gen2 slot at index 241 holds `0x0074`, which [2.25](dve-opcode-table.md)'s split (gen2: `row = V & 0x7F`, `slow = V >> 7`) decodes to **control row 116, slow 0** — index 242 → **row 119, slow 0**. The integers 116/119 are the *rows the entry points at*, **not** opcodes; opcode index ≠ slot value. (This corrects a slip that read `0x074` as an opcode and guessed an "extended-op helper" — [2.25 §7](dve-opcode-table.md) flagged the 241/242 names as INFERRED; this page resolves them.) CONFIRMED.

Control rows 116 (0xF1) and 119 (0xF2) are **real, unique, paired** programs, not idle stubs or fill: they are referenced by exactly one opcode each, differ only in one sequencer-phase byte, and share an identical 7-active-lane datapath block found at no other row in the table. So the gen2 residency is a **live microcode program**, not a zero-fill artifact — but, per §3, gen2's compiler has no surface for it. STRONG (rows/datapath structure read off the gen2 control/datapath blobs; the datapath-word *field semantics* are firmware-side and INFERRED, see [2.25 §6](dve-opcode-table.md)).

## 3. No gen2 encoder — the decisive proof [CONFIRMED]

`nm -DC libwalrus.so` for the name table, the encoders, and the validators of all three ops. The name table is `core_vN::enum_variant_string_opcode(int, char*, int)` ([2.1](instruction-bundle.md)'s opcode→name authority; one per core, all three present: `core_v2 @0x127aea0`, `core_v3 @0x1369a40`, `core_v4 @0x143fd80`).

**(a) gen2's ISA layer has no name for `0xF1`/`0xF2`.** Disassembling `core_v2::enum_variant_string_opcode`'s jump table: opcode `0xF0` lands on a real `lea "ExtendedInst"; strncpy` case; `0xF1` and `0xF2` jump straight to the shared `pop %rbx; ret` epilogue — `strncpy` is never called, the output buffer is returned **unmodified** (an empty no-case). At `core_v3`/`core_v4` the same slots resolve to `"DmaGatherTranspose"` (0xF1) and `"NonzeroWithCount"` (0xF2). The names **first exist at v3**. CONFIRMED (byte-exact jump table + string deref; `core_v2` names only `0xF0`).

**(b) No gen2 encoder or validator exists for either op.** The full symbol roster:

```
NonzeroWithCount (0xF2):
  CoreV3GenImpl::visitInstNonzeroWithCount        @0x1355a30   (the only visitInst encoder)
  CoreV3GenImpl::runISACheckInstNonzeroWithCount  @0x13463c0
  CoreV4GenImpl::runISACheckInstNonzeroWithCount  @0x14325c0
  core_v3::{is,dbg_is}_valid_nonzero_with_count   @0x13d8ae0 / 0x13d8d80
  core_v4::{is,dbg_is}_valid_nonzero_with_count   @0x14b6f10 / 0x14b71a0
  → NO CoreV2GenImpl::visitInstNonzeroWithCount, NO core_v2 validator.

DmaGatherTranspose (0xF1):
  core_v3::{is,dbg_is}_valid_dma_gather_transpose @0x139e040 / 0x139e370
  core_v4::{is,dbg_is}_valid_dma_gather_transpose @0x1478960 / 0x1478c70
  → NO core_v2 validator. NO CoreVNGenImpl::visitInstDmaGatherTranspose in ANY core
    (it is a DMA/gather-path op, encoded off the DMA path, not a CoreVN TPB-inst visitor).
```

The absence is meaningful, not an artifact of a missing class: `CoreV2GenImpl` exists and stamps **68** `visitInst*` encoders — `NonzeroWithCount`/`DmaGatherTranspose` are simply not among them (vs `CoreV3GenImpl`'s 13 gen3-new visitors, which **do** include `visitInstNonzeroWithCount @0x1355a30`). CONFIRMED.

**(c) The gen3 validator gates out gen2 by `CORE_VERSION`.** `core_v3::dbg_is_valid_nonzero_with_count` asserts the wire opcode byte `== 0xF2`, then compares the `CORE_VERSION` argument against 2 (`cmpb $0x2, …; seta …; jbe <reject>`): the op is valid **iff version > 2** (gen3=3, gen4=4), rejecting gen2 (version 2). `dma_gather_transpose` carries the same gate. So even the gen3-resident validator structurally denies gen2. CONFIRMED.

**(d) The gen2 human-readable op manifest omits them.** `dve_bin_gen2/dve-tables-info.txt` enumerates the gen2 op families (copy/cast/transpose, tensor scalar/tensor/reduce, pool, max-pool-select, reciprocal, memset, rng, cumulative, batch-norm-*, load-param-ram/mask-select, Max 8, Match Value Load, Find Index 8, Pool Buffer Load, Gather). It lists **no** nonzero, extended, gather-transpose, or count op. The gen2 authors' own manifest does not name a `0xF1`/`0xF2` op. CONFIRMED.

## 4. The reconciliation — J27 ↔ T01, and the gen3 migration

**J27 (the [2.1](instruction-bundle.md) wire word) and T01 (the [2.25](dve-opcode-table.md) decode slot) share the opcode integer as their only hinge — and both are correct.** The compiler **emits** a 4-byte header word `{opcode, 0x10, 0x00, 0x00}` = little-endian `0x10NN`, where `NN` is the opcode byte and `0x10` is `inst_word_len` (16 dwords = 64 B), via `setupHeader` ([2.1](instruction-bundle.md)). For `0xF2` the word is `0x10F2`; its low byte `0xF2` = the T01 `opcode_table` **slot index 242**. The high byte `0x10` is a length and has **no bearing** on the decode-table rows. So J27's "0xF2 `NonzeroWithCount` = gen3-new" (an encode-side, compiler-ISA fact) and T01's "gen2 `opcode_table` carries a 0xF2 entry" (a decode-side, firmware fact) describe **two different layers of the same opcode integer**, not a contradiction. The name `NonzeroWithCount` is the `core_v3+` stringifier case for `0xF2`; gen2's stringifier returns empty for the same slot. CONFIRMED.

**Why the decode table has it on gen2 but not gen3, while the compiler is the reverse.** Join two facts:

- (A) `opcode_table.bin` is the **DVE engine's** firmware decode table — it ships only under `neuronxcc/dve/`, and no Pool/Act/PE/SP `opcode_table` blob exists in the wheel. It carries **DVE-engine ops only**.
- (B) The compiler ISA layer (name + validator + visitor + roster) is a separate, gen-versioned artifact (§3).

At **gen2 (Sunda, `core_v2`)** the DVE silicon holds live microcode for `0xF1`/`0xF2` (rows 116/119, §2), but no `core_v2` name/validator/visitor/roster entry exists — **decode-capable, compiler-never-emitted.** At **gen3 (Cayman, `core_v3`)** `0xF1`/`0xF2` become real compiler ops, but they execute on **other engines** — `NonzeroWithCount` (`0xF2`) on the **Pool** engine ([1.09](../arch/pool-engine.md); `InstNonzeroWithCount` = IT104, a gen3-new Pool primitive), `DmaGatherTranspose` (`0xF1`) on the DMA/gather path. Because they are no longer **DVE** ops, they leave the DVE-only `opcode_table` (slots zeroed, §2) and live entirely in the compiler ISA on a different engine. The op "graduated" out of the DVE decode table **and** into the compiler ISA on a different engine — one move, two visible faces. STRONG (joins the engine assignment from [1.09](../arch/pool-engine.md) / [2.18 DVE search encoding](dve-search-encoding.md) with the zeroed gen3 DVE slots).

```c
/* Two questions a 0xF1/0xF2 entry can answer — keep them separate. */

/* DECODE side (firmware, DVE engine):  "can the DVE sequencer decode opcode k?" */
bool dve_can_decode(int gen, uint8_t op) {        /* op in {0xF1,0xF2} */
    return gen == 2;        /* gen2: rows 116/119 present.  gen3/4: slot == 0 → no. */
}

/* ENCODE side (compiler ISA):  "will any compiler path EMIT opcode k, and on which engine?" */
engine_t compiler_emits(int gen, uint8_t op) {    /* op in {0xF1,0xF2} */
    if (gen < 3) return ENGINE_NONE;              /* no core_v2 name/validator/visitor    */
    return (op == 0xF2) ? ENGINE_POOL             /* NonzeroWithCount → Pool (gen3+)       */
                        : ENGINE_DMA;             /* DmaGatherTranspose → DMA/gather (gen3+)*/
}
/* The two never both return "yes" for the same gen — that IS the inversion, resolved. */
```

**Why gen2 microcode pre-stages two ops gen2 can't emit.** INFERRED: the gen2 DVE control/datapath table appears to be generated by a table-generator that also targets later silicon; the `0xF1`/`0xF2` sequencer programs were authored early (the DVE datapath exists), but ISA exposure + engine assignment were deferred to gen3 — at which point the op was reassigned off the DVE engine. This is the strongest form of the "ISA-ready / not-yet-rostered" pre-staging pattern seen for 96/147/152/240 ([2.25 §7](dve-opcode-table.md), promoted at gen3): `0xF1`/`0xF2` are not even ISA-named at gen2, only microcode-resident. No single decisive store pins the *reason*; tagged INFERRED.

## 5. In-place corrections

- **[2.25 §7](dve-opcode-table.md)** lists `{147, 240, 241, 242}` as "gen2-only residents" and says gen3/4 carry "zero residents". **Refined here:** 147 and 240 are gen2 residents that gen3/gen4 **promote into the JSON `ops[]` roster** (so on gen3/4 they are roster members, not residents — §2 bytes); only **241/242** are gen2-decode-table-only (zeroed on gen3/4). The residents exist as [2.25](dve-opcode-table.md) found; the "gen2-only for all four" and "gen3/4 zero residents" sub-claims are corrected. CONFIRMED.
- **[2.25 §7](dve-opcode-table.md)** marks the 241/242 names INFERRED ("extended-op helper candidates") and reads slot value `0x074` toward opcode `0x74`. **Resolved here:** 0xF1 = `DmaGatherTranspose`, 0xF2 = `NonzeroWithCount` (both `core_v3+` stringifier cases, §3a) — neither is named on gen2; `0x074` is the slot **value** (control row 116), not opcode `0x74` (§2b). CONFIRMED.

## 6. Confidence summary

**CONFIRMED** (byte-exact blobs / disassembled symbols):
- gen2 `opcode_table` slots 241=`0x0074`/242=`0x0077` nonzero & absent from the 46-op gen2 roster (resident); gen3/gen4 slots 241/242 = `0x00000000` (gone); 147/240 promoted into the gen3/4 roster.
- `core_v2::enum_variant_string_opcode`: `0xF0`→`"ExtendedInst"`; `0xF1`/`0xF2`→ empty no-case. `core_v3`/`core_v4`: `0xF1`→`"DmaGatherTranspose"`, `0xF2`→`"NonzeroWithCount"`.
- No `CoreV2GenImpl::visitInst*` and no `core_v2` validator for either op (vs 68 `CoreV2GenImpl::visitInst*` total); both first appear at `core_v3`, with a `CORE_VERSION > 2` arch gate that rejects gen2.
- `opcode_table.bin` is DVE-engine-only; gen3 `0xF2`→Pool (IT104), `0xF1`→DMA/gather; gen2 `dve-tables-info.txt` lists no nonzero/extended/gather-transpose/count op.
- J27 word `0x10F2` low byte = T01 slot index 242 — the shared integer hinge; high byte `0x10` is `inst_word_len`, not part of the opcode.

**STRONG**: the engine-migration explanation of the inversion (gen2 DVE microcode-resident vs gen3 compiler-op-on-Pool/DMA); the "decode-capable, compiler-never-emitted" characterization of gen2 `0xF1`/`0xF2`; the live-and-unique status of gen2 control rows 116/119 + their shared 7-lane datapath block.

**INFERRED**: *why* the gen2 DVE microcode pre-stages `0xF1`/`0xF2` (table-generator targets later silicon, ISA exposure + engine assignment deferred to gen3) — consistent with the [2.25 §7](dve-opcode-table.md) promotion pattern, not pinned to a single store; the exact *semantics* of the gen2 `0xF1`/`0xF2` datapath program (firmware-side field maps do not ship here — [2.25 §6](dve-opcode-table.md)).

## Cross-References

- [2.25 — DVE On-Device Microcode: `opcode_table`](dve-opcode-table.md) — the T01 decode table this page reconciles; the slot format (stride, bit-split), the gen2 residents, and the §7 entry corrected in place here.
- [2.1 — The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the J27 wire word `0x10NN` (`setupHeader`); the encode-side opcode this page hinges against the decode-side slot.
- [1.11 — The DVE Engine](../arch/dve-engine.md) — the engine that owns the `opcode_table` and the 46→52→59 roster growth; the engine `0xF1`/`0xF2` *leave* at gen3.
- [1.09 — The Pool Engine](../arch/pool-engine.md) — the gen3 migration **target** for `0xF2` `NonzeroWithCount` (IT104); the gen3-new Pool primitive.
- [2.18 — DVE Search & Datamove Encoding](dve-search-encoding.md) — the gen3+ `NonzeroWithCount` (`0xF2`, IT104) encoder field map (`CoreV3GenImpl::visitInstNonzeroWithCount @0x1355a30`, Pool engine).
- [Build & Version Provenance](../reference/versions.md) — the pinned build, the libwalrus hash, and the cp310/11/12 blob-parity argument.
