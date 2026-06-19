# ISA Batch 27 — base-Xtensa System / SR / Register-Window / Sync

This page documents the 56 base-Xtensa **system-control** opcodes of the GPSIMD
NeuronCore (Cadence Tensilica Vision-Q7 NX "Cairo", config `ncore2gp`): the
special-register access families `RSR`/`WSR`/`XSR`, the user-register pair
`RUR`/`WUR.THREADPTR`, the windowed-ABI rotation primitives
`ENTRY`/`RETW`/`MOVSP`/`ROTW`/`SETW`/`SPILLW`/`TOSSW`/`EXIT`, the windowed
spill/fill memory ops `L32E`/`S32E`/`S32STK`, the memory-ordering fences
`ISYNC`/`RSYNC`/`ESYNC`/`DSYNC`/`MEMW`/`EXTW`, and the external-register bus pair
`RER`/`WER`. These are the registers and barriers that ground the boot/reset
spine (see [Boot / Reset](../../uarch/boot-reset.md)) and the memory-ordering
model (see [Atomics + Ordering](../../uarch/atomics-ordering.md)).

> **CRITICAL FRAMING.** Every op on this page is a **non-FLIX scalar 24-bit
> decode-tree instruction** (or, for `RETW.N`, a 16-bit density narrow), **not**
> an IVP vector-datapath op. The Encoding section below names the *scalar-form
> bit-fields and the selector CONST* read out of each `Opcode_<mn>_Slot_inst_encode`
> thunk — **not** a FLIX format+slot. The whole-instruction template word IS the
> literal Xtensa scalar encoding with the AR operand fields zeroed. Sibling
> scalar pages: [B25 — arith/logic/shift](b25-xt-core.md),
> [B26 — ld/st/branch/density/MUL32](b26-xt-ctrl.md). Partition contract:
> [Template & 30-Batch Partition](template-and-partition.md). Preceding committed
> boundary: [B21 — select/shuffle](b21-select-shuffle.md).

All bytes on this page are read directly from
`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libisa-core.so`
(ELF64, **not stripped**, 12681 `*_encode` thunks) and round-tripped through the
device assembler `tools/XtensaTools/bin/xtensa-elf-as` and disassembler
`xtensa-elf-objdump` with `XTENSA_CORE=ncore2gp`,
`XTENSA_SYSTEM=tools/XtensaTools/config`. ISS issue timing is read from the
DWARF-bearing companion `tools/ncore2gp/config/libcas-core.so`
(`x24_Inst_0_inst_<MN>_issue`). Value semantics are grounded against the 864
`module__xdref_*` leaves of `tools/ncore2gp/config/libfiss-base.so`. Confidence
tags: **HIGH/MED/LOW × OBSERVED** (literal bytes / oracle round-trip) /
**INFERRED** (derived over OBSERVED + canonical Xtensa arithmetic) /
**CARRIED** (from a sibling batch report).

---

## 1. Encoding model — the scalar decode skeleton

[HIGH/OBSERVED] B27 ops share one base-Xtensa field map. The 24-bit word `W` is
little-endian (`movl $W,(%rdi)` in the encode thunk; emitted low byte first):

```
bit:  23..20  19..16  15..12  11..8   7..4    3..0
fld:   op2     op1      r       s       t      op0
```

Composite fields used by this batch:

| Field | Bits | Meaning |
|-------|------|---------|
| `op0` | `[3:0]`   | major opcode; `0x0` = QRST/CORE root for all SR/window/sync ops |
| `t`   | `[7:4]`   | `art` — AR operand (read target / write source) |
| `s`   | `[11:8]`  | `ars` — AR source / window base operand |
| `r`   | `[15:12]` | `arr` — AR destination (used by `RUR`) / sub-op selector |
| `op1` | `[19:16]` | decode sub-group (RST0..3 selector) |
| `op2` | `[23:20]` | leaf selector within the sub-group |
| `sr`  | `[15:8]`  | 8-bit Special-Register number = `r‖s` |
| `st`  | `[11:4]`  | 8-bit User-Register number = `s‖t` |
| `imm4`| `[15:12]` | `simm4` rotate / `uimm` window field |

### 1.1 The R/W/X special-register lattice

[HIGH/OBSERVED] `RSR`/`WSR`/`XSR` are **one parameterised datapath** gated by the
`(op1,op2)` pair, with the SR number in `sr[15:8]` as the shared index. The three
selector CONSTs, read byte-exact from the thunks:

```
rsr.<S>  word = 0x00030000 | (SR << 8)      (op1=3, op2=0)   read  SR -> art
wsr.<S>  word = 0x00130000 | (SR << 8)      (op1=3, op2=1)   write art -> SR
xsr.<S>  word = 0x00610000 | (SR << 8)      (op1=1, op2=6)   atomic swap art <-> SR
```

[HIGH/OBSERVED] These three constants are **invariant across every SR**. The
cross-family XORs are constant for all SRs that carry all three forms:
`xsr.X ^ rsr.X == 0x00620000` and `xsr.X ^ wsr.X == 0x00720000`. The
read→write delta is the clean single-nibble `op1` flip `wsr.X ^ rsr.X ==
0x00100000`. **XSR is two nibbles from both** — it sits at its own
`(op1=1,op2=6)` lattice point, **not** a single-bit variant of RSR or WSR.

