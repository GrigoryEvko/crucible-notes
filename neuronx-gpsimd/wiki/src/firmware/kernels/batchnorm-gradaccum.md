# BatchNormalize — GradAccum (`BatchNormGradAccumulate` / `…GradAccumulate2`)

This page decodes the **back-prop reduce** half of the DVE (Data/Vector Engine,
`engine_idx=3`) **BatchNormalize** kernel family on the GPSIMD NeuronCore: the two
gradient-accumulation ops the device firmware self-names `S: BatchNormalizeGradAccum`
(opcode `0x63`) and `S: BatchNormalizeGradAccum2` (opcode `0x94`). These are the
**Σ_batch** kernels — the reduce-over-batch step that streams `Om` (forward ofmap) and
`∇O'm` (incoming `d_y`), consumes the forward-saved `μb` and `σb-0.5` as pointer
immediates, and accumulates exactly **three** back-prop sums (`d_gamma`/`d_beta` plus a
mean-coupling term) into a 3-element destination tensor. They are the producer side of
batch-norm training: the [Back-Prop](batchnorm-backprop.md) apply (`0x65`, `S3S3D3_TT`)
**consumes** the three sums; GradAccum **produces** them.

The central reverse-engineering question this page answers — *what is the difference
between GradAccum and GradAccum2?* — resolves to a hard, byte-exact answer that is **not**
a math difference: the two opcodes compute the **identical** three sums on the identical
`S3+S3→D1` shape with the identical dtype set. GradAccum2 is purely an **encoding**
evolution: it lets the `μb`/`σb-0.5` immediates come from a **register pointer** (not only
an instruction-encoded SBUF/PSUM pointer), and it pays for the new per-immediate
source-select byte by **packing** `(immediate_dtype, out_dtype)` into a single nibble pair.
That difference is witnessed in the handler bodies down to the exact `extui`/`bnei`
instructions, and is the decisive distinction proved in §8.

This is the **Cadence Tensilica Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD compute core's** own
firmware — windowed-ABI Xtensa code in the `ncore2gp` (Xtensa24, RI-2022.9, NX1.1.4,
32-byte FLIX/VLIW) configuration — plus its NX/SEQ sequencer dispatch. Every device fact
below is byte-pinned to the same `.rodata`-resident DVE carve the
[forward-statistics sibling](batchnorm-forward.md) uses, re-derived this pass from
`libnrtucode_internal.so` with the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`);
every host-ISA fact is read out of the `aws_neuron_isa_tpb_s3s3d1_bn{,2}.h` arch-isa headers
shipped in the same customop-lib package, and the **struct offsets were compile-verified**
(`gcc` `offsetof`/`sizeof`) — both structs are `ISA_STATIC_ASSERT(sizeof == 64)`. The
`extracted/` tree is gitignored — reach it with `fd --no-ignore` or absolute paths.
Confidence/evidence tags follow the project
[Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / proven-by-compile, `[MED/INFERRED]` = reasoned over OBSERVED,
`[…/CARRIED]` = re-used at a cited sibling's confidence without re-reading the artifact.

> **NOTE — what was carved this pass, and the exact objects used.** The firmware container
> is `…/custom_op/c10/lib/libnrtucode_internal.so`. The DVE images are `.rodata`-resident,
> so **file offset == device VA** for `.text`/`.rodata`; no `.data` delta applies to these
> carves. The DEBUG carve `DVE_DEBUG_IRAM` (`0x16f660` / `0x1bcc0`, sha256 `259769ff…`,
> 44,989 disasm lines) holds the handler bodies + `S:` logs; `DVE_DEBUG_DRAM` (`0x18b320` /
> `0x6d60`, sha256 `c106642d…`) holds the `S:` strings, the SEQ dispatch table, and the
> staged descriptors. Both sha256 reproduce the forward-page anchor exactly; `objdump`
> exit 0, empty stderr. The two self-name strings sit at .so file offset `0x18d5c0`
> (`S: BatchNormalizeGradAccum`) and `0x18d5dc` (`…GradAccum2`) — with carve base `0x18b320`
> that is DRAM `0x22a0` / `0x22bc`. `[HIGH/OBSERVED — all re-read this pass]`

> **CORRECTION — three task-premise corrections.** (1) "GradAccum2 is a second-moment /
> variance / fused / different-dtype / cross-microbatch accumulator." **No.** It computes
> the **identical** three sums; the only delta is immediate-source flexibility + field
> packing (§8, byte-proven). (2) The accumulate is **not** a POOL `CrossLaneReduce` /
> partition fold — it is a DVE **stream fold** over the batch (element) axis, inside the
> GradAccum handler, into the wide 1536-bit `wvec` MAC accumulator (`ivp_radd*` is
> near-absent on DVE — count `1`; §6). (3) There is **no `÷N` / bias-correction divide**
> inside GradAccum: it emits raw **sums**; the `1/N` and the `γ·σb-0.5/N` prefactor are
> [Back-Prop](batchnorm-backprop.md)/[ParamLoad](batchnorm-paramload.md)'s job. All three
> are byte-grounded below. `[HIGH/OBSERVED]`

---

## 0. TL;DR — the two ops in nine facts

1. **`BatchNormGradAccumulate` = opcode `0x63`, struct `S3S3D1_BN`; `…GradAccumulate2` =
   opcode `0x94`, struct `S3S3D1_BN2`.** Both decode `imm0=μb`, `imm1=σb-0.5`, `src0=Om`,
   `src1=∇O'm`, and write **three** fp32-class sums to a `dst` of **exactly 3 elements**.
   `[HIGH/OBSERVED — header opcode enum `0x63`/`0x94`; struct2opcode 1:1; both sizeof==64]`
2. **`0x63` is deprecated; `0x94` is the maintained variant.** The opcode enum tags `0x63`
   *"n, use BatchNormGradAccumulate2 instead"* and `0x94` *"Y"*; the v1 header itself says
   *"Need immediate from reg? Use s3s3d1_bn2/BatchNormGradAccumulate2 instead."*
   `[HIGH/OBSERVED — common.h enum comments + s3s3d1_bn.h]`
3. **`dst` is exactly 3 elements — the dst-count contract, doubly witnessed.** The header
   predicate `bnga_dst_element_cnt_check` requires `dst.num_elem[0] == 3`; both staged
   in-firmware descriptors carry `word[0] = 0x00000003` (DRAM `0x2290` for v1, `0x22e0` for
   v2). `[HIGH/OBSERVED — predicate + descriptor bytes read this pass]`
4. **The three sums = the Σ_batch for the BN backward.** `d_beta = Σ(d_y)`,
   `d_gamma = Σ(d_y·x_norm)` with `x_norm = (Om − μb)·σb-0.5` formed on the fly from
   `imm0`/`imm1`, plus a third mean-coupling sum feeding the `d_x` term. `[d_beta/d_gamma
   HIGH from the header+algebra; the exact 3rd accumulator INFERRED-HIGH — the header cites
   an internal spec not in the binary]`
5. **The accumulate is a stream-fold into the wide `wvec` MAC accumulator.** The
   `d_y·x_norm` product folds into the dedicated 1536-bit `wv0..wv3` accumulator via the
   unsigned sum-of-products MAC family (`ivp_mulusp*`/`ivp_muluupan16xr16` — the last
   OBSERVED live in a FLIX bundle at `0xb5ab`); the running `Σ d_y` folds via the
   add-normalize accumulate `ivp_baddnormnx16` (×8). `[HIGH vocab + wvec model OBSERVED; the
   per-term schedule MED across the FLIX desync]`
