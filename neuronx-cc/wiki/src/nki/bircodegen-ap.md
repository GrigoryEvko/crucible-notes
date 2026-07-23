# BirCodeGenLoop Access-Pattern Builders

> *All symbols, addresses, and py-lines on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, module `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (ELF64, SHA-256 `6509029b…f29dfd`, **unstripped Cython carrying `debug_info`**). Other wheels differ; treat every address as version-pinned.*

## Abstract

`BirCodeGenLoop` is the **beta3** Penguin→BIR codegen driver — the layer that walks a `penguin.ir` tensoriser graph and emits `birpy.Instruction` objects directly (the beta3 alternative to the C++ `klr` path whose `codegenAP` is documented in [NeuronCodegen TensorOps](neuroncodegen-tensorops.md)). When a codegen routine attaches an operand or result to a BIR instruction it must hand it an **access pattern**: the `(stride, size)`-pair descriptor the hardware DMA/compute engines walk a tensor with. This page documents the *family of methods that construct those access patterns* — fourteen builders plus their three shared set-split cores — and the one architectural fact that distinguishes this layer from its `klr` twin.

The reference frame is the abstract `(stride, size)` model shared with the `klr` path: a Penguin access pattern is a `NeuronAP` built via `NeuronAP.createAP(addrs=…, access_shape=…, partition_ap=…)`, where `addrs` is the per-free-dim **stride** list, `access_shape` is the per-free-dim **count** list, and `partition_ap` is the separate SBUF partition-axis descriptor. A broadcast dim is a stride-0 entry. This `NeuronAP` is the *abstract* form; a downstream J-encoder lowers it to the concrete `TENSOR3D` / `MEM_PATTERN3D` wire descriptor ([Tensor4D / MemPattern4D encoding](../isa/tensor4d-mempattern4d.md)). This page builds the abstract AP.

The single fact that shapes the whole layer: **the access-pattern set-algebra runs here, at codegen, not upstream.** In the `klr` path, `codegenAP` (`0xf13f50`, 214 bytes) is a *pure pass-through* — it copies `klr::APPair {step:i32, num:u32}` verbatim into a `SmallVector<APPair,4>` with no reshaping, because the reduce-fold / double-row-pack / reshape arithmetic was already done in the upstream KLR IR ([NeuronCodegen TensorOps — `codegenAP`](neuroncodegen-tensorops.md)). In the Penguin path the same arithmetic is done *inside these builders* — `tonga_reduce_ap` splits the free/reduce sets, `cayman_matmul_double_row_ap` carves the size-2 pack dim, `reshapeAccessPattern` re-factors the free set — and each of those three set-split cores **owns its own hardware-legality assertion string** (proven below, [§6](#6-the-set-algebra-at-codegen-divergence)). The Penguin BIR leaf therefore carries materially more AP-construction logic than the `klr` leaf.

For reimplementation, the contract is:

- The `NeuronAP.createAP(addrs, access_shape, partition_ap[, dtype, tensor])` build-and-attach skeleton, and the four `*Access` kinds it dispatches over.
- The three set-split cores — `tonga_reduce_ap` (reduce fold), `cayman_matmul_double_row_ap` (gen3 2-row pack), `reshapeAccessPattern` (shape re-factor) — including the `split`/`flatten`/`nest`/`result_hull_size`/`is_singleton`/`start` vocabulary and the assertion gates that bound each.
- The auxiliary builders (`APNode`-over-interval fallbacks; the dynamic-dummy placeholder; the SB↔SB collective delinearizer) and *when* each is chosen.
- The divergence from the `klr` path and why it matters to a reimplementer porting between the two codegens.

| | |
|---|---|
| **Module** | `BirCodeGenLoop.cpython-310` (class `BirCodeGenLoop`), unstripped, `debug_info` |
| **Dispatch funnel** | `addInstToBir` `pw@0x063f30` → `dispatch_codegen` |
| **Access-kind union** | `Union[FullTensorAccess, NDimSubTensorAccess, TileAccess, OpaqueAccess]` (string pool) |
| **Build primitive** | `NeuronAP.createAP(addrs=stride, access_shape=count, partition_ap=…)` |
| **Set-split cores** | `tonga_reduce_ap` `pw@0x0ef020` · `cayman_matmul_double_row_ap` `pw@0x0ecf30` · `reshapeAccessPattern` `pw@0x1cd690` |
| **Tensor wraps** | `transformNeuronSBTensor` → `NeuronSBTensor` (§1/§3.1–3.3) · `NeuronBlockTensor` (§3.4 batch) |
| **Wire-form lowering** | downstream J-encoder → `TENSOR3D` / `MEM_PATTERN3D` ([Tensor4D/MemPattern4D](../isa/tensor4d-mempattern4d.md)) |
| **klr counterpart** | `codegenAP` `0xf13f50` — pure `APPair` pass-through ([NeuronCodegen TensorOps](neuroncodegen-tensorops.md)) |

> **GOTCHA —** Python line numbers for this module are not a reliable way to name a method: `addr2line` on a method's entry point and the DWARF `decl_line` of its `__pyx_mdef` table entry disagree (for `addReduceAP`, `3420` vs `3421`), and third-party line citations for these builders run several lines low. Anchor on the `pw@` address, which is exact in `nm`.

---

## 1. The build-and-attach skeleton and the four Access kinds

### Purpose

Every AP builder in this module is a variation on one 4-beat skeleton; the per-method work is the *per-axis `(stride, size)` derivation* between beats 2 and 3. Understanding the skeleton once makes every builder readable as "skeleton + this method's set arithmetic."

Every Penguin tensor reference is exactly one of four `*Access` kinds — the union string `Union[FullTensorAccess, NDimSubTensorAccess, TileAccess, OpaqueAccess]` sits in the binary's string pool, and each of the four kinds (except `FullTensorAccess`, whose AP is the whole-tensor contiguous default) has a dedicated builder: `addBIRKernelTileAccess` (§1.2), `addBIRKernelNDimSubTensorAccess` (§1.3), and `addOpaqueAP` (§4.1). A shared transform `transformNeuronSBTensor` maps the raw SBUF tensor to a `NeuronSBTensor` before the AP is built.

### Algorithm — the common skeleton

```c
// The 4-beat skeleton recurs in ~every builder. Names below are the resolved
// __pyx_n_s_* attribute/method symbols read from each decompiled body.
function build_AP_and_attach(self, birinst, access, isOutput):    // generic shape
    // 1. RESOLVE tensor + name
    t    = self.tensor(access)                                    // __pyx_n_s_tensor
    name = self.tensorname(access)                                // __pyx_n_s_tensorname
    // 2. DERIVE the FREE AP  (the method-SPECIFIC step: reduce / double-row / sparse / …)
    (addrs, access_shape) = derive_free_dims(access)              // varies per builder
    // 3. BUILD / PICK the PARTITION AP
    partition_ap = pick_partition_ap(access)                      // literal or tonga_partition_ap
    // 4. BUILD the NeuronAP and ATTACH it
    ap = NeuronAP.createAP(addrs=addrs, access_shape=access_shape, // __pyx_n_s_createAP
                           partition_ap=partition_ap)
    self.addArgumentOrOutput(birinst, ap, isOutput)               // __pyx_n_s_addArgumentOrOutput
    //   isOutput=False -> INPUT operand ; isOutput=True -> OUTPUT result
    //   (the BIR side of the addArgument/addOutput duality; see I19)
