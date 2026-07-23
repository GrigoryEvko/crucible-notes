# Dropout

This page decodes the **Dropout** kernel of the NeuronCore GPSIMD ISA — the
**DVE-engine** handler that, per element of an input tensor, draws a pseudo-random
number, compares it against a keep/drop threshold, and emits either the input value
or `0.0`. It pins the opcode (`0x7f`), the 64-byte `s3d3_dropout` operand struct
byte-for-byte from the shipped ISA header, the inline **LFSR** randomness source (the
headline correction of this page), the per-element compare→mask datapath, the dtype
matrix, and the per-generation presence — and it issues several **CORRECTIONS** against
the firmware-body-only backing survey, because the authoritative host **ISA header**
`aws_neuron_isa_tpb_s3d3_dropout.h` was not consulted by that survey and it overturns
three of its headline claims.

Dropout runs on the **Cadence Tensilica Vision-Q7 NX "Cairo" 512-bit FLIX/VLIW DSP**
(`ncore2gp` config, one per NeuronCore) — specifically on the **DVE** (Data/Vector
compute) sequencer, `engine_idx = 3`. It is one of the 28 DVE-only SEQ-dispatch
handlers. Unlike the RNG *producer* kernels, which live on the POOL engine, Dropout
carries its **own inline per-element RNG** — it is *not* a cross-engine consumer of a
staged random tensor (§5).

Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**. Every device fact is byte-pinned to a carve out of
`libnrtucode_internal.so`; every host-ISA fact is read out of the
public `aws_neuron_isa_tpb_*.h` headers shipped in the same customop-lib package.

> **NOTE — the exact objects used.** The firmware
> container is
> `…/custom_op/c10/lib/libnrtucode_internal.so`
> (`sha256 b7c67e898a116454…`, `10,276,288 B`, ELF64 x86-64 DYN — the FW-26/27/28/41
> anchor, re-verified in-task). The DVE DEBUG images are carved by file-offset `dd`
> (`.rodata` file-offset `==` device VA) and disassembled with the native
> `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa
> Tools 14.09) that ships inside the gpsimd-tools package. The recovered `S:` log
> strings and the `s3d3_dropout` / `common.h` ISA structs are themselves binary
> evidence and are cited as such.
>
> | object | file off / size | sha256 (first 16) | disasm |
> |---|---|---|---|
> | `DVE_DEBUG_IRAM` | `0x16f660` / `0x1bcc0` | `259769ff1b47b3b3` | 44,989 lines, exit 0, empty stderr |
> | `DVE_DEBUG_DRAM` | `0x18b320` / `0x06d60` | `c106642d38386cb7` | (string source: `S: Dropout` @`0x1ee7`) |
> | `DVE_PERF_IRAM` | `0x06f600` / `0x15c20` | `9fa066f40f3cafc5` | (IVP-vocabulary corroboration) |
>
> All three sha256 reproduce the DVE image anchors **exactly**; the
> DEBUG IRAM reset vector reads `j 0x1dc` (the SEQ/DVE boot vector).

---

## 1. The headline — and the three corrections

Dropout is decoded two ways that must agree: the **firmware body** on the DVE engine
(the `S: Dropout` handler at IRAM `0x96bc`) and the **authoritative host ISA header**
`aws_neuron_isa_tpb_s3d3_dropout.h`. The header is HIGH/OBSERVED, compile-pinned
(`ISA_STATIC_ASSERT(sizeof == 64)`), and it is the *specification* the firmware
implements. Where the two diverge, the header wins and the firmware-body read is
re-interpreted in its light.

1. **Opcode = `0x7f`, struct = `s3d3_dropout` (64 B).** From `common.h:218`
   (`NEURON_ISA_TPB_OPCODE_DROPOUT = 0x7f, // Y`) and the
   `instruction_mapping.json` `struct2opcode` binding
   `NEURON_ISA_TPB_S3D3_DROPOUT_STRUCT → [NEURON_ISA_TPB_OPCODE_DROPOUT]`.

2. **The inline RNG is an LFSR — *not* a staged Xorwow tensor.** The header's own
   doc-comment states it verbatim: Dropout *"generates a u32 LFSR for each lane for
   each element; converts it to an f32 in range (0.0 to 1.0); compares to threshold."*
   So the random source is the per-lane **LFSR** (`rand_algorithm == LFSR(0)`),
   generated *inline per element* from the generic integer vector-ALU primitives — not
   the POOL Xorwow producer, and not a precomputed random tensor.
   `[HIGH/OBSERVED — header verbatim]`

3. **There is NO `1/(1-p)` survivor scale.** The header's algorithm is a *pure binary
   mask*: a survivor is emitted **unchanged** (`output = input`), a dropped element is
   emitted as `0.0`. There is no multiply, no inverted-dropout `1/(1-p)` rescale on
   device. A byte-scan of the Dropout body confirms it: zero `1.0f`/reciprocal/scale
   literal in `0x96bc..0x9ac4` or the workers. `[HIGH/OBSERVED — header + byte-negative]`

