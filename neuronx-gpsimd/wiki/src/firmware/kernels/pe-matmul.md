# PE Matrix-Multiply Path (LdWeight / Matmul / PeManageSeed)

This page decodes the **PE engine** (`engine_idx = 0`) of the NeuronCore — the systolic
matrix-multiply (TensorE) accelerator and the firmware micro-op orchestrator that drives it.
The GPSIMD complex runs on a **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW DSP**
(`ncore2gp` config, one per NeuronCore); the PE engine is the same `cayman/seq/` SEQ-dispatch
chassis as the ACT/POOL/DVE/SP sequencers, recompiled with the systolic-matmul handler subset.
Its compute micro-ops are **SEQ handlers** — `Ldweights` / `Matmul` / `MatmulSparse` / `LdTags` /
`PeRegWrite` / `PeManageSeed` / `LdweightsMX` / `MatmulMX` / `ConvLutLoad` — each self-naming via
a `S:` DEBUG string and each carrying a 64-byte operand struct. This page pins the **full
micro-op opcode set** byte-exact, every **operand struct** with compile-verified offsets and
per-gen layout, **what `PeManageSeed` actually manages** (the PSUM stochastic-rounding seed
state — *not* a PE-array per-cell PRNG), the **weight-load → multiply-accumulate → PSUM-drain
datapath** reconciled with the [integer-MAC ISA](../../isa/ref/b04-mac-integer.md), the
**PE↔SEQ `arr_seq` CSR handshake**, and the per-gen evolution **CAYMAN → MARIANA →
MARIANA_PLUS → MAVERICK** including the MX (microscaling) matmul path.

The HW-vs-FW boundary is explicit: the **systolic array, the per-cell multiply+adder, the PSUM
accumulator banks, and the stochastic-rounding RNG are silicon** (no netlist ships). The PE
firmware is the **micro-op orchestrator** that decodes the instruction stream, programs the
array (tile-select, dtype, weight-load), issues the matmul, and manages the seed save/restore.
The on-NX-core IVP widening-MAC family the engine also carries is the GPSIMD-side
int-quantized / assist datapath (§6).

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every host-ISA fact is read out of the `aws_neuron_isa_tpb_*.h`
headers shipped in the customop-lib package and was re-compile-verified this session; every
device fact is byte-pinned to a carve from `libnrtucode_internal.so`, decoded with the shipped
`ncore2gp` `xtensa-elf-objdump`; every CSR fact is read from the shipped `arr_seq` register JSON.

> **NOTE — the objects used this session.** The firmware container is
> `…/custom_op/c10/lib/libnrtucode_internal.so` (ELF64 x86-64 DYN, `.rodata` identity-mapped:
> VMA == file offset). The PE firmware images are getter-blob `.rodata` carves; IRAM file
> offset == device IRAM VA (reset vector at byte 0), DRAM string-file offset == device DRAM
> VA − `0x80000`. The arch-isa C structs (`s3_lw.h`, `s3d3_mm.h`, `s4d3_mm.h`,
> `s2s1d2_pe_seed.h`, `smx1_lw.h`, `smx1d3_mm.h`, `s3_lt.h`, `common.h`) ship in the
> customop-lib package and are themselves binary evidence; all six PE operand structs
> compile-verify `sizeof == 64` (host `gcc -std=c11`, this session). The `arr_seq`
> handshake CSRs are the RTL-generated `tpb_arr_seq_{top,cluster}_host_visible.json`. The
> `S:`/handler-name strings are ASCII baked into the shipped DEBUG firmware, used purely as
> name anchors.
>
> | object | file off / size | note |
> |---|---|---|
> | `MARIANA PE DEBUG IRAM` | `0x42c520` / `0x18c20` | dispatch compare-chain `+0x2934`; handler stubs `0x2b8x–0x2bc7` |
> | `MARIANA PE PERF IRAM` | `0x335ca0` / `0x12ce0` | the IVP widening-MAC census (PERF strips `S:`) |
> | `CAYMAN PE DEBUG DRAM` | (`0x1ab000`-class) | `S: BEGIN on cayman`; **0** `PeManageSeed`/`MatmulMX` hits |
> | `MARIANA PE DEBUG DRAM` | (`0x445000`-class) | `S: PeManageSeed(SAVE/LOAD)`@`0x4459b1`/`0x4459c8`; micro-op strings @`0x447520`/`0x447547` |
> | `MARIANA_PLUS PE DEBUG DRAM` | (`0x70e000`-class) | same set + `S: BEGIN on mariana_plus` |
>
> `xtensa-elf-objdump --xtensa-core=ncore2gp` decodes the IRAM to real Q7/NX windowed-ABI +
> FLIX-VLIW (exit 0; the MAC census and dispatch chain reproduced this session). `[HIGH/OBSERVED]`

---

## 1. The headline

The PE matmul path is a **SEQ-engine instruction set, not a Q7 EXTISA kernel**. The Q7
`kernel_info_table` carries the POOL/Vector compute back-end (Pool, CrossLaneReduce,
TensorTensor, Dequant) — it has **no** matmul/ldweight opcode. The PE compute micro-ops are
SEQ handlers in the PE flat IRAM/DRAM image. Eight facts pin the engine:

1. **The PE micro-op block is `0x01..0x0A` + `0xe4`** — read verbatim from the
   `NEURON_ISA_TPB_OPCODE` enum (`common.h:158-167,310`) and independently byte-decoded from
   the firmware dispatch compare-chain (§2). `[HIGH/OBSERVED]`
2. **`Ldweights` (0x01) loads the stationary weight tile** from SBUF into the systolic array
   (the `x`/stationary operand of `nc_matmul`); operand `S3_LW_STRUCT`. `[HIGH/OBSERVED]`
3. **`Matmul` (0x02) streams the moving activation tile**, multiply-accumulating
   `dst = stationary.T @ moving` down the columns into the **FP32 PSUM banks**; operand
   `S3D3_MM_STRUCT`. `[HIGH/OBSERVED]`
4. **`MatmulSparse` (0x07) is the fine-grain-sparse matmul** with a distinct **4-D** source
   struct `S4D3_MM_STRUCT` (the 4th dim = the 2/3/4 fmaps per partition); fed by `LdTags`
   (0x06, `S3_LT`) and `Ldweights` `tag_weight_mode` — the consumer of the
   [SparsityCompress producer](./sparsity-compress-tag.md). `[HIGH/OBSERVED]`
5. **`PeManageSeed` (0x08) manages the PSUM fp32→bf16 stochastic-rounding RNG seeds** — *not*
   a PE-array per-cell PRNG; its own struct `S2S1D2_PE_SEED_STRUCT`; v4+ only. `[HIGH/OBSERVED]`
6. **The MX (microscaled / FP4) matmul has two mechanisms**: v4 uses **separate ops**
   `LdweightsMX` (0x09, `SMX1_LW`) + `MatmulMX` (0x0A, `SMX1D3_MM`); v5 **folds MX into
   `Ldweights`/`Matmul`** via the `MEM_PATTERN3D` `mx` union member. `[HIGH/OBSERVED]`
7. **Internal accumulation is FP32 always**; `dst` dtype is FP32 (all gens) or BF16 (v4+).
   The inner MAC is the IVP widening family (`ivp_mul4t*`/`mulpa*`/`dmulq*`/`packvr*`,
   XR-broadcast weight) — re-read from the PE PERF IRAM this session. `[HIGH/OBSERVED]`
8. **The feature set grows per gen** — CAYMAN `{Ldweights, Matmul, MatmulSparse, LdTags,
   PeRegWrite}`; MARIANA/MARIANA_PLUS add `PeManageSeed + LdweightsMX + MatmulMX +
   ConvLutLoad`; MAVERICK re-packs the structs for the 256-column array and unifies MX
   (§7). `[HIGH/OBSERVED]`

