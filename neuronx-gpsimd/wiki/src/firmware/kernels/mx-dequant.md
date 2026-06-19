# MX (Microscaling) Dequant Compute Paths

This page is the **compute-path deep-dive** of POOL opcode `0x7b`
[`TensorDequantize`](tensor-dequantize.md). Where the parent page covers the
dispatch, the operand struct, and the format selector, **this page decodes the
three `proc_*` bodies the firmware actually runs** — the arithmetic that turns a
microscaled low-bit block (4-bit `FP4_E2M1` / `INT4` / `NF4`, 6-bit `FP6_E2M3`)
into FP8 on the Vision-Q7 *Cairo* `ncore2gp` IVP datapath. There is **one
opcode** and **three named compute paths**, self-disclosed by the firmware's own
DEBUG build:

| firmware DEBUG name | format string | `(fmt, group_size)` | role |
|---|---|---|---|
| `proc_4bit_mx_8`   | `P%i: proc_4bit_mx_8: num_elem=%d, num_groups=%d`   | 4-bit, `grp==8` | **the MX block path** (per-block scale) |
| `proc_4bit_non_mx` | `P%i: proc_4bit_non_mx: num_elems=%d`                | 4-bit, `grp==0` | 4-bit scale-free widen |
| `proc_6bit_non_mx` | `P%i: proc_6bit_non_mx: num_elem=%d, num_groups=%d`  | 6-bit, `grp==0` | FP6/E2M3 scale-free widen |

