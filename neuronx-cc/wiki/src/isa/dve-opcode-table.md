# DVE On-Device Microcode — `opcode_table`

> *Every byte offset, size, and hash on this page is read from one build: `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; the `cp310`/`cp311`/`cp312` wheels ship **byte-identical** `dve/` blobs — the table is a property of the toolchain version, not the Python ABI). The blobs analyzed live under `neuronxcc/dve/dve_bin_gen{2,3,4}/<set>_opcode_table.bin` inside the wheel. Unlike the [PE matmul page](pe-matmul-encoding.md), the evidence here is not a libwalrus encoder — it is a **shipped on-device microcode blob** that the compiler packages without interpreting. Treat every offset as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte decode of the DVE **`opcode_table`** blob — the first lookup the on-device DVE sequencer performs when it receives an opcode. It is the on-silicon counterpart of the architecture view in [1.11 the DVE engine](../arch/dve-engine.md): that page describes the indexed-lookup *flow*; this page proves the **format of the first table in that flow** from the raw bytes.

`opcode_table.bin` is a **headerless, 256-slot, direct-opcode-indexed array**. There is no magic, no length prefix, no offset table — the whole file *is* the array, and slot `k` is the descriptor for DVE opcode integer `k`. The entry stride is `filesize / 256`: **8 bytes** (gen2, `u64`-LE) or **4 bytes** (gen3/gen4, `u32`-LE). Each nonzero entry is a packed selector into the *control* table(s): a low **fast-row** field and a high **slow-program** field. A zero entry means the opcode is **disabled** in this table-set. The set of nonzero slots equals the table-set's `ops[]` roster (plus slot 0, the idle/reset descriptor), to the integer.

The bar for this page: a reader can **parse any `opcode_table.bin` and map every opcode to its microcode rows** — compute the stride, read slot `k` at byte `k·stride`, split it into `(fast_row, slow_row)`, and know which control rows the on-device sequencer will fetch. Every claim carries a confidence tag (CONFIRMED = exact from the shipped bytes + JSON; STRONG = cross-table-consistent; INFERRED = read off the layout, no shipped decoder; SPECULATIVE). The byte values quoted are `xxd`/`struct.unpack` of the shipped blob; none is fabricated. The **payload** the rows point at — the control-word and datapath-word field maps — is the scope of [2.26 control table](dve-control-table.md) *(planned)* and [2.27 datapath table](dve-datapath-table.md) *(planned)*; this page decodes only the opcode→row map.

## At a glance

| | gen2 | gen3 | gen4 |
|---|---|---|---|
| File (`default` set) | `dve_bin_gen2/default_opcode_table.bin` | `dve_bin_gen3/…` | `dve_bin_gen4/…` |
| File size | **2048 B** | **1024 B** | **1024 B** |
| Entry stride = size/256 | **8 B** (`u64`-LE) | **4 B** (`u32`-LE) | **4 B** (`u32`-LE) |
| Index slots | 256 | 256 | 256 |
| Nonzero slots (`default`) | **51** = 50 + idle | **53** = 52 + idle | **60** = 59 + idle |
| JSON `ops[]` count (`default`) | **46** | **52** | **59** |
| Bit-split (low \| high) | `[0:7]` row \| `[7:]` slow-idx | `[0:8]` fast \| `[8:]` slow | `[0:8]` fast \| `[8:]` slow |
| Max entry value (`default`) | `0x9cf` | `0x3008` | `0x3010` |
| Slot-0 descriptor (all sets) | `0x180` | `0x600` | `0x600` |

```
opcode_table[k]  =  *(uintW*)(blob + k * stride)        # W = 8 (gen2) | 4 (gen3,4)

