# ISA Batch 28 — base-Xtensa exception-dispatch / booleans / loop / min-max (the control tail)

This is the **fourth base-Xtensa scalar batch**, and the one that grounds the firmware control
spine. Everything on the IVP vector pages [B01–B24](b21-select-shuffle.md) rides the 512-bit
coprocessor; the scalar core was opened at [B25](b25-xt-core.md) (AR arith/logic/shift), continued
through [B26](b26-xt-ctrl.md) (ld/st, branch, density, `MUL32`, scalar-div) and [B27](b27-xt-system.md)
(system SRs, the windowed-call ABI, sync fences). **B28 is the control / dispatch tail**: the integer
compare-select (`MIN`/`MAX`/`MINU`/`MAXU`, plus the adjacent saturating `CLAMPS`), the **boolean
register file** logic and lane-reduce (`ANDB`…`XORB`, `ALL4`/`ANY4`/`ALL8`/`ANY8`) with its
`BT`/`BF` boolean branch, the **zero-overhead loop** construct (`LOOP`/`LOOPNEZ`/`LOOPGTZ` + the
wide-15 forms + the `LBEG`/`LEND`/`LCOUNT` loop special registers), the **privileged
register-window dispatch** primitives the window-exception handler issues (`ROTW`/`SETW`/`SPILLW`/
`TOSSW`/`EXIT`), the **branch-predictor BTB** read/write (`RBTB0-2`/`WBTB0-2`), and the
**exception-dispatch / debug-trap miscellany** (`EXCW`/`MEMW`/`ILL`/`ILL.N`/`SYSCALL`/`BREAK*`/
`HALT*`). These are the ops the OS, the window-spill/restore vectors, the on-chip-debug unit, and the
control plane of every vector thread actually execute. **None touch a vector lane.**

The structural wall against the IVP axis is the same as [B25](b25-xt-core.md): **every** `ivp_` op
carries `CPENABLE` state-in and raises `Coprocessor1Exception` when the coprocessor enable bit is
clear; **none** of the 51 ops on this page do. The datapath subset (min/max, the booleans, the
lane-reduce, `BT`/`BF`) is unconditionally executable with `state_in = []`, `exc = []`; the
control subset reads `PS.RING`/`InOCDMode`/`MS_DISPST` and raises *control-side* traps
(`PrivilegedException`, `WindowOverflow8`/`WindowUnderflow8`, `SyscallException`, `BreakException`,
`HaltException`) — never a coprocessor exception.

Everything below is grounded in the shipped binaries: the **encoding** from the
`Opcode_<mnem>_Slot_inst_encode` thunks in `libisa-core.so` (read byte-for-byte; the selector word is
the architectural opcode nibble with register fields zeroed); the **value semantics** by disassembling
the matching `module__xdref_*` leaf bodies in `libfiss-base.so`; the **issue timing and exception
side-effects** from the per-op `*_inst_*_issue` / `*_inst_stageN` scoreboard bodies and the named
`*Exception_exc` helpers in `libcas-core.so`; and a byte-exact **encode/decode oracle** from the
device-native `xtensa-elf-as` / `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). Every representative
round-trips through that device oracle. Confidence tags per
[the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-disasm-or-round-trip, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic and binary identity.** `libisa-core.so`
> (9 690 712 B, ET_DYN x86-64, **not stripped** — 45 198 symtab entries). Per `readelf -SW`:
> `.text` (VMA `0x312c10`) and `.rodata` (VMA `0x3b6e40`) are **VMA == file-offset**; `.data` and
> `.data.rel.ro` (VMA `0x67bb00` ↔ file `0x47bb00`) carry the per-binary delta **`0x200000`** — **not**
> libtpu's `0x400000`. The encode thunks live in `.text` (VMA == file), so `objdump -d` reads them
> directly. The `regfiles[]` / `regfile_views[]` tables sit in `.data.rel.ro`, so a raw `xxd` must use
> file offset `VMA − 0x200000` (e.g. `regfile_views[]` VMA `0x74a780` ↔ file `0x54a780`). All config
> DLLs are under `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/` (gitignored; reach with
> `fd --no-ignore` or an absolute path).

---

## 0. Headline — the count, and what is *absent*

**B28 is exactly 51 distinct mnemonics across 11 semantic groups** (§1 table). Every one round-trips
through the device `xtensa-elf-as` → `xtensa-elf-objdump` byte-for-byte; the worked bytes in
§7 are copy-exact from that disassembly. The roster is anchored to the `nm` symbol table of
`libisa-core.so`, **not** a decompile:

```
nm libisa-core.so | rg -o 'Opcode_[a-z0-9_]+_Slot' | sed 's/Opcode_//;s/_Slot//' | sort -u   # 1534 total
```

intersected with the B28 semantic-group set yields exactly 51 (§1, §8).

> **CORRECTION — this `ncore2gp` config has NO return-from-exception / interrupt-return
> architecture.** A naïve XEA2/XEA3 reading expects `RFE`/`RFI`/`RFDE`/`RFWO`/`RFWU`/`RSIL` to anchor
> the exception machine. **They do not exist here.** Both the symbol table and the device assembler
> agree:
>
> ```
> nm libisa-core.so | rg -o 'Opcode_rf[a-z0-9]*_Slot' | sort -u
>   → Opcode_rfdo_Slot   Opcode_rfr_Slot          # ONLY these two rf* ops
> xtensa-elf-as <<< 'rfe'   → Error: unknown opcode or format name 'rfe'
> xtensa-elf-as <<< 'rfwo'  → Error: unknown opcode or format name 'rfwo'
> xtensa-elf-as <<< 'rfwu'  → Error: unknown opcode or format name 'rfwu'
> xtensa-elf-as <<< 'rsil a3, 1' → Error: unknown opcode or format name 'rsil'
> ```
>
> `RFE`/`RFI`/`RFME`/`RFUE`/`RFWO`/`RFWU`/`RFDD`/`RSIL` are **all** rejected as unknown opcodes; only
> `rfdo` (debug-return) and `rfr` (read-FP-register) survive, and **both belong to
> [B29](b25-xt-core.md), not here** (each needs an operand the bare-name probe is missing). The real
> dispatch surface of this minimal Vision-Q7 LX is the **13-op misc + brk + halt set** (`EXCW`/`MEMW`/
> `ILL`/`ILL.N`/`SYSCALL`/`BREAK`/`BREAK.N`/`BREAK1`/`HALT`/`HALT.N`) plus the `SyscallException` /
> `Window*` ARG_OUT traps the window-dispatch ops raise. There is no software vector that re-enters via
> a return-from-exception opcode; the firmware exception machine is built entirely on these traps and
> the OS-issued window/loop primitives documented below. See
> [the XEA3 exception architecture](../../control/interrupt/xea3-interrupt-architecture.md) and
> [the firmware error handler](../../firmware/seq/error-handler.md) for how the trap surface is wired.
> `[HIGH/OBSERVED — nm + device-assembler refutation]`

> **NOTE — the windowed-call ABI vs the privileged window dispatch.** `ENTRY`/`RETW`/`RETW.N`/`MOVSP`
> — the *user-level* windowed call/return surface — **exist** (`nm` confirms
> `Opcode_{entry,retw,retw_n,movsp}_Slot_inst`) but are **[B27](b27-xt-system.md)**, not here. B28 owns
> the *privileged dispatch* the window-exception handler issues: `ROTW`/`SETW`/`SPILLW`/`TOSSW`/`EXIT`.
> The split rule: **B27 = the windowed-call ABI; B28 = the privileged window-management dispatch the
> OS/handler runs**. `[HIGH/OBSERVED — xt_regwin group split]`

---

## 1. Roster & count (semantic-group membership, 0 cross-overlap)

| semantic group | iclass | members | n |
|----------------|--------|---------|---|
| `xt_sem_minmax` | `xt_iclass_minmax` | `MIN MAX MINU MAXU` | 4 |
| `xt_sem_bool` | `xt_iclass_bbool1` / `bbool4` / `bbool8` | `ANDB ANDBC ORB ORBC XORB` · `ALL4 ANY4` · `ALL8 ANY8` | 9 |
| `xt_sem_bool_branch` | `xt_iclass_bbranch` | `BT BF` | 2 |
| `xt_sem_rdwr_btb` | `xt_iclass_rbtb` / `wbtb` | `RBTB0 RBTB1 RBTB2` · `WBTB0 WBTB1 WBTB2` | 6 |
| `xt_sem_core_regwin` | `xt_iclass_{rotw,setw,spillw,tossw,exit}` | `ROTW SETW SPILLW TOSSW EXIT` | 5 |
| loop core | `xt_iclass_loop` / `loopz` | `LOOP` · `LOOPNEZ LOOPGTZ` | 3 |
| `xt_wide_loop` | `xt_iclass_wloop` / `wloopz` | `LOOP.W15` · `LOOPNEZ.W15 LOOPGTZ.W15` | 3 |
| loop SRs | `rsr/wsr/xsr.{lbeg,lend,lcount}` | `RSR/WSR/XSR.LBEG` · `.LEND` · `.LCOUNT` | 9 |
| `xt_sem_misc` | `xt_iclass_{excw,memw,ill,ill.n,syscall}` | `EXCW MEMW ILL ILL.N SYSCALL` | 5 |
| `xt_sem_core_brk` | `xt_iclass_{break,break.n,break1}` | `BREAK BREAK.N BREAK1` | 3 |
| `xt_sem_core_halt` | `xt_iclass_{halt,halt_n}` | `HALT HALT.N` | 2 |
| | | **TOTAL** | **51** |

```
minmax 4 + bool 9 + bool_branch 2 + rdwr_btb 6 + core_regwin 5 + loop_core 3
       + wide_loop 3 + loop_SR 9 + misc 5 + brk 3 + halt 2  =  51
