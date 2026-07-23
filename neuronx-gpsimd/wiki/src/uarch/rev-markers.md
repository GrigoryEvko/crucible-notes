# NX1.1.4 Revision Markers

This is the **field-by-field rev-pinning reference**: exactly which config fields
encode the silicon revision of the GPSIMD core (Cadence Tensilica Vision-Q7 NX
"Cairo", CoreID `ncore2gp`), *where* each one lives, and *how* the in-simulator
consumer checks them. It is the citable provenance witness a reimplementer points
at to ground the core identity. The headline scalars (`ConfigID`, the version
triple, the toolchain stamp) and the gen-axis identity (`coretype`/`arch_id`) are
owned by [The Config Identity](../isa/core/identity-config.md); the full param
inventory is on [The Config Reference Sheet](../isa/core/config-reference-sheet.md).
This page does not restate either — it supplies the **byte-level marker table plus
the consumer-gate decode** that pin the same identity from the config files, the
isadll const tables, and the ISS consumer disassembly.

All facts below are read straight from the shipped config files and from host
`objdump`/`nm`/`readelf`/`xxd`/`strings` on the `ncore2gp` config DLLs and the ISS
consumer DLL. Recovered symbols, strings, and consts are binary-derived and
citeable. Every claim is tagged `[HIGH/MED/LOW]` × `[OBSERVED/INFERRED/CARRIED]`;
the page default is `[HIGH/OBSERVED]` and claims that depart from it carry an explicit tag.

> **TL;DR.** The NX1.1.4 rev is pinned by a small, fixed set of fields re-emitted
> *redundantly* across four host-toolchain files and one isadll const table, then
> cross-checked by the ISS at load. The decisive markers are the 64-bit ConfigID
> `{0xC4019686, 0x2908E4E3}`, the zero-width micro-arch window
> `HWMicroArchEarliest == HWMicroArchLatest == 281040`, the eval-license pair
> `{ConfigKey0=0x5FA7C9E6, ConfigKey1=0xB2AEBB83}`, and the build/tool stamp
> `{BuildUniqueID=795646(0xC23FE), SWToolsRelease=RI-2022.9, SWToolsVername=14.09,
> Customer ID=19270}`. The enforcement gate is libsimxtcore's
> `xt_config_info_check` @`0x226620`, fed ConfigKeys through libxtisa's
> `xtensa_isa_configkey` @`0x7d40`.

---

## 1. The five carriers and one consumer

Five host-toolchain artifacts carry rev markers; one ISS DLL pair consumes them.

| Tag | File | Size | Role | `[conf]` |
|-----|------|------|------|----------|
| `P` | `ncore2gp/config/default-params` (== `ncore2gp-params`) | 15443 B | flat `KEY = VALUE` ISS-facing param dump | `[HIGH/OBSERVED]` |
| `X` | `ncore2gp/config/core.xparm` | 193946 B | XML golden DB (typed declarations + live `<xt>` element) | `[HIGH/OBSERVED]` |
| `CF` | `ncore2gp/control/config.cf` | — | toolchain control file (4th independent rev witness) | `[HIGH/OBSERVED]` |
| `Y` | `ncore2gp/config/core.yml` | — | CompLib `_class_def` schema (field-slot map) | `[HIGH/OBSERVED]` |
| `HW` | `ncore2gp/config/libisa-core-hw.so` | 36576 B | "hw" isadll stub: 8-pair `config_table` incl. live `ConfigKey0/1` | `[HIGH/OBSERVED]` |
| `FH` | `ncore2gp/config/libisa-core.so` | 9690712 B | full isadll: 6-pair `config_table`, **no** keys, decode payload | `[HIGH/OBSERVED]` |
| `H` | `ncore2gp/xtensa-elf/arch/include/xtensa/config/core-isa.h` | — | `XCHAL_*` compiled-header witness (cross-ref) | `[HIGH/OBSERVED/CARRIED]` |
| consumer | `XtensaTools/lib/libxtisa.so` | 96672 B | isa-API wrapper: `xtensa_isa_configkey` + `xtensa_isa_init` (config_table parser, dlopen owner) | `[HIGH/OBSERVED]` |
| consumer | `XtensaTools/lib/iss/libsimxtcore.so` | 2804296 B | ISS core model + param consumer: the `xt_config_info_*` gate family | `[HIGH/OBSERVED]` |

> **NOTE — two byte-identical param copies, one path-divergent third.** Both
> `ncore2gp/config/default-params` and `ncore2gp/config/ncore2gp-params` are
> `md5 2972b339db8512b4a7ff0137f8f71145`, 15443 B — *byte-identical*. The third
> copy at `XtensaTools/config/ncore2gp-params` (`md5 4a52d1d0…`) differs **only**
> in the install/config-prefix/DLL-path block (lines 548–563); the entire rev
> block (lines 16–27, 109–111, 541–544, 573–576) is byte-identical across all
> three. The rev markers are install-prefix-invariant.

