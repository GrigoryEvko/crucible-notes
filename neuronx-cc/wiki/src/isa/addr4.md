# ADDR4 — the 32-Bit Address Word

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core). The encoder, decoder, and aligner live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset); the enums (`NEURON_ISA_TPB_DTYPE`, `bir::Register`) live in `libBIR.so`. Other wheels differ; treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

**ADDR4** is the four-byte, little-endian `u32` start-address word that opens **every** on-chip tensor memory-pattern slot the Tonga backend emits — `TENSOR1D`/`2D`/`3D`/`4D` (= `MEM_PATTERN`), the MX microscaling patterns `MXMEM_PATTERN1D`, and the gather descriptor `MXINDIRECT16B`. It is the atom every higher descriptor composes: a tensor descriptor (2.3), a 4-D mem-pattern (2.4), a 2-D/3-D pattern (2.5), and the three-slot `INST_UNION` all begin with one of these words. Get ADDR4 wrong and nothing above it round-trips.

The word packs four things into 32 bits: a **29-bit byte address** (low bits `0..28`), a **region discriminator** folded into the high address bits `25..28` that splits SBUF from PSUM, a **two-bit mode nibble** at bits `29..30` (`static / indirect-gather / active / dead`), and a top **register-mode flag** at bit `31` that re-purposes byte 0 as an 8-bit colored-register id. ADDR4 is **arch-universal**: the gen2/gen3/gen4 (`CoreV2`/`CoreV3`/`CoreV4`) encoders are byte-identical and the three decoders share every constant, with exactly two documented deltas (one alignment widening for the gen4-only FP4 type, one inlined helper).

The bar for this page is that a reader can **encode or decode any ADDR4 word by hand** — knowing its mode, its memory space, its per-dtype alignment requirement, and which of two address resolvers (data vs. scale) produced it. Everything is recovered from the `libwalrus.so` encoder/decoder bodies; the bit positions are pinned against the literal mask and shift constants in the disassembly, not inferred.

## At a glance

| Field | Bits | Mask | Meaning | Confidence |
|---|---|---|---|---|
| Byte address | `0..28` | `0x1FFFFFFF` | 29-bit on-chip byte address (SBUF or PSUM) | CERTAIN |
| Region class | `25..28` | `0x1E000000` | high addr bits; `0` ⇒ SBUF, nonzero ⇒ PSUM | CERTAIN |
| Mode bit 0 | `29` | `0x20000000` | INDIRECT-gather (the index/offset slot) | CERTAIN |
| Mode bit 1 | `30` | `0x40000000` | ACTIVE / dynamic-AP marker | CERTAIN (consumer); MEDIUM (name) |
| Register-mode flag | `31` | `0x80000000` | RM=1 ⇒ byte 0 is a register id, bits `8..30` zero | CERTAIN |
| Register id | `0..7` | `0xFF` | only when RM=1; valid range `0..63` (`< 0x40`) | CERTAIN |

**Mode nibble** = `byte3 & 0x60` = word bits `[30:29]`: `0b00` static · `0b01` indirect-gather · `0b10` active/dynamic · `0b11` **dead** (no producer, no consumer).

## The bit-field

```
 bit  31   30   29   28 ............... 25 24 ............................. 0
     +----+----+----+--------------------+--------------------------------+
     | RM | M1 | M0 |    region class    |        byte address (29-bit)   |
     +----+----+----+--------------------+--------------------------------+
      0x80 0x40 0x20 \____ 0x1E000000 ___/ \__________ 0x1FFFFFFF _________/
       │    │    │     (bits 25..28: high addr bits   (bits 0..28: the full
       │    │    │      that double as SBUF/PSUM        29-bit byte address;
       │    │    │      region discriminator)           bits 25..28 included)
       │    │    └─ MODE bit0  : 0x20  INDIRECT / gather-index slot
       │    └────── MODE bit1  : 0x40  ACTIVE / dynamic-AP
       └─────────── REGISTER-MODE flag (orthogonal to the mode nibble)

  byte layout (little-endian on the wire):
     byte0 = bits 0..7    byte1 = bits 8..15   byte2 = bits 16..23   byte3 = bits 24..31
     ^ register id (RM=1)                                            ^ all field bits live here:
                                                                       bit5=0x20 bit6=0x40 bit7=0x80
```