entry == 0          → opcode k DISABLED in this set
gen2  (u64):  row  = V & 0x7F ;  slow_idx = V >> 7
gen3/4 (u32): fast = V & 0xFF ;  slow_row = V >> 8        # slow == 0 → fast-only op
```

The opcode counts mirror [1.11](../arch/dve-engine.md)'s 46→52→59 roster growth exactly: gen2 `default` `ops[]` = 46 (the blob adds slot 0 + four residents `{147,240,241,242}` → 51 nonzero; of those, 147/240 are promoted into the gen3/4 roster and 241/242 are gen2-only — §7); gen3 = 52 (+idle = 53); gen4 = 59 (+idle = 60). CONFIRMED.

`default_opcode_table.bin` hashes (sha256, cp310):

```
gen2  9888f54e5b5b951b62f2bebc3dd4335d64ed25b12ffba6a0e25396bf07d8dc43
gen3  894b52a1ed661ada99860783db5077e853cd05e900354cb0a0ad3918741ad0a1
gen4  3a796d2cc413247d8b32e1d1ba4cb2a75f209939604b40838f495286e9a57623
```

The gen3 blob is `md5`-identical across the cp310/cp311/cp312 wheels (`33215204be9e838d41a4af59584f0c4f`). CONFIRMED.

---

## 1. Indexing — direct opcode-indexed, no header

The blob is a flat array keyed by the raw DVE opcode integer. No header, no magic, no offset table, no length prefix. Three proofs, all from the bytes.

**(a) `size == 256 · stride`, exactly.** `2048 = 256·8` (gen2), `1024 = 256·4` (gen3/gen4). No bytes are left for a header — the file is wholly the array. CONFIRMED.

**(b) Nonzero slots == the JSON `ops[]` roster (+ idle slot 0), zero off-by-one.** Parsing gen3 `default` as 256 `u32`-LE, the nonzero indices are

```
{0, 65,66,67,68,69,70,71,72,73, 77,78, 81,82,83,84, 88, 94, 96,97,98,99,100,101,102,
 105,106,107,108,109,110,111, 114, 127, 130,131,132, 142, 147,148, 152,153,154,155,
 157,158, 188, 229,230, 232,233,234, 240}
```

The set `{65..240}` is exactly gen3 `default` `ops[]` (52 ints, [1.11](../arch/dve-engine.md)); index 0 is the extra idle/reset slot. `nonzero − ops = {0}`; `ops − nonzero = {}`. CONFIRMED — re-derived this turn:

```
gen2: |ops|=46 |nonzero|=51   nonzero−ops = {0,147,240,241,242}   ops−nonzero = {}
gen3: |ops|=52 |nonzero|=53   nonzero−ops = {0}                   ops−nonzero = {}
gen4: |ops|=59 |nonzero|=60   nonzero−ops = {0}                   ops−nonzero = {}
```

gen2's four extra nonzero slots `{147,240,241,242}` are *residents* — nonzero but absent from the 46-op gen2 roster (§7). At gen3/gen4, 147 and 240 are **promoted into the `ops[]` roster** (so they are rostered there, not residents) while 241/242 are zeroed; gen3/gen4 therefore carry no residents and their nonzero set is exactly `ops[] + idle` (§7 CORRECTION, [2.28](dve-engine-migration.md)). If the blob were header+offset, the nonzero positions would not coincide with the raw opcode integers — they do, to the byte.

**(c) Stride proof by byte address (gen3 `default`).** Opcode 65 (`TensorTensorArithOp`) must live at `65·4 = 260 = 0x104`:

```
$ xxd -s 0x104 -l 4 dve_bin_gen3/default_opcode_table.bin
00000104: 0830 0000     →  0x00003008  →  fast 0x08, slow 0x30  ✓
$ xxd -s 0x124 -l 4 …                # opcode 73 Memset @ 73·4 = 0x124
00000124: 3006 0000     →  0x00000630  →  fast 0x30=48, slow 6  ✓
$ xxd -s 0x3a8 -l 4 …                # opcode 234 SelectReduce @ 234·4 = 0x3A8
000003a8: 7d00 0000     →  0x0000007d  →  fast 0x7d=125, slow 0 ✓
```

Every opcode resolves at `offset = opcode · stride`. The index *is* the opcode. CONFIRMED.

> **GOTCHA — only the opcode_table is opcode-keyed.** The descriptor it yields is a *control row*, and the sibling `control_*`/`datapath` tables are keyed by that **row**, not by the opcode (the datapath block index equals `fast_row`, [2.27](dve-datapath-table.md) *(planned)*). The opcode→row indirection happens exactly once, here.

## 2. Entry stride — `filesize / 256`

```
stride = filesize / 256          # 256 = the full 8-bit opcode index space
  gen2 : 2048 / 256 = 8 B  (u64-LE)
  gen3 : 1024 / 256 = 4 B  (u32-LE)
  gen4 : 1024 / 256 = 4 B  (u32-LE)
