# MXMEM_PATTERN1D — MX Data + E8M0 Scale

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 share the C++ core logic, but `libwalrus.so` is rebuilt per-wheel — re-confirm any address against cp311/cp312 before relying on it). The encoder, decoder, and the two address resolvers live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`, MD5 `1d93972b81e619ce6d178a0e4b9003b3`). The simulator's MX dequant lives in `libBIRSimulator.so` (build-id family `a9b1ea38`); the `bir::Dtype` enum lives in `libBIR.so`. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

**MXMEM_PATTERN1D** is the 16-byte on-chip access-pattern descriptor that the gen4 (`CoreV4`, arch 40, *mariana* / Trn3) backend emits to address a **microscaling (MX) operand and its per-block exponent scale together**. Where a plain `TENSOR1D`/`MEM_PATTERN` descriptor (2.3) opens with one [ADDR4](addr4.md) word and then carries loop step/num words, the MX descriptor opens with **two ADDR4 words back-to-back** — one for the packed FP4/FP8 data tensor, one for the SBUF-only E8M0 (`float8_e8m0fnu`) shared-exponent scale stream — plus a packed-element extent, a step-direction sign, and a scale base-partition alignment byte. It is the structural divergence of the MX family from the rank-*n* `MEM_PATTERN` family: one slot, two addresses.

The descriptor exists for exactly one reason: an MX matmul (`MatmultMx`, `LdWeightMx`) or quantize (`QuantizeMx`) consumes a stationary/moving operand whose values are stored in OCP-MXFP form — 32-element blocks, each block sharing one 8-bit E8M0 exponent. Silicon must fetch the packed data *and* the scale byte for each block in lockstep, so the codegen binds the two addresses into one bundle slot. The data word may live in SBUF or PSUM; the scale word is **hard-restricted to SBUF** (`CoreV4Hwm::getStartAddressForMXScale` asserts `memloc.isSB()` with the string *"MX Scale Tensor can only be in SB"*). Both addresses are minted by the shared `assignStartAddr<ADDR4>` leaf — a boolean `a4` argument forks the resolver to the data vtable slot (`+0x20`) or the scale vtable slot (`+0x28`); that fork, not any wire nibble, is "the scale-stream selector."

The bar for this page is that a reader can **encode an MX operand's data+scale addressing by hand** — knowing the byte layout, which 12 of the 16 bytes are written, how the x4-unpacked element count is computed, how the step-sign and scale-partition bytes are validated, and which arch generation accepts the descriptor at all. Everything is recovered from the `libwalrus.so` encoder/decoder/resolver bodies; the byte offsets are pinned against the literal store/load instructions in the disassembly, not inferred. One open question from earlier passes — *"is the descriptor 12 bytes or 16?"* — is settled here against the type's ABI footprint and the validator's per-byte reads: **16-byte type, 12 bytes written, 4 trailing reserved bytes** (zero on the static path, reused by the indirect sibling).

For reimplementation, the contract is:

- **The 16-byte layout** — which byte carries the data ADDR4, the scale ADDR4, the K-extent, the step-sign, and the scale base-partition, and which four bytes are reserved.
- **The dual-ADDR4 fork** — how the same `assignStartAddr<ADDR4>` leaf routes data (`a4=0` → `getStartAddress`, SBUF|PSUM) vs. scale (`a4=1` → `getStartAddressForMXScale`, SBUF-only).
- **The x4-unpack** — when (`Dtype ∈ {2,8,9}`) and how (`n <<= 2`) the K-extent is multiplied by four.
- **The wire validation** — the per-byte checks `mxmem1d_valid` runs, including the data-dtype dual-accept and the indirect-layout discriminator.
- **The CoreV4-only gate** — why this descriptor and all its machinery exist on gen4 alone.

## At a glance

| | |
|---|---|
| **Descriptor type size** | 16 bytes (ABI aggregate, passed `rdi`=qword0 + `rsi`=qword1) |
| **Bytes written (static path)** | 12 (`+0x00`..`+0x0B`); `+0x0C`..`+0x0F` reserved = 0 |
| **Encoder** | `CoreV4GenImpl::assignAccessForMX<core_v4::…MXMEM_PATTERN1D>` @ `0x150e2f0` |
| **Indirect sibling encoder** | `assignIndirectPatternForMX<core_v4::…MXINDIRECT16B>` @ `0x150de90` |
| **ADDR4 leaf** | `Generator::assignStartAddr<core_v4::…ADDR4>(slot, AP, bool a4)` @ `0x1508df0` |
| **Wire validator** | `core_v4::mxmem1d_valid` @ `0x1447190` (plt alias `0x6231e0`) |
| **DATA resolver** | `CoreV4Hwm::getStartAddress` @ `0x185e780` (vtbl `+0x20`, SBUF\|PSUM) |
| **SCALE resolver** | `CoreV4Hwm::getStartAddressForMXScale` @ `0x185fbe0` (vtbl `+0x28`, **SBUF-only**) |
| **Consumers** | `MatmultMx` / `LdWeightMx` (slot `bundle+0x10`); `QuantizeMx` (slot `bundle+0x30`) |
| **Block size** | 32 (OCP-MXFP), per-block E8M0 exponent — *not* a descriptor field |
| **Arch gate** | CoreV4 only (arch 40 = `core_v4` / *mariana* / Trn3); no gen2/gen3 MX symbols |

## The 16-byte layout

The descriptor base `B` is `bundle+0x10` (for `MatmultMx`/`LdWeightMx`) or `bundle+0x30` (for `QuantizeMx`). The bundle frame is `pxor`/`movaps`-zeroed before the encoder runs (`generateMatmultMx 0x143ed0a..`), so any byte the encoder does not write stays zero.

```text
 byte  +0x0 +0x1 +0x2 +0x3 | +0x4 +0x5 +0x6 +0x7 | +0x8 +0x9 | +0xA | +0xB | +0xC ........ +0xF
      +-------------------+--------------------+-----------+------+------+-------------------+
      |  ADDR4  DATA      |  ADDR4  SCALE      |  K-extent | step | sP   |  RESERVED (=0)    |
      |  packed FP4/FP8   |  E8M0 exponent     |  u16      | ±1   | %PE  |  (static path)    |
      +-------------------+--------------------+-----------+------+------+-------------------+
       \____ qword0 (rdi) — data lo32, scale hi32 ___/  \___ qword1 (rsi) — K/step/sP/resv _/

   step ∈ {0x01, 0xFF}  = +1 / -1 contraction-walk sign
   sP   = scaleBasePartition % PE_count(128)   (the data&scale "same quadrant" alignment)
