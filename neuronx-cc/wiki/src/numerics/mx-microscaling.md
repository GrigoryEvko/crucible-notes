# MX Microscaling: Quantize, Block-Scaling and E8M0

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 wheel), `neuronxcc/starfish/lib/libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`). The simulator binary maps VA == file offset for `.text` (`0xf1ad0..`) and `.rodata` (`0x58f000..`); the canonical fp8 narrow lives in the GOT-imported `libBIR`. Other versions and other strands of the toolchain will differ.*

## Abstract

The OCP **MX** (microscaling) format replaces one floating-point value per element with a pair: a *narrow* element (FP4 or FP8) plus a *shared block exponent* (E8M0, one unsigned byte) covering a fixed-size block of 32 elements. neuronx_cc lowers the HLO `QuantizeMX_*` custom-call to the BIR opcode `InstQuantizeMx` (class id 96), and the BIR simulator's `visitInstQuantizeMx` is the reference implementation of the silicon quantizer: it computes the per-block shared exponent, pre-scales every element down by that exponent, and round-to-nearest-even narrows each pre-scaled value into the destination grid. This page reconstructs that numeric model from the simulator's per-opcode kernel.

The piece worth understanding is the **EMAX scale method**. A block's shared exponent is *not* the average or the L2 norm — it is derived purely from the single largest-magnitude element's IEEE-754 exponent, measured against the destination format's maximum-normal exponent. The shared exponent is whatever power of two divides the block down so the biggest element lands at or just below the format's largest representable value. That makes the narrow rarely saturate: the EMAX bias is exactly the budget that maps the block max into the format's normal range. The math is a handful of integer operations on the raw bit patterns — no `ilogbf`, no logarithm, no division — which is why it maps cleanly to hardware.

This page is the *standalone* quantize path: BF16/F16 input → `{quantized FP4/FP8 data (x4-packed), E8M0 scale}`. It is distinct from the matmul-fused **dequantize** path (page 9.9), which consumes an existing E8M0 scale and applies it. Here we document how the scale is *computed* and how each element is *rounded into the grid*. Evidence is D-F13.

For reimplementation, the contract is:

- **Block geometry** — a block is 8 partitions × 4 free-dim columns = 32 elements; the scale tensor has shape `[P//8, F//4]`.
- **E8M0 derivation** — `max(1, max(0, maxBiasedExp(block) − EMAX_format))`, where `maxBiasedExp` is the largest IEEE-754 biased exponent over the 32 elements and `EMAX_format` is a per-dtype constant.
- **The prescale** — every element is multiplied by `2^(127 − E8M0)` before the narrow, mapping it into the destination format's normal range.
- **The narrow** — FP4 e2m1 has a *local* RNE-saturate encoder (grid `{0, ±0.5, ±1, ±1.5, ±2, ±3, ±4, ±6}`); FP8 e4m3fn/e5m2 are delegated to `libBIR::CastToNewDType` with saturate forced on.
- **The packing** — FP4 nibbles are x4-packed two-per-byte with a stride-dependent lane permutation; the E8M0 scale tensor is laid out with a `(i&3) + 32*(i>>2)` block-major interleave.

| | |
|---|---|
| **Opcode** | `InstQuantizeMx`, BIR class id 96 |
| **Visitor** | `birsim::InstVisitor::visitInstQuantizeMx` @ `0x1f6920` (full inlined body; `0xed230` is the `.`-prefixed PLT copy) |
| **Numeric core** | `doQuantizeMx` @ `0x29e380` → `generateMxScales` @ `0x29e1d0` + `quantizeDataApplyingMxScales` @ `0x29e2a0` |
| **Block reduce** | `findMaxExponentPerInputBlock` @ `0x29e150` |
| **EMAX table** | `byte_5F7898` @ `0x5F7898` = `02 00 00 08 00 0f 08 0f` (indexed `dt−2`) |
| **FP4 encode** | `sub_2A3530` (RNE + saturate-to-6.0) ; **decode** `sub_2A3F30` |
| **FP8 narrow** | delegated → `bir::CastToNewDType` (PLT `0xed930` → `0x22b1230`, libBIR import) |
| **Block size** | 8 partitions × 4 columns = **32 elements** (OCP block_size) |
| **Scale tensor** | E8M0 `uint8`, shape `[P//8, F//4]`, `(i&3)+32*(i>>2)` interleave |
| **Operands** | 1 input (BF16/F16), 2 outputs (`{data, scale}`) — the HLO 2-tuple |

---

## The Quantize Data Path

### Purpose

`visitInstQuantizeMx` is the simulator twin of the silicon QuantizeMx unit and of the CoreV4 encoder's `visitInstQuantizeMx` (wire opcode `0xE3` = 227, `S3DMX1_QUANT`). The HLO contract is fixed: a BF16 or F16 input tensor becomes a 2-tuple `{quantized FP4/FP8 data, E8M0 per-block scale}` with `block_size=32` and `scale_method=EMAX` (see page 4.29 for the legalization of the custom-call).