> **CORRECTION (vs the firmware-body survey).** The backing survey, working
> from the firmware body **alone** (it did not read `s3d3_dropout.h`), reached three
> conclusions that the header overturns:
>
> | survey claim | reality (header) | why the survey missed it |
> |---|---|---|
> | "pure consumer of a precomputed random TENSOR; no inline RNG" | **inline per-element LFSR** | the survey scanned for **Xorwow** math (state words, Weyl `362437`) and correctly found none — because Dropout uses **LFSR**, which has *no* embedded tap/Weyl constant and rides inside FLIX-desync'd bundles, so it is invisible to a constant byte-search |
> | "scales survivors by `1/(1-p)` (inverted dropout)" | **no scale; output = input or 0.0** | the survey saw the DVE multiply in the IVP *vocabulary* and over-fit the standard PyTorch inverted-dropout convention; the header has no multiply step |
> | "opcode LOW/UNRECOVERED; CAYMAN-only scope" | opcode **`0x7f`**, present on **all four** generations | the opcode is runtime-bound in the firmware dispatch table, but it is HIGH/OBSERVED in `common.h`; the survey never opened the header tree |
>
> The survey's *firmware-body* facts (entry `0x96bc`, the `S: Dropout` self-name, the
> dtype default `0xA`, the dtype-class checker `0x9ac8`, the two workers
> `0xeff0`/`0xf110`, the FLIX-desync honesty) are all confirmed below and remain
> correct — only the three *interpretations* above are corrected.

4. **Per-generation presence: all four generations.** `OPCODE_DROPOUT = 0x7f, // Y`
   and an `s3d3_dropout.h` ship for **SUNDA (v2), CAYMAN (v3), MARIANA (v4), MAVERICK
   (v5)**. The struct is byte-identical across v2/v3/v4; MAVERICK's only delta is a
   tile-aware channel-range gate (§8).

5. **Threshold polarity is a field, not a fixed convention.** `threshold_type`
   (`DROP_RATE=0` / `KEEP_RATE=1`) selects whether `threshold` is a *drop* rate or a
   *keep* rate, flipping the compare direction (§6).

---

## 2. Opcode, struct binding, and where Dropout lives

**Opcode.** Read verbatim from `aws_neuron_isa_tpb_common.h` (cayman line 218;
byte-identical in sunda/mariana/maverick):

```c
NEURON_ISA_TPB_OPCODE_DROPOUT = 0x7f,    // Y
```

The `// Y` annotation marks Dropout a **maintained, wired** op (contrast the
deprecated `Rand = 0x76, // n`).

**Struct binding.** The shipped `instruction_mapping.json` `struct2opcode` table binds
the operand struct to the opcode exactly once:

```json
"NEURON_ISA_TPB_S3D3_DROPOUT_STRUCT": ["NEURON_ISA_TPB_OPCODE_DROPOUT"]
```

The `S3D3` struct family is the predicated/3-source tensor family — Dropout is a
sibling of `S3D3_CP_PRED_SCALAR` (CopyPredicatedScalar), `S3D3_TS_SELECT`
(TensorScalarSelect), and `S3D3_AC` (Activate) in the same struct group.

**Engine.** Dropout runs on the **DVE** sequencer (`engine_idx = 3`). The handler
self-names via the DEBUG build's own log string `S: Dropout` at DVE DRAM file offset
`0x1ee7` (device VA `0x81ee7`). The handler entry is the Xtensa windowed-ABI function
at **IRAM `0x96bc`**; its prologue immediately loads and logs the self-name:

```asm
96bc:  entry   a1, 80               ; the Dropout handler frame
96c2:  const16 a10, 8               ┐ build DRAM VA 0x81ee7
96c5:  const16 a10, 0x1ee7          ┘ (bytes a4e71e — byte-verified)
96c8:  call8   0x18010              ; LOG "S: Dropout"
```

The handler is registered into the DVE dispatch table by the registration stub at IRAM
`0x216c`:

```asm
216c:  entry   a1, 48
2172:  const16 a2, 0x96bc           ; bytes 24bc96 — the Dropout funcVA  (byte-verified)
2175:  s32i    a2, a1, 12           ; -> the kernel_info funcVA slot [a1+12]
217f:  call8   0x951c              ; the DVE kernel-register routine
```

`[HIGH/OBSERVED]`

> **NOTE — the dispatch chain (SEQ-opcode → trampoline → thin-handler → Handler-invoke).**
> A SEQ-decoded Dropout instruction (opcode `0x7f`) resolves through the DVE dispatch
> table to a trampoline, which jumps to the registered funcVA `0x96bc` (the thin
> handler), which decodes the operand-record frame and hands off to the per-element
> worker (§5). The **opcode→trampoline→funcVA** descriptor binding is a *runtime-bound*
> table entry (the registration `l32r` descriptor literals resolve outside the carved
> IRAM window — the same out-of-carve limitation flagged for [Rand2](rand2.md) and the
> RNG seed-state ops). The **funcVA → body** edge (`0x96bc`) and the **opcode value**
> (`0x7f`, from `common.h`) are both HIGH/OBSERVED; only the descriptor byte between
> them is runtime-bound. `[opcode HIGH; descriptor MED/out-of-carve; funcVA→body HIGH]`

---

## 3. The `s3d3_dropout` operand struct — byte-exact, compile-pinned

The authoritative operand layout is `NEURON_ISA_TPB_S3D3_DROPOUT_STRUCT`
(`aws_neuron_isa_tpb_s3d3_dropout.h`), `ISA_STATIC_ASSERT(sizeof == 64)`:

| off | size | field | type | meaning / constraint |
|----:|----:|-------|------|----------------------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `header.opcode == Dropout (0x7f)` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | semaphore/event block |
| 12 | 4 | `reserved0[4]` | `uint8_t` | must be `0` |
| 16 | 16 | `src_mem_pattern` | `TENSOR3D` | the **input** activations (ADDR4 + 3×int16 step + 3×uint16 num_elem) |
| 32 | 1 | `in_dtype` | `DTYPE` | input dtype — **FP32R not allowed** (`DtypeAllowFP32R::False`) |
| 33 | 1 | `out_dtype` | `DTYPE` | output dtype — **must `==` `in_dtype`** (FP32R allowed in the enum check) |
| 34 | 1 | `num_active_channels` | `uint8_t` | `1..128` (`POOLING_NUM_CHANNELS`) |
| 35 | 1 | `threshold_src` | `IMM_SRC` | where `threshold` comes from: inst-imm / ptr / reg-ptr |
| 36 | 1 | `threshold_dtype` | `DTYPE` | dtype of the threshold value (`0` → FP32 for back-compat) |
| 37 | 6 | `reserved1[6]` | `uint8_t` | must be `0` |
| 43 | 1 | `threshold_type` | `DROPOUT_THRESHOLD_TYPE` | `DROP_RATE(0)` / `KEEP_RATE(1)` |
| 44 | 4 | `threshold` | `IMM_VAL_INST_FIELD` | the keep/drop rate: fp32 imm / ptr / reg |
| 48 | 16 | `dst_mem_pattern` | `TENSOR3D` | the **output** tensor (same element count as src) |

`[HIGH/OBSERVED — offsets are the header's own `// (NN)` column]`

The supporting types:

```c
typedef struct NEURON_ISA_TPB_TENSOR3D {
    NEURON_ISA_TPB_ADDR4 start_addr;     // 4
    int16_t              step_elem[3];   // 6
    uint16_t             num_elem[3];    // 6
} NEURON_ISA_TPB_TENSOR3D;               // = 16

typedef enum NEURON_ISA_TPB_IMM_SRC {
    NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE = 0,  // fp32 value in the instruction
    NEURON_ISA_TPB_IMM_SRC_POINTER_IMMEDIATE     = 1,  // PartitionOffset ptr to the fp32 immediate
    NEURON_ISA_TPB_IMM_SRC_REG_PTR_IMMEDIATE     = 2,  // PartitionOffset ptr from a register
} NEURON_ISA_TPB_IMM_SRC;

typedef enum NEURON_ISA_TPB_DROPOUT_THRESHOLD_TYPE {
    NEURON_ISA_TPB_DROPOUT_THRESHOLD_TYPE_DROP_RATE = 0,  // if rand() < threshold, flush the element to zero
    NEURON_ISA_TPB_DROPOUT_THRESHOLD_TYPE_KEEP_RATE = 1,  // if rand() > threshold, flush the element to zero
} NEURON_ISA_TPB_DROPOUT_THRESHOLD_TYPE;
```

`[HIGH/OBSERVED — `common.h:649 / 1207 / 1135`]`

> **NOTE — only one tensor operand pair, not three.** The struct carries **exactly two
> tensors** (`src_mem_pattern@16`, `dst_mem_pattern@48`) plus a **scalar** threshold —
> there is **no random-tensor operand**. This is the structural proof that the
> randomness is generated *inline* (the LFSR per lane per element), not staged as a
> third tensor. A reimplementer wiring this op supplies one input tensor, one output
> tensor, and one threshold scalar — nothing else. `[HIGH/OBSERVED — the struct has no
> random-source pointer field]`

> **GOTCHA — `src` and `dst` element counts must match, and dtypes must be equal.**
> The validation block enforces `same_element_count_t3d(src, dst)`
> (`s3d3_dropout_src_dst_count_check`) and `out_dtype == in_dtype`
> (`s3d3_dropout_same_src_dst_type`). Dropout is a **bitvec op** — it does not convert
> dtype between input and output; the threshold-compare is the only float operation.
> A mismatched src/dst dtype or element count fails decode.

### 3.1 The firmware handler's frame decode (corroboration)

The DVE handler at `0x96bc` decodes the SEQ-delivered operand record into its **local
windowed-ABI frame** (`a1`-relative slots), then hands the frame to the worker. These
`a1`-relative offsets are the handler's *stack frame*, distinct from the 64-byte
`s3d3_dropout` *operand record* of §3; they are the firmware's working copy of the
decoded fields:

```asm
96d7:  s32i   a2, a1, 8            ; a count/dim field -> frame[8]
96e1:  s8i    a2, a1, 12           ; dtype byte -> frame[12]
96eb:  s8i    a2, a1, 28           ; config byte -> frame[28]
96f8:  l32i.n a2,[a1+8] ; addi a2,a2,-1 ; movi.n a3,1 ; saltu a2,a2,a3 ; s8i a2,[a1+24]
       ;   frame[24] = ((frame[8]-1) < 1) = "count == 1?"  (single-element / scalar-broadcast flag)
9704:  l8ui   a10,[a1+28] ; call8 0x9ac8 ; s8i a10,[a1+20]
       ;   frame[20] = is_integer_dtype(frame[28])          (the dtype-class checker, §4)
9710:  bnez.n a2, 0x971a ; movi.n a2, 10 ; s8i a2,[a1+12] ; s8i a2,[a1+16]
       ;   DTYPE DEFAULT: dtype == 0 (INVALID) -> 10 (=0xA = FP32); stored to frame[12] AND frame[16]
```

The dtype default is byte-verified: `movi.n a2, 10` at `0x9715` (bytes `0ca2`), reached
by the `bnez.n a2, 0x971a` / `j 0x9715` guard at `0x9710` — i.e. *"if the operand dtype
byte is zero, default it to `0xA` = FP32."* This matches the header's back-compat note
*"threshold_dtype == 0 is treated as FP32 for backward compatibility"* and the data-path
default. `[HIGH/OBSERVED]`

---

