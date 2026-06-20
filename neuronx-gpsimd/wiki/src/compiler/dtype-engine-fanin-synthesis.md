# Dtype/Engine/Gen Fan-In + CC-Lane Synthesis

> **Scope — the compiler-lane capstone.** This page closes the GPSIMD compiler seam
> with two consolidations. **(A)** The **dtype / engine / gen fan-in** — the mechanism by
> which **one** penguin BIR class (`InstTensorTensor`, `InstTensorScalar`,
> `InstTensorReduce`, `InstActivation`, `InstCopyPredicated`, `InstMatmultMx`) resolves
> to **many** device opcodes along three orthogonal axes (dtype, engine, generation),
> with the `INT_WIDE 0xf3/0xf4` (MAVERICK-only), the SUNDA BF16 cluster `0x8a–0x8f`, the
> TensorTensor/TensorScalar **GpSimd↔Vector** engine resolution, and the v4/v5 MX fold as
> the worked cases. **(B)** The **CC-lane synthesis** — the complete consolidated chain
> **NKI/HLO → `emit_*` → BIR `Inst*` → SundaISel → SundaISAInst → device opcode → engine →
> ISS value model**, unifying the ten committed `compiler/` pages, ending in the **final
> coverage statement** (how many `emit_*` / BIR classes map to confirmed opcodes, and the
> residual firmware-internal opcodes with **no** BIR producer).
>
> **Audience.** A senior C++/LLVM engineer rebuilding a Vision-Q7-compatible GPSIMD engine
> who needs the *single* mental model that ties the host compiler decision to the device
> ISA byte and the value oracle — and the honest enumeration of what the compiler never
> emits.

This is a **synthesis** page: it carries the consolidated facts from
[compiler-map](compiler-map.md), [sundaisel](sundaisel.md), [mx-path](mx-path.md),
[mx-device-bodies](mx-device-bodies.md), [tiling-memory-scheduling](tiling-memory-scheduling.md),
[bir-inst-roster](bir-inst-roster.md), [fused-cc-lowering](fused-cc-lowering.md),
[opt-sync-insertion](opt-sync-insertion.md), [collective-loadtime-rewrite](collective-loadtime-rewrite.md),
and [nki-frontend](nki-frontend.md), re-grounding their key anchors and presenting the
engine-fan-in reconciliation as the authoritative unified view. There is **one** device
opcode catalog — the [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md)
— and every numeric here is reconciled against it.

> **GUARD.** `sunda` / `tonga` / `cayman` / `mariana` / `maverick` are the per-generation
> **codegen-target / arch-ISA** axis (sunda = CoreV2 / Trn1–Inf2 floor; tonga = Inf1
> legacy; cayman = CoreV3 / Trn2; mariana = CoreV4 / Trn3; maverick = CoreV5 / NCFW),
> **not** new silicon. The 110-class libBIR `Inst*` roster is gen-**agnostic** (one IR for
> all targets); the per-gen opcode split (the BF16 retire, the `INT_WIDE` add, the v4/v5 MX
> fold) is applied at SundaISel / walrus over the per-gen arch-isa enum, **never** inferred
> as a silicon fact.

**Confidence convention.** Every claim is tagged `HIGH/MED/LOW` × `OBSERVED` (read from a
shipped artifact / symbol / string / header this pass) / `INFERRED` (reasoned over
OBSERVED) / `CARRIED` (consolidated from a cited sibling page at its stated confidence).
The two host corpora and the device contract:

```
CC  = neuronx-cc/extracted/neuronx_cc-2.24.5133.0+58f8de22-cp311-cp311-linux_x86_64/neuronxcc/
NKI = neuronx-misc/extracted/nki-0.3.0+23928721754.g18aa1271-cp311-cp311-linux_x86_64/nki/
ISA = aws-neuronx-gpsimd-customop-lib_0.21.2.0  (the four per-gen arch-isa headers)
```

cp310/cp311/cp312 wheels are byte-identical in the Python layer; the Cython `.so` are
**not stripped** (class/method qualnames survive in `.rodata`). The `extracted/` trees are
gitignored — reach them with `fd --no-ignore` or an absolute path.

---

## A. The fan-in — one BIR class → many device opcodes

### A.1 The mechanism — validator-gated dispatch, not a runtime dtype field `[HIGH/OBSERVED]`

The single most important structural fact about the GPSIMD compiler seam: **the fan-in is
a validator-gated `{dtype, engine, gen}` dispatch over a generic BIR class — not a
polymorphic opcode carrying a dtype field that selects a datapath at run time.** For each
specialised dtype there is a **dedicated opcode** whose **validator hard-pins** the dtype;
SundaISel (or, for the engine axis, the NKI frontend) chooses *which* opcode by matching
the BIR node's `(dtype, op, flags, gen)` tuple to the validator that accepts it.

The validator field names were read byte-exact this pass from the TPB opcode validator
surface `CC/isa_tpb/sunda/neuron_isa_tpb_pybind.cpython-311-*.so` (strings):

| validator field | gate | selects |
|---|---|---|
| `bf16_opcode` | "this opcode *is* a BF16 fast-path opcode" | the dedicated BF16 opcode class |
| `tensor_tensor_bf16_op` | the `AluOp`↔opcode match for the BF16 TT trio | `0x8a`/`0x8b`/`0x8f` by op |
| `tensor_bf16_in_dtype` | both inputs `== BFLOAT16` (the TT path) | the packed-BF16 TT pin |
| `tensor_reduce_bf16_in_dtype` | input `== BFLOAT16` (the reduce path) | `0x8c`/`0x8d` |
| `valid_int_dtype_datapath` | the `{I/U 8/16/32}` integer-datapath gate | `INT_WIDE` `0xf3`/`0xf4` |
| `same_inout_dtype` | cast vs no-cast | `0x46` COPY vs `0x47` CAST |
| `valid_reversed` | the reverse-operand variant gate | the TensorScalar `getReverse0/1` forms |
| `tensor_tensor_scan_opcode` | the SCAN flag | `0xe5` TENSOR_TENSOR_SCAN_ARITH |
| `scalar_tensor_tensor_opcode` | the 3-operand STT selector | `0x9d`/`0x9e` |
| `activate_opcode` | the activation-opcode gate | `0x21` vs `0x25`/`0x26` |
| `copy_cast_predicated_opcode` | the CopyPredicated cast-form gate | `0x99` CAST_PREDICATED |
| `copy_predicated_scalar_opcode` | the CopyPredicated scalar-form gate | `0xe8` COPY_PREDICATED_SCALAR |

The dtype enum these predicates read (`CC/isa_tpb/python/enum_mapping.cpython-311-*.so`,
strings this pass): `BFLOAT16`, `FP32R`, `INT32`, `UINT32` — the four the CC lane routes
on. `[HIGH/OBSERVED — strings on both pybinds.]`

> **NOTE — the opcode byte is engine-invariant.** Across the entire fan-in, the *engine*
> (POOL / DVE / ACT) is a **parallel** `NeuronEngine` field on the Tonga inst, not part of
> the opcode byte. `targets/Opcodes.so` defines `ALUOpcode` with **two distinct property
> members** — `ALUOpcode.opcode` (the instruction-class byte, e.g. `0x41`) and
> `ALUOpcode.cc_opcode` (the compute-cluster math-op *within* the class). The base
> `TensorTensorOpGen.get_isa_opcode` even raises *"ISA opcode generation not implemented
> for TensorTensorOpGen"* — the opcode comes from `ALUOpcode`, the engine from the
> separate field. So `0x41` is fixed; POOL(GpSimd) vs DVE(Vector) is structural, not an
> opcode split. `[HIGH/OBSERVED — sundaisel §3.3.]`

