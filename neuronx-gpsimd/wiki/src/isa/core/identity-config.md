# Core Identity & Configuration

This is the authoritative byte-pinned **core-identity** reference: the exact set of
configuration constants a reimplementer pins their core to so that the objects it produces
and the firmware it accepts match the shipped GPSIMD. Every value below is read **directly
from a shipped file this session** — the auto-generated processor-generator parameter file
`ncore2gp-params`, the HAL config header `core-isa.h`, the version-map header
`xtensa-versions.h`, or a symbol/byte in one of the config binaries (`libisa-core.so`,
`libnrtucode_internal.so`). Where a constant is a struct field, the offset and symbol are
given. Where prompt-level folklore disagrees with the binary, **the binary wins**.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string/define read from a shipped artifact; `INFERRED` = reasoned over
OBSERVED facts; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but
real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading),
**NOTE** (orienting context).

Two corpus facts govern every anchor: the device config libraries carry a full `.symtab`
(use `nm`) but **no DWARF**; and the `.data` VMA↔file-offset delta is **per-binary** — for
`libisa-core.so` it is `0x200000` (`readelf -SW`: `.data` VMA `0x764040` ↔ file `0x564040`),
so a `.data`-resident table like `regfiles` @ VMA `0x74a800` is read at file offset
`0x54a800`. `.text`/`.rodata` are VMA == file-offset.

---

## 1. The identity card — pin these or you are not building this core

These are the constants the rest of the toolchain validates against. The first three lines
of the identity (CoreID / ConfigName / uarch) plus the ConfigID word are what the loader and
the disassembler key on; pin them and every device object resolves, change one and the
`xtensa-elf-objdump` registry no longer recognises the core.

| Field | Value | Read from (file · line/symbol) | Confidence |
|---|---|---|---|
| **CoreID** | `ncore2gp` | `ncore2gp/build.info` (`CoreID ncore2gp`); the sole core in the `xtensa-elf-objdump` registry | `[HIGH/OBSERVED]` |
| **ConfigName** | `Xm_ncore2gp` | `ncore2gp-params:22` (`ConfigName = Xm_ncore2gp`) | `[HIGH/OBSERVED]` |
| **uarchName** | `Cairo` | `ncore2gp-params:25` (`uarchName = Cairo`); `isUarchCairo=1` in `core.yml` | `[HIGH/OBSERVED]` |
| **arch (ISA family)** | `Xtensa24` | `ncore2gp-params:24` (`arch = Xtensa24`); enum `{Xtensa24, RiscV}` in `core.xparm:2637` | `[HIGH/OBSERVED]` |
| **Exception arch** | **XEA3** | `core-isa.h:719` `XCHAL_XEA_VERSION 3`; `:726` `XCHAL_HAVE_XEA3 1` (`HAVE_XEA1/2/5 = 0`) | `[HIGH/OBSERVED]` |
| **TargetHWVersion** | `NX1.1.4` (= `LX7.1.4`) | `ncore2gp-params:21`; `core-isa.h:260` `XCHAL_HW_VERSION_NAME "NX1.1.4"` | `[HIGH/OBSERVED]` |
| **HW micro-arch (int)** | `281040` (= HW **RI-2020.4**) | `ncore2gp-params:19,20`; `core-isa.h:264` `XCHAL_HW_VERSION 281040` | `[HIGH/OBSERVED]` |
| **HW accept window** | `281040 .. 281040` (**zero-width**) | `ncore2gp-params:19,20` `HWMicroArchLatest == HWMicroArchEarliest`; `core-isa.h:273,277` `MIN == MAX` | `[HIGH/OBSERVED]` |
| **ConfigID0** | `0xC4019686` | `ncore2gp-params:110`; `core-isa.h:258` `XCHAL_HW_CONFIGID0` | `[HIGH/OBSERVED]` |
| **ConfigID1** | `0x2908E4E3` | `ncore2gp-params:111`; `core-isa.h:259` `XCHAL_HW_CONFIGID1` | `[HIGH/OBSERVED]` |
| **ConfigKey0 / Key1** | `0x5FA7C9E6` / `0xB2AEBB83` | `ncore2gp-params:543,544` | `[HIGH/OBSERVED]` |
| **SW tools release** | `RI-2022.9` = `14.09` = `1409000` | `ncore2gp-params:16,17,18`; `core-isa.h:242` `XCHAL_SW_VERSION 1409000` | `[HIGH/OBSERVED]` |
| **Customer ID** | `19270` | `ncore2gp-params:3` (header comment); `core-isa.h` header | `[HIGH/OBSERVED]` |
| **BuildUniqueID** | `795646` = `0xc23fe` | `ncore2gp-params:541`; `build.info`; header `Build=0xc23fe` (`0xc23fe == 795646`) | `[HIGH/OBSERVED]` |
| **BuildMode** | `Evaluation` | `ncore2gp-params:542` | `[HIGH/OBSERVED]` |
| **SW_ABI** | `windowed` | `ncore2gp-params:525` | `[HIGH/OBSERVED]` |
| **SW_FloatingPointABI** | `1` | `ncore2gp-params:526` | `[HIGH/OBSERVED]` |
| **Vision type** | **Q7** (`XCHAL_VISION_TYPE = 7`) | `core-isa.h:208`; `vq7_isa=1` in `core.xparm:3638` | `[HIGH/OBSERVED]` |
| **Coprocessor count** | `7` | `ncore2gp-params:98` `IsaCoprocessorCount = 7`; `core-isa.h:94` `XCHAL_CP_MAXCFG 7` | `[HIGH/OBSERVED]` |
| **Register files** | `8` | `num_regfiles` @ `0x3b5c20` in `libisa-core.so` = `mov $0x8,%eax` | `[HIGH/OBSERVED]` |
| **FLIX issue grid** | 14 formats / 46 slots | `num_formats` @ `0x3b65e0` = `0xe`; `num_slots` @ `0x3b6510` = `0x2e` | `[HIGH/OBSERVED]` |