## 4. The dtype-class checker `0x9ac8`

The handler routes the int-vs-float vector lane path through an **is-integer-dtype**
predicate at IRAM `0x9ac8`, decoded byte-exact:

```asm
9ac8:  entry  a1, 48
9ad3:  beqi   a3, 2, 0x9b13        ; dtype == 2  (INT8)   -> return 1
9ade:  beqi   a3, 4, 0x9b13        ; dtype == 4  (INT16)  -> return 1
9ae9:  beqi   a3, 8, 0x9b13        ; dtype == 8  (INT32)  -> return 1
9af4:  beqi   a3, 3, 0x9b13        ; dtype == 3  (UINT8)  -> return 1
9aff:  beqi   a3, 5, 0x9b13        ; dtype == 5  (UINT16) -> return 1
9b08:  addi   a2, a2, -9 ; saltu a2,a2,a3   ; (dtype - 9) < 1  -> dtype == 9 (UINT32) -> 1
9b13:  extui  a2, a2, 0, 1 ; retw.n          ; return bool
```

The `beqi` immediates **are** the `NEURON_ISA_TPB_DTYPE` codes. The valid-integer set is:

| dtype | code | in checker? |
|---|---:|:---:|
| `INT8` | `0x2` | ✓ |
| `UINT8` | `0x3` | ✓ |
| `INT16` | `0x4` | ✓ |
| `UINT16` | `0x5` | ✓ |
| `INT32` | `0x8` | ✓ |
| `UINT32` | `0x9` | ✓ |
| `BFLOAT16` | `0x6` | ✗ (float path) |
| `FP16` | `0x7` | ✗ (float path) |
| `FP32` | `0xA` | ✗ (float path, the default) |

So the checker returns true for `{INT8, UINT8, INT16, UINT16, INT32, UINT32} =
{2,3,4,5,8,9}` and false for the FP dtypes — it selects the **integer vs float**
vector datapath. `[HIGH/OBSERVED; int/float routing INFERRED-HIGH]` The full enum
(`common.h:722`):

```c
typedef enum NEURON_ISA_TPB_DTYPE {
    INVALID=0x0, UINT64=0x1, INT8=0x2, UINT8=0x3, INT16=0x4, UINT16=0x5,
    BFLOAT16=0x6, FP16=0x7, INT32=0x8, UINT32=0x9, FP32=0xA, FP32R=0xB,
    INT64=0xC, FP8_EXP3=0xD, FP8_EXP4=0xE, FP8_EXP5=0xF,
} NEURON_ISA_TPB_DTYPE;
```

> **NOTE — the int path runs the same compare/select datapath in the integer lanes.**
> Dropout is a **bitvec op** (header: *"single dtype for input/output"*). For an integer
> `in_dtype`, the input is moved through unchanged for survivors and zeroed for drops;
> the LFSR draw is still cast to fp32 for the threshold compare (the threshold is fp32
> unless an FP `in_dtype` allows an FP threshold — §6). The integer lanes do not float
> the *data*, only the *random draw vs threshold* comparison. `[in/out same-dtype HIGH;
> int-lane select INFERRED-HIGH]`

---

## 5. The inline LFSR randomness path — the headline finding

This is where the page departs hardest from the firmware-body survey. The header's
doc-comment is the specification:

```
// Dropout
//  - generates a u32 LFSR for each lane for each element
//  - converts it to an f32 in range (0.0 to 1.0)
//  - compares to threshold (ptr or immediate)
```

Three facts follow, each reconciled against the wider RNG subsystem:

1. **The RNG is the LFSR, not Xorwow.** Dropout's inline generator is the **LFSR** arm
   of the `rand_algo` enum (`{LFSR=0, PCG32=1, PHILOX=2, XORWOW=3}`, see
   [rng-lfsr-dispatch.md](rng-lfsr-dispatch.md)). The LFSR is the **1-`u32`-word-per-lane**
   generator: each of the (up to) 128 lanes is an independent single-register LFSR,
   seeded from `rand_state[0]`, advanced one or more times, emitting one `u32`. This is
   exactly the "generates a u32 LFSR for each lane for each element" the header
   describes. `[HIGH/OBSERVED — header verbatim]`

2. **It is generated INLINE, per element — not staged.** The operand struct has **no
   random-tensor field** (§3), and the handler makes **no cross-engine call to the POOL
   RNG**. The LFSR advance is composed from the generic integer vector-ALU primitives
   (the shift+xor recurrence over GF(2)) that the DVE datapath already exposes — the same
   composition pattern the POOL Xorwow uses, but in the DVE engine and per element. The
   firmware-body survey's *negative* finding ("no Xorwow state, no Weyl `362437`, no
   cross-engine RNG call") is **fully consistent with an inline LFSR**: the LFSR carries
   **no** Weyl constant and **no** embedded tap-mask constant (an exhaustive scan of the
   POOL LFSR body for the eight classical 32-bit tap masks returned zero — see
   [rng-lfsr-dispatch.md](rng-lfsr-dispatch.md) §5.4), so a constant byte-search *cannot*
   find it; it rides inside the FLIX-desync'd worker. `[HIGH/OBSERVED; the exact LFSR
   recurrence is a named wall]`

3. **The `uint32 → float[0,1)` cast is the `0x3F800000` seam.** The header's "converts
   it to an f32 in range (0.0 to 1.0)" is the canonical `uint32 → fp32` uniform cast: take
   the mantissa bits, OR in the `0x3F800000` (`1.0f`) exponent field to land in `[1,2)`,
   subtract `1.0` to get `[0,1)`. On the DVE side the convert op is the
   `ivp_ufloatn_2x32`-class (uint32 → fp32) primitive. A whole-IRAM scan for the
   `0x3f80` (1.0f high half) literal finds it **only once, as a branch target**
   (`beqz a5, 0x3f80`), never as a clean literal — i.e. the cast constant rides inside
   the FLIX-desync'd worker, exactly as on the POOL producer side and in
   [Rand2](rand2.md). `[seam direction HIGH; mantissa-fill cast MED]`

```c
/* The inline per-element LFSR randomness path the Dropout handler runs, per lane.
 * (rand_algorithm == LFSR(0); the LFSR is the 1-u32-word/lane generator of
 *  rng-lfsr-dispatch.md.)  The recurrence taps are a NAMED WALL — unrecovered. */
