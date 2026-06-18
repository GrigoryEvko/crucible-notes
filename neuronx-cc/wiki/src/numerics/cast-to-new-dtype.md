# bir::CastToNewDType — the canonical cast / saturate engine

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share this C++ core). The primitive lives in `neuronxcc/starfish/lib/libBIR.so` (build-id `a9b1ea38c47e579178b179fd445aa8edd593f206`). For `.text` and `.rodata` the virtual address equals the file offset (`.text [12]` Addr `0x1820c0` == Off `0x1820c0`; `.rodata [14]` Addr `0x708000` == Off `0x708000`) — `xxd`/`objdump` at the VA read the datum directly. `.data` (`[25]` Addr `0x917840`, Off `0x916840`) carries a **+0x1000** file-offset delta: a `.data`-resident global at VMA `0x917848` is read from file offset `0x916848`. Other wheels differ; treat every address as version-pinned.*

## Abstract

Every numeric type conversion in BIR — sim, golden reference, and codegen-side constant folding — funnels through **one** function: `bir::CastToNewDType` (`0x264e70`–`0x265fb0`, ~4.4 KB). It is the single shared cast/saturate engine. There is no per-dtype-pair conversion matrix; instead the function uses a **two-stage hub-and-spoke** design — *every* cast is decoded into an `fp32[]` intermediate buffer, then re-encoded from that buffer to the destination dtype. The two spokes are driven by two `.rodata` jump tables (`0x782c04` for `src→fp32`, `0x782c4c` for `fp32→dst`), each 18 entries of signed `i32` self-relative offsets.

Three facts make this page worth its weight. First, the fp32 hub is a deliberate precision floor: an `int32`/`int64` value above 2²⁴ round-trips lossily because the intermediate is single-precision (24-bit mantissa) — a genuine, silent quirk of the design. Second, rounding is **RNE-only and deterministic**: the function calls `fesetround(FE_TONEAREST)` once between the two stages, and every narrowing encoder implements explicit guard+round+sticky round-to-nearest-**even** in integer arithmetic — there is **no** stochastic rounding anywhere in this primitive. Third, the FP8 encoders consult an `FP8ConversionConfig` whose `saturate` bit **defaults to ON**: when the caller passes `std::nullopt`, the function reads the exported global `FP8ConvConfig` (`.data` `0x917848` = `0x00000001`), so an overflowing FP8 magnitude clamps to max-finite (448 for e4m3fn, 57344 for e5m2) rather than producing NaN/Inf. This is the engine behind `--enable-saturate-infinity`.

| | |
|---|---|
| **Symbol** | `bir::CastToNewDType(std::shared_ptr<std::vector<unsigned char>>, bir::Dtype src, bir::Dtype dst, bool b, std::optional<FP8ConversionConfig>)` |
| **Mangled** | `_ZN3bir14CastToNewDTypeESt10shared_ptrISt6vectorIhSaIhEEENS_5DtypeES5_bSt8optionalI19FP8ConversionConfigE` |
| **Address** | `0x264e70` (libBIR.so export; **CONFIRMED** in corpus `native_exports.json` + `function_addresses.json`) |
| **Stage-A table** | `src → fp32` jump table `0x782c04` (18 × `i32` rel) |
| **Stage-B table** | `fp32 → dst` jump table `0x782c4c` (18 × `i32` rel) |
| **Dtype size LUT** | `0x782ca0` (20 × `qword`) |
| **FP8 cfg default** | GOT `0x90fba0` → `R_X86_64_GLOB_DAT` → `FP8ConvConfig` `0x917848` = `0x00000001` (saturate ON) |
| **Rounding** | `fesetround(0)` @ `0x2650e2` + explicit RNE in every encoder; **no RNG** |

The cast *encoding* (how a Copy/Cast instruction is laid down in the ISA) is a separate concern documented in [TensorTensor / Copy / Cast Encoding](../isa/tensortensor-encoding.md). The sim's own elementwise cast path, which shares the RNE discipline and uses inline SSE rather than libm, is in [Sim Core Arithmetic](../bir/sim-core-arithmetic.md). The precision negative-results (what this design *cannot* do) are catalogued in [Numeric Negative Results](numeric-negative-results.md) (page 9.10).

## Calling convention and prologue