When **RM=1** the word collapses to `0x80000000 | (regid & 0xFF)` with `regid < 0x40` and bits `8..30` forced to zero — the address field is *not* used; the colored register supplies the address at runtime ([5.25 register-ALU materialization](../penguin/)).

## Function roster

Every body below is a real, disassembled function in `libwalrus.so` (the dynamic symbol table survives, so `nm -DC` demangles them). The `.text` VA equals the file offset.

| Role | Symbol | Address | Confidence |
|---|---|---|---|
| **Encoder** | `Generator::assignStartAddr<core_v2::…ADDR4>` | `0x1172e10` | CERTAIN |
| | `Generator::assignStartAddr<core_v4::…ADDR4>` | `0x1508df0` | CERTAIN (byte-identical to v2) |
| **Decoder** | `core_v2::tensor_start_addr_valid` | `0x127f590` | CERTAIN |
| | `core_v3::tensor_start_addr_valid` | `0x136ed70` | CERTAIN |
| | `core_v4::tensor_start_addr_valid` | `0x1446480` | CERTAIN (twin of v3) |
| **Aligner** | `core_v2::addr_aligned_dtype` | `0x127f530` | CERTAIN |
| | `core_v3::addr_aligned_dtype` | `0x136ed10` | CERTAIN |
| | `core_v4::addr_aligned_dtype` | `0x1446420` | CERTAIN (one delta, §Alignment) |
| **PSUM-dtype helper** | `sub_1343c70` (v3) / `0x142de60` (v4) | `0x1343c70` | CERTAIN |
| **Nibble consumer** | `core_v3::mem4d_valid` | `0x136ee40` | CERTAIN |
| | `core_v3::tensor1d_valid` | `0x136f200` | CERTAIN |
| **Indirect stamp** | `CoreV4GenImpl::assignIndirectPatternForMX<MXINDIRECT16B>` | `0x150de90` | CERTAIN |
| **MX dual-ADDR4 leaf** | `CoreV4GenImpl::assignAccessForMX<MXMEM_PATTERN1D>` | `0x150e2f0` | CERTAIN |

> **NOTE — there is no `core_v3` template instantiation of the encoder.** `CoreV3` reuses the `core_v2::ADDR4` `assignStartAddr` body verbatim; only the decoder and aligner have a distinct `core_v3` copy. The decoder symbol `tensor_start_addr_valid @ 0x136ed70` and aligner `addr_aligned_dtype @ 0x136ed10` are **core_v3** functions — the demangled name carries `7core_v3`. The gen4 copies are `0x1446480` / `0x1446420`.

## The 29-bit address field — bits 0..28

The decoder's first move on the non-register path is to mask off everything but the address:

```asm
0x136ed96:  41 81 e7 ff ff ff 1f    and    r15d, 1FFFFFFFh   ; ⭐ the 29-bit mask
0x136eda5:  e8 …                     call   addr_aligned_dtype(r15d, dtype)
```

Bits `29..31` are stripped before the address is interpreted; the alignment check, the PSUM-window subtraction, and the region-class mask all operate on this 29-bit value (`r15d`). The encoder side asserts the resolved 64-bit byte address fits in `u32` (`shr rax,0x20; setz dil` → the error string *"memory address out of uint32 range"*) and stores the low 32 bits verbatim.

*Anchors: the 29-bit mask at `0x136ed96` (v3), `0x14464a6` (v4), `0x127f5bf` (v2).*

