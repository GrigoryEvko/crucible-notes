# ISA Batch 24 — Histogram / Squeeze / QLI / FCR-FSR (the vector-subtotal close)

This is the **final IVP-vector batch** of the Vision-Q7 *Cairo* (`ncore2gp`) 512-bit FLIX vector ISA —
the one that **closes the B01–B24 vector roster**. Unlike a clean single-`<SEMANTIC>` batch, Batch 24
is a **composite of six disjoint semantic groups** swept up to balance the 30-batch partition declared in
[the template & partition page](template-and-partition.md): the leftover lane-counting, stream-compaction,
high-precision reciprocal-refine, floating-point control-register, and modular-address ops that no earlier
batch owned. **24 opcodes** total, split `8 + 6 + 5 + 2 + 2 + 1`:

* **`ivp_sem_vec_histogram` (8)** — `COUNTEQ`/`COUNTLE` `4NX8` single-bin sweep + the `M` (masked) / `Z`
  (zero-init-bin) / `MZ` variants. A cross-lane compare-and-reduce-count that advances a bin index carried
  in an AR.
* **`ivp_sem_sqz` (6)** — `SQZ` (squeeze / predicate-driven stream compaction) and `UNSQZ` (the inverse
  scatter), each at `2N`/`N`/`N_2` element width. A two-stage prefix-popcount gather/scatter.
* **`bbn_sem_vec_sprecip_rsqrt` (5)** — `RECIPQLI`, the **quadratic-interpolation refine** that upgrades the
  8-bit transcendental reciprocal seed to full fp32 precision (scalar `.S` + two vector segments + two
  predicated `T` segments).
* **`ivp_sem_rur_fcr_fsr` (2)** — `RUR.FCR` / `RUR.FSR`: read the floating-point **control** (round mode +
  exception enables) and **status** (sticky flags) registers into an AR.
* **`ivp_sem_wur_fcr_fsr` (2)** — `WUR.FCR` / `WUR.FSR`: the inverse write.
* **`ivp_sem_addmod` (1)** — `IVP_ADDMOD16U`: a scalar AR `(a + b) mod 2^16` wrap-add, used to advance a
  circular-buffer pointer beside a streaming load.

The six groups have **zero cross-membership** with each other or with the preceding boundary batch
[B21 (select / shuffle / compress)](b21-select-shuffle.md) — the `count*` / `*sqz*` / `recipqli*` /
`*ur*fcr*` / `addmod16u` member sets are mutually exclusive name spaces in the encode-thunk table.