### A.2 The dtype axis — the BF16 cluster and the `INT_WIDE` pair

These are the two ends of the opcode roster: the **oldest** generation's dtype fast path
(SUNDA BF16, retired) and the **newest** generation's width capability (MAVERICK
`INT_WIDE`, added). Both are reached **only** as lowering targets of a generic BIR class —
**no `emit_` names them, no named BIR class exists** for them. The device-side decode is
the ground truth in [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md);
the compiler-side fan-in is below.

#### A.2.1 The SUNDA BF16 cluster `0x8a/0x8b/0x8f` TT + `0x8c/0x8d` reduce — SUNDA-only `[HIGH/CARRIED]`

One `InstTensorTensor` with **both inputs `BFLOAT16`** → on the **SUNDA** target SundaISel
matches `tensor_bf16_in_dtype` + `bf16_opcode` + `tensor_tensor_bf16_op` and emits a
dedicated 2×-throughput packed-BF16 opcode instead of the generic `0x41`:

| op | enum identifier | struct | `AluOp` pin | semantics | engine |
|---|---|---|---|---|---|
| `0x8a` | `TENSOR_TENSOR_ADD_BF16` | `S3S3D3_TT` | `Add 0x04` | 2× packed-BF16 TT add | POOL `[MED]` |
| `0x8b` | `TENSOR_TENSOR_MULT_BF16` | `S3S3D3_TT` | `Mult 0x06` | 2× packed-BF16 TT mult | POOL `[MED]` |
| `0x8f` | `TENSOR_TENSOR_SUB_BF16` | `S3S3D3_TT` | `Subtract 0x05` | 2× packed-BF16 TT sub | POOL `[MED]` |
| `0x8c` | `TENSOR_REDUCE_ADD_BF16` | `S4D4_TR` | reduce-Sum | 2× packed-BF16 reduce-sum | POOL `[MED]` |
| `0x8d` | `TENSOR_REDUCE_MAX_BF16` | `S4D4_TR` | reduce-Max | 2× packed-BF16 reduce-max | POOL `[MED]` |

The structural tells (CARRIED, [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) §4):

- **The opcode encodes the op.** Unlike the generic `0x41` (where the `AluOp` field
  *selects* the op), here the opcode *is* the op and the `AluOp` field is **pinned** to
  match it (`AddBf16↔Add`, `MultBf16↔Mult`, `SubBf16↔Subtract`). So the dtype-axis fan-in
  is `dtype(BF16) × op(add/mult/sub)` → one of three dedicated opcodes.
- **The 2× throughput packs two BF16 per 32-bit lane.** The validator reads the src
  patterns as `Dtype::UINT32` and requires `num_elem[0] % 2 == 0`, `step_elem[0] == 1`
  (the packed-pair access pattern). Inputs are hard-pinned `BFLOAT16 (0x6)`; the output is
  free (commonly cast up, including to `FP32R`).
- **`0x8e` is NOT a BF16 op** — it is `BATCH_NORM_PARAM_LOAD2`, the one byte inside the
  `0x8a–0x8f` span that is a *different*, all-gen-maintained instruction. The cluster is
  **five** ops, not six.

> **GOTCHA — the BF16 fast path permits PSUM; `INT_WIDE` forbids it.** The BF16 TT/reduce
> patterns are validated `AllowedInPSUM::True`, so the SUNDA BF16 fast path can read/write
> PSUM. The MAVERICK `INT_WIDE` ops are the opposite — strictly `SBUF`-only
> (`AllowedInPSUM::False`). `[HIGH/CARRIED — intwide-bf16-extremes §4.1/§3.]`

#### A.2.2 The MAVERICK `INT_WIDE` pair `0xf3` TT (POOL) / `0xf4` TS (DVE) — MAVERICK-only `[HIGH/CARRIED · INFERRED-MED engine]`

One `InstTensorTensor` / `InstTensorScalar` whose inputs are 32-bit ints **and whose
result must capture the high half** → on the **MAVERICK** target SundaISel matches
`valid_int_dtype_datapath` + the wide-result constraint and emits a dedicated
dual-destination wide-integer opcode (the last two real opcodes before `INVALID 0xff`):

| op | enum identifier | struct (`sizeof==64`) | engine | semantics |
|---|---|---|---|---|
| `0xf3` | `TENSOR_TENSOR_INT_WIDE` | `S2S2D2D2_TT` (sole member) | POOL `[MED]` | `full=(i64)s0 op (i64)s1`; `dst0=low32`, `dst1=high32/carry` |
| `0xf4` | `TENSOR_SCALAR_INT_WIDE` | `S2D2D2_TS_WIDE` (sole member) | DVE `[MED]` | `full=(i64)s op (i64)scalar`; `dst0=low32`, `dst1=high32/carry` |

The fan-in facts (CARRIED, [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) §3/§5):

- **Inputs are `is_valid_int_dtype_datapath` = exactly `{INT8, UINT8, INT16, UINT16,
  INT32, UINT32}`** — *not* `INT64`/`UINT64`. The 64-bit-ness is **output width** (the
  dual 32-bit `dst0`/`dst1`), distinct from the native-`INT64` dtype path, which the
  regular `0x41` TT takes via a single even-register-pair destination.
- **The 8-op `AluOp` set:** `AddInt 0xC4` / `SubtractInt 0xC6` / `MultInt 0xC5` /
  `MultUint 0xDB` + 4 shifts (`0x02`/`0x03`/`0x10`/`0x11`); the `0xCx` ops carry the
  `bit[7:6]==0x3 → integer engine` marker. Per-op signedness: `Add`/`Sub`/`MultUint`
  unsigned, `MultInt` signed, shift-amount unsigned.
- **The 2-D pattern is forced.** `S2S2D2D2_TT` uses four `MEM_PATTERN2D` (12 B each = 48 B)
  to fit a *second destination* into the 64-byte frame; the 2-D demotion (vs the 3-D
  `S3S3D3_TT`) is the budget cost of carrying the dual dest.
- **Engine:** the `0xf3` validator gates `POOLING_NUM_CHANNELS` (POOL); `0xf4` gates
  `DVE_NUM_CHANNELS` (DVE). Both constants are numerically `128`, so the discriminating
  signal is the validator's *choice of constant name*, plus the corpus-wide "TT is POOL /
  TS can be DVE" convention — hence the engine attribution is **INFERRED-MED**, not HIGH.

> **CORRECTION — the `0x8a–0x8f` task framing is a byte *span*, not the op set.** The
> SUNDA BF16 cluster is the **five** ops `{0x8a, 0x8b, 0x8c, 0x8d, 0x8f}`; `0x8e` inside
> that span is `BATCH_NORM_PARAM_LOAD2`, maintained on all four gens. Treat the cluster as
> five, and never as a contiguous six-byte range. `[HIGH/CARRIED — intwide-bf16-extremes §1.]`

#### A.2.3 The dtype decision table (TT/reduce) `[HIGH/INFERRED from the validators]`

The unified dtype × gen routing of one `InstTensorTensor` / `InstTensorReduce`:

