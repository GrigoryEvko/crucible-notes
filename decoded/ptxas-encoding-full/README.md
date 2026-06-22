# ptxas SASS-encoding dispatch tables

Facts recovered by static + behavioural analysis of the freely-distributed
`ptxas` (CUDA **13.0.88**, `V13.0.88`,
sha256 `daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`)
and its companion `nvdisasm` (V13.1.115). Interoperability / research RE;
DMCA 17 U.S.C. § 1201(f). Our own tools + uncopyrightable factual data only.

## Layout overview

`dispatch_layer_summary.tsv` and `dispatch_reconciliation_matrix.tsv` describe
the layered encoder: a flat `opcode_to_encoding` table, two slotted encoding
trees, two SASS dispatch sub-table sets, and the **per-arch** `per_sm_dispatch`
layer that overrides them per target. The remaining `encoding_tree_*`,
`format_descriptors`, `modifier_*`, `slot_geometry_model`, `tier2_modifiers`
files describe the format/modifier grammar (target-invariant).

## Per-arch encoder blocks (the target axis)

The per-arch SASS-encoding dispatch region in `.rodata` spans VMA
`0x22E7AD0 .. 0x23EFB60`. It is a packed sequence of **seven arch encoding
blocks**. Each block begins at a 0x200-capacity "lead" main table whose first
16 bytes are `[0x200, lead_handler_va]`; the body is a packed run of 24-byte
slots `[key, handler_va, pad]` with `key = (format_id<<8)|minor_opcode`.

The prior extraction (`per_sm_handler_dispatch.tsv`) named five blocks
(`sm50_7x`, `sm75`, `sm100`, `sm80_8x`, `sm86_89`) — all sharing lead handler
`0xEA7440` and drawing their per-opcode emitters from one shared low `.text`
region (`0xC693D0+`). It stopped there. **This run adds the two remaining
blocks**, which the older toolchains did not have:

| blk | lead_va    | lead handler | emitter region        | slots | distinct h | %unique vs classic | arch |
|-----|------------|--------------|-----------------------|-------|-----------|--------------------|------|
| 1   | 0x22E7AD0  | 0xEA7440     | 0xC693D0+ (shared)    | 1616  | 1616      | baseline           | sm_50..sm_72 |
| 2   | 0x2348FB0  | 0xEA7440     | 0xC693D0+ (shared)    | 1759  | 1759      | 18.2%              | sm_75 |
| 3   | 0x236CD10  | 0xEA7440     | 0xC693D0+ (shared)    | 2142  | 2142      | 34.5%              | sm_90/sm_100/sm_103 |
| 4   | 0x238C9B0  | 0xEA7440     | 0xC693D0+ (shared)    | 1744  | 1744      | 17.5%              | sm_80 |
| 5   | 0x23A8090  | 0xEA7440     | 0xC693D0+ (shared)    | 1754  | 1754      | 18.0%              | sm_86/87/88/89 |
| **6** | **0x23BA8A0** | **0x18F0540** | **0x18DF870..0x18F0CF0 (≈71 KB, disjoint)** | **657** | **657** | **100%** | **sm_110 Jetson Thor** |
| **7** | **0x23DB120** | **0x1AEE1D0** | **0x1A01050..0x1AF5230 (≈1 MB, disjoint)** | **682** | **682** | **100%** | **sm_120/121 consumer Blackwell** |

The decisive structural fact: blocks 6 and 7 share **zero** per-opcode emitter
handlers with the classic family (blocks 1–5, union 2408 handlers) — they are
backed by entirely separate `.text` emitter code in disjoint address ranges.
Verified two ways: (a) handler-set intersection is empty (b6∩classic = 0,
b7∩classic = 0, b6∩b7 = 0); (b) the per-opcode emitter VAs of each new block
fall in a single isolated 1 MB `.text` window — block 6's 657 emitters all lie
in `0x18Dxxxx..0x18Fxxxx`, block 7's 682 all in `0x1A0xxxx..0x1AFxxxx`, with no
classic handler numerically inside either window. They are genuinely new
encoding families, not parameter variants.

Caveat on the *lead* handler: for the classic blocks 1–5 the lead handler
`0xEA7440` is a full default-emitter routine, but for blocks 6 (`0x18F0540`)
and 7 (`0x1AEE1D0`) the lead qword points at a trivial `mov $1; ret` predicate
stub (one of a run of identical 16-byte-aligned stubs), not a full dispatcher.
The per-arch *encoding identity* of blocks 6/7 therefore rests on their
per-opcode slot emitters (the disjoint 1 MB windows above), not on the lead
handler. The lead pointer is also not reachable from any external arch-indexed
pointer table in the binary — each lead VA appears exactly once, inside its own
block header — so the block↔arch assignment is an inference (next section), not
a directly-tabulated dispatch.

### block ↔ arch mapping basis
Blocks 1–5 keep the prior run's labels. sm_103 (datacenter Blackwell Ultra)
has **no dedicated block** — it shares block 3 with sm_90/sm_100 and differs
only via downstream isel/scheduling parameters (its SASS differs from sm_100,
but it selects from the same encoder family). Blocks 6 and 7 are the two
genuinely-new families. The 6↔sm_110 / 7↔sm_120 assignment follows the emitter
footprint: block 7's emitter is ≈14× larger (≈1 MB vs ≈71 KB), matching the
richer consumer-Blackwell ISA (RT/TTU cores + consumer tensor classes), while
block 6's lean emitter matches Jetson Thor's smaller ISA. Cross-checked against
the nvdisasm SASS-ISA class counts below (consumer 1012 > Jetson 901).

