# ISA Batch 11 — vbool ALU / predicate (boolean algebra · fold-into-FS · predicate scan)

This is the per-instruction reference for the **`vbool`-consuming, `vbool`/FS-producing** forms — the
pure **boolean-algebra ALU** over predicate masks and the **predicate-reduce / predicate-scan**
primitives. It is the *consumer* complement of the compare batches:
[B01](b01-vec-alu-int.md)/[B02](b02-vec-alu-fp.md)/[B03](b03-vec-alu-rest.md) **produce** a `vbool`
predicate from a `vec→vbool` comparison; **B11 owns what you do with the predicate once you have it** —
`andb`/`orb`/`xorb`/`notb`/`andnotb`/`ornotb` over two masks, the `bnorfs`/`borfs` reduce that collapses
eight `vbool` registers into one `IVP_FS*` predicate-state register, the `movvfs`/`movfsv` `vbool`↔FS
512-bit bridge, the `popc`/`count*` population-scan that turns a mask into an `AR` scalar, and the
single-bit (`BR`-register) `notb1`/`andnotb1`/`ornotb1` forms. B03 explicitly defers these here
("vbool→vbool boolean logic and the vbool-folding `ivp_bnorfs2n`/`ivp_borfs2n` are **B11**").

Concretely, B11 owns **27 mnemonics / 189 placements** of the certified-perfect `1534 / 12569` cover
([coverage-tally](../core/coverage-tally.md)), in five sub-families:

1. **vbool boolean algebra** (`andb`/`orb`/`xorb`/`notb`/`andnotb`/`ornotb`/`joinb`) — `vbool→vbool`,
   7 mnemonics. The pure 64-bit-mask bitwise operators.
2. **Single-bit `BR` boolean** (`notb1`/`andnotb1`/`ornotb1`) — the 1-bit scalar-boolean (`BR` file)
   siblings of the mask forms, 3 mnemonics.
3. **Predicate move / make / extract** (`mb`/`movab1`/`movba1`/`ext0ib`) — `AR`↔`vbool`↔`BR` structural
   bridges and bit-extract, 4 mnemonics.
4. **Fold-into-FS reduce** (`bnorfs2n`/`borfs2n`) — OR/NOR-reduce **eight** `vbool` registers into one
   `IVP_FS*` predicate-state register, 2 mnemonics.
5. **vbool↔FS bridge + predicate scan** (`movvfs`/`movfsv`; `popc2nx8`; the eight `count*4nx8`) — the
   512-bit lane-mask↔FS-state bit-permutation and the population/match-count scans, 11 mnemonics.

The encoding is read byte-exact from the non-stripped `libisa-core.so` (`Opcode_*_encode` selector
templates, the `ivp_sem_vbool_alu_ltr_{vbt,vbr,vbs}` field thunks); the **value semantics are proven by
execution** by driving the matching `module__xdref_*` leaves in `libfiss-base.so` live via `ctypes`
(license-free value lane). Every numeric claim is anchored to a binary address or a live call and tagged
`[HIGH/OBSERVED]` unless flagged. The prose is derived from static analysis and in-process execution of
the shipped artifacts only.

> **The B01/B03/B08/B11/fp-sub-isa partition boundary (read this first).** The predicate datapath is
> split by **what reads / writes the predicate**, with **zero mnemonic overlap**:
>
> | batch | op shape | reads | writes | examples |
> |---|---|---|---|---|
> | **B01/B02** | `vec→vec` bitwise | `vec` | `vec` | `ivp_and2nx8`, `ivp_or2nx8`, `ivp_xor2nx8`, `ivp_not2nx8` |
> | **B03** | compare / B-variant / `*t` | `vec` (+`vbool` for `*t`) | `vbool` / `vec`-merge | `ivp_eqnx16`, `ivp_bmaxnx16`, `ivp_addnx16t` |
> | **B08** | cross-lane reduce **within one mask** | `vbool` | `vbool` | `ivp_randb2n`, `ivp_rorbn` (the "all/any **within** a mask" reduce) |
> | **B11** (this page) | boolean algebra · multi-reg fold · scan | `vbool`/`BR` | `vbool`/FS/`AR` | `ivp_andb`, `ivp_borfs2n`, `ivp_movvfs`, `ivp_popc2nx8` |
> | **fp-sub-isa** | FCR/FSR arith-control state | — | FCR/FSR | `wur.fcr`, `rur.fsr` (the IEEE rounding/sticky state — **not** the `IVP_FS*` predicate file) |
>
> The vec-bitwise `ivp_and2nx8`/`ivp_or2nx8`/`ivp_xor2nx8`/`ivp_not2nx8` are **B01** (`vec→vec`,
> element-width-typed); the **boolean-typed** `ivp_andb`/`ivp_orb`/`ivp_xorb`/`ivp_notb` (`vbool→vbool`)
> are **B11** — they are *different opcodes with different files*, separated by the `b` suffix and the
> `_64_64_64` value-leaf signature (a flat 64-bit mask, no element width). The within-mask reduce
> `ivp_randb`/`ivp_rorb` ("is every / any lane of **this one** mask set?") is **B08**; B11's
> `ivp_borfs2n`/`ivp_bnorfs2n` reduce **eight separate** `vbool` registers into one FS register (§4.2).
> The `IVP_FS0..FS7` predicate-accumulator file B11 targets is **not** the FCR/FSR arithmetic-control
> state — that 5-flag IEEE sticky block lives in [fp-sub-isa](../core/fp-sub-isa.md) and is a different
> state file ([fp-sub-isa §5.4](../core/fp-sub-isa.md)). The full family-prefix assignment is §7.
> `[HIGH/OBSERVED]`

---

## 1. Batch facts

