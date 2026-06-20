# fiss Slotfill — Narrow Formats N0/N1/N2

> **Completes the slotfill grid.** This is the third and final slotfill sibling.
> Together with [FLIX Formats F0–F3](./fiss-slotfill-f0-f3.md) and
> [FLIX Formats F4/F6/F7/F11](./fiss-slotfill-f4-f11.md) it covers **all 11 issue
> formats**. Where the F-pages decode the *wide* 16-byte / 128-bit bundles, this
> page decodes the three **narrow 8-byte / 64-bit** FLIX formats — **N0, N1, N2** —
> the ones whose entire operand grammar lives in the lower 64 bits of the bundle.
> The decoder shape is identical to the wide formats; the *constraint* is new.

`libfiss-base.so` is the **value oracle** of the GPSIMD ISS (see
[The fiss Datapath Oracle](./fiss-datapath-oracle.md)). Inside it the
`slotfill__*` functions form the **operand-decode layer**: each one takes a raw
FLIX bundle word and extracts one slot's operand fields — register *indices*,
sign-extended *immediates*, and Mul/compare *sub-opcode selectors* — into a fixed
set of context latches. They are the functional-ISS inverse of the `libisa-core`
encode thunks ([Table / Codec Schema](../isa/core/libisa-table-schema.md)): the
slotfill *decodes* what the encode thunk *built*. Of the **12,569** `slotfill__`
exports in the binary, **1,790** belong to N0/N1/N2.

All facts below are derived from static analysis of the two shipped binaries
only:

```
FISS = extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so
       ELF64 LSB shared object, x86-64, SYSV, NOT stripped
       12,330,016 bytes (12.3 MB); 20,379 dynamic exports
ISA  = extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libisa-core.so
       ELF64 LSB shared object, x86-64, SYSV, NOT stripped; full .symtab
       9,690,712 bytes (9.7 MB)
```

Tooling: `nm -D`, `readelf -SW`, `objdump -d/-s`, plus a small x86 field-probe
emulator (toggle one encoded bundle bit, observe which decoded field moves).
Every claim is tagged **confidence** × **provenance**: `OBSERVED` = read directly
off the binary, `INFERRED` = strong deduction from observed evidence, `CARRIED` =
taken from a prior report and re-checked here against the binary.

> **NOTE — section offsets.** `.text` and `.rodata` are mapped **VMA == file
> offset** (`readelf -SW`: `.text` VMA `0x190430` / off `0x190430`; `.rodata` VMA
> `0x88ff00` / off `0x88ff00`), so every `objdump --start-address` below is also a
> raw file offset. `.data` is **not** identity-mapped (VMA `0xc8eb68` / off
> `0xa8eb68`, delta `0x200000`); no `.data`-resident struct is read in this page,
> so the delta never bites here — but confirm per-section before `xxd`-ing any
> `.data` address.

---

## 1. Executive finding

The N0/N1/N2 slotfill exports are the **same pure bitfield decoder** proven for
F0–F11, re-anchored to each narrow format's slot bit-offsets and **clamped to the
lower 64 bits** (word0 + word1) of the bundle. Given `%rdi` → a per-op context
whose first 8 bytes hold the raw 64-bit narrow bundle as `word0..word1`, each
slotfill `shr/and/or`-masks the slot's operand sub-fields and writes register
*indices*, sign-extended *immediates*, or *sub-opcode* selectors into fixed
operand latches. **No arithmetic on operand values.** `word2`/`word3` (bundle
offsets `0x8`/`0xc`) are **never read** — the defining narrow trait, verified for
every body probed. `[HIGH · OBSERVED]`

The slice carries **exactly 1,790** slotfill exports, reconciled to the unit per
`(format × slot × unit)` cell (§2). The three formats map to:

| Format | Bundle | Slots | Slot roster | Profile |
|---|---|---|---|---|
| **N0** | 64-bit | 4 | `LdSt @4` · `None` · `None` · `ALU @16` | ALU-heavy (483 ALU ops) |
| **N1** | 64-bit | 3 | `LdSt @4` · `None` · `Mul @16` | Mul-heavy (381 Mul ops) |
| **N2** | 64-bit | 2 | `LdSt @4` · `Ld @16` | LdSt-heavy (360 LdSt, no ALU/Mul) |

The three `None` slots (`N0_S1`, `N0_S2`, `N1_S1`) are pure **NOP placeholders**:
their slotfill body is literally `xor %eax,%eax ; ret` — no operand decode.
`[HIGH · OBSERVED]`

Two decode families coexist (identical to the wide formats) `[HIGH · OBSERVED]`:

* **(a) Scalar base-Xtensa RRR/RRI8 AR form** — 3 index bits + 1 high
  window/bank-select bit (XOR-inverted, ×8) + window-base (`ctx+0x30` ×8), masked
  `& 0x3f` ⇒ 64 physical AR. Latches `dst(r)→0x24`, `src1(s)→0x48`,
  `src2/imm(t)→0x50`. Immediates **sign-extended** (`shl;sar`), registers
  **zero-extended** (`shr;and`).
* **(b) Vector IVP form** — 5-bit IVP vector-register operands (vr/vs/vt) plus
  Mul sub-op and compare lane-mask latches. Two latch sets:
  * **ALU canonical:** `vt→0x94`, `vs→0x50`, `vr→0xd8` (2-lane `_S`/`_H` thunks)
  * **Mul / alt:** `vt→0x28`, `vs→0x6c`, `vr→0xb0` (full-width `IVP_*NX16`),
    plus the 2-bit Mul sub-op at `0x28` with vreg fields `0xec`/`0x130`, and the
    vbool/lane-mask compare latches `0x28`/`0x34`/`0x78`.

The genuine **narrow-format-specific** findings of this slice:

* **64-bit clamp.** Every N-format slotfill reads only `word0`/`word1`. No body
  reads bundle `0x8`/`0x10` (`word2`/`word3`).
* **Immediate scatter differs per format.** The RRI8 imm8 (ADDI / S32I store
  offset) is packed *around* each format's live slots, so its sign bit lands at a
  **different bundle bit** in each: bit **50** (N0), bit **38** (N1 and N2); the
  one mid bit at bit **21** (N0/N1) vs bit **25** (N2). All three sign-extend
  identically to `-128..127`.
