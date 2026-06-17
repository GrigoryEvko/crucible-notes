# Tensilica Xtensa and Vision-Q7 Identification

> *All evidence on this page applies to the GPSIMD/"Q7" microcode embedded in `libnrtucode_extisa.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (host build-id `7bb03bc42ce1530924a1797ec9d5e518a7ae5e44`, sha256 `dc00763d…`, 9,656,488 B; `.rodata` VMA == file offset, so every `0x…` offset is a host file offset). The authoritative ISA config is the Cadence/Tensilica `Xm_ncore2gp` "Cairo" build shipped in `aws-neuronx-gpsimd-tools 0.21.0.0-bc9b5fad5` (`gpsimd_tools.tgz → tools/ncore2gp`, generator `RI-2022.9`, Customer ID `19270`).*
> *Evidence grade: **Confirmed (byte-anchored)** — the identification is closed three independent ways (in-blob mangled TIE type, host ELF32 machine fields, and the on-disk `.tie`/`.xparm` config + `xt_ivp32.h` header). Other versions will differ. · Part XI — Tensilica Xtensa & Vision-Q7 Identification · [back to index](../index.md)*

## Abstract

The AWS Neuron **GPSIMD pool engine** — the firmware self-labels it "Q7" — is **not a bespoke AWS ISA and not ARM-derived**. It is a **Cadence Tensilica Xtensa LX core with the IVP (Instruction Vision Processing) vector extension**, in the AWS `Xm_ncore2gp` "Cairo" configuration: the silicon Cadence sells as **Vision-Q7**. Every byte of microcode the runtime ships for this engine is a standard Xtensa ELF32 image (`e_machine = 0x5e`), and every vector instruction it issues is a Cadence IVP intrinsic (`ivp_*` mnemonic / `IVP_*` C builtin) operating on the IVP register-file model (a 512-bit vector file, a 1536-bit wide-MAC accumulator, predicate/align/gather side files). This page is the **canonical identification record**: other pages cite it for "what the Q7 engine is" so the proof lives in exactly one place.

The case is closed by three mutually independent artifacts, no two of which could agree by accident. (1) **In-blob**: the only TIE coprocessor *type* name that survives in the stripped microcode is `_TIE_xt_ivp32_xb_vec2Nx8U` — the Cadence mangled name for the IVP32 "2N-wide unsigned byte" vector register class — appearing in six `cptc_decode_impl<1..6>` instantiations, plus the single uppercase builtin `IVP_MULUSAN_2X32` and the firmware's own `"Q7: rdma_desc_gen"` diagnostics. (2) **Host ELF32**: `readelf` reports 13 embedded `ELFCLASS32`/little-endian/`EXEC` objects, machine *"Tensilica Xtensa Processor"*, `e_flags = 0x300`, a `.comment` of `"XtensaTools-14.09 clang version 10.0.1"`, every kernel opening with the textbook Xtensa windowed prologue `entry a1,<frame>` (`0x36…`). (3) **The config itself**: `tools/ncore2gp/config/core.xparm` declares `arch="Xtensa24" uarchName="Cairo" name="Xm_ncore2gp"` with the Vision block `simd16="0x20" vp6_isa="1" vq7_isa="1" dualquad8x8_mac="1"`, and `xtensa-elf/arch/include/xtensa/tie/xt_ivp32.h` defines `_TIE_xt_ivp32_xb_vec2Nx8U` and `IVP_MULUSAN_2X32` by name. Because the `.tie` config is *present on disk*, the ISA decodes exactly — superseding the earlier framing that called the Q7 TIE "opaque" because the config was assumed unshipped.

This page documents what a reimplementer must reproduce to *recognise* a Q7 blob and decode its ISA model: the three identification fingerprints; the IVP register-file model (8 files); the FLIX (Flexible Length Instruction eXtensions) format/length scheme (14 formats, the self-describing length nibble, the 5-slot bundle); and the per-mnemonic opcode-encoding shape. It does **not** enumerate all 1065 IVP operations (that is the [IVP ISA Catalog](ivp-isa-catalog.md)) or carve the blob container (the [Q7 Microcode Blobs](q7-blobs.md) page).

For reimplementation, the contract is:

- **The three-way identification proof** — the in-blob `_TIE_xt_ivp32_*` type, the host ELF32 machine/flags/comment, and the `Xm_ncore2gp` "Cairo" `.xparm` Vision block — and how to reproduce each from the binaries.
- **The IVP register-file model** — 8 files with exact widths and entry counts (`vec` 512b×32, `wvec` 1536b×4, `vbool` 64b×16, `valign` 512b×4, `b32_pr` 64b×16, `gvr`/`xb_gsr` 512b×8, plus core `AR` 32b×64 and `BR` 1b×16).
- **The FLIX format/length scheme** — the self-describing first-nibble length decode, the 14 format nodes, and the canonical 5-slot bundle (`s0` LdSt, `s1` Ld/ALU, `s2` Mul→wvec, `s3` ALU, `s4` ALU only on F3/F11).
- **The opcode-encoding shape** — one `OPCODEDEF` per (operation, FLIX format), opcode = AND of slot-local `fld[hi:lo]==const`, operands in the slot's remaining bits, direction in the iclass arg-list — enough to decode any `ivp_*` instruction once the config is loaded.

| | |
|---|---|
| **Engine** | GPSIMD "pool" engine — firmware self-label **"Q7"** |
| **ISA** | Cadence Tensilica **Xtensa LX** + **IVP** vector extension (Vision-Q7) |
| **Config** | `Xm_ncore2gp` "Cairo", `arch=Xtensa24`, `XEA3`, `TargetHWVersion=NX1.1.4`, Customer ID `19270` (AWS) |
| **Host machine type** | `e_machine = 0x5e` ("Tensilica Xtensa Processor"), `ELFCLASS32` LE, `EXEC`, `e_flags = 0x300` |
| **Toolchain** | `.comment` = `XtensaTools-14.09 clang version 10.0.1`; config generator `RI-2022.9` |
| **In-blob proof type** | `_TIE_xt_ivp32_xb_vec2Nx8U` (six `cptc_decode_impl<1..6>` instantiations, `lib3`) |
| **In-host proof builtin** | `IVP_MULUSAN_2X32` (gather-index MAC; in the raw Q7 memory-image, not the ELF32 blobs) |
| **Vector width** | 512-bit (`simd16=0x20` ⇒ 32 lanes × 16-bit; 64 × i8 / 32 × i16) |
| **Wide-MAC accumulator** | 1536-bit `wvec` (3 × 512; 48-bit headroom per 16-bit lane) |
| **Authoritative config files** | `tools/ncore2gp/config/{core.xparm, core.yml, libtie-core.so, libisa-core.so}` + `xt_ivp32.h` |

---

## 1. The Identification Proof

### Purpose

This section is the core of the page: three fingerprints, each independent, that together identify the engine beyond reasonable doubt. A reimplementer who finds *any one* of these in an unknown binary has strong evidence; finding all three is conclusive. Each is reproducible from the on-disk binaries with `readelf`, `rg`, and `c++filt`.

### Fingerprint 1 — the in-blob TIE coprocessor type

The microcode `.symtab` is stripped, but the Xtensa toolchain emits a `.xt.prop.<mangled-symbol>` section per kernel, and the *shstrtab* preserves the mangled C++ name. The one TIE coprocessor *type* that survives there is the decisive proof. Six template instantiations of the compressed-tensor decoder carry it:

```text
_Z16cptc_decode_implILh1EEvj25_TIE_xt_ivp32_xb_vec2Nx8US0_ttthb   (… ILh2 … through ILh6 …)
```

`c++filt` resolves this to a function taking **two IVP32 vector registers**:

```c
// rg -a -o '_Z16cptc_decode_impl[A-Za-z0-9_]*' libnrtucode_extisa.so | c++filt
void cptc_decode_impl<(unsigned char)1>(
        unsigned int,
        _TIE_xt_ivp32_xb_vec2Nx8U,   // first IVP "2N-wide unsigned byte" vector reg
        _TIE_xt_ivp32_xb_vec2Nx8U,   // S0_ mangling substitution = same type repeated
        unsigned short, unsigned short, unsigned short,
        unsigned char, bool);