6. **fp32 compute hub.** `src0`/`src1` ∈ `{fp16, bf16, fp32, i32}` are **converted to fp32
   before computation** (header verbatim); the accumulator and all math are fp32; output ∈
   `{fp32, bf16, fp16}`. `[HIGH/OBSERVED — header constraint + bnga_valid_* predicates]`
7. **No rsqrt on DVE; `σb-0.5` is forward-precomputed.** `imm1` is *"the reciprocal of the
   square root of the mini-batch variance (σb-0.5), saved from forward propagation"* — a
   pointer immediate, not a computed value. `grep` finds zero `ivp_rsqrt0`/`recip0` on DVE
   (carried from the forward page). `[HIGH/OBSERVED — header verbatim; CARRIED grep]`
8. **The decisive v1↔v2 distinction is encoding-only.** v1 immediates are
   **pointer-immediates only**; v2 may pick **pointer OR register-pointer** per immediate
   via the new `imm0_imm1_src` `IMM_SRC_PAIR` `@62`, and packs `(imm_dtype, out_dtype)` into
   `immediate_out_dtype` `@61`. Witnessed: v1 body has a lone `movi.n a2,10` (FP32 default);
   v2 body has `extui …,0,4` + `extui …,4,4` + two `bnei a2,2` → reg-fetch sub-handlers.
   `[HIGH/OBSERVED — compile-verified structs + handler bytes]`
9. **DVE-only, stable cayman→sunda.** Both opcodes and both structs are present in
   cayman/mariana/mariana_plus/maverick/sunda; the `bn2` struct body is byte-identical
   cayman==sunda. POOL carries no batch-norm opcode in any gen. `[HIGH/OBSERVED — header
   presence + struct body diff; v5 interior INFERRED]`

---

## 1. The two opcodes in the BN family — where GradAccum sits

The DVE BN family is six handlers (plus `LOAD_PARAMETER_RAM`) over five 64-byte operand
structs; the forward-statistics page enumerates the whole table. The two rows this page owns:

| opcode | name | struct | status | `S:` self-name (DRAM off) | handler body |
|---|---|---|---|---|---|
| `0x63` | `BATCH_NORM_GRAD_ACCUMULATE` | `S3S3D1_BN` | `n` (deprecated) | `0x22a0` `S: BatchNormalizeGradAccum` | `0xb518` |
| `0x94` | `BATCH_NORM_GRAD_ACCUMULATE2` | `S3S3D1_BN2` | **Y** (maintained) | `0x22bc` `S: BatchNormalizeGradAccum2` | `0xb55c` |

`[HIGH/OBSERVED — opcode enum at common.h:191 (`0x63`, `"n, use …2"`) and :228 (`0x94`,
`"Y"`); both `S:` strings re-read at .so off `0x18d5c0`/`0x18d5dc` ⇒ DRAM `0x22a0`/`0x22bc`]`

**`struct2opcode` (1:1 — the wire format is bespoke per op, `jq` from
`instruction_mapping.json`):**

```
NEURON_ISA_TPB_S3S3D1_BN_STRUCT   -> [ NEURON_ISA_TPB_OPCODE_BATCH_NORM_GRAD_ACCUMULATE  ]   (0x63)
NEURON_ISA_TPB_S3S3D1_BN2_STRUCT  -> [ NEURON_ISA_TPB_OPCODE_BATCH_NORM_GRAD_ACCUMULATE2 ]   (0x94)
```

`[HIGH/OBSERVED]` Unlike the back-prop `S3S3D3_TT` struct (shared by five opcodes), each
grad struct binds to **exactly one** opcode — so GradAccum's decode trampoline and handler
body are dedicated, not a shared Tensor-Tensor reuse.

> **NOTE — the `S:` name is the *kernel* name here, not a family name.** Stats2 logs the
> *family* name `S: BatchNormalize`; GradAccum and GradAccum2 each have their **own**
> distinct self-name (`…GradAccum` / `…GradAccum2`) because they each have a dedicated
> handler body (`0xb518` / `0xb55c`) — no shared trampoline. `[HIGH/OBSERVED]`

---

## 2. The operand struct — `NEURON_ISA_TPB_S3S3D1_BN_STRUCT` (64 B, op `0x63`)

Read field-exact from `aws_neuron_isa_tpb_s3s3d1_bn.h` (CAYMAN arch-isa, NC-v3) and
**compile-verified** (`gcc` `offsetof`/`sizeof`). Header title: *"Neuron 'S3S3D1_BN'
Format — two 3d SRC Tensor, one 1d DST Tensor. Use for: BatchNormGradAccumulate."*
`[HIGH/OBSERVED]`

| off | size | field | type | role |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `HEADER` | `{opcode=0x63, inst_word_len, debug_cmd, debug_hint}` |
| 4–11 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12–15 | 4 | `imm0` | `IMM_VAL_INST_FIELD` | **`μb`** (mini-batch mean), pointer-immediate |
| 16–31 | 16 | `src0_mem_pattern` | `TENSOR3D` | **`Om`** (forward ofmap), read tensor |
| 32–47 | 16 | `src1_mem_pattern` | `TENSOR3D` | **`∇O'm`** (incoming gradient `d_y`), read tensor |
| 48–55 | 8 | `dst_mem_pattern` | `TENSOR1D` | the **3** grad sums (`num_elem[0] == 3`) |
| 56–59 | 4 | `imm1` | `IMM_VAL_INST_FIELD` | **`σb-0.5`** (inv-std), pointer-immediate |
| 60 | 1 | `in0_in1_dtype` | `DTYPE_PAIR` | src0 dtype = lo nibble, src1 dtype = hi nibble |
| 61 | 1 | `immediate_dtype` | `DTYPE` | dtype of **both** imms (`0x0/Invalid ⇒ fp32`) |
| 62 | 1 | `out_dtype` | `DTYPE` | `{fp32, fp16, bf16}` (`AllowFP32R::True`) |
| 63 | 1 | `num_active_channels` | `u8` | `1 .. POOLING_NUM_CHANNELS` (= 128) |

```c
typedef struct NEURON_ISA_TPB_S3S3D1_BN_STRUCT {
    NEURON_ISA_TPB_HEADER             header;               //  0 -  3
    NEURON_ISA_TPB_EVENTS             events;               //  4 - 11
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm0;                 // 12 - 15   μb   (mini-batch mean)
    NEURON_ISA_TPB_TENSOR3D           src0_mem_pattern;     // 16 - 31   Om   (forward ofmap)
    NEURON_ISA_TPB_TENSOR3D           src1_mem_pattern;     // 32 - 47   ∇O'm (incoming d_y)
    NEURON_ISA_TPB_TENSOR1D           dst_mem_pattern;      // 48 - 55   3 grad sums
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm1;                 // 56 - 59   σb-0.5 (inv-std)
    NEURON_ISA_TPB_DTYPE_PAIR         in0_in1_dtype;        // 60        src0 lo / src1 hi
    NEURON_ISA_TPB_DTYPE              immediate_dtype;      // 61        dtype of BOTH imms
    NEURON_ISA_TPB_DTYPE              out_dtype;            // 62        out dtype
    uint8_t                           num_active_channels;  // 63        1..128
} NEURON_ISA_TPB_S3S3D1_BN_STRUCT;                          // ISA_STATIC_ASSERT == 64
```