```

> **NOTE —** `addArgumentOrOutput` with the boolean `isOutput` is what makes one builder serve both operand and result attachment. `isOutput` and `addArgumentOrOutput` appear as resolved `__pyx_n_s_*` symbols in every builder body; `createAP`, `addrs`, `access_shape`, `tensor`, `tensorname`, `dtype` likewise.

### 1.1 `strip_fp32r` — the shared dtype-normalize toggle

Two of the matmul builders (`addDoubleRowAP` §3.3, `addBatchTransposeAP` §3.4) carry a `strip_fp32r` flag and a `{float32r → float32}` coercion block. `float32r` is TF32 ("fp32 reduced/round"); when `strip_fp32r` is set, an `access.dtype` of `np.float32r` is coerced to `np.float32` *before* the AP is built, so the descriptor records the **storage** dtype, not the compute dtype. The tokens `strip_fp32r`, `float32r`, `float32` appear in both bodies (`addDoubleRowAP` loads `strip_fp32r` at body line 191, `float32r` at 343, `float32` at 463). The same toggle is shared with the DMA builder `addComplicatedDMAAP` ([BirCodeGenLoop DMA Codegen](bircodegen-dma.md)).

### 1.2 `addBIRKernelTileAccess` — the tiled-access AP

`pw@0x1170e0` (2669-line body; `addr2line` → `BirCodeGenLoop.py:3376`). Signature `addBIRKernelTileAccess(self, birinst, access, src_shape, isOutput)`, the five args resolving as `__pyx_n_s_{self, birinst, access, src_shape, isOutput}`. Builds the AP for a `TileAccess` — a tile-grid sub-region of an SBUF tensor.

### Algorithm — `addBIRKernelTileAccess`

```c
function addBIRKernelTileAccess(self, birinst, access, src_shape, isOutput):  // pw@0x1170e0
    nsbt = NeuronSBTensor(access.tensor)        // body L368/L372: builtin/module getname
    lnc  = LncSize[lnc_size]                     // L514: per-arch LNC tiling factor
                                                 //   (Logical-NeuronCore partition scale)
    pdim = access.partition_dim                  // L1002: partition-axis index
    // --- (stride,size) DERIVATION: collapse the multi-dim tile via reduce(mul, ...) ---
    s0 = functools.reduce(mul, access_shape[..a]) // L1172 reduce + L1185 mul
    s1 = functools.reduce(mul, access_shape[..b]) // L1631 reduce + L1644 mul (TWO folds)
    elts = access.access_elts_per_dim_2          // L1526: the TILED per-dim count list
                                                 //   (the `_2` variant == Tile, not NDimSub)
    // --- shape <-> symbolic-AP consistency gate ---
    if access_shape_dims != symbolic_ap_dims:    // L1858 raise
        raise AssertionError(
            "BIR requires access shape and symbolic ap dim matching for access: " + access)
    // --- BUILD: 5-kwarg createAP ---
    ap = NeuronAP.createAP(addrs=access.addrs, dtype=access.dtype,
                           tensor=nsbt, access_shape=elts_folded,
                           partition_ap=part_ap)  // L1874 createAP
    self.addArgumentOrOutput(birinst, ap, isOutput)   // L1995
