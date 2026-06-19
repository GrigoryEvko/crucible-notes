# ISA Batch 26 — base-Xtensa Control-Transfer + Memory + MUL32 + Scalar Divide

This batch is the **scalar control plane** of the Vision-Q7 *Cairo* (`ncore2gp`) core — the
24-bit/16-bit base-Xtensa instructions that decode on the **AR (address-register) file**, *not* the
512-bit IVP vector datapath that [B01–B24](template-and-partition.md) document. Everything here is the
machinery the device firmware and the custom-op dispatcher run *between* vector bundles: loads and
stores that move data in/out of local memory, the full conditional-branch tree, the jump/call/return
family (including the **windowed `callx8`** the custom-op ABI dispatches through), the 16-bit `.N`
code-density sub-ISA, the 32×32 integer multiplier, and the iterative scalar integer divider. It owns
**60 opcodes** across six functional clusters:

* **9 core load/store** — `L32I`/`L16UI`/`L16SI`/`L8UI`/`L32R`/`CONST16` + `S32I`/`S16I`/`S8I`: the
  AR-file ↔ local-memory datapath and the PC-relative literal-pool load.
* **24 conditional branches** — three iclasses: 12 reg-reg (`bst8`), 8 reg-immediate (`bsi8`), 4
  reg-vs-zero (`bsz12`). The condition lives in the `r`-nibble; inverse pairs differ by one bit.
* **8 jump/call/return** — `J`/`JX`/`CALL0`/`CALLX0`/`CALL8`/`CALLX8`/`RET`/`RETW`. This config
  ships the **CALL0 (no-window) and CALL8 (+8 window-rotation) ABIs only** — there is no
  `call4`/`call12` in `ncore2gp`.
* **10 `.N` density forms** — `L32I.N`/`S32I.N`/`ADD.N`/`ADDI.N`/`MOV.N`/`MOVI.N`/`BEQZ.N`/
  `BNEZ.N`/`RET.N`/`RETW.N`: 16-bit re-encodings the assembler narrows to automatically.
* **5 MUL32** — `MULL`/`MULUH`/`MULSH`/`MUL16S`/`MUL16U`: low-32 / high-32 / 16² products.
* **4 scalar divide** — `QUOS`/`QUOU`/`REMS`/`REMU`: the only B26 group that raises a *true*
  architectural exception (`IntegerDivideByZeroException`) and serializes via the `XTSYNC` token.

These are **scalar base-Xtensa ops**: they do **not** carry `STATE_IN=CPENABLE` and do **not** raise
`Coprocessor1Exception` — that gate is IVP-vector-only ([B01–B24](template-and-partition.md)). The
`.N` density arith forms (`ADD.N`/`ADDI.N`/`MOV.N`/`MOVI.N`) live **here** (package `xt_density`),
*not* in the 24-bit arith batch [B25](b25-xt-core.md). `NOP.N`/`BREAK.N`/`HALT.N`/`ILL.N` stay with
their misc/debug groups in [B27 (system)](b27-xt-system.md) and B28/B29; `RET.N`/`RETW.N` *are*
density control-transfer and *are* counted here.

Everything below is re-grounded against the shipped binaries **this pass**: the **encoding** from
`libisa-core.so` (`Opcode_<mnem>_Slot_<slot>_encode` thunks read byte-for-byte, the selector CONST
read out of each thunk's `movl $IMM,(%rdi)`), the **issue timing** from the per-op
`*_inst_<MNEM>_issue` scoreboard bodies in `libcas-core.so`, and a byte-exact **encode/decode
oracle** from the device-native `xtensa-elf-as` / `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
`XTENSA_SYSTEM=…/XtensaTools/config`). Every representative below was round-tripped through that
oracle this pass. Confidence tags: `[HIGH/OBSERVED]` = read-from-byte / proven by the device
oracle; `[MED/INFERRED]` = reasoned over OBSERVED; `[…/CARRIED]` = re-used at a sibling page's
confidence.

> **NOTE — address arithmetic re-confirmed this pass.** `libisa-core.so` (9 690 712 B, ET_DYN
> x86-64, not stripped). `readelf -SW` this pass: `.text` (VMA `0x312c10` ↔ file `0x312c10`) and
> `.rodata` (VMA `0x3b6e40` ↔ file `0x3b6e40`) are **VMA == file-offset**; `.data` (VMA `0x764040`
> ↔ file `0x564040`) and `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) carry the per-binary
> delta **`0x200000`** — **not** the `0x400000` of the libtpu family. Encode thunks live in `.text`
> (VMA == file-offset, so a thunk `objdump`s at its symbol VMA directly). All config DLLs are under
> `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/` (gitignored; reach with `--no-ignore`
> or absolute paths).

---

## 0. Scope, roster, and disjointness

The 60 mnemonics are **all present** in `libisa-core.so` as encode thunks — verified by a
`nm | rg -c "Opcode_<mnem>(_w15)?_Slot_"` presence sweep this pass (**0 missing of 60**). The
subtotals close exactly:

| cluster        | count | members |
|----------------|------:|---------|
| core ld/st     | **9** | `L32I` `L16UI` `L16SI` `L8UI` `L32R` `CONST16` `S32I` `S16I` `S8I` |
| branch         | **24**| 12 `bst8` + 8 `bsi8` + 4 `bsz12` (full list §4) |
| jump/call      | **8** | `J` `JX` `CALL0` `CALLX0` `CALL8` `CALLX8` `RET` `RETW` |
| density `.N`   | **10**| `L32I.N` `S32I.N` `ADD.N` `ADDI.N` `MOV.N` `MOVI.N` `BEQZ.N` `BNEZ.N` `RET.N` `RETW.N` |
| MUL32          | **5** | `MULL` `MULUH` `MULSH` `MUL16S` `MUL16U` |
| scalar divide  | **4** | `QUOS` `QUOU` `REMS` `REMU` |
| **total**      | **60**| 9+24+8+10+5+4 = **60** |

`[HIGH/OBSERVED]` Most groups are **semantic singletons** (group name == mnemonic). The only two
genuine multi-member base groups are `xt_mul32` `{MULL,MULUH,MULSH,MUL16S,MUL16U}` and `xt_sem_div`
`{QUOS,QUOU,REMS,REMU}` — they share the same `MUL32/DIV` RRR slot, differing only in the `t`-nibble
selector (§7–§8). Zero cross-membership with [B25 (arith/logic/shift)](b25-xt-core.md),
[B27 (system/SR/regwindow)](b27-xt-system.md), or B28 (exc/dispatch/booleans).

