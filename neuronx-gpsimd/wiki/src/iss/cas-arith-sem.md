# cas Vector-Arithmetic Semantics

This page documents how the **cycle-accurate ISS** in `libcas-core.so` models the IVP vector
**arithmetic** ops — `add / sub / abs / neg / min / max` and their abs-difference and exponent-add
cousins, the ones that issue on the **ALU slot class** (`S3`/`S4`). It is the second page of
[Part 14 — the ISS as Executable Oracle](./cas-core-surface.md) and it threads the keystone that
governs *every* page in this Part:

> **Keystone — cas decodes, fiss values.** The cycle-accurate ISS `libcas-core.so`
> (`BASE = libcas-Xtensa.so`, `VERS_1.1`, GCC 4.9.4, modeled core = Cayman / Vision-Q7 IVP VLIW)
> computes **DECODE + TIMING + REGISTER-HAZARD**, but it does **NOT** compute element **values**.
> The per-lane `a OP b` bit-math is **delegated to the host** through the 119 `nx_*_interface`
> TIE-port callbacks (and a per-instance host-callback dispatch slot); the actual value functions
> live in `libfiss-base.so` (the 864 `module__xdref_*` leaves, drivable live via `ctypes` —
> [fiss Datapath, the value oracle](./fiss-datapath-oracle.md)). So this page covers the
> **cas half**: the decode that binds each ISA-lane encoding to a unique IVP mnemonic, the
> per-op ALU datapath *shape*, and the cycle schedule — and shows exactly where the value
> callback fires. The *value* half (saturation/signedness/rounding/NaN) is fiss's.

