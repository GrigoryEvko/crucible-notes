# TensorScalarImmLd (Arith/Bitvec) — the immediate-vector loader for PtrMulti

> **Scope.** `TensorScalarImmLdArith` (opcode `0x70`) and `TensorScalarImmLdBitvec` (opcode `0x71`)
> are the **immediate-LOAD** members of the GPSIMD `TensorScalar*` family on the DVE (Data/Vector
> Engine, `engine_idx=3`). The first thing this page establishes byte-for-byte is what they are
> **not**: they are **not** a *"tensor op an inline immediate"* elementwise compute. They are
> **load/staging** instructions that **preload up to eight per-channel scalars** from SBUF/PSUM into
> the DVE's storage flops, where the **following** [`TensorScalarPtrMulti`](ts-ptrmulti.md)
> (`0x4F`/`0x5F`) compute op consumes them as its per-W-slice multi-immediates. They ride the
> **`S2_BN` 64-byte SRC-only operand struct** — the **same** wire format used by
> [`BatchNormParamLoad`](batchnorm-paramload.md) and [`MatchValueLoad`](search-cluster.md) — so this
> page keeps the `S2_BN` field layout byte-identical to those siblings and documents how ImmLd's
> *use* of that struct differs.
>
> This page proves: the two opcodes + their `// n, ucode/kaenadve exists, not maintained/used`
> deprecation markers; the `S2_BN` struct (compile-verified `sizeof == 64` on all four gens); the
> preload-then-consume two-instruction protocol; the **Arith-vs-Bitvec split** — which, uniquely in
> this family, is a **dtype/conversion** split (`tsm_immld_dtypes`), **not** an AluOp-set split
> (`S2_BN` carries no `op0`/`op1` field at all); the dtype matrix; and the per-gen presence — the
> ops are **ISA-defined and kernel-registered on every DVE gen, but their on-device workers are
> 40-byte LOG-only stubs** (the firmware realisation of *"not maintained/used"*).
>
> Confidence tags use the `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` model defined in
> [`../../reference/confidence-model.md`](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
> read-from-byte / proven-by-compile, `[MED/INFERRED]` = reasoned over OBSERVED, `[…/CARRIED]` =
> re-used at a sibling's confidence without re-reading the artifact this pass.

---

## 1. TL;DR — the pinned facts

| # | Fact | Evidence | Tag |
|---|------|----------|-----|
| 1 | `TensorScalarImmLdArith` = **`0x70`**, `TensorScalarImmLdBitvec` = **`0x71`**; both carry `// n, ucode/kaenadve exists, not maintained/used` — byte-identical on **all four gens**. | `common.h:209/210` (mariana) | HIGH/OBSERVED |
| 2 | **ImmLd is a LOAD, not a compute op.** `S2_BN` is *"one 2d SRC, no DST"* — **no** `dst_mem_pattern`, **no** `op0`/`op1` AluOp, **no** `reverse_operands`, **no** accumulator. It preloads ≤8 per-channel scalars into DVE storage flops for the *next* op to read. | `s2_bn.h:11–16/72–92` header verbatim | HIGH/OBSERVED |
| 3 | The **consumer** is [`TensorScalarPtrMulti`](ts-ptrmulti.md) (`0x4F`/`0x5F`, struct `S4D4_TSM`): `for i in range(W): dst[i,…] = (src[i,…] op scalar[i])`, where `scalar[i]` is the i-th ImmLd-preloaded value and `W = num_elem[3]` must equal the count ImmLd loaded. | `s4d4_tsm.h:23–49` verbatim | HIGH/OBSERVED |
| 4 | **Struct = `NEURON_ISA_TPB_S2_BN_STRUCT`, 64 B**, shared by exactly **four** opcodes: `BatchNormParamLoad`, `MatchValueLoad`, `ImmLdArith` (`0x70`), `ImmLdBitvec` (`0x71`). Compile-verified `sizeof == 64` on sunda/cayman/mariana/maverick. | `instruction_mapping.json` `struct2opcode` + `gcc` `offsetof`/`sizeof` this pass | HIGH/OBSERVED |
| 5 | The **Arith-vs-Bitvec split is a DTYPE/CONVERSION split, not an AluOp-set split.** `S2_BN` has no AluOp field; the *only* `0x70`/`0x71` difference is the accepted load dtype set + bit-interpretation (`tsm_immld_dtypes`): Arith = 12 dtypes → **converted to fp32**; Bitvec = 4 dtypes → **raw bits, zero-extended**. | `s2_bn.h:215–234` (`tsm_immld_dtypes`) verbatim | HIGH/OBSERVED |
| 6 | The values to load come **entirely from `src_mem_pattern`** (1..8 elements); `imm1`/`imm0_ptr`/`imm0_src` are **forced zero** (`has_s2_bn_zero_immediates`). ImmLd carries **no inline scalar** — it loads a *vector* of up-to-8 scalars from memory. | `s2_bn.h` `has_tsm_immediates_load_src_element_cnt` + `has_s2_bn_zero_immediates` | HIGH/OBSERVED |
| 7 | **Universally present, universally stubbed.** Eight `"S: TensorScalarImmLd{Arith,Bitvec}"` self-name strings (2/gen × 4 DVE regions). The on-device workers are **40-byte LOG-only stubs** (`entry a1,32`; log self-name; `retw.n` — **no compute**), kernel-registered via the stub template. | `grep -abo` (8 hits) + native `xtensa-elf-objdump` of the MARIANA stub block | HIGH/OBSERVED |
| 8 | **One CORRECTION to the family premise:** `S2_BN` is **not** shared with `NonzeroWithCount` (which uses `S3D3_NONZERO_WITH_COUNT`). The real `S2_BN` co-residents besides `MatchValueLoad` are `BatchNormParamLoad` + the two ImmLd ops. | `instruction_mapping.json` `struct2opcode` | HIGH/OBSERVED |

---

## 2. Provenance / carve anchors

Every device-firmware fact below derives **solely** from static analysis of the shipped
device-firmware blob carved out of `libnrtucode_internal.so`, disassembled with the Cadence
Tensilica Xtensa toolchain that ships *inside* the gpsimd-tools package (`xtensa-elf-objdump`,
`XTENSA_CORE=ncore2gp` — the Vision-Q7 *Cairo* 512-bit FLIX/VLIW vector DSP), plus the shipped
public ISA C headers (`aws_neuron_isa_tpb_*.h` / `instruction_mapping.json`) and the host symbol
table (`nm`). No source was consulted. The `extracted/` tree is gitignored — reach it with
`fd --no-ignore` or absolute paths.

| Artifact | Value |
|----------|-------|
| Container | `…/custom_op/c10/lib/libnrtucode_internal.so` |
| Container sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` (10,276,288 B) — re-verified this pass |
| Disassembler | `…/gpsimd_tools_tgz/bin/xtensa-elf-objdump` (Binutils 2.34.20200201, Xtensa Tools 14.09), `XTENSA_CORE=ncore2gp` |
| MARIANA `NX_DVE` DEBUG IRAM | `MARIANA_NX_DVE_DEBUG_IRAM_get.data` file offset `0x408fc0` / size `0x1c560` (`.rodata` VA == file offset; disassembled at VA 0) — carved & decoded this pass |
| MARIANA `NX_DVE` DEBUG DRAM | `MARIANA_NX_DVE_DEBUG_DRAM_get.data` file offset `0x425520` / size `0x7000` (the `S:` self-name strings) |
| CAYMAN `NX_DVE` DEBUG IRAM | file offset `0x16f660` / size `0x1bcc0` |
| MAVERICK `NX_DVE` DEBUG IRAM | file offset `0x8945c0` / size `0x19000` |
| ISA headers (compile-authoritative) | `…/c10/include/neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_{s2_bn,common,s4d4_tsm}.h` + `instruction_mapping.json` |

The `nm -S` symbol-table offsets, the eight self-name strings, the MARIANA stub-block disassembly,
and the four-gen `gcc` compile-verify were all **reproduced this pass**; `objdump` exit 0.
`[HIGH/OBSERVED]`

---

## 3. The opcodes — ImmLd in the `TensorScalar*` family block

Read verbatim from `aws_neuron_isa_tpb_common.h` (mariana; byte-identical value **and** marker on
every gen — diff'd this pass):

```c
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_ARITH    = 0x70,   // n, ucode/kaenadve exists, not maintained/used
NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_BITVEC   = 0x71,   // n, ucode/kaenadve exists, not maintained/used
```

These are the **immediate-load** members of the family mapped on
[`tensor-scalar.md`](tensor-scalar.md): the *maintained* compute ops are `TensorScalarArith`
(`0x43`) / `TensorScalarBitvec` (`0x53`) and the deprecated-but-wired `…Ptr` pair (`0x44`/`0x54`);
ImmLd (`0x70`/`0x71`) and its consumer [`TensorScalarPtrMulti`](ts-ptrmulti.md) (`0x4F`/`0x5F`)
form the **deprecated/unwired** ImmLd→PtrMulti sub-family — all four carrying the same
`// n, ucode/kaenadve exists, not maintained/used` marker. `[HIGH/OBSERVED — `common.h:209/210` +
`190/191`, all four gens]`

> **NOTE — `0x70`/`0x71` are firmware kernel-lane opcodes, not Xtensa ISA mnemonics.** As with the
> rest of the family ([`tensor-scalar.md` §3 NOTE](tensor-scalar.md)), keep the two axes distinct:
> the ~140-entry `NEURON_ISA_TPB_OPCODE_*` enum is the *firmware kernel-lane* dispatch axis; the
> `ivp_*` vector roster is the *Xtensa ISA* axis. ImmLd's opcode dispatches a DVE handler — which on
> this build is a stub (§7). `[HIGH/OBSERVED]`

### 3.1 The deprecation marker, decoded

The `// n, ucode/kaenadve exists, not maintained/used` comment is **not** the bare `// n, use … instead`
deprecation that `TensorScalarPtr` (`0x44`/`0x54`) or `BatchNormParamLoad` (`0x64`) carry. It is the
**stronger** statement: `kaenadve` (the DVE microcode toolchain) **emits a named placeholder worker**
for the op, but the runtime **does not maintain or use it** — i.e. there is a registered handler with
a valid funcVA, but its body was never given a compute implementation. §7 confirms this at the byte
level: the workers are 40-byte LOG-and-return stubs. `[HIGH/OBSERVED — marker verbatim; the
firmware-level meaning proven in §7]`