### Encoding conventions (slot-local bit positions)

The base-Xtensa AR-file operand fields are the standard RRR / RRI8 / BRI8 / CALL layouts. In the
**standalone 24-bit form** (objdump's `Slot_inst` form — what `xtensa-elf-as` emits with no FLIX
bundle siblings, and what the chip's instruction fetch resolves when un-bundled), the little-endian
24-bit value packs:

```
                  byte0          byte1          byte2
        ┌──────────────┬──────────────┬──────────────┐
  bits  │ 7 .. 4│ 3..0 │15..12│11.. 8│23..20│19..16    (RRR)
        │   t   │ op0  │  r   │  s   │ op2  │  op1
        └──────────────┴──────────────┴──────────────┘
   op0 = [3:0]   t = [7:4]   s = [11:8]   r = [15:12]   op1 = [19:16]   op2 = [23:20]
```

`[HIGH/OBSERVED]` This was VERIFIED by distinct-register round-trips this pass (e.g. `mull a5,a6,a7`
§7 isolates every field). **Objdump prints the 24-bit instruction as a single MSB-first hex literal**
(e.g. `822340`), which is the reverse of memory byte order (`82 23 40`). Throughout this page the
"`→ XXYYZZ`" notation is the objdump literal; the per-field decode always reads the underlying
**little-endian** bytes.

> **CORRECTION — branch `op0` is `0x7`, not the displacement-contaminated low nibble.** An earlier
> worked-byte table read the reg-reg branch `op0` as `0x2` from a particular forward-label distance,
> where the 8-bit signed displacement byte happened to dress up `byte0`. Re-grounded this pass with a
> **zero-displacement** branch (`beq a3,a4,.L` with `.L` immediately after): raw memory bytes
> `47 13 ff` → `byte0=0x47` → **`op0=0x7`** (the standard Xtensa `BRI8` conditional-branch major),
> `t=0x4` (`a4`, src2), `s=0x3` (`a3`, src1), `r=0x1` (`BEQ` condition), displacement byte `0xff` in
> `byte2`. The *condition nibbles* in §4 are unaffected and correct; only the major-op claim is
> corrected to `op0=0x7`.

---

## 1. Core load — `L32I` / `L16UI` / `L16SI` / `L8UI` / `L32R`

`[HIGH/OBSERVED]` AR-file scalar load from local data memory (or, for `L32R`, the PC-relative
literal pool). `vAddr = ars + (imm << scale)`; read `w` bits; zero-/sign-extend into `art`. Each is
its own semantic singleton. These ops expose the `gen_sig` state-out tokens `ScalarMemAccess` +
`Load` and the mem-pipe pseudo-operands (`VAddrBase`/`VAddrOffset`/`MemControl`/`ScalarMemDataIn32`/
`MemDataIs{8,16,32}Bits`); `L8UI`/`L16*` also expose `VAddrRes`, the sub-word residue mux. **No
`CPENABLE`.**

| op | iclass | scale | extend | offset range | issue (`libcas-core`) |
|----|--------|------:|--------|--------------|-----------------------|
| `L32I`  | `xt_iclass_l32i`  | `<<2` | aligned 32-bit | `{0,4,…,1020}` | `x24_Inst_0_inst_L32I_issue` |
| `L16UI` | `xt_iclass_l16ui` | `<<1` | zero-ext 16   | `{0,2,…,510}`  | `…_inst_L16UI_issue @0x1677510` |
| `L16SI` | `xt_iclass_l16si` | `<<1` | sign-ext 16   | `{0,2,…,510}`  | `…_inst_L16SI_issue` |
| `L8UI`  | `xt_iclass_l8i`   | `<<0` | zero-ext 8    | `{0..255}`     | `…_inst_L8UI_issue` |
| `L32R`  | `xt_iclass_l32r`  | `<<2` | literal-pool  | neg PC-rel ×4  | `…_inst_L32R_issue @0x1677570` |

**Semantics** (assembler operand order `art, ars, offset`; `L32R`: `art, label`):

```c
/* xt_iclass_l32i / l16ui / l16si / l8i — AR scalar load */
void L32I (uint5 t,s, uint8 imm8) { AR[t] = mem32[ AR[s] + (imm8<<2) ]; }       /* aligned word   */
void L16UI(uint5 t,s, uint8 imm8) { AR[t] = (uint32)mem16[ AR[s] + (imm8<<1) ];}/* zero-ext       */
void L16SI(uint5 t,s, uint8 imm8) { AR[t] = (int32)(int16)mem16[ AR[s]+(imm8<<1) ]; } /* sign-ext  */
void L8UI (uint5 t,s, uint8 imm8) { AR[t] = (uint32)mem8 [ AR[s] + imm8 ]; }     /* zero-ext byte  */

/* xt_iclass_l32r — negative PC-relative literal-pool word load                          */
/* uimm16 is the 1's-complement word distance; the chip ORs the high 16 ones to form a    */
/* NEGATIVE offset, then base = the PC of the *next* instruction, word-aligned.            */
void L32R (uint5 t, uint16 uimm16) {
    uint32 base  = (NextPC & ~3u);
    int32  off   = (int32)(0xFFFF0000u | uimm16) << 2;   /* always negative */
    AR[t] = mem32[ base + off ];
}
```

`[HIGH/OBSERVED]` **Round-trip** (`--no-transform`, so the assembler keeps the wide form):

```
l32i  a3, a4, 252  → 3f2432      l16ui a3, a4, 4 → 021432      l16si a3, a4, 4 → 029432
l8ui  a3, a4, 1    → 010432
l32r  a3, .Llit    → 000031      (uimm16 fills [23:8]; reloc resolves to the preceding literal)
```

Without `--no-transform` the density encoder **narrows** `l32i a3,a4,8` to `l32i.n` (`2438`, §6)
when the offset fits 4 bits ×4. `L32I` carries **19** slot placements in `libisa-core.so` (all FLIX
formats + the `Ld`-slot copies + `N0/N1/N2`); `L32R` is **inst-slot only** (1 placement — the
literal-pool form has no FLIX copy). `[HIGH/OBSERVED]`

**Timing** (`INSTR_SCHEDULE` USE/DEF, Tdoc `rstage0/estage3/mstage4/wstage6`): address USE `ars@1`,
`MemControl`/`VAddrBase@1`; data DEF `art@5`, `ScalarMemDataIn32` USE `@5`. → **load-use latency ≈2
cycles** relative to the `estage-3/mstage-4` base pipe (load data lands the cycle after M). `L32R`
has identical literal-load latency (`art@5`). `[HIGH/OBSERVED]`

---

## 2. Literal-immediate build — `CONST16`

`[HIGH/OBSERVED]` Two-instruction 32-bit constant build *without a literal-pool round-trip*.
`CONST16` shifts `art` left 16 and ORs in a 16-bit immediate; a back-to-back pair (high half, then
low half) materialises a full 32-bit constant. Group = singleton `CONST16`, iclass
`xt_iclass_const16`. `art` is the implicit INOUT shift-OR accumulator; `imm16` is the only explicit
`ARG_IN`. **No mem access, no `CPENABLE`.** Issue: `x24_Inst_0_inst_CONST16_issue @0x1677450`; **12**
placements.

```c
/* xt_iclass_const16 — high/low constant build */
void CONST16(uint5 t, uint16 imm16) { AR[t] = (AR[t] << 16) | imm16; }
/*  const16 a3, HI ; const16 a3, LO   =>   AR[3] = (HI<<16) | LO  */
```

`[HIGH/OBSERVED]` Round-trip: `const16 a3, 0x1234 → 123434` (the `imm16` fills the narrow field,
`t=a3`). Timing `CONST16_schedule` — DEF `art@4`, USE `art@3`,`imm16@3` (single base-pipe cycle,
result in the `wstage` region).

---

## 3. Core store — `S32I` / `S16I` / `S8I`

`[HIGH/OBSERVED]` AR-file scalar store to local data memory. `vAddr = ars + (imm << scale)`; write
the low `w` bits of `art`. Singletons `S32I`/`S16I`/`S8I`, **11** placements each. State-out
`ScalarMemAccess` + `Store`; mem-pipe OUT pseudo-operands `VAddrBase`/`VAddrOffset`/
`ScalarMemDataOut32`/`MemDataIs{8,16,32}Bits`/`MemControl`. **No `CPENABLE`.** Assembler order
`art(data), ars(base), offset`.

```c
void S32I(uint5 t,s, uint8 imm8) { mem32[ AR[s] + (imm8<<2) ] = AR[t]; }
void S16I(uint5 t,s, uint8 imm8) { mem16[ AR[s] + (imm8<<1) ] = AR[t] & 0xFFFF; }
void S8I (uint5 t,s, uint8 imm8) { mem8 [ AR[s] +  imm8     ] = AR[t] & 0xFF;   }
```

`[HIGH/OBSERVED]` Round-trip: `s32i a3,a4,252 → 3f6432`, `s16i a3,a4,4 → 025432`,
`s8i a3,a4,1 → 014432`. (`s32i a3,a4,8` narrows to `s32i.n = 2439`.) In the standalone form the
store and load opcodes interleave in the same `LdSt` class: the `F0` 4-LSB subcode `0xbc..0xbe` =
`S16I/S32I/S8I`, `0xb8..0xbb` = the loads — a dense op subcode within the `LdSt` class.

**Timing** (`S32I_xt_sched_st`, issue `…_inst_S16I_issue @0x1677680`): address USE `ars@1`,
`VAddrBase@1`; store **data** USE `art@5`, DEF `ScalarMemDataOut32@5`. → a value produced by a
2-cycle producer can feed a store with **no stall** (data consumed late, `@5`). `[HIGH/OBSERVED]`

---

## 4. Conditional branch — 24 ops

`[HIGH/OBSERVED]` Compare two AR values (reg-reg), an AR vs a small immediate, or an AR vs zero, and
branch ±range to a PC-relative label. Each mnemonic is a semantic singleton; they share **three
iclasses**:

* `xt_iclass_bst8` — reg-reg, 8-bit signed label, **op0 = `0x7`** (12 members).
* `xt_iclass_bsi8b` / `…_b8` — reg-immediate, 8-bit label (8 members).
* `xt_iclass_bsz12` — reg-vs-zero, 12-bit label (4 members).

No data OUT to AR (pure control transfer); OUT tokens `BranchTarget`/`BranchTaken`
(`BBCI`/`BBSI` also carry the immediate bit-index); **no `CPENABLE`.** Each branch additionally
ships as a `_w15` FLIX-bundle variant in `libisa-core.so` (e.g. `Opcode_beq_w15_Slot_f0_s0_ldst_encode`)
— the wide-immediate forms that fit a FLIX `LdSt`/`Ld` slot so a branch can co-issue in a bundle; the
standalone fetch path uses the `_inst` form documented here. `[HIGH/OBSERVED]`

### 4a. Reg-reg (`bst8`) — condition is the `r`-nibble `[15:12]`

`op0=0x7`, `t=[7:4]` = second source AR, `s=[11:8]` = first source AR, signed 8-bit label in
`[23:16]`. The **condition selector is `r=[15:12]`**, verified this pass by a 12-way round-trip:

| mnem  | `r` | semantics (`label = PC + 4 + sext(label8)`)   |
|-------|----:|-----------------------------------------------|
| `BNONE` | `0` | `if (ars & art) == 0`                        |
| `BEQ`   | `1` | `if ars == art`                             |
| `BLT`   | `2` | `if ars <  art` (signed)                     |
| `BLTU`  | `3` | `if ars <  art` (unsigned)                   |
| `BALL`  | `4` | `if (~ars & art) == 0` (all `art`-bits set in `ars`) |
| `BBC`   | `5` | `if bit(ars, art[4:0]) == 0`                 |
| `BANY`  | `8` | `if (ars & art) != 0`                        |
| `BNE`   | `9` | `if ars != art`                             |
| `BGE`   | `a` | `if ars >= art` (signed)                     |
| `BGEU`  | `b` | `if ars >= art` (unsigned)                   |
| `BNALL` | `c` | `if (~ars & art) != 0`                       |
| `BBS`   | `d` | `if bit(ars, art[4:0]) == 1`                 |

`[HIGH/OBSERVED]` The pairs `BNONE/BANY`, `BEQ/BNE`, `BLT/BGE`, `BLTU/BGEU`, `BALL/BNALL`, `BBC/BBS`
are **inverse-pairs differing only in `r`-bit[15]** (the negate bit: `+0x8`). Round-trip (label
`.L1`; objdump literals — displacement varies per instruction, the `r`-nibble is the invariant):

```
beq a3,a4,.L1 → 441347   bne → 419347   blt → 3e2347   bltu → 3b3347
bge → 38a347   bgeu → 35b347   bany → 328347   bnone → 2f0347
ball → 2c4347  bnall → 29c347  bbc → 265347   bbs → 23d347
```

```c
/* xt_iclass_bst8 — reg-reg conditional branch (op0=0x7), per r-nibble */
bool bst8_cond(uint4 r, uint32 ars, uint32 art) {
    bool neg = r & 0x8; uint4 base = r & 0x7;
    bool c;
    switch (base) {
      case 0: c = (ars & art) != 0;                    break; /* BANY  / !BNONE */
      case 1: c = (ars == art);                        break; /* BEQ   / BNE    */
      case 2: c = ((int32)ars <  (int32)art);          break; /* BLT   / BGE    */
      case 3: c = (ars <  art);                        break; /* BLTU  / BGEU   */
      case 4: c = ((~ars & art) != 0);                 break; /* BNALL / BALL   */
      case 5: c = ((ars >> (art & 31)) & 1) != 0;      break; /* BBS   / BBC    */
    }
    return neg ? c : !c;   /* r-bit[15] selects taken vs not-taken sense */
}
/* taken => NextPC = PC + 4 + sext8(label) ;  else fall through */
```

### 4b. Reg-immediate (`bsi8`) — 8 ops

`BEQI`/`BNEI`/`BLTI`/`BLTUI`/`BGEI`/`BGEUI` compare `ars` against a **`B4CONST`/`B4CONSTU`** table
value (the small-immediate is a 4-bit *table index* in `r=[15:12]`, mapping to the standard Xtensa
power-of-two-rich constant set). `BBCI`/`BBSI` (`op0=0x7`) test bit `#bbi` of `ars`, where the 5-bit
bit index is split across `r`+`t`. Round-trip:

```
beqi a3,1,.L1 → 201326   bnei a3,1 → 1d1366   blti a3,1 → 1a13a6   bltui a3,2 → 1723b6
bgei a3,1 → 1413e6      bgeui a3,2 → 1123f6   bbci a3,5 → 0e6357   bbsi a3,5 → 0be357
```

```c
/* xt_iclass_bsi8b — reg vs B4CONST table value */
void BEQI (uint5 s, uint4 idx, int8 lbl) { if (AR[s] == B4CONST[idx])             goto_rel(lbl); }
void BLTI (uint5 s, uint4 idx, int8 lbl) { if ((int32)AR[s] < (int32)B4CONST[idx])goto_rel(lbl); }
void BGEUI(uint5 s, uint4 idx, int8 lbl) { if (AR[s] >= B4CONSTU[idx])            goto_rel(lbl); }
/* BBCI/BBSI — immediate bit test (bbi = 5-bit bit index) */
void BBCI (uint5 s, uint5 bbi, int8 lbl) { if (((AR[s] >> bbi) & 1) == 0)         goto_rel(lbl); }
void BBSI (uint5 s, uint5 bbi, int8 lbl) { if (((AR[s] >> bbi) & 1) == 1)         goto_rel(lbl); }
```

`[MED/INFERRED]` The `B4CONST`/`B4CONSTU` index→value map is the documented standard-Xtensa table
(structure HIGH; the exact table values are the canonical Xtensa set, not separately re-derived from
these byte patterns).

### 4c. Reg-vs-zero (`bsz12`) — 4 ops

`BEQZ`/`BNEZ`/`BLTZ`/`BGEZ` compare `ars` against `0` with a **12-bit** signed label (wider reach
than the 8-bit forms). Round-trip:

```
beqz a3,.L1 → 008316   bnez → 005356   bltz → 002396   bgez → fff3d6
```

```c
void BEQZ(uint5 s, int12 lbl) { if (AR[s] == 0)         goto_rel(lbl); }
void BNEZ(uint5 s, int12 lbl) { if (AR[s] != 0)         goto_rel(lbl); }
void BLTZ(uint5 s, int12 lbl) { if ((int32)AR[s] <  0)  goto_rel(lbl); }
void BGEZ(uint5 s, int12 lbl) { if ((int32)AR[s] >= 0)  goto_rel(lbl); }
```

`[HIGH form / MED selector bit]` The 12-bit displacement spans the low bits, so a byte-view of the
`bsz12` form interleaves selector + label; the mnemonic/operand round-trip is **exact** and the form
is OBSERVED, but the precise selector sub-nibble assignment (`BEQZ`/`BNEZ` in the eq/ne pair,
`BLTZ`/`BGEZ` in the lt/ge pair — the standard BRI12 `n/m` discriminator) is INFERRED.

**Timing** (`xt_sched_beq`/`_beqz`/`_bltz`; issue `x24_Inst_0_inst_BEQI_issue @0x16773b0`,
`…_BALL_issue @0x16773f0`): USE `ars@3`,`art@3`,`label@2`,`PC_R2@1`; DEF
`BranchTarget@3`,`BranchTaken@3`. → condition resolved **@3** (estage), single base-pipe branch.
Operands are read **late** (`@3`), so a 2-cycle ALU producer feeding a branch operand incurs no extra
stall. `[HIGH/OBSERVED]`

> **GOTCHA — the firmware's PC-bounds enforcement watches *these* offsets.** Every branch/jump target
> is `PC + 4 + sext(label)` (the `+4` base is the *next*-instruction PC, not the current one); a
> reimplementation that bases off the current PC will mis-target by the instruction width. The
> sequencer's PC-bounds gate (see [firmware/seq/pc-bounds](../../firmware/seq/pc-bounds.md)) clamps
> the computed `BranchTarget` against the loaded code window *before* the next-PC mux commits.

---

## 5. Jump / call / return — 8 ops

`[HIGH/OBSERVED]` Unconditional control transfer. Two encoding families share this batch:

* **CALL-format** (`op0` selector in the low byte, 18-bit `soffset` in `[23:6]`): `J`(`op0=6`),
  `CALL0`(`op0=5,n=0`), `CALL8`(`op0=5,n=2`).
* **CALLX / RRR** (`op0=0`, the `ars` register in `s=[11:8]`, the op distinguished by the **high
  nibble of `byte0`** = the major selector): `JX`(`0xa0`), `CALLX0`(`0xc0`), `CALLX8`(`0xe0`),
  `RET`(`0x80`), `RETW`(`0x90`).

The selector CONSTs read **directly from each encode thunk's `movl $IMM,(%rdi)`** in `libisa-core.so`
this pass:

| mnem    | encode thunk @VMA | selector CONST | decode |
|---------|-------------------|---------------:|--------|
| `CALL0` | `Opcode_call0_Slot_inst_encode @0x33a1e0`  | `0x05`     | `op0=5`, `n[5:4]=0` (rotate +0) |
| `CALL8` | `Opcode_call8_Slot_inst_encode @0x33d1f0`  | `0x25`     | `op0=5`, **`n[5:4]=2` (rotate +8)** |
| `J`     | `Opcode_j_Slot_inst_encode @0x33a4a0`      | `0x06`     | `op0=6` |
| `JX`    | `Opcode_jx_Slot_inst_encode @0x33a4c0`     | `0xa0`     | RRR major `a` |
| `CALLX0`| `Opcode_callx0_Slot_inst_encode @0x33a1f0` | `0xc0`     | RRR major `c`, `n=0` |
| `CALLX8`| `Opcode_callx8_Slot_inst_encode @0x33d200` | `0xe0`     | RRR major `e`, `n=2` (rotate +8) |
| `RET`   | `Opcode_ret_Slot_inst_encode @0x33bac0`    | `0x80`     | RRR major `8` |
| `RETW`  | `Opcode_retw_Slot_inst_encode @0x33d240`   | `0x90`     | RRR major `9` |

`[HIGH/OBSERVED]` Each thunk is exactly `c7 07 <imm32>  c3` (`movl $imm,(%rdi); ret`). Round-trip,
with raw bytes for `CALL0`/`CALL8` confirming the `n`-field directly:

```
j   .L0    → fffcc6 (op0=6)        jx  a3 → 0003a0 (s=a3)
call0 .Lt  → 000185               call8 .Lt → 000165
callx0 a3  → 0003c0               callx8 a3 → 0003e0
ret        → 000080               retw → 000090
ret.n      → f00d                 retw.n → f01d        (density forms, §6)

raw call0 = c5 ff ff  -> byte0 0xc5: op0=5, n[5:4]=0          /* +0 window */
raw call8 = e5 ff ff  -> byte0 0xe5: op0=5, n[5:4]=2 (=10b)   /* +8 window */
```

### Windowed-call register-window semantics

> **QUIRK — `ncore2gp` is an 8-register-window config: `CALL8`/`CALLX8` rotate by exactly 8, and
> `call4`/`call12`/`callx4`/`callx12` are *not assemblable*.** `xtensa-elf-as` rejects them
> (`unknown opcode or format name 'call4'`). The window-exception model in `libcas-core.so` confirms
> this: the only window exceptions present are `WindowOverflow8_exc @0x177a950`,
> `WindowUnderflow8_exc @0x177a890`, and `WindowInstCall0Exception_exc @0x177f9f0` — there are **no**
> `…4`/`…12` variants. The custom-op dispatcher's `callx8` (see
> [abi/complete-customop-abi](../../abi/complete-customop-abi.md)) therefore always rotates the AR
> window by a fixed 8 registers. `[HIGH/OBSERVED]`

