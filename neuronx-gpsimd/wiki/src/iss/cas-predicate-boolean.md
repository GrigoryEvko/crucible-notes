# cas/fiss Predicate / Boolean (vbool) Semantics

This page documents the **vector-boolean (`vbool`) predicate subsystem** of the
Vision-Q7 / Cayman IVP core as it is modeled by the cycle-accurate ISS. It is a
page of [Part 14 — the ISS as Executable Oracle](./cas-core-surface.md) and rests
on the keystone that governs every page in this Part:

> **Keystone — cas decodes, fiss values.** The cycle-accurate ISS
> `libcas-core.so` (`BASE = libcas-Xtensa.so`, `VERS_1.1`, GCC 4.9.4, modeled core
> = Cayman / Vision-Q7 IVP VLIW) computes **DECODE + TIMING + REGISTER-HAZARD**,
> but it does **NOT** compute element **values**. The per-lane compare and boolean
> bit-math are delegated to the host through the **119 `nx_*_interface` TIE-port
> callbacks**; the actual value functions live in `libfiss-base.so` (the 864
> `module__xdref_*` value leaves / 12 569 `slotfill__` slots, drivable live via
> `ctypes` — [fiss Datapath, the value oracle](./fiss-datapath-oracle.md)). The
> cas plugin ABI is a flat C `dll_*` accessor vocabulary — **not**
> `getInterface` / `CycleCore::register_client`. This page keeps the
> cas(timing)-vs-fiss(value) split coherent for every predicate primitive.

For a predicate, the split is concrete and visible:

* **Where the predicate is *born*** — a compare (`olt`/`oeq`/`ole`, `lt`/`eq`/`le`,
  `ltu`/`leu`) reads two `vec` registers and writes a `vbool`. The cas half
  schedules the `vec` reads and posts the `vbool` write at a fixed latency; the
  fiss half does the NaN-aware bit-compare (§3, §4).
* **What you *do* with it** — boolean algebra (`andb`/`orb`/`xorb`/`notb`/`andnotb`),
  predicate-reduce (`randbn`/`rorbn`), and the bridges to the matmul `pr` file
  (§5, §7).
* **Where it *gates*** — the `_t` masked forms (`add*t`/`sel*t`/`mov*t`/`sv*t`)
  consume a `vbool` per lane through the `bitkillt` mask and **merge** (§6).

