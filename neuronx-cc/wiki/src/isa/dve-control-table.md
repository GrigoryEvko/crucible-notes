# DVE On-Device Microcode — `control_table` (gen2 single → gen3/gen4 fast/slow split)

> *All sizes, hashes, and byte values on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 ship byte-identical DVE blobs — re-confirm only the loader offsets against cp311/cp312). The microcode blobs are shipped data files under the Python package, not symbols in `libwalrus.so`; their bytes are read directly with `xxd`/`od`/`python`. The opcode→row link (`opcode_table`) and the latency model are cross-read from `libwalrus.so`. Treat every value as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

The DVE ("Deep Vector Engine") is **microcode-programmed**: its behavior is not compiled into the C++ backend but shipped as indexed lookup-table blobs that the runtime loads into the engine ([1.11 the DVE engine](../arch/dve-engine.md)). [2.25 the opcode table](dve-opcode-table.md) maps a DVE opcode integer to a packed descriptor; that descriptor holds **row indices** into the *control table* — the subject of this page. A control row is one **microcode word** that tells the DVE's sequencer how to drive its datapath for that op: which lanes are live, which step op to issue, and (for iterative ops) which slow micro-program to loop.

This page is the **byte-exact decode of the control table**. The geometry is uniform and small: every control row is **16 bytes** in every generation. The schema changes exactly once. **gen2** ships one `control_table.bin` (2048 B, 128 rows, a 96-bit word). **gen3 and gen4** split it into two blobs — `control_fast_table.bin` (4096 B, 256 rows, a 64-bit word) and `control_slow_table.bin` (4096 B, 256 rows, a 112-bit word). The split is **born at gen3**, not gen4, and there is no generation in which the control table is null. The fast word is a compact single-issue datapath-step config; the slow word is a wider, **loopable iterative micro-program** built from a repeating 3-nibble step. An opcode runs the slow path iff its `opcode_table` descriptor carries a nonzero slow-row index.

The bar for this page: a reader can **take any control blob, slice it into 16-byte rows, decode a fast row's fields, and walk a slow row's iterative micro-program** — and, given an opcode's `opcode_table` entry, find which fast/slow rows it runs. Every claim carries a confidence tag (CONFIRMED = byte-exact from the blob; STRONG = derived from per-byte cardinality + cross-gen field tracking; INFERRED = implied by structure with no direct silicon witness; SPECULATIVE). No byte value on this page is fabricated; every hex literal was read from the shipped `.bin`.

## At a glance

| | |
|---|---|
| **Blob (gen2)** | `neuronxcc/dve/dve_bin_gen2/default_control_table.bin` — **2048 B**, `sha256 7998a037…387b919e` |
| **Blob (gen4 fast)** | `dve_bin_gen4/default_control_fast_table.bin` — **4096 B**, `sha256 1b109593…8495d35c` |
| **Blob (gen4 slow)** | `dve_bin_gen4/default_control_slow_table.bin` — **4096 B**, `sha256 9f2c2d81…c26e51f5` |
| **Row stride** | **16 bytes**, every generation (`size / row_count`) — CONFIRMED |
| **Row count** | gen2 = 128; gen3/gen4 fast = 256, slow = 256 — CONFIRMED |
| **Effective word width** | gen2 = 96-bit (`bytes[12:16]==0`); fast = 64-bit (`bytes[8:16]==0`); slow = 112-bit (`bytes[14:16]==0`) — CONFIRMED |
| **Schema split** | born at **gen3** (gen2 = 3 keys, single `control_table`; gen3/gen4 = 4 keys, `control_fast` + `control_slow`) — CONFIRMED |
| **Row index source** | `opcode_table` entry: `fast_row = V & 0xFF`, `slow_row = V >> 8` — CONFIRMED |
| **Slow selector** | `slow_row == 0` ⇒ fast-only single step; `slow_row != 0` ⇒ run `control_slow[slow_row]` — CONFIRMED |
| **Slow grammar** | a packed stream of fixed-width steps; the default step body = `0x30c` (advance/NOP); tail byte = loop/step count — CONFIRMED structure, STRONG semantics |
| **Table-set deltas** | `saturate` = localized patch on the quantize/sparsity rows (143+); `transformer` = wholesale re-compaction — CONFIRMED |