* **Narrow Ld dst packing reused verbatim.** `N2_S1_Ld` reuses the
  `F4_S1_Ld`/`F3_S1` narrow split-dst `{26}|{29,30}|bank{31}` decoder
  **byte-identically**.
* **`N0_S3_ALU` anchors at bundle bit 16** (not the 31/36 of the wide ALU slots):
  the narrow bundle has no room above 64 bits, so the vector operand group sits
  low.

> **CORRECTION — "byte-load support" is field granularity, not a byte-load
> instruction.** The task scope names *sub-dword byte-load support*. There is
> **no** `movzbl`/`movzwl`/`movb`/`movw` instruction anywhere in any N-format
> body (0 across all 6 sampled cells, §3.5). The narrow decode loads each bundle
> half as a **single 32-bit dword** (`mov (%rdi)`, `mov 0x4(%rdi)`) and then
> extracts operands **narrower than a dword** — and immediates that **straddle**
> the word0/word1 boundary — purely with `shr`/`and`/`or` over those two dwords.
> "Byte/half granularity" lives in the **`and`-mask widths** (e.g. `and $0x7` =
> 3-bit AR index, `and $0x1f` = 5-bit vreg, `and $0xffffff80` = 8-bit signed
> imm), not in the load width. `[HIGH · OBSERVED]`

---

## 2. Slotfill census — reconciled to the unit

`nm -D --defined-only FISS | rg -c 'slotfill__'` = **12,569**; of those
`rg -c 'slotfill__(N0|N1|N2)__'` = **1,790**. `[HIGH · OBSERVED]`

| Format | nm count | Slots |
|---|---|---|
| N0 | **652** | 4 |
| N1 | **558** | 3 |
| N2 | **580** | 2 |
| **Total** | **1,790** | — |

Per `(format × slot × unit)` cell — `rg -o '<FMT>_S[0-9]_<UNIT>_slot[0-9]' | sort | uniq -c`:

| Cell | Count | | Cell | Count |
|---|---:|---|---|---:|
| `N0_S0_LdSt_slot0` | 167 | | `N1_S0_LdSt_slot0` | 176 |
| `N0_S1_None_slot1` | 1 | | `N1_S1_None_slot1` | 1 |
| `N0_S2_None_slot2` | 1 | | `N1_S2_Mul_slot2` | 381 |
| `N0_S3_ALU_slot3` | 483 | | **N1 sum** | **558** ✓ |
| **N0 sum** | **652** ✓ | | `N2_S0_LdSt_slot0` | 360 |
| | | | `N2_S1_Ld_slot1` | 220 |
| | | | **N2 sum** | **580** ✓ |

All 9 cells enumerated; column sums equal the per-format `nm` counts exactly;
grand total = 1,790. Slot counts (N0=4 / N1=3 / N2=2) match the FLIX format table
in [FLIX Encoding](../isa/core/flix-encoding.md). `[HIGH · OBSERVED]`

The 3 `None` cells (count 1 each) are the single NOP filler per slot:

| Symbol | Address | Body |
|---|---|---|
| `slotfill__N0__N0_S1_None_slot1__NOP` | `0x388020` | `31 c0` `xor %eax,%eax` ; `c3` `ret` |
| `slotfill__N0__N0_S2_None_slot2__NOP` | `0x388030` | `xor %eax,%eax ; ret` |
| `slotfill__N1__N1_S1_None_slot1__NOP` | `0x73d0b0` | `xor %eax,%eax ; ret` |

`[HIGH · OBSERVED]` These carry **no** operand decode — they are the
narrow-bundle reserved positions (FLIX slot widths 1/1/3 bits). The slotfill just
returns success (`eax = 0`).

Per-cell scalar/vector composition (`rg` over the `IVP_` prefix in symbols)
`[HIGH · OBSERVED]`:

| Cell | Total | IVP (vector) | scalar/base | Notes |
|---|---:|---:|---:|---|
| `N0_S0_LdSt` | 167 | 112 | 55 | RRR/RRI8 + IVP gather/cmp |
| `N0_S3_ALU` | 483 | 397 | 86 | the busy ALU cell (`alu@16`) |
| `N1_S0_LdSt` | 176 | 111 | 65 | |
| `N1_S2_Mul` | 381 | 300 | 81 | `IVP_MUL2NX8`/`MULNX16` + `ADD_S` |
| `N2_S0_LdSt` | 360 | 238 | 122 | RRR/RRI8 + IVP gather/cmp |
| `N2_S1_Ld` | 220 | 168 | 52 | narrow Ld `@16` |

Each populated cell hosts **both** scalar AR forms and IVP vector forms; the
field math within a `(form, slot)` is identical across mnemonics (§6).

---

## 3. The slotfill ABI (narrow-bundle variant)

### 3.1 Calling convention and context layout

Single argument `%rdi` → context; returns `0` in `eax`. No host call, no stack
frame — verified across every N0/N1/N2 body. `[HIGH · OBSERVED]`

| Offset | Direction | Meaning |
|---|---|---|
| `+0x00` | **read** | `word0` — bundle bits `[31:0]` |
| `+0x04` | **read** | `word1` — bundle bits `[63:32]` |
| `+0x08` / `+0x0c` | *(never read)* | `word2`/`word3` — **narrow trait** |
| `+0x2c` | read | windowed-mode predicate (`test;je` selects the AR window-rotation path) |
| `+0x30` | read | window-base value (`shl $3` ⇒ ×8, added into each AR index, then `& 0x3f`) |
| `+0x24` | **write** | scalar `dst` AR index (r) |
| `+0x48` | **write** | scalar `src1` AR index (s) |
| `+0x50` | **write** | scalar `src2` AR index (t) **or** sign-extended immediate / IVP-ALU `vs` |
| `+0x94` | write | IVP-ALU canonical `vt` |
| `+0xd8` | write | IVP-ALU canonical `vr` |
| `+0x28` | write | IVP Mul/alt `vt` **and** 2-bit Mul sub-op selector **and** compare latch |
| `+0x6c` | write | IVP Mul/alt `vs` |
| `+0xb0` | write | IVP Mul/alt `vr` |
| `+0xac` | write | unary `_S`/`_H` thunk dest/guard latch |
| `+0xec` / `+0x130` | write | MUL2NX8-class vreg fields (multiplicand / 3rd reg) |
| `+0x34` / `+0x78` | write | vbool / lane-mask compare fields |