### Entry Point

The visitor takes three BIR operands via `getInOutPhysicalAP(I, idx, isOutput)` @ `0x1cfdc0`:

```text
visitInstQuantizeMx (0x1f6920)        ── opcode-96 standalone visitor
  ├─ getInOutPhysicalAP(I, 0, 0)      ── INPUT  : BF16/F16 source tensor      → v60
  ├─ getInOutPhysicalAP(I, 0, 1)      ── OUTPUT0: quantized DATA (FP4/FP8 x4)  → v62
  ├─ getInOutPhysicalAP(I, 1, 1)      ── OUTPUT1: E8M0 SCALE tensor (uint8)    → v66
  ├─ cast_to(input → Dtype 16 = fp32) ── widen BF16/F16 to fp32  (0x1f6df8)
  ├─ doQuantizeMx(in, outData, outScale, dataDtype)   ── the numeric core (0x29e380)
  ├─ FP8ConversionConfig stamp        ── saturate byte[0]=1, forced  (0x1f6ff4)
  └─ Memory::write(data) / Memory::write(scale)  ── vtable+72, narrow happens HERE
```

The 1-input/2-output shape *is* the HLO 2-tuple. (CONFIRMED — operand fetches and the two `call qword ptr [rax+48h]` writes at `0x1f703f` / `0x1f7061` read end-to-end.)

### Algorithm

```c
function visitInstQuantizeMx(I):                       // sub_1F6920
    in   = getInOutPhysicalAP(I, 0, 0);   // BF16/F16
    data = getInOutPhysicalAP(I, 0, 1);   // FP4/FP8 x4 out
    scale= getInOutPhysicalAP(I, 1, 1);   // E8M0 uint8 out

    NEPP = getNumElementsPerPartition(data);           // free-dim extent
    dt   = data.Dtype;                                 // AP+48 (0x30)
    if (dt == 2 || (uint)(dt - 8) <= 1)                // x4 dtypes {2, 8, 9}
        NEPP *= 4;                                     // DATA out is x4-expanded

    cast_to(inputMemObj -> Dtype 16 /*fp32*/);         // widen BF16/F16 → fp32
    copyArrayTo2DVector<float>(inputMemObj -> in2D);   // partition-major 2D

    doQuantizeMx(in2D, outData2D, outScale2D, dt);     // sub_29E380 — §scale, §apply

    copy2DVectorToArray<float>(outData2D -> fp32buf);  // pre-scaled fp32 data
    copy2DScaleVectorToArray<uchar>(outScale2D -> e8m0buf);  // block-major interleave

    // FP8ConversionConfig stamp (sub_1F6FF4):
    cfg.byte[0] = 1;                                   // SATURATE ON, unconditional
    cfg.byte[1] = FP8ConvConfig[1];                    // nan-suppress (forced ≥1)

    Memory::write(data, dataMemObj, I, 0);   // vtable+72: fp32→FP4/FP8 NARROW + reshape
    Memory::write(scale, scaleMemObj, I, 0); // write E8M0 scale tensor
```

> **QUIRK —** the `data` output's free extent is multiplied by 4 for the x4 dtypes `{2, 8, 9}` (`float4_e2m1fn_x4`, `float8_e4m3fn_x4`, `float8_e5m2_x4`). Four logical narrow elements live inside one container. A reimplementation that sizes the data buffer by the logical element count rather than the container count will under-allocate by 4×. The trigger is `dt == 2 || (dt − 8) <= 1u`.

> **NOTE —** the fp32 → FP4/FP8 narrow does **not** happen inside `doQuantizeMx`. `doQuantizeMx` leaves the *pre-scaled fp32* values in `outData2D`; the actual width reduction is deferred to `Memory::write` → `cast_and_reshape` → `cast_to` → the per-format narrow. So the simulator computes the scale and the prescale eagerly, but narrows lazily on write.

---

## Block-Scale Computation

### Purpose

`doQuantizeMx` @ `0x29e380` picks the per-format EMAX, calls `generateMxScales` to fill the E8M0 scale tensor, then tail-calls `quantizeDataApplyingMxScales`. The scale derivation is the heart of the EMAX method.

### Algorithm — doQuantizeMx (EMAX pick)