| BIR node + dtype | SUNDA (NC-v2) | CAYMAN+ (NC-v3/4) | MAVERICK (NC-v5) |
|---|---|---|---|
| `InstTensorTensor` int32 add/sub/mul, no PSUM | `0x41` (GpSimd, §A.3) | `0x41` (GpSimd) | `0x41` (GpSimd) |
| `InstTensorTensor` int32 wide-result (lo+hi dst) | — (unavailable) | — (unavailable) | **`0xf3`** INT_WIDE (POOL) |
| `InstTensorTensor` fp32 add/sub/mul | `0x41` (DVE) | `0x41` (DVE) | `0x41` (DVE) |
| `InstTensorTensor` **bf16** add/mult/sub | **`0x8a`/`0x8b`/`0x8f`** (2× pack) | `0x41` + bf16 dtype | `0x41` + bf16 dtype |
| `InstTensorTensor` bitwise/logical | `0x51` BITVEC | `0x51` BITVEC | `0x51` BITVEC |
| `InstTensorTensor` scan flag | `0xe5` SCAN | `0xe5` SCAN | `0xe5` SCAN |
| `InstTensorReduce` **bf16** add/max | **`0x8c`/`0x8d`** (2× pack) | `0x42` + bf16 dtype | `0x42` + bf16 dtype |
| `InstTensorScalar` int32 wide-result | — | — | **`0xf4`** INT_WIDE (DVE) |

The retire mechanism (specialisation → generalisation): at CAYMAN+ the BF16 opcodes are
removed from the enum and the shared host structs shed exactly those members
(`S3S3D3_TT` 8→5, `S4D4_TR` 13→11), leaving the **generic** `InstTensorTensor` /
`InstTensorReduce` to handle BF16 via the ordinary dtype dispatch. The "folded into the
generalised dtype dispatch" mechanism is INFERRED-MED from the struct-membership delta +
the dtype-enum dispatch (not byte-traced through the cayman+ generic op). `[retire facts
HIGH/CARRIED; the fold mechanism MED/INFERRED — intwide-bf16-extremes §7.]`

### A.3 The engine axis — `GpSimd ↔ Vector`, same opcode `[HIGH/OBSERVED]`

`emit_tensor_tensor_arith` (`0x41`) and `emit_tensor_scalar_arith` (`0x43`) carry an
`engine` kwarg; the **same opcode** runs on GpSimd **or** Vector (or, for `0x43`, Scalar).
The engine selects *which hardware datapath decodes the byte* (the POOL Q7 FLIX kernel vs
the hardwired DVE/ACT datapath), not the byte. This resolves the ledger's "POOL/DVE" hedge
for these ops as **"POOL when routed there, else DVE/ACT" — a compiler-policy axis, not an
opcode split.**

The resolution rule is a **frontend dtype/op/buffer predicate**, re-read byte-exact this
pass from `NKI/isa/tensor_ops.py` (the `0x41` route) — there is **no shape- or cost-driven
ISel switch**:

```c
/* nki/isa/tensor_ops.py : tensor_tensor(dst, d1, d2, op, engine=unknown)
 * The ONLY auto-GpSimd trigger in the whole arith surface.  OBSERVED byte-exact. */
Engine resolve_tt_engine(Tensor dst, Tensor d1, Tensor d2, AluOp op, Engine engine) {
    if (is_bitvec_op(op))   return /*backend*/ Vector;       /* 0x51 TENSOR_TENSOR_BITVEC */
    if (op == power)        return Gpsimd;                   /* nl.power: ALWAYS GpSimd, fp32-cast */

    Engine resolved = engine;                                /* caller kwarg wins */
    static const AluOp GPSIMD_ARITH[] = { add, subtract, multiply };
    if (resolved == Engine_unknown
        && op   in GPSIMD_ARITH
        && dst.dtype in {int32, uint32}
        && d1.dtype  in {int32, uint32}
        && d2.dtype  in {int32, uint32}
        && !is_psum(dst.buffer) && !is_psum(d1.buffer) && !is_psum(d2.buffer))
        resolved = Engine_gpsimd;                            /* -> 0x41 on POOL */
    /* else: stays Vector -> 0x41 on DVE (fp32-cast path) */
    return resolved;                                         /* engine field; opcode byte UNCHANGED */
}
```

The decision in three lines: **(int32/uint32) AND (engine unspecified) AND (no PSUM
operand) → GpSimd (`0x41` POOL); else → Vector (`0x41` DVE); an explicit `engine=` kwarg
overrides both.** The engine enum (`NKI/isa/enums.py`, OBSERVED): `tensor=1, scalar=2,
gpsimd=3, dma=4, vector=5, sync=6, unknown=0`.

The **`tensor_scalar` GpSimd route is narrower** — there is no auto-GpSimd; the `engine`
kwarg passes through verbatim, and the docstring restricts the GpSimd option to *"only
allowed for rsqrt"* (`tensor_ops.py:242`). `[HIGH/OBSERVED — sundaisel §3.1/§3.2.]`

> **QUIRK — the docstring claims shape-based selection; the code does not.** The `engine`
> kwarg doc reads *"unknown (default, let compiler select best engine based on the input
> tile shape)"*, but the only `unknown`→non-unknown resolution in the body is the dtype/op/
> buffer predicate above — no tile shape is read. The "best engine based on shape" wording
> is aspirational in this NKI version. `[HIGH/OBSERVED.]`

> **CORRECTION — the "home-class opcode" is NOT the engine.** A reimplementer reading the
> ledger's engine column for a POOL-home-class opcode must not assume the op *runs* on the
> GpSimd Q7 cores. **The POOL home-class opcodes `0x41`/`0x42`/`0x43`/`0x46`/`0x48` (and
> the batch-norm cluster `0x61`/`0x62`/`0x65`/`0x82`/`0x94`) RUN ON DVE for fp data** per
> the SundaISel fp-routing rule — the engine is the parallel field, the opcode byte is
> engine-invariant. The batch-norm family is unambiguously **DVE** at the device
> (`engine_idx=3`), not GPSIMD POOL, even though SundaISel's BN transforms are
> GPSIMD-*adjacent* macros. The **only** place the GpSimd Q7 cores own arithmetic is the
> int32/uint32 add/sub/mul **no-PSUM** route on `0x41`. `[HIGH/OBSERVED — sundaisel §2.1
> CORRECTION + §3; the int32 route OBSERVED tensor_ops.py.]`

> **CORRECTION — `quantize_mx`, `tensor_scalar_cache_reduce`, `tensor_scalar_cache_cumulative`
> declare Vector at the irbuilder.** An earlier compiler-seam pass tabulated these as
> `gpsimd`. The `_nki_irbuilder.so` docstrings read verbatim: `quantize_mx` *"…using
> **Vector Engine**"*, `tensor_scalar_cache_reduce` *"…reduce along free dimensions using
> **Vector Engine**"*, `tensor_scalar_cache_cumulative` *"…cumulative reduction using
> **Vector Engine**."* The opcodes (`0xe3`/`0x9a`/`0xe6`) are unchanged and
> ledger-confirmed; only the declared engine differs. (For contrast, `tensor_scalar_addr`
> *does* declare `Gpsimd` — *"…64-bit address calculations using Gpsimd Engine"* — for the
> `0x74` address-staging op.) `[HIGH/OBSERVED — irbuilder docstrings, compiler-map §4.2.]`

### A.4 The engine axis meets the value model — the deepest tie `[HIGH · OBSERVED + CARRIED]`

When the engine fan-in routes int32 add/sub/mul to **GpSimd (`0x41` POOL)**, the device
runs a Q7 FLIX kernel whose per-lane body is the libfiss xdref integer leaf
(`module__xdref_add/sub/mul`, 32-bit wrap, no saturation). The NKI numpy reference sim
makes the consequence visible (OBSERVED, `NKI/backends/simulator/tensor_ops.py`):

