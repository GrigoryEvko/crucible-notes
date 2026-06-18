# Config-Grounded Microarch Reference Sheet

This page is the single quantitative table set for the GPSIMD core. Every microarch *number* a
reimplementer needs — the vector width, the eight register files and their widths/depths, the
FLIX format/slot/length geometry, the functional-unit and coprocessor inventory, the
core-local memory map (IRAM / DataRAM), the MPU entry count, the pipeline depth, and the
load/store and fetch widths — is read straight out of the shipped Cairo configuration and
pinned here with the exact **symbol / offset / header token** it comes from and a confidence
tag. Where a number drives a reimplementation decision, the row says how.

The companion page [Core Identity & Configuration](identity-config.md) owns the *identity*
constants (`ConfigName = Xm_ncore2gp`, `uarchName = Cairo`, `arch = Xtensa24`,
`TargetHWVersion = NX1.1.4`, `SWToolsRelease = RI-2022.9`, the `ConfigKey`s); this page does
**not** restate them — it cross-links them and stays on the *microarch quantities derived from
the config*. The deep encoding mechanics live on
[FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) and
[The Eight Register Files](register-files.md); this sheet is the dense roll-up you keep open
while reimplementing.

Two grounding sources are used, and they are kept distinct:

* **`ncore2gp-params`** — the Tensilica processor-generator parameter text
  (`tools/ncore2gp/config/ncore2gp-params`) and its generated header
  `xtensa/config/core-isa.h`. These are the **config of record**: a `key = value` text the
  build emitted. Cited as `param:<key>` / `core-isa.h:<MACRO>`.
* **`libisa-core.so`** — the shipped TIE-generated ISA DB library
  (`tools/ncore2gp/config/libisa-core.so`, ELF64, **not** stripped, 9,690,712 bytes). Its
  config getters and `.data.rel.ro` tables are the **machine-readable** form the disassembler
  and ISS consume. Cited as `<symbol> @ 0x<addr>`.

Every getter immediate, table byte, and header token below was **re-read from those two
artifacts this pass** — `objdump -d` on the getter bodies, `objdump -s` on the raw table
bytes, `rg` on the two config texts. No external or vendor source tree was consulted; this is
binary/config-derived prose only. `[HIGH/OBSERVED]` throughout except where a row is tagged
otherwise.

> **GOTCHA — the `.data.rel.ro` VMA↔file-offset delta is `0x200000` in this binary.** The
> `regfiles`/`funcUnits` tables live in `.data.rel.ro` (VMA base `0x67bb00`, file `0x47bb00`);
> `.rodata` (where `length_table`, `formats`, `slots`, and all the string pools sit) is
> VMA == file-offset. Before `objdump -s`-ing a `.data.rel.ro` struct, subtract `0x200000`;
> `.rodata` needs no adjustment. Measured with `readelf -SW libisa-core.so` this pass.

---

## 1. Top-line microarch identity (one row each)

These are the headline scalars. The identity tokens (`Cairo`, `Xtensa24`, `NX1.1.4`) are on
[identity-config.md](identity-config.md); repeated here only as the **microarch-shaping**
quantities they imply.

| Parameter | Value | Source | Tag | Why a reimplementer cares |
|---|---|---|---|---|
| Vector / SIMD width | **512 bits** (`SIMD16` × 32-bit, 2N×8 = 64 bytes) | `core-isa.h:XCHAL_VISION_SIMD16 = 32`; `vec` width @ `regfiles[2]` | HIGH/OBS | the native lane count: a vector op is 64 B/cycle; everything downstream sizes to 512 b |
| Vision generation | **Q7** (`sp_vfpu`, `hp_vfpu`, `2xfmac`, quad-MAC) | `core-isa.h:XCHAL_VISION_TYPE = 7` | HIGH/OBS | selects the IVP feature set (2×FMAC, dual-quad 8×8 MAC, SuperGather) |
| Endianness | **little-endian** | `param:IsaIsBigEndian = 0`; `core-isa.h:XCHAL_HAVE_BE = 0` | HIGH/OBS | byte→lane packing for the decoder; `insn[w] |= byte[i] << 8*(i&3)` |
| Max instruction (fetch) size | **32 bytes** | `param:IsaMaxInstructionSize = 32`; `core-isa.h:XCHAL_MAX_INSTRUCTION_SIZE = 32` | HIGH/OBS | the fetch window; FLIX *bundles* are 2/3/8/16 — the 32 is the buffer, not a bundle len |
| Instruction-fetch width | **256 bits** (32 B) | `param:InstFetchWidth = 256`; `core-isa.h:XCHAL_INST_FETCH_WIDTH = 32` | HIGH/OBS | one fetch grabs a full 16-byte wide bundle plus headroom |
| Load/store data width | **512 bits** (64 B) | `param:LoadStoreWidth = 512` | HIGH/OBS | one vector ld/st moves a full 512-bit `vec` in one access |
| Hardware contexts | **1** | `param:IsaNumContexts = 1` | HIGH/OBS | single-context core; no HW thread interleave to model |
| Cores per config | **1** (×8 SPMD per NeuronCore) | `param:NumOfCores = 1` | HIGH/OBS | the config is one core; the POOL cluster instantiates it 8× (PRID 0..7) |
| Physical address width | **32 bits** | `param:PhysicalAddressWidth = 32`; `param:PC_Width = 32` | HIGH/OBS | 4 GiB device address space; pointers are 32-bit on the Q7 |
| Byte-enable width | **16** (one bit per 512-bit/16-byte… ) | `param:ByteEnableWidth = 16` | HIGH/OBS | partial-write granularity on the bus |
| `interface_version` (libisa ABI) | **118** (`0x76`) | `interface_version @ 0x3b5b20` | HIGH/OBS | the Xtensa libisa table-ABI rev a reimplementation of the getters must match |

