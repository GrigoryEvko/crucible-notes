# ptxas v13.0.88 — Extracted .rodata Tables

Machine-readable extraction of every static constant table from the ptxas binary's `.rodata` section. 45 JSON files, 13 MB total, covering 100% of the 7.16 MB `.rodata` section (63.9% extracted as structured data, 36.1% identified as DFA tables / vtables / padding / strings not worth extracting).

**Source binary**: ptxas v13.0.88 from CUDA Toolkit 13.0 (37.7 MB stripped x86-64 ELF)
**Extraction script**: `../tools/extract_rodata.py` — run with `python3 extract_rodata.py` from `ptxas/`
**Runtime**: ~0.5 seconds, zero dependencies beyond Python 3.10+ stdlib

---

## Quick Start

```python
import json

# Load the 322 SASS opcode mnemonics
opcodes = json.load(open("opcode_names.json"))
for entry in opcodes["opcode_names"]["entries"]:
    print(f'Opcode {entry["va"]}: {entry["mnemonic"]}')
    # -> "Opcode 0x21C1DC8: ERRBAR", "Opcode 0x21C1DC2: IMAD", ...

# Load per-SM latency tables for scheduling constraints
latency = json.load(open("per_sm_latency_tables.json"))
for unit in latency["per_sm_latency_tables"]["sm_8x_shared"]["entries"]:
    print(f'Unit {unit["unit_id"]}: params={unit["sched_params"][:4]}')

# Load format descriptors for SASS instruction encoding geometry
fmts = json.load(open("format_descriptors.json"))
for f in fmts["format_descriptors"]:
    active = [s for s in f["slot_sizes"] if s != -1]
    print(f'{f["label"]}: {f["instruction_width"]}-bit, slots={active}')
```

---

## File Catalog

### Opcode & Instruction Identity

| File | Size | Records | What it is |
|------|------|---------|------------|
| `opcode_names.json` | 98K | 322 primary + Mercury extended | ROT13-decoded SASS instruction mnemonics. Index = runtime opcode ID. Entry 0 = ERRBAR, 1 = IMAD, 321 = LAST. Mercury names are supplementary (HMMA, GMMA, DMMA, etc.). |
| `opcode_master.json` | 60K | 322 cross-referenced records | Per-opcode master record joining name + encoding category + ISel encoding slot + SM generation. The single lookup table. |
| `encoding_category_map.json` | 3.5K | 322 × int32 | Maps opcode index → encoding category. Currently a verified identity map (category[i] = i). |
| `opcode_to_encoding.json` | 15K | 222 × uint16 | Maps opcodes 0–221 to ISel encoding slot indices. Sentinel 355 = "use extended path". Opcodes ≥ 222 use virtual dispatch instead of this table. |
| `extended_sass_names.json` | 69K | 467 strings | Additional ROT13-decoded SASS mnemonics with modifiers (FFMA2, FENCE.T, DSETP, etc.). |
| `modifier_format_strings.json` | 96K | 533 strings | ROT13-decoded instruction format templates with modifier placeholders (e.g. `SYNCS.ARRIVE.A1TR.ART0.A0TR.A0TX`). |

### SASS Instruction Encoding (Mercury Pipeline)

| File | Size | Records | What it is |
|------|------|---------|------------|
| `format_descriptors.json` | 29K | 38 descriptors | Encoding geometry per format group. Each 136-byte descriptor: xmmword header (format_id, width) + 3 × DWORD[10] arrays (slot_sizes, slot_types, slot_flags). 15 unique format IDs. 1-slot = 64-bit, 2-slot = 128-bit, 3-slot = wide 128-bit, ID 16 = 256-bit. |
| `universal_slot_template.json` | 1.2K | 3 × 10 DWORDs | Default slot geometry template (7,302 xrefs — most-referenced table in encoding). Sizes [3,2,4,6,8], overridden per format descriptor. |
| `encoding_bitfield_lookup.json` | 185K | 4,096 × (u32, u32) | Maps modifier combinations to SASS bitfield positions. 98% fill rate. Field A = .text function pointer or small index; field B = count/flags (mostly 0). |
| `encoding_constants.json` | 13K | 8 sub-tables + 128 u16 pairs | Structured encoding slot ID lookups. NOT a flat array — 8 small indexed sub-tables with sentinel values (1149–1230 range), plus 128 packed u16 encoding slot pairs. |
| `encoding_geometry.json` | 317K | 38 formats × modifiers | Derived cross-reference: format descriptors + tier-2 layout parameters combined. |
| `encoding_trees.json` | 2.5M | 2 trees, 13,568 nodes | Hierarchical encoding decision trees. Internal nodes have child pointers; leaves hold encoding IDs and .text handler VAs. Tree 1: 6,144 slots (274 internal). Tree 2: 7,424 slots (427 internal). |
| `tier2_modifiers.json` | 6.8K | 6 groups, 28 xmmwords | Per-SM-generation encoding layout parameters loaded into encoder context at +404. Not "modifiers" — they configure how the encoder partitions the instruction bit-space. |
| `modifier_value_tables.json` | 21K | 40 lookup arrays | Map Ori IR modifier enum values → SASS binary encoding values. Most common: tristate [0,1,2] (63 helper refs), quaternary [0,1,2,3] (44 refs). Non-identity tables handle reordering and stride multiplication. |
| `instruction_legality.json` | 1.5M | 60,416 entries (sparse) | Maps (opcode, modifier_combination) → legality flags. 68.4% zeros — stored as sparse array (only 19,086 non-zero). Flag 0x08000000 = special validation required. |

