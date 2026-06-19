# ISA Batch 12 — Vector Shift / Rotate / Normalize

This is the per-instruction batch for the **bit-positioning datapath on the `vec` register file**: the
per-lane logical and arithmetic shifts (`ivp_sll*` left, `ivp_srl*` logical-right zero-fill,
`ivp_sra*` arithmetic-right sign-fill), the rotates (`ivp_rotr*` — a lane-internal rotate-right with no
bit loss), and the **normalize-shift-amount** primitive (`ivp_nsa*` / `ivp_nsau*` — count-leading-sign
/ count-leading-zero, the front half of a block-float normalize). Each shift verb ships in two
flavours: an **immediate-amount** form (`ivp_slli*`, the literal shift count packed into the bundle)
and a **per-lane-vector-amount** form (`ivp_slln*`, the shift count read element-wise from a second
`vec` source). The batch owns **24 mnemonics / 266 of the 12 569 shipped placements**
([the coverage tally's certified-perfect denominator](../core/coverage-tally.md)).

Everything below is re-grounded against the shipped binaries this pass: the **encoding** from
`libisa-core.so` (the `Opcode_<mnem>_Slot_<slot>_encode` thunks, the `Field_*_get` operand accessors,
and the immediate-amount `vec_alu_i_imm{3,4,5}` fields); the **value semantics** by *executing* the
matching `module__xdref_*` leaves in `libfiss-base.so` live in-process (license-free) over full input
domains; an independent **encode/decode oracle** from the device-native `xtensa-elf-as`/
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`); and **retirement latency** from the `libcas-core.so`
per-instruction stage symbols. Confidence tags per
[the Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-execution, `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` =
re-used at a sibling page's confidence. Prose reads as derived from shipped-artifact static analysis
and in-process execution of license-free value leaves (lawful interoperability RE).

> **Scope split — read this before pairing a mnemonic to a batch.** This batch is the **standalone
> `vec → vec` bit-shift core**. Four boundaries are enforced so the 30 batches never double-count:
> * **The de-scaling right-shift *inside* a `wvec` pack → [B10](b10-wvec-pack.md), not here.** A
>   `pack`/`packv` op folds a rounding arithmetic-right-shift into its wide-accumulator readout (the
>   `…rnx48` "shift-and-round" pack); that shift is an *operand* of the pack opcode, owned by B10. We
>   own only the **freestanding** `ivp_sll*`/`ivp_srl*`/`ivp_sra*` that take a `vec` source and write a
>   `vec` destination.
> * **Bit-reverse / shuffle / element permute → [B21](b21-select-shuffle.md), not here.** `ivp_shfl*`
>   (the `shfl2nx8`/`shfl…i_s0/_s2/_s4` family) is a *cross-lane element permutation*, not a within-lane
>   bit shift; it shares the `sh*` name prefix but is structurally B21's. The classifier's `sh` token is
>   a trap — `shfl` is **not** ours.
> * **Boolean / predicate-file rotates (`ivp_rorb*`) → [B11](b11-vbool-alu.md).** `ivp_rorbn`/`rorb2n`/
>   `rorbn_2` rotate a **`vbool`/predicate** register's bit-mask, not a `vec` data lane; they live in
>   B11. Our `ivp_rotr*` rotate data inside a `vec` lane.
> * **Base-Xtensa scalar shifts (`sll`/`srl`/`sra`/`slli`/`srai`/`src`/`ssl`/`ssr`/`ssa8`) →
>   [B25](b25-xt-core.md).** Those operate on the `AR` scalar file and the `SAR` special register (the
>   `ssl ax`/`sll`/`src` funnel idiom); they are the *scalar* shift ISA. The `ivp_`-prefixed ops here
>   are the *vector* shift ISA and take **no** `AR`/`SAR` operand (the device assembler rejects an
>   `a`-register as a shift amount — §3.3).

---

## 1. Batch key facts

| Fact | Value | Binary source |
|---|---|---|
| Datapath register file | `vec` (idx 2) — 32 × 512-bit, 5-bit index, ctype `…vec2Nx8` | `regfiles[]` ([register-files §3](../core/register-files.md)) |
| Mnemonics this batch | **24** | §2; `nm libisa-core.so` distinct `Opcode_ivp_{sll,srl,sra,rotr,nsa}…` |
| Placements this batch | **266** | `nm \| rg -c` on the explicit 24-op glob (§6) |
| Value leaves resolved | **30** | `nm libfiss-base.so \| rg -c 'xdref_(sll\|srl\|sra\|rotr\|nsa\|nsau)_'` (§6) |
| Semantic iclass | `ivp_sem_vec_shift` (fields `vr`/`vt`/`vs`) + `ivp_sem_vec_alu_i_imm{3,4,5}` for the imm amount | `Field_fld_ivp_sem_vec_shift_*` / `…_vec_alu_i_imm*` thunks (§3) |
| Functional unit | the `alu` slot class (immediate forms additionally fold into the Mul-class slot) | `slots[]` ([flix-encoding §5](../core/flix-encoding.md)) |
| Retirement latency | issues stage 0, retires **stage 11** (1-cycle ALU, `use@10 → def@11`) | `libcas-core.so` `IVP_*_inst_stage{0..11}` symbols `[HIGH/OBSERVED]` |
| Lane geometry | `2nx8`=64×8b · `nx16`=32×16b · `n_2x32`=16×32b (one 512-bit reg) | value-leaf width suffix; `xt_ivp32.h` typedefs |
| Encode-thunk ABI | `C7 07 imm32 [C7 47 04 0] C3` — `imm32` = the `(opcode×slot)` selector, `word1≡0` | [flix-encoding §6.1](../core/flix-encoding.md) |
| Shift-leaf ABI (binary) | `void leaf(uint64 ctx, uint32 val, uint32 amt, uint32 *out)` — `val`=`rsi`, `amt`=`rdx`, `out`=`rcx` | disassembled §4 |
| Normalize-leaf ABI (unary) | `void leaf(uint64 ctx, uint32 val, uint32 *out)` — `val`=`rsi`, count→`rdx` | disassembled §4.5 |
| Oracle | `xtensa-elf-as`/`objdump`, `XTENSA_CORE=ncore2gp` | round-trips all forms byte-exact (§5) |

The whole batch issues on the **ALU slot class** of the FLIX grid — the same trailing-ALU slots the
integer ALU core (B01) uses (`F0_S3_ALU`, `F1_S3_ALU`, …, `N0_S3_ALU`). The *immediate-amount* forms
additionally fold into the **Mul-class** slot of the dual-issue wide formats (`F1_S2_Mul`, `F2_S2_Mul`,
`F7_S2_Mul`), which is why an `ivp_slli*` has **12** placements against an `ivp_slln*`'s **9** — the
immediate form's smaller operand budget (it spends the third operand window on a 3–5-bit literal rather
than a 5-bit `vec` index) fits the Mul slot where the three-`vec`-source variable form cannot (§6).

> **The whole batch is one barrel-shifter slice multiplexed by decoded op-group.** All 24 opcodes
> decode into a single semantic shift slice (`ivp_sem_vec_shift`) selected by decoded group signals:
> `op_LEFT` vs `op_RIGHT` (direction), `op_GRP_ARITH` (sign-fill vs zero-fill on right shift),
> `op_GRP_ROTATE` (feed the shifted-out bits back in), `op_GRP_NORMALIZE` (the count-leading path), and
> the `op_GRP_{8,16,32}BIT` lane-width selector that sets the lane-internal mask. A reimplementation
> builds **one** parameterized per-lane barrel shifter — `(val, amt, width, {logical|arith|rotate})` —
> plus a small count-leading-bit unit for the normalize path, and drives both from the decoded opcode
> group, exactly as §4's leaves do. `[HIGH/OBSERVED]`

---

## 2. Batch roster — 24 shift / rotate / normalize opcodes

Columns: `mnemonic` · `lanes×width` · representative `F0_S3_ALU` **opcode-selector imm** (the
`Opcode_<mnem>_Slot_f0_s3_alu_encode` thunk's `movl $imm`, disassembled this pass) · **shift-amount
source** (immediate field / per-lane `vec`) and its bit-width · one-line lane semantics · `[conf]`.
Every selector imm is for the **`F0_S3_ALU` slot specifically** and `word1 ≡ 0` (the §3 GOTCHA: the
selector is per-`(opcode×slot)`, never a roster-wide direction bit). The three `vec` operand fields
(dest `vr`, source `vt`, amount-source `vs`) occupy slot-fixed bit windows identical across the batch
(§3.2). Every placement here is an **8-byte** FLIX bundle (`formats[].length` = 8 for the ALU/Mul slots
these ops occupy), so a per-row byte-size column is omitted.

### 2.1 Logical left shift (`sll` — zero-fill from the right)

| mnemonic | lanes×w | F0_S3_ALU sel | amount source | semantics | conf |
|---|---|---|---|---|---|
| `ivp_slli2nx8`   | 64×8  | `0x868c8100` | imm3 (3-bit, 0–7)   | `a = (b << k) & 0xFF` per lane | `[HIGH/OBSERVED]` |
| `ivp_sllinx16`   | 32×16 | `0x868c8200` | imm4 (4-bit, 0–15)  | `a = (b << k) & 0xFFFF` | `[HIGH/OBSERVED]` |
| `ivp_sllin_2x32` | 16×32 | `0x86a38000` | imm5 (5-bit, 0–31)  | `a = b << k` (32-bit) | `[HIGH/OBSERVED]` |
| `ivp_slln_2x32`  | 16×32 | `0x86b38000` | per-lane `vec` (signed amt) | `a = b <</>> amt_lane` (bidirectional, §4.4) | `[HIGH/OBSERVED]` |
| `ivp_sllnx16`    | 32×16 | `0x86ab8000` | per-lane `vec` (signed amt) | `a = b <</>> amt_lane`, over-shift → 0 | `[HIGH/OBSERVED]` |

### 2.2 Logical right shift (`srl` — zero-fill from the left)

| mnemonic | lanes×w | F0_S3_ALU sel | amount source | semantics | conf |
|---|---|---|---|---|---|
| `ivp_srli2nx8`   | 64×8  | `0x868d8100` | imm3 (3-bit) | `a = (b & 0xFF) >> k` (zero-fill) | `[HIGH/OBSERVED]` |
| `ivp_srlinx16`   | 32×16 | `0x868e0200` | imm4 (4-bit) | `a = (b & 0xFFFF) >> k` | `[HIGH/OBSERVED]` |
| `ivp_srlin_2x32` | 16×32 | `0x86ac8000` | imm5 (5-bit) | `a = b >>u k` (32-bit logical) | `[HIGH/OBSERVED]` |
| `ivp_srln_2x32`  | 16×32 | `0x86bc8000` | per-lane `vec` | logical right, over-shift → 0 | `[HIGH/OBSERVED]` |
| `ivp_srlnx16`    | 32×16 | `0x86b48000` | per-lane `vec` | logical right, over-shift → 0 | `[HIGH/OBSERVED]` |

### 2.3 Arithmetic right shift (`sra` — sign-fill from the left)

| mnemonic | lanes×w | F0_S3_ALU sel | amount source | semantics | conf |
|---|---|---|---|---|---|
| `ivp_srai2nx8`   | 64×8  | `0x868d0100` | imm3 (3-bit) | `a = sext8(b) >> k` (sign-fill) | `[HIGH/OBSERVED]` |
| `ivp_srainx16`   | 32×16 | `0x868d8200` | imm4 (4-bit) | `a = sext16(b) >> k` | `[HIGH/OBSERVED]` |
| `ivp_srain_2x32` | 16×32 | `0x86b40000` | imm5 (5-bit) | `a = b >>s k` (32-bit arithmetic) | `[HIGH/OBSERVED]` |
| `ivp_sran_2x32`  | 16×32 | `0x86a48000` | per-lane `vec` (signed amt) | arithmetic right, bidirectional | `[HIGH/OBSERVED]` |
| `ivp_sranx16`    | 32×16 | `0x86bc0000` | per-lane `vec` (signed amt) | arithmetic right, bidirectional | `[HIGH/OBSERVED]` |

### 2.4 Rotate right (`rotr` — lane-internal, no bit loss)

| mnemonic | lanes×w | F0_S3_ALU sel | amount source | semantics | conf |
|---|---|---|---|---|---|
| `ivp_rotri2nx8`   | 64×8  | `0x868c0100` | imm3 (3-bit) | `a = ror8(b, k & 7)` | `[HIGH/OBSERVED]` |
| `ivp_rotrinx16`   | 32×16 | `0x868c0200` | imm4 (4-bit) | `a = ror16(b, k & 15)` | `[HIGH/OBSERVED]` |
| `ivp_rotrin_2x32` | 16×32 | `0x86ba8000` | imm5 (5-bit) | `a = ror32(b, k & 31)` | `[HIGH/OBSERVED]` |
| `ivp_rotrn_2x32`  | 16×32 | `0x86ab0000` | per-lane `vec` | `a = ror32(b, amt_lane & 31)` | `[HIGH/OBSERVED]` |
| `ivp_rotrnx16`    | 32×16 | `0x86a30000` | per-lane `vec` | `a = ror16(b, amt_lane & 15)` | `[HIGH/OBSERVED]` |

### 2.5 Normalize shift amount (`nsa`/`nsau` — count-leading, unary, count → `vec`)

| mnemonic | lanes×w | F0_S3_ALU sel | source | semantics (count emitted) | conf |
|---|---|---|---|---|---|
| `ivp_nsanx16`    | 32×16 | `0x808f8304` | `vec` (1 src) | `a = CLS16(b) − 1` (leading-sign run minus one) | `[HIGH/OBSERVED]` |
| `ivp_nsan_2x32`  | 16×32 | `0x808f8306` | `vec` (1 src) | `a = CLS32(b) − 1` | `[HIGH/OBSERVED]` |
| `ivp_nsaunx16`   | 32×16 | `0x809f8304` | `vec` (1 src) | `a = CLZ16(b)` (leading-zero count) | `[HIGH/OBSERVED]` |
| `ivp_nsaun_2x32` | 16×32 | `0x809f8306` | `vec` (1 src) | `a = CLZ32(b)` | `[HIGH/OBSERVED]` |

> **QUIRK — `i` means immediate-amount, no `i` means per-lane-vector amount; there is no AR-scalar
> amount form.** The naming is the amount *source*, not the operation: `ivp_sllinx16` (`i` after `sll`)
> reads its shift count from a **3/4/5-bit literal field packed into the bundle**; `ivp_sllnx16` (no
> `i`) reads a **per-lane count from a second `vec` register** — each 16-bit lane shifts by *its own*
> amount. The Vision-Q7 vector shift ISA has **no AR-scalar broadcast-amount form**: the device
> assembler rejects an `a`-register as the third operand (`Error: bad register name: a2`, §3.3).
> Broadcast-by-one-scalar is achieved by splatting the scalar into a `vec` first (B16) and then issuing
> the variable form. (The *scalar* `SAR`-based shift `sll`/`src` is base-Xtensa, B25.) `[HIGH/OBSERVED]`

> **QUIRK — `nsa*` is a unary op whose result is a *count*, not a shifted value.** `ivp_nsanx16 v3,v1`
> takes **one** `vec` source and writes a per-lane **shift-amount** (an exponent/normalization count)
> into `v3`. To normalize a value you then issue `ivp_sllnx16 v3, v1, <nsa-result>` — the two-instruction
> "compute exponent, then shift" sequence is the block-float normalize (§4.5). `nsa` (signed) counts the
> **leading sign run minus 1** so that the post-shift MSB sits one bit below the sign (a guard bit);
> `nsau` (unsigned) is a plain count-leading-zeros. The destination is `vec`, **not** `vbool`/`b32_pr`
> — the device assembler rejects `vb0` as the `nsa` destination (§3.3). `[HIGH/OBSERVED]`

---

## 3. Encoding — amount sources, field windows, the selector structure

### 3.1 The lane-geometry suffix and the amount bit-width are linked

The mnemonic suffix re-partitions one 512-bit `vec` into a SIMD vector exactly as in B01:

| suffix | element | lanes | imm-amount field | imm max | var-amount lane mask |
|---|---|---|---|---|---|
| `2nx8`   | 8-bit  | 64 | `imm3` (3-bit) | 7  | `amt & 7` |
| `nx16`   | 16-bit | 32 | `imm4` (4-bit) | 15 | `amt & 15` |
| `n_2x32` | 32-bit | 16 | `imm5` (5-bit) | 31 | `amt & 31` |

The immediate-amount field width is exactly `log2(element_width)` — 3/4/5 bits index 0…(width−1). This
is read off both the field accessor (`vec_alu_i_imm3/4/5`, §3.2) *and* the value-leaf suffix
(`sll_u_16_16_4` — the trailing `4` is the immediate amount's bit-width) *and* confirmed by the device
assembler's range check: `IVP_SLLINX16 v3,v1,16` is rejected (`operand 3 … has invalid value '16'`,
imm4 maxes at 15), `IVP_SLLI2NX8 v3,v1,8` rejected (imm3 maxes at 7), `IVP_SLLIN_2X32 v3,v1,32`
rejected (imm5 maxes at 31); `15`/`7`/`31` respectively assemble. `[HIGH/OBSERVED]`

The **variable** forms carry a per-lane amount field one bit *wider* — the value-leaf suffix is `6`
(16-bit) / `7` (32-bit), a **6/7-bit signed** amount whose extra bit is the **direction sign** (§4.4).
That is why `sll_u_16_16_4` (immediate) and `sll_s_16_16_6` (variable) are *different leaves*: the
immediate amount is unsigned `[0,15]`, the variable amount is signed `[−32,+31]`.

### 3.2 The slot-word operand fields (`vec_shift` iclass + `imm` field)

The `ivp_sem_vec_shift` iclass exposes three `vec` role fields and the shifts additionally pull an
immediate field; all disassembled this pass for `F0_S3_ALU`:

```c
// dest vr — Field_fld_ivp_sem_vec_shift_vr_Slot_f0_s3_alu_get @ 0x32e650 :
//   edx = slotword & 1 ;  eax = (slotword >> 3) & 0x1e ;  vr = eax | edx
//   -> a 5-bit vec index assembled from a scattered low bit + a 4-bit run (role-scattered, not one window)
// src  vt — Field_fld_ivp_sem_vec_shift_vt_Slot_f0_s3_alu_get @ 0x32e980 :
//   eax = (slotword << 0x11) >> 0x1b   == zero-extended bits [14:10]   -> 5-bit vec index
// amt  vs — Field_fld_ivp_sem_vec_shift_vs_Slot_f0_s3_alu_get @ 0x335640 :
//   edx = (slotword << 0x1c) >> 0x1d ;  eax = (slotword >> 5) & 0x18 ;  vs = eax | edx
//   -> 5-bit vec index, two disjoint ranges OR-ed (the amount-source vec, variable forms only)
// imm amount — Field_fld_ivp_sem_vec_alu_i_imm4_Slot_f0_s3_alu_get @ 0x32ae70 :
//   eax = (slotword << 0x12) >> 0x1c   == bits [13:10]   -> 4-bit immediate shift count (sllinx16)
//   imm3 @ 0x32a9f0 : (slotword << 0x13) >> 0x1d  == bits [12:10]  (3-bit, 2nx8)
//   imm5 @ 0x32b2f0 : (slotword << 0x11) >> 0x1b  == bits [14:10]  (5-bit, n_2x32)
// the matching _set thunks do  `and $0x{7,f,1f}; shl $0xN`  — bit-width deposit, confirming the inverse.
```

The immediate amount and the variable amount-source occupy the **same** slot-word region (the bits
`[14:10]` in `vt`'s neighbourhood): an immediate form spends that window on a literal count, a variable
form spends it on the `vs` `vec` index. The `vr`/`vs` role fields **scatter** (a disjoint low bit OR-ed
with a 4-bit run) — a reimplementation deposits the 5-bit `vec` index through the role's `_set` thunk,
never by a single contiguous mask. `[HIGH/OBSERVED]`

### 3.3 The selector is per-`(opcode×slot)`, and the device operand grammar pins the amount source

Every thunk is the canonical `C7 07 imm32 ; C7 47 04 00000000 ; C3` template — re-disassembled this
pass, e.g.:

```asm
0000000000343060 <Opcode_ivp_sllinx16_Slot_f0_s3_alu_encode>:   # immediate form
  343060: c7 07 00 82 8c 86    movl  $0x868c8200,(%rdi)
  343066: c7 47 04 00 00 00 00 movl  $0x0,0x4(%rdi)            # word1 ≡ 0
  34306d: c3                   ret
0000000000343400 <Opcode_ivp_sllnx16_Slot_f0_s3_alu_encode>:    # variable (vec amount) form
  343400: c7 07 00 80 ab 86    movl  $0x86ab8000,(%rdi)
  343406: c7 47 04 00 00 00 00 movl  $0x0,0x4(%rdi)
  34340d: c3                   ret
0000000000344320 <Opcode_ivp_nsanx16_Slot_f0_s3_alu_encode>:    # normalize (unary) form
  344320: c7 07 04 83 8f 80    movl  $0x808f8304,(%rdi)
  344326: c7 47 04 00 00 00 00 movl  $0x0,0x4(%rdi)
  34432d: c3                   ret
```

The `nsa*` family shares the `0x808f83xx` / `0x809f83xx` base (the `0x808xxxx` special/compare-class
base, the same region B01's compares draw from at `0x80870000`): the `0x__0f____ → __9f____` nibble
flips `nsa` (signed/sign-count) to `nsau` (unsigned/zero-count), and the `…04`/`…06` low nibble selects
16-bit vs 32-bit. The shift verbs draw the `0x86_____` ALU base. As in B01, these clean nibble patterns
hold **only within `F0_S3_ALU`** — the selector is format-local opcode packing, not a roster-wide
direction/arith bit.

The **device assembler is the operand-grammar oracle** (`XTENSA_CORE=ncore2gp`, this pass) — it pins
the amount source structurally:

```
IVP_SLLNX16  v3,v1,v2   -> { nop;nop;nop; ivp_sllnx16  v3, v1, v2 }   (3 vec operands: dst, src, amt-vec)
IVP_SLLINX16 v3,v1,4    -> { nop;nop;nop; ivp_sllinx16 v3, v1, 4 }    (dst, src, literal)
IVP_NSANX16  v3,v1      -> { nop;nop;nop; ivp_nsanx16  v3, v1 }       (unary: dst-count, src)
IVP_SLLNX16  v3,v1,a2   -> Error: bad register name: a2               (no AR-scalar amount)
IVP_SLLINX16 v3,v1,16   -> Error: operand 3 of 'ivp_sllinx16' has invalid value '16'  (imm4 ≤ 15)
IVP_NSANX16  vb0,v1     -> Error                                       (nsa dest is vec, not vbool)
```

`[HIGH/OBSERVED]`

---

## 4. Lane value semantics — proven by execution

The `module__xdref_*` value leaves in `libfiss-base.so` are the per-element value functions, **callable
in-process via ctypes with no license**. The binary-shift ABI (disassembly-fixed) is
`void leaf(uint64 ctx /*rdi, unused*/, uint32 val /*rsi*/, uint32 amt /*rdx*/, uint32 *out /*rcx*/)`;
the normalize ABI drops the `amt` argument and writes the count to `*rdx`. Every run below was executed
live this pass.

### 4.1 Logical right (`srl`, zero-fill) vs arithmetic right (`sra`, sign-fill) — live

`srl_u_16_16_4` is `mov %edx,%ecx ; shr %cl,%esi ; mov %esi,(%rax)` — a plain zero-filling logical
shift. `sra_u_16_16_4` is `movswl %si,%esi ; and $0x7fffffff,%esi ; shr %cl,%esi ; and $0xffff` — it
**sign-extends** the 16-bit lane to 32 bits, then logical-shifts the sign-replicated value, then
re-narrows: an arithmetic shift realized by the sign-extend-then-logical-`shr` idiom. Executed
(`amt = 4`):

```
 value     srl (>>4, zero-fill)   sra (>>4, sign-fill)
 0x8000        0x0800                 0xf800        # sra replicates the sign bit
 0xc000        0x0c00                 0xfc00
 0xff00        0x0ff0                 0xfff0
 0x7fff        0x07ff                 0x07ff        # non-negative: identical
 0x0f00        0x00f0                 0x00f0
```

```c
// ivp_srlnx16 lane (logical right, 16-bit):     return (uint16_t)((uint16_t)b >> k);   // zero-fill
// ivp_sranx16 lane (arithmetic right, 16-bit):  return (uint16_t)((int16_t) b >> k);   // sign-fill
//   (the leaf realizes sra by sext16->shr; the visible result is the C arithmetic shift, bit-exact)
```

The `0x8000 >> 4` row is the decisive edge: `srl = 0x0800` (logical) vs `sra = 0xf800` (arithmetic). A
reimplementation that uses one shift for both is wrong on every negative lane.
`[HIGH/OBSERVED by execution]`

### 4.2 Over-shift masking — the variable form clamps `amt ≥ width → 0`, the immediate form is range-bounded — live

This is the §1 reimplementation trap. The **variable** 6-bit leaves carry an explicit over-shift guard:
`sll_u_16_16_6` is `cmp $0x1f,%edx ; ja → result=0 ; else shl %cl,%esi ; movzwl` — if the (masked) amount
exceeds 31 the lane is zeroed; `srl_u_16_16_6` is `shr %cl ; cmp $0x1f,%edx ; cmova $0,%esi` likewise.
Because a 16-bit lane is fully shifted out by any amount ≥ 16, the *observable* result is **0 for any
amount ≥ 16**. Executed:

```
 amt  srl_u_16_16_6(0xFFFF)   sll_u_16_16_6(0x0001)
   0       0xffff                  0x0001
  15       0x0001                  0x8000
  16       0x0000                  0x0000        # fully shifted out -> 0
  31       0x0000                  0x0000
  32       0x0000                  0x0000        # leaf's >31 guard also yields 0
  63       0x0000                  0x0000
```

The **immediate** form has no runtime over-shift path: its amount field is only 3/4/5 bits, so it
*cannot* encode an out-of-range count — the device assembler rejects `16`/`8`/`32` at assembly time
(§3.3). A reimplementation must: (a) for the variable form, check `amt ≥ width → 0` *before* a bare host
shift (whose count would otherwise wrap mod register width and give a wrong non-zero result); (b) for the
immediate form, trust the field width. `[HIGH/OBSERVED by execution]`

```c
// variable logical-left/right lane (16-bit), exact per the executed _6 leaf:
uint16_t sll_var16(uint16_t b, int amt /*signed lane*/) {
    if (amt < 0) return srl_var16(b, -amt);          // signed amount reverses direction (§4.4)
    if (amt >= 16) return 0;                          // over-shift -> 0 (no host mod-wrap)
    return (uint16_t)(b << amt);
}
```

### 4.3 Rotate right — lane-internal, no bit loss — live

`rotr_u_16_16_4` is the textbook `left = b << ((-amt) & 0xf) ; right = b >> amt ; result = (left|right) &
0xffff`; the `& 0xf` (16-bit) / `& 0x7` (8-bit) / `& 0x1f` (32-bit) masks the rotate amount to the lane
width so a full rotation is the identity. Executed:

```
 rotr16(0x1234, 4) = 0x4123     rotr16(0x000f, 4) = 0xf000   # low nibble wrapped to the top
 rotr16(0x8001, 1) = 0xc000     rotr16(0x0001, 1) = 0x8000   # LSB rotates into the MSB
 rotr16(0xabcd, 8) = 0xcdab     rotr8 (0xab,   3) = 0x75
```

No bit is lost — every set bit that leaves the bottom re-enters at the top. There is **no rotate-left
opcode**; a left rotate by `k` is `rotr*` by `width − k` (the rotate amount is taken mod width).
`[HIGH/OBSERVED by execution]`

```c
// ivp_rotrnx16 lane:  uint16_t ror16(uint16_t b, unsigned k){ k&=15; return (uint16_t)((b>>k)|(b<<((16-k)&15))); }
```

### 4.4 The variable form is **bidirectional** — a signed lane amount flips direction — live

The variable (`_s`/`_6`/`_7`) leaves treat the per-lane amount as **signed**: `sra_s_16_16_6` is
`test $0x20,%dl` (bit-5 of the 6-bit amount = sign) — if set, `neg %edx ; and $0x3f` (magnitude) and
dispatch to the **opposite-direction** shift. So a *negative* amount on a left-shift opcode performs a
right shift, and vice-versa. Executed (6-bit signed amount: `0x3C = −4`, `0x04 = +4`):

```
 sll_s_16_16_6(0x00F0, +4) = 0x0F00     sll_s_16_16_6(0x00F0, -4) = 0x000F   # left opcode, neg amt -> right
 srl_s_16_16_6(0x0F00, +4) = 0x00F0     srl_s_16_16_6(0x0F00, -4) = 0xF000   # right opcode, neg amt -> left
 sra_s_16_16_6(0x00F0, +4) = 0x000F     sra_s_16_16_6(0x00F0, -4) = 0x0F00
 sra_s_16_16_6(0x00F0, -32 [0x20]) = 0x0000                                  # magnitude 32 -> fully out
```

This is the single most important quirk for a compiler back-end: one *variable* shift opcode covers
both directions via the sign of the per-lane amount, so a "shift by a signed exponent" (e.g. a per-lane
`ldexp`-style rescale) is one instruction, not a select between `sll`/`srl`. The *immediate* forms have
no sign bit and are unidirectional. `[HIGH/OBSERVED by execution]`

### 4.5 Normalize — count-leading-sign / count-leading-zero, value + count — live

`nsau_16_16` (unsigned, count-leading-zeros): `test %esi ; jne → mov $0xf,%eax ; bsr %esi,%esi ; sub`
— `15 − bsr(v)` = CLZ over 16 bits, with the `v == 0` special case returning 16. `nsa_16_16` (signed,
count-leading-sign-minus-1): if the sign bit is set it inverts the value (`not ; and $0x7fff`), forms
`x = 2·x′ + 1` (to keep `bsr` defined), then `15 − bsr(x)` — i.e. it counts the leading *sign* run and
subtracts one, leaving a single guard bit below the sign. The 32-bit forms use `0x1f`/`0x20`.
Executed, with the normalize completed by `ivp_sllnx16 v, nsa(v)`:

```
 nsa16 (count-leading-sign − 1)               nsau16 (count-leading-zero)
 v=0x0000 nsa=15  norm(v<<15)=0x0000           v=0x0000 nsau=16
 v=0x0001 nsa=14  norm(v<<14)=0x4000           v=0x0001 nsau=15
 v=0x4000 nsa= 0  norm        =0x4000           v=0x4000 nsau= 1
 v=0x7fff nsa= 0  norm        =0x7fff           v=0x8000 nsau= 0
 v=0x8000 nsa= 0  norm        =0x8000           v=0x00ff nsau= 8
 v=0xc000 nsa= 1  norm(v<<1) =0x8000
 v=0xffff nsa=15  norm(v<<15)=0x8000
 v=0x00ff nsa= 7  norm(v<<7) =0x7f80

 32-bit:  nsa32(0x00000001)=30  nsau32(0x00000001)=31   nsa32(0xFFFFFFFF)=31  nsau32(0xFFFFFFFF)=0
          nsa32(0x40000000)= 0  nsau32(0x80000000)= 0   nsa32(0x0000FFFF)=15  nsau32(0x0000FFFF)=16
```

The **signed** `nsa` leaves a guard bit (e.g. `0x0001 → nsa 14 → 0x4000`, MSB at bit-14 not bit-15) so
the normalized value stays a *signed* magnitude that will not alias the sign bit; the **unsigned** `nsau`
packs the leading one all the way to bit 15 (`0x0001 → nsau 15`). The two-instruction sequence
`nsa; sll` is the block-float normalize: `nsa` produces the exponent, `sll` applies it.
`[HIGH/OBSERVED by execution]`

```c
// ivp_nsaunx16 lane:  uint16_t clz16(uint16_t v){ if(!v) return 16; return 15 - bsr16(v); }   // bsr = index of MSB
// ivp_nsanx16  lane:  uint16_t cls16(int16_t v){ uint16_t x = (v<0)? (uint16_t)(~v)&0x7fff : (uint16_t)v&0x7fff;
//                                                 x = 2*x + 1; return 15 - bsr16(x); }         // leading-sign − 1
```

> **GOTCHA — `bsr(0)` is undefined, and both normalize leaves special-case it.** `nsau` returns the
> full lane width on a zero input (`16`/`32`); `nsa` forms `2·v+1` so the `bsr` argument is never zero
> even for `v = 0` (which then yields `15`/`31`). A reimplementation that runs `width − bsr(v)` without
> the zero guard reads garbage on an all-zero lane — a real correctness hazard the leaves close.
> `[HIGH/OBSERVED]`

---

## 5. Device-assembler oracle — byte-exact round-trip

Feeding the device-native `xtensa-elf-as` (`XTENSA_SYSTEM=…/ncore2gp/config`, `XTENSA_CORE=ncore2gp`)
the mnemonics and disassembling back. Every form assembles `rc=0` and round-trips to the same lowercase
mnemonic as a single op in an **8-byte FLIX bundle** (the rest `nop`). Verbatim bytes (LE), this pass:

| mnemonic | operands | 8-byte bundle | disasm |
|---|---|---|---|
| `IVP_SLLNX16`    | `v3,v1,v2` | `3252c7a800c2452f` | `{ nop;nop;nop; ivp_sllnx16 v3,v1,v2 }` |
| `IVP_SLLINX16`   | `v3,v1,4`  | `3250ce0800d4452f` | `ivp_sllinx16 v3,v1,4` |
| `IVP_SRLNX16`    | `v3,v1,v2` | `3252c6f800c2452f` | `ivp_srlnx16 v3,v1,v2` |
| `IVP_SRLINX16`   | `v3,v1,4`  | `3252870800d4452f` | `ivp_srlinx16 v3,v1,4` |
| `IVP_SRANX16`    | `v3,v1,v2` | `3252c7d800c2452f` | `ivp_sranx16 v3,v1,v2` |
| `IVP_SRAINX16`   | `v3,v1,4`  | `3250cf0800d4452f` | `ivp_srainx16 v3,v1,4` |
| `IVP_ROTRNX16`   | `v3,v1,v2` | `3252c68800c2452f` | `ivp_rotrnx16 v3,v1,v2` |
| `IVP_ROTRINX16`  | `v3,v1,4`  | `3250ce0800c4452f` | `ivp_rotrinx16 v3,v1,4` |
| `IVP_NSANX16`    | `v3,v1`    | `3252877800de452f` | `ivp_nsanx16 v3,v1` |
| `IVP_NSAUNX16`   | `v3,v1`    | `32514d0800c0452f` | `ivp_nsaunx16 v3,v1` |
| `IVP_NSAN_2X32`  | `v3,v1`    | `3252877800df452f` | `ivp_nsan_2x32 v3,v1` |
| `IVP_SLLN_2X32`  | `v3,v1,v2` | `3252c6b800c2452f` | `ivp_slln_2x32 v3,v1,v2` |
| `IVP_SLLIN_2X32` | `v3,v1,5`  | `3252c6a800c5452f` | `ivp_sllin_2x32 v3,v1,5` |
| `IVP_SLLI2NX8`   | `v3,v1,3`  | `3252875800db452f` | `ivp_slli2nx8 v3,v1,3` |
| `IVP_ROTRI2NX8`  | `v3,v1,3`  | `3252875800d3452f` | `ivp_rotri2nx8 v3,v1,3` |

Three structural facts the oracle pins:

* **Variable forms take three `vec` operands** (`v3,v1,v2` — dest, source, per-lane amount), **immediate
  forms take two `vec` + a literal** (`v3,v1,4`), **normalize takes two `vec`** (`v3,v1` — count-dest,
  source). The third-operand grammar is the amount-source discriminator (§3.3).
* **An `a`-register (AR) is rejected as the amount** — there is no scalar-broadcast vector shift; and
  **`vb0` is rejected as the `nsa` destination** — the count lands in `vec`. Both are hard assembler
  errors (§3.3).
* The packed-bundle byte order is the assembler's layout and is **not** byte-identical to the
  `libisa-core` slot-normalized selector imm of §2 (a different representation); they agree
  *structurally* — the placement exists, the mnemonic and operand files round-trip — which is the
  property the oracle certifies. Note the operand encoding tracks the amount source precisely: the
  variable forms carry the `…c2…` operand byte (vec-amount), the immediate forms carry `…d4…` /
  `…c4…` (imm-amount), in a common `…45 2f` narrow-`N0` frame.

`[HIGH/OBSERVED]`

---

## 6. Batch coverage tally — 24 mnemonics / 266 placements / 30 value leaves

Re-counted this pass with `nm libisa-core.so | rg -c 'Opcode_ivp_<glob>_Slot_…_encode'` over the
explicit 24-op glob (never the decompile — [coverage-tally §0 GOTCHA](../core/coverage-tally.md)).
Every one of the 24 grounds to ≥ 9 placements; **none ungrounded**.

| sub-family | mnemonics | placements | placements/op | slot reach |
|---|--:|--:|--:|---|
| `slli` / `srli` / `srai` / `rotri` (immediate amount) | 12 | 152 | 12–17 | ALU class (10–15) **+ Mul-class fold** (imm form's narrow budget fits Mul) |
| `slln` / `srln` / `sran` / `rotrn` (variable `vec` amount) | 8 | 72 | 9 | ALU class only (3 `vec` sources cannot fold into the Mul slot) |
| `nsa` / `nsau` (normalize, unary) | 4 | 42 | ~10.5 | ALU class (1-source → also picks up the LdSt-adjacent ALU placements) |
| **TOTAL** | **24** | **266** | — | — |

The exact per-group split, re-tallied this pass: `slli`-group **33** (12+12+9), `srli`-group **43**
(17+17+9), `srai`-group **43** (17+17+9), `rotri`-group **33** (12+12+9); `sll`-var **18** (9+9),
`srl`-var **18**, `sra`-var **18**, `rotr`-var **18**; `nsa` **21** (12+9), `nsau` **21** (12+9). Sum =
`33+43+43+33 + 18·4 + 21·2 = 152 + 72 + 42 = 266`. ✓ The asymmetry — `srli`/`srai` 16/8-bit forms at
**17** placements vs `slli` at **12** — is a recovered fact: the right-shift immediate forms ride a few
extra LdSt-adjacent ALU slots that the left-shift forms do not, mirroring B01's single-source-op LdSt
reach.

**Value leaves: 30.** `nm libfiss-base.so | rg -c 'xdref_(sll|srl|sra|rotr|nsa|nsau)_'` = **30**. They
fold the 24 mnemonics by `(verb × signed/unsigned-amount × dtype × amount-width)`: e.g. `sll` resolves
to `sll_u_8_8_3` (8b imm), `sll_u_16_16_4` (16b imm) / `sll_u_16_16_6`+`sll_s_16_16_6` (16b var) /
`sll_u_32_32_5` (32b imm) / `sll_u_32_32_7`+`sll_s_32_32_7` (32b var); `nsa`/`nsau` resolve to the four
`nsa_{16,32}` / `nsau_{16,32}` count leaves. The `_u`/`_s` axis is the **amount signedness**
(immediate = unsigned, variable = signed-bidirectional), the trailing integer is the **amount bit-width**
(3/4/5 immediate, 6/7 variable). These 30 leaves were each disassembled and the key ones
(`srl`/`sra`/`rotr`/`nsa`/`nsau`, both widths) executed live (§4).

These 266 placements are a strict subset of the **12 569 certified-perfect placements**; they roll into
the `1534 ↔ 12569` pairing (never `12642`), and the boundary families — the `wvec` pack's embedded
de-scaling shift (B10), the `ivp_shfl*` element shuffles (B21), and the `ivp_rorb*` predicate rotates
(B11) — are counted in *their* batches, never here. `[HIGH/OBSERVED]`

> **NOTE — adjacency, deferred by design.** `ivp_shfl2nx8`/`ivp_shfl…i_s0/_s2/_s4`/`ivp_shfln_2x32`
> share the `sh*` prefix and a "move bits around" feel but are **cross-lane element permutes**, owned by
> [B21](b21-select-shuffle.md). `ivp_rorbn`/`ivp_rorb2n`/`ivp_rorbn_2` rotate a *predicate* bit-mask and
> are [B11](b11-vbool-alu.md)'s. `ivp_clsfynxf16`/`ivp_clsfyn_2xf32` (float *classify* — sign/exp/mantissa
> category, not a shift) are the fp ALU's, [B02](b02-vec-alu-fp.md). They are cited so a reader greping
> the `sh`/`ror`/`nsa` neighbourhood knows where they went, not to claim them. `[HIGH/OBSERVED]` on the
> adjacency.

---

## 7. Adversarial self-verification — the five strongest claims

Each re-challenged against the binary this pass; failures fixed.

1. **"24 shift/rotate/normalize mnemonics own 266 placements, none ungrounded."** Re-run:
   `nm libisa-core.so | rg -c 'Opcode_ivp_(slli2nx8|…|nsaunx16)_Slot_…_encode'` over the explicit 24-op
   glob = **266**; per-op loop shows **0 with < 9 placements**. The per-group breakdown sums
   `152 + 72 + 42 = 266`. ✓ `[HIGH/OBSERVED]`
2. **"Logical right zero-fills, arithmetic right sign-fills — distinct opcodes."** Re-challenged by
   executing both leaves on `0x8000`: `srl_u_16_16_4(0x8000,4) = 0x0800` (zero-fill) but
   `sra_u_16_16_4(0x8000,4) = 0xf800` (sign-fill) — opposite top nibbles on identical bits. A full
   **65 536 × 16** differential of `sra_u_16_16_4` against `sext16→shr` returned **0 mismatches**;
   `srl_u_8_8_3` over **256 × 8** vs `(v&0xff)>>a` returned **0 mismatches**. They are different leaves
   (`sra_u_*` sign-extends, `srl_u_*` does not). ✓ `[HIGH/OBSERVED by execution]`
3. **"The variable form over-shift clamps `amt ≥ width → 0`; the immediate form is field-bounded."**
   Re-challenged: `srl_u_16_16_6(0xFFFF, 16) = 0x0000` and `…(0xFFFF, 63) = 0x0000` (the leaf's `cmp
   $0x1f / cmova 0` guard), and a **65 536 × 40** sweep of `srl_u_16_16_6` against
   `0 if a>31 else (v>>(a&31))` returned **0 mismatches**; the immediate `sllinx16,16` is *rejected at
   assembly* (imm4 ≤ 15). A bare host shift would wrap the count mod register width and give the wrong
   non-zero result. ✓ `[HIGH/OBSERVED by execution]`
4. **"The variable shift is bidirectional — a negative per-lane amount reverses direction."**
   Re-challenged: `sll_s_16_16_6(0x00F0, −4) = 0x000F` (a *left* opcode shifting *right*) and
   `srl_s_16_16_6(0x0F00, −4) = 0xF000` (a *right* opcode shifting *left*); the leaf's `test $0x20,%dl
   ; neg ; and $0x3f` is the sign dispatch. The immediate forms have no sign bit (only `_u` 3/4/5-bit
   leaves) and are unidirectional. ✓ `[HIGH/OBSERVED by execution]`
5. **"`nsa` is a unary op emitting a count to `vec`; `nsa` = leading-sign−1, `nsau` = leading-zero;
   `bsr(0)` is guarded."** Re-challenged: a **65 536**-input differential of both `nsa_16_16`
   (vs `15 − bsr(2·x′+1)`) and `nsau_16_16` (vs `16 if v==0 else 15−bsr(v)`) returned **0 mismatches**
   each; `nsa16(0x0001)=14`, `nsau16(0x0001)=15` (the guard-bit difference); `nsau16(0)=16` and
   `nsa16(0)=15` (the zero special-cases). The device assembler rejects `ivp_nsanx16 vb0,v1` (dest is
   `vec`, not `vbool`) and accepts only two operands (unary). ✓ `[HIGH/OBSERVED by execution]`

**Ungrounded / flagged items** (honest residue): (a) the `opc#`/`iclass#` row indices are **not**
tabulated per row here — the opcodes-table name pointers did not resolve under the
`.data.rel.ro − 0x200000` base on this pass (the table appears to reference the rodata name strings
indirectly), so the roster cites the **iclass *name*** (`ivp_sem_vec_shift`) and the `nm`-counted
placement total (the hard OBSERVED anchor) rather than a numeric `iclass#`; this is `[MED/INFERRED]` on
the exact row numbers, `[HIGH/OBSERVED]` on the iclass name and the placement counts. (b) The
`vec_shift_vr`/`vs` role-field *scatter* (a low bit OR-ed with a 4-bit run) is OBSERVED in disasm but
its exact composition beyond the clean `vt = [14:10]` window is `[MED/INFERRED]`. (c) The **stage-11
retirement** latency is `[HIGH/OBSERVED]` from the `libcas-core.so` `IVP_*_inst_stage{0..11}` symbol
population (the deepest stage symbol for every shift op is `stage11`), but the full cycle-accurate
interlock (issue/stall timing) is license-walled for *retirement* accuracy. None is a missing decode or
a missing value semantics.

---

## 8. Function & symbol map

`libisa-core.so` `.text`: VMA == file. `libfiss-base.so` leaves: VMA == file (`.text`).

| Symbol / address | Role |
|---|---|
| `Opcode_ivp_sllinx16_Slot_f0_s3_alu_encode` @ `0x343060` | imm-form selector `0x868c8200`, `word1≡0` |
| `Opcode_ivp_sllnx16_Slot_f0_s3_alu_encode` @ `0x343400` | var-form selector `0x86ab8000` |
| `Opcode_ivp_nsanx16_Slot_f0_s3_alu_encode` @ `0x344320` | normalize selector `0x808f8304` |
| `Field_fld_ivp_sem_vec_shift_vt_Slot_f0_s3_alu_get` @ `0x32e980` | source `vt` = bits `[14:10]` |
| `Field_fld_ivp_sem_vec_shift_vs_Slot_f0_s3_alu_get` @ `0x335640` | amount-source `vs` (scattered 5-bit) |
| `Field_fld_ivp_sem_vec_alu_i_imm4_Slot_f0_s3_alu_get` @ `0x32ae70` | 4-bit imm amount (`imm3`@`0x32a9f0`, `imm5`@`0x32b2f0`) |
| `module__xdref_srl_u_16_16_4` @ `0x858080` (`libfiss-base`) | logical-right (zero-fill) value leaf |
| `module__xdref_sra_u_16_16_4` @ `0x858060` (`libfiss-base`) | arithmetic-right (sign-fill) value leaf |
| `module__xdref_sll_u_16_16_6` @ `0x8580e0` (`libfiss-base`) | variable left, over-shift→0 guard |
| `module__xdref_sra_s_16_16_6` @ `0x858210` (`libfiss-base`) | bidirectional signed-amount shift |
| `module__xdref_rotr_u_16_16_4` @ `0x5c0bf0` (`libfiss-base`) | rotate-right value leaf |
| `module__xdref_nsa_16_16` / `nsau_16_16` @ `0x858570` / `0x8585a0` | leading-sign−1 / leading-zero count |
| `module__xdref_nsa_32_32` / `nsau_32_32` @ `0x5c0c80` / `0x5c0ca0` | 32-bit normalize counts |
| `module__xdref_shiftamtsat_16` / `_32` @ `0x858090` / `0x5c00a0` | shift-amount saturation helper |
| `IVP_NSANX16_inst_stage{0..11}` (`libcas-core`) | per-stage timing model (retire = stage 11) |
| `xtensa-elf-objdump` | device round-trip oracle (`XTENSA_CORE=ncore2gp`) |

---

## 9. Cross-references

* [The FLIX VLIW Encoding](../core/flix-encoding.md) — the 14-format/46-slot grid these ops issue on,
  the encode-thunk ABI, and the §6.2 two-tier selector model the §3.3 fact invokes.
* [The Eight Register Files](../core/register-files.md) — `vec` (datapath / amount source / count sink),
  the operand short-name (`v`), and the `@10`/`@11` ALU stage model the latency row cites.
* [ISA Coverage & the 1534/12569 Tally](../core/coverage-tally.md) — the certified-perfect denominator
  this batch's 266/12569 is a subset of, and the `nm`-not-decompile counting rule.
* [ISA Reference — Template & 30-Batch Partition](template-and-partition.md) — the schema this page
  follows and the B12 partition row (`(sll|srl|sra|nsa|rot|norm|sh) → B12`, with the `sh`/`shfl` trap).
* [B01 — Vector ALU (int / compare / logic core)](b01-vec-alu-int.md) — the integer ALU core sharing the
  ALU slot class, the `vec` datapath, and the `0x80870000`-class special selector base.
* [B10 — wvec Pack (wide→narrow readout)](b10-wvec-pack.md) — the **de-scaling rounding right-shift
  folded into a pack opcode** (a pack operand, not a standalone shift) — the B12/B10 boundary.
* [B11 — vbool ALU / predicate](b11-vbool-alu.md) — the `ivp_rorb*` predicate-mask rotates (predicate
  file, not `vec`) — the B12/B11 boundary.
* [B21 — Select / Shuffle / Compress](b21-select-shuffle.md) — the `ivp_shfl*` cross-lane element
  permutes (the `sh` classifier trap) — the B12/B21 boundary.
* [B25 — base-Xtensa scalar arith / logic / shift](b25-xt-core.md) — the scalar `AR`/`SAR`
  `sll`/`src`/`ssl` shift ISA (no `ivp_` prefix) — the vector/scalar shift boundary.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags, the license-walled
  cycle oracle, and the proven-by-execution value lane.

---

*Provenance: encoding (`Opcode_*`/`Field_*` thunks, selector imms, `imm3/4/5` amount fields), placement
counts, and operand grammar are `[HIGH/OBSERVED]` — disassembled / `nm`-counted in-checkout from
`libisa-core.so` (`ncore2gp/config`, not stripped, sha256 `8fe68b…`, `.data.rel.ro − 0x200000`). Lane
value semantics are `[HIGH/OBSERVED by execution]` — the `module__xdref_*` shift/rotate/normalize leaves
in `libfiss-base.so` were called live via ctypes (license-free value lane) over full input domains
(srl 65 536×40, sra 65 536×16, rotr/srl 256×8, nsa/nsau 65 536 each — **0 mismatches**). The byte
round-trip is `[HIGH/OBSERVED]` from the device-native `xtensa-elf-as`/`objdump` (`XTENSA_CORE=ncore2gp`).
Stage-11 retirement latency is `[HIGH/OBSERVED]` from `libcas-core.so` per-instruction stage symbols;
cycle-accurate interlock is license-walled. All facts read as derived from shipped-artifact static
analysis and in-process execution of license-free leaves (lawful interoperability RE).*