> **CORRECTION — `S4D3_MM`, not `S3D3_MM`, is the `MatmulSparse` struct.** Plain **`Matmul`
> (0x02) uses `S3D3_MM_STRUCT`** (one 3-D source); **`MatmulSparse` (0x07) uses
> `S4D3_MM_STRUCT`** (one **4-D** source — the extra dim carries the 2/3/4 sparse fmaps per
> partition, one 3-D dst). The `struct2opcode` map binds `S3D3_MM_STRUCT → MATMUL` and
> `S4D3_MM_STRUCT → MATMUL_SPARSE` (`jq`-verified). A reimplementer must use the 4-D source
> struct for the sparse path. `[HIGH/OBSERVED]`

---

## 2. The micro-op roster — byte-exact opcodes

The PE compute opcodes come from the `NEURON_ISA_TPB_OPCODE` enum (`common.h`), triple-confirmed
against (i) the enum, (ii) the `struct2opcode` map in `instruction_mapping.json` (`jq`, this
session), and (iii) the firmware DEBUG handler self-names + byte-decoded dispatch chain. The
`// Y` tag is the header's own "tested/maintained" annotation. `[HIGH/OBSERVED]`

| opcode | enum (`…_OPCODE_…`) | line | operand struct | NKI name | role |
|---:|---|---:|---|---|---|
| `0x01` | `LDWEIGHTS` | 158 | `S3_LW_STRUCT` | `LoadStationary` | load stationary weight tile (`x` of `nc_matmul`) |
| `0x02` | `MATMUL` | 159 | `S3D3_MM_STRUCT` | `MultiplyMoving` | stream moving acts, MAC → PSUM |
| `0x03` | `PE_REG_WRITE` | 160 | (PE config-reg write) | — | write a PE configuration register |
| `0x04` | `WEIGHT_MASK` | 161 | (tonga, deprecated) | — | structured-sparsity weight mask |
| `0x05` | `WEIGHT_SHIFT` | 162 | (tonga, deprecated) | — | structured-sparsity weight shift |
| `0x06` | `LDTAGS` | 163 | `S3_LT_STRUCT` | — | load sparsity tags for `MatmulSparse` |
| `0x07` | `MATMUL_SPARSE` | 164 | `S4D3_MM_STRUCT` | — | structured-sparse matmul (4-D src) |
| `0x08` | `PE_MANAGE_SEED` | 165 | `S2S1D2_PE_SEED_STRUCT` | — | PSUM SR-RNG seed save/restore `[v4+]` |
| `0x09` | `LDWEIGHTS_MX` | 166 | `SMX1_LW_STRUCT` | — | MX weight-load (separate v4 op) |
| `0x0A` | `MATMUL_MX` | 167 | `SMX1D3_MM_STRUCT` | — | MX matmul (separate v4 op) |
| `0xe4` | `CONV_LUT_LOAD` | 310 | `S2_CONVLUT` | — | 4-bit input-converter LUT load `[v4+]` |

`struct2opcode` bindings (`jq`, this session): `S3_LW_STRUCT → LDWEIGHTS`,
`S3D3_MM_STRUCT → MATMUL`, `S4D3_MM_STRUCT → MATMUL_SPARSE`,
`S2S1D2_PE_SEED_STRUCT → PE_MANAGE_SEED`, `SMX1_LW_STRUCT → LDWEIGHTS_MX`,
`SMX1D3_MM_STRUCT → MATMUL_MX`, `S3_LT_STRUCT → LDTAGS`. In the **CAYMAN** and **SUNDA** maps the
v4+ structs (`S2S1D2_PE_SEED`, `SMX1_LW`, `SMX1D3_MM`) return `null` — they do not exist pre-v4.
`[HIGH/OBSERVED]`

### 2.1 The dispatch — byte-decoded raw-opcode compare-chain

The MARIANA PE DEBUG IRAM dispatch site (carved file `0x42c520`, IRAM `+0x2934`, decoded
`ncore2gp`, exit 0) is a **raw-opcode segmented compare-chain** — the PE compares the raw opcode
byte (no `addi`-normalisation), each opcode routing to a distinct stub in the `0x2b8x..0x2bc7`
cluster. Reproduced byte-for-byte this session (the `66 0X 02` encoding is the `BNEI a2,X`):

```text
2934:  l32i.n a2,[a1+16]            ; a2 = opcode word
2936:  661202  bnei a2, 1, 0x293c  ; j 0x2b86   ; 0x01 Ldweights    -> stub 0x2b86
293f:  662202  bnei a2, 2, 0x2945  ; j 0x2b8e   ; 0x02 Matmul       -> 0x2b8e
2948:  663202  bnei a2, 3, 0x294e  ; j 0x2ba6   ; 0x03 PeRegWrite   -> 0x2ba6
2951:  666202  bnei a2, 6, 0x2957  ; j 0x2b96   ; 0x06 LdTags       -> 0x2b96
295a:  667202  bnei a2, 7, 0x2960  ; j 0x2b9e   ; 0x07 MatmulSparse -> 0x2b9e
2963:  668202  bnei a2, 8, 0x2969  ; j 0x2bc7   ; 0x08 PeManageSeed -> 0x2bc7
296c:  movi.n a3,9 ; bne a2,a3,…   ; j 0x2baf   ; 0x09 LdweightsMX  -> 0x2baf
2977:  669202  bnei a2,10, 0x297d  ; j 0x2bb7   ; 0x0a MatmulMX     -> 0x2bb7
…      (0x9f.. SEQ-core control opcodes follow)
2a70:  movi a3,228 (=0xe4) …       ; 0xe4 ConvLutLoad
```

So the **+4 MARIANA opcodes over CAYMAN** are byte-confirmed to be exactly
`{0x08 PeManageSeed, 0x09 LdweightsMX, 0x0A MatmulMX, 0xe4 ConvLutLoad}`, matching the ISA enum
and the DEBUG handler names — triple agreement. The dispatch **chain** decodes clean; the
handler **bodies** the stubs jump into FLIX-bundle and do not linearly decode (the WALL below).
The PE compute handlers are **SEQ-engine handlers**, *not* entries in the POOL Q7 EXTISA
`kernel_info_table`. `[HIGH/OBSERVED chain; MED interiors]`

The handler self-name strings are byte-exact in the MARIANA/MARIANA_PLUS PE DEBUG DRAM (this
session): `S: Ldweights`, `S: Matmul`, `S: PeRegWrite`, `S: LdTags`, `S: MatmulSparse`,
`S: PeManageSeed(SAVE)`/`(LOAD)`, `S: LdweightsMX`, `XS: MatmulMX` (note the `XS:` prefix),
`S: ConvLutLoad`, with the gen anchors `S: BEGIN on {cayman,mariana,mariana_plus,maverick}`.
The CAYMAN PE DEBUG DRAM carries **zero** `PeManageSeed`/`MatmulMX` hits. `[HIGH/OBSERVED]`

> **WALL — the FLIX handler-body tail stays MED.** The PE handlers are hand-scheduled FLIX/VLIW
> with interleaved literal/selector spans; the stock `ncore2gp` linear sweep loses bundle sync
> across those spans, so the **handler interiors** (the `const16`-pairs that load the `S:` DRAM
> string offsets, the mid-body micro-op issue) do not linearly decode. What **decodes clean**
> and is `[HIGH]`: the reset/boot vectors, the dispatch compare-chain (above), the IVP-MAC
> census (§6), and **all** DEBUG self-naming strings. The opcode/struct/enum facts are
> **header-compile-verified** (not disasm-dependent). The exact in-handler body schedule is
> `[MED]` — flagged, not over-claimed.

---

## 3. `Ldweights` (0x01) — load the stationary weight tile