### 3.2 The `struct2opcode` binding (`instruction_mapping.json`)

```json
"NEURON_ISA_TPB_S2_BN_STRUCT": [
    "NEURON_ISA_TPB_OPCODE_BATCH_NORM_PARAM_LOAD",         // 0x64  batchnorm-paramload.md
    "NEURON_ISA_TPB_OPCODE_MATCH_VALUE_LOAD",              // 0x6d  search-cluster.md
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_ARITH",    // 0x70  this page
    "NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_IMM_LD_BITVEC"    // 0x71  this page
]
```

`S2_BN` binds **exactly four** opcodes — `jq`-read this pass. All four are **load/setup ops**
(src-only, load-into-DVE-state): `BatchNormParamLoad` stages the BN back-prop params,
`MatchValueLoad` stages the 8 match keys `mv[0..7]`, and ImmLd stages the ≤8 PtrMulti scalars.
That shared *"load N values into the engine's storage flops"* role is exactly why ImmLd lives on
`S2_BN`. `[HIGH/OBSERVED — `jq` this pass]`

> **CORRECTION — `S2_BN` is NOT shared with `NonzeroWithCount`.** An earlier family map placed
> `NonzeroWithCount` on `S2_BN`. The `struct2opcode` table refutes this: `NonzeroWithCount` rides
> `S3D3_NONZERO_WITH_COUNT`, and is **absent** from the `S2_BN` binding. The real `S2_BN`
> co-residents besides `MatchValueLoad` are `BatchNormParamLoad` + the two ImmLd ops.
> `[HIGH/OBSERVED — `instruction_mapping.json`]`

---

## 4. The operand struct — `NEURON_ISA_TPB_S2_BN_STRUCT` (64 B, SRC-only)

This is the **same** 64-byte struct decoded for `BatchNormParamLoad` in
[`batchnorm-paramload.md` §3.1](batchnorm-paramload.md) and for `MatchValueLoad` in
[`search-cluster.md` §5.1](search-cluster.md). The field layout is byte-identical; ImmLd's roles
differ. The header banner is verbatim: *"Neuron 'S2_BN' Format — one 2d SRC — no DST. Use for:
BatchNormParamLoad / MatchValueLoad / TensorScalarImmLd."*

A standalone `gcc` `offsetof`/`sizeof` probe over **all four gens'** headers **passed** this pass,
byte-identical every gen:

```
sunda    sizeof=64 src=16 imm1=28 dtype=32 nac=34 imm0_ptr=44 imm0_src=48
cayman   sizeof=64 src=16 imm1=28 dtype=32 nac=34 imm0_ptr=44 imm0_src=48
mariana  sizeof=64 src=16 imm1=28 dtype=32 nac=34 imm0_ptr=44 imm0_src=48
maverick sizeof=64 src=16 imm1=28 dtype=32 nac=34 imm0_ptr=44 imm0_src=48
  TENSOR2D=12 IMM_VAL=4 DTYPE=1 IMM_SRC=1   (all gens)
```