```

The two `reduce(mul, …)` folds (body L1172/1185 and L1631/1644) collapse the multi-dim tile into the descriptor's leading-dim stride and the flattened free count. The shape↔symbolic-AP assertion is the consistency gate: if the static `access_shape` and the symbolic-AP dim count disagree, codegen raises (`_Pyx_Raise(AssertionError)` at L1858; the format string is interned as `__pyx_k_BIR_requires_access_shape_and_sy`).

### 1.3 `addBIRKernelNDimSubTensorAccess` — the strided N-dim sub-tensor AP

`pw@0xc5c60` (2359-line body; `addr2line` → `BirCodeGenLoop.py:3336`). Same arg surface as Tile. Builds the AP for an `NDimSubTensorAccess` — an arbitrary strided rectangular slice (not a tile grid). **Structurally identical to §1.2** (same `NeuronSBTensor` wrap, `LncSize`/`lnc_size` partition scale, `partition_dim`, `reduce`/`mul` folds, `createAP(addrs, dtype, tensor, access_shape, partition_ap)`, `addArgumentOrOutput` tail) with two divergences:

| Axis | `addBIRKernelTileAccess` (§1.2) | `addBIRKernelNDimSubTensorAccess` (§1.3) | Confidence |
|---|---|---|---|
| Per-dim element source | `access_elts_per_dim_2` (the `_2` tiled list) | `access_elts_per_dim` (un-suffixed n-dim list) | CERTAIN |
| `reduce(mul,…)` folds | 2× `reduce` + 2× `mul` | 4× `reduce` + 4× `mul` | CERTAIN (token counts) |

The extra folds in the n-dim builder are consistent with an arbitrary-rank sub-tensor needing more `(stride, size)` collapses. The two are a Tile/sub-tensor pair over one `NeuronAP.createAP` build path; the only real difference is the per-dim count source list and the fold count.

---

## 2. The reduce-axis AP — `addReduceAP` + `tonga_reduce_ap`

### Purpose

`addReduceAP` builds the AP for a reduce instruction (`TensorReduce` / `PartitionReduce` / the `NeuronReduceMacro` family). It is the thin BIR wrapper; the real work — carving the reduce axes out of the free set and validating the hardware reduce constraints — is delegated to `tonga_reduce_ap`, the first of the three set-split cores.

### 2.1 `addReduceAP` — the wrapper

`pw@0x0f47f0` (1666-line body; `addr2line` → `BirCodeGenLoop.py:3420`).

### Algorithm — `addReduceAP` (token order = body program order)

```c
function addReduceAP(self, birinst, inst, isOutput, ...):   // pw@0x0f47f0
    n_red  = inst.min_red_set_dims              // L208: count of reduced axes
    src    = inst.src                           // L271
    rsize  = inst.reduce_size                   // L293: per-output reduce extent
    t      = self.tensor(inst); name = self.tensorname(inst)  // L365/391/396
    psize  = self.local_access_partition_size   // L559: partition-band size
    pap    = inst.partition_ap                  // L577
    fap    = inst.free_ap                       // L594: pre-reduce free-axis template
    dt     = access.dtype                       // L610
    // --- delegate the reduce-set carve to the set-split core ---
    reduced = self.tonga_reduce_ap(fap, pap, rsize, n_red, ...)  // L625
    ap = NeuronAP.createAP(addrs=..., access_shape=...)          // L813 createAP
                                                                 //   L830 addrs, L859 access_shape
    self.addArgumentOrOutput(birinst, ap, isOutput)             // L890
```

### 2.2 `tonga_reduce_ap` — the reduce set-split core

`pw@0x0ef020` (2642-line body; `addr2line` → `BirCodeGenLoop.py:3471`). It exists both as a bound method and as a free-standing module function `tonga_reduce_ap` at `pw@0x10d180` — the un-bound twin, both present in `nm`. Given a `NeuronAP`, it partitions the access dimensions into a FREE set and a REDUCE set and validates the hardware reduce constraints. The "tonga" prefix marks it the Trainium/Tonga-architecture reduce splitter.

The set-split vocabulary, in body program order: `partition_set` (L305) · `free_set` (L307) · `reduce_size` (L307) · `min_free_set_size` (L309) · `min_red_set_dims` (L309) · `split` (L368) · `flatten` (L566) · `nest` (L622) · `result_hull_size` (L654) · `is_singleton` (L1087) · `start` (L1117).

### Algorithm — `tonga_reduce_ap`

```c
function tonga_reduce_ap(self, neuron_ap, partition_set, reduce_size, min_red_set_dims, ...):
    // pw@0x0ef020 ; module twin pw@0x10d180
    // 1. minimum free size that must survive after carving the reduce axes
    min_free = compute_min_free_set_size(reduce_size)     // L120/297/.. min_free_set_size
    fset = neuron_ap.free_set                             // L307
    pset = neuron_ap.partition_set                        // L305
    // 2. SPLIT: trailing `reduce_size`-worth of free elements -> REDUCE sub-set,
    //           leading part stays FREE
    (free_part, reduce_part) = split(fset, reduce_size)   // L368 split
    // 3. re-shape the split sets into the canonical nested (stride,size) form
    free_part   = flatten(free_part)                      // L566 flatten
    reduce_part = nest(reduce_part)                       // L622 nest
    hull = result_hull_size(free_part, reduce_part)       // L654
    if is_singleton(reduce_part): collapse()              // L1087: size-1 reduced dim
    start = start_offset(free_part, pset)                 // L1117
    // --- HARDWARE-LEGALITY GATES (strings OWNED by this body) ---
    if not legal_partition(free_part, reduce_part):
        raise AssertionError("Incorrect access pattern!") // L1823/L1833
    if reduce_dim_count > min_red_set_dims_limit:
        raise AssertionError("Too many reduce dims")       // L1049
    return reduced_neuron_ap
