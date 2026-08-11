# Byte-Decode of the MX Device Bodies

This page closes the **INFERRED residual** the [compiler-side MX path](mx-path.md)
left open: it **byte-decodes the actual device firmware bodies** of the OCP
Microscaling (MX) path on the GPSIMD engines and pins the **scale-application
point** to the instruction byte. Where [the MX path page](mx-path.md) is the
*lowering map* (host op → device opcode, value model driven from the shipped nki
simulator), this page is the *body decode*: the FORWARD `0xE3 QUANTIZE_MX` DVE FLIX
kernel disassembled native, the v4 PE `0x09 LDWEIGHTS_MX` / `0x0A MATMUL_MX` pair and
the v5 `MXTENSOR_V2` `0x01`/`0x02` fold struct-pinned, and the E8M0 scale multiply
cross-validated **live** against the ISS value oracle (`xdref_addexp` /
`xdref_addexpm`, driven via `ctypes`, not merely cited).

It carries one headline result and one **CORRECTION** to the gen-floor framing of the
sibling firmware pages: the [MX path page §6.3](mx-path.md), the
[MX dequant page §6.2](../firmware/kernels/mx-dequant.md) and the
[datatype model §2.4](../firmware/kernels/dtype-model.md) flagged the `0xE3` gen floor
as *contested* — ledger/`.pyi` say v4+, while the `s3dmx1_quant_valid_nc` predicate
reads `nc == V5`. §6.3 here **resolves it from the device body's own gen-gate**: it is
**not** a contradiction — each per-arch ISA header gates `0xE3` on **its own** core
version, and the true floor is **MARIANA (v4)**.

> **Headline.** The forward `0xE3 QUANTIZE_MX` DVE body is now **byte-decoded, not
> inferred.** Both compute legs are clean FLIX bundles, byte-identical across the
> MARIANA DVE PERF and TEST images (a real shared kernel, not a desync artifact). The
> E8M0 extract is `srln_2x32 (>>23)` + `bmaxn_2x32 (max-over-block)`; the ÷scale +
> narrow + ldexp leg is `mulus4tn16xr16 (×packed scale)` + `trunc16nxf16` +
> `baddnorm/bsubnormnx16`; the clamp + ×4-pack is `cvtg48n_2x32h` + `ueqn_2xf32t` +
> `sel2nx8i_s4`. The scale rides a **packed scale register** (`xb_int64pr`) into the
> MAC on *both* engines. `[HIGH/OBSERVED — every cited bundle disassembled native,
> SHA-pinned carve.]`

Confidence per [the Confidence & Walls model](../reference/confidence-model.md):
`[HIGH/MED/LOW]` × `OBSERVED` (read from byte / disassembled / compile-verified /
live-driven) / `INFERRED` (reasoned over OBSERVED) / `CARRIED` (re-used from
a cited sibling page at its confidence). The `extracted/` and `ida/` trees are
gitignored — reach them with `fd --no-ignore` or an absolute path.

> **NOTE — engine class.** The NCFW management core is scalar Xtensa-LX, but the MX
> bodies decoded here are **FLIX/VLIW** DVE (Vector) and PE (Tensor) kernels: they are
> decoded with the shipped Cadence `xtensa-elf-objdump --xtensa-core=ncore2gp`, which
> emits the 32-byte FLIX bundles as `{ slot ; slot ; … }`. Do **not** decode these
> bodies as scalar-LX — the op-0 `e`/`f` length bytes are FLIX bundle headers here, not
> 3-byte LX scalar ops.

---

## 0. TL;DR — the residual is closed

1. **The forward `0xE3 QUANTIZE_MX` body is FLIX, byte-decoded.** Two compute legs,
   both byte-identical PERF≡TEST. `[HIGH/OBSERVED]`
2. **The E8M0 extract is `>>23` then max-over-block.** `ivp_srln_2x32 v14,v25,v5`
   (logical-shift-right by the literal **23** broadcast in `v5`) + `ivp_bmaxn_2x32`
   (the 32-bit max-reduce over the 8×4 block = the shared biased exponent). `[HIGH/OBSERVED]`
3. **The ÷-by-scale + fp8-narrow + ldexp leg is a packed-register MAC.**
   `ivp_mulus4tn16xr16 wv0,v31,v7,pr8` (the runtime scale MAC, scale in `pr8`) +
   `ivp_trunc16nxf16 v0,v15,7` (fp narrow) + `ivp_baddnormnx16` / `ivp_bsubnormnx16`
   (block exponent add/sub = `ldexp`). `[HIGH/OBSERVED]`
4. **The clamp + ×4-pack leg is `cvtg48n_2x32h` + `ueqn_2xf32t` + `sel2nx8i_s4`** (fp8
   narrow-extract, the ±`max_val` range test, the 8-bit lane-compaction packing four
   fp8 bytes into one `uint32` word). `[HIGH/OBSERVED]`
5. **The forward op has its own 64-byte ISA struct, compile-verified.**
   `S3DMX1_QUANT` (`sizeof==64`): `src_mem_pattern@16` (MEM_PATTERN3D),
   `num_active_channels@33`, `in_dtype@34`, `out_dtype@35`, `dst_mem_pattern@48`
   (MXMEM_PATTERN1D). Its dst is **the same descriptor the PE matmul reads** — producer
   format == consumer format. `[HIGH/OBSERVED — `gcc -I…/mariana/tpb`]`
6. **The v4 PE pair is byte-pinned on both sides.** Firmware dispatch (`movi.n a3,9 ;
   bne a2,a3 ; j 0x2baf` → op `0x09` LdweightsMX; `bnei a2,10 ; j 0x2bb7` → op `0x0A`
   MatmulMX) + the `SMX1_LW`/`SMX1D3_MM` structs + the out-of-band E8M0 scale at
   `MXTENSOR1D.scale_addr@4`. `[HIGH/OBSERVED]`
7. **The v5 `MXTENSOR_V2` fold is a pure ADDR-marker branch.** `0x01 LDWEIGHTS` /
   `0x02 MATMUL` enter MX mode by `start_addr.marker_a4 == ADDR4MARKER_MXTENSOR_V2 =
   0x01` (LSB set) — no separate opcode. v5 interiors are **header-OBSERVED only**;
   every v5-interior claim is flagged INFERRED. `[HIGH/OBSERVED struct + marker; v5
   firmware INFERRED]`
