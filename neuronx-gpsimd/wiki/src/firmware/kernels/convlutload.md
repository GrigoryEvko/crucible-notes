# ConvLutLoad — the legacy LUT-load path of opcode `0xe4`

Opcode `0xe4` (`ConvLutLoad`) is **dual-purpose**, and the discriminator is a
single operand byte: `lut_dtype` at struct offset **47**. When that byte is one
of the two *legacy sentinels* — `Invalid(0x0)` or `FP32(0xA)` — the instruction
is the **classic 4-bit data-converter LUT load**; when it is anything else (the
`CPTC1..7 = 0x19..0x1F` codes) the instruction is the **CPTC compressed-tensor
decode** documented in the sibling [CPTC codec](cptc-codec.md) page. This page
owns the **legacy half** — the `lut_dtype ∈ {Invalid, FP32}` branch — and it
delivers a verdict that closes the `0xe4` picture from the other side: **the
GPSIMD / POOL firmware never executes the legacy LUT load.** The legacy
converter LUT is a **PE-array** operation; the only POOL-side body behind `0xe4`
is the CPTC dispatcher. The two purposes share one opcode byte and one 64-byte
operand struct (`NEURON_ISA_TPB_S2_CONVLUT`), but they live on **different
engines**.

This page delivers six things, each byte-grounded against the shipped binaries:
(1) the **branch mechanism** — the `s2_convlut_is_cptc` predicate on
`lut_dtype@47`, read verbatim from the compile-verified ISA header; (2) the
**dispatch census** proving the legacy leg has **no POOL slot** — the base POOL
library `EXTISA_0` carries no `0xe4` entry, and the only `0xe4` slot
(`EXTISA_3` idx7 → `pool_conv_lut_load`) exists exclusively for CPTC; (3) **what
the legacy LUT is** — a 128-row, per-row, 16-entry, 4-bit input data-converter
table for the PE array, stored `UINT16`-wide in SBUF; (4) the **operand struct**
`NEURON_ISA_TPB_S2_CONVLUT` (64 B, compile-verified) with its legacy field roles
and reserved-zero constraints; (5) the **dtype matrix** binding legacy ↔
`(UINT16, 16-elem)` against CPTC ↔ `(UINT32, 4-elem)`; and (6) the **per-gen
presence**, including a gen-dependent `valid_nc` refinement that makes the
legacy leg ISA-legal at NC-v4 even though there is no POOL body for it.

Everything below is re-grounded this pass. The dispatch census and the
dispatcher prologue are carved out of `libnrtucode_internal.so` (host sha256
`b7c67e89…`, re-hashed MATCH) by `dd` identity-map and read with the shipped
device-native `xtensa-elf-objdump`/`readelf` (`XTENSA_CORE=ncore2gp`, FLIX/VLIW);
the operand struct, the `is_cptc`/`valid_nc` predicates, and the dtype enum are
read byte-for-byte (and `gcc`-compile-verified) from the in-package `arch-isa`
headers; the SEQ-side handler grouping and the env-gate are read from the host
lib's string sections.

