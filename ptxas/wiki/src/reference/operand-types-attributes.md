# Operand-Type Signatures & Instruction Attributes

> *Addresses apply to ptxas v13.0.88 (CUDA 13.0). VA base `0x400000` (non-PIE).*

Every PTX instruction ptxas accepts is registered with three pieces of static data:
an **operand-type signature**, a parallel **datatype-code string**, and a **136-bit
attribute mask** (17 significant bytes held in a 20-byte field). Together they drive
operand type-checking and instruction-form selection. This page documents all three as
recovered from the binary — 1410 registrations across 268 unique mnemonics.

## The instruction registry

Registration runs through one shared constructor body invoked from two sites (the
mechanism and counts are covered in [PTX Parser](../pipeline/ptx-parser.md); summarized
here):

| Site | Dict offset | Path | Registrations |
|---|---|---|---|
| STANDARD | `table+2472` | `sub_46E000` → 1141 inline `sub_46BED0` calls | 1141 |
| EXTENDED | `table+2480` | 269-entry func-ptr array at `0x29FCA68` → `sub_465030` | 269 |

The per-registration argument layout, read off the call sites:

```text
reg(table, operand_type_sig, name, datatype_sig, index, _, _, attr_mask_xmm16, attr_byte16_dword)
```

The attribute mask is built byte-by-byte on the stack. The constructor (`sub_46BED0`,
`0x46bed0`) stores the 16-byte `__m128i` at the instruction record's offset **+12**
(`movups %xmm0,0xc(%rbx)`) and a contiguous extra dword — arg `a9` — at offset **+28**
(`mov %eax,0x1c(%rbx)`; `0xc + 16 = 0x1c`). That dword's **low byte is byte 16 of the
mask (bits 128–135)**; its upper three bytes are never written, so bytes 17–19 are pure
padding. The C decompiler mis-models `a9` as the `__m128i` union's `m128i_i32[0]`; the true
byte-16 value is recovered from the call-site machine code, where it is staged as
`movb $V,0x40(%rsp); mov 0x40(%rsp),%eax; mov %eax,0x10(%rsp)` (vs. `movl $0,0x10(%rsp)`
for every form whose byte 16 is zero). A small set of STANDARD entries pass the name
slot as a bare 32-bit integer token rather than a string pointer (10 instructions
exist only in this integer-keyed form); these are recorded as `#<int>`.

## Operand-type signature grammar

The signature is a left-to-right concatenation of per-operand type fields. Each field
is a **type letter** + a **width** in bits; `[a|b|c]` denotes width-alternation within
one operand (e.g. `B[32|64]`). The type letters and the widths actually observed in the
binary's data:

| Letter | Code | Type | Observed widths |
|---|---|---|---|
| F | 1 | float | F16, F32, F64 |
| H | 2 | packed f16x2 | H32, H64 |
| N | 3 | named / opaque | N32 |
| I | 4 | integer | I2, I4, I8, I16, I32 |
| B | 5 | untyped bits | B1, B4, B8, B16, B32, B64, B128 |
| P | 6 | predicate | (1 bit) |
| O | 7 | opaque | (zero size) |
| E | 8 | bf16 (exp8 float) | E16, E32 |
| T | 9 | tf32 | T32 |
| Q | 10 | fp8 (e4m3 / e5m2) | Q8, Q16, Q32 |
| R | 11 | sub-byte microscaling float (e.g. fp4 e2m1) | R4, R8, R16 |

`E`, `T`, `Q`, and `R` (bf16 / tf32 / fp8 / fp4-microscaling) are **binary-discovered
newer-ISA additions** — they have no counterpart in older type-string documentation.
Example block-scaled MMA signature: `F32R4R4F32Q8` (an fp4×fp4 accumulate into fp32
with an fp8 scale operand).

A second per-instruction **datatype string** (e.g. `"000U"`, `"hhhhdC"`) is parsed into
per-operand datatype codes by a separate switch:

```text
x→1  u→2  U→3  s→4  f→5  h→6  l→7  b→8  c→9  d→10  e→11
i→12 C→13 D→14 P→15 Q→16 M→17 S→18 T→19 A→20 V→21 L→22
```

Digits in the datatype string denote a literal size; a leading digit (e.g. the `0`s in
`"000U"`) selects which entry of the instruction's type list each operand follows.

## The 136-bit attribute mask

The mask is a flat little-endian bitfield: **17 significant bytes (bits 0–135) held in a
20-byte field** (bytes 17–19 are always-zero padding). Bytes 0–15 are the `__m128i` at
record offset +12; byte 16 (bits 128–135) is the low byte of the extra dword at offset
+28. **Bit numbering is logical LSB-first**: bit 0 is the least-significant bit of byte 0,
bit 8 is the LSB of byte 1, … bit 127 is the MSB of byte 15, and bits 128–135 live in
byte 16. (Reading the low 16 bytes as a single big-endian integer flips the low half —
bit `b` then appears at `8·(15 − b÷8) + (b mod 8)` for `b < 128`.)

Across all 1410 forms, **114 of the 136 bits are used**: 110 in the low 128 (byte 0–15)
plus 4 in byte 16 (bits 128–131). The masks themselves are exact binary facts; the
*names* below were recovered by correlating which instructions set which bits against PTX
semantics (generic PTX vocabulary), then cross-checked by two independent solvers that
agreed with zero disagreements. Confidence is annotated.

### Boolean attributes (54 bits, high confidence)