`Ldweights` (NKI `LoadStationary`) reads a **stationary** weight tile from SBUF
(`AllowedInSBUF::True` / `AllowedInPSUM::False`) and loads it into the systolic array. In the
GPSIMD/IVP MAC datapath this is the `*XR8`/`*XR16` **XR reduce-register weight broadcast** — one
factor (the weight) is read from the dedicated reduce register the matmul broadcasts across a
column ([B04 §3](../../isa/ref/b04-mac-integer.md)); the other factor (the activation) comes from
the moving vector lane.

The **MAVERICK** `S3_LW_STRUCT` (compile-verified `sizeof == 64`, this session):

```c
typedef struct NEURON_ISA_TPB_S3_LW_STRUCT {       // ISA header for NC-v5
    NEURON_ISA_TPB_HEADER          header;          // 0    (opcode=0x01 LDWEIGHTS)
    NEURON_ISA_TPB_EVENTS          events;          // 4
    uint8_t                        reserved0[4];    // 12
    NEURON_ISA_TPB_MEM_PATTERN3D   src_mem_pattern; // 16   stationary tile (SBUF / MX union)
    NEURON_ISA_TPB_DTYPE           in_dtype;        // 32   weight dtype
    NEURON_ISA_TPB_PE_FP32MODE     fp32_mode;       // 33   None/Low/High/Low_High
    uint8_t                        num_active_rows; // 34   PE-array active rows
    uint8_t                        num_active_cols; // 35   active cols (0 => 256, full c256)
    NEURON_ISA_TPB_TILE_SIZE       tile_size;       // 36   {row r64/r128, col c128/c256}
    NEURON_ISA_TPB_TILE_SEL        tile_sel;        // 37   {row_sel:4, col_sel:4}
    NEURON_ISA_TPB_LD_WEIGHT_FLAGS flags;           // 38   {xpose, scalar, seed:2, load_order}
    NEURON_ISA_TPB_MX_PERF_MODE    perf_mode;       // 39   MX pump (QUAD_ROW/OCT_ROW)
    uint8_t                        reserved1[24];   // 40 - 63
} NEURON_ISA_TPB_S3_LW_STRUCT;                       // sizeof == 64
```

> **NOTE — `S3_LW` packs one byte tighter than `S3D3_MM`.** Because `Ldweights` has **no**
> `out_dtype` field (it produces no PSUM output), its tail is shifted one byte earlier than
> `Matmul`'s: on MAVERICK, `S3_LW.flags`@**38** / `perf_mode`@**39** vs `S3D3_MM.flags`@**39** /
> `perf_mode`@**40**. A reimplementer must **not** reuse one struct's offset table for the other
> — both are 64 B but the `tile_size`/`tile_sel`/`flags`/`perf_mode` tail sits at different
> offsets. `[HIGH/OBSERVED — both compile-verified this session]`

### 3.1 `LD_WEIGHT_FLAGS` and `PE_LW_ORDER`

`NEURON_ISA_TPB_LD_WEIGHT_FLAGS` (`common.h:1388-1394`) is a packed byte
(LSB→MSB): `xpose_mode:1`, `scalar_mode:2`, `seed_mode:2`, `load_order:1`, `reserved_bitfield:2`.
The `load_order` field is `NEURON_ISA_TPB_PE_LW_ORDER` (`common.h:1301-1302`):

| value | enum | meaning |
|---:|---|---|
| `0` | `LAST_COL_FIRST` | **default** (backward-compatible) — the first data read goes to the **last** column (weights are loaded in reversed column order) |
| `1` | `FIRST_COL_FIRST` | forward — the first data read goes to the **first** column |

The reversed default load order is the same reversal `LdTags` and the sparse weight format use
([sparsity-compress §6b](./sparsity-compress-tag.md)). `[HIGH/OBSERVED]`

### 3.2 Per-gen `S3_LW` layout

The **CAYMAN** (v3) `S3_LW` carries a `TENSOR3D` source (not the MX-capable union),
`transpose_mode`@34 + `perf_opt`@35 (separate fields, no `flags` byte), `num_active_rows`@38 /
`num_active_cols`@39, `row_grp`@44 / `col_grp`@45 (the array tiling) — and **no `flags`/`seed_mode`
byte** (so no `PeManageSeed`). The **MARIANA** (v4) `S3_LW` adds a `bg_xpose_mode`@12
(background-transpose mode) + a `MEM_PATTERN3D` source + the `LD_WEIGHT_FLAGS` `flags`@**47** (with
`seed_mode`), keeping `row_grp`@44 / `col_grp`@45. **MAVERICK** (v5) re-packs the whole tail for
`tile_size`/`tile_sel`/`flags`@38/`perf_mode` and drops `row_grp`/`col_grp` (§11). All gens
`sizeof == 64`. `[HIGH/OBSERVED — three gens compile-verified this session]`

---

## 4. `Matmul` (0x02) — stream the moving acts, MAC into PSUM

`Matmul` (NKI `MultiplyMoving`) streams the **moving** activation tile through the loaded array;
each PE cell multiply-accumulates; partial sums accumulate down the columns into PSUM, computing
`dst = stationary.T @ moving`. **Internal accumulation = FP32** (the PSUM banks); the `dst` dtype
is FP32 (all gens) or BF16 (v4+). The MAVERICK `S3D3_MM_STRUCT` (compile-verified, this session):

```c
typedef struct NEURON_ISA_TPB_S3D3_MM_STRUCT {     // ISA header for NC-v5
    NEURON_ISA_TPB_HEADER        header;            // 0    (opcode=0x02 MATMUL)
    NEURON_ISA_TPB_EVENTS        events;            // 4
    uint8_t                      reserved0[4];      // 12
    NEURON_ISA_TPB_MEM_PATTERN3D src_mem_pattern;   // 16   moving acts (SBUF / MX union)
    NEURON_ISA_TPB_DTYPE         in_dtype;          // 32
    NEURON_ISA_TPB_PE_FP32MODE   fp32_mode;         // 33   None/Low/High/Low_High
    NEURON_ISA_TPB_DTYPE         out_dtype;         // 34   FP32 (all) | BF16 (v4+) | 0 (=FP32)
    uint8_t                      num_active_rows;   // 35
    uint8_t                      num_active_cols;   // 36
    NEURON_ISA_TPB_TILE_SIZE     tile_size;         // 37   {r64/r128, c128/c256}
    NEURON_ISA_TPB_TILE_SEL      tile_sel;          // 38
    NEURON_ISA_TPB_MATMUL_FLAGS  flags;             // 39   {scalar, seed:2, accumulate:1, xpose}
    NEURON_ISA_TPB_MX_PERF_MODE  perf_mode;         // 40   QUAD_ROW / OCT_ROW (MX pump)
    uint8_t                      reserved1[7];      // 41 - 47
    NEURON_ISA_TPB_MEM_PATTERN3D dst_mem_pattern;   // 48   PSUM
} NEURON_ISA_TPB_S3D3_MM_STRUCT;                     // sizeof == 64
```

The **MARIANA** (v4) `S3D3_MM` instead carries `out_dtype`@40, a `MATMUL_FLAGS` `flags`@**42**, a
**separate** `psum_accumulate_flags`@43 byte, `row_grp`@44 / `col_grp`@45, and
`psum_zero_region`@47 — i.e. v4 has both `flags.accumulate_flag` and the legacy
`psum_accumulate_flags` byte. The **CAYMAN** (v3) `S3D3_MM` has the same `psum_accumulate_flags`@43
/ `row_grp`@44 / `col_grp`@45 / `psum_zero_region`@47 tail but **no `flags` byte at all** (no
`seed_mode`). MAVERICK drops the separate `psum_accumulate_flags`/`row_grp`/`col_grp`/
`psum_zero_region` in favour of `tile_size`/`tile_sel`/`flags.accumulate_flag`/`perf_mode`. All
gens `sizeof == 64`. `[HIGH/OBSERVED — three gens compile-verified]`