Confidence tags per [the Confidence & Walls model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / disasm / header / hash; `[MED/INFERRED]` =
reasoned over OBSERVED (often across a FLIX/literal-pool desync);
`[…/CARRIED]` = re-used at a sibling report's confidence without re-reading.

> **GOTCHA — the headline is a negative result.** The deliverable for the legacy
> leg is that **it has no POOL implementation**. A reimplementor building a
> Vision-Q7 GPSIMD must *not* expect a POOL-side 4-bit-converter LUT loader: the
> POOL `0xe4` handler is a pure CPTC router (it branches `is_cptc` and the legacy
> arm has nowhere to go on POOL). The legacy converter LUT belongs to the **PE
> array's** matmul/convolution datapath, which is a different engine entirely and
> is out of this corpus. [HIGH/OBSERVED]

---

## 1. The branch split — `is_cptc` on `lut_dtype@47`

The discriminator is the ISA header's own validity function, read byte-for-byte
from `aws_neuron_isa_tpb_s2_convlut.h` (cayman; "ISA header for NC-v3") and
re-confirmed this pass: [HIGH/OBSERVED]

```c
/* aws_neuron_isa_tpb_s2_convlut.h — verbatim (header validity fns, read this pass). */
bool s2_convlut_is_cptc(Inst i) {
    return i.s2_convlut.lut_dtype != Dtype::Invalid    /* Invalid == 0x0 */
        && i.s2_convlut.lut_dtype != Dtype::FP32;      /* FP32    == 0xA */
}
```

The enum values are compile-grounded from `aws_neuron_isa_tpb_common.h`
(cayman, read this pass): `NEURON_ISA_TPB_DTYPE_INVALID = 0x0`,
`NEURON_ISA_TPB_DTYPE_FP32 = 0xA`, `NEURON_ISA_TPB_DTYPE_UINT16 = 0x5`,
`NEURON_ISA_TPB_DTYPE_UINT32 = 0x9`. Therefore: [HIGH/OBSERVED]

* **LEGACY leg** ⟺ `lut_dtype ∈ {Invalid(0x0), FP32(0xA)}` — the
  `is_cptc == false` half. *This page.*
* **CPTC leg** ⟺ `lut_dtype ∈ {0x19..0x1F}` (`CPTC1..7`) — the `is_cptc == true`
  half. *[CPTC codec](cptc-codec.md).*

The header carries the designer's own remark explaining why `FP32` is a legacy
sentinel and not a real dtype here: *"Have to add FP32 here because the default
value of `Dtype` when unspecified is FP32."* In other words, an operand that
simply does **not** set `lut_dtype` defaults to `FP32(0xA)` and so falls into the
legacy arm — the legacy LUT load is the *unspecified-dtype* default behavior, and
CPTC is the *explicitly-coded* opt-in. The two-value sentinel set
`{Invalid, FP32}` exists precisely to make "no CPTC code present" route to legacy.

> **NOTE — this is the exact predicate the CPTC page branches on.** The
> [CPTC codec](cptc-codec.md) §4.1 reads the *same* `s2_convlut_is_cptc` test and
> branches `is_cptc == true` into the CPTC decode; this page reads it from the
> *legacy* side and routes `is_cptc == false` away from the POOL decoder. The two
> pages share the predicate byte-for-byte (`lut_dtype@47`, the `{0x0,0xA}`
> sentinels). A reimplementation must branch on `lut_dtype` **inside** the `0xe4`
> handler, not on a distinct opcode. [HIGH/OBSERVED both pages]

### 1.1 The structural binding of the two legs

The header does not stop at the `lut_dtype` sentinel: it cross-binds each leg to
a specific `in_dtype` and source element count, so the two legs are
**type-and-shape disjoint**. From `s2_convlut_check_dtype` and
`has_conv_lut_load_src_element_cnt` (both read verbatim this pass): [HIGH/OBSERVED]

```c
/* s2_convlut_check_dtype — verbatim (header, read this pass). */
bool s2_convlut_check_dtype(Inst i) {
    return (( !s2_convlut_is_cptc(i) && i.in_dtype == Dtype::UINT16)   /* LEGACY: UINT16 */
         || (  s2_convlut_is_cptc(i) && i.in_dtype == Dtype::UINT32))  /* CPTC:   UINT32 */
        && is_valid_dtype(i.in_dtype, /*DtypeAllowFP32R=*/false);
}
/* has_conv_lut_load_src_element_cnt — verbatim (header, read this pass). */
/*   LEGACY : num_elem[0]==16 && num_elem[1]==1   (the 16-entry table; transpose [1]==16 also legal) */
/*   CPTC   : num_elem[0]== 4 && num_elem[1]==1   (a 4-unit compressed block)                         */
```

So the firmware (and any reimplementation) does not even need to read
`lut_dtype` to know which leg it is on once the operand is validated: legacy is
`(UINT16, 16-element)` and CPTC is `(UINT32, 4-element)`. The `lut_dtype@47`
byte is the **routing** discriminator; the `(in_dtype, num_elem)` pair is the
**validation** binding that the assertions enforce on each arm. [HIGH/OBSERVED]

The legacy arm additionally requires the **repurposed** operand bytes to read
zero — the bytes that carry CPTC scale/block parameters otherwise: [HIGH/OBSERVED]

```c
/* s2_convlut_legacy_reserved_zero — verbatim (header, read this pass). */
bool s2_convlut_legacy_reserved_zero(Inst i) {
    return s2_convlut_is_cptc(i)                          /* CPTC leg: unconstrained here */
        || (i.multiplicative_const[0] == 0               /* LEGACY: @40 must be 0 */
         && i.multiplicative_const[1] == 0               /* LEGACY: @42 must be 0 */
         && i.block_size == 0);                           /* LEGACY: @46 must be 0 */
}
```

i.e. in the legacy arm, operand bytes `@40`, `@42`, and `@46` **must be zero**
(they are the CPTC `multiplicative_const`/`block_size` fields, meaningless to the
legacy load). This is the byte-exact expression of "the legacy load uses only
header/events/src/in_dtype/rows/cols/row_grp/col_grp; everything CPTC-specific is
reserved-zero." [HIGH/OBSERVED]

---

## 2. The dispatch census — the legacy leg has NO POOL slot

The central FW-43 finding is established not by tracing a legacy body (there is
none) but by a **kernel-info-table census** across both POOL libraries. The host
resolver stages, per POOL coretype, either the **base** library `EXTISA_0`
(lib UID 0, default) or the **CPTC superset** `EXTISA_3` (lib UID 3) when the
env-gate is set (§6). We carved and re-decoded both this pass.

### 2.1 `EXTISA_0` (base POOL lib) — 17 entries, NO `0xe4`

Carved `CAYMAN_EXTISA_0` (host file off `0x2ef7e0`, size `0xa260`, sha256
`910d41c3…`). Its `kernel_info_table` at **VMA `0x02000380` / file `0x7400`**
has 17 records `{b0=0, b1=0, spec, opcode, funcVA(LE,4)}`, decoded byte-exact
this pass: [HIGH/OBSERVED]

| idx | opcode/spec | idx | opcode/spec | idx | opcode/spec |
|----:|:-----------:|----:|:-----------:|----:|:-----------:|
| 0 | `0x7e`/0 | 6 | `0xf0`/0 | 12 | `0x46`/0 |
| 1 | `0x7c`/0 | 7 | `0xf0`/1 | 13 | `0x47`/0 |
| 2 | `0x7d`/0 | 8 | `0xf0`/2 | 14 | `0xbe`/0 |
| 3 | `0x45`/0 | 9 | `0xf0`/4 | 15 | `0xf2`/0 |
| 4 | `0x51`/0 | 10 | `0xf0`/3 | 16 | `0x7b`/0 |
| 5 | `0x41`/0 | 11 | `0x52`/0 | | |

The `0xf0` specs present are exactly `{0,1,2,3,4}` — the **base** ExtendedInst
bridge (copy / extcopy / exttensortensor / rand). **`0xe4` is absent; `0xf0`
spec-7 is absent.** The base POOL library cannot dispatch `ConvLutLoad` at
all — neither leg. [HIGH/OBSERVED bytes]

```
EXTISA_0 kernel_info @ file 0x7400 (xxd, this pass):
  7400: 0000 007e 8000 0001  0000 007c f803 0001   ; idx0 0x7e, idx1 0x7c
  ...
  7430: 0000 00f0 7033 0001  0000 01f0 8033 0001   ; idx6 0xf0/0, idx7 0xf0/1
  7440: 0000 02f0 8434 0001  0000 04f0 a837 0001   ; idx8 0xf0/2, idx9 0xf0/4
  7450: 0000 03f0 603a 0001  ...                    ; idx10 0xf0/3   <- NO spec7
  ⇒ exhaustive scan of all 17 b3 bytes: 0xe4 NOT present; 0xf0/spec7 NOT present.
```

### 2.2 `EXTISA_3` (CPTC superset) — `0xe4` slot exists, but it is the CPTC dispatcher

Carved `CAYMAN_EXTISA_3` (host file off `0x2fbf00`, size `0x6974`, sha256
`052ac31c…`, == the [CPTC codec](cptc-codec.md) anchor). Its 9-entry
`kernel_info_table` at **VMA `0x020008c8` / file `0x4748`** decoded byte-exact
this pass: [HIGH/OBSERVED]

| idx | opcode | spec | funcVA | role |
|----:|:------:|:----:|--------|------|
| 6 | `0x7b` | 0 | `0x01001964` | decode_tensor_dequantize |
| **7** | **`0xe4`** | **0** | **`0x01002258`** | **`pool_conv_lut_load` = the CPTC dispatcher** |
| **8** | **`0xf0`** | **7** | **`0x01003b64`** | **`ExtendedInstCptcDecode` = the ext-inst bridge** |

```
EXTISA_3 kernel_info @ file 0x4748 (xxd, this pass):
  4778: 0000 007b 6419 0001  0000 00e4 5822 0001   ; idx6 0x7b@0x1964, idx7 0xe4@0x01002258
  4788: 0000 07f0 643b 0001                         ; idx8 0xf0/7@0x01003b64
  ⇒ idx7 = {b0=0,b1=0,spec=0,opcode=0xe4, fv=0x01002258}  = pool_conv_lut_load
```

The `funcVA 0x01002258` resolves to the `.xt.prop` symbol `pool_conv_lut_load`
(name present in the carved blob's string table this pass). MARIANA's table is
structurally identical with `idx7` relocated `+0x8` (`0x01002260`) and `idx8`
`+0x10` (`0x01003b74`). [HIGH/OBSERVED CAYMAN; MARIANA CARRIED [CPTC codec](cptc-codec.md)]

> **GOTCHA — the only POOL `0xe4` is the CPTC dispatcher.** `pool_conv_lut_load`
> @`0x01002258` is the body the CPTC codec page documents — it reads the operand,
> and (for the CPTC leg) runs the 6-arm `callx8 a3` ladder into
> `cptc_decode_impl<1..6>`. There is **no second body** for the legacy leg
> between the operand read and the two `retw.n` exits (`0x01002388` /
> `0x0100239a`). The legacy operand, if it ever reached this POOL handler, has no
> legacy load path here. [HIGH the prologue/exits/census; MED the exact in-body
> legacy-reject vs fall-through — the body is FLIX-desynced, §7]

### 2.3 The dispatcher prologue (byte-exact)

The `0xe4` body's prologue is scalar and resyncs cleanly. At VMA `0x01002258`
(carved-blob file `0x2358` = `0x100 + (0x01002258 − 0x01000000)`): [HIGH/OBSERVED]

```
01002258: 36 41 00   entry  a1, 32        ; minimal frame — a thin router
0100225b: 8f …       (FLIX wide-bundle format selector — DESYNCS linear sweep)
0100225c: 42 20 03   l32i   a4, a0, 12    ; read the operand-state ptr
…
0100239a: 1d f0      retw.n               ; one of two exits
```

Raw bytes at file `0x2358`: `36 41 00 8f 42 20 03 …`. The `entry a1,32` + the
`l32i a4,a0,12` operand-ptr read are the all-phases-agree clean anchors; the
`8f` at `0x0100225b` is the first FLIX wide-bundle selector that desyncs stock
objdump's linear sweep — see §7 for the desync honesty flag. [HIGH/OBSERVED]

> **NOTE — VMA == file offset only inside `.text`/`.rodata`.** In the carved
> `EXTISA_3` blob, `.text` VMA `0x01000000` maps to file `0x100`, so the file
> offset of a `.text` VMA is `0x100 + (VMA − 0x01000000)`. Do **not** apply the
> `ncore2gp` config-DLL `.data` delta (`0x200000`) here — this is a `.text`
> address, where VMA==fileoff in-section after the `0x100` ELF-header skew. The
> `kernel_info_table` at VMA `0x020008c8` lives in `.rodata` (file `0x4748`),
> also VMA==fileoff in-section. [HIGH/OBSERVED]

---

## 3. What the legacy LUT is

The legacy ConvLutLoad LUT is **not** an activation table, **not** a dequant
codebook, and **not** a POOL/GPSIMD object. The ISA header is the authoritative
description, read verbatim this pass: [HIGH/OBSERVED]

* **Purpose** (header lines 27–29): *"This instruction is used to set the LUT
  data converter tables for 4-bit conversion. Currently this functionality
  [is] only supported in the PE array."*
* **Source** (lines 46–50): `src_mem_pattern` is a *"Tensor containing the LUT
  table. Has to contain exactly 16 elements, one for each table entry. Only
  non-indirect tensors are allowed (Tensor2d)."*
* **`in_dtype`** (lines 53–57): *"Only UINT16 is allowed. This does not represent
  the actual datatype stored in the LUT, only how much space it occupies in the
  SBUF."* ⟹ the 16 entries are stored **16-bit-wide** in SBUF, but each encodes
  a **4-bit-conversion** value (16 entries = the 16 possible 4-bit input codes).
* **`num_active_rows`** (lines 60–64): *"The number of active rows in the
  PE-array. Has to be set to 128 (no support for partial row programming)."*
* **`num_active_cols`** (lines 67–72): *"Has to be set to 128. This value has no
  meaning for this instruction (LUTs are per row), but setting it explicitly
  simplifies handling in hardware."*
* **`row_grp` / `col_grp`** (lines 75–90): both must be `0xf` (rows/cols
  `00–127`); all other values *"illegal/unpredictable."*

So the legacy LUT is a **128-row PE-array, per-row, 16-entry, 4-bit input
data-converter table**, stored `UINT16`-wide in SBUF. It is the table the PE
array uses to map a packed **4-bit input code** to its converted MAC operand
value — a low-bit weight/activation converter. It is a **convolution/matmul**
input-conversion LUT — the *"Conv"* in *ConvLutLoad* — consumed during
weight/input loading by the PE MAC. [HIGH/OBSERVED]

### 3.1 Rule-outs against the named siblings

The legacy LUT is frequently confused with three other GPSIMD lookup
mechanisms. Each is a **different table, engine, and load path**: [HIGH/OBSERVED CARRIED]

| candidate | what it actually is | why it is NOT the ConvLutLoad LUT |
|-----------|--------------------|-----------------------------------|
| **Activation PWL** ([activation/transcendental tables](../../uarch/activation-transcendental-tables.md)) | a piecewise-**polynomial** (cubic `d0..d3` + breakpoint) machine fed by a host-loaded **ACT PROFILE** table on the **ACT** engine | different engine (ACT, not PE), different table (profile coeffs, not 16 4-bit entries), different load (host staging, not a `0xe4` instruction) |
| **Dequant codebook** ([TensorDequantize](tensor-dequantize.md)) | an **NF4** 16-entry NormalFloat4 codebook **embedded** in its own POOL kernel (`proc_4bit`) | not loaded via `ConvLutLoad`; it is a POOL-side constant, not a PE-array converter |
| **CPTC dictionary** ([CPTC codec](cptc-codec.md)) | the CPTC compressed-tensor **decode** (the `0xe4` CPTC leg) — a bit-plane de-interleave + scale-MAC, **not** a 16-entry table | same opcode `0xe4`, different `lut_dtype` (`0x19..0x1F`), different engine (POOL), different operand binding (`UINT32`, 4-elem) |

The NF4-vs-ConvLutLoad distinction is the subtle one: both are *4-bit* and both
are *16-entry*, so it is tempting to conflate them. They are not the same object.
The NF4 codebook is a **dequantization** codebook (4-bit index → fp value)
embedded in the POOL `TensorDequantize` kernel; the ConvLutLoad LUT is a **PE
MAC input-conversion** table programmed per-row into PE-array converter
registers. Different engines, different load mechanisms, different consumers.
[HIGH/OBSERVED CARRIED — see [TensorDequantize](tensor-dequantize.md)]

---

## 4. The operand struct — `NEURON_ISA_TPB_S2_CONVLUT` (64 B)

Source: `aws_neuron_isa_tpb_s2_convlut.h` (cayman), bound by
`instruction_mapping.json` to `NEURON_ISA_TPB_OPCODE_CONV_LUT_LOAD = 0xe4`
(common.h line 294, `// Y` opcode-present marker). **Compile-verified this pass**
(`gcc -std=c11`): `sizeof == 64`; `offsetof` gives `src_mem_pattern=16`,
`in_dtype=32`, `num_active_rows=38`, `num_active_cols=39`,
`multiplicative_const=40`, `row_grp=44`, `col_grp=45`, `block_size=46`,
`lut_dtype=47`. The struct is **shared** with the CPTC leg — it is the same 64 B
typedef — so this table is the union of both roles, annotated for the legacy
arm. [HIGH/OBSERVED]

| off | size | field | type | LEGACY role (`lut_dtype ∈ {Invalid, FP32}`) |
|----:|-----:|-------|------|---------------------------------------------|
| 0–3 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{ opcode=0xe4 ConvLutLoad, inst_word_len }` |
| 4–11 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | wait/update sync |
| 12–15 | 4 | `reserved0[4]` | — | `==0` (`s2_convlut_reserved_zero`) |
| 16–27 | 12 | `src_mem_pattern` | `TENSOR2D` | the **16-entry `UINT16` LUT** source (SBUF, non-indirect) |
| 28–31 | 4 | `reserved1[4]` | — | `==0` |
| 32 | 1 | `in_dtype` | `NEURON_ISA_TPB_DTYPE` | `== UINT16(0x5)` (SBUF footprint only, **not** the stored datum) |
| 33–37 | 5 | `reserved2[5]` | — | `==0` |
| 38 | 1 | `num_active_rows` | `uint8_t` | `== 128` (`PE_ARRAY_NUM_ROWS`) |
| 39 | 1 | `num_active_cols` | `uint8_t` | `== 128` (no meaning; HW-handling aid) |
| 40–43 | 4 | `multiplicative_const[2]` | `uint16_t[2]` | **BOTH `==0`** for legacy (`s2_convlut_legacy_reserved_zero`) |
| 44 | 1 | `row_grp` | `uint8_t` | `== 0xf` (rows `00–127`) |
| 45 | 1 | `col_grp` | `uint8_t` | `== 0xf` (cols `00–127`) |
| 46 | 1 | `block_size` | `uint8_t` | **`==0`** for legacy (CPTC-only field) |
| 47 | 1 | `lut_dtype` | `NEURON_ISA_TPB_DTYPE` | **`== Invalid(0x0)` or `FP32(0xA)` — THE LEGACY SENTINEL** |
| 48–63 | 16 | `reserved4[16]` | — | `==0` (`s2_convlut_reserved_zero`) |

So the **legacy load consumes exactly**: `header`, `events`, `src_mem_pattern`
(the 16-entry `UINT16` LUT), `in_dtype(=UINT16)`, `num_active_rows(=128)`,
`num_active_cols(=128)`, `row_grp(=0xf)`, `col_grp(=0xf)`. It does **not** use
`multiplicative_const` or `block_size` (both reserved-zero in the legacy arm),
and `lut_dtype` serves **only** as the discriminator (`Invalid`/`FP32`). There is
no index/stride beyond the `Tensor2d`'s own `MemPattern2d`
(`num_elem[0]==16`, `num_elem[1]==1`, the 16-entry shape). [HIGH/OBSERVED]

> **CORRECTION — bytes `40..47` were repurposed; the older field-doc table is
> pre-CPTC.** The header's prose field-doc comment block (lines 104–107) calls
> bytes `40..63` *`reserved3[4] + row_grp + col_grp + reserved4[18]`* — that is
> the **pre-CPTC** layout, where `40..43` and `46..47` were all reserved. The
> **actual `typedef`** (lines 123–128) repurposed those bytes:
> `multiplicative_const[2]@40`, `block_size@46`, `lut_dtype@47`. The compile
> (`sizeof==64`, `ISA_STATIC_ASSERT`) and `offsetof` are the witnesses. In the
> **legacy** arm the repurposed bytes `40/42/46` must read zero (so the
> instruction is wire-compatible with the pre-CPTC layout), and `lut_dtype@47` is
> the `{0x0,0xA}` sentinel. [HIGH/OBSERVED]

> **CORRECTION — `reserved4` is 16 bytes, not 18.** The `typedef` annotates
> `reserved4[16]` with a stale comment `// 18 (48 - 63)`. `48..63` is **16**
> bytes, and 16 is exactly what brings the struct to the asserted 64 B (the
> `s2_convlut_reserved_zero` predicate also enumerates exactly
> `reserved4[0..15]`). The width annotation is a header typo carried from the
> pre-CPTC `reserved4[18]`; the field is 16 B. [HIGH/OBSERVED]

### 4.1 Cross-gen struct stability

The 64 B layout is **byte-identical** across cayman (NC-v3), mariana (NC-v4), and
maverick (NC-v5) headers — diffing the three `s2_convlut.h` files this pass shows
only (a) the doc-comment NC-version string, (b) a `Tensor2d` typedef rename
(`NEURON_ISA_TPB_TENSOR2D` → `NEURON_ISA_TPB_MEM_PATTERN2D`, a same-size
wrapper), and (c) the predicate-body texts (the `valid_nc` refinement, §6.2).
**No field offset moves**, so a reimplementation can use one 64 B struct across
all gens. [HIGH/OBSERVED]

---

## 5. The load mechanism (legacy)

**On the GPSIMD / POOL core: there is no legacy load mechanism.** The POOL
firmware never executes the legacy LUT load (§2). The load lands in the **PE
array's per-row converter-table registers**, programmed by the PE engine when it
executes ConvLutLoad as a PE micro-op (the PE engine and its converter-table SRAM
are **out of this corpus**). What the *contract* specifies about the load: [HIGH the contract; the PE-side microarchitecture INFERRED/out-of-corpus]

* **Source layout**: a `Tensor2d` of **exactly 16 `UINT16`-wide elements**
  (16-entry table), non-indirect, SBUF-resident
  (`tensor2d_valid(..., AllowedInPSUM::False, AllowedInSBUF::True)`).
* **Destination**: the PE-array per-row 4-bit converter tables — **128 rows**
  (`num_active_rows==128`, no partial programming; *"LUTs are per row"*). The
  16-entry table is broadcast/replicated **identically per row**: one table per
  PE row, programmed by the PE-array LUT-load hardware.
* **Width**: 16 entries × `UINT16` = **32 bytes** of SBUF read per row group; the
  converter value actually used is the low 4-bit datum (the `UINT16` is only the
  SBUF footprint). The src `start_addr` is channel-validated
  (`start_addr_active_channels(start_addr, num_active_rows)` on cayman;
  `check_m2d_active_channels(...)` on mariana — a rename, same intent).
* **No format conversion on load**: the 16 entries are loaded **as-is** into the
  converter table; the "conversion" the LUT performs happens later, in the PE
  MAC, when a 4-bit input code indexes the loaded table.

The following is the PE-side load shape **as specified by the contract** (the
loop microarchitecture is INFERRED — the PE engine is out of corpus):

```c
/* LEGACY ConvLutLoad — PE-engine load (CONTRACT shape; PE microarchitecture INFERRED).
 * The GPSIMD/POOL firmware has NO body for this; it lands in PE-array converter SRAM. */
void pe_conv_lut_load_legacy(const NEURON_ISA_TPB_S2_CONVLUT *op) {
    /* validation (from the header assertions, OBSERVED): */
    assert(op->lut_dtype == DTYPE_INVALID || op->lut_dtype == DTYPE_FP32); /* legacy sentinel  */
    assert(op->in_dtype  == DTYPE_UINT16);                                 /* SBUF footprint    */
    assert(op->src_mem_pattern.num_elem[0] == 16 &&
           op->src_mem_pattern.num_elem[1] == 1);                          /* exactly 16 entries*/
    assert(op->num_active_rows == 128 && op->num_active_cols == 128);      /* full PE array     */
    assert(op->row_grp == 0xf && op->col_grp == 0xf);                      /* rows/cols 00-127  */
    assert(op->multiplicative_const[0] == 0 &&
           op->multiplicative_const[1] == 0 && op->block_size == 0);       /* legacy reserved-0 */

    /* load: read 16 UINT16 entries from SBUF, broadcast identically to all 128 PE rows.   */
    uint16_t table[16];
    sbuf_read16(op->src_mem_pattern.start_addr, table, 16);               /* 32 B per row grp */
    for (int row = 0; row < 128; ++row)                                   /* "LUTs are per row" */
        pe_converter_table_program(row, table);    /* INFERRED: identical table per row     */
    /* NO sub-byte unpack, NO scale, NO fp-reconstruct (those are the CPTC leg's, cptc-codec.md). */
    /* The 4-bit -> value conversion is applied LATER, in the PE MAC at matmul/conv time.    */
}
```

By contrast the CPTC leg ([CPTC codec](cptc-codec.md)) lands a *decompressed
tensor* in the POOL datapath via six 0x100-byte permutation tables + IVP
unpack/scale/fp-reconstruct. The two "loads" share **nothing but the opcode byte
and the 64 B struct**. [HIGH/OBSERVED CARRIED [CPTC codec](cptc-codec.md)]

---

## 6. The consumer, the env-gate, and per-gen presence

### 6.1 The consumer — the PE-array matmul / convolution datapath

The legacy LUT is read by the **PE-array matmul/convolution** MACs. Two
independent OBSERVED signals pin this: [HIGH/OBSERVED grouping; MED the exact in-MAC tap]

1. **The header** (verbatim): the converter LUT *"functionality is only
   supported in the PE array."*
2. **The SEQ-side handler grouping**: in the host lib's DEBUG self-naming
   strings (read this pass), `"S: ConvLutLoad"` sits **immediately adjacent** to
   the PE micro-op handler block:

   ```
   S: ConvLutLoad
   S: PeManageSeed : micro-op : LdWeight
   S: PeManageSeed : micro-op : Matmul : is_load=%d
   S: LdweightsMX
   S: MatmulMX
   S: MatmulSparse
   S: LdTags
   ```

   ⟹ `ConvLutLoad` is a **PE-engine SEQ op** grouped with weight-load and matmul
   micro-ops. The PE array reads the loaded per-row 4-bit converter LUT during
   weight/input loading (`LdWeight`/`LdweightsMX`) and applies it across the MAC
   (`Matmul`/`MatmulMX`) — the LUT converts the packed 4-bit weights/inputs to
   the MAC's operand precision. The exact tap (at `LdWeight` unpack vs at the
   multiplier input) is in the PE datapath, which is **not in this corpus** —
   recorded MED/INFERRED. [HIGH grouping; MED tap]

The CPTC leg's consumer is **different**: the `cptc_decode` output tensor is read
by whatever downstream op consumes the reconstructed tensor (a question owned by
the CPTC family). The legacy LUT and the CPTC output have unrelated consumers.

### 6.2 Per-gen presence and the `valid_nc` refinement

Two distinct things vary per generation: (a) whether the **POOL `0xe4` slot**
exists (the CPTC dispatcher), and (b) whether the **legacy leg is ISA-legal** in
the operand-validation contract (`s2_convlut_valid_nc`). [HIGH/OBSERVED]

| gen (NC) | POOL `0xe4` slot (`EXTISA_3`) | legacy leg ISA-legal? (`valid_nc`) | CPTC dtype enum |
|----------|:-----------------------------:|:----------------------------------:|:---------------:|
| SUNDA (v2) | **ABSENT** — no `EXTISA_3`; no `s2_convlut.h` header at all | n/a (no ConvLutLoad on SUNDA) | absent `[CARRIED]` |
| CAYMAN (v3) | **PRESENT** — sha `052ac31c`, idx7 `0xe4`@`0x01002258` | **NO** — `valid_nc = is_cptc && nc>=V3` (CPTC-only) | **absent** (0 CPTC in cayman common.h) |
| MARIANA (v4) | **PRESENT** — sha `8477ff26`, idx7 `0xe4`@`0x01002260` (+0x8) | **YES** — `valid_nc = (nc==V4) \|\| (is_cptc && nc>=V3)` | `CPTC1..7 = 0x19..0x1F` |
| MARIANA_PLUS (v4+) | **PRESENT** — byte-identical to MARIANA `[CARRIED]` | YES (V4 header) | present `[CARRIED]` |
| MAVERICK (v5) | **PRESENT (header-OBSERVED)** — `kernel_info` `0xe4`/`0xf0`-spec7 rows + CPTC DEBUG strings | **YES** — header `valid_nc = (nc==V4) \|\| (is_cptc && nc>=V3)` | present (header) |

> **CORRECTION — the legacy leg becomes ISA-legal at NC-v4, even though there is
> still no POOL body.** The cayman (NC-v3) header gates `valid_nc =
> s2_convlut_is_cptc(i) && nc >= V3` — so on V3 a *non-CPTC* (legacy)
> ConvLutLoad is **rejected** by validation. But the mariana/maverick (NC-v4)
> header *adds an arm*: `valid_nc = (nc == V4) || (is_cptc && nc >= V3)`, read
> verbatim this pass. The `(nc==V4)` disjunct admits the legacy leg unconditionally
> at V4. This is the ISA contract growing the legacy converter LUT into a legal
> NC-v4 instruction — but it is a **PE-engine** instruction; the POOL `EXTISA_3`
> body remains a pure CPTC dispatcher (§2). So "legacy is ISA-legal at v4" and
> "legacy has no POOL body" are both true and not in tension: the legality is for
> the PE engine. [HIGH/OBSERVED — both header texts read this pass]

> **GOTCHA — the codec slot is CAYMAN+ but the CPTC dtype enum is MARIANA+.**
> CAYMAN silicon ships the `EXTISA_3` `0xe4` dispatcher (the CPTC decode body),
> but the cayman `aws_neuron_isa_tpb_common.h` has **zero** `CPTC1..7` enumerants
> (re-confirmed: 0 CPTC hits this pass), while the mariana header defines
> `CPTC1..7 = 0x19..0x1F`. So on CAYMAN the decode machinery exists but the v3 ISA
> cannot legally *name* a CPTC `lut_dtype` to it; CPTC becomes a usable surface at
> **MARIANA**. This mirrors the split the [datatype model](dtype-model.md)
> records. [HIGH/OBSERVED both legs]

### 6.3 The env-gate

The POOL `0xe4` slot is **never reachable by default**. The host resolver stages
the `EXTISA_3` library (lib UID 3) **only** when
`getenv("NRT_UCODE_UNSTABLE_LIBRARY_FLAG_CPTC_DECODE")` is set (string read in
the host lib this pass), on a PERF coretype; otherwise lib UID 0 (`EXTISA_0`, no
`0xe4`) is staged. So there is **no env/lib state** in which POOL dispatches a
*legacy* ConvLutLoad: the only POOL `0xe4` is the env-gated CPTC dispatcher.
[HIGH/OBSERVED string; CARRIED RT-10 for the staging logic]

### 6.4 The dtype matrix

| | LEGACY leg (POOL: not implemented; PE-engine contract) | CPTC leg (POOL: implemented — [CPTC codec](cptc-codec.md)) |
|---|---|---|
| `lut_dtype@47` | `Invalid(0x0)` \| `FP32(0xA)` (the legacy sentinels; `FP32` = "unspecified default") | `CPTC1..7 = 0x19..0x1F` (`bit-count = code & 0x7`; `is_cptc = (code&0xF8)==0x18 && (code&0x7)!=0`) |
| `in_dtype@32` | `UINT16(0x5)` (SBUF footprint of the 16-entry table; not the stored datum) | `UINT32(0x9)` (transport; `out_dtype` validated separately) |
| `src num_elem[0]` | `16` (or `[1]==16`, transpose) | `4` (or `[1]==4`) |
| stored datum | 4-bit conversion table values (16 entries = the 16 possible 4-bit codes) | sub-byte 1..7-bit codes, bit-plane-permuted |
| engine | **PE array** (matmul/conv MACs) | **POOL/GPSIMD** (`pool_conv_lut_load` → `cptc_decode_impl<N>`) |
| reserved-0 bytes | `mult_const[0/1]@40,42 == 0`, `block_size@46 == 0` | those bytes carry the CPTC scale/block params |

---

## 7. Honesty: FLIX desync and the out-of-corpus PE engine

* **FLIX-literal desync.** `pool_conv_lut_load` @`0x01002258` is hand-scheduled
  FLIX VLIW with a wide-bundle format selector byte (`0x8f` at `0x0100225b` is the
  first such) that desyncs stock `xtensa-elf-objdump`'s linear sweep. Many
  decoded "instructions" inside the body (apparent out-of-image `call0` targets,
  backward branch "loops", inline `ivp_*` bundles) are **desync artifacts, not
  genuine instructions**, and are NOT asserted. This is confirmed two ways:
  (a) **zero relocations** in `0x01002258..0x010024b4` (`readelf -r`), so the
  apparent external calls are not real relocs; (b) the all-phases-agree robust
  set (`entry a1,32`; `l32i a4,a0,12`; the two `retw.n` exits; the 192-frame CPTC
  worker prologue downstream) is what is reported HIGH. The **exact in-body
  handling of a legacy operand** (reject vs ignore vs fall-through) is
  FLIX-desynced and NOT byte-pinned (MED). The *"legacy has no POOL body"*
  conclusion rests on the **`EXTISA_0`/`EXTISA_3` key-set census** + the **header
  contract** + the **env-gate lib census** + the **SEQ-side PE grouping** — NOT on
  tracing the desynced body. [HIGH the census/anchors; MED the in-body trace]
* **The PE engine is out of corpus.** The actual legacy LUT load + its in-MAC
  application live in the PE engine's firmware/RTL, which this GPSIMD (POOL Q7)
  corpus does not contain. §3/§5/§6.1's PE-side microarchitecture (the per-row
  broadcast, the in-MAC tap, the converter-table SRAM geometry) is **INFERRED**
  from the header contract + the SEQ grouping; only *"no POOL body"* is OBSERVED
  HIGH. [MED/INFERRED]