**Component types (compile-verified):** `TENSOR3D` (16 B) =
`{ADDR4 start_addr; int16 step_elem[3]; uint16 num_elem[3]}`; `TENSOR1D` (8 B) =
`{ADDR4 start_addr; int16 step_elem[1]; uint16 num_elem[1]}`. `IMM_VAL_INST_FIELD` is a
4-byte union `{PARTITION_OFFSET imm_ptr; IMM_REG imm_reg; float imm_arith_fp32; …}` — for
GradAccum the `imm_ptr` arm is the only legal one (the v1 validity gate tests `imm_ptr`).
All three tensors are `AllowedInPSUM=True, AllowedInSBUF=True`; both srcs are
`WriteTensor=False`, the dst is `WriteTensor=True`. `[HIGH/OBSERVED — common.h TENSOR3D@649,
TENSOR1D@637, IMM_VAL_INST_FIELD@866; tensor3d_valid/tensor1d_valid predicate args]`

> **GOTCHA — `imm0`/`imm1` are *pointers*, not values.** The 4-byte `IMM_VAL_INST_FIELD`
> holds a **`PARTITION_OFFSET` pointer into SBUF/PSUM**, not the fp32 mean/inv-std value
> itself. The header is explicit: *"These values come from immediate pointers only."* The
> kernel dereferences `imm0`→`μb` and `imm1`→`σb-0.5` per channel from memory. A
> reimplementer must **not** stuff the fp32 mean/inv-std into the instruction word — that
> is what the `InstructionImmediate` source (absent here in v1) would mean.
> `[HIGH/OBSERVED — bnga_imm_check tests `imm.imm_ptr` + `addr_aligned_dtype` + the header
> "immediate pointers only" clause]`

### 2.1 The validity predicates (the wire contract, header-verbatim)

`is_valid_s3s3d1_bn` (the master gate) is worth reading in full because it pins every
constraint a reimplementer must enforce:

```c
fn is_valid_s3s3d1_bn(i) ->
       has_valid_neuron_header(i) && has_valid_neuron_events(i)
    && has_s3s3d1_bn_opcode(i)                                       /* opcode == 0x63        */
    && is_valid_dtype(out_dtype, AllowFP32R::True)                   /* out  ∈ {fp32,fp16,bf16}*/
    && is_valid_dtype(in0_in1_dtype.lo, AllowFP32R::False)           /* src0 dtype            */
    && is_valid_dtype(in0_in1_dtype.hi, AllowFP32R::False)           /* src1 dtype            */
    && start_addr_active_channels(src0|src1|dst.start_addr, num_active_channels)
    && s3s3d1_bn_channels(i)                  /* 1 <= num_active_channels <= 128 (POOLING)    */
    && tensor3d_valid(src0, …, WriteTensor::False, PSUM::True, SBUF::True)   /* Om read       */
    && tensor3d_valid(src1, …, WriteTensor::False, PSUM::True, SBUF::True)   /* ∇O'm read     */
    && tensor1d_valid(dst,  out_dtype, WriteTensor::True, PSUM::True, SBUF::True) /* dst write */
    && bnga_valid_input_type(i)   && bnga_valid_output_type(i) && bnga_valid_immediate_type(i)
    && bnga_src_element_cnt_check(i)          /* prod(src0.num_elem) == prod(src1.num_elem)   */
    && bnga_dst_element_cnt_check(i)          /* dst.num_elem[0] == 3                          */
    && bnga_imm_check(imm0, immediate_dtype, num_active_channels)     /* imm_ptr aligned       */
    && bnga_imm_check(imm1, immediate_dtype, num_active_channels)
    && tt_valid_partitions(src0.start_addr, src1.start_addr);
```

Three constraints carry weight:

* **`bnga_src_element_cnt_check`** — `prod(src0.num_elem[0..2]) == prod(src1.num_elem[0..2])`
  (or the shape comes from a register). `Om` and `∇O'm` must have the **same element count**
  — this product **is** the batch extent over which the three sums reduce. The sum extent is
  tied to the **src** stream, not to the dst. `[HIGH/OBSERVED]`
* **`bnga_dst_element_cnt_check`** — `dst.num_elem[0] == 3`. Exactly three outputs.
  `[HIGH/OBSERVED]`
* **`bnga_imm_check`** — `addr_aligned_dtype(imm.imm_ptr, imm_dtype)` **OR**
  `(imm_dtype == Invalid && addr_aligned_dtype(imm.imm_ptr, FP32))`, **and**
  `tpb_addr_active_channels(imm.imm_ptr, num_channels)`. This is exactly the
  `Invalid ⇒ fp32 default` rule made explicit. `[HIGH/OBSERVED]`

> **QUIRK — the `immediate_dtype` field is a 2022 retrofit over a `reserved0` byte.** The
> v1 header documents the change verbatim (*"Change 7/19/22: the 'immediate_dtype' field is
> a new field being added; replaces 'reserved0' which should always have been 0x0; will
> represent the data type of *both* immediates; to avoid breaking existing code 0x0
> (Dtype::Invalid) is allowed and means fp32"*). That is **why** the handler body's default
> path is a literal `movi.n a2,10` (`Dtype::FP32 = 0xA`) — see §3.5. A reimplementer
> encoding v1 may leave `@61 = 0` and get fp32 immediates for free. `[HIGH/OBSERVED — header
> change-note + the byte at `0xb54d`]`

### 2.2 What the op computes — header semantics, verbatim sense

> *"This instruction gets two immediates from immediate pointers in `imm0` and `imm1`:
> `imm0`: the mini-batch mean (`μb`), saved from forward propagation; `imm1`: the reciprocal
> of the square root of the mini-batch variance (`σb-0.5`), saved from forward propagation.
> … The instruction then streams two 3d tensors: `src0`: the ofmap values (`Om`), saved
> from forward propagation; `src1`: the ofmap gradients (`∇O'm`) being back-propagated.
> With these values, the DVE accumulates **three separate results** required for batch
> normalization backpropagation … the dst tensor must have exactly 3 elements."* —
> `s3s3d1_bn.h`

The header points the *exact* per-accumulator definitions at an internal spec
(*"BNGradientAccumulate Definition"*) **not present in the binary** — so the identity of
each of the three is pinned to the standard BN-backward algebra, which is HIGH for two of
the three and INFERRED-HIGH for the coupling term (§6.1).

---

## 3. The two dispatch chains — byte-exact (DVE DEBUG build)

Both opcodes route through the SEQ direct-indexed DRAM jump table:
`key = opcode_byte − 0x41`, table1 base at DRAM file offset `0x814` (device VA `0x80814`),
word = LE32 at `0x814 + 4·key`. Read **byte-exact this pass** from `DVE_DEBUG_DRAM.bin`:

| opcode | idx | table off | word | trampoline |
|---|---|---|---|---|
| `0x61` Stats2 | `0x20` | `0x894` | `0x308e` | `0x308e` (shared S4D2_BN) |
| `0x62` Aggregate | `0x21` | `0x898` | `0x308e` | `0x308e` (shared) |
| **`0x63` GradAccum** | **`0x22`** | **`0x89c`** | **`0x309e`** | **`0x309e`** |
| `0x64` ParamLoad | `0x23` | `0x8a0` | `0x30b6` | `0x30b6` |
| `0x65` BackProp | `0x24` | `0x8a4` | `0x30ae` | `0x30ae` |
| `0x66` LoadParamRAM | `0x25` | `0x8a8` | `0x30e6` | `0x30e6` |
| `0x8e` ParamLoad2 | `0x4d` | `0x948` | `0x30be` | `0x30be` |
| **`0x94` GradAccum2** | **`0x53`** | **`0x960`** | **`0x30a6`** | **`0x30a6`** |

`[HIGH/OBSERVED — dispatch words read via `struct.unpack_from('<I', dram, 0x814+4*key)`,
reproduced this pass]`

The trampolines, register-handler thunks, and handler bodies were re-disassembled with the
native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) directly from the carved IRAM:

```
;; ---- op 0x63 GradAccum ----
309e:  e5f0fe   call8   0x1fac            ; -> the register-handler thunk
30a1:  465b00   j       0x3212            ; -> the common Handler invoke

1fac:  366100   entry   a1, 48
1faf:  240000   const16 a2, 0
1fb2:  2418b5   const16 a2, 0xb518        ; *** stage the GradAccum body VA into the Handler frame
1fb5:  226103   s32i    a2, a1, 12        ;     frame off 12
1fb8:  cf …              [FLIX bundle prefix; l32r a1 / l32r a10]
1fbf:  e55507   call8   0x951c            ; the Handler dispatch
1fc2:  900000   retw

;; ---- op 0x94 GradAccum2 ----
30a6:  25f2fe   call8   0x1fc8
30a9:  465900   j       0x3212

1fc8:  366100   entry   a1, 48
1fce:  245cb5   const16 a2, 0xb55c        ; *** stage the GradAccum2 body VA
1fd1:  226103   s32i    a2, a1, 12
1fdb:  255407   call8   0x951c
1fde:  900000   retw
```

`[HIGH/OBSERVED — every byte above read by the native objdump this pass]` Both chains
converge on the common Handler invoke at IRAM `0x3212` (`j 0x2e87`, the C++
`Handler::execute()` path). The thunk's `const16 a2,<body VA>` + `s32i a2,a1,12` is the
"stage the handler body pointer into the Handler object frame at offset 12, then call the
generic dispatcher" pattern the whole DVE ISA uses.

### 3.1 The staged 3-element descriptors

Each handler body also `const16`-stages a per-opcode instruction **descriptor** from DRAM:

```
GradAccum  @0xb533:  const16 a2, 0x2290    DRAM 0x2290 = 0x00000003 0x78000000 0xffcfbfa6 0x0f421061
GradAccum2 @0xb577:  const16 a2, 0x22e0    DRAM 0x22e0 = 0x00000003 0x78000000 0xffc00026 0x0f421061
```

`word[0] = 0x00000003` for **both** — the "dst must have exactly 3 elements" contract,
encoded redundantly in the firmware descriptor as well as the `bnga_dst_element_cnt_check`
predicate. The descriptors differ only in `word[2]` (`0xffcfbfa6` vs `0xffc00026`) — the
per-opcode decode-spec / inst-word-len flags; `word[3] = 0x0f421061` is shared.
`[HIGH/OBSERVED — both 4-word descriptors read this pass]`

### 3.2 Handler bodies + entry frame sizes

| handler | body VA | entry frame | self-name log |
|---|---|---|---|
| `BatchNormStats2` | `0xb380` | `entry a1, 96` | `S: BatchNormalize` (`0x2261`) |
| **`GradAccum`** | **`0xb518`** | **`entry a1, 48`** | **`S: BatchNormalizeGradAccum`** (`0x22a0`) |
| **`GradAccum2`** | **`0xb55c`** | **`entry a1, 64`** | **`S: BatchNormalizeGradAccum2`** (`0x22bc`) |
| `ParamLoad` | `0xb62c` | `entry a1, 48` | `…ParamLoad` (`0x22f0`) |
| `ParamLoad2` | `0xb694` | `entry a1, 48` | `…ParamLoad2` (`0x230c`) |
| `BackProp` | `0xb6f0` | `entry a1, 32` | `…BackProp` (`0x2350`) |

`[HIGH/OBSERVED — `entry` widths re-read this pass]`

> **NOTE — GradAccum2's frame is 16 bytes deeper than GradAccum's (`a1,64` vs `a1,48`).**
> Every other v1 BN handler (Stats2 excepted) is `entry a1,48`; GradAccum2 is the **only**
> grad handler with a 64-byte frame. Those extra 16 bytes hold the unpacked
> `(imm_dtype, out_dtype, imm0_src, imm1_src)` nibble spills + the two register-pointer-fetch
> sub-handler call frames (§8). The frame delta is itself a fingerprint of the v2
> per-immediate src-dispatch machinery. `[HIGH/OBSERVED]`

### 3.3 The v1 handler body — `GradAccum @0xb518` (68 B window)

```
b518:  366100   entry   a1, 48
b51b:  j 0xb521
b521:  a40800   const16 a10, 8           ; SEQ log shim preamble
b524:  a4a022   const16 a10, 0x22a0      ; *** "S: BatchNormalizeGradAccum"
b527:  a5ae0c   call8   0x18010          ;     the 'S:'-log helper
b530:  240800   const16 a2, 8
b533:  249022   const16 a2, 0x2290       ; *** stage the v1 descriptor (word[0]=3 dst-count)
b536:  …                                 ; FLIX-VLIW region begins (the streaming MAC fold)
b54d:  0ca2     movi.n  a2, 10           ; *** Dtype::FP32 (0xA) — the SINGLE immediate_dtype
                                         ;     DEFAULT. No nibble unpack, no per-imm src test.
b559:  1df0     retw.n
```

`[HIGH/OBSERVED — the `entry`/log/`const16`-descriptor/`movi.n a2,10`/`retw.n` are clean
scalar instructions, read directly; the FLIX-VLIW compute region past `0xb536` desyncs under
stock linear sweep and is reported structurally (§6), not byte-quoted]`

### 3.4 The v2 handler body — `GradAccum2 @0xb55c` (208 B window)

```
b55c:  368100   entry   a1, 64                      ; deeper frame than v1
b565:  a40800   const16 a10, 8
b568:  a4bc22   const16 a10, 0x22bc                 ; *** "S: BatchNormalizeGradAccum2"
b56b:  65aa0c   call8   0x18010
b574:  240800   const16 a2, 8
b577:  24e022   const16 a2, 0x22e0                  ; *** stage the v2 descriptor (word[0]=3)
b57a:  …                                            ; FLIX region (the same MAC fold as v1)
b58c:  202034   extui   a2, a2, 0, 4                ; *** unpack LOW nibble  (DTYPE_PAIR.lo / IMM_SRC_PAIR.lo)
b59c:  202434   extui   a2, a2, 4, 4                ; *** unpack HIGH nibble (DTYPE_PAIR.hi / IMM_SRC_PAIR.hi)
b5ab:  { … ivp_lvn_2x16u_i v11,a8,0xca0 ; ivp_muluupan16xr16 wv0,v2,v20,pr0 ;
          ivp_shfl2nx8i v9,v29,16 ; ivp_babssubunx16 vb4,v16,v18,v0 }   ; LIVE FLIX MAC bundle
b5c3:  66220a   bnei    a2, 2, 0xb5d1               ; *** if imm0_src == RegPtrImmediate(=2):
b5c9:  a50100   call8   0xb5e4                      ;     fetch imm0 pointer from a register
b5d3:  66220a   bnei    a2, 2, 0xb5e1               ; *** if imm1_src == RegPtrImmediate(=2):
b5d9:  e50200   call8   0xb608                      ;     fetch imm1 pointer from a register
b5e4:  366100   entry   a1, 48 …                    ; imm0 reg-pointer-fetch sub-handler
b608:  366100   entry   a1, 48 …                    ; imm1 reg-pointer-fetch sub-handler
```

