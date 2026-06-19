# Formal Semantics I — Arithmetic / MAC / Load–Store / Gather

This is the **bit-precise per-opcode reference compute** for four op groups of the Vision-Q7
*Cairo* (`ncore2gp`) ISA: vector **arithmetic** (add/sub/min/max/abs/avg + the width and
saturation rules), **multiply / MAC** (the `mul`/`mulp`/`mulsup` variants, the 48-bit `wvec`
accumulator, the fp single-rounding FMA), **load–store** (the dual-LSU address generation plus
the alignment funnel), and **gather / scatter + permute** (the SuperGather index→element mapping,
`elem_sz` scaling, the out-of-bounds sentinel, and the select/shuffle crossbar). Unlike the B01–B30
[ISA-reference pages](../ref/template-and-partition.md) — which catalog *encodings* — this page is
the **reference math** those encodings drive, synthesized across the batches and grounded against
the executable value oracle.

Three binary tiers are read against each other so no claim rests on a single artifact:

* The **`libtie-core.so` `post_rewrite` TIE semantics** are the source-of-truth datapath: the
  blob lives in `.data` at VMA `0x20104a` (`nm libtie-core.so | rg xml_data_post_rewrite`), with
  `xml_data_compiler` at `0x30c1d27` marking its end. `.data` has VMA `0x201018` but file offset
  `0x001018` (`readelf -SW`), so the blob's **file offset = VMA − `0x200000`** — *not* the
  `0x400000` delta seen in other DLLs. Each op group resolves to one shared, fully-multiplexed
  `<SEMANTIC>`/`<MODULE>`/`<FUNCTION>` block, *not* N per-opcode functions.
* The **`libfiss-base.so` `module__xdref_*` value leaves** (864 of them; `nm -D | rg -c
  module__xdref_`) are the **executable oracle**: self-contained, integer-only, in-process. This
  page **drives them live via `ctypes`** and prints the exact result bits for at least one
  representative op of every group. This is the ground truth that both the `libcas-core` `xdsem`
  model and the TIE reference are checked against.
* The **`libtie-Xtensa-msem.so` `xt_*_semantic` functions** carry the memory-access reference
  (alignment, funnel, byte-disable, sign/zero-extend) for load–store, and the SuperGather host
  `<INTERFACE>` ports for gather. The blob is decoded the same way.

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte / immediate / symbol / **executed** value read from the shipped binary;
`INFERRED` = reasoned over OBSERVED; `CARRIED` = re-used at a cited page's confidence; crossed with
`HIGH`/`MED`/`LOW`. Counts are grounded with `nm | rg -c` against the `.symtab`, never a decompile
grep; the `extracted/` tree is gitignored (reach it with `fd --no-ignore` or an absolute path). All
prose is binary / static-analysis derived only.

> **Scope in one line.** Four shared datapaths, one per group: the multiplexed integer ALU slice
> `xdsem_vec_alu_arith_8Mbit_64`; the partial-product/carry-save `ivp_sem_mul_slice` with its
> **48-bit-per-lane** `wvec` accumulator; the msem `xt_load_semantic`/`xt_store_semantic` funnel
> pair; and the `ivp_sem_vec_scatter_gather` index plane + `xdsem_tiesel_5_32` permute crossbar.
> Each opcode is a *parameterization* of its group's one datapath by decode-group selector bits —
> never a private function. `[HIGH/OBSERVED]`

---

## 0. The shared-datapath thesis and the live oracle ABI

Every one of these four groups is built the same way in silicon and in the TIE source: **one
parametric reference datapath**, specialized per mnemonic by *decode-group selector wires* (the
`op_<GROUP>` OR-reductions in the integer paths; the FUNCTION's `has_*`/`Isa*` selector inputs in
the memory paths; the `operation_fld` / `elem_sz_fld` control fields in gather). The `(operation,
signedness, saturation, rounding, lane-width, predication)` tuple of an opcode is therefore
**decoder-exhaustive and observed**, not inferred from the mnemonic spelling.

The value oracle that proves the math is the `module__xdref_*` family. Its calling convention was
read directly from the prologues (`objdump -d`) and is **not** the obvious one — there is a dead
first integer argument:

| primitive shape | `%rdi` (arg1) | `%rsi` (arg2) | `%rdx` (arg3) | `%rcx` (arg4) | `%r8` (arg5) |
|---|---|---|---|---|---|
| 3-operand lane prim (`add`, `min`, `mul`) | *ignored* | A | B | result ptr | — |
| unary prim (`sext`, `bitkillf`) | *ignored* | A | result ptr | — | — |
| 4-operand accumulate (`mula`) | *ignored* | acc ptr | B | C | result ptr |
| wide funnel (`wideldshift`) | *ignored* | line ptr (512 b) | shamt | result ptr | — |

The dead `%rdi` is why the SX survey reported "A in `%esi`, B in `%edx`": in System-V x86-64 those
are args 2 and 3. The C signature for a 3-operand leaf is therefore
`void f(uint64_t /*unused*/, uint32_t a, uint32_t b, uint32_t *res)`. Every live value below was
produced with that ABI; the driver is reproducible from the disassembly alone.

> **NOTE — `.text` is one-to-one, `.data` is not.** For `libfiss-base.so` the `.text` VMA equals
> the file offset (verified), so the `objdump --start-address=<VMA>` anchors below are exact. For
> the TIE/msem blobs the payload is `.data`-resident, so subtract `0x200000` from the VMA to reach
> the file offset. Mixing these up was a real prior false-positive. `[HIGH/OBSERVED]`

---

## 1. Group 1 — Vector Arithmetic (`xdsem_vec_alu_arith_8Mbit_64`)

Batch [B01](../ref/b01-vec-alu-int.md) (int ALU). The whole IVP vector-arithmetic ISA — 102
`IVP_*` opcodes across 11 op-families (ADD/SUB/MAX/MIN/AVG/ABS/ABSSUB/NEG/MULSGN/MINORMAX/ADDEXP) —
lives in **one** `<SEMANTIC name="ivp_sem_vec_alu">` block (decoded-XML byte `33,778,228`,
`301,208 B`, member LIST of 244 opcodes). Per-opcode behavior is a *selection* over that one block.