> **NOTE — the fast/slow split is BORN at gen3, not gen4.** A natural but wrong reading is that the control table goes null in gen3 (folded into opcode/datapath) and re-appears at gen4 as a dual table. The shipped blobs refute this directly: `dve_bin_gen3/` already contains `default_control_fast_table.bin` *and* `default_control_slow_table.bin`, both 4096 B, and `gen3/dve_info.json`'s `dve_table_keys` is the 4-key list `[opcode_table, control_fast_table, control_slow_table, datapath_table]` — identical to gen4. The real evolution is **gen2 single (128 rows) → gen3 split (256 fast + 256 slow) → gen4 (same split schema, re-authored payload)**. There is no null-control generation. (CONFIRMED — `ls` + `jq '.dve_table_keys'` on all three gens.)

---

## 1. Geometry — strides, row counts, word widths

The control blobs are tiny and have no header: they are flat arrays of fixed-stride rows, sliced by dividing the file size by the row count the `opcode_table`'s index space implies.

| Set / gen | File | Size (B) | Rows | Stride | Word width | Used rows |
|---|---|---|---|---|---|---|
| gen2 | `default_control_table.bin` | 2048 | 128 | **16 B** | 96-bit | 127 / 128 (row 127 all-zero) |
| gen3 | `default_control_fast_table.bin` | 4096 | 256 | **16 B** | 64-bit | 182 / 256 |
| gen3 | `default_control_slow_table.bin` | 4096 | 256 | **16 B** | 112-bit | 182 / 256 |
| gen4 | `default_control_fast_table.bin` | 4096 | 256 | **16 B** | 64-bit | 182 / 256 |
| gen4 | `default_control_slow_table.bin` | 4096 | 256 | **16 B** | 112-bit | 182 / 256 |

**Stride is a constant 16 bytes in every generation.** The gen2→gen3 change is not a stride change — it **doubles the row count** (128 → 256) by giving the slow program its own 256-row table. (CONFIRMED — `size / row_count`.)

> **GOTCHA — the row is 16 bytes, not 8.** Read with `xxd -c16`, a gen2 row often looks like an 8-byte entry padded with zeros (the back half of the line is frequently `00`). That is sparseness, not absence: 76 of 128 gen2 rows carry nonzero bytes in offset `[8:16]` (e.g. row 8 ends `… 00 c0 00 04`). Slicing at an 8-byte stride splits real rows in half and mis-aligns every subsequent decode. The row spans the full 16 bytes; the high bytes are sparsely used. (CONFIRMED — `python`: 76 rows nonzero in `[8:16]`.)

**Effective word width** is the trailing-zero envelope — the highest byte any used row ever writes (per-byte distinct-value cardinality over used rows, where `distinct==1 && value==0` means "never written"):

- **gen2**: `bytes[12:16]` are `0x00` in all 127 used rows → a **96-bit** word in a 16-byte slot.
- **gen4 fast**: `bytes[8:16]` are `0x00` → a **64-bit** word (narrow).
- **gen4 slow**: `bytes[8:14]` carry data (per-byte distinct = 11,16,6,6,4,2), `bytes[14:16]` = `0x00` → a **~112-bit** word (wide).

> **NOTE — the slow word is *wider* than the fast word.** At gen4 the slow word carries ~48 bits more than the fast word (112 vs 64). Those extra `bytes[8:14]` are the iterative-program payload (loop body / per-step config) that a single-issue fast step does not need. This width asymmetry is the structural fingerprint of the partition: fast = a compact single-step config, slow = a wide iterative-program descriptor. gen2's fused single word sits at 96 bits, between the two. (CONFIRMED — trailing-zero cardinality.)