| Fact | Value | Binary witness |
|---|---|---|
| Mnemonics in batch | **27** (7 logic + 3 b1 + 4 bridge/extract + 2 fold + 11 bridge/scan) | `nm \| rg` roster, §2/§8 |
| Placements in batch | **189** | `Σ` per-mnemonic `Opcode_ivp_*_Slot_*_encode` counts, §8 |
| Predicate file (logic/fold) | `vbool` idx 3, **16 × 64-bit** | [register-files §3](../core/register-files.md) |
| Predicate-state file (fold/bridge dst) | `IVP_FS0..FS7`, **8 × 64-bit**, separate | [fp-sub-isa §5.4](../core/fp-sub-isa.md) |
| Single-bit file (`*b1`) | `BR` idx 1, **16 × 1-bit**, file pkg `xt_booleans`, op pkg `xt_ivp32` | [register-files §3](../core/register-files.md) line 102 |
| Issue slot (boolean logic) | **`f0_s1_ld`** — the **Load** pipe, not the ALU | encode-thunk slot token, §3 |
| Issue slot (`count*`, `movfsv`, `movab1`) | `f0_s3_alu` | §3 |
| Issue slot (`popc2nx8`) | `f1_s2_mul` (the Mul pipe) | §3 |
| vbool operand fields (3-op logic) | `vbt`=dst [16:13], `vbr`=src1 [3:0], `vbs`=src2 [11:8] | `Field_fld_ivp_sem_vbool_alu_ltr_*_get`, §3.3 |
| Predicate width per lane | **1 bit / 8-bit lane** (one `vbool` bit per 8-bit sub-lane of 512b) | [register-files §3](../core/register-files.md) line 162 |
| Pipeline depth (logic, Ld pipe) | reaches **stage 12** (`vbool` write @10/@11) | `libcas-core.so` `IVP_ANDB_inst_stage*` |
| Pipeline depth (`popc`, Mul) | reaches **stage 15** | `libcas-core.so` `IVP_POPC2NX8_inst_stage*` |
| Value leaves driven live | `andb`/`orb`/`xorb`/`notb`/`andnotb`/`ornotb`, `bnorfs`/`borfs`, `popc8`/`popc64`, `extbi`, `joinb`, `notb1`/`andnotb1`/`ornotb1` | §4–§5 |
| Confidence | `[HIGH/OBSERVED]` (encoding + execution) | — |

Every B11 mnemonic is package **`xt_ivp32`** (parsed `opcodes[i].package` at offset `+0x08` for all 27,
this pass — all returned `xt_ivp32`). So the whole batch counts toward the **1065 `ivp_`-prefix vector
axis**, **not** the `xt_booleans` package — that package holds the *base-Xtensa scalar* `BR` ops
(`all4`/`any8`/branch-on-bool), which are owned by **B28**. The `*b1` ops here *use* the `BR` register
file but are themselves `xt_ivp32` Vision-ISA forms, not `xt_booleans`.

> **CORRECTION — the partition row's "`xt_ivp32`+`xt_booleans`" package anchor is half right.** The
> [template §4.1](template-and-partition.md) row for B11 names the package anchor as
> "`xt_ivp32`+`xt_booleans`". Re-parsed this pass, **all 27 B11 mnemonics resolve to package
> `xt_ivp32`** — none are `xt_booleans`. The `xt_booleans` package (the 16×1-bit `BR` file's *own*
> instruction set: scalar `all`/`any`/branch-on-bool) is a **base-Xtensa** package owned by **B28**
> ([template §4.1](template-and-partition.md) B28 row), not by B11. B11 merely *references* the `BR`
> file from three `xt_ivp32` Vision ops (`notb1`/`andnotb1`/`ornotb1`). Pin the package anchor as
> **`xt_ivp32` only** for B11. `[HIGH/OBSERVED]` (package parse of all 27 rows).

---

## 2. Roster

`FLIX format·slot` names the canonical placement used for the selector column (the **Load** slot
`f0_s1_ld` for the boolean-logic group; `f0_s3_alu` / `f1_s2_mul` for the scan/bridge group).
`opcode-sel imm` is `word0` of the representative `Opcode_ivp_<mn>_Slot_<slot>_encode` template
(`C7 07 imm32` — [flix-encoding §6.1](../core/flix-encoding.md)). `files (dir)` names the operand files
the op touches and the dst direction. `[conf]` is `[HIGH/OBSERVED]` for every row unless flagged.

### 2.1 vbool boolean algebra — `vbool→vbool` (7 mnemonics, 56 placements)

| mnemonic | format·slot | opcode-sel `word0` | files (dir) | size | one-line semantics | n |
|---|---|---|---|--:|---|--:|
| `ivp_andb`     | F0·S1_Ld | `0x70104a00` | vbt(o),vbr(i),vbs(i) | 8 B | `vbt = vbr & vbs` (64-bit mask) | 8 |
| `ivp_orb`      | F0·S1_Ld | `0xb0104a00` | vbt(o),vbr(i),vbs(i) | 8 B | `vbt = vbr \| vbs` | 8 |
| `ivp_xorb`     | F0·S1_Ld | `0xd0004a00` | vbt(o),vbr(i),vbs(i) | 8 B | `vbt = vbr ^ vbs` | 8 |
| `ivp_andnotb`  | F0·S1_Ld | `0xa0004a00` | vbt(o),vbr(i),vbs(i) | 8 B | `vbt = vbr & ~vbs` (AND-NOT) | 8 |
| `ivp_ornotb`   | F0·S1_Ld | `0xc0004a00` | vbt(o),vbr(i),vbs(i) | 8 B | `vbt = vbr \| ~vbs` (OR-NOT) | 8 |
| `ivp_notb`     | F0·S1_Ld | `0xe0104a00` | vbt(o),vbr(i)        | 8 B | `vbt = ~vbr` (2-op, no `vbs`) | 9 |
| `ivp_joinb`    | F0·S1_Ld | `0xb0004a00` | vbt(o),vbr(i),vbs(i) | 8 B | interleave/zip two masks (lane re-pack) | 8 |

All seven issue in the **Load slot** (`f0_s1_ld`) and share the low 16 bits `…4a00` — the boolean-logic
iclass selector in the Ld slot. The high byte is the **operator selector** (`0x70`=AND, `0xb0`=OR,
`0xd0`=XOR, `0xa0`=ANDNOT, `0xc0`=ORNOT, `0xe0`=NOT). The value leaves are flat-64-bit
(`andb_64_64_64`, etc.) — **no element width**: the boolean op treats the mask as 64 raw bits, agnostic
to whether the producing compare wrote 1/2/4-bit-per-lane (§4.4).

### 2.2 Single-bit `BR` boolean — `BR→BR` (3 mnemonics, 26 placements)

| mnemonic | format·slot | opcode-sel `word0` | files (dir) | size | one-line semantics | n |
|---|---|---|---|--:|---|--:|
| `ivp_notb1`    | F0·S1_Ld | `0xe0114a00` | BR(o),BR(i)       | 8 B | `b_out = (b_in == 0)` | 9 |
| `ivp_andnotb1` | F0·S1_Ld | `0xa0104a00` | BR(o),BR(i),BR(i) | 8 B | `b_out = a & ~b` (1-bit) | 8 |
| `ivp_ornotb1`  | F0·S1_Ld | `0xc0104a00` | BR(o),BR(i),BR(i) | 8 B | `b_out = a \| ~b` (1-bit) | 8 |

The `1` suffix marks the **single-bit (`BR`) sibling**: same operator, on a 1-bit scalar boolean rather
than a 64-bit lane mask. Value leaves `notb1_1_1`, `andnotb1_1_1_1`, `ornotb1_1_1_1` (the `_1` = 1-bit
width). Their selector words differ from the mask forms only in byte-2 (`0x11`/`0x10` vs `0x10`/`0x00`),
the `BR`-vs-`vbool` operand-class bit. (No plain `andb1`/`orb1`/`xorb1` mnemonic exists — only the
NOT-fused forms ship a `*b1`; plain single-bit AND/OR/XOR are base-Xtensa `xt_booleans`, B28.)