### 1.1 The shared datapath structure

The arithmetic core is the module `xdsem_vec_alu_arith_8Mbit_64` (`213,306 B`), instantiated
**8 bytes ("M0..M7") × 8 = 512 bits** across the vector register. It is a **two's-complement
carry-chain adder** with lane-break masks (the `FUNCTION xdsem_vec_alu_arith_core_Mbit_64`,
`49,892 B`, holds the only native `<ADD>` operators — there is no native subtract, min, or abs
primitive; everything is structural over that one adder). The bit-precise structure, transcribed
from the TIE AST as annotated C:

```c
// xdsem_vec_alu_arith_core_Mbit_64  — the shared integer ALU primitive, per lane.
// op_GRP_A1_SUB, op_GRP_UNSIGN, op_GRP_SAT, op_AVGR, op_MAX/op_MIN/op_ABS/...
// are 1-bit DECODE-GROUP wires, each an OR-reduction over the opcodes in that group.
static int64_t vec_alu_lane(int64_t a, int64_t b, int w /*lane bits 8/16/32*/,
                            bool op_GRP_A1_SUB, bool op_GRP_UNSIGN, bool op_GRP_SAT,
                            bool op_AVGR, bool op_AVG, bool op_MAX, bool op_MIN,
                            bool op_ABS, bool op_ABSSUB, bool op_MULSGN) {
    // --- the single carry-chain adder (res_adder1) ---
    int64_t b_inv  = op_GRP_A1_SUB ? ~b : b;        // SUB => invert b   (inp_b_inv_lo)
    int     cin    = (op_GRP_A1_SUB || op_AVGR) ? 1 : 0;  // SUB carry-in; AVGR round +1
    // propagate_mask zeroes the carry crossing each w-bit lane boundary => wrap mod 2^w
    int64_t res_adder1 = lane_add_carrybreak(a, b_inv, cin, w);   // a + ~b + 1 = a - b

    // signed vs unsigned is the GUARD-BIT EXTENSION (ctrl_unsign_adder = op_GRP_UNSIGN):
    //   signed   => replicate sign bit into the guard;  unsigned => zero-extend.

    // --- saturation, gated by op_GRP_SAT only ---
    if (op_GRP_SAT) {
        int64_t SAT_POS = (1LL << (w-1)) - 1;       // {1, 15{1}}  => 0x7FFF for w=16
        int64_t SAT_NEG = -(1LL << (w-1));          // {0, 15{0}}  => 0x8000 for w=16
        if (adder1_overflow(res_adder1))  return SAT_POS;   // +ovf clamp to MAX
        if (adder1_underflow(res_adder1)) return SAT_NEG;   // -ovf clamp to MIN
    }

    // --- average: AVG=(a+b)>>1, AVGR=(a+b+1)>>1 (the +1 already added via cin) ---
    if (op_AVG || op_AVGR) return res_adder1 >> 1;   // res_adder1_shifted_8M

    // --- a second adder computes (a - b); its le/ge flags drive MIN/MAX/ABSSUB ---
    int64_t res_adder2 = lane_sub(a, b, w);
    if (op_MAX)    return (res_adder2 >= 0) ? a : b;  // signed/unsigned per op_GRP_UNSIGN
    if (op_MIN)    return (res_adder2 <= 0) ? a : b;
    if (op_ABSSUB) return (res_adder2 < 0) ? -res_adder2 : res_adder2;  // |a - b|
    if (op_ABS)    return (a < 0) ? lane_sub(0, a, w) : a;  // |a| via sign-conditional neg
    if (op_MULSGN) return a * sign_of(b);            // a*sgn(b) in {-a, 0, +a}

    return res_adder1;   // plain ADD / SUB / NEG (op_GRP_A1_OUT)
}
```

### 1.2 Per-op parameterization

Every opcode reduces to a setting of those wires. The selector for a group is literally written in
the XML as `op_<GROUP> = BITWISE_OR{member opcodes}`, e.g. `op_ADD = OR{ADD2NX8, ADDNX16,
ADDN_2X32, ADDSNX16, BADDNORMNX16, …}` and `op_GRP_SAT = OR{ABSSNX16, ADDSNX16, NEGSNX16,
SUBSNX16, …}`. Representative bindings:

| opcode | datapath setting | reference op | lanes×w | sat | sign |
|---|---|---|---|---|---|
| `IVP_ADDNX16` | `op_ADD`, `op_GRP_A1_SUB=0` | `a + b`, wrap | 32×16 | wrap | S |
| `IVP_SUBNX16` | `op_ADD`, `op_GRP_A1_SUB=1` | `a + ~b + 1` | 32×16 | wrap | S |
| `IVP_ADDSNX16` | `op_ADD`, `op_GRP_SAT=1` | `sat16(a+b)` | 32×16 | **clamp** | S |
| `IVP_ADDNX16U` | `op_ADD`, `op_GRP_UNSIGN=1` | `a + b` zero-ext | 32×16 | wrap | U |
| `IVP_MAXNX16` / `IVP_MAXUNX16` | `op_MAX`, `op_GRP_UNSIGN` 0/1 | signed/unsigned `max` | 32×16 | wrap | S/U |
| `IVP_AVGNX16` / `IVP_AVGRNX16` | `op_AVG` / `op_AVGR` | `(a+b)>>1` / `(a+b+1)>>1` | 32×16 | wrap | S |
| `IVP_ABSSUBNX16` / `…U` | `op_ABSSUB`, `op_GRP_UNSIGN` | `\|a−b\|` signed/unsigned | 32×16 | wrap | S/U |
| `IVP_MULSGNNX16` | `op_MULSGN` | `a·sgn(b)` | 32×16 | wrap | S |

The lane grid is fixed by the 512-bit register: `2NX8` = 64×8b, `NX16` = 32×16b, `N_2X32` = 16×32b.
The `T` (predicated) variants take a `vboolN d` and a `/*inout*/ a`, computing per lane
`d ? op(b,c) : a` (a predicated merge into the prior destination).

