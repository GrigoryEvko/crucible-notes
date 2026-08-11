# ISA Batch 29 — base-Xtensa Debug / Timer / Cache / RAM / MMU-MPU / Atomic

This page documents the **88** base-Xtensa **privileged-platform-control** opcodes
of the GPSIMD NeuronCore (Cadence Tensilica Vision-Q7 NX "Cairo", config
`ncore2gp`): the on-chip-debug breakpoint registers
`{IBREAKA,IBREAKC,DBREAKA,DBREAKC}{0,1}` plus the OCD `DDR` scan mailbox, the
three-timer subsystem `CCOUNT`/`CCOMPARE0..2`, the instruction-cache hint/test
ops `IHI`/`IPF`/`III`/`LICT`/`SICT`/`LICW`/`SICW`, the local-RAM + cache + MPU
config registers `CACHEADRDIS`/`IRAM0CFG`/`DRAM0CFG`/`MPUENB`/`MPUCFG`, the
atomic-ordering control `ATOMCTL` (the control register of the `L32EX`/`S32EX`
exclusive-access pair), the external-register access window `ERACCESS` (the gate
in front of B27's `RER`/`WER`), the OCD fast load/store `LDDR32.P`/`SDDR32.P`,
the MPU/TLB region ops `PPTLB`/`RPTLB0`/`RPTLB1`/`WPTLB`, the identity/trace
registers `CONFIGID0`/`CONFIGID1`/`MMID`/`PRID`, the `MISC0`/`MISC1` scratch
registers, the OCD debug-return `RFDO`, and the four stray pure-datapath misc ops
`SEXT`/`CLAMPS`/`NSA`/`NSAU`. These ground the debug/profiling surface, the
memory-ordering model (see [Atomics + Ordering](../../uarch/atomics-ordering.md)),
the MPU isolation model, and the trace/debug access-gating perimeter (see
[Profiling / Trace / Debug Gating](../../control/security/profiling-trace-debug-gating.md)).

> **CRITICAL FRAMING.** Every op on this page is a **non-FLIX scalar 24-bit
> decode-tree instruction**, **not** an IVP vector-datapath op. None are
> density-narrow and none carry the FLIX vector marker; all 88 disassemble as
> 3-byte words. The Encoding section names the *scalar-form bit-fields and the
> selector CONST* read out of each `Opcode_<mn>_Slot_inst_encode` thunk — **not**
> a FLIX format+slot. The whole-instruction template word IS the literal Xtensa
> scalar encoding with the AR operand fields zeroed; this page shows the round-trip
> with `t = a3` (or `s/t = a3/a4`) so the operand nibbles are non-zero. Sibling
> scalar pages: [B27 — system / SR / regwin / sync](b27-xt-system.md) (the
> `RSR`/`WSR`/`XSR` SR layer and the `RER`/`WER` opcodes B29 does **not** re-own),
> [B28 — exception dispatch / bool / loop / minmax](b28-xt-exc.md). Partition
> contract: [Template & 30-Batch Partition](template-and-partition.md). Preceding
> committed boundary: [B23 — divide](b23-divide.md).

All bytes on this page are read directly from
`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libisa-core.so`
(ELF64, **not stripped**; the `Opcode_<mn>_Slot_inst_encode` thunk for each op is
`movl $W,(%rdi); ret`) and round-tripped through the device assembler
`tools/XtensaTools/bin/xtensa-elf-as` and disassembler `xtensa-elf-objdump` with
`XTENSA_CORE=ncore2gp`, `XTENSA_SYSTEM=tools/XtensaTools/config`. Silicon-config
facts (cache/MPU/timer/debug/atomic sizes) are read from the shipped
`tools/ncore2gp/xtensa-elf/arch/include/xtensa/config/core-isa.h`. ISS issue/stall
timing is read from the DWARF-bearing companion
`tools/ncore2gp/config/libcas-core.so` (`x24_Inst_0_inst_<MN>_{issue,stall}`).
Value semantics for the datapath ops are grounded against the `module__xdref_*`
leaves of `tools/ncore2gp/config/libfiss-base.so`, **driven live** via `ctypes`.
Confidence tags: **HIGH/MED/LOW × OBSERVED** (literal bytes / oracle round-trip /
live value-fn) / **INFERRED** (derived over OBSERVED + canonical Xtensa
arithmetic) / **CARRIED** (from a sibling batch report). The page default is
`[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag.

---

## 0. Headline

All **88** B29 opcodes resolve end-to-end and round-trip through
the device toolchain. `nm libisa-core.so | rg -c` counts **70** distinct
SR-access encode thunks (`{rsr,wsr,xsr}` of the 26 B29 special registers) and
**18** distinct non-SR thunks (`clamps ihi iii ipf lddr32.p lict licw nsa nsau
pptlb rfdo rptlb0 rptlb1 sddr32.p sext sict sicw wptlb`), for **88** total. Every
candidate word emitted by the field-resolver was assembled by `xtensa-elf-as` and
disassembled back by `xtensa-elf-objdump` to the exact mnemonic + operands.

**Config grounding** (`core-isa.h`, shipped):
`ICACHE_SIZE = 16384` (16 KB, `ICACHE_LINESIZE = 64`, `ICACHE_WAYS = 4`) ⇒ the
icache ops are real; `DCACHE_SIZE = 0` ⇒ **no** L1 dcache, so the dcache peers
`DHI`/`DHU`/`DII`/`DIU`/`DPF` are **absent** from the opcode DB (`DCACHE_TEST = 0`)
and this batch carries **icache-only** cache ops. `HAVE_MPU = 1`,
`MPU_ENTRIES = 16`, `MPU_BACKGROUND_ENTRIES = 2`, `MPU_ALIGN = 4096`;
`HAVE_PTP_MMU = 0`, `HAVE_TLBS = 0`, `HAVE_SPANNING_WAY = 0` ⇒ a 16 fg + 2 bg
region MPU at 4 KB granularity under flat identity mapping, **not** a page-table
MMU. `NUM_TIMERS = 3` (`TIMER{0,1,2}_INTERRUPT = 28/29/30`). `HAVE_OCD = 1`,
`NUM_IBREAK = 2`, `NUM_DBREAK = 2`, `HAVE_OCD_LS32DDR = 1`. `HAVE_EXCLUSIVE = 1`
(`L32EX`/`S32EX`), `HAVE_S32C1I = 0` (no compare-and-swap). `NUM_MISC_REGS = 2`.
`HAVE_PRID = 1`. `HAVE_EXTERN_REGS = 1` (`WER`/`RER`, owned by B27/B26).
`HW_CONFIGID0 = 0xC4019686`, `HW_CONFIGID1 = 0x2908E4E3`.

**Privilege.** All 70 SR-access ops, plus
`III`/`LICT`/`SICT`/`LICW`/`SICW`/`LDDR32.P`/`SDDR32.P`/`PPTLB`/`RPTLB0`/`RPTLB1`/`WPTLB`,
are PRIVILEGED — each raises `PrivilegedException` and reads
`PSRING` (PS.RING) / `MS_DISPST` / `InOCDMode` at estage 3. The only
**non-privileged** B29 ops are the cache **hints** `IHI`/`IPF` (no privilege gate,
empty exception set) and the four pure-datapath ops `SEXT`/`CLAMPS`/`NSA`/`NSAU`.
`RFDO` is an OCD-mode op (raises `InvalidOperand` outside OCD). **None** of the
88 carry `CPENABLE`/`Coprocessor1Exception` — that distinguishes the entire base
scalar surface from every IVP vector op (B01–B24).

---

## 1. Encoding model — the scalar decode skeleton

All 88 B29 ops share the base-Xtensa field map. The 24-bit word
`W` is emitted little-endian (`movl $W,(%rdi)` in the encode thunk; low byte
first):

```
bit:  23..20  19..16  15..12  11..8   7..4    3..0
fld:   op2     op1      r       s       t      op0
```

| Field | Bits | Meaning |
|-------|------|---------|
| `op0` | `[3:0]`   | major opcode |
| `t`   | `[7:4]`   | `art` — AR operand; the SR data port for every `RSR`/`WSR`/`XSR` |
| `s`   | `[11:8]`  | `ars` — AR address/source; the cache/tlb/OCD-LS index operand |
| `r`   | `[15:12]` | `arr` — AR destination (`SEXT`/`CLAMPS`); or sub-op selector for the cache-test / MPU / `RFDO` ops |
| `op1` | `[19:16]` | decode sub-group selector |
| `op2` | `[23:20]` | leaf selector within the sub-group |
| `sr`  | `[15:8]`  | 8-bit Special-Register number = `r‖s` (for `RSR`/`WSR`/`XSR`) |

### 1.1 The R/W/X special-register lattice

The 70 SR-access ops are `RSR`/`WSR`/`XSR` with `op0 = 0`,
`sr[15:8] = SR#`, and `t = art`:

| Family | `op0` | `op1` | `op2` | Effect |
|--------|-------|-------|-------|--------|
| `RSR.<S>` | `0` | `3` | `0` | `art ← S` |
| `WSR.<S>` | `0` | `3` | `1` | `S ← art` |
| `XSR.<S>` | `0` | `1` | `6` | atomic `art ↔ S` swap |

The SR number sits as `sr = (r << 4) | s`. Worked: `RSR.PRID a3 = 0x03eb30`
(`op1 = 3 → [19:16]`, `op2 = 0`, `sr = 0xeb`, `t = 3`); `WSR.MMID a3 = 0x135930`
(`op2 = 1 → +0x100000`); `XSR.ATOMCTL a3 = 0x616330` (`op1 = 1`, `op2 = 6`).

**SR direction asymmetry.** Not every SR offers all three
directions. The asymmetry is proven two ways: the encode thunk for the missing
form is **absent** from `libisa-core.so` (`nm | rg -c` returns 0), and the device
assembler **rejects** the missing spelling. The eight absent forms are
`{wsr,xsr}.prid`, `{wsr,xsr}.configid1`, `{rsr,xsr}.mmid`, `xsr.configid0`,
`xsr.mpucfg`:

| SR | RSR | WSR | XSR | Class |
|----|-----|-----|-----|-------|
| `PRID`, `CONFIGID1` | ✓ | — | — | read-only |
| `MMID` | — | ✓ | — | write-only |
| `CONFIGID0`, `MPUCFG` | ✓ | ✓ | — | RSR+WSR (no XSR) |
| all other 21 B29 SRs | ✓ | ✓ | ✓ | full R/W/X |

Arithmetic: 21 full-R/W/X × 3 = 63, 2 RSR+WSR × 2 = 4, 2 RSR-only = 2,
1 WSR-only = 1 ⇒ `63 + 4 + 2 + 1 = 70` SR-access ops across 26 distinct B29 SRs.

### 1.2 The non-SR families (decode-group parents folded to the QRST root)

The 18 non-SR ops fold their appendix-P decode-group parents to
the `QRST`/`CORE` root; the leaf field is the cache-op selector / MPU sub-op /
immediate:

| Op family | `op0` | `op1` | `op2` | Selector | Operands |
|-----------|-------|-------|-------|----------|----------|
| `IHI`/`IPF`/`III` | `2` | — | — | `t` = cache-op (`IHI 0xe`, `IPF 0xc`, `III 0xf`) | `s = ars`, `uimm8x4 @[15:8]` |
| `LICT`/`SICT`/`LICW`/`SICW` | `0` | `1` | `15` | `r[15:12]` = `0/1/2/3` | `s = ars`, `t = art` |
| `LDDR32.P`/`SDDR32.P` | `0` | `0` | `0` | `r = 7`, `t = 0xe`(ld)/`0xf`(st) | `s = ars` |
| `PPTLB`/`RPTLB0`/`RPTLB1`/`WPTLB` | `0` | — | `5` | `r` = `0xd/0xb/0xf/0xe` | `s = ars`, `t = art` |
| `RFDO` | `0` | `1` | `15` | `r = 0xe`, `imms = s` | — |
| `NSA`/`NSAU` | `0` | — | `4` | `r = 0xe`(NSA)/`0xf`(NSAU) | `s = ars`, `t = art` |
| `SEXT`/`CLAMPS` | `0` | `3` | `2`(SEXT)/`3`(CLAMPS) | `r = dst`, `s = src` | `t = imm − 7` (range 7..22) |

> **GOTCHA — `op0` is the family discriminator.** The cache **hints**
> `IHI`/`IPF`/`III` live in the `LSAI`/`CACHE` group at `op0 = 2`; everything else
> in this batch (SR access, cache-test, OCD-LS, MPU, `RFDO`, `NSA`, `SEXT`) is at
> `op0 = 0`. A decoder must split on `op0` first, then on `op1`/`op2`, then on the
> per-family `r`/`t` selector nibble. Don't conflate `III` (`op0 = 2`, the
> privileged icache invalidate) with `LICT`/`SICT` (`op0 = 0`, the icache-test
> ports) — they are different decode groups despite both touching the icache.