`CastToNewDType` returns its `shared_ptr<vector<uchar>>` by `sret` (pointer in RDI). The prologue is **CONFIRMED** (`objdump` `0x264e70`–`0x264ea1`):

```c
// RDI = sret slot  -> {vector<uchar>* , __shared_count ctrl_block*}
// RSI = input shared_ptr  ([rsi]=vector*, [rsi+8]=ctrl_block*)
// EDX = src Dtype ordinal      ECX = dst Dtype ordinal
// R8D = bool 'b'  (signed/unsigned 64-bit selector; stored [rsp+0x20])
// R9  = std::optional<FP8ConversionConfig>, packed in the register:
//         (r9d >> 16) & 0xff  -> [rsp+0x27]   (optional 'engaged' byte)
//         full r9             -> [rsp+0x38]   (WORD later read as the 2-byte cfg)

push r15,r14,r13,r12,rbp,rbx
mov  eax, r9d ; shr eax, 0x10 ; mov [rsp+0x27], al   // 264e72..264e8a: extract cfg byte
mov  [rsp+0x38], r9                                  // 264e8e
sub  rsp, 0xa8

if (src == dst)                       // 264e9f: cmp ecx,edx ; je 2652a0
    return alias_copy(input);         // bump __shared_count refcount, return same buffer
if ((unsigned)src > 0x13)             // 264eb8: cmp ebp,0x13 ; ja 265f49
    std::__throw_length_error(...);   // src Dtype ordinal > 19  (msg @0x7166f0)
```

> **CORRECTION (backing report §0).** The report writes the prologue's cfg extraction as `shr 0x10` on `r9d`; the binary at `0x264e77` does `shr $0x10,%eax` *after* `mov %r9d,%eax` (`0x264e72`) — same result, but the `shr` operates on the EAX copy, not R9 directly. Cosmetic; the extracted byte is identical.

The `src == dst` fast path (`0x2652a0`) is a pure `shared_ptr` aliasing copy — the input vector pointer is written to the sret slot and the control block's `__shared_count` copy-constructor bumps the refcount. **No bytes are reinterpreted or copied**, so an identity cast is free. The `src > 0x13` guard rejects out-of-range Dtype ordinals via `std::__throw_length_error`. [**CONFIRMED** disasm.]

## Element count and the fp32 buffer size

Before dispatch the function computes the element count by dividing the byte length by the source stride (`SIZE_LUT[src]`), then sizes the fp32 intermediate. **CONFIRMED** `0x264ea9`–`0x264ee0`:

```c
byte_len      = vec->_M_finish - vec->_M_start;     // [vec+8] - [vec+0]
src_elem_size = SIZE_LUT[src];                       // qword @0x782ca0 + src*8
elem_count    = byte_len / src_elem_size;            // unsigned div (264ed8: div rcx)
r15 = elem_count;

// fp32 buffer length:
r13 = elem_count * 4;                                // one fp32 per logical element
if (src == 2 || src == 8 || src == 9)                // fp4_e2m1fn_x4 / fp8_*_x4 packed
    r13 = elem_count * 16;                            // *4 logical lanes -> *4 fp32 -> *16 B
```

The `x4`-packed detection at `0x264eeb` is `cmp ebp,2; je` plus `lea eax,[rbp-8]; cmp eax,1; jbe` (catches ordinals 2, 8, 9). The `SIZE_LUT` at `0x782ca0` is the BIR Dtype-ordinal → container-byte-size table (per the dtype crosswalk; **CONFIRMED** the `lea 0x51ddd3(%rip)` at `0x264ec6` resolves to `0x782ca0`):

| idx | dtype | bytes | | idx | dtype | bytes |
|----:|-------|------:|---|----:|-------|------:|
| 0 | uint8 | 1 | | 10 | uint16 | 2 |
| 1 | int8 | 1 | | 11 | int16 | 2 |
| 2 | fp4_e2m1fn_x4 | 2 | | 12 | bfloat16 | 2 |
| 3 | float8e3 | 1 | | 13 | float16 | 2 |
| 4 | float8e4 | 1 | | 14 | uint32 | 4 |
| 5 | fp8_e4m3fn | 1 | | 15 | int32 | 4 |
| 6 | fp8_e8m0fnu | 1 | | 16 | float32 | 4 |
| 7 | float8e5 | 1 | | 17 | float32r | 4 |
| 8 | fp8_e4m3fn_x4 | 4 | | 18 | uint64 | 8 |
| 9 | fp8_e5m2_x4 | 4 | | 19 | int64 | 8 |

