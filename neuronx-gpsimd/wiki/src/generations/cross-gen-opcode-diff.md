# Cross-Generation Opcode-Table Diff + TONGA

This is the **ISA-evolution step-diff** for the GPSIMD opcode namespace and the
**definitive TONGA V1 deep-dive**. It answers, opcode by opcode, what every
generation step *adds*, *changes*, and *removes* across the unified ladder —
**SUNDA (v2) → CAYMAN (v3) → MARIANA (v4) → MARIANA_PLUS (v4+) → MAVERICK (v5)** —
with **TONGA (V1)** characterized as the pre-unified outlier from its own,
separate ISA package. It carries four principal results, each proven from the
shipped bytes:

> 1. The opcode byte is a **write-once global namespace.** Every opcode that lives
>    in two generations carries the **identical** hex value in both — proven across
>    the full SUNDA↔MAVERICK span (138 common opcodes, **zero** value drift). New
>    generations only **append**; the single re-baseline (v2→v3) **abandons** seven
>    bytes that are never reused.
> 2. There are **four** ISA opcode tables, not five. `NEURON_ISA_TPB_OPCODE` exists
>    in exactly the SUNDA / CAYMAN / MARIANA / MAVERICK trees. **MARIANA_PLUS reuses
>    the MARIANA table 1:1** — it is a firmware/register flag-refresh, **not a
>    distinct ISA**.
> 3. **TONGA V1 has a real opcode ISA of its own** — an **engine-scoped**
>    `TONGA_ISA_TPB_OPCODE` (`byte = local | (ENGINE<<5)`) with a 64-byte
>    instruction-struct family — but **not** the unified flat namespace. V1 is the
>    genesis of the v2+ opcode bytes (the datapath bytes are inherited verbatim) and
>    the genesis of the flattening (v2 drops the engine bitfield).
> 4. **There is no Vision-Q7 GPSIMD POOL engine in V1.** V1's POOL is hardwired
>    MaxPool/AvgPool; GPSIMD (the programmable `NX_POOL` Q7 cluster the rest of this
>    book documents) is a **SUNDA-onward (v2+) feature**.

Every value, count, and presence cell below was read from the shipped
header enums (bracket-bounded), cross-checked against the per-gen
`instruction_mapping.json`, and grounded with direct `rg`/bracket counts — never a
decompile. Confidence/evidence tags follow [The Confidence & Walls
Model](../reference/confidence-model.md): **HIGH/MED/LOW** × **OBSERVED/INFERRED/CARRIED**.
The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag.

This page owns the **evolution narrative + TONGA**. For the image-grounded,
per-generation KIT→kernel/engine/struct Rosetta, see the sibling [Cross-Gen
Kernel-Info Matrix](../images/cross-gen-kernel-info-matrix.md); for the per-header
structural diff see [Arch-ISA Header Diff](./arch-isa-header-diff.md); for the
completeness boundary (140 real HW opcodes) see [The Opcode Catalog
Ledger](../firmware/kernels/opcode-catalog-ledger.md); for the portability proof
that justifies decoding one machine see [The Gen-Invariance
Thesis](../orientation/gen-invariance.md); for the `(arch_id, coretype)` identity
algebra see [The Codename → Generation Map](./codename-generation-map.md).

---

## 1. What actually ships — the header-tree inventory

There are **four** `NEURON_ISA_TPB_OPCODE` enum trees (the authoritative opcode
source), one TONGA V1 ISA package (a *different* opcode form), and zero
`mariana_plus` opcode tree.

| Tree | NC version | `NEURON_ISA_TPB_OPCODE` body | enumerator count | enum-body lines |
|---|---|---|---|---|
| `neuron_sunda_arch_isa` | v2 | present | **145** | `153–299` |
| `neuron_cayman_arch_isa` | v3 | present | **150** | `154–305` |
| `neuron_mariana_arch_isa` | v4 (≡ v4+) | present | **159** | `155–315` |
| `neuron_maverick_arch_isa` | v5 | present | **165** | `157–323` |
| `arch-isa/aws_tonga_isa_*` | **v1** | **`TONGA_ISA_TPB_OPCODE`** (engine-scoped, 44 entries) | 44 | §5 |
| `neuron_mariana_plus_arch_isa` | — | **does not exist** | — | — |

All four counts are **independently bracket-bounded** (the count of
`NEURON_ISA_TPB_OPCODE_… = 0x…` lines between `typedef enum NEURON_ISA_TPB_OPCODE {`
and `} NEURON_ISA_PACKED NEURON_ISA_TPB_OPCODE;`), and each count includes the
`INVALID = 0xff` sentinel. The path stem is
`…/custom_op/c10/include/neuron_<gen>_arch_isa/tpb/aws_neuron_isa_tpb_common.h`.