### 4.1 `MATMUL_FLAGS` and the accumulate mode

`NEURON_ISA_TPB_MATMUL_FLAGS` (`common.h:1380-1386`) is a packed byte (LSB→MSB):
`scalar_mode:2`, `seed_mode:2`, `accumulate_flag:1`, `xpose_mode:1`, `reserved_bitfield:2`.
The `accumulate_flag` is `NEURON_ISA_TPB_ACCUMULATE_MODE` = `OVERWRITE = 0` / `ACCUMULATE = 1`
(`common.h:1375-1378`). The PSUM accumulate **group** is the
`NEURON_ISA_TPB_MATMUL_PSUM_ACCUMULATE_FLAGS` bit set plus the derived `static const` modes
(`common.h:1335-1349`):

```c
// the bit flags:
BEGIN_MATMUL_GROUP       = (1 << 0);   // 0x1
END_MATMUL_GROUP         = (1 << 1);   // 0x2
BEGIN_ACCUM_MATMUL_GROUP = (1 << 2);   // 0x4
// the derived group modes (static const):
MULTI_MID   = 0;   // continue accumulating into the prior PSUM (RMW)
MULTI_START = 1;   // begin (zero) a multi-matmul accumulate group
MULTI_END   = 2;   // commit the group (drain to SBUF)
SINGLE      = 3;   // one-shot matmul (zero, compute, commit)
```

This is exactly the NKI accumulate semantics: `accumulate=False → SINGLE`; `accumulate=None`
auto → first write `MULTI_START` (overwrite), subsequent `MULTI_MID` (accumulate on top); the
group ends at `MULTI_END` before PSUM is evicted. `[HIGH/OBSERVED enum + CARRIED NKI semantics]`

### 4.2 The PSUM zero-region

The `psum_zero_region` (`NEURON_ISA_TPB_MATMUL_ZERO_REGION`, `common.h:1350-1354`) selects how
many physical banks the matmul clears on the first write of a group:

| enum value | clears | physical banks |
|---:|---|---:|
| `SIZE2048` = 0 | 2048 fp32 cells | **1 bank** |
| `SIZE4096` = 1 | 4096 cells | 2 banks |
| `SIZE8192` = 2 | 8192 cells | 4 banks |
| `SIZE16384` = 3 | 16384 cells | **8 banks** |

So **PSUM = 8 physical banks × 2048 fp32 accumulator cells** — and the `2048`-per-bank figure is
the same count `PeManageSeed` manages (§5): one stochastic-rounding seed per accumulator cell of
a bank. `[HIGH/OBSERVED]`

### 4.3 `fp32_mode` (TF32 / split-mantissa select)

`NEURON_ISA_TPB_PE_FP32MODE` (`common.h:1281-1285`): `None=0 / Low=1 / High=2 / Low_High=3`. The
validity (`s3d3_mm_valid_fp32_mode`, header verbatim) requires **FP32 input ⇒ `Low`/`High`/
`Low_High`** and **non-FP32 input ⇒ `None`**. `tfloat32` (TF32) is FP32 input + `fp32_mode=Low`
(the reduced-mantissa pass); `Low_High` is a **two-pass full-FP32 split-mantissa accumulate**.
`[HIGH/OBSERVED enum + validity]`

### 4.4 The matmul micro-op sequence

```c
// ---------------------------------------------------------------------------
// PE systolic weight-load -> matmul -> PSUM-drain micro-op sequence.
// Real symbols: LDWEIGHTS=0x01 (S3_LW_STRUCT), MATMUL=0x02 (S3D3_MM_STRUCT).
// Inner MAC: ivp_mul4t* / ivp_mulpa* / ivp_dmulq* / ivp_packvr* (XR weight broadcast).
// The firmware ORCHESTRATES; the systolic array + PSUM banks are HW.
// ---------------------------------------------------------------------------

// One accumulate group: dst[col] = sum_k stationary[k,col] * moving[k]  (FP32 PSUM)
static void pe_matmul_group(const s3_lw_t   *lw,        // Ldweights descriptor (0x01)
                            const s3d3_mm_t *mm_tiles,   // a run of Matmul descriptors (0x02)
                            int n_tiles)
{
    // 1) LDWEIGHTS (0x01): load the STATIONARY tile into the array as the XR weight bus.
    //    SBUF-only (AllowedInPSUM::False); in_dtype/fp32_mode select the multiplier path;
    //    load_order (LAST_COL_FIRST default) feeds first data to the last column.
    pe_load_weights(lw);                                 // "S: Ldweights" -> stub 0x2b86

    for (int t = 0; t < n_tiles; ++t) {
        const s3d3_mm_t *mm = &mm_tiles[t];
        unsigned mode = psum_group_mode(mm);             // SINGLE / MULTI_{START,MID,END}

        // 2) zero-or-accumulate per the group mode (the decode-selected PSUM path):
        if (mode == SINGLE || mode == MULTI_START)
            psum_zero(mm->dst_mem_pattern, zero_region_banks(mm));  // clear 1..8 banks

        // 3) MATMUL (0x02): stream the MOVING tile, MAC into PSUM.
        //    per output column group (tile_sel.col_sel): per contraction tile
        //    (num_active_rows reduce, K folded 2/4/8 at a time):
        //        XR = weight (from LdWeight), vec = moving activation lane;
        //        ivp_mul4t* (4-term) / ivp_mulpa* (pair) / ivp_dmulq* (quad) into the
        //        1536-bit wvec accumulator (int) or the FP32 PSUM bank (float);
        //        the accumulator is forwarded back-to-back (II=1, the MAC chain).
        pe_stream_matmul(mm);                            // "S: Matmul" -> stub 0x2b8e

        // 4) on MULTI_END / SINGLE: commit the group; out_dtype (FP32 | BF16 v4+) drains.
        //    A BF16 drain uses the PSUM stochastic-rounding RNG (seeds managed by op 0x08).
        //    The ACT engine later evicts PSUM via ActivationReadAccumulator (op 0x24).
        if (mode == SINGLE || mode == MULTI_END)
            psum_commit(mm->dst_mem_pattern, mm->out_dtype);  // matmul_done_last -> SBUF resp
    }
}
```

---

## 5. `PeManageSeed` (0x08) — the PSUM stochastic-rounding seeds

`PeManageSeed` has its **own** 64-byte operand struct (`S2S1D2_PE_SEED_STRUCT`, compile-verified)
and manages the **PSUM fp32→bf16 stochastic-rounding RNG seed state** — useful for ML model
checkpointing (a stochastic-rounding matmul restarts from a saved seed). The header is decisive:

> *"The PeManageSeed instruction is used to: 1. Load seeds into the PSUM fp32→bf16 stochastic
> rounding logic from SBUF … 2. Save PSUM stochastic rounding seeds (RNG states) into PSUM …
> For NeuronCore-v4, there are 2048 seeds in total for a PSUM. Each PSUM partition has 16
> different stochastic rounding states (8 banks × 2 rounding/bank) … Each seed is 32-bit … The
> seeds to load are of shape 16×128 in SBUF partition 0-15 (128/partition); the seeds to store
> are 128×16 in PSUM partition 0-127 (16/partition) … we must transpose the saved seeds in PSUM
> before we can load them back."* `[HIGH/OBSERVED — header verbatim]`