`[HIGH/OBSERVED — `gcc` `offsetof`/`sizeof` over the shipped headers, all four gens this pass]`

| off | size | field | type | role **for TensorScalarImmLd** |
|----:|-----:|-------|------|--------------------------------|
| 0 | 4 | `header` | `HEADER` | `opcode = 0x70 (Arith)` or `0x71 (Bitvec)`, `inst_word_len`, dbg |
| 4 | 8 | `events` | `EVENTS` | wait/update semaphore sync |
| 12 | 4 | `reserved0[4]` | `u8[4]` | **must be 0** (`s2_bn_reserved_zero`) |
| 16 | 12 | `src_mem_pattern` | `TENSOR2D` | **THE LOAD SOURCE** — the 1..8 values to preload (read SBUF/PSUM) |
| 28 | 4 | `imm1` | `IMM_VAL_INST_FIELD` | **must be 0** (`has_s2_bn_zero_immediates`) |
| 32 | 1 | `dtype` | `DTYPE` | **THE LOAD DTYPE** — the Arith/Bitvec gate (§6) |
| 33 | 1 | `reserved1[1]` | `u8` | must be 0 |
| 34 | 1 | `num_active_channels` | `u8` | partition count `1..128` |
| 35 | 9 | `reserved2[9]` | `u8[9]` | must be 0 |
| 44 | 4 | `imm0_ptr` | `IMM_VAL_INST_FIELD` | **must be 0** (`has_s2_bn_zero_immediates`) |
| 48 | 1 | `imm0_src` | `IMM_SRC` | **must be 0** for ImmLd |
| 49 | 15 | `reserved3[15]` | `u8[15]` | must be 0 |

```c
typedef struct NEURON_ISA_TPB_S2_BN_STRUCT {
    NEURON_ISA_TPB_HEADER             header;               //  0 -  3   opcode 0x70 / 0x71
    NEURON_ISA_TPB_EVENTS             events;               //  4 - 11
    uint8_t                           reserved0[4];         // 12 - 15
    NEURON_ISA_TPB_TENSOR2D           src_mem_pattern;      // 16 - 27   the 1..8 values to load
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm1;                 // 28 - 31   FORCED ZERO for ImmLd
    NEURON_ISA_TPB_DTYPE              dtype;                // 32        the load dtype (Arith/Bitvec gate)
    uint8_t                           reserved1[1];         // 33
    uint8_t                           num_active_channels;  // 34        1..128
    uint8_t                           reserved2[9];         // 35 - 43
    NEURON_ISA_TPB_IMM_VAL_INST_FIELD imm0_ptr;             // 44 - 47   FORCED ZERO for ImmLd
    NEURON_ISA_TPB_IMM_SRC            imm0_src;             // 48        FORCED ZERO for ImmLd
    uint8_t                           reserved3[15];        // 49 - 63
} NEURON_ISA_TPB_S2_BN_STRUCT;                              // ISA_STATIC_ASSERT == 64
```

`NEURON_ISA_TPB_TENSOR2D` (`common.h`) = `{ ADDR4 start_addr(4); int16 step_elem[2](4);
uint16 num_elem[2](4) }` = **12 B** — a **2-D** strided access pattern (note: 2-D, vs the 3-D
`TENSOR3D` of the `S3D3_TS` *compute* ops — ImmLd reads a small ≤8-element vector, not a full
operand tensor). `[HIGH/OBSERVED — `gcc` `sizeof` + header]`

> **GOTCHA — `S2_BN` has NO compute fields at all.** Contrast the `S3D3_TS` compute struct of
> [`tensor-scalar.md` §4](tensor-scalar.md): that struct has `src`@16, `dst`@48, `op0`@36, `op1`@37,
> `reverse_operands`@38, `accumulator_cmd`@12, and two scalar immediates @40/@44 — the full compute
> descriptor. `S2_BN` has **only** a `src`, a `dtype`, a `num_active_channels`, and the
> **forced-zero** immediate slots. There is **no `dst_mem_pattern`, no `op0`/`op1` AluOp, no
> `reverse_operands`, no `accumulator_cmd`.** This is a **load descriptor, not a compute
> descriptor** — which is the structural reason the Arith/Bitvec split here cannot be an AluOp-set
> split (§6). `[HIGH/OBSERVED — compile-verified field set]`

### 4.1 The forced-zero immediates — why the slots exist at all

The `imm1`/`imm0_ptr`/`imm0_src` slots are present **only because the struct is shared** with
`BatchNormParamLoad`, which *does* use them (`imm1 = N`, `imm0_ptr = batch_mean`). For ImmLd they
are validated **forced-zero** by `has_s2_bn_zero_immediates` (verbatim):

```c
// fn has_s2_bn_zero_immediates(i: Inst) -> bool {
//       (i.s2_bn.imm0_ptr.imm_arith_fp32 == 0.0)
//    && (i.s2_bn.imm0_src           == 0)
//    && (i.s2_bn.imm1.imm_arith_fp32    == 0.0)
// }
```

The exact same gate is applied to `MatchValueLoad` (see [`search-cluster.md`](search-cluster.md)).
So ImmLd carries **no inline or pointer scalar in the instruction itself** — the 1..8 values come
**entirely** from `src_mem_pattern`, which is the whole point of a load op. `[HIGH/OBSERVED —
`has_s2_bn_zero_immediates` verbatim]`

> **GOTCHA — the same `S2_BN` wire bytes are validated by FOUR different predicate sets.** Because
> `S2_BN` is shared, the **opcode byte selects the meaning** and each opcode has its own validity
> gate. A reimplementer reusing the `S2_BN` encoder across these ops **must** match the per-opcode
> constraints — getting the `src` element count or the immediate-zero rule wrong fails decode:
>
> | opcode | `src` element count | immediates | dtype gate |
> |---|---|---|---|
> | `BatchNormParamLoad` (`0x64`) | **exactly 3** (`{3×1}`/`{1×3}`) | `imm1=N`, `imm0_ptr=batch_mean` **live** | `s2_bn_dtype` (`{FP32,FP16,BF16}`) |
> | `MatchValueLoad` (`0x6d`) | **exactly 8** (`{1×8}`/`{2×4}`/`{4×2}`/`{8×1}`) | **must be zero** | `is_valid_dtype(.., FP32R::False)` |
> | `ImmLdArith` (`0x70`) | **1..8** (`num_elem[0]·num_elem[1] ∈ [1,8]`) | **must be zero** | `tsm_immld_dtypes` (12 dtypes, §6) |
> | `ImmLdBitvec` (`0x71`) | **1..8** | **must be zero** | `tsm_immld_dtypes` (4 dtypes, §6) |
>
> `[HIGH/OBSERVED — the four `…src_element_cnt` predicates + `has_s2_bn_zero_immediates` /
> `has_valid_bn_param_load_imm0` in `s2_bn.h`]`

