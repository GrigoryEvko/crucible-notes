# Master Glossary

This is the wiki-wide definitional reference. Every other page links *into* it: when a
subsystem page writes "the POOL engine", "`coretype 13`", "a wide FLIX bundle", "the CCE
reduce", "`kernel_info_table`", or "`[MED/INFERRED]`", it is naming an entry below. Each
entry gives a crisp definition, the **canonical anchor** (the symbol / enum / string /
opcode-byte / offset where the term is grounded in the shipped binaries), and a link to the
page that owns its deep treatment.

Everything here is derived from static analysis of the shipped binaries, headers, config, and
TIE artifacts — no source tree was consulted. Anchors are written so a reader can re-derive
them: an `nm`/`objdump`/`readelf`/`rg` against the named binary reproduces the cited symbol,
byte, or string. Where a term carries a confidence caveat (the v5 / MAVERICK interiors, the
`arch_id` stride), the entry states it inline; the tag vocabulary is defined under
**[Confidence tags](#confidence-tags-high--med--low--observed--inferred--carried)**.

Two corpus facts govern every anchor below: the device config libraries (`libisa-core.so`,
`libfiss-base.so`, …) carry a full `.symtab` (use `nm`) but **no DWARF** — line info lives
only in the host `libnrt.so`; and the `.data` VMA↔file-offset delta is **per-binary** (measure
it with `readelf -SW` before `xxd`/`objdump`-ing a `.data` struct). `.text`/`.rodata` are
VMA == file-offset for all thirteen host libraries.

---

## How this glossary is organised

The entries are grouped by layer — **[Core & ISA identity](#core--isa-identity)**,
**[The FLIX encoding](#the-flix-vliw-encoding)**, **[Register files & ISA metadata](#register-files--isa-metadata)**,
**[Generations & codenames](#generations--codenames)**, **[Engines](#the-engines)**,
**[Firmware & device idioms](#firmware--device-idioms)**, **[Runtime & host](#runtime--host)**,
**[DMA, memory & collectives](#dma-memory--collectives)**, **[Container & compiler](#container--compiler)**,
**[The ISS oracle](#the-iss-executable-oracle)**, and
**[Confidence & methodology](#confidence--methodology)** — and alphabetised within each group.
A term that fits two layers is defined once and cross-referenced.

---

## Core & ISA identity

**Cairo** — The Tensilica microarchitecture name of the GPSIMD core, read verbatim from the
processor-generator config: `uarchName = Cairo`. It is one frozen configuration of the Cadence
**Vision-Q7 NX** DSP; all five GPSIMD generations run device firmware built for this single
uarch. *Anchor:* `uarchName = Cairo` in `ncore2gp-params` (the `ncore2gp/config` params text).
*Deep page:* [Core Identity & Configuration](isa/core/identity-config.md).

**`ncore2gp`** — The Tensilica **CoreID** / registered core name of the Cairo config, and the
only core in the shipped `xtensa-elf-objdump` registry. It is the handle a reimplementer passes
as `XTENSA_CORE=ncore2gp` to drive the device-native disassembler. *Anchor:* `CoreID ncore2gp`
/ `ConfigName = Xm_ncore2gp` in `ncore2gp-params`; the sole registered core in
`XtensaTools/bin/xtensa-elf-objdump`. *Deep page:* [Core Identity & Configuration](isa/core/identity-config.md).

**Vision-Q7 / Vision-Q7 NX** — The Cadence Tensilica off-the-shelf vector DSP IP that GPSIMD
*is*. GPSIMD is not a bespoke AWS ISA; it is this DSP in the Cairo config, instantiated eight
times as the per-NeuronCore POOL cluster. The "Q7" suffix recurs in the firmware accessor names
(`*_Q7_POOL_PERF_EXTISA_*`). *Anchor:* the `XCHAL_VISION_TYPE = 7` / `vq7_isa = 1` config tokens
in `core-isa.h`; the `Vision` coproc on the six SIMD register files (see
**[register files](#register-files--isa-metadata)**). *Deep page:* [Core Identity & Configuration](isa/core/identity-config.md).

**`Xtensa24` / XEA3** — The instruction-set architecture and exception model of the core.
`arch = Xtensa24` in the config; "XEA3" is the Xtensa Exception Architecture 3 it implements
(`XCHAL_XEA_VERSION 3`): a single unified `DispatchVector` (`numOfVectors = 0`), reset
`PC = VECBASE = 0x0`, software-managed dispatch via the `MS`/`DISPST` mode register —
single-context, single-dispatch. (The "37-entry cause table" sometimes cited is **not**
byte-anchored to a literal `37` in the corpus; the firm anchor is the single-DispatchVector
model.) It is the LX7 family — windowed ABI (`entry`/`callx8`/`retw.n`), 16-bit density, plus
FLIX/VLIW. *Anchor:* `arch = Xtensa24` in `ncore2gp-params`; `XCHAL_XEA_VERSION 3` in the core
config; the windowed `entry a1,32` / `retw.n` decode validated by the device disassembler.
*Deep page:* [The XEA3 Interrupt / Exception Architecture](control/interrupt/xea3-interrupt-architecture.md).

**NX1.1.4 / LX7.1.4 / RI-2020.4** — The single hardware-IP revision the core targets, expressed
three equivalent ways. `TargetHWVersion = NX1.1.4`; the integer `281040`
(`HWMicroArchEarliest == HWMicroArchLatest`, a **zero-width** accept window) decodes via the
shipped `xtensa-versions.h` macro `XTENSA_HWVERSION_RI_2020_4 281040 /* versions NX1.1.4,
LX7.1.4 */`. The NX- and LX-family names denote the same IP. *Caution — distinct from the
toolchain:* the **HW IP** is RI-2020.4, but the **toolchain** that builds for it is
**RI-2022.9** (Xtensa Tools 14.09 / binutils 2.34 / clang-10 for the four shipped gens). Do not
conflate the two axes. *Anchor:* `TargetHWVersion = NX1.1.4`, `HWMicroArch* = 281040`,
`SWToolsRelease = RI-2022.9` in `ncore2gp-params`; `xtensa-versions.h`. *Deep page:* [Toolchain Inventory & Versions](reference/toolchain-versions.md).

**`Xm_ncore2gp`** — The full **ConfigName** of the Cairo build (the `Xm_` prefix is the
Tensilica configuration-management tag). Synonymous with the core at the build-config layer.
*Anchor:* `ConfigName = Xm_ncore2gp` in `ncore2gp-params`. *Deep page:* [Core Identity & Configuration](isa/core/identity-config.md).

**GPSIMD** — The General-Purpose SIMD compute substrate inside every NeuronCore: the **POOL**
engine, eight Vision-Q7 Cairo DSP cores running one SPMD image. In the product it owns three
narrow lanes — the native int32/uint32 add/sub/mul datapath (opcode `0x41`), the
gather/scatter/custom-op lane (rides opcode `0xF0`), and the SB2SB collective hop (`0xBF`) —
and is explicitly **not** the float hot-path. It cannot address PSUM (see **[SBUF / PSUM](#sbuf--psum)**).
*Deep page:* [What GPSIMD Is](orientation/what-gpsimd-is.md).

---

## The FLIX VLIW encoding

**FLIX** — Tensilica **F**lexible **L**ength **I**nstruction e**X**tensions: the VLIW scheme
the Vision-Q7 ISA uses, in which one fetched **bundle** holds several independent operation
**slots** issued the same cycle. The Cairo config exposes **14 instruction formats** and
**46 total slots**. *Anchor:* `num_formats` @ `0x3b65e0` (`mov $0xe,%eax`) and `num_slots` @
`0x3b6510` (`mov $0x2e,%eax`) in `libisa-core.so` — both byte-verified. *Deep page:* [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md).

**Bundle / format / slot** — A **bundle** is one fetched FLIX word (16 bytes *wide*, 8 bytes
*narrow*, or a 2-/3-byte scalar fallback). A **format** is one of the 14 layouts a bundle word
can take (`x24`, `x16a`, `x16b` scalar; `F0`/`F1`/`F2`/`F3`/`F4`/`F6`/`F7`/`F11` wide;
`N0`/`N1`/`N2` narrow). A **slot** is one operation position within a format; the 14 formats
partition into exactly 46 slots. *Anchor:* `format_decoder` @ `0x3b5970`, `formats[]` @
`0x6cd980`, `slots[]` @ `0x6cdb00` in `libisa-core.so`. *Deep page:* [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md).

**FLIX length question — the reconciled truth** — The runtime `length_decoder` (@ `0x3b5a50`)
indexes a 256-entry `length_table` (@ `0x3d4100`) by `((byte3 & 0xF) << 4) | (byte0 & 0xF)` and
yields **seven distinct length-class outcomes** that map to **four distinct instruction
byte-lengths `{2, 3, 8, 16}`** — the four lengths plus the `op0==0xF` 8-vs-16 split keyed on
byte 3 plus the illegal `-1`. State it this way: "7 length-class outcomes → 4 byte-lengths",
**never** either number alone. The Tensilica static macro `XCHAL_BYTE0_FORMAT_LENGTHS` keys
length on byte 0 only and is **wrong** for `op0==0xF`; the binary `length_table` is
authoritative. *Anchor:* `length_table[256]` @ `0x3d4100` in `libisa-core.so`. *Deep page:* [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md).

**Wide / narrow bundle** — A **wide** bundle (`F0`/`F1`/`F2`/`F3`/`F4`/`F6`/`F7`/`F11`) is
16 bytes = four 32-bit lanes = a 128-bit bundle carrying 4–5 issue slots; it leads with
`op0==0xE` or `op0==0xF` with **odd** `b3lo`. A **narrow** bundle (`N0`/`N1`/`N2`) is 8 bytes =
two 32-bit lanes, 2–4 slots (some `None`/NOP filler); it leads with `op0==0xF` and **even**
`b3lo`. Mis-classifying a wide bundle as narrow is the single most common cause of a desynced
sweep. *Anchor:* the `b3lo` columns of `length_table`. *Deep page:* [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md).

**FLIX-desync (the desync wall)** — A linear byte sweep over a dense FLIX `.text` does not stay
synchronized: literal pools and boot stubs sit between bundles, and a literal word whose low
nibble masquerades as a valid `op0` advances the cursor by the wrong length, locking onto a
bundle interior. Everything read at a *known* address (table bases, `kernel_info_table` entries,
reset vectors) stays HIGH; per-instruction bodies *inside* a desynced span are `[MED/OBSERVED]`.
Recovery re-anchors on `entry aN,M` / `retw.n` / a `.xt.prop` function-start. *Anchor:* the
`length_decoder` `-1` (illegal) return + out-of-image branch targets are the desync tells.
*Deep page:* [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md).

**`ivp_*`** — The mnemonic prefix of the Vision-Q7 vector instruction family (the IVP SIMD TIE
package `xt_ivp32`). Examples: `ivp_mulan_2xf32`, `ivp_gatheranx16`, `ivp_minn_2xf32`,
`ivp_dextrprn_2x32`. Roughly 1,534 shipped Vision-Q7 mnemonics carry it (1,607 pre-fold in the
TIE DB; 12,642 placements). The base-Xtensa scalar ops (`addi`, `l32i.n`, `entry`) are *not*
`ivp_*`-prefixed. (The corpus does not spell out a literal expansion of "IVP"; it is named only
as the Vision-Q7 / IVP SIMD family — do not invent one.) *Anchor:* the `ivp_*` opcode-name string
pool in `libisa-core.so`, package `xt_ivp32`. *Deep page:* [ISA Reference — Template & 30-Batch Partition](isa/ref/template-and-partition.md).

---

## Register files & ISA metadata

**The eight register files** — The Cairo config defines exactly 8 register files
(`num_regfiles` @ `0x3b5c20` → `mov $0x8,%eax`), read byte-exact from the `regfiles` table @
`0x74a800` in `libisa-core.so`. Two are core/scalar (`AR`, `BR`); six are the Vision-Q7 SIMD
coprocessor files (`vec`, `vbool`, `valign`, `wvec`, `b32_pr`, `gvr`):

| idx | name | short | width (bits) | count | package | role |
|---|---|---|---|---|---|---|
| 0 | **`AR`** | `a` | 32 | 64 | `xt_xtensa` | scalar address/general registers (windowed) |
| 1 | **`BR`** | `b` | 1 | 16 | `xt_booleans` | scalar boolean registers (`BR2/4/8/16` views) |
| 2 | **`vec`** | `v` | 512 | 32 | `xt_ivp32` | the 512-bit SIMD vector file (32 × 16-bit lanes) |
| 3 | **`vbool`** | `vb` | 64 | 16 | `xt_ivp32` | per-lane SIMD predicate (boolean) file |
| 4 | **`valign`** | `u` | 512 | 4 | `xt_ivp32` | alignment registers priming unaligned vector loads |
| 5 | **`wvec`** | `wv` | 1536 | 4 | `xt_ivp32` | wide MAC accumulators (quad-width readout) |
| 6 | **`b32_pr`** | `pr` | 64 | 16 | `xt_ivp32` | 64-bit predicate/pack registers (`int64pr` ctype) |
| 7 | **`gvr`** | `gr` | 512 | 8 | `xt_ivp32` | global / state vector registers (`gsr` ctype; flags `0x0d`) |

Each carries `num_callee_saved = 0` (every file caller-saved in this ABI). *Anchor:* `regfiles`
@ `0x74a800`, stride 56, in `libisa-core.so`. *Deep page:* [The Eight Register Files](isa/core/register-files.md).

**`wvec` / SuperGather / `valign`** — Three Vision-Q7 specialisations worth naming separately.
**`wvec`** is the 1536-bit wide accumulator file that quad-MAC widening writes into (regfile 5).
**SuperGather** is the IVP two-phase, gather-register-staged vector gather/scatter unit — the
hardware behind the gather/scatter/embedding lane (faults via
`LoadStoreSGAccErrorException_exc`; five `nx_GSControl_*`/`nx_GSEnable_*` ports). **`valign`**
(regfile 4) holds the alignment state an unaligned vector load primes before a streaming read.
*Anchor:* `wvec` row in `regfiles`; the SuperGather slot bodies / `cas`/`fiss` SuperGather
leaves. *Deep pages:* [B19 SuperGather Scatter/Gather](isa/ref/b19-scatter-gather.md), [B06 Vector Loads + valign priming](isa/ref/b06-loads.md).

**TIE** — Tensilica **Instruction Extension**: the language/database in which the Vision-Q7
coprocessor ISA (the `ivp_*` ops, the SIMD register files, the operand encodings) is defined.
The shipped `libtie-core.so` (~48 MiB) is a data-table container of the TIE opcode/encoding
tables (its `.text` is a trivial `0x48` bytes; the substance is `.data`); `libtie-Xtensa-msem.so`
is the smaller memory-semantics variant. *Anchor:* `libtie-core.so` (SHA `06fc43ea…`). *Deep page:* [The TIE Database & Four Independent ISA Sources](isa/core/tie-database.md).

**`ctype` / `coproc` / `funcUnit`** — The three libisa per-operand / per-regfile metadata fields,
held as `.data.rel.ro` tables in `libisa-core.so`. **`ctype`** is the TIE C value-type a register
or operand maps to (e.g. `_TIE_xt_ivp32_xb_vec2Nx8` for `vec`; the `ctypes` table has 64
entries). **`coproc`** is the coprocessor (the `coprocs` table has exactly **one**:
`{name="Vision", number=1}`; `AR`/`BR` are core, `coproc = ""`). **`funcUnit`** is the functional
unit an opcode binds (the `funcUnits` table has exactly **one**:
`{name="XT_LOADSTORE_UNIT", num_copies=2}`). *Anchor:* `regfile_ctype` @ `0x3b5d10`,
`regfile_coproc` @ `0x3b5d30`, and the `ctypes`/`coprocs`/`funcUnits` `.data.rel.ro` tables in
`libisa-core.so`. *Deep page:* [ctype / coproc / funcUnit / bypass Tables](isa/core/ctype-coproc-funcunit.md).

---

## Generations & codenames

**`coretype`** — The firmware byte that identifies a GPSIMD generation, read **directly** from
the `nrtucode_get_ext_isa_internal` switch (@ `0x9b2b30` in `libnrtucode_internal.so`). The five
constants are `{6, 13, 21, 29, 37}` for SUNDA / CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK,
spaced **+8** apart. `coretype` is **`[HIGH/OBSERVED]`** for all five (MAVERICK's `37` is anchored
twice — the header enum ordinal and the twin resolver bitmasks). *Anchor:* the `case 6/13/21/29/37`
switch @ `0x9b2b30`; `nrtucode_core_get_coretype` @ `0x9b0a10`. *Deep page:* [Codename ↔ Generation Cross-Walk](reference/codename-crosswalk.md).

**`arch_id`** — The host-side NCFW image-selector byte. The relation is **`arch_id = coretype − 1`**
(an INFERRED stride, *not* a literal table): `{0x05, 0x0c, 0x14, 0x1c}` for the four shipped gens.
The four shipped values are firmware-byte-grounded (the `libncfw_get_image` `cmpl` ladder);
MAVERICK's `arch_id = 36 (0x24)` is **`[MED/INFERRED]`** — there is no `cmp $0x24` anywhere and no
v5 NCFW image, so it is the `coretype − 1` extrapolation only. **Rule:** `coretype` OBSERVED,
`arch_id` INFERRED (v5 most acutely). *Anchor:* `libncfw_get_image` ladder `{0x05,0x0c,0x14,0x1c}`,
`ja` default >`0x1c`, in `libncfw.so`. *Deep page:* [Codename ↔ Generation Cross-Walk](reference/codename-crosswalk.md).

**SUNDA** — Generation v2 (NeuronCore-v2; Trn1 / Inf2). `coretype 6`, `arch_id 0x05`. The
in-line floor: flat CSR schema, **no** HW-Decode, **no** CC, **no** SB2SB; the only generation
shipping a real JSON opcode manifest. *Anchor:* `sunda.c` source string + `SUNDA_*` accessor
symbols in `libnrtucode_internal.so` / `libncfw.so`. *Deep page:* [SUNDA v2 Baseline Topology](generations/sunda-v2-baseline.md).

**CAYMAN** — Generation v3 (NeuronCore-v3; Trn2). `coretype 13`, `arch_id 0x0c`. The
byte-grounded reference generation; four `EXTISA_0..3` libs, own compile. *Anchor:* `cayman.c`;
`CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` @ `0x9b3aa0` in `libnrtucode_internal.so`. *Deep page:* [Codename ↔ Generation Map](generations/codename-generation-map.md).

**MARIANA** — Generation v4 (NeuronCore-v4). `coretype 21`, `arch_id 0x14`. Distinct compile of
the CAYMAN opcode contract. *Anchor:* `mariana.c`; `MARIANA_Q7_POOL_PERF_EXTISA_*` accessors.
*Deep page:* [Codename ↔ Generation Map](generations/codename-generation-map.md).

**MARIANA_PLUS** — Generation v4+ (Trn3-pre). `coretype 29`, `arch_id 0x1c`. A *feature-flag
delta* on the MARIANA silicon ISA, **not** new device bytes: its EXTISA blobs and NCFW DRAM are
**byte-identical** to MARIANA, and it has no `neuron_mariana_plus_arch_isa` dir. Distinct only at
the NCFW / getter / coretype level; selected via the `NEURON_RT_DBG_V4_PLUS=0/1` env (which
replaced the removed `NRTUCODE_MPLUS_ON_MARIANA` flag). *Anchor:* `mariana_plus.c`; the removed-flag
tripwire string in `libnrtucode.so`. *Deep page:* [MARIANA_PLUS (v4+) Generation Delta](generations/mariana-plus-delta.md).

**MAVERICK** — Generation v5 (NeuronCore-v5). `coretype 37 [HIGH/OBSERVED]`, `arch_id 36/0x24
[MED/INFERRED]`. **Header-OBSERVED only** — it exists solely in `libnrtucode_internal.so`'s
`.rodata` (four DYN/PIC, fully stripped, clang-15 / XtensaTools-15.05 Q7 ELFs); there is **no**
shipped v5 NCFW image (`libncfw_get_image` tops at MARIANA_PLUS). Every v5 *interior* claim is
flagged INFERRED. No product part-binding ("Trn4") is named in the corpus — do not fabricate
one. *Anchor:* the `maverick` literal appears **189×** in `libnrtucode_internal.so`, **0×** in
`libnrtucode.so`; `maverick_libs` @ `0x9b9050`. *Deep page:* [MAVERICK (v5) Profile](generations/maverick-profile.md).

**TONGA** — The pre-unified **outlier** (legacy NC-v1, the "L" family; Inf1). It is **not** a
sixth GPSIMD generation and is **outside** the `coretype = arch_id + 1` / +8 stride family: no
`coretype`, no `arch_id`, no NCFW image, no EXTISA blob. Its dtype enum is a distinct 8-code
`TONGA_ISA_TPB_DTYPE_*` family — a *different name family* (`TONGA_` vs `NEURON_`) signalling an
older ISA. *Anchor:* `TONGA_ISA_TPB_DTYPE_*` in `arch-isa/tpb/aws_tonga_isa_tpb_common.h`;
`strings | rg -i tonga` on `libnrtucode_internal.so` returns zero. *Deep page:* [Cross-Generation Opcode-Table Diff + TONGA](generations/cross-gen-opcode-diff.md).

**NC-v1 … NC-v5** — The NeuronCore-version axis: NC-v1 = TONGA (legacy, outlier), NC-v2 = SUNDA,
NC-v3 = CAYMAN, NC-v4 = MARIANA, NC-v4+ = MARIANA_PLUS, NC-v5 = MAVERICK. The "v#" leg in the
NCFW selector (`v2`/`v3`/`v4`/`v4_plus`) maps `arch_id → firmware image`. *Anchor:* the eight
`v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` blob symbols in `libncfw.so`. *Deep page:* [Codename ↔ Generation Cross-Walk](reference/codename-crosswalk.md).

**EXTISA** — The per-(generation) statically-embedded Vision-Q7 **ext**ended-**ISA** device
image: a freestanding ELF32-Xtensa (`e_machine = 0x5e = 94`) blob whose `kernel_info_table` *is*
the opcode→function map for that generation. Reached in the host binaries by the
`<GEN>_Q7_POOL_PERF_EXTISA_<n>_SO_get` accessors (PERF flavor; each has a paired 32-byte
`{"dummy_message": "hello world"}` JSON stub). CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK ship four
(`EXTISA_0..3`); SUNDA ships one (weak-undef). *Anchor:* `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` @
`0x9b3aa0` and 16 embedded Xtensa ELFs in `libnrtucode_internal.so`; 13 in `libnrtucode_extisa.so`.
*Deep page:* [EXTISA Q7 SO-Blob Inventory](images/extisa-inventory.md).

**`EXTENDED_INST` (opcode `0xF0`)** — The opcode (240) that opens the "extended instruction space
for customer-specific ops"; a hand-authored custom op rides `0xF0` (spec-multiplexed via several
`kernel_info_table` rows) and DMAs its own Q7 EXTISA image into Q7 instruction memory. *Anchor:*
`NEURON_ISA_TPB_OPCODE_EXTENDED_INST = 0xf0` in `neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h`.
*Deep page:* [POOL Extended-Opcode (0xF0) Dispatch](firmware/pool/pool-ext-0xf0.md).

---

## The engines

**TPB (Tensor-Processing Block)** — One NeuronCore as a block of engines sharing one on-chip
SBUF. The five compute/control engines are **PE**, **ACT**, **POOL**, **DVE**, **SP** (with
**TOP_SP** as the sequencer that walks the collective program). *Deep page:* [The Seven Faces of the One Machine](orientation/seven-faces.md).

**`engine_idx` (firmware engine numbering)** — The firmware engine ordinal used throughout this
wiki: **PE 0, ACT 1, POOL 2, DVE 3, SP (`TPB_SP`) 4, TOP_SP 5**. *This is a different integer
space from the NKI/compiler engine enum* (`tensor=1, scalar=2, gpsimd=3, dma=4, vector=5,
sync=6`) — never equate the two. *Anchor:* the per-engine firmware image families; the engine
enum in the SP/TOP_SP kernel report. *Deep page:* [The GPSIMD-Relevant Compiler Map](compiler/compiler-map.md).

**PE** — The Processing-Element array: the 128×128 systolic Tensor engine, and the **only**
writer of PSUM. `engine_idx 0`. *Anchor:* `PE`-keyed firmware images; the `pe-matmul` kernel.
*Deep page:* [PE Matrix-Multiply Path](firmware/kernels/pe-matmul.md).

**ACT** — The Scalar / activation / PWL (piecewise-linear) engine. `engine_idx 1`. Folds into
DVE on MAVERICK (the ACT→DVE fold). *Anchor:* `ACT`-keyed firmware images; the `activate-pwl`
kernel. *Deep page:* [Activate + the PWL Application Mechanism](firmware/kernels/activate-pwl.md).

**POOL** — The GPSIMD engine: eight Vision-Q7 Cairo DSP cores. `engine_idx 2`. The subject of
this wiki; the `*_Q7_POOL_PERF_EXTISA_*` accessor names carry it. *Anchor:* `Q7_POOL` in the
EXTISA accessor symbols. *Deep page:* [POOL Engine Main Dispatch Loop](firmware/pool/pool-dispatch.md).

**DVE** — The Vector engine. `engine_idx 3`. Owns the predicated-op family and the engine-state
read-back ops; absorbs ACT on MAVERICK. *Anchor:* `DVE`-keyed firmware images; the
`dve-read-state` kernel. *Deep page:* [DVE State Read-Back](firmware/kernels/dve-read-state.md).

**SP (`TPB_SP`)** — The per-NeuronCore Sync/control front-end executor that fires collective and
sync primitives. `engine_idx 4`. *Anchor:* `TPB_SP`-keyed firmware images / the SP engine enum.
*Deep page:* [Per-Engine Firmware Depth](uarch/per-engine-depth.md).

**TOP_SP** — The standalone NX-core sequencer that **walks the `cc_op` collective program** in
SPAD — the engine that lowers a `TriggerCollective` into the per-step descriptor schedule.
`engine_idx 5`. Distinct from SP. *Anchor:* `TOP_SP_0_TPB_SP` @ SoC `0x8280200000`; the TOP_SP
collective-lowering report. *Deep page:* [TOP_SP Collective Lowering](collectives/ops/top-sp-lowering.md).

**NCFW (NeuronCore Firmware)** — A **separate** scalar **Xtensa-LX** management core (not the
Vision-Q7 FLIX core) that orchestrates collectives — the ring/mesh/hierarchical schedulers. Its
firmware lives in `libncfw.so` as eight `v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` blobs. **Do not
run the FLIX decoder on NCFW images:** the LX core uses the scalar length rule (`op0∈{e,f}` →
3-byte, else 2-byte), and `op0=e/f` bytes are operand bytes, not bundle leaders — applying FLIX
yields the spurious "~26–28% FLIX" artifact. No LX disassembler ships, so NCFW interiors are a
distinct wall. *Anchor:* `libncfw.so` (SHA `598920d7…`, SONAME `libncfw.so.2.31.1.0.cf13a49f`);
`sunda.c` / `cayman.c` / `mariana.c` source strings in its `.rodata`. *Deep page:* [The NCFW Scalar-LX Management Core](uarch/ncfw-lx-core.md).

**Three cores, do not conflate** — **Q7** = the Vision-Q7 POOL/DVE DSP (this wiki's subject,
FLIX). **NCFW** = a scalar Xtensa-LX collective-management core. **TOP_SP** = the NX sequencer
walking `cc_op`. Three cores, three ISAs. *Deep page:* [Keystone Facts Reimplementers Get Wrong](orientation/keystone-facts.md).

---

## Firmware & device idioms

**`kernel_info_table`** — The PROGBITS section baked into every device image that maps an opcode
key to a device handler: an array of **8-byte** entries laid out
`{ u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA }` — i.e. a big-endian key `(spec << 8) | opcode`
at `+0` and a little-endian `funcVA` at `+4`, where `funcVA` is the only relocated field (one
`R_XTENSA_RELATIVE` per entry, stride 8 — a self-checking anchor for the entry count). CAYMAN's
table has **17** entries. *Anchor:* section `[7] kernel_info_table` PROGBITS @ VMA `0x02000380`,
size `0x88` (= 17×8) in the carved Cayman EXTISA image. *Deep page:* [kernel_info_table Binary Layout](firmware/pool/kernel-info-table.md).

**`.xt.prop`** — Per-function FLIX **property** sections the device ELFs ship **un-merged** as
`.xt.prop.<mangled>`, whose section *names are the C++ mangled symbols* of the function they
describe (e.g. `.xt.prop._Z40decode_extended_inst_tensor_tensor_arithbj` →
`decode_extended_inst_tensor_tensor_arith`). Merging them and synthesising a `.symtab` from the
function-start records flips the disassembler from data-mode to FLIX-aware code-mode — the
primary recovery anchor against the desync wall. *Anchor:* `.xt.prop.<mangled>` sections in the
carved Cayman/Mariana EXTISA images. *Deep page:* [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md).

**`'S:'` / `'P%i:'` trace tags** — The DEBUG-build microcode trace-string idiom. `'S:'`-prefixed
strings are the SEQ control-engine stream (e.g. `S: NX in Sunda mode: HW decode disabled`);
`'P%i:'`-prefixed strings are the per-CPU POOL/Q7 data-plane stream, the `%i` filled with the
core's PRID (e.g. `P%i: Decode : SB2SB_Collective`). They double as recovery anchors (a named
function self-identifies via its `S:`/`P%i:` literal). *Anchor:* `S:`/`P%i:` literals in
`libnrtucode_extisa.so` / `libnrtucode_internal.so` `.rodata`. *Deep page:* [SEQ Decode / Dispatch Hub](firmware/seq/dispatch-hub.md).

**HW-Decode vs Sunda mode** — The two device-fetch dispatch modes. Later generations use a
hardware opcode-decode CAM table (HW-Decode); SUNDA disables it and uses a software dual-fetch
path — captured by the trace `S: NX in Sunda mode: HW decode disabled`. *Anchor:* that string;
the dual-fetch dispatch report. *Deep page:* [HW-Decode vs Sunda Dual Fetch](firmware/seq/dual-fetch.md).

**DGE (Descriptor Generation Engine)** — The device-side engine that builds DMA descriptors: a
3-backend selector — **Pool** (the Q7 software path; "Select backend Pool"), **RTL** (the
hardware SDMA path), and **software** (Q7 SW-DGE) — plus a host-private priority/mailbox/PC-bounds
API. *Anchor:* the `dge_backend_rtl` selector ("NO BACKEND FOUND" string); the DGE firmware
reports. *Deep pages:* [DGE 3-Backend Selector](firmware/dge/dge-backend-selector.md), [The DGE Host-Private API](runtime/dge-host-api.md).

**SEQ** — The device sequencer front-end: boot/entry, the main FSM loop, fetch + PC-redirect,
IRAM cache/overlay, branch/prefetch, PC-bounds enforcement, the decode/dispatch hub, the
run-state machine, the error handler, and the IRQ "surprises" poll. *Anchor:* the SEQ firmware
image bodies. *Deep page:* [SEQ Main FSM Loop](firmware/seq/main-loop.md).

**Boot handshake** — The Q7 bring-up handshake: an unbooted sentinel `0x6099CB34` that the host
CAS-writes to the claim value `0x502B2DA1` to take ownership of a core. *Anchor:* the sentinel /
claim constants (`[HIGH/OBSERVED]`, index quick-reference). *Deep page:* [Boot / Reset Sequence](uarch/boot-reset.md).

**RTTI absence** — The device firmware is compiled `-fno-rtti`: the `_ZTS` / `_ZTI` / `_ZTV`
byte-count (symbol *and* byte-string) is **0 across all 29 device ELFs** (`ELFCLASS32`,
`e_machine = 94`), so no glossary or page rests on a recovered class hierarchy. *Anchor:*
`nm | rg -c '_ZT[ISV]'` = 0 on the 29 carved Xtensa blobs. *Deep page:* [Methodology — How This Was Reverse-Engineered](reference/methodology.md).

---

## Runtime & host

**`libnrt`** — The host runtime library (`libnrt.so.2.31.24.0`, with a vendored `KaenaHal`): it
lays out the `aws_hal_q7` register programming, loads a NEFF (`nrt_load`), DMAs any custom Q7
EXTISA image into Q7 instruction memory, and boots the cores. It is the only binary in the stack
carrying DWARF line info. *Anchor:* the `nrt_*` public API surface (host `libnrt.so`, a sibling
package). *Deep page:* [The libnrt Surface Map](runtime/libnrt-surface.md).

**`nrtucode`** — The host runtime **micro-code** subsystem that manages Q7 device images:
context/core lifecycle, the coretype/ext-ISA resolvers, the opcode→library resolver, the
ll-load/unload sequence generators, the opset API, and the prelinker. *Anchor:*
`nrtucode_get_ext_isa_internal` @ `0x9b2b30`, `nrtucode_core_get_coretype` @ `0x9b0a10` in
`libnrtucode_internal.so`. *Deep page:* [The nrtucode Subsystem + Device Bring-Up](runtime/nrtucode-bringup.md).

**`nrtucode_core_t` / `nrtucode_context_t`** — The two central host runtime structs: the
per-core handle (`0x70` bytes, friendly-name `"nrtucode_core_t@%p"`, the `coretype` byte at
`ctx+0x10`) and the per-model context (`0x28` bytes — lifecycle + the `dmem` allocator). *Anchor:*
`nrtucode_core_t` / `nrtucode_context_t` referenced by the introspection getters in
`libnrtucode_internal.so`. *Deep pages:* [nrtucode_core_t Struct](runtime/nrtucode-core.md), [nrtucode_context_t + Lifecycle](runtime/nrtucode-context.md).

**`nrtucode` family (the four release containers)** — `libnrtucode.so` is the 4-gen **shipped
front** getter (stripped); `libnrtucode_internal.so` is the not-stripped **5-gen twin** (carries
MAVERICK + the EXTISA accessors); `libnrtucode_extisa.so` is the blob container (13 embedded
Xtensa ELFs); `libnrtucode.a` is the static archive. *Anchor:* SONAMEs / BuildIDs in the corpus
contract; the `*_internal.so` is the debug-info source for the stripped front. *Deep page:* [The libnrt Surface Map](runtime/libnrt-surface.md).

**`aws_hal_q7`** — The host-side Q7 Hardware-Abstraction-Layer symbol family (`aws_hal_q7_*`):
the low-level register/window/swap-table accessors the runtime uses to talk to a Q7 core
(e.g. `aws_hal_q7_swap_table`, `aws_hal_q7_swap_file_io_table`). *Anchor:* `aws_hal_q7_*` symbol
prefix in `libnrt.so`. *Deep page:* [The aws_hal_q7_* HAL](runtime/aws-hal-q7.md).

**PRID / processing-rank** — The SPMD rank of a Q7 core: the eight POOL cores run one image
(SPMD), `rank = PRID ∈ {0..7}`, `count = 8`, over per-core-private memory — data-race-free by
construction. `XCHAL_HAVE_PRID = 1`, special-register `PRID = 235`; the low 4 bits
(`PRID_ID & 0xF`) are the instance id. The `'P%i:'` trace tag fills `%i` with the PRID. *Anchor:*
the 8 linker specs `lsp_fll_load_cpu0..7`; `PSEUDO_CUR_PROCESSING_RANK_ID` (opcode `0xDB`). *Deep
pages:* [The 8-Core SPMD Execution Model](runtime/spmd-teardown.md), [PseudoCurProcessingRankID](collectives/ops/rank-id.md).

**The install seam (`nrt_set_pool_eng_ucode`)** — The unauthenticated host call that overrides a
Q7 image. The device load path has **no** signature verification — admission is an *unkeyed*
hash + structural ELF/reloc/core-count checks + a ucode semver gate; the trust root is the host
OS/process boundary, not a hardware root of trust. *Anchor:* `nrt_set_pool_eng_ucode` install
seam. *Deep page:* [Firmware Trust Chain + Threat Model](control/security/trust-chain-threat-model.md).

---

## DMA, memory & collectives

<a id="sbuf--psum"></a>
**SBUF / PSUM** — **SBUF** (on-chip State Buffer, `STATE_BUF`) is the shared on-chip memory all
five engines address — SoC base `0x2000000000`, 32 MiB. **PSUM** is the PE-private accumulator
bank — written *only* by PE. **Keystone fact:** the Q7 GPSIMD cores reach SBUF (and HBM) through
an AXI aperture but **cannot address PSUM at all** ("PSUM is structurally unreachable — no AXI
aperture, no NX window"); the compiler routes an int32/uint32 op to GpSimd but **falls back to
the Vector engine if any operand lives in PSUM**. Model GPSIMD as SBUF/HBM-only. *Anchor:*
`STATE_BUF` @ `0x2000000000`; the keystone CC finding ("Since GpSimd Engine cannot access
PSUM…"). *Deep page:* [On-Chip State-Buffer (SBUF) + PSUM Bank Model](dma/sbuf-psum-banks.md).

**RDMA** — The cross-core / cross-die SBUF→SBUF peer-to-peer byte-movement path the collectives
use over the die mesh. *Anchor:* the `rdma_desc_gen` (ExtendedInst sub-op 8) / `start` (sub-op 9)
pair in `remote_copy.cpp`. *Deep page:* [RDMA Cross-Die SBUF→SBUF P2P](dma/rdma-cross-die.md).

**CCE (Compute-DMA / in-transfer compute)** — The SDMA "Compute" datapath that performs the
reduction **inside** the transfer rather than on a compute engine — a reduce-class collective's
arithmetic (and dtype-convert + stochastic rounding) happens in the SDMA fabric in-flight.
`CDMA = DDMA + CCE`. *Anchor:* `SDMA_CCETYPE` + `reduction_type_t` driving `add_dma_packet_cce`
(SDMAOP `CCE = 4`); the `encd_dma_reduce_copy_sb2sb` `0xBF` leg. *Deep page:* [CCE (Compute-DMA) In-Transfer Compute](dma/cce-in-transfer.md).

**`cc_op`** — The collective-communication operation program the NCFW / TOP_SP walks: the
`spad-ctrl` table of per-step descriptors (`cc_op_info{cc_op, op_type, alg, alg_name}`, header
`cc_op == 1` flag) and the `tsync` sequencing that realises a collective. *Anchor:*
`ncfw_log_spad_ctrl_cc_op_entry`; the `"cc_op"` string @ `0x650d4` in `libncfw.so`. *Deep page:* [NCFW spad-ctrl cc_op Table + tsync](collectives/ncfw/spad-ccop-tsync.md).

**Collective / `cc_op` opcodes** — The GPSIMD-relevant collective and barrier opcodes,
byte-grounded in the arch-ISA header `NEURON_ISA_TPB_OPCODE_*` enum:

| byte | name | meaning |
|---|---|---|
| `0x41` | `TENSOR_TENSOR_ARITH_OP` | the int32/uint32 add/sub/mul datapath (GpSimd-native) |
| `0xBF` | `SB2SB_COLLECTIVE` | one SBUF→SBUF collective hop (a ring all-reduce step) |
| `0xC3` | `PSEUDO_DMABARRIER` | DMA barrier |
| `0xC7` | `PSEUDO_TRIGGER_ALL_REDUCE` | all-reduce trigger |
| `0xC8` | `PSEUDO_TRIGGER_COLLECTIVE` | the generic collective trigger |
| `0xCB` | `PSEUDO_SEND_RECV` | point-to-point send/recv |
| `0xD5` | `PSEUDO_SYNC_BARRIER` | sync barrier |
| `0xD8` | `PSEUDO_CORE_BARRIER` | core barrier |
| `0xD9` | `PSEUDO_TRIGGER_COLLECTIVE2` | the extended collective trigger |
| `0xDB` | `PSEUDO_CUR_PROCESSING_RANK_ID` | read this core's PRID/rank |
| `0xF0` | `EXTENDED_INST` | custom-op extended instruction space |

(`ALL_REDUCE = 0x1` is a *collective-type* code on `TriggerCollective`, not an opcode byte.)
*Anchor:* `NEURON_ISA_TPB_OPCODE_*` in `neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h`
(bytes verified directly); `SX-CCL-11` enum report. *Deep page:* [Collective-Type + cc_op Enum Reference](collectives/ops/collective-enums.md).

**SB2SB (opcode `0xBF`)** — "State-Buffer to State-Buffer": the intra-/inter-die collective hop
the GPSIMD POOL engine performs natively (one step of a ring all-reduce). The reduce-class variant
pairs with a CCE descriptor. Handler `decode_sb2sb_collective` (`remote_copy.cpp`); struct
`NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT`. *Anchor:* `NEURON_ISA_TPB_OPCODE_SB2SB_COLLECTIVE = 0xbf`;
`encd_dma_{copy,reduce_copy}_sb2sb`. *Deep page:* [S3D3 Collective (SB2SB, 0xBF)](collectives/ops/s3d3-collective.md).

**XRP** — The host↔DSP messaging transport the collective control plane uses to pass commands
between the host runtime and the device firmware. **Note:** despite the name there is **no
Cadence XRP framework** present in the corpus; the transport is a *bespoke Annapurna-Labs host↔DSP
message queue* (the only `xrp` substring corpus-wide is `libnrt`'s unrelated `AF_RXRPC`/`PF_RXRPC`).
*Anchor:* the XRP host↔DSP messaging report (`SX-CCL-13`). *Deep page:* [XRP Host↔DSP Messaging Transport](collectives/ops/xrp-host-dsp-messaging.md).

**`pring`** — The physical / persistent device-memory DMA-descriptor **ring** the NCFW collective
scheduler builds once (by copying a `vring` template into device memory) and reuses across steps.
*Anchor:* the strings `"Copying vring to pring %s"` / `"vring is copied to pring. ndesc=%u"` in
`libncfw.so`. *Deep page:* [pring (Persistent DMA Descriptor Ring)](collectives/ncfw/pring-descriptors.md).

**`al_udma`** — The Annapurna-Labs micro-DMA hardware engine (the M2S / S2M / GEN descriptor
queues) that physically moves data; the device CSR blocks `udma_m2s` / `udma_s2m` / `udma_gen`
program it. *Anchor:* the `udma_*` CSR blocks; the `al_udma` engine report. *Deep page:* [The al_udma Hardware DMA Engine](dma/udma-hw-engine.md).

**FIS** — The per-fabric-master error-trigger / isolation block (associated with fabric
firewall / RAS-violation routing; the corpus does not spell out the acronym, so this glossary
does not invent one). It routes faults such as APB timeouts into the interrupt fabric via three
error-trigger vectors. *Anchor:* `csrs/fis/fis_control.json` (`apb_timeout → fis_cntrl_intr`),
the `fis_cntrl`/`sprot`/`errtrig` vectors, the per-master `FIS_0` container. *Deep page:* [CSR — FIS Control + errtrig + spad](control/csr/fis-errtrig-spad.md).

---

## Container & compiler

**NEFF (Neuron Executable File Format)** — The on-disk container the host compiler emits and the
runtime loads: a **1024-byte** header (`header_size == 0x400`, **no** literal magic — identified
structurally) followed by an inner gzip→**in-memory tar** (512-byte ustar) of per-engine 64-byte
sequencer-microcode streams, weights, relocation tables, and the `metaneff` descriptor. *Anchor:*
the 1024-byte `neff_header_t` + `archive_read_open_memory` tar in the NEFF container report. *Deep
page:* [NEFF Container Byte Format](neff/container-byte-format.md).

**`metaneff`** — The NEFF's host-side **protobuf** I/O key-ring (`message MetaNeff` /
`MetaTensor`, parsed by `MetaNeff::_InternalParse`): it binds each `MetaTensor[i].name` →
NEFF `var_id i` → device `mem_ref[i]`, mapping host tensors to device SBUF/HBM addresses. It
carries **no** weights. *Anchor:* the `MetaNeff`/`MetaTensor` protobuf messages. *Deep page:* [metaneff Protobuf + var/mem_ref Device I/O ABI](neff/metaneff-io-abi.md).

**BIR / Penguin** — The `neuronx-cc` compiler's "penguin" backend intermediate representation
(`libBIR.so`, ~110 `Inst*` classes). GPSIMD-relevant nodes are `InstCustomOp` (a custom op →
opcode `0xF0`) and `InstCollective` / `InstCollectiveSend` / `InstCollectiveRecv` (a collective →
the `0xC7`/`0xC8`/`0xBF` family). The compiler lowers a traced NKI/HLO graph into BIR, then into
per-engine ISA streams packed into a NEFF. *Anchor:* the `Inst*` roster in `libBIR.so`;
`InstCustomOp` / `InstCollective` node names. *Deep page:* [The Penguin BIR Instruction Set + BIR→ISA Map](compiler/bir-inst-roster.md).

**NKI** — The Neuron Kernel Interface: the Python-level frontend a custom op is authored in; it
lowers (via BIR) into the device opcodes documented here. Its engine enum (`tensor=1, scalar=2,
gpsimd=3, dma=4, vector=5, sync=6`) is **a different space** from the firmware `engine_idx`.
*Anchor:* the NKI frontend / reference-simulator report. *Deep page:* [The NKI Frontend + Reference Simulator](compiler/nki-frontend.md).

**MX (microscaling)** — The block-scaled low-precision (MX-format) dequant/compute path; the
compiler's MX microscaling lowering and the device MX dequant bodies. *Anchor:* the `MXTENSOR_*`
opcodes; the MX path report. *Deep page:* [The MX Microscaling Path](compiler/mx-path.md).

**`SundaISel`** — The compiler instruction-selection pass for the GPSIMD/Sunda lane (the routing
that decides an int op goes to GpSimd vs Vector, enforcing the PSUM fall-back). *Anchor:* the
`SundaISel` pass in the compiler map. *Deep page:* [SundaISel Deep-Dive](compiler/sundaisel.md).

---

## The ISS (executable oracle)

**ISS** — The Instruction-Set Simulator shipped as host config libraries, used in this reference
as a **live value oracle** (driven in-process via `ctypes`). It splits into a *value* lane and a
*cycle* lane. *Deep page:* [The ISS Semantic-Model Synthesis](iss/iss-semantic-synthesis.md).

**`libfiss-base` (the value oracle)** — The fast ISS (FISS): its **864** `module__xdref_*` value
leaves are the per-element value functions of every GPSIMD value opcode (864/864 classified), and
they are **callable in-process with no license**. Running a leaf on a known input and diffing
against a reference model is what makes a value claim "proven-by-execution" (the strongest static
fact). *Anchor:* `nm libfiss-base.so | rg -c module__xdref_` = 864. *Deep page:* [fiss Datapath — the 864-Leaf Value Oracle](iss/fiss-datapath-oracle.md).

**`xdref`** — The naming stem of the value leaves (`module__xdref_<op>_<wout>_<win0>_<win1>`,
e.g. `module__xdref_add_16_16_16`): the per-lane, bit-exact element-arithmetic primitive a vector
op calls once per lane (described as a "cross-decode reference value function"; the suffix is
output width then operand widths). *Anchor:* the `module__xdref_*` family in `libfiss-base.so`.
*Deep page:* [fiss Datapath — the 864-Leaf Value Oracle](iss/fiss-datapath-oracle.md).

**`libcas-core` (the cycle oracle)** — The cycle-accurate ISS (CAS: latency, stall, issue,
fault). It loads and surfaces fine, but instruction *retirement* hits the `AUTH::check_iss_licenses`
gate and halts at "Unable to get license" without a FlexNet key — so cycles, the fault machine,
and single-step are **license-gated** (a `closable-with-license` wall), while the value lane runs
free. *Anchor:* `libcas-core.so` (SHA `7f1d86da…`); the `AUTH::check_iss_licenses` retirement gate
is identified in the cas execution path (not a `.symtab`-exported symbol). *Deep page:* [libcas-core — The Cycle/Pipeline Timing Model](iss/cas-timing-model.md).

**`libctype`** — The CSTUB custom-type helper library (the C-type stub functions the ISS uses to
materialise TIE custom types). *Anchor:* `libctype.so` (SHA `eb79ff9f…`). *Deep page:* [libctype — CSTUB Custom-Type Functions](iss/libctype-cstub.md).

**ref vs production ISS** — The corpus ships paired *production* and *ref* ISS variants
(`libcas-core` ↔ `libcas-ref-core`, `libfiss-base` ↔ `libfiss-ref-base`); the ref variants are
the slower reference model used to cross-validate the production leaves. *Anchor:* the four
library SHAs in the corpus contract. *Deep page:* [ref-vs-production ISS Variant Diff](iss/ref-vs-production-diff.md).

---

## Confidence & methodology

### Confidence tags `<HIGH | MED | LOW>` / `<OBSERVED | INFERRED | CARRIED>`

Every non-trivial claim carries a two-part tag: **confidence** (how much to trust it) crossed
with **provenance** (where it came from). The two axes are orthogonal.

- **OBSERVED** — read directly from a shipped artifact (a byte at a symbol/offset, a section, a
  config token) **or computed by executing the shipped simulator** on a known input. The binary
  is the witness.
- **INFERRED** — reasoned *over* OBSERVED facts by a named deduction (a stride, a structural
  mirror, an absence-implies argument). No single artifact states it.
- **CARRIED** — reused from a cited prior report *at that report's confidence*, without
  re-reading the artifact this pass (one inheritance step from the binary).
- **HIGH** — byte-exact and either directly read, multiply-corroborated, or proven-by-execution;
  encode it as a hard requirement.
- **MED** — sound but single-witness, partially tooling-bounded, or one structural inference;
  use it, flag it, plan to confirm it.
- **LOW** — best-available but materially uncertain (behind a wall, an absent-data substitute, or
  an unproven label); do **not** hard-code it.

A synthesis never raises a claim's confidence: a `MED/INFERRED` fact stays `MED/INFERRED` when a
later page cites it. *Deep page:* [The Confidence & Walls Model](reference/confidence-model.md).

**Wall** — A genuine boundary of static analysis on *this* corpus: a question whose answer cannot
be produced by any further reading or reasoning over the binaries in hand. Each named wall states
its nature, provenance, and **closability** (`closable-with-corpus` / `closable-with-license` /
`closable-with-hardware` / `closable-with-static` / `fundamental`). The headline: **no wall is a
missing datapath body, opcode decode, or value semantics** — every wall is a driver / checkout /
key / capture boundary. The two carried throughout: **`arch_id 36`** (v5, INFERRED,
closable-with-corpus) and **`ct37`** (v5 coretype, OBSERVED — the anchor that bounds it). *Deep
page:* [The Confidence & Walls Model](reference/confidence-model.md).

**OBSERVED-by-execution / proven-by-execution** — The strongest static fact: a value the shipped
`libfiss-base.so` leaf *returned* when called live on a sweep of inputs and matched against a
reference model. ~95% of value-bearing leaves carry such a certificate across ~2.09M comparisons,
with zero firmware value bugs found. *Deep page:* [The Confidence & Walls Model](reference/confidence-model.md).

**Callout markers** — Inline flags used on every page: **QUIRK** (a counter-intuitive but real
behavior to reproduce), **GOTCHA** (a reimplementation/recovery trap), **CORRECTION** (a value
that overturns an earlier or naive reading — state the corrected value, never the superseded one),
**NOTE** (orienting context). *Deep pages:* [How to Read This Guide](reference/how-to-read.md), [The Do-Not-Repeat / Correction Ledger](reference/correction-ledger.md).

**The corpus (13 + 29)** — The substrate: **13** host x86-64 config/runtime libraries (the
`nrtucode` pair, the `libisa`/`libtie`/`libcas`/`libfiss`/`libctype` ISA/sim/TIE libs, `libncfw`,
`libnrtucode_extisa`) and **29** embedded ELF32-Xtensa (`e_machine = 94`) device blobs carried in
their `.rodata`, plus cleartext arch-ISA headers / config / TIE. Each host library is pinned by
SHA-256. *Anchor:* the 13-binary SHA table. *Deep page:* [The Corpus, Tiers & Binary Inventory](reference/corpus-inventory.md).

**The device disassembler** — `XtensaTools/bin/xtensa-elf-objdump` (GNU Binutils 2.34 / Xtensa
Tools 14.09) with `XTENSA_CORE=ncore2gp` — the only core in its registry — is the device-native
oracle for Q7 `.text`. It is correct for FLIX Q7 images and *wrong* for NCFW LX images. *Anchor:*
`--xtensa-core=ncore2gp`; validated on a carved EXTISA `.text`. *Deep page:* [Methodology — How This Was Reverse-Engineered](reference/methodology.md).

---

## See also

- [How to Read This Guide](reference/how-to-read.md) — the 16-Part map, reading paths, and page anatomy.
- [The Confidence & Walls Model](reference/confidence-model.md) — the full tag and wall taxonomy.
- [FLIX Bundle-Decoding Methodology](reference/flix-decoding.md) — the 14-format / 46-slot / 7-length decoder.
- [Codename ↔ Generation Cross-Walk](reference/codename-crosswalk.md) — the codename ↔ coretype ↔ arch_id table.
- [Toolchain Inventory & Versions](reference/toolchain-versions.md) — the toolchain / package / HW-revision axes.
- [Appendix · Abbreviations & Symbol Index](appendix/abbreviations-index.md) — the printable abbreviation card.