```c
function doQuantizeMx(in2D, outData2D, scale2D, bir::Dtype dt):   // sub_29E380
    idx  = (uint)(dt - 2);                             // sub ecx, 2
    emax = 0;
    if (idx <= 7u)                                     // cmp ecx, 7; ja
        emax = byte_5F7898[idx];                       // lea unk_5F7898; movzx [rax+rcx]

    // emaxBias = (MxAlternativeEmaxEn == 0) + emax − 1
    // encoded as: cmp byte[MxAlternativeEmaxEn], 1 ; adc dl, 0xFF
    //   MxAlternativeEmaxEn == 0 → CF=1 → dl + 0xFF + 1 ≡ emax       (default)
    //   MxAlternativeEmaxEn == 1 → CF=0 → dl + 0xFF   = emax − 1     (alt mode)
    generateMxScales(in2D, scale2D, (uint8)emaxBias);  // sub_29E1D0

    return quantizeDataApplyingMxScales(in2D, outData2D, scale2D);  // tail-call sub_29E2A0
```

> **QUIRK —** the `emaxBias` is built with one carry-arithmetic trick: `cmp byte[MxAlternativeEmaxEn], 1` followed by `adc dl, 0xFFh`. Comparing 0 against 1 *borrows* (sets CF), so the default `MxAlternativeEmaxEn == 0` path adds `0xFF + 1 ≡ 0 (mod 256)` and leaves `emaxBias == EMAX`. The alternative mode (`== 1`) clears CF, adds `0xFF == −1`, giving `EMAX − 1`. This matches the NEFF emission `emax_fp8_e4m3 = MxAlternativeEmaxEn ? 7 : 8`, `emax_fp8_e5m2 = ? 14 : 15`. (CONFIRMED — disasm `0x29e3b0`–`0x29e3b6`.)

### The EMAX Table

`byte_5F7898` @ `0x5F7898` is an 8-byte table of per-format **maximum-normal exponents** (unbiased), indexed by `dt − 2`:

| `dt` | Dtype | Index | EMAX | Largest normal | Confidence |
|---|---|---|---|---|---|
| 2 | `float4_e2m1fn_x4` | `[0]` = `0x02` | 2 | 6.0 (= 4·1.5) | CONFIRMED |
| 3 | `float8e3` (legacy) | `[1]` = `0x00` | 0 | — (not an OCP MX target) | CONFIRMED |
| 4 | `float8e4` (legacy) | `[2]` = `0x00` | 0 | — | CONFIRMED |
| 5 | `float8_e4m3fn` | `[3]` = `0x08` | 8 | 448 | CONFIRMED |
| 6 | `float8_e8m0fnu` | `[4]` = `0x00` | 0 | (the scale type itself) | CONFIRMED |
| 7 | `float8e5` | `[5]` = `0x0f` | 15 | 57344 | CONFIRMED |
| 8 | `float8_e4m3fn_x4` | `[6]` = `0x08` | 8 | 448 | CONFIRMED |
| 9 | `float8_e5m2_x4` | `[7]` = `0x0f` | 15 | 57344 | CONFIRMED |

> **NOTE —** these are the OCP per-format max-normal exponents. e2m1's largest representable is `4.0 · 1.5 = 6.0` (emax 2); e4m3fn's is 448 (emax 8); e5m2's is 57344 (emax 15). They are the *reference* the block shared exponent is measured against. The table read instruction (`movzx edx, byte[unk_5F7898 + rcx]`) is CONFIRMED in the `doQuantizeMx` disasm; the table byte values are CONFIRMED by `xxd` of the `.rodata` at `0x5F7898` and are byte-identical to the matmul-MX dequant transcription (D-F02 §7).

### Algorithm — findMaxExponentPerInputBlock

This is the per-block max-exponent reduce — the EMAX method's measurement step.

```c
function findMaxExponentPerInputBlock(in2D, rowGroup, colBlock):   // sub_29E150
    // window: 8 partition-rows × 4 columns = one 32-element block
    c0 = 4 * colBlock;  c1 = 4 * colBlock + 4;        // lea r11,[rdx*4]; lea r8,[r11+4]
    rowBase = in2D.begin;
    uint8 exp[32];                                    // stack buffer
    grpByteBase = 192 * rowGroup;                     // lea r10,[rsi+rsi*2]; shl r10,6  ⇒ ×192
    j = 0;                                            // dest index into exp[]
    for (partRow = 0; partRow < 8; partRow++) {       // 192 = 24·8 ; +24 per row
        float* row = *(float**)(rowBase + grpByteBase);   // 24 = sizeof(vector<float>)
        for (col = c0; col != c1; col++) {            // 4 columns
            uint32_t bits = ((uint32_t*)row)[col];    // mov edx, [rdi+rax*4]
            exp[partRow*4 + (col & 3)] = (uint8)(bits >> 23);  // shr edx,17h; mov [..],dl
        }
        grpByteBase += 24;                            // next partition-row's vector header
    }
    // max-reduce the 32 exponent bytes  (cmovb):
    uint8 m = exp[0];
    for (k = 1; k < 32; k++) if (m < exp[k]) m = exp[k];
    return m;                                         // the block's largest biased fp32 exponent
```