---

## 5. The ImmLd semantics — an immediate-vector LOAD for PtrMulti

### 5.1 What it does (header verbatim)

`s2_bn.h` documents the contract in plain text (verbatim):

> *"Neuron TensorScalarImmLd[Arith/Bitvec]: These instructions load up to 8 elements per channel
> into storage flops in the DVE Engine. These instruction loads values at the ptrs (up to 8) for
> use as the immediate values for the following TensorScalarPtrMulti instruction."* — `s2_bn.h:72–80`

So ImmLd is a **staging/setup load**: it reads 1..8 elements per channel from `src_mem_pattern`
(SBUF/PSUM) into per-channel DVE **storage flops**, where they sit as the immediate vector that the
**next** [`TensorScalarPtrMulti`](ts-ptrmulti.md) (`0x4F`/`0x5F`) consumes. The `TensorScalar`
prefix is because the loaded values **are** the scalars a subsequent Tensor-Scalar-style
(per-channel-broadcast) op will use — but ImmLd itself performs **no arithmetic** and has **no
output tensor**. The name is *"Immediate-LOAD"*, not *"load an immediate and compute."*
`[HIGH/OBSERVED — header verbatim]`

The destination is **internal DVE state** — the same *"no DST, into the engine's per-channel
flops"* model as `BatchNormParamLoad` (whose target is the `DATAPATH_RAM` bank — see
[`batchnorm-paramload.md` §2.1](batchnorm-paramload.md)) and `MatchValueLoad` (whose target is the
`mv[0..7]` state). The header validator confirms the src is read-only:
`tensor2d_valid(src, dtype, WriteTensor::False, AllowedInPSUM::True, AllowedInSBUF::True)` — so
ImmLd can preload its scalars **straight off the matmul PSUM** as readily as off SBUF.
`[HIGH/OBSERVED — `is_valid_tsm_immediates_load` body]`

### 5.2 The element count = 1..8 (`has_tsm_immediates_load_src_element_cnt`)

Verbatim:

```c
// fn has_tsm_immediates_load_src_element_cnt(i: Inst) -> bool {
//       (   (i.s2_bn.src_mem_pattern.num_elem[0] * i.s2_bn.src_mem_pattern.num_elem[1] >= 1)
//        && (i.s2_bn.src_mem_pattern.num_elem[0] * i.s2_bn.src_mem_pattern.num_elem[1] <= 8))
//    || shape_from_register(i.s2_bn.src_mem_pattern.start_addr)
// }
```

The loaded immediate-vector is **1 to 8 values wide** — the *product* of the two `TENSOR2D` element
counts, unconstrained in shape (a `{1×n}`, `{n×1}`, or `{a×b}` with `a·b ≤ 8` all pass). This is the
laxest of the three `S2_BN` element-count gates: `BatchNormParamLoad` demands exactly 3 elements,
`MatchValueLoad` exactly 8, ImmLd anything in `[1,8]`. `[HIGH/OBSERVED — predicate verbatim]`

> **NOTE — the `shape_from_register` escape is a per-gen feature (absent on SUNDA).** The
> `|| shape_from_register(start_addr)` clause lets the element count come from a register rather
> than the instruction. It is present on cayman/mariana/maverick but **absent on SUNDA (NC-v2)** —
> the only semantic delta in the entire ImmLd surface across gens (§8). `[HIGH/OBSERVED — `diff`
> sunda↔mariana `s2_bn.h`]`

### 5.3 The two-instruction protocol (the stateful preload-then-consume pair)

ImmLd is **step 0** of a stateful pair, structurally the exact analog of
`MatchValueLoad`→`FindIndex8`/`MatchReplace8` (load DVE state, then a later op reads it):

```text
STEP 0   TensorScalarImmLd{Arith|Bitvec}  (0x70/0x71, S2_BN — THIS page):
            read 1..8 elements from src_mem_pattern -> per-channel DVE storage flops imm[0..W-1],
            converting to fp32 (Arith) or loading raw-bits-zero-extended (Bitvec).   (no output)

STEP 1   TensorScalarPtrMulti{Arith|Bitvec}  (0x4F/0x5F, S4D4_TSM — ts-ptrmulti.md):
            for i in range(W):                                # W = src num_elem[3], 1..8
                dst[i, :, :, :] = (src[i, :, :, :]  op  scalar[i])   # scalar[i] = ImmLd's i-th value
```

The consumer header (`aws_neuron_isa_tpb_s4d4_tsm.h`) makes the binding explicit, verbatim:

> *"Two variants: TensorScalarPtrMultiArith — Preload immediate using TensorScalarImmLdArith;
> TensorScalarPtrMultiBitvec — Preload immediate using TensorScalarImmLdBitvec. … The value of W
> must be equal to the number of scalars preloaded using TensorScalarImmLd (i.e. 1 <= W <= 8). …
> for i in range(W): dst[i,:,:,:] = (src[i,:,:,:] op scalar[i]) … swaps a new scalar every time one
> round of XYZ dimensions are completed."* — `s4d4_tsm.h:23–49`

So the LOAD-then-broadcast that the *compute* side performs is split across **two instructions**:
the LOAD is ImmLd (this page), the per-W-slice BROADCAST + single AluOp is PtrMulti
([`ts-ptrmulti.md`](ts-ptrmulti.md)). `[HIGH/OBSERVED — both headers verbatim]`

The annotated C for the ImmLd half (naming the real validity predicate + the verbatim dtype gate):

```c
// TensorScalarImmLd preload. Validity is pre-checked by is_valid_tsm_immediates_load (s2_bn.h)
// before this runs: opcode 0x70/0x71, src element count 1..8, immediates forced zero, dtype in the
// tsm_immld_dtypes set, channels 1..128. There is NO dst, NO AluOp, NO compute.
//
//   W       = num_elem[0] * num_elem[1]              ; 1..8, the count of scalars to preload
//   dve_imm = the per-channel DVE storage-flop bank read by the next PtrMulti as scalar[0..W-1]
static void tsm_immediates_load(const NEURON_ISA_TPB_S2_BN_STRUCT *inst,
                                scalar_flop_t dve_imm[/*num_active_channels*/][8]) {
    const unsigned W = inst->src_mem_pattern.num_elem[0] * inst->src_mem_pattern.num_elem[1];
    for (unsigned ch = 0; ch < inst->num_active_channels; ++ch) {       // 1..128
        for (unsigned i = 0; i < W; ++i) {                              // 1..8
            uint32_t raw = read_tensor2d(&inst->src_mem_pattern, ch, i, inst->dtype);  // SBUF/PSUM
            dve_imm[ch][i] = (inst->header.opcode == OPCODE_TENSOR_SCALAR_IMM_LD_ARITH)
                           ? convert_to_fp32(raw, inst->dtype)          // 0x70: Arith  -> fp32
                           : zero_extend_raw_bits(raw, inst->dtype);    // 0x71: Bitvec -> raw 1/2/4 B
        }
    }
    // No store-to-tensor: the loaded values live in dve_imm, consumed by the following
    // TensorScalarPtrMulti (ts-ptrmulti.md) as scalar[i].
}
```