`[HIGH/OBSERVED — `entry`/log/descriptor/`extui ,0,4`/`extui ,4,4`/two `bnei a2,2`/the two
`call8` sub-handler targets + both sub-handler `entry a1,48` prologues read directly; the
`b5ab` FLIX MAC bundle decodes live but sits at the FLIX/literal boundary → MED]`

The two reg-pointer-fetch sub-handlers `0xb5e4` (imm0) and `0xb608` (imm1) are
near-identical `entry a1,48` stubs that differ only in which register slot they read the
pointer from (`movi.n a4,29` at `0xb5e8` vs the analogous constant in the imm1 stub) — i.e.
which of the two `imm_reg` slots feeds `μb` vs `σb-0.5`. `[HIGH/OBSERVED bytes; the precise
reg-encoding LOW — it lives in the FLIX-desynced body]`

### 3.5 PERF (production) build

PERF strips the `S:` logs. PERF DRAM dispatch (table @ `0x814`):
`op 0x61 Stats2 → 0x835a` (live); `op 0x63 GradAccum → 0x9090` — the **shared
deprecated-cluster** handler that the three deprecated variants `0x63/0x64/0x65` route to,
consistent with GradAccum being marked *"n, use v2"*. `op 0x94 GradAccum2 → 0x000c`, which
is **not** a trustworthy VA: idx `0x53` past ~`0x40` overlaps the assertion string pool in
the tiny `0x2fc0` PERF DRAM. **The DEBUG dispatch is the reliable substrate for GradAccum2.**
`[HIGH for `0x61`/`0x63`; the `0x94` PERF row MED/unusable — flagged honestly]`

---

## 4. The accumulation math — the three sums

### 4.1 What the three accumulators are

The standard batch-norm backward (per channel, `x_norm = (Om − μb)·σb-0.5`,
`N` = batch extent) requires:

```
d_beta  = Σ_batch( d_y )                      ; gradient w.r.t. the shift β   (src1 alone)
d_gamma = Σ_batch( d_y · x_norm )             ; gradient w.r.t. the scale γ   (src1 × normalize(src0))
d_x_i   = (γ·σb-0.5 / N) · ( N·d_y_i − d_beta − x_norm_i · d_gamma )   ; the per-element input grad
```

The `d_x` formula needs three batch reductions: `Σ d_y` (= `d_beta`),
`Σ(d_y·x_norm)` (= `d_gamma`), and a third coupling sum. GradAccum produces exactly **three**
batch sums (`dst.num_elem[0] == 3`); the two named gradients are `d_beta` and `d_gamma`, and
the third is the mean-coupling term the [Back-Prop](batchnorm-backprop.md) apply combines
into `d_x`. `[d_beta/d_gamma HIGH from the header+algebra; the exact 3rd accumulator
INFERRED-HIGH — the header's per-sum definition points at an internal spec absent from the
binary]`

```c
/* BatchNormGradAccumulate — the Σ_batch reduce (per channel, num_active_channels lanes).
   imm0 = μb (mean), imm1 = σb-0.5 (inv-std), both DEREFERENCED from SBUF/PSUM pointers;
   src0 = Om stream, src1 = ∇O'm (d_y) stream; same element count = the batch extent N.
   All inputs CONVERTED TO fp32 first; the three accumulators are fp32-internal.            */

float mu       = *(float*)imm0.imm_ptr;        /* μb,     forward-saved, per channel         */
float inv_std  = *(float*)imm1.imm_ptr;        /* σb-0.5, forward-saved (NOT recomputed)     */

float sum_dy       = 0.0f;   /* -> d_beta                                                    */
float sum_dy_xnorm = 0.0f;   /* -> d_gamma                                                   */
float sum_coupling = 0.0f;   /* -> the 3rd mean-coupling sum (exact form INFERRED-HIGH)      */

for (i = 0; i < N; ++i) {                       /* stream fold over the batch axis           */
    float om   = to_fp32(load(src0, i));        /* Om_i,  in_dtype -> fp32                    */
    float dy   = to_fp32(load(src1, i));        /* ∇O'm_i = d_y_i                             */
    float xn   = (om - mu) * inv_std;           /* x_norm_i, formed on the fly from imm0/imm1 */
    sum_dy       += dy;                          /* ivp_baddnormnx16 add-normalize accumulate */
    sum_dy_xnorm += dy * xn;                     /* ivp_mulus* / ivp_muluupan16xr16 -> wvec   */
    sum_coupling += /* the coupling term       */ ;
}
/* write exactly 3 fp32-class outputs to dst (TENSOR1D, num_elem[0] == 3): */
store3(dst, sum_dy /*d_beta*/, sum_dy_xnorm /*d_gamma*/, sum_coupling);
```

`[the d_beta/d_gamma fold HIGH from header+algebra+vocab; the per-term schedule MED across
the FLIX desync; the coupling term INFERRED-HIGH]`

> **NOTE — no `÷N` and no `γ·σb-0.5/N` prefactor inside GradAccum.** GradAccum emits raw
> **sums**, not means. The `1/N` normalization and the `γ·σb-0.5/N` prefactor in the `d_x`
> formula are applied **later** by the [Back-Prop](batchnorm-backprop.md) apply (`0x65`),
> which uses the `N` staged by [ParamLoad](batchnorm-paramload.md) plus the 256-entry fp32
> reciprocal Parameter-RAM the forward page documents. The `+ε` is folded into `σb-0.5`
> host-side **before** it arrives as `imm1`. So there is **no per-element divide inside
> GradAccum**. `[HIGH — the no-divide-here from the header (sums, not means) + the divide
> machinery living on the apply/forward side]`

### 4.2 The fold — stream-accumulate into the wide `wvec`, not a partition reduce

The `Σ_batch` runs **inside** the GradAccum handler as a streaming MAC over the `src`
TENSOR3D element stream (the batch extent = `src0`/`src1` `num_elem` product), folding:

* **`d_gamma = Σ(d_y·x_norm)`** — the unsigned sum-of-products MAC into the **dedicated
  1536-bit wide-vector accumulator** `wv0..wv3`. The MAC vocabulary harvested from
  `DVE_DEBUG_IRAM.dis` (rg counts, this pass): `ivp_mulusp2n8xr16` (5),
  `ivp_muluspn16xr16` (2), `ivp_muluspa2n8xr16` (4, `A`=accumulate),
  `ivp_muluupan16xr16` (3, OBSERVED live in the `0xb5ab` bundle), `ivp_muluupn16xr16` (1).
  Wide-accumulator references `wv0`/`wv1`/`wv2`/`wv3` = 43/32/23/17 = **115** total — the
  1536-bit MAC regfile. `[HIGH/OBSERVED — counts re-grepped this pass]`