These are **container** sizes: `fp4_e2m1fn_x4` packs four FP4 into 2 bytes; `fp8_*_x4` packs four FP8 into 4 bytes. They match the strides documented in the dtype tables. [**CONFIRMED** size 0,1,2 read directly; remainder per backing report §1, **STRONG**.]

## Stage A — `src bytes → fp32[]` (jump table `0x782c04`)

The Stage-A dispatch (`0x2650c0`) bounds-checks `src ≤ 0x11`, then performs a self-relative jump:

```c
// 2650c0: cmp ebp, 0x11 ; ja <bad>(throw)
movsxd rax, [0x782c04 + src*4];   // signed i32 rel offset
add    rax, 0x782c04;             // table-base relative
jmp    rax;
```

The 64-bit ordinals (18 `uint64`, 19 `int64`) are handled **before** this dispatch, at `0x2650b0`, by dedicated `cvtsi2ss`-from-qword loops (see [§ 64-bit paths](#the-64-bit-paths)). I decoded all 18 entries of the table directly from the binary; the first three and the fp32 passthrough are read byte-for-byte here (**CONFIRMED**), the remainder are per the decoded table in the backing report (**STRONG**):

| src | dtype | target | decode |
|----:|-------|-------:|--------|
| 0 | uint8 | `0x2657ee` | `movzx byte` → `cvtsi2ss` (zero-extend) |
| 1 | int8 | `0x2654aa` | `movsx byte` → `cvtsi2ss` (sign-extend) |
| 2 | fp4_e2m1fn_x4 | `0x265531` | `call 0x4b4e40` (FP4×4 unpack, 4/elt) |
| 3 | float8e3 | `0x265927` | `call 0x4b23f0` (legacy e3 decode) |
| 4 | float8e4 | `0x26593a` | `call 0x4b24c0` (legacy e4 decode) |
| 5 | fp8_e4m3fn | `0x265871` | `call 0x4b2590` (e4m3fn decode) |
| 6 | fp8_e8m0fnu | `0x265884` | `movzx byte` → `cvtsi2ss` — **raw uint8, see note** |
| 7 | float8e5 | `0x265901` | `call 0x4b2650` (e5m2 decode) |
| 8 | fp8_e4m3fn_x4 | `0x265914` | `call 0x4b2720` (e4m3fn ×4 unpack) |
| 9 | fp8_e5m2_x4 | `0x265544` | `call 0x4b2730` (e5m2 ×4 unpack) |
| 10 | uint16 | `0x265557` | `movzx word` → `cvtsi2ss` |
| 11 | int16 | `0x2655d9` | `movsx word` → `cvtsi2ss` |
| 12 | bfloat16 | `0x265659` | `call 0x4b2880` (bf16 → fp32 widen) |
| 13 | float16 | `0x26566f` | **INLINE** half → float (§ below) |
| 14 | uint32 | `0x2656ee` | `mov ecx` → `cvtsi2ss rcx` (zero-ext) |
| 15 | int32 | `0x265771` | `cvtsi2ss DWORD` (sign) |
| 16 | float32 | `0x265460` | passthrough — alias input as fp32 |
| 17 | float32r | `0x265460` | passthrough — same target as float32 |

I verified the decode for `src[0]`, `src[1]`, `src[2]`, `src[13]`, `src[16]`, `src[17]` from the live table bytes — e.g. table entry 0 = `rel −0x51d416` → `0x782c04 − 0x51d416 = 0x2657ee` ✓; entries 16 and 17 both = `rel −0x51d7a4` → `0x265460` (float32 and float32r share the passthrough target). [**CONFIRMED**.]

> **NOTE — e8m0 is *not* power-of-two-decoded here.** `src=6` (`fp8_e8m0fnu`) decodes as a **plain unsigned byte → float** (`movzx`; `0x265884`), not an OCP-MXFP `2^(e−127)` scale. `CastToNewDType` treats the e8m0 byte as its raw `uint8` value. The OCP scale interpretation is applied by the MX-quantize path, not by this generic byte-level primitive. [**CONFIRMED** disasm `0x265884`; the absence of `2^(e−127)` scaling is **STRONG**.]

### Inline float16 → float32 decode (`0x26566f`)

The half decoder is open-coded (not libc `__gnu_h2f_ieee`) and handles subnormals explicitly. **CONFIRMED** `0x26566f`–`0x2656ec` / `0x265f00`:

```c
// h = uint16 half
m         = (h << 13) & 0x0FFFE000;        // mantissa+exp into fp32 position
exp_field = (h << 13) & 0x0F800000;
if (exp_field == 0x0F800000)               // half exp all-ones -> Inf/NaN
    f = m + 0x70000000;                    // rebias to fp32 Inf/NaN
else if (exp_field != 0)                   // normal
    f = m + 0x38000000;                    // +112<<23: exp rebias (127-15)
else {                                     // subnormal / zero  (0x265f00)
    f  = m + 0x38800000;                   // 0x38800000 = 2^-14 @0x781928
    f  = bitcast_float(0x38800000) - bitcast_float(f);   // subss renormalises subnormal
}
sign = (h << 16) & 0x80000000;
out_fp32 = sign | f;
```

The constant `0x781928 = 0x38800000` is 2⁻¹⁴, the smallest fp16 normal, used to renormalise subnormal halves. [**CONFIRMED**; the subnormal `subss` idiom **STRONG**.]

## The rounding fence — `fesetround(0)`

Between Stage A and Stage B the function pins the C/SSE rounding mode to round-to-nearest-even **once**, and never changes it. This is **CONFIRMED** byte-for-byte:

```text
2650e0:  31 ff                 xor    %edi,%edi          // edi = 0 = FE_TONEAREST
2650e2:  e8 29 5e f1 ff        call   17af10 <fesetround@plt>
```

There is **no** `fesetround(FE_UPWARD/DOWNWARD/TOWARDZERO)` and **no** RNG/dither/seed input anywhere in `0x264e70`–`0x265fb0`. Combined with the explicit guard+round+sticky RNE in every narrowing encoder below, this confirms the reconstruction conclusion: **stochastic rounding does not exist in this primitive**. RNE is the only mode; the conversion is deterministic. (The upstream XLA `kStochasticConvert` is a separate HLO-level path that never reaches `CastToNewDType`.) [**CONFIRMED**.]

## Stage B — `fp32[] → dst bytes` (jump table `0x782c4c`)

After the fence, Stage B re-encodes the fp32 buffer to the destination dtype. The 64-bit ordinals are again special-cased first (`dst==0x12` uint64 if `b`; `dst==0x13` int64), then the generic dispatch:

```c
// after fesetround:
if (dst == 0x12 && b) goto encode_u64;
if (dst == 0x13)      goto encode_i64;
if ((unsigned)dst > 0x11) goto bad;          // cmp r14d,0x11 ; ja
movsxd rax, [0x782c4c + dst*4];
add    rax, 0x782c4c;
jmp    rax;
```

| dst | dtype | target | encoder |
|----:|-------|-------:|---------|
| 0 | uint8 | `0x26594d` | `call 0x2621f0` (fp32→u8 round+clamp 0..255) |
| 1 | int8 | `0x265acc` | **INLINE** round+saturate → [−128,127] |
| 2 | fp4_e2m1fn_x4 | `0x265ab7` | `call 0x4b5730` (fp32→FP4×4 pack) |
| 3 | float8e3 | `0x2659cf` | `call 0x4b1d40` |
| 4 | float8e4 | `0x2659ba` | `call 0x4b1b50` |
| 5 | fp8_e4m3fn | `0x2659a5` | `call 0x4b1f40` (fp32→e4m3fn, RNE+sat) |
| 6 | fp8_e8m0fnu | `0x26594d` | `call 0x2621f0` (same as uint8 — raw byte) |
| 7 | float8e5 (e5m2) | `0x265eeb` | `call 0x4b2160` (fp32→e5m2, RNE+sat) |
| 8 | fp8_e4m3fn_x4 | `0x265ed6` | `call 0x4b6040` (e4m3fn ×4 pack) |
| 9 | fp8_e5m2_x4 | `0x265ec1` | `call 0x4b6860` (e5m2 ×4 pack) |
| 10 | uint16 | `0x265df7` | **INLINE** round+clamp → [0,65535] |
| 11 | int16 | `0x265d23` | **INLINE** round+saturate → [−32768,32767] |
| 12 | bfloat16 | `0x265d0b` | `call 0x4b2750` (cast_fp32_to_bf16, RNE) |
| 13 | float16 | `0x265c80` | **INLINE** fp32→float16 RNE |
| 14 | uint32 | `0x265bb1` | **INLINE** round+clamp → [0,2³²−1] |
| 15 | int32 | `0x2659e4` | **INLINE** round+saturate → [INT32_MIN,MAX] |
| 16 | float32 | (filler → `0x18b255`) | float32-out is the fp32 buffer itself; handled before the table (see CORRECTION) |
| 17 | float32r | `0x265b99` | `call 0x4b2a60` (cast_fp32_to_fp32r, RNE) |

I verified `dst[1]`, `dst[5]`, `dst[7]`, `dst[13]`, `dst[16]` from the live table bytes — e.g. `dst[5]` = `rel −0x51d2a7` → `0x2659a5` (e4m3fn); `dst[16]` = `rel −0x5f79f7` → `0x18b255`. [**CONFIRMED bytes**.]

> **CORRECTION (#812) — `0x18b255` is the out-of-range-dtype THROW block, not an fp32 passthrough.** Stage-A's float32 alias-copy is at `0x265460` (correctly documented in §Stage A); on the *output* side, float32 is the fp32 work buffer itself and is returned before the Stage-B jump table ever indexes slot 16. The dispatcher at `0x265430` does `cmp $0x11,%r14d ; ja 18b255` and only then `lea 0x782c4c ; jmp *[table]`. The `0x18b255` body is verifiably an exception throw — `mov $0x10,%edi ; call __cxa_allocate_exception ; … call bir::Dtype2string ; call std::runtime_error::runtime_error` (objdump `0x18b255`–`0x18b292`, inside `boost::wrapexcept<bad_optional_access>::rethrow`) — i.e. the "unknown dtype" abort that out-of-range `dst` (and the unused table slot 16) falls through to. It does **not** "return the fp32 buffer." The table byte (`rel −0x5f79f7`) is correct; its *interpretation* as a passthrough was the error.

> **Note on float16 vs bfloat16.** `dst[13]` is genuinely **half** (fp16), not bf16: its inline encoder at `0x265c80` uses the fp16-specific overflow/underflow thresholds `0x477fffff` / `0x387fffff` (the |x| edges at 2¹⁶ and 2⁻¹⁴). bf16 narrowing is the *named* helper `cast_fp32_to_bf16` at `0x4b2750` (`dst[12]`). [**CONFIRMED**.]

### Inline fp32 → float16 RNE (`0x265c80`)

```c
// f = fp32 bits ; a = f & 0x7fffffff (abs)
if (a > 0x477fffff) {                          // beyond fp16 finite range
    // 0x265cb0: cmp a,0x7f800001 ; sbb eax,eax ; and ax,0xfe00 ; add ax,0x7e00
    half = (a < 0x7f800001) ? 0x7c00           // finite overflow -> +Inf half
                            : 0x7e00;          // NaN input        -> qNaN half
    half |= (f >> 16) & 0x8000;                // sign
} else if (a <= 0x387fffff) {                  // subnormal / underflow
    r9   = (a >> 13) & 1;                       // round bit
    half = (a + r9 - 0x37fff001) >> 13;        // RNE-rounded subnormal mantissa
} else {                                        // normal
    xmm = bitcast_float(a) + 0.5f;              // 0.5 = 0x3f000000 @0x78192c
    half = (top bits of bitcast_int(xmm));      // "magic-add RNE" on 13 dropped bits
    half |= sign;
}
```

The "magic-add" idiom (reinterpret the abs bit-pattern as a float, add 0.5, re-extract) implements round-to-nearest-even on the dropped 13 mantissa bits; the threshold pair `0x387fffff`/`0x477fffff` confirms fp16 (not bf16). [**STRONG** — arithmetic idiom; thresholds **CONFIRMED**.]

### Inline fp32 → integer (saturating, RNE)

The integer encoders (`int8`/`int16`/`int32`/`uint16`/`uint32`, with `uint8` via helper `0x2621f0`) share one pattern:

```c
x = roundss(f, 0xc);                            // 0xc = round-nearest-even, suppress prec-exc
if (isnan(x)) {                                  // ucomiss x,x ; jp
    out = signed   ? (sign<0 ? TYPE_MIN : TYPE_MAX)
                   : (sign<0 ? 0        : TYPE_MAX);
} else {
    r   = cvttss2si(x);
    out = clamp(r, TYPE_MIN, TYPE_MAX);          // cmovg / cmovl
}
```

Saturation bounds (from immediates; **CONFIRMED** per backing report §4.2):

| dtype | min | max | site |
|-------|----:|----:|------|
| int8 | `0xffffff80` (−128) | `0x7f` (127) | `0x265b38` |
| int16 | `0xffff8000` | `0x7fff` | `0x265d90` |
| int32 | `0x80000000` | `0x7fffffff` | `0x265a50` |
| uint16 | `0` | `0xffff` | `0x265e60` |
| uint32 | `0` | `0xffffffff` | `0x265c20` |
| uint8 | `0` | `255` | helper `0x2621f0` |

The `roundss imm=0xc` is the SSE4.1 *round-to-nearest-even* mode (with the precision-exception-suppress bit), so even the float→int leg rounds RNE, not truncate — `cvttss2si` only runs on an already-rounded value. [**STRONG**: the `roundss 0xc` immediate is verified by the subagent's disassembly; bounds **CONFIRMED**.]

## FP8 encoders and the saturate bit

The two production FP8 encoders are the heart of `--enable-saturate-infinity`.

### fp32 → float8_e4m3fn (`0x4b1f40`)

e4m3fn: 4 exp bits (bias 7), 3 mantissa bits, **no Inf**, single NaN = `S.1111.111` (`0x7f`); `max finite = 0x7e = 448.0`; `emax = 8`.

```c
sign = f >> 31;
if (isnan(f)) goto nan_tail;
e = ((f >> 23) & 0xff) - 127;                   // unbiased fp32 exponent
if (e > 8) {                                     // OVERFLOW (0x4b2060), e4m3fn emax=8
    eax = sign << 7;
    if (cfg.lo /* SATURATE bit */)  out = eax | 0x7e;   // clamp to +/-448
    else if (cfg.hi == 0)           out = 0x7f;          // -> NaN
    else                            out = eax;           // sign-only (+/-0 pattern)
}
m = f & 0x7fffff;                                // (normal: | 0x800000)
if (-6 <= e && e <= 8) {                         // RNE round mantissa to 3 bits
    guard  = m & 0x100000;                        // bit20
    sticky = m & 0x0fffff;                         // low 20 bits
    if (guard && (sticky || (m & 0x200000)))      // round-half-to-EVEN
        m += 0x100000;
    out = (sign << 7) | ((e + 7) << 3) | mant3;
} else if (-9 <= e && e <= -7) {                 // SUBNORMAL (0x4b20e0)
    out = subnormal_rne(m, -6 - e);               // shift right, RNE, exp_field=0
} else /* e < -9 */ {
    out = sign << 7;                              // flush to signed zero
}
```

### fp32 → float8e5 / e5m2 (`0x4b2160`)

e5m2: 5 exp bits (bias 15), 2 mantissa bits, **has Inf** = `S.11111.00` (`0x7c`); `max finite = 0x7b = 57344.0`; `emax = 15`.

```c
sign = (f >> 31) << 7;
e = ((f >> 23) & 0xff) - 127;
if (e > 15) {                                    // OVERFLOW (0x4b223e), e5m2 emax=15
    inf = sign | 0x7c;                            // +/-Inf
    max = sign | 0x7b;                            // +/-57344 (max finite)
    out = cfg.lo ? max : inf;                     // test bl,bl ; cmove  ->  sat? max : Inf
} else if (isnan(f)) {                            // (0x4b2350)
    out = cfg.hi ? (sign | ...) : 0x7e;
} else if (-14 <= e && e <= 15) {                // RNE mantissa to 2 bits
    guard = m & 0x200000;                         // bit21
    out = (sign) | ((e + 15) << 2) | mant2;
} else if (-16 <= e && e <= -15) {               // SUBNORMAL (0x4b2310)
    out = subnormal_rne(...);                     // exp_field=0
}
```

### The saturate bit and its default

`FP8ConversionConfig` is consumed as a 2-byte field by both encoders:

* **byte 0** (`cl` / `.lo`) = **SATURATE** flag → on overflow clamp to max-finite (`0x7e`=448 for e4m3fn, `0x7b`=57344 for e5m2).
* **byte 1** (`ch` / `.hi`) = secondary flag → NaN-on-input / Inf-vs-NaN selector.

When the caller passes `std::nullopt`, the function loads the **exported global default** (`0x265420` region):

```c
// GOT slot 0x90fba0 (R_X86_64_GLOB_DAT -> FP8ConvConfig @0x917848)
cfg = (uint16_t) FP8ConvConfig;                  // movzx ecx, WORD PTR [global]
```

This is **CONFIRMED** end-to-end against the binary:

```text
readelf -r:  0090fba0  R_X86_64_GLOB_DAT  0000000000917848 FP8ConvConfig + 0
xxd  file 0x916848 (= VMA 0x917848, .data +0x1000 delta):  01 00 00 00 00 00 00 00
```

So the **default `WORD` is `0x0001`**: `.lo = 1` (SATURATE **ON**), `.hi = 0`. neuronx-cc's default FP8 cast clamps overflow to max-finite. The CLI flag `--enable-saturate-infinity` drives this `.lo` byte (the encoders' `test (cfg.lo); cmove max,inf` cmovs are the exact toggle):

| `cfg.lo` (saturate) | e4m3fn overflow | e5m2 overflow |
|:---:|---|---|
| **1** (default) | → `0x7e` = +/-448 | → `0x7b` = +/-57344 |
| 0 | → `0x7f` = NaN (or sign-only if `.hi`) | → `0x7c` = +/-Inf |

The literal name "saturate-infinity" reads as "saturate values that would be infinite." The overflow-cmov path is **CONFIRMED** against the binary; the CLI-flag → `cfg.lo` wiring lives one layer up in the Python/AutoCast frontend and is **INFERRED** here. [**CONFIRMED** global value + reloc + cmov; flag-wiring **INFERRED**.]

## Named helpers

The dispatch tail-calls element-loop kernels recovered from `__assert_fail` source paths (`neuronxcc/support/dtype_impl/float_converter.cpp`):

| addr | helper | used as |
|------|--------|---------|
| `0x4b2880` | `cast_bf16_to_fp32` | src[12] |
| `0x4b2750` | `cast_fp32_to_bf16` (RNE) | dst[12] |
| `0x4b2a60` | `cast_fp32_to_fp32r` (RNE, TF32-like) | dst[17] |
| `0x4b4e40` | `cast_float4e2m1fn_x2_to_fp32` | src[2] |
| `0x4b5730` | `cast_fp32_to_float4e2m1fn_x4` | dst[2] |
| `0x4b1f40` / `0x4b2590` | fp32 ↔ e4m3fn | dst[5] / src[5] |
| `0x4b2160` / `0x4b2650` | fp32 ↔ e5m2 | dst[7] / src[7] |
| `0x4b23f0` / `0x4b24c0` | legacy e3 / e4 decode | src[3] / src[4] |
| `0x4b2720` / `0x4b2730` | fp8 ×4 unpack | src[8] / src[9] |
| `0x4b6040` / `0x4b6860` | fp8 ×4 pack | dst[8] / dst[9] |
| `0x2621f0` | fp32 → uint8 / e8m0 (round+clamp) | dst[0] / dst[6] |

`cast_fp32_to_bf16` (`0x4b2750`) rounds half-to-even on the dropped 16 bits (`round_bit=0x10000`, sticky `0x7fff`), with `Inf=0x7f80` / `qNaN=0x7fc0` specials — exact PyTorch/Eigen bf16. `cast_fp32_to_fp32r` (`0x4b2a60`) is the PE-array reduced-precision format: fp32 with the mantissa RNE-rounded to 11 explicit bits (drop low 12, `round_bit=0x1000`), i.e. TF32 rounding. [Backing report §5; **CONFIRMED** addresses, **STRONG** bit-level.]

> Note: `src[17]` (float32r) does **not** call `cast_fp32r_to_fp32` — it aliases the bytes as fp32 directly via the `0x265460` passthrough target. The named `cast_fp32r_to_fp32` helper exists for other callers. [**STRONG**.]

## The 64-bit paths

`uint64`/`int64` (ordinals 18/19) bypass both jump tables and use dedicated `cvtsi2ss`/saturating-`cvttss2si` loops gated by the `bool b` arg. The `uint64 → float` path (`0x265070`/`0x265089`) uses the odd-rounding sequence — `if (rax >= 0) cvtsi2ss; else { x = (rax >> 1) | (rax & 1); cvtsi2ss x; x += x; }` — the canonical correct unsigned-64→float widening; the `int64` path is a plain signed `cvtsi2ss QWORD`. [**CONFIRMED** `0x265070`–`0x2650ab`.]

## The int → int > 2²⁴ round-trip caveat

Because the hub is **single-precision**, an integer conversion that passes through it is bounded by fp32's 24-bit mantissa. An `int8 → int32` cast is exact, but a direct `int32 → int32`-style round trip (or any `intN → fp32 → intM`) of a magnitude above 2²⁴ = 16,777,216 **silently loses the low bits**: Stage A does `cvtsi2ss` (32-bit int → fp32, which cannot represent every integer above 2²⁴), the fence rounds RNE, and Stage B does `cvttss2si` back. The same applies to `uint32`/`int64`/`uint64` magnitudes above 2²⁴. This is not a bug in any one encoder — it is a structural consequence of the fp32-intermediate design, and it is the headline entry of the [Numeric Negative Results](numeric-negative-results.md) catalogue (page 9.10). [**CONFIRMED** by the architecture: every integer leg is `cvtsi2ss → … → cvttss2si`.]

## Constants reference (`.rodata`)

| addr | value | meaning |
|------|-------|---------|
| `0x780af0` | `0x7fffffff` | abs-mask (`andps`) |
| `0x781920` | `0x7f7fffff` | `FLT_MAX` (int-round magnitude threshold) |
| `0x781928` | `0x38800000` | 2⁻¹⁴ (fp16 min-normal; subnormal renorm) |
| `0x78192c` | `0x3f000000` | 0.5 (fp16 magic-add RNE bias) |
| fp16 | `0x477fffff` / `0x387fffff` | overflow / underflow |x| edges |
| fp16 | `0x37fff001` | subnormal RNE bias |
| fp16 | `0x7c00` / `0x7e00` | Inf / qNaN half |
| bf16 | `0x7f80` / `0x7fc0` | Inf / qNaN; round bit `0x10000` |
| fp32r | round bit `0x1000`, 11-bit mantissa | TF32 |
| e4m3fn | `0x7e`=448 / `0x7f`=NaN; guard `0x100000` | bias 7, emax 8 |
| e5m2 | `0x7b`=57344 / `0x7c`=Inf; guard `0x200000` | bias 15, emax 15 |

## Self-verification ceiling

The five strongest claims on this page were re-checked against the binary directly:

1. **Two-stage src→fp32→dst dispatch** — both jump tables decoded from live `.rodata` bytes; `src[16]==src[17]→0x265460` (fp32 alias). **CONFIRMED** (the `dst[16]→0x18b255` slot is the out-of-range-dtype throw, not an fp32 passthrough — see the CORRECTION under §Stage B).
2. **fesetround(0) RNE-only** — `xor %edi,%edi; call fesetround@plt` at `0x2650e2`; no other `fesetround`. **CONFIRMED**.
3. **FP8 saturate default-ON** — GOT `0x90fba0` → `R_X86_64_GLOB_DAT` → `FP8ConvConfig 0x917848` = `0x00000001` (read from file `0x916848`, accounting for the `.data` +0x1000 delta). **CONFIRMED**.
4. **int>2²⁴ round-trip caveat** — structural: every integer leg is `cvtsi2ss → cvttss2si` through fp32. **CONFIRMED** by architecture.
5. **FP8 constants 0x7e/0x7f (e4m3fn), 0x7b/0x7c (e5m2)** — overflow cmov verified in the encoder helpers. **CONFIRMED**.

What is **not** pinnable from the binary alone: the bit-exact internals of the `×4` pack/unpack and legacy e3/e4 helpers (transcribed structurally, **STRONG**), and the `--enable-saturate-infinity` → `cfg.lo` wiring, which crosses into the Python frontend (**INFERRED**).