Confidence tags follow [the Confidence & Walls model](../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / proven by disassembly, `[MED/INFERRED]` = reasoned over
OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence. All offsets are into the shipped
`extracted/.../ncore2gp/config/libcas-core.so` (45 878 080 B; ELF64 x86-64, **not stripped**).

> **GOTCHA — VMA vs file offset is not uniform.** `.text` (`0x572fa0`) and `.rodata` (`0x17ba840`)
> are **VMA == file offset**; the writable sections are **not**: `.data.rel.ro` (VMA `0x2070900` →
> file `0x1e70900`) and `.data` (VMA `0x2280ed8` → file `0x2080ed8`) both carry a **`0x200000`
> delta** (`readelf -SW`). Every `xxd` on a decode record below subtracts `0x200000`; the `.text`
> disassembly addresses are raw VMA.

---

## 1. The executive finding — zero SIMD arithmetic, value handed to the host

The single decisive fact: a 45.9 MB binary that *actually evaluated* 32×16-bit lane adds would
contain packed SSE/AVX (`paddw`/`pminsw`/…) or at minimum a scalar lane loop with `add`/`cmov`.
A full-image disassembly sweep finds **none of the former**:

| Sweep (`objdump -d \| grep -Ec …`) | Count | Meaning |
|---|---:|---|
| Packed-int arith `padd*/psub*/pmaxs*/pmins*/pmull*/pabs*` + AVX `vp*` | **0** | no integer lane compute `[HIGH/OBSERVED]` |
| Packed-float arith `v?addp[sd]/subp[sd]/mulp[sd]/maxp[sd]/minp[sd]` | **0** | no float lane compute `[HIGH/OBSERVED]` |
| Scalar `add`/`sub`/`imul` total | **35 473** | all address / index / counter / timing math, never element datapath `[HIGH/OBSERVED]` |

The arithmetic *reaches* the datapath through an **indirect call on a host-callback dispatch slot**
held in the per-instance state struct. The ALU execute site (verbatim, the common compute path at
VMA `0x151b7e0`, inside `F0_F0_S3_ALU_36_ivp_sem_vec_alu_semantic_stage10.isra.200`):

```asm
; --- the host hand-off: where the element VALUE is delegated ---
151b7e0:  mov    0x15200(%rbx),%rdi      ; rdi = host-callback context (state+0x15200)
151b7e7:  mov    $0xa,%esi               ; esi = 10  -> the EXECUTE pipeline stage
151b7ec:  call   *0xe7098(%rbx)          ; INDIRECT host datapath callback  <-- VALUE delegated here
151b7f2:  movabs $0x1000002000000,%rax   ; (post-call: scoreboard/def-mask test)
151b7fc:  test   %rax,0x870(%rbx)
```

The `call *0xe7098(%rbx)` slot is real and used **9 times** across the image (one per format/slot
ALU executor); data movement to/from that callback uses **named PLT ports** — e.g.
`nx_VectorMemDataOut512_0_interface` (105 call sites) and `nx_ScatterData_0_interface` (85). The
import table is exactly **120 undefined symbols = 1× `memset` + 119× `nx_*_interface`** — the
simulator's only runtime coupling.

> **The consequence for reimplementation.** The ground-truth element semantics — saturating vs
> wrapping, signed vs unsigned, rounding, NaN handling — are **not executed inside this DLL**. They
> are *fully encoded in the opcode identity* that this DLL decodes and forwards. So this page
> recovers two things, and only two: **(a)** the decode/dispatch machinery that binds each ISA-lane
> encoding to a unique IVP mnemonic, and **(b)** the cycle-accurate operand-read / result-write
> *schedule*. For the bit-exact `a OP b` you drive the matching `module__xdref_*` leaf in
> `libfiss-base.so` — see [fiss Datapath](./fiss-datapath-oracle.md), which on `IVP_ADDNX16`
> realises the op as `module__xdref_add_16_16_16` called **32 times** (one per NX16 lane).
> `[HIGH/CARRIED]`

> **NOTE — the `my_vec_*` exports are not the datapath either.** The 275 `my_vec_*` exports
> ([core surface §2](./cas-core-surface.md)) are pure **scoreboard metadata** keyed by operand
> *role*, not by mnemonic. `my_vec_0_opnd_ivp_sem_vec_alu_vr_set_use` masks a register index
> (`& 0x1f`), records the operand, ORs a hazard bit, and returns — **no arithmetic**. `vec_alu` is
> the operand-*class* ("an ALU-shaped op with `vr`/`vs`/`vt` operands"); the `add`-vs-`sub`-vs-`min`
> distinction is **invisible** at that layer. The mnemonic-level identity lives in the per-mnemonic
> local stage functions and the decode bitmap, decoded in §3.

---

## 2. The mnemonic string pool & the lane-encoding grammar

`.rodata` opens at VMA `0x17ba840` (immediately after `.text`) with a packed NUL-terminated
mnemonic pool: first the scalar Xtensa core opcodes (`ADDI`/`ADDX2`/`ADDX4`/`SUBX2…`/`MOVI`/
`CLAMPS`/`MAXU`/…), then the **1 073 `IVP_*` vector intrinsic strings**. Each arithmetic mnemonic
appears **exactly once** as a string (`strings -td`), because the strings are *keyed into* the
decode tables, not duplicated per use.

The IVP name *is* the ISA-lane key — it encodes element type, lane count, and saturation. The
native lane parameter `N = 32` (for 16-bit) follows from the **512-bit** vector datapath: the host
data ports are `nx_VectorMemDataIn512/Out512` and the width enum spans
`nx_MemDataIs{8,16,32,64,128,256,512}Bits` (all seven present in the import table).

| Token | Meaning (one 512-bit `vec` reg) | Conf |
|---|---|---|
| `2NX8` | `2N` lanes × 8-bit int → **64 lanes** | `[HIGH/OBSERVED]` |
| `NX16` | `N` lanes × 16-bit int → **32 lanes** | `[HIGH/OBSERVED]` |
| `N_2X32` | `N/2` lanes × 32-bit int → **16 lanes** | `[HIGH/OBSERVED]` |
| `NXF16` | `N` lanes × fp16 → **32 lanes** | `[HIGH/OBSERVED]` |
| `N_2XF32` | `N/2` lanes × fp32 → **16 lanes** | `[HIGH/OBSERVED]` |
| leading `S` (`ADD-S-NX16`) | **saturating** variant (distinct opcode) | `[HIGH/OBSERVED]` |
| `U` (`MAXU`/`MINU`/`ABSSUBU`) | **unsigned** element interpretation | `[HIGH/OBSERVED]` |
| trailing `T` (`ADD…T`) | **predicated** (per-lane `vbool` guard) | `[HIGH/OBSERVED]` |
| `MOD…U` (`ADDMOD16U`) | unsigned **modular** (wrap) — address-gen helper | `[MED/INFERRED]` |
| `NUM` (`MINNUMN`/`MAXNUMN`) | IEEE-754 **NaN-suppressing** fp min/max (`minNum`/`maxNum`) | `[MED/INFERRED]` |

> **QUIRK — signedness and saturation are *opcodes*, not flags.** `MAXNX16` and `MAXUNX16` are not
> one op with a "signed?" bit — they are two distinct mnemonics with two distinct decode bits (§3),
> exactly as the ISA opcode tables (B01–B03) model them: signed vs unsigned differ *only* in the
> opcode-selector field, each an independent `opcodedefs` row. A reimplementation must surface every
> `(op, width, signedness, saturation, predication)` tuple as its **own** encoding.

The pool is consumed by a **24-byte-stride decode-record array** in `.data.rel.ro` (VMA `0x2070900`
→ file `0x1e70900`). Each record is `{ mnemonic_ptr, 0x10 tag, descriptor_ptr }`. Record 60 =
`IVP_ADDNX16` reads, byte-for-byte (`xxd -s 0x1e70ea0 -l 24`):

```text
01e70ea0:  eda9 7b01 0000 0000   1000 0000 0000 0000   . . 7 . . . . .  ; ptr 0x017ba9ed ; tag 0x10
01e70eb0:  0020 0802 0000 0000                          . . . . . . . .  ; descriptor 0x02082000
```

`0x17ba9ed` is the string `"IVP_ADDNX16"` (`.rodata`, VMA==file). The descriptor `0x02082000`
(→ file `0x1e82000`) is an array of `.text` function pointers — the semantic stage-callbacks
`{0x5b0970, 0x5ad4f0, 0x5d91d0, 0x60ad60, …}`.

> **The template-sibling tell.** The SUB sibling descriptor sits one record below at file
> `0x1e81f80` and is **element-wise parallel**: `{0x5b0930, 0x5ad4b0, 0x5d91b0, 0x60bc60}` — each
> entry is ADD's minus a small fixed delta (`Δ=0x40, 0x40, 0x20`). That regular per-entry offset is
> the signature of **template-generated sibling functions**: `add` and `sub` are the *same* code
> shape parameterized by the op, which is precisely why neither computes a value — they both end at
> the *same* host hand-off.

---

## 3. Decode → dispatch — how an ISA-lane encoding becomes an op

Each `(instruction-format, issue-slot, mnemonic)` triple gets its own family of **16 pipeline-stage
functions**, named (non-stripped):

```text
F<fmt>_F<fmt>_S<slot>_<class>_<id>_IVP_<MNEMONIC>_inst_stage<0..15>
```

157 775 such `_inst_stage` functions exist. Slots observed: `S0` LdSt/LdStALU, `S1` Ld, `S2` Mul,
`S3`/`S4` ALU. Arithmetic `add/sub/min/max/abs/neg` live in the **ALU slots `S3`/`S4`**; multiply
lives in the Mul slot `S2` ([cas MAC / FMAC](./cas-mac-fmac.md)).

### 3.1 The per-mnemonic stage-0 wrapper — the decode marker

Every mnemonic's `stage0` lights **exactly one bit** in a per-bundle **decoded-opcode bitmap** at
`state+0x70x..0xb7x`, writes its slot/stage into the trace cursor, calls the shared per-slot
stage-0 callback, then clears its bit on return. `F0_F0_S3_ALU_36_IVP_ADDNX16_inst_stage0`
@ VMA `0x14df3f0`, verbatim:

```asm
14df3f0:  orb    $0x8,0x712(%rdi)         ; LIGHT this op's decode bit (ADDNX16 = bit 0x08 @ 0x712)
14df3f7:  push   %rbx
14df3f8:  lea    0xde680(%rdi),%rsi       ; operand-latch base for this slot
14df3ff:  mov    %rdi,%rbx
14df402:  movl   $0x3,0x4a09dc(%rdi)      ; trace cursor: slot = 3 (ALU)
14df40c:  movl   $0x0,0x4a09e0(%rdi)      ; trace cursor: stage = 0
14df416:  call   12a2c20 <ivp_sem_vec_alu_opcode_stage0>   ; shared per-slot stage callback
14df41b:  andb   $0xf7,0x712(%rbx)        ; CLEAR the decode bit on return (~0x08)
14df423:  ret
```

The trace pair `state+0x4a09dc` (slot) and `state+0x4a09e0` (stage) is the ISS's **current-op
cursor**, rewritten by every stage wrapper (`stage5` writes `(3,5)`, etc.).

> **The same mnemonic lights a *different* bit per format.** The decode bitmap is keyed by
> `(format, slot, mnemonic)`, not by mnemonic alone — the VLIW format selects the byte:
> `IVP_ADDNX16` is `0x08 @ 0x712` in `F0/S3`, `0x20 @ 0x713` in `F4/S3`, `0x01 @ 0x713` in `F1/S3`,
> `0x20 @ 0x712` in `F11/S4`. A flat "opcode → bit" table is therefore *wrong*; the bit is a
> function of the bundle format.

### 3.2 The shared executor — testing the bitmap, funnelling to one hand-off

The big executor is `F0_F0_S3_ALU_36_ivp_sem_vec_alu_semantic_stage10.isra.200` @ VMA `0x151aa80`
(0x1b18 B; **25 distinct** such executors exist, one per format/slot). It is a long
`testb $bit, 0xNNN(%rbx) ; jne <common compute path>` chain — **364** `testb` tests — covering every
arithmetic mnemonic's bit, all funnelling to the **one** host hand-off at `0x151b7e0` (§1). The
recovered, **byte-verified** mnemonic → decode-bit map for the arith group (`orb`/`testb` operands
read directly from each `stage0` and from the executor chain):

| Mnemonic | decode bit | Mnemonic | decode bit |
|---|---|---|---|
| `ADDNX16` | `$0x08, 0x712` | `SUBNX16` | `$0x04, 0x714` |
| `ADDSNX16` | `$0x20, 0x742` | `SUBSNX16` | `$0x10, 0x744` |
| `ADDN_2X32` | `$0x20, 0x9f9` | `ADD2NX8` | `$0x10, 0x9b6` |
| `MAXNX16` | `$0x04, 0x71c` | `MAXUNX16` | `$0x02, 0x71e` |
| `MINNX16` | `$0x10, 0x718` | `MINUNX16` | `$0x08, 0x71a` |
| `NEGNX16` | `$0x04, 0x716` | `ABSNX16` | `$0x01, 0xb35` |
| `ABSSUBNX16` | `$0x20, 0xb39` | `ABSSUBUNX16` | `$0x10, 0xb3b` |
| `ABSSSUBNX16` | `$0x08, 0xb3d` | | |

`MAXNX16` (`0x71c.4`) vs `MAXUNX16` (`0x71e.2`), and `MINNX16` (`0x718.10`) vs `MINUNX16`
(`0x71a.8`), are **distinct bits** — confirming §2's QUIRK that signed/unsigned are distinct
opcodes, never a shared mode toggle.

### 3.3 The datapath, as annotated C (naming the real cas symbols)

The cas-side model of *every* ALU arithmetic op reduces to this single shape. The `a OP b` is **not**
here; it is the `call *0xe7098` — i.e. the host (fiss) value leaf. The op identity is carried purely
by which decode bit is live.

```c
/* cas-side ALU arithmetic model — one shape for add/sub/abs/neg/min/max,
 * realised per (format,slot,mnemonic). Symbols are the real, non-stripped names.
 * The VALUE (a OP b) is computed HOST-SIDE by the callback at state+0xe7098;
 * fiss realises it as a per-lane loop over module__xdref_<op>_<w>_<w>_<w>. */

/* stage0: decode marker — F<f>_F<f>_S3_ALU_<id>_IVP_<M>_inst_stage0 */
static void inst_stage0(iss_state *st) {
    st->decode_bitmap[BIT_BYTE(M)] |=  BIT_MASK(M);   /* orb  $mask, 0xNNN(%rdi)  */
    st->trace_slot  = 3;                              /* movl $0x3, 0x4a09dc(%rdi) (ALU) */
    st->trace_stage = 0;                              /* movl $0x0, 0x4a09e0(%rdi)       */
    ivp_sem_vec_alu_opcode_stage0(st, &st->latch[SLOT]); /* shared no-op latch (repz ret) */
    st->decode_bitmap[BIT_BYTE(M)] &= ~BIT_MASK(M);   /* andb $~mask, 0xNNN(%rdi)         */
}

/* stages 1..9: operand-field extraction (nibble reg-index decode `and $0xf`)
 * happens in an early stage (opcode_stage9 @0x12a5d60); stage0/1 are repz-ret no-ops. */

/* stage10: the shared executor — F<f>_F<f>_S3_ALU_<id>_ivp_sem_vec_alu_semantic_stage10 */
static void semantic_stage10(iss_state *st) {           /* @0x151aa80 (F0/S3) */
    /* a 364-deep testb chain selects the live op by its decode bit ... */
    if (st->decode_bitmap[0x712] & 0x08) goto compute;  /* testb $0x08,0x712(%rbx) ADDNX16 */
    if (st->decode_bitmap[0x714] & 0x04) goto compute;  /* testb $0x04,0x714(%rbx) SUBNX16 */
    /* ... MAXNX16 0x71c.4, MINNX16 0x718.10, NEGNX16 0x716.4, ABSNX16 0xb35.1, ... */
compute:                                                /* the one common path @0x151b7e0 */
    void   *host_ctx = st->host_callback_ctx;           /* mov 0x15200(%rbx),%rdi */
    int     exec_stage = 10;                            /* mov $0xa,%esi          */
    st->host_datapath(host_ctx, exec_stage);            /* call *0xe7098(%rbx)  <-- VALUE here */
    /*  ^ fiss: per-lane loop, e.g. for NX16 -> module__xdref_add_16_16_16 x32  */

    /* post-call: def-mask / scoreboard test, then writeback is driven by
     * dll_cycle_advance committing the reservation records (st->slot+0x9554/0x94d4/0x93d4). */
}
```

`ivp_sem_vec_alu_opcode_stage0`/`stage1` are literally `repz ret` (no-op latch stages); operand-field
extraction (nibble register-index decode) is seen in `opcode_stage9` @ `0x12a5d60`.

### 3.4 The cycle model (the genuine "cycle-accurate" content)

* The ALU slot is modeled with **16 pipeline stages** (`stage0..stage15` present for every ALU
  mnemonic).
* The **result-compute host callback fires at stage 10** (`esi=$0xa`). This dovetails with the ISA
  reference's ALU latency (source `use_stage = 10` → dest `def_stage = 11`, a 1-cycle ALU; see
  [B01](../isa/ref/b01-vec-alu-int.md)).
