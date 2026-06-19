# SparsityCompress / SparsityCompressTag

This page decodes the **SparsityCompress** and **SparsityCompressTag** kernels of the
NeuronCore GPSIMD ISA — the two **DVE-engine** (`engine_idx = 3`) *top-4 reduction-with-index*
handlers that take a dense input tensor, reduce every `op_dim`-subgroup to its **four
extreme elements** under a `min` / `max` / `abs_min` / `abs_max` relative reduction, and
emit those four winners **plus their four within-group 16-bit indices** ("tags"). The two
kernels share one algorithm and differ only in output packaging. This page pins the two
architectural opcodes (`0xE0`, `0xE1`), both 64-byte operand structs byte-for-byte from the
shipped ISA headers (compile-verified `sizeof == 64`), the top-4 selection datapath, the
**data + tag metadata format** the kernels write, and the *reconciliation with the PE
sparse-matmul consumer* — `MatmulSparse` (`0x07`), `LdTags` (`0x06`) and `Ldweights`
`tag_weight_mode`. The producer's `{value, 16-bit index}` pair **is** the unit the PE array
reads as `{compressed weight, fmap-select tag}`; the formats match end-to-end.

SparsityCompress(Tag) runs on the **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit
FLIX/VLIW DSP** (`ncore2gp` config, one per NeuronCore) — specifically on the **DVE**
(Dynamic Vector Engine) sequencer. The handler self-name strings (`SparsityCompress`,
`SparsityCompressTag`) live **only** in the DVE image string pool, and **only** from the
MARIANA (NC-v4) generation forward — they are wholly **absent** from the CAYMAN DVE image.
This places the on-device hardware compressor squarely on the MARIANA DVE, in deliberate
contrast to [Sort](./sort.md) (a POOL-engine value+index sort) and alongside
[NonzeroWithCount](./nonzero-with-count.md) (the POOL compaction sibling); the four winners
it emits feed the [PE matrix-multiply path](./pe-matmul.md). All three compaction kernels
compose from the same compare→`vbool` + relative reduce + lane-permute ISA primitives
surveyed in the [select/shuffle ISA batch](../../isa/ref/b21-select-shuffle.md).

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every host-ISA fact is read out of the public
`aws_neuron_isa_tpb_*.h` headers shipped in the customop-lib package and was
re-compile-verified this session; every device fact is byte-pinned to a carve from
`libnrtucode_internal.so`.

> **NOTE — what was carved this session, and the exact objects used.** The firmware
> container is `…/custom_op/c10/lib/libnrtucode_internal.so`
> (`sha256 b7c67e898a116454…fc329b`, ELF64 x86-64 DYN — the FW-49/60 anchor, re-verified
> in-task). The DVE images are identity-mapped `.rodata` blobs (VMA == file offset);
> IRAM file offset == device IRAM VA (reset vector at byte 0), and the DRAM string-file
> offset == device DRAM VA − `0x80000`. They are disassembled with the native
> `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, GNU Binutils 2.34 / Xtensa Tools 14.09)
> that ships inside the gpsimd-tools package. The recovered `S:`/handler-name strings and
> the `s3d3_sc.h` / `s2d2d2_sc.h` / `common.h` ISA structs are themselves binary evidence
> and are cited as such.
>
> | object | file off / size | sha256 | note |
> |---|---|---|---|
> | `MARIANA DVE DEBUG IRAM` | `0x408fc0` / `0x1c560` | `4c75ba8e…` | dispatch + L2 stubs + common entry |
> | `MARIANA DVE DEBUG DRAM` | `0x425520` / `0x7000` | `6f65a629…` | `SparsityCompress`@`+0x3023`, `SparsityCompressTag`@`+0x3053`; jump tables @`0x800`/`0xaec` |
> | `MARIANA DVE PERF IRAM`  | `0x31f5c0` / `0x13540` | `dd14c1e3…` | compute IVP primitives (PERF strips `S:`) |
> | `MARIANA DVE TEST IRAM`  | `0x38f080` / `0x13560` | `78922240…` | (diff baseline) |
> | `CAYMAN DVE DEBUG DRAM`  | `0x18b320` / `0x6d60`  | `c106642d…` | **0** sparsity/compress strings — the absence proof |
>
> `xtensa-elf-objdump --xtensa-core=ncore2gp` decodes all IRAM to real Q7/NX windowed-ABI
> + FLIX-VLIW (exit 0). `[HIGH/OBSERVED]`

---

## 1. The headline

SparsityCompress and SparsityCompressTag are two **first-class, named ISA instructions** —
not composed host helpers. Each has its own architectural opcode, its own 64-byte operand
struct, its own validity predicate, and its own self-named on-device kernel. Eight facts
pin them:

1. **Two opcodes: `SPARSITY_COMPRESS = 0xE0`, `SPARSITY_COMPRESS_TAG = 0xE1`.** From
   `common.h:300-301` (`NEURON_ISA_TPB_OPCODE_SPARSITY_COMPRESS = 0xe0, // Y` /
   `…_SPARSITY_COMPRESS_TAG = 0xe1, // Y`), the `instruction_mapping.json` `struct2opcode`
   bindings `NEURON_ISA_TPB_S3D3_SC_STRUCT → [NEURON_ISA_TPB_OPCODE_SPARSITY_COMPRESS]`
   and `NEURON_ISA_TPB_S2D2D2_SC_STRUCT → [NEURON_ISA_TPB_OPCODE_SPARSITY_COMPRESS_TAG]`,
   and `debug_assert.h:201-205`
   (`case …_SPARSITY_COMPRESS: check = dbg_is_valid_s3d3_sc(i,nc);` /
   `case …_SPARSITY_COMPRESS_TAG: check = dbg_is_valid_s2d2d2_sc(i, nc);`). The header
   comment at the `0xe0` slot is candid: *"we are out of contiguous opcode space for
   general vector instructions … jumping ahead to the 0xe0 space"*. `[HIGH/OBSERVED]`