### SASS Handler Dispatch (Opcode → Encoder Function)

| File | Size | Records | What it is |
|------|------|---------|------------|
| `sass_handler_dispatch_1.json` | 1.3M | 6,915 entries | Maps opcode IDs to SASS encoding handler .text addresses. Sub-table structure with Format A/B entry variants. First sub-table = validation stubs; later = full encoders. `opcode_id = (category << 8) \| variant`. |
| `sass_handler_dispatch_2.json` | 619K | 3,511 entries | Second handler dispatch table. Real encoding functions with full prologues. |
| `per_sm_handler_dispatch.json` | 2.0M | 5 SM-generation tables | Per-SM encoding handler dispatch: SM50-7x, SM75, SM80-8x, SM86-89, SM100+. 492 opcodes shared across all generations. |

### Instruction Scheduling & Latency

| File | Size | Records | What it is |
|------|------|---------|------------|
| `per_sm_latency_tables.json` | 843K | 3 tables: 256+430+619 entries | Per-functional-unit 72-byte records: unit_id, 2×8 pipeline availability masks (0xFF = unused pipe), 12 scheduling parameters (latency, throughput, stall counts). SM 8x shared (256 units), SM 10x (430), SM 7x (619). |
| `per_sm_dependency_rules.json` | 1.5M | 11 per-SM tables | Per-unit 40-byte dependency rules: latency, throughput_inv, barrier_latency, barrier_throughput, read/write_latency (-1 = N/A), stall_cycles, issue_slots. One table per SM: sm_60/70/72/75/80/86/89/90/90a/100/103. |
| `per_sm_scoreboard_configs.json` | 171K | 7 per-SM tables | Per-unit 88-byte scoreboard config: up to 6 (scoreboard_id, threshold, mask) triplets. sm_100 is richest (75 entries, up to 6 triplets); sm_80/86/90 use 1 triplet (31 entries). |
| `scheduling_vtable.json` | 17K | 77 function pointers | Scheduling backend virtual dispatch table: 8 core methods + 3 × 23 per-SM-generation pipeline query methods. |
| `sched_encoder_dispatch.json` | 57K | 330 jump table + 126+65 vtables | The dispatch tables for sub_89FBA0 (85KB scheduling encoder): 330-entry opcode jump table (the "no jump table" claim was wrong), 773-element identity permutation, 14-entry resource class dispatch, 2 C++ vtables, sub-opcode tables, SSE bitmask, double 1000000.0 (clock conversion). |
| `opcode_pipeline_map.json` | 7.4K | SM 10x: 31, SM 7x: 37 pairs | Sorted (opcode_id, pipeline_flags) pairs mapping opcodes to execution pipeline assignments. Flags: 0=special, 1=ALU, 2=FP64, 3=SFU, 4=other. |
| `sm_scheduling_seeds.json` | 8.4K | 50 triplets (45 active) | (sm_id, gen_code, variant) seeds selecting per-SM scheduling builder function. Gen codes: 1=Fermi → 9=Thor. |

### Register Allocation