static inline uint32_t dropout_lfsr_next(uint32_t *state_word /* rand_state[0], per lane */)
{
    /* 32-bit single-word xorshift-style LFSR over GF(2), advanced rand_num_steps>=1
     * times.  Composed from the generic int vector ALU (xor / sll / srl) — NO dedicated
     * LFSR opcode, NO embedded tap-mask constant.  The exact taps / Galois-vs-Fibonacci
     * direction are NOT byte-recoverable (FLIX-desync'd worker; header silent on poly). */
    *state_word = lfsr_advance(*state_word);   /* the unrecovered recurrence */
    return *state_word;                        /* one u32 per lane */
}

static inline float lfsr_uniform01(uint32_t u)
{
    /* "converts it to an f32 in range (0.0 to 1.0)"  — the 0x3F800000 seam.
     * f = ((u >> 9) | 0x3F800000) gives [1,2); subtract 1.0 -> [0,1).
     * DVE op: the ivp_ufloatn_2x32-class uint32->fp32 convert. */
    union { uint32_t b; float f; } v;
    v.b = (u >> 9) | 0x3F800000u;              /* [1, 2) */
    return v.f - 1.0f;                         /* [0, 1) */
}
```

> **QUIRK — Dropout's RNG is the LFSR; Rand2's is XORWOW; they do not share a source.**
> The two DVE stochastic ops pick *different* algorithms from the same `rand_algo` enum:
> [Rand2](rand2.md) is hard-restricted to `XORWOW(3)` (`restrict_rand2_algorithm`),
> while Dropout uses `LFSR(0)`. A reimplementer must not assume Dropout consumes the POOL
> Xorwow draws — it generates its own LFSR stream inline. `[HIGH/OBSERVED —
> `s3d3_dropout.h` vs `d3_rand.h`]`

> **WALL — the LFSR feedback polynomial is a named wall (`closable-with-corpus` /
> `closable-with-hardware`).** The Dropout RNG's exact taps, width-direction, and
> seed-state plumbing are not byte-recoverable here: the LFSR carries no embedded
> tap-mask constant and its advance rides inside FLIX-desync'd vector bundles (the flat
> DEBUG image has no `.xt.prop` property table). What **is** specified: 32-bit single-word
> per-lane state, seeded from `rand_state[0]`, advanced `rand_num_steps ≥ 1` times,
> emitting one `u32`, cast to fp32 `[0,1)`. A reimplementer needing bit-identical output
> must recover the recurrence by a captured device round-trip. See
> [rng-lfsr-dispatch.md](rng-lfsr-dispatch.md) §5.4 for the full LFSR characterization.

---

## 6. The compare → mask datapath (no scale)

Per the header, the per-element operation is a **pure binary mask** keyed on the
threshold and the `threshold_type`:

```c
/* The Dropout element op (header doc-comment, verbatim semantics).
 * NOTE: there is NO survivor scale — output is either `input` or `0.0`. */
