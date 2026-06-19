# Rand2 (user random-tensor op)

> **Scope.** Rand2 is **opcode `0xe2`** — the maintained, user-facing random-tensor
> generation op of the NeuronCore GPSIMD ISA. This page decodes its operand struct
> (`d3_rand`, 64 B, compile-verified), proves its hard XORWOW-only +
> UniformInRange-only contract from the header bytes, decodes the DVE-engine
> thin-consumer handler instruction-exact (entry `0xf2cc`, self-name `"S: Rand2"`),
> contrasts it byte-for-byte against the deprecated `Rand` op (`0x76`, `d4_rand`),
> and tabulates per-generation presence and the accepted dtypes. **This page CAPS
> the GPSIMD RNG subsystem** (the producer side is documented in
> [`rng-xorwow-sw.md`](./rng-xorwow-sw.md) / [`rng-xorwow-tie.md`](./rng-xorwow-tie.md),
> seed state in [`rng-seed-state-ops.md`](./rng-seed-state-ops.md), the LFSR fork in
> [`rng-lfsr-dispatch.md`](./rng-lfsr-dispatch.md), and the sibling consumer in
> [`dropout.md`](./dropout.md)).
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md).

---

## 1. TL;DR — the seven pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | Rand2 is **opcode `0xe2`** (`// Y` = supported/maintained); its predecessor `Rand` is `0x76` (`// n, ucode exists, not maintained/used`). | `aws_neuron_isa_tpb_common.h:302` / `:214` | HIGH/OBSERVED |
| 2 | Rand2 **runs on the DVE engine**, not POOL. The `"S: Rand2"` self-name string lives in the `NX_DVE` DEBUG DRAM image; the handler is a DVE SEQ-dispatch function. | `MARIANA_NX_DVE_DEBUG_DRAM` + handler decode | HIGH/OBSERVED |
| 3 | The operand struct is the **64-byte `NEURON_ISA_TPB_D3_RAND_STRUCT`** (`d3_rand`), distinct from `Rand`'s `d4_rand`. | header + `ISA_STATIC_ASSERT==64` + `gcc` compile | HIGH/OBSERVED |
| 4 | The contract is **hard-restricted**: `rand_algorithm == XORWOW` ONLY, `post_process == UniformInRange` ONLY, `dtype` must be FP (FP32R allowed). | `restrict_rand2_algorithm` / `has_valid_rand2_post_processing` / `has_valid_rand2_dtype` | HIGH/OBSERVED |
| 5 | The handler (entry `0xf2cc`, frame 80) is a **thin randomness CONSUMER** in the Dropout mould — no inline Xorwow state, no Weyl constant in DVE IRAM. | handler disasm + Weyl-absence sweep | HIGH/OBSERVED |
| 6 | The XORWOW draws are **staged cross-engine** (the POOL `Rng (XORWOW)` producer → DMA), then DVE applies the affine UniformInRange map. | `"MEMSET/RNG"` + `"push GENERATE to DMA"` strings | consumer HIGH/OBSERVED; staging INFERRED |
| 7 | Per-gen: Rand2 present on **MARIANA / MARIANA_PLUS / MAVERICK**; absent on **CAYMAN / SUNDA** (no `d3_rand.h`, no `"S: Rand2"`). | header dir + 3× string-image membership | HIGH/OBSERVED; MAVERICK interior INFERRED |

---

## 2. Provenance / carve anchors

All facts derive from static analysis of the shipped device-firmware blob (disassembled
with the Cadence Xtensa toolchain, `XTENSA_CORE=ncore2gp`, Vision-Q7 FLIX/VLIW) and the
shipped host customop-lib **ISA C headers**. No source was consulted.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) — the FW-26/27/28/41 anchor, re-verified in-task |
| Disassembler | `gpsimd_tools/tools/XtensaTools/bin/xtensa-elf-objdump` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| MARIANA `NX_DVE` DEBUG IRAM | host VA `0x408fc0`, size `0x1c560` (`.data` VA == device file offset) |
| MARIANA `NX_DVE` DEBUG DRAM | host VA `0x425520`, size `0x7000` |
| MARIANA `Q7_POOL` DEBUG IRAM/DRAM | sha256 `47f76629…` / `02cacff0…` (reproduces FW-28 EXACTLY — the RNG **producer** images) |

> **NOTE (carve anchor for this page).** This page's DVE carve was re-derived in-task by
> `dd`-slicing the container at the host-symtab VAs and disassembling the binary slice.
> The MARIANA `NX_DVE` DEBUG IRAM slice disassembles to **46,162 lines, empty stderr**.
> The task's stated DEBUG-image anchors (`CAYMAN_NX_POOL_DEBUG` IRAM 116768 B sha
> `8e4412b9` / DRAM 28448 B sha `7bdf6ed7`) belong to the **CAYMAN POOL** producer side;
> the Rand2 consumer lives in the **DVE** image, which is where every Rand2 byte below was
> read. [carve methodology HIGH/OBSERVED]