8. **The scale-application point is the packed-register MAC on both engines, and the
   E8M0 multiply is bit-exact against the ISS oracle driven LIVE.**
   `xdref_addexp(a, b) == a × 2^(exp(b)−127)`, verified across seven value pairs through
   `ctypes` — the firmware `baddnorm/bsubnormnx16` IS this `addexp/addexpm` leaf in
   vector form. `[HIGH/OBSERVED — live drive]`

---

## 1. Locator + toolchain — the carve

Every fact below is read from the carried-device firmware image
`libnrtucode_internal.so` (`sha256 b7c67e898a116454…`) inside
the GPSIMD custom-op library, disassembled **native** with the shipped Cadence Xtensa
toolchain registered for the Vision-Q7 `ncore2gp` config.

```sh
GP=…/neuronx-gpsimd
NEST=$GP/extracted/nested/gpsimd_tools_tgz/tools
export XTENSA_SYSTEM=$NEST/XtensaTools/config XTENSA_CORE=ncore2gp
OD=$NEST/XtensaTools/bin/xtensa-elf-objdump          # GNU binutils 2.34 / Xtensa Tools 14.09
F=$GP/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/…/c10/lib/libnrtucode_internal.so
ISA=$GP/extracted/…/c10/include/neuron_{mariana,maverick}_arch_isa/tpb
LF=$NEST/ncore2gp/config/libfiss-base.so             # the ISS value oracle (x86-64, not stripped)
```

The four MARIANA images carved identity-mapped (`.text`/`.rodata` VMA == file offset;
IRAM offset == device IRAM VA, reset-vector byte 0):

| image | file off / size | sha256[:8] | role |
|---|---|---|---|
| MARIANA DVE PERF IRAM  | `0x31f5c0` / `0x13540` | `dd14c1e3` ✔ | forward `0xE3` body |
| MARIANA DVE TEST IRAM  | `0x38f080` / `0x13560` | `78922240` ✔ | forward body (must match PERF) |
| MARIANA PE  PERF IRAM  | `0x335ca0` / `0x12ce0` | `a077f110` ✔ | PE scale-MAC tap |
| MARIANA PE  DEBUG IRAM | `0x42c520` / `0x18c20` | `6600e24a` ✔ | PE opcode dispatch |

> **GOTCHA — the DVE descriptor→body numeric edge is data-driven, not statically
> resolvable.** The `0xE3` dispatch chain (`0xE3 → DRAM jump-table idx → trampoline
> 0x317a { call8 0x2298 ; j 0x31ee }`; the `QuantizeMx` L2 stub `@0x2298` sets a
> `kernel_info` field then `call8 0x9920`, whose tail is `l32i a2,[a2,0] ; callx8 a2`)
> **terminates at an indirect call** — the per-kernel math body is reached by its
> `kernel_info` data pointer, not a resolvable descriptor edge. The forward body is
> therefore located by its **IVP fingerprint** (§2), not a static pointer. The
> fingerprint→`QuantizeMx` attribution is HIGH (the `srln`/`bmaxn`/`mulus4t`/`cvtg48`/
> `sel` cluster is the unique MX-quantize op-set — no other DVE op needs the
> `>>23`-exponent-extract + fp8-pack chain), but the *byte-exact body-start VA* is
> INFERRED. `[HIGH fingerprint attribution; INFERRED exact start VA.]`

---

## 2. The forward `0xE3 QUANTIZE_MX` DVE body — byte-decoded

The op is the **forward pack**: extract the per-(8×4)-block shared exponent (E8M0),
divide the data by `2^(scale−127)`, clamp to `±max_val`, cast to fp8, ×4-pack to a
32-bit word, scatter the scale ×32 into the SBUF quadrants. Each leg is now a decoded
FLIX bundle. The bundle disassemblies below are reproduced **verbatim** from
`xtensa-elf-objdump --xtensa-core=ncore2gp`; the leading slot is the branch/load slot
of the FLIX bundle and is *not* part of the MX datapath.

### 2.1 The IVP-op fingerprint (PERF IRAM inventory)

The MARIANA DVE PERF IRAM carries, as a coherent set, exactly the ops the §4 value
model needs — the unique signature of the MX-quantize body: `[HIGH/OBSERVED]`

| role | IVP op(s) |
|---|---|
| exponent-extract shift | `ivp_srln_2x32` (32-bit logical >>; the `>>23`) |
| E8M0 max-reduce | `ivp_bmaxn_2x32`, `ivp_bmaxun_2x32` |
| runtime scale-MAC | `ivp_mulus4tn16xr16`, `ivp_mulus4tan16xr16`, `ivp_muluspa2n8xr16`, `ivp_dmulqa2n8xr8` (all carry an `xb_int64pr` packed scale operand) |
| E8M0 → scale register | `ivp_dextrprn_2x32` (→ `xb_int64pr`), `ivp_extrpr2nx8` |
| ldexp normalize | `ivp_baddnormnx16` (exp +), `ivp_bsubnormnx16` (exp −) |
| fp8 narrow / convert | `ivp_trunc16nxf16`, `ivp_cvtg48n_2x32h` |
| clamp / pack | `ivp_ueqn_2xf32t` (range test), `ivp_sel2nx8i_s4` (×4 lane-pack) |

### 2.2 LEG B — the E8M0 shared-exponent extract

`@PERF 0x79ec`, bytes `3e0402a29de1115a00fe080002606048` (`[HIGH/OBSERVED]`):

```
79ec: 3e0402a29de1115a00fe080002606048
   { bbci.w15 a4,3,0x7a0f ; ivp_lsn_4x64_i v10,a2,0x180 ; addi.a a1,a0,0
   ; ivp_srln_2x32 v14,v25,v5 ; ivp_bmaxn_2x32 vb0,v12,v5,v24 }
```

- `ivp_srln_2x32 v14,v25,v5` — 32-bit-lane **logical** shift-right by a *vector* amount;
  `v5` broadcasts the literal **23** (`movi.n a0,23` present `@PERF 0xc795`). This is the
  `(bits >> 23)` of the biased fp32 exponent.
- `ivp_bmaxn_2x32 vb0,v12,v5,v24` — the 32-bit **max-reduce** producing the running max
  over the [8,4] block = the per-block shared exponent (E8M0). The `& 0xFF` falls out of
  the downstream byte-lane narrowing.

The E8M0 byte is captured into a packed scale **register** fused with the max-reduce.
`@PERF 0x22b5` (`[HIGH/OBSERVED]`):

```
22b5: 5eb35b19261a06f7b67001220d041612
   { bltu.w15 a3,a5,0x587a ; addi.a a15,a11,5 ; srai a12,a13,27
   ; ivp_dextrprn_2x32 pr8,v14,v16,5,10 ; ivp_bmaxn_2x32 vb1,v16,v0,v9 }
```