⇒ The addressable on-chip space per descriptor is `2^29 = 512 MiB` of byte address. Both physical windows fit comfortably inside it — SBUF (192 KiB/partition × 128 partitions) and PSUM (8 banks × 2 KiB × 128 partitions); see [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) (1.05).

## The mode nibble — bits 29/30 (`byte3 & 0x60`)

The nibble is read off byte 3 of the word. The multi-dim validators isolate it with `& 0x60` and dispatch:

```asm
; core_v3::mem4d_valid @ 0x136ee40 (core_v2 0x127f660 byte-identical)
0x136ee51:  movzx  r13d, byte [rsp+0x33]   ; byte3 of the ADDR4 word
0x136ee5a:  and    eax, 60h                ; ⭐ isolate bits 30:29 (the nibble)
0x136ee5d:  cmp    al, 20h                  ; ⭐ == 0b01 (INDIRECT)?
0x136ee5f:  jne    <static path>
…
0x136ee8a:  test   byte [rsp+0x37], 0E0h    ; the index slot's data partner: bits 29/30/31 must be 0
```

| Nibble | Bits 30:29 | Meaning | Producer | Consumer | Confidence |
|---|---|---|---|---|---|
| `0x00` | `00` | STATIC physical address | default (no stamp) | static-addr path | CERTAIN |
| `0x20` | `01` | INDIRECT-GATHER index | `assignIndirectPatternForMX` (`or +3,0x20`) | `memNd_valid` (`cmp 0x20`) | CERTAIN |
| `0x40` | `10` | ACTIVE / dynamic | upstream descriptor template (**not** the ADDR4 encoder) | `tensor1d_valid` (`test 0x40000000`) | CERTAIN (consumer); MEDIUM (name) |
| `0x60` | `11` | UNUSED / dead | NONE | NONE | CERTAIN |

The encoder stamps **only** the indirect bit, and only onto the index slot of a gather:

```asm
; assignIndirectPatternForMX<MXINDIRECT16B> @ 0x150de90
0x150e26b:  80 48 03 20    or  byte [rax+3], 20h   ; ⭐ bit29 = TENSOR-INDIRECT (mode 0b01)
```

The `0x40` (ACTIVE) branch is exercised by `tensor1d_valid`, which tests bit 30 directly and falls through to a static/register split on the sign bit:

```asm
; core_v3::tensor1d_valid @ 0x136f200
0x136f221:  f7 c7 00 00 00 40    test  edi, 40000000h   ; ⭐ bit30 (ACTIVE/dynamic) set?
0x136f23d:  79 29                jns   <static addr path>; (bit31 clear ⇒ static; set ⇒ register)
```

Bit 30 is read here but never written here. An exhaustive `.text` sweep turns up zero `+3 |= 0x40` and zero `+3 |= 0x60` writes anywhere in `libwalrus.so`: the ACTIVE marker is stamped into the *descriptor template* that the dynamic-AP path materializes upstream, and `tensor1d_valid` only consumes it. The single mode bit `assignStartAddr` writes is bit 29 (`0x20`, the gather-index marker). The `0b11` nibble is genuinely dead — no producer emits it, no validator accepts it.

> **GOTCHA — the encoder writes one mode bit, not two.** Reading `assignStartAddr` alone will not show you where ACTIVE comes from, because it never sets `0x40`. Look upstream at the dynamic-AP descriptor template.

## PSUM-window detection + region class — bits 25..28

The high bits of the 29-bit address double as the SBUF/PSUM discriminator, because the Hwm region math places PSUM at a fixed window base `0x2000000 = 1<<25` and SBUF at addresses with bits `25..28` clear. The decoder expresses the same split two ways — a window subtraction *and* a region mask — and they agree:

```asm
; core_v3::tensor_start_addr_valid @ 0x136ed70, non-register path
0x136edae:  41 81 ef 00 00 00 02   sub  r15d, 2000000h    ; ⭐ subtract PSUM window base (32 MiB)
0x136edb5:  41 81 ff ff ff 3f 00   cmp  r15d, 3FFFFFh      ; ⭐ > 4 MiB ⇒ NOT PSUM (SBUF)
0x136edbc:  77 62                  ja   <SBUF path>
0x136edbe:  41 80 fe 01            cmp  r14b, 1            ; ALLOWED_IN_PSUM == 1?
0x136edca:  81 e3 00 00 00 1e      and  ebx, 1E000000h     ; ⭐ region-class mask (PSUM path)
…
0x136ee20:  81 e3 00 00 00 1e      and  ebx, 1E000000h     ; ⭐ region-class mask (SBUF path)
```

Decoded:

| Condition | Test | Result |
|---|---|---|
| PSUM | `(addr29 − 0x2000000) ≤ 0x3FFFFF` ⇔ `addr29 ∈ [0x2000000, 0x23FFFFF]` | 4 MiB window based at 32 MiB |
| SBUF | otherwise | bits `25..28` **must be 0** (validator rejects a SBUF addr with any set) |

A PSUM access must set bits `25..28` (at minimum bit 25 = `!scaleFlag`); a SBUF access must clear them. The physical PSUM content (8 banks × 2 KiB × 128 partitions = 2 MiB) sits inside the 4 MiB address window; see [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) (1.05). The 4 MiB literal is exactly `0x3FFFFF + 1`.

> **NOTE — bits 25..28 are not a separate "region + size class" nibble.** They are simply the high bits of the 29-bit byte address, which the PSUM window base (`0x2000000 = 1<<25`) forces nonzero for PSUM and zero for SBUF. The window-subtract test and the `& 0x1E000000` test are two views of one region split, and the validator contains both.

**PSUM-dtype restriction.** A PSUM *write* must target a 4-byte accumulator type. The helper isolates that:

```asm
; sub_1343c70 (v3) / 0x142de60 (v4)
0x1343c70:  83 ef 08      sub    edi, 8
0x1343c73:  40 80 ff 03   cmp    dil, 3
0x1343c77:  0f 96 c0      setbe  al          ; ⇒ wireTag ∈ {8,9,10,11}
```

`wireTag ∈ {8,9,10,11}` = `{int32, uint32, float32, float32r}`. A PSUM *read* (`WRITE_TENSOR==0`) is allowed with any dtype; for SBUF the post-base `addr > 0x3FFFFF` term is `1`, so the restriction is vacuously satisfied (SBUF accepts all dtypes on this axis).

## Per-dtype byte alignment — `addr_aligned_dtype`

`addr_aligned_dtype(unsigned addr, DTYPE wireTag)` returns `(addr & (align-1)) == 0`. It is keyed on the **NEURON_ISA_TPB_DTYPE wire-tag**, not the BIR Dtype — to get the alignment for a BIR Dtype, first map `wire_tag = byte_1DFBAD0[dt]` (the dtype→wire-tag table). The branch decode, from core_v3 `0x136ed10`:

```asm
0x136ed10:  lea  eax, [rsi-2]   ; cmp al,1  setbe   ; wireTag-2 ≤ 1  ⇒ {2,3}
0x136ed13:  lea  edx, [rsi-0Dh] ; cmp dl,2  setbe   ; wireTag-0xd ≤ 2 ⇒ {13,14,15}
            ;  if (wireTag ∈ {2,3,13,14,15}) return true            → ALIGN 1
0x136ed25:  lea  eax, [rsi-4]   ; cmp al,3  ja       ; wireTag-4 ≤ 3 ⇒ {4,5,6,7}
0x136ed2c:  not  edi  ; and eax,1                    → ALIGN 2  (mask &1)
0x136ed38:  lea  eax, [rsi-8]   ; cmp al,3  jbe      ; wireTag-8 ≤ 3 ⇒ {8,9,10,11}
0x136ed60:  and  edi, 3                              → ALIGN 4  (mask &3)
0x136ed3f:  cmp  sil,1 / cmp sil,0Ch                 ; wireTag ∈ {1,12}
0x136ed51:  and  edi, 7                              → ALIGN 8  (mask &7)
            ;  else (wireTag 0,16,…) return false    ; no defined alignment
```