---

## 2. The gen2 single `control_table` — field map

Per-byte distinct-value cardinality over the 127 used gen2 default rows pins where the data lives (`byte[i] = N` ⇒ N distinct values across used rows):

```text
byte[ 0]=18  byte[ 1]=16  byte[ 2]=20  byte[ 3]= 5
byte[ 4]=46  byte[ 5]=17  byte[ 6]= 9  byte[ 7]= 5
byte[ 8]= 6  byte[ 9]= 5  byte[10]= 4  byte[11]= 4
byte[12..15]=1   (all 0x00 → the word is 96-bit)
```

| Field | Bytes | Meaning | Confidence |
|---|---|---|---|
| **A — lane-mode** | `[0:2]` u16 LE | 8 datapath lanes × 2 bits each; gates which lanes are live | STRONG |
| **B — step op-sel** | `[2]` | step control-op / ALU-op selector (5-bit op + high flag bit) | STRONG |
| **C — sequencer PC / imm** | `[4]` | per-step program counter / immediate (highest-entropy byte) | STRONG |
| **D — issue marker** | `[4:6]` | sequencer fetch/issue marker (`20 80`, `a0 8x` families) | STRONG |
| **E — operand / mux** | `[6:12]` | secondary operand / mux fields (low cardinality 4–9) | STRONG |

**Field A — lane-mode (`bytes[0:2]`).** Decoded as eight 2-bit lane modes (u16 LE), the dominant value is `0x5555` (×55 rows) = lanes `[1,1,1,1,1,1,1,1]` = all lanes in mode `01` (the pass/default/idle config). Row 0 (the NOP row) is `0x5555`. Active rows perturb individual low lanes while leaving the high lanes at `01`:

```text
0x5555 (×55)  lanes [1,1,1,1,1,1,1,1]   all-pass / idle  (row 0 = NOP)
0x5550 (×16)  lanes [0,0,1,1,1,1,1,1]   lanes 0,1 disabled
0x5505 (×11)  lanes [1,1,0,0,1,1,1,1]   lanes 2,3 disabled
0x5520 (×4)   lanes [0,0,2,0,1,1,1,1]   lane 2 → mode 10
0x55c0 (×2)   lanes [0,0,0,3,1,1,1,1]   lane 3 → mode 11
```

The 8 lanes here are the **8 datapath lanes** that the [2.27 datapath table](dve-datapath-table.md) configures (8 words per datapath block). The control word's lane-mode field gates which lanes are live for this step; the datapath block then configures the live lanes. The two tables are co-indexed by control row. Mode `01` = pass/default, `00` = disabled, `10`/`11` = alternate routes. (STRONG — `0x5555 = 8×'01'`; the exact silicon meaning of `10`/`11` is SPECULATIVE.)

**Field B — step op-sel (`byte[2]`).** Distinct values are dominated by `0x10` (48 rows), with a `0x10..0x1f` step-variant cluster and a high-flag class (`0x80`/`0x83`, 4+1 rows). `byte[2] < 0x20` in 122 of 127 rows ⇒ a 5-bit op selector plus a high flag bit. (STRONG.)

**Field C — sequencer PC/immediate (`byte[4]`).** 46 distinct values (the highest cardinality) — nearly contiguous `0..0x1f` plus scattered highs. This is the most data-like byte and climbs roughly monotonically with row index in the active region, consistent with a per-step program-counter / immediate. (STRONG.)