### 2.3 Predicate move / make / extract (4 mnemonics, 35 placements)

| mnemonic | format·slot | opcode-sel `word0` | files (dir) | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_mb`      | F0·S1_Ld | `0xe00f4a00` | vbt(o), imm | make-bool: mask from an immediate / AR pattern | 9 |
| `ivp_movab1`  | F0·S3_ALU| `0x00508164` | AR(o), BR(i) | move a single `BR` bit → an `AR` scalar | 9 |
| `ivp_movba1`  | F0·S1_Ld | `0xe00c4a00` | BR(o), AR(i) | move an `AR` bit → a `BR` boolean | 8 |
| `ivp_ext0ib`  | F0·S1_Ld | `0xd0104a00` | vbt(o),vbr(i),imm7 | extract bit `i` (7-bit index) of a mask | 9 |

`mb`/`movab1`/`movba1` have **no dedicated `module__xdref_*` value leaf** — they are *structural*
moves (bit copies between files) the ISS resolves by composition, so their behaviour is validated by the
objdump round-trip and the field thunks, **not** by `xdref` execution (`[HIGH/OBSERVED]` on encoding,
`[MED/INFERRED]` on value). `ext0ib` *does* have a leaf (`ext0ib_64_64_7`, §4.3) — it extracts bit `i`
(a 7-bit index operand) of the source mask.

### 2.4 Fold-into-FS reduce (2 mnemonics, 4 placements)

| mnemonic | format·slot | opcode-sel `word0` | files (dir) | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_borfs2n`  | F0·S1_Ld | `0xe1184a00` | FS(o), 8×vbool(i) | `FS = vb0 \| vb1 \| … \| vb7` (OR-reduce 8 regs) | 2 |
| `ivp_bnorfs2n` | F0·S1_Ld | `0xe0184a00` | FS(o), 8×vbool(i) | `FS = ~(vb0 \| … \| vb7)` (NOR-reduce) | 2 |

The `2n` suffix is the lane geometry (`2n` = the 64×8b mask shape, 64 lanes over 512 bits). Each op
reads **eight consecutive `vbool` registers** and writes one **`IVP_FS*` predicate-state** register
(only **2** placements each — legal only in the Ld slot of two formats; §6). This is the only B11
family whose *destination* is the FS file rather than `vbool`.

### 2.5 vbool↔FS bridge + predicate scan (11 mnemonics, 68 placements)

| mnemonic | format·slot | opcode-sel `word0` | files (dir) | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_movvfs`  | F0·S1_Ld | `0xac206000` | FS(o,512b) ← 8×vbool(i) | pack 8 `vbool` regs → one FS-state (lane→FS geometry) | 8 |
| `ivp_movfsv`  | F0·S3_ALU| `0x00de9864` | 8×vbool(o) ← FS(i,512b) | unpack one FS-state → 8 `vbool` regs (FS→lane geometry) | 9 |
| `ivp_popc2nx8`| F1·S2_Mul| `0x8020fc02` | AR(o) ← vec(i) | population count: # set bits per 8-bit lane | 3 |
| `ivp_counteq4nx8`  | F0·S3_ALU | `0x00006086` | AR(o) ← vec,vec | count lanes where `a == b` | 8 |
| `ivp_counteqz4nx8` | F0·S3_ALU | `0x00026086` | AR(o) ← vec | count lanes where `a == 0` | 8 |
| `ivp_counteqm4nx8` | (alu) | — | AR(o) ← vec,vec,vbool | count `a==b` **under a mask** | 4 |
| `ivp_counteqmz4nx8`| (alu) | — | AR(o) ← vec,vbool | count `a==0` under a mask | 4 |
| `ivp_countle4nx8`  | F0·S3_ALU | `0x00007086` | AR(o) ← vec,vec | count lanes where `a <= b` | 8 |
| `ivp_countlez4nx8` | F0·S3_ALU | `0x00027086` | AR(o) ← vec | count lanes where `a <= 0` | 8 |
| `ivp_countlem4nx8` | (alu) | — | AR(o) ← vec,vec,vbool | count `a<=b` under a mask | 4 |
| `ivp_countlemz4nx8`| (alu) | — | AR(o) ← vec,vbool | count `a<=0` under a mask | 4 |

> **QUIRK — `popc`/`count*` cross a file boundary: vector in, *scalar* out.** Unlike the boolean-logic
> group (mask→mask), the scan ops read a `vec`/`vbool` and write an **`AR` scalar** (a count, 0..64).
> This is the [register-files §6.4](../core/register-files.md) `vbool→AR` extraction path ("a
> population/index count … the path that produces a scalar `AR` result"). The `m`-infixed forms
> (`counteqm`/`countlem`) additionally read a `vbool` mask to gate which lanes are counted; the `z`
> forms compare against the implicit zero (one fewer `vec` operand). `[HIGH/OBSERVED]`

---

## 3. Encoding

### 3.1 The boolean-logic group lives in the **Load** slot, selected by the high byte

All seven §2.1 logic ops and all three §2.2 `*b1` ops issue in **`f0_s1_ld`** — the **Load** pipe — and
share the low 16 bits **`0x4a00`** (the `vbool`-ALU iclass marker in the Ld slot). The discriminator is
the **high byte of `word0`** (re-read byte-exact from the encode thunks `@0x341…`/`@0x353…`/`@0x361…`
this pass):

```
andb   0x70104a00     andnotb  0xa0004a00     notb   0xe0104a00     joinb   0xb0004a00
orb    0xb0104a00     ornotb   0xc0004a00     mb     0xe00f4a00     ext0ib  0xd0104a00
xorb   0xd0004a00
                      andnotb1 0xa0104a00     notb1  0xe0114a00     ornotb1 0xc0104a00