| Wire-tag set | Align | Mask | BIR dtypes (via the dtype→wire-tag table) | Confidence |
|---|---|---|---|---|
| `{2,3,13,14,15}` | 1 | — | `int8`(2), `uint8`/`e8m0`(3), `float8_e3`(13), `e4m3`(14), `float8_e5`(15) | CERTAIN |
| `{4,5,6,7}` | 2 | `&1` | `int16`(4), `uint16`(5), `bfloat16`(6), `float16`(7) | CERTAIN |
| `{8,9,10,11}` | 4 | `&3` | `int32`(8), `uint32`(9), `float32`(10), `float32r`(11) | CERTAIN |
| `{1,12}` | 8 | `&7` | `uint64`(1), `int64`(12) | CERTAIN |
| `{0,16,…}` | n/a | `false` | wireTag 16 = FP4-x4 (gen4-only; see delta) | CERTAIN |

The required alignment **equals the element byte-size** of the wire container (1/2/4/8) — the start address must be naturally aligned to its element. The aligner is the **first gate** on the static path: `tensor_start_addr_valid` calls it on the 29-bit address (`r15d = a1 & 0x1FFFFFFF`) *before* the region test, and returns `false` if it fails (`0x136eda5`).

> **CORE_V4 DELTA — FP4-x4 joins the no-alignment set.** The gen4 aligner widens the second group: `cmp dl, 3` (gen2/3 use `cmp dl, 2`) ⇒ `wireTag-0xd ≤ 3` ⇒ `{13,14,15,16}`. So core_v4 adds **wire-tag 16** (FP4-x4, BIR Dtype `float4_e2m1fn_x4`) to ALIGN-1 — on gen4 the packed FP4-x4 type is "always aligned". On gen2/3, wire-tag 16 hits the default (`false`, no defined alignment) because FP4-x4 is a gen4-only type. The one-byte difference is at `0x144642d: cmp dl, 3` (v4) against `0x136ed1b: cmp dl, 2` (v3). The `{4,5,6,7}→&1` group is refactored into a tail-called inline helper (`0x142de70: sub edi,4; cmp dil,3; setbe`) — same `{4..7}⇒align-2` semantics, no functional change.

## Register mode — bit 31 set, byte 0 = register id

When an access pattern carries a colored register instead of a static address, the encoder takes the `kind-3 RegisterAP` path: it asserts no offset-register is also applied, stamps bit 31, fetches the register id, and packs it into byte 0:

```asm
; assignStartAddr<core_v4::ADDR4> @ 0x1508df0, RegisterAP path
0x1508f28:  83 f7 01      xor   edi, 1       ; assert AP+0x114 (is_regloc_offset) — error if set
0x1508f58:  80 4b 03 80   or    byte [rbx+3], 80h   ; ⭐ bit31 = REGISTER-MODE flag
0x1508f65:  e8 …          call  bir::Register::getRegId(regref@AP+0xE8)
0x1508fca:  44 88 3b      mov   byte [rbx], r15b     ; ⭐ byte0 = 8-bit register id
```

⇒ the word becomes `0x80000000 | (regid & 0xFF)`; bits `8..30` are left zero. The decoder reads byte 3, takes the register branch on the sign bit, and validates the packing:

```asm
; core_v3::tensor_start_addr_valid @ 0x136ed70, register branch
0x136ed76:  c1 e8 18              shr   eax, 18h         ; byte3
0x136ed85:  84 c0                 test  al, al
0x136ed87:  78 77                 js    loc_136EE00      ; ⭐ bit31 set ⇒ register mode
0x136ee02:  f7 c7 00 ff ff 00     test  edi, 0FFFF00h    ; ⭐ bits 8..23 MUST be zero
0x136ee0a:  40 80 ff 40           cmp   dil, 40h
0x136ee0e:  0f 92 c0              setb  al               ; ⭐ return (regid < 0x40)
```