```

> **CORRECTION — divide by 256, not by the opcode count.** A tempting wrong model is `filesize / opcode_count`. gen2 `default` has 46 ops, and `2048 / 46 = 44.5` — not an integer, so the model is false. The table is sized to the **full 256-slot index space**, most of which is zero (disabled opcodes). `opcode_count` tells you how many slots are *nonzero* (membership), never the stride. CONFIRMED — the only divisor that yields an integer stride consistent with the byte-address proof of §1(c) is 256.

**The gen2→gen3 halving (2048→1024) is a width change, not a row-count change.** gen2's `u64` entries are wasteful: across the gen2 `default` set the maximum entry value is `0x9cf` (12 bits), and **every** entry's bytes `[2..7]` are `0x00` — verified this turn (`gen2 entries with bytes[2..7] nonzero: []`). gen3 re-packed the same 256 slots into `u32` and halved the file. Same index space, same information, narrower word. CONFIRMED.

## 3. The bit-split — forced, not chosen

The split between the low control-row field and the high slow field is not a convention; only **one** split position keeps every entry on a valid, *used* control row. This is an exhaustive in-range test over the shipped bytes, not a guess.

**gen2 `default` (50 nonzero non-idle entries; gen2 `control_table` has 128 rows):**

```
split @ bit 8  (row = V & 0xFF):  7 entries land on row ≥ 128 → OUT OF RANGE
    op0  V=0x180 → row 128      op73  V=0x1a4 → row 164
    op77 V=0x1a5 → row 165      op102 V=0x9cf → row 207
    op105 V=0x5cf → row 207     op106 V=0x281 → row 129    op159 V=0x180 → row 128
split @ bit 7  (row = V & 0x7F):  0 entries out of range; ALL land on a used row → VALID, unique
⇒ gen2 split is BIT 7:  control_row = V & 0x7F ;  slow_idx = V >> 7        CONFIRMED — forced
```

**gen3 / gen4 `default` (`control_fast` and `control_slow` are each 256-row tables):**

```
split @ bit 8  (fast = V & 0xFF, slow = V >> 8):
    gen3: 53 nonzero — all fast rows used; 16 entries with slow ≠ 0, all on used slow rows → VALID
    gen4: 60 nonzero — all fast rows used; 18 entries with slow ≠ 0 → VALID
⇒ gen3/4 split is BIT 8:  fast_row = V & 0xFF ;  slow_row = V >> 8         CONFIRMED
```

**Why the split moved 7→8.** gen2 had a 128-row combined control table, so 7 bits address it and the spare top bits hold the proto-slow index. gen3 grew control to a 256-row `control_fast_table` (needs the full low byte) and gave the slow program its own 256-row `control_slow_table` (the next byte). The split tracks the control-table row count exactly. CONFIRMED — the schema change is the one fixed in [1.11](../arch/dve-engine.md) (`dve_table_keys` goes from 3 to 4 keys at gen3).

## 4. The cross-generation slow-selector invariant

The gen2 high field (`V>>7`) and the gen3 high field (`V>>8`) are the **same logical field** — the slow-program selector — with gen3 having doubled the slow-table granularity. Proof: for every opcode that is slow in both gen2 and gen3 `default`, `gen3_slow == 2 · gen2_slow`, exactly. Re-derived this turn:

```
op    gen2 (V>>7)   gen3 (V>>8)   ratio
  0        3             6         ×2     (idle slot)
 73        3             6         ×2     Memset
 77        3             6         ×2     Rng
