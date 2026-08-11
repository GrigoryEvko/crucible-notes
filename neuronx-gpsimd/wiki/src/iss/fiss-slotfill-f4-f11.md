# fiss Slotfill — FLIX Formats F4/F6/F7/F11

> **Scope.** This page documents the **operand-decode** layer of the functional
> ISS — the `slotfill__*` exports of **`libfiss-base.so`** — for the four **wide**
> FLIX formats **F4, F6, F7, and the 5-slot F11**. It is the direct sibling of
> [the F0–F3 page](./fiss-slotfill-f0-f3.md) and the
> [narrow-format page](./fiss-slotfill-n0-n2.md): the decode *shape* is identical,
> only the slot bit-offsets change. Read those first if you have not — this page
> assumes the slotfill ABI (`%rdi`→context, bundle as four LE dwords, fixed index
> latches) established there.
>
> A reimplementer who wants the *value* layer (the 864-leaf oracle that actually
> computes results) wants [the fiss Datapath Oracle](./fiss-datapath-oracle.md).
> The encode side — the inverse of everything here — is
> [the FLIX encoding table](../isa/core/flix-encoding.md) and
> [the libisa table schema](../isa/core/libisa-table-schema.md).

Everything below is read from two shipped binaries, by absolute path, with
`nm` / `objdump -d` / `readelf` / `xxd` and a single-bit-probe field emulator. No
external source was consulted; every offset is a byte in one of these two files.

| binary | role | path under `ncore2gp/config/` | size |
| --- | --- | --- | --- |
| `libfiss-base.so` | **value oracle** — the slotfill *decoders* documented here | `libfiss-base.so` | 12 330 016 |
| `libisa-core.so` | **codec** — the encode thunks + `_get`/`_set` field accessors | `libisa-core.so` | 9 690 712 |

`readelf -SW libfiss-base.so` confirms `.text` (VMA `0x190430` == file off `0x190430`)
and `.rodata` (VMA `0x88ff00` == file off `0x88ff00`) are **VMA==fileoffset**; only
`.data` carries a delta (VMA `0xc8eb68` − file off `0xa8eb68` = **`0x200000`**). Every
`objdump --start-address` below uses a `.text` VMA directly.
---

## 1. Where these four formats sit

The keystone for the whole `libfiss-base.so` survey: **`nm -D --defined-only` lists
20 379 defined exports**, of which **12 569 are `slotfill__*` operand decoders** and
**864 are `module__xdref_*` value leaves**. The slotfill functions are the
operand-**decode** layer — the functional-ISS inverse of the libisa encode thunks.
This page carves out the four wide formats from that 12 569.
```
$ nm -D --defined-only libfiss-base.so | rg -c 'slotfill__'                 # 12569
$ nm -D --defined-only libfiss-base.so | rg -c 'slotfill__(F4|F6|F7|F11)__' #  4158
```

The 4 158 wide-format slotfills reconcile **to the unit** per (format × slot × unit)
cell. Symbol convention (read from `nm`): `slotfill__<FMT>__<FMT>_S<N>_<unit>_slot<N>__<MNEMONIC>`,
e.g. `slotfill__F11__F11_S0_Ld_slot0__ADD`.

| format | slots | unit roster | op0 / `byte3` selector | slotfill count |
| --- | --- | --- | --- | --- |
| `F4` | 4 | Ld · Ld · Mul · ALU (dual-load) | `0f 00 00 09` | 753 |
| `F6` | 4 | LdSt · Ld · Mul · ALU | `0f 00 00 23` | 1 045 |
| `F7` | 4 | LdSt · Ld · Mul · ALU | `0f 00 00 13` | 1 674 |
| `F11` | **5** | Ld · ALU · Mul · ALU · ALU | `0e 00 00 08` | 686 |

Per-cell `nm` census — all 17 cells, every column sum equal to the per-format total,
grand total **4 158** (verified by `rg -o 'S[0-9]_[A-Za-z]+_slot[0-9]' | sort | uniq -c`):

| F4 (4) | n | F6 (4) | n | F7 (4) | n | **F11 (5)** | **n** |
| --- | --: | --- | --: | --- | --: | --- | --: |
| `S0_Ld` | 192 | `S0_LdSt` | 329 | `S0_LdSt` | 348 | **`S0_Ld`** | **93** |
| `S1_Ld` | 249 | `S1_Ld` | 266 | `S1_Ld` | 257 | **`S1_ALU`** | **66** |
| `S2_Mul` | 61 | `S2_Mul` | 203 | `S2_Mul` | 521 | **`S2_Mul`** | **203** |
| `S3_ALU` | 251 | `S3_ALU` | 247 | `S3_ALU` | 548 | **`S3_ALU`** | **233** |
| | | | | | | **`S4_ALU`** | **91** |
| **753** | | **1045** | | **1674** | | **686** | |