```

**Disjointness against the adjacent base batches** (each member machine-checked into exactly one
group): the full-width AR ALU/shift, conditional `MOV`cc, `SALT`/`SALTU` and `THREADPTR` are
[B25](b25-xt-core.md) — note `SALT`/`SALTU` materialise a 0/1 in an **AR** register and are *not*
boolean-file ops, whereas `ANDB`…`XORB` write the 1-bit **BR** file and live here. `L32I`/`S32I`/
branches/`CALL`/`J`/density `.N`/`MUL32`/scalar-`div` are [B26](b26-xt-ctrl.md). The non-loop SRs
(`SAR`, `PS`, `EPC`, `VECBASE`, …), `ENTRY`/`RETW`/`MOVSP`, `MEMCTL`/`PREFETCH` and the generic sync
fences are [B27](b27-xt-system.md). `SEXT`, **`CLAMPS`** (cross-referenced in §3.12 below), `NSA`/`NSAU`,
and the `RFDO`/`RFR` debug-return + cache/timer/MMU remainder are [B29](b25-xt-core.md).

> **NOTE — the loop SRs are pulled in with the construct they serve.** `LBEG`/`LEND`/`LCOUNT` are
> Xtensa special registers and would, by a generic SR sweep, fall to B27; they are placed here because
> they *are* the zero-overhead-loop subsystem — the `LOOP*` ops write them and the fetch unit reads
> them for the hardware back-edge. The `num_states` accessor (`libisa-core.so` `@0x3b6670` →
> `mov $0x51,%eax; ret`) reports **81** architectural state cells in the core config; `LBEG`/`LEND`/`LCOUNT`
> are three of them. Merging the `libisa-core-hw.so` module (its `num_states` → `mov $0x6,%eax` = 6) gives
> **87 = 81 + 6** — the merged figure the Part-2 register/identity pages cite. Read **81** for the core
> architectural-state count, **87** for the merged (incl. `libisa-core-hw`) total.

---

## 2. Common encoding model

### 2.1 Register files and fields

| file | width × count | fields | used by |
|------|---------------|--------|---------|
| **AR** | 32-bit × 64 (16 window-visible) | `arr` = `r[15:12]`, `ars` = `s[11:8]`, `art` = `t[7:4]` | min/max, BTB addr in/out, loop count src, loop-SR `art` |
| **BR** | 1-bit × 16 (`b0`…`b15`) | `br` = `r`, `bs` = `s`, `bt` = `t` (single-bit selects) | booleans, `BT`/`BF`, the ALL/ANY result |

The boolean file is `regfiles[]` index **1** (VMA `0x74a800`, file `0x54a800`): `num_bits = 1`,
`count = 16`, package `xt_booleans`, ctype `_TIE_xtbool`, `coproc = ""` (a base-ISA core file, *not*
a coprocessor file). It is documented in full at [the register-files page](../core/register-files.md).
Its four narrowed **views** live in `regfile_views[]` (VMA `0x74a780`, file `0x54a780`, stride 32, 4
entries), each `{ name, parent="BR", width }`:

| view | width field (`+0x10`) | ctype | source of |
|------|----------------------|-------|-----------|
| `BR2` | `0x02` | `_TIE_xtbool2` | (paired-bool select) |
| `BR4` | `0x04` | `_TIE_xtbool4` | `ALL4`/`ANY4` 4-aligned quad |
| `BR8` | `0x08` | `_TIE_xtbool8` | `ALL8`/`ANY8` 8-aligned octet |
| `BR16` | `0x10` | `_TIE_xtbool16` | (full-file mask view) |

> **CORRECTION — the four BR views are `BR2`/`BR4`/`BR8`/`BR16`, not `BR`/`BR2`/`BR4`/`BR8`.** Read
> byte-exact from `regfile_views[]` at file offset `0x54a780`: the four name pointers
> resolve to the `.rodata` strings `"BR2"`, `"BR4"`, `"BR8"`, `"BR16"`, each with `parent = "BR"` and
> the width field `{2,4,8,16}`. The full file is `BR` (in `regfiles[]`); the views are the strided
> sub-windows. The lane-reduce ops read a `BR4`/`BR8` view; there is no `BR1`/`BR8`-of-`BR` naming
> mismatch. `[HIGH/OBSERVED — regfile_views[] raw read]`

### 2.2 The 24-bit RRR field layout

All B28 core forms are 24-bit (3-byte) Xtensa instructions (no `.N` density except the four narrow
debug forms `ILL.N`/`BREAK.N`/`HALT.N`). Little-endian on the wire; the wire value is
`v = b0 | b1<<8 | b2<<16`:

```
op0 = v[3:0]   t = v[7:4]   s = v[11:8]   r = v[15:12]   op1 = v[19:16]   op2 = v[23:20]
```

`objdump` prints the three bytes **big-endian-grouped** in its hex column, e.g. `"434560"` ⇒ wire
bytes `60 45 43` ⇒ `v = 0x434560`. The §7 decodes show both spellings.

### 2.3 Selector constants — read from the encode thunks

Each `Opcode_<mnem>_Slot_inst_encode` thunk in `libisa-core.so` is a one-line x86 body
`movl $word0,(%rdi); ret` (bytes `C7 07 <imm32le> C3`). `word0` is the **architectural opcode with
the register fields zeroed** — the bare selector. The assembler ORs the operand fields in. Read
byte-for-byte:

| op | `Slot_inst` VMA | template `word0` | decode |
|----|-----------------|------------------|--------|
| `MIN` | `0x33e5f0` | `0x00430000` | RRR/CUST0 op2=4 |
| `MAX` | `0x33e250` | `0x00530000` | op2=5 |
| `MINU` | `0x33e7c0` | `0x00630000` | op2=6 |
| `MAXU` | `0x33e420` | `0x00730000` | op2=7 |
| `ANDB` | `0x33dba0` | `0x00020000` | RST2 op2=0 |
| `ANDBC` | `0x33dbb0` | `0x00120000` | op2=1 |
| `ORB` | `0x33dbc0` | `0x00220000` | op2=2 |
| `ORBC` | `0x33dbd0` | `0x00320000` | op2=3 |
| `XORB` | `0x33dbe0` | `0x00420000` | op2=4 |
| `ANY4` | `0x33dc00` | `0x00008000` | r=8 |
| `ALL4` | `0x33dbf0` | `0x00009000` | r=9 |
| `ANY8` | `0x33dc20` | `0x0000a000` | r=10 |
| `ALL8` | `0x33dc10` | `0x0000b000` | r=11 |
| `LOOP` | `0x33a9a0` | `0x00008076` | op0=6, r=8 |
| `LOOPNEZ` | `0x33a9c0` | `0x00009076` | r=9 |
| `LOOPGTZ` | `0x33a9b0` | `0x0000a076` | r=10 |
| `RBTB0` | `0x33efc0` | `0x00508000` | RST r=8 |
| `WBTB0` | `0x33ef90` | `0x00500000` | RST r=0 |
| `ROTW` | `0x33d230` | `0x00408000` | r=8 s=0 |
| `SETW` | `0x33d280` | `0x00408100` | r=8 s=1 |
| `SPILLW` | `0x33d260` | `0x00003600` | r=3 s=6 |
| `TOSSW` | `0x33d270` | `0x00003700` | r=3 s=7 |
| `EXIT` | `0x33d290` | `0x000001b0` | density-window m=2 s=1 n=3 |
| `EXCW` | `0x338610` | `0x00002080` | t=8 |
| `MEMW` | `0x33cb90` | `0x000020c0` | t=12 |
| `SYSCALL` | `0x338620` | `0x00005000` | s=0 |
| `ILL` | `0x33a410` | `0x00000000` | all-zero |
| `BREAK` | `0x33d980` | `0x00004000` | r=4 |
| `BREAK1` | `0x33d990` | `0x000000f5` | m=3 n=3 |
| `HALT` | `0x338630` | `0x00005200` | s=2 |

Loop-SR `RSR`/`WSR`/`XSR` carry the SR number in the `sr` byte: **`LBEG = 0`, `LEND = 1`,
`LCOUNT = 2`** (confirmed by the §7.7 round-trip `030040`/`030140`/`030240`).

> **NOTE — every datapath op (min/max, bool) also has the full FLIX slot-placement set.** Beyond the
> 24-bit `Inst` form, the bundler can co-issue min/max/bool next to a vector op via `F0…F11`/`N0…N2`
> placements, each with its own template. E.g. for `MIN`: `F11_S1_ALU` template `0x000a4000`
> (`@0x33e4c0`), `F0_S0_LdSt` `0x10f67000` (`@0x33e480`); for `MAX`: `F11_S1_ALU` `0x0009f000`,
> `F0_S0_LdSt` `0x10f65000`. The selector bits move per format but the architectural opcode is the
> same. This page documents the **24-bit standalone forms** — the ones a control thread executes
> outside a bundle.

---

## 3. Per-opcode reference

Common to all 51: `LANES` = scalar `1×i32` (AR) for min/max/btb/loop/clamps, `1×i1` (BR) for the
booleans; `STATE`/`EXC` per §6; `TIMING` from the `libcas-core.so` `*_inst_stageN` scoreboard chain
(stage = pipeline cycle). Pipeline frame (per the `INSTR_SCHEDULE` model): `rstage = 0`, `estage = 3`,
`mstage = 4`, `wstage = 6`. **No `CPENABLE` / no `Coprocessor1Exception` anywhere on this page.**

### 3.1 `MIN` / `MAX` / `MINU` / `MAXU` — integer compare-select

```
OPERANDS  OUT[ arr : AR(r) ]  IN[ ars : AR(s), art : AR(t) ]
ENC       24-bit RRR, op2 = { MIN 4, MAX 5, MINU 6, MAXU 7 }; r=dst s=src1 t=src2