```python
def compute(d1, d2):
    if engine == engine_enum.gpsimd:
        return np_op(d1, d2)                       # NATIVE int — 32-bit wrap, NO fp cast / mask
    d1_fp32 = d1 if d1.dtype == np.float32 else d1.astype(np.float32)
    d2_fp32 = d2 if d2.dtype == np.float32 else d2.astype(np.float32)
    return np_op(d1_fp32, d2_fp32)                 # Vector path casts to fp32 first (the DVE datapath)
```

The GpSimd path is `np_op(d1, d2)` on the **native int dtype** — wraps mod 2³² with no
mask — exactly the device POOL `0x41` integer datapath (the firmware `0xC4..0xE1`
integer-engine band where `bit[7:6]==0x3` selects the int engine, no saturation). So
**NKI-sim native-int wrap == device int-engine wrap, bit-for-bit.** The xdref oracle is
closed at 864/864 leaves, so the value model under the POOL int route is fully enumerated.
This is the **only** place the host engine decision and the device value oracle meet, and
they are consistent — the engine fan-in is the host-side *reason* the device runs the
xdref int datapath. `[engine rule HIGH/OBSERVED; xdref identity HIGH/CARRIED.]`

> **QUIRK — `nl.power` casts even on GpSimd.** `power` is *always* GpSimd (§A.3) but its
> sim oracle always casts to fp32. So "GpSimd == native int" is the rule for the int32
> **arith** route only, not for every op that lands on GpSimd. `[HIGH/OBSERVED.]`

### A.5 The gen axis — per-gen opcode selection and the v4/v5 MX fold

The same `InstX` produces a **different opcode** on a different gen target. The two
bracket-extreme dtype cases (§A.2) are the loudest; the MX path is the gen fan-in on the
PE/MX leg.

#### A.5.1 The v4/v5 MX fold `[HIGH/CARRIED]`

`InstMatmultMx` is **one** BIR class; the opcode it lowers to is gen-routed:

| gen | ops | structs | scale carrier |
|---|---|---|---|
| **v4 (MARIANA / CoreV4)** | `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` | `SMX1_LW_STRUCT` / `SMX1D3_MM_STRUCT` (both 64 B) | `MXMEM_PATTERN1D` with **out-of-band** `scale_addr` (E8M0 per-block) |
| **v5 (MAVERICK / CoreV5)** | folds into `0x01 LDWEIGHTS` / `0x02 MATMUL` | `MEM_PATTERN3D` union | the `MXTENSOR_V2` member (`ADDR4MARKER = 0x01` LSB) + `MX_PERF_MODE {QUAD_ROW/OCT_ROW}` 4×/8× row pumping |

The forward pack `InstQuantizeMx → 0xE3 QUANTIZE_MX` runs on **DVE**; the inverse standalone
dequant `0x7B TENSOR_DEQUANTIZE` runs on **POOL** and has **no forward BIR producer** (the
MX matmul/dequant kernel consumes the packed data internally — §C.3a). The "`MATMUL 0x02
(mx)`" of the host-abstract view is correct for v5; the v4 device truth is the dedicated
`0x09`/`0x0A` pair. `[HIGH/CARRIED — mx-path §3, mx-device-bodies, pe-matmul §2/§7.]`

> **CORRECTION — the forward `QUANTIZE_MX` gen floor is NC-v4 (MARIANA); the `nc==V5` is a
> maverick-header-LOCAL gate, not a global floor.** The per-arch `s3dmx1_quant_valid_nc`
> gate is the deciding artifact, and it resolves the floor that
> [mx-path](mx-path.md) §6.3 left flagged. The `s3dmx1_quant.h` header is **present in
> mariana(v4) and maverick(v5), ABSENT in sunda and cayman(v3)** (byte-checked: 6 header
> files each in mariana/maverick, 0 in sunda/cayman). Each present header carries a
> generation-LOCAL gate: `mariana/.../s3dmx1_quant.h:142` reads `nc == V4`;
> `maverick/.../s3dmx1_quant.h:103` reads `nc == V5`. So `nc == V5` is the gate **inside the
> MAVERICK header** (each header pins its own generation), NOT a claim that `0xE3` first
> ships on v5. The enum-presence column (`--YYY`) and the host `.pyi` ("v4 and newer") agree
> with the header presence: the forward `QUANTIZE_MX` gen floor is **NC-v4 (MARIANA)**. The
> opcode number and engine (DVE) were never in dispute. `[HIGH/OBSERVED — mx-device-bodies
> per-arch header presence + the two `nc==Vn` gate lines; downgrades mx-path §6.3's flagged
> divergence to a resolved CORRECTION.]`

#### A.5.2 The collective gen fan-in (the per-arch HAL leg) `[HIGH/CARRIED]`

`InstCollective` is one BIR class → PSEUDO `0xC7`/`0xC8`/`0xD9` → the NRT
SELECT→COMPOSE→EMIT pipeline picks the algorithm engine (ring / kangaring / rdh / hier /
mesh) per `(op, size, dtype, topology, gen)`. The per-gen split (SUNDA-no-`0xBF`;
CAYMAN ≡ MARIANA io_d2d; MAVERICK UCIe re-IP) is applied at the per-arch HAL/topology
layer, **not** the BIR — even the collective lowering is a gen fan-in over one BIR class.
`[CARRIED — collective-loadtime-rewrite.]`

### A.6 The flag-routed fan-ins — the same mechanism by flag `[HIGH]`

The validator-gated mechanism is not unique to dtype/engine/gen — it also routes by
**flag**, on the same generic-class + per-opcode-validator design:

- `InstTensorTensor` SCAN flag → `tensor_tensor_scan_opcode` → `0xe5`
  TENSOR_TENSOR_SCAN_ARITH (vs `0x41` plain).
- `InstAbstractCopy` / `InstTensorCopy`: `same_inout_dtype` true → `0x46` COPY; false
  (cast) → `0x47` CAST.
- `InstCopyPredicated` four forms: plain `0x72` / `copy_cast_predicated_opcode` → `0x99`
  CAST_PREDICATED / reduce → `0xea` SELECT_REDUCE / `copy_predicated_scalar_opcode` →
  `0xe8` COPY_PREDICATED_SCALAR. One BIR class, opcode by flag.
- `InstActivation` `activate_opcode` → `0x21` ACTIVATE, with the multipass/dtype flag
  reaching ACTIVATE2 `0x25` / ACTIVATE_MULTIPASS `0x26` as a fan-in target — no distinct
  BIR class.
- `InstTensorScalar` `valid_reversed` → the reverse-operand variants (`getReverse0/1`).

So the fan-in is a **uniform design**: a generic BIR class + a family of per-opcode
validators; the lowering selects the opcode whose validator the node satisfies, along the
relevant axis. `[HIGH — validator names OBSERVED this pass; opcode targets CARRIED
bir-inst-roster §3.5.]`

### A.7 The "GPSIMD-is-not-the-float-hotpath" keystone `[HIGH/CARRIED]`

The engine fan-in's most important *negative* result, consolidated from the
[fused-cc-lowering](fused-cc-lowering.md) per-keystone engine-count tally: across the four
production keystones — RmsNorm, Softmax, AllReduce, MX-MoE — the count of float ops on the
GpSimd Q7 cores is **zero**. Every float compute op runs on **ACT / DVE / PE**; the only
GpSimd appearance is the *optional* `0xBF SB2SB_COLLECTIVE` LNC2 partial-sum hop (a
data-movement extended instruction, not float arithmetic). A "GPSIMD collective" therefore
runs **no** GpSimd float math at all. This is the host-side corollary of the device
keystone (cannot reach PSUM; not the float hot-path — see
[keystone-facts](../orientation/keystone-facts.md) K2): the int32-no-PSUM `0x41` route
(§A.3) is the *only* arithmetic the literal Q7 cores own, and the keystones do not exercise
it. `[HIGH/CARRIED — fused-cc-lowering engine tally; keystone-facts K2.]`

---

## B. The consolidated CC-lane pipeline

### B.1 The 7-hop chain — every hop sourced `[HIGH/OBSERVED]`

The complete GPSIMD compiler lane, from either frontend to the value oracle. Both
frontends (NKI direct `emit_`, and XLA-HLO via `hlo2penguin`) converge on the **same**
penguin BIR layer; there is **no second codegen** (the PJRT plugin shells out to
`neuronx-cc compile --framework=XLA`). So this chain is the **complete** GPSIMD op surface
for both paths.

```
  HOP 0   FRONTEND (two lanes)
            nki.isa.<op> ──► emit_<op>            (62 emit_, plain py, NKI/backends/mlir_tracer/isa_emit.py)
            XLA-HLO ──► hlo2penguin                (CC/starfish/bin/hlo2penguin)
            raw-byte escape: n_bytes / extended_inst ──► 0xF0 EXTENDED_INST  (InstInlineASMBytes)
              │
              ▼
  HOP 1   PENGUIN BIR (the IR pivot — the one vocabulary both frontends target)
            libBIR.so : 110 concrete Inst* classes ; JSON op-tag == class basename minus "Inst"
              │
              ▼
  HOP 2   SundaISel  (the rewrite + the §A FAN-IN)
            34 transformT*Operator (canonicalize the BIR-DAG)
            + 21 *CodeGen (emit the ISA inst)
            + the dtype/engine/gen fan-in:
                dtype : bf16_opcode / valid_int_dtype_datapath / same_inout_dtype validators
                engine: int32-no-PSUM -> GpSimd ; PSUM-or-fp -> Vector  (frontend predicate)
                gen   : per-gen arch-isa enum (BF16 retire / INT_WIDE add / MX v4->v5 fold)
                macro : AllReduce/RmsNorm/Softmax/MX-MoE expand to a micro-schedule
              │
              ▼
  HOP 3   SUNDA ISA
            SundaISAInst.<Name>Op  (33 *.Op qualnames)
              │
              ▼
  HOP 4   DESCRIPTOR EMIT
            libwalrus CoreV{2,3,4,5}GenImpl get_bytes -> NEURON_ISA_TPB_*_STRUCT byte offsets
            (+ AssignHWDGEEngine native backend pass for HWDGE trigger-engine pick)
              │
              ▼
  HOP 5   DEVICE OPCODE
            TPB opcode byte (opcode-catalog-ledger numeric) -> engine (POOL/DVE/ACT/PE/NX)
            + the bf16 / int-wide / MX gen-variant of §A
              │
              ▼
  HOP 6   ISS VALUE MODEL
            the per-lane xdref leaf the POOL int datapath runs (32-bit wrap add/sub/mul),
            864/864 xdref leaves enumerated
  --      (device ISA floor) the Q7 Xtensa / FLIX kernel the opcode dispatches to (kernel_info_table)
  --      (three dispatch surfaces) .bin TPB-seq slot / SEQ ASCII (index = opcode − 0x41) / POOL Q7 kernel_info_table
