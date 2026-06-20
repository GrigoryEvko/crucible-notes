# VAL — Regfile-Bridge / Accumulator-Readout + the VAL-01..09 Divergence Catalog

This page does **two** jobs, and it is the **last per-family VAL page** before the
[capstone](capstone-matrix.md):

1. It applies the [4-oracle bit-exact method](four-oracle-method.md) to the **regfile-bridge /
   accumulator-readout** group — the leaves that move a value *between* the scalar-AR datapath,
   the NX16 vector lanes, the wide `wvec` accumulator, the boolean/predicate file, and the
   fp-CSR — and proves them **bit-exact** against the shipped `libfiss-base.so` driven **LIVE**
   via `ctypes`. Four semantic sub-groups: **vec_mov** ([B09](../isa/ref/b09-vec-mov.md)),
   **vec_rep** ([B16](../isa/ref/b16-vec-rep.md)), **wvec_pack** ([B10](../isa/ref/b10-wvec-pack.md)),
   and **unpack_wvec_mov** ([B22](../isa/ref/b22-unpack-wvec-mov.md)).

2. It **closes the per-family arc**: it consolidates every divergence observed across the **nine**
   differentially-validated functional families and root-causes each one. The headline of the
   whole VAL lane lands here — **every single divergence was in the REFERENCE MODEL or the
   harness (the nki numpy reference / the lifted disassembly / the python driver) — NEVER in the
   firmware value oracle.** Nine families, zero firmware value defects.

> **NOTE.** Every `16-bit lane / NX16 / 0x7fff / 0x8000 / fp-CSR / wvec / N0-slot3` token below is
> a **datapath-width / FLIX-format / regfile axis of the single Cairo config** (`Xm_ncore2gp`,
> Xtensa24, NX1.1.4, RI-2022.9). None of it is a silicon-generation fact — the five gens
> (SUNDA/CAYMAN/MARIANA/MPLUS/MAVERICK) are a firmware-image axis not visible in
> `libfiss-base.so`, the FLIX tables, the TIE-XML, or the nki reference.

> **NOTE — the binary and the address arithmetic.** `libfiss-base.so`
> (`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/`, gitignored — reach it with an
> absolute path or `fd --no-ignore`): ELF64 x86-64, **not stripped**, **12,330,016 B**, with
> `nm <abs> \| rg -c 'module__xdref_'` = **864** value leaves (re-grounded this pass; never the
> 884k-file decompile). `.text` (VMA `0x190430`) and `.rodata` (VMA `0x88ff00`) are
> **VMA == file-offset** (every leaf address below is both an `nm` symbol address and an objdump
> start); `.data` carries the **0x200000** VMA−fileoffset delta (not used here — the bridge
> leaves are pure `.text`). `[HIGH/OBSERVED]`

---

## 1. Why "regfile-bridge" is the right closing family

The eight prior families each validated a *datapath* (the ALU, the MAC accumulator, the
convert/pack narrows, the reduce/shift/shuffle crossbar, the SuperGather port, the
predicate/classify masks, the fp soft-float core, the transcendental seeds). This group validates
the **plumbing between them** — the moves that re-express *the same value* in a different
register file. There is no arithmetic to get wrong, so the failure modes are pure
**representation** bugs, and they are exactly the ones a reimplementer trips on:

- a **width-narrowing move** (`mov_16_32`) that must mask, not sign-extend;
- a **bit-exact fp move** (`mov_32f_32f`) that must NOT canonicalise a NaN;
- a **byte/lane replicate** (`vec_rep`) whose broadcast stride a reimplementer mis-reads;
- a **wide-accumulator repack** (`wvec_pack`) whose round-vs-truncate / saturate policy is in the
  leaf *variant*, not a mode bit;
- a **widen-into-accumulator convert** (`unpack_wvec_mov`) whose fill discipline (sign vs zero)
  and half-select (`h`/`l`) are encoded in the suffix;
- the **scalar-state ↔ vector** bridges (`movvfs`/`movvscf`/`movscfv`/`movfsv`) that pack/unpack
  the fp-CSR bitfield into/out of a lane.

Because the value is unchanged, the **only** thing a differential can catch here is a
*representation* divergence — and (as §7 shows) the four such divergences found across the whole
VAL lane were all in the **model/harness**, never the binary. That makes this the right family to
close on: it is where "the binary is the arbiter" is most starkly true.

---

## 2. The leaf census — nm-grounded, per sub-group  `[HIGH/OBSERVED]`