---

## 2. The B29 special-register file (number → name)

All 26 SR numbers were recovered as `sr = (W >> 8) & 0xff` from
each `RSR.<S>` template word and match `core-isa.h`. The RSR base template (with
`t = 0`) is `0x03<sr>00`; `WSR` adds `0x100000`, `XSR` is `0x6<sr>00` with the
`r`/`s` nibbles re-derived from `sr`. (Below the words carry `t = a3` to show a
live operand; subtract `0x30` for the bare base.)

| SR# | Name | Owner | RSR `t=a3` | WSR `t=a3` | XSR `t=a3` |
|-----|------|-------|-----------|-----------|-----------|
| `0x59` | `MMID` | B29/trace | — | `0x135930` | — |
| `0x5a` | `MPUENB` | B29/mpu | `0x035a30` | `0x135a30` | `0x615a30` |
| `0x5c` | `MPUCFG` | B29/mpu | `0x035c30` | `0x135c30` | — |
| `0x5f` | `ERACCESS` | B29/extreg | `0x035f30` | `0x135f30` | `0x615f30` |
| `0x62` | `CACHEADRDIS` | B29/cache | `0x036230` | `0x136230` | `0x616230` |
| `0x63` | `ATOMCTL` | B29/atomic | `0x036330` | `0x136330` | `0x616330` |
| `0x68` | `DDR` | B29/debug | `0x036830` | `0x136830` | `0x616830` |
| `0x69` | `IRAM0CFG` | B29/ram | `0x036930` | `0x136930` | `0x616930` |
| `0x6a` | `DRAM0CFG` | B29/ram | `0x036a30` | `0x136a30` | `0x616a30` |
| `0x80`/`0x81` | `IBREAKA0/1` | B29/debug | `0x038030`/`0x038130` | `0x138030`/… | `0x618030`/… |
| `0x90`/`0x91` | `DBREAKA0/1` | B29/debug | `0x039030`/`0x039130` | `0x139030`/… | `0x619030`/… |
| `0xa0`/`0xa1` | `DBREAKC0/1` | B29/debug | `0x03a030`/`0x03a130` | `0x13a030`/… | `0x61a030`/… |
| `0xb0` | `CONFIGID0` | B29/config | `0x03b030` | `0x13b030` | — |
| `0xc0`/`0xc1` | `IBREAKC0/1` | B29/debug | `0x03c030`/`0x03c130` | `0x13c030`/… | `0x61c030`/… |
| `0xd0` | `CONFIGID1` | B29/config | `0x03d030` | — | — |
| `0xea` | `CCOUNT` | B29/timer | `0x03ea30` | `0x13ea30` | `0x61ea30` |
| `0xeb` | `PRID` | B29/id | `0x03eb30` | — | — |
| `0xf0`/`0xf1`/`0xf2` | `CCOMPARE0/1/2` | B29/timer | `0x03f030`/`0x03f130`/`0x03f230` | `0x13f030`/… | `0x61f030`/… |
| `0xf4`/`0xf5` | `MISC0/1` | B29/scratch | `0x03f430`/`0x03f530` | `0x13f430`/… | `0x61f430`/… |