```

### B.2 The three lowering shapes (the BIR→ISA boundary) `[HIGH/CARRIED]`

Every BIR→opcode binding is exactly one of three shapes:

| shape | example | what happens |
|---|---|---|
| **1:1 DIRECT** | `InstActivation → 0x21`; `InstIota → 0x7e`; `InstTensorTensor` (generic int/fp) `→ 0x41` | one BIR node, one opcode (the bulk) |
| **FAN-OUT / MACRO** | `InstCollective → {trigger + DGE/CCE descriptors}`; `InstBIRKernel(rmsnorm/softmax) → a POOL/ACT/DVE micro-schedule`; `InstTongaReduceMacroSymbolic → CrossLaneReduce + loop` | one BIR node, **many** ISA insts (a micro-schedule, not an opcode) |
| **FAN-IN / POLY** | `InstTensorTensor → {0x41 \| 0x8a/8b/8f bf16 \| 0xf3 int-wide \| 0xe5 scan \| 0x51 bitvec \| DVE/POOL engine}` | one BIR class, opcode picked at SundaISel by `{dtype, engine, gen, flag}` — **§A** |

This page closes the **FAN-IN** shape; [fused-cc-lowering](fused-cc-lowering.md) and
[collective-loadtime-rewrite](collective-loadtime-rewrite.md) own the **MACRO** shape; the
1:1 bulk is [compiler-map](compiler-map.md).

### B.3 The worked fan-in lowering (annotated pseudocode) `[HIGH/INFERRED from the validators]`

The reconstructed SundaISel decision for `InstTensorTensor` over the three axes — the
single routine that subsumes the whole of §A:

```c
/* SundaISel: lower one InstTensorTensor over {dtype, engine, gen}.
 * Validators (CC/isa_tpb/sunda pybind) OBSERVED; per-gen enum + opcodes CARRIED. */
