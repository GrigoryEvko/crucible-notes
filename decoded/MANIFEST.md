# Deciphered CUDA-toolchain data — decoder toolkit

This directory documents the four data-obfuscation schemes embedded in NVIDIA's
CUDA 13.x compiler toolchain and ships the **decoder/extractor tools** plus the
**uncopyrightable facts** we recovered from them. Everything here was obtained by
static + dynamic reverse engineering of the freely-distributed binaries — no
source, no NDA.

Toolchain versions: ptxas / cicc / nvlink = CUDA **13.0.88** (our IDA corpus)
and **13.1.x** (system binaries under `/usr/local/cuda-13.1`); nvdisasm =
**V13.1.115**.

## Scope — what is published here vs kept local

DMCA 17 U.S.C. § 1201(f) (and *Sega v. Accolade*, *Sony v. Connectix*, EU
2009/24/EC Arts. 5–6) protects the **act of decrypting and studying** a publicly
distributed binary for interoperability and research, and the **disclosure of the
circumvention means** to others for that purpose. It does **not** grant a license
to *redistribute NVIDIA's copyrighted expression wholesale*. So the line we draw:

**Published (in git):**
- **Our decoder/extractor tools** (`tools/`, `ptxas-scheduling/extract_sched_tables.py`).
  Disclosing circumvention means for interoperability is exactly what § 1201(f)(2)–(3)
  permits, and the code is our own.
- **Uncopyrightable facts** we extracted: numeric scheduling/latency values, opcode
  ids, the cipher algorithm and constants (`ptxas-scheduling/*.txt`, the cipher map
  below). Raw facts and functional data are not copyrightable (*Feist v. Rural*).
- **This methodology write-up.**

**Kept local only (git-ignored, never redistributed):**
- The **wholesale decoded NVIDIA data tables** — the PTX-macro pools, the SASS-ISA
  grammar dumps, the cicc name pools. These are NVIDIA-authored creative/compiled
  expression; § 1201(f) lets us decrypt and analyze them, not republish them. Our
  *analysis* of their contents lives in the wiki as commentary; the raw bytes
  stay off-repo.
