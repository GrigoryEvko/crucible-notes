# ISA Batch 25 — base-Xtensa scalar arith / logic / shift (the AR-file core)

This is the **first base-Xtensa scalar batch**. Everything documented above [B01–B24](b21-select-shuffle.md)
rides the 512-bit IVP vector coprocessor; this batch is the **other datapath** entirely — the
*non-FLIX* scalar integer core of the Cadence Vision-Q7 *Cairo* (`ncore2gp`) DSP, the Xtensa-LX
24-bit (and 16-bit `.N` density) instruction set operating on the **32-bit AR register file**. These
are the ops the device firmware control spine, the custom-op CRT/startup, and every host-side
control thread of the vector engine actually execute: integer add/sub, the `addx`/`subx`
shift-add address forms, bitwise logic, the immediate and dynamic shifters, the branch-free
conditional moves, set-on-compare, the immediate-load family, and the `THREADPTR` thread-local
base register. None of them touch a vector lane.

The structural wall against the IVP axis is sharp and machine-checkable: **every** `ivp_` op carries
the `CPENABLE` state-in and raises `Coprocessor1Exception` when the coprocessor enable bit is clear;
**none** of the 39 ops on this page do. They are unconditionally-executable AR-file ops whose only
implicit state is the 6-bit `SAR` shift-amount register (dynamic shifts), the 1-bit boolean register
file `BR` (`MOVT`/`MOVF`), and user register #231 = `THREADPTR` (`RUR`/`WUR`). `[HIGH/OBSERVED — ICLASS state args]`

Everything below is grounded in the shipped binaries: the **encoding** from the
`Opcode_<mnem>_Slot_inst_encode` thunks in `libisa-core.so` (read byte-for-byte; the selector word is
the architectural op2/op1 nibble), the **value semantics** by disassembling the matching
`module__xdref_*` leaf bodies in `libfiss-base.so`, the **issue timing** from the per-op
`*_inst_*_issue` scoreboard bodies in `libcas-core.so`, and a byte-exact **encode/decode oracle**
from the device-native `xtensa-elf-as` / `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). Every
representative round-trips through that device oracle. Confidence tags per
[the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-disasm-or-round-trip, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic and binary identity.** `libisa-core.so`
> (9 690 712 B, ET_DYN x86-64, **not stripped**). Per `readelf -SW`: `.text` (VMA `0x312c10`)
> and `.rodata` (VMA `0x3b6e40`) are **VMA == file-offset**; `.data` (VMA `0x764040` ↔ file
> `0x564040`) and `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) carry the per-binary delta
> **`0x200000`** — **not** libtpu's `0x400000`. The encode thunks documented here live in `.text`
> (VMA == file), so `objdump -d` reads them directly with no offset correction. All config DLLs are
> under `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/` (gitignored; reach with
> `fd --no-ignore` or an absolute path).

---

## 0. Scope boundary — what is and is not on this page

The `ncore2gp` opcode table (`opcodes[]@0x6ce6c0`, 1534 rows) splits cleanly by mnemonic prefix:
**469 scalar (base-ISA, non-`ivp_`) mnemonics** versus **1065 vector (`ivp_`)** — counted directly
from the symbol table (`nm libisa-core.so | rg -oP 'Opcode_\K\w+(?=_Slot_)' | sort -u`, then
filtering `^ivp_`). This page is the first of the five base-Xtensa batches that partition those 469;
it owns the **full-width core arith/logic/shift singleton groups plus the `THREADPTR` UR pair =
39 mnemonics**. The roster is fixed by `<SEMANTIC>`-group membership (each op is its own singleton
group, e.g. `<SEMANTIC name="ADD">`), disjoint by construction.

The following adjacent families **spell** like this batch but route elsewhere — verified 0
cross-membership:

* **`.N` density narrow forms (`ADD.N` `ADDI.N` `MOV.N` `MOVI.N`) → [B26](b26-xt-ctrl.md), not here.**
  These are *separate* `<SEMANTIC>` groups on the 16-bit `Inst16a`/`Inst16b` formats. The config ships
  exactly **14** such 16-bit-slot ops (`nm … | rg -c '_Slot_inst16[ab]_encode$'` = 14):
  `add.n addi.n mov.n movi.n beqz.n bnez.n l32i.n nop.n ret.n retw.n break.n halt.n ill.n s32i.n`.
  The four ALU members of that set are the density counterparts of ops on *this* page; they are cited
  in §6 as the prime density-encoding QUIRK but counted at B26.
* **`MUL16S`/`MUL16U`/`MULL`/`MULSH`/`MULUH` (the 16/32-bit integer multiplier) → B26.** Group
  `xt_mul16`/`xt_mul32`/`xt_mul32h`, distinct iclasses; not an arith/logic/shift op.
* **`MIN`/`MAX`/`MINU`/`MAXU` and `QUOS`/`QUOU`/`REMS`/`REMU` → B28.** Groups `xt_iclass_minmax`,
  `xt_iclass_div`; both branch-free integer select/divide, not on this page.