The single most consequential scalar is **512-bit `vec`**: it is the SIMD register width, the
load/store width, and (×32 lanes of 16 bit) the `SIMD16` factor — three independent config
tokens (`XCHAL_VISION_SIMD16`, the `vec` regfile `num_bits`, `LoadStoreWidth`) that all agree
on 512.

---

## 2. The eight register files

`num_regfiles` is **8** — the getter body is `mov $0x8,%eax; ret` at `num_regfiles @
0x3b5c20`, re-disassembled this pass. The authoritative table is `regfiles @ 0x74a800`
(`.data.rel.ro`, file `0x54a800`), **stride 56 bytes** (the `regfile_name` accessor computes
`rdi*64 − rdi*8 = rdi*56`). Each entry is `{name, shortname, package, num_bits, num_entries,
num_callee_saved, flags, ctype, coproc}`. Two files are core/scalar (`AR`, `BR`); six are the
Vision-Q7 SIMD coprocessor files. Every width/count below is read **byte-exact** from the raw
`objdump -s` of the table this pass.

| idx | name | short | width (bits) | count | total state | package | coproc | flags | role / reimpl note |
|---|---|---|---|---|---|---|---|---|---|
| 0 | **`AR`** | `a` | 32 | 64 | 2 KiB | `xt_xtensa` | — (core) | `0x05` | windowed scalar address/general file; FLIX slots address a 4-bit `a0..a15` subset |
| 1 | **`BR`** | `b` | 1 | 16 | 16 b | `xt_booleans` | — (core) | `0x05` | scalar boolean file; the 4 views in §3 are sub-views of it |
| 2 | **`vec`** | `v` | **512** | 32 | 16 KiB | `xt_ivp32` | Vision | `0x05` | the SIMD vector file (32×16-bit lanes); FLIX `v` operand fields are 5-bit |
| 3 | **`vbool`** | `vb` | 64 | 16 | 1 KiB | `xt_ivp32` | Vision | `0x05` | per-lane SIMD predicate/mask file |
| 4 | **`valign`** | `u` | 512 | 4 | 2 KiB | `xt_ivp32` | Vision | `0x05` | alignment state primed before an unaligned vector load |
| 5 | **`wvec`** | `wv` | **1536** | 4 | 6 KiB | `xt_ivp32` | Vision | `0x05` | wide-MAC accumulators (3× a `vec`); `wv` operand fields are 2-bit |
| 6 | **`b32_pr`** | `pr` | 64 | 16 | 1 KiB | `xt_ivp32` | Vision | `0x05` | 64-bit predicate/pack file (gather/select) |
| 7 | **`gvr`** | `gr` | 512 | 8 | 4 KiB | `xt_ivp32` | Vision | `0x0d` | SuperGather index/descriptor file; `gr` operand fields are 3-bit |

`[HIGH/OBSERVED]` on every cell. Raw-byte spot-checks this pass: `vec` @ `0x74a888` reads
`00 02 00 00` (`num_bits = 0x200 = 512`) `20 00 00 00` (`num_entries = 0x20 = 32`); `wvec` @
`0x74a930` reads `00 06 00 00` (`0x600 = 1536`) `04 00 00 00`; `gvr` @ `0x74a9a0` reads
`00 02 00 00` (512) `08 00 00 00` (8) and `flags = 0x0d`. The bytes after entry 7 (`0x74a9d8+`)
are zero — confirming exactly 8 entries, consistent with the `num_regfiles = 8` getter.

**Three quantities a reimplementer hard-codes from this table:**

* **`vec` = 512 b × 32** is the SIMD datapath. A reimplemented decoder sizes a `v` operand
  field at **5 bits** (`log2 32`) inside every slot it appears in.
* **`wvec` = 1536 b × 4** is the wide accumulator the quad/paired integer MACs write
  (`ivp_mulnx16`, `ivp_mulpnx16`); a `wv` operand is **2 bits** (`log2 4`). 1536 = 3 × 512, the
  widening readout width.
* **`gvr` = 512 b × 8** is the SuperGather descriptor file (`gr` operand = **3 bits**); its
  count of **8** is independently the `GS_GatherRegs = 8` ScatterGather param (§6).