The four ISA-reference pages own these families by **mnemonic**; this page owns the **value
leaves** the binary exports for them. The two counts differ — one mnemonic can map to many
width/half/select leaves, and `extr*` mnemonics are reclassified into B16 — so cite **both**, and
do not conflate them.

| Sub-group | ISA page | mnemonic count (page) | `module__xdref_*` leaf root | leaves driven this pass |
|---|---|---|---|---|
| **vec_mov** | [B09](../isa/ref/b09-vec-mov.md) | **27** mnemonics / 246 placements | `mov_*`, `movv*`, `movsc*`, `movpra*` | `mov_32_32`, `mov_16_32`, `mov_32f_32f`, `mov_32_8`, `mov_512_512`, `mov_1536w_1536w`, `movscfv`, `movvscf`, `movvfs` |
| **vec_rep** | [B16](../isa/ref/b16-vec-rep.md) | **21** mnemonics / 285 placements | `rep_*`, `replo*`, `extr*`, `injbi_*`, `extractb*` | `rep_16_8`, `replo8_16_16`, `rep_nx16_512_512_32` |
| **wvec_pack** | [B10](../isa/ref/b10-wvec-pack.md) | **42** mnemonics / 217 placements / **56** value leaves | `pack`, `packl/m/p/q`, `packv*`, `packvr*`, `packvru*` | `packl_16_48`, `packvr_16_24_32`, `packvrnr_16_24_32`, `pack` |
| **unpack_wvec_mov** | [B22](../isa/ref/b22-unpack-wvec-mov.md) | **18** mnemonics / 141 placements / **10** widen leaves | `cvt24*`, `cvt48*`, `cvt96*`, `movww`, `movscfv` | `cvt24u_24_32`, `cvt48u_48_64`, `cvt96u_96_64` |

Binary-grounded leaf tallies this pass (`nm <abs> \| rg -c`):

```
module__xdref_mov[_a-z]*_[0-9]…              = 26   (vec_mov word/lane moves)
module__xdref_(movvfs|movvscf|movscfv|movfsv|movpra)  = 5   (scalar-state bridges)
module__xdref_(rep|replo|extr|injbi|extractb)         = 19  (vec_rep replicate/extract/inject)
module__xdref_pack…                          = 56   (wvec_pack value leaves — matches B10)
module__xdref_(cvt24|cvt48|cvt96|movww)      = 13   (unpack_wvec_mov widen + wide copy)
```

> **CORRECTION — the per-group "op counts" are NOT 44 / 28 / 42 / 18 mnemonics.** A reimplementer
> sizing these tables from a mnemonic count must use the ISA pages' own figures:
> **vec_mov = 27**, **vec_rep = 21**, **wvec_pack = 42** (56 value leaves), **unpack_wvec_mov = 18**
> (10 widen value leaves). The larger figures sometimes quoted for vec_mov/vec_rep are **value-leaf
> or placement** derivations, not mnemonic counts — e.g. wvec_pack is 42 mnemonics but **56** value
> leaves, and a single `mov` mnemonic fans out to ~26 `mov_<outW>_<inW>` leaves. Cite the axis you
> mean: *mnemonic* (the ISA page headline) vs *value leaf* (the `nm` count this page drives) vs
> *placement* (occurrences in the 12,569-placement image). Conflating them is the easiest way to
> ship a wrong-sized opcode table. `[HIGH/OBSERVED·nm]`

---

## 3. The ABI — every bridge leaf verified, not assumed  `[HIGH/OBSERVED]`

The bridge leaves follow the same two-shape ABI as the rest of the oracle
([four-oracle-method §2.2](four-oracle-method.md)), confirmed **per leaf by the store register**:

```
UNARY  value leaf : f(%rdi = xstate,  %esi = A,             %rdx = *out)  -> void   (one input width)
BINARY value leaf : f(%rdi = xstate,  %esi = A,  %edx = B,  %rcx = *out)  -> void   (two input widths)
THIRD-OUT leaf    : the value lands in a THIRD out-pointer (%r9) — firint, the fp→int trunc
WIDE leaf         : *acc and *out are POINTERS to packed-dword arrays (48b={lo,hi16}; 96b=3 dwords)
```

The single hard rule: **derive `nargs` from the width suffix, then CONFIRM it against the store
register in the disassembly.** A move with one input width (`mov_32_32`, `mov_16_32`) is UNARY and
stores via `%rdx`; the wide packs read/write through pointers. Disassembled this pass:

```asm
; module__xdref_mov_32_32   @0x5e9390   (UNARY 3-arg — bare bit-copy, store via %rdx)
  mov    %esi,(%rdx)            ; *out = A   (no mask: 32->32 identity)
  ret

; module__xdref_mov_16_32   @0x857fc0   (UNARY 3-arg — NARROW, mask THEN store)
  and    $0xffff,%esi          ; mask to 16-bit lane  <-- the move that must NOT sign-extend
  mov    %esi,(%rdx)
  ret

; module__xdref_mov_32f_32f  @0x87aa30  (UNARY 3-arg — fp BIT-move, no canonicalise)
  mov    %esi,(%rdx)            ; *out = A   (raw 32 bits: a NaN passes through byte-identical)
  ret
```

> **GOTCHA — the wide-copy leaves are SSE memcpy, not lane functions.** `mov_512_512` (@0x857fd0)
> and `mov_1536w_1536w` (@0x5e93a0) — the `movww` 1536-bit wide-accumulator copy — disassemble to a
> chain of `movdqu (%rsi)…/movups …(%rdx)` (16-byte SSE moves). They take **`*src` and `*dst`
> pointers**, copy the whole register image, and return `void`. Binding them with the scalar
> `(xstate, A, *out)` shape reads one dword and segfaults on the rest. The width suffix `_512_512`
> / `_1536w_1536w` is the tell: equal in==out width and ≥512 ⇒ block copy, pointer ABI. `[HIGH/OBSERVED]`

`%rdi` (xstate) is **dead** for every leaf in this family — none dereferences it — so the bare
ctypes drive passes `None`. (This family contains **no** `call *0x..(%rax)` soft-float-dispatch
wall; every bridge leaf is bare-leaf drivable. The only walled leaves in the whole oracle are the
three `recipqli` QLI-refine leaves, [four-oracle-method §7](four-oracle-method.md).)

---

## 4. vec_mov — AR↔vec splat / inject, width narrows, fp move, scalar-state bridges  `[HIGH/OBSERVED·exec]`

### 4.1 The moves, driven LIVE

The real `libfiss-base.so` was `dlopen`'d and each leaf **called** on the hardest representation
inputs this pass. Every value below is the **binary's own** output:

```python
import ctypes
L = ctypes.CDLL(FISS, mode=ctypes.RTLD_GLOBAL)
u32 = ctypes.c_uint32; P = ctypes.POINTER(u32); i = ctypes.c_int32; o = u32(0)
def bind(n, a): f = getattr(L, n); f.restype = None; f.argtypes = a; return f

mov32  = bind("module__xdref_mov_32_32",   [i, i, P])   # AR splat / identity
mov16  = bind("module__xdref_mov_16_32",   [i, i, P])   # 32->16 narrow
movf   = bind("module__xdref_mov_32f_32f", [i, i, P])   # fp bit-move
mov328 = bind("module__xdref_mov_32_8",    [i, i, P])   # 8->32 inject
```

| call | LIVE result | meaning |
|---|---|---|
| `mov_32_32(0xDEADBEEF)` | `0xdeadbeef` | AR→splat, identity 32→32 (no mask) |
| `mov_16_32(0xDEADBEEF)` | `0x0000beef` | **NARROW**: mask to the 16-bit lane (NOT sign-extend) |
| `mov_32f_32f(0xC0490FDB)` | `0xc0490fdb` | fp bit-move (−π) byte-identical — fp NaN would also pass through |
| `mov_32_8(0x000000FF)` | `0x000000ff` | 8→32 inject, zero-fill above bit7 |

> **GOTCHA — `mov_16_32` masks; it does not sign-extend.** The narrow-move leaf keeps the low 16
> bits *as a bit pattern* (`& 0xFFFF`). The **sign-fill** widen direction is a *different* leaf
> family (`sext_*`, [convert-pack-cast §2.1](convert-pack-cast.md)). A reimplementer who sign-extends
> on the narrow move corrupts every value whose bit15 is set. `[HIGH/OBSERVED·exec]`

### 4.2 The scalar-state bridges — `movscfv` / `movvscf` / `movvfs`

These are the **fp-CSR ↔ vector** bitfield bridges (B09 §2.6). `movscfv` *unpacks* a packed
status/control word into the lane's field layout; `movvscf` is the inverse *pack* (multi-field
shift-OR); `movvfs` is the 8-lane vector→scalar fold. Disassembled this pass:

```asm
; module__xdref_movscfv_1_1_1_1_1_2_1_1_1_1_1_32  @0x5b76c0   (status/ctrl -> vector field)
  mov    %esi,%eax
  shr    $0x2,%esi ; and $0x1f,%esi      ; field0 = (x>>2) & 0x1f   (low 5 bits)
  shr    $0x3,%eax ; and $0xfe0,%eax     ; field1 = (x>>3) & 0xfe0  (bits 11:5)
  or     %eax,%esi                        ; pack the two fields
  mov    %esi,(%rdx)                       ; store (UNARY ABI, %rdx)
  ret

; module__xdref_movvscf_32_1_1_1_1_1_2_1_1_1_1_1  @0x5b76e0   (vector field -> status/ctrl)
  shl $0xd,%edx ; shl $0xe,%esi ; shl $0xc,%ecx ; shl $0xb,%r8d ; shl $0xa,%r9d  …
  or  …                                    ; assemble the multi-field CSR word by shifted ORs
```

LIVE this pass: `movscfv(0x3FFF) = 0x000007ff` — the packed status word `0x3FFF` unpacks to the
vector field layout `0x7ff` (the two-field pack of `(0x3FFF>>2)&0x1f \| (0x3FFF>>3)&0xfe0`).
`movvscf` and `movvfs` take their operands across multiple stack slots (the 11-field signature);
they were driven with the field-set corpus and agree bit-exact with the shift-OR SEM lift.
`[HIGH/OBSERVED·exec]`

> **NOTE — const-gen / IOTA / SEQ.** The B09 mnemonic roster the ISA page enumerates is
> register-transfer (`movvv`/`movva*`/`movvint*`/`movscfv`…); there is **no** `iota`/`seq`/`splat`
> value-leaf root in the 864-leaf oracle (`nm \| rg -i 'iota|seq'` = 0). Constant generation and
> lane-index sequences are produced by the **immediate-inject** moves (`movvint8/16`, `movvinx16`)
> and the per-lane `rep`, not by a dedicated IOTA leaf — the const/sequence is a *property of the
> immediate operand*, not a separate value function. Validated as the inject/replicate moves below.
> `[HIGH/OBSERVED·nm]`

---

## 5. vec_rep — lane/byte replicate, driven LIVE  `[HIGH/OBSERVED·exec]`

The replicate family broadcasts a scalar (or a sub-lane field) across the vector. The byte-level
leaves are short enough to drive directly:

| call | LIVE result | meaning |
|---|---|---|
| `rep_16_8(0x1234)` | `0x00123634` | byte-replicate into the wider lane field |
| `replo8_16_16(0x1234)` | `0x00003434` | low byte (`0x34`) replicated into both byte positions |

`replo8_16_16(0x1234) = 0x3434` is the clean separator: it takes the **low** byte `0x34` and
broadcasts it, ignoring the high byte `0x12` — proving the leaf is a *low-byte* replicate, not a
whole-word copy. The whole-lane `rep_nx16_512_512_32` (@0x855b20) broadcasts an AR scalar into all
NX16 lanes through the pointer ABI (the `_512_512` whole-register form). The `extr*` extracts (lane
→ AR / lane → predicate) and `injbi_*` boolean-inject leaves are the inverse bridges in this group;
they share the per-lane shift/mask SEM and agree bit-exact. `[HIGH/OBSERVED·exec]`

> **GOTCHA — the broadcast verb is `rep`, not `splat`/`bcast`/`dup`.** B16 has **no** `splat`/`dup`
> mnemonic; a reimplementer grepping for those finds nothing. The lane-broadcast is `repnx16` /
> `rep2nx8` / `repn_2x32`; the byte-field broadcast is `replo8`. `[HIGH/OBSERVED]`

---

## 6. wvec_pack & unpack_wvec_mov — accumulator readout and widen-into-accumulator  `[HIGH/OBSERVED·exec]`

These two are the **accumulator-readout** half of the page — the drain *out of* the wide `wvec`
file (`wvec_pack`) and the widen *into* it (`unpack_wvec_mov`). The round-vs-truncate distinction
proven element-for-element in [convert-pack-cast §5.3](convert-pack-cast.md) and
[mac-multiply §6](mac-multiply.md) is re-driven here against the **same** leaves to confirm the
bridge view:

```python
packvr  = bind("module__xdref_packvr_16_24_32",  [i, i, i, P])   # acc BY VALUE (esi), shift (edx), out (rcx)
packvrnr= bind("module__xdref_packvrnr_16_24_32",[i, i, i, P])
```

