# BirCodeGenLoop DMA and Indirect Codegens

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310 wheel), module `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (BuildID `cb19fae04f37f9a7ef7acc610b6d32d36ac816f1`, 11,139,256 B, `with debug_info, NOT stripped`). Other versions and the cp311/cp312 twins will differ in address but not in shape. Provenance: D-P11.*

## Abstract

This page documents the **data-movement half** of the beta3 BIR code generator — the
`BirCodeGenLoop` layer that lowers Penguin tensoriser-IR DMA and indirect ops into
`birpy.Instruction` nodes. It is the third layer of the NKI three-layer stack: a NKI
kernel is traced in Python to `penguin.ir`, then `penguin.ir` is lowered to BIR by one of
two parallel emitters — the beta3 `BirCodeGenLoop` path documented here, or the older
beta2 `klr` → `KlirToBirCodegen` C++ path in `libwalrus`. Both terminate at the **same**
BIR L1 node family (`DMACopy`, `GenericIndirectLoad`/`Save`, `IndirectCopy`, `PoolGather`);
they are siblings, not stages of a pipeline. The compute side of this layer (matmul,
activation, reduce) lives on [BirCodeGenLoop Compute Codegens](bircodegen-compute.md); the
driver dispatch lives on [The BirCodeGenLoop Driver](bircodegenloop.md).

Because `BirCodeGenLoop.cpython-310.so` is an unstripped Cython module, every codegen method
carries a `_Pyx_AddTraceback("…BirCodeGenLoop.<method>", <pyline>, …)` anchor and resolves
its attribute/method names as named `_pyx_mstate_global_static.__pyx_n_s_<name>` loads. The
exact emit order — which setter is called, in which sequence, on which operand — is therefore
**byte-recoverable from the decompile**, not inferred from vocabulary. The whole DMA family is
present as decompiled Hex-Rays bodies in the IDA sidecar; this page is grounded against those
bodies, the module `_strings.json`, and the `nm`/`function_addresses` symtab.

The structurally interesting findings are concentrated in **`addComplicatedDMAAP`** (the
multi-dimensional / delinearised DMA access-pattern builder) and the
`codegenNdDMAAP` decision that routes a DMA to the *simple* (`addAP`) versus *complicated*
(`addComplicatedDMAAP`) AP path. That decision — "what makes a DMA complicated" — and the
front-loading of the multi-dim descriptor build *into codegen* (rather than into a downstream
strided-DMA legalization pass, as the klr path does) are the novel behaviors of this layer.

For reimplementation, the contract is:

- The **two-class emit model**: each `codegen<Op>` is an *override* — the BIR `Instruction` is
  allocated and named by the auto-generated `BirCodeGenLoopGen` base class via `super()`; the
  override here only sets the DMA-specific attribute surface (mode, DGE, OOB, QoS, indirect
  dims, access patterns).
- The **simple-vs-complicated AP gate** in `codegenNdDMAAP` and the full
  `addComplicatedDMAAP` algorithm (reshape → fp32r-strip → partition-AP select →
  `NeuronAP.createAP` → `addArgumentOrOutput`).
- The **indirect family**: `codegenNeuronIndirectSave` handling both scatter-overwrite and
  scatter-accumulate (RMW), with `codegenNeuronIndirectRMW` a three-line trampoline into it;
  the `is_offset_dma` fork to register/dynamic-offset DMA.
- Which BIR `Opcode` each path emits and where the QoS and OOB knobs are bound — and the
  fact that the **wire descriptor is NOT chosen here** but downstream in the J-encoder.

| | |
|---|---|
| **Module** | `BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (codegen/, unstripped) |
| **Input IR** | Penguin `DMACopyOp` / `NeuronIndirect{Load,Save,RMW}` / `IndirectCopy` / `PoolGather` / `DMATranspose{,Indirect}` |
| **Output IR** | `birpy.Instruction` — Opcode `DMACopy` / `GenericIndirect{Load,Save}` / `IndirectCopy` / `PoolGather` |
| **Emit model** | Gen-base `super()` allocates+names the node; impl override sets DMA attrs |
| **AP gate** | `codegenNdDMAAP` @ `0x1433b0` (py 1662) — simple `addAP` vs `addComplicatedDMAAP` |
| **Richest fn** | `addComplicatedDMAAP` @ `0x169db0` (py 3574), ~12.8 KB body |
| **Parallel emitter** | beta2 klr `KlirToBirCodegen` (C++/libwalrus) — see [Cross-References](#cross-references) |

---

## 1. Foundation — the DMA/Indirect Roster and the Two-Class Emit Model

### Purpose

Eleven `BirCodeGenLoop` methods make up the data-movement family. Each is registered in the
module `PyMethodDef` table and exists as a Cython `__pyx_pw_…` wrapper (the single-argument
methods inline their body into the wrapper). The roster, with the verified address, the
`_Pyx_AddTraceback` body pyline, and role:

| Idx | Method | Address | Body pyline | Role |
|---|---|---|---|---|
| `_125` | `codegenDMACopyOp` | `0x0e8e10` | 1649 | Penguin `DMACopyOp` → bir `DMACopy` (plain space↔space) |
| `_63` | `codegenDMATranspose` | `0x06ded0` | 1111 | `DMATranspose` → `DMACopy` (transpose flavour) |
| `_65` | `codegenDMAIndirectTranspose` | `0x0b0150` | 1135 | gather+transpose fused DMA |
| `_127` | `codegenNdDMAAP` | `0x1433b0` | 1662 | the per-operand N-dim AP builder / simple-vs-complicated gate |
| `_291` | `addComplicatedDMAAP` | `0x169db0` | 3574 | multi-dim / delinearised AP descriptor build |
| `_145` | `codegenNeuronIndirectLoad` | `0x1b4e10` | 1816 | index-vector gather load |
| `_147` | `codegenNeuronIndirectSave` | `0x158510` | 1859 | index-vector scatter — handles Save **and** RMW |
| `_157` | `codegenNeuronIndirectRMW` | `0x063430` | 1985 | 3-line trampoline → `codegenNeuronIndirectSave` |
| `_149` | `codegenIndirectCopy` | `0x1c4d40` | 1926 | on-engine local gather → `IndirectCopy` |
| `_203` | `codegenPoolGather` | `0x055ad0` | 2553 | Pool-engine gather → `PoolGather` |
| `_143` | `codegenNeuronReadTensorPtr` | `0x1bd150` | 1810 | dynamic-offset tensor-ptr read |
| `_107` | `codegenNeuronPrintInst` | `0x1aaae0` | 1540 | on-device print/dump |

Supporting functions, all CONFIRMED in `function_addresses`:

| Idx | Function | Address | Role |
|---|---|---|---|
| `_77` | `add_sb_to_sb_cc_ap` | `0x13b540` | SB↔SB collective access-pattern builder (+ inner `add_delinearized_access` @ `0x89210`) |
| `_1` | `_has_cc_expr_in_access` | `0x07b930` | module-level fn (py 69) — collective-channel detector |
| `_293` | `addBatchTransposeAP` | `0x132e70` | rank-4 batch-transpose AP build |
| `_289` | `reshapeAccessPattern` | `0x1cd690` | canonical multi-dim AP reshape (called by `addComplicatedDMAAP`) |

> **CORRECTION (D-P11) —** the backing report's §0.1 roster cited "source lines" that are the
> `def`-line of each method, whereas the `_Pyx_AddTraceback` anchor in the decompiled body
> emits the line of the *first call inside the method body* — they differ by a small skew
> (e.g. `addComplicatedDMAAP` def cited as py 3581 vs the verified body anchor py 3574;
> `codegenNeuronIndirectRMW` def unstated vs body anchor py 1985). This page cites the
> **body-anchor** pyline throughout, since that is the line directly read from the binary.
> A reimplementer correlating to a `.py` listing should expect the method `def` a few lines
> *above* the anchors quoted here.

### The two-class emit model

Every `codegen<Op>` in this module is an **override** of the same-named method on the
auto-generated parent class (the `generated/BirCodeGenLoopGen` module). The override first
calls the parent, which allocates and names the bare `birpy.Instruction`; the override then
configures only the DMA-specific attributes. This is byte-confirmed in `codegenDMACopyOp`:

```text
0xe8e10  __pyx_pw_…_125codegenDMACopyOp
  line 598:  v23 = _pyx_builtin_super              // bind the super proxy
  line 604:  PyObject_Call(super, (DMACopyOp_cls, self))
  line 613:  getattr(super_obj, codegenDMACopyOp)  // parent allocates+names the bir node
  ── from here the override sets its own attrs ──
  line 653:  bb.setmode(…)
  line 697:  bb.set_dge_type(…)
  line 793:  cpy.dma_qos  →  line 806: bb.dma_qos = …   (attribute assignment)
  line 890:  bb.setReplicaGroups(…)
  line 921:  self.codegenNdDMAAP(bb, cpy)
```

> **QUIRK —** this divergences from the klr/C++ path. In `KlirToBirCodegen` the leaf both
> *allocates* the bir node (`insertElement` / `addUnaryInstruction`) **and** sets the attrs.
> Here the allocation is entirely in the Gen super-class; the impl override does only the
> attribute surface. A reimplementer who folds allocation into the override will produce a
> double-allocated or unnamed node. The `super()` call is mandatory and must come first.

The method receives `(self, <op>, bb)`: `<op>` is the Penguin Inst (`cpy`/`transpose`/`inst`)
and `bb` is the BIR `Instruction` handle just allocated by the parent. Despite the name `bb`,
this is the instruction handle, not a basic block.

### BIR opcode and enum tokens used by this family

The following tokens are all present in the module `_strings.json` (CONFIRMED, one hit each):

```text
DMACopy   GenericIndirectLoad   GenericIndirectSave
IndirectCopy   PoolGather
DMAQoSClass   DGEMode   OOBMode   ALUOpcode   birEngineType   DebugDeviceBuffer
setTransposeOp   set_dge_type   set_indirect_dims   setCceOp   setOobIsErr
```

> **NOTE —** the descriptor *wire-struct* names — `TENSOR1D`/`TENSOR3D`, `MEM_PATTERN2D`/
> `MEM_PATTERN3D`, `INDIRECT16B`/`INDIRECT20B`, `ADDR4` — are **absent** from this binary
> (grep count 0 for each). This layer emits the abstract `birpy` AccessPattern (`NeuronAP`
> built via `createAP`/`addAP`); the concrete wire descriptor is picked **downstream** by the
> J-encoder from dim-count + dtype + DGE. See [Tensor Descriptors](../isa/tensor-descriptors.md)
> and [MemPattern 2D/3D](../isa/mempattern-2d-3d.md).

---

## 2. codegenDMACopyOp — the Plain Space↔Space DMA

### Purpose

Lowers a Penguin `DMACopyOp` (which by the time it reaches here is the plain space↔space copy
— the indirect/gather forks branched off upstream in `NeuronCodegen`) into a bir `DMACopy`
instruction. It is a thin configurator: it sets mode, DGE type, QoS, and replica groups, then
delegates the actual access-pattern construction to `codegenNdDMAAP`.

### Algorithm

```c
function codegenDMACopyOp(self, cpy, bb):          // 0xe8e10, body py1649
    super().codegenDMACopyOp(cpy, bb)              // py1649 — Gen base allocates+names bb
    bb.setmode(COPY_MODE_TUPLE)                    // py1651 — plain-copy CopyMode (const tuple)
    bb.set_dge_type(convert_to_bir_type(cpy.dge_mode))   // DGEType from Penguin DGEMode
    bb.dma_qos = cpy.dma_qos                        // py1654 — QoS bound HERE (tp_setattro)
    bb.setReplicaGroups(...)                        // collective replica groups
    self.codegenNdDMAAP(bb, cpy)                    // py1662 — build + attach src/dst APs (§4)
    has_cc_expr_in_access(...)                       // collective-channel-expr gate (§6)
```

The `dma_qos` binding is not a setter call but an attribute *assignment* on the bir node:
the decompile reads `cpy.dma_qos` via `tp_getattro` (line 793) and assigns it onto `bb` via
`tp_setattro(bb, "dma_qos", …)` (line 806).

> **QUIRK (QoS divergence) —** the beta3 `BirCodeGenLoop` path **sets QoS here**, whereas the
> beta2 klr leaf `codegenNcDmaCopy` left QoS at its default (the field at `+0x1D8` was never
> written) and relied on a later default. This is not a contradiction — the two front-ends
> differ in *where* QoS is bound; both feed the same downstream QoS encoder. A reimplementer
> targeting beta3 must read and propagate `dma_qos` at codegen.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `codegenDMACopyOp` | `0x0e8e10` | plain DMA configurator | CERTAIN |
| `convert_to_bir_type` | (module fn) | Penguin `DGEMode` → birpy `DGEType` | CERTAIN |
| `codegenNdDMAAP` | `0x1433b0` | AP construction delegate (§4) | CERTAIN |

> **NOTE —** the literal value of the `CopyMode` mode tuple (the constant `__pyx_tuple__`
> passed to `setmode`) was not byte-decoded; it is INFERRED to be the `Copy` form by parity
> with the klr path's `CopyMode +0x128 = Copy(0)`. The fact that `setmode` is called and the
> call ordering are CONFIRMED.

---

## 3. DMA Transpose Codegens

### Purpose

Two methods lower transpose-flavour DMAs. `codegenDMATranspose` is the straight transpose;
`codegenDMAIndirectTranspose` is the gather+transpose fusion (the upstream Penguin op carrying
an `indirect_tensor_ref` on its source). Both emit Opcode `DMACopy` with a transpose mode and
are pinned to the Pool engine. The transpose permutation (the XZYW / `[3,1,2,0]` 4-D axis
swap) is baked into the `setTransposeOp` constant — it is not recomputed here.

### Algorithm — codegenDMATranspose

```c
function codegenDMATranspose(self, transpose, bb):   // 0x6ded0, body py1111
    super().codegenDMATranspose(transpose, bb)        // py1111
    bb.setmode(TRANSPOSE_MODE_TUPLE)                  // CopyMode = transpose form
    bb.setTransposeOp(XZYW_CONST)                     // the [3,1,2,0] axis perm, baked
    bb.set_dge_type(convert_to_bir_type(transpose.dge_mode))
    bb.dma_qos = transpose.dma_qos
    y = transpose.y_size                               // transpose tile Y extent
    self.addBatchTransposeAP(bb, transpose.src, batch_size)   // src rank-4 transpose AP
    self.addBatchTransposeAP(bb, transpose.dst, batch_size)   // dst rank-4 transpose AP
```

`addBatchTransposeAP` (`0x132e70`) builds the rank-4 transpose access pattern from
`{y_size, batch_size}` — the batched 2-D-tile-stacked transpose. The names
`addBatchTransposeAP`, `batch_size`, `y_size`, `setTransposeOp`, `dma_qos` are all CONFIRMED
in the decompiled body.

### Algorithm — codegenDMAIndirectTranspose

```c
function codegenDMAIndirectTranspose(self, transpose, bb):   // 0xb0150, body py1135
    super().codegenDMAIndirectTranspose(transpose, bb)        // py1135
    bb.setmode(TRANSPOSE_MODE_TUPLE)                          // CopyMode = transpose form
    bb.setTransposeOp(XZYW_CONST)
    bb.set_dge_type(convert_to_bir_type(transpose.dge_mode))
    bb.set_indirect_dims(transpose.generic_dims)              // the gather index-dim coefficients
    bb.set_engine(birEngineType.Pool)                          // PINNED to Pool engine
    bb.dma_qos = transpose.dma_qos
    bb.setOobIsErr(OOBMode.SKIP)                               // gather OOB → skip
    y = transpose.y_size
    self.addBatchTransposeAP(bb, transpose.src, batch_size)   // src transpose AP
    self.addAP(bb, transpose.generic_addrs)                    // per-index base-address AP
    self.addBatchTransposeAP(bb, transpose.dst, batch_size)   // dst transpose AP
```

So `DMAIndirectTranspose = DMATranspose + {set_indirect_dims(generic_dims),
addAP(generic_addrs), Pool-pin, OOB-skip}` — it fuses the §4 indirect index AP into the §3
transpose. The names `birEngineType`, `Pool`, `set_engine`, `set_indirect_dims`,
`generic_dims`, `generic_addrs`, `OOBMode`, `SKIP`, `setOobIsErr` are CONFIRMED in the body.

> **NOTE —** the rank-4 requirement of the transpose AP and the precise XZYW constant value
> are asserted upstream / in the Gen base; here the override only sets `setTransposeOp` to the
> fixed constant. The `[3,1,2,0]` permutation matches the Penguin `dma_transpose` 4-D axes and
> the klr-path `TransposeOps = XZYW(11)`. See [DMA Encoding](../isa/dma-encoding.md).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `codegenDMATranspose` | `0x06ded0` | straight transpose DMA | CERTAIN |
| `codegenDMAIndirectTranspose` | `0x0b0150` | gather+transpose fusion | CERTAIN |
| `addBatchTransposeAP` | `0x132e70` | rank-4 batch-transpose AP | CERTAIN |

---

## 4. codegenNdDMAAP and addComplicatedDMAAP — the AP Gate and Complicated-DMA Build

This is the central unique finding of the layer. Every DMA codegen routes its operand
access-pattern construction through `codegenNdDMAAP`, which per-operand chooses the **simple**
AP (`addAP`) versus the **complicated** AP (`addComplicatedDMAAP`).

### 4.1 codegenNdDMAAP — "what makes a DMA complicated"

```c
function codegenNdDMAAP(self, birinst, inst, strip_fp32r=False):   // 0x1433b0, body py1662
    n_src = len(inst.src.free_ap.step_n_steps)        // py1663 — # of strided free dims (src)
    n_dst = len(inst.dst.free_ap.step_n_steps)        // py1664 — # of strided free dims (dst)
    shape, n_strides = extract_offloaded_memcpy_shape_block(src, dst)   // py1666
        // collapses a contiguous-block DMA expressed as many strides into its true
        // {shape, stride-count}

    if shape_is_nontrivial(shape):                    // py1667 — the COMPLICATED predicate
        self.addComplicatedDMAAP(birinst, inst.src, ...)   // py1668
        self.addComplicatedDMAAP(birinst, inst.dst, ...)   // py1669
    else:
        assert n_strides <= self.target.neuron_max_strides     // py1667
            // else: raise AssertionError "Cannot legalize strided load!"
            //       ("…store!" in the indirect/save path)
        self.addAP(birinst, inst.src, ...)            // py1666 — the simple ≤max_strides AP
        self.addAP(birinst, inst.dst, ...)
```

The names `extract_offloaded_memcpy_shape_block`, `step_n_steps`, `free_ap`,
`neuron_max_strides`, `target`, `addAP`, `addComplicatedDMAAP`, `strip_fp32r` are all
CONFIRMED in the decompiled body.

**The complicated-DMA definition.** A DMA is *complicated* when its free-axis access pattern,
after the offloaded-memcpy collapse, cannot be expressed as a single standard strided AP
within `self.target.neuron_max_strides` descriptor strides — i.e. it is a
multi-dimensional / over-strided / delinearised pattern. The simple case (`n_strides ≤
neuron_max_strides`) takes the plain `addAP`; otherwise the multi-dim descriptor is built by
`addComplicatedDMAAP`. If *neither* fits, codegen raises `AssertionError("Cannot legalize
strided load!")` (or `…store!`).

> **GOTCHA —** `neuron_max_strides` is a per-arch `self.target` capability, **not** a constant.
> A reimplementer hard-coding a stride budget will produce DMAs that legalize on one Trainium
> generation and assert on another. The budget must come from the target descriptor.

> **NOTE —** `strip_fp32r` (default `False`) toggles the float32r → float32 dtype
> normalization performed inside `addComplicatedDMAAP` (§4.2 step 2). It is threaded through
> `codegenNdDMAAP` from the caller.

### 4.2 addComplicatedDMAAP — the multi-dim descriptor build

This is the ~12.8 KB body that builds the multi-dimensional / delinearised AP that the simple
`addAP` cannot express. The ordered emit, read directly from the decompiled name-load sequence
(decompile line numbers in comments), with body pyline 3574:

```c
function addComplicatedDMAAP(self, birinst, access, reshape, isOutput, strip_fp32r=False):
    // 0x169db0, body py3574; signature kwargs isOutput, strip_fp32r (decompile line 266)
    free_ap = self.free_ap                                       // read the free-axis template
    access.reshapeAccessPattern(free_ap, reshape)               // decompile 694 — reshape into
                                                                //   canonical multi-dim form

    // dtype normalize: TF32 / "fp32 reduced" → plain float32
    if access.dtype is np.float32r and strip_fp32r:             // decompile 436
        access.dtype = np.float32                                // (the dt/float32r/float32 block)

    t = self.tensor; name = tensorname(access)                  // decompile 764 — resolve tensor+name

    // PARTITION-AP SELECTION (RichCompare)
    if self.local_access_partition_size(access, ...) != access.input_hull_size:  // 810 vs 827
        partition_ap = tonga_partition_ap(...)                  // decompile 897 — re-derived
                                                                //   TONGA partition stride
    else:
        partition_ap = access.partition_ap                      // decompile 919 — op's own AP

    NeuronAP = createAP(addrs=access.addrs,                     // decompile 1660 (createAP),
                        access_shape=access.access_shape,       //   1682 (addrs), 1721 (access_shape)
                        partition_ap=partition_ap)              //   — build the abstract NeuronAP
                                                                //   via a kwargs dict (PyDict_New
                                                                //   + PyDict_SetItem per kwarg)

    self.addArgumentOrOutput(birinst, NeuronAP, isOutput)       // decompile 1775 — attach as
                                                                //   INPUT (isOutput=False) or
                                                                //   OUTPUT (isOutput=True)
```

Every step name and the call ordering are CONFIRMED: `reshapeAccessPattern`, `float32r`,
`float32`, `strip_fp32r`, `tensorname`, `local_access_partition_size`, `input_hull_size`,
`tonga_partition_ap`, `partition_ap`, `NeuronAP`, `createAP`, `addrs`, `access_shape`,
`addArgumentOrOutput`, `isOutput` are all the resolved `__pyx_n_s_` loads in the body.

**The partition-AP re-derivation.** When the *local access partition size* differs from the
op's *input hull size* — i.e. the partition dim spans more than the tile's hull — a TONGA
partition AP is synthesized in place of the literal `access.partition_ap`, re-deriving the
partition stride to fit the hardware partition band. Otherwise the op's own partition AP is
used unchanged. (The compare at decompile lines 810/827 is a `RichCompare`; whether it is `>`
or `!=` was not byte-decoded — the *re-derivation behavior* is CONFIRMED, the exact comparison
operator is STRONG by context.)

**The createAP kwargs.** `createAP` is invoked with a freshly built keyword dict: the
decompile shows `PyDict_New()` (line 1675) followed by `PyDict_SetItem` for `addrs`
(1682), then `access_shape` (1721), then `partition_ap`. The resulting `NeuronAP` carries
`{addrs (per-dim address/stride list), access_shape (per-dim element counts), partition_ap}`.
This is the abstract AP that the downstream J-encoder converts to a `TENSOR3D` / `MEM_PATTERN3D`
wire descriptor: `addrs` ↔ stride words, `access_shape` ↔ num words. See
[Tensor4D / MemPattern4D](../isa/tensor4d-mempattern4d.md).

> **QUIRK (front-loading) —** there is **no twin of `addComplicatedDMAAP` in the klr/C++
> path.** The reshape + tonga-partition + `createAP` multi-dim build happens *at codegen* here,
> whereas the equivalent strided-DMA legalization in the klr path is a separate *downstream*
> pass. The beta3 / Penguin path front-loads the multi-dim descriptor decision into codegen.
> A reimplementer porting from the klr model must move that decision earlier.

### 4.3 The simple AP path (addAP) and companions

`addAP` (in the `_77` family) is the simple ≤`neuron_max_strides` standard strided AP: the
non-reshaped, `partition_ap`-direct attach (versus §4.2's reshape + tonga-partition +
`createAP`). Its companions in this module are `add_sb_to_sb_cc_ap` (the SB↔SB collective AP,
§6), `addBatchTransposeAP` (§3), and the compute-side `addReduceAP` / `addSparseMatmulAP` /
`addDoubleRowAP` covered on [BirCodeGenLoop Compute Codegens](bircodegen-compute.md).

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `codegenNdDMAAP` | `0x1433b0` | simple-vs-complicated AP gate | CERTAIN |
| `addComplicatedDMAAP` | `0x169db0` | multi-dim AP build | CERTAIN |
| `reshapeAccessPattern` | `0x1cd690` | canonical multi-dim reshape | CERTAIN |
| `extract_offloaded_memcpy_shape_block` | (module fn) | contiguous-block stride collapse | CERTAIN |
| `tonga_partition_ap` | (module fn) | partition stride re-derivation | CERTAIN |

---

## 5. The Indirect Family — Gather / Scatter / RMW

### Purpose

`codegenNeuronIndirectLoad` (gather) and `codegenNeuronIndirectSave` (scatter) lower
index-driven DMAs. The Penguin op carries `generic_addrs` (per-index base-address list),
`generic_dims` (indirect-dim coefficients), and `is_offset_dma` (register/offset-DMA flag) —
the beta3 twins of the klr index-AP. `codegenNeuronIndirectSave` handles **both** plain
scatter-overwrite and scatter-accumulate (RMW); `codegenNeuronIndirectRMW` is a trampoline.

### 5.1 codegenNeuronIndirectRMW — the trampoline

```c
function codegenNeuronIndirectRMW(self, inst, bb):   // 0x63430, body py1985
    return self.codegenNeuronIndirectSave(inst, bb)
```

CONFIRMED: the 553-line decompiled body contains exactly one `getattr` of
`codegenNeuronIndirectSave` (line 166) and one `PyObject_Call` (line 211). The
RMW-vs-overwrite split lives **inside** `codegenNeuronIndirectSave`, gated by
`isinstance(inst, NeuronIndirectRMW)`.

### 5.2 codegenNeuronIndirectSave — Save and RMW

```c
function codegenNeuronIndirectSave(self, inst, bb):   // 0x158510, body py1859
    id = ...; build_debuginfo(...)                    // name + debug-loc stamp
    has_cc_expr_in_access(inst)                        // collective-channel gate (§6)

    if isinstance(inst, NeuronIndirectRMW):            // the RMW (accumulate) branch
        self.target.enable_dge_on_dst_reduce           // target flag: RMW-via-DGE allowed?
        inst.unique_indices                            // unique-index / dup-index handling
        bb.opcode = Opcode.DMACopy; bb.setmode(...)    // DGE-RMW path (DMACopy form)
        bb.setCceOp(ALUOpcode.<np.add>.opcode)         // the RMW ACCUMULATE op = add
        bb.setOobIsErr(OOBMode.SKIP)
    else:                                              // plain scatter-overwrite
        bb.opcode = Opcode.GenericIndirectSave         // the bulk index-vector scatter

    if inst.is_offset_dma:                             // offset-DMA / dynamic-offset fork
        if self.target.enable_dge_on_vector_indirect_dma:
            bb.opcode = Opcode.DMACopy; bb.setmode(...); bb.setOobIsErr(OOBMode.SKIP)
            // GUARD:
            if isinstance(inst, NeuronIndirectRMW):
                raise AssertionError("IndirectRMW does not support dynamic DMA mode")
        else:
            bb.opcode = Opcode.DMACopy; bb.setmode(...)   // standard DGE form

    bb.setReplicaGroups(...)
    bb.set_dge_type(convert_to_bir_type(inst.dge_mode))
    if rmw_path: bb.setop(...)                          // RMW reduce operand binding
    bb.set_indirect_dims(inst.generic_dims)             // the indirect-dim coefficient AP

    // AP construction, per src/dst — the §4 complicated/simple split:
    for side in (inst.src, inst.dst):
        iterate side.free_ap.step_n_steps
        shape = extract_offloaded_memcpy_shape_block(...)
        if complicated:
            self.addComplicatedDMAAP(bb, side, generic_addrs=...)
            // assert "Cannot legalize strided store!" if illegal
        else:
            self.addAP(bb, side, generic_addrs=...)
```

All branch tokens are CONFIRMED in the decompiled body: `NeuronIndirectRMW`,
`enable_dge_on_dst_reduce`, `unique_indices`, `DMACopy`, `setCceOp`, `ALUOpcode`, `add`,
`OOBMode`, `SKIP`, `setOobIsErr`, `GenericIndirectSave`, `is_offset_dma`,
`enable_dge_on_vector_indirect_dm[a]`, `setReplicaGroups`, `set_dge_type`,
`convert_to_bir_type`, `dge_mode`, `setop`, `set_indirect_dims`, `generic_dims`,
`generic_addrs`, `extract_offloaded_memcpy_shape_block`, `addComplicatedDMAAP`, `addAP`. The
guard literal **`"IndirectRMW does not support dynamic DMA mode"`** is CONFIRMED verbatim in
`_strings.json`.

> **GOTCHA —** scatter-overwrite emits `Opcode.GenericIndirectSave` with **no** `setCceOp`;
> scatter-accumulate (RMW) emits `Opcode.DMACopy` **plus** `setCceOp(ALUOpcode.add)` and
> requires `self.target.enable_dge_on_dst_reduce`. A reimplementer that always emits
> `GenericIndirectSave` will silently produce an *overwrite* where an *accumulate* was
> intended. This is the exact beta3 twin of the klr fork `compute_op none→IndirectSave,
> add→accumulate`.

### 5.3 codegenNeuronIndirectLoad — the gather

```c
function codegenNeuronIndirectLoad(self, inst, bb):   // 0x1b4e10, body py1816
    addInstruction(...); id = ...; build_debuginfo(...)
    has_cc_expr_in_access(inst); has_dynamic_iv         // guards

    if inst.is_offset_dma:
        if self.target.enable_dge_on_vector_indirect_dma:
            bb.opcode = Opcode.DMACopy; bb.setmode(...); bb.setOobIsErr(OOBMode.SKIP)
            // the register/dynamic-offset gather form
        else:
            bb.opcode = Opcode.GenericIndirectLoad      // bulk index-vector gather
    else:
        bb.opcode = Opcode.GenericIndirectLoad

    bb.setReplicaGroups(...)
    bb.set_indirect_dims(inst.generic_dims)
    bb.set_dge_type(convert_to_bir_type(inst.dge_mode))

    for side in (inst.src, inst.dst):
        if complicated:
            self.addComplicatedDMAAP(bb, side, generic_addrs=...)
            // assert "Cannot legalize strided load!" if illegal
        else:
            self.addAP(bb, side, generic_addrs=...)     // src; dst gets a plain NeuronAP
```

Names `is_offset_dma`, `enable_dge_on_vector_indirect_dm[a]`, `DMACopy`,
`GenericIndirectLoad`, `has_dynamic_iv`, `setReplicaGroups`, `set_indirect_dims`,
`generic_dims`, `generic_addrs`, `addComplicatedDMAAP`, `addAP`, `NeuronAP` are CONFIRMED.
`generic_addrs` is the per-index base-address AP attached to the **source**.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `codegenNeuronIndirectSave` | `0x158510` | scatter — Save + RMW | CERTAIN |
| `codegenNeuronIndirectRMW` | `0x063430` | trampoline → Save | CERTAIN |
| `codegenNeuronIndirectLoad` | `0x1b4e10` | gather | CERTAIN |

---

## 6. The Minor Codegens

### codegenIndirectCopy — on-engine local gather

```c
function codegenIndirectCopy(self, inst, bb):   // 0x1c4d40, body py1926
    super().codegenIndirectCopy(inst, bb)
    bb.set_num_valid_indices(compute_num_valid_indices(inst.accumulated_tripcount,
                                                       inst.free_indices_2))  // py1926
    bb.set_num_elem_per_idx(inst.num_elem_per_idx)   // contiguous run length per index
    self.addAP(bb, inst.dst)                          // dst gather AP (NeuronAP)
```

CONFIRMED names `set_num_valid_indices`, `compute_num_valid_indices`, `accumulated_tripcount`,
`free_indices_2`, `set_num_elem_per_idx`, `addAP`, `dst`. The valid-index count is computed
from the accumulated trip-count over the free indices (the per-core index budget of the local
gather).

### codegenPoolGather, codegenNeuronReadTensorPtr — thin overrides

```c
function codegenPoolGather(self, inst, bb):       // 0x55ad0,  body py2553
    super().codegenPoolGather(inst, bb)
    self.addAP(bb, inst.dst)

function codegenNeuronReadTensorPtr(self, inst, bb):   // 0x1bd150, body py1810
    super().codegenNeuronReadTensorPtr(inst, bb)
    self.addAP(bb, inst.dst)                       // NeuronAP — the dynamic-offset register read
```

Both are minimal overrides: the gather index/data and the register/Opcode binding are set in
the Gen base; the impl override only attaches the dst AP. `codegenNeuronReadTensorPtr` reads a
tensor pointer's dst access pattern — the dynamic-offset register read (the source of
`is_offset_dma` offsets). CONFIRMED names; the "tensor-ptr read" semantics are STRONG by the
upstream Penguin `load_tensor_to_register` correspondence.

