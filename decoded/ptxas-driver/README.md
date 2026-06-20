# ptxas compilation driver, pipeline orchestration & O0-O5 model (CUDA 13.0.88)

Binary-derived reverse-engineering of NVIDIA `ptxas` (CUDA 13.0.88). Every
claim is pinned to a binary address and byte-checked. `.text`/`.rodata` use
`VMA == file_offset + 0x400000`. **The binary is ground truth**; any external
reference that disagrees is flagged as drift.

## What this directory documents

The end-to-end backend driver: how `ptxas` goes from a parsed PTX module to a
per-function optimization pipeline, how the **PhaseManager** registers and
dispatches phases, the **O0-O5 opt-level model**, and the **recipe /
NvOptRecipe / named-phases** override mechanism.

## Files

| File | Contents |
|---|---|
| `driver_callgraph.md` | Full call chain `start -> main -> sub_446240 -> ... -> sub_7FB6C0`; OCG-knob mechanism; PhaseManager build; recipe path. |
| `opt_level_model.md` | The `-O`/`--opt-level` model: external 0..3 -> internal {1,2,4}; per-function nvopt 0..5; accessor `sub_7DDB50`; knob-499 override; gating sites. |
| `phase_dispatch_157_vs_159.md` | Definitive 157-vs-159 resolution (159 registered, 157 dispatched by default). |
| `phase_index.tsv` | All 159 phase IDs/names/VAs/is_default. Verified vs `phase_names.json` + `ptxas-passes/phase_pipeline.tsv` (zero mismatches). |
| `ocg_knobs.tsv` | The OCG-knob model: 72-byte entries at `config+0x48`, offset = `id*72`; the 6 driver-relevant knobs (249/297/298/391/499). |
| `recipe_override_dsl.tsv` | The named-phases / recipe override DSL parsed by `sub_9F4040` (17 tokens). |
| `phase_order_table.txt` | The default-order table at `0x22BEEA0` (identity [0..156] + 2 sentinels). |
| `wiki_outline_and_corrections.md` | Corrections + outline for the wiki pipeline/passes pages. |

## The one-paragraph model

`ptxas` parses argv in `sub_446240` into a parsed-options struct, builds one
**OCG context** per function via `sub_7F7DC0` (which resolves `-O` 0..3 into
the internal tier {1,2,4} at `ctx+0x838`), then runs the **optimization
driver** `sub_7FB6C0`. The driver reads **OCG knob 298**: if clear (the
default), it builds a **PhaseManager** (`sub_C62720`, registers **159** phase
objects), fetches the **default schedule** (`sub_C60D20` -> `(&0x22BEEA0,
157)`), and dispatches exactly **157** phases (IDs 0..156) through
`sub_C64F70`. If knob 298 is set, it instead takes `sub_9F63D0`, which uses
`sub_9F4040` to build a custom schedule from a small recipe DSL and feeds it to
the same dispatch loop. Each phase self-gates on the opt level via
`sub_7DDB50` (164 callers). The two trailing registered phases —
`DebuggerBreak` (157) and `NOP` (158) — are never dispatched on a default
compile.

## Confidence

**Very high.** The driver chain, the OCG-knob arithmetic (`id*72`), the
opt-level remap, the 157/159 split, the factory jump table, and the recipe DSL
are each disassembled and matched to the Hex-Rays C view. The phase table is
byte-read from `0x22BD0C0` and cross-checked. Analytical labels (a phase's
"meaning") are medium confidence; threshold tests and addresses are high.
