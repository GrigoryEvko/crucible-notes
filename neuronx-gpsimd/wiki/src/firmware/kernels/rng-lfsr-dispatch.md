# RNG — LFSR + `rand_algo` Dispatch Tree

This page documents the **second** random generator the GPSIMD POOL engine carries — a
**32-bit linear-feedback shift register (LFSR)** — and the `rand_algo` **dispatch tree**
that forks between it and Xorwow. The LFSR is *not* a separate generator function and *not*
a dedicated opcode: it shares the per-draw advance driver with the Xorwow path and differs
**only** in seed initialization. The fork is a single `bbci` instruction in the *shared*
`SetSeeds` body, driven by a `saltu(rand_algo, 1)` comparison that resolves the four-value
ISA enum down to the two algorithms POOL actually wires. This page decodes that fork
instruction-exact, reconciles it against the shipped host ISA headers, proves which silicon
generations carry the LFSR, and **closes the RNG subsystem** with a five-way map tying the
software-Xorwow, TIE-Xorwow, LFSR, Dropout, and Rand2 pieces together.

This is the **Cadence Vision-Q7 GPSIMD compute core's** own firmware — windowed-ABI Xtensa
code on the `ncore2gp` (Cairo) core — and the companion **NX/SEQ sequencer** firmware that
validates the operand before it reaches the compute core. Every device fact below is
byte-pinned to a carve re-derived this session from `libnrtucode_internal.so`; every
host-ISA fact is read out of the public `aws_neuron_isa_tpb_*.h` headers shipped in the same
customop-lib package. Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

