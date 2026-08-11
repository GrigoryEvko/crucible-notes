# DVE On-Device Microcode: `datapath_table`

> *All blobs, offsets, and field maps on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 `dve/` blobs are byte-identical — see [versions](../reference/versions.md)). The evidence is a **shipped, headerless binary blob** — `neuronxcc/dve/dve_bin_gen{2,3,4}/<set>_datapath_table.bin` — read directly with `xxd`/`struct.unpack`, and **validated** against the BIR simulator (`libBIRSimulator`, build-id `f1c6885f`), whose DVE body interprets these same lane words. The firmware unit that turns a lane word into physical mux/ALU signals does **not** ship in this wheel; field positions are therefore reconstructed from the blob's own statistics and cross-gen invariants, and every field carries an explicit confidence tag. Other wheels differ; treat every offset as version-pinned.*

## Abstract

The DVE (Deep Vector Engine, Trainium's vector/data-movement leg) is **microcode-programmed**, not C++-encoded ([arch 1.11](../arch/dve-engine.md)). An opcode integer indexes a 256-entry `opcode_table`; that yields a *fast control row* `R`; the control table at `R` carries the sequencer word; and `R` also indexes one **8-lane datapath block** in `datapath_table.bin`. The datapath block is the last link in that chain — it is the **per-lane ALU configuration** the engine loads to run the op. This page is the byte-exact decode of that blob.

`datapath_table.bin` is a flat, **control-row-indexed** array of fixed **64-byte blocks**, with **no header, no magic, and no offset table** — the whole file is the array. Each block is **8 lanes × one u64-LE word**; lane `L` of block `b` lives at file bytes `[b*64 + L*8 .. +8)`. The block stride is `blob_size / control_row_count = 64` in **every** generation (gen2 8192 B → 128 blocks; gen3/gen4 16384 B → 256 blocks), and `#blocks == #fast-control-rows` exactly. Every block falls into one of **three classes** — *all-zero* (datapath OFF: the op runs on a different functional unit), *all-idle* (datapath in pass-through/bypass: pure data-move), or *active* (the ALU pipeline is engaged). Within an active block the non-idle lanes are **left-packed**, and the count of active lanes `k` is the op's datapath **pipeline depth**.

The lane word itself is a packed bit-field: **OPA** (operand/source-select), **OPB** (operand-B / accumulator-command), **MUX** (datapath route/crossbar), **VALID** (step-present marker — the idle word is *exactly* this bit alone), **MODE** (accumulator/convert role), and **STAGE** (output-lane / crossbar destination). The field positions **relocate up by a fixed +12 bits gen2→gen3** (proven byte-exact by XOR on one op across two gens) and +10 more gen3→gen4, with gen2's fields preserved verbatim in the low bits — a forward-compatible *append*, not a re-layout. Six independent simulator-semantic checks (copy/cast, reciprocal, Max8 vs FindIndex8, MaxPoolSelect, BatchNormStats, Dropout) all corroborate the field roles from the datapath bytes alone.

For reimplementation, the contract is:

- **The geometry** — 64-byte / 8-lane block, keyed by the *fast control row* (not the opcode), `#blocks == #fast-rows`, stride invariant across gens.
- **The three block classes** — all-zero / all-idle / active, and what each says about *which functional unit* runs the op and *how deep* its datapath pipeline is.
- **The lane-word bit map** — OPA / OPB / MUX / VALID / MODE / STAGE positions for gen2, and the +12 / +10-bit cross-gen relocation rule.
- **The simulator validation** — how to confirm a decoded block against the op's behavior in `libBIRSimulator`'s DVE body.

| | |
|---|---|
| **Blob** | `neuronxcc/dve/dve_bin_gen2/default_datapath_table.bin` |
| **Size** | **8192 bytes** (gen2); gen3/gen4 = 16384 bytes |
| **SHA-256 (gen2 default)** | `cccf0da1a55bb5b8495f3173663a78f402b9c2fb00891869e9da126c977eac97` |
| **SHA-256 (gen3 default)** | `603d9fee658662ccd811a4f3749b88f5fa7f503f9158e0554766a545d5c11cbc` |
| **SHA-256 (gen4 default)** | `286a88676ce96845278a792d956d02fc224a4adba6016e16fed21939f480e2ce` |
| **Format** | headerless flat array; block = 64 B = 8 × u64-LE |
| **Index key** | the **fast control row** `R` (`opcode → opcode_table[op] → R`), **not** the opcode |
| **Block count** | gen2 = 128, gen3/gen4 = 256 (== fast-control-row count) |
| **Stride** | `blob_size / control_row_count` = **64 B**, invariant across all three gens |
| **Idle (pass-through) word** | gen2 `0x00100000` (bit 20), gen3 `0x100000000` (bit 32), gen4 `0x40000000000` (bit 42) |
| **Used word width** | gen2 ≈ 27 bit, gen3 ≈ 37 bit, gen4 ≈ 61 bit |
| **Block-class census (gen2 default)** | 13 all-zero, 16 all-idle/pass-through, 99 active |
| **Validator** | `libBIRSimulator.so` (build-id `f1c6885f`) DVE body; BIR `InstDveReadAccumulator` (`libBIR`/`libwalrus`) field names |
| **Table-sets** | per set, its own blob: `default`, `transformer[_fwd/_bwd/_bwd2]`, `saturate` (gen4) |

---

## 1. Block Geometry — Byte-Exact

### The whole file is the array

`datapath_table.bin` has no header. Parsing the gen2 default blob as u64-LE yields `8192 / 8 = 1024` words, grouped into `1024 / 8 = 128` blocks of 8 lanes. Block `b` owns the eight u64 words at indices `[b*8 .. b*8+7]`, file bytes `[b*64 .. b*64+63]`. The same parse on gen3/gen4 yields `16384 / 8 = 2048` words → `256` blocks.

```text
gen2  8192 B → 1024 u64 → 128 blocks × 8 lanes   (657 nonzero words)
gen3 16384 B → 2048 u64 → 256 blocks × 8 lanes   (664 nonzero words)
gen4 16384 B → 2048 u64 → 256 blocks × 8 lanes  (1068 nonzero words)
```

### The index key is the fast control row, not the opcode

A block is **not** addressed by the opcode integer. The chain is `opcode → opcode_table[op] → fast_row R → datapath block R` — the same `R` that indexes the control table ([2.25 opcode table, planned](dve-opcode-table.md); [2.26 control table, planned](dve-control-table.md)). Two facts pin this:

- **`#blocks == #fast-control-rows`, exactly** — 128 in gen2, 256 in gen3/gen4. The table is sized to the control-row space, not to the opcode roster (the default rosters are only 46 / 52 / 59 ops; 8192/46 isn't even integer).
- **No orphan blocks** — every *active* block lands on a *used* control row, and no active block sits on an unused row. The set "control rows not used but datapath active" is empty in all three gens.

> **NOTE —** Op-family pairs that share a control row (e.g. the Arith/Bitvec variants of TensorTensor) therefore share **one** datapath block. The Arith-vs-Bitvec distinction is carried by the opcode + the MODE/STAGE fields, not by a separate block. The ALU *function* (add vs mul vs max vs bitvec) is the BIR opcode; the datapath block only configures *sources, routes, and stages*.

### Stride is invariant

The 64-byte stride is fixed by the 8-lane × u64 block and is identical across all three generations: `8192/128 = 16384/256 = 64`. The gen2→gen3 size doubling (8K→16K) is therefore **more control rows** (128→256, from the gen3 control fast/slow split), **not** more opcodes and **not** wider blocks — the opcode index space is a constant 256 in every gen. gen3→gen4 keeps 16384 B / 256 blocks but lights ~55% more blocks (99→155 active) and **widens each lane word** (≈37→61 bit) to carry the new gen4 op classes `{48, 119, 120, 224..227}`.

> **GOTCHA —** the 8K→16K size jump is **gen2→gen3**, driven by the control-row split — not a gen4 doubling for saturating arithmetic. gen4's change happens at *constant* file size (more active blocks, wider words). Decode each blob against its own generation's idle-word position, never gen2's.

---

## 2. The Three Block Classes

Every block is exactly one of three classes. The class encodes *which functional unit* runs the op. Census on the gen2 default blob:

| Class | Meaning | gen2 count | Example ops (fast row) |
|---|---|---|---|
| **All-zero** | All 8 lanes `0x0` — datapath fully **OFF**; the op runs on a *different* unit (PWP-LUT / RNG / RAM-stream / index-search) | 13 | Reciprocal (r10), Rng (r37), LoadParameterRam (r79), LoadMaskSelect (r79), FindIndex8 (r98), MatchReplace8 (r102) |
| **All-idle / pass-through** | Every active lane == the gen's **idle word**; the datapath is *wired but no-op* — pure data-move (bypass) | 16 | Copy/Cast (r1), Memset (r36), TensorCumulative (r39), StreamTranspose/ExtInst (r105) |
| **Active** | ≥ 1 lane carries a non-idle config word; the ALU pipeline is **engaged** | 99 | TensorTensor / TensorScalar / TensorReduce, Pool, MaxPoolSelect, BatchNorm*, Dropout, Select, ScalarTensorTensor, Max8 |

> **GOTCHA — the all-idle class has two sub-forms.** A pass-through block is either all 8 lanes idle (6 blocks: rows 1, 36, 39, 45, 48, 105) *or* a mix of idle + trailing zero with **no active lane** (10 blocks). Both mean "datapath in bypass". Count them together (6 + 10 = **16**) when comparing against the "pure data-move" op set; a census that only matches *all-8-idle* will report 6 and appear to contradict the roster. The blob's own split is `all8idle=6`, `idle+zero=10`.

### The idle word *is* the VALID bit alone

The idle word is a single set bit, and its position is the generation's VALID marker (§4):

```text
gen2  0x00100000     (bit 20)   — 256 copies in the gen2 default blob
gen3  0x100000000    (bit 32)   — 221 copies
gen4  0x40000000000  (bit 42)   — 233 copies
```

A block of 8 idle words is the datapath **wired but doing nothing** → pass-through (class B). A block of 8 zeros is the datapath **unwired** → bypassed to another unit (class A). The idle-word position climbing 20 → 32 → 42 is precisely the field-widening of §5: new low bits are inserted *below* the VALID marker, pushing it up.

*Anchors: single-bit-set census on each generation's blob.*

---

## 3. Lane Packing — Pipeline Depth

Within an active block the non-idle lanes are **left-packed**: lanes `0..k-1` carry config, lanes `k..7` are the idle word (or zero). The active-lane count `k` is the op's datapath **pipeline depth** — the number of distinct datapath stages it lights:

| `k` | role | ops (gen2 default) |
|---|---|---|
| 0 | idle / bypass | Copy, Cast, StreamShuffle; TensorCumulative; Memset; StreamTranspose/ExtInst |
| 1 | single ALU stage | TensorTensorArith/Bitvec; Max8; TensorTensorScanArith |
| 2 | prep + op (or op + reduce) | TensorScalar*; TensorReduce*; Pool |
| 4 | compare + select + index + writeback | MaxPoolSelect; BatchNormParamLoad |
| 5 | reduction pipeline | TensorReduceAddBf16; TensorReduceMaxBf16 |
| 6 | wide reduction | BatchNormBackProp |
| 7 | compound multi-stage | BatchNormGradAccum; Dropout; MatchValueLoad |
| 8 | full-width fan-in tree | BatchNormStats2; BatchNormAggregate; CopyPredicated; ScalarTensorTensor; SelectReduce |

This `k` tracks op *semantic* complexity (validated in §6): the simplest binary op `a⊕b` lights 1 stage; the BatchNorm reductions / Dropout / select-reduce light all 8 lanes (a fan-out reduction/select tree). It is an independent, byte-derived fingerprint of the op's identity. The left-packing itself is byte-exact; the correlation between `k` and semantic complexity is an interpretation of it.

---

## 4. The Lane Word — gen2 Bit-Field Map

The gen2 lane word is ≈ 27 bits wide (highest set bit over all non-idle words is 26). Per-bit set-counts over the 401 non-idle, non-zero gen2 lane words expose the field boundaries (bit 15 is *never* set inside the MUX field — a hard break; bits 21–26 factor into MODE+STAGE by value):

```text
bit: 00 01 02 03 04 05 06 07 | 08 09 10 | 11 12 13 14 15 16 17 18 19 | 20 | 21 22 23 | 24 25 26
cnt:102 119 120 102 72 111 92 146| 54 77 180|  4  2 128 74  0 73  2 87 84 |274| 14 21 54 |164 94 104
                                                          ^^bit15=0 (field break)
```

The field map (gen2; bit offsets shift +12 in gen3, +22 in gen4; semantics invariant — §5, §8):

| bits | field | width | role | confidence |
|---|---|---|---|---|
| `[0:8]` | **OPA** | 8 | operand-A / source-select / immediate — **not** an ALU-opcode (§7) | HIGH |
| `[8:11]` | **OPB** | 3 | operand-B / accumulator-command sub-select; pairs with OPA as the second source | HIGH |
| `[11:20]` | **MUX** | 9 | datapath route/crossbar; bit 15 hard-zero → really `11:15 ‖ 16:20`; the `0x180`-family = cross-lane fan-in route (§8) | HIGH |
| `[20]` | **VALID** | 1 | "intermediate datapath step present"; the **idle word == this bit alone** (pass-through). 127 active words clear it — those are terminal/output lanes carrying a STAGE tag instead | CERTAIN |
| `[21:24]` | **MODE** | 3 | accumulator / convert role; on BatchNorm, `MODE=3` marks the M2 (Σdev²) lane vs `MODE=0` mean/n lane (§6 V5) | HIGH |
| `[24:27]` | **STAGE** | 3 | pipeline-stage / output-lane (crossbar destination); ranges `0..7` → the 8 lanes; reductions fan to STAGE 7 | HIGH |

Two structural facts pin the boundaries:

- **`bit20` varies independently of STAGE.** Over the 401 words, `STAGE=1` appears with `b20=1` (`0x11`, 58×) *and* `b20=0` (`0x10`, 36×). So `bit20` is a separate VALID bit, not the low bit of STAGE — the independence holds over all 401 words.
- **The idle word `0x100000` is `bit20` alone** — byte-exact. This is the keystone: a "present but no-op" datapath lane.

> **NOTE —** The decode of one lane word in C:
>
> ```c
> /* gen2 lane word (u64-LE). Returns the per-lane ALU config. */
> typedef struct { u8 opa; u8 opb; u16 mux; u8 valid; u8 mode; u8 stage; } dve_lane_t;
>
> dve_lane_t decode_gen2(uint64_t w) {
>     dve_lane_t c;
>     c.opa   = (w      ) & 0xFF;   /* [0:8]   operand/source-select   */
>     c.opb   = (w >>  8) & 0x07;   /* [8:11]  operand-B / acc-command  */
>     c.mux   = (w >> 11) & 0x1FF;  /* [11:20] route/crossbar (4+4)     */
>     c.valid = (w >> 20) & 0x01;   /* [20]    step-present (idle bit)  */
>     c.mode  = (w >> 21) & 0x07;   /* [21:24] accumulator/convert role */
>     c.stage = (w >> 24) & 0x07;   /* [24:27] output-lane / crossbar   */
>     return c;
> }
> /* gen3: same masks, all high fields (>= VALID) shifted +12 (VALID at bit 32). */
> /* gen4: high fields shifted +22 (VALID at bit 42), plus a HIGH section >= bit 48. */
> ```

### Block-class dispatch

```c
/* Classify a 64-byte block (8 lane words) into its functional-unit routing. */
enum dve_class { DVE_OFF, DVE_PASSTHRU, DVE_ACTIVE };

enum dve_class classify_block(const uint64_t lane[8], uint64_t idle_word) {
    int n_zero = 0, n_idle = 0;
    for (int L = 0; L < 8; ++L) {
        if      (lane[L] == 0)         ++n_zero;
        else if (lane[L] == idle_word) ++n_idle;
        else return DVE_ACTIVE;                 /* any non-idle config lane */
    }
    if (n_idle == 0) return DVE_OFF;            /* all 8 zero  -> unit bypass */
    return DVE_PASSTHRU;                        /* idle (+ zero) -> data-move */
    /* idle_word: gen2 0x100000, gen3 0x100000000, gen4 0x40000000000 */
}
```

---

## 5. Cross-Gen Widening — the +12 / +10-Bit Rule

The lane word widens by inserting field *above* the gen2 layout and pushing the VALID/idle marker up. The idle-word position is the marker:

```text
gen2 VALID bit20  (idle 0x100000)
gen3 VALID bit32  (idle 0x100000000)   → +12 bits inserted below VALID
gen4 VALID bit42  (idle 0x40000000000) → +10 more bits
```

The OPA low byte survives across all three gens; the MUX/route field grows; gen4 adds a brand-new HIGH section (bits ≥ 48) for its new op classes. Three ops traced bit-for-bit:

```text
op88 MaxPoolSelect  lane0:  gen2 0x1102000   gen3 0x1100014000     gen4 0x440000014000
op65 TensorTensorArith L0:  gen2 0x10041c    gen3 0x100001420      gen4 0xa00040000001438
op66 TensorReduceArith L1:  gen2 0x10040c    gen3 0x10000140c      gen4 0xa00040000001406
```

**The XOR proof (gen2→gen3, exact +12).** On the BatchNormStats2 block, the mean-lane and M2-lane differ *only* in the MODE field:

```text
gen2  mean 0x071d6000  ^  M2 0x077d6000  = 0x600000 = bits {21,22}  → MODE @ [21:24]
gen3  mean 0x71005b4000 ^ M2 0x77005b4000 = 0x6000000000 = bits {33,34} → MODE @ [33:35]
                                                              ⇒ MODE moved 21→33 = +12, EXACT
STAGE (=7 on every BNStats lane): gen2 [24:27]=7 → gen3 [36:39]=7   = +12
VALID marker:                     gen2 bit20     → gen3 bit32        = +12
```

A field identified purely by value-factoring in gen2 reappears, *semantically identical*, exactly 12 bits higher in gen3, on the same op. That coincidence-proof relationship is what grounds the §4 field map.

> **NOTE — mechanism.** Each DVE generation adds datapath features (more mux modes, the gen4 saturating + new reduce/select ops). Rather than re-pack, the format **appends** new field above the prior VALID marker and relocates VALID to the new top. gen2 fields are preserved verbatim in the low bits — a forward-compatible superset widening, not a re-layout.

---

## 6. Validation Against the BIR Simulator

The simulator (`libBIRSimulator.so`, build-id `f1c6885f`) is the **oracle**: its DVE body interprets these same lane words at execution time, and each op's behavior there is the independent ground truth for a decoded block. Six checks, all pass:

| # | op (block) | datapath signature | simulator behavior | ✓ |
|---|---|---|---|---|
| V1 | Copy/Cast (op70/71, block1) | 8× idle word (pass-through) | straight AP→AP element copy; any dtype cast happens inside the read — **no datapath compute** | ✓ |
| V2 | Reciprocal (op72, block10) | all-zero (datapath OFF) | a PWP (piecewise-poly) LUT op — runs on the activation/PWP unit, *not* the DVE ALU | ✓ |
| V3 | Max8 vs FindIndex8 (op108 b80 / op110 b98) | 1 compare lane vs all-zero | Max8 = running 8-entry max via a `>` compare datapath; FindIndex8 = pure index *search* (no arithmetic) → index unit, datapath OFF | ✓ |
| V4 | MaxPoolSelect (op88, block8) | 4 lanes, STAGE `1,1,1,0` | windowed compare → select-max → select-index → writeback: a 4-stage select pipeline | ✓ |
| V5 | BatchNormStats2 (op97, block41) | 8 lanes, `MODE=3` on lanes 3,7 | two interleaved Welford triples `[n,mean,M2]`; `MODE=3` marks exactly the two M2 (Σdev²) accumulators (high slot of each 4-lane group); the other 6 are `MODE=0` mean/n | ✓ |
| V6 | Dropout (op127, block77) | 7 lanes, STAGE `5,7,7,3,3,3,1` | RNG-mask → threshold-compare → scale → multiply: a genuine multi-stage program (every lane a *different* STAGE, unlike a uniform-STAGE reduction) | ✓ |

The net mapping the simulator confirms: **lane-count = pipeline depth; block-class = functional-unit routing; MODE = accumulator/convert role; STAGE = output-lane/crossbar route.** All six corroborate the op→name binding from the datapath bytes alone.

> **NOTE — compiler-side grounding.** The firmware row-decoder is absent from the wheel, but the BIR instruction layer *names* the operand-config fields the bin-gen compiles **into** these words: `bir::InstDveReadAccumulator` (a DVE instruction whose datapath reads an accumulator source), the `s_dve_read_accumulator_opcode` / `s_dve_read_indices_opcode` rodata selectors (which accumulator / which index stream a step reads), and the `AccumulatorCmd` + `imm1` pair per DVE op, with per-core validators `core_v{2,3,4}::dbg_is_valid_activation_read_accumulator` (= gen2/3/4). These confirm the datapath is configured by an *operand/source select* + a small *command + immediate* — exactly the `OPA[0:8]` (source/imm) + `OPB[8:11]` (cmd) structure — and **not** by an ALU-opcode field. The field *roles* rest on that naming; the exact bit *positions* remain [INFERRED], since no firmware packer ships in the wheel.

---

## 7. The OPA/OPB Fields — What They Are *Not*

`OPA[0:8]` is an operand/source select, **not** an ALU-opcode. Three observations rule out the op-code reading:

- **Adjacent lanes carry consecutive OPA.** TensorScalar lane0 `OPA=0x1c`, lane1 `OPA=0x1d` — an op-code would not increment lane-to-lane; a source/register address does.
- **Unrelated ops share an OPA.** TensorTensorArith (a binary add/mul) and TensorScalar (a different op) *both* carry lane `OPA=0x1c`. An op-code would differ.
- **Complex ops carry varied, distinct per-lane OPAs** (e.g. BNGradAccum lanes `{5,6,6,27,166,27,155}`) — each lane reads a *different* source. Both patterns are source-address-like, neither is op-code-like.

So `OPA[0:8] ‖ OPB[8:11]` together form an 11-bit **operand/source selector** (which datapath register / immediate the lane reads). The ALU *function* is the BIR opcode + the MODE/STAGE fields — which is why op-family pairs sharing a control row share the datapath block but the opcode disambiguates them.

### The MUX/route field `[11:20]`

MUX value distribution (gen2 active lanes): `0x000` (186, no route), `0x004` (89, basic), a `0x180`-family `{0x180,0x184,0x1a8,0x1ac,...}` (≈70), and `0x028/0x02c`. Bit 15 (= MUX bit 4) is never set → MUX is really `11:15 ‖ 16:20`, a split route field. The `0x180`-family (high MUX bits 18,19) marks **cross-lane / fan-in routing**: it lights on BatchNormStats2 (all 8 lanes `0x1ac`), BNGradAccum (`0x184`), MatchValueLoad / ScalarTensorTensor / SelectReduce (`0x180`) — the ops that read across lanes.

> **CAVEAT —** BatchNormAggregate (a reduction) carries `MUX=0x0`, so the `0x180` bit is a *specific cross-lane operand-broadcast route*, not a generic "is-reduction" flag. It lights when a lane sources from a different lane/column, which Aggregate (already pre-reduced inputs) doesn't need. Five fan-in ops carry the bit; the single non-match is exactly the one that needs no broadcast.

---

## 8. Worked Decode — One Real 64-Byte Block

`xxd` of block 41 (BatchNormStats2), file byte `41*64 = 0xA40`, gen2 default blob:

```text
00000a40: 0060 1d07 0000 0000  0060 1d07 0000 0000   .`.......`......
00000a50: 0060 1d07 0000 0000  0060 7d07 0000 0000   .`.......`}.....
00000a60: 0060 1d07 0000 0000  0060 1d07 0000 0000   .`.......`......
00000a70: 0060 1d07 0000 0000  0060 7d07 0000 0000   .`.......`}.....
```

Read as u64-LE, six lanes are `0x071d6000` and lanes 3 & 7 are `0x077d6000`. Decode each with `decode_gen2` (§4):

| lane | word | OPA | OPB | MUX | VALID | MODE | STAGE | role |
|---|---|---|---|---|---|---|---|---|
| 0 | `0x071d6000` | 0x00 | 0 | `0x1ac` | 1 | 0 | 7 | n/mean accumulator |
| 1 | `0x071d6000` | 0x00 | 0 | `0x1ac` | 1 | 0 | 7 | n/mean accumulator |
| 2 | `0x071d6000` | 0x00 | 0 | `0x1ac` | 1 | 0 | 7 | n/mean accumulator |
| **3** | `0x077d6000` | 0x00 | 0 | `0x1ac` | 1 | **3** | 7 | **M2 (Σdev²) accumulator** |
| 4 | `0x071d6000` | 0x00 | 0 | `0x1ac` | 1 | 0 | 7 | n/mean accumulator |
| 5 | `0x071d6000` | 0x00 | 0 | `0x1ac` | 1 | 0 | 7 | n/mean accumulator |
| 6 | `0x071d6000` | 0x00 | 0 | `0x1ac` | 1 | 0 | 7 | n/mean accumulator |
| **7** | `0x077d6000` | 0x00 | 0 | `0x1ac` | 1 | **3** | 7 | **M2 (Σdev²) accumulator** |

Reading the configuration: all 8 lanes are active (`k=8`, full-width fan-in) with `MUX=0x1ac` (cross-lane fan-in route) and `STAGE=7` (all fan into the last output column). `MODE=3` on exactly lanes 3 and 7 — the high slot of each 4-lane group — marks the two M2 accumulators; the other 6 lanes (`MODE=0`) are the n/mean accumulators. This is the two-interleaved-Welford-triple structure the simulator's BatchNorm body computes (§6 V5). A reader can now decode any block to its per-lane ALU configuration.

Three more blocks for reference, end-to-end (`opcode → opcode_table[op] → fast_row → block`):

```text
op65 TensorTensorArith → row3 → block3 @0xC0  (1-lane binary op)
  lane0 0x0010041c : OPA=0x1c OPB=4 MUX=0x000 VALID MODE=0 STAGE=0
  lane1..7 idle (0x100000)
  → one stage reads operand 0x1c, result to output lane 0.

op67 TensorScalarArith → row2 → block2 @0x80  (2-lane tensor⊕scalar)
  lane0 0x0211441c : OPA=0x1c OPB=4 MUX=0x028 VALID MODE=0 STAGE=2  (scalar broadcast)
  lane1 0x0010051d : OPA=0x1d OPB=5 MUX=0x000 VALID MODE=0 STAGE=0  (the op stage)
  → broadcast scalar (STAGE 2, MUX 0x28), then apply (STAGE 0).

op88 MaxPoolSelect → row8 → block8 @0x200  (4-lane select pipeline)
  lane0 0x01102000 : MUX=0x004 VALID STAGE=1     lane1 0x01102000 : MUX=0x004 VALID STAGE=1
  lane2 0x01002000 : MUX=0x004 VALID=0 STAGE=1   lane3 0x00100480 : OPA=0x80 OPB=4 STAGE=0
  lane4..7 idle → 3 compare/select stages + 1 writeback.
```

> **QUIRK —** In MaxPoolSelect lane2 the VALID bit (20) is *clear* while STAGE=1 is set: this is a terminal/intermediate lane that carries a STAGE route instead of the step-present marker. 127 active words across the gen2 blob do this — VALID is "intermediate step present", not a universal "lane in use" flag.

---

## 9. Table-Sets and Saturate

Each table-set ships its **own** `datapath_table.bin` — the set is independently compiled, not a base + overlay. Differences are localized to the datapath payload:

- **transformer vs default (gen2):** 588 of 1024 words differ across 93 blocks — a large diff, because the transformer set has a different opcode roster, so each op's fast control row is repacked and nearly every block shifts.
- **saturate vs default (gen4):** 134 lane words differ, all in blocks 143..182 (29 blocks) — the gen4 high-numbered ops `{op227@143, op225@170, op234@180}` plus the intermediate pipeline-stage rows their programs traverse. The slow rows are unchanged. The XOR deltas are large and varied (65 distinct, e.g. `0x0001800001540000`) — saturate is a full multi-field datapath re-config (saturating clamp + altered route), not a single "saturate flag" bit.

---

## 10. Evidence Anchors and Limits

**Read byte-exact off the shipped blobs:** the 64-byte / 8-lane block and its stride `blob_size / control_row_count` = 64, invariant across generations; `#blocks == #fast-control-rows` (128 / 256 / 256) with every active block landing on a used row; the three block classes; the idle word as a single VALID bit (gen2 bit 20 / gen3 bit 32 / gen4 bit 42), 256 of them in the gen2 default blob; the left-packing of lanes and `k` as pipeline depth; the exact +12-bit gen2→gen3 field relocation (via the BatchNormStats XOR) and +10 more gen3→gen4; the gen2→gen3 size doubling as a control fast/slow split (128→256 rows) against a constant 256-entry opcode index space; and saturate as a datapath-payload variant scoped to blocks 143..182.

**Interpretation resting on cross-checks rather than a decoder:** the §4 field *roles* (OPA source-select, OPB command, MUX route, VALID, MODE accumulator-role, STAGE output-lane), the six simulator-semantic validations, MUX `0x180` as the cross-lane fan-in route, and the BIR `InstDveReadAccumulator` / `AccumulatorCmd` / `imm1` naming that corroborates the source-select + command structure.

**[INFERRED]:** the exact bit *boundaries* between OPB, MUX, and MODE — factored from value distributions plus the +12-bit cross-gen shift, not from a decompiled firmware packer — and the gen4 HIGH-section (bits ≥ 48) field assignment.

**Open, and not answerable from this wheel:** the physical mux/ALU each bit drives; the slow-program datapath usage of blocks 143..182; and gen4 HIGH-section semantics for the new op classes. The firmware unit that turns a lane word into mux/ALU signals does not ship here.

---

## Cross-References

- [DVE Engine — Microcode-Table Architecture (1.11)](../arch/dve-engine.md) — the engine view: table-set roster, schema, opcode→name Rosetta, indexed-lookup flow.
- [DVE Opcode Table (2.25, planned)](dve-opcode-table.md) — `opcode → opcode_table[op] → fast_row` (the Rosetta this page's block key consumes).
- [DVE Control Table (2.26, planned)](dve-control-table.md) — the sequencer word paired with each datapath block.
- [DVE Engine Migration — gen2/3/4 (2.28, planned)](dve-engine-migration.md) — the cross-generation deltas in full.
- [BatchNorm-Family Encoding](batchnorm-encoding.md) — the BIR-level encoding of the BatchNorm ops decoded here (V5).
- [Pool / TensorReduce / Reciprocal / Iota Encoding](pool-reduce-encoding.md) — the BIR encoding of Pool / Reduce / Reciprocal (V2, V4).
- [DVE Search & Datamove Encoding — Max8 / FindIndex8](dve-search-encoding.md) — Max8 vs FindIndex8 at the BIR level (V3).
- [Build & Version Provenance](../reference/versions.md) — the pinned build and the cp310/cp311/cp312 parity argument.
- BIR simulator (Part 7, planned) — the DVE body that interprets these lane words at execution time (the validation oracle).
