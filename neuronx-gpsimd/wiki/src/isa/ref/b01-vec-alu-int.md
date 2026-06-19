# ISA Batch 01 — Vector ALU (int / compare / logic core)

This is the first per-instruction batch of the Vision-Q7 *Cairo* (`ncore2gp`) ISA reference, and
it covers the **integer Vector-ALU core on the `vec` register file**: the everyday element-wise
add / sub / abs / neg, signed-and-unsigned min / max, the integer compares that *produce* a
`vbool` predicate (eq / lt / le / neq, signed and unsigned), and the bitwise logic
(and / or / xor / not). These are the 50 opcodes a kernel emits in its hottest inner loops; they
own **894 of the 12 569 shipped placements** ([the coverage tally's certified-perfect
denominator](../core/coverage-tally.md)).

Everything below is re-grounded against the shipped binaries this pass: the **encoding** from
`libisa-core.so` (the `Opcode_<mnem>_Slot_<slot>_encode` thunks and the `Field_*_get` operand
accessors), the **value semantics** by *executing* the matching `module__xdref_*` leaves in
`libfiss-base.so` live in-process (license-free), and an independent **encode/decode oracle** from
the device-native `xtensa-elf-as`/`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). Confidence tags
per [the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-execution, `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` =
re-used at a sibling page's confidence.

> **Scope split — read this before pairing a mnemonic to a batch.** This batch is the **integer,
> non-predicated, base** core on `vec`. Three boundaries are enforced so the 30 batches don't
> double-count:
> * **Float (`…nxf16` / `…n_2xf32`) variants → [B02](b02-vec-alu-fp.md).** Every op here has a
>   float sibling (`ivp_addnx16` ↔ `ivp_addnxf16`); the float ones are B02's, not ours.
> * **Predicated `…t` (throttle/merge) and the `b…` B-variants → [B03](b03-vec-alu-rest.md).**
>   14 of our 50 base opcodes carry a `…t` predicated form (e.g. `ivp_addnx16t`), and the
>   `ivp_bmin*`/`ivp_baddnorm*` "B-variant" family is wholly B03's. We document only the base.
> * **Predicate-file (`vbool`/`b32_pr`) boolean logic (`ivp_andb`/`ivp_andnotb`/`ivp_orb`/…) →
>   [B11](b11-vbool-alu.md).** Our logic ops (`ivp_and2nx8`/…) operate on `vec` storage bitwise;
>   the `…b` ops operate on the predicate files and belong to B11.
> * **Composites (`ivp_abssub*` abs-of-difference, `ivp_avg*` averaging, `ivp_minormax*`) → not
>   here.** They share this batch's datapath and are cited as adjacency where instructive, but are
>   owned by [B03](b03-vec-alu-rest.md) / [B24](b24-composite.md).

---

## 1. Batch key facts

| Fact | Value | Binary source |
|---|---|---|
| Datapath register file | `vec` (idx 2) — 32 × 512-bit, 5-bit index, ctype `…vec2Nx8` | `regfiles[]` @ `0x74a800` ([register-files §3](../core/register-files.md)) |
| Predicate sink (compares) | `vbool` (idx 3) — 16 × 64-bit per-lane masks | `regfiles[]` idx 3 |
| Base mnemonics this batch | **50** | §2; `nm libisa-core.so` distinct `Opcode_ivp_*` |
| Placements this batch | **894** | `nm \| rg -c` on the explicit 50-op glob (§6) |
| Functional unit | the `alu` slot class (+ `mul` for compares, +`ldst` for 1-source) | `slots[]` ([flix-encoding §5](../core/flix-encoding.md)) |
| ALU result latency | source `use_stage = 10` → dest `def_stage = 11` (1-cycle ALU) | `opcodes[]` use/def stages `[HIGH/OBSERVED]` |
| Lane geometry | `2nx8`=64×8b · `nx16`=32×16b · `n_2x32`=16×32b (one 512-bit reg) | `xt_ivp32.h` typedefs; value-leaf width suffix |
| Encode-thunk ABI | `C7 07 imm32 [C7 47 04 0] C3` — `imm32` = the `(opcode×slot)` selector | [flix-encoding §6.1](../core/flix-encoding.md) |
| Value-leaf ABI (binary op) | `void leaf(uint64 ctx, uint32 a, uint32 b, uint32 *out)` | disassembled §4 |
| Oracle | `xtensa-elf-as`/`objdump`, `XTENSA_CORE=ncore2gp` | round-trips all 50 byte-exact (§5) |

The whole batch issues on the **ALU slot class** of the FLIX grid. An integer ALU op is legal in
the last-ALU slot of every wide format (`F0_S3_ALU`, `F1_S3_ALU`, `F2_S3_ALU`, `F3_S3_ALU`,
`F3_S4_ALU`, `F4_S3_ALU`, `F6_S3_ALU`, `F7_S3_ALU`, `F11_S3_ALU`, `F11_S4_ALU`) and the narrow
`N0_S3_ALU`; the dual-issue wide formats additionally fold an ALU op into the Mul-class slot
(`F1_S2_Mul`, `F2_S2_Mul`, `F7_S2_Mul`, …) and `F1`'s fused `S0_LdStALU`. That reach — **the same
mnemonic legal in 15–21 distinct slots** — is what lets the scheduler co-issue two ALU ops per
bundle (the `1+1` co-issue ceiling, [coverage-tally §7](../core/coverage-tally.md)).

> **The whole batch is one multiplexed reference datapath.** All 50 opcodes (plus their float and
> predicated siblings) decode into a *single* semantic ALU slice (`ivp_sem_vec_alu`, ~244 member
> opcodes) selected by decoded **OR-group** signals — `op_ADD`, `op_SUB` (`b XOR (~) +cin`),
> `op_MAX`/`op_MIN`, `op_GRP_SAT` (the `s`-clamp), `op_GRP_UNSIGN` (zero- vs sign-extend),
> `op_GRP_{8,16,32}BIT` (the lane-width carry-break). The arithmetic core is a two's-complement
> carry-chain adder with **per-lane carry-break masks** (`propagate_mask{p0..p3}`) that wrap mod
> `2^w` at each 8/16/32-bit lane edge. A reimplementation builds one parameterized lane ALU and
> drives it from the decoded opcode group, exactly as §4's leaves do. `[HIGH/OBSERVED]`

---

## 2. Batch roster — 50 base integer-ALU opcodes

Columns: `mnemonic` · `lanes×width` · representative `FLIX slot` and its **opcode-selector imm**
(the `Opcode_<mnem>_Slot_<slot>_encode` thunk's `movl $imm`, disassembled) · the **vec 5-bit
operand field positions** in the slot-word · device byte-size · one-line lane semantics · `[conf]`.
Every selector imm below is for the **`F0_S3_ALU` slot specifically** and was disassembled this
pass — see the GOTCHA after the tables: *the selector is per-(opcode×slot); there is no global
add-bit.* The vec operand fields (slot-word bits `[14:10]`/`[19:15]`/`[24:20]`) are **identical for
every op in this batch** — they are a property of the *slot*, not the opcode (§3.2).

### 2.1 Add / sub (wrapping and saturating)

| mnemonic | lanes×w | F0_S3_ALU sel | opc#/iclass# | bytes | semantics | conf |
|---|---|---|---|---|---|---|
| `ivp_add2nx8`   | 64×8  | `0x80ad0000` | 982 / 895 | 16/8 | `a = wrap8(b + c)` | `[HIGH/OBSERVED]` |
| `ivp_addnx16`   | 32×16 | `0x80b50000` | 422 / 335 | 16/8 | `a = wrap16(b + c)` | `[HIGH/OBSERVED]` |
| `ivp_addn_2x32` | 16×32 | `0x80bd0000` | 1025 / 938 | 16/8 | `a = wrap32(b + c)` | `[HIGH/OBSERVED]` |
| `ivp_addsnx16`  | 32×16 | `0x80c50000` | 450 / 363 | 16/8 | `a = satS16(b + c)` (signed clamp) | `[HIGH/OBSERVED]` |
| `ivp_sub2nx8`   | 64×8  | `0x86b10000` | 983 / 896 | 16/8 | `a = wrap8(b − c)` | `[HIGH/OBSERVED]` |
| `ivp_subnx16`   | 32×16 | `0x86b90000` | 423 / 336 | 16/8 | `a = wrap16(b − c)` | `[HIGH/OBSERVED]` |
| `ivp_subn_2x32` | 16×32 | `0x86a18000` | 1026 / 939 | 16/8 | `a = wrap32(b − c)` | `[HIGH/OBSERVED]` |
| `ivp_subsnx16`  | 32×16 | `0x86a98000` | 451 / 364 | 16/8 | `a = satS16(b − c)` | `[HIGH/OBSERVED]` |

### 2.2 Abs / neg (wrapping and saturating)

These take **one** `vec` source and so are also legal in the LdSt slot class (§6); the table shows
the `F0_S3_ALU` selector.

| mnemonic | lanes×w | F0_S3_ALU sel | opc#/iclass# | semantics | conf |
|---|---|---|---|---|---|
| `ivp_abs2nx8`   | 64×8  | `0x64c05c00` | 1017 / 930 | `a = wrap8(\|b\|)`  (wraps at `INT8_MIN`) | `[HIGH/OBSERVED]` |
| `ivp_absnx16`   | 32×16 | `0x64c85c00` | 1367 / 1280 | `a = wrap16(\|b\|)` (wraps at `0x8000`) | `[HIGH/OBSERVED]` |
| `ivp_absn_2x32` | 16×32 | `0x64d85c00` | 1018 / 931 | `a = wrap32(\|b\|)` | `[HIGH/OBSERVED]` |
| `ivp_abssnx16`  | 32×16 | `0x64e85c00` | 1368 / 1281 | `a = satS16(\|b\|)` (`abs(0x8000)=0x7fff`) | `[HIGH/OBSERVED]` |
| `ivp_neg2nx8`   | 64×8  | `0x6488d400` | 984 / 897 | `a = wrap8(−b)` | `[HIGH/OBSERVED]` |
| `ivp_negnx16`   | 32×16 | `0x6488d800` | 424 / 337 | `a = wrap16(−b)` (`neg(0x8000)=0x8000`) | `[HIGH/OBSERVED]` |
| `ivp_negn_2x32` | 16×32 | `0x6490d000` | 1027 / 940 | `a = wrap32(−b)` | `[HIGH/OBSERVED]` |
| `ivp_negsnx16`  | 32×16 | `0x6490d800` | 452 / 365 | `a = satS16(−b)` | `[HIGH/OBSERVED]` |

### 2.3 Min / max (signed and unsigned)

| mnemonic | lanes×w | F0_S3_ALU sel | opc#/iclass# | semantics | conf |
|---|---|---|---|---|---|
| `ivp_min2nx8`     | 64×8  | `0x80e58000` | 985 / 898 | `a = (sB ≤ sC) ? b : c` (signed) | `[HIGH/OBSERVED]` |
| `ivp_minnx16`     | 32×16 | `0x80fd8000` | 425 / 338 | signed min, 16-bit | `[HIGH/OBSERVED]` |
| `ivp_minn_2x32`   | 16×32 | `0x86a80000` | 1028 / 941 | signed min, 32-bit | `[HIGH/OBSERVED]` |
| `ivp_minu2nx8`    | 64×8  | `0x86b80000` | 986 / 899 | unsigned min, 8-bit | `[HIGH/OBSERVED]` |
| `ivp_minunx16`    | 32×16 | `0x86a08000` | 426 / 339 | unsigned min, 16-bit | `[HIGH/OBSERVED]` |
| `ivp_minun_2x32`  | 16×32 | `0x86a88000` | 1029 / 942 | unsigned min, 32-bit | `[HIGH/OBSERVED]` |
| `ivp_max2nx8`     | 64×8  | `0x80958000` | 987 / 900 | signed max, 8-bit | `[HIGH/OBSERVED]` |
| `ivp_maxnx16`     | 32×16 | `0x80ad8000` | 427 / 340 | signed max, 16-bit | `[HIGH/OBSERVED]` |
| `ivp_maxn_2x32`   | 16×32 | `0x80bd8000` | 1030 / 943 | signed max, 32-bit | `[HIGH/OBSERVED]` |
| `ivp_maxu2nx8`    | 64×8  | `0x80cd8000` | 988 / 901 | unsigned max, 8-bit | `[HIGH/OBSERVED]` |
| `ivp_maxunx16`    | 32×16 | `0x80d58000` | 428 / 341 | unsigned max, 16-bit | `[HIGH/OBSERVED]` |
| `ivp_maxun_2x32`  | 16×32 | `0x80dd8000` | 1031 / 944 | unsigned max, 32-bit | `[HIGH/OBSERVED]` |

### 2.4 Integer compare → `vbool` predicate (signed and unsigned)

Compares write a **`vbool`** register (not `vec`): each lane sets an all-ones predicate field on
true, zero on false. They issue on the **Mul/compare slot class** as well as the ALU class, hence
21 placements each (§6). The selector below is `F0_S3_ALU`; all share base `0x80870000`, with the
**relation and dtype encoded in the low bits — within this one slot only**.

| mnemonic | lanes×w | F0_S3_ALU sel | opc#/iclass# | predicate write | conf |
|---|---|---|---|---|---|
| `ivp_eq2nx8`    | 64×8  | `0x80870000` | 991 / 904 | `vb_lane = (b == c)` (sign-irrelevant) | `[HIGH/OBSERVED]` |
| `ivp_eqnx16`    | 32×16 | `0x80870002` | 434 / 347 | `vb_lane = (b == c)` | `[HIGH/OBSERVED]` |
| `ivp_eqn_2x32`  | 16×32 | `0x80870004` | 1037 / 950 | `vb_lane = (b == c)` | `[HIGH/OBSERVED]` |
| `ivp_neq2nx8`   | 64×8  | `0x80870200` | 992 / 905 | `vb_lane = (b != c)` | `[HIGH/OBSERVED]` |
| `ivp_neqnx16`   | 32×16 | `0x80870200`◇ | 435 / 348 | `vb_lane = (b != c)` | `[HIGH/OBSERVED]` |
| `ivp_neqn_2x32` | 16×32 | `0x80870204`† | 1038 / 951 | `vb_lane = (b != c)` | `[HIGH/OBSERVED]` opc; `[MED]` imm |
| `ivp_lt2nx8`    | 64×8  | `0x80870102` | 989 / 902 | `vb_lane = (sB < sC)` (signed) | `[HIGH/OBSERVED]` |
| `ivp_ltnx16`    | 32×16 | `0x80870104` | 432 / 345 | signed `<` | `[HIGH/OBSERVED]` |
| `ivp_ltn_2x32`  | 16×32 | `0x80870106`† | 1035 / 948 | signed `<` | `[HIGH/OBSERVED]` opc; `[MED]` imm |
| `ivp_ltu2nx8`   | 64×8  | `0x80870108` | 993 / 906 | `vb_lane = (uB < uC)` (unsigned) | `[HIGH/OBSERVED]` |
| `ivp_ltunx16`   | 32×16 | `0x8087010a` | 436 / 349 | unsigned `<` | `[HIGH/OBSERVED]` |
| `ivp_ltun_2x32` | 16×32 | `0x8087010c`† | 1039 / 952 | unsigned `<` | `[HIGH/OBSERVED]` opc; `[MED]` imm |
| `ivp_le2nx8`    | 64×8  | `0x80870006`† | 990 / 903 | `vb_lane = (sB ≤ sC)` | `[HIGH/OBSERVED]` opc; `[MED]` imm |
| `ivp_lenx16`    | 32×16 | `0x80870008` | 433 / 346 | signed `≤` | `[HIGH/OBSERVED]` |
| `ivp_len_2x32`  | 16×32 | `0x8087000a`† | 1036 / 949 | signed `≤` | `[HIGH/OBSERVED]` opc; `[MED]` imm |
| `ivp_leu2nx8`   | 64×8  | `0x8087000c`† | 994 / 907 | `vb_lane = (uB ≤ uC)` | `[HIGH/OBSERVED]` opc; `[MED]` imm |
| `ivp_leunx16`   | 32×16 | `0x8087000e` | 437 / 350 | unsigned `≤` | `[HIGH/OBSERVED]` |
| `ivp_leun_2x32` | 16×32 | `0x80870…e`† | 1040 / 953 | unsigned `≤` | `[HIGH/OBSERVED]` opc; `[MED]` imm |

### 2.5 Bitwise logic (element-agnostic, full 512-bit)

These are **width-agnostic full-register bitwise** ops; the `2nx8` suffix is conventional spelling
only (an `AND` over a 512-bit register is the same regardless of lane interpretation, §4.4). Note
the **contiguous opcode block** (xor 418, and 419, or 420, not 421).

| mnemonic | lanes×w | F0_S3_ALU sel | opc#/iclass# | semantics | conf |
|---|---|---|---|---|---|
| `ivp_and2nx8` | 512-bit | `0x80cd0000` | 419 / 332 | `a = b & c` | `[HIGH/OBSERVED]` |
| `ivp_or2nx8`  | 512-bit | `0x86a90000` | 420 / 333 | `a = b \| c` | `[HIGH/OBSERVED]` |
| `ivp_xor2nx8` | 512-bit | `0x86b18000` | 418 / 331 | `a = b ^ c` | `[HIGH/OBSERVED]` |
| `ivp_not2nx8` | 512-bit | `0x6490dc00` | 421 / 334 | `a = ~b` (1 source) | `[HIGH/OBSERVED]` |

`†` = the low-bit step is **extrapolated** from the disassembled `eq/lt/le` nibble structure
(§3.3); the *base* `0x80870000` and the `opc#/iclass#` are `[HIGH/OBSERVED]`, the `†` imm cells
`[MED/INFERRED]`. `◇` = `neqnx16` and `neq2nx8` both show base `0x80870200` for the `neq` relation
across the disassembled cells (the dtype nibble distinguishes width elsewhere in the field);
`[HIGH/OBSERVED]` on `neqnx16`, `neq2nx8`.

