# On-Chip State-Buffer (SBUF) + PSUM Bank Model

Every NeuronCore TPB carries two on-chip SRAM arrays the GPSIMD Q7 cluster and the
fixed-function engines fight over: a **32 MiB State Buffer (SBUF)** and a **4 MiB
PSUM** accumulator. Both are organised as **128 partitions** (rows) × a per-partition
byte budget; the partition axis is the systolic array's width. This page reconstructs,
to the addressing math, how a `(partition, byte_offset)` pair maps to a linear address;
how the 16 SBUF / 32 PSUM ECC banks decompose that address space; how the PE array
streams partial sums into PSUM; how the `axi2sram` crossbar transposes in flight; how
the two-stage SBUF arbiter + TDM resolves bank conflicts; and — the crux —
**why the GPSIMD Q7 cores can reach SBUF but physically cannot touch PSUM.**

Everything here is grounded in the shipped compile-grade ISA headers
(`aws_neuron_isa_tpb_common.h`, `aws_neuron_isa_tpb_s3d3_mm.h`, …), the Cayman CSR
JSON (`tpb_sbuf_cluster.json`, `erg_parity_model.json`, `tpb_arr_seq_*.json`), and the
RTL-generated SoC address map (`address_map_flat.yaml`). Confidence is tagged
**HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED** per claim.