> **NOTE — `num_callee_saved = 0` for all eight files.** Every register file in this config is
> caller-saved (the `+0x20` field is zero in all 8 rows). A reimplemented ABI/context-save
> layer spills *nothing* on a call boundary by regfile convention; the windowed `AR` file uses
> the Xtensa register-window rotation instead. `[HIGH/OBSERVED]`

> **QUIRK — `gvr` carries flag bit `0x08` (flags `0x0d` vs `0x05` on the other seven).** The
> bit is real and unique to `gvr`; its *meaning* (a "global / state" attribute, consistent with
> the `gsr` ctype and the SuperGather descriptor role) is `[MED/INFERRED]` — there is no
> flag-name table in the binary to decode it. The width/count/ctype of `gvr` are
> `[HIGH/OBSERVED]`; only the flag-bit *semantics* are inferred.

### 2.1 Register operand field widths (derived)

The FLIX slot operand-field width for each register class is `ceil(log2(count))`, the bits a
slot needs to name one register of that file. A reimplemented field extractor uses these
directly:

| file | count | operand-field width | confirmed against |
|---|---|---|---|
| `AR` (FLIX subset) | 16 of 64 | **4 bits** | `ivp_lv2nx8_i` `ars` field `slotbits[0..3]` |
| `vec` | 32 | **5 bits** | `ivp_gatheranx16` `vs` field `slotbits[9..13]` |
| `gvr` | 8 | **3 bits** | `ivp_gatheranx16` `gt` field `slotbits[4..6]` |
| `wvec` | 4 | **2 bits** | `ivp_mulnx16` `wvt` field `slotbits[19..20]` |

`[HIGH/OBSERVED]` on the widths (they equal `log2 count`); the *bit positions* are
`[HIGH/OBSERVED]` per slot from the field-get thunks. Full FLIX outside the address-window: the
`AR` file is 64 entries but FLIX slots compress to a 4-bit `a0..a15` subset (the standard
Xtensa FLIX AR-compression) — outside FLIX, base/scalar formats reach all 64 via 6-bit fields.

---

## 3. Register-file views (BR sub-views)

`num_regfile_views` is **4** (`mov $0x4,%eax; ret` @ `num_regfile_views @ 0x3b5d50`). The
table is `regfile_views @ 0x74a780`, **stride 32 bytes** (`regfile_view_name` computes
`rdi*32`), entry `{name, parent, num_bits, ctype}`. All four are narrowed views of the single
`BR` boolean file — there are **no** views over any SIMD file.

| idx | view | parent | width (bits) | role |
|---|---|---|---|---|
| 0 | `BR2` | `BR` | 2 | boolean-pair view |
| 1 | `BR4` | `BR` | 4 | boolean-quad view |
| 2 | `BR8` | `BR` | 8 | boolean-octet view |
| 3 | `BR16` | `BR` | 16 | the full 16×1-bit file as one 16-bit value |

`[HIGH/OBSERVED]`. A reimplementer treats these as *alternate operand encodings of the same
silicon* (the 16×1-bit `BR` file), **not** as four extra register files. The libisa
introspection ABI (`xtensa_isa_num_regfiles`) reports **12** — that count is `8 regfiles + 4
views` flattened into one namespace; the **canonical microarch count is 8 files + 4 views**,
not 12. Do not double-count the views as silicon.

---

## 4. FLIX format / slot / length geometry

The VLIW encoding is **FLIX** — 14 formats, 46 slots, 7 length-class outcomes collapsing to 4
distinct byte-lengths `{2, 3, 8, 16}`. All three counts are byte-verified getter immediates
this pass. The full decode mechanics (the `format_decoder` predicate ladder, the per-slot
gather thunks, the worked oracle-validated example) are on
[FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md); this section is the
quantitative roll-up.

| Quantity | Value | Source | Tag |
|---|---|---|---|
| `num_formats` | **14** (`0x0e`) | `num_formats @ 0x3b65e0` → `mov $0xe,%eax` | HIGH/OBS |
| `num_slots` | **46** (`0x2e`) | `num_slots @ 0x3b6510` → `mov $0x2e,%eax` | HIGH/OBS |
| length-class outcomes | **7** → byte-lengths **`{2, 3, 8, 16}`** (+ illegal `-1`) | `length_table[256] @ 0x3d4100` (`.rodata`) | HIGH/OBS |
| `formats[]` table | base `0x6cd980`, **stride 24**, 14 entries | `format_name @ 0x3b65f0` (`lea (rdi,rdi,2); *8`) | HIGH/OBS |
| `slots[]` table | base `0x6cdb00`, **stride 48**, 46 entries | `slot_name @ 0x3b6520` (`lea (rdi,rdi,2); shl $4`) | HIGH/OBS |
| widest co-issue | **5 slots** (formats `F3`, `F11`) | format slot rosters below | HIGH/OBS |

The 14 formats partition into **3 scalar** (one slot each), **8 wide** (16 byte / 4–5 slots),
and **3 narrow** (8 byte / 2–4 slots). The per-format roster and slot count, byte-exact from
the table:

| idx | name | len (B) | slots | issue profile (S0…Sn) | class |
|---|---|---|---|---|---|
| 0 | `x24` | 3 | 1 | `Inst` (24-bit RRR core op) | scalar |
| 1 | `x16a` | 2 | 1 | `Inst16a` (density) | scalar |
| 2 | `x16b` | 2 | 1 | `Inst16b` (density) | scalar |
| 3 | `F0` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 4 | `F11` | 16 | **5** | Ld · ALU · Mul · ALU · ALU | wide |
| 5 | `F1` | 16 | 4 | LdStALU · Ld · Mul · ALU | wide |
| 6 | `F2` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 7 | `F3` | 16 | **5** | LdSt · Ld · Mul · ALU · ALU | wide |
| 8 | `F4` | 16 | 4 | Ld · Ld · Mul · ALU (dual-load) | wide |
| 9 | `F6` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 10 | `F7` | 16 | 4 | LdSt · Ld · Mul · ALU | wide |
| 11 | `N1` | 8 | 3 | LdSt · None · Mul | narrow |
| 12 | `N2` | 8 | 2 | LdSt · Ld | narrow |
| 13 | `N0` | 8 | 4 | LdSt · None · None · ALU | narrow |

Slot-count census: `1+1+1+4+5+4+4+5+4+4+4+3+2+4 = 46 = num_slots` — the 46 cross-checks the
getter. The two 5-slot formats (`F3`, `F11`) are the maximum-issue layouts; `F4`/`F11` carry
**no** store slot (dual-load / ALU-heavy specializations). The `Fn` numbering has real gaps
(no `F5`/`F8`/`F9`/`F10`). **For the byte-3 sub-format decode** (why six `op0==0xF` formats are
16 bytes despite the static `op0`-only macro saying 8) see
[flix-decoding §2/§4](../../reference/flix-decoding.md). `[HIGH/OBSERVED]`

The four byte-lengths and the `length_table` (the `b3lo == 0` row literal bytes `03 03 03 03 03
03 03 03 02 02 02 02 02 02 10 08` = `{3×8, 2×6, 16, 8}`, re-read this pass) drive the **one**
sweep-advance decision: a reimplemented linear decoder advances the cursor by `length_table[((
byte3 & 0xF) << 4) | (byte0 & 0xF)]`, never by `op0` alone.

### 4.1 The co-issue ceiling — `1 + 1` is the sound bound

A FLIX format declares up to 5 *slots*, but the question a scheduler asks is *how many
operations co-issue per functional unit per cycle*. The functional-unit reservation that would
answer this — the `MODULE_SCHEDULE` per-port matrices — is **empty** in this config (no
per-port reservation table is populated). With the reservation data absent, the **sound,
non-speculative bound** is the structural one the format itself states: per the single
`XT_LOADSTORE_UNIT` with **`num_copies = 2`** (§5), at most **two memory ops** (the LdSt + Ld
slots of a wide format) and the format's Mul + ALU slots co-issue — i.e. **1 load/store-class
op + 1 of each other class per slot the format declares**, capped by the 2 load/store unit
copies. Do **not** infer a tighter per-cycle throughput than the format roster + the 2 LSU
copies allow; the empty `MODULE_SCHEDULE` means a per-port hazard model is **not** recoverable
from this corpus, so the `1+1` co-issue ceiling (two memory slots, one per other class) is the
ceiling to encode. `[HIGH/OBSERVED on the slot rosters and the 2 LSU copies; the absence of a
tighter per-port model is OBSERVED-negative — `MODULE_SCHEDULE` is empty.]`

---

## 5. Functional-unit & coprocessor inventory

The config declares **exactly one** functional unit and **exactly one** coprocessor — both
getter immediates and both table bodies re-read byte-exact this pass.

| Quantity | Value | Source | Tag |
|---|---|---|---|
| `num_funcUnits` | **1** | `num_funcUnits @ 0x3b5bd0` → `mov $0x1,%eax` | HIGH/OBS |
| funcUnit[0] name | **`XT_LOADSTORE_UNIT`** | `funcUnits @ 0x74a9c0` +0 → str `0x3cd0d3` | HIGH/OBS |
| funcUnit[0] `num_copies` | **2** | `funcUnits @ 0x74a9c0` +8 = `02 00 00 00` | HIGH/OBS |
| `num_coprocs` | **1** | `num_coprocs @ 0x3b6dc0` → `mov $0x1,%eax` | HIGH/OBS |
| coproc[0] name | **`Vision`** | `coprocs @ 0x67bb00` +0 → str `0x3bb80b` | HIGH/OBS |
| coproc[0] `number` | **1** (CP1) | `coprocs @ 0x67bb00` +8 = `01 00 00 00` | HIGH/OBS |