```c
typedef struct NEURON_ISA_TPB_S2S1D2_PE_SEED_STRUCT {
    NEURON_ISA_TPB_HEADER       header;               // 0   (opcode=0x08 PE_MANAGE_SEED)
    NEURON_ISA_TPB_EVENTS       events;               // 4
    NEURON_ISA_TPB_TENSOR2D     src_seed_mem_pattern; // 12  LoadSeed: input seeds in SBUF (16x128, INT32)
    NEURON_ISA_TPB_TENSOR1D     identity_mem_pattern; // 24  the HW "magic" 16-element identity diagonal
    NEURON_ISA_TPB_DTYPE        identity_dtype;       // 32  BF16/FP16/FP32r/FP8
    NEURON_ISA_TPB_PE_SEED_MODE mode;                 // 33  None(0)/LoadSeed(1)/SaveSeed(2)
    uint8_t                     num_active_rows;      // 34  hard-coded 16
    uint8_t                     num_active_cols;      // 35  hard-coded 128
    NEURON_ISA_TPB_TENSOR2D     dst_seed_mem_pattern; // 36  SaveSeed: output seeds in PSUM (128x16)
    uint8_t                     reserved[16];         // 48 - 63  must be 0
} NEURON_ISA_TPB_S2S1D2_PE_SEED_STRUCT;                // sizeof == 64
```

The `mode` is `NEURON_ISA_TPB_PE_SEED_MODE` (`common.h:1288-1292`):
**`NONE = 0`, `LOAD_SEED = 1`, `SAVE_SEED = 2`**.

> **CORRECTION — the `s2s1d2_pe_seed.h` prose comment is off-by-one; the enum is authoritative.**
> The header's prose reads *"LoadSeed (value of **0**)"* and *"SaveSeed (value of **1**)"*, but
> the actual `NEURON_ISA_TPB_PE_SEED_MODE` enum is `NONE = 0, LOAD_SEED = 1, SAVE_SEED = 2`. The
> enum is the wire contract; the prose comment is **stale**. A reimplementer must encode
> **LoadSeed = 1, SaveSeed = 2, None = 0** — and the firmware DEBUG strings
> `S: PeManageSeed(LOAD)` / `(SAVE)` are the two non-`None` modes. `[HIGH/OBSERVED — enum vs
> comment diff this session]`

### 5.1 What it manages, and how (the micro-op realisation)

The seed rides the weight/matmul ports, but the **thing pushed through the array is the
identity matrix**, not the seed vectors. The handler's own DEBUG strings decompose it (byte-exact
this session, `0x447520`/`0x447547`):

```
S: PeManageSeed(SAVE)                            // read the seed state OUT (PSUM -> )
S: PeManageSeed(LOAD)                            // write the seed state IN  ( -> PSUM)
S: PeManageSeed : micro-op : LdWeight           // load the IDENTITY matrix as the stationary weight
S: PeManageSeed : micro-op : Matmul : is_load=%d// run a TRANSPOSING matmul; is_load = LoadSeed vs SaveSeed
```

