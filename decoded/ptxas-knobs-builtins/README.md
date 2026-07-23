# ptxas knobs & builtins — decoded data

White-hat RE of NVIDIA **ptxas** (CUDA **13.0.88**, binary at `ptxas/ptxas`). Internal
knob registry, the OKT dump-schema, the canonical opcode table, and the `__cuda_*`
builtin/prototype catalog. All facts are **binary-derived**.

## Knob counts (binary-verified)

Two static initializers register the knob definition tables:

| Table | Initializer | Entries | How counted |
|---|---|---|---|
| OCG | `ctor_005` @ `0x40D860` | **1,000** | name+len+type triples; cross-checked by dump-schema sentinel `(0x1CFB580 − 0x1CE9C40)/72 = 1000` |
| DAG | `ctor_007` @ `0x421290` | **99** | name+len+type triples |
| **Total** | | **1,099** | |

The OCG count (1,000) equals the OKT dump-schema entry count exactly; the two are emitted
in lockstep from one ordered list.

## Files

| File | Contents | Notes |
|---|---|---|
| `okt_descriptors.tsv` | the `0x1CE9C40` OKT dump-schema — **1,000** entries, 9 string fields each | produced by `extract_okt_descriptors.py`; byte-faithful to the binary table. Type histogram: INT 616, NONE 139, DBL 100, BDGT 88, STR 28, FLOAT 12, IRNG 8, OPCODE_STR_LIST 4, ILIST 3, WHEN 2. Flags histogram: `0x0`:534 `0x2`:155 `0x3`:112 `0x1`:103 `0x4`:59 `0x6`:36 `0x7`:1. |
| `knob_names.tsv` | ROT13-decoded knob names harvested from the `.rodata` string ranges | **String-pool sweep, not a registration count.** The OCG range (`0x21B64C8..0x21C0D50`) over-captures by ~19 because it bleeds into adjacent non-knob helper strings (`NamedPhases`, `VERTEX_AB`, `VERTEX_A`, phase names). Use `ctor_005`/`ctor_007` (1,000 / 99) for authoritative counts, not the row totals here (1,019 / 102). |
| `opcode_master_canonical.tsv` | canonical opcode/encoding table | per-SM-gen encoding slots & pipeline flags |
| `builtins_catalog.tsv` | `__cuda_*` prototype/builtin catalog — **1,080** rows | matches the `sub_5FF700` 1,080-case prototype dispatch and `embedded_ptx_intrinsics.json` |
| `builtins_wgmma_infra.tsv` | wgmma builtin-infrastructure blocks | |

## Extractor

`extract_okt_descriptors.py <ptxas>` — walks the OKT dump-schema at `0x1CE9C40` (72-byte
stride, 9 `char*` per entry) to the `0x1CFB580` sentinel and emits the 1,000-entry TSV.
Auto-resolves `.rodata` string pointers via the ELF section table; no external library.