> **NOTE — disjoint from B27/B28.** B29 carries **zero** SR overlap with B27's
> `{SAR, PS, MS, WB, MEMCTL, PREFCTL, CPENABLE, BR}` or B28's loop
> `{LBEG, LEND, LCOUNT}` and exception-dispatch
> `{EPC, EXCCAUSE, EXCVADDR, IEVEC, IEEXTERN, VECBASE, ISB, ISL, KSL}` sets. The
> external-register bus opcodes `RER`/`WER` stay in B27; B29 owns only `ERACCESS`,
> the SR that **gates** them.

---

## 3. Per-opcode reference

Shared `RSR`/`WSR`/`XSR` ABI (identical to [B27 §1.1](b27-xt-system.md)):
`RSR.<S> → art ← S`; `WSR.<S> → S ← art`; `XSR.<S> → atomic art ↔ S`. Every B29
SR is PRIVILEGED (`PrivilegedException` if `PS.RING != 0 && !InOCDMode`).

### 3.1 Instruction breakpoint — address (`IBREAKA0` `0x80`, `IBREAKA1` `0x81`)

* **Mnemonic** `{RSR,WSR,XSR}.{IBREAKA0,IBREAKA1}` — the 2 hardware
  instruction-breakpoint address registers (`NUM_IBREAK = 2`).
* **Semantics.** `IBREAKAn` holds a virtual instruction address; when the fetch
  PC matches an enabled `IBREAKAn` (enable bit lives in the paired `IBREAKCn`,
  §3.2), the core takes a debug break-in. `RSR` samples the armed address, `WSR`
  re-arms it, `XSR` swaps.

```c
// canonical OCD instruction-breakpoint match, evaluated at fetch
bool ibreak_hit(uint32_t pc) {
  for (int n = 0; n < XCHAL_NUM_IBREAK /* = 2 */; n++)
    if ((IBREAKENABLE >> n) & 1)            // IBREAKCn enable bit
      if (pc == IBREAKAn[n]) return true;   // exact-address match, no mask
  return false;
}
```

* **Encoding.** `sr = 128/129`. `RSR.IBREAKA0 a3 = 0x038030`,
  `WSR.IBREAKA0 a3 = 0x138030`, `XSR.IBREAKA0 a3 = 0x618030`; `IBREAKA1` is
  `sr + 1`.
* **ISS.** `RSR` DEFs `art @5`; `WSR` DEFs `IBREAKA* @6` (commit at wstage).
  No `XTSYNC` (arming the *address* alone does not resync the fetch pipe; the
  *enable* in `IBREAKC` does — §3.2).

### 3.2 Instruction breakpoint — control (`IBREAKC0` `0xc0`, `IBREAKC1` `0xc1`)

* **Mnemonic** `{RSR,WSR,XSR}.{IBREAKC0,IBREAKC1}` — the per-breakpoint
  enable + match-mode word for the paired `IBREAKA`.
* **Semantics.** `IBREAKCn` arms/disarms the instruction breakpoint; writing it
  resyncs the fetch pipeline (`WSR`/`XSR` assert `XTSYNC @6`).
* **Encoding.** `sr = 192/193`. `RSR.IBREAKC0 a3 = 0x03c030`,
  `WSR.IBREAKC0 a3 = 0x13c030`, `XSR.IBREAKC0 a3 = 0x61c030`.
* **ISS.** `WSR` DEFs `IBREAKC* @6 + XTSYNC @6`; `RSR` DEFs `art @5`.

### 3.3 Data breakpoint — address (`DBREAKA0` `0x90`, `DBREAKA1` `0x91`)

* **Mnemonic** `{RSR,WSR,XSR}.{DBREAKA0,DBREAKA1}` — the 2 hardware
  data-watchpoint address registers (`NUM_DBREAK = 2`).
* **Semantics.** A load/store whose effective address matches an enabled
  `DBREAKAn` (under the `DBREAKCn` mask/mode, §3.4) takes a data-watchpoint debug
  exception. `WSR`/`XSR` assert `XTSYNC @6`.
* **Encoding.** `sr = 144/145`. `RSR.DBREAKA0 a3 = 0x039030`,
  `WSR.DBREAKA0 a3 = 0x139030`, `XSR.DBREAKA0 a3 = 0x619030`.
* **ISS.** `WSR` DEFs `DBREAKA* @6 + XTSYNC @6`; `RSR` DEFs `art @5`.

### 3.4 Data breakpoint — control (`DBREAKC0` `0xa0`, `DBREAKC1` `0xa1`)

* **Mnemonic** `{RSR,WSR,XSR}.{DBREAKC0,DBREAKC1}` — enable + load/store
  selector + byte-mask for the paired `DBREAKA`.
* **Semantics.** `DBREAKCn` controls which access kinds (`LOADBREAK`/`STOREBREAK`)
  and which bytes (4-bit mask) trigger the watchpoint. The match qualifier is a
  **masked address compare**, not exact:

```c
// canonical Xtensa data-watchpoint match (DBREAKCn layout, INFERRED field names,
// OBSERVED load/store split via the DBREAKC_SG0/SG1 schedule shadow state)
bool dbreak_hit(uint32_t addr, bool is_store, uint8_t byte_lanes) {
  for (int n = 0; n < XCHAL_NUM_DBREAK; n++) {
    uint32_t mask = DBREAKCn[n] & DBREAKC_MASK;      // address-mask field
    bool en = is_store ? (DBREAKCn[n] & STOREBREAK)
                       : (DBREAKCn[n] & LOADBREAK);  // direction qualifier
    if (en && ((addr ^ DBREAKAn[n]) & ~mask) == 0
           && (byte_lanes & (DBREAKCn[n] & 0xf)))    // byte-mask overlap
      return true;
  }
  return false;
}
```

* **Encoding.** `sr = 160/161`. `RSR.DBREAKC0 a3 = 0x03a030`,
  `WSR.DBREAKC0 a3 = 0x13a030`, `XSR.DBREAKC0 a3 = 0x61a030`.
* **ISS.** `WSR` DEFs `DBREAKC* @6`, the shadow `DBREAKC_SG* @6`, `XTSYNC @6`;
  `RSR` DEFs `art @5`. The presence of the `DBREAKC_SG0`/`DBREAKC_SG1`
  ("store-group") shadow states in the schedule confirms the load-vs-store +
  byte-mask split is real silicon state, not just an ISA convention.

### 3.5 OCD debug data register (`DDR` `0x68`)

* **Mnemonic** `{RSR,WSR,XSR}.DDR` — the OCD scan-chain Debug Data Register; the
  bidirectional mailbox between the on-chip-debug unit (JTAG/ERI/APB host) and
  core software (`HAVE_OCD_LS32DDR = 1`).
