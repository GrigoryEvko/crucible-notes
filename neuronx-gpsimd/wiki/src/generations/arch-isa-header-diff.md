# Cross-Generation Arch-ISA Header Diff

This page is the per-generation **operand-struct + enum reference** for the GPSIMD
arch-ISA. Every NeuronCore generation ships its own `aws_neuron_isa_tpb*.h` header
family that defines the 64-byte instruction word as a union of per-shape operand
structs (`S4D4_TR`, `S3D3_MM`, `S3_LW`, …) plus the shared enum vocabulary
(`NEURON_ISA_TPB_DTYPE`, `…_ALU_OP`, `…_OPCODE`, …). The headers are **shipped,
compile-grade C** — every size/offset claim on this page is anchored to a
`header:line` and verified by a real `gcc` `_Static_assert` / `offsetof` run, *not*
hand-computed.

The five generation trees are:

| Codename | NC-ver | arch_id | coretype | ISA-header tree | Banner |
|----------|--------|---------|----------|-----------------|--------|
| TONGA    | V1     | — (none)      | — (none)     | `arch-isa/`                  | `Copyright 2018` (no `NC-v#` line) |
| SUNDA    | v2     | `0x05`        | `6`          | `neuron_sunda_arch_isa/`     | `ISA header for NC-v2.` |
| CAYMAN   | v3     | `0x0c`        | `13`         | `neuron_cayman_arch_isa/`    | `ISA header for NC-v3.` |
| MARIANA  | v4     | `0x14`        | `21`         | `neuron_mariana_arch_isa/`   | `ISA header for NC-v4.` |
| MARIANA_PLUS | v4+ | `0x1c`       | `29`         | *(shares MARIANA — no own ISA tree)* | — |
| MAVERICK | v5     | `0x24` *(INFERRED)* | `37` *(OBSERVED)* | `neuron_maverick_arch_isa/` | `ISA header for NC-v5.` |

`[HIGH/OBSERVED — banners read from each tree's tpb/aws_neuron_isa_tpb_common.h:1-4; arch_id/coretype carried from`
[`codename-generation-map.md`](codename-generation-map.md)`.]`

> **NOTE — MAVERICK confidence split.** The MAVERICK *headers* are present and
> readable, so every v5 **struct layout / enum value** on this page is **HIGH ×
> OBSERVED** (compile-verified). What is *not* observed is the v5 **runtime**
> envelope: `arch_id 0x24` is **INFERRED** (no shipped v5 NCFW image), `coretype 37`
> is **OBSERVED**. Treat "header-observed v5 layout" and "v5 firmware interior" as
> two different confidence classes.

> **GOTCHA — the headers are gitignored.** They live under `extracted/`, which the
> repo `.gitignore` excludes. Default `fd` / `rg` (which honor `.gitignore`) return
> **0 hits** and make the headers look absent. Use `--no-ignore` or absolute paths.
> The one physical location:
> ```
> INC=…/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/include
> ```
> with `$INC/neuron_{sunda,cayman,mariana,maverick}_arch_isa/{common,tpb}/` for the
> four modern trees and `$INC/arch-isa/{common,sp,tpb}/` for the legacy TONGA set.

**Cross-links:** the DTYPE consumer model in
[`../firmware/kernels/dtype-model.md`](../firmware/kernels/dtype-model.md); the
opcode-value diff in [`cross-gen-opcode-diff.md`](cross-gen-opcode-diff.md); the
per-gen profiles [`sunda-v2-baseline.md`](sunda-v2-baseline.md),
[`mariana-plus-delta.md`](mariana-plus-delta.md),
[`maverick-profile.md`](maverick-profile.md); the codename map
[`codename-generation-map.md`](codename-generation-map.md).

---

## 1. Header-set inventory + the definitive availability map

### 1.1 Tree-level inventory

Each modern tree carries the same *spine* — `aws_neuron_isa_tpb_common.h` (the master
struct/enum file), `_enums.h`, `_assert.h`, `_extended.h`, `_extended_utils.h`, the
per-shape operand headers (`s4d4_*`, `s3d3_*`, `s3_lw`, …), and `instruction_mapping.json`
(the opcode↔struct binding, §4). File counts grow **strictly monotonically** with
generation — a capability signature.

| Tree | `tpb/*.h` | `common/*.h` | `instruction_mapping.json` |
|------|-----------|--------------|----------------------------|
| `neuron_sunda_arch_isa`    | **99**  | 1 | 1 |
| `neuron_cayman_arch_isa`   | **108** | 3 | 1 |
| `neuron_mariana_arch_isa`  | **117** | 3 | 1 |
| `neuron_maverick_arch_isa` | **123** | 3 | 1 |
| `arch-isa/` (TONGA)        | **40** `tpb` + 20 `sp` + 2 `common` | — | *(none — no JSON)* |

`[HIGH/OBSERVED — fd --no-ignore -e h per tree.]`

> **NOTE — header count vs. report.** GEN-05 §2 cited "~62 tpb headers" for SUNDA;
> the filesystem reports **99** `.h` files in `neuron_sunda_arch_isa/tpb`. The "~62"
> was a count of operand-struct headers only (excluding `_enums.h`, `_assert.h`,
> sub-include helpers, etc.). The authoritative figure for *every `.h` in the tpb dir*
> is the table above. `[CORRECTION — fd --no-ignore -e h, count is 99, not ~62.]`

