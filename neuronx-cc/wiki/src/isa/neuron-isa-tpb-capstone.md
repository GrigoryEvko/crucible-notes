# NEURON_ISA_TPB Struct-Family Capstone — the `.h`

> *All symbols, offsets, and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp311/cp312 rebuild `libwalrus.so` per ABI — re-confirm any raw address against the target wheel). Every struct, field, and offset below is read from `neuronxcc/starfish/lib/libwalrus.so` (GNU build-id `92b4d331a42d7e80bb839e03218d2b9b0c23c346`, MD5 `1d93972b81e619ce6d178a0e4b9003b3`); the `bir::InstructionType` enum and engine ordinals come from `libBIR.so` (build-id `a9b1ea38…`). For `.text`/`.rodata`, virtual address equals file offset (`.text` base `0x62d660`, `.rodata` base `0x1c72000`). The recovered struct/field names (`NEURON_ISA_TPB_TENSOR3D`, `…MXMEM_PATTERN1D`, `…INDIRECT16B`, …) are read directly from the surviving dynamic symbol table (`nm -DC`) — they are binary-derived, not invented. Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This is the **synthesis page** for [Part 2](../). The preceding eight pages each recovered one piece of the Tonga L3 wire format — the [ADDR4](addr4.md) start word (2.2), the [TENSOR1/2/3D](tensor-descriptors.md) descriptors (2.3), the [TENSOR4D / MEM_PATTERN4D](tensor4d-mempattern4d.md) spill (2.4), the [MEM_PATTERN2D/3D](mempattern-2d-3d.md) DST role (2.5), the [MXMEM_PATTERN1D](mxmem-pattern1d.md) MX descriptor (2.6), the [indirect-gather](indirect-descriptors.md) descriptors (2.7), the [64-byte instruction bundle](instruction-bundle.md) header + three slot families (2.1), and the [AP encoder dispatch](ap-encoder-dispatch.md) role→type fork (2.8). This page assembles all of them into **one compilable C header** — `neuron_isa_tpb.h` — that types every instruction bundle and every access pattern. The bar is concrete: a reimplementer `#include`s this one header and compiles their encoder/decoder against it; a reader who has seen none of the eight sub-pages can lay out any bundle from this file alone.

The "`NEURON_ISA_TPB` struct family" is **not one fixed struct**. It is a **64-byte (16-dword) instruction bundle** whose 4-byte header word is the *only* universal field, and whose remaining 60 bytes are an **op-specific discriminated union** (`NEURON_ISA_TPB_INST_UNION`). The union **discriminant is the opcode byte** at `raw[0]`. Descriptor *slots* inside the body land at one of **three family-fixed offset sets** (A/B/C), chosen by descriptor width and operand count — never freely per op. Every descriptor slot opens with an `ADDR4` word and follows the 4+4N rule; SRC operands carry the `TENSOR*` wire type, DST operands the byte-identical `MEM_PATTERN*` wire type (the distinction is *role*, not bytes).