> **GOTCHA — there is no global "add bit" or "subtract bit"; the selector is per-(opcode×slot).**
> `ivp_addnx16` encodes to `0x80b50000` in `F0_S3_ALU`, but `0x00090000` in `F3_S3_ALU`,
> `0x12938000` in `F1_S0_LdStALU`, `0x6c480000` in `N0_S3_ALU`, and `0x02a50000` in `F7_S2_Mul`
> (all disassembled this pass). The clean nibble pattern in §2.4 (compares stepping by `0x100` /
> `0x008` / a dtype low-nibble) holds **only within the `F0_S3_ALU` slot**. This is exactly the
> [flix-encoding §6.2 "two-tier selector"](../core/flix-encoding.md) fact: the selector bits are
> format-local opcode-packing, not a roster-wide bit. Independently, the `s`/`u`/width/`t`
> distinctions are **distinct opcodes with their own `iclass`** (an `s` op is `op_GRP_SAT`-tagged,
> a `u` op `op_GRP_UNSIGN`-tagged), never a runtime toggle of one base op — note each row above
> has a *different* `opc#`/`iclass#`. A reimplementation's encoder indexes the `(opcode, slot)`
> pair into `opcodedefs[]`; it never computes a selector by OR-ing a global "operation" field.
> `[HIGH/OBSERVED]`