### 1.3 Key invariants

* **SUB is `a + ~b + 1`.** There is no subtract primitive; the `<ADD>` core gets `b` XOR-ed with
  `op_GRP_A1_SUB` and a carry-in of 1. `NEG = 0 + ~a + 1`. `[HIGH/OBSERVED]`
* **Integer add/sub/min/max/avg WRAP** mod `2^w`, because `propagate_mask` breaks the carry chain
  at each 8/16/32-bit lane edge. Saturation is a *separate* clamp gated only by `op_GRP_SAT` (the
  `*S` opcodes); it clamps to `±(2^(w−1))` with the literal constants `{1,15{1}}` = `0x7FFF` and
  `{0,15{0}}` = `0x8000`. `[HIGH/OBSERVED]`
* **Signedness is operand-extension, not a runtime flag.** `ctrl_unsign_adder` (= `op_GRP_UNSIGN`)
  selects sign-extend vs zero-extend of each lane's guard bit before the add; the signed and
  unsigned opcodes are *separate intrinsics* feeding the same datapath. `[HIGH/OBSERVED]`
* **`AVGR` rounds half-up:** the `+1` enters through the same `arith_cin1` carry-in *before* the
  `>>1`, so `AVGR(a,b) = (a+b+1)>>1` and `AVG(a,b) = (a+b)>>1`. `[HIGH/OBSERVED]`
* **MIN/MAX vs MINNUM/MAXNUM (fp) differ on NaN:** plain min/max use the raw ordered compare
  (operand-order NaN propagation); `MINNUM`/`MAXNUM` use the `f*_minnum_sel_a`/`maxnum_sel_a`
  NaN-suppress selectors (IEEE-754 `minNum`/`maxNum`: NaN → the other operand). `[HIGH/OBSERVED]`

### 1.4 Live-driven representative value