> **GOTCHA —** the per-element exponent is `(fp32_bits >> 23)` *truncated to a byte*. Bit 31 (the sign) is shifted into bit 8 of the 9-bit field `bits>>23` and then **dropped by the byte store** (`mov [..], dl`). So the reduce is over the magnitude exponent only — it is **sign-independent**. This is a raw bit-extract equal to `ilogb(x) + 127`, **not** a call to `ilogbf`: a *denormal* fp32 input reads exponent byte `0` (its biased exponent field is 0), so a block of only denormals yields a max-exp of 0. A reimplementation that calls `ilogbf` (which returns `INT_MIN`/`FP_ILOGB0` for zero and the true sub-normal exponent for denormals) will diverge on tiny inputs.

> **NOTE —** the geometry constants pin the block shape exactly: `192 = 24 · 8` where 24 is `sizeof(std::vector<float>)` (three pointers: begin/end/cap), so `rowGroup` selects a *group of 8 partition rows*; the `+24` inner step walks one vector header at a time; the column window is `[4·colBlock, 4·colBlock+4)` = 4 columns. 8 partitions × 4 columns = **32 elements** = the OCP MXFP `block_size`.

### Algorithm — generateMxScales (E8M0 clamp)

```c
function generateMxScales(in2D, scale2D, uint8 emaxBias):          // sub_29E1D0
    cols    = (in2D[0].end - in2D[0].begin) >> 2;     // floats per partition
    rows    = (in2D.end - in2D.begin) / 24;           // #partition vectors (×0xAAAA..AB / 24)
    nRowGrp = rows >> 3;                               // rows / 8   (8 partitions per group)
    nColBlk = cols >> 2;                              // cols / 4   (4 columns per block)
    for (rowGroup = 0; rowGroup < nRowGrp; rowGroup++)
        for (colBlock = 0; colBlock < nColBlk; colBlock++) {
            int v = (uint8)findMaxExponentPerInputBlock(in2D, rowGroup, colBlock);
            v -= emaxBias;                            // sub eax, r12d
            if (v < 0) v = 0;                         // cmovs eax, 0   — CLAMP-LOW
            uint8* dst = &scale2D[rowGroup][colBlock];
            *dst = (uint8)v;                          // mov [..], al
            if (*dst == 0) *dst = 1;                  // cmp byte,0; jnz; mov byte,1 — ZERO-FLOOR
        }
```

The result, per 8×4 block:

```text
E8M0 = max( 1, max(0, maxBiasedExp(block) − emaxBias) )
       └─────────────┘  └─────────────────────────────┘
        ZERO-FLOOR        CLAMP-LOW (signed result floored to 0)
```

with `emaxBias = byte_5F7898[dt−2]` in the default mode. Interpretation:

- `maxBiasedExp = (largest |elem| exponent) + 127`. `emaxBias = EMAX_format`. So `maxBiasedExp − emaxBias = (largest |elem| exponent + 127) − EMAX` is the E8M0 byte such that `2^(E8M0 − 127)` divides the block down until its largest element lands at or below the format's emax. This *is* the OCP "EMAX" `scale_method`.
- **CLAMP-LOW (→ 0):** a block whose max already fits the format would produce a *negative* shared exponent byte. E8M0 is unsigned, so the signed result is floored to 0 (`cmovs`) — meaning "no down-scaling needed".
- **ZERO-FLOOR (→ 1):** an E8M0 byte of 0 (= `2^−127`, the smallest E8M0) is bumped to 1 (= `2^−126`). This is the denormal / all-zero-block guard: a fully-zero or already-tiny block keeps a valid non-zero shared exponent, and the dequant side never multiplies by `2^−127` exactly.

> **GOTCHA —** the two clamps **compose in order**: the signed `maxExp − emaxBias` is first floored to 0, *then* a resulting 0 byte is bumped to 1. The emitted E8M0 byte is therefore always in `[1, 255]`. Skipping either clamp breaks round-trip exactness with the matmul-MX dequant (page 9.9), which reads the same bytes back. (CONFIRMED — `cmovs eax, edi` at `0x29e260`, `mov byte[rcx], 1` at `0x29e276`.)

### Algorithm — quantizeDataApplyingMxScales (the prescale)

```c
function quantizeDataApplyingMxScales(in2D, out2D, scale2D):       // sub_29E2A0
    cols = (in2D[0].end - in2D[0].begin) >> 2;
    rows = (in2D.end - in2D.begin) / 24;
    for (row = 0; row < rows; row++)
        for (col = 0; col < cols; col++) {
            uint8 e8m0 = scale2D[row >> 3][col >> 2];     // same 8×4 block index as §scale
            uint32_t mbits = (254u - e8m0) << 23;         // mov ebp,0FEh; sub; shl 17h
            float mul = bit_cast<float>(mbits);           // movd xmm0, edx
            out2D[row][col] = mul * in2D[row][col];       // mulss xmm0, [rsi+rax*4]
        }
```