### codegenNeuronPrintInst — the device print

```c
function codegenNeuronPrintInst(self, inst, bb):   // 0x1aaae0, body py1540
    super().codegenNeuronPrintInst(inst, bb)
    bb.setPrefix(inst.prefix)                       // the print prefix string
    bb.setOutputBuffer(convert_to_bir_type(inst.output_buffer))   // a DebugDeviceBuffer
```

CONFIRMED names `setPrefix`, `prefix`, `setOutputBuffer`, `convert_to_bir_type`,
`output_buffer`, `DebugDeviceBuffer`. The on-device print/dump = `{prefix, DebugDeviceBuffer
output_buffer}`; a related toggle `set_enable_device_dump` is present in the module strings.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `codegenIndirectCopy` | `0x1c4d40` | on-engine local gather | CERTAIN |
| `codegenPoolGather` | `0x055ad0` | Pool-engine gather | CERTAIN |
| `codegenNeuronReadTensorPtr` | `0x1bd150` | dynamic-offset tensor-ptr read | CERTAIN |
| `codegenNeuronPrintInst` | `0x1aaae0` | on-device print | CERTAIN |

---

## 7. Cross-Cutting Machinery — Collective AP, DGE, OOB, QoS

### The collective access pattern

`_has_cc_expr_in_access` (`0x07b930`, a **module-level** function — body pyline 69, no class
prefix in its traceback) detects whether a DMA's access pattern contains a *collective-channel
expression* (a cross-core / replica index in the AP). When present, the SB↔SB collective AP
builder `add_sb_to_sb_cc_ap` (`0x13b540`) — with its inner `add_delinearized_access`
(`0x89210`) to flatten the multi-dim collective AP — is used, together with `setReplicaGroups`
on the inst. This is why every DMA codegen here calls `setReplicaGroups` and
`has_cc_expr_in_access`: the same `DMACopy` Inst class carries the collective (SB-to-SB CC)
variant. CONFIRMED names; the cc-AP build path is STRONG.