* **Semantics.** The host writes/reads `DDR` to exchange a word with the halted
  core; the `LDDR32.P`/`SDDR32.P` ops (§3.15) stream a memory block through it.
  Because `DDR` couples the core pipeline to the external debug scan domain, **all
  three** directions assert `XTSYNC`.
* **Encoding.** `sr = 104`. `RSR.DDR a3 = 0x036830`, `WSR.DDR a3 = 0x136830`,
  `XSR.DDR a3 = 0x616830`.
* **ISS.** `RSR` DEFs `art @5 + XTSYNC @6`; `WSR` DEFs `XTSYNC @6 + WSRBus @6`.
  Accessible `InOCDMode` even when `PS.RING != 0`.

### 3.6 Timer — cycle count (`CCOUNT` `0xea`)

* **Mnemonic** `{RSR,WSR,XSR}.CCOUNT` — the free-running 32-bit cycle counter.
* **Semantics.** `CCOUNT` increments once per cycle; `RSR.CCOUNT` is the cheap
  profiling wall-clock; `WSR.CCOUNT` reseeds it. `RSR.CCOUNT` is fed from
  `RSRBus` (a live bus sample, not a static SR latch). When `CCOUNT == CCOMPAREn`
  the timer-`n` interrupt fires (§3.7).
* **Encoding.** `sr = 234`. `RSR.CCOUNT a3 = 0x03ea30`, `WSR.CCOUNT a3 = 0x13ea30`,
  `XSR.CCOUNT a3 = 0x61ea30`.
* **ISS.** `RSR` DEFs `art @5`; `WSR` DEFs `WSRBus @6 + XTSYNC @6` (reseeding the
  live counter resyncs the pipe).

### 3.7 Timer — compare (`CCOMPARE0` `0xf0`, `CCOMPARE1` `0xf1`, `CCOMPARE2` `0xf2`)

* **Mnemonic** `{RSR,WSR,XSR}.{CCOMPARE0,CCOMPARE1,CCOMPARE2}` — 3 timer-compare
  registers (`NUM_TIMERS = 3`, ints 28/29/30).
* **Semantics.** Writing `CCOMPAREn` arms timer `n`; the timer interrupt asserts
  *while* `CCOUNT == CCOMPAREn` and is cleared by re-writing `CCOMPAREn`:

```c
// timer-n interrupt assertion (level-style: held while equal, cleared by re-arm)
for (int n = 0; n < XCHAL_NUM_TIMERS /* = 3 */; n++)
  INTERRUPT[XCHAL_TIMER_n_INTERRUPT /* 28+n */] = (CCOUNT == CCOMPAREn[n]);
```

* **Encoding.** `sr = 240/241/242`. `RSR.CCOMPARE0 a3 = 0x03f030`,
  `WSR.CCOMPARE0 a3 = 0x13f030`, `XSR.CCOMPARE0 a3 = 0x61f030`; `CCOMPARE1/2` are
  `sr + 1/+2`.
* **ISS.** `RSR` DEFs `art @5`; `WSR` DEFs `CCOMPARE* @6` (commit at wstage, **no**
  `XTSYNC` — arming a compare does not resync the pipe).

### 3.8 Cache / RAM / MPU config (`CACHEADRDIS` `0x62`, `IRAM0CFG` `0x69`, `DRAM0CFG` `0x6a`, `MPUENB` `0x5a`, `MPUCFG` `0x5c`)

* **Mnemonic** `{RSR,WSR,XSR}.{CACHEADRDIS,IRAM0CFG,DRAM0CFG,MPUENB}`;
  `RSR/WSR.MPUCFG` (no `XSR`).
* **Semantics.**
  * `CACHEADRDIS` — a per-512 MB-octant icache-disable bitmask (which of the 8
    address octants bypass the 16 KB icache).
  * `IRAM0CFG`/`DRAM0CFG` — the single-bank instruction/data local-RAM base +
    enable config.
  * `MPUENB` — the 16-bit foreground-region enable bitmask (1 bit per MPU entry,
    `MPU_ENTRIES = 16`). `WSR`/`XSR.MPUENB` assert `XTSYNC @6` (re-enabling
    regions resyncs the access pipe).
  * `MPUCFG` — RSR+WSR only; `RSR.MPUCFG` returns `MPUNUMENTRIES` (the
    foreground-entry geometry = 16), i.e. the read port surfaces the entry-count
    info rather than a generic latch.
* **Encoding.** `sr = 98/105/106/90/92`. `RSR.CACHEADRDIS a3 = 0x036230`,
  `RSR.IRAM0CFG a3 = 0x036930`, `RSR.DRAM0CFG a3 = 0x036a30`,
  `RSR.MPUENB a3 = 0x035a30`, `RSR.MPUCFG a3 = 0x035c30` (no `XSR`).
* **ISS.** `RSR` DEFs `art @5`; `WSR` DEFs `<SR> @6` (`MPUENB`/`CACHEADRDIS` add
  `XTSYNC @6`; `IRAM0CFG`/`DRAM0CFG` do not). `RSR.MPUCFG` USEs `MPUNUMENTRIES @4`.

> **GOTCHA — `CACHEADRDIS`/`IRAM0CFG`/`DRAM0CFG`/`ATOMCTL` are config SRs.** Their
> SR number, privilege, direction and ISS timing are all OBSERVED, but the
> internal **bit-field layouts** (per-octant disable bits; RAM base/size encodings;
> the per-memory-type 2-bit ordering fields of `ATOMCTL`) are config-specific and
> are **header-OBSERVED only** — they are not bit-decoded from a thunk template
> here, because the encode thunk carries only the opcode word, not the SR payload
> semantics. Treat their field layouts as `[LOW]` pending a dedicated MMU/cache
> config dump.

### 3.9 Atomic-ordering control (`ATOMCTL` `0x63`)

* **Mnemonic** `{RSR,WSR,XSR}.ATOMCTL` — the control register of the NX
  exclusive-access monitor.
* **Semantics.** `ATOMCTL` configures the cacheability/ordering behaviour of the
  `L32EX`/`S32EX` exclusive load/store pair (`HAVE_EXCLUSIVE = 1`,
  `HAVE_S32C1I = 0`). Per memory type, a 2-bit field selects how an exclusive
  access is handled (RCW / internal-bus / raise-exception). The exclusive monitor
  works on a **reservation granule** set by an `L32EX` and cleared/consumed by the
  matching `S32EX`:

```c
// reservation model backed by ATOMCTL (the L32EX/S32EX opcodes are B26;
// ATOMCTL is their per-memory-type policy register)
uint32_t L32EX(uint32_t *addr) {            // load-exclusive
  set_reservation(addr & ~(GRANULE-1));     // arm monitor on the granule
  return *addr;
}
bool S32EX(uint32_t *addr, uint32_t val) {  // store-exclusive
  if (!reservation_valid(addr)) return false;  // lost to an intervening write
  switch (atomctl_field(memtype(addr))) {   // per-memtype 2-bit policy
    case ATOMCTL_RCW:      *addr = val; break;        // read-conditional-write
    case ATOMCTL_INTERNAL: *addr = val; break;        // internal-bus exclusive
    case ATOMCTL_EXCEPTION: raise(LoadStoreError); break;  // not orderable here
  }
  clear_reservation();
  return true;                              // success → status into AR
}
```

* **Encoding.** `sr = 99`. `RSR.ATOMCTL a3 = 0x036330`,
  `WSR.ATOMCTL a3 = 0x136330`, `XSR.ATOMCTL a3 = 0x616330`.
* **ISS.** `RSR` USEs `ATOMCTL @4 + XTSYNC @4` (the read serialises against
  in-flight exclusive accesses), DEFs `art @5`; `WSR` DEFs
  `ATOMCTL @6 + XTSYNC @6 + WSRBus @6` (broadcasting the new ordering policy).