The multiplier is `bit_cast<float>((254 − E8M0) << 23) = 2^((254 − E8M0) − 127) = 2^(127 − E8M0)`. So:

```text
out = in · 2^(127 − E8M0) = in / 2^(E8M0 − 127)
```

which is the **exact inverse** of the matmul-MX dequant's `ldexpf(x, E8M0 − 127)` (page 9.9). The prescale divides each element down by the block's shared exponent so the narrow that follows operates on values already mapped into the format's normal range.

> **NOTE —** the constant is **254, not 255**. The E8M0 bias is 127; building a multiplier as a normal fp32 with exponent field `(254 − E8M0)` gives `2^(127 − E8M0)`: `E8M0 = 127` (unbiased 0) → `2^0 = ×1` (no scale); `E8M0 > 127` → scale down; `E8M0 < 127` → scale up. The ZERO-FLOOR (`E8M0 ≥ 1`) keeps `(254 − E8M0) ≤ 253`, so the multiplier is always a finite normal fp32, never `Inf`. (CONFIRMED — `mov ebp, 0FEh` at `0x29e2ef`.)

---

## Round-to-Grid: FP4 and FP8

The pre-scaled fp32 values still have to be narrowed into the destination grid. The two element widths take different paths: FP4 is narrowed by a **local** encoder inside the simulator; FP8 is **delegated** to `libBIR`.

### FP4 e2m1 (dtype 2) — local encode/decode

The e2m1 grid is `{1 sign, 2 exp, 1 mantissa, bias 1}`. The decoder `sub_2A3F30` gives the 16 grid points directly:

```c
function decode_e2m1(uint8 nib):                       // sub_2A3F30
    sign    = nib >> 3;
    mantBit = nib & 1;
    exp     = (nib >> 1) & 3;
    if ((exp | mantBit) && (exp || !mantBit)):          // normal (exp 1..3, or m with exp)
        bits = (mantBit << 22) | ((exp + 126) << 23) | (sign << 31);
    else:                                               // ±0  or  the ±0.5 subnormal
        bits = (mantBit ? 0x3F000000 : 0) | (sign << 31);
    return bit_cast<float>(bits);
```

The fp32 exponent is `e2m1_exp + 126`, so the e2m1 bias is `127 − 126 = 1`. The full grid:

| `exp\m` | m=0 | m=1 |
|---|---|---|
| 0 | ±0 | ±0.5 (subnormal, `0x3F000000`) |
| 1 | ±1.0 | ±1.5 |
| 2 | ±2.0 | ±3.0 |
| 3 | ±4.0 | ±6.0 |

**Grid = `{0, ±0.5, ±1, ±1.5, ±2, ±3, ±4, ±6}`.** Max magnitude 6.0. No `Inf`, no `NaN` — `e2m1fn` is finite-only. (CONFIRMED — every grid point derived from the decode arithmetic.)

The encoder `sub_2A3530` (fp32 → 4-bit nibble) is RNE with hard saturate:

```c
function encode_e2m1(float x):                          // sub_2A3530, disasm-verified
    sign = (bits(x) >> 31) << 3;                        // shr esi,1Fh; shl esi,3
    if (isnan(x))                                        // ucomiss x,x; jp
        return sign;                                    // → signed ZERO nibble (fp4 has no NaN)
    e = (bits(x) >> 23) & 0xFF;                          // biased fp32 exponent
    if ((int)e - 0x7F > 2)                               // lea ecx,[rax-7Fh]; cmp ecx,2; jg
        return sign | 7;                                // SATURATE to nibble 0x7 = ±6.0
    // ... RNE round of the (1-bit) mantissa, guard bit 21 (bt r10d,15h), sticky/odd ...
    // round-up across the 6.0 boundary (cmp r8d,3; jz) ALSO saturates to sign|7.
    // denormal (exp < 0) branch normalises with the same RNE; values below ~0.25 → ±0.
```

> **GOTCHA —** FP4 overflow **saturates**, it does not produce `Inf` (there is no `Inf` code). Any pre-scaled value whose unbiased exponent exceeds 2 (i.e. `|x| ≥ 8`) clamps to `sign | 0x7 = ±6.0`, and RNE rounding *up* across the 6.0 boundary saturates too. A `NaN` input becomes a *signed zero* nibble, not a poison value. The saturate is `or eax, 7` at `0x2a3630`. There is no rounding-mode field anywhere — RNE is hard-wired, matching the fp8 paths.

### FP8 e4m3fn (dtype 5/8) and e5m2 (dtype 7/9) — delegated

The simulator has **no** local canonical-fp8 narrow. The string table carries only the FP4 and bf16/fp16/fp32 `cast_*` signatures — there is no `cast_fp32_to_float8e4m3fn` / `e5m2` symbol. The narrow runs in the GOT-imported `libBIR`:

```text
Memory::write(data) → MemoryObject::cast_and_reshape (0x28f1f0)
                    → MemoryObject::cast_to           (0x28db30)
                    → bir::CastToNewDType  (PLT 0xed930 → 0x22b1230, libBIR import)
```

`CastToNewDType(shared_ptr<vector<uchar>>, fromDtype, toDtype, bool, optional<FP8ConversionConfig>)` does the round/saturate per format, driven by the `FP8ConversionConfig` stamped before the write (saturate `byte[0] = 1`):

| `toDtype` | Format | Max finite | Overflow (sat=1) | Rounding | Confidence |
|---|---|---|---|---|---|
| 5 / 8 | `float8_e4m3fn` (1s/4e/3m, bias 7) | 448 = code `0x7E` | `0x7E` (no `Inf`) | hard RNE | HIGH (libBIR import) |
| 7 / 9 | `float8_e5m2` (1s/5e/2m, bias 15) | 57344 = code `0x7B` | `0x7B` (no `Inf`; `0x7C` is the `Inf` code, only with sat=0) | hard RNE | HIGH (libBIR import) |
| 2 | `float4_e2m1fn` | 6.0 | the LOCAL packer (config ignored) | hard RNE | CONFIRMED |

> **QUIRK —** the standalone QuantizeMx **always** narrows fp8 with saturate on (`byte[0] = 1`, forced unconditionally at `0x1f6ff4`). This is different from the simulator's fp8 *reduce* path (`sub_2a36e0`/`38d0`/`3cf0`, dtypes 3/4/7), which passes `config = 0` (Inf-mode, the legacy `E3M4`/`E4M3-240`/`E5M2` accumulate narrows). Do not conflate the two: QuantizeMx targets dtypes `{2, 5, 7, 8, 9}` and saturates; the reduce path is Inf-mode. The e4m3fn 448 / e5m2 57344 maxima are exactly the OCP MX targets, and the §scale EMAX bias is precisely what keeps the pre-scaled block max below them so the narrow rarely saturates. The narrow constants live in the imported libBIR (same `float_converter.cpp` source baked into this binary @ `0x5cae80`), so they are HIGH for this binary, not re-disassembled here; the *delegation* is CONFIRMED.

---

## x4-Pack and Scale-Tensor Interleave

Two reshapes finish the quantize: FP4 nibbles are x4-packed with an intra-container lane permutation, and the E8M0 scale tensor is laid out with a block-major interleave.

### FP4 x4-Pack and Intra-x4 Lane Order

The FP4 narrow-and-pack driver is `cast_fp32_to_float4e2m1fn_x4 = sub_2A72C0` (string @ `0x5cb1d8`). It asserts `count % 4 == 0 && "Input count must be multiples of 4"` (`float_converter.cpp`), then per element: `sub_2A3530` (encode nibble) → `sub_2A6A80` (pack into the x4 container).

```c
function pack_nibble(packCtx, index, nibble):           // sub_2A6A80 (live path past the assert)
    if (index >= nibbleCount) assert_fail("index < (nibbleCount)");
    stride = packCtx[24];
    if (stride) index = lane_remap(index, stride);       // sub_2A3500 — LANE permutation
    nib = nibble & 0xF;                                  // and edx, 0Fh
    uint8* dst = base + (index >> 1);                    // 2 nibbles per byte
    if ((index & 1) == packCtx[16])                      // packCtx[16] = nibble-parity flag
        *dst = (*dst & 0x0F) | (nib << 4);              // HIGH nibble
    else
        *dst = (*dst & 0xF0) | nib;                     // LOW nibble
```

The lane-order remap is a deinterleave by `2·stride`:

```c
function lane_remap(index, stride):                      // sub_2A3500
    w = 2 * stride;                                     // window
    q = index / w;  r = index % w;                      // div rsi
    result = w * (q + 1) - r;                            // reverse within the window
    if ((index & 1) == 0) result -= 2;                  // even/odd pairing offset
    return result;
```

> **GOTCHA —** the four logical FP4 sub-elements of an x4 container are **not** in plain little-endian nibble order. Within each window of `2·stride` elements the linear index is *reversed* and even/odd indices are paired offset-by-2. A reimplementation that packs nibble `k` at byte `k>>1`, parity `k&1`, with no remap, will produce a byte-permuted result that the dequant lane-unpack (`sub_2A6080`, which applies the *same* `sub_2A3500`) cannot read back. The permutation **formula** is CONFIRMED; the concrete `stride`/`parity` for a given tensor come from the access-pattern geometry via `sub_2A58A0` (read at role level, not field-traced to a specific tile), so they are HIGH, not pinned to a NEFF fixture.