The header is the union of the byte-verified facts from all eight sub-pages. Where it differs from an earlier sub-page reading, the disagreement is reconciled in §[Reconciled disagreements](#reconciled-disagreements) and tagged with an in-place CORRECTION. Each struct and field carries an inline confidence tag; the spine — the 64-byte bundle, the three families, the 8/12/16/20-byte descriptor sizes, the role fork, the dtype enum — is CONFIRMED (encoder writes and decoder reads agree byte-exact, and the `.rodata` size asserts + the recovered type names are read directly from `libwalrus.so` this pass).

## At a glance — the struct roster

| Struct | Size | Role | Source page | Conf |
|---|---|---|---|---|
| `NEURON_ISA_TPB_ADDR4` | 4 B (`u32`) | start-address word, opens every descriptor slot | [2.2](addr4.md) | CONFIRMED |
| `NEURON_ISA_TPB_TENSOR1D` | 8 B | 1-free-dim SRC pattern (`4+4·1`) | [2.3](tensor-descriptors.md) | CONFIRMED |
| `NEURON_ISA_TPB_TENSOR2D` | 12 B | 2-free-dim SRC pattern (`4+4·2`) | [2.3](tensor-descriptors.md) | CONFIRMED |
| `NEURON_ISA_TPB_TENSOR3D` | 16 B | 3-free-dim SRC pattern — the workhorse | [2.3](tensor-descriptors.md) | CONFIRMED |
| `NEURON_ISA_TPB_TENSOR4D` | 20 B | 4-free-dim SRC spill (`4+4·4`) | [2.4](tensor4d-mempattern4d.md) | CONFIRMED |
| `NEURON_ISA_TPB_MEM_PATTERN2D/3D/4D` | 12/16/20 B | DST byte-twins of `TENSOR2/3/4D` | [2.5](mempattern-2d-3d.md) | CONFIRMED |
| `NEURON_ISA_TPB_MXMEM_PATTERN1D` | 16 B | MX data+E8M0-scale dual-ADDR4 (CoreV4-only; 12 written) | [2.6](mxmem-pattern1d.md) | CONFIRMED |
| `NEURON_ISA_TPB_MXINDIRECT16B` | 16 B | MX gather (index+data+scale ADDR4) | [2.7](indirect-descriptors.md) | CONFIRMED |
| `NEURON_ISA_TPB_INDIRECT16B` | 16 B | non-MX 3-D gather (index+data+num) | [2.7](indirect-descriptors.md) | CONFIRMED |
| `NEURON_ISA_TPB_INDIRECT20B` | 20 B | non-MX 4-D gather | [2.7](indirect-descriptors.md) | CONFIRMED |
| `NEURON_ISA_TPB_HEADER` | 4 B | universal `{opcode, 0x10, 0, 0}` header word | [2.1](instruction-bundle.md) | CONFIRMED |
| `…_BUNDLE_MATMUL` (Family A) | 64 B | src `+0x10` / dst `+0x30`, control band `+0x20..+0x2F` | [2.1](instruction-bundle.md) | CONFIRMED |
| `…_BUNDLE_TENSORTENSOR` (Family B) | 64 B | dst `+0x10` / in0 `+0x20` / in1 `+0x30`, band `+0x0C..+0x0F` | [2.1](instruction-bundle.md) | CONFIRMED |
| `…_BUNDLE_POOLCOPY` (Family C) | 64 B | in `+0x0C` / out `+0x2C`, control band `+0x20..+0x2B` | [2.1](instruction-bundle.md) | CONFIRMED |
| `…_SEMAPHORE` | 64 B | SP sync record (opcode `0xA0`) | [1.14](../arch/execution-sync-model.md) | CONFIRMED |
| `…_BUNDLE_BRANCH` | 64 B | SP branch record (opcode `0xA9`) | [2.1](instruction-bundle.md) | CONFIRMED |
| `NEURON_ISA_TPB_INST_UNION` | 64 B | the discriminated bundle (all of the above) | — | CONFIRMED |

The four `.rodata` size asserts that pin the descriptor sizes are read directly from the binary this pass: `"ISA mem pattern 1D must have 8 bytes to encode"` / `2D … 12` / `3D … 16` / `4D … 20` — `slot_size(N) = 4 + 4N`. The struct **type names** above are likewise read from the live `nm -DC` symbol table (`NEURON_ISA_TPB_TENSOR1D … TENSOR4D`, `MEM_PATTERN2D/3D/4D`, `MXMEM_PATTERN1D`, `MXINDIRECT16B`, `INDIRECT16B/20B`, `ADDR4`, `DTYPE` — all present).

---

## The authoritative header — `neuron_isa_tpb.h`

This is the centerpiece. It is self-contained, `gcc -fsyntax-only`-clean, and includes the `_Static_assert`s that pin every size. Every field cites the sub-page that byte-verified it. The structs use natural alignment, which — because every offset is hand-checked and every field is byte/2-byte/4-byte at a naturally aligned offset — reproduces the wire layout with **no implicit padding** (the emitter `memset`-zeroes all 64 bytes, so packed == wire); for a strict ABI-portable build, wrap each struct in `#pragma pack(push,1)`/`#pragma pack(pop)` (see the [packing note](#a-note-on-packing)).

```c
/* neuron_isa_tpb.h — AWS Neuron / Trainium TPB L3 instruction-bundle wire format.
 * Reverse-engineered from neuronx_cc 2.24.5133.0+58f8de22 libwalrus.so (build-id
 * 92b4d331) via static analysis. Little-endian, x86-64-emitted. CoreV2/V3/V4.
 *
 * One bundle = 64 bytes = 16 dwords. Header word @+0 = {opcode, 0x10, 0, 0}.
 * The 60-byte body is an op-specific union; the opcode byte @+0 (raw[0]) is the
 * union discriminant. Three descriptor-slot families (A/B/C) cover all compute ops.
 *
 * Confidence: all structs CONFIRMED (encoder writes + decoder reads agree byte-
 * exact; .rodata size asserts + recovered type names read from the binary) unless
 * a field is tagged otherwise inline. */
#ifndef NEURON_ISA_TPB_H
#define NEURON_ISA_TPB_H
#include <stdint.h>

/* ---- compile-time invariants (rodata "ISA mem pattern ND must have K bytes") --- */
#define NEURON_ISA_BUNDLE_BYTES   64u   /* inst_word_len 0x10 = 16 dwords  [2.1 §1] */
#define NEURON_ISA_WORD_LEN       0x10u /* hardcoded byte[1]; zero non-16 bundles    */

/* ============================================================================
 * 1. ENUMS
 * ========================================================================== */

/* 1.1 NEURON_ISA_TPB_DTYPE — the ON-WIRE dtype tag (NOT the BIR Dtype ordinal).
 *     Ordered by container-size class. The dtype->wire-tag LUT is .rodata
 *     0x1dfbad0 = {03 02 10 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c}
 *     i.e. wire_tag = byte_1DFBAD0[BIR_Dtype].  [2.6 / D-D04 CONFIRMED — LUT read
 *     this pass.]  Address alignment keys on THIS tag (addr_aligned_dtype, 2.2):
 *     {2,3,13,14,15(,16 v4)}=align1, {4,5,6,7}=align2, {8,9,10,11}=align4,
 *     {1,12}=align8. A PSUM *write* requires a 4-byte tag {8,9,10,11}. */
typedef enum {
    ISA_DT_UINT64         = 1,   /* align 8                                          */
    ISA_DT_INT8           = 2,   /* align 1                                          */
    ISA_DT_UINT8          = 3,   /* align 1; E8M0 (float8_e8m0fnu, MX scale) maps here */
    ISA_DT_INT16          = 4,   /* align 2                                          */
    ISA_DT_UINT16         = 5,   /* align 2                                          */
    ISA_DT_BFLOAT16       = 6,   /* align 2; matmul dst "nc" form accepts bf16       */
    ISA_DT_FLOAT16        = 7,   /* align 2                                          */
    ISA_DT_INT32          = 8,   /* align 4; PSUM-legal                              */
    ISA_DT_UINT32         = 9,   /* align 4; PSUM-legal (matmul-dst addr forced tag 9)*/
    ISA_DT_FLOAT32        = 10,  /* align 4; PSUM accumulator (matmul dst dt=0xa)    */
    ISA_DT_FLOAT32R       = 11,  /* align 4; fp32-round 2-FMA dst                    */
    ISA_DT_INT64          = 12,  /* align 8                                          */
    ISA_DT_FLOAT8_E3      = 13,  /* align 1                                          */
    ISA_DT_FLOAT8_E4M3    = 14,  /* align 1; FP8 e4m3 (+_x4 share this tag)          */
    ISA_DT_FLOAT8_E5M2    = 15,  /* align 1; FP8 e5m2 (+_x4 share this tag)          */
    ISA_DT_FLOAT4_E2M1_X4 = 16   /* CoreV4-ONLY; packed FP4-x4 (BIR Dtype 2, wire 0x10)*/
} NEURON_ISA_TPB_DTYPE;
/* NOTE: the LUT maps BIR Dtype 2 (float4_e2m1fn_x4) -> wire 0x10 (its own MX-era tag
 *       on CoreV4); on CoreV2 the FP4-x4 type does not exist. [2.6 / D-M08 CONFIRMED] */

/* 1.2 ADDR4 mode nibble (byte3 & 0x60 = word bits 30:29). [2.2 CONFIRMED]          */
typedef enum {
    ISA_ADDR_STATIC    = 0x00, /* plain physical address (default, no stamp)         */
    ISA_ADDR_INDIRECT  = 0x20, /* bit29 — gather/index slot (encoder: or [slot+3],0x20)*/
    ISA_ADDR_ACTIVE    = 0x40, /* bit30 — dynamic-AP marker (set UPSTREAM, never by   */
                               /*         the ADDR4 encoder; CONFIRMED consumer)      */
    ISA_ADDR_MODE_DEAD = 0x60  /* 0b11 — no producer, no consumer (dead)             */
} NEURON_ISA_ADDR4_MODE;       /* register-mode is bit31 (0x80), ORTHOGONAL to nibble */

/* 1.3 The per-op L3 opcode byte (the union discriminant @raw[0]). The wire WORD is
 *     0x10<<8 | opcode (0x10 = inst_word_len). Representative subset; the full
 *     110-IT x 3-arch table is the opcode-word authority. (* = CoreV4 overrides.)
 *     [2.1 / D-J32 CONFIRMED] */
typedef enum {
    ISA_OP_LOADSTATIONARY     = 0x01, /* matmul weight stage   (Family A, no dst)    */
    ISA_OP_MATMUL             = 0x02, /* dense matmul          (Family A)            */
    ISA_OP_LOADTAGS_SPARSE    = 0x06,
    ISA_OP_MATMUL_SPARSE      = 0x07,
    ISA_OP_LDWEIGHT_MX        = 0x09, /* word 0x1009; CoreV4 MX weight stage (A/MX)  */
    ISA_OP_MATMUL_MX          = 0x0A, /* word 0x100A; CoreV4 MX matmul       (A/MX)  */
    ISA_OP_ACTIVATION         = 0x21, /* *CoreV4 = 0x25 (Activate2)          (Family A)*/
    ISA_OP_ACT_TABLE_LOAD     = 0x23, /* LoadActFuncSet                              */
    ISA_OP_ACT_READ_ACCUM     = 0x24,
    ISA_OP_TENSOR_TENSOR      = 0x41, /* 0x51 = bitvec engine  (Family B)            */
    ISA_OP_TENSOR_SCALAR      = 0x43, /* (+0x93/0x53/0x9A variants)  (Family A)      */
    ISA_OP_POOL               = 0x45, /* MaxPool/AvgPool       (Family C)            */
    ISA_OP_COPY               = 0x46, /* TensorCopy; +1=0x47 = casting copy   (C)    */
    ISA_OP_MEMSET             = 0x49, /* Const-fill; 0x4D = Random/Rng  (Family C)   */
    ISA_OP_BNSTATS            = 0x61, /* 0x82 = +transpose     (Family C mixed)      */
    ISA_OP_GATHER             = 0x68, /* (+0x67 PoolBufferLoad)  (Family C/indirect) */
    ISA_OP_MAX8               = 0x6C, /* DVE Max8              (Family C)            */
    ISA_OP_EVENT_SEMAPHORE    = 0xA0, /* SP sync record        (semaphore)           */
    ISA_OP_HALT               = 0xA1,
    ISA_OP_DRAIN              = 0xA2,
    ISA_OP_NOOP               = 0xA4,
    ISA_OP_REGISTER_MOVE      = 0xA7,
    ISA_OP_REGISTER_ALU       = 0xA8,
    ISA_OP_BRANCH             = 0xA9, /* CompareAndBranch + UnconditionalBranch (ctrl)*/
    ISA_OP_GROUP_RESET_SEMA   = 0xB0,
    ISA_OP_DMA_TRIGGER        = 0xC1,
    ISA_OP_CALL               = 0xD3,
    ISA_OP_DMA_DIRECT2D       = 0xD4, /* Family D — ADDR8 descriptor (distinct)      */
    ISA_OP_ALL_ENGINE_BARRIER = 0xD5
} NEURON_ISA_TPB_OPCODE;

/* 1.4 ALU wire byte (RegisterAlu / TensorTensor / TensorScalar). sub_12039C0:
 *     add=4 sub=5 mult=6 max=8 min=9 ... is_gt=0x13 is_ge=0x14 is_le=0x15
 *     is_lt=0x16 not_eq=0x18 abs=0x19 pow=0x1a mod=0x1b rsqrt=0x1d. [D-M10 STRONG]  */
/* 1.5 Branch comp_op wire byte sub_1203580: {IMM LT..GT = 1..6, REG = 9..14};
 *     comp_op 0 = "always" (unconditional). [D-M10 CONFIRMED]                       */
/* 1.6 Semaphore CMP-OP @+0x20 {0x01 EQ, 0x04 default(wait+dec), 0x05 GE, 0x85 GE-reg};
 *     SUBTYPE @+0x22 {0x13 inc+1, 0x14 dec-1, 0x15 add-imm, 0x17 sub-imm, 0x19 set;
 *     0x11 evt-set -> event bank}. [1.14 CONFIRMED]                                 */

/* ============================================================================
 * 2. ACCESS-PATTERN SUB-STRUCTS  (every descriptor opens with an ADDR4)
 * ========================================================================== */

/* 2.1 ADDR4 — the 4-byte start-address word @slot+0 of EVERY descriptor.  [2.2]
 *     bits 0..28 (0x1FFFFFFF) = 29-bit byte address;
 *       bits 25..28 (0x1E000000) = SBUF(0) / PSUM(nonzero) region discriminator;
 *       PSUM iff (addr29 - 0x2000000) <= 0x3FFFFF (4 MiB window @ 32 MiB).
 *     bit 29 (0x20) = INDIRECT-gather index marker (the ONLY mode bit the encoder
 *                     stamps: `or [slot+3],0x20`).
 *     bit 30 (0x40) = ACTIVE/dynamic (set upstream; consumed by tensor1d_valid).
 *     bit 31 (0x80) = REGISTER-MODE: byte0 = regid (<0x40), bits 8..30 = 0. */
typedef uint32_t NEURON_ISA_TPB_ADDR4;   /* packed u32; bitfield helpers below      */
#define ADDR4_ADDR29(w)      ((w) & 0x1FFFFFFFu)
#define ADDR4_REGION(w)      ((w) & 0x1E000000u)         /* 0 => SBUF, !=0 => PSUM   */
#define ADDR4_MODE(w)        (((w) >> 24) & 0x60u)       /* byte3 & 0x60 nibble      */
#define ADDR4_IS_REGISTER(w) (((w) & 0x80000000u) != 0u) /* bit31                    */
#define ADDR4_REGID(w)       ((w) & 0xFFu)               /* valid iff IS_REGISTER    */
#define ADDR4_PSUM_BASE      0x02000000u                 /* PSUM window base         */
#define ADDR4_PSUM_SPAN      0x003FFFFFu                 /* 4 MiB window             */

/* 2.2 TENSOR1D/2D/3D/4D — the static on-chip SRC descriptors (the 4+4N rule).
 *     strides = SIGNED i16 (ELEMENT units, [-32768,32767]); nums = UNSIGNED u16
 *     ([1,65535]; 0 illegal). Two SEPARATE contiguous arrays (all strides, then
 *     all nums) — NOT interleaved. Spare dims {stride=1,num=1}-filled (0x00010001),
 *     never 0. The PARTITION dim (Pattern[0]=W) folds into ADDR4, not a stride/num.
 *     [2.3/2.4 CONFIRMED — encoder<->decoder byte-exact both ways.]                 */
typedef struct {                 /* 8 bytes  — rodata "1D must have 8 bytes"         */
    NEURON_ISA_TPB_ADDR4 addr;   /* +0x00                                           */
    int16_t  stride0;            /* +0x04                                           */
    uint16_t num0;               /* +0x06   (num base = 4 + 2N = +6)                */
} NEURON_ISA_TPB_TENSOR1D;

typedef struct {                 /* 12 bytes — "2D must have 12 bytes"              */
    NEURON_ISA_TPB_ADDR4 addr;   /* +0x00                                           */
    int16_t  stride[2];          /* +0x04 stride0, +0x06 stride1                    */
    uint16_t num[2];             /* +0x08 num0,    +0x0A num1   (num base = +8)     */
} NEURON_ISA_TPB_TENSOR2D;

typedef struct {                 /* 16 bytes — "3D must have 16 bytes"              */
    NEURON_ISA_TPB_ADDR4 addr;   /* +0x00                                           */
    int16_t  stride[3];          /* +0x04 / +0x06 / +0x08                           */
    uint16_t num[3];             /* +0x0A / +0x0C / +0x0E   (num base = +0xA)       */
} NEURON_ISA_TPB_TENSOR3D;        /* THE workhorse: matmul/activation/TS/TT slots    */

typedef struct {                 /* 20 bytes — "4D must have 20 bytes"              */
    NEURON_ISA_TPB_ADDR4 addr;   /* +0x00                                           */
    int16_t  stride[4];          /* +0x04 / +0x06 / +0x08 / +0x0A   (+0x0A = spill)  */
    uint16_t num[4];             /* +0x0C / +0x0E / +0x10 / +0x12   (num base +0xA->+0xC)*/
} NEURON_ISA_TPB_TENSOR4D;        /* copy/pool/reduce/BN; count base relocates +2 B  */

/* 2.3 MEM_PATTERN2D/3D/4D — the DST/PSUM descriptors. BYTE-IDENTICAL to TENSOR;
 *     distinct ONLY in (a) wire-type name, (b) dispatcher assert
 *     ("ISA mem pattern is union but is missing field" — DST), vs the SRC assert
 *     ("static tensor pattern but has indirect field"), (c) validator role flags
 *     (WR=1/PSUM=1/SBUF=0 for a PSUM dst). The PSUM bank rides ADDR4 hi bits;
 *     accumulate/zero-region/dst-dtype are SIBLING bundle bytes, NOT descriptor
 *     fields. [2.5 CONFIRMED — both asserts read from the binary this pass.]        */
typedef NEURON_ISA_TPB_TENSOR2D NEURON_ISA_TPB_MEM_PATTERN2D;
typedef NEURON_ISA_TPB_TENSOR3D NEURON_ISA_TPB_MEM_PATTERN3D;
typedef NEURON_ISA_TPB_TENSOR4D NEURON_ISA_TPB_MEM_PATTERN4D;

/* 2.4 MXMEM_PATTERN1D — the CoreV4-ONLY 16-byte MX (microscaling FP4/FP8)
 *     descriptor: paired packed-x4 DATA addr + E8M0-SCALE addr. 16-byte TYPE,
 *     12 bytes written, +0xC..+0xF reserved/zero on the static path. DATA via
 *     getStartAddress (SB|PSUM); SCALE via getStartAddressForMXScale (SBUF-only,
 *     "MX Scale Tensor can only be in SB"). K-extent = NEPP x4 iff BIR Dtype in
 *     {2,8,9}. step-dir in {0x01,0xFF}. scalePart = scaleBasePart % PE_count(128).
 *     The "scale selector" is the encode-time a4=1 fork to vtbl+0x28, NOT a wire
 *     nibble. [2.6 CONFIRMED]                                                       */
typedef struct {                 /* 16 bytes                                        */
    NEURON_ISA_TPB_ADDR4 data;   /* +0x00  packed FP4/FP8-x4 data start addr (a4=0)  */
    NEURON_ISA_TPB_ADDR4 scale;  /* +0x04  E8M0 per-block exponent start addr (a4=1) */
    uint16_t k_extent;           /* +0x08  x4-unpacked element count (!=0)          */
    uint8_t  step_dir;           /* +0x0A  step sign {0x01=+1, 0xFF=-1}             */
    uint8_t  scale_base_part;    /* +0x0B  scaleBasePart % PE_count(128)            */
    uint8_t  _reserved[4];       /* +0x0C..+0x0F  RESERVED (static path; reused by   */
                                 /*               MXINDIRECT16B, see 2.5 below)      */
} NEURON_ISA_TPB_MXMEM_PATTERN1D;

/* 2.5 The three TENSOR-INDIRECT (gather) descriptors. The FIRST (INDEX) ADDR4
 *     carries bit29 (0x20). The index vector supplies the addressing; on the
 *     indirect branch the static stride/num region is INERT (not stride/num-filled).
 *     [2.7 CONFIRMED — the indirect branch RETURNS without the static fill.]        */
typedef struct {                 /* 16 bytes (MXMEM_PATTERN1D + INDEX pushed front)  */
    NEURON_ISA_TPB_ADDR4 index;  /* +0x00  gather index-vector addr (bit29 set, a4=0)*/
    NEURON_ISA_TPB_ADDR4 data;   /* +0x04  indirectly-addressed FP4/FP8-x4 data (a4=0)*/
    NEURON_ISA_TPB_ADDR4 scale;  /* +0x08  E8M0 scale addr (a4=1)                    */
    uint16_t k_extent;           /* +0x0C  getNumIndirectIndices x4 ({2,8,9})       */
    uint8_t  _spare;             /* +0x0E  high half of the count word              */
    uint8_t  scale_base_part;    /* +0x0F  scaleBasePart % PE_count                 */
} NEURON_ISA_TPB_MXINDIRECT16B;   /* MX/DGE gather (MoE expert/token routing)        */

typedef struct {                 /* 16 bytes — non-MX 2-ADDR4 gather (3-D AP)       */
    NEURON_ISA_TPB_ADDR4 index;  /* +0x00  gather index addr (bit29 set)            */
    NEURON_ISA_TPB_ADDR4 data;   /* +0x04  data tensor addr                         */
    uint16_t num;                /* +0x08  getNumIndirectIndices (NO x4)            */
    uint8_t  _inert[6];          /* +0x0A..+0x0F  inert (TENSOR3D static region)    */
} NEURON_ISA_TPB_INDIRECT16B;     /* Gather(92)/IndirectCopy(26); 3-D index/data     */

typedef struct {                 /* 20 bytes — non-MX 4-D gather                    */
    NEURON_ISA_TPB_ADDR4 index;  /* +0x00                                           */
    NEURON_ISA_TPB_ADDR4 data;   /* +0x04                                           */
    uint16_t num;                /* +0x08  getNumIndirectIndices (NO x4)            */
    uint8_t  _inert[10];         /* +0x0A..+0x13  inert (TENSOR4D static region)    */
} NEURON_ISA_TPB_INDIRECT20B;     /* same payload, 20-byte; 16-vs-20 by AP free dims */

/* ============================================================================
 * 3. THE COMMON 64-BYTE HEADER  [2.1 CONFIRMED — setupHeader byte-exact this pass]
 * ========================================================================== */
typedef struct {                 /* the ONLY universal bytes (setupHeader)          */
    uint8_t  opcode;             /* +0x00  L3 opcode (union discriminant)           */
    uint8_t  inst_word_len;      /* +0x01  = 0x10 = 16 dwords (hardcoded immediate)  */
    uint16_t reserved;           /* +0x02  = 0x0000                                 */
} NEURON_ISA_TPB_HEADER;
/* *(u16*)bundle = 0x10NN little-endian (NN = opcode, hi 0x10 = inst_word_len).      */

/* ============================================================================
 * 4. THE THREE COMPUTE-BUNDLE FAMILIES  (op-specific union bodies)
 * ========================================================================== */

/* 4.A FAMILY A — "3D 2-slot": src@+0x10 / dst@+0x30, control band @+0x20..+0x2F.
 *     Canonical witness = DENSE MATMUL (opcode 0x02). The +0x20 "third-slot"
 *     position is the CONTROL BAND, not a descriptor. Users: matmul, activation,
 *     TensorScalar, DVE scalar ops. [2.1 §Family A / 2.5 §6 CONFIRMED]             */
typedef struct {                                     /* 64 bytes total              */
    uint8_t  opcode;            /* +0x00 = 0x02 (matmul)                            */
    uint8_t  inst_word_len;     /* +0x01 = 0x10                                     */
    uint16_t reserved;          /* +0x02 = 0                                        */
    uint8_t  _pad04[0x0C];      /* +0x04..+0x0F (sync band if SyncInfo present)     */
    NEURON_ISA_TPB_TENSOR3D moving;  /* +0x10  MOVING/ifmap (SB) src slot           */
    /* ---- CONTROL BAND @+0x20..+0x2F (between the two slots) ---- */
    uint8_t  in_dtype;          /* +0x20  ifmap/in DTYPE (ISA wire tag)            */
    uint8_t  perf_sel;          /* +0x21  matmul perf-mode selector 0/2/3          */
    uint8_t  _ctl22;            /* +0x22                                           */
    uint8_t  perf_opt;          /* +0x23  perf/format byte                         */
    uint8_t  row_group;         /* +0x24  PE row-tile group (CoreV2; {1,2,4,8})    */
    uint8_t  col_group;         /* +0x25  PE col-tile group (CoreV2)               */
    uint8_t  wt_base_rows;      /* +0x26  weights base partition / num_active_rows  */
    uint8_t  num_active_cols;   /* +0x27                                           */
    uint8_t  dst_dtype;         /* +0x28  PSUM-DST DTYPE (matmul/MX); ==6 -> bf16 nc */
    uint8_t  _ctl29[2];         /* +0x29..+0x2A                                    */
    uint8_t  accumulate;        /* +0x2B  {b0 START / b1 STOP / b2 ACCUMULATE}     */
    uint8_t  rowcol_group_v3;   /* +0x2C  CoreV3 packed (col<<8|row) lands here    */
    uint8_t  _ctl2D;            /* +0x2D                                           */
    uint8_t  col_group_norm;    /* +0x2E  normalized col {1,2,4,8,16} (CoreV2)     */
    uint8_t  psum_zero_region;  /* +0x2F  ceil(log2 bankspan)                      */
    NEURON_ISA_TPB_MEM_PATTERN3D psum_dst;  /* +0x30  PSUM dst slot (16 B)         */
} NEURON_ISA_TPB_BUNDLE_MATMUL;
/* Activation/TensorScalar reuse the +0x10/+0x30 slots; their control band differs
 *   (Act: +0x20 in-dtype, +0x21 out-dtype, +0x23 act_tbl_sel; TS: +0x20/+0x21 in/out
 *    dtype, +0x22 scalar-base, +0x24 op0 ALU, +0x25 op1 ALU, +0x26 reverse). [2.1]  */

/* 4.B FAMILY B — "3D 3-slot": dst@+0x10 / in0@+0x20 / in1@+0x30 CONTIGUOUS;
 *     control band @+0x0C..+0x0F BEFORE the slots. Witness = TensorTensor
 *     (opcode 0x41). The three TENSOR3D slots fill +0x10..+0x3F exactly.
 *     [2.1 §Family B / D-J05 CONFIRMED]                                           */
typedef struct {                                     /* 64 bytes total              */
    uint8_t  opcode;            /* +0x00 = 0x41 (0x51 bitvec)                       */
    uint8_t  inst_word_len;     /* +0x01 = 0x10                                     */
    uint16_t reserved;          /* +0x02                                           */
    uint8_t  _pad04[8];         /* +0x04..+0x0B (sync band if present)             */
    /* ---- CONTROL BAND @+0x0C..+0x0F (BEFORE the slots) ---- */
    uint8_t  in_dtype_nibbles;  /* +0x0C  low=in0 dtype, high=in1 dtype            */
    uint8_t  out_dtype;         /* +0x0D                                           */
    uint8_t  alu_op;            /* +0x0E  AluOp wire byte (sub_12039C0)            */
    uint8_t  num_active_parts;  /* +0x0F                                           */
    NEURON_ISA_TPB_MEM_PATTERN3D dst;  /* +0x10  out                              */
    NEURON_ISA_TPB_TENSOR3D      in0;  /* +0x20  tensor0/arg0                     */
    NEURON_ISA_TPB_TENSOR3D      in1;  /* +0x30  tensor1/arg1                     */
} NEURON_ISA_TPB_BUNDLE_TENSORTENSOR;

/* 4.C FAMILY C — "4D 2-slot": in@+0x0C / out@+0x2C, control band @+0x20..+0x2B.
 *     Witness = POOL (opcode 0x45) and TensorCopy (0x46). The low 4D slot ends at
 *     +0x20; the high 4D slot starts at +0x2C; the control band sits between.
 *     [2.1 §Family C / D-J03 / D-J06 CONFIRMED]                                   */
typedef struct {                                     /* 64 bytes total              */
    uint8_t  opcode;            /* +0x00 = 0x45 (Pool) / 0x46 (Copy)               */
    uint8_t  inst_word_len;     /* +0x01 = 0x10                                     */
    uint16_t reserved;          /* +0x02                                           */
    uint8_t  _pad04[8];         /* +0x04..+0x0B (sync band if present)             */
    NEURON_ISA_TPB_TENSOR4D in; /* +0x0C  in/src slot (20 B, ends at +0x20)        */
    /* ---- CONTROL BAND @+0x20..+0x2B (between the slots) ---- */
    uint8_t  src_dtype;         /* +0x20  (Copy: src dtype; Pool: in dtype)        */
    uint8_t  dst_dtype;         /* +0x21  (Copy: dst dtype; Pool: out dtype)       */
    uint8_t  _ctl22[2];         /* +0x22..+0x23                                   */
    uint8_t  func;              /* +0x24  Pool func {1=Max, 2=Avg}                 */
    uint8_t  mode;              /* +0x25  Pool mode = 3 (pool sub-mode, const)     */
    uint8_t  _ctl26[2];         /* +0x26..+0x27                                   */
    uint32_t scale_or_fill;     /* +0x28  Pool 1/N reciprocal (Max=0x3F800000);    */
                                /*        Memset fill imm; (DWORD)                 */
    NEURON_ISA_TPB_MEM_PATTERN4D out; /* +0x2C  out slot (20 B, ends at +0x40)     */
} NEURON_ISA_TPB_BUNDLE_POOLCOPY;
/* MIXED-WIDTH variant (BNStats): in 4D @+0x0C, out 2D @+0x30 — the low/high anchor
 *   positions accept variable-width descriptors. [2.1 §"mixed-width slots"]        */

/* ============================================================================
 * 5. THE SEMAPHORE / SYNC RECORD (opcode 0xA0; SP engine)  [1.14 CONFIRMED]
 * ========================================================================== */
typedef struct {                                     /* 64 bytes total              */
    uint8_t  opcode;            /* +0x00 = 0xA0 (word 0x10A0)                       */
    uint8_t  inst_word_len;     /* +0x01 = 0x10                                     */
    uint16_t reserved;          /* +0x02                                           */
    uint8_t  events[8];         /* +0x04  EMBEDDED-EVENTS predicate (inline wait/    */
                                /*        signal: mode@+4, idx@+5, val@+8)         */
    uint8_t  _r0C[0x14];        /* +0x0C..+0x1F reserved                          */
    uint8_t  cmp_op;            /* +0x20  WAIT comparator {0x01 EQ, 0x05 GE, ...}  */
    uint8_t  wait_idx;          /* +0x21  WAIT sema id (u8 < 256)                  */
    uint8_t  subtype;           /* +0x22  ACT family {0x13 inc, 0x14 dec, 0x15..}  */
    uint8_t  act_idx;           /* +0x23  ACT sema id (u8 < 256)                   */
    uint32_t wait_value;        /* +0x24  WAIT threshold (or RegId if GE-reg)      */
    uint32_t act_value;         /* +0x28  ACT inc/dec/set operand                  */
    uint8_t  _r2C[0x14];        /* +0x2C..+0x3F reserved                          */
} NEURON_ISA_TPB_SEMAPHORE;
/* 256 semaphores/NeuronCore, signed-32 counter, 8-bit idx, arch-stable v2/v3/v4.    */

/* ============================================================================
 * 6. A CONTROL/BRANCH BUNDLE (opcode 0xA9; SP engine)  [D-M10 CONFIRMED]
 * ========================================================================== */
typedef struct {                                     /* 64 bytes total              */
    uint8_t  opcode;            /* +0x00 = 0xA9 (CompareAndBranch / Uncond)        */
    uint8_t  inst_word_len;     /* +0x01 = 0x10                                     */
    uint16_t reserved;          /* +0x02                                           */
    uint8_t  _pad04[8];         /* +0x04..+0x0B (sync band)                        */
    uint8_t  comp_op;           /* +0x0C  wire comp_op (0=always; 1..6 IMM, 9..14 REG)*/
    uint8_t  lhs_dtype;         /* +0x0D  LHS dtype wire                          */
    uint8_t  ap_kind;           /* +0x0E  operand-AP kind = 3 (scalar reg)        */
    uint8_t  _r0F;              /* +0x0F                                           */
    uint32_t rhs_imm;           /* +0x10  RHS immediate (IMM form only)            */
    uint8_t  _r14[0x0C];        /* +0x14..+0x1F                                   */
    uint8_t  lhs_reg;           /* +0x20  LHS register id (u8)                     */
    uint8_t  rhs_reg;           /* +0x21  RHS register id (REG form only)          */
    uint8_t  _r22[0x0E];        /* +0x22..+0x2F                                   */
    uint64_t target_pc;         /* +0x30  branch target = getBranchTargetId        */
    uint8_t  _r38[8];           /* +0x38..+0x3F                                   */
} NEURON_ISA_TPB_BUNDLE_BRANCH;

/* ============================================================================
 * 7. THE INST_UNION — the discriminated 64-byte instruction bundle
 * ========================================================================== */
typedef union {                              /* sizeof == 64 (all members)          */
    uint8_t                            raw[NEURON_ISA_BUNDLE_BYTES]; /* the wire     */
    NEURON_ISA_TPB_HEADER              header;        /* opcode = discriminant @raw[0]*/
    NEURON_ISA_TPB_BUNDLE_MATMUL       matmul;        /* Family A (op 0x02, 0x01, ..)*/
    NEURON_ISA_TPB_BUNDLE_TENSORTENSOR tensortensor;  /* Family B (op 0x41/0x51)     */
    NEURON_ISA_TPB_BUNDLE_POOLCOPY     poolcopy;      /* Family C (op 0x45/0x46/..)  */
    NEURON_ISA_TPB_SEMAPHORE           semaphore;     /* op 0xA0 (SP sync)           */
    NEURON_ISA_TPB_BUNDLE_BRANCH       branch;        /* op 0xA9 (SP branch)         */
    /* Family A also covers Activation(0x21/0x25), TensorScalar(0x43..);
     * Family C also covers TensorReduce, Reciprocal, Iota, Memset(0x49),
     * StreamShuffle/Transpose, Gather, BNStats; MX matmul (0x09/0x0A) embeds an
     * NEURON_ISA_TPB_MXMEM_PATTERN1D at +0x10. Discriminant = raw[0] (the opcode).  */
} NEURON_ISA_TPB_INST_UNION;

_Static_assert(sizeof(NEURON_ISA_TPB_INST_UNION)     == 64, "bundle must be 64 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_TENSOR1D)       == 8,  "1D = 8 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_TENSOR2D)       == 12, "2D = 12 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_TENSOR3D)       == 16, "3D = 16 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_TENSOR4D)       == 20, "4D = 20 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_MXMEM_PATTERN1D)== 16, "MXMEM1D = 16 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_MXINDIRECT16B)  == 16, "MXINDIRECT16B = 16 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_INDIRECT16B)    == 16, "INDIRECT16B = 16 bytes");
_Static_assert(sizeof(NEURON_ISA_TPB_INDIRECT20B)    == 20, "INDIRECT20B = 20 bytes");

#endif /* NEURON_ISA_TPB_H */
```

### A note on packing

The C structs above use natural alignment, which (because every field is byte / 2-byte / 4-byte and every offset is hand-checked) reproduces the wire layout **without implicit padding** for `TENSOR1-4D`, `MXMEM_PATTERN1D`, the indirect descriptors, and the family bundles. Two facts make this safe: (1) the `int16_t stride[]` / `uint16_t num[]` arrays are naturally 2-byte aligned and contiguous, exactly matching the encoder's `step@+4+2i` / `num@+(4+2N)+2i` stores; (2) the family-bundle bodies are explicit `uint8_t` arrays at the spill points, so no compiler ever inserts padding inside them. The emitter `memset`-zeroes all 64 bytes before field-filling, so any byte the C struct leaves implicit is a hard zero on the wire — *packed == wire*. For a strict ABI-portable build (where a compiler might over-align an aggregate), wrap each struct in `#pragma pack(push,1)` / `#pragma pack(pop)`; the wire format has no padding, so the packed form is byte-identical.

> **GOTCHA — `SRC = TENSOR*`, `DST = MEM_PATTERN*` is a role, not a layout.** The `MEM_PATTERN2D/3D/4D` types are `typedef`s of the `TENSOR2D/3D/4D` types — byte-for-byte identical. The fork is expressed in the binary in three non-byte places: the wire-struct **type name**, the dispatcher **assert** (`"ISA mem pattern is union but is missing field"` for DST vs `"static tensor pattern but has indirect field"` for SRC — both strings read from `libwalrus.so` this pass), and the validator **role flags** (`WR=1/PSUM=1/SBUF=0` for a PSUM dst). A reimplementer encodes both spellings identically; the type name only routes the operand to the read-role or write-role validator and tells the verifier which union member is present. [2.5 CONFIRMED]

> **QUIRK — there is no descriptor at bundle byte `+0x48`.** Earlier per-op field maps recurringly cite a `+0x48`. That is the *vtable* offset through which `setupHeader` is reached (`call [rax+0x48]`, slot 9, since `0x48/8 = 9`) — a **code** offset, not a bundle byte. The narrowest descriptor that could sit at bundle `+0x48` is a `TENSOR3D` (16 B), which would span `+0x48..+0x57` and overflow the 64-byte bundle (`0x58 > 0x40`). The "slots at `+16/+32/+48`" phrasing describes **Family B only** (`0x10/0x20/0x30`); it is not the universal layout. [2.1 §CORRECTION CONFIRMED]

---

## The op → family → slot-type map

The union discriminant is the opcode byte at `raw[0]`. The L2 validator dispatch (`runSingleISACheck → is_valid_neuron_engine_instruction → is_valid_<format>`) keys on the opcode to pick the per-format validator, which then reads the slots/control-band for *that* family. The descriptor a slot gets is the **role fork**: a READ operand (`getArgument(i)`) → `TENSORnD`; a WRITE operand (`getOutput(0)`) → `MEM_PATTERN_nD` ([2.8](ap-encoder-dispatch.md)).

| Op (opcode) | Family | Slots (offset : type) | Control band | Src page |
|---|---|---|---|---|
| Matmul (`0x02`) | A | src `TENSOR3D@+0x10` / dst `MP3D@+0x30` | `+0x20..+0x2F` | [2.5](mempattern-2d-3d.md) |
| LoadStationary (`0x01`) | A | weight stage `@+0x10` (no dst) | `+0x20..+0x2F` | [2.1](instruction-bundle.md) |
| MatmulMx (`0x100A`) | A / MX | `MXMEM1D@+0x10` / dst `MP3D@+0x30` | `+0x20..+0x2F` | [2.6](mxmem-pattern1d.md) |
| LdWeightMx (`0x1009`) | A / MX | `MXMEM1D@+0x10` (no dst) | — | [2.6](mxmem-pattern1d.md) |
| Activation (`0x21`/`*0x25`) | A | src `TENSOR3D@+0x10` / dst `MP3D@+0x30` | `+0x20..+0x2F` | [2.1](instruction-bundle.md) |
| TensorScalar (`0x43`..) | A | in `TENSOR3D@+0x10` / out `MP3D@+0x30` | `+0x20..+0x27` | [2.1](instruction-bundle.md) |
| TensorTensor (`0x41`/`0x51`) | B | dst `MP3D@+0x10` / in0 `T3D@+0x20` / in1 `T3D@+0x30` | `+0x0C..+0x0F` | [2.1](instruction-bundle.md) |
| TensorCopy (`0x46`/`0x47`) | C | src `TENSOR4D@+0x0C` / dst `MP4D@+0x2C` | `+0x20..+0x2B` | [2.4](tensor4d-mempattern4d.md) |
| Pool (`0x45`) | C | in `TENSOR4D@+0x0C` / out `MP4D@+0x2C` | `+0x20..+0x2B` | [2.1](instruction-bundle.md) |
| TensorReduce (var) | C | in/out `TENSOR4D @+0x0C/+0x2C` | `+0x20..+0x2B` | [2.4](tensor4d-mempattern4d.md) |
| Reciprocal / Iota / Memset (`0x49`) | C | out `@+0x2C` (Memset 1D `@+0x2C`) | `+0x20..+0x2B` | [2.1](instruction-bundle.md) |
| StreamShuffle / Transpose | C | data `TENSOR4D @+0x0C/+0x2C` | `+0x20..+0x2B` | [2.4](tensor4d-mempattern4d.md) |
| Gather (`0x68`) | C | out `@+0x2C`; indirect index/data slot | `+0x20..+0x2B` | [2.7](indirect-descriptors.md) |
| BNStats (`0x61`/`0x82`) | C mixed | in `4D@+0x0C` / out `2D@+0x30` | `+0x20..+0x2B` | [2.4](tensor4d-mempattern4d.md) |
| EventSemaphore (`0xA0`) | sync | predicate `@+0x04`, wait/act `@+0x20..+0x28` | — | [1.14](../arch/execution-sync-model.md) |
| CompareAndBranch (`0xA9`) | ctrl | comp `@+0x0C`, regs `@+0x20/+0x21`, PC `@+0x30` | — | [D-M10] |
| DMA DIRECT2D (`0xD4`) | D | `ADDR8` src `@+0x10` / dst `@+0x28` (distinct) | — | [2.1 §NOTE] |

> **NOTE —** the family membership of the byte-verified ops above is CONFIRMED; the family of the remaining ~90 ops in the [110-IT opcode table](../walrus/setupheader-opcode-table.md) is assigned by descriptor-width evidence in their per-engine pages (STRONG for the tail). The op→family rule itself — *width + operand count picks the slot offsets, the control band fills the gap* — is CONFIRMED across all four byte-verified families.

---

## The struct nesting + the union discriminant

```text
INST_UNION (64 B)
 |- HEADER (4 B @+0)  — opcode (discriminant @raw[0]), inst_word_len=0x10, reserved
 |- [optional] SYNC BAND (@+0x04..+0x0B) — present iff the inst carries SyncInfo
 |     (DVE/Pool/Act/SP/DMA); ABSENT on the matmul MM bundle.  [2.1 §sync]
 |- CONTROL BAND — position is FAMILY-FIXED:
 |     Family A/C: @+0x20..+0x2F  (between the two slots)
 |     Family B:   @+0x0C..+0x0F  (before the three contiguous slots)
 |- DESCRIPTOR SLOTS — each opens with ADDR4 @slot+0, then strides[], nums[]:
       Family A: src TENSOR3D @+0x10, dst MEM_PATTERN3D @+0x30
       Family B: dst MEM_PATTERN3D @+0x10, in0 TENSOR3D @+0x20, in1 TENSOR3D @+0x30
       Family C: in TENSOR4D @+0x0C, out MEM_PATTERN4D @+0x2C
       MX:       MXMEM_PATTERN1D @+0x10 (data ADDR4@+0 + scale ADDR4@+4 nested)
       indirect: a slot's ADDR4 carries bit29; the slot becomes INDIRECT16B/20B
                 or MXINDIRECT16B (a self-promotion inside the same slot leaf).
```

The 64 bytes are emitted as `std::array<u8,64>`: `emplace_back`, `memset`-zero, stamp the 4-byte header via `setupHeader` (vtable slot 9), field-fill the body, then `fwrite(bundle, 1, 0x40, …)`. `setupHeader` is byte-identical across CoreV2 (`0x1172120`), CoreV3 (`0x1369280`), CoreV4 (`0x143f440`); the six-instruction body `movzx eax,[rdx]; mov byte[rsi+1],0x10; mov[rsi],al; xor eax,eax; mov word[rsi+2],ax; ret` is byte-confirmed (`0f b6 02 c6 46 01 10 88 06 31 c0 66 89 46 02 c3`) directly from `libwalrus.so` this pass. [2.1 CONFIRMED]

---

## Reconciled disagreements

The header is the union of all eight sub-pages' byte-verified facts. The residual cross-page disagreements are resolved here; each was already fixed in place on the owning page, and the header adopts the corrected reading.

| ID | Reconciliation | Owning page |
|---|---|---|
| R-1 | **TensorScalar slots = `+0x10`/`+0x30` (Family A)**, NOT a "`+0x28` packing." The header places TS input `@+0x10` and output `@+0x30`, identical to matmul/activation. TS's control-band offsets (`+0x20/+0x21` dtype, `+0x24/+0x25` ALU, `+0x26` reverse) are retained. | [2.1 §CORRECTION](instruction-bundle.md) |
| R-2 | **`mem2d_valid`/`mem3d_valid` take the `MEM_PATTERN` (DST/general) type**, not "plain-tensor SRC." The header types `TENSORnD` and `MEM_PATTERNnD` as byte-identical `typedef`s — the type label is a routing hint, not a layout difference. | [2.5 §2.1](mempattern-2d-3d.md) |
| R-3 | **`MXMEM_PATTERN1D` TYPE = 16 bytes, 12 written, `+0xC..+0xF` reserved.** The header reserves all 16. (Earlier "12-byte MX" counted only the meaningful bytes.) | [2.6 §NOTE](mxmem-pattern1d.md) |
| R-4 | **`INDIRECT16B`'s indirect branch does NOT fill the `TENSOR3D` static region.** Bytes `+0xA..+0xF` are inert (`_inert[]`); `num@+8 = getNumIndirectIndices` (vtbl `+0x90`), NO ×4. | [2.7 §CORRECTION](indirect-descriptors.md) |
| R-5 | **No descriptor at bundle byte `+0x48`** (= `setupHeader` vtable slot). The three-contiguous-slot premise is **Family B only**. | [2.1 §CORRECTION](instruction-bundle.md) |
| R-6 | **PSUM bank in ADDR4 hi bits; `accumulate@+0x2B`, `zero-region@+0x2F`, `dst-dtype@+0x28` are bundle control-band bytes, NOT descriptor fields.** The matmul struct places them in the control band; the dst slot stays a plain `MEM_PATTERN3D`. | [2.5 §4](mempattern-2d-3d.md) |

> **CORRECTION (this page, R-1 adopted) —** the header's Family-A definition supersedes any earlier table that placed TensorScalar descriptors at `+0x28..+0x3F`. The slot offsets are `+0x10` (input) and `+0x30` (output), byte-verified at `generateTensorScalarOrPtr` (`lea [r14+0x10]` / `[r14+0x30]`). No new disagreement is introduced by this synthesis; every field traces to a CONFIRMED sub-page fact or a fresh binary check made this pass.

---

## Adversarial self-verification — the five strongest claims

1. **The `INST_UNION` is 64 B with `raw[0]` (opcode) as discriminant.** *Challenge*: could the length ever be non-16, breaking the fixed size? *Re-derive*: `setupHeader` writes `byte[1] = 0x10` as a hardcoded immediate (`c6 46 01 10` — `mov byte[rsi+1],0x10`) with zero data dependence, confirmed byte-exact at `0x1172120` this pass; the emit skeleton `memset`s 64 and `fwrite`s `0x40`. **Holds — CONFIRMED.**
2. **The three bundle-family slot offsets (A `0x10/0x30`, B `0x10/0x20/0x30`, C `0x0C/0x2C`).** *Challenge*: are these per-op or family-fixed? *Re-derive*: the witnesses are `lea [base+OFF]` immediately before `assignAccess<…>` — `generateMatMul` (`+0x10`/`+0x30`), `visitInstTensorTensor` (`+0x10`/`+0x20`/`+0x30`), `visitInstTensorCopy` (`+0x0C`/`+0x2C`), each cited in [2.1](instruction-bundle.md). The control band fills the one-descriptor gap each family leaves. **Holds — CONFIRMED.**
3. **Descriptor sizes 8/12/16/20.** *Challenge*: could a size be off by the partition word? *Re-derive*: the four `.rodata` asserts `"ISA mem pattern {1,2,3,4}D must have {8,12,16,20} bytes to encode"` are read directly from `libwalrus.so` this pass; `slot_size(N) = 4 + 4N` exactly. The partition dim folds into ADDR4 and is *not* a stride/num word. **Holds — CONFIRMED.**
4. **The op→family map.** *Challenge*: does matmul's dst really ride at `+0x30` as a `MEM_PATTERN3D`? *Re-derive*: `is_valid_matmul_regular` decodes `[rsp+0x160] = union+0x30` as the PSUM-dst slot and `union+0x28` as its dtype byte, validated by `mm_dst_mem3d_valid_nc_v4` (symbol present at `0x1447fe0`, confirmed in dynsym this pass). The accumulate/zero-region siblings at `+0x2B`/`+0x2F` are control-band, not descriptor, bytes. **Holds — CONFIRMED.**
5. **The enum ordinals.** *Challenge*: is the dtype wire-tag table right, and is `ISA_ADDR_INDIRECT = 0x20`? *Re-derive*: the dtype→wire-tag LUT at `.rodata 0x1dfbad0` reads `03 02 10 0d 0e 0e 03 0f 0e 0f 05 04 06 07 09 08 0a 0b 01 0c` this pass — index 2 → `0x10` (FP4-x4), index 6 → `0x03` (E8M0), 8/9 → `0x0E`/`0x0F`; the alignment classes follow. The ADDR4 mode nibble `{0x00,0x20,0x40,0x60}` is the `& 0x60` consumer plus the `or [slot+3],0x20` encoder stamp ([2.2](addr4.md)). **Holds — CONFIRMED.**

---

## Confidence ledger + gaps

**CONFIRMED** (consolidated from the eight byte-verified sub-pages + fresh binary checks this pass):

- the 64-byte header word `{opcode,0x10,0,0}`; `inst_word_len` always `0x10`; `setupHeader` byte-exact across V2/V3/V4 (`0x1172120` read this pass).
- `ADDR4` 32-bit bitfield (29-bit addr, region `25..28`, mode nibble, register bit31). [2.2]
- `TENSOR1/2/3/4D` + `MEM_PATTERN2/3/4D` = `4+4N`, separate stride/num arrays, `{1,1}` fill, no on-wire dim count, sizes 8/12/16/20 (the four `.rodata` asserts read this pass). [2.3/2.4/2.5]
- `MXMEM_PATTERN1D` 16-byte (12 written), data+scale ADDR4, x4-K, step-dir, scalePart; CoreV4-only; "MX Scale Tensor can only be in SB" assert read this pass. [2.6]
- the three indirect descriptors (bit29 marker, num source, 16/20-byte). [2.7]
- the three slot-offset families (A `0x10/0x30`, B `0x10/0x20/0x30`, C `0x0C/0x2C`). [2.1]
- the matmul control band (`dtype@+0x20`, `dst-dtype@+0x28`, `accumulate@+0x2B`, `zero-region@+0x2F`, row/col group). [2.5]
- the TensorTensor (B) control band `@+0x0C..+0x0F`; the Pool/Copy (C) band `@+0x20..+0x2B`. [2.1]
- the 64-byte semaphore record + embedded-events predicate `@+0x04`. [1.14]
- the control/branch bundle (`comp_op@+0x0C`, regs `@+0x20/+0x21`, PC `@+0x30`). [D-M10]
- the op→family→slot-type map for all byte-verified ops. [2.8]
- the dtype wire-tag enum + alignment (LUT `0x1dfbad0` read this pass). [D-D04]
- the recovered struct/field type names (all `NEURON_ISA_TPB_*` present in `nm -DC` this pass).

**STRONG:**

- family membership of the ops not individually disassembled (assigned by descriptor-width evidence in their per-engine pages).
- the low/high-anchor-straddles-control-band geometric rationale. [2.1 §STRONG]
- CoreV3 byte-identity of the families (vtable-reuse argument). [2.3]

**INFERRED:**

- the exact per-op meaning of every control-band byte for the un-disassembled Family-A/C ops (only the witnessed ops — matmul, activation, TS, TT, Pool, Copy, BNStats — are byte-pinned).

**GAPS:**

- **G1** — no compiled-`.neff` byte fixture was diffed in any source pass; the header is encoder-write + decoder-read derived (byte-exact both ways), not a captured wire dump.
- **G2** — the natural-alignment vs `#pragma pack(1)` choice is noted in §[A note on packing]; the wire has no padding, so packed == wire, but a portable build should pack explicitly.
- **G3 (SPECULATIVE)** — a fully generic tagged union spelling out every one of the ~110 op bodies is out of scope; the header gives the three families + sync + branch + MX, which structurally cover the whole compute/control/sync surface.

---

## Cross-References

- [2.1 The 64-Byte Instruction Bundle & Header Skeleton](instruction-bundle.md) — the universal header, the emit skeleton, and the three slot families this header types.
- [2.2 ADDR4 — the 32-Bit Address Word](addr4.md) — the `u32` start word that opens every descriptor slot; mode nibble, PSUM window, register mode.
- [2.3 TENSOR1D / 2D / 3D Descriptors — the 4+4N Rule](tensor-descriptors.md) — the SRC descriptor geometry the header's `TENSOR*` structs encode.
- [2.4 TENSOR4D / MEM_PATTERN4D — the Spill Descriptor](tensor4d-mempattern4d.md) — the 20-byte 4D case and the `+0xA→+0xC` count-base relocation.
- [2.5 MEM_PATTERN2D / 3D — the DST/PSUM Role](mempattern-2d-3d.md) — the DST byte-twin and the PSUM bank/accumulate/zero-region split.
- [2.6 MXMEM_PATTERN1D — MX Data + E8M0 Scale](mxmem-pattern1d.md) — the CoreV4 dual-ADDR4 MX descriptor at `bundle+0x10`.
- [2.7 Indirect-Gather Descriptors — INDIRECT16B / 20B / MXINDIRECT16B](indirect-descriptors.md) — the bit29-marked gather slots.
- [2.8 Access-Pattern Encoder Dispatch](ap-encoder-dispatch.md) — the role fork (`getArgument → TENSOR*`, `getOutput → MEM_PATTERN*`) that picks each slot's wire type.
- [ISA Enum Ordinals](isa-enum-ordinals.md) — the full dtype / opcode / ALU / mode enum tables *(2.23, planned)*.
- [Part 14 — ISA Quick-Reference Appendix](../appendix/) — the consolidated per-op offset cheat-sheet *(planned)*.
- [1.14 Execution & Sync Model](../arch/execution-sync-model.md) — the `+0x04..+0x0B` sync band and the semaphore record's wait/act semantics.