```

> **GOTCHA —** "Incorrect access pattern!" does not mean a malformed input AP; it means the *split itself failed* — `reduce_size` was not factorable out of the `free_set`. A reimplementer who treats it as an input-validation error will look in the wrong place. The two assert strings (`Too many reduce dims` at L1049, `Incorrect access pattern!` at L1823/L1833) are loaded **inside this body** (4 `AssertionError` raises total), which is the direct evidence that the reduce set-algebra lives at codegen — see [§6](#6-the-set-algebra-at-codegen-divergence).

> **NOTE —** the *callers* that drive this path own related reduce strings but are not in this builder family: `codegenTransposeTensorReduceOp` ("…non-reduce free size must be size…"), `codegenTensorReduceOp` / `codegenPartitionReduceOp` ("There is not reduce mean!"), and `getReduceDim` (`_129`), which also loads "Too many reduce dims". The split arithmetic itself is here; those compute codegens call in.

The vocabulary order above is read directly from the body. The exact integer split arithmetic — precisely which trailing run becomes the reduce sub-set — is **INFERRED** from that vocabulary plus the two assertion gates.

---

## 3. The matmul-family AP — sparse / double-row / batch-transpose

### 3.1 `addSparseMatmulAP` — the compressed-matmul AP

`pw@0x0f2430` (1727-line body; `addr2line` → `BirCodeGenLoop.py:3444`). Penguin twin op `MatMulSparseOp` / `MatmultSparse`; the compute codegen is `codegenMatMulSparseOp`. Builds the AP for a matmul whose moving operand is compressed (structured sparsity).

### Algorithm — `addSparseMatmulAP`

```c
function addSparseMatmulAP(self, birinst, inst, isOutput, ...):   // pw@0x0f2430
    n_red = inst.min_red_set_dims                  // L217
    psize = self.local_access_partition_size       // L500
    pap   = inst.partition_ap                      // L523
    fap   = inst.free_ap                           // L545
    cr    = inst.compress_ratio_2                  // L586: the SPARSE compression ratio
    n_steps = derive_steps(cr)                     // L605: descriptor step count from cr
    ap_walk = neuron_ap.split(... n_steps ...)     // L642: strided walk over kept columns
    ap = NeuronAP.createAP(addrs=..., access_shape=...)   // L860
    self.addArgumentOrOutput(birinst, ap, isOutput)       // L947
```

The compression ratio `compress_ratio_2` (the `_2` variant; the un-suffixed `compress_ratio` / `set_compress_ratio` symbols also exist) drives `n_steps`, which drives a `split` that re-expresses the compressed contraction as a strided walk over the kept columns — the descriptor strides skip the pruned columns.

The body contains exactly **one** `split` attribute load, driven in a loop by `n_steps`; `reshapeAccessPattern` (§4.5) is built the same way. Static site count and runtime invocation count differ here — a single `split` GetAttr issues one call per step.

### 3.2 `addDoubleRowAP` — the gen3 double-row wrapper

`pw@0x1415a0` (1533-line body; `addr2line` → `BirCodeGenLoop.py:3505`). The BIR-side wrapper for TRN2/gen3 "double-row" 2-row PE packing. Double-row is an FP8/uint8 mode (`fp8e4m3` / `fp8e5m2` / `uint8`), so it strips any `fp32r` tag first (§1.1).

### Algorithm — `addDoubleRowAP`

```c
function addDoubleRowAP(self, birinst, access, isOutput, ...):   // pw@0x1415a0
    n_free = access.min_free_set_dims              // L189
    if self.strip_fp32r and access.dtype == np.float32r:  // L191/343
        access.dtype = np.float32                  // L463
    fap   = self.free_ap                           // L519
    pap   = access.partition_ap                    // L534
    psize = self.local_access_partition_size       // L610
    // --- delegate the 2-row pack geometry to the cayman core ---
    packed_pap = self.cayman_matmul_double_row_ap(fap, pap, psize, ...)  // L622
    ap = NeuronAP.createAP(... packed_pap ...)     // L715
    self.addArgumentOrOutput(birinst, ap, isOutput) // L820