> **NOTE —** FP8 x4 (dtypes 8/9) carries its x4-ness in the access pattern (`num × 4` with a u32 container stride of 4), **not** in a nibble pack. FP8 bytes are 1-per-element and contiguous — only FP4 needs the nibble permutation, because two FP4 elements share a byte.

### E8M0 Scale-Tensor Layout

`copy2DScaleVectorToArray<uchar>` @ `0x29e570` writes the 2D scale vector to the output E8M0 byte tensor with a reorder:

```c
function copy2DScaleVectorToArray(scale2D, base, nScaleRows, scaleBytesPerRow):  // sub_29E570
    for (i = 0; i < nScaleRows; i++) {
        size_t slot   = (i & 3) + 32 * (i >> 2);        // shr/shl 5 + and 3 + add
        size_t dstOff = slot * scaleBytesPerRow;        // imul rdi, r14
        memcpy(base + dstOff, scale2D[i].data, scaleBytesPerRow);
    }
```

Scale-row `i` lands at output slot `(i & 3) + 32 * (i >> 2)`: groups of 4 consecutive scale rows stay contiguous, then the group jumps by 32. The dequant side reads it back via `copyScalesFromArray<uchar>` @ `0x29e5e0` with the **identical** index — a symmetric write/read, round-trip exact.

> **NOTE —** the `32` stride is the 8-partition × 4-column MX block granularity in scale space; the 4-bit group `(i & 3)` maps to the `MXMEM_PATTERN1D` "scale-stream selector" (≤ `0x0F`, the 4-bit E8M0 sub-bank) at `byte+15` of the descriptor (page 2.6). `nScaleRows` is `(Pattern[0].num) >> 3` (= partitions / 8) and `scaleBytesPerRow` is the scale-AP free extent (= cols / 4). So the **E8M0 scale tensor shape is `[partitions/8, cols/4]` = `[P//8, F//4]`**, laid out with the `(i&3) + 32*(i>>2)` interleave. (CONFIRMED — `0x29e596`–`0x29e5c2`.)

---

## End-to-End MX Quantize Contract

The simulator is the functional twin of the silicon QuantizeMx. The layers line up:

| Layer | Artifact | Anchor |
|---|---|---|
| HLO (4.29) | `QuantizeMX_{f16\|bf16}_{e5m2\|e4m3fn}`, 1 input → 2-tuple `{data, scale}`, `block_size=32`, `scale_method=EMAX` | 1-in/2-out operands; block = 8×4 = 32 |
| NKI | `quantize_mx()`: `scale=[P//8, F//4]`, FP8-x4 dst, src 32-mult / 4-mult | scale shape; NEPP×4; `count % 4` assert |
| MLIR | `nisa.quantize_mx(src, dst, dst_scale)` | three operands |
| BIR | `InstQuantizeMx` = class id 96 | opcode 96 → `visit` @ `0x1f6920` |
| wire | `S3DMX1_QUANT` op `0xE3` = 227; `byte[1].bit0` = saturate; `MEM_PATTERN3D` data + `MXMEM_PATTERN1D` scale | `FP8ConvConfig[0]` saturate; scale interleave |
| sim numeric | `E8M0 = max(1, max(0, maxExp − EMAX))`; data `× 2^(127 − E8M0)`; narrow → FP4/FP8 grid | `generateMxScales`; prescale; round-to-grid |

The runtime E8M0 max-abs reduce was previously known to "live in silicon, not in libwalrus"; the simulator's `findMaxExponentPerInputBlock` is exactly that reduce, recovered as the reference implementation.

---

## Function and Table Map