> **CORRECTION — the gen2 field map does NOT transplant onto gen4 fast.** The gen2 offsets (lane-mode `@[0:2]`, op-sel `@[2]`, PC `@[4]`, issue marker `@[4:6]`) describe the gen2 **12-byte** layout only. gen4 re-packs the word into 8 bytes with shifted fields: the gen2 step-valid marker `byte[6]=0x10` (row 0 `55 55 .. 10 ..`) **moves to `byte[4]=0x10`** in gen4 fast (present in all 182 used fast rows). The gen2 lane-mode `@[0:2]` does not survive at `[0:2]` in gen4 — gen4 fast `byte[0]` has only 4 distinct values `{0,1,2,8}` while `byte[1]` becomes the 92-distinct operand/PC. Use the gen2 map for gen2 only. (CONFIRMED — the word was re-packed 12 B → 8 B.)

### 2.1 gen2 has no slow control table — only a proto-slow index

A targeted scan finds **zero** gen2 control rows carrying the `c3 30 0c` slow-program backbone that defines gen3/gen4 slow rows (§3). Every gen2 control row is fast-style microcode. The gen2 `opcode_table`'s high field (`V >> 7`, a "slow index" in [2.25](dve-opcode-table.md)) does **not** index a slow *control* table — there is none in gen2. The slow ops park on idle base control rows, and their iterative work is driven by a separate gen2 mechanism that is not shipped as a control blob. (CONFIRMED — no `c3`/`30`/`0c` backbone in any gen2 row; all five gen2 table-sets are fast-only.)

So the 128 → 256 row doubling at gen3 is the **slow table being created**, not a field "promoted." gen2 had a slow *index* (spare opcode bits); gen3 adds a genuinely new 256-row `control_slow` blob.

---

## 3. gen3/gen4 fast vs slow — the two encodings differ

Per-byte distinct-value cardinality over the 182 used rows (gen4 default):

```text
control_FAST:  byte[0]= 4  byte[1]=92  byte[2]=27  byte[3]=12
               byte[4]= 6  byte[5]= 5  byte[6]=19  byte[7]= 2
               byte[8..15]=1            → 64-bit / 8-byte word

control_SLOW:  byte[0]=12  byte[1]=22  byte[2]=13  byte[3]=10
               byte[4]= 8  byte[5]= 4  byte[6]=25  byte[7]= 3
               byte[8]=11  byte[9]=16  byte[10]=6  byte[11]=6
               byte[12]=4  byte[13]=2  byte[14..15]=1   → ~112-bit / 14-byte word
```

The slow word carries ~48 bits more than the fast word; the extra `bytes[8:14]` are the iterative payload. (CONFIRMED.)

### 3.1 The fast word — single-issue datapath-step config

A fast row is a single sequencer issue → one datapath block. The recurring structure: `byte[1]` (92 distinct) is the high-entropy operand/immediate/micro-PC; `bytes[4:6]` carry the `20 80` / `a0 8x` fetch-issue marker (the same family as the gen2 Field D marker, re-packed). Representative gen4 default fast rows:

```text
row  1  Pool          01 00 02 08 a0 89 0c 00     byte1=op-imm, [4:6]=a0 89 (issue)
row 16  TTArith       00 00 00 00 20 80 09 00     [4:6]=20 80 (issue), byte6=0x09
row 48  Copy / Cast   00 00 00 00 20 80 08 00     shared by Copy(70) & Cast(71)
row111  StrTranspose  00 00 e8 00 a0 82 08 00
row143  QuantizeMX    00 00 00 00 20 80 79 00     byte6=0x79 = the quantize step-op
```

### 3.2 The slow word — the iterative micro-program grammar

The slow word has a **repeating 12-bit / 3-nibble backbone**. Read `bytes[0:8]` as a nibble stream, low-nibble first:

```text
slow row 0   0c c3 30 0c c3 30 00 00
             nibbles  [12,0,3]  [12,0,3]  [12,0,3]  [12,0,3]  tail [0,0]
slow row 1   00 c1 30 0c c3 30 03 00
             nibbles  [0,0,1]   [12,0,3]  [12,0,3]  [12,0,3]  tail [3,0]
```