```

> **GOTCHA — the boolean ops are NOT in the ALU slot; they co-issue with a *load*.** A reimplementer who
> assumes "boolean ALU ⇒ ALU slot" mis-schedules every B11 logic op. They are placed in **`f0_s1_ld`**
> (the LSU's S1 sub-slot), so a `vbool` `andb` co-issues with a vector *load*, not with a vector ALU op.
> This is why B03 calls them "ld-pipe" ops, and why their placement count is **low** (8–9, vs 15–21 for
> a vec-ALU op) — they are legal in far fewer slots. The compiler that wants to overlap predicate
> bookkeeping with data movement gets it for free; one that tries to issue `andb` in the ALU slot finds
> no placement. `[HIGH/OBSERVED]` (slot token in every `Opcode_ivp_<logic>_Slot_f0_s1_ld_encode`).

The byte-2 nibble distinguishes the operand **class**: `0x_0_` for the XOR/ANDNOT/ORNOT/`joinb`/`mb`
forms (byte-2 `0x00`), `0x_1_` for AND/OR/NOT and the `*b1` forms (byte-2 `0x10`/`0x11`). There is **no**
single flat counter — encode by table lookup, exactly as
[flix-encoding §6.2](../core/flix-encoding.md)'s two-tier selector model prescribes.

### 3.2 The fold and bridge ops drift to other selector bands

`borfs`/`bnorfs`/`movvfs` stay in the Ld slot but carry distinct high bytes (`0xe1`/`0xe0` with byte-2
`0x18` for the fold; `0xac20` for `movvfs`). `movfsv`/`movab1` move to **`f0_s3_alu`** (their `word0` is
the ALU-slot template `0x00……64`). `popc2nx8` is in **`f1_s2_mul`** (`0x8020fc02`) — it borrows the
multiply pipe's lane-reduction tree to sum the set bits. The `count*4nx8` ops are ALU-slot
(`0x0000_086` / `0x0002_086` band, the count-iclass). The selector band per op is the ground truth in
§2; do not compute it.

### 3.3 The three `vbool` operand fields — `vbt`/`vbr`/`vbs`, distinct bit-windows

A 3-operand boolean op (`andb vbt, vbr, vbs`) names three `vbool` registers via the
`ivp_sem_vbool_alu_ltr_{vbt,vbr,vbs}` field group. The *get* thunk bodies (re-disassembled this pass)
give the exact bit-windows in the Ld-slot `word0`:

```c
// Field_fld_ivp_sem_vbool_alu_ltr_vbt_Slot_f0_s1_ld_get @ 0x330250   (DESTINATION)
uint32_t vbt_get(uint32_t w0) { return (w0 << 0x0f) >> 0x1c; }   // isolate bits [16:13] -> 4-bit reg
// _set @ 0x330260 :  (w0 & 0xfffe1fff) | ((vbt & 0xf) << 13)

// Field_fld_ivp_sem_vbool_alu_ltr_vbr_Slot_f0_s1_ld_get @ 0x32fe40   (SOURCE 1)
uint32_t vbr_get(uint32_t w0) { return w0 & 0xf; }               // bits [3:0]

// Field_fld_ivp_sem_vbool_alu_ltr_vbs_Slot_f0_s1_ld_get @ 0x330150   (SOURCE 2)
uint32_t vbs_get(uint32_t w0) { return (w0 << 0x14) >> 0x1c; }   // isolate bits [11:8]
```

So the three 4-bit register fields are **`vbt` = dst @[16:13]**, **`vbr` = src1 @[3:0]**, **`vbs` = src2
@[11:8]** — each naming one of the 16 `vbool` registers. The 2-operand `notb`/`mb`/`ext0ib` omit `vbs`.
The fields are deposited *separately* by `field_set`, **not** by the `C7 07 imm32` thunk (which carries
only the opcode selector) — [template §3.2 NOTE](template-and-partition.md). `[HIGH/OBSERVED]`

> **GOTCHA — the dst field `vbt` sits at the *top* of the word ([16:13]), the sources at the bottom.**
> The operand-window order is **not** dst-first in the bitstream: `vbr` (src1) is the low nibble
> `[3:0]`, `vbs` (src2) is `[11:8]`, and the *destination* `vbt` is the highest field `[16:13]`. A
> reimplementer who lays the operands out left-to-right (dst, src1, src2) will deposit the destination
> register into the source nibble and silently compute into the wrong `vbool`. Read the windows from the
> field thunks, never from operand order. `[HIGH/OBSERVED]`

### 3.4 Operand count and direction

The `*_operands` descriptor and `num_args` immediate pin the shape. `andb` is `num_args = 3`
(`o, i, i` — `vbt` out, `vbr`/`vbs` in); `notb` is `num_args = 2` (`o, i`); `borfs`/`bnorfs` are
`num_args = 9` (one FS `o` + eight `vbool` `i`); `popc`/`count*` write an `AR` `o`. The destination is a
plain **`out`** (`0x6f`, 'o') in every B11 op — there is **no `m`/inout merge** here (that is the B03
`*t` family). A B11 boolean op fully overwrites its destination mask; it never reads-modifies-writes.
`[HIGH/OBSERVED]`

---

## 4. Per-lane / per-mask semantics — annotated C

The four required pseudocode shapes — (a) the bitwise predicate logic, (b) the `bnorfs`/`borfs`
reduce-into-FS, (c) the population/scan, (d) the predicate-width semantics — each annotated against the
live-driven `module__xdref_*` leaf.

### 4.1 (a) Bitwise predicate logic — `ivp_andb` / `ivp_orb` / `ivp_xorb` / `ivp_andnotb`

The mask is a 64-bit value the leaf processes as **two 32-bit words** (low `(%rsi)`, high `0x4(%rsi)`).
Disassembled bodies (re-read this pass), unified:

```c
// module__xdref_andb_64_64_64 @ 0x856f80  (ABI: rdi=ctx unused, rsi=a, rdx=b, rcx=out)
void andb_mask(const uint32_t a[2], const uint32_t b[2], uint32_t out[2]) {
    out[0] = a[0] & b[0];            // 856f80: mov (%rsi) ; and (%rdx) ; mov ->(%rcx)
    out[1] = a[1] & b[1];            // 856f86: same on the high word
}
// orb  @0x856f90 : out = a | b      (0x0b: 'or' opcode)
// xorb @0x856fa0 : out = a ^ b      (0x33: 'xor')
// notb @0x856f70 : out = ~a         (2-op, rdx=out;  f7 d0 = 'not')