SEM  // module__xdref_min_32_32_32 @ libfiss-base.so 0x5c05e0 (signed forms: MSB-flip trick)
     MIN  : arr = ( (int32)ars  <  (int32)art ) ? ars : art    // signed min
     MAX  : arr = ( (int32)ars  >  (int32)art ) ? ars : art    // signed max
     MINU : arr = ( (uint32)ars <  (uint32)art) ? ars : art    // unsigned min
     MAXU : arr = ( (uint32)ars >  (uint32)art) ? ars : art    // unsigned max
```

The signed value function `module__xdref_min_32_32_32` (@`0x5c05e0`) does **not** issue a signed
compare. It biases both operands by flipping the sign bit, then runs an *unsigned* compare + `cmov`,
disassembled byte-for-byte:

```asm
; esi = ars, edx = art, (%rcx) = arr
mov   %esi,%r8d ; mov %esi,%eax ; mov %edx,%edi
not   %r8d
and   $0x7fffffff,%eax      ; eax  = ars & 0x7fffffff   (clear sign of ars)
and   $0x7fffffff,%edi      ; edi  = art & 0x7fffffff   (clear sign of art)
and   $0x80000000,%r8d      ; r8d  = (~ars) & 0x80000000 (FLIP sign of ars)
or    %eax,%r8d             ; r8d  = ars with sign bit inverted
mov   %edx,%eax ; not %eax ; and $0x80000000,%eax ; or %edi,%eax  ; eax = art sign-flipped
cmp   %eax,%r8d
cmovae %edx,%esi            ; if (flip(ars) >= flip(art)) arr = art  else keep ars
mov   %esi,(%rcx)
ret
```

i.e. `flip(x) = (x ^ 0x80000000)`; the unsigned compare of the flipped operands reproduces a signed
compare, and `cmovae` selects `art` (the larger-or-equal arm). The **unsigned** forms are the bare
two-instruction body — `module__xdref_maxu_32_32_32` (@`0x5c0770`) is literally
`cmp %edx,%esi ; cmovb %edx,%esi ; mov %esi,(%rcx) ; ret` — no sign-flip;
`module__xdref_minu_32_32_32` (@`0x5c08d0`) is the same shape with the *strict* `cmova` arm.
`MIN`/`MAX` are the only
full-width integer compare-**select** ops in the base scalar ISA (the LX `min/max` option); contrast
the [B25](b25-xt-core.md) `SALT`/`SALTU` (which materialise a 0/1 boolean into AR) and the `MOV`cc
conditional moves (which predicate an AR write). Signedness is a *decode* choice (`op2`), never a
runtime mode.

```
TIMING  arr DEF @5; ars/art USE @4; opcode @3.  2-cycle compare-select — one stage later than the
        B25 single-cycle ALU (DEF@4). The libcas-core scoreboard chain confirms it: the x24 issue
        function x24_Inst_0_inst_MAX_issue (@0x1677ab0) drives MAX_inst_stage0 … stage5 (six stages,
        DEF lands at stage 5). Bypassable. No flags, no saturation.  [HIGH/OBSERVED]