2. **They are top-4 reductions, NOT 2:4 / N:M weight pruners.** Each streams a dense
   tensor and reduces every `op_dim`-subgroup to its **4** extreme elements; "compression"
   means *a group of N elements → 4 `{value, index}` pairs*. The header is explicit:
   *"This is a reduction instruction, outputting 4 elements plus location information from
   each subset of the input stream … Each reduced group will be reduced to 4 elements."*
   `[HIGH/OBSERVED]`

3. **They differ only in output packaging.** SparsityCompress (`S3D3_SC`) writes **one**
   3-D dst holding an *interleaved* `{data, tag}` struct array. SparsityCompressTag
   (`S2D2D2_SC`) writes **two** 2-D dsts — a separate data buffer and a separate
   (optionally bit-packed) tag buffer. The reduction algorithm is identical.
   `[HIGH/OBSERVED]`

4. **Both structs compile-verify `sizeof == 64`.** Built `verify.c` against
   `neuron_mariana_arch_isa/tpb` with host gcc this session:
   `sizeof(S3D3_SC_STRUCT) == 64`, `sizeof(S2D2D2_SC_STRUCT) == 64`,
   `sizeof(S3D3_SC_D16) == 2`, `sizeof(S3D3_SC_D32) == 4`. Every offset below is
   gcc-emitted. `[HIGH/OBSERVED]`

5. **The tag is a 16-bit within-group index.** *"save the 4 min/max/abs_min/abs_max
   elements along with their index (16 bits)."* For `SparsityCompressTag` with
   `packed_tags == 1`, four 4-bit tags are packed into each `u16` (lowest bits = first
   data element). `[HIGH/OBSERVED]`

6. **The CompressOp is *inverted* relative to the underlying ALU op.** `common.h:1100-1110`
   states the special encoding `CompressOp::Min = AluOp::Max`, `CompressOp::Max =
   AluOp::Min`, `AbsMin = AbsMax`, `AbsMax = AbsMin`, *"because of the way the DVE
   implements sort … letting the smaller element pass down the pipe."* This is the
   datapath fingerprint and explains the extremity-order write rule (fact 7).
   `[HIGH/OBSERVED]`

7. **Winners are written in extremity order.** *"max or abs_max: smallest written first;
   min or abs_min: largest written first."* `[HIGH/OBSERVED]`

8. **MARIANA-generation addition, gated on V4.** The validity predicates begin with
   `check_core_version(nc, NeuronCoreVersion::V4)`. SparsityCompress(Tag) is **present**
   on MARIANA + MAVERICK, **absent** on CAYMAN + SUNDA (no opcode, no struct headers, zero
   firmware strings). `[HIGH/OBSERVED]`

> **CORRECTION — the `s2d2d2_sc.h` byte-table comment is wrong; the C struct is right.**
> The human-readable byte table at the top of `aws_neuron_isa_tpb_s2d2d2_sc.h` lists
> **both** `data_dst_mem_pattern` and `tag_dst_mem_pattern` at `"(48-63)"` and types them
> `Tensor2d 16` — a copy/paste error (a 2-D mem-pattern is 12 bytes, not 16, and two of
> them cannot both occupy `48-63`). The actual `typedef struct` (lines 80-97) and the
> compile-verified `offsetof` place `data_dst_mem_pattern @ 40` (`40-51`) and
> `tag_dst_mem_pattern @ 52` (`52-63`), each a 12-byte `TENSOR2D`. **The struct, not the
> comment table, is authoritative.** `[HIGH/OBSERVED]`

---

## 2. Dispatch chain — byte-exact (MARIANA DVE DEBUG IRAM)

The DVE SEQ command stream is an **ASCII-opcode** stream: the dispatcher subtracts the
opcode base (`0x30`), bounds-checks, and indexes a per-image jump table. Both
SparsityCompress and SparsityCompressTag are reached through the same three-stage chain
(normalize → trampoline → uniform L2 stub → common SEQ entry → data-driven `callx8`).

### 2a. The normalized jump table

There are two dispatcher sites (the *HW-Decode* arm and the *Sunda*-descriptor arm); both
normalize the opcode and index a **187-entry** LE `u32` table:

```
site A (HW-Decode) 0x2f09:  addi a2,a2,-48        ; opcode -= 0x30
                            movi a3,186           ; bound = 187-1
                            bgeu a3,a2,<default>  ; out-of-range -> 0x31e3
                            const16 a3,0x800      ; table base (DRAM file off)
                            addx4 a2,a2,a3        ; &table[opcode-0x30]
                            l32i.n a2,[a2]        ; target = table[idx]
                            jx a2
site B (Sunda)     0x3715:  addi -48 ; movi a3,186 ; const16 a3,0xaec ; addx4 ; jx
```

`table = 187 × LE u32` at DRAM **file 0x800** (HW-Decode) / **0xaec** (Sunda);
`target = table[opcode - 0x30]`; the default (out-of-range) arm is `0x31e3` (HW-Decode) /
`0x39fa` (Sunda) and appears 105× in each table (only 7 real opcodes are populated in this
+7 MARIANA set). `[HIGH/OBSERVED]`

The seven MARIANA-new real opcode slots, read from **both** DRAM tables this session:

| opcode | ISA name (`common.h`) | HW-Dec target | Sunda target |
|---|---|---|---|
| `0x30` | `EXPONENTIAL` | `0x31aa` | `0x39c1` |
| `0x77` | `RAND_GET_STATE` | `0x3192` | `0x39a9` |
| `0x78` | `RAND_SET_STATE` | `0x319a` | `0x39b1` |
| **`0xE0`** | **`SPARSITY_COMPRESS`** | **`0x3182`** | **`0x3999`** |
| **`0xE1`** | **`SPARSITY_COMPRESS_TAG`** | **`0x318a`** | **`0x39a1`** |
| `0xE2` | `RAND2` | `0x31a2` | `0x39b9` |
| `0xE3` | `QUANTIZE_MX` | `0x317a` | `0x3991` |

Byte read at `0x800 + 176*4 = 0xac0` is `82 31 00 00` → `0x3182`; at `0xac4` is
`8a 31 00 00` → `0x318a`; the Sunda table at `0xaec + 176*4` is `99 39 00 00` → `0x3999`,
at `0xaec + 177*4` is `a1 39 00 00` → `0x39a1`. `[HIGH/OBSERVED]`

> **GOTCHA — the trampoline region desyncs under a linear FLIX sweep.** The 187 jump-table
> targets land in a region of densely packed 5-byte trampolines (`call8` is 3 bytes + the
> `j` is 2). A linear disassembly from `0x3182` mis-aligns at `0x3188` and prints spurious
> bundles. Every clean fact here comes from disassembling **each target at its own aligned
> start** plus the raw DRAM table read — never the desynced sweep. `[MED]`

### 2b. Trampolines → L2 stubs → common entry

Decoded at their own aligned starts:

```
0x3182:  call8 0x22b4 ; j 0x31ee     (-> j 0x2e2b epilogue)   = SparsityCompress
0x318a:  call8 0x22d0 ; j 0x31ee                              = SparsityCompressTag
```

Each L2 stub is the uniform SEQ command-descriptor wrapper. The per-kernel `const16 a2`
immediate is a signed-negative descriptor-table offset stashed into the command frame at
`[a1+12]`:

```
0x22b4 (SC):   entry a1,48
               const16 a2,0xee90           ; SC descriptor offset
               s32i  a2,[a1+12]            ; stash into command frame
               l32r  a11,<desc>
               l32r  a2,<name = 0x83023 = "SparsityCompress">
               call8 0x9920               ; common SEQ handler entry
               retw
0x22d0 (SCT):  entry a1,48
               const16 a2,0xf29c           ; SCT descriptor offset
               s32i  a2,[a1+12]
               l32r  a11,<desc>
               l32r  a2,<name = 0x83053 = "SparsityCompressTag">
               call8 0x9920
               retw
```

The per-kernel `const16 a2` immediate (`SC = 0xee90`, `SCT = 0xf29c`; the siblings:
`Exp 0xf698`, `RandGet 0xec44`, `RandSet 0xebe0`, `Rand2 0xf2cc`, `QuantizeMx 0xfaf8`) is
the only static difference between the two stubs besides the name pointer. `[HIGH/OBSERVED]`

### 2c. The common entry `0x9920` is a data-driven `callx8`

`0x9920` is the shared SEQ handler entry. It reloads the stashed descriptor pointer from
`[a1+12]`, dereferences word0 to a function pointer, and `callx8`-es it:

```c
// common SEQ handler entry @ 0x9920 (MARIANA DVE DEBUG IRAM) — reconstructed from disasm
void seq_handler_entry_0x9920(seq_frame_t *a1 /* command frame */) {
    // ... entry a1,48 ; s32i a2,[a1+12] (descriptor already stashed by the L2 stub) ...
    // DEBUG build interleaves a trace helper here:
    debug_log_0x3aa0(/* … */);                  // call8 0x3aa0 — DEBUG image only
    void  *desc = *(void **)((char *)a1 + 12);  // l32i a2,[a1+12] -> descriptor ptr
    void (*body)(void) = *(void (**)(void))desc; // l32i.n a2,[a2,0] -> word0 = fn ptr
    body();                                      // callx8 a2 — the per-kernel math body
}
```

> **GOTCHA — the static path terminates at an indirect call.** The concrete compute target
> is a **runtime descriptor word** (the SEQ `kernel_info` model, same as the
> [DVE dispatch template](./pe-matmul.md)). The chain to `callx8` is HIGH/OBSERVED, but the
> per-kernel body reached through it is data-driven, so a clean per-handler CFG separating
> "SC body" from "SCT body" was **not** statically obtained — see §5. The DEBUG build adds
> an interleaved `call8 0x3aa0` trace helper before the deref+`callx8`; the
> load-desc → deref-word0 → `callx8` mechanism is otherwise exactly as described. `[HIGH]`

---

## 3. Operand structs — compile-verified

Both structs were compiled against `neuron_mariana_arch_isa/tpb` with host gcc this
session; every offset and size below is `offsetof`/`sizeof`-emitted. `[HIGH/OBSERVED]`

### 3a. SparsityCompress — `NEURON_ISA_TPB_S3D3_SC_STRUCT` (opcode `0xE0`, 64 B)

One 3-D source, **one** 3-D dst (interleaved `{data, tag}`):

