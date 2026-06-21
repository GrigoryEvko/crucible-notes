# SM Version Codes & Scheduler Generations

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE); file offset = VA − `0x400000`.*

ptxas maps a target architecture through three small `.rodata` tables: a **version-code**
table (the packed `sm_NN` identifier), a **supported-SM enumeration** (the list of compute
capabilities it accepts), and a **scheduler-generation seed** table (which internal scheduling
model a target uses). All three are keyed by an *internal arch index*, not the SM number directly.

## Version-code table

At VMA `0x2020620`, a `128 × u16` array. The code packs the compute capability into three fields:

```text
bits[15:12] = major × 10      bits[11:8] = minor      bits[7:0] = variant
e.g.  0x8000 → major 8, minor 0, variant 0   (sm_80)
      0x7003 → major 7, minor 0, variant 3   (the third gen-7 chip)
      0x9004 → major 9, minor 0, variant 4   (the fourth gen-9 chip)
```

The reader is `sub_60FBF0`: `version_code = word_2020620[internal_arch_id − 20]`, clamped to
index `≤ 0x65` (101) — entries at index ≥ 102 are adjacent-table bleed, not version codes. The
reader stores `word_2020620[arch_enum − 20]` opaquely (`sub_60FBF0:871`); it never extracts a
letter or decomposes the variant byte.

### The variant byte is a per-generation chip ordinal, not a suffix selector

The low byte is **a per-generation chip ordinal index** — the *N-th* chip introduced inside that
compute-major generation — **not** a selector that maps a number to a suffix letter. There is no
runtime code anywhere in ptxas that turns a variant number into an `a`/`c`/`f` character.

The ordinal run is proven by the contiguous `compute_86`/`compute_87`/`compute_88`/`compute_89`
strings at VA `0x201F4D6` / `0x201F4E1` / `0x201F4F2` / `0x201F503`, and by the profile
constructors that stamp the code into `profile+0x15c`:

| Generation | variant 0 | variant 1 | variant 2 | variant 3 | variant 4 | variant 5 |
|---|---|---|---|---|---|---|
| **gen-7** (major 8) | — | `sm_80` | `sm_86` | `sm_87` | `sm_88` | `sm_89` |
| **gen-9** (major 9) | `sm_100` | `sm_101` / `sm_110` | *(unused, 0)* | `sm_103` | `sm_120` | `sm_121` |

So **variant 2 = `sm_86`** (gen-7; the gen-9 `0x9002` slot is zero/unused), and **variant 4 =
`sm_88`** (gen-7) or **`sm_120`** (gen-9). Both are real, user-selectable `-arch` targets — not
internal-only, and never a suffix character. "variant 3" is `sm_87` (gen-7) or `sm_103` (gen-9),
never a `c` suffix. Representative constructors:

```text
0x60AC30   movl $0x7002, …(profile+0x15c)     ; sm_86  (gen-7, variant 2)
0x60AB30   movl $0x7004, …(profile+0x15c)     ; sm_88  (gen-7, variant 4)
0x608DF0   … = 0x9004                          ; sm_120 (gen-9, variant 4)
```

### Suffix letters are an orthogonal naming property

The `a` and `f` suffixes (`sm_90a`, `sm_120f`, …) are a **separate, hardcoded name-registration
property**, *orthogonal* to the variant byte. `sm_120`, `sm_120a`, and `sm_120f` all collapse to
the **same** internal code `0x9004` once the profile is built — the suffix character is discarded
after registration and never re-derived from the code. The string inventory confirms this: only
`a` (15×) and `f` (14×) suffix strings exist, all in gen-9; there is no `b`/`c`/`d`/`e`. The
apparent `sm_9f` token is a disassembler artifact of the hardcoded `sm_90a` deprecation-message
builder, not a real target. The earlier `variant 1→a, 3→c, 5→f` reading of this table was wrong:
it confused two independent mechanisms.

The raw table contents (with the variant column understood as a chip ordinal) are in
[Version-Code Table & Suffix Map](../reference/data/sm-version-codes.md).

## Supported-SM enumeration

A cleaner arch-index → SM map at VMA `0x1CE7F80`, `28 × u32`, read by `sub_442710`:
`sm_id = dword_1CE7F80[internal_arch_index − 7]` for index `7..34`. Contents (26 active +
2 NULL separators that split the datacenter batch from the consumer batch):

```text
sm_30 32 35 37 50 52 53 60 61 62 70 72 73 75 80 · [null] · 86 87 88 89 90 100 101 110 · [null] · 103 120 121
```

## Scheduler-generation seeds

At VMA `0x1D16148`, `50 × {u32 sm_id, u32 gen_code, u32 variant}`. **`gen_code` is an internal
scheduler-model generation index, not the marketing architecture name** — an earlier extraction
legend that read it as "1=Fermi … 9=Thor" was a mislabel. The binary-derived groupings:

| gen | sm_ids | (marketing) |
|---|---|---|
| 1 | 10, 11, 12, 13 | Tesla |
| 2 | 20, 21 | Fermi |
| 3 | 30, 35 | Kepler |
| 4 | 32, 37, 50, 52, 53 | late-Kepler + Maxwell (one model) |
| 5 | 60, 61, 62 | Pascal |
| 6 | 70, 72, 75, 82 | Volta + Turing (shared) |
| 7 | 80, 86, 87, 88, 89, 90 | Ampere / Ada (+ sm_90 as "A") |
| 8 | 90, 100, 101, 103, 120, 121 | Hopper + Blackwell |
| 9 | 110 | Thor |

`sm_90` appears in **both** gen 7 and gen 8 — two scheduling models selected by context, which is
the same split seen in the per-SM dependency tables (sm_90 disables 6 WGMMA/async classes that
sm_90a keeps). The seed table's own `variant` column is the chip ordinal described above (e.g.
`sm_86` variant 1, `sm_88` variant 3, `sm_120` variant 7 in the gen-8 segment), not a suffix.

The full per-index tables are in the repo at `decoded/ptxas-targets/` (`sm_version_codes.tsv`,
`sm_id_enumeration.tsv`, `sm_scheduling_seeds.tsv`, `supported_targets.tsv`).

## Cross-References

- [SM Architecture Map](./index.md) — the feature matrix per SM.
- [Instruction Legality Matrix](../reference/instruction-legality.md) — what each target may encode.
- [Version-Code Table & Suffix Map](../reference/data/sm-version-codes.md) — full table dump.
- [Extracted .rodata Tables](../reference/extracted-tables.md).