### The DGE / OOB / QoS setter surface

```text
set_dge_type(convert_to_bir_type(dge_mode))   DGEType from the Penguin DGEMode enum
setOobIsErr(OOBMode.SKIP)                      OOB handling — SKIP on gather/indirect paths
<inst>.dma_qos → bb.dma_qos                     QoS bound at THIS layer (vs deferred in klr)
self.target flags:
    enable_dge_on_dst_reduce                    RMW-via-DGE allowed
    enable_dge_on_vector_indirect_dma           offset-DMA gather/scatter allowed
    neuron_max_strides                          the simple-AP stride budget (§4.1)
```

These `self.target.<flag>` knobs are per-arch capability gates: a DMA's emit path forks on the
target's capabilities, so the same Penguin op lowers to different BIR opcodes on different
Trainium generations.

---

## 8. Convergence and Divergence vs the klr Path

`BirCodeGenLoop` (beta3, this page) and `KlirToBirCodegen` (beta2, C++/libwalrus) are two
**parallel** emitters that converge on the same BIR L1 node family. The deltas:

| Facet | beta3 `BirCodeGenLoop` (this) | beta2 `KlirToBirCodegen` (klr/C++) |
|---|---|---|
| Language | Python/Cython (birpy binding) | C++ (libwalrus) |
| Input IR | Penguin `DMACopyOp` / `NeuronIndirect*` | klr `NcDmaCopy` / `TensorLoad`/`Store` |
| Inst allocation | Gen base-class `super()` | leaf `insertElement` / `addUnaryInstruction` |
| Plain DMA | `Opcode.DMACopy` + `setmode` | `InstDMACopy(32)` + `addUnaryInstruction` |
| DGEType | `set_dge_type(convert_to_bir_type)` | `translateDGEMode {3,1,2,0}` @ `+0xF8` |
| OOB | `setOobIsErr(OOBMode.SKIP)` | `+0x130 oob_is_err` |
| RMW / accumulate | `setCceOp(ALUOpcode.add)` + `enable_dge_on_dst_reduce` | `compute_op==2` → op `add(4)` |
| Scatter / gather | `GenericIndirect{Save,Load}` **or** `DMACopy`(offset-dma) | `GenericIndirectSave(44)` / `InstDMACopy(32)` |
| Indirect-dim coef | `set_indirect_dims(generic_dims)` | `indirect_dims` vec @ `+0x128` |
| Index address list | `addAP`/`addComplicatedDMAAP(generic_addrs)` | `extractDynamicApToStandAlone` + `addArgument` |
| Transpose | `setTransposeOp` + `addBatchTransposeAP`, Pool | `CopyMode=Transpose`, `TransposeOps=XZYW(11)`, Pool |
| QoS | **SET** (`dma_qos`) | **DEFERRED** (left default `+0x1D8`) |
| Complicated/multidim AP | **`addComplicatedDMAAP`** (reshape + tonga + `createAP`) | no twin — strided-DMA legalization is a downstream pass |
| "strided too big" | `assert "Cannot legalize strided load/store!"` | downstream strided-DMA split |