The authoritative struct/enum source is the shipped public ISA headers (same package):

- `…/neuron_mariana_arch_isa/tpb/aws_neuron_isa_tpb_d3_rand.h` — **Rand2** (NC-v4)
- `…/neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_d3_rand.h` — **Rand2** (NC-v5; sole diff = the channel-range gate, §8)
- `…/aws_neuron_isa_tpb_d4_rand.h` — the predecessor **Rand** + `RandGetState`
- `…/aws_neuron_isa_tpb_s1_rand.h` — **RandSetState**
- `…/aws_neuron_isa_tpb_common.h` — opcodes `0x76/0x77/0x78/0xe2/0x30`; the
  `RAND_ALGORITHM` / `RAND_POST_PROC` / `IMM_SRC` enums; `TENSOR3D`;
  `DVE_NUM_CHANNELS = POOLING_NUM_CHANNELS = 128`

> **QUIRK.** `cayman/` and `sunda/` arch-isa dirs ship `d4_rand.h` and `s1_rand.h` but
> **NOT** `d3_rand.h` (verified by directory listing). The header tree alone tells you
> Rand2 is a MARIANA-and-later op before you read a single firmware byte. [HIGH/OBSERVED]

---

## 3. The opcode ladder — Rand vs Rand2 vs RandGet/SetState

Read verbatim from `aws_neuron_isa_tpb_common.h` (mariana; **identical in maverick** —
opcode lines `217/247/308` there):

```c
NEURON_ISA_TPB_OPCODE_RAND            = 0x76,  // n, ucode exists, not maintained/used
NEURON_ISA_TPB_OPCODE_RAND_GET_STATE  = 0x77,  // Y
NEURON_ISA_TPB_OPCODE_RAND_SET_STATE  = 0x78,  // Y
NEURON_ISA_TPB_OPCODE_EXPONENTIAL     = 0x30,  // (Rand2's DVE sibling distribution op)
NEURON_ISA_TPB_OPCODE_RAND2           = 0xe2,  // Y
```

- `Rand` (`0x76`) is the **deprecated** legacy op — its own header (`d4_rand.h:18`)
  literally opens `// TODO: deprecate Rand`. [HIGH/OBSERVED]
- `Rand2` (`0xe2`) is the **maintained** replacement (the user-facing op). [HIGH/OBSERVED]
- The two seed-state ops `0x77`/`0x78` are shared (see [`rng-seed-state-ops.md`](./rng-seed-state-ops.md)). [HIGH/OBSERVED]

> **CORRECTION (vs a naïve reading).** Rand2 ≠ "Rand with a bigger version number on the
> same engine." Rand2 is a **different struct on a different engine with a strictly
> smaller capability surface**. The version bump encodes a deliberate
> simplification/restriction, not a feature superset. See §6.

---

## 4. The `d3_rand` operand struct — byte-exact, compile-verified

`NEURON_ISA_TPB_D3_RAND_STRUCT` (`aws_neuron_isa_tpb_d3_rand.h`), `ISA_STATIC_ASSERT(sizeof == 64)`:

| off | size | field | type | constraint |
|----:|----:|-------|------|-----------|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `header.opcode == Rand2 (0xe2)` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | — |
| 12 | 1 | `rand_algorithm` | `RAND_ALGORITHM` | **must == XORWOW (3)** |
| 13 | 1 | `reserved0[1]` | `uint8_t` | must be 0 |
| 14 | 1 | `post_process` | `RAND_POST_PROC` | **must == UNIFORM_IN_RANGE (1)** |
| 15 | 17 | `reserved1[17]` | `uint8_t` | must be 0 |
| 32 | 1 | `dtype` | `DTYPE` | **FP dtype only (FP32R allowed)** |
| 33 | 1 | `reserved2[1]` | `uint8_t` | must be 0 |
| 34 | 1 | `num_active_channels` | `uint8_t` | `1..128` |
| 35 | 3 | `reserved3[3]` | `uint8_t` | must be 0 |
| 38 | 1 | `min_src` | `IMM_SRC` | where `min` comes from |
| 39 | 1 | `max_src` | `IMM_SRC` | where `max` comes from |
| 40 | 4 | `min` | `IMM_VAL_INST_FIELD` | fp32 imm / ptr / reg |
| 44 | 4 | `max` | `IMM_VAL_INST_FIELD` | fp32 imm / ptr / reg |
| 48 | 16 | `dst_mem_pattern` | `TENSOR3D` | ADDR4 + 3×int16 step + 3×uint16 num_elem |

**Compile verification** (a self-contained harness mirroring the header field-for-field,
`gcc -O0`, this task):

```
sizeof = 64
offsets: rand_algorithm=12 post_process=14 dtype=32 num_active_channels=34
         min_src=38 max_src=39 min=40 max=44 dst_mem_pattern=48
```