```

### 3.3 `cayman_matmul_double_row_ap` — the 2-row pack geometry

`pw@0x0ecf30` (1717-line body; `addr2line` → `BirCodeGenLoop.py:3530`). The second set-split core. The gen3-Cayman packing interleaves two matmul rows into one PSUM pass; the pack axis is the trailing F dim of size 2. The "F dim must be 2" contract is enforced **upstream** (in `combine_trn2_double_row_matmult_tiles`, [BirCodeGenLoop Compute Codegens](bircodegen-compute.md)); what is enforced *here* is the pack stride legality.

The set-op vocabulary, in body order: `partition_set` (L230) · `min_free_set_dims` (L231) · `free_set` (L233) · `min_free_set_size` (L233) · `split` (L281) · `num_result_elts` (L288) · `flatten` (L570) · `nest` (L612) · `result_hull_size` (L643) · `step` (L713) · `double_row_stride_alignment` (L732) · `input_hull_size` (L1101). The assert token `incorrect_double_row` is at L789.

### Algorithm — `cayman_matmul_double_row_ap`

```c
function cayman_matmul_double_row_ap(self, neuron_ap, ...):    // pw@0x0ecf30
    fset = neuron_ap.free_set                     // L233
    pset = neuron_ap.partition_set                // L230
    pack = p_ob_base // 2                          // L346: PyInt_FloorDivide(..., 2, ...)
                                                   //   the size-2 pack divide
    (pack_dim, rest) = split(fset)                // L281: carve the size-2 pack dim
    n_elts = num_result_elts(pack_dim, rest)      // L288
    rest = flatten(rest); pack_dim = nest(pack_dim) // L570 / L612
    hull = result_hull_size(...)                  // L643
    step = compute_double_row_step(...)           // L713: the DOUBLE-ROW interleave stride
    align = self.target.double_row_stride_alignment // L732: per-arch required alignment
    // --- 2-row stride legality gate ---
    if (step % align) != 0:                       // L748: PyNumber_Remainder(step, align)
        raise AssertionError("incorrect double row step")  // L789
    in_hull = input_hull_size(...)                // L1101
    return packed_partition_set, packed_free_set
```

The legality gate is the sharpest part of the body: `PyNumber_Remainder` at L748 computes `step % double_row_stride_alignment`, and the "incorrect double row step" raise follows at L789. Tokens `double_row`, `double_row_set`, `double_row_gen3` cluster around this body.

> **GOTCHA —** the interleave stride is *not* a hardcoded `i*32+j` over a 32-wide PE column. The body holds a modulo check against the per-arch target attribute `double_row_stride_alignment` and a divide-by-2 for the pack dim; **no literal `32` appears in the arithmetic at all** (the only `0x20` constants are object-flag tests). Any `32`-granularity interleave model is **SPECULATIVE**; the alignment check is the real contract.

### 3.4 `addBatchTransposeAP` — the 32×32 batch-transpose AP

`pw@0x132e70` (3764-line body — the largest in the family; `addr2line` → `BirCodeGenLoop.py:3603`). Builds the AP for a batched 32×32 transpose, the `TransposeBatchnormStats2` / `TransposeTensorReduceOp` family. The transpose semantics are stated verbatim in the module's docstring pool:

```text
TransposeBatchnormStats2 first does a 32x32 transpose, where we transpose the
lowest dim of free_ap with partition_ap. Then TransposeBatchnormStats2 reduces
all free elements after the transpose.
```

Unlike §1–§3.3 (which wrap `NeuronSBTensor`), this builder resolves the tensor as `NeuronBlockTensor` — the batched/blocked SBUF tensor form (body L2923/L2927).

### Algorithm — `addBatchTransposeAP`

```c
function addBatchTransposeAP(self, birinst, access, isOutput, ...):   // pw@0x132e70
    if self.strip_fp32r and access.dtype == np.float32r:   // L452/658
        access.dtype = np.float32
    nbt = NeuronBlockTensor(access.tensor)                 // L2923/L2927
    bsz = access.batch_size                                // L450/L3258
    ysz = access.y_size                                    // L451/L3393
    n_in = access.num_input_elts                           // L1127
    psz  = access.partition_size                           // L1706
    dep  = access.depth                                    // L1609/L3078
    // --- the 32x32 transpose: swap partition_ap with lowest free dim ---
    a = split(free_ap)                                     // L1153
    b = split(a)                                           // L1302  (two split sites)
    if is_singleton(dim): collapse()                       // L1646
    ap = NeuronAP.createAP(addrs=..., access_shape=..., partition_ap=...)
    self.addArgumentOrOutput(birinst, ap, isOutput)
    // --- partition-set legality gates (strings OWNED by this body) ---
    //   "Incorrect access pattern!"   (shared with tonga_reduce_ap)
    //   "Incorrect partition set!"
    //   "too many partition dims! %s"