| off | field | type | note |
|---|---|---|---|
| 0 | `header` | `Header` (4) | opcode at byte 0 |
| 4 | `events` | `Events` (8) | |
| 12 | `in_dtype` | `Dtype` (1) | any normal dtype; **FP32R disallowed** |
| 13 | `transpose_src` | `u8` | `1` ⇒ 32×32-group transpose of the src stream |
| 14 | `reserved0[2]` | — | must be `0` |
| 16 | `src_mem_pattern` | `Tensor3d` (16) | one 3-D source |
| 32 | `out_data_dtype` | `Dtype` (1) | `fp8_e3/e4/e5` **or** `bf16/fp16` (+ReLU variants) |
| 33 | `out_struct_dtype` | `Dtype` (1) | `u16` (1-byte data) or `u32` (2-byte data) |
| 34 | `num_active_channels` | `u8` | `≤ DVE_NUM_CHANNELS (= 128)` |
| 35 | `reserved1[1]` | — | `0` |
| 36 | `op` | `CompressOp` (1) | `MIN`/`MAX`/`ABS_MIN`/`ABS_MAX` |
| 37 | `op_dim` | `TensorSubdim` (1) | `X`/`XY`/`XYZ`/`XYZW` (reduced subgroup) |
| 38 | `reserved2[10]` | — | `0` (the inline `// 11` comment is a typo; `38..47` = 10 B) |
| 48 | `dst_mem_pattern` | `Tensor3d` (16) | one 3-D dst, interleaved `{data, tag}` |

The interleaved output element is one of two structs, selected by `out_data_dtype`:

```c
typedef struct NEURON_ISA_TPB_S3D3_SC_D16 {  // fp8 output — sizeof == 2
    uint8_t  data;   // u8 representing any 8-bit dtype
    uint8_t  tag;    // 8-bit within-group index
} NEURON_ISA_TPB_S3D3_SC_D16;

typedef struct NEURON_ISA_TPB_S3D3_SC_D32 {  // bf16/fp16 output — sizeof == 4
    uint16_t data;   // u16 representing any 16-bit dtype
    uint16_t tag;    // 16-bit within-group index
} NEURON_ISA_TPB_S3D3_SC_D32;
```

> **NOTE — `out_struct_dtype` is redundant-but-mandatory.** The header states it is
> *"redundant information as the size will be constrained by `out_data_type`, but useful in
> the instruction as this specifies the element size used by the `dst_tensor`."* The
> validity predicate `is_valid_sparsity_compress_dtypes` enforces the pairing exactly:
> `{fp8_e3,fp8_e4,fp8_e5} ⇒ out_struct == u16`, `{bf16,fp16} ⇒ out_struct == u32`. So the
> dst memory-pattern stride is sized by the *struct* dtype (2 or 4 bytes), while the *data*
> byte inside it is sized by `out_data_dtype`. `[HIGH/OBSERVED]`

### 3b. SparsityCompressTag — `NEURON_ISA_TPB_S2D2D2_SC_STRUCT` (opcode `0xE1`, 64 B)

One 2-D source, **two** separate 2-D dsts (data + tag):

| off | field | type | note |
|---|---|---|---|
| 0 | `header` | `Header` (4) | |
| 4 | `events` | `Events` (8) | |
| 12 | `in_dtype` | `Dtype` (1) | any normal; **FP32R disallowed** |
| 13 | `transpose_src` | `u8` | `1` ⇒ 32×32-group transpose |
| 14 | `packed_tags` | `u8` | `0` or `1` (tag bit-packing select) |
| 15 | `reserved0[5]` | — | `0` |
| 20 | `src_mem_pattern` | `Tensor2d` (12) | one 2-D source |
| 32 | `out_data_dtype` | `Dtype` (1) | any normal (**FP32R allowed here**) |
| 33 | `out_tag_dtype` | `Dtype` (1) | `u8`/`u16`/`u32` (unpacked) \| `u16` only (packed) |
| 34 | `num_active_channels` | `u8` | `≤ 128` |
| 35 | `reserved1[1]` | — | `0` |
| 36 | `op` | `CompressOp` (1) | `MIN`/`MAX`/`ABS_MIN`/`ABS_MAX` |
| 37 | `op_dim` | `TensorSubdim` (1) | `X`/`XY` (2-D only) |
| 38 | `reserved2[2]` | — | `0` |
| **40** | **`data_dst_mem_pattern`** | `Tensor2d` (12) | **separate** data dst (`40-51`) |
| **52** | **`tag_dst_mem_pattern`** | `Tensor2d` (12) | **separate** tag dst (`52-63`) |

> **QUIRK — the tag dst is SBUF-only, and both dsts must share a quadrant.**
> `tensor2d_valid(tag_dst, …, AllowedInPSUM::False, AllowedInSBUF::True)`: the tag buffer
> **cannot** live in PSUM, only SBUF. And `has_valid_sparsity_compress_tag_dst_quadrants`
> requires `data_dst` and `tag_dst` to lie in the **same PSUM quadrant** (if both PSUM) or
> the **same SBUF quadrant** (if both SBUF). Combined with the SBUF-only tag rule, the
> practical layout is: both buffers in the same SBUF quadrant. `[HIGH/OBSERVED]`

### 3c. Enum values (gcc-emitted)

| enum | values |
|---|---|
| `Opcode` | `SPARSITY_COMPRESS = 0xE0`, `SPARSITY_COMPRESS_TAG = 0xE1`, `LDTAGS = 0x06`, `MATMUL_SPARSE = 0x07`, `RAND_GET_STATE = 0x77`, `RAND_SET_STATE = 0x78`, `RAND2 = 0xE2`, `QUANTIZE_MX = 0xE3`, `EXPONENTIAL = 0x30` |
| `CompressOp` | `MIN = 0x08`, `MAX = 0x09`, `ABS_MIN = 0x20`, `ABS_MAX = 0x21` |
| `TensorSubdim` | `UNUSED = 0x00`, `X = 0x02`, `XY = 0x03`, `XYZ = 0x04`, `XYZW = 0x05` |
| `Dtype` | `FP8_EXP3 = 0xD`, `FP8_EXP4 = 0xE`, `FP8_EXP5 = 0xF`, `FP16 = 0x7`, `BFLOAT16 = 0x6`, `FP32R = 0xB`, `UINT8 = 0x3`, `UINT16 = 0x5`, `UINT32 = 0x9` |
| `TagWgtMode` | `WEIGHT_ONLY = 0`, `TAG_WEIGHT = 1` |
| const | `DVE_NUM_CHANNELS = 128` |