> **QUIRK — `RSR.ATOMCTL` is the only B29 read that asserts a barrier on the
> *read* path.** Every other `RSR.<S>` is a bare `art @5` DEF; `RSR.ATOMCTL`
> additionally USEs `XTSYNC @4`, so a reader of the ordering policy is itself
> serialised against pending exclusives. See
> [Atomics + Ordering](../../uarch/atomics-ordering.md).

### 3.10 External-register access window (`ERACCESS` `0x5f`)

* **Mnemonic** `{RSR,WSR,XSR}.ERACCESS` — the privilege/address window that
  **gates** the B27 `RER`/`WER` external-register bus ops.
* **Semantics.** `RER`/`WER` read `ERACCESS` as state-in and raise
  `ExternalRegisterPrivilegeException` if the access falls outside the
  `ERACCESS`-permitted window. The OS programs `ERACCESS` to open the
  interrupt-distributor / fabric register space for a privileged region.
  `WSR.ERACCESS` DEFs `ERACCESS @5` (early — the gate must commit *before* a
  following `RER`/`WER` samples it).
* **Encoding.** `sr = 95`. `RSR.ERACCESS a3 = 0x035f30`,
  `WSR.ERACCESS a3 = 0x135f30`, `XSR.ERACCESS a3 = 0x615f30`.
* **ISS.** `RSR` USEs `ERACCESS @4`, DEFs `art @5`; `WSR` DEFs `ERACCESS @5`
  (early commit).

### 3.11 Config / trace / processor identity + scratch (`CONFIGID0` `0xb0`, `CONFIGID1` `0xd0`, `MMID` `0x59`, `PRID` `0xeb`, `MISC0` `0xf4`, `MISC1` `0xf5`)

* `CONFIGID0` (RSR+WSR, no XSR) — hi-32 of the build ConfigID
  (`HW_CONFIGID0 = 0xC4019686`). `WSR.CONFIGID0` is a one-time lock-in write.
  `RSR.CONFIGID0 a3 = 0x03b030`, `WSR.CONFIGID0 a3 = 0x13b030`.
* `CONFIGID1` (RSR-only) — lo-32 of the ConfigID
  (`HW_CONFIGID1 = 0x2908E4E3`). `RSR.CONFIGID1 a3 = 0x03d030`.
* `MMID` (WSR-only) — writes a memory/module trace-id marker into the trace
  stream; write-only by construction, so it carries `WSRBus @6 + XTSYNC @4`.
  `WSR.MMID a3 = 0x135930`.
* `PRID` (RSR-only) — processor ID; low 4 bits = core-instance id within a
  multi-core tile (`HAVE_PRID = 1`), upper bits carry ConfigID. `RSRBus`-fed.
  `RSR.PRID a3 = 0x03eb30`.
* `MISC0`/`MISC1` (full R/W/X) — 2 privileged 32-bit scratch registers
  (`NUM_MISC_REGS = 2`) for fast handler temporaries that survive without a memory
  access. `RSR.MISC0 a3 = 0x03f430`, `WSR.MISC0 a3 = 0x13f430`,
  `XSR.MISC0 a3 = 0x61f430`; `MISC1` is `sr + 1`.
* **ISS.** `RSR` DEFs `art @5` (the `CONFIGID0/1` read is a bare `art @5`, no
  SR-bus side effects); `WSR.MMID` DEFs `WSRBus @6 + XTSYNC @4`; `MISC` writes DEF
  `<SR> @6`.

### 3.12 Instruction-cache hint (`IHI`, `IPF`; NON-privileged)

* **Mnemonic** `IHI as, imm` / `IPF as, imm`. Out: `[VAddrBase, VAddrOffset]`;
  In: `[ars:AR, uimm8x4]`. **No** privilege gate, **no** exception (pure hints).
* **Semantics.** Effective address `= AR[s] + (imm8 << 2)`. `IHI` =
  instruction-cache *hit-invalidate* (invalidate the line if present — the
  SW-managed coherence primitive after a code write). `IPF` = instruction-cache
  *prefetch* (pull the line into the 16 KB icache). Both advisory.
* **Encoding.** `LSAI`/`CACHE` group, `op0 = 2`, `t = 0xe`(IHI)/`0xc`(IPF),
  `s = ars`, `uimm8x4 @[15:8]` (×4 byte scale, range 0..1020).
  `IHI a3,0 = 0x0073e2`, `IHI a3,16 = 0x0473e2`, `IPF a6,64 = 0x1076c2`.
* **ISS.** USEs `ars @1`, `imm @0`; DEFs `VAddrBase @1`, `VAddrOffset @0`
  (addr-gen only, no AR result).

### 3.13 Instruction-cache invalidate (`III`; PRIVILEGED)

* **Mnemonic** `III as, imm`. Out: `[VAddrBase, VAddrOffset]`; In:
  `[ars:AR, uimm8x4, MS_DISPST, PSRING, InOCDMode]`. PRIVILEGED — unlike the
  `IHI`/`IPF` hints, `III` is an **unconditional** invalidate (whether or not the
  line is present/dirty), so it is ring-gated.
* **Encoding.** `op0 = 2`, `t = 0xf`, `s = ars`, `uimm8x4`.
  `III a3,16 = 0x0473f2`, `III a5,16 = 0x0475f2`.
* **ISS.** USEs `PSRING`/`MS_DISPST`/`InOCDMode @3`, `ars @1`; DEFs
  `VAddrBase @1`.

### 3.14 Instruction-cache test (tag/data) (`LICT`/`LICW` load, `SICT`/`SICW` store; PRIVILEGED)

* **Mnemonic** `LICT at,as` / `LICW at,as` / `SICT at,as` / `SICW at,as`
  (`HAVE_ICACHE_TEST = 1`).
* **Semantics.** The icache diagnostic ports indexed by `AR[s]`: `L/SICT` =
  load/store the icache **TAG**; `L/SICW` = load/store the icache instruction-**word**
  (data). `LICT`/`LICW` read the tag/word into `AR[t]`; `SICT`/`SICW` write `AR[t]`
  into the tag/data array (used by BIST / cache scrub / debugger). The selector is
  the high nibble: `LICT 0`, `SICT 1`, `LICW 2`, `SICW 3`.
* **Encoding.** `RST1`/`IMP`, `op0 = 0`, `op1 = 1`, `op2 = 15`, `s = ars`,
  `t = art`. `LICT a4,a3 = 0xf10340`, `SICT a4,a3 = 0xf11340`,
  `LICW a4,a3 = 0xf12340`, `SICW a4,a3 = 0xf13340`.
* **ISS.** `LICT`/`LICW` USE `ars @1`, `InstructionMemDataIn @5`, DEF `art @6`
  (deep — cache-array read). `SICT`/`SICW` USE `art @5`, DEF
  `ScalarMemDataOut32 @5`.

### 3.15 OCD fast DDR load/store (`LDDR32.P` load, `SDDR32.P` store; PRIVILEGED/OCD)

* **Mnemonic** `LDDR32.P as` / `SDDR32.P as` (`HAVE_OCD_LS32DDR = 1`).
* **Semantics.** The OCD fast path: `LDDR32.P` loads `mem32[AR[s]]` into the `DDR`
  register and post-increments `AR[s]` by 4; `SDDR32.P` stores `DDR` to
  `mem32[AR[s]]` and post-increments. The debugger streams a memory block through
  `DDR` with one op per word plus the auto address bump (no separate `addi`):

```c
void LDDR32_P(uint32_t **as) { DDR = *(uint32_t*)(*as); *as += 1; } // load + post-inc
void SDDR32_P(uint32_t **as) { *(uint32_t*)(*as) = DDR; *as += 1; } // store + post-inc
```