```

The three partition-set asserts — `Incorrect access pattern!`, `Incorrect partition set!`, `too many partition dims! %s` — are all loaded inside this body, which is the owning site for `Incorrect_partition_set`. They gate the partition-dim count and the validity of the partition↔lowest-free swap. The `batch_set` / `batch_group_count` tokens (the batch axis) also appear here.

---

## 4. Auxiliary builders — interval / opaque / stream-id / dynamic-dummy / SB↔SB / reshape

### 4.1 `addSeqAccess` & `addOpaqueAP` — the `APNode`-over-interval twins

`addSeqAccess` `pw@0x121230` (1355 L) and `addOpaqueAP` `pw@0x11f820` (1354 L) are near-identical builders that wrap a tensor in a single `APNode` over an `interval` — the **un-strided fallback**, used when there is no multi-dim `(stride, size)` descriptor, just one contiguous interval. Both share the same body shape (`APNode` at L594, `access_shape` at L705, `addArgumentOrOutput` at L749, `interval` at L368/372). They diverge only in the free-extent source:

| Builder | Free-extent source | Access kind | Confidence |
|---|---|---|---|
| `addSeqAccess` | `access.access_elts` (body L386) — the per-element list (`NeuronScalarAccess`/register form) | sequential element walk | CERTAIN |
| `addOpaqueAP` | `access.nelements` (body L386) — a single flat element count | `OpaqueAccess` (4th union arm) | CERTAIN |

`addOpaqueAP` is the builder for the `OpaqueAccess` arm of the §1 union and is paired with `codegenOpaqueCall`. The two builders write the *same offset* (L386) for their differing source attribute — a clean illustration that they are a copy-paste pair specialized only in the source field.

### 4.2 `addStreamId` — the collective stream-id stamp (not an AP builder)

`pw@0x095fd0` (667 L). Signature `addStreamId(self, inst, ccOp)`. **Not an AP builder** — a stream-id stamp:

```c
function addStreamId(self, inst, ccOp):                    // pw@0x095fd0
    streamid = ccOp.get_attr_default(<stream attr>, <default>)  // L178
    inst.setStreamId(streamid)                                  // L208
```

It pulls the stream id off a collective-channel op (`ccOp`, body L106) and stamps it on the BIR instruction, binding a DMA/collective inst to its hardware stream/queue id (the QoS/ordering channel). The body's entire name set is `{ccOp, inst, get_attr_default, setStreamId}`.

### 4.3 `addTensorCopyDynamicDummyAP` — the dynamic-offset placeholder AP

`pw@0x0fd440` (2819 L; `addr2line` → `BirCodeGenLoop.py:1944`). For a `TensorCopy` whose src or dst is dynamically addressed (a register / `AffineExpr` offset) and therefore cannot be statically resolved, this builds a *dummy placeholder* AP; the real address is supplied at runtime.

### Algorithm — `addTensorCopyDynamicDummyAP`

```c
function addTensorCopyDynamicDummyAP(self, inst, ...):     // pw@0x0fd440
    if inst.isScatter: (a, b) = (dst, src)                 // L317: scatter vs gather
    else:              (a, b) = (src, dst)
    apsize = access.access_partition_size                  // L437
    // PARTITION-AP SELECT (mirrors addComplicatedDMAAP, P11 step 5):
    if apsize / partition_dim spans beyond hull:           // L1514 partition_dim
        partition_ap = self.tonga_partition_ap(...)        // L525/L530: re-derived
    else:
        partition_ap = access.partition_ap                 // L559: literal
    idx   = AffineExpr(...)                                 // L1619/L1623: symbolic address
    shape = dummy access_shape                              // L1482: static placeholder
    ap = NeuronAP.createAP(addrs=..., access_shape=shape, partition_ap=partition_ap) // L1852
    self.addArgumentOrOutput(birinst, ap, isOutput)        // L1914
```

The dynamic index is an `AffineExpr` (the symbolic address); the AP's free extent is a static placeholder `access_shape`. The partition-AP select mirrors the DMA builder `addComplicatedDMAAP` ([BirCodeGenLoop DMA Codegen](bircodegen-dma.md)): when the access spans beyond the hull, the partition AP is re-derived via `tonga_partition_ap` instead of taken literally.

### 4.4 `add_sb_to_sb_cc_ap` (+ inner `add_delinearized_access`) — the SB↔SB collective AP

`pw@0x13b540` (1575 L). The on-chip GPSIMD SB-to-SB collective AP builder, used when `_has_cc_expr_in_access` (module fn `_1`) detects a collective-channel (replica/core) index in a DMA's access pattern. The outer method iterates `call.operands` (body L615) and `call.results` (body L857) — the transfer's src buffers and dst buffers — and invokes an inner closure for each. The inner closure `add_delinearized_access` lives in its own `pw@0x089210`, scoped as `__pyx_scope_struct_11_add_sb_to_sb_cc_ap`.

### Algorithm — `add_sb_to_sb_cc_ap` + inner

```c
function add_sb_to_sb_cc_ap(self, birinst, call, ...):     // pw@0x13b540
    for buf in call.operands:  add_delinearized_access(buf, isOutput=False)  // L615
    for buf in call.results:   add_delinearized_access(buf, isOutput=True)   // L857

function add_delinearized_access(access, isOutput):        // pw@0x089210 (inner closure)
    name  = self.tensorname(access)                        // L269/273
    elts  = access.access_elts                             // L364: flat element walk
    ds    = create_dataset(...)                            // L473/477: register collective buffer
    ap    = APNode(interval=access.interval,               // L399 APNode, L568 interval
                   access_shape=access.access_shape)        // L624
    self.addArgumentOrOutput(birinst, ap, isOutput)        // L672