Every offset matches the header's own `// (NN)` column. [HIGH/OBSERVED — header text +
`ISA_STATIC_ASSERT==64` + independent `gcc` `sizeof`/`offsetof`]

### 4a. The `IMM_SRC` enum (min_src / max_src semantics)

```c
NEURON_ISA_TPB_IMM_SRC_INSTRUCTION_IMMEDIATE = 0,  // fp32 value in the instruction
NEURON_ISA_TPB_IMM_SRC_POINTER_IMMEDIATE     = 1,  // PartitionOffset ptr to the fp32 immediate
NEURON_ISA_TPB_IMM_SRC_REG_PTR_IMMEDIATE     = 2,  // PartitionOffset ptr from a register
```

So each bound is one of: **inline fp32** (src==0, value lives at off 40/44), a **memory
pointer** (==1), or a **register-supplied pointer** (==2). This per-bound flexibility is
a Rand2 addition (§6). [HIGH/OBSERVED `common.h:1352`]

> **GOTCHA.** `common.h` *also* defines a SECOND, **distinct** enum
> `NEURON_ISA_TPB_IMM_SRC_N` (`:1375`) with the **0/1 swapped**
> (`N_POINTER_IMMEDIATE=0`, `N_INSTRUCTION_IMMEDIATE=1`). `d3_rand`'s `min_src`/`max_src`
> are typed `NEURON_ISA_TPB_IMM_SRC` — the **non-`_N`** variant — so for Rand2 the
> mapping is `0=instruction, 1=pointer, 2=reg`. A reimplementer copying the `_N` table
> by mistake will invert min/max bound sourcing. [HIGH/OBSERVED — both enums read]

### 4b. The `min`/`max` value field

`NEURON_ISA_TPB_IMM_VAL_INST_FIELD` is a 4-byte union overlaying:

```c
union {
  NEURON_ISA_TPB_PARTITION_OFFSET imm_ptr;          // when *_src selects a pointer
  NEURON_ISA_TPB_IMM_REG          imm_reg;
  float                           imm_arith_fp32;    // the fp32 bound (the UniformInRange view)
  int32_t                         imm_bitvec_int32;
  uint32_t                        imm_bitvec_uint32;
  uint16_t                        imm_bitvec_uint16[2];
};
```

For Rand2/UniformInRange the relevant view is `imm_arith_fp32` (when `*_src==0`) or
`imm_ptr` (when `*_src∈{1,2}`). [HIGH/OBSERVED `common.h:959`]

### 4c. `TENSOR3D` (dst_mem_pattern)

```c
typedef struct {
  NEURON_ISA_TPB_ADDR4 start_addr;   // 4
  int16_t              step_elem[3]; // 6
  uint16_t             num_elem[3];  // 6
} NEURON_ISA_TPB_TENSOR3D;           // = 16
```

The handler's element count = **product of the three `num_elem` dims** (decoded at IRAM
`0xf67d..0xf689`, §5b). [HIGH/OBSERVED `common.h:682`]

---

## 5. The DVE thin-consumer handler — instruction-exact (MARIANA `0xf2cc`)

### 5a. Dispatch chain: opcode → DVE kernel_info → funcVA → body

Three **consecutive** registration stubs in MARIANA DVE IRAM register the modern random
family (the FW-70 dispatch template). Decoded bytes:

```
230e:  24e0eb   const16  a2, 0xebe0   ; -> handler 0xebe0  ("S: RandSetState")
232a:  24ccf2   const16  a2, 0xf2cc   ; -> handler 0xf2cc  ("S: Rand2")        *** Rand2 ***
2346:  2498f6   const16  a2, 0xf698   ; -> handler 0xf698  ("S: Exponential")
```

Each stub is `entry a1,48 ; const16 a2,<funcVA> ; s32i a2,[a1+12] ; l32r a11,<descr> ;
l32r a2,<descr> ; call8 0x9920`. `call8 0x9920` = the kernel-register routine; `[a1+12]`
= the `kernel_info` funcVA slot. [stub edges HIGH/OBSERVED]

The handler→name pinning (the decisive identity proof): handler `0xf2cc` loads
`const16 a10,0x3068` at `0xf650`, and DRAM `@0x3068` reads (xxd-verified this task):

```
00003068: 53 3a 20 52 61 6e 64 32 0a 00 53 3a 20 45 78 70   S: Rand2..S: Exp
00003072: 53 3a 20 45 78 70 6f 6e 65 6e 74 69 61 6c 0a 00   S: Exponential..
```

So the canonical chain is:

```
[SEQ: Rand2 opcode 0xe2] -> [DVE kernel_info slot] -> funcVA 0xf2cc -> body "S: Rand2"
```