> **GOTCHA — `CompressOp` does NOT use the `AluOp::ABS_*` numbering.** The `AluOp` enum has
> `ABS_MAX = 0x20`, `ABS_MIN = 0x21`, but `CompressOp::ABS_MIN = 0x20` and
> `CompressOp::ABS_MAX = 0x21` — the *swap* of §4. Do not cross-wire the two enums.
> `[HIGH/OBSERVED]`

---

## 4. The compression algorithm

Both kernels share one reduction algorithm, read verbatim from the `s3d3_sc.h` /
`s2d2d2_sc.h` "Behavior" block and the validity predicates. `[HIGH/OBSERVED header]`

### 4a. The pipeline

```c
// SparsityCompress / SparsityCompressTag — top-4 relative reduction with within-group index.
// Reconstructed from the s3d3_sc.h / s2d2d2_sc.h "Behavior" spec + the CompressOp encoding.
// op ∈ {MIN, MAX, ABS_MIN, ABS_MAX}; op_dim selects the reduced subgroup size.
void sparsity_compress(const stream_t *src, sc_params_t *p, dst_t *out) {
    // (1) STREAM in a single dense element stream from src_mem_pattern.
    // (2) OPTIONAL 32x32-group transpose (transpose_src == 1; gated on
    //     num_active_channels being a multiple of 32 AND an exact 32x32 element count).
    // (3) CONVERT each element to fp32 internally (regardless of in_dtype).
    for (each op_dim_subgroup G in src) {     // X / XY / XYZ / XYZW
        // reduce_dim_size_check(., op_dim, 4): |G| must be >= 4.
        winner_t w[4];                         // 4 winners with their within-group index

        // (4) REDUCE G to its 4 extreme elements under the *relative* op.
        //     KEY SEMANTIC INVERSION (common.h:1100): the ALU runs the OPPOSITE op so the
        //     LOSING element passes down the sorting pipe first:
        //       CompressOp::MAX  -> AluOp::MIN     (find the 4 largest)
        //       CompressOp::MIN  -> AluOp::MAX     (find the 4 smallest)
        //       CompressOp::ABS* -> swapped likewise
        //     Equivalent to a partial selection sort keeping the top-4 by |op|.
        top4_select_with_index(G, p->op, /*out*/ w);  // 4 {value, idx16} pairs

        // (5) RECORD each winner's 16-bit WITHIN-GROUP index (0..|G|-1) as its tag.
        // (6) WRITE the 4 winners in EXTREMITY ORDER:
        //       MAX / ABS_MAX -> SMALLEST winner written first
        //       MIN / ABS_MIN -> LARGEST  winner written first
        emit_4_winners_extremity_ordered(out, w, p);   // §5: interleaved or split packaging
    }
}
```

### 4b. Validation (the predicate, not the body)

| predicate | meaning |
|---|---|
| `check_core_version(nc, V4)` | MARIANA-and-later only (the architectural lock) |
| `reduce_dim_size_check(.,op_dim,4)` | the reduced dim must hold **≥ 4** elements |
| `reduce_dst_num_elements(.,4)` | exactly **4** outputs per group |
| `is_valid_dtype(in_dtype, AllowFP32R::False)` | input may be any normal dtype except FP32R |
| `has_*_reserved_zero(i)` | every reserved byte must be `0` |
| `has_valid_active_channel_range(.,128)` | `num_active_channels ≤ DVE_NUM_CHANNELS (128)` |
| `is_valid_subdim_3d` / `is_valid_subdim_2d` | SC allows `X/XY/XYZ/XYZW`; SCT allows `X/XY` |
| `is_valid_*_transpose_src` | `transpose_src == 1` requires a 32-channel multiple + 32×32 element count |

### 4c. The dtype matrix

**SparsityCompress** (`is_valid_sparsity_compress_dtypes`):

| `out_data_dtype` | `out_struct_dtype` | output element |
|---|---|---|
| `fp8_e3` / `fp8_e4` / `fp8_e5` | `u16` | `S3D3_SC_D16 = { u8 data, u8 tag }` (2 B) |
| `bf16` / `fp16` | `u32` | `S3D3_SC_D32 = { u16 data, u16 tag }` (4 B) |

`in_dtype` may be any normal type (FP32R disallowed). `[HIGH/OBSERVED]`

**SparsityCompressTag** (`is_valid_sparsity_compress_tag_dtypes`):

| `packed_tags` | `out_tag_dtype` | tag-buffer element count |
|---|---|---|
| `0` | `u8` \| `u16` \| `u32` | one tag per data element (`same_element_count`) |
| `1` | **`u16` only** | `data_count / 4` (`quadruple_element_count`: 4 × 4-bit tags per `u16`) |

For `SparsityCompressTag`, `out_data_dtype` may be **any** normal type including FP32R
(unlike SparsityCompress, which is restricted to fp8/bf16/fp16 data). `[HIGH/OBSERVED]`

### 4d. Firmware fingerprint — the IVP compute primitives

The MARIANA-new IVP vector ops realizing the above were tallied from the PERF IRAM disasm.
As a *coherent set* they are **absent** on the CAYMAN DVE — corroborating that the
compressor is a MARIANA addition independent of the host headers:

| role | IVP ops |
|---|---|
| stream load | `ivp_lvn_2x16u_i`, `ivp_lvn_2x16s_i` |
| compare→`vbool` (relative mask, ISS-06) | `ivp_eq2nx8`, `ivp_uneqnxf16t` |
| relative reduce min/max (the `CompressOp` core) | `ivp_rbminnumnxf16t`, `ivp_bmaxnx16`, `ivp_rbmaxn_2x32`, `ivp_bminn_2x32`, `ivp_minunx16t`/`maxunx16t` |
| fused min-OR-max select (the op selector) | `ivp_minormax2nx8`, `ivp_minormaxu2nx8` |
| abs reduce (for ABS_MIN/ABS_MAX) | `ivp_babssub{,u}{2nx8,nx16}`, `ivp_absn_2xf32t` |
| lane-compaction / index pack (ISS-08) | `ivp_sel2nx8i_s4`, `ivp_dselnx16t`, `ivp_shfl2nx8i`, `ivp_seln_2x32t` |

A clean representative FLIX bundle (PERF IRAM `0x3503`) fuses load + compare→`vbool` +
relative-reduce-min + reduce-max in a single 32 B bundle:

```
{ ivp_rep2nx8t       v20,v2,22,vb1
  ivp_lvn_2x16u_i    v8,a0,0x200          ; stream load
  ivp_eq2nx8         vb6,v22,v26          ; compare -> vbool
  ivp_rbminnumnxf16t vb1,v3,v0,vb1        ; relative reduce min
  ivp_bmaxnx16       vb5,v1,v15,v24 }     ; reduce max
```

This is the exact `(convert)/compare/reduce` skeleton the ISA behaviour describes. The
index of each winner is produced by the **in-lane reduce+select** path (the `vbool`
predicate carries the winning lane forward), **not** by a SuperGather drain — the gather
register file (`gr0..gr7`, ISS-05) does not appear in the bundle. `[IVP ops HIGH/OBSERVED;
the in-lane-vs-gather attribution MED/INFERRED]`

---

## 5. SparsityCompress vs SparsityCompressTag — the distinction

| | **SparsityCompress** (`0xE0`) | **SparsityCompressTag** (`0xE1`) |
|---|---|---|
| struct | `S3D3_SC` (`s3d3_sc.h`) | `S2D2D2_SC` (`s2d2d2_sc.h`) |
| src | one 3-D tensor | one 2-D tensor |
| dst | **one** tensor (interleaved) | **two** tensors (data + tag) |
| output element | `{data, tag}` struct (`D16`/`D32`) | data stream + tag stream |
| tag packaging | inline beside each datum | separate; opt. 4 × 4-bit → `u16` |
| `out_struct`/`out_tag` | `u16` (1-B data) / `u32` (2-B data) | `u8`/`u16`/`u32`, or `u16` if packed |
| `out_data` dtype | `fp8_e3/e4/e5` \| `bf16/fp16` | any normal (**FP32R allowed**) |
| `op_dim` reach | `X/XY/XYZ/XYZW` (3-D) | `X/XY` (2-D) |
| algorithm | identical top-4 `min/max/abs` reduction + 16-bit index | identical |
| dst placement rule | (single dst) | `data_dst` & `tag_dst` same quadrant; `tag_dst` SBUF-only |

**Use:** SparsityCompress emits a compact `{value, index}` stream where the consumer reads
an interleaved struct — precisely the `S3D3_SC_D32 = {u16 data, u16 tag}` layout that
`Ldweights` in `tag_weight_mode` loads (§6c). SparsityCompressTag separates the tags into
their own (optionally bit-packed) buffer — precisely the 4 × 4-bit-per-`u16` layout that
`LdTags` loads into the PE array (§6b). The packaging choice is *which PE load instruction*
will consume the output.

> **GOTCHA — the SC/SCT firmware *body* split could not be statically separated.** The
> per-kernel math is reached via the §2c descriptor `callx8`, which is data-driven; the
> PERF/TEST IRAM also desync under FLIX-VLIW bundling. The two kernels' *algorithmic*
> difference is established from the two distinct 64-byte ISA structs (compile-verified) +
> the two distinct DEBUG `S:` names + the two distinct dispatch trampolines
> (`0xE0 → 0x3182`, `0xE1 → 0x318a`). The shared IVP reduce/select primitives are OBSERVED
> in the image; their *split* across the two handler bodies is INFERRED. `[structs/names/
> dispatch HIGH; body split MED]`

---

## 6. Tag format reconciled with the PE consumer

This is the must-match. The DVE producer emits `{value, index}`; the PE array consumes
`{compressed weight, fmap-select tag}`. The PE consumer (`MatmulSparse 0x07`, `LdTags
0x06`) exists on **CAYMAN, MARIANA, MAVERICK** — i.e. it predates the on-device producer.
On CAYMAN the tags had to be built by host/sw; MARIANA added the on-device DVE producer.

### 6a. `MatmulSparse` (`S4D3_MM`, opcode `0x07`) — the tag is a per-cell fmap selector

`MatmulSparse` is *"the version of Matmul with fine grain sparsity enabled … it sends
2/3/4 fmaps per PE partition across the PE array where metadata on the weights will select
which fmap to use for any PE array cell."* `src.w_num` (= `num_elem[3]`) selects 4/3/2
fmaps per partition per cycle; each cell uses its loaded **tag** to pick among them:

| PE row,col tile | fmaps per cell | tag → fmap selection |
|---|---|---|
| 128×128 | 16 | tag at row 0/32/64/96 selects from 16 fmaps |
| 64×128 | 8 | tag at row 0/32 → fmaps 0-7; row 64/96 → 8-15 (set bit[3] in LdTags) |
| 32×128 | 4 | row 0 → 0-3, row 32 → 4-7, row 64 → 8-11, row 96 → 12-15 |