* **`NSA`/`NSAU`, `CLAMPS`, `SEXT` → system/misc batches.** Normalize-shift-amount and the
  sign-extend/clamp helpers are their own iclasses.
* **`CONST16`/`L32R` (the 32-bit immediate builders) → B26 (loads/immediate-load family).** Both are
  real ops in this config (`Opcode_const16_Slot_inst_encode @0x33a280` template `0x4`;
  `Opcode_l32r_Slot_inst_encode @0x33a860` template `0x1`). They are documented here in §3.8 / §6 as
  the *companions* to `MOVI` for building wide constants, because reimplementing the immediate-load
  path requires all three together — but the partition assigns them to B26.

The control-flow ops (`L32I`/`S32I`/branches/`CALL`/`LOOP`) are [B26](b26-xt-ctrl.md); the
special-register / register-window / sync machinery (`RSR`/`WSR`/`MOVSP`/`ENTRY`) is
[B27](b27-xt-system.md); the CRT/startup code that *uses* this batch's ops to set up the AR window
and `THREADPTR` is documented at [boot-reset.md](../../uarch/boot-reset.md). The 30-batch
classifier itself is [Template & Partition](template-and-partition.md).

---

## 1. Roster and count verification

The 39 mnemonics, by iclass (each row enumerated from its `Opcode_<mnem>_Slot_inst_encode` thunk in
`libisa-core.so`; the `_Slot_` anchor pins the mnemonic boundary so `add` does not bleed into
`addx2`/`addi`/`addmi`):

| iclass | members | n |
|--------|---------|---|
| `xt_iclass_addsub` | `ADD SUB ADDX2 ADDX4 ADDX8 SUBX2 SUBX4 SUBX8` | 8 |
| `xt_iclass_addi` | `ADDI` | 1 |
| `xt_iclass_addmi` | `ADDMI` | 1 |
| `xt_iclass_bit` | `AND OR XOR` | 3 |
| `xt_iclass_neg` | `NEG ABS` | 2 |
| `xt_iclass_slli` / `srli` / `srai` | `SLLI` · `SRLI` · `SRAI` | 3 |
| `xt_iclass_shiftst` | `SRC` | 1 |
| `xt_iclass_shifts` / `shiftt` | `SLL` · `SRL SRA` | 3 |
| `xt_iclass_sar` | `SSL SSR SSA8B SSA8L` | 4 |
| `xt_iclass_sari` | `SSAI` | 1 |
| `xt_iclass_exti` | `EXTUI` | 1 |
| `xt_iclass_movi` | `MOVI` | 1 |
| `xt_iclass_movz` | `MOVEQZ MOVNEZ MOVLTZ MOVGEZ` | 4 |
| `xt_iclass_bmove` | `MOVT MOVF` | 2 |
| `xt_iclass_salt` | `SALT SALTU` | 2 |
| `rur_threadptr` / `wur_threadptr` | `RUR.THREADPTR` · `WUR.THREADPTR` | 2 |
| | **TOTAL** | **39** |

All 39 round-trip byte-for-byte through the device toolchain (§7). The `rur.threadptr` / `wur.threadptr`
mnemonics mangle the `.` to `_` in the symbol name: `Opcode_rur_threadptr_Slot_inst_encode @0x338790`,
`Opcode_wur_threadptr_Slot_inst_encode @0x3387a0`.

---

## 2. Common encoding model

### 2.1 Register file and operand fields

All operands are in the **AR file** (`regfiles[]` index 0 = 32-bit × 64 physical / 16 window-visible).
The standalone form every op uses is the canonical Xtensa **24-bit RRR** layout, little-endian on the
wire (`v = b0 | b1<<8 | b2<<16`):

```
   op0 = v[3:0]    t = v[7:4]    s = v[11:8]
     r = v[15:12]  op1 = v[19:16]  op2 = v[23:20]
```

The field roles for the core ALU group (`op0 = 0`, RST family): `r` = destination `arr`, `s` =
source1 `ars`, `t` = source2 `art`; the `op2` nibble discriminates the operation. The RRI8 group
(`ADDI`/`MOVI`/`ADDMI`, `op0 = d`/`4`/`1`) uses `t` = dest, `s` = src, with the immediate in the
byte-0 / byte-2 field and the `r` nibble as the sub-selector.

### 2.2 Selector constants — read from the encode thunks

Each `Opcode_<mnem>_Slot_inst_encode` thunk is a one-line x86 body `movl $word0,(%rdi); ret`
(bytes `C7 07 <imm32le> C3`). The `word0` template carries the **architectural opcode bits with the
register fields zeroed** — the bare selector. Read from `libisa-core.so`:

| op | `Slot_inst` VMA | template `word0` | decode |
|----|-----------------|------------------|--------|
| `ADD` | `0x338de0` | `0x00800000` | RST0 op2=8 |
| `ADDX2` | `0x338fb0` | `0x00900000` | op2=9 |
| `ADDX4` | `0x339180` | `0x00a00000` | op2=10 |
| `ADDX8` | `0x339350` | `0x00b00000` | op2=11 |
| `SUB` | `0x339520` | `0x00c00000` | op2=12 |
| `SUBX2` | `0x3396f0` | `0x00d00000` | op2=13 |
| `SUBX4` | `0x3398c0` | `0x00e00000` | op2=14 |
| `SUBX8` | `0x339a90` | `0x00f00000` | op2=15 |
| `AND` | `0x339c60` | `0x00100000` | op2=1 |
| `OR` | `0x339e30` | `0x00200000` | op2=2 |
| `XOR` | `0x33a000` | `0x00300000` | op2=3 |
| `NEG` | `0x33b640` | `0x00600000` | RT0 s=0 |
| `ABS` | `0x33b4b0` | `0x00600100` | RT0 s=1 |
| `SLLI` | `0x33c7c0` | `0x00010000` | RST1 op2=0 |
| `SRAI` | `0x33c980` | `0x00210000` | op2=2 |
| `SRLI` | `0x33cb40` | `0x00410000` | op2=4 |
| `SRC` | `0x33c290` | `0x00810000` | op2=8 |
| `SRL` | `0x33c5f0` | `0x00910000` | op2=9 |
| `SLL` | `0x33c140` | `0x00a10000` | op2=10 |
| `SRA` | `0x33c430` | `0x00b10000` | op2=11 |
| `SSR` | `0x33bef0` | `0x00400000` | ST1 r=0 |
| `SSL` | `0x33be40` | `0x00401000` | r=1 |
| `SSA8L` | `0x33bd90` | `0x00402000` | r=2 |
| `SSA8B` | `0x33bd00` | `0x00403000` | r=3 |
| `SSAI` | `0x33bfa0` | `0x00404000` | r=4 |
| `EXTUI` | `0x33a3c0` | `0x00040000` | QRST op1=4 |
| `MOVI` | `0x33abc0` | `0x0000a002` | LSAI r=10 |
| `MOVEQZ` | `0x33ad80` | `0x00830000` | RST3 op2=8 |
| `MOVNEZ` | `0x33b2f0` | `0x00930000` | op2=9 |
| `MOVLTZ` | `0x33b120` | `0x00a30000` | op2=10 |
| `MOVGEZ` | `0x33af50` | `0x00b30000` | op2=11 |
| `MOVF` | `0x33dc50` | `0x00c30000` | op2=12 |
| `MOVT` | `0x33dc60` | `0x00d30000` | op2=13 |
| `SALTU` | `0x33d190` | `0x00620000` | RST2 op2=6 |
| `SALT` | `0x33cfc0` | `0x00720000` | op2=7 |
| `ADDI` | `0x3389a0` | `0x0000c002` | LSAI r=12 |
| `ADDMI` | `0x338c00` | `0x0000d002` | LSAI r=13 |
| `RUR.THREADPTR` | `0x338790` | `0x00e30e70` | st=231 (0xE7) |
| `WUR.THREADPTR` | `0x3387a0` | `0x00f3e700` | sr=231 (0xE7) |

The `word0` byte[2] high nibble *is* the architectural op2 for the RST0/RST1/RST2/RST3 sub-tables;
the assembler then ORs in the `r`/`s`/`t` register bits to form the emitted instruction. The `extui`
template `0x00040000` has op0=4 with op1=4 (the QRST family `EXTUI` selector).

### 2.3 Immediate operand semantics

Read from the per-op immediate descriptors and confirmed by the device round-trip (§7):

| op | imm type | semantics | range |
|----|----------|-----------|-------|
| `ADDI` | `simm8` | `sext(imm8)` | −128 … 127 |
| `ADDMI` | `simm8x256` | `sext(imm8) << 8` | −32768 … 32512, step 256 |
| `MOVI` | `simm12b` | `sext(imm12)` | −2048 … 2047 |
| `SLLI` | `msalp32` | shift amount = `32 − imm` | imm 1 … 31 |
| `SRLI` | `s` | 4-bit unsigned amount | 0 … 15 |
| `SRAI` | `sargt` | 5-bit unsigned amount (split field) | 0 … 31 |
| `EXTUI` | `sae` + `op2p1` | start (5-bit, split) + width−1 | start 0 … 31, width 1 … 16 |
| `SSAI` | `sas` | 5-bit immediate → `SAR` | 0 … 31 |

The `SLLI` `32 − imm` convention is verified by an amount sweep in §6; the `ADDMI` step-256, `MOVI`
±2048 and `SSAI` 0–31 ranges are verified at their boundaries in §7.

---

## 3. Per-opcode reference

Common to all 39: scalar 1×i32 (AR); **no** `CPENABLE` state-in, **no** `Coprocessor1Exception`
(plus the per-op `SAR`/`BR`/`THREADPTR` noted). Timing is from the `*_inst_*_issue` scoreboard bodies
in `libcas-core.so` — each registers its operand USE/DEF at a pipeline stage encoded as the `$imm`
passed to the scoreboard callback (see §5).

### 3.1 `ADD` / `SUB` / `ADDX`n / `SUBX`n — `xt_iclass_addsub`