> **NOTE — what was carved this session, and the exact objects used.** The firmware
> container is
> `…/custom_op/c10/lib/libnrtucode_internal.so`
> (`10,276,288 B`, sha256 `b7c67e898a116454…` — the FW-26/FW-27/FW-41 anchor, re-verified
> in-task). The per-generation POOL images are `.rodata`-resident `_get.data` symbols
> (so **file offset == symbol VA**, `.rodata` `Address==Off==0x46b0`; no `.data` delta
> applies). Carved with `dd`, disassembled with the native `xtensa-elf-objdump`
> (`XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa Tools 14.09) shipped inside
> the gpsimd-tools package.
>
> | carved object | file off / size | sha256 (first 8) | role |
> |---|---|---|---|
> | `MARIANA_Q7_POOL_DEBUG_IRAM` | `0x4e2440` / `0x1ed40` | `47f76629` | the fork + bodies |
> | `MARIANA_Q7_POOL_DEBUG_DRAM` | `0x501180` / `0x15d80` | `02cacff0` | the log strings |
> | `MARIANA_PLUS_Q7_POOL_DEBUG_IRAM` | `0x7adf40` / `0x1ef00` | `1c9c15bc` | byte-match proof |
> | `MARIANA_NX_POOL_DEBUG_IRAM` | `0x44b540` / `0x1c080` | `41b6c798` | SEQ enum gate |
> | `MARIANA_NX_POOL_DEBUG_DRAM` | `0x4675c0` / `0x7000` | — | SEQ error strings |
> | `CAYMAN_Q7_POOL_DEBUG_IRAM` | `0x249020` / `0x1ea40` | `513a8a22` | per-gen anchor |
> | `CAYMAN_NX_POOL_DEBUG_IRAM` | `0x1b1420` / `0x1c820` | — | per-gen anchor |
> | `SUNDA_Q7_POOL_RELEASE_DRAM` | `0x4cec0` / `0xa540` | — | per-gen negative |
>
> The MARIANA IRAM sha `47f76629` and DRAM sha `02cacff0` reproduce FW-27 exactly; the
> CAYMAN IRAM sha `513a8a22` reproduces the FW-00 anchor. `[HIGH/OBSERVED]`

---

## 1. The headline

1. **The full `rand_algo` enum is recovered verbatim** from the shipped public ISA headers and
   is airtight-consistent with the disassembled fork:
   `NEURON_ISA_TPB_RAND_ALGORITHM { LFSR=0, PCG32=1, PHILOX=2, XORWOW=3 }`. On the POOL/Q7
   software RNG, exactly **two** of the four are wired: `LFSR(0)` and `XORWOW(3)`. `PCG32(1)`
   and `PHILOX(2)` are ISA-defined but hit the SEQ
   *"rand_algorithm(0x%x) not currently supported on POOL"* arm. `[HIGH/OBSERVED]`
2. **The LFSR is a single-`u32`-word-per-lane generator** (vs Xorwow's 5–6 words). The
   `d4_rand.h` header states it verbatim, and the Q7 `LfsrSetSeeds` body's structural
   minimalism — frame **192**, one FLIX bundle, no `memset`, no multi-word seed chain, no
   Weyl const — confirms the one-word state. `[HIGH/OBSERVED header + structure]`
3. **There is no separate `LfsrRng` function and no dedicated LFSR opcode.** The per-draw
   advance is **shared** with the Xorwow(TIE) driver `0xbc78`; only the *seed init* differs
   (`LfsrSetSeeds 0xb700` vs `XorwowSetSeeds(TIE) 0xb744`). The fork lives only in the shared
   `Set`/`GetSeeds` bodies. `[HIGH/OBSERVED]`
4. **The LFSR feedback polynomial / tap form is not byte-recoverable.** No classical tap-mask
   constant appears anywhere in the Q7 POOL IRAM/DRAM, and the state-write lives in a
   FLIX-desync'd bundle. The recurrence is a 32-bit single-word xorshift-style LFSR over
   GF(2); the exact taps and Galois-vs-Fibonacci direction are **unrecovered**.
   `[width=32 HIGH; taps LOW/UNRECOVERED]`
5. **The output contract is the `RAND_POST_PROC` enum:** `RAW_U32(0)` / `UNIFORM_IN_RANGE(1)` /
   `NORMAL(2, unsupported)`. `RAW_U32` is the `uint32` Dropout/Rand2 consume; `UNIFORM_IN_RANGE`
   is the `0x3F800000` float seam. `[HIGH/OBSERVED]`
6. **Dropout's inline RNG is the LFSR:** `s3d3_dropout.h` says *"generates a u32 LFSR for each
   lane for each element; converts it to an f32 in range (0.0 to 1.0); compares to threshold"*.
   This pins the [Dropout](dropout.md) consumer to the LFSR arm. `[HIGH/OBSERVED]`

---

## 2. The `rand_algo` enum — recovered from the shipped ISA headers

The authoritative source is `aws_neuron_isa_tpb_common.h`, present per-arch under
`…/c10/include/neuron_<arch>_arch_isa/tpb/`. The `NEURON_ISA_TPB_RAND_ALGORITHM` enum is
**byte-identical across cayman/mariana/maverick**; SUNDA's is **truncated** (no `XORWOW`):

```c
/* aws_neuron_isa_tpb_common.h  (mariana: line 987; cayman: 894; maverick: 1142) */
typedef enum NEURON_ISA_TPB_RAND_ALGORITHM {
    NEURON_ISA_TPB_RAND_ALGORITHM_LFSR   = 0,
    NEURON_ISA_TPB_RAND_ALGORITHM_PCG32  = 1,  // PCG-XSH-RR, 64-bit state, 32-bit output
    NEURON_ISA_TPB_RAND_ALGORITHM_PHILOX = 2,  // Philox4x32, 10 round
    NEURON_ISA_TPB_RAND_ALGORITHM_XORWOW = 3,  // XORWOW implementation     <-- ABSENT on SUNDA
} NEURON_ISA_PACKED NEURON_ISA_TPB_RAND_ALGORITHM;
```

`[HIGH/OBSERVED — all four arch headers re-grepped: SUNDA stops at PHILOX=2; the other three
carry XORWOW=3.]`

Two companion enums fix the **output** and the **state source**:

```c
typedef enum NEURON_ISA_TPB_RAND_POST_PROC {     /* mariana common.h:994 */
    NEURON_ISA_TPB_RAND_POST_PROC_RAW_U32          = 0,  // raw u32 in [0, 2^32-1]
    NEURON_ISA_TPB_RAND_POST_PROC_UNIFORM_IN_RANGE = 1,  // fp32 in (min_fp32, max_fp32)
    NEURON_ISA_TPB_RAND_POST_PROC_NORMAL           = 2,  // Not supported yet
} NEURON_ISA_PACKED NEURON_ISA_TPB_RAND_POST_PROC;

typedef enum NEURON_ISA_TPB_RAND_SRC {           /* mariana common.h:1001 */
    NEURON_ISA_TPB_RAND_SRC_RNG_LFSR        = 0,
    NEURON_ISA_TPB_RAND_SRC_RNG_XORWOW      = 1,
    NEURON_ISA_TPB_RAND_SRC_RNG_PHILOX      = 2,
    NEURON_ISA_TPB_RAND_SRC_OUTPUT_CVT_LFSR = 3,
} NEURON_ISA_PACKED NEURON_ISA_TPB_RAND_SRC;
```

`[HIGH/OBSERVED]` Note the asymmetry between the two enums: `RAND_ALGORITHM` and `RAND_SRC`
are **different namespaces** with different integer encodings — `XORWOW` is `3` as an
algorithm but `1` as a source (`RNG_XORWOW`), and `LFSR` is `0` as both an algorithm *and* a
source (`RNG_LFSR`), with a fourth source `OUTPUT_CVT_LFSR(3)` being a conversion variant that
also binds to `RandAlgorithm::LFSR`. So **the LFSR has two source modes** (`RNG_LFSR` and
`OUTPUT_CVT_LFSR`); the device dispatcher only ever reads `RAND_ALGORITHM`, never `RAND_SRC`,
when it forks (§4).

> **GOTCHA — do not conflate `rand_algo` with `rand_generator`.** A reimplementer wiring the
> operand must keep the two fields independent: `rand_algorithm` (offset 12) selects the
> *recurrence* and is what the device branches on; `rand_generator`/`RandSrc` (offset 15 in
> `D4_RAND`, offset 24 in `S1_RAND`) selects the *state-storage mode* and is validated for
> mutual consistency but never used to pick the code path. The `has_valid_*` constraint blocks
> in `d4_rand.h` enumerate the *legal pairings* — e.g. `(RandSrc::RNG_XORWOW, RandAlgorithm::
> XORWOW)`, `(RandSrc::RNG_LFSR, RandAlgorithm::LFSR)`, `(RandSrc::OUTPUT_CVT_LFSR,
> RandAlgorithm::LFSR)` — and reject mismatches at decode. `[HIGH/OBSERVED]`

### 2.1 The "general Rand" vs the "POOL Rng" distinction

`d4_rand.h` documents the **general TPB hardware Rand instruction** (`// note, different from
RNG instruction`), whose own comment block names *"Three allowed values: LFSR (disabled in ISA
as of Oct 7, 2022 due to ucode issues), PCG32 (disabled …), PHILOX"* — i.e. the *general HW
Rand op* effectively does only PHILOX. That is a **different** instruction from the POOL
GPSIMD software RNG documented here. The POOL `Rng` handler (the `'S: Rng …'` SEQ front-end,
opcode `RNG`, see [Rand2](rand2.md)) is a *software* generator running on the POOL compute
engine, and it implements **LFSR + XORWOW**. The `XORWOW=3` enum value was *added at cayman*
(absent on SUNDA) specifically for this POOL software RNG. `[HIGH/OBSERVED — header text +
the per-gen enum diff in §6]`

---

## 3. The operand structs — field offsets cross-checked against the device

The dispatcher's field reads (§4) are a decisive cross-check: every byte offset the device
loads matches a named struct field. The three relevant structs (all `64 B`, `ISA_STATIC_ASSERT`-pinned):

```c
/* NEURON_ISA_TPB_D4_RAND_STRUCT  (the Rand op + RandGetState) — d4_rand.h:95 */
struct {
    HEADER         header;              /* +0   ( 0- 3) */
    EVENTS         events;              /* +4   ( 4-11) */
    RAND_ALGORITHM rand_algorithm;      /* +12          <-- the fork field         */
    uint8_t        rand_num_steps;      /* +13          <-- LFSR multi-step count   */
    RAND_POST_PROC post_process;        /* +14          <-- RAW_U32/UNIFORM/NORMAL  */
    RAND_SRC       rand_generator;      /* +15                                      */
    RAND_PARAM     params[4];           /* +16  (16-31)  min_fp32/max_fp32 for UNIFORM */
    DTYPE          dtype;               /* +32          */
    uint8_t        num_active_channels; /* +34          1..128                      */
    TENSOR4D       dst_mem_pattern;     /* +44  (44-63) */
};

/* NEURON_ISA_TPB_S1_RAND_STRUCT  (RandSetState) — s1_rand.h:26 */
struct {
    /* header+events at +0..+11 */
    RAND_ALGORITHM    rand_algorithm;   /* +12          <-- SEQ validates this {0,3} */
    uint8_t           num_active_channels; /* +13 */
    RAND_STATE_UPDATE update_state;     /* +14 */
    DTYPE             in_dtype;         /* +15 */
    TENSOR1D          src_mem_pattern;  /* +16  (16-23) */
    RAND_SRC          rand_generator;   /* +24 */
    uint32_t          imm_state[8];     /* +32  (32-63)  the 8x u32 seed/state operand */
};

/* NEURON_ISA_TPB_D3_RAND_STRUCT  (backs RAND2 *and* RNG) — d3_rand.h:19 */
struct {
    /* header+events at +0..+11 */
    RAND_ALGORITHM rand_algorithm;      /* +12          <-- Rand2/RNG fork on the same enum */
    /* +13 */
    RAND_POST_PROC post_process;        /* +14 */
    /* … 64 B total … */
};
```

The match is byte-exact: `rand_algorithm@12` is precisely the `[a1+12]` the shared `SetSeeds`
body stores/loads at `0xb6df`/`0xb6e2`, and `dst_mem_pattern@44` is the `[a4+45]` the driver
reads. The struct↔opcode binding is fixed by the shipped `instruction_mapping.json`
(`struct2opcode`): `D4_RAND_STRUCT → {RAND, RAND_GET_STATE}`, `S1_RAND_STRUCT →
{RAND_SET_STATE}`, `D3_RAND_STRUCT → {RAND2, RNG}`, `PSEUDO_SET_RNG_SEED_STRUCT →
{PSEUDO_SET_RNG_SEED}`. `[HIGH/OBSERVED — header offsets + JSON mapping re-read in-task]`

> **NOTE — Rand2 and the POOL `Rng` op share `D3_RAND_STRUCT`, hence share the `rand_algo`
> field.** The `D3_RAND` validation block references both `RandAlgorithm::XORWOW` and
> `RandAlgorithm::LFSR`, so a Rand2/Rng instruction selects its generator through the *same*
> `{LFSR, XORWOW}` mechanism documented here. The *device routing* of Rand2 specifically is
> [Rand2](rand2.md)'s scope and is not decoded on this page; the **struct-level** evidence
> that it reuses this enum is `[HIGH/OBSERVED]`.

---

## 4. The dispatch tree — instruction-exact

Two cores cooperate. The **SEQ/NX sequencer** validates the `rand_algo` enum *before* the
operand reaches the compute core (the `{0,3}` gate, §4.1). The **Q7 POOL compute core** then
decodes the operand, computes the algorithm-select bit, and forks at the *shared* `SetSeeds`
body (§4.2). The per-draw generator dispatcher does **not** fork on `rand_algo` (§4.3).

### 4.1 The SEQ enum gate — POOL accepts exactly `{0, 3}`

In `MARIANA_NX_POOL_DEBUG_IRAM`, the `RandSetState` validation (`0x301b…0x304a`) builds two
equality flags and admits the operand iff `rand_algo ∈ {0, 3}`:

```
301b:  l32i   a2, a1, 64          ; a2 = rand_algorithm
301e:  addi   a3, a2, -3
3021:  movi.n a2, 1
3023:  saltu  a3, a3, a2          ; flag1 = ((rand_algo-3) < 1) = (rand_algo == 3)
3026:  s8i    a3, a1, 60
3029:  l32i   a3, a1, 64
302c:  saltu  a3, a3, a2          ; flag0 = (rand_algo < 1)     = (rand_algo == 0)
302f:  s8i    a3, a1, 56
3032:  l8ui   a3, a1, 56
3035:  bbsi   a3, 0, 0x3041       ; if rand_algo == 0 -> SUPPORTED
303b:  l8ui   a2, a1, 60          ; (else) a2 = flag1 = (rand_algo == 3)
3041:  extui  a2, a2, 0, 1
3044:  s8i    a2, a1, 52
3047:  l8ui   a2, a1, 52
304a:  bbsi   a2, 0, 0x3065       ; if (rand_algo == 0 OR == 3) -> SUPPORTED (0x3065)
3056:  const16 a10, 0xe89         ; else: ERROR string @DRAM 0xe89
3059:  call8  0x183b0             ; log
```

DRAM `0xe89` is, byte-exact:
`"S: RandSetState : rand_algorithm(0x%x) not currently supported on POOL\n"`. The symmetric
`RandGetState` gate (same `{0,3}` structure) logs DRAM `0xe41`:
`"S: RandGetState : rand_algorithm(0x%x) not currently supported on POOL\n"`. So **POOL
admits exactly `rand_algo ∈ {0=LFSR, 3=XORWOW}`**; `{1=PCG32, 2=PHILOX}` fall to the
"not currently supported on POOL" arm — exactly the ISA enum's POOL subset.
`[HIGH/OBSERVED — disasm + both error strings read directly from NX DRAM]`

> **CORRECTION (vs an earlier draft).** The error strings read *"not **currently** supported
> on POOL"* (the word `currently` is present in both `0xe89` and `0xe41`). Any prose dropping
> `currently` is wrong; the bytes are decisive.

### 4.2 The Q7 dispatch + the algo-select bit

`RandSetState` enters at `0xb5f4`, logs the entry, decodes the operand, and tail-calls the
dispatcher `0xb640`:

```
b5f4:  entry   a1, 48
b617:  l32i.n  a12, a1, 12          ; num_active_channels
b619:  l32i.n  a13, a1, 8           ; rand_algo (operand-local copy)
b61e:  const16 a10, 0x1e80          ; log @DRAM 0x1e80
b621:  call8   0x18d0c
b62b:  call8   0x7fd0               ; operand decode
b631:  call8   0x44f0               ; setup
b634:  l32i.n  a11, a1, 8           ; rand_algo  -> arg
b636:  call8   0xb640               ; -> dispatcher
```

DRAM `0x1e80` (byte-exact):
`"P%i: RandSetState : num_chans = %0d : rand_algo = 0x%x\n"`.

The dispatcher `0xb640` computes the **algorithm-select bit** with one `saltu` and stores it
at `[a1+36]`:

```
b640:  entry   a1, 80
b646:  s32i.n  a3, a1, 24           ; [a1+24] = rand_algo
b64c:  l32i    a0, a0, 0
b64f:  extui   a4, a0, 0, 2         ; 2-bit config read
b659:  extui   a0, a0, 1, 2
b676:  l32i.n  a2, a1, 24           ; a2 = rand_algo
b678:  movi.n  a3, 1
b67a:  saltu   a2, a2, a3           ; a2 = (rand_algo < 1) = (rand_algo == 0)
b67d:  s8i     a2, a1, 36           ; ALGO-SELECT bit = (rand_algo == 0)
b680:  l8ui    a2, a1, 36
b683:  bbci    a2, 0, 0xb6c0        ; algo-select==0 -> skip the ==0 extra-setup block
       ; (algo-select==1, i.e. rand_algo==0/LFSR -> b68d..b6b0 extra state writes)
b6c0:  l8ui    a2, a1, 40
b6c3:  bbci    a2, 0, 0xb6d8        ; ENABLE gate: skip seeding if [a1+40] bit0 clear
b6c9:  l8ui    a2, a1, 36           ; reload algo-select bit
b6ce:  extui   a10, a2, 0, 1        ; arg = algo-select bit (0/1)
b6d1:  call8   0xb6dc               ; -> SHARED SetSeeds, passing the select bit
```

> **GOTCHA — `0xb683` is *not* a second algorithm fork.** It branches on the same
> algo-select bit `[a1+36]`, but both arms converge at the enable gate `0xb6c0`; the `==0`
> (LFSR) arm merely performs a few extra state-field writes before re-joining. The *only*
> function-level fork is at the shared `SetSeeds 0xb6dc`. Treat `0xb683` as conditional
> in-line setup, not a dispatch. `[HIGH/OBSERVED — both arms re-join at 0xb6c0]`

### 4.3 The fork instruction — shared `SetSeeds 0xb6dc`

This is the decisive instruction. The shared body stores the passed select bit, reloads it,
and forks with a single `bbci`:

```
b6dc:  entry   a1, 48
b6df:  s8i     a2, a1, 12           ; store the algo-select bit
b6e2:  l8ui    a2, a1, 12
b6e5:  bbci    a2, 0, 0xb6f5        ; *** THE FORK ***   bytes: 07 62 0c
       ;  bit SET   (1) -> fall through -> b6e8 j 0xb6ed -> call8 0xb700  (LfsrSetSeeds, frame 192)
       ;  bit CLEAR (0) -> branch 0xb6f5            -> call8 0xb744  (XorwowSetSeeds(TIE), frame 0x400)
b700:  entry   a1, 192              ; LfsrSetSeeds  (SMALL frame)
b716:  const16 a10, 0x1eb8          ; log "P%i: LfsrSetSeeds\n"
b719:  call8   0x18d0c
b744:  entry   a1, 0x400           ; XorwowSetSeeds(TIE)  (LARGE frame)
b75a:  const16 a10, 0x1ecb          ; log "P%i: XorwowSetSeeds(TIE)\n"
```

DRAM `0x1eb8 = "P%i: LfsrSetSeeds\n"`; DRAM `0x1ecb = "P%i: XorwowSetSeeds(TIE)\n"` — both read
directly. `[HIGH/OBSERVED — fork bytes 07 62 0c at 0xb6e5; both body entries + both log
strings.]`

`RandGetState`'s shared `GetSeeds 0xb9ec` mirrors it exactly — same `bbci a2, 0, 0xba05`
(bytes `07 62 0c`), LFSR arm log `0x1f1d = "P%i: LfsrGetSeeds"`, Xorwow arm log
`0x1f30 = "P%i: XorwowGetSeeds(TIE)"`. `[HIGH/OBSERVED]`

### 4.4 The net polarity

Composing §4.2 and §4.3 resolves the bit polarity end-to-end:

```
algo_select_bit = saltu(rand_algo, 1) = (rand_algo == 0)

   rand_algo == 0 (LFSR)   -> bit = 1 -> SET   -> fall through -> LfsrSetSeeds   0xb700
   rand_algo == 3 (XORWOW) -> bit = 0 -> CLEAR -> branch       -> XorwowSetSeeds 0xb744
```

This matches the ISA enum exactly (`LFSR=0, XORWOW=3`) and is **cross-validated three ways**:
the `saltu` chain, the SEQ `{0,3}` gate (§4.1), and the host enum (§2). `[HIGH/OBSERVED for
the `bbci` fork + the two body entries; the integer→algo mapping HIGH by triple
cross-validation.]`

### 4.5 The generator dispatcher does **not** fork on `rand_algo`

The per-draw `Rng` generator dispatcher `0xbf74` logs `"P%i: Rng : num_chans = %0d"`
(DRAM `0x1f4a`), decodes, sets up, and unconditionally tail-calls `0xbed8` — **no algo
branch**:

```
bf74:  entry   a1, 48
bf90:  const16 a10, 0x1f4a          ; "P%i: Rng : num_chans = %0d"
bf9c:  call8   0x7fd0               ; decode
bfa2:  call8   0x44f0               ; setup
bfa5:  call8   0xbed8               ; -> post-proc + driver   (NO rand_algo read)
```

`0xbed8` is a *dtype / post-process* switch (`beqi a2, 10`; `bnei a2, 6`) that builds the
`0x3F80` (`= 0x3F800000 = 1.0f` high half) and `127`/`-1` mantissa-fill constants at
`0xbf2d…0xbf3f`, then **unconditionally** `call8 0xbc78` at `0xbf69` — again no algo branch.
The driver `0xbc78` (frame `0x100`) self-names `"P%i: XorwowRng(TIE)"` (DRAM `0x1f66`, log
@`0xbd57`) and carries the Xorwow Weyl constant:

```
be08:  movi.n  a2, 5
be0a:  const16 a2, 0x87c5           ; const16-pair builds 0x587c5 = 362437  (Weyl increment)
```

> **CORRECTION — the Weyl constant is `0x587c5 = 362437`, not `0x87c5`.** Xtensa `const16`
> assembles a 32-bit immediate from a *pair* of `const16` ops (high half via the preceding
> `movi.n a2, 5`, then the low half `const16 a2, 0x87c5`), yielding `(5 << 16) | 0x87c5 =
> 0x587c5 = 362437` — the classic XORWOW Weyl-sequence additive constant (the same `362437`
> seen in the [software-Xorwow](rng-xorwow-sw.md) path). The bare low half `0x87c5` alone is
> `34757` and is meaningless in isolation. `[HIGH/OBSERVED — arithmetic identity on the
> const16 pair at 0xbe08/0xbe0a.]`

So **the seed init is the only thing that forks; the advance is shared.** Whether the LFSR arm
applies the Weyl step or the shared recurrence degenerates to a pure single-word shift under
the LFSR's differently-initialized state is *not* byte-decidable — there is no clean
algo-conditional at the driver entry; any per-algo behaviour is either state-driven or buried
in a FLIX-desync'd bundle. `[HIGH that there is no clean algo fork in the driver; MED/INFERRED
that the driver is fully algo-agnostic.]`

---

## 5. The LFSR state, seed, and recurrence

### 5.1 The state — one `u32` word per lane

`d4_rand.h` states the LFSR state model verbatim:

> *"LFSR treats each of the 128 lanes as an independent random number generator. The lowest
> u32 word of the rand_state (rand_state[0]) for each lane is used as the initial state. The
> LFSR algorithm is applied one or more times (number of steps parameter) and the resulting
> u32 is issued as the random number for that lane. (On Neuron this takes about 30 cycles to
> generate one element for each lane.)"*

So the LFSR is **128 independent single-register generators**, each seeded from `rand_state[0]`.
Contrast the other algorithms (also from `d4_rand.h`): PCG32 uses **two** words
(`rand_state[0..1]`, ~110 cycles), [Xorwow](rng-xorwow-tie.md) uses **5–6** words (`x,y,z,w,v`
+ Weyl `d`), and Philox is **engine-wide** (a single 16 B counter + 8 B key + 4 B offset
stream distributed across all lanes — *not* per-lane). The `S1_RAND` operand carries
`imm_state[8]` (eight `u32`, the maximum state envelope); for LFSR only `imm_state[0]` is the
seed (the rest constrained to `0` for the LFSR pairings per the `has_valid_*` blocks). The
`pseudo_set_rng_seed` operand likewise carries a single 4-byte `seed` (offset 16), consistent
with the one-word LFSR seed. `[HIGH/OBSERVED — header verbatim + struct offsets]`

### 5.2 The structural smoking gun — `LfsrSetSeeds` vs `XorwowSetSeeds`

The two seed-set bodies, decoded side by side, confirm the 1-word-vs-6-word asymmetry:

| | `LfsrSetSeeds 0xb700` | `XorwowSetSeeds(TIE) 0xb744` |
|---|---|---|
| `entry` frame | **192** (small) | **`0x400`** (large) |
| `memset` of state | none | present (the `0x180`-stride zero) |
| seed-word writes | one FLIX bundle (desync'd) | 6-fold unrolled (`b769/b779/b789/…`) |
| Weyl const | none | `0x587c5` chain |
| big vector helpers | none | `call8 0x14a70/0x15538/0x1d440/0x25348/…` |
| body size | ~`0x44 B` | large |
| `GetSeeds` counterpart | single `call8 0xcca8` | `entry a1, 0x580` |

The frame-size and body-size gap directly mirrors the **1-word LFSR state** vs the **6-word
Xorwow state**. `[HIGH/OBSERVED — both `entry` frames byte-read; body structure observed past
the FLIX desync via explicit `--start-address`.]`

### 5.3 `rand_num_steps` — the LFSR-only multi-step advance

`d4_rand.h`: `rand_num_steps` (offset 13) is the number of times HW updates the RNG state
before emitting one output `u32`. *"Only used for LFSR to mitigate back-to-back random number
correlations. If LFSR, rand_num_steps >= 1. If PCG32 or PHILOX, rand_num_steps == 1."* The ISA
constraint is explicit:

```
fn has_valid_rand_num_steps(i: Inst) -> bool {
       (i.d4_rand.rand_algorithm == RandAlgorithm::LFSR)
    || (i.d4_rand.rand_num_steps == 1)
}
```

So the LFSR is iterated `rand_num_steps` times per draw; the other algorithms exactly once.
The Q7 multi-step loop is *not* cleanly observable (it lives in the FLIX-desync'd inner core):
the step-count **semantics** are `[HIGH/OBSERVED header + constraint]`; the exact Q7 loop
construct is `[MED]`.

### 5.4 The recurrence / polynomial — what is and is not recoverable

* **Width = 32 bits** (one `u32` word per lane; output is one `u32`). `[HIGH/OBSERVED]`
* **Galois vs Fibonacci / exact taps / feedback polynomial: UNRECOVERED.** An exhaustive
  little-endian scan of the MARIANA Q7 POOL IRAM **and** DRAM for the eight classical 32-bit
  LFSR tap masks `{0x80000057, 0x800000C2, 0xA3000000, 0x80200003, 0x04C11DB7, 0xEDB88320,
  0xD0000001, 0x8000000B}` returns **zero** little-endian hits — there is no embedded
  feedback-mask constant. `[HIGH/OBSERVED negative]` The `d4_rand.h` header names the
  algorithm *"LFSR"* but, unlike PCG32/Philox, does **not** state its polynomial or tap layout.
  The state-advance lives in a FLIX-desync'd bundle (the flat DEBUG Q7 IRAM has no `.xt.prop`
  property table — the corpus-wide [FLIX-desync ceiling](../../reference/confidence-model.md)),
  so the per-step shift/xor micro-ops are not byte-pinnable.
* **Characterization.** The "LFSR" is therefore a **32-bit single-word xorshift-style
  linear-feedback generator** (a shift+xor recurrence over GF(2) on the full word), composed
  from the generic integer vector ALU primitives (the xor/sll/srl grid driven through the
  shared TensorTensorArith engine) — **not** a tap-bit Galois/Fibonacci LFSR with an embedded
  feedback-mask constant, and **not** a dedicated LFSR opcode (the GPSIMD opcode roster carries
  no `rng`/`lfsr` mnemonic; the advance is the shared driver `0xbc78`). The 32-bit-width +
  xorshift-family + GF(2)-linear + no-dedicated-opcode characterization is `[HIGH]`; the exact
  taps and Galois-vs-Fibonacci direction are `[LOW/UNRECOVERED]` and honestly flagged.

> **This is a named wall — `closable-with-corpus` (a FLIX-aware disassembler config) or
> `closable-with-hardware` (a device run that exposes the recurrence).** Do **not** fabricate a
> tap polynomial. A reimplementer who needs bit-identical output must recover the recurrence by
> a captured device round-trip; the *interface* (1-word `u32` per lane, seeded from
> `rand_state[0]`, iterated `rand_num_steps>=1`, output `u32`) is fully specified above.

---

## 6. Per-generation presence — closing the subsystem

Two independent evidence streams agree on which generation carries what: **(A)** the host ISA
enum per arch header, and **(B)** the per-generation Q7 POOL RNG **log strings**.

| GEN | ISA enum has `XORWOW`? | Q7 POOL RNG strings present | wired POOL algos |
|---|---|---|---|
| **SUNDA** (v2) | **NO** (`{LFSR,PCG32,PHILOX}`; no `XORWOW`) | **none** — no `Lfsr`/`Xorwow`/`Rng`/`RandSet`/`RandGet` strings in the SUNDA Q7 POOL image | none (RNG not implemented on POOL) |
| **CAYMAN** (v3) | YES (`XORWOW=3` added) | `Xorwow(SW)` Init/Set/Get/Rng only; **no** `Lfsr`, **no** `(TIE)` | **XORWOW only** (software; see [rng-xorwow-sw](rng-xorwow-sw.md)) |
| **MARIANA** (v4) | YES | `Xorwow*(TIE)` **+** `LfsrSetSeeds`/`LfsrGetSeeds` (decoded here) | **LFSR + XORWOW** (TIE) |
| **MARIANA_PLUS** (v4+) | YES | same `(TIE)` + `Lfsr` set; fork bytes byte-match MARIANA | **LFSR + XORWOW** (TIE) |
| **MAVERICK** (v5) | YES | same `(TIE)` + `Lfsr` set (string-confirmed; IRAM body nm-aliased onto DRAM, **not separately carved**) | **LFSR + XORWOW** (header-OBSERVED → interior INFERRED) |

`[HIGH/OBSERVED — enum per-arch header grep + per-gen string sweep + MARIANA_PLUS fork-address
byte-match.]`

The proofs:

* **SUNDA negative.** The SUNDA Q7 POOL image (only a RELEASE build ships; no DEBUG) contains
  the `…/sunda/pool/src/…` source-path strings (proving the carve hit SUNDA) but **zero**
  `Lfsr`/`Xorwow`/`Rng`/`RandSetState`/`RandGetState` strings. SUNDA defined the `LFSR` *enum*
  value (`=0`) before POOL implemented *any* RNG handler. `[HIGH/OBSERVED]`
* **CAYMAN is Xorwow(SW)-only.** Its Q7 POOL DRAM carries `XorwowSetSeeds(SW)`,
  `XorwowRng(SW)`, `XorwowGetSeeds(SW)`, `Xorwow(SW) : Initializing XORWOW state…` — and **no**
  `Lfsr*` strings and **no** `(TIE)` suffix. CAYMAN added the `XORWOW=3` enum value and shipped
  the *software* Xorwow only. `[HIGH/OBSERVED]`
* **MARIANA / MARIANA_PLUS / MAVERICK carry LFSR + Xorwow(TIE).** All three Q7 POOL DRAMs
  carry `LfsrSetSeeds`, `LfsrGetSeeds`, and the `Xorwow*(TIE)` family. The MARIANA_PLUS fork
  is byte-identical to MARIANA: bytes `07 62 39` @`0xb683`, `07 62 11` @`0xb6c3`, `07 62 0c`
  @`0xb6e5`. `[HIGH/OBSERVED byte-match]`

> **NOTE — LFSR was added at MARIANA, not CAYMAN.** The progression is: SUNDA = no POOL RNG;
> CAYMAN = `XORWOW(SW)` only (the enum value's debut); MARIANA+ = the `(TIE)`-build Xorwow +
> the LFSR. MAVERICK adds **no new algorithm** (still `{LFSR, XORWOW}` on POOL; PCG32/PHILOX
> remain ISA-defined-but-"not currently supported on POOL"). `[HIGH/OBSERVED]`

> **MAVERICK (v5) interior — header-OBSERVED only → INFERRED.** Per the
> [generation-grounding policy](../../reference/confidence-model.md), the v5 LFSR+Xorwow
> *presence* is OBSERVED (its Q7 POOL DRAM carries the `Lfsr*`/`Xorwow*(TIE)` strings), but the
> v5 IRAM image is **nm-aliased onto its DRAM symbol and not separately carvable**, so the
> fork-body *bytes* are **INFERRED identical-family** from MARIANA/MARIANA_PLUS, not re-read.
> Do not cite a Maverick fork address as byte-observed. `[presence HIGH/OBSERVED; body
> MED/INFERRED]`

---

## 7. The `uint32` output contract — reconciled with the consumers

* **Raw product.** Both LFSR and Xorwow emit a `uint32` per lane (LFSR: `rand_state[0]`
  advanced `rand_num_steps` times → `u32`; Xorwow: `v + d` → `u32`). The `RAW_U32`/`uint32`
  ABI is **generation-invariant and algorithm-invariant** — only `post_process` optionally
  floats it. `[HIGH/OBSERVED — header + the shared driver]`
* **The `post_process` field is the contract** (§2):
  * `RAW_U32(0)` → the raw `uint32` (what [Dropout](dropout.md) / [Rand2](rand2.md) consume).
  * `UNIFORM_IN_RANGE(1)` → fp32 uniform in `(min_fp32=params[0], max_fp32=params[1])`; this is
    the `0x3F800000` (`1.0f`) + `0x7FFFFF` mantissa-fill float seam built at `0xbf2d…0xbf3f`
    inside `0xbed8`. `[the `0x3F80`/`127`/`-1` constants HIGH; the exact mantissa-fill cast
    MED through the FLIX desync — same condition as the Xorwow pages.]`
  * `NORMAL(2)` → *"Not supported yet"* (`d4_rand.h`: *"Normal is not supported by HW yet"*).
* **Dropout reconciliation.** `s3d3_dropout.h` states the Dropout op *"generates a u32 LFSR for
  each lane for each element; converts it to an f32 in range (0.0 to 1.0); compares to
  threshold (ptr or immediate)"* — with `if threshold_type == DropRate: if (rand > threshold)
  output 0.0` and `if threshold_type == KeepRate: if (rand < threshold) output 0.0`. So the DVE
  Dropout's **inline** random source is the **LFSR** (`RAW_U32` produced, then cast
  `uint32→fp32`, then compared). This pins the [Dropout](dropout.md) consumer to the LFSR arm.
  `[HIGH/OBSERVED header verbatim]`

---

## 8. The RNG subsystem — the five-way closing map

This page closes the GPSIMD RNG subsystem. The five pieces and their relationship:

| piece | what it is | algo | per-draw advance | output | page |
|---|---|---|---|---|---|
| **Xorwow-SW** | software XORWOW on the POOL compute engine (CAYMAN debut) | XORWOW | `XorwowRng(SW)` | `u32` | [rng-xorwow-sw](rng-xorwow-sw.md) |
| **Xorwow-TIE** | the `(TIE)`-build XORWOW (MARIANA+) | XORWOW | `XorwowRng(TIE)` driver `0xbc78`, Weyl `0x587c5` | `u32` | [rng-xorwow-tie](rng-xorwow-tie.md) |
| **LFSR** | second algo (MARIANA+); 1-word/lane, `rand_num_steps≥1` | LFSR | **shares** driver `0xbc78`; only the seed init `0xb700` differs | `u32` | *this page* |
| **Dropout** | DVE consumer; inline LFSR → f32(0,1) → threshold compare | LFSR (inline) | inline per element | mask / passthrough | [dropout](dropout.md) |
| **Rand2** | user random-tensor op (`D3_RAND_STRUCT`, opcodes `RAND2`/`RNG`) | `{LFSR, XORWOW}` via same enum | (FW-48 scope) | `u32` / fp32 | [rand2](rand2.md) |

The unifying structure:

* **One enum, one fork.** `rand_algorithm` (offset 12) selects the recurrence; the device
  reduces the four-value ISA enum to `{LFSR(0), XORWOW(3)}` via `saltu(rand_algo, 1)` and forks
  with a single `bbci` in the *shared* `Set`/`GetSeeds` body. PCG32/PHILOX are ISA-defined but
  POOL-unsupported.
* **One advance, two seeds.** There is no separate `LfsrRng` and no LFSR opcode; LFSR and
  Xorwow share the per-draw driver and differ only in seed initialization
  (`LfsrSetSeeds 0xb700` frame 192 vs `XorwowSetSeeds 0xb744` frame `0x400`).
* **One output ABI.** Every generator emits a `uint32` per lane (`RAND_POST_PROC::RAW_U32`),
  optionally cast to fp32 `(min, max)` (`UNIFORM_IN_RANGE`). Dropout consumes the raw `uint32`
  from the LFSR arm; the producer decode (Rng + RandGet/SetState on POOL) is complete, with the
  Rand2 device routing flagged as the one remaining boundary
  ([Rand2](rand2.md) / `d3_rand`).
* **Seed/state plumbing** (the `RandSetState`/`RandGetState` operand transport, the
  `pseudo_set_rng_seed` single-word seed) is documented at
  [RNG Seed-State Opcodes](rng-seed-state-ops.md).

---

## 9. Honesty ledger

**HIGH / OBSERVED** — direct disasm, byte read, symtab/header read, or arithmetic identity:

* The `rand_algo` enum `{LFSR=0, PCG32=1, PHILOX=2, XORWOW=3}` from the shipped ISA headers,
  byte-identical across cayman/mariana/maverick; SUNDA truncated (no `XORWOW`).
* The `RAND_POST_PROC {RAW_U32=0, UNIFORM_IN_RANGE=1, NORMAL=2}` and `RAND_SRC {RNG_LFSR=0,
  RNG_XORWOW=1, RNG_PHILOX=2, OUTPUT_CVT_LFSR=3}` enums.
* The `D4_RAND`/`S1_RAND`/`D3_RAND` field offsets (`rand_algorithm@12` etc.) and their match to
  the Q7 dispatcher field reads; the `struct2opcode` JSON binding.
* The SEQ `{0,3}` validation gate (`saltu`/`addi -3`/`bbsi` @`0x301b`); the two error strings
  (`0xe89`/`0xe41`, "**not currently** supported on POOL").
* The Q7 fork: `RandSetState 0xb5f4 → dispatcher 0xb640` (`saltu rand_algo,1`) → shared
  `SetSeeds 0xb6dc` `bbci a2,0,0xb6f5` (bytes `07 62 0c`) → `0xb700`(Lfsr, frame 192) vs
  `0xb744`(Xorwow, frame `0x400`); the symmetric `GetSeeds 0xb9ec` fork; every log xref
  (`0x1eb8`/`0x1ecb`/`0x1f1d`/`0x1f30`) read directly.
* The net polarity `rand_algo==0→LFSR, ==3→XORWOW`, triple-cross-validated.
* The `Rng` dispatcher `0xbf74` + `0xbed8` do **not** fork on `rand_algo`; unconditional
  `call8 0xbc78` (shared `XorwowRng(TIE)` driver); the Weyl `0x587c5 = 362437` const16 pair.
* The LFSR = 1 `u32` word/lane (header verbatim) + the structural confirmation (frame 192 /
  no memset / no seed chain / no Weyl / single bundle).
* `rand_num_steps` = LFSR-only multi-step advance (header + ISA constraint).
* No LFSR tap-mask constant anywhere in Q7 POOL IRAM/DRAM (negative).
* Dropout's inline RNG = LFSR (`s3d3_dropout.h` verbatim).
* The per-gen matrix (SUNDA none / CAYMAN XORWOW-SW only / MARIANA+ LFSR+XORWOW-TIE), enum +
  string streams agreeing; MARIANA_PLUS fork-address byte-match.

**MED / INFERRED:**

* The exact Q7 realization of the LFSR per-draw step (the `rand_num_steps` loop; whether the
  shared driver applies the Weyl step on the LFSR arm or behaves as a pure single-word shift) —
  no clean algo-conditional at the driver entry; either state-driven or FLIX-desync'd.
* The `uint32→fp32` `UNIFORM_IN_RANGE` mantissa-fill cast (the `0x3F80`/`127`/`-1` constants
  are HIGH; the exact cast steps MED through the desync).
* The `0xb6c3` "enable gate" precise field (`[a1+40]` bit0 — `num_chans>0` vs `update_state`)
  is inferred, not byte-pinned.

**LOW / UNRECOVERED:**

* The LFSR feedback **polynomial** / exact tap positions / Galois-vs-Fibonacci form (no
  embedded mask constant; FLIX-desync'd body; header silent on the poly). 32-bit width +
  xorshift-family + GF(2)-linear + no-dedicated-opcode is solid; the precise recurrence is not
  recovered. **Named wall.**
* The MAVERICK Q7 POOL IRAM **body** (nm-aliased onto DRAM; not separately carved; presence
  HIGH by string, body INFERRED identical-family).
* The Rand2 (`d3_rand`) device operand path — struct-level reuse of this enum is OBSERVED; the
  device routing is [Rand2](rand2.md)'s scope, flagged.

---

## Cross-references

* **[RNG — Xorwow Software Path](rng-xorwow-sw.md)** — the CAYMAN software XORWOW sibling
  (the `(SW)` strings; the `362437` Weyl debut).
* **[RNG — Xorwow TIE Hardware Path](rng-xorwow-tie.md)** — the MARIANA+ `(TIE)` XORWOW; the
  shared driver `0xbc78` this page's LFSR reuses for its advance.
* **[RNG Seed-State Opcodes](rng-seed-state-ops.md)** — the `RandSetState`/`RandGetState`
  operand transport and the `pseudo_set_rng_seed` single-word seed.
* **[Rand2 (user random-tensor op)](rand2.md)** — the `D3_RAND` op that reuses this `rand_algo`
  enum; the one remaining device-routing boundary.
* **[Dropout](dropout.md)** — the DVE consumer whose inline RNG is the LFSR (`RAW_U32` →
  `uint32→fp32` → threshold compare).
* **[The Confidence & Walls Model](../../reference/confidence-model.md)** — the
  HIGH/MED/LOW × OBSERVED/INFERRED tagging, the FLIX-desync ceiling, and the v5/Maverick
  header-OBSERVED-only policy this page applies.