> **NOTE — MARIANA_PLUS has no opcode tree.** There is no
> `neuron_mariana_plus_arch_isa/` directory anywhere in the package. A *register*
> tree `arch-headers/mariana_plus/` does exist, but it carries CSR/register-block
> headers (`aws_mariana_isa_common.h`, `a2i`, `cce`, `address_map`, …) with **no
> `aws_neuron_isa_tpb_common.h` and no `NEURON_ISA_TPB_OPCODE`**. MARIANA_PLUS
> executes the MARIANA 159-opcode table verbatim — see [§7](#7-is-mariana_plus-a-distinct-isa-no).

### 1.1 The version axis is cumulative, and it is born at v2

`NEURON_ISA_TPB_NEURON_CORE_VERSION` grows with the tree, and **no tree has a
`V4_PLUS` / `V4P` enumerator**:

| Tree | `NEURON_CORE_VERSION` enumerators |
|---|---|
| sunda | `V2 = 2` only |
| cayman | `V2 = 2`, `V3 = 3` |
| mariana | `V2 = 2`, `V3 = 3`, `V4 = 4` |
| maverick | `V2 = 2`, `V3 = 3`, `V4 = 4`, `V5 = 5` |

The SUNDA enumerator carries the decisive in-band comment:

```c
typedef enum NEURON_ISA_TPB_NEURON_CORE_VERSION {
    NEURON_ISA_TPB_NEURON_CORE_VERSION_V2 = 2,   // V1 ISA is maintained in separate package
} ... ;
```
`aws_neuron_isa_tpb_common.h:132` (sunda).

This single line is the structural keystone of the whole page: **the version axis
starts at v2**, it has **no V1 point** (V1 is externalized by name to a separate
package — that is TONGA, [§5](#5-tonga-v1-the-pre-unified-outlier)), and it has **no
v4+ point** (MARIANA_PLUS is distinguished by a runtime *string* selector, not an
ISA-version number — [§7](#7-is-mariana_plus-a-distinct-isa-no)).

---

## 2. The master opcode-presence matrix

Columns: **V2**=SUNDA, **V3**=CAYMAN, **V4/4+**=MARIANA (≡ MARIANA_PLUS, a single
shared column — [§7](#7-is-mariana_plus-a-distinct-isa-no)), **V5**=MAVERICK. Cells
show the hex value where present, `·` where absent. TONGA (V1) is **not** a per-row
column — its bytes *are* mappable onto this namespace (the V1 datapath bytes equal
the v2 bytes, [§6](#6-the-v1v2-transition)), but V1 uses a different *encoding* and
a different *opcode set*, so it is treated structurally in [§5](#5-tonga-v1-the-pre-unified-outlier),
not as a fifth column here.

> **TONGA (V1):** engine-scoped `TONGA_ISA_TPB_OPCODE` (44 entries), `byte = local
> \| (ENGINE<<5)`, in the separate `TongaArchIsa` package. Its compute-datapath
> bytes are inherited by v2 verbatim ([§6.1](#61-carried-forward-byte-identical-the-compute-datapath)); its control/SIM bytes
> are re-homed/dropped. See [§5](#5-tonga-v1-the-pre-unified-outlier).

The presence matrix below is the **union across v2..v5 = 172 distinct opcode names**
(including the `0xff` sentinel). `add`/`change`/`remove` are marked inline where they
occur at a generation boundary.

| VAL | OPCODE | V2 | V3 | V4/4+ | V5 | step note |
|---|---|---|---|---|---|---|
| 0x01 | LDWEIGHTS | 01 | 01 | 01 | 01 | invariant (from V1) |
| 0x02 | MATMUL | 02 | 02 | 02 | 02 | invariant (from V1) |
| 0x03 | PE_REG_WRITE | 03 | 03 | 03 | 03 | invariant |
| 0x04 | WEIGHT_MASK | 04 | 04 | 04 | 04 | invariant |
| 0x05 | WEIGHT_SHIFT | 05 | 05 | 05 | 05 | invariant |
| 0x06 | LDTAGS | · | 06 | 06 | 06 | **+v3** |
| 0x07 | MATMUL_SPARSE | · | 07 | 07 | 07 | **+v3** |
| 0x08 | PE_MANAGE_SEED | · | · | 08 | 08 | **+v4** |
| 0x09 | LDWEIGHTS_MX | · | · | 09 | 09 | **+v4** |
| 0x0a | MATMUL_MX | · | · | 0a | 0a | **+v4** |
| 0x21 | ACTIVATE | 21 | 21 | 21 | 21 | invariant (from V1) |
| 0x22 | ACTIVATE_QUANTIZE | 22 | 22 | 22 | 22 | invariant (from V1) |
| 0x23 | ACTIVATION_TABLE_LOAD | 23 | 23 | 23 | 23 | invariant |
| 0x24 | ACTIVATION_READ_ACCUMULATOR | 24 | 24 | 24 | 24 | invariant |
| 0x25 | ACTIVATE2 | · | · | 25 | 25 | **+v4** |
| 0x26 | ACTIVATE_MULTIPASS | · | · | · | 26 | **+v5** |
| 0x30 | EXPONENTIAL | 30 | 30 | 30 | 30 | invariant (low-traffic) |
| 0x41 | TENSOR_TENSOR_ARITH_OP | 41 | 41 | 41 | 41 | invariant (from V1) |
| 0x42 | TENSOR_REDUCE_ARITH_OP | 42 | 42 | 42 | 42 | invariant (from V1) |
| 0x43 | TENSOR_SCALAR_ARITH_OP | 43 | 43 | 43 | 43 | invariant (from V1) |
| 0x44 | TENSOR_SCALAR_PTR_ARITH_OP | 44 | 44 | 44 | 44 | invariant (from V1) |
| 0x45 | POOL | 45 | 45 | 45 | 45 | invariant (from V1; engine swapped, §6.4) |
| 0x46 | COPY | 46 | 46 | 46 | 46 | invariant (from V1) |
| 0x47 | CAST | 47 | 47 | 47 | 47 | invariant (from V1) |
| 0x48 | RECIPROCAL | 48 | 48 | 48 | 48 | invariant (from V1) |
| 0x49 | MEMSET | 49 | 49 | 49 | 49 | invariant (from V1) |
| 0x4a | REG_LOAD | 4a | 4a | 4a | 4a | invariant (from V1) |
| 0x4b | REG_STORE | 4b | 4b | 4b | 4b | invariant (from V1) |
| 0x4c | REG_SHUFFLE | 4c | 4c | 4c | 4c | invariant (from V1) |
| 0x4d | RNG | 4d | 4d | 4d | 4d | invariant (from V1) |
| 0x4e | TENSOR_CUMULATIVE_ARITH_OP | 4e | 4e | 4e | 4e | invariant (from V1) |
| 0x4f | TENSOR_SCALAR_PTR_MULTI_ARITH | 4f | 4f | 4f | 4f | invariant |
| 0x51 | TENSOR_TENSOR_BITVEC_OP | 51 | 51 | 51 | 51 | invariant (from V1) |
| 0x52 | TENSOR_REDUCE_BITVEC_OP | 52 | 52 | 52 | 52 | invariant (from V1) |
| 0x53 | TENSOR_SCALAR_BITVEC_OP | 53 | 53 | 53 | 53 | invariant (from V1) |
| 0x54 | TENSOR_SCALAR_PTR_BITVEC_OP | 54 | 54 | 54 | 54 | invariant (from V1) |
| 0x58 | MAX_POOL_SELECT | 58 | 58 | 58 | 58 | invariant |
| 0x5e | TENSOR_CUMULATIVE_BITVEC_OP | 5e | 5e | 5e | 5e | invariant (from V1) |
| 0x5f | TENSOR_SCALAR_PTR_MULTI_BITVEC | 5f | 5f | 5f | 5f | invariant |
| 0x60 | BATCH_NORM_STATS | 60 | 60 | 60 | 60 | invariant |
| 0x61 | BATCH_NORM_STATS2 | 61 | 61 | 61 | 61 | invariant |
| 0x62 | BATCH_NORM_AGGREGATE | 62 | 62 | 62 | 62 | invariant |
| 0x63 | BATCH_NORM_GRAD_ACCUMULATE | 63 | 63 | 63 | 63 | invariant |
| 0x64 | BATCH_NORM_PARAM_LOAD | 64 | 64 | 64 | 64 | invariant |
| 0x65 | BATCH_NORM_BACK_PROP | 65 | 65 | 65 | 65 | invariant |
| 0x66 | LOAD_PARAMETER_RAM | 66 | 66 | 66 | 66 | invariant |
| 0x67 | POOL_BUFFER_LOAD | 67 | 67 | 67 | 67 | invariant |
| 0x68 | GATHER | 68 | 68 | 68 | 68 | invariant |
| 0x69 | LOAD_MASK_SELECT | 69 | 69 | 69 | 69 | invariant |
| 0x6a | STREAM_SHUFFLE | 6a | 6a | 6a | 6a | invariant |
| 0x6b | STREAM_TRANSPOSE | 6b | 6b | 6b | 6b | invariant |
| 0x6c | MAX8 | 6c | 6c | 6c | 6c | invariant |
| 0x6d | MATCH_VALUE_LOAD | 6d | 6d | 6d | 6d | invariant |
| 0x6e | FIND_INDEX8 | 6e | 6e | 6e | 6e | invariant |
| 0x6f | MATCH_REPLACE8 | 6f | 6f | 6f | 6f | invariant |
| 0x70 | TENSOR_SCALAR_IMM_LD_ARITH | 70 | 70 | 70 | 70 | invariant |
| 0x71 | TENSOR_SCALAR_IMM_LD_BITVEC | 71 | 71 | 71 | 71 | invariant |
| 0x72 | COPY_PREDICATED | 72 | 72 | 72 | 72 | invariant |
| 0x73 | ROI_ALIGN | 73 | 73 | 73 | 73 | invariant |
| 0x74 | TENSOR_SCALAR_ADDR | 74 | 74 | 74 | 74 | invariant |
| 0x76 | RAND | 76 | 76 | 76 | 76 | invariant |
| 0x77 | RAND_GET_STATE | 77 | 77 | 77 | 77 | invariant |
| 0x78 | RAND_SET_STATE | 78 | 78 | 78 | 78 | invariant |
| 0x79 | EMBEDDING_UPDATE | 79 | 79 | 79 | 79 | invariant |
| 0x7a | LOAD_POOL_ARGUMENT | 7a | 7a | 7a | 7a | invariant |
| 0x7b | TENSOR_DEQUANTIZE | 7b | 7b | 7b | 7b | invariant |
| 0x7c | CROSS_LANE_REDUCE_ARITH | 7c | 7c | 7c | 7c | invariant |
| 0x7d | CROSS_LANE_REDUCE_BITVEC | 7d | 7d | 7d | 7d | invariant |
| 0x7e | IOTA | 7e | 7e | 7e | 7e | invariant |
| 0x7f | DROPOUT | 7f | 7f | 7f | 7f | invariant |
| 0x81 | JPEG_DECODE | · | 81 | 81 | 81 | **+v3** (fills a pre-existing hole — see CORRECTION) |
| 0x82 | TRANSPOSE_BATCH_NORM_STATS2 | 82 | 82 | 82 | 82 | invariant |
| 0x83 | TRANSPOSE_TENSOR_REDUCE_ARITH_OP | 83 | 83 | 83 | 83 | invariant |
| 0x84 | TRANSPOSE_TENSOR_REDUCE_BITVEC_OP | 84 | 84 | 84 | 84 | invariant |
| 0x85 | CUSTOM_OP_HEADER | 85 | 85 | 85 | 85 | invariant |
| 0x86 | CUSTOM_OP_PAYLOAD | 86 | 86 | 86 | 86 | invariant |
| 0x87 | TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH | 87 | · | · | · | **−v3 (abandoned)** |
| 0x88 | TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC | 88 | · | · | · | **−v3 (abandoned)** |
| 0x8a | TENSOR_TENSOR_ADD_BF16 | 8a | · | · | · | **−v3 (abandoned)** |
| 0x8b | TENSOR_TENSOR_MULT_BF16 | 8b | · | · | · | **−v3 (abandoned)** |
| 0x8c | TENSOR_REDUCE_ADD_BF16 | 8c | · | · | · | **−v3 (abandoned)** |
| 0x8d | TENSOR_REDUCE_MAX_BF16 | 8d | · | · | · | **−v3 (abandoned)** |
| 0x8e | BATCH_NORM_PARAM_LOAD2 | 8e | 8e | 8e | 8e | **survives** (sandwiched, not retired) |
| 0x8f | TENSOR_TENSOR_SUB_BF16 | 8f | · | · | · | **−v3 (abandoned)** |
| 0x92 | TENSOR_SCALAR_AFFINE_SELECT | 92 | 92 | 92 | 92 | invariant |
| 0x93 | TRANSPOSE_TENSOR_SCALAR_ARITH_OP | 93 | 93 | 93 | 93 | invariant |
| 0x94 | BATCH_NORM_GRAD_ACCUMULATE2 | 94 | 94 | 94 | 94 | invariant |
| 0x95 | MODIFY_POOL_CONFIG | 95 | 95 | 95 | 95 | invariant |
| 0x96 | SORT | 96 | 96 | 96 | 96 | invariant |
| 0x98 | TENSOR_SCALAR_SELECT | 98 | 98 | 98 | 98 | invariant |
| 0x99 | CAST_PREDICATED | 99 | 99 | 99 | 99 | invariant |
| 0x9a | TENSOR_SCALAR_CACHE_REDUCE | 9a | 9a | 9a | 9a | invariant |
| 0x9b | DVE_READ_ACCUMULATOR | 9b | 9b | 9b | 9b | invariant |
| 0x9c | TENSOR_REDUCE_RANGE_CHECK | 9c | 9c | 9c | 9c | invariant |
| 0x9d | SCALAR_TENSOR_TENSOR_ARITH | 9d | 9d | 9d | 9d | invariant |
| 0x9e | SCALAR_TENSOR_TENSOR_BITVEC | 9e | 9e | 9e | 9e | invariant |
| 0x9f | ENGINE_NOP | 9f | 9f | 9f | 9f | invariant |
| 0xa0 | EVENT_SEMAPHORE | a0 | a0 | a0 | a0 | invariant (v2 control redesign, §6.2) |
| 0xa1 | HALT | a1 | a1 | a1 | a1 | invariant |
| 0xa2 | DRAIN | a2 | a2 | a2 | a2 | invariant |
| 0xa3 | INSTRUCTION_FLUSH | a3 | a3 | a3 | a3 | invariant |
| 0xa4 | NOP | a4 | a4 | a4 | a4 | invariant (re-homed from V1 0x68, §6.2) |
| 0xa5 | WRITE | a5 | a5 | a5 | a5 | invariant (re-homed from V1 0x69, §6.2) |
| 0xa6 | NOTIFY | a6 | a6 | a6 | a6 | invariant (re-homed from V1 0x6a, §6.2) |
| 0xa7 | MOVE | a7 | a7 | a7 | a7 | invariant |
| 0xa8 | ALU_OP | a8 | a8 | a8 | a8 | invariant |
| 0xa9 | COMPARE_BRANCH | a9 | a9 | a9 | a9 | invariant |
| 0xaa | TENSOR_LOAD | aa | aa | aa | aa | invariant |
| 0xab | TENSOR_STORE | ab | ab | ab | ab | invariant |
| 0xb0 | EVENT_SEMAPHORE_RANGE_CLEAR | b0 | b0 | b0 | b0 | invariant |
| 0xb1 | SET_ORDERING_MODE | b1 | b1 | b1 | b1 | invariant |
| 0xb2 | MOVE_SHAPE | b2 | b2 | b2 | b2 | invariant |
| 0xb3 | POLL_SEM | b3 | b3 | b3 | b3 | invariant |
| 0xb4 | TEST_EVENT_SEM | · | · | b4 | b4 | **+v4** |
| 0xb5 | BRANCH_PREFETCH_HINT | b5 | b5 | b5 | b5 | invariant |
| 0xb6 | COMPACT_CONTROL_INST | · | · | · | b6 | **+v5** |
| 0xb8 | DMAMEMCPY | b8 | b8 | b8 | b8 | invariant |
| 0xb9 | DMA_MEMCPY2 | · | · | · | b9 | **+v5** |
| 0xba | DMA_IMMEDIATE | · | · | · | ba | **+v5** |
| 0xbb | DMA_INDIRECT | bb | bb | bb | bb | invariant |
| 0xbc | RANGE_SELECT | · | bc | bc | bc | **+v3** |
| 0xbd | DMA_TRANSPOSE | · | bd | bd | bd | **+v3** |
| 0xbe | GET_SEQUENCE_BOUNDS | · | be | be | be | **+v3** |
| 0xbf | SB2SB_COLLECTIVE | · | bf | bf | bf | **+v3** |
| 0xc1 | PSEUDO_DMATRIGGER | c1 | c1 | c1 | c1 | invariant (pseudo band, from V1 RT) |
| 0xc2 | PSEUDO_DMAREARM | c2 | c2 | c2 | c2 | invariant |
| 0xc3 | PSEUDO_DMABARRIER | c3 | c3 | c3 | c3 | invariant |
| 0xc4 | PSEUDO_DMAMEMCPY_FULL_IND | c4 | c4 | c4 | c4 | invariant |
| 0xc5 | PSEUDO_SEMAPHORE_SET | c5 | c5 | c5 | c5 | invariant (from V1 0xc5) |
| 0xc6 | PSEUDO_LOAD_ACT_FUNC_SET | c6 | c6 | c6 | c6 | invariant (from V1 0xc6) |
| 0xc7 | PSEUDO_TRIGGER_ALL_REDUCE | c7 | c7 | c7 | c7 | invariant |
| 0xc8 | PSEUDO_TRIGGER_COLLECTIVE | c8 | c8 | c8 | c8 | invariant |
| 0xc9 | PSEUDO_READ_VAR_ADDR | c9 | c9 | c9 | c9 | invariant (re-homed from V1 0xc8, §6.2) |
| 0xca | PSEUDO_EMBEDDING_UPDATE | ca | ca | ca | ca | invariant |
| 0xcb | PSEUDO_SEND_RECV | cb | cb | cb | cb | invariant |
| 0xcc | PSEUDO_BRANCH_LABEL | cc | cc | cc | cc | invariant |
| 0xcd | PSEUDO_TENSOR_STORE | cd | cd | cd | cd | invariant |
| 0xce | PSEUDO_TENSOR_LOAD | ce | ce | ce | ce | invariant |
| 0xcf | PSEUDO_DMASWAP_QUEUE_SET | cf | cf | cf | cf | invariant |
| 0xd0 | PSEUDO_SET_RNG_SEED | d0 | d0 | d0 | d0 | invariant |
| 0xd1 | PSEUDO_FUNCTION_BEGIN | d1 | d1 | d1 | d1 | invariant |
| 0xd2 | PSEUDO_FUNCTION_RETURN | d2 | d2 | d2 | d2 | invariant |
| 0xd3 | PSEUDO_FUNCTION_CALL | d3 | d3 | d3 | d3 | invariant |
| 0xd4 | PSEUDO_DMA_DIRECT2D | d4 | d4 | d4 | d4 | invariant |
| 0xd5 | PSEUDO_SYNC_BARRIER | d5 | d5 | d5 | d5 | invariant |
| 0xd6 | PSEUDO_RANGE_CHECK | d6 | d6 | d6 | d6 | invariant |
| 0xd7 | PSEUDO_JPEG_DECODE | · | d7 | d7 | d7 | **+v3** |
| 0xd8 | PSEUDO_CORE_BARRIER | d8 | d8 | d8 | d8 | invariant |
| 0xd9 | PSEUDO_TRIGGER_COLLECTIVE2 | d9 | d9 | d9 | d9 | invariant |
| 0xda | PSEUDO_EXTENSION | da | da | da | da | invariant |
| 0xdb | PSEUDO_CUR_PROCESSING_RANK_ID | db | db | db | db | invariant |
| 0xdc | PSEUDO_GID_LOAD | dc | dc | dc | dc | invariant |
| 0xdd | PSEUDO_BRANCH_PREFETCH_HINT | dd | dd | dd | dd | invariant |
| 0xde | PSEUDO_TENSOR_COMPLETION | de | de | de | de | invariant |
| 0xdf | PSEUDO_INST | df | df | df | df | invariant |
| 0xe0 | SPARSITY_COMPRESS | · | · | e0 | e0 | **+v4** |
| 0xe1 | SPARSITY_COMPRESS_TAG | · | · | e1 | e1 | **+v4** |
| 0xe2 | RAND2 | · | · | e2 | e2 | **+v4** |
| 0xe3 | QUANTIZE_MX | · | · | e3 | e3 | **+v4** (binds DVE — see §3) |
| 0xe4 | CONV_LUT_LOAD | · | e4 | e4 | e4 | **+v3** |
| 0xe5 | TENSOR_TENSOR_SCAN_ARITH | e5 | e5 | e5 | e5 | invariant |
| 0xe6 | TENSOR_SCALAR_CACHE_CUMULATIVE | e6 | e6 | e6 | e6 | invariant |
| 0xe7 | INDIRECT_COPY | e7 | e7 | e7 | e7 | invariant |
| 0xe8 | COPY_PREDICATED_SCALAR | e8 | e8 | e8 | e8 | invariant |
| 0xe9 | DVE_READ_INDICES | · | e9 | e9 | e9 | **+v3** |
| 0xea | SELECT_REDUCE | ea | ea | ea | ea | invariant |
| 0xf0 | EXTENDED_INST | f0 | f0 | f0 | f0 | invariant (customer-specific space) |
| 0xf1 | DMA_GATHER_TRANSPOSE | · | f1 | f1 | f1 | **+v3** |
| 0xf2 | NONZERO_WITH_COUNT | · | f2 | f2 | f2 | **+v3** |
| 0xf3 | TENSOR_TENSOR_INT_WIDE | · | · | · | f3 | **+v5** |
| 0xf4 | TENSOR_SCALAR_INT_WIDE | · | · | · | f4 | **+v5** |
| 0xff | INVALID (sentinel) | ff | ff | ff | ff | invariant |

**Matrix dimensions: 172 opcode rows × 4 generation columns (V2/V3/V4-4+/V5).**
Per-column live totals (enum-body entries, sentinel included): **V2 = 145, V3 = 150,
V4 ≡ V4+ = 159, V5 = 165.**

> **CORRECTION (to GEN-04 §2.1 / §5.3).** GEN-04 states JPEG_DECODE (`0x81`,
> +v3) *"reuses a byte freed by a sunda removal"* and calls `0x81` *"the only reused
> freed byte."* **That is wrong.** In the SUNDA opcode enum the sequence steps
> directly from `DROPOUT = 0x7f` (`sunda common.h:215`) to
> `TRANSPOSE_BATCH_NORM_STATS2 = 0x82` (`:216`) — **`0x80` and `0x81` are both holes,
> never occupied by any SUNDA opcode** (and both stay holes in all four gens). So
> CAYMAN's `JPEG_DECODE = 0x81` (`cayman common.h:219`) fills a **pre-existing free
> byte**, not a freed-and-reused one. The corrected invariant is **stronger**: v2→v3
> performs **zero byte-reuse** — all 12 CAYMAN additions claim genuinely-free bytes,
> and all 7 retired SUNDA bytes are permanently abandoned.

---

## 3. The per-step diff narrative

The lineage net-growth is `145 −(7) +(12) → 150 +(9) → 159 +(6) → 165`. Exactly one
step has removals (the v2→v3 re-baseline); v3→v4 and v4→v5 are strictly additive.

### 3.1 SUNDA (v2) → CAYMAN (v3): 145 → 150 (+12 / −7)

The only step in the lineage with **removals**. It is a re-baselining: seven
SUNDA-era ops are retired and twelve image/sparse/collective/codec ops land.

**ADDED in CAYMAN (12)** — all claim **fresh** (previously-unoccupied) bytes:
`LDTAGS 0x06`, `MATMUL_SPARSE 0x07` (PE low range); `JPEG_DECODE 0x81`,
`PSEUDO_JPEG_DECODE 0xd7` (codec); `RANGE_SELECT 0xbc`, `DMA_TRANSPOSE 0xbd`,
`GET_SEQUENCE_BOUNDS 0xbe`, `SB2SB_COLLECTIVE 0xbf` (DMA/collective cluster);
`CONV_LUT_LOAD 0xe4`, `DVE_READ_INDICES 0xe9`, `DMA_GATHER_TRANSPOSE 0xf1`,
`NONZERO_WITH_COUNT 0xf2` (high cluster).

**REMOVED at CAYMAN (7)** — the only deletions in v2..v5, all **abandoned** (never
reused v3..v5):

| VAL | SUNDA-only opcode | SUNDA tag |
|---|---|---|
| 0x87 | TENSOR_SCALAR_PTR_MULTI_DUAL_ARITH | `// n, ucode/kaenadve exists, not maintained/used` |
| 0x88 | TENSOR_SCALAR_PTR_MULTI_DUAL_BITVEC | `// n, ucode/kaenadve exists, not maintained/used` |
| 0x8a | TENSOR_TENSOR_ADD_BF16 | `// Y` (live in v2, retired at v3) |
| 0x8b | TENSOR_TENSOR_MULT_BF16 | `// Y` |
| 0x8c | TENSOR_REDUCE_ADD_BF16 | `// Y` |
| 0x8d | TENSOR_REDUCE_MAX_BF16 | `// Y` |
| 0x8f | TENSOR_TENSOR_SUB_BF16 | `// Y` |

The freed band is `{0x87,0x88,0x8a,0x8b,0x8c,0x8d,0x8f}`; the **sandwiched
`0x8e BATCH_NORM_PARAM_LOAD2` survives all five gens** (it is *not* in the retired
band). The dual-pointer multi ops were already deprecated in SUNDA (`// n`); the
BF16-fused arithmetic ops were live in v2 (`// Y`) and superseded at the v3
re-baseline by the generic `TensorTensor`/`TensorScalar`/`TensorReduce` arith ops
carrying a dtype field. Anchors: `sunda common.h:221–228`.

> **GOTCHA — forensic value of the abandoned band.** Because the seven freed bytes
> are **never reused** in v3/v4/v5, a stray `0x87/0x88/0x8a/0x8b/0x8c/0x8d/0x8f` in a
> v3+ instruction stream is a **decode error or a v2 artifact**, never a live v3+
> instruction. Likewise `0x80`/`0x81` (with `0x81` taken by JPEG_DECODE only from v3)
> are SUNDA holes. A decoder validating against the wrong generation will mis-flag
> these.

**Second-source cross-check.** The per-gen `instruction_mapping.json` `struct2opcode`
(the wire-encodable subset) goes **sunda 89 → cayman 99 (+10)**: of the 12 added, 10
carry a new encodable struct; the two pseudo-form additions (`PSEUDO_JPEG_DECODE`,
plus the pseudo accounting) sit in the pseudo band. `[HIGH/OBSERVED on the 89→99
totals; MED/INFERRED on the exact per-op struct accounting]`

### 3.2 CAYMAN (v3) → MARIANA (v4): 150 → 159 (+9 / −0)

**Pure-additive.** MARIANA introduces the microscaling (MX) PE path, the
sparsity-compress path, a second-gen RNG, the PE seed manager, ACTIVATE2, and an
event-semaphore test op:

`PE_MANAGE_SEED 0x08`, `LDWEIGHTS_MX 0x09`, `MATMUL_MX 0x0a` (PE family, next free low
bytes after SUNDA's `0x01–0x07`); `ACTIVATE2 0x25` (after the `0x21–0x24` ACT
cluster); `TEST_EVENT_SEM 0xb4` (between `POLL_SEM 0xb3` and `BRANCH_PREFETCH_HINT
0xb5`); `SPARSITY_COMPRESS 0xe0`, `SPARSITY_COMPRESS_TAG 0xe1`, `RAND2 0xe2`,
`QUANTIZE_MX 0xe3` (the MX/sparse cluster, just below `CONV_LUT_LOAD 0xe4`). None of
the seven v2→v3-freed bytes are touched. `struct2opcode: cayman 99 → mariana 108
(+9)` — the cleanest cross-source confirmation in the lineage (all nine are
encodable, none pseudo).

> **NOTE — `QUANTIZE_MX 0xe3` binds DVE, not POOL.** `QUANTIZE_MX` is the MX-quantize
> handler that runs on the **DVE** engine; it is **absent from the POOL kernel-info
> tables** (the named POOL QuantizeMx handler count drops 60→59 when it is moved out).
> The POOL MX surface is `TENSOR_DEQUANTIZE 0x7b`, not `0xe3`. See the [Cross-Gen
> Kernel-Info Matrix](../images/cross-gen-kernel-info-matrix.md) for the per-engine
> KIT binding.

### 3.3 MARIANA (v4) → MAVERICK (v5): 159 → 165 (+6 / −0)

**Pure-additive.** MAVERICK adds the compact-control instruction, the DMA
immediate/2D-copy fast forms, a second copy-memcpy, a multipass activate, and the
INT_WIDE tensor ops:

`ACTIVATE_MULTIPASS 0x26` (after MARIANA's `ACTIVATE2 0x25`); `COMPACT_CONTROL_INST
0xb6`, `DMA_MEMCPY2 0xb9`, `DMA_IMMEDIATE 0xba` (control/DMA cluster, around the
existing `0xb5`/`0xb8`/`0xbb`); `TENSOR_TENSOR_INT_WIDE 0xf3`, `TENSOR_SCALAR_INT_WIDE
0xf4` (after MARIANA-era `0xf1/0xf2`). `struct2opcode: mariana 108 → maverick 114
(+6)`, all six encodable. Anchors: `maverick common.h:173, 266, 268, 269, 320, 321`.
`[HIGH/OBSERVED — header values OBSERVED; v5 interior semantics INFERRED]`

> **GOTCHA — slot-policy is topical, not sequential.** New ops do **not** simply take
> the next free byte globally; they take the next free byte **in a topical cluster**
> (PE family low, ACT 0x2x, control/DMA 0xb*, high cluster 0xe*/0xf*). This is why the
> additions are interleaved with invariant rows, and why the abandoned `0x8x` band is
> *never* backfilled even though plenty of high-numbered bytes were free. The byte's
> high nibble is a soft engine-affinity hint, not a hard field (in v2+; in V1 it was a
> hard 3-bit field — [§5.2](#52-the-opcode-construction-rule-engine--local)). `[MED/INFERRED]`

---

## 4. The value-stability proof

> **Every opcode present in two generations carries the identical hex value in
> both.** The pairwise value-mismatch join is **empty** for `sunda↔cayman`,
> `cayman↔mariana`, `mariana↔maverick`, **and the full `sunda↔maverick` span** (138
> common opcodes, **zero** mismatches). The opcode byte is a **write-once global
> namespace**: new generations only append, removals only abandon, nothing is ever
> renumbered.

A representative cross-range sample, value-identical across **all** of V2/V3/V4/V5
(verified directly from the four enum bodies):

| OPCODE | V2 | V3 | V4 | V5 |
|---|---|---|---|---|
| LDWEIGHTS | 01 | 01 | 01 | 01 |
| MATMUL | 02 | 02 | 02 | 02 |
| POOL | 45 | 45 | 45 | 45 |
| COPY | 46 | 46 | 46 | 46 |
| CAST | 47 | 47 | 47 | 47 |
| BATCH_NORM_PARAM_LOAD2 | 8e | 8e | 8e | 8e |
| DMAMEMCPY | b8 | b8 | b8 | b8 |
| PSEUDO_DMATRIGGER | c1 | c1 | c1 | c1 |
| EXTENDED_INST | f0 | f0 | f0 | f0 |
| INVALID | ff | ff | ff | ff |

The strong form is the `sunda↔maverick` (full-span) check: every one of the ~138
opcodes that survive from v2 to v5 keeps its **v2 byte unchanged through three
generation steps**. This is the gen-invariance evidence that lets a reimplementer
hard-code one opcode→handler table and parameterize only the *presence set* per
generation (see [The Gen-Invariance Thesis](../orientation/gen-invariance.md)). And
the invariant's **first link is V1→V2** — the entire V1 compute-datapath byte
namespace is inherited verbatim by SUNDA ([§6.1](#61-carried-forward-byte-identical-the-compute-datapath)).

The **union** across v2..v5 is **172 distinct opcode names**; the **live** set is
**165** (the MAVERICK superset). `172 − 7 (the SUNDA-only retired ops) = 165`. Every
live op is present in MAVERICK, which is a strict superset of `{CAYMAN ∪ MARIANA}`.
On the opcode axis the chain is `SUNDA′ ⊊ CAYMAN ⊊ {MARIANA ≡ MARIANA_PLUS} ⊊
MAVERICK` (where `SUNDA′` = SUNDA minus the 7 retired).

> **NOTE — opcode axis ≠ register/CSR axis.** On the **register/CSR** axis the
> inclusion is `SUNDA ⊂ {CAYMAN ≡ MARIANA ≡ MARIANA_PLUS} ⊂ MAVERICK` (CAYMAN and
> MARIANA share a CSR schema). On the **opcode** axis CAYMAN ⊊ MARIANA strictly
> (MARIANA *adds* 9 opcodes). The `≡` between MARIANA and MARIANA_PLUS holds on
> **both** axes; the `≡` between CAYMAN and MARIANA holds **only** on the CSR axis.
> The two are not contradictory — they describe different axes.

---

## 5. TONGA (V1): the pre-unified outlier

TONGA is **NeuronCore-v1** — the first-generation Trn1/Inf1-era Tensor-Processing
Block (TPB). It is **outside** the GPSIMD family: it has no coretype, no `arch_id`,
no NCFW firmware image, no EXTISA blob, and **zero** runtime strings; it lives only
in legacy ISA/register headers, and the SPIS Product-ID register places it one
generation *before* SUNDA (Tonga `0x01` < Sunda `0x02` < Cayman `0x03`). See [The
Codename → Generation Map](./codename-generation-map.md) for its identity placement.
This section recovers what V1's ISA actually *is*.

> **CORRECTION (to GEN-04 §1.2 / §7).** GEN-04 reports TONGA as *"register-block
> RTL only, no opcode enum,"* and marks the matrix TONGA column `n/a`. That reading is
> correct **only for the RTL register tree** `arch-headers/tonga/` (cmake
> `TongaArchHeaders`, 215 files). It is **incomplete**: there is a *second* TONGA tree
> — `arch-isa/aws_tonga_isa_*` (cmake `TongaArchIsa`, copyright 2018) — which **does**
> ship a real `typedef enum TONGA_ISA_TPB_OPCODE` (44 entries) plus the 64-byte
> instruction-struct family and a separate SP sequencer ISA. V1 has a structured
> opcode/struct ISA; it simply uses an **engine-scoped** encoding rather than the
> unified flat namespace. The `n/a` is right for the *flat* axis but V1's bytes **are**
> mappable onto it ([§6.1](#61-carried-forward-byte-identical-the-compute-datapath)).

### 5.1 The V1 NeuronCore geometry

`TONGA_ISA_TPB_ARCH_CONSTS` (`arch-isa/tpb/aws_tonga_isa_tpb_common.h:13–37`) — the
classical fixed-function TPB:

| Const | Value | Meaning |
|---|---|---|
| `INST_NBYTES` | 64 | every TPB instruction is 64 bytes |
| `INST_NWORDS` | 16 | ⇒ 4-byte instruction words |
| `PE_ARRAY_NUM_ROWS × NUM_COLS` | 128 × 64 | the systolic MatMul array |
| `POOLING_NUM_CHANNELS` | 64 | pooling engine width |
| `ACTIVATION_NUM_CHANNELS` | 64 | activation engine width |
| `STATE_BUF_NUM_PARTITIONS` | 128 (mid = 64) | 8B word, 128 KB/partition (96 KB active) |
| `PSUM_BUF_NUM_PARTITIONS / BANKS` | 64 / 4 | 16 KB/partition (8 KB active), 2 KB/bank |
| `MEM_PATTERN_MAX_NUM_DIM` | 4 | 1D–4D access patterns |
| `NUM_EVENTS / NUM_SEMAPHORES` | 256 / 32 | sync resources; semaphore max 65535 |

This is a PE-array + ACT + POOL over SB/PSUM. **There is no 8-core Q7-POOL DSP
cluster, no IRAM/DRAM Q7 apertures, no Vision-Q7 microarch** — those are the v2+
GPSIMD additions ([§7](#7-the-gpsimd-genesis-answer)).

### 5.2 The opcode construction rule: engine ‖ local

V1's opcode namespace is **engine-scoped**: the engine field is *encoded in* the
opcode byte. From `aws_tonga_isa_tpb_common.h:40–48, 65–67`:

```c
typedef enum TONGA_ISA_TPB_ENGINE {            // :40
    TONGA_ISA_TPB_ENGINE_PE      = 0x0,
    TONGA_ISA_TPB_ENGINE_ACT     = 0x1,
    TONGA_ISA_TPB_ENGINE_POOL    = 0x2,
    TONGA_ISA_TPB_ENGINE_ALL     = 0x3,
    TONGA_ISA_TPB_ENGINE_RT      = 0x6,        // instructions patched by the runtime
    TONGA_ISA_TPB_ENGINE_SIM     = 0x7,        // simulator-only, not in hardware
    TONGA_ISA_TPB_ENGINE_INVALID = 0xFF
} TONGA_ISA_TPB_ENGINE;

typedef enum TONGA_ISA_TPB_OPCODE {            // :65
    TONGA_ISA_TPB_OPCODE_LDWEIGHTS = 0x01 | (TONGA_ISA_TPB_ENGINE_PE   << 5),  // :67 ⇒ 0x01
    TONGA_ISA_TPB_OPCODE_POOL      = 0x05 | (TONGA_ISA_TPB_ENGINE_POOL << 5),  // :87 ⇒ 0x45
    TONGA_ISA_TPB_OPCODE_NOP       = 0x08 | (TONGA_ISA_TPB_ENGINE_ALL  << 5),  // :106 ⇒ 0x68
    /* ... */
} TONGA_ISA_PACKED TONGA_ISA_TPB_OPCODE;
```

**The 1-byte opcode is `[ engine:3 (bits 7..5) ‖ local_op:5 (bits 4..0) ]`.**
DECODE: `engine = opcode >> 5`, `local = opcode & 0x1f`. The byte *carries
structure*. The unified v2+ `NEURON_ISA_TPB_OPCODE` **inherits the byte values but
drops the field semantics** — the engine becomes an out-of-band dispatch property,
not a bitfield, and the byte is frozen as a flat write-once key. **This is the
genesis of the "stable global opcode byte" that [§4](#4-the-value-stability-proof)
proves invariant v2..v5.**

A further structured field survives inside the encoding: within the POOL engine the
**`0x10` local bit** is the arith/bitvec discriminator. E.g.
`TENSOR_TENSOR_ARITH_OP` is `local 0x01 → byte 0x41`, while
`TENSOR_TENSOR_BITVEC_OP` is `local 0x11 → byte 0x51`
(`aws_tonga_isa_tpb_common.h:75–76`). The unified enum preserves both bytes (0x41,
0x51) but no longer documents the bit as a field.

### 5.3 The 44-entry V1 roster (byte = local ‖ engine<<5)

| engine (`<<5`) | bytes | opcodes |
|---|---|---|
| PE (`0x00`) | 0x01–0x02 | LDWEIGHTS, MATMUL |
| ACT (`0x20`) | 0x21–0x22 | ACTIVATE, ACTIVATE_QUANTIZE |
| POOL (`0x40`) | 0x41–0x4e arith; 0x51–0x5e bitvec | TENSOR_TENSOR/REDUCE/SCALAR/SCALAR_PTR/CUMULATIVE (arith+bitvec twins), POOL 0x45, COPY 0x46, CAST 0x47, RECIPROCAL 0x48, MEMSET 0x49, REG_LOAD 0x4a, REG_STORE 0x4b, REG_SHUFFLE 0x4c, RNG 0x4d |
| ALL (`0x60`) | 0x61–0x6a | EVENT_WAIT 0x61, EVENT_SET 0x62, EVENT_CLEAR 0x63, SEMAPHORE_TEST_AND_SET 0x65, NOP 0x68, WRITE 0x69, NOTIFY 0x6a |
| RT (`0xc0`) | 0xc1–0xc8 | PSEUDO_DMA_TRIGGER 0xc1, …_REARM 0xc2, …_BARRIER 0xc3, …_MEMCPY 0xc4, PSEUDO_SEMAPHORE_SET 0xc5, PSEUDO_LOAD_ACT_FUNC_SET 0xc6, PSEUDO_SET_TRANSFER_ADJUST 0xc7, PSEUDO_READ_VAR_ADDR 0xc8 |
| SIM (`0xe0`) | 0xeb–0xef | SIM_DMA_COPY 0xeb, SIM_WRFILE 0xec, SIM_WRNPY 0xed, SIM_RDNPY 0xee, SIM_MEMCPY 0xef |
| — | 0xff | INVALID |

(RT ops are runtime-patched placeholders, "never executed by H/W"; SIM ops are
simulator-only.)

### 5.4 The 64-byte `TONGA_ISA_TPB` instruction-struct family

V1's instruction format is a **64-byte fixed record** (the v2+ model is a flat
operand-struct keyed off the opaque opcode; V1's is engine-scoped and self-validating
per instruction). The umbrella `aws_tonga_isa_tpb.h` pulls in **39** struct headers:
**25 compute/ALL** (always), **8 RT-pseudo**, **5 SIM-only** (under `#ifdef
SIM_ONLY`). Every compute struct `STATIC_ASSERT`s to `INST_NBYTES = 64`, and each
ships a `tonga_isa_tpb_<op>_check_validity()` — this is a **wire-format-exact host
encoder/validator library**, not an internal model.

**The shared 8-byte prefix** (every TPB instruction):

```
TONGA_ISA_TPB_INST_HEADER (4B, common.h:214–226):
    +0 opcode(1)  +1 inst_word_len(1)  +2 debug_cmd(1)  +3 debug_hint(1)
TONGA_ISA_TPB_INST_EVENTS (4B, common.h:229–235):
    +0 wait_event_mode(1)  +1 wait_event_idx(1)  +2 set_event_mode(1)  +3 set_event_idx(1)
```

The embedded per-instruction wait-on-event / set-on-done synchronization is part of
**every** compute instruction — V1's dataflow sync model. The memory-access patterns
`MEM_ACCESS_1D/2D/3D/4D` are `8/12/16/20` bytes (`{ tpb_addr start_addr; int16
step_elem[N]; uint16 num_elem[N]; }`, i.e. `4 + 4N`; `common.h:246–269`).

**Exemplar — `TONGA_ISA_TPB_COPY_INST` (64 B, opcode `0x46`, POOL engine)**
(`aws_tonga_isa_tpb_copy.h:14–49`):

| off | size | field | type |
|---|---|---|---|
| 0x00 | 4 | inst_header | INST_HEADER |
| 0x04 | 4 | inst_events | INST_EVENTS |
| 0x08 | 1 | dtype | TONGA_ISA_TPB_DTYPE |
| 0x09 | 3 | reserved_0[3] | uint8_t |
| 0x0c | 20 | src_mem_pattern | MEM_ACCESS_4D |
| 0x20 | 20 | dst_mem_pattern | MEM_ACCESS_4D |
| 0x34 | 1 | num_active_channels | uint8_t |
| 0x35 | 11 | reserved[11] | uint8_t |
| **0x40** | | total | `STATIC_ASSERT(sizeof == INST_NBYTES)` (`:49`) |

Header doc: *"Copy … done through the pooling engine."*

**Exemplar — `TONGA_ISA_TPB_MATMUL_INST` (64 B, opcode `0x02`, PE engine)**
(`aws_tonga_isa_tpb_matmul.h:96–197`): `inst_header(4) + inst_events(4) +
in_dtype(1) + perf_opt(1: PE_PERF_OPT_MODE) + psum_accumulate_flags(1) +
ifmap_replication_{resolution,shift_amnt,num_rows}(3) + quant_offset union(2) +
src/dst MEM_ACCESS_3D(16+16) + num_active_rows(1) + num_active_cols(1) +
reserved[14] == 64`. Math (doc `:10`): `Psum_out = (Xq−Xqz)·(Wq−Wqz) + Psum_in` over
the 128×64 array — native asymmetric UINT8/UINT16 quantization with zero-points; UINT8
`DOUBLE_ROW/COLUMN/PIXEL` 2×/cycle perf modes. **Note MatMul uses MEM_ACCESS_3D (16 B)
while COPY/POOL use MEM_ACCESS_4D (20 B).**

### 5.5 The SP sequencer ISA (separate, 8-byte, opcode:7 + phase:1)

The Sequencer (SP) is a **separate** instruction set with its own encoding (8-byte
instructions: `SP_INST_NBYTES = 8`, `_NWORDS = 2`). The instruction header is a
bitfield `{ opcode:7; p:1 }` (`aws_tonga_isa_sp_common.h:45–48`), masked
`OPCODE_MASK = 0x7F` / `P_MASK = 0x80` (`aws_tonga_isa_sp_instruction_fields.h`). The
**16** SP opcodes (`aws_tonga_isa_sp_instruction_opcodes.h:9–24`):

| op | val | op | val | op | val | op | val |
|---|---|---|---|---|---|---|---|
| WRITE8_IND | 0x04 | WAIT_CYCLES | 0x20 | BRANCH_C_EVENT_SET | 0x34 | DECR_CNTR | 0x41 |
| WRITE32_IND | 0x05 | WAIT_EVENT_SET | 0x24 | BRANCH_C_CNTR_Z | 0x38 | CLR_ALL_EVENTS | 0x50 |
| WRITE8_DIR | 0x08 | WAIT_EVENT_SET_M | 0x25 | BRANCH_C_CNTR_NZ | 0x39 | CLR_EVENT | 0x51 |
| NOTIFY_HALT | 0x10 | BRANCH_U | 0x30 | SET_CNTR | 0x40 | SET_AR | 0x60 |

The SP is V1's control-flow / event / counter / branch engine — the on-chip program
sequencer that drives the TPB engines, with full 64-bit wire-field diagrams in
`sp_isa.txt`.

### 5.6 The V1 dtype family

`TONGA_ISA_TPB_DTYPE` (1-byte packed, `common.h:51–62`) is the **distinct name
family** (`TONGA_ISA_TPB_DTYPE_*` vs the unified `NEURON_ISA_TPB_DTYPE_*`) that marks
V1 as the older arch — but the byte-width-encoded *values* carry forward into the v2+
dtype family: `INVALID 0x0, UINT8 0x3, UINT16 0x5, BFLOAT16 0x6, FP16 0x7, INT32 0x8,
FP32 0xA, INT64 0xC`.

---

## 6. The V1→V2 transition

Computed by joining V1's `TONGA_ISA_TPB_OPCODE` bytes (`engine<<5 | local`) against
the unified SUNDA `NEURON_ISA_TPB_OPCODE`. Of V1's 44 entries, 30 share a *name* with
SUNDA; of those, **26 are byte-identical** and **4 are re-homed**; **14 are V1-only**.

### 6.1 Carried forward byte-identical — the compute datapath

The **entire V1 compute-datapath byte namespace** is inherited verbatim by V2:

- **PE:** LDWEIGHTS `0x01`, MATMUL `0x02`.
- **ACT:** ACTIVATE `0x21`, ACTIVATE_QUANTIZE `0x22`.
- **POOL (22 ops):** the whole `0x41–0x5e` block — TENSOR_TENSOR/REDUCE/SCALAR/
  SCALAR_PTR/CUMULATIVE (arith `0x41–0x4e` + bitvec `0x51–0x5e`), POOL `0x45`,
  COPY `0x46`, CAST `0x47`, RECIPROCAL `0x48`, MEMSET `0x49`, REG_LOAD/STORE/SHUFFLE
  `0x4a–0x4c`, RNG `0x4d`.
- **PSEUDO:** PSEUDO_SEMAPHORE_SET `0xc5`, PSEUDO_LOAD_ACT_FUNC_SET `0xc6`.
- **SENTINEL:** INVALID `0xff`.

**This is the birth of the write-once opcode byte** that [§4](#4-the-value-stability-proof)
proves invariant v2..v5 — the invariant's first link is established at V1→V2.

### 6.2 Re-homed — the control/sync block moves out of `ENGINE_ALL`

V1 put cross-engine control ops in the `ENGINE_ALL` (`3<<5 = 0x60`) band; V2 rebuilt
control/sync into the `0xa0–0xab` cluster and replaced the engine-scoped event ops
with the unified `EVENT_SEMAPHORE` family:

| V1 op | V1 byte | V2 op | V2 byte |
|---|---|---|---|
| NOP | 0x68 | NOP | **0xa4** |
| WRITE | 0x69 | WRITE | **0xa5** |
| NOTIFY | 0x6a | NOTIFY | **0xa6** |
| PSEUDO_READ_VAR_ADDR | 0xc8 | PSEUDO_READ_VAR_ADDR | **0xc9** |
| EVENT_WAIT/SET/CLEAR, SEMAPHORE_TEST_AND_SET | 0x61–0x65 | → EVENT_SEMAPHORE 0xa0 + EVENT_SEMAPHORE_RANGE_CLEAR 0xb0 + POLL_SEM 0xb3 | (family replace) |

The 4 re-homed shared ops are `{NOP, WRITE, NOTIFY, PSEUDO_READ_VAR_ADDR}`; the
engine-scoped EVENT/SEM ops are *replaced* (not moved) by the unified
`EVENT_SEMAPHORE` model. **The compute surface V2 kept; the control surface it
redesigned.**

### 6.3 Dropped at V2 — V1-specific bands

The engine-scoped `EVENT_{WAIT,SET,CLEAR}` / `SEMAPHORE_TEST_AND_SET` (replaced); the
RT-pseudo `PSEUDO_DMA_{TRIGGER 0xc1, REARM 0xc2, BARRIER 0xc3, MEMCPY 0xc4}` +
`PSEUDO_SET_TRANSFER_ADJUST 0xc7` (V2 keeps the byte band `0xc1–0xc4` for
`PSEUDO_DMATRIGGER/DMAREARM/DMABARRIER/DMAMEMCPY_FULL_IND` — **same bytes, renamed and
reorganized** into the unified `0xc1–0xdf` PSEUDO band); and the **entire `SIM_*`
band** (`SIM_DMA_COPY 0xeb … SIM_MEMCPY 0xef`, `ENGINE_SIM 7<<5`) — simulator-only ops
that V2 does not carry into the runtime enum.

### 6.4 What the transition means

V2 SUNDA is **V1 TONGA with (a) the engine-field encoding flattened to an opaque
byte, (b) the control/sync block redesigned around `EVENT_SEMAPHORE`, (c) the SIM band
dropped, and (d) the POOL engine replaced by the programmable `NX_POOL` GPSIMD DSP
([§7](#7-the-gpsimd-genesis-answer)) — while keeping the producer-side opcode bytes.**
The compute-datapath bytes are the conserved core; everything else is re-homed.
`[HIGH/OBSERVED on (a)(b)(c); MED/INFERRED on (d)'s "producer-side bytes kept across a
silicon swap" framing — the cleanest reading of why POOL `0x45` survives a complete
engine replacement.]`

---

## 7. The GPSIMD-genesis answer

> **Is there a Vision-Q7 GPSIMD POOL engine in V1? NO.** GPSIMD — the programmable
> Cadence Tensilica Vision-Q7 `NX_POOL` cluster running the Vision-Q7 device ISA, with
> custom-op image download, DVE, and IRAM/DRAM apertures — is a **SUNDA-onward (v2+)
> feature.** V1 predates it.

Three independent shipped-artifact lines of evidence:

1. **V1's POOL is fixed-function, not programmable.** `aws_tonga_isa_tpb_pool.h`
   defines `POOL_INST` (`0x45`) as a hardwired planar reduction: a 2-value `pool_func`
   enum (`MAXPOOL = 0x01`, `AVGPOOL = 0x02`; `pool.h:54–58`), a `pool_dim`
   (TENSOR_SUBDIM), and a pre-baked `pool_scale` `float` (`= 1/(R·S)` for averaging).
   The doc states: *"applies a planar function … Supported pooling functions are
   MaxPool and AveragePool. All pooling calculations are performed in FP32"*
   (`pool.h:6–11`). **No downloadable kernel, no custom-op image, no instruction RAM,
   no Q7 core** — a fixed `pool_func` selector, not a kernel. The 64-byte
   `POOL_INST` layout: `inst_header(4)+inst_events(4)+pool_func(1)+in_dtype(1)+
   out_dtype(1)+pool_dim(1)+pool_scale(4)+src/dst MEM_ACCESS_4D(20+20)+
   num_active_channels(1)+reserved[7] == 64` (`pool.h:61–137`).

2. **Zero GPSIMD / Vision-Q7 / DVE / custom-op footprint in the V1 ISA.** A
   case-insensitive scan of the **entire** TONGA `arch-isa/` tree for
   `gpsimd|dve|q7|ncore2gp|vision.?q7|custom.?op` returns **0** matches across every
   term. The V1 ISA has no concept of a programmable DSP cluster, no DVE, no custom-op
   descriptor, no Q7 apertures.

3. **Every runtime core is an `NX_POOL` core — and there is no TONGA core.** The
   shipped runtime container exposes exactly
   `NRTUCODE_CORE_{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS,MAVERICK}_NX_POOL` and the per-gen
   image tables `sunda_libs … maverick_libs`. Every v2+ core carries the **`_NX_POOL`**
   suffix (NX = the Vision-Q7 GPSIMD POOL engine). There is **no TONGA core, no
   `tonga_libs`, and zero `"tonga"` strings** in the runtime lib. The TONGA ISA package
   is **compile-time-only** (a host-side encoder/validator + cmake export), with zero
   runtime identity.

**The genesis, stated.** The `POOL` *name* and the producer-side *opcode byte* `0x45`
are conserved across the V1→V2 boundary, but the *thing behind them* is swapped: V1
POOL = fixed-function MaxPool/AvgPool/vector engine; V2+ `NX_POOL` = the programmable
Vision-Q7 GPSIMD cluster (the device DSP this entire book documents). The producer-side
TPB opcode byte is kept so the host emitter and the NEFF wire format stay stable, but
the **consumer changed from fixed silicon to a downloadable-kernel DSP.** GPSIMD is a
v2+ feature; V1 is the pre-GPSIMD, fixed-function-POOL baseline. `[HIGH/OBSERVED on the
three evidence lines; MED/INFERRED on the silicon-swap-behind-conserved-opcode framing]`

---

## 8. Is MARIANA_PLUS a distinct ISA? — No

> **MARIANA_PLUS (v4+, `arch_id 28` / `coretype 29`) is NOT a distinct ISA. It is a
> recompile + flag-refresh of MARIANA (v4, `arch_id 20` / `coretype 21`).** It
> executes the MARIANA 159-opcode table **verbatim** — same key-set, same values, same
> KIT keys.

The resolution rests on three independent shipped-artifact lines:

1. **No distinct ISA opcode table.** There is no `neuron_mariana_plus_arch_isa/`
   directory; the register tree `arch-headers/mariana_plus/` contains **no
   `NEURON_ISA_TPB_OPCODE`** (it is CSR/register-block only). MARIANA_PLUS therefore
   runs the MARIANA 159-opcode enum, which is why the matrix `V4/4+` column is a
   **single shared column**.
2. **No version-axis point.** No tree has a `V4_PLUS` / `V4P` enumerator in
   `NEURON_ISA_TPB_NEURON_CORE_VERSION` ([§1.1](#11-the-version-axis-is-cumulative-and-it-is-born-at-v2)). The runtime distinguishes v4 from v4+ via a
   **string selector** (`NEURON_RT_DBG_V4_PLUS=0/1`, formerly the compile flag
   `NRTUCODE_MPLUS_ON_MARIANA`), not an ISA version number.
3. **It is a first-class firmware *core* with byte-stable ISA.** MARIANA_PLUS is a peer
   `NRTUCODE_CORE_MARIANA_PLUS_NX_POOL` core with its own image set and register-map
   dir — but every getter name, dispatch handler, opcode value, PROF table, reset/boot
   geometry, and EXTISA Q7 kernel container is **byte-stable or byte-identical** v4 ↔
   v4+. The *only* functional addition is **one gen-wide firmware feature — the DGE
   reshape fast-path** (a microcode/register behavior, not an opcode-table change,
   which is precisely why no new opcode appears).

On the generation axis: `v2 SUNDA < v3 CAYMAN < v4 MARIANA ≤ v4+ MARIANA_PLUS < v5
MAVERICK`, where `≤` means **ISA-equal, firmware/register-revised.** For the full
byte-by-byte v4↔v4+ delta (the DGE fast-path, the `arch_id`/`coretype` algebra, the
register-map refresh), see [MARIANA_PLUS (v4+) Generation
Delta](./mariana-plus-delta.md). `[HIGH/OBSERVED on the ISA-equal relation; the
product-revision label is MED/INFERRED]`

---

## 9. Corrections to the backing reports

Two in-place corrections were established from the bytes.

- **`0x81` is a pre-existing hole, not a reused freed byte.** GEN-04 §2.1/§5.3 call
  `JPEG_DECODE 0x81` *"the only reused freed byte."* The SUNDA opcode enum jumps
  `DROPOUT 0x7f → TRANSPOSE_BATCH_NORM_STATS2 0x82` (`sunda common.h:215–216`); `0x80`
  and `0x81` are **never occupied in any generation** before CAYMAN fills `0x81` (and
  `0x80` stays a hole in all four). So v2→v3 performs **zero byte-reuse**. (Recorded in
  the matrix and [§3.1](#31-sunda-v2--cayman-v3-145--150-12--7).)
- **TONGA V1 has a real opcode enum.** GEN-04 §1.2/§7 mark TONGA as *"register-block
  only, no opcode enum."* That holds for `arch-headers/tonga/` (the RTL tree) but
  **not** for `arch-isa/aws_tonga_isa_*` (cmake `TongaArchIsa`), which ships a real
  engine-scoped `TONGA_ISA_TPB_OPCODE` (44 entries) + 64-byte struct family + SP ISA.
  (Recorded in the [§5](#5-tonga-v1-the-pre-unified-outlier) CORRECTION callout.)

---

## 10. Confidence ledger

| Claim | Conf / Prov |
|---|---|
| Per-gen enum counts 145 / 150 / 159 / 165 (bracket-bounded) | HIGH / OBSERVED |
| All 172 presence-matrix cells + step add/remove markers | HIGH / OBSERVED |
| Value-stability (138 common sunda↔maverick, zero drift) | HIGH / OBSERVED |
| `0x80`/`0x81` are SUNDA holes (the `0x81` CORRECTION) | HIGH / OBSERVED |
| 7 retired bytes abandoned, never reused (forensic) | HIGH / OBSERVED |
| `struct2opcode` 89 / 99 / 108 / 114 (instruction_mapping.json) | HIGH / OBSERVED |
| CORE_VERSION enumerators per tree; no V4_PLUS anywhere | HIGH / OBSERVED |
| MARIANA_PLUS = flag-refresh, no own ISA/version | HIGH / OBSERVED |
| TONGA engine-scoped `local \| (ENGINE<<5)` encoding + 44 roster | HIGH / OBSERVED |
| TONGA 64-byte struct family (COPY/POOL/MATMUL layouts) + SP 16-opcode ISA | HIGH / OBSERVED |
| GPSIMD-genesis = NO (POOL fixed-function; zero Q7 strings; no TONGA core) | HIGH / OBSERVED |
| V1→V2 join (26 carried / 4 re-homed / 14 V1-only) | HIGH / OBSERVED |
| MAVERICK opcode-enum *values* (header) | HIGH / OBSERVED |
| MAVERICK `arch_id 36`; v5 interior semantics | INFERRED |
| "producer-bytes-kept across silicon swap" framing (§6.4 d, §7) | MED / INFERRED |

**Related pages:** [Cross-Gen Kernel-Info Matrix](../images/cross-gen-kernel-info-matrix.md) ·
[Arch-ISA Header Diff](./arch-isa-header-diff.md) ·
[Opcode Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) ·
[Gen-Invariance Thesis](../orientation/gen-invariance.md) ·
[Codename → Generation Map](./codename-generation-map.md) ·
[MARIANA_PLUS (v4+) Delta](./mariana-plus-delta.md) ·
[Confidence & Walls Model](../reference/confidence-model.md).