* **`d_beta = Σ(d_y)`** — the running add-normalize accumulate `ivp_baddnormnx16`
  (DEBUG **8**), the same fold geometry the forward page found for Stats2's sum.
  `[HIGH/OBSERVED]`

The partition-reduce family (`ivp_radd*`) is **near-absent** on the whole DVE image —
`ivp_radd` count = **1** (a single `ivp_raddu2nx8t`). That count is the proof: GradAccum is
a **stream fold over the batch (element) axis**, not a cross-**partition** fold. The POOL
engine's `CrossLaneReduce` (`0x7c`/`0x7d`) and `tensor_partition_reduce` fold the SBUF/PSUM
**partition** axis on a **different engine**; the BN batch reduce folds the **streamed
element** axis on the **DVE** inside GradAccum. Distinct mechanisms, distinct engines,
distinct axes. `[HIGH the IVP vocab + `radd`-near-absent count OBSERVED; the
GradAccum-internal-reduce attribution INFERRED-HIGH — the fold schedule is FLIX-desynced]`

```c
/* The DVE wide-MAC fold for d_gamma = Σ(d_y · x_norm) (ISS-grounded vocabulary).
   ivp_muluupan16xr16 wv0, v_dy, v_xnorm, pr0  ==  wv0 += dy * x_norm  (unsigned pair-accumulate)
   The 1536-bit wv0..wv3 regfile holds the running sum-of-products at full integer width;
   it is reduced/cast to the fp32 sum at the end (ivp_*_2xf32t fp32 finalize).               */
wvec wv = 0;                                  /* 4-entry, 1536-bit dedicated MAC accumulator  */
for (each 16-element FLIX chunk of the d_y / x_norm streams)
    wv = ivp_muluupan16xr16(wv, v_dy, v_xnorm, pr0);   /* out += products, pair-accumulate    */
float d_gamma = wvec_to_fp32(wv);             /* finalize cast (FLIX-desynced in-body)        */
```

`[HIGH the MAC op + wvec model OBSERVED; the exact per-chunk schedule + the wvec→fp32
finalize cast MED across the FLIX desync]`

### 4.3 Accumulator precision and persistence

* **Precision.** The accumulator is **fp32-internal** (header: *"both [srcs] will be
  converted to fp32 before computation"*); the integer MAC products land in the wide
  1536-bit `wvec`, then are reduced/cast to the fp32 sum (`ivp_*_2xf32t` fp32 finalize). No
  fp64, no hardware FP — soft-float bodies, consistent with the forward page. `[HIGH the
  fp32-internal via header + vocab; the wvec→fp32 finalize INFERRED-HIGH across the desync]`
* **Persistence.** The `dst` is a 3-element TENSOR1D in SBUF/PSUM. GradAccum is a
  **single-instruction reduce** over the streamed `src`: the sum extent is tied to the src
  element count (`bnga_src_element_cnt_check`), **not** to a prior `dst` value. The dst is
  `WriteTensor=True` and there is **no read-modify-write of dst** in the validity contract —
  i.e. GradAccum does **not** add into a prior dst accumulator across calls. Cross-microbatch
  gradient accumulation, if needed, is a **host/compiler-orchestrated** add of successive
  `dst` tensors — not a feature of this opcode. `[HIGH the WriteTensor + src-count-bound
  contract; the "no in-place accumulate-to-prior" INFERRED-HIGH from the contract]`

> **GOTCHA — the three sums are *plain sums*, not running state.** A reimplementer must
> **not** model GradAccum as an in-place `dst += …` accumulator. Each instruction reduces
> one `src` stream to three fresh sums and overwrites the 3-element dst. Multi-microbatch
> training accumulates by having the host add the per-instruction dst tensors. `[HIGH —
> from the WriteTensor + src-count contract]`

### 4.4 `μb`/`σb-0.5` are reused from forward — not recomputed

GradAccum does **not** recompute the mean or the inv-std. `imm0 = μb` and `imm1 = σb-0.5`
arrive *"saved from forward propagation"* (header verbatim). There is **no `ivp_rsqrt0` on
DVE** (the forward page's grep is `0` in both builds; the only seed→Newton the DVE BN path
runs is reciprocal **division** for means). `σb-0.5` is forward/host-precomputed and handed
to GradAccum as a pointer immediate. `[HIGH/OBSERVED — header + CARRIED grep from the
forward page]`

---

## 5. The dtype matrix

DTYPE ordinals (`common.h`): `INVALID 0x0`, `INT8 0x2`, `UINT8 0x3`, `INT16 0x4`,
`UINT16 0x5`, `BFLOAT16 0x6`, `FP16 0x7`, `INT32 0x8`, `UINT32 0x9`, `FP32 0xA`,
`FP32R 0xB`, `INT64 0xC`. `[HIGH/OBSERVED — common.h:727–737]`

| field | `0x63` GradAccum (`S3S3D1_BN`) | `0x94` GradAccum2 (`S3S3D1_BN2`) |
|---|---|---|
| `src0` (`in0_in1_dtype.lo @60`) | `{FP16, BF16, FP32, INT32}` → fp32 (`AllowFP32R::False`) | same |
| `src1` (`in0_in1_dtype.hi @60`) | `{FP16, BF16, FP32, INT32}` → fp32 (`AllowFP32R::False`) | same |
| imm dtype | `immediate_dtype @61`: `{Invalid⇒FP32, FP16, BF16, FP32, INT32}` | `immediate_out_dtype.lo @61` (`AllowFP32R::False`): `{FP16, BF16, FP32, INT32}` |
| out dtype | `out_dtype @62`: `{FP32, FP16, BF16}` (`AllowFP32R::True`) | `immediate_out_dtype.hi @61` (`AllowFP32R::True`): `{FP32, FP16, BF16}` |
| imm source | implicit `PointerImmediate` only | `imm0_imm1_src @62`: per-imm `{PointerImmediate, RegPtrImmediate}` |
| compute | fp32 throughout; `dst` = 3 elements | fp32 throughout; `dst` = 3 elements |

`[HIGH/OBSERVED — read from the `bnga_valid_*` / `bnga2_valid_*` predicate comments,
compile-verified]`

> **CORRECTION — the v1 `immediate_dtype` `Invalid⇒fp32` default does NOT carry to v2.** In
> v1, `@61` is a **scalar** `Dtype` and `0x0` (`Invalid`) is legal, meaning fp32. In v2,
> `@61` is repurposed into the **packed** `immediate_out_dtype` `DTYPE_PAIR` — its low
> nibble is `bnga2_valid_input_type`-gated to `{FP16, BF16, FP32, INT32}` (`AllowFP32R::
> False`), with **no `Invalid⇒fp32` escape**. So a v2 encoder must set a real imm dtype in
> the low nibble; it cannot leave `@61 = 0` and expect fp32. The fp32-default convenience is
> a v1-only legacy affordance. `[HIGH/OBSERVED — v1 `bnga_valid_immediate_type` lists
> `Invalid`; v2 `bnga2_valid_input_type` does not]`

---

## 6. The GradAccum vs GradAccum2 distinction — decisive

This is the task's central question. The two ops are the **same math** (the three batch
sums; `dst==3`; `imm0=μb`, `imm1=σb-0.5`, `src0=Om`, `src1=∇O'm`). Each candidate hypothesis
is eliminated by direct evidence:

* **NOT a second-moment / variance accumulator.** Both headers say *identically* *"the DVE
  accumulates three separate results required for batch normalization backpropagation"*;
  `bn2` adds no variance term, no fourth sum. `[HIGH/OBSERVED — both headers verbatim]`