The `funcUnits` table is stride-16 (`funcUnit_name` does `shl $0x4`), and only entry 0 is
populated — the bytes after it (`0x74a9d0+`) are zero. The `coprocs` table is likewise stride-16
with one populated entry. So a reimplementation models **one** coprocessor (`Vision`, the IVP32
SIMD package, CP1) and **one** shared functional unit (`XT_LOADSTORE_UNIT`) with **two copies**
— the 2 copies are exactly why a wide FLIX format can present two memory slots (LdSt + Ld) that
co-issue (§4.1). `[HIGH/OBSERVED]`

> **NOTE — the single coprocessor governs `CPENABLE`.** `XCHAL_HAVE_CP = 1` and
> `XCHAL_CP_MAXCFG = 7` (`core-isa.h`), but only **CP1 = `Vision`** is populated; a
> reimplemented `CPENABLE` model needs exactly one coprocessor-enable bit (CP1). All six SIMD
> register files (§2) bind to this one coprocessor. `[HIGH/OBSERVED]`

The ISA is **stock Cadence** — `num_opcodes` is **1534** (`0x5fe` @ `num_opcodes @ 0x3b61d0`),
**zero** of which are user-defined (every opcode belongs to a Tensilica package; `xt_ivp32` =
1072 of them). There is no bespoke AWS TIE opcode group; the GPSIMD-custom layer is firmware +
host-side routing, not new silicon opcodes. Two further DB scalars verified this pass:
`num_states = 81` (`0x51`), `num_sysregs = 34` (`0x22`), `num_protos = 3484` (`0xd9c`).

---

## 6. SuperGather geometry

SuperGather (`XCHAL_HAVE_SUPERGATHER = 1`) is the headline Q7 gather/scatter engine; its
geometry is config-pinned and drives the `gvr`/`b32_pr` register counts.

| Parameter | Value | Source | Tag | Reimpl note |
|---|---|---|---|---|
| Gather registers | **8** | `param:GS_GatherRegs = 8` | HIGH/OBS | == `gvr` count → `gr` operand = 3-bit |
| Scatter registers | **2** | `param:GS_ScatterRegs = 2` | HIGH/OBS | scatter-descriptor file depth |
| Elements per cycle | **32** | `param:GS_ElementsPerCycle = 32` | HIGH/OBS | = SIMD16×2 = a full 64-byte vector gathered/cycle |
| Unaligned support | **0** | `param:GS_Unalign = 0` | HIGH/OBS | gather addresses are aligned; no unaligned-gather path |

The **8 gather registers** = the `gvr` file count (§2), and the gather op carries exactly one
hidden state operand (the FAST9/SuperGather select state). The `elementsPerCycle = 32` is the
throughput a reimplemented gather model assumes: one 512-bit `vec` worth of gathered bytes per
cycle. `[HIGH/OBSERVED]`

---

## 7. Core-local memory geometry (IRAM / DataRAM + the address map)