> **QUIRK — XSR is a genuine atomic read-and-write, not a flag bit.** Because XSR
> moves to a distinct `(op1,op2)` lattice point rather than toggling one nibble of
> RSR/WSR, the decoder routes it through a separate sub-branch
> (`x24_Inst_0_inst_XSR_*_issue` in `libcas-core.so`), and its semantics are
> `tmp=AR[t]; AR[t]=SR; SR=tmp` in a single architectural step. This is the
> canonical primitive for swapping `PS` in an exception prologue.

### 1.2 The user-register (`RUR`/`WUR`) sub-family

[HIGH/OBSERVED] THREADPTR is a **user register** (`UR 0xe7`), reachable only via
`RUR`/`WUR`. There is **no `rsr.threadptr`** — the device assembler rejects it
("unknown opcode"). The UR number sits in `st = s‖t` (for `RUR`, where `t` is
free) and the AR operand goes in a *different* field than the SR forms:

```
rur.<U>  word = 0x00e30000 | (UR<<8 split s‖t) ; AR dest in r[15:12]  (op1=3,op2=0xe)
wur.<U>  word = 0x00f30000 | (UR<<8 split r‖s) ; AR src  in t[7:4]    (op1=3,op2=0xf)
```

> **GOTCHA — the AR field moves between RUR and WUR.** `rur.threadptr a7`
> assembles to `0xe37e70`: the AR **destination** `a7` lands in the **r-field**
> (`0x7<<12`), while `UR 0xe7` occupies `s=0xe,t=0x7`. `wur.threadptr a8`
> assembles to `0xf3e780`: the AR **source** `a8` lands in the **t-field**
> (`0x8<<4`), while `UR 0xe7` occupies `r=0xe,s=0x7`. The XOR against the
> AR-zeroed thunk template proves it: `0xe30e70 ^ 0xe37e70 = 0x7000` (r-field) for
> RUR vs `0xf3e700 ^ 0xf3e780 = 0x80` (t-field) for WUR. Reusing the RSR `t`-field
> convention for RUR would place the AR in the wrong slot.

### 1.3 Window-control, spill/fill, fence and external-register skeletons

[HIGH/OBSERVED] The remaining families pin distinct `op0`/`op1`/`op2`/`r`/`s`
constants; each was read from its `Opcode_<mn>_Slot_inst_encode` thunk:

| Family | Members | Template base | Decode chain |
|--------|---------|---------------|--------------|
| window rotate | `ENTRY` | `0x000036` | `op0=6` SI/BI1; `s=ars`, `imm12@[23:12]` |
| | `RETW` | `0x000090` | QRST←RST0←ST0←SNM0←JR |
| | `RETW.N` | `0xf01d` (x16b) | density narrow `op0=0xd` |
| | `MOVSP` | `0x001000` | QRST←RST0←ST0 (`r=1`); `s=ars`,`t=art` |
| | `ROTW` | `0x408000` | ST1 (`op2=4`,`r=8`); `imm4=simm4` |
| | `SETW` | `0x408100` | ST1 (`op2=4`,`r=8`,`s=1`); `imm4` |
| | `SPILLW` | `0x003600` | ST0 (`r=3`,`s=6`) |
| | `TOSSW` | `0x003700` | ST0 (`r=3`,`s=7`) |
| | `EXIT` | `0x0001b0` | `op0=6` SI/BI family |
| spill/fill | `L32E` | `0x090000` | LSCX `op0=9`; `s=ars`,`t=art`,`immr×4` |
| | `S32E` | `0x490000` | `op0=9` hi-nibble 4 |
| | `S32STK` | `0x698000` | `op0=9` hi-nibble 6, `r`-bit set; `immr_stk` |
| fences | `ISYNC` | `0x002000` | SYNCT `op0=0`,`op1=0`,`op2=0`,`r=2`; `t`=sel |
| | `RSYNC` | `0x002010` | `t=1` |
| | `ESYNC` | `0x002020` | `t=2` |
| | `DSYNC` | `0x002030` | `t=3` |
| | `MEMW` | `0x0020c0` | `t=12` |
| | `EXTW` | `0x0020d0` | `t=13` |
| extreg | `RER` | `0x406000` | `op2=4`,`op1=0,r=6`; `s=ars`,`t=art` |
| | `WER` | `0x407000` | `op1=0,r=7` |

> **NOTE — `S32STK` template is `0x698000`, not `0x690000`.** The hi-nibble-6
> spill-stack store carries the `r`-group bit `0x008000` in its template; the
> assembled word at the maximum legal offset (`s32stk a3,a4,192`) is `0x698430`,
> confirming the base `0x698000` plus `s=4,t=3` plus the `immr_stk` nibble.

> **NOTE — `SYNCT` parent and `EXCW` exclusion.** `SYNC`/`SYNCT` is the
> appendix-P decode-group parent (word `0x002000`, == `ISYNC`); the assembler
> never emits bare `SYNC`, it relaxes to a specific fence. `EXCW` (`t=8`,
> `0x002080`) is the same `SYNCT` family but is the *exception-wait* op assigned
> to the exception-dispatch batch, **not** B27 — listed here only to mark the
> boundary. If `SYNC` is excluded as a non-leaf pseudo, the leaf count is 55;
> with the parent it is 56.

---

## 2. The architectural special-register file (number → name)