102       19            38         ×2     LoadParameterRam
105       11            22         ×2     LoadMaskSelect
106        5            10         ×2     StreamShuffle
107        2             4         ×2     StreamTranspose
130        2             4         ×2     TransposeBatchNormStats2
131        2             4         ×2     TransposeTensorReduceArithOp
132        2             4         ×2     TransposeTensorReduceBitvecOp
147        2             4         ×2     TransposeTensorScalarArithOp
240        6            12         ×2     ExtendedInst
                                          → 12 / 12 EXACT
```

A coincidence this clean across 12 ops at this many distinct values is not plausible: the high field is the same selector, scaled ×2 when gen3 split control into a 256-row slow table (gen2 packed slow in 7 spare bits of a 128-row combined table). This is an arithmetic fact over the shipped bytes, independent of any decompiled decoder. CONFIRMED.

> **NOTE — the slow field's exact role is INFERRED past the ×2 scaling.** The ×2 invariant shows it indexes the `control_slow` table. Whether the on-device sequencer further indirects (e.g. uses it as a program *base* rather than a row index) cannot be verified from this wheel, because the unit that *decodes* a slow row into datapath signals is firmware/libtpu-side and does not ship here (§6). The opcode_table *format* is fully decoded; the row payload is [2.26](dve-control-table.md)/[2.27](dve-datapath-table.md) *(planned)*.

## 5. Byte layout & a worked decode

### Slot layout

| Field | gen2 (`u64`-LE, 8 B) | gen3/gen4 (`u32`-LE, 4 B) | Meaning | Confidence |
|---|---|---|---|---|
| Low row | bits `[0:7]` (`V & 0x7F`) | bits `[0:8]` (`V & 0xFF`) | `control[_fast]` row — the inline (single-cycle) program | CONFIRMED |
| High slow | bits `[7:]` (`V >> 7`) | bits `[8:]` (`V >> 8`) | slow-program index (`>0` ⇒ multi-cycle / iterative op) | CONFIRMED |
| Upper pad | bits `[12:64]` always 0 | — | gen2 `u64` waste (max value `0x9cf`) | CONFIRMED |
| Whole entry | `== 0` | `== 0` | opcode **disabled** in this set | CONFIRMED |

### Reference parser (annotated C)

```c
/* opcode_table.bin: headerless, 256 slots, slot k = descriptor for DVE opcode k.
 * stride is read from the file size, never from the opcode count.            */
typedef struct { uint8_t fast_row; uint16_t slow; int present; } dve_desc_t;

static int dve_opcode_table_parse(const uint8_t *blob, size_t size,
                                  dve_desc_t out[256])
{
    if (size != 2048 && size != 1024) return -1;       /* only two legal sizes */
    const size_t stride = size / 256;                  /* 8 (gen2) | 4 (gen3,4) */
    const int    gen2   = (stride == 8);

    for (int k = 0; k < 256; ++k) {
        const uint8_t *p = blob + (size_t)k * stride;  /* DIRECT index: no offset table */
        uint64_t v = 0;
        for (size_t b = 0; b < stride; ++b)            /* little-endian assemble */
            v |= (uint64_t)p[b] << (8 * b);

        out[k].present = (v != 0);                     /* zero ⇒ opcode disabled here  */
        if (gen2) {                                    /* 128-row control table → 7-bit row */
            out[k].fast_row = (uint8_t)(v & 0x7F);
            out[k].slow     = (uint16_t)(v >> 7);
        } else {                                       /* 256-row control_fast → 8-bit row */
            out[k].fast_row = (uint8_t)(v & 0xFF);
            out[k].slow     = (uint16_t)(v >> 8);
        }
    }
    return 0;   /* slot 0 = engine idle/reset descriptor (always present, never emitted) */
}
```

### Worked example — three opcodes, gen3 `default`

```
opcode 70 "Copy"               @ 70·4  = 0x118
    xxd: 20 00 00 00  →  V = 0x00000020
    fast = 0x20 & 0xFF = 32 ;  slow = 0x20 >> 8 = 0   → fast-only, control_fast[32]

opcode 65 "TensorTensorArithOp" @ 65·4 = 0x104
    xxd: 08 30 00 00  →  V = 0x00003008
    fast = 0x08 & 0xFF = 8  ;  slow = 0x3008 >> 8 = 0x30 = 48  → multi-cycle:
                                control_fast[8] AND control_slow[48]
    (op81 TensorTensorBitvecOp carries the IDENTICAL descriptor 0x3008 → same rows;
     the arith-vs-bitvec distinction is carried at the ALU-lane level, not by a row.)