---

## 2. The rev-marker provenance table

Every field that encodes the rev, with the exact value, every carrier it appears
in, and its role. `[HIGH/OBSERVED]` unless noted. Values are the bytes actually
present in the files.

| Marker | Value | `P` line | `X` (3546 / decl) | `CF` line | `Y` slot | `H` | Role |
|--------|-------|----------|-------------------|-----------|----------|-----|------|
| `TargetHWVersion` | `NX1.1.4` | 21 | `="NX1.1.4"` / 2673 | 41 | 51 (`&106`) | 260 | human rev name |
| `TargetHWEarliestVersion` | `NX1.1.4` | — | `="NX1.1.4"` / 2674 | 40 | 52 (`&104`) | — | earliest-rev name |
| `HWMicroArchLatest` | `281040` | 19 | — | — | — | 277 | uarch ver, **upper** |
| `HWMicroArchEarliest` | `281040` | 20 | — | — | — | 273 | uarch ver, **lower** |
| `TargetHW_VNum` | `281040` | — | — | 38 | — | — | config.cf upper |
| `TargetHW_EarliestVNum` | `281040` | — | — | 39 | — | — | config.cf lower |
| `HWConfigID0` / `TargetHWConfigID0` | `0xC4019686` | 110 | `="0xc4019686"` / 2675 | — | 53 (`&102`) | 258 | ConfigID hi32 |
| `HWConfigID1` / `TargetHWConfigID1` | `0x2908E4E3` | 111 | `="0x2908e4e3"` / 2676 | — | 54 (`&103`) | 259 | ConfigID lo32 |
| `ConfigKey0` | `0x5FA7C9E6` | 543 | `="0"` (NULL) / 2678 | — | 56 (`&84`) | — | eval-license key0 |
| `ConfigKey1` | `0xB2AEBB83` | 544 | `="0"` (NULL) / 2679 | — | 57 (`&85`) | — | eval-license key1 |
| `TargetHWUniqueID` | `0` | — | `="0"` / 2677 | — | 55 (`&105`) | — | unset in this cfg |
| `ConfigName` / `name` | `Xm_ncore2gp` | 22 | `name="Xm_ncore2gp"` | — | — | — | XPG config name |
| `arch` | `Xtensa24` | 24 | `="Xtensa24"`; enum 2637 | 98 | — | — | XEA3 ISA gen |
| `uarchName` | `Cairo` | 25 | `="Cairo"`; enum 2641 | 97 | — | — | uarch codename |
| `tempActualUarchName` | `Cairo` | — | `="Cairo"`; enum 2645 | — | — | — | actual uarch |
| `coprocessorCount` | `7` | 98 | `="7"` | — | — | — | CP groups (Vision) |
| `vectorPipe` | `1` | 26 (`HasVectorPipe`) | `vectorPipe="1"` | — | — | — | Vision SIMD present |
| `prid` | `0` | 109 (`ProcessorID`) | `prid="0"` | — | — | — | core-instance id |
| `BuildUniqueID` | `795646` (`0xC23FE`) | 541 | banner | 92 (`0xc23fe`) | — | — | sw build id |
| `Customer ID` | `19270` | 3 (banner) | 3 (banner) | 7 (banner) | — | — | Tensilica licensee |
| `BuildMode` | `Evaluation` | 542 | — | — | — | — | license class |
| `SWToolsRelease` | `RI-2022.9` | 16 | path 3636/3639 | — | — | — | toolchain release |
| `SWToolsVername` | `14.09` | 17 | — | — | — | — | Xtensa Tools 14.09 |
| `SWToolsVersion` | `1409000` | 18 | — | — | — | 242 | tool ver int |
| `IsPreconfiguredCore` | `0` | 23 | — | — | — | — | XPG custom config |
| `tie-checksum-0..3` | `0,0,0,0` | 573–576 | — | — | — | — | TIE attestation (zeroed) |
| `SW_ABI` | `windowed` | 525 | `SW_ABI="windowed"` | — | — | — | ABI (gate-checked) |

> **NOTE — the two decisive markers.** Everything below the `{ConfigID,
> 281040-window}` pair (`Cairo`, `Xtensa24`, `coprocessorCount=7`, `vectorPipe=1`)
> is implied-by-construction once the 64-bit ConfigID is fixed, but is re-emitted
> verbatim so the consumer can cross-check **without inverting the ConfigID hash**.
> `[INFERRED/MED]` ConfigID0/1 are a serialization/hash of the full option vector;
> the ISS never needs to invert it because the option fields are present verbatim
> in the same param file.

### 2.1 The version triple `NX1.1.4 = LX7.1.4 = RI-2020.4 = 281040`