> **NOTE (out-of-carve edge).** The stub's `l32r` descriptor literals (printed
> `0xfffc849c` / `0xfffc3a1c`) resolve to negative PC-relative offsets **outside** the
> carved IRAM window — the opcode→descriptor binding is a **runtime-bound table entry**,
> not statically pinned in this carve (the identical limitation FW-41 flagged for the
> Dropout opcode). The opcode value (`0xe2`) is HIGH from `common.h`; the
> `kernel_info → funcVA` edge is the FW-70 template; the **funcVA → body** is
> HIGH/OBSERVED. [chain: opcode HIGH; descriptor MED/out-of-carve; funcVA→body HIGH]

### 5b. Handler body — entry, self-name, element-count, return

```
f2cc:  36a100   entry  a1, 80               ; SMALL frame -> thin handler
f2cf:  movi a10,0 ; movi a11,0 ; movi.n a12,0 ; movi.n a13,0
f2d9:  2594fd   call8  0xcc1c               ; a 4-bool packer (extui-bit flags -> config word)
f2dc:  a0a074   extui  a10, a10, 0, 8
f2df:  e56dfa   call8  0x99bc               ; the shared DVE setup helper
f2e3:  l32r a11,<lit> ; call8 0x14e88 ; s16i a11,a4,176 ; ...  ; FLIX vector-context setup
...
f650:  a46830   const16 a10, 0x3068         ; "S: Rand2"
f653:  252509   call8  0x188a4              ; the DVE "S:" log printf
f660:  25ba05   call8  0x15204              ; -> the DVE worker hand-off (does the actual work)
f674:  l32r a0,<lit> ; s32i.n a3,a1,20 ; s32i.n a4,a1,16 ; s32i.n a5,a1,28  ; spill TENSOR3D dims
f67d:  2861     l32i.n a2, a1, 24
f67f:  3851     l32i.n a3, a1, 20
f681:  302282   mull   a2, a2, a3           ; dim_a * dim_b
f684:  3841     l32i.n a3, a1, 16
f686:  302282   mull   a2, a2, a3           ; * dim_c  = num_tensor_elements
f689:  2931     s32i.n a2, a1, 12           ; store total element count
f68e:  bc59     beqz.n a9, 0xf6c7
f694:  1df0     retw.n                      ; *** Rand2 handler RETURNS ***
```

C pseudocode for the handler (the byte-pinned skeleton; the FLIX vector-context block is
elided as MED — see GOTCHA):

```c
// MARIANA NX_DVE handler @ 0xf2cc, frame=80. Thin randomness CONSUMER.
static void dve_rand2_handler(dve_ctx_t *ctx /* a1 frame */)
{
    int f0 = 0, f1 = 0, f2 = 0, f3 = 0;
    uint32_t cfg = pack4_bool_flags(&f0, &f1, &f2, &f3); // call8 0xcc1c
    cfg &= 0xff;                                          // extui a10,a10,0,8
    dve_setup(ctx, cfg);                                 // call8 0x99bc (shared by every DVE handler)

    program_vector_context(ctx);                         // FLIX/VLIW window setup (MED, not byte-pinned)

    dbg_log("S: Rand2");                                 // const16 0x3068 ; call8 0x188a4

    dve_worker_dispatch(ctx);                            // call8 0x15204  <-- the real generate+post-proc

    // num_tensor_elements = product of the 3 TENSOR3D num_elem dims
    uint32_t d0 = ctx->slot24, d1 = ctx->slot20, d2 = ctx->slot16;
    uint32_t n = d0 * d1;                                // mull @0xf681
    n = n * d2;                                          // mull @0xf686
    ctx->elem_count = n;                                 // s32i [a1+12]

    return;                                              // retw.n @0xf694
}
```

The handler spans `0xf2cc..0xf694` (~`0x3c8` bytes): **setup → vector-context program →
self-name → compute `num_tensor_elements` → hand off to the DVE worker → return**. It
carries **no multi-word RNG state machine**. [entry, the log, the `mull` element-count
chain, the `retw.n` boundary all HIGH/OBSERVED]

> **GOTCHA (FLIX desync).** The flat DEBUG image has no `.xt.prop` FLIX property table, so
> a linear `objdump` sweep desyncs the VLIW bundles in the vector-context block
> (`0xf2e6..0xf64d`) — the per-lane register-window programming. The entry, the `call8`
> edges, the self-name, the `mull` element-count chain, and the `retw.n` boundary are all
> single/short-format instructions that resync cleanly and **are** byte-pinned; the
> interior bundle operands are MED. This is the SX-FW-00 limitation shared by FW-26/27/41.

### 5c. Operand-field reads

A generic 4-byte-stride byte-reassembly routine at `0xfbf0..0xfcb0` walks the record
reading fields at offsets matching `d3_rand`. The byte-pinned reads:

```
fc4d:  420127   l8ui  a4, a1, 39    ; max_src
fc53:  520126   l8ui  a5, a1, 38    ; min_src
```