**Convergence.** Both terminate at the same BIR L1 node family — `DMACopy`,
`GenericIndirectLoad`+`Save`, `IndirectCopy`, `PoolGather` — and both leave the **wire
descriptor** (`TENSOR`/`MEM_PATTERN`/`INDIRECT16B-20B`) to the J-encoder. They are parallel,
not a pipeline.

**The novel beta3 behavior** is `addComplicatedDMAAP`: the reshape + tonga-partition +
`createAP` multi-dim AP build happens *at codegen* here, whereas the klr path defers the
equivalent strided-DMA legalization to a separate downstream pass. The beta3 / Penguin path
front-loads the multi-dim descriptor decision.

---

## 9. Confidence and Re-Verification Ceiling

CONFIRMED (decompiled-body name trace + `_Pyx_AddTraceback` pylines + `function_addresses`
symtab + `_strings.json` of this binary): the eleven roster methods at the cited
addresses/pylines; the two-class `super()`-override emit model (the `_pyx_builtin_super` →
`PyObject_Call` → `getattr(codegenDMACopyOp)` sequence at DMACopyOp lines 598/604/613);
`codegenNeuronIndirectRMW` as the trampoline (single getattr + call); the
`codegenNeuronIndirectSave` Save-vs-RMW branch tokens; the `is_offset_dma` →
`enable_dge_on_vector_indirect_dma` → `DMACopy`(offset-dma) vs `GenericIndirect*` fork; the
full setter surface; `codegenNdDMAAP`'s complicated-vs-simple decision via
`extract_offloaded_memcpy_shape_block` + `neuron_max_strides`; the complete
`addComplicatedDMAAP` algorithm (every step name + decompile line); the validation literals
`"Cannot legalize strided load!"` / `"…store!"` / `"IndirectRMW does not support dynamic DMA
mode"` quoted verbatim; the `_has_cc_expr_in_access` + `add_sb_to_sb_cc_ap` +
`add_delinearized_access` roster; the absence of the `TENSOR`/`MEM_PATTERN`/`ADDR4` descriptor
names (grep count 0 → wire descriptor is downstream).