`[HIGH · OBSERVED]` The latch set is **identical** to F0–F11 — only the bundle
bit-positions the indices are gathered *from* differ (narrow scatter within
`[0:63]`). The operand-VALUE blocks (16-dword / 512-bit NX16) at each latch+4 are
written by `regload`, **not** by slotfill (§5).

> **GOTCHA — `word0` reads have no displacement.** `mov (%rdi),%eax` (offset 0)
> renders without a displacement in `objdump`, so a naive `rg 'mov .*0x.*\(%rdi\)'`
> will **miss** the word0 load and make a body look like it reads only one word.
> Always grep for `mov\s+\(%rdi\)` as well. (e.g. `N0_S0` ADD reads `(%rdi)` at
> `0x38170b` and the ctx fields `0x2c`/`0x30`.) `[HIGH · OBSERVED]`

### 3.2 Window-rotation arithmetic (scalar AR — 64 physical registers)

Recovered from `N0_S0_LdSt` ADD `@0x381700` `[HIGH · OBSERVED]`:

```
0x381700  mov  0x2c(%rdi),%eax     ; windowed-mode predicate
0x381703  mov  0x30(%rdi),%edx     ; window-base
0x381706  shl  $0x3,%edx           ; window-base ×8
0x381709  test %eax,%eax
0x38170b  mov  (%rdi),%eax         ; <-- word0 (no displacement!)
0x38170f  je   0x381788            ; non-windowed path (both agree on bits)
0x381711  shr  $0xf,%esi           ; bank bit = word0 bit 15
0x381714  xor  $0x1,%esi           ; XOR-inverted
0x381717  and  $0x1,%esi
0x38171a  lea  0x0(,%rsi,8),%ecx   ; bank ×8
0x381723  shr  $0xc,%esi           ; dst low 3 bits = word0 bits {12,13,14}
0x381726  and  $0x7,%esi
0x381729  or   %ecx,%esi           ; index = {12,13,14} | (bank<<3)
0x381730  add  %edx,%esi           ; + window-base×8
0x381735  and  $0x3f,%esi          ; mask to 6 bits  => 64 physical AR
```

So `AR_index = (word>>HIGH & 1 XOR 1)·8 + (word>>LOW & 7) + window_base·8`, masked
`& 0x3f`. Emulated truth table:

| bundle bit 15 | window-base | `0x24` (dst) |
|---|---|---|
| CLEAR | 0 | 8 (XOR-inverted bank adds 8) |
| SET | 0 | 0 |
| reg field = 0 | 2 | 8 + 16 = 24 |

⇒ exactly the 64-physical-AR / 4-bit-field-plus-window scheme of F0–F11.
`[HIGH · OBSERVED]`

### 3.3 Scalar RRR cells — per-format byte-anchored decode

`dst(r)→0x24`, `src1(s)→0x48`, `src2(t)→0x50`. Bit lists are
`{3 index bits + 1 high bank bit}`; the bank bit is XOR-inverted, ×8, window-base
added, masked `& 0x3f`. Emulated both windowed and non-windowed; paths agree.

| Cell | Address | rep | slotoff | r/dst `0x24` | s/src1 `0x48` | t/src2 `0x50` | Conf |
|---|---|---|---|---|---|---|---|
| `N0_S0_LdSt` | `0x381700` | ADD | 4 | `{12,13,14,+15}` | `{8,9,10,+11}` | `{4,5,6,+7}` | HIGH · OBSERVED |
| `N1_S0_LdSt` | `0x7362b0` | ADD | 4 | `{12,13,14,+15}` | `{8,9,10,+11}` | `{4,5,6,+7}` | HIGH · OBSERVED |
| `N2_S0_LdSt` | `0x3fb9b0` | ADD | 4 | `{12,13,14,+15}` | `{8,9,10,+11}` | `{4,5,6,+7}` | HIGH · OBSERVED |
| `N2_S1_Ld` | `0x37a5c0` | ADD | 16 | `{26}\|{29,30}\|+31` | `{16,17,18,+19}` | `{20,21,22,+23}` | HIGH · OBSERVED |

> **NOTE — one decoder, re-anchored, not three copies.** The three `S0` ADD
> bodies (`0x381700`, `0x7362b0`, `0x3fb9b0`) are **byte-identical** (raw byte
> column equal over the full body; the only `objdump`-rendered difference is the
> PC-relative `je` *label*, whose `74 77` rel8 displacement is the same +0x88 in
> all three). They are byte-identical to `slotfill__x24__Inst_slot0__ADD` (the
> base-ISA scalar decoder at `0x296320`) and to `F0_S0_LdSt` ADD — one decoder
> re-anchored at slot bit-offset 4. `[HIGH · OBSERVED]`

The **narrow Ld** dst is *split-packed*. `N2_S1_Ld` ADD `@0x37a5c0`
`[HIGH · OBSERVED]`:

```
0x37a5c0  shr  $0x1c,%edx ; and $0x6      ; dst bits {29,30}
          shr  $0x1a,%esi ; and $0x1      ; dst bit  {26}
          shr  $0x1f ; not ; shl $0x3     ; bank bit {31}, XOR-inverted, ×8
          or ; and $0x3f                  ; 6-bit AR
          mov  %edx,0x24(%rdi)            ; -> dst latch
```

`N2_S1_Ld` ADD is **byte-identical** to `slotfill__F4__F4_S1_Ld_slot1__ADD`
(`0x2a5cf0`) — the same narrow split-dst decoder the F4 page documents. (`S0`
offset 4 / Ld slot offset 16 match the FLIX slot table.) `[HIGH · OBSERVED]`

### 3.4 Immediate operand — the format-specific scatter

RRI8 form (ADDI / S32I store-offset): `dst(r)→0x24`, `src(s)→0x48`, **IMM→0x50**.
The imm8 is **sign-extended** (`shl;sar`) and **scattered differently per narrow
format** — the genuine narrow finding.