The handler loads the `identity_mem_pattern` 16-element diagonal (the header's *"magic tensor
required by HW to make load/save seed work"*) as the stationary weight, then issues a
**transposing** `Matmul` micro-op to move the 2048 seeds between SBUF and PSUM. The validity
contracts pin the exact micro-op shape (header verbatim): on **MAVERICK**,
`s3d3_mm_check_valid_load_seed_fields` / `_save_seed_fields` require `tile_size {r64, c256}`,
a single source element of value 1 (the identity), `in_dtype FP32`, `fp32_mode High`, `xpose_mode
Enabled`, `num_active_rows 1`, `num_active_cols 0`, `out_dtype FP32`; SaveSeed's dst is a `1×1×2`
PSUM tensor. The same `seed_mode` field rides `S3D3_MM.flags` and `S3_LW.flags`. The
`PeManageSeed` struct itself hard-codes `num_active_rows == 16`, `num_active_cols == 128`. The
validity gate is `nc == NeuronCoreVersion::V4`. `[HIGH/OBSERVED struct + validity; the in-array
routing INFERRED-HIGH from the identity-matrix mechanism]`

> **CORRECTION — `PeManageSeed` is the PSUM SR-RNG, NOT a "PE-array per-cell PRNG".** An earlier
> survey read the firmware's `LfsrSetSeeds`/`XorwowSetSeeds` strings as `PeManageSeed`'s target.
> Those LFSR/XORWOW PRNGs are the **GpSimd / Vector** engine's seed state, managed by the
> **separate** `rand_set_state` (0x78) / `rand_get_state` (0x77) opcodes (present on all gens,
> targeting GpSimd/Vector, NeuronCore-v3+). `PeManageSeed` (0x08, v4-only) manages the
> **TensorE PSUM** stochastic-rounding seeds (the fp32→bf16 drain). The two are different RNG
> state managers on different engines. `[HIGH/OBSERVED — the header is decisive; the
> rand_*_state opcodes confirmed `jq` + enum this session]`

### 5.2 The PSUM-save layout (SaveSeed validity)

The SaveSeed dst is a PSUM `TENSOR2D` with `num_elem[0] == 2`, `num_elem[1] == 8` (two seeds per
bank × 8 banks), `step_elem[0] == 1`, `step_elem[1] == 512` (the per-bank stride is 512
elements), `start_addr == 0x200_0000` (byte 0 of PSUM partition 0), dtype `UINT32`,
`AllowedInPSUM::True`. The header warns: *"Neuron must evict all data out of PSUM from previous
computation before running this PeSaveSeed instruction."* `[HIGH/OBSERVED validity]`

---

## 6. The inner MAC — the IVP widening family (PE PERF IRAM)

On the GPSIMD/int-quantized path the PE engine carries the full **IVP widening-MAC** datapath.
The census below was **re-read this session** directly from the MARIANA PE PERF IRAM (carved file
`0x335ca0`, decoded `ncore2gp`, exit 0); the counts reproduce byte-for-byte. The `*XR8`/`*XR16`
suffix is the XR reduce-register **weight** broadcast (loaded by `Ldweights`); the other factor
is the **moving activation** lane. `[HIGH/OBSERVED census this session]`

| mnemonic (count) | role ([B04](../../isa/ref/b04-mac-integer.md) / [B05](../../isa/ref/b05-mac-mixed.md)) |
|---|---|
| `ivp_mul4t2n8xr8` (64) | 4-TERM dot, i8 × XR8 — the densest, the i8 matmul core |
| `ivp_mul4tn16xr8` (35) | 4-TERM dot, i16 × XR8 |
| `ivp_mulpan16xr16` (21) | PAIR-accumulate MAC (2×-FMAC): `acc += a1·b1 + a2·b2` |
| `ivp_mul4ta2n8xr8` (12) | 4-TERM accumulate, i8 × XR8 → 1536-bit `wvec` |
| `ivp_mul4tan16xr8` (11) | 4-TERM accumulate, i16 × XR8 |
| `ivp_mulsupn16xr16` (9) | signed×unsigned PAIR ([B05](../../isa/ref/b05-mac-mixed.md)) |
| `ivp_mulp2n8xr16` (9) | PAIR, i8 × XR16 |
| `ivp_muluupn16xr16` (8) | unsigned×unsigned PAIR |
| `ivp_mul4tan16xr16` (7) | 4-TERM accumulate, i16 × XR16 → 1536-bit `wvec` |
| `ivp_mulus4t2n8xr8` (5) | u×s 4-TERM, i8 × XR8 ; `ivp_muluspan16xr16` (5) u×s PAIR-acc |
| `ivp_dmulusq2n8xr8` (5) | u×s **dual-QUAD** MAC (8 products) — the MX `OCT_ROW` pump |
| `ivp_packvr2nx24` / `ivp_packvru2nx24` (1 ea) | `wvec` pack/drain (the i8→24 accumulator load/store) |

Reconciliation (each maps onto a [B04](../../isa/ref/b04-mac-integer.md)/[B05](../../isa/ref/b05-mac-mixed.md) fact):

- **`mul4t*`/`mul4ta*`** (4-TERM) = the full-tile MAC folding 4 input pairs per op into the
  4-entry **1536-bit `wvec`** accumulator (48-bit-per-lane × 32 lanes); the matmul folds K input
  pairs per issued op (2 for PAIR, 4 for 4T, 8 for the dual-QUAD `dmulq`). `[HIGH]`
- **`mulpa*`** (PAIR-accumulate) = the 2×-FMAC `acc += a1·b1 + a2·b2` — the **FP8 `double_row`**
  perf mode (two element pairs, two multiplies per cycle, contraction free-dim == 2). `[HIGH/CARRIED NKI]`
- **`mulus*`/`muluu*`/`mulsu*`** = the signed/unsigned/mixed widening MAC (the int8/int16
  quantized matmul with mixed signedness — activation signed, weight unsigned, etc.). `[HIGH]`
- **`ivp_packvr*`** = the `wvec` pack/unpack — zero/seed the accumulator before the MAC chain,
  drain it after. `[HIGH]`

The **integer** accumulator is the 4-entry 1536-bit `wvec` regfile (widening i8→24 / i16→48 /
i32→96 per lane, wrapping mod-`2^acc_w`, no saturate — see
[SIMD datapath §3](../../uarch/simd-datapath.md) and [pipeline timing](../../uarch/pipeline-timing.md)
for the int-MAC@LAT-12 / FMA@LAT-10 two-track datapath). The **TensorE float** accumulator is the
FP32 PSUM banks (always FP32 internal). The MAC family is gen-stable v4→v5 (the MAVERICK PE PERF
IRAM carries the same `ivp_mul4t*`/`mulpa*`/`dmulq*` set). `[HIGH/OBSERVED census]`

> **HW-vs-FW boundary.** The IVP MAC ops above are the GPSIMD NX-core's **own** vector-MAC
> datapath the PE firmware also carries — the int-quantized / assist path. The dedicated
> systolic array does the bulk float MAC in HW (no netlist ships). The exact division of labour
> (which matmuls run on the dedicated array vs the NX-core MAC) is `[INFERRED-MED]` — the array
> RTL is out of corpus.

---

## 7. The MX (microscaling) matmul path

The MX path has **two mechanisms, per gen**: `[HIGH/OBSERVED structs]`

* **v4 (MARIANA / MARIANA_PLUS): SEPARATE ops.** `LdweightsMX` (0x09, `SMX1_LW_STRUCT`) +
  `MatmulMX` (0x0A, `SMX1D3_MM_STRUCT`). The operand is an `MXMEM_PATTERN1D` carrying its **own**
  `scale_addr` (the out-of-band E8M0 per-block scale) + data address (+ indirect index). Both
  structs `sizeof == 64`: `SMX1_LW` = `src_mem_pattern`@16 `MXMEM_PATTERN1D`, `in_dtype`@32,
  `num_active_rows`@38, `num_active_cols`@39, `row_grp`@44, `col_grp`@45; `SMX1D3_MM` adds
  `out_dtype`@40, `psum_accumulate_flags`@43, `psum_zero_region`@47, `dst_mem_pattern`@48
  `MEM_PATTERN3D` (→ PSUM).
* **v5 (MAVERICK): UNIFIED.** The separate MX ops are retained in the enum but the preferred path
  **folds MX into `Ldweights`(0x01)/`Matmul`(0x02)**. The header is explicit: *"when the source
  uses MXTensorV2 access pattern, the instruction operates in MX mode … This replaces the separate
  LdWeightMX/MatmulMX instruction from previous chips."* The `src_mem_pattern` is a
  `MEM_PATTERN3D` union `{t: TENSOR3D | i: INDIRECT16B | mx: MXTENSOR_V2}`; the `start_addr`'s
  `ADDR4MARKER_MXTENSOR_V2 = 0x01` (LSB set; `mxtensorv2_pattern()` validity selector) picks the
  `mx` member with its `scale_addr`. `[HIGH/OBSERVED struct + validity selector]`

The MX dtype set (`is_valid_mx_dtype`, header verbatim) is `{FP8_EXP2(0x11), FP8_EXP3(0xD),
FP8_EXP4(0xE), FP8_EXP5(0xF), FP4_EXP2(0x10), INT4}`; the per-block scale is the OCP-MX
power-of-two `SFP8_E8 = 0x13` (`FP8_S0E8M0`). The `MX_PERF_MODE` (`common.h:1396-1404`) pumps
rows: `NONE=0x0`, `QUAD_ROW=0x1` (+`_INTERLEAVE=0x2`/`_TILED=0x3`), `OCT_ROW=0x4`
(+`_INTERLEAVE=0x5`/`_TILED=0x6`) — the 4×/8× MX-pumped matmul perf modes (the int4/fp4/fp8
density advantage). The MX compute/dequant paths are the
[MX dequant kernel page](./mx-dequant.md). `[HIGH/OBSERVED enum]`

> **NOTE — the MX block-scale TAP is `[MED/INFERRED]`.** That the per-block E8M0 scale is applied
> **in the MAC datapath** (the IVP `mul*`/`dmulq*` family carries the packed-register scale
> operand) is `[HIGH/OBSERVED]`; whether the scale enters at the multiplier input vs at the PSUM
> drain is **not** byte-decodable from the FLIX-desynced handler interior — the array RTL is out
> of corpus.

---

## 8. The dtype matrix

The NKI matmul contract (`validate_matmul_dtypes` / `validate_tensor_engine_output_dtype`,
documented in the backing reports — no NKI `.py` in this checkout, so `[HIGH/CARRIED]`)
reconciled with the compile-verified header validity (`has_valid_mm_in_dtype` /
`has_valid_mm_out_dtype`, `[HIGH/OBSERVED]`):

- **Valid input dtypes** (stationary AND moving): `{float8_e4m3, float8_e5m2, bfloat16, float16,
  tfloat32, float32}` (plus the e2m5/e3m4 fp8 variants in the header). RULE: if either input is
  `tfloat32`/`float32`, both must be — fp8/bf16/fp16 may mix, FP32 cannot mix with low-precision.
- **Internal accumulation = FP32 always** (the PSUM banks).
- **Output `dst` dtype** (`has_valid_mm_out_dtype`): `out_dtype == 0` (= FP32) on **NC-v2/v3**;
  `FP32 | BFLOAT16 | 0` on **NC-v4+**. So **FP32 (all gens), BF16 (v4+ only)**.

| inputs (stat × mov) | accumulate | output dst | gen / perf |
|---|---|---|---|
| fp8 e2m5/e3m4/e4m3/e5m2 (may mix) | FP32 | FP32 / BF16(v4+) | v3+ ; v3+ `double_row` PAIR (2 mults/cyc) |
| bf16 × bf16 | FP32 | FP32 / BF16(v4+) | all |
| fp16 × fp16 | FP32 | FP32 / BF16(v4+) | all |
| tf32 × tf32 (`fp32_mode Low`) | FP32 | FP32 | all |
| fp32 × fp32 (`fp32_mode Low/High/Low_High`) | FP32 | FP32 | all |
| MX fp4_e2m1 / fp8 / int4 (E8M0 scale) | FP32 | FP32 / BF16 | v4 (`SMX`) / v5 (unified); QUAD/OCT pump |

dtype ordinals (`common.h`): `BFLOAT16=0x6, FP16=0x7, FP32=0xA, FP32R=0xB (FP22/tf32-RTL),
FP8_EXP3=0xD, FP8_EXP4=0xE, FP8_EXP5=0xF, FP4_EXP2=0x10, FP8_EXP2=0x11, SFP8_E8=0x13`.
`[HIGH/OBSERVED ordinals + header validity; CARRIED NKI surface]`

---

## 9. `MatmulSparse` (0x07) + `LdTags` (0x06) — the sparse-matmul consumer

`MatmulSparse` is *"the version of Matmul with fine grain sparsity enabled … it sends 2/3/4
fmaps per PE partition across the PE array where metadata on the weights will select which fmap to
use for any PE array cell."* It uses the **4-D source** struct `S4D3_MM_STRUCT` (one `TENSOR4D`
src `@12-31`, one `TENSOR3D` dst `@48-63`):