[HIGH/OBSERVED] The binary ships **45 `rsr.*`, 44 `wsr.*`, 41 `xsr.*`** encode
thunks (`nm libisa-core.so | rg -c 'Opcode_{r,w,x}sr_.*_Slot_inst_encode'`). That
full set spans all of batches 27/28/29; the **B27 subset** owns the
processor-control / shifter / loop / windowing / prefetch / coprocessor SRs. The
table below is the complete `sr` byte → name map read out of the
`rsr.*`/`wsr.*`/`xsr.*` thunk templates (`SR = (word>>8) & 0xff`). The **B27**
column flags which SRs belong to this page; the rest route to the
exception-dispatch (B28) and debug/timer/cache/mmu (B29) batches.

| SR# | Name | B27? | RSR word | WSR word | XSR word |
|-----|------|------|----------|----------|----------|
| `0x00` | `LBEG`    | **B27** | `0x030000` | `0x130000` | `0x610000` |
| `0x01` | `LEND`    | **B27** | `0x030100` | `0x130100` | `0x610100` |
| `0x02` | `LCOUNT`  | **B27** | `0x030200` | `0x130200` | `0x610200` |
| `0x03` | `SAR`     | **B27** | `0x030300` | `0x130300` | `0x610300` |
| `0x04` | `BR`      | **B27** | `0x030400` | `0x130400` | `0x610400` |
| `0x28` | `PREFCTL` | **B27** | `0x032800` | `0x132800` | `0x612800` |
| `0x48` | `WB` (WindowBase) | **B27** | `0x034800` | `0x134800` | `0x614800` |
| `0x59` | `MMID`    | B28/trace | — | `0x135900` | — |
| `0x5a` | `MPUENB`  | B29 | — | `0x135a00` | `0x615a00` |
| `0x5c` | `MPUCFG`  | B29 | `0x035c00` | `0x135c00` | — |
| `0x5f` | `ERACCESS`| B29 | `0x035f00` | `0x135f00` | `0x615f00` |
| `0x61` | `MEMCTL`  | **B27** | `0x036100` | `0x136100` | `0x616100` |
| `0x62` | `CACHEADRDIS` | B29 | `0x036200` | `0x136200` | `0x616200` |
| `0x63` | `ATOMCTL` | B29 | `0x036300` | `0x136300` | `0x616300` |
| `0x68` | `DDR`     | B29 | `0x036800` | `0x136800` | `0x616800` |
| `0x69` | `IRAM0CFG`| B29 | `0x036900` | `0x136900` | `0x616900` |
| `0x6a` | `DRAM0CFG`| B29 | `0x036a00` | `0x136a00` | `0x616a00` |
| `0x72` | `IEEXTERN`| B28 | `0x037200` | `0x137200` | `0x617200` |
| `0x74` | `IEVEC`   | B28 | `0x037400` | `0x137400` | `0x617400` |
| `0x80`/`0x81` | `IBREAKA0/1` | B29 | `0x038000`/`0x038100` | … | … |
| `0x90`/`0x91` | `DBREAKA0/1` | B29 | `0x039000`/`0x039100` | … | … |
| `0xa0`/`0xa1` | `DBREAKC0/1` | B29 | `0x03a000`/`0x03a100` | … | … |
| `0xb0`/`0xd0` | `CONFIGID0/1` | B29 | `0x03b000`/`0x03d000` | … | … |
| `0xb1` | `EPC1`    | B28 | `0x03b100` | `0x13b100` | `0x61b100` |
| `0xc0`/`0xc1` | `IBREAKC0/1` | B29 | `0x03c000`/`0x03c100` | … | … |
| `0xe0` | `CPENABLE`| **B27** | `0x03e000` | `0x13e000` | `0x61e000` |
| `0xe5` | `MS`      | B28 | `0x03e500` | `0x13e500` | `0x61e500` |
| `0xe6` | `PS`      | **B27** | `0x03e600` | `0x13e600` | `0x61e600` |
| `0xe7` | `VECBASE` | B28 | `0x03e700` | `0x13e700` | `0x61e700` |
| `0xe8` | `EXCCAUSE`| B28 | `0x03e800` | `0x13e800` | `0x61e800` |
| `0xea` | `CCOUNT`  | B29/timer | `0x03ea00` | `0x13ea00` | `0x61ea00` |
| `0xec` | `ISB`     | B28 | — | `0x13ec00` | `0x61ec00` |
| `0xee` | `EXCVADDR`| B28 | `0x03ee00` | `0x13ee00` | `0x61ee00` |
| `0xf0`/`0xf1`/`0xf2` | `CCOMPARE0/1/2` | B29/timer | `0x03f000`/… | … | … |
| `0xf4`/`0xf5` | `MISC0/1` | B29 | — | `0x13f400`/`0x13f500` | … |
| `0xf8`/`0xf9` | `ISL`/`KSL` | B28 | — | `0x13f800`/`0x13f900` | … |

The **B27 SR roster** is therefore the eleven entries
`{LBEG, LEND, LCOUNT, SAR, BR, PREFCTL, WB, MEMCTL, CPENABLE, PS}` plus `MS` for
the windowed-state read (MS is shared with B28; the window primitives read
`MS_DISPST`). The `WindowBase` (`WB`, `0x48`) state args are confirmed present as
`Iclass_xt_iclass_{rsr,wsr,xsr}_windowbase_stateArgs` in `libisa-core.so`.

