# Microarchitecture Synthesis

> **Scope.** This is the consolidated, publishable **microarchitecture chapter** for the GPSIMD
> compute core — the Cadence Tensilica **Vision-Q7 NX "Cairo"** DSP, config `ncore2gp`
> (`uarchName="Cairo"`, `TargetHWVersion="NX1.1.4"`). It composes the twelve Part-4 microarch
> pages into **one cycle-approximate model** a reimplementer rebuilds against: the core-config +
> register-file inventory; the 14-format / 46-slot FLIX issue model + co-issue rules + the
> pipeline-stage timing substrate; the datapath microarchitecture (the VFPU FMA/FCR/FSR tree, the
> integer / quad-MAC pipes, the two activation/transcendental engines, the load/store + `valign`
> mechanism, the SuperGather and permute crossbars); the memory hierarchy as the core sees it; and
> the per-class latency / throughput reference table.
>
> **This is a SYNTHESIS — it introduces NO new measurement.** Every number is *carried* from a
> committed sibling page (cited inline). Where two sibling pages legitimately differ in framing,
> this chapter is where they are **unified**: it states ONE model and cites both source framings,
> with the divergence resolved by the binary as arbiter. The companion config-grounded number sheet
> is [Config-Grounded Microarch Reference Sheet](../isa/core/config-reference-sheet.md); this page is
> its narrative cycle-approximate companion.