Confidence/provenance tags follow [the Confidence & Walls model](../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read from disassembled bytes this pass, `[MED/INFERRED]` =
reasoned over OBSERVED evidence, `[…/CARRIED]` = re-used from a reconciled sibling
report at its confidence. All offsets are into the shipped binaries:

```
extracted/.../ncore2gp/config/libcas-core.so   45,878,080 B  ELF64 x86-64  NOT stripped  (DECODE+TIMING)
extracted/.../ncore2gp/config/libfiss-base.so  12,330,016 B  ELF64 x86-64  NOT stripped  (VALUE)
extracted/.../ncore2gp/config/libisa-core.so    9,690,712 B  ELF64 x86-64  NOT stripped  (ENCODE/ROSTER)
```

> **GOTCHA — VMA vs file offset is not uniform.** In `libcas-core.so`, `.text`
> (`0x572fa0`) and `.rodata` (`0x17ba840`) are **VMA == file offset**; the writable
> `.data.rel.ro` (VMA `0x2070900` → file `0x1e70900`) and `.data` (VMA `0x2280ed8`
> → file `0x2080ed8`) carry a **`0x200000` delta** (`readelf -SW`). The
> `dll_regfile_table` (§2) lives in `.data.rel.ro`, so every `xxd`/`objdump -s` on
> it subtracts `0x200000`. In `libfiss-base.so`, `.text` (`0x190430`) is
> **VMA == file offset** — `xdref` bodies disassemble at raw VMA. `[HIGH/OBSERVED]`

---

## 1. The executive finding — five decoded layers

The predicate subsystem has **five layers**, each obeying the cas-DECODE/TIMING vs
fiss-VALUE split:

| Layer | What | cas LAT | fiss primitive | § |
|---|---|---|---|---|
| `vbool` register file | 16 regs × 64-bit (1 bit / sub-lane) | — (state) | — | §2 |
| COMPARE → `vbool` | ordered-fp `olt`/`oeq`/`ole` (+unord) + int `lt`/`eq`/`le` (signed) + `ltu`/`leu` (unsigned) | 10 (fp def) / 11 (int def) | `olt`/`oeq`/`ole` + `lt`/`le`/`eq`/`ltu`/`leu` | §3/§4 |
| BOOL over `vbool` | `andb`/`orb`/`xorb`/`notb`/`andnotb`/`ornotb` | 10 (S1_Ld) | `*b_64_64_64` | §5 |
| CONSUME (`_t` masked) | `add*t`/`sel*t`/`dsel*t`/`mov*t`/`sv*t` (per-lane **MERGE**, not zero) | 10 | `bitkillt` mask + per-lane op | §6 |
| BOOL-REDUCE | `randbn` = reduce-AND / `rorbn` = reduce-OR (fold mask → scalar bool) | 10 | `randbn`/`rorbn`_64_64 | §7 |

The decisive facts, each re-challenged against the binary this pass:

* **The `vbool` file is 16 entries × 64 bits.** `opnd_sem_vbool_addr` masks the
  operand with `and $0xf` (16 regs); `dll_regfile_table[3]` reads
  `{name="vbool", short="vb", count=0x10, width=0x40}`. A separate `b32_pr`
  (`regfile[6]`, 16 × 64) feeds the matmul `pr` operand; the scalar Xtensa `BR`
  file (`regfile[1]`, 16 × 1-bit) is the single-bit cousin. `[HIGH/OBSERVED]`
* **Ordered fp compares are FALSE on NaN; unordered are TRUE on NaN.** The ordered
  bodies `and`-in a "both-operands-ordered" (not-NaN) flag before the store; the
  unordered bodies `or`-in the NaN flag. This is exactly IEEE-754
  ordered/unordered predicate semantics, byte-confirmed with full binary16/binary32
  NaN classification, integer-only soft-float (zero x87/SSE). `[HIGH/OBSERVED]`
* **The `_t` masked consume is a MERGE, not a zero-fill.** `bitkillt` emits
  **all-ones in the KILLED (predicate-FALSE) lane** — the "keep the destination"
  mask. A killed lane retains its prior destination value; a live lane takes the
  fresh op result. `bitkillf` is the inverse polarity. `[HIGH for polarity/OBSERVED;
  destination-retain at commit INFERRED-HIGH]`
* **`randbn` is reduce-AND, `rorbn` is reduce-OR — not RNG.** `randbn` gathers the
  per-lane bits and tests `cmp $0xffffffff ; sete` (all set); `rorbn` ends in
  `setne` (any set). `[HIGH/OBSERVED]`
* **cas never inspects the predicate value.** Ordered/unordered/signed/unsigned
  variants share one cas timing model; only the host/fiss callback differs.
  `[HIGH/OBSERVED]`

---

## 2. The `vbool` register-file model

### 2.1 The operand-index accessors (cas, byte-exact)

The operand field width *is* the file size. Read directly from `libcas-core.so`:

```asm
; opnd_sem_vbool_addr @0x17aa280
17aa280:  89 f0        mov    %esi,%eax
17aa282:  83 e0 0f     and    $0xf,%eax        ; 4-bit field  => 16 vbool registers
17aa285:  c3           ret
; opnd_sem_b32_pr_addr @0x17aa2a0
17aa2a0:  89 f0        mov    %esi,%eax
17aa2a2:  83 e0 0f     and    $0xf,%eax        ; 4-bit field  => 16 b32_pr registers
17aa2a5:  c3           ret
```

For contrast, the neighbouring accessors size the other files: `opnd_sem_vec_addr`
`and $0x1f` (32 `vec`), `opnd_sem_gvr_addr` `and $0x7` (8 gather), the MAC/align
accessors `and $0x3` (4). `[HIGH/OBSERVED — all read at 0x17aa270..0x17aa2c0]`

### 2.2 The `dll_regfile_table` — byte-exact

`dll_get_regfile_table @0x17aa380` returns `dll_regfile_table` at VMA `0x227eac0`
(`.data.rel.ro`, file offset `0x207eac0`, **delta `0x200000`**). Record stride is
**56 bytes** (`0x38`): `{ char* name; char* short; u64 (count<<32 | widthBits);
fn ptrs… }`. The `+0x10` field of record `i` parses at file offset
`0x207eac0 + i*0x38 + 0x10`. All eight records parse (matching the "8 regfiles" of
the ISA roster):

| idx | name | short | count | widthBits | role |
|---|---|---|---:|---:|---|
| 0 | `AR` | `a` | 64 | 32 | scalar windowed `AR` file |
| 1 | `BR` | `b` | 16 | 1 | Xtensa scalar **BOOLEAN** (1-bit) file |
| 2 | `vec` | `v` | 32 | 512 | 512-bit vector data registers |
| 3 | **`vbool`** | `vb` | **16** | **64** | **VECTOR PREDICATE** (1 bit / sub-lane) |
| 4 | `valign` | `u` | 4 | 512 | unaligned-access align registers |
| 5 | `wvec` | `wv` | 4 | 1536 | wide MAC accumulator |
| 6 | **`b32_pr`** | `pr` | **16** | **64** | PACKED PREDICATE-REGISTER (MAC `pr`) |
| 7 | `gvr` | `gr` | 8 | 512 | SuperGather registers |

`[HIGH/OBSERVED — each `+0x10` qword byte-read this pass.]`

> **CORRECTION — the count/width field order, pinned by cross-check.** The `+0x10`
> qword is `(count << 32) | widthBits`: the **high** 32 bits are the register count,
> the **low** 32 bits are the bit-width. This is *not* obvious from a single record,
> so it is pinned against three records of known geometry, byte-read this pass
> (`xxd -e`, low word then high word):
>
> | record | low word | high word | known geometry | ⇒ field order |
> |---|---:|---:|---|---|
> | `AR` | `0x20` (32) | `0x40` (64) | **64 regs × 32-bit** | count = high, width = low ✓ |
> | `vec` | `0x200` (512) | `0x20` (32) | **32 regs × 512-bit** | count = high, width = low ✓ |
> | `wvec` | `0x600` (1536) | `0x04` (4) | **4 regs × 1536-bit** | count = high, width = low ✓ |
> | `vbool` | `0x40` (64) | `0x10` (16) | ⇒ **16 regs × 64-bit** | (derived) |
>
> So `vbool`'s qword `0x0000001000000040` decodes to **count = `0x10` = 16**,
> **width = `0x40` = 64** — exactly consistent with the `opnd_sem_vbool_addr`
> `and $0xf` (16 registers). A naïve "low = count" reading would (wrongly) make
> `vec` a 512-register file and `vbool` a 64-register file; the `AR`/`vec`/`wvec`
> cross-check rules that out. `[HIGH/OBSERVED]`

### 2.3 The width — 64 bits = 1 predicate bit per 8-bit sub-lane

A 64-bit `vbool` register holds exactly one bit per **8-bit sub-lane** of the
512-bit datapath: the densest format is `2NX8` = 64 int8 lanes, so all 64 bits are
meaningful there; for `NX16` (32 lanes) only 32 bits are meaningful, for `N_2X32`
(16 lanes) only 16. Every fiss bitwise/reduce body treats the register as **two
32-bit halves** (`andb_64_64_64`, `randbn_64_64`, the `predflag*` packers).

> **NOTE — "1 bit per 8-bit sub-lane", refined.** The 64-bit register *width* is
> byte-read (`width=0x40`). The "one bit per 8-bit sub-lane" reading is corroborated
> by [B11 — vbool ALU §1](../isa/ref/b11-vbool-alu.md) (which records *predicate
> width = 1 bit / 8-bit lane*) and by the per-lane bit indexing in `randbn`; the
> shorthand "1 bit / lane" is only exact for the 64-lane `2NX8` case. `[HIGH for
> 64-bit width / OBSERVED; sub-lane mapping MED/CARRIED from B11]`

### 2.4 Three predicate-ish files — distinct roles

* **`BR` (16 × 1-bit)** — the base-Xtensa scalar boolean file (`xt_booleans`
  package). The single-bit boolean ops `notb1`/`andnotb1`/`ornotb1`
  (`xdref_notb1_1_1` @`0x5b8020`, `andnotb1_1_1_1` @`0x5b8030`) target it.
* **`vbool` (16 × 64-bit)** — the general vector predicate: the compare result, the
  `_t` mask, the select guard. It is the dominant predicate file by callsite count.
* **`b32_pr` (16 × 64-bit)** — the **packed predicate-register** fed to the matmul
  as the `pr0`/`pr1`/`pr2` operand (the MAC `pr` operand of the convolution ops).
  Converted from `vec` by `MOVVPR`, copied by `MOVPRPR`, packed by the `predflag*`
  ops; consumed by `EXTRPR`/`DEXTRPR` and the `MUL*XR16` family. `[HIGH/OBSERVED —
  the operand accessors + roster.]`

> **QUIRK — the `pr` file is not the `vbool` file.** A reimplementation must keep
> `vbool` (general per-lane predicate, consumed by `bitkillt`) and `b32_pr` (matmul
> column-mask, consumed by the systolic MAC) as **separate 16×64 files** with
> separate operand fields. They share the `and $0xf` indexer geometry but never the
> same physical register. `[HIGH/OBSERVED]`

---

## 3. The predicate-generating FP compares (ordered + unordered, NaN-aware)

### 3.1 The roster

Per fp format — `N_2XF32` (16 lanes) and `NXF16` (32 lanes), each with a base and
a `_t` predicated form:

```
ORDERED    : ivp_oeqn_2xf32(t) / oeqnxf16(t)      ; == ordered equal
             ivp_olen_2xf32(t) / olenxf16(t)      ; <= ordered
             ivp_oltn_2xf32(t) / oltnxf16(t)      ; <  ordered
UNORDERED  : ivp_ueqn / ulen / ultn / uleqn / ultqn / uneqn / unn _2xf32(t) (+ *xf16)
             ; un = the pure isUnordered (isNaN(a) || isNaN(b)) test
```

12 distinct ordered, 31 unordered, all with `_t` variants. `[HIGH/OBSERVED]`

### 3.2 The NaN semantics — byte-exact (fiss)

The ordered fp16 less-than `module__xdref_olt_1_1_16f_16f @0x521e60` classifies NaN
fully (binary16 = 1-5-10: `exp=(x>>10)&0x1f`, `mant=x&0x3ff`, NaN iff
`exp==0x1f && mant!=0`), computes the magnitude `<` on the sign-`7fff` form
(`and $0x7fff ; cmp ; setb`), and writes the **final ordered predicate** as the
AND of the compare result with the not-NaN flag:

```asm
; module__xdref_olt_1_1_16f_16f, tail @0x521ef7
521ef7:  39 d7        cmp    %edx,%edi          ; magnitude compare
521efc:  0f 92 c7     setb   %dil               ; raw a<b
…                                               ; fold the not-NaN / ordered flags …
521f2f:  21 f8        and    %edi,%eax          ; result &= ORDERED(both-not-NaN)
521f32:  41 89 00     mov    %eax,(%r8)         ; => ordered predicate, FALSE if any NaN
```

`ole`/`oeq` share the shape (`ole @0x521d70` tail `and %eax,%edx ; mov %edx,(%r8)`;
`oeq @0x521f40` tail `cmp ; sete ; and %edx,%eax ; mov %eax,(%r8)`). The unordered
counterpart `module__xdref_ult_1_1_16f_16f @0x51be10` instead **OR**s the NaN flag
into the result before the store (`or %r9d,%ebp ; mov %ebp,(%r8)` @`0x51bf02`) so an
unordered compare is **TRUE** when an operand is NaN.

> **The IEEE-754 ordered/unordered rule, byte-confirmed.** `o*` compares
> (`olt`/`ole`/`oeq`) return **FALSE** if either operand is NaN (the body `and`s in
> the ordered flag); `u*` compares (`ult`/`ueq`/…) return **TRUE** if either is NaN
> (the body `or`s in the NaN flag); `unn` is the pure `isUnordered` test. The whole
> classification is **integer-only soft-float** — no x87/SSE; binary16 uses
> `0x1f`/`0x3ff`/`0x7c00`, binary32 uses `0xff`/`0x7fffff`/`0x7f800000`.
> `[HIGH/OBSERVED — `olt`/`ole`/`oeq`/`ult` bodies read this pass.]`

### 3.3 The cas decode/encode — the +0x1000 predicate nibble (libisa-core)

The fp-compare predicate is a per-format **enumerated nibble**, not a global bit.
On `F1_S3_ALU`, the `Opcode_*_encode` thunks write (byte-exact, this pass):

```asm
; Opcode_ivp_oeqn_2xf32_Slot_f1_s3_alu_encode @0x34b490
34b490:  c7 07 00 c8 05 27   movl $0x2705c800,(%rdi)   ; predicate nibble = 5 (eq)
; Opcode_ivp_olen_2xf32_..._encode @0x34b2d0
34b2d0:  c7 07 00 c8 06 27   movl $0x2706c800,(%rdi)   ; predicate nibble = 6 (le)
; Opcode_ivp_oltn_2xf32_..._encode @0x34b3b0
34b3b0:  c7 07 00 c8 07 27   movl $0x2707c800,(%rdi)   ; predicate nibble = 7 (lt)
```

The `{eq:5, le:6, lt:7}` field steps **+0x1000** each. The **unordered** variants
flip the `c8` → `cc` byte (the bit-10 "unordered" toggle, e.g. `ueqn` →
`0x2704cc00`) and use a distinct nibble; `unn` encodes `0x2782c800` (the pure
isUnordered test). `[HIGH/OBSERVED — read from the encode thunks; reconciles the
ISA `+0x1000` predicate-nibble model.]`

### 3.4 The result `vbool` lane layout

The per-lane compare writes a small predicate word (the int compares mask the
result `and $0x3` and write `0x3`/`0x0`; the fp compares write `0`/`1`); the `_t`
per-lane consume reads bit 0 (`bitkillt … and $0x1`). The per-instance staging then
packs these into the 64-bit `vbool` register (the `predflag` packers do the
lane→bit compaction — §7.3). `[HIGH/OBSERVED]`

---

## 4. The predicate-generating integer compares

### 4.1 The roster

Per width `{2NX8, NX16, N_2X32}`:

```
SIGNED    : ivp_lt* / le* / eq*                  (15 distinct in the stage-function set)
UNSIGNED  : ivp_ltu* / leu* / geu*
```

There is **no separate signed `IVP_GT*`/`IVP_GE*` mnemonic** in the roster: `gt`/`ge`
are realised by operand swap (against `lt`/`le`) or via the unsigned `geu` set.
`[HIGH/OBSERVED for the roster; the swap-realisation INFERRED from the absence.]`

### 4.2 The fiss value — byte-exact (the int16 forms @`0x8585c0`…)

```c
// module__xdref_lt_2_16_16 @0x8585c0  — SIGNED less-than via SIGN-BIAS
// for each operand x: flip the sign bit to map signed order onto unsigned order
//   shr $0xf ; test ; sete ; and $0x7fff ; shl $0xf ; or   (sign-bias)
// then a bare unsigned-style  cmp ; sbb  ; result &= 0x3 ; write (%rcx)
// module__xdref_le_2_16_16 @0x858600  — same sign-bias ; cmp ; setbe ; neg     [signed <=]
// module__xdref_eq_2_16_16 @0x858640  — bare cmp ; sete ; neg ; and $0x3       [equal]
// module__xdref_neq_2_16_16@0x858650  — cmp ; setne                            [not-equal]
// module__xdref_ltu_2_16_16@0x858660  — BARE cmp ; sbb ; and $0x3              [UNSIGNED <]
// module__xdref_leu_2_16_16@0x858670  — bare cmp ; setbe ; neg                 [UNSIGNED <=]
```

So **signed** compares use the sign-bias + unsigned-compare trick (the same
structural trick as the fiss min/max); **unsigned** compares are a bare `cmp`. The
8-bit (`@0x5bc450`…) and 32-bit (`@0x5c0cc0`…) forms share the shape per width. The
result is a 2-bit per-lane predicate value (`0x3` = true / `0x0` = false).
`[HIGH/OBSERVED]`

### 4.3 The cas timing — int compare posts its def one stage later

The int-compare issue fn `LTNX16` writes its `vbool` **result** at **LAT 11**
(`mov $0xb,%esi` immediately after `opnd_sem_vbool_addr`) and reads its two `vec`
sources at LAT 10; the fp compare (`OLEN`, §8) posts its `vbool` **result** at
**LAT 10**. The 1-stage difference is the def-post position; both are forwardable
within the `vbool` interlock window (§8). `[HIGH/OBSERVED — both issue fns read
this pass.]`

---

## 5. The boolean ops over `vbool` (and/or/xor/not/and-not)

### 5.1 The roster

`IVP_ANDB`, `ORB`, `XORB`, `NOTB`, `ANDNOTB`, `ORNOTB` over `vbool`, plus the 1-bit
`BR` forms `NOTB1`, `ANDNOTB1`, `ORNOTB1`. There is **no `XORNOTB`** — XOR has no
not-variant. `[HIGH/OBSERVED]`

### 5.2 The fiss value — plain 64-bit bitwise, two 32-bit halves

ABI: `A=%rsi`, `B=%rdx`, result=`%rcx`; each body processes the register as two
32-bit words:

```c
// module__xdref_notb_64_64    @0x856f70 :  dst = ~A
// module__xdref_andb_64_64_64 @0x856f80 :  dst = A & B
// module__xdref_orb_64_64_64  @0x856f90 :  dst = A | B
// module__xdref_xorb_64_64_64 @0x856fa0 :  dst = A ^ B
// module__xdref_andnotb_64_64_64 @0x856fb0 : dst = A & ~B   // B complemented: not(%rdx);and(%rsi)
// module__xdref_mov_64_64     @0x856fd0 :  dst = A          // the vbool copy
```

Semantics are pure per-bit (= per-lane) boolean logic with **no element
boundaries** — the whole 64-bit register is processed at once. The 1-bit `BR`
analogues are `notb1_1_1 @0x5b8020` / `andnotb1_1_1_1 @0x5b8030`. `[HIGH/OBSERVED]`

> **NOTE — `andnotb` complements the SECOND operand.** The fiss leaf computes
> `A & ~B` — the **B** operand (`%rdx`) is the one inverted. The ISA reference batch
> [B11 — vbool ALU §4.1](../isa/ref/b11-vbool-alu.md) documents the same op under
> the `andnotb` mnemonic with the `_64_64_64` value-leaf signature, and places the
> whole logic group in the **Load** slot — consistent with §5.3 below.
> `[HIGH/OBSERVED + CARRIED]`

### 5.3 The cas decode/timing — boolean unit is the Load slot, not the ALU

`IVP_ANDB` issues in `F0_S1_Ld` (the boolean unit @`0x71d9a0`, **not** the ALU):
both `vbool` operands and the result resolve via `opnd_sem_vbool_addr` at **LAT 10**
(`mov $0xa,%esi`), plus an `AR` set-def at LAT 3. The encode thunk binds it to a
base-ISA boolean opcode:

```asm
; Opcode_ivp_andb_Slot_f0_s1_ld_encode @0x3418a0  (libisa-core)
3418a0:  c7 07 70 10 4a 00   movl $0x004a1070,(%rdi)   ; xt_booleans-package opcode
```

So the `vbool` boolean ops issue in `S1_Ld` and post at LAT 10. `[HIGH/OBSERVED —
encode byte-read this pass.]`

---

## 6. The predicate-consuming `_t` masked forms — MERGE, not zero

### 6.1 The per-lane guard `bitkillt` / `bitkillf` — byte-exact (fiss)

```asm
; module__xdref_bitkillt_16_2 @0x85cc20  — the `_t` TRUE-mask (16-bit lane)
85cc20:  83 e6 01        and    $0x1,%esi          ; take the lane's vbool bit
85cc23:  83 f6 01        xor    $0x1,%esi          ; invert -> (!pred)
85cc26:  f7 de           neg    %esi               ; -(!pred):  pred==1 -> 0x00000000
85cc28:  81 e6 ff ff..   and    $0xffff,%esi        ;            pred==0 -> 0x0000FFFF
85cc2e:  89 32           mov    %esi,(%rdx)         ; write the 16-bit KILL MASK
85cc30:  c3              ret
;   => mask = (pred ? 0x0000 : 0xFFFF)   — ALL-ONES in the KILLED (pred-FALSE) lane
```

```asm
; module__xdref_bitkillf_16_2 @0x82d000  — the INVERSE / FALSE-mask
82d000:  c1 e6 1f        shl    $0x1f,%esi
82d003:  c1 fe 1f        sar    $0x1f,%esi          ; broadcast bit0 across 32 bits
82d006:  81 e6 ff ff..   and    $0xffff,%esi
82d00c:  89 32           mov    %esi,(%rdx)
82d00e:  c3              ret
;   => mask = (pred ? 0xFFFF : 0x0000)   — ALL-ONES in the LIVE (pred-TRUE) lane
```

The 8-bit and 32-bit lane widths share the polarity:
`bitkillt_8_1 @0x5bc360` (`test;sete;neg;and $0xff`), `bitkillt_32_4 @0x528dd0`
(`and $1;xor $1;neg`). So **`bitkillt` = "all-ones where the lane should be KILLED
(kept)"`; `bitkillf` = "all-ones where the lane is LIVE (written)".** `[HIGH/OBSERVED]`

### 6.2 The merge semantics — how the mask is applied

The canonical `_t` arithmetic body
`opcode__ivp_addnx16t__stage_5 @0x256d50` (the 32-lane `NX16` predicated add) runs
three phases per instruction:

```c
// (a) build 32 per-lane kill masks
for (lane = 0; lane < 32; ++lane) {
    int pred_bit = (state[0x138 + …] >> shift) & 0x3;   // this lane's vbool bit
    kill[lane]   = module__xdref_bitkillt_16_2(pred_bit); // (pred ? 0x0000 : 0xFFFF)
}
// the 32 masks are packed two-per-32-bit-word (shl $0x10 ; or) into the per-instance
// WRITE-ENABLE / merge region at state offsets 0x6c..0xa8.

// (b) compute the 32 real adds (sources at state 0xb0.. and 0xf4..)
for (lane = 0; lane < 32; ++lane)
    result[lane] = module__xdref_add_16_16_16(srcA[lane], srcB[lane]);

// (c) the results pack back into the destination region (0x4c..0x68); the staged
// bitkillt masks gate the per-instance write at commit so a LIVE lane takes
// `result[lane]` and a KILLED lane RETAINS the prior destination value.
```

> **The `_t` masked op is a MERGE, not a zero-fill.** Predicate-FALSE lanes keep the
> **old destination** value; the surviving input in a killed lane is the destination
> (the merge target) — which is *why* `bitkillt` emits all-ones (the "keep the
> destination" mask) for killed lanes. The mask polarity and the staging into the
> write-enable region are **OBSERVED**; the final destination-retain at the
> `dll_cycle_advance` commit is **INFERRED-HIGH** (the polarity only makes sense as
> "keep the destination", and the commit writes the masked result, but the
> merge-with-old-dest commit step was not traced end-to-end inside the 16 KB
> `dll_cycle_advance`). `[HIGH for polarity/staging-OBSERVED; commit-retain
> INFERRED-HIGH]`

### 6.3 The `_t` family (reconciled across the report set)

| `_t` kind | example | mechanism |
|---|---|---|
| ARITHMETIC | `add*t` / `adds*t` / `sub*t` / `mul*t` | `bitkillt` per lane → merge (§6.2) |
| SELECT | `selnx16t` / `dselnx16t` / `seln_2x32t` | `opcode__ivp_selnx16t__stage_5 @0x4cc620` calls `bitkillt_16_2` per lane |
| PREDICATED MOVE | `mov2nx8t` / `movnx16t` | copy src→dst per `vbool` mask (gather blend) |
| MASKED STORE | `SVNX8UT` | `vbool` gates **which lanes store** (same `bitkillt` model) |
| REDUCE | `radd_nx16_*_t` | folds only `vbool`-TRUE lanes (mask staged in the larger frame) |

The predicate-move `IVP_MOV2NX8T` reads a `vbool` at LAT 10 plus the `vec` src/dst
at LAT 10. The masked store and the `_t` reduce reuse the **same** per-lane
`bitkillt` predicate gate. `[OBSERVED for the select/move; the store/reduce gates
CARRIED from the load/store and reduce siblings.]`

---

## 7. The boolean-reduce — `randbn` = reduce-AND / `rorbn` = reduce-OR

### 7.1 The roster

`IVP_RANDBN`, `RANDBN_2`, `RANDB2N` (AND-reduce); `IVP_RORBN`, `RORBN_2`, `RORB2N`
(OR-reduce). These are the boolean siblings of the arithmetic cross-lane reduce
(`radd`/`rmax`/`rmin`) — they fold a `vbool` to a **scalar boolean** and have
**nothing to do with RNG**.

> **CORRECTION — `randbn`/`rorbn` are not random-number generators.** The
> `rand`/`ror`-stemmed predicate ops are *reduce-**AND**-bool* and
> *reduce-**OR**-bool* respectively (the bodies are bit-reductions, §7.2). The only
> `rand`-stemmed ops in the whole ISA are this AND-reduce — there is no on-core RNG
> instruction here. `[HIGH/OBSERVED — bodies read this pass.]`

### 7.2 The fiss value — byte-exact

```asm
; module__xdref_randbn_64_64 @0x81cc90 (0x19d B)  — gather lane bits, then:
81ce21:  83 f8 ff        cmp    $0xffffffff,%eax   ; all 32 gathered bits set?
81ce24:  0f 94 c0        sete   %al                ; => 1 IFF ALL lanes true = reduce-AND
; module__xdref_rorbn_64_64 @0x81ce30 (0x19a B)  — SAME gather, OR-fold tail:
81cfc1:  0f 95 c0        setne  %al                ; => 1 IFF ANY lane bit set = reduce-OR
```

The narrow `b2n` forms are the clean all-set / any-set tests:
`randb2n_64_64 @0x81cc40` = `cmpl $0xffffffff,(%rsi) && cmpl $0xffffffff,0x4(%rsi)`
(both halves all-set); `rorb2n_64_64 @0x81cc70` = `test (%rsi); test 0x4(%rsi); setne`
(any bit). The `_2` variants (`randbn_2_64_64 @0x81cfd0`, `rorbn_2_64_64 @0x81d0a0`)
are second-lane-stride forms. `[HIGH/OBSERVED]`

> **GOTCHA — within-mask reduce vs multi-register fold.** `randbn`/`rorbn` reduce
> **the lanes of one mask** ("is every / any lane of *this* `vbool` set?"). They are
> distinct from the `borfs2n`/`bnorfs2n` ops, which OR/NOR-reduce **eight separate
> `vbool` registers** into one `IVP_FS*` predicate-state register. The ISA reference
> places the within-mask reduce in batch **B08** and the multi-register fold in
> **B11** — a reimplementation must not collapse the two. `[HIGH/OBSERVED + CARRIED
> from B11.]`

### 7.3 The predicate pack / scalar-condition generators

```
module__xdref_predflag2t_1_32_64 @0x5b3d60  — pack 32 lane flags (2 bits/lane) -> 64-bit vbool
module__xdref_predflag4t_1_16_64 @0x528ca0  — 16 lanes (4 bits/lane)
module__xdref_predflag8t_1_8_64  @0x1b0900  —  8 lanes (8 bits/lane)
bitkill_mov{eqz,nez,gez,ltz}_1 @0x5b7780..   — make a 1-bit predicate FROM a scalar:
                                              eqz=(x==0), nez=(x!=0), gez=(~sign), ltz=(sign)
```

The `predflag` packers do the lane→bit compaction noted in §3.4; the
`bitkill_mov*` ops are the scalar-condition predicate generators. `[HIGH/OBSERVED]`

### 7.4 The cas decode for `randbn`

`randbn` rides the shared boolean-slot stage functions (no dedicated `_issue`
thunk); its encode binds to a base-boolean-package opcode in the same band as
`andb`:

```asm
; Opcode_ivp_randbn_Slot_f0_s1_ld_encode @0x362480  (libisa-core)
362480:  c7 07 e0 13 4a 00   movl $0x004a13e0,(%rdi)   ; cf. andb 0x004a1070
```

Its result is a scalar boolean posted on the `BR`/`AR` timing. `[HIGH/OBSERVED for
the encode + slot; the exact scalar-bool result LAT (~`AR`-class) is INFERRED since
`randbn` has no dedicated cas `_issue` thunk.]`

---

## 8. The cas timing — compare→`vbool` latency + predicate forwarding

The compare/boolean/consume issue fns exist in several format/slot placements; the
`S2_Mul` placement is read below (`mov $LAT,%esi` before each scoreboard call).

| op | issue fn (one placement) | `vbool` def-LAT | operand reads | structural |
|---|---|---|---|---|
| `OLEN_2XF32T` (fp cmp) | `F0_F0_S2_Mul_28_inst_IVP_OLEN_2XF32T_issue` @`0xde1410` | 10 (`vbool`) | `vbool` 10 **and** 11, `vec` … | WB `0x15650` |
| `LTNX16` (int cmp) | `F0_F0_S2_Mul_28_inst_IVP_LTNX16_issue` @`0xde12c0` | 11 (`vbool`) | `vec` 10 | — |
| `ANDB` (bool op) | `F0_F0_S1_Ld_16_inst_IVP_ANDB_issue` @`0x71d9a0` | 10 (`vbool`) | `vbool` 10, `AR` 3 | — |
| `MOV2NX8T` (pred-move) | (S3_ALU placement) | 10 (`vec`) | `vbool` 10, `vec` 10 | — |
| `MOVVPR` (`vec`→`b32_pr`) | (S3_ALU placement) | 10 (`b32_pr`) | `vec` 12 | — |

Interpretation:

* A **compare** posts its `vbool` **result def** at **LAT 10** (fp) / **11** (int)
  — the same vector-ALU result stage as any `vec` op; a consumer (a `_t` op, a
  boolean op, a reduce) needs the predicate at LAT 10. `[HIGH/OBSERVED — both def
  sites read this pass.]`
* The **boolean ops** post at LAT 10 in the `S1_Ld` boolean unit. `[HIGH/OBSERVED]`

> **CORRECTION — "int=11 / fp=10" is a *def-post* rule, not a per-operand one.**
> The LAT differs on the *result definition*: `LTNX16` writes its `vbool` def at
> `mov $0xb` (11) right after the `opnd_sem_vbool_addr` call (@`0xde12e9`); the fp
> `OLEN` writes a `vbool` at `mov $0xa` (10) (@`0xde1457`). But the *predicated*
> `OLEN_2XF32T` form **also reads** a second `vbool` operand at `mov $0xb` (11)
> (@`0xde1482`) — that is the `_t` mask input, not the result. So an issue fn
> legitimately carries **both** LAT 10 and LAT 11 `vbool` slots; the clean rule is
> per the *result def* (int 11 / fp 10), not "every `vbool` slot in an fp compare is
> 10". `[HIGH/OBSERVED]`

* The `_t` predicate-**consume** reads the `vbool` operand at LAT 10 (it must be
  forwardable by the consumer's read stage); the per-file interlock-scan window for
  `vbool` is **12–13 stages**, so a predicate produced up to ~3 stages before the
  consume forwards without a stall. `[HIGH numbers / MED window-rule.]`
* The `$0xe`(14) values fed to `*0x15648/0x15650(%rbx)` are the WB-port / bypass-
  horizon **structural** stall args — **not** value latencies. `[HIGH/OBSERVED]`
* As always, the cas scoreboard never inspects the predicate **value** — it
  schedules reg#/LAT slots; the value is computed by the fiss `xdref` bodies (§3–§7).
  Ordered/unordered/signed/unsigned variants share the **same** cas timing; they
  differ only in the host/fiss callback. `[HIGH/OBSERVED]`

---

## 9. Generate → consume reconciled — the Dropout chain end to end

The CastPredicated / Dropout firmware kernel
([CastPredicated — opcode `0x99`](../firmware/kernels/castpredicated.md)) decodes to
`compare → mask → select → multiply`. Every leg's fiss VALUE body and cas predicate
plumbing close here:

| STEP | DVE mnemonic | fiss VALUE body | role |
|---|---|---|---|
| uint→float | `ivp_ufloatn_2x32t` | `xdref ufloat` | convert |
| COMPARE `r<p` → `vbool` | `ivp_oltn_2xf32t` | `opcode__ivp_oltn_2xf32t__stage_5 @0x58ffa0` → `module__xdref_olt_1_4_32f_32f_4f_t @0x528fa0` (×4) | §3: ordered fp32 `<` → `vbool`, NaN ⇒ FALSE |
| MASK apply → keep/drop | `ivp_dselnx16t` / `ivp_seln_2x32t` | `opcode__ivp_selnx16t__stage_5 @0x4cc620` → `module__xdref_bitkillt_16_2` per lane | §6: `bitkillt` MERGE (keep dst/old where pred false) |
| SCALE survivors `×1/(1-p)` | `ivp_muln_2xf32t` | `opcode__ivp_muln_2xf32t__stage_5 @0x599220` | §6: predicated fp32 multiply |

Reconciliation:

* The Dropout `random < p` test **is** an ordered fp32 less-than producing a
  `vbool` (§3): the ordered NaN-handling means a NaN random value yields predicate
  FALSE = "drop". The compare writes the 16×64-bit `vbool`. `[HIGH/OBSERVED — body
  located, calls `olt_1_4_32f_32f_4f_t`.]`
* The mask-apply select **consumes** that `vbool` via `bitkillt_16_2` (§6) — a
  MERGE: survivor lanes (pred true) take the activation, killed lanes take the
  merge-source (zero in the `dsel`-vs-zero blend). `[HIGH/OBSERVED — `selnx16t`
  calls `bitkillt` per lane.]`
* The scale multiply is the predicated fp32 multiply by the host-supplied
  `1/(1-p)`. `[HIGH/OBSERVED — body present @0x599220.]`

So the Dropout op is **composed entirely** from the standard `vbool` predicate
primitives decoded here: `compare(generate) → bitkillt(consume/merge) →
predicated-multiply`. There is no bespoke dropout hardware op; the generate→consume
chain is closed. `[HIGH/OBSERVED]`

---

## 10. Reimplementation checklist

A Vision-Q7-compatible `vbool` engine must reproduce, exactly:

1. **Two 16×64-bit predicate files** — `vbool` (general, `and $0xf` operand) and
   `b32_pr` (matmul `pr`, `and $0xf`) — plus the 16×1-bit scalar `BR` file. Never
   alias them. The regfile-table qword is `(count << 32) | widthBits` (§2.2).
2. **Ordered/unordered NaN polarity** — ordered `o*` `and`-in the not-NaN flag (NaN
   ⇒ FALSE); unordered `u*` `or`-in the NaN flag (NaN ⇒ TRUE); `unn` = pure
   isUnordered. Full integer-only binary16/binary32 NaN classification.
3. **Signed sign-bias, unsigned bare-cmp** — signed `lt`/`le` flip the sign bit then
   unsigned-compare; `ltu`/`leu`/`eq` are bare `cmp`. No signed `GT`/`GE` mnemonic.
4. **Boolean algebra as flat 64-bit bitwise** — `notb`/`andb`/`orb`/`xorb` and
   `andnotb` (`A & ~B`, second operand inverted); issue in the **Load** slot at
   LAT 10.
5. **`_t` = MERGE** — `bitkillt` emits all-ones in the KILLED lane (keep
   destination); the live lane takes the fresh result. `bitkillf` is the inverse.
   This is a blend, not a zero-fill.
6. **`randbn` = reduce-AND** (`cmp $0xffffffff;sete`), **`rorbn` = reduce-OR**
   (`setne`); the `b2n` narrow all-set/any-set; the `predflag*` lane-packers; the
   `bitkill_mov*` scalar-condition generators. Not RNG.
7. **Timing** — compare→`vbool` **result-def** LAT 10 (fp) / 11 (int); boolean and
   `_t`-consume LAT 10; `MOVVPR` LAT 10; `vbool` forward window 12–13 stages. An
   issue fn may legitimately carry both LAT-10 and LAT-11 `vbool` slots (result vs
   `_t`-mask operand).

The independent validation harness `VAL-07` exercises this whole layer
(generate→boolean→consume→reduce) against the fiss leaves driven live; its results
corroborate the merge polarity and the reduce direction documented above.

> **CORRECTION — the single strongest one.** The `_t` masked forms are a
> **destination-MERGE**, not a zero-fill, and `bitkillt` proves it byte-exact:
> `and $0x1 ; xor $0x1 ; neg ; and $0xffff` yields **`0xFFFF` in the
> predicate-FALSE (killed) lane** — an all-ones "keep the prior destination" mask —
> and `0x0000` in the live lane. A reimplementation that zero-fills killed lanes
> (the naïve "masked op" assumption) is wrong for *every* `_t` arithmetic, select,
> predicated-move and masked-store on this core. `[HIGH/OBSERVED]`

---

## 11. Cross-references

* [libcas-core Surface + ISS Plugin ABI](./cas-core-surface.md) — the cas/fiss
  split, the `dll_*` accessor vocabulary, the 119 `nx_*_interface` callbacks.
* [fiss Datapath — the value oracle](./fiss-datapath-oracle.md) — the 864
  `module__xdref_*` value leaves these predicate primitives belong to.
* [ISA Batch 11 — vbool ALU / predicate](../isa/ref/b11-vbool-alu.md) — the
  per-instruction encoding reference for the boolean-algebra and reduce/scan forms
  (the `vbool` operand fields, the Load-slot placement, the B08/B11 partition).
* [CastPredicated — opcode `0x99`](../firmware/kernels/castpredicated.md) — the
  predicated firmware kernel whose generate→consume chain §9 reconciles.
* [Group semantics II — the `vbool` algebra](../isa/semantics/group-semantics-ii.md)
  — the formal predicate/boolean semantics, live-driven.