Driving the `xdref` oracle (the `libcas`/TIE model's ground truth) confirms the wrap-vs-saturate
split and the rounding exactly:

```text
# module__xdref_add_16_16_16  @0x858480  :  add %esi,%edx ; and $0xffff ; mov %edx,(%rcx)
add_16_16_16 (0x7fff, 0x0001)  -> 0x00008000   # 32767+1 WRAPS to -32768=0x8000  (not saturated)

# module__xdref_adds_16_16_16 @0x85aa10  :  17-bit overflow-detect, clamp 0x7fff/0x8000
adds_16_16_16(0x7fff, 0x0001)  -> 0x00007fff   # SAT clamp to INT16_MAX
adds_16_16_16(0x8000, 0xffff)  -> 0x00008000   # SAT clamp to INT16_MIN (neg overflow)

# module__xdref_avg / avgr_16_16_16  @0x81d170 / @0x81d1e0
avg (3,4) -> 0x3   avgr(3,4) -> 0x4            # floor((3+4)/2)=3  vs round=4
avg (-4,-2) -> 0xfffd  (signed -3)             # signed average: (-4 + -2)/2 = -3

# module__xdref_min_16_16_16 vs minu_16_16_16  (DISTINCT bodies)
min_16_16_16 (0xffff, 1) -> 0xffff             # signed: -1 < 1 -> -1
minu_16_16_16(0xffff, 1) -> 0x0001             # unsigned: 1 < 65535 -> 1
```

The identical `add` input under two opcodes gives `0x8000` (wrap) versus `0x7fff` (saturate),
proving the wrap/saturate axis is a decode bit, not a value-dependent behavior. See
[`iss/cas-arith-sem.md`](../../iss/cas-arith-sem.md) for the cas-side decode bitmap these wires
were generated from.

---

## 2. Group 2 — Multiply / MAC (`ivp_sem_mul_slice`)

Batches [B04](../ref/b04-mac-integer.md) (signed int MAC), [B05](../ref/b05-mac-mixed.md)
(mixed/unsigned), [B17](../ref/b17-spfma.md) (fp32 FMA), [B18](../ref/b18-hp-fma.md) (fp16 FMA),
and the [B22 `wvec` accumulator geometry](../ref/b22-unpack-wvec-mov.md). All 232 integer multiply
opcodes live in one `<SEMANTIC name="ivp_sem_multiply">` block (decoded byte `34,953,115`,
`313,045 B`); the fp FMAs live in two *separate* sibling blocks (`fp_sem_hp_fma` and
`ivp_sem_spfma`) because they are a genuinely different datapath.

### 2.1 The shared datapath structure

The integer core is `ivp_sem_mul_slice` (decoded byte `9,672,770`, `168,488 B`). **There is no
native `<MUL>` operator anywhere in the 49 MB blob** (`<ADD>`×1968, `<SUB>`×748, `<MUL>`×0): the
multiplier is a Wallace/Dadda partial-product array — sign-corrected partial products summed in a
carry-save (redundant sum/carry) tree, resolved by the 5 carry-propagate `<ADD>`s
(`add_cs0_8x8 = res_acc_sum0_8x8 + res_acc_car0_8x8`, …). Annotated C:

```c
// ivp_sem_mul_slice — handles 2 lanes (even/odd) x 48-bit accumulator, x16 = 1536-bit wvec.
// Accumulator rails are LITERALLY [47:0] in the TIE:
//   accum_even[47:0], accum_odd[47:0], accum_wvu_even[47:0], prod_8x8_even[47:0] ...
//   carry-save compressor inputs are [49:0] = 48 + 2 guard bits.
static void mac_slice(int48_t *wvt /*acc rail*/, int48_t *wvu,
                      int16_t a, int16_t b,
                      bool sign_a, bool unsign_a, bool unsign_b,  // per-operand extension
                      bool op_acc, bool op_decneg)                // RMW vs overwrite; mul-SUB
{
    // signedness = the partial-product EXTENSION select (s*s / u*u / s*u / u*s):
    int64_t pa = unsign_a ? (uint16_t)a : (int16_t)a;
    int64_t pb = unsign_b ? (uint16_t)b : (int16_t)b;
    int48_t product = (int48_t)(pa * pb);     // carry-save array -> 48-bit lane product
    if (op_decneg) product = -product;        // MULS = mul-SUBTRACT (two's-comp partial-prod neg)

    // accumulate-vs-overwrite merge (op_acc): A-suffix MACs read prior acc; plain MUL zeroes it.
    int48_t old = op_acc ? *wvt : 0;
    *wvt = (int48_t)(old + product);          // WRAPS mod 2^48 — no sat/round in the accumulate
}
```

### 2.2 Per-op parameterization — the widening precision matrix

The accumulator is **always wider than the product, and the product wider than the inputs**. The
widths are read three ways and agree (TIE wire widths · `xt_ivp32.h` acc vector types · `xdref`
leaf width-tuples):

| inputs | product | accumulator | TIE wire | `xt_ivp32.h` acc type |
|---|---|---|---|---|
| `i8 × i8` | 24-bit | **24-bit** | `res_acc_*_8x8[23:0]` | `xb_vec2Nx24` (64×24b) |
| `i16 × i16` | 32-bit | **48-bit** | `accum_even/odd[47:0]` | `xb_vecNx48` (32×48b) |
| `i16 × i32` | 48-bit | **64/96-bit** | `op_mul_16x32_*` | `xb_vecN_2x64w` |
| `i32 × i32` | 64-bit | **96-bit** (2×48) | `wvt/wvu[95:0]` | `xb_vecN_2x64w` |

The *form* is selected by partial-product topology: `op_mul_*` (plain), `op_mulp_*` (PAIR / 2×-FMAC:
`a1·b1 + a2·b2`), `op_mulq_*`/`op_mul4t_*` (QUAD / 4-term dot), `op_sqr_*` (square), `op_dmul*_8x8`
(dual quad-8). The `XR` forms (`MULPAN16XR16`, `MULQAN16XR16`) source one factor from the packed
reduce-register `pr<N>` — the matmul weight bus. Representative bindings:

| opcode | form | reference | precision | acc |
|---|---|---|---|---|
| `IVP_MULNX16` | plain s×s | `a·b`, overwrite | 16×16→48 | overwrite |
| `IVP_MULANX16` | acc s×s | `acc += a·b` | 16×16→48 | RMW |
| `IVP_MULSNX16` | mul-sub | `acc −= a·b` (`op_decneg`) | 16×16→48 | RMW |
| `IVP_MULPANX16` | pair-acc | `acc += a1·b1 + a2·b2` | 16×16→48 | RMW |
| `IVP_MULPAN16XR16` | pair-acc ×`pr` | `acc += (a1·b1+a2·b2)·pr<N>` | 16×16→48 | RMW (the cptc scale-MAC) |
| `IVP_MULSUPN16XR16` | s×u pair | `(a1·b1+a2·b2)` mixed-sign | 16×16→48 | overwrite |
| `IVP_MUL4TAN16XR16` | 4-term dot | `acc += Σ4 products` | 16×16→48 | RMW (PE inner-product) |
| `IVP_MULANX16PACKL` | acc-then-pack | `sat16(acc)` narrow | 48→16 | RMW + **pack-sat** |

### 2.3 Key invariants

* **The i16 MAC accumulator is exactly 48 bits per lane.** The `[47:0]` accum wires give 32 guard
  bits over the 16-bit input — ~`2^32` MACs before overflow. The `wvec` register is
  `1536 b = 16 slices × 96 b = 32 lanes × 48 b` (regfile idx 5, 4 entries; matches the
  [B22 geometry](../ref/b22-unpack-wvec-mov.md)). `[HIGH/OBSERVED]`
* **The integer accumulate WRAPS — no saturation, no rounding inside the MAC.** Saturation is a
  *post-op*: the `PACKL`/`PACKP`/`PACKQ` suffixes narrow the wide accumulator back to a `vec` lane
  with a signed clamp. `[HIGH/OBSERVED]`
* **Signedness is the partial-product extension** (`sign_a`/`unsign_a`/`unsign_b`), giving the
  `s*s` / `u*u` / `s*u` / `u*s` quad as *separate opcodes*, not a mode. The `XR` forms add the
  packed-reduce-register factor. `[HIGH/OBSERVED]`
* **The fp FMA is a genuine single-rounding fused multiply-add** in two separate blocks:
  `fp_sem_hp_fma` (fp16 `NXF16`) and `ivp_sem_spfma` (fp32 `N_2XF32`). The product is kept
  full-width before the add+round (one rounding, not two). Form decode: `op_madd` (`a·b+c`),
  `op_msub` (`c−a·b`), `op_maddn` (`−(a·b)+c`), `op_mulsone`. The fp MACs accumulate in the native
  fp `vec` type — **no wide accumulator**. `[HIGH structure]`

> **The two rounding levels.** The fp FMA's tie-break rounding is *parameter-driven*: the round/flag
> context arrives in the `%r8`/`%r9` argument and a status word is written back, i.e. the rounding
> mode is the **`RoundMode` SR plumbed by the caller**. The `FCR` reset value is **RNE**; an
> *un-parameterized* fiss leaf defaults to **RZ** (round-toward-zero). State which applies for the
> single-rounding invariant: in the production path the caller supplies `RoundMode = RNE` from the
> `FCR`; a bare leaf call with no context rounds RZ. The exact tie-break table is the
> [fp-rounding slice](group-semantics-ii.md)'s domain. `[HIGH structure / MED bit-exact tie-break]`

### 2.4 Live-driven representative value

The `mul_48_16_16` leaf is a signed 16×16 widening multiply that writes the 48-bit product as a
low-32 word plus a sign-extended high-16 word. The `mula_48_48_16_16` leaf is the accumulate form,
and chaining it proves the accumulator exceeds 32 bits:

```text
# module__xdref_mul_48_16_16 @0x68a690 : movswl both ; imul ; (%rcx)=low32 ; 0x4(%rcx)=sext hi16
mul_48_16_16(0x7fff, 0x7fff) -> lo=0x3fff0001 hi=0x0000  48b=0x00003fff0001  # 32767^2 = 1073676289 ok
mul_48_16_16(0xffff, 0x0002) -> 48b=0xfffffffffffe  signed=-2                # (-1)*2 = -2, 48b sext

# module__xdref_muln_2x16x32_0_96_32_32 @0x68a940 : 16x32 widen, 64-bit imul, 48-bit sext result
muln_2x16x32(0xffff, 3) -> w0=0xfffffffd w1=0xffffffff w2=0x00000000        # (-1)*3 = -3 ok

# module__xdref_mula_48_48_16_16 @0x68a6b0 : acc += movswl(C)*movswl(D), 48-bit back
mula(acc=0,     -2,    3)     -> 0xfffffffffffa  signed=-6                   # 0 + (-2*3) = -6
mula(acc=-6, 10000, 10000)    -> 0x000005f5e0fa  signed=99999994            # -6 + 1e8; 27-bit result
```

The chained `mula` result `0x000005f5e0fa` = 99,999,994 is **larger than 32 bits** and survives the
read-modify-write across the 32-bit boundary, confirming the per-lane accumulator is wider than 32
bits — exactly the 48-bit `wvec` lane. See [`iss/cas-mac-fmac.md`](../../iss/cas-mac-fmac.md) for
the cas/fiss two-track (integer wide-MAC vs fp FMA) model.

---

## 3. Group 3 — Load / Store (`xt_load_semantic` / `xt_store_semantic`)

Batches [B06](../ref/b06-loads.md) (loads), [B07](../ref/b07-stores.md) (stores). The
memory-access reference lives in a **separate provider**: it is *not* a base-DB `ivp_sem_*` block.
The base DB (`libtie-core`) carries each opcode's **address generation** (which AR is the base, the
immediate/register offset, the post-increment writeback) via its PROTO/ICLASS; the **memory-access
compute** (alignment, funnel, byte-disable, extend, endianness) is the msem package's one shared
FUNCTION pair `xt_load_semantic` / `xt_store_semantic`, which every load/store opcode feeds. The
base DB has *zero* references to `xt_load_semantic`/`xtms_aligned_load` — the split is real.