| File | Size | Records | What it is |
|------|------|---------|------------|
| `register_file_config.json` | 28K | 2,784 uint32 values | Per-SM resource limits: GPR banks (120), predicate regs (64), uniform regs (256), barriers (32), warp sizes, etc. First 8 = [120, 120, 64, 256, 32, 8, 32, 4]. |
| `register_class_aux.json` | 143K | SM 10x: 97, SM 8x: 24, SM 7x: 150 | Per-SM register class descriptors (64-byte records): class_id, sub-variants, range bounds. Flag 1 = simple class, 2 = extended (uses auxiliary refs). |
| `register_class_constraints.json` | 153K | 3 × 72 records | Per-SM register operand constraint tables (SM 3x/4x/5x). Each 64-byte row: up to 5 (class_id, sub_a, sub_b) triplets. Rows 0–39 = anonymous/inline, rows 40–71 = class-based. |
| `regalloc_init_data.json` | 38K | 196-ptr vtable + 1,218 register IDs | Register allocator initialization: 196-entry method dispatch vtable (122 are NOP stubs, 11 real handlers), 6 SM-variant method groups, 10 register ID arrays with bank encoding (bank << 16 \| reg_number), sentinel patterns, diagnostic strings. |
| `occupancy_constants.json` | 2.6K | 8 × xmmword (128-bit) | Occupancy formula parameters per SM family. Formula: `max_warps = (-granularity & (2 * half_reg_file / regs)) - offset`. Used by sub_A99FE0 (7 lines). |
| `operand_resource_strategy.json` | 13K | 6 vtables + 9 jump tables + 2 matrices | Per-SM operand resource cost evaluation: strategy vtables, operand type dispatch, register-count lookup matrices (min 13, max 128). |

### Instruction Selection (ISel)

| File | Size | Records | What it is |
|------|------|---------|------------|
| `isel_dispatch_tables.json` | 34K | 1,885 function pointers | ISel DAG pattern matcher dispatch. 273 entries = sentinel (no-match stub at 0xBA9E23). All 1,885 pointers verified in .text. |
| `isel_node_descriptors.json` | 98K | Vtable pool + 2 descriptor objects | ISel node type system: vtable pool (sub-vtables for polymorphic node types), two descriptor objects with operand field offset blocks, handler dispatch arrays. |
| `isel_operand_constraints.json` | 52K | 39 records × 0x100-byte stride | Per-architecture operand constraint records for 9 opcodes (JMX, UTMAPF, VIADD, CREDUX, etc.). 25 type IDs, 47-entry operand handler vtable, 399-entry instruction operation vtable. |

### SM Architecture & Targets

| File | Size | Records | What it is |
|------|------|---------|------------|
| `sm_id_enumeration.json` | 3.5K | 28 SM IDs | Canonical list of supported compute capabilities: sm_30 through sm_121 (26 active + 2 null separators). |
| `sm_version_codes.json` | 12K | 128 × uint16 | Maps internal ptxas arch indices → SM version codes. Encoding: `bits[15:12]=major_tens, [11:8]=minor, [7:0]=variant`. e.g. 0x9004 = sm_90a. |
| `shared_memory_configs.json` | 645B | 11 global + 3 SM 75 sizes | Shared memory size options per SM. Global: 0 to 335,872 bytes (ascending). SM 75 (Turing): 3 sizes. |

### Compiler Configuration

| File | Size | Records | What it is |
|------|------|---------|------------|
| `phase_names.json` | 18K | 159 phase names | All 159 optimization pipeline phase names. Index 0 = OriCheckInitialProgram, 158 = NOP. Dereferenced from pointer table at 0x22BD0C0. |
| `knob_strings.json` | 215K | 1,142 entries (1,126 ROT13 + 16 plaintext) | ROT13-decoded internal compiler tuning knob names from OCG and DAG regions. 21 known false positives (instruction mnemonics) filtered out. |
| `okt_knob_descriptors.json` | 215K | 994 entries × 9 fields | Structured knob descriptor table: type (OKT_INT/OKT_NONE/OKT_FLOAT/OKT_BDGT/OKT_STR/etc.), default value, 3 parameters, flags, .bss offset. Name field empty in static image — linked to knob_strings at runtime. |
| `supplemental_pass_names.json` | 15K | 100 names (98 ROT13 + 2 plaintext) | Scheduler/scoreboard pass and feature names: SbXBlock*, SchedLds*, IssueDelay*, DumpCFG, OptimizeNaNOrZero, etc. |

### Embedded Code & Intrinsics