The AR file is `regfiles[]` index 0 = **32-bit × 64**, windowed; `WindowBase` names which 16-register
window of the 64 is architecturally visible as `a0..a15`. `CALL8` advances `WindowBase` by 8 (so the
callee's `a0` is the caller's `a8`), packs the caller's return address into the **new** `a0`, and
records the rotation in the high bits of that link so `RETW` can undo it:

```c
/* xt_iclass_call0 / call8 / callx0 / callx8 — entry */
void CALL0 (int18 soffsetx4) {                       /* CALL0 ABI: no window move */
    AR[0] = PC + 3;                                  /* return link in a0 (3 = insn width) */
    PC    = (PC & ~3u) + 4 + (soffsetx4 << 2);       /* word-scaled PC-relative target     */
}
void CALLX0(uint5 s) { AR[0] = PC + 3; PC = AR[s]; } /* register-indirect, no window move  */

void CALL8 (int18 soffsetx4) {                       /* windowed ABI: rotate +8            */
    /* the return address is packed with the call increment (=2 for a +8 rotation) in     */
    /* its TWO HIGH bits, so RETW can recover the rotation amount:                          */
    uint32 link = ((CALL_INC_8 /*=2*/) << 30) | ((PC + 3) & 0x3FFFFFFFu);
    WindowBase += 8;                                 /* callee sees caller's a8 as its a0  */
    AR[0]       = link;                              /* into the NEW window's a0           */
    if (window_would_spill(WindowBase))             /* WindowOverflow8_exc @0x177a950     */
        raise(WindowOverflow8);                      /* spill handler runs, then re-enters */
    PC = (PC & ~3u) + 4 + (soffsetx4 << 2);
}
void CALLX8(uint5 s) {                               /* register-indirect windowed         */
    uint32 link = ((CALL_INC_8) << 30) | ((PC + 3) & 0x3FFFFFFFu);
    uint32 tgt  = AR[s];                             /* read BEFORE the window moves       */
    WindowBase += 8; AR[0] = link;
    if (window_would_spill(WindowBase)) raise(WindowOverflow8);
    PC = tgt;
}

/* xt_iclass_return / retw — exit */
void RET (void) { PC = AR[0]; }                      /* CALL0 ABI: plain jump to the link  */
void RETW(void) {                                    /* windowed return                    */
    uint32 link = AR[0];
    uint32 inc  = link >> 30;                        /* recover the rotation amount (=2=>8) */
    WindowBase -= (inc << 2);                         /* undo the caller's CALL8 rotation   */
    if (window_underflow(WindowBase))               /* WindowUnderflow8_exc @0x177a890    */
        raise(WindowUnderflow8);                      /* restore the spilled caller frame   */
    PC = link & 0x3FFFFFFFu;                          /* drop the 2 increment bits          */
}
```

