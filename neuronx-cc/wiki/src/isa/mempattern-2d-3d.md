# MEM_PATTERN2D / 3D — the DST/PSUM Role

> *All addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; the cp311/cp312 wheels rebuild `libwalrus.so`, so confirm any address against the target wheel — see [Build & Version Provenance](../reference/versions.md)). Every symbol and offset is read from `neuronxcc/starfish/lib/libwalrus.so` (cp310 GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`); the BIR instruction handles live in `libBIR.so` (build-id `a9b1ea38…`). For `.text`/`.rodata`, virtual address equals file offset; `.data` carries a `+0x400000` delta and is not touched here. Treat every address as version-pinned.*

## Abstract

A Tonga compute instruction names its operands with **access-pattern descriptors** — the 4+4N-byte wire structs that encode "start here, step by these strides, this many elements per dimension." [2.3](tensor-descriptors.md) covers the **SRC** family (`TENSOR1D/2D/3D/4D`). This page covers its **byte-twin**: the **DST** family `MEM_PATTERN2D` (12 B) / `MEM_PATTERN3D` (16 B) / `MEM_PATTERN4D` (20 B), the descriptor a matmul (or activation, tensor-tensor, quantize…) writes its output through, and the role through which a PSUM accumulator slot is addressed.

The headline is that **`MEM_PATTERN` and `TENSOR` are the same bytes**. The on-wire layout is byte-identical, the packer is literally the same function, and the `ADDR4` start word is identical. There is **no PSUM-specific field added to the descriptor struct** — no bank field, no zero-region field, no accumulate field. The DST-vs-SRC distinction is entirely a matter of **role**, expressed in the binary in three non-byte places: the C++ wire-struct **type name**, a codegen **dispatcher assert** ("union but is missing field" for DST vs "static tensor pattern but has indirect field" for SRC), and the **validator role-flag args** (`WR=1/PSUM=1/SBUF=0` for a PSUM dst vs `WR=0/PSUM=0/SBUF=1` for an SBUF src — the *same* validator body, opposite flags).

The PSUM-specific information a matmul needs is **split**. The destination *bank* rides in the high bits of the `ADDR4` start word (the address lands in the PSUM window; see [2.2](addr4.md)). The *zero / accumulate / drain* control rides on **sibling bundle bytes** of the 64-byte compute instruction — `accumulate` at bundle `+0x2B`, `psum_zero_region` at `+0x2F`, and the dst dtype at a **per-generation** offset (dense matmul `+0x23`, MX matmul `+0x28`; see the CORRECTION in §6) — *not* on the descriptor. The descriptor at `INST_UNION +0x30` says **where** (bank + strides + nums); the `+0x2B`/`+0x2F` bytes say **how** (zero / accumulate / drain).

For reimplementation the contract is:

- The 4+4N geometry (identical to [2.3](tensor-descriptors.md)) and the rodata asserts that pin slot size.
- The three-layer DST/SRC fork: wire-type, dispatcher assert, validator flags — none of which touch the bytes.
- The matmul PSUM-dst validator `mm_dst_mem3d_valid_nc_v4`: address **required** in the PSUM window, dst dtype accepted iff 2-byte (`{4,5,6,7}` = bf16) or 4-byte (`{8,9,10,11}` = fp32), all three num words `≠ 0`.
- The `INST_UNION` offset map: ifmap src at `+0x10`, PSUM dst `MEM_PATTERN3D` at `+0x30`, dst dtype byte at `+0x23` (dense, CoreV2/V3) or `+0x28` (MX, CoreV4) — siblings of the `+0x2B`/`+0x2F` accumulate/zero-region bytes. The `is_valid_matmul_regular` decode quoted on this page is the **CoreV4** validator, which reads the dst dtype at `union+0x28`; the dense CoreV2 path reads it at `union+0x23` (§6 CORRECTION).

## At a glance

| | |
|---|---|
| **Wire structs** | `NEURON_ISA_TPB_MEM_PATTERN2D` (12 B) / `MEM_PATTERN3D` (16 B) / `MEM_PATTERN4D` (20 B) |
| **Geometry** | `slot_size(N) = 4 + 4N` — `[ADDR4 u32 @+0][N × i16 stride @+4][N × u16 num @+4+2N]` |
| **Byte-twin of** | `TENSOR2D/3D/4D` ([2.3](tensor-descriptors.md)) — identical bytes, packer, `ADDR4` |
| **Packer (shared)** | `assignStaticPattern<core_v4::TENSOR3D>` @ `0x150c390` (DST has *no* own packer symbol) |
| **DST dispatchers** | `assignAccess<MEM_PATTERN3D>` @ `0x15092b0` (→ vtbl`+0x30`); `<MEM_PATTERN2D>` @ `0x1509b80` (→ vtbl`+0x28`) |
| **3D validators** | `mem3d_valid` @ `0x14467d0` (general); `mm_dst_mem3d_valid_nc_v4` @ `0x1447fe0` (matmul PSUM-dst) |
| **2D / 4D validators** | `mem2d_valid` @ `0x14468c0` ; `mem4d_valid` @ `0x1446550` ; `m4d_not_in_psum` @ `0x14463a0` |
| **Consumer** | `is_valid_matmul_regular` @ `0x14b5c60` ; `is_valid_smx1d3_mm` @ `0x1501ac0` |
| **DST/SRC fork** | wire-type name · dispatcher assert (`0x1d71d18` vs `0x1d71dc8`) · validator flags `WR/PSUM/SBUF` |
| **PSUM window** | `(addr29 − 0x2000000) ≤ 0x3FFFFF` (4 MiB @ 32 MiB); region bits 25..28 = `0x1E000000` |
| **PSUM split** | bank → `ADDR4` hi bits · accumulate → bundle `+0x2B` · zero-region → `+0x2F` · dst dtype → `+0x23` dense / `+0x28` MX (per-gen, §6) |
| **INST_UNION map** | ifmap src `+0x10` · PSUM dst `MEM_PATTERN3D` `+0x30` · dst dtype byte `+0x23` (dense CoreV2/V3) / `+0x28` (MX CoreV4) |
| **Evidence** | full `objdump -M intel` disasm of the v4 validator/dispatcher bodies + `nm -DC` symtab |

---

## 1. The 4+4N geometry — byte-identical to the SRC twin

A `MEM_PATTERN` descriptor is the same shape as a `TENSOR` descriptor: one 32-bit `ADDR4` start word, then a contiguous array of signed `i16` **strides**, then a contiguous array of unsigned `u16` **nums** (element counts). The strides and nums are *separate* arrays — all strides, then all nums — not interleaved.

```text
MEM_PATTERN2D (12 B):  [addr32 @+0][stride0 @+4][stride1 @+6][num0 @+8 ][num1 @+0xA]
MEM_PATTERN3D (16 B):  [addr32 @+0][stride0 @+4][stride1 @+6][stride2 @+8]
                                   [num0  @+0xA][num1   @+0xC][num2   @+0xE]
MEM_PATTERN4D (20 B):  [addr32 @+0][stride0 @+4][stride1 @+6][stride2 @+8][stride3 @+0xA]
                                   [num0  @+0xC][num1   @+0xE][num2   @+0x10][num3 @+0x12]
```

| Field | Type | Range | Notes |
|---|---|---|---|
| `addr32` @+0 | `ADDR4` u32 | 29-bit byte addr + mode bits | partition `Pattern[0]=W` folds in here; carries the PSUM bank ([2.2](addr4.md)) |
| `strideᵢ` | **signed** `i16` | `[-32768, 32767]` | **ELEMENT** units (not bytes); two's complement |
| `numᵢ` | **unsigned** `u16` | `[1, 65535]` | `0` is illegal — the SBUF decoder rejects it |

> **NOTE — unused free dims are `{stride=1, num=1}`-filled, never zeroed.** A descriptor template wider than the access pattern's active dimension count fills the slack with the `0x00010001` immediate (`stride=1`, `num=1`), so a degenerate dimension iterates exactly once rather than zero times. A `num=0` word reads as a decode error, not a no-op. (CONFIRMED — [2.3](tensor-descriptors.md); the unit-fill is emitted by the shared packer, §3.)

The slot size is pinned by four rodata assert strings that the DST dispatcher loads verbatim — the **same** strings the SRC dispatcher uses, because the two roles share the descriptor:

| rodata @ | String | Implies |
|---|---|---|
| `0x1d6e810` | `"ISA mem pattern 1D must have 8 bytes to encode"` | `…1D` = 8 B |
| `0x1d6e8b0` | `"ISA mem pattern 2D must have 12 bytes to encode"` | `MEM_PATTERN2D` = 12 B |
| `0x1d6e920` | `"ISA mem pattern 3D must have 16 bytes to encode"` | `MEM_PATTERN3D` = 16 B |
| `0x1d6e990` | `"ISA mem pattern 4D must have 20 bytes to encode"` | `MEM_PATTERN4D` = 20 B |

⇒ `slot_size(N) = 4 + 4N`. `assignAccess<MEM_PATTERN3D>` @ `0x15092b0` loads the 3D string with `lea rsi,[rip+…] = 0x1d6e920` at `0x1509551` — the identical string the `TENSOR3D` dispatcher cites. (CONFIRMED byte-exact; the "mem pattern" wording even appears in the *TENSOR* asserts because the string table is shared, which is itself the byte-twin proof.)

> **QUIRK — the assert string literally says "mem pattern" for both roles.** [2.3](tensor-descriptors.md) attributes `"…12 bytes to encode"` to `TENSOR2D`; this page attributes it to `MEM_PATTERN2D`. Both are correct: there is one assert string and both dispatchers load it, because at the byte level the two descriptors are one struct. The string is named after the `MEM_PATTERN` (DST) type; the `TENSOR` (SRC) dispatcher borrows it.

---

## 2. The MEM_PATTERN-vs-TENSOR diff — three layers, zero byte difference

The DST role is distinguished from the SRC role in three places, **none** of which is a byte in the descriptor.

### 2.1 Layer 1 — the wire-struct type name

`nm -DC` demangles distinct C++ types for the same byte width. The DST validators and dispatchers take/produce `NEURON_ISA_TPB_MEM_PATTERN{2,3,4}D`; the SRC ones take/produce `NEURON_ISA_TPB_TENSOR{2,3,4}D`:

| Symbol | @ | Operand type |
|---|---|---|
| `core_v4::mem2d_valid(MEM_PATTERN2D, …)` | `0x14468c0` | DST/general 2D |
| `core_v4::mem3d_valid(MEM_PATTERN3D, …)` | `0x14467d0` | DST/general 3D |
| `core_v4::mem4d_valid(MEM_PATTERN4D, …)` | `0x1446550` | DST/general 4D |
| `mm_dst_mem3d_valid_nc_v4(MEM_PATTERN3D, DTYPE)` | `0x1447fe0` | matmul PSUM-dst 3D |
| `tt_valid_src_rams_3d(MEM_PATTERN3D, MEM_PATTERN3D)` | `0x1448ae0` | TensorTensor src-pair |
| `assignAccess<MEM_PATTERN3D>` / `<MEM_PATTERN2D>` | `0x15092b0` / `0x1509b80` | DST dispatchers |
| `assignAccess<TENSOR3D>` | `0x150a450` | SRC dispatcher |

The byte **width** is the same (12/16/20); the C++ type is nominally distinct so the op-encoder can route each operand to the right validator by role. (CONFIRMED — symbol demangling.)

> **CORRECTION (refines [2.3](tensor-descriptors.md) §5) — `mem2d_valid` / `mem3d_valid` are NOT "plain-tensor SRC" validators.** Their declared input type is `MEM_PATTERN2D/3D`, the DST/general mem-pattern type. They serve src operands *too* (called with `WRITE_TENSOR=0`), but the type attribution is `MEM_PATTERN`, not `TENSOR`. The byte-decode in [2.3](tensor-descriptors.md) stands unchanged; only the type label is corrected here.

### 2.2 Layer 2 — the dispatcher assert (the role fork at codegen time)

`assignAccess<MEM_PATTERN3D>` @ `0x15092b0` opens with an assert string at `0x1d71d18`:

```text
"ISA mem pattern is union but is missing field"
```

`assignAccess<TENSOR3D>` @ `0x150a450` opens with a *different* string (region `0x1d71dc8`):

```text
"… is static tensor pattern but has indirect field"
```

The semantics: the **DST** (`MEM_PATTERN`) is a *union member* of the 64-byte `INST_UNION` compute bundle — its dispatcher asserts the union's field-presence tag is set (the dst slot is present). The **SRC** (`TENSOR`) is a standalone static read pattern — its dispatcher asserts the access pattern is **not** indirect. This is the cleanest in-binary expression of the operand-role split. (CONFIRMED — string reads: `0x1d71d18` for `MEM_PATTERN3D`; `0x1d71d58` the same wording for `MEM_PATTERN2D`.) Both dispatchers then run the **same** "must be SB or PSUM" location assert (`isLocationSB` @ `0x62a2c0` `||` `isLocationPSUM` @ `0x606230`) and route through the same vtable slot.

> **CORRECTION (refines the encoder/dispatcher account in [2.3](tensor-descriptors.md)) — the DST dispatcher's first assert is the *union* check, not the *not-indirect* check.** An earlier reading attributed the "static tensor pattern but has indirect field" assert to the DST path. That assert belongs to the **SRC** (`TENSOR`) dispatcher (`0x1d71dc8`). The DST (`MEM_PATTERN`) dispatcher runs the union field-presence assert (`0x1d71d18`) instead. Both then share the SB‖PSUM assert and the vtable route. (CONFIRMED — string reads this pass.)

### 2.3 Layer 3 — the validator role-flag args (the runtime fork)

`mem3d_valid`'s full signature is `(MEM_PATTERN3D a1, DTYPE, WRITE_TENSOR, ALLOWED_IN_PSUM, ALLOWED_IN_SBUF)`. The **same body** is called twice with opposite flags inside `is_valid_matmul_regular` @ `0x14b5c60`:

| Operand | flags | dtype | @ |
|---|---|---|---|
| **DST** (PSUM) | `WR=1` / `PSUM=1` / `SBUF=0` | `0xa` (fp32, forced) | `0x14b5fa8` |
| **SRC** (ifmap) | `WR=0` / `PSUM=0` / `SBUF=1` | actual ifmap dtype | `0x14b6085` |

The validator is one function; the role is the flag vector. (CONFIRMED — both call sites this pass.)

---

## 3. The encoder — MEM_PATTERN reuses the TENSOR packer

There is **no** `assignStaticPattern<…MEM_PATTERN3D>` symbol anywhere in the dynsym. The DST dispatcher tail-calls the SRC packer through its vtable:

```text
; assignAccess<MEM_PATTERN3D> @0x15092b0 — tail
0x15095d3  mov  rax, QWORD PTR [rax+0x30]   ; vtbl[+0x30] = CoreV4 assignAccess3D
0x15095e5  jmp  rax                          ; → 0x150ccf0 → assignStaticPattern<core_v4::TENSOR3D> @0x150c390

; assignAccess<MEM_PATTERN2D> @0x1509b80 — tail
           mov  rax, QWORD PTR [rax+0x28]    ; vtbl[+0x28] = assignAccess2D
           jmp  rax                          ; → assignStaticPattern<core_v4::TENSOR2D> @0x150b7e0
```

The vtable slots are **identical** to the `TENSOR` family (3D → `+0x30`, 2D → `+0x28`). So the DST descriptor is packed by the *exact same byte-math* as the SRC ([2.3](tensor-descriptors.md)):

```c
// shared packer: assignStaticPattern<core_v4::TENSOR3D> @0x150c390
// (reached identically from the MEM_PATTERN3D DST dispatcher and the TENSOR3D SRC dispatcher)
void pack_static_pattern_3d(u8 *slot /*16 B*/, AccessPattern *ap) {
    // partition dim Pattern[0]=W is NOT a word here — it folds into the ADDR4 start
    assignStartAddr_ADDR4(slot + 0x0, ap);          // ADDR4 @+0: 29-bit byte addr (PSUM bank in hi bits)
    for (int i = 0; i < 3; i++)                      // 3 signed-i16 strides @+4,+6,+8
        write_i16(slot + 0x4 + 2*i, ap->stride[i+1]); // sub_116ca30
    for (int i = 0; i < 3; i++)                      // 3 unsigned-u16 nums @+0xA,+0xC,+0xE
        write_u16(slot + 0xA + 2*i, ap->num[i+1]);    // sub_116d620
    // unused dims pre-filled {stride=1, num=1} (0x00010001) by the AP normalizer, never 0
}
```

The shared `assignAccess3D` leaf (`0x150ccf0`) additionally carries an inline indirect/gather arm (two `ADDR4` words: data base @+0, index @+4, stamping `desc[+3] |= 0x20`) reached for an *indirect* dst/src; the plain static DST path skips it. SRC and DST differ **only** in the wire-struct type and the validator they bind — not in a single packed byte. (CONFIRMED — vtable-slot loads + absence of any `MEM_PATTERN` packer symbol.)

---

## 4. The PSUM-specific fields — where bank / zero-region / accumulate actually live

This is the answer to "where are the PSUM fields?" — they are **not** in the descriptor. The `MEM_PATTERN` struct has no bank field, no zero-region field, no accumulate field. The information rides elsewhere.

### 4.1 Bank → inside the `ADDR4` @+0 word

The PSUM dst's bank is encoded in the high bits of the 29-bit byte address. The validator detects "this is a PSUM address" by a window test:

```text
addr29 = addr & 0x1FFFFFFF
if ((addr29 − 0x2000000) <= 0x3FFFFF)  // → in the 4 MiB PSUM window at 32 MiB
    region bits 25..28 (mask 0x1E000000) must be nonzero
```

The encoder folds the bank into the address upstream as `(part<<15) + (bank<<11) + inPartAddr` — the bank stride is `2048 B = 0x800`, i.e. `bank<<11`, matching the constant PSUM bank size (`getPsumBankSize = 2048`, [1.05](../arch/sbuf-psum-geometry.md)). The descriptor carries the bank *only* as part of its start address; the packer writes no separate bank field. (CONFIRMED — window math [2.2](addr4.md); packer-has-no-field §3.)

### 4.2 Zero-region + start/stop/accumulate → sibling bundle bytes

The zero/accumulate/drain control lives in **separate PE-instruction bundle bytes**, not descriptor bytes:

| Bundle offset | Field | Bit assignment / value |
|---|---|---|
| `+0x2B` | `accumulate` byte | bit0 `START` (zero-bank), bit1 `STOP` (drain), bit2 `ACCUMULATE` (add) |
| `+0x2F` | `psum_zero_region` | `ceil(log2 bankspan)` — pow-2 window ≤ 16 banks (inst `+0x118`) |
| `+0x23` / `+0x28` | PSUM-dst dtype | LUT[BIR Dtype]; selects the dst validator path (§5.3). **Per-generation**: dense matmul (op `0x02`, CoreV2/V3) at `+0x23`; MX matmul (op `0x100A`, CoreV4) at `+0x28`. See §6 CORRECTION. |

The first matmul of an accumulation group sets `START` (zeroes the bank on write), the last sets `STOP` (drains/reads out), interior ones set `ACCUMULATE`. These bits are set **upstream** (the accumulate-group legalizer: `set_psum_accumulate_flag` @ `0x16e1410`, `set_psum_zero_region` @ `0x16daff0`) and read by codegen (`generateMatMulAccumulateFlag` @ `0x1428630` → bundle `+0x2B`; the MX path copies raw `inst+0xF0 → +0x2B`, `inst+0x118 → +0x2F`). The wire bit-OR is byte-exact:

```text
0x124963d  or  BYTE PTR [r15+0x2b], 0x1   ; START
0x1249664  or  BYTE PTR [r15+0x2b], 0x2   ; STOP
           or  BYTE PTR [r15+0x2b], 0x4   ; ACCUMULATE
```

(CONFIRMED — cross-checked against [2.9](neuron-isa-tpb-capstone.md) / the PE-ISA accumulate cluster.)

> **GOTCHA — the descriptor and the accumulate/zero-region bytes are *siblings*, not parent and child.** The `MEM_PATTERN3D` dst slot at `INST_UNION +0x30` and the accumulate/zero-region bytes at `+0x2B`/`+0x2F` are different fields of the same 64-byte bundle. A reimplementer who looks for a "zero region" or "accumulate" field *inside* the 16-byte descriptor will not find one. The descriptor says **where** (bank + strides + nums); the `+0x2B`/`+0x2F` bytes say **how** (zero / accumulate / drain).

### 4.3 PSUM dtype widening — why the dst is always validated as 4-byte

The PSUM accumulator is physically fp32 (4-byte cells). `psum_legalization` widens any sub-4-byte matmul dst dtype to fp32 (except bf16 / transpose) and ×ratio-expands the free extent **before** codegen, so by the time the `MEM_PATTERN3D` is packed the dst dtype is already a legal accumulator type. That is why the general matmul-dst path hardwires the address-alignment dtype to `9` (uint32, 4-byte align) regardless of the declared dst dtype (§5.1). (CONFIRMED — alignment hardwire §5.)

---

## 5. The DST validators — decode + the matmul-specific path

### 5.1 `mem3d_valid` @ `0x14467d0` — the general 3D mem-pattern decoder

Signature `(MEM_PATTERN3D a1 [16 B in rdi:rsi], DTYPE, WRITE_TENSOR, ALLOWED_IN_PSUM, ALLOWED_IN_SBUF)`. `ebx = byte3` (the high byte of the `ADDR4` word).

```c
// mem3d_valid @0x14467d0 — CONFIRMED full body
bool mem3d_valid(MEM_PATTERN3D *d, DTYPE dt, int WR, int PSUM, int SBUF) {
    int region = byte3(d->addr) & 0x60;
    if (region == 0x20) {                                  // PSUM-dst / indirect-marked path
        if (read_word(d, 0x8) == 0)        return false;   // stride2 word @+8 must be ≠0 (cmpw)
        if (!tensor_start_addr_valid(d->addr, /*dt=*/9, /*WR=*/1, /*PSUM=*/1, /*SBUF=*/0))
                                           return false;   // ADDR4 valid as a 4-byte PSUM slot
        if (byte(d, 0x7) & 0xe0)           return false;   // stride1 high 3 bits must be clear
        return tensor_start_addr_valid(read_word(d, 0x4), dt, WR, PSUM, SBUF); // 2nd ADDR4-style word @+4
    } else {                                               // SBUF path
        if (!tensor_start_addr_valid(d->addr, dt, WR, PSUM, SBUF)) return false;
        if ((byte3 & 0x70) == 0x40)        return false;   // SB sub-region reject
        return read_word(d,0xA) && read_word(d,0xC) && read_word(d,0xE); // num0/1/2 all ≠0
    }
}
```

> **NOTE — the PSUM path hardwires `dtype = 9` for the address-validity call** (`0x1446818: mov esi,0x9`). A PSUM-dst address is always validated as a 4-byte (uint32) accumulator slot, independent of the operand's declared dtype — the consequence of the fp32 widening in §4.3.

### 5.2 `mm_dst_mem3d_valid_nc_v4` @ `0x1447fe0` — the matmul PSUM-dst validator

This is the DST-specific path. `"nc"` reads as *no-convert* (the bf16-or-fp32 PSUM-dst form). Signature `(MEM_PATTERN3D [rdi:rsi], DTYPE [edx])` — only two args.

```c
// mm_dst_mem3d_valid_nc_v4 @0x1447fe0 — CONFIRMED full body 0x1447fe0..0x144814c
bool mm_dst_mem3d_valid_nc_v4(MEM_PATTERN3D *d, DTYPE dt) {
    int region = byte3(d->addr) & 0x60;
    if (region == 0x20) {                                   // indirect / secondary-window dst
        if (read_word(d, 0xA) == 0) return false;           // num0 word must be present (test si,si)
        // … validates the @+4 secondary word as a PSUM-window addr, stride1 hi clear,
        //     accepts iff helper{8..11}(dt) || helper{4..7}(dt)  (dt ∈ 4..11)
    } else {                                                // static dst path (byte3 & 0x60 == 0)
        if ((byte3 & 0xA0) == 0x80) {                       // register-mode (bit31) addr
            // require RegId < 0x40 (cmp byte0, 0x3F)        ([2.2](addr4.md) §6)
        } else if (byte3 sign bit set) {
            return false;                                   // bit31 without reg form
        } else {                                            // plain physical addr
            u32 addr29 = d->addr & 0x1FFFFFFF;
            if (!addr_aligned_dtype(addr29, dt))         return false;
            if ((addr29 − 0x2000000) > 0x3FFFFF)         return false;  // ⭐ MUST be in PSUM window
            if (!(helper_4byte_8_11(dt) || helper_2byte_4_7(dt))) return false; // dt ∈ {4..11}
        }
        if ((byte3 & 0x70) == 0x40)                      return false;  // SB sub-region reject
        if (!(read_word(d,0xA) && read_word(d,0xC) && read_word(d,0xE))) return false; // 3 nums ≠0
    }
    return true;
}
```

The DST-specific semantics, versus the general `mem3d_valid` (§5.1):

- **The address MUST be in the PSUM window** — `(addr29 − 0x2000000) ≤ 0x3FFFFF` is a *requirement*, not a branch. A matmul dst is always PSUM. `mem3d_valid` lets SBUF through; `mm_dst_…_nc_v4` does not.
- **It accepts the dst dtype iff it is 4-byte `{8,9,10,11}` OR 2-byte `{4,5,6,7}`** — fp32 *or* bf16. This is the wire-byte encoding of "matmul may write BF16 in addition to a 4-byte-aligned type." The `"nc"` form is reached precisely when the dst dtype byte (`INST_UNION +0x28`) equals `6` (bf16) — see §5.3.
- It re-checks all three num words `≠ 0` (the dst must address ≥1 element per dimension).

(CONFIRMED — full disasm `0x1447fe0..0x144814c`; dtype helpers `0x142de60` `{8..11}` / `0x142de70` `{4..7}`; the window `sub 0x2000000` / `cmp 0x3FFFFF` at `0x1448053` / `0x144805a`.)

### 5.3 Who picks `mm_dst` vs the plain dst path — `is_valid_matmul_regular`

The decode below is `core_v4::is_valid_matmul_regular @0x14b5c60` (the **CoreV4/MX** validator — symbol confirmed in `nm -DC`). The 64-byte `INST_UNION` arrives by value at `[rsp+0x130]` (`= union+0`). The consumer decodes the slots and validates each by role:

```text
[rsp+0x140] = union+0x10  → moving/ifmap SRC slot
[rsp+0x160] = union+0x30  → PSUM-DST MEM_PATTERN3D slot       (cached → [rsp+0xe0])
byte[rsp+0x158] = union+0x28 → PSUM-DST DTYPE byte (CoreV4/MX) (cached → [rsp+0x12])
```

> **NOTE — `+0x28` is the CoreV4/MX dst-dtype offset; dense CoreV2/V3 uses `+0x23`.** The validator decoded here is `core_v4::is_valid_matmul_regular`, which reads the dst dtype at `union+0x28` — matching the CoreV4 `generateMatmultMx` encoder (`mov [r13+0x28],al @0x143eded`). The **dense** CoreV2 path is a different struct generation: `core_v2::is_valid_matmul_regular @0x12de340` reads the dst dtype at `union+0x23` (`movzx ...,[rsp+0xe3]`), matching `generateMatMul`'s `mov [r15+0x23],al @0x1248882`. Both `==6` → the bf16 `nc` path. The offset is per-generation, not universal (§6 CORRECTION, [2.10](pe-matmul-encoding.md)).

| Check | Call | @ |
|---|---|---|
| **SRC** ifmap (read, SBUF) | `mem3d_valid(union+0x10, dt=union+0x14, WR=0, PSUM=0, SBUF=1)` | `0x14b6085` |
| **DST** PSUM (write, fp32 forced) | `mem3d_valid(union+0x30, dt=0xa, WR=1, PSUM=1, SBUF=0)` | `0x14b5fa8` |
| **DST** bf16 special | if dst-dtype byte `== 6` → `mm_dst_mem3d_valid_nc_v4(…, dt=6)` | `0x14b6531` / `0x14b654b` |
| **DST** fp32r 2-FMA | `check_mm_fp32r_dst_mem_pattern(TENSOR3D dst, dt)` | `0x1449ff0` |

The sparse-MX matmul `is_valid_smx1d3_mm` @ `0x1501ac0` likewise calls `mm_dst_mem3d_valid_nc_v4(…, dt=6)` for its bf16 PSUM dst (`0x1501739`). (CONFIRMED — call roster + arg setup; `union+0x28` / `union+0x30` match [2.9](neuron-isa-tpb-capstone.md).)

> **QUIRK — `check_mm_fp32r_dst_mem_pattern` is declared as `TENSOR3D`, not `MEM_PATTERN3D`.** The fp32-round 2-FMA dst pattern validator takes the *SRC* type name even though it validates a destination. Because the two types are byte-identical (§1–3), the type label is interchangeable at the call site; only the *validator selection* matters, not the nominal type. This is a reminder that the type name is a routing hint, not a layout difference.

### 5.4 `mem2d_valid` @ `0x14468c0` (12-B) and `mem4d_valid` @ `0x1446550` (20-B)

`mem2d_valid`: 12 B = `rdi` (bytes +0..7) + `esi` (bytes +8..0xB). PSUM path: `tensor_start_addr_valid(addr@+0, dt=9, WR=1, PSUM=1, SBUF=0)`; `testb [stride1 hi], 0xe0`; tail-validates the `@+4` word. SBUF path: `addr@+0` valid; `(byte3 & 0x70) != 0x40`; `num0@+8 ≠0 ∧ num1@+0xA ≠0`. (CONFIRMED full body — `0x1446998`/`0x14469a1`.)

`mem4d_valid`: byte3 read at `[rsp+0x33]`; PSUM stride check at `[rsp+0x38]`; nums at `[rsp+0x3c/+0x3e/+0x40/+0x42]` = `num0..3 @+0xC/+0xE/+0x10/+0x12` — confirms the 20-B layout. `m4d_not_in_psum` @ `0x14463a0` is a 4D dst that asserts its secondary/indirect word is **not** in the PSUM window — a DST-PSUM-exclusion guard for the 4D ops. (CONFIRMED head + offsets.)

---

## 6. INST_UNION offset map — where the DST descriptor sits

The matmul compute bundle is a 64-byte `INST_UNION`. The DST `MEM_PATTERN3D` is a *union member* of it (§2.2), at `+0x30`. The full map for the matmul-regular bundle, cross-checked against [2.9](neuron-isa-tpb-capstone.md) and the PE-ISA layout:

| Offset | Field | Type / size | Role | Confidence |
|---|---|---|---|---|
| `+0x00` | *(bundle header / opcode)* | — | instruction word | CONFIRMED |
| `+0x10` | **ifmap (moving) SRC** | `TENSOR3D` (16 B) | read, SBUF-resident | CONFIRMED |
| `+0x14` | ifmap dtype | byte | SRC dtype | CONFIRMED |
| `+0x23` | **PSUM-DST dtype (dense)** | byte | dense matmul op `0x02`, CoreV2/V3; selects dst validator (`==6` → `nc` path) | CONFIRMED |
| `+0x28` | **PSUM-DST dtype (MX)** | byte | MX matmul op `0x100A`, CoreV4; selects dst validator (`==6` → `nc` path) | CONFIRMED |
| `+0x2B` | **accumulate** | byte | b0 START / b1 STOP / b2 ACCUMULATE | CONFIRMED |
| `+0x2F` | **psum_zero_region** | byte | `ceil(log2 bankspan)`, pow-2 ≤16 banks | CONFIRMED |
| `+0x30` | **PSUM-DST descriptor** | `MEM_PATTERN3D` (16 B) | write, PSUM-resident; bank in `ADDR4` hi | CONFIRMED |

The dst descriptor (`+0x30`) and the accumulate/zero-region/dtype bytes (dst-dtype `+0x23`-dense/`+0x28`-MX, `+0x2B`, `+0x2F`) are **siblings** in the bundle — together they fully describe the PSUM write: the descriptor the *address pattern* (incl. bank), the sibling bytes the *accumulate contract* (incl. dtype). (CONFIRMED — `core_v4::is_valid_matmul_regular` slot decode `0x14b5cdb`/`0x14b5d9a`/`0x14b5db3` + PE-ISA cross-ref.)

> **CORRECTION — the matmul dst-dtype offset is PER-GENERATION, not a fixed `+0x28`.** Earlier revisions of this page stated the matmul PSUM-dst dtype byte is at `INST_UNION +0x28` throughout. Binary re-disassembly this pass shows that is the **CoreV4 MX** offset only. The **dense** matmul (op `0x02`, CoreV2/V3) writes the dst dtype at `+0x23` (`generateMatMul`: `mov [r15+0x23],al @0x1248882`; no `+0x28` store exists in the dense encoder — that byte is pxor-zero) and reads it back at `union+0x23` (`core_v2::is_valid_matmul_regular`: `[rsp+0xe3]`). The **MX** matmul (op `0x100A`, CoreV4) writes/reads it at `+0x28` (`generateMatmultMx @0x143eded`; `core_v4::is_valid_matmul_regular`: `[rsp+0x158]`). The `is_valid_matmul_regular` decode quoted in §5.3 is the CoreV4 validator, so its `union+0x28` reading is correct for the MX struct; do **not** generalize it to the dense bundle. Both offsets feed `==6` → the bf16 `nc` path. [2.10 §Matmul CORRECTION CONFIRMED.]

> **NOTE — the moving SRC at `+0x10` is a `TENSOR3D`, the dst at `+0x30` is a `MEM_PATTERN3D`.** This is the operand-role convention made concrete: `getArgument(i)` (a read) → `assignAccess<TENSORnD>`; `getOutput(0)` (a write) → `assignAccess<MEM_PATTERN_nD>`. The union tag tells the verifier which member is present; the member's *type* routes it to the read-role or write-role validators.

---

## 7. 2D vs 3D vs 4D — when each DST form is emitted

The descriptor dim-count is the *type the op-encoder picks for an operand* (a compile-time template choice); the access pattern's active free-dim count is then range-checked `≤ N+1`. Which v4 ops emit which DST form — by the enclosing function of each validator's call sites:

| DST form | Validator | Producers |
|---|---|---|
| **`MEM_PATTERN3D`** (workhorse) | `mem3d_valid` / `mm_dst_…_nc_v4` | matmul (`is_valid_matmul_regular`), sparse-MX matmul (`is_valid_smx1d3_mm`), TensorTensor, Activation (`is_valid_activate`), TensorScalar, dequantize/quant, LoadWeights stationary tensor |
| **`MEM_PATTERN2D`** | `mem2d_valid` | conv LUT load (`is_valid_conv_lut_load`), Activation v2 (`is_valid_activate2`) |
| **`MEM_PATTERN4D`** | `mem4d_valid` / `m4d_not_in_psum` | 4-D BN / TransposeReduce / sparse-matmul ifmap / `PARAM_LOAD` |

⇒ **3D is the workhorse DST form** (matmul PSUM dst, activation, TT, TS, quant); 2D is for the few 2-axis dsts; 4D for the 4-free-dim ops. **The matmul PSUM dst is always `MEM_PATTERN3D` at bundle `+0x30`.** (CONFIRMED — `nm -DC` enclosing-function attribution of the validator call sites.)

---

## 8. Encoder ↔ decoder byte agreement (DST path)

Every offset the shared packer (`assignStaticPattern<TENSOR3D>`) writes matches an offset a DST decoder reads. The table is verified both ways:

| Field | Encoder writes | Decoder reads |
|---|---|---|
| `ADDR4` @+0 | `assignStartAddr<ADDR4>` (bank in hi bits) | `tensor_start_addr_valid` (dt=9 forced PSUM; window `addr−0x2000000 ≤ 0x3FFFFF`) |
| `stride0` @+4 | `sub_116ca30` i16 | 2nd `ADDR4`-style PSUM word |
| `stride1` @+6 | `sub_116ca30` i16 | byte+7 hi (`testb 0xe0`) PSUM bound |
| `stride2` @+8 | `sub_116ca30` i16 | mem3d PSUM: `cmpw +8 ≠0` |
| `num0` @+0xA | `sub_116d620` u16 | `mm_dst`: `test num0 ≠0`; mem3d SBUF: `cmpw ≠0` |
| `num1` @+0xC | `sub_116d620` u16 | `cmpw +0xC ≠0` |
| `num2` @+0xE | `sub_116d620` u16 | `cmpw +0xE ≠0` |
| partition | folded → `ADDR4` band | addr band bits 25..28 / window |
| reg-mode | `or byte+3, 0x80` | `(byte3 & 0xA0) == 0x80 → RegId < 0x40` |
| **PSUM bank** | *(no field)* — `ADDR4` hi bits | window test (not decoded as a field) |
| **accumulate** | *(NOT in descriptor — bundle `+0x2B`)* | set upstream / read by codegen |
| **zero-region** | *(NOT in descriptor — bundle `+0x2F`)* | set upstream / read by codegen |
| dim count | *(implicit type)* + active `≤ N+1` | *(none on wire)* |

⇒ Every descriptor write offset matches a DST-decoder read offset (2D via `mem2d_valid`, 3D via `mem3d_valid` + `mm_dst_…_nc_v4`, 4D via `mem4d_valid`). The PSUM bank / accumulate / zero-region are confirmed **absent** from the descriptor — no packer field; the decoder sees them only as `ADDR4`-window bits and the sibling `+0x2B`/`+0x2F` bundle bytes. (CONFIRMED 2D/3D/4D, both ways.)

---

## 9. Confidence ledger

**CONFIRMED** (full disasm of the v4 bodies + cross-check):

- `MEM_PATTERN2D/3D/4D` = byte-identical to `TENSOR2D/3D/4D` (12/16/20 B; shared "must have K bytes" asserts; no separate packer symbol; same vtbl slots `+0x28`/`+0x30`).
- `mem3d_valid` / `mem2d_valid` / `mem4d_valid` full byte decode.
- `mm_dst_mem3d_valid_nc_v4`: matmul PSUM-dst validator; addr **required** in PSUM window; dst dtype `{4..11}` (bf16 OR fp32); 3 nums `≠0`; reached when the dst dtype byte `== 6` (read at `union+0x28` by the CoreV4/MX validator; the dense CoreV2 validator reads `union+0x23` — §6 CORRECTION).
- DST role fork: dispatcher assert (`"union but is missing field"`, `0x1d71d18`), validator flags (`WR=1/PSUM=1/SBUF=0` dst vs `WR=0/PSUM=0/SBUF=1` src), forced fp32 dtype on dst.
- PSUM bank in `ADDR4` hi bits; accumulate `+0x2B` / zero-region `+0x2F` / dst dtype (`+0x23` dense, `+0x28` MX — per-generation, §6) are sibling bundle bytes, NOT descriptor fields.
- `INST_UNION` map: src `+0x10`, dst `MEM_PATTERN3D` `+0x30`, dst dtype byte `+0x23` (dense CoreV2/V3) / `+0x28` (MX CoreV4).
- Encoder↔decoder byte agreement (2D/3D/4D, both ways).

**STRONG (INFERRED):**

- v2/v3 `MEM_PATTERN` byte-identity — `core_v2::mem4d_valid` @ `0x127f660`, `core_v3::mem4d_valid` @ `0x136ee40` exist by symbol; the v2/v3 dst bodies were not re-disassembled, so byte-identity is inferred from the arch-invariant encoder and the shared asserts.
- `"nc"` = *no-convert* / bf16-aware naming — inferred from the `{4..7} ∪ {8..11}` dtype acceptance + the bf16 PSUM-legality exception; there is no literal `"nc"` string.

**INFERRED:**

- The exact bit decomposition of the bank *within* the `ADDR4` window (bits 26..28 carry part/bank overflow; the validator tests only the aggregate `0x1E000000`) — see [2.2](addr4.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `TENSOR2D/3D/4D` ([2.3](tensor-descriptors.md)) | the SRC byte-twin — same bytes, same packer, distinct only by type/role |
| `ADDR4` ([2.2](addr4.md)) | the @+0 start word that carries the 29-bit addr, the PSUM-window bit, and the bank |
| `assignStaticPattern<TENSOR3D>` (`0x150c390`) | the shared packer both roles tail-call through their vtable |
| `INST_UNION` ([2.9](neuron-isa-tpb-capstone.md)) | the 64-byte compute bundle the DST descriptor is a union member of |
| PSUM banks ([1.05](../arch/sbuf-psum-geometry.md)) | the 2048-B fixed banks the DST address indexes into |

## Cross-References

- [2.3 TENSOR Descriptors](tensor-descriptors.md) — the SRC twin: same 4+4N bytes, same packer; this page is the DST role of that struct.
- [2.2 ADDR4](addr4.md) — the @+0 start word; the PSUM-window test `(addr − 0x2000000) ≤ 0x3FFFFF`, region bits `0x1E000000`, and the `RegId < 0x40` register-mode form.
- [2.9 NEURON_ISA_TPB INST_UNION](neuron-isa-tpb-capstone.md) — the 64-byte compute-bundle header; the DST descriptor sits at union member `+0x30`, dst dtype at `+0x23` (dense) / `+0x28` (MX), accumulate at `+0x2B`, zero-region at `+0x2F`.
- [1.05 SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — the 2048-B PSUM banks this DST role addresses, and the 4 MiB PSUM window at 32 MiB.
- [PE Engine](../arch/pe-engine.md) — the matmul that produces the canonical PSUM dst: the moving ifmap (`+0x10`), the PSUM accumulator (`+0x30`), and the two-pass fp32 / accumulate-group machinery.
- [Frontend Pipeline](../front/pipeline.md) — where the access patterns that become these descriptors are built and normalized before codegen.