ISAInst *lower_tensor_tensor(InstTensorTensor *bir, GenTarget gen) {

    /* (1) FLAG axis — the scan flag is its own opcode, gen-invariant. */
    if (bir->flags & SCAN)        return emit(TensorTensorScanOp, /*op=*/0xE5);
    if (is_bitvec_op(bir->alu))   return emit(TensorTensor,       /*op=*/0x51);   /* BITVEC, DVE */

    /* (2) DTYPE x GEN axis — the dedicated specialised opcodes. */
    if (bir->in0_dtype == BFLOAT16 && bir->in1_dtype == BFLOAT16 && gen == SUNDA) {
        /* validator: tensor_bf16_in_dtype && bf16_opcode && tensor_tensor_bf16_op */
        switch (bir->alu) {                                       /* AluOp PINNED to the opcode */
            case ADD:  return emit(TensorTensor, 0x8A);           /* 2x packed-BF16, POOL */
            case MULT: return emit(TensorTensor, 0x8B);
            case SUB:  return emit(TensorTensor, 0x8F);
        }
    }   /* cayman+: fall through to the generic 0x41 with the bf16 dtype carried on the node */

    if (is_int_datapath(bir) && bir->wide_result && gen == MAVERICK) {
        /* validator: valid_int_dtype_datapath {I/U 8/16/32} + the dual-dest constraint */
        return emit(TensorTensor, 0xF3);                          /* INT_WIDE, dst0=lo/dst1=hi, POOL */
    }

    /* (3) ENGINE axis — generic 0x41; the engine field is set at the NKI frontend (A.3),
     *     the opcode byte does NOT change. */
    Engine eng = bir->resolved_engine;   /* int32-no-PSUM -> GpSimd(POOL) ; else Vector(DVE) */
    return emit_with_engine(TensorTensor, /*op=*/0x41, eng);
}
```

The same one-class-many-opcodes shape applies to `InstCopyPredicated`
(`0x72`/`0x99`/`0xea`/`0xe8`), `InstActivation` (`0x21`/`0x25`/`0x26`), `InstTensorReduce`
(`0x42`/`0x52`, transpose `0x83`/`0x84`, cumulative `0x4e`/`0x5e`, bf16 `0x8c`/`0x8d`), and
`InstMatmultMx` (v4 `0x09`/`0x0A` pair vs v5 folded `0x01`/`0x02`).

### B.4 The lane in one row per stage — the consolidated map table `[HIGH]`

| hop | stage | artifact / function | source |
|---|---|---|---|
| 0 | frontend (NKI) | `nki.isa.<op>` → `emit_<op>` (62, plain py) | [compiler-map](compiler-map.md) §2/§3, [nki-frontend](nki-frontend.md) |
| 0 | frontend (XLA) | XLA-HLO → `hlo2penguin` → same BIR | [compiler-map](compiler-map.md) §6 |
| 0 | raw-byte | `n_bytes`/`extended_inst` → `0xF0` (`InstInlineASMBytes`) | [compiler-map](compiler-map.md) §5 |
| 1 | penguin BIR | `libBIR.so` — **110** `Inst*` classes | [bir-inst-roster](bir-inst-roster.md) §2 |
| 2 | SundaISel rewrite + fan-in | 34 `transformT*` + 21 `*CodeGen` + the §A dispatch | [sundaisel](sundaisel.md), §A |
| 2 | macro expand | AllReduce/RmsNorm/Softmax/MX-MoE micro-schedules | [fused-cc-lowering](fused-cc-lowering.md), [collective-loadtime-rewrite](collective-loadtime-rewrite.md) |
| 3 | Sunda ISA | `SundaISAInst.<Name>Op` (33 `*.Op`) | [bir-inst-roster](bir-inst-roster.md) §8 |
| 4 | descriptor emit | `libwalrus CoreVNGenImpl get_bytes`; `AssignHWDGEEngine` | [sundaisel](sundaisel.md) §6.5 |
| 5 | device opcode | TPB byte → engine (POOL/DVE/ACT/PE/NX) | [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) |
| 6 | ISS value model | the per-lane xdref leaf (32-bit wrap), 864/864 | §A.4 |

---

## C. The final coverage statement

### C.1 The producer accounting `[HIGH · OBSERVED + CARRIED]`

| layer | count | grounding |
|---|---|---|
| NKI `emit_` surface | **62** | `rg -c '^def emit_' isa_emit.py` == 62; each → exactly one `_nki_irbuilder.<mnemonic>` (perfect 1:1) |
| BIR `Inst*` roster | **110** concrete classes | `_ZTV` − 2 abstract bases == 110 (canonical); `createFromJson` == 110 after filtering `bir::Instruction` (§D.1) |
| SundaISAInst ISA-inst | **33** `*.Op` qualnames | `strings SundaISAInst.so \| rg -o 'SundaISAInst\.[A-Z][A-Za-z0-9_]+' \| sort -u` == 33 |
| SundaISel rewrite rules | **34** `transform*` + **21** `*CodeGen` | `.rodata` qualnames, SundaISel.so |
| device opcode union | **172** (= 140 real HW + 31 PSEUDO + 1 INVALID) | [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md); per-gen 145/150/159/165 |
| device Xtensa ISA floor | **1607** mnemonics / **12642** placements | the Q7 native layer each POOL opcode dispatches into |
| value oracle | **864/864** xdref leaves | the per-lane math under the POOL int route |

> **CORRECTION — the BIR `Inst*` count is 110, not 73 and not ~119.** Three figures have
> circulated; this capstone fixes the canonical one. **110** is the count of concrete
> classes, grounded on `_ZTV` vtables minus the two abstract bases (`Instruction`,
> `InstructionBasicBlockHolder`) = 110, and corroborated by `createFromJson` (111 raw → 110
> after filtering the abstract base `bir::Instruction`, which also carries a
> `createFromJson` — see §D.1). The **"73"** was the GPSIMD-relevant *subset* (the compute/gather/PE/ACT/
> BN/MX/RNG/collective/DMA families, minus the 24-class control spine, the 4 load/store, and
> the 5 kernel containers). The **"~119"** is the raw symbol count *including* abstract
> bases plus ctor/template symbols. Ground the count on `createFromJson` or `_ZTV`, never on
> the `C2ERKNSt` constructor recipe (which silently drops `InstDynamicForLoop`, undercounting
> by one to 109). `[HIGH/OBSERVED — bir-inst-roster §2/§8.]`

### C.2 The binding verdict — zero BIR produces a missing opcode `[HIGH/CARRIED]`

**Every GPSIMD-relevant BIR `Inst*` maps to a ledger opcode** (direct 1:1, a §A dtype/
engine/gen-routed fan-in, or a §B.2 macro). **Zero BIR `Inst*` produces an opcode the
firmware roster lacks.** The dtype/gen fan-in targets — `0x8a–0x8f` bf16, `0xf3`/`0xf4`
int-wide, `0x09`/`0x0A` MX-v4, `0x25`/`0x26` activate-variant — **all** have a BIR producer
(the **generic** `InstTensorTensor`/`InstTensorReduce`/`InstActivation`/`InstMatmultMx`),
even though no `emit_` or named BIR class names them directly. They are **lowering
targets**, not frontend ops. The 62 `emit_` are the NKI-exposed subset; the XLA/macro/
control delta (~48: PE sparse `0x06`/`0x07`, generic load/store/scatter, BN grad/backprop,
the 24-class control spine, RNG state mgmt, kernel containers) accounts for the remaining
BIR classes — `62 + ~48 = 110`, all bound to ledger opcodes or §B.2 macros. `[HIGH/CARRIED
— bir-inst-roster §6/§7, compiler-map §4.1.]`

### C.3 The residual firmware-internal opcodes (no BIR producer) `[HIGH · CARRIED + OBSERVED]`

The opcodes the compiler **never** emits — the device firmware materializes them
internally (**~9 families**):

- **(a) Inverse MX dequant `0x7B` TENSOR_DEQUANTIZE (POOL).** The compiler emits only the
  *forward* `InstQuantizeMx → 0xE3`; `0x7B` has no forward BIR producer — the MX matmul/
  dequant kernel consumes the packed data. (§A.5.1.)
- **(b) The cptc codec impls (inside `0xE4` CONV_LUT_LOAD).** dtype-selected sub-paths
  *inside* the `0xE4` dispatcher — no opcode, no BIR class.
- **(c) BN param-loads `0x64`/`0x66`/`0x8e`.** firmware-internal sub-steps of the BN
  codegen (the 256-reciprocal RAM load, param2 load), not distinct BIR nodes.
- **(d) `0x25` ACTIVATE2 / `0x26` ACTIVATE_MULTIPASS.** dtype/multipass fan-in targets of
  `InstActivation` (the `activate_opcode` validator selects); no distinct BIR class.
- **(e) The bf16 `0x8a–0x8f` + int-wide `0xf3`/`0xf4`.** dtype/gen fan-in targets of the
  **generic** `InstTensorTensor`/`InstTensorReduce`/`InstTensorScalar` — no NAMED BIR
  class, no `emit_` name. The BIR carries the dtype; SundaISel picks the opcode. (§A.2.)
- **(f) The firmware-internal SEQ sync band `0xb1`–`0xb4`/`0xb6`:** SET_ORDERING_MODE
  `0xb1`, MOVE_SHAPE `0xb2`, POLL_SEM `0xb3`, TEST_EVENT_SEM `0xb4`, COMPACT_CONTROL_INST
  `0xb6` — SEQ/NX-firmware control with no host op. (The *controllable* sync ops `0xa0`/
  `0xa1`/`0xa2`/`0xb0` **do** have BIR producers: `InstEventSemaphore`/`InstHalt`/`InstDrain`/
  `InstGroupResetSemaphores`.)
- **(g) The dormant legacy band (8 `// n` opcodes):** WEIGHT_MASK `0x04`, WEIGHT_SHIFT
  `0x05`, REG_LOAD/STORE/SHUFFLE `0x4a`–`0x4c`, ROI_ALIGN `0x73`, JPEG_DECODE `0x81`,
  TENSOR_REDUCE_RANGE_CHECK `0x9c` — legacy tonga opcodes the current BIR never emits.