Tags per claim: `[CONF × PROV]` — confidence `HIGH/MED/LOW` × provenance `OBSERVED` (read from the
binary/config), `INFERRED` (derived from observed structure), `CARRIED` (re-used at a cited sibling
page's confidence, re-grounded here where a divergence was spot-checked against the binary). The
shipped artifacts are the `ncore2gp` config headers (`core-isa.h` / `system.h` / `specreg.h`), the
unstripped config DLLs (`libisa-core.so` = the ISA/encoding DB; `libcas-core.so` = the cycle/ISS
schedule model, full `.symtab`, **no DWARF**; `libfiss-base.so` = the host value oracle), and the
device assembler/disassembler (`xtensa-elf-{as,objdump}`, `XTENSA_CORE=ncore2gp`). The RTL is **not**
in the corpus; all block topology is INFERRED from OBSERVED ports/stages/widths/values. All prose
reads as derived from binary/static analysis alone (lawful interoperability RE, DMCA 17 U.S.C.
1201(f)).

> **GOTCHA — section deltas, vtable base, and gitignore (carried into every spot-check on this
> page).** For these `ncore2gp` DLLs `.text`/`.rodata` are `VMA == file offset`; `.data` /
> `.data.rel.ro` carry `VMA − fileoffset = 0x200000` (NOT libtpu's `0x400000`), confirmed
> per-section: `libcas-core.so` `.data.rel.ro` VMA `0x2070900` / file `0x1e70900`;
> `.data` VMA `0x2280ed8` / file `0x2080ed8` (`readelf -SW`). `extracted/` is gitignored — reach it
> with an absolute path or `fd --no-ignore`. Ground every count with `nm <lib> | rg -c`, never a
> decompile (which inflates 2–12×) and never an opcode-*name* grep (co-issue legality is a
> slot/unit property, not a name property).

---

## 0. Headline — the Vision-Q7 in one paragraph

`[HIGH × CARRIED]` The GPSIMD compute core is a **Cadence Tensilica Vision-Q7 NX DSP** (CoreID
`ncore2gp`, HW `NX1.1.4`, 64-bit ConfigID `{0xC4019686, 0x2908E4E3}`, XEA3/NX, windowed ARs,
identity-mapped single ring, **no D-cache**). It is a **VLIW / FLIX** machine: a 7-stage in-order
scalar/address pipe (`A1 / B3 / E4 / M5 / W6`, plus a deep retire `D9`) shares fetch/issue with a
**separate deep 512-bit SIMD vector pipe** (architectural read port @stage 10, results @11/12/13).
The vector ISA is delivered as **14 FLIX formats / 46 slots**; a wide bundle issues up to **5** ops
(formats F3/F11) over the class-partitioned lanes `mem(S0,S1) + intMAC(S2_Mul) + FP-FMA(S3_ALU,
co-S2) + ALU(S3/S4, +S1 on F11)`. The compute heart is a **32-physical-lane (512-bit) datapath**
statically partitioned `64×8b / 32×16b / 16×32b`, hosting five functional clusters: a 1-cycle
vector ALU, a 2-cycle 4-tap quad-MAC widening into the **1536-bit `wvec` accumulator** (a same-stage
`(12,12)` read-modify-write that sustains an `II=1` MAC chain), a 3-cycle SP/HP **2×FMA VFPU**, a
two-phase **SuperGather** address-gen engine, and a 2-cycle **permute** crossbar; plus a 2-cycle
in-core **transcendental SEED LUT**. The data side is local RAM + an 8-entry write buffer with **no
D-cache and no PSUM port**: the core dereferences a flat 32-bit NX space (IRAM @`0x0`, 4-bank DataRAM
@`0x80000`, a 1-GiB system-RAM AXI window @`0x100000`) and reaches SBUF as a memory-mapped
AXI/ACE-Lite master through `axi2sram` (the 1/8 TDM DMA slot). **Eight** such cores form one SPMD
POOL cluster, sharing one IRAM image, self-identifying by PRID, each on private memory. The config
and the entire core microarchitecture are **invariant across all five NeuronCore generations**
(v2..v5 = SUNDA/CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK); only SoC-fabric scaling axes differ. This
chapter is the cycle-approximate model of that core.

> **NOTE — the second management core is NOT this core.** The NCFW (collective-firmware) management
> processor is a **separate scalar Xtensa-LX** core with **no Vision/SIMD, no FLIX, no TIE** — see
> [NCFW Scalar-LX Core](ncfw-lx-core.md). Any "~26–28 % FLIX" figure obtained by pointing the
> `ncore2gp` disassembler at NCFW firmware is a **mis-decode artifact** (the Q7's `op0=e/f` →
> Vision-bundle length rule eats LX operand bytes); it does **not** describe a real VLIW layer and
> does **not** change the Q7 microarch model below. `[HIGH × OBSERVED]`

---

## 1. Core-config summary + register-file inventory

### 1.1 Identity / version pinning `[HIGH × OBSERVED — rev-markers.md, config-reference-sheet.md]`

| field | value | source |
|---|---|---|
| `XCHAL_CORE_ID` | `"ncore2gp"` | `core-isa.h:249` |
| `ConfigName` / `uarchName` / `arch` | `Xm_ncore2gp` / `Cairo` / `Xtensa24` (XEA3) | identity tokens |
| `XCHAL_HW_VERSION_NAME` | `"NX1.1.4"`; integer **281040** (maj 2810 / min 4 / mic 0) | `core-isa.h:260,270–272` |
| `XCHAL_HW_MIN_VERSION == MAX_VERSION` | **281040** (zero-width accept window) | `core-isa.h:273,277` |
| 64-bit ConfigID `{ID0, ID1}` | `{0xC4019686, 0x2908E4E3}` (RELIABLE; from silicon PRID/CONFIGID) | params / xparm |
| `BuildUniqueID` / Customer | `0xC23FE` (795646) / `19270`; BuildMode `Evaluation` | rev-markers |
| `coprocessorCount` / `vectorPipe` | `7` / `1` | core.xparm |

> **CORRECTION (version axes) — `RI-2020.4` and `RI-2022.9` name two DIFFERENT numbers on two
> axes; both are correct.** The **hardware** lineage is `NX1.1.4 = LX7.1.4 = RI-2020.4 = 281040`
> (`xtensa-versions.h:317`, `XTENSA_HWVERSION_RI_2020_4 281040`); the **toolchain** is
> `RI-2022.9 = "Xtensa Tools 14.09" = 1409000` (`SWToolsRelease=RI-2022.9`,
> `SWToolsVersion=1409000`). The single most consequential pin for a reimplementer is the **HW
> point**: `HW_MIN == HW_MAX == 281040` — there is ONE target HW version, not a family window
> ([rev-markers §2.1](rev-markers.md)). `[HIGH × OBSERVED]`

The PRID layout is `PRID_ID_SHIFT 0 / BITS 4 / MASK 0x0000000F` (`core-isa.h:332–334`): the low 4
bits are the core instance id within the 8-core tile (the SPMD selector, §1.6); the upper PRID bits
carry the CONFIGID. The core is **plain compute**: `XCHAL_HAVE_MP_RUNSTALL 0`, `MP_INTERRUPTS 0`,
`MPCoherencySupport 0`, `WWDT[Configured]=0`, all power-saving (`PSO`/`PSO_CDM`/`FULL_RETENTION`) 0,
synthesized for TSMC 28 HPC+ 9-track at a single corner/voltage (no DVFS) — [clock-reset-power.md].
`[HIGH × OBSERVED]`

### 1.2 ISA option group `[HIGH × OBSERVED]`

* **Core family** — `HAVE_NX 1` (XEA3), `LX 0`, `RNX 0`, `MX 0`, `BE 0` (little-endian).
* **Base ISA** — `WINDOWED 1` (64 AREGs = 8 windows × 8), `DENSITY 1` (16-bit insns), `LOOPS 1` +
  `LOOP_BUFFER_SIZE 128`, `NSA/MINMAX/SEXT/DEPBITS/CLAMPS 1`, `MUL32 1` + `MUL32_HIGH 1` + `DIV32 1`,
  `MAC16 0` (the MAC capability is in the Vision SIMD, **not** a MAC16 package), `L32R 1` + `CONST16
  1`, `EXCLUSIVE 1` (L32EX/S32EX), `S32C1I 0` (no legacy LX CAS), `RELEASE_SYNC 1` (L32AI/S32RI),
  `THREADPTR 1`, `BOOLEANS 1`, `PRID 1`, `EXTERN_REGS 1`, `CP 1` + `CP_MAXCFG 7` (one populated
  coproc, CP1 = `Vision`).
* **FP scalar** — `HAVE_FP 1` (single-precision scalar FPU) + `FP_DIV/RECIP/SQRT/RSQRT 1`;
  `USER_DPFPU 0`.
* **Vision (the compute heart)** — `HAVE_VISION 1`, `VISION_TYPE 7` (Q7), `VISION_SIMD16 32` (32
  lanes of 16-bit = 512-bit datapath), `QUAD_MAC_TYPE 1`, `HAVE_VISION_HISTOGRAM 1`, `SP_VFPU 1` +
  `SP_VFPU_2XFMAC 1`, `HP_VFPU 1` + `HP_VFPU_2XFMAC 1`, `DP_VFPU 0`, `SUPERGATHER 1`.
* **Explicitly absent (all 0)** — HiFi1-5, Fusion*, ConnX BBE/BSP/SSP/B10/B20, Vectra, PDX, GRIVPEP,
  VISIONC, XNNE. (`libhal` ships `xthal_xnne_*` NO-OP stubs; **not** evidence of an XNNE block.)

The ISA is **stock Cadence**: `num_opcodes = 1534` (`0x5fe @ 0x3b61d0`), `num_iclasses = 1447`
(`0x5a7`), `num_operands = 232` (`0xe8`), `num_states = 81` (`0x51`), with **zero** user-defined
opcodes — the GPSIMD-custom layer is firmware + host routing, not new silicon opcodes.

> **NOTE — `num_states`: 81 (libisa runtime DB) vs 87 (binutils module).** The libisa runtime getter
> reads `num_states = 0x51 = 81` (`@0x3b6670`); the shipped binutils/gdb
> `xtensa-modules.c` carries `#define n 87`. These are **different denominators** (the runtime ISA
> DB count vs the disassembler module's state array including non-DB entries). The FCR/FSR field set
> §3.2 reasons over the per-state structs regardless of either total; cite **81** for the libisa DB.
> `[HIGH × OBSERVED]`

### 1.3 The eight register files — the regfile inventory

`num_regfiles = 8` (`mov $0x8 @ 0x3b5c20`); `regfiles[] @ VMA 0x74a800` (`.data.rel.ro`, file
`0x54a800`, **stride 56**). Read byte-exact ([register-files.md], [regfile-ports.md]):

| idx | file | short | geom | ctype | total | role | hot-path port |
|---:|---|---|---|---|---|---|---|
| 0 | `AR` | `a` | 32 b × 64 | `_TIE_uint32` | 2 KiB | windowed scalar address/general (8×8) | READ @1/3/4/5; WRITE @1/3/4/5/6/12 |
| 1 | `BR` | `b` | 1 b × 16 | `_TIE_xtbool` | 16 b | scalar boolean predicates | READ @3/4; WRITE @4 (scalar E-stage) |
| 2 | **`vec`** | `v` | **512 b × 32** | `…xb_vec2Nx8` | 16 KiB | SIMD vector data — **the hot file** | **READ @10** (unified); WRITE @10/11/12/13 staggered by class |
| 3 | `vbool` | `vb` | 64 b × 16 | `…vbool2N` | 1 KiB | per-lane SIMD predicate masks | READ @10; WRITE @10/11 |
| 4 | `valign` | `u` | 512 b × 4 | `…valign` | 2 KiB | unaligned-L/S rotate-merge + iterative-divide scratch | hazard @9 (load) / @10 (store); file @10 read / @12 write |
| 5 | **`wvec`** | `wv` | **1536 b × 4** | `…xb_wvecspill` | 6 KiB | wide MAC accumulators (3 × `vec`) | **@12 RMW** `(12,12)`; file-bound to 4 chains |
| 6 | `b32_pr` | `pr` | 64 b × 16 | `…xb_int64pr` | 1 KiB | quad-MAC RADIX (byte-split = taps) + packed pred | @10 (MAC radix); WRITE @10/11 |
| 7 | `gvr` | `gr` | 512 b × 8 | `…xb_gsr` (flags **0x0d**) | 4 KiB | DUAL: SuperGather "gsr" staging **and** VFPU FCR/FSR CSR | gather @10/11 (`gt`/`gs`) + `RUR`/`WUR` |

Operand-field widths are `ceil(log2 count)`: `vec` = 5-bit, `gvr` = 3-bit, `wvec` = 2-bit, FLIX `AR`
subset = 4-bit. All eight files are **caller-saved** (`num_callee_saved = 0`). `gvr` alone carries
flag bit `0x08` (flags `0x0d` vs `0x05`): the unique structural marker of a file written by one
instruction (gather A-phase) and read by a different one (D-phase) — `[HIGH × OBSERVED]` byte,
`[MED × INFERRED]` meaning.

> **NOTE — 8 files, 4 BR views, never 12 silicon files.** The libisa introspection ABI
> (`xtensa_isa_num_regfiles`) reports **12** = `8 regfiles + 4 BR sub-views` (`BR2/BR4/BR8/BR16`,
> all narrowings of the single `BR` file). The canonical microarch count is **8 files + 4 views** —
> do not double-count the views as silicon ([register-files.md], config-reference-sheet §3).
> The TIE `tie.h` `SA_LIST` totals `32 v + 16 vb + 4 u + 16 pr + 4 wv = 72` CP1 registers.
> `[HIGH × OBSERVED]`

### 1.4 Exception / dispatch arch = XEA3 `[HIGH × OBSERVED — boot-reset.md, config-reference-sheet.md]`

`XEA_VERSION 3` (`core-isa.h:719`, `HAVE_XEA3 1`), single-frame dispatch (no `_n` EPC/EPS/EXCSAVE
banks). Boot-relevant SRs (`specreg.h`): `PREFCTL 40`, `WB`(WindowBase) `72`, `MPUENB 90`, `MEMCTL
97`, `MS 229`, `PS 230`, `VECBASE 231`, `PRID 235`, `ISB 236`; `IMPRECISE_EXCEPTIONS 1`. **37
interrupts**, **7 levels**, **3 CCOMPARE timers** (interrupts 28/29/30), `IDMA_DONE/ERR` = 35/36; a
single relocatable DispatchVector (`numOfVectors=0`). `[HIGH × OBSERVED]`

**Boot spine (byte-exact, [boot-reset.md])**: reset PC `0x0` → `j _ResetHandler` → `wsr.isb 0x8FED0`
(the int-stack top) + `wsr.vecbase _DispatchVector` (*the single act that arms the XEA3 machine*) →
`iii` 256-line (16 KiB) I-cache invalidate + `isync` → `wsr.memctl 0xFFFFFF08` (I-cache still off) +
`wsr.prefctl 0x3044` (8-entry prefetcher) → `wsr.wb 0x40000000` (= `1<<30`, **not** 1) → 16-entry
MPU `wptlb` loop (`mpuenb 0` first, walks `__xt_mpu_init_table`) → `MEMCTL |= 0x09` (I-cache enable)
→ `wsr.ms 0` → `callx0 _start` (crt1: SP `__stack`, `wsr.isl` stack limit, `wsr.ps 0x80`, `.bss`
clear, PRID-gated reent init, `__clibrary_init`) → `callx8 main` = the dispatch loop. The host then
claims the booted core via a DRAM[0] one-word mailbox: READY sentinel `0x6099CB34` (a linked-image
initializer @ firmware VMA `0x02000408`) → host writes CLAIM `0x502B2DA1` (handler
`nrtucode_core_on_ucode_booted @0x308F90`). `[HIGH × OBSERVED]`

### 1.5 MMU/MPU + cache / local-mem census `[HIGH × OBSERVED]`

No PTP-MMU, no TLBs, `IDENTITY_MAP 1` (`vaddr == paddr`), `MMU_RINGS 1`. MPU present: **16
foreground + 2 background** entries, 4 KiB granularity. I-cache **16 KiB / 64-B line / 4-way / 64
sets/way / 32-B access**, prefetch 8 entries; **no D-cache** (`DCACHE_SIZE 0`). **8-entry write
buffer**. `DATA_WIDTH 64 B` (512-bit data bus), `INST_FETCH_WIDTH 32 B` (256-bit instbuf). AXI/
ACE-Lite master with ECC. BTB 128-entry / 4-way / 22-tag-bit + 8-entry RAS. Full memory model §4.

### 1.6 The 8-core SPMD POOL cluster `[HIGH × OBSERVED — boot-reset.md, atomics-ordering.md]`

Eight `ncore2gp` Vision-Q7 cores form **one POOL cluster**. They **share one IRAM image**, boot the
same PRID-gated `_SharedResetVector` (`rsr.prid; extui a0,a0,0,4; addx4` into a per-core
`_ResetTable_base[core_id]` handler table), run **SPMD** (one kernel image, self-identify by PRID),
and own **private memory**: private DataRAM, a private `hbm_scratch` sub-window (`0x84000000 +
prid*0x200000`), a per-core SoC DRAM rebase (`idx = 9 + 2*cpu_id`, [lsu-memory §1.3]), no-lock
per-core heaps. There is **no shared mutable structure** between cores ⇒ the software is
**data-race-free by construction**, not by synchronization ([atomics-ordering.md]; zero shipped
exclusives/spinlocks). At SoC reset all 8 cores are run-stalled (Q7 run-stall CSR `@0x3000`,
bits `[7:0]`, reset `0xFF`); on CAYMAN/MARIANA one host CSR write `0xFF → 0x00` releases all 8;
SUNDA brings the cluster online via the SEQ/SP `EVT_SEM` fabric instead.

---

## 2. The FLIX issue model — 14 formats / 46 slots + co-issue + timing

`XCHAL_HAVE_FLIX3 = 0` is a **red herring** — FLIX3 names only the generic 3-way option; the
Vision-Q7 FLIX is supplied by the Vision TIE package as `<FORMAT>`/`<SLOTDEF>` ([flix-encoding.md]).
`num_formats = 14` (`0xe @ 0x3b65e0`), `num_slots = 46` (`0x2e @ 0x3b6510`), validated by two
independent decoders in `libisa-core.so` and a device-bundle cross-check.

### 2.1 The 14 formats / 46 slots — the byte-level decode `[HIGH × OBSERVED — flix-encoding.md, co-issue-matrix.md]`

Two table-driven decoders run on each word: `format_decoder @0x3b5970` (a flat mask-and-match ladder
keyed on `op0 = byte0[3:0]` + the `byte3` selector → format `0..13` or `−1`) and `length_decoder
@0x3b5a50` (`idx = ((byte3 & 0xF) << 4) | (byte0 & 0xF)` → `length_table[256] @0x3d4100` → byte length
`{2, 3, 8, 16}` or illegal). They are **mutually exact** (0 mismatches / 4096 combos). The 7
length classes (`l24 / l16a / l16b / l128a / l128b / l128c / l64a`) map onto the four byte sizes;
`l128a` = F3/F11 (16 B, `op0=0xE`), `l128b` = F0/F4, `l128c` = F1/F2/F6/F7, `l64a` = N0/N1/N2.

| idx | fmt | len | #slots | real width | issue profile (S0…Sn) | `op0` / `b3lo` |
|---:|---|:--:|:--:|:--:|---|---|
| 0 | `x24` | 3 | 1 | 1 | one 24-bit core insn | 0–7 |
| 1 | `x16a` | 2 | 1 | 1 | one density insn | 8–B |
| 2 | `x16b` | 2 | 1 | 1 | one density insn | C–D |
| 3 | `F0` | 16 | 4 | 4 | `LdSt · Ld · Mul · ALU` | F / sel 0x01 |
| 4 | `F11` | 16 | **5** | **5** | `Ld · ALU(S1) · Mul · ALU · ALU` | E / bit27=1 |
| 5 | `F1` | 16 | 4 | 4 | `LdStALU · Ld · Mul · ALU` | F / sel 0x03 |
| 6 | `F2` | 16 | 4 | 4 | `LdSt · Ld · Mul · ALU` | F / sel 0x33 |
| 7 | `F3` | 16 | **5** | **5** | `LdSt · Ld · Mul · ALU · ALU` | E / bit27=0 |
| 8 | `F4` | 16 | 4 | 4 | `Ld · Ld · Mul · ALU` (dual-load) | F / sel 0x09 |
| 9 | `F6` | 16 | 4 | 4 | `LdSt · Ld · Mul · ALU` | F / sel 0x23 |
| 10 | `F7` | 16 | 4 | 4 | `LdSt · Ld · Mul · ALU` | F / sel 0x13 |
| 11 | `N1` | 8 | 3 | **2** | `LdSt · None · Mul` | F / sel 0x08 |
| 12 | `N2` | 8 | 2 | 2 | `LdSt · Ld` | F / sel 0x18 |
| 13 | `N0` | 8 | 4 | **2** | `LdSt · None · None · ALU` | F / b3lo {0,2,4,6} |

Slot-count census: `1+1+1 + 4+5+4+4+5+4+4+4 + 3+2+4 = 46 = num_slots` (EXACT). `F5/F8/F9/F10` do not
exist (sparse numbering). **Peak issue width = 5** (F3/F11). The three `None` slots (`N0_S1`,
`N0_S2`, `N1_S1`) host exactly one op — `nop` — NOP-only filler, not real issue ports. The per-slot
opcode census sums to **12569** `Opcode_*_Slot_*` placements; `F4_S2_Mul = 61` (not the older
60/65). Device cross-check: **1479/1479** real FLIX bundles in `libneuroncustomop.a`
decoded with `xtensa-elf-objdump` (0 miss; this management library tops out at 4-op bundles — the
5-issue F3/F11 vision bundles are validated by structure, not by this scalar-dominated sample).

### 2.2 Per-slot datapath binding — the class-exclusive lanes `[HIGH × OBSERVED]`

Beneath the *permissive* `SLOT_OPCODES` supersets (Tensilica lists nearly every op as slot-legal),
the **semantic class** binding is the schedulable fact, derived from the slot/unit and
cross-checked against the opcode-name superset ([co-issue-matrix §2], [regfile-ports
§3], [simd-datapath §3.6/§4.3]):

* **MEMORY** (`ivp_sem_ld_st` + scatter/gather): **S0** (`LdSt`/`LdStALU` = full load+store + the
  richest gather lane) and **S1** (`Ld` = load-only). The 2-LSU unified config exposes exactly two
  memory-port interfaces — `nx_Load_0_interface`, `nx_Load_1_interface`, `nx_Store_0_interface`
  (**no** `nx_Store_1_interface`) — so a bundle issues **≤ 2 memory ops, of
  which ≤ 1 STORE** (stores live only in S0). `F4` = 2 loads (no vector store).
* **INTEGER QUAD-MAC** (`ivp_mulqa*`): **EXCLUSIVELY `S2_Mul`**, one per format — at most **one**
  integer quad-MAC per bundle. Confirmed: `Opcode_ivp_mulqa*` outside `*_s2_mul` = **∅**;
  `HAVE_MAC16 = 0` (no replicated multiplier).
* **FP FUSED MULTIPLY-ADD** (`madd/msub.{s,h}`, `ivp_mulan/mulsn_2xf32`, FP-divide-step `divn`):
  primarily **`S3_ALU`**, but **NOT S3-exclusive** — see the CORRECTION below.
* **VECTOR / FP ALU** (`vec_alu`/`mov`/`shift`/`vbool`): `S3_ALU` + `S4_ALU` (F3) and `S1_ALU` + S3 +
  S4 (F11) — 2–3 dedicated ALU lanes.

> **CORRECTION (divergence #4) — FP-FMA is NOT `S3_ALU`-exclusive; the bound is slot-count
> `1×S2 + 1×S3`, not slot identity.** A prior framing asserted "`MADD`/`MSUB` live in S3_ALU, S2_Mul
> has zero MADD." The binary refutes it: `nm libisa-core.so | rg
> 'Opcode_madd_s_Slot'` lists **`s2_mul` ×3** alongside `s3_alu` ×4 (and `msub_s` identically); on
> **N1** — which has no S3_ALU slot — the only FP-FMA placement *is* in `S2_Mul`. In `libcas-core.so`,
> `my_vec_2_opnd_ivp_sem_spfma_{vr,vs,vt}_use` **and** `my_vec_3_opnd_ivp_sem_spfma_*` both resolve.
> **Unified model:** `S2_Mul` (integer-widen) and `S3_ALU` (FP-FMA) are **distinct issue ports with
> overlapping eligibility**. They **co-issue** — the densest MAC-class bundle is `1 integer quad-MAC
> (S2) + 1 FP-FMA (S3)`. But an FMA *placed on S2* **excludes** a same-bundle integer MAC (they
> contend for the one S2_Mul lane). This **supersedes** any per-format "1–3 multiply-class" superset
> count, which was an artifact of the permissive opcode lists. `[HIGH × OBSERVED]`
> ([co-issue-matrix §2.3/§3.2], [regfile-ports §3], [simd-datapath §4.3], [vfpu-ieee §5])

> **NOTE (divergence #5) — the MAC count frame is `188 = 65 signed + 123 mixed`, with `24` FP-typed
> forms reclaimed to B17/B18.** The corrected partition ([B04]/[B05]): the integer-vector MAC roster
> is **188** = 65 signed (B04) + 123 mixed/unsigned/complex (B05, with `us 54 / su 21 / uu 37 /
> complex 11`); the **24** FP-typed MAC forms (`*xf16`/`*xf32`/`*sone`) belong to the FP-FMA pages
> [B17 spfma]/[B18 hp-fma], **not** the integer MAC. The older `71/141/212` framing was arithmetically
> loose (it left the 24 FP forms inside the integer count). Use **188 = 65 + 123** for the integer
> quad-MAC and route the 24 FP forms to the VFPU. `[HIGH × OBSERVED]`

**The refined co-issue rule** (the densest compute bundle is F3): `{2 mem (≤1 store), 1 integer
quad-MAC (S2), 1 FP-FMA (S3), 1 ALU}`; F11 = `{1 mem, 1 ALU(S1), 1 quad-MAC, 2 ALU}`. `[HIGH ×
OBSERVED]`

### 2.3 The two coupled pipelines `[HIGH × OBSERVED — pipeline-timing.md]`

The core runs **two pipelines that share fetch/issue but differ in depth**. The ISS stamps every
operand's read/write stage as a `mov $0xN,%esi` immediate in the per-`(format,slot,opcode)` `_issue`
function body — that compiled stage table is the ground truth.

**(A) SCALAR / ADDRESS pipe — 7 stages, `A1 / B3 / E4 / M5 / W6` (+ deep retire `D9`):**

| stage | name | what settles |
|:--:|---|---|
| 1 | A (address) | AR base read for L/S address-gen; `MOVI` immediate DEF; post-increment AR write |
| 3 | B (decode/branch) | branch condition read + resolve; vision-op issue marker; `CPENABLE` cp1 gate sampled |
| 4 | E (execute) | simple-ALU compute + AR write (`arr·ars·art`); BR write |
| 5 | M (memory) | scalar load data return; store data egress into the write buffer |
| 6 | W (writeback) | MUL32/MUL16 result; SR writes; XTSYNC sync barriers |
| 9 | D (done/deep) | retire point the deep vector pipe's tail aligns to |

> **CORRECTION (divergence #2) — two stage-numbering conventions, `+1` apart on E/M; this synthesis
> uses ISS `A1/B3/E4/M5/W6`.** The **TIE-root** convention is `r0 / e3 / m4 / w6`; the **ISS
> config** convention (the one the latency stamps actually use) is `A1 / B3 / E4 / M5 / W6` (+ deep
> `D9`). They **agree on W (=6)** and on the stage-10+ vector ports, but the ISS execute/memory
> stages sit one higher: `ISS-E4 ≡ TIE-E3`, `ISS-M5 ≡ TIE-M4`. **All numeric stages on this page are
> in the ISS `A1/B3/E4/M5/W6/D9` convention** — to translate any stamp to the TIE convention,
> subtract 1 from E/M (W and the vector ports unchanged). `[HIGH × OBSERVED]` ([pipeline-timing §2]).
> (The [config-reference-sheet §9] additionally cites a TIE-DB `num_pipe_stages = 15` — the full
> declared pipeline length used for latency modeling — and the coarse ISS `B3/E4/M5/W6`; both are
> consistent with the per-operand stamps below.)

**(B) VECTOR / VFPU pipe — DEEP parallel pipe, ~stages 2..15** ([pipeline-timing §2.2], [vfpu-ieee
§1]):

| stage | role | ISS evidence |
|:--:|---|---|
| 3 | `CPENABLE` cp1 coprocessor-enable gate (USE; clear ⇒ squash, the only precise FP exception) | `my_CPENABLE_stall @0x178e420` |
| 8–9 | early flops — per-lane mask / gather-offset / divide-early read; **the stage-9 vector memory port** | load hazard `mov $0x9` |
| **10** | **THE UNIFIED VEC READ PORT** — all `vec`/`vbool`/`gvr`/`valign` source operands | universal `opnd_sem_vec_addr → mov $0xa` |
| 11 | 1-cycle result: vec-ALU / vec-mov / vec-shift / vbool-ALU / compare / FP min-max | `IVP_ANDB`/`IVP_MINN_2XF32T` DEF @11 |
| 12 | 2-cycle result: multiply / quad-MAC / reduce / pack / lookup / select / permute / divide-step / `TRUNC`; the `wvec` `(12,12)` RMW | `IVP_MULQA2N8QXR8` wvec DEF @12; `IVP_TRUNCN_2XF32T` DEF @12 |
| 13 | 3-cycle result: FMA / SP·HP convert / recip-rsqrt seed | `IVP_MULAN_2XF32T` DEF @13 |
| 14 | IEEE FSR flag DEFs (Invalid/DivZero/Overflow/Underflow/Inexact) | each FP `_issue` opens with 3–5× `mov $0xe` |
| 15 | deferred imprecise vector fp exception post (`VectorPipeImpreciseErr`) | `nx_VectorPipeImpreciseErr_interface` |

The vector pipe is **not** a continuation of the scalar pipe — it is parallel, with operand read @10
and a forwarding network bridging every write stage (11/12/13) back to the @10 read, so
**dependency-latency `== write_stage − 10`** exactly. A vision op has a long *issue-to-result* depth
(~8–10 absolute stages) but a **short 1–3 cycle producer→consumer dependency latency** because of
that forward. `[HIGH × OBSERVED gaps; INFERRED network]`

### 2.4 The reservation / timing substrate — present, not empty `[HIGH × OBSERVED — co-issue-matrix §4]`

> **CORRECTION (divergence #1) — the per-`(format,slot,opcode,stage)` reservation model IS present
> in `libcas-core.so`; it is NOT an empty wall. The realizable structural bound is operand-
> availability + mem-port + FLIX slot-count; exact bank/issue stall cycle-counts stay MED.** Two
> sibling framings differ legitimately and are unified here against the binary:
>
> * **[pipeline-timing §6]** framed the model as the "honest empty-reservation wall": the TIE
>   `MODULE_SCHEDULE` reservation bodies ship **empty** (1994 self-closing elements) and the
>   `…_stall` functions implement a per-register DEF/USE **scoreboard** (RAW/WAW availability), with a
>   resource-keyword scan (`reservation|port_busy|fu_busy|…`) over all three DLLs returning **0
>   hits** — concluding the finer per-port co-issue ceiling is unrecoverable.
> * **[co-issue-matrix §4]** found that while the TIE encoding DB (`libisa-core.so`) indeed carries
>   no schedule, the **cycle model `libcas-core.so` ships the reservation FULLY POPULATED** as
>   function bodies.
>
> **Binary arbiter (`nm libcas-core.so | rg -c`):** `_issue` = **2149**,
> `_stall` = **1746**, `_stage<N>` = **~160 k** (stages 0–15, ~159 937 by this regex). The empty
> `MODULE_SCHEDULE` is the *TIE-DB* table; the populated per-stage `_issue` bodies **ARE** the
> scoreboard/reservation model. **Unified model:** the realizable structural-hazard substrate is
> `operand-availability (RAW/WAW scoreboard) + the 2 mem-ports + the FLIX slot count + the
> class-exclusive lane partition`. The **one genuine functional-unit port** modeled is memory-only —
> the `nx_{Load_0,Load_1,Store_0}_interface` set, called **only** by load/store `_stall` functions
> (arithmetic `_stall` calls no interface); compute units have **no** modeled busy/port counter
> beyond the slot count. So: every functional unit is fully pipelined (`II = 1`) bounded by FLIX slot
> count + operand availability; the **exact per-port single-issue stall cycle counts are recoverable
> in principle from the populated stage bodies but not reduced to a byte-readable literal here —
> `[MED]`, a task, not a wall.** `[HIGH × OBSERVED on the bodies' presence and the slot/mem-port
> bound; MED × the cycle counts]`

> **NOTE — the stall-count divergence is resolved to 1746.** [pipeline-timing §6/§7] and
> [regfile-ports §7] previously cited **1651** `_stall` functions (and [lsu-memory §8] cited a
> third stale variant, **1795**); [co-issue-matrix §4] cited **1746**. The binary
> (`nm libcas-core.so | rg -c '_stall$'`) returns **1746** — the co-issue-matrix figure
> is correct, and the source pages now carry an in-place CORRECTION to **1746**. `[HIGH × OBSERVED]`

> **NOTE — `libcas-core.so` has NO DWARF; symtab is 179 079, no debug sections (divergence #6).**
> `readelf -SW` shows **zero** `.debug_*` sections; "unstripped" here means a full `.symtab` only
> (179 079 symbols total; sibling pages cite 177 936 / 178 959 — text-only or filtered
> counts). All co-issue / stage / port facts are read from **symbol names + PLT relocations**, not
> DWARF type/line info. `[HIGH × OBSERVED]`

---

## 3. The datapath microarchitecture

> **Epistemic guard.** The RTL is not in the corpus. The SIGNALS, PORTS, STAGES, WIDTHS, LANE
> COUNTS, and VALUE FUNCTIONS are OBSERVED (and, for the multiply/FMA trees, the hardware-block model
> functions are **named** in `libcas-core.so`); the block-to-block WIRING is the unique topology
> consistent with them, tagged `[INFERRED]` where it is the only structure the observed
> ports/stages/values admit. `[HIGH widths/stages OBSERVED; topology INFERRED — simd-datapath.md]`

### 3.1 The 512-bit / 32-lane substrate `[HIGH × OBSERVED lanes; INFERRED carry-break]`

`VISION_SIMD16 = 32` ⇒ **32 physical lanes of 16-bit = 512 bits**. The lane width is the opcode's
lane **token**, not a mode register: `2NX8` (64×8b) / `NX16` (32×16b, native) / `N_2X32` (16×32b).
The 8/32-bit modes are **carved from the same 32×16b array** by a segmented-adder carry-break mask at
the 8/16/32 boundaries (corroborated by per-width `_8`/`_16`/`_32` value leaves with their own
saturation constants). **Five functional clusters** hang off ONE unified stage-10 vec read port;
results write through a STAGGERED port (10 load / 11 ALU / 12 MAC+perm+divstep / 13 FMA+cvt) — the
read ≫ write asymmetry that keeps the same-stage write-port count (~2–3) far below the ~10–11-port
read array (§7).

### 3.2 The VFPU FMA / FCR / FSR substrate — the 3-cycle SP/HP 2×FMA pipe `[HIGH × OBSERVED — vfpu-ieee.md]`

**ONE** fully-multiplexed fused-multiply-add tree serves **both** binary16 (`NXF16`, 32 lanes) and
binary32 (`N_2XF32`, 16 lanes); a decode-time op-class mux (`add_only`/`mul`/`madd`/`op_negadd`/
`op_maddn`/`op_mulsone`/`op_divn`/`op_pred`/`op_scalar` prefanouts) routes all 27 HP + 27 SP mnemonics
through it. The significand multiply is a **partial-product grid** (`mulpp25x25` for fp32's 24-bit
significand, `mulpp12x12` for fp16's 11-bit) — the same Booth/PP discipline as the integer array, not
a native multiplier. Internal 3-stage tree (the `s0/s1/s2` flop names):

* **s0** (~10→11) SIGNIFICAND MULTIPLY + CLASSIFY: the 2×2 partial-product grid → the EXACT product;
  exp sum, sign algebra (`negate_axb` for MSUB), inf/zero/NaN classify, RoundMode latch.
* **s1** (~11→12) ALIGN + SINGLE FUSED ADD: align the addend to the product exponent, **one** add on
  the EXACT (un-pre-rounded) product ⇒ a **true single-rounding FMA**.
* **s2** (~12→13) NORMALIZE + ROUND + FLAG: LZC normalize, round under RoundMode {RNE/RZ/RU/RD}; the
  ROUNDED result DEF **@stage 13**.

> **QUIRK — true single-rounding FMA; a reimplementer MUST NOT pre-round the product.** The product
> enters the fused add full-width (`s1_sum_sig_a = aligned(c) ± EXACT(a·b)`), so `madd` rounds
> **once**. The soft-float oracle proves it: madd `1152/1152` bit-exact across round modes. A two-step
> `c + round(a·b)` would diverge on inexact products. `[HIGH × CARRIED — validation/fp-soft-float.md]`

**FCR / FSR (the `gvr` "gr" CSR file, accessed via `RUR`/`WUR`, no data operand):**

* **FCR** (control): `RoundMode(2b)` + `{Invalid,DivZero,Overflow,Underflow,Inexact}Enable` (1b each)
  + the `CPENABLE` cp1 gate. Reset RoundMode = RNE (`00`). The 2-bit field encodes
  `RNE=0 / RTZ=1 / +inf=2 / −inf=3`; a 3rd-state `away/RNA=4` extension exists beyond the 2-bit field.
* **FSR** (status): the same 5 flags, each `XTENSA_STATE_IS_SHARED_OR` (`flags=0x02`) so the lanes
  sticky-OR into ONE bit (IEEE sticky); accumulate **@stage 14**; persist until `wur.fsr` clears.

The full 11-field FCR+FSR pack is the `movscfv`/`movvscf` pair: bits `{IxF@14,UfF@13,OfF@12,DzF@11,
IvF@10, RM@9:8, IxE@6,UfE@5,OfE@4,DzE@3,IvE@2}` (`Iclass_IVP_MOVVSCF_stateArgs @0x84b400`). The
**two-tier exception model**: PRECISE `Coprocessor1Exception` (cp1 enable sampled @stage 3; if clear
the op is SQUASHED before any datapath effect — `Coprocessor1Exception_exc @0x1780ff0`) vs IMPRECISE
(the 5 IEEE exceptions surface late, FSR flag @14 + `VectorPipeImpreciseErr` @15 — too deep to pin,
so software polls the sticky FSR). The **'N' forms** (`MADDN`/`MSUBN`/`MULAN`/`MULSN`) emit **neither**
flags nor the imprecise edge (zero `mov $0xe`) — the un-flagged fast path inside the DIVN/recip Newton
loop; `IVP_DIVNN_2XF32T` (the SP divide-step) **does** post 3 flags (no Invalid). `[HIGH × OBSERVED]`

> **NOTE — soft-float ISS vs hardware FP: a model/implementation pair of ONE value function.** The
> host ISS computes the fp math **integer-only** (the fp16 add body `@0x51c640` carries zero
> hardware-FP insns) because a bit-accurate model cannot trust the host's binary64 rounding; the
> differential proves it emits the SAME bits as the hardware tree (>150 000 lane comparisons, 0
> mismatch). Two implementations of one value function, not a conflict. `[HIGH × OBSERVED]`

### 3.3 The integer / quad-MAC pipes — the 2-cycle `S2_Mul` datapath `[HIGH × OBSERVED — simd-datapath §3]`

**No native multiplier.** The multiply datapath is a chain of **named hardware-block functions** in
`libcas-core.so` — Booth radix-4 encoders (`module_ivp_booth_enc_{8,16}`), partial-product generators
(`module_ivp_su_xtmulpp_8x8`), 3:2/4:2/5:2 carry-save (Wallace) compressors (11× `module_ivp_comp_*`),
the multiply-slice pipeline (`module_ivp_sem_mul_slice_stage{0..3}`) — each a bit-level model with
**zero** host `imul`. `is_signed` and `negate` (MULA `+` / MULS `−`) are **decode-time**, never
runtime modes.

The **1536-bit `wvec` accumulator** re-partitions the same bits by source width — `i8·i8 → 24-bit ×
64 lanes` / `i16·i16 → 48-bit × 32 lanes` / `i32·i32 → 96-bit × 16 lanes`, all `= 1536 = 3 × 512`.
Three decode-selected modes: OVERWRITE (`mul`/`muln`), RMW-ADD (`mula`), RMW-SUB (`muls`); the
accumulate **wraps mod 2^lanewidth** (no saturation; narrowing is a separate `packvr`).

**The 4-tap quad-MAC** (`QUAD_MAC_TYPE 1`): one issue reads the running `wvec` accumulator (inout) +
**four** vec taps + a `b32_pr` **radix** (split into 4 bytes); each output lane `= Σ_{i=0..3}
tap_i · radix_byte_i` — a fused 4-tap micro-dot-product (driven live: taps `[10,20,30,40]` × radix
bytes `[3,5,7,11]` → `520 = 0x208`). It demands **five** `vec` read ports (`vp/vq/vr/vs` inputs + a
narrow `vt` RMW) @10 **plus** the wide `wvt`/`wvu` `wvec` RMW @12.

> **THE 2-CYCLE RECURRENCE (the inner-loop critical path).** The wide accumulator is addressed as a
> low half `wvt` and a high half `wvu`, **both** read AND written at stage 12 — a same-stage
> `(12,12)` read-modify-write (`my_wvec_2_opnd_ivp_sem_multiply_{wvt,wvu}_{use,
> def}` all resolve). `write@12 → next-MAC read@12` is the SAME stage ⇒ single-cycle accumulator
> forward ⇒ the MAC chain **issues every cycle (`II = 1`)** with a **2-cycle result latency**
> (taps@10 → acc@12). Only **4** `wvec` entries ⇒ at most **4 independent MAC chains** live (a
> software register-pressure bound, not a port stall — one `wvec` RMW port per bundle, on S2_Mul).
> `[HIGH × OBSERVED]`

> **CORRECTION — the `wvec` RMW is TWO operands (`wvt` + `wvu`), not one.** A model tracking only
> `wvt` undercounts the accumulator's read/write ports by half. This synthesis adopts the
> binary-witnessed two-operand RMW ([regfile-ports §5], [simd-datapath §3.5]). `[HIGH × OBSERVED]`

Scalar integer: AR-ALU 0-cyc (E-stage forward @4), MUL32 2-cyc (@6, W-stage only — no @4 forward, a
real 2-cyc stall), DIV32 via QUOS/QUOU/REMS/REMU.

### 3.4 The two activation / transcendental engines `[HIGH × OBSERVED — activation-transcendental-tables.md]`

> **GOTCHA — TWO physically + architecturally SEPARATE table engines; do NOT conflate them.** They
> share no table, no key, no SoC region, and have different content provenance.

* **ENGINE A — the Q7 in-core transcendental SEED LUT (the "RECIP0 path").** A baked pair of
  **128×8-bit** tables — `RECIP_Data8 @0x958fc0` (`round(256/(1+(i+0.5)/128))`, monotone `0xff→0x81`)
  and `RSQRT_Data8 @0x958dc0` (a two-range exponent-parity table, odd-half first) — indexed by the
  top 7 (recip) / top 6 + exp-parity (rsqrt) mantissa bits, emitting a ~2⁻⁷ Newton seed (recip 7.0
  bits, rsqrt 7.5 bits, 50k-sample sweep). The reciprocal goes to full fp32 by a **single QLI
  (quadratic-interpolation) refine** off a second LUT pair (`fp_recip_qli_lut1/lut2 {A,gx}`, 6-bit
  segment index `seg = (x>>25) & 0x3f`); rsqrt/sqrt refine via the generic FMA Newton step. The seed
  ops (`ivpep_sem_hp_lookup` 30 + `ivpep_sem_sp_lookup` 29) are **ONE multiplexed S3-ALU-class lookup
  datapath, 2-cyc** (`vr@10 → vt@12`). **Content fixed in silicon and fully recovered byte-exact.**
* **ENGINE B — the ACT-engine piecewise-CUBIC PWP table (the "activation path").** A **separate
  four-table SoC-block machine** (CAM 32 B → PROFILE 128 B → CONTROL 32 B `{act_tbl_base:11,
  extract_lsb:5, extract_size:4}` → BUCKET 32 B `{float d0,d1,d2,d3,x0}`), evaluating `d0 + d1·t +
  d2·t² + d3·t³` (`t = x − x0`) over a log-spaced fp-bit-extract index. On **maverick (v5)** the PWP
  SRAM physically folds into the DVE block (`PWP_CONTROL_TABLE @0xB0000`, `PWP_BUCKETS_TABLE
  @0xC0000`). The format is fully recovered; the per-function **coefficient CONTENT** (relu/gelu/
  sigmoid/tanh/exp cubics) is **host-loaded** per model (`0x23 ACTIVATION_TABLE_LOAD`) and is
  **OUT-OF-CORPUS** — the host-supplied-content wall.

**The relationship:** Engine A is a fixed 8-bit per-lane arithmetic **primitive** a kernel composes
(softmax `1/sum`, layernorm `1/√var`); Engine B is a programmable per-segment cubic **function** the
compiler emits as a fused 2D activation. They meet only at the algebra layer and share the §3.2 VFPU
FMA/FCR/FSR substrate (Engine A's refine rides the 'N' no-imprecise FMA path; Engine B's affine runs
flagged). `[HIGH × OBSERVED tables/format; LOW × the host PWP content WALL]`

### 3.5 The load/store + valign datapath `[HIGH × OBSERVED — lsu-memory.md]`

**Two load/store units** (`LSU=2`, `UNIFIED_LSU=1`): **S0** = full Load+Store (+ the richest
scatter/gather lane), **S1** = Load-only — proven by the `nx_{Load_0,Load_1,Store_0}_interface` set
(no `Store_1`) and stores resolving in S0 only (0/18 CAS, 0/66 ISA stores in S1). **Access granules**
(do not conflate): **64 B** = compute data bus / natural access / vector-reg quantum; **8 B** = SBUF
SRAM word; **16 B** = on-core iDMA beat; **32 B** = SBUF DMA-engine beat AND I-cache access granule.

**UNALIGNED POLICY** (the central asymmetry): scalar unaligned L/S **TRAPS**
(`UNALIGNED_*_EXCEPTION 1`, `*_HW 0`) — scalar pointers must be aligned; vector unaligned streams are
handled **entirely** by the `valign` file (a correctness requirement, not an optimization). **The
valign accumulator:** load side `uul` prime (`ivp_la_pp`/`ZALIGN`/`MALIGN`) → iterate (`LA*`
concatenates the residue with the next aligned row, extracts the 512-bit vector, updates the residue);
store side `uus` seed (`ivp_salign_i`) → iterate (`SA*` rotates bytes into the accumulator, commits a
64-byte-ALIGNED window with byte-DISABLES masking the head/tail) → flush (`SAPOS`). **The hardware
therefore only ever issues naturally-aligned 64-byte accesses.** `[HIGH × OBSERVED]`

> **NOTE — the valign load-side stage has two consistent views: @9 (hazard) vs @10/@12 (file-port).**
> The hazard/dependency model exposes the load-side `uul` at **@9** (the forwarding-availability
> stage, where the new residue is available to forward to the next `LA*`); the file-port model
> ([regfile-ports.md]) exposes the `valign` file as **read @10 / write @12** (the architectural
> register-file port). These are not in conflict — `@9` is one stage ahead of the @10 file read. Cite
> **@9** for back-to-back aligning-load chains, **@10/@12** for read/write-port pressure. The store
> side is `uus` @10 (RMW), commit @11. `[HIGH × OBSERVED — lsu-memory §5.3]`

### 3.6 SuperGather address-gen + the permute crossbar `[HIGH × OBSERVED — simd-datapath §5/§6]`

**SuperGather** (`SUPERGATHER 1`, 8 gather + 2 scatter regs, 32 elem/cycle) is a memory-subsystem
engine in S0/S1: A-phase (`GATHERA*`, S0) posts per-lane `addr_k = VAddrBase + offset_k·elem_sz` +
validity into the `gvr` "gsr" staging file; D-phase (`GATHERD*`, S1) muxes the collected bytes into a
`vec` reg. `GSControl[3:0]`: 1=gathera 2=gatherd 3=mgatherd 4=scatter 5=scatterw 6=scatterinc
(histogram-add). Ports: base/control @1, offset-vector/enable/scatter-data @10.

> **CORRECTION — `gvr` carries real scatter/gather data operands; it is not "RUR-only / no-operand".**
> `my_gvr_0_opnd_ivp_sem_vec_scatter_gather_gt_def` (target index vector, A-phase, slot 0) and
> `my_gvr_1_opnd_ivp_sem_vec_scatter_gather_gs_use` (source index vector, D-phase, slot 1) both
> resolve. `gvr` is **dual-role**: gather-staging data path **and** VFPU
> FCR/FSR CSR (`RUR`/`WUR`) — both real. `[HIGH × OBSERVED]` ([regfile-ports §2], [simd-datapath §5.2])

**The PERMUTE crossbar** (`vec_select`+`seli`, 2-cyc @12): `SEL` (`2N→N` full crossbar), `SHFL`
(`N→N` intra-reg), `DSEL` (dual-output butterfly), `DCMPRS` (predicate left-pack) — a full 2N-input
lane mux distinct from the `valign` byte-rotate alignment crossbar.

---

## 4. The memory hierarchy as the core sees it `[HIGH × OBSERVED — lsu-memory.md]`

### 4.1 The flat NX-local address map (identity-mapped, no MMU)

| NX range | size | what it is | backing / reach |
|---|---|---|---|
| `[0x00000000,0x00010000)` | 64 KiB | INSTRAM0 (IRAM): code + reset/exception vectors (VECBASE @`0x0`) | on-core SRAM; ECC 0; L/S-able; no iDMA |
| `[0x00010000,0x00080000)` | 448 KiB | gap / MPU device-trap hole | — |
| `[0x00080000,0x00090000)` | 64 KiB | DATARAM0: the data side, **4 banks**, 512-bit bus | on-core SRAM; ECC 0; iDMA target; ISB top @`0x8FED0` |
| `[0x00090000,0x00100000)` | 448 KiB | gap / MPU device-trap hole | — |
| `[0x00100000,0x40100000)` | **1 GiB** | SYSTEM-RAM AXI window (`XSHAL_RAM`, VECTOR1 @`0x100000`) | PIF/AXI master; cached-writeback attr (write-combining only — no D-cache) |
| `0x70000000` | 224 MiB | IOBLOCK cached (devices) | AXI |
| `0x90000000` | 224 MiB | IOBLOCK bypass (devices) | AXI bypass |
| `0xA0000000` | 512 MiB | RAM bypass alias | AXI bypass alias |
| `0xC0000000` | 512 MiB | SimIO | AXI sim/host I/O |

Overlaid by the device runtime (5-region software TLB): NX `0x80000000` → SoC `STATE_BUF` (SBUF 32
MiB) pinned 64-MiB window; NX `0x84000000` → `hbm_scratch`; three 16-MiB dynamic windows. The local
DataRAM `[0x80000,0x90000)` is **rebased per core** (`idx = 9 + 2*cpu_id`) into a disjoint SoC slice —
the one place the 8 SPMD cores have hardware-disjoint memory. `[HIGH × OBSERVED]`

### 4.2 SBUF via AXI / axi2sram — no compute port, no PSUM `[HIGH × OBSERVED]`

The Q7 has **no dedicated SBUF compute port**. It reaches the 32-MiB SBUF as memory-mapped AXI
through the `axi2sram` bridge: SBUF appears at the AXI aperture and, on-core, through the pinned
64-MiB NX window @`0x80000000`. A GPSIMD load/store to SBUF is an AXI/ACE-Lite transaction that
arbitrates as the **1/8 TDM DMA slot** behind the 2-stage SBUF arbiter — the longest data path the
core issues. SBUF addressing: 29-bit linear (`partition*PARTITION_SIZE + byte_offset`, mask
`0x1fffffff`), 128 partitions, a GPSIMD core owns 16 contiguous; `STATE_BUF_WORD_SIZE = 8`.

> **CORRECTION — PSUM is structurally unreachable by the Q7 (the "no-PSUM" fact).** PSUM
> (`[0x2000000,…)`) has **no AXI aperture and no NX window** → the Q7 cannot dereference it. The
> compiler's "GPSIMD args must be in SBUF" rule is this **absence**, not a policy. `[HIGH × OBSERVED]`

### 4.3 The per-core dmem / scratchpad + the table SRAMs `[HIGH × OBSERVED]`

The 64-KiB DataRAM0 (4 banks, 512-bit bus, single-cycle SRAM folded into the pipe — scalar @5,
vector @9) **is** the per-core scratchpad; **no D-cache** means every data access is a fixed-latency
local-RAM or AXI transaction (no miss/refill on the data side). The table SRAMs are separate: Engine-A
SEED LUT is **baked** into the in-core S3-ALU lookup unit (resident, recovered byte-exact); Engine-B
PWP SRAM is an off-core ACT-engine (or v5 DVE) block whose coefficient content is host-loaded (the
wall). The on-core **iDMA** is a separate 128-bit master (1 channel, 32 outstanding, 64-B descriptor,
DataRAM-targeted; done/err INT 35/36), distinct from the SBUF-side DMA engines.

### 4.4 The atomic / ordering model `[HIGH × OBSERVED — atomics-ordering.md]`

**Weak, store-buffered consistency with EXPLICIT-FENCE ordering** (canonical NX). Single-core program
order preserved for dependent accesses; between distinct addresses / internal-vs-external targets,
stores are NOT ordered (the 8-entry write buffer drains asynchronously, out of order). A plain aligned
32-bit access is single-copy atomic; wider/unaligned is not (and unaligned traps). Multi-master
ordering needs an explicit fence: **MEMW** (full, `0x0020c0`), **EXTW** (drain external write buffer +
wait AXI write-response, `0x0020d0`, the deepest), **DSYNC** (intra-core L/S), **ISYNC**/RSYNC/ESYNC,
or the exclusive monitor **L32EX/S32EX** (gated by `ATOMCTL` SR#99=0x63, 10-bit `[9:0]`, external
memtype; XTSYNC barrier @stage 6; `S32C1I` absent ⇒ **LL/SC not CAS**), or the one-sided
**RELEASE_SYNC** pair (`S32RI`/`L32AI`, present but unused). The shipped SPMD code uses **only** the
doubled doorbell barrier `memw; memw; store doorbell` (drain descriptor/payload writes before the SDMA
launch) — the central correctness fence; **zero** exclusives/spinlocks shipped (data-race-free by
per-core-private design; 32 total `memw` across the 10 customop objects, all SDMA doorbell barriers).
**STRONG_ORDER** is a host/debugger-armed bit-9 surprise ("re-program ordering," promotes the core to
strongly-ordered AXI), **not** a kernel fence. `[HIGH × OBSERVED structure; MED × off-core cycle adds]`

---

## 5. The per-class latency / throughput reference table

Per-semantic-group result latency + dependent-issue latency, recovered from the
per-`(format,slot,opcode)` `_issue` stage stamps. `result@` = the DEF stage of the architectural
result; `dep-lat` = `result@ − consumer read stage` (vector consumers read @10; `wvec` accumulator
@12; scalar AR @4). All units are fully pipelined (`II = 1`) per §2.4. This is the deliverable a
cycle-approximate model indexes by op class.

### 5.1 Vector (`xt_ivp32`) — read port @10 `[HIGH × OBSERVED — pipeline-timing §3, vfpu-ieee, activation-transcendental]`

| group | n | result@ | dep-lat | class / lane |
|---|---:|:--:|:--:|---|
| `ivp_sem_vec_alu` | 243 | 11 | 1 | vector integer ALU — S3/S4/S1_ALU |
| `ivp_sem_multiply` | 232 | 12 | 2 | SIMD multiply / quad-MAC — **S2_Mul** |
| `ivp_sem_ld_st` | 185 | 10 (mem) | 0 fwd | vector load/store — S0/S1 |
| `ivp_sem_vec_reduce` | 56 | 12 | 2 | horizontal reduce |
| `ivp_sem_vec_mov` | 44 | 11 | 1 | vec move / shuffle — S3/S4 |
| `ivp_sem_wvec_pack` | 42 | 12 | 2 | wide-acc pack/narrow |
| `ivp_sem_vec_shift` | 32 | 11 | 1 | vector shift — S3/S4 |
| `ivpep_sem_sp_cvt` | 31 | 13 | 3 | SP float convert — S3_ALU |
| `ivpep_sem_hp_lookup` | 30 | 12 | 2 | HP seed/table lookup (Engine A) — S3 |
| `ivpep_sem_sp_lookup` | 29 | 12 | 2 | SP seed/table lookup (Engine A) — S3 |
| `fp_sem_hp_fma` | 27 | 13 | 3 | HP fused multiply-add — S2\|S3 |
| `ivp_sem_spfma` | 27 | 13 | 3 | SP fused multiply-add — S2\|S3 |
| `ivp_sem_vec_rep` | 26 | 12 | 2 | replicate / broadcast |
| `ivp_sem_vec_scatter_gather` | 23 | 10/11 | 0–1 | SuperGather A/D-phase — S0/S1 |
| `ivp_sem_vbool_alu_ltr` | 21 | 11 | 1 | vector-boolean ALU (AR-result @12 on `ltr`) |
| `ivpep_sem_hp_cvt` | 21 | 13 | 3 | HP float convert — S3_ALU |
| `ivp_sem_unpack_wvec_mov` | 18 | 12 | 2 | wide-acc unpack / move |
| `ivp_sem_vec_select` | 18 | 12 | 2 | vector select / permute crossbar |
| `ivp_sem_divide` | 14 | 12 | 2* | SIMD divide-STEP (valign scratch) |
| `ivp_sem_vec_histogram` | 8 | 12 | 2 | histogram accumulate (scatterinc) |
| `ivp_sem_sqz` | 6 | 12 | 2 | squeeze / compact (AR-result @12) |
| `ivp_sem_vec_specialized_seli` | 6 | 12 | 2 | specialized select-immediate |
| `bbn_sem_vec_sprecip_rsqrt` | 5 | 13 | 3 | SP recip / rsqrt seed (Engine A) |

> **QUIRK — `TRUNC` is one cycle faster than every other convert.** `IVP_TRUNCN_2XF32T`,
> `IVP_NEXP01N_2XF32T`, `IVP_MKSADJN_2XF32T` write @stage **12** (2-cyc), while `IVP_FLOATN_2X32T`
> and int↔float/widen converts write @**13** (3-cyc). Model the convert unit as 3-cycle with a 2-cycle
> `TRUNC` fast path. `[HIGH × OBSERVED — pipeline-timing §3.1, B13]`

`*divide`: inputs early-read @8, result @12; a full quotient = N chained `DIVN` step-ops (e.g.
`DIVN_2X32X16U_4STEPN`). **wvec accumulating MAC**: the `(12,12)` self-RMW chain accumulates at
`II=1` with a 2-cyc result latency. **2×FMA throughput**: `SP/HP_VFPU_2XFMAC` doubles per-cycle FMA
throughput (2 FMA/cyc in the FMA lane) while latency stays 3 cyc (`[HIGH config; INFERRED dual-issue
micro-binding]`).

### 5.2 Scalar core (`A1/B3/E4/M5/W6`) `[HIGH × OBSERVED — pipeline-timing §3.2]`

| class | result@ | dep-lat | notes |
|---|:--:|:--:|---|
| ADD/SUB/ADDI/ADDX/logic (incl. `.N`) | 4 | 0 | E-stage forward (back-to-back IPC) |
| BOOLEAN (AND/OR/XOR B) | 4 | 0 | BR @4 |
| MUL32 (MULL/MULUH/MULSH/MUL16S) | 6 | 2 | W-stage only; **NO @4 forward** = 2-cyc stall |
| branch (B*/B*.W15) | resolve @3 | — | BranchTarget/Taken @3 (BTB) |
| L8/16/32 I, L32R, L32AI | 5 | 1 | M-stage load-use bubble; addr base @1 |
| S8/16/32 I, S32RI | egress @5 | — | into 8-entry write buffer |
| L32EX / S32EX (exclusive) | 5; XTSYNC @6 | — | exclusive-monitor sync barrier @6 |
| RSR/WSR/XSR (special reg) | art @5 / SR @6 | — | SR read @5, SR write @6 |
| vision-scalar (`IVP_SQZN`, `RUR.FSR/FCR`, `vbool_alu_ltr`) | arr @12 | ~8 | deep-pipe scalar result; software schedules the gap (no fast forward vector→scalar) |

### 5.3 Memory-access latency `[HIGH on-core × OBSERVED; MED off-core]`

| access | result / return | load-use | notes |
|---|---|:--:|---|
| I-cache hit (fetch) | feeds instbuf/cycle | 0 | 32-B/cyc into 256-bit instbuf |
| I-cache miss (refill) | `*` | `*` | 64-B line = 2×32-B AXI beats + fabric RTT `[MED]` |
| scalar load (L32I/L16UI) | @5 | 1 | M-stage data return |
| scalar store (S32I) | egress @5 | — | into 8-entry write buffer |
| exclusive L32EX/S32EX | @5 + XTSYNC @6 | 1 | monitor-sync barrier @6 |
| vector load (IVP_LVN) | port @9, result @10 | 0-cyc fwd | stage-10 vec read bypass to next bundle |
| vector store (IVP_SVN) | src @10, commit @11 | — | 512-bit store egress (commit-late-by-one vs load) |
| scatter/gather (SuperGather) | offset @10, result @10/11 | 0–1 | address-vector op, S0/S1 |
| local DataRAM (4-bank) | folded (scalar @5 / vec @9) | as above | no D-cache; fixed-latency; bank-conflict `*` `[MED]` |
| SBUF via AXI (GPSIMD) | on-core stage + AXI `*` | + arbiter | 1/8 TDM DMA slot, 2-stage SBUF arbiter `[MED]` |
| system RAM (PIF/AXI, 1 GiB) | AXI RTT `*` | + write-buf | 8-entry write buffer decouples stores `[MED]` |

`*` = SoC-fabric-dependent or buried inside the ~160 k stage bodies — structure pinned, **cycle count
MED** (the one piece not reducible to a byte-readable literal: DataRAM 4-bank conflict timing, the
I-cache miss penalty, the SBUF-AXI round-trip).

### 5.4 Structural co-issue bounds (the throughput ceiling) `[HIGH × OBSERVED]`

* Issue width ≤ format slot count (F3/F11 = 5; F0–F7 = 4; N0–N2 = 2; x24/x16 = 1).
* Memory ≤ 2/bundle (S0 + S1, ≤ 1 store).
* Integer quad-MAC ≤ 1/bundle (the single `S2_Mul` lane).
* FP-FMA / FP-divide-step ≤ 1/bundle on `S3_ALU` (co-issuing with the S2 MAC) **or** on `S2_Mul`
  (excluding the MAC) — `1×S2 + 1×S3`, not exclusivity.
* Vec / FP-ALU ≤ 2 (F3) / ≤ 3 (F11).
* `vec` read port sized to **~10–11 simultaneous @10 reads** for the 5-slot formats (per-slot read sum
  `{3,1,5,4,2}`); writes stagger across 10/11/12/13 (worst same-stage write = F11's 3 ALU lanes @11).

**PEAK COMPUTE / cycle** (a well-scheduled F3/F11 bundle): `1 integer quad-MAC (4 i16 taps → wvec) +
1 FP-FMA (16 fp32 or 32 fp16, ×2 via 2×FMAC) + 2–3 vec-ALU + 2 mem/gather`. `[HIGH counts; INFERRED
peak composition]`

---

## 6. Generation invariance — one Q7 core, five NeuronCores `[HIGH × OBSERVED — rev-markers.md]`

There is exactly ONE `ncore2gp` Vision-Q7 config (ConfigID `0xC4019686`, HW `NX1.1.4`), and every
NeuronCore generation's device firmware is built **for this one core** (`coretype {6,13,21,29,37}` =
SUNDA/CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK = v2..v5). A SINGLE Vision-Q7 reimplementation — one FLIX
decoder, one microarch, one boot/IRQ spine, one frozen Q7-control CSR core — covers all five, selected
by SoC-fabric **scaling axes**, not by core-microarch differences. The §1–§5 model is
**generation-invariant**.

**The scaling axes (what differs per gen):**

* Per-core IRAM/DRAM aperture geometry: SUNDA 64 KiB IRAM + 64 KiB DRAM (no reserved tail,
  POOLING-only outlier); CAYMAN+ 128 KiB IRAM + 256 KiB DRAM + reserved tails. (This is the
  host-allocated aperture; the on-core `core-isa.h` INSTRAM0/DATARAM0 are each 64 KiB — §4.1.)
* Q7 `base_offset` (BAR0-rel): SUNDA `0x2980000`; CAYMAN/MARIANA `0x3100000`.
* The run-stall release path: CAYMAN/MARIANA one host CSR write (`0xFF → 0x00`); SUNDA the SEQ/SP
  `EVT_SEM` rendezvous.
* The ACT PWP host content + (v5) the ACT→DVE HW-region fold (§3.4).

**Gen-invariant (the core model):** the `ncore2gp` microarch, the XEA3 boot spine + magic words
(`0x6099CB34`/`0x502B2DA1`), the 8-core PRID-gated `_SharedResetVector`, the run-stall reset `0xFF`,
the 14-format/46-slot FLIX decode, the §5 latency model. **EXCLUSION:** TONGA V1 is a genuinely
different device CSR schema — not covered by the v2..v5 reimplementation (and ships no NCFW/GPSIMD
image). `[HIGH × OBSERVED]`

---

## 7. The unified block diagram + connectivity map

`[HIGH × OBSERVED stages + named modules; INFERRED box-to-box wiring]`

```
  SCALAR FRONT-END (shared, 7-stage: A1 / B3 / E4 / M5 / W6 / D9)
  +------------------------------------------------------------------------------------+
  | s0 fetch | s1 AGU base (ars@1 -> VAddrBase) | s3 vision decode/issue + CPENABLE     |
  |  256-bit instbuf, 32-B/cyc | branch resolve @3 | cp1 enable gate (USE @3, squash)   |
  +----------+----------------------------------------------------------------+---------+
             |  FLIX bundle  (peak 5 slots: S0  S1  S2_Mul  S3_ALU  S4_ALU)
             |     mem(S0,S1<=1 store) | intMAC(S2) | FP-FMA(S2|S3) | ALU(S3/S4,+S1 F11)
             v
  ==========================  THE 512-bit DEEP VECTOR PIPE  ===========================
   s8-9   per-lane mask / gather-offset / divide-early read | STAGE-9 vector mem port
   s10  +===================================================================+
   THE  |     UNIFIED 512-bit vec READ PORT   (all vec _use @10; ~10-11 wide)|
   vec  |     feeds ALL clusters; vbool@10, valign-divide@10, GSVAddrOff@10  |
   read +=+=========+=============+===============+================+=========+
          | (S0/S3/S4) (S2_Mul)    (S2|S3)         (S0/S1 mem)      (S3 + pinned)
          v          v             v               v                v
     +---------+ +-----------+ +-------------+ +--------------+ +----------------+
     | VEC-ALU | | QUAD-MAC  | | VFPU 2xFMA  | | SuperGather  | | PERMUTE crossbar|
     | 32-lane | | Booth+PP  | | mulpp25x25  | | addr-gen     | | SEL/SHFL/DSEL/  |
     | 8/16/32 | | +CSA tree | | (fp32 sig)  | | base+off*sz  | | DCMPRS          |
     | add/sub | | -> wvec   | | mulpp12x12  | | -> gvr (gsr) | | 2N->N / N->N    |
     | logic/  | | acc RMW   | | (fp16 sig)  | | +GSEnable    | | lane mux        |
     | shift   | | (12,12)   | | s0/s1/s2    | | A:@1 D:@10   | | + DCMPRS pack   |
     +----+----+ +-----+-----+ +------+------+ +-------+------+ +--------+--------+
   s11      | @11      | @12         | @13          | @10/11(D)        | @12
   s12      |          v 2-cyc <- wvec acc (12,12) RMW, II=1 MAC chain v 2-cyc
   s13      |          (4 live chains max)         |                   |
            +----------+-------------+-------------+--------------------+
                                     |  STAGGERED 512-bit vec WRITE PORT
                                     v  { @10 load | @11 ALU | @12 MAC/perm/divstep | @13 FMA/cvt }
   s14   FSR sticky-flag accumulate {Invalid/DivZero/Overflow/Underflow/Inexact} (SHARED_OR)
   s15   deferred VectorPipeImpreciseErr post (status, not data)

   side gate (early): s3 CPENABLE(cp1) --clear--> Coprocessor1Exception (precise squash)

  DATA SIDE: local DataRAM (4-bank, 512-b, @5 scalar/@9 vec) + 8-entry write buffer; NO D-cache.
             AXI/ACE-Lite master -> system RAM (1 GiB @0x100000) / SBUF (axi2sram, 1/8 TDM slot).
             PSUM: NO aperture, NO NX window  ==>  STRUCTURALLY UNREACHABLE.

  SIDE FILES:  vec 512x32 (@10 read) | wvec 1536x4 (@12 RMW) | vbool 64x16 | valign 512x4
               gvr 512x8 (gather gsr + FSR/FCR CSR) | b32_pr 64x16 (MAC radix) | AR 32x64 | BR 1x16
```

**Key connectivity facts** `[HIGH × OBSERVED]`: one unified stage-10 `vec` read port feeds all five
clusters (the MAC's wide `wvec` accumulator is a separate @12 file, never an 11th @10 read); writes
stagger across 10/11/12/13 (anti-port-pressure); the `wvec` `(12,12)` RMW is the only same-stage
RMW file; the forwarding network bridges every compute write stage back to @10 (`dep-lat = wstage −
10`) + the `wvec` @12→@12 self-forward; there is **no** fast path from a vector write stage to the
early scalar AR read — cross-pipe vec→scalar transfers eat the full pipe-depth gap (~8 cycles).

---

## 8. The unified-view reconciliation ledger

This is where the sibling pages' legitimate framing differences are resolved into ONE model. Each row
states the unified claim, the two source framings, and the binary arbiter.

| # | unified claim | framing A | framing B | binary arbiter / verdict |
|---:|---|---|---|---|
| 1 | **Reservation model = scoreboard (operand availability) + mem-port + slot-count; bodies PRESENT, finer stall counts MED** | [pipeline-timing §6]: "empty wall, MODULE_SCHEDULE empty, ceiling unprovable" | [co-issue-matrix §4]: "present — 2149 issue + ~160k stage + 1746 stall" | `nm`: 2149 issue / 1746 stall / ~160k stage. The empty `MODULE_SCHEDULE` is the *TIE-DB* table; the populated `_issue` stage bodies in `libcas-core.so` ARE the reservation. **UNIFIED** — realizable model = availability + mem-port + slot-count; exact cycle counts `[MED]`. |
| 2 | **Stage convention `A1/B3/E4/M5/W6` (ISS); subtract 1 on E/M for TIE** | ISS config `A1/B3/E4/M5/W6/D9` | TIE-root `r0/e3/m4/w6` | W=6 agreed; ISS E/M = TIE E/M + 1. **UNIFIED** — this page uses ISS stamps (the convention the latency stamps actually use). |
| 3 | **Pipe stages: int-MAC @12, FMA @13, TRUNC @12, vec read @10, store commit @11, load valign @9 (hazard) / @10/@12 (file-port), vec-ALU/vbool @11** | hazard model (valign @9) | file-port model (valign @10/@12) | both OBSERVED; @9 = forwarding-availability one stage ahead of the @10 file read. **UNIFIED** — both stage views cited, reconciled. |
| 4 | **FP-FMA is NOT S3-exclusive; bound = `1×S2 + 1×S3`** | prior "MADD only S3, S2 zero MADD" | corrected "madd.s also rides S2 (F2/F7/N1)" | `nm 'Opcode_madd_s_Slot'` = s2_mul ×3 + s3_alu ×4. **UNIFIED** — distinct ports, overlapping eligibility; S2-FMA excludes a same-bundle int-MAC. |
| 5 | **Integer MAC roster = 188 = 65 signed + 123 mixed; 24 FP → B17/B18** | loose "212 = 71 + 141" | corrected "188 integer (65+123) + 24 FP reclaimed" | [B04]/[B05] classifier over `nm Opcode_ivp_mul*`. **UNIFIED** — 188 integer / 24 FP; the old 71/141/212 left the 24 FP inside the integer count. |
| 6 | **`libcas-core.so` not stripped, full `.symtab`, NO DWARF — symbol-table model** | "unstripped" | "no DWARF, 177 936 / 178 959 symtab" | `readelf -SW` = 0 `.debug_*`; `nm` total = 179 079 (text/filtered counts give the lower figures). **UNIFIED** — symbol-table model; cite 179 079 total. |

**Disagreements remaining: NONE that affect the cycle-approximate model.** The one residual is the
**MED tier** (DataRAM 4-bank conflict timing, the I-cache miss penalty, the SBUF-AXI round-trip) —
structure pinned, exact cycle counts SoC-fabric-dependent or buried in the ~160 k stage bodies,
flagged inline (`*` in §5.3), never invented.

---

## 9. Adversarial self-verification — the 5 strongest synthesis claims

Each strongest claim, its committed source page(s), and the binary spot-check where a divergence was
re-grounded.

1. **The vector pipe reads @10 and writes 11/12/13 by latency class; dep-lat = `result@ − 10`
   exactly; the `wvec` accumulator is a same-stage `(12,12)` RMW sustaining `II=1`.** Sources:
   [pipeline-timing §2.2/§3], [regfile-ports §5], [simd-datapath §3.5]. Spot-check:
   `my_wvec_2_opnd_ivp_sem_multiply_{wvt,wvu}_{use,def}` all resolve (`nm`). **VERDICT:
   STANDS `[HIGH × OBSERVED]`.**

2. **FP-FMA rides `S2_Mul` OR `S3_ALU` (not S3-exclusive); co-issue ceiling = `1 int-MAC (S2) + 1
   FP-FMA (S3)`; integer quad-MAC is S2-exclusive.** Sources: [co-issue-matrix §2.3], [regfile-ports
   §3], [vfpu-ieee §5], [simd-datapath §4.3]. Spot-check: `Opcode_madd_s_Slot` = s2_mul ×3 +
   s3_alu ×4; `Opcode_ivp_mulqa*` outside s2_mul = ∅; `F4_S2_Mul = 61`. **VERDICT: STANDS — corrects
   the prior S3-exclusive framing `[HIGH × OBSERVED]`.**

3. **The reservation/timing model is PRESENT in `libcas-core.so` (2149 issue / 1746 stall / ~160k
   stage), not an empty wall; the realizable structural bound is availability + 2 mem-ports +
   slot-count.** Sources: [co-issue-matrix §4] (corrects [pipeline-timing §6]). Spot-check:
   `nm -c` → issue 2149, stall 1746, stage ~159 937; `nx_{Load_0,Load_1,Store_0}_interface` present,
   no `Store_1`. **VERDICT: STANDS — resolves divergence #1; stall = 1746, not 1651 `[HIGH ×
   OBSERVED]`.**

4. **No native multiplier — Booth + partial-product + carry-save Wallace tree (named modules); the
   1536-bit `wvec` = 3×512 re-partitioned i8→24 / i16→48 / i32→96.** Sources: [simd-datapath §3],
   [register-files.md]. Anchors: `module_ivp_booth_enc_{8,16}`, `module_ivp_su_xtmulpp_8x8`, 11×
   `module_ivp_comp_*`, `mulpp{12x12,25x25}` (zero host `imul` in the device modules); `wvec` =
   1536 b × 4 in `regfiles[]`. **VERDICT: STANDS `[HIGH × OBSERVED structure; INFERRED tree wiring]`.**

5. **PSUM is structurally unreachable (no AXI aperture, no NX window); SBUF reached only as
   memory-mapped AXI through `axi2sram` (the 1/8 TDM DMA slot); no dedicated compute port; no
   D-cache.** Sources: [lsu-memory §4.3], config-reference-sheet §7. Anchors: `STATE_BUF`/`PSUM_BUF`
   constants (sunda header), `DCACHE_SIZE = 0`, the AXI-master fault handlers. **VERDICT: STANDS
   `[HIGH × OBSERVED config; MED × the off-core arbiter cycle add]`.**

All five cite a committed source page; #2, #3, #4 were additionally re-grounded against the binary
(the three divergences where two sibling pages disagreed). No unsupported claim remains.

---

## 10. Confidence ledger

**HIGH / OBSERVED (carried from device-disasm-validated / header-exact / `nm`-exact sources):** the
config census (§1), the 14-format/46-slot decode + the 46-SLOTDEF census + the 12569 placement count
(§2.1), the class-exclusive lane binding (§2.2), the 2-pipe stage structure + the per-op stage stamps
(§2.3/§5), the reservation-bodies-present fact (§2.4: 2149/1746/~160k), the FCR/FSR field table + the
single-rounding FMA (§3.2), the no-native-MUL named-module tree + the `wvec` `(12,12)` RMW (§3.3), the
Engine-A seed-LUT byte content + the Engine-B PWP format (§3.4), the 2-LSU split + valign + the address
map + SBUF-AXI / no-PSUM (§3.5/§4), the atomic/ordering model (§4.4), the per-class latency table
(§5), the gen invariance (§6).

**HIGH / INFERRED (block topology grounded on OBSERVED ports/stages/values; RTL not in corpus):** the
compute-datapath box-to-box wiring (§3.1/§7), the carry-save tree wiring (§3.3), the 2×FMAC dual-issue
micro-binding (§3.2/§5.1), the peak-compute bundle composition (§5.4).

**MED:** the SoC-fabric latency adds — I-cache miss penalty, SBUF-AXI round-trip, DataRAM 4-bank
conflict timing (the `*` rows, §5.3); the exact per-port single-issue stall cycle counts inside the
~160 k populated stage bodies (recoverable in principle, a task not a wall); the AXI-master
read-outstanding depth (not a config constant); the `gvr` flag-bit `0x08` semantics.

**LOW / NOT CLAIMED:** the host-supplied ACT PWP coefficient CONTENT (the wall, §3.4); the physical
port/PE counts (vs the worst-case demand derived in [regfile-ports §3]); the QLI refine polynomial
interior (the FW-42 wall, §3.4).

---

## 11. Cross-references

* [Config-Grounded Microarch Reference Sheet](../isa/core/config-reference-sheet.md) — the
  config-grounded number sheet this page is the narrative companion to.
* [Pipeline Timing Model](pipeline-timing.md) · [FLIX Co-Issue Matrix](co-issue-matrix.md) ·
  [Register-File Port Model + Bypass Network](regfile-ports.md) — the stage/co-issue/port substrate
  behind §2 / §5 / §7 (and the source of divergences #1–#3).
* [The SIMD Compute-Datapath](simd-datapath.md) · [The VFPU / IEEE-754 Exception Model](vfpu-ieee.md)
  · [Activation + Transcendental Table Engine](activation-transcendental-tables.md) — the datapath
  microarch behind §3 (and divergences #4–#5).
* [Local-Memory / System-Bus / LSU Model](lsu-memory.md) · [Atomics + Ordering Model](atomics-ordering.md)
  — the memory hierarchy + consistency model behind §4.
* [Boot / Reset Sequence](boot-reset.md) · [Clock / Reset / Power](clock-reset-power.md) ·
  [HW Revision Markers](rev-markers.md) — the boot spine, clock/power, and version pinning behind
  §1 / §6.
* [NCFW Scalar-LX Core](ncfw-lx-core.md) — the *separate* management core (NOT this microarch).
* [The Eight Register Files](../isa/core/register-files.md) · [The FLIX VLIW Encoding](../isa/core/flix-encoding.md)
  · [ISA Coverage Tally](../isa/core/coverage-tally.md) — the Part-2 core pages this synthesis builds on.

---

*Provenance: this chapter consolidates the twelve Part-4 microarchitecture pages plus the committed
Part-2 core pages into ONE cycle-approximate model. Every fact is CARRIED from a committed source
page (cited inline); the six cross-page divergences (§8) were re-grounded against the binary
(`libcas-core.so` / `libisa-core.so` `nm`/`readelf`/`objdump`), with the binary as arbiter.
The shipped artifacts are the `ncore2gp` config headers, the de-ciphered TIE-XML / TIE-generated ISA
DB, the cycle ISS schedule model, the host value oracle, and the device assembler/disassembler
(`XTENSA_CORE=ncore2gp`). The RTL is NOT in the corpus; all block topology is INFERRED from OBSERVED
ports/stages/widths/values, tagged per claim. Lawful interoperability reverse engineering (DMCA 17
U.S.C. 1201(f)); no vendor source tree was referenced/consulted/quoted — every fact reads as derived
from shipped-binary + shipped-public-header + device-disassembler static analysis alone. Section
deltas (`.data`/`.data.rel.ro` = `0x200000`), vtable vptr = `_ZTV+0x10`, and the gitignored
`extracted/` tree are confirmed per the GOTCHA at the head of the page.*