```c
// OUT[arr:AR]  IN[ars:AR, art:AR] ; RST0 op2 selects op & shift
// real value primitive: module__xdref_addw_32_32_32 / subw_* @libfiss-base.so
arr = (ars +  art) & 0xFFFFFFFF;            // ADD   op2=8
arr = ((ars << 1) + art) & 0xFFFFFFFF;      // ADDX2 op2=9   (shift-add address form)
arr = ((ars << 2) + art) & 0xFFFFFFFF;      // ADDX4 op2=10
arr = ((ars << 3) + art) & 0xFFFFFFFF;      // ADDX8 op2=11
arr = (ars -  art) & 0xFFFFFFFF;            // SUB   op2=12
arr = ((ars << k) - art) & 0xFFFFFFFF;      // SUBX{2,4,8}  k=log2(n)
```

Pure two's-complement **wrap** — base Xtensa has no carry/overflow flag and no condition codes.
The `ADDXn`/`SUBXn` forms are the array-index/address builders (`base + i*elemW`). Timing: `arr`
DEF @stage 4; `ars`, `art` USE @4 — a 1-cycle E-stage ALU op with full forwarding. Example §7.1.

### 3.2 `ADDI` / `ADDMI` — `xt_iclass_addi` / `xt_iclass_addmi`

```c
// OUT[art:AR]  IN[ars:AR, imm8]  (RRI8: t=dst, s=src)
art = (ars + sext8(imm8)) & 0xFFFFFFFF;          // ADDI  (-128..127)
art = (ars + (sext8(imm8) << 8)) & 0xFFFFFFFF;   // ADDMI (step 256)
```

`ADDMI` is the coarse-grained add used to reach a stack/frame offset in one instruction when the
displacement is a multiple of 256. Timing: `art` DEF @4; `ars` USE @4, imm @3. Example §7.2.

### 3.3 `NEG` / `ABS` — `xt_iclass_neg`

```c
// OUT[arr:AR]  IN[art:AR] ; RT0 s={NEG:0, ABS:1}
// module__xdref_neg_32_32 @0x5c0c50 :  neg %esi ; store      -> arr = -art
// module__xdref_abs_32_32 @0x5c0b70 :  branchless two's-comp abs
arr = (-art) & 0xFFFFFFFF;                        // NEG ; NEG(0x80000000) == 0x80000000 (wraps)
arr = (art >> 31) ? ((-art) & 0xFFFFFFFF) : art;  // ABS ; ABS(0x80000000) == 0x80000000
// the fiss leaf computes ABS as: m = art >> 31 (arith) ; arr = (art ^ m) - m
```

The `module__xdref_abs_32_32` body is the canonical sign-mask form
(`eax = src>>31; ecx = −eax; esi ^= ecx; eax += esi` = `(x ^ (x>>31)) − (x>>31)`), so `ABS` wraps at
`INT32_MIN` like `NEG` — **no saturation**. Timing: `arr` DEF @4; `art` USE @4. Example §7.3.
`[HIGH/OBSERVED — fiss leaf disasm]`

### 3.4 `AND` / `OR` / `XOR` — `xt_iclass_bit`

```c
// OUT[arr:AR]  IN[ars:AR, art:AR] ; RST0 op2={AND:1, OR:2, XOR:3}
arr = ars & art;   arr = ars | art;   arr = ars ^ art;   // bitwise, full 32b
```

Timing: `arr` DEF @4; `ars`, `art` USE @4.

### 3.5 Immediate shift — `SLLI` / `SRLI` / `SRAI`

```c
// SLLI OUT[arr] IN[ars, msalp32] ; SRLI/SRAI OUT[arr] IN[art, imm]
// RST1 op2={SLLI:0, SRAI:2, SRLI:4}
arr = (ars << (32 - imm_field)) & 0xFFFFFFFF;  // SLLI  shift = 32 - encoded field (see §6)
arr = (uint32)art >> s;                        // SRLI  logical, s in 0..15
arr = (int32) art >> sargt;                    // SRAI  arithmetic, sargt in 0..31
```

The `SLLI` amount-encoding QUIRK is the prime gotcha — the encoded field is `32 − amount`, proven by
the sweep in §6. Timing: `arr` DEF @4; src USE @4, imm @3. Example §7.4.

### 3.6 Dynamic shift — `SLL` / `SRL` / `SRA` / `SRC` + `SAR` setup

The dynamic shifters route the shift amount through the 6-bit `SAR` special register, loaded
separately by an `SSx` op — the classic Xtensa "`ssr`/`ssl`/`ssai` then `sll`/`srl`/`sra`/`src`"
idiom.

```c
// shift ops (RST1): read SAR, write arr
arr = (ars << (32 - SAR)) & 0xFFFFFFFF;        // SLL  op2=10, t=0   (left by SAR)
arr = (uint32)art >> SAR;                      // SRL  op2=9,  s=0   (logical right)
arr = (int32) art >> SAR;                      // SRA  op2=11, s=0   (arithmetic right)
arr = (uint32)(((uint64)ars << 32 | art) >> SAR);  // SRC op2=8 : 64-bit funnel, take low 32
// module__xdref_sll_u_32_32_5 @0x5c0000 : ecx=amount ; shl %cl,value ; store

// SAR setup (ST1 group ; no AR destination, writes SAR):
SAR = ars & 0x1F;            // SSR   r=0  (set for right shift)
SAR = 32 - (ars & 0x1F);     // SSL   r=1  (set for left shift)
SAR = (ars & 0x3) * 8;       // SSA8L r=2  (byte align, little-endian merge)
SAR = 32 - (ars & 0x3) * 8;  // SSA8B r=3  (byte align, big-endian merge)
SAR = sas;                   // SSAI  r=4  (5-bit immediate, 0..31)
```