`[the dtype branch + W derivation HIGH/OBSERVED from the header; the per-channel flop addressing
INFERRED-HIGH — the header says "into storage flops in the DVE Engine" but does not enumerate a
slot map]`

---

## 6. The Arith-vs-Bitvec split — a DTYPE/CONVERSION split (byte-exact)

This is the central, non-obvious finding. For the *compute* ops in this family
([`tensor-scalar.md` §6](tensor-scalar.md): `0x43`/`0x53`; `ts-ptrmulti.md`: `0x4F`/`0x5F`) the
Arith/Bitvec axis is the **`is_general_arith_op` vs `is_bitvec_op` AluOp-set split** on `op0`/`op1`.
**ImmLd has no AluOp** (§4 GOTCHA — `S2_BN` carries no `op0`/`op1` field), so its `0x70`/`0x71`
split **cannot** be that. The *only* difference between the two opcodes is the **load-dtype
acceptance set + the bit-interpretation**, read verbatim from `tsm_immld_dtypes` (`s2_bn.h:215–234`):

```c
// fn tsm_immld_dtypes(i: Inst) -> bool {
//       (   (i.header.opcode == Opcode::TensorScalarImmLdArith)            // 0x70
//        && (   dtype == FP32   || dtype == FP16   || dtype == BFLOAT16
//            || dtype == FP8_EXP3 || dtype == FP8_EXP4 || dtype == FP8_EXP5
//            || dtype == UINT32 || dtype == UINT16 || dtype == UINT8
//            || dtype == INT32  || dtype == INT16  || dtype == INT8))      // 12 dtypes
//    || (   (i.header.opcode == Opcode::TensorScalarImmLdBitvec)           // 0x71
//        && (dtype == UINT32 || dtype == UINT16 || dtype == UINT8 || dtype == INT32)) // 4 dtypes
// }
```

- **ARITH (`0x70`):** the 12 accepted dtypes are **loaded and converted to fp32** in the DVE
  (*"all will be converted to fp32 in the DVE"*, `s2_bn.h:84`). This is the **float-immediate path**
  — the loaded values become fp32 scalars for an arith `PtrMulti`.