[HIGH/OBSERVED] The privilege class (gated when `PS.RING != 0 && !InOCDMode`):
`PS`, `MS`, `WB`, `MEMCTL`, `CPENABLE` raise `PrivilegedException`; `SAR`, `BR`,
`LBEG`, `LEND`, `LCOUNT`, `PREFCTL` are user-accessible (no gate). `THREADPTR`
(via `RUR`/`WUR`) is non-privileged user state.

> **NOTE — `IEVEC` (`0x74`) carries the iclass `xt_iclass_*sr.gserr`.** All three
> of `rsr/wsr/xsr.ievec` resolve to a `gserr`-named iclass row — a config-specific
> table-naming alias for the IEVEC slot. The encode template (`0x037400` etc.)
> and symtab are self-consistent; this is a naming observation, not an encode
> anomaly. [MED/INFERRED]

> **CORRECTION (vs. a prior windowed-state note).** `num_states` returns
> `0x51 = 81` (disassembled at `libisa-core.so` `num_states` ⇒ `mov $0x51,%eax;
> ret`) — read **81 (libisa-core) / 87 (merged incl. `libisa-core-hw.so`, whose
> `num_states → 0x6` adds the 6 hardware states)**. The SR set maps onto these
> states. The AR register file is `regfiles[]` index 0 — 32-bit × 64
> physical / 16 visible, **windowed** — and `ENTRY`/`RETW`/`MOVSP` rotate it via
> the `WindowBase`/`WindowStart` SRs. [HIGH/OBSERVED for `num_states`; AR-file
> width CARRIED from the batch template.]

---

## 3. Per-opcode reference

Each entry gives the scalar-form encoding (selector CONST + SR/UR field), the
operands, the C-pseudocode semantics naming the real iclass/issue symbol, the ISS
issue function in `libcas-core.so`, and ≥1 worked round-trip
(`xtensa-elf-as → objdump`, oracle = device disassembler).

### 3.1 SAR — shift amount (SR `0x03`, non-privileged)

```c
// iclass xt_iclass_{rsr,wsr,xsr}.sar ; ISS x24_Inst_0_inst_WSR_SAR_issue
RSR.SAR  art:  AR[t] = SAR;                          // read 6-bit funnel-shift amt
WSR.SAR  art:  SAR   = AR[t] & 0x3f;                 // consumed by SLL/SRL/SRC (B25)
XSR.SAR  art:  tmp = AR[t]; AR[t] = SAR; SAR = tmp;  // atomic
```

* **Encoding** `rsr.sar a3 = 0x030330`, `wsr.sar a3 = 0x130330`,
  `xsr.sar a3 = 0x610330`. Round-trip `wsr.sar a3` ⇒ objdump `wsr.sar a3`
  (bytes LE `30 03 13`). [HIGH/OBSERVED]
* **Side-effects** `WSR.SAR` asserts `XTSYNC` (the funnel shifter samples the new
  amount on the next cycle); `RSR.SAR` defs `art` at the M-stage. **No privilege
  gate** — SAR is user-visible.

### 3.2 PS — processor state (SR `0xe6`, PRIVILEGED)

```c
// iclass xt_iclass_{rsr,wsr,xsr}.ps ; pkg xt_core
RSR.PS  art:  AR[t] = pack{ENTRYNR,SS,STACK,RING,DI,DIEXC,...};
WSR.PS  art:  unpack AR[t] into PS.{ENTRYNR,SS,STACK,RING,DI,DIEXC}; XTSYNC;
XSR.PS  art:  atomic swap (the exception-prologue PS save/restore primitive);
```

* **Encoding** `rsr.ps a4 = 0x03e640`, `wsr.ps a4 = 0x13e640` (LE `40 e6 13`),
  `xsr.ps a4 = 0x61e640`. Round-trip `wsr.ps a4` ⇒ objdump `wsr.ps a4`.
  [HIGH/OBSERVED]
* **Side-effects** PRIVILEGED — reads `PS.RING`/`MS_DISPST`/`InOCDMode`, raises
  `PrivilegedException` when `RING != 0 && !InOCDMode`. `WSR.PS`/`XSR.PS` force a
  full pipeline resync (`XTSYNC` latest of the family) because PS controls
  ring/interrupt/window-callinc state.

### 3.3 MS, WB — micro-arch / window-base state (SR `0xe5`, `0x48`, PRIVILEGED)

```c
// iclass xt_iclass_{r,w,x}sr_MS / *.windowbase
RSR.MS  art:  AR[t] = {MS_DISPST, MS_DE, REV8AR};
WSR.MS  art:  {MS_DISPST,MS_DE,REV8AR} = AR[t];   // blocked under NMILock
RSR.WB  art:  AR[t] = {WB_Start, WB_Number, WB_Current, WB_Pending};
WSR.WB  art:  WB.{S,N,C,P} = AR[t];               // committed early (stage 1)
```

* **Encoding** `rsr.ms a5 = 0x03e550`, `wsr.ms a5 = 0x13e550`,
  `rsr.wb a6 = 0x034860`, `wsr.wb a6 = 0x134860`, `xsr.wb a6 = 0x614860`.
  [HIGH/OBSERVED]