| Cell | Address | imm bits → `0x50` | sign bit | mid bit | imm shift |
|---|---|---|---|---|---|
| `N0_S0` ADDI | `0x3817d0` | `{12,13,14,15}∪{21}∪{33,34}∪{50}` | bit **50** | bit 21 | `shl $0xd` |
| `N1_S0` ADDI | `0x736380` | `{12,13,14,15}∪{21}∪{33,34}∪{38}` | bit **38** | bit 21 | `shl $0x19` |
| `N2_S0` ADDI | `0x3fba80` | `{12,13,14,15}∪{25}∪{33,34}∪{38}` | bit **38** | bit 25 | `shl $0x19` |
| `N2_S0` S32I | `0x3fca60` | identical scatter to N2 ADDI (store offset reuses the RRI8 decoder) | bit 38 | bit 25 | `shl $0x19` |

`[HIGH · OBSERVED]` The sign-extension is uniform: each body assembles the 8 imm
bits into the top of a 32-bit register, then `shl $0xN ; sar $0x18` floods the
sign. Disassembly excerpt, N0 ADDI:

```
0x3817d0  ... shl $0xd,%edx          ; N0: shift imm field up by 13
          ... sar $0x18,%edx         ; arithmetic right 24 -> floods sign
          ... and $0xffffff80,%edx   ; 8-bit signed mask (-128..127)
```

N1 and N2 use `shl $0x19` (25) instead of `shl $0xd` (13) — same `sar $0x18`,
same `-128..127` result. Single-bit probe: setting **only** the sign bit yields
`0x50 = 0xFFFFFF80` (= -128); each low bit yields 1,2,4,8,16,32,64 — a clean
8-bit signed field. `[HIGH · OBSERVED]`

> **QUIRK — the imm straddles word0/word1.** Because N1/N2 push the imm high bits
> into bundle bits 33/34/38 (all in `word1`), the ADDI body loads **both**
> `(%rdi)` (word0) and `0x4(%rdi)` (word1) and `or`s their masked fragments — the
> only narrow operand that crosses the 32-bit word boundary. The low nibble
> `{12,13,14,15}` and bits `{33,34}` are **shared** across all three formats;
> only the sign/mid bits move. Extension mode is the operand-kind discriminator:
> **REG = zero-extend** (`shr;and`), **IMM = sign-extend** (`shl;sar`).
> `[HIGH · OBSERVED]`

### 3.5 Vector IVP cells

**Full-width form (`IVP_*NX16`; Mul/alt latch set):** `vt→0x28`, `vs→0x6c`,
`vr→0xb0`.

| Cell | Address | rep | vt `0x28` | vs `0x6c` | vr `0xb0` | Conf |
|---|---|---|---|---|---|---|
| `N0_S3_ALU` | `0x389270` | `IVP_ADDNX16` | `{22,23,25,26,28}` | `{16,17,18,19,20}` | `{29,30,31,32,35}` | HIGH · OBSERVED |

Disassembly excerpt (`0x389270`) confirms the latch offsets and the `vs` field at
the slot anchor:

```
0x389270  ... and $... ; mov %ecx,0x28(%rdi)        ; vt
          ... shr $0x10,%ecx ; and $0x1f            ; vs = word0 bits {16..20}
          ... mov %ecx,0x6c(%rdi)                   ; vs (5-bit, slot anchor)
          ... mov %eax,0xb0(%rdi)                   ; vr
```

`N0_S3_ALU` is anchored at bundle bit 16 (the narrow ALU slot offset): the `vs`
field `{16-20}` sits **exactly** at the slot offset; `vt`/`vr` scatter above it
into `word1`. Reads word0+word1 only. Field math is uniform across the ALU cell —
`IVP_MAXNX16` and `IVP_MINNX16` bodies are **byte-identical** to `IVP_ADDNX16`
(operand decode reused per cell). The cell also hosts 2-reg softfloat `_S`/`_H`
thunks (e.g. `ADDEXP_S @0x388520`): `vt→0x28` plus a dest/guard latch `0xac` —
the unary scalar-of-vector form writing one source + one dest. `[HIGH · OBSERVED]`

**3-register ALU-canonical form (`ADD_S` in the Mul slot):** `vt→0x94`,
`vs→0x50`, `vr→0xd8`.

| Cell | Address | rep | vt `0x94` | vs `0x50` | vr `0xd8` | Conf |
|---|---|---|---|---|---|---|
| `N1_S2_Mul` | `0x73f130` | `ADD_S` | `{16,17,18,19,20}` | `{41,42,43,44,45}` | `{22,23,35,36,37}` | HIGH · OBSERVED |

The N1 Mul slot carries **both** families: the 2-lane canonical `ADD_S`
(`0x94`/`0x50`/`0xd8`) and the full-width `MUL2NX8`/`MULNX16`
(`0x28`/`0xec`/`0x130`, below). Both read word0+word1 only. The vr field
`{22,23,35,36,37}` is **shared** between `ADD_S` (`0xd8`) and `MUL2NX8` (`0x130`)
— the Mul slot's 3rd-reg field. `[HIGH · OBSERVED]`

**Mul-slot MUL2NX8-class form (2-bit sub-op + two vregs):**

| Cell | Address | rep | subop `0x28` | vfield `0xec` | vr `0x130` | Conf |
|---|---|---|---|---|---|---|
| `N1_S2_Mul` | `0x3f6d50` | `IVP_MUL2NX8` | `{31,32}` | `{16,17,18,19,20}` | `{22,23,35,36,37}` | HIGH · OBSERVED |
| `N1_S2_Mul` | `0x3f76b0` | `IVP_MULNX16` | `{31,32}` | `{16,17,18,19,20}` | `{22,23,35,36,37}` | HIGH · OBSERVED |

`IVP_MUL2NX8` and `IVP_MULNX16` are **byte-identical** (field math shared). The
**2-bit sub-opcode selector** at `{31,32}` is `word0 bit31 \| (word1 bit32 << 1)`
— it selects the Mul-slot sub-operation. (The same sub-op-selector pattern the F0
page documents for `MULNX16 {28,29}` and the F6/F7/F11 page for `MUL2NX8`; here
the narrow Mul slot anchors at bit 16.) The multiplicand `vfield {16-20}` sits at
the slot offset; `vr {22,23,35,36,37}` spills into `word1`. Body, confirmed by
disassembly `[HIGH · OBSERVED]`:

```
0x3f6d50  shr $1,%edx ; and $0x1c                   ; \  vr low fragment
          shr $0x16,%eax ; and $0x3 ; or %edx,%eax  ; /  vr = {22,23,...} -> 0x130
          shr $0x10,%ecx ; and $0x1f                ; vfield {16..20} -> 0xec
          shr $0x1f (word0 bit31) ; (word1<<1 & 2)  ; subop {31,32} -> 0x28
```

**Comparison / bool form (`IVP_EQNX16`-class; vbool latch set):**

| Cell | Address | rep | latch `0x28` | latch `0x34` | latch `0x78` | Conf |
|---|---|---|---|---|---|---|
| `N2_S0_LdSt` | `0x3ffa30` | `IVP_EQNX16` | `{14,15,25,36}` | `{5,6,7,12,13}` | `{4,8,9,10,11}` | MED · INFERRED |

The compare ops in the LdSt slot decode into the vbool/lane-mask latch set
(`0x28`/`0x34`/`0x78`) — the per-lane predicate path. Bit-extraction is
`OBSERVED`; which latch is the bool *dst* vs the two vector *sources* is
`INFERRED` from latch ordering. `[MED · INFERRED]`

> **NOTE — no byte-load instruction.** Across the six probed cells
> (`0x381700`, `0x3817d0`, `0x389270`, `0x3f6d50`, `0x37a5c0`, `0x3fba80`) the
> `movz`/byte-load instruction count is **0**. Every operand — down to a 1-bit
> bank select or a 2-bit sub-op — is extracted from a 32-bit dword by
> `shr`+`and`. The "sub-dword / byte granularity" is in the *mask*, not the
> *load*. `[HIGH · OBSERVED]`

---

## 4. Annotated C pseudocode — the three narrow decoders

Reproduced from the disassembled bodies; symbol names are the real `slotfill__`
exports. `bundle.w0`/`bundle.w1` are the two 32-bit context words at `+0x00`/`+0x04`.

```c
/* ----- helper: scalar AR index with window rotation (64 physical AR) -----
   Shared verbatim across N0/N1/N2 S0 and the base-ISA x24 decoder.
   `lo` = bit position of the 3 low index bits; `hi` = bank-select bit.   */
static uint8_t ar_index(uint32_t w, int lo, int hi, uint32_t window_base) {
    uint32_t bank = ((w >> hi) ^ 1u) & 1u;          /* high bit, XOR-inverted */
    uint32_t idx  = (bank << 3)                      /* bank * 8               */
                  + ((w >> lo) & 0x7u)               /* 3 low index bits       */
                  + (window_base << 3);              /* window-base * 8        */
    return (uint8_t)(idx & 0x3fu);                   /* 64 physical AR         */
}

/* === slotfill__N0__N0_S0_LdSt_slot0__ADD  @0x381700 ====================== *
 * Byte-identical to N1_S0, N2_S0, x24 base ADD, and F0_S0_LdSt ADD.        */
int N0_S0_LdSt__ADD(ctx *c) {
    uint32_t w  = c->w0;                             /* word0 only           */
    uint32_t wb = c->windowed ? c->window_base : 0;  /* ctx+0x2c / ctx+0x30  */
    c->lat_0x24 = ar_index(w, /*lo*/12, /*hi*/15, wb);   /* dst r   */
    c->lat_0x48 = ar_index(w, /*lo*/ 8, /*hi*/11, wb);   /* src1 s  */
    c->lat_0x50 = ar_index(w, /*lo*/ 4, /*hi*/ 7, wb);   /* src2 t  */
    return 0;
}

/* === slotfill__N0__N0_S0_LdSt_slot0__ADDI @0x3817d0 ====================== *
 * RRI8: t-field becomes a sign-extended imm8 scattered for N0 (sign @ bit50). */
int N0_S0_LdSt__ADDI(ctx *c) {
    uint32_t w0 = c->w0, w1 = c->w1, wb = c->windowed ? c->window_base : 0;
    c->lat_0x24 = ar_index(w0, 12, 15, wb);          /* dst r  = {12..14}+bank */
    c->lat_0x48 = ar_index(w0,  8, 11, wb);          /* src s  = { 8..10}+bank */
    /* imm8 gathered low->high, then sign-flooded.  Sign bit lands at bit 50. */
    int32_t imm  = (int32_t)((w0 >> 12) & 0xf)       /* {12,13,14,15} low nibble */
                 | (((w0 >> 21) & 1) << 4)           /* {21} mid bit             */
                 | (((w1 >> 1)  & 3) << 5)           /* {33,34}                  */
                 | (((w1 >> 18) & 1) << 7);          /* {50} -> sign            */
    c->lat_0x50 = (imm << 24) >> 24;                 /* shl;sar -> -128..127    */
    return 0;
}
/* N1_S0 ADDI: identical EXCEPT sign bit is {38} (w1>>6) not {50}; shl 0x19.  */
/* N2_S0 ADDI: sign {38}, mid {25} (not {21}); S32I store-offset reuses this. */

/* === slotfill__N2__N2_S1_Ld_slot1__ADD  @0x37a5c0 ======================== *
 * Narrow Ld slot: split-packed dst {26}|{29,30}|bank{31}.                   *
 * Byte-identical to slotfill__F4__F4_S1_Ld_slot1__ADD @0x2a5cf0.           */
int N2_S1_Ld__ADD(ctx *c) {
    uint32_t w = c->w0, wb = c->windowed ? c->window_base : 0;
    uint32_t dst = ((w >> 0x1a) & 1)                 /* bit {26}            */
                 | ((w >> 0x1c) & 6);                /* bits {29,30}        */
    uint32_t bank = ((w >> 0x1f) ^ 1u) & 1u;         /* bit {31}, inverted  */
    c->lat_0x24 = ((dst | (bank << 3)) + (wb << 3)) & 0x3fu;   /* dst       */
    c->lat_0x48 = ar_index(w, 16, 19, wb);           /* src1 s {16..18}+19  */
    c->lat_0x50 = ar_index(w, 20, 23, wb);           /* src2 t {20..22}+23  */
    return 0;
}

/* === slotfill__N0__N0_S3_ALU_slot3__IVP_ADDNX16 @0x389270 =============== *
 * Vector ALU at slot bit 16; Mul/alt latch set; 5-bit IVP vreg indices.    */
int N0_S3_ALU__IVP_ADDNX16(ctx *c) {
    uint32_t w0 = c->w0, w1 = c->w1;
    c->lat_0x6c = (w0 >> 16) & 0x1f;                 /* vs {16..20} @slot     */
    c->lat_0x28 = /* vt {22,23,25,26,28} gathered */;
    c->lat_0xb0 = /* vr {29,30,31,32,35} (spills into w1) */;
    return 0;                                         /* MAXNX16/MINNX16 == this */
}

/* === slotfill__N1__N1_S2_Mul_slot2__IVP_MUL2NX8 @0x3f6d50 =============== *
 * Mul slot: 2-bit sub-op {31,32} + multiplicand {16..20} + vr {22,23,35..37}. */
int N1_S2_Mul__IVP_MUL2NX8(ctx *c) {
    uint32_t w0 = c->w0, w1 = c->w1;
    c->lat_0xec  = (w0 >> 16) & 0x1f;                /* vfield (multiplicand) */
    c->lat_0x130 = ((w0 >> 22) & 3) | ((w1 << 1) & 0x1c) /* ... */;  /* vr     */
    c->lat_0x28  = ((w0 >> 31) & 1) | ((w1 << 1) & 2);  /* sub-op {31,32}    */
    return 0;                                         /* MULNX16 == this body  */
}

/* === slotfill__N0__N0_S1_None_slot1__NOP @0x388020 (and N0_S2, N1_S1) ==== */
int None__NOP(ctx *c) { return 0; }   /* xor eax,eax ; ret — no decode      */
```