This routine is a **generic endian-pack**, not a Rand2-exclusive decode; the exact
Rand2 field→semantic dispatch is inside the FLIX-desync'd worker (`call8 0x15204`). The
struct offsets are header-authoritative (HIGH); the `+38`/`+39` reads OBSERVED; the
on-device `{algo==XORWOW, post_proc==UNIFORM}` gate is enforced **host-side in the ISA
assertion block** (header) and is not separately byte-pinned in this DVE DEBUG carve
(MED). [offsets HIGH/header; the `38/39` reads OBSERVED; on-device gate MED]

---

## 6. The XORWOW-only + UniformInRange-only contract (proven from header bytes)

This is the central correctness contract of Rand2. The `is_valid_d3_rand` predicate
(`aws_neuron_isa_tpb_d3_rand.h`, commented spec block) **conjuncts three restriction
functions** — read verbatim:

```c
// is_valid_d3_rand(i) =
//      ... && restrict_rand2_algorithm(i)
//          && has_valid_rand2_post_processing(i)
//          && has_valid_rand2_dtype(i) ...

// fn restrict_rand2_algorithm(i) -> bool {
//    (i.d3_rand.rand_algorithm == RandAlgorithm::XORWOW)        // <-- XORWOW ONLY
// }
// fn has_valid_rand2_post_processing(i) -> bool {
//    (i.d3_rand.post_process == RandPostProc::UniformInRange)   // <-- UNIFORM ONLY
// }
// fn has_valid_rand2_dtype(i) -> bool {
//    is_valid_dtype(i.d3_rand.dtype, DtypeAllowFP32R::True)     // <-- FP ONLY (FP32R ok)
// }
```

with the algorithm and post-proc enums (full domain, from `common.h`):

```c
RAND_ALGORITHM:  LFSR=0, PCG32=1, PHILOX=2, XORWOW=3
RAND_POST_PROC:  RAW_U32=0, UNIFORM_IN_RANGE=1, NORMAL=2  // "Not supported yet"
```

**What is rejected (proved by the equality predicates):**

| field | accepted | rejected |
|-------|----------|----------|
| `rand_algorithm` | `XORWOW (3)` only | `LFSR (0)`, `PCG32 (1)`, `PHILOX (2)` — all fail `restrict_rand2_algorithm` |
| `post_process` | `UNIFORM_IN_RANGE (1)` only | `RAW_U32 (0)`, `NORMAL (2)` — both fail `has_valid_rand2_post_processing` |
| `dtype` | FP (`FP32`, `FP32R`, `BF16`, `FP16`) | any integer dtype (`is_valid_dtype(.., FP32R::True)` admits FP only) |

[HIGH/OBSERVED — three equality predicates + the two enum domains]

> **QUIRK (the disabled LFSR alternative).** `d3_rand.h:106` carries a **commented-out**
> alternative inside the restriction block:
>
> ```c
> //    (i.d3_rand.rand_algorithm == RandAlgorithm::LFSR) ||
> ```
>
> i.e. an `(LFSR || XORWOW)` form was contemplated and then **disabled**, leaving the
> single live clause `rand_algorithm == XORWOW`. LFSR was deliberately excluded from
> Rand2. [HIGH/OBSERVED]

### 6a. The generate → post-process pipeline

Rand2 is a randomness **consumer** (the FW-41 Dropout model), proven three ways:

1. **No inline generator.** The MARIANA DVE IRAM contains **no Weyl constant `0x87c5`**
   (=362437) — the Xorwow signature FW-26/28 found on POOL. The single textual hit for
   `0x87c5` in the disasm is the **branch target** of `bltu a2, a3, 0x87c5` at `0x87bf`
   (a control-flow address, not a literal-pool constant). So the DVE does **not** inline
   the Xorwow generator. [HIGH/OBSERVED negative]
2. **Cross-engine staging exists.** The DVE carries a `"MEMSET/RNG"` handler
   (`"S: MEMSET/RNG"` @DRAM `0x200e`) and a `"push GENERATE to DMA[%d]"` path
   (DRAM `0x3896`) — the mechanism by which XORWOW uint32 draws produced by the POOL
   `Rng (XORWOW)` generator (FW-26/27/28; driver `0xbc78` `"XorwowRng(TIE)"`) are DMA'd to
   the DVE destination. [strings HIGH/OBSERVED]
3. **The state lives on a different element count.** The `d4_rand`
   `rand_get_state_legal_combinations` block (read verbatim) independently documents the
   per-engine XORWOW state size: `Pool+XORWOW → elem_cnt 6`, `Pool+PHILOX → 7`,
   **`DVE+XORWOW → elem_cnt 24`**, `DVE+LFSR → 4`. The DVE's 128-lane XORWOW state is
   materialized as a **24-element tensor** (the per-lane 6-word state distributed across
   the DVE datapath). This is an *independent* header attestation that XORWOW on DVE is a
   first-class, supported combination. [HIGH/OBSERVED header]

The post-process (`UNIFORM_IN_RANGE`) maps the uint32 draw `u` to fp32 in `(min, max)`:

```
out = min + u_norm * (max - min),   where u_norm = (fp32 fraction of the uint32 draw)
```