* **Side-effects** Both PRIVILEGED. `WSR.WB` commits `WB_{S,N,C,P}` *early* (the
  window rotation must land before a dependent `ENTRY`/`RETW`); `WSR.MS` is gated
  by `NMILock` (write blocked under NMI). `XSR.WB` exists (`0x614800`) — atomic
  window re-base. ISS `x24_Inst_0_inst_WSR_*` present in `libcas-core.so`.

### 3.4 Loop registers — LBEG `0x00`, LEND `0x01`, LCOUNT `0x02` (non-privileged)

```c
RSR.LBEG/LEND/LCOUNT art:  AR[t] = {LBEG|LEND|LCOUNT};   // identity move
WSR.LBEG/LEND        art:  {LBEG|LEND} = AR[t];          // assert WSRBus (loop fetch)
WSR.LCOUNT           art:  LCOUNT = AR[t]; XTSYNC;        // pipeline-sensitive
// HW (not an op): on PC==LEND && LCOUNT!=0 : PC = LBEG; LCOUNT--;
```

* **Encoding** `rsr.lbeg a7 = 0x030070`, `rsr.lend a8 = 0x030180`,
  `rsr.lcount a9 = 0x030290`. Round-trip `rsr.lcount a9` ⇒ objdump
  `rsr.lcount a9`. [HIGH/OBSERVED]
* **Side-effects** non-privileged. `LBEG`/`LEND` writes broadcast on `WSRBus`
  (they steer the loop-back fetch); `LCOUNT` R/W asserts `XTSYNC` and `RSR.LCOUNT`
  defs `art` one stage later than `LBEG`/`LEND` (the counter read crosses the
  resync). ISS `x24_Inst_0_inst_RSR_LEND_issue` present.

### 3.5 MEMCTL `0x61` (PRIVILEGED) / PREFCTL `0x28` (non-privileged) / CPENABLE `0xe0` (PRIVILEGED) / BR `0x04` (non-privileged)

```c
WSR.MEMCTL   art:  MEMCTLOut = AR[t]; /* live to cache/local-mem ctlrs */ MEMCTL = AR[t];
WSR.PREFCTL  art:  PREFCTL = AR[t];  /* sampled by prefetch engine via WSRBus */
WSR.CPENABLE art:  CPENABLE = AR[t]; /* clearing cp1 -> Coprocessor1Exception on next vector op */
RSR.BR       art:  AR[t][15:0] = {b15..b0};   // pack the 16 boolean regs
WSR.BR       art:  {b15..b0} = AR[t][15:0];
```

* **Encoding** `rsr.memctl a10 = 0x0361a0`, `wsr.memctl a10 = 0x1361a0`,
  `xsr.memctl a10 = 0x6161a0`; `rsr.prefctl a11 = 0x0328b0`,
  `wsr.cpenable a12 = 0x13e0c0`; `rsr.br a13 = 0x0304d0`. All six oracle-validated.
  [HIGH/OBSERVED]
* **Side-effects** `MEMCTL`/`CPENABLE` PRIVILEGED; `WSR.MEMCTL` drives
  `MEMCTLOut` the same cycle (cache/local-mem reconfig); `WSR.CPENABLE` takes
  effect late (W-stage) so in-flight vector ops still see the old mask — the
  architectural hook for lazy-coprocessor context switch and the
  `Coprocessor1Exception` gate that every IVP vector op reads. `PREFCTL`/`BR`
  non-privileged.

### 3.6 THREADPTR — RUR / WUR (UR `0xe7`)

```c
// iclass wur_threadptr / rur ; pkg xt_core
RUR.THREADPTR arr:  AR[r] = THREADPTR;     // AR DEST in r-field
WUR.THREADPTR art:  THREADPTR = AR[t];     // AR SRC  in t-field
```

* **Encoding** thunk templates `rur=0xe30e70` (`op2=0xe`), `wur=0xf3e700`
  (`op2=0xf`). Round-trip `rur.threadptr a7 = 0xe37e70`,
  `wur.threadptr a8 = 0xf3e780`. [HIGH/OBSERVED] See §1.2 GOTCHA for the r/t
  field swap.
* **Side-effects** non-privileged TLS pointer; no exception.

### 3.7 Register-window control

```c
// ENTRY  (op0=6 SI/BI1) ; iclass xt_iclass_entry ; ISS x24_Inst_0_inst_ENTRY_issue
ENTRY as, imm12x8:
    new_sp = AR[s] - (imm12 << 3);              // uimm12x8: imm is BYTES, scaled x8
    if (call0_ABI)              raise WindowInstCall0Exception;
    if (new_sp < ISL || new_sp < KSL) raise StackLimitViolation;   // XEA3 stack-limit
    rotate_window(PS.CALLINC); AR[entry_target] = new_sp;

// RETW  (0x000090, JR family) ; iclass xt_iclass_retw
RETW:
    WindowBase -= prior_CALLINC;
    if (underflow) vector to UNDERFLOW_4/8/12 handler;
    else branch to return PC;

// MOVSP at, as (0x001000) ; iclass xt_iclass_movsp
MOVSP:
    if (window partly spilled) force spill via movsp trap;  // StackLimitViolation possible
    else AR[t] = AR[s];

ROTW simm4  (0x408000): WindowBase.Current += simm4;        // PRIVILEGED
SETW simm4  (0x408100): WindowBase.Pending  = simm4;        // PRIVILEGED
SPILLW (0x003600): force-spill all live windows to stack;    // ctx save
TOSSW  (0x003700): discard spilled-window state (frames dead);
EXIT   (0x0001b0): pop dispatched window frame and resume;
```