### 3.1 The shared datapath structure

`xt_load_semantic` (msem byte `120,818`, `52,219 B`) returns `[512:0]` (512-bit loaded data + a
kill bit). Its body is a readable RTL netlist; the bit-precise compute as annotated C:

```c
// xt_load_semantic — the dual-LSU load reference. VAddr = the address-gen result;
// LSBytes = access width; SignExtendFrom/To, RotateAmount, LoadByteDisable = per-opcode bindings.
static u512 xt_load(u32 VAddr, u8 LSBytes, bool has_SignExtendFrom, u8 SignExtendFrom,
                    u8 SignExtendTo, bool has_LoadByteDisable, u64 LoadByteDisable,
                    bool handle_unaligned /*IsaSys|IsaHardwareHandlesMisAligned*/) {
    u8  issue_bytes = has_SignExtendFrom ? SignExtendFrom : LSBytes;   // natural width
    u32 issue_bits  = issue_bytes * 8;
    u32 aligned_vaddr = VAddr & ~(issue_bytes - 1);   // ALIGN DOWN to natural width
    u32 addrsel       = VAddr &  (issue_bytes - 1);   // the misalignment offset
    u32 next_aligned  = aligned_vaddr + issue_bytes;  // the SECOND (tail) aligned access

    u512 lv0 = xtms_aligned_load(aligned_vaddr);      // pipe 0
    u512 lv1 = xtms_aligned_load(next_aligned);       // pipe 1 (tail, only for unaligned)

    u32  shiftsel   = handle_unaligned ? (addrsel * 8) : 0;
    u512 loaded_val = (lv0 >> shiftsel) | (lv1 << (issue_bits - shiftsel));  // FUNNEL MERGE (LE)

    u512 disable_val = loaded_val & ~disable_mask(LoadByteDisable);          // byte-disable

    // sign/zero extend + truncate-to-extend_to:
    u8   extend_to = has_SignExtendTo ? SignExtendTo : LSBytes;
    u512 sign_mask = lanes_above(issue_bytes);        // bytes above natural width
    u512 zero_mask = lanes_below(extend_to);          // bytes below extend_to kept
    bool sign      = bit(disable_val, issue_bits - 1);// natural-width MSB
    u512 sign_splash = sign ? ALL_ONES : 0;
    return (disable_val | (sign_splash & sign_mask)) & zero_mask;
    // SIGNED load: sign_mask bytes get the sign bit => sign-extend.
    // UNSIGNED load: SignExtendFrom absent => sign_mask=0 => those bytes stay 0 => zero-extend.
}
```

`xt_store_semantic` (msem byte `198,656`, `38,523 B`) mirrors it and adds truncation:
`mem_data = (MemDataOut >> nx_store_shift) & ~zero_mask` writes **exactly `issue_bytes` bytes**
(high bytes dropped), honoring a per-byte (`StoreByteDisable`) *and* per-word (`StoreWordDisable`,
16×32b) disable mask, split across two pipes for unaligned addresses.

### 3.2 Per-op parameterization — addressing-mode grammar

The addressing mode is the opcode suffix; the alignment/extend behavior is `xt_load_semantic`
parameterized by that opcode's `(LSBytes, SignExtendFrom/To, RotateAmount)` binding:

| suffix | offset operand | address semantics |
|---|---|---|
| `_I` | immediate `c` | `addr = base_AR + IMM` (no writeback) |
| `_IP` | `inout ptr b` + imm | `addr = base_AR ; base_AR += IMM` (post-inc) |
| `_X` | `int32 c` | `addr = base_AR + REG` (no writeback) |
| `_XP` | `inout ptr b` + reg | `addr = base_AR ; base_AR += REG` (post-inc) |
| `_PP` | (LA/SA prime) | prime the `valign` align-register from `base_AR` |
| `_PPXU` | prime + reg | prime + `base_AR += REG` |
| `_FP` | (SA flush) | flush the partial-store align tail |