The Q7 core has **one** instruction RAM and **one** data RAM, both 64 KiB, at fixed
core-local virtual addresses. This is the *core-local* address map — distinct from the
NeuronCore-level SBUF/PSUM SoC map (which is reached over the AXI aperture; see
[the glossary's SBUF/PSUM entry](../../glossary.md#sbuf--psum) and
[Memory Model](../../topics/memory-model.md)).

| Memory | Count | Size | VA base | PA base | Banks / sub-banks | Access width | Latency | iDMA |
|---|---|---|---|---|---|---|---|---|
| **InstRAM0** (IRAM) | 1 | **64 KiB** (`0x10000`) | `0x00000000` | `0x00000000` | 1 / 1 | 256 b | **3** | no |
| **DataRAM0** | 1 | **64 KiB** (`0x10000`) | `0x00080000` | `0x00080000` | **4** / **8** | 512 b | **4** | yes |

Sources: `core-isa.h:XCHAL_NUM_INSTRAM = 1` / `XCHAL_NUM_DATARAM = 1`;
`XCHAL_INSTRAM0_{VADDR=0x0, SIZE=65536}`; `XCHAL_DATARAM0_{VADDR=0x80000, SIZE=65536, BANKS=4}`;
`param:InstRAM0Latency = 3`, `param:DataRAM0Latency = 4`;
`param:ISSDataRAMInfo = [ 0x10000 0x00080000 512 … ]` (size, base, access-width),
`param:ISSInstRAMInfo = [ 0x10000 0x00000000 256 … ]`; `param:ISSDataRAMBanks = 4`,
`param:ISSDataRAMSubBanks = 8`. `[HIGH/OBSERVED]`

**The core-local address map a reimplementer pins:**

| Region | VA range | Note |
|---|---|---|
| IRAM | `0x00000000 … 0x0000FFFF` | also the reset/`VECBASE` region (`XCHAL_VECBASE_RESET_VADDR = 0x0`) |
| DataRAM | `0x00080000 … 0x0008FFFF` | the 4-bank / 8-sub-bank data RAM; DMA-capable |
| Interrupt stack base (ISB) | `0x0008FED0` | `core-isa.h:XCHAL_ISB_VADDR`; sits at the top of DataRAM |
| Reset vector 0 | `0x00000000` | `XCHAL_RESET_VECTOR0_VADDR`; the boot entry |
| Reset vector 1 | `0x00100000` | `XCHAL_RESET_VECTOR1_VADDR` (static vector base 1) |

> **NOTE — there is no data cache; the I-cache is the only cache.** `DataCacheBytes = 0`
> (`XCHAL_DCACHE_SIZE = 0`); the I-cache is **16 KiB**, 4-way, 64-byte lines, 2 banks
> (`param:InstCacheBytes = 16384` / `WayCount = 4` / `LineBytes = 64` / `Banks = 2`;
> `core-isa.h:XCHAL_ICACHE_SIZE = 16384`). A reimplemented memory model treats data accesses as
> uncached RAM/AXI and models only the I-cache. The L1S/L1V/L2/DataCache config blocks are all
> zero. `[HIGH/OBSERVED]`

> **NOTE — iDMA: one channel, 64-byte max descriptor.** `param:iDMA = 1`,
> `iDMANumChannels = 1`, `iDMAMaxDescriptorSize = 64` (`core-isa.h:XCHAL_IDMA_DESC_SIZE = 64`),
> `iDMAMaxOutstandingReq = 32`, `iDMAAddrWidth = 32`. Only DataRAM0 is iDMA-capable
> (`XCHAL_DATARAM0_HAVE_IDMA = 1`; IRAM is not). `[HIGH/OBSERVED]`

---

## 8. MPU (memory-protection unit)

The core has an **MPU**, not an MMU (`XCHAL_HAVE_MPU = 1`, `MMU configured = 0`). The entry
counts are config-pinned.

| Parameter | Value | Source | Tag |
|---|---|---|---|
| MPU present | **yes** | `core-isa.h:XCHAL_HAVE_MPU = 1`; `param:MPU[configured] = 1` | HIGH/OBS |
| Foreground entries (SW-writable via `WPTLB`) | **16** | `core-isa.h:XCHAL_MPU_ENTRIES = 16`; `param:MPU[fg] = 16` | HIGH/OBS |
| Background map entries | **2** | `param:MPU[bg] = 2` | HIGH/OBS |
| MTU entries | **0** | `param:MPU[mtu] = 0` | HIGH/OBS |
| VA-start LSB (region granularity) | bit **12** (4 KiB) | `param:MPU[va_lsb] = 12` | HIGH/OBS |
| Entry/VECBASE lock | **disabled** | `param:MPU[lock] = 0` | HIGH/OBS |
| MMU | **none** | `param:MMU[configured] = 0` | HIGH/OBS |

The two **background map** entries (the reset-time default protection map) are, byte-exact from
`param:MPU`:

| # | VA start | size | access-rights | memory-type |
|---|---|---|---|---|
| 0 | `0x00000000` | `0x80000000` (2 GiB) | `0x07` | `0x06` |
| 1 | `0x80000000` | `0x80000000` (2 GiB) | `0x07` | `0x06` |

So a reimplemented protection model exposes **16 software-writable foreground MPU entries** at
**4 KiB** granularity, over a 2-entry background map that splits the 4 GiB space in half with
identical rights. `[HIGH/OBSERVED]`. (`PMPPMA[configured] = 0` — no separate PMP/PMA block.)

---

## 9. Pipeline depth

The pipeline depth is config-stated two ways, on two different axes — keep them distinct.

| Quantity | Value | Source | Tag | Meaning |
|---|---|---|---|---|
| ISA pipeline length (`num_pipe_stages`) | **15** | `libisa-core.so` ISA-DB (`xtensa_isa_num_pipe_stages`) | MED/OBS | the full TIE-declared pipeline length used for hazard/latency modeling |
| ISS model stages (B/E/M/W) | **3 / 4 / 5 / 6** | `param:ISSPipeBStage/EStage/MStage/WStage` | HIGH/OBS | the ISS's coarse 4-stage timing model (begin/execute/mem/writeback) |

The **15-stage** figure is the ISA DB's `xtensa_isa_num_pipe_stages` (the per-opcode latency
tables the cycle-accurate ISS consumes); it is `[MED/OBSERVED]` here because it is read through
the libisa introspection ABI rather than re-disassembled to a single immediate this pass. The
**B=3 / E=4 / M=5 / W=6** figures are the ISS's own coarse pipe-stage parameters, read
byte-exact from `ncore2gp-params` — these are the stages the value-lane model uses, *not* the
silicon's full 15-stage depth. A reimplemented timing model uses the 15-stage figure for
latency; the B/E/M/W stages only matter to match the ISS oracle's coarse model.