```

A multi-dim collective access cannot use the plain multi-dim `NeuronAP`: the cross-core index is **delinearized** into a single flat `APNode`-over-interval walk (same form as §4.1), with `create_dataset` registering the collective buffer/dataset. The `setReplicaGroups` companion on the DMA side is the other half of this path ([BirCodeGenLoop DMA Codegen](bircodegen-dma.md)).

### 4.5 `reshapeAccessPattern` — the AP reshape primitive

`pw@0x1cd690` (1972 L; `addr2line` → `BirCodeGenLoop.py:3556`). The third set-split core and the shared helper that re-shapes a `NeuronAP`'s `free_set` into a target shape — called by `addComplicatedDMAAP` ([BirCodeGenLoop DMA Codegen](bircodegen-dma.md)) and the matmul/transpose builders.

### Algorithm — `reshapeAccessPattern`

```c
function reshapeAccessPattern(self, neuron_ap, reshape, ...):   // pw@0x1cd690
    target = reshape                              // L244
    fset   = neuron_ap.free_set                   // L246
    depth  = rank(reshape)                        // L289
    n_steps = ...                                 // L532/595
    step    = ...                                 // L582
    start   = ...                                 // L634
    n_in    = num_input_elts(fset)                // L797: INVARIANT across reshape
    // re-express the existing (stride,size) pattern over the new `reshape` shape
    new_fset = split(fset, n_steps)               // L818 (single split site, loop-driven)
    assert total_elts(new_fset) == n_in           // num_input_elts conserved
    return reshaped_neuron_ap
```

`reshapeAccessPattern` re-factors the `free_set`'s `(stride, size)` pairs into the `reshape` target via the `depth`/`n_steps`/`step`/`start` split, conserving `num_input_elts` (the total element count is invariant under reshape). It is the canonical "change the AP's logical shape without moving data" op — the multi-dim builder `addComplicatedDMAAP` feeds it before `createAP`.

---

## 5. The three AP representations

This layer emits three distinct AP representations, distinguished by the builder vocabulary:

| Representation | Built via | Builders | Free-dim model |
|---|---|---|---|
| **(a) `NeuronAP`** (multi-dim) | `NeuronAP.createAP` | `addBIRKernelTileAccess`, `addBIRKernelNDimSubTensorAccess`, `addReduceAP`, `addSparseMatmulAP`, `addDoubleRowAP`, `addBatchTransposeAP`, `addTensorCopyDynamicDummyAP` (+ P11 `addComplicatedDMAAP`) | `addrs`(stride) + `access_shape`(num) + `partition_ap`, optional `dtype`/`tensor` |
| **(b) `APNode`-over-interval** (un-strided) | `APNode(interval=…)` | `addSeqAccess` (`access_elts`), `addOpaqueAP` (`nelements`), inner `add_delinearized_access` (collective, + `create_dataset`) | single contiguous interval |
| **(c) set-split cores** (free/partition-set algebra) | feed (a) | `tonga_reduce_ap`, `cayman_matmul_double_row_ap`, `reshapeAccessPattern` | `split`/`flatten`/`nest`/`result_hull_size`/`is_singleton`/`start` |

The set-split cores (c) are not builders in their own right — they are the geometry engines that *produce* the reshaped `free_set`/`partition_set` consumed by the (a) `createAP` calls. They share the assert family `{"Incorrect access pattern!", "Too many reduce dims", "incorrect double row step", "Incorrect partition set!", "too many partition dims! %s"}`.

---

## 6. The set-algebra-at-codegen divergence

This is the architectural claim that defines the layer, and it is provable from the binary, not asserted.

### The klr leaf is a pure pass-through

In the `klr`/C++ path, `codegenAP` (`0xf13f50`, 214 bytes, [NeuronCodegen TensorOps — `codegenAP`](neuroncodegen-tensorops.md)) copies each `klr::APPair {step:i32, num:u32}` verbatim into a `SmallVector<APPair,4>` as `{step:i64, num:i64}` — a width promotion only, with no scaling; any scaling was applied by an upstream KLR cast. A broadcast dim is relayed verbatim as an `APPair` with `step==0`. There is **no** `split` / `flatten` / `nest` / `result_hull_size` in that function: the reduce-fold, double-row-pack, and reshape arithmetic was already done in the upstream KLR IR before codegen ever saw the pattern.

### The Penguin leaf does the set-algebra itself

Here, the same arithmetic is done *inside the AP builders* — and the proof is that **each set-split core owns its hardware-legality assertion string in its own decompiled body**:

| Set-split core | Owns assert string (body evidence) | What it carves |
|---|---|---|
| `tonga_reduce_ap` (`pw@0x0ef020`) | `Too many reduce dims` (body L1049), `Incorrect access pattern!` (L1823/L1833); 4 `AssertionError` raises | reduce sub-set out of the free set |
| `cayman_matmul_double_row_ap` (`pw@0x0ecf30`) | `incorrect double row step` (L789), gated by `step % double_row_stride_alignment` (`PyNumber_Remainder` L748) | size-2 double-row pack dim |
| `addBatchTransposeAP` (`pw@0x132e70`) | `Incorrect partition set!`, `too many partition dims! %s` | partition↔lowest-free 32×32 swap |

Each of those strings resolves to **exactly one owning method** across all 816 bodies in the module — plus the Cython string-tab initializer, which interns every constant and so is not a second owner. If the set-algebra were upstream, these asserts would live in the upstream KLR module, not in `BirCodeGenLoop`. They live here.

> **QUIRK —** because the set-algebra is at codegen in the Penguin path, a reimplementer porting a kernel between the beta2 (`klr`) and beta3 (`BirCodeGenLoop`) codegens must move the reduce-fold / double-row-pack / reshape logic *across the layer boundary*: in beta2 it is an IR-rewrite before codegen; in beta3 it is `tonga_reduce_ap` / `cayman_matmul_double_row_ap` / `reshapeAccessPattern` *during* AP construction. The same logical transform, two different homes. The abstract `NeuronAP` these builders produce is then lowered to the concrete `TENSOR3D` / `MEM_PATTERN3D` wire descriptor downstream by the J-encoder ([Tensor4D/MemPattern4D](../isa/tensor4d-mempattern4d.md)), which is the *only* stage that materializes the on-wire `(stride, size)` bytes.

> **NOTE —** `addReduceAP`, `addSparseMatmulAP`, `addDoubleRowAP` and `addBatchTransposeAP` are full independent `createAP` builders, each driving its own set-split core. They are peers of `addAP`, not thin wrappers around it.

---

## Builder Function Map

| Method | `pw@` (== body) | Body lines | Role | Confidence |
|---|---|---|---|---|
| `addInstToBir` | `0x063f30` | 553 | dispatch funnel → `dispatch_codegen` (builds no AP) | CERTAIN |
| `addBIRKernelTileAccess` | `0x1170e0` | 2669 | `TileAccess` AP (`access_elts_per_dim_2`, 2× reduce·mul) | CERTAIN |
| `addBIRKernelNDimSubTensorAccess` | `0xc5c60` | 2359 | n-dim sub-tensor AP (`access_elts_per_dim`, 4× reduce·mul) | CERTAIN |
| `addReduceAP` | `0x0f47f0` | 1666 | reduce-inst AP wrapper → `tonga_reduce_ap` | CERTAIN |
| `tonga_reduce_ap` | `0x0ef020` | 2642 | reduce set-split core (module twin `0x10d180`) | CERTAIN |
| `addSparseMatmulAP` | `0x0f2430` | 1727 | sparse-matmul AP (`compress_ratio_2` → `n_steps`) | CERTAIN |
| `addDoubleRowAP` | `0x1415a0` | 1533 | double-row wrapper → `cayman_*` | CERTAIN |
| `cayman_matmul_double_row_ap` | `0x0ecf30` | 1717 | 2-row pack geometry (`step % double_row_stride_alignment`) | CERTAIN |
| `addBatchTransposeAP` | `0x132e70` | 3764 | 32×32 batch transpose (`NeuronBlockTensor`) | CERTAIN |
| `addSeqAccess` | `0x121230` | 1355 | `APNode`-over-interval (`access_elts`) | CERTAIN |
| `addOpaqueAP` | `0x11f820` | 1354 | `APNode`-over-interval (`nelements`, `OpaqueAccess`) | CERTAIN |
| `addStreamId` | `0x095fd0` | 667 | stream-id stamp (not an AP) → `setStreamId` | CERTAIN |
| `addTensorCopyDynamicDummyAP` | `0x0fd440` | 2819 | dynamic-offset placeholder AP (`AffineExpr`) | CERTAIN |
| `add_sb_to_sb_cc_ap` | `0x13b540` | 1575 | SB↔SB collective; inner `add_delinearized_access` `0x089210` | CERTAIN |
| `reshapeAccessPattern` | `0x1cd690` | 1972 | AP reshape core (`free_set` re-factor) | CERTAIN |

All `pw@` addresses come from `nm -C` on `BirCodeGenLoop.cpython-310-…-gnu.so`. The "body lines" column is the decompiled body length, useful only as a rough size signal; the per-method `__pyx_n_s_*` line citations inside each `### Algorithm` are positions within those bodies.