---

## 5. Encode / decode inverse — slotfill ⇄ libisa-core

The `libisa-core` disassembler exposes, per slot, a **get_fn**
(`Slot_<fmt>_Format_<fmt>_s<N>_<unit>_<BITOFF>_get`: bundle → normalized 32-bit
slot word) plus per-operand `Field_*_Slot_<slot>_get`/`_set` accessors. The
`libfiss` slotfill **fuses** `get_fn ∘ field_get` into one bundle→index pass. The
inverse check composes them and verifies `field_get(field_set(0, v)) == v` over
the full field range.

### 5.1 get_fn BITOFF symbols match the slot table exactly

From `nm --defined-only ISA` `[HIGH · OBSERVED]`:

| Slot | get_fn symbol | get / set addr |
|---|---|---|
| `n0 s0` | `Slot_n0_Format_n0_s0_ldst_4_get` | `0x3b5400` / `0x3b54a0` |
| `n0 s1` | `Slot_n0_Format_n0_s1_none_58_get` | `0x3b5530` / `0x3b5580` |
| `n0 s2` | `Slot_n0_Format_n0_s2_none_59_get` | `0x3b55b0` / `0x3b55f0` |
| `n0 s3` | `Slot_n0_Format_n0_s3_alu_16_get` | `0x3b5610` / `0x3b56e0` |
| `n1 s0` | `Slot_n1_Format_n1_s0_ldst_4_get` | `0x3b4c30` / `0x3b4cf0` |
| `n1 s1` | `Slot_n1_Format_n1_s1_none_54_get` | `0x3b4db0` / `0x3b4e20` |
| `n1 s2` | `Slot_n1_Format_n1_s2_mul_16_get` | `0x3b4e70` / `0x3b4f70` |
| `n2 s0` | `Slot_n2_Format_n2_s0_ldst_4_get` | `0x3b5090` / `0x3b5170` |
| `n2 s1` | `Slot_n2_Format_n2_s1_ld_16_get` | `0x3b5250` / `0x3b5320` |

Every BITOFF token (`_4`, `_58`, `_59`, `_16`, `_54`) equals the FLIX slot
bit-offset. `[HIGH · OBSERVED]`

> **NOTE — the `None` get_fn BITOFFs (58/59/54) are real.** The two N0 `None`
> slots sit at bundle bits 58/59 and the N1 `None` at bit 54 in `libisa-core`'s
> slot table, even though the **slotfill** body for those slots is a pure NOP.
> The placeholder *has* a bit position in the encoder; the decoder just declines
> to read it. `[HIGH · OBSERVED]`

### 5.2 Field composition matches the slotfill latch — field-for-field

`[HIGH · OBSERVED]` (set/get accessor pairs all present, e.g.
`Field_fld_n0_s0_ldst_12_4_Slot_n0_s0_ldst_{get,set}`,
`Field_fld_n2_s1_ld_24_16_Slot_n2_s1_ld_{get,set}`):

| Cell | libisa field | slotfill latch | result |
|---|---|---|---|
| `N0_S0_LdSt` | r `{12,13,14,15}` | `0x24` | CONFIRMED |
| `N0_S0_LdSt` | s `{8,9,10,11}` | `0x48` | CONFIRMED |
| `N0_S0_LdSt` | t `{4,5,6,7}` | `0x50` | CONFIRMED |
| `N2_S1_Ld` | r `{26,29,30,31}` | `0x24` | CONFIRMED |
| `N2_S1_Ld` | s `{16,17,18,19}` | `0x48` | CONFIRMED |
| `N2_S1_Ld` | t `{20,21,22,23}` | `0x50` | CONFIRMED |
| `N0_S3_ALU` | vt `{22,23,25,26,28}` | `0x28` | CONFIRMED |
| `N0_S3_ALU` | vs `{16,17,18,19,20}` | `0x6c` | CONFIRMED |
| `N0_S3_ALU` | vr `{29,30,31,32,35}` | `0xb0` | CONFIRMED |
| `N1_S2_Mul` | multiply_vs `{16..20}` | `0xec` | CONFIRMED |
| `N1_S2_Mul` | multiply_vr `{22,23,35,36,37}` | `0x130` | CONFIRMED |
| `N1_S2_Mul` | sub-op `{31,32}` | `0x28` | CONFIRMED |