```

| Off | Sz | Field | Encoder write | Validator read | Confidence |
|---|---|---|---|---|---|
| `+0x00` | 4 | **DATA ADDR4** — packed x4 data start addr | `assignStartAddr(&B[0], dataAP, a4=0)` | qword0.lo, DTYPE 9 (or 5 & tag `0x10`) | CONFIRMED |
| `+0x04` | 4 | **SCALE ADDR4** — E8M0 exponent start addr | `assignStartAddr(&B[4], scaleAP, a4=1)` | qword0.hi, DTYPE 3 (E8M0) | CONFIRMED |
| `+0x08` | 2 | **K-extent** (x4-unpacked element count, u16) | `mov [rcx+8], ax` | qword1.word0, asserted ≠ 0 | CONFIRMED |
| `+0x0A` | 1 | **step-dir sign** `{0x01, 0xFF}` | `mov [rbx+0xa], al` | byte10 ∈ {+1,-1} asserted | CONFIRMED |
| `+0x0B` | 1 | **scale base-part** `% PE_count` | `mov [rbx+0xb], dl` | byte11 (≤ `0x0F`, low-2-bits == 0) | CONFIRMED |
| `+0x0C`..`+0x0F` | 4 | **RESERVED** (zero, static path) | (none) | not read (static path) | CONFIRMED |

> **NOTE — the 12-vs-16 reconciliation.** Earlier passes (the MX dual-ADDR4 field layout, the `MatmultMx` bundle map) described a *"12-byte descriptor."* That counted the **meaningful** bytes. The **type** is 16 bytes, proven three ways: (1) `mxmem1d_valid` takes the descriptor by value and the System-V x86-64 ABI passes a 16-byte aggregate in `rdi`+`rsi` (qword0/qword1 — confirmed below); (2) the validator's call-site loads it as one 16-byte XMM, `movdqu xmm4,[rsp+0xe0]` @ `0x14c7ae0`, then `call mxmem1d_valid@plt` @ `0x14c7b1b`; (3) the bundle slot is a 16-byte-aligned region `pxor`-zeroed with the rest of the 64-byte bundle. The encoder writes only `+0x00`..`+0x0B`; `+0x0C`..`+0x0F` are left zero. So both statements are true: **16-byte type, 12 written, 4 reserved.**

### Byte +0x00 — DATA ADDR4 (a4 = 0)

The first 4 bytes are an ADDR4 word minted with `a4 = 0`, routing the address through `CoreV4Hwm::getStartAddress` (vtbl `+0x20`). The data word may therefore be SBUF (`basePart<<18 + inPartAddr`) or PSUM (`basePart<<15 + (!scale)<<25 + bank<<11 + inPartAddr`); see [ADDR4 §PSUM-window detection](addr4.md) (2.2). The internal ADDR4 sub-fields (29-bit byte address, region bits `25..28`, mode nibble `29:30`, register-mode bit `31`) are entirely ADDR4's domain.

```asm
; assignAccessForMX<MXMEM_PATTERN1D> @ 0x150e2f0
0x150e544:  31 c9            xor  ecx, ecx              ; ⭐ a4 = isOut = 0  (DATA)
0x150e54f:  e8 …             call assignStartAddr<ADDR4>(&B[0], dataAP, a4=0)
```

### Byte +0x04 — SCALE ADDR4 (a4 = 1)

The next 4 bytes are a second ADDR4 word, minted with `a4 = 1`, routing the address through `CoreV4Hwm::getStartAddressForMXScale` (vtbl `+0x28`) — the SBUF-only scale resolver (§The E8M0 scale).

```asm
0x150e554:  b9 01 00 00 00   mov  ecx, 1               ; ⭐ a4 = isOut = 1  (SCALE)
0x150e55f:  48 8d 73 04      lea  rsi, [rbx+4]         ; ⭐ scale slot = B + 4
0x150e563:  e8 …             call assignStartAddr<ADDR4>(&B[4], scaleAP, a4=1)
```

> **NOTE — "the scale-stream selector" is `a4`, not a wire field.** The thing that distinguishes the scale word from the data word is the encode-time boolean `a4` that forks the shared ADDR4 emitter to vtbl `+0x28` instead of `+0x20`. There is **no** 4-bit selector nibble in the descriptor. A reimplementer who looks for a selector field in the 16 bytes will not find one; the selection happened in the codegen, before any byte was written. (CORRECTION — CONFIRMED; see §Corrections.)

### Byte +0x08 — K-extent (u16, x4-unpacked)

The extent at `+0x08` is the **logical** inner element count, multiplied by four for the packed-x4 dtypes (§The x4-unpack), stored as a `u16`. The validator requires it be nonzero.

```asm
0x150e603:  83 fa 02         cmp  edx, 0x2             ; dataAP.Dtype == 2 (float4_e2m1fn_x4)?
0x150e608:  83 ea 08         sub  edx, 0x8             ; else dt - 8
0x150e60b:  83 fa 01         cmp  edx, 0x1             ;   ≤ 1  ⇒ dt ∈ {8,9}
0x150e610:  c1 e0 02         shl  eax, 0x2             ; ⭐ n *= 4  (packed-x4)
0x150e61c:  66 89 41 08      mov  WORD PTR [rcx+8], ax ; ⭐ store K-extent @ B+0x08
```

### Byte +0x0A — step-direction sign

The sign of the innermost loop step is reduced to `{0x01, 0xFF}` (= `+1` / `-1`). The encoder takes the boolean `!isTensorIndirectDynamicAP()`, shifts it into the high bit of a 64-bit value, and arithmetic-shifts to splat the sign; the net effect is the `(step>>63)|1` form the validator enforces exactly.

```asm
0x150e5d9:  48 c1 f8 3f      sar  rax, 0x3f            ; → 0 (≥0) or -1 (<0)
0x150e5e0:  88 43 0a         mov  BYTE PTR [rbx+0xa], al  ; ⭐ step-dir @ B+0x0A
```

### Byte +0x0B — scale base-partition

The scale stream's base partition is reduced modulo `PE_count` (`= 128`, read from the ArchModel object at offset `+0x64`) and stored as a byte. This keeps the per-block E8M0 scale in the data's partition/quadrant family — the "data & scale share the same 32-partition quadrant" invariant the verifier enforces.

```asm
0x150e56b:  e8 …             call getBasePartitionInUnderlyingMemory(scaleAP)
0x150e574:  8b 4a 64         mov  ecx, DWORD PTR [rdx+0x64]  ; PE_count = ArchModel[+0x64] = 128
0x150e577:  31 d2            xor  edx, edx
0x150e579:  48 f7 f1         div  rcx                  ; p % PE_count → dl
0x150e57c:  88 53 0b         mov  BYTE PTR [rbx+0xb], dl ; ⭐ scalePart @ B+0x0B
```

### Bytes +0x0C..+0x0F — reserved

No encoder store lands between `+0x0B` and the function epilogue on the static path. The validator's static path never reads these bytes (§The wire validator). They are slack for `MXMEM_PATTERN1D` — but **fully reused** by the indirect sibling `MXINDIRECT16B`, whose `+4`-shifted layout puts its K-extent at `+0x0C` and its scale-partition at `+0x0F` (§The indirect sibling).

> **GOTCHA — do not write `+0x0C`..`+0x0F` for a static MX descriptor.** Some `mov [rbx+0x8],…` stores do appear in `assignAccessForMX`, but at `0x150e800`/`0x150e8a3` they target `rbx = &std::string` in the cold assert-builder path (SSO machinery), *not* the MXMEM slot. The only MXMEM-slot stores are the two ADDR4 writes (`+0x00`/`+0x04`), `[rcx+8]` (K), `[rbx+0xa]` (step), and `[rbx+0xb]` (scalePart). Writing anything at `+0x0C`..`+0x0F` would alias the indirect layout's fields.

## The x4-unpack

The K-extent counts **logical** inner elements, but three dtypes pack four logical elements into one physical container, so the encoder multiplies the count by four. The predicate is `(Dtype == 2) || ((unsigned)(Dtype - 8) <= 1)` ⇔ `Dtype ∈ {2, 8, 9}`:

| BIR Dtype | Name | Wire tag | Container | Packing |
|---|---|---|---|---|
| 2 | `float4_e2m1fn_x4` | `0x10` | `u16` (2 B) | 4 × 4-bit nibbles |
| 8 | `float8_e4m3fn_x4` | `0x0E` | `u32` (4 B) | 4 × FP8 |
| 9 | `float8_e5m2_x4` | `0x0F` | `u32` (4 B) | 4 × FP8 |

The same predicate appears twice in the encoder — once on the static count arm (`getNumElementsPerPartition`, `0x150e603`) and once on the indirect-count arm (`getNumIndirectIndices`, `0x150e637`) — byte-identical. The wire tags are read from the dtype→wire-tag table at `.rodata 0x1dfbad0`: `03 02 10 0d 0e 0e 03 0f ...` — index 2 → `0x10`, indices 8/9 → `0x0E`/`0x0F`, and the E8M0 entry (index 6) → `3`.

> **QUIRK — the K-extent is x4-*unpacked*, but the ADDR4 start address is in *packed* container units.** The descriptor's `+0x08` extent is the count of **logical** elements (×4); the data ADDR4 at `+0x00` points at the start of the **packed** tensor (`u16`/`u32` containers). The two are deliberately in different unit systems: the extent tells silicon how many MX-block elements to walk, the address tells it where the first packed container sits. A reimplementer who walks `extent` containers from the base address overshoots by 4×.

## The wire validator

`mxmem1d_valid` reads the descriptor by value (`rdi` = qword0, `rsi` = qword1) plus four enum args (`WRITE_TENSOR` in `edx`, `ALLOWED_IN_PSUM` in `ecx`, `ALLOWED_IN_SBUF` in `r8d`, `DTYPE` in `r9d`). The 16-byte split is `qword0 = {DATA@+0 (lo32), SCALE@+4 (hi32)}`, `qword1 = {K@+8, step@+0xA, scalePart@+0xB, reserved@+0xC..+0xF}`.

### Prologue — the layout discriminator

The first move isolates byte `+0x03` (the DATA ADDR4's high byte = its mode nibble) and tests for the indirect-gather marker `0x20`:

```asm
; mxmem1d_valid @ 0x1447190
0x144719c:  49 c1 ee 18      shr  r14, 0x18            ; r14 = byte +0x03 (DATA ADDR4 high byte)
0x14471a2:  44 89 f0         mov  eax, r14d
0x14471a9:  83 e0 60         and  eax, 0x60            ; ⭐ isolate ADDR4 mode nibble (bits 29:30)
0x14471b4:  3c 20            cmp  al, 0x20             ; ⭐ == 0b01 (INDIRECT-GATHER)?
0x14471b6:  0f 84 …          je   0x1447290            ; → indirect-marked path (§The indirect sibling)
```

### Static path (DATA nibble ≠ 0x20)

```c
bool mxmem1d_valid(MXMEM_PATTERN1D d, WRITE_TENSOR wt,        // 0x1447190
                   ALLOWED_IN_PSUM aip, ALLOWED_IN_SBUF ais, DTYPE descDt) {
    u32 dataA4 = qword0_lo(d);   u32 scaleA4 = qword0_hi(d);   // +0x00 / +0x04
    u16 K = word0(qword1(d));    i8 step = byte10(qword1(d));  // +0x08 / +0x0A
    u8  scalePart = byte11(qword1(d));                          // +0x0B

    // (1) DATA ADDR4 — try as a 4-byte type (wire tag 9)            0x14471c6 esi=9
    bool ok = tensor_start_addr_valid(dataA4, /*DTYPE=*/9, wt, /*PSUM=*/0, ais);
    if (!ok) {
        // (2) fallback: accept as a 2-byte type (tag 5) iff descDt == 0x10 (FP4-x4)
        ok = tensor_start_addr_valid(dataA4, /*DTYPE=*/5, wt, 0, ais)  // 0x14471e9 esi=5
             && (descDt == 0x10);                                       // 0x14471f5 cmp r13b,0x10
        if (!ok) return false;
    }
    // (3) SCALE ADDR4 — validate as a 1-byte type (tag 3 = E8M0)       0x1447224 esi=3
    if (!tensor_start_addr_valid(scaleA4, /*DTYPE=*/3, wt, 0, ais)) return false;

    // (4) DATA ADDR4 ACTIVE/dynamic? (nibble bits == 0x40) → skip count/step  0x1447238
    if ((byte3(dataA4) & 0x70) != 0x40) {
        if (K == 0) return false;                                       // (5) 0x1447240 test r12w
        if (((step + 1) & 0xFD) != 0) return false;                     // (6) step ∈ {+1,-1}
    }
    // (7) common tail                                                  0x1447262
    if ((byte3(dataA4) & 0xA0) == 0x80) return true;   // register-mode data: done
    return ((scalePart_u32 & 0x3000000) == 0)          // ⭐ scalePart low-2-bits == 0
        && (scalePart <= 0x0F);                         // ⭐ scalePart ≤ 15
}
```

Each numbered check pins to a literal:

- **(1)/(2) data dtype dual-accept** — `mov esi,0x9` @ `0x14471c6`; fallback `mov esi,0x5` @ `0x14471e9` then `cmp r13b,0x10; sete; and` @ `0x14471f5`. The data ADDR4 is accepted as a 4-byte address, **or**, for the FP4-x4 case (descriptor DTYPE arg `0x10`), as a 2-byte address — because the FP4-x4 packed container is 2 bytes wide.
- **(3) scale dtype** — `mov esi,0x3` @ `0x1447224` (wire tag 3 = E8M0 / 1-byte class). `shr rbp,0x20` @ `0x1447218` extracts qword0.hi (the scale ADDR4).
- **(4) ACTIVE skip** — `and edx,0x70; cmp dl,0x40` @ `0x1447238`: a dynamic-AP data word validates its own runtime fields, so the static count/step checks are skipped.
- **(5) K ≠ 0** — `test r12w,r12w; je →false` @ `0x1447240`.
- **(6) step ∈ {+1,-1}** — `mov edx,r12d; shl 8; sar 0x18; add 1; and 0xfd; jne →false` @ `0x144724a`: `(step_byte + 1) & ~0x02 == 0` ⇒ `step_byte ∈ {0x01, 0xFF}`. Exactly matches the encoder's `(step>>63)|1`.
- **(7) tail** — `and r14d,0xFFFFFFA0; cmp r14b,0x80` @ `0x1447262` (register-mode data short-circuits); else `shr rdx,0x18` (byte11), `test r12d,0x3000000; sete` + `cmp dl,0xf; setbe; and` @ `0x1447273`.

> **NOTE — encoder/decoder byte agreement (static path).** The encoder writes `+0x00` (data ADDR4), `+0x04` (scale ADDR4), `+0x08` (K≠0), `+0x0A` (step ∈ {1,0xFF}), `+0x0B` (scalePart = `p % 128`). The validator re-checks each: data addr valid (4-byte, or 2-byte for FP4-x4), scale addr valid (1-byte E8M0), K≠0, step ∈ {+1,-1}, scalePart aligned & bounded. Bytes `+0x0C`..`+0x0F` are written by neither and read by neither. Perfect agreement.

## The indirect sibling — MXINDIRECT16B

When the DATA operand is tensor-indirect, `assignAccessForMX` tail-calls `assignIndirectPatternForMX<MXINDIRECT16B>` (`@0x150de90`, reached via vtbl `+0x80`), which repacks the **same 16-byte footprint** as a three-ADDR4 gather triple — the leading ADDR4 becomes an index vector, shifting data and scale `+4`:

```text
  static MXMEM_PATTERN1D            indirect MXINDIRECT16B (+4 shifted)
  +0x00  ADDR4 DATA                 +0x00  ADDR4 INDICES  (mode nibble = 0x20)
  +0x04  ADDR4 SCALE                +0x04  ADDR4 DATA
  +0x08  K-extent                   +0x08  ADDR4 SCALE
  +0x0A  step                       +0x0C  K-extent (getNumIndirectIndices ×4)
  +0x0B  scalePart                  +0x0F  scalePart
  +0x0C..+0x0F  reserved            (footprint fully consumed)