- **Third-party material** (redplait's extractor and his decrypted pool dump) — not
  ours to redistribute.

Anyone can regenerate the local-only outputs from their own CUDA install using the
published tools below; we ship the keys and the method, not NVIDIA's bytes.

## Cipher map (four schemes)

| Binary | Concealed data | Cipher | Decoder |
|---|---|---|---|
| ptxas / nvlink | PTX-macro lowering pool (~1.85 MB) | LCG keystream ⊕ S-box ⊕ ciphertext-feedback, key `0x5389A4F8` | `tools/decode_pool.py` |
| nvdisasm | per-arch SASS ISA tables | same stream cipher (per-arch key) wrapping an **LZ4** block | `tools/nvdisasm_decode.py` |
| cicc | LLVM/Clang name tables + PTX mnemonics | position-XOR `plain[i] = cipher[i] ^ ((3*i) & 0xFF)` | `tools/decode_blobC.py` |
| ptxas | opcode + tuning-knob names | ROT13 (static ctor) | (documented in ptxas wiki) |

The ptxas/nvlink/nvdisasm stream cipher (per output byte, MSB-first keystream):

```
M = 0x41C64E6D, INC = 0x3039            # glibc/MS LCG constants
state = key                             # 32-bit; ptxas/nvlink key = 0x5389A4F8
al    = (~key) & 0xFF                    # ciphertext-feedback register
cnt   = 0
for each cipher byte c:
    if cnt == 0: state = (state*M + INC) & 0xFFFFFFFF; ks = state; cnt = 4
    else:        ks >>= 8
    cnt -= 1
    plain = SBOX[(al ^ c) & 0xFF] ^ (ks & 0xFF)
    al    = c                            # chain on the CIPHER byte
    emit plain
```

ptxas decodes the whole pool once (decoder `sub_430710`, init `sub_4305D0`);
nvdisasm decodes per-arch sub-tables on demand (decoder `0x87620`, S-box at
`.rodata` file offset `0xF4680`) then LZ4-decompresses each. The S-box is read
out of the user's own binary at run time — it is not shipped here.

## Published contents

### Top-level report
- `sm_coverage.md` — SM-version coverage + correctness matrix across every per-arch
  table in `decoded/`, for the full Blackwell lineup (sm_100/103/110/120/121), with the
  two-oracle (13.0.88 + 13.1.115) ground truth (e_flags, `__CUDA_ARCH__`, max-regs, FP64
  DFMA gap, SASS byte-identity, ELF-container details).

### `tools/` — decoders (our code)
- `decode_pool.py` — reproduces the ptxas/nvlink PTX-macro pool from the on-disk
  binary (reads the encrypted blob + S-box out of the ELF you point it at).
- `nvdisasm_decode.py` — decodes the per-arch SASS ISA tables from a user-supplied
  nvdisasm `.data` blob; requires `lz4` (`pip install lz4`).
- `decode_blobC.py` — cicc position-XOR decoder.
- `blob_scan.py` — generic ELF entropy/xref scanner used to locate the blobs.
- `decode_pseudo_names.py` — de-obfuscates ptxas's 473 numeric pseudo-instruction
  handler keys (e.g. `1030557441`): each is `adler32(__cuda_* macro name)`, inverted
  by hashing the `__cuda_*` names harvested from the macro pool + string tables.

### `sass-tools/` — SASS-ISA model tooling (our code)
- `sass_isa_parser.py` — reconstructs each instruction class's bit-field layout from
  the decoded SASS-ISA tables (per-sub-form `OPCODES`+`ENCODING` pairing). `sass_decode.py` —
  table-driven 128-bit decoder (opcode `{91}++{11:0}`, operands, control word);
  round-trip-validated 368 instrs / 8 arches @ 100% via `sass_roundtrip.py`.
- `sass_ctrl_decode.py` — scheduling-control-word decoder; `sass_sched_sim.py` — single-warp
  issue/timing simulator (validated: 221 producer→consumer scoreboard pairs, opex invariant
  1912/1912). `sass_legality.py` — constraint checker + `iadd3`/`lop3`/`shf`/`prmt` functional
  model; `sass_validate.py` — hypothesis harness vs real ptxas SASS.
- `sass_sem_int.py` / `sass_sem_fp.py` / `sass_warp.py` — bit-exact functional models
  (integer/logic, IEEE float, 32-lane warp collectives + MMA), each gated by a live-GPU
  differential harness (`sass_gpu_probe.py`, `sass_launch.py` generic cubin launcher).
  `sass_scheduler.py` with `sass_smt.py`/`sass_optsched.py` — latency-optimal scheduler.
- All require the user to regenerate the (local-only) decoded tables first via
  `tools/nvdisasm_decode.py`. Documented in the ptxas wiki `sass-isa/` section.

### `sass-bitsem/` — typed, machine-checked bit semantics (Lean 4, our code)
- `SassBitsem/{Logic,Convert,Synthesis}.lean` — the SASS bit functions as width-typed
  `BitVec` operations (lop3 LUT, funnel shift, bmsk; integer casts + bf16↔f32 surgery;
  invented-op synthesis). Every identity is proved by `bv_decide` (bit-blast → SAT), so it
  holds for all inputs with a kernel-checked term. `lake build`; no external deps. The
  typed companion to the executable `sass-tools/` models.

### `ptxas-scheduling/` — the SASS scheduling model (tool + extracted facts)
- `extract_sched_tables.py` — extracts both tables below from a `.rodata` dump of
  your own ptxas (our code).
- `sched_class_table.txt` — the 256 per-scheduling-class descriptors (id 2..771):
  pipe-eligibility masks, dual-issue masks, throughput class, max-stall, per-class
  params. Numeric facts read out of `.rodata` VMA `0x2297C00` (72-byte stride).
- `scalar_latency_oracle.txt` — the per-Ori-opcode latency bands (6 / 13 / 24 / 30 /
  300) ptxas's OCG scheduler reads, built at runtime by `sub_738E20`, queried via
  `sub_8BF3A0` (`oracle+744`). The flattened scalar model — the dense producer×
  consumer hazard matrix is a build-time DSL that ships in no binary.
- `README.md` — column semantics + reproduce steps.

### `ptxas-tokens/` — the PTX lexer vocabulary (facts)
- `ptx_tokens.tsv` — token id → keyword(s); `ptxas_action_to_token.tsv` — the
  552-action flex map. Recovered from the embedded flex DFA; dumper at
  `tools/ptxas_flex_tokens.py`.

### `ptxas-instr-defs/` — the instruction registry (facts + our solver)
- `instruction_table.tsv` — 1410 registrations / 268 names (operand-type signature,
  datatype string, 128-bit attribute mask). `attribute_bits.tsv` — the recovered
  bit → attribute legend. `extract_instruction_table.py` / `solve_attribute_bits.py` —
  our extractor + correlation solver. `union_mask.txt` — the 110 used bits.

### `ptxas-pseudo-instructions/` — the `sub_5D4190` registry (facts)
- `pseudo_instruction_names.tsv` — 115 named + 473 `adler32 → __cuda_* name` rows.
  `macro_catalog.tsv` — per-handler family / arch / one-line expansion summary (our
  prose). Regenerate names with `tools/decode_pseudo_names.py`.

### `ptxas-messages/` — the diagnostic catalog (facts)
- `messages.tsv` — 506 messages (id, severity, text); `extract_messages.py` — our
  extractor. All strings are cleartext in the binary.

### `ptxas-isel/` — instruction selection (facts + our tools)
- Static: `opcode_to_encoding.tsv` (opcode→slot, `sm_gen` = introduction
  generation), `isel_dispatch_groups.tsv` (144 groups), `isel_operand_constraint_records.tsv`,
  `isel_node_vtables.tsv`. Behavioural per-arch: `per_arch_opcode_histogram.tsv` +
  `per_arch_encoding_opbyte.tsv` (sm_90a/100/103/110/120/121 SASS selection for a
  fixed probe) via `extract_per_arch_isel.py`. See its README for the
  consumer-vs-datacenter move-idiom / integer-ALU differences.

### `ptxas-encoding-full/` — SASS-encoding dispatch (facts + our tools)
- The seven per-arch encoder blocks in `.rodata 0x22E7AD0..0x23EFB60`: blocks 0–4
  (sm50_7x/sm75/sm100/sm80_8x/sm86_89), block 6 (sm_110 Jetson Thor) and block 7
  (sm_120/121 consumer Blackwell) — `arch_blocks.tsv`,
  `new_arch_blocks_sm110_sm120.tsv`, `per_arch_encoder_blocks.tsv` via
  `extract_arch_blocks.py`. Blocks 6/7 share 0 emitters with the classic family
  (disjoint `.text` code). `sass_class_presence_by_arch.tsv` cross-checks ISA-class
  coverage (tcgen05/TMEM only on sm_100/103/110; RT/TTU + consumer-tensor only on
  sm_120). See its README.

### `ptxas-ir/` — the Ori internal IR (facts)
- `ir_node_layout.tsv` (296-byte instruction-object field map), `opcode_enum.tsv`
  (322 primary Ori opcodes, ROT13-decoded, with `sm_gen`), `opcode_enum_mercury.tsv`
  (385 Mercury/SM103 tensor extended opcode names), `operand_packed_word.tsv` (packed
  inline-operand bitfield), `operand_kind_enum.tsv` (ISel operand-descriptor kinds).
  All recovered from the stripped binary; see its README.

### `ptxas-passes/` — the optimization phase pipeline (facts)
- `phase_pipeline.tsv` — 159 phases: default `exec_order`, authoritative `bin_index`
  (phase-name table at `0x22BD0C0` + factory switch), plaintext `phase_name`,
  `name_string_va`, analytical category, and the layer-1 opt-level gate per phase.

### `ptxas-passes-detail/` — per-phase deep-dive (facts)
- `passes_detail.tsv` — one row per of 14 high-leverage phases:
  `phase | bin_index | vtable_va | execute_thunk_va | worker_or_slot | dispatch_kind |
  role | key_transform`. Grounded in disassembly + vtable layout.

### `ptxas-driver/` — compilation driver, O0–O5 model, recipe DSL (facts + prose)
- `phase_index.tsv` (factory/dispatch phase index), `phase_order_table.txt` (default
  order), `ocg_knobs.tsv` (OCG tuning knobs), `recipe_override_dsl.tsv` (the
  NvOptRecipe / named-phases override grammar, option 298). Prose: `driver_callgraph.md`,
  `opt_level_model.md`, `phase_dispatch_157_vs_159.md`.

### `ptxas-targets/` — target / SM-version gating tables (facts)
- `sm_id_enumeration.tsv`, `sm_version_codes.tsv` (legacy 16-bit hash-slot codes),
  `sm_target_properties.tsv`, `supported_targets.tsv`, `sass_elf_eflags.tsv` (EF_CUDA
  flag encoding), `sm_scheduling_seeds.tsv`, `gating_diagnostics.tsv`, and
  `instruction_legality.tsv` (~19k per-arch instruction/feature legality rows). See README.

### `ptxas-regalloc/` — register allocation / occupancy tables (facts)
- `register_classes.tsv` + `register_class_summary.tsv` (3 per-generation class tables,
  selector `sub_ABF590`), `register_file_config.tsv` / `register_file_limits.tsv`,
  `occupancy_constants.tsv`, `abi.tsv` (calling-convention constraints),
  `operand_regcount_matrix.tsv`, `per_arch_regalloc_binding.tsv`,
  `fp64_throughput_class.tsv`, `register_id_arrays.tsv`. Corroborated with `ptxas -v`.

### `ptxas-sched-full/` — full per-SM SASS scheduling model (facts + our tools)
- The complete latency / dependency-rule / scoreboard tables for every SM through
  sm_121: `latency_table_sm{7x,8x,10x,11x,12x}.tsv`, `dependency_rules_sm_*.tsv`
  (+ merged `dependency_rules_all.tsv`), `scoreboard_configs_sm_*.tsv`,
  `scalar_latency_oracle.tsv`, `opcode_pipeline_map.tsv`, `sm_coverage_summary.tsv`,
  `blackwell_consumer_stall_deltas.tsv`. Our code: `render_sched_full.py`,
  `extract_blackwell_consumer.py`, `measure_blackwell_deltas.py`. The full set
  spanning what `ptxas-scheduling/` covers representatively. See README.

### `ptxas-elf-output/` — device-ELF (cubin) output emitter (facts)
- What ptxas writes when assembling PTX into a cubin: `section_catalog.tsv` (CUDA
  section types), `eiattr_codes.tsv` (`.nv.info` EIATTR codes), `eicompat_codes.tsv`,
  `reloc_types.tsv`, `nvinfo_wire_groundtruth.tsv`, `header_notes_compat.tsv`,
  `per_arch_sm110_120_121.tsv`. Re-extracted from `.rodata` name-pointer tables and
  cross-checked against emitted bytes / `cuobjdump --dump-elf`. See README.

### `ptxas-mercury/` — Mercury container / capmerc / Finalizer (facts)
- The Mercury SASS container + capsule packaging + R_MERCURY relocations + FNLZR
  finalizer + section content-dedup: `capsule_section_layout.tsv`, `r_mercury_relocs.tsv`,
  `reloc_conversion_chain.tsv`, `finalizer_{functions,attributes,pipeline}.tsv`,
  `merc_section_names.tsv`, `merc_sass_map.tsv`, `nvrs_symbol_value_actions.tsv`,
  `dedup_functions.tsv`, `arch_compat_capbits.tsv` (off-target family-compat cap-bits).

### `ptxas-fp-debug/` — FP constant-fold, DWARF debug, SASS text printing (facts)
- `softfloat_{routines,callers}.tsv` (the FP constant-fold engine — see README's key
  negative finding), `dwarf_{sections,emitters}.tsv` (debug-info emission),
  `sass_{listing_fields,printer_fns}.tsv` (the `-v` resource report + self-check SASS).

### `ptxas-knobs-builtins/` — tuning knobs + builtin/opcode catalogs (facts)
- `knob_names.tsv` (1121 tuning-knob names, ROT13 where stored that way),
  `okt_descriptors.tsv` (option-knob-table descriptors), `builtins_catalog.tsv` (1080
  builtin function declarations), `builtins_wgmma_infra.tsv` (wgmma infra blocks),
  `opcode_master_canonical.tsv` (322 canonical opcodes: mnemonic, sm_gen, encoding slot,
  per-family pipeline flags).

### `ptxas-gap-closure/` — resolved ptxas internals (facts)
- `resolved_items.tsv` — one row per (item, subitem): resolution, binary evidence,
  and confidence.

## Local-only outputs (not in git — regenerate with the tools above)

These are produced by the published tools but are NVIDIA's data tables, so
they are git-ignored and not redistributed:

- `ptxas-ptx-macro-pool/`, `nvlink-ptx-macro-pool/` — the decoded printf-template
  pools (pseudo-PTX → PTX lowering recipes). nvlink's copy is byte-identical to
  ptxas's. Regenerate: `python3 tools/decode_pool.py <your ptxas>`.
- `nvdisasm-sass-isa/` — the 13 per-arch SASS ISA grammar dumps
  (`CLASS`/`FORMAT`/`OPCODES`/`ENCODING`/`CONDITIONS`/`PROPERTIES`) and the S-box.
  Aliases share a table (SM90≡SM90a, SM120≡SM121, SM86≡SM87≡SM88). Regenerate:
  `python3 tools/nvdisasm_decode.py <your nvdisasm>`.
- `cicc-tables/` — the bundled-LLVM `Intrinsics.inc` (25,063 names, all in-tree
  targets, pins cicc to LLVM 21), the Clang `Builtins` table (12,506), and cicc's
  PTX-emission mnemonic dictionary. The LLVM/Clang names are themselves public
  (Apache-2.0) and the PTX mnemonics are documented in NVIDIA's PTX ISA; we keep the
  bulk dumps off-repo regardless and cite the facts in the wiki.
- `reference/` — redplait's `denvdis` extractor and his decrypted pool dump
  (third-party; see his blog, not republished here).

## Provenance / legal

Recovered solely from analysis of binaries NVIDIA distributes freely at
developer.nvidia.com without NDA or access restriction. Reverse engineering of
publicly distributed software for research, education, and interoperability is
protected under DMCA 17 U.S.C. § 1201(f) (and *Sega v. Accolade*, *Sony v.
Connectix*) in the US and EU Directive 2009/24/EC (Arts. 5–6). No proprietary
source, trade secrets, or confidential materials were used. NVIDIA's copyrighted
data tables are studied but **not** redistributed — only our tools, the cipher
mechanism, and uncopyrightable factual data are published here.