`min`/`max` are the fp32 bounds at off 40/44, sourced per `min_src`/`max_src`. The DVE's
fp32 datapath supplies the building blocks — a `uint32 → fp32` convert
(`ivp_ufloatn_2x32`-class), an fp32 multiply (`ivp_muln_2xf32t`-class) and a multiply-add
to offset by `min`.

> **CORRECTION (honesty).** The mnemonics `ivp_ufloatn_2x32` / `ivp_muln_2xf32t` /
> `ivp_mulsonen_2xf32t` are **not** present as ASCII strings in this container (verified:
> zero hits). They name the **op vocabulary** the DVE fp32 datapath exposes for the affine
> map; the exact bundle order/operands/constants are **not byte-pinnable** here (the FLIX
> desync of §5b). Treat the affine-map decomposition as **INFERRED** (FW-41 model +
> standard uniform-in-range math), **not** as an observed instruction trace. The
> `0x3F800000` (=`1.0f`) / `0x7FFFFF` mantissa-fill seam FW-26/27/28 flagged is the
> `uint32 → fp32 [0,1)` normalization; the subsequent `*(max-min)+min` is the affine range
> map. [op vocabulary direction HIGH; the exact MAD bundle MED/INFERRED]

### 6b. RAW_U32 and NORMAL are unreachable from Rand2

- **RAW_U32** (`post_process==0`) — the FW-41 Dropout / old-Rand uint32 pass-through — is
  **rejected** by `has_valid_rand2_post_processing` (UNIFORM only). Rand2 never emits raw
  u32. [HIGH/OBSERVED]
- **NORMAL** (`post_process==2`) is enum-defined but `// Not supported yet` across
  **all** of Rand/Rand2. There is **no** Box-Muller / inverse-CDF / gaussian math in any
  POOL or DVE image (no `log`/`sqrt`/`2π` constants tied to RNG). A normal distribution
  must be composed host-side from UniformInRange draws. The Rand2 ecosystem's only
  non-uniform **on-device** distribution is the **separate** Exponential op (`0x30`), not a
  Rand2 post-proc. [HIGH/OBSERVED — enum value + absence of gaussian math]

---

## 7. Rand vs Rand2 — the complete byte-grounded difference

| Attribute | **Rand** (`d4_rand`, `0x76`) | **Rand2** (`d3_rand`, `0xe2`) |
|-----------|------------------------------|-------------------------------|
| status | `// n, ... not maintained/used` (deprecated; header: `// TODO: deprecate Rand`) | `// Y` maintained — the user op |
| engine | POOL (general HW Rand + the POOL gpsimd `Rng` SW path) | **DVE** (handler `0xf2cc`, `"S: Rand2"`) |
| algorithms | `restrict_rand_algorithm` selectable; POOL wires `{LFSR, XORWOW}` (FW-28) | **XORWOW ONLY** (`restrict_rand2_algorithm`) |
| post_process | `RawU32` OR `UniformInRange` | **UniformInRange ONLY** |
| `rand_num_steps` | yes (off 13; the LFSR multi-step knob) | **DROPPED** |
| `rand_generator` (`RAND_SRC`) | yes (off 15) | **DROPPED** (fixed XORWOW source) |
| range bounds | `params[0],[1]` (off 16; always inst-imm) | `min`/`max` (off 40/44) **with** `min_src`/`max_src` IMM_SRC (inst-imm / ptr / reg) |
| dst pattern | `TENSOR4D` (20 B, off 44) | `TENSOR3D` (16 B, off 48) |
| dtype | int (RawU32) OR fp (Uniform) | **FP only** (`DtypeAllowFP32R`) |
| struct size | 64 B (`d4_rand`) | 64 B (`d3_rand`) |
| on-device handler | POOL Rng + RandGet/SetState (FW-28) | DVE thin consumer (this page) |
| NORMAL | enum-defined, unsupported | enum-defined, unsupported |

**The deltas, byte-grounded** (Rand2 vs Rand):

- **DROPPED `rand_num_steps`** — Rand2 is XORWOW-only; `num_steps` was the LFSR-only
  multi-step knob (`d4_rand.h:32`: `// If LFSR, rand_num_steps >= 1`), irrelevant once
  LFSR is excluded. [HIGH]
- **DROPPED `rand_generator`** — Rand2 has a single fixed source (XORWOW); no per-instruction
  generator selector. [HIGH]
- **DROPPED `params[4]` (16 B)** — replaced by the explicit `min`/`max` + `min_src`/`max_src`
  quartet. Rand carried `min_fp32`/`max_fp32` in `params[0]`/`params[1]` (`d4_rand.h:42`);
  Rand2 promotes them to first-class fields. [HIGH]
- **ADDED `min_src`@38 / `max_src`@39** — the per-bound source selector Rand lacked (Rand's
  params were always instruction-immediate). [HIGH]
- **CHANGED dst** from `TENSOR4D` (4-D, Rand) to `TENSOR3D` (3-D, Rand2). [HIGH]