---

## 3. Encoding — lane geometry, the dtype suffix, and the selector structure

### 3.1 The lane-geometry suffix is the whole datapath story

One 512-bit `vec` register is re-partitioned by the mnemonic suffix into a SIMD vector. The suffix
*is* the lane count × element width — there is no separate dtype operand:

| suffix | element | lanes (`N`=16) | C intrinsic type | example |
|---|---|---|---|---|
| `2nx8`   | 8-bit  | 64 | `xb_vec2Nx8` / `…U` | `ivp_add2nx8` |
| `nx16`   | 16-bit | 32 | `xb_vecNx16` / `…U` | `ivp_addnx16` |
| `n_2x32` | 32-bit | 16 | `xb_vecN_2x32v` / `…U` | `ivp_addn_2x32` |

(`N` is the symbolic native half-width: `2N`=64 8-bit lanes, `N`=32 16-bit lanes, `N/2`=16 32-bit
lanes — all summing to the same 512 bits.) The C types are read from the device intrinsic header
`xt_ivp32.h` (`xb_vecNx16` signed, `xb_vecNx16U` unsigned, `vboolN` the compare result); the
public `IVP_ADDNX16` macro expands to the `_TIE_xt_ivp32_IVP_ADDNX16` builtin. `[HIGH/OBSERVED]`