| File | Size | Records | What it is |
|------|------|---------|------------|
| `embedded_ptx_intrinsics.json` | 544K | 1,080 PTX declarations + string table | Complete `.weak .func` prototypes for all CUDA builtin intrinsics: WMMA, MMA, barrier ops, shared memory ops, sanitizer hooks. Categories: cuda_other (549), sm70 (433), sm20_math (70), redux_sync (17), sanitizer (7). |
| `wgmma_intrinsic_infra.json` | 77K | 12 pipeline params + 469 handler ptrs + vtables + enum tables | WGMMA (Warp Group Matrix Multiply-Accumulate) infrastructure: pipeline config (depth=32, max_pending=128, latency=24), 10 warning message strings, 469 PTX intrinsic lowering handlers, 19 operand type vtables, 100 Mercury opcode enum lookup arrays. |
| `high_entropy_blob.json` | 1.1K | Metadata only | Documents the 2.80 MB pre-compiled SASS function stub region (607 `__cuda_*` builtin functions + Fermi-era macro expansions). Entropy 7.998 bits/byte. SHA-256 fingerprint. Preceded by 607-entry blob offset index. |

### Derived / Cross-Reference

| File | Size | What it is |
|------|------|------------|
| `manifest.json` | 8.9K | Binary metadata (SHA-256, version, VA layout) + per-file checksums. |

---

## Key Concepts

**Virtual Address (VA)**: All addresses in these files are virtual addresses from the ptxas ELF image. File offset = VA − 0x400000. The binary is non-PIE with VA base 0x400000.

**ROT13 encoding**: NVIDIA obfuscates most user-visible strings (opcode names, knob names, pass names) with ROT13. All extracted names are already decoded to human-readable form. The original ROT13 form is preserved in the `rot13` field.

**Opcode index**: The integer key for SASS instructions. Range 0–321. This is the runtime opcode value used internally by ptxas — it is NOT the SASS binary opcode field. The `opcode_master.json` file is the canonical mapping from index to mnemonic + encoding info.

**SM generation**: GPU architecture target. The tables span SM 30 (Kepler) through SM 121 (consumer Blackwell). Most tables are per-SM or per-SM-family (sm_7x, sm_8x, sm_10x).

**Sentinel values**: `-1` (0xFFFFFFFF) in slot arrays = unused entry. `355` (0x163) in opcode-to-encoding = use extended dispatch path. `0xBA9E23` in ISel dispatch = no-match pattern stub.

---

## Usage for SMT-Based SASS Code Generation

The primary consumer of these tables is an SMT/Z3-based SASS code generator. Here's which files feed which solver constraints:

**Instruction encoding** (what bits to emit):
- `format_descriptors.json` → instruction width + slot geometry per format group
- `universal_slot_template.json` → default slot layout (base case)
- `encoding_bitfield_lookup.json` → modifier → bit position mapping
- `modifier_value_tables.json` → modifier enum → binary value translation
- `sass_handler_dispatch_1.json` + `_2.json` → opcode → encoding handler address (for decompilation)
- `encoding_trees.json` → encoding decision tree structure

**Instruction scheduling** (ordering + barrier assignment):
- `per_sm_latency_tables.json` → functional unit latency/throughput per SM
- `per_sm_dependency_rules.json` → dependency latency/barrier constraints per SM
- `per_sm_scoreboard_configs.json` → scoreboard barrier configuration per SM
- `opcode_pipeline_map.json` → opcode → functional unit pipeline

**Register allocation** (how many registers, which classes):
- `register_file_config.json` → per-SM register bank sizes and limits
- `register_class_aux.json` → register class definitions per SM
- `register_class_constraints.json` → operand register class constraints
- `occupancy_constants.json` → occupancy formula parameters
- `regalloc_init_data.json` → register ID arrays with bank encoding

**Instruction legality** (what's valid on this SM):
- `instruction_legality.json` → (opcode, modifier) → legal flag
- `sm_id_enumeration.json` → supported SM targets
- `sm_version_codes.json` → internal arch index → SM version

---

## Regeneration

```bash
cd ptxas
python3 tools/extract_rodata.py                    # defaults: --binary ptxas --output extracted/
python3 tools/extract_rodata.py --binary /path/to/ptxas --output /tmp/out
```

The script reads the binary once (37.7 MB → memory), extracts all 45 tables in ~0.5s, and writes JSON + manifest with SHA-256 checksums. All VA constants are hardcoded for ptxas v13.0.88 — a different version will require updating the addresses.