* **MAVERICK / SUNDA carries.** MAVERICK bodies are stripped `ET_DYN`
  (interiors header-OBSERVED → INFERRED per the [CPTC codec](cptc-codec.md));
  SUNDA's EXTISA container is out of corpus (no `EXTISA_3`, and no `s2_convlut.h`
  header — its `s2_*` family is `s2_bn`/`s2_bnpl2` only). Those rows rest on
  `kernel_info` bytes / header census, not fresh body disasm. [CARRIED]

---

## Cross-references

* [The CPTC codec](cptc-codec.md) — the **other half** of `0xe4`: the
  `lut_dtype ∈ {0x19..0x1F}` CPTC-decode leg, `pool_conv_lut_load` →
  `cptc_decode_impl<1..6>`, the 6-arm ladder, and the env-gate. This page and
  that one share the `s2_convlut_is_cptc` predicate and the 64 B struct
  byte-for-byte.
* [Activation / Transcendental Tables](../../uarch/activation-transcendental-tables.md)
  — the ACT-engine piecewise-polynomial PWL machine and its host-loaded PROFILE
  table; **not** the ConvLutLoad LUT (different engine, table, and load path —
  §3.1).
* [TensorDequantize](tensor-dequantize.md) — the POOL NF4 16-entry dequant
  codebook (`proc_4bit`), the closest look-alike to the legacy LUT (both 4-bit,
  both 16-entry) but a distinct object on a distinct engine (§3.1).
* [The Unified Datatype Model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` enum
  (`Invalid=0x0`, `FP32=0xA`, `UINT16=0x5`, `UINT32=0x9`, `CPTC1..7=0x19..0x1F`)
  and the per-gen CPTC presence the dtype matrix here mirrors.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tagging used throughout.
* [FLIX decoding](../../reference/flix-decoding.md) — the bundle-decode
  methodology behind the dispatcher prologue read and the desync flags (§7).