The signed/unsigned and saturating distinctions ride **separate opcodes**, not a flag:

* **`u` infix → unsigned** comparison/order (`ivp_minunx16`, `ivp_maxunx16`, `ivp_ltunx16`,
  `ivp_leunx16`). Decoded `op_GRP_UNSIGN` selects **zero-extend** instead of sign-extend at the
  lane guard bits. For add/sub the wrap result is bit-identical signed-vs-unsigned, so there is no
  `addu` opcode — the C header exposes an `…U`-typed *intrinsic* over the *same* opcode.
* **`s` infix → signed-saturating** (`ivp_addsnx16`, `ivp_subsnx16`, `ivp_abssnx16`,
  `ivp_negsnx16`); decoded `op_GRP_SAT` applies the output clamp. The un-`s` form **wraps**
  (two's-complement truncation by the lane carry-break mask).

### 3.2 The slot-word operand fields (5-bit `vec` indices)

Every integer ALU op in a given slot reads its three `vec` operands from the *same* slot-word bit
positions — a property of the slot, decoded by the `Field_fld_<slot>_<hi>_<lo>_Slot_<slot>_get`
accessors. For `F0_S3_ALU`, disassembled this pass:

```c
// Field_fld_f0_s3_alu_14_10_get @ 0x314000 :  mov (%rdi),%eax; shl $0x11; shr $0x1b
//   == (slotword << 17) >> 27  == zero-extended bits [14:10]   -> 5-bit vec index
// Field_fld_f0_s3_alu_19_15_get @ 0x3142d0 :  (slotword << 12) >> 27  == bits [19:15]  (5-bit)
// Field_fld_f0_s3_alu_24_20_get @ 0x3140d0 :  (slotword <<  7) >> 27  == bits [24:20]  (5-bit)
// the matching _set thunks do  `and $0x1f; shl $0xN`  — 5-bit deposit, confirming the inverse.
```

The role→field mapping (`ivp_sem_vec_alu_{vt,vr,vs}`) sits above these raw fields and **can
scatter** (the `vt` semantic field reads two disjoint ranges — `Field_fld_ivp_sem_vec_alu_vt`
disassembles to `shl $0x1c; shr $0x5; … and $0x18`, two ranges OR-ed). A reimplementation deposits
the 5-bit index into the raw `[14:10]`/`[19:15]`/`[24:20]` windows and lets the `sem_*` layer name
the role. The compare ops additionally carry an `ivp_sem_vec_alu_vbr` 4-bit field (the **`vbool`
destination** index, `Field_fld_…_vbr`, bits `[18:15]`-class). `[HIGH/OBSERVED]`

### 3.3 The compare selector nibble structure (F0_S3_ALU only)

Disassembled `F0_S3_ALU` compare selectors share base `0x80870000` and encode three independent
sub-fields in the low 12 bits:

```
selector = 0x80870000
         | dtype_nibble    // bits[2:0]:  2nx8=0, nx16=2, n_2x32=4   (lane width)
         | relation        // eq=0x000, le=0x008, lt=0x100, neq=0x200
         | unsigned_bit     // signed lt 0x100 -> unsigned ltu 0x108 (+0x008 on lt);
                            // signed le 0x008 -> unsigned leu 0x00e (+0x006 on le)
```

Worked, all `[HIGH/OBSERVED]` (disassembled): `eqnx16=0x80870002`, `lenx16=0x80870008`,
`leunx16=0x8087000e`, `ltnx16=0x80870104`, `ltunx16=0x8087010a`, `neqnx16=0x80870200`,
`eq2nx8=0x80870000`, `eqn_2x32=0x80870004`. This nibble model is the §2.4 `†` source and is sound
**within F0_S3_ALU**; do not export it across slots (§2 GOTCHA). `[HIGH/OBSERVED]` on the
disassembled cells; the model that interpolates the rest is `[MED/INFERRED]`.

---

## 4. Lane value semantics — proven by execution

The `module__xdref_*` value leaves in `libfiss-base.so` are the per-element value functions and are
**callable in-process via ctypes with no license** ([coverage-tally §5](../core/coverage-tally.md)).
Each is the lane kernel: it computes **one lane** and writes the result to `*out`. Disassembly fixed
the ABI; the runs below were executed live this pass.

**Binary-op ABI** (`add`, `sub`, `min`, `max`, compare): `void leaf(uint64 ctx /*rdi, unused for
these*/, uint32 a /*esi*/, uint32 b /*edx*/, uint32 *out /*rcx*/)`. **Unary** (`abs`, `neg`):
drops the `b` argument. **Full-register logic** (`and`/`or`/`xor`/`not`): pointers in/out.

### 4.1 Add / sub — wrap vs saturate (live)

`add_16_16_16` is literally `add %esi,%edx; and $0xffff,%edx; mov %edx,(%rcx)` — a two's-complement
truncating add. `adds_16_16_16` sign-extends, adds in 17 bits, detects the overflow sign-bit
mismatch, and clamps. Executed:

```
adds_16_16_16 (saturating)            add_16_16_16 (wrapping)
  a=0x7000 b=0x2000 -> 0x7fff           -> 0x9000   (wrap goes negative; sat clamps to +max)
  a=0x4000 b=0x4000 -> 0x7fff           -> 0x8000
  a=0x8000 b=0x8000 -> 0x8000           -> 0x0000   (both -32768; sat clamps to -min)
  a=0x7fff b=0x0001 -> 0x7fff           -> 0x8000
  a=0x0001 b=0x0001 -> 0x0002           -> 0x0002   (no overflow: identical)
```

The clamp is **asymmetric**: positive overflow → `0x7fff` (`INT16_MAX`), negative overflow →
`0x8000` (`INT16_MIN`). A reimplementation that clamps both ways to `0x7fff` is wrong on the
`0x8000+0x8000` case. `[HIGH/OBSERVED by execution]`

```c
// ivp_addsnx16 lane (signed-saturating 16-bit add), exact per the executed leaf:
int16_t adds16(int16_t b, int16_t c) {
    int32_t s = (int32_t)b + (int32_t)c;          // 17-bit headroom
    if (s >  32767) return  32767;                 // 0x7fff
    if (s < -32768) return -32768;                 // 0x8000
    return (int16_t)s;
}
// ivp_addnx16 lane (wrapping): return (int16_t)((uint16_t)b + (uint16_t)c);  // bit-truncate
// ivp_subnx16 lane: realized as  b + (~c) + 1  (op_GRP_A1_SUB), then lane-truncated.
```

### 4.2 Min / max — the signed-via-unsigned sign-flip (live)

`minu_16_16_16` is a plain `cmp %edx,%esi; cmova %edx,%esi` (unsigned select). `min_16_16_16` does
**not** sign-extend; instead it *toggles bit 15* of each operand (`shr $0xf; sete; shl $0xf; or`),
turning a signed compare into an unsigned one, then `cmovae` — the classic bias trick. Executed,
the polarity flips exactly with signedness:

```
a=0x8000(-32768/+32768u) b=0x0001 :  min=0x8000  minu=0x0001   max=0x0001  maxu=0x8000
a=0xffff(-1    /+65535u) b=0x0001 :  min=0xffff  minu=0x0001   max=0x0001  maxu=0xffff
a=0x7fff(+32767/+32767u) b=0x8000 :  min=0x8000  minu=0x7fff   max=0x7fff  maxu=0x8000
```

```c
// ivp_minnx16 / ivp_minunx16 lane:
int16_t  min_s16 (int16_t  b, int16_t  c){ return (b <= c) ? b : c; }    // signed
uint16_t minu_u16(uint16_t b, uint16_t c){ return (b <= c) ? b : c; }    // unsigned
// (the binary realizes min_s16 by XOR-ing bit15 of each input and doing an unsigned compare —
//  bit-identical result, no sign-extend; reproduce the *semantics*, the trick is an impl detail.)
```

`[HIGH/OBSERVED by execution]`

### 4.3 Integer compare → 2-bit-per-lane `vbool` predicate (live)

The compare leaves are named `<rel>_<predw>_<inw>_<inw>` where `predw` is the predicate width in
*bits* per lane: `eq_2_16_16` = a **2-bit** predicate from two 16-bit inputs. The leaf produces an
all-ones predicate field on true (`& 0x3` = `0b11`), zero on false. `eq_2_16_16` is `xor %eax;
cmp; sete %al; neg %eax; and $3` — i.e. `(b==c) ? 0x3 : 0x0`. The signed `lt_2_16_16` uses the same
bit-15 sign-flip as min, then `sbb; and $3`; `ltu_2_16_16` skips the flip. Executed:

```
                                eq    lt(signed)  ltu(unsigned)  le(signed)
a=0x0005 b=0x0005           :   0x3      0x0           0x0          0x3
a=0x8000 b=0x0001 (-32768<1):   0x0      0x3           0x0          0x3      // signed lt TRUE, unsigned FALSE
a=0xffff b=0x0001 (-1    <1):   0x0      0x3           0x0          0x3
a=0x0001 b=0xffff (1 <u 65535): 0x0      0x0           0x3          0x0      // unsigned ltu TRUE
```

The `0x8000 lt 0x0001` row is the decisive edge: **signed** `lt` is true (−32768 < 1) but
**unsigned** `ltu` is false (32768 < 1 is false) — the two opcodes diverge exactly here.

```c
// ivp_eqnx16 / ivp_ltnx16 / ivp_ltunx16 lane → 2-bit vbool field per 16-bit lane:
//   per-lane predicate field (all-ones on true) written into the vbool destination register.
uint2_t eq16 (int16_t b, int16_t c){ return (b == c)                      ? 0b11 : 0b00; }
uint2_t lt16 (int16_t b, int16_t c){ return ((int16_t)b  < (int16_t)c)    ? 0b11 : 0b00; }  // signed
uint2_t ltu16(uint16_t b, uint16_t c){ return ((uint16_t)b < (uint16_t)c) ? 0b11 : 0b00; }  // unsigned
// vbool is 64-bit / register; a 16-bit lane (32 lanes) consumes 2 predicate bits per lane
// (2 * 32 = 64).  An 8-bit lane (2nx8, 64 lanes) consumes 1 bit/lane (eq_1_8_8).  A 32-bit
// lane (n_2x32, 16 lanes) consumes 4 bits/lane (eq_4_32_32).  predw scales so the mask always
// fills the 64-bit vbool register: predw = 64 / num_lanes.   [HIGH/OBSERVED by execution + naming]
```

That **predicate-bits-per-lane = 64 / num_lanes** rule (`8b→1, 16b→2, 32b→4`) is read straight off
the leaf names (`eq_1_8_8` / `eq_2_16_16` / `eq_4_32_32`) and the `& 0x3` / `& 0x1` / `& 0xf` masks
in their bodies. The downstream consumption of this mask (predicated select / throttle) is
[B11](b11-vbool-alu.md)'s subject. Signed `lt`/`le` flip the MSB
(`{~a[15],a[14:0]} < {~b[15],b[14:0]}`) to map two's-complement order onto the native unsigned
operator; **there is no signed `gt`/`ge` opcode — GT/GE = LT/LE with the operands swapped.**
`[HIGH/OBSERVED]`

### 4.4 Bitwise logic — full 512-bit, lane-agnostic (live)

`and_512_512_512` is four `movdqu`/`pand` pairs over the whole 512-bit register (`rsi`/`rdx` in,
`rcx` out); `not_512_512` is `pcmpeqd`(all-ones)+`pxor`. No element boundary is consulted — the
`2nx8` spelling is cosmetic. Executed (first two bytes shown):

```
a = f00f   b = cc33
and = c003     or = fc3f     xor = 3c3c     not(a) = 0ff0
```

`F0 & CC = C0`, `0F & 33 = 03`, etc. — byte-exact. `ivp_not2nx8` is the only single-source op in
the logic group (and gains 4 LdSt placements like abs/neg, §6). `[HIGH/OBSERVED by execution]`

### 4.5 Abs / neg — wrapping vs saturating (live)

```
        abs(wrap)  abss(sat)  neg(wrap)
0x8000:   0x8000     0x7fff     0x8000     // |INT16_MIN| and -INT16_MIN both overflow; wrap=self
0xffff:   0x0001     0x0001     0x0001
0x0005:   0x0005     0x0005     0xfffb
0x7fff:   0x7fff     0x7fff     0x8001
```

`abs(0x8000)` wraps to `0x8000` (`|−32768|=32768` is unrepresentable → truncates to itself), while
`abss(0x8000)=0x7fff` saturates. Same story for `neg`. The wrapping forms are pure two's-complement
`-b` / `(b<0?-b:b)` with 16-bit truncation. `[HIGH/OBSERVED by execution]`

---

## 5. Device-assembler oracle — byte-exact round-trip

The strongest end-to-end check: feed the **device-native** `xtensa-elf-as`
(`XTENSA_SYSTEM=…/ncore2gp/config`, `XTENSA_CORE=ncore2gp`) the mnemonics and disassemble back. All
50 base ops (sampled 20 below) assemble `rc=0` and round-trip to the same lowercase mnemonic, each
as an **8-byte FLIX bundle** (the op lands in one slot, the rest `nop`). Verbatim bytes (LE):

| mnemonic | operands | 8-byte bundle | disasm bundle |
|---|---|---|---|
| `IVP_ADDNX16`   | `v3,v1,v2`  | `3251890020c1452f` | `{ nop; nop; nop; ivp_addnx16 v3,v1,v2 }` |
| `IVP_ADD2NX8`   | `v3,v1,v2`  | `3251490020c1452f` | `ivp_add2nx8 v3,v1,v2` |
| `IVP_ADDN_2X32` | `v3,v1,v2`  | `3251c90020c1452f` | `ivp_addn_2x32 v3,v1,v2` |
| `IVP_ADDSNX16`  | `v3,v1,v2`  | `32500a0020c1452f` | `ivp_addsnx16 v3,v1,v2` |
| `IVP_SUBNX16`   | `v3,v1,v2`  | `3252c71020c1452f` | `ivp_subnx16 v3,v1,v2` |
| `IVP_ABSNX16`   | `v3,v1`     | `3252869800d3452f` | `ivp_absnx16 v3,v1` |
| `IVP_NEGNX16`   | `v3,v1`     | `3252877800d6452f` | `ivp_negnx16 v3,v1` |
| `IVP_MINNX16`   | `v3,v1,v2`  | `3252477020c1452f` | `ivp_minnx16 v3,v1,v2` |
| `IVP_MINUNX16`  | `v3,v1,v2`  | `325247c020c1452f` | `ivp_minunx16 v3,v1,v2` |
| `IVP_MAXNX16`   | `v3,v1,v2`  | `32518b0020c1452f` | `ivp_maxnx16 v3,v1,v2` |
| `IVP_MAXUNX16`  | `v3,v1,v2`  | `3252472020c1452f` | `ivp_maxunx16 v3,v1,v2` |
| `IVP_AND2NX8`   | `v3,v1,v2`  | `32500b0020c1452f` | `ivp_and2nx8 v3,v1,v2` |
| `IVP_OR2NX8`    | `v3,v1,v2`  | `3252c70020c1452f` | `ivp_or2nx8 v3,v1,v2` |
| `IVP_XOR2NX8`   | `v3,v1,v2`  | `3252c63020c1452f` | `ivp_xor2nx8 v3,v1,v2` |
| `IVP_NOT2NX8`   | `v3,v1`     | `3252877800db452f` | `ivp_not2nx8 v3,v1` |
| `IVP_EQNX16`    | `vb0,v1,v2` | `029c58e49c04022f` | `{ ivp_eqnx16 vb0,v1,v2; nop }` |
| `IVP_LTNX16`    | `vb0,v1,v2` | `029c5a469c04022f` | `ivp_ltnx16 vb0,v1,v2` |
| `IVP_LTUNX16`   | `vb0,v1,v2` | `029c5a849c04022f` | `ivp_ltunx16 vb0,v1,v2` |
| `IVP_LENX16`    | `vb0,v1,v2` | `029c5a029c04022f` | `ivp_lenx16 vb0,v1,v2` |
| `IVP_NEQNX16`   | `vb0,v1,v2` | `029c5ac29c04022f` | `ivp_neqnx16 vb0,v1,v2` |

Two structural facts the oracle pins:

* **Compares write `vb` (the `vbool` short-name), arithmetic/logic write `v`.** The compares
  assemble into a *different bundle shape* (`029c…2f`, op in the first slot of a 2-op bundle) than
  the arithmetic ops (`32…2f`, op in the last slot of a 4-position bundle) — consistent with the
  compares riding the Mul/compare slot class (§6) and the arithmetic riding the trailing ALU slot.
  The `0x2f` trailer (op0 nibble `0xF`, even) is the **N0**/narrow framing
  ([flix-encoding §4](../core/flix-encoding.md)): `{nop;nop;nop;<alu>}` is exactly the
  `N0 = LdSt + None + None + ALU` profile.
* **`b0`/`br0` are rejected — the boolean operand spells `vb0`.** This is the `vbool` file's
  `shortname` (`vb`) from [register-files §3](../core/register-files.md), not the scalar `BR` file.

The device byte order is the assembler's packed-bundle layout and is **not** byte-identical to the
`libisa-core` slot-normalized selector imm of §2 (which is the *slot-word* template, a different
representation); they agree *structurally* — the placement exists, the mnemonic and register file
round-trip — which is the property the oracle certifies. `[HIGH/OBSERVED]`

---

## 6. Batch coverage tally — 50 mnemonics / 894 placements

Re-counted this pass with `nm libisa-core.so | rg -c 'Opcode_ivp_<glob>_Slot_…_encode'` (never the
decompile — [coverage-tally §0 GOTCHA](../core/coverage-tally.md)). Every one of the 50 grounds to
≥ 1 placement; **none ungrounded**.

| sub-family | mnemonics | placements | placements/op | slot reach |
|---|--:|--:|--:|---|
| add (wrap+sat) | 4 | 60 | 15 | ALU class (10) + Mul (4) + `F1_S0_LdStALU` |
| sub (wrap+sat) | 4 | 60 | 15 | same as add |
| abs (wrap+sat) | 4 | 76 | 19 | add-set **+ 4 LdSt slots** (1-source ⇒ rides LdSt) |
| neg (wrap+sat) | 4 | 76 | 19 | same as abs |
| min (signed+unsigned) | 6 | 90 | 15 | ALU+Mul class |
| max (signed+unsigned) | 6 | 90 | 15 | ALU+Mul class |
| eq / neq | 6 | 126 | 21 | ALU+Mul **+ Mul on every wide fmt + `N1_S2_Mul` + `N2_S0_LdSt`** |
| lt (signed+unsigned) | 6 | 126 | 21 | same as eq |
| le (signed+unsigned) | 6 | 126 | 21 | same as eq |
| logic and/or/xor | 3 | 45 | 15 | ALU+Mul class |
| logic not | 1 | 19 | 19 | add-set + 4 LdSt (1-source) |
| **TOTAL** | **50** | **894** | — | — |

The **placements/op pattern is itself a recovered fact** and a useful decode cross-check:

* **15** — the canonical two-source ALU op (add/sub/min/max/and/or/xor): the 10 ALU-class slots +
  the 4 Mul-class slots the dual-issue wide formats fold ALU into + `F1`'s fused `S0_LdStALU`.
* **19** — single-source ops (abs/neg/not) gain the **4 `*_s0_ldst` slots** (`F0`/`F3`/`F6`/`F7`):
  a one-operand op fits the LdSt slot's narrower operand budget where a two-source op cannot.
* **21** — the **compares** ride the Mul/compare slot class on every wide format *plus* the narrow
  `N1_S2_Mul` and `N2_S0_LdSt` — the predicate-producing path has the widest slot reach because the
  compare unit is shared with the Mul pipe's flag-producing stage.

These 894 are a strict subset of the **12 569 certified-perfect placements**; the float siblings
(B02), the 14 `…t` predicated forms (each `−2`/`−2` placements vs its base — e.g. `addnx16t`=13 vs
`addnx16`=15, `negnx16t`=17 vs `negnx16`=19, since the `t` forms drop the S4-ALU slots; B03), and
the `…b` predicate-file logic (B11) are counted in *their* batches, never here — no double-count.
`[HIGH/OBSERVED]`

> **NOTE — adjacency, deferred by design.** Three families share this datapath but are owned
> elsewhere: `ivp_abssub{,u}{nx16,2nx8}` (abs-of-difference — `|b−c|`, executed: `abssub_16_16_16`
> negates the difference if the borrow bit is set), `ivp_avg{,r,u}{nx16,2nx8}` (averaging —
> `avg=(b+c)>>1`, `avgr=(b+c+1)>>1` rounding, `avgu` unsigned, all executed live), and
> `ivp_minormax2nx8` (combined min&max select). They are cited so a reader who greps the
> `ivp_add`/`ivp_min` neighbourhood knows where they went, not to claim them. →
> [B03](b03-vec-alu-rest.md) / [B24](b24-composite.md). `[HIGH/OBSERVED]` on the adjacency.

---

## 7. Adversarial self-verification — the five strongest claims

Each re-challenged against the binary this pass; failures fixed.

1. **"50 base integer-ALU mnemonics own 894 placements, none ungrounded."** Re-run:
   `nm libisa-core.so | rg -c 'Opcode_ivp_(add2nx8|…|not2nx8)_Slot_…_encode'` over the explicit
   50-op glob = **894**; per-op loop shows **0 with zero placements**. The per-family breakdown
   (§6) sums `60+60+76+76+90+90+126+126+126+45+19 = 894`. ✓ `[HIGH/OBSERVED]`
2. **"Saturating add clamps asymmetrically (+→0x7fff, −→0x8000)."** Re-challenged by executing
   `adds_16_16_16(0x8000,0x8000)` live → **`0x8000`** (not `0x7fff`); `(0x7000,0x2000)` → `0x7fff`.
   The asymmetry is real and would be a bug if both clamped to `0x7fff`. ✓ `[HIGH/OBSERVED by
   execution]`
3. **"Signed and unsigned compare diverge; `lt` is signed, `ltu` unsigned."** Executed
   `lt_2_16_16(0x8000,0x0001)=0x3` but `ltu_2_16_16(0x8000,0x0001)=0x0` — opposite results on the
   same bits. Initially I assumed a single `lt` with a sign flag; the binary shows **two distinct
   opcodes** with two distinct value leaves *and two distinct `iclass#`* (`ltnx16`=345 vs
   `ltunx16`=349). Fixed. ✓ `[HIGH/OBSERVED by execution]`
4. **"The opcode-selector is per-(opcode×slot), not a global add/sub bit."** Re-challenged by
   disassembling `ivp_addnx16` across 8 slots: `0x80b50000` (F0_S3) ≠ `0x00090000` (F3_S3) ≠
   `0x12938000` (F1_S0_LdStALU) ≠ `0x6c480000` (N0_S3) ≠ `0x02a50000` (F7_S2_Mul). The clean
   nibble pattern is **F0_S3_ALU-local only**. The §2 imm column is correctly scoped to one slot
   and flagged; and the `s`/`u`/`t`/width variants each carry their own `opc#`/`iclass#` (§2). ✓
   `[HIGH/OBSERVED]`
5. **"Compares write `vbool`, not `vec`, with `64/num_lanes` predicate bits per lane."** Re-checked
   the leaf names (`eq_1_8_8`/`eq_2_16_16`/`eq_4_32_32`) and the device oracle (`ivp_eqnx16 vb0,…`
   assembles, `v0` as dest is the arithmetic form, `b0` is *rejected*). The `& 0x3` mask in
   `eq_2_16_16` and `& 0x1` in `eq_1_8_8` confirm 2-bit / 1-bit fields; `2×32 lanes = 4×16 lanes =
   1×64 = 64-bit vbool`. ✓ `[HIGH/OBSERVED]`

**Ungrounded / flagged items** (honest residue): (a) the `†`-marked compare imms in §2.4 are
**interpolated** from the disassembled `eq/lt/le` nibble model (`[MED/INFERRED]` imm; the `opc#`,
`iclass#`, and *relation* are `[HIGH/OBSERVED]`); (b) the role-field *scatter* of
`ivp_sem_vec_alu_vt/vbr` is OBSERVED in disasm but its exact bit composition is `[MED/INFERRED]`
beyond the clean `[14:10]`/`[19:15]`/`[24:20]` windows; (c) the **1-cycle ALU latency**
(`use@10 → def@11`) is `[HIGH/OBSERVED]` from `opcodes[]` use/def stages, but the full
cycle-accurate interlock (the cas-core model — `_IVP_ADDNX16_inst_stage*` symbols resolve) is
license-walled for *retirement* timing. None is a missing decode or a missing value semantics.

---

## 8. Cross-references

* [The FLIX VLIW Encoding](../core/flix-encoding.md) — the 14-format/46-slot grid these ops issue
  on, the encode-thunk ABI, and the §6.2 two-tier selector model the §2 GOTCHA invokes.
* [The Eight Register Files](../core/register-files.md) — `vec` (datapath), `vbool` (compare sink),
  the operand short-names (`v`/`vb`), and the `@10`/`@11` ALU stage model.
* [ISA Coverage & the 1534/12569 Tally](../core/coverage-tally.md) — the certified-perfect
  denominator this batch's 894/12569 is a subset of, and the `nm`-not-decompile counting rule.
* [B02 — Vector ALU (fp16/fp32 slice)](b02-vec-alu-fp.md) — the float siblings of every op here.
* [B03 — Vector ALU (int / B-variant / flag / predicated)](b03-vec-alu-rest.md) — the `…t`
  predicated forms, the `b…` B-variants, and the `abssub`/`avg`/`minormax` composites.
* [B11 — vbool ALU / predicate](b11-vbool-alu.md) — how the `vbool` masks these compares produce
  are consumed (predicated select / throttle), and the predicate-file `…b` boolean logic.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the license-walled
  cycle oracle, and the proven-by-execution value lane.

---

*Provenance: encoding (`Opcode_*`/`Field_*` thunks, `opc#`/`iclass#`), placement counts, and
operand bitfields are `[HIGH/OBSERVED]` — disassembled / `nm`-counted in-checkout from
`libisa-core.so` (`ncore2gp/config`, not stripped). Lane value semantics are `[HIGH/OBSERVED by
execution]` — the `module__xdref_*` leaves in `libfiss-base.so` were called live via ctypes
(license-free value lane). The byte round-trip is `[HIGH/OBSERVED]` from the device-native
`xtensa-elf-as`/`objdump` (`XTENSA_CORE=ncore2gp`); C intrinsic types from the shipped `xt_ivp32.h`.
ALU `use@10/def@11` latency is `[HIGH/OBSERVED]` from `opcodes[]` stages; cycle-accurate retirement
is license-walled. All facts read as derived from shipped-artifact static analysis and in-process
execution of license-free leaves (lawful interoperability RE).*
