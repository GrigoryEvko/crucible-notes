# SM Version-Code Table & Suffix Map

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

The raw `128 × u16` version-code table at VMA `0x2020620`, read by `sub_60FBF0` as
`word_2020620[internal_arch_id − 20]`. The code packs `bits[15:12]=major×10`, `bits[11:8]=minor`,
`bits[7:0]=variant`, where **variant is a per-generation chip ordinal index, not a suffix-letter
selector** (see [SM Version Codes](../../targets/version-codes.md)). Only populated indices are
shown; index ≥ 102 is adjacent-table bleed.

The `name (legacy label)` column is the spelling the original string-inventory pass attached to
each index. The `major.minor` / `variant` decomposition is authoritative; the legacy labels that
end in a letter (`…a`, `…c`, `…f`) are name-registry spellings — the variant byte itself is just
the chip ordinal (variant 2 / 4 are real numbered chips, never a `b`/`d` suffix). `·N` marks an
index whose legacy pass left the suffix unresolved.

| index | code (hex) | code (dec) | major.minor | variant | name (legacy label) |
|---|---|---|---|---|---|
| 0 | 0x2000 | 8192 | 2.0 | 0 | `sm_20` |
| 1 | 0x2001 | 8193 | 2.0 | 1 | `sm_20a` |
| 50 | 0x5000 | 20480 | 5.0 | 0 | `sm_50` |
| 52 | 0x5001 | 20481 | 5.0 | 1 | `sm_50a` |
| 53 | 0x6000 | 24576 | 6.0 | 0 | `sm_60` |
| 55 | 0x6001 | 24577 | 6.0 | 1 | `sm_60a` |
| 62 | 0x7001 | 28673 | 7.0 | 1 | `sm_70a` |
| 66 | 0x7002 | 28674 | 7.0 | 2 | `sm_70·2` |
| 67 | 0x7003 | 28675 | 7.0 | 3 | `sm_70c` |
| 68 | 0x7004 | 28676 | 7.0 | 4 | `sm_70·4` |
| 69 | 0x7005 | 28677 | 7.0 | 5 | `sm_70f` |
| 70 | 0x8000 | 32768 | 8.0 | 0 | `sm_80` |
| 80 | 0x9000 | 36864 | 9.0 | 0 | `sm_90` |
| 81 | 0x9001 | 36865 | 9.0 | 1 | `sm_90a` |
| 83 | 0x9003 | 36867 | 9.0 | 3 | `sm_90c` |
| 90 | 0x9001 | 36865 | 9.0 | 1 | `sm_90a` |
| 100 | 0x9004 | 36868 | 9.0 | 4 | `sm_90·4` |
| 101 | 0x9005 | 36869 | 9.0 | 5 | `sm_90f` |

## Suffix letters (orthogonal to variant)

Suffix letters are a name-registration property collapsed away after the profile is
built — `sm_120`, `sm_120a`, `sm_120f` all share internal code `0x9004`. Only `a` (15
strings) and `f` (14 strings) exist; there is no `b`/`c`/`d`/`e`. A variant byte of 3 is the
third chip of a generation (`sm_87` / `sm_103`), never a `c` suffix.