`SRC` is the 64-bit **funnel** (`{ars:art} >> SAR`, low 32) — the primitive behind unaligned 32-bit
load assembly: load two aligned words, set `SAR` from the byte offset via `SSA8L`/`SSA8B`, then
`SRC` extracts the straddling word. The `module__xdref_sll_u_32_32_5` body confirms the shift
primitive is `value << (amount & 0x1f)`. Timing: shift ops — `arr` DEF @4, `SAR`+src USE @4; `SSx` —
`SAR` DEF @4, `ars`/`sas` USE @4/@3. Example §7.5.

### 3.7 `EXTUI` — `xt_iclass_exti`

```c
// OUT[arr:AR]  IN[art:AR, sae:start(5b,split), op2p1:width(1..16)] ; QRST op1=4
arr = (art >> sae) & ((1u << op2p1) - 1);   // unsigned bit-field extract, zero-extended
```

`EXTUI` is the **only** bit-field extract in the base ISA: zero-extended, width 1–16, start 0–31.
Wider or signed extracts compose from `SLLI`+`SRAI`. Timing: `arr` DEF @4; `art` USE @4, imm @3.
Example §7.6.

### 3.8 `MOVI` — `xt_iclass_movi`

```c
// OUT[art:AR]  IN[simm12b] ; LSAI r=10
art = sext12(imm12);   // -2048 .. 2047
```

`MOVI` is a **fast immediate**: the `MOVI` issue body registers its result DEF at **stage 1**, not 4
— there is no register read on the input side, so the constant is available a full ALU depth earlier
than an arithmetic result. Constants outside ±2047 need the two-step builders: `CONST16` (two
instructions, high-then-low 16-bit halves accumulated `result = (CONST16_hi << 16) | CONST16_lo`) or
the PC-relative literal-pool load `L32R` — both at [B26](b26-xt-ctrl.md). Timing: `art` DEF @1;
imm USE @0. Example §7.7.

### 3.9 Conditional move (compare-zero) — `MOVEQZ` / `MOVNEZ` / `MOVLTZ` / `MOVGEZ` — `xt_iclass_movz`

```c
// OUT[arr:AR]  IN[ars:AR (value), art:AR (condition)] ; RST3 op2={8,9,10,11}
// NB: arr is BOTH output AND implicit input (kept unchanged on a false predicate)
if (art == 0)            arr = ars;   // MOVEQZ op2=8
if (art != 0)            arr = ars;   // MOVNEZ op2=9
if ((int32)art <  0)     arr = ars;   // MOVLTZ op2=10
if ((int32)art >= 0)     arr = ars;   // MOVGEZ op2=11
```

These are branch-free predicated AR writes — the compiler's if-conversion primitive. The destination
`arr` is a read-modify operand: on a false predicate it retains its prior value, so the scoreboard
lists `arr` as an input as well as an output. Timing: `arr` DEF @4; `ars`, `art` USE @4. Example §7.8.

### 3.10 Boolean conditional move — `MOVT` / `MOVF` — `xt_iclass_bmove`

```c
// OUT[arr:AR]  IN[ars:AR, bt:BR(1-bit)] ; RST3 op2={MOVF:12, MOVT:13} ; t-field selects BR
if (bt == 1)  arr = ars;   // MOVT  (else arr unchanged)
if (bt == 0)  arr = ars;   // MOVF
```

The predicate is a 1-bit boolean from the `BR` register file (selected by the `t` field), the
companion to the compare-and-branch booleans of B28. Timing: `arr` DEF @4; `ars`, `bt` USE @4.
Example §7.9.

### 3.11 Set-on-less-than — `SALT` / `SALTU` — `xt_iclass_salt`

```c
// OUT[arr:AR]  IN[ars:AR, art:AR] ; RST2 op2={SALTU:6, SALT:7}
arr = ((int32) ars < (int32) art)  ? 1 : 0;   // SALT  (signed)
arr = ((uint32)ars < (uint32)art)  ? 1 : 0;   // SALTU (unsigned)
```

Materialises a branch-free 0/1 comparison into an AR (the LX "set-less-than" added for
if-conversion). Signedness is a **distinct decode** (op2 6 vs 7), never a runtime mode. Timing:
`arr` DEF @4; `ars`, `art` USE @4. Example §7.10.

### 3.12 Thread pointer — `RUR.THREADPTR` / `WUR.THREADPTR`

```c
// RUR.THREADPTR OUT[arr:AR] IN[THREADPTR]  : arr = THREADPTR   (read user reg #231)
// WUR.THREADPTR OUT[THREADPTR] IN[art:AR]  : THREADPTR = art   (write user reg #231)
```