> **NOTE — F11 is one of only two 5-slot formats.** Its sibling is `F3`
> (`LdSt·Ld·Mul·ALU·ALU`). F11's roster begins `Ld` (not `LdSt`) and ends with a
> *third* ALU slot, making it the **no-store triple-ALU wide format**. The F11
> count 686 splits 93/66/203/233/91; the table above is the ground truth a
> reimplementer must reproduce.
### 1.1 Load/store capability is not gated by the slot name

A genuine finding worth flagging because the slot *label* misleads:

- **`F4_S0_Ld`** is named "Ld" yet its 192 mnemonics include real integer stores
  (`S32I`/`S16I`/`S8I`) plus `IVP_SALIGN_I`/`SAPOS_FP` — F4 s0 **permits store
  encodings**.
- **`F11_S0_Ld`** is also named "Ld" but carries **zero** `S32I`/`S16I`/`S8I` — it
  is **genuinely load-only**, confirming F11 has no dedicated store path on s0.
- **`F6_S0_LdSt`** / **`F7_S0_LdSt`** are full load/store (the 329/348 sets include
  `SALT`/`SALTU` ALU forms plus true stores).

> **GOTCHA — do not infer store-capability from the TIE unit label.** F4's `Ld`
> slot stores; F11's `Ld` slot does not. The discriminator is the *mnemonic set*
> behind the cell, not the `Ld`/`LdSt` suffix.
---

## 2. The slotfill ABI (re-stated for the wide formats)

Every `slotfill__(F4|F6|F7|F11)__*` body obeys the same convention proven for F0–F3:
**one argument `%rdi` → per-op context; no frame, no host call; `xor eax,eax; ret`.**
Offsets the body **reads** are bundle words; offsets it **writes** are operand latches.

```
context (%rdi):
  +0x00  word0 [31:0]    +0x04 word1 [63:32]   +0x08 word2 [95:64]   +0x0c word3 [127:96]
  +0x2c  windowed-mode predicate   (test;je -> select AR window-rotation path)
  +0x30  window-base value          (shl $3 => x8, added into each AR index, then & 0x3f)

  ---- SCALAR base-Xtensa AR latches ----
  +0x24  r / dst  AR index          +0x48  s / src1 AR index
  +0x50  t / src2 AR index  OR  sign-extended immediate

  ---- VECTOR IVP latches ----
  ALU canonical : +0x50 vs   +0x94 vt   +0xd8 vr      (ADD_S / _H two-lane thunks)
  Mul / alt     : +0x6c vs   +0x28 vt   +0xb0 vr      (IVP_*NX16 full 16-lane)
  Mul sub-op    : +0x28 (2-bit MUL2NX8-class selector) + 0xec / 0x130 vreg fields
```

The two **vector latch sets are not redundant**: the `0x94/0x50/0xd8` ALU-canonical
set carries the two-lane `ADD_S`/`_H` "scalar-of-vector" thunks (operands in the low
64 bits, word2/word3 untouched); the `0x28/0x6c/0xb0` Mul/alt set carries the full
16-lane `IVP_*NX16` forms (operands scatter all the way into word3). Which set a given
mnemonic uses is fixed by its form, not chosen at run time.
> **NOTE — the slotfill writes *indices only*.** No arithmetic on operand values
> ever happens here. The index latch `L` later pairs with a 16-dword (512-bit NX16)
> value block at `L+4`, written by `regload__*` (§6), not by the slotfill.

---

## 3. The decode primitive: bit-extraction is the inverse of the encode thunk

Every slotfill body is a fixed `shr`/`and`/`or` chain — one straight-line gather, no
loop, no table (the immediate-table forms in §5.4 are the sole exception). There are
exactly two field-extraction idioms, and they are the literal inverse of how
[the libisa encode thunk](../isa/core/libisa-table-schema.md) *deposited* the field:

**Register index (zero-extend).** `idx = (word >> shift) & mask`. The encode thunk did
`word |= (idx & mask) << shift`; the decode is its exact bit-inverse over the field
width.

**Scalar AR bank/window assembly.** The 4 bits of a base-Xtensa AR field are
`{3 contiguous index bits} + {1 high bank bit}`. The bank bit is **XOR-inverted**,
weighted **×8** (`lea (,reg,8)`), the **window-base** (`ctx+0x30 << 3`) is added, and
the result is masked `& 0x3f` — yielding a 6-bit physical AR index (64 physical AR
registers behind a rotating 16-entry window).

**Signed immediate (sign-extend).** For immediate operands the body uses
`shl ; sar` (arithmetic right shift) instead of `shr ; and`. The `sar` propagates the
sign bit. **Operand *kind* is the discriminator: register = zero-extend, immediate =
sign-extend.** (Worked example in §5.2.)