> **NOTE — the RT-pseudo band `0xC1`–`0xDF` is the *inverse* case.** These 31 PSEUDO
> opcodes **do** have BIR producers (`InstDMATrigger → 0xC1`, `InstCollective →
> 0xC7`/`0xC8`/`0xD9`, `InstReadVarAddr → 0xC9`, `InstGetGlobalRankId → 0xDC`,
> `InstGetCurProcessingRankID → 0xDB`, `InstAllEngine/CoreBarrier → 0xD5`/`0xD8`,
> `InstTensorCompletion → 0xDE`) but are **relocated** by libnrt to real WRITE/SEMAPHORE/
> descriptor-trigger instructions *before any device decode*. So **BIR producer YES,
> device-firmware decode NO** — the opposite of the (a)–(g) set. `[HIGH/CARRIED —
> bir-inst-roster §4.3, collective-loadtime-rewrite.]`

### C.4 Open gaps (honest residual) `[explicit]`

- **The device body of the fan-in opcodes is not byte-traced.** The per-gen POOL/DVE
  Xtensa funcVA and the micro-op packing for `0x8a–0x8f` / `0xf3`/`0xf4` are not carved:
  SUNDA ships RELEASE (string-stripped, no self-name→VA), MAVERICK ships **no
  `MAVERICK_NX_POOL_DEBUG` image** (so a `0xf3` POOL self-name cannot appear), and `0xf4`
  has no dedicated self-name in the present MAVERICK DVE DEBUG. The struct / validator /
  semantics / per-gen presence **are** observed; the device byte-trace is the deferred
  boundary. `[MED/deferred — intwide-bf16-extremes §6/WALL.]`
- **The engine attribution for `0xf3` POOL / `0xf4` DVE** rests on the validator channel
  constant name (`POOLING_NUM_CHANNELS` vs `DVE_NUM_CHANNELS`, numerically both `128`) +
  the shared-struct analogy, not a self-name multiplicity. `[MED/INFERRED.]`
- **The BF16 "folded into the generalised dtype dispatch" retirement mechanism** is
  INFERRED from the struct-membership delta (8→5, 13→11) + the dtype-enum dispatch, not
  byte-traced through the cayman+ generic op. `[MED/INFERRED.]`
- **The in-ISel branch micro-order** inside SundaISel's lowering is not disassembled
  (SundaISel.so is a Cython `.so` without a rich dtype string surface). The *consulted*
  validators (isa_tpb pybind) and the frontend rule (tensor_ops.py) **are** observed; the
  exact bit-level branch order is the residual. `[the validators + frontend rule OBSERVED;
  the micro-order MED/deferred.]`
- **The `0xE3` gen floor is now RESOLVED** (not an open gap): the per-arch
  `s3dmx1_quant.h` header presence (mariana+maverick only) + the generation-local
  `nc==V4`/`nc==V5` gates pin the floor at **NC-v4 (MARIANA)** — see the §A.5.1 CORRECTION.
  The residual is only the **v5/MAVERICK MX interior** (the `MXTENSOR_V2` handler body),
  which is header-OBSERVED only and FLIX-desynced → INFERRED. `[v4 floor HIGH/OBSERVED; v5
  interior MED/INFERRED.]`

---

## D. Adversarial self-verify — the 5 strongest claims

Each claim re-derived from the binaries, attacked, resolved. `[HIGH/OBSERVED]`

**(1) The BIR roster is exactly 110 — not 73, not 109, not 111, not 112, not ~119.**
Two independent symbol families in `libBIR.so` must agree once the abstract bases are
filtered. Verified byte-exact this pass (`nm -D`, **mangled**, on the cp311 copy):

```
$ nm -D .../starfish/lib/libBIR.so | rg -o '_ZTVN3bir[0-9]+Inst[A-Za-z0-9_]+E' | sort -u | wc -l   # 112 raw
$   ... | rg -v '11InstructionE|27InstructionBasicBlockHolderE' | wc -l                              # 110  ← canonical
$ nm -D .../libBIR.so | rg -o '_ZN3bir[0-9]+Inst[A-Za-z0-9_]+14createFromJson' | sort -u | wc -l    # 111 raw
$   ... | rg -v '11Instruction14'                                                                    # 110  (filters bir::Instruction)
```

**The createFromJson set is `_ZTV − {InstructionBasicBlockHolder}`**: the abstract base
`bir::Instruction` *does* carry a `createFromJson` (so the raw grep is **111**, and the
regex `Inst[A-Za-z0-9_]+` catches it because "Instruction" starts with "Inst"), while
`InstructionBasicBlockHolder` does **not**. So the createFromJson set needs **one** filter
(`Instruction`) and the `_ZTV` set needs **two** (`Instruction` + `InstructionBasicBlockHolder`)
— both land on **110**. Attack: does the count depend on the cp-tag? cp310/cp311/cp312 are
byte-identical, so the dynamic table is the same. **Verdict: 110, grounded on `_ZTV` minus
the two abstract bases (the canonical recipe), with `createFromJson` corroborating once
`bir::Instruction` is filtered.** "73" = the GPSIMD compute subset; "111" is the
unfiltered createFromJson; "112" counts the two abstract-base vtables; "109" is the
`C2ERKNSt` recipe artifact (drops `InstDynamicForLoop`); "~119" is the raw symbol count
incl ctor/template syms.

**(2) `INT_WIDE` `0xf3`/`0xf4` appear ONLY in the MAVERICK enum; BF16 `0x8a–0x8f` ONLY in
SUNDA.** Re-grounded against the four arch-isa headers:

```
$ rg -n 'INT_WIDE|_BF16' ISA/.../neuron_{sunda,cayman,mariana,maverick}_arch_isa/tpb/aws_neuron_isa_tpb_common.h
  # INT_WIDE -> only maverick:320 (0xf3) / :321 (0xf4) ; zero in sunda/cayman/mariana
  # _BF16    -> only sunda:223-228 (0x8a/0x8b/0x8c/0x8d/0x8f, all // Y) ; zero later
```

Attack: is `0x8e` a sixth BF16 op in the span? No — `0x8e = BATCH_NORM_PARAM_LOAD2`,
`// Y` in all four gens. **Verdict: the cluster is five `{0x8a,0x8b,0x8c,0x8d,0x8f}`,
SUNDA-only; the INT_WIDE pair is MAVERICK-only.** (CARRIED-verified vs
[intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) §1.)

**(3) The int32-no-PSUM → GpSimd rule is the ONLY auto-GpSimd arith route, and the opcode
byte is engine-invariant.** Re-read `NKI/isa/tensor_ops.py`: the sole `unknown→gpsimd`
branch is the `{add,subtract,multiply} && all int32/uint32 && no PSUM` predicate. The
opcode-vs-engine separation is byte-confirmed in `targets/Opcodes.so`: `ALUOpcode.opcode`
(getter) and `ALUOpcode.cc_opcode` are *distinct members*, and `NeuronEngine` is a
*separate field*. Attack: is there a shape/cost ISel switch? Grep of SundaISel.so shows no
`transformTTensorTensorOperator` — `tensor_tensor` flows through base `NeuronISel`; the
engine is set at the frontend. **Verdict: `0x41` is fixed; GpSimd vs DVE is the parallel
engine field; the int32-no-PSUM predicate is the only auto route.**

