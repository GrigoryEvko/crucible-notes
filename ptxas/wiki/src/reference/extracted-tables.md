# Extracted `.rodata` Tables

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base is `0x400000` (non-PIE); file offset = VA − `0x400000`.*

Every static constant table in ptxas's `.rodata` has been extracted to
machine-readable JSON — **45 files covering 100% of the 7.16 MB section** (≈64%
recovered as structured data; the remainder is flex DFA tables, vtables, padding,
and loose strings). The corpus lives in [`ptxas/extracted/`](https://github.com/GrigoryEvko/crucible-notes/tree/main/ptxas/extracted)
and is regenerated in ~0.5 s by `ptxas/tools/extract_rodata.py` (stdlib-only):

```bash
cd ptxas && python3 tools/extract_rodata.py     # --binary ptxas --output extracted/
```

`manifest.json` records the binary SHA-256, VA layout, and a checksum per file.
All VA constants are pinned to v13.0.88; a different build needs new addresses.

This page indexes the corpus and links each group to the wiki section it backs.
The tables *are* the primary evidence behind those pages; cite them directly when
you need the full data rather than the representative rows quoted in prose.

## Conventions

- **Opcode index** — the runtime integer key for a SASS instruction (0–321). This
  is ptxas's internal id, **not** the SASS binary opcode field;
  `opcode_master.json` is the canonical index → mnemonic + encoding map.
- **ROT13** — user-visible names (opcodes, knobs, passes) are ROT13-obfuscated in
  the image; extracted names are decoded, with the raw form kept in a `rot13` field.
- **Sentinels** — `-1`/`0xFFFFFFFF` = unused slot; `355` in opcode→encoding = take
  the extended dispatch path; `0xBA9E23` in ISel dispatch = no-match stub.
- **SM generation** — most tables are per-SM or per-family (`sm_7x`, `sm_8x`, `sm_10x`),
  spanning sm_30 (Kepler) through sm_121 (consumer Blackwell).

## Opcode & instruction identity

Backs [SASS Opcode Catalog](sass-opcodes.md), [PTX Instruction Table](ptx-instructions.md), [Instructions & Opcodes](../ir/instructions.md).

| File | Records | Contents |
|---|---|---|
| `opcode_names.json` | 322 + Mercury | ROT13-decoded SASS mnemonics; index = runtime opcode id (0=ERRBAR, 1=IMAD, 321=LAST) |
| `opcode_master.json` | 322 | Canonical per-opcode record: name + encoding category + ISel slot + SM generation |
| `encoding_category_map.json` | 322 | opcode index → encoding category (verified identity map) |
| `opcode_to_encoding.json` | 222 | opcodes 0–221 → ISel encoding slot; `355` = extended path; ≥222 use virtual dispatch |
| `extended_sass_names.json` | 467 | ROT13-decoded mnemonics with modifiers (FFMA2, FENCE.T, DSETP, …) |
| `modifier_format_strings.json` | 533 | ROT13-decoded format templates with modifier placeholders |

## SASS instruction encoding (Mercury pipeline)

Backs [SASS Instruction Encoding](../codegen/encoding.md), [Encoding Dispatch Tables](../codegen/encoding-tables.md).

| File | Records | Contents |
|---|---|---|
| `format_descriptors.json` | 38 | Per-format encoding geometry (136-byte descriptors: width + slot_sizes/types/flags); 15 unique format IDs; 1-slot=64b, 2-slot=128b, ID 16=256b |
| `universal_slot_template.json` | 3×10 | Default slot geometry (7,302 xrefs — most-referenced encoding table); sizes `[3,2,4,6,8]` |
| `encoding_bitfield_lookup.json` | 4,096 | modifier combination → SASS bitfield position (98% fill) |
| `encoding_constants.json` | 8 sub-tables | Structured encoding-slot lookups + 128 packed u16 slot pairs |
| `encoding_geometry.json` | 38 formats | Derived: format descriptors × tier-2 layout params |
| `encoding_trees.json` | 2 trees / 13,568 nodes | Hierarchical encoding decision trees (internal child ptrs; leaves = encoding id + `.text` handler VA) |
| `tier2_modifiers.json` | 6 groups / 28 xmmwords | Per-SM-generation encoder layout params (loaded at encoder ctx +404); each group carries its `sm_range` |
| `modifier_value_tables.json` | 40 arrays | Ori IR modifier enum → SASS binary value (tristate/quaternary + reordering tables) |
| `instruction_legality.json` | 60,416 sparse | (opcode, modifier combo) → legality flags; `0x08000000` = special validation |

## SASS handler dispatch

Backs [Encoding Dispatch Tables](../codegen/encoding-tables.md), [Mercury Encoder](../codegen/mercury.md).

| File | Records | Contents |
|---|---|---|
| `sass_handler_dispatch_1.json` | 6,915 | opcode id → SASS encoder `.text` address (Format A/B variants; `opcode_id = (category<<8) \| variant`) |
| `sass_handler_dispatch_2.json` | 3,511 | Second handler table (full-prologue encoders) |
| `per_sm_handler_dispatch.json` | 5 SM-gen tables | Per-SM encoding handler dispatch (SM50-7x, 75, 80-8x, 86-89, 100+); 492 opcodes shared across gens |

## Instruction scheduling & latency

Backs [Latency Model](../scheduling/latency-model.md), [Scheduler Overview](../scheduling/overview.md), [Scoreboards](../scheduling/scoreboards.md).

| File | Records | Contents |
|---|---|---|
| `per_sm_latency_tables.json` | 256+430+619 | Per-FU 72-byte records (unit_id, 2×8 pipe masks, 12 sched params); sm_8x/10x/7x |
| `per_sm_dependency_rules.json` | 11 SM tables | Per-unit 40-byte dependency rules (latency, throughput⁻¹, barrier latency/throughput, r/w latency, stall, issue slots); sm_60…sm_103 |
| `per_sm_scoreboard_configs.json` | 7 SM tables | Per-unit 88-byte scoreboard config (≤6 (id,threshold,mask) triplets); sm_100 richest |
| `scheduling_vtable.json` | 77 ptrs | Scheduling backend vtable (8 core + 3×23 per-gen pipeline-query methods) |
| `sched_encoder_dispatch.json` | 330 + vtables | Dispatch tables for the 85 KB scheduling encoder (`sub_89FBA0`): 330-entry jump table, 773 identity perm, resource-class dispatch |
| `opcode_pipeline_map.json` | sm_10x:31, sm_7x:37 | (opcode, pipeline flags) → execution pipe (1=ALU, 2=FP64, 3=SFU, …) |
| `sm_scheduling_seeds.json` | 50 | (sm_id, gen_code, variant) → per-SM scheduling builder; gen 1=Fermi…9=Thor |

## Register allocation

Backs [Allocator Architecture](../regalloc/overview.md), [Fatpoint Algorithm](../regalloc/algorithm.md), [GPU ABI](../regalloc/abi.md).

| File | Records | Contents |
|---|---|---|
| `register_file_config.json` | 2,784 u32 | Per-SM resource limits (GPR banks 120, pred 64, uniform 256, barriers 32, …) |
| `register_class_aux.json` | sm_10x:97, 8x:24, 7x:150 | Per-SM register-class descriptors (64-byte: class_id, variants, range bounds) |
| `register_class_constraints.json` | 3×72 | Per-SM operand register-class constraints (≤5 (class,sub_a,sub_b) triplets) |
| `regalloc_init_data.json` | 196-ptr vtable + 1,218 reg ids | Allocator init (dispatch vtable, SM-variant groups, reg-id arrays with `bank<<16\|reg`) |
| `occupancy_constants.json` | 8 xmmword | Per-family occupancy-formula params |
| `operand_resource_strategy.json` | 6 vtables + 9 jump tables | Per-SM operand resource-cost eval (register-count matrices, min 13 / max 128) |

## Instruction selection (ISel)

Backs [Instruction Selection](../codegen/isel.md).

| File | Records | Contents |
|---|---|---|
| `isel_dispatch_tables.json` | 1,885 ptrs | ISel DAG pattern-matcher dispatch (273 = no-match sentinel `0xBA9E23`; all verified in `.text`) |
| `isel_node_descriptors.json` | vtable pool + 2 descriptors | ISel node type system (polymorphic node vtables, operand field-offset blocks) |
| `isel_operand_constraints.json` | 39 × 0x100 | Per-arch operand-constraint records (9 opcodes; 25 type ids; 47-entry operand vtable; 399-entry op vtable) |

## SM architecture & targets

Backs [SM Architecture Map](../targets/index.md).

| File | Records | Contents |
|---|---|---|
| `sm_id_enumeration.json` | 28 | Supported compute capabilities sm_30…sm_121 |
| `sm_version_codes.json` | 128 u16 | internal arch index → SM version code (`bits[15:12]=major×10, [11:8]=minor, [7:0]=variant`; `0x9004`=sm_90a) |
| `shared_memory_configs.json` | 11 + 3 | Per-SM shared-memory size options (0…335,872 B) |

## Compiler configuration

Backs [Knobs System](../config/knobs.md), [DUMPIR & NamedPhases](../config/dumpir.md), [Pass Inventory](../passes/index.md).

| File | Records | Contents |
|---|---|---|
| `phase_names.json` | 159 | Optimization pipeline phase names (0=OriCheckInitialProgram, 158=NOP) |
| `knob_strings.json` | 1,142 | ROT13-decoded internal tuning-knob names (OCG + DAG regions) |
| `okt_knob_descriptors.json` | 994 | Structured knob descriptors (type, default, params, flags, `.bss` offset; names linked at runtime) |
| `supplemental_pass_names.json` | 100 | Scheduler/scoreboard pass + feature names (SbXBlock*, SchedLds*, …) |

## Embedded code & intrinsics

Backs [Intrinsic Table](../intrinsics/index.md), [Pseudo-Instruction Macros](../intrinsics/pseudo-instruction-macros.md), [String-Pool Encryption](string-pool-cipher.md).

| File | Records | Contents |
|---|---|---|
| `embedded_ptx_intrinsics.json` | 1,080 | `.weak .func` prototypes for CUDA builtins (WMMA/MMA/barrier/shared/sanitizer); categories cuda_other 549 / sm70 433 / sm20_math 70 / redux_sync 17 / sanitizer 7 |
| `wgmma_intrinsic_infra.json` | 469 handlers + enums | WGMMA infra (pipeline depth=32/max_pending=128/latency=24; 469 PTX-intrinsic lowering handlers; 100 Mercury opcode enum arrays) |
| `high_entropy_blob.json` | metadata | Describes the 2.80 MB precompiled SASS stub region (607 `__cuda_*` builtins; entropy 7.998; SHA-256) |
| `manifest.json` | — | Binary metadata + per-file checksums |

## Cross-References

- [SASS Opcode Catalog](sass-opcodes.md) · [PTX Instruction Table](ptx-instructions.md)
- [SASS Instruction Encoding](../codegen/encoding.md) · [Latency Model](../scheduling/latency-model.md)
- [Methodology](../methodology.md) — how the binary was swept and decoded.