> **NOTE — three names, one core.** `ncore2gp` is the Tensilica **CoreID** (the registry
> handle a reimplementer passes as `XTENSA_CORE=ncore2gp`); `Xm_ncore2gp` is the full
> **ConfigName** (the `Xm_` prefix is Tensilica's configuration-management tag); `Cairo` is
> the **uarchName** (the microarchitecture codename). They name the same configured core at
> three layers. The literal type-hierarchy chain in `core.yml` is
> `Processor → Processor_Cairo → xpg_ncore2gp` — i.e. the `xpg_ncore2gp` config *is-a*
> Processor_Cairo. Do **not** confuse `Cairo` with the AWS silicon codenames
> (SUNDA/CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK): those live in the `NRTUCODE_CORE_*` enum and
> the `*.c` source strings, a different namespace. The correct framing is "the Cairo core
> *inside* the CAYMAN SoC", never "Cairo == CAYMAN".

The verbatim identity block from `ncore2gp-params` (the single source of truth), with the
header customer/build stamp:

```
# Customer ID=19270; Build=0xc23fe; Copyright (c) 2004-2018 Tensilica Inc. ALL RIGHTS RESERVED.   (line 3)
SWToolsRelease     = RI-2022.9      (16)
SWToolsVername     = 14.09          (17)
SWToolsVersion     = 1409000        (18)
HWMicroArchLatest  = 281040         (19)
HWMicroArchEarliest= 281040         (20)   <- MIN == MAX, a zero-width accept window
TargetHWVersion    = NX1.1.4        (21)
ConfigName         = Xm_ncore2gp    (22)
arch               = Xtensa24       (24)
uarchName          = Cairo          (25)
HasVectorPipe      = 1              (26)
HasXNNE            = 0              (27)
IsaMaxInstructionSize = 32          (31)
IsaCoprocessorCount   = 7           (98)
HWConfigID0        = 0xC4019686     (110)
HWConfigID1        = 0x2908E4E3     (111)
LoadStoreWidth     = 512            (314)
SW_ABI             = windowed       (525)
SW_FloatingPointABI= 1              (526)
BuildUniqueID      = 795646         (541)
BuildMode          = Evaluation     (542)
ConfigKey0         = 0x5FA7C9E6     (543)
ConfigKey1         = 0xB2AEBB83     (544)
```

`ConfigID 0xC4019686 / 0x2908E4E3` is the most operationally meaningful identity in the
file: it is the word the loader validates against the running core, and `core-isa.h:268`
asserts `XCHAL_HW_CONFIGID_RELIABLE 1` — the silicon reports it and the runtime can trust
it. The same two words appear in `core.xparm:3546` as `TargetHWConfigID0/1` and are
re-emitted into every device ELF's config-check, so a mismatched ConfigID is the canonical
"wrong core" failure.

> **QUIRK — the params header says 2004–2018, the LSP specs say 2001–2015, the HAL header
> says 1999–2025.** All three copyright vintages are genuine binary stamps of different
> generator components (the `*-params` writer, the LSP `specs` writer, the `core-isa.h`
> generator), not a contradiction. Pin the *values*, not the copyright year.

---

## 2. The zero-width HW-accept window — `MIN == MAX == 281040`

The single most decisive fact in the whole config is that the hardware-version accept window
has **width zero**: the software targets **exactly one** HW revision, with no range.

```
ncore2gp-params:  HWMicroArchLatest = 281040   HWMicroArchEarliest = 281040
core-isa.h:       XCHAL_HW_MIN_VERSION = 281040   XCHAL_HW_MAX_VERSION = 281040
                  (MAJOR 2810, MINOR 4, MICRO 0 — earliest == latest)
```

A normal Xtensa config carries a *range* `[earliest, latest]` of HW point-releases it will
run on; this one collapses both bounds onto the single value `281040`. The encoding rule is
in the `core-isa.h:264` comment — `version = major*100 + (major<2810 ? minor : minor*10 +
micro)`, so `2810*100 + (4*10 + 0) = 281040`. A reimplementer reads this as: **the GPSIMD
firmware is built for one frozen hardware revision and will not load against any other**;
there is no forward/backward HW-version slack to exploit, and a loader that finds a different
HW number must reject, not down-clock to the nearest supported point.

> **NOTE — this is *not* the same as gen-invariance.** A zero-width *HW-accept* window
> (281040, one Cairo IP rev) is orthogonal to the *generation* axis (SUNDA…MAVERICK). All
> four shipped generations run firmware built for this *same* 281040 Cairo core — that is the
> gen-invariance result (§5), not a contradiction of the zero-width window. One core revision,
> instantiated inside five different SoC envelopes.

---

## 3. The two "RI" axes — hardware RI-2020.4 ≠ software-tools RI-2022.9

The biggest naming trap is that "RI-20xx" is overloaded: Cadence stamps a **hardware**
micro-architecture release **and** a **software-tools** release with the same `RI_YYYY_N`
scheme, and for this core they land on different years. The decoder is the shipped
`xtensa-versions.h`:

```c
#define XTENSA_HWVERSION_RI_2020_4   281040    /* versions NX1.1.4, LX7.1.4 */   // line 317
#define XTENSA_HWVERSION_RI_2022_9   281090    /* versions NX1.1.9, LX7.1.9 */   // line 338
#define XTENSA_SWVERSION_RI_2020_4   1404000   /* versions 14.04 */              // line 430
#define XTENSA_SWVERSION_RI_2022_9   1409000   /* versions 14.09 */              // line 437
#define XTENSA_SWVERSION             XTENSA_SWVERSION_RI_2022_9                  // line 519
```

Reading `ncore2gp-params` against this header pins both axes precisely and **distinctly**:

| Axis | Param key (value) | Header symbol | "RI" label | Confidence |
|---|---|---|---|---|
| **Hardware** | `HWMicroArchLatest = 281040`, `TargetHWVersion = NX1.1.4` | `XTENSA_HWVERSION_RI_2020_4 = 281040` | HW = **RI-2020.4** | `[HIGH/OBSERVED]` |
| **Software tools** | `SWToolsRelease = RI-2022.9`, `SWToolsVername = 14.09`, `SWToolsVersion = 1409000` | `XTENSA_SWVERSION_RI_2022_9 = 1409000` | tools = **RI-2022.9** | `[HIGH/OBSERVED]` |

> **GOTCHA — `RI-2020.4` names *both* a HW number and a SW number; the config uses only the
> HW one.** `versions.h:430` also defines `XTENSA_SWVERSION_RI_2020_4 = 1404000` (SW "14.04").
> So "RI-2020.4" is ambiguous between HW `281040` and SW `1404000`. This config uses the HW
> sense (`281040 = NX1.1.4`) and a *different, later* SW release (`14.09 = RI-2022.9 =
> 1409000`). Statements "the hardware is RI-2020.4" and "the toolchain is RI-2022.9" are
> **both true and not contradictory** — they name different axes. A 2022 SW release building
> a config for a 2020 HW point is ordinary back-compat. Never collapse the two; the core does
> **not** ship the SW `14.04 / RI-2020.4` tools.

The three equivalent HW names — `NX1.1.4`, `LX7.1.4`, `RI-2020.4` — are all `281040`: NX is
the family name, LX7 the legacy alias (`versions.h:317` lists both on the same line),
RI-2020.4 the hardware release. `core-isa.h` carries the release-flag triple
`XCHAL_HW_REL_NX1 = 1`, `XCHAL_HW_REL_NX1_1 = 1`, `XCHAL_HW_REL_NX1_1_4 = 1` (lines 265–267),
the only `XCHAL_HW_REL_*` defines set — confirming exactly the `NX1.1.4` point.

The full toolchain (Clang-10 / binutils 2.34 / the LSPs / the FlexLM gate / the SDK package
versions) is the subject of [Toolchain Inventory & Versions](../../reference/toolchain-versions.md);
this page owns only the *core-identity* slice of it.

---

## 4. The ISA-family + datapath identity — `Xtensa24` base, Vision-Q7 NX coprocessor, XEA3

Three orthogonal axes define what the core *is*, beyond its name:

**`Xtensa24` is the ISA *family*, not a year.** `core.xparm:2637` declares the legal values
of the `arch` field as the two-member enum `{Xtensa24, RiscV}` — so `arch = Xtensa24` selects
the Xtensa (24-bit core encoding) ISA family over the RISC-V family. It is **not** a 2024
toolchain and **not** a config version; it carries no version number and is absent from
`versions.h`. Both the Xtensa LX and NX product lines are "Xtensa24" at this layer; the
LX-vs-NX distinction is separate (this core is **NX** — `vq7_isa=1`, `lx_wide_loops=0` in
`core.xparm:3638`).

**The datapath is the Vision-Q7 NX SIMD coprocessor.** The Q7 binding is byte-grounded in
`core-isa.h`:

```c
XCHAL_HAVE_VISION              1     // line 206  (comment text "Vision P5/P6" is template lag)
XCHAL_VISION_SIMD16            32    // line 207  -> 32 × 16-bit lanes = 512-bit SIMD
XCHAL_VISION_TYPE             7      // line 208  -> Q7  (the numeric type, authoritative)
XCHAL_HAVE_VISION_HISTOGRAM    1     // line 210
XCHAL_HAVE_VISION_DP_VFPU      0     // line 211  -> no double-precision VFPU
XCHAL_HAVE_VISION_SP_VFPU      1     // line 212
XCHAL_HAVE_VISION_SP_VFPU_2XFMAC 1   // line 213  -> the Q7-specific 2×FMAC option
XCHAL_HAVE_VISION_HP_VFPU      1     // line 214
XCHAL_HAVE_VISION_HP_VFPU_2XFMAC 1   // line 215  -> the Q7-specific 2×FMAC option
```

> **CORRECTION — the comment text says "Vision P5/P6", the core is Q7.** Several Vision
> defines in `core-isa.h` are *commented* "Vision P5/P6" while the numeric
> `XCHAL_VISION_TYPE = 7` and the Q7-only `*_2XFMAC` options say Q7. This is template-comment
> lag in the 14.09 header generator, **not** a config mismatch. The numeric type + the Q7
> option flags (`sp_vfpu_2xfma`, `hp_vfpu_2xfma`, `dp_vfpu = 0`) are the Q7-class profile;
> trust the numbers, not the comment.

**The exception model is XEA3.** `core-isa.h:719` `XCHAL_XEA_VERSION 3`, `:726`
`XCHAL_HAVE_XEA3 1` (XEA1/2/5 all `0`). The XEA3 spine: relocatable vectors
(`XCHAL_HAVE_VECBASE 1`, line 735) with **reset PC = VECBASE = `0x00000000`**
(`XCHAL_VECBASE_RESET_VADDR 0x0`, line 736); 37 interrupts across 7 levels
(`XCHAL_NUM_INTERRUPTS 37`, line 457; `XCHAL_NUM_INTLEVELS 7`, line 460); no halt/bootloader
architecture (`XCHAL_HAVE_HALT 0`, `XCHAL_HAVE_BOOTLOADER 0`, lines 731–732).

> **GOTCHA — "37-entry cause table" is the interrupt count, not a byte-anchored cause-table
> size.** `XCHAL_NUM_INTERRUPTS = 37` is a real OBSERVED define, but it is the *interrupt
> count*; the corpus does **not** anchor a literal `37`-entry XEA3 *cause table*. The firm
> XEA3 anchors are `XCHAL_XEA_VERSION = 3`, the single relocatable `VECBASE`, and reset
> `PC = 0x0`. State the interrupt count as `37`; do not over-claim a "37-entry cause table".

> **GOTCHA — XEA3 (Q7) ≠ XEA2 (NCFW). Two cores, two exception models.** The *Vision-Q7
> Cairo* datapath core documented here is **XEA3** (`XCHAL_XEA_VERSION 3`). The **separate**
> scalar **Xtensa-LX** NCFW management core (a different core entirely, with no config in the
> corpus) is **XEA2** — its windowed-overflow handler and `entry/retw.n` ABI are the classic
> windowed XEA2 model. They *share* the windowed base ISA (so base ops decode under either),
> which is exactly why a reimplementer must keep them apart: do not apply the Q7 FLIX/XEA3
> facts to NCFW. See [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md).

---

## 5. Which config constants are gen-invariant, which scale per generation

The five GPSIMD product generations are **not five processors** — they are this *one* Cairo
core instantiated inside per-generation SoC envelopes. The core-identity constants split
cleanly into a **gen-invariant** block (the same byte across SUNDA…MARIANA_PLUS, and the
identity surface of MAVERICK) and a small **gen-scaling** envelope. A reimplementer builds
the invariant core *once*.

**Gen-invariant (pin once, reuse across all generations):** the entire identity card in §1.
There is exactly **one** `core-isa.h`, **one** real `ncore2gp-params` (plus its byte-identical
`default-params` alias), **one** `ConfigName`, **one** `uarchName`, **one** HW revision —
`fd --no-ignore` finds no second Xtensa core config of any kind in the corpus. Corroboration:
every shipped-generation EXTISA Q7 blob carries the **same** toolchain comment —
`strings libnrtucode_internal.so | rg 'XtensaTools-14.09 clang version 10.0.1'` is present for
the four shipped gens — and the same Xtensa core/ABI `e_flags 0x300`. One core, one toolchain,
one ABI across the four shipped generations. `[HIGH/OBSERVED]`

**Gen-scaling (the bounded parameter vector):** the axes that *do* move are the
**`coretype`/`arch_id` identity slot** (this section), the per-generation opcode/dtype/ALU
brackets, the EXTISA blob set, the on-chip collective uplift, and the transport/memory IP —
the full ledger is the [Gen-Invariance Thesis](../../orientation/gen-invariance.md). The one
gen-scaling axis that is itself a *core-identity* datum is the `coretype`/`arch_id` pair:

| Codename | NC-ver | `coretype` `[OBSERVED]` | `arch_id` (`= ct − 1`) | shipped? |
|---|---|---|---|---|
| **SUNDA** | v2 | **6** | `0x05` (5) | yes |
| **CAYMAN** | v3 | **13** | `0x0c` (12) | yes |
| **MARIANA** | v4 | **21** | `0x14` (20) | yes |
| **MARIANA_PLUS** | v4+ | **29** | `0x1c` (28) | yes |
| **MAVERICK** | v5 | **37** `[OBSERVED]` | `0x24`/36\* `[INFERRED]` | internal twin only |

The five `coretype` constants `{6, 13, 21, 29, 37}` are literal in
`libnrtucode_internal.so`: `nrtucode_core_get_coretype` @ `0x9b0a10` (`T`, exported) and the
`nrtucode_get_ext_isa_internal` switch @ `0x9b2b30` (`t`, local). The resolver
`nrtucode_get_num_ext_isa_libs` @ `0x9b2c90` bounds the space with `cmp $0x25,%edi` (37) +
`movabs $0x2020202000,%rcx` (a bitmask with bit 37 set) and `cmp $0x6,%rdx` (6) — both
verified by disassembly this session.

> **CORRECTION — the `coretype` stride is *not* a flat +8, and `arch_id = coretype − 1` is
> the only uniform relation.** The set `{6, 13, 21, 29, 37}` steps **+7 (6→13) then +8, +8,
> +8** — neither axis has a uniform +8 stride. The one relation that holds uniformly is
> `arch_id = coretype − 1`, and it is that `−1` (never a "+8 read off the coretype axis") that
> extends the unshipped MAVERICK `arch_id` to `36`. `coretype 37` is **OBSERVED** (anchored
> two ways — the `NRTUCODE_CORE_*` enum ordinal and the twin resolver bitmasks); `arch_id 36*`
> is **INFERRED** (there is no `cmp $0x24` anywhere and no v5 NCFW image). Carry the `36*`
> caveat; never present it as binary-observed. The full proof is the
> [Codename ↔ Generation Cross-Walk](../../reference/codename-crosswalk.md).

> **NOTE — MAVERICK is a *rebuild*, not a *redesign*, and its core config does not ship.** The
> four MAVERICK Q7 blobs carry a different toolchain comment —
> `XtensaTools-15.05 clang version 15.0.7` (vs `14.09 / clang-10` for the four shipped gens) —
> and ship ET_DYN/PIC, in the internal twin only. Whether MAVERICK's core is *still* NX1.1.4 /
> Cairo (recompiled) or a newer NX revision is **not observable** from this corpus — its core
> config (`-params` / `core-isa.h`) is not present. The toolchain bump is OBSERVED; a HW-rev
> bump is plausible but **not proven** (`[LOW/OPEN]`).

---

## 6. The config knobs that pin a reimplementation

Beyond the identity strings, these are the datapath/ABI knobs a reimplementer must match for
the objects and the disassembler to agree. All are read directly from `ncore2gp-params` /
`core-isa.h` / `libisa-core.so` this session.

| Knob | Value | Anchor | Why it pins the reimplementation |
|---|---|---|---|
| **Vector width** | **512-bit** SIMD (32 × 16-bit lanes) | `LoadStoreWidth = 512` (`params:314`); `XCHAL_DATA_WIDTH 64` bytes (`core-isa.h:229`); `vec` regfile width `512`/count `32` in `regfiles` (§7) | the SIMD lane count every Vision op assumes; the gather/scatter unit moves `GS_ElementsPerCycle = 32` per cycle |
| **FLIX issue** | **14 formats / 46 slots**, max instr 32 B | `num_formats` @ `0x3b65e0` = `0xe`; `num_slots` @ `0x3b6510` = `0x2e`; `IsaMaxInstructionSize = 32` (`params:31`); `XCHAL_MAX_INSTRUCTION_SIZE 32` (`core-isa.h:53`); `XCHAL_INST_FETCH_WIDTH 32` (`:228`) | the VLIW bundle scheme — get the format/slot grid wrong and bundles desync. Full decoder: [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) |
| **8 register files** | 2 core (`AR`,`BR`) + 6 Vision (`vec`,`vbool`,`valign`,`wvec`,`b32_pr`,`gvr`) | `num_regfiles` @ `0x3b5c20` = `0x8`; the `regfiles` table @ `0x74a800` (§7) | the entire visible register state + the windowed `AR` ABI. Deep page: [The Eight Register Files](register-files.md) |
| **Coprocessor set** | **7** enable slots, **1** populated (`Vision`) | `IsaCoprocessorCount = 7` (`params:98`); `XCHAL_CP_MAXCFG 7` (`core-isa.h:94`); `num_coprocs` @ `0x3b6dc0` = `0x1` (`{name="Vision", number=1}`) | the `-mcoproc` build flag enables the vector pipe; `CPENABLE` is 7-bit but only Vision is realised. The two TIE modules are `vision.tie` + `mul32.tie` (`config.cf:43`) |
| **Functional units** | 1 (`XT_LOADSTORE_UNIT`, `num_copies = 2`) | `num_funcUnits` @ `0x3b5bd0` = `0x1`; `LoadStoreUnitsCount = 2` (`params`) | two dual-copy LSUs; ALU/MUL are FLIX slots, not funcUnit objects |
| **Windowed ABI** | windowed, 64 physical AR | `SW_ABI = windowed` (`params:525`); `XCHAL_HAVE_WINDOWED 1` (`core-isa.h:50`); `XCHAL_NUM_AREGS 64` (`:51`); `num_aregs = 64` (`params:581`) | the `entry`/`callx8`/`retw.n` calling convention every custom-op object uses |
| **Density + loops + L32R** | all on | `XCHAL_HAVE_DENSITY 1`, `XCHAL_HAVE_LOOPS 1` (`core-isa.h:55,56`) | 16-bit narrow ops + zero-overhead loops are in the decode set |
| **Int options** | MUL16=1, MUL32=1, **MAC16=0**, BOOLEANS=1 | `core-isa.h:63,64,95,92`; `params:45,46,79` | MAC16 is **off** — a reimplementer must not emit `mula.*`; the int multiply is the MUL16/MUL32 path |
| **FP** | single-precision (SPFPU) on, **double-precision off** | `XCHAL_HAVE_FP 1` (`core-isa.h:136`); `XCHAL_HAVE_DFP 0` (`:141`); `SW_FloatingPointABI = 1` (`params:526`) | the FP sub-ISA is SP-only + the Q7 2×FMAC VFPU; no DFP. Deep page: [The FP Sub-ISA](fp-sub-isa.md) |
| **Gather/scatter** | 8 gather + 2 scatter regs, 32 elem/cyc | `GatherScatter = 1`, `GS_GatherRegs = 8`, `GS_ScatterRegs = 2`, `GS_ElementsPerCycle = 32` (`params`) | the SuperGather unit behind the gather/scatter/embedding lane |
| **Memory geometry** | IRAM 64 KB @ `0x0`, DataRAM 64 KB @ `0x80000`, 1 GB SysRAM @ `0x100000` | `core-isa.h` reset/RAM defines; `params` ISS RAM info | the device memory map a custom-op LSP links against. Deep page: [LSP Linker Specs + ELF Layout](../../abi/lsp-elf.md) |
| **Pipeline** | B/E/M/W = 3/4/5/6, vector D-stage 9 | `config.cf` pipe stages; `params` ISSPipe* | the timing model the cycle-accurate ISS uses |

> **NOTE — the build flags that turn these knobs on.** The custom-op build recipe
> (`build_custom_op.py`) selects this core not by a `-target` triple but by
> `--xtensa-core=ncore2gp --xtensa-system=<sys>`, and turns on the coprocessor set with
> `-mcoproc` and the windowed/far-call ABI with `-mlongcalls`. The objects it produces stamp
> `XtensaTools-14.09 clang version 10.0.1` into their `.comment`. The flag↔knob mapping is the
> [Toolchain Inventory](../../reference/toolchain-versions.md) §A.3.

---

## 7. The `regfiles` table — reading the 512-bit vector width from `.data`

The vector width is not a single define; it is a field in the `regfiles` table that
`libisa-core.so` exports. Reading it is the canonical demonstration of the `.data` offset
GOTCHA. The table is at symbol `regfiles` @ VMA `0x74a800` (`nm`: `d regfiles`); with the
`0x200000` `.data` delta it sits at file offset `0x54a800`. Each entry is 56 bytes:
`{ name_ptr (u64), short_ptr (u64), …, width (u32 @ +16), count (u32 @ +20), …,
default_ctype_idx (u32 @ +28), flags (u32 @ +36), … }`. The eight entries, read this session:

| idx | name | width (bits) | count | role |
|---|---|---|---|---|
| 0 | `AR` | 32 | 64 | scalar address/general registers (windowed) |
| 1 | `BR` | 1 | 16 | scalar boolean registers |
| 2 | **`vec`** | **512** | 32 | **the 512-bit SIMD vector file (32 × 16-bit lanes)** |
| 3 | `vbool` | 64 | 16 | per-lane SIMD predicate file |
| 4 | `valign` | 512 | 4 | alignment registers for unaligned vector loads |
| 5 | `wvec` | 1536 | 4 | wide MAC accumulators (quad-width readout) |
| 6 | `b32_pr` | 64 | 16 | 64-bit predicate/pack registers |
| 7 | `gvr` | 512 | 8 | global/state vector registers (`gsr`; flags `0x0d`) |

The `vec` entry's `width = 512`, `count = 32` is the byte-exact source of the "512-bit vector
width" claim, read straight from `.data` (not from a comment or a synthesised number). The
six SIMD files (idx 2–7) are the populated `Vision` coproc; the two core files (idx 0–1,
`coproc = ""`) are base Xtensa. Full per-file treatment: [The Eight Register Files](register-files.md).

---

## 8. C pseudocode — how the runtime reads and validates the config identity

Two decode/validate idioms a reimplementer reproduces. The first is the **ConfigID + HW
window** check the loader applies before accepting a device image; the second is the
**`coretype → arch_id`** resolution the runtime applies when it picks a per-generation EXTISA
library. Both are reconstructed from the binary (the constants are byte-pinned; the control
flow mirrors the `nrtucode_*` resolvers), not copied from any source.

```c
/* ---- the frozen Cairo / ncore2gp identity, byte-pinned from ncore2gp-params + core-isa.h ---- */
enum { NCORE2GP_HW_CONFIGID0 = 0xC4019686u,   /* params:110 / core-isa.h:258 */
       NCORE2GP_HW_CONFIGID1 = 0x2908E4E3u,   /* params:111 / core-isa.h:259 */
       NCORE2GP_HW_VERSION   = 281040 };      /* params:19,20 / core-isa.h:264 (NX1.1.4) */

/*
 * config_accept_image — gate a candidate Q7 device image against the frozen core identity.
 *
 * The accept window is ZERO-WIDTH (MIN == MAX == 281040): the firmware targets exactly one
 * HW revision, so a single-value equality is correct — NOT a range test. ConfigID is the
 * reliable identity word (XCHAL_HW_CONFIGID_RELIABLE == 1, core-isa.h:268).
 *
 * @param img_cfgid0/1   the ConfigID word the candidate image declares.
 * @param img_hw_version the HW micro-arch number the candidate declares.
 * @return  0 on accept; a negative reason code on reject (no down-clock fallback exists).
 */
static int config_accept_image(uint32_t img_cfgid0, uint32_t img_cfgid1,
                               uint32_t img_hw_version)
{
    if (img_cfgid0 != NCORE2GP_HW_CONFIGID0 ||
        img_cfgid1 != NCORE2GP_HW_CONFIGID1)
        return -1;                               /* wrong core config (Cairo / ncore2gp only) */

    /* zero-width window: earliest == latest == 281040. Equality, not [min,max] containment. */
    if (img_hw_version != NCORE2GP_HW_VERSION)
        return -2;                               /* wrong HW revision (must be NX1.1.4) */

    return 0;                                    /* accepted */
}

/*
 * coretype_to_arch_id — the one uniform generation relation: arch_id = coretype - 1.
 *
 * coretype is the OBSERVED firmware byte {6,13,21,29,37}; arch_id is the NCFW image selector
 * {0x05,0x0c,0x14,0x1c} for the four shipped gens. The stride is +7 then +8,+8,+8 — so do NOT
 * derive arch_id by subtracting 8 from a coretype; the only invariant is the -1 relation.
 * MAVERICK (coretype 37) yields arch_id 36 by this relation but has NO shipped NCFW image:
 * the libncfw selector tops out at 0x1c and sends anything above it to the unsupported path.
 *
 * @return arch_id (= coretype - 1), or -1 if coretype is not a recognised generation.
 */
static int coretype_to_arch_id(uint32_t coretype)
{
    switch (coretype) {                          /* the literal cases @ libnrtucode_internal 0x9b2b30 */
        case  6:                                 /* SUNDA  v2  -> arch_id 0x05, NCFW image present */
        case 13:                                 /* CAYMAN v3  -> arch_id 0x0c, NCFW image present */
        case 21:                                 /* MARIANA v4 -> arch_id 0x14, NCFW image present */
        case 29:                                 /* M_PLUS v4+ -> arch_id 0x1c, NCFW image present */
            return (int)coretype - 1;
        case 37:                                 /* MAVERICK v5 -> arch_id 36* (INFERRED); no NCFW */
            return (int)coretype - 1;            /* selector has no 0x24 case -> unsupported path */
        default:
            return -1;                           /* unknown generation */
    }
}
```

The bounds the shipped resolver actually emits are `cmp $0x6` (low) and `cmp $0x25` (= 37,
high) with a bit-37-set `movabs` mask — i.e. it treats `{6,13,21,29,37}` as the valid set and
rejects anything outside `[6, 37]`. A reimplementer's loader must apply the same accept set;
the `coretype → silicon-part` binding (Trn1/Trn2/…) is **not** inside any GPSIMD binary in
this corpus, so do not hard-code one.

---

## 9. Adversarial self-verify — the five strongest constants, re-read from the binary

Each re-read independently this session; where a constant lives in two files, both are checked
to agree.

1. **ConfigID `0xC4019686 / 0x2908E4E3`** — cross-checked between two independent files:
   `ncore2gp-params:110,111` (`HWConfigID0/1`) **and** `core-isa.h:258,259`
   (`XCHAL_HW_CONFIGID0/1`). Both read the identical pair. `[HIGH/OBSERVED]`
2. **Zero-width HW window `281040`** — `ncore2gp-params:19,20`
   (`HWMicroArchLatest == HWMicroArchEarliest == 281040`) **and** `core-isa.h:273,277`
   (`XCHAL_HW_MIN_VERSION == XCHAL_HW_MAX_VERSION == 281040`). MIN == MAX confirmed.
   `[HIGH/OBSERVED]`
3. **XEA3** — `core-isa.h:719` `XCHAL_XEA_VERSION 3`, `:726` `XCHAL_HAVE_XEA3 1`
   (XEA1/2/5 = 0). `[HIGH/OBSERVED]`
4. **8 register files** — `objdump -d` of `num_regfiles` @ `0x3b5c20` in `libisa-core.so`
   returns `b8 08 00 00 00  mov $0x8,%eax`. The `vec` entry's `width = 512`, `count = 32` was
   read independently from the `regfiles` table at file offset `0x54a800` (§7). `[HIGH/OBSERVED]`
5. **`coretype 37` resolver** — `objdump -d` @ `0x9b2c9c` in `libnrtucode_internal.so`:
   `83 ff 25  cmp $0x25,%edi` immediately followed by `48 b9 00 20 20 20 20 …
   movabs $0x2020202000,%rcx` (bit 37 set). Confirms the OBSERVED `coretype 37` anchor; the
   `maverick` literal count is `189` in the internal twin and `0` in the shipped front
   (`strings | rg -ci maverick`). `[HIGH/OBSERVED; arch_id 36* MED/INFERRED]`

---

## 10. Confidence ledger

| Claim | Confidence | Provenance |
|---|---|---|
| `ncore2gp` / `Xm_ncore2gp` / `Cairo` / `Xtensa24` identity | `[HIGH/OBSERVED]` | `ncore2gp-params:21–25`; `core.xparm` enum |
| `ConfigID 0xC4019686 / 0x2908E4E3`; `ConfigKey 0x5FA7C9E6 / 0xB2AEBB83` | `[HIGH/OBSERVED]` | `params:110,111,543,544`; `core-isa.h:258,259` |
| HW `NX1.1.4 = LX7.1.4 = 281040 = RI-2020.4`; **zero-width** MIN==MAX | `[HIGH/OBSERVED]` | `params:19–21`; `core-isa.h:260–277`; `versions.h:317` |
| SW tools `14.09 = RI-2022.9 = 1409000`; HW≠SW RI axes distinct | `[HIGH/OBSERVED]` | `params:16–18`; `core-isa.h:242`; `versions.h:437,519` |
| `Customer ID 19270`; `Build 0xc23fe = 795646`; `BuildMode Evaluation` | `[HIGH/OBSERVED]` | `params:3,541,542`; `build.info` |
| XEA3 (`XCHAL_XEA_VERSION 3`); VECBASE reset PC `0x0`; 37 interrupts / 7 levels | `[HIGH/OBSERVED]` | `core-isa.h:719,726,735,736,457,460` |
| Vision-Q7 (`VISION_TYPE 7`, SIMD16 32, 2×FMAC, DFP off); "P5/P6" comment is lag | `[HIGH/OBSERVED]` | `core-isa.h:206–215`; `core.xparm:3638` |
| 8 register files; `vec` 512-bit × 32; 14 formats / 46 slots; 1 coproc; 1 funcUnit | `[HIGH/OBSERVED]` | `libisa-core.so` `num_*` symbols + `regfiles` @ `0x54a800` (file) |
| Knob vector (512-bit LSU, windowed/64-AR, MAC16 off, SP-FP, 7 coproc, gather 8/2) | `[HIGH/OBSERVED]` | `params` + `core-isa.h` defines |
| `coretype {6,13,21,29,37}` OBSERVED; `arch_id = coretype − 1`; stride +7/+8/+8/+8 | `[HIGH/OBSERVED]` | `libnrtucode_internal.so` `0x9b0a10` / `0x9b2b30` / `0x9b2c90` |
| MAVERICK `coretype 37` OBSERVED; `arch_id 36*` INFERRED; core config not shipped | `[ct HIGH/OBSERVED; arch_id MED/INFERRED; HW-rev LOW/OPEN]` | resolver bitmasks; absent `cmp $0x24`; absent v5 NCFW image |

---

## See also

- [Toolchain Inventory & Versions](../../reference/toolchain-versions.md) — the full build/analysis toolchain, the FlexLM gate, and the SDK package versions (this page is its core-identity slice).
- [Codename ↔ Generation Cross-Walk](../../reference/codename-crosswalk.md) — the canonical `codename ↔ coretype ↔ arch_id` join and the `36*` proof.
- [The Gen-Invariance Thesis](../../orientation/gen-invariance.md) — Ledger A (invariant) / Ledger B (scaling): which axes move and which are frozen.
- [The FLIX VLIW Encoding](flix-encoding.md) · [The Eight Register Files](register-files.md) · [The FP Sub-ISA](fp-sub-isa.md) — the deep treatments of the §6 knobs.
- [Master Glossary — Core & ISA identity](../../glossary.md#core--isa-identity) — every term on this page defined with its anchor.