> **KEYSTONE (HIGH / OBSERVED).** The Q7/GPSIMD cores reach SBUF only as **AXI bus
> masters** through the `axi2sram` bridge + a pinned NX window onto the 32 MiB SBUF
> SoC aperture. **PSUM has no AXI aperture and no pinned NX window** — its SoC region
> sits in a *disjoint* top-level address block reachable only by the PE/ACT/POOL
> datapath ports. This is the physical reason the verifier forbids any GPSIMD/customop
> tensor in PSUM. Byte evidence in [§7](#7-access-asymmetry--q7-via-axi-aperture-vs-engines-via-arbiter-clients).
> See also [Keystone Facts](../orientation/keystone-facts.md).

Related pages: the LSU / on-core memory model is in [LSU & Memory](../uarch/lsu-memory.md);
the SBUF/PSUM SoC address regions in [TPB POOL Address Subtree](../control/address/tpb-pool.md);
the matmul firmware in [PE Matmul](../firmware/kernels/pe-matmul.md); the DMA descriptor
machinery in [Descriptor Model](descriptor-model.md); the working-memory overview in
[On-Chip Working Memory](onchip-working-memory.md).

---

## 1. Geometry constants — the per-generation machine model

The ISA header `aws_neuron_isa_tpb_common.h` carries the machine-model constants as
`static const uint32_t` — byte-exact, one header per codegen target
(`sunda`=NC-v2, `cayman`=NC-v3, `mariana`=NC-v4, `maverick`=NC-v5). All values below
are **HIGH / OBSERVED** unless noted.

| `NEURON_ISA_TPB_…` constant            | sunda      | cayman     | mariana    | maverick    |
| -------------------------------------- | ---------- | ---------- | ---------- | ----------- |
| `STATE_BUF_NUM_PARTITIONS`             | 128        | 128        | 128        | 128         |
| `STATE_BUF_WORD_SIZE`                  | 8 B        | 8 B        | 8 B        | 8 B         |
| `STATE_BUF_PARTITION_SIZE` (stride)    | 256 KiB    | 256 KiB    | 256 KiB    | **1024 KiB**|
| `STATE_BUF_PARTITION_ACTIVE_SIZE`      | 192 KiB    | 224 KiB    | 256 KiB    | 512 KiB     |
| `PSUM_BUF_NUM_PARTITIONS`              | 128        | 128        | 128        | 128         |
| `PSUM_BUF_PARTITION_SIZE` (stride)     | 32 KiB     | 32 KiB     | 32 KiB     | 32 KiB      |
| `PSUM_BUF_PARTITION_ACTIVE_SIZE`       | 16 KiB     | 8×2048     | 8×2048     | 8×2048      |
| `PSUM_BUF_NUM_BANKS`                   | **16**     | **8**      | **8**      | **8**       |
| `PSUM_BUF_BANK_SIZE`                   | **1024 B** | **2048 B** | 2048 B     | 2048 B      |
| `TPB_PARTITION_ADDR_MASK`              | 0x1fffffff | 0x1fffffff | 0x1fffffff | 0x1fffffff  |
| `STATE_BUF_OFFSET`                     | 0          | 0          | 0          | 0           |
| `PSUM_BUF_OFFSET`                      | 0x2000000  | 0x2000000  | 0x2000000  | 0x2000000   |
| `STATE_BUF_SZ`                         | 0x2000000  | 0x2000000  | 0x2000000  | **0x8000000** |
| `PSUM_BUF_SZ`                          | 0x400000   | 0x400000   | 0x400000   | **0x0** ⚠   |
| `PE_ARRAY_NUM_ROWS`                    | 128        | 128        | 128        | 128         |
| `PE_ARRAY_NUM_COLS`                    | 128        | 128        | 128        | **256**     |
| `PE_ARRAY_NUM_ROW_GROUPS`              | 4          | 4          | 4          | **2**       |
| `PE_ARRAY_NUM_COL_GROUPS`              | 4          | 4          | 4          | 4           |
| `DMA_ENGINE_DATA_WIDTH_BYTES`          | 32         | 32         | 32         | **256**     |
| `DMA_ENGINE_COUNT_PER_TPB`             | 16         | 16         | 16         | **8**       |

> **CORRECTION (vs the earlier DX-DMA-06 synthesis).** Several maverick (v5) constants
> diverge from the "stable across gens" picture the synthesis carried. Byte-exact from
> `neuron_maverick_arch_isa/.../aws_neuron_isa_tpb_common.h`:
> `PE_ARRAY_NUM_COLS = 256` (not 128), `PE_ARRAY_NUM_ROW_GROUPS = 2` (not 4),
> `STATE_BUF_SZ = 0x8000000` (128 MiB region, not 32 MiB),
> **`PSUM_BUF_SZ = 0x0` — PSUM is sized out / disabled on the maverick target**,
> `DMA_ENGINE_DATA_WIDTH_BYTES = 256`, `DMA_ENGINE_COUNT_PER_TPB = 8` (with a
> `DMA_ENGINE_COUNT` enum {ONE,TWO,FOUR,EIGHT}). [HIGH / OBSERVED, maverick header.]
> Because v5/maverick is **header-OBSERVED only** (no byte-grounded firmware image),
> all *interior* v5 geometry below (interleave, bank wiring) is flagged **INFERRED**.

> **NOTE — stride vs active.** `PARTITION_SIZE` is the address-space *stride* between
> partitions; `PARTITION_ACTIVE_SIZE` is the SRAM actually backing each partition. On
> sunda the low 192 KiB of each 256 KiB stride is SRAM, the top 64 KiB is decode pad,
> so the validity predicate `addr_in_sbuf_partition()` honours the ACTIVE bound
> ([§1.2](#12-the-validity-predicates-byte-exact-pseudo-code)). The active size grew
> 192 → 224 → 256 KiB across v2 → v3 → v4, then maverick re-based to a 1 MiB stride /
> 512 KiB active. The compiler floor "≤ 176 KiB usable" is `192 − 16` (the v2 evaccel +
> compiler carveout), the sunda-targeting case.

The SoC address map confirms the SBUF region size byte-exact:
`STATE_BUF` carries `size = 33554432 = 0x2000000` (32 MiB) in `address_map.txt`.
[HIGH / OBSERVED.]

### 1.1 The 8-byte SBUF word

`STATE_BUF_WORD_SIZE = 8` is the native SBUF access granule (a 64-bit word). The
verifier's `addr_aligned_dtype()` permits 1/2/4/**8**-byte dtype alignment, the 8-byte
maximum being this word. The Q7 data bus is 512-bit (64 B) and the SDMA data width is
32 B (sunda) — both are integer multiples of the 8-B word, which is the unit the 16 ECC
banks protect. [HIGH for the constant; **INFERRED** that 8 B is the ECC/interleave word.]

---

## 2. Address decode — `(partition, byte_offset) ↔ 29-bit linear address`

A TPB on-chip address is a **29-bit `PartitionOffset`** (`TPB_PARTITION_ADDR_MASK =
0x1fffffff`). SBUF lives in `[0, STATE_BUF_SZ)` = `[0, 0x2000000)`; PSUM in
`[PSUM_BUF_OFFSET, PSUM_BUF_OFFSET + PSUM_BUF_SZ)` = `[0x2000000, 0x2400000)`. The
decode is **a single multiply-add**: the partition index is the high part (stride
`PARTITION_SIZE`), the byte offset the low part.

```c
// All names are the byte-exact ISA-header constants/predicates in
// neuron_sunda_arch_isa/tpb/aws_neuron_isa_tpb_common.h (validity pseudo-code,
// reproduced here as C). [HIGH / OBSERVED — every line is a header comment fn.]

// addr_imm_mask: a TPB on-chip address is only 29 bits wide.
static inline uint32_t addr_imm_mask(uint32_t addr) {
    return addr & TPB_PARTITION_ADDR_MASK;            // 0x1fffffff
}

// Region test is a pure magnitude compare: STATE_BUF_OFFSET == 0,
// PSUM_BUF_OFFSET == 0x2000000. Anything at/above 32 MiB is PSUM.
static inline bool addr_in_sbuf(uint32_t a) { return a < STATE_BUF_OFFSET + STATE_BUF_SZ; }
static inline bool addr_in_psum(uint32_t a) {
    return a >= PSUM_BUF_OFFSET && a < PSUM_BUF_OFFSET + PSUM_BUF_SZ;
}

// The forward map: place (partition, byte_offset) at a linear address.
static inline uint32_t sbuf_addr(uint32_t partition, uint32_t byte_off) {
    return STATE_BUF_OFFSET + partition * STATE_BUF_PARTITION_SIZE + byte_off;
}
static inline uint32_t psum_addr(uint32_t partition, uint32_t byte_off) {
    return PSUM_BUF_OFFSET  + partition * PSUM_BUF_PARTITION_SIZE  + byte_off;
}

// The inverse map (SBUF): divide/modulo by the per-gen stride.
static inline uint32_t get_sbuf_partition_id(uint32_t a)        { return (a - STATE_BUF_OFFSET) / STATE_BUF_PARTITION_SIZE; }
static inline uint32_t get_sbuf_byte_offset_in_partition(uint32_t a) { return (a - STATE_BUF_OFFSET) % STATE_BUF_PARTITION_SIZE; }

// A quadrant is 32 partitions: STATE_BUF_PARTITION_SIZE * 32.
static inline uint32_t get_sbuf_quadrant_id(uint32_t a) {
    return (a - STATE_BUF_OFFSET) / (STATE_BUF_PARTITION_SIZE * 32);
}
```

> **QUIRK (HIGH / OBSERVED).** PSUM detection is *purely a magnitude test*:
> `addr_in_psum()` is `addr >= 0x2000000`. There is no bank bit, no region register —
> an `ADDR4` whose 29-bit immediate lands at/above 32 MiB *is* a PSUM address, below it
> is SBUF. This is why the same `MEM_PATTERN3D` walker addresses both: the partition
> axis is the stride-`PARTITION_SIZE` high part, the free axes are the in-partition
> byte budget.

### 2.1 The 32-partition quadrant

The verifier groups partitions into four 32-wide **quadrants** with bases
`{0, 32, 64, 96}`. `get_sbuf_quadrant_id()` divides by `STRIDE × 32`, and
`addresses_in_same_sbuf_quadrant(a0, a1)` literally tests membership of
`addr_in_sbuf_partition({0,32,64,96}, a)`. The matmul, the tensor-tensor op, and the
SDMA all attend to quadrant granularity (e.g. SDMA rounds the active-partition count up
to a multiple of 32). [HIGH / OBSERVED — `addresses_in_same_sbuf_quadrant` /
`addresses_in_same_psum_quadrant`.]

### 2.2 The validity predicates (byte-exact pseudo-code)

The decode is wrapped by `tensor_start_addr_valid()`, which gates every op's operand by
per-op `AllowedInPSUM` / `AllowedInSBUF` flags and forces fp32 width on PSUM writes:

```c
// tensor_start_addr_valid: the per-operand placement gate. [HIGH / OBSERVED]
bool tensor_start_addr_valid(Addr4 addr, Dtype dtype, WriteTensor write,
                             AllowedInPSUM allow_psum, AllowedInSBUF allow_sbuf) {
    // (a) a register-marker address (marker == 0x80, regnum < NUM_ADDR_REGISTERS=64)
    //     bypasses the placement check entirely (resolved at runtime).
    if (addr.marker == ADDR4_MARKER_ADDR_REG /*0x80*/ &&
        addr.reserved0_lo == 0 && addr.reserved0_hi == 0 &&
        addr.regnum < NUM_ADDR_REGISTERS)
        return true;

    uint32_t a = addr_imm_mask(addr.addr_immediate);
    return addr_aligned_dtype(addr.addr_immediate, dtype)
        && (addr_in_psum(a) || addr_in_sbuf(a))            // must be on-chip
        && (allow_psum == AllowedInPSUM_True || !addr_in_psum(a))   // op may write PSUM?
        && (allow_sbuf == AllowedInSBUF_True || !addr_in_sbuf(a))   // op may write SBUF?
        // *** PSUM writes are the fp32 accumulator domain: a write to PSUM must be 4-byte
        && (type_size_check(dtype, 4) || !addr_in_psum(a) || write == WriteTensor_False);
}

// tt_valid_partitions: a tensor_tensor op may touch AT MOST ONE PSUM operand.
// {PSUM,SBUF}, {SBUF,PSUM}, or {SBUF,SBUF}+same-quadrant — never {PSUM,PSUM}.
bool tt_valid_partitions(Addr4 a0, Addr4 a1) {
    if (addr_from_register(a0) || addr_from_register(a1)) return true;  // 0x80 bypass
    uint32_t x = addr_imm_mask(a0.addr_immediate), y = addr_imm_mask(a1.addr_immediate);
    return (addr_in_psum(x) && addr_in_sbuf(y))
        || (addr_in_sbuf(x) && addr_in_psum(y))
        || (addr_in_sbuf(x) && addr_in_sbuf(y) && addresses_in_same_sbuf_quadrant(x, y));
}
```

> **NOTE.** A **GPSIMD-routed** tensor_tensor op is the `{SBUF, SBUF}` subset of
> `tt_valid_partitions` — *both* operands SBUF — because the Q7 has no PSUM path
> ([§7](#7-access-asymmetry--q7-via-axi-aperture-vs-engines-via-arbiter-clients)). The
> compiler's "customop args/outputs must be in SBUF" rule is exactly this predicate
> restricted to the GPSIMD engine. [HIGH / OBSERVED predicate; INFERRED tie.]

---

## 3. SBUF banking — 128 partitions × (8 cluster × 2 half) = 16 ECC banks

The 32 MiB SBUF has **two orthogonal decompositions** of the same address space:

**(A) Partition axis (the ISA/datapath view).** 128 partitions × `PARTITION_SIZE` stride.
Total stride space = 128 × 256 KiB = 32 MiB; SRAM-backed = 128 × 192 KiB = 24 MiB
(sunda), rising to 32 MiB on mariana. [HIGH / OBSERVED — header.]

**(B) ECC-bank axis (the PEB/RTL view).** The SoC address map enumerates exactly **16**
SBUF ECC blocks per TPB:

```
PEB_APB_IO_0_AMZN_SE_0_TPB_0_SBUF_CLUSTER_{0..7}_{0,1}_ERG_CSR   →  16 nodes
   = 8 SBUF clusters × 2 halves (0/1)
   base 0x..D10000, inter-cluster stride 0x2000 (two halves × 0x1000 each)
   each block: size 0x40, schema csrs/erg/erg_parity_model.json
```

32 MiB / 16 = **2 MiB per ECC bank**. [HIGH / OBSERVED — exact 16-node count + stride
in `address_map_flat.yaml`.]

### 3.1 How (A) and (B) relate — the partition→bank interleave

The 128 partitions and 16 ECC banks are orthogonal. The only reading consistent with
both counts *and* the GPSIMD wiring:

- The **8 SBUF clusters** align with the **8 GpSimd cores'** partition ranges — each
  GpSimd core owns 16 *contiguous* partitions (8 × 16 = 128), so SBUF cluster *c* covers
  partitions `[16c, 16c+16)`. [The 8×16 partition map is HIGH/OBSERVED; the
  cluster↔core alignment is the **MED/INFERRED** reading that makes 8 clusters × 2
  halves = 16 banks line up with 8 cores.]
- The **2 halves** split each 16-partition cluster's 2 MiB either as 16 partitions ×
  128 KiB or 8 partitions × 256 KiB — both fit a 2 MiB bank. The address map is a single
  32 MiB leaf with no sub-decode, so the bit that selects the half (a partition bit vs a
  byte-offset bit) is **not byte-recoverable** and is flagged INFERRED.

> The **OBSERVED** anchors are firm: 128 partitions, 16 ECC banks, 8 clusters × 2
> halves, 2 MiB/bank, 256 KiB partition stride. Only the half-select bit is MED.

### 3.2 The per-bank ECC / BIST block (`erg_parity_model`, 0x40 B)

Each of the 16 SBUF (and 32 PSUM, [§4](#4-psum-banking--128-partitions--4-cluster--8--32-ecc-banks))
ECC banks owns one 64-byte `erg_parity_model` register block. Verified byte-exact from
`csrs/erg/erg_parity_model.json` (`SizeInBytes = 0x40`):

| reg @off            | fields (bit) — `ResetValue`                                                                 | role |
| ------------------- | ------------------------------------------------------------------------------------------ | ---- |
| `init_cfg` @0x0     | `start[0]` (init pulse), `serialize[4]`, `init_type[8]` (1=BIST/0=legacy), `init_clk_en[12]`| SRAM init / BIST kick |
| `init_status` @0x4  | `busy[0]`, `done[1]`, `unsupported[2]`                                                       | init status |
| `mem_cfg` @0x8      | `shutdown[0]` (power-gate), `test1a[20]`/`test1b[21]` (self-timed bypass), `testrnm[22]`, `testrwm[23]` (margin) | bank power/test |
| `cfg` @0xc          | `dis_uncerr[0]`, `eg_cfg0[11:4]`/`eg_cfg1[19:12]` (error-gen), **`erg_type[20]` (0=Parity, 1=ECC)** | ECC config |
| `eg_sram_uncerr` @0x10 / `uncerr_sram_mask` @0x14 / `uncerr_cnt` @0x18 `[7:0]` / `uncerr_sram_status` @0x1c / `uncerr_stat_clear` @0x20 | per-SRAM uncorrectable-error gen-enable / mask / count (saturates 0xFF) / status / clear | uncerr path |
| `spare_reg` @0x24   | `spare[31:0]`                                                                               | metal ECO |

> **CORRECTION (vs DX-DMA-06).** The synthesis listed redundancy-MUX fields
> `rmea/rmeb/rma/rmb` (SRAM row-repair selects) in `mem_cfg`. **Those fields are not
> present** in the shipped `erg_parity_model.json` — `mem_cfg` carries only
> `shutdown` + the four `test1a/test1b/testrnm/testrwm` margin pins. The `erg_type`
> field (Parity vs ECC, bit 20 of `cfg`) is the real ECC-mode selector. [HIGH / OBSERVED.]

---

## 4. PSUM banking — 128 partitions × (4 cluster × 8) = 32 ECC banks; 8 banks/partition

Same dual-decomposition, two different 8's that must not be conflated:

**(A) Partition/bank axis (ISA).** 128 partitions × 16 KiB active = **8 HW banks of
2048 B** per partition (= 512 fp32/bank — the matmul accumulator unit). Stride is
32 KiB/partition (16 KiB active + 16 KiB pad). [HIGH / OBSERVED — header.]

**(B) ECC-bank axis (PEB).** The SoC map enumerates exactly **32** PSUM ECC blocks:

```
PEB_APB_IO_0_AMZN_SE_0_TPB_0_PSUM_CLUSTER{0..3}_ERG_CSR_{0..7}   →  32 nodes
   = 4 PE clusters × 8 ECC blocks
   PSUM_CLUSTER0 base 0x..2C10000; ERG_CSR_n stride 0x40 (the 0x40 erg block)
   inter-cluster stride 0x1000:  C10000 / C11000 / C12000 / C13000
   each block: size 0x40, schema csrs/erg/erg_parity_model.json
```

4 MiB / 32 = 128 KiB per ECC bank. [HIGH / OBSERVED — exact 32-node count + strides.]

### 4.1 The 4 PSUM clusters == the 4 PE column-groups

`PE_ARRAY_NUM_COL_GROUPS = 4` (header, [§1](#1-geometry-constants--the-per-generation-machine-model)).
The 128 array columns split into 4 column-groups; each column-group drains into one
PSUM cluster. The 4 PSUM ECC clusters are therefore the 4 PE column-groups' drain banks
— the partial sums accumulate **down the columns** of each cluster
([§5](#5-psum-accumulator-write-pattern--the-matmul--psum-datapath)). [HIGH / OBSERVED
both counts; the column-group↔ECC-cluster identity is HIGH/INFERRED.]

### 4.2 Reconciling "8 banks/partition" with "32 ECC banks = 4×8"

| 8                              | axis        | meaning |
| ------------------------------ | ----------- | ------- |
| **8 banks/partition** (ISA)    | partition   | the 8 accumulator banks (2048 B = 512 fp32 each) one partition's 16 KiB active PSUM is divided into; what `MATMUL_ZERO_REGION` selects and the modulo allocator rotates |
| **8 ECC banks/cluster** (PEB)  | per-cluster | the 8 `ERG_CSR` ECC blocks (128 KiB each) per PE column-cluster |

Cell budget cross-check: 128 partitions × 8 banks × 2048 B = 2 MiB of accumulator
cells; the region is 4 MiB (fp32 width / decode pad). The 32 ECC banks protect that.
[HIGH counts; the cells-vs-region 2× is **CARRIED/INFERRED**.]

> **GOTCHA — the sunda 16-bank convention.** `PSUM_BUF_NUM_BANKS = 16, BANK_SIZE =
> 1024 B` on **sunda**, but `8 × 2048 B` on cayman/mariana/maverick — both equal 16 KiB
> active / partition. The matmul ISA only ever names the **8-bank (2048 B)** view (the
> `MATMUL_ZERO_REGION` granularity, [§5.3](#53-matmul_zero_region--the-zero-on-begin-granularity)),
> so the 8-bank view is the matmul-relevant one. 16 ISA-banks(1024 B) == 8 HW-banks(2048 B)
> == 16 KiB == 512 fp32/partition. The silicon reason sunda reports 16 (likely a finer
> ECC granule on CoreV2) is not decoded. [HIGH/OBSERVED both conventions; LOW why.]

---

## 5. PSUM accumulator write pattern — the Matmul → PSUM datapath

PSUM is written only by the systolic array. The `S3D3_MM` (Matmul) instruction
(`aws_neuron_isa_tpb_s3d3_mm.h`, **64 B**, `OPCODE_MATMUL`) is the writer; `LdWeights`
(`s3_lw.h`, `OPCODE_LDWEIGHTS`) loads the stationary weight tile **from SBUF only**.

```c
// Verified byte-exact: ISA_STATIC_ASSERT(sizeof(...) == 64). Offsets are header comments.
typedef struct NEURON_ISA_TPB_S3D3_MM_STRUCT {
    NEURON_ISA_TPB_HEADER   header;            //  4  (0  - 3)   opcode = OPCODE_MATMUL
    NEURON_ISA_TPB_EVENTS   events;            //  8  (4  -11)
    uint8_t                 reserved0[4];      //  4  (12 -15)
    NEURON_ISA_TPB_TENSOR3D src_mem_pattern;   // 16  (16 -31)   MOVING (ifmap), SBUF only
    NEURON_ISA_TPB_DTYPE    in_dtype;          //  1  (32)       FP32/FP16/BF16/FP8e{3,4,5}/UINT8
    NEURON_ISA_TPB_PE_FP32MODE fp32_mode;      //  1  (33)       Low/High/LowHigh/None
    uint8_t                 reserved1[1];      //  1  (34)
    NEURON_ISA_TPB_PERF_OPT_MODE perf_opt;     //  1  (35)       None/DoubleRow/DoubleColumn/DoublePixel
    NEURON_ISA_TPB_QUANT_OFFSET quant_offset;  //  2  (36 -37)
    uint8_t                 num_active_rows;   //  1  (38)       contraction depth (1..128)
    uint8_t                 num_active_cols;   //  1  (39)       output cols → dst PSUM partition span
    uint8_t                 reserved2[3];      //  3  (40 -42)
    uint8_t                 psum_accumulate_flags; // 1 (43)     BEGIN/END group bitfield
    uint8_t                 row_grp;           //  1  (44)       SBUF source quadrant bitmask
    uint8_t                 col_grp;           //  1  (45)       PSUM dest quadrant bitmask
    uint8_t                 xbus_sel;          //  1  (46)       SBUF read / PSUM write bus select
    NEURON_ISA_TPB_MATMUL_ZERO_REGION psum_zero_region; // 1 (47) zero-on-begin granularity
    NEURON_ISA_TPB_TENSOR3D dst_mem_pattern;   // 16  (48 -63)   PSUM only, fp32 (uint32 for UINT8 in)
} NEURON_ISA_TPB_S3D3_MM_STRUCT;
```

> **CORRECTION (vs DX-DMA-06).** The synthesis's field table omitted the matmul's
> `row_grp @44`, `col_grp @45`, and `xbus_sel @46`. These three are the heart of the
> matmul→PSUM partition decode (below). [HIGH / OBSERVED struct.]

### 5.1 The write mechanism

`LdWeights` loads the stationary weight tile into the array (`tensor3d_valid(src, …,
AllowedInPSUM::False, AllowedInSBUF::True)` — **SBUF only**). `Matmul` streams the
moving activation tile (`src_mem_pattern`, SBUF) through the loaded array; each PE cell
multiply-accumulates; **partial sums accumulate down the columns into PSUM**.
`dst_mem_pattern` is validated `AllowedInPSUM::True, AllowedInSBUF::False` (UINT32 for
UINT8 input, else FP32) — **PSUM is the write-only, fp32 accumulator domain.**
`num_active_cols` (1..128) selects the destination partition span; `num_active_rows`
(≤128) the contraction depth. [HIGH / OBSERVED — `valid_dest_tensor`, `s3_lw.h:63`.]

### 5.2 The column-group decode — `col_grp` → PSUM quadrant, with an HW BUG

`col_grp` is a 4-bit **column-group bitmask** selecting which PSUM quadrant the result
drains into, jointly gating `num_active_cols`, the PSUM destination partition, and the
crossbar bus (`xbus_sel`). All three are cross-checked by the verifier:

```c
// valid_mm_psum_quadrant + s3d3_mm_valid_col_group_active_columns + s3d3_mm_valid_xbus_sel
// (header pseudo-code, byte-exact). [HIGH / OBSERVED]
//
//  col_grp   PSUM dst quadrant       num_active_cols   xbus_sel   comment
//  -------   --------------------    ---------------   --------   -------------------------
//   0xf      partition 0  (full)         ≤ 128           0x10      full array → xbus[4]
//   0x3      partition 0  (lower half)   ≤  64           0x01      lower half → xbus[0]
//   0xc      partition 64 (upper half)   ≤  64           0x04      upper half → xbus[2]
//   0x1      partition 0  (quad 0)       ≤  32           0x01      q0
//   0x2      partition 32 (quad 1)       ≤  32           0x02      q1
//   0x4      partition 64 (quad 2)       ≤  32           0x04      q2
//   0x8      partition 96 (quad 3)       ≤  32          (0x08)     q3 — *** NOT SUPPORTED, BUG ***
```

> **QUIRK / HW BUG (HIGH / OBSERVED).** The header documents `col_grp == 0x8` (quadrant
> 3) as *"q3 // NOT SUPPORTED, BUG"*: `valid_mm_psum_quadrant` maps it to PSUM partition
> 96 and `s3d3_mm_valid_col_group_active_columns` allows `num_active_cols ≤ 32`, **but
> `s3d3_mm_valid_xbus_sel` has no `0x8 → 0x08` case** — so a matmul targeting PSUM
> quadrant 3 alone via `col_grp=0x8` fails xbus validation. A reimplementation must
> route q3-only writes through the full/half groupings. The mirror `row_grp` field
> (`@44`) applies the identical bitmask to the **SBUF source** quadrant via
> `valid_mm_sbuf_quadrant` (0xf/0x3→part0, 0xc→part64, 0x1→part0, 0x2→part32, 0x4→part64,
> 0x8→part96).

### 5.3 `MATMUL_ZERO_REGION` — the zero-on-begin granularity

The bank-count the matmul zeros at group-begin is selected by `psum_zero_region @47`.
**This enum is per-generation** — a hard correction:

```c
// sunda / cayman:                          mariana / maverick:
enum MATMUL_ZERO_REGION {                   enum MATMUL_ZERO_REGION {
  SIZE2048  = 0,  // one physical bank        SIZE2048  = 0,  // one   physical bank (1)
};                                            SIZE4096  = 1,  // two   physical banks (2)
                                              SIZE8192  = 2,  // four  physical banks (4)
                                              SIZE16384 = 3,  // eight physical banks (8)
                                            };
```

> **CORRECTION (vs DX-DMA-06).** The synthesis stated the 1/2/4/8-bank
> `SIZE2048/4096/8192/16384` enum is universal. **It is not.** On **sunda and cayman**
> the enum has **only `SIZE2048 = 0`** — the matmul can zero exactly **one** 2048-B
> bank at begin; the 2/4/8-bank zero regions exist only from **mariana (v4)** onward.
> [HIGH / OBSERVED, all four `aws_neuron_isa_tpb_common.h` headers.] The validity gate
> `mm_psum_flags()` accepts `{0,1,2,3}` and `MATMUL_ZERO_REGION_MAX_PLUS_ONE = 4`.

### 5.4 The accumulation flags — the K-loop pattern

```c
enum MATMUL_PSUM_ACCUMULATE_FLAGS {
    BEGIN_MATMUL_GROUP = (1 << 0),   // first matmul of an accumulation group: zero target
    END_MATMUL_GROUP   = (1 << 1),   // last  matmul of the group: drain / round
};
// Equivalent 2-bit "ACCUMULATE_MODE": MULTI_START=1 (begin), MULTI_MID=0, MULTI_END=2,
// SINGLE=3 (begin|end). mm_psum_flags() accepts exactly {0,1,2,3}.
```

A K-loop matmul therefore sets `BEGIN` on the first tile (which zeros the
`psum_zero_region` banks), `0` (mid) on the middle tiles (read-modify-write the *same*
PSUM bank), and `END` on the last (triggering the fp32→bf16 stochastic-rounding drain on
v4+). The PSUM bank stays resident across the K-loop. [HIGH / OBSERVED enum + flags.]

### 5.5 PSUM bank selection is the dst address magnitude — no CSR

Which of the 8 PSUM banks a matmul targets is encoded in the dst `MEM_PATTERN3D`
`start_addr` (an `ADDR4` in `[0x2000000, 0x2400000)`): the in-partition byte offset
selects the bank — `byte_off / 2048 = bank index (0..7)`. There is **no `psum_*` CSR**;
the PSUM addressing is the array sequencer + the instruction's dst address. [HIGH /
OBSERVED — the decode + the absence of any `psum_` register in the CSR set.]

### 5.6 The PE-array sequencer (the drain control)

The sequencer that orchestrates Ldweights/Matmul (and thus the PSUM bank writes) is
exposed by `tpb_arr_seq_top_host_visible.json` + `tpb_arr_seq_cluster_host_visible.json`.
Verified byte-exact:

```
arr_seq_top.queue_cfg @0x0:
    fifo_size_sel[0]                         (FIFO-full threshold)
    bypass_peregwrite_instr[4]               (PeRegwrite skips SBUF fetch)
    disable_dependency_check[8]              (single-thread the queue)
    disable_bg_xpose[12]            rst=0x1   *** background transpose DISABLED at reset ***
    disable_tagweight_mode[16]      rst=0x1
arr_seq_cluster.arr_cluster_cfg @0x0:
    enable_wl_last_active_col[0]
    flush_p2f_fifos[4]                       (flush PE→FIFO toward PSUM before next rsp)
    matmul_done_last[8]                      (done on last vs first SBUF response)
    en_inter_instr_dly[12]
    inter_instr_dly_cnt[21:16]      rst=0x8   (8-cycle inter-instruction delay)
    p2fifo_af[26:24]                rst=0x2   (PE→FIFO almost-full threshold)
arr_seq_cluster.array_stagger_rg{0,1,2,3}_ctrl1 @0x18/0x1C/0x20/0x24:
    rg{n}_ctrl1[15:0]                        (the 4 ROW-GROUP load-stagger idle timers)
arr_seq_top.arr_seq_ififo_perf_matmul:
    98 regs = 49 × {lsb,msb} 64-bit counters, keyed by tile shape
    (tile_128x128_tile0, tile_128x64_tile0/tile2, …) — per-tile-shape matmul counts
```

> **NOTE.** `p2fifo` is the PE-array→FIFO path draining accumulated columns toward
> PSUM; `flush_p2f_fifos` + `p2fifo_af` manage that drain. The 4 `array_stagger_rg`
> registers are the 4 `ROW_GROUP`s; the PSUM-side 4-cluster split is the 4 `COL_GROUP`s.
> `disable_bg_xpose` (bit 12, **reset = 1**) is the sequencer-side background-transpose
> disable — the same flag the runtime `gconf` exposes
> ([§6](#6-the-transpose-path--the-axi2sram-crossbar)). [HIGH / OBSERVED.]

---

## 6. The transpose path — the `axi2sram` crossbar

Moving a tensor so partition ↔ free axes swap is done by the `axi2sram` crossbar while
the DMA streams — a cross-partition (cross-bank) data move.

### 6.1 The `axi2sram` HW port

`tpb_sbuf_cluster.json` exposes the bridge config (byte-exact):

```
axi2sram_config @0x400:
    axi2sram_transpose_en @0x0:  transpose_en[0]  rst=0x1   "Enables transpose mode in axi2sram block"
    raw_config           @0x4:   raw_en[0]        rst=0x1   "Enables Read Add Write Logic"
    raw_conv             @0x8:   sns[0] (non-FP8 NaN saturate), sis[1] (non-FP8 Inf saturate)
    misc_config          @0x10:  clk_redun_pipe_gate_en[0]
```

> **NEW FIND (HIGH / OBSERVED) — the `axi2sram` does Read-Add-Write.** Beyond the
> transpose crossbar, the AXI→SRAM bridge carries a **`raw_en` Read-Add-Write logic
> block (reset = 1)** with non-FP8 NaN/Inf saturation converters (`raw_conv`). This is
> an atomic accumulate path *on the SBUF write side* — distinct from the PE-array PSUM
> accumulation. (Not mentioned in the DX-DMA-06 synthesis.) `transpose_en` is reset
> **enabled** (transpose-capable by default); the sequencer's `disable_bg_xpose`
> ([§5.6](#56-the-pe-array-sequencer-the-drain-control)) is reset **disabled** — the
> port is wired but background transpose is off until the firmware enables it.

### 6.2 The tile geometry — the 16-row crossbar tile

The DMA transpose structs pin the tile to **16 source rows**, byte-exact:

```c
// aws_neuron_isa_tpb_dma_direct2d_xpose.h  (OPCODE_DMA_DIRECT2D)  — non-indexed transpose
//   tile_src_rows  @60 : MUST == 16                      (has_valid_xpose_tile_rows_temporary)
//   tile_src_cols  @61 : 1..128 for 2-B out_dtype        (has_valid_xpose_tile_cols_temporary)
//                        1..64  for 4-B out_dtype
//   dst 32-B aligned (xbar transpose HW constraint)
//
// aws_neuron_isa_tpb_dma_gather_xpose.h  (OPCODE_DMA_GATHER_TRANSPOSE) — indexed transpose
//   src_num_elem[gather_dim] % 16 == 0   (row-tile alignment)
//   src_num_elem[1] == dst_num_elem[0]   (index count preserved)
//   dst_start_addr % 32 == 0, dst_step_elem[1] % 32 == 0   (xbar transpose HW constraints)
```

The crossbar reads N elements from each of 16 source partitions and writes them as a
column across N destination partitions — a cross-bank move (the 16 source partitions and
the N destination partitions live in different ECC banks). [HIGH / OBSERVED tile
constants.] The stride permutation is realised by the DGE swapping `step_elem[]` axes
(signed `DIMPUSH` for reverse walks); with `transpose_en` set, `axi2sram` lands the
16×N tile transposed. [tile + port HIGH/OBSERVED; stride-swap CARRIED from the
descriptor model.]

### 6.3 The canonical PSUM↔SBUF transpose — `PeManageSeed`

The stochastic-rounding seed flow is the textbook cross-bank transpose, documented
byte-exact in `aws_neuron_isa_tpb_s2s1d2_pe_seed.h` (`PeManageSeed`, mariana/maverick
only — v4+):

> *"For NeuronCore-v4, there are 2048 seeds in total for a PSUM. Each PSUM partition has
> 16 different stochastic rounding states (**8 banks × 2 rounding/bank**) … the seeds to
> load are of shape **16×128** and expected to be in **SBUF** partition 0–15 … the seeds
> to store are of shape **128×16** and expected to be in **PSUM** partition 0–127 … we
> must transpose the saved seeds in PSUM before we can load them back."*

> **REFINEMENT (vs DX-DMA-06).** The 16 seeds/partition is not arbitrary — it is
> **8 PSUM banks × 2 rounding states/bank**, a direct cross-check on the 8-bank/partition
> count ([§4.2](#42-reconciling-8-banks-partition-with-32-ecc-banks--48)). `SaveSeed`
> writes 128×16 to PSUM (one bank's worth per partition); `LoadSeed` reads 16×128 from
> SBUF — the same 16-row `axi2sram` tile running across the cluster boundary. A 16×16
> identity "magic tensor" in SBUF partition 0 is required by HW to drive the load/save.
> [HIGH / OBSERVED — header prose.]

The three transpose mechanisms are distinct: `axi2sram` (this section, DMA-driven SBUF
cross-partition); DVE `STREAM_TRANSPOSE` (`OPCODE_STREAM_TRANSPOSE`, a 32×32 datapath
lane-permute in the DVE engine, **not** the SBUF xbar); and SuperGather (index-driven
gather/scatter). [HIGH / OBSERVED opcode names.]

---

## 7. Access asymmetry — Q7 via AXI aperture vs engines via arbiter clients

This is the crux of the shared-state-buffer model: the Q7 cores and the fixed-function
engines reach SBUF by **different physical paths**, and that asymmetry is why GPSIMD
cannot touch PSUM.

### 7.1 PE / ACT / POOL / DVE — the in-cluster arbiter clients

The SBUF is a multi-client banked SRAM. PE/ACT/POOL/DVE have **dedicated datapath
ports** into the 16 ECC banks, arbitrated by `tpb_sbuf_cluster.json` (host-visible at
**clusters 0 and 4 only** — the two ends of the 8-cluster array, confirmed by the SoC
map exposing only `SBUF_CLUSTER_0/_4`). The arbiter is **two-stage**, byte-exact:

```
STAGE 1  arb_stage1 @0x000  — arbitrate the 5 PE-array sequencer clients:
   arb_prio  @0x0: pe_client_0[2:0] … pe_client_4[18:16]   3-bit strict prio, all rst=0x0
   arb_weight@0x4: pe_client_0[3:0] … pe_client_4[19:16]   4-bit DWRR weight, all rst=0x1
   → picks ONE PE read winner (the 5 clients = the 4 PE column-clusters + 1 fan-out).

STAGE 2  arb_stage2 @0x100  — arbitrate the engine clients + the stage-1 winner:
   arb_prio  @0x0: pool_rd[2:0] act_rd[6:4] pool_wr[10:8] act_wr[14:12] pe_rd_client[18:16]
                   → pe_rd_client rst=0x1  (the ONLY non-zero reset priority); others rst=0x0
   arb_weight@0x4: all clients rst=0x1 (round-robin DWRR among equals)
   → arbitrates {pool rd, act rd, pool wr, act wr, PE-read-winner} for the SBUF port.
```

> **CORRECTION (vs DX-DMA-06).** The synthesis read the stage-1 `pe_client_0..4` reset
> *priorities* as 1. They are **all reset 0**; only stage-2 `pe_rd_client` has reset
> priority **0x1**. The bandwidth bias toward the systolic array is set there (at reset
> the PE-read winner from stage 1 wins ties against POOL/ACT). [HIGH / OBSERVED reset
> values.] A bank conflict is two clients targeting the same ECC bank in the same cycle;
> the winner proceeds, the loser stalls one slot. [arbiter HIGH/OBSERVED; the
> conflict-stall reading INFERRED.]

These engines also reach PSUM directly — PE writes it via the column drain
([§5](#5-psum-accumulator-write-pattern--the-matmul--psum-datapath)); ACT/POOL read it
through their datapath ports (the "Pool/Act results to PSUM" path).

### 7.2 The TDM + throttler layers

Above the per-client arbiter sit two more layers:

```
tpb_sbuf_cluster.json  tdm_config @0x300:
   tdm_cfg @0x0: tdm_max_slots[15:0] rst=0x8   (8 total TDM slots)
                 tdm_dma_slots[31:16] rst=0x1  (1 DMA slot ⇒ 7 compute / 1 DMA at reset)

tpb_sbuf_pool_act.json  {pool,act,dve}_throttler @0x000/0x100/0x200:
   rd_throttle_cfg0.disable_throttle[0]  rst=0x1 (OFF)
   rd_throttle_cfg1: window_len[7:0], transfer_cnt[23:16]   (token bucket)
   wr_throttle_cfg0.disable_throttle[0]  rst=0x1 (OFF)
   wr_throttle_cfg1: window_len[7:0], transfer_cnt[23:16]
```

The full contention stack, coarse → fine:

```
TDM (8 slots: 7 compute / 1 DMA)            ── coarse compute-vs-DMA bandwidth split
  └─► 2-stage arbiter (PE clients → engine clients)   ── per-cycle SBUF port winner
        └─► per-engine rd/wr throttler (token bucket)  ── optional fine rate limit (OFF by default)
```

The DMA gets a guaranteed 1/8 SBUF port share so a DMA staging tensors cannot be starved
by compute, and DMA interference with compute is bounded to 1/8. [HIGH / OBSERVED — three
CSR layers.]

### 7.3 The Q7 / GPSIMD cores — the SBUF AXI aperture

A GPSIMD Q7 core has **no dedicated SBUF compute port**. Its ncore2gp core config
(`core-isa.h`) sets the AXI bus = 1 with ECC on the AXI bus = 1, no D-cache, DataRAM-only
data side — it is an **AXI/ACE-Lite bus master**. It reaches SBUF as memory-mapped AXI:

- The 32 MiB SBUF appears at its SoC aperture. The address map gives
  `TPB_0_STATE_BUF` base **`0x2000000000`**, size **`0x2000000`** (32 MiB), with
  `TPB_0_STATE_BUF_SCRATCH_RAM` immediately above at **`0x2004000000`** (another 32 MiB)
  — together the pinned 64 MiB NX-local window the Q7 sees onto SBUF. [HIGH / OBSERVED.]
- A GPSIMD load/store to SBUF is an AXI transaction that enters through the `axi2sram`
  bridge ([§6.1](#61-the-axi2sram-hw-port)) and arbitrates as part of the TDM DMA slot —
  the Q7/SDMA share the 1/8 DMA TDM slot, **not** a compute client port. [MED / INFERRED
  — `axi2sram` is the only AXI→SBUF port, and `tdm_dma_slots` is the DMA share; the join
  is inferred, not byte-exact.]

### 7.4 Why GPSIMD cannot touch PSUM — the keystone proof

> **KEYSTONE PROOF (HIGH / OBSERVED).** Compare the two SoC apertures from
> `address_map_flat.yaml`:
>
> ```
> TPB_0_STATE_BUF            base 0x000002000000000  size 0x0000002000000  (32 MiB)
> TPB_0_STATE_BUF_SCRATCH_RAM base 0x000002004000000  size 0x0000002000000  (the NX-window pad)
> TPB_0_PSUM_BUF            base 0x000002802000000  size 0x0000000400000  ( 4 MiB)
> ```
>
> `STATE_BUF` and `PSUM_BUF` live in **disjoint top-level address blocks** — SBUF in the
> `0x2000000000`-based window (with its scratch pad, the pinned 64 MiB NX aperture the Q7
> maps), **PSUM in the separate `0x2800000000`-based block.** PSUM's only other SoC
> presence is `PEB_…_PSUM` + its 32 `PSUM_CLUSTER{0..3}_ERG_CSR_{0..7}` ECC CSRs — the
> PEB control plane, not a data aperture. There is **no pinned NX window onto PSUM and no
> `axi2sram`-equivalent PSUM bridge**, so an AXI master (the Q7) has no path to PSUM at
> all.

This is reinforced at the ISA level: `LdWeights` src is `AllowedInPSUM::False`
(stationary weights from SBUF only); `Matmul` dst is `AllowedInPSUM::True,
AllowedInSBUF::False` (PSUM is the PE write-only accumulator); and no non-PE op carries
`AllowedInPSUM::True` *for a GPSIMD-routed operand*. The GPSIMD-no-PSUM /
customop-SBUF-only rule the compiler enforces is therefore **not a policy** — it is the
physical absence of a PSUM AXI path. [HIGH / OBSERVED — aperture disjointness +
per-op flags.] See [Keystone Facts](../orientation/keystone-facts.md).

### 7.5 The 8-core × 16-partition GPSIMD↔SBUF wiring

The 8 GpSimd cores each own 16 *contiguous* SBUF partitions
(`core[c] → partition[16c : 16c+16]`; 8 × 16 = 128). A GPSIMD op runs over its core's 16
partitions independently. This 8×16 map aligns with the 8 SBUF ECC clusters
([§3.1](#31-how-a-and-b-relate--the-partition-bank-interleave)). The SDMA rounds the
active-partition count to a multiple of 32 (`ceil(max(src,dst)/32)*32`), so even a GPSIMD
op spanning < 32 partitions triggers a one-quadrant (= 2-core) SDMA window. [HIGH /
OBSERVED partition map; MED cluster↔core alignment.]

---

## 8. End-to-end — a tile's life in the bank model

1. **Compile.** The tiler splits a tensor into a partition axis (→ 128 partitions,
   stride `PARTITION_SIZE`) + free axes (→ the per-partition byte budget). PSUM tiles
   ceil at one bank (512 fp32 = 2048 B). SBUF gets a 1-D byte offset; PSUM gets a
   `bank_id (0..7)` + in-bank offset, the 8 banks rotated for multi-buffering. GPSIMD
   tiles get **SBUF addresses only** ([§7.4](#74-why-gpsimd-cannot-touch-psum--the-keystone-proof)).
2. **Place.** The `ADDR4` `start_addr` encodes `(partition, byte_offset)`
   ([§2](#2-address-decode--partition-byte_offset--29-bit-linear-address)); PSUM
   addresses land in `[0x2000000, 0x2400000)`, SBUF below; the base partition is a
   quadrant base `{0,32,64,96}`.
3. **Stage.** SDMA/DGE DMAs HBM→SBUF through `axi2sram`, taking the 1/8 TDM DMA slot; a
   transpose rides the `axi2sram` 16×N crossbar tile. The Q7 AXI master uses the same
   path.
4. **Compute.** `LdWeights` reads SBUF (PE arbiter client); `Matmul` streams SBUF
   activations and accumulates **down columns into PSUM** (fp32; `BEGIN`/mid/`END` group
   flags across the K-loop, `col_grp` selecting the PSUM quadrant + xbus); ACT/POOL read
   PSUM and write SBUF; **GPSIMD reads/writes SBUF only.**
5. **Arbitrate.** Every SBUF bank access: TDM → 2-stage arbiter → optional throttler; a
   same-bank same-cycle conflict stalls the lower-priority client one slot.
6. **Drain.** `END_MATMUL_GROUP` triggers the fp32→bf16 SR drain (v4+), whose seeds need
   a PSUM→SBUF transpose ([§6.3](#63-the-canonical-psumsbuf-transpose--pemanageseed)).

---

## 9. Confidence ledger & corrections

**HIGH / OBSERVED** (byte-exact this pass):

- All per-gen geometry constants from the four `aws_neuron_isa_tpb_common.h` headers
  ([§1](#1-geometry-constants--the-per-generation-machine-model)), incl. the maverick
  divergences and `PSUM_BUF_SZ = 0`.
- The full `(partition, byte_offset) ↔ addr` decode + quadrant + `tt_valid_partitions` +
  `tensor_start_addr_valid` ([§2](#2-address-decode--partition-byte_offset--29-bit-linear-address)).
- The 16 SBUF ECC nodes (`SBUF_CLUSTER_{0..7}_{0,1}_ERG_CSR`) and 32 PSUM ECC nodes
  (`PSUM_CLUSTER{0..3}_ERG_CSR_{0..7}`) + strides + the `erg_parity_model` 0x40 block,
  from `address_map_flat.yaml` + `erg_parity_model.json` ([§3](#3-sbuf-banking--128-partitions--8-cluster--2-half--16-ecc-banks), [§4](#4-psum-banking--128-partitions--4-cluster--8--32-ecc-banks)).
- The Matmul struct (`row_grp`/`col_grp`/`xbus_sel`/`psum_zero_region`), the col_grp→PSUM
  quadrant map + the q3 BUG, the per-gen `MATMUL_ZERO_REGION` enum, the accumulate flags
  ([§5](#5-psum-accumulator-write-pattern--the-matmul--psum-datapath)).
- The `axi2sram` `transpose_en`/`raw_en`, the 16-row transpose tile, the `PeManageSeed`
  128×16↔16×128 transpose with the 8×2-state decomposition
  ([§6](#6-the-transpose-path--the-axi2sram-crossbar)).
- The two-stage arbiter (reset priorities), TDM (8 slots / 1 DMA), per-engine throttlers,
  the PE-array sequencer ([§5.6](#56-the-pe-array-sequencer-the-drain-control), [§7](#7-access-asymmetry--q7-via-axi-aperture-vs-engines-via-arbiter-clients)).
- The keystone: `STATE_BUF` (`0x2000000000`) vs `PSUM_BUF` (`0x2802000000`) disjoint
  apertures + the per-op `AllowedInPSUM/SBUF` flags ([§7.4](#74-why-gpsimd-cannot-touch-psum--the-keystone-proof)).

**MED** — the partition→ECC-bank half-select bit; the cluster↔core alignment; the Q7 AXI
master sharing the TDM DMA slot. **LOW** — the sunda 16×1024 PSUM-bank silicon reason;
the Q7-vs-SDMA arbitration order inside the DMA slot; **all v5/maverick interior geometry
(header-OBSERVED only).**

**Corrections issued vs the DX-DMA-06 synthesis** (all byte-grounded above):

| # | claim corrected | truth |
| - | --------------- | ----- |
| 1 | `MATMUL_ZERO_REGION` = 1/2/4/8 banks on all gens | sunda/cayman have **only** `SIZE2048` (1 bank); 2/4/8 banks are mariana(v4)+ |
| 2 | maverick geometry stable (128 cols, 4 row-groups, 32 MiB SBUF, PSUM present) | maverick: `COLS=256`, `ROW_GROUPS=2`, `STATE_BUF_SZ=0x8000000`, **`PSUM_BUF_SZ=0`**, `DMA width=256`, `engines=8` |
| 3 | `erg_parity_model.mem_cfg` has `rma/rmb/rmea/rmeb` redundancy fields | not present — only `shutdown` + `test1a/test1b/testrnm/testrwm`; ECC mode is `cfg.erg_type` |
| 4 | matmul fields = `…/psum_accumulate_flags@43/…/psum_zero_region@47` | also `row_grp@44`, `col_grp@45`, `xbus_sel@46` — the actual quadrant/bus decode |
| 5 | stage-1 `pe_client_0..4` reset priority 1 | all reset **0**; only stage-2 `pe_rd_client` resets to **0x1** |
| 6 | (new) `axi2sram` is transpose-only | also carries a `raw_en` **Read-Add-Write** accumulate block with NaN/Inf saturation |
| 7 | seeds: 16/partition (flat) | 16 states/partition = **8 banks × 2 rounding/bank** (the v4 8-bank cross-check) |