// module__xdref_andnotb_64_64_64 @ 0x856fb0   — AND with the *complement* of b
void andnotb_mask(const uint32_t a[2], const uint32_t b[2], uint32_t out[2]) {
    out[0] = a[0] & ~b[0];           // 856fb5: not %edx ; and (%rsi)
    out[1] = a[1] & ~b[1];
}
// module__xdref_ornotb_64_64_64 @ 0x814fc0 : out = a | ~b  (identical shape, 'or' instead of 'and')
```

The op is **pure 64-bit bitwise** — no element width, no saturation, no sign. The `andnotb`/`ornotb`
forms invert the *second* source before the bitwise op (`a OP ~b`), the canonical "mask `a` minus the
lanes flagged by `b`" idiom (`andnotb`) and "force-set the lanes `b` does not flag" (`ornotb`).
**Live-validated** (§5.1): `andb(f0f0…,00ff…)=00f0…`, and a 4096-pair differential vs
`a&b`/`a|b`/`a^b`/`a&~b` returned **0 mismatches**.

### 4.2 (b) Fold-into-FS reduce — `ivp_borfs2n` / `ivp_bnorfs2n`

`borfs` is a **horizontal OR across eight `vbool` registers**, written to one FS-state register;
`bnorfs` is the same with a final complement. The body (re-read `@0x8327f0`/`@0x832870`) ORs eight
64-bit masks word-by-word:

```c
// module__xdref_borfs_2n_64_64_64_64_64_64_64_64_64 @ 0x8327f0
//   ABI: rdi=ctx, then 8 mask ptrs (rsi,rdx,rcx,r8,r9 + 3 on stack), then out ptr (stack)
void borfs_fold(const uint32_t *m[8], uint32_t fs_out[2]) {
    uint32_t lo = 0, hi = 0;
    for (int k = 0; k < 8; k++) {        // 832819..83284f: a long chain of 'or' over the 8 masks
        lo |= m[k][0];                   //   low word  of each mask
        hi |= m[k][1];                   //   high word of each mask
    }
    fs_out[0] = lo;  fs_out[1] = hi;     // 832852/832854: store to the FS register
}
// module__xdref_bnorfs_2n_... @ 0x832870 : identical OR-tree, then  fs = ~fs   (8328b8: not %eax/%edx)
```

The semantic is **"collapse eight predicate registers into one FS predicate-state register by lane-wise
OR"** — `FS[bit] = vb0[bit] | vb1[bit] | … | vb7[bit]`. A reimplementer reads this as the
predicate-accumulator update: after a loop that produces up to 8 partial masks (`vb0..vb7`), one
`borfs2n` merges them into the persistent `IVP_FS*` state in a single op, and `bnorfs2n` gives the
complement (the "no register set this lane" mask). **Live-validated** (§5.2): `borfs` of eight
single-bit masks `{1,2,4,8,16,32,64,1<<63}` → `0x800000000000007f` (the bitwise OR), `bnorfs` →
`0x7fffffffffffff80` (its complement); all-zero inputs → `borfs=0`, `bnorfs=0xffff…ffff`.

> **QUIRK — `borfs` reduces *across registers*, not *within a register*.** This is the sharp line vs
> B08's `randb`/`rorb`. B08's `ivp_randb2n` tests whether **one** mask is all-ones
> (`module__xdref_randb2n_64_64 @0x81cc40`: `cmpl $0xffffffff,(%rsi)` → a within-mask "all-set?"
> reduce). B11's `borfs` ORs **eight separate** masks into one FS register — an *inter-register* fold.
> Same word "reduce", orthogonal axis: B08 folds lanes within a mask, B11 folds registers into FS.
> Never merge the two tallies. `[HIGH/OBSERVED]`

### 4.3 (c) Population / match scan — `ivp_popc2nx8`, `ivp_counteq4nx8`, `ivp_ext0ib`

`popc` is per-lane population count; `count*` sums a per-lane predicate to an `AR` scalar; `ext0ib`
extracts one bit. The simplest leaf (`popc_8_8 @0x832df0`) is a textbook 8-bit popcount:

```c
// module__xdref_popc_8_8 @ 0x832df0   (rsi=byte, rdx=out)   — count set bits, saturate to a 4-bit field
uint8_t popc8(uint8_t x) {
    int c = (x & 1);
    for (int s = 1; s <= 7; s++) c += (x >> s) & 1;   // 832df4..832e33: unrolled shr/and/add over bits
    return (uint8_t)(c & 0xf);                          // 832e35: and $0xf  (0..8 fits in 4 bits)
}
// module__xdref_popc64_7_64 @0x8236c0 : same idea over a 64-bit mask -> count 0..64

// module__xdref_ext0ib_64_64_7 @ 0x856dd0   — extract bit `i` (a 7-bit index) of a mask
//   reads the index, masks/shifts to isolate one lane bit, writes it (a 'select one predicate lane' op)
```

`popc` over a mask answers **"how many lanes are set"** — the building block for *any* (`popc != 0`),
*all* (`popc == lanecount`), and density-driven control flow. There is **no dedicated `anyb`/`allb`/
`firstb` mnemonic** in the Q7 ISA (`nm | rg -i 'any|allb|firstb'` over the roster = 0); *any/all/first*
are synthesised from `popc`/`borfs`/`bnorfs` + `ext0ib` + a scalar compare. **Live-validated** (§5.3):
`popc8` over all 256 inputs = **0 mismatches**; `popc64` over 2000 random masks = **0 mismatches**;
`ext0ib` extracts the indexed bit with the correct width scaling.

### 4.4 (d) Predicate-width semantics — one `vbool` bit per 8-bit sub-lane

The boolean-logic ops are **width-agnostic** (flat 64-bit), but the masks they consume were *written*
by a compare at a definite lane width: a `2nx8` compare sets **1 bit/lane**, an `nx16` compare **2
bits/lane**, an `n_2x32` compare **4 bits/lane** ([B03 §5.2](b03-vec-alu-rest.md)). The geometry is
**one `vbool` bit per 8-bit sub-lane of the 512-bit vector** ([register-files §3](../core/register-files.md)
line 162), so a 64-lane `vbool` covers `512/8 = 64` sub-lanes. The `extbi` extract-to-scalar leaf shows
the width scaling directly:

```c
// module__xdref_extbi_1_8_32  : extract a 1-bit-per-lane predicate -> returns 0/1
// module__xdref_extbi_2_16_32 : extract a 2-bit-per-lane predicate -> returns 0/3
```

Driven live, `extbi_1_8_32(mask=0b10110, i)` over `i=0..5` → `[0,1,1,0,1,0]` (the raw bit), while
`extbi_2_16_32` over the same mask → `[0,3,3,0,3,0]` (the bit *replicated to its 2-bit lane field*).
A B11 boolean op operating on a 16-bit-lane mask therefore manipulates **pairs** of bits per lane; an
`andb` over two such masks is still correct bit-for-bit because both operands carry the same 2-bit lane
encoding. The reimplementer's invariant: **the boolean op is bit-parallel; the lane-width contract lives
in the producing compare, not in the boolean op** — so `andb` of an `nx16`-compare mask and a
`2nx8`-compare mask is *encodable but meaningless* (mismatched lane geometries), exactly as a C `&` of
two differently-strided bitsets would be. `[HIGH/OBSERVED]`

---

## 5. Driven live — `libfiss-base.so` value leaves via `ctypes`

`libfiss-base.so` (sha256 `260b110c…`, 864 `module__xdref_*` value leaves) is callable in-process with
no license. The per-element/per-mask leaf ABI is **System-V with `rdi` reserved** (a ctx pointer the
mask leaves ignore): mask args are `uint32_t[2]` pointers, scalar args pass by value, outputs are
pointer args. Six leaf families driven live this pass; the transcripts below reproduce with
`ctypes.CDLL(...)`.

### 5.1 vbool boolean algebra over two 64-bit masks (the required logic case)

```
a       = f0f0f0f0f0f0f0f0
b       = 00ff00ff00ff00ff
andb    = 00f000f000f000f0   (= a & b)
orb     = f0fff0fff0fff0ff   (= a | b)
xorb    = f00ff00ff00ff00f   (= a ^ b)
andnotb = f000f000f000f000   (= a & ~b)
ornotb  = fff0fff0fff0fff0   (= a | ~b)
notb    = 0f0f0f0f0f0f0f0f   (= ~a)