* Writeback / scoreboard commit is handled by `dll_cycle_advance` against the def-reservation
  records at `slot+0x9554` (reg#), `slot+0x94d4` (valid), `slot+0x93d4` (value)
  ([core surface §1](./cas-core-surface.md)).
* **Modeled retire latency** ≈ execute at stage 10 of a 16-deep slot pipeline + writeback drain.
  `[MED/INFERRED]`

> **GOTCHA — `IVP_ADDMOD16U` is *not* an ALU op.** Despite the `ADD`, the unsigned-modular add is
> dispatched in the **Load slot `S1`** (`F0_F0_S1_Ld_16_IVP_ADDMOD16U_inst_stage0` @ `0x73b1e0`,
> and across `F1/F2/F3/F4/F6/F7`) — it is an **address-generation / circular-buffer** helper, not a
> general-purpose lane add. Do not fold it into the ALU datapath.

---

## 4. The arith-op semantics table (read from the grammar, *valued* by fiss)

Lane counts assume `N = 32` (512-bit reg / 16-bit element). The `operation` / `sat` / `round` /
`overflow` columns are **read from the IVP naming grammar** (§2) and corroborated by the ISA opcode
tables ([B01](../isa/ref/b01-vec-alu-int.md) / [B02](../isa/ref/b02-vec-alu-fp.md) /
[B03](../isa/ref/b03-vec-alu-rest.md)) and the formal arithmetic semantics in
[group-semantics-i](../isa/semantics/group-semantics-i.md) — they are **not executed inside this
DLL** (§1). Every mnemonic below is present as a string **and** as an ALU-slot `_inst_stage` family
in the binary. **89 distinct ALU-slot arithmetic mnemonics** total. `[HIGH/OBSERVED for presence;
MED for the valued column → confirmed by fiss leaves]`

| Mnemonic | op | lanes × w | sat | overflow / round | conf (semantics) |
|---|---|---|---|---|---|
| `IVP_ADD2NX8` | `a+b` i8 | 64 × 8 | no | wrap mod 2⁸ | `[HIGH/OBSERVED]` |
| `IVP_ADDNX16` | `a+b` i16 | 32 × 16 | no | wrap mod 2¹⁶ | `[HIGH/OBSERVED]` |
| `IVP_ADDN_2X32` | `a+b` i32 | 16 × 32 | no | wrap mod 2³² | `[HIGH/OBSERVED]` |
| `IVP_ADDSNX16` | `sat(a+b)` i16 | 32 × 16 | **YES** | clamp `[-2¹⁵, 2¹⁵-1]` | `[MED/INFERRED]` |
| `IVP_SUB{2NX8,NX16,N_2X32}` | `a-b` signed | per w | no | wrap | `[HIGH/OBSERVED]` |
| `IVP_SUBSNX16` | `sat(a-b)` i16 | 32 × 16 | **YES** | clamp | `[MED/INFERRED]` |
| `IVP_NEG{2NX8,NX16,N_2X32}` | `-a` (2's-comp) | per w | no | wrap on `INT_MIN` | `[MED/INFERRED]` |
| `IVP_NEGSNX16` | `sat(-a)` i16 | 32 × 16 | **YES** | clamp (handles `INT16_MIN`) | `[MED/INFERRED]` |
| `IVP_ABS{2NX8,NX16,N_2X32}` | `\|a\|` | per w | no | wrap on `INT_MIN` | `[MED/INFERRED]` |
| `IVP_ABSSNX16` | `sat(\|a\|)` i16 | 32 × 16 | **YES** | clamp (`\|INT16_MIN\|→INT16_MAX`) | `[MED/INFERRED]` |
| `IVP_ABSSUB{2NX8,NX16}` | `\|a-b\|` signed | per w | no | — | `[HIGH/OBSERVED]` |
| `IVP_ABSSUBU{2NX8,NX16}` | `\|a-b\|` unsigned | per w | no | — | `[HIGH/OBSERVED]` |
| `IVP_ABSSSUBNX16` | `sat(\|a-b\|)` i16 | 32 × 16 | **YES** | clamp | `[MED/INFERRED]` |
| `IVP_MAX/MIN{2NX8,NX16,N_2X32}` | `max/min(a,b)` signed | per w | — | — | `[HIGH/OBSERVED]` |
| `IVP_MAXU/MINU{2NX8,NX16}` · `…UN_2X32` | `max/min(a,b)` unsigned | per w | — | — | `[HIGH/OBSERVED]` |
| `IVP_MINORMAX2NX8` · `…U2NX8` | combined min&max selector (s/u) | 64 × 8 | — | — | `[LOW/INFERRED]` |
| `IVP_ADD/SUBNXF16` · `…N_2XF32` | `a±b` fp16/fp32 | 32/16 | — | round: `RoundMode` SR | `[MED/INFERRED]` |
| `IVP_NEG/ABSNXF16` · `…N_2XF32` | sign flip / clear sign | 32/16 | — | exact | `[MED/INFERRED]` |
| `IVP_MIN/MAXNXF16` · `…N_2XF32` | fp min/max (operand-order NaN) | 32/16 | — | — | `[MED/INFERRED]` |
| `IVP_MIN/MAXNUMNXF16` · `…N_2XF32` | IEEE-754 `minNum`/`maxNum` (NaN-suppress) | 32/16 | — | — | `[MED/INFERRED]` |
| `IVP_ADDEXP{,M}NXF16` · `…N_2XF32` | add int to fp **exponent** field (ldexp-style) | 32/16 | — | — | `[LOW/INFERRED]` |
| `IVP_MULSGN{,S}NX16` | multiply-by-sign (±1, 0) — ALU slot `S3` | 32 × 16 | (S) | — | `[HIGH/OBSERVED slot]` |

> **NOTE on rounding & FP flags.** Integer ALU ops carry no rounding (`n/a`). Float ops round per
> the architectural `RoundMode` special register and set `IVP_FS0..7` exception flags — but both the
> rounded value *and* the flag updates are produced **host-side**; cas only lights the op's decode
> bit (e.g. `ADDN_2XF32` in the `0x7f9` flag region) and forwards. The fp status/flag state machine
> (`RoundMode` + the `{Invalid,DivZero,Overflow,Underflow,Inexact}{Flag,Enable}` pairs) is owned by
> [cas Convert / Pack / FP](./cas-convert-pack-fp.md). `[MED/INFERRED]`

---

## 5. The `NEURON_ISA_TPB_ALU_OP` ↔ IVP mnemonic ↔ semantics binding

The firmware `NEURON_ISA_TPB_ALU_OP` enum is the Pool / Activation / Tensor-engine op selector;
each value is implemented on the IVP vector core by one of §4's mnemonics. The enum is a
**`NEURON_ISA_PACKED` 1-byte** field with **60 entries**: a contiguous base band `0x00..0x1D`, then
a 30-op integer-engine band `0xC4..0xE1` (bit `[7:6] == 0b11` selects the integer engine). The
codes below are **grounded** on the firmware arch-ISA header `aws_neuron_isa_tpb_common.h:939`
and cross-referenced by the `.xt.prop` signatures of the firmware consumers
(`setup_64bit_rw(uint, NEURON_ISA_TPB_ALU_OP)`, `tensor_tensor_64bit_dispatch<…>(…, ALU_OP)`).

> **Two distinct confidences — keep them separate.** The enum **codes** (the left column) are
> `[HIGH/OBSERVED]` — they are read from a header line and confirmed by every consumer's type
> signature. The **mnemonic join** (which IVP op realises each code) is this page's contribution
> from the `libcas-core` string pool + ALU-slot dispatch, and is `[MED/INFERRED]` at best —
> *do not* conflate "code 0x08 is `MAX`" (high) with "code 0x08 is realised by `IVP_MAXNX16`"
> (inferred). `[HIGH/OBSERVED for codes; MED/INFERRED for join]`

| ALU-OP | code | IVP mnemonic family (ALU slot) | semantics | conf (join) |
|---|---|---|---|---|
| `ADD` | `0x04` | `IVP_ADD{2NX8,NX16,N_2X32}` · `ADDN_2XF32`/`ADDNXF16` | signed int / fp add (RoundMode) | `[MED/INFERRED]` |
| `SUBTRACT` | `0x05` | `IVP_SUB{2NX8,NX16,N_2X32}` · `SUBN_2XF32`/`SUBNXF16` | signed int / fp sub | `[MED/INFERRED]` |
| `MULT` | `0x06` | `IVP_MUL*` (Mul slot `S2`) | int / fp multiply → [cas MAC / FMAC](./cas-mac-fmac.md) | `[HIGH/OBSERVED slot]` |
| `MAX` | `0x08` | `IVP_MAX{2NX8,NX16,N_2X32}` · `MAXN_2XF32`/`MAXNXF16(,NUM)` | signed int / fp max (`NUM`=NaN-suppress) | `[MED/INFERRED]` |
| `MIN` | `0x09` | `IVP_MIN{2NX8,NX16,N_2X32}` · `MINN_2XF32`/`MINNXF16(,NUM)` | signed int / fp min | `[MED/INFERRED]` |
| `ABSOLUTE_DIFF` | `0x17` | `IVP_ABSSUB{2NX8,NX16}` | `\|a-b\|` signed | `[MED/INFERRED]` |
| `ABSOLUTE_VALUE` | `0x19` | `IVP_ABS{2NX8,NX16,N_2X32}` | `\|a\|` (signed; `INT_MIN` wraps) | `[MED/INFERRED]` |
| `ADD_INT` | `0xC4` | `IVP_ADD*` (signed/unsigned share) | int add (dtype-typed) | `[MED/INFERRED]` |
| `MULT_INT` | `0xC5` | `IVP_MUL*` (signed, Mul slot) | signed int mul | `[MED/INFERRED]` |
| `SUBTRACT_INT` | `0xC6` | `IVP_SUB*` | int sub | `[MED/INFERRED]` |
| `ABS_MAX_INT` | `0xCB` | `IVP_MAX*` of `\|a\|,\|b\|` (composed) | signed abs-max | `[LOW/INFERRED]` |
| `ABS_MIN_INT` | `0xCC` | `IVP_MIN*` of `\|a\|,\|b\|` (composed) | signed abs-min | `[LOW/INFERRED]` |
| `ABS_DIFF_INT` | `0xCD` | `IVP_ABSSUB*` (signed) / `IVP_ABSSUBU*` (unsigned) | `\|a-b\|` | `[MED/INFERRED]` |
| `ABS_VALUE_INT` | `0xCE` | `IVP_ABS*` (signed) | signed `\|a\|` | `[MED/INFERRED]` |
| `MAX_INT` | `0xCF` | `IVP_MAX{NX16,2NX8,N_2X32}` | signed int max | `[MED/INFERRED]` |
| `MIN_INT` | `0xD0` | `IVP_MIN{NX16,2NX8,N_2X32}` | signed int min | `[MED/INFERRED]` |
| `MAX_UINT` | `0xD5` | `IVP_MAXU{NX16,2NX8}` · `MAXUN_2X32` | unsigned max | `[MED/INFERRED]` |
| `MIN_UINT` | `0xD6` | `IVP_MINU{NX16,2NX8}` · `MINUN_2X32` | unsigned min | `[MED/INFERRED]` |
| `AMAX/AMIN_(U)INT` | `0xDE–0xE1` | argmax/argmin (index out u32; reduction path) | not a per-lane ALU op | `[LOW/INFERRED]` |
| `DIVIDE`/`POW`/`MOD`/`RSQRT` | `0x07`/`0x1A`/`0x1B`/`0x1D` | reciprocal / Newton / poly | not general-arith → [cas Convert / FP](./cas-convert-pack-fp.md) | — |

> **CORRECTION — the saturating IVP variants have no enum counterpart.** The saturating
> mnemonics (`ADDS`/`SUBS`/`NEGS`/`ABSS`/`ABSSSUB-NX16`) bind to **no** `NEURON_ISA_TPB_ALU_OP`
> code in the 60-entry table. In the tensor-engine path saturation is a *separate dtype/clamp
> stage*, whereas the IVP core exposes saturation as a **distinct opcode**. So the enum→mnemonic
> join is *not* a bijection: several IVP arith opcodes (every `…S…` saturating form, plus
> `MINORMAX`, `ADDEXP`, `MULSGN`) exist purely on the IVP side and are reached by **name**, not by
> any ALU-OP code. Treat the §5 table as a *partial* map. `[MED/INFERRED]`

---

## 6. ISA cross-validation

The ALU-slot mnemonics here are corroborated by the per-instruction ISA reference batches
[B01](../isa/ref/b01-vec-alu-int.md) (integer ALU), [B02](../isa/ref/b02-vec-alu-fp.md) (float ALU),
[B03](../isa/ref/b03-vec-alu-rest.md) (abs-diff / saturating / predicated rest), with the formal
op definitions in [group-semantics-i](../isa/semantics/group-semantics-i.md):

* **Names & spelling.** Every ALU-slot `IVP_*` arithmetic mnemonic found here appears in the ISA
  opcode/iclass tables (e.g. `ivp_minn_2x32` opc#1028, iclass#941 = `IVP_MINN_2X32`, package
  `xt_ivp32`, slots `F0_S3_ALU` / `F11_S3_ALU` / `F11_S4_ALU`). The ISA's lowercase intrinsic names
  = this DLL's UPPER-case iclass strings.
* **Signedness as opcode.** The ISA finds signed vs unsigned (`minnx16` vs `minunx16`) differ *only*
  in the opcode-selector field — independent `opcodedefs` rows, not a global bit. This DLL
  corroborates exactly: `MAXNX16` (`0x71c.4`) vs `MAXUNX16` (`0x71e.2`), `MINNX16` (`0x718.10`) vs
  `MINUNX16` (`0x71a.8`) are distinct decode bits.
* **Predication (`T`).** The `…T` variants are independent rows here too (`ADDNX16` lights a
  *different* bit from `ADDNX16T`), matching the ISA's per-`(mnemonic, slot)` row model and the
  operand-role `vp`/`vbool` guard.
* **Slot assignment.** The ISA places these in the vector (`ivp_*`) iclasses, package `xt_ivp32`;
  this DLL realises them in issue slots `S3`/`S4` (ALU) — distinct from the Mul slot `S2` and Load
  slots `S0`/`S1`, consistent with a VLIW that dual-issues ALU ops.

These mnemonics are independently *executable* in `libfiss-base.so`; the live round-trip — drive the
`module__xdref_*` leaf and check the bytes — is documented as VAL-01 / VAL-02 (Part 15) and threaded
through [fiss Datapath](./fiss-datapath-oracle.md).

---

## 7. Honesty ledger

**`[HIGH/OBSERVED]` (read-from-byte / proven by disassembly):**
* Zero packed-SIMD arith (int **0**, float **0**) → no element compute in the DLL; **35 473** scalar
  add/sub/imul are address/index/timing only; value delegation via the `call *0xe7098(%rbx)` host
  callback (9 sites) + named `nx_*` PLT ports (120 imports = `memset` + 119 `nx_*_interface`).
* The decode-bitmap dispatch model: per-`(format,slot,mnemonic)` bit set by a `stage0` wrapper,
  tested by the shared `…semantic_stage10` executor (364 `testb`), funnelled to one host hand-off;
  the exact mnemonic→bit map for the cited ops (byte-verified).
* 16-stage ALU pipeline; execute/host-callback at stage 10 (`esi=$0xa`); trace cursor
  `0x4a09dc` (slot) / `0x4a09e0` (stage); ADD/SUB descriptor arrays element-wise parallel.
* The decode record `{ptr 0x17ba9ed("IVP_ADDNX16"), 0x10, ptr 0x2082000}` @ file `0x1e70ea0`.
* `IVP_ADDMOD16U` dispatched in Load slot `S1`, not ALU.
* ISA cross-validation: names, signedness-as-opcode, predication-as-row, slot placement.
* `NEURON_ISA_TPB_ALU_OP` enum **codes** (60 entries, base `0x00..0x1D` + int `0xC4..0xE1`).

**`[MED/INFERRED]` (reasoned over OBSERVED):**
* Exact saturation clamp bounds and fp rounding = `RoundMode` (read from naming grammar; *valued* by
  fiss, not in-DLL — confirmed against the `module__xdref_*` saturating leaves).
* The enum → IVP-mnemonic **join** for the int band (`0xC4…`) and `ADD_INT`/`SUB_INT` typing.
* Retire latency beyond "execute at stage 10".

**`[LOW/INFERRED]`:**
* `ABS_MAX_INT`/`ABS_MIN_INT` as `IVP_MAX`/`MIN`-of-abs compositions (no single mnemonic).
* `MINORMAX` combined-op output packing; `ADDEXP` exact exponent semantics.
* `AMAX`/`AMIN` argmax/argmin lane semantics (reduction path, not per-lane ALU).

> **The single most important takeaway.** When you reimplement, **split the responsibilities the
> way the shipped ISS does**: this `cas` model owns *decode + a 16-deep ALU pipeline that executes
> at stage 10 + the hazard scoreboard*, and forwards the op identity (one decode bit) to a value
> oracle; the `fiss` library owns *the bit-exact `a OP b`* (one `module__xdref_*` leaf per lane).
> Do not try to recover saturation/rounding/NaN bounds from `libcas-core` — they are physically not
> there. Get them from [fiss Datapath](./fiss-datapath-oracle.md).
