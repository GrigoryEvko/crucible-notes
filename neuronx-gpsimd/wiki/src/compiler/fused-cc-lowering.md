# Fused-CC / nkilib Kernel Lowering (worked)

> **Scope.** The four keystone GPSIMD product workloads — **RmsNorm, Softmax,
> AllReduce, MX/MoE** — carried *end-to-end* from the shipped nkilib Python kernel
> body down to the device opcode byte. For each: **(A)** the op DAG (the exact
> `nisa.<op>` sequence the kernel emits), **(B)** the SBUF/PSUM tiling, **(C)** the
> per-op engine assignment (GpSimd / Vector(DVE) / PE / ACT / SP), **(D)** the
> constituent device opcodes (`nisa.<op>` → BIR `Inst*` → `SundaISAInst` → TPB opcode
> byte, each cross-checked against the ledger), and **(E)** the data flow (tensor
> lifetimes + the PSUM↔SBUF moves). **RmsNorm and AllReduce are carried byte-by-byte.**
>
> This page is also the source of the **"GPSIMD is NOT the float hot-path"** keystone
> ([K2](../orientation/keystone-facts.md)): the float math of every keystone lands on
> Vector(DVE) / PE / ACT — the literal Q7 GpSimd cores appear only at the SB2SB
> collective hop (`0xBF`) and the int/gather/control lane. That conclusion falls out
> of the engine-assignment columns below, not from assertion: count how many ops land
> on `GpSimd` versus `Vector`/`PE`/`ACT`.

> **Audience.** A reimplementer building a Vision-Q7-compatible engine who already has
> the [BIR `Inst*` roster](bir-inst-roster.md) (the node→opcode map), the
> [SundaISel engine-selection rules](sundaisel.md), and the
> [opcode ledger](../firmware/kernels/opcode-catalog-ledger.md), and now needs to see
> *which* opcodes a real fused kernel actually issues, *in what order*, *on which
> engine*, and *with what PSUM traffic* — so the reimplemented micro-schedule matches
> the compiler's.

All facts below derive from static analysis of the shipped `neuronx-cc
2.24.5133.0+58f8de22` wheel: the **nkilib plaintext Python kernel library** (the
`.py` kernel bodies the NKI tracer runs to build each fused-CC BIR node, read directly
this pass — these are binary-derived, citeable artifacts), the cc-stubs
`nki/isa/__init__.pyi` op signatures, and the committed device pages cited inline.
Confidence is tagged per claim `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED`, where
OBSERVED = read directly from a shipped artifact this pass, CARRIED = OBSERVED in a
cited in-repo page and reused, INFERRED = reasoned over OBSERVED.

> **GUARD.** `sunda`/`tonga`/`cayman`/`mariana`/`maverick` are the per-generation
> codegen-target / arch-ISA axis (sunda = CoreV2 floor; cayman = CoreV3; mariana =
> CoreV4; maverick = CoreV5), **not** new silicon. The nkilib kernels are gen-agnostic
> Python; the per-gen opcode split (MX `0x09`/`0x0A` v4 vs folded `0x01`/`0x02` v5;
> exp ACT `0x21` vs DVE `0x30` by `nc_version`) is applied downstream at
> `SundaISel`/walrus and at the kernel's own `nc_version` branch. No silicon-gen fact
> is inferred from any compiler/kernel descriptor.

---

## 0. The one idea — a "fused-CC kernel" is a plaintext `nisa` op sequence