The gen2 decoder enforces the same invariant with a stricter combined mask — `(a1 & 0xFFFFFF00) == 0x80000000` (bits `8..30` zero *and* bit 31 set), then `regid ≤ 0x3F`. **Register-id packing**: an 8-bit field in byte 0, valid range `0..63` (`< 0x40`); the register was minted by `lower_ap`/`convertSymAP` before register allocation, and `assignStartAddr` only stamps the id and the flag. Both arch families enforce this identically. See [Part 7 — codegenAccess](../bir/).

> **GOTCHA — register mode is bit 31, and bit 30 is a different bit entirely.** The two are easy to conflate because both live in byte 3, but they are tested independently and mean unrelated things: RM (bit 31, `0x80`, reached by a sign test on byte 3) says "byte 0 holds a register id", while bit 30 (`0x40`) is the second mode-nibble bit, reached only through the `& 0x60` nibble mask. They are orthogonal — a register-AP word carries mode nibble `0b00` together with `RM=1`.

*Anchors: the encoder stamps `or [rbx+3], 80h` at `0x1508f58`; the decoder's register branch is the sign test `shr eax,18h; test al,al; js` at `0x136ed76`–`0x136ed87`, which reads bit 7 of byte 3 = word bit 31. Bit 30 is instead tested by `test edi, 40000000h` at `0x136f221` and by the `and eax, 60h` nibble mask at `0x136ee5a` — never by the register branch.*

## The data-vs-scale resolver fork — the `a4` argument

`assignStartAddr<ADDR4>(slot, AP, bool a4)` takes a third `bool a4` (in `cl`) that, on the kind-1 Physical path, selects **which Hwm vtable slot** resolves the address:

```asm
; assignStartAddr<core_v4::ADDR4> @ 0x1508df0
0x1508e44:  test  cl, cl ; jne 0x1508ed0           ; a4 (scaleFlag)?
0x1508e4c:  ff 50 20     call qword [rax+20h]       ; a4==0 → DATA  → vtbl+0x20 = getStartAddress
0x1508ed0:  ff 50 28     call qword [rax+28h]       ; a4==1 → SCALE → vtbl+0x28 = getStartAddressForMXScale
```

`a4=1` is passed **only** by the two MX leaves; every plain `TENSORnD` slot passes `a4=0`. The MX dual-ADDR4 encoder threads data and scale into adjacent 4-byte slots:

```asm
; assignAccessForMX<MXMEM_PATTERN1D> @ 0x150e2f0
0x150e544:  31 c9            xor  ecx, ecx           ; ⭐ DATA  (a4=0)  → ADDR4 @ slot+0  (rsi = rbx)
0x150e54f:               …   call assignStartAddr<ADDR4>
0x150e554:  b9 01 00 00 00   mov  ecx, 1             ; ⭐ SCALE (a4=1)  → ADDR4 @ slot+4
0x150e55f:  48 8d 73 04      lea  rsi, [rbx+4]
0x150e563:               …   call assignStartAddr<ADDR4>
```

The tri-ADDR4 gather leaf (`assignIndirectPatternForMX<MXINDIRECT16B>`) likewise passes `a4=1` only for the E8M0 scale slot (`@+8`); index (`@+0`) and data (`@+4`) use `a4=0`. So **"scale" is an exclusively-MX concept** — the resolver fork exists to route the E8M0 block-scale stream to `getStartAddressForMXScale` (SB-only, `(basePart<<18)&0xFF800000`). See [tensor4d / mempattern4d](tensor4d-mempattern4d.md) (2.4).

*Anchors: the `a4` selectors at `0x150e544` (data) and `0x150e554` (scale), matching the MX dual-ADDR4 layout data@+0 / scale@+4.*