The triplet `(12,0,3)` = **`0x30c`** repeats 3–4×. The slow row is a **packed micro-program**: a stream of fixed-width sequencer *steps*, each step's default body = `0x30c` (an advance/NOP step). The variation lives in (a) the **head nibbles** (`byte[0]`, low nibble of `byte[1]`), which substitute a non-default step body for the first step(s), and (b) the **tail byte** (`byte[6]`/`byte[7]`), which is the **step/loop count** — how many iterations the program runs (observed values `0,1,2,3,7,a,f`). The slow-only `bytes[8:14]` hold the loop body / bound config. (CONFIRMED structure; STRONG loop-count semantics; the exact loop-bound encoding is SPECULATIVE.)

The seven most common `bytes[1:7]` backbones over the 182 used slow rows:

```text
c3300cc33000 ×56   c3300cc33001 ×26   c3300cc33002 ×9   c1300cc33002 ×8
ce300cc33003 ×8    c1300cc33003 ×7    c0300cc33003 ×6   40300cc33007 ×3
```

The trailing nibble of each (`…00`, `…01`, `…02`, `…03`, `…07`) is the loop count; the leading nibbles (`c3`, `c1`, `ce`, `c0`, `40`) substitute the program's first step body. A reimplementer walks a slow program as:

```c
// Walk one slow micro-program from a control_slow row.
// row16 = 16 raw bytes of control_slow[slow_row].
void run_slow_program(const uint8_t row16[16]) {
    // 1. The program is a stream of 12-bit (3-nibble) steps over bytes[0:8].
    //    Read low-nibble first.  Default step body 0x30c = "advance / NOP".
    uint16_t steps[6];               // up to ~5-6 packed steps per row
    nibble_stream_t ns = nibbles_lo_first(row16, /*len*/ 8);
    int nstep = 0;
    while (nibbles_remaining(&ns) >= 3)
        steps[nstep++] = read_nibble_triplet(&ns);   // e.g. 0x30c

    // 2. The tail byte is the loop / iteration count.
    uint8_t loop_count = row16[7] ? row16[7] : row16[6];   // tail (0,1,2,3,7,a,f)

    // 3. The slow-only payload bytes[8:14] parameterize the loop body
    //    (bounds / strides / per-step config).  Exact packing: SPECULATIVE.
    const uint8_t *body = &row16[8];      // bytes[8:14] carry data; [14:16]==0

    // 4. Execute: for each iteration, issue the step bodies in order.
    for (uint8_t iter = 0; iter <= loop_count; ++iter)
        for (int s = 0; s < nstep; ++s)
            issue_sequencer_step(steps[s], body);   // 0x30c = advance
}
```

> **NOTE — fast rows are single-issue; slow rows are loopable.** A fast row issues one step and is done. A slow row is a small program with an iteration count, re-running its step body `loop_count+1` times. Whether the program is fetched once and looped in hardware or re-issued per element is **SPECULATIVE** — the blob proves the loop-count field exists and the `0x30c` step repeats, not the silicon fetch discipline.

### 3.3 Gen-specificity and cross-set

gen3 vs gen4 `control_fast` differ (first diff at byte 81); gen3 vs gen4 `control_slow` differ in **all 182 used rows** — the slow micro-programs were fully re-authored gen3→gen4 as the roster grew and the iterative kernels were recompiled. Each generation ships its own compiled control image, not a shared blob. All five gen2 table-sets carry **zero** slow-backbone rows, reinforcing §2.1: the slow control table is a gen3+ construct, universally absent in gen2. (CONFIRMED.)

---

## 4. The selection predicate — how an opcode picks fast vs slow

The [2.25 opcode table](dve-opcode-table.md) entry is the selector. For gen3/gen4 the entry is a u32 `V` indexed by opcode (`opcode_table[op]`), and:

```c
uint8_t fast_row = V & 0xFF;     // always present: the inline / setup step
uint8_t slow_row = V >> 8;       // 0 ⇒ fast-only; nonzero ⇒ iterative slow program

if (slow_row == 0)
    run_fast_step(control_fast[fast_row]);            // single inline step
else
    run_slow_program(control_slow[slow_row]);         // iterative micro-program
                                                      // (fast_row = its setup/stub)
```

Decoded gen4 default opcodes (read directly from `default_opcode_table.bin`, 256 × u32, and the named rows from the control blobs):

| op | `V` | fast | slow | name | path |
|---|---|---|---|---|---|
| 48 | `0x00008` | 8 | 0 | Exponential | fast-only |
| 65 | `0x03010` | 16 | 48 | TensorTensorArith | **SLOW** (slow48 = `00 c3 30 0c c3 30 01`) |
| 66 | `0x00018` | 24 | 0 | TensorReduceArith | fast-only |
| 69 | `0x00001` | 1 | 0 | Pool | fast-only |
| 70 | `0x00030` | 48 | 0 | Copy | fast-only |
| 71 | `0x00030` | 48 | 0 | Cast | fast-only (**shares fast48 with Copy**) |
| 73 | `0x00640` | 64 | 6 | Memset | **SLOW** (slow6) |
| 77 | `0x00648` | 72 | 6 | Rng | **SLOW** (**shares slow6 with Memset**) |
| 102 | `0x0264c` | 76 | 38 | LoadParameterRam | **SLOW** (fast76 = idle stub) |
| 105 | `0x0164c` | 76 | 22 | LoadMaskSelect | **SLOW** (**shares idle fast76**, slow differs) |
| 106 | `0x00a30` | 48 | 10 | StreamShuffle | **SLOW** (shares fast48, adds slow10) |
| 107 | `0x0046f` | 111 | 4 | StreamTranspose | **SLOW** |
| 127 | `0x00070` | 112 | 0 | Dropout | fast-only |
| 227 | `0x0008f` | 143 | 0 | QuantizeMX | fast-only (the saturate-divergent row) |
| 234 | `0x000b4` | 180 | 0 | SelectReduce | fast-only |

Observations (all CONFIRMED):

- **Slow ops are the iterative / multi-pass families**: Memset & Rng (fill/generate N elements), StreamShuffle/StreamTranspose (permutation passes), LoadParameterRam / LoadMaskSelect (RAM streaming loads), and one banked TensorTensorArith variant. **Fast-only ops are single-pass element ops**: Copy, Cast, Pool, Reciprocal, Exponential, Dropout, QuantizeMX, SelectReduce, BatchNormStats2.
- **Memset(73) and Rng(77) share `slow_row 6`** — the same fill-loop micro-program; they differ only in their fast/setup row (64 vs 72). Slow rows are reusable iterative kernels shared across ops with the same loop shape.
- **op102 / op105 share `fast_row 76`** (an idle setup stub) and differ only in `slow_row` (38 vs 22): for pure-slow ops the fast row is a no-op placeholder and the whole computation lives in the slow program.
- **op106 StreamShuffle shares `fast_row 48`** with Copy/Cast and adds `slow_row 10` — same single-step setup plus an iterative permutation program.

---

## 5. Worked decode — one fast row and one slow row, byte by byte

### 5.1 Fast: op 65 TensorTensorArith setup step (`control_fast[16]`)

```text
$ xxd -g1 -c16 -s $((16*16)) -l16 dve_bin_gen4/default_control_fast_table.bin
00000100: 00 00 00 00 20 80 09 00 00 00 00 00 00 00 00 00
```

Decode (gen4 fast layout):