The three equivalent **hardware** names are bound on a single binary line in
`XtensaTools/include/xtensa-versions.h:317`:

```c
#define XTENSA_HWVERSION_RI_2020_4   281040   /* versions NX1.1.4, LX7.1.4 */
```

NX is the marketing family name, LX7.1.4 the LX-core family name, RI-2020.4 the
hardware *release* — all three resolve to the single micro-arch integer `281040`.
`core-isa.h` corroborates with `XCHAL_HW_VERSION_NAME "NX1.1.4"` (line 260) and
the release-flag triple `XCHAL_HW_REL_NX1 / NX1_1 / NX1_1_4 == 1` (lines 265–267,
the only `XCHAL_HW_REL_*` set), confirming exactly the `NX1.1.4` point.

> **GOTCHA — `RI-2020.4` and `RI-2022.9` each name *two distinct numbers* on two
> axes.** `xtensa-versions.h` carries `XTENSA_HWVERSION_RI_2020_4 = 281040` (HW
> rev, line 317) **and** `XTENSA_SWVERSION_RI_2020_4 = 1404000` (SW tools 14.04,
> line 430). It also carries `XTENSA_HWVERSION_RI_2022_9 = 281090` (HW, NX1.1.9,
> line 338) **and** `XTENSA_SWVERSION_RI_2022_9 = 1409000` (SW tools 14.09, line
> 437). This config uses the **HW sense of RI-2020.4** (`281040 = NX1.1.4`) and a
> *different, later* **SW release** (`RI-2022.9 = 14.09 = 1409000`). The hardware
> is RI-2020.4; the toolchain is RI-2022.9; the two "RI" labels live on different
> axes and must not be conflated. It does **not** ship the SW 14.04 tools.

> **CORRECTION — `core.yml` slot numbers are the *first element* of a
> `[index, '$']` pair, not a YAML anchor.** The anchor table at `core.yml:1688+`
> lists each field as `Name: &NN  / - <slot>  / - $`. The slot is `<slot>`; the
> `&NN` is a separate YAML anchor id. So `TargetHWConfigID0` maps to **slot 53**
> (anchor `&102`), `ConfigKey0` to **slot 56** (`&84`), `TargetHWVersion` to
> **slot 51** (`&106`). Cite the slot index, not the anchor.

---

## 3. Where each marker lives — byte-grounded

### 3.1 `default-params` — the generated flat dump `[P]`

The verbatim rev block, line-anchored (`rg -n` on `default-params`):

```text
  3: # Customer ID=19270; Build=0xc23fe; Copyright (c) 2004-2018 Tensilica Inc. ...
 16: SWToolsRelease     = RI-2022.9
 17: SWToolsVername     = 14.09
 18: SWToolsVersion     = 1409000
 19: HWMicroArchLatest  = 281040
 20: HWMicroArchEarliest= 281040          <- MIN == MAX => zero-width window
 21: TargetHWVersion    = NX1.1.4
 22: ConfigName         = Xm_ncore2gp
 24: arch               = Xtensa24
 25: uarchName          = Cairo
 26: HasVectorPipe      = 1
110: HWConfigID0        = 0xC4019686       <- UPPERCASE hex
111: HWConfigID1        = 0x2908E4E3
541: BuildUniqueID      = 795646           (0xc23fe == 795646)
542: BuildMode          = Evaluation
543: ConfigKey0         = 0x5FA7C9E6       <- LIVE eval-license key0
544: ConfigKey1         = 0xB2AEBB83       <- LIVE eval-license key1
573-576: tie-checksum-0..3 = 0            <- all zero (neutralized; see §4.4)
```

### 3.2 `core.xparm` — the XML golden DB `[X]`

The schema-typed declaration block (lines 2673–2679) is the authoritative type
contract:

```xml
<TargetHWVersion        b="refh" link="types.string"/>   <!-- 2673 -->
<TargetHWEarliestVersion b="refh" link="types.string"/>  <!-- 2674 -->
<TargetHWConfigID0      b="refh" link="types.u32"/>       <!-- 2675 -->
<TargetHWConfigID1      b="refh" link="types.u32"/>       <!-- 2676 -->
<TargetHWUniqueID       b="refh" link="types.u32"/>       <!-- 2677 -->
<ConfigKey0             b="refh" link="types.u32"/>       <!-- 2678 -->
<ConfigKey1             b="refh" link="types.u32"/>       <!-- 2679 -->
```

`arch`/`uarchName` are constrained **enums** (lines 2637/2641/2645):

```text
arch                range = {Xtensa24, RiscV}                          => "Xtensa24"
uarchName           range = {Athens,Barcelona,Cairo,Darwin,Edinburgh,Greenwood}  => "Cairo"
tempActualUarchName range = {Cairo,Darwin,Edinburgh}                   => "Cairo"
```