Each maps 1:1 to a single bit. Several were verified directly against semantics — e.g.
`copysign` sets only `RESULT`; `div.full` = `RESULT|FTZ` (bits 5,9); `rsqrt` =
`RESULT|APRX|FTZ` (5,7,9).

| Group | Bit:name |
|---|---|
| Result / rounding / precision | 5 RESULT · 6 RESULTP · 7 APRX · 8 RELU · 9 FTZ · 10 NOFTZ · 11 SAT · 12 SATF · 22 VSAT · 25 ROUNDF · 26 ROUNDI |
| Arithmetic / operand form | 0 BOP · 2 CMP · 23 CC · 24 SHAMT · 27 SIGNED · 32 DOUBLERES · 33 LARG · 34 SREGARG · 107 TRANSA · 109 NEGB · 116 OOB |
| Control flow | 28 FLOW · 29 BRANCH |
| Texture / surface | 46 TEXADDR · 47 TEXMOD · 63 COMPMOD · 64 SURFQ · 65 SMPLQ · 66 TEXQ |
| Atomic / reduction | 68 ATOMOPF · 69 ATOMOPI · 70 ATOMOPB · 71 ARITHOP · 72 CAS · 73 CLAMP |
| Warp / collective | 37 TESTP · 67 VOTE · 76 PRMT · 77 SHFL |
| Memory / cache | 38 CACHEOP · 43 EVICTPRIORITY |
| Barrier / sync | 82 NOINC · 83 NOCOMPLETE · 84 SHAREDSCOPE · 85 BAR |
| Tensor / MMA | 93 TRANS · 94 NUM · 95 SEQ · 96 GROUP · 98 EXPAND · 99 THREADGROUP · 100 SPARSITY · 101 SPFORMAT |

### Multi-bit field regions (25 bits, names known, sub-split ambiguous)

These bits sit in enum-like regions where several attributes share the span, so the
exact bit↔name split is not separable from the masks alone:

- **Memory / cache / scope** — bits 35, 36, 39, 40, 41, 42, 44, 60, 115 (MEMSPACE(S), ORDER, SCOPE, PROXYKIND, LEVEL, PREFETCHSIZE, CACHEHINT, DESC).
- **Tensor descriptor** — bits 48–51, 54–56 (MULTICAST, PACKEDOFF, MBARRIER, IM2COL, TENSORDIM).
- **Barrier / sync** — bits 81, 86 (ALIGN, SYNC).
- **MMA / tcgen05** — bits 90, 91, 92, 108, 110, 111, 117 (SHAPE, IGNOREC, PROXYKIND, VECTORIZABLE).

### Ambiguous and unnamed

- **Ambiguous (5 bits)** — {74,75} = SHR|VMAD and {105,106,114} = ABS|NANMODE|XORSIGN: each group always co-occurs in the matched forms, so the masks cannot separate them.
- **Unnamed newer-ISA (19 bits)** — 3, 4, 13, 14, 15, 19, 52, 78, 87, 97, 112, 119, 121–127: set only by wgmma / tcgen05 / `cp.async.bulk` / multimem / clusterlaunchcontrol forms that postdate older naming conventions.
- **Used-but-unlabeled (7 bits)** — 30, 45, 53, 59, 104, 118, 120: too sparse for a clean correlation.

### Byte 16 — bits 128–135 (the 17th byte)

Bits 128–131 are the only bits beyond 127 that are ever set, and only by 8 STANDARD
forms (the newest Blackwell pseudo-ops). Bits 132–135 and bytes 17–19 are never set.
Bit 131 is observably read at a lowering site as `*(_BYTE *)(desc + 28) & 8` (an
early-out predicate), confirming byte 16 is a genuine continuation of the attribute mask.

| Bit | Hex | Forms (count) | Set by |
|---|---|---|---|
| 128 | `0x01` | 1 | `tcgen05.wait` |
| 129 | `0x02` | 5 | `_tcgen05.guardrails.are_columns_allocated` · `_tcgen05.guardrails.in_physical_bounds` · `_tcgen05.guardrails.datapath_alignment` |
| 130 | `0x04` | 3 | `_tcgen05.guardrails.are_columns_allocated` · `_tcgen05.guardrails.in_physical_bounds` · `_tcgen05.guardrails.datapath_alignment` |
| 131 | `0x08` | 2 | `cp.async.bulk` |

Bits 129–130 behave as a small enum-like field shared across the `tcgen05.guardrails.*`
pseudo-ops (values `0x02` and `0x06` observed); the exact sub-split is not separable from
the masks alone.

## Reproduce

The full 1410-row instruction table and the bit legend are in the repo:
`decoded/ptxas-instr-defs/instruction_table.tsv` (index, name, operand_type_signature,
datatype_sig, attribute_mask_hex — now 34 hex chars / 17 bytes, STD/EXT, name_kind) and
`attribute_bits.tsv` (bits 0–135), regenerated by `extract_instruction_table.py` (parses
the registration call arguments for bytes 0–15 and injects byte 16 from the call-site
machine code) and `solve_attribute_bits.py` (the correlation solver). The full-width OR
of all masks is in `union_mask.txt` (`fdffc87fffffff99ff7ffefc3ffffdff0f`).

## Cross-References

- [PTX Parser](../pipeline/ptx-parser.md) — the registration mechanism and the two dictionaries.
- [Pseudo-Instruction Expansion](../intrinsics/pseudo-instruction-macros.md) — how typed forms lower via the macro pool.
- [PTX Instruction Table](ptx-instructions.md) · [Extracted .rodata Tables](extracted-tables.md).