`THREADPTR` is user register **#231 = 0xE7** — the thread-local-storage base, the only UR touched in
this batch. In the `RUR.THREADPTR` template `0x00e30e70` the UR number packs across the `s`/`t`
nibbles (`(s<<4)|t = (0xe<<4)|0x7 = 0xE7`); in the `WUR.THREADPTR` template `0x00f3e700` the byte
`0xe7` is literally visible. The CRT/startup at [boot-reset.md](../../uarch/boot-reset.md) is the
primary writer (initializing the per-thread TLS base before the custom-op body runs). Timing: AR
result / UR write at the E-stage. Example §7.11. `[HIGH/OBSERVED — template + device round-trip;
MED/INFERRED — exact UR-access stage]`

---

## 4. Semantics notes — signedness, wrap, and `SAR` `[HIGH/OBSERVED + MED/INFERRED]`

* **Wrap everywhere.** The base-Xtensa integer ALU is pure two's-complement modulo 2³². No
  saturation, no carry/overflow flag, no condition-code register. (The saturating/flagged arithmetic
  is the IVP vector datapath's, [B01–B03], and the `CLAMPS` TIE extension's — not here.) `NEG` and
  `ABS` both wrap `INT32_MIN` to itself, as the `module__xdref_abs_32_32` body shows.
* **One `SAR`, three setup verbs + immediate.** The dynamic shifts read the single 6-bit `SAR`;
  `SSR`/`SSL` load it from `ars[4:0]` (right) or `32 − ars[4:0]` (left); `SSA8L`/`SSA8B` load the
  byte-alignment amount `8·(ars[1:0])` (or its `32 −` complement) for unaligned-access merge; `SSAI`
  loads a 5-bit immediate. `SLL` uses the internal `(32 − SAR)` convention so that "`SSL ax; SLL …`"
  shifts *left* by the value in `ax`.
* **`EXTUI` is the sole bit-field extract**, zero-extended only.
* **Conditional moves keep the destination on a false predicate** — `arr` is an implicit input.
  Reimplementations that allocate `arr` write-only will mis-model `MOVcc`/`MOVT`/`MOVF`.

---

## 5. Timing summary

The `*_inst_*_issue` scoreboard bodies in `libcas-core.so` extract each operand's AR-field and
register a USE/DEF at a fixed pipeline stage. The `F0_F0_S0_LdSt_4_inst_ADD_issue @0x1188b60` body
extracts the `r` (dest) field via `(word<<0x14)>>0x1c` = bits[15:12], the `s`/`t`
sources via `& 0xf`, and passes `$0x4` (stage 4) to the scoreboard callback `opnd_sem_AR_addr` for
**every** operand — confirming the 1-cycle E-stage ALU model. The `MOVI` body passes `$0x1` for its
result.

| op-class | inputs USE @stage | result DEF @stage | latency |
|----------|-------------------|-------------------|---------|
| `add`/`sub`/`addx`/`subx` | `ars`,`art` @4 | `arr` @4 | 1-cyc ALU, bypassable |
| `and`/`or`/`xor` | `ars`,`art` @4 | `arr` @4 | " |
| `neg`/`abs` | `art` @4 | `arr` @4 | " |
| `slli`/`srli`/`srai` | src @4, imm @3 | `arr` @4 | " |
| `sll`/`srl`/`sra`/`src` | `SAR` @4, src @4 | `arr` @4 | " (after `SAR` ready) |
| `ssl`/`ssr`/`ssa8x`/`ssai` | `ars`/`sas` @4/@3 | `SAR` @4 | `SAR` write |
| `extui` | `art` @4, sae/op2p1 @3 | `arr` @4 | " |
| `movi` | imm @0 | `art` @1 | **fast immediate** (DEF @1) |
| `moveqz`/`movnez`/`movltz`/`movgez` | `ars`/`art`/`arr` @4 | `arr` @4 | 1-cyc predicated |
| `movt`/`movf` | `ars`/`bt`/`arr` @4 | `arr` @4 | 1-cyc predicated |
| `salt`/`saltu` | `ars`,`art` @4 | `arr` @4 | 1-cyc |
| `rur`/`wur.threadptr` | `art` / `THREADPTR` | `arr` / `THREADPTR` | UR access (E-stage) |

Scalar ALU results land at the M-stage boundary (4) with full E-stage forwarding, so dependent ops
issue back-to-back. `MOVI`'s DEF @1 reflects its immediate-only datapath.

---

## 6. QUIRK / GOTCHA — the encoding traps a reimplementation will hit

> **QUIRK — `SLLI` encodes its amount as `32 − amount`, split across two fields.** A device-oracle
> sweep (`xtensa-elf-as --no-transform`):
> ```
> slli a4,a5,1   -> 1145f0   (encoded amount field reads 31)
> slli a4,a5,7   -> 114590   (reads 25)
> slli a4,a5,16  -> 114500   (reads 16)
> slli a4,a5,31  -> 014510   (reads 1, top bit set)
> ```
> The encoded field is `32 − amount`, so amount 0 is **not encodable** by `SLLI` (a no-op left shift
> uses `OR`/`MOV`). `SRLI` (4-bit, 0–15) and `SRAI` (5-bit split, 0–31) encode the amount *directly*:
> `srli a4,a6,0 = 414060`, `srli a4,a6,15 = 414f60`; `srai a4,a6,0 = 214060`,
> `srai a4,a6,31 = 314f60`. A reimplementation that uses the same amount-field convention for all
> three immediate shifts will silently mis-shift `SLLI`.