```

> **QUIRK — the tie arm differs signed vs unsigned (observationally exact).** The signed forms use a
> *non-strict* `cmov` (`cmovae`/`cmovbe`), so equal operands take the `cmov` arm and select **`art`**;
> the unsigned forms use a *strict* `cmov` (`cmova`/`cmovb`), so equal operands fall through and keep
> **`ars`**. The returned *value* is identical either way (the operands are equal), so this is exact —
> but pin it if you reimplement against a model with a fixed tie-resolution rule. `[HIGH/OBSERVED —
> signed `cmovae`/`cmovbe` vs unsigned `cmova`/`cmovb` arm; values equal so behaviourally exact.]`

Worked: §7.1.

### 3.2 Boolean logic `ANDB` / `ANDBC` / `ORB` / `ORBC` / `XORB` — `xt_iclass_bbool1`

```
OPERANDS  OUT[ br : BR(r) ]  IN[ bs : BR(s), bt : BR(t) ]   (single-bit boolean regs)
ENC       24-bit RRR, RST2 subtree, op2 = { ANDB 0, ANDBC 1, ORB 2, ORBC 3, XORB 4 }; r/s/t ∈ b0..b15

SEM  // module__xdref_{andb,orb,xorb}_* @ libfiss-base.so
     ANDB  : br = bs & bt
     ANDBC : br = bs & ~bt        // "and with complement of bt"
     ORB   : br = bs | bt
     ORBC  : br = bs | ~bt        // "or with complement of bt"
     XORB  : br = bs ^ bt
```

Pure 1-bit boolean-register ops. They combine the AR-compare and vector-mask predicates that feed
`BT`/`BF` and the IVP mask machinery — the scalar control-side mirror of the vector `vbool` masks
(the [B01–B03](b21-select-shuffle.md) vector compares produce `vbool` masks; the booleans + `ALL`/
`ANY` + `BT`/`BF` turn a mask into a scalar control-flow decision).

```
TIMING  br DEF @4; bs/bt USE @4; opcode @3.  1-cycle bool.  [HIGH/OBSERVED]
```

Worked: §7.2.

### 3.3 Boolean lane-reduce `ALL4` / `ANY4` / `ALL8` / `ANY8` — `xt_iclass_bbool4`/`bbool8` `[HIGH/OBS]`

```
OPERANDS  ALL4/ANY4 : OUT[ bt : BR(t) ]  IN[ bs4 : BR4(s) ]   (4-aligned quad)
          ALL8/ANY8 : OUT[ bt : BR(t) ]  IN[ bs8 : BR8(s) ]   (8-aligned octet)
ENC       24-bit RRR, r-field = { ANY4 8, ALL4 9, ANY8 10, ALL8 11 }; s-field carries the aligned
          group base. objdump prints the full span, e.g. "all4 b4, b8:b9:b10:b11".

SEM  ALL4 : bt = bs4[0] & bs4[1] & bs4[2] & bs4[3]    // AND-reduce 4 (BR4 view)
     ANY4 : bt = bs4[0] | bs4[1] | bs4[2] | bs4[3]    // OR-reduce 4
     ALL8 : bt = AND of the 8 bools                   // AND-reduce 8 (BR8 view)
     ANY8 : bt = OR  of the 8 bools                   // OR-reduce 8
```

Reduce a fixed-aligned boolean group to one bool — the branch-on-all-lanes / branch-on-any-lane
predicate the vector compares drive. The aligned group is exactly the `BR4`/`BR8` view from §2.1.

```
TIMING  bt DEF @4; bs4/bs8 USE @4; opcode @3.  1-cycle reduce.  [HIGH/OBSERVED]
```

Worked: §7.3.

### 3.4 Boolean branch `BT` / `BF` — `xt_iclass_bbranch`

```
OPERANDS  OUT[ BranchTarget, BranchTaken ]  IN[ bs : BR(s), label8, PC ]
ENC       24-bit RRI8-branch, r = { BF 0, BT 1 }; imm8 = signed PC-relative target (label8)

SEM  BT : if (bs == 1) PC = PC + sext(label8) + 4    // branch if true
     BF : if (bs == 0) PC = PC + sext(label8) + 4    // branch if false
```

Conditional control transfer keyed on a 1-bit boolean register — the BR-file analogue of the AR-bit
tests `BBC`/`BBS` ([B26](b26-xt-ctrl.md)). `label8` is the sign-extended 8-bit offset (±127 insns).

```
TIMING  bs USE @3, label8 @2, PC @1, opcode @0; BranchTarget/Taken DEF @3 (resolves in E-stage).
        Taken-penalty per the fetch pipeline.  [HIGH/OBSERVED]
```

Worked: §7.4.

### 3.5 Zero-overhead loop (core) `LOOP` / `LOOPNEZ` / `LOOPGTZ` — `xt_iclass_loop`/`loopz` `[HIGH]`

```
OPERANDS  OUT[ LBEG, LEND, LCOUNT, BranchTarget, BranchTaken, LoopImmediate ]
          IN[ ars : AR(s), ulabel8, PC, NextPC ]
ENC       24-bit RRI8, op0=6 (BRI8/loop major), r = { LOOP 8, LOOPNEZ 9, LOOPGTZ 10 }; s = AR count
          src; imm8 = ulabel8 (UNSIGNED forward loop-end offset, 0..255)

SEM  // arm the zero-overhead-loop hardware
     LBEG = NextPC                  // address of the instruction AFTER the LOOP
     LEND = PC + ulabel8 + 4        // the loop-end address
     LOOP    : LCOUNT = ars - 1                 // iterate ars times; NO zero-guard
     LOOPNEZ : if (ars == 0) skip body (BranchTaken→LEND); else LCOUNT = ars - 1
     LOOPGTZ : if ((int32)ars <= 0) skip body;            else LCOUNT = ars - 1
```

`LOOP*` write `LBEG`/`LEND`/`LCOUNT` and arm the hardware back-edge. When the fetch unit reaches `LEND`
with `LCOUNT != 0`, it **decrements `LCOUNT` and re-fetches from `LBEG` with no branch instruction and
no taken-penalty**. The loop continues until the cycle in which the fetch unit observes `LCOUNT == 0`
at `LEND`, when it falls through.

```
TIMING  ars USE @3, NextPC @2, PC @1, ulabel8 @0, opcode @0; LCOUNT/LBEG/LEND/BranchTarget/Taken
        DEF @3, LoopImmediate DEF @1.  [HIGH/OBSERVED]