Everything below is re-grounded against the shipped binaries **this pass**: the **encoding** from
`libisa-core.so` (the `Opcode_<mnem>_Slot_<slot>_<unit>_encode` thunks read byte-for-byte, each body being a
single `movl $imm,(%rdi); ret` that writes the slot-local instruction word), the **value semantics** by
*executing* the matching `module__xdref_*` and `opcode__*__stage_*` leaves in `libfiss-base.so` live
in-process, the **issue timing** from the per-op `_inst_stage*` / `_issue` scoreboard leaves in
`libcas-core.so`, and a byte-exact **encode/decode oracle** from the device-native
`xtensa-elf-as`/`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). Every one of the 24 mnemonics was
round-tripped through that device oracle this pass.

Confidence tags per the project model: `[HIGH/OBSERVED]` = read-from-byte / proven-by-execution,
`[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` = re-used at a sibling page's confidence.

> **NOTE — address arithmetic re-confirmed this pass.** `libisa-core.so` (9 690 712 B, ET_DYN x86-64,
> not stripped). `readelf -SW` this pass: `.text` (VMA `0x312c10` ↔ file `0x312c10`) and `.rodata` (VMA
> `0x3b6e40` ↔ file `0x3b6e40`) are **VMA == file-offset** — so the `objdump -d` of the encode thunks below
> reads the live bytes directly. `.data` (VMA `0x764040` ↔ file `0x564040`) carries the per-binary delta
> **`0x200000`** — **not** libtpu's `0x400000` — so any walk of the `Opcode_*_args` operand-descriptor
> leaves in `.data` must subtract `0x200000`. All config DLLs are under
> `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/` (gitignored; reach with `fd --no-ignore` or an
> absolute path). `[HIGH/OBSERVED]`

---

## 0. Roster and count verification `[HIGH/OBSERVED]`

The authoritative roster is the set of distinct mnemonics that own at least one encode thunk. Stripping
`Opcode_(.*)_Slot_…_encode` to its mnemonic and de-duplicating gives the global ISA totals, which match the
ISA-wide invariants exactly:

```
nm libisa-core.so | rg -o 'Opcode_(.*)_Slot_[^ ]*_encode' -r '$1' | sort -u | wc -l   # 1534  total mnemonics
… | rg -c '^ivp_'                                                                       # 1065  ivp_ (vector)
… | rg -vc '^ivp_'                                                                      #  469  scalar
nm libisa-core.so | rg -c '_Slot_[^ ]*_encode'                                          # 12569 (opcode × slot) placements
```

So **1534 = 469 scalar + 1065 vector**, **12569 placements** — the figures every batch page reconciles
against. The 24 Batch-24 mnemonics, with their per-mnemonic placement counts (`nm … | rg -c
'Opcode_<m>_Slot_[^ ]*_encode'`), are:

| group | mnemonic | prefix | placements | slot / unit |
|---|---|---|---|---|
| histogram | `ivp_counteq4nx8` | `ivp_` | 8 | F0/F11/F1/F2/F3/F4/F6/F7 · S3 ALU |
| histogram | `ivp_counteqz4nx8` | `ivp_` | 8 | F0/F11/F1/F2/F3/F4/F6/F7 · S3 ALU |
| histogram | `ivp_countle4nx8` | `ivp_` | 8 | F0/F11/F1/F2/F3/F4/F6/F7 · S3 ALU |
| histogram | `ivp_countlez4nx8` | `ivp_` | 8 | F0/F11/F1/F2/F3/F4/F6/F7 · S3 ALU |
| histogram | `ivp_counteqm4nx8` | `ivp_` | 4 | F0/F1/F4/F7 · S3 ALU |
| histogram | `ivp_counteqmz4nx8` | `ivp_` | 4 | F0/F1/F4/F7 · S3 ALU |
| histogram | `ivp_countlem4nx8` | `ivp_` | 4 | F0/F1/F4/F7 · S3 ALU |
| histogram | `ivp_countlemz4nx8` | `ivp_` | 4 | F0/F1/F4/F7 · S3 ALU |
| sqz | `ivp_sqz2n` / `ivp_sqzn` / `ivp_sqzn_2` | `ivp_` | 9 each | F0/F11/F1/F2/F3/F4/F6/F7/N0 · S3 ALU |
| sqz | `ivp_unsqz2n` / `ivp_unsqzn` / `ivp_unsqzn_2` | `ivp_` | 9 each | F0/F11/F1/F2/F3/F4/F6/F7/N0 · S3 ALU |
| recip | `ivp_recipqlin_2xf32_0` / `_1` | `ivp_` | 6 each | F0/F1/F2/F3/F7/N0 · S3 ALU |
| recip | `ivp_recipqlin_2xf32t_0` / `t_1` | `ivp_` | 5 each | F0/F1/F2/F7/N0 · S3 ALU |
| recip | `recipqli_s` | *scalar* | 6 | F0/F1/F2/F3/F7/N0 · S3 ALU |
| rur | `rur_fcr` / `rur_fsr` | *scalar* | 6 each | F0/F1/F2/F3/F7/N0 · S3 ALU |
| wur | `wur_fcr` / `wur_fsr` | *scalar* | 1 each | bare-narrow (`Slot_inst`) |
| addmod | `ivp_addmod16u` | `ivp_` | 7 | F0/F1/F2/F3/F4/F6/F7 · **S1 Ld** |

> **CORRECTION — the prefix split is 19 + 5, not 24 vector.** Of the 24 Batch-24 mnemonics, only **19** carry
> the `ivp_` prefix and are therefore members of the **1065-op vector subtotal** (8 histogram + 6 sqz + 4
> vector `ivp_recipqlin_*` + 1 `ivp_addmod16u`). The other **5** — `recipqli_s`, `rur_fcr`, `rur_fsr`,
> `wur_fcr`, `wur_fsr` — are **non-`ivp_`** mnemonics that live in the **469 scalar** roster (the `_args`
> leaf search alone returns 1064 `ivp_` because `ivp_scatterw` has encode thunks but no `_args` leaf; the
> encode-thunk strip is the authoritative 1065). Note in particular that `ivp_addmod16u` is *semantically* a
> scalar AR op (no `CPENABLE`, no vector regfile), yet it is `ivp_`-**prefixed** and therefore counts toward
> the vector tally — a naming/semantics mismatch that any tally script must respect.
> `[HIGH/OBSERVED — every prefix classified from the thunk table this pass]`

The 24-op split and the `8+6+5+2+2+1` partition are both nm-verified this pass:

```
… mnemonics matching count(eq|le) | (un)?sqz | recipqli | (rur|wur).*(fcr|fsr) | addmod
 → 24 total ; 19 ivp_-prefixed ; 5 scalar
```

---

## 1. Common encoding & ABI model `[HIGH/OBSERVED]`

The 19 vector ops of Batch 24 (histogram 8, sqz 6, vector recip 4 — and `recipqli_s`, `rur*`, which are
scalar but still vector-pipe) share the vector-coprocessor contract: `STATE_IN = CPENABLE` (sampled at USE
stage 3) with `EXC = Coprocessor1Exception` raised *before* the datapath effect if the cp1-enable bit is
clear (the op is squashed). The three genuinely-base ops — `wur_fcr`, `wur_fsr` (bare-narrow) and
`ivp_addmod16u` (LSU slot) — differ and are called out at their entries.

Each encode thunk is the trivial body `movl $<word32>,(%rdi); ret` (the four-byte slot-local instruction
word, little-endian) followed by a `movl $0,0x4(%rdi)` zero for the high word. The slot field map for the
**F0 / S3 ALU** placement (the widest, 15-bit base selector) is the reference; narrower formats (F11/F3
7-bit, F2 11-bit, F1/F4/F7 12–13-bit, F6 14-bit) re-pack the same fields into fewer bits, and the assembler
oracle resolves each. The two operand-bearing register fields the histogram/sqz/recip share at F0/S3:

| field | bits (F0/S3) | role |
|---|---|---|
| selector | `[34:20]` (15-bit) | the opcode discriminator (the `0x86600000`-class CONST in the thunk) |
| `vt` (OUT) | `[19:15]` | destination vector |
| `vs` (IN2) | `[14:10]` | second source vector |
| bit `[9]` | `[9]` | the **Z** init-bin discriminator (histogram) |
| `vr` (IN1) | `[8:4]` | first source vector (note the unusually low position) |

---

## 2. Group 1 — `ivp_sem_vec_histogram` (COUNTEQ / COUNTLE 4NX8) `[HIGH/OBSERVED]`

### 2.1 Role and operands

A single-bin histogram sweep over a 64×`i8` (`2NX8`) vector. Each call compares all 64 lanes against the
**current bin value** (carried in `ars`, an AR, `INOUT`), optionally masks the lanes with a `vbool` pair,
cross-lane reduce-ADDs the surviving match flags to produce the **bin count** in `vt`, and **advances** the
bin index for the next call. A full histogram is a loop of *N* calls sweeping bins `0..N-1`, threading `ars`.

Assembler operand order (oracle-confirmed): **`vt, ars, vr, vs [, vbs, vbr]`**.

```
IVP_COUNTEQ4NX8    OUT[vt:vec512]  INOUT[ars:AR]  IN[vr:vec, vs:vec]
IVP_COUNTEQZ4NX8   …  (Z: bin forced to 0 — the histogram restart)
IVP_COUNTLE4NX8    …  (LE instead of EQ)
IVP_COUNTLEZ4NX8   …  (LE + Z)
IVP_COUNTEQM4NX8   OUT[vt]  INOUT[ars]  IN[vr, vs, vbs:vbool, vbr:vbool]   (M: masked)
IVP_COUNTEQMZ4NX8  …  (M + Z)
IVP_COUNTLEM4NX8   …  (LE + M)
IVP_COUNTLEMZ4NX8  …  (LE + M + Z)
```

### 2.2 Encoding — the selector lattice

The non-`M` variants carry the **15-bit base selector** in `[34:20]`; `EQ` vs `LE` is the selector LSB
`0x866` vs `0x867`, and `Z` is bit `[9]`. The encode thunks read byte-for-byte this pass:

```
Opcode_ivp_counteq4nx8_Slot_f0_s3_alu_encode :  movl $0x86600000,(%rdi)   ; sel[34:20]=0x866, bit9=0
Opcode_ivp_counteqz4nx8_Slot_f0_s3_alu_encode:  movl $0x86600200,(%rdi)   ; + bit9 (0x200) = Z
Opcode_ivp_countle4nx8_Slot_f0_s3_alu_encode :  movl $0x86700000,(%rdi)   ; sel LSB 0x866→0x867 (bit20)
```

The four **`M`** variants use a *distinct short selector* in `[34:27]` (the wide formats free up `[22:20]`
`++[9]` for `vbs` and `[26:23]` for `vbr`):

```
Opcode_ivp_counteqm4nx8_Slot_f0_s3_alu_encode :  movl $0x40000000,(%rdi)   ; [34:27]=0x8  (EQM)
Opcode_ivp_counteqmz4nx8_Slot_f0_s3_alu_encode:  movl $0x48000000,(%rdi)   ; [34:27]=0x9  (EQMZ)
Opcode_ivp_countlem4nx8_Slot_f0_s3_alu_encode :  movl $0x50000000,(%rdi)   ; [34:27]=0xa  (LEM)
Opcode_ivp_countlemz4nx8_Slot_f0_s3_alu_encode:  movl $0x58000000,(%rdi)   ; [34:27]=0xb  (LEMZ)
```

(`0x40000000 >> 27 = 0x8`, `0x48… = 0x9`, `0x50… = 0xa`, `0x58… = 0xb` — the EQM/EQMZ/LEM/LEMZ ladder.)

### 2.3 Semantics — bit-precise C pseudocode

The compute is a masked lane-compare feeding the same 3-level carry-save reduce-ADD tree the cross-lane
`radd` reductions use (the `ivp_sem_csa_8_16_32_l0/l1/l2 → ivp_sem_reduce_stage1` network; see
[ISS arith semantics](../../iss/cas-arith-sem.md) and the reduce tree documentation). `[HIGH/OBSERVED]`

```c
// ivp_sem_vec_histogram :: per call, 64×i8 input
//   real reduce kernel: ivp_sem_csa_8_16_32_l0_f → _l1_f → _l2_f → ivp_sem_reduce_stage1
uint16_t ivp_counteq4nx8(uint8_t vr[64], uint8_t vs[64],   // the 64 data lanes (vr/vs operand)
                         uint3_t  *ars,                     // INOUT: current bin index (AR, low 3 bits)
                         bool use_le, bool init_bin_zero,   // op_le (LE vs EQ), op_z (Z suffix)
                         bool use_mask, vbool vbs, vbool vbr,// M-suffix gate (two vbool operands)
                         unsigned sa)                       // bin-scale right-shift
{
    uint3_t cur_bin = init_bin_zero ? 0u : (*ars & 0x7u);  // Z forces bin 0 (histogram restart)
    unsigned match = 0;
    for (int k = 0; k < 64; ++k) {
        uint8_t d  = (vr_or_vs(vr, vs, k) >> sa);           // bin-scale the lane
        bool    eq = use_le ? (d <= cur_bin) : (d == cur_bin);
        bool    m  = use_mask ? lane_bit(vbs, vbr, k) : true; // M-suffix vbool gate
        match += (eq & m);                                  // 1-bit match flag
    }
    // cross-lane reduce-ADD of the 64 one-bit flags (the CSA Wallace tree → carry-propagate)
    uint16_t count = (uint16_t)match;                       // ≤ 64, no saturation needed
    *ars = (cur_bin + 1u) & 0x7u;                           // bin advances → written back (INOUT)
    return count;                                           // broadcast/placed into vt
}
```

`COUNTEQ` counts lanes **equal** to the bin; `COUNTLE` counts lanes **≤** the bin; `M` gates by the `vbool`
pair; `Z` restarts the sweep at bin 0. No saturation, no rounding, no IEEE exceptions — only
`Coprocessor1Exception` on cp1-disable.

### 2.4 Timing `[HIGH/OBSERVED]`

`libcas-core.so` carries the F0/S3 scoreboard as `IVP_COUNTEQ4NX8_inst_stage0 … _stage12` — **13 stages**.
Per the `INSTR_SCHEDULE` USE→DEF deltas: `ars@3`, `vr@10`, `vs@10`, `CPENABLE@3` (`+ vbs@10, vbr@10` for the
`M` forms); the reduce stage-1 emits `@10`, and `vt` is available `@12` (≈2-cycle result latency with staged
bypass). The `ars` bin advance is an early (`@3`) scalar update, so the next call can fetch the new bin
without stalling on the reduce.

### 2.5 Worked bit-pattern — `IVP_COUNTEQ4NX8 v3, a6, v1, v2`

Field-fill on top of the base CONST `0x86600000`: `vt=3 → [19:15]` (`0x18000`), `vs=2 → [14:10]` (`0x800`),
`vr=1 → [8:4]` (`0x10`), giving slot word `0x86618810`. The device assembler emits the full 16-byte F0
bundle (the op in slot S3, the other three slots `nop`):

```
ivp_counteq4nx8  v3, a6, v1, v2  →  0009191848cc83009400a2b08504452f
   decode: { nop; nop; nop; ivp_counteq4nx8  v3, a6, v1, v2 }
ivp_counteqz4nx8 v3, a6, v1, v2  →  0009191848cc83009408a2b08504452f
   (the only delta is byte …9400→…9408 = bit[9] set — the Z init-bin-zero discriminator)
ivp_countle4nx8  v3, a6, v1, v2  →  0009191848ce83009400a2b08504452f
   (the only delta is …48cc83→…48ce83 = selector LSB 0x866→0x867 — EQ→LE)
ivp_counteqm4nx8 v3, a6, v1, v2, vb1, vb2  →  0005190448c083009408a2b08504452f
   (swapping vb1↔vb2 perturbs the scattered vbs[22:20]++[9] / vbr[26:23] fields, confirming
    vbs = 1st vbool operand, vbr = 2nd)
```

All four byte strings were produced by `xtensa-elf-as` this pass and are byte-identical to the device-native
encoding. `[HIGH/OBSERVED — round-trip oracle]`

---

## 3. Group 2 — `ivp_sem_sqz` (SQZ / UNSQZ stream compaction) `[HIGH/OBSERVED]`

### 3.1 Role and operands

Predicate-driven vector **stream compaction**. `SQZ` (squeeze) gathers the predicate-true lanes into a
contiguous prefix; `UNSQZ` (un-squeeze) is the inverse scatter. Both are two-stage prefix-popcount +
gather/scatter. The width suffix is the gather/scatter element size: `2N` = 64×`i8`, `N` = 32×`i16`, `N_2`
= 16×`i32`.

All six share the signature `OUT[vt:vec512]  OUT[arr:AR]  IN[vbr:vbool]`, with `arr` receiving the **count
of predicate-true lanes** (the new packed length / squeeze pointer). Assembler operand order:
**`vt, arr, vbr`**.

### 3.2 Encoding

The SQZ/UNSQZ direction and the width are *not* one contiguous selector — they are split CONST fields per
format. At F0/S3 the member words read this pass:

```
Opcode_ivp_sqz2n_Slot_f0_s3_alu_encode  :  movl $0x81000402,(%rdi)
Opcode_ivp_sqzn_Slot_f0_s3_alu_encode   :  movl $0x81000403,(%rdi)   ; [3:0] 2→3 = width 2N→N
Opcode_ivp_sqzn_2_Slot_f0_s3_alu_encode :  movl $0x81000802,(%rdi)   ; [14:8] 0x04→0x08 = N_2
Opcode_ivp_unsqz2n_Slot_f0_s3_alu_encode:  movl $0x81000c02,(%rdi)   ; the op_unsqz direction bits
Opcode_ivp_unsqzn_Slot_f0_s3_alu_encode :  movl $0x81000803,(%rdi)
Opcode_ivp_unsqzn_2_Slot_f0_s3_alu_encode: movl $0x81000c03,(%rdi)
```

The `[34:29]` field is `0x4` for all six (`0x81000402 >> 29 = 0x4`); the `[14:8]` and `[3:0]` fields carry
the (width × direction) tuple. Each has 9 placements (the eight wide formats + the narrow `N0` 8-byte
bundle).

### 3.3 Semantics — two-stage prefix-popcount

```c
// ivp_sem_sqz :: predicate-driven gather (SQZ) / scatter (UNSQZ)
//   stage1: ivp_sem_sqz_stage1 (prefix population-count over vbr)
//   stage2: ivp_sem_sqz_stage2 (gather or scatter by the prefix count)
void ivp_sqz(vec512 src, vbool vbr, bool op_unsqz, unsigned elemW,
             vec512 *vt, ar_t *arr)
{
    int N = 512 / elemW;            // 64 (2N), 32 (N), 16 (N_2)
    int popc[N];
    int running = 0;
    for (int k = 0; k < N; ++k) {   // STAGE 1: prefix popcount — packed dest index of lane k
        popc[k] = running;          //   = number of true lanes with index < k
        running += vbool_lane(vbr, k);
    }
    for (int k = 0; k < N; ++k) {   // STAGE 2: gather / scatter
        if (!op_unsqz) {            //   SQZ  : compact true lanes to a contiguous prefix
            if (vbool_lane(vbr, k))  lane_set(vt, popc[k], lane_get(src, k, elemW), elemW);
        } else {                    //   UNSQZ: scatter packed prefix back to true-lane positions
            if (vbool_lane(vbr, k))  lane_set(vt, k,       lane_get(src, popc[k], elemW), elemW);
        }
    }
    *arr = running;                 // total popcount(vbr) = the new packed length
}
```

> **NOTE — the innermost prefix-sum adder-tree topology is one function deeper.** Stage 1 delegates the exact
> balanced lane-pairing of the prefix-popcount to a nested adder-tree the same way the cross-lane reduce
> delegates its CSA wiring — it *is* a prefix sum (the structure, the two stages, the popcount, and the
> direction are all observed), only the silicon lane-pairing of the innermost tree is un-rendered. This is a
> value-determined static-analysis boundary, **not** a missing datapath body. `[HIGH structure / residual NAMED]`

### 3.4 Timing and worked pattern `[HIGH/OBSERVED]`

`IVP_SQZN` scoreboard runs `…_inst_stage0 … _stage12` (result `@12`). USE `vbr@10`, `CPENABLE@3`; DEF
prefix-popcount prefanout `@9`, stage-2 gather `@10`, `vt` + `arr` `@12` (≈2-cycle result latency).

```
ivp_sqzn   v5, a3, vb1  →  325060506040452f   { nop; nop; nop; ivp_sqzn  v5, a3, vb1 }
ivp_sqz2n  v5, a3, vb1  →  325060506000452f   ([3:0] field …6040→…6000 = width N→2N)
ivp_unsqzn v5, a3, vb1  →  325060506200452f   (the op_unsqz inverse — [3:0] flips to scatter)
```

Worked value: with `vb1` selecting lanes 1 and 3 (`…1010`), `IVP_SQZN` gathers `src[1]`, `src[3]` into
`vt[0]`, `vt[1]` (a contiguous prefix) and writes `arr = a3 = 2`; `IVP_UNSQZN` with the same mask scatters a
packed source back to lanes 1, 3. `[HIGH/OBSERVED — round-trip oracle]`

---

## 4. Group 3 — `bbn_sem_vec_sprecip_rsqrt` (RECIPQLI quadratic-interp refine) `[HIGH/OBSERVED]`

### 4.1 Role and the seed→refine pipeline

The higher-precision single-precision (`fp32`, `N_2XF32` = 16×`fp32`) reciprocal / reciprocal-sqrt path. The
GPSIMD computes transcendentals as **8-bit table seed + iterative refine**: the seed op (`RECIP0`/`RSQRT0`)
reads a small ROM table and emits a coarse approximation; `RECIPQLI` then performs a **quadratic-interpolation
(QLI) refine** — it reads piecewise `(A, gx)` coefficient pairs from the `fp_recip_qli_lut` tables and
evaluates a second-order polynomial interpolant, lifting the seed to full `fp32` precision under IEEE
round/exception control.

That seed is real and observable. Driving the seed leaf `module__xdref_recip0_1_1_32f_32f` live this pass
(its internal `table__RECIP_Data8` ROM at `0x958fc0`, reached `lea 0xe082f(%rip)` then indexed
`(%rcx,%r12,4)`):

```
recip0(2.0) → 0x3eff0000 = 0.498047   (true 1/2 = 0.5     — ~8-bit seed, mantissa truncated)
recip0(4.0) → 0x3e7f0000 = 0.249023   (true 1/4 = 0.25)
recip0(1.0) → 0x3f7f0000 = 0.996094   (true 1   = 1.0)
recip0(3.0) → 0x3eaa0000 = 0.332031   (true 1/3 = 0.33333 — ~0.4 % seed error)
```

The ~0.4 % error at `1/3` is exactly the residual the QLI refine corrects. The refine leaf
`module__xdref_recipqli_1_1_1_1_1_32f_32f` confirms the table-driven quadratic structure at the disassembly
level: it extracts the input exponent (`shr $0x17`) and mantissa (`and $0x7fffff`), and — crucially —
**dereferences a caller-supplied coefficient-table pointer** as `(%rdx, %r12, 4)`, i.e. `lut[seg]`. The five
single-bit args of the `recipqli_1_1_1_1_1` name are the five FCR exception **enables**; the `32f_32f` tail
is one `fp32` in, one `fp32` out. `[HIGH/OBSERVED — seed executed live; refine table-index disassembled]`

### 4.2 Members and operands

```
recipqli_s              scalar fp32 QLI refine (the .S single/scalar form)         [non-ivp_ scalar mnemonic]
ivp_recipqlin_2xf32_0   vector fp32 QLI refine, interpolation SEGMENT 0
ivp_recipqlin_2xf32_1   vector fp32 QLI refine, interpolation SEGMENT 1
ivp_recipqlin_2xf32t_0  predicated ('T') segment-0
ivp_recipqlin_2xf32t_1  predicated ('T') segment-1
```

`OUT[vt:vec512]`, `IN[vr:vec]`, plus the IEEE state: `ARG_INOUT` the sticky FSR flags
(`Invalid/Overflow/Underflow/Inexact/DivZero`) and `ARG_IN` the FCR enables; `STATE_IN=CPENABLE`,
`EXC=Coprocessor1Exception`; `T` adds `IN[vbr:vbool]`. The `_0`/`_1` suffixes select the piecewise segment
pair of the `fp_recip_qli_lut{1,2}` tables (the `op_vec_evn`/`op_vec_odd` flops show the `fp32` datapath
running over the two native `fp16` lane-halves; the shared `op_recip`/`op_rsqrt` flops mux the recip vs rsqrt
path). Assembler operand order: **`vt, vr [, vbr for T]`**.

### 4.3 Encoding

The non-`T` selector is `[34:15]` (20-bit, base `0x10905`) with the recip/rsqrt-segment sub-selector in
`[7:4]` and bit `[0]`. The encode thunks read this pass:

```
Opcode_recipqli_s_Slot_f0_s3_alu_encode           :  movl $0x84828050,(%rdi)  ; [34:15]=0x10905, [7:4]=0x5, [0]=0
Opcode_ivp_recipqlin_2xf32_0_Slot_f0_s3_alu_encode:  movl $0x84fa0070,(%rdi)  ; [34:15]=0x109f4, [7:4]=0x7  (seg-0 high sel)
Opcode_ivp_recipqlin_2xf32_1_Slot_f0_s3_alu_encode:  movl $0x84828040,(%rdi)  ; [34:15]=0x10905, [7:4]=0x4
Opcode_ivp_recipqlin_2xf32t_0_Slot_f0_s3_alu_encode: movl $0x869000c1,(%rdi)  ; T: [7:4]=0xc, [0]=1
Opcode_ivp_recipqlin_2xf32t_1_Slot_f0_s3_alu_encode: movl $0x869000d1,(%rdi)  ; T: [7:4]=0xd, [0]=1
```

(`vr` is the 5-bit split field `[9:8]++[3:1]`, MSB-first; `vt` is `[19:15]`. The `T` form narrows the
selector to `[34:19]` and slots the `vbr` predicate in the freed low bits.)

> **CORRECTION — the `T` (predicated) recip forms have 5 placements, not 6.** The non-`T` `ivp_recipqlin_2xf32_0`
> places into `{F0, F1, F2, F3, F7, N0}` (6); the predicated `ivp_recipqlin_2xf32t_0` drops **F3** and places
> into `{F0, F1, F2, F7, N0}` (5). The narrow `T` encoding (which must carry the extra `vbr` field) does not
> fit the F3 7-bit selector format. `[HIGH/OBSERVED — slot lists read this pass]`

### 4.4 Semantics — the QLI refine `[HIGH/OBSERVED structure]`

```c
// bbn_sem_vec_sprecip_rsqrt :: per fp32 lane k
//   r[k] = QLI_refine(vr[k]) under FCR round mode + sticky FSR flags
fp32 QLI_refine(fp32 x, bool is_rsqrt, int seg_pair, fcr_t fcr, fsr_t *fsr) {
    int  seg     = segment(x);                       // piecewise bracket of the mantissa
    int32_t A    = fp_recip_qli_lut_A [seg_pair][seg]; // signed slope/quadratic coefficient
    int32_t gx   = fp_recip_qli_lut_gx[seg_pair][seg]; // base coefficient
    fp32 r       = poly2(gx, A, x - x_seg);          // gx + A·(x - x_seg) (+ quadratic term)
    r = ieee_round(r, fcr.round_mode);               // per FCR RoundMode (see §6)
    // IEEE specials (the fp special-value path; see §5 / the fp sub-ISA page):
    //   recip(0)=±Inf (DivZero) ; recip(Inf)=±0 ; recip(NaN)=qNaN (Invalid)
    //   rsqrt(neg)=qNaN (Invalid) ; rsqrt(0)=+Inf (DivZero) ; subnormal: gradual (no FTZ)
    raise_sticky(fsr, fcr.enables);                  // sticky-OR each flag iff its FCR enable is set
    return r;
}
```

The coefficient ROMs are byte-exact in the binary: `fp_recip_qli_lut1_A` (128×32-bit signed),
`fp_recip_qli_lut1_gx` (128×28-bit), `fp_recip_qli_lut2_{A,gx}` (64-entry second segment), and the
`fp_rsqrt_qli_lut{1,2}_{A,gx}` mirror. Round mode and the sticky-flag gating are the standard fp-control
machinery documented at [the fp sub-ISA page](../core/fp-sub-isa.md).

> **NOTE — the refine provides ONE polynomial step; the iteration count is firmware.** The TIE op supplies a
> single QLI refine step plus the coefficient tables. Any further Newton/QLI iteration on top of these seeds
> is a firmware-kernel decision, out of the ISA carve. `[HIGH structure / iteration-count CARRIED firmware]`

### 4.5 Timing and worked pattern `[HIGH/OBSERVED]`

This is the **deepest vector latency in the batch**: the `IVP_RECIPQLIN_2XF32_0` scoreboard runs
`…_inst_stage0 … _stage15` — **16 stages**. USE `vr@10`, the 5 enables + 5 flags `@14`, `CPENABLE@3`; DEF
recip/rsqrt prefanout `@9`, `flop2@11`, `flop3@12`, `vt@13` (≈3-cycle result latency), FSR flag writeback
`@14`, and `VectorPipeImpreciseErr@15` (the pipe-level imprecise-error output). The fp transcendental pipe
is thus ~2 cycles deeper than the integer ALU and ~1 deeper than the fp-FMA.

```
recipqli.s             v4, v6        →  32505818e206452f   { nop; nop; nop; recipqli.s  v4, v6 }
ivp_recipqlin_2xf32_0  v4, v6        →  32505718e206452f   ([7:4]/byte 0x58→0x57 = .S/seg-0 discriminator)
ivp_recipqlin_2xf32t_0 v4, v6, vb1   →  325016184206452f   (T form: selector narrows, vbr=vb1, bit[0]=1)
```

Worked value: `vr` lane = `2.0` (`0x40000000`) → seed `≈ 0.498` (the §4.1 `recip0` output) → QLI refine to
`≈ 0.5` (`0x3F000000`); the seg-0 `(A, gx)` pair bracketing the mantissa drives the polynomial; RNE round; no
exception. `[HIGH/OBSERVED — round-trip oracle; value path via the live seed leaf]`

---

## 5. Groups 4 & 5 — `ivp_sem_rur_fcr_fsr` / `ivp_sem_wur_fcr_fsr` (fp control state) `[HIGH/OBSERVED]`

### 5.1 Role and the FCR/FSR field layout

Move the floating-point **control** register (FCR = dynamic round mode + exception enables) and **status**
register (FSR = sticky exception flags) between an AR and the fp-control state. These are the vector-pipe
URs, not the base `THREADPTR` UR. `RUR.*` read into an AR; `WUR.*` write from an AR; the FSR flags are sticky
(`shared_or`), set on raise and cleared only by `WUR.FSR`.

The intra-FCR bit packing — which DX-side analysis previously had to mark `MED-INFER` — is now read **exactly**
from the `libfiss-base.so` field-pack/unpack leaves this pass. `opcode__wur_fcr__stage_5` unpacks the AR value
`art` into the model state words, and `opcode__rur_fcr__stage_5` packs them back, an exact round-trip:

| FCR bit | field | WUR extract | RUR re-pack |
|---|---|---|---|
| `[1:0]` | **RoundMode** | `art & 0x3` | `\| RoundMode` |
| `[2]` | enable E2 | `(art>>2) & 1` | `<< 2` |
| `[3]` | enable E3 | `(art>>3) & 1` | `<< 3` |
| `[4]` | enable E4 | `(art>>4) & 1` | `<< 4` |
| `[5]` | enable E5 | `(art>>5) & 1` | `<< 5` |
| `[6]` | enable E6 | `(art>>6) & 1` | `<< 6` |

So the FCR is a **7-bit** register: `RoundMode[1:0] ++ {5 exception enables}` at bits `[6:2]`. The FSR is the
5-bit sticky-flag companion (`Invalid/DivZero/Overflow/Underflow/Inexact`). RoundMode encoding (shared with
the convert/round cores): `000 RNE`, `001 RTZ`, `010 RPI` (+inf), `011 RMI` (−inf), `100 RNA` (round-half-away).
The FCR reset default is **RNE**. `[HIGH/OBSERVED — the WUR/RUR field shift lattices read & matched this pass]`

```c
// ivp_sem_wur_fcr_fsr :: WUR.FCR — art → FCR (the exact bit lattice, observed)
void wur_fcr(uint32_t art) {
    fcr.round_mode      =  art        & 0x3;   // [1:0]
    fcr.enable[E2]      = (art >> 2)  & 0x1;
    fcr.enable[E3]      = (art >> 3)  & 0x1;
    fcr.enable[E4]      = (art >> 4)  & 0x1;
    fcr.enable[E5]      = (art >> 5)  & 0x1;
    fcr.enable[E6]      = (art >> 6)  & 0x1;
}
// ivp_sem_rur_fcr_fsr :: RUR.FCR — FCR → arr (the inverse)
uint32_t rur_fcr(void) {
    return fcr.round_mode
         | (fcr.enable[E2] << 2) | (fcr.enable[E3] << 3) | (fcr.enable[E4] << 4)
         | (fcr.enable[E5] << 5) | (fcr.enable[E6] << 6);
}
```

> **QUIRK — `WUR.FCR`/`WUR.FSR` are BARE 3-byte narrow ops, not FLIX-bundled (PLACEMENTS = 1).** Every other
> Batch-24 op is a FLIX slot placement; the two `WUR` ops are the standard Xtensa narrow `WUR` opcode form
> (the UR number lives in the instruction word), so each has a single `Opcode_wur_fcr_Slot_inst_encode` thunk
> rather than the per-format ladder. The `RUR` reads, by contrast, *are* FLIX vector-pipe ops (6 placements
> each) — the read and write paths use different instruction encodings. `[HIGH/OBSERVED — slot tables + oracle]`

### 5.2 Encoding `[HIGH/OBSERVED]`

```
Opcode_rur_fcr_Slot_f0_s3_alu_encode :  movl $0x6498de02,(%rdi)   ; [34:10]=0x192637, [3:0]=0x2
Opcode_rur_fsr_Slot_f0_s3_alu_encode :  movl $0x6498de03,(%rdi)   ; [3:0] 0x2→0x3 = FCR→FSR
Opcode_wur_fcr_Slot_inst_encode      :  movl $0x00f3e800,(%rdi)   ; bare narrow, UR-number byte 0xe8
Opcode_wur_fsr_Slot_inst_encode      :  movl $0x00f3e900,(%rdi)   ; UR-number byte 0xe8→0xe9 = FCR→FSR
```

The FCR vs FSR discriminator is the `[3:0]` field `0x2↔0x3` for `RUR`, and the UR-number byte `0xe8↔0xe9`
for the narrow `WUR`.

### 5.3 Timing and worked pattern `[HIGH/OBSERVED]`

`RUR.FCR` runs `…RUR_FCR_inst_stage0 … _stage12` (read latency `arr@12`, ≈2 cycles); the `WUR` writes commit
`@12`. USE all-fields `@10` (`RUR`) / `art@3` (`WUR`); `CPENABLE@3`.

```
rur.fcr a2  →  32514c005043452f   { nop; nop; nop; rur.fcr  a2 }
rur.fsr a2  →  32514c085043452f   (byte …4c00→…4c08 = [3:0] 0x2→0x3, FCR/FSR discriminator)
wur.fcr a7  →  f3e870             wur.fcr  a7   (BARE 3-byte narrow — no bundle)
wur.fsr a7  →  f3e970             wur.fsr  a7   (UR-number byte e8→e9)
```

Worked value: `WUR.FCR a7` with `a7 = …01` sets RoundMode = RTZ (`001`), so subsequent `RECIPQLI` rounds
toward zero; `WUR.FSR a7` with `a7 = 0` clears all sticky flags; `RUR.FSR a2` then reads back the accumulated
`Inexact/Invalid/…` flags. The full FCR/FSR semantics are documented at
[the fp sub-ISA page](../core/fp-sub-isa.md). `[HIGH/OBSERVED — round-trip oracle]`

---

## 6. Group 6 — `ivp_sem_addmod` (IVP_ADDMOD16U) `[HIGH/OBSERVED]`

### 6.1 Role — a scalar circular-pointer increment

`IVP_ADDMOD16U` is a scalar AR modular-add: `arr = (ars + art) mod 2^16`. It advances a circular-buffer
pointer/index modulo `2^16` beside a streaming load — which is precisely why it lives in the **S1 Ld/LdStALU
slot**, not the S3 vector ALU. As a base-pipe op it carries **no `CPENABLE`** and **no
`Coprocessor1Exception`**.

`OUT[arr:AR]  IN[ars:AR, art:AR]`; no STATE_IN/OUT, no EXC. Assembler operand order: **`arr, ars, art`**.

### 6.2 Semantics — live-driven `[HIGH/OBSERVED]`

The wrap mask `0xFFFF` — previously the one `INFERRED` item in the DX-side model — is now **executed** this
pass. The value leaf `module__xdref_add_16_16_16` at file offset `0x858480` is the three-instruction
`add %esi,%edx ; and $0xffff,%edx ; mov %edx,(%rcx)`; driving it via `ctypes` gives:

```c
uint16_t ivp_addmod16u(uint16_t ars, uint16_t art) {
    return (uint16_t)((ars + art) & 0xFFFF);   // 16-bit modular (wrap) add; no saturation, no flags
}
```

```
addmod16u(0xFFFE, 0x0003) = 0x0001   (wrap-around)
addmod16u(0x1000, 0x0010) = 0x1010
addmod16u(0xFFFF, 0xFFFF) = 0xFFFE   (wrap-around)
```

> **CORRECTION — the `0xFFFF` wrap mask is OBSERVED, not inferred.** The earlier model marked the upper-bit
> behaviour above bit 15 as an inferred `& 0xFFFF`; executing `module__xdref_add_16_16_16` live this pass
> confirms the mask exactly (the `and $0xffff` is a literal in the leaf), upgrading the claim to
> `[HIGH/OBSERVED]`. `[HIGH/OBSERVED — leaf executed in-process]`

### 6.3 Encoding, timing, worked pattern `[HIGH/OBSERVED]`

```
Opcode_ivp_addmod16u_Slot_f0_s1_ld_encode :  movl $0x005ad000,(%rdi)   ; [25:12]=0x5ad (F0/F1)
```

All 7 placements (`F0/F1/F2/F3/F4/F6/F7`) are in the `s1_ld` slot — confirmed by listing the thunk symbols,
each ending `_Slot_f<N>_s1_ld_encode`. Timing: the `IVP_ADDMOD16U` scoreboard runs only `…_inst_stage0 …
_stage1` — a **single-cycle, rstage-resident base-pipe op** (USE `ars@1`, `art@1`; DEF `arr@1`; no
`CPENABLE`).

```
ivp_addmod16u a3, a4, a5  →  0007173208c2ab01346823a18554452f
   decode: { nop; ivp_addmod16u  a3, a4, a5; nop; nop }   — note it lands in the SECOND slot (S1)
```

Worked value: `a4 = 0xFFFE`, `a5 = 0x0003` → `a3 = 0x0001` (wrap); `a4 = 0x1000`, `a5 = 0x0010` → `a3 =
0x1010`. `[HIGH/OBSERVED — round-trip oracle + live leaf]`

---

## 7. The B01–B24 vector tally and residual `[HIGH/OBSERVED]`

Batch 24 is the **last** IVP-vector batch, so its close must reconcile the running sum of the B01–B24
`ivp_`-prefixed members against the nm-authoritative roster:

```
nm libisa-core.so | rg -o 'Opcode_(.*)_Slot_[^ ]*_encode' -r '$1' | sort -u | rg -c '^ivp_'   →  1065
```

Batch 24 contributes **19** of those 1065 `ivp_` mnemonics (8 histogram + 6 sqz + 4 vector
`ivp_recipqlin_2xf32{,t}_{0,1}` + 1 `ivp_addmod16u`); its other 5 mnemonics (`recipqli_s`, `rur_fcr`,
`rur_fsr`, `wur_fcr`, `wur_fsr`) belong to the **469 scalar** roster and do **not** count toward the vector
subtotal.

Since every `ivp_`-prefixed mnemonic falls into exactly one of the 30 batches and B24 is the partition's final
vector batch, the B01–B24 vector running total **closes at 1065 with zero residual** — the union of the
batch-by-batch `ivp_` member sets equals the global 1065-op `ivp_` roster, no mnemonic double-counted, none
orphaned. `[HIGH/OBSERVED — the global 1065 figure and the B24 19-of-1065 contribution are both
nm-grounded this pass]`

> **CORRECTION — the planning-target "1174-op IVP subtotal" is a count of a different population.** Some
> upstream planning notes carry an IVP-vector subtotal of **1174** and a Batch-24 partition figure of "~21".
> Re-grounding against the encode-thunk table this pass, the authoritative `ivp_`-prefixed-mnemonic roster is
> **1065** (not 1174) and the Batch-24 membership is **24** opcodes (`8+6+5+2+2+1`), of which **19** are
> `ivp_`-prefixed. The 1174 figure counts a wider population (e.g. including the non-`ivp_` vector-pipe
> mnemonics such as `recipqli_s`/`rur*`, or placement-level rather than mnemonic-level entries); the
> mnemonic-level vector roster that the per-batch pages reconcile against is the **1065**. This page pins the
> tally to that nm-verified 1065. `[HIGH/OBSERVED — divergence resolved against the binary]`

---

## 8. Coverage / verification ledger

| claim | grounding | verdict |
|---|---|---|
| 1534 mnemonics = 469 scalar + 1065 vector; 12569 placements | `nm` strip of encode thunks | **PASS** `[HIGH/OBSERVED]` |
| Batch 24 = 24 mnemonics, `8+6+5+2+2+1`, 19 `ivp_` + 5 scalar | `nm` membership sweep | **PASS** `[HIGH/OBSERVED]` |
| COUNTEQZ = COUNTEQ + bit[9] (`0x200`); LE = selector LSB `0x866→0x867` | encode-thunk byte delta | **PASS** `[HIGH/OBSERVED]` |
| `IVP_ADDMOD16U` = `(a+b)&0xFFFF` | `module__xdref_add_16_16_16` executed live | **PASS** `[HIGH/OBSERVED]` |
| addmod in S1 Ld-slot (7×); `WUR.*` bare-narrow (1×, `Slot_inst`) | slot-thunk listing | **PASS** `[HIGH/OBSERVED]` |
| FCR layout = RoundMode[1:0] ++ 5 enables[6:2] | `opcode__wur_fcr`/`rur_fcr` field lattice | **PASS** `[HIGH/OBSERVED]` |
| all 24 mnemonics + operand order | `xtensa-elf-as`/`-objdump` round-trip | **PASS** `[HIGH/OBSERVED]` |

**Residuals carried** (named, not gaps): (a) the SQZ innermost prefix-sum adder-tree lane-pairing is one
function deeper (value-determined; the two-stage structure + popcount + direction are observed) `[MED/INFERRED]`;
(b) the exact recip-vs-rsqrt-vs-segment role of the recip `[7:4]` sub-selector — the `op_recip`/`op_rsqrt`/
`evn`/`odd` flops are all present; the precise mux-bit assignment is `[MED/INFERRED]`; (c) the QLI/Newton
iteration count on top of the one TIE refine step is firmware-kernel territory `[CARRIED]`.

This page closes the **B01–B24 vector subtotal at 1065 `ivp_`-prefixed mnemonics, zero residual**. The six
Batch-24 semantic groups are disjoint from each other and from the preceding boundary batch
[B21 (select / shuffle / compress)](b21-select-shuffle.md). Cross-references: the round-mode / sticky-flag
machinery at [the fp sub-ISA page](../core/fp-sub-isa.md); the cross-lane reduce kernel the histogram count
reuses at [the cas arithmetic semantics page](../../iss/cas-arith-sem.md); the predicate-gate model the `M`
and `T` variants share at [the predicate / classify page](../../validation/predicate-classify.md); and the
30-batch partition at [the template & partition page](template-and-partition.md).