## Arch-universality

- **Encoder**: `core_v2 @ 0x1172e10` is byte-identical to `core_v4 @ 0x1508df0` (same 3-way kind switch, same mode/region/register stamps); `CoreV3` reuses the v2 template.
- **Decoder**: v2/v3/v4 share every constant — 29-bit mask `0x1FFFFFFF`, PSUM window base `0x2000000` + span `0x3FFFFF`, region mask `0x1E000000`, register branch (bit 31 + bits `8..23` zero + `regid<0x40`), PSUM-dtype helper `{8,9,10,11}` — byte-exact across all three.
- **Aligner**: v2 `0x127f530` == v3 `0x136ed10`; v4 `0x1446420` adds wire-tag 16 (FP4-x4) to the no-align set and refactors the `{4..7}→&1` group into helper `0x142de70` — no semantic change beyond the new gen4 type.

## Evidence summary

Every bit position on this page is pinned to a literal mask or shift in the disassembly, not to a name or a docstring.

- **Register mode = bit 31.** The sign test at `0x136ed85` operates on `al`, and `eax = a1 >> 0x18` was set at `0x136ed76`, so `al` is byte 3 — bit 7 of byte 3 is word bit 31, `0x80000000`. The encoder writes that same bit at `0x1508f58: or [rbx+3], 80h`.
- **The address field is 29 bits.** `0x136ed96: and r15d, 1FFFFFFFh` — five `F` nibbles plus a leading `1`, i.e. bits `0..28`, not `0x0FFFFFFF`.
- **The mode nibble is `byte3 & 0x60`.** `0x136ee5a: and eax, 60h` isolates exactly bits 5–6 of byte 3, which are word bits 29–30; bit 7 (`0x80`, RM) is masked out and tested separately. The `0xE0` test at `0x136ee8a` is a different assertion entirely — it requires all three top bits zero on the *data* slot.
- **The PSUM window is `[0x2000000, 0x23FFFFF]`.** `sub r15d, 2000000h; cmp r15d, 3FFFFFh; ja <SBUF>` uses unsigned-greater, so the accept range is `≤ 0x3FFFFF` inclusive — `0x400000` = 4 MiB.
- **`a4` selects vtable `+0x20` (data) over `+0x28` (scale), not the reverse.** `test cl,cl; jne 0x1508ed0` jumps to `call [rax+0x28]` when `a4 != 0`, and the MX leaf passes `a4=1` for the `lea rsi,[rbx+4]` slot, which is the scale slot at `+4`.

### Limits of this reading

Two items are weaker than the rest. The *name* "ACTIVE/dynamic" for bit 30 is read from the shape of the `tensor1d_valid` validation arm rather than from any string literal — the bit's consumer is unambiguous, its label is INFERRED. And the validator only ever tests the aggregate region mask `& 0x1E000000`; it never decodes bits 26..28 individually, so their PSUM bank/partition meaning comes from the encoder's Hwm `(part<<15)+(bank<<11)` math rather than from the decoder.

## Cross-References

- [Tensor Descriptors](tensor-descriptors.md) (2.3) — the descriptor that wraps an ADDR4 + loop words.
- [tensor4d / mempattern4d](tensor4d-mempattern4d.md) (2.4) — the 4-D mem-pattern slot; the MX dual-ADDR4 (data@+0 / scale@+4) layout.
- [mem-pattern 2-D / 3-D](mempattern-2d-3d.md) (2.5) — lower-rank patterns that open with the same ADDR4 word.
- [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md) (1.05) — the physical space the region discriminator targets; the `0x2000000` PSUM base.
- [Penguin middle-end](../penguin/) (5.25) — register-ALU materialization: where the colored register (RM=1) is minted.
- [BIR codegenAccess](../bir/) (Part 7) — the access-pattern lowering (`lower_ap`/`convertSymAP`) that feeds `assignStartAddr`.