```

> **QUIRK — `LOOP` has no zero-guard; the empty body is rejected.** `LOOP` sets `LCOUNT = ars − 1`
> *unconditionally*, so `ars = 0` wraps to `0xFFFFFFFF` and runs a `2^32`-iteration loop — the compiler
> emits `LOOPNEZ`/`LOOPGTZ` when the count may be ≤ 0. Separately, the device assembler **rejects an
> empty loop body** ("invalid empty loop"): the body must contain ≥ 1 instruction so that `LEND ≠
> LBEG`. The hardware back-edge compares the fetch PC against `LEND`; a zero-length body would make
> `LEND == LBEG` and the compare ambiguous. `[HIGH/OBSERVED — device-assembler diagnostic + LCOUNT
> decrement model]`

Worked: §7.5.

### 3.6 Wide loop `LOOP.W15` / `LOOPNEZ.W15` / `LOOPGTZ.W15` — `xt_iclass_wloop`/`wloopz` `[HIGH]`

```
OPERANDS  as §3.5 but IN[ ..., xt_wloop_label, ... ]  (15-bit loop-end label, not u8)
ENC       FLIX-ONLY — NO 24-bit core form.  Emitted as an 8-byte wide-format bundle
          "{ loop.w15 a3, target; nop }" carrying the 15-bit loop-end label, extending the loop
          range from the core ±255-byte ulabel8 to a 15-bit span.
SEM       identical loop-register arming as §3.5; only LEND reach differs
          (LEND = PC + wloop_label + 4).  The compiler's choice when the body exceeds the 8-bit reach.
TIMING    mirrors loopz; the libcas scoreboard LOOP_W15 chain runs stage0…stage3.  [MED — schedule
          inferred from the shared loop-register DEF set + the 4-stage LOOP_W15 stage functions.]
```

Worked: §7.6.

### 3.7 Loop special registers `RSR`/`WSR`/`XSR` `.LBEG`/`.LEND`/`.LCOUNT` (9 ops)

```
OPERANDS  RSR.x : OUT[ art : AR ]                IN[ <SR> ]
          WSR.x : OUT[ <SR> ]                    IN[ art : AR ]
          XSR.x : atomic swap  IN/OUT art ↔ <SR>
          <SR> ∈ { LBEG (sr 0), LEND (sr 1), LCOUNT (sr 2) }
ENC       24-bit RSR/WSR/XSR; sr-field = { LBEG 0, LEND 1, LCOUNT 2 }; RSR op-pattern 0x30, WSR 0x130,
          XSR 0x610 (read directly off the §7.7 round-trip).

SEM  RSR.LBEG   : art = LBEG          WSR.LBEG   : LBEG = art
     RSR.LEND   : art = LEND          WSR.LEND   : LEND = art
     RSR.LCOUNT : art = LCOUNT        WSR.LCOUNT : LCOUNT = art
     XSR.x      : tmp = SR; SR = art; art = tmp    // atomic exchange
```

These are the OS/handler accessors to save/restore the zero-overhead-loop state across a context
switch and to set up software-managed loops.

> **QUIRK — `WSR.LCOUNT` / `XSR.LCOUNT` carry an `XTSYNC` pipeline-sync side-effect.** Writing
> `LCOUNT` re-arms the loop hardware, so the model serialises the write against any in-flight back-edge
> decrement. The issue functions prove it: `x24_Inst_0_inst_WSR_LCOUNT_issue` (@`0x1678760`) opens with
> `mov $0x6,%esi ; call my_XTSYNC_set_def` *before* it resolves the AR source and writes `LCOUNT`, and
> `x24_Inst_0_inst_XSR_LCOUNT_issue` (@`0x16787b0`) issues two `my_XTSYNC_set_def` calls around the
> read-old/write-new exchange — mode `0x6` is the loop-re-arm sync class. The `XTSYNC` machinery itself
> (`my_XTSYNC_set_def`/`my_XTSYNC_stall` @ `0x1776590`…`0x1776980`) stalls the loop datapath until the
> new count commits. A reimplementation that lets a `WSR.LCOUNT` retire out-of-order with respect to the
> back-edge will mis-count the loop. `[HIGH/OBSERVED — issue-fn XTSYNC mode-0x6 set_def]`

Worked: §7.7.

### 3.8 Branch-predictor BTB `RBTB0-2` / `WBTB0-2` — `xt_iclass_rbtb`/`wbtb`

```
OPERANDS  RBTBn : OUT[ art : AR, PrivilegedException ]  IN[ ars : AR, MS_DISPST, PSRING, InOCDMode,
                                                            BranchPredictorReadData ]
          WBTBn : OUT[ PrivilegedException ]  IN[ art : AR, ars : AR, MS_DISPST, PSRING, InOCDMode ]
ENC       24-bit RRR (RST family); r = { WBTB0/1/2 0/1/2, RBTB0/1/2 8/9/10 }; the 0/1/2 index selects
          one of three BTB data/tag/way ports (an enumerated t-field on the encode side).

SEM  RBTBn : art = read of BTB field n at index ars (BranchPredictorReadData → art); privileged.
     WBTBn : write BranchPredictorWriteData (from {art,ars}) into BTB field n; privileged.
```

Diagnostic / OS branch-predictor inspect & configure — **not** user datapath. `PrivilegedException`
fires when `PS.RING != 0`. These carry the `flags = 0x100` serializing bit in `opcodes[]` (alongside
`WAITI`), marking them as non-bundleable control ops that need special pipeline handling.

```
TIMING  RBTB: ars USE @1, PSRING/MS_DISPST/InOCDMode @3, ReadData @4; art DEF @5.
        WBTB: ars USE @1, art @5, ctl @3; WriteData DEF @6.  The longest scalar control-register
        paths on this page.  [HIGH/OBSERVED]
```

Worked: §7.8.

### 3.9 Privileged window dispatch `ROTW` / `SETW` / `SPILLW` / `TOSSW` / `EXIT` — `xt_iclass_*regwin` `[HIGH]`

```
ROTW   OUT[ WB_P, PrivilegedException ]  IN[ simm4, MS_DISPST, PSRING, InOCDMode ]
       : rotate the register window by simm4 (the handler walks the call chain). Privileged.
SETW   OUT[ WB_P, PrivilegedException ]  IN[ simm4, MS_DISPST, PSRING, InOCDMode ]
       : stage a WindowBase-pending (WB_P) change by simm4.
SPILLW OUT[ InvalidOperand, WindowOverflow8, WindowInstCall0Exception ]
       IN[ WB_C, WB_N, WB_S, SAR, MS_DISPST ]
       : spill the live windowed registers to the stack (raises WindowOverflow8 to drive the spill
         handler; SAR does the spill-address math).
TOSSW  OUT[ WindowInstCall0Exception ]  IN[ WB_C, WB_S, SAR ]
       : discard (toss) a window without spilling.
EXIT   OUT[ WB_P, WindowUnderflow8, WindowInstCall0Exception ]  IN[ WB_N, WB_S, MS_DISPST ]
       : exit a window frame (raises WindowUnderflow8 to drive the underflow/restore handler).