The `{Athens..Greenwood}` list is the Vision-Q7 NX uarch lineage; **Cairo** is the
`NX1.1.x` member this config selects. The single big `<xt ... t="xpg_ncore2gp">`
element at line 3546 carries the live values inline:
`TargetHWConfigID0="0xc4019686"`, `TargetHWConfigID1="0x2908e4e3"` (**lowercase**),
`TargetHWVersion="NX1.1.4"`, `name="Xm_ncore2gp"`, `coprocessorCount="7"`,
`vectorPipe="1"`, `prid="0"`. TIE source paths at 3636/3639 anchor the toolchain:
`.../RI-2022.9/.../Coprocessors/XDG/Tie/vision.tie` — the Vision SIMD is the
**XDG** (Xtensa DSP Group) coprocessor, and RI-2022.9 is corroborated a third way
(path string).

> **GOLDEN-DB FINDING — in the xparm the keyed identity is NULL.** The live `<xt>`
> element carries `ConfigKey0="0"`, `ConfigKey1="0"`, `TargetHWUniqueID="0"`. The
> *real* ConfigKeys are stamped only into the **generated** artifacts —
> `default-params` (§3.1) and `libisa-core-hw.so` (§3.5). The xparm pins the
> ConfigID + version + options but is **not** the license-key carrier.

### 3.3 `config.cf` — the toolchain control file `[CF]`

A fourth independent rev carrier, restating the rev with the `VNum` spelling:

```text
  7: # Customer ID=19270; Build=0xc23fe; Copyright (c) 2005-2025 by Tensilica Inc. ...
 38: TargetHW_VNum            281040
 39: TargetHW_EarliestVNum    281040
 40: TargetHWEarliestVersion  NX1.1.4
 41: TargetHWVersion          NX1.1.4
 92: BuildUniqueID            0xc23fe       (hex spelling, vs 795646 decimal in P)
 97: uarchName                Cairo
 98: arch                     Xtensa24
```

`config.cf` does **not** carry `ConfigKey0/1` or `HWConfigID0/1` (`rg` confirms
absent) — it is a name+VNum+build witness only.

> **NOTE — three distinct copyright spans, identical rev.** `default-params`
> banner is `(c) 2004-2018`, `config.cf` is `(c) 2005-2025`, `core.xparm` header
> is `(c) 2008`. The rev *values* are byte-identical across all three; only the
> generator banner spans differ — i.e. the files were emitted by generator stamps
> of different vintages but pin the same NX1.1.4 identity.
> (date-delta interpretation `[MED/INFERRED]`)

### 3.4 `core.yml` — the CompLib schema `[Y]`

The `Xtensa::SystemComponent` `_class_def` (line 1672) enumerates the rev fields
as named CompLib slots, and is unique in **decomposing** the version into separate
scalar slots: `$TargetHW_Major`(22), `$TargetHW_Minor`(23), `$TargetHW_Micro`(24),
`$TargetHW_EarliestMajor`(25), `…Minor`(26), `…Micro`(27), `$CurrentHW_Major`(28),
`$CurrentHW_Minor`(29). The anchor table (1688+) binds the packed fields:
`TargetHWVersion`→51, `TargetHWEarliestVersion`→52, `TargetHWConfigID0`→53,
`TargetHWConfigID1`→54, `TargetHWUniqueID`→55, `ConfigKey0`→56, `ConfigKey1`→57.

> **NOTE — the Major/Minor/Micro decomposition is first-class in the CompLib
> schema.** `281040` decodes `major=2810 / minor=4 / micro=0` (per
> `XCHAL_HW_MIN_VERSION_MAJOR=2810 / MINOR=4 / MICRO=0`, `core-isa.h:270–272`).
> The flat files carry only the packed `281040` + the `NX1.1.4` string; `core.yml`
> is the schema witness, the concrete per-instance scalars live in the state
> arrays the slots index and are authoritatively read from the flat files (§2).

### 3.5 `libisa-core-hw.so` — the keyed-identity config_table `[HW]`

Three exported accessors, confirmed by disasm:

```text
get_config_table   @0x2e20  ->  lea 0x2033d9(%rip),%rax  # 206200 <config_table>
interface_version  @0x2e10  ->  mov $0x76,%eax ; ret      (=118, isadll ABI version)
num_formats        @0x2e30  ->  xor %eax,%eax  ; ret      (=0, the -hw stub has zero tables)
```