`ivp_dextrprn_2x32 pr8,v14,v16,5,10` (proto → `xb_int64pr`) extracts `pr8` from the
exponent vector — the **exact register consumed by the LEG-A scale-MAC**.

### 2.3 LEG A — the ÷scale + fp8-narrow + ldexp

`@PERF 0x66a6`, bytes `deb53762f33e38100e80a01050003e30`. The same 16 bytes appear at
`TEST 0x64c7` — a **real shared kernel**, not a FLIX-desync ghost (`[HIGH/OBSERVED]`):

```
66a6: deb53762f33e38100e80a01050003e30        (≡ TEST 0x64c7, byte-identical)
   { bnall.w15 a5,a13,0x7bac ; ivp_lanx8s_xp v6,u1,a7,a3
   ; ivp_mulus4tn16xr16 wv0,v31,v7,pr8 ; ivp_trunc16nxf16 v0,v15,7
   ; ivp_baddnormnx16 vb4,v0,v1,v0 }
```

- `ivp_mulus4tn16xr16 wv0,v31,v7,pr8` — the unsigned×signed 4-term widening **MAC**; the
  4th operand `pr8` is the **packed scale register** (from §2.2) carrying the
  E8M0-derived multiplier. This is the `2^(scale−127)` multiply with a **runtime** scale.
- `ivp_trunc16nxf16 v0,v15,7` — fp16-narrow truncate (round/scale immediate 7), the
  dtype narrowing toward fp8.
- `ivp_baddnormnx16 vb4,v0,v1,v0` — block exponent-**ADD** normalize (`ldexp +`).

