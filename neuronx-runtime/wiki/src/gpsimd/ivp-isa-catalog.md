# The IVP Vector ISA Catalog

> *All op names, counts, and bit-fields on this page apply to the Cadence/Tensilica `Xm_ncore2gp` "Cairo" Vision-Q7 config shipped in `aws-neuronx-gpsimd-tools 0.21.0.0-bc9b5fad5` (`gpsimd_tools.tgz → tools/ncore2gp/config`, generator `RI-2022.9`, Customer ID `19270`, toolchain `XtensaTools-14.09`). The emitted-subset figures are decoded from the GPSIMD/"Q7" microcode in `libnrtucode_extisa.so` (`aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`). The authoritative op table is `libisa-core.so` (`Iclass_IVP_*_args`, 9.7 MB); the per-mnemonic encoding is `libtie-core.so`'s `get_xml_*` TIE-XML (51 MB).*
> *Evidence grade: **Decoded (config-anchored)** — every op name is an `Iclass_IVP_*_args` table present on disk; every FLIX format and slot base-bit is a `Format_*_encode` / `Slot_*` symbol in `libisa-core.so`. Per-member lane width/signedness is read from the canonical suffix grammar (HIGH where the suffix is standard, MED for long-tail variants). Other versions will differ. · Part XI — The IVP Vector ISA Catalog · [back to index](../index.md)*

## Abstract

The Q7 GPSIMD engine is a Cadence Vision-Q7 (Xtensa LX + IVP) core; its vector ISA is the **IVP** ("Instruction Vision Processing") family, spelled `ivp_*` as Xtensa mnemonics and `IVP_*` as C builtins / TIE `OPCODEDEF` names. The [identification page](xtensa-vision-q7.md) proves the engine *is* IVP and fixes the 8-regfile and FLIX model; **this page is the op catalog**. It does not re-derive identification — it enumerates the operation space by its *category axes* and shows how any `ivp_*` instruction is encoded once the `.tie` config is loaded.

