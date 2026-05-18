# Mercury Encoder Pipeline

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

Mercury is NVIDIA's intermediate encoding layer between the optimizer's Ori IR and native SASS machine code. It is not a direct binary encoding of SASS -- it is a separate representation that contains pseudo-instructions, lacks dependency barriers, and requires multiple transformation passes before it becomes executable GPU code. The Mercury pipeline occupies phases 113--122 of the 159-phase PhaseManager, forming a six-stage sub-pipeline: encode/decode verification, pseudo-instruction expansion, two WAR-hazard passes (one before and one after operation expansion), scoreboard/latency generation ("opex"), and final SASS microcode emission. All recent GPU architectures (SM 75+) use Mercury as the encoding backend; SM 100+ (Blackwell) defaults to "Capsule Mercury" (`capmerc`), a variant that embeds additional metadata for relocatable patching.

| | |
|---|---|
| **Pipeline phases** | 113--122 (8 active phases within Mercury sub-pipeline) |
| **Core orchestrator** | `sub_6F52F0` (23KB, RunStages -- 18 parameters) |
| **Master encoder** | `sub_6D9690` (94KB, EncodeInstruction -- largest backend function) |
| **Opex body** | `sub_6FFDC0` (66KB, EmitInstructions -- scoreboard generation) |
| **Expansion pass** | `sub_C3CC60` (26KB, MercExpand::run) |
| **WAR generator** | `sub_6FBC20` (7.4KB, GenerateWARHazards) |
| **SASS emitter** | `sub_6E4110` (24KB, MercGenerateSassUCode) |
| **Bitfield insert** | `sub_7B9B80` (216 bytes, 18,347 callers across binary) |
| **Encoding table funcs** | 530 functions at `0xC66000`--`0xD27000` |
| **Mercury mode flag** | `*(DWORD*)(context+385) == 2` |
| **Mode check** | `sub_10ADF10` returns bool from target descriptor |
| **MercConverter** | `sub_9F3340` (7KB orchestrator), `sub_9EF5E0` (27KB operand reorganization) |
| **CLI option** | `--binary-kind mercury,capmerc,sass` |

## Architecture

```
Phase 113  PostFixForMercTargets          Late Ori fixups for Mercury targets
Phase 114  FixUpTexDepBarAndSync          Texture dependency bars + sync fixups
Phase 115  AdvancedScoreboardsAndOpexes   Arch hook point (noop by default)
Phase 116  ProcessO0WaitsAndSBs           -O0 scoreboard insertion
                                          ──────────────────────────────
Phase 117  MercEncodeAndDecode            ┐
Phase 118  MercExpandInstructions         │  Six-stage Mercury core
Phase 119  MercGenerateWARs1              │
Phase 120  MercGenerateOpex               │
Phase 121  MercGenerateWARs2              │
Phase 122  MercGenerateSassUCode          ┘

sub_6F52F0 (23KB orchestrator, 18 params)
  │
  ├─ [1] Decode:     sub_6F2BF0 (59KB)  — Encode Ori→Mercury binary, decode back
  │      └─ sub_6D9690 (94KB master encoder switch)
  │           ├─ sub_6D2750 — append operand word
  │           ├─ sub_6D28C0 — commit encoded instruction
  │           ├─ sub_6D9580 — encode literal values
  │           └─ sub_931690 — create instruction record
  │
  ├─ [2] Expansion:  sub_C3CC60 (26KB)  — Expand pseudo-instructions to SASS
  │      ├─ sub_C37A10 (16KB) — expandInstruction (jump table dispatch)
  │      ├─ sub_C39B40 (10KB) — expandMemoryOp
  │      ├─ sub_C3A460 (6KB)  — expandAtomicOp
  │      ├─ sub_C3B560 (8KB)  — expandTexture
  │      ├─ sub_C3BCD0 (19KB) — expandControlFlow
  │      └─ sub_C3E030 (18KB) — finalizeExpansion
  │
  ├─ [3] WAR pass 1: sub_6FBC20 (7.4KB) — DEPBAR/scoreboard for pre-opex hazards
  │      ├─ sub_6FA5B0 — detect WAR hazard per instruction
  │      ├─ sub_6FA930 — insert scoreboard barrier (opcode 54)
  │      ├─ sub_6FA7B0 — insert WAITDP (opcode 246)
  │      └─ sub_6FAA90 — insert stall cycles
  │
  ├─ [4] Opex:       sub_6FFDC0 (66KB)  — Generate scoreboards + latency waits
  │      └─ sub_703480 (1.4KB entry) or sub_7032A0 (2.3KB MercOpex entry)
  │
  ├─ [5] WAR pass 2: sub_6FBC20          — Same pass, re-run for opex-introduced hazards
  │
  └─ [6] SASS emit:  sub_6E4110 (24KB)  — Final SASS microcode generation
         └─ sub_735290 — per-instruction encoding pipeline
              ├─ sub_733FA0 — encode instruction operands
              ├─ sub_734370 — encode immediates
              ├─ sub_734820 — encode predicates
              ├─ sub_734AD0 — encode memory operands
              └─ sub_734D20 — encode complex operands (texture/surface/barrier)
```

Each stage logs its completion via trace infrastructure: `"After Decode"`, `"After Expansion"`, `"After WAR post-expansion"`, `"After Opex"`, `"After WAR post-opexing"`.

## Mercury vs SASS vs Capsule Mercury

The ptxas CLI (`sub_703AB0`) accepts `--binary-kind` with three values:

| Mode | CLI value | Default for | Description |
|---|---|---|---|
| Mercury | `mercury` | SM 75--99 | Traditional Mercury intermediate encoding |
| Capsule Mercury | `capmerc` | SM 100+ (Blackwell) | Mercury + embedded PTX source + relocation metadata |
| Raw SASS | `sass` | (explicit only) | Direct SASS binary output |

Additional CLI flags:
- `--cap-merc` -- force Capsule Mercury generation
- `--self-check` -- roundtrip verification: reconstitute SASS from capmerc, compare with original
- `--out-sass` -- dump reconstituted SASS from capmerc

Mercury mode is flagged at `*(DWORD*)(context+385) == 2`. The function `sub_10ADF10` queries the target descriptor to determine whether Mercury encoding is active for the current architecture.

## MercConverter -- Operand Reorganization for Encoding

| | |
|---|---|
| **Phase** | 141 (MercConverter) |
| **Orchestrator** | `sub_9F3340` (7KB) |
| **Post-conversion lowering** | `sub_9EF5E0` (27KB) |
| **Opcode dispatch** | `sub_9ED2D0` (25KB, shared with phase 5) |
| **Strings** | `"CONVERTING"`, `"After MercConverter"` |

Phase 141 runs the MercConverter infrastructure a second time, after the full optimization pipeline has completed. While phase 5 (`ConvertUnsupportedOps`) performs the initial PTX-to-SASS opcode conversion early in the pipeline, phase 141 re-invokes the same machinery to handle instructions that were introduced or modified by optimization passes (rematerialization, peephole, loop transformations) and may contain PTX-derived opcodes that were never legalized. After phase 141 completes, the `"After MercConverter"` diagnostic string appears, and every instruction in the IR carries a valid SASS opcode ready for Mercury encoding.

The orchestrator `sub_9F3340` runs two steps sequentially:

1. **Opcode conversion** (`sub_9F1A90`, 35KB): the main MercConverter dispatch documented in [ISel](./isel.md). Converts any remaining PTX-derived opcodes to SASS equivalents via the master switch in `sub_9ED2D0`. Gated by `*(BYTE*)(*(context+8) + 1398) & 0x20`.

2. **Operand reorganization** (`sub_9EF5E0`, 27KB): post-conversion lowering that restructures operand lists into a form the Mercury encoder can consume directly. Gated by `*(BYTE*)(*(context+16) + 1048) != 0` AND `*(context+104) != 0` (non-empty instruction BST).

**Gate flag `byte_1398 & 0x20` -- who sets it.** The flag lives in the SM-backend profile object at byte offset 1398. It is set by the architecture initialization function `sub_7DD480`, which reads the architecture capability word at `profile+1413` and programs byte 1398 based on the encoding-mode bits. Specifically, bit 0x20 is set (`byte_1398 = old & 0x1F | 0x60`) when capability bits 3 (`cap & 0x08`, Mercury-capable) or bits 4-5 (`cap & 0x30 == 0x20`, Capsule-Mercury-capable) are present. When the capability word has `cap & 0x30 == 0x10` (legacy non-Mercury mode), bit 0x20 is left unset (`byte_1398 &= ~0x80`; bit 5 untouched in the 0x10 path). The net effect: bit 0x20 at offset 1398 means "this SM target supports Mercury opcode conversion," and when clear, both phase 5 and phase 141 skip the opcode conversion step entirely, running only operand reorganization (if applicable).

### Post-Conversion Lowering -- `sub_9EF5E0` (27KB)

This function transforms the BST (binary search tree) of converted instructions produced by step 1 into encoding-ready conversion nodes. For each instruction record in the BST, it performs three operations:

**1. Operand sort.** Calls `sub_9EC160`, a linked-list merge sort (Floyd's slow/fast pointer midpoint, recursive split-and-merge) that sorts the operand chain by the operand index at entry+16. This establishes a canonical ordering required by the encoder.

**2. Contiguous/gap partitioning.** Walks the sorted operand list and classifies each operand into one of two sublists:

```c
// Simplified partitioning logic (lines 215-348 of decompilation)
for (op = first; op != sentinel; op = op->next) {
    int cur_idx  = *(DWORD*)(op + 16);
    int next_idx = *(DWORD*)(op->next + 16);

    if (next_idx - cur_idx == 32) {
        // Consecutive register indices -> contiguous sublist
        append_to_contiguous_list(node, cur_idx);
    } else {
        // Non-consecutive -> gap sublist (stores both cur and next index)
        append_to_gap_list(node, cur_idx, prev_idx);
    }
}
```

The stride of 32 reflects the operand index encoding: `index = register_number * 32 + modifier_bits`. Contiguous operands (stride-32 sequences like R0, R1, R2, R3) represent packed register groups -- common in wide loads (`LDG.128`), GMMA matrix operands, and multi-register moves. The encoder can represent these as a single register-range specifier. Gap operands break the stride and require individual encoding slots.

**3. Conversion node construction.** Allocates a 168-byte conversion node per instruction, inserts it into a per-record BST sorted by `(block_id, sub_block_id)`, and links the two operand sublists:

```
Conversion Node (168 bytes):
  +0     8B    BST left child
  +8     8B    BST right child
  +16    8B    BST parent
  +24    4B    block_id
  +28    4B    sub_block_id
  +32    48B   Contiguous operand doubly-linked list (6 pointers)
  +80    4B    Contiguous operand count
  +88    8B    Contiguous list ref-counted handle
  +96    48B   Gap operand doubly-linked list (6 pointers)
  +144   4B    Gap operand count
  +152   8B    Gap list ref-counted handle
  +160   1B    Flags
```

BST insertion calls `sub_7C11F0` for red-black tree rebalancing. The record tracks min/max block IDs at `record+32` and `record+40` for range queries.

### Encoding Validation and Fallback

After building the conversion node, the function attempts encoding:

```c
// Lines 949-982 of decompilation
nullsub_644(*(a1+16), node, "CONVERTING");      // diagnostic trace
int result = sub_7BFC30(node);                   // encoding validation

if (result == -1) {
    // Encoding failed: recursive fallback
    sub_9CE210(a1, node);
    // Continue with next instruction in BST
} else {
    // Encoding succeeded: emit to output
    *(node + 4) = result;                        // store encoding index
    output_slot = vtable_alloc(*(a1+24), 120);   // allocate output record
    *(output_slot + 96) = node;                  // link conversion node
    sub_9314F0(&scratch, *(a1+8), 0xF, 1, 1,    // emit SASS instruction
               &control_word);                   // control = 0x60000000
}
```

`sub_7BFC30` walks the conversion node's BST, accumulates a bit-budget for all operand partitions, and returns an encoding-unit index (0 or 1) or -1 on overflow:

```
validate_encoding(record):                    // sub_7BFC30
    node = record->bst_root          (+32)    // first conversion node
    if node == NULL: return 1                  // empty -> trivially valid
    bits = 4                                   // base overhead (4 bits)

    for each node in BST in-order:
        contig_count = node->contig_count (+80) - 2    // minus 2 sentinels
        gap_count    = build_gap_list(node)     - 2    // sub_7BEEC0; also minus 2

        // Per-partition limit: max 15 contiguous, max 15 gap operands
        if contig_count > 15 OR gap_count > 15:
            return -1                          // partition too wide

        // Accumulate bit cost: 2-bit node header
        //   + ceil(10 * contig_count / 8) for contiguous encoding
        //   + ceil(15 * gap_count    / 8) for gap encoding
        bits += 2 + ((10 * contig_count + 7) >> 3)
                  + ((15 * gap_count    + 7) >> 3)

    // Convert total bits to encoding-unit index
    rounded = bits + 17                        // +17 = 16-bit unit alignment + 1
    if rounded < 0: rounded = bits + 32        // negative guard (large bit counts)
    unit_index = rounded >> 4                  // divide by 16
    return unit_index < 2 ? unit_index : -1    // must fit in units 0 or 1
```

The 10-bit and 15-bit per-operand costs reflect the contiguous and gap encoding widths: a contiguous operand needs 10 bits (register base + range length), while a gap operand needs 15 bits (two register indices + modifier delta). The ceiling division `(N*W+7)>>3` byte-aligns each partition's contribution.

On failure, `sub_9CE210` (a recursive fallback) re-processes the instruction by splitting the operand group into smaller sub-groups that each fit within the 2-unit encoding budget.

### Relationship to Phase 5

Phase 5 and phase 141 share the same code (`sub_9F3340` orchestrator, `sub_9ED2D0` dispatch, `sub_9EF5E0` post-conversion). The difference is context:

| Property | Phase 5 | Phase 141 |
|---|---|---|
| Pipeline position | Before optimization | After optimization, before Mercury encoding |
| Purpose | Convert PTX opcodes to SASS | Re-legalize instructions introduced by optimizer |
| Input | Raw Ori IR with PTX opcodes | Optimized Ori IR with possibly-illegal opcodes |
| Output | Optimizer-ready SASS-opcode IR | Encoding-ready IR for Mercury phase 142+ |
| Gate flag | `*(BYTE*)(profile + 1398) & 0x20` (set by `sub_7DD480`) | Same flag, re-checked |

## Stage 1: MercEncodeAndDecode -- Roundtrip Verification

| | |
|---|---|
| **Phase** | 117 |
| **Orchestrator** | `sub_6F52F0` (23KB, 18 parameters) |
| **Decode worker** | `sub_6F2BF0` (59KB) |
| **String** | `"After EncodeAndDecode"` |

This phase encodes the Ori IR instruction stream into Mercury binary form, then immediately decodes it back and verifies that the decoded result matches the original. This is a self-consistency check that catches encoding bugs early -- if the roundtrip fails, the instruction cannot be correctly represented in Mercury format.

The orchestrator `sub_6F52F0` passes the entire pipeline state (18 parameters) to `sub_6F2BF0`, which performs the actual encode-decode cycle using the master encoder `sub_6D9690`.

### Master Encoder -- `sub_6D9690` (94KB)

The central SASS instruction encoding function and the single largest function in the ptxas backend. It reads the instruction type from `*(int*)(instruction+8)`, initializes the encoding base constant `0x2000000000LL`, calls `sub_C00BF0` for the opcode table index, then dispatches through a 119-case `switch` covering Ori instruction types 61--544. Unhandled types hit the `default` branch and return 0 (failure). The epilog at `LABEL_3` frees the dynamic word buffer if it grew beyond the 264-byte inline array.

#### Operand Word Type Prefix -- bits `[31:28]`

Every operand word appended via `sub_6D2750` carries a 4-bit type tag in its upper nibble. The encoder constructs these with literal OR masks:

| Prefix | Hex mask | Meaning | Typical source |
|--------|----------|---------|----------------|
| `0x1` | `0x10000000` | Register operand | `sub_91D160` return value, `sub_A99F50` for pred/special |
| `0x5` | `0x50000000` | Immediate / constant bank ref | Constant buffer slot encoding (case 429) |
| `0x6` | `0x60000000` | Control / modifier word | Inline flag assembly, appears in every case |
| `0x7` | `0x70000000` | Literal value | `sub_6D9580` literal encoder (case 61) |
| `0x9` | `0x90000000` | Special register / address | `sub_A99F50` for pred-reg, vtable calls for SR |
| `0xF` | `0xF0000000` | Null / padding sentinel | `1879048192 == 0x70000000` unused-reg placeholder |

#### Case Enumeration (119 cases, 103 distinct types)

The switch groups by instruction category. Cases sharing a body are listed together. The format ID column gives the 3rd argument to `sub_931690` (primary encoder) or `sub_934630` (secondary/multi-record encoder).

| Cases | Ori Opcode(s) | Category | Format ID | Operand pattern |
|-------|---------------|----------|-----------|-----------------|
| 61, 455 | BAR, WMMA.store_d_col_f32_shared | Barrier / WMMA store (literal path) | 18 (primary), 17 (datatype extension), 124 (literal-only) | dst, src0, src1 via `sub_C01520`; control word with SM100+ knob 4176 gate |
| 64, 66 | SETLMEMBASE, DEPBAR | LMem base / dep-barrier | 26 | Sub-switch on `(modifier>>3)&7` for 6 operand modes |
| 69, 70 | JMP, JMX | Jump / jump-indirect | -- | Delegated to `sub_6D9310` |
| 207 | (extended) | Multi-record loop encoding | 239 | Predicate + 2 sources; emits `v452` records in a loop |
| 221 | (extended) | Conditional branch variant | 107 (via `sub_934630`) | Two sub-paths: mode 1 = operand + 2 words, mode 2 = 1 word |
| 416, 417 | WMMA.load_c (global/shared) | MMA load C | vtable(type) | dst + 1 source + optional `0x60000000` + modifier |
| 418--420, 431, 434, 436, 445--448 | WMMA.mma variants (10 cases) | MMA compute | vtable(type) | dst + 2 sources + 4 control words; vtable dispatch for format ID |
| 423 | WMMA.mma (col_col_f32_f16_sat) | MMA 2-source | 20 | dst + 2 operands + control; multi-record if `v452 > 0` |
| 424 | WMMA.mma (col_col_f32_f32) | MMA 3-source | 21 | dst + 3 operands + control; multi-record |
| 425 | WMMA.mma (col_col_f32_f32_sat) | MMA passthrough | variable | dst + 1 source; format from `v284` |
| 429 | WMMA.mma (col_row, f16 accum) | MMA with const-bank | 86 + 130 (secondary) | Complex: `sub_934630` for const-bank slots with `0x50000000` prefix, then 2 sources + control |
| 437 | WMMA.mma (row_col_f16_f32_sat) | MMA 2-source | 162 | dst + 2 operands + control; multi-record |
| 438 | WMMA.mma (row_col_f32_f16) | MMA 1-source | 67 | dst + 1 operand + control |
| 439 | WMMA.mma (row_col_f32_f16_sat) | MMA 4-operand | 166 | dst + 3 sources + control word |
| 441 | WMMA.mma (row_col_f32_f32_sat) | MMA 4-op + modifiers | 274 | dst + 3 sources + extended modifier assembly |
| 442 | WMMA.mma (row_row_f16) | MMA 4-op | 275 | dst + 3 sources + modifier word |
| 443 | WMMA.mma (row_row_f16_sat) | MMA 4-op | 276 | dst + 3 sources + conditional modifier (nested if on `instr+12` bits) |
| 444 | WMMA.mma (row_row_f16_f32) | MMA variable-length | -- | Loop over operands via `sub_C01A90`; delegated commit |
| 449, 450, 453 | WMMA.store_d variants | WMMA store | -- | Delegated to `sub_6CC000`/`sub_6CC1F0`/`sub_6CBEC0` |
| 456, 466 | WMMA.store_d (row) / MEMBAR | WMMA store / membar | -- | Delegated to `sub_6CC390` |
| 460 | WMMA.store_d (row_f32_global) | WMMA store conditional | 31 (via `sub_934630`) | Conditional on `instr+17` sign bit; multi-record emit |
| 461 | WMMA.store_d (row_f32_shared) | MMA 4-op + conditional | 211 | dst + 3 sources + up to 1 extra source; multi-record |
| 462 | WMMA.load_a (col) | MMA load | 81 | Predicate + 1 source + control |
| 463--465 | WMMA.load (global/col_global/a_row) | MMA load variants | 58, 69, 34 | dst + 1--2 sources + control word |
| 467 | WMMA.load (a_row_global) | MMA load + modifier | 138 (or 145/146) | dst + 2 sources + data-type-dependent format |
| 468 | WMMA.load (a_row_shared) | MMA load + loop | 119 | dst + 3 sources + loop emit for `v452` records |
| 469 | (extended MMA) | MMA control-flow | 51 | dst + 3 sources + control with data-type comparison |
| 470 | (vtable SR) | Special-register encode | 203 | vtable dispatch for SR index; `0x90000000` prefix |
| 471 | (data-typed) | Typed ALU | 202 | Data-type via `sub_7D6860`; dst + 3 sources + modifier |
| 472--474 | (extended) | Simple control word | 253, 252, 250 | dst + 1--2 sources + control |
| 475 | (extended) | Complex multi-source | 242 / 156 | dst + 3 sources + extended modifier; architecture flag gate |
| 476 | (extended) | Conditional 2-path | 287 / 210 | Branches on `instr+16` bit 0: 2-source or predicate-encode path |
| 477--480 | (extended simple) | 1-word instructions | -- | Control word only; minimal encoding |
| 482--490 | (extended range) | Mixed formats | variable | Cases 482--488: 1-source + control; 489--490: simple control |
| 491 | (extended) | MMA multi-record | 174 | dst + 3 sources + complex multi-record with 14 goto-label state machine |
| 492 | (extended) | 2-source + flag | 24 | dst + 2 sources + 1-bit flag control |
| 493 | (extended) | 2-source + flag | 209 | Same pattern as 492, different format |
| 494--499 | (delegated) | Specialized handlers | -- | Each delegates to a unique sub-function (`sub_6D1F30`, `sub_6CE200`, `sub_6CE040`, `sub_6CED80`, `sub_6CF270`, `sub_6CF180`) |
| 500 | (extended) | Multi-mode ALU | 186 | 2 sources + sub-switch on `(modifier>>1)&7` (6 data-type modes) |
| 501--504 | (extended simple) | Minimal | -- | 1 source or control-only |
| 505 | (extended) | 2-source | 35 | dst + 2 sources + control |
| 506 | (extended) | Delegated | -- | Calls `sub_6D1B90` |
| 507, 508 | (extended) | Identical format | 226 | Simple 1-word encoding |
| 509 | (extended) | Delegated | -- | Calls `sub_6D1B90` |
| 510 | (extended) | TMEM/TCGen05 | 323 / 309 (secondary) | Mode switch on `modifier&3`: mode 2 = 8-word `sub_934630` record; else register + control |
| 511 | (extended) | Delegated | -- | Calls `sub_6D1EB0` |
| 512 | (extended) | TMEM load | 315 | Null sentinel + conditional encode + register + mode-word |
| 513 | (extended) | TMEM store | 316 | 1 source + optional register + mode-word |
| 514 | (extended) | TMEM fence | 303 (secondary) | Zero-operand `sub_934630` |
| 515 | (extended) | TMEM alloc/dealloc | 302 (secondary) | 3-way mode switch; each emits 2-word `sub_934630` |
| 516 | (extended) | TMEM prefetch | 307 | dst + 1 source + control |
| 517, 518 | (extended simple) | Minimal | -- | Control-only |
| 519 | (extended) | TCGen05 fence | 298 (secondary) | Zero-operand `sub_934630` |
| 520 | (extended) | TCGen05 ctrl | 313 (secondary) | Zero-operand `sub_934630` |
| 521 | (extended) | TCGen05 commit | 277 (secondary) | 1-word `sub_934630` |
| 522--527 | (extended simple) | Minimal / paired | -- | Control-only; 526+527 share body |
| 528 | (extended) | TCGen05 op | 305 | Simple 1-word |
| 529 | (extended) | TCGen05 op | 306 | 2 sources |
| 530 | (extended) | TCGen05 op | 304 | 2 sources |
| 531, 532 | (extended simple) | Minimal | -- | Control-only |
| 533 | (extended) | TCGen05 multi | 322 (secondary) | 3-way mode switch; each emits 2-word `sub_934630` |
| 534 | (extended) | TCGen05 wide | 320 (secondary) | 4-word `sub_934630` |
| 535--537 | (extended simple) | Minimal | -- | Control-only |
| 538, 539 | (extended) | Barrier ops | 293, 294 | 1 source + control |
| 540 | (extended) | Barrier/sync | 218 | Simple 1-word |
| 541 | (extended) | Barrier variant | 296 | 2 sources |
| 542 | (extended) | Barrier variant | 295 | 1 source; format arg from `instr+176` |
| 543 | (extended) | Multi-record barrier | 286/23 (secondary) | 2 `sub_934630` calls: 286 (12 cols) then 23 |
| 544 | (extended) | Multi-record wide | 23/272 (secondary) | 3 `sub_934630` calls: two format-23 then format-272 |

Encoding details:
- Instructions are encoded as sequences of 8-byte words
- Operand word type prefix in bits `[31:28]`: `0x1` = register, `0x5` = immediate/constant, `0x6` = control/modifier, `0x7` = literal, `0x9` = special
- Control words carry the `0x60000000` prefix
- Architecture-specific bits accumulated in a flags variable, with SM 100+ extensions via knob 4176
- `sub_7D6860` handles data type encoding (FP32/FP64/INT, etc.)
- `sub_C00BF0` provides opcode lookup from the encoding tables
- `sub_91D160` handles register operand encoding

### Instruction Word Format

The Mercury instruction word is a 1280-bit (160-byte, 20-QWORD) structure located at offset `+544` in the encoder object. All bit-field insertions use `sub_7B9B80`:

```c
// sub_7B9B80 -- bitfield insert (216 bytes, 18,347 callers)
// Signature: (encoder_obj, bit_offset, bit_width, value)
void bitfield_insert(void *a1, int bit_offset, int bit_width, uint64_t value) {
    uint64_t mask = (1ULL << bit_width) - 1;
    int qword_idx = bit_offset >> 6;
    int bit_pos   = bit_offset & 63;
    *(uint64_t *)(a1 + 8 * qword_idx + 544) |= (value & mask) << bit_pos;
    // handles cross-QWORD boundary cases in a loop up to bit 1280
}
```

Two companion helpers run before operand encoding:
- `sub_7B9D30` (38 bytes) -- clears the 16-entry constant buffer slot table at `a1+468` to `0xFF`
- `sub_7B9D60` (408 bytes) -- encodes reuse flags (1 bit) and predicate register index (5 bits) into the instruction word

### Encoding Table Functions (530 functions)

The range `0xC66000`--`0xD27000` contains 530 functions that each initialize one row of the instruction format table. Every function calls `sub_7B9B80` multiple times to describe the SASS bit layout for one instruction format variant:

```c
// Example: sub_C6CF40 — one instruction format initializer
void init_format_XYZ(void *a1) {
    sub_7B9B80(a1, 0,    4, 1);       // bits[0:3]   = opcode field = 1
    sub_7B9B80(a1, 4,    3, 0);       // bits[4:6]   = format = 0
    sub_7B9B80(a1, 8,    9, 0xC);     // bits[8:16]  = subopcode = 12
    sub_7B9B80(a1, 0x11, 8, 0x13);    // bits[17:24] = modifier = 19
    sub_7B9B80(a1, 0x19, 7, 5);       // bits[25:31] = unit = 5
}
```

Function sizes are remarkably uniform (1000--1600 bytes), reflecting mechanical code generation -- roughly 10 functions per ISA opcode group, covering all SASS formats for SM 100+.

## Stage 2: MercExpandInstructions -- Pseudo-Instruction Expansion

| | |
|---|---|
| **Phase** | 118 |
| **Entry** | `sub_C3CC60` (26KB, MercExpand::run) |
| **Strings** | `"After MercExpand"`, `"EXPANDING"` |

Mercury uses abstract instruction forms that may map to multiple real SASS instructions. This phase expands every pseudo-instruction into its concrete SASS equivalent sequence. The expansion is type-dispatched:

| Handler | Size | Instruction class |
|---|---|---|
| `sub_C37A10` | 16KB | General instruction expansion (jump table with 4+ cases) |
| `def_C37B2E` | 13KB | Complex expansion cases (default handler, creates new nodes) |
| `sub_C39B40` | 10KB | Memory operations (LDG, STG, LDS, etc.) |
| `sub_C3A460` | 6KB | Atomic operations |
| `sub_C3B560` | 8KB | Texture operations |
| `sub_C3BCD0` | 19KB | Control flow (branches, jumps, calls) |

`sub_C3CC60` iterates over every instruction in the function, dispatching to the appropriate handler. Handlers create new instruction nodes, link them into the list, and delete the original pseudo-instruction. After all expansions, `sub_C3E030` (18KB) performs finalization and cleanup.

#### sub_C37A10 -- expandInstruction jump table

This handler processes general pseudo-instructions. It reads the operand record at `instr+16`, extracts the Ori opcode from field `[2]` (offset +8), and maps it through `byte_22B7B60[opc]` (52-entry LUT, range 0..0x33) to a slot index (default 27 for out-of-range). It then applies predication from field `[4]`: mode 2 = conditional-true (`sub_8F0DC0(slot, 1)`), mode 3 = conditional-false (`sub_8F0DC0(slot, 0)`), mode 4 = uniform (`sub_8F0DF0(slot)`). The main switch dispatches by opcode:

| Case(s) | Vtable offset | Action | Operand source |
|---|---|---|---|
| 0 | +16 (`emitArg`) | Emit 2-operand word: mode=10, val=`[6]`, aux=`[7]` | `instr[6..7]` |
| 1 | +16 (`emitArg`) | Emit 2-operand word: mode=10, val=`[5]`, aux=`[7]` | `instr[5,7]` |
| 2--6, 8--9, 0xB--0x11, 0x1B--0x21, 0x2C--0x33 | +128 (`setValue`) | Set slot value to `instr[7]`; internal state machine (11 sub-cases on record byte 0: states 0--5,7--8 = direct store; 6,9 = linked-list flush; 0xA = conditional release) | `instr[7]` |
| 7, 0xA, 0x12, 0x22--0x24, 0x27--0x2A | +112 (`getValue`) | Read current slot value; same 11-state machine, then set record byte to state 4 | (read-only) |
| 0x17 | +128 (`setValue`) | Set slot value to `instr[7]`, then propagate side flag: if `a4[0]==-1` set it to `instr[11]`, else if mismatch set `instr+132 |= 0x1000` | `instr[7,11]` |
| 0x18--0x1A | +72/+120/+128 | Conditional max: if `isAllocated(slot)` and `getValue(slot) < instr[7]`, update to `instr[7]`; same 11-state machine on setValue | `instr[7]` |
| 0x25--0x26 | +24 (`pushWord`) | Triple push: `pushWord(slot, 5, instr[10])`, `pushWord(slot, 5, instr[9])`, `pushWord(slot, 5, instr[7])` | `instr[7,9,10]` |
| 0x2B | +24 (`pushWord`) | Single push: `pushWord(slot, 12, instr[5])` | `instr[5]` |

Overflow to `def_C37B2E` (at `0xC38180`, 13KB) handles complex cases that create new instruction nodes rather than modifying slot state.

#### sub_C3BCD0 -- expandControlFlow SASS sequence

This handler expands a single control-flow pseudo-instruction into an 8-instruction SASS sequence. It first hashes `instr+32` with FNV-1a (prime 16777619, basis `0x811C9DC5`) to look up a dedup cache at `ctx+488`. Each new instruction is allocated by `sub_10B1EE0`, configured by `sub_10AE590` (field setter) and `sub_10AE3F0`/`sub_10AE640` (operand setters), linked by `vtable+136` (list splice), then committed by `sub_10AF260`.

The emitted sequence (8 instructions, in order of creation):

| # | Opcode (dec) | SASS mnemonic | Field config | Operands |
|---|---|---|---|---|
| 1 | 270 | `TCATOMSWS` | field(118, 519), nops=1 | reg(src_class, 8, 1) |
| 2 | 270 | `TCATOMSWS` | field(118, 519), nops=1 | reg(src_class, src_phys, 1), reg(8, 0, 1) |
| 3 | 272 | `TCSTSWS` | field(118, 519), nops=1 | reg(src_class, v118, 1), reg(src_class, src_phys, 1), imm(6, 57, 1) |
| 4 | 39 | `FLO` | field(98, 452) + field(480, 2481), nops=2 | reg(src_class, src_phys, 1) x2, reg(src_class, v118, 1), val(2) |
| 5 | 47 | `I2F` | field(480, 2481), nops=1 | reg(src_class, src_phys, 1) x2 |
| 6 | 54 | `BMOV_B` | field(29, 126), nops=1 | reg(src_class, src_phys, 1) x3, imm(11, 0, 2) |
| 7 | 32 | `VABSDIFF4` | field(332, 1853) + field(398, 2117), nops=1 | reg(src_class, v118, 1), reg(src_class, src_phys, 1), copy(instr+48 + 128) via `sub_10AFAF0`, reg(src_class, v118, 1) |
| 8 | 270 | `TCATOMSWS` | field(118, 519), nops=1 | reg(src_class, src_phys, 1), reg(8, 0, 1) |

After the 8-instruction sequence, `sub_C35F90` (architecture validator) runs on the result, and `sub_10ADF90` splices the new nodes into the instruction list replacing the original pseudo-instruction. The field IDs (118, 98, 480, 29, 332, 398) correspond to the Mercury field namespace documented in Stage 1; the values (519, 452, 2481, 126, 1853, 2117) are encoding-table indices that select the concrete SASS bit-field layout for each instruction.

The expansion engine also uses `sub_719D00` (50KB), which builds output for expanded instructions across different operand widths (32/64/128-bit, predicate). The four nearly identical code blocks within that function correspond to template instantiations over operand width types.

## Stage 3: WAR Hazard Resolution (Phases 119, 121)

| | |
|---|---|
| **Phases** | 119 (MercGenerateWARs1), 121 (MercGenerateWARs2) |
| **Entry** | `sub_6FC220` / `sub_6FC240` |
| **Main pass** | `sub_6FBC20` (7.4KB) |
| **String** | `"After MercWARs"` |
| **Knob** | #16 (WAR generation control) |

Write-After-Read hazards occur when an instruction reads a register that a later instruction will overwrite -- the hardware pipeline can execute them out of order, causing the read to see the wrong value. The WAR pass inserts explicit DEPBAR (dependency barrier) instructions and scoreboard annotations to force correct ordering.

Two passes are needed: WAR1 runs after expansion but before opex, and WAR2 runs after opex. The second pass exists because opex itself introduces new instructions (scoreboard waits, synchronization barriers) that create additional WAR hazards not present in the pre-opex stream.

### WAR Pass Algorithm -- `sub_6FBC20`

```c
// Simplified WAR generation pass
void GenerateWARs(context) {
    // Guard conditions
    if (!(context->instr_flags & 1))  return;  // no WAR-sensitive instrs
    if (context->mode != 2)           return;  // not Mercury mode

    // Per-instruction walk
    for (instr = first; instr != end; instr = instr->next) {
        // Detect hazard
        int severity = DetectWARHazard(state, instr);  // sub_6FA5B0

        if (severity >= 3) {
            InsertScoreboardBarrier(state, instr);       // sub_6FA930, opcode 54
            InsertWAITDP(state, instr);                  // sub_6FA7B0, opcode 246
            InsertWARStalls(state, instr, severity);     // sub_6FAA90
        }
    }

    PostWARAdjustment(state);    // sub_6FB850
    FinalizeWARPass(state);      // sub_6FB350
}
```

### WAR Hazard Detection -- `sub_6FA5B0` (2.5KB)

The detector uses a three-stage opcode filter to classify each instruction.

**Stage 1 -- first bitmask `0x100000400001` (opcodes 34--78).** The range check `(opcode - 34) > 0x2C` admits opcodes 34--78. Within that window the 45-bit bitmask selects three opcodes for architecture-specific vetting via vtable +968 / +1008:

| Bit | Opcode | Mnemonic | Meaning |
|-----|--------|----------|---------|
| 0   | 34     | IDE      | Vtable-gated -- non-hazardous only if arch confirms |
| 22  | 56     | BMOV     | Vtable-gated |
| 44  | 78     | RTT      | Vtable-gated |

All 42 other opcodes in 34--78 (I2I, I2IP, IMNMX, POPC, FLO, FCHK, IPA, MUFU, F2F, F2F_X, F2I, F2I_X, I2F, I2F_X, FRND, FRND_X, AL2P, AL2P_INDEXED, BREV, BMOV_B, BMOV_R, S2R, B2R, R2B, LEPC, BAR, BAR_INDEXED, SETCTAID, SETLMEMBASE, GETLMEMBASE, DEPBAR, BRA, BRX, JMP, JMX, CALL, RET, BSSY, BREAK, BPT, KILL, EXIT) have their bitmask bit clear and proceed directly to stage 2.

**Stage 2 -- second bitmask `0x800200000100001` (opcodes 71--130) plus opcode 235.** An explicit equality test marks opcode 235 (UBLKRED) as never-hazardous. The range check `(opcode - 71) > 0x3B` admits opcodes 71--130. Within that window the 60-bit bitmask flags four opcodes as unconditionally non-hazardous:

| Bit | Opcode | Mnemonic | Rationale |
|-----|--------|----------|-----------|
| 0   | 71     | CALL     | Control flow -- no register write hazard |
| 20  | 91     | AST      | Attribute store -- write-only |
| 45  | 116    | PIXLD    | Pixel load -- separate pipe, no WAR |
| 59  | 130    | HSET2    | Half-precision set -- result is predicate |

If the combined flag (opcode==235 OR second-bitmask bit set) is nonzero the detector returns immediately with counter = 0 (no hazard). All opcodes outside both bitmask windows that are not 235 fall through to the vtable +688 check.

**Stage 3 -- opcode-specific hazard classification.** After the vtable +688 filter (arch-specific blanket non-hazard check), the remaining opcodes are classified:

| Category | Opcodes | Mnemonics | Action |
|----------|---------|-----------|--------|
| Always hazardous | 49, 92, 248 | FRND, OUT, VIADDMNMX | ++counter (severity 1) |
| Conditionally hazardous | 75 | BPT | hazardous unless `sub_10AE600(ctx, operand, 179)` succeeds |
| Severity 3 (medium) | 35 | I2I | via vtable +528 arch check |
| Severity 4 (high) | 35, 246 | I2I, VHMNMX | via vtable +504 arch check (I2I); unconditional (VHMNMX) |

The detector maintains per-instruction state:
- `*(DWORD*)(state+8)` -- WAR counter (incremented per detected hazard)
- `*(DWORD*)(state+12)` -- severity level (3 = medium, 4 = high)

For severity 3 or 4, `sub_6FA430` inserts `(severity - counter)` additional stall slots before the instruction, then advances the counter to match.

### Inserted Instructions

**DEPBAR / Scoreboard barrier** (opcode 54) -- `sub_6FA930`:
- Created when `*(BYTE*)(instr+48) & 0x10` is set (barrier-needed flag)
- Barrier type extracted from bits 7:5 of the flag byte
- Encoding: `*(DWORD*)(new_instr+56) = 4` (barrier format)
- Control bits: `*(DWORD*)(new_instr+48) &= 0xFFF83FFF | 0x50000`

**WAITDP** (opcode 246) -- `sub_6FA7B0`:
- Skipped if a WAITDP already exists at the insertion point
- Operands configured with codes 102/467 and 301/1520
- Uses FNV-1a hash lookup for instruction deduplication

**FNV-1a dedup cache** -- `sub_6E4110` offset +128:

The emission pass maintains a hash map that caches previously emitted byte sequences keyed by `(instr_offset, block_base_addr)`.  When emitting the same logical instruction at the same block-relative position, the cache returns the prior encoded result and avoids redundant encoding work.  The cache lives in the emitter context at offsets `+128` (entry count), `+136` (bucket array pointer), `+144` (bucket count, always a power of two).

```
// --- FNV-1a hash over (instr_offset, block_addr) ---
// instr_offset = *(DWORD*)(instr+144)    // 4 bytes: assigned byte position
// block_addr   = current block base addr  // 8 bytes
fn fnv1a_dedup_key(instr_offset: u32, block_addr: u64) -> u32:
    h = 0x811C9DC5                          // FNV offset basis
    for byte in instr_offset.to_le_bytes(): // 4 rounds
        h = (h ^ byte) * 16777619           // 0x01000193
    for byte in block_addr.to_le_bytes():   // 8 rounds
        h = (h ^ byte) * 16777619
    return h

// --- cache lookup / insert ---
// bucket_array: pointer to array of 24-byte chained entries
// bucket_count: power-of-two bucket count (stored at ctx+144)
// entry layout (24 bytes):
//   +0  next_ptr   (u64) -- singly-linked chain within bucket
//   +8  block_addr (u64) -- key part 1
//   +16 instr_off  (u32) -- key part 2
//   +24 cached_val (i32) -- cached emission result (signed delta)
fn dedup_lookup(ctx, instr_offset, block_addr) -> Option<i32>:
    if ctx.entry_count == 0:
        return None                         // empty cache → miss
    h    = fnv1a_dedup_key(instr_offset, block_addr)
    slot = ctx.bucket_array + 24 * ((ctx.bucket_count - 1) & h)
    node = *(u64*)slot                      // head of chain
    while node != 0:
        if *(u64*)(node+8) == block_addr && *(u32*)(node+16) == instr_offset:
            return Some(*(i32*)(node+24))   // hit → cached delta
        node = *(u64*)node                  // follow chain
    return None                             // miss

// --- bypass conditions ---
// The cache is skipped entirely when:
//   1. instr_offset == -1  (instruction not yet assigned a position)
//   2. The instruction is a branch target (opcode == -1, pseudo-label)
//   3. The instruction carries a relocation flag (*(BYTE*)(instr+148) & 0x10)
```

The cached delta is used to compute a relocation adjustment: `delta = cached_val - block_addr`.  Both `sub_6E4110` and `sub_726E00` use the identical FNV-1a constants and per-byte folding pattern; `sub_726E00` hashes only the 4-byte instruction ID for its own microcode sequence cache at context offsets `+744`/`+752`/`+760`.

**Stall cycles** -- `sub_6FAA90` (7.9KB):
- Computes required stall cycles from architecture-specific latency tables
- Vtable methods at +888, +896, +904 for stall calculation
- GPU family dispatch: `v8[14] == 9` triggers specific handling
- Adjusts stall count fields in the instruction control word

## Stage 4: Opex -- Operation Expansion

| | |
|---|---|
| **Phase** | 120 (MercGenerateOpex) |
| **Entry** | `sub_703480` (1.4KB, RunOpexPass) |
| **MercOpex entry** | `sub_7032A0` (2.3KB, RunMercOpexPass) |
| **Body** | `sub_6FFDC0` (66KB) |
| **String** | `"After MercOpex"` |
| **Knobs** | #17 (expansion options), #743 (reduce-reg), #747 (dynamic batch) |

"Operation expansion" is the most complex stage. It generates the dependency scoreboards, computes latency waits, and inserts synchronization barriers that the hardware needs to manage instruction-level parallelism. After opex, the instruction stream contains all the scheduling metadata required for correct execution.

### Entry Points

Two entry paths exist, both calling the same `sub_6FFDC0` body:

**`sub_703480`** (RunOpexPass, 1.4KB):
1. Creates pipeline context via `sub_6FC280`
2. Queries knob #17 to disable WAR penalty flags: `*(context->flags+52) &= ~0x10`
3. Architecture check: `*(DWORD*)(context+52) == 20481` (SM 100a)
4. For SM 100a: queries knob at offset 1296/1304 for loop unroll factor
5. Sets Mercury mode: `*(DWORD*)(context+385) = 2`
6. Calls `sub_6FFDC0` for actual expansion

**`sub_7032A0`** (RunMercOpexPass, 2.3KB):
- Nearly identical to `sub_703480`
- Additionally calls `sub_10ADF10` to verify Mercury mode is active
- Allocates 40-byte + 24-byte records for Mercury-specific context
- Calls `sub_6FED20` to destroy previous Mercury context before creating new one

### Opex Body -- `sub_6FFDC0` (66KB)

This 66KB function (200+ locals, ~0x7A0 bytes stack frame) is the core of Mercury scheduling. It walks every basic block, computes latencies, allocates scoreboards, inserts waits and barriers, and sets stall counts. The six algorithmic steps execute in sequence within a per-block loop.

```
OpexBody(ctx):                                         // sub_6FFDC0
  // ── Initialization ────────────────────────────────
  func_obj    = *ctx
  is_mercury  = sub_10ADF10(func_obj)                  // Mercury mode?
  sb_count    = 0;  flags = {0,0,0,0}
  sub_6FC810(ctx, &sb_count, &flags[0..3], is_mercury) // query knobs #17/#743/#747
  if !ctx->byte_29:  goto fast_path                    // skip scoreboard if disabled

  // Allocate 4 tracking arrays sized by *(func+984) (max scoreboard index):
  //   A[0..N]=1 (occupied), B[0..N]=1 (refcount), C[0..N]=0 (pending waits)
  sub_BE21D0(tracking_state)                           // finalize array D from A+B+C

fast_path:
  if ctx->mode == 1:                                   // trivial single-block kernel
    sub_6FC3A0(ctx, 1, 1)                              // single-pass expansion
    goto done

  // ── Build block iteration order ───────────────────
  sub_8A4F20(block_order, func_obj)                    // RPO block list
  sub_6FF070(dep_ctx, func_obj, block_order, ctx->i8, sb_count)
  ref_A = alloc_refcount(3);  ref_B = alloc_refcount(3)
  sub_6FD5D0(ref_B);  sub_6FD550(ref_A)               // producer/consumer set init
  if is_mercury:
    merc_state = alloc(272)
    sub_8BE320(merc_state, func_obj, block_order, ctx->i8, ctx->end_ptr)

  // ── Step 1: Per-instruction latency lookup ────────
  //   sub_8A9D80: for each instruction in every block:
  //     latency_idx = instruction_legality[opcode * 4 + variant]
  //       (60416-entry table @ VA 0x22FEE00; 4 bytes/entry; 68% zero)
  //     if latency_idx & 0x08000000:  special-case handling (259 opcodes)
  //     raw_latency = latency_idx & 0xFFFF (cycle count)
  //     pipe_class  = latency_idx >> 16    (functional unit ID)
  //   Arch-specific overrides via vtable at func+39, offsets +72/+120
  //   Knob #743: compresses latency to reduce register lifetime
  //   Knob #747: applies DynBatch scaling factors (see SCHED-36)
  sub_8A9D80(tracking_state, func_obj, dep_ctx, block_order,
             ctx->end_ptr, &dep_root, flags[0..3],
             is_mercury, merc_state, ref_context)

  // ── Prepare per-block processing state ────────────
  sb_limit  = dep_ctx->i22 + 1                         // max scoreboard entries
  has_cap10 = vtable_query(func+39, 10);  has_cap87 = vtable_query(func+39, 87)
  // Allocate 7 scoreboard tables (sub_6FDD30): sb_tables[0]=256x56B (active
  // descriptors), [1..3]=8x56B (barrier groups), [4..5]=256x56B (wait state),
  // [6]=8x56B (membar). Auxiliary: hash_A/B 8x56B (dedup), reg_map 36x8B,
  // stall_vec 36x8B, dep_bitmap 773x8B (per_sm_scoreboard_configs: SM100 has
  // 75 entries x 88B, triplet={scoreboard_id, threshold=56, mask=0xFFFFFFFF})
  sub_BFAFC0(dep_bitmap, func_obj, ctx->end_ptr)      // init dependency bitmap
  sub_719D00(block_iter)                               // begin block iteration

  // ── Steps 2-6: Per-block loop ─────────────────────
  for block_id = 0 .. func_obj->i246:
    block_desc = func_obj->ptr_976[block_id]           // 40-byte block record
    sub_718C10(ctx->end_ptr, &block_desc)              // load block

    if is_mercury:
      cap = *(func+39 + 72)                           // vtable-derived knob state
      if !cap || (cap == 1 && *(func+39)->i4904):
        sub_8B6860(merc_state, &block_desc)            // Mercury block init
      else:
        sub_8A5CA0(merc_state)                         // simplified init

    // Step 2 ── Scoreboard entry allocation ──────────
    //   sub_8BDC40: for each producer instruction in block:
    //     key = encoding_bitfield_lookup[opcode_key]
    //       (4096 entries @ VA 0x23F2E00, pair {field_a, field_b})
    //     if key.field_b != 0xFFFFFFFF:
    //       sb_entry = alloc from sb_tables[0] (LRU evict if full)
    //       sb_entry.id chosen from per_sm_scoreboard_configs triplets
    //       mark register outputs as tracked by sb_entry
    sub_8BDC40(func_obj, dep_ctx, &opex_state, block_order,
               ctx->end_ptr, &block_desc)

    // Step 3 ── Wait insertion ───────────────────────
    //   sub_BFC850: scan consumer operands vs active scoreboards:
    //     if consumer reads reg owned by sb_entry with remaining_lat > 0:
    //       insert DEPBAR.WAIT before consumer (opcode 54, format=4)
    //       wait_mask |= (1 << sb_entry.scoreboard_id)
    //   Called twice: flag=1 (pre-scan), flag=0 (commit)
    if tracking_state[148]:
      sub_BFC850(dep_bitmap, &opex_root, &block_desc, 1, 0)  // pre-scan
    sub_8BB4D0(func_obj, tracking_state, &block_desc)  // update tracking
    sub_BFC850(dep_bitmap, &opex_root, &block_desc, 0, 0)    // commit waits

    // Step 4 ── Stall count computation ──────────────
    //   sub_BFE320: for each instruction:
    //     stall = max(0, producer_latency - distance)
    //     stall = min(stall, 15)          // 4-bit field in control word
    //     Knob #17: if WAR penalty disabled, skip penalty additions
    //     Arch vtable +888/+896/+904: per-pipe stall overrides
    //     Writes stall into control word bits [3:0]
    sub_BFE320(dep_bitmap, &block_desc)

    // Step 5 ── Memory barrier placement ─────────────
    //   sub_717C50: inserts MEMBAR/FENCE at block boundaries and before
    //   memory ops crossing consistency domains.  Only when mode > 2.
    //   Uses sb_tables[6] to coalesce redundant barriers per block.
    if ctx->mode > 2:
      sub_717C50(block_iter, &block_desc)

  // ── Cleanup (reverse-order destruction of all tables/ref-counted nodes) ──
done:
  func_obj->i385 = 2                                  // mark Mercury mode = done
```

**Step 6 -- Knob interaction**: knobs #17, #743, and #747 are not separate phases but modulate steps 1-5 throughout. `sub_6FC810` reads all three at initialization, storing derived flags in `flags[0..3]`:
- **Knob #17** (expansion options): clears bit 4 of `ctx->flags+52`, disabling WAR penalty additions in step 4. When set, `sub_BFE320` skips the WAR stall penalty term.
- **Knob #743** (reduce-reg): passed to `sub_8A9D80` in step 1. Compresses producer latencies so consumers schedule sooner, reducing live register count at the cost of more stalls.
- **Knob #747** (dynamic batch): also passed to `sub_8A9D80`. Applies DynBatch priority scaling factors (see SCHED-36) to latency values, favouring throughput over latency.

New instructions created by opex use `sub_10B1F90` (instruction allocator) and `sub_10AE590` (operand configuration).

## Stage 5: SASS Microcode Emission

| | |
|---|---|
| **Phase** | 122 (MercGenerateSassUCode) |
| **Entry** | `sub_6E4110` (24KB) |

The final stage converts the fully expanded, WAR-resolved, scoreboard-annotated Mercury stream into native SASS binary. This is the point of no return -- after this phase, the output is executable GPU machine code.

`sub_6E4110` takes 8 parameters (context, instruction list, descriptors, format info, ...) and dispatches to the per-instruction encoding pipeline:

```
sub_6E4110 (24KB, final SASS emission)
  ├─ sub_735290 — per-instruction encoding pipeline
  │    ├─ sub_733FA0 (5.1KB)  — encode instruction operands
  │    │    └─ sub_733870 (10KB) — source operand encoder
  │    ├─ sub_734370 (6.1KB)  — encode immediates
  │    ├─ sub_734820 (4.1KB)  — encode predicates
  │    ├─ sub_734AD0 (3.3KB)  — encode memory operands
  │    └─ sub_734D20 (8.1KB)  — encode complex operands (texture/surface/barrier)
  ├─ sub_726E00 (30.6KB) — instruction encoding with FNV-1a dedup cache
  │    └─ sub_7266A0 (11.7KB) — hash table lookup (24-byte entries, separate chaining)
  ├─ sub_6E3F80 (2.2KB) — encode branch offsets
  ├─ sub_6E3560 (2.6KB) — finalize scheduling control words
  └─ sub_712E70 (9.6KB) — handle relocations (cross-BB branch targets)
```

The encoding pipeline uses FNV-1a hashing (seed `0x811C9DC5`, multiplier `16777619`) to cache instruction encodings and avoid re-encoding identical instructions.

## Architecture-Specific Dispatch

Architecture selection reads `*(int*)(config + 372) >> 12` to determine the SM generation. A vtable at `*(context+416)` (equivalently `*(compilation_state+1584)`) with 400+ slots provides per-architecture behavior for encoding, latency tables, resource queries, and hazard rules. 41 slots have confirmed call sites; the highest observed is slot 402 (+3216).

| SM generation | `config+372 >> 12` | SM versions |
|---|---|---|
| Kepler | 3 | sm_30--sm_37 |
| Maxwell | 5 | sm_50--sm_53 |
| Pascal | 6 | sm_60--sm_62 |
| Volta/Turing | 7 | sm_70--sm_75 |
| Ampere | 8 | sm_80--sm_89 |
| Hopper | 9 | sm_90--sm_90a |
| Blackwell | (10+) | sm_100--sm_121 |

The encoder state initializer `sub_6E8EB0` (64KB) sets architecture-specific flags and populates the opcode descriptor table (40+ entries mapping internal opcode IDs to encoding words). For SM 80 (`0x5000`) it sets bits 1 and 8; for SM 84 (`0x5004`) it sets bits 16 and 64.

Vtable dispatch helpers at `0xC65530`--`0xC656E0`:
- `sub_C65530` -- 3-key dispatch (opcode, subop1, subop2), binary search through 24-byte table entries
- `sub_C65600` -- instruction-keyed dispatch, reads keys from `instr+12/14/15`
- `sub_C656E0` -- instruction-keyed dispatch with fallback to default handler `sub_9B3020`

### Architecture Vtable Method Catalog

The vtable is accessed as `**ptr` where `ptr = *(compilation_state + 1584)` or `*(pipeline_context + 416)`. Each slot is 8 bytes (function pointer). Methods are grouped by functional area; "refs" is the number of distinct decompiled call sites.

**Scheduling class / latency** (most heavily used):

| Slot | Offset | Refs | Signature | Purpose |
|---|---|---|---|---|
| 113 | +904 | 48 | `(self, opcode) -> int` | Get scheduling class for opcode (returns pipe index 0--7). Called by every latency, stall, and scoreboard computation. Also called with no args for default class. |
| 79 | +632 | 15 | `(self[, opcode, ...]) -> int` | Get register-file width for type. 0--5 args depending on context (spill cost, operand width, encoding bit-count). Bitmask `0x1008E0E` in `sub_A9BD00` gates translation to arch default. |
| 86 | +688 | 4 | `(self, context) -> ptr` | Create architecture-specific scheduling annotation record |
| 78 | +624 | 1 | `(self, type) -> int` | Get operand bit-width for negative type codes |
| 5 | +40 | 1 | `(self, sched_class) -> int` | Get register bank count for scheduling class |

**Resource query** (read-only architecture parameters):

| Slot | Offset | Refs | Default stub | Purpose |
|---|---|---|---|---|
| 36 | +288 | 3 | `nullsub_135` | Post-scheduling fixup hook |
| 62 | +496 | 1 | `nullsub_195` | Mid-expansion arch-specific hook |
| 135 | +1080 | 1 | `sub_7D70F0` | Get basic block extension data (default returns bb+40) |
| 161 | +1288 | 1 | -- | Has extended register encoding (gates alternate encode path) |
| 163 | +1304 | 1 | `sub_744F70` | Has uniform register reuse |
| 164 | +1312 | 1 | `sub_7D7230` | Has TMEM support |
| 165 | +1320 | 1 | `sub_7D7240` | Has extended operand modes |
| 209 | +1672 | 1 | -- | Has wide-register merge support |
| 240 | +1920 | 1 | -- | Has predicate-register dual-issue support |
| 247 | +1976 | 1 | `sub_7D7630` | Has extended memory type support |
| 294 | +2352 | 1 | `sub_745030` | Has post-RA scheduling fixup |
| 295 | +2360 | 1 | `sub_745040` | Has control-flow scheduling override |
| 402 | +3216 | 1 | -- | Supports instruction-level predicate promotion |

**Hazard detection** (accessed through `*(pipeline_context+416)` in WAR pass `sub_6FA5B0`):

| Slot | Offset | Refs | Purpose |
|---|---|---|---|
| 63 | +504 | 3+ | Architecture-specific WAR fixup emitter |
| 66 | +528 | 3+ | WAR hazard filter predicate / instruction legality check |
| 121 | +968 | 4+ | Is operand subject to WAR penalty (per-arch rule) |
| 126 | +1008 | 6+ | Is operand safe from WAR (cross-check negation of slot 121) |

**Operand / encoding support**:

| Slot | Offset | Refs | Signature | Purpose |
|---|---|---|---|---|
| 70 | +560 | 1 | `(self, instr, operand) -> bool` | Is operand a predicate-guarded source |
| 81 | +648 | 1 | `(self, instr, src, dst_list, n, out) -> bool` | Try constant-buffer operand coalescing |
| 82 | +656 | 1 | `(self, instr, modifier) -> void` | Patch modifier for operand reuse encoding |
| 125 | +1000 | 4 | `(self, context, flag) -> void` | Post-legalization instruction fixup |
| 128 | +1024 | 1 | `(self, instr) -> bool` | Does instruction require extended encoding space |
| 190 | +1520 | 4 | `(self, instr, operand_idx) -> int` | Get operand encoding class for modifier dispatch |
| 201 | +1608 | 1 | `(self, out, self2, instr, src_idx, flag) -> void` | Emit arch-specific operand fixup sequence |
| 207 | +1656 | 1 | `(self, operand_record) -> int` | Get address computation stride for memory operand |
| 218 | +1744 | 1 | `(self, instr, operand_idx) -> double` | Get spill cost weight for operand (FP return) |
| 223 | +1784 | 1 | `(self, src_instr, cb_rec, buf1, buf2) -> void` | Emit constant-buffer materialization sequence |

**Instruction legalization / rewrite**:

| Slot | Offset | Refs | Signature | Purpose |
|---|---|---|---|---|
| 172 | +1376 | 1 | `(self) -> bool` | Supports PRMT.B32 rewrite (default `sub_744F80`) |
| 173 | +1384 | 1 | `(self) -> bool` | Supports PRMT extended modes (default `sub_744F90`) |
| 182 | +1456 | 6 | `(self, instr) -> bool` | Is instruction eligible for predication |
| 187 | +1496 | 3 | `(self, opcode, type) -> bool` | Can opcode accept type as direct operand |
| 188 | +1504 | 1 | `(self, instr, flag, out) -> bool` | Try modifier legalization |
| 193 | +1544 | 1 | `(self, instr, type) -> bool` | Is type-specific encoding legal |
| 194 | +1552 | 3 | `(self, instr, type, subtype) -> bool` | Can combine two operand types in one instruction |
| 197 | +1576 | 3 | `(self[, instr]) -> int` | Get constant-buffer slot limit |
| 198 | +1584 | 2 | `(self, instr, target) -> bool` | Is cross-block branch rewrite legal |
| 224 | +1792 | 1 | `(self, instr, src_idx, out) -> bool` | Try immediate-to-constant-buffer promotion |
| 225 | +1800 | 4 | `(self, instr) -> bool` | Is instruction a uniform-register consumer |
| 226 | +1808 | 1 | `(self, instr, src, dst) -> int` | Get register bank conflict penalty |
| 227 | +1816 | 1 | `(self, instr) -> bool` | Is instruction eligible for post-RA optimization |

The separate scheduling vtable at `0x21DBC80` (77 entries, 32 unique functions) is a per-pipe-class dispatch table. It has three pipeline groups (A/B/C) of 23 entries each mapping pipe index to latency/throughput query functions, preceded by 8 core scheduling methods at `0x8DA680`--`0x8DC620` (stall computation, barrier allocation, yield threshold).

## Data Structures

### Mercury Instruction Word

```
Offset  Size    Field
------  ------  --------------------------------------------------
+0      8B      vtable pointer (encoder object)
+468    64B     Constant buffer slot table (16 x DWORD, cleared to 0xFF)
+532    4B      Constant buffer slot count
+544    160B    Instruction word (1280 bits = 20 QWORDs)
                — populated by sub_7B9B80 bitfield inserts
                — max addressable bit: 1280
```

### SASS Encoding Record (~264 bytes)

Output of `sub_6D9690`. Contains the encoded instruction words, operand data, and metadata. The encoding base constant is `0x2000000000LL`.

### Pipeline Context

```
Offset  Size    Field
------  ------  --------------------------------------------------
+52     4B      Architecture ID (20481 = sm100a)
+236    1B      Uses shared memory flag
+284    4B      Function flags (bits 0, 3, 7 checked by WAR pass)
+385    4B      Mercury mode flag (2 = Mercury/Capsule mode)
+416    8B      Architecture vtable pointer (400+ slots, 41 confirmed -- see catalog above)
```

### Scheduling Control Word (per SASS instruction)

```
Offset  Size    Field
------  ------  --------------------------------------------------
+48     4B      Control bits (barrier flags at bits 17:13)
+56     4B      Encoding format (4 = barrier format)
+144    4B      Scheduling slot
+164    4B      Resource class
+168    1B      Stall bits
+236    4B      Latency value
```

## Mercury Instruction Node Layout

The Mercury pipeline (phases 117--122) operates on its own instruction representation, distinct from the 296-byte Ori IR instruction node documented in [Instructions & Opcodes](../ir/instructions.md). The master encoder `sub_6D9690` (phase 117) reads Ori IR nodes and produces Mercury instruction nodes; all subsequent phases -- expansion, WAR resolution, opex, and SASS emission -- operate exclusively on Mercury nodes.

### Allocation

Mercury instruction nodes are allocated by `sub_10AF8C0` (92 lines), which either recycles a node from a per-block free list or allocates exactly **160 bytes** from the arena. The primary API wrappers are `sub_10B1F90` and `sub_10B1EE0`, which call `sub_10AF8C0` and perform additional bookkeeping (FNV-1a deduplication cache registration, scheduling state propagation).

### Node Layout (160 bytes)

| Offset | Size | Type | Init value | Field | Description |
|--------|------|------|-----------|-------|-------------|
| +0 | 8 | `ptr` | 0 | `next` | Forward pointer in per-block doubly-linked list |
| +8 | 8 | `ptr` | 0 | `prev` | Backward pointer in per-block doubly-linked list |
| +16 | 8 | `ptr` | source loc | `source_loc` | Source location copied from context (slot 124) |
| +24 | 4 | `u32` | 772 (0x304) | `node_type` | Constant type marker -- never modified after init |
| +28 | 2 | `u16` | 0xFFFF | `opcode` | SASS opcode number (0xFFFF = sentinel / BB boundary) |
| +30 | 1 | `u8` | 0xFF | `sub_key_1` | Encoding sub-key 1 (format variant selector) |
| +31 | 1 | `u8` | 0xFF | `sub_key_2` | Encoding sub-key 2 (modifier selector) |
| +32 | 4 | `u32` | counter | `sequence_id` | Monotonically increasing ID; FNV-1a dedup key |
| +36 | 4 | --- | --- | (padding) | Alignment to 8-byte boundary |
| +40 | 8 | `ptr` | ctx | `context_ptr` | Back-pointer to allocator / code-object base |
| +48 | 8 | `u64` | 0 | `encoded_data_0` | Encoded operand / property data |
| +56 | 8 | `u64` | 0xFFFFFFFF | `sentinel_56` | Sentinel / uninitialized marker |
| +64 | 8 | `u64` | 0 | `encoded_data_1` | Encoded operand / property data |
| +72 | 8 | `u64` | 0 | `encoded_data_2` | Encoded operand / property data |
| +80 | 8 | `u64` | 0 | `encoded_data_3` | Encoded operand / property data |
| +88 | 8 | `i64` | -1 | `sentinel_88` | Sentinel (end-of-data marker) |
| +96 | 8 | `i64` | -1 | `sentinel_96` | Sentinel |
| +104 | 8 | `u64` | 0xFFFFFFFF | `sentinel_104` | Sentinel |
| +112 | 8 | `u64` | 0 | `reserved_112` | Reserved (zeroed) |
| +120 | 8 | `u64` | 0 | `reserved_120` | Reserved (zeroed) |
| +128 | 8 | `ptr` | alloc'd | `sched_ctrl_ptr` | Pointer to 60-byte scheduling control record |
| +136 | 8 | `ptr` | ctx sched | `sched_context` | Context scheduling state (context slot 52) |
| +144 | 8 | `i64` | 0xFFFFFFFF | `sched_slot` | Scheduling slot (sentinel = unscheduled) |
| +148 | 4 | `u32` | 0 | `node_flags` | Node flags (bit 1 = BB boundary, bit 10 = 0x400) |
| +152 | 4 | `u32` | 0xFFFFFFFF | `block_seq` | Basic-block sequence number |

The opcode field at +28 carries the Mercury/SASS opcode number. Known values include: 0xFFFF (sentinel, BB boundary marker), 54 (DEPBAR -- dependency barrier), 246 (WAITDP -- wait for dependency pipeline). All other values are SASS instruction opcodes.

### Scheduling Control Record (60 bytes)

Each Mercury instruction node points (via +128) to a separately allocated 60-byte scheduling control record. This record carries barrier state, stall counts, and encoding format metadata that the WAR and opex passes read and modify.

| Offset | Size | Type | Init | Field | Description |
|--------|------|------|------|-------|-------------|
| +0 | 16 | `xmm` | SSE const | `header` | SSE-initialized from `xmmword_2027620` |
| +16 | 16 | `xmm` | SSE const | `latency` | SSE-initialized from `xmmword_202DC90` |
| +32 | 1 | `u8` | 0 | `flag_32` | General-purpose flag byte |
| +36 | 8 | `i64` | -1 | `barrier_state` | Barrier tracking sentinel |
| +44 | 4 | `u32` | 0 | `stall_count` | Stall cycle count |
| +48 | 4 | `u32` | 0xEE (low byte) | `control_bits` | Scheduling control word; bits 17:13 = barrier type |
| +56 | 4 | `u32` | 0 | `encoding_format` | Format discriminator (1 = basic, 4 = barrier, 15 = NOP stall) |

The `control_bits` field at sched+48 is the primary target of WAR pass modifications:

```
Bits 17:13  — barrier type (masked via 0xFFF83FFF then OR'd with type << 13)
Bit  4      — barrier-needed flag (in byte at sched+50)
Bits  7:5   — barrier sub-type (in byte at sched+50)
```

WAR insertion functions modify this field with specific patterns:
- `sub_6FA930` (InsertScoreboardBarrier): `sched[48] = (sched[48] & 0xFFF83FFF) | 0x50000`; clears bit 4 of sched[50]; sets `sched[56] = 4`
- `sub_6FA430` (InsertNOP): `sched[48] = (sched[48] & 0xFFF83FFF) | 0x44000`; clears bit 4 of sched[50]; sets `sched[56] = 1`
- `sub_6FAFD0` (InsertStall): `sched[48] = (sched[48] & 0xFFF83FFF) | 0x3C000`; sets bit 4 of sched[50]; sets `sched[56] = 15`

### Linked-List Structure

Mercury nodes form a doubly-linked list per basic block, managed through the `next` (+0) and `prev` (+8) pointers:

```
           head (ctx+40)                          tail (ctx+32)
              |                                      |
              v                                      v
         [node_0]  <-->  [node_1]  <-->  ...  <-->  [node_N]
         next=node_1     next=node_2                 next=0
         prev=0          prev=node_0                 prev=node_{N-1}
```

New nodes are inserted before the reference node by `sub_10AF8C0`. The WAR pass (`sub_6FBC20`) iterates forward through the list; `sub_6FB850` (PostWARAdjustment) iterates backward, skipping sentinel nodes (opcode == 0xFFFF).

### FNV-1a Deduplication

The `sequence_id` at +32 serves as the FNV-1a hash key for the instruction deduplication cache. The hash is computed over the 4-byte ID using the standard FNV-1a parameters (seed `0x811C9DC5`, multiplier `16777619`). The cache resides at context+488 (hash table pointer) with capacity at context+496 and entry count at context+480. Each hash table entry is 24 bytes with separate chaining via pointer at entry+0, key at entry+8, and value (Mercury encoding record pointer) at entry+16.

### Relationship to Ori IR Instruction Node

The Mercury node is distinct from the Ori IR instruction node:

| Property | Ori IR node | Mercury node |
|----------|-------------|--------------|
| Size | 296 bytes | 160 bytes |
| Allocator | `sub_7DD010` | `sub_10AF8C0` |
| Opcode location | +72 (32-bit word) | +28 (16-bit) |
| Operand model | Packed array at +84 | Encoded data at +48..+120 |
| Scheduling | Pointer at +40 | Pointer at +128 (60-byte record) |
| List linkage | +0 / +8 (prev/next) | +0 / +8 (next/prev) |
| Pipeline phases | 1--116 | 117--122 |

Phase 117 (MercEncodeAndDecode) reads Ori IR nodes via the master encoder `sub_6D9690` and produces Mercury nodes. All subsequent Mercury pipeline phases operate on Mercury nodes exclusively.

## Configuration

| Knob | Purpose | Context |
|---|---|---|
| 16 | WAR generation control | Checked in `sub_6FBC20` |
| 17 | Expansion/opex options; disables WAR penalty flags | `sub_703480` entry |
| 595 | Scheduling enable check | Scheduling pre-check |
| 743 | Scheduling reduce-reg mode | `sub_6FFDC0` opex body |
| 747 | Scheduling dynamic batch mode | `sub_6FFDC0` opex body |
| 4176 | SM 100+ extension bits for encoding | `sub_6D9690` encoder |

## Diagnostic Strings

| String | Source | Trigger |
|---|---|---|
| `"After Decode"` | `sub_6F2BF0` | Decode stage completion |
| `"After Expansion"` | `sub_6F2BF0` | Expansion stage completion |
| `"After WAR post-expansion"` | `sub_6F2BF0` | WAR pass 1 completion |
| `"After Opex"` | `sub_6F2BF0` | Opex stage completion |
| `"After WAR post-opexing"` | `sub_6F2BF0` | WAR pass 2 completion |
| `"After MercWARs"` | `sub_6FC240` | WAR pass trace |
| `"After MercOpex"` | `sub_7032A0` | Opex pass trace |
| `"After MercExpand"` | `sub_C3DFC0` | Expansion pass trace |
| `"After MercConverter"` | `0x9F3818` | MercConverter phase completion |
| `"CONVERTING"` | `sub_9EF5E0` | Active operand reorganization (per instruction) |
| `"After EncodeAndDecode"` | `0x23D1A60` | Roundtrip verification |
| `"EXPANDING"` | `0xC381B3` | Active instruction expansion |
| `"ENCODING"` | `0x21C2880` | Active instruction encoding |

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_9F1A90` | 35KB | MercConverter::ConvertInstruction (opcode dispatch, phase 5/141) | HIGH |
| `sub_9EF5E0` | 27KB | MercConverter::ReorganizeOperands (post-conversion lowering) | HIGH |
| `sub_9ED2D0` | 25KB | MercConverter::Dispatch (master opcode switch, `& 0xCF` mask) | HIGH |
| `sub_9F3340` | 7KB | MercConverter::Run (orchestrator, calls 9F1A90 then 9EF5E0) | HIGH |
| `sub_9EC160` | ~2KB | MergeSort (linked-list merge sort for operand chains) | HIGH |
| `sub_7BFC30` | ~4KB | MercConverter::ValidateEncoding (returns -1 on failure) | HIGH |
| `sub_9CE210` | ~6KB | MercConverter::FallbackConvert (recursive re-encoding) | MEDIUM |
| `sub_6D9690` | 94KB | MercuryEncode::EncodeInstruction (master switch) | HIGH |
| `sub_6FFDC0` | 66KB | MercuryPipeline::EmitInstructions (opex body) | HIGH |
| `sub_6E8EB0` | 64KB | BasicBlock::Initialize (encoder state init) | MEDIUM |
| `sub_6F2BF0` | 59KB | DecodePipeline::DecodeAndExpand | MEDIUM |
| `sub_719D00` | 50KB | ExpansionEngine::buildOutput | MEDIUM |
| `sub_726E00` | 30.6KB | Instruction encoding + FNV-1a dedup cache | HIGH |
| `sub_C3CC60` | 26KB | MercExpand::run (pseudo-instruction expansion) | HIGH |
| `sub_6FC810` | 24KB | MercuryPipeline::Configure | MEDIUM |
| `sub_6E4110` | 24KB | MercGenerateSassUCode (final SASS emission) | HIGH |
| `sub_6F52F0` | 23KB | DecodePipeline::RunStages (orchestrator) | MEDIUM |
| `sub_C3BCD0` | 19KB | MercExpand::expandControlFlow | HIGH |
| `sub_6FF070` | 18KB | Predicate handling in expansion | MEDIUM |
| `sub_C3E030` | 18KB | MercExpand::finalizeExpansion | HIGH |
| `sub_C37A10` | 16KB | MercExpand::expandInstruction | HIGH |
| `sub_C38180` | 13KB | MercExpand::expandInstruction (complex cases) | HIGH |
| `sub_7266A0` | 11.7KB | FNV-1a hash table (instruction cache) | HIGH |
| `sub_733870` | 10KB | Source operand encoder | MEDIUM |
| `sub_C39B40` | 10KB | MercExpand::expandMemoryOp | HIGH |
| `sub_6FAA90` | 7.9KB | WAR stall insertion | HIGH |
| `sub_735290` | 7.6KB | Per-instruction SASS encoding pipeline | MEDIUM |
| `sub_6FBC20` | 7.4KB | WAR generation main pass | HIGH |
| `sub_C3B560` | 8KB | MercExpand::expandTexture | HIGH |
| `sub_734D20` | 8.1KB | Complex operand encoder (texture/surface/barrier) | MEDIUM |
| `sub_C3A460` | 6KB | MercExpand::expandAtomicOp | HIGH |
| `sub_734370` | 6.1KB | Immediate operand encoder | MEDIUM |
| `sub_733FA0` | 5.1KB | Instruction operand encoder | MEDIUM |
| `sub_734820` | 4.1KB | Predicate operand encoder | MEDIUM |
| `sub_734AD0` | 3.3KB | Memory operand encoder | MEDIUM |
| `sub_6FA5B0` | 2.5KB | WAR hazard detector | HIGH |
| `sub_7032A0` | 2.3KB | RunMercOpexPass (entry) | HIGH |
| `sub_6FC280` | 1.8KB | Create pipeline context | MEDIUM |
| `sub_6FA7B0` | 1.7KB | InsertWAITDP (opcode 246) | HIGH |
| `sub_703480` | 1.4KB | RunOpexPass (entry) | HIGH |
| `sub_6FA930` | 1.4KB | InsertScoreboardBarrier (opcode 54) | HIGH |
| `sub_10AF8C0` | ~0.5KB | MercNode::Allocate (160-byte node allocator, core initializer) | HIGH |
| `sub_10B1F90` | ~0.2KB | MercNode::Create (wrapper: allocate + dedup cache + sched state) | HIGH |
| `sub_10B1EE0` | ~0.2KB | MercNode::Clone (wrapper: allocate from clone source) | HIGH |
| `sub_10B14B0` | ~0.2KB | MercNode::CreateBBBoundary (creates sentinel pair, opcode 0xFFFF) | HIGH |
| `sub_6FAFD0` | ~1KB | InsertScoreboardStalls (allocate NOP stall nodes) | HIGH |
| `sub_6FA430` | ~0.5KB | InsertNOP (allocate NOP barrier nodes) | HIGH |
| `sub_7B9B80` | 216B | Bitfield insert primitive (18,347 callers) | CERTAIN |
| `sub_7B9D30` | 38B | Clear constant buffer slot table | HIGH |
| `sub_7B9D60` | 408B | Encode reuse flags + predicate | HIGH |

## Mercury Knob Cluster -- DAG Table Reference

The Mercury encoder is gated by a 21-knob cluster registered as a contiguous block inside `ctor_005` (0x420A50--0x4210E0) into the DAG knob table indexed by `sub_6F0820` (`GetKnobIndex`, 99 total entries). Names are stored ROT13-obfuscated in `.rodata` at `0x21B6900`--`0x21B6CA0`; the runtime decodes them on demand for `-knob NAME=VALUE` CLI parsing and `DUMP_KNOBS_TO_FILE` diagnostic output. Indices are determined by registration order within the DAG table -- the 21 Mercury knobs do not occupy a fixed numeric range across ptxas builds, but they are always registered as one alphabetical run, so their relative ordering is stable.

| | |
|---|---|
| **Table** | DAG knob array (99 entries x 64-byte descriptor) |
| **Registrar** | `ctor_005` (0x40D860, 80KB) -- Mercury cluster at 0x420A50--0x4210E0 |
| **GetKnobIndex** | `sub_6F0820` (name -> index lookup, init-time only) |
| **GetKnobIntValue** | `sub_7A1B80` (returns `*(int32*)(state + 72*idx + 8)`) |
| **GetKnobBoolValue** | `sub_7A1CC0` (returns `IsKnobSet && type == 4`) |
| **ParseKnobValue** | `sub_6F7360` (parses `name=value`, `name=value[when=...]`) |
| **Storage region** | `0x21B6900`--`0x21B6CA0` (672 bytes of ROT13'd name strings) |
| **Consumer roots** | `sub_6F52F0` (RunStages), `sub_6FBC20` (WAR), `sub_6FFDC0` (Opex) |
| **CLI override** | `-knob MercuryX=N` (parsed by entry point at `sub_703AB0`) |
| **Env override** | `KNOBS` environment variable, processed by `sub_79C9D0` |

### Knob Inventory (21 entries, alphabetical)

Confidence column reflects only the **name** (string-anchored, decoded from ROT13) and **registration site** (xref to `ctor_005`). Role and consumer assignments are inferred from the name plus context in the orchestrator / WAR / opex bodies; treat them as MED unless the body has been independently traced.

| # | Name | Inferred Role | Consumer site (most likely) | Confidence |
|---|------|---------------|------------------------------|------------|
| 1 | `MercuryAssumePTXPortability` | Tag emitted instructions as PTX-portable; affects which target-specific encodings are skipped. | Decode (`sub_6F2BF0`) / Encode (`sub_6D9690`) | name HIGH / role MED |
| 2 | `MercuryCompactedAssumes` | Coalesce adjacent assume-stream entries into a single compacted record. | Assume-stream builder inside opex (`sub_6FFDC0`) | name HIGH / role MED |
| 3 | `MercuryConsumeAssumes` | Drain the assume stream during opex, attaching residual assumes to the emitted SASS. | Opex (`sub_6FFDC0` -> `sub_703480`/`sub_7032A0`) | name HIGH / role MED |
| 4 | `MercuryConverterStats` | Emit per-pass instruction-count statistics from the MercConverter (phase 5 + phase 141). | MercConverter (`sub_9F3340`, `sub_9F1A90`) | name HIGH / role HIGH |
| 5 | `MercuryDepStagePreferNonLiveinPSB` | Prefer PSBs (Predicated SchedBarriers) on non-live-in defs when constructing the dep-stage graph. | Opex dep-stage builder (`sub_6FFDC0`) | name HIGH / role MED |
| 6 | `MercuryDisableLegalizationOfTexToURBound` | Disable the late legalization pass that rebinds tex operands to uniform-register bounds. | PostFixForMercTargets phase 113 (`sub_6F52F0` step 0) | name HIGH / role MED |
| 7 | `MercuryDumpInstsAsBinary` | Dump per-instruction Mercury binary blob (raw encoded bytes) to a side channel. | Encode (`sub_6F2BF0`) after `sub_6D9690` returns | name HIGH / role HIGH |
| 8 | `MercuryEncodeDecode` | Master switch / verbosity for the encode-then-decode roundtrip (phase 117 `MercEncodeAndDecode`). | `sub_6F2BF0` (59KB encode/decode driver) | name HIGH / role HIGH |
| 9 | `MercuryEncodeNewWorkerFiles` | Route the encoder output into the new per-worker file format (post-CUDA-13 split-file scheme). | `sub_6F2BF0` output sink | name HIGH / role MED |
| 10 | `MercuryForceISAClass` | Override the auto-selected SASS ISA class (Turing/Ampere/Hopper/Blackwell) used by the master encoder switch. | `sub_6D9690` (94KB encoder) | name HIGH / role HIGH |
| 11 | `MercuryForceUnknownTcgen05Attr` | Force a "tcgen05 attribute unknown" marker on tcgen05 instructions; used to exercise the fallback emit path on sm100+. | `sub_6D9690` tcgen05 branch | name HIGH / role MED |
| 12 | `MercuryGenSassUCode` | Master switch for the final SASS microcode emit stage (phase 122 `MercGenerateSassUCode`). | `sub_6E4110` (24KB SASS emitter) | name HIGH / role HIGH |
| 13 | `MercuryInsertAssumes` | Allow the opex pipeline to insert new assume-stream entries (cross-block latency hints). | Opex (`sub_6FFDC0`) | name HIGH / role HIGH |
| 14 | `MercuryInsertBackedgeDepbar` | Inject DEPBAR (dependency-bar) instructions on loop backedges to flush pre-edge writes. | WAR pass (`sub_6FBC20` / `sub_6FA930`) | name HIGH / role HIGH |
| 15 | `MercuryInsertXblockWait` | Insert cross-block wait barriers when the live-in scoreboard set crosses a basic-block boundary. | Opex (`sub_6FFDC0`) + WAR (`sub_6FBC20`) | name HIGH / role HIGH |
| 16 | `MercuryIssueDelayWBStallSelfLoop` | Allow issue-delay/writeback stalls on instructions that self-loop (their own predecessor in the dep graph). | Opex stall inserter (`sub_6FAA90`) | name HIGH / role MED |
| 17 | `MercuryMergePrologueBlocks` | Merge function-prologue blocks before WAR pass 1 to reduce barrier insertion at function entry. | Phase 113 / 117 boundary | name HIGH / role MED |
| 18 | `MercuryPresumeXblockWaitBeneficial` | Assume cross-block waits are beneficial without running the cost analyzer; biases the heuristic toward inserting more waits. | Opex (`sub_6FFDC0`) cost gate | name HIGH / role HIGH |
| 19 | `MercuryTepidAwareSb` | Make scoreboard placement aware of "tepid" (cold-but-not-frozen) blocks identified by hot/cold partitioning. | Opex (`sub_6FFDC0`) tepid heuristic | name HIGH / role MED |
| 20 | `MercuryTrackMultiReadsWarLatency` | Track latency contributions from multi-read operands when scoring WAR hazards. | WAR (`sub_6FBC20` / `sub_6FA5B0`) | name HIGH / role HIGH |
| 21 | `MercuryUseActiveThreadCollectiveInsts` | Lower collective ops (e.g. bar.warp, redux) into active-thread variants where legal. | `sub_6D9690` collective branch | name HIGH / role MED |

### Defaults and Visibility

The DAG knob descriptor (64 bytes) stores a default value, a type tag (INT/BOOL/STR/etc.), and a presence byte. None of the 21 Mercury knobs has its default encoded as a separate `.rodata` string, which means defaults are immediate constants baked into `ctor_005`. Without symbol-grade recovery of the descriptor inits, defaults are LOW confidence; circumstantial evidence (the knobs ride alongside conservative-by-default pass enablement) suggests:

- `MercuryEncodeDecode`, `MercuryGenSassUCode` -- default ON (these are core stages; turning them off would break the pipeline). Confidence: HIGH.
- `MercuryInsertBackedgeDepbar`, `MercuryInsertXblockWait`, `MercuryInsertAssumes` -- default ON for SM 100+, default OFF or downgraded for SM 75--99 (the cross-block / backedge dep-bar machinery is a Blackwell-era addition). Confidence: MED.
- `MercuryConverterStats`, `MercuryDumpInstsAsBinary`, `MercuryEncodeNewWorkerFiles` -- default OFF (diagnostic / dev knobs). Confidence: MED.
- `MercuryForceISAClass`, `MercuryForceUnknownTcgen05Attr` -- default OFF (Force* knobs override the auto-selected class only when set). Confidence: HIGH (Force* convention).
- `MercuryPresumeXblockWaitBeneficial`, `MercuryTrackMultiReadsWarLatency`, `MercuryTepidAwareSb` -- default OFF (heuristic-bias knobs; the production pipeline ships with the safer cost-analyzed path). Confidence: LOW.
- `MercuryDisableLegalizationOfTexToURBound`, `MercuryIssueDelayWBStallSelfLoop` -- Disable* / permissive switches default OFF. Confidence: MED.
- `MercuryAssumePTXPortability`, `MercuryUseActiveThreadCollectiveInsts`, `MercuryMergePrologueBlocks`, `MercuryCompactedAssumes`, `MercuryConsumeAssumes`, `MercuryDepStagePreferNonLiveinPSB` -- assignment unclear without descriptor trace. Confidence: LOW.

Visibility-wise, knob names never reach `strings(1)` output of the unmodified binary because the descriptor table holds them ROT13-obfuscated; only the dumper at `sub_79CB10` (DAG variant `sub_6F8000`-ish) decodes them at dump time. The plaintext form `Mercury*` therefore leaks only when `DUMP_KNOBS_TO_FILE` is exercised, not on a normal compile.

### Pipeline-Stage Interaction Map

```
Phase 113 PostFixForMercTargets ──── MercuryDisableLegalizationOfTexToURBound
Phase 114 FixUpTexDepBarAndSync
Phase 117 MercEncodeAndDecode ─────── MercuryEncodeDecode, MercuryDumpInstsAsBinary,
                                      MercuryEncodeNewWorkerFiles, MercuryForceISAClass,
                                      MercuryForceUnknownTcgen05Attr,
                                      MercuryAssumePTXPortability,
                                      MercuryUseActiveThreadCollectiveInsts
Phase 118 MercExpandInstructions ──── (DAG knobs #743, #747 -- see knobs.md)
Phase 119 MercGenerateWARs1 ────────── MercuryInsertBackedgeDepbar,
                                      MercuryTrackMultiReadsWarLatency,
                                      MercuryMergePrologueBlocks
Phase 120 MercGenerateOpex ──────────── MercuryInsertAssumes, MercuryConsumeAssumes,
                                      MercuryCompactedAssumes,
                                      MercuryInsertXblockWait,
                                      MercuryPresumeXblockWaitBeneficial,
                                      MercuryDepStagePreferNonLiveinPSB,
                                      MercuryTepidAwareSb,
                                      MercuryIssueDelayWBStallSelfLoop
Phase 121 MercGenerateWARs2 ────────── (same WAR knobs as 119, re-run post-opex)
Phase 122 MercGenerateSassUCode ────── MercuryGenSassUCode
```

The MercConverter sweeps (phase 5 and phase 141) consume `MercuryConverterStats` -- not registered in the Mercury cluster's runtime dispatch path but emitted via the same dumper code path.

### Quirks

> ⚡ **QUIRK -- ROT13 obfuscation defeats `strings(1)`**
> All 21 Mercury knob names are stored ROT13-encoded in `.rodata` (e.g. `ZrephelVafregOnpxrqtrQrcone` decodes to `MercuryInsertBackedgeDepbar`). A naive `strings ptxas | grep Mercury` recovers only six identifiers -- the six pipeline-phase names (`MercEncodeAndDecode`, `MercExpandInstructions`, `MercGenerate*`, `PostFixForMercTargets`) plus the four CLI tokens (`mercury`, `capmerc`, `cap-merc`, `mercury,capmerc,sass`). The 21-knob cluster is invisible to that pattern, which is why surface-level audits routinely under-report the Mercury control surface by ~3x.

> ⚡ **QUIRK -- defaults are not in `.rodata`**
> Unlike most ptxas knobs whose defaults appear as ASCII strings (`"true"`, `"false"`, `"3"`, etc.) and can be recovered by name -> nearby-string proximity, the Mercury cluster's defaults are immediate constants embedded directly in `ctor_005` registration calls. Recovering them requires decompiling `ctor_005` register-passing or stepping the descriptor table at init time -- not pattern-matching on `.rodata`. Confidence on documented defaults is therefore necessarily LOW unless the value is determined by the knob's name (e.g. `Disable*` defaults to OFF by convention).

> ⚡ **QUIRK -- the cluster lives in DAG, not OCG**
> ptxas hosts two independent knob tables: OCG (1,195 entries, indexed by `sub_79B240`) and DAG (99 entries, indexed by `sub_6F0820`). The Mercury knobs are entirely in DAG. This matters because the two tables have separate `-knob` parsers and separate `KNOBS` env var processors -- a `-knob MercuryInsertXblockWait=1` is parsed by the DAG path (`sub_6F7360`), never reaches OCG, and a typo silently misses the OCG table without any "unknown knob" warning. Cross-table contamination is also impossible: the DAG and OCG tables share no indices.

> ⚡ **QUIRK -- sm_75--sm_99 ignores most of the cluster**
> The cross-block / backedge / Xblock-wait family (`MercuryInsertBackedgeDepbar`, `MercuryInsertXblockWait`, `MercuryPresumeXblockWaitBeneficial`, `MercuryDepStagePreferNonLiveinPSB`) only takes effect when `*(BYTE*)(profile+1398) & 0x20` is set, i.e. on SM targets whose capability word (`profile+1413`) advertises Mercury or Capsule-Mercury capability. Setting these knobs on Turing/Ampere/Hopper compiles silently no-ops; the WAR / Opex bodies guard on the same capability bit before consulting the knob value. The cluster is effectively a Blackwell-onwards control surface despite being available on all targets at the CLI level.

### Inferred Value Types

The DAG knob descriptor (64 bytes) stores a type tag at descriptor offset +24. The five generic ptxas knob types appear in the table below; assignment per Mercury knob is inferred from naming conventions consistently observed across the rest of the binary (1,294-knob universe documented in `config/knobs.md`).

| Type tag | Type | Naming convention | Storage |
|---|---|---|---|
| 0 | STRING | (none in Mercury cluster) | char* in arena |
| 1 | INT | numeric verbs / nouns ending in `Limit`, `Threshold`, `Factor`, `Count`, `ISAClass`, `Stats` | int32 at state+72*idx+8 |
| 2 | FLOAT | (none) | float at state+72*idx+8 |
| 3 | DOUBLE | (none) | double at state+72*idx+8 |
| 4 | BOOL | `Disable*`, `Insert*`, `Force*`, `Presume*`, `Track*`, `Use*`, `Merge*`, `Consume*`, `Compacted*`, `Assume*`, `Issue*`, `TepidAware*`, `EncodeDecode`, `GenSassUCode`, `DumpInstsAsBinary`, `EncodeNewWorkerFiles`, `DepStagePrefer*` | presence byte + int32 marker |

Applying that taxonomy:

| Knob | Inferred type | Rationale |
|---|---|---|
| `MercuryAssumePTXPortability` | BOOL | `Assume*` toggle |
| `MercuryCompactedAssumes` | BOOL | adjective gate |
| `MercuryConsumeAssumes` | BOOL | `Consume*` toggle |
| `MercuryConverterStats` | BOOL | `Stats` flag (emit/don't) |
| `MercuryDepStagePreferNonLiveinPSB` | BOOL | `Prefer*` toggle |
| `MercuryDisableLegalizationOfTexToURBound` | BOOL | `Disable*` switch |
| `MercuryDumpInstsAsBinary` | BOOL | `Dump*` toggle |
| `MercuryEncodeDecode` | BOOL | master phase switch |
| `MercuryEncodeNewWorkerFiles` | BOOL | format selector |
| `MercuryForceISAClass` | INT | enum-valued (Turing=0/Ampere=1/Hopper=2/Blackwell=3) -- `Class` noun, not a verb |
| `MercuryForceUnknownTcgen05Attr` | BOOL | `Force* + Unknown*` toggle |
| `MercuryGenSassUCode` | BOOL | master phase switch |
| `MercuryInsertAssumes` | BOOL | `Insert*` toggle |
| `MercuryInsertBackedgeDepbar` | BOOL | `Insert*` toggle |
| `MercuryInsertXblockWait` | BOOL | `Insert*` toggle |
| `MercuryIssueDelayWBStallSelfLoop` | BOOL | conditional-stall toggle |
| `MercuryMergePrologueBlocks` | BOOL | `Merge*` toggle |
| `MercuryPresumeXblockWaitBeneficial` | BOOL | `Presume*` heuristic-bias toggle |
| `MercuryTepidAwareSb` | BOOL | `*Aware*` toggle |
| `MercuryTrackMultiReadsWarLatency` | BOOL | `Track*` toggle |
| `MercuryUseActiveThreadCollectiveInsts` | BOOL | `Use*` toggle |

Net: 20 boolean knobs + 1 enum-valued integer (`MercuryForceISAClass`). Confidence: type tag assignments are MED (naming-convention inference); the binary descriptor table at descriptor offset +24 would resolve them definitively.

### Registration-Site Anatomy

Each Mercury knob is registered by a single call from `ctor_005` to the DAG knob registration helper. The repeating 0x50-byte stride between calls (0x420A50 -> 0x420AA0 -> 0x420AF0 -> ...) implies a uniform calling shape: load name pointer, load default value, load type tag, load presence-test predicate, call helper. The helper writes a 64-byte descriptor into the DAG knob table at the next free slot, then increments the index counter.

Because the knobs are registered in alphabetical order and the registration loop is monolithic, the cluster occupies a contiguous index range. If `K0` is the index of `MercuryAssumePTXPortability` (the first to register), then `MercuryUseActiveThreadCollectiveInsts` lives at `K0 + 20`. The base `K0` is determined at link time by the registration order across all DAG knob ctors -- it is not stable across ptxas builds, but the relative offsets within the cluster are.

Cross-referenced DAG knob indices already documented elsewhere in the wiki (`config/knobs.md` table at line 590): `#8`, `#16`, `#17`, `#743`, `#747`. None of these five fall within the contiguous Mercury* range, which implies the Mercury cluster sits in a different region of the 99-entry table -- consistent with the cluster being a recent (post-r13.0) addition appended at the tail.

> ⚡ **QUIRK -- the cluster only has 21 entries despite 99 DAG slots**
> The DAG knob table reserves 99 slots, but only ~25 are documented across the wiki (knob #8/#16/#17/#743/#747 plus the 21 Mercury* knobs). The remaining ~73 slots are either unused (placeholders for forward compatibility), legacy DAG-scheduler controls deprecated when Mercury subsumed the old scheduler, or anonymous-test knobs invisible to documentation tooling because their names live in dead code paths. A handful are likely registered by other ctors and we have not yet traced them.

## Cross-References

- [ISel & Opcode Selection](./isel.md) -- MercConverter opcode dispatch table (`sub_9ED2D0`), handler details
- [Instructions & Opcodes](../ir/instructions.md) -- Ori IR instruction node layout (296 bytes, input to Mercury encoder)
- [Code Generation Overview](./overview.md) -- high-level codegen pipeline context
- [SASS Instruction Encoding](./encoding.md) -- detailed bit-level encoding format
- [Capsule Mercury & Finalization](./capmerc.md) -- capmerc variant and `--self-check`
- [Scoreboards & Dependency Barriers](../scheduling/scoreboards.md) -- DEPBAR/WAITDP semantics
- [Phase Manager](../passes/phase-manager.md) -- 159-phase pipeline infrastructure
- [Optimization Levels](../config/opt-levels.md) -- `-O0` vs higher-level scoreboard behavior
- [Knobs System](../config/knobs.md) -- knob #16, #17, #743, #747 details