| off | field | type | note |
|---:|---|---|---|
| 0 | `header` | `Header` (4) | opcode `0x07 MATMUL_SPARSE` |
| 4 | `events` | `Events` (8) | |
| 12 | `src_mem_pattern` | `Tensor4d` (20) | the 4-D src; dim[3] = the sparse fmaps per partition |
| 32 | `in_dtype` | `Dtype` (1) | |
| 33 | `fp32_mode` | `PeFp32Mode` (1) | |
| 34 | `out_dtype` | `Dtype` (1) | |
| 35 | `perf_opt` | `PerfOptMode` (1) | |
| 36 | `reserved_quant[2]` | — | formerly `quant_offset` |
| 38 | `num_active_rows` | `u8` | |
| 39 | `num_active_cols` | `u8` | |
| 40 | `reserved[3]` | — | |
| 43 | `psum_accumulate_flags` | `u8` | SINGLE/MULTI group |
| 44 | `row_grp` | `u8` | row-group mask |
| 45 | `col_grp` | `u8` | `0x0f` (fine-grain sparsity uses all 4 column groups) |
| 46 | `active_row_mode` | `ActiveRowMode` (1) | logical-vs-physical top rows |
| 47 | `psum_zero_region` | `MatmulZeroRegion` (1) | |
| 48 | `dst_mem_pattern` | `Tensor3d` (16) | → PSUM |

The tag (a 16-bit within-group index) is produced by `LdTags` (`S3_LT`, op 0x06): the 4-bit tags
are packed **4 per `u16`** (LSB → larger logical PE column ID, col 127), with `force_tag_bits`
`{0x00, 0x04, 0x08, 0x0C}` OR'd into the low bits to select the row-tile fmap group. The
`Ldweights` `tag_weight_mode = TAG_WEIGHT (1)` path instead reads an interleaved `{u16 weight,
u16 tag}` `UINT32` element. These tag formats are the exact unit the
[SparsityCompress(Tag) producer](./sparsity-compress-tag.md) emits — producer and consumer match
end-to-end. `[HIGH/OBSERVED — struct compile-verified; tag format cross-pinned with the producer page]`

---

## 10. The PE↔SEQ handshake — the `arr_seq` CSR block

Two RTL-generated CSR surfaces sequence the array (field names byte-exact from
`tpb_arr_seq_top_host_visible.json` and `tpb_arr_seq_cluster_host_visible.json`, this session).
The host window is **96% telemetry** — the matmul *descriptor* format is carried by the micro-op
stream (this firmware's structs), **not** as host CSRs. `[HIGH/OBSERVED]`

### 10.1 The host-visible config + telemetry (`tpb_arr_seq_top`, 305 regs)

* **`arr_seq_cfg` — the control surface:**
  * `queue_cfg.fifo_size_sel` — FIFO-full policy (full-on-fill vs single-entry).
  * `queue_cfg.bypass_peregwrite_instr` — `PeRegWrite` skips the SBUF fetch (reg-immediate).
  * `queue_cfg.disable_dependency_check` — queues become single-threaded (no inter-instr
    dependency check; serialises in-flight micro-ops).
  * `perf_cntr_cfg.{cntr_en, cntr_rst}` — perf-counter master enable / reset-pulse.
* **Three per-tile perf banks** `arr_seq_ififo_perf_{weight_load, matmul, pe_regwrite}`,
  byte-identical layout — **49 logical 64-bit counters each** (98 registers/bank = 49 ×
  `{_instr_cnt_lsb, _instr_cnt_msb}`), one per **tile shape** (`tile_128x128` … `tile_32x32`,
  i.e. 128/64/32 × 128/64/32 = 9 shapes). So the sequencer counts `Ldweights`(0x01) /
  `Matmul`(0x02) / `PeRegWrite`(0x03) issue **per sub-tile** — the 3-micro-op-class telemetry
  directly mirroring the PE micro-op set. `[HIGH/OBSERVED]`

> **NOTE — no precision/dtype/accumulate/dimension register in the host window.** There is no
> PSUM accumulate reg, no dtype reg, no array-dimension-select reg host-visible — those are
> carried by the micro-op descriptors (`S3_LW`/`S3D3_MM`). Only the queue policy + the per-tile
> telemetry are host-visible. `[HIGH/OBSERVED CSR negative]`

### 10.2 The cluster sequencer (`tpb_arr_seq_cluster`) — the matmul sequencing bits

The actual weight-load / matmul sequencing lives in `arr_cluster_cfg` (AddressOffset `0x0`; field
positions + reset values verbatim from the JSON):

| field | bit pos | reset | role |
|---|---|---|---|
| `enable_wl_last_active_col` | 0 | 0x0 | set the last-active column for a weight-load's target column |
| `flush_p2f_fifos` | 4 | 0x0 | flush the PE-to-fabric response FIFOs |
| `matmul_done_last` | 8 | 0x0 | generate Matmul "DONE" on the **last** SBUF response (vs first) — the completion-notification control |
| `en_inter_instr_dly` | 12 | 0x0 | enable the inter-instruction response delay |
| `inter_instr_dly_cnt` | 21:16 | **0x8** | inter-instruction response delay count (reset 8) |
| `p2fifo_af` | 26:24 | 0x2 | p2f FIFO almost-full threshold |
| `array_stagger_rg{0..3}_ctrl1` | — | — | the array idle-timer max limit (per row-group) |

**The handshake:** the SEQ engine (this firmware) decodes the instruction stream and issues the
`LdWeight`/`Matmul`/`PeRegWrite` micro-op descriptors into the array's input FIFO (the "ififo");
the array consumes weights/activations from SBUF and writes partials to PSUM; **completion** is
signalled by the matmul-done on the SBUF response (`matmul_done_last` selects last-vs-first); the
`en_inter_instr_dly`/`inter_instr_dly_cnt` pair throttles back-to-back micro-op responses;
dependency checking between in-flight micro-ops is the queue's job (`disable_dependency_check`
serialises). `[HIGH/OBSERVED CSR field names + positions; end-to-end flow INFERRED-HIGH from the
CSR semantics + the structs]`

---

## 11. Per-generation evolution

| feature | CAYMAN (v3) | MARIANA (v4) | MARIANA_PLUS (v4+) | MAVERICK (v5) |
|---|---|---|---|---|
| PE handler set | `{Ldweights, Matmul, MatmulSparse, LdTags, PeRegWrite}` | + `PeManageSeed`, `LdweightsMX`, `MatmulMX`, `ConvLutLoad` | same as v4 | (PERF dominant; opcode set via enum) |
| `S3_LW` flags | **none** (no `seed_mode`) | `LD_WEIGHT_FLAGS`@**47** + `bg_xpose_mode`@12 | same | re-packed: `flags`@**38** + `tile_size`/`tile_sel`/`perf_mode` |
| `S3D3_MM` flags | **none** | `MATMUL_FLAGS`@**42** + `psum_accumulate_flags`@43 | same | `flags`@**39** (incl. `accumulate_flag`); separate byte gone |
| src/dst mem-pattern | `TENSOR3D` | `MEM_PATTERN3D` (MX-capable union) | same | `MEM_PATTERN3D` (MX-capable union) |
| array tiling | `row_grp`/`col_grp` | `row_grp`/`col_grp` | same | `tile_size{r64/r128 × c128/c256}` + `tile_sel` |
| array columns | 128 | 128 | 128 | **256** (`c256` = two 128-col halves, double-pumped) |
| `out_dtype` BF16 | no | yes | yes | yes |
| `PeManageSeed` / MX / `ConvLutLoad` | absent | present | present | present (MX **unified** into 0x01/0x02) |