> **QUIRK — `SSL`/`SSR`/`SSA8x` carry no shift amount in the instruction.** They take the amount from
> the *register* `ars` at run time and write it to `SAR`; the actual shift is a separate
> `SLL`/`SRL`/`SRA`. There is no single dynamic-shift-by-register instruction — the amount is always
> staged through `SAR`. `SSL a5 = 401500`, `SSR a5 = 400500`, `SSA8L a5 = 402500`,
> `SSA8B a5 = 403500`.

> **GOTCHA — `CONST16` is *one* 24-bit op carrying *16* immediate bits; a 32-bit constant is *two*
> `CONST16`s.** Device round-trip: `const16 a4,0x1234 = 123444`, `const16 a4,0x5678 = 567844` — each
> is a single instruction with the 16-bit immediate in bytes [0:1] and op0=4. The assembler/compiler
> emits the pair high-then-low and the hardware accumulates `a4 = (a4 << 16) | imm` across the two,
> so the canonical 32-bit-constant idiom is `const16 a4, HI ; const16 a4, LO`. `MOVI` (this page)
> handles only ±2047; for anything wider the firmware uses the `CONST16` pair or a `L32R` literal-pool
> load (both [B26](b26-xt-ctrl.md)). Reimplementing the immediate-load path requires all three.

> **NOTE — the assembler defaults to the 16-bit `.N` density form.** Without `--no-transform`,
> `add a4,a5,a6` assembles to the **2-byte** `add.n` (`6a 45`), not the 3-byte `add` (`80 45 60`) —
> the relaxer prefers the narrow encoding when the operands fit. The `.N` density ops (`ADD.N`/
> `ADDI.N`/`MOV.N`/`MOVI.N`, on the `Inst16a`/`Inst16b` 16-bit formats) are *separate* `<SEMANTIC>`
> groups counted at [B26](b26-xt-ctrl.md); this page documents the 24-bit standalone forms, which a
> disassembler emits only for amounts/registers the narrow form cannot express, or under
> `--no-transform`.

---

## 7. Worked bit-patterns — round-tripped through the device oracle

Oracle: `xtensa-elf-as --no-transform` → `.o` → `xtensa-elf-objdump -d`
(`XTENSA_CORE=ncore2gp`, `XTENSA_SYSTEM=…/XtensaTools/config`). Wire bytes little-endian; the bytes
below are copy-exact from that disassembly.

```
7.1  add    a4,a5,a6 = 80 45 60   ; a4 = (a5 + a6) mod 2^32
     addx2..addx8     = 90/a0/b0 45 60     sub = c0 45 60
     subx2..subx8     = d0/e0/f0 45 60     (top byte enumerates op2/Xn)
7.2  addi   a4,a5,13  = 0d c5 42   ; a4 = a5 + 13
     addi   a4,a5,-1  = ff c5 42   ; imm 0xff = -1
     addmi  a4,a5,256 = 01 d5 42   ; a4 = a5 + (1<<8) = a5 + 256
     addmi  a4,a5,-32768 = 80 d5 42 (imm 0x80 -> (sext<<8) = -32768)
     addmi  a4,a5, 32512 = 7f d5 42 (imm 0x7f -> 32512)
7.3  neg    a4,a6     = 60 40 60   ; a4 = -a6
     abs    a4,a6     = 60 41 60   ; a4 = |a6|  (s-field 0->1)
7.4  slli   a4,a5,7   = 11 45 90   ; a4 = (a5 << 7)        (field = 32-7 = 25)
     srli   a4,a6,5   = 41 45 60   ; a4 = a6 >>logical 5
     srai   a4,a6,9   = 21 49 60   ; a4 = a6 >>arith   9
7.5  src    a4,a5,a6  = 81 45 60   ; a4 = ({a5:a6} 64b >> SAR)[31:0]  (funnel)
     sll    a4,a5     = a1 45 00   ; a4 = a5 << SAR-derived
     srl    a4,a6     = 91 40 60   ; sra a4,a6 = b1 40 60
     ssl a5 = 40 15 00   ssr a5 = 40 05 00   ssa8l a5 = 40 25 00
     ssa8b a5 = 40 35 00   ssai 0 = 40 40 00   ssai 31 = 40 4f 10
7.6  extui  a4,a6,5,8 = 74 45 60   ; a4 = (a6 >> 5) & 0xFF
7.7  movi   a4,100    = 64 a0 42   ; a4 = 100      (result DEF @stage 1)
     movi   a4,-2048  = 00 a8 42   ; objdump prints 0xfffff800
     movi   a4, 2047  = ff a7 42   ; (simm12b boundary)
7.8  moveqz a4,a5,a6  = 83 45 60   ; if a6==0: a4=a5 else keep
     movnez/ltz/gez   = 93/a3/b3 45 60     (op2 enumerates the condition)
7.9  movt   a4,a5,b2  = d3 45 20   ; if b2==1: a4=a5 else keep
     movf   a4,a5,b2  = c3 45 20   ; if b2==0: a4=a5   (t-field selects BR b2)
7.10 salt   a4,a5,a6  = 72 45 60   ; a4 = (int)a5 < (int)a6 ? 1:0
     saltu  a4,a5,a6  = 62 45 60   ; unsigned compare
7.11 rur.threadptr a4 = e3 4e 70   ; a4 = THREADPTR (UR 231=0xE7)
     rur.threadptr a7 = e3 7e 70   ; (only the r/dest nibble changes)
     wur.threadptr a4 = f3 e7 40   ; THREADPTR = a4  (byte 0xe7 = UR#231)
```