> **NOTE (the deprecated op is unwired).** There is **no** standalone `"S: Rand"` (without
> the `2`) handler string anywhere in the firmware (exhaustive grep: zero hits — the only
> hits are `"S: Rand2"`). The deprecated `Rand` (`0x76`) is **not** wired as a device
> kernel in this build — only `RandGet/SetState` (POOL, FW-28) and `Rand2` (DVE) are.
> [HIGH/OBSERVED negative]

---

## 8. Per-generation presence + dtype matrix

### 8a. Per-gen presence (two independent streams agree)

(A) ISA-header presence: `d3_rand.h` ships **only** for `mariana` (NC-v4) and `maverick`
(NC-v5). (B) the `"S: Rand2"` / `"S: Exponential"` DVE DEBUG strings per generation
(`"S: Rand2"` appears **exactly 3×** in the container — mariana, mariana_plus, maverick).

| GEN | `d3_rand.h`? | `"S: Rand2"` in NX_DVE DRAM | Rand2 wired? | Tag |
|-----|--------------|------------------------------|--------------|-----|
| SUNDA (V1) | NO | (no NX_DVE DEBUG image) | NO | HIGH/OBSERVED |
| CAYMAN (NC-v3) | NO | ABSENT (only MEMSET/RNG, RngClr) | NO | HIGH/OBSERVED |
| MARIANA (NC-v4) | YES | `@0x428588` (DVE str `0x3068`) | YES (handler `0xf2cc`) | HIGH/OBSERVED |
| MARIANA_PLUS | (mariana hdr) | `@0x6f02a8` | YES (corroborated) | HIGH/OBSERVED string; body INFERRED |
| MAVERICK (NC-v5) | YES | `@0x8b0688` (DVE str `0x30c8`) | YES (handler ~`0xfa44`) | header-OBSERVED → **interior INFERRED** |

> **MAVERICK (NC-v5) — header-OBSERVED → INFERRED.** The `"S: Rand2"` string and the
> handler string-load (`@0xfa44`) are observed in the MAVERICK DVE image, and the
> MAVERICK `d3_rand.h` is read directly. The MAVERICK handler **body** was not fully
> disassembled; it is assumed identical-family by the byte-stable MARIANA decode + the
> matching maverick string-load. **The MAVERICK interior is INFERRED, not byte-pinned.**

**The MAVERICK delta.** The *only* `d3_rand.h` diff vs mariana is the channel-range gate:
mariana uses `has_valid_active_channel_range(num_active_channels, POOLING_NUM_CHANNELS)`;
maverick uses
`has_valid_active_channel_range_with_tile(num_active_channels, DVE_NUM_CHANNELS, header.inst_flags)`
— NC-v5 adds **tile-aware channel ranging**. Both channel counts = 128
(`POOLING_NUM_CHANNELS == DVE_NUM_CHANNELS == 128U`). The struct + the XORWOW/UNIFORM
restriction are **byte-identical** (verified by `diff` of the two headers). [HIGH/OBSERVED — header diff]

### 8b. The dtype matrix

`has_valid_rand2_dtype = is_valid_dtype(dtype, DtypeAllowFP32R::True)` — the dtype must be
a **floating-point** dtype (FP32R allowed). FP dtype codes (from `common.h`):

| dtype | code | Rand2 accepts? |
|-------|-----:|:--------------:|
| `FP32` | `0xA` | YES |
| `FP32R` | `0xB` | YES (the `DtypeAllowFP32R::True` flag) |
| `BFLOAT16` / `BF16` | `0x6` | YES |
| `FP16` | `0x7` | YES |
| `UINT32` | `0x9` | **NO** (integer — would only be valid under RawU32, which Rand2 forbids) |
| any other integer dtype | — | **NO** |

Contrast: the **old Rand** allowed int dtypes (RawU32) OR fp dtypes (UniformInRange)
(`d4_rand.h:45`). `RandGetState` forces `dtype == UINT32` (the state read-back). [HIGH/OBSERVED]

---

## 9. RNG subsystem roll-up (FW-26 / 27 / 28 / 41 / 48) — CAPPED

The GPSIMD RNG subsystem is a **centralized-producer / thin-consumer** stack. A single
`rand_algo` enum `{LFSR=0, PCG32=1, PHILOX=2, XORWOW=3}` and a `RAND_POST_PROC`
`{RAW_U32=0, UNIFORM_IN_RANGE=1, NORMAL=2(unsupported)}` span the whole stack.