The CAYMAN PE DEBUG DRAM carries **0** `PeManageSeed`/`MatmulMX` hits; the `s2s1d2_pe_seed.h`,
`smx1_lw.h`, `smx1d3_mm.h` headers are absent from the cayman/sunda header sets and their
`struct2opcode` entries return `null`. The `c256` double-pump is pinned by `TILE_COL_SIZE`
(`common.h:1318-1320`: `C128=7`=2⁷, `C256=8`=2⁸) and the MM validity (*"if c256 and
tensor_pattern, the upper dim of dst must have a size of 2, to hold MM output from both PE col
0-127 and PE col 128-255"*); a MAVERICK `Ldweights`/`Matmul` with `num_active_cols == 0` runs the
full 256-column array. `valid_pe_tile_select` constrains `tile_sel.{row_sel,col_sel} ≤ 1` (the
2×2 quadrant selector). `[HIGH/OBSERVED struct + enum diffs, compile-verified per gen]`

> **WALL — v5/MAVERICK interiors are header-OBSERVED only.** The MAVERICK struct/enum/opcode
> facts are header-compile-verified `[HIGH/OBSERVED]`. The MAVERICK PE image carries a
> `S: BEGIN on maverick` anchor but the full handler-name set is not symbol-recovered from this
> carve; MAVERICK firmware-interior claims are `[INFERRED]`. The per-`(gen×PE)` carve bytes are
> the forward image pages (Part 6).

---

## 12. Honesty ledger

**HIGH / OBSERVED (this session):**

- The PE opcode roster byte-exact (`LDWEIGHTS=0x01 … MATMUL_MX=0x0A, CONV_LUT_LOAD=0xe4`) from
  the ISA enum AND independently byte-decoded from the MARIANA PE DEBUG IRAM dispatch chain
  (`bnei a2,1/2/3/6/7/8` + `movi a3,9` + `bnei a2,10` — the `66 0X 02` encodings reproduced) AND
  the `struct2opcode` map AND the DEBUG handler strings (quadruple agreement).
- All six PE operand structs compile-verified `sizeof == 64` with per-gen byte-offsets: `S3_LW`
  (flags @47 MARIANA / @38 MAVERICK), `S3D3_MM` (flags @42 MARIANA / @39 MAVERICK,
  `psum_accumulate_flags`@43 MARIANA), `S4D3_MM` (MatmulSparse, 4-D src, `psum_zero_region`@47),
  `S2S1D2_PE_SEED` (64B), `SMX1_LW`, `SMX1D3_MM`.
- `PeManageSeed` (0x08) manages the **PSUM fp32→bf16 stochastic-rounding seeds** (2048/PSUM,
  32-bit; LoadSeed 16×128 SBUF / SaveSeed 128×16 PSUM; `mode` enum `NONE=0/LOAD_SEED=1/
  SAVE_SEED=2`, the prose comment 0/1 is stale); the identity-matrix transport; the validity
  contract pinning the micro-op shape. Distinct from the GpSimd/Vector `rand_set_state`(0x78)/
  `rand_get_state`(0x77) PRNG.
- The IVP widening-MAC census re-read from the MARIANA PE PERF IRAM (`ivp_mul4t2n8xr8`=64 …
  `ivp_dmulusq2n8xr8`=5, `ivp_packvr2nx24`); XR weight broadcast; 1536-bit `wvec` int acc / FP32
  PSUM. PSUM = 8 banks × 2048 fp32 cells (the `MATMUL_ZERO_REGION` enum).
- The PSUM accumulate group (`MULTI_START=1/MULTI_MID=0/MULTI_END=2/SINGLE=3`), the `MATMUL_FLAGS`
  / `LD_WEIGHT_FLAGS` bitfields, `ACCUMULATE_MODE` (OVERWRITE=0/ACCUMULATE=1), `PE_LW_ORDER`
  (LAST_COL_FIRST=0/FIRST_COL_FIRST=1), `PE_FP32MODE`, `TILE_SIZE`/`TILE_SEL`, `MX_PERF_MODE`,
  the dtype ordinals.
- The MX two-mechanism (v4 separate ops 0x09/0x0A `MXMEM_PATTERN1D`+`scale_addr`; v5 unified via
  the `MEM_PATTERN3D` `mx` union + `ADDR4MARKER_MXTENSOR_V2=0x01`); `is_valid_mx_dtype`
  {fp8 variants, fp4_e2m1, int4}; `SFP8_E8=0x13` scale.
- The `arr_seq` handshake CSRs: the three per-tile perf banks (49 counters × 9 tile shapes),
  `queue_cfg` (dependency/serialise/peregwrite-bypass), `arr_cluster_cfg` (`matmul_done_last`
  pos 8, `en_inter_instr_dly` pos 12, `inter_instr_dly_cnt` pos 21:16 reset 0x8, `p2fifo_af`,
  `enable_wl_last_active_col`, `flush_p2f_fifos`, `array_stagger_rg{0..3}`).
- Per-gen handler presence (CAYMAN 0 `PeManageSeed`/`MatmulMX` hits; MARIANA/MARIANA_PLUS the
  full set; the gen anchors `S: BEGIN on {cayman,mariana,mariana_plus,maverick}`).

**MED / INFERRED:**

- The exact in-handler micro-op issue order (the FLIX handler bodies do not linearly decode; the
  dispatch chain + struct contract + MAC census + strings are HIGH).
- The MX block-scale TAP in the MatmulMX datapath (multiplier-input vs PSUM-drain — array RTL out
  of corpus); the HW/FW labour split (dedicated array vs NX-core IVP MAC).
- The NKI surface (LoadStationary/MultiplyMoving naming, the dtype set, `double_row`) is `[CARRIED]`
  from the backing reports — no NKI `.py` in this checkout.

**LOW / NOT CLAIMED:**

- The PE-array converter-table SRAM geometry (`ConvLutLoad` target; out of corpus).
- The PE matmul HW cycle cost (the systolic fill/drain latency — array timing is HW).
- The MAVERICK PE firmware handler *names* beyond the enum/opcode presence and the
  `S: BEGIN on maverick` anchor (the v5 interior WALL).

---

## 13. Cross-references

- [SparsityCompress / SparsityCompressTag](./sparsity-compress-tag.md) — the DVE producer of the
  `{value, 16-bit index}` tags this engine consumes via `MatmulSparse 0x07` / `LdTags 0x06` /
  `Ldweights tag_weight_mode`.
- [MX (Microscaling) Dequant Compute Paths](./mx-dequant.md) — the MX block-scale / dequant
  paths the `LdweightsMX`/`MatmulMX` (v4) and unified MX `Ldweights`/`Matmul` (v5) consume.
- [ISA Batch 04 — Integer MAC Matrix (signed)](../../isa/ref/b04-mac-integer.md) and
  [ISA Batch 05 — MAC (mixed-sign / wide-acc)](../../isa/ref/b05-mac-mixed.md) — the per-instruction
  reference for the `ivp_mul4t*`/`mulpa*`/`mulus*`/`packvr*` widening-MAC family the matmul issues.
- [Per-Engine Firmware Depth](../../uarch/per-engine-depth.md) — the cross-engine micro-op roster
  (PE / SP / TOP_SP / ACT) and the `arr_seq` handshake at the engine level.
- [SIMD compute datapath](../../uarch/simd-datapath.md) and
  [pipeline timing](../../uarch/pipeline-timing.md) — the Vision-Q7 int-MAC@LAT-12 / FMA@LAT-10
  two-track datapath and the 1536-bit `wvec` accumulator.
- [Confidence & Walls Model](../../reference/confidence-model.md) — the tag taxonomy.
- The per-`(gen×PE)` firmware-image carve bytes are the forward PE image pages (Part 6).