`[HIGH/OBSERVED]` field set (the `n`-field, the major nibbles, the `CALL8`→`+8` rotation, the
over/underflow exceptions); `[MED/INFERRED]` the intra-field bit order of the 2-bit call-increment in
the link's high bits (the standard windowed-ABI layout — packed in `a0[31:30]`, `inc=2` for `+8`).
`CALL`/`RET` expose `PSRING`/`VECBASE` (the privilege ring + vector base used to validate the target).

**Timing**: `J` `BranchTarget@3` (resolved @estage), single cycle. `CALL0` (`…_inst_CALL0_issue
@0x16782a0`) `ar0(link)@4`, target`@3`. `CALLX0` (`@0x16782d0`) `ars` USE`@3`, `ar0@4`, target`@3`.
`CALL8` (`@0x1678f10`)/`CALLX8` (`@0x1678f40`) add the `WindowBase` update + the early spill check.
`RET` `target@3`, `MS_DISPST@4` (dispatch-state token). `RETW` (`@0x1678fb0`) does the window
underflow check as an **early-pipe** action (`Underflow_Excep@1`, `WB_*@0`), target`@3`; `JX`
(`@0x1678330`) target`@3`. `[HIGH/OBSERVED]`

---

## 6. `xt_density` — 16-bit code-density sub-ISA (`.N`), 10 ops

