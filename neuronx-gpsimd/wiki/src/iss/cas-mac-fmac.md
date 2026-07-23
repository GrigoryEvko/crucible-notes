# cas/fiss MAC / 2×-FMAC Semantics — the ISS Oracle for the matmul/conv datapath

This page reconstructs the **multiply-accumulate (MAC) and fused-multiply-add (FMAC) family** of the
Vision-Q7 *Cairo* (`ncore2gp`) IVP VLIW vector core *as the cycle-accurate ISS actually models it* —
i.e. as the executable oracle, not just as an ISA table. It is the Part-14 companion to the
per-instruction ISA references [B04 (signed int MAC)](../isa/ref/b04-mac-integer.md),
[B05 (mixed/unsigned/complex MAC)](../isa/ref/b05-mac-mixed.md),
[B17 (fp32 spfma)](../isa/ref/b17-spfma.md) and [B18 (fp16 hp_fma)](../isa/ref/b18-hp-fma.md): those
pages enumerate the mnemonics and their encodings; *this* page shows how the ISS **decodes, times, and
values** them, and drives a real MAC value primitive **live via `ctypes`** to certify the arithmetic.

The reconstruction rests on the two-binary split that the whole ISS is built on (see
[the cas/fiss surface page](cas-core-surface.md)):

* **`libcas-core.so`** (45,878,080 B, ELF64 x86-64, **not stripped**) — the cycle-accurate IVP-core
  model. It owns **DECODE** (per-mnemonic decode-bit lighting) and **TIMING** (the 32-deep stage ring,
  per-unit latency, the latency-aware bypass scoreboard). It computes **no element value** — every
  lane value is delegated to a host TIE-port callback at the execute stage.
* **`libfiss-base.so`** (12,330,016 B, ELF64 x86-64, **not stripped**) — the value oracle. Its 864
  `module__xdref_*` leaves are the actual per-lane arithmetic, drivable directly with `dlopen` +
  `ctypes`. For the MAC family these are the `mula`/`muls`/`mulp`/`mulq`/`mul4t` (integer) and
  `mula_*_{16f,32f}` (soft-float FMA) bodies.

> **Scope in one line.** This page = *cas* DECODE+TIMING **⊕** *fiss* VALUE for the MAC/FMAC family:
> the multiply operands + accumulator model, the widening dtype matrix, the 2×/4×/4T dual-issue
> mechanics, the `a·b+c` value (rounding/saturation, **driven live**), the 1536-bit `wvec`
> wide-accumulator timing (LAT 12), and the fp-vs-int MAC split.

For both `.so` the `.text` and `.rodata` VMA **equals** the file offset (so an `objdump
--start-address=0xNNN` reads the same bytes a runtime `dladdr` would); only `.data.rel.ro` carries the
`+0x200000` VMA→file delta (`readelf -SW libcas-core.so`: `.data.rel.ro` VMA `0x2070900`, file
`0x1e70900`), and it is not used on this page. Confidence tags follow
[the Confidence & Walls model](../reference/confidence-model.md): **OBSERVED** = a byte / immediate /
symbol / **executed** value read out of the shipped binary; **INFERRED** = reasoned over OBSERVED;
**CARRIED** = re-used at a cited page's confidence — crossed with **HIGH** / **MED** / **LOW**. All prose
is static-analysis / live-drive derived from the shipped binaries only.

---

## 1. Key facts