> **GOTCHA — the 2-input multiply aliases its multiplicand.** `libisa`
> `multiply_vt` is `{41,42,43,44,45}` (the `ADD_S` canonical `vs` latch), but in
> `MUL2NX8` the slotfill **collapses** vt and vs into the single `0xec`
> multiplicand field — the same `vt == vs` aliasing the F6/F11 page documents.
> The slotfill writes only the field bits each mnemonic needs (e.g. `multiply_arr`
> is the full 4-bit array selector `{25,26,29,30}`; the per-mnemonic body writes
> the 2 selector bits `{31,32}` it uses). `[HIGH · OBSERVED]`

### 5.3 Field-level `set == get⁻¹` inverse

`field_get(field_set(0, v)) == v` over the **full** field width
`[HIGH · OBSERVED]`:

| Field width | Fields | Range | Result |
|---|---|---|---|
| Vector 5-bit | `N0_S3_ALU` vr/vs/vt ; `N1_S2_Mul` vr | 0..31 | ALL OK |
| Scalar 4-bit | `N0_S0_LdSt` r/s/t ; `N2_S1_Ld` r | 0..15 | ALL OK |

Each field's `_set` is the **exact bit-inverse** of its `_get` over the full
range — encode and decode are mutual inverses.

### 5.4 Format-framing inverse (encode template ⇄ decode trigger)

`Format_N{0,1,2}_encode` (libisa, at `0x3b5950`/`0x3b5910`/`0x3b5930`) write a
16-byte template from `.rodata`. The first dword bytes (LE) `[HIGH · OBSERVED]`:

| Format | `.rodata` addr | bytes (byte0..3) | word0 (LE) |
|---|---|---|---|
| N0 | `0x3e2d10` | `0f 00 00 00` | `0x0000000f` (op0=F, b3lo=0) |
| N1 | `0x3e2cf0` | `0f 00 00 08` | `0x0800000f` (op0=F, b3lo=8) |
| N2 | `0x3e2d00` | `0f 00 00 18` | `0x1800000f` (op0=F, b3lo=8 + bit28) |

Feeding each word0 through the FLIX format-decoder predicate tree returns
N0/N1/N2 respectively — `encode == decode⁻¹` at the framing level. **Disjointness:**
no N-format slotfill operand field touches bits `[0:3]` (the op0 selector); the
minimum operand bit is 4 (S0) or 16 (S1/S2/S3). Selector and operand fields are
disjoint, so encode and decode are mutually consistent. **No mismatch was found in
any cross-check in this slice.** `[HIGH · OBSERVED]`

---

## 6. Operand-struct handoff — feeding the pipeline

The `regload`/`stateload`/`opcode`/`writeback` families are keyed by **mnemonic**
(1,534 regload for the whole image), not by `format×slot`. The ~1,036 distinct
N-format mnemonics therefore feed the **same shared handoff path** proven for
F0–F11 — the narrow-format work is **entirely** in the slotfill bit decode;
everything downstream reads the operand-index latches.

End-to-end trace for a full-width `IVP_ADDNX16` (`N0_S3_ALU` class)
`[HIGH · OBSERVED]`:

| Stage | Symbol | Action |
|---|---|---|
| **DECODE** | `slotfill__…IVP_ADDNX16 @0x389270` | bundle → vt `0x28`, vs `0x6c`, vr `0xb0` |
| **STATE** | `stateload__ivp_addnx16 @0x39cb60` | config/window arch-state → context |
| **VALUES** | `regload__ivp_addnx16 @0x39cb80` | reads index latch `0x6c` (`shl $0x4` = ×16); `base = *(*(rsi+8)+0x10)`; for lane 0..15: `ctx[0x70+4·lane] = base[idx·16+lane]` (512-bit NX16 gather); same for `0xb0`, `0x28` |
| **EXECUTE** | `opcode__ivp_addnx16__stage_5 @0x39c6f0` | per-lane `module__xdref_*` datapath |
| **COMMIT** | `writeback__ivp_addnx16 @0x39cd10` | `dstidx = ctx[0x28]` (vt doubles as dst); `shl $0x4`; for lane: `base[dstidx·16+lane] = ctx[0x2c+4·lane]` (16-lane scatter) |

The three index latches `regload` dereferences (`0x6c`/`0xb0`/`0x28`) are
**exactly** the three the `N0_S3_ALU` slotfill wrote — this is the identical
`regload__ivp_addnx16` / `writeback__ivp_addnx16` the F4/F6/F7/F11 page traces,
confirming the narrow formats reuse the shared downstream machinery. The scalar
path is the analogue with `dst→0x24` (`writeback__add`:
`ctx[0x28] → regfile[ctx[0x24]]`). `[HIGH · OBSERVED]`

So: **slotfill** = the only producer of operand *indices*; **regload** converts
indices → 512-bit values; **opcode** computes per-lane; **writeback** commits to
`regfile[idx]`. The N0/N1/N2 slotfill is the decode root of the per-instruction
pipeline, with the same downstream path as F0–F11.

---

## 7. Wide vs narrow — the one structural contrast

| Trait | Wide (F0–F11) | Narrow (N0/N1/N2) |
|---|---|---|
| Bundle size | 16 B / 128 bit | **8 B / 64 bit** |
| Words read | word0..word3 (`0x0`/`0x4`/`0x8`/`0xc`) | **word0/word1 only** |
| Vector ALU slot anchor | bit 31 / 36 (room above 64) | **bit 16** (no room above 64) |
| Imm8 scatter | fixed per wide format | **moves per narrow format** (sign 50/38/38, mid 21/21/25) |
| Scalar AR decoder | `x24` base, re-anchored | **same `x24` base, re-anchored** (byte-identical) |
| Narrow Ld split-dst | `F4_S1`/`F3_S1` | **same body, byte-identical** |
| Latch set | `0x24/0x48/0x50` + `0x94/0x50/0xd8` + `0x28/0x6c/0xb0` | **identical** |

The decoder is the same machine. The only narrow-specific code is: (1) the
absence of `word2`/`word3` reads, (2) the low ALU/Mul slot anchor, and (3) the
per-format immediate scatter shift (`shl $0xd` vs `shl $0x19`). `[HIGH · OBSERVED]`

---

## 8. ASCII picture — the three narrow 64-bit bundles