The [BIR roster §4.2](bir-inst-roster.md#42-bir-ops-with-no-direct-opcode--macro--fan-out-highobserved)
named the fused-CC kernel nodes — `InstBIRKernel` / `InstNKIKernel`, and the
`SundaISAInst` containers `TiledRmsNormOp` / `TiledSoftmaxOp` /
`TiledNativeKernel{Attention,MLP,QKV,RMSNormQuant}` — as *"one BIR node that
`RmsNormCodeGen` / `SoftmaxCodeGen` / `codegenTiledCCOp*` expands into a **sequence**
of POOL/PE/ACT/DVE opcodes."* But that page could only *name* the macro; the expansion
itself lives inside the stripped `RmsNormCodeGen` / `SoftmaxCodeGen` `.so` bodies.

This page recovers the sequence **from the other end**. Each fused kernel is a small
ordered list of `nisa.<op>` calls in a *plaintext* nkilib `.py` kernel — the very
program the NKI tracer runs to build that BIR node. So the macro expansion **is** the
kernel body, and it is in cleartext. The recovery is:

```
   nkilib .py kernel body          (the tracer's input — plaintext, read this pass)
     └─▶ ordered nisa.<op> list     §1–§4 op DAGs
           └─▶ penguin BIR Inst*    (bir-inst-roster §3: the node the tracer builds)
                 └─▶ SundaISAInst    (the ISA inst)
                       └─▶ TPB opcode byte   (opcode-catalog-ledger)
```

`[HIGH/OBSERVED — the op DAGs §1–§4; the per-op Inst*→opcode binding CARRIED
bir-inst-roster §3 + opcode-catalog-ledger]`

> **NOTE — the source-DAG is the macro body, not necessarily the *emitted* opcode
> order byte-for-byte.** The 1:1 "one `nisa.<op>` in the kernel == one BIR node the
> tracer builds == one §3-bound opcode" mapping is `[INFERRED-HIGH]` from the NKI
> IR-builder model (each `nisa.<op>` calls `_nki_irbuilder` to build exactly one
> penguin `Inst*`). A byte-level disasm of `RmsNormCodeGen`/`SoftmaxCodeGen` could
> still expose a *codegen fusion* the source presents as two ops — e.g. the
> `activation` `.pyi` carries a `reduce_op`/`reduce_cmd` param, so a `square` +
> `reduce` pair *can* fold into one `0x21 ACTIVATE` with an accumulating reduce. Where
> such a fold is plausible it is flagged GOTCHA below. The source DAG is the *upper
> bound* on the op count; codegen can only fuse, never add.

---

## 1. Keystone #1 (byte-carried) — RmsNorm

**Source.** `nkilib/core/subkernels/rmsnorm_tkg.py`, function `_process_rmsnorm_tile`
(**lines 306–442**, the per-tile compute core; the outer `_rmsnorm_tkg_llama_impl`,
**lines 445–560**, loops tiles and loads `gamma`/`eps`/the ones-matrix). The math:
`output = (input · gamma) / sqrt(mean(input²) + eps)`; the per-tile intermediate dtype
is `inter_dtype = nl.float32` (L345). Shapes use the
[tiling geometry](tiling-memory-scheduling.md): `H0 = pmax = 128` SBUF partitions;
`BxS = batch·seq` (tiled by `BxS_FULL_TILE_SIZE = 512`, the L34 constant); `H1 = H/128`
(the per-partition free axis). `[HIGH/OBSERVED]`

### 1.1 The op DAG (byte-exact, `nisa.<op>` in emission order)

```
in:  input_sb[H0,BxS,H1]  (SBUF)        gamma_sb[H0,BxS,H1]  (SBUF, broadcast)
     eps_sb[H0,1]         (SBUF)        matmul_reduction_const[H0,H0]=1.0  (SBUF)
```

| # | line | `nisa.<op>` (source) | role | → opcode |
|---|---|---|---|---|
| 1 | L357 | `activation(rmsnorm_square, op=nl.square, data=input_sb_view)` | x² | **ACT `0x21`** |
| 2 | L367 | `tensor_reduce(reduced, nl.add, rmsnorm_square, axis=2)` | Σ over H1 (free axis) | **DVE `0x42`** |
| 2b | L379+L388 | *[shard_on_h]* `sendrecv(remote_reduced_square)` + `tensor_tensor(reduced, reduced, remote, nl.add)` | LNC2 partial-sum exchange | **GpSimd `0xBF`** + DVE `0x41` |
| 3 | L401 | `tensor_tensor(gamma_mult, input_sb_view, gamma_sb_view, nl.multiply)` | input·gamma | **DVE `0x41`** |
| 4 | L413 | `nc_matmul(final_reduced⟨nl.psum⟩, stationary=matmul_reduction_const[H0,H0]=1.0, moving=reduced)` | Σ over H0 (cross-**partition**) | **PE `0x01`+`0x02`** |
| 5 | L421 | `activation(reduced, op=nl.rsqrt, data=final_reduced⟨PSUM⟩, scale=1.0/hidden_actual, bias=eps_view)` | 1/√(mean+eps); PSUM→SBUF drain | **ACT `0x21`** |
| 6 | L431 | `tensor_tensor(output_sb_view, gamma_mult_view, reduced_view.bcast(2,H1), nl.multiply)` | (input·gamma)·rms⁻¹ | **DVE `0x41`** |

```
out: output_sb[H0,BxS,H1]  (SBUF)
```

`[HIGH/OBSERVED — every row is a `nisa.<op>` call in `_process_rmsnorm_tile`.]`

### 1.2 The BIR `Inst*` / `SundaISAInst` / TPB-opcode join

The worked lowering table — each `nisa.<op>` resolved through
[bir-inst-roster §3](bir-inst-roster.md#3-the-complete-bir-instisatpb-opcode-map),
the engine column resolved through
[sundaisel §3](sundaisel.md#3-the-engine-polymorphic-decision--tensor_tensor--tensor_scalar),
the opcode numeric cross-checked against the
[ledger](../firmware/kernels/opcode-catalog-ledger.md):

| # | `nisa.<op>` | BIR `Inst*` | `SundaISAInst` | TPB opcode | engine | ISS value note |
|---|---|---|---|---|---|---|
| 1 | `activation(square)` | `InstActivation` | `ActivateOp` | `0x21` ACTIVATE | **ACT** | `f(scale·x+bias)`; `op=square` selects the quadratic PWL bucket |
| 2 | `tensor_reduce(add,axis=2)` | `InstTensorReduce` | `LocalReduceOp` | `0x42` TENSOR_REDUCE_ARITH | **DVE** | free-axis (H1) add-reduce, *within* partition |
| 3 | `tensor_tensor(mul)` | `InstTensorTensor` | `TensorTensor` | `0x41` TENSOR_TENSOR_ARITH | **DVE** | fp → DVE datapath (fp32-cast); int32 would route GpSimd |
| 4 | `nc_matmul(ones,reduced)` | `InstMatmult` | `MatMulOp` | `0x01` LDWEIGHTS + `0x02` MATMUL | **PE** | `final_reduced[p,:] = Σ_q ones[q,p]·reduced[q,:] = Σ_q reduced[q,:]` |
| 5 | `activation(rsqrt,scale,bias)` | `InstActivation` | `ActivateOp` | `0x21` ACTIVATE | **ACT** | `rsqrt(x/hidden + eps)` is **one** ACT inst (scale+bias fused) |
| 6 | `tensor_tensor(mul)` | `InstTensorTensor` | `TensorTensor` | `0x41` TENSOR_TENSOR_ARITH | **DVE** | fp → DVE |
| 2b | `sendrecv` | `InstCollectiveSend`/`Recv` | `SendRecvOp` | `0xBF` SB2SB_COLLECTIVE | **GpSimd** | intra-node SB2SB partial-sum hop (LNC2 only) |

`[HIGH — op DAG OBSERVED; the Inst*→ISA→opcode bindings CARRIED bir-inst-roster §3;
engine routing CARRIED sundaisel §3.1; opcode numerics CARRIED opcode-catalog-ledger]`

The two distinctive rows:

- **Row 4 — the cross-partition reduce is a PE matmul-against-ones, hand-written in
  the kernel.** Summing across the 128 SBUF partitions (the `H0` axis) is *not* done
  with a partition-reduce op. The kernel itself issues
  `nc_matmul(stationary=ones[H0,H0]=1.0, moving=reduced)`: with a constant-1 stationary
  matrix the systolic array contracts the partition axis, so each output row is the
  column-sum `Σ_q reduced[q,:]`. This is exactly the
  [SundaISel §5.1 `transformPartitionReduceOpAdd → matmultWithOnes`](sundaisel.md#51-the-dispatch-transformpartitionreduceop-body--0x35020-wrapper--0x48420-observed)
  correction — but seen **from the frontend**: the nkilib kernel does not rely on
  `LegalizePartitionReduce` to synthesize the ones-matmul; it hand-writes it. The
  `[H0,H0]` ones-matrix `matmul_reduction_const` is allocated `nl.sbuf` and filled by
  `nisa.memset(matmul_reduction_const, value=1.0)` in the outer impl (**L535**); the
  per-tile docstring (L327) reads *"matmul_reduction_const_view … [H0, H0], Constant
  matrix for reduction."* The same ones-matmul reduce recurs byte-for-byte in
  `rmsnorm_mx_quantize_tkg` (memset L121, matmul L230). `[HIGH/OBSERVED]`

- **Row 5 — `rsqrt(x/hidden + eps)` is one fused ACT instruction.** The `nisa.activation`
  signature is `f(scale·x + bias)` ([activate-pwl](../firmware/kernels/activate-pwl.md)):
  passing `scale=1/hidden` and `bias=eps` folds the division-by-`hidden`, the
  add-`eps`, and the `rsqrt` PWL into a single `0x21 ACTIVATE`. This is also the *only*
  op that reads PSUM (it drains `final_reduced` from PSUM and writes the reciprocal back
  to SBUF). `[HIGH/OBSERVED — the nisa.activation `scale`/`bias` kwargs]`

> **CORRECTION — `0x42`/`0x41` engine column.** The
> [opcode ledger](../firmware/kernels/opcode-catalog-ledger.md) lists `0x41
> TENSOR_TENSOR_ARITH`, `0x42 TENSOR_REDUCE_ARITH`, `0x43`, `0x46`/`0x47` under the
> **POOL** engine column — that is the opcode's *home class engine*, not the engine the
> op runs on for this kernel. For RmsNorm's **fp32** intermediates the engine is the
> **DVE (Vector)** datapath, per the
> [SundaISel §3.1 routing rule](sundaisel.md#31-tensor_tensor-engine-resolution-tensor_opspy86124-observed-byte-exact)
> (int32/uint32 `add`/`sub`/`mul` with no PSUM operand → GpSimd; *everything else* →
> Vector). The opcode byte (`0x41`/`0x42`) is engine-invariant; the engine is a
> *parallel* `NeuronEngine` field. So rows 2/3/6 carry opcode `0x42`/`0x41` (POOL home
> class) but run on **DVE**. This is the crux of the "not-the-float-hot-path"
> conclusion (§5). `[HIGH/OBSERVED — sundaisel §3.1; ledger engine column]`

### 1.3 The engine assignment (the fused-CC engine map for RmsNorm)

| op # | engine | why |
|---|---|---|
| 1, 5 | **ACT (Scalar)** | `square` / `rsqrt` are PWL activation functions (`0x21`) |
| 2 | **DVE (Vector)** | fp32 free-axis reduce (`0x42`) |
| 3, 6 | **DVE (Vector)** | fp32 elementwise multiply (`0x41`) |
| 4 | **PE (Tensor)** | cross-partition reduce via ones-matmul (`0x01`+`0x02`) |
| 2b | **SB2SB / GpSimd** | LNC2 `sendrecv` partial-sum exchange (`0xBF`) — only when sharded on H across cores |

**RmsNorm touches ACT + DVE + PE (+ the SB2SB GpSimd hop only on the LNC2-sharded
path). The Q7 GpSimd cores do NO float arithmetic here.** Of the six core ops, **zero**
run on the GpSimd Q7 cores; the only GpSimd appearance is the optional `0xBF` SB2SB
copy in the sharded partial-sum exchange — a *data-movement* hop, not float math.
`[HIGH — OBSERVED ops + CARRIED engine routing sundaisel §3]`

### 1.4 The SBUF/PSUM tiling + data flow

**Partition axis** = `H0` = 128 (the full partition span; the input is laid out
`[H0=128, BxS, H1]`). **Free axis** = `BxS`, tiled by `BxS_FULL_TILE_SIZE = 512` (the
outer `TiledRange` loop); within a tile the per-partition free budget holds
`[BxS_tile, H1]` fp32.

**SBUF-resident (all of):** `input`, `gamma`, `eps`, the ones-matrix, `square`,
`reduced`, `gamma_mult`, `output` — every intermediate is SBUF
([`StackAllocator` byte cursor](tiling-memory-scheduling.md), the linear-scan SBUF
allocator). **PSUM-resident:** **only** `final_reduced[H0,BxS]` (the `nc_matmul` dst) —
the single PSUM bank this kernel uses
([`InferPSumTensor`](tiling-memory-scheduling.md) assigns the matmul result to PSUM; the
rest stays SBUF because the DVE/ACT ops write SBUF). The result fits one PSUM bank
(`BxS_tile ≤ 512` fp32 = the [2048-B bank ceiling](tiling-memory-scheduling.md)).

Data flow (tensor lifetimes):

```
   input ──square(ACT 0x21)──▶ square ──reduce(DVE 0x42)──▶ reduced ───────┐
   input ──┐                                                                │
   gamma ──┴── mul(DVE 0x41) ──▶ gamma_mult ──┐         reduced ── matmul-ones(PE 0x01/0x02) ──▶
                                               │              final_reduced  (PSUM, the ONLY PSUM tensor)
   final_reduced ── rsqrt(ACT 0x21) ──▶ reduced(reused) ──┐
   gamma_mult ──────────────────────────────────────── mul(DVE 0x41) ──▶ output  (SBUF)
```

**The only PSUM↔engine moves are the PE write (matmul → `final_reduced`) and the ACT
read (rsqrt reads `final_reduced` from PSUM, writes the reciprocal back to SBUF).
Everything else is SBUF-local.** This is the
[GPSIMD-no-PSUM rule (K2)](../orientation/keystone-facts.md#k2--gpsimd-is-not-the-float-hot-path-and-it-cannot-reach-psum-at-all)
in action — and consistent: the only PSUM toucher is the PE (which owns the
accumulator) and the ACT drain; no GpSimd op goes near PSUM. `[HIGH/OBSERVED tensor
buffers + the `nl.psum` tag on `final_reduced` in source]`

### 1.5 The load / transpose prelude

`norm_tkg_utils.load_input_to_sbuf` (def L270) stages an HBM input into SBUF **with a
transpose** so `H` becomes the partition axis. The `hidden_dim_tp=True` path uses
`nisa.dma_transpose` (the axi2sram 16×N crossbar → `InstDMADescriptorTranspose` → `0xBD`
DMA_TRANSPOSE) at L312; the small-`H` on-chip path
(`contiguous_load_transpose`, def L150, when `H ≤ _CONTIGUOUS_LOAD_H_THRESHOLD = 2048`)
uses `nisa.dma_copy` (L211) → `nisa.nc_transpose` into PSUM (L249, `tp_psum = nl.psum`)
→ `nisa.tensor_copy` PSUM→SBUF (L265); the default falls to `nisa.dma_copy(…,
dge_mode=_DGE_MODE_NONE=3)` (L320). `load_gamma_to_sbuf` (def L341) likewise
`dma_transpose`s the `[1,H]` gamma to `[H0,H1]` (L376). The `_rmsnorm_tkg_llama_impl`
docstring (rmsnorm_tkg.py **L476**) is explicit: *"Uses Static DMA for input data reads
(superior performance vs DGE)."*

So the full kernel prelude is:
`HBM ──dma_transpose (0xBD) / dma_copy (0xB8) + nc_transpose (0x01/0x02)──▶ SBUF`,
**then** the §1.1 compute.
`[HIGH/OBSERVED — the load helpers + the `nisa.dma_transpose`/`nc_transpose` calls]`

### 1.6 RmsNorm — byte-carry summary

The full path: the entire `rmsnorm_tkg`, traced by the NKI frontend, builds one
`InstBIRKernel`/`InstNKIKernel` node; `SundaISel`'s `RmsNormCodeGen` (the
`transformTRmsNormOperator` dispatcher → `TiledRmsNormOp`) is the rule that emits the
§1.1 opcode sequence as the micro-schedule. The §1.1 list **is** that micro-schedule,
recovered from the kernel the tracer runs.
[`SundaSizeTiling`](tiling-memory-scheduling.md) tiles `BxS` by 512; the allocator
places `final_reduced` in one PSUM bank and the rest in SBUF; the scheduler +
`AntiDependencyAnalyzer` order the ACT/DVE/PE stream and insert the inter-engine
semaphores; libwalrus `CoreVNGenImpl` emits the
`0x21`/`0x42`/`0x41`/`0x01`/`0x02` descriptors into the per-engine `.bin` slots.

**The byte-carried opcode multiset for one RmsNorm tile (no LNC2 shard):**
`{0x21 ×2 (ACT square, rsqrt), 0x42 ×1 (DVE reduce), 0x41 ×2 (DVE mul), 0x01 ×1 +
0x02 ×1 (PE ones-matmul)}` = **6 compute ops across 3 engines (ACT, DVE, PE)**, plus
the `0xBD`/`0xB8` load prelude. **GpSimd Q7 op count for the float compute: 0.**
`[HIGH — §1.1 OBSERVED; the BIR-node + ISel-rule + tiling/alloc/schedule path CARRIED
bir-inst-roster + tiling-memory-scheduling]`

---

## 2. Keystone #2 (byte-carried) — AllReduce

AllReduce is the structurally **different** keystone: its "compute" is an **in-SDMA
reduce**, not a TPB-engine op sequence. The lowering chain is BIR → PSEUDO trigger →
NRT host-side rewrite → SDMA descriptor schedule → in-flight CCE reduce. The byte-level
host-side rewrite is the subject of
[the collective load-time rewrite page](collective-loadtime-rewrite.md); this section
carries the end-to-end DAG and ties it to the `sendrecv` primitive the *other*
keystones share.

### 2.1 The op DAG (the lowering, top to bottom)

```
FRONTEND   nki.isa.all_reduce / emit_all_reduce
             └─▶ penguin BIR  InstCollective(ctype=ALL_REDUCE)
                   ctype tag ALL_REDUCE = 0x1 ; reduce op rides ALU_OP
                   (ADD=0x04 / MAX=0x08 / MIN=0x09) ; dtype rides DTYPE
                   (BF16=6 / FP16=7 / FP32R=11 / FP8_E*=13/14/15)

ISEL       transformAllReduceOp + codegenTiledCCOpWith(out)Rank
             └─▶ TiledCollective(Reduce)Op  →  NO direct opcode; a PSEUDO trigger:
                   0xC7 PSEUDO_TRIGGER_ALL_REDUCE      (compact: op,dtype,src,dst,num_elems)
                   0xC8 PSEUDO_TRIGGER_COLLECTIVE      (ctype=ALL_REDUCE, DRAM handles)
                   0xD9 PSEUDO_TRIGGER_COLLECTIVE2     (ctype=ALL_REDUCE, SBUF ADDR8TENSOR2D)
                 all three: upper-three opcode bits 0b110 ⇒ PSEUDO ⇒ never HW-decoded;
                 NRT rewrites them at model load.

RUNTIME    NRT __select_algorithms picks enc_alg_type per leg:
(NRT,host)   RING(0) / HIER(1) / MESH(2) / KANGARING(3) / SINGLE_CYCLE_RING(4) / RDH(5,7)
             intra reduce-scatter/all-gather = RING|KANGARING
             inter all-reduce = RING|INTER_RDH|SINGLE_CYCLE_RING|MESH
             op enum  ISA ALU_OP → SDMA_CCETYPE {ADD0, FMA1, MAX2, MIN3}

DEVICE     chosen algorithm emits SDMA descriptors; each reduce step
             (recv_reduce_send / recv_reduce_copy / direct_reduce_send_*) builds a CCE
             descriptor (add_dma_packet_cce) whose 16-B meta-control word carries the
             CCE-valid bit + op sub-selector + in/out/accum dtype nibbles.
           THE REDUCE HAPPENS IN THE SDMA DATAPATH (CCE block of the CDMA channel):
             per element  scale·src + accum  (FMA) or ADD/MIN/MAX, with dtype-convert +
             stochastic rounding — NO Q7 firmware FMA loop ships.
           The intra-core SB2SB hop of the ring rides InstGPSIMDSB2SB → 0xBF
             SB2SB_COLLECTIVE (GpSimd) + decode_sb2sb_collective device ucode.
```

`[chain HIGH/OBSERVED at each named hop; CARRIED the collective encoding + the in-SDMA
CCE reduce + the 0xBF SB2SB binding]`

### 2.2 The decomposition (the "DAG" of a RING all-reduce)

```
RING all-reduce over N ranks:
  REDUCE-SCATTER phase  (N−1 steps)
    each rank: recv_reduce_send — receive a chunk, CCE-reduce it (the op) into its own,
               forward. After N−1 steps each rank owns one fully-reduced chunk.
  ALL-GATHER phase      (N−1 steps)
    each rank: direct_recv_send — the reduced chunks rotate the ring WITHOUT reduction
               (pure copy).

HIER all-reduce  = intra reduce-scatter → inter all-reduce → intra all-gather
   (the NCFW 3-phase: local ring reduce-scatter, inter-die mesh all-reduce,
    ring all-gather broadcast-back).
```

`[HIGH/CARRIED the RING/HIER decomposition]`

### 2.3 The engine assignment + tiling + data flow

**Engine.** The **trigger** is an SP/GpSimd pseudo (relocated to a TOP-SP instruction
series at load — the [TOP_SP sequencer (K3)](../orientation/keystone-facts.md#k3--there-are-three-distinct-cores-in-the-stack-do-not-conflate-them)
walks the cc_op program); the **reduce** is in the **SDMA (engine 4 / CCE)**; the
intra-core SB2SB copy is GpSimd/POOL (`0xBF`). **No PE/DVE/ACT op runs for the reduce
itself** — the contrast with RmsNorm/Softmax/MoE. `[HIGH/CARRIED]`

**Tiling.** The collective operands are SBUF 2-D tensors (`0xD9`, ADDR8TENSOR2D) or
DRAM handles (`0xC8`); the SDMA CCE reads N source streams off the M2S leg and writes 1
reduced result down the S2M leg. The CCE element-grouping factor is dtype-dependent
(BF16/FP16 packed 2-wide, FP32R 4-wide, FP8 1-wide); a `max_cce_elements` cap bounds
the per-descriptor element count (e.g. cayman 0x2000 / 0x800). `[HIGH/CARRIED]`

**Data flow.**

```
   src(SBUF/DRAM) ──M2S──▶ [ CCE reduce IN-FLIGHT ] ──S2M──▶ dst(SBUF/DRAM)
   across ranks the chunks rotate via the ring DMA legs;
   a two-semaphore EVT_SEM completion orders the steps.
```

`[HIGH/CARRIED]`

### 2.4 The nkilib tie — `sendrecv` is the collective primitive the other keystones share

The keystone kernels all call
`nisa.sendrecv(src, dst, send_to_rank, recv_from_rank, pipe_id, dma_engine)` for the LNC2
partial-statistic exchange, **always** with `send_to_rank = recv_from_rank = 1 − shard_id`
(or `1 − sprior_prg_id`), `pipe_id` distinguishing concurrent exchanges:

- **RmsNorm** (`rmsnorm_tkg.py`): `_rmsnorm_tkg_shard_on_h` issues `sendrecv` of the
  per-tile partial square-sum (**L379**) then `tensor_tensor(…, nl.add)` (**L388**) to
  form `full_sq = local_sq + remote_sq` — the reduce half of an all-reduce, *inside*
  `_process_rmsnorm_tile`. `_rmsnorm_tkg_shard_on_bxs` issues `sendrecv` of the whole
  output tile (**L218**, `pipe_id=0`).
- **Softmax** (`attention_tkg.py`): `sendrecv` of running max (**L2126**/L1111) and sum
  (**L2438**/L1192), each followed by `tensor_tensor(maximum)` / `tensor_tensor(add)`.
- **MX/MoE** (`rmsnorm_mx_quantize_tkg.py`): **3×** `sendrecv` on pipe 0/1/2 (**L263/270/
  277**) for `output` / `output_quant` / `output_scale`.

`nisa.sendrecv` → `InstCollectiveSend`/`Recv` → `SendRecvOp` → **`0xBF` SB2SB_COLLECTIVE**
(GpSimd, the intra-node SB2SB hop) **or** the PSEUDO `0xCB` SEND_RECV (relocated). The
`dma_engine` kwarg steers it: `gpsimd_dma` (when `use_gpsimd_sb2sb`) vs the shared `dma`.

**So the same `0xBF` SB2SB hop that is one step of a RING all-reduce is the partial-sum
exchange inside the fused norm/softmax kernels** — the collective and the fused-CC
kernel share the SB2SB device leg
([sb2sb-remote-copy](../firmware/kernels/sb2sb-remote-copy.md)). This is the *one*
place the literal Q7 GpSimd engine touches a keystone float kernel — and it is a
*movement* op, not float math. `[HIGH/OBSERVED `nisa.sendrecv` calls + CARRIED the
`0xBF` binding]`

### 2.5 AllReduce — byte-carry summary

**The byte-carried trigger for one AllReduce node** is one PSEUDO opcode from the
`0xC7`/`0xC8`/`0xD9` band (which `0b110`-prefix byte depends on operand kind: compact /
DRAM-handle / SBUF-2D), which the NRT host rewrites into a RING or HIER schedule of SDMA
CCE descriptors. **The TPB compute-engine opcode count for the reduce is 0** — every
arithmetic step is an in-SDMA `{ADD0, FMA1, MAX2, MIN3}` CCE op, and the only Q7 opcode
in the whole schedule is the `0xBF` SB2SB intra-core hop. AllReduce is the keystone that
*proves the negative*: a "GPSIMD collective" runs **no** GpSimd float math at all.
`[HIGH/OBSERVED the trigger band; CARRIED the in-SDMA CCE reduce]`

---

## 3. Keystone #3 (worked) — Softmax (flash / online, attention)

**Source.** `nkilib/core/attention/attention_tkg.py` (the token-generation attention
kernel; 3606 lines). The softmax is the **online/flash** form, computed per FA tile with
a running max/sum and a correction factor — there is **no** full-tensor softmax. The
per-tile DAG below is the steady-state (gen4+ pipelined) path. Function anchors:
`_compute_qk_matmul` (def L1576, MM1), `_cascaded_max_reduce` (L2060),
`_compute_exp_qk` (L2318), `_cascaded_sum_reduction` (L2395),
`_fa_update_running_max` (L1133) / `_fa_update_running_sum` (L1233) /
`_fa_update_correction_factor` (L1074), `_compute_pv_matmul_and_store` (L2650, MM2),
`_fa_finalize_and_store` (L1324). `[HIGH/OBSERVED]`

### 3.1 The op DAG (per FA tile, `nisa.<op>` in emission order)

```
in:  qk[p_max, …] = Q@Kᵀ result  (a prior PE matmul, in SBUF after PSUM drain)
```

| step | line | `nisa.<op>` (source) | role | → opcode |
|---|---|---|---|---|
| 1 | L1974 | `nc_matmul(qk_psum, stationary=k_tile, moving=q_sb_view)` | QKᵀ (MM1, → KQᵀ) | PE `0x01`/`0x02` |
| 2.1 | L2079 | `tensor_reduce(qk_max, op=nl.maximum, qk_view, axis=[2])` | tile max | DVE `0x42` |
| 2.2 | L2267+L2273 | `nc_transpose(_transpose_max_psum)` + `tensor_reduce(maximum, axis=1)` | cross-part transpose + reduce | PE `0x01`/`0x02` + DVE `0x42` |
| 2.3 | L2126 | *[LNC2]* `sendrecv(qk_max, …, dma_engine=gpsimd_dma\|dma)` then `tensor_tensor(maximum)` | sync max across cores | `0xBF` + `0x41` |
| 2.4 | L2305 | `tensor_scalar(op0=nl.maximum, operand0=_MIN_FLOAT32)` | clamp −inf→minfloat | DVE `0x43` |
| 3a | L2340/L2353 | `tensor_tensor(qk, qk, qk_max, op=nl.add if max_negated else nl.subtract)` | qk − max | DVE `0x41` |
| 3b | L2349/L2359 | `activation(qk_io_type, op=nl.exp, data=qk)` | exp(qk−max) | ACT `0x21` |
| 4.1 | L2589+L2596 | `nc_matmul(sum_reduce_psum, stationary=qk_io_type, moving=bufs.one_vec)` then `tensor_reduce` evict | tile sum (ones-**vector** matmul) | PE `0x01`/`0x02` + DVE `0x42` |
| 4.2 | L1079 | `activation(op=nl.exp, scale=−1.0, bias=…)` correction | exp(prev−curr) | ACT `0x21` |
| | L1256+L1263 | `running_sum·corr + tile_sum`: `tensor_tensor(mul)` + `tensor_tensor(add)` | online sum | DVE `0x41` ×2 |
| | L2438/L1192 | *[LNC2]* `sendrecv` + `tensor_tensor(add)` | sync sum across cores | `0xBF` + `0x41` |
| 5 | L2952 | `nc_matmul(exp_v_psum, stationary=v_sb_view, moving=qk_io_type)` | softmax @ V (MM2) | PE `0x01`/`0x02` |
| | L2964/L2973 | evict: `tensor_copy(exp_v, psum)` (FA) **or** `tensor_tensor(exp_v, psum, recip, mul)` (non-FA) | online output | `0x46` / `0x41` |
| 6 | L1348/L2555 | `reciprocal(fa_running_sum, fa_running_sum)` | 1/sum | DVE `0x48` |
| | L1359 | `tensor_tensor(out, out, recip.bcast, nl.multiply)` | normalize | DVE `0x41` |

```
out: out[d_head, s_active_bqh]  (SBUF) ──▶ HBM
```

`[HIGH/OBSERVED — every row is a `nisa.<op>` in `attention_tkg.py` at the cited line.]`

> **GOTCHA — the tile-sum is a ones-*vector* matmul, the same trick as RmsNorm.** The
> per-tile `exp_sum` is *not* a free-axis `tensor_reduce`; `_tile_sum_reduction` (L2563)
> issues `nc_matmul(stationary=qk_io_type, moving=bufs.one_vec)` (**L2589**) into PSUM
> then drains it with a `tensor_reduce` (L2596). So Softmax, like RmsNorm (§1.1 row 4),
> pushes a *reduction* onto the **PE array** via a constant-1 operand — here a ones-
> *vector* rather than a `[H0,H0]` ones-*matrix*. A reimplementer must wire the PE for
> both reduce shapes, not just the matmul proper. `[HIGH/OBSERVED L2589]`

### 3.2 The BIR/ISA/opcode join (the deltas vs RmsNorm)

| `nisa.<op>` | BIR `Inst*` | `SundaISAInst` | TPB opcode | engine | note |
|---|---|---|---|---|---|
| `tensor_reduce(maximum/add)` | `InstTensorReduce` | `LocalReduceOp` | `0x42` (arith) | DVE | `max`/`add` are arith; bitwise would be `0x52` |
| `activation(op=nl.exp)` *(both gens)* | `InstActivation` | `ActivateOp` | `0x21` ACTIVATE | ACT | the fused-ACT exp — emitted on **all** gens |
| `tensor_scalar(maximum)` | `InstTensorScalar` | `TensorScalar` | `0x43` (arith) | DVE | the `_MIN_FLOAT32` −inf clamp (2.4) |
| `reciprocal` | `InstReciprocal` | `Reciprocal` | `0x48` RECIPROCAL | DVE | maintained decode-gap |
| `nc_transpose` / `nc_matmul` | `InstMatmult` | `MatMulOp` | `0x01`+`0x02` | PE | QKᵀ, max-transpose-to-PSUM, ones-vec sum, softmax@V |
| `tensor_copy` | `InstTensorCopy` | `TensorCopy` | `0x46` COPY / `0x47` CAST | DVE/Scalar | running-stat copy / PSUM-evict |

> **CORRECTION — this kernel emits `activation(op=nl.exp)` (`0x21` ACT) on BOTH gen
> branches; it does NOT emit the standalone `0x30 EXPONENTIAL`.** The DX-CC-07 backing
> report read the `nc_version >= gen4` branch (L2332) as *"gen4+ → ACT `0x21`, older →
> standalone `InstExponential` `0x30 DVE`."* The OBSERVED source contradicts the second
> half: **both** arms call `nisa.activation(op=nl.exp, data=qk)` — gen4+ at L2349, older
> at L2359. The branch changes only **loop granularity** (gen4+ loops per-`s_prior` tile
> to pipeline; older does one broadcast `tensor_tensor` + one `activation`; comment L2331
> *"Instruction startup time on TRN2 does not outweigh pipelining advantages"*), **not**
> the exp opcode. The `0x30 EXPONENTIAL` lowering (`InstExponential`,
> [exponential](../firmware/kernels/exponential.md)) exists in the
> [BIR roster](bir-inst-roster.md#31-pooldve-elementwise--reduce) and is reachable via a
> standalone `nisa.exp`, but this attention kernel never takes it. A reimplementer tracing
> *this* kernel should expect `0x21` for every exponential, on every gen.
> `[HIGH/OBSERVED — `_compute_exp_qk` L2332/L2349/L2359; corrects DX-CC-07 §3.2]`

> **NOTE — the correction factor `exp(prev−curr)` is one fused ACT inst.**
> `_fa_update_correction_factor` (L1074) computes
> `exp(prev_running_max − curr_running_max)` as `activation(op=nl.exp, scale=−1.0,
> bias=…)` (L1079) — the affine `−1·curr + prev` folded into the PWL, one `0x21`.
> `[HIGH/OBSERVED]`

### 3.3 The engine assignment + online structure

| engine | ops |
|---|---|
| **ACT (Scalar)** | `exp(qk−max)` (3b), correction `exp` (4.2), all exponentials — `0x21` on every gen |
| **DVE (Vector)** | max-reduce, sum-evict (`0x42`); the −inf clamp (`0x43`); every running-stat `tensor_tensor` mul/add/max/sub (`0x41`); reciprocal (`0x48`); the copies (`0x46`) |
| **PE (Tensor)** | QKᵀ (step 1), the max-transpose-to-PSUM (2.2), the ones-vector sum (4.1), softmax@V (step 5) |
| **SB2SB / GpSimd** | the LNC2 `sendrecv` of running max/sum (`0xBF`), optionally `gpsimd_dma` (`use_gpsimd_sb2sb`) |

The **online/flash** form is the distinguishing structure: instead of {max over full
tensor; exp; sum over full tensor; divide}, it keeps a running `(max, sum, output)`
across FA tiles and rescales by `exp(prev_max − curr_max)`. So each tile is a bounded
max/exp/sum + two correction multiplies, and the final divide is one reciprocal +
multiply. This is *why* Softmax's DAG is **longer** than RmsNorm's but uses the **same
engine set** (ACT exp + DVE elementwise/reduce + PE matmul). **GpSimd Q7 float-op count:
0** — the only GpSimd appearance is, again, the optional `0xBF` LNC2 hop. `[HIGH/OBSERVED]`

> **GOTCHA — the SB2SB exchange pads partitions to a multiple of 16.** Before each
> `sendrecv`, the kernel pads the partition count to a `PARTITIONS_PER_GPSIMD_CORE`
> multiple: the comment *"Extended instructions require input/output tensors have
> multiple of 16 partitions"* sits at **L2101** (max), **L2410** (exp_sum), **L2670**
> (exp_v), each followed by `pad_partitions_for_ext_inst(…)` (def L3604). This is the
> 8-core × 16-partition GpSimd geometry: the SB2SB collective leg (`0xBF`, an *extended*
> instruction riding the GpSimd cores) only operates on 16-partition-aligned tiles. A
> reimplementer wiring the SB2SB hop must enforce the 16-partition alignment or the
> extended-inst decode rejects the tile. `[HIGH/OBSERVED — L2101/2410/2670]`

### 3.4 Tiling + data flow

**Partition axis** = `p_max` = 128 (the `s_active_bqh` / `d_head` partition). **Free
axis** = the `s_prior` / token tile. `qk` and `exp_v` are PE matmul results → drained
PSUM→SBUF; the running max/sum/output/correction are small SBUF tensors (shape
`[s_active_bqh_tile, n_bsq_tiles]`) held across **all** FA tiles. The transpose buffers
(max-to-PSUM) are the only transient PSUM tensors besides the matmul outputs.

```
QK(PE,PSUM)→SBUF → reduce-max(DVE) → [transpose(PE,PSUM)] → running-max(DVE) →
sub(DVE) → exp(ACT) → sum-ones-vec-matmul(PE,PSUM)→SBUF(DVE) → running-sum(DVE) →
PV(PE,PSUM)→SBUF → running-out(DVE) → [next tile] … → recip(DVE) → normalize(DVE) → HBM
```

`[HIGH/OBSERVED buffer alloc + `nl.psum` tags]`

---

## 4. Keystone #4 (worked) — MX quantize + MoE gate/up MX matmul

Two sources — the producer/consumer pair of the MX path
([the MX path page](mx-path.md) carries the end-to-end MX detail):

- **(a)** `nkilib/core/subkernels/rmsnorm_mx_quantize_tkg.py` — the fused
  (residual-add +) RMSNorm + MX-quantize: the **forward pack** that produces the fp8×4 +
  E8M0-scale MoE input.
- **(b)** `nkilib/core/moe/moe_tkg/gate_up_projection_mx.py` — the MoE gate/up projection
  that **consumes** the packed MX (`nc_matmul_mx`) and re-quantizes `gate·up` for the
  down projection.

**MX block geometry** (`projection_mx_constants.py`, byte-exact): `_q_height = 8` (L28),
`_q_width = 4` (L29) — comment L27 *"Trn3 quantization block shape for MXFP4/8"*; a
512-elem block = 128 partitions × 4 = `MAX_MATMULT_MX_UNPACKED_CONTRACT_DIM = 512`
(L59, *"128P x 4F"*); `MIN_MATMULT_MX_P_DIM = 8` (L58, *"at least 1× [8P, 4F] block"*).
`quantize_mx` accepts unpacked P ∈ `ALLOWED_QMX_UNPACKED_P_DIM = [128,256,384,512]` (L46)
→ packed P ∈ `ALLOWED_QMX_P_DIM = (32,64,96,128)` (L45). The SBUF quadrant geometry the
scale layout exploits: `SBUF_QUADRANT_SIZE = 32` (L25), `NUM_QUADRANTS_IN_SBUF = 4`
(L24), `SCALE_P_ELEM_PER_QUADRANT = 4` (L37); `MX_DTYPES = [float4_e2m1fn_x4,
float8_e4m3fn_x4, float8_e5m2_x4]` (L32). `[HIGH/OBSERVED — `projection_mx_constants.py`]`

### 4.1 The quantize-MX leg (`rmsnorm_mx_quantize_tkg`, after the §1 RmsNorm)

```
… [the §1 RmsNorm DAG, but with:
     dma_transpose load (0xBD);
     optional residual  tensor_tensor(add)  (0x41);
     square via  activation(square, bias=0)  (0x21) ] …
   ──▶ output[H0,BxS,H1]  (the normalized fp16/bf16 tensor)
```

Source `rmsnorm_mx_quantize_tkg.py` `rmsnorm_mx_quantize_tkg` (L29–382); LNC2-enforced
(`num_shards==2`, L107). Setup: `memset(reduction_const_matrix, 1.0)` (L121, the ones
matrix), `memset(eps_loaded, eps)` (L122), `memset(zero_bias, 0.0)` (L120); gamma
transpose-load `dma_transpose` (L128).

| step | line | `nisa.<op>` | role | → opcode | engine |
|---|---|---|---|---|---|
| input/residual load | L186/L189 | `dma_transpose` (+ optional residual `tensor_tensor(add)` L192) | HBM→SBUF transpose | `0xBD` (+`0x41`) | SDMA (+DVE) |
| square | L200 | `activation(op=nl.square, data=…, bias=zero_bias)` | x² | `0x21` | ACT |
| ×gamma → reduce → ones-matmul → rsqrt → norm | L204/L227/L230/L233/L237 | (the §1 RmsNorm DAG: `tensor_tensor` mul, `tensor_reduce` add axis=2, `nc_matmul`(ones)→`nl.psum`, `activation` rsqrt scale=1/hidden bias=eps, `tensor_tensor` mul) | normalize | `0x41`/`0x42`/`0x01`+`0x02`/`0x21`/`0x41` | DVE/PE/ACT |
| swizzle | L247 | `for h512_tile in …: for q∈affine_range(_q_width): tensor_copy(output → swizzled[H0,nH512,BxS,4])` | re-lay H1 into the 4-wide MX block | `0x46` COPY | DVE/Scalar |
| quantize | L254 | `quantize_mx(src=swizzled[…,0:_q_width], dst=output_quant⟨FP8x4⟩, dst_scale=output_scale⟨E8M0/uint8⟩)` | **forward** pack | `0xE3` QUANTIZE_MX | **DVE** |
| gather | L263/L270/L277 | 3× `sendrecv` (pipe 0/1/2): exchange `output`(unquant, for router topk) / `output_quant` / `output_scale` across LNC2 cores | LNC2 gather | `0xBF` SB2SB | GpSimd |

`[HIGH/OBSERVED — every row is a `nisa.<op>` in `rmsnorm_mx_quantize_tkg.py` at the cited
line; `final_reduced` is `nl.psum` (L152)]`

The `quantize_mx` forward pack (per-(8×4)-block max-exponent → E8M0 scale, src/scale
clipped, packed fp8×4) lowers to **`InstQuantizeMx` → `QuantizeMXOp` → `0xE3
QUANTIZE_MX` on the DVE** — the
[MX direction correction](bir-inst-roster.md#3-the-complete-bir-instisatpb-opcode-map):
forward `0xE3` (DVE), inverse `0x7B` TENSOR_DEQUANTIZE (POOL) is firmware-internal.

### 4.2 The MoE gate/up MX matmul leg (`gate_up_projection_mx_shard_I` + `_matmul_mx_accumulate`)

Source `gate_up_projection_mx.py`: `gate_up_projection_mx_shard_I` (L53–291), the matmul
helper `_matmul_mx_accumulate` (L452–491, loops `q_width_I_idx ∈ sequential_range(4)` ×
`tile_h ∈ sequential_range(n_H512_tiles)`), the clamp helper `_clamp_tensor` (L426–448).

| step | line | `nisa.<op>` | role | → opcode | engine |
|---|---|---|---|---|---|
| 3.1 | L485 | `nc_matmul_mx(dst=out_psum⟨bf16⟩, stationary=weight_sb + stationary_scale=weight_scale_sb, moving=input_quant_sb + moving_scale=input_scale_sb)` | W_mxfp4 @ input_mxfp8 | v4: `0x09`+`0x0A` / v5: `0x01`+`0x02` | **PE** |
| 3.2 | L186/L195 | `tensor_tensor(out_sb, out_psum, gate_bias_sb, nl.add)` **or** `tensor_copy(out_sb, out_psum)` | PSUM-evict + bias | `0x41` / `0x46` | DVE |
| 3.3 | L201 (→L441) | `_clamp_tensor` → `tensor_scalar(op0=nl.minimum, operand0=upper, op1=nl.maximum, operand1=lower)` | clamp (one 2-op inst) | `0x43` | DVE |
| 3.4 | L209 | `activation(op=get_nl_act_fn_from_type(hidden_act_fn))` *(gate; default `Swish`/silu)* | act fn | `0x21` | ACT |
| 4.1–3 | L230/L252/L267 | *(up projection: same 3.1–3.3 into `intermediate_sb`)* | | | |
| 4.4 | L274 | `tensor_tensor(out_sb, out_sb, intermediate, nl.multiply)` | gate · up | `0x41` | DVE |
| 4.5 | L285 | `quantize_mx(src=out_sb, dst=out_quant_sb⟨float8_e4m3fn_x4⟩, dst_scale=out_scale_sb⟨uint8⟩)` | re-quant → down | `0xE3` | DVE |

`[HIGH/OBSERVED — every row is a `nisa.<op>` in `gate_up_projection_mx.py` at the cited
line; `out_psum` is `nl.bfloat16, buffer=nl.psum` (L162/L228); the re-quant partition
count is padded to a valid {32,64,96,128} via `pad_to_valid_qmx_partitions` (L284)]`

### 4.3 The BIR/ISA/opcode join + the forward/inverse split

- **`nc_matmul_mx` → `InstMatmultMx` → `MatMulMXOp`** → **v4: `0x09` LDWEIGHTS_MX +
  `0x0A` MATMUL_MX**; **v5: folded into `0x01`/`0x02`** with MX_PERF_MODE (gen-routed,
  [mx-path](mx-path.md)). The stationary (weight) and moving (input) **each** carry a
  block scale (`stationary_scale` / `moving_scale`) — the two-level MX scaling. Engine
  **PE**. The **inverse dequant** (`0x7B` TENSOR_DEQUANTIZE, POOL/GpSimd) is
  **firmware-internal** to the MX matmul (it unpacks fp8 + E8M0 before the multiply) and
  has **no BIR/nisa producer** — the kernel never calls a dequant. So the MX path's BIR
  producers are **only** the forward `quantize_mx` (`0xE3`) and `matmul_mx`
  (`0x09`/`0x0A` or `0x01`/`0x02`); the `0x7B` dequant is the matmul's own internal step.
  `[HIGH/OBSERVED `nc_matmul_mx` + `quantize_mx` calls; CARRIED `0x09`/`0x0A`/`0x7B`/`0xE3`]`

- **`tensor_scalar(op0=min, op1=max)` → `InstTensorScalar` → `TensorScalar` → `0x43`
  TENSOR_SCALAR_ARITH.** The clamp is **one** `tensor_scalar` with two fused ALU ops:
  `min(x,upper)` then `max(·,lower)` (the dual-op accessors). `[HIGH/OBSERVED
  `_clamp_tensor` + CARRIED `0x43`]`

- **`activation(silu/gelu) → InstActivation → `0x21` ACTIVATE.** The SwiGLU/GeGLU gate
  nonlinearity is a PWL activation function. `[HIGH/OBSERVED]`

### 4.4 The engine assignment + tiling

| engine | ops |
|---|---|
| **PE (Tensor)** | `nc_matmul_mx` (gate, up) → `0x09`/`0x0A` (v4) \| `0x01`/`0x02` (v5); dst PSUM bf16 |
| **DVE (Vector)** | `quantize_mx` (`0xE3`); bias-add / `gate·up` `tensor_tensor` (`0x41`); clamp `tensor_scalar` (`0x43`); swizzle/PSUM-evict `tensor_copy` (`0x46`) |
| **ACT (Scalar)** | the gate activation (silu/gelu, `0x21`); the `square` in the RMSNorm leg |
| **SB2SB / GpSimd** | the LNC2 `sendrecv` of quant/scale (`0xBF`) |

**Tiling.** `out_psum[TILE_I, I_4, TILE_T]` bf16 is the matmul dst (PSUM); the MX
**data** and its **E8M0 scale** are co-resident in the **same 32-partition quadrant**
(the verifier-enforced MX layout — `dataStartPartition/32 == scalesStartPartition/32`,
the [QuantizeMX ×32-quadrant scale stride](sundaisel.md#8-the-quantizemx-scale-placement-layout-the-mx-leg)).
`T` is tiled by 256, `I` by 512 (the MX contract cap), `H` by 512 (the block size); the
`q_width(4)` loop walks the 4-wide MX block. `[HIGH/OBSERVED tile loops + CARRIED the MX
quadrant rule]`

**Data flow.**

```
weight_mxfp4(SBUF)+scale, input_mxfp8(SBUF)+scale ── matmul_mx(PE) ──▶ out_psum(bf16,PSUM)
   ── bias-add/copy(DVE) ──▶ out_sb(SBUF) ── clamp(DVE) ──▶ ── act(ACT) ──▶ gate
   (up the same into intermediate) ; gate·up(DVE) ──▶ quantize_mx(DVE)
   ──▶ out_quant(fp8x4) + out_scale(E8M0)  (SBUF) ──▶ down projection
```

`[HIGH/OBSERVED]`

---

## 5. The fused-CC engine/opcode matrix — and the "not-the-float-hot-path" conclusion

The recurring opcode vocabulary of the fused-CC kernels — every cell a `nisa.<op>`
OBSERVED in keystone source → BIR `Inst*` → TPB opcode:

| `nisa.<op>` | BIR `Inst*` | TPB opcode | engine | used in |
|---|---|---|---|---|
| `activation` | `InstActivation` | `0x21` ACTIVATE | ACT | RMS, SM, MoE (sq/rsqrt/**exp**/silu) — SM uses `0x21` for exp on *all* gens |
| `exponential` | `InstExponential` | `0x30` EXPONENTIAL | DVE | *(standalone `nisa.exp` only — **not** emitted by these keystones; see §3.2 CORRECTION)* |
| `tensor_reduce` | `InstTensorReduce` | `0x42` TENSOR_REDUCE | DVE | RMS, SM (sum/max) |
| `tensor_tensor` | `InstTensorTensor` | `0x41` TENSOR_TENSOR | **DVE** (fp) / GpSimd (i32) | RMS, SM, MoE (mul/add/sub/max) |
| `tensor_scalar` | `InstTensorScalar` | `0x43` TENSOR_SCALAR | DVE | MoE (clamp min/max) |
| `tensor_copy` | `InstTensorCopy` | `0x46` COPY / `0x47` CAST | DVE/Scalar | SM, MoE (swizzle/evict) |
| `reciprocal` | `InstReciprocal` | `0x48` RECIPROCAL | DVE | SM (1/sum) |
| `nc_matmul` | `InstMatmult` | `0x01` LDWEIGHTS + `0x02` MM | PE | RMS (ones), SM (QK, PV) |
| `nc_transpose` | `InstMatmult` (xpose) | `0x01`+`0x02` (identity) | PE | RMS, SM (cross-part xpose) |
| `nc_matmul_mx` | `InstMatmultMx` | `0x09`+`0x0A` (v4) / `0x01`+`0x02` (v5) | PE | MoE (gate/up MX matmul) |
| `quantize_mx` | `InstQuantizeMx` | `0xE3` QUANTIZE_MX | DVE | MoE, MX (forward pack) |
| `dma_transpose` | `InstDMADescriptorTranspose` | `0xBD` DMA_TRANSPOSE | SDMA/DGE | RMS, MX (HBM→SBUF xpose load) |
| `dma_copy` | `InstDMACopy` | `0xB8` DMAMEMCPY | SDMA/DGE | all (HBM↔SBUF stage) |
| `sendrecv` | `InstCollectiveSend`/`Recv` | `0xBF` SB2SB / `0xCB` | **GpSimd**/SP | RMS, SM, MoE (LNC2 xchg), AllReduce |
| `all_reduce` | `InstCollective`(AR) | `0xC7`/`0xC8`/`0xD9` PSEUDO | SP/SDMA | AllReduce (CCE reduce in SDMA) |

### 5.1 Count the engine columns — the conclusion falls out

Tally the **compute** ops (excluding the load/stage `0xBD`/`0xB8`) by engine across the
three TPB-engine keystones:

| keystone | ACT | DVE | PE | **GpSimd Q7 (float math)** | GpSimd (movement, `0xBF`) |
|---|---|---|---|---|---|
| **RmsNorm** (1 tile, no shard) | 2 (`0x21` sq, rsqrt) | 3 (`0x42`, `0x41`×2) | 1 (`0x01`+`0x02`) | **0** | 0 (1 if LNC2-sharded) |
| **Softmax** (1 FA tile) | ≥2 (`0x21` exp, corr) | ≥7 (`0x42`×2, `0x43`, `0x41`×5+, `0x48`, `0x46`) | 4 (QK, max-xpose, ones-vec sum, PV) | **0** | 0 (≥1 if LNC2) |
| **MX/MoE** (gate+up) | 1 (`0x21` gate) | ≥5 (`0xE3`, `0x41`×2, `0x43`, `0x46`) | 2 (`0x09`/`0x0A` gate, up) | **0** | ≥1 (`0xBF` gather) |

And **AllReduce** has **0** ops on any TPB compute engine — its reduce is the in-SDMA
CCE datapath, its only Q7 opcode the `0xBF` SB2SB hop.

**The conclusion — "GPSIMD is NOT the float hot-path"
([K2](../orientation/keystone-facts.md#k2--gpsimd-is-not-the-float-hot-path-and-it-cannot-reach-psum-at-all))
— is now arithmetic, not assertion.** Across all four keystones the **GpSimd Q7
float-arithmetic op count is exactly 0**. The float work is a **MATMUL/TRANSPOSE spine
(PE)** + an **ELEMENTWISE/REDUCE body (DVE)** + an **ACTIVATION/EXP layer (ACT)**. The
literal Q7 GpSimd engine appears only at:

1. the **SB2SB collective hop** (`0xBF`) — the LNC2 partial-stat exchange and the
   ring-step copy: *data movement*, not arithmetic; and
2. (not exercised by these keystones) the **int32 `add`/`sub`/`mul`** datapath (`0x41`
   on GpSimd) and the **gather/scatter/custom-op** lane (`0xF0` EXTENDED_INST).

The PSUM is touched **only** by the PE (matmul/transpose writes) and the ACT/DVE
PSUM-evict reads; no GpSimd op goes near PSUM — consistent with the
[GPSIMD-no-PSUM rule](../orientation/keystone-facts.md#k2--gpsimd-is-not-the-float-hot-path-and-it-cannot-reach-psum-at-all).
A reimplementation that targets float throughput on the Q7 cores — or that lets a
keystone kernel address PSUM from GpSimd — contradicts both the datapath and the
compiler's placement rule. `[HIGH — OBSERVED op DAGs; the engine pattern INFERRED-HIGH
from the routing rules]`

---

## 6. Cross-check ledger — this page vs the corpus

| claim (this page) | source (OBSERVED) | prior page tie | status |
|---|---|---|---|
| RmsNorm = sq(ACT)+reduce(DVE)+mul(DVE)+ones-matmul(PE)+rsqrt(ACT)+mul(DVE) | `rmsnorm_tkg.py` | [bir-inst-roster §4.2](bir-inst-roster.md#42-bir-ops-with-no-direct-opcode--macro--fan-out-highobserved) `TiledRmsNormOp` | ✔ |
| cross-part reduce = matmul-against-ones (hand-written) | `rmsnorm_tkg` `nc_matmul(1.0)` | [sundaisel §5.1](sundaisel.md#51-the-dispatch-transformpartitionreduceop-body--0x35020-wrapper--0x48420-observed) `matmultWithOnes` | ✔ |
| RmsNorm PSUM = only `final_reduced` | `nl.psum` tag in source | [tiling `InferPSumTensor`](tiling-memory-scheduling.md) | ✔ |
| fp elementwise → DVE; int32 → GpSimd | engine routing | [sundaisel §3.1](sundaisel.md#31-tensor_tensor-engine-resolution-tensor_opspy86124-observed-byte-exact) | ✔ |
| Softmax = online/flash (running max/sum) | `attention_tkg.py` | [bir-inst-roster §4.2](bir-inst-roster.md#42-bir-ops-with-no-direct-opcode--macro--fan-out-highobserved) `TiledSoftmaxOp` | ✔ |
| Softmax exp → ACT `0x21` on **both** gens (not `0x30`) | `_compute_exp_qk` L2349/L2359 | [exponential](../firmware/kernels/exponential.md) (standalone `0x30` unused here) | **CORRECTS DX-CC-07 §3.2** |
| Softmax tile-sum = ones-**vector** matmul (PE) | `_tile_sum_reduction` L2589 | [pe-matmul](../firmware/kernels/pe-matmul.md) | ✔ (NEW vs report's "`0x42` reduce") |
| reciprocal → `0x48` | `nisa.reciprocal` (L1348/L2555) | [bir-inst-roster §3.1](bir-inst-roster.md#31-pooldve-elementwise--reduce) `0x48` | ✔ |
| AllReduce → `0xC7`/`0xC8`/`0xD9` PSEUDO | `emit_all_reduce` | [collective-loadtime-rewrite](collective-loadtime-rewrite.md) | ✔ |
| reduce happens IN the SDMA (no Q7 loop) | the CCE descriptor path | [collective-loadtime-rewrite](collective-loadtime-rewrite.md) | ✔ |
| `sendrecv` → `0xBF` SB2SB | `nisa.sendrecv` calls | [sb2sb-remote-copy](../firmware/kernels/sb2sb-remote-copy.md) | ✔ |
| `quantize_mx` forward → `0xE3` DVE | `nisa.quantize_mx` | [mx-path §6.2](mx-path.md) (the correction) | ✔ |
| MX dequant `0x7B` = firmware-internal | no dequant in source | [mx-dequant](../firmware/kernels/mx-dequant.md) | ✔ |
| `nc_matmul_mx` → `0x09`/`0x0A` v4 / `0x01`/`0x02` v5 | `nisa.nc_matmul_mx` | [mx-path](mx-path.md) / [pe-matmul](../firmware/kernels/pe-matmul.md) | ✔ |
| MX data+scale co-resident in quadrant | gate_up tiling | [sundaisel §8](sundaisel.md#8-the-quantizemx-scale-placement-layout-the-mx-leg) | ✔ |
| clamp = `tensor_scalar` 2-op fused | `_clamp_tensor` | [bir-inst-roster §3.1](bir-inst-roster.md#31-pooldve-elementwise--reduce) `0x43` | ✔ |
| load via `dma_transpose` (axi2sram xbar) | `norm_tkg_utils` | [bir-inst-roster §3.4](bir-inst-roster.md#34-collective--dma--load-store--control--containers) `0xBD` | ✔ |

**Verdict: zero disagreements with the BIR roster (#889), SundaISel (#885), the opcode
ledger (#688), or the MX path — and two refinements of DX-CC-07** (both sharpening the
report against the OBSERVED source, not contradicting any opcode binding): (1) the
Softmax exp is `0x21` ACT on **both** gen branches, not `0x21`/`0x30` gen-split (§3.2
CORRECTION); (2) the Softmax tile-sum is a ones-**vector** PE matmul (L2589), the same
reduce-on-PE trick as RmsNorm, not a free-axis `0x42` reduce. The fused-CC macro
expansions the
[BIR roster §4.2](bir-inst-roster.md#42-bir-ops-with-no-direct-opcode--macro--fan-out-highobserved)
could only *name* are here *enumerated* as plaintext `nisa` op sequences; every op binds
to a roster BIR class + a ledger opcode; the engine routing matches SundaISel; the MX
direction matches the mx-path correction; the AllReduce path matches the collective
load-time rewrite. `[HIGH]`

---

## 7. Adversarial self-verify — the 5 strongest claims

Each claim is re-derived, attacked, resolved.

**(1) The RmsNorm cross-partition reduce is a PE ones-matmul (`0x01`+`0x02`), not a
GpSimd cross-lane reduce.**
*Attack:* the obvious way to sum 128 partitions is `0x7C`/`0x7D` CROSS_LANE_REDUCE on
GpSimd — is the matmul reading a coincidence?
*Resolve:* the kernel **issues `nc_matmul` against a `[H0,H0]=1.0` stationary matrix**
explicitly; `nc_matmul → InstMatmult → 0x01`+`0x02 (PE)` is unambiguous. And
[SundaISel §5](sundaisel.md#5-legalizepartitionreduce--the-cross-partition-reduce-correction)
proves the *compiler* never emits `0x7C`/`0x7D` for a macro partition-reduce either
(`cross_lane` grep count = 0 in both passes; add → matmul-vs-ones). The frontend and the
legalizer agree: the partition-sum is a PE op. **Verdict: PE ones-matmul, doubly
grounded.**

**(2) `rsqrt(x/hidden + eps)` is ONE `0x21 ACTIVATE`, not three ops.**
*Attack:* division, add, and rsqrt look like three datapath steps.
*Resolve:* the `nisa.activation` signature is `f(scale·x + bias)`
([activate-pwl](../firmware/kernels/activate-pwl.md)); `scale=1/hidden` and `bias=eps`
fold the affine into the PWL bucket evaluation, so the whole thing is one `InstActivation`
→ `ActivateOp` → `0x21`. The kernel passes exactly those kwargs. **Verdict: one ACT
instruction; the affine is free.**

**(3) `quantize_mx` (forward) is `0xE3` on DVE; `0x7B` is firmware-internal with no BIR
producer.**
*Attack:* an earlier reading bound `quantize_mx → 0x7B`. Could `0x7B` be a flag-variant
of `InstQuantizeMx`?
*Resolve:* the ledger rows `0xE3 QUANTIZE_MX | DVE` and `0x7B TENSOR_DEQUANTIZE | POOL`
are distinct opcodes on distinct engines; there is **no `InstDequantizeMx`** in the
[110-class roster](bir-inst-roster.md#2-the-full-110-class-bir-inst-roster); the kernel
calls `quantize_mx` (forward) and `nc_matmul_mx` (consume), **never** a dequant. The
`0x7B` dequant is the MX matmul's own internal unpack. **Verdict: forward `0xE3` DVE;
inverse `0x7B` POOL firmware-internal — matches the
[mx-path correction](mx-path.md).**

**(4) AllReduce runs NO TPB compute-engine op — the reduce is in the SDMA.**
*Attack:* surely a "GPSIMD collective" runs *some* GpSimd arithmetic?
*Resolve:* the lowering is `InstCollective(AR)` → PSEUDO `0xC7`/`0xC8`/`0xD9` (the
`0b110`-prefix RT-pseudo band, NRT-rewritten, never HW-decoded) → SDMA CCE descriptors;
the element-wise reduce is the in-SDMA `{ADD0,FMA1,MAX2,MIN3}` CCE op. The only Q7 opcode
in the schedule is the `0xBF` SB2SB *copy* hop — movement, not arithmetic. No
PE/DVE/ACT/GpSimd arithmetic op is emitted. **Verdict: 0 TPB compute ops; the reduce is
an SDMA-datapath operation.**

**(5) The GpSimd Q7 float-arithmetic op count across all four keystones is exactly 0.**
*Attack:* the workloads are literally called "GPSIMD kernels" — how can GpSimd do none of
the math?
*Resolve:* tally §5.1 — every float op resolves to ACT (`0x21`), DVE (`0x41`/`0x42`/
`0x43`/`0x46`/`0x48`/`0xE3`/`0x30`), or PE (`0x01`/`0x02`/`0x09`/`0x0A`) by the
[SundaISel fp-routing rule](sundaisel.md#31-tensor_tensor-engine-resolution-tensor_opspy86124-observed-byte-exact)
(fp → DVE/ACT/PE; only int32-no-PSUM → GpSimd). None of these keystones has an int32
arith op, a gather, or a custom-op. The sole GpSimd opcode that appears (`0xBF`) is a
collective *movement* hop. **Verdict: GpSimd float-math op count = 0 — the
"GPSIMD-kernel" name is a product label for a multi-engine micro-schedule, not a
statement about the Q7 datapath.**

---

## 8. Honesty ledger / confidence

**HIGH / OBSERVED** (read directly this pass from the shipped nkilib Python):

- The RmsNorm op DAG (`rmsnorm_tkg._process_rmsnorm_tile`): square / reduce / mul /
  `nc_matmul`-ones / rsqrt / mul — every `nisa.<op>` + the `nl.psum` tag on
  `final_reduced` + the memset-1.0 ones-matrix + the `shard_on_h` `sendrecv` partial-sum
  exchange.
- The Softmax op DAG (`attention_tkg` flash/online): `tensor_reduce` max,
  `nc_transpose`, the **ones-vector `nc_matmul` tile-sum** (L2589), `tensor_tensor`
  sub/mul/add/max, `activation` exp (`0x21` on **both** gens — the §3.2 CORRECTION) +
  correction, `reciprocal`, `nc_matmul` QK/PV, the `nc_version` *granularity-only* branch
  (L2332), the LNC2 `sendrecv` of running max/sum (L2126/L2438), the 16-partition pad.
- The MX quantize leg (`rmsnorm_mx_quantize_tkg`): `dma_transpose` load, residual
  `tensor_tensor`, square `activation`, the swizzle `tensor_copy` loop, `quantize_mx`,
  the 3× `sendrecv` gather.
- The MoE gate/up MX matmul (`gate_up_projection_mx` + `_matmul_mx_accumulate` +
  `_clamp_tensor`): `nc_matmul_mx` (stationary+scale, moving+scale), bias
  `tensor_tensor`, clamp `tensor_scalar`, `activation` gate, `gate·up` `tensor_tensor`,
  `quantize_mx`.
- The MX block geometry (`_q_width=4`, `_q_height=8`, 512-block, valid P {32..128}).
- The `norm_tkg_utils` load/transpose path (`dma_transpose` / `nc_transpose` / `dma_copy`).

**HIGH / CARRIED** (OBSERVED in a cited in-repo page, reused):

- Every `nisa.<op>` → BIR `Inst*` → TPB opcode binding
  ([bir-inst-roster §3](bir-inst-roster.md#3-the-complete-bir-instisatpb-opcode-map)).
- The engine routing fp→DVE / int32→GpSimd / matmul→PE / activation→ACT
  ([sundaisel §3](sundaisel.md#3-the-engine-polymorphic-decision--tensor_tensor--tensor_scalar)).
- The cross-partition reduce = matmul-against-ones
  ([sundaisel §5.1](sundaisel.md#51-the-dispatch-transformpartitionreduceop-body--0x35020-wrapper--0x48420-observed)).
- The MX direction forward `0xE3` DVE / inverse `0x7B` POOL ([mx-path](mx-path.md)).
- The AllReduce encoding + reduce-op chain + algorithm selection + decomposition + the
  in-SDMA CCE reduce + the `0xBF` SB2SB hop
  ([collective-loadtime-rewrite](collective-loadtime-rewrite.md),
  [sb2sb-remote-copy](../firmware/kernels/sb2sb-remote-copy.md)).
- The SBUF/PSUM geometry + the GPSIMD-no-PSUM rule + tiling/alloc/schedule
  ([tiling-memory-scheduling](tiling-memory-scheduling.md),
  [keystone-facts K2](../orientation/keystone-facts.md#k2--gpsimd-is-not-the-float-hot-path-and-it-cannot-reach-psum-at-all)).

**MED / INFERRED:**

- The **exact opcode order** `RmsNormCodeGen`/`SoftmaxCodeGen` emits per node was not
  disassembled this pass (the codegen `.so` is stripped). The §1–§4 sequences are
  recovered from the **frontend** kernel the NKI tracer runs to *build* the BIR node; the
  per-op opcode is the [roster §3](bir-inst-roster.md#3-the-complete-bir-instisatpb-opcode-map)
  binding. The 1:1 correspondence (kernel `nisa.<op>` == the BIR node the tracer builds
  == the §3 opcode) is INFERRED-HIGH from the NKI IR-builder model. A byte-level disasm
  of `RmsNormCodeGen` would *confirm* the exact emitted order, including any codegen
  fusion the source presents as two ops (e.g. an `activation`+`reduce` fold via the
  `activation` `.pyi` `reduce_op` param).
- Whether the v5 MX matmul truly folds to `0x01`/`0x02` vs keeps `0x09`/`0x0A` is the
  [mx-path](mx-path.md) gen-routing CARRIED claim; not re-confirmed against a v5 NEFF
  here. The v5/MoE-MX interiors are INFERRED.

**LOW:**

- The per-(engine,opcode) **cycle cost** the scheduler uses to order these sequences —
  the latency getters exist; the numerics are CARRIED.

---

## 9. Version anchors

- **neuronx-cc** `2.24.5133.0+58f8de22` — the nkilib Python kernels
  (`rmsnorm_tkg.py`, `norm_tkg_utils.py`, `rmsnorm_mx_quantize_tkg.py`,
  `attention_tkg.py`, `gate_up_projection_mx.py`, `projection_mx_constants.py`) + the
  cc-stubs `nki/isa/__init__.pyi` op signatures read this pass.
- The nkilib kernels are **gen-agnostic** Python. The per-gen opcode split (MX v4/v5; exp
  ACT/DVE by `nc_version`) is applied downstream at `SundaISel`/walrus and at the kernel's
  own `nc_version` branch. No silicon-generation fact is inferred from the kernel library.

---

## See also

- [The Penguin BIR Instruction Set + BIR→ISA Map](bir-inst-roster.md) — the
  `nisa.<op>` → `Inst*` → opcode bindings every row here joins against.
- [SundaISel Deep-Dive](sundaisel.md) — the fp→DVE / int32→GpSimd engine routing and the
  `matmultWithOnes` partition-reduce that the RmsNorm ones-matmul instantiates.
- [The Collective NRT-Load-Time Rewrite (byte-level)](collective-loadtime-rewrite.md) —
  the AllReduce PSEUDO → SDMA-CCE descriptor rewrite this page's §2 summarizes.
- [Keystone Facts — K2: GPSIMD is not the float hot-path](../orientation/keystone-facts.md#k2--gpsimd-is-not-the-float-hot-path-and-it-cannot-reach-psum-at-all) —
  the orientation statement of the §5 conclusion.
- [The MX Microscaling Path (end-to-end)](mx-path.md) — the full MX forward/inverse and
  MoE wiring the §4 keystone subsets.
- [Opcode Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) — the device-side
  opcode roster every numeric here is reconciled against.