---

## Related Components

| Name | Relationship |
|---|---|
| `dispatch_codegen` / `codegen<Op>` family | the compute codegens that *call* these AP builders to attach operands/results |
| `codegenMatMulSparseOp` / `codegenTransposeBatchnormStats2` | compute codegens whose APs are built by §3.1 / §3.4 |
| `getReduceDim` (`_129`) | also raises "Too many reduce dims"; drives `addReduceAP` |
| `addComplicatedDMAAP` (P11) | the DMA `createAP` builder; shares `strip_fp32r`, `tonga_partition_ap`, `reshapeAccessPattern` |
| J-encoder (`TENSOR3D` / `MEM_PATTERN3D`) | lowers the abstract `NeuronAP` to the on-wire descriptor |

## Cross-References

- [BirCodeGenLoop: the beta3 Penguin→BIR Driver](bircodegenloop.md) — the driver these builders run inside, and the module-wide py-line skew
- [BirCodeGenLoop Compute Codegens](bircodegen-compute.md) — the `codegen<Op>` routines that invoke these AP builders
- [BirCodeGenLoop DMA Codegen](bircodegen-dma.md) — P11: `createAP`, `addComplicatedDMAAP`, `reshapeAccessPattern`, the shared AP build path and the SB↔SB collective companion
- [NeuronCodegen TensorOps](neuroncodegen-tensorops.md) — the `klr` `codegenAP` (`0xf13f50`), the pure-pass-through twin whose upstream set-algebra is what this page does at codegen
- [Tensor4D / MemPattern4D encoding](../isa/tensor4d-mempattern4d.md) — the `TENSOR3D`/`MEM_PATTERN3D` wire descriptors the J-encoder emits from these `NeuronAP`s
- [Symbolic AP / Register ALU](../penguin/symbolic-ap-register-alu.md) — Part 5: the penguin-side `RegisterAccessPattern` model these builders feed