> **GOTCHA — cycle/latency modeling is license-gated.** The full pipeline/latency behavior
> lives in the cycle-accurate ISS (`libcas-core.so`), whose *retirement* path hits an
> `AUTH::check_iss_licenses` gate without a FlexNet key — so the per-stage **cycle counts**
> behind the 15-stage depth are a `closable-with-license` wall. The **structural** depth (15
> stages) and the **value** semantics (via `libfiss-base.so`) are recoverable; the timing
> *numbers* are not. See the [glossary ISS entries](../../glossary.md#the-iss-executable-oracle).
> `[HIGH/OBSERVED on the structural figures; the cycle counts are behind the license wall.]`

---

## 10. Branch prediction & loop buffer (config-stated)

Two more config-pinned microarch quantities a cycle-level reimplementation needs:

| Parameter | Value | Source | Tag |
|---|---|---|---|
| BTB present | **yes** | `param:BTB[configured] = 1` | HIGH/OBS |
| BTB entries | **128** | `param:BTB[entries] = 128` | HIGH/OBS |
| BTB associativity | **4-way** | `param:BTB[assoc] = 4` | HIGH/OBS |
| BTB tag bits | **22** | `param:BTB[tagbits] = 22` | HIGH/OBS |
| Return-address-stack entries | **8** | `param:BTB[ras] = 8` | HIGH/OBS |
| Zero-overhead loop buffer | **128** | `param:LoopBufferSize = 128`; `core-isa.h:XCHAL_LOOP_BUFFER_SIZE = 128` | HIGH/OBS |
| Branch-predicted classes | CallX, JX, Loops, Return-Jumps | `param:BTB[…]` | HIGH/OBS |

`[HIGH/OBSERVED]`. The BTB predicts CallX/JX/zero-overhead-loops/return-jumps; the 8-entry RAS
backs the windowed call/return. The 128-instruction loop buffer holds short zero-overhead loops
without re-fetch.

---

## 11. Bus / interrupt scalars (config-stated)

Rounding out the config-pinned quantities; these matter for a system-level reimplementation
but not for ISA decode:

| Parameter | Value | Source | Tag |
|---|---|---|---|
| PIF read/write data bits | **128 / 128** | `param:PIFReadDataBits / PIFWriteDataBits` | HIGH/OBS |
| PIF bridge / bus | **AXI** (AXI4 + ACE-lite) | `param:PIFBridgeType = AXI`; `param:AceLite = 1` | HIGH/OBS |
| Interrupt count | **37** | `param:InterruptCount = 37` | HIGH/OBS |
| Max interrupt level | **7** | `param:InterruptLevelMax = 7` | HIGH/OBS |
| External interrupts | **25** | `param:InterruptExtCount = 25` | HIGH/OBS |
| Timers | **3** | `param:TimerCount = 3` (interrupts 28/29/30) | HIGH/OBS |
| `MISC` registers | **2** | `param:NumMiscRegs = 2` | HIGH/OBS |
| Vector base relocatable | **yes** | `param:RelocatableVectors = 1` | HIGH/OBS |

`[HIGH/OBSERVED]`. The exception model is XEA3 with a single unified DispatchVector
(`numOfVectors = 0`); the deep treatment of the 37 interrupts / 7 levels is on the
[XEA3 interrupt architecture page](identity-config.md) (cross-linked from identity-config). The
`PhysicalAddressWidth = 32` (§1) bounds the AXI aperture at 4 GiB.

---

## 12. Master quantity table (the one-screen roll-up)

| Parameter | Value | Symbol / token | Tag |
|---|---|---|---|
| Vector / SIMD width | **512 b** | `XCHAL_VISION_SIMD16 = 32`; `vec` num_bits | HIGH/OBS |
| Register files | **8** | `num_regfiles @ 0x3b5c20` = `0x8` | HIGH/OBS |
| Register-file views | **4** (BR sub-views) | `num_regfile_views @ 0x3b5d50` = `0x4` | HIGH/OBS |
| `AR` / `vec` / `wvec` / `gvr` widths | 32 / 512 / 1536 / 512 b | `regfiles @ 0x74a800` | HIGH/OBS |
| `vec` / `gvr` / `wvec` counts | 32 / 8 / 4 | `regfiles @ 0x74a800` | HIGH/OBS |
| FLIX formats | **14** | `num_formats @ 0x3b65e0` = `0xe` | HIGH/OBS |
| FLIX slots | **46** | `num_slots @ 0x3b6510` = `0x2e` | HIGH/OBS |
| FLIX length-classes → byte-lengths | **7 → {2,3,8,16}** | `length_table @ 0x3d4100` | HIGH/OBS |
| Max co-issue | **5 slots** (`F3`/`F11`); 1+1 sound ceiling | format rosters; empty `MODULE_SCHEDULE` | HIGH/OBS |
| Functional units | **1** (`XT_LOADSTORE_UNIT`, 2 copies) | `num_funcUnits @ 0x3b5bd0` = `0x1` | HIGH/OBS |
| Coprocessors | **1** (`Vision`, CP1) | `num_coprocs @ 0x3b6dc0` = `0x1` | HIGH/OBS |
| Opcodes | **1534** (0 user-defined) | `num_opcodes @ 0x3b61d0` = `0x5fe` | HIGH/OBS |
| IRAM | **64 KiB @ `0x0`** | `XCHAL_INSTRAM0_SIZE = 65536` | HIGH/OBS |
| DataRAM | **64 KiB @ `0x80000`**, 4 banks | `XCHAL_DATARAM0_{SIZE,BANKS}` | HIGH/OBS |
| Load/store width | **512 b** | `param:LoadStoreWidth = 512` | HIGH/OBS |
| Instruction-fetch width | **256 b (32 B)** | `param:InstFetchWidth = 256` | HIGH/OBS |
| MPU foreground / background entries | **16 / 2** | `XCHAL_MPU_ENTRIES = 16`; `param:MPU` | HIGH/OBS |
| MMU | **none** | `param:MMU = 0` | HIGH/OBS |
| Pipeline depth | **15 stages** (ISS B/E/M/W = 3/4/5/6) | ISA-DB `num_pipe_stages`; `param:ISSPipe*` | MED/OBS · HIGH/OBS |
| I-cache | **16 KiB**, 4-way, 64 B lines | `XCHAL_ICACHE_SIZE = 16384` | HIGH/OBS |
| D-cache | **none** | `XCHAL_DCACHE_SIZE = 0` | HIGH/OBS |
| SuperGather gather/scatter regs | **8 / 2**, 32 elem/cycle | `param:GS_GatherRegs/ScatterRegs/ElementsPerCycle` | HIGH/OBS |
| BTB / RAS / loop buffer | **128 / 8 / 128** | `param:BTB[…]`, `LoopBufferSize` | HIGH/OBS |

Every value in this roll-up is `[HIGH/OBSERVED]` except the **15-stage pipeline depth**
(`[MED/OBSERVED]` — read through the introspection ABI, not a single re-disassembled immediate;
the cycle counts behind it are license-walled) and the **`gvr` flag-bit `0x08` semantics**
(`[MED/INFERRED]`; the width/count/role are HIGH).

---

## 13. Adversarial self-verification (re-read from the binary this pass)

Five of the strongest quantities, re-derived from the artifacts directly:

1. **`num_regfiles = 8`** — `objdump -d` at `0x3b5c20` is literally `b8 08 00 00 00`
   (`mov $0x8,%eax`) `c3` (`ret`). The table at `0x74a800` has 8 populated 56-byte entries and
   zeros after. **Confirmed.**
2. **`vec` = 512 b × 32** — raw `objdump -s` at `0x74a888` reads `00 02 00 00` (`num_bits =
   0x200 = 512`) then `20 00 00 00` (`num_entries = 0x20 = 32`). **Confirmed byte-exact.**
3. **`num_formats = 14`, `num_slots = 46`** — `0x3b65e0` is `mov $0xe,%eax`; `0x3b6510` is
   `mov $0x2e,%eax`. The 14 per-format slot counts sum to 46 (`1+1+1+4+5+4+4+5+4+4+4+3+2+4`).
   **Confirmed both ways.**
4. **`num_funcUnits = 1` / `XT_LOADSTORE_UNIT` / `num_copies = 2`** — `0x3b5bd0` is
   `mov $0x1,%eax`; `funcUnits @ 0x74a9c0` reads name-ptr `0x3cd0d3` → the ASCII
   `XT_LOADSTORE_UNIT` and `+8 = 02 00 00 00`. `num_coprocs = 1`, `coprocs @ 0x67bb00` →
   `Vision`, `number = 1`. **Confirmed.**
5. **`length_table` row 0 + the four byte-lengths** — `objdump -s` at `0x3d4100` reads
   `03 03 03 03 03 03 03 03 …` (the `b3lo == 0` row: `op0 0..7 → 3`, `8..D → 2`, `E → 0x10 =
   16`, `F → 0x08 = 8`). The four distinct lengths `{2, 3, 8, 16}` are the literal table
   values. **Confirmed.**

All five re-reads agree with the tables above. The `.data.rel.ro` delta (`0x200000`) was
applied to the `regfiles`/`funcUnits` reads; `.rodata` reads (`length_table`, the string pools)
needed none — verified with `readelf -SW` this pass.

---

## 14. Cross-references

* [Core Identity & Configuration](identity-config.md) — the identity constants (`Cairo`,
  `Xtensa24`, `NX1.1.4`, the `ConfigKey`s) this sheet deliberately does not restate.
* [The Eight Register Files](register-files.md) — the per-register-file deep treatment (dbnums,
  the `gvr`-vs-`tie.h` discrepancy, the view semantics).
* [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) — the format/slot/length
  decode mechanics this section rolls up quantitatively.
* [The FLIX VLIW Encoding](flix-encoding.md) — the 14-format / 46-slot / byte-3 sub-format map
  in full.
* [ctype / coproc / funcUnit Tables](ctype-coproc-funcunit.md) — the deep treatment of the
  single coproc / single funcUnit / ctype tables.
* [Codename ↔ Generation Cross-Walk](../../reference/codename-crosswalk.md) — the per-generation
  identity axis (these microarch quantities are gen-invariant: one Cairo config, five gens).
* [On-Chip State-Buffer (SBUF) + PSUM Bank Model](../../topics/memory-model.md) — the
  NeuronCore-level SoC memory map (SBUF/PSUM), distinct from the §7 core-local IRAM/DataRAM map.