> **NOTE — under-full groups are unpredictable.** *"if `src_mem_pattern.num_elem[3] < 4`,
> then 3 or 2 fmaps will be sent across each row … The tags loaded for that sparse matmul
> should only select from the available 2 or 3 fmaps. If the unused fmaps are selected by
> the compressed_weight tags, the results will be unpredictable."* The producer's top-4
> reduction must therefore be matched to the consumer's `w_num`. `[HIGH/OBSERVED]`

### 6b. `LdTags` (`S3_LT`, opcode `0x06`) — packs 4 tags per `u16`, mirrors SCT `packed_tags=1`

`LdTags` (`sizeof == 64`) loads the tags into the array. Its header pins the packing
*exactly* the same as SparsityCompressTag's `packed_tags == 1`:

- *"The 4-bit tags are packed 4 tags to a 16-bit unsigned integer. uint16 is therefore the
  only legal dtype."* (`s3_lt.in_dtype` must be `UINT16`.)
- *"Within a `uint16` element, the least significant four bits correspond to the tag loaded
  into the larger logical PE column ID … least significant four bits are loaded into column
  127, while most significant four bits are loaded into column 124."*
- Tags are loaded in **reversed** order, like the weights (negative compiler strides).

`force_tag_bits` (offset 33) is OR'd into the low 4 bits of every tag and selects the
row-tile fmap group — the byte-exact table from the header:

| row tile | tag bits from data | `force_tag_bits` |
|---|---|---|
| 0-127 | `4'b____` | `0x00` |
| 0-63 / 64-127 (8:2) | `4'b0___` / `4'b1___` | `0x00` / `0x08` (sets tag bit[3]) |
| 0-31 / 32-63 / 64-95 / 96-127 (4:1) | `4'b00__` … `4'b11__` | `0x00` / `0x04` / `0x08` / `0x0C` |

Plus `col_grp = 0x0f` (offset 45 — *"Fine Grain sparsity only works across the full array,
all 4 column groups"*), `num_active_rows` (38), `row_grp` (44, a one-hot/two-hot row-group
mask), `active_row_mode` (46, logical-vs-physical top rows). The legal `force_tag_bits`
values are `{0x00, 0x04, 0x08, 0x0C}` exactly. `[HIGH/OBSERVED]`

### 6c. `Ldweights` (`S3_LW`) `tag_weight_mode` — interleaved `{weight, tag}` mirrors `S3D3_SC_D32`

`Ldweights` (`sizeof == 64`) flags byte (offset 47, bit 0) carries `tag_weight_mode`
(`WEIGHT_ONLY = 0` / `TAG_WEIGHT = 1`). In `TAG_WEIGHT` mode:

- `in_dtype` must be **BF16 or FP16**;
- the source tensor is validated as a **`UINT32`** memory pattern
  (`mem3d_valid(src, Dtype::UINT32, …)`) — i.e. each element is a 32-bit `{16-bit weight,
  16-bit tag}` pair;
- `transpose_mode` must be `Disabled` and `perf_opt` must be `None`.

A 32-bit `{u16 weight, u16 tag}` element is **bit-identical** to the
`NEURON_ISA_TPB_S3D3_SC_D32 = { uint16_t data; uint16_t tag; }` element that
**SparsityCompress emits for bf16/fp16 output**. The producer's interleaved struct and the
consumer's `TAG_WEIGHT` load are the same 4-byte layout. `[HIGH/OBSERVED]`

### 6d. Reconciliation verdict

The DVE producer's `{value, 16-bit index}` pair **is** the unit the PE consumer reads as
`{compressed weight, fmap-select tag}`. The 4-winners-per-group reduction **is** the "fine
grain sparsity" (4-of-N) the MatmulSparse header names. The two tag encodings are the two
ends of the same index:

- **raw 16-bit** (SC `D32` / SCT unpacked `u16`) — the full within-group index;
- **packed 4-bit** (SCT `packed_tags=1`, 4 per `u16`, LSB = first element) — exactly the
  `LdTags` packing (4 per `u16`, LSB → column 127), with `force_tag_bits` OR-ing the high
  bits to extend the 4-bit index across the 8:2 / 4:1 row tiles.

Producer and consumer formats **match**. `[HIGH]`

---

## 7. Per-generation presence

Checked across all four shipped `arch_isa` header sets (opcode in `common.h`, presence of
`s3d3_sc.h` / `s2d2d2_sc.h`) **and** the firmware DRAM string byte-check:

| arch | `common.h` SC opcode | `s3d3_sc.h` | `s2d2d2_sc.h` | DVE DEBUG-DRAM strings | PE consumer (`0x07`/`0x06`) |
|---|---|---|---|---|---|
| **CAYMAN** (NC-v2) | absent | NO | NO | 0 sparsity strings (`c106642d…`) | **PRESENT** |
| **SUNDA** | absent | NO | NO | (no sparse PE either) | **ABSENT** |
| **MARIANA** (NC-v4) | `0xE0` | yes | yes | `SparsityCompress` + `…Tag` (`6f65a629…`) | **PRESENT** |
| **MAVERICK** (NC-v5) | `0xE0` | yes | yes | (inherited) | **PRESENT** |

The DVE hardware compressor is a **MARIANA-generation addition**. CAYMAN's PE could already
consume sparse weights + tags (`MatmulSparse 0x07` / `LdTags 0x06` are present), but had
**no on-device DVE producer** — the tags were built by host software. SUNDA is a separate
lineage with neither side. The `check_core_version(nc, V4)` gate in both validity
predicates is the architectural lock. `[HIGH/OBSERVED]`