* **NOT a fused / two-input variant.** Both already take two srcs (`Om`, `∇O'm`); `bn2` adds
  no third src. The `S3+S3→D1` shape is **identical**. `[HIGH/OBSERVED — both struct
  layouts]`
* **NOT a different dtype path.** Both allow `src ∈ {fp16, bf16, fp32, i32}`,
  `out ∈ {fp32, fp16, bf16}`; `bn2` merely **packs** `(imm_dtype, out_dtype)` into one byte
  — the allowed **set** is the same. `[HIGH/OBSERVED — compile-verified predicates]`
* **NOT a cross-microbatch add-to-prior accumulator.** Neither op read-modify-writes a prior
  `dst`; the sum extent ties to the **src** element count, not a prior dst value (§4.3).
  `[HIGH/OBSERVED contract; the negative INFERRED-HIGH]`

**The actual difference — immediate-source flexibility + field packing.** The `S3S3D1_BN2`
struct (§2 layout, `@60` unchanged) differs from `S3S3D1_BN` in exactly two bytes:

| off | `S3S3D1_BN` (v1) | `S3S3D1_BN2` (v2) |
|---|---|---|
| `60` | `in0_in1_dtype` (`DTYPE_PAIR`: src0 lo / src1 hi) | **unchanged** |
| `61` | `immediate_dtype` (**scalar** `DTYPE`, `Invalid⇒fp32`) | `immediate_out_dtype` (**packed** `DTYPE_PAIR`: imm dtype lo / out dtype hi) |
| `62` | `out_dtype` (scalar `DTYPE`) | `imm0_imm1_src` (**new** `IMM_SRC_PAIR`: imm0 src lo / imm1 src hi) |
| `63` | `num_active_channels` | **unchanged** |

The v2 frees a byte by packing `(imm_dtype, out_dtype)` into `@61`, and spends it on the new
`imm0_imm1_src` `@62` — *"allows pointers coming from registers."* The `IMM_SRC` enum the
pair holds is the canonical `{INSTRUCTION_IMMEDIATE=0, POINTER_IMMEDIATE=1,
REG_PTR_IMMEDIATE=2}` (`common.h:1207`). v1's `bnga_imm_check` tests **only** `imm_ptr`
(pointer-immediate); v2's `bnga2_imm_check` branches per-immediate:

```c
fn bnga2_imm_check(imm, dtype, imm_src, num_active_channels) ->
       (  imm_src == ImmSrc::PointerImmediate                      /* = 1: SBUF/PSUM pointer */
       && addr_aligned_dtype(imm.imm_ptr, dtype)
       && tpb_addr_active_channels(imm.imm_ptr, num_active_channels))
    || (  imm_src == ImmSrc::RegPtrImmediate                       /* = 2: pointer FROM a reg */
       && is_valid_imm_reg(imm.imm_reg));
```

**The firmware witnesses this exactly** (clean scalar disasm, §3.3/§3.4):

```c
/* v1 GradAccum @0xb518 — a single immediate_dtype, no unpack, no per-imm src test: */
movi.n  a2, 10;                  /* Dtype::FP32 (0xA) — the Invalid=>fp32 default          */

/* v2 GradAccum2 @0xb55c — unpack the 4+4 nibble pairs, then per-immediate src dispatch:   */
extui   a2, a2, 0, 4;            /* low  nibble: imm_dtype / imm0_src                       */
extui   a2, a2, 4, 4;            /* high nibble: out_dtype / imm1_src                       */
if (imm0_src == 2 /*RegPtrImmediate*/) call(sub_0xb5e4);   /* fetch imm0 pointer from a reg */
if (imm1_src == 2 /*RegPtrImmediate*/) call(sub_0xb608);   /* fetch imm1 pointer from a reg */
```

The body/frame delta is the whole story: v2 is **208 B / `entry a1,64`**; v1 is
**68 B / `entry a1,48`** — and the entire 140-byte / 16-byte-frame difference is the
nibble-unpack + the two register-pointer-fetch sub-handlers (`0xb5e4`, `0xb608`). v1 has
neither. `[HIGH/OBSERVED — every instruction above read this pass via the native objdump]`

**Why v2 exists.** GradAccum (`0x63`) is deprecated (*"n, use BatchNormGradAccumulate2"*);
GradAccum2 (`0x94`) is the maintained replacement. The register-pointer immediates let the
compiler feed `μb`/`σb-0.5` from a **register** (e.g. a loop-carried pointer) instead of a
fixed instruction-encoded SBUF/PSUM pointer — the same v1→v2 evolution
[ParamLoad → ParamLoad2](batchnorm-paramload.md) makes. `[HIGH/OBSERVED — enum status
markers + the v1 header's own "use s3s3d1_bn2 for reg immediates" note]`

---

## 7. Per-generation presence

| GEN | opcodes `0x63`/`0x94` | `s3s3d1_bn{,2}.h` structs | DVE GradAccum bodies | notes |
|---|---|---|---|---|
| **SUNDA** (v2) | present | present | not separately diffed | `bn2` struct body **byte-identical** to cayman |
| **CAYMAN** (v3) | present — **the carve substrate** | present, **compile-verified** | **OBSERVED** (`0xb518`/`0xb55c` decoded byte-exact) | the carve |
| **MARIANA** (v4) | present | present | structurally identical SEQ engine | header-stated |
| **MARIANA_PLUS** (v4+) | present | present | structurally identical SEQ engine | header-stated |
| **MAVERICK** (v5) | present | present | **header-OBSERVED → interior INFERRED** | header-stated |

Both opcodes (`0x63`, `0x94`) and both structs (`S3S3D1_BN`, `S3S3D1_BN2`) are present in
the `cayman / mariana / mariana_plus / maverick / sunda` arch-isa header trees (each
`aws_neuron_isa_tpb_s3s3d1_bn{,2}.h` exists per gen, fd-verified). The `bn2` struct **body**
is byte-identical cayman==sunda (the struct-field lines diff is empty; per-file sha differs
only in copyright/comment text). The DVE DEBUG dispatch (both chains
`0x63→0x309e→0x1fac→0xb518` and `0x94→0x30a6→0x1fc8→0xb55c`) + both self-names are in the
`cayman/seq` NX DVE firmware. The POOL engine carries **no** batch-norm opcode in **any**
carved gen (the POOL EXTISA image strings are empty for `batch`/`grad`). GradAccum is a
**DVE-only** kernel. `[HIGH/OBSERVED — per-gen header presence + the cayman DVE carve
decoded this pass + the cayman==sunda `bn2`-body diff; MED/INFERRED for non-cayman
handler-body byte-equality, not exhaustively diffed]`

> **MAVERICK (v5) interior — header-OBSERVED only → INFERRED.** Per the
> [generation-grounding policy](../../reference/confidence-model.md), the v5 grad
> struct/opcode *presence* is OBSERVED (the `aws_neuron_isa_tpb_s3s3d1_bn{,2}.h` and the
> opcode enum ship in the `neuron_maverick_arch_isa` tree), but the v5 DVE handler
> **interior** is reasoned from the cayman carve plus the header equality, **not** separately
> carved and decoded here. Treat v5 GradAccum/GradAccum2's `entry`-width, nibble-unpack, and
> FLIX schedule as **INFERRED** until a v5 DVE image is carved and diffed. `[HIGH/OBSERVED
> header; MED/INFERRED interior]`