```

The discriminator is the indices ADDR4's mode nibble: `assignIndirectPatternForMX` stamps `or byte [B+0x03], 0x20` (the indirect-gather bit), and `mxmem1d_valid`'s prologue tests `(byte+3 & 0x60) == 0x20` to select the indirect path. The indirect validator (`0x1447290`..`0x1447316`) reads:

```asm
0x1447290:  48 89 f0         mov  rax, rsi
0x1447293:  48 c1 e8 20      shr  rax, 0x20            ; ⭐ qword1 hi-word = K-extent @ +0x0C..+0x0D
0x1447297:  66 85 c0         test ax, ax               ; ⭐ MUST be nonzero
0x14472a0:  …                tensor_start_addr_valid(INDICES = qword0.lo, DTYPE=9, …)
            …                tensor_start_addr_valid(DATA    = qword1.lo, DTYPE=3, …)   ; +0x04
            …                tensor_start_addr_valid(SCALE   = qword0.hi, DTYPE=9, …)   ; +0x08
            …                scalePart = byte +0x0F; test &0x3 == 0; cmp ≤ 0x0F
```

So the indirect path's final field is `scalePart @ +0x0F` — the `+4`-shifted alias of the static `+0x0B`. This is why the 16-byte footprint is fully used by `MXINDIRECT16B` but has 4 slack bytes for the static descriptor. (The index-vector vs. data-slot dtype roles swap — the index vector carries the real addressing, so the `+0x04` data slot is validated as a 1-byte-class address.) The field-computation algorithm and the `INDIRECT16B`-vs-`INDIRECT20B` plain-indirect choice are downstream concerns, orthogonal to MX.

## The E8M0 scale

The scale operand at `+0x04` is the OCP-MXFP per-block shared exponent: **E8M0** (`bir::Dtype` 6, `float8_e8m0fnu`, MLIR `f8E8M0FNU`; the `llvm::APFloatBase::Float8E8M0FNU` type is present in `libwalrus.so`). It is 8-bit, stride 1, align 1 — wire tag 3, which is why `mxmem1d_valid` validates the scale ADDR4 as a 1-byte-class type.

### The SBUF-only resolver

The scale ADDR4 is resolved by `CoreV4Hwm::getStartAddressForMXScale` (vtbl `+0x28`), selected by the `a4 = 1` fork. It hard-asserts the memory location is SBUF and computes a pure-SBUF address — no PSUM term, no region bit:

```asm
; CoreV4Hwm::getStartAddressForMXScale @ 0x185fbe0
0x185fc0a:  e8 …             call getBasePartitionInUnderlyingMemory(scaleAP)
0x185fc15:  e8 …             call getStartingAddressInUnderlyingMemory(scaleAP)
0x185fc1a:  48 c1 e3 12      shl  rbx, 0x12            ; basePart << 18
0x185fc22:  81 e3 00 00 80 ff and ebx, 0xFF800000      ; ⭐ mask to the partition field
0x185fc28:  48 01 d8         add  rax, rbx             ; + inPartByteAddr
            …
