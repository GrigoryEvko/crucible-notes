# SM Version Codes & Scheduler Generations

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE); file offset = VA − `0x400000`.*

ptxas maps a target architecture through three small `.rodata` tables: a **version-code**
table (the packed `sm_NN[suffix]` identifier), a **supported-SM enumeration** (the list
of compute capabilities it accepts), and a **scheduler-generation seed** table (which
internal scheduling model a target uses). All three are keyed by an *internal arch index*,
not the SM number directly.

## Version-code table

At VMA `0x2020620`, a `128 × u16` array. The code packs the compute capability:

```text
bits[15:12] = major × 10      bits[11:8] = minor      bits[7:0] = variant
e.g.  0x9004 → sm_90 variant 4      0x9001 → sm_90a      0x5000 → sm_50
```

The reader is `sub_60FBF0`: `version_code = word_2020620[internal_arch_id − 20]`, clamped
to index `≤ 0x65` (101) — entries at index ≥ 102 are adjacent-table bleed, not version
codes. Observed variant→suffix mapping: `1→a`, `3→c`, `5→f` (variants 2/4 are
internal/unlabeled). The table is **variant-only for some arches** — it stores
`0x7001..0x7005` (sm_70 suffixed forms) and the sm_90 variants but no bare `0x7000`, i.e.
it emits the separate-compilation `a`/`c`/`f` tags rather than every base SM.

## Supported-SM enumeration

A cleaner arch-index → SM map at VMA `0x1CE7F80`, `28 × u32`, read by `sub_442710`:
`sm_id = dword_1CE7F80[internal_arch_index − 7]` for index `7..34`. Contents (26 active +
2 NULL separators that split datacenter from consumer batches):

```text
sm_30 32 35 37 50 52 53 60 61 62 70 72 73 75 80 · [null] · 86 87 88 89 90 100 101 110 · [null] · 103 120 121
```

## Scheduler-generation seeds

At VMA `0x1D16148`, `50 × {u32 sm_id, u32 gen_code, u32 variant}`. **`gen_code` is an
internal scheduler-model generation index, not the marketing architecture name** — an
earlier extraction legend that read it as "1=Fermi … 9=Thor" was a mislabel. The
binary-derived groupings:

| gen | sm_ids | (marketing) |
|---|---|---|
| 1 | 10,11,12,13 | Tesla |
| 2 | 20,21 | Fermi |
| 3 | 30,35 | Kepler |
| 4 | 32,37,50,52,53 | late-Kepler + Maxwell (one model) |
| 5 | 60,61,62 | Pascal |
| 6 | 70,72,75,82 | Volta + Turing (shared) |
| 7 | 80,86,87,88,89,90 | Ampere / Ada (+ sm_90) |
| 8 | 90,100,101,103,120,121 | Hopper + Blackwell |
| 9 | 110 | Thor |

`sm_90` appears in **both** gen 7 and gen 8 — two scheduling models selected by context,
which is the same split seen in the per-SM dependency tables (sm_90 disables 6 WGMMA/async
classes that sm_90a keeps).

The full per-index tables are in the repo at `decoded/ptxas-targets/` (`sm_version_codes.tsv`,
`sm_id_enumeration.tsv`, `sm_scheduling_seeds.tsv`, `supported_targets.tsv`).

## Cross-References

- [SM Architecture Map](./index.md) — the feature matrix per SM.
- [Instruction Legality Matrix](../reference/instruction-legality.md) — what each target may encode.
- [Extracted .rodata Tables](../reference/extracted-tables.md).