```

`_TIE_xt_ivp32_xb_vec2Nx8U` is **not** an AWS invention. It is the exact, automatically generated typedef the Cadence config emits — present verbatim in the shipped header `xt_ivp32.h`:

```c
// gpsimd_tools/.../xtensa/tie/xt_ivp32.h  (header banner: "Definitions for the xt_ivp32 TIE package")
typedef _TIE_xt_ivp32_xb_vec2Nx8U xb_vec2Nx8U;   // 2N-wide (64-lane) 8-bit UNSIGNED vector
```

> **NOTE —** the `25` in `…vj25_TIE_xt_ivp32_xb_vec2Nx8U…` is the Itanium-ABI length prefix: `_TIE_xt_ivp32_xb_vec2Nx8U` is exactly 25 characters. `S0_` is a substitution reference back to that type. These are mechanical mangling artifacts; their internal consistency is part of why the name cannot be a coincidence. The type names a register *class*, not a struct — `2N×8U` = 2N-wide 8-bit unsigned, i.e. all 64 byte-lanes of the 512-bit `vec` file viewed as unsigned. CONFIDENCE: **CERTAIN**.

### Fingerprint 2 — the host ELF32 machine fields

`libnrtucode_extisa.so` is a host x86-64 carrier that embeds the on-device images as raw `.rodata`. Scanning for the ELF magic finds **14** `\x7fELF` headers: one host x86-64 plus **13 Xtensa ELF32** objects. Carve any one (offsets are fixed; v4_plus `lib0` is at host file offset `0x1ce60`) and the standard `readelf` reports:

```text
$ readelf -h v4plus_lib0.elf
  Class:    ELF32                          Type:    EXEC (Executable file)
  Data:     2's complement, little endian  Machine: Tensilica Xtensa Processor
  Flags:    0x300                          Entry:   0x1005658