- **PRODUCER** — the POOL/Q7 `Rng` software generator: the Xorwow SW PRNG
  ([`rng-xorwow-sw.md`](./rng-xorwow-sw.md): 6-word per-lane state + Weyl `+362437`, uint32
  draw) that became the `(TIE)` build-variant on MARIANA+
  ([`rng-xorwow-tie.md`](./rng-xorwow-tie.md)) and forks Xorwow-vs-LFSR at the shared
  `SetSeeds 0xb6dc` ([`rng-lfsr-dispatch.md`](./rng-lfsr-dispatch.md)) with the shared
  advance driver `0xbf74 → 0xbed8 → 0xbc78` `"XorwowRng (TIE)"`. POOL wires exactly
  `LFSR(0) + XORWOW(3)`; PCG32/PHILOX are ISA-defined-but-unsupported. Seed state:
  [`rng-seed-state-ops.md`](./rng-seed-state-ops.md).
- **CONSUMERS** — thin DVE handlers carrying no inline RNG, applying a post-proc to staged
  uint32 draws: **Dropout** ([`dropout.md`](./dropout.md): `uint32 → ufloat → float`,
  compare<threshold, select, scale-by-`1/(1-p)`) and — **capping the stack** — **Rand2**
  (this page: opcode `0xe2`, DVE handler `0xf2cc` `"S: Rand2"`, `d3_rand` 64-B struct,
  XORWOW-only + UniformInRange-only, fp32 `min`/`max` via flexible IMM_SRC bounds,
  `uint32 → ufloat → min+u*(max-min) → fp32 TENSOR3D output`), which replaces the
  deprecated **Rand** (`0x76`, `d4_rand`, POOL, the selectable-algo legacy op).

NORMAL is enum-defined but **UNIMPLEMENTED everywhere** (no Box-Muller / inverse-CDF); the
only non-uniform on-device distribution is the **separate** Exponential op (`0x30`, Rand2's
DVE sibling). Per-gen: SUNDA none / CAYMAN Xorwow(SW) POOL only, no Rand2 /
MARIANA+MARIANA_PLUS+MAVERICK `LFSR+XORWOW(TIE)` on POOL **plus** Rand2+Exponential on DVE
(MAVERICK adds NC-v5 tile-aware channel ranging, no new algorithm). **The RNG subsystem is
CLOSED.**

---

## 10. Honesty ledger

**HIGH / OBSERVED** — Opcodes (`RAND2=0xe2`, `RAND=0x76`, `RANDGET=0x77`, `RANDSET=0x78`,
`EXPONENTIAL=0x30`, verbatim, mariana+maverick parity). The `d3_rand` 64-B struct layout
+ `ISA_STATIC_ASSERT==64` + independent `gcc` `sizeof`/`offsetof`. The three restriction
predicates (`restrict_rand2_algorithm==XORWOW`, `has_valid_rand2_post_processing==UNIFORM`,
`has_valid_rand2_dtype==FP/FP32R`) + the disabled-LFSR comment. `IMM_SRC`
`{INST=0,PTR=1,REG=2}` (and the distinct `_N` variant). Rand2 RUNS ON DVE (`"S: Rand2"` in
MARIANA/MARIANA_PLUS/MAVERICK NX_DVE DRAM, ABSENT on CAYMAN/SUNDA; container hit-count 3).
Registration stub trio `0x230e/0x232a/0x2346` → handlers `0xebe0/0xf2cc/0xf698`; DRAM
`@0x3068` = `"S: Rand2\n"` (xxd). Handler entry `0xf2cc` (`entry a1,80`); the `num_tensor_elements`
= product of the 3 TENSOR3D dims (`mull`×2 @`0xf681`/`0xf686`); `retw.n` @`0xf694`. **No**
Weyl `0x87c5` literal in DVE IRAM (the one hit is a branch target). The DVE `MEMSET/RNG`
handler + `"push GENERATE to DMA"`. The `d4_rand` DVE XORWOW state `elem_cnt 24`. Per-gen
presence + the maverick channel-range diff. Container sha256 `b7c67e89…`; MARIANA Q7_POOL
producer images reproduce FW-28. No standalone `"S: Rand"` handler.

**MED / INFERRED** — The exact UniformInRange affine-map bundle (`out = min + u_norm*(max-min)`):
op-vocabulary direction HIGH, the precise FLIX-bundle order/operands/constants MED (no
`.xt.prop` table). **The `ivp_*` mnemonics are NOT ASCII strings here** — the affine-map
decomposition is INFERRED from the FW-41 model, not an observed instruction trace. The
`kernel_info`/decode-table descriptor binding opcode `0xe2 → funcVA 0xf2cc` (the stub edge
is HIGH but the `l32r` literals resolve out-of-carve / runtime-bound). The exact
cross-engine staging hop (POOL XORWOW draw → DMA → DVE dst). The on-device enforcement of
the XORWOW/UNIFORM gate (header is authoritative HIGH; a separate byte-pinned device-side
validation was not isolated). The MAVERICK handler body (string-confirmed; body INFERRED
identical-family).

**LOW / UNRECOVERED** — The exact opcode→funcVA descriptor byte (runtime-bound, out-of-carve).
The MARIANA_PLUS / MAVERICK Rand2 handler bodies beyond string-confirmed presence. The
Exponential op's internal math (separate op, out of scope).