0x185fc48:  e8 …             call __assert_fail        ; if !memloc.isSB() →
0x185fc4d:  …                lea rcx, [rip+…]          ; "MX Scale Tensor can only be in SB"
```

⇒ `scale_addr = (scaleBasePart << 18 & 0xFF800000) + inPartByteAddr`. The assert string lives at `.rodata 0x1da07b0` (*"MX Scale Tensor can only be in SB"*). Contrast the **data** resolver `getStartAddress @0x185e780`, which keeps both an SBUF arm (`shl 0x12`) and a PSUM arm (`shl 0xf` / `shl 0x19` / `shl 0xb` for partition/region/bank). The E8M0 stream is structurally a plain SBUF tensor.

### Block geometry is not a descriptor field

The OCP-MXFP block size is **32**, hard-required by the upstream legalize-scaled-matmul backend config (*"block_size must be 32"*), with `scale_method = "EMAX"`. The 32-element MX block is 8 partitions × 4 columns. The simulator's `dequantizeMx` indexes the scale stream as one E8M0 byte per 8-partition × 4-element block — `E8M0[(K/4)·(row/8) + (col/4)] − 127` as the `ldexpf` shared exponent.

> **NOTE — the descriptor carries only the scale *base address*, not the block stride.** `block_size = 32`, the `8×4` block tiling, EMAX/RNE, and the per-block scale walk are **not** MXMEM_PATTERN1D wire fields. The descriptor's 5-tuple is `{data addr, scale addr, K, step, scalePart}`. The per-block stride geometry lives in the scale operand's own `AccessPattern` (consumed by silicon) and is enforced by `checkQuantizeMxInstruction` + the operand APs upstream — a reimplementer of the *descriptor* needs the base address and base-partition only. (G-2, STRONG: the `8×4`/`32` geometry is cross-confirmed from the simulator's index formula and the backend's `block_size=32` config, but it is not read out of the 16 descriptor bytes.)

## The CoreV4-only gate

MXMEM_PATTERN1D and its entire machinery exist on gen4 alone. An `nm -DC` sweep of `libwalrus.so` returns MX symbols **only** under `core_v4` / `CoreV4`:

| Symbol | Owner | Address | Confidence |
|---|---|---|---|
| `mxmem1d_valid` | `core_v4` | `0x1447190` | CONFIRMED |
| `assignAccessForMX<MXMEM_PATTERN1D>` | `CoreV4GenImpl` | `0x150e2f0` | CONFIRMED |
| `assignIndirectPatternForMX<MXINDIRECT16B>` | `CoreV4GenImpl` | `0x150de90` | CONFIRMED |
| `visitInstMatmultMx` | `CoreV4GenImpl` | `0x143f410` | CONFIRMED |
| `visitInstQuantizeMx` | `CoreV4GenImpl` | `0x143dc60` | CONFIRMED |
| `getStartAddressForMXScale` | `CoreV4Hwm` | `0x185fbe0` | CONFIRMED |

There is **no** `core_v2`/`core_v3` `mxmem1d_valid`, no `CoreV2GenImpl`/`CoreV3GenImpl` `assignAccessForMX`, no gen2/gen3 `visitInstMatmultMx`/`visitInstQuantizeMx`, and no gen2/gen3 `getStartAddressForMXScale` — the demangled-symbol sweep tags every MX symbol as `core_v4`/`CoreV4`. (`core_v3` has `tensor3d_valid` and the rest of the rank-*n* validators, but nothing MX.) Arch 40 = `core_v4` internal / *mariana* runtime / Trn3 external (see [Codename Taxonomy](../arch/codename-taxonomy.md), 1.02).

> **GOTCHA — gen2/gen3 do not FATAL on an MX op; they never see one.** `CoreV2GenImpl`/`CoreV3GenImpl` do not override `visitInstMatmultMx`/`visitInstQuantizeMx`, so an `InstMatmultMx`/`InstQuantizeMx` in a gen2/gen3 codegen would fall through to the base `Generator` visitor (its default "not implemented" arm). In practice the MX path is gated **upstream**: the compiler only lowers to the MX custom-ops when targeting gen4 (the FP4 dtype slot exists only on the gen4 PE constructor). The `getStartAddressForMXScale` resolver simply does not exist on the gen2/gen3 Hwm vtables. (CONFIRMED gate; STRONG on the exact gen2/3 fall-through diagnostic — the base visitor was not byte-traced to a FATAL because no gen2/3 MX encoder exists to reach it.)

## The consumers

The 16-byte slot lives inside the 64-byte instruction bundle of three MX ops:

| Op | Generator | Slot | Layout from slot base |
|---|---|---|---|
| `MatmultMx` (`0x100A`) | `generateMatmultMx @0x143ebd0` | `bundle+0x10` | ifmap x4 DATA `+0x10`, E8M0 SCALE `+0x14`, K `+0x18`, step `+0x1A`, scalePart `+0x1B`, reserved `+0x1C..+0x1F` |
| `LdWeightMx` (`0x1009`) | `generateLdweightMx @0x143e350` | `bundle+0x10` | weights x4 DATA + weights E8M0 SCALE (the stationary side) |
| `QuantizeMx` (`0x10E3`) | `visitInstQuantizeMx @0x143dc60` | `bundle+0x30` | same encoder `assignAccessForMX<MXMEM_PATTERN1D> @0x150e2f0` |

The slot is always a 16-byte-aligned region of the bundle, `pxor`/`movaps`-zeroed with the bundle (`0x143ed0a..`), 12 bytes written + 4 reserved. The CoreV4 `runISACheck` dispatch loads the slot as one 16-byte XMM (`movdqu xmm4,[rsp+0xe0]` @ `0x14c7ae0`) and passes it by value to `mxmem1d_valid` (`call …@plt 0x6231e0` @ `0x14c7b1b`).

## Encode walkthrough

Annotated C pseudocode for the data+scale encode, naming the real symbols. A reimplementer fills the 16-byte slot from a data AccessPattern + a physical scale AccessPattern:

```c
// CoreV4GenImpl::assignAccessForMX<MXMEM_PATTERN1D> @ 0x150e2f0
void encode_mx_descriptor(u8 *B, const AccessPattern &dataAP,
                          const PhysicalAccessPattern &scaleAP) {
    if (dataAP.isTensorIndirectDynamicAP())                  // 0x150e420, vtbl+0x80
        return encode_mxindirect16b(B, dataAP, scaleAP);     // tail-call (§indirect)

    // (a) DATA ADDR4 @ B+0x00 — a4=0 → CoreV4Hwm::getStartAddress (SBUF|PSUM)
    assignStartAddr<ADDR4>(&B[0x00], dataAP, /*a4=*/0);       // 0x150e544/54f

    // (b) SCALE ADDR4 @ B+0x04 — a4=1 → getStartAddressForMXScale (SBUF-only)
    assignStartAddr<ADDR4>(&B[0x04], scaleAP, /*a4=*/1);      // 0x150e554/63

    // (c) scalePart @ B+0x0B = scaleBasePartition % PE_count(=128)
    u64 p  = scaleAP.getBasePartitionInUnderlyingMemory();   // 0x150e56b
    u32 pe = archModel->PE_count;            // ArchModel[+0x64] = 128
    B[0x0B] = (u8)(p % pe);                                   // 0x150e579 div / 0x150e57c

    // (d) step-dir @ B+0x0A ∈ {0x01, 0xFF}  = (step >> 63) | 1
    i64 step = dataAP.pattern[dataAP.pattern.size - 1].step;
    B[0x0A] = (u8)((step >> 63) | 1);                        // 0x150e5d9 sar / 0x150e5e0

    // (e) K-extent @ B+0x08 (u16), x4-unpacked for packed-x4 dtypes
    u32 n = dataAP.getNumElementsPerPartition();
    if (dataAP.Dtype == 2 || (u32)(dataAP.Dtype - 8) <= 1)   // {2,8,9}: 0x150e603-610
        n <<= 2;                                              // ⭐ logical → packed-x4 count
    *(u16 *)&B[0x08] = (u16)n;                                // 0x150e61c

    // (f) B[0x0C..0x0F] left ZERO (bundle was pxor-zeroed) — reserved
}
```

## Adversarial self-verification — the five strongest claims

1. **16-byte type, 12 bytes written.** *Challenge*: is the type really 16 bytes, or could the trailing 4 be a separate adjacent field? *Re-derive*: the validator takes the descriptor by value — the call-site loads it as one 16-byte XMM (`movdqu xmm4,[rsp+0xe0]` @ `0x14c7ae0`) and the body splits it as `rdi`=qword0 / `rsi`=qword1 (`mov r14,rdi; shr r14,0x18` reads byte+3, `mov r12,rsi` holds qword1). An exhaustive store-enum of `assignAccessForMX` finds MXMEM-slot writes only at `+0x00`/`+0x04` (ADDR4), `[rcx+8]` (`+0x08`), `[rbx+0xa]`, `[rbx+0xb]` — none at `+0x0C`..`+0x0F`. **Holds — CONFIRMED.**
2. **Dual ADDR4: data (a4=0) + scale (a4=1).** *Challenge*: are `+0x00` and `+0x04` truly two independent ADDR4 words, and is the second the scale? *Re-derive*: `xor ecx,ecx; call assignStartAddr` @ `0x150e544` (a4=0, slot `B`), then `mov ecx,1; lea rsi,[rbx+4]; call assignStartAddr` @ `0x150e554` (a4=1, slot `B+4`). `a4=1` forks the leaf to `call [rax+0x28]` = `getStartAddressForMXScale`, which hard-asserts SBUF. **Holds — CONFIRMED.**
3. **block_size = 32.** *Challenge*: is 32 a descriptor field, or external? *Re-derive*: the descriptor's 16 bytes carry `{data, scale, K, step, scalePart}` — no `block_size` byte. `block_size = 32` is the OCP-MXFP standard enforced by the upstream backend config (*"block_size must be 32"*) and reflected in the simulator's per-32-element block-index formula; the descriptor only carries the scale base address + base-partition. **Holds — CONFIRMED (value); STRONG (it lives outside the descriptor).**
4. **CoreV4-only.** *Challenge*: could a gen2/gen3 MX symbol be hiding under a different mangling? *Re-derive*: `nm -DC libwalrus.so | rg 'mxmem1d_valid|assignAccessForMX<…MXMEM|getStartAddressForMXScale|assignIndirectPatternForMX<…MXINDIRECT'` tags every match `core_v4`/`CoreV4` (10 + 3 hits); zero `core_v2`/`core_v3`/`CoreV2`/`CoreV3`. The base `bir::Hwm::getStartAddressForMXScale` is undefined-only (`U`); only `CoreV4Hwm` defines it. **Holds — CONFIRMED.**
5. **x4-unpack for `{2,8,9}`.** *Challenge*: is the gate really `{2,8,9}` and not `{2,8,9,10}`? *Re-derive*: `cmp edx,0x2; je` (Dtype 2) `or` `sub edx,0x8; cmp edx,0x1` (`(Dtype-8) ≤ 1` ⇒ {8,9}) then `shl eax,0x2` @ `0x150e610`. `(unsigned)(10-8) = 2 > 1`, so Dtype 10 is excluded. The wire-tag table at `0x1dfbad0` confirms only indices 2/8/9 map to packed-x4 tags `0x10`/`0x0E`/`0x0F`. **Holds — CONFIRMED.**

## Corrections

> **CORRECTION (N04-1) — no "region/length @+0xC" field, no "4-bit scale selector @+0xF" in the static descriptor.** An earlier enumeration posited two extra fields in the trailing 4 bytes. The binary does not support them for the static `MXMEM_PATTERN1D`: `+0x0C`..`+0x0F` are reserved/zero and the validator's static path never reads them. The only byte ever read at `+0x0F` is in the indirect-marked `MXINDIRECT16B` layout (= its `+4`-shifted scale-partition). And the "scale-stream selector" is **not** a 4-bit descriptor nibble — it is the `a4 = 1` / `isOut = 1` boolean argument to `assignStartAddr<ADDR4>` that routes the scale word to `getStartAddressForMXScale` (vtbl `+0x28`) instead of `getStartAddress` (vtbl `+0x20`). The selection is encode-time, not a wire field. **CONFIRMED both ways.**

> **CORRECTION (N04-2) — "12-byte descriptor" is a count of meaningful bytes, not the type footprint.** Prior MX field-layout passes' *"12-byte descriptor"* and this page's *"16-byte type"* are both correct, reconciled as **16-byte type, 12 written, 4 reserved.** Not a contradiction — a clarification of what was being counted.

## Gaps

- **G-1 (STRONG):** the *silicon* meaning of the step-dir `+0x0A` byte (the ±1 contraction-walk direction) is read from the encoder's sign-extract + the validator's `{+1,-1}` enforcement; the silicon walk direction itself is inferred, not pinned to a string.
- **G-2 (STRONG):** the E8M0 block-stride geometry (`8×4`/`32`) lives in the scale operand's AccessPattern + silicon, not the descriptor; cross-confirmed from the simulator's `dequantizeMx` index formula and the backend's `block_size=32` config, but not a wire field.
- **G-3 (residual):** the ADDR4 internal sub-fields (29-bit addr / region `25..28` / mode nibble / register-mode bit) are `assignStartAddr<ADDR4>`'s domain — closed in [ADDR4](addr4.md) (2.2), not re-derived here.
- **G-4 (GAP):** no `.neff` byte-fixture was diffed. The layout is disassembly-derived (encoder writes and validator reads agree byte-exact), but a real emitted `MXMEM_PATTERN1D` from a compiled `.neff` was not captured.

## Cross-References

- [ADDR4 — the 32-Bit Address Word](addr4.md) (2.2) — the word each of the two slots holds; the `a4` data-vs-scale resolver fork and the mode nibble that selects the indirect layout.
- [Tensor Descriptors — the 4+4N Rule](tensor-descriptors.md) (2.3) — the non-MX `MEM_PATTERN`/`TENSORnD` family this descriptor diverges from (one ADDR4 + loop words vs. two ADDR4 words).
- [MEM_PATTERN2D / 3D — the DST/PSUM Role](mempattern-2d-3d.md) (2.5) — lower-rank patterns that open with the same ADDR4 word.
- [PE Matmul Encoding](pe-matmul-encoding.md) (2.10) — *(planned)* the `MatmultMx`/`LdWeightMx` bundles that carry this slot at `bundle+0x10`.
- [PE Engine — the Systolic Matmul Array](../arch/pe-engine.md) (1.08) — the engine that consumes the MX data+scale pair per block.
- [DVE Engine — Microcode-Table Architecture](../arch/dve-engine.md) (1.11) — the other consumer of MX-addressed operands.
- [Codename ↔ Device ↔ Generation Taxonomy](../arch/codename-taxonomy.md) (1.02) — CoreV4 = arch 40 = `core_v4` / *mariana* / Trn3, the gate this descriptor lives behind.
- [Numeric Semantics — MX microscaling & E8M0](../numerics/) (9.8/9.10) — *(planned)* the OCP-MXFP block format, the `float8_e8m0fnu` shared exponent, `block_size=32`, EMAX.
- [BIR, libBIR & the Simulator](../bir/) (Part 7) — *(planned)* the simulator's `dequantizeMx` block-index geometry and the `checkQuantizeMxInstruction` operand-AP checks.