STRONG: the §8 convergence/divergence table and the QoS-set-here-vs-deferred-in-klr delta; the
tonga-partition re-derivation rationale; the `ReadTensorPtr` = dynamic-offset tensor-ptr-read
semantics.

INFERRED: the exact `setmode` constant values (the mode tuples were not byte-decoded — the
`Copy`/`Transpose` enum values are inferred by parity with the klr `CopyMode +0x128`); whether
the §4.2 partition compare is `>` or `!=` (the re-derivation behavior is CONFIRMED, the
operator is STRONG by context).

**Re-verification ceiling.** Every function on this page was re-disassembled from its complete
decompiled `.c` body in the cp310 IDA sidecar; none was truncated. The mode-tuple constant
values and the partition RichCompare operator are the only two facts that resisted byte-level
decode — both are flagged inline. The upstream Penguin op field layouts and the downstream
J-encoder wire mapping live in other modules (see Cross-References) and were not
re-disassembled here.

---

## Cross-References

- [The BirCodeGenLoop Driver](bircodegenloop.md) — the dispatch loop that invokes these codegens
- [BirCodeGenLoop Compute Codegens](bircodegen-compute.md) — the compute half (matmul / activation / reduce) of this layer
- [NeuronCodegen Memory Builders](neuroncodegen-memory.md) — the upstream `penguin.ir` `DMACopyOp` / `NeuronIndirect*` op construction (`generic_addrs` / `generic_dims` / `is_offset_dma`)
- [DGE-Level Dynamic DMA](../penguin/dge-level-dynamic-dma.md) — the offset-DMA / DGE dynamic-offset model this layer forks on
- [DMA Encoding](../isa/dma-encoding.md) — the transpose / CopyMode / OOB encoding the J-encoder produces downstream
- [Tensor Descriptors](../isa/tensor-descriptors.md), [MemPattern 2D/3D](../isa/mempattern-2d-3d.md), [Tensor4D / MemPattern4D](../isa/tensor4d-mempattern4d.md), [Indirect Descriptors](../isa/indirect-descriptors.md) — the wire descriptors the abstract `NeuronAP` is encoded into