> **CORRECTION — MAVERICK (NC-v5) is NOT a byte-identical inherit; it adds a tile flag.**
> The MAVERICK `s3d3_sc.h` / `s2d2d2_sc.h` differ from MARIANA in two ways: (1) the header
> banner reads *"ISA header for NC-v5"*; (2) the active-channel-range check is swapped from
> `has_valid_active_channel_range(num_active_channels, DVE_NUM_CHANNELS)` to
> `has_valid_active_channel_range_with_tile(num_active_channels, DVE_NUM_CHANNELS,
> header.inst_flags)` — i.e. v5 admits a per-instruction **tile flag** modulating the
> channel range. The struct layout, opcode, CompressOp/tag semantics, and the dtype matrix
> are otherwise unchanged. Per the standing v5 rule, the *header* difference is OBSERVED but
> the v5 firmware interior (no MAVERICK carve was decoded — MAVERICK folds ACT into DVE) is
> INFERRED. `[header diff HIGH/OBSERVED; v5 interior INFERRED]`

---

## 8. Honesty ledger

**HIGH / OBSERVED:**

- Both kernels exist on MARIANA DVE (`SparsityCompress` @ DRAM file `0x3023`,
  `SparsityCompressTag` @ `0x3053`, with the `S: ` literal at `0x3065`); **zero** on CAYMAN
  DVE (`c106642d` byte-checked).
- Dispatch byte-exact: `0xE0 → 0x3182 → call8 0x22b4 → 0x9920` (SC),
  `0xE1 → 0x318a → call8 0x22d0 → 0x9920` (SCT); HW-Decode table @ DRAM `0x800`, Sunda table
  @ `0xaec`, `idx = opcode − 0x30`, 187 entries, default `0x31e3`/`0x39fa`. The seven new
  real opcodes (`0x30/77/78/e0/e1/e2/e3`) read from both DRAM tables. The L2 stub
  immediates `SC = 0xee90` / `SCT = 0xf29c` decoded aligned; `0x9920` loads the descriptor
  ptr `[a1+12]`, derefs word0, `callx8` (DEBUG build interleaves a `call8 0x3aa0` trace
  helper).
- Operand structs compile-verified (gcc): `S3D3_SC` 64 B (interleaved `{data,tag}`
  `D16`/`D32`), `S2D2D2_SC` 64 B (`data_dst @ 40`, `tag_dst @ 52`). `LdTags S3_LT` 64 B
  (`force_tag_bits @ 33`, `col_grp @ 45`), `Ldweights S3_LW` 64 B (`flags @ 47`). Opcodes
  and enums gcc-emitted.
- The algorithm (stream / opt-32×32-transpose / convert-fp32 / reduce-4-extremes /
  record-16b-index / extremity-order-write) + the `CompressOp ↔ AluOp` inversion + the
  dtype matrix + the packed-tag `data_count/4` rule + the dst-quadrant rule, all read from
  the ISA-header behaviour spec and predicates.
- Per-gen presence: SC/SCT absent CAYMAN+SUNDA, present MARIANA+MAVERICK; PE
  `MatmulSparse`/`LdTags` present CAYMAN/MARIANA/MAVERICK, absent SUNDA.
- Tag-format reconciliation: producer `{value,index}` == consumer `{weight, fmap-select
  tag}`; `LdTags` packs 4 × 4-bit per `u16` (LSB → col 127) exactly as SCT `packed_tags=1`
  (LSB = first element); `force_tag_bits` `{0x00,0x04,0x08,0x0C}` extends the high bits;
  `Ldweights TAG_WEIGHT` reads a `UINT32` `{u16 weight, u16 tag}` element == `S3D3_SC_D32`.
- MARIANA-new compute IVP ops present with a byte-exact representative FLIX bundle.

**MED / INFERRED:**

- The split of the shared reduce/select IVP body between the SC and SCT handlers (the
  descriptor `callx8` is data-driven; PERF/TEST FLIX-desync). The two kernels' difference
  rests on the two distinct 64-B structs + names + dispatch trampolines.
- The exact firmware bit-layout of the 16-bit tag (which bits = index vs which OR'd by
  `force_tag_bits`) is read from the `LdTags` doc table, not from a firmware bit-field
  decode.
- The compaction uses in-lane reduce+select, **not** SuperGather (`gr0..gr7` absent from
  the bundle) — inferred from the bundle composition.
- The MAVERICK firmware interior (`has_valid_active_channel_range_with_tile` behaviour) —
  the header diff is OBSERVED; no MAVERICK carve was decoded.

**LOW / NOT CLAIMED:**

- The exact descriptor-table layout the `const16 a2` offsets (`SC 0xee90` / `SCT 0xf29c`)
  index into.
- Per-instruction latency / FLIX-slot assignment of the compress IVP ops.

---

## 9. Cross-references

- [PE Matrix-Multiply Path](./pe-matmul.md) — the sparse-matmul **consumer**
  (`MatmulSparse 0x07`, `LdTags 0x06`, `Ldweights tag_weight_mode`) that reads the tags this
  kernel produces.
- [Sort / DECODE_SORT](./sort.md) — the POOL-engine value+index sort; the `CompressOp`
  encoding note (*"the way the DVE implements sort"*) ties the compressor's datapath to the
  same compare-exchange machinery.
- [NonzeroWithCount](./nonzero-with-count.md) — the sibling POOL compaction kernel.
- [select/shuffle ISA batch](../../isa/ref/b21-select-shuffle.md) — the `sel`/`dsel`/
  `shuffle` lane-compaction primitives (ISS-08) that pack the 4 winners + their indices.
- [Confidence & Walls Model](../../reference/confidence-model.md) — the tag taxonomy.