* **Encoding.** `OCD`/`ST0`, `op0 = 0`, `op1 = 0`, `op2 = 0`, `r = 7`,
  `t = 0xe`(load)/`0xf`(store), `s = ars`. `LDDR32.P a3 = 0x0073e0`,
  `SDDR32.P a5 = 0x0075f0`.
* **ISS.** USE `ars @1`, `ScalarMemDataIn32 @5`/`RSRBus @4`; DEF `ars @4`
  (post-inc), `XTSYNC @6` (LDDR), `MemControl @1`. The `LDDR32.P` DEF set includes
  `SRWrite`/`SRAddr`/`WSRBus` (the load routes through the SR/`DDR` port).

### 3.16 MPU / TLB region ops (`PPTLB` probe, `RPTLB0`/`RPTLB1` read, `WPTLB` write; PRIVILEGED)

* **Mnemonic** `PPTLB at,as` / `RPTLB0 at,as` / `RPTLB1 at,as` / `WPTLB at,as`.
* **CONFIG CAVEAT.** This `ncore2gp` config has **no** paging MMU
  and **no** TLBs (`HAVE_PTP_MMU = 0`, `HAVE_TLBS = 0`, identity mapping,
  vaddr == paddr). The page-table-style `*PTLB` opcodes are present in the TIE DB
  superset and assemble cleanly, but on this silicon they address the **MPU region
  machinery** (16 fg + 2 bg entries at 4 KB), **not** a page table:
  * `RPTLB0`/`RPTLB1` — read MPU region descriptor `[AR[s]]` field 0/1
    (address+attr / rights) via `MMUDataIn` into `AR[t]`; `RPTLB0` also reads
    `MPUENB`.
  * `WPTLB` — write `AR[t]` (descriptor data, `AR[s]` index) into the MPU region,
    updating `MPUENB`.
  * `PPTLB` — probe: given a virtual address in `AR[s]`, return the matching MPU
    region index/attr in `AR[t]`.

```c
// "which region covers this address" probe, under flat identity mapping
uint32_t PPTLB(uint32_t vaddr) {
  for (int i = 0; i < XCHAL_MPU_ENTRIES /* 16 fg */; i++)
    if ((MPUENB >> i) & 1)
      if (vaddr >= region[i].base && vaddr < region[i].base + region[i].size)
        return (i << ATTR_SHIFT) | region[i].attr;   // index ‖ access attributes
  return background_region(vaddr);                    // 2 bg entries fall-through
}
```

* **Encoding.** `MMU`/`RST0`, `op0 = 0`, `op2 = 5`, `r = 0xd`(PPTLB)/`0xb`(RPTLB0)/
  `0xf`(RPTLB1)/`0xe`(WPTLB), `s = ars`, `t = art`. `PPTLB a4,a3 = 0x50d340`,
  `RPTLB0 a4,a3 = 0x50b340`, `RPTLB1 a4,a3 = 0x50f340`, `WPTLB a4,a3 = 0x50e340`.
* **ISS.** `R*TLB`/`PPTLB` USE `ars @1`, `MMUDataIn @4`; DEF `art @5`,
  `VAddrBase @1`. `WPTLB` USEs `art @5`, `MPUENB @6`; DEFs `MMUDataOut @6`,
  `MPUENB @6`, **`XTSYNC @8`** — the single deepest barrier in the whole base
  scalar ISA: re-mapping a region must fully drain the access pipe.

> **NOTE — MPU-vs-page-table is INFERRED.** The opcode mechanics (op-selector,
> `MMUDataIn`/`MMUDataOut` routing, `MPUENB` read/write, `XTSYNC @8`) are
> OBSERVED; the *interpretation* that they address the 16 fg + 2 bg MPU rather
> than a page table is INFERRED from the shipped config (`HAVE_PTP_MMU = 0`,
> `HAVE_TLBS = 0`) — the opcodes are present in the DB superset and assemble, but
> the silicon has no page table for them to walk. See the MPU isolation model in
> [Profiling / Trace / Debug Gating](../../control/security/profiling-trace-debug-gating.md).

### 3.17 OCD debug return (`RFDO`; PRIVILEGED/OCD)

* **Mnemonic** `RFDO imms`. Out:
  `[NextOCDEnabled, BranchTarget, BranchTaken, InvalidOperand]`; In:
  `[imms, EPC, InOCDMode]`.
* **Semantics.** "Return From Debug Operation" — the OCD handler's return:
  restore the interrupted PC from `EPC`, branch back, toggle the `NextOCDEnabled`
  debug-mode latch. `imms` selects the return mode. The debug-exception
  counterpart of B28's `RFE`/`RFI`. Raises `InvalidOperand` if executed outside
  OCD mode.
* **Encoding.** `RST1`/`IMP`, `op0 = 0`, `op1 = 1`, `op2 = 15`, `r = 0xe`,
  `imms = s`. `RFDO 0 = 0xf1e000`.
* **ISS.** USE `InOCDMode @3`, `EPC @3`, `imms @2`; DEF `BranchTarget @3`,
  `BranchTaken @3`, `InOCDMode @6`, `NextOCDEnabled @6`.

### 3.18 Normalize-shift-amount (`NSA` signed, `NSAU` unsigned; NON-privileged)

* **Mnemonic** `NSA at,as` / `NSAU at,as` (`HAVE_NSA = 1`). Out: `[art:AR]`;
  In: `[ars:AR]`. **No** privilege, **no** exception (pure datapath).
* **Semantics** — driven live from `libfiss-base.so`.
  * `NSAU` = count of leading **zeros** of `AR[s]` (0..32; returns 32 for input 0).
    Live: `0x80000000→0`, `0x40000000→1`, `0x00010000→15`, `0x00000003→30`,
    `0x00000001→31`, `0x00000000→32`, `0xffffffff→0` (`module__xdref_nsau_32_32`).
  * `NSA` = signed normalize amount = count of redundant sign bits − adjust; the
    shift that puts the top significant bit at bit 30. Live: `0x00000000→31`,
    `0xffffffff(−1)→31`, `0x40000000→0`, `0x80000000→0`, `0xc0000000→1`,
    `0x00000001→30`, `0x00000002→29`, `0x7fffffff→0`
    (`module__xdref_nsa_32_32`).

```c
uint32_t NSAU(uint32_t x) { return x ? __builtin_clz(x) : 32; }       // clz
uint32_t NSA (int32_t  x) { return x == 0 ? 31 : __builtin_clrsb(x); } // sign-redundant count
```

* **Encoding.** `ST1`/`RST0`, `op0 = 0`, `op2 = 4`, `r = 0xe`(NSA)/`0xf`(NSAU),
  `s = ars`, `t = art`. `NSA a4,a3 = 0x40e340`, `NSAU a4,a3 = 0x40f340`.
* **ISS.** USE `ars @4`; DEF `art @4` (1-cycle E-stage ALU, bypassable).

### 3.19 Sign-extend / clamp-signed (`SEXT`, `CLAMPS`; NON-privileged)

* **Mnemonic** `SEXT ar,as,imm` / `CLAMPS ar,as,imm` (`HAVE_SEXT = 1`,
  `HAVE_CLAMPS = 1`). Out: `[arr:AR]`; In: `[ars:AR, imm]`. **No** privilege,
  **no** exception. `imm ∈ 7..22`.