| Function | Addr | Role | Confidence |
|---|---|---|---|
| `visitInstQuantizeMx` | `0x1f6920` | opcode-96 standalone visitor | CONFIRMED |
| `doQuantizeMx` | `0x29e380` | EMAX pick + generate + apply | CONFIRMED |
| `generateMxScales` | `0x29e1d0` | E8M0 = clamp(maxExp − EMAX), zero-floor 1 | CONFIRMED |
| `findMaxExponentPerInputBlock` | `0x29e150` | max biased fp32 exp over 8×4 = 32 block | CONFIRMED |
| `quantizeDataApplyingMxScales` | `0x29e2a0` | data × 2^(127 − E8M0) prescale | CONFIRMED |
| `copyArrayTo2DVector<float>` | `0x29e3e0` | input fp32 array → 2D (partition-major) | CONFIRMED |
| `copy2DVectorToArray<float>` | `0x29e4a0` | scaled fp32 2D → data array | CONFIRMED |
| `copy2DScaleVectorToArray<uchar>` | `0x29e570` | E8M0 2D → tensor, `(i&3)+32(i>>2)` | CONFIRMED |
| `copyScalesFromArray<uchar>` | `0x29e5e0` | inverse scale interleave (dequant) | CONFIRMED |
| `cast_fp32_to_float4e2m1fn_x4` | `0x2a72c0` | FP4 narrow+pack driver (`count % 4`) | CONFIRMED |
| ` └ fp32→e2m1 nibble encode` | `0x2a3530` | RNE + saturate-to-6.0 (`or eax, 7`) | CONFIRMED |
| ` └ x4 nibble pack` | `0x2a6a80` | 2 nibbles/byte + lane remap | CONFIRMED |
| ` └ lane-order remap` | `0x2a3500` | deinterleave-by-2·stride | CONFIRMED |
| ` └ pack-ctx init` | `0x2a58a0` | stride/parity from AP | HIGH |
| `cast_float4e2m1fn_x2_to_fp32` | `0x2a6900` | FP4 decode driver (`count % 2`) | CONFIRMED |
| ` └ e2m1 nibble → fp32` | `0x2a3f30` | grid `{0,.5,1,1.5,2,3,4,6}` | CONFIRMED |
| `MemoryObject::cast_and_reshape` | `0x28f1f0` | fp32→dst narrow + reshape | CONFIRMED |
| `MemoryObject::cast_to` | `0x28db30` | → `bir::CastToNewDType` | CONFIRMED |
| `bir::CastToNewDType` | `0xed930` → `0x22b1230` | canonical fp8 narrow (libBIR import) | CONFIRMED (import) |
| `getInOutPhysicalAP` | `0x1cfdc0` | `(I, idx, isOutput)` operand AP | CONFIRMED |
| `byte_5F7898` | `0x5F7898` | EMAX table `02 00 00 08 00 0f 08 0f` | CONFIRMED |
| `FP8ConvConfig` (GOT import) | → libBIR | saturate byte[0] / nan-suppress byte[1] | CONFIRMED |
| `MxAlternativeEmaxEn` (GOT import) | → libBIR | EMAX − 1 select | HIGH |

**Access-pattern field offsets** (`PhysicalAccessPattern`): `AP+48 (0x30)` Dtype · `AP+80 (0x50)` Pattern `SmallVector<APPair>` ptr · `AP+88 (0x58)` Pattern.size · `Pattern[0]+8` = #partitions.

---

## Gaps and Caveats

- **G1 — FP8 narrow constants.** `bir::CastToNewDType` is a libBIR PLT import (`0x22b1230` stub, decompile-failed). The canonical fp32 → `{e4m3fn, e5m2}` round/saturate bytes (448 = `0x7E`, 57344 = `0x7B`, RNE bias, NaN codes) are taken from the imported libBIR (same `float_converter.cpp` source) — HIGH for this binary; the simulator does not contain the canonical fp8 narrow at all (only the legacy reduce-path narrows).
- **G2 — GOT-imported globals.** `FP8ConvConfig` (default `0x0001` saturate-on) and `MxAlternativeEmaxEn` (the alt-emax bool) live in libBIR, read through `_ptr` GOT slots. `MxAlternativeEmaxEn`'s default byte was not re-read from libBIR here; it is HIGH that it is 0 (the `EMAX`-not-`EMAX−1` mode, matching the default NEFF `emax_fp8_e4m3 = 8` / `e5m2 = 15`).
- **G3 — lane-permutation parameterisation.** `sub_2A58A0` sets `packCtx` stride (`+24`) and parity (`+16`) from the access pattern, but the exact AP-field → stride mapping was not field-traced. The permutation **formula** is CONFIRMED; its concrete stride/parity for a given tile is HIGH (no NEFF byte-fixture).
- **G4 — clamp rationale.** The ZERO-FLOOR (`E8M0 0 → 1`) and CLAMP-LOW (`signed < 0 → 0`) are recovered byte-exact. Whether silicon uses the same all-zero-block convention is inferred from the arithmetic, not cross-checked against a hardware MX reference. (INFERRED for the silicon claim; the simulator behavior is CONFIRMED.)

---

## Cross-References

- [MX FP8 Legalization](../hlo-opt/mx-fp8-legalization.md) — page 4.29; how `QuantizeMX_*` lowers from HLO to BIR `InstQuantizeMx`
- [MX MatMul (simulator)](../bir/sim-matmul-mx.md) — page 9.9; the dequant that consumes this E8M0 scale (`ldexpf(x, E8M0 − 127)`)
- [MX MatMul Codegen](../bir/codegen-matmul-mx.md) — the CoreV4 encoder side of the same MX path
- [MXMEM Pattern1D](../isa/mxmem-pattern1d.md) — page 2.6; the scale-stream descriptor whose 4-bit selector the `(i&3)+32(i>>2)` interleave drives
- [CastToNewDType](cast-to-new-dtype.md) — page 9.3; the libBIR per-format fp8 narrow that finishes the e4m3fn/e5m2 write
- [MXFP Weight-Scale Load](../nki/mxfp-weight-scale-load.md) — the NKI subkernel that reads `[P//8, F//4]` E8M0 scales back for matmul