opcode 102 "LoadParameterRam"   @ 102·4 = 0x198
    xxd: 45 26 00 00  →  V = 0x00002645
    fast = 0x45 = 69 (an idle stub fast row) ;  slow = 0x26 = 38
    → the canonical SLOW-PATH op: opcode_table parks it on an idle fast stub and routes
      all real work through slow_row 38. (op105 LoadMaskSelect shares fast stub 69, slow 22.)
```

So opcode 65 ⇒ `(fast 8, slow 48)`; the engine fetches `control_fast[8]`, then (because `slow ≠ 0`) `control_slow[48]`, then the datapath block keyed by `fast = 8`. That is the full opcode_table half of the [1.11](../arch/dve-engine.md) lookup flow; the rows it lands on are decoded in [2.26](dve-control-table.md)/[2.27](dve-datapath-table.md) *(planned)*.

## 6. The compiler ships this blob opaquely

The on-device `opcode_table.bin` is packaged into the executable by the compiler **without the compiler ever bit-decoding a row**. Two binary-derived facts establish this.

**(a) The string `opcode_table` appears in no `.py` and no `.so` text — only in JSON and the wheel manifest.** A whole-wheel grep over the cp310 tree finds `opcode_table` only in the three `dve_info.json` files (as a string *value*) and in the `dist-info/RECORD` packaging manifest:

```
$ rg -l 'opcode_table' <cp310-wheel-tree>/   # excluding .bin
  …/neuronxcc/dve/dve_bin_gen2/dve_info.json
  …/neuronxcc/dve/dve_bin_gen3/dve_info.json
  …/neuronxcc/dve/dve_bin_gen4/dve_info.json
  …/neuronx_cc-…dist-info/RECORD              # the manifest only
```

The literal never occurs in any Python source or any shared-library `.text`. CONFIRMED — verified this turn.

**(b) `dve_info.json` references the blob by filename, beside an integer `ops[]` roster that equals the blob's nonzero set.** Each table-set is one JSON object: `{name, opcode_table, control_fast_table, control_slow_table, datapath_table, ops[]}`. The four table fields are *filenames*; `ops[]` is a flat integer list. The compiler-side DVE pass (`neuronxcc::backend::LowerDVE::fillAllDVEInfos`, pinned at `libwalrus.so` `@0x116f9d0` in the libwalrus disassembly — STRONG, the `.so` is not retained in this repo so the address is cited, not re-derived this turn) walks `dve_table_keys`, reads each table's filename, and deserializes the referenced `.bin` into a `std::vector<unsigned char>` via the `nlohmann …, std::vector<unsigned char>` `from_json` template — i.e. as an **opaque byte vector** for embedding into the NEFF. The only opcode-level logic compiler-side is *membership*: `LowerDVE::checkMissingOpcodes` (`@0x116b260`) fatals with *"dve_info.json is missing a DVE opcodes table that contains union of: …"* if the chosen set's `ops[]` (the JSON int list, a `DenseSet<u32>`) does not cover every DVE opcode the module emits. That assertion reads the JSON `ops[]`, never the `.bin` rows.

> **NOTE — opaque-ship is the reason field semantics are firmware-side.** The compiler treats `opcode_table.bin` as bytes to copy, not a structure to read. The unit that turns `(fast_row, slow_row)` into datapath mux/ALU signals — the row *decoder* — is on the silicon (firmware/libtpu side) and does **not** ship in this wheel. Consequently this page's **format** claims (stride, index scheme, bit-split, membership) are CONFIRMED from the bytes + JSON, while the **meaning** of a row is INFERRED from the cross-gen ×2 invariant (§4) and the architecture page, not from a decompiled decoder. A reimplementer reproducing the compiler need only ship the blob verbatim and assert `ops[]` coverage; a reimplementer reproducing the *engine* must additionally decode the rows ([2.26](dve-control-table.md)/[2.27](dve-datapath-table.md) *(planned)*).

## 7. Table-set membership, residents, and the idle slot

**A table-set is a complete, independently-compiled microcode image.** Its `opcode_table` is the per-set opcode→row map; membership and row compaction differ per set. Verified diffs (gen2 `default` → `transformer`):

```
DROPPED (entry → 0x0):  {88 MaxPoolSelect, 108 Max8, 109 MatchValueLoad,
                         110 FindIndex8, 111 MatchReplace8}     # max/index family off