`[HIGH/OBSERVED]` The Xtensa code-density option: 16-bit-wide re-encodings of the most common ops,
emitted **automatically** by the assembler when the operands fit the narrow fields. Package
`xt_density`. The semantic body of each `.N` form **reuses the corresponding wide op's datapath** —
the density variant is purely an encoding compression, decode-equivalent to its 24-bit sibling. **No
`CPENABLE`.**

| `.N` op | iclass | op0 | wide equivalent | issue (`libcas-core`) |
|---------|--------|----:|-----------------|-----------------------|
| `L32I.N`  | `xt_iclass_loadi4`  | `0x8` | `L32I` (offset ×4, 4-bit) | `x16a_Inst16a_0_inst_L32I_N_issue @0x71c7d0` |
| `S32I.N`  | `xt_iclass_storei4` | `0x9` | `S32I`                    | `x16a_…_S32I_N_issue @0x71c830` |
| `ADD.N`   | `xt_iclass_add.n`   | `0xa` | `ADD` (RRRN)              | `x16a_…_ADD_N_issue @0x71c6e0` |
| `ADDI.N`  | `xt_iclass_addi.n`  | `0xb` | `ADDI` (4-bit, `0=>-1`)   | `x16a_…_ADDI_N_issue @0x71c770` |
| `MOV.N`   | `xt_iclass_mov.n`   | `0xd` | `OR ar,ars,ars` (RRRN)    | `x16b_…_MOV_N_issue @0xd193d0` |
| `MOVI.N`  | `xt_iclass_movi.n`  | `0xc` | `MOVI` (7-bit packed)     | `x16b_Inst16b_0_inst_MOVI_N_issue @0xd19430` |
| `BEQZ.N`  | `xt_iclass_bz6`     | `0xc` | `BEQZ` (6-bit fwd)        | `x16b_…_BEQZ_N_issue @0xd19390` |
| `BNEZ.N`  | `xt_iclass_bz6`     | `0xc` | `BNEZ` (6-bit fwd)        | `x16b_…_BEQZ_N_issue` (shared) |
| `RET.N`   | density `return`    | `0xf` | `RET`                     | `x16b_…_RET_N_issue @0xd19470` |
| `RETW.N`  | density `retw`      | `0xf` | `RETW`                    | `x16b_…_RETW_N_issue @0xd194c0` |