ENC    ROTW (r=8 s=0); SETW (r=8 s=1); SPILLW (r=3 s=6 t=0); TOSSW (r=3 s=7 t=0); EXIT (density-window
       sub-table m=2 s=1 n=3).
```

These are the privileged primitives of the windowed-register exception machinery — the spill/restore
that the `WindowOverflow8` / `WindowUnderflow8` vectors run. The `libcas-core.so` issue functions
confirm the operand model: `x24_Inst_0_inst_EXIT_issue` (@`0x1679010`) drives `my_WB_N_set_def` /
`my_WB_P_set_def` / `my_WB_C_set_def`, and `x24_Inst_0_inst_ROTW_issue` (@`0x1678fa0`) drives
`my_MS_DISPST_set_def` / `my_WB_C_set_def` / `my_WB_P_set_def`. The privilege *check* and the trap are
**not** in the issue function — they are reached **indirectly through the per-op `*_stage_functions`
data tables**: the `ROTW`/`SETW`/`RBTB`/`WBTB` stage chains route to `PrivilegedException_exc`
(@`0x177edc0`), `SPILLW`'s to `WindowOverflow8_exc` (@`0x177a950`), and `EXIT`'s to
`WindowUnderflow8_exc` (@`0x177a890`). The window-exception bodies then call the imported host
interface — `WindowOverflow8_exc` calls `nx_SpillWOverflow_interface`, and each `*Exception_exc`
commits the architectural trap state (`EXCCAUSE`, `WB_C`/`WB_S`/`WB_N`, `MS_DISPST`) via the
`my_*_use_commit` helpers. `WindowInstCall0Exception` flags use of a window op inside a `CALL0`
(non-windowed) frame.

> **GOTCHA — `RFWO`/`RFWU` are *not* the exit ops; they do not exist.** A classic Xtensa window machine
> spills/restores under `SPILLW`/`EXIT` and *returns* from the overflow/underflow vector via
> `RFWO`/`RFWU`. In this config there is no `RFWO`/`RFWU` (see §0 CORRECTION) — `SPILLW`/`EXIT` raise
> `WindowOverflow8`/`WindowUnderflow8`, and the handler returns by ordinary control flow after running
> the spill/fill, not by a dedicated return-from-window opcode. `[HIGH/OBSERVED — nm + device-assembler]`

```
TIMING  ROTW: PSRING/InOCDMode USE @0, MS_DISPST @1; WB_P DEF @0 (control-side, commits at issue).
        SPILLW/TOSSW/EXIT: WB_*/SAR/MS_DISPST in, Window* exc out (window dispatch).  [MED — control-
        state DEF stages from the issue-fn set_def chain.]
```

Worked: §7.9.

### 3.10 Exception-dispatch misc `EXCW` / `MEMW` / `SYSCALL` / `ILL` / `ILL.N` — `xt_sem_misc` `[HIGH/OBS]`

```
EXCW    OUT[]  IN[]   (t=8) : EXCEPTION WAIT — a barrier ensuring all prior instructions that can
        raise an exception have done so before continuing (the exception-ordering fence). USE @2.
MEMW    OUT[]  IN[]   (t=c) : MEMORY WAIT — orders memory accesses across the instruction (the
        load/store ordering fence). USE @2.
SYSCALL OUT[ SYSCALLCause, SyscallException ]  IN[ immt ]  (s=0) : raise the system-call exception;
        SYSCALLCause = immt sub-code (objdump prints "syscall 0"). USE immt @2; SYSCALLCause DEF @5.
        Always traps; the SYSCALL stage chain routes to SyscallException_exc (@0x177f0c0), which
        calls the host nx_SYSCALLCause_interface to deliver the cause.
ILL     OUT[ InvalidOperand ]  IN[]   (all-zero encoding) : the canonical illegal-instruction op
        (raises IllegalInstruction → IllegalInstructionException_exc @0x177eaa0). USE @2;
        InvalidOperand DEF @3.
ILL.N   OUT[ InvalidOperand ]  IN[]   (s=0 t=6 density, 16-bit) : the .N illegal form.
```

`EXCW`/`MEMW` are **pure ordering fences** — no operands, no datapath; the scalar analogues of the IVP
sync fences. `ILL`/`ILL.N` force an illegal-instruction trap; `SYSCALL` forces a syscall trap with a
sub-code.

Worked: §7.10.

### 3.11 `BREAK` / `BREAK.N` / `BREAK1` / `HALT` / `HALT.N` — `xt_sem_core_brk`/`halt`

```
BREAK   OUT[ BreakNum, BreakException, MaybeOCDBreakException ]  IN[ imms, immt ]  (r=4)
        : software breakpoint; imms (s) and immt (t) are two 4-bit sub-codes → BreakNum; raises
          BreakException (or an OCD-mode break if in debug). USE imms/immt @4; BreakNum DEF @5.
BREAK.N OUT[ BreakNum, BreakNException, MaybeOCDBreakException ]  IN[ imms ]  (t=2, 16-bit)
        : the density narrow breakpoint (one 4-bit sub-code).
BREAK1  OUT[ BreakNum, BreakException, MaybeOCDBreakException ]  IN[ imms1 ]  (m=3 n=3)
        : a second 24-bit break encoding (imms1 sub-code).
HALT    OUT[ HaltImmediate, HaltException ]  IN[ immt, InOCDMode ]  (s=2)
        : halt the core (raises HaltException; OCD halt if InOCDMode). immt = sub-code.
HALT.N  OUT[ HaltImmediate, HaltException ]  IN[ InOCDMode ]  (s=1 t=6, 16-bit).
```

Debug-trap dispatch — `BREAK*` drop to the debugger / break vector, `HALT*` stop the core.
`MaybeOCDBreakException` = the break is intercepted by the on-chip-debug unit when present.

Worked: §7.11.

### 3.12 `CLAMPS` — saturating compare-select (cross-ref to B29)

`CLAMPS` is the saturating sibling of the min/max compare-select family and the task groups it with
them — but by the canonical partition it is `xt_sem_clamps`, **owned by [B29](b25-xt-core.md)**, so it
is **not** counted in the B28 total of 51 (this preserves the 0-overlap rule, the same way scalar-`div`
was cross-referenced to B26). One-line recap:

```
OPERANDS  OUT[ art : AR(t) ]  IN[ ars : AR(s), imm (saturation bit-width, 7..22) ]
ENC       24-bit; encode template word0 = 0x00330000 (Opcode_clamps_Slot_inst_encode @0x33e090).
          The immediate field encodes (imm − 7): clamps a4,a5,7 → 334500, ,15 → 334580, ,22 → 3345f0.