Everything below is re-grounded **this pass** against the shipped binary:
`libnrtucode_internal.so` (`sha256 b7c67e89…`, the customop-lib host blob that
carries all 16 per-arch `Q7_POOL` device images as `.rodata` getter blobs) and
its carved `CAYMAN_Q7_POOL` EXTISA_0 image (`sha256 910d41c3…`,
`.text` VMA `0x01000000` == file off `0x100`), read with the shipped Cadence
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); the operand contract from the
in-package `aws_neuron_isa_tpb_s3d3_tens_dequant.h` / `…common.h` /
`…s3dmx1_quant.h` arch-isa headers; and the firmware's own `"P%i: …"`
self-naming DEBUG strings. Confidence per [the Confidence & Walls
model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` = read-from-byte,
`[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` = re-used at a sibling
report's confidence. The `extracted/` and `ida/` trees are gitignored — reach
them with `fd --no-ignore` or an absolute path.

> **NOTE — two distinct "MX" mechanisms, do not conflate.** This `0x7b`
> `TensorDequantize` is the **NC-v3 dequant-side MX**: a block of **8** elements,
> an **in-band** per-block scale, applied as a **vector multiply**. It is *not*
> the NC-v5 `MXTensorV2` family ([`QuantizeMX 0xe3`](../../compiler/mx-device-bodies.md),
> [`MatmulMX 0x0A`/`LdweightsMX 0x09`](pe-matmul.md)) which uses an **out-of-band**
> `scale_addr` tensor and an explicit **E8M0** (`SFP8_E8`) shared-exponent scale.
> Both are "microscaling"; their block/scale plumbing is different. §6 reconciles
> them. `[HIGH/OBSERVED both contracts — §6.]`

---

## 0. TL;DR — the model in seven facts

1. **One opcode, three paths.** `0x7b TensorDequantize` dispatches (byte-exact)
   to a trampoline `@0x01004dc4` → `decode_tensor_dequantize @0x01004df0`, which
   either calls the **standalone** `proc_4bit_mx_8 @0x0100511c` (MX) or runs one
   of two **inline** non-MX arms. `[HIGH/OBSERVED — §2.]`
2. **The MX block is 8, not 32.** `group_size` is literally `8`; the OCP-MX
   block-of-32 is **not** what this op uses. The scale rides **in-band** within
   each group of 8 source codes — `s3d3_tens_dequant` has **no `scale_addr`
   field**. `[HIGH/OBSERVED — struct §3 + the `grp==8`/`8:5` rule §3.3.]`
3. **The scale is applied as a multiply.** `proc_4bit_mx_8` reconstructs each
   nibble to FP16/FP32, then **multiplies** by the broadcast per-block scale via
   the IVP MAC ops (`ivp_mulus4tan16xr16` / `ivp_dmulqa2n8xr8`, packed-register
   scale `pr<N>`). Not a `2^exp` add on the dequant side. `[HIGH that a multiply
   happens / MED exact register routing — §5, §10.]`
4. **`4-bit` vs `6-bit` is width + pack-density + scale-presence, not a
   different scale algebra.** 4-bit nibble-packs 2/byte (ratio `2:1` non-MX,
   `8:5` MX); 6-bit FP6 bit-packs 4 codes per 3 bytes (ratio `4:3`, `grp==0`
   only — the ISA **forbids** `grp==8` for FP6). Only the 4-bit MX path applies a
   scale. `[HIGH/OBSERVED — §4, §7.]`
5. **Output is FP8.** `E5M2` for `FP4`/`INT4`, `E4M3` for `NF4`/`FP6` (the
   `dequant_fmt` table). Intermediate compute is soft-float FP16/FP32 (no HW FP);
   the narrow-to-FP8 is a saturating clamp (`ivp_bmin*`/`ivp_bmax*`) after a
   48-bit accumulator extract (`ivp_cvtg48n_2x32l`). Rounding RNE (implicit).
   `[HIGH output FP8 / MED implicit RNE — §5.]`
6. **`NF4` is a 16-entry table lookup, not a bitfield re-bias.** The `const16`
   constants at the proc head (`0x43a8 0x22d5 0xc465 0x1841 0xc558 0xf228 0x6506
   0xe3a1`, OBSERVED in the body) are the packed NormalFloat-4 codebook /
   re-encode constants. `[HIGH that NF4 is a lookup / MED the exact table — §4.]`
7. **Per-gen: NC-v3+.** Present (byte-verified `kernel_info` entry) on
   CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK; **absent** on SUNDA (NC-v2 — the
   `has_valid_nc_tens_dequant: nc >= V3` gate, and SUNDA ships no
   `s3d3_tens_dequant` header and no `0x7b` POOL kernel). `[HIGH/OBSERVED — §8.]`

---

## 1. Where these bodies live, and how they self-name

The compute bodies are **not** byte-recoverable from the host x86 lib directly —
they are Xtensa device code carved out of the `Q7_POOL` EXTISA blobs the host lib
holds as `.rodata`. The single most useful artifact for *naming* the paths is the
firmware's **own DEBUG build**, which prints each function's name. Those format
strings ship verbatim in the host lib (`strings -a -t x`, this pass):

```
 2696b4  P%i: TensorDequantize : num_chans = %0d          <- per-core entry log
 2696dd  P%i: TensorDequantize: decode                    <- decode-stage trace
 2696fc  P%i: Error: Unimplemented dequant format(0x%x)   <- the reject/default arm
 26972c  P%i: proc_6bit_non_mx: num_elem=%d, num_groups=%d <- FP6/E2M3 path
 26975f  P%i: proc_4bit_non_mx: num_elems=%d               <- 4-bit no-scale path
 269784  P%i: proc_4bit_mx_8: num_elem=%d, num_groups=%d   <- 4-bit MX block path
```

`[HIGH/OBSERVED — byte offsets read this pass; the same six strings repeat in the
MARIANA / MARIANA_PLUS / MAVERICK `Q7_POOL` DEBUG blocks (four identical per-arch
blobs in the one host lib).]`

The format strings are themselves a compute-path **contract**:

- `proc_4bit_mx_8` and `proc_6bit_non_mx` print **`num_groups`** ⇒ they iterate
  *blocks* (groups), not raw elements. The `_8` suffix is the literal
  `group_size == 8`.
- `proc_4bit_non_mx` prints **only `num_elems`** (note the plural form differs) ⇒
  the simplest 2:1 widen, no grouping, no scale.

> **GOTCHA — `proc_6bit_non_mx` / `proc_4bit_non_mx` have NO standalone symbol.**
> Only `proc_4bit_mx_8` is emitted as a separate `.xt.prop` function (the
> demangled symbol `_ZN16TensorDequantize14proc_4bit_mx_8Ej` is in the lib at
> `0x2f9482`, and its prop func-start VMA is `0x0100511c`). The two non-MX paths
> are compiled **inline** inside `decode_tensor_dequantize` — a full demangled
> `.xt.prop` sweep across CAYMAN/MARIANA/MARIANA_PLUS EXTISA_0..3 finds **exactly
> two** dequant prop symbols (`decode_tensor_dequantize(bool)` and
> `TensorDequantize::proc_4bit_mx_8(unsigned int)`). The DEBUG strings prove the
> inline source functions are real and named; their exact inline boundaries
> within `decode` are not separately pinned (no func-start record).
> `[HIGH/OBSERVED — string + prop sweep.]`

> **CORRECTION (resolves the SX-FW-63 "no `proc_6bit` exists" finding).** An
> earlier pass searched only `.xt.prop` symbols and concluded `proc_6bit` did not
> exist. The firmware DEBUG self-naming (`@0x26972c`) **proves it does** — it is a
> real, named source function, merely inlined into `decode`. The three-arm model
> `{4bit_mx(grp8), 4bit_non_mx(grp0), 6bit_non_mx(grp0)}` is the correct one.
> `[HIGH/OBSERVED — supersedes the prop-only census.]`

---

## 2. Dispatch — byte-exact, `kernel_info` → trampoline → bodies

The POOL kernel-dispatch table is a flat array of 8-byte records
`{ u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA }` (the FW-18 record format). On
the carved `CAYMAN_Q7_POOL` EXTISA_0 the `TensorDequantize` record is the **last
real entry**, immediately followed by the `.data` terminator word `34cb9960`
(OBSERVED this pass):

```
$ xxd -s 0x7480 -l 16 CAYMAN_0.so
00007480: 0000 007b c44d 0001 34cb 9960 0000 0000
          └idx16┘└op┘ └funcVA 0x01004dc4 LE┘ └terminator┘
```

`[HIGH/OBSERVED bytes — op `0x7b`=123, spec 0, funcVA `0x01004dc4`.]` The
trampoline at `0x01004dc4` is clean-decoding (binary-mode, OBSERVED): `entry a1,32`;
`const16 a2,0x0200 ; const16 a2,0x0480` (= `0x02000480`, the per-kernel dequant
state slot in the `.data`/`.bss` state band); a state-setup `call0`; then
`const16 a2,0x4df0 ; callx8 a2` — i.e. it tail-calls
`decode_tensor_dequantize @0x01004df0`. Raw confirmation:

```
$ xxd -s 0x4ec4 -l 48 CAYMAN_0.so   # file off = 0x01004dc4 - 0x01000000 + 0x100
00004ec4: 3641 0024 0002 2480 048f 3290 0b24 0000   entry; const16 a2,0x200/0x480; ...
00004ee4: 24f0 4de0 ...                              const16 a2,0x4df0 ; callx8
                └ 0x4df0 LE ┘
```

`decode_tensor_dequantize` then reaches the MX path by a **direct intra-kernel
call**, byte-pinned by a `const16 a2,0x511c` literal preceding a `callx8`
(`0x0100511c` == `proc_4bit_mx_8`'s func-start VMA). The two non-MX arms are
inline branches within `decode`. The function extents (from the first `.xt.prop`
record's LE VMA, OBSERVED):

| function | func-start VMA | extent |
|---|---|---|
| `decode_tensor_dequantize(bool)`           | `0x01004df0` | `0x01004df0 .. 0x0100511c` (~812 B; holds the two inline non-MX paths) |
| `TensorDequantize::proc_4bit_mx_8(unsigned int)` | `0x0100511c` | `0x0100511c .. ~0x010055e6` (~1226 B) |

`[HIGH/OBSERVED func-starts; HIGH the `const16==funcVA` pin; MED the exact branch
arms — FLIX desync §10.]`

---

## 3. The operand struct — `NEURON_ISA_TPB_S3D3_TENS_DEQUANT_STRUCT` (64 B)

Read byte-for-byte from `aws_neuron_isa_tpb_s3d3_tens_dequant.h` (cayman;
`ISA_STATIC_ASSERT(sizeof == 64)`). This is the **same struct the parent
[`TensorDequantize`](tensor-dequantize.md) page establishes** — reproduced here
because the compute paths key directly off `dequant_fmt`, `group_size`, and the
element-count ratios. **There is no `src1`/scale tensor, no zero-point, no
`scale_addr`** — one source, one destination.

| off | sz | field | type | notes |
|---|---|---|---|---|
| 0–3   | 4  | `header`              | `NEURON_ISA_TPB_HEADER`   | `{ opcode=0x7b, inst_word_len, debug }` |
| 4–11  | 8  | `events`              | `NEURON_ISA_TPB_EVENTS`   | wait/update sync |
| 12–27 | 16 | `src_mem_pattern`     | `TENSOR3D` (mav: `MEM_PATTERN3D`) | **input** (packed quantized), read-only, SBUF |
| 28–31 | 4  | `reserved0[4]`        | —                         | `== 0` |
| 32    | 1  | `in_dtype`            | `NEURON_ISA_TPB_DTYPE`    | **`== UINT32`** (transport container) |
| 33    | 1  | `out_dtype`           | `NEURON_ISA_TPB_DTYPE`    | **`== UINT32`** (transport container) |
| 34    | 1  | `num_active_channels` | `uint8_t`                 | `1..128` (`POOLING_NUM_CHANNELS=128`) |
| 35    | 1  | `dequant_fmt`         | `NEURON_ISA_TPB_DEQUANT_FMT` | **the format selector** (§4) |
| 36    | 1  | `group_size`          | `uint8_t`                 | `0` (non-MX) or `8` (MX block of 8) |
| 37–43 | 7  | `reserved1[7]`        | —                         | `== 0` |
| 44–59 | 16 | `dst_mem_pattern`     | `TENSOR3D` (mav: `MEM_PATTERN3D`) | **output** (FP8), written, SBUF |
| 60–63 | 4  | `reserved2[4]`        | —                         | `== 0` |

`[HIGH/OBSERVED layout — header struct declaration read this pass; the validity
fns are comment-only so the layout is OBSERVED, not compile-asserted beyond the
`==64` static assert.]`

> **QUIRK — `in_dtype == out_dtype == UINT32`, always.** The dtype fields do
> **not** carry the logical micro-format. They are a fixed `UINT32` transport
> container; the real format lives in `dequant_fmt`. So the dequant op never
> consumes the sub-byte dtype codes (`FP4_EXP2=0x10`, `INT4=0x12`) through the
> `dtype` field — it consumes them through `dequant_fmt` + the `UINT32`
> transport. This is exactly why the [dtype model](dtype-model.md) marks
> `FP4`/`INT4` as **transport-only** dtypes. `[HIGH/OBSERVED — header validity
> `has_valid_tens_dequant_in_dtype: d == UINT32`.]`

### 3.1 Validity contract (`is_valid_tensor_dequantize`, verbatim)

- `has_valid_nc_tens_dequant`: `nc >= NeuronCoreVersion::V3` — **excludes SUNDA**.
- `check_active_channels`: `num_active_channels != 0 && <= 128`.
- `has_valid_dequant_fmt`: `fmt ∈ {1,2,3,4}` (`Invalid=0` rejected — the
  `"Unimplemented dequant format"` DEBUG arm `@0x2696fc`).
- `has_valid_group_size`: `group_size == 0 || group_size == 8`.
- `s3d3_tens_dequant_reserved_zero`: all 15 reserved bytes `== 0`.
- `in_dtype == UINT32 && out_dtype == UINT32`; src read-only, dst write,
  **SBUF-only** (`AllowedInPSUM::False`), partition-aligned.

### 3.2 The 4-bit-vs-6-bit legality rule (`has_valid_dequant_fmt_group_size`)

This single predicate is the entire `grp8`/`grp0` split, verbatim from the header:

```c
// fn has_valid_dequant_fmt_group_size(fmt, grp) -> bool {
//       (grp == 0)
//    || (grp == 8 && fmt == DequantFmt::E2M1ToE5M2)
//    || (grp == 8 && fmt == DequantFmt::INT4ToE5M2)
//    || (grp == 8 && fmt == DequantFmt::NF4ToE4M3)
// }
```

`[HIGH/OBSERVED.]` ⇒ **`group_size==8` is legal only for the three 4-bit formats.
`E2M3ToE4M3` (FP6) has no `grp==8` form** — `proc_6bit` is **always** non-MX.

### 3.3 Element-count ratios (`s3d3_tens_dequant_element_count_check_t3d`)

The header pins the exact `src:dst` element ratio per `(fmt, group_size)` — this
is what each `proc_*` body's loop reciprocal realises:

| fmt | group | `src:dst` ratio | `src_count %` | → handler |
|---|:-:|:-:|:-:|---|
| `E2M1`/`INT4`/`NF4` | 0 | `2 : 1` | `% 1 == 0` | `proc_4bit_non_mx` |
| `E2M3` (FP6)        | 0 | `4 : 3` | `% 3 == 0` | `proc_6bit_non_mx` |
| `E2M1`/`INT4`/`NF4` | 8 | `8 : 5` | `% 5 == 0` | `proc_4bit_mx_8` |

`[HIGH/OBSERVED — `ratio_element_count_t3d(src, dst, A, B)` + `is_num_elem_mult_n_t3d`
calls, verbatim from the header.]`

> **NOTE — the `8:5` ratio *is* the in-band scale being consumed.** For the MX
> block, 8 source units map to 5 destination FP8 outputs: of the 8 packed units,
> the shared block exponent/scale is extracted in-band and the remainder expand
> to 5 FP8. This is why the prologue carries an `n/5` reciprocal (§5.1) and why
> the DEBUG name prints `num_groups`. `[HIGH ratio / MED which of the 8 packed
> units is the scale byte — desync §10.]`

---

## 4. The format selector — `NEURON_ISA_TPB_DEQUANT_FMT`

`common.h:969` (maverick; **byte-identical** in cayman `:806` and mariana `:899`
— gen-stable):

```c
typedef enum NEURON_ISA_TPB_DEQUANT_FMT {
    NEURON_ISA_TPB_DEQUANT_FMT_INVALID     = 0,
    NEURON_ISA_TPB_DEQUANT_FMT_E2M1TO_E5M2 = 1,    // FP4_E2M1 to FP8_E5M2
    NEURON_ISA_TPB_DEQUANT_FMT_INT4TO_E5M2 = 2,    // INT4 to FP8_E5M2
    NEURON_ISA_TPB_DEQUANT_FMT_NF4TO_E4M3  = 3,    // NF4 to FP8_E4M3
    NEURON_ISA_TPB_DEQUANT_FMT_E2M3TO_E4M3 = 4,    // FP6_E2M3 to FP8_E4M3
} NEURON_ISA_PACKED NEURON_ISA_TPB_DEQUANT_FMT;
```

`[HIGH/OBSERVED — read byte-for-byte this pass.]`

| fmt | input micro-format | output FP8 | MX (`grp8`)? | ratio | per-format arithmetic |
|---|---|---|:-:|---|---|
| 0 | — (`INVALID`) | — | — | (rejected) | `"Unimplemented dequant format"` |
| 1 | `FP4_E2M1` (4-bit `1.2.1`) | `FP8_E5M2` | yes | `2:1`/`8:5` | exp rebias `1.2.1→1.5.2` + mantissa zero-extend |
| 2 | `INT4` (4-bit signed)     | `FP8_E5M2` | yes | `2:1`/`8:5` | sign-extend int4 → int→fp convert |
| 3 | `NF4` (4-bit codebook)    | `FP8_E4M3` | yes | `2:1`/`8:5` | **16-entry table lookup** → re-encode E4M3 |
| 4 | `FP6_E2M3` (6-bit `1.2.3`) | `FP8_E4M3` | **no** | `4:3` | exp rebias `1.2.3→1.4.3`, **mantissa passes through (3 bits)** |

`[HIGH/OBSERVED enum + ratios; HIGH conversion model / MED exact in-FLIX micro-op
order — §10.]`

The element micro-formats (bit fields, from the enum comments + the
[dtype model](dtype-model.md)):

- **`FP4_E2M1`** — 4-bit `1-2-1`, OCP FP4. Dtype code `FP4_EXP2 = 0x10`.
- **`INT4`** — 4-bit signed integer. Dtype code `INT4 = 0x12`.
- **`NF4`** — NormalFloat-4: a **non-uniform 16-entry codebook**, i.e. a *table
  index*, **not** a float bitfield. There is no `NF4` `NEURON_ISA_TPB_DTYPE` code
  — it exists only as a `dequant_fmt`. `[HIGH — enum comment.]`
- **`FP6_E2M3`** — 6-bit `1-2-3`. **E2M3, not E3M2** — confirmed by the enum
  name. There is no FP6 `NEURON_ISA_TPB_DTYPE` code either.
- **Outputs** — `FP8_E5M2` (= `FP8_EXP5 = 0x0F`) for FP4/INT4; `FP8_E4M3`
  (= `FP8_EXP4 = 0x0E`) for NF4 and FP6.

> **GOTCHA — there is NO `(q - zp) * scale` affine dequant here.** Op `0x7b` is a
> micro-format **expansion** (+ optional per-block MX multiply). The classic
> integer-affine dequant (zero-point subtract, scale multiply) lives in a
> **different engine** (the Activation engine), not this POOL kernel. The only
> multiply this op performs is the MX block-scale multiply on the `grp8` path.
> `[HIGH/OBSERVED — the struct has no zero-point/scale field; the rebias paths
> are scale-free.]`

> **QUIRK — the output FP8 is an ISA/ucode intermediate, not an `at::Tensor`
> dtype.** In `v0.21.2.0` there is no pre-FP8 `c10::ScalarType`, so the FP8
> produced here is an internal datapath value selected by `dequant_fmt`, not
> something marshallable across the host `at::Tensor` boundary. `[HIGH cross-link
> — dtype model §FP8.]`

---

## 5. The `proc_4bit_mx_8` datapath — recovered IVP

`ncore2gp` is a ConnX-BBE-class Q7 Vision vector core: 512-bit registers
(`nx16` = 32×`i16`, `2nx8` = 64×`i8`, `n_2x32` = 16×`i32`), no hardware FP — the
`ivp_*f16`/`ivp_*f32` ops are soft-float bodies (CARRIED from ISS-13). The IVP
vocabulary below was harvested per function by a merged-property code-mode decode
plus a 10-phase binary-mode scan; both agree.

### 5.1 The prologue — the `n/5` reciprocal

```
0100511c <proc_4bit_mx_8>:
  ... const16 a4,0xCCCD ; muluh a3,a3,a4 ; srli a3,a3,2   <- a3 = num_elem / 5
```

The `0xCCCD` half is OBSERVED at file off `0x5228` (`cdcc` little-endian inside
the first FLIX bundle word `3340cccd`); it is the low half of the magic constant
`0xCCCCCCCD` whose `(x * 0xCCCCCCCD) >> (32+2)` computes `x/5`. This realises the
`8:5` block ratio (5 FP8 outputs per group of 8 source units). `[HIGH the `/5`
idiom + the `0xCCCD` byte / MED the exact upper half — it sits in a FLIX-desync
span, §10.]`

### 5.2 The IVP intrinsic set (phase-stable)

| stage | IVP intrinsics | what it does |
|---|---|---|
| **4-bit unpack** | `ivp_sel2nx8i_s4`, `ivp_l2a4nx8_ip`, `ivp_dselnx16` | `s4` = 4-bit select/unpack; `l2a4nx8_ip` = the **group-of-8 packed load** with post-increment; lane select |
| **MX scale ×** | `ivp_mulus4tan16xr16`, `ivp_mul4tan16xr16`, `ivp_dmulqa2n8xr8`, `ivp_muln_2x32` | unsigned×signed widening MAC by the **packed-register per-block scale** (`pr<N>`) — the unpacked element × the broadcast block scale |
| **accumulator extract** | `ivp_cvtg48n_2x32l` | 48-bit MAC accumulator → 32-bit lane extract (the convert-out / narrow step) |
| **saturate / clamp** | `ivp_bminnx16`, `ivp_bminn_2x32`, `ivp_bmin2nx8`, `ivp_bmaxn_2x32`, `ivp_bmaxun_2x32` | saturating min/max → **clamp** to the FP8 `E5M2`/`E4M3` representable range |
| **fp gate** | `ivp_ultn_2xf32t` | fp32 unordered compare — overflow/NaN gating |

`[HIGH that these ops are emitted in the body / MED exact register routing — §10.]`

### 5.3 The clean FLIX bundles (byte-exact, merged-prop)

A handful of FLIX bundles resync cleanly; these are reported byte-exact. The
**single bundle** that captures the whole MX inner loop is `@0x010051a9`:

```
@0x010051a9:
  { bbci.w15 a5,0,… ; addi.a a10,a1,20 ;
    ivp_mulus4tan16xr16 wv2,v26,v1,pr1 ;   <- MX SCALE-MULTIPLY (pr1 = packed block scale)
    ivp_bmin2nx8        vb3,v4,v20,v16 ;    <- SATURATE
    ivp_sel2nx8i_s4     v26,v3,v1,9 }       <- 4-bit UNPACK
```

i.e. **one FLIX issue = UNPACK(`s4`) → SCALE-MULTIPLY(by `pr1`) → SATURATE**,
executed in parallel. Other clean bundles:

- `@0x010051e1` `{ ivp_cvtg48n_2x32l wv0,v29,v22 ; ivp_bminnx16 ; ivp_bminn_2x32 }`
  — accumulator-extract + clamp.
- `@0x010052ee` `{ ivp_muln_2x32 ; ivp_ultn_2xf32t vb1,v13,v16,vb9 ;
  ivp_sel2nx8i_s4 v21,v17,v16,0 }` — mul + fp32-compare + unpack.
- `@0x010053c0` `{ ivp_dmulqa2n8xr8 wv3,wv1,v25,v9,v13,v29,pr0 ; ivp_dselnx16 v2,… }`
  — dual quad-8b MAC with packed scale `pr0` + lane select.

`[HIGH/OBSERVED — these four bundles decode clean under the merged property
table; the desynced spans between them are NOT reported as real instructions.]`

### 5.4 The codebook / re-bias constants

The proc head loads eight `const16` immediates that are the packed `NF4` codebook
+ re-encode constants (the path that maps a 4-bit code → FP8). All eight are
OBSERVED in the proc body (`0x0100511c..~0x010055e6`):

```
0x43a8  0x22d5  0xc465  0x1841  0xc558  0xf228  0x6506  0xe3a1
```

`[HIGH that these `const16` constants are present in the body (each verified by a
little-endian byte search of the carved proc extent) / MED the exact code→FP8
mapping table — desync prevents pinning the per-code routing.]`

### 5.5 `proc_4bit_mx_8` as annotated C pseudocode

```c
/*
 * TensorDequantize::proc_4bit_mx_8(unsigned int num_elem)   @ VMA 0x0100511c
 *
 * The 4-bit MX (microscaling) block path: group_size == 8. Reconstructs each
 * 4-bit code (FP4_E2M1 / INT4 / NF4) to soft-float, multiplies by the per-block
 * in-band scale, saturates to the target FP8 range, packs to FP8. src:dst = 8:5.
 *
 * dst FP8 form: E5M2 for FP4/INT4 (fmt 1/2), E4M3 for NF4 (fmt 3).
 * num_active_channels lanes run in parallel across the 128-channel POOL.
 *
 * Reconstructed from: prologue (0xCCCD /5), clean FLIX bundles @0x10051a9 /
 * @0x10051e1 / @0x10052ee / @0x10053c0, IVP vocabulary, the ISA 8:5 ratio.
 * Confidence: HIGH structure / MED exact micro-op order (FLIX-literal desync).
 */
static void proc_4bit_mx_8(unsigned int num_elem)
{
    const unsigned int num_groups = num_elem / 5;      /* 0xCCCD muluh>>2: the 8:5 block ratio */

    for (unsigned int g = 0; g < num_groups; ++g) {
        /* GROUP-OF-8 packed load: 8 nibble codes + the in-band block scale.
         * ivp_l2a4nx8_ip streams 4-bit lanes with post-increment. */
        ivp_int2nx8 packed = ivp_l2a4nx8_ip(&src);     /* advances src */

        /* The shared per-block scale rides in-band within the group of 8; it is
         * extracted and broadcast into a packed scale register pr<N>. The exact
         * byte position is not pinned from the binary (desync §10). */
        ivp_pr   scale = extract_block_scale(packed);  /* -> pr0/pr1 */

        /* 4-bit UNPACK: ivp_sel2nx8i_s4 selects the low/high nibble of each byte
         * into a 16-bit lane (sign-handled per fmt via dsel/neg). For NF4 the
         * nibble indexes the 16-entry codebook (the const16 table @ proc head). */
        ivp_nx16 elem = ivp_sel2nx8i_s4(packed, /*sel imm*/ 9);   /* fmt-specific */
        if (fmt == NF4ToE4M3)
            elem = nf4_codebook_lookup(elem);          /* 16-way table via dsel/sel map */
        else                                            /* E2M1 / INT4 */
            elem = bitfield_widen_or_sext(elem, fmt);  /* exp-rebias or int->fp */

        /* MX SCALE-MULTIPLY: unpacked element x broadcast block scale.
         * Unsigned x signed widening MAC; the 48-bit accumulator absorbs it. */
        ivp_wv acc = ivp_mulus4tan16xr16(elem, scale); /* @0x10051a9 */
        /* (the dual-MAC variant ivp_dmulqa2n8xr8(...,pr0) @0x10053c0 handles
         *  two quad-8b sub-blocks per issue) */

        /* ACCUMULATOR EXTRACT: 48b -> 32b lane (the convert-out). */
        ivp_n_2x32 wide = ivp_cvtg48n_2x32l(acc);      /* @0x10051e1 */

        /* fp gate: fp32 unordered compare for overflow/NaN. */
        ivp_vbool nanmask = ivp_ultn_2xf32t(wide, FP8_MAX);  /* @0x10052ee */

        /* SATURATE / CLAMP to the FP8 E5M2/E4M3 representable range, then pack. */
        ivp_2nx8 fp8 = ivp_bmin2nx8(clamp_lo, wide, clamp_hi);  /* @0x10051a9 */
        fp8 = ivp_bmaxun_2x32(fp8, FP8_MIN);

        store_fp8(&dst, fp8, /*5 outputs this group*/);  /* RNE implicit on cvtg */
    }
}
```

`[HIGH the loop shape, the unpack→scale-mul→saturate→pack chain, and the named
IVP ops at the cited bundle addresses; MED the per-fmt arm order and the exact
`pr` routing — §10.]`

---

## 6. The two "MX" mechanisms — in-band block-of-8 vs out-of-band E8M0

### 6.1 Dequant-side MX (this op, `proc_4bit_mx_8`, NC-v3+)

- **Block size 8** — the `group_size` field is literally `8`. The OCP-MX block of
  **32 is not used**.
- **Scale is in-band** — `s3d3_tens_dequant` has no `scale_addr`; the per-block
  scale rides within each group of 8 source codes (the `8:5` ratio consumes it).
- **Applied as a MULTIPLY** — the unpacked element × broadcast block scale via the
  packed-register (`pr<N>`) MAC ops (§5).

### 6.2 NC-v5 `MXTensorV2` + `QuantizeMX` (maverick, the *forward* data→MX)

This is the place where the OCP-MX **E8M0** shared-exponent scale is **explicit**.
`NEURON_ISA_TPB_MXTENSOR_V2` (`common.h:884`, read byte-for-byte this pass):

```c
typedef struct NEURON_ISA_TPB_MXTENSOR_V2 {
    NEURON_ISA_TPB_ADDR4 data_addr;          // Data tensor base address
    NEURON_ISA_TPB_ADDR4 scale_addr;         // Scale tensor base address  <-- OUT-OF-BAND
    uint8_t              num_elem[2];         // tile dims
    int16_t              step_elem_data_1;    // Y-stride for data  (mult of 4B)
    int16_t              step_elem_scale_1;   // Y-stride for scales (mult of 4B)
    uint8_t              p_f_dim;             // Packed F (hi nibble) + P (lo nibble) as 2's exponents
    NEURON_ISA_TPB_DTYPE scale_dtype;         // Scale datatype (0 = no scales)
} NEURON_ISA_TPB_MXTENSOR_V2;
```

`[HIGH/OBSERVED.]` The `QuantizeMX` header comment (verbatim): *"finds scale
exponents for groups of lanes/partitions, scales all elements by the reciprocal
of their scale, quantizes to FP8/FP4, and outputs both quantized/scaled data and
scales."* Its struct `NEURON_ISA_TPB_S3DMX1_QUANT_STRUCT` (64 B, read this pass)
binds to opcode `QUANTIZE_MX = 0xe3` (`instruction_mapping.json`), is **NC-v5
only** (`s3dmx1_quant_valid_nc: nc == V5`), and requires
`num_active_channels ∈ [16,128], %16==0` (group-of-16-partitions).

Key facts, all OBSERVED from the maverick header:

- **The MX shared scale is E8M0.** `scale_dtype` is one of the **scale-only**
  codes: `SFP8_E8 = 0x13` (`// FP8_S0E8M0`, an E8M0 power-of-two exponent with no
  sign bit), `SFP8_E7 = 0x14`, `SFP8_E6 = 0x15`, `SFP8_E5 = 0x16` — or
  `FP8_EXP4`/`Invalid` (`is_valid_mxtensorv2` at `common.h:3277`). `[HIGH/OBSERVED.]`
- **The block dims are power-of-two.** `p_f_dim` packs F (upper nibble) and P
  (lower nibble) as 2's exponents; the validity gate allows `p_f_dim == 0x04 ||
  0x44` (`common.h:3275`). An OCP-MX block of 32 is `2^5`. `[HIGH/OBSERVED.]`
- **The MX element dtype** is `is_valid_mx_dtype` = `{ FP8_EXP2(0x11),
  FP8_EXP3(0x0D), FP8_EXP4(0x0E), FP8_EXP5(0x0F), FP4_EXP2(0x10), INT4(0x12) }`
  (`common.h:1712`) — matching the [dtype model](dtype-model.md). `[HIGH/OBSERVED.]`
- `QuantizeMX 0xe3` is one of the MARIANA-DVE-new opcodes (DVE engine);
  [`MatmulMX 0x0A`/`LdweightsMX 0x09`](pe-matmul.md) are the PE-side MX matmul.

### 6.3 The reconciliation

```
                       DEQUANT-side MX (op 0x7b)        MXTensorV2 (op 0xe3 / 0x09 / 0x0A)
  direction            MX -> FP8 (dequant)              data -> MX (quantize) / MX matmul
  block                8 elements (in-band)             power-of-two via p_f_dim (e.g. 2^5=32)
  scale location       IN-BAND (group of 8)             OUT-OF-BAND (scale_addr tensor)
  scale bit-format     not named (implicit)             E8M0 (SFP8_E8) explicit  [OBSERVED]
  scale applied as     vector MULTIPLY (pr<N> MAC)      reciprocal-multiply (forward quantize)
  arrives in gen       NC-v3 (CAYMAN)                   NC-v5-class (MARIANA+ DVE/PE)
  engine               POOL                             DVE / PE
```

> **NOTE — the dequant-side in-band scale's bit-format is NOT pinned.**
> `s3d3_tens_dequant` names no `scale_dtype`; the block-of-8 scale is in-band and
> its exact encoding (is it itself E8M0?) is not recoverable from the struct.
> Whether `proc_4bit_mx_8`'s in-band scale is E8M0 is **MED/INFERRED** (the MX
> family convention). The **E8M0 fact is HIGH/OBSERVED only for the separate
> NC-v5 `MXTensorV2`/`QuantizeMX` `scale_dtype` field**. The device applies the
> dequant-side scale as a multiply regardless of its exact bit-format.
> `[flagged honestly — §10.]`

> **QUIRK — the POOL dequant MX *pre-dates* the MARIANA MXTensorV2 wave.**
> `proc_4bit_mx_8` is already present on CAYMAN (NC-v3). The framing "MX is the
> MARIANA+ addition" refers to the **DVE/PE `MXTensorV2`** surface
> (`QuantizeMX`/`MatmulMX`/`LdweightsMX`), which *is* genuinely MARIANA+. Both
> coexist on maverick. `[HIGH reconciliation — §8.]`

---

## 7. The 4-bit-vs-6-bit difference — width + density + scale, not algebra

|  | `proc_4bit_mx_8` | `proc_4bit_non_mx` | `proc_6bit_non_mx` |
|---|---|---|---|
| element width | 4 bit | 4 bit | 6 bit |
| formats | E2M1 / INT4 / NF4 | E2M1 / INT4 / NF4 | E2M3 (FP6) only |
| output FP8 | E5M2 / E4M3 (NF4) | E5M2 / E4M3 (NF4) | E4M3 |
| `group_size` | 8 (MX) | 0 | 0 |
| per-block scale | **YES** (in-band, ×mul) | NO | NO |
| `src:dst` ratio | `8:5` | `2:1` | `4:3` |
| packing | nibble, 2/byte | nibble, 2/byte | 6-bit bit-packed, 4 codes / 3 bytes |
| loop reciprocal | `n/5` (`0xCCCD muluh>>2`) | `n/1` (none) | `n/3` (`0xAAAB`, inferred from the 4:3 ISA ratio) |
| standalone `.xt.prop` | **YES** (`@0x0100511c`) | NO (inline) | NO (inline) |
| unpack IVP | `ivp_sel2nx8i_s4` + `ivp_l2a4nx8_ip` | `ivp_sel2nx8i` | `ivp_sel2nx8i` (6-bit stride) |
| scale IVP | `ivp_mulus4tan16xr16` / `ivp_dmulqa2n8xr8`(pr) | rebias only (`ivp_muluupan16xr16`) | rebias only (`ivp_muluupan`/`ivp_mulsupn16xr16`) |
| DEBUG name fields | `num_elem`, `num_groups` | `num_elems` | `num_elem`, `num_groups` |

The difference is: **(1)** element width 4 vs 6 bit → different unpack
stride/select; **(2)** pack density (nibble vs 6-bit) → different ratio
(`2:1`/`8:5` vs `4:3`); **(3)** scale: only the 4-bit MX (`grp8`) path applies a
per-block scale-multiply; both non-MX paths are scale-free bitfield-rebias
widens. FP6 can **never** be MX (the ISA forbids `grp8` for `E2M3`), so 6-bit is
never scaled. **The scale algebra does not differ between paths beyond
present/absent — there is no second scale scheme.** `[HIGH/OBSERVED — ISA +
DEBUG + IVP.]`

### 7.1 The two inline non-MX bodies (in `decode_tensor_dequantize`)

The `decode` body holds both non-MX arms; the recovered IVP set is the
format-widen (rebias) chain — **no** per-block scale multiply:

| stage | IVP intrinsics |
|---|---|
| compressed strided **load** | `ivp_labvdcmprs2nx8_xp` |
| sub-byte **unpack** | `ivp_sel2nx8i`, `ivp_dselnx16t`, `ivp_dseln_2x32t` |
| **rebias multiply** | `ivp_muluupan16xr16` (unsigned widening MAC), `ivp_mulsupn16xr16` (signed) |
| **subtract / bias** | `ivp_babssub2nx8`, `ivp_babssubunx16` |
| fp **convert / gate** | `ivp_movpint16`, `ivp_negnxf16t`, `ivp_oltnxf16t` (fp16 ordered compare) |

Clean bundle `@0x01004fc8` (byte-exact):

```
@0x01004fc8:
  { bbci.w15 a2,30,… ; extui a0,a4,1,4 ;      <- extract a 4-bit field (fmt/group decode)
    ivp_muluupan16xr16 wv3,v0,v23,pr3 ;       <- REBIAS MULTIPLY (pr3 = rebias factor)
    ivp_sel2nx8i       v10,v26,v7,53 ;        <- UNPACK sub-byte codes
    ivp_babssub2nx8    vb9,v8,v11,v1 }         <- SUBTRACT (bias / zero adjust)
```

and `@0x01004dfc { ivp_labvdcmprs2nx8_xp vb12,… ; ivp_oltnxf16t vb2,… ; ivp_dselnx16t v0,… }`
(compressed strided load + fp16 compare + lane move). `[HIGH/OBSERVED bundles.]`

```c
/*
 * proc_6bit_non_mx / proc_4bit_non_mx (inline in decode_tensor_dequantize @0x01004df0)
 *
 * Scale-free micro-format WIDEN. Differs from the MX path only by: no scale mul,
 * different unpack stride (4:3 for FP6, 2:1 for 4-bit), different rebias consts.
 * Confidence: HIGH chain shape / MED per-path arm boundaries (FLIX desync §10).
 */
static void proc_Nbit_non_mx(unsigned int num_elem)   /* N in {4,6} */
{
    /* 6-bit: 4 codes per 3 src bytes (ratio 4:3, n/3); 4-bit: 2 codes/byte (2:1). */
    const unsigned int stride = (N == 6) ? 6 : 4;

    for (each tile in num_elem) {
        ivp_2nx8 packed = ivp_labvdcmprs2nx8_xp(&src);      /* compressed strided load */
        ivp_nx16 elem   = ivp_sel2nx8i(packed, stride);     /* sub-byte unpack */
        elem = ivp_babssub2nx8(elem, bias);                 /* SUBTRACT bias / zero adjust */
        /* exponent re-bias (FP4 1.2.1->1.5.2 / FP6 1.2.3->1.4.3) or int->fp.
         * NF4 -> 16-entry table lookup. NO per-block scale here. */
        ivp_wv  acc = ivp_muluupan16xr16(elem, rebias_pr3);  /* widen/rebias, NOT a block scale */
        ivp_vbool g  = ivp_oltnxf16t(acc, FP8_MAX);          /* fp16 range/NaN gate */
        store_fp8(&dst, narrow_to_fp8(acc));                 /* clamp + RNE pack */
    }
}
```

`[HIGH the load→unpack→subtract→rebias→gate→pack chain and the named IVP ops at
`@0x01004fc8`/`@0x01004dfc`; MED which fmt takes which inline arm and the exact
`n/3` (`0xAAAB`) FP6 divisor — the constant sits in a desync span, §10.]`

---

## 8. Per-gen presence — reconciled with the cross-gen MX carves

**Two distinct MX surfaces with different per-gen arrival — do not conflate.**

### A) POOL `TensorDequantize` MX (this report) — NC-v3+

| gen | `kernel_info @0x7480` (or maverick `@0x70b8`) | funcVA | bodies |
|---|---|---|---|
| **CAYMAN** | `0000007b c44d0001` | `0x01004dc4` | `decode @0x01004df0` / `mx_8 @0x0100511c` |
| **MARIANA** | `0000007b 044e0001` | `0x01004e04` | `decode @0x01004e30` / `mx_8 @0x01005164` (+0x40/+0x48 build delta) |
| **MARIANA_PLUS** | `0000007b 044e0001` | `0x01004e04` | same as MARIANA |
| **MAVERICK** | `0000007b ec500000` | rel `0x000050ec` | ET_DYN, **stripped** (no `.xt.prop`/strings); op present, bodies not symbol-recoverable |
| **SUNDA** | — (no `0x7b` record) | — | **ABSENT** |

`[HIGH/OBSERVED — every `kernel_info` line read this pass with `xxd`.]` The three
`proc_*` DEBUG names appear in **all four** per-arch `Q7_POOL` DEBUG blocks
(including maverick — the host lib carries four identical blocks), so the maverick
path-names are recoverable from the DEBUG strings even though its EXTISA is
stripped.

> **GOTCHA — SUNDA declares the opcode but ships no kernel.** The SUNDA
> `common.h:211` *does* declare `OPCODE_TENSOR_DEQUANTIZE = 0x7b`, but **(a)**
> SUNDA ships **no** `aws_neuron_isa_tpb_s3d3_tens_dequant.h` (verified — no such
> file under `neuron_sunda_arch_isa/`), and **(b)** the SUNDA POOL device image
> carries no `0x7b` kernel — the `has_valid_nc_tens_dequant: nc >= V3` gate makes
> dequant an NC-v3+ feature and SUNDA is the pre-v3 (NC-v2) POOL set. The opcode
> number is reserved in the enum; the kernel is not built. `[HIGH/OBSERVED —
> header gate + missing struct header + the NC-v3 gate.]`

> **NOTE — the dequant contract did not change across NC-v3..v5.** The
> `DEQUANT_FMT` 5-value enum and the `s3d3_tens_dequant` struct are byte-identical
> cayman/mariana/maverick; maverick only renames `TENSOR3D → MEM_PATTERN3D` (a
> union widening that adds the `MXTensorV2`/indirect access patterns) and adds
> indirect-quadrant guards. `[HIGH/OBSERVED diff.]`

### B) The NC-v4/v5 `MXTensorV2` family (separate engines, MARIANA+)

These are **not** this POOL kernel — they are the genuine MARIANA+ MX additions
on the DVE and PE engines, documented at
[the compiler MX device bodies](../../compiler/mx-device-bodies.md) and
[the PE MX matmul](pe-matmul.md):

- **DVE `QuantizeMX 0xe3`** (`S3DMX1_QUANT`) — the forward data→MX, out-of-band
  E8M0 scale. NC-v5.
- **PE `MatmulMX 0x0A` + `LdweightsMX 0x09`** — MX matmul / weight-load. MARIANA+.

They use the `MXTensorV2` out-of-band-scale, E8M0, power-of-two-block mechanism
(§6.2), **not** the POOL in-band-block-of-8 path. `[HIGH cross-link — reconciled.]`

---

## 9. Reimplementation checklist

To rebuild a Vision-Q7-compatible `TensorDequantize` engine:

1. **Decode**: read the 64 B `s3d3_tens_dequant`; enforce
   `fmt ∈ {1,2,3,4}`, `group ∈ {0,8}`, the `fmt×group` legality (FP6 ⇒ `grp==0`),
   `in/out == UINT32`, all reserved `== 0`, src/dst SBUF-only.
2. **Branch** on `(fmt, group)`: `grp==8 && 4-bit fmt → proc_4bit_mx_8`;
   `grp==0 && 4-bit fmt → proc_4bit_non_mx`; `grp==0 && fmt==4 → proc_6bit_non_mx`.
3. **Element-count**: assert the `2:1` / `4:3` / `8:5` ratios and the
   `% {1,3,5}` source divisibility before issuing.
4. **MX path** (`proc_4bit_mx_8`): group-of-8 packed load (`l2a4nx8_ip`), extract
   the in-band block scale → `pr<N>`, `s4` nibble-unpack, NF4 codebook lookup or
   exp-rebias/sext, **multiply** by the block scale (`mulus4tan16xr16` /
   `dmulqa2n8xr8`), 48-bit accumulator extract (`cvtg48n_2x32l`), saturating clamp
   to the FP8 range (`bmin*`/`bmax*`), pack 5 FP8 per group, RNE rounding.
5. **Non-MX paths**: identical minus the scale multiply; rebias via
   `muluupan`/`mulsupn16xr16`, `babssub` bias adjust, fp16 gate, clamp, pack.
6. **Output FP8**: `E5M2` for FP4/INT4, `E4M3` for NF4/FP6 — but note FP8 is a
   ucode intermediate, not an `at::Tensor` dtype in this build.

---

## 10. Honest limitations / desync flags

- **FLIX-literal desync.** The bodies of `decode_tensor_dequantize` and
  `proc_4bit_mx_8` are hand-scheduled FLIX VLIW with interleaved literal/selector
  bytes that desync stock `xtensa-elf-objdump`'s linear sweep. Even with the
  merged property table only a handful of bundles resync cleanly (~3 in `decode`,
  ~4 in `proc_4bit_mx_8`); the rest sweep into spurious scalar/`.byte`. The clean
  bundles (§5.3, §7.1) are byte-exact; the desynced spans are **not** reported as
  real instructions. The 10-phase binary-mode scan recovers the IVP **vocabulary**
  robustly but **not** the exact bundle order in desynced spans.
- **Consequently MED, not byte-exact**: the per-format switch-arm sequence in
  `decode` (which inline arm a given `fmt`/`group` takes), the exact MX-scale `pr`
  register routing (`pr0`/`pr1`/`pr3`), the loop back-edges, the upper half of the
  `n/5` divisor, and the FP6 `n/3` (`0xAAAB`) reciprocal (the `4:3` ratio is HIGH
  from the ISA; the `/3` constant sits in a desync span). The dequant **model**
  (§3/§4/§6/§7) is HIGH/OBSERVED — a multi-source agreement (ISA header +
  `kernel_info` bytes + `.xt.prop` names + DEBUG self-naming + surviving bundles),
  not a single fragile decode.
- **The non-MX inline boundaries** within `decode` are not separately pinned (no
  func-start record); their **existence + names** are HIGH/OBSERVED from the DEBUG
  strings.
- **The dequant-side in-band scale bit-format** is not pinned (the struct names no
  `scale_dtype`); whether it is itself E8M0 is MED/INFERRED. E8M0 is HIGH/OBSERVED
  only for the **separate** NC-v5 `MXTensorV2`/`QuantizeMX` `scale_dtype`
  (`SFP8_E8=0x13`). §6.3.
- **The `s3d3_tens_dequant` struct** layout is the **header declaration**
  (OBSERVED) — the validity fns are comment-only, so it is not compile-asserted
  beyond the `ISA_STATIC_ASSERT(==64)`.
- **MAVERICK** device images are stripped (ET_DYN, no symtab/prop/strings) — the
  maverick proc bodies are not byte-recoverable; op `0x7b` presence is pinned by
  the `kernel_info` entry only.
- **The `NEURON_ISA_TPB_DTYPE` global dispatch-map** standardization is owned by
  the [dtype model](dtype-model.md); the codes used here (`FP4_EXP2=0x10`,
  `INT4=0x12`, `SFP8_E8=0x13`, `FP8_EXP4=0x0E`, `FP8_EXP5=0x0F`) are read locally
  from the maverick `common.h` enum. The dequant op constrains `in/out == UINT32`
  (transport), so it does **not** consume the sub-byte dtype codes through the
  `dtype` field — it consumes them through `dequant_fmt` + `UINT32` transport.

---

## See also

- [`TensorDequantize`](tensor-dequantize.md) — the parent op: full dispatch, the
  `s3d3_tens_dequant` struct, the format selector.
- [The unified datatype model](dtype-model.md) — `FP4_EXP2=0x10`, `INT4=0x12`,
  `FP8_EXP4/5`, the `SFP8_E8..E5` (E8M0) scale codes, the FP32 convert hub.
- [The compiler MX device bodies](../../compiler/mx-device-bodies.md) — the
  `QuantizeMX 0xe3` (`MXTensorV2`) forward path. *(planned)*
- [The PE matrix-multiply path](pe-matmul.md) — `MatmulMX 0x0A` /
  `LdweightsMX 0x09`, the PE-side MX matmul.