The format-selector nibble `bits[3:0] = op0` (and the `byte3` selector) is **never
touched** by operand decode — every slotfill reads only bits ≥ 4 within its slot, so
the encode-time selector and the decode-time operand fields are **disjoint**. That
disjointness is what makes encode and decode mutually consistent (§7).

---

## 4. The 5-slot F11 in full (the widest format)

F11 gets the deepest treatment because it is the widest and exercises every idiom.
The five `get_fn` bit-offsets in `libisa-core.so` are
`Slot_f11_Format_f11_s0_ld_4_get`, `…_s1_alu_16_get`, `…_s2_mul_41_get`,
`…_s3_alu_31_get`, `…_s4_alu_24_get` — bit-offsets **4, 16, 41, 31, 24**, matching
[the FLIX slot roster](../isa/core/flix-encoding.md#5-the-46-slot-roster--bit-offsets-widths-units-formats)
exactly.
```
   128-bit FLIX F11 bundle (op0 = 0xE, byte3 lo = 8):
   bit 0   4         16        24      31        41             79  81   97
       +---+---------+---------+-------+---------+--------------+---+----+
       |op0| s0 Ld   | s1 ALU  | s4ALU | s3 ALU  | s2 Mul @41   |s3 |s4  | s4
       |=E | @4 w30  | @16 w21 | @24   | @31     | sub-op{58,91}|vr |vt  | vt
       +---+---------+---------+-------+---------+--------------+---+----+
   (physical bit order is NOT logical slot order: s4 @24 sits BEFORE s3 @31 and
    s2 Mul @41; high vreg bits of s3/s4 spill into word2/word3 at 79/81/97/98.)
```

### 4.1 `F11_S0_Ld` — scalar AR, load-only (`@0x77d250`, `ADD`)

The cleanest scalar body. `objdump -d --start-address=0x77d250`:

```asm
77d250: mov 0x2c(%rdi),%eax        ; windowed-mode predicate
77d253: mov 0x30(%rdi),%edx ; shl $3 ; → window-base x8 in %edx
77d259: test %eax,%eax  ;  mov (%rdi),%eax   ; word0
77d25f: je   77d2d8                ; (windowed path; non-windowed shown below)
        ; ---- t / src2 → 0x50 : {4,5,6} + bank{7} ----
77d261: shr $0xf,%esi ; xor $1,%esi ; and $1,%esi ; lea (,%rsi,8),%ecx  ; bank bit15 inv x8 (... see note)
77d273: mov %eax,%esi ; shr $0xc,%esi ; and $7,%esi ; or %ecx,%esi      ; {12,13,14} for r-path
        ...
77d2b2: mov %ecx,0x48(%rdi)        ; s / src1 = {8,9,10}+bank{11}
77d2c3: mov %esi,0x24(%rdi)        ; r / dst  = {12,13,14}+bank{15}
77d2cd: mov %eax,0x50(%rdi)        ; t / src2 = {4,5,6}+bank{7}
77d2d0: xor %eax,%eax ; ret
```

Decoded fields (bundle-absolute bits; `+N` = the high bank bit):

| latch | role | bits |
| --- | --- | --- |
| `0x50` | `t` / src2 | {4, 5, 6, **+7**} |
| `0x48` | `s` / src1 | {8, 9, 10, **+11**} |
| `0x24` | `r` / dst | {12, 13, 14, **+15**} |

Each AR field: `(word0 >> shift) & 7` for the 3 index bits; the bank bit is
`((word0 >> bankshift) ^ 1) & 1`, weighted `<< 3`, then `+ window_base`, then `& 0x3f`.
**S0 begins at bundle bit 4** — exactly the slot's `_4_` `get_fn` token.
### 4.2 `F11_S1_ALU` — scalar AR with dst spilling into word1 (`@0x743ed0`, `ADD`)

S1 begins at bundle bit 16. Its dst lands in the **high bits of word1** (`@0x743ed0`):

```asm
743ed6: mov 0x4(%rdi),%ecx         ; word1
743ee2: mov (%rdi),%r8d            ; word0 (for the dst bank bit)
743ee7: shr $0x1c,%ecx ; and $7,%ecx        ; r index = word1[28,29,30] = bundle{60,61,62}
743eea: shr $0x1f,%eax ; shl $3,%eax ; or %ecx,%eax   ; bank = word0[31] = bundle{63}, x8
743f00: and $0x3f,%eax
743f29: mov %eax,0x24(%rdi)         ; r/dst  = {60,61,62,+63}
743f2f: mov %ecx,0x48(%rdi)         ; s/src1 = {16,17,18,+19}
        ; (t/src2 path → 0x50 = {20,21,22,+23})
```

| latch | role | bits |
| --- | --- | --- |
| `0x50` | `t` / src2 | {20, 21, 22, **+23**} |
| `0x48` | `s` / src1 | {16, 17, 18, **+19**} |
| `0x24` | `r` / dst | {60, 61, 62, **+63**} |

> **QUIRK — the dst of S1 lives 44 bits away from its src operands.** `s`/`t` sit
> contiguously at bits 16–23, but `r`/dst is packed into the *top* of word1
> (bits 60–63), with the bank bit borrowed from word0[31]. This is the FLIX
> non-contiguity: a "slot" is a *set* of scattered bit-runs, not a span.
### 4.3 `F11_S2_Mul` — vector, 2-bit sub-op + two vregs (`@0x749140`, `IVP_MUL2NX8`)

The Mul slot decodes a **2-bit sub-opcode selector** plus two 5-bit vreg fields
(`@0x749140`):

```asm
749140: mov 0x8(%rdi),%eax         ; word2
749143: mov 0x4(%rdi),%edx         ; word1
749148: shr $1,%eax  ; and $0x1f,%eax  ; → 0x130 = word2[1..5]  = bundle{65,66,67,68,69}
749154: shr $9,%edx  ; and $0x1f,%edx  ; → 0xec  = word1[9..13] = bundle{41,42,43,44,45}
        ; sub-op (2 bits): word2[26]→bit91 | word1[26]→bit58
74914a: shr $0x1a,%ecx ; and $2,%esi  ; (word2 bit27→0x2)
749157: shr $0x1a,%ecx ; and $1,%ecx  ; (word1 bit26→0x1) ; or → 0x28
749163: mov %eax,0x130(%rdi)
749169: mov %edx,0xec(%rdi)
749173: mov %ecx,0x28(%rdi)
```

| latch | role | bits |
| --- | --- | --- |
| `0x28` | 2-bit Mul sub-op | {58, 91} |
| `0xec` | multiplicand vfield | {41, 42, 43, 44, 45} |
| `0x130` | product / 3rd vreg | {65, 66, 67, 68, 69} |

The sub-op selector `{58,91}` = `word1[26] \| word2[27]` picks the Mul-slot
sub-operation (the `MUL2NX8`-class family). The multiplicand vfield lands at the slot
offset (bit 41 = `mul@41`); the product reg sits in word2.
> **NOTE — `vt`==`vs` for `MUL2NX8`.** The libisa `_get` accessors return the *same*
> bits `{41-45}` for both `vt` and `vs`; the slotfill collapses them into the single
> `0xec` latch. The 2-input multiply **aliases its multiplicand input** — one
> physical vreg feeds both ALU input ports. Bits OBSERVED; the aliasing role is
> `[MED/INFERRED]` from the latch collapse + op semantics.

### 4.4 `F11_S3_ALU` — full 16-lane vector, roles permuted (`@0x74bb60`, `IVP_ADDNX16`)

S3 begins at bundle bit 31 (`alu@31`). It reads **word0/1/2** and is the cleanest
full-width vector body (`@0x74bb60`):

```asm
74bb60: mov 0x4(%rdi),%eax ; mov (%rdi),%edx   ; word1, word0
        ; ---- vs → 0x6c ----
74bb65: mov %eax,%ecx ; and $6,%ecx             ; word1[1,2] = bundle{33,34}
74bb67: shr $0x1f,%edx ; or %edx,%ecx           ; word0[31]  = bundle{31}
74bb6f: shr $0xf,%edx2... and $0x18              ; word1[15,16] -> bundle{50,51}
74bb7c: mov %ecx,0x6c(%rdi)                      ; vs = {31,33,34,50,51}
        ; ---- vr → 0xb0 ----
74bb84: shr $4,%edx ; and $0xf,%edx              ; word1[4,5,6,7] = bundle{36,37,38,39}
74bb7f: mov 0x8(%rdi),%edx2; shr $0xb; and $0x10 ; word2[15]   = bundle{79}
74bb9c: mov %edx,0xb0(%rdi)                      ; vr = {36,37,38,39,79}
        ; ---- vt → 0x28 ----
74bb96: shr $0xd,%esi ; and $0x1e               ; word1[14,15,16,17] = bundle{46,47,48,49}
74bb93: shr $0x18,%eax ; and $1                 ; word1[24]          = bundle{56}
74bba7: mov %eax,0x28(%rdi)                     ; vt = {46,47,48,49,56}
74bbac: ret
```

| latch | role | bits |
| --- | --- | --- |
| `0x28` | `vt` | {46, 47, 48, 49, 56} |
| `0x6c` | `vs` | {31, 33, 34, 50, 51} |
| `0xb0` | `vr` | {36, 37, 38, 39, **79**} |

> **CORRECTION — F11_S3 is the `alu@31` layout but with the latch *roles* permuted
> versus F7's identical-offset slot.** F7_S3_ALU (also `alu@31`) maps the same bit
> sets to `vt→0x94, vs→0x50, vr→0xd8` (ALU-canonical); F11_S3 maps them to the
> Mul/alt latch set `0x28/0x6c/0xb0` with `vt` and `vr` **swapped** relative to F7.
> A reimplementer that assumes "same `get_fn` bit-offset ⇒ same operand latch
> assignment" will mis-route F11's S3 operands. The bit sets are shared; the latch
> destinations are not.
### 4.5 `F11_S4_ALU` — the extra ALU slot, the only one reaching word3 (`@0x751070`, `IVP_ADDNX16`)

S4 is F11's third ALU slot, anchored at bit 24, and the **only F11 slot whose vreg
fields reach word3** (`@0x751070`):

```asm
751070: mov 0xc(%rdi),%esi         ; word3   <-- the widest read in F11
751073: mov 0x8(%rdi),%ecx         ; word2
751076: mov (%rdi),%edx            ; word0
751078: mov 0x4(%rdi),%eax         ; word1
        ; ---- vt → 0x28 ----
75107b: shl $2,%esi ; and $0x18,%esi          ; word3[1,2]   = bundle{97,98}
751089: shr $0x11,%esi2; and $7                ; word2[17,18,19] = bundle{81,82,83}
751095: mov %esi,0x28(%rdi)                    ; vt = {81,82,83,97,98}
        ; ---- vs → 0x6c ----  word2[6,7,8]/word0[24,25] → {72,73,74,25,26}
7510a8: mov %ecx,0x6c(%rdi)                    ; vs = {25,26,72,73,74}
        ; ---- vr → 0xb0 ----  word0[24]/word1[22,23,25,27] → {24,54,55,57,59}
7510c7: mov %eax,0xb0(%rdi)                    ; vr = {24,54,55,57,59}
7510cd: xor %eax,%eax ; ret
```

| latch | role | bits |
| --- | --- | --- |
| `0x28` | `vt` | {81, 82, 83, **97, 98**} |
| `0x6c` | `vs` | {25, 26, 72, 73, 74} |
| `0xb0` | `vr` | {24, 54, 55, 57, 59} |

> **QUIRK — F11_S4's `vt` is the deepest scatter in the whole wide slice.** Its
> `vt` field draws bits 97–98 from **word3** (`shl $2 ; and $0x18` on `0xc(%rdi)`),
> bits the other F11 slots never touch. This is the "5th issue slot pushes operands
> off the top of the bundle" effect — exactly what the `S4 @24` low offset plus a
> word3 spill produces.
---

## 5. F4 / F6 / F7 — the four-slot wide formats

The four-slot formats reuse F11's idioms; the differences are the slot bit-offsets
(`flix-encoding.md §5`) and which mnemonics each cell hosts. Representative bodies:

### 5.1 Scalar S0 is identical across all four wide formats (`@0x1a2c10` / `@0x415b00` / `@0x493cb0` / `@0x77d250`)

`F4_S0_Ld`, `F6_S0_LdSt`, `F7_S0_LdSt`, `F11_S0_Ld` all decode `ADD` with the same
bit layout — `t→0x50 {4,5,6,+7}`, `s→0x48 {8,9,10,+11}`, `r→0x24 {12,13,14,+15}` —
because **every s0 in the ISA begins at bundle bit 4**. The four bodies are
byte-for-byte the same gather; only their addresses differ.
### 5.2 The signed-immediate form — `F4_S0_Ld` `ADDI` (`@0x1a2ce0`)

The task-requested sign-extension case. `objdump -d --start-address=0x1a2ce0`:

```asm
1a2d29: mov %edx,0x24(%rdi)         ; r/dst (zero-ext, shr;and)
        ; ---- IMM → 0x50 : scattered across word0/word1/word2, SIGN-extended ----
1a2d3c: mov 0x8(%rdi),%esi
1a2d3f: shl $0x7,%esi               ; push the high imm field to the MSB
1a2d42: sar $0x18,%esi              ; ARITHMETIC right shift 24 -> sign-extends
        ; (combined by `or` with the low imm bits + and $0xffffff80 mask)
```

The decisive instruction is the **arithmetic `sar`** at `1a2d42`: register fields use
`shr` (logical, zero-fill); the immediate uses `sar` (arithmetic, sign-fill). The
operand **kind** — register vs immediate — is the *only* thing that selects between
zero-extend and sign-extend. The decoded `0x50` immediate is fed by the field
union with `word2[24]` (= bundle bit 88, the high imm chunk) as the eventual sign
source.
### 5.3 The narrow S1 dst-split — `F4_S1_Ld` `ADD` (`@0x2a5cf0`)

`F4_S1_Ld` is the **narrow** (24-bit) Ld slot, so its dst is split into discontiguous
runs (`@0x2a5cf0`):

```asm
2a5d01: shr $0x1c,%edx ; and $6,%edx       ; word0[29,30]  -> bundle{29,30}
2a5d09: shr $0x1a,%esi ; ... and $1         ; word0[26]     -> bundle{26}
2a5d13: not %esi ; shr $0x1f,%esi ; shl $3  ; bank bit = ~word0[31] x8  -> bundle{31}
2a5d2d: and $0x3f,%edx                       ; -> r/dst = {26}|{29,30}|bank{31}
```

| latch | role | bits |
| --- | --- | --- |
| `0x50` | `t` / src2 | {20, 21, 22, **+23**} |
| `0x48` | `s` / src1 | {16, 17, 18, **+19**} |
| `0x24` | `r` / dst | {26} ∪ {29, 30} ∪ bank{**31**} |

> **CORRECTION — the bank-bit inversion uses `not`, not `xor $1`.** The clean S0
> bodies invert the bank bit with `xor $0x1` *after* isolating it; the narrow
> `F4_S1_Ld` body inverts the **whole register** with `not %esi` *before* the
> `shr $0x1f` that isolates bit 31. The two idioms are functionally identical (both
> flip the bank bit), but a literal-instruction reimplementation must reproduce the
> `not`+`shr` sequence for the narrow dst, not an `xor`. The dst packing mirrors the
> `F3_S1` `{26,29,30}` split.
### 5.4 Immediate-table forms — `F11_S1_ALU` `IVP_EXT0IB` (`@0x745ba0`)

A handful of slotfills are **not** pure `shr`/`and`/`or` chains: they map a small bit
field through a `.rodata` table. `F11_S1_ALU IVP_EXT0IB` uses
`lea table__imm1_2N_tab ; mov (tbl,idx*4)` to translate a 3-bit field into a TIE
immediate value, writing the lane latches `0x28`/`0x34`/`0x40`. This is the only
table-driven decode shape in the wide slice; everything else is open-coded gather.
### 5.5 The two-lane ALU thunk (`ADD_S`) lives only in F7 (`@0x292320`)

`F7_S3_ALU ADD_S` (`@0x292320`) is the "scalar-of-vector" two-lane form: it reads
**word0+word1 only** (`objdump` shows 7× `(%rdi)` + 7× `0x4(%rdi)`, never `0x8`/`0xc`),
so its operands live in bits [0:63]. It uses the **ALU-canonical** latch set
`vt→0x94, vs→0x50, vr→0xd8`. F4/F6/F11 carry no `ADD_S` in their ALU slots — only
F7's ALU hosts the two-lane scalar-of-vector form; F4/F6/F11 ALU vectors are the full
`IVP_*NX16` family (Mul/alt latch set).
> **GOTCHA — "reads two words" vs "reads four words" is the latch-set tell.** A
> body that touches only `(%rdi)`/`0x4(%rdi)` is a two-lane ADD_S/_H form
> (`0x94/0x50/0xd8`); a body that touches all four words is a full 16-lane
> `IVP_*NX16` form (`0x28/0x6c/0xb0`). You can disambiguate the operand-latch
> assignment of a mnemonic *just* from which bundle words its slotfill reads.

### 5.6 `F4_S2_Mul` is the wide quad-multiply outlier (`@0x2ae3c0`, `IVP_DMULQ2N8XR8`)

Most Mul slots are the `MUL2NX8` shape (sub-op + 2 vregs, §4.3). `F4_S2_Mul` (`mul@28`,
the only F4 Mul offset) instead hosts the **quad packed-multiply** `IVP_DMULQ2N8XR8`,
which writes the **largest operand struct in the slice**: a sub-op at `0x28`{57,59},
**four** vreg fields at `0x1b0`/`0x238`/`0x27c`/`0x1f4`, a guard at `0x2c0`{84,85,86,87},
and a second selector at `0xec`{28,58}. It is still pure bitfield decode — just a
wider handoff. The extra-latch *roles* (which is dst/src/guard) are `[MED/INFERRED]`
from latch ordering + the `L+4` value-block pairing convention, not separately traced
per-latch. `[HIGH/OBSERVED bits; MED/INFERRED roles]`

---

## 6. Operand-struct handoff — what the decoded indices feed

The `regload`/`stateload`/`opcode`/`writeback` families are keyed by **mnemonic**, not
by format×slot (`nm`: 1 534 `regload__*`, 1 708 `opcode__*` image-wide). So the 4 158
wide-format slotfills feed the **same shared pipeline** proven for F0–F3 — the
format-specific work is entirely in the slotfill bit decode; everything downstream
reads the operand-index latches.

End-to-end trace for a full-width `IVP_ADDNX16` (the F4/F6/F7/F11 S3_ALU class):

```
slotfill__…IVP_ADDNX16            bundle → vt=0x28, vs=0x6c, vr=0xb0          (DECODE)
stateload__ivp_addnx16           config/window arch-state → context          (STATE)
regload__ivp_addnx16  @0x39cb80  reads latches 0x6c, 0xb0, 0x28:             (GATHER)
                                   idx = ctx[L];  idx <<= 4;                   ; x16 stride
                                   base = *(*(rsi+0x8)+0x10);
                                   for lane 0..15: ctx[L+4 + 4*lane] = base[idx*16 + lane]
                                   → 512-bit NX16 value blocks at 0x70.., 0xb4.., 0x2c..
opcode__ivp_addnx16__stage_5     per-lane module__xdref_* datapath           (EXECUTE)
writeback__ivp_addnx16 @0x39cd10 dstidx = ctx[0x28] (vt latch doubles as dst);(SCATTER)
                                   idx <<= 4;  base[dstidx*16 + lane] = ctx[0x2c + 4*lane]
```

`objdump -d --start-address=0x39cb80` confirms `regload__ivp_addnx16` opens with
`mov 0x6c(%rdi),%edx ; shl $0x4,%edx` — it reads the **exact `0x6c` latch the S3
slotfill wrote** and applies the ×16 (`shl $4`) gather stride. The decode → gather
contract is byte-verified.
> **NOTE — dst overlaps a source for the NX16 ALU forms.** The IVP vector ALU
> encodes dst on top of the `vt` operand group: `writeback` re-reads `0x28` (= `vt`)
> as the write target. This mirrors the F0–F3 `add_h` case (dst = `0x50`). F4's
> quad-multiply extends the operand struct with the extra 512-bit blocks
> (`0x1b0`/`0x1f4`/`0x238`/`0x27c`) for its four vector inputs.

So: **the slotfill is the only producer of operand indices**; `regload` converts
indices → 512-bit values; `opcode` computes per-lane over the 864-leaf `module__xdref_*`
primitives; `writeback` commits to `regfile[idx]`. The wide-format slotfill is the
decode root of the per-instruction pipeline, with the same downstream machinery as
F0–F3.
---

## 7. Encode ⇄ decode inverse — cross-validated against `libisa-core.so`

`libisa-core.so` exposes, per slot, a `get_fn`
(`Slot_<fmt>_Format_<fmt>_s<N>_<unit>_<bitoff>_get`: bundle → normalized 32-bit slot
word) plus per-operand `Field_*_Slot_<fmt>_s<N>_<unit>_get`/`_set` accessors (read/write
a named field in that normalized word). The `libfiss` slotfill **fuses** `get_fn ∘
field_get` into one direct bundle→index pass. If the reverse-engineering is correct,
two things must hold, and both were checked:

**(a) `get_fn` bit-offset tokens equal the slot roster.** Read from `nm`:

```
Slot_f4_Format_f4_s0_ld_4_get     Slot_f4_…_s1_ld_16    s2_mul_28    s3_alu_36
Slot_f6_…_s0_ldst_4               s1_ld_16              s2_mul_41    s3_alu_36
Slot_f7_…_s0_ldst_4               s1_ld_16              s2_mul_41    s3_alu_31
Slot_f11_Format_f11_s0_ld_4_get   s1_alu_16             s2_mul_41    s3_alu_31   s4_alu_24
```

Every `<bitoff>` token equals
[the FLIX slot bit-offset](../isa/core/flix-encoding.md#5-the-46-slot-roster--bit-offsets-widths-units-formats).
The five F11 `get_fn` thunks sit at `0x3b0ee0 / 0x3b10c0 / 0x3b1220 / 0x3b13e0 /
0x3b15e0`.
**(b) field accessors compose to the slotfill latch bits, field-for-field.** The
`Field_fld_ivp_sem_vec_alu_{vr,vs,vt}_Slot_f11_s3_alu_get` accessors exist (and their
`_set` siblings) and reproduce the `F11_S3` bit sets `{46-49,56}` / `{31,33,34,50,51}`
/ `{36-39,79}` from §4.4. Every wide cell tested — scalar AR (S0/S1) + vector ALU
(S3/S4) + Mul (S2) — composed to the slotfill latch bits with **no mismatch**.
**(c) `set == get⁻¹` over the field width.** `libisa-core.so` ships **3 237 `Field_*_get`
and 3 230 `Field_*_set`** accessors. For every field tested,
`field_get(field_set(0, v)) == v` over the full field range (vector 5-bit `v∈0..31`,
scalar 4-bit `v∈0..15`) — the field `_set` is the exact bit-inverse of its `_get`.
**Encode and decode are mutual inverses.**
> **NOTE — the 7-element `_get`/`_set` shortfall (3237 vs 3230) is fields with no
> writer**, not a broken inverse: every field that *has* a `_set` inverts its `_get`.
> A handful of derived/read-only fields expose only a getter.

**(d) format-framing inverse.** Each format's encode template is a 16-byte `movdqa`
constant in libisa `.rodata` whose `byte0`/`byte3` are the format signature. Read
directly with `xxd`/`objdump -s`:

| format | template (`@0x3e2c80…`) | op0 | `byte3` |
| --- | --- | --- | --- |
| `F11` | `0e 00 00 08` | E | `0x08` (bit27 set) |
| `F3` | `0e 00 00 00` | E | `0x00` |
| `F4` | `0f 00 00 09` | F | `0x09` |
| `F6` | `0f 00 00 23` | F | `0x23` |
| `F7` | `0f 00 00 13` | F | `0x13` |

These match
[the FLIX format-decoder triggers](../isa/core/flix-encoding.md#3-format-selection--format_decoder-0x3b5970)
exactly. The encode side fixes `bits[3:0]=op0` + the `byte3` selector; the slotfill
operand decode reads only bits ≥ 4 within each slot — **selector and operand fields
are disjoint**, so encode and decode are mutually consistent by construction.
**No mismatch was found in any cross-check across all 17 wide cells.**

---

## 8. Structural notes for a reimplementer

- **Template-generated bodies.** The 4 158 wide slotfills are one body per
  mnemonic × slot × format. Within a `(form, slot)` cell the field math is *identical*
  across mnemonics (verified across `ADD`/`ADDI`/`ADD_S`/`IVP_ADDNX16`/`MUL2NX8`
  samples); cells differ only in *how many* mnemonics they host. You can codegen the
  decoder from the slot roster + a per-form template.
- **Two vector latch sets are semantic, not redundant.** ALU-canonical
  (`0x94/0x50/0xd8`) = two-lane scalar-of-vector thunks (low 64 bits); Mul/alt
  (`0x28/0x6c/0xb0`) = full 16-lane `IVP_*NX16` (scatters into word3). The slot
  `get_fn` + field family disambiguate which set a mnemonic uses; the bundle-word
  read pattern is a reliable secondary tell (§5.5).
- **FLIX slots are scattered, not contiguous.** Every vector operand is a scattered
  5-bit field; the slotfill open-codes the gather as a fixed `shr`/`and`/`or` chain
  (no loop, no table) — except the immediate-table forms (§5.4).
- **F11 is the no-store wide format** (s0 load-only, triple-ALU s1/s3/s4); F4 is the
  dual-Ld format whose s0 still permits store encodings; F6/F7 are full 4-issue
  `LdSt·Ld·Mul·ALU` differing only in their `byte3` hi-selector and a couple of slot
  bit-offsets (F6 `alu@36` vs F7 `alu@31`; both `mul@41`).
- **Operand kind = extension mode.** Register index → zero-extend (`shr`/`and`);
  signed immediate → sign-extend (`shl`/`sar`). The format-selector nibble is never
  touched by operand decode.
---

## 9. Confidence ledger

| claim | tag |
| --- | --- |
| 4 158 wide-format slotfill census; all 17 per-cell counts; F11 = 5-slot (93/66/203/233/91); load/store capability nuance (F11 no-store, F4 stores-in-Ld, F6/F7 LdSt) | HIGH / OBSERVED |
| Slotfill ABI: bundle words `0x0–0xc`; scalar latches `0x24/0x48/0x50`; vector latches `0x94/0x50/0xd8` (ALU) and `0x28/0x6c/0xb0` (Mul/alt); Mul sub-op `0x28` + `0xec/0x130` | HIGH / OBSERVED |
| Every §4/§5 bit field (scalar AR incl. `F4_S1` `{26,29,30}` split; immediate `shl;sar` sign-ext; vector vr/vs/vt scatter; `MUL2NX8` sub-op; F11_S4 word3 spill) | HIGH / OBSERVED |
| libisa `get_fn ∘ field` composition matching the slotfill latch bits for every cell; `get_fn` bitoff tokens = slot roster; `set==get⁻¹`; encode-template framing inverse | HIGH / OBSERVED |
| `slotfill → regload → opcode → writeback` handoff (regload reads the same latches; ×16 gather; writeback scatter to `regfile[idx]`); two-lane vs full-width word usage | HIGH / OBSERVED |
| `MUL2NX8` `vt==vs` aliasing *role* (bits OBSERVED; "shares multiplicand input" semantic INFERRED) | MED / INFERRED |
| `F4` quad-multiply (`DMULQ2N8XR8`) extra-latch *roles* (`0x1b0/0x1f4/0x238/0x27c/0x2c0` dst/src/guard); bit-extraction OBSERVED, role assignment inferred from latch ordering | MED / INFERRED |
| IVP vector register file size (32 entries × 16 dwords / 512-bit NX16): inferred from ×16 gather stride + 5-bit index width, not a declared bound | MED / INFERRED |

Every §4/§5 row is anchored to a disassembled body in `libfiss-base.so` and, for
every cell, corroborated by the `libisa-core.so` `get_fn ∘ field` composition (§7).
No `LOW` claims are material to a reimplementation.