| Fact | Value | Binary source |
|---|---|---|
| MAC family is **two physical tracks** | INTEGER MAC (`s2_mul`, `wvec`) **and** FP FMA (`s3_alu`, vec) | distinct executors + slots ([§2](#2-the-executive-split--two-physical-tracks)) |
| `ivp_mul*` mnemonic roster (cas string pool) | **212** = 142 `MUL` + 23 `MULA` + 47 `MULS` | `strings libcas-core.so \| rg -c '^IVP_MUL'` = 212 `[HIGH/OBSERVED]` |
| Integer-vector MAC ISA cover (FIXUP #999) | **188** = **65** signed (B04) + **123** mixed/uns/cplx (B05) | [B05 §9 partition](../isa/ref/b05-mac-mixed.md#9-the-b04--b05--fp-partition-boundary) `[CARRIED/HIGH]` |
| FP FMA ISA cover | **24** = fp32 `spfma` (B17) + fp16 `hp_fma` (B18) | [B17](../isa/ref/b17-spfma.md) / [B18](../isa/ref/b18-hp-fma.md) `[CARRIED/HIGH]` |
| Integer-MAC issue slot | **`s2 = Mul`** of every wide FLIX format + `N1` | only `F*_S2_Mul` slot strings exist; no `S1/S3/S4_Mul` `[HIGH/OBSERVED]` |
| FP-FMA issue slot | **`s3 = ALU`** | `F*_S3_ALU_*_fp_sem_hp_fma_*` / `*_ivp_sem_spfma_*` `[HIGH/OBSERVED]` |
| Integer accumulator | dedicated **`wvec`** wide file: **4 entries × 1536-bit** | `opnd_sem_wvec_addr` `& 0x3`; `wvt_def` copies 192 B `[HIGH/OBSERVED]` |
| FP accumulator | the **destination vec register** (`d = a·b + c`, 3 vec srcs) | `xdref_mula_*_{16f,32f}` element-width leaf `[HIGH/OBSERVED]` |
| Lane → product → acc widths (int) | 8→24, 16→48, 32→96 (always wider) | `xdref` width-signature ([§4](#4-the-widening-dtype-matrix)) `[HIGH/OBSERVED]` |
| Integer-MAC result latency | **inputs @10 → wide-acc def @12** = **2-cycle, II = 1** | stage12 posts 4 × `mov $0xc,%esi` ([§6](#6-the-cas-timing--lat-12-and-accumulator-forwarding)) `[HIGH/OBSERVED]` |
| FP-FMA result latency | **LAT 10** (= a vector ALU op) | `hp_fma`/`spfma` stage10 `esi=$0xa` `[HIGH/OBSERVED]` |
| Integer accumulate overflow | **wraps mod 2^(acc-width)** (no saturate in the accumulate) | executed `mula_24_24_8_8`, `mula_48_48_16_16` ([§5](#5-the-fiss-value--driven-live)) `[HIGH/OBSERVED]` |
| FP rounding | true FMA — **product kept full-width, single round** of `a·b+c` | soft-float body, RoundMode via `r8/r9` `[HIGH/OBSERVED]` |
| 2×/4× "dual-FMAC" | **operand packing in ONE `s2_mul` op**, not two Mul slots | `mulp` = two `imul` summed; one Mul slot per format `[HIGH/OBSERVED]` |
| Value-leaf join key | **(op, acc-width, in-width)**, not the mnemonic spelling | `module__xdref_<op>_<accW>_<accW>_<inW>_<inW>` `[HIGH/OBSERVED]` |

The reason the family splits into two tracks is precision growth. A matmul/conv inner loop must keep a
*wide* running sum free of intermediate overflow, so the integer MAC reduces an 8/16/32-bit product
into a **24/48/96-bit** accumulator lane held in a *separate* register file (`wvec`). Floating-point,
by contrast, carries its own dynamic range in the exponent, so the FP FMA accumulates **in the
destination vector element itself** with no wide file — and therefore runs in the cheaper ALU slot at
the same latency as any vector ALU op. Everything below is organized around that split.

---

## 2. The executive split — two physical tracks

The single most important fact about the MAC family is that it is **not one op family but two**,
separated by issue slot, accumulator file, and result latency. Both obey the universal cas/fiss
division (cas = decode + timing; fiss = value).

| track | mnemonics (root after `IVP_MUL`) | issue slot | accumulator | cas LAT | fiss value executor |
|---|---|---|---|---|---|
| **INTEGER MAC** (widening) | `MULA*` / `MULS*` / `MULP*` / `MULQ*` / `MUL4T*` | `s2_mul` (Mul unit) | **`wvec`** — dedicated 4-entry **1536-bit** RMW | **12** | `ivp_sem_multiply_semantic` → 4 host ports |
| **FP MAC** (FMA) | `MULAN_2XF32` / `MULANXF16` / `MULSN*` / `MULANN*` / `MULSONE*` | `s3_alu` (ALU unit) | **dest vec reg** (`d = a·b+c`) | **10** | `fp_sem_hp_fma` (fp16) / `ivp_sem_spfma` (fp32) |

The decisive evidence:

* **212 `IVP_MUL*` strings** in cas — `strings libcas-core.so | rg -c '^IVP_MUL'` returns exactly
  `212`, split `142` plain `MUL` / `23` `MULA` (mul-**add** = MAC) / `47` `MULS` (mul-**sub**).
* The integer accumulator is a **dedicated 4-entry 1536-bit `wvec`** file (`opnd_sem_wvec_addr`
  = `and $0x3`; `wvt_def` copies 48 dwords = 192 B); the FP FMA has **no** wide file.
* The fiss MAC value `mula_48_48_16_16` literally computes `acc48 = acc48 + sext48(a16 · b16)` — and
  it returns that, byte-for-byte, when **driven live** ([§5](#5-the-fiss-value--driven-live)).
* The "2× FMAC" is the **PAIR** (`P`) variant — one Mul-slot op summing two products — *not* two FLIX
  Mul slots (every format exposes exactly one `s2_mul`).
* The FP FMA is integer-only **soft-float** (zero `addss`/`mulss`/`vfmadd`) with full IEEE binary16/32
  field logic, executing in the ALU slot at LAT 10.

> **NOTE — naming.** The IVP ISA uses Tensilica `MULA`/`MULS` (mul-add / mul-sub) for the MAC; there is
> **no** `IVP_MAC` or `IVP_FMAC` string. The *fused* FP form surfaces only through the
> `fp_sem_hp_fma` / `ivp_sem_spfma` executors — the only `fma`-named symbols in the image.

### 2.1 The MAC naming grammar

The matmul-relevant tokens (the root immediately after `IVP_MUL`), reconstructed from the cas string
pool and corroborated against the ISA-39 encode roster:

| token | meaning | example |
|---|---|---|
| `A` | **ACCUMULATE** (mul-add): `out += a·b` — the MAC proper | `MULANX16` |
| `S` | mul-**SUBTRACT**: `out -= a·b` (when not the leading saturate `S`) | `MULSNX16` |
| `P` | **PAIR / 2×**: two products summed in one op (the "2×-FMAC") | `MULPN16XR16` |
| `Q` | **QUAD / 4×**: four products in one op | `MULQ2N8XR8` |
| `4T` | **4-TERM** dot-product (four multiplies) | `MUL4TN16XR16` |
| `D` | **DOUBLE** accumulator / dual-output (`DXR` forms) | `MULQ2N8DXR8` |
| `XR8/16` | **X-register source** — the broadcast/reduce weight operand (the matmul weight bus) | `*XR16` |
| `N` / `2N` / `NX` | lane-count / widening disambiguator (`N`=32-lane native) | `MULAN_2X32` |
| `NN` | **NEGATED** product (fp): `out = -(a·b) + c` | `MULANN_2XF32` |
| `SU/US/UU` | signed·unsigned / unsigned·signed / unsigned·unsigned | `MULSUN_2X16X32` |
| `C` / `J` | **COMPLEX** / **CONJUGATE** operand | `MULANX16C` / `…J` |
| `PACKL` | pack-low post-op (narrowing pack of the product) | `MULANX16PACKL` |
| `T` | **PREDICATED** (per-lane `vbool` guard) | `MULANXF16T` |

---

## 3. The cas DECODE — the Mul-slot decode-bit template

The MAC decode follows the same per-`(format, slot, mnemonic)` decode-bit template the rest of the
vector ISA uses, but in the **Mul slot (`s2`)** with a Mul-specific shared executor. The crucial
property — verified here — is that **MAC-vs-plain-MUL, signed-vs-unsigned, the P/Q/4T pairing, and the
saturating/predicated variants are each an INDEPENDENT decode bit**, never a runtime mode toggle.

### 3.1 Per-mnemonic stage0 wrapper — distinct decode bits

The stage0 wrapper for each mnemonic lights one decode bit, records the trace slot/stage, and calls the
shared Mul-slot stage0. Comparing plain `MULNX16` against its MAC sibling `MULANX16` (both F0, both
`s2_mul`, slot id 28):

```text
F0_F0_S2_Mul_28_IVP_MULNX16_inst_stage0    @0xdfde30:
    80 8f 72 0a 00 00 10   orb  $0x10,0xa72(%rdi)   ; LIGHT plain-MUL decode bit  (a72.4)
    movl $0x2,0x4a09dc(%rdi)                        ; trace slot = 2 (Mul)
    movl $0x0,0x4a09e0(%rdi)                        ; trace stage = 0
    call ivp_sem_multiply_opcode_stage0            ; shared Mul-slot stage0
    andb $0xef,0xa72(%rbx) ; ret                   ; clear-on-return

F0_F0_S2_Mul_28_IVP_MULANX16_inst_stage0   @0xdfddf0:   (the MAC sibling)
    80 8f 73 0a 00 00 10   orb  $0x10,0xa73(%rdi)   ; LIGHT MAC decode bit  (a73.4 — DIFFERENT byte)
    …identical structure… call ivp_sem_multiply_opcode_stage0 …
```

The plain multiply lights byte `0xa72`, the MAC lights byte `0xa73` — **distinct opcodes routed
through the same Mul-slot decode/executor**, exactly as the formal ADD-vs-SUB split in
[group-semantics-I](../isa/semantics/group-semantics-i.md#2-group-2--multiply--mac-ivp_sem_mul_slice).

### 3.2 The shared Mul-slot executor and the four host ports

The shared integer datapath dispatch is `…ivp_sem_multiply_semantic_stage10`. For F0 it lives at
`0xe12ec0` (`0x1075` B). Structurally it is a long `testb $bit,0xNNN(%rbx) ; jne <compute>` chain over
every MAC/MUL decode bit (plus fast multi-bit `test $imm,0xNNN(%rbx)` probes that check several
MAC-class bits at once), funnelling to the **EXECUTE** stage (`esi=$0xa`, stage 10) — but with **four**
adjacent host vtable slots rather than one:

```text
F0 ivp_sem_multiply_semantic_stage10  @0xe12ec0 :
    … mov $0xa,%esi ;  call *0x273bf0(%rbx)    ; host port 0  (rdi = host base 0x15200)
    … mov $0xa,%esi ;  call *0x273bf8(%rbx)    ; host port 1
    … mov $0xa,%esi ;  call *0x273c00(%rbx)    ; host port 2
    … mov $0xa,%esi ;  call *0x273c08(%rbx)    ; host port 3  (rdi = 0x15380 — SECOND host ctx)
```

The four ports write the **four 512-bit lanes/halves of the 1536-bit `wvec`** product/accumulator
(3 × 512 + a carry/overflow lane). cas itself computes **no product** — the element value is delegated
to the host value port (the fiss `xdref` oracle), keyed by whichever MAC decode bit is live. The
`esi=$0xa` immediate is the *execute-stage* read latency (10); the *result* def latency (12) is posted
later, in stage12 ([§6](#6-the-cas-timing--lat-12-and-accumulator-forwarding)).

> **GOTCHA — port count ≠ four operands.** The four `0x273bf0…c08` calls are **not** four operands; they
> are the four 512-bit *halves* of the single wide accumulator. The MAC reads two vector operands (or a
> vector + an XR weight) and one wide accumulator; the four ports are the *write* fan-out across the
> 1536-bit result. (Lane→port map is count-and-width-matched but not byte-traced — `[MED/INFERRED]`.)

---

## 4. The widening dtype matrix

The accumulator is **always wider** than the product, and the product wider than the inputs — the
matmul precision-growth ladder. The widths are pinned from the fiss `xdref` width-signature
(`module__xdref_<op>_<accW>_<accW>_<inW>_<inW>`) and cross-checked against the B04/B05 mnemonic widths.
Every body below was read by `objdump` at the address shown.

| inputs | product | accumulator | fiss primitive (the MAC body) | addr |
|---|---|---|---|---|
| `i8 × i8` | 24-bit | **24-bit** | `module__xdref_mula_24_24_8_8` | `0x68a820` |
| `i16 × i16` | 32-bit | **48-bit** | `module__xdref_mula_48_48_16_16` | `0x68a6b0` |
| `i32 × i32` | 64-bit | **96-bit** | `module__xdref_mula_96_96_32_32` | `0x832e60` |
| `i16 × i32` | 48-bit | **96-bit** | `module__xdref_mulan_2x16x32_*_96_96_32_32` (lo/hi) | `0x68ab40`/`0x68ad20` |
| `i8 × i16` | 48-bit | **48-bit** | `module__xdref_mulai2nx8x16_48_48_16_16_16` | — |
| `32c × 32c` | 96c | **96c** | `module__xdref_mula_96c_96c_32c_32c` | `0x8336f0` |
| `32j × 32c` (conj) | 96c | **96c** | `module__xdref_mula_96c_96c_32j_32c` | `0x8338a0` |
| `fp16 × fp16` | fp16 | **fp16** (vec) | `module__xdref_mula_1_1_1_1_16f_16f_16f_16f_2` | `0x5252b0` |
| `fp32 × fp32` | fp32 | **fp32** (vec) | `module__xdref_mula_1_1_1_1_32f_32f_32f_32f_2` | `0x1a6b30` |

**Signedness is selected in the extension prologue** (the `movs*l` vs `movz*l` choice at the top of the
body), not by a separate opcode track:

| variant | extension | fiss primitive |
|---|---|---|
| `mula` | both `movs*l` (signed × signed) | `xdref_mula_*` |
| `muluua` | raw (unsigned × unsigned) | `module__xdref_muluua_48_48_16_16` `@0x68a720` |
| `mulusa` / `mulsua` | one `movs*l`, one `movz*l` (u×s / s×u) | `xdref_mulus*` / `xdref_mulsu*` |

The **full-tile** (1536-bit) accumulator forms are the matmul spine: `module__xdref_mul4tan16xr16_1536_1536_512_512_64`
`@0x850980` (4-term `i16` MAC into a 1536-bit acc, two 512-bit operand vectors, `64` = lane/reduce
stride) and `module__xdref_mul4ta2n8xr8_1536_1536_512_512_64` `@0x818520` (the `i8` 4-term form). The
`1536` in the signature is the byte-level confirmation of the 192-byte `wvt_def` copy
([§3.2 note](#32-the-shared-mul-slot-executor-and-the-four-host-ports) and
[§7](#7-the-accumulator-model--a-1536-bit-4-entry-rmw-regfile)).

Lane counts follow the 512-bit datapath: `2NX8` = 64 `i8` lanes, `NX16` = 32 `i16`, `N_2X32` = 16
`i32`, `NXF16` = 32 fp16, `N_2XF32` = 16 fp32. `[HIGH/INFERRED from the 512-bit width + lane tokens]`

---

## 5. The fiss VALUE — driven live

cas delegates the element value to the fiss `xdref` oracle. Below the bodies are first read by
disassembly, then **executed through `ctypes`** to certify `a·b+c`. The MAC ABI is the 4-operand
oracle ABI: `A` in the accumulator slot, `B`/`C` the two factors, the result written through `(%r8)`.

### 5.1 The int8 MAC body — `module__xdref_mula_24_24_8_8 @0x68a820`

```text
68a820:  0f be d2          movsbl %dl,%edx          ; B = sext8 (arg3 / dl)
68a823:  0f be c9          movsbl %cl,%ecx          ; C = sext8 (arg4 / cl)
68a826:  0f af d1          imul   %ecx,%edx         ; product = B * C   (signed 8×8 → ≤16-bit)
68a829:  01 d6             add    %edx,%esi         ; acc += product    (acc in arg2 / esi)
68a82b:  81 e6 ff ff ff 00 and    $0xffffff,%esi    ; mask to 24 bits   (WRAP, no saturate)
68a831:  41 89 30          mov    %esi,(%r8)        ; store acc24 through arg5 / r8
68a834:  c3                ret
```

`acc24 = (acc + a8·b8) mod 2^24`. Signed product, **wrapping** accumulate (mask to acc width), no
rounding (it is integer). ABI: `A=esi` (acc in-value), `B=dl`, `C=cl`, result `*=r8`.

### 5.2 The int16 MAC body — `module__xdref_mula_48_48_16_16 @0x68a6b0`

```text
68a6b0:  mov (%rsi),%eax           ; read acc48 low-32   (arg2 = acc pointer, low32 @+0, high16 @+4)
68a6b2:  movswl %cx,%ecx           ; B = sext16 (arg4 / cx)
68a6b5:  movswl %dx,%edx           ; C = sext16 (arg3 / dx)
68a6b8:  imul   %ecx,%edx          ; product = B * C   (signed 16×16 → 32-bit)
         …carry-careful two-word combine of acc.low32 / acc.high16…
68a6df:  sar $0x1f,%edx            ; SIGN-EXTEND the 32-bit product …
68a6e2:  movzwl %dx,%edx           ; … to 48 bits
68a6e5:  shl $0x20,%rdx
68a6ec:  add %rax,%rdx             ; acc48 += sext48(product)
68a6ef:  mov %edx,(%r8)            ; store acc48 low-32
68a6fc:  mov %edx,0x4(%r8)         ; store acc48 high-16 (after >>32 & 0xffff)
```

`acc48 = acc48 + sext48(a16·b16)`. The 32-bit product is sign-extended to 48 bits and added into the
two-word (low32 + high16) accumulator; the `-1`/`+1 lea` sequences are the careful two-word
add-with-sign that avoids a spurious carry. ABI: `A=(%rsi)` (acc pointer), `B=cx`, `C=dx`, result
`*=r8`.

> **CORRECTION — the operand order is `(acc, dx, cx)`, not `(acc, cx, dx)`.** The body multiplies
> `movswl %cx` × `movswl %dx`, i.e. arg4 (`rcx`) × arg3 (`rdx`); when calling through `ctypes` you must
> pass the two factors in the **rdx-then-rcx** SysV slots or the product is unaffected (multiplication
> commutes) but any *future* asymmetric form (`mulus`/`mulsu`) would silently swap signedness. The drive
> below passes them in the binary's order to keep the rig correct for the asymmetric leaves.

### 5.3 The live `ctypes` drive (VAL-03)

The keystone certification: `dlopen` the value oracle, resolve the two MAC leaves, and execute them on
known operand triples — confirming the disassembled semantics produce the right `a·b+c` *at runtime*.
This is the live half of the VAL-03 validation harness.

```python
import ctypes, struct

FISS = (".../ncore2gp/config/libfiss-base.so")   # absolute path; .text VMA == file offset
lib  = ctypes.CDLL(FISS)

# --- int8 MAC: acc24 = (acc + b*c) mod 2^24 ---
# ABI @0x68a820:  arg1 rdi=unused, arg2 esi=acc, arg3 dl=B, arg4 cl=C, arg5 r8=result*
f8 = lib.module__xdref_mula_24_24_8_8
f8.restype  = None
f8.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
               ctypes.POINTER(ctypes.c_uint32)]

def mac8(acc, b, c):
    out = ctypes.c_uint32(0)
    f8(None, acc, b, c, ctypes.byref(out))
    return out.value

# --- int16 MAC: acc48 = acc + sext48(b*c) ---
# ABI @0x68a6b0:  arg2 rsi=acc* (low32@+0, high16@+4), arg3 rdx=C, arg4 rcx=B, arg5 r8=result*
f16 = lib.module__xdref_mula_48_48_16_16
f16.restype  = None
f16.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

def mac16(acc48, b, c):
    a = (ctypes.c_uint8 * 8)(); struct.pack_into("<IH", a, 0, acc48 & 0xffffffff, (acc48 >> 32) & 0xffff)
    o = (ctypes.c_uint8 * 8)()
    f16(None, ctypes.byref(a), c, b, ctypes.byref(o))      # pass factors in rdx-then-rcx order
    lo, hi = struct.unpack_from("<I", o, 0)[0], struct.unpack_from("<H", o, 4)[0]
    return lo | (hi << 32)
```

Executed (all triples returned **exactly** the reference `a·b+c`):

| primitive | acc | b | c | got | reference | |
|---|---:|---:|---:|---|---|---|
| `mula_24_24_8_8` | 0 | 3 | 4 | `0x00000c` (12) | `(0 + 12) & 0xffffff` | ✓ |
| `mula_24_24_8_8` | 100 | −5 | 6 | `0x000046` (70) | `(100 − 30) & 0xffffff` | ✓ |
| `mula_24_24_8_8` | 0 | −128 | −128 | `0x004000` (16384) | signed `(−128)²` | ✓ |
| `mula_24_24_8_8` | 0 | −1 | 1 | `0xffffff` | **24-bit wrap** of −1 | ✓ |
| `mula_48_48_16_16` | 0 | 300 | 400 | `0x0000_0001_d4c0` (120000) | `0 + 120000` | ✓ |
| `mula_48_48_16_16` | 1000 | −200 | 50 | `0xffff_ffff_dcd8` (−9000) | **48-bit two's-complement** | ✓ |
| `mula_48_48_16_16` | 0 | −32768 | −32768 | `0x0000_4000_0000` (2³⁰) | signed `(−2¹⁵)²` | ✓ |
| `mula_48_48_16_16` | 0 | −1 | 1 | `0xffff_ffff_ffff` | **48-bit wrap** of −1 | ✓ |

> **QUIRK — the accumulate WRAPS, it does not saturate.** `(−1)` lands at `0xffffff` (int8 acc) and
> `0xffffffffffff` (int16 acc): the accumulate is pure modular `mod 2^(acc-width)`. Saturation, when a
> mnemonic asks for it, happens only in the **`PACKL` narrowing post-op** (`xdref` pack-saturate), never
> inside the MAC accumulate. This matters for a reimplementation: a 256-deep `i8` MAC chain that would
> overflow 24 bits *silently wraps* — the headroom (24 vs 8+8=16 product bits ⇒ ~256 sums) is the
> architectural guarantee, and the firmware is responsible for not exceeding it.

### 5.4 Mul-subtract, unsigned, and the pair (2×) bodies

* **`module__xdref_muls_48_48_16_16 @0x68a840`** — byte-identical to the int16 MAC except the final
  `48 29 d0  sub %rdx,%rax` ⇒ `acc48 = acc48 − sext48(a16·b16)`. (The `MULS` root = mul-sub; do not
  confuse with the leading saturate `S`.)
* **`module__xdref_muluua_48_48_16_16 @0x68a720`** — same shape, raw `imul` (no sign-extend) ⇒ unsigned
  product.
* **The 2×-FMAC core — `module__xdref_mulp_48_16_16_16_16 @0x816030`** calls the
  `module__xdref_mul_48_16_16_8@plt` (16×16→48) primitive **twice** and sums the two products:

  ```text
  816030:  …             call module__xdref_mul_48_16_16_8@plt   ; product1 = a1 * b1
  816061:                call module__xdref_mul_48_16_16_8@plt   ; product2 = a2 * b2
  81609d:  48 01 d0      add  %rdx,%rax                          ; PAIR = product1 + product2
           store 48-bit PAIR
  ```

  `mulp = a1·b1 + a2·b2`. Its accumulate sibling **`module__xdref_mulpa_48_48_16_16_16_16 @0x8160c0`**
  calls `mulp` then adds the wide accumulator: `acc48 = acc48 + (a1·b1 + a2·b2)` — **two FMACs per
  op**. `mulps @0x816130` is the pair-subtract sibling; `mulq*`/`mul4t*` extend this to four products.
* **The XR (reduce-register) MAC — `module__xdref_mula2n8xr16_24_24_8_64 @0x5c10b0`** sources one
  factor from the **XR broadcast/reduce register** (`movswl (%rcx),%eax`, the matmul weight bus) and the
  other from the activation lane (`movsbl %dl`): `movswl (%rcx),%eax ; movsbl %dl,%edx ; imul %eax,%edx ;
  add %edx,%esi ; and $0xffffff` — the matmul inner-product MAC.

### 5.5 The FP FMA — integer-only soft-float, single round

`module__xdref_mula_1_1_1_1_16f_16f_16f_16f_2 @0x5252b0` (`0xc52` B) and the fp32
`…_32f_32f_32f_32f_2 @0x1a6b30` (`0x1983` B) are **integer-only soft-float**: `objdump -d` over the fp16
body finds **zero** `addss`/`subss`/`mulss`/`divss`/`vfmadd`/`cvtss` — instead, full IEEE binary16 field
extraction:

```text
5252bb:  41 c1 ea 0a      shr $0xa,%r10d           ; exponent field  (bits 14..10)
5252cb:  41 83 e2 1f      and $0x1f,%r10d          ;   5-bit exponent
5252d8:  41 81 e6 ff03..  and $0x3ff,%r14d         ;  10-bit mantissa
5252e7:  41 83 fa 1f      cmp $0x1f,%r10d           ; NaN/Inf test (exp == 0x1f)
```

The fp32 body uses `shr $0x17` (8-bit exponent), `cmp $0xff` (NaN/Inf). Both compute `d = a·b + c` as a
**true** fused multiply-add: the product is kept at full internal width *before* the add, and a
**single** rounding is applied to `(a·b+c)` (the defining FMA property — no double-round). Round-mode and
exception flags are parameter-driven via `r8`/`r9` (the `RoundMode` SR plumbed by the caller); a status
word is written back. The FP MAC family shapes:

| mnemonic root | value | fiss leaf |
|---|---|---|
| `mulan` | `d = a·b + c` | `xdref_mula_*_{16f,32f}` |
| `mulsn` | `d = c − a·b` | `xdref_mulsn_*_{16f,32f}` |
| `mulann` (`NN`) | `d = −(a·b) + c` | `xdref_mulann_*` `[HIGH name / MED exact sign]` |
| `mulsone` | FMA-with-one variant | `xdref_mulsone_*` `[HIGH name / MED exact form]` |

`[HIGH for soft-float + IEEE field logic / OBSERVED ; MED for exact round-tie-break / INFERRED]`

---

## 6. The cas TIMING — LAT 12 and accumulator forwarding

### 6.1 Result latency — wide-acc def @12, FP-FMA @10

The integer MAC writes the wide accumulator with **LAT 12 — the deepest unit** (multiply +
accumulate), versus the generic vec/ALU LAT 10. The F0 multiply stage12 (`@0xe11410`) posts **four**
def reservations, each preceded by `mov $0xc,%esi` (= 12) into the wide-accumulator lane ports:

```text
F0 ivp_sem_multiply_semantic_stage12.isra.71  @0xe11410 :
  e118d2:  be 0c 00 00 00   mov $0xc,%esi ;  ff 93 20 3c 27 00  call *0x273c20(%rbx)   ; wide-acc lane 0
  e11903:  be 0c 00 00 00   mov $0xc,%esi ;  call *0x273c28(%rbx)                       ; lane 1
  e1192d:  be 0c 00 00 00   mov $0xc,%esi ;  call *0x273c30(%rbx)                       ; lane 2
  e11989:  be 0c 00 00 00   mov $0xc,%esi ;  …                                          ; lane 3
```

The `0xc` lands in the `wvec` ready-cycle column (`0x12b84`, [§7](#7-the-accumulator-model--a-1536-bit-4-entry-rmw-regfile)).
The FP FMA (ALU slot) by contrast loads `esi=$0xa` (= 10) at its stage10 host handoffs
(`fp_sem_hp_fma_semantic_stage10 @0x15298b0`, fp16; `ivp_sem_spfma_semantic_stage10 @0x152ba40`,
fp32) — the same latency as any vector ALU op.

| op | slot | result LAT | evidence |
|---|---|---|---|
| integer widening MAC (`wvec`) | `s2_mul` | **12** (deepest) | stage12 four `mov $0xc,%esi` def-posts `[HIGH/OBSERVED]` |
| fp16 / fp32 FMA (vec) | `s3_alu` | **10** (= vector ALU) | hp_fma/spfma stage10 `esi=$0xa` `[HIGH/OBSERVED]` |
| plain integer multiply (vec dst) | `s2_mul` | **10** | the vec-dst histogram `[HIGH/MED]` |

Inputs are read at stage 10, the wide-acc result is posted at stage 12, so the MAC has **2-cycle
result latency with II = 1** — consistent with the [B04 key-facts](../isa/ref/b04-mac-integer.md#1-key-facts)
framing of "inputs `@10` → result `@12`".

### 6.2 Accumulator-chain forwarding — the matmul throughput property

`my_wvec_stall @0x17a7ac0` is the accumulator consumer interlock — the same latency-aware bypass
scoreboard the vector files use, applied to `wvec`. It scans an interlock window of **13** ring slots
(`r8 = 1..0xd`), reads each in-flight `wvec` DEF's reg# (`0x12a04`), valid flag (`0x12b04`) and
ready-cycle (`0x12b84`), and for a future stage that reserves the **same** accumulator reg# computes
`avail = ready_cycle − stage_distance(r8)`, stalling iff the consumer's use-LAT exceeds it:

```text
mov 0x12b84(%rcx),%r10d ; sub %r8d,%r10d ; cmp %esi,%r10d ; jl <stall>
```

i.e. a dependent MAC stalls **only if** the prior MAC's accumulator result has not propagated far enough
to be **forwarded**. So back-to-back MACs to the **same** accumulator do **not** each pay the full
12-cycle latency — they pay only the un-bypassable residue, letting a MAC chain sustain near
1 MAC/cycle issue once the bypass window is satisfied. This is the matmul-throughput property.
`[HIGH/OBSERVED for the forwarding mechanism ; MED for the exact "1/cycle sustained" rate — that depends
on the host port model, not enumerated in cas]`

Commit drains the accumulator value into the regfile commit array N stages later;
`my_wvec_commit_value` / `my_wvec_set_commit_value @0x1765a00/0x1765a50` are the `wvec`-specific commit
hooks. The MAC also shares the Mul-slot writeback-port and multi-slot-dispatch stall chains (a single
`s2` Mul unit per FLIX format), so two *independent* MACs cannot co-issue from one bundle except via the
P/Q/4T packing ([§8](#8-the-2--4-dual-issue-mechanics--packed-not-co-issued)).

---

## 7. The accumulator model — a 1536-bit 4-entry RMW regfile

The integer MAC's accumulator is a **dedicated** wide register file, **not** the general `vec` file.

**Identity & size.** `opnd_sem_wvec_addr @0x17aa290` is `mov %esi,%eax ; and $0x3,%eax ; ret` — the
`wvec` file has exactly **4 entries** (`& 0x3`). Compare `vec` = `& 0x1f` (32), `gvr` = `& 0x7` (8),
`b32_pr` = `& 0xf` (16). The width is **1536 bits**: `my_wvec_2_opnd_ivp_sem_multiply_wvt_def @0x17a80b0`
copies the accumulator value word-by-word — 48 dwords, offsets `0x00`…`0xbc(%rdx)` (the last,
`mov 0xbc(%rdx),%edx`, is loaded into `%edx` not `%ecx`, which is why a `,%ecx`-only grep undercounts to
47) — = **192 bytes = 1536 bits = 3 × 512-bit vector registers**.

> **GOTCHA — 48 dwords, not 47.** The copy loop in `wvt_def` is `mov N(%rdx),%ecx ; mov %ecx,M(%rsp)` for
> offsets `0x00`…`0xb8`, **plus a trailing `mov 0xbc(%rdx),%edx`** (different dest register). Counting
> only `…,%ecx` loads gives 47 dwords (188 B); the true copy is 48 dwords (192 B = 1536 bit). Verify with
> `objdump -d --start-address=0x17a80b0 --stop-address=0x17a82b0` and look for the `0xbc(%rdx),%edx` tail.

**Read-modify-write.** The MAC reads **and** writes the same `wvec` accumulator:

* `my_wvec_2_opnd_ivp_sem_multiply_wvu_set_use @0x17a7d20` — the accumulator **READ** ("wvu" =
  wide-vector use): `eax = wvec_ringbase(0xc580) & 0x1f`; record reg# at `0x12d04(,eax,4)`; set the
  hazard-use bit `(1<<cl)` in `0xc578`.
* `my_wvec_2_opnd_ivp_sem_multiply_wvt_set_def @0x17a7db0` — the accumulator **WRITE** ("wvt" =
  wide-vector target), byte-exact:

  ```text
  17a7dbd:  c7 80 04 2b 01 00 01  movl $0x1,0x12b04(%rax)   ; valid flag
  17a7dc7:  89 b0 84 2b 01 00     mov  %esi,0x12b84(%rax)   ; READY-CYCLE = the LAT (§6, esi=12)
  17a7dcd:  89 90 04 2a 01 00     mov  %edx,0x12a04(%rax)   ; accumulator reg#
  17a7dd3:  81 8f 74 c5 00 00 00  orl  $0x1000,0xc574(%rdi) ; wvec def/dirty mask
  17a7ddd:  c3                    ret
  ```

Both `wvu` (read) and `wvt` (write) address the **same** `wvec` file ⇒ the accumulator is a
**same-regfile read-modify-write** operand: `acc ← acc (via wvu) + a·b (via wvt)`. It is *not* an
implicit single global accumulator and *not* a vec-dst RMW — it is its own wide file with explicit
src(`wvu`)/dst(`wvt`) roles, the classic back-to-back MAC chain.

**Init / carry.** The accumulator is an architectural register; "init to 0" / "carry across MACs" is the
caller's job (zero the `wvec` before the first MAC, then chain). cas schedules only the `wvu`-read /
`wvt`-write reg# + latency; the **value** is host-resident. `[HIGH role / MED for the explicit zero-init
op being `wvec_pack`]`

**Pack / drain** bracket a MAC chain: `ivp_sem_wvec_pack_semantic` (S1_Ld slot, stage3) packs `vec`
lanes **into** `wvec` (zero/seed the accumulator), and `ivp_sem_unpack_wvec_mov_semantic` (S2_Mul,
stage12) moves the `wvec` result **out** to a `vec` register for the next stage / requant.

---

## 8. The 2× / 4× dual-issue mechanics — packed, not co-issued

The "dual-FMAC" (2 MACs per instruction) is realised by **operand packing within a single Mul-slot
op**, *not* by two FLIX Mul slots:

* **Exactly one Mul slot per FLIX format.** Only `F*_S2_Mul` / `N1_S2_Mul` slot strings exist — there is
  **no** `S1_Mul`, `S3_Mul`, or `S4_Mul` anywhere in cas. The Mul unit sits at `s2` in every wide format
  F0…F11.
* **PAIR (`P`) packs two multiplies + a sum into one op** (`mulp = a1·b1 + a2·b2`,
  [§5.4](#54-mul-subtract-unsigned-and-the-pair-2-bodies)); **QUAD (`Q`)** packs four; **4T** packs four
  (4-term dot). The accumulate variants (`PA`/`QA`/`4TA`) add that packed sum to the wide accumulator.
* **Operand sourcing:** the two/four factor-pairs come from adjacent lanes of the operand vectors, and
  the shared factor (the **weight**) from the **XR reduce-register** (the `*XR8`/`*XR16` token; the body
  reads it via `(%rcx)`, [§5.4](#54-mul-subtract-unsigned-and-the-pair-2-bodies)). So a 2×/4× MAC reads:
  activation-vector lanes + **one XR weight register** + the wide accumulator (RMW). It is a
  dot-product / reduction primitive — the matmul inner loop folds K input pairs per issued op (2 for P,
  4 for Q/4T). `[HIGH for the packing + XR source / OBSERVED ; MED for the exact lane-pairing order]`
* **`D` (DOUBLE / `DXR`)** produces a dual-output 1536-bit accumulator
  (`xdref_mulq2n8dxr8_*`, the `512`-suffix full-vector forms) — two accumulator results from one
  quad-MAC. `[HIGH presence / OBSERVED ; MED for the exact dual-output lane mapping]`

> **NOTE.** "2× FMAC" therefore means **one Mul-slot op, one accumulator, two products
> summed-and-accumulated**: the parallelism lives in the multiplier array (2/4 multipliers feeding one
> adder tree into the wide accumulator), surfaced as a single opcode — not in a second issue unit.

---

## 9. fp vs int MAC — and the predicate `_t` forms

| axis | INTEGER MAC | FP MAC (FMA) |
|---|---|---|
| issue slot | `s2_mul` (Mul unit) | `s3_alu` (ALU unit) |
| accumulator | dedicated 1536-bit `wvec` (4 entries), RMW (`wvu` read + `wvt` write) | same-as-dst vec register (`d = a·b + c`, 3 vec srcs) |
| cas executor | `ivp_sem_multiply_semantic` | `fp_sem_hp_fma` (fp16) / `ivp_sem_spfma` (fp32) |
| cas LAT | **12** (deepest) | **10** (= vector ALU) |
| fiss value | `mula`/`muls`/`mulp`/`mulq`/`mul4t` (**wrap** mod acc-width, no round) | `mula_*_{16f,32f}` soft-float (IEEE FMA, **single round**, flags) |
| widening | 8→24, 16→48, 32→96 (acc ≫ product) | none (fp keeps element width) |

**Predicate `_t` forms** (`IVP_MULANXF16T`, `IVP_MULSN_2XF32T`, `IVP_MULANX16PACKLT`, …): the trailing
`T` is the per-lane `vbool`-predicated MAC — lanes whose guard is false **retain the prior
accumulator**. They are **independent decode bits / opcodes** (same template, an added `vbool` operand
role), exactly as ADD-vs-ADDT. The per-lane execute loop applies a bitkill mask on the disabled lanes.
`[HIGH for presence + the predicated-decode model / OBSERVED ; MED for the exact bitkill wiring in the
MAC path / INFERRED]`

---

## 10. Matmul relevance — the PE inner loop

This page reconstructs the *engine*; the [PE matrix-multiply firmware path](../firmware/kernels/pe-matmul.md#4-matmul-0x02--stream-the-moving-acts-mac-into-psum)
reconstructs the *driver* (`Ldweights` / `Matmul` / `PeManageSeed`). What this page establishes for that
join:

* The matmul inner loop on the NX core is the **integer widening MAC** (`MULA*`/`MULPA*`/`MULQA*`/`MUL4TA*`
  in `s2_mul`) folding into the **4-entry 1536-bit `wvec`** accumulator; the **weight** is broadcast via
  the **XR reduce-register** (the `*XR8`/`*XR16` operand), the **activation** via the vector operand.
* A matmul tile is: **zero/seed** the `wvec` (`ivp_sem_wvec_pack`) → run a chain of `PA`/`QA`/`4TA` MACs
  (2/4 input pairs each, accumulator-forwarded back-to-back inside the LAT-12 bypass window) → **drain**
  via `ivp_sem_unpack_wvec_mov` to a `vec` register for the next stage / requant.
  `[HIGH for the op-sequence presence / INFERRED for the exact firmware lowering — that join is the PE
  firmware page's to make]`
* fp matmul (where present) would use the ALU-slot FMA (`MULAN_2XF32`/`MULANXF16`) accumulating **in a
  vec register** — no wide accumulator, LAT 10. `[HIGH for the op / MED for FW use]`

---

## 11. Adversarial self-verify ledger

The five strongest claims and their evidence:

| claim | challenge | result |
|---|---|---|
| `wvec` = 1536-bit | naive `,%ecx`-only grep counts 47 dwords (188 B) | **48 dwords** confirmed — the `0xbc(%rdx),%edx` tail closes 192 B = 1536 bit `[HIGH/OBSERVED]` |
| 2×/4×/4T is packing, not co-issue | could a second Mul slot exist? | **no** `S1/S3/S4_Mul` strings; only `S2_Mul` × F0…F11; `mulp` = two `imul` summed `[HIGH/OBSERVED]` |
| live fiss MAC value | does the disassembly survive execution? | **8/8 triples** matched `a·b+c` incl. 24-bit & 48-bit **wrap** and signed products `[HIGH/OBSERVED]` |
| wide-acc LAT 12 | is 12 the *result* def, not just a read? | stage12 posts **4 × `mov $0xc,%esi`** def-posts into `wvec` ready-cycle `0x12b84` `[HIGH/OBSERVED]` |
| FP FMA = soft-float | any hardware FP slip-through? | **zero** `addss`/`mulss`/`vfmadd`; IEEE field extract (`shr $0xa`,`and $0x3ff`,`cmp $0x1f`) `[HIGH/OBSERVED]` |

**Honesty ledger.** *HIGH/OBSERVED:* the 212/142/23/47 roster; MAC-vs-MUL distinct decode bits (`a72`
vs `a73`); the four stage10 host ports (`0x273bf0…c08`) + two host contexts (`0x15200`/`0x15380`); the
4-entry 1536-bit `wvec` RMW (`wvu`/`wvt`); LAT 12 (stage12 `mov $0xc,%esi`) vs FP-FMA LAT 10
(`esi=$0xa`); `my_wvec_stall` window-13 forwarding; the live `mula_24_24_8_8` / `mula_48_48_16_16`
values; `muls`(sub) / `muluua`(unsigned) / `mulp`(pair) bodies; the XR `(%rcx)` reduce-register source;
the 1536-bit `mul4ta*` full-tile forms; FP soft-float IEEE field logic. *MED/INFERRED:* the four ports →
four 512-bit halves map (count+width match, not byte-traced); zero-init = `wvec_pack`; pair/quad
lane-pairing order and the `D` dual-output map; `mulann` exact sign / `mulsone` exact form; FP
round-tie-break table; the exact sustained MAC/cycle rate; the `PACKL` saturating-narrow binding.
*CARRIED:* the **65 / 123 / 188 / 24** B04/B05/FP partition (FIXUP #999, from
[B05 §9](../isa/ref/b05-mac-mixed.md#9-the-b04--b05--fp-partition-boundary)).

---

## 12. Function & symbol map

| symbol | addr | binary | role |
|---|---|---|---|
| `F0_F0_S2_Mul_28_IVP_MULNX16_inst_stage0` | `0xdfde30` | cas | plain-MUL decode bit (`orb $0x10,0xa72`) |
| `F0_F0_S2_Mul_28_IVP_MULANX16_inst_stage0` | `0xdfddf0` | cas | MAC decode bit (`orb $0x10,0xa73`) |
| `ivp_sem_multiply_opcode_stage0` | — | cas | shared Mul-slot stage0 |
| `F0_F0_S2_Mul_28_ivp_sem_multiply_semantic_stage10` | `0xe12ec0` | cas | execute; 4 host ports `0x273bf0…c08`, `esi=$0xa` |
| `F0_F0_S2_Mul_28_ivp_sem_multiply_semantic_stage12.isra.71` | `0xe11410` | cas | def-post; 4 × `mov $0xc,%esi` (LAT 12) |
| `opnd_sem_wvec_addr` | `0x17aa290` | cas | `wvec` index: `& 0x3` (4 entries) |
| `my_wvec_2_opnd_ivp_sem_multiply_wvu_set_use` | `0x17a7d20` | cas | accumulator READ (`wvu`) |
| `my_wvec_2_opnd_ivp_sem_multiply_wvt_set_def` | `0x17a7db0` | cas | accumulator WRITE (`wvt`); ready-cycle `0x12b84` |
| `my_wvec_2_opnd_ivp_sem_multiply_wvt_def` | `0x17a80b0` | cas | 192-byte (1536-bit) accumulator value copy |
| `my_wvec_stall` | `0x17a7ac0` | cas | accumulator-chain bypass (window 13) |
| `my_wvec_commit_value` / `…set_commit_value` | `0x1765a00` / `0x1765a50` | cas | `wvec` commit hooks |
| `ivp_sem_wvec_pack_semantic` (S1_Ld) | — | cas | pack `vec` → `wvec` (seed) |
| `ivp_sem_unpack_wvec_mov_semantic` (S2_Mul) | — | cas | drain `wvec` → `vec` |
| `F0_F0_S3_ALU_*_fp_sem_hp_fma_semantic_stage10` | `0x15298b0` | cas | fp16 FMA, LAT 10 |
| `F0_F0_S3_ALU_*_ivp_sem_spfma_semantic_stage10` | `0x152ba40` | cas | fp32 FMA, LAT 10 |
| `module__xdref_mula_24_24_8_8` | `0x68a820` | fiss | int8 MAC value (**driven live**) |
| `module__xdref_mula_48_48_16_16` | `0x68a6b0` | fiss | int16 MAC value (**driven live**) |
| `module__xdref_mula_96_96_32_32` | `0x832e60` | fiss | int32 MAC value |
| `module__xdref_muls_48_48_16_16` | `0x68a840` | fiss | int16 mul-subtract |
| `module__xdref_muluua_48_48_16_16` | `0x68a720` | fiss | int16 unsigned MAC |
| `module__xdref_mulp_48_16_16_16_16` | `0x816030` | fiss | PAIR (2×): `a1·b1 + a2·b2` |
| `module__xdref_mulpa_48_48_16_16_16_16` | `0x8160c0` | fiss | PAIR-accumulate (2× MAC) |
| `module__xdref_mula2n8xr16_24_24_8_64` | `0x5c10b0` | fiss | XR reduce-register MAC |
| `module__xdref_mul4tan16xr16_1536_1536_512_512_64` | `0x850980` | fiss | 4-term `i16` full-tile MAC |
| `module__xdref_mul4ta2n8xr8_1536_1536_512_512_64` | `0x818520` | fiss | 4-term `i8` full-tile MAC |
| `module__xdref_mula_1_1_1_1_16f_16f_16f_16f_2` | `0x5252b0` | fiss | fp16 FMA (soft-float, single round) |
| `module__xdref_mula_1_1_1_1_32f_32f_32f_32f_2` | `0x1a6b30` | fiss | fp32 FMA (soft-float, single round) |

---

## See also

* [libcas-core Surface + ISS Plugin ABI](cas-core-surface.md) — the cas/fiss decode/timing/value split.
* [ISA Batch 04 — Integer MAC Matrix (signed)](../isa/ref/b04-mac-integer.md) — the 65 signed mnemonics.
* [ISA Batch 05 — Mixed/Unsigned/Complex MAC](../isa/ref/b05-mac-mixed.md) — the 123 mixed mnemonics + the 65/123/188/24 partition.
* [ISA Batch 17 — fp32 spfma](../isa/ref/b17-spfma.md) · [ISA Batch 18 — fp16 hp_fma](../isa/ref/b18-hp-fma.md) — the 24 FP FMA mnemonics.
* [Formal Semantics I — Arithmetic / MAC / Load–Store / Gather](../isa/semantics/group-semantics-i.md#2-group-2--multiply--mac-ivp_sem_mul_slice) — the formal MAC semantics.
* [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md#4-matmul-0x02--stream-the-moving-acts-mac-into-psum) — the firmware that drives this engine.

> The live-drive certification on this page (the `mula_24_24_8_8` / `mula_48_48_16_16` `ctypes`
> executions) is the runtime half of the **VAL-03** validation harness; the static-vs-executed
> reconciliation is recorded there.