$ readelf -l v4plus_lib0.elf
  LOAD  0x000100 0x01000000 0x01000000 R E   ── .text (pool kernels)
  LOAD  0x007080 0x02000000 0x02000000 RWE   ── .rodata/.data/kernel_info_table/.globstruct/.bss
  LOAD  0x007500 0x03000000 0x03000000  WE   ── .dynamic (RELA reloc input for the host loader)
$ readelf -p .comment v4plus_lib0.elf
  XtensaTools-14.09 clang version 10.0.1
```

`e_machine = 0x5e` (94 decimal) is the IANA/ELF assignment for **Tensilica Xtensa**. The `.text` VMA of `0x01000000` and the `entry a1,<frame>` windowed prologue at every kernel entry are the textbook Xtensa LX fingerprint. This rules out RISC-V (`0xf3`, reset = a 4-byte `JAL`) and ARM (`0x28`/`0xb7`, reset = a vector table of `u32` words); the Xtensa reset/dispatch idiom is a 3-byte `J` and a windowed `entry`.

> **GOTCHA —** do not confuse the *host* carrier's machine type with the payload's. `readelf -h` on `libnrtucode_extisa.so` itself reports `x86-64` — that is the loader/provider library, not the firmware. The Xtensa proof is only visible after carving the 13 embedded ELF32 blobs out of `.rodata`. The carve offsets are dispatch-table-driven; see [Q7 Microcode Blobs](q7-blobs.md). CONFIDENCE: **CERTAIN**.

### Fingerprint 3 — the `Xm_ncore2gp` "Cairo" config

The third proof is the Cadence config itself, shipped in `gpsimd_tools.tgz`. `tools/ncore2gp/config/core.xparm` is the processor-configuration parameter file. Its core block names the configuration; its Vision block enables exactly the IVP/Q7 features:

```text
# core.xparm  (Customer ID=19270; Build=0xc23fe; "Copyright (c) 2008 Tensilica Inc.")
<xt … arch="Xtensa24" uarchName="Cairo" name="Xm_ncore2gp" usePRID="1"
      exceptionArch="XEA3" TargetHWVersion="NX1.1.4" TargetHWConfigID0="0xc4019686"
      SW_ABI="windowed" loadStoreUnitsCount="2" coprocessorCount="7" density="1" … >

<hash t="Vision" simd16="0x20" histogram="1" hp_vfpu="1" sp_vfpu="1"
      vp6_isa="1" paired_mac="1" quad_mac="1" vq7_isa="1"
      quad16x16_mac="1" dualquad8x8_mac="1" sp_vfpu_2xfma="1" hp_vfpu_2xfma="1"
      sp_recip_qli="1" fast9="1" vp7_isa="0" … >