- **BITVEC (`0x71`):** the 4 accepted dtypes have **their raw bits loaded, zero-extended** to the
  DVE register width (*"the raw bits of data will be loaded into the DVE registers, 1/2/4 bytes zero
  extended depending on the data type specified"*, `s2_bn.h:86–89`). This is the
  **integer-immediate path** — the loaded values stay raw bits for a bitvec `PtrMulti`.

The split **mirrors** the arith/bitvec contract of the compute ops (arith = fp hub, bitvec =
int-only) — but realised as a **load-time convert-vs-rawbits choice**, not an AluOp gate.
`[HIGH/OBSERVED — `tsm_immld_dtypes` verbatim; byte-identical all four gens]`

### 6.1 The set relation — Bitvec ⊂ Arith (by ordinal)

`DTYPE` ordinals (`common.h`, shared with [`tensor-scalar.md` §6d](tensor-scalar.md)): `INT8 0x2`,
`UINT8 0x3`, `INT16 0x4`, `UINT16 0x5`, `BF16 0x6`, `FP16 0x7`, `INT32 0x8`, `UINT32 0x9`,
`FP32 0xA`, `FP32R 0xB`, `INT64 0xC`, `FP8_EXP3 0xD`, `FP8_EXP4 0xE`, `FP8_EXP5 0xF`, `UINT64 0x1`.

| relation | dtypes | count |
|---|---|---|
| Arith-only (`0x70`, **not** `0x71`) | `FP32` `FP16` `BF16` `FP8_E3` `FP8_E4` `FP8_E5` `INT8` `INT16` | 8 |
| **both** (`0x70` **and** `0x71`) | `UINT8` `UINT16` `UINT32` `INT32` | 4 |
| Bitvec-only (`0x71`, **not** `0x70`) | *(none)* | 0 |

So the Bitvec set `{UINT8, UINT16, UINT32, INT32}` is a **strict subset** of the Arith set. Bitvec
is the *"raw integer container widths"* — the 1/2/4-byte **unsigned** widths plus the two 32-bit
ints — that **zero-extend cleanly**. `[HIGH/OBSERVED — direct set comparison of the two verbatim
lists]`

> **NOTE — Bitvec drops `INT8`/`INT16`, and that omission is meaningful.** The signed narrow widths
> `INT8`/`INT16` are in the **Arith** set (they convert to fp32) but **not** the Bitvec set. This
> matches the *"zero extended"* rule: a raw signed-narrow load has no defined zero-extension (you
> would want sign-extension), so only the **unsigned** narrow widths + the two 32-bit ints are
> allowed as raw loads. `[the rationale is MED/INFERRED from the verbatim dtype lists + the "zero
> extended" header text; the lists themselves are HIGH/OBSERVED]`

---

## 7. The DVE dispatch chain — opcode `0x70`/`0x71` → a LOG-only STUB worker

The ImmLd ops dispatch through the same DVE SEQ-ASCII `"S:"` self-name surface as the rest of the
family. The decisive *"not maintained/used"* evidence is at the byte level: the on-device workers
are **40-byte LOG-and-return stubs**.

### 7.1 The self-name strings (DVE DEBUG DRAM)

`grep -abo 'S: TensorScalarImmLd[A-Za-z]*'` over `libnrtucode_internal.so` returns **8 hits** — 2
per gen × 4 DVE regions (CAYMAN, MARIANA, MARIANA_PLUS, MAVERICK). The MARIANA DEBUG DRAM (file
offset `0x425520`) holds them at in-image DRAM offsets:

| gen | `S: TensorScalarImmLdArith` | `S: TensorScalarImmLdBitvec` |
|---|---|---|
| CAYMAN | `0x275f` | `0x277a` |
| MARIANA / MARIANA_PLUS | `0x283f` | `0x285a` |
| MAVERICK | `0x286f` | `0x288a` |

Byte-read this pass: the bytes at MARIANA DRAM `0x283f` are `"S: TensorScalarImmLdArith\0"` and at
`0x285a` are `"S: TensorScalarImmLdBitvec\0"`. `[HIGH/OBSERVED — `grep -abo` + `dd` byte-read +
`nm`-region mapping]`

### 7.2 The stub workers (40 bytes, LOG + `retw.n`, no compute)

The MARIANA ImmLd workers, disassembled natively (`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`) this
pass — the byte-clean skeleton:

```asm
0000ca5c <ImmLdArith worker>:
  ca5c:  364100   entry    a1, 32              ; *** SMALL frame (maintained workers use entry a1,80)
  ca65:  a40800   const16  a10, 8
  ca68:  a43f28   const16  a10, 0x283f         ; *** "S: TensorScalarImmLdArith" (DRAM self-name)
  ca6b:  a5e30b   call8    0x188a4             ;     the 'S:'-log helper
  ca74:  240800   const16  a2, 8
  ca77:  248028   const16  a2, 0x2880          ;     (a small data write; FLIX literal desyncs here)
  ca81:  1df0     retw.n                        ; *** RETURNS — no config write, no compute
0000ca84 <ImmLdBitvec worker>:                  ; identical 40-B template, self-name 0x285a, retw.n
```

The four `// n` sub-family workers form a **contiguous 40-byte-stride stub block** (MARIANA):

```text
0xca5c  ImmLdArith   (self-name 0x283f)   |  0xca84  ImmLdBitvec   (0x285a)
0xcaac  PtrMultiArith(self-name 0x28a0)   |  0xcad4  PtrMultiBitvec(0x28be)
```

Each is `entry a1, 32`; `const16 a10, <self-name>`; `call8 0x188a4` (log); `retw.n`. **No** config
write, **no** FLIX vector-window program, **no** `alu_op.cpp` compute call, **no** `call0` compute
edges. `[HIGH/OBSERVED — entry frame + LOG loader + `retw.n` byte-decoded; the 40-B stride + 4-stub
block are direct byte counts]`

> **QUIRK — the body-size contrast is the firmware signature of "not maintained/used".** A
> *maintained* DVE worker (e.g. `CacheCumulative`, [`tensor-scalar.md` family](tensor-scalar.md)
> sibling) is `entry a1, 80` (large frame), logs its self-name, then does an `s32i` config write,
> `call0` setup helpers, an `s16i` FLIX window-program, and a ~1.3-KB body with multiple compute
> edges. Across the MARIANA DVE IRAM, `entry a1,80` (large/maintained) frames number **18**, while
> `entry a1,32` (small stub) frames number **137** — the ImmLd workers are firmly in the stub
> population. The ImmLd stub has **none** of the maintained machinery — it is 40 bytes of
> LOG-and-return. The `kaenadve` toolchain emitted a **named placeholder**, but no compute body was
> implemented. `[HIGH/OBSERVED — entry-frame census + the 40-B body decode]`

### 7.3 The registration stubs (the funcVA is wired, the body is a stub)

The ImmLd workers **are** kernel-registered (they have a `kernel_info` slot + a worker funcVA) —
they are *registered placeholders*, not absent. The MARIANA registration stubs, byte-decoded this
pass:

```asm
0x1faa:  entry a1,48 ; const16 a2,0xca5c ; s32i a2,[a1+12] ;     ; stage ImmLdArith funcVA 0xca5c
         l32r a11,0xfffc811c ; l32r a2,0xfffc369c ; call8 0x9920 ; retw   ; DVE kernel-register
0x1fc6:  entry a1,48 ; const16 a2,0xca84 ; s32i a2,[a1+12] ;
         l32r a11,0xfffc8138 ; l32r a2,0xfffc36b8 ; call8 0x9920 ; retw   ; ImmLdBitvec funcVA 0xca84
   (the PtrMulti twins: 0x1f72 -> 0xcaac, 0x1f8e -> 0xcad4 — the consecutive block.)
```

`s32i a2,[a1+12]` writes the funcVA into the `kernel_info` slot; `call8 0x9920` is the DVE
kernel-register routine (the same template `BatchNormParamLoad` and Tensor-Scalar use). The two
`l32r` literals per op (`0xfffc811c`/`0xfffc369c` for Arith) are **negative PC-relative** → they
resolve **outside** the carved IRAM (the runtime-bound opcode-`0x70`/`0x71` → descriptor table). So:
the ImmLd ops are **registered** (slot + funcVA) but the worker is a **LOG-only stub**.
`[stub edges + registration HIGH/OBSERVED; the opcode→descriptor literal LOW/out-of-carve — the same
limitation the sibling Tensor-Scalar / BatchNorm dispatch surveys note]`

### 7.4 The canonical chain

```text
TensorScalarImmLd{Arith|Bitvec}
  -> [SEQ opcode 0x70/0x71]
  -> [DVE kernel_info slot, registered by MARIANA stub 0x1faa / 0x1fc6]
  -> funcVA 0xca5c / 0xca84  ("S: TensorScalarImmLd{Arith|Bitvec}" STUB worker)
  -> LOG self-name -> retw.n        (NO load body)
```

The op is **decode-validatable** (`is_valid_tsm_immediates_load`, §5) — a compiler can emit it and
it passes ISA check — but the on-device worker performs **no load**. The real load semantics (1..8
elements → DVE storage flops, §5) are the **documented contract**; the maintained-path realisation
was never wired in this build. `[chain HIGH/OBSERVED; the absence of a real load body is the
"not maintained/used" fact]`

---

## 8. The dtype matrix + per-channel range

`tsm_immld_dtypes` (§6) is the inner gate; an **outer** `is_valid_dtype(dtype, AllowFP32R::False)`
gate (in `is_valid_tsm_immediates_load`) independently rejects `FP32R` and the 64-bit types for
**both** opcodes. `num_active_channels ∈ [1,128]` (`check_active_channels`; `POOLING_NUM_CHANNELS
== DVE_NUM_CHANNELS == 128`).

| dtype | ord | ImmLdArith `0x70` | ImmLdBitvec `0x71` | load behaviour |
|---|---|:---:|:---:|---|
| `INT8`  | `0x2` | Y | – | Arith → fp32 |
| `UINT8` | `0x3` | Y | Y | Arith → fp32 ; Bitvec raw 1 B zero-ext |
| `INT16` | `0x4` | Y | – | Arith → fp32 |
| `UINT16`| `0x5` | Y | Y | Arith → fp32 ; Bitvec raw 2 B zero-ext |
| `BF16`  | `0x6` | Y | – | Arith → fp32 |
| `FP16`  | `0x7` | Y | – | Arith → fp32 |
| `INT32` | `0x8` | Y | Y | Arith → fp32 ; Bitvec raw 4 B |
| `UINT32`| `0x9` | Y | Y | Arith → fp32 ; Bitvec raw 4 B |
| `FP32`  | `0xA` | Y | – | Arith (already fp32) |
| `FP32R` | `0xB` | – | – | **rejected** (`is_valid_dtype` `FP32R::False`) |
| `INT64` | `0xC` | – | – | **rejected** (64-bit not in set) |
| `FP8_E3`| `0xD` | Y | – | Arith → fp32 |
| `FP8_E4`| `0xE` | Y | – | Arith → fp32 |
| `FP8_E5`| `0xF` | Y | – | Arith → fp32 |
| `UINT64`| `0x1` | – | – | **rejected** |

Arith count = **12**, Bitvec count = **4** (a subset). `[HIGH/OBSERVED — `tsm_immld_dtypes` +
`is_valid_dtype` verbatim, all four gens; byte-identical]`

> **NOTE — the ImmLd dtype matrix differs from the compute ops' matrix.** Compare
> [`tensor-scalar.md` §6d](tensor-scalar.md): TensorScalar **Bitvec** (`0x53`) is int-only with
> `in == out` identity over `{INT8/16/32, UINT8/16/32}`. ImmLd **Bitvec** (`0x71`) is narrower —
> `{UINT8, UINT16, UINT32, INT32}` (it **drops** `INT8`/`INT16`) because the load is
> raw-zero-extend, not an op-result. And TensorScalar **Arith** accepts any valid dtype incl. the
> compute-result `FP32R` on output; ImmLd **Arith** is the fp-convert input set (`{fp8/fp16/bf16/
> fp32 + int8/16/32 + uint8/16/32}`) with **no** 64-bit and **no** `FP32R`. The `dtype`@32 byte
> means a different thing per opcode on this shared struct. `[HIGH/OBSERVED — the two predicate
> bodies side by side]`

---

## 9. Per-generation presence

| GEN | opcode `0x70`/`0x71` | `S2_BN` | `tsm_immld_dtypes` | `S:…ImmLd*` DVE str | STUB worker | registered? |
|---|---|---|---|---|---|---|
| **SUNDA** (v2) | defined `// n` | 64 B id | 12 / 4 (**no reg-shape**) | (no DVE DEBUG carve) | n/a | (ISA-defined) |
| **CAYMAN** (v3) | defined `// n` | 64 B id | 12 / 4 | DRAM `0x275f`/`0x277a` | `0xc478`/`0xc4a0` | STUB-registered |
| **MARIANA** (v4) | defined `// n` | 64 B id | 12 / 4 | DRAM `0x283f`/`0x285a` | `0xca5c`/`0xca84` | STUB-registered |
| **MARIANA_PLUS** | (mariana hdr) | 64 B id | 12 / 4 | == MARIANA region | (stable) | STUB-registered |
| **MAVERICK** (v5) | defined `// n` | 64 B id | 12 / 4 | DRAM `0x286f`/`0x288a` | `0xca24`/`0xca4c` | STUB-registered |

The opcode values `0x70`/`0x71` + the `// n, ucode/kaenadve exists, not maintained/used` marker +
the `S2_BN` struct (compile-verified) + the `tsm_immld_dtypes` op-set (12 arith / 4 bitvec) are
**byte-identical on all four gens**. The eight self-name strings + the LOG-only stub workers + the
registration stubs are present on **every DVE-equipped gen** (CAYMAN/MARIANA/MAVERICK) — but the
worker is a 40-byte LOG-and-return placeholder on **every** gen (no real load body anywhere).

> **CORRECTION — the only per-gen ImmLd header delta is the SUNDA `shape_from_register` omission.**
> A `diff` of `s2_bn.h` between SUNDA (v2) and MARIANA (v4) is non-empty, but every ImmLd-relevant
> delta is SUNDA *lacking* a cayman+/mariana/maverick addition: the `shape_from_register(start_addr)`
> escape on the element-count predicate (§5.2), the `s2_bn_zgen_restriction` function, and the ZGEN
> backward-compat comments. The `typedef` body, the `tsm_immld_dtypes` op-set, the 1..8 count rule,
> and the forced-zero-immediate gate are **byte-identical** v2→v5. `[HIGH/OBSERVED — `diff` this
> pass]`

> **MAVERICK (v5) interior — header-OBSERVED, interior INFERRED.** Per the
> [generation-grounding policy](../../reference/confidence-model.md), the v5 `0x70`/`0x71` opcode +
> `s2_bn` struct **presence** is OBSERVED (the header ships in `neuron_maverick_arch_isa`, and the
> `0xca24`/`0xca4c` stub VAs + `0x286f`/`0x288a` self-names are carved). The dtype set is
> compile-verified identical. Treat any deeper v5 worker-interior claim as **INFERRED** — the v5
> workers are the same stub template as v3/v4. `[HIGH/OBSERVED header + stub VAs; deeper interior
> INFERRED]`

So: ImmLd is a **universally-present-but-universally-stubbed** op — ISA-defined and
`kernel_info`-registered on all gens, never given a maintained compute body. This is fully
consistent with the `// n, not maintained/used` flag. `[HIGH/OBSERVED]`

---

## 10. The ImmLd → PtrMulti sub-family roll-up

ImmLd (this page) and [`TensorScalarPtrMulti`](ts-ptrmulti.md) are a **load/consume pair**, and the
*whole* sub-family is deprecated/unwired:

| op | name | opcode | struct | role | DVE worker |
|---|---|---|---|---|---|
| `0x70` | `TensorScalarImmLdArith` | `0x70` | `S2_BN` | **LOAD** ≤8 scalars → DVE flops (→ fp32) | stub `0xca5c` |
| `0x71` | `TensorScalarImmLdBitvec` | `0x71` | `S2_BN` | **LOAD** ≤8 scalars → DVE flops (raw bits) | stub `0xca84` |
| `0x4F` | `TensorScalarPtrMultiArith` | `0x4F` | `S4D4_TSM` | **CONSUME**: `dst[i]=src[i] op scalar[i]`, arith | stub `0xcaac` |
| `0x5F` | `TensorScalarPtrMultiBitvec` | `0x5F` | `S4D4_TSM` | **CONSUME**: bitvec | stub `0xcad4` |
| — | `TensorScalarPtrMultiDual{Arith,Bitvec}` | — | `S4D4_TSM` | richer immediate-swap variants ([`ts-ptrmulti.md`](ts-ptrmulti.md)) | (stubs) |

The **key contrast** for a reimplementer: ImmLd's Arith/Bitvec split is a **dtype/convert** split
(`tsm_immld_dtypes`, §6 — `S2_BN` has no AluOp); PtrMulti's Arith/Bitvec split, by contrast, **is**
the `is_general_arith_op` vs `is_bitvec_op` AluOp-set split (PtrMulti *does* carry a single AluOp,
with `op1 = bypass`). The family-wide *"Arith vs Bitvec"* naming is consistent, but realised as a
**dtype gate** on the ImmLd load op and an **AluOp gate** on the PtrMulti compute op. See
[`ts-ptrmulti.md`](ts-ptrmulti.md) for the consumer and [`alu-op-matrix.md`](alu-op-matrix.md) for
the full ALU-op accept-set / dtype matrix that the compute side gates against. `[HIGH/OBSERVED —
the ImmLd gate is `tsm_immld_dtypes`; the PtrMulti gate is `is_general_*_op` per `s4d4_tsm.h`; the
parallel is structural]`

---

## 11. Confidence ledger

**HIGH / OBSERVED (disassembly, byte-read, header-read, or compile-verify this pass):**

* Opcodes `TENSOR_SCALAR_IMM_LD_ARITH = 0x70`, `…_BITVEC = 0x71`, both
  `// n, ucode/kaenadve exists, not maintained/used`, byte-identical all four gens (`common.h`).
* `struct2opcode`: `S2_BN` binds **exactly** `{BatchNormParamLoad, MatchValueLoad, ImmLdArith,
  ImmLdBitvec}`; `NonzeroWithCount` is **not** in `S2_BN` (premise corrected — it is
  `S3D3_NONZERO_WITH_COUNT`).
* `S2_BN` SRC-only, `sizeof == 64`, `header`@0 / `events`@4 / `reserved0`@12 / `src TENSOR2D`@16 /
  `imm1`@28 / `dtype`@32 / `num_active`@34 / `imm0_ptr`@44 / `imm0_src`@48 — compile-verified
  byte-identical sunda/cayman/mariana/maverick (`gcc` `offsetof`/`sizeof` this pass; matches
  [`batchnorm-paramload.md`](batchnorm-paramload.md) / [`search-cluster.md`](search-cluster.md)).
* Semantics: *"load up to 8 elements per channel into DVE storage flops … for use as the immediate
  values for the following TensorScalarPtrMulti instruction"* (`s2_bn.h:72–80` verbatim) — a
  load/setup op, no dst, no AluOp, no compute. Element count 1..8
  (`has_tsm_immediates_load_src_element_cnt`); `imm1`/`imm0_ptr`/`imm0_src` forced zero
  (`has_s2_bn_zero_immediates`).
* The Arith-vs-Bitvec split = `tsm_immld_dtypes` (verbatim, byte-exact): Arith `0x70` = 12 dtypes
  `{fp32/fp16/bf16/fp8_e3/e4/e5/u32/u16/u8/i32/i16/i8}` → convert-to-fp32; Bitvec `0x71` = 4 dtypes
  `{u32/u16/u8/i32}` → raw-bits-zero-extended; Bitvec ⊂ Arith. **Not** an AluOp split (`S2_BN` has
  no AluOp).
* The dtype matrix (ordinals from `common.h`); the outer `is_valid_dtype(FP32R::False)` gate;
  channels 1..128.
* Eight `"S: TensorScalarImmLd{Arith,Bitvec}"` self-name strings (`grep -abo`, 8 hits); the MARIANA
  DRAM byte-read at `0x283f`/`0x285a`.
* The 40-byte LOG-only stub workers (`entry a1,32`; log self-name; `retw.n` — no compute) at MARIANA
  `0xca5c`/`0xca84`, the contiguous 4-stub block with the PtrMulti twins (`…/0xcaac/0xcad4`); the
  registration stubs (`0x1faa`→`0xca5c`, `0x1fc6`→`0xca84`; `call8 0x9920`); the entry-frame census
  (137 `a1,32` stub vs 18 `a1,80` maintained) — all disassembled natively this pass.
* The ImmLd→PtrMulti pairing (`s4d4_tsm.h:23–49` verbatim: *"Preload immediate using
  TensorScalarImmLd{Arith,Bitvec}"*; the `for i in range(W)` broadcast loop). PtrMulti opcodes
  `0x4F`/`0x5F`, same `// n` marker.
* Per-gen: opcode + struct + dtype-set byte-identical all four; STUB-registered on every DVE gen;
  SUNDA-only `shape_from_register` / `s2_bn_zgen_restriction` omission. Container sha256 `b7c67e89…`.

**MED / INFERRED:**

* Bitvec drops `INT8`/`INT16` *because* raw signed-narrow has no defined zero-extension — INFERRED
  from the verbatim dtype lists + the "zero extended" header text (the lists are OBSERVED; the
  *why* is the inference).
* The per-channel DVE flop addressing within the storage-flop bank (the "into storage flops" is
  OBSERVED; the exact slot map is not enumerated by the header).
* That the on-device stub performing only LOG+`retw.n` means the load is never executed in this
  build — INFERRED-HIGH from the 40-byte body having no load/compute instruction + the `// n`
  marker.

**LOW / UNRECOVERED:**

* The opcode-`0x70`/`0x71` → funcVA descriptor bytes (the registration-stub `l32r` literals
  `0xfffc811c`/`0xfffc369c` are negative PC-relative → resolve out-of-carve; runtime-bound — the
  same limitation the sibling dispatch surveys note).
* The exact byte-content of the stub's `const16 a2, 0x2880` data write (the stub does nothing
  meaningful with it before `retw.n`); the FLIX literal-pool `.byte 0x2f` lead-byte desync in the
  stub body is **not** reported as a real instruction — the `entry`/LOG/`retw.n` skeleton is
  byte-clean and unambiguous.

---

## See also

* [TensorScalarPtrMulti (Arith/Bitvec)](ts-ptrmulti.md) — the **consumer**: applies one AluOp
  per-W-slice (`dst[i] = src[i] op scalar[i]`) using the scalars ImmLd preloaded; its Arith/Bitvec
  split **is** the AluOp-set split (the inverse of ImmLd's dtype split).
* [The TensorScalar\* family map](tensor-scalar.md) — the maintained `0x43`/`0x53` compute ops, the
  deprecated `0x44`/`0x54` Ptr ops, the splat-then-two-AluOp datapath, the `S3D3_TS` struct.
* [BatchNormalize — ParamLoad](batchnorm-paramload.md) — the `S2_BN` co-resident that *does* use the
  `imm1`/`imm0_ptr` slots (`N` + `batch_mean`); the canonical "load N values into engine flops" op.
* [DVE Search/Select Cluster](search-cluster.md) — `MatchValueLoad` (`0x6d`), the other `S2_BN`
  loader; the structural analog of ImmLd (load 8 keys `mv[0..7]` for a later consume op).
* [The ALU-Op Datapath + Dtype Matrix](alu-op-matrix.md) — the AluOp accept set / dtype contract the
  PtrMulti compute side gates against.
* [The Confidence & Walls model](../../reference/confidence-model.md) — what the tags mean.