`[HIGH/OBSERVED]` Round-trip (auto-narrowed, no `--no-transform`):

```
l32i.n a3,a4,8 → 2438   s32i.n a3,a4,8 → 2439   add.n a2,a3,a4 → 234a
addi.n a2,a3,1 → 231b   mov.n a2,a3 → 032d      movi.n a2,5 → 520c
beqz.n a3,.L1 → 438c    bnez.n a3,.L1 → 23cc    ret.n → f00d   retw.n → f01d
```

Two density quirks, both re-verified by raw-byte round-trip this pass:

> **QUIRK — `ADDI.N`'s 4-bit immediate `0` decodes as `-1`.** Raw bytes:
> `addi.n a2,a3,-1 → 0b 23` (imm-nibble `0`), `addi.n a2,a3,1 → 1b 23` (nibble `1`),
> `addi.n a2,a3,15 → fb 23` (nibble `15`). So the field range is **`{-1, 1, 2, …, 15}`** — there is
> *no* `+0` `ADDI.N`; the all-zero immediate is repurposed for `-1` (the common decrement). A
> reimplementation that decodes the nibble straight as a `0..15` unsigned add will be off by 1 at the
> low end. `[HIGH/OBSERVED]`

> **QUIRK — `BEQZ.N`/`BNEZ.N` are *forward-only*, 6-bit reach.** `xtensa-elf-as` assembles a
> 60-byte-forward `beqz.n` (`a3bc`) but **rejects** any backward target
> (`Error: operand 2 of 'beqz.n' has out of range value '4294967293'` = `-3` as unsigned). The
> `uimm6` displacement covers `{4 .. 68}` bytes forward only — loops that must branch backward fall
> back to the 12-bit `BEQZ`/`BNEZ`. `[HIGH/OBSERVED]`

`MOVI.N` packs a 7-bit value over the range `-32..95` (the low 5 bits + a sign-ish high extension).
Timing: each `.N` form inherits its wide op's USE/DEF stages — `L32I.N art@5/ars@1` (same load-use as
`L32I`), `ADD.N arr@4/ars,art@4` (single-cycle ALU), `RET.N` like `RET` (target`@3`,`MS_DISPST@4`).
`[HIGH/OBSERVED]`

---

## 7. `xt_mul32` — 32-bit integer multiply unit, 5 ops

`[HIGH/OBSERVED]` The 32×32 integer multiplier. `MULL` = low 32 bits of the product; `MULUH`/`MULSH`
= high 32 bits of the unsigned/signed 64-bit product; `MUL16U`/`MUL16S` = 16×16 → 32 of the
zero-/sign-extended low 16-bit halves. All five share semantic group `xt_mul32` (`MULL` iclass
`xt_mul32`; `MULUH`/`MULSH` iclass `xt_mul32h`; `MUL16U/S` iclass `xt_mul16`). All RRR, AR-file,
`OUT[arr] IN[ars, art]`. **No state, no exception (multiply cannot fault); no `CPENABLE`.**

**Encoding** (RRR; the mul/div family is `op2=0x4`, `op1=0x0`; the op is the `t`-nibble selector +
`op0` sub-major). Field map VERIFIED this pass by isolating every field with `mull a5,a6,a7`:

```
mull a5,a6,a7 -> 825670  => bytes 82 56 70 => op0=2, t=8(MULL sel), s=6(ars=a6),
                                              r=5(arr DEST=a5), op2=7(art=a7), op1=0
  arr (dest)  = r-field   [15:12]
  ars (src1)  = s-field   [11:8]
  art (src2)  = op2-field [23:20]
  selector    = t-field   [7:4]      (op0=2: MULL=8 MULUH=a MULSH=b ; op0=1: MUL16U=c MUL16S=d)
  op0         = [3:0]                 (2 = MUL32/DIV sub-major ; 1 = MUL16 sub-major)
```

| mnem | op0 | `t` sel | inst-slot CONST (`movl`) | placements |
|------|----:|--------:|--------------------------|-----------:|
| `MULL`   | 2 | 8 | `Opcode_mull_Slot_inst_encode @0x33d7e0` = `0x820000` | 11 |
| `MULUH`  | 2 | a | (`…_muluh_…`)                                          | 11 |
| `MULSH`  | 2 | b | (`…_mulsh_…`)                                          | 11 |
| `MUL16U` | 1 | c | (`…_mul16u_…`)                                         | 18 |
| `MUL16S` | 1 | d | (`…_mul16s_…`)                                         | 18 |