```

`vq7_isa="1"` is the literal **Vision-Q7 ISA enable**. `simd16="0x20"` = 32 lanes of 16-bit = a **512-bit** vector datapath. `dualquad8x8_mac="1"` / `quad16x16_mac="1"` are the wide-MAC modes the pool kernels drive (`ivp_dmulq2n8xr8`, etc.). `core.yml` independently states `loadStoreWidth: 512`. The build provenance (`build.info`): generator `RI-2022.9`, `name="Xm_ncore2gp"`, evaluation license CID `0x4b46` ("KF"). The companion header carries the unambiguous vendor banner:

```c
// xt_ivpn.h banner
// Customer ID=19270; Build=0xc23fe; Copyright (c) 2017-2019 Cadence Design Systems, Inc.
```

> **CORRECTION (FW-XTENSA seed) —** the original seed framing (`P1-FW-XTENSA`, `P2-W2-XT-EXTISA`) tagged the Q7 vector body as `<TIE/IVP>` "not decoded — needs the proprietary `.tie` config" and treated the ISA as opaque/ARM-ambiguous. **That is superseded.** The `.tie` configuration *is* shipped, in `aws-neuronx-gpsimd-tools` (`gpsimd_tools.tgz → tools/ncore2gp/config/{core.xparm, core.yml, libtie-core.so, libisa-core.so}` plus the generated `xtensa/tie/xt_ivp32.h`). With it the engine is identified as Cadence Vision-Q7 (`Xm_ncore2gp` "Cairo") and every `ivp_*` instruction decodes by name. The "opaque TIE" caveat applies only to *byte-exact bundle re-splitting* of the carved blobs (which lose their `.xt.prop` addresses on carve), not to the ISA identity or the per-mnemonic encoding. CONFIDENCE: **CERTAIN** (identity); **HIGH** (encoding via on-disk `libtie-core`/`libisa-core`).

### Function Map — what produces each fingerprint

| Artifact | Where | Role | Confidence |
|---|---|---|---|
| `_TIE_xt_ivp32_xb_vec2Nx8U` | `.xt.prop` names, `lib3` (cptc) | In-blob IVP32 type proof | CERTAIN |
| `IVP_MULUSAN_2X32` | host `.rodata` ~`0x55000–0x59000` (raw Q7 image) | In-host IVP builtin name | CERTAIN (string) |
| `"Q7: rdma_desc_gen"`, `"remote_q7_xt_addr"` | same raw Q7 image | Firmware self-label | CERTAIN |
| `e_machine=0x5e`, `e_flags=0x300`, `.comment` | 13 carved ELF32 blobs | Xtensa machine/toolchain | CERTAIN |
| `vq7_isa=1`, `simd16=0x20`, `Xm_ncore2gp`/Cairo | `core.xparm` / `core.yml` | Config = Vision-Q7 | CERTAIN |
| `xt_ivp32.h` typedefs + `IVP_*` macros | `xtensa/tie/xt_ivp32.h` | Canonical type/intrinsic source | CERTAIN |
| `1065 × Iclass_IVP_*_args`, 5 × `get_xml_*` | `libisa-core.so` / `libtie-core.so` | ISA breadth + XML provider | CERTAIN |

### Considerations

The `IVP_MULUSAN_2X32` builtin and the `"Q7:"` diagnostics live in a **raw, non-ELF Q7 memory-image** region of the host `.rodata` (~`0x50000–0x59000`), served by the separate `nrtucode_get_memory_image` path — *not* inside the 13 ext-ISA ELF32 blobs. They are still genuine Q7 microcode (the region decodes as `entry a1,32` + IVP FLIX bundles), but they belong to a debug-flavor image selected by `NEURON_UCODE_FLAVOR`, not to a named production ulib. The attribution is exact by offset arithmetic; the *flavor* (debug vs test) is MED. This split is why the builtin name appears once in the whole file while the production blobs carry only the mangled *type*.

---

## 2. The IVP Register-File Model

### Purpose

Identifying the core is half the job; the other half is the register model a decoder must implement. The IVP coprocessor adds eight register files on top of the base Xtensa `AR`/`BR` files. Every `ivp_*` operand references one of these by a slot-local index field, so the widths and entry counts below are what a reimplementer needs to size operand decode and to model the datapath.

### Encoding

The eight files, with widths cross-validated against `core.xparm` (`simd16=0x20` ⇒ 512-bit vector, `loadStoreWidth=512`) and the `xt_ivp32.h` typedefs:

| File | Short | Bit width | Entries | Index bits | Role |
|---|---|---|---|---|---|
| `AR` | `a` | 32 | 64 | 6 | Core Xtensa scalar/address (windowed ABI) |
| `BR` | `b` | 1 | 16 | 4 | Core boolean (paired view `BR2`) |
| `vec` | `v` | **512** | 32 | 5 | The IVP vector file: 64 × i8 / 32 × i16 / 16 × i32 |
| `wvec` | `wv` | **1536** | 4 | 2 | **Wide-MAC accumulator** (3 × 512; 48-bit/lane headroom) |
| `vbool` | `vb` | 64 | 16 | 4 | Predicate / compare-result mask file |
| `valign` | `u` | 512 | 4 | 2 | Load-align / post-pointer state (aligning loads/stores) |
| `b32_pr` | `pr` | 64 | 16 | 4 | Gather/permute select-control |
| `gvr` (`xb_gsr`) | `gr` | 512 | 8 | 3 | Gather-source registers (8 gather / 2 scatter) |

The accumulator width is the design's defining feature. A 16-bit-lane multiply produces a 32-bit product; accumulating many of them needs headroom, so `wvec` is **three times** the vector width (1536 = 3 × 512), giving 48 bits per 16-bit lane. The `xt_ivp32.h` prototypes make this concrete — the accumulator argument of a MAC is typed as a *wide* view, not a plain `vec`:

```c
// xt_ivp32.h — the packed-accumulate 16×16 MAC: wvec accumulator typed as xb_vecNx48
extern void _TIE_xt_ivp32_IVP_MULPAN16XR16(
        xb_vecNx48 a /*inout*/,           // accumulator: N (=32) lanes × 48 bits = 1536b wvec view
        xb_vecNx16 b, xb_vecNx16 c,       // two 16-bit-lane vector inputs
        xb_int32pr d);                    // pr select control