if (threshold_type == DROP_RATE) {       /* threshold is a DROP rate */
    out = (rand_f32 > threshold) ? input : 0.0f;   /* keep when rand > drop_rate */
} else /* KEEP_RATE */ {                  /* threshold is a KEEP rate */
    out = (rand_f32 < threshold) ? input : 0.0f;   /* keep when rand < keep_rate */
}
```

Equivalently, from the enum's own comments: **DROP_RATE** flushes the element to zero
*"if rand() < threshold"*; **KEEP_RATE** flushes it to zero *"if rand() > threshold."*
The two phrasings agree — `DROP_RATE` keeps the survivor when `rand > drop_rate`,
`KEEP_RATE` keeps it when `rand < keep_rate`. `[HIGH/OBSERVED — the header doc-comment +
the `DROPOUT_THRESHOLD_TYPE` enum comments]`

On the DVE datapath this realizes as three IVP vector ops (the value vocabulary is
OBSERVED in the cleaner PERF IRAM histogram; the per-op role assignment is INFERRED-HIGH
from the header semantics):

| step | DVE IVP op family | role |
|---|---|---|
| `uint32 → fp32` | `ivp_ufloatn_2x32`-class | cast the LFSR draw to a float in `[0,1)` (§5) |
| compare → predicate | `ivp_oltn_2xf32t` / `ivp_oltnxf16t` (ordered fp less-than, `t`-suffix = per-lane `vbool` write) | `rand < threshold` (or `>`) → the per-lane keep/drop **mask** |
| predicated select | `ivp_dsel*` / `ivp_sel*..t` | select `input`-vs-`0.0` per lane under the mask predicate |

`[IVP op family HIGH/OBSERVED; bundle order + operands MED]`

> **CORRECTION — there is NO multiply, NO `1/(1-p)` survivor scale.** The firmware-body
> survey listed a multiply (`ivp_muln_2xf32t`) as the "scale-by-`1/(1-p)`" step. That op
> exists in the DVE *vocabulary*, but the header's Dropout algorithm has **no multiply**:
> a survivor passes through **unchanged** (`output = input`), a drop becomes `0.0`. A
> byte-scan of `0x96bc..0x9ac4` and the workers finds **zero** `1.0f`/reciprocal/scale
> literal. Inverted-dropout's `1/(1-p)` rescale, *if used by a framework*, must be applied
> by a **separate** op (e.g. a TensorScalar multiply), not by this Dropout instruction.
> The on-device Dropout is mask-only. `[HIGH/OBSERVED — header semantics + byte-negative]`

### 6.1 The threshold sourcing + dtype rules

The `threshold` value (off 44) is sourced per `threshold_src` (off 35):

- `INSTRUCTION_IMMEDIATE(0)` → an inline fp32 in the instruction (and then
  `threshold_dtype` **must be FP32**).
- `POINTER_IMMEDIATE(1)` / `REG_PTR_IMMEDIATE(2)` → a partition-offset pointer to the
  threshold value.

The threshold dtype rules (`s3d3_dropout_threshold_dtype_check`, verbatim from the
header): for an **immediate** threshold, `threshold_dtype` must be FP32; for a **pointer**
threshold, if `in_dtype` is integer the threshold must be FP32, and if `in_dtype` is FP
the threshold may be any FP dtype. `threshold_dtype == 0` (Invalid) is treated as FP32
for backward compatibility. `[HIGH/OBSERVED — the header's `threshold_dtype_fp32` and
`s3d3_dropout_threshold_dtype_check` predicate blocks]`

---

## 7. The per-element worker loop (workers `0xeff0` / `0xf110`)

The heavy per-element vector loop runs in two **Dropout-specific** workers, called only
from the Dropout handler:

- `call8 0xeff0` (at `0x9624`) — worker A: reads the operand record, sets up the vector
  context (the `src`/`dst` base pointers, the strides, the dtype/threshold mode).
- `call8 0xf110` (at `0x9748`) — worker B: the FLIX-VLIW SIMD loop (generate LFSR →
  cast → compare → select → store), iterated over `num_tensor_elements` (the product of
  the three `TENSOR3D.num_elem` dims).

```c
/* The Dropout handler @0x96bc (DVE, frame 80) — the byte-pinned skeleton.
 * The FLIX vector-loop body is elided as MED (FLIX desync). */
static void dve_dropout_handler(dve_ctx_t *ctx /* a1 frame */)
{
    dbg_log("S: Dropout");                       /* const16 0x1ee7 ; call8 0x18010 */

    decode_operand_frame(ctx);                   /* 0x96d0..0x9707: dtype, count==1, config */
    ctx->is_int = dtype_is_integer(ctx->dtype);  /* call8 0x9ac8 (§4) */
    if (ctx->dtype == 0) ctx->dtype = 10;        /* DEFAULT -> FP32 (movi.n a2,10 @0x9715) */

    dropout_worker_setup(ctx);                   /* call8 0xeff0 @0x9624 — worker A */
    config_pack(ctx);                            /* call8 0x9b18 flag packer */

    /* worker B: the per-element SIMD loop over num_tensor_elements */
    dropout_simd_loop(ctx);                      /* call8 0xf110 @0x9748 — worker B */
    /*   per chunk of N lanes (16-32 per 512-bit vector):
     *     1. LFSR-advance the per-lane u32 state          (inline; §5)
     *     2. uint32 -> fp32 [0,1)                          (ivp_ufloatn_2x32-class)
     *     3. compare rand vs threshold -> vbool mask       (ivp_oltn_2xf32t, polarity per threshold_type)
     *     4. load the src activations chunk
     *     5. select input-vs-0.0 under the mask            (ivp_dsel*/sel*..t)
     *     6. store to dst ; advance ; loop until i >= N
     *   (NO multiply / NO 1/(1-p) scale step.) */

    /* tail: l32i a10,[a1+4] ; call8 0x9b58 ; retw.n  @0x9abf..0x9ac4 */
}
```

`[HIGH/OBSERVED op set + loop + worker call edges; bundle order MED]`

> **GOTCHA — FLIX-bundle desync bounds the worker recovery to MED.** The two Dropout
> workers are densely-scheduled FLIX-VLIW (32-byte bundles, up to a scalar/branch slot +
> 4 IVP vector slots). The flat DEBUG IRAM carries no `.xt.prop` FLIX property table, so a
> linear sweep loses bundle sync across literal/selector boundaries: slot-0 branches print
> garbage targets and many spans render as `.byte`. The IVP *vector slots* within a
> recovered bundle decode to internally-valid IVP ops, but their exact operands/order are
> MED. The cleaner PERF IRAM (no log call-sites) corroborates the IVP op *vocabulary*
> (compare-to-predicate / uint→float / predicated-select), not the Dropout-worker-exact
> schedule. `[the corpus FLIX-desync ceiling]`

---

## 8. The dtype matrix

Dropout is a **bitvec op** with a **single dtype** for input and output
(`in_dtype == out_dtype`). The decode-time validation is:

- `is_valid_dtype(in_dtype, DtypeAllowFP32R::False)` — input may be any valid dtype
  **except FP32R**.
- `is_valid_dtype(out_dtype, DtypeAllowFP32R::True)` — output check allows FP32R in the
  enum predicate, but `out_dtype == in_dtype` forces them equal, so FP32R is effectively
  excluded by the input check.

| dtype | code | Dropout `in`/`out`? | path |
|---|---:|:---:|---|
| `FP32` | `0xA` | YES (the default) | float |
| `FP16` | `0x7` | YES | float |
| `BFLOAT16` / `BF16` | `0x6` | YES | float |
| `FP32R` | `0xB` | **NO** (`DtypeAllowFP32R::False` on input) | — |
| `INT8` | `0x2` | YES | integer |
| `UINT8` | `0x3` | YES | integer |
| `INT16` | `0x4` | YES | integer |
| `UINT16` | `0x5` | YES | integer |
| `INT32` | `0x8` | YES | integer |
| `UINT32` | `0x9` | YES | integer |

`[HIGH/OBSERVED — the `is_valid_dtype` calls in `is_valid_dropout`]` The default dtype,
when the operand byte is `0` (Invalid), is **`0xA` = FP32** (the `movi.n a2,10`
@`0x9715`, §3.1). The `threshold_dtype` defaults to FP32 too (header back-compat).

> **NOTE — the c10 `ScalarType` bridge.** The host customop layer maps PyTorch
> `c10::ScalarType` (`…/c10/core/ScalarType.h`) onto these `NEURON_ISA_TPB_DTYPE` codes
> when it lowers a `torch.nn.functional.dropout` to the device instruction. The device
> only ever sees the `NEURON_ISA_TPB_DTYPE` byte; the ScalarType bridge is host-side.
> See [the dtype model](dtype-model.md). `[header HIGH/OBSERVED; lowering map host-side]`

---

## 9. Per-generation presence

Dropout is present on **every** generation that ships an arch-isa header — both an
`s3d3_dropout.h` and `OPCODE_DROPOUT = 0x7f, // Y` ship for all four:

| GEN | `OPCODE_DROPOUT` | `s3d3_dropout.h` | struct | delta |
|---|---|---|---|---|
| **SUNDA** (v2) | `0x7f` `// Y` | YES (NC-v2) | 64 B, identical | — |
| **CAYMAN** (v3) | `0x7f` `// Y` | YES (NC-v3) | 64 B, identical | the byte-decoded reference image (§2–§7) |
| **MARIANA** (v4) | `0x7f` `// Y` | YES (NC-v4) | 64 B, identical | — |
| **MAVERICK** (v5) | `0x7f` `// Y` | YES (NC-v5) | 64 B | **tile-aware channel gate** (below) |

`[HIGH/OBSERVED — `OPCODE_DROPOUT` + `s3d3_dropout.h` in all four arch dirs]`

The struct + validation are **byte-identical** across SUNDA/CAYMAN/MARIANA. The **only**
MAVERICK (v5) delta is the channel-range gate — the same NC-v5 tile-aware change
[Rand2](rand2.md) carries:

```c
/* CAYMAN/MARIANA (v3/v4): */
//  && has_valid_active_channel_range(num_active_channels, POOLING_NUM_CHANNELS)
/* MAVERICK (v5): */
//  && has_valid_active_channel_range_with_tile(num_active_channels, DVE_NUM_CHANNELS, header.inst_flags)
```

Both channel counts are `128` (`POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128U`); v5
adds tile-aware ranging keyed on `header.inst_flags`. `[HIGH/OBSERVED — header diff]`

> **NOTE — v5/MAVERICK interior is header-OBSERVED only.** The MAVERICK Dropout *header*
> is read directly (HIGH); its firmware *body* is not separately carved (the v5
> DVE image is nm-aliased onto its DRAM symbol, per the corpus v5 policy). The decoded
> firmware body (§2–§7) is **CAYMAN**; the MAVERICK body is INFERRED identical-family from
> the byte-identical struct + the matching opcode. Do not cite a MAVERICK Dropout handler
> address as byte-observed. `[presence HIGH/OBSERVED; v5 interior INFERRED]`

---

## 10. Reconciliation with the RNG subsystem

Dropout closes against the RNG subsystem pages as the **LFSR-arm DVE consumer**:

- **vs [rng-lfsr-dispatch.md](rng-lfsr-dispatch.md):** that page pins Dropout's inline
  RNG to the **LFSR** from this same `s3d3_dropout.h` comment ("generates a u32 LFSR for
  each lane for each element"). This page is the consumer-side decode of that finding. The
  LFSR is the 1-`u32`-word/lane generator (`rand_algorithm == LFSR(0)`), seeded from
  `rand_state[0]`, advanced `rand_num_steps ≥ 1` times; its exact recurrence is a named
  wall. **MATCH.**
- **vs [rng-xorwow-sw.md](rng-xorwow-sw.md) / [rng-xorwow-tie.md](rng-xorwow-tie.md):**
  the POOL Xorwow producer is a **different** generator on a **different** engine.
  Dropout does **not** consume the POOL Xorwow draws — its randomness is the inline DVE
  LFSR. The firmware-body survey's "no Xorwow state / no Weyl / no cross-engine RNG call"
  is the *correct* observation that Dropout is **not** a Xorwow consumer — but the right
  conclusion is "it uses the inline LFSR," not "it consumes a staged Xorwow tensor."
  **RECONCILED via correction.**
- **vs [rand2.md](rand2.md):** Rand2 (opcode `0xe2`, DVE) is the sibling DVE stochastic
  op, but it is hard-restricted to **XORWOW + UniformInRange** and emits a random
  *tensor*; Dropout (opcode `0x7f`, DVE) uses **LFSR** inline and emits a *masked* tensor.
  Both are DVE thin handlers; they differ in algorithm, post-process, and output shape.
  **CONTRASTED.**

The `0x3F800000` (`1.0f`) float seam is the common `uint32 → fp32 [0,1)` normalization
across the whole subsystem — for Dropout it is the "converts to an f32 in range (0.0 to
1.0)" step the header names. `[HIGH the seam direction / OBSERVED; the exact mantissa-fill
cast MED through the FLIX desync.]`

---

## 11. Honesty ledger

**HIGH / OBSERVED** (direct disasm, byte read, header read, or compile-pinned struct):

- Opcode `OPCODE_DROPOUT = 0x7f, // Y` (`common.h:218`, all four arches);
  `struct2opcode` binding `S3D3_DROPOUT_STRUCT → DROPOUT` (`instruction_mapping.json`).