`mariana_plus` ships **no** `neuron_mariana_plus_arch_isa/` ISA tree — it reuses the
MARIANA ISA headers verbatim. It *does* carry its own **register-map** dir
(`arch-headers/mariana_plus/`, 848 files, distinct from `arch-headers/mariana/`'s 832)
— but those are CSR/address maps, **not** operand-struct ISA. So MARIANA_PLUS is an
NCFW/register-map/coretype delta on the MARIANA *silicon ISA*; it adds **0 new ISA
bytes**. See [`mariana-plus-delta.md`](mariana-plus-delta.md). `[HIGH/OBSERVED —
ls neuron_mariana_plus_arch_isa → "No such file"; arch-headers/mariana_plus = 848 files.]`

### 1.2 The header-availability grid (gen × header)

The **definitive** add/remove map, built from `comm` over the per-gen tpb-header
basename sets — not from any report. `●` = present, `–` = absent.

| Header (`aws_neuron_isa_tpb_…`) | SUNDA | CAYMAN | MARIANA | MAVERICK | Binds opcode (§4) |
|---|:---:|:---:|:---:|:---:|---|
| `…_common.h` / `_enums.h` / `_assert.h` (spine) | ● | ● | ● | ● | — |
| `…_s4d4_tr.h` / `_s4d4_pl.h` / `_s4d4_cr.h`      | ● | ● | ● | ● | tensor-reduce / pool / cross-lane |
| `…_s3d3_mm.h` / `_s3_lw.h`                       | ● | ● | ● | ● | MATMUL / LDWEIGHTS |
| `…_extended.h` / `_extended_utils.h`            | ● | ● | ● | ● | RDMA / ENOP / COPY / SB2SB … |
| `…_custom_op_header.h` / `_custom_op_payload.h` | ● | – | – | – | *(removed at v3)* |
| `…_nx_map.h`                                     | ● | – | – | – | *(removed at v3 — no binding)* |
| `…_dma_direct2d_xpose.h` / `_dma_gather_xpose.h` | – | ● | ● | ● | DMA xpose |
| `…_pseudo_jpeg_decode.h` / `_s2d2_jpeg.h`        | – | ● | ● | ● | JPEG |
| `…_s2_convlut.h` / `_s2d2_rs.h` / `_s3_lt.h`     | – | ● | ● | ● | conv-lut / RS / list |
| `…_s3d3_collective.h`                            | – | ● | ● | ● | collective |
| `…_s3d3_nonzero_with_count.h` / `_s3d3_seq_bounds.h` | – | ● | ● | ● | nonzero / seq-bounds |
| `…_s3d3_tens_dequant.h`                          | – | ● | ● | ● | TENSOR_DEQUANTIZE |
| `…_s4d3_mm.h`                                    | – | ● | ● | ● | MATMUL_SPARSE |
| `…_smx1d3_mm.h` / `_smx1_lw.h` / `_s3dmx1_quant.h` | – | – | ● | ● | MATMUL_MX / LDWEIGHTS_MX / QUANTIZE_MX |
| `…_ctrl_test_es.h` / `_d3_rand.h` / `_s2d2_ac.h` | – | – | ● | ● | test-es / rand / accumulate |
| `…_s2d2d2_sc.h` / `_s3d3_sc.h` / `_s2s1d2_pe_seed.h` | – | – | ● | ● | scatter / scatter / PE-seed |
| `…_ctrl_cci.h` / `_dma_copy2d.h` / `_dma_immediate.h` | – | – | – | ● | CCI / copy2d / immediate |
| `…_s1s2d2_am.h` / `_s2d2d2_ts_wide.h` / `_s2s2d2d2_tt.h` | – | – | – | ● | AM / TS-wide / TT |

**Transition counts (basename `comm`):**

| Step | added | removed |
|------|:-----:|:-------:|
| SUNDA → CAYMAN  | **+12** | **−3** (`custom_op_header`, `custom_op_payload`, `nx_map`) |
| CAYMAN → MARIANA | +9 | 0 |
| MARIANA → MAVERICK | +6 | 0 |

`[HIGH/OBSERVED — comm -13/-23 over fd-derived basename lists.]`

> **CORRECTION — SUNDA→CAYMAN removals are 3 header files, not 2.** GEN-05 §9
> lists "−2" removals (`CUSTOM_OP_HEADER`, `CUSTOM_OP_PAYLOAD`). The filesystem shows
> **three** removed `.h` files — the report missed `aws_neuron_isa_tpb_nx_map.h`. The
> "−2" is correct *for the `instruction_mapping.json` struct2opcode keys* (`nx_map.h`
> defines no operand struct, so it has no opcode binding to remove), but the *header
> file* count drops by 3. The custom-op pair are the only removals carrying an opcode
> binding. `[CORRECTION — comm -23 sunda→cayman returns 3 files; struct2opcode keys
> drop only 2.]`

These removals/additions are **purely additive after CAYMAN** (no removals in any
v3→v4 or v4→v5 step). The custom-op header/payload pair (SUNDA-only) is the one piece
ever *removed* from the operand namespace.

---

## 2. Core-struct cross-gen field diff (compiled sizeofs)

### 2.1 The 64-byte word is invariant V1..V5

The instruction word width is a fixed constant in every tree:

* `static const uint32_t NEURON_ISA_TPB_INST_NBYTES = 64U;` (modern `common.h:27`)
* `TONGA_ISA_TPB_INST_NBYTES = 64` (`arch-isa/tpb/aws_tonga_isa_tpb_common.h:14`)

Every operand struct carries its own `ISA_STATIC_ASSERT(sizeof(...) == 64, ...)`
(e.g. `cayman s4d4_tr.h:43`). The verify macros (`cayman common.h`):
`NEURON_ISA_PACKED = __attribute__((__packed__))`, and
`ISA_STATIC_ASSERT → _Static_assert` (C11) / `static_assert` (C++11).

**Compile-verified** (`gcc 16.1.1`, `-std=c11`). The probe — one TU per tree,
including only the per-shape header + `aws_neuron_isa_tpb_common.h`:

```c
/* probe_core.c — compiled with: gcc -std=c11 -I "$INC/neuron_<gen>_arch_isa/tpb" probe_core.c */
#include <stdint.h>
#include <stddef.h>
#include "aws_neuron_isa_tpb_s4d4_tr.h"
#include "aws_neuron_isa_tpb_s4d4_pl.h"
#include "aws_neuron_isa_tpb_s4d4_cr.h"
#include "aws_neuron_isa_tpb_s3d3_mm.h"
#include "aws_neuron_isa_tpb_s3_lw.h"
_Static_assert(sizeof(NEURON_ISA_TPB_S4D4_TR_STRUCT) == 64, "S4D4_TR != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_S4D4_PL_STRUCT) == 64, "S4D4_PL != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_S4D4_CR_STRUCT) == 64, "S4D4_CR != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_S3D3_MM_STRUCT) == 64, "S3D3_MM != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_S3_LW_STRUCT)   == 64, "S3_LW != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_HEADER) == 4, "HEADER != 4");
_Static_assert(sizeof(NEURON_ISA_TPB_EVENTS) == 8, "EVENTS != 8");
_Static_assert(sizeof(NEURON_ISA_TPB_DTYPE)  == 1, "DTYPE != 1");
```

**Compiled-sizeof matrix** (every cell is a *passed* `_Static_assert` + the
`printf`'d `sizeof`, all four gens identical):

| Struct | SUNDA | CAYMAN | MARIANA | MAVERICK | TONGA *(legacy names)* |
|--------|:-----:|:------:|:-------:|:--------:|:----------------------:|
| `S4D4_TR_STRUCT` | 64 | 64 | 64 | 64 | — |
| `S4D4_PL_STRUCT` | 64 | 64 | 64 | 64 | — |
| `S4D4_CR_STRUCT` | 64 | 64 | 64 | 64 | — |
| `S3D3_MM_STRUCT` | 64 | 64 | 64 | 64 | `MATMUL_INST` = **64** |
| `S3_LW_STRUCT`   | 64 | 64 | 64 | 64 | `LDWEIGHTS_INST` = **64** |
| `HEADER`         | 4  | 4  | 4  | 4  | `INST_HEADER` = **4** |
| `EVENTS`         | 8  | 8  | 8  | 8  | `INST_EVENTS` = **4** ← |
| `DTYPE`          | 1  | 1  | 1  | 1  | `DTYPE` = **1** |

`[HIGH/OBSERVED — gcc -std=c11 -I "$INC/neuron_<gen>_arch_isa/tpb" probe_core.c; all
8 asserts pass per gen. TONGA: gcc -std=c11 -I "$INC/arch-isa/tpb" -I
"$INC/arch-isa/common" -I "$INC" probe_tonga.c.]`

The single `EVENTS` size difference (modern **8B** vs TONGA **4B**) is the legacy→modern
transition, characterized in §5.

### 2.2 The extended + MX operand families (also all 64B)

```c
/* probe_ext.c — extended_utils.h MUST follow extended.h (it uses
   NEURON_ISA_TPB_EXT_COMPLETION_INFO defined there — naive order → "unknown type"). */
#include "aws_neuron_isa_tpb_common.h"
#include "aws_neuron_isa_tpb_extended.h"
#include "aws_neuron_isa_tpb_extended_utils.h"
_Static_assert(sizeof(NEURON_ISA_TPB_EXTENDED_STRUCT)               == 64, "EXTENDED != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_EXTENDED_ENOP_STRUCT)          == 64, "ENOP != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_EXTENDED_SB2SB_STRUCT)         == 64, "SB2SB != 64");
_Static_assert(sizeof(NEURON_ISA_TPB_EXTENDED_RDMA_DESC_GEN_STRUCT) == 64, "RDMA_GEN != 64");
/* … COPY / TTA / RSS / RGS / CPTCD / RDMA_DESC_START also asserted == 64 … */
```

| Family | structs (each = 64B) | SUNDA | CAYMAN | MARIANA | MAVERICK |
|--------|----------------------|:-----:|:------:|:-------:|:--------:|
| Extended | `EXTENDED`, `EXTENDED_{ENOP,COPY,TTA,RSS,RGS,SB2SB,CPTCD}`, `RDMA_DESC_{GEN,START}` | 64 | 64 | 64 | 64 |
| MX (`probe_mx.c`) | `SMX1D3_MM`, `SMX1_LW`, `S3DMX1_QUANT`, `S4D3_MM` | n/a | n/a *(MX absent)* | 64 | 64 |

`[HIGH/OBSERVED — probe_ext.c compiles+asserts on all 4 gens; probe_mx.c compiles only
on mariana/maverick. On cayman it fails: "aws_neuron_isa_tpb_smx1d3_mm.h: No such file"
— the MX headers are genuinely absent at v3.]`

> **NOTE — SB2SB is header-present in SUNDA but runtime-absent at v2.** The header
> `EXTENDED_SB2SB_STRUCT` is defined and compiles to 64B in **all four** modern trees,
> including SUNDA (`sunda extended_utils.h:143`). At the *opcode/runtime* level, however,
> the SB2SB collective (`0xBF`) first appears at CAYMAN (SUNDA does cross-die via RDMA
> `gen/start` + sendrecv only — see [`cross-gen-opcode-diff.md`](cross-gen-opcode-diff.md)).
> So "SUNDA has no SB2SB" is true of the *opcode roster*, not the *header struct set*.
> Header-presence ≠ opcode-presence. `[HIGH/OBSERVED — rg -c EXTENDED_SB2SB_STRUCT sunda = 3.]`

### 2.3 `S4D4_TR` field-offset table (the most-bound operand, 11 opcodes)

Compiled `offsetof` confirms `in_dtype` @ **32**, `out_dtype` @ **33** in every gen.
The full body (cayman `s4d4_tr.h:28-41`; **byte-identical offsets all four gens**):

| off | size | field | type |
|----:|-----:|-------|------|
| 0  | 4  | `header`              | `NEURON_ISA_TPB_HEADER` |
| 4  | 8  | `events`              | `NEURON_ISA_TPB_EVENTS` |
| 12 | 20 | `src_mem_pattern`     | `TENSOR4D` *(sunda,cayman)* \| `MEM_PATTERN4D` *(mariana,maverick)* † |
| 32 | 1  | `in_dtype`            | `NEURON_ISA_TPB_DTYPE` |
| 33 | 1  | `out_dtype`           | `NEURON_ISA_TPB_DTYPE` |
| 34 | 1  | `num_active_channels` | `uint8_t` |
| 35 | 1  | `negated`             | `uint8_t` |
| 36 | 1  | `op`                  | `NEURON_ISA_TPB_ALU_OP` |
| 37 | 1  | `op_dim`              | `NEURON_ISA_TPB_TENSOR_SUBDIM` |
| 38 | 1  | `mask_enable`         | `uint8_t` |
| 39 | 5  | `reserved1[5]`        | `uint8_t` |
| 44 | 20 | `dst_mem_pattern`     | *(same type as src)* |

† **The only cross-gen change in this struct is the mem-pattern TYPE** — see §2.5.
`[HIGH/OBSERVED — probe_core.c offsetof: in_dtype=32, out_dtype=33 on all 4 gens;
field bodies sed'd from each gen's s4d4_tr.h.]`

### 2.4 The compute-struct rework — `S3D3_MM` (matmul) field diff

Unlike the stable tensor-reduce/pool/cross-lane operands, the matmul/ldweight structs
**rework their internals** each generation while staying 64B (reserved-byte rebalancing).
Every cell is the **compiled offset** (the `// off (lo - hi)` comment in the header is
the vendor annotation; it matches the C layout — all gens pass the 64B assert).

| field | SUNDA off | CAYMAN off | MARIANA off | MAVERICK off |
|-------|:---------:|:----------:|:-----------:|:------------:|
| `header` (4) / `events` (8) | 0 / 4 | 0 / 4 | 0 / 4 | 0 / 4 |
| `reserved0[4]`              | 12 | 12 | 12 | 12 |
| `src_mem_pattern` (16)      | 16 `TENSOR3D` | 16 `TENSOR3D` | 16 `MEM_PATTERN3D` | 16 `MEM_PATTERN3D` |
| `in_dtype`                  | 32 | 32 | 32 | 32 |
| `fp32_mode`                 | 33 | 33 | 33 | 33 |
| `transpose_mode`            | – `reserved1[1]@34` | **34** | **34** | – *(removed)* |
| `out_dtype`                 | – | 40 | 40 | **34** |
| `perf_opt`                  | 35 | 35 | 35 | – *(removed)* |
| `quant_offset` (2)          | **36** `QUANT_OFFSET` | – `reserved_quant[2]@36` | – `reserved_quant[2]@36` | – *(removed)* |
| `num_active_rows`/`cols`    | 38/39 | 38/39 | 38/39 | **35/36** |
| `tile_size` / `tile_sel`    | – | – | – | **37 / 38** |
| `flags` (`MATMUL_FLAGS`)    | – | – | **42** | **39** |
| `perf_mode` (`MX_PERF_MODE`)| – | – | – | **40** |
| `psum_accumulate_flags`     | 43 | 43 | 43 | – *(removed)* |
| `row_grp` / `col_grp`       | 44 / 45 | 44 / 45 | 44 / 45 | – *(removed)* |
| `xbus_sel`                  | **46** | – `reserved_xbs[1]@46` | – `reserved_xbs[1]@46` | – *(removed)* |
| `psum_zero_region`          | 47 | 47 | 47 | – `reserved1[7]@41` |
| `dst_mem_pattern` (16)      | 48 | 48 | 48 `MEM_PATTERN3D` | 48 `MEM_PATTERN3D` |

**Reading.** SUNDA/CAYMAN are the classic quant-offset/xbus matmul; **CAYMAN** drops
the explicit `quant_offset`/`xbus_sel` (→ reserved) and gains `out_dtype` (@40) +
`transpose_mode` (@34). **MARIANA** swaps `TENSOR3D` → `MEM_PATTERN3D` (indirect
addressing) and adds a `MATMUL_FLAGS flags` byte (@42). **MAVERICK** is the
**microscaled (MX) reorg**: it *deletes* `transpose_mode`/`perf_opt`/`row_grp`/`col_grp`/
`xbus_sel`/`psum_accumulate_flags`/`psum_zero_region`, shifts `out_dtype`→34 and
`num_active_rows/cols`→35/36, and adds `tile_size`/`tile_sel`/`perf_mode`
(`MX_PERF_MODE`). All four still assert 64B. `[HIGH/OBSERVED — full bodies sed'd per
gen; sizeof=64 compiled.]`

`S3_LW` (ldweights) follows the same arc: SUNDA/CAYMAN carry `quant_offset`/`xbus_sel`;
CAYMAN adds `transpose_mode`; **MARIANA** adds `bg_xpose_mode` (@12, a `BG_XPOSE_MODE`
that pushes `reserved0` to 3B) + `LD_WEIGHT_FLAGS flags` (@47); **MAVERICK** drops the
classic transpose/perf/grp fields and adds `tile_size`/`tile_sel`/`perf_mode` (the same
MX reorg). `[HIGH/OBSERVED.]`

> **QUIRK — the MARIANA `S3_LW` moves a non-reserved field into the header gap.**
> Every other operand keeps `reserved0[4]` at offset 12. MARIANA's `S3_LW` puts a
> real field there: `NEURON_ISA_TPB_BG_XPOSE_MODE bg_xpose_mode` @ **12** (4B), then
> `reserved0[3]` @ 13. So a v4 ldweight word has live bytes at 12-15 that are reserved
> in every other gen's ldweight — a decoder keying byte 12 blindly will mis-decode the
> background-transpose mode. (MAVERICK reverts byte 12 to `reserved0[4]`.)
> `[HIGH/OBSERVED — mariana s3_lw.h:12; sunda/cayman/maverick s3_lw reserved0[4]@12.]`

**The stable operands** (md5-identical body across all four gens): `S4D4_PL` (12 fields)
and `S4D4_CR` (11 fields). The pool + cross-lane-reduce operands never changed.
`[HIGH/OBSERVED.]`

### 2.5 The mem-pattern union evolution (a packing quirk)

`TENSOR4D` is a 20-byte direct addressing descriptor:

```c
/* cayman common.h:660 */
typedef struct NEURON_ISA_TPB_TENSOR4D { ADDR4 start_addr; int16 step_elem[4]; uint16 num_elem[4]; }
```

The **union** `MEM_PATTERN4D` wraps it with an indirect-addressing arm:

```c
/* cayman common.h:741  ← present from CAYMAN, NOT introduced at MARIANA */
typedef union NEURON_ISA_TPB_MEM_PATTERN4D { NEURON_ISA_TPB_TENSOR4D t; NEURON_ISA_TPB_INDIRECT20B i; }
```

Both arms are 20B → the union is 20B → no struct ever changes size. What *does* change
per gen is which **field type** an operand uses:

| Operand | SUNDA | CAYMAN | MARIANA | MAVERICK |
|---------|-------|--------|---------|----------|
| `S4D4_TR` `src/dst_mem_pattern` | `TENSOR4D` | `TENSOR4D` | `MEM_PATTERN4D` | `MEM_PATTERN4D` |
| `S3D3_MM` / `S3_LW` mem-pattern | `TENSOR3D` | `TENSOR3D` | `MEM_PATTERN3D` | `MEM_PATTERN3D` |
| `S4D4_PL`, `S4D4_CR`            | `TENSOR4D` | `TENSOR4D` | `TENSOR4D` | `TENSOR4D` *(no indirect)* |

> **CORRECTION — the `MEM_PATTERN4D/3D/2D` union exists from CAYMAN, not MARIANA.**
> GEN-05 §3c states the union evolution happens "at MARIANA" (`mariana common.h:832`).
> The **union *type definitions* are already in CAYMAN** (`cayman common.h:741/745/749`,
> with the `INDIRECT20B/16B/12B` arms). What changes at MARIANA is that the *operand
> structs* (`S4D4_TR`, `S3D3_MM`, `S3_LW`) **switch their field type** from `TENSOR4D/3D`
> to `MEM_PATTERN4D/3D`. The union machinery predates its first operand use by one
> generation. `[CORRECTION — cayman common.h:741 defines MEM_PATTERN4D; cayman
> s4d4_tr.h:33 still uses TENSOR4D. The union-vs-field-type distinction is the precise
> claim.]`

> **NOTE — indirect addressing is selectively applied.** Even at MARIANA/MAVERICK the
> pool (`S4D4_PL`) and cross-lane-reduce (`S4D4_CR`) operands keep plain `TENSOR4D` (no
> indirect arm). Only the tensor-reduce + matmul/ldweight datapaths gained indirect
> addressing. `[HIGH/OBSERVED — S4D4_PL/CR md5-stable across gens.]`

---

## 3. The `NEURON_ISA_TPB_DTYPE` cross-gen diff (the dtype-model prerequisite)

`NEURON_ISA_TPB_DTYPE` is a 1-byte `NEURON_ISA_PACKED` enum (`common.h`,
`_Static_assert(sizeof == 1)`). It is a **strict additive superset chain**: ordinals
are reserved — a code never changes meaning, later gens only *add*. This is the
canonical input to [`../firmware/kernels/dtype-model.md`](../firmware/kernels/dtype-model.md);
the table below is fully consistent with it.

The enum bodies were extracted verbatim from each `common.h`:

| code | name | SUNDA | CAYMAN | MARIANA | MAVERICK | line (maverick) | note |
|------|------|:-----:|:------:|:-------:|:--------:|---|---|
| `0x00` | `INVALID`   | ● | ● | ● | ● | 850 | used in RTL for bitvec |
| `0x01` | `UINT64`    | ● | ● | ● | ● | 861 | |
| `0x02` | `INT8`      | ● | ● | ● | ● | 862 | |
| `0x03` | `UINT8`     | ● | ● | ● | ● | 858 | |
| `0x04` | `INT16`     | ● | ● | ● | ● | 863 | |
| `0x05` | `UINT16`    | ● | ● | ● | ● | 859 | |
| `0x06` | `BFLOAT16`  | ● | ● | ● | ● | 855 | |
| `0x07` | `FP16`      | ● | ● | ● | ● | 854 | |
| `0x08` | `INT32`     | ● | ● | ● | ● | 864 | |
| `0x09` | `UINT32`    | ● | ● | ● | ● | 860 | |
| `0x0A` | `FP32`      | ● | ● | ● | ● | 856 | |
| `0x0B` | `FP32R`     | ● | ● | ● | ● | 857 | RTL uses 0xB for FP22 partial |
| `0x0C` | `INT64`     | ● | ● | ● | ● | 865 | |
| `0x0D` | `FP8_EXP3`  | ● | ● | ● | ● | 851 | |
| `0x0E` | `FP8_EXP4`  | ● | ● | ● | ● | 852 | |
| `0x0F` | `FP8_EXP5`  | ● | ● | ● | ● | 853 | |
| `0x10` | `FP4_EXP2`  | – | – | ● | ● | 866 | OCP **FP4_E2M1** |
| `0x11` | `FP8_EXP2`  | – | – | – | ● | 874 | FP8_E2M5 |
| `0x12` | `INT4`      | – | – | – | ● | 875 | |
| `0x13` | `SFP8_E8`   | – | – | – | ● | 876 | scale-only, **FP8_S0E8M0** |
| `0x14` | `SFP8_E7`   | – | – | – | ● | 877 | scale-only, FP8_S0E7M1 |
| `0x15` | `SFP8_E6`   | – | – | – | ● | 878 | scale-only, FP8_S0E6M2 |
| `0x16` | `SFP8_E5`   | – | – | – | ● | 879 | scale-only, FP8_S0E5M3 |
| `0x19` | `CPTC1`     | – | – | ● | ● | 867 | Computed-Permutation Trellis Coding |
| `0x1A` | `CPTC2`     | – | – | ● | ● | 868 | `(d&0xF8)==0x18 && (d&0x7)!=0` ⇒ CPTC |
| `0x1B` | `CPTC3`     | – | – | ● | ● | 869 | |
| `0x1C` | `CPTC4`     | – | – | ● | ● | 870 | |
| `0x1D` | `CPTC5`     | – | – | ● | ● | 871 | |
| `0x1E` | `CPTC6`     | – | – | ● | ● | 872 | |
| `0x1F` | `CPTC7`     | – | – | ● | ● | 873 | `d & 0x7` = bit count |

**Enumerator counts (compiled-extracted, not grepped):** SUNDA **16**, CAYMAN **16**,
MARIANA **24**, MAVERICK **30**. SUNDA == CAYMAN (byte-identical 16-code base);
MARIANA = +`FP4_EXP2` + `CPTC1..7` (+8 → 24); MAVERICK = +`FP8_EXP2` + `INT4` +
`SFP8_E8..E5` (+6 → 30). `[HIGH/OBSERVED — enum bodies sed'd + Python-counted, all 4 gens.]`

> **QUIRK — MARIANA's additions are split across two ordinal bands, MAVERICK fills the
> gap.** MARIANA adds `0x10` *and* `0x19..0x1F` but leaves `0x11..0x18` empty. MAVERICK
> then fills `0x11..0x16` (FP8_EXP2/INT4/SFP8). So the enum is **not** densely packed by
> generation — the CPTC band (`0x19..0x1F`) is allocated before the FP8/INT4 band
> (`0x11..0x16`) is. A bit-test like `(d & 0xF8) == 0x18 && (d & 0x7) != 0` (the
> header's own CPTC check) is the safe way to classify, not a range compare.

### 3.1 The MARIANA companion types (alongside FP4/CPTC)

* `typedef enum NEURON_ISA_TPB_DTYPE_BASIC {…}` — a parallel `BASIC_*` alias family of
  the 16 base codes, **mariana + maverick only** (`rg -c` = 1 each; 0 in sunda/cayman).
* `typedef struct NEURON_ISA_TPB_MXTENSOR1D {…}` + a wrapping union — the Microscaled
  Tensor descriptor (data + scale), **mariana + maverick only** (`rg -c` = 3 refs each;
  0 in sunda/cayman). MAVERICK additionally carries the `ADDR4MARKER_MXTENSOR_V2 = 0x01`
  marker (the "Microscaled Tensor v2" addressing). `[HIGH/OBSERVED.]`

### 3.2 Other enums (cross-gen)

| enum | SUNDA | CAYMAN | MARIANA | MAVERICK | location |
|------|:-----:|:------:|:-------:|:--------:|----------|
| `ALU_OP`         | **33** | **60** | **64** | **65** | `common.h` |
| `OPCODE`         | **145** | **150** | **159** | **165** | `common.h` |
| `REDUCE_OP`      | 6 | 6 | 6 | 6 | `s4d4_cr.h:62` (ADD/AVERAGE/MAX/OR/AND/XOR) |
| `POOL_TYPE`      | 3 | 3 | 3 | 3 | `s4d4_pl.h:277` (NONE/MAX_POOL/AVG_POOL) |
| `COLLECTIVE_TYPE`| 10 | 10 | 10 | 10 | `common.h:792` |
| `CCE_OP`         | 3 | 3 | 3 | 3 | `common.h` (ADD/MAX/MIN) |
| `TENSOR_SUBDIM`  | 5 | 5 | 5 | 5 | `common.h` (UNUSED/X/XY/XYZ/XYZW) |

`ALU_OP` is the big grower: SUNDA→CAYMAN adds the entire INT/UINT variant family (+27);
MARIANA adds `ABS_MAX/ABS_MIN/RE_LU/SQUARE` (+4); MAVERICK adds `SYMMETRIC_CLAMP` (+1).
No removals. `[HIGH/OBSERVED — Python enum-body counter, all 4 gens.]`

> **NOTE — `cc_op` / `enc_alg_type` / `enc_op_type` are NOT in the ISA headers.** Those
> are host-side enums (encoder vocabulary + firmware spad command nibble), in the
> `libnrt`/`libnrtucode` binaries, not the arch-isa C headers. A header `rg` for
> `CC_OP`/`ENC_ALG` = 0 hits. The header carries only the *on-instruction* collective
> enums (`COLLECTIVE_TYPE`, `CCE_OP`, …). `[HIGH/OBSERVED — header grep = 0.]`

---

## 4. Opcode → struct binding per gen (`instruction_mapping.json`)

Each modern tree ships `tpb/instruction_mapping.json` with two maps —
`struct2opcode` (struct → opcodes it decodes) and `struct2pseudo_opcode` (2 entries,
all gens). The JSON mirrors the operand-header set exactly, so its key deltas track the
header add/removes of §1.2.

**`struct2opcode` key counts** (`jq '.struct2opcode | keys | length'`):
SUNDA **89**, CAYMAN **99**, MARIANA **108**, MAVERICK **114**.
`[HIGH/OBSERVED — jq '.struct2opcode | keys | length'.]`

**Core operand bindings** (cayman JSON; stable across gens that carry the struct):

| struct | bound opcodes | count |
|--------|---------------|:-----:|
| `S4D4_TR_STRUCT` | `TENSOR_REDUCE_ARITH/BITVEC`, `TRANSPOSE_TENSOR_REDUCE_ARITH/BITVEC`, `TENSOR_CUMULATIVE_ARITH/BITVEC`, `COPY`, `CAST`, `RECIPROCAL`, `STREAM_SHUFFLE`, `STREAM_TRANSPOSE` | **11** |
| `S4D4_CR_STRUCT` | `CROSS_LANE_REDUCE_ARITH`, `CROSS_LANE_REDUCE_BITVEC` | 2 |
| `S4D4_PL_STRUCT` | `POOL`, `MAX_POOL_SELECT` | 2 |

**The matmul/ldweight family binding** (maverick JSON, full prefix `NEURON_ISA_TPB_…`):

| struct | opcode | first gen |
|--------|--------|-----------|
| `S3D3_MM_STRUCT`        | `OPCODE_MATMUL`            | SUNDA |
| `S3_LW_STRUCT`          | `OPCODE_LDWEIGHTS`         | SUNDA |
| `S4D3_MM_STRUCT`        | `OPCODE_MATMUL_SPARSE`     | CAYMAN |
| `SMX1D3_MM_STRUCT`      | `OPCODE_MATMUL_MX`         | MARIANA |
| `SMX1_LW_STRUCT`        | `OPCODE_LDWEIGHTS_MX`      | MARIANA |
| `S3DMX1_QUANT_STRUCT`   | `OPCODE_QUANTIZE_MX`       | MARIANA |
| `S3D3_TENS_DEQUANT_STRUCT` | `OPCODE_TENSOR_DEQUANTIZE` | CAYMAN |

`[HIGH/OBSERVED — jq '.struct2opcode' on each gen's JSON.]`

**The only `struct2opcode` removals in the whole chain** are
`CUSTOM_OP_HEADER_STRUCT` and `CUSTOM_OP_PAYLOAD_STRUCT` (present in SUNDA's JSON,
absent in CAYMAN+). This is **2** struct2opcode keys — vs the **3** header files removed
(`nx_map.h` is the third file but binds no opcode). See the §1.2 CORRECTION.
`[HIGH/OBSERVED — jq keys diff sunda↔cayman.]`

> **NOTE — `instruction_mapping.json` appears generated from the headers.** The
> key↔header-file correspondence is exact (every struct2opcode key has a matching
> operand `.h`; every operand `.h` addition shows up as a key). No generator script is
> shipped, so "JSON is generated from headers" is **MED/INFERRED** — but it is a safe
> ground-truth for opcode→struct dispatch.

---

## 5. The legacy TONGA → modern S3D3/S4D4 struct transition

TONGA is the **V1 predecessor** — `Copyright 2018`, banner `TONGA ISA / TPB /
COMMON-FUNCTIONS`, no `NC-v#` line, carried only in `arch-isa/` (40 `tpb` headers).
Every modern `common.h` carries the in-band note "V1 ISA is maintained in separate
package" on its `NEURON_CORE_VERSION_V2` enumerator. TONGA has **zero runtime identity**
(no coretype, no arch_id) — it is purely the historical ISA-header ancestor.

It is a **naming + organization rewrite**, not a width change. Both `TONGA_ISA_TPB_…`
operands compile to 64B.

| axis | TONGA (V1, legacy "L") | MODERN (SUNDA..MAVERICK, V2..V5) |
|------|-------------------------|----------------------------------|
| struct naming | per-**mnemonic** `*_INST` (`TONGA_ISA_TPB_MATMUL_INST`, `…_LDWEIGHTS_INST`) | shape-**coded** `S<n>D<m>_*_STRUCT` (`S3D3_MM_STRUCT`, `S3_LW_STRUCT`, `S4D4_TR/PL/CR_STRUCT`) |
| header layout | 1 file per mnemonic (`_matmul.h`, `_ldweights.h`, …; 40 tpb files) | spine (`common`+`enums`+`assert`+`extended`) + per-shape operand headers |
| type prefix | `TONGA_ISA_TPB_*` | `NEURON_ISA_TPB_*` |
| header sub-struct | `INST_HEADER` (4B) + `INST_EVENTS` (**4B**) | `HEADER` (4B) + `EVENTS` (**8B**) |
| `EVENTS` delta | `wait_event_mode/idx` + `set_event_mode/idx` (4 × 1B) | + `uint32 semaphore_value` (the entire +4B) |
| mem-pattern type | `TONGA_ISA_TPB_MEM_ACCESS_3D` (per-mnemonic, in-struct) | `TENSOR3D` → `MEM_PATTERN3D` union |
| dtype enum | `TONGA_ISA_TPB_DTYPE` (**8 codes**) | `NEURON_ISA_TPB_DTYPE` (16 → 30) |
| instruction width | 64B (`TONGA_ISA_TPB_INST_NBYTES`) | 64B (`NEURON_ISA_TPB_INST_NBYTES`) |
| copyright | 2018 | 2019+ |

**The TONGA DTYPE 8-code subset** (`arch-isa/tpb/aws_tonga_isa_tpb_common.h:52-60`):
`INVALID=0x0`, `UINT8=0x3`, `UINT16=0x5`, `BFLOAT16=0x6`, `FP16=0x7`, `INT32=0x8`,
`FP32=0xA`, `INT64=0xC`. It is a **strict subset** of the modern 16-code base, under a
**distinct name family** (`TONGA_ISA_TPB_DTYPE_*`, not `NEURON_ISA_TPB_DTYPE_*`). The
modern codes TONGA lacks (`0x1,0x2,0x4,0x9,0xB,0xD,0xE,0xF` = `UINT64/INT8/INT16/UINT32/
FP32R/FP8_E3/E4/E5`) are exactly the SUNDA/CAYMAN-base additions.

> **GOTCHA — TONGA `MATMUL_INST` carries per-mnemonic fields the modern struct dropped.**
> The TONGA matmul body (`aws_tonga_isa_tpb_matmul.h:96`) has `ifmap_replication_resolution`,
> `ifmap_replication_shift_amnt`, `ifmap_replication_num_rows`, a `quant_offset` union,
> and `MEM_ACCESS_3D src/dst_mem_pattern` — a CNN-replication-specific layout. The modern
> `S3D3_MM` replaced this with the generic `TENSOR3D`/`MEM_PATTERN3D` mem-pattern + the
> `transpose_mode`/`flags`/`tile_*` evolution of §2.4. The V1→V2 mnemonic→shape rename is
> **not** a field-preserving rename: the operand semantics were re-modeled.
> `[HIGH/OBSERVED — TONGA matmul body vs sunda S3D3_MM body.]`

So the V1→V2 step was: per-mnemonic `*_INST` → shape-coded `*_STRUCT`; `TONGA_` →
`NEURON_`; `INST_EVENTS` 4B → `EVENTS` 8B (the `uint32 semaphore_value`); `MEM_ACCESS_3D`
→ `TENSOR3D`. The 64B word and the reserved dtype/opcode ordinals were kept. TONGA is
the legacy predecessor **outside** the unified v2..v5 reimplementation envelope — its
bridge to SUNDA is scoped to the V1→V2 lineage work, not this header diff.

---

## 6. Reproduction (commands)

```sh
INC=…/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/include

# availability grid (basenames, then comm):
for g in sunda cayman mariana maverick; do
  fd --no-ignore -e h . "$INC/neuron_${g}_arch_isa/tpb" -x basename {} | sort > /tmp/$g.lst; done
comm -13 /tmp/sunda.lst /tmp/cayman.lst    # +12 ; comm -23 → 3 removed

# compiled sizeofs (per gen):
gcc -std=c11 -I "$INC/neuron_<g>_arch_isa/tpb" probe_core.c -o p && ./p   # all 64
gcc -std=c11 -I "$INC/neuron_<g>_arch_isa/tpb" probe_ext.c  -o p && ./p   # extended = 64
gcc -std=c11 -I "$INC/neuron_{mariana,maverick}_arch_isa/tpb" probe_mx.c  # MX = 64 (fails on cayman)
gcc -std=c11 -I "$INC/arch-isa/tpb" -I "$INC/arch-isa/common" -I "$INC" probe_tonga.c  # MATMUL/LDW=64, INST_EVENTS=4

# enums: enumerator bodies sed'd from common.h; counted in Python.
# opcode→struct: jq '.struct2opcode | keys | length' "$INC/neuron_<g>_arch_isa/tpb/instruction_mapping.json"
```

`gcc (GCC) 16.1.1`, `-std=c11`. All `_Static_assert`s pass; all `sizeof`/`offsetof`
values above are printed by the compiled probe, not hand-computed.

---

## 7. Confidence ledger

**HIGH × OBSERVED** (header-exact + compile-verified): the header-availability grid
(99/108/117/123 tpb; +12/−3, +9/−0, +6/−0); all core operand `sizeof == 64` across all 5
gens incl. TONGA; the `S4D4_TR` offset table (in_dtype@32/out_dtype@33 all gens); the
`S3D3_MM`/`S3_LW` field reworks; the `TENSOR4D`→`MEM_PATTERN4D` field-type switch at
MARIANA (union type itself from CAYMAN); the full DTYPE ordinal table + counts
(16/16/24/30); `ALU_OP` 33/60/64/65; `OPCODE` 145/150/159/165; the stable
`REDUCE/POOL/COLLECTIVE/CCE/SUBDIM` enums; `struct2opcode` counts 89/99/108/114 + the
matmul-family bindings; the legacy `INST_EVENTS`(4) → `EVENTS`(8) transition; TONGA's
8-code DTYPE subset under the distinct `TONGA_ISA_TPB_DTYPE_*` family; MARIANA_PLUS has a
register-map dir but no ISA dir.

**MED × INFERRED:** "indirect addressing is enabled by the `MEM_PATTERN` union" (the
union arms are OBSERVED; runtime enablement is inferred from naming);
"`instruction_mapping.json` is generated from the headers" (exact key↔file correspondence,
no generator seen).

**INFERRED / out-of-scope:** MAVERICK `arch_id 0x24` (no shipped v5 NCFW — runtime
envelope only; the v5 *header* layout is OBSERVED); the exact MX `tile_size`/`tile_sel`/
`perf_mode` field semantics (a dedicated MX task can read the `s3dmx1_quant.h`/`smx1*.h`
bodies); MARIANA_PLUS runtime ISA == MARIANA byte-for-byte at the `.so` level (asserted
elsewhere; out of header scope).

**Corrections folded in:** (1) SUNDA→CAYMAN removes **3** header files, not 2 (§1.2);
(2) the `MEM_PATTERN4D/3D/2D` union exists from **CAYMAN**, not MARIANA — only the
operand field-type switches at MARIANA (§2.5); (3) SUNDA tpb header count is **99**, not
"~62" (§1.1).
