# ISA Batch 03 — Vector ALU (integer compare · B-variant flag · predicated/merge)

This is the per-instruction reference for the **integer Vector-ALU forms whose datapath
touches a predicate file** — the complement of [B01](b01-vec-alu-int.md) (the int-ALU *core*:
plain `add`/`sub`/`max`/`min`/`abs`/logic that read and write **only** `vec`) and
[B02](b02-vec-alu-fp.md) (the fp16/fp32 slice). Concretely, B03 owns exactly three sub-families
of the integer ALU, defined by *which extra register file the op names*:

1. **Integer compares** (`ivp_{eq,neq,le,leu,lt,ltu}{2nx8,nx16,n_2x32}`) — read two `vec`
   operands, **write a `vbool` predicate** (the canonical "B-variant: write a flag from a
   comparison"). 18 mnemonics.
2. **B-variant flag-producing ALU** (`ivp_b*` — `bmax/bmin/babs/babssub/baddnorm/bsubnorm/…`) —
   a base ALU op that **additionally writes a `vbool` flag** recording the comparison/carry that
   the op resolved. 22 mnemonics.
3. **Predicated / conditional-merge ALU** (the integer `*t` throttle forms —
   `ivp_{add,sub,adds,subs,neg,negs,max,maxu,min,minu}…t`) — a base ALU op that **reads a
   `vbool` mask** and merges per-lane: `out[l] = mask[l] ? alu(a,b) : out_old[l]`. 14 mnemonics.

**54 mnemonics / 889 placements** of the 1534/12569 certified-perfect cover
([coverage-tally](../core/coverage-tally.md)). The encoding is read out of the non-stripped
`libisa-core.so` (`Opcode_*_encode` selector templates, the `ivp_sem_vec_alu_vbr` field thunk);
the **value semantics are proven by execution** by driving the matching `module__xdref_*` leaves
in `libfiss-base.so` live via `ctypes` (license-free value lane). Every numeric claim is anchored
to a binary address or a live call and tagged `[HIGH/OBSERVED]` unless flagged. The prose is
derived from static analysis of the shipped artifacts only.

> **The B01/B02/B03 partition boundary (read this first).** The three integer-ALU batches
> partition the integer datapath by **register-file footprint**, with **zero mnemonic overlap**:
>
> | batch | reads | writes | examples |
> |---|---|---|---|
> | **B01** int core | `vec` | `vec` | `ivp_addnx16`, `ivp_subnx16`, `ivp_maxnx16`, `ivp_absnx16`, `ivp_andnx16` |
> | **B02** fp slice | `vec` (fp16/fp32) | `vec`/`vbool` | `ivp_addnxf16`, `ivp_oltn_2xf32`, the `*xf16`/`*_2xf32` ops incl. their `t`-forms |
> | **B03** (this page) | `vec` (+ `vbool` for `*t`) | `vbool` (compares/B-variant) or `vec`-merge (`*t`) | `ivp_eqnx16`, `ivp_bmaxnx16`, `ivp_addnx16t` |
>
> The base op `ivp_addnx16` is **B01**; its predicated form `ivp_addnx16t` is **B03** — they are
> distinct opcodes (distinct `opcodes[]` rows, distinct selector words, §3). The fp compares
> `ivp_oltn_2xf32`/`ivp_oeqnxf16` (which *also* write `vbool`) are **B02**, not B03, because their
> inputs are fp — B03 is integer-input only. Reduce-and-flag ops (`ivp_radd…t`, `ivp_rmax…t`,
> `ivp_ltrn`, `ivp_ltrsn`) are **B08**; vbool→vbool boolean logic and the vbool-folding
> `ivp_bnorfs2n`/`ivp_borfs2n` are **B11**; `sel`/`dsel`/shuffle are **B21**; `rep` is **B16**;
> scatter/gather `*t` is **B19**. The explicit family-prefix assignment is the partition table in
> §7.

---

## 1. Batch facts

| Fact | Value | Binary witness |
|---|---|---|
| Mnemonics in batch | **54** (18 compare + 22 B-variant + 14 predicated-t) | `nm \| rg` rosters, §2 |
| Placements in batch | **889** (378 + 321 + 190) | per-mnemonic `Opcode_*_Slot_*_encode` counts |
| Result file (compares / B-variant) | `vbool` idx 3, 16×64-bit | [register-files §3](../core/register-files.md) |
| Mask file (predicated-t input) | `vbool` idx 3 | same |
| `vbool` operand field (`vec_alu_vbr`) | slot bits **[18:15]**, 4-bit (16 regs) | `Field_fld_ivp_sem_vec_alu_vbr_*` @ `0x32b710` |
| Predicate width per lane | **1 bit / 8-bit lane**, 2 / 16-bit, 4 / 32-bit | live `eq_{1,2,4}_{8,16,32}` |
| FLIX slots used | `alu` (`f*_s3/s4_alu`, `n0_s3_alu`), `mul` (compares co-issue here too), `ldst`/`ldstalu` (a few) | placement census |
| Value leaves driven live | `eq/neq/lt/ltu/le/leu_{1,2,4}_*`, `bmax/bmin/bmaxu/bminu`, `baddnorm/bsubnorm`, `babs`, `bxorabssubu`, `bitkillt/bitkillf`, `add_*` | §4–§5 |
| Confidence | `[HIGH/OBSERVED]` (encoding + execution) | — |

The batch is a **pure subset** of `xt_ivp32` (the 1072-op vector package). It draws on the FLIX
grid ([flix-encoding](../core/flix-encoding.md): 14 formats / 46 slots, classes →
`{2,3,8,16}`) and the `vbool`/`b32_pr` files
([register-files](../core/register-files.md): `vbool` idx3 64b×16, `b32_pr` idx6 64b×16).

---

## 2. Roster

`FLIX format·slot` lists the canonical placement used for the selector column; **most ops carry
15–21 placements** across the wide/narrow ALU and `mul` slots (the per-mnemonic count is in the
last cell). `opcode-sel imm` is `word0` of the `Opcode_<mn>_Slot_f0_s3_alu_encode` template
(`C7 07 imm32` — [flix-encoding §6.1](../core/flix-encoding.md)); for ops not legal in
`f0_s3_alu` the first available placement is named. `vbr field` = the `vbool` operand at slot
bits [18:15]; its **direction** is the discriminator: `out` (compare/B-variant writes the flag),
`in` (predicated-t reads the mask). Every row of §2.1–§2.3 is `[HIGH/OBSERVED]`.

### 2.1 Integer compares — write a `vbool` predicate (18 mnemonics, 378 placements)

| mnemonic | format·slot | opcode-sel `word0` | vec / vbr | lanes | one-line semantics | n |
|---|---|---|---|---|---|--:|
| `ivp_eq2nx8`   | F0·S3_ALU | `0x80870000` | a,b∈vec → vb(out) | 64×8b | `vb[l] = (a==b)` (1-bit/lane) | 21 |
| `ivp_eqnx16`   | F0·S3_ALU | `0x80870002` | a,b → vb | 32×16b | `vb[l] = (a==b)` (2-bit/lane) | 21 |
| `ivp_eqn_2x32` | F0·S3_ALU | `0x80870004` | a,b → vb | 16×32b | `vb[l] = (a==b)` (4-bit/lane) | 21 |
| `ivp_le2nx8`   | F0·S3_ALU | `0x80870006` | a,b → vb | 64×8b | `vb[l] = (a≤b)` signed | 21 |
| `ivp_lenx16`   | F0·S3_ALU | `0x80870008` | a,b → vb | 32×16b | `vb[l] = (a≤b)` signed | 21 |
| `ivp_len_2x32` | F0·S3_ALU | `0x8087000a` | a,b → vb | 16×32b | `vb[l] = (a≤b)` signed | 21 |
| `ivp_leu2nx8`  | F0·S3_ALU | `0x8087000c` | a,b → vb | 64×8b | `vb[l] = (a≤b)` unsigned | 21 |
| `ivp_leunx16`  | F0·S3_ALU | `0x8087000e` | a,b → vb | 32×16b | `vb[l] = (a≤b)` unsigned | 21 |
| `ivp_leun_2x32`| F0·S3_ALU | `0x80870100` | a,b → vb | 16×32b | `vb[l] = (a≤b)` unsigned | 21 |
| `ivp_lt2nx8`   | F0·S3_ALU | `0x80870102` | a,b → vb | 64×8b | `vb[l] = (a<b)` signed | 21 |
| `ivp_ltnx16`   | F0·S3_ALU | `0x80870104` | a,b → vb | 32×16b | `vb[l] = (a<b)` signed | 21 |
| `ivp_ltn_2x32` | F0·S3_ALU | `0x80870106` | a,b → vb | 16×32b | `vb[l] = (a<b)` signed | 21 |
| `ivp_ltu2nx8`  | F0·S3_ALU | `0x80870108` | a,b → vb | 64×8b | `vb[l] = (a<b)` unsigned | 21 |
| `ivp_ltunx16`  | F0·S3_ALU | `0x8087010a` | a,b → vb | 32×16b | `vb[l] = (a<b)` unsigned | 21 |
| `ivp_ltun_2x32`| F0·S3_ALU | `0x8087010c` | a,b → vb | 16×32b | `vb[l] = (a<b)` unsigned | 21 |
| `ivp_neq2nx8`  | F0·S3_ALU | `0x8087010e` | a,b → vb | 64×8b | `vb[l] = (a≠b)` | 21 |
| `ivp_neqnx16`  | F0·S3_ALU | `0x80870200` | a,b → vb | 32×16b | `vb[l] = (a≠b)` | 21 |
| `ivp_neqn_2x32`| F0·S3_ALU | `0x80870202` | a,b → vb | 16×32b | `vb[l] = (a≠b)` | 21 |

All 18 share the iclass prefix `0x80870000` (read byte-exact from the F0·S3_ALU templates; the
low 16 bits are the enumerated `(predicate, lane-width)` selector, §3.1). There is **no
`gt`/`ge`** opcode — greater-than is the assembler swapping operands into `lt`/`le` (a B01/B02
convention; B03 ships the canonical 6-predicate basis × 3 widths). Each compare is legal in **21**
slots (every ALU slot + every Mul slot + `n2_s0_ldst`); the per-slot selector differs (the
selector is format-local, §3.2) but the value semantics are slot-invariant.

### 2.2 B-variant flag-producing ALU — ALU result **plus** a `vbool` flag (22 mnemonics, 321 placements)

| mnemonic | format·slot | opcode-sel `word0` | vec(out)·vbr(out) | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_bmaxnx16`     | F0·S3_ALU | `0x67070000` | vt·vb | `vt=max_s(a,b); vb=(a>b)` 16b | 15 |
| `ivp_bmax2nx8`     | (mul/alu) | `0x67068000` | vt·vb | `vt=max_s(a,b); vb=(a>b)` 8b | 15 |
| `ivp_bmaxn_2x32`   | (mul/alu) | `0x67078000` | vt·vb | `vt=max_s(a,b); vb=(a>b)` 32b | 15 |
| `ivp_bmaxunx16`    | F0·S3_ALU | `0x80808000` | vt·vb | `vt=max_u(a,b); vb=(a>b)u` 16b | 15 |
| `ivp_bmaxu2nx8`    | (mul/alu) | — | vt·vb | unsigned bmax 8b | 15 |
| `ivp_bmaxun_2x32`  | (mul/alu) | — | vt·vb | unsigned bmax 32b | 15 |
| `ivp_bminnx16`     | F0·S3_ALU | `0x80820000` | vt·vb | `vt=min_s(a,b); vb=(a<b)` 16b | 15 |
| `ivp_bmin2nx8`     | (mul/alu) | — | vt·vb | signed bmin 8b | 15 |
| `ivp_bminn_2x32`   | (mul/alu) | — | vt·vb | signed bmin 32b | 15 |
| `ivp_bminunx16`    | F0·S3_ALU | `0x80838000` | vt·vb | `vt=min_u(a,b); vb=(a<b)u` 16b | 15 |
| `ivp_bminu2nx8`    | (mul/alu) | — | vt·vb | unsigned bmin 8b | 15 |
| `ivp_bminun_2x32`  | (mul/alu) | — | vt·vb | unsigned bmin 32b | 15 |
| `ivp_babsnx16`     | F0·S3_ALU | `0x64805400` | vt·vb | `vt=|a|; vb=(a<0)` 16b | 17 |
| `ivp_babs2nx8`     | (mul/alu) | — | vt·vb | `vt=|a|; vb=(a<0)` 8b | 17 |
| `ivp_babsn_2x32`   | (mul/alu) | — | vt·vb | `vt=|a|; vb=(a<0)` 32b | 17 |
| `ivp_babssubnx16`  | F0·S3_ALU | `0x67048000` | vt·vb | `vt=|a−b|; vb=(a<b)` signed 16b | 15 |
| `ivp_babssub2nx8`  | (mul/alu) | — | vt·vb | `vt=|a−b|; vb` signed 8b | 15 |
| `ivp_babssubunx16` | F0·S3_ALU | `0x67058000` | vt·vb | `vt=|a−b|; vb=(a<b)u` unsigned 16b | 15 |
| `ivp_babssubu2nx8` | (mul/alu) | — | vt·vb | `vt=|a−b|u; vb` unsigned 8b | 15 |
| `ivp_baddnormnx16` | F0·S3_ALU | `0x67060000` | vt·vb | `vt=norm(a+b); vb=overflow` 16b | 15 |
| `ivp_bsubnormnx16` | F0·S3_ALU | `0x80848000` | vt·vb | `vt=norm(a−b); vb=overflow` 16b | 15 |
| `ivp_bxorabssubu2nx8` | F0·S3_ALU | — | vt·vb (vb in+out) | `vt=|a−b|u; vb=pin⊕(a<b)u` 8b | (alu/mul) |

The `b`-prefix marks **"+flag" variants** of B01 base ops: same datapath result, **plus** a
`vbool` flag carrying the comparison/carry the op resolved (which operand won the max/min, the
sign for abs, the overflow for the norm forms). `ivp_bxorabssubu2nx8` is the one fused form whose
flag is also an **input** (a predicate accumulation chain, §5.3).

### 2.3 Predicated / conditional-merge ALU — read a `vbool` mask, merge per-lane (14 mnemonics, 190 placements)

| mnemonic | format·slot | opcode-sel `word0` | vec(inout)·vbr(in) | one-line semantics | n |
|---|---|---|---|---|--:|
| `ivp_addnx16t`  | F0·S3_ALU | `0x66f80000` | vt(m)·vb(in) | `vt[l]= m? a+b : vt[l]` 16b | 13 |
| `ivp_add2nx8t`  | (alu)     | — | vt(m)·vb | predicated add 8b | 13 |
| `ivp_addn_2x32t`| (alu)     | — | vt(m)·vb | predicated add 32b | 13 |
| `ivp_addsnx16t` | F0·S3_ALU | `0x67880000` | vt(m)·vb | predicated **saturating** add 16b | 13 |
| `ivp_subnx16t`  | F0·S3_ALU | `0x80500000` | vt(m)·vb | `vt[l]= m? a−b : vt[l]` 16b | 13 |
| `ivp_sub2nx8t`  | (alu)     | — | vt(m)·vb | predicated sub 8b | 13 |
| `ivp_subn_2x32t`| (alu)     | — | vt(m)·vb | predicated sub 32b | 13 |
| `ivp_subsnx16t` | F0·S3_ALU | `0x80600000` | vt(m)·vb | predicated saturating sub 16b | 13 |
| `ivp_maxnx16t`  | F0·S3_ALU | `0x67a00000` | vt(m)·vb | `vt[l]= m? max_s(a,b) : vt[l]` | 13 |
| `ivp_maxunx16t` | F0·S3_ALU | `0x67b80000` | vt(m)·vb | predicated unsigned max | 13 |
| `ivp_minnx16t`  | F0·S3_ALU | `0x67d00000` | vt(m)·vb | predicated signed min | 13 |
| `ivp_minunx16t` | F0·S3_ALU | `0x67f80000` | vt(m)·vb | predicated unsigned min | 13 |
| `ivp_negnx16t`  | F0·S3_ALU | `0x64105000` | vt(m)·vb | `vt[l]= m? −a : vt[l]` | 17 |
| `ivp_negsnx16t` | F0·S3_ALU | `0x64105c00` | vt(m)·vb | predicated saturating negate | 17 |

The `t` suffix ("throttle") is the **per-lane write-enable** form: the destination is marked
**`m`** (inout) in the operand table because un-predicated lanes keep their prior value (§3.3,
§5.1). The full `t`-family across the ISA is 140 ops; B03 owns **only the 14 integer-ALU-base
ones** above. The fp-base (`*xf16t`/`*_2xf32t`), reduce-base (`r*t`), gather/scatter (`*t`),
`rep`, `mov`, `sel`/`dsel`, and convert `t`-forms belong to other batches (§7).

---

## 3. Encoding

### 3.1 The compare selector — an enumerated `(predicate × width)` field on a shared iclass

All 18 integer compares share `word0[31:16] == 0x8087` on the F0·S3_ALU template; the **low 16
bits** are the `(predicate, lane-width)` selector. Read byte-exact from the encode thunks
(`Opcode_ivp_<cmp>_Slot_f0_s3_alu_encode`, addresses `0x344…`/`0x359…`/`0x35b…`):

```
eq2nx8  0x0000   le2nx8  0x0006   leu2nx8  0x000c   lt2nx8  0x0102   ltu2nx8  0x0108   neq2nx8   0x010e
eqnx16  0x0002   lenx16  0x0008   leunx16  0x000e   ltnx16  0x0104   ltunx16  0x010a   neqnx16   0x0200
eqn2x32 0x0004   len2x32 0x000a   leun2x32 0x0100   ltn2x32 0x0106   ltun2x32 0x010c   neqn2x32  0x0202
```

> **GOTCHA — the selector is *enumerated*, not a flat linear counter.** It is tempting to read the
> low 16 bits as `index×2` (it *is* `index×2` within a byte: `…0,2,4,6,8,a,c,e`), but the field
> **carries** across the byte boundary: after `0x0e` the next value is `0x0100`, not `0x0010`. The
> real shape is a structured `(group∈{0,1,2}, sub∈{0..7})` enum: `sel = (group<<8) | (sub<<1)`. A
> reimplementer must look the selector up per-opcode from the `opcodes[]`/template table, **not**
> compute it from a single counter, or the `le→lt→neq` wrap mis-encodes. The per-op selector word
> above is the ground truth.

The width-suffix (`2nx8`/`nx16`/`n_2x32`) interleaves with the predicate as the **fast** axis
(width-minor, predicate-major in display) — the binary orders them width-minor inside each
predicate group. Lane-width selects the element size (8/16/32-bit) of the two `vec` source reads
**and** the produced predicate width (§5.2).

### 3.2 B-variant and predicated-t selectors are distinct opcode words

The `b`-prefix and `t`-suffix forms are **separate opcodes** (separate `opcodes[]` rows, separate
iclasses), not a bit-flip of the base. The F0·S3_ALU `word0` templates make this concrete:

| base (B01) | sel | B-variant (B03) | sel | predicated-t (B03) | sel |
|---|---|---|---|---|---|
| `ivp_addnx16` | `0x80b50000` | `ivp_baddnormnx16` | `0x67060000` | `ivp_addnx16t` | `0x66f80000` |
| `ivp_subnx16` | `0x86b90000` | `ivp_bsubnormnx16` | `0x80848000` | `ivp_subnx16t` | `0x80500000` |
| `ivp_maxnx16` | `0x80ad8000` | `ivp_bmaxnx16`     | `0x67070000` | `ivp_maxnx16t` | `0x67a00000` |
| `ivp_minnx16` | `0x80fd8000` | `ivp_bminnx16`     | `0x80820000` | `ivp_minnx16t` | `0x67d00000` |
| `ivp_negnx16` | `0x6488d800` | (n/a — abs is `babs`) | — | `ivp_negnx16t` | `0x64105000` |

This is the two-tier selector model of [flix-encoding §6.2](../core/flix-encoding.md) in
action: the predicate-touching variant is a **format-local distinct opcode**, not a global "B-bit"
or "T-bit". The `t`-forms cluster in the `0x66/0x67` high-byte band and the bare-`b` variants
straddle `0x67`/`0x80`; there is **no** single XOR that turns `add` → `addt` across all formats
(it drifts per format — confirmed against the §3 mask discipline). Encode by table lookup.

### 3.3 The `vbool` operand — `ivp_sem_vec_alu_vbr`, slot bits [18:15]

The extra `vbool` operand (the flag for compares/B-variants, the mask for predicated-t) is
deposited/extracted by the `Field_fld_ivp_sem_vec_alu_vbr_Slot_<slot>_{get,set}` thunks. The
*get* body is identical across all 20 ALU/Mul/LdSt slots that carry it (disassembled at
`0x32b710` for F0·S3_ALU, `0x32ba30` for N0·S3_ALU, `0x32b7d0` for F1·S3_ALU — all byte-equal):

```c
// Field_fld_ivp_sem_vec_alu_vbr_Slot_f0_s3_alu_get @ 0x32b710
uint32_t vec_alu_vbr_get(uint32_t slotbuf) {
    return (slotbuf << 13) >> 28;          // isolate bits [18:15] -> 4-bit vbool index (0..15)
}
// _set @ 0x32b720 :  (slotbuf & 0xfff87fff) | ((vbr & 0xf) << 15)
```

So the `vbool` register operand is a **4-bit field at slot positions [18:15]** naming one of the
16 `vbool` registers — uniform across every B03 slot. The direction (`in` vs `out`) is *not* in
this field; it is in the per-opcode operand-descriptor table (§3.4), which is what splits compares
/ B-variants (write) from predicated-t (read).

### 3.4 Operand count and direction — the byte that defines the sub-family

The `proto_TIE_xt_ivp32_<OP>_insn_num_args` immediate and the `_operands` descriptor array
(`.data.rel.ro`, file = VMA − `0x200000`) pin the operand shape byte-exact:

| opcode | `num_args` | operand directions (descriptor +0x0f byte) | meaning |
|---|--:|---|---|
| `IVP_ADDNX16` (B01) | **3** | `o, i, i` (`0x6f,0x69,0x69`) | `vt=out, vr=in, vs=in` |
| `IVP_ADDNX16T` (B03) | **4** | `m, i, i, i` (`0x6d,0x69,…`) | `vt=**inout(merge)**, vr,vs,vb=in` |
| `IVP_BMAXNX16` (B03) | **4** | `o, i, i, o` (`0x6f,0x69,0x69,0x6f`) | `vt=out **data**, vr,vs=in, vb=out **flag**` |

The discriminating bytes (read from `IVP_ADDNX16T_operands` @ `0x82c880`,
`IVP_BMAXNX16_operands` @ `0x82e880`):

* **Predicated-t** → destination direction is **`0x6d` ('m', modify/inout)**. The hardware reads
  the old destination so un-predicated lanes survive (the merge). The 4th operand is an **`in`**
  `vbool` mask.
* **B-variant** → there are **two `0x6f` ('o', out)** operands: the data result *and* a second
  `vbool` flag output.
* **Base (B01)** → a single `0x6f` data out, no `vbool` operand at all.

This is the cleanest byte-level proof of the B01/B02/B03 boundary: the **arg count and the
direction byte** distinguish the three classes mechanically. `[HIGH/OBSERVED]`

---

## 4. Per-lane semantics — annotated C

The three required pseudocode shapes — (a) the predicate test that gates a conditional/merge op,
(b) how a B-variant writes a flag from a comparison, (c) the carry/overflow-producing add/sub —
each annotated against the live-driven `module__xdref_*` leaf.

### 4.1 (b) B-variant flag write from a comparison — `ivp_bmaxnx16`

The leaf `module__xdref_bmax_2_16_16_16` (`0x85a870`) takes two 16-bit lanes and writes **both** a
2-bit predicate and the 16-bit max. Disassembled body, annotated:

```c
// module__xdref_bmax_2_16_16_16  @ 0x85a870   (ABI: rdi=ctx unused, rsi=a, rdx=b, rcx=pred_out, r8=data_out)
void bmax_lane_s16(int16_t a, int16_t b, uint16_t *pred_out, int16_t *data_out) {
    // signed compare via sign-bit-flip bias (a' = a ^ 0x8000), then unsigned 'seta'
    uint16_t au = (uint16_t)a ^ 0x8000;          // 85a870: shr/test/sete on bit15, and 0x7fff, or
    uint16_t bu = (uint16_t)b ^ 0x8000;
    int a_wins = (au >  bu);                      // 85a8a6: cmp ; 85a8a9: seta %dil  (strictly greater)
    *pred_out  = a_wins ? 0x3 : 0x0;              // 85a8b1: neg ; and $0x3   -> 2-bit lane mask
    *data_out  = a_wins ? a : b;                  // 85a8b9: cmove %edx,%esi  (keep a, else b)
}
```

The predicate is the **comparison the max resolved**: `vb[l] = (a > b)` — *did operand a win?* The
unsigned form `bmaxu` skips the sign-bit bias and uses a direct `cmp`. `bmin` flips the
predicate polarity (`setb` instead of `seta`) and selects the smaller. **Live-validated** (§5.2):
`bmax(7,5)→(pred=0x3,data=7)`, `bmax(5,7)→(pred=0x0,data=7)`.

### 4.2 (a) The per-lane predicate test that gates a conditional / merge ALU op — `ivp_addnx16t`

The predication is a **two-part composition**: the base value leaf computes `alu(a,b)`
unconditionally, and a `bitkill` kill-mask leaf turns the per-lane predicate into a 16-bit
write-enable mask; the ISS merges. The kill-mask leaf body (`module__xdref_bitkillf_16_2` @
`0x82d000`):

```c
// module__xdref_bitkillf_16_2  @ 0x82d000      (rsi = predicate bit, rdx = mask_out)
uint16_t bitkillf_16(uint32_t pred) {            // 'f' polarity: keep when pred TRUE
    return (uint16_t)((int32_t)(pred << 31) >> 31);   // 82d000: shl $31 ; sar $31  -> 0xFFFF if bit0 else 0x0000
}
// module__xdref_bitkillt_16_2  @ 0x85cc20      (opposite: keep when pred FALSE)
//   and $1; xor $1; neg; and 0xffff   ->  pred? 0x0000 : 0xFFFF

// The predicated-t op, reconstructed:  out[l] = mask[l] ? (a[l]+b[l]) : out_old[l]
void addnx16t_lane(int16_t a, int16_t b, uint16_t pred, int16_t *out /*inout 'm'*/) {
    int16_t  s   = (int16_t)(a + b);             // base add_16_16_16 (unconditional)
    uint16_t keep = bitkillf_16(pred);           // pred=1 -> 0xFFFF (write), pred=0 -> 0x0000 (hold)
    *out = (uint16_t)((s & keep) | (*out & ~keep));   // merge: predicated lanes update, others survive
}
```

The destination is **inout** (`0x6d`, §3.4) precisely because the `& ~keep` term reads the old
value. **Live-validated** (§5.1): a 4-lane merge with `mask = [1,0,1,0]` updates lanes 0,2 to
`a+b` and leaves lanes 1,3 at their old `0xBB`/`0xDD`.

### 4.3 (c) Carry / overflow-producing add — `ivp_baddnormnx16`

`baddnorm` is the carry-style variant: it adds in 17-bit precision, detects signed overflow by
comparing **bit15 (result sign) against bit16 (true carry-out)**, sets the `vbool` flag, and
**normalizes** the overflowed result back into 16-bit range by an arithmetic `>>1`. Leaf
`module__xdref_baddnorm_2_16_16_16` (`0x8148e0`), annotated:

```c
// module__xdref_baddnorm_2_16_16_16  @ 0x8148e0   (rsi=a, rdx=b, rcx=pred_out, r8=data_out)
void baddnorm_lane(int16_t a, int16_t b, uint16_t *pred_out, int16_t *data_out) {
    int32_t  t   = (int32_t)a + (int32_t)b;       // 8148e6: add (sign-extended 17-bit sum)
    uint32_t u   = (uint32_t)t & 0x1FFFF;         // keep 17 bits
    int sign16   = (u >> 15) & 1;                 // bit15  (would-be 16-bit sign)
    int carry17  = (u >> 16) & 1;                 // bit16  (true carry-out)
    int overflow = (sign16 != carry17);           // 8148ff: cmp ; sete  ->  signed overflow
    *pred_out    = overflow ? 0x3 : 0x0;          // 8148fc..814912: xor/neg/and 0x3 (2-bit lane flag)
    int16_t norm = (int16_t)(u >> 1);             // 814905: shr $1   (normalize: /2)
    *data_out    = overflow ? norm : (int16_t)u;  // 814918: cmove  -> normalized only when overflowed
}
```

`bsubnorm` is identical with `a − b`. The `vbool` flag is thus a **per-lane carry/overflow
indicator** and the result is the saturate-by-normalize (not clamp) value. **Live-validated**
(§5.3): `baddnorm(20000,20000)→(pred=0x3, data=20000)` (40000 overflowed → `>>1` = 20000);
`baddnorm(30000,5000)→(pred=0x3, data=17500)` (35000>>1); `baddnorm(100,200)→(pred=0x0,
data=300)` (no overflow, no normalize).

> **NOTE — there is no general add-with-carry-in chain in the integer Vector-ALU.** The Q7 integer
> ALU exposes the carry/overflow as a **`vbool` flag output** (`baddnorm`/`bsubnorm`) and as the
> **selecting predicate** of the B-variant min/max — *not* as a consumed carry-in register that
> threads a multi-word addition. The one op that **consumes** an incoming predicate is the fused
> `ivp_bxorabssubu2nx8` (§5.4), whose `vbool` operand is both `in` and `out` (a predicate
> XOR-accumulation, not an arithmetic carry). Wide/multi-precision arithmetic is instead done in
> the `wvec` accumulator path (B04/B05 MAC), not by carry-chaining these 16-bit ALU ops. No
> `*carry*`/`*cin*`/`*cout*` mnemonic exists: `nm | rg -i 'carry|cin|cout'` = 0.

---

## 5. Driven live — `libfiss-base.so` value leaves via `ctypes`

`libfiss-base.so` (sha256 `260b110c…`, 864 `module__xdref_*` value leaves) is callable in-process
with no license. The per-element leaf ABI is **System-V with `rdi` reserved** (a ctx pointer the
scalar leaves ignore): value args begin in `rsi`, outputs are pointer args. Twelve leaves were
driven live; transcripts below are reproducible with `ctypes.CDLL(...)`.

### 5.1 Predicated-merge — masked vs unmasked lanes (the required merge case)

Composing `add_16_16_16` with the `bitkillf_16_2` keep-mask reproduces `ivp_addnx16t` per-lane;
`a=[10,20,30,40]`, `b=3`, `old=[0xAA,0xBB,0xCC,0xDD]`, `mask=[1,0,1,0]`:

```
 lane  a   b   a+b  mask  killf(keep)  merged(= mask?a+b:old)
  0   10   3    13   1    0xffff       0x000d     <- predicated lane updated
  1   20   3    23   0    0x0000       0x00bb     <- UNmasked lane: old value survives
  2   30   3    33   1    0xffff       0x0021     <- updated
  3   40   3    43   0    0x0000       0x00dd     <- old value survives
```

The unmasked lanes (1, 3) keep `0xBB`/`0xDD` exactly — the inout/merge semantics, proven by
execution.

### 5.2 Compares and B-variant min/max — signed/unsigned divergence + flag

```
== 16-bit compares (out = 2-bit lane mask; 0x3 true / 0x0 false) ==
a=  -1 b=   1 :: eq=0x0 neq=0x3 lt=0x3 ltu=0x0 le=0x3 leu=0x0   <- signed lt TRUE, unsigned ltu FALSE
a=   1 b=  -1 :: eq=0x0 neq=0x3 lt=0x0 ltu=0x3 le=0x0 leu=0x3   <- mirror
a=   5 b=   5 :: eq=0x3 neq=0x0 lt=0x0 ltu=0x0 le=0x3 leu=0x3

== predicate WIDTH scales with lane width (one bit per 8-bit sub-lane) ==
eq_1_8_8(5,5)=0x1   eq_2_16_16(5,5)=0x3   eq_4_32_32(9,9)=0xf      (8/16/32-bit lane -> 1/2/4-bit mask)

== B-variant bmax/bmin (signed) + bmaxu/bminu (unsigned): (pred, data) ==
a=  -1 b=   1 :: bmax(p=0x0,d=   1) bmin(p=0x3,d=  -1) bmaxu(p=0x3,d=0xffff) bminu(p=0x0,d=0x0001)
a=   7 b=   5 :: bmax(p=0x3,d=   7) bmin(p=0x0,d=   5) bmaxu(p=0x3,d=0x0007) bminu(p=0x0,d=0x0005)
```

Signed/unsigned correctly diverge (`a=−1,b=1`: signed `lt`=true but `ltu`=false because
`0xFFFF > 1` unsigned; `bmaxu` picks `0xFFFF`, `bmax` picks `+1`). The B-variant flag is `(a won?)`
in every case. `[HIGH/OBSERVED by execution]`

### 5.3 Carry/overflow-producing `baddnorm`/`bsubnorm` (the required carry-out case)

```
baddnorm( 100,  200): raw_sum=  300  pred=0x0 data=  300   (no overflow -> pass through)
baddnorm(20000,20000): raw_sum=40000  pred=0x3 data=20000   (overflow -> flag set, result >>1)
baddnorm(30000, 5000): raw_sum=35000  pred=0x3 data=17500   (35000 >> 1)
baddnorm(32767,    1): raw_sum=32768  pred=0x3 data=16384   (32768 >> 1)
bsubnorm(20000,-20000):             pred=0x3 data=20000     (40000 difference -> overflow flag)
```

The `vbool` flag is the per-lane carry/overflow; the data is the normalize-on-overflow value.

### 5.4 The fused predicate-chain form `ivp_bxorabssubu2nx8`

`module__xdref_bxorabssubu_1_8_8_8_1` (`0x8329d0`) — data = unsigned `|a−b|`, flag = an **incoming
predicate XOR the borrow** (`a<b` unsigned). The `vbool` operand is both read and written:

```
bxorabssubu(a=10,b= 3,pin=0) -> pred=0x0 data=0x07   (|a-b|=7 ; pin(0) ^ (a<b)=0)
bxorabssubu(a= 3,b=10,pin=0) -> pred=0x1 data=0x07   (pin(0) ^ (a<b)=1)
bxorabssubu(a=10,b= 3,pin=1) -> pred=0x1 data=0x07   (pin(1) ^ 0 = 1)
```

This is the only B03 op that *consumes* a predicate to *produce* a predicate — a fused
abs-difference whose flag accumulates a running boolean (used in sort/argmin kernels). Note it
exposes **no** arithmetic carry-in; the chained quantity is a boolean.

---

## 6. Slot legality and co-issue

* **Compares** are legal in **21** placements each: every wide/narrow ALU slot
  (`f*_s3_alu`, `f3/f11_s4_alu`, `n0_s3_alu`), every Mul slot (`f*_s2_mul`, `n1_s2_mul` — a
  compare can co-issue *as the Mul-slot op*), and `n2_s0_ldst`. They never require an ALU slot
  exclusively, so a bundle can issue two compares (one in ALU, one in Mul).
* **B-variants** carry **15–17** placements — ALU and Mul slots, plus the `abs`-family (`babs*`,
  17 each) which additionally reaches the LdSt slots of `F3/F6/F7`.
* **Predicated-t** carry **13** placements (the `neg*t` 17) — concentrated in ALU slots
  (`f*_s3_alu`, the two `s4_alu`, `n0_s3_alu`) plus `f1_s0_ldstalu`, `f1/f2/f7_s2_mul`.

The structural co-issue ceiling is the format's slot roster (one LdSt-class + Mul + 1–3 ALU,
capped by the 2 LSU copies — [coverage-tally §7](../core/coverage-tally.md)). A B03 compare's
ability to issue in the Mul slot is what lets a fused `compare; predicated-merge` pair land in one
bundle (compare → Mul slot writes `vbool`; the `*t` form → ALU slot reads it next bundle).

> **GOTCHA — the `vbool` written by a compare is read at stage 10 by a same-or-later op, not
> forwarded within the bundle.** Per [register-files §5](../core/register-files.md), `vbool`
> writes land @10/@11 and reads land @10. A predicated-t op that consumes a compare's flag must be
> in a *later* bundle (or rely on the scheduler's stage model) — a compare and its dependent `*t`
> in the **same** bundle would read a stale mask. The 2-port `vbool` read limit also caps a bundle
> at two predicate-consuming ops. `[HIGH/OBSERVED]` on the stages; bundle-level forwarding is the
> scheduler's responsibility.

---

## 7. Partition discipline — the exact family-prefix assignment

To guarantee **no mnemonic is double-counted** across B01/B02/B03 (and the predicate-adjacent
batches), the per-prefix ownership, derived against the roster:

| family / prefix | example | owner | why |
|---|---|---|---|
| int `add/sub/max/min/abs/and/or/xor`, no flag, no mask | `ivp_addnx16` | **B01** | `vec→vec` only (3 args, single `o`) |
| fp `*xf16`/`*_2xf32` incl. fp compares + their `t`-forms | `ivp_oltn_2xf32`, `ivp_addnxf16t` | **B02** | fp inputs |
| int compares `{eq,neq,le,leu,lt,ltu}{2nx8,nx16,n_2x32}` | `ivp_eqnx16` | **B03** | int in → `vbool` out |
| int B-variant `ivp_b{max,min,abs,absub,addnorm,subnorm,xorabssubu}*` | `ivp_bmaxnx16` | **B03** | int ALU + `vbool` flag out |
| int predicated `ivp_{add,sub,adds,subs,neg,negs,max,maxu,min,minu}…t` | `ivp_addnx16t` | **B03** | int ALU + `vbool` mask in (merge) |
| reduce + flag `ivp_r{add,max,min}…t`, `ivp_ltr*`, `ivp_ltrs*` | `ivp_raddnx16t`, `ivp_ltrn` | **B08** | reduce tree (loads, `f0_s1_ld`) |
| vbool→vbool boolean logic + folds `ivp_bnorfs2n`, `ivp_borfs2n`, `andb`/`orb`/`xorb` | `ivp_bnorfs2n` | **B11** | `vbool` in → `vbool` out, ld-pipe |
| select/merge by mask `ivp_sel*`, `ivp_dsel*`, shuffle | `ivp_selnx16` | **B21** | 3-input mux, `vec_select_*` sem |
| replicate `ivp_rep*` (incl `*t`) | `ivp_repnx16t` | **B16** | broadcast |
| scatter/gather `*t` | `ivp_scatternx16t` | **B19** | memory |
| convert `t`-forms | `ivp_float16nx16t` | **B13/B20** | int↔fp convert |

The arithmetic that makes the partition airtight: B03's 14 predicated-t are the integer subset of
the 140-op `t`-family; the other 126 `t`-ops are owned by B02 (fp), B08 (reduce), B16, B19,
B21, B13/B20 by the prefixes above — **disjoint**. The 18 compares and 22 B-variants have no
counterpart in B01 (which is flagless `vec→vec`) or B02 (which is fp). `ivp_bnorfs2n`/`ivp_borfs2n`
are explicitly **excluded** from B03's 22 B-variants (they fold 8 `vbool` inputs in the load pipe
→ B11). `[HIGH/OBSERVED]`

---

## 8. Batch tally vs `nm`

| sub-family | mnemonics | placements | leaf prefix (libfiss) |
|---|--:|--:|---|
| Integer compares | 18 | 378 | `eq/neq/lt/ltu/le/leu_{1,2,4}_{8,16,32}` |
| B-variant flag ALU | 22 | 321 | `bmax/bmin/babs/babssub/baddnorm/bsubnorm/bxorabssubu` |
| Predicated-t merge | 14 | 190 | base leaf + `bitkillt/bitkillf` |
| **B03 total** | **54** | **889** | — |

Cross-check: `378 = 18×21`; `190 = 12×13 + 2×17` (the two `neg*t` carry 17 each); the B-variant
321 is the sum of the per-mnemonic counts in §2.2 (15 for max/min/sub forms, 17 for the three
`babs`). The 54 mnemonics are a strict subset of the 1065 `ivp_*`-prefixed ops, and together with
B01/B02 cover the integer+fp ALU region without overlap (§7). No B03 mnemonic appears in any other
batch's prefix set. The 889 placements are part of — never additive beyond — the certified-perfect
12569 ([coverage-tally](../core/coverage-tally.md)). `[HIGH/OBSERVED]`

---

## 9. Adversarial self-verification — five strongest claims re-challenged

1. **"The compare result is a `vbool` predicate, width = lane-bytes bits per lane."** Driving
   `eq_1_8_8`/`eq_2_16_16`/`eq_4_32_32` live gives outputs `0x1`/`0x3`/`0xf` on a true
   compare — exactly 1/2/4 set bits, matching the 8/16/32-bit lane and the `vbool` "one mask bit
   per 8-bit sub-lane" geometry ([register-files §3](../core/register-files.md)). The leaf
   bodies `and $0x1`/`$0x3`/`$0xf` confirm the mask width. **Holds.**
2. **"B-variant ops write *two* outputs (data + flag), predicated-t marks dst inout."** The
   `_operands` descriptor bytes: `IVP_BMAXNX16` has `num_args=4` with two `0x6f` ('o') bytes;
   `IVP_ADDNX16T` has `num_args=4` with the dst byte `0x6d` ('m', inout); `IVP_ADDNX16` has
   `num_args=3`, one `0x6f`. The byte-level direction tags are the discriminator, not an inferred
   convention. **Holds.**
3. **"`baddnorm` produces a carry/overflow flag and normalizes by >>1."** Execution across 5
   inputs: `(20000,20000)→(0x3,20000)`, `(30000,5000)→(0x3,17500)`, `(100,200)→(0x0,300)`.
   The disasm shows the bit15-vs-bit16 compare (`sete`) and the conditional `shr $1`/`cmove`. The
   value-and-flag both reproduce. **Holds.**
4. **"The compare selector is enumerated, not a flat `index×2` counter."** Reading
   all 18 selector words: within a byte they step by 2 (`0,2,…,e`) but `0x0e→0x100` is a carry, so
   `sel = (group<<8)|(sub<<1)`, not a single counter. A naive `index×2` would mis-encode every op
   past `leunx16`. The per-op table is authoritative. **Holds (and corrects the naive reading).**
5. **"`ivp_addnx16` is B01, `ivp_addnx16t` is B03 — distinct opcodes."** They carry distinct
   `Opcode_*_encode` symbols, distinct `word0` templates (`0x80b50000` vs `0x66f80000`), distinct
   `num_args` (3 vs 4), distinct `opcodes[]` rows. Not a bit-flip — the `t` band is `0x66/0x67`,
   uncorrelated with the base op's selector. The partition is mechanical, not nominal. **Holds.**

**Ungrounded / flagged items.** (i) The `2nx8`/`n_2x32` **B-variant** selector words for the
non-`nx16` widths (e.g. `ivp_bmaxu2nx8`, `ivp_bminn_2x32`) are present and counted but only a
subset were template-dumped; their value semantics *are* execution-validated via the
`bmax_1_8_8_8`/`bmax_4_32_32_32` leaves, so the gap is the *selector word*, not the behaviour —
`[MED/OBSERVED]` on those specific selector immediates. (ii) Bundle-level whether a compare can
forward its `vbool` to a same-bundle `*t` consumer is **scheduler policy**, not an ISA fact; the
stage model (`vbool` write@10/11, read@10) is `[HIGH/OBSERVED]` but the intra-bundle forward is
left to the scheduler. (iii) The customop `instruction_mapping.json` is a **different** (TPB
tensor-engine) opcode layer (`struct2opcode` schema) and does **not** cross-validate these Q7 ISA
mnemonics — noted so no downstream page treats it as a second witness for B03.

---

## 10. Cross-references

* [FLIX VLIW Encoding](../core/flix-encoding.md) — 14 formats / 46 slots, the `C7 07 imm32`
  encode-thunk ABI (§6.1), and the two-tier selector model (§6.2) this batch instantiates.
* [The Eight Register Files](../core/register-files.md) — `vbool` idx3 (the flag/mask file),
  `b32_pr` idx6, and the `vbool`/`vec` read/write stages (§5) that bound co-issue (§6).
* [ISA Coverage & the 1534/12642 Tally](../core/coverage-tally.md) — the certified-perfect
  12569 placement denominator this batch's 889 are part of.
* [B01 — Vector ALU int core](b01-vec-alu-int.md) · [B02 — Vector ALU fp slice](b02-vec-alu-fp.md)
  — the two batches B03 complements (the partition boundary, §7).
* [B08 — Reduce](b08-reduce.md) (reduce-and-flag `r*t`, `ltr`) ·
  [B11 — vbool ALU / predicate](b11-vbool-alu.md) (vbool→vbool logic, `bnorfs`/`borfs`) ·
  [B21 — Select / shuffle](b21-select-shuffle.md) (`sel`/`dsel`) ·
  [B19 — Scatter / gather](b19-scatter-gather.md) — the predicate-adjacent batches B03 is
  disjoint from.
* [libisa Table Schema & Codec ABI](../core/libisa-table-schema.md) — the `_operands`
  descriptor / direction-byte format read in §3.4.
* [The Confidence & Walls Model](../../reference/confidence-model.md) — the tags and the free
  in-process value lane (`libfiss-base`) that makes §5 `OBSERVED by execution`.

---

*Provenance: selector templates, `num_args`/`_operands` direction bytes and the
`ivp_sem_vec_alu_vbr` field thunk are disassembled from `libisa-core.so` (sha256 `8fe68bf4…`,
`ncore2gp/config/`, not stripped); the 12 value leaves in §4–§5 are driven live via `ctypes`
against `libfiss-base.so` (sha256 `260b110c…`), license-free. Counts via `nm | rg -c`; `.data.rel.ro`
file = VMA − `0x200000`; the `extracted/` tree is gitignored (reach with absolute paths). All
prose is derived from static analysis and in-process execution of the shipped artifacts only;
nothing here is read from a vendor source tree.*