* **Encoding round-trips** `entry a1, 32 = 0x004136` (imm-nibble 4 → 4×8=32),
  `movsp a2,a3 = 0x001320`, `rotw 1 = 0x408010`, `rotw -3 = 0x4080d0`
  (`simm4=0xd`), `rotw -8 = 0x408080`, `setw 2 = 0x408120`, `spillw = 0x003600`,
  `tossw = 0x003700`, `exit = 0x0001b0`. [HIGH/OBSERVED]
* **`RETW` relaxation** the assembler relaxes `retw` → `retw.n` (`0xf01d`,
  density 2-byte) whenever it can; both forms share iclass `xt_iclass_retw` and
  carry identical semantics. The wide form `0x000090` is reachable only inside a
  `no-transform` region. [HIGH/OBSERVED]

> **GOTCHA — ENTRY is XEA3 with a hard 32-byte minimum and ×8 alignment.** The
> `ncore2gp-params` config carries `ExceptionArch3 = 1` / `NewExceptionArch = 1`
> — this is **XEA3**. The assembler enforces three rules on the `ENTRY` immediate:
> (1) it must be a **multiple of 8** (`entry a1, 33` ⇒ "invalid value '33'");
> (2) the stack decrement must be **≥ 32** (`entry a1, 0` and `entry a1, 8` ⇒
> "entry instruction with stack decrement < 32 in XEA3"); (3) the maximum is
> **32760** (`= 4095 × 8`; `entry a1, 32768` ⇒ "invalid value" + "unaligned entry
> instruction"). The legal range is therefore `imm ∈ [32, 32760]`, step 8. Any
> reimplementation must reproduce the **XEA3** stack-limit semantics, not the
> older XEA2 window-overflow/underflow vectoring. [HIGH/OBSERVED — CORRECTION]

### 3.8 Windowed spill/fill memory — L32E / S32E / S32STK

```c
// L32E at, as, imm  (0x090000) ; PRIVILEGED window-fill load
L32E:  AR[t] = mem32[AR[s] + (immr << 2)];  // interrupted-frame ring/EPC context
                                            // exc: PrivilegedException, L32ETailchain
// S32E at, as, imm  (0x490000) ; window-overflow spill store
S32E:  mem32[AR[s] + (immr << 2)] = AR[t];  // interrupted-frame context

// S32STK at, as, imm (0x698000) ; stack-limit-checked spill store
S32STK: spill AR[t] to stack at AR[s]+imm; produce updated SP;
        if (cross ISL) raise ISLStackLimitViolation;
        if (cross KSL) raise KSLStackLimitViolation;
```

* **Encoding round-trips** `l32e a3,a4,-64 = 0x090430`, `s32e a3,a4,-64 =
  0x490430`. [HIGH/OBSERVED]
* **`S32STK` immediate field** legal values are **positive, [80, 192], step 16**
  (`immr_stk` is a 4-bit field encoding a fixed-granularity downward stack
  offset). The descending sweep: `imm=80 → 0x69f430`, `96 → 0x69e430`,
  `… 192 → 0x698430` (nibble `0x10 − imm/16`). All other tested values
  (0, ±4, ±64, 32, 2048, …) are **rejected** by the assembler. [HIGH/OBSERVED]

> **QUIRK — S32STK is positive-only and coarse.** Unlike `L32E`/`S32E` (which
> accept signed `immr×4` offsets such as −64), `S32STK` encodes only a small
> positive, ×16-quantised guard-band offset. This is the hardware-assisted
> safe-spill primitive: its `immr_stk` field is the guard distance, not a free
> displacement. A reimplementation that reuses the `L32E` offset encoder for
> `S32STK` will mis-encode every spill.

### 3.9 Memory-ordering fences (SYNCT group, `t`-field selector)

All fences share `op0=0,op1=0,op2=0,r=2`; the `t`-field selects the domain. No
data operands, no regfile DEF — a schedule-only barrier (the ISS DEF list is
empty for every fence).

| Mn | `t` | Word | Domain ordered | ISS USE |
|----|-----|------|----------------|---------|
| `ISYNC` | 0 | `0x002000` | flush fetch/decode so prior WSR/cache/config writes are seen by subsequent fetch (full I-barrier) | — |
| `RSYNC` | 1 | `0x002010` | stall until pending SR/AR writes (`XTSYNC`) retire | `XTSYNC@3` |
| `ESYNC` | 2 | `0x002020` | execution sync — superset of `RSYNC` | `XTSYNC@4` |
| `DSYNC` | 3 | `0x002030` | order load/store + `WSR.MEMCTL` vs following memory ops | `XTSYNC@2` |
| `MEMW`  | 12| `0x0020c0` | full memory-access barrier (the C "memory clobber") | `MEMW@2` |
| `EXTW`  | 13| `0x0020d0` | drain the **external/device** write buffer | `EXTW@8` |

* **Round-trips** `isync=0x002000`, `rsync=0x002010`, `esync=0x002020`,
  `dsync=0x002030`, `memw=0x0020c0`, `extw=0x0020d0` — all oracle-validated.
  [HIGH/OBSERVED]

> **GOTCHA — the four `*SYNC` fences are NOT interchangeable.** They resync
> different pipeline domains, distinguishable by their ISS `XTSYNC` stage:
> `DSYNC` (stage 2, data-memory ordering) < `RSYNC` (stage 3, register writes) <
> `ESYNC` (stage 4, full execution). `ISYNC` is the *instruction-fetch* barrier —
> the only one that flushes the fetch/decode front-end, and the one that **must**
> follow any `WSR` to a fetch-affecting register (`MEMCTL`, cache config,
> `IBREAK*`) before the change is observed by an instruction fetch. `EXTW`
> (stage 8) is the deepest — it drains the *external* write buffer and must
> bracket device-register stores done through `WER`. Picking `MEMW` where `EXTW`
> is needed leaves device writes unflushed. [HIGH/OBSERVED for stages]

### 3.10 External-register bus — RER / WER (PRIVILEGED)

```c
// RER at, as (0x406000) ; iclass xt_iclass_rer ; ISS x24_Inst_0_inst_RER_issue
RER:  AR[t] = external_reg[AR[s]];   // ER bus: interrupt distributor / fabric regs
      if (!ERACCESS || PS.RING) raise ExternalRegisterPrivilegeException;
// WER at, as (0x407000) ; iclass xt_iclass_wer ; ISS x24_Inst_0_inst_WER_issue
WER:  external_reg[AR[s]] = AR[t];   // commonly ack/mask IRQs in the distributor
      // DEFs ActivePriority/Interrupt/Fairness/CurrentPriority + WERBus
```

* **Encoding round-trips** `rer a11,a12 = 0x406cb0` (`s=0xc,t=0xb`),
  `wer a11,a12 = 0x407cb0`. The read/write delta is the `r`-field
  `+0x1000` (`RER r=6`, `WER r=7`). [HIGH/OBSERVED]
* **Side-effects** both PRIVILEGED (gated by `ERACCESS` + `PS.RING`). `RER` has a
  deep result latency (bus round-trip, `art@6`); `WER` defs the
  interrupt-controller state and `WERBus`, then an `ERI_RAW_INTERLOCK` at the
  late stage. [HIGH/OBSERVED for ISS presence; bus-latency stages CARRIED.]

---

## 4. ISS / INSTR_SCHEDULE timing

[HIGH/OBSERVED] Each B27 op has a dedicated `x24_Inst_0_inst_<MN>_issue` in
`libcas-core.so` (DWARF-bearing; e.g. `WSR_SAR_issue` is 0x59 bytes at
`0x1678820`); `RETW.N` uses the 16-bit `x16b_Inst16b_0_inst_RETW_N_issue`. The
pipeline-stage root is `rstage=0 / estage=3 / mstage=4 / wstage=6`. Summary of
the USE/DEF stage profile (stage relative to issue):

| Op class | result/state DEF | barrier / resync |
|----------|------------------|------------------|
| `RSR.<S>` (read) | `art` @ M-stage (4–5); `LCOUNT` read @5 | — |
| `WSR.SAR/BR/PREFCTL` | SR @4 | `XTSYNC`/`WSRBus`@6 |
| `WSR.LBEG/LEND` | SR @4 | `WSRBus`@6 |
| `WSR.LCOUNT` | SR @4 | `XTSYNC`@6 |
| `WSR.PS`/`XSR.PS` | fields @4 | `XTSYNC`@7 (latest — full resync) |
| `WSR.WB` | `WB_{S,N,C,P}` @1 (early) | trace@6 |
| `WSR.MEMCTL` | `MEMCTLOut`@3, `MEMCTL`@6 | — |
| `WSR.CPENABLE` | `CPENABLE`@6 (late) | — |
| `ENTRY` | `ars_entry`@1, stack-limit-exc@1 | — |
| `RETW` | `BranchTarget`/`Taken`@3, Underflow@1 | — |
| `MOVSP` | `art`@1, stack-limit-exc@1 | — |
| `L32E` | `art`@5; branch/exc@3 | — |
| `S32E` | `ScalarMemDataOut32`@5 | — |
| `S32STK` | `ar_stk`@4, `ar_stk_early`@1, MemOut@5 | — |
| fences | (none — empty DEF) | `XTSYNC`/`MEMW`/`EXTW` USE @2–8 |
| `RER` | `art`@6 (bus), `ERRead`@3 | — |
| `WER` | IRQ-state@5, `WERBus`@6 | `ERI_RAW_INTERLOCK`@7 |

[MED/INFERRED] The exact integer stage of each USE/DEF is read from the
`INSTR_SCHEDULE` USE/DEF lists; the bus-latency stages for `RER`/`WER` and the
fence USE stages are corroborated by the ISS function presence but the precise
cycle counts are inferred over the stage root.

---

## 5. Worked bit-patterns (oracle-validated)

Every word below was produced by `xtensa-elf-as` and disassembled back by
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) to the listed mnemonic.