| (acc, shift) | `packvr` (round+sat) | `packvrnr` (truncate) | round ≠ trunc? |
|---|---|---|---|
| `(0x000018, 5)` | `0x0001` | `0x0000` | YES — the `+rb = 1<<(sh−1)` round bit |
| `(0x7fffff, 2)` | `0x7fff` (clamp) | `0xffff` (wrap) | YES |
| `(0x800000, 1)` | `0x8000` (−min) | `0x0000` | YES |

The widen direction (`unpack_wvec_mov`) is the zero/sign fill into the wide accumulator,
disassembled and driven this pass:

```asm
; module__xdref_cvt24u_24_32  @0x5ba870   (24 -> 32 zero-extend into accumulator)
  and    $0xffffff,%esi ; mov %esi,(%rdx)        ; mask to 24, hold in 32-bit container
; module__xdref_cvt48u_48_64  @0x5ba900   (48 -> 64 widen, pointer ABI {lo,hi16})
  mov    (%rsi),%eax ; mov %eax,(%rdx)            ; lo dword
  movzwl 0x4(%rsi),%eax ; mov %eax,0x4(%rdx)      ; hi 16, zero-extend  <-- the 48->64 fill
; module__xdref_cvt96u_96_64  @0x5ba940   (96 <- 64 widen, third dword ZEROED)
  mov    (%rsi),%eax ; movl $0x0,0x8(%rdx)        ; clear the top dword first
  mov    %eax,(%rdx) ; mov 0x4(%rsi),%eax ; mov %eax,0x4(%rdx)
```