```text
byte[0]   = 0x00   lane / op-class low (4-distinct field {0,1,2,8}) — here 0
byte[1]   = 0x00   operand / micro-PC immediate (the 92-distinct field) — here 0
byte[2:4] = 00 00  step config (cardinality 27 / 12)
byte[4]   = 0x20 } issue marker `20 80` (the fetch/issue family); byte[4]=0x20
byte[5]   = 0x80 }   carries the step-valid bit moved from gen2 byte[6]
byte[6]   = 0x09   step op-selector  (= 9, the TensorTensor arith step)
byte[7]   = 0x00   high op bits
byte[8:16]= 0      unused (64-bit word)
```

This is the single inline step the engine issues for the *setup* of op 65; because op 65's `slow_row` is 48 (nonzero), the engine then runs the slow program at `control_slow[48]`.

### 5.2 Slow: op 65's iterative program (`control_slow[48]`)

```text
$ xxd -g1 -c16 -s $((48*16)) -l16 dve_bin_gen4/default_control_slow_table.bin
00000300: 00 c3 30 0c c3 30 01 00 00 00 00 00 00 00 00 00
```

Walk it with the §3.2 grammar:

```text
bytes[0:8] = 00 c3 30 0c c3 30 01 00
nibble stream (lo-first):
   00 -> [0,0]          head: low nibble of byte0/1 selects first step body
   c3 -> [3,12]
   30 -> [0,3]      → triplet stream: [0,0,1] [12,0,3] [12,0,3] [12,0,3]
   0c -> [12,0]                          ^head     ^0x30c   ^0x30c   ^0x30c
   c3 -> [3,12]
   30 -> [0,3]
   01 -> [1,0]   ← byte[6]=0x01 = LOOP COUNT (run the body twice: iter 0..1)
   00 -> tail

steps     = { 0x001 (head), 0x30c, 0x30c, 0x30c }   // 0x30c = advance/NOP
loop_count= 1                                        // iterate 0..1
payload   = bytes[8:14] = 00 00 00 00 00 00          // this kernel: no extra body
```

So op 65 runs: the fast setup step (`control_fast[16]`, the `0x09` arith issue), then the slow program at `control_slow[48]` — a head step `0x001` followed by three `0x30c` advance steps, looped twice. A reimplementation that ignores `slow_row` and emits only the fast step under-issues every iterative op. (CONFIRMED — bytes read firsthand; the head-step semantics beyond "non-default first step" are STRONG.)

---

## 6. Table-set deltas — default / saturate / transformer