```
4.1  rsr.sar a3   -> 0x030330
     op0=0[3:0] op1=3[19:16] op2=0[23:20] sr=0x03[15:8] t=3(a3)[7:4]
     = 0 | (3<<16) | (0<<20) | (0x03<<8) | (3<<4) = 0x030330   LE: 30 03 03
     SEM: AR[3] = SAR

4.2  wsr.ps a4    -> 0x13e640   (privileged)
     op1=3 op2=1 sr=0xe6 t=4 = (3<<16)|(1<<20)|(0xe6<<8)|(4<<4) = 0x13e640  LE: 40 e6 13
     SEM: unpack AR[4] into PS.{RING,STACK,SS,DI,...}; XTSYNC@7; PrivilegedException if RING!=0

4.3  xsr.sar a3   -> 0x610330   (atomic swap)
     op1=1 op2=6 sr=0x03 t=3 = (1<<16)|(6<<20)|(0x03<<8)|(3<<4) = 0x610330  LE: 30 03 61
     SEM: tmp=AR[3]; AR[3]=SAR; SAR=tmp

4.4  entry a1, 32 -> 0x004136
     op0=6 base 0x000036 | s=1[11:8] | imm12=4[23:12] (32 = 4<<3, uimm12x8 x8)
     = 0x000036 | (1<<8) | (4<<12) = 0x004136   LE: 36 41 00
     SEM: new_sp = AR[1]-(4<<3); rotate window; XEA3 stack-limit check vs ISL/KSL

4.5  dsync       -> 0x002030
     SYNCT op0=0,op1=0,op2=0,r=2, t=3 (DSYNC) = (2<<12)|(3<<4) = 0x002030  LE: 30 20 00
     SEM: data-memory ordering barrier; USE XTSYNC@2, empty DEF

4.6  l32e a3,a4,-64 -> 0x090430
     op0=9 base 0x090000 | s=4[11:8] | t=3[7:4] | immr×4 encodes -64
     SEM: AR[3] = mem32[AR[4]-64] in the interrupted-frame context

4.7  rer a11,a12 -> 0x406cb0
     base 0x406000 | s=0xc[11:8] | t=0xb[7:4]
     SEM: AR[11] = external_reg[AR[12]] over the ER bus; art@6; ExternalRegisterPriv exc

4.8  s32stk a3,a4,192 -> 0x698430   (max legal offset)
     op0=9 base 0x698000 | s=4 | t=3 | immr_stk nibble 8 ([80,192] step16, positive-only)
     SEM: spill AR[3] to stack at guard offset; ISL/KSL stack-limit checked
```