SEM       art = clamp( (int32)ars, -2^imm, 2^imm - 1 )   // signed saturation to an imm-bit range
```

The device assembler restricts the immediate to **7…22** (`imm = 0/1/23` are rejected with "operand 3
… has invalid value"), i.e. `CLAMPS` saturates `ars` to the signed range `[−2^imm, 2^imm − 1]` for an
`imm`-bit field. Like min/max it carries no `CPENABLE` and raises no arithmetic exception — saturation
is silent. Full entry at [B29](b25-xt-core.md). `[HIGH/OBSERVED — encode thunk + immediate-range probe]`

---

## 4. Timing summary (from the `libcas-core.so` `*_inst_stageN` scoreboard chains)

| op-class | inputs USE @stage | result DEF @stage | latency |
|----------|-------------------|-------------------|---------|
| `MIN`/`MAX`/`MINU`/`MAXU` | `ars`@4, `art`@4, op@3 | `arr`@5 | 2-cyc cmp-sel |
| `ANDB`/`ORB`/`XORB`/… | `bs`@4, `bt`@4, op@3 | `br`@4 | 1-cyc bool |
| `ALL4`/`ANY4`/`ALL8`/`ANY8` | `bs4`/`bs8`@4, op@3 | `bt`@4 | 1-cyc reduce |
| `BT`/`BF` | `bs`@3, `label8`@2, op@0 | BranchTarget/Taken@3 | E-stage branch |
| `LOOP`/`LOOPNEZ`/`LOOPGTZ` | `ars`@3, NextPC@2, PC@1, `ulabel8`@0, op@0 | LCOUNT/LBEG/LEND@3, LoopImm@1 | loop arm |
| `LOOP.W15`/… | (mirrors loopz) | loop SRs@3 | [MED inferred] |
| `RSR`/`WSR`/`XSR.L*` | `art` / `<SR>` (+`XTSYNC` on `LCOUNT` write) | `art` / `<SR>` | SR access |
| `RBTB0-2` | `ars`@1, ctl@3, ReadData@4 | `art`@5 | long ctl path |
| `WBTB0-2` | `ars`@1, `art`@5, ctl@3 | WriteData@6 | long ctl path |
| `ROTW`/`SETW` | PSRING/InOCD@0, MS_DISPST@1 | WB_P@0 | control commit |
| `SPILLW`/`TOSSW`/`EXIT` | WB_*/SAR/MS_DISPST | Window* exc | window dispatch |
| `EXCW` / `MEMW` | op@2 | — | ordering fence |
| `ILL` / `ILL.N` | op@2 | InvalidOperand@3 | illegal trap |
| `SYSCALL` | `immt`@2, op@2 | SYSCALLCause@5 | syscall trap |
| `BREAK` / `BREAK1` | `imms`@4, `immt`@4, op@4 | BreakNum@5 | break trap |
| `HALT` / `HALT.N` | `immt`@4 / InOCDMode | HaltImmediate | halt |

The min/max DEF @5 (vs the B25 ALU DEF @4) is the extra compare-then-select stage — confirmed by the
six-stage `MAX_inst_stage0…stage5` chain in `libcas-core.so`. The BTB ops are the longest scalar
control-register paths (DEF @5/@6).

---

## 5. Exceptions / state

- **Datapath (`MIN`/`MAX`, booleans, `ALL`/`ANY`, `BT`/`BF`, `CLAMPS`):** `state_in = []`, `exc = []`.
  Unconditionally executable; **no `CPENABLE`, no `Coprocessor1Exception`.**
- **Control / dispatch exception ARG_OUTs** (gen_sig, control-side traps; the named `*_exc` helpers in
  `libcas-core.so` are the targets):

| exception | raised by | helper |
|-----------|-----------|--------|
| `PrivilegedException` | `ROTW` `SETW` `RBTB0-2` `WBTB0-2` | `PrivilegedException_exc` @`0x177edc0` |
| `WindowOverflow8` | `SPILLW` | `WindowOverflow8_exc` @`0x177a950` |
| `WindowUnderflow8` | `EXIT` | `WindowUnderflow8_exc` @`0x177a890` |
| `WindowInstCall0Exception` | `SPILLW` `TOSSW` `EXIT` | (gen_sig ARG_OUT) |
| `SyscallException` | `SYSCALL` | `SyscallException_exc` @`0x177f0c0` (→ `nx_SYSCALLCause_interface`) |
| `BreakException`/`BreakNException`/`MaybeOCDBreakException` | `BREAK` `BREAK.N` `BREAK1` | `BreakException_exc` @`0x177f3e0`, `BreakNException_exc` @`0x177f6f0` |
| `HaltException` | `HALT` `HALT.N` | `HaltException_exc` @`0x1765780` |
| `InvalidOperand` / `IllegalInstruction` | `ILL` `ILL.N` `SPILLW` | `IllegalInstructionException_exc` @`0x177eaa0` |

> **NOTE — exceptions are dispatched via the per-op `*_stage_functions` tables, not a direct `call`
> from the `_issue` body.** The value-computing `_issue` functions for the datapath ops (min/max, bool,
> `ALL`/`ANY`) are exception-free and `CPENABLE`-free under tight start→next-symbol disassembly bounds
> (a wide-window grep falsely spills into the adjacent function and appears to show an exception). The
> control-op traps above are reached by the stage chain routing to the named `*_exc` body, which then
> commits the architectural trap state (`EXCCAUSE`, `WB_C`/`WB_S`/`WB_N`, `MS_DISPST`, `InOCDMode`) via
> the `my_*_use_commit` helpers. `[HIGH/OBSERVED — bounded issue-fn disasm + stage-table routing]`

- **Implicit control state read:** `PSRING` (`PS.RING` privilege), `MS_DISPST` (dispatch state),
  `InOCDMode` (debug mode), `SAR` (window spill addressing), `WB_C`/`WB_N`/`WB_S`/`WB_P` (WindowBase
  current/next/save/pending), `XTSYNC` (loop-count re-arm). The loop SRs `LBEG`/`LEND`/`LCOUNT` are the
  only SRs owned by this batch.

---

## 6. Worked bit-pattern examples (round-tripped: `as` → `.o` → `objdump`)

Oracle: `XTENSA_SYSTEM=.../tools/XtensaTools/config  XTENSA_CORE=ncore2gp`; `xtensa-elf-as -o b28.o
b28.s` then `xtensa-elf-objdump -d`. `objdump` prints the 3 core bytes big-endian-grouped; the
little-endian wire value `v = b0|b1<<8|b2<<16` and the `(op0,t,s,r,op1,op2)` decode are shown. Every
byte below is copy-exact from the device disassembly.

**7.1 `MIN`/`MAX`** — `min a4,a5,a6` → `434560` ⇒ wire `60 45 43`, `v=0x434560`
(`op0=0 t=6 s=5 r=4 op2=4`). `arr=a4 ars=a5 art=a6`; `a4 = ((int32)a5 < (int32)a6) ? a5 : a6`.
`max a4,a5,a6 = 534560` (op2=5), `minu = 634560` (6), `maxu = 734560` (7), `max a7,a8,a9 = 537890`
(`r=7 s=8 t=9`).

**7.2 `ANDB`** — `andb b4,b5,b6` → `024560` ⇒ wire `60 45 02`, `v=0x024560`
(`op2=0` RST2/bbool1). `br=b4 bs=b5 bt=b6`; `b4 = b5 & b6`. `andbc = 124560` (op2=1),
`orb = 224560` (2), `orbc = 324560` (3), `xorb = 424560` (4).

**7.3 `ALL4`** — `all4 b4,b8` → `009840` ⇒ wire `40 98 00`, `v=0x009840` (`r=9` ALL4; `s` encodes the
`b8:b9:b10:b11` quad; `t=b4`). objdump: `all4 b4, b8:b9:b10:b11`. `b4 = b8&b9&b10&b11`. `any4 = 008840`
(`r=8`), `all8 b0,b8 = 00b800` (`r=11`, octet `b8..b15`), `any8 b0,b8 = 00a800` (`r=10`).

**7.4 `BT`/`BF`** — `bt b2,.` → `fc1276` ⇒ wire `76 12 fc`, `v=0xfc1276` (`op0=6 BRI8`, `r=1` BT,
`s=b2`, `imm8=0xfc = -4` back-branch). `if b2 → branch`. `bf b2,. = f90276` (`r=0`).

**7.5 `LOOP`** — `loop a3,end` → `018376` ⇒ wire `76 83 01`, `v=0x018376` (`op0=6`, `r=8` LOOP,
`s=a3`, `imm8=0x01` forward offset). `LCOUNT=a3-1; LBEG=NextPC; LEND=PC+1+4`. `loopnez = 019376`
(`r=9`), `loopgtz = 01a376` (`r=10`). (body must be non-empty.)

**7.6 `LOOP.W15`** — `loop.w15 a3,end` → 8-byte bundle `029c16009c04036f` ⇒
`{ loop.w15 a3, <end>; nop }` (FLIX-only, 15-bit loop-end label). `loopnez.w15 = 029c14009c04036f`,
`loopgtz.w15 = 029c12009c04036f`. (Bundle bytes depend on the loop-end label distance; these are
copy-exact for a body of one `nop`.)

**7.7 `RSR.LBEG`** — `rsr.lbeg a4` → `030040` ⇒ wire `40 00 03`, `v=0x030040` (`RSR`, `sr=0`=LBEG,
`t=a4`). `a4 = LBEG`. `wsr.lbeg = 130040`, `xsr.lbeg = 610040`, `rsr.lend = 030140` (`sr=1`),
`rsr.lcount = 030240` (`sr=2`), `wsr.lcount = 130240`. **Confirms LBEG=0, LEND=1, LCOUNT=2.**

**7.8 `RBTB0`** — `rbtb0 a4,a5` → `508540` ⇒ wire `40 85 50`, `v=0x508540` (RST, `r=8`=RBTB0, `s=a5`,
`t=a4`). `a4 = BTB[port0]` read @ index `a5` (privileged). `rbtb1 = 509540`, `rbtb2 = 50a540`,
`wbtb0 a4,a5 = 500540` (`r=0`), `wbtb1 = 501540`, `wbtb2 = 502540`.

**7.9 `ROTW`** — `rotw 1` → `408010` ⇒ wire `10 80 40`, `v=0x408010` (`r=8 s=0` ROTW, `simm4=1`).
rotate window by +1 (privileged). `setw 1 = 408110` (`s=1`), `spillw = 003600` (`r=3 s=6`),
`tossw = 003700` (`s=7`), `exit = 0001b0` (density-window `m=2 s=1 n=3`).

**7.10 `EXCW`** — `excw` → `002080` (`t=8`) exception-wait fence. `memw = 0020c0` (`t=c`),
`syscall = 005000` (objdump "syscall 0"), `ill = 000000`, `ill.n = f06d` (16-bit density).

**7.11 `BREAK 1,2`** — `004120` (`r=4 s=1 imms`, `t=2 immt`). BreakNum from `(1,2)`; raise
BreakException. `break.n 3 = f32d` (16-bit), `break1 3 = 0003f5`, `halt 5 = 005250` (`s=2`),
`halt.n = f16d` (16-bit `s=1 t=6`). And the cross-ref: `clamps a4,a5,7 = 334500`, `,15 = 334580`,
`,22 = 3345f0` (§3.12).

All 51 representatives assembled + disassembled identically through the device toolchain.

---

## 7. Divergence & correction ledger

> **CORRECTION 1 — no `RFE`/`RFI`/`RFWO`/`RFWU`/`RSIL`.** This config has no return-from-exception /
> interrupt-disable architecture; the only `rf*` ops are `RFDO`/`RFR` (B29). The exception machine is
> built on traps + window/loop primitives. (§0) `[HIGH/OBSERVED — nm + device assembler]`
>
> **CORRECTION 2 — the four BR views are `BR2`/`BR4`/`BR8`/`BR16`** (widths 2/4/8/16, parent `BR`),
> not `BR`/`BR2`/`BR4`/`BR8`. (§2.1) `[HIGH/OBSERVED — regfile_views[] raw read]`
>
> **CORRECTION 3 — `num_states` = 81 (libisa-core) / 87 (merged).** `libisa-core.so` `num_states` returns
> `0x51` = **81** (core architectural states); a prior note used 87 *as if it were the libisa-core value* —
> that is wrong for libisa-core alone. The **87** is the *merged* total: `81 + 6` from the `libisa-core-hw.so`
> module (`num_states → 0x6`). The Part-2 register/identity pages correctly use **87** (merged); this
> exception page reads **81** for the core-config state count. `LBEG`/`LEND`/`LCOUNT` are three of the 81.
> (§1) `[HIGH/OBSERVED — num_states accessor, both modules]`
>
> **DIVERGENCE — `CLAMPS` is counted at B29, not B28.** The task groups `CLAMPS` with `MIN`/`MAX`
> (it is the saturating compare-select), so it is documented here (§3.12) as an adjacent cross-ref, but
> it belongs to the `xt_sem_clamps` group owned by [B29](b25-xt-core.md) and is excluded from the 51
> to preserve the disjoint partition. No double-count.
>
> **DIVERGENCE — wide-loop §7.6 bytes vs an earlier draft.** The wide-loop bundle's last byte encodes
> the resolved loop-end label, so it is body-length-dependent. The bytes above (`…04036f`) are the
> round-trip for a one-`nop` body; an earlier per-instruction draft cited a different label
> distance. The 8-byte `{ loop.w15 …; nop }` structure is invariant.

---

## See also

- [ISA Batch 25 — base-Xtensa scalar arith / logic / shift](b25-xt-core.md) — the AR-file ALU core;
  the `SALT`/`MOV`cc set-on-compare ops that contrast with these boolean-file ops.
- [ISA Batch 26 — ld/st / branch / density / MUL32 / div](b26-xt-ctrl.md) — the `BBC`/`BBS` AR-bit
  branches that mirror `BT`/`BF`, and the scalar-`div` arithmetic exception.
- [ISA Batch 27 — System / SR / RegWindow / Sync](b27-xt-system.md) — the non-loop SRs, the
  windowed-call ABI (`ENTRY`/`RETW`/`MOVSP`) the privileged window dispatch here serves.
- [The boolean / register-file model](../core/register-files.md) — the `BR` file (idx 1, 1-bit×16) and
  its `BR2`/`BR4`/`BR8`/`BR16` views.
- [The FLIX encoding model](../core/flix-encoding.md) — how the datapath ops (min/max, bool) take their
  bundle placements alongside the 24-bit standalone forms.
- [The XEA3 exception architecture](../../control/interrupt/xea3-interrupt-architecture.md) — the trap
  surface these dispatch ops feed (this config has no RFE/RFI return-from-exception path).
- [Boot / reset sequence](../../uarch/boot-reset.md) — the CRT/startup that arms the window and loop
  state before firmware runs.
- [The firmware error handler](../../firmware/seq/error-handler.md) — the `SYSCALL` / `BREAK` / window
  trap consumers.
- [ISA reference template & 30-batch partition](template-and-partition.md) — the per-batch membership
  rule that pins this roster to exactly 51 disjoint mnemonics.