All 41 representatives (the 39 mnemonics plus the second `addi`/`movi`/`addmi` boundary forms)
assembled and disassembled identically; the `.N` relaxation default and the `SLLI` amount-field
were checked adversarially against this oracle.

---

## 8. Self-verification ledger (5 strongest claims, adversarially re-tested)

| # | claim | test | verdict |
|---|-------|------|---------|
| 1 | `ADD`=op2 8 … `SUB`=op2 12 selector consts | `objdump -d` the `Slot_inst_encode` thunks: `add`=`0x800000`, `sub`=`0xc00000`, `and`=`0x100000`, `salt`=`0x720000`, `extui`=`0x040000` | **PASS** — templates match the §2.2 table byte-for-byte |
| 2 | `SLLI` amount = `32 − encoded field` | device sweep amount 1/7/16/31 → encoded 31/25/16/1 | **PASS** — confirmed; amount 0 not encodable |
| 3 | `THREADPTR` = UR #231 (0xE7) | `rur` template `0xe30e70` → `(s<<4)\|t`=0xE7; `wur` template `0xf3e700` has literal byte `e7`; device `rur.threadptr a4`=`e3 4e 70` | **PASS** |
| 4 | scalar roster = 469 (vs 1065 vector); this batch = 39 | `nm \| rg -oP 'Opcode_\K\w+(?=_Slot_)' \| sort -u` = 1534, filter `^ivp_` → 469/1065; iclass enumeration → 39 | **PASS** |
| 5 | scalar ALU result DEF @stage 4; `MOVI` DEF @1 | disasm `F0_F0_S0_LdSt_4_inst_ADD_issue @0x1188b60` (passes `$0x4` per operand) and the `MOVI` issue body (`$0x1`) | **PASS** |

> **CORRECTION / DIVERGENCE LEDGER** (every entry `[HIGH/OBSERVED]`).
> 1. **Batch size = 39, not the "~80 (subset of 120)" planning figure.** The 30-batch plan's
>    early estimate over-counted by lumping the density `.N` variants and the `MUL16`/`MUL32` family
>    into this batch; those are *separate* `<SEMANTIC>` groups correctly deferred to B26. The binding
>    rule (per-`<SEMANTIC>`-singleton membership) fixes the disjoint full-width arith/logic/shift +
>    `THREADPTR` roster at **39**.
> 2. **The TIE field named `op2` is the *architectural* RST selector, not the emitted wire's high
>    nibble.** The encode template `ADD = 0x00800000` has op2=8 with `r`/`s`/`t` zeroed; the emitted
>    `add a4,a5,a6` wire `80 45 60` has top byte `0x60` because the assembler ORs in the register
>    fields. Both are consistent; `xtensa-elf-objdump` is the authoritative decoder and was used as
>    the ground truth for every byte pattern in §7.
> 3. **`CONST16`/`L32R` appear on this page but belong to B26.** They are documented in §3.8/§6
>    because the `MOVI` immediate-load path is incomplete without them, but the partition assigns the
>    32-bit-constant builders to the load/immediate batch [B26](b26-xt-ctrl.md). No double-count: they
>    are *not* in the §1 roster of 39.

---

## 9. Cross-references

* [ISA Batch 26 — base-Xtensa ld/st / branch / density / MUL32 / div](b26-xt-ctrl.md) — the sibling
  control batch: the loads/stores `MOVI` builds addresses for, the `.N` density variants, the integer
  multiplier, and `CONST16`/`L32R`.
* [ISA Batch 27 — System / SR / RegWindow / Sync](b27-xt-system.md) — the special-register and
  register-window machinery (`RSR`/`WSR`/`MOVSP`/`ENTRY`) that the AR window this batch operates on
  depends on.
* [Boot / Reset Sequence + Startup Config](../../uarch/boot-reset.md) — the CRT/startup that
  initializes the AR window and writes `THREADPTR` before the custom-op body runs.
* [Template & Partition](template-and-partition.md) — the 30-batch classifier and the scalar/vector
  split.
* [ISA Batch 21 — Select / Shuffle / Compress](b21-select-shuffle.md) — the preceding (vector)
  reference page and the IVP-axis side of the coprocessor-gate wall.