* **Semantics** [SEXT HIGH/OBSERVED — driven live; CLAMPS HIGH/INFERRED canonical].
  * `SEXT` — `arr` = sign-extend `AR[s]` from bit `imm`: replicate `bit[imm]`
    across `[31:imm+1]`, keep `[imm:0]` (sign-extend an `imm+1`-bit value). Live
    from `module__xdref_sext_32_8` (bit 7): `0x7f→127`, `0x80→−128`, `0xff→−1`,
    `0x123456ff→−1` (upper bits ignored); `module__xdref_sext_32_16` (bit 15):
    `0x8000→−32768`, `0xdead8000→−32768`.
  * `CLAMPS` — `arr` = signed-saturate `AR[s]` to `[−2^imm, 2^imm − 1]` (clamp to a
    signed `imm+1`-bit range) — the saturating pack primitive (scalar mirror of the
    IVP `CLAMP` vector op).

```c
int32_t SEXT  (int32_t x, int imm) { int sh = 31 - imm; return (x << sh) >> sh; }
int32_t CLAMPS(int32_t x, int imm) {
  int32_t hi = (1 << imm) - 1, lo = -(1 << imm);
  return x > hi ? hi : (x < lo ? lo : x);
}
```

* **Encoding.** `RST3`/`QRST`, `op0 = 0`, `op1 = 3`, `op2 = 2`(SEXT)/`3`(CLAMPS),
  `r = dst`, `s = src`, `t = imm − 7` (so `imm = t + 7`, range 7..22).
  `SEXT a4,a3,7 = 0x234300`, `SEXT a4,a3,22 = 0x2343f0`,
  `CLAMPS a4,a3,7 = 0x334300`, `CLAMPS a4,a3,22 = 0x3343f0`.
* **ISS.** USE `ars @4`, `tp7 @3`; DEF `arr @4` (1-cycle E-stage ALU).

> **QUIRK — the `imm − 7` immediate bias.** The `t` nibble does not encode the
> bit position directly; it encodes `imm − 7`, so `t = 0` → `imm = 7` and
> `t = 0xf` → `imm = 22`. The device assembler rejects `imm = 6` and `imm = 23`
> with *"operand 3 has invalid value"*, pinning the legal range to 7..22 for both
> `SEXT` and `CLAMPS`.

---

## 4. ISS / `INSTR_SCHEDULE` timing

Pipeline frame (root `rstage = 0`, `estage = 3`, `mstage = 4`,
`wstage = 6`). The per-op `issue`/`stall` functions are read from the
DWARF-bearing `libcas-core.so` (`x24_Inst_0_inst_<MN>_{issue,stall}`; e.g.
`x24_Inst_0_inst_RSR_DDR_issue`, `..._RSR_ATOMCTL_stall`, `..._LICT_issue`,
`..._RFDO_issue`, `..._RPTLB0_stall`).

| op-class | inputs USE @stage | result DEF @stage | note |
|----------|-------------------|-------------------|------|
| `RSR.<any B29 SR>` | `<SR>`/`RSRBus @4`, priv `@3` | `art @5` | SR read |
| `WSR.{dbreak*,ibreakc*,ddr,ccount,atomctl,mpuenb,cacheadrdis}` | `art @4`, priv `@3` | `<SR> @6` (+`XTSYNC @6`) | priv write, resync |
| `WSR.{dram0cfg,iram0cfg,ccompare,misc,ibreaka,configid0}` | `art @4`, priv `@3` | `<SR> @6` (no `XTSYNC`) | plain config commit |
| `WSR.ERACCESS` | `art @4`, priv `@3` | `ERACCESS @5` (early) | gate commits before `RER`/`WER` |
| `WSR.MMID` | `art @5`, priv `@3` | `WSRBus @6` + `XTSYNC @4` | trace emit |
| `RSR.{CONFIGID0,CONFIGID1}` | priv `@3` | `art @5` (bare) | no SR-bus side fx |
| `RSR.MPUCFG` | priv `@3`, `MPUNUMENTRIES @4` | `art @5` | geometry read |
| `RSR.ATOMCTL` | `ATOMCTL @4` + `XTSYNC @4`, priv `@3` | `art @5` | read serialises |
| `XSR.<full-RWX SR>` | `art @4`, `<SR> @4`, priv `@3` | `art @5`, `<SR> @6` | atomic swap |
| `IHI`/`IPF` (hint) | `ars @1`, `imm @0` | `VAddrBase @1` | addr-gen, no AR result |
| `III` (priv inval) | `ars @1`, priv `@3` | `VAddrBase @1` | priv inval |
| `LICT`/`LICW` (tag/data ld) | `ars @1`, `InstMemDataIn @5` | `art @6` | deep cache rd |
| `SICT`/`SICW` (tag/data st) | `ars @1`, `art @5` | `ScalarMemDataOut32 @5` | cache wr |
| `LDDR32.P`/`SDDR32.P` | `ars @1`, `MemDataIn @5`/`RSR @4` | `ars @4` (post-inc), `XTSYNC @6` | OCD fast LS, auto bump |
| `PPTLB`/`RPTLB0`/`RPTLB1` | `ars @1`, `MMUDataIn @4` | `art @5`, `VAddrBase @1` | MPU read/probe |
| `WPTLB` | `art @5`, `MPUENB @6` | `MMUDataOut @6`, `MPUENB @6`, **`XTSYNC @8`** | MPU write, deepest resync |
| `RFDO` | `EPC @3`, `imms @2`, `InOCD @3` | `BranchTarget @3`, `InOCDMode @6` | debug return |
| `NSA`/`NSAU` | `ars @4` | `art @4` | 1-cycle ALU |
| `SEXT`/`CLAMPS` | `ars @4`, `tp7 @3` | `arr @4` | 1-cycle ALU |

Privileged-control writes commit at wstage (6) and assert `XTSYNC` when they
reconfigure pipeline-visible state (debug match, atomic ordering, timer count, MPU
enable, DDR scan). `WPTLB`'s `XTSYNC @8` is the single deepest barrier in the base
scalar ISA. The four pure-datapath ops (`NSA`/`NSAU`/`SEXT`/`CLAMPS`) are plain
1-cycle E-stage ALU (DEF `@4`), identical to the B25 arith class.

---

## 5. Worked bit-patterns (oracle-validated)

All assembled by `xtensa-elf-as` (`XTENSA_CORE=ncore2gp`) and disassembled back —
100% mnemonic + operand agreement.

```
# --- SR direction / family representatives ---
03eb30   rsr.prid       a3   # sr=0xeb, read-only (wsr/xsr.prid REJECTED)
135930   wsr.mmid       a3   # sr=0x59, write-only (rsr/xsr.mmid REJECTED)
616330   xsr.atomctl    a3   # sr=0x63, op1=1 op2=6 atomic swap, +XTSYNC@6
13f030   wsr.ccompare0  a3   # sr=0xf0, arm timer-0 (int 28)
03ea30   rsr.ccount     a3   # sr=0xea, live cycle-counter sample
036830   rsr.ddr        a3   # sr=0x68, OCD mailbox, +XTSYNC@6
035c30   rsr.mpucfg     a3   # sr=0x5c, RSR+WSR only (xsr.mpucfg REJECTED)
03d030   rsr.configid1  a3   # sr=0xd0, read-only lo-32 ConfigID
# --- non-SR families ---
0473e2   ihi  a3, 16         # op0=2 t=0xe s=3 uimm8x4=4 (16=4<<2), NON-priv hint
0473f2   iii  a3, 16         # op0=2 t=0xf, PRIVILEGED unconditional invalidate
f10340   lict a4, a3         # op0=0 op1=1 op2=15 r=0, icache TAG load
f12340   licw a4, a3         # r=2, icache WORD load
0073e0   lddr32.p a3         # op0=0 op2=0 r=7 t=0xe, DDR=mem32[a3]; a3+=4
0075f0   sddr32.p a5         # t=0xf store, s=5
50e340   wptlb a4, a3        # op0=0 op2=5 r=0xe, MPU region write, XTSYNC@8
50d340   pptlb a4, a3        # r=0xd probe
f1e000   rfdo 0             # op0=0 op1=1 op2=15 r=0xe, PC=EPC; toggle OCD
234300   sext   a4, a3, 7    # op0=0 op1=3 op2=2 t=0 (imm=t+7) -> sign-extend@bit7
2343f0   sext   a4, a3, 22   # t=0xf -> imm=22
334300   clamps a4, a3, 7    # op2=3 -> signed-clamp to [-128,127]
40f340   nsau a4, a3         # op0=0 op2=4 r=0xf -> clz
40e340   nsa  a4, a3         # r=0xe -> signed normalize
```