The full Vision-Q7 ISA defines **1065 operations** — too many to list, and most never reach Q7 silicon. The number a reimplementer of the Neuron pool microcode actually needs is the **285** that the shipped GPSIMD kernels emit, decoded by the runnable `ncore2gp` `xt-objdump` over the three carved compute blobs (Cayman `lib0` = 149, `lib3` = 112, SUNDA = 182 → 285 union). Both numbers are real and both matter: the 1065 is the *capability* (sized from `libisa-core.so`'s `1064` `Iclass_IVP_*_args` operand tables, ≡ the `1065` `IVP_*` `OPCODEDEF`s in the TIE-XML); the 285 is the *working set* the firmware exercises. This page tabulates the space by **category × emitted-count × ISA-wide-count × lane-shape**, the way a 1540-slot atomic table is best described by its four axes — never as 1065 rows.

The page is organized as three structural units: the **category dimension table** (§1) — the fourteen IVP categories, each with how many ops the kernels emit, how many the ISA defines, the canonical lane shape, and a representative mnemonic; the **FLIX format scheme** (§2) — the fourteen format nodes, the self-describing length nibble, and the per-format slot base-bit map (the only 5-slot forms are F3 and F11); and a **worked encode** (§3) — `IVP_MULPAN16XR16` decoded to its slot-local field constants, plus the canonical `cptc` decode bundle and the MX-4-bit dequantize datapath the kernels build from these ops.

For reimplementation, the contract is:

- **The category axis model** — the fourteen op categories, their emitted-vs-ISA-wide counts, and the lane-shape grammar (`2Nx8`/`nx16`/`n_2x32`/`2nx24`…) that names each op's datapath shape. A reimplementer recognises an unknown `ivp_*` by binning its mnemonic into a category and reading the lane shape off the suffix.
- **The lane-shape and suffix grammar** — `2Nx8` = 64×i8, `nx16` = 32×i16, `n_2x32` = 16×i32, `2nx24`/`2x96` = wide-MAC pack widths; suffixes `t`(tail), `u`/`s`(sign), `_i`/`_ip`/`_xp`(immediate/post-inc), `p`(packed-accumulate), `r8`/`r16`(result width).
- **The 14-format FLIX scheme** — the first-nibble length decode, the 8 wide / 3 narrow / 3 core formats, and the per-format slot base-bit table (`s0` LdSt, `s1` Ld/ALU, `s2` Mul→`wvec`, `s3` ALU, `s4` ALU only on F3/F11).
- **The opcode-encoding shape** — one `OPCODEDEF` per (op, format); opcode = AND of slot-local `fld[hi:lo]==const`; operands in the slot's remaining bits; direction in the iclass arg-list — enough to encode/decode any cataloged op.

| | |
|---|---|
| **ISA** | Cadence Tensilica IVP (Instruction Vision Processing), Vision-Q7 |
| **Config** | `Xm_ncore2gp` "Cairo", `arch=Xtensa24`, `simd16=0x20` (512-bit), `vq7_isa=1`, `dualquad8x8_mac=1` |
| **ISA-wide op count** | **1065** (`Iclass_IVP_*_args` = 1064 in `libisa-core.so`; ≡ 1065 `IVP_*` `OPCODEDEF`s in the TIE-XML) |
| **Emitted-subset op count** | **285** distinct `ivp_*` across the 3 carved compute blobs (149 + 112 + 182 → 285 union) |
| **Categories** | 14 (mul/MAC, load, store, select/permute, compare, bool-min/max, convert, add/reduce, logic, abs-diff, move, count/norm, gather, other) |
| **FLIX formats** | **14** — 8 wide-128 (`F0/F1/F2/F3/F4/F6/F7/F11`), 3 narrow-64 (`N0/N1/N2`), 3 core (`x24/x16a/x16b`) |
| **5-slot formats** | `F3` and `F11` **only** (both carry `s4_alu_24`) |
| **Vector / accumulator** | `vec` 512-bit ×32 · `wvec` 1536-bit ×4 (3×512, 48-bit/lane MAC headroom) |
| **Authoritative tables** | `libisa-core.so` `Iclass_IVP_*_args` (op breadth) · `libtie-core.so` `get_xml_{post_parse,compiler,xinfo}` (encoding) |
| **In-blob proof type** | `_TIE_xt_ivp32_xb_vec2Nx8U` (six `cptc_decode_impl<1..6>` instantiations) |

---

## 1. The Category Dimension Table

### Purpose

The IVP op space is 1065 wide, but it is not 1065 *unrelated* ops — it is fourteen categories, each a width/sign/variant family generated by appending lane-shape and modifier suffixes to a small set of base operations. A reimplementer who learns the fourteen categories and the suffix grammar can classify any `ivp_*` mnemonic and predict its operands without an exhaustive table. This is the same discipline the [DMA descriptor](../dma/descriptor-format.md) page applies to bit-fields: describe the generating rule, not the generated rows.

### The lane-shape grammar

Every IVP op names its datapath shape in the mnemonic. The shape is the lane count × element width, derived from the 512-bit `vec` file (`simd16=0x20` ⇒ N = 32 sixteen-bit lanes):

```text
2nx8 / 2Nx8   2N-wide 8-bit       N=32 → 64 lanes   (the 512-bit byte vector "vec2Nx8")
nx8           N-wide 8-bit        32 lanes
nx16          N-wide 16-bit       32 lanes          ("vecNx16")
n_2x16        N pairs of 16-bit   16 pairs
n_2x32        N pairs of 32-bit   16 lanes          (int)
2xf32         N pairs of fp32     16 lanes          (float)
nxf16         N-wide fp16         32 lanes
2nx24 / 2x96 / 2x64   wide-MAC pack widths (the 1536-bit wvec accumulator, partitioned)
```

The suffix grammar (decoded from the mnemonic set and cross-checked against the `Iclass_IVP_*_args` names):

```text
t       tail / true variant         u / s   unsigned / signed
b       byte- or bool-domain op     d       dual (2-output / dual-quad)
p       packed-accumulate           r8/r16  result element width
_i      immediate offset            _ip     immediate + post-increment
_ipi    immediate + auto-increment  _xp     post-increment by AR step
```

> **NOTE —** the suffix is the operand contract. `ivp_lv2nx8_i` and `ivp_lv2nx8_ip` are the *same* load op in two FLIX `OPCODEDEF` forms — the `_ip` form sets one extra post-increment bit (`[8:8]==0`) and consumes the `AR` step register. This is why the raw mnemonic tally in `libisa-core.so` (4762 `ivp_*` strings, format-and-form duplicated) collapses to 1064 distinct ops. CONFIDENCE: **HIGH** (form duplication confirmed by the `Iclass_*` de-dup count).

### The fourteen categories

Each category lists how many distinct ops the GPSIMD kernels **emit** (decoded by `xt-objdump` over the three carved blobs) and how many the **ISA** defines (binned from the `1064` `Iclass_IVP_*_args` names in `libisa-core.so`). The two counts are independent axes: *Emitted* sizes the firmware's working set; *ISA-wide* sizes the silicon's capability. A reimplementer targeting the Neuron pool kernels needs the Emitted column; one building a general Q7 assembler needs the ISA-wide column.

| Category | Emitted | ISA-wide | FLIX slot | Lane shape (canonical) | Representative op | Confidence |
|---|---|---|---|---|---|---|
| **mul / MAC** | 50 | ~236 | `s2_mul` | `v,v,pr → wv` (2Nx8 / nx16 → wide) | `ivp_mulpan16xr16`, `ivp_dmulq2n8xr8` | DECODED HIGH |
| **load** | 41 | ~122 | `s0_ldst` / `s1_ld` | `→ v` (+ `valign` state) | `ivp_lv2nx8_i`, `ivp_la2nx8_ipi` | DECODED HIGH |
| **store** | 17 | ~54 | `s0_ldst` | `v[,vb] →` (mem) | `ivp_sv2nx8_i`, `ivp_sav2nx8_xp` | DECODED HIGH |
| **select / permute / pack** | 21 | ~75 | `s3_alu` / `s4_alu` | `v,v[,v] → v[,v]` | `ivp_dselnx16t`, `ivp_sel2nx8i` | DECODED HIGH |
| **compare → predicate** | 23 | ~59 | `s3_alu` | `v,v → vb` | `ivp_oltn_2xf32`, `ivp_eqn_2x32` | DECODED HIGH |
| **bool min / max** | 31 | ~12 † | `s3_alu` / `s4_alu` | `v,v → vb,v` | `ivp_bmin2nx8`, `ivp_bmaxnx16` | DECODED HIGH |
| **convert / pack-convert** | 15 | ~74 | `s3_alu` | `v[,imm,vb] → v` | `ivp_float16nx16t`, `ivp_truncn_2xf32` | DECODED HIGH |
| **add / sub / reduce** | 13 | ~33 | `s3_alu` | `v,v → v` | `ivp_addn_2x32`, `ivp_addsnx16t` | DECODED HIGH |
| **logic (bitwise)** | 12 | ~38 | `s1_ld` / `s4_alu` | `v,v → v` ; `vb,vb → vb` | `ivp_and2nx8`, `ivp_andb`, `ivp_sllin_2x32` | DECODED HIGH |
| **abs-diff / SAD** | 7 | ~13 | `s4_alu` | `v,v,v → vb` | `ivp_babssub2nx8`, `ivp_abssubunx16` | DECODED HIGH |
| **move / const** | 10 | ~32 | `s3_alu` (move) | `a↔v` ; `v→v` ; `imm→v` | `ivp_movva32`, `ivp_constn_2xf32` | DECODED HIGH |
| **count / norm / subnorm** | 5 | ~14 | `s4_alu` | `v[,vb] → v` | `ivp_counteqm4nx8`, `ivp_baddnormnx16` | DECODED HIGH |
| **gather / scatter** | 3 | ~23 | `gvr` file | `pr/a idx → v` | `ivp_gatheran_2x32t`, `ivp_gatherdnx16` | DECODED HIGH |
| **other (mixed)** | ~37 | (remainder) | various | `v,v → v` / mixed | `ivp_avg2nx8`, `ivp_extbin_2x32`, `ivp_sext` | DECODED HIGH |
| **TOTAL** | **285** | **1065** | | | | |

> **GOTCHA (†) —** the ISA-wide column is a **prefix bin from the mangled `Iclass` name**, not a semantic classification, so two cells need care. `bool min/max` shows only ~12 ISA-wide because most `B*` ops bin into the literal `B…` prefix while many *emitted* `bmin/bmax/bsubnorm/babssub` variants are counted under their datapath category (compare, abs-diff, count) — the Emitted "31" is the kernel-decoded `b*` reduce family, which spans several `Iclass` prefixes. Likewise `other` is the un-binned remainder, not a real category. Bin by *datapath role* (the lane shape + slot), not by the leading letters, when a precise per-op count matters. CONFIDENCE: **HIGH** (emitted, from `xt-objdump`); **MED** (the ISA-wide prefix bins, which over/under-count across naming boundaries).

> **QUIRK —** `mul/MAC` is the dominant category in *both* columns (50 emitted, ~236 ISA-wide) because the wide MAC is the engine's reason to exist: the entire `s2_mul` slot, the 1536-bit `wvec` accumulator, and the `dualquad8x8`/`quad16x16` config flags exist to feed it. A Q7 kernel that is not MAC-bound is using the engine wrong; the `cptc` and dequantize datapaths (§3) are both `{load → MAC → select/convert}` pipelines. The `gather/scatter` category is tiny (3 emitted) but anchors the embedding path — and is where the one host-image `IVP_MULUSAN_2X32` indirect-index MAC is named.

### Function Map — what grounds each count

| Artifact | File | Role | Confidence |
|---|---|---|---|
| `Iclass_IVP_*_args` (1064 distinct) | `libisa-core.so` (out-of-tree) | The ISA-wide op breadth (≡ 1065 `OPCODEDEF`s) | DECODED HIGH |
| `ncore2gp xt-objdump` over carved `lib0`/`lib3`/SUNDA | `XtensaTools/bin` (out-of-tree) + carved blobs | The 285 emitted ops, by-blob 149/112/182 | DECODED MED |
| `_TIE_xt_ivp32_xb_vec2Nx8U` ×6 | `libnrtucode_extisa.so` `cptc` blobs | The 2Nx8U lane-shape proof | DECODED CERTAIN |
| `get_xml_{post_parse,compiler,xinfo}` | `libtie-core.so` (out-of-tree) | Per-op `OPCODEDEF`/`FIELDDEF`/operand-sem (encoding) | DECODED MED |
| `core.xparm` Vision block | `ncore2gp/config` (out-of-tree) | `simd16=0x20` (lane count), `dualquad8x8_mac=1` (MAC modes) | DECODED HIGH |

> **NOTE —** the op-count figures on this page (**1065** ISA-wide / **285** emitted / **1064** `Iclass_IVP_*_args` / **~12642** `OPCODEDEF` rows / the per-blob **149 / 112 / 182** split / the FLIX format and slot base-bit tables) are grounded on the Cadence/Tensilica `ncore2gp` toolchain — the `.tie` config, `xt-objdump`, `libisa-core.so`, `libtie-core.so`, and `core.xparm`. **None of those artifacts ship in this repository tree** (verified: `find . -name '*.tie' -o -iname '*xt-objdump*' -o -iname 'libisa-core*' -o -iname 'libtie-core*' -o -iname '*xparm*'` returns nothing). They are corroborated by the in-repo `raw/NX-*` notes, but a reimplementer **cannot reproduce these counts from anything shipped here** — they are externally anchored, hence the downgraded confidence above. What *is* in-binary-confirmed (and stays CERTAIN) is the single `IVP_MULUSAN_2X32` builtin name and the six `cptc_decode_impl<1..6>` instantiations carrying the `_TIE_xt_ivp32_xb_vec2Nx8U` type, both present in `libnrtucode_extisa.so` (build-id `7bb03bc4…`).

### Considerations

The Emitted counts are a *union* across three blobs of *different* silicon (Cayman `lib0`/`lib3`, SUNDA), so a single arch sees fewer: SUNDA leans on the move + gather families (`movva32`/`gatheran_2x32t`) for `send_gather_request`/`embedding_update`, while Cayman `lib3` leans on the mul + convert families for the `cptc`/MX-dequantize path. The per-member lane width and signedness in the lane-shape column is read from the canonical suffix grammar — HIGH where the suffix is standard (`_2x32`, `nx16`, `u`/`s`), MED for long-tail `mul*`/`dmul*`/`cvt*` variants whose exact pack width was not each individually `OPCODEDEF`-verified.

---

## 2. The FLIX Format Scheme

### Purpose

IVP ops do not issue alone — they are packed into Tensilica **FLIX** (Flexible Length Instruction eXtensions) bundles, and the *same* op has a different `OPCODEDEF` (different slot, different base bit) in each format it can appear in. A reimplementer cannot decode a single `ivp_*` without first finding the bundle boundary (the length nibble) and the slot base bit (the format). This section is the format axis that the §1 "FLIX slot" column indexes into.

### The length decode

Instruction length is self-describing in the **first nibble** of word0 (`InstBuf[3:0]`), read by `libisa-core.so`'s `length_decoder` / `length_table`:

```text
first nibble (InstBuf[3:0])   length      format class
  0x0 .. 0x7                  3 bytes     x24    (core Xtensa, 24-bit)
  0x8 .. 0xb                  2 bytes     x16a   (density group A)
  0xc .. 0xd                  2 bytes     x16b   (density group B)
  0xe                         16 bytes    l128a  → wide FLIX F3 / F11 (5-slot)
  0xf                         16 bytes    l128b/c → wide FLIX F0/F1/F2/F4/F6/F7 (4-slot)
                          OR   8 bytes    l64a   → narrow FLIX N0/N1/N2 (2-3 slot)
                               (0xf disambiguated by a 2nd InstBuf predicate: 2'b01 / 3'b011 / 1'b0)
```

> **GOTCHA —** nibble `0xf` is **overloaded** — it begins either a 16-byte wide bundle or an 8-byte narrow bundle, split only by a secondary bit predicate inside the fetch word. A decoder that assumes `0xf ⇒ 16 bytes` desyncs the instant it meets a narrow bundle, and every subsequent instruction is garbage. The empirical mix in the carved kernels is **1709 narrow-64 + 966 wide-128** bundles — narrow bundles are the common case, so this is not a corner the decoder can skip. The `length_decoder` in `libisa-core.so` is the reference. CONFIDENCE: **DECODED HIGH**.

### The 14 formats and the slot base-bit map

`libisa-core.so` exports exactly **14** `Format_<F>_encode` symbols: 3 core (`x24`, `x16a`, `x16b`), 8 wide-128 (`F0`, `F1`, `F2`, `F3`, `F4`, `F6`, `F7`, `F11`), 3 narrow-64 (`N0`, `N1`, `N2`). The wide bundles carry **four** slots; **only F3 and F11 carry a fifth** (`s4`). The slot base bits below are read byte-exact from the `Slot_f<fmt>_Format_f<fmt>_s<n>_<class>_<basebit>` symbol names — the `<basebit>` suffix *is* the slot's low bit:

| Format | Length | Slots | `s0` | `s1` | `s2` (Mul) | `s3` (ALU) | `s4` (ALU) |
|---|---|---|---|---|---|---|---|
| `F0` | 16 B | 4 | `ldst`@4 | `ld`@16 | `mul`@28 | `alu`@36 | — |
| `F1` | 16 B | 4 | `ldstalu`@4 | `ld`@16 | `mul`@41 | `alu`@31 | — |
| `F2` | 16 B | 4 | `ldst`@4 | `ld`@16 | `mul`@27 | `alu`@31 | — |
| `F3` | 16 B | **5** | `ldst`@4 | `ld`@16 | `mul`@28 | `alu`@33 | `alu`@24 |
| `F4` | 16 B | 4 | `ld`@4 | `ld`@16 | `mul`@28 | `alu`@36 | — |
| `F6` | 16 B | 4 | `ldst`@4 | `ld`@16 | `mul`@41 | `alu`@36 | — |
| `F7` | 16 B | 4 | `ldst`@4 | `ld`@16 | `mul`@41 | `alu`@31 | — |
| `F11` | 16 B | **5** | `ld`@4 | `alu`@16 | `mul`@41 | `alu`@31 | `alu`@24 |
| `N0`/`N1`/`N2` | 8 B | 2–3 | core+1–2 vector slots | | | | |
| `x24`/`x16a`/`x16b` | 3/2/2 B | 1 | core Xtensa only | | | | |

The canonical slot *classes* are fixed across the wide formats: `s0` = LdSt (scalar control/branch + vector store), `s1` = Ld/ALU (vector load post-inc / predicate logic), `s2` = Mul (the wide MAC → `wvec`), `s3` = ALU (select / arithmetic), `s4` = ALU (arith / abs-diff / bool-reduce, F3/F11 only). The §1 "FLIX slot" column names which class each category lands in.

> **QUIRK —** the slot *base bits move between formats* — `s2_mul` is at bit 28 in F0/F3/F4 but bit 41 in F1/F6/F7/F11 and bit 27 in F2; `s3_alu` ranges over 31/33/36. The same op therefore encodes at different absolute positions per format, which is precisely why there is one `OPCODEDEF` *per (op, format)* and why the ISA defines ~12642 `OPCODEDEF` rows over only 1065 ops. A reimplementer must key the encoding on `(op, format)`, never on the op alone. F11 is the odd one: its `s0` is a *load* (`ld`@4) and its `s1` an *ALU* (`alu`@16), swapping the F0 `s0`/`s1` roles. CONFIDENCE: **DECODED HIGH** (base bits from the `Slot_*` symbol suffixes).

### Function Map — the format providers

| Artifact | File | Role | Confidence |
|---|---|---|---|
| `length_decoder`, `length_table` | `libisa-core.so` (out-of-tree) | First-nibble length decode | DECODED HIGH |
| `Format_{F0,F1,F2,F3,F4,F6,F7,F11,N0,N1,N2,x24,x16a,x16b}_encode` | `libisa-core.so` (out-of-tree) | The 14 FLIX formats | DECODED HIGH |
| `Slot_f3_…_s4_alu_24`, `Slot_f11_…_s4_alu_24` | `libisa-core.so` (out-of-tree) | The 5-slot forms (F3/F11 only) | DECODED HIGH |
| `Slot_f<fmt>_…_s<n>_<class>_<basebit>` | `libisa-core.so` (out-of-tree) | Per-format slot base bits | DECODED HIGH |
| `get_xml_post_parse/compiler/xinfo`, `interface_version` | `libtie-core.so` (out-of-tree) | TIE-XML `OPCODEDEF`/`FIELDDEF` provider | DECODED MED |

### Considerations

The narrow N0/N1/N2 (8-byte) formats carry 2–3 slots and are the dominant emitted form (1709 of 2675 bundles), but their per-slot base bits were not individually transcribed here — they follow the same `Slot_n<fmt>_…` scheme and are recoverable from `libisa-core.so` the same way as the wide forms. The core x24/x16a/x16b formats hold one base-Xtensa instruction (the windowed-ABI scalar/address bookkeeping that dominates the operand-class tally: `AR` ≈ 35,000 vs `vec` ≈ 5,300 across the three blobs), not an IVP op; they are present so a FLIX program can interleave scalar control with vector compute in a single fetch stream.

---

## 3. Worked Encode and Datapath

### Purpose

The §1 category table and the §2 format table meet in a single `OPCODEDEF`: an op's opcode is the AND of slot-local field constants at the format's slot base bit, with operands in the remaining bits. This section walks one op end-to-end, then shows the two canonical kernel datapaths the cataloged ops compose into.

### The opcode-encoding shape

Each op defines one `OPCODEDEF` per FLIX format it issues in. The opcode is the AND of slot-local `fld[hi:lo]==const`; operands occupy the slot's remaining bits; direction (IN/OUT/MODIFY) lives in the iclass arg-list. From the decrypted `get_xml` TIE-XML (`libtie-core.so`):

```text
IVP_MULPAN16XR16 (format F0, slot s2_mul base bit 28):
    s2_mul[27:21]==6  &  [14:12]==1  &  [7:6]==0
      ├─ MAC major opcode [27:21]==6 — shared by the packed/usp/4t MAC family
      └─ sub-op {[14:12],[7:6]} = term-count + signedness selector:
           {4t:0, packed:1, usp:2, pn/sup:5, uup:6}
    operands (slot remaining bits):  vec=5b, vbool=4b, wvec=2b, AR=4b (in-slot)
    direction (iclass ARG_LIST):     wvt:MODIFY (read-accumulate-write wv),
                                     vs/vr:IN(vec), arr:IN(AR scale),
                                     + implicit CPENABLE:IN / Coprocessor1Exception:OUT
```

So `ivp_mulpan16xr16 wv0, v8, v12, pr3` (a packed-accumulate 32×16-bit MAC into accumulator 0) places major `6` and sub-op `{1,0}` into the `s2_mul` field at bit 28+, the `wv` index `0` and `vec` indices `8`/`12` and `pr` index `3` into the slot's operand bits, and reads-modifies-writes `wv0` — the accumulate is in the MODIFY direction, which is why a MAC chain never re-reads the accumulator into a `vec`.

> **NOTE —** the same `IVP_MULPAN16XR16` has a *different* `OPCODEDEF` in F1/F6/F7/F11 (where `s2_mul` is at base bit 41), with the field constant shifted by 13 bits. The op identity is one row in §1; the encoding is one row *per format* in the TIE-XML. CONFIDENCE: **DECODED HIGH** (the `[27:21]==6` constant and sub-op map from the TIE-XML `OPCODEDEF`; the F1-family shift is inferred from the §2 base-bit delta).

### Datapath 1 — the `cptc` decode inner kernel

The compressed-tensor decoder (`cptc_decode_impl<1..6>`, Cayman `lib3`) is the canonical `{load | MAC | select | bool}` pipeline — the six instantiations carry the in-blob `_TIE_xt_ivp32_xb_vec2Nx8U` proof type. The `<1>` body, decoded over an F0-class 16-byte bundle:

```text
# cptc_decode_impl<1>  (entry a1,0x340)  — one F0 bundle:
  s0:  bgeui.w15 a5,16,…                  ; loop/branch control          (s0_ldst @4)
  s1:  ivp_l2a4nx8_ip   v?,a4,a12          ; align-load 2×4 N×8, post-inc (s1_ld @16, valign state)
  s2:  ivp_mulpan16xr16 wv1,v?,v?,pr3      ; packed-accumulate MAC → wv   (s2_mul @28)
  s3:  ivp_sel2nx8i     v0,v2,…            ; immediate lane-select        (s3_alu @36)
  … then ivp_dextrprn_2x32 (pr extract) + ivp_bmin2nx8 (bool reduce) … callx8 a5
```

The N in `cptc_decode_impl<N>` selects `{operand width, MAC term-count, signedness, output pack}`: `<2>` uses `ivp_mul4tn16xr16` (4-term) + `ivp_babssubu2nx8` (SAD); `<3>` uses `ivp_mulsupn16xr16` (signed×unsigned) + `ivp_ultnxf16t` (fp16 compare) + `ivp_baddnormnx16`; `<6>` uses `ivp_muln_2x16x32_0` (16×16→32 widen) + `ivp_mul4t2n8xr8` (dual-quad). Every op is an `Iclass_IVP_*_args` table present in `libisa-core.so`. The common shape is **read a packed input vector → MAC-expand against a codebook/scale into `wvec` → select/pack to the output dtype**.

### Datapath 2 — MX-4-bit → fp16 dequantize

The block-scaled MX-4-bit dequantize path (`decode_tensor_dequantize` / `proc_4bit_mx_8`, `lib3`) is a `{load | MAC | convert | subnorm}` pipeline — it is where the `convert` category's `ivp_float16nx16t` (the int→fp16 expand) does its work:

```text
# proc_4bit_mx_8 dequantize core (DECODED bundle):
  ball.w15 …                                ; predicate/branch
  ivp_lvn_2x16s_i   v23,a4,0x200            ; load packed int16, imm offset   (load)
  ivp_mulusp2n8xr16 wv0,v0,v0,pr9           ; u×s packed 2N×8 → r16 MAC        (mul/MAC)
  ivp_float16nx16t  v17,v8,10,vb0           ; int → fp16 expand (reads rounding STATE) (convert)
  ivp_bsubnormnx16  vb1                     ; subnormal handling               (count/norm)
```

`ivp_float16nx16t` is the MX-4-bit→fp16 dequant expand step; it reads the rounding-mode STATE register and takes an immediate scale and a `vbool` mask. The pairing with `ivp_bsubnormnx16` is the fp-dequant subnormal fixup — the two ops are the dequantize category's reason to exist.

> **QUIRK —** `IVP_MULUSAN_2X32` — the one *uppercase* `IVP_*` builtin name anywhere in `libnrtucode_extisa.so` — is **not** in the 285 emitted by the ELF32 blobs. It lives in the host **raw Q7 memory-image** (`.rodata` ~`0x50000–0x59000`), named in the gather-DMA diagnostic `"Before IVP_MULUSAN_2X32: address=…, last_indices=…, indirection_step=…"`. It is the gather/embedding indirect-index MAC (multiply-unsigned + accumulate-narrow, 2×32-bit lanes), driving the SUNDA `send_gather_request` / `rdma_desc_gen` path. It belongs to the `gather/scatter` category's datapath but is selected by a debug image flavor, not a production ulib — which is why it appears by *builtin name* once while the production blobs carry only the mangled *type*. CONFIDENCE: **HIGH** (string + offset); semantics **MED**.

### Considerations

The complete per-op encoding (every one of the 1065 ops × every FLIX format it issues in, with operand bit-lanes) is the ~12642-`OPCODEDEF` join in the TIE-XML — present on disk in `libtie-core.so` (decryptable via the minimal-tie cipher `p = 0x73 if c==0xff else (c-0x0d)&0x7f`), but not exhaustively transcribed here; this is a *catalog* page keyed on category axes, and the full dump belongs to a dedicated encoding-reference page. The one residual gap is **byte-exact bundle re-splitting of the carved blobs**: carving an ELF32 image out of host `.rodata` zeroes the `.xt.prop` section addresses, so `xt-objdump` cannot drive the property-record FLIX split inside literal-dense stretches and falls back to raw fetch words. Every emitted bundle is *valid* and every `ivp_*` cross-checks the config; only the automatic boundary split needs an `ET_REL` relink with relocated `.xt.prop`. CONFIDENCE: **LOW** (only the automatic split is blocked, not the ISA or any op's identity).

---

## Related Components

| Name | Relationship |
|---|---|
| Vision-Q7 Identification | Proves the ISA *is* IVP and fixes the 8-regfile / FLIX model this page catalogs |
| Xtensa Toolchain & TIE Config | The `ncore2gp` `libisa-core.so` / `libtie-core.so` that source every op count and encoding here |
| Q7 Microcode Blobs | The 13 carved ELF32 images whose `xt-objdump` decode yields the 285 emitted ops |
| `cptc_decode_impl<1..6>` | The compressed-tensor kernels carrying the in-blob `vec2Nx8U` proof type; Datapath 1 |

## Cross-References

- [Tensilica Xtensa and Vision-Q7 Identification](xtensa-vision-q7.md) — the canonical identification record; the 8 regfiles, FLIX model, and three-way proof this catalog builds on (this page is the op roster, that page is the identity)
- [The Xtensa Toolchain and TIE Config](xtensa-toolchain.md) — the shipped `ncore2gp` config, `core.xparm`, `libisa-core.so` / `libtie-core.so`, and the `xt-objdump` that decode every op name and bit-field cited here
- [The 13 Q7 Microcode Blobs](q7-blobs.md) — carving the ELF32 images the 285-op emitted count is decoded from
- [The ext-ISA Provider (nrtucode_* API)](extisa-provider.md) — the host loader that serves the blobs and the raw Q7 memory-image where `IVP_MULUSAN_2X32` is named
- [The 16-Byte UDMA Descriptor](../dma/descriptor-format.md) — the descriptor format the SUNDA gather ops (`rdma_desc_gen`) emit onto the DMA rings
- [back to index](../index.md)