- The 64-byte `NEURON_ISA_TPB_S3D3_DROPOUT_STRUCT` layout (`ISA_STATIC_ASSERT == 64`):
  `src@16` / `in_dtype@32` / `out_dtype@33` / `num_active_channels@34` /
  `threshold_src@35` / `threshold_dtype@36` / `threshold_type@43` / `threshold@44` /
  `dst@48` — and the **absence** of any random-tensor field.
- The inline RNG is the **LFSR** (`s3d3_dropout.h` doc-comment verbatim: "generates a
  u32 LFSR for each lane for each element; converts it to an f32 in range (0.0 to 1.0);
  compares to threshold"); the `DROP_RATE(0)` / `KEEP_RATE(1)` polarity from the
  `DROPOUT_THRESHOLD_TYPE` enum.
- The Dropout algorithm is a **pure binary mask** (`output = input` or `0.0`); **no**
  multiply, **no** `1/(1-p)` scale (header semantics + byte-negative on
  `1.0f`/reciprocal in `0x96bc..0x9ac4`).
- Firmware body: entry `0x96bc` logging `S: Dropout` (DRAM `0x1ee7`, `const16
  a10,0x1ee7` @`0x96c5`); registration stub `0x216c` (`const16 a2,0x96bc` @`0x2172`,
  bytes `24bc96`); dtype default `0xA = FP32` (`movi.n a2,10` @`0x9715`, bytes `0ca2`);
  the dtype-class checker `0x9ac8` admitting `{2,3,4,5,8,9}` (the `beqi` immediates ARE
  the dtype codes); the count==1 `saltu` flag; the two Dropout-specific workers
  `0xeff0`/`0xf110`.
- Carve sha256 reproduce the DVE anchors exactly (DEBUG IRAM `259769ff`,
  DEBUG DRAM `c106642d`, PERF IRAM `9fa066f4`); DEBUG IRAM 44,989 disasm lines, exit 0,
  empty stderr; reset vector `j 0x1dc`.
- Per-gen presence: Dropout on SUNDA/CAYMAN/MARIANA/MAVERICK; struct byte-identical on
  v2/v3/v4; the MAVERICK-only tile-aware channel-range gate (header `diff`).

**MED / INFERRED:**

- The exact per-step order/operands of the SIMD-loop bundles (generate-LFSR / cast /
  compare / select / store): the op SET + the loop are HIGH; the precise FLIX-bundle
  schedule is MED (flat-image desync, no `.xt.prop` table).
- The `uint32 → fp32 [0,1)` mantissa-fill cast: the `0x3F800000` seam direction is HIGH;
  the exact cast bundle is MED (the one `0x3f80` text hit in DVE IRAM is a branch target,
  not a literal).
- The int-vs-float lane routing of the bitvec select (from the dtype-class checker +
  the same-dtype bitvec-op header): INFERRED-HIGH.
- The opcode→trampoline→funcVA descriptor byte (runtime-bound, out-of-carve); the
  opcode (HIGH) and the funcVA→body edge (HIGH) bracket it.
- The MAVERICK firmware body (header-OBSERVED; body INFERRED identical-family).

**LOW / UNRECOVERED (named wall):**

- The Dropout LFSR's exact feedback polynomial / tap positions / Galois-vs-Fibonacci
  direction (no embedded tap-mask constant; FLIX-desync'd worker; header silent on the
  poly). 32-bit single-word per-lane state, seeded from `rand_state[0]`, advanced
  `rand_num_steps ≥ 1`, output `u32` cast to fp32 `[0,1)` is fully specified; the precise
  recurrence is a **named wall** (`closable-with-corpus` / `closable-with-hardware`). See
  [rng-lfsr-dispatch.md](rng-lfsr-dispatch.md) §5.4.

---

## Cross-references

- **[RNG — LFSR + `rand_algo` Dispatch Tree](rng-lfsr-dispatch.md)** — the LFSR
  generator Dropout inlines; the `rand_algo` enum; the LFSR state model and the named
  wall on its recurrence.
- **[RNG — Xorwow Software Path](rng-xorwow-sw.md)** /
  **[RNG — Xorwow TIE Hardware Path](rng-xorwow-tie.md)** — the POOL Xorwow producer
  Dropout does **not** consume (the correction of §1).
- **[Rand2 (user random-tensor op)](rand2.md)** — the sibling DVE stochastic op
  (XORWOW + UniformInRange, opcode `0xe2`), contrasted in §10.
- **[The dtype model](dtype-model.md)** — the `NEURON_ISA_TPB_DTYPE` codes and the c10
  `ScalarType` bridge used in §4/§8.
- **[The Confidence & Walls Model](../../reference/confidence-model.md)** — the
  `HIGH/MED/LOW × OBSERVED/INFERRED` tags, the FLIX-desync ceiling, and the
  v5/Maverick header-OBSERVED-only policy this page applies.