ADDED   (0x0 → nonzero): {138, 139, 143, 154, 155, 230}         # bf16-fused + cache ops on
```

Ops present in both sets carry **different** row numbers, because each set compacts its own control/datapath tables — the row number is set-local; the opcode is global. CONFIRMED this turn.

**`saturate` (gen4 only) is a datapath-payload variant, not an opcode-map variant.** Its `opcode_table` differs from gen4 `default` in exactly **2 of 256** entries — op225 (`SparsityCompressTag`, fast 170→171) and op234 (`SelectReduce`, fast 180→181):

```
gen4 default vs saturate opcode_table diffs: 2
    op225: 0xaa → 0xab     op234: 0xb4 → 0xb5
```

The opcode map is essentially identical; the divergence is in the `datapath` blobs (clamp vs wrap). CONFIRMED — matches [1.11](../arch/dve-engine.md)'s *"`saturate` is not an alias of `default`"* QUIRK.

**Slot 0 is the engine idle/reset descriptor, invariant across every set in a generation.** It is *not* a real opcode (opcode 0 is never emitted) — it is the default/reset program pointer:

```
gen2 slot-0 = 0x180   (all 5 sets)     row 0, slow_idx 3
gen3 slot-0 = 0x600   (all 5 sets)     fast 0, slow 6
gen4 slot-0 = 0x600   (all 3 sets)     fast 0, slow 6
```

CONFIRMED this turn. In gen2, slot-0's descriptor `0x180` is byte-identical to op159 `EngineNop` — the NOP op literally points at the reset program; gen3 dropped `EngineNop`, so `0x600` has no named twin there.

**gen2 residents `{147, 240, 241, 242}` — two promoted, two gen2-only.** Beyond its 46-op JSON roster, gen2's `opcode_table` carries four fixed nonzero entries: 147 = `0x102` (`TransposeTensorScalarArithOp`), 240 = `0x369` (`ExtendedInst`), 241 = `0x074`, 242 = `0x077`. These four split into two classes at gen3:

- **147 and 240 are PROMOTED into the gen3/gen4 JSON `ops[]` roster** — on gen3 slot 147 = `0x0410` and slot 240 = `0x0C68`, both **roster members**, not residents (gen4: 147 = `0x0420`, 240 = `0x0C6F`). So on gen3/gen4 they are ordinary rostered ops, and the gen3/gen4 nonzero set = `ops[]` + idle for them.
- **Only 241 and 242 are gen2-decode-table-only** — `0x074` (control row 116) / `0x077` (control row 119); gen3/gen4 **zero** these slots (`0x00000000`). They are decode-resident on gen2 but never compiler-emittable on any gen, and become real compiler ops on **other engines** at gen3 (241 = `DmaGatherTranspose` on DMA/gather, 242 = `NonzeroWithCount` on Pool). The slot values `0x074`/`0x077` are control **rows** 116/119, not opcodes.

CONFIRMED that all four entries exist on gen2; the engine-migration reconcile and the 241/242 names are resolved in [2.28](dve-engine-migration.md).

> **CORRECTION (refined by [2.28](dve-engine-migration.md)) — not all four are "gen2-only", and gen3/4 do not carry "zero residents" for all four.** An earlier reading of this section called `{147, 240, 241, 242}` all "gen2-only residents" and said gen3/gen4 carry zero residents. Binary re-check ([2.28 §2](dve-engine-migration.md), `struct.unpack` of each gen's blob): **147 and 240 are PROMOTED into the gen3/gen4 `ops[]` roster** (they are rostered ops there, not residents); only **241 and 242** are gen2-decode-table-only (zeroed on gen3/gen4). The "gen2-only for all four" and "gen3/4 zero residents" sub-claims are corrected here; the residents exist exactly as found, but their gen3/4 fate differs by op. The 241/242 names — earlier marked INFERRED ("extended-op helper candidates") and read as opcode `0x74` — are resolved in [2.28](dve-engine-migration.md): `0xF1`=`DmaGatherTranspose`, `0xF2`=`NonzeroWithCount`, and `0x074` is the slot **value** (control row 116), not opcode `0x74`. CONFIRMED.

> **GOTCHA — opcode_table rows ≠ the wire bundle word `0x10NN`.** Two different "opcode→number" structures share the opcode integer as a hinge but are otherwise unrelated. The 16-bit **bundle header word** `0x10NN` (`NN` = opcode byte, `0x10` = `inst_word_len` for the 64-byte bundle; [PE matmul page](pe-matmul-encoding.md) `setupHeader`) is what the compiler **emits** into each instruction. The `opcode_table` entry is a **control-row selector** the on-device sequencer uses to **decode** a received opcode. J27's word low byte equals the `opcode_table` index, but its high byte (`0x10`, a length) has no bearing on the fast/slow rows. The opcode integer is the hinge; encode and decode are different layers. The 0xF1/0xF2 residency reconciliation is [2.28 DVE engine migration](dve-engine-migration.md) *(planned)*.

## 8. Confidence summary

**CONFIRMED** (byte-exact from the shipped blobs + JSON):
- Headerless, 256-slot, direct opcode-indexed array; opcode `k` at byte `k·stride`.
- `stride = filesize/256` = 8 B (gen2, `u64`-LE) / 4 B (gen3,gen4, `u32`-LE); not `filesize/opcode_count`.
- `entry == 0` ⇒ opcode disabled; nonzero ⇒ enabled (nonzero set == JSON `ops[]` + idle, to the integer).
- Bit-split **forced**: gen2 `[0:7]` row / `[7:]` slow-idx; gen3/4 `[0:8]` fast / `[8:]` slow.
- gen2 `slow_idx == gen3 slow/2` over all 12 shared slow ops (exact) ⇒ same logical field.
- Table-set = full image; disabled ops zeroed; per-set row recompaction; `saturate` ≈ `default` (2/256 diff).
- Slot-0 invariant idle/reset descriptor (`0x180` gen2 / `0x600` gen3,4) across all sets.
- The compiler ships the blob opaque: `opcode_table` appears in no `.py`/`.so` text, only in JSON + the manifest.

**STRONG**: control-row sharing by arith/bitvec op-family (one sequencer program per family); the `LowerDVE` loader address `@0x116f9d0` and the membership-assert at `@0x116b260` (from the libwalrus disassembly; the `.so` is not retained here, so cited not re-derived).

**INFERRED**: the names of gen2-only residents 241/242; the slow field's precise role past indexing `control_slow` (the firmware row decoder does not ship here).

**SPECULATIVE / out of scope**: the control-word and datapath-word bit-field semantics — [2.26](dve-control-table.md)/[2.27](dve-datapath-table.md) *(planned)*.

## Cross-References

- [1.11 — The DVE Engine](../arch/dve-engine.md) — the architecture this blob implements: the table-set roster, the per-gen schema, the 46→52→59 opcode growth, and the indexed-lookup flow this page decodes the first table of.
- [2.26 — DVE Control Table](dve-control-table.md) *(planned)* — the `control[_fast]` row payload (sequencer word / lane modes) that `opcode_table`'s low field selects.
- [2.27 — DVE Datapath Table](dve-datapath-table.md) *(planned)* — the 8-lane datapath block, keyed by `fast_row`, that completes the lookup.
- [2.28 — DVE Engine Migration](dve-engine-migration.md) *(planned)* — the 0xF1/0xF2 residency reconcile and the gen2→gen3→gen4 op-lifecycle deltas.
- [PE Matmul Encoding](pe-matmul-encoding.md) — the encode-side wire bundle (`0x10NN` header word); contrast with this decode-side table (§7 GOTCHA).
- [Build & Version Provenance](../reference/versions.md) — the pinned build and the cp310/11/12 parity argument.