## Files added this run
- `extract_arch_blocks.py` — our stdlib-only extractor for all seven blocks.
  `python3 extract_arch_blocks.py <ptxas>` → `arch_blocks.tsv` + summary.
- `arch_blocks.tsv` — every dispatch slot of all 7 blocks (block, lead_va,
  slot_va, key, format_id, minor, handler_va, pad).
- `new_arch_blocks_sm110_sm120.tsv` — just the two NEW blocks (6 = sm_110,
  7 = sm_120/121), the data the prior run lacked.
- `per_arch_encoder_blocks.tsv` — the per-block summary table above.
- `per_arch_encoding_opbyte.tsv` — behavioural cross-check: per-target SASS
  primary-opcode-byte histogram for a fixed probe (regenerated by
  `../ptxas-isel/extract_per_arch_isel.py`).
- `sass_class_presence_by_arch.tsv` — per-arch SASS-ISA class presence matrix
  (1 = class exists for that arch), distilled from `nvdisasm`'s decoded ISA
  tables (the verbatim tables stay local-only per `../MANIFEST.md`; only the
  class-name presence facts are published here).

## SASS-ISA class coverage facts (cross-check vs nvdisasm)

`nvdisasm` distinct SASS-class counts per arch: SM90/90a 1168, SM100 975,
SM103 971, **SM110 901**, **SM120 = SM121 1012** (byte-identical). Category
deltas that explain the encoder-block split:

| category (SASS classes)                  | sm_100 | sm_103 | sm_110 | sm_120 |
|------------------------------------------|:------:|:------:|:------:|:------:|
| tcgen05 / TMEM (`utc*`, `ldtm_`, `sttm_`)| 38     | 31     | 35     | **0**  |
| RT / TTU ray-tracing (`ttu*`)            | 0      | 0      | 0      | **7**  |
| consumer tensor (`qmma`/`omma`/`mxqmma`) | 0      | 0      | 0      | **7**  |
| datacenter FP8 quad (`qadd4`/`qfma4`/`qmul4`) | 11 | 11     | **0**  | 0      |
| gather / scatter / metadata              | 4      | 4      | **0**  | 0      |
| native 64-bit mov-imm (`mov_imm64` etc.) | 0      | 0      | 0      | **4**  |

The tcgen05/TMEM row counts the full `utc*`/`ldtm_*`/`sttm_*` family per arch
(38/31/35/0). A prior draft listed `8` here — that was the count of only the
TMEM/async-warp subset that the published presence matrix tabulates
(`ldtm_`, `sttm_`, `utcatomsws_{cas,fas,op}_`, `utcbar_flush_`, `utcldsws_`,
`utcstsws_`), not the whole tensor-core family; corrected against the nvdisasm
class sets. sm_110 keeps almost the whole family (35); sm_103 drops 8
integer-MMA / scaled-mxqmma `utc*` variants relative to sm_100 (see below),
so 31. Consumer Blackwell (sm_120) has none.

Consumer Blackwell (sm_120/121) has **no tcgen05/TMEM** but adds RT/TTU and
the `qmma`/`omma` consumer-tensor and native 64-bit MOV-immediate classes —
exactly the differences seen in the per-arch SASS (no `IMAD.MOV`, real
`MOV.64`). Jetson Thor (sm_110) keeps tcgen05/TMEM (35 classes) but is the
leanest ISA (drops the datacenter FP8-quad and gather/scatter ops). sm_103
shares encoder block 3 with sm_100 but its **class set is not identical**: a
direct set diff of the nvdisasm class names shows sm_103 *drops* 8 sm_100
tcgen05 variants (`utcimma_{1,2}cta__A_{gdesc,tmem}`,
`utcmxqmma_{1,2}cta_scale__A_{gdesc,tmem}` — integer-MMA and scaled-mxqmma)
and *adds* 4 (`ldtm_stat_`, `mufu_fp16_simd__R{I,R,U}` — a TMEM-load-status
form and FP16-SIMD MUFU), netting 971 vs 975. So sm_100 and sm_103 select
from the same encoder family (block 3) but expose slightly different tensor /
MUFU class menus; they are not byte-identical the way sm_120 ≡ sm_121 is.

## Verification provenance

Every table here was re-validated against ground truth (not just re-asserted):
the block extractor was re-run against the binary (`sha256 daba837a…`,
`V13.0.88`) and reproduces `arch_blocks.tsv` byte-for-byte; the lead headers,
slot counts (1616/1759/2142/1744/1754/657/682), and the empty handler-set
intersections were confirmed by reading `.rodata` directly (objcopy) and
disassembling the lead/slot handlers (objdump). The SASS-ISA class counts and
the entire `sass_class_presence_by_arch.tsv` (209 classes × 4 arches = 836
cells) were checked against fresh `nvdisasm -c` class-name sets with **zero**
mismatches. The category-delta table was the only place a count was wrong (the
tcgen05/TMEM row, fixed above).