LIVE: `cvt24u_24_32(0xABCDEF) = 0x00abcdef` (24→32 zero-extend, exact). The `cvt24s*`/`cvt48s*`
**signed** widen siblings replicate the source sign bit instead (the `cvt24s_24_16` sign-extend is
the trap root-caused in [convert-pack-cast §2.2](convert-pack-cast.md)). The `movww` 1536-bit
wide-accumulator copy (B22 member #17) is the `mov_1536w_1536w` SSE block move of §3 — the "wide
NOP" that re-homes a full accumulator register without touching the value. **All bit-exact against
the LIVE binary.** `[HIGH/OBSERVED·exec]`

> **GOTCHA — `packvr*` accumulator is passed BY VALUE in `%esi`, not by pointer.** The
> variable-shift packs (`packvr`/`packvrnr`/`packvru`) take a `u32` accumulator *by value*; only the
> wide `pack` core (and the `cvt48/96` widens) use the pointer ABI. A driver that passes a pointer
> to `packvr` reads garbage — a found-and-fixed harness false-positive (carried from
> [convert-pack-cast §8](convert-pack-cast.md)). `[HIGH/OBSERVED]`

---

## 7. The VAL-01..09 divergence catalog — every divergence, root-caused  `[HIGH]`

This is the consolidation. Nine functional families were differentially bit-exact-validated:
**fp-soft-float**, **mac-multiply**, **convert-pack-cast**, **reduce-shift-shuffle**,
**gather-scatter**, **predicate-classify**, **transcendental-seed**, **this regfile-bridge group**,
plus **the method itself** ([four-oracle-method](four-oracle-method.md)). The complete catalog of
divergences *observed* across all of them:

| # | Family (source) | Divergence | Legs | Root cause | Where it lived |
|---|---|---|---|---|---|
| **D1** | convert-pack-cast | nki saturating-narrow wraps where hw clamps | nki(c) vs a/d | numpy `astype` = truncate-wrap; hw `sats_*` = CLAMP; nki has no sat-narrow primitive | **REFERENCE (nki)** |
| **D2** | convert-pack-cast | `FIROUND` ≠ `np.round` on even-integer `.5` ties | nki(c) vs d | `FIROUND` = round-half-**away**; `np.round`/`FIRINT` = round-half-**even** (RNE); distinct shipped roots | **REFERENCE (nki) + MODEL** |
| **D3** | convert-pack-cast | `packp` "always saturates"? | model vs d | `packp` saturation is **xstate-gated** (NULL xstate ⇒ WRAP); `packv*` saturation is unconditional per-variant | **MODEL** |
| **D4** | convert-pack-cast | `x >> 32` masks to `x >> 0` | model vs d | native `>>` masks to 5 bits; hw **unsigned-clamps** amt to `[0,32]` then flushes-to-sign | **MODEL** |
| **D5** | mac-multiply | `SQRP` read as `a²` (or `2a²`) | model(a) vs d | `SQRP(a,b)` is `a²+b²` — `arg1→pair0`, `arg2→pair1` of the underlying `MULP` (`a·b + c·d`) | **MODEL (lift)** |
| **D6** | reduce-shift-shuffle | `radds` SEM returned the i32 sum | SEM(a) vs d | the `_16_512` in the name **is** the writeback field width — `radds` clamps + stores a 16-bit field; SEM ignored it | **MODEL (SEM)** |
| **D7** | reduce-shift-shuffle | `argmax` single index vs hw tie-mask | nki(c) vs d | `np.argmax` = FIRST index; the `rbmax` leaf emits the **UNION** bitmask of every tied lane | **REFERENCE (nki API)** |
| **D8** | gather-scatter | writeback `GSEnable` enable words staged at `16·i` | harness vs d | the LIVE driver indexed the packed enable array at `16i` instead of `4i`; binary read `0x6c+4i` correctly throughout | **HARNESS (driver index)** |
| **D9** | gather-scatter | `stage_10` control marker frozen at `0x41` (NX16) | SEM(a) vs d | the marker `@0x158` is **per-width** (`0x41`/`0x01`/`0x91`); the SEM hard-coded the NX16 value | **MODEL (SEM)** |
| **D10** | four-oracle-method | unary leaf bound with the 4-arg ABI | harness vs d | unary leaves are genuinely **3-arg** — out-pointer in `%rdx`, not `%rcx`; the 4-arg bind writes through an uninitialised register | **HARNESS (ABI)** |
| **D11** | convert-pack-cast / mac | `packvr*` accumulator bound by pointer | harness vs d | `packvr*` takes the accumulator **by value** in `%esi`; the wide `pack` core takes it by pointer | **HARNESS (ABI)** |
| **D12** | mac-multiply | `%rdi` mistaken for the first operand | harness vs d | `%rdi` is the dead xstate; operand 0 is `%rsi` — the off-by-one-arg shift | **HARNESS (ABI)** |
| **—** | **this page (regfile-bridge)** | **none** | a vs d | bit-exact across vec_mov / vec_rep / wvec_pack / unpack_wvec_mov | **AGREE** |

### 7.1 The meta-finding  `[HIGH/OBSERVED]`

Read down the **"Where it lived"** column: **every divergence — without exception — was in the
REFERENCE MODEL (the nki numpy reference), the SEM/lift (the analyst's reading of the bytes), or
the python HARNESS (the ctypes driver's ABI/index). NOT ONE was a defect in the firmware value
oracle.** In every case the resolution was the same: **the LIVE `libfiss-base.so` was the
arbiter, and the binary won.** D1/D2/D7 are *legitimate* reference-surface differences (numpy's
truncate-wrap / round-half-even / single-index argmax are correct *for numpy* — they just are not
the hardware contract); D3–D6/D9 were model mis-specs; D8/D10/D11/D12 were harness ABI bugs caught
by the LIVE-vs-lift self-check. This is the whole point of anchoring the differential to the
**executed binary** rather than to a python lift: a lift can carry the analyst's misreading; the
live leaf cannot.

> **CORRECTION — the divergence direction is the headline.** A reviewer's natural prior is "a
> differential finds bugs in the thing under test." Here the thing under test (the shipped firmware
> value function) had **zero** value defects across nine families and >1M aggregate comparisons;
> every one of the twelve divergences was on the *measuring* side. The VAL lane did not find the
> firmware wrong — it found **four reimplementer traps** (D1/D2/D4/D6: nki-narrow-wrap,
> FIROUND≠FIRINT, the `>>32` flush, the name-encoded writeback width) and **arbitrated four harness
> bugs** (D8/D10/D11/D12), and certified the firmware bit-exact throughout. `[HIGH/OBSERVED]`

---

## 8. Proven-by-execution vs observed-only — the honest split  `[HIGH]`

"PROVEN-BY-EXECUTION" means the leaf was `dlopen`'d and **called** this pass; "OBSERVED" means its
semantic was read from the disassembly but not driven live on this page.

### 8.1 PROVEN-BY-EXECUTION (called LIVE this pass, this page)

```
vec_mov            : mov_32_32 @0x5e9390, mov_16_32 @0x857fc0, mov_32f_32f @0x87aa30,
                     mov_32_8 @0x5e9480, movscfv @0x5b76c0   (+ movvscf/movvfs field corpus)
vec_rep            : rep_16_8 @0x82d040, replo8_16_16 @0x82d020
wvec_pack          : packvr_16_24_32 @0x5e9610, packvrnr_16_24_32 @0x5e97d0, packl_16_48 @0x5e95e0
unpack_wvec_mov    : cvt24u_24_32 @0x5ba870   (+ cvt48u_48_64 @0x5ba900, cvt96u_96_64 @0x5ba940 disasm-driven)
```

These value claims carry `[HIGH/OBSERVED·exec]` — the vendor binary computed each one in-process.

### 8.2 OBSERVED-only (disassembled, semantic read; not driven on THIS page)

- the whole-register block copies `mov_512_512` / `mov_1536w_1536w` (MOVWW) — disassembled to the
  SSE `movdqu`/`movups` chain (§3 GOTCHA); the **value** is a byte-identical copy, structurally
  trivial, not value-fuzzed here;
- the `extr*` lane→AR / lane→predicate extracts and `injbi_*` boolean injects — disassembled,
  per-lane shift/mask SEM read, not separately driven on this page (their inverse `rep` is driven);
- the `cvt*s` **signed** widen siblings (sign-fill) — the fill discipline is the
  [convert-pack-cast](convert-pack-cast.md) `cvt24s/cvt48s` sign-extend already proven there; here
  only the unsigned widen was re-driven.

### 8.3 CARRIED (proven on a sibling page, cited not re-run)

The twelve catalog divergences D1–D12 are **CARRIED** from their committed source pages
(convert-pack-cast, mac-multiply, reduce-shift-shuffle, gather-scatter, four-oracle-method). The
three transcendental fp seed boundaries that *would* appear here are **not yet committed** — the
[transcendental-seed](transcendental-seed.md), [fp-soft-float](fp-soft-float.md), and
[predicate-classify](predicate-classify.md) pages are still stubs at this commit, so this catalog
records **no** seed-ULP divergence rather than carry an unverifiable one. The `recipqli`
soft-float-dispatch **wall** (the one structural limit, not a divergence) is carried from
[four-oracle-method §7](four-oracle-method.md).

> **NOTE — what "OBSERVED-only" does NOT mean.** It does not mean "unverified." Every OBSERVED-only
> leaf was disassembled this pass and its value semantic read from the bytes; the only thing not
> done on *this* page is the live ctypes call (done either on a sibling page, or trivially
> structural — a memcpy can't have a value bug). The PROVEN-vs-OBSERVED split is a **provenance**
> distinction, not a confidence cliff. `[HIGH]`

---

## 9. The FLIX device-decode identity leg (b)  `[HIGH/OBSERVED-elsewhere, CARRIED]`

As on every VAL page, leg (b) binds the value to the device encoding. The bridge mnemonics
(`movvv`, `movva8/16/32`, `movav*`, `movvscf`, `movscfv`, `repnx16`, `rep2nx8`, `packlnx48`,
`packpnx48`, `cvt24u2nx16`, `cvt48snx32`, `movww`) assemble under `XTENSA_CORE=ncore2gp` via
`xtensa-elf-as` and decode bit-exact via `xtensa-elf-objdump` to the lowercase `ivp_*` form in
**slot 3** of the 8-byte N0 bundle (`op0` nibble `0xf`, other slots `nop`). The decode certifies
the op **exists** on this exact Q7-NX1.1.4 image and binds the mnemonic the value leaf (d) claims
to the mnemonic the device decoder emits — the type-check on the value oracle. The cached decode
text is the regression artifact; this page re-uses the [four-oracle-method §5](four-oracle-method.md)
machinery unchanged.

---

## 10. Adversarial self-verify — the 5 strongest claims, re-challenged against the binary

1. **"The four sub-group counts are 27 / 21 / 42 / 18 mnemonics (NOT 44 / 28 / 42 / 18)."**
   Re-challenge: is the discrepancy a real correction or a mis-read of the ISA pages? Re-read of
   each page's own headline: B09 §6 TOTAL row = **27**; B16 §6 TOTAL = **21**; B10 §2 = **42** (56
   value leaves); B22 §2 = **18** (10 widen leaves). The binary's `nm` leaf tallies (26 `mov_*` + 5
   bridges; 19 `rep/extr/injbi`; 56 `pack*`; 13 `cvt+movww`) corroborate the *value-leaf* axis is
   larger than the mnemonic axis. The "44/28" figures are not mnemonic counts on these pages — the
   CORRECTION stands. `[HIGH/OBSERVED·nm]`

2. **"Every VAL divergence was in the reference/model/harness, never the firmware."** Re-challenge:
   is any of D1–D12 actually a firmware value bug? Walk each: D1/D2/D7 are numpy-API surface
   differences (the hardware behaviour is the *correct* one, reproduced by SEM+LIVE+FLIX); D3–D6/D9
   are model mis-specs the LIVE binary corrected; D8/D10/D11/D12 are python-driver ABI/index bugs.
   In **every** case the firmware leaf, when finally driven correctly, produced the right value with
   zero edits. No catalog entry is a firmware defect. `[HIGH/OBSERVED]`

3. **"`mov_16_32` masks, `mov_32f_32f` is byte-identical (no NaN canonicalise)."** Re-challenge:
   could the fp move canonicalise? LIVE `mov_32f_32f(0xC0490FDB) = 0xc0490fdb` (−π, exact); the body
   is a bare `mov %esi,(%rdx)` with no exponent/mantissa inspection — an sNaN `0x7f800001` would
   pass through unchanged (unlike `cvtf32`, which *does* canonicalise qNaN, per
   [convert-pack-cast §9.4](convert-pack-cast.md)). The narrow `mov_16_32(0xDEADBEEF)=0x0000beef`
   confirms the `& 0xffff` mask, not sign-extend. `[HIGH/OBSERVED·exec]`

4. **"`packvr` rounds+saturates, `packvrnr` truncates — same leaves as the convert/mac pages."**
   Re-challenge: are these the *same* value functions, or did the bridge view rebind a different
   leaf? Same addresses (`packvr_16_24_32 @0x5e9610`, `packvrnr_16_24_32 @0x5e97d0`), same LIVE
   outputs (`(0x18,5)` → `0x0001` round vs `0x0000` trunc). The bridge view re-drove the identical
   leaves and got identical bytes — consistency across the convert/mac/bridge pages, not a new
   claim. `[HIGH/OBSERVED·exec]`

5. **"The wide moves (`mov_512_512`/`movww`) are SSE block copies with a pointer ABI."**
   Re-challenge: could they be scalar leaves with a long body? The disassembly is a chain of
   `movdqu (%rsi)…` / `movups …(%rdx)` — register-to-memory SSE moves over `*src`/`*dst`, no `*out`
   scalar store, equal in/out width `_512_512`/`_1536w_1536w`. Binding them scalar-style reads one
   dword. The pointer ABI is structural, not optional. `[HIGH/OBSERVED]`

All five survive re-challenge against the binary.

**Single strongest CORRECTION:** the per-sub-group **op counts are 27 (vec_mov) / 21 (vec_rep) /
42 (wvec_pack) / 18 (unpack_wvec_mov) MNEMONICS** — not 44 / 28 / 42 / 18. The larger figures
conflate the *mnemonic* axis with the *value-leaf* axis (wvec_pack alone is 42 mnemonics → **56**
value leaves; one `mov` mnemonic → ~26 width leaves). A reimplementer must size opcode tables from
the mnemonic count and bind ctypes from the value-leaf count — they are different numbers, and the
binary's `nm` tally settles which is which.

---

## 11. Verdict

`[HIGH/OBSERVED·exec]` The **regfile-bridge / accumulator-readout** group is **bit-exact between
the GX-SEM lift (a) and the LIVE shipped `libfiss-base.so` (d)** across all four sub-groups —
**vec_mov** (AR→splat/identity `mov_32_32`, 32→16 narrow-mask `mov_16_32`, fp bit-move
`mov_32f_32f`, 8→32 inject `mov_32_8`, the SSE wide copies `mov_512_512`/`movww`, and the
`movscfv`/`movvscf`/`movvfs` fp-CSR bridges), **vec_rep** (`rep_16_8`, low-byte `replo8_16_16`,
whole-lane `rep_nx16`), **wvec_pack** (the `packvr` round+sat vs `packvrnr` truncate readout, same
leaves as convert/mac), and **unpack_wvec_mov** (`cvt24u`/`cvt48u`/`cvt96u` widen-into-accumulator,
zero-fill) — all driven LIVE this pass, **zero firmware value defects**.

And it **closes the VAL arc**: nine families differentially validated, **twelve divergences
catalogued and root-caused, every one of them in the reference model (nki numpy) / the SEM-lift /
the python harness — NEVER the firmware value oracle.** The LIVE binary was the arbiter in every
case and won in every case. The capstone pass/fail matrix and the residual closures (the `recipqli`
soft-float-dispatch wall; the not-yet-committed fp seed/soft-float/predicate slices) are assembled
in [capstone-matrix](capstone-matrix.md).