// the dual-quad 8×8 MAC writes a 2N×24 accumulator view (also a wvec view)
extern void _TIE_xt_ivp32_IVP_DMULQ2N8XR8(
        xb_vec2Nx24 a, xb_vec2Nx24 b,     // accumulator pair
        xb_vec2Nx8 c, xb_vec2Nx8 d, xb_vec2Nx8 e, xb_vec2Nx8 f,  // four byte-vector inputs
        xb_int64pr g);
```

> **QUIRK —** the accumulator is addressed by *several* C type names (`xb_vecNx48`, `xb_vec2Nx24`, `xb_vecN_2x64w`) that are all **views of the same 1536-bit `wvec` file** — the suffix encodes how the 48-bit-per-lane store is partitioned for that operation (N lanes × 48b, or 2N lanes × 24b, or N pairs × 64b). A reimplementer must model `wvec` as one physical 1536-bit file with multiple lane interpretations, not as three separate files. CONFIDENCE: **HIGH** (widths from `core.xparm` + header; entry counts cross-checked against `libisa-core` operand-class range-validators).

### The C type system

On top of the eight register files the config exposes ~64 C coprocessor types — the `_TIE_xt_ivp32_xb_vec*` family the kernels declare. The shipped `xt_ivp32.h` enumerates them; the families a decoder must recognise:

```c
// representative ctypes (all typedef'd in xt_ivp32.h)
xb_vec2Nx8, xb_vec2Nx8U          // 64-lane signed / UNSIGNED byte  (the in-blob proof type)
xb_vecNx16, xb_vecNx16U          // 32-lane 16-bit signed / unsigned
xb_vecN_2x16, xb_vecN_2x32v      // N pairs of 16/32-bit (paired-lane)
xb_vecN_2xf16, xb_vecN_2xf32     // N pairs of fp16 / fp32
xb_vec2Nx24, xb_vecNx48          // wide-MAC accumulator views (wvec)
xb_vecN_2x64w, xb_vecN_4x64      // 64-bit-pack accumulator / load views
valign, xb_gsr                   // align state file / gather-source file
vbool1, vboolN, vbool2N          // predicate views (per-lane width)
```

### Considerations

Operand decode is uniform: a vector operand is a 5-bit `vec` index, a predicate a 4-bit `vbool` index, an accumulator a 2-bit `wvec` index, a gather a 3-bit `gvr` index, an `AR` scale a 4-bit (in-slot) index. The empirical operand-class tally across the three carved compute blobs (`a` ≈ 35,000, `v` ≈ 5,300, `vb` ≈ 1,140, `wv` ≈ 333, `pr` ≈ 201, `u` ≈ 92) matches these index widths — the dominant `AR` traffic is the windowed-ABI scalar/address bookkeeping, the next-most-common `vec` is the actual SIMD work.

---

## 3. The FLIX Instruction Format

### Purpose

Q7 instructions are not fixed-width. The IVP config uses Tensilica **FLIX** (Flexible Length Instruction eXtensions): a single fetch word can hold a 16/24-bit core-Xtensa instruction *or* a multi-slot VLIW-style bundle that issues 2–5 operations at once. A decoder must implement the self-describing length decode before it can even find instruction boundaries. This is the most error-prone part of a Q7 disassembler and the reason a naive linear sweep desyncs.

### The length decode

Instruction length is self-describing in the **first nibble** of word0 (`InstBuf[3:0]`). The decode (from `libisa-core.so`'s `length_decoder` / `length_table` and the format-encoder names):

```text
first nibble (InstBuf[3:0])      length        format class
  0x0 .. 0x7                      3 bytes       x24    (core Xtensa, 24-bit)
  0x8 .. 0xb                      2 bytes       x16a   (density group A)
  0xc .. 0xd                      2 bytes       x16b   (density group B)
  0xe                             16 bytes      l128a  → wide FLIX F3 / F11 (5-slot)
  0xf                             16 bytes      l128b/c → wide FLIX F0/F1/F2/F4/F6/F7 (4-slot)
                              OR   8 bytes       l64a   → narrow FLIX N0/N1/N2 (2-3 slot)
                                   (0xf disambiguated by a 2nd InstBuf predicate: 2'b01 / 3'b011 / 1'b0)
```

> **GOTCHA —** nibble `0xf` is overloaded: it can begin a 16-byte wide bundle *or* an 8-byte narrow bundle, distinguished only by a secondary bit predicate inside the fetch word. A decoder that assumes nibble `0xf` ⇒ 16 bytes will misalign the instant it meets a narrow bundle, and every subsequent instruction is garbage. This is exactly the desync the hand-rolled base-ISA decoders hit; the Cadence `length_decoder` in `libisa-core.so` is the reference implementation. CONFIDENCE: **HIGH** (decoded from `libisa-core` length table + format encoders).

### The 14 formats and the 5-slot bundle

`libisa-core.so` exports one `Format_<F>_encode` per format. There are exactly **14**: 3 core (`x24`, `x16a`, `x16b`), 8 wide-128 (`F0`, `F1`, `F2`, `F3`, `F4`, `F6`, `F7`, `F11`), and 3 narrow-64 (`N0`, `N1`, `N2`). The wide bundles carry **four** slots; **only F3 and F11 carry a fifth slot** (`s4`). The canonical slot classes and their base bit positions (from the `Format_<f>_s<n>_<class>_<bit>_get` encoder names) define the inner-kernel issue model:

| Slot | Class | Role | Base bit (F0 / F3 / F11) |
|---|---|---|---|
| `s0` | LdSt | scalar control/branch + vector store | 4 / 4 / 4 |
| `s1` | Ld / ALU | vector load (post-inc) / predicate logic | 16 / 16 / 16 |
| `s2` | Mul | the wide MAC → `wvec` accumulator | 28 / 28 / 41 |
| `s3` | ALU | vector select / arithmetic | 36 / 33 / 31 |
| `s4` | ALU | vector arith / abs-diff / bool-reduce (F3, F11 **only**) | — / 24 / 24 |

The empirical bundle mix in the carved compute kernels is ~1709 × 64-bit (2–3 ops) + ~966 × 128-bit (4–5 ops): the engine spends most of its time in narrow bundles, escalating to the 5-slot F3/F11 forms for the densest fused `{load | MAC | select | reduce}` inner loops.

```text
# canonical Q7 inner-kernel bundle (decode_pool, F0-class, 16-byte):
  s0:  bgeui.w15 a5,16,…              ; loop/branch control
  s1:  ivp_lsrn_2x32_xp v16,a3,a12    ; vector load, post-increment the AR pointer
  s2:  ivp_dmulq2n8xr8 wv1,wv1,…,pr3  ; dual-quad 8×8 MAC → 1536-bit wvec accumulator
  s3:  ivp_dselnx16t v0,v2,…,vb0      ; lane-select / permute the result
```

### The opcode-encoding shape

Each operation defines one `OPCODEDEF` per FLIX format it can issue in. The opcode is the AND of slot-local field constants; operands occupy the slot's remaining bits; direction (IN/OUT/MODIFY) lives in the iclass arg-list. From the decrypted `get_xml` TIE-XML (`libtie-core.so` provides it via `get_xml_post_parse` / `get_xml_compiler` / `get_xml_xinfo`):

```text
IVP_MULPAN16XR16 (format F0):  s2_mul[27:21]==6 & [14:12]==1 & [7:6]==0
    — MAC major opcode [27:21]==6 is shared by the packed/usp/4t family;
      the sub-op {[14:12],[7:6]} selects term-count + signedness.
IVP_SEL2NX8I  (F0):  s3_alu[34:22]==0x19a          ; IVP_DSELNX16T (F0): s3_alu[34:29]==0
IVP_LV2NX8_I  (F0):  s0_ldst[31:17]==0x847         ; IVP_LV2NX8_IP adds [8:8]==0 (post-inc)
    operand lanes per slot: vec=5b, vbool=4b, wvec=2b, AR=4b (in-slot)
    direction example (MULPAN16XR16): wvt:MODIFY (read-accumulate-write wv), vs/vr:IN(vec),
      arr:IN(AR scale), + implicit CPENABLE:IN / Coprocessor1Exception:OUT on every Vision op
```

### Function Map — the config providers

| Artifact | File | Role | Confidence |
|---|---|---|---|
| `length_decoder`, `length_table` | `libisa-core.so` (out-of-tree) | First-nibble length decode | HIGH |
| `Format_{F0..F11,N0..N2,x24,x16a,x16b}_encode` | `libisa-core.so` (out-of-tree) | The 14 FLIX formats | HIGH |
| `Slot_f*_s4_alu_24_*` (F3, F11 only) | `libisa-core.so` (out-of-tree) | 5-slot formats | HIGH |
| `Iclass_IVP_*_args` (1065) | `libisa-core.so` (out-of-tree) | Per-op operand tables = ISA breadth (1:1 with opcodes) | HIGH |
| `get_xml_post_parse/compiler/xinfo`, `interface_version` | `libtie-core.so` (out-of-tree) | Obfuscated TIE-XML provider (OPCODEDEF/FIELDDEF) | MED |
| `IVP_*` macros (count unverified — see correction) / `_TIE_xt_ivp32_IVP_*` protos | `xt_ivp32.h` (out-of-tree) | Source-level intrinsic catalog | MED |

> **CORRECTION (IVP-COUNT) —** an earlier scaffold gave the `Iclass_IVP_*_args` operand-table count as **1064**; the authoritative folded count is **1065**, corroborated 7× across four independent sources (`libisa-core` `opcodes[]`@`0x4ce6c0` stride-72-to-NULL, `xtensa-modules.c`, the ISA-39 synthesis, the TIE-DB) with a verified 1:1 ivp-opcode↔`Iclass_IVP_*` mapping. The earlier "**2415** `IVP_*` macros" figure was a `xt_ivp32.h` `grep` count that could not be re-verified against any in-tree artifact (the header is out-of-tree), so it is withdrawn here rather than asserted; a reimplementer with the `ncore2gp` config should re-derive it via `grep -c IVP_ xt_ivp32.h`. CONFIDENCE: **HIGH** (the 1065 folded count); the macro tally is unverified.
>
> **NOTE —** the ISA-breadth and FLIX figures above (the **1065** `Iclass_IVP_*_args` / **14** formats / 5-slot `Slot_*` base bits) are grounded on the `ncore2gp` toolchain — `.tie`, `xt-objdump`, `libisa-core.so`, `libtie-core.so`, `core.xparm`, `xt_ivp32.h`. **None of those ship in this repository tree** (verified: `find . -name '*.tie' -o -iname '*xt-objdump*' -o -iname 'libisa-core*' -o -iname 'libtie-core*' -o -iname '*xparm*'` returns nothing), so a reimplementer cannot reproduce them from anything shipped here; they are externally anchored, hence the downgraded confidence. The identification facts that *are* in-binary-confirmed stay **CERTAIN**: the in-blob `_TIE_xt_ivp32_xb_vec2Nx8U` type across the six `cptc_decode_impl<1..6>` instantiations, the single `IVP_MULUSAN_2X32` builtin in the host raw-Q7 image, and the 13 carved Xtensa ELF32 machine/flags/comment fields — all present in `libnrtucode_extisa.so` (build-id `7bb03bc4…`).

### Considerations

The full per-mnemonic encoding (every operation × every format with operand bit-lanes) is available on disk — `libtie-core.so` serves the TIE-XML and `libisa-core.so` holds the operand tables (1065 `Iclass_IVP_*_args`) — but is not exhaustively transcribed here; this is an *identification* page, and the complete op enumeration belongs to the [IVP ISA Catalog](ivp-isa-catalog.md). The one residual gap is **byte-exact bundle re-splitting of the carved blobs**: carving an ELF32 image out of host `.rodata` zeroes the `.xt.prop` section addresses, so the Cadence `xt-objdump` cannot map the property records to `.text` to drive FLIX splitting (it falls back to printing raw fetch words). Every emitted bundle is valid and every `ivp_*` cross-checks the config; only the automatic boundary split needs an `ET_REL` relink with relocated `.xt.prop`. CONFIDENCE: **LOW** (only the automatic split is blocked, not the ISA).

---

## 4. The Engine in Context

### Purpose

The Q7 engine does not exist in isolation: it is one of two Xtensa configs in the Neuron firmware, and its microcode is loaded by a host-side provider. This section places the identification within the surrounding system so a reader knows what is and is not Q7.

### Two Xtensa configs, one ISA family

Neuron firmware runs two *distinct Xtensa configurations*, and conflating them is the most common identification error:

- **GPSIMD "Q7"** — the **Vision-IVP32** config documented here (`Xm_ncore2gp` "Cairo"). Runs the pool/compute kernels (`decode_pool`, `cptc_decode_impl`, `iota_kernel`, gather/embedding). Shipped as the 13 ELF32 blobs in `libnrtucode_extisa.so`.
- **NCFW (sequencer)** — a *different* Xtensa LX config (sequencer/collective firmware, no IVP vector files; its custom TIE is the `op0=4` MAC16 window-save block). Shipped as 8 raw (non-ELF) IRAM/DRAM blobs in `libncfw.so`.

Both are Tensilica Xtensa LX — **one ISA family, two TIE configs**. The NCFW config does **not** decode with the `ncore2gp` `.tie` and must not be identified as Vision-Q7.

### How a Q7 blob is loaded

The host provider exposes the microcode via three exported functions in `libnrtucode_extisa.so`; the on-device dispatch then launches a kernel by pool-opcode through a standard windowed call:

```text
nrtucode_get_num_ext_isa_libs(arch_id,*out)  → {arch6:1, arch13:4, arch21:4, arch29:4}
nrtucode_get_ext_isa(arch_id,lib_idx,out)     → {Xtensa ELF32 body, JSON opcode header}
nrtucode_get_memory_image(arch_id,engine,flavor,…) → raw Q7 image (the IVP_MULUSAN_2X32 debug flavor)
        │
        ▼ on device: loader sets up windowed frame, indexes kernel_info_table[opcode]
  kernel_info_table : N × 8-byte records  [u32 BE opcode | u32 LE .text entry VA]
        └─ CALL entry  → `entry a1,<frame>` (windowed prologue) → IVP FLIX inner loop → RETW.N
```

> **CORRECTION (ARCH-ID) —** an earlier scaffold labelled the reachable `arch_id`s `{5, 12, 20, 28}` (i.e. `idx = arch_id − 5`). The binary computes **`idx = arch_id − 6`** (immediate `41 83 c7 fa` = `add $-6` @`0x870b` in `get_num_ext_isa_libs` @`0x87b0`), so the real, code-reachable `arch_id`s are **`{6, 13, 21, 29}`** with library counts `{arch6:1, arch13:4, arch21:4, arch29:4}`. The `{1,4,4,4}` counts were always right; only the `arch_id` labels were off-by-one against the firmware coretype keys. Byte-proven and fully decoded in [dispatch-tables.md](dispatch-tables.md) (`JT_NUMLIBS`@`0x920e78`, reachable only at idx `0/7/15/23`; corroborated by `libnrt` `CSWTCH.113 = [6,13,21]`@`0x86ada8`). CONFIDENCE: **HIGH**.

### Considerations

The kernel argument types (`NEURON_ISA_TPB_ALU_OP`, `NEURON_ISA_TPB_REDUCE_OP`, `NEURON_ISA_TPB_DTYPE`, `NEURON_ISA_TPB_ADDR4`) are Neuron-TPB enums, *not* Xtensa/IVP types — they are how the pool ops parameterize arithmetic/dtype, orthogonal to the ISA identity. The compressed-tensor decode path is gated by the `NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE` runtime flag (string present in the host carrier), which is what selects the `cptc_decode_impl<N>` kernels that carry the in-blob proof type.

---

## Related Components

| Name | Relationship |
|---|---|
| GPSIMD overview | The engine's place in the NeuronCore TPB |
| IVP ISA Catalog | The 285 emitted / 1065 total `IVP_*` operations this page identifies |
| Q7 Microcode Blobs | The 13-blob = 9-distinct container these images carve from |
| Xtensa Toolchain & TIE Config | The `gpsimd_tools` `ncore2gp` config + `xt-objdump` that decodes the ISA |
| NCFW firmware | The *other* Xtensa config — sequencer-class, not Vision-Q7 |

## Cross-References

- [Overview: the Q7 GPSIMD Engine](overview.md) — where the Q7 engine sits in the NeuronCore
- [The Xtensa Toolchain and TIE Config](xtensa-toolchain.md) — the shipped `ncore2gp` config, `core.xparm`, and `xt-objdump` that make the ISA decode
- [The IVP Vector ISA Catalog (1065 Ops)](ivp-isa-catalog.md) — the full operation roster; this page proves the ISA *is* IVP, that page enumerates it
- [The 13 Q7 Microcode Blobs](q7-blobs.md) — carving the ELF32 images whose machine type and `_TIE_xt_ivp32_*` names are Fingerprints 1 and 2
- [The ext-ISA Provider (nrtucode_* API)](extisa-provider.md) — the host loader/indexer that serves the blobs
- [ext-ISA Dispatch Tables and arch-id Scheme](dispatch-tables.md) — `arch_id` → coretype → lib-count mapping