The **divide-direction** normalize (the forward quantize's `÷2^(scale−127)`),
`@PERF 0x6c4b`, bytes `2ea444b463196e5ab5404980afb84498` (`[HIGH/OBSERVED]`):

```
6c4b: 2ea444b463196e5ab5404980afb84498
   { bany.w15 a4,a2,0x9450 ; ivp_lv2nx8_i v27,a4,0x3500
   ; ivp_muluspa2n8xr16 wv1,v12,v26,pr6 ; ivp_dextrprn_2x32 pr8,v26,v2,12,13
   ; ivp_bsubnormnx16 vb1,v28,v5,v26 }
```

`ivp_bsubnormnx16 vb1,v28,v5,v26` = block exponent-**SUBTRACT** normalize = the
`÷2^(scale−127)` (subtract the shared exponent from each element's exponent), with
`ivp_dextrprn_2x32 pr8,…` re-deriving the scale register.

The **fp8 PACK** (clamp + ×4 lane-compaction), `@PERF 0x216d`, bytes
`2e430c0017e1c11c082f841b0c60c105` (`[HIGH/OBSERVED]`):

```
216d: 2e430c0017e1c11c082f841b0c60c105
   { bbsi.w15 a3,2,0x232d ; addi a0,a12,32 ; ivp_cvtg48n_2x32h wv2,v16,v4
   ; ivp_ueqn_2xf32t vb8,v2,v17,vb5 ; ivp_sel2nx8i_s4 v18,v17,v22,16 }
```

- `ivp_cvtg48n_2x32h wv2,v16,v4` — 48-bit accumulator → narrow extract (the fp8 narrowing).
- `ivp_ueqn_2xf32t vb8,v2,v17,vb5` — the fp32 range/clamp test (the `±max_val` saturating
  compare).
- `ivp_sel2nx8i_s4 v18,v17,v22,16` — the 8-bit lane-compaction that packs four fp8 into
  the 32-bit ×4 word.

### 2.4 The annotated forward body

```c
/*
 * quantize_mx  —  device body of 0xE3 QUANTIZE_MX (DVE / Vector).
 * Decoded native (ncore2gp) from MARIANA DVE PERF IRAM (sha dd14c1e3); the two compute
 * legs are byte-identical in the TEST image (sha 78922240) -> a real shared kernel.
 * VMAs are PERF-image relative. Per-(8 partition x 4 element) block; num_active lanes run
 * in parallel on the DVE.  Value model: mx-path.md §4 (the shipped nki sim).
 */
void quantize_mx_body(const f16 *src, u32 *dst, u8 *dst_scale, int P, int F, mx_fmt_t fmt)
{
    /* ---- LEG B: E8M0 shared-exponent extract  (@0x79ec, @0x22b5) ---- */
    v32 ebits  = ivp_srln_2x32(src_as_u32, v_const(23));    /* (bits >> 23) -> biased exp  */
    v32 emax   = ivp_bmaxn_2x32(ebits /* over the 8x4 block */);  /* max-reduce = E8M0      */
    xb_int64pr pr8 = ivp_dextrprn_2x32(emax);              /* E8M0 byte -> packed scale reg */
    /* the host-side  (uint8)max - max_exp  lands in dst_scale via the §3 scatter         */

    /* ---- LEG A: divide by scale, narrow to fp8, ldexp  (@0x66a6 / @0x6c4b) ---- */
    wv  acc = ivp_mulus4tn16xr16(src_f16, /*coeff*/ pr8);   /* widening MAC, scale in pr8   */
    v16 nar = ivp_trunc16nxf16(acc, /*rmode*/ 7);           /* fp16-narrow truncate         */
    v16 q   = ivp_bsubnormnx16(nar, emax);                  /* exp- = / 2^(scale-127)       */
    /*        (the inverse direction would use ivp_baddnormnx16 = exp+ = ldexp +)          */

    /* ---- PACK: clamp +-max_val, fp8-cast, x4-compact  (@0x216d) ---- */
    wv  g   = ivp_cvtg48n_2x32h(q);                         /* 48-bit accum -> fp8 narrow   */
    vbool clamp = ivp_ueqn_2xf32t(g, v_max_val(fmt));       /* +-max_val saturating compare */
    *dst    = ivp_sel2nx8i_s4(g_clamped);                   /* 4 fp8 bytes -> one u32 word  */

    /* ---- SCALE SCATTER: x32 quadrant placement (§3 / §5 of mx-path) ----            */
    /*   dst_scale lands at SBUF partitions {0-3, 32-35, 64-67, 96-99} (the x32 stride). */
}
```

### 2.5 The compile-verified operand struct

`NEURON_ISA_TPB_S3DMX1_QUANT_STRUCT` (`aws_neuron_isa_tpb_s3dmx1_quant.h`, opcode
`QuantizeMX = 0xe3`), **compile-verified `sizeof==64` and offsets**
(`gcc -I…/mariana/tpb`, `__builtin_offsetof`): `[HIGH/OBSERVED]`

| off | field | type | note |
|---|---|---|---|
| 0  | `header`              | Header(4)         | opcode `QuantizeMX = 0xe3` |
| 4  | `events`             | Events(8)         | |
| 12 | `reserved0[4]`        | (0)               | `s3dmx1_quant_reserved_zero` asserts all-zero |
| 16 | `src_mem_pattern`     | MemPattern3d(16)  | `bf16`/`fp16` source; **SBUF-only**, UINT32-view 3-D tensor |
| 32 | `reserved1[1]`        | (0)               | |
| 33 | `num_active_channels` | u8                | ∈ {32, 64, 96, 128} (the quadrant rule) |
| 34 | `in_dtype`            | Dtype(1)          | `s3dmx1_quant_valid_in_dtype` ⇒ {FP16, BFLOAT16} |
| 35 | `out_dtype`           | Dtype(1)          | `s3dmx1_quant_valid_out_dtype` ⇒ {FP8\_EXP5, FP8\_EXP4} — **fp8 only** |
| 36 | `reserved2[12]`       | (0)               | |
| 48 | `dst_mem_pattern`     | MxMemPattern1d(16)| MX out: `start_addr` (packed fp8) + `scale_addr` (E8M0) |

The header's own behaviour-spec comments (read verbatim) pin the validity predicate
`is_valid_s3dmx1_quant`: `has_quantize_mx_opcode` (`header.opcode == Opcode::QuantizeMX`)
∧ `mem3d_valid` src (`UINT32`-view, **AllowedInPSUM::False, AllowedInSBUF::True**) ∧
`mxmem1d_valid` dst ∧ `is_valid_dtype(in, AllowFP32R::False)` ∧
`is_valid_dtype_inc_fp4(out, AllowFP4::False)` ∧ the SBUF-quadrant gate (§5).

> **NOTE — fp8-only output, confirmed at the validity gate.** `s3dmx1_quant_valid_out_dtype`
> ⇒ `{FP8_EXP5, FP8_EXP4}` and the `out_dtype` predicate passes `DtypeAllowFP4::False` —
> so the forward op **packs fp8 only, never MXFP4**, confirming the
> [MX path GOTCHA](mx-path.md) (`quantize_mx` cannot produce MXFP4) at the ISA-validity
> level, not just the host shape-validator. `[HIGH/OBSERVED — header predicate.]`

**Direction verdict.** `0xE3` = the **forward pack**. Its `dst_mem_pattern`
(MXMEM_PATTERN1D = data+scale) is the **identical descriptor** the v4 PE `SMX1_LW`
reads (§3): the forward op *produces* the MX tensor the matmul *consumes*, field for
field. `[HIGH/OBSERVED]`

---

## 3. The PE matmul path — v4 `0x09`/`0x0A`, v5 `MXTENSOR_V2` `0x01`/`0x02`

### 3.1 v4 (MARIANA) firmware dispatch — byte-pinned

PE_DEBUG IRAM (`sha 6600e24a`), `@0x296c` (`[HIGH/OBSERVED]`):

```
296c: 0c93     movi.n  a3, 9
296e: 379202   bne     a2, a3, 0x2974
2971: 868e00   j       0x2baf            ; op 0x09 LdweightsMX -> handler 0x2baf
2977: 669202   bnei    a2, 10, 0x297d
297a: 468e00   j       0x2bb7            ; op 0x0A MatmulMX    -> handler 0x2bb7
```

This sits in the same dispatch ladder as the non-MX ops (`bnei a2,1 → 0x2b86`
Ldweights; `bnei a2,2 → 0x2b8e` Matmul; …) — confirming `0x09`/`0x0A` are **separate,
dedicated** MX opcodes on v4, not a flag on the base matmul. Re-confirmed against the
carved image (matches [pe-matmul §2](../firmware/kernels/pe-matmul.md)). `[HIGH/OBSERVED]`

### 3.2 v4 operand structs — compile-verified `sizeof==64`

All offsets `__builtin_offsetof`-verified (`gcc -I…/mariana/tpb`): `[HIGH/OBSERVED]`

```c
NEURON_ISA_TPB_SMX1_LW_STRUCT   (op 0x09, sizeof==64):
   src_mem_pattern @16 (MXMEM_PATTERN1D) | in_dtype @32 | num_active_rows @38
 | num_active_cols @39 | row_grp @44 | col_grp @45
   /* row_grp/active_rows gate: {0xf,128} | {0x3,64} | … ; col_grp 0xf, cols<=128 */
NEURON_ISA_TPB_SMX1D3_MM_STRUCT (op 0x0A, sizeof==64):
   src_mem_pattern @16 (MXMEM_PATTERN1D) | in_dtype @32 | num_active_rows @38
 | out_dtype @40 | psum_accumulate_flags @43 | dst_mem_pattern @48 (MEM_PATTERN3D -> PSUM)
   /* in_dtype: is_valid_dtype_inc_fp4(…, AllowFP4::True) -> matmul ACCEPTS FP4_E2M1 too */
```

The **out-of-band E8M0 scale** lives in `MXTENSOR1D` (compile-verified `sizeof==16`):

```c
NEURON_ISA_TPB_MXTENSOR1D (sizeof==16):
   ADDR4  start_addr  @0   /* packed MXFP8/MXFP4 DATA pointer  */
   ADDR4  scale_addr  @4   /* the E8M0 per-block scale pointer (OUT-OF-BAND) */
   u16    num_elem[1] @8
   i8     step_elem[1]@10
   u8     scale_pidx  @11  /* scale partition index */
   u8     reserved[4] @12  /* pad to MXIndirect16B size */
NEURON_ISA_TPB_MXMEM_PATTERN1D = union { MXTENSOR1D t | MXIndirect16B i }  (sizeof==16)
```

So the v4 PE pair loads the packed weight from `start_addr` and the E8M0 scale
**separately** from `scale_addr@4` — the out-of-band scale is byte-pinned at offset 4.
`[HIGH/OBSERVED]`

> **CORRECTION — operand-name reconciliation (`MXMEM_PATTERN1D` ⊃ `MXTENSOR1D`).**
> [pe-matmul §7](../firmware/kernels/pe-matmul.md) names the v4 operand
> `MXMEM_PATTERN1D` (and does not print `MXTENSOR1D`); the
> [MX path page](mx-path.md) calls it `MXTENSOR1D`. **These are the same
> object at two levels:** the *struct field* `src_mem_pattern` has type
> `MXMEM_PATTERN1D`, which is the `union { t: MXTENSOR1D | i: MXIndirect16B }`;
> `MXTENSOR1D` is its `.t` member (`start_addr@0`/`scale_addr@4`). A reimplementer must
> emit the **union** type for the field and the **`.t` member** for the out-of-band
> data+scale case — both names are correct, at different levels. `[HIGH/OBSERVED —
> compile-verified union/member relation.]`

### 3.3 v5 (MAVERICK) — the `MXTENSOR_V2` ADDR-marker fold

`NEURON_ISA_TPB_MXTENSOR_V2` (MAVERICK `common.h`, compile-read — offsets
from the struct text): `[HIGH/OBSERVED struct; v5 firmware-interior INFERRED]`

```c
NEURON_ISA_TPB_MXTENSOR_V2:
   ADDR4  data_addr         @0
   ADDR4  scale_addr        @4    /* E8M0 scale — still out-of-band, still @4 */
   u8     num_elem[2]       @8    /* tile dims */
   i16    step_elem_data_1  @10
   i16    step_elem_scale_1 @12
   u8     p_f_dim           @14   /* packed F(upper nibble)/P(lower nibble) as 2's exponents */
   Dtype  scale_dtype       @15   /* 0 = no scales (normal) ; E8M0 for MX */
   /* MXTENSOR_V2_20B pads +4 for the MEM_PATTERN4D union; member `mx` of 3D AND 4D */
```

The v5 `0x01 LDWEIGHTS` / `0x02 MATMUL` select MX mode by the **ADDR4 marker**, not a
separate opcode. The header behaviour spec (OBSERVED):

```
s3_lw_mode_checks :   mxtensorv2_pattern(src.t.start_addr) && mx_mode
                   || not_mxtensorv2_pattern(...)         && normal_mode
mxtensorv2_pattern(addr) := addr.addr_reg_v2.marker_a4 == Addr4Marker::MXTensorV2
ADDR4MARKER_MXTENSOR_V2 = 0x01   /* 00001b, LSB set — confirmed in the ADDR4MARKER enum */
```

`MX_PERF_MODE` (4×/8× row pumping): `s3_lw.h@39` / `s3d3_mm.h@40`; `QUAD_ROW = 0x1`,
`OCT_ROW = 0x4` (+ `_INTERLEAVE`/`_TILED` variants `0x2`/`0x3`/`0x5`/`0x6`). `is_valid_mx_dtype`:
`{FP8_EXP2/3/4/5, FP4_EXP2, INT4}`.

> **GOTCHA — v5 interiors are header-OBSERVED only, never byte-decoded.** MAVERICK folds
> ACT into the DVE and ships the *same* FLIX kernel as the MARIANA v4/v5 shared body for
> the compute leg; only the **header presence** of `MXTENSOR_V2` / `ADDR4MARKER` /
> `MX_PERF_MODE` is compile-verified here. **Every v5-interior claim (the firmware
> handler that consumes the marker, the perf-mode pump micro-sequencing) is INFERRED.**
> A reimplementer targeting v5 must validate against its own MAVERICK image. `[struct
> HIGH/OBSERVED; v5 firmware interior MED/INFERRED.]`

### 3.4 The scale-application point — byte-OBSERVED on the PE

The same packed-register MAC family present in the DVE forward op is present on the PE,
with the E8M0 entering via a packed register. PE_PERF IRAM (`sha a077f110`):

```
5fa7: be4e0801014cc4e94300014ebe583921       [HIGH/OBSERVED]
   { bbsi.w15 a14,11,0x8c8b ; extui a0,a0,24,14 ; ivp_mulus4tan16xr16 wv0,v6,v1,pr1
   ; ivp_extrpr2nx8 pr1,v0,36 ; ivp_babssubu2nx8 vb1,v24,v14,v23 }

d451: 5fb15c79221a64eefe7f010c62240900       [HIGH/OBSERVED]
   { bltu.w15 a1,a5,0x9cb7 ; ivp_lavnx8s_xp v6,u0,a12,a5
   ; ivp_dmulqa2n8xr8 wv3,wv3,v31,v31,v13,v31,pr0 ; ivp_dselnx16t v8,v16,v10,v0,v28,vb4 }
```

- `@0x5fa7` — `ivp_extrpr2nx8 pr1,v0,36` (proto → `xb_int32pr`) extracts the scale
  register `pr1`, consumed by the MX dequant-MAC `ivp_mulus4tan16xr16 wv0,v6,v1,pr1`.
- `@0xd451` — `ivp_dmulqa2n8xr8 …,pr0` is the QUAD-pumped packed MAC carrying scale
  register `pr0`.

**The TAP.** The E8M0 block scale rides the MAC's `xb_int64pr`/`xb_int32pr`
packed-register operand — expanded into the FP datapath at the **multiplier input**,
applied as part of the widening MAC, **before** the FP32 PSUM accumulate.

> **NOTE — multiplier-input vs PSUM-drain micro-tap is MED.** That the per-block E8M0
> scale enters via the packed-register MAC operand is `[HIGH/OBSERVED]` (the
> `extrpr → xb_intNpr → mulus4t/dmulqa` chain is decoded). Whether it enters at the
> multiplier input vs at the PSUM drain is **not byte-separable** under the FLIX bundling
> — the PE array RTL is out of corpus
> ([pe-matmul §7](../firmware/kernels/pe-matmul.md) standing caveat). The packed-reg-MAC
> scale contract is the OBSERVED anchor; the precise micro-tap is `[MED/INFERRED]`.

---

## 4. The value model — five altitudes, the ISS oracle driven LIVE

The forward EXTRACT+÷ and the inverse ×scale are the **same** exponent-add primitive in
opposite directions. Five altitudes now agree, with the firmware leg **OBSERVED** and
the ISS leg **driven live**:

| altitude | the operation | status |
|---|---|---|
| **sim** ([mx-path §4](mx-path.md)) | `exp=(bits>>23)&0xFF` → `max(...)` → `−max_exp`; `clip(src/2^(s−127), ±max_val)`; ×4 view | OBSERVED (CARRIED) |
| **torch** (`mx_torch`) | `(exp_per_block + 127 − max_exp).to(uint8)` E8M0; `2^(s−127)` mult | OBSERVED (CARRIED) |
| **FIRMWARE** | `srln_2x32(>>23)` + `bmaxn_2x32` (extract) ; `bsubnorm/baddnormnx16` + `mulus4tn16xr16(×pr8)` (÷×ldexp) ; `cvtg48n_2x32h` + `ueqn_2xf32t` + `sel2nx8i_s4` (clamp/pack) | **OBSERVED** ✔ (§2) |
| **ISS value leaf** | `module__xdref_addexp` / `addexpm` = "fp exponent-add (ldexp)" | **OBSERVED — driven live** ✔ |

### 4.1 The live ISS drive — `xdref_addexp` IS `ldexp`

The ISS value oracle `libfiss-base.so` (x86-64, not stripped) exports
`module__xdref_addexp_32f_32f_32f @0x87a5a0` and `…_16f_16f_16f @0x5218d0` (and the
`addexpm` siblings `@0x87a5e0` / `@0x521900`). Rather than cite the symbol, the
`addexp_32f` body was **driven live via `ctypes`**:

```python
lib = ctypes.CDLL(".../ncore2gp/config/libfiss-base.so")
addexp = lib.module__xdref_addexp_32f_32f_32f   # void(u32 _, u32 a, u32 b, u32 *out)
# a carries the value; b carries the scale (its biased exponent = the E8M0 code)
```

Result (`a × 2^(exp(b)−127)`, bit-exact across all probes):

| `a` | `b` | `exp(b)` | `addexp(a,b)` | `a·2^(exp(b)−127)` |
|---|---|---|---|---|
| 1.5 | 1.0 | 127 | 1.5  | 1.5 ✔ |
| 1.5 | 2.0 | 128 | 3.0  | 3.0 ✔ |
| 1.5 | 4.0 | 129 | 6.0  | 6.0 ✔ |
| 1.5 | 0.5 | 126 | 0.75 | 0.75 ✔ |
| 3.0 | 8.0 | 130 | 24.0 | 24.0 ✔ |
| −2.5| 4.0 | 129 | −10.0| −10.0 ✔ (sign from `a`) |
| 5.0 | E8M0=134 (2⁷=128) | 134 | 640.0 | 640.0 ✔ |

The disassembled body confirms the mechanism: it keeps the **sign+mantissa of `a`** and
sets the result exponent to `exp(a) + exp(b) − 127` (`lea 0x81(%rdx,%rsi)`, where
`0x81 = 129 ≡ −127 (mod 256)` after the two `>>23` exponent extracts) — i.e. exactly
`ldexp(a, exp(b)−127)`. **`xdref_addexp` is the E8M0 scale multiply.** `[HIGH/OBSERVED —
live drive + disasm]`

> **ADVERSARIAL SELF-VERIFY (live drive).** Driving the firmware-shipped `libfiss-base`
> body — not a re-implementation — and obtaining `a·2^(exp(b)−127)` for *negative* `a`
> (sign preserved), *fractional* `b` (negative exponent), and an E8M0-realistic scale
> (`s=134 → 2⁷`) rules out the failure modes "it's just a multiply by `b`" (it is not —
> only `b`'s *exponent* matters, the mantissa of `b` is discarded by the `>>23`) and
> "it's `exp(a)+exp(b)` without the −127 rebias" (the `0x81` constant supplies exactly
> the −127). The value semantics are confirmed against the real oracle, not asserted.

### 4.2 The `addexpm` variant — the packed-register form

The `addexpm` sibling (`module__xdref_addexpm_32f_32f_32f @0x87a5e0`) reads the scale
exponent from **bits [21:14]** of operand `b` (disasm `shr $0xe` = `>>14`), not the
fp32 exponent field, and XORs a sign bit from `b[22]`. Driving it live: packing the
E8M0 code `s` into bits 14–21 (`b = s << 14`) yields `addexpm(a, b) = a·2^(s−127)`
bit-exact (`a=4.0`: `s=127→4.0`, `s=128→8.0`, `s=130→32.0`, `s=124→0.5`). `[HIGH/OBSERVED
— live drive]`

> **QUIRK — `addexpm` is the *packed-lane* exponent-add; `addexp` is the *float-field*
> form.** `addexp` reads the scale from `b`'s standard fp32 exponent field (`b>>23`);
> `addexpm` reads it from a sub-word lane (`b>>14`) **and** folds a sign. This is
> precisely why the firmware uses the **packed-register** MAC: the E8M0 byte sits in a
> sub-word lane of the `xb_int64pr` scale register (placed there by
> `ivp_dextrprn_2x32`), not in an fp32 exponent field — so the device leaf is the
> `addexpm`-shaped (`bsubnorm`/`baddnorm`) form, and the host `addexp` is the
> scalar-field equivalent. Both compute `a·2^(s−127)`; they differ only in **where the
> scale exponent is read from**. `[HIGH/OBSERVED — both bodies driven live; the lane-vs-
> field mapping INFERRED-HIGH.]`

⇒ `(bits>>23)&0xFF` max-block `−max_exp` == `srln`+`bmaxn` ; `÷/×2^(scale−127)` ==
`bsubnorm`/`baddnorm` == `mulus4t(…,pr8)` == `addexp`/`addexpm`. **Bit-exact across all
five altitudes.** The PE MX matmul applies the inverse (×scale) via the *same*
`mulus4t…xr…` / `dmulqa2n8xr8` packed-reg MAC (§3.4).

---

## 5. The SBUF scale scatter — ISA-header-confirmed (×32 quadrant)

[mx-path §5](mx-path.md) grounds the ×32 quadrant scatter in the sim and the
`SundaISel` emitter docstring. The device body adds the **ISA-validity gate** —
`valid_s3dmx1_quant_sbuf_quadrant` (read verbatim from the MARIANA header behaviour
spec) requires **both** the dst DATA address and the dst SCALE address to lie in SBUF
partition `{0, 32, 64, 96}`: `[HIGH/OBSERVED]`

```
fn valid_s3dmx1_quant_sbuf_quadrant(addr0, addr1) -> bool {
      (addr_in_sbuf_partition(0,  addr0) && addr_in_sbuf_partition(0,  addr1))
   || (addr_in_sbuf_partition(32, addr0) && addr_in_sbuf_partition(32, addr1))
   || (addr_in_sbuf_partition(64, addr0) && addr_in_sbuf_partition(64, addr1))
   || (addr_in_sbuf_partition(96, addr0) && addr_in_sbuf_partition(96, addr1))
}
```

with `num_active_channels ∈ {32, 64, 96, 128}`. So the hardware-validity rule for the
forward op **IS** the ×32 quadrant placement: data and its E8M0 scale must co-reside in
the same quadrant `{0,32,64,96}` — exactly the `0-3/32-35/64-67/96-99` positions
[mx-path §5](mx-path.md) derived from the sim and the host emitter. The
`MXTENSOR1D.scale_pidx@11` field selects the scale partition within the quadrant. The
device-validity gate and the host emitter agree field-for-field.

---

## 6. The opcode ledger — the MX cluster, struct-pinned

| host op (nki) | device opcode | engine | gen | ISA struct (compile-verified) | scale |
|---|---|---|---|---|---|
| `quantize_mx` (FWD pack) | `0xE3 QUANTIZE_MX` | DVE (Vector) | **v4+** | `S3DMX1_QUANT` (64B) src@16 / dst MxMem1d@48 | dst `scale_addr` (E8M0) |
| *(standalone dequant)* | `0x7B TENSOR_DEQUANTIZE` | POOL (GpSimd) | all gens | `S3D3_TENS_DEQUANT` (64B) | in-band (group of 8) |
| `nc_matmul_mx` (v4) | `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` | PE (Tensor) | v4+ | `SMX1_LW` / `SMX1D3_MM` (64B) | `MXTENSOR1D.scale_addr@4` (oob) |
| `nc_matmul_mx` (v5) | `0x01 LDWEIGHTS` / `0x02 MATMUL` + `MXTENSOR_V2` | PE (Tensor) | v5 | `MEM_PATTERN3D.mx = MXTENSOR_V2` (marker 0x01) | `MXTENSOR_V2.scale_addr@4` (oob) |
| *(E8M0 scale dtype)* | `SFP8_E8 = 0x13 = FP8_S0E8M0` | — | — | power-of-two block scale, bias 127, no sign | — |

`[opcode numerics + engine + gen CARRIED from the ledger and the firmware pages; struct
offsets + opcode enums + validity predicates OBSERVED (compile-verified); the
`0xE3` DVE body (§2) + PE dispatch/scale-MAC (§3) OBSERVED.]`

### 6.1 The gen-floor question — RESOLVED from the device body's own gen-gate

[mx-path §6.3](mx-path.md) flagged a divergence: the
[opcode ledger](../firmware/kernels/opcode-catalog-ledger.md) + the host `.pyi` place
`0xE3` from **v4** (`--YYY`), while [mx-dequant §6.2](../firmware/kernels/mx-dequant.md)
quotes `s3dmx1_quant_valid_nc: nc == V5` and [dtype-model §2.4](../firmware/kernels/dtype-model.md)
calls `QuantizeMX` "NC-v5". This page resolves it by reading **both** per-arch headers'
gen-gate directly: `[HIGH/OBSERVED — header byte-check]`

```
neuron_mariana_arch_isa/tpb/aws_neuron_isa_tpb_s3dmx1_quant.h:142
    // fn s3dmx1_quant_valid_nc(nc) -> bool {  (nc == NeuronCoreVersion::V4)  }
neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_s3dmx1_quant.h:103
    // fn s3dmx1_quant_valid_nc(nc) -> bool {  (nc == NeuronCoreVersion::V5)  }
```

The header is **present in mariana(v4) + maverick(v5), absent in sunda(v3) + cayman(v3)**
(byte-checked: `QUANTIZE_MX` matched in 6 files per gen for mariana/maverick, **0** for
sunda/cayman). Each per-arch header gates `0xE3` on **its own** core version: the
MARIANA header validates `nc == V4`, the MAVERICK header validates `nc == V5`.

> **CORRECTION — the `0xE3` gen floor is NC-v4 (MARIANA), and "nc == V5" is not a
> global floor.** The `nc == V5` predicate that [mx-dequant §6.2](../firmware/kernels/mx-dequant.md)
> quotes is the **MAVERICK** header's gate (correct for the v5 build); the **MARIANA**
> header's own `s3dmx1_quant_valid_nc` reads `nc == V4`. There is **no contradiction with
> the ledger** — the opcode/struct first appear at **MARIANA (v4)**, and each per-gen
> header simply asserts its own gen. The "NC-v5 only" framing in
> [mx-dequant §6.2](../firmware/kernels/mx-dequant.md) and
> [dtype-model §2.6](../firmware/kernels/dtype-model.md) is **gen-local to the maverick
> header read**, not the global availability floor. A reimplementer should treat the
> floor as **v4** and use the per-target header's own `s3dmx1_quant_valid_nc` as the
> validity gate (`V4` on MARIANA, `V5` on MAVERICK). `[HIGH/OBSERVED — both per-arch
> headers byte-read; downgrades [mx-path §6.3](mx-path.md)'s flagged MED divergence to a
> resolved CORRECTION.]`

### 6.2 Forward / inverse struct distinctness

The forward `S3DMX1_QUANT` (`0xe3`, DVE) and the inverse `S3D3_TENS_DEQUANT` (`0x7b`,
POOL) are **two distinct 64-byte structs** with distinct opcodes on distinct engines —
confirming the [mx-path §6.2 CORRECTION](mx-path.md) at the **struct** level, not just
the engine-tag level. The inverse `0x7b` body
(`proc_4bit_mx_8 @0x0100511c`, group-of-8 in-band scale, the `8:5` block ratio, the
`ivp_mulus4tan16xr16(…,pr1)` / `ivp_dmulqa2n8xr8(…,pr0)` MAC + `ivp_cvtg48n_2x32l`
extract) is decoded on [the MX dequant page §5](../firmware/kernels/mx-dequant.md); it
is a separate, older block-of-8 mechanism (NC-v3+) — do not conflate it with the OCP
block-of-32 surface this page traces.

---

## 7. Cross-check verdict

| axis | finding | status |
|---|---|---|
| fwd `0xE3` DVE body | `srln(>>23)`+`bmaxn` extract ; `mulus4t(pr8)`+`bsubnorm`/`baddnorm` ÷×ldexp ; `cvtg48`+`ueqn`+`sel` pack | **OBSERVED** ✔ (was INFERRED) |
| fwd ISA struct | `S3DMX1_QUANT` 64B (src@16 / num\_chan@33 / in@34 / out@35 / dst MxMem1d@48) | OBSERVED ✔ (compile-verified) |
| fwd fp8-only output | `valid_out_dtype` ⇒ {FP8\_EXP5, FP8\_EXP4}, `AllowFP4::False` | OBSERVED ✔ |
| ×32 quadrant scatter | ISA gate `{0,32,64,96}` data+scale same quad ; `num_active_channels∈{32,64,96,128}` | CONFIRMED ✔ (header) |
| v4 PE dispatch | `0x09→0x2baf` / `0x0A→0x2bb7` (`sha 6600e24a`) | OBSERVED ✔ |
| v4 PE structs | `SMX1_LW`/`SMX1D3_MM` 64B ; `MXTENSOR1D.scale_addr@4` (oob E8M0) | OBSERVED ✔ (compile-verified) |
| v5 `MXTENSOR_V2` fold | union member, `ADDR4` marker `0x01` selects MX ; firmware interior | struct OBSERVED ✔ ; v5 interior INFERRED |
| scale-application tap | `extrpr → xb_int(32/64)pr → mulus4t`/`dmulqa` MAC | OBSERVED ✔ (micro-tap MED) |
| value tie (5 altitudes) | sim == torch == FW == `addexp`/`addexpm` (driven live) | bit-exact ✔ |
| E8M0 scale dtype | `SFP8_E8 = 0x13 = FP8_S0E8M0`, bias 127 | ✔ (carried) |
| `0xE3` gen floor | MARIANA header `nc==V4` ; MAVERICK header `nc==V5` ; absent v3 | **RESOLVED** ✔ (was FLAGGED) |

**Verdict:** the [mx-path §7](mx-path.md) INFERRED-HIGH residual is **closed**. The
forward `0xE3` body is byte-decoded; the v4/v5 PE path is struct-pinned + firmware-
confirmed; the scale-application is the packed-register MAC on both engines; the value
model is bit-exact across sim/torch/firmware/ISS with the firmware rung now OBSERVED and
the ISS leaf **driven live**. One **CORRECTION** (the `0xE3` gen floor is v4, resolving
the flagged divergence) and one operand-name reconciliation (`MXMEM_PATTERN1D ⊃
MXTENSOR1D`); **zero opcode-numeric disagreements** with the sibling pages.

---

## 8. Reimplementation checklist

1. **Forward `0xE3` (DVE).** Emit a FLIX kernel with three legs: (a) **extract** —
   `srln_2x32(src_u32, 23)` then `bmaxn_2x32` over each 8×4 block (= E8M0); capture into
   a packed scale register via `dextrprn_2x32`. (b) **divide+narrow** —
   `mulus4tn16xr16(src, pr8)` then `trunc16nxf16(·,7)` then `bsubnormnx16` (the
   `÷2^(scale−127)`). (c) **clamp+pack** — `cvtg48n_2x32h`, `ueqn_2xf32t` (`±max_val`
   from [mx-path §4](mx-path.md): `448.0`/`57344.0`), `sel2nx8i_s4` (4 fp8 → one
   `uint32`). Output **fp8 only** (`out_dtype ∈ {FP8_EXP4, FP8_EXP5}`). Emit the
   `S3DMX1_QUANT` 64B struct (src MEM_PATTERN3D@16, dst MXMEM_PATTERN1D@48), and enforce
   `valid_s3dmx1_quant_sbuf_quadrant` (data+scale in the same `{0,32,64,96}` partition).
2. **Scale application = the packed-register MAC.** Put the E8M0 byte in the
   `xb_int64pr` sub-word lane (the `addexpm`/`bsubnorm`/`baddnorm` form), not an fp32
   exponent field; the value semantics are `a·2^(s−127)` (= `xdref_addexp`, verified live).
3. **MX matmul (PE).** v4: emit `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` (`SMX1_LW` /
   `SMX1D3_MM` 64B), out-of-band E8M0 at `MXTENSOR1D.scale_addr@4`; the MAC carries the
   scale via the packed register (`extrpr2nx8 → mulus4tan16xr16` / `dmulqa2n8xr8`). v5:
   emit `0x01`/`0x02` with `MEM_PATTERN3D.mx = MXTENSOR_V2` selected by
   `start_addr.marker_a4 == 0x01`; perf mode `QUAD_ROW=0x1` / `OCT_ROW=0x4`.
4. **Gen gate.** Target the **v4** floor; validate against the per-target header's own
   `s3dmx1_quant_valid_nc` (`V4` on MARIANA, `V5` on MAVERICK). Do not treat "NC-v5" as a
   global floor.
5. **Do not conflate** the inverse `0x7b` POOL block-of-8 in-band dequant
   ([mx-dequant](../firmware/kernels/mx-dequant.md)) with this OCP block-of-32 surface.

---

## 9. Honest limitations

- **The DVE descriptor→body numeric edge is data-driven** (the `kernel_info` `callx8`),
  so the forward body is reached by its IVP fingerprint (HIGH attribution), not a static
  pointer — the byte-exact body-start VA is INFERRED (§1 GOTCHA).
- **The PE-side MX scale micro-tap** (multiplier-input vs PSUM-drain) is MED/INFERRED —
  the array RTL is out of corpus; the packed-reg-MAC operand chain is the OBSERVED anchor
  (§3.4 NOTE).
- **v5/MAVERICK MX firmware interiors are header-OBSERVED only.** `MXTENSOR_V2` /
  `ADDR4MARKER` / `MX_PERF_MODE` are compile-verified; the v5 handler interiors are
  INFERRED (the MARIANA images carry the shared FLIX kernel for the v4/v5 body).
- **`addexpm`'s exact lane→firmware-op mapping is INFERRED-HIGH** — the body is driven
  live (the `a·2^(s−127)` value is OBSERVED), but the identification of `addexpm`'s
  bits[21:14] lane with the firmware `dextrprn → xb_int64pr` packed lane is reasoned, not
  byte-traced through the FLIX bundle (§4.2 QUIRK).
- **`max_val`/`max_exp` constants** (`8/448.0`, `15/57344.0`) are CARRIED from
  [mx-path §4](mx-path.md) (the shipped nki sim); they are not spelled in the firmware
  bodies (the `ueqn_2xf32t` compares against a runtime-loaded immediate).

---

## See also

- [The MX Microscaling Path (end-to-end)](mx-path.md) — the compiler-side lowering map
  this page closes (the `0xE3` forward / `0x7B` inverse split, the value model, the ×32
  scatter).
- [MX (Microscaling) Dequant Compute Paths](../firmware/kernels/mx-dequant.md) — the
  inverse `0x7b` POOL block-of-8 dequant (`proc_4bit_mx_8`); the maverick `nc == V5`
  validity quote reconciled in §6.1.
- [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) — `LdweightsMX 0x09` /
  `MatmulMX 0x0A` (v4) and the unified `MXTENSOR_V2` matmul (v5); the `MXMEM_PATTERN1D`
  operand naming reconciled in §3.2.
- [The unified datatype model](../firmware/kernels/dtype-model.md) — `SFP8_E8 = 0x13 =
  FP8_S0E8M0` (E8M0), bias 127, the FP32 convert hub.