**(4) `InstQuantizeMx` forward = `0xE3` (DVE), and `0x7B` has no forward BIR producer.** The
ledger rows `0xE3 | QUANTIZE_MX | DVE` and `0x7B | TENSOR_DEQUANTIZE | POOL` are distinct
opcodes on distinct engines. `InstQuantizeMx` is the *only* quantize BIR class in the 110;
there is no `InstDequantizeMx`. The nki irbuilder binding names the engine verbatim
(*"…using **Vector Engine**"*). Attack: could `0x7B` be reached via a flag on
`InstQuantizeMx`? No — opposite directions (pack vs unpack) on opposite engines (DVE vs
POOL), and the dequant is consumed by the MX matmul internally. **Verdict: forward `0xE3`
on DVE; inverse `0x7B` on POOL is firmware-internal with no forward producer.**

**(5) Zero BIR `Inst*` produces an opcode the firmware roster lacks.** Every cited opcode
in the §A/§B map resolves to a row in the
[opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) (172 union = 140 real
HW + 31 PSEUDO + 1 INVALID). The fan-in targets all have the generic BIR producer; the
~9 residual firmware-internal families (§C.3) are precisely the opcodes with **no** BIR
producer. Attack: is there a "hidden collective" opcode the BIR emits but the device lacks?
No — `InstCollective` lowers to the PSEUDO `0xC7`/`0xC8`/`0xD9` triggers (NRT-relocated,
not device-decoded); only the SB2SB p2p leg `0xBF` is a real device opcode. **Verdict: the
binding is total — every BIR-produced opcode is in the firmware roster.**

---

## E. Cross-check ledger — this synthesis vs the sibling pages `[HIGH]`

| claim (this capstone) | source (OBSERVED-this-pass) | sibling tie |
|---|---|---|
| dtype validators `bf16_opcode` / `tensor_*_bf16_*` / `valid_int_dtype_datapath` / `same_inout_dtype` / `valid_reversed` | strings isa_tpb sunda pybind | [bir-inst-roster](bir-inst-roster.md) §3.5 |
| flag validators `activate_opcode` / `copy_*_predicated_opcode` / `scalar_tensor_tensor_opcode` / `tensor_tensor_scan_opcode` | strings isa_tpb sunda pybind | [compiler-map](compiler-map.md) §4 |
| DTYPE enum `BFLOAT16`/`FP32R`/`INT32`/`UINT32` | strings enum_mapping pybind | [mx-path](mx-path.md) (E8M0 / dtype) |
| int32-no-PSUM → GpSimd; PSUM/fp → Vector; opcode engine-invariant | `rg` `nki/isa/tensor_ops.py` + `targets/Opcodes.so` | [sundaisel](sundaisel.md) §3 |
| 62 `emit_` functions | `rg -c isa_emit.py` == 62 | [nki-frontend](nki-frontend.md) |
| 110 BIR `Inst*` (`_ZTV`−2-abstract; createFromJson 111→110) | `nm -D` libBIR.so | [bir-inst-roster](bir-inst-roster.md) §2/§8 |
| 33 SundaISAInst `.Op` / 34 transform + 21 codegen | strings SundaISAInst.so / SundaISel.so | [sundaisel](sundaisel.md) §2 |
| `0x8a/8b/8f` TT bf16 + `0x8c/8d` reduce (SUNDA-only) | (CARRIED) | [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) §4 |
| `0xf3` TT INT_WIDE POOL / `0xf4` TS INT_WIDE DVE (MAVERICK-only) | (CARRIED) | [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) §3 |
| `S3S3D3_TT` 8→5 / `S4D4_TR` 13→11 cayman+ | (CARRIED) | [intwide-bf16-extremes](../firmware/kernels/intwide-bf16-extremes.md) §2 |
| MX v4 `0x09`/`0x0A` pair; v5 fold `0x01`/`0x02` MXTENSOR_V2 | (CARRIED) | [mx-path](mx-path.md) §3, [mx-device-bodies](mx-device-bodies.md) |
| `0xE3` fwd DVE (v4/MARIANA floor); `0x7B` POOL no producer | (CARRIED) | [mx-path](mx-path.md) §6 |
| gather 2:1 split: `local_gather`→`0xe7`; `nc_n_gather`→`0x68` | (CARRIED) | [compiler-map](compiler-map.md) §4.3, [keystone-facts](../orientation/keystone-facts.md) K12 |
| GPSIMD-not-float-hotpath (0 GpSimd float ops across keystones) | (CARRIED) | [fused-cc-lowering](fused-cc-lowering.md), [keystone-facts](../orientation/keystone-facts.md) K2 |
| xdref int wrap == POOL `0x41` int datapath (864/864) | (CARRIED) | §A.4, [sundaisel](sundaisel.md) §3.4 |
| device opcode union 172 (= 140 + 31 + 1) | (CARRIED) | [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) |

**VERDICT: zero firmware disagreements.** The fan-in is a validator-gated `{dtype, engine,
gen, flag}` dispatch over the generic BIR classes; every dtype/gen variant opcode has the
generic BIR producer; the residual firmware-internal opcodes are enumerated (~9 families);
the 7-hop CC lane is complete and consistent across the ten committed `compiler/` pages +
the opcode ledger + the int-wide/BF16 device extremes + the ISS oracle.

---

## See also

- [keystone-facts](../orientation/keystone-facts.md) — K2 (GPSIMD is not the float
  hot-path, cannot reach PSUM) and K12 (the gather 2:1 split) — the device-side keystones
  this page's engine fan-in is the host-side corollary of.
- [bir-inst-roster](bir-inst-roster.md) — the 110-class BIR `Inst*` set and the §3.5
  one-class-many-opcode table this page closes.
- [compiler-map](compiler-map.md) — the 62-row `emit_*`→opcode table (the NKI-exposed
  subset).
- [sundaisel](sundaisel.md) — the BIR→ISA rewrite rules and the engine-resolution internals.
- [mx-path](mx-path.md) / [mx-device-bodies](mx-device-bodies.md) — the MX gen fan-in
  (`0xE3` fwd / `0x7B` inv / v4 `0x09`/`0x0A` / v5 MXTENSOR_V2).
- [fused-cc-lowering](fused-cc-lowering.md) — the MACRO shape (RmsNorm/Softmax/MoE
  micro-schedules) and the per-keystone engine tally.
- [collective-loadtime-rewrite](collective-loadtime-rewrite.md) — the collective gen
  fan-in and the RT-pseudo relocation.
- [opt-sync-insertion](opt-sync-insertion.md) — the sync/semaphore insertion the lane
  threads through.
- [tiling-memory-scheduling](tiling-memory-scheduling.md) — the GPSIMD launch-cost reason
  small tiles stay on Vector.
- [nki-frontend](nki-frontend.md) — the 62-`emit_` frontend and the two-frontend convergence.
- [../firmware/kernels/opcode-catalog-ledger.md](../firmware/kernels/opcode-catalog-ledger.md)
  — the **single** device opcode catalog every numeric here is reconciled against.
- [../firmware/kernels/intwide-bf16-extremes.md](../firmware/kernels/intwide-bf16-extremes.md)
  — the device decode of the `0xf3`/`0xf4` INT_WIDE + `0x8a–0x8f` BF16 fan-in targets.
- [crossref-neuronxcc](crossref-neuronxcc.md) — the sibling capstone mapping this seam into
  the full neuronx-cc compiler wiki.