> **VERIFICATION GOTCHA — `config_table` is in `.data`, VMA−fileoffset = 0x200000.**
> `readelf -SW libisa-core-hw.so`: `.data` VMA `0x206040` / file offset `0x6040`,
> `.data.rel.ro` VMA `0x205b40` / offset `0x5b40` — both **delta 0x200000** (this
> is the gpsimd ncore2gp DLL delta, **not** libtpu's 0x400000). `.rodata` is
> VMA==fileoffset (`0x3cf1`). So `config_table` @VMA `0x206200` = **file offset
> `0x6200`**; the rodata strings it points at are read at their addend directly.
> Always `readelf -SW` per-section before `xxd`.

The table is a NULL-terminated array of `{key,value}` C-string pointers; 16
`R_X86_64_RELATIVE` entries at `0x206200..0x206280` = **8 pairs**, each addend a
`.rodata` file offset resolved with `xxd` (NUL-terminated):

```text
 +0x00  IsaMemoryOrder        = LittleEndian
 +0x10  IsaMaxInstructionSize = 32
 +0x20  PipeWStage            = 6
 +0x30  DataMemWidth          = 512
 +0x40  IsaCoprocessorCount   = 7
 +0x50  IsaUseBooleans        = 1
 +0x60  ConfigKey0            = 0x5fa7c9e6   <-- the LIVE key0 (lowercase)
 +0x70  ConfigKey1            = 0xb2aebb83   <-- the LIVE key1 (lowercase)
 [terminator]
```

The 6 ISA pairs are an identity *subset*; the two `ConfigKey` pairs are the
licensed half, present **only** in the -hw stub.

> **CASE NOTE — keys are lowercase in the isadll, UPPERCASE in `default-params`.**
> isadll `0x5fa7c9e6 / 0xb2aebb83` vs params `0x5FA7C9E6 / 0xB2AEBB83`; same value.
> The consumer parses with `strtoul` base 0 (§4.2), which is case-insensitive, so
> they bind identically. The ConfigID shows the same split (params UPPER `0xC4019686`,
> xparm lower `0xc4019686`) and the same value.

### 3.6 `libisa-core.so` — the full payload config_table `[FH]` (no keys)

```text
get_config_table   @0x3b5b30  ->  lea ...  # 85ea40 <config_table>
interface_version  @0x3b5b20  ->  mov $0x76,%eax ; ret
```

`config_table` @VMA `0x85ea40` (`.data`, file offset `0x65ea40` — same 0x200000
delta) is **exactly 6 pairs** (12 ptrs) + NULL: `IsaMemoryOrder=LittleEndian,
IsaMaxInstructionSize=32, PipeWStage=6, DataMemWidth=512, IsaCoprocessorCount=7,
IsaUseBooleans=1`. The two qwords after the 12th pointer (`0x85eaa0`, `0x85eaa8`)
are zero — the NULL terminator. No `ConfigKey` pairs; a full-binary `strings`
sweep finds **zero** hits for any rev marker (`C4019686`, `2908E4E3`, `281040`,
`NX1.1.4`, `5fa7c9e6`, `ConfigKey0`). The full isadll is the decode/timing
**payload**; the -hw stub is the licensed **identity**.

> **QUIRK — the adjacent `Xtensa xti2752.9` literal is *not* a 7th pair.** The
> next `.data` pointer object at `0x85eab0` resolves to `Xtensa xti2752.9` (a
> tools-version literal at rodata `0x3d3efb`), but it sits **after** the two NULL
> qwords that terminate the table — it belongs to an unrelated adjacent object,
> not `config_table`.

> **CORRECTION — both isadlls export 159 global text symbols, not 160.** Verified
> three ways: `nm -D --defined-only … | grep -c ' T '` = 159 (hw) and 159 (full);
> `nm --defined-only … | grep -c ' T '` = 159; zero weak (`W`) symbols. The 159 are
> `get_config_table` + `interface_version` + 157 `num_*`/`<thing>_<attr>`
> accessors. A prior count of "160" should read **159**.

---

## 4. The consumer — how the rev is checked

### 4.1 Link topology — who dlopens the isadll

`objdump -p` / `nm -D`:

```text
libsimxtcore.so  DT_NEEDED: libdl, libpthread, libxtisa.so, libxtparams.so,
                            liblog4xtensa, libstdc++, libm, libgcc_s, libc
libxtisa.so      DT_NEEDED: libdl, libc       (imports dlopen/dlsym/dlerror)
libisa-core*.so  DT_NEEDED: libc only         (standalone; the dlopen targets)
```

```text
libsimxtcore --[DT_NEEDED]--> libxtisa --[dlopen]--> libisa-core{,-hw}.so
```

> **CORRECTION — the isadll dlopen lives in `libxtisa.so`, not `libsimxtcore.so`.**
> Both libraries import `dlopen` (libsimxtcore uses it for the iss/fiss kernels),
> but the **ISA-config** dlopen path is one layer down in libxtisa.
> `libxtparams.so` parses the flat param syntax yet carries **none** of the rev
> keys (a `strings` sweep for `HWMicroArch`/`TargetHWVersion`/`HWConfigID`/`281040`/
> `NX1.1.4` returns **0**) — the rev params are owned by libsimxtcore's
> `read_params` (§4.3).

### 4.2 The ConfigKey read path — `libxtisa::xtensa_isa_configkey`

Exported by `libxtisa.so` @`0x7d40`; imported UNDEF by libsimxtcore
(`xtensa_isa_configkey@VERS_1.1`, called at `0x955bf` and `0x1d2155`). Full disasm:

```c
/* xtensa_isa_configkey(xtensa_isa *isa /rdi/, u32 *k0_out /rsi/, u32 *k1_out /rdx/) */
int xtensa_isa_configkey(xtensa_isa *isa, u32 *k0_out, u32 *k1_out) {
    if (!k0_out) return -1;            /* test rsi,rsi ; je .err */
    if (!k1_out) return -1;            /* test rdx,rdx ; je .err */
    *k0_out = *(u32*)((char*)isa + 0x10);  /* mov 0x10(%rdi),%eax ; mov %eax,(%rsi) */
    *k1_out = *(u32*)((char*)isa + 0x14);  /* mov 0x14(%rdi),%eax ; mov %eax,(%rdx) */
    return 0;                          /* xor eax,eax ; ret */
    /* .err: mov $0xFFFFFFFF,%eax ; ret */
}
```

The parsed `xtensa_isa` struct caches `ConfigKey0` at `+0x10`, `ConfigKey1` at
`+0x14`. `[HIGH/OBSERVED, full disasm]`

### 4.3 Who fills `+0x10`/`+0x14` — `xtensa_isa_init`'s config_table walk

`xtensa_isa_init` @`0xee40` (libxtisa) `lea`s the key strings (`interface_version`,
`get_config_table`, `IsaMemoryOrder`, `BigEndian`, `IsaMaxInstructionSize`,
`ConfigKey0`, `ConfigKey1`, `IsaCoprocessorCount`, `num_formats`) at rodata
`0x130d8..0x13140`, walks the `{key,value}` array (stride `0x10`), terminates on a
NULL value ptr, and for each key does an inlined `strcmp` (`repz cmpsb`) then a
conversion:

```c
"IsaMemoryOrder"        -> sete (val == "LittleEndian")        -> isa[+0x00]
"IsaMaxInstructionSize" -> strtol(val, NULL, 10)               -> isa[+0x04]   /* mov $0xa,%edx */
"ConfigKey0"            -> strtoul(val, NULL, 0)               -> isa[+0x10]   /* xor %edx,%edx => base 0 */
"ConfigKey1"            -> strtoul(val, NULL, 0)               -> isa[+0x14]   /* xor %edx,%edx => base 0 */
```

The two `strtoul@plt` call sites (`0xf2bf`, `0xf2e6`) are each preceded by
`xor %edx,%edx` → **base 0** (auto-detect, case-insensitive hex). This is the
mechanical bridge: the -hw config_table string `"0x5fa7c9e6"` → `strtoul` base 0 →
`isa[+0x10]` → `xtensa_isa_configkey` → the value the ISS compares. The base-0
parse is why the params-UPPER / isadll-lower case split binds identically (§3.5).
`[HIGH/OBSERVED, full disasm]`

### 4.4 The gate — `libsimxtcore::xt_config_info_check`

The `xt_config_info_*` family (all exported by libsimxtcore, dynsym):

| Symbol | Addr | Role |
|--------|------|------|
| `xt_config_info_check` | `0x226620` | the compatibility **gate** |
| `xt_config_info_generate` | `0x225cb0` | serialize live config → packed blob |
| `xt_config_info_pack` | `0x226480` | pack the blob |
| `xt_config_info_unpack` | `0x226310` | unpack the blob |
| `xt_config_info_get_number` | `0x225c50` | read a u64 by key from the blob |
| `xt_config_info_get_string` | `0x225bf0` | read a string by key from the blob |

The "Xtensa configuration may be incompatible" message is emitted in
`SYSTEM_BASE::load_bfd`: at `0x1f4d54` it `call`s `xt_config_info_check@plt`, then
at `0x1f4d74` `lea`s the format string `Xtensa configuration may be incompatible
(%s) for %s` (rodata VMA `0x24c310`) and emits it via a virtual logging method
`call *0x200(%r10)` (vtable slot `+0x200`, where the vptr is `_ZTV…+0x10`). The
gate, decoded from full disasm:

```c
/* xt_config_info_check(packed_blob /rdi/, param_kv /rsi=rbp/,
                        int do_abi_check /edx/, const char **errmsg_out /rcx=r12/) */
int xt_config_info_check(blob *b, kv *p, int do_abi_check, const char **errmsg_out) {

    /* (1) ABI check — ONLY when edx != 0 (test %edx,%edx ; je .configid) */
    if (do_abi_check) {
        u64 abi_packed;
        if (xt_config_info_get_number(b, "ABI", &abi_packed)) goto fail;  /* key absent */
        const char *sw_abi = xtensa_string_value(xtensa_find_key(p, "SW_ABI"));
        int neq = (memcmp("windowed", sw_abi, 9) != 0);   /* repz cmpsb, ecx=9 */
        if ((int)abi_packed != neq) {                     /* cmp packed vs bool */
            *errmsg_out = "ABI does not match";  return 1;
        }
    }
.configid:
    /* (2) ConfigID0:  packed HW_CONFIGID0  vs  param HWConfigID0  */
    {   u64 id0p;
        if (!xt_config_info_get_number(b, "HW_CONFIGID0", &id0p)) {
            int id0 = xtensa_int_value(xtensa_find_key(p, "HWConfigID0"));   /* stored at XT_CONFIG+0xe20 */
            if (id0 != (int)id0p) { *errmsg_out = "configuration ID does not match"; return 1; }
        }
    }
    /* (3) ConfigID1:  packed HW_CONFIGID1  vs  param HWConfigID1  */
    {   u64 id1p;
        if (!xt_config_info_get_number(b, "HW_CONFIGID1", &id1p)) {
            int id1 = xtensa_int_value(xtensa_find_key(p, "HWConfigID1"));   /* stored at XT_CONFIG+0xe24 */
            if (id1 != (int)id1p) { *errmsg_out = "configuration ID does not match"; return 1; }
        }
    }
    /* (4) TIE checksums: find param tie-checksum-0..3; if all present & enabled,
           compare packed TIE_CHECKSUM_0..3 (get_number) vs param values.
           Also reads packed SW_FLOATING_POINT_ABI on this path. */
    ... xtensa_find_key(p, "tie-checksum-0".."tie-checksum-3") ...
    ... if mismatch: *errmsg_out = "TIE checksum does not match"; return 1; ...

    return 0;   /* all match => compatible */
}
```

The **packed-vs-param key naming is the tell**: the serialized blob uses ALL-CAPS
keys (`HW_CONFIGID0`, `HW_CONFIGID1`, `TIE_CHECKSUM_0..3`, `ABI`) while the param
file uses the mixed-case spellings (`HWConfigID0`, `tie-checksum-0`, `SW_ABI`).
`xt_config_info_check` bridges the two namings field by field. The live param-side
values are pre-loaded by `read_params` @`0x10fcff`, which does
`find_key("HWConfigID0") → int_value → mov %eax,0xe20(%rbx)` and the `HWConfigID1`
twin into `+0xe24`. `xt_config_info_generate` @`0x225cb0` reads the same
`HWConfigID0`/`HWConfigID1` params via `find_key`/`int_value` to build the blob the
gate compares against. `[HIGH/OBSERVED, full disasm]`

> **NOTE — the ABI check is gated by `edx != 0`.** The entry `test %edx,%edx ;
> je .configid` skips the ABI comparison when `do_abi_check == 0` and falls
> straight to the ConfigID checks; with `edx != 0` it runs the `SW_ABI ==
> "windowed"` comparison first. The ConfigID0/ConfigID1 checks are unconditional.

> **EFFECTIVE-TEETH FINDING — the TIE-checksum slots are neutralized in this
> config.** The four `tie-checksum-0..3` params are all `0` (`default-params:573–576`),
> so the TIE-checksum comparison is effectively a no-op (both sides 0 / the
> per-key enable gate is not satisfied by zero values). The **operative** rev guard
> is therefore `{HW_CONFIGID0, HW_CONFIGID1}` plus the `SW_ABI == windowed` check.
> The ConfigID is the real lock; the TIE-checksum machinery is present but
> unpopulated.

### 4.5 The sibling consistency gates (distinct from the ConfigID gate)

The same `SYSTEM_BASE::load_bfd` / `elf_load` region carries sibling checks whose
error strings live in libsimxtcore rodata. These corroborate the ISA *shape*, not
the rev *id*, and are owned structurally by the ISA-shape / table-census view:

```text
"Version mismatch: libelf %08x  target %08x"     <- ELF class/version check on the loaded image
"Problem with incompatible state: %s"
"Problem with incompatible state indices: %s: %d != %d"
"Problem with incompatible regfile indices: %s: %d != %d"
"incompatible exception architecture"
"incompatible use of the floating point ABI"
"incompatible use of the Extended L32R option"
```

The `libelf` version check compares the ELF header's machine/version fields, **not**
the ConfigID — it is separate from the rev gate. `[HIGH/OBSERVED — strings + region]`

---

## 5. The locked-window semantics

`HWMicroArchEarliest == HWMicroArchLatest == 281040` (`default-params:19/20`) and
its `config.cf` twin `TargetHW_EarliestVNum == TargetHW_VNum == 281040` (`CF:38/39`)
express a **zero-width acceptance window**: the SW/ISS targets *exactly*
micro-arch `281040`, with no version range. A core reporting any other micro-arch
is rejected. `core-isa.h:273/277` shows `XCHAL_HW_MIN_VERSION == XCHAL_HW_MAX_VERSION
== 281040`, the same window, decoding `major=2810 / minor=4 / micro=0` (lines
270–272). The version-name string `TargetHWVersion="NX1.1.4"` and the release-flag
triple `XCHAL_HW_REL_NX1 / NX1_1 / NX1_1_4 == 1` are the human-readable redundant
encoding of the same `281040`.

> **NOTE — zero-width HW-accept window ≠ gen-invariance.** A zero-width window
> means the *toolchain/ISS* targets one Cairo IP rev (`281040`); the *generation*
> axis (SUNDA…MAVERICK, `coretype {6,13,21,29,37}`) is orthogonal. All shipped
> generations run firmware built for this **same** `281040` Cairo core. The
> per-generation `coretype`/`arch_id` identity (`coretype 37` `[OBSERVED]`,
> `arch_id 36` `[INFERRED]` = `coretype − 1`, unshipped MAVERICK) is owned by
> [The Config Identity](../isa/core/identity-config.md).

---

## 6. The two-DLL identity split

| DLL | Carries | Tables |
|-----|---------|--------|
| `libisa-core-hw.so` | **licensed identity**: 6 ISA-identity pairs + the live `ConfigKey0/1` | every decode table empty (`num_formats=0`) |
| `libisa-core.so` | **payload**: the same 6 identity pairs (no keys) + the populated opcode/field/regfile/format tables | full decode/timing tables |

The rev-relevant content of the isadll is concentrated in the **-hw stub's**
`config_table` (the `ConfigKey`s + the 6 ISA-identity pairs); the full isadll
contributes only the 6 non-keyed identity pairs and the decode tables. `[INFERRED/MED]`
the stub-first/payload-second registration (the isadll stub is registered before
the payload, so the keyed identity is in place first) is standard two-DLL behavior;
`[HIGH/OBSERVED]` facts are the `DT_NEEDED` standalone-ness, the dlopen in libxtisa,
and the 8-pair (hw) vs 6-pair (full) `config_table`s.

---

## 7. Where the rev markers do *not* appear

A `strings` sweep for `{C4019686, 2908E4E3, c4019686, 2908e4e3, NX1.1.4, 281040,
5fa7c9e6, ConfigKey0}` returns **zero** hits in `libisa-core.so` (the payload
isadll), `libxtparams.so` (the param-syntax parser), and `libcas-core.so` (the
45.9 MB ISS reference model, not stripped, 178959-symbol `.symtab`). The rev
markers are confined to the **host toolchain** artifacts — the config files
(`default-params`/`core.xparm`/`config.cf`/`core.yml`), the -hw isadll stub, and
the `core-isa.h` / `xtensa-versions.h` headers. The silicon rev is pinned by the
host build config, not re-encoded in the ISS payload or the device runtime.

---

## 8. Reimplementer's checklist

To reproduce a Vision-Q7-compatible config that passes `xt_config_info_check`:

1. Emit `HWConfigID0 = 0xC4019686`, `HWConfigID1 = 0x2908E4E3` in the param file
   (`HW_CONFIGID0`/`HW_CONFIGID1` ALL-CAPS in any packed blob).
2. Emit `HWMicroArchLatest = HWMicroArchEarliest = 281040` (zero-width window).
3. Emit `SW_ABI = windowed` (the gate `memcmp`s 9 bytes against literal `windowed`).
4. Stamp the live eval keys `ConfigKey0 = 0x5FA7C9E6`, `ConfigKey1 = 0xB2AEBB83`
   into the param file **and** the -hw isadll `config_table` (any case; `strtoul`
   base 0 is case-insensitive). The xparm golden DB keeps them `"0"`.
5. The `tie-checksum-0..3` slots may be `0` (neutralized) — the operative guard is
   `{HW_CONFIGID0, HW_CONFIGID1, SW_ABI}`.
6. The isadll must export `get_config_table` (returns the `{key,value}*` array),
   `interface_version` (returns `0x76`), and `num_formats`; the parser
   `xtensa_isa_init` walks the table at stride `0x10` until a NULL value ptr.

---

## See also

| Topic | Page | What it owns |
|-------|------|--------------|
| Headline identity scalars, the HW/SW RI axes, `coretype`/`arch_id` | [The Config Identity](../isa/core/identity-config.md) | `ConfigID`, version triple, the gen-axis identity, `core-isa.h` macro decode |
| Full param inventory + the config-binding mechanism | [The Config Reference Sheet](../isa/core/config-reference-sheet.md) | the 442-row param census, the xparm→params→isadll dlopen order |
| The NCFW LX management core (XEA2) | [The NCFW LX Core](ncfw-lx-core.md) | the second, scalar-LX core — a different ISA from the Vision-Q7 (XEA3) this rev pins |