The `valign` ctype is the 4-entry align-register file; `LA*`/`SA*` (aligning load/store) are the
unaligned front-end built on the same reference with `RotateAmount` = the held rotate residue.

### 3.3 Key invariants

* **Align-down + within-line funnel.** The reference aligns the address *down* to the natural
  element width, fetches the aligned line, and extracts via the funnel shift `shiftsel = addrsel·8`.
  Straddling accesses fetch a second line at `aligned_vaddr + issue_bytes` and merge
  `(lv0 >> shiftsel) | (lv1 << (issue_bits − shiftsel))` for little-endian (big-endian is the
  mirror with the shift arms swapped). When `addrsel == 0` only `lv0` is used. `[HIGH/OBSERVED]`
* **Signed load = sign-splash; unsigned load = zero-fill.** `extended_val = (val | (sign_splash &
  sign_mask)) & zero_mask`. For a signed dtype `SignExtendFrom` is present and the bytes above the
  natural width get the sign bit; for unsigned, `sign_mask = 0` so they stay 0. `[HIGH/OBSERVED]`
* **Store truncates.** `mem_data = (MemDataOut >> nx_store_shift) & ~zero_mask` drops the bytes
  above `issue_bytes` — a positional truncate, no numeric cast. `[HIGH/OBSERVED]`
* **Dual LSU.** Both reference functions instantiate two pipes (`loaded_value0/1`,
  `store_val0/1`); the ISA declares one `funcUnit XT_LOADSTORE_UNIT` with `num_copies=2`; a normal
  access uses one pipe (the second materializes only for an unaligned tail), while the 5 `L2A*`/
  `L2U*` 2-vector loads (`ivp_l2a4nx8_ip`, `ivp_l2au2nx8_ip`, `ivp_l2au2nx8_ipi`, `ivp_l2u2nx8_xp`,
  `ivp_l2au2nx8_xp`) consume **both** pipes in one issue. `[HIGH/OBSERVED — three sources agree]`
* **`xtms_aligned_load` is the uninterpreted memory seam.** It does not model RAM contents — it is
  the named hook (`byte_width[6:0]` up to 64 bytes; `MemDataOut[1023:0]` padded for the split
  write) the simulator replaces with a real 64-byte line fetch. The interesting math is the
  surrounding wrapper. `[HIGH/OBSERVED]`

### 3.4 Live-driven representative value — the alignment funnel

The funnel itself is executable in `libfiss-base` as `wideldshift_16_512_6` — `arg2` = a pointer to
the 512-bit line, `arg3` = the funnel shamt. It extracts a 16-bit element from the line, performing
the cross-word `(word[i] >> cl) | (word[i+1] << (32−cl))` merge when the element straddles a 32-bit
word boundary:

```text
# line = [word0=0xBBBBAAAA, word1=0x1111EEEE, ...]  (32 contiguous 16-bit halfwords)
# module__xdref_wideldshift_16_512_6 @0x857410
shamt= 0 -> 0xaaaa    shamt= 1 -> 0xaaaa     # halfword[0] = low16 of word0
shamt= 2 -> 0xbbbb    shamt= 3 -> 0xbbbb     # halfword[1] = high16 of word0
shamt= 4 -> 0xeeee    shamt= 5 -> 0xeeee     # halfword[2] = low16 of word1 (CROSS-WORD)
# verified against the pure model halfword[shamt>>1] for shamt 0..11 — ALL MATCH.

# the extend post-op: module__xdref_sext_32_16 @0x858680 vs zeroext_32_16 @0x870e90
sext_32_16   (0xFFFF) -> 0xffffffff           # signed load:   sign-splash
zeroext_32_16(0xFFFF) -> 0x0000ffff           # unsigned load: zero-fill
```

> **QUIRK — the funnel resolves at *element* granularity, not byte granularity.** The `_16_512_6`
> leaf decodes its shamt as `in_bits = shamt << 3 ; half_index = (in_bits & 0x1f0) >> 5`, then
> `sub = in_bits & 0x10` selects the high vs low 16-bit half of the indexed 32-bit word. The net
> effect is that the funnel returns **halfword `(shamt >> 1)`** of the line: `shamt = 0` and
> `shamt = 1` both return halfword 0. This is correct hardware behavior — a 16-bit-element load is
> already element-aligned, so the funnel granularity *is* the element. An adversarial byte-granular
> model mispredicted `shamt = 1 → 0xBBAA`; the **oracle is right and the model was wrong** — the
> funnel never produces an unaligned byte slice for an element-width access. `[HIGH/OBSERVED —
> corrected against the live leaf]`

See [`iss/cas-load-store.md`](../../iss/cas-load-store.md) for the cas `wideldshift_<W>_512_6`
cross-word join and the line-mask layer that composes with this element funnel.

---

## 4. Group 4 — Gather / Scatter + Permute (`ivp_sem_vec_scatter_gather` / `xdsem_tiesel_5_32`)

Batch [B19](../ref/b19-scatter-gather.md) (scatter/gather). Two reference computes: the
gather/scatter index plane (base-DB `<SEMANTIC name="ivp_sem_vec_scatter_gather">`, decoded byte
`34,462,563`, `49,589 B`, 24 opcodes) and the permute/select crossbar (the
`xdsem_tiesel_5_32` lane mux, hosted in `ivp_sem_vec_select`, byte `34,195,174`, `126,551 B`). The
gather **straddles**: the index/control/predicate/data marshaling is the base-DB compute, while the
per-element memory access is delegated to the msem SuperGather host `<INTERFACE>` ports — the
gather analogue of the load black-box.

### 4.1 The shared datapath structure — the index→element mapping

The reference does *not* read `table[addr]`; it computes the per-lane address components and hands
them to a 4-port host interface. The bit-precise marshaling as annotated C:

```c
// ivp_sem_vec_scatter_gather — the SuperGather index plane (base-DB compute).
struct GSPorts {
    u32  VAddrBase;       // = ars  (the AR scalar base = table base)        [31:0]
    u512 GSVAddrOffset;   // = the Voff vector: 16 lanes x 32-bit per-lane offset
    u16  GSControl;       // {8'd0, elem_sz[1:0], offst_sz[1:0], operation[3:0]}
    u512 GSEnable;        // op_pred ? vbr_in : ALL_ONES   (per-lane predicate)
    u512 ScatterData;     // ivp_sem_scatter_mux(vr, ...)  (width-muxed store data)
};
static struct GSPorts gather_marshal(u32 ars, u512 voff, bool op_pred, u512 vbr_in,
                                     int operation /*1..6*/, int elem_sz /*0/1/2*/) {
    struct GSPorts p;
    p.VAddrBase     = ars;
    p.GSVAddrOffset = voff;                              // addr[lane] = base + Voff[lane]
    p.GSControl     = (elem_sz << 4) | (offst_sz << 2) | operation;  // 16-bit control word
    p.GSEnable      = op_pred ? vbr_in : ALL_ONES;       // predicated forms gate by vbool
    // the HOST applies: addr = base + Voff[lane] * elem_size ; table[addr] (the black-box)
    return p;
}
```

`GSControl` bit-layout (the constants are read from the TIEsel arms):

* `operation_fld[3:0]` = `{gathera:1, gatherd:2, mgatherd:3, scatter:4, scatterw:5, scatterinc:6}`.
* `elem_sz_fld[1:0]` = `(op_nx8|op_2nx8)→0 (8-bit)`, `(op_scatterw|op_nx16)→1 (16-bit)`,
  `op_n_2x32→2 (32-bit)` — the **`elem_sz` scaling** the host multiplies the offset by
  (`NX16 = 2`, `NX8U = 1`, `N_2X32 = 4` bytes per element).
* `offst_sz_fld[1:0]` = `(op_n_2x32 & ~op_scatterw) ? 1 : 0` — the offset-width selector.

The permute crossbar is `xdsem_tiesel_5_32`: a **5-bit one-hot 32-way multiplexer** (`ARG_OUT
z[31:0]`; 32 candidate 32-bit lanes, each with a one-hot select). Per output lane the 5-bit index
picks one of 32 source lanes — `z = src[idx]`. The select SEMANTIC instantiates it ×2 for the dual
index halves.

### 4.2 Per-op parameterization

| op / opcode | `operation_fld` | reference | predicate |
|---|---|---|---|
| `IVP_GATHERAN_2X32` (post) | 1 | stage `{base, Voff, pred}` into `gt` | `op_pred` |
| `IVP_GATHERDNX16` (drain) | 2 | drain gathered lanes → vec | — |
| `IVP_GATHERDNX8S` (signed drain) | 2 | drain + sign-extend (`gd_even_sext`) | — |
| `IVP_MOVGATHERD` | 3 | gr→gr move (`gt_out = op_mgatherd ? vs : 0`) | — |
| `IVP_SCATTERNX16` | 4 | `data[lane] → base+Voff[lane]` | `op_pred` |
| `IVP_SCATTERW` | 5 | word-granular, completion-deferred | — |
| `IVP_SCATTERINCNX16` | 6 | `table[base+Voff] += val` (`ScatterData` carries `16'b1`) | `op_pred` |

Permute opcodes route through `xdsem_tiesel_5_32`: **`SHUFFLE`** `dst[i] = src[idx[i]]` (1 source,
`log2(N)`-bit index); **`SELECT`** `dst[i] = {srcB++srcA}[idx[i]]` (2 sources, `log2(2N)`-bit index
— the extra bit picks the source vector); **`DSEL`** = two independent permutations of one source
pair (dual output); **`DCMPRS`** = the compress/de-interleave variant. The sub-byte immediate
`sel2nx8i_s0/s2/s4` read a 7-bit LUT (`tab_selimm_7b` / `tab_shflimm_7b`); `_s4` is the 4-bit
sub-byte unpack the cptc de-interleave uses. The `_t` forms gate each output lane with the
per-lane vbool kill.

### 4.3 Key invariants

* **Gather address = base + per-lane 32-bit offset, `elem_sz`-scaled.** `VAddrBase = ars`,
  `GSVAddrOffset` = the 16×32-bit `Voff` vector; the host computes
  `addr = base + Voff[lane] * elem_size` with `elem_size ∈ {1, 2, 4}` bytes from `elem_sz_fld`.
  `[HIGH/OBSERVED]`
* **The OOB sentinel `0xffffffff` is host-applied, not in the TIE block.** The reference emits only
  `GSEnable` (the per-lane predicate); the *host* applies the miss policy
  (`IMMEDIATE_WRITE`/`SKIP_WRITE`) to lanes where `GSEnable = 0`, filling them with the
  `0xffffffff` sentinel. The bound compare (`ivp_ltun`/`leun_2x32 < bound`) is *upstream* — the
  caller builds `vbr`; the gather op only *consumes* it. `[HIGH that the TIE stops at the port;
  the sentinel/bound are CARRIED]`
* **Scatter-add is the histogram primitive.** `op_scatterinc` carries a `16'b1` increment lane so
  `table[base+Voff] += val` runs in place (the `DGE_COMPUTE_OP ADD` reduce). `[HIGH/OBSERVED]`
* **Permute is a mux net, not arithmetic.** The `scatter_gather`/`select` blocks have no `<ADD>` or
  `<MUL>`; the index×stride arithmetic is the host's, and the lane routing is the `tiesel` +
  `bitkill` MODULEs. `[HIGH/OBSERVED]`

### 4.4 Live-driven representative value — the predicate / enable mask

The per-lane enable the gather emits (`GSEnable`) and the predicated-select lane kill are the same
`bitkillf` primitive. Driving it shows the exact mask — and shows that the OOB sentinel is *not*
what the value oracle produces (it produces the enable; the host applies the sentinel):

```text
# module__xdref_bitkillf_16_2 @0x82d000 : shl 31 ; sar 31 ; and 0xffff ; mov (%rdx)
bitkillf_16_2(pred=1) -> 0x0000ffff           # lane ENABLED  (GSEnable bit set)
bitkillf_16_2(pred=0) -> 0x00000000           # lane MASKED   (OOB / predicate off)

# module__xdref_bitkillf_32_4 @0x82d010 : full 32-bit lane width
bitkillf_32_4(pred=1) -> 0xffffffff           # enabled 32-bit lane
bitkillf_32_4(pred=0) -> 0x00000000           # masked   32-bit lane
```