4096-pair differential and/or/xor/andnot vs C model : 0 mismatches
```

Every operator matches its bitwise C model exactly. `[HIGH/OBSERVED by execution]`

### 5.2 Fold-into-FS — `borfs` / `bnorfs` over 8 vbool registers (the required reduce case)

```
masks = [0x…01, 0x…02, 0x…04, 0x…08, 0x…10, 0x…20, 0x…40, 0x8000000000000000]
borfs  = 800000000000007f   (= OR of all 8 masks)           match
bnorfs = 7fffffffffffff80   (= ~OR)                          match
borfs (all-zero masks)  = 0000000000000000   (no register set this lane)
bnorfs(all-zero masks)  = ffffffffffffffff   (every lane: "none of the 8 set it")
```

`borfs` is the inter-register OR-reduce into the FS register; `bnorfs` its complement. The all-zero case
is the FS "nothing matched" sentinel. `[HIGH/OBSERVED by execution]`

### 5.3 Population / extract scan — `popc`, `extbi` (the required scan case)

```
popc8(0x00)=0  popc8(0x0f)=4  popc8(0xff)=8  popc8(0x80)=1  popc8(0xaa)=4
popc8 full 256-input sweep              : 0 mismatches
popc64(0x0)=0  popc64(0xff)=8  popc64(0xffffffffffffffff)=64  popc64(0xaaaa…)=32
popc64 2000-input random sweep          : 0 mismatches
extbi_1_8_32 (mask 0b10110, i=0..5) = [0,1,1,0,1,0]   (raw predicate bit)
extbi_2_16_32(mask 0b10110, i=0..5) = [0,3,3,0,3,0]   (bit replicated into the 2-bit lane field)
```

`popc` answers "how many lanes set"; `extbi` extracts one indexed predicate lane (with lane-width
replication). Together they synthesise *any/all/first-set*, for which the ISA ships **no dedicated
opcode**. `[HIGH/OBSERVED by execution]`

### 5.4 Single-bit `BR` boolean — `notb1` / `andnotb1` / `ornotb1`

```
notb1(0)=1  notb1(1)=0                         (= b == 0)
andnotb1(a,b) over {0,1}² : 0&~0=0  0&~1=0  1&~0=1  1&~1=0     (= a & ~b)
ornotb1(a,b)  over {0,1}² : 0|~0=1  0|~1=0  1|~0=1  1|~1=1     (= a | ~b)
```

The 1-bit `BR`-file forms reproduce their mask siblings on a scalar boolean — `notb1` is "is the bit
clear?", `andnotb1`/`ornotb1` invert `b` then AND/OR. `[HIGH/OBSERVED by execution]`

---

## 6. Slot legality and co-issue

* **Boolean logic** (`andb`/…/`notb`/`joinb`/`*b1`/`mb`/`ext0ib`) carry **8–9** placements each, all in
  the **Load** slot (`f0_s1_ld` + the same sub-slot of a few other formats). They co-issue with a vector
  *load*, not a vector ALU op — the predicate-bookkeeping pipe overlaps data movement
  ([register-files §6.5](../core/register-files.md)).
* **Fold** (`borfs`/`bnorfs`) carry only **2** placements each — legal in the Ld slot of two formats
  only. The 8-register read is expensive; the hardware exposes it in a narrow placement set.
* **Bridge** (`movvfs` 8, `movfsv` 9) split across Ld (`movvfs`) and ALU (`movfsv`).
* **Scan** (`popc` 3 in the **Mul** slot; `count*` 4–8 in the ALU slot) — `popc` borrows the
  multiply pipe's reduction tree, so it reaches **stage 15** (`libcas-core` `IVP_POPC2NX8_inst_stage15`)
  vs the boolean ops' **stage 12** (`IVP_ANDB_inst_stage12`).

> **GOTCHA — a `vbool`/FS written by a B11 fold is read at stage @10, not forwarded within the bundle.**
> Per [register-files §5](../core/register-files.md), `vbool` writes land @10/@11 and reads @10, with a
> **2-port** read limit. A `borfs2n` that consumes eight `vbool` registers saturates the predicate read
> ports for the bundle — two predicate-consuming ops cannot co-issue with it. A downstream op that reads
> the produced FS state must be in a *later* bundle. The `libcas-core` stage models
> (`IVP_BNORFS2N_inst_stage12`, `IVP_MOVVFS_inst_stage11`) confirm the Ld-pipe depth. `[HIGH/OBSERVED]`
> on the stages; intra-bundle forwarding is scheduler policy.

---

## 7. Partition discipline — the exact family-prefix assignment

To guarantee **no mnemonic is double-counted** across the predicate-adjacent batches, the per-prefix
ownership, re-derived against the roster this pass:

| family / prefix | example | owner | why |
|---|---|---|---|
| `vec→vec` bitwise, element-width-typed | `ivp_and2nx8`, `ivp_not2nx8` | **B01** | reads/writes `vec`, has a `2nx8`/`nx16` element-width suffix |
| compare `vec→vbool`, B-variant, `*t` merge | `ivp_eqnx16`, `ivp_bmaxnx16`, `ivp_addnx16t` | **B03** | produces/consumes `vbool` around an arithmetic op |
| within-mask reduce (`all`/`any` of **one** mask) | `ivp_randb2n`, `ivp_rorbn`, `ivp_rorbn_2` | **B08** | `_64_64` single-mask horizontal reduce |
| **vbool boolean algebra** `andb`/`orb`/`xorb`/`notb`/`andnotb`/`ornotb`/`joinb` | `ivp_andb` | **B11** | `vbool→vbool`, `_64_64_64` flat-mask leaf, Ld slot |
| **single-bit `BR`** `notb1`/`andnotb1`/`ornotb1` | `ivp_notb1` | **B11** | `xt_ivp32` op on the `BR` file, `_1_1` leaf |
| **predicate move/make/extract** `mb`/`movab1`/`movba1`/`ext0ib` | `ivp_mb` | **B11** | `AR`↔`vbool`↔`BR` structural bridge |
| **fold-into-FS** `borfs2n`/`bnorfs2n` | `ivp_borfs2n` | **B11** | 8×`vbool` → FS-state OR/NOR-reduce |
| **vbool↔FS bridge + scan** `movvfs`/`movfsv`/`popc2nx8`/`count*4nx8` | `ivp_movvfs` | **B11** | 512-bit lane↔FS permute; `vec/vbool→AR` count |
| FS-producing compare `fsNltu2nx8` | `ivp_fs3ltu2nx8` | **fp-sub-isa / B03-sibling** | `vec` compare → FS slot (a *producer*, like B03) |
| FCR/FSR arith-control state `wur.fcr`/`rur.fsr` | `wur.fcr` | **fp-sub-isa (B24)** | IEEE rounding/sticky, **not** the `IVP_FS*` predicate file |
| select/merge by mask `sel`/`dsel`/shuffle | `ivp_selnx16` | **B21** | 3-input mux on `vec` data |
| scatter/gather `*t` | `ivp_scatternx16t` | **B19** | memory, `b32_pr` mask |

The arithmetic that makes the partition airtight: B11's boolean group is the **`b`-suffixed,
`vbool`-typed** complement of B01's `2nx8`/`nx16` element-typed bitwise ops — *different opcodes,
different files, different leaves* (`andb_64_64_64` vs `and_2nx8`). B08's `randb`/`rorb` are
*within-mask* reduces (`_64_64`, one mask); B11's `borfs`/`bnorfs` are *inter-register* folds
(`_64×8→FS`). The eight `fsNltu2nx8` ops are **FS-state-producing compares** — structurally a B03/fp
*producer* (`module__xdref_fsNltu_64_8_8` is a thin wrapper over `ltu_1_8_8`), so they are documented at
the boundary but **not** counted in B11's 27. `[HIGH/OBSERVED]`

> **NOTE — why `fsNltu2nx8` is a boundary case, not a B11 member.** `ivp_fs0ltu2nx8 … fs7ltu2nx8`
> (8 mnemonics, 5 placements each) *write* the `IVP_FS*` predicate file from an unsigned `vec` compare —
> `module__xdref_fs0ltu_64_8_8 @0x8328d0` is literally `call ltu_1_8_8; store into FS[0]`. They are the
> **producers** into the FS file that `movfsv` later unpacks; they are siblings of B03's `vec→vbool`
> compares (just with an FS destination and a fixed slot index). B11 owns the *consumers* of `vbool`/FS,
> so these belong with the compare/classify producers ([fp-sub-isa §5.4](../core/fp-sub-isa.md) /
> [B03](b03-vec-alu-rest.md)). They are excluded from the §8 tally to avoid double-counting a producer
> the compare batches already own. `[HIGH/OBSERVED]`

---

## 8. Batch tally vs `nm`

| sub-family | mnemonics | placements | leaf prefix (libfiss) |
|---|--:|--:|---|
| vbool boolean algebra | 7 | 56 | `andb/orb/xorb/notb/andnotb/ornotb/joinb_64_64_64` |
| single-bit `BR` boolean | 3 | 26 | `notb1/andnotb1/ornotb1_1_1` |
| predicate move/make/extract | 4 | 35 | `ext0ib_64_64_7` (rest: structural, no leaf) |
| fold-into-FS reduce | 2 | 4 | `borfs/bnorfs_2n_64×9` |
| vbool↔FS bridge + scan | 11 | 68 | `movvfs/movfsv_512_64×8`, `popc{8,16,32,64}`, `count*` (composed) |
| **B11 total** | **27** | **189** | — |

Re-grounded this pass: `Σ` placements over the 27 mnemonics = **189** (`nm libisa-core.so | rg -c
'Opcode_ivp_<mn>_Slot_.*_encode'` summed; the per-mnemonic counts in §2). The 27 mnemonics are a strict
subset of the **1065** `ivp_`-prefix vector ops and of package **`xt_ivp32`** (all 27 parsed `xt_ivp32`
this pass). Together with B01/B02/B03 (the `vec`/compare/merge ALU) and B08 (within-mask reduce) they
cover the predicate datapath without overlap (§7). No B11 mnemonic appears in any other batch's prefix
set. The 189 placements are part of — never additive beyond — the certified-perfect **12569**
([coverage-tally](../core/coverage-tally.md)); the valid pairing stays **`1534 ↔ 12569`**, never
`12642`.

**Value-leaf contribution.** B11 resolves to **≈14 distinct `module__xdref_*` leaves** (7 logic + 3 b1
+ `ext0ib` + `borfs`/`bnorfs` + `popc{8,16,32,64}` + `movvfs`/`movfsv`), of which the 6 logic-family,
the 2 fold, the 2 popc widths driven, the 3 b1, and `extbi` were **driven live to 0 mismatches** this
pass; `mb`/`movab1`/`movba1`/`count*` are structural/composed (no dedicated leaf — validated by the
objdump round-trip, not `xdref` execution). All counted in the 864 leaf denominator
([coverage-tally §5](../core/coverage-tally.md)).

---

## 9. Adversarial self-verification — five strongest claims re-challenged

1. **"The boolean ops are `vbool→vbool` flat-64-bit, issued in the *Load* slot, distinct from the
   `vec→vec` bitwise B01 ops."** Re-challenged two ways: (a) the value leaves are `andb_64_64_64`
   (flat 64-bit, no element-width token) vs B01's `and_2nx8` (element-typed) — *different leaves*;
   (b) every `Opcode_ivp_andb_Slot_*_encode` symbol carries the slot token **`f0_s1_ld`** (the Load
   sub-slot), confirmed by the `word0` low bytes `0x4a00`, and the `libcas-core` model
   `F0_F0_S1_Ld_16_inst_IVP_ANDB_issue`. A reimplementer scheduling `andb` in the ALU slot finds no
   placement. **Holds.** `[HIGH/OBSERVED]`
2. **"`borfs`/`bnorfs` OR/NOR-reduce *eight* `vbool` registers into one FS register, not a within-mask
   reduce."** Re-challenged by driving the leaf live: `borfs` of eight distinct single-bit masks →
   `0x800000000000007f` (the bitwise OR of all eight), `bnorfs` → its complement; all-zero inputs →
   `0`/`0xffff…ffff`. The disasm shows a chain of 8 `or` ops over 8 pointer args then a store. Contrast
   B08's `randb2n` (`cmpl $0xffffffff,(%rsi)` — a *within-one-mask* all-set test). The two reduces are
   on orthogonal axes. **Holds.** `[HIGH/OBSERVED by execution]`
3. **"`popc`/`count*` cross a file boundary: `vec`/`vbool` in, `AR` scalar out — and there is no
   dedicated `any`/`all`/`first-set` opcode."** Re-challenged: `popc8` over all 256 inputs and `popc64`
   over 2000 random masks both returned **0 mismatches** against `bin(x).count('1')`; the destination is
   an `AR` register ([register-files §6.4](../core/register-files.md) `vbool→AR` path). `nm | rg -i
   'any|allb|firstb'` over the roster = **0** — *any/all/first* are synthesised from
   `popc`/`borfs`/`ext0ib` + a scalar compare, not shipped as opcodes. **Holds.** `[HIGH/OBSERVED]`
4. **"The three `vbool` operand fields sit at non-adjacent windows: `vbt`(dst)@[16:13], `vbr`(src1)@[3:0],
   `vbs`(src2)@[11:8] — dst is *not* first in the bitstream."** Re-challenged by disassembling the three
   `Field_fld_ivp_sem_vbool_alu_ltr_*_Slot_f0_s1_ld_get` thunks: `vbt` = `(w0<<0xf)>>0x1c` (bits
   [16:13]), `vbr` = `w0 & 0xf` (bits [3:0]), `vbs` = `(w0<<0x14)>>0x1c` (bits [11:8]). A left-to-right
   (dst,src1,src2) layout would deposit the destination into the `vbr` source nibble. The windows are
   read from the thunks, not operand order. **Holds (and corrects the naive layout).** `[HIGH/OBSERVED]`
5. **"Every B11 mnemonic is package `xt_ivp32`, not `xt_booleans` — the partition row's package anchor
   is half-wrong."** Re-challenged by parsing `opcodes[i].package` (`+0x08`) for all 27 B11 rows
   directly from the `.data.rel.ro` table (file = VMA − `0x200000`): **all 27 return `xt_ivp32`**;
   `andb`/`notb1`/`movvfs`/`borfs2n`/`popc2nx8`/`counteq4nx8` all parsed `xt_ivp32`. The `xt_booleans`
   package is the *base-Xtensa* `BR` instruction set (B28), which the `*b1` ops only *reference*. The
   template's "`xt_ivp32`+`xt_booleans`" anchor is corrected to `xt_ivp32` only (§1 CORRECTION).
   **Holds.** `[HIGH/OBSERVED]`

**Ungrounded / flagged items.** (i) `ivp_mb`/`ivp_movab1`/`ivp_movba1`/`ivp_count*4nx8` have **no
dedicated `module__xdref_*` value leaf** — their value semantics are `[MED/INFERRED]` (composed from
primitives; validated by encoding + the objdump round-trip, not by `xdref` execution). The `count*`
*encoding* and slot are `[HIGH/OBSERVED]`; the exact saturation of the count output to its `AR` field
width is `[MED/INFERRED]`. (ii) `ivp_joinb`'s precise lane-interleave map was read from the disasm
(`shr/and 0x2,0x4,0x8…` bit-spread) and spot-driven, but the full 64-bit permutation table was **not**
exhaustively swept — `[MED/OBSERVED]` on the exact permutation. (iii) The eight `fsNltu2nx8` ops are
*producers* into the FS file (a B03/fp boundary, §7 NOTE) and are deliberately **excluded** from the 27
to avoid double-counting; their semantics are validated (`fsNltu_64_8_8 = ltu_1_8_8` wrapper) but they
are tallied by the compare batches, not here. (iv) `popc16_5_16`/`popc32_6_32` leaves exist in
`libfiss-base` (the width family of `popc`) even though only the `popc2nx8` mnemonic ships — the
multi-width leaves serve the one mnemonic's per-lane element width; counted once.

---

## 10. Cross-references

* [The Eight Register Files](../core/register-files.md) — `vbool` idx3 (16×64-bit, the mask file), `BR`
  idx1 (16×1-bit, the `*b1` file), the `vbool`→`AR` extraction path (§6.4) the `popc`/`count*` ops use,
  and the `vbool` write@10/@11 / read@10 stages that bound co-issue (§6).
* [The FP Sub-ISA (FCR/FSR, RNE/RZ)](../core/fp-sub-isa.md) — the `IVP_FS0..FS7` predicate-accumulator
  file (§5.4) that `borfs`/`bnorfs`/`movvfs`/`movfsv` target, kept **separate** from the FCR/FSR
  arithmetic-control state (the boundary §1/§7 enforce).
* [FLIX VLIW Encoding](../core/flix-encoding.md) — the 14 formats / 46 slots, the `C7 07 imm32`
  encode-thunk ABI (§6.1), and the two-tier high-byte selector model (§6.2) this batch instantiates in
  the Load slot.
* [ISA Coverage & the 1534/12642 Tally](../core/coverage-tally.md) — the certified-perfect **12569**
  placement denominator this batch's 189 are part of, the no-cross-pair law, the 864 value leaves.
* [B01 — Vector ALU int core](b01-vec-alu-int.md) (the `vec→vec` bitwise `and2nx8`/`or2nx8`) ·
  [B03 — Vector ALU rest](b03-vec-alu-rest.md) (the `vec→vbool` compare *producers* that feed B11) ·
  [B08 — Cross-Lane Reduce](b08-reduce.md) (the *within-mask* `randb`/`rorb` reduce, disjoint from
  B11's *inter-register* `borfs`/`bnorfs`) ·
  [B21 — Select / Shuffle](b21-select-shuffle.md) (predicated data-lane `sel`/`dsel`) ·
  [B19 — Scatter / Gather](b19-scatter-gather.md) (`b32_pr` mask path) — the predicate-adjacent batches
  B11 is disjoint from.
* [ISA Reference Template & 30-Batch Partition](template-and-partition.md) — the canonical B01–B30
  schema (§3), the partition (§4) whose B11 package anchor §1 here corrects, and the §6 roll-up B11's
  27/189 close onto.
* [The libisa Table Schema & Codec ABI](../core/libisa-table-schema.md) — the `opcodes[]` /
  `opcodedefs[]` / `_operands` struct layouts and the field-thunk ABI the §3 extraction reads.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags and the free in-process
  value lane (`libfiss-base`) that makes §4–§5 `OBSERVED by execution`.

---

*Provenance: selector templates, `num_args`/`_operands` direction bytes, the
`ivp_sem_vbool_alu_ltr_{vbt,vbr,vbs}` field thunks, and the package parse of all 27 `opcodes[]` rows are
re-disassembled/re-read from `libisa-core.so` (sha256 `8fe68bf4…`, `ncore2gp/config/`, not stripped);
the value leaves in §4–§5 (`andb`/`orb`/`xorb`/`notb`/`andnotb`/`ornotb`, `borfs`/`bnorfs`,
`popc8`/`popc64`, `extbi`, `notb1`/`andnotb1`/`ornotb1`, `joinb`) are driven live via `ctypes` against
`libfiss-base.so` (sha256 `260b110c…`), license-free; pipeline stages from `libcas-core.so` (DWARF).
Counts via `nm | rg -c`; `.data.rel.ro` file = VMA − `0x200000` (confirmed `readelf -SW`: `.data.rel.ro`
VMA `0x67bb00` ↔ file `0x47bb00`); the `extracted/` tree is gitignored (reached with absolute paths).
All prose is derived from static analysis and in-process execution of the shipped artifacts only;
nothing here is read from a vendor source tree.*