`[HIGH/OBSERVED]` Round-trip:

```
mull   a2,a3,a4 → 822340   muluh a2,a3,a4 → a22340   mulsh a2,a3,a4 → b22340
mul16s a2,a3,a4 → d12340   mul16u a2,a3,a4 → c12340   mull a5,a6,a7 → 825670
```

```c
/* xt_mul32 / xt_mul32h / xt_mul16 — AR x AR integer products (no flags, no saturation) */
void MULL  (uint5 r,s,t) { AR[r] = (uint32)( (uint64)AR[s] * (uint64)AR[t] ); }    /* low 32, sign-agnostic */
void MULUH (uint5 r,s,t) { AR[r] = (uint32)( ((uint64)AR[s] * (uint64)AR[t]) >> 32 ); }
void MULSH (uint5 r,s,t) { AR[r] = (uint32)( ((int64)(int32)AR[s] * (int64)(int32)AR[t]) >> 32 ); }
void MUL16U(uint5 r,s,t) { AR[r] = (uint32)(AR[s] & 0xFFFF) * (uint32)(AR[t] & 0xFFFF); }
void MUL16S(uint5 r,s,t) { AR[r] = (uint32)( (int32)(int16)AR[s] * (int32)(int16)AR[t] ); }
/* full 64-bit product = MULL(low) ++ MUL{U,S}H(high) of the same operands */
```

**Worked value** (value-model): `ars=0x00010000, art=0x00010000` → `MULL=0`, `MULUH=1` → 64-bit
product `0x1_0000_0000`; `MUL16U` of the same inputs = `0` (low halves are 0). `[HIGH]`

**Timing** (`MULL_xt_mul32`; issue `…_inst_MUL16S_issue @0x1677920` + per-FU copies): USE
`ars@4`,`art@4`; internal `mulin0/1_reg@4`, partials `p0/p1_reg@5`; DEF `arr@6`. → **2-cycle
multiply latency** relative to the `estage-3/mstage-4` base pipe (operands sampled `@4`, product
`@6`; a dependent op stalls ~2 cycles). **Fully pipelined**, 1/cycle throughput. `[HIGH/OBSERVED]`

---

## 8. `xt_sem_div` — scalar integer divide unit, 4 ops

`[HIGH/OBSERVED]` The scalar core integer divider (distinct from the IVP vector `ivp_sem_divide` of
[B23](template-and-partition.md)). `QUOS`/`QUOU` = signed/unsigned quotient; `REMS`/`REMU` =
signed/unsigned remainder. Semantic group `xt_sem_div`, iclass `xt_iclass_div`. It **shares the
`MUL32/DIV` RRR slot** with the multiplier (`op0=2`), differing only in the `t`-nibble selector — the
divide and multiply are one functional block. This is the **only B26 group with a true architectural
exception**: `IntegerDivideByZeroException`, raised when `art==0`, *before* the `arr` writeback. It
serializes via the `XTSYNC` token. **No `CPENABLE`.**

**Encoding** — same RRR field map as MUL32 (`arr=r`, `ars=s`, `art=op2`), `op0=2`, divide selector in
`t`:

| mnem | `t` sel | inst-slot CONST | N-slot CONSTs (asymmetric vs MUL32) | placements |
|------|--------:|-----------------|-------------------------------------|-----------:|
| `QUOU` | c | (`…_quou_…`)                                  |                                   | 10 |
| `QUOS` | d | `Opcode_quos_Slot_inst_encode @0x33ed80` = `0xd20000` | `N0 @0x33ed90` = `0x471000` | 11 |
| `REMU` | e | (`…_remu_…`)                                  |                                   | 10 |
| `REMS` | f | (`…_rems_…`)                                  |                                   | 10 |

`[HIGH/OBSERVED]` Note the N-slot asymmetry: `QUOS` places on `N0`+`N2`, `MULL` on `N1`+`N2` —
divide and multiply do not share an identical narrow-slot map even though they share the wide RRR
slot. Round-trip:

```
quos a8,a9,a10 → d289a0  => op0=2, t=d(QUOS), s=9(ars=a9), r=8(arr=a8), op2=a(art=a10)
quos a2,a3,a4 → d22340   quou a2,a3,a4 → c22340   rems a2,a3,a4 → f22340   remu a2,a3,a4 → e22340
```

```c
/* xt_iclass_div — scalar integer divide (round toward zero, C-style) */
void QUOS(uint5 r,s,t){ if(AR[t]){ AR[r]=(uint32)((int32)AR[s] / (int32)AR[t]); } else raise(IntegerDivideByZeroException); }
void QUOU(uint5 r,s,t){ if(AR[t]){ AR[r]= AR[s] / AR[t]; }                       else raise(IntegerDivideByZeroException); }
void REMS(uint5 r,s,t){ if(AR[t]){ AR[r]=(uint32)((int32)AR[s] % (int32)AR[t]); } else raise(IntegerDivideByZeroException); }
void REMU(uint5 r,s,t){ if(AR[t]){ AR[r]= AR[s] % AR[t]; }                       else raise(IntegerDivideByZeroException); }
/* on art==0 the exception fires BEFORE the writeback, so arr is left UNCHANGED */
```

**Worked value**: `ars=17, art=5` → `QUOS=3`, `REMS=2`. `ars=-17, art=5` → `QUOS=-3` (truncate
toward zero), `REMS=-2` (remainder takes the **sign of the dividend**). `art=0` →
`IntegerDivideByZeroException`, `arr` unchanged. `[HIGH value-model]`

> **GOTCHA — `INT_MIN / -1` does *not* raise a second exception.** The standard Xtensa convention is
> to return `INT_MIN` (the arithmetic wraps) with **no** overflow trap; only `art==0` traps. "No
> second exception" is `[HIGH]`; the exact returned wrap value is the documented Xtensa convention
> `[MED]`.

**Timing** (`QUOS_xt_sched_div`; issue `x24_Inst_0_inst_QUOS_issue @0x1678990` + per-FU copies): USE
`XTSYNC@3`,`ars@3`,`art@3`,`OperandIn1@4`; DEF `IntegerDivideByZeroException@3`,`arr@4`,`XTSYNC@4`,
`OperandOut1/2@3`. The divide is **serialized via the `XTSYNC` use@3 / def@4 self-dependency** — a
non-pipelined iterative unit: each divide holds the `XTSYNC` token (`my_XTSYNC_stall @0x1776590`,
`my_XTSYNC_cycle @0x1765b90` in `libcas-core.so`), so back-to-back divides serialize. Divide-by-zero
is detected **early (@3)**; the `arr` result `@4` is the scoreboard-visible completion, while the true
iterative latency is multi-cycle (modelled by the `XTSYNC` stall chain). `[HIGH structure / MED exact
cycle count]`