The mask is `0x0000ffff` / `0xffff_ffff` (lane width) when enabled and `0x0` when masked — the
TIE-level `GSEnable` plane. The host's response to a `0x0` lane (the `0xffffffff` miss sentinel) is
the *next* stage and is correctly not produced here. See
[`iss/cas-supergather.md`](../../iss/cas-supergather.md) for the cas decode/timing and fiss
per-lane `bitkillf` value path, and [B19](../ref/b19-scatter-gather.md) for the full 24-opcode
roster.

---

## 5. Adversarial self-verification ledger

Five of the strongest semantic claims were each driven against the live `xdref` oracle and a
verdict recorded; failures were corrected against the oracle (not the other way round).

| # | Claim | Live test | Verdict |
|---|---|---|---|
| ADV-1 | The load alignment funnel does `(lv0>>shamt)\|(lv1<<(bits−shamt))` | `wideldshift_16_512_6` over a 2-word line, shamt 0..11 | **PASS** — matches the halfword model after correcting the granularity (see QUIRK §3.4); the *byte-granular* model was wrong, the oracle is element-granular |
| ADV-2 | The i16 MAC accumulator is wider than 32 bits (48-bit `wvec` lane) | `mula(acc=-6, 10000, 10000) = 0x05f5e0fa` = 99,999,994 | **PASS** — a 27-bit result survives the RMW across the 32-bit boundary |
| ADV-3 | The gather OOB `0xffffffff` is host-applied; the TIE emits only `GSEnable` | `bitkillf_16_2(0) = 0x0` (mask), `(1) = 0xffff` (enable) | **PASS** — the oracle produces the enable mask, never the sentinel; not over-claimed |
| ADV-4 | `AVGR = (a+b+1)>>1` round-half-up vs `AVG = (a+b)>>1` floor | `avg(3,4)=3`, `avgr(3,4)=4`; `avg(-4,-2)=-3` signed | **PASS** — round-up on odd sum; signed 17-bit intermediate |
| ADV-5 | Signed `MIN` and unsigned `MINU` are *distinct* bodies, not a toggle | `min(-1,1)=0xffff` vs `minu(0xffff,1)=0x0001` | **PASS** — different opcodes give different results on the same bits |

> **CORRECTION (raised by ADV-1).** The funnel claim was initially stated at *byte* granularity. The
> live `wideldshift_16_512_6` leaf proves the funnel for an element-width access resolves at
> **element (halfword) granularity** — `shamt = 0` and `shamt = 1` both return halfword 0. The page
> body (§3.4 QUIRK) reflects the corrected, oracle-verified semantics: a 16-bit-element load never
> returns an unaligned byte slice. No other claim required correction. `[HIGH/OBSERVED]`

---

## 6. Cross-page reconciliation and honesty ledger

* **Operand-naming layer (load/store).** The cas/fiss internal mnemonics are `LSNX16`/`SSNX16`; the
  TIE-DB / IVP intrinsic names are `IVP_LVNX16`/`IVP_SVNX16`. This is an intrinsic-vs-internal
  naming difference (the folded runtime renames), **not** a semantic divergence — the operand
  decode and the reference compute match. `[HIGH]`
* **The `iss/cas-*.md` cross-links are forward references** to the planned Part-14 ISS pages
  ([SUMMARY](../../SUMMARY.md) Part 14). They are the cas/fiss companions to this TIE-source page;
  this page is the *reference math*, those are the *executable-model* views of the same ops.
* **Where this page stops.** It does not model `table[addr]` (the host memory plane), the fp
  round-tie-break table (the [fp-rounding slice](group-semantics-ii.md)), the exact `MINORMAX` dual
  output packing, the `BADDNORM`/`BSUBNORM` norm arithmetic, or the per-immediate gather miss-fill
  policy — these are explicitly deferred, not guessed. `[stated boundary]`

| confidence | claims |
|---|---|
| **HIGH / OBSERVED** | the four shared-datapath structures; the `op_<GROUP>` OR-reduction decoder grammar; SUB = `a+~b+1`; wrap-via-lane-mask; signed/unsigned-via-extension; the saturation clamp constants `0x7fff`/`0x8000`; `AVG`/`AVGR` `>>1` (+1); the 48-bit `wvec` accumulator (live-chained); the i8→24 / i16→48 / i32→96 widening matrix (triple-confirmed); the align-down funnel + sign/zero-extend + truncate; the dual-LSU (three sources); the `GSControl` field layout; the `elem_sz` scaling; `GSEnable` predicate; all five live oracle values |
| **MED / INFERRED** | the fp FMA exact round-tie-break (structure observed, the `RoundMode`→tie-break table deferred); the `DECNEG` exact partial-product fold; the per-cell PE-array micro-op→slice binding; the exact MX-scale coupling |
| **CARRIED** | the gather `0xffffffff` miss sentinel and 4096-index bound (host/firmware facts, not in the TIE block); the 8-core/16-partition SBUF geometry; the fp16/fp32 round-mode SR wiring |

---

*Provenance: shipped `libtie-core.so` + `libtie-Xtensa-msem.so` `post_rewrite` TIE semantics
(`.data` blob, file offset = VMA − `0x200000`), the `libfiss-base.so` `module__xdref_*` value
leaves driven live via `ctypes` (`.text` VMA = file offset), and `libcas-core.so` decode/timing
tags. No silicon-generation fact is inferred from any TIE descriptor: every width/lane/rail token
(`8Mbit`, `M0..M7`, `2NX8`/`NX16`/`N_2X32`, `wvt`/`wvu`, `_s0/_s2/_s4`, `operation_fld`) is a
config / datapath / lane axis of the one Cairo config (`Xm_ncore2gp`, `Xtensa24`, `NX1.1.4`,
`RI-2022.9`), not one of the firmware-image generations.*