`SR# = (W >> 8) & 0xff` recovers `0xeb/0x59/0x63/0xf0/0xea/0x68/0x5c/0xd0` for the
SR words above — each matches `core-isa.h`.

---

## 6. Verification ledger

* [HIGH/OBSERVED] **Roster count.** `nm libisa-core.so | rg -c` returns **70**
  distinct SR-access `Opcode_<mn>_Slot_inst_encode` thunks and **18** non-SR
  thunks (`clamps ihi iii ipf lddr32.p lict licw nsa nsau pptlb rfdo rptlb0
  rptlb1 sddr32.p sext sict sicw wptlb`) = **88**. Reconciled against the planning
  figure: B29's portion of the 469 scalar mnemonics expands to 88 once the per-SR
  R/W/X cross-product (×3 for full SRs, ×2/×1 for the asymmetric ones) is unrolled.
* [HIGH/OBSERVED] **Oracle round-trip.** All 88 candidate words assemble + dis-
  assemble back to the exact mnemonic + operands (70 SR ops + 22 non-SR
  bit-patterns incl. range/spelling probes). §5 words are copy-exact from the
  disassembly.
* [HIGH/OBSERVED] **Direction asymmetry — proven two ways.** The eight absent
  forms (`{wsr,xsr}.prid`, `{wsr,xsr}.configid1`, `{rsr,xsr}.mmid`,
  `xsr.configid0`, `xsr.mpucfg`) have **no** encode thunk in `libisa-core.so`
  (`nm | rg -c` = 0) **and** the device assembler rejects each spelling with
  *"unknown opcode or format name"*. This pins the 70-op count: `21×3 + 2×2 +
  2×1 + 1×1 = 70`.
* [HIGH/OBSERVED] **SR numbers.** All 26 B29 SR numbers extracted as
  `(W >> 8) & 0xff` match `core-isa.h` (`MMID 0x59`, `MPUENB 0x5a`, `MPUCFG 0x5c`,
  `ERACCESS 0x5f`, `CACHEADRDIS 0x62`, `ATOMCTL 0x63`, `DDR 0x68`, `IRAM0CFG 0x69`,
  `DRAM0CFG 0x6a`, `IBREAKA0/1 0x80/0x81`, `DBREAKA0/1 0x90/0x91`,
  `DBREAKC0/1 0xa0/0xa1`, `CONFIGID0 0xb0`, `IBREAKC0/1 0xc0/0xc1`,
  `CONFIGID1 0xd0`, `CCOUNT 0xea`, `PRID 0xeb`, `CCOMPARE0/1/2 0xf0/0xf1/0xf2`,
  `MISC0/1 0xf4/0xf5`).
* [HIGH/OBSERVED] **Config grounding.** `core-isa.h`: `ICACHE_SIZE 16384`,
  `ICACHE_LINESIZE 64`, `ICACHE_WAYS 4`, `DCACHE_SIZE 0`, `HAVE_ICACHE_TEST 1`,
  `DCACHE_TEST 0`, `MPU_ENTRIES 16`, `MPU_BACKGROUND_ENTRIES 2`, `MPU_ALIGN 4096`,
  `HAVE_PTP_MMU 0`, `HAVE_TLBS 0`, `NUM_TIMERS 3` (ints 28/29/30), `HAVE_OCD 1`,
  `NUM_IBREAK 2`, `NUM_DBREAK 2`, `HAVE_OCD_LS32DDR 1`, `HAVE_EXCLUSIVE 1`,
  `HAVE_S32C1I 0`, `NUM_MISC_REGS 2`, `HAVE_PRID 1`, `HAVE_NSA 1`, `HAVE_SEXT 1`,
  `HAVE_CLAMPS 1`, `HW_CONFIGID0 0xC4019686`, `HW_CONFIGID1 0x2908E4E3`.
* [HIGH/OBSERVED] **Live value semantics.** `NSAU`, `NSA`, and `SEXT` driven
  through their `module__xdref_*` leaves in `libfiss-base.so` via `ctypes` and
  match canonical Xtensa exactly (§3.18–3.19). `NSAU(0) = 32`, `NSAU(1) = 31`,
  `NSA(0) = NSA(−1) = 31`, `SEXT` from bit 7: `0xff → −1`, `0x80 → −128`.
* [HIGH/OBSERVED] **`SEXT`/`CLAMPS` immediate range.** The assembler accepts
  `imm ∈ {7,22}` and rejects `{6,23}` for both — the `t = imm − 7` bias pins the
  4-bit field to 7..22.
* [HIGH/OBSERVED] **ISS source.** Per-op `issue`/`stall` functions exist in the
  DWARF-bearing `libcas-core.so` for the full B29 set (e.g.
  `x24_Inst_0_inst_RSR_DDR_issue`, `..._RSR_ATOMCTL_stall`, `..._LICT_issue`,
  `..._RFDO_issue`, `..._RPTLB0_stall`).
* [HIGH/OBSERVED] **CORRECTION — breakpoint `XTSYNC` scope is asymmetric.** Among
  the four breakpoint banks, `WSR`/`XSR` of `DBREAKA`, `DBREAKC` and `IBREAKC`
  assert `XTSYNC @6` (resync the fetch/access pipe), but `WSR.IBREAKA` does **not**
  — arming an instruction-breakpoint *address* alone is a plain config commit; only
  the *enable* in `IBREAKC` resyncs (§3.1–3.4). A blanket "every `WSR.*BREAK*` →
  `XTSYNC`" reading is too broad; `IBREAKA` is the exception. The §4 table reflects
  this (`dbreak*`/`ibreakc*` in the `XTSYNC` row, `ibreaka` in the plain-commit
  row).
* [MED/INFERRED] **`*PTLB` = MPU not page-table** (§3.16): mechanics OBSERVED,
  the MPU-region interpretation INFERRED from `HAVE_PTP_MMU = 0`/`HAVE_TLBS = 0`.
* [MED/INFERRED] **`DBREAKC` match qualifiers** (§3.4): the
  `DBREAKC_SG0`/`DBREAKC_SG1` shadow states OBSERVED in the schedule confirm the
  load/store + byte-mask split; the exact `LOADBREAK`/`STOREBREAK`/4-bit-mask field
  layout is the canonical Xtensa `DBREAKC` layout (INFERRED).
* [LOW] **Config-SR field layouts** (§3.8 GOTCHA): the internal bit-fields of
  `CACHEADRDIS`/`IRAM0CFG`/`DRAM0CFG`/`ATOMCTL` are header-OBSERVED at the
  register level only; the per-octant / per-memtype bit decode is not resolved
  here.
* [HIGH/OBSERVED] **Disjointness.** Zero B29 opcode overlaps B27 (`RER`/`WER` stay
  in B27 — B29 owns only the `ERACCESS` gate SR) or B28 (the exception-dispatch
  SRs stay in B28). With B29, the base-Xtensa scalar ISA (B25–B29) is complete and
  disjoint.