```
N0 (op0=0xF, b3lo=0)  — 64-bit, 4 slots (LdSt | None | None | ALU@16):
bit 0   4              16                  22  25 28 29     35
    +---+--------------+-------------------+---+--+--+-------+ ... bit63
    |op0| s0 LdSt @4    | s3 ALU @16 (vs)   |vt |vt|vt| vr hi |
    |=F | RRR t/s/r     | {16-20}           |   |  |  | (w1)  |
    +---+--------------+-------------------+---+--+--+-------+
    s0 LdSt (scalar): t->0x50{4,5,6}+b7  s->0x48{8,9,10}+b11  r->0x24{12,13,14}+b15
    s3 ALU  (vector): vt->0x28{22,23,25,26,28} vs->0x6c{16-20} vr->0xb0{29,30,31,32,35}
    s1/s2 None      : NOP filler (xor eax,eax;ret)

N1 (op0=0xF, b3lo=8)  — 64-bit, 3 slots (LdSt | None | Mul@16):
bit 0   4              16              22 31 32        35-37  41-45
    +---+--------------+---------------+--+--+--------+------+------+
    |op0| s0 LdSt @4    | s2 Mul @16    |vr|subop     | vr hi| vs hi|
    +---+--------------+---------------+--+--+--------+------+------+
    s0 LdSt (scalar): RRR t/s/r at {4,5,6}/{8,9,10}/{12,13,14}+banks
    s2 Mul (MUL2NX8): subop->0x28{31,32} vfield->0xec{16-20} vr->0x130{22,23,35,36,37}
    s2 Mul (ADD_S)  : vt->0x94{16-20} vs->0x50{41-45} vr->0xd8{22,23,35,36,37}
    s1 None         : NOP filler

N2 (op0=0xF, b3lo=8|bit28)  — 64-bit, 2 slots (LdSt@4 | Ld@16):
bit 0   4              16          20      26  29 30 31
    +---+--------------+-----------+-------+--+--+--+ ... bit63
    |op0| s0 LdSt @4    | s1 Ld @16 | t     |r |r |r |
    +---+--------------+-----------+-------+--+--+--+
    s0 LdSt (scalar): RRR/RRI8; imm8 sign-ext scatter {12-15}∪{25}∪{33,34}∪{38}
    s1 Ld  (narrow) : t->0x50{20,21,22}+b23  s->0x48{16,17,18}+b19
                      r->0x24{26}|{29,30}+bank31  (split-dst, == F4_S1)

All three: operand fields live in bits[0:63] (word0/word1); word2/word3 unused.
regload x16-gathers each index into a 512-bit NX16 value block; opcode__*_stage5
calls module__xdref_*; writeback x16-scatters to regfile[dstidx].
```

---

## 9. Confidence ledger

**HIGH · OBSERVED**

* The 1,790 N0/N1/N2 slotfill census and every per-`(format×slot×unit)` count;
  slot counts N0=4/N1=3/N2=2; the 3 NOP-only `None` slots (`xor eax,eax;ret`).
* The slotfill ABI: bundle words `@0x0`/`@0x4` only (`word2`/`word3` never read);
  index latches `0x24`/`0x48`/`0x50` scalar; `0x94`/`0x50`/`0xd8` ALU-canonical;
  `0x28`/`0x6c`/`0xb0` Mul/alt; `0x28` Mul sub-op + `0xec`/`0x130` vreg;
  `0x28`/`0x34`/`0x78` vbool compare; `0xac` unary guard.
* The scalar AR field bit-positions for all S0/S1 cells incl. `N2_S1`'s narrow
  `{26,29,30,+31}` split-dst; the window-rotation 6-bit-AR masking; the
  byte-identity of N-format S0 ADD with `x24` and of `N2_S1` ADD with `F4_S1`.
* The immediate sign-extension (`shl;sar`) and the **format-specific** imm8
  scatter (sign 50/38/38, mid 21/21/25), per-bit by emulation; S32I reusing the
  RRI8 decoder.
* The vector vr/vs/vt 5-bit scatter for `N0_S3_ALU` (`alu@16`) and `N1_S2_Mul`
  (`ADD_S` canonical + `MUL2NX8`/`MULNX16`); the 2-bit Mul sub-op `{31,32}`;
  field-math uniformity (MAX/MINNX16 == ADDNX16, MULNX16 == MUL2NX8).
* The libisa `get_fn ∘ field` composition matching the slotfill latch bits for
  every cell tested, no mismatch; get_fn BITOFF symbols matching the slot table;
  field `set == get⁻¹` (vector 5-bit + scalar 4-bit, all values); the framing
  inverse and selector/operand disjointness.
* The slotfill → regload → opcode → writeback handoff shared with F0–F11.
* **No `movz`/byte-load instruction** in any probed N-format body.

**MED · INFERRED**

* The `IVP_EQNX16`-class vbool/compare-latch **roles** (`0x28`/`0x34`/`0x78`):
  bit-extraction OBSERVED, but which latch is the bool dst vs the two vector
  sources is inferred from latch ordering.
* The N0_S0/N2_S0 IVP gather/scatter ops (`IVP_GATHERAN*`/`LA*`) have
  addressing-mode predicate branches (`jne`) the linear field-probe emulator does
  not fully execute; their register-index decode is the same vector latch family
  (HIGH), but a few of those bodies' exact addressing-mode sub-fields are MED.
* The IVP vector register file size (32 entries × 16 dwords / 512-bit NX16) is
  inferred from the ×16 gather stride and 5-bit index width, not a declared bound.

**LOW:** none material — every §3/§4 row for a non-gather mnemonic is anchored to
a disassembled body and corroborated by the libisa-core composition (§5).

---

*The full slotfill grid — F0–F3, F4/F6/F7/F11, N0/N1/N2, all 11 issue formats —
is now complete. The narrow formats prove the decoder is a single re-anchored
bitfield machine: same latch set, same scalar/narrow-Ld bodies, same downstream
pipeline; only the 64-bit clamp, the low slot anchors, and the per-format
immediate scatter are new. The runnable cross-validation harness that toggles a
bundle bit and watches the latch move — and that closes the `set == get⁻¹` loop
against libisa-core — is described in the validation chapter of Part 15.*
