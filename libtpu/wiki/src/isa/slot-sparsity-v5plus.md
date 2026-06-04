# Structured-Sparsity Slot (v5+)

> *Addresses apply to libtpu.so from the libtpu-0.0.40-cp314 wheel (BuildID md5 `89edbbe81c5b328a958fe628a9f2207d` — the unambiguous anchor; the runtime-reported `0.103` is not statically verifiable in the binary; not stripped — full C++ symbols). Other versions differ.*

## Abstract

This page covers the **TensorCore** structured-sparsity datapath for Viperfish-and-later (v5+): the *1:N* (2:4-style) weight sparsity that the MXU consumes natively, so that a sparse matmul or convolution clocks `block_size`× fewer systolic steps than its dense equivalent. The feature reaches the MXU as a small attribute carried on the convolution/dot HLO — an `xla::SparsityConfig` proto — that survives shape inference, gets validated against a strict constraint set in the `jellyfish` (TPU TensorCore) lowering, and finally threads a `block_size` packing factor into the gain-latch and window-config math of the matmul emitter. There is no separate "sparsity opcode": the sparsity is a *property* of the matmul that re-shapes the stationary operand and is emitted as the SME structured-sparsity outer-product instruction family, gated by a compiler flag.

Three subsystems in this binary all say "sparsity" and must not be confused. **This page is the first one.** (1) The TensorCore MXU sparsity here, owned by `xla::jellyfish::ConvolutionEmitter`, `SpatialMajorConvolution`, and `MatrixMultiplyAccumulateFunctor`. (2) The **SparseCore** embedding engine (`xla::tpu::sparse_core::CustomKernelEmitter`, the SCS/TAC/TEC sequencers) — a wholly separate Part IX subsystem whose `"Sparsity only supported on 2nd minor dimension"` check is *not* about the MXU. (3) A dead **NVGPU/NVVM** import path (`mlir::nvgpu::MmaSparseSyncOp`, `mlir::NVVM::MmaSpOp`, `getSparsitySelector`, the `sparsitySelector` attribute) that ships in the binary because the MLIR libraries are linked whole, but which targets NVIDIA `mma.sp` hardware and is never reachable from the TPU backend. The "selector operand" the task brief asked about belongs to *that* NVIDIA path, not the TPU MXU slot; the TPU MXU has no per-step selector operand — it derives the kept-lane pattern from the packed-kernel layout itself. This distinction is the single most important thing on the page and is documented in [§5](#5-what-is-not-the-tpu-mxu-path-nvgpu-and-sparsecore).

The structured-sparsity contract a reimplementer must honour:

- **The config proto** — `xla::SparsityConfig` carries an optional `lhs` and an optional `rhs`, each a nested `SparsityConfig_TensorSparsityConfig` with four scalar fields: `num_non_zero`, `block_size`, `dimension`, `stride`. Parsed by `ParseSparsityConfig` @ `0x1e4fb500`, serialized by `SparsityConfigToString` @ `0x1e5a50c0`.
- **The 1:N restriction** — `num_non_zero` must equal `1`; any other value is rejected in `ShapeInference::InferConvolveShape` @ `0x1e539040` with *"Only 1:N sparsity is currently supported."* The dense contraction extent is `block_size` × the stored extent.
- **The constraint gauntlet** — `ConvolutionEmitter::ValidateConvolutionWithSparseKernel` @ `0x130d6300` enforces `stride == 1`, the sparse dimension being the *kernel input-feature* dimension laid out on sublanes, no spatial dims, and `feature_group_count == batch_group_count == 1`.
- **The MXU threading** — `block_size` becomes the packing factor in `LatchKernelPossiblyPackedImpl` @ `0x1312c2c0` and `SpatialMajorConvolution::UpdateWindowConfigAndMegacoreSplitDim` @ `0x1316e380`, both of which `CHECK` that the input-feature tiling is a multiple of `block_size` and that `num_non_zero() == 1`.
- **The emission gate** — the SME structured-sparsity outer-product instruction family is gated by the flag whose help string `"Enable SME Structured sparsity outer product instructions."` lives at `0xa00bed8`. Availability is v5+ (Viperfish onward); v4 and earlier have no SME path.

| | |
|---|---|
| **Subsystem** | TensorCore MXU structured (1:N) sparsity — *not* SparseCore, *not* NVGPU |
| **Config carrier** | `xla::SparsityConfig` proto (optional `lhs`, optional `rhs`) |
| **Per-tensor fields** | `num_non_zero`, `block_size`, `dimension`, `stride` |
| **Sparsity ratio** | 1:N only (`num_non_zero == 1`, `N == block_size`) |
| **First generation** | v5+ / Viperfish (`kViperfish`=3) onward; SME path absent on Pufferfish (v4) and earlier |
| **Emission gate** | flag `enable_sme_structured_sparsity_outer_product_instructions` (help @ `0xa00bed8`) |
| **Owning namespace** | `xla::jellyfish` (TensorCore backend) |
| **Key validator** | `ConvolutionEmitter::ValidateConvolutionWithSparseKernel` @ `0x130d6300` |

---

## 1. The `SparsityConfig` proto and its field layout

Structured sparsity enters the compiler as an `xla::SparsityConfig` message attached to a convolution (the HLO `convolution` op carries it; `HloInstruction::sparsity_config()` @ `0x1e5aa080` is the accessor, returning a reference to the embedded proto). The message has exactly two optional sub-messages — one for each matmul operand:

```text
message SparsityConfig {
  optional TensorSparsityConfig lhs = 1;   // presence bit 0  (mask |1)
  optional TensorSparsityConfig rhs = 2;   // presence bit 1  (mask |2)

  message TensorSparsityConfig {           // xla::SparsityConfig_TensorSparsityConfig
    optional int64 num_non_zero = ?;       // the "1" of 1:N
    optional int64 block_size   = ?;       // the "N" of 1:N
    optional int64 dimension    = ?;       // which logical dim is sparse
    optional int64 stride       = ?;       // must be 1 (see §3)
  }
}
```

The presence of `lhs` / `rhs` is tracked by a 32-bit `_has_bits_` word at offset `+0x10` of the `SparsityConfig` object (the parser writes `*((_DWORD*)cfg + 4) |= 1` for `lhs`, `|= 2` for `rhs`; see [§2](#2-parsesparsityconfig)). The two pointers to the nested `TensorSparsityConfig` objects sit at qword offsets `+3` (`lhs`, `+0x18`) and `+4` (`rhs`, `+0x20`); both are lazily arena-allocated via `proto2::Arena::DefaultConstruct<SparsityConfig_TensorSparsityConfig>`.

Inside each `TensorSparsityConfig`, the four `int64` fields and the message's own `_has_bits_` byte are laid out from the parser's stores and the `ToString` reader as:

| Field | Object offset | `_has_bits_` (byte @ `+0x10`) | `ToString` vtable slot | Confidence |
|---|---|---|---|---|
| `num_non_zero` | `+0x18` | bit 0 (`\|1`) | `vt[3]` | High — parser store @ `0x1e4fb500:320`, reader @ `0x1e5a50c0:95` |
| `block_size` | `+0x20` | bit 1 (combined `\|3`) | `vt[4]` | High — parser store @ `0x1e4fb500:323`, reader @ `0x1e5a50c0:105` |
| `dimension` | `+0x28` | bit 2 (`\|4`) | `vt[5]` | High — parser store @ `0x1e4fb500:294`, reader @ `0x1e5a50c0:118` |
| `stride` | `+0x30` | bit 3 (`\|8`) | `vt[6]` | High — parser store @ `0x1e4fb500:270`, reader @ `0x1e5a50c0:141` |

> **NOTE (bit numbering / offsets) —** all bit positions on this page are **LSB-first**: `_has_bits_` "bit 0" is the `0x1` mask, "bit 1" is `0x2`, and so on, matching the `|1`/`|2`/`|4`/`|8` ORs the parser actually emits. The per-field object offsets above are read off the proto `Clear`/`InternalSerialize`/parser stores (`*(_QWORD*)(cfg + 24)`, `+ 32`, `+ 40`, `+ 48`). The proto-runtime *field numbers* are not recoverable from the binary (proto3 lite drops the field-number → name map), so the wire tags are not pinned; only the in-memory layout and the textual format are. The `ToString` vtable slots `vt[3..6]` are the message's accessor thunks, used here only to confirm field identity.

### The textual form

`SparsityConfigToString` (@ `0x1e5a50c0`) is the canonical serializer and the clearest single witness to field semantics. It emits, per present operand:

```text
lhs={sparsity=<num_non_zero>x<block_size> dimension=<dimension> stride=<stride>}
rhs={sparsity=<num_non_zero>x<block_size> dimension=<dimension> stride=<stride>}
```

joined by a single space when both are present. The `sparsity=` value is literally `FastIntToBuffer(num_non_zero) + "x" + FastIntToBuffer(block_size)` (`0x1e5a50c0:95-111`), so a config printed as `sparsity=1x4` means *num_non_zero=1, block_size=4* — i.e. one non-zero kept per group of four, the classic 1:4 / 2:8-equivalent structured pattern. `dimension=` reads `vt[5]`, `stride=` reads `vt[6]`.

---

## 2. `ParseSparsityConfig`

The HLO text parser entry point is `HloParserImpl::ParseSparsityConfig` @ `0x1e4fb500` (file/line strings pin it to `hlo_parser.cc:7164`; the nested per-operand parse logs as `ParseTensorSparsityConfig` at `:7199`). It implements the grammar `sparsity_config={lhs={…}, rhs={…}}`:

```c
// ParseSparsityConfig(SparsityConfig* out)  @ 0x1e4fb500
expect '{';                       // open brace, else return false
while (peek != '}') {
  name = lex_identifier();        // must be a token, else "expects attribute name"
  if      (name == "lhs") { out->_has_bits_ |= 1; cfg = out->mutable_lhs(); }
  else if (name == "rhs") { out->_has_bits_ |= 2; cfg = out->mutable_rhs(); }
  else                    { Error("unknown attribute"); return false; }
  expect '{';                     // open the TensorSparsityConfig body

  while (peek != '}') {           // ParseTensorSparsityConfig body
    attr = lex_identifier();
    if      (attr == "stride")    { cfg->set_stride( ParseInt64() );     cfg->_has |= 8; }
    else if (attr == "dimension") { cfg->set_dimension( ParseInt64() );  cfg->_has |= 4; }
    else if (attr == "sparsity")  {                       // value is "num_non_zero:block_size"
        auto [nnz, blk] = ParseDxD();                     // expects 'num_non_zero:block_size'
        cfg->set_num_non_zero(nnz); cfg->_has |= 1;
        cfg->set_block_size(blk);   cfg->_has |= 3;
    }
    else { Error("unknown attribute"); return false; }
  }
  expect '}';
}
expect '}';
```

Three details are worth a reimplementer's attention. First, the operand keys are matched by an inlined 3-byte compare against `"lhs"` (`0x686C`,`'s'`) and `"rhs"` (`0x6872`,`'s'`) — there are exactly two, no `out` or `both`. Second, the `sparsity` value is parsed by `ParseDxD` and the error text is *"expects 'num_non_zero:block_size'"* (`0x1e4fb500:329`), confirming the colon-separated `N:M` syntax where the first number is `num_non_zero` and the second is `block_size`. Third, the inner attribute compares are against `"stride"` (`0x69727473`+`0x6564`), `"dimension"` (`0x6F69736E656D6964`+`'n'`), and `"sparsity"` (`0x7974697372617073`) — those three and only those three; `num_non_zero` and `block_size` are *not* independent text attributes, they are the two halves of the `sparsity=` value.

> **GOTCHA (selector) —** there is no `selector` attribute in the TPU grammar. The `sparsitySelector` attribute string and the *"Invalid attribute `sparsitySelector` in property conversion:"* error (@ `0xa255171`) belong to `mlir::nvgpu::MmaSparseSyncOp::setPropertiesFromAttr` @ `0x17067b40`, the dead NVGPU path ([§5](#5-what-is-not-the-tpu-mxu-path-nvgpu-and-sparsecore)). A TPU sparse matmul carries no per-instruction selector operand; the kept-lane mask is implicit in how the packed kernel is stored on sublanes ([§4](#4-mxu-threading-the-block_size-packing-factor)).

---

## 3. Shape inference and the constraint gauntlet

### Shape inference: 1:N only, contraction × `block_size`

`ShapeInference::InferConvolveShape` @ `0x1e539040` (logged from `shape_inference.cc`) is where the sparse convolution's output shape is computed and the first hard restriction lands. After it reaches the RHS-with-sparsity branch, it reads `num_non_zero` (vtable slot `vt[3]`) and demands it be `1`:

```c
// InferConvolveShape, sparse-RHS branch  @ 0x1e539040:1262
auto* tsc = sparsity_config.rhs();        // TensorSparsityConfig*
if (tsc->num_non_zero() == 1)             // vt[3] == 1
    output_feature_extent *= tsc->block_size();   // vt[4]; dense extent = stored × N
else
    return InvalidArgument("Only 1:N sparsity is currently supported.");  // @0x1e539040:1271
```

The semantic is exact: the *stored* (compressed) RHS holds one kept value per `block_size` group, so the **dense** contracting extent is `block_size × stored_extent`. A reimplementer must apply this multiply when inferring the matmul's K dimension, or downstream tiling math will be off by `block_size`.

> **QUIRK (1:N, not 2:4) —** despite "2:4-style" being the colloquial name, the binary supports the more general **1:N** form, not a hard-wired 2:4. `num_non_zero` is pinned to `1`, but `block_size` (the `N`) is a free `int64` (`sparsity=1x4`, `1x8`, …). The downstream MXU emitter additionally `CHECK`s `KernelSparsityConfig().num_non_zero() == 1` (`SpatialMajorConvolution::UpdateWindowConfigAndMegacoreSplitDim` @ `0x1316e380:2980`), so the 1:N invariant is asserted twice — once as a user-facing error in shape inference, once as an internal `CHECK` in lowering.

### The `jellyfish` validator

Once shape inference passes, the TensorCore lowering re-validates against a far stricter set in `ConvolutionEmitter::ValidateConvolutionWithSparseKernel` @ `0x130d6300` (`convolution_emitter.h`). Every constraint below is a separate `InvalidArgument` with its own format string; all are owned by this single function:

| Constraint | Rejection message (abridged) | Confidence |
|---|---|---|
| Sparse dimension == kernel **input-feature** dim | *"expected kernel input feature dimension to be the sparse dimension."* | High — string @ `0xe6_…`, owner pinned |
| Sparse dim laid out **on sublanes** | *"expected kernel sparse dimension to be on sublanes."* | High |
| `stride == 1` | *"sparse conv with kernel_sparsity_stride not equal to 1 is not supported yet."* | High — @ `0xe53__` |
| **No spatial dims** | *"sparse conv with spatial dimensions is not supported yet."* | High |
| `feature_group_count == batch_group_count == 1` | *"…feature_group_count or batch_group_count not equal to 1 is not supported yet."* | High |
| batch a multiple of *N* | *"expected batch to be a multiple of %d."* | High |
| input-feature a multiple of *N* | *"expected input feature to be a multiple of %d."* | High |
| kernel / indices type in allowed set | *"expected kernel type to be one of %s and indices type to be one of %s."* | High |

Read together these say: a structured-sparse matmul on this TPU is a **pointwise** (1×1, no spatial window) convolution whose RHS (kernel) is compressed along its **input-feature** axis, that axis lives on the **sublane** dimension of the VMEM tile, the compression `stride` is `1`, there is no feature/batch grouping, and both the batch and the input-feature extents are exact multiples of the block factor `N`. The `%d` multiple in the batch/input-feature messages is the `block_size`.

> **NOTE (kernel-as-tuple) —** the packed sparse kernel arrives as a *2-tuple* operand: `{values, indices}` (the compressed weights plus the per-block kept-position metadata). `TpuInstructionFusion::BitcastConvOperands` guards this with *"Got tuple as kernel but no kernel sparsity config"* (string @ `0x86645ed`) and the shape-inference RHS path with *"rhs of convolution, if a tuple, must have 2 elements for sparsity"*. So the sparsity metadata is a real second operand, not a side attribute — but it is consumed structurally, not as a numeric selector per multiply.

---

## 4. MXU threading: the `block_size` packing factor

Inside the matmul emitter, `block_size` is the single number that re-shapes the systolic schedule. Two functions carry it.

`SpatialMajorConvolution::UpdateWindowConfigAndMegacoreSplitDim` @ `0x1316e380` `CHECK`s `KernelSparsityConfig().num_non_zero() == 1` (`:2980`) before adjusting the window/megacore split so that the compressed input-feature axis is walked in `block_size`-sized chunks rather than the dense extent. The "packed" representation means the MXU's stationary operand pool holds `1/block_size` of the dense weights, so one latch + matmul sequence covers `block_size`× more dense contraction than a dense one.

`MatrixMultiplyAccumulateFunctor::LatchKernelPossiblyPackedImpl` @ `0x1312c2c0` (the name's *PossiblyPacked* is the sparsity hook) is the gain-latch builder, shared with the dense path. Its two sparsity-specific assertions pin the tiling contract:

```c
// LatchKernelPossiblyPackedImpl  @ 0x1312c2c0
CHECK(effective_input_feature_sublane_chunks_per_tile
        % conv_->KernelSparsityConfig().block_size() == 0);   // :129
CHECK(effective_input_feature_tile_start_in_chunks
        % conv_->KernelSparsityConfig().block_size() == 0);   // :151
```

Both require the input-feature sublane chunking — *count per tile* and *tile start offset* — to be an exact multiple of `block_size`. This is the physical reason for the `"input feature to be a multiple of %d"` constraint in [§3](#3-shape-inference-and-the-constraint-gauntlet): the packed kernel is loaded into the array a `block_size`-chunk at a time, and a partial chunk has no valid latch encoding. The emitter then issues the SME structured-sparsity outer-product instruction family rather than the dense MXU `matmul`; the kept-lane pattern is carried by the packed layout (values+indices on sublanes), so no extra operand field is added to the bundle slot itself.

> **CORRECTION (no new slot) —** this topic is named a "slot" by the frontier register, but at the bundle level structured sparsity adds **no new TensorCore bundle slot and no new operand field**. It is the *same* MXU `VectorExtended` slot ([MXU slot](slot-mxu.md)) and the *same* gain-latch sub-slots ([matprep / IAR / latch](slot-matprep-iar-latch.md)); what differs is (a) the instruction *family* selected (SME outer-product vs dense matmul, chosen by the emission flag) and (b) the `block_size` divisor woven into the tiling/latch math. A reimplementer should not look for a "sparsity field" in the v5+ 64-bit bundle — there isn't one. The state that makes the matmul sparse lives in the operand's packed memory layout, not in a bundle bit. Bit-position recovery for a dedicated sparsity field therefore returns *nothing*, and that absence is the finding.

---

## 5. What is *not* the TPU MXU path: NVGPU and SparseCore

Three string clusters look relevant and are not. Pinning them down is part of the recovery.

### NVGPU / NVVM (`mma.sp`) — dead NVIDIA path

| Symbol | Owner | Why it is not the TPU path | Confidence |
|---|---|---|---|
| `getSparsitySelector` | `mlir::nvgpu::MmaSparseSyncOp` @ `0x17052500` | NVGPU dialect op for NVIDIA `mma.sync` sparse; never lowered on TPU | High |
| `sparsitySelector` attr | `MmaSparseSyncOp::setPropertiesFromAttr` @ `0x17067b40` | the *"Invalid attribute `sparsitySelector` in property conversion:"* error @ `0xa255171` is NVGPU property conversion | High |
| *"sparsity selector should be 0 or 1"* | `MmaSparseSyncOp::verify` @ … | NVIDIA's 2-bit metadata selector, not a TPU concept | High |
| *"sparsity selector must be i32 type"* | `mlir::NVVM::MmaSpOp::verify` @ `0x1658c500` | NVVM intrinsic verifier for `mma.sp`; targets PTX | High |

These ship because libtpu statically links the upstream MLIR `NVGPU`/`NVVM` dialect libraries; none of them are reachable from the `jellyfish` TPU backend. The "selector operand" in the original topic framing is *this* selector — a 2-bit-per-pair metadata index that NVIDIA tensor cores read — and it has **no TPU analogue**. The TPU keeps its kept-lane information in the packed operand layout instead.

### SparseCore — different subsystem entirely

`xla::tpu::sparse_core::CustomKernelEmitter::ChooseWindowLayout` owns *"Sparsity only supported on 2nd minor dimension"* (string @ `0x85fbe43`). That is the SparseCore embedding/scatter-gather engine (the SCS/TAC/TEC sequencers), a separate Part IX subsystem with its own custom-kernel emitter. Its "sparsity" is sparse *embeddings*, not MXU structured weight sparsity, and its 2nd-minor-dimension rule is unrelated to the `block_size`/sublane rules above. Do not merge the two.

---

## 6. Per-generation availability

The availability signal is the SME structured-sparsity outer-product instruction family and its emission flag.

| External name | Codename | `TpuVersion` ordinal | SME structured sparsity | Confidence |
|---|---|---|---|---|
| TPU v2 / v3 | Jellyfish / Dragonfish | `kJellyfish`=0 / `kDragonfish`=1 | No | High — no SME path; MXU is the v3 single-slot encoder |
| TPU v4 | Pufferfish | `kPufferfish`=2 | No | High — SME family absent on the pre-v5 encoder |
| **TPU v5** | **Viperfish** | **`kViperfish`=3** | **Yes** | Med — first gen with the SME outer-product path; gen boundary inferred (see NOTE below) |
| TPU v6 lite | Ghostlite | `kGhostlite`=4 | Yes | Med — v5+ family member; SME path shared with Viperfish |
| TPU7x | 6acc60406 | `k6acc60406`=5 | Yes | Med — v5+ family member; SME path shared |

The gate itself is a compiler flag whose help string *"Enable SME Structured sparsity outer product instructions."* sits at `0xa00bed8` (identifier `aEnableSmeStructuredSparsityOuterProductInstructions`; the string offset is byte-confirmed in the binary). The help string is reached only from a flag-registration table, not from any decompiled function — a sweep of the decompiled corpus finds **no code reference** to the string or the `enable_sme_structured_sparsity_outer_product_instructions` identifier — confirming it is a **boolean compiler flag** that toggles whether the lowering may emit the SME family, rather than a per-call runtime branch.

> **NOTE (gate vs gen — boundary inferred) —** the flag is the *emission* gate; the *hardware* gate is implicit in which gens have an SME unit (v5+). On a gen with no SME unit the flag would have no instructions to emit. The exact `TpuVersion` enum ordinal that first reports SME support was **not** traced to a single comparison site in this pass; the v5+/Viperfish boundary is an *inference* from the SME family being a v5-era addition and the absence of any SME sparsity reference reachable from the v3/v4 MXU encoders. Consistent with this, [VF 64-bit Bundle](bundle-vf-64b.md) records no `Sparsity` op among the Viperfish `VectorExtended0` families and leaves the VF-specific backing finding explicitly open. Both pages agree on the same fact: the source-level SME/`jellyfish` machinery documented here is generation-agnostic, and the per-gen *availability* (which silicon actually carries the SME unit) is the soft, unconfirmed part.

> **GOTCHA (block_size default) —** the flag has no numeric `block_size` of its own. `block_size` is per-operand, supplied in the `SparsityConfig`, and constrained only by the `num_non_zero == 1`, the multiple-of-`N` batch/input-feature rules, and the sublane-chunk divisibility in [§4](#4-mxu-threading-the-block_size-packing-factor). The common `1x4` is a convention, not a hardware constant; the binary does not pin a fixed `N`.

---

## 7. Reimplementation checklist

| Step | What to do | Anchor |
|---|---|---|
| 1. Carry the config | Attach `SparsityConfig{ rhs={num_non_zero=1, block_size=N, dimension=ifeat, stride=1} }` to the conv | `ParseSparsityConfig` @ `0x1e4fb500` |
| 2. Infer shape | Multiply the dense contraction extent by `block_size`; reject `num_non_zero != 1` | `InferConvolveShape` @ `0x1e539040` |
| 3. Validate | Enforce: pointwise (no spatial), sparse dim = kernel-ifeat on sublanes, `stride==1`, no groups, batch & ifeat multiples of `N` | `ValidateConvolutionWithSparseKernel` @ `0x130d6300` |
| 4. Provide metadata | Pass the kernel as a `{values, indices}` 2-tuple; the values are the compressed weights | `BitcastConvOperands` (tuple guard @ `0x86645ed`) |
| 5. Thread `block_size` | Walk the input-feature axis in `block_size` sublane chunks; assert divisibility | `LatchKernelPossiblyPackedImpl` @ `0x1312c2c0`, `UpdateWindowConfig…` @ `0x1316e380` |
| 6. Emit | Issue the SME structured-sparsity outer-product family on the *existing* MXU slot, gated by the flag | help @ `0xa00bed8` |

---

## Cross-References

- [MXU Slot](slot-mxu.md) — the `VectorExtended` matmul slot the sparse path re-uses; no sparsity field is added to it.
- [Matprep, IAR, and Latch Sub-Slots](slot-matprep-iar-latch.md) — the gain-latch machinery whose tiling math takes the `block_size` divisor.
- [VF 64-bit Bundle](bundle-vf-64b.md) — the Viperfish (TPU v5) bundle format; first gen with the SME path. There is no per-bundle sparsity field, and that page's VF-specific sparsity finding is independently marked open.
- [GL Bundle](bundle-gl.md) / [GF Bundle](bundle-gf.md) — Ghostlite (TPU v6 lite) / 6acc60406 (TPU7x) bundle formats; v5+ family members sharing the SME path.
- [TpuVersion / Codename Matrix](../targets/tpu-version-codename-matrix.md) — the authoritative enum↔codename↔external-name reconciliation; why `Trillium`/`Ironwood` have zero binary occurrences.
- [Instruction-Bits Master Database](instbits-master-db.md) — the per-gen field registry; confirms the absence of a dedicated sparsity slot.
- [ISA Overview](overview.md) — slot taxonomy and the v3 vs v4/v5+ encoder lineage split.
- [Dot/Conv → MXU Lowering](../compiler/dot-conv-mxu-lowering.md) — the matmul-lowering pipeline this sparsity attribute rides through.
- [RaggedDot & Convolution Lowering](../compiler/raggeddot-convolution.md) — the convolution emitter family that owns `ValidateConvolutionWithSparseKernel`.