---

## 6. Verification ledger

5 strongest claims, adversarially re-assembled with the device oracle (independent
of the prior decode-chain resolver):

1. **R/W/X selector lattice `rsr=0x00030000|SR<<8`, `wsr=0x00130000|SR<<8`,
   `xsr=0x00610000|SR<<8`.** [VERIFIED] `rsr.memctl a10/wsr.memctl a10/xsr.memctl
   a10` ⇒ `0x0361a0 / 0x1361a0 / 0x6161a0`; XOR `wsr^rsr=0x100000`,
   `xsr^rsr=0x620000`. Byte-exact.
2. **`ENTRY` is XEA3 with min-32 / ×8 / max-32760 immediate.** [VERIFIED]
   `ncore2gp-params: ExceptionArch3=1`; assembler rejects `33` (non-×8), `0`/`8`
   (< 32, message names "XEA3"), `32768` (> max + unaligned); accepts `32 →
   0x004136` and `32760`.
3. **`RUR`/`WUR` place the AR operand in different fields (r vs t).** [VERIFIED]
   `rur.threadptr a7=0xe37e70` (AR in r, `^template=0x7000`); `wur.threadptr
   a8=0xf3e780` (AR in t, `^template=0x80`).
4. **`S32STK` immediate is positive-only, [80,192] step 16, template 0x698000.**
   [VERIFIED] Sweep: only 80…192 step-16 assemble; `192 → 0x698430`,
   `80 → 0x69f430`; all signed/other values rejected.
5. **The four fences are distinct words / domains, `RETW` relaxes to `RETW.N`.**
   [VERIFIED] `isync/rsync/esync/dsync/memw/extw =
   0x002000/10/20/30/c0/d0`; bare `retw` ⇒ disassembles `retw.n` (`0xf01d`) unless
   in a `no-transform` region.

**Count.** 56 B27 ops (55 leaf + `SYNC`/`SYNCT` parent). All B27 mnemonics are
present in `libisa-core.so` as `Opcode_<mn>_Slot_inst[16b]_encode` thunks; the
roster anchors to `nm` (45 `rsr.*` / 44 `wsr.*` / 41 `xsr.*` total across all
system batches; B27 owns the 11-SR processor/shift/loop/window/prefetch/coproc
subset). `num_states = 81` (OBSERVED). [HIGH/OBSERVED]

**Confidence summary.** Encoding templates, the SR/UR number map, the privilege
class, the XEA3 / S32STK / RUR-WUR / RETW-relax behaviors, and the fence words are
**HIGH/OBSERVED** (binary bytes + device oracle). The precise ISS USE/DEF cycle
numbers are **MED/INFERRED** over the `INSTR_SCHEDULE` stage root; the `RER`/`WER`
external-address-space layout and the `LBEG`/`LEND` HW loop-back trigger are
**LOW/INFERRED** (mechanism resolved, config-specific layout not enumerated).

---

*See also:* [Atomics + Memory-Ordering Model](../../uarch/atomics-ordering.md) ·
[Boot / Reset Sequence](../../uarch/boot-reset.md) ·
[SEQ Uarch Register Model + Single-Step Debugger](../../firmware/seq/uarch-debugger.md) ·
[B25 — scalar arith/logic/shift](b25-xt-core.md) ·
[B26 — ld/st/branch/density/MUL32](b26-xt-ctrl.md) ·
[ISA Template & 30-Batch Partition](template-and-partition.md).