Each table-set ships its own compiled `control_fast` + `control_slow` pair, consistent with [1.11's default / transformer / saturate roster](../arch/dve-engine.md). Diffing gen4 control rows against gen4 default:

**`saturate` vs `default` — a localized arithmetic-mode patch.** `control_fast` differs in **28 / 256** rows; `control_slow` in **17 / 256**. The divergence is localized: it starts at **row 143** and clusters in rows 143..182 — exactly the quantize / sparsity op rows (op227 QuantizeMX → fast 143; op224 SparsityCompress → 152; op225 SparsityCompressTag → 170). Example:

```text
fast row 143  default   00 00 00 00 20 80 79 00     byte6=0x79 (quantize step)
              saturate  01 92 00 00 08 80 01 00     rewired: byte0/1 set, byte6=0x01
```

`saturate` rewires the QuantizeMX control step (op-selector `0x79` → a clamping micro-op) and its slow program; every common op (Copy, Cast, Pool, …) is **byte-identical** to default. `saturate` is a surgical patch on the saturating-arithmetic rows, not a wholesale different image. This is the *control-word* witness for [1.11's QUIRK](../arch/dve-engine.md): `saturate` is not an alias of `default`. (CONFIRMED — diff localized to rows 143+.)

**`transformer` vs `default` — a wholesale re-compaction.** `control_fast` differs in **170 / 256** rows. The transformer set has a different op roster, so its compiler assigns different control-row numbers to shared ops (independent table compaction); nearly every populated row shifts, and the set's own `opcode_table` re-maps op→row to match its own control image. (CONFIRMED.)

So the control table is, like the opcode and datapath tables, a **per-table-set microcode image**: `saturate` = a localized sat-op-row patch; the `transformer` family = a fully independent compaction.

> **NOTE — the microcode-sequencer tier is not the PerfSim cost tier.** The fast/slow control partition is a *hardware micro-sequencer* choice baked into the shipped blobs (single inline step vs iterative program). PerfSim's DVE latency model in `libwalrus` is a *separate* analytic cost model that does **not** read these blobs — it computes `getDVEInitialCycles(inst) + getExecLatencyHelper(...)` with a dtype-tiered init (gen2 const 100; gen3/gen4 = dtype-specialized, e.g. 65 for fp16 args) and an NEP-scaling exec term. The two tiers *correlate* at the op-family level (control-slow ops are the NEP-scaling iterative families; control-fast ops are the init-dominated single-pass families) but are computed independently — e.g. TensorReduceArith is control-fast-only yet NEP-scaling in PerfSim, its iteration folded into a wider fast datapath block. Do not claim the perf model consumes `control_slow`; it does not. (STRONG.)

---

## 7. Confidence summary

**CONFIRMED (byte-exact from the blobs):**

- Schema: gen2 = single `control_table` (3 keys); gen3 **and** gen4 = `control_fast` + `control_slow` (4 keys). No null-control generation; the split is born at gen3.
- Stride = 16 B/row in every gen; gen2 = 128 rows, gen3/gen4 fast = 256, slow = 256.
- Word widths: gen2 = 96-bit, fast = 64-bit, slow = 112-bit (slow is the widest).
- Selection predicate: `opcode_table` `slow_row = V>>8`; `==0` ⇒ fast-only, `!=0` ⇒ run `control_slow[slow_row]`. 15 gen4 ops decoded end to end; Memset & Rng share slow6; op102/105 share an idle fast stub.
- gen2 has no slow control table (zero `c3/30/0c` backbone rows; all five gen2 sets fast-only).
- Slow word = a repeating `0x30c`-step micro-program with a loop-count tail; fast word = single-step config with `20 80` / `a0 8x` issue markers.
- Variants: `saturate` = control diff localized to quantize/sparsity rows (28 fast / 17 slow, rows 143+); `transformer` = wholesale re-compaction (170 fast rows differ).

**STRONG:** gen2 field map (lane-mode `[0:2]`, op-sel `[2]`, PC `[4]`, issue marker `[4:6]`, operand/mux `[6:12]`); lane-mode gates the 8 datapath lanes; slow tail byte = loop/step count; fast/slow tier correlates with PerfSim's init-vs-NEP tier but is mechanistically independent.

**INFERRED:** exact bit boundaries inside the fast 64-bit word's `byte[1]` (the 92-distinct operand/PC) and the slow word's per-step packing beyond the `0x30c` backbone.

**SPECULATIVE:** the precise loop-bound encoding of the slow program (how `byte[7]` + `bytes[8:14]` set iteration count / stride); whether the slow program is fetched once and looped in hardware or re-issued per element; the silicon meaning of lane-modes `10` / `11`.

---

## Cross-References

- [1.11 The DVE engine](../arch/dve-engine.md) — engine architecture, the default / transformer / saturate table-set roster, the gen2-3-key → gen3/gen4-4-key schema, and the opcode→name Rosetta.
- [2.25 DVE opcode table](dve-opcode-table.md) — the opcode→row index this page consumes (`fast_row = V&0xFF`, `slow_row = V>>8`).
- [2.27 DVE datapath table](dve-datapath-table.md) — the 8-lane datapath block each control row owns; co-indexed by control row.
- [2.28 DVE engine migration](dve-engine-migration.md) — the gen2→gen3 `0xF1`/`0xF2` reconcile and roster growth across generations.
- [Build & Version Provenance](../reference/versions.md) — the pinned build and the cp310/11/12 parity argument.