---

## 8. Boundaries — what GradAccum does NOT own

GradAccum is the **reduce** (the sums); it composes with two siblings into the full
BN-backward:

* **[ParamLoad / ParamLoad2](batchnorm-paramload.md)** (`0x64` / `0x8e`, `S2_BN` /
  `S2_BNPL2`) **stage** `N` (the normalization count), `batch_mean`, and the `A/B/C` terms
  into the DVE per-lane datapath flops. The `1/N` normalization and the `γ·σb-0.5/N`
  prefactor the backward applies use the **ParamLoad-staged `N`** + the 256-entry reciprocal
  Parameter-RAM — **not** a GradAccum field. GradAccum's only immediates are `μb`/`σb-0.5`.
  `[boundary stated]`
* **[Back-Prop](batchnorm-backprop.md)** (`0x65`, `S3S3D3_TT`) is the per-element `d_x`
  **apply** that **consumes** the three sums this op produces, multiply-subtract-scaling them
  with the `γ·σb-0.5/N` prefactor on the shared Tensor-Tensor ALU datapath. GradAccum = the
  **sums**; Back-Prop = the per-element **apply**. `[boundary stated]`

The forward producer of `μb`/`σb-0.5` and the `n·var` aggregation is documented at the
[forward-statistics page](batchnorm-forward.md). `[NOTE — composition map]`

---

## 9. Confidence ledger

**HIGH / OBSERVED (disasm / byte read / header read / compile-verify this pass):**

* The DVE carves reproduce the forward-page anchor byte-identically (sha256 IRAM
  `259769ff…`, DRAM `c106642d…`); `objdump` exit 0; `.dis` = 44,989 lines.
* The two opcodes `0x63` (`"n, use …2"`) / `0x94` (`"Y"`); the 1:1 `struct2opcode`
  (`S3S3D1_BN→0x63`, `S3S3D1_BN2→0x94`); both `S:` self-names at .so off
  `0x18d5c0`/`0x18d5dc` ⇒ DRAM `0x22a0`/`0x22bc`.
* **Both dispatch chains byte-exact:** `0x63 → table1[0x89c]=0x309e → call8 0x1fac`
  (thunk `const16 a2,0xb518; s32i a2,a1,12; call8 0x951c`) → body `0xb518` (`entry a1,48`;
  `const16 a10,0x22a0; call8 0x18010`); and `0x94 → table1[0x960]=0x30a6 → call8 0x1fc8`
  (thunk `const16 a2,0xb55c`) → body `0xb55c` (`entry a1,64`; `const16 a10,0x22bc`); common
  Handler invoke `0x3212` (`j 0x2e87`).
* The staged 3-element descriptors @DRAM `0x2290` (v1) / `0x22e0` (v2), `word[0]=0x00000003`.
* The **compile-verified struct offsets** (gcc): `S3S3D1_BN` `imm0@12`/`src0@16`/`src1@32`/
  `dst@48`/`imm1@56`/`in0_in1_dtype@60`/`immediate_dtype@61`/`out_dtype@62`/
  `num_active_channels@63`; `S3S3D1_BN2` `@60 in0_in1_dtype` / `@61 immediate_out_dtype`
  (`DTYPE_PAIR`) / `@62 imm0_imm1_src` (`IMM_SRC_PAIR`, 1 B) / `@63 num_active_channels`;
  both `sizeof==64`; `TENSOR3D=16`, `TENSOR1D=8`.
* The **decisive distinction**: v2's `extui a2,a2,0,4` + `extui a2,a2,4,4` nibble-unpack +
  two `bnei a2,2` (`==RegPtrImmediate`) tests → reg-fetch sub-handlers `0xb5e4`/`0xb608`;
  v1's lone `movi.n a2,10` (FP32 default). The frame/size delta (`a1,64`/208 B vs
  `a1,48`/68 B). The canonical `IMM_SRC` enum `{InstImm=0, PointerImm=1, RegPtrImm=2}`.
* The header math contract (`imm0=μb`, `imm1=σb-0.5`, `src0=Om`, `src1=∇O'm`, *"3 separate
  results"*, `dst==3`, srcs→fp32) + the `bnga`/`bnga2` validity predicates.
* The MAC/accumulate vocabulary (`ivp_mulusp*`/`muluspa*`/`muluupan16xr16` into `wv0..wv3`,
  `wv0..wv3` = 115 refs, `ivp_baddnormnx16` ×8) + `ivp_radd*` near-absent (×1), confirming
  a stream fold (not a partition fold).
* Per-gen header + struct-body stability (`bn2` body byte-identical cayman==sunda); POOL has
  no BN opcode.

**MED / INFERRED:**

* The exact identity of the **third** accumulator (`d_beta`/`d_gamma` HIGH from
  algebra+header; the 3rd mean-coupling sum INFERRED-HIGH — the header cites an internal
  "BNGradientAccumulate Definition" spec not in the binary).
* The per-term MAC fold **schedule** inside the FLIX-desynced bodies (the `b5ab` MAC bundle
  decodes live but sits at the FLIX/literal boundary → MED; the vocabulary is HIGH).
* The "GradAccum does the Σ_batch internally as a stream fold" attribution + the
  wvec(integer 1536-bit)→fp32 finalize cast (model HIGH via the IVP vocab; the in-body cast
  instructions desynced).
* The "no in-place accumulate-to-prior" negative (INFERRED-HIGH from the WriteTensor +
  src-count contract).
* Non-cayman DVE GradAccum handler-body byte-equality (header presence OBSERVED; bodies not
  exhaustively diffed) → MAVERICK interior INFERRED.

**LOW / WALL:**

* The PERF GradAccum2 dispatch row (`PERF[0x960]=0x000c` overlaps the assertion string pool;
  the DEBUG dispatch is the reliable substrate).
* The exact register slots the two GradAccum2 sub-handlers (`0xb5e4`/`0xb608`) read the
  reg-pointer immediate from (they differ at bytes `+4`/`+0x12`; the precise reg encoding is
  in the FLIX-desynced body).

---

## See also

* [BatchNormalize — Forward Statistics](batchnorm-forward.md) — the `Stats2`/`Aggregate`
  producer of `μb`/`σb-0.5` and the 256-reciprocal Parameter-RAM.
* [BatchNormalize — ParamLoad](batchnorm-paramload.md) — staging `N`/`batch_mean`/`A,B,C`
  into the per-lane datapath flops the backward divide uses.
* [BatchNormalize — Back-Prop](batchnorm-backprop.md) — the per-element `d_x` apply over the
  shared `S3S3D3_TT` Tensor-Tensor format that **consumes** these three sums.
* [Cross-lane reduce](cross-lane-reduce.md) — the POOL `ivp_radd*`/partition fold that
  GradAccum's stream fold is explicitly **not** (distinct engine/axis).
* [The unified datatype model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` code space and
  the `DTYPE_PAIR`/`IMM_SRC_PAIR` nibble packing.
* [B15 — fp32 transcendental seeds (`sp_lookup`)](../../isa/ref/b15-sp-lookup.md) — the
  reciprocal/rsqrt seed semantics (forward-side; `σb-0.5` is precomputed, not on DVE).
* [The Confidence & Walls model](../../reference/confidence-model.md) — what the tags mean.