> **NOTE — there is no named "divide latency" constant in the binary.** The iterative cost lives in
> the per-FU `*_QUOS_issue` / `*_QUOU_issue` scheduling bodies (the `XTSYNC` stall edges), not in a
> standalone latency symbol; DWARF in `libcas-core.so` names no `xtsync`/`quotient`/`iterative`
> constant. The exact non-pipelined cycle count is therefore `[MED]` (structure HIGH).

---

## 9. Coverage / verification ledger — Batch 26

`[HIGH/OBSERVED]` **60 opcodes**, count CHECKS exact: 9 core ld/st + 24 branch (12 `bst8` + 8
`bsi8` + 4 `bsz12`) + 8 jump/call + 10 density `.N` + 5 `xt_mul32` + 4 `xt_sem_div` = **60**. A
`nm libisa-core.so | rg -c "Opcode_<mnem>(_w15)?_Slot_"` presence sweep this pass returned **0
missing of 60**.

`[HIGH/OBSERVED]` All 60 mnemonics **round-tripped** `xtensa-elf-as → xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`, `XTENSA_SYSTEM=…/XtensaTools/config`): exact spelling + operand order. The
density encoder narrows `L32I`/`S32I`/`RET`/`RETW` to `.N` when legal (both the auto-narrowed and the
`--no-transform` wide encodings verified). Standard field layout VERIFIED by distinct-register
round-trips (`op0=[3:0]`, `t=[7:4]`, `s=[11:8]`, `r=[15:12]`, `op1=[19:16]`, `op2=[23:20]` LE).

`[HIGH/OBSERVED]` **Selector CONSTs read out of the encode thunks' `movl $IMM,(%rdi)` bytes**:
`call0=0x05`, `call8=0x25` (the `n[5:4]=2` ⇒ `+8` rotation), `callx0=0xc0`, `callx8=0xe0`,
`ret=0x80`, `retw=0x90`, `j=0x06`, `jx=0xa0`, `mull=0x820000`, `quos=0xd20000`. Branch condition =
the `r`-nibble (`BNONE=0 BEQ=1 BLT=2 BLTU=3 BALL=4 BBC=5 BANY=8 BNE=9 BGE=a BGEU=b BNALL=c BBS=d`;
inverse pairs differ by `+0x8`). Multiply/divide share `op0=2` (MUL16: `op0=1`), op in the `t`-nibble
(`MULL=8 MULUH=a MULSH=b MUL16U=c MUL16S=d QUOU=c QUOS=d REMU=e REMS=f`); operands
`arr=r`/`ars=s`/`art=op2`.

`[HIGH/OBSERVED]` **`ncore2gp` is an 8-only windowed config**: `call4`/`call12`/`callx4`/`callx12` are
unassemblable; only `WindowOverflow8_exc`/`WindowUnderflow8_exc`/`WindowInstCall0Exception_exc` exist
in `libcas-core.so`. `CALL8`/`CALLX8` rotate `WindowBase` by 8; `RETW` recovers the rotation from the
link's 2 high bits and checks underflow early.

`[HIGH/OBSERVED]` **Timing** from `INSTR_SCHEDULE` USE/DEF stage deltas
(`rstage0/estage3/mstage4/wstage6`): loads `art@5`/`ars@1` (≈2-cyc load-use); stores `data@5`/
`addr@1`; branches resolve `@3`; `J`/`CALL` target`@3`, link `ar0@4`; MUL32 `arr@6`/operands`@4`
(≈2-cyc, 1/cyc throughput); scalar DIV `arr@4` with the `XTSYNC` self-dependency (serialized
iterative, divide-by-zero `@3`); `.N` forms inherit their wide op's stages.

`[HIGH/OBSERVED]` **ABI**: these are SCALAR base-Xtensa AR-file ops — **no `CPENABLE`** state-in, **no
`Coprocessor1Exception`**. Memory ops expose `ScalarMemAccess`/`Load`/`Store` + the
`VAddr`/`MemControl`/`MemData` mem-pipe pseudo-operands; branches expose
`BranchTarget`/`Taken`/`ImmOffset`; `xt_sem_div` is the only B26 group with a true exception
(`IntegerDivideByZeroException`) + an `XTSYNC` serialization token.

`[MED/INFERRED]` residual uncertainties: (a) the exact iterative cycle count of the scalar divider
(the `XTSYNC` chain models the stall; no named latency constant); (b) the `INT_MIN/-1` `QUOS` wrap
value (standard Xtensa `INT_MIN`, no second trap — "no exception" HIGH, exact value MED); (c) the
intra-field bit order of the `CALL8`/`RETW` call-increment in the link's 2 high bits (standard
windowed-ABI layout — field set HIGH, bit order MED); (d) the `B4CONST`/`B4CONSTU` index→value map
for `BEQI`/`BLTI`/`BGEI`/`BGEUI`/`BLTUI` (standard Xtensa table — structure HIGH).

> **CORRECTION carried forward.** The reg-reg branch major-op is **`op0=0x7`** (re-grounded this pass
> with a zero-displacement round-trip), not the displacement-contaminated `0x2` an earlier
> worked-byte table reported. Condition nibbles are unaffected.

---

## Cross-references

* [B25 — base-Xtensa arith / logic / shift](b25-xt-core.md) — the sibling scalar batch; the 24-bit
  `ADD`/`SUB`/`AND`/`OR`/`EXTUI`/`MOVI` singletons (the `.N` density arith forms live *here*, not
  there).
* [B27 — System / SR / RegWindow / Sync](b27-xt-system.md) — the `WindowBase`/`WindowStart` special
  registers and the spill/fill handlers that back `CALL8`/`RETW`.
* [firmware/seq/pc-bounds](../../firmware/seq/pc-bounds.md) — the sequencer's branch / PC-bounds
  enforcement that clamps the computed `BranchTarget`.
* [abi/complete-customop-abi](../../abi/complete-customop-abi.md) — the `callx8` dispatch ABI that
  drives custom ops through this batch's windowed call.
* [Template & Partition](template-and-partition.md) — the 30-batch partition map and the
  semantic-group ledger this batch's 60-op count reconciles against.
