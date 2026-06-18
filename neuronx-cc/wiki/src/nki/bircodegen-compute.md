# BirCodeGenLoop Compute Codegens: Matmul / Activation / Tensor / Reduce / Copy

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The subject is `neuronxcc/starfish/penguin/targets/codegen/BirCodeGenLoop.cpython-310-x86_64-linux-gnu.so` (11,139,256 B; BuildID `cb19fae04f37f9a7ef7acc610b6d32d36ac816f1`; **unstripped, with DWARF** — a Cython goldmine). cp311/cp312 siblings live under the same path; addresses there differ, treat every address as version-pinned. The auto-generated `generated/BirCodeGenLoopGen.cpython-3xx.so` is a **different** module — do not conflate them.*

## Abstract

`BirCodeGenLoop` is **layer 2 of Strand-P**: it walks the Penguin *tensoriser IR* — one op at a time, at the TongaISAInst level — and **builds Backend IR (BIR) directly**. Its own module docstring (verbatim `.rodata`) states the job: *"BirCodeGen - Generate Backend IR from tensoriser IR at the TongaISAInst level."* The class is a single public `BirCodeGenLoop` carrying **173 `PyMethodDef` methods**, of which roughly 120 are `codegen<OpName>` per-instruction lowerings. This page catalogs the **compute** subset — the matmul family (`codegenMatMulOp` / `MXOp` / `SparseOp`), the activation pair (`codegenActivationOp` / `codegenActivate2`), the tensor-scalar pointer emitters, and the tensor/reduce/copy/DVE/register-load/sequence-bounds/inline-asm lowerings — naming for each the real symbol and the `birpy.Instruction` it constructs.

The central structural fact, and a **correction to the layer-2 picture drawn by upstream reports**, is that this module emits BIR *directly* via the Python `birpy.Instruction` binding — there is **no** `Penguin IR → klr → BIR` chain inside it. The BIR construction surface it imports is `neuronxcc.starfish.birpy.{Instruction, Opcodes, MemoryLocation, Function, Module, BirAffineExpr}` (all confirmed in `.rodata`). The only `klr`/`KLIR` tokens in the binary belong to a *separate, unrelated* method, `codegenExternalNativeNkiKlirKernel` (the external-NKI beta2 KLIR-tracing kernel path), whose config docstring literally reads *"beta2: Use beta2 KLIR tracing (default)"*. So layer 2 here is the **beta3 / Penguin** front-end onto BIR, running **in parallel** with the C++ **beta2 / klr** front-end (`KlirToBirCodegen`, libwalrus). The two are twin emitters that converge on the *same* BIR `Inst` node family — they are **not** a pipeline.

The page is organized: foundation and the two emit idioms (§1), then the matmul family (§2), activation (§3), the tensor-scalar pointer ops (§4), the reduce pair (§5), the Sunda/DVE max/argmax/match-replace family (§6), the offloaded-FMA / register-load / sequence-bounds / inline-asm / misc family (§7), and finally the convergence/divergence table against the klr twins (§8).

| | |
|---|---|
| **Class** | `BirCodeGenLoop` (single public class; 173 `PyMethodDef`) |
| **Module docstring** | *"BirCodeGen - Generate Backend IR from tensoriser IR at the TongaISAInst level."* |
| **IR level** | Penguin tensoriser-IR `Op` **IN** → `birpy.Instruction` **OUT** |
| **BIR surface** | `neuronxcc.starfish.birpy.{Instruction, Opcodes, MemoryLocation, Function, Module, BirAffineExpr}` |
| **Dispatcher** | `dispatch_codegen` (`_263`, `0x745d0`) → `getattr(self, 'codegen'+type(op).__name__)` |
| **Shared tail (matmul/act)** | `addInstToBir` (`_265`, `0x63f30`) — set name/engine, `addInstruction` |
| **`.text` / `.rodata`** | VA `0x09ac0` == fileoff (len `0x1d99cd`) / VA `0x1e4000` == fileoff (len `0x14b20`) |
| **Twin (klr/C++)** | `KlirToBirCodegen` (libwalrus) — beta2/klr path, **parallel**, same BIR node |

---

## 1. Foundation: the dual-path model and the two emit idioms

### 1.1 Where this sits — beta3 ∥ beta2, both → BIR

There are two front-ends onto Backend IR, and they are *siblings*, not stages:

```text
  L1  KernelBuilder.NeuronCodegen   : nl / nisa  → Penguin tensoriser IR (MatMulOp, …)
       │
       ├── L2  BirCodeGenLoop.codegen*  : Penguin IR → BIR     [THIS MODULE, beta3]
       │         via birpy.Instruction (the PYTHON BIR binding)
       │
       └── Lx  KlirToBirCodegen (C++)   : klr AST  → BIR        [libwalrus, beta2]
                 via bir::InstBuilder (the C++ BIR binding)
                                  ╲        ╱
                                   ╲      ╱
                            same  bir  Inst  node family
                       (Matmult / MatmultMx / MatmultSparse /
                        Activation / TensorScalarPtr / Memset / …)
```

> **CORRECTION — layer 2 does *not* emit klr.** An earlier reading drew layer 2 as *"Penguin IR → klr (KLIR) AST → klr::NcMatMul"*. The binary contradicts the klr-intermediate claim. `BirCodeGenLoop` imports `birpy.Instruction` and appends with `addInstruction` — there is **no** `klr::NcMatMul` / klr AST construction in any matmul or tensor codegen. The 3-stage chain *"Penguin → klr → BIR"* is wrong; the truth is *"Penguin → BIR (L2)"* running **parallel** to *"klr → BIR (Lx)"*. CONFIRMED by: the `birpy.*` import strings; the absence of any klr build call in the codegen bodies; and the fact that every `klr`/`KLIR` token in `.rodata` is owned by `codegenExternalNativeNkiKlirKernel` (`_233`) plus its `klir_binary` / `klir_input_names` config — an unrelated external-NKI kernel path, **not** the per-inst lowerings.

The `_INTERNAL_KERNEL_REGISTRY` module global (and `_build_internal_kernel_registry` with its `._get_attrs` / `._get_conv_attrs` / `._get_resize_args` closures, plus `get_internal_kernel_registry`) is **confirmed present in this same binary**. The matmul/activation/tensor codegens do **not** consult it — it is the compiled-vs-readable internal-kernel selector used by a different cluster of methods (`_trace_internal_kernel_to_new_nki_frontend`, `_resolve_kernel_config`, `codegenInternalNativeNkiKernel`).

### 1.2 The call signature and dispatch

Every method here is `def codegenXxx(self, inst)` — the Cython `__pyx_pyargnames` tuple is literally `[self, inst]` (e.g. `codegenIndexValueInst` @`0x15e110` sets `__pyx_pyargnames=[__pyx_n_s_self, __pyx_n_s_inst]`). So `inst` is the **incoming Penguin tensoriser-IR op**; the body reads attributes off `inst` and **builds a new `birpy.Instruction`**.

Dispatch is name-based. `dispatch_codegen` (`_263`, `0x745d0`, ~3088 B) routes a Penguin `Op` to its handler by class name — `getattr(self, 'codegen' + type(op).__name__)` — backed by the contiguous interned method-name pool (`dispatch_codegen…codegenReplicaIdTensor…codegenTransposeOp…`). This is the same name-table mechanism used by the driver page [§6.5.10 BirCodeGenLoop](./bircodegenloop.md).

### 1.3 Two emit idioms (both confirmed in disasm)

The codegen bodies split into two append styles. The split is real and observable, and a reimplementer must reproduce both:

**FLAVOR A — explicit-build tail** (modern ops: the Sunda/DVE family, offloaded-FMA, sequence-bounds, register-load, inline-ASM, max-index). The body **inlines** the debuginfo→append sequence:

```c
// FLAVOR A (e.g. codegenSundaMaxIndex8, codegenOffloadedFMA, …)
i = Instruction(Opcode.<Name>);     // Opcode enum accessed SINGULAR: Opcode.<Name>
i.build_debuginfo(inst.id);          // attach source location from the Penguin op id
/* … set attrs … addAP(dst, isOutput=True) … */
bb.addInstruction(i);                // append onto the current BasicBlock
```

CONFIRMED ordered tokens common to flavor A: `{id, build_debuginfo, addInstruction, Opcode}`.

**FLAVOR B — attribute-marshal tail** (memset, local-reduce, dropout, index-value, stream-shuffle): build the inst, set its named attrs (`setmode`, `setop`, …), `addAP(dst)`, then append. The dst operand is always marked by `addAP(..., isOutput=...)` over a `NeuronAP`.

**Shared matmul/activation tail — `addInstToBir`** (`_265`, `0x63f30`): the matmul/activation family does **not** inline the append; it calls the shared helper, which sets the inst name (`set_name`), the engine (`set_engine`, via `engineTrans` → `birEngineType` / `EngineType`), then `addInstruction` onto the BIR `Function`. CONFIRMED tokens `set_engine`, `addInstruction`, `engineTrans`, `birEngineType`.

> **NOTE — `Opcode` is accessed singular.** The import is `birpy.Opcodes` (plural, the enum module) but call sites read `Opcode.<Name>` (singular). Both tokens are confirmed in `.rodata`. The opcode *name* of an inst is selected through this enum loaded from module-state — which is why opcodes like the plain `Matmult` and `Memset` do **not** surface as standalone strings (see §2 / §7.1).

---

## 2. The matmul family

`codegenMatMulOp` is the largest body in the module (~27.8 KB), which is itself a signal: most of that mass is operand-binding and access-pattern construction, not the inst object.

| Method | idx | `pw` addr | ~size | Builds `birpy.Instruction` class | engine |
|---|---|---|---|---|---|
| `codegenMatMulOp` | `_35` | `0x145910` | 27.8 KB | `Matmult` (enum-selected) | PE |
| `codegenMatMulMXOp` | `_257` | `0x16ebd0` | 11.9 KB | `MatmultMx` (`.rodata` token) | PE |
| `codegenMatMulSparseOp` | `_37` | `0x102cd0` | — | `MatmultSparse` (`.rodata` token) | PE |

> **QUIRK — `Matmult` has no standalone string; `MatmultMx` / `MatmultSparse` do.** Searching `.rodata` for whole-word `Matmult` returns **nothing**, while `MatmultMx` and `MatmultSparse` are present as inline class-name literals. The plain matmul instruction class is selected through the `Opcode` enum loaded from module-state (so it never materializes as a bare string), whereas the MX/Sparse variants carry an explicit class-name literal. Do not read the missing string as "no plain matmul" — it is enum-selected.

### 2.1 `codegenMatMulOp` (_35) → bir `Matmult`

The upstream Penguin `MatMulOp` carries **stationary** (=weight), **moving** (=ifmap), **dst** (=PSUM), the booleans `isStationaryOneZero` / `isMovingZero` / `isTranspose`, the PE-tile position+size vectors, and `perf_mode` (`"double_row"` → `DoubleRow`).

```c
// codegenMatMulOp  —  Penguin MatMulOp → bir Matmult, engine PE.
// CONFIRMED: vocabulary, validation literals.  INFERRED: exact call order.
Instruction codegenMatMulOp(self, op) {
    // (1) operand memlocs.  createMemLoc(_299) builds a BIR MemoryLocation set
    //     for each tensor operand; transformNeuronPSUMTensor(_305) maps the dst
    //     NeuronPSUMTensor → PSUM memloc; transformNeuronWeightTensor(_309) maps
    //     the stationary NeuronWeightTensor (validates rank-2: literal
    //     "A splittable NeuronWeightTensor should be of rank 2"; is_splitable).
    ml_w   = transformNeuronWeightTensor(op.stationary);     // weight  (LHS)
    ml_mov = createMemLoc(op.moving);                        // ifmap   (RHS)
    ml_dst = transformNeuronPSUMTensor(op.dst);              // psum    (OUT)
    // getMatmultOperandType classifies each as Stationary/Moving/Output.

    inst = Instruction(Opcode.Matmult);                      // enum-selected
    // (2) bind operands + access patterns.  psum dst uses
    //     access_psum_as_operand / set_psum_buf_shape / psum_buf_shape;
    //     is_single_assign_psum_fast + SINGLE_ASSIGN_PSUM classify the fast
    //     single-assignment PSUM case.

    // (3) the 3 boolean attrs  (CONFIRMED setters; note the weight/fmap renaming):
    inst.set_is_transpose(op.isTranspose);
    inst.set_is_weight_onezero(op.isStationaryOneZero);      // Penguin is_lhs_onezero
    inst.set_is_fmap_onezero(op.isMovingZero);               // Penguin is_rhs_onezero
    inst.set_ifmap_quant_offset(op.ifmap_quant_offset);      // moving-operand quant

    // (4) PE-tile geometry (x/y split into named fields):
    inst.set_tile_size(op.pe_tile_size);                     // + pe_tile_size_x/_y
    inst.pe_tile_position(op.pe_tile_position);              // + pe_tile_position_x/_y
    inst.setnum_active_channels(op.num_active_partitions);

    // (5) perf_mode.  DoubleRow is the ONLY named PerfMode token in the binary.
    if (op.perf_mode == "double_row") {
        inst.set_perf_mode(PerfMode.DoubleRow);
        addDoubleRowAP(inst, op);          // _285 / cayman_matmul_double_row_ap _287
        inst.set_replication_num_rows(...); // validates "incorrect double row step"
    }
    inst.set_psum_buf_shape(...);

    // is_accumulate is RECORDED on the inst, but the PSUM-accumulate Calc flags are
    // NOT finalized here — the later scheduler passes do that (parity with the klr twin).
    return addInstToBir(self, inst);        // set_name / set_engine(PE) / addInstruction
}
```

Validation literals (CONFIRMED verbatim in `.rodata`): `"A splittable NeuronWeightTensor should be of rank 2"`, `"Incorrect access pattern!"`, `") or dst start_partition("` (the multi-line *"src start_partition(…) or dst start_partition(…)"* mismatch message, shared with tensorcopy — `"tensorcopy src start_partition("` is its sibling), and `"incorrect double row step"`.

> **NOTE — naming divergence vs the klr twin.** The Penguin op exposes `is_lhs_onezero` / `is_rhs_onezero`; this BIR surface renames them weight/fmap-centric (`set_is_weight_onezero` / `set_is_fmap_onezero`). The klr twin uses the field order `{isStationaryOneZero, isMovingZero, isTranspose}`. Same semantics, different setter surface — lhs/stationary = weight, rhs/moving = ifmap.

### 2.2 `codegenMatMulMXOp` (_257) → bir `MatmultMx`

Microscaling matmul. On top of the §2.1 base operands, the MX variant adds **two E8M0 block-scale tensors** (`lhs_scale` / `rhs_scale`) and their partition access-patterns:

```c
// codegenMatMulMXOp  —  Penguin MatMulMXOp → bir MatmultMx, engine PE.
Instruction codegenMatMulMXOp(self, op) {
    bind dst / stationary / moving memlocs;                  // as §2.1
    ap_ls = getMxScalePartitionAp(self, op.lhs_scale);       // _255: [P/8] partition AP
    ap_rs = getMxScalePartitionAp(self, op.rhs_scale);       //       for the E8M0 scales
    inst = Instruction(Opcode.MatmultMx);                    // CONFIRMED .rodata token
    inst.set_scale(...);                                     // scale / scales / scale_ptr
    //   "Scale should be 1.0 if scale_ptr is specified"  ← when an explicit scale_ptr
    //   tensor is bound, the immediate scale must be 1.0.  (CONFIRMED literal.)
    //   per-operand fields: lhs_scale / rhs_scale / dst_scale / offset_scale.
    inst.set_tile_size(...);  inst.pe_tile_position(...);    // as §2.1
    return addInstToBir(self, inst);                         // PE
}
```

MX is **always** transpose=false, onezero=false, PE-engine, **no** `perf_mode` — MX-ness rides on the operand DTYPE plus the two scale tensors. Second validation literal: `"offset scale is not equivalent to the accumulated tensor size on right of generic dim in {inst}"`. (`lhs_scale` / `rhs_scale` / `getMxScalePartitionAp` and the two literals are all CONFIRMED in `.rodata`.)

### 2.3 `codegenMatMulSparseOp` (_37) → bir `MatmultSparse`

Structured-sparse matmul. Reuses the §2.1 base operand binding (stationary/moving/dst, tile geometry, onezero/transpose) and adds the sparse access-pattern builder `addSparseMatmulAP` plus the `set_compress_ratio` setter that threads the user's compression ratio onto the BIR node:

```c
// codegenMatMulSparseOp  —  Penguin MatMulSparseOp → bir MatmultSparse, engine PE.
Instruction codegenMatMulSparseOp(self, op) {
    bind §2.1 base operands;
    addSparseMatmulAP(self, op);          // _281: the sparse-matmul access pattern
    inst = Instruction(Opcode.MatmultSparse);             // CONFIRMED .rodata token
    inst.set_compress_ratio(op.compress_ratio);           // verbatim copy from the IR node
    inst.set_is_transpose / set_is_fmap_onezero / set_is_weight_onezero;
    inst.set_replication_{resolution,shift_amnt,num_rows}; setTilePosition; set_tile_size;
    return addInstToBir(self, inst);      // PE
}
```

> **CORRECTION —** an earlier revision claimed this body calls `inst.setMask(op.mask)` and cited `"Mask pattern only support 1 step multiplier"` as its validation literal. The decompiled body (`__pyx_pw_…_BirCodeGenLoop_37c` @ `0x102cd0`) contains **zero** `mask`/`setMask`/`shuffle_mask` attribute references; its setter vocabulary is exactly `set_compress_ratio`, `set_is_transpose`, `set_is_fmap_onezero`, `set_is_weight_onezero`, the three `set_replication_*`, `setTilePosition`, `set_tile_size` (`__pyx_n_s_*` interned-name set). The `"Mask pattern only support 1 step multiplier"` string *is* present in `BirCodeGenLoop.so`'s `.rodata`, but it belongs to a different codegen (the mask is a stream-shuffle / lane-mask surface — see `codegenStreamShuffleInst`'s `setMask(shuffle_mask)`, §7.6), **not** the sparse matmul. The structured sparsity input is the `tags` / `compress_ratio` pair carried on `MatMulSparseOp` (no boolean mask). Verified against the body and consistent with [Sparse Matmul Lowering §4](sparse-matmul-lowering.md).

### 2.4 The matmul-family AP machinery

Three AP builders feed the matmul codegens:

- **`addDoubleRowAP`** (`_285`, `0x1415a0`) / **`cayman_matmul_double_row_ap`** (`_287`, `0xecf30`) — build the double-row (`perf_mode=DoubleRow`) access pattern for the Cayman/Trillium PE array. Vocabulary: `double_row`, `double_row_set`, `double_row_gen3` (a 3rd-gen double-row variant gate), `double_row_stride_alignment`, `set_replication_num_rows`. Validation `"incorrect double row step"`.
- **`addSparseMatmulAP`** (`_281`, `0xf2430`) — §2.3 sparse AP.
- **`getMxScalePartitionAp`** (`_255`, `0xcb880`) — §2.2 MX scale partition AP.

---

## 3. The activation pair

`codegenActivationOp` (`_33`, `0x197310`, ~15.5 KB) and `codegenActivate2` (`_31`, `0xa4d00`) **share one BIR `Activation` builder**. The classic form emits `Activation`; the accumulating form emits `ActivationAccumulationOp`; the dual/2-output "activation v2" form flips a flag. The class-name tokens `Activation` and `ActivationAccumulationOp` are both CONFIRMED standalone in `.rodata`. The `Activate2` op is the imported `neuronxcc.starfish.penguin.targets.generated.Activate2`.

```c
// codegenActivationOp / codegenActivate2  —  Penguin → bir Activation.
Instruction codegenActivationOp(self, op) {
    bind in / out memlocs;
    cls  = op.is_accumulate ? "ActivationAccumulationOp" : "Activation";
    inst = Instruction(Opcode.<cls>);

    // (1) ACTIVATION-FUNC marshalling — bind the act-func selector + its tensors:
    inst.set_func(op.func);
    inst.set_func_args(op.func_args);
    inst.set_func_outs(op.func_outs);

    // (2) bias / scale.  bias has dual-direction forms up_bias / down_bias.
    inst.set_scale(op.scale);
    // immediates imm0/imm1 carried CONSTANT or SYMBOLIC/POINTER (imm0_ptr/imm1_ptr)
    // via ImmediateValue / SymbolicImmediateValue / addImmValue / setImmediateVal:
    bind_bias(inst, op.bias, op.up_bias, op.down_bias);

    // (3) FUSED reduce — an activation that also reduces along free:
    if (op.has_fused_reduce) {
        inst.setreduce_op(op.reduce_op);
        inst.setreduce_cmd(op.reduce_cmd);
    }

    // (4) Activate2 — the dual form flips setIsActivate2 + binds the 2nd func/out:
    if (op is Activate2)  inst.setIsActivate2(True);

    inst.is_accumulate = op.is_accumulate;
    return addInstToBir(self, inst);     // Activation engine
}
```

Confirmed setters: `set_func` / `set_func_args` / `set_func_outs`, `set_scale`, `setreduce_op` / `setreduce_cmd`, `setIsActivate2`. `generateCopyAct` (`_163`) is the copy-as-activation helper — a `Copy` lowered as an identity `Activation`. CONFIRMED: `set_func[_args/_outs]`, `setreduce_op/_cmd`, `setIsActivate2`, `set_scale`, `ActivationAccumulationOp` are all present in `.rodata`.

---

## 4. The tensor-scalar pointer emitters

`codegenAffSelTensorScalarOp` (`_57`, `0x9dba0`) and `codegenTensorScalarGEPOp` (`_179`, `0x1c6450`) both lower a Penguin tensor-scalar variant onto **one shared BIR `TensorScalarPtr` emitter**, `getTensorScalarPtr` (`_169`, `0x882a0`). The union-type literals in `.rodata` make the sharing explicit: `"Union[TensorScalarGEPOp, TensorScalarPtrOp]"` and `"Union[TensorScalarPtrOp, TensorScalarCacheCumulative]"`. The companions `codegenTensorScalarPtrOp` (`_171`) and `codegenTensorScalarCacheCumulative` (`_173`) share the same emitter. Downstream class token `TensorScalarPtr` is CONFIRMED standalone in `.rodata`.

```c
// getTensorScalarPtr (_169)  —  the common TS-ptr BIR builder → bir TensorScalarPtr.
Instruction getTensorScalarPtr(self, op) {
    inst = Instruction(Opcode.TensorScalarPtr);
    inst.setop0(op.op0);  inst.setop1(op.op1);   // the TWO ALU operands (no underscore)
    //   op0_opcode_str = the ALU opcode for op0; is_op0_scalar / is_op1_scalar /
    //   is_tensor_scalar classify the operands; _compare_op0 / _compare_op1 carry
    //   the compare variants.
    inst.set_is_scalar_tensor_tensor(...);       // the STT mode flag
    inst.set_reverse_pred(...);
    inst.setfill_value(op.fill_value);           // fill_value / _fill_value
    //   "LOGIC FAULT, TSC mode must have op1 set!"  ← two-operand TSC mode requires op1.
    return inst;                                 // appended by the caller
}
```

**`codegenAffSelTensorScalarOp` (_57)** folds an affine-select into a predicated tensor-scalar. The affine-select condition becomes a set of `BirAffinePredicate` guards on the `TensorScalarPtr` inst (with `fill_value` for the unselected lanes):

```c
// codegenAffSelTensorScalarOp  —  affine-select → predicated tensor-scalar.
Instruction codegenAffSelTensorScalarOp(self, op) {
    inst = getTensorScalarPtr(self, op);                  // shared TS builder
    for (p in enumerate_predicates_in_codegen_order(op))
        inst.predicates.add(BirAffinePredicate(p));       // affine guards per lane
    inst.setfill_value(op.fill_value);                    // unselected-lane fill
    return addInstToBir(self, inst);
}
```

CONFIRMED: `BirAffinePredicate`, `predicates`, `enumerate_predicates_in_codegen_order` all in `.rodata`. Siblings that share this predicate machinery: `codegenRangeSelect` (`_185`), `codegenSelectReduce` (`_187`), `codegenTensorSelect` (`_189`), `codegenTensorCopyPredicated` (`_191`).

**`codegenTensorScalarGEPOp` (_179)** lowers a tensor-scalar whose scalar operand is fetched through an indexed/pointer (getelementptr-style) access. It routes through `getTensorScalarPtr` with the indirect operand bound via the `TensorScalarPtr` `scale_ptr` / `op1` slot. (STRONG.)

---

## 5. The reduce pair — local vs. macro

Two reduce lowerings, both converging on a bir `Reduce` / `TensorReduce` node (both tokens CONFIRMED standalone in `.rodata`). The local-vs-macro split is the defining distinction.

| Method | idx | `pw` addr | ~size | bir target |
|---|---|---|---|---|
| `codegenLocalReduceOp` | `_73` | `0x6ca10` | 5.3 KB | `Reduce` (single-step) |
| `codegenNeuronReduceMacro` | `_193` | `0x13d2b0` | 5.9 KB | `Reduce` × N (multi-step) |

### 5.1 `codegenLocalReduceOp` (_73) — single-step reduce

Ordered tokens read straight from disasm program order: `setop(op,opcode)` → `setReplicaGroups` → `setKind` → `setLocal` → `codegenNdDMAAP` → (`strip_fp32r`) → `ALUOpcode`.

```c
// codegenLocalReduceOp  —  the elementary single-step reduce → bir Reduce.
Instruction codegenLocalReduceOp(self, op) {
    inst = <bir Reduce>;
    inst.setop(op.op, opcode = ALUOpcode.<add/max/min/…>); // reduce ALU op
    inst.setReplicaGroups(op.groups);   // cross-replica/partition grouping
    inst.setKind(op.kind);              // reduce kind
    inst.setLocal(True);                // ← the LOCAL flag: within-engine/-partition
    codegenNdDMAAP(self, op);           // _127: build the N-d DMA access pattern
    return <append>;                    // FLAVOR-B tail
}
```

`setLocal` is the half that names this the *local* reduce (a within-engine / within-partition reduce); `setReplicaGroups` gives the partition-reduce flavor. `codegenNdDMAAP` (`_127`) confirms this reduce drives a DMA-shaped AP. ALU pool tokens seen in the module include `bypass`, `Rsqrt`.

### 5.2 `codegenNeuronReduceMacro` (_193) — multi-step reduce

Ordered tokens: `is_first_reduce` → `setReduceAxes(reduce_axes)` → loop[`enumerate_axes` → it] → `setop(op,opcode)` → `addAP(dst,isOutput)` + `strip_fp32r` → `NeuronAP`.

```c
// codegenNeuronReduceMacro  —  the MULTI-STEP (multi-axis) reduce expansion.
void codegenNeuronReduceMacro(self, op) {
    for (i, ax in enumerate_axes(op.reduce_axes)) {       // iterate the axis LIST
        inst = <bir Reduce>;
        inst.setReduceAxes(op.reduce_axes);
        inst.setop(op.op, ALUOpcode.<…>);
        inst.is_first_reduce = (i == 0);  // FIRST step reads ifmap; rest fold the partial
        inst.addAP(dst, isOutput=True);   // strip_fp32r unwraps the fp32-replicated dtype
        <append>;                         // a Reduce node PER STEP
    }
}
```

> **CORRECTION — "Macro" is *not* a preprocessor macro.** The name `NeuronReduceMacro` denotes a **multi-STEP / multi-axis reduce expansion** — a chain of `Reduce` nodes where only the first (`is_first_reduce`) reads the source tensor and the rest fold the running partial. It has nothing to do with C-preprocessor macros. CONFIRMED tokens `setReduceAxes`, `is_first_reduce`, `enumerate_axes`.

> **NOTE — a real divergence vs. the klr twin.** The klr path emits a **single** `InstTensorReduce` whose `AxisListType` holds *all* axes at once. The Penguin macro path here expands to a **multi-step chain** (`is_first_reduce`). Same `Reduce` node family, different expansion granularity — the Penguin side does axis-by-axis what the klr side packs into one `AxisListType`.

---

## 6. The Sunda / DVE max-argmax-match-replace family

The `Sunda*` prefix is the **Sunda / DVE engine codename**. This is the 8-wide reduction primitive family behind argmax / Top-K / MoE. All four converge on the bir `Max` / `MaxIndex` / `MatchReplace` / `MaxIndexAndMatchReplace` opcodes (the I12 IT88–IT91 node family).

| Method | idx | `pw` addr | ~size | bir Opcode | klr/IT |
|---|---|---|---|---|---|
| `codegenSundaMax8` | `_195` | `0x1bbf70` | 4.5 KB | `Max` (enum) | IT88 |
| `codegenSundaMaxIndex8` | `_197` | `0x1b9ec0` | 8.2 KB | `MaxIndex` | IT89 |
| `codegenSundaMatchReplace8` | `_199` | `0x68270` | 5.6 KB | `MatchReplace` (enum) | IT90 |
| `codegenMaxIndexAndMatchReplace` | `_201` | `0xcf480` | 7.2 KB | `MaxIndexAndMatchReplace` | IT91 |

`MaxIndex` and `MaxIndexAndMatchReplace` are CONFIRMED standalone tokens; bare `Max` and `MatchReplace` are enum-selected (no standalone string).

### 6.1 `codegenMaxIndexAndMatchReplace` (_201) — the fused index+replace

The **fused** form that produces *both* the max-index and the match-replaced values in one inst. FLAVOR A, ordered tokens: `inst` → `id` → `build_debuginfo` → `Instruction(Opcode.MaxIndexAndMatchReplace)` → `imm`/`dtype` → `asscalar_or_str` → `setImmediateVal` → `clip_scalar` → `addAP(dst, isOutput)` + `dst_idx` + `vals`/`data` → `addInstruction`.

```c
// codegenMaxIndexAndMatchReplace  —  bir MaxIndexAndMatchReplace (IT91), dual outputs.
Instruction codegenMaxIndexAndMatchReplace(self, inst_in) {
    i = Instruction(Opcode.MaxIndexAndMatchReplace);
    i.build_debuginfo(inst_in.id);
    val = asscalar_or_str(inst_in.imm);   // accept python scalar OR symbolic-imm string
    i.setImmediateVal(clip_scalar(val));  // clamp to operand dtype range
    i.addAP(dst,     isOutput=True);      // dst     = the replaced-VALUES output
    i.addAP(dst_idx, isOutput=True);      // dst_idx = the max-INDEX output
    /* vals / data = the value stream */
    bb.addInstruction(i);
    return i;
}
```

CONFIRMED: `MaxIndexAndMatchReplace`, `dst_idx`, `asscalar_or_str`, `clip_scalar`, `setImmediateVal` all in `.rodata`. The fused node's two outputs (`dst` replaced-values + `dst_idx` max-index) match the klr path's *"index output present → IT91"* selection.

### 6.2 The unfused arms

- **`codegenSundaMaxIndex8` (_197) → `MaxIndex`** (FindIndex8 / argmax-of-8). Ordered tokens: `Instruction(Opcode.MaxIndex)` → `max_vals` → `dst` → `reinterpret_dst_inplace`/`reinterpret_inplace` (uint16↔uint32) + `unsigned_int_dtype` + `is_int_type` + `dt` → `addAP(dst)` → `NeuronAP` → `addInstruction`. The argmax8 returns `{index(uint), max_vals}`; the **index output is reinterpreted to an unsigned-integer dtype** (uint16/uint32 by width) using the same dtype-view machinery as Memset (§7.1).
- **`codegenSundaMatchReplace8` (_199) → `MatchReplace` (IT90)** — the non-fused arm (index output **absent**). Same `asscalar_or_str` / `setImmediateVal` / `clip_scalar` immediate surface as §6.1, minus the index output.
- **`codegenSundaMax8` (_195) → `Max` (IT88)** — the bare max-of-8 reduce; minimal body (`inst` → `addAP(dst)` → `dst`), no index.

> **NOTE — fused/unfused expressed differently than the klr twin.** The Penguin path names the fused node explicitly (a *separate method* `codegenMaxIndexAndMatchReplace`), whereas the klr path reaches IT90/IT91 from a *single* `klr::MatchReplace8` via a runtime "engaged-byte" / index-output flag. Both converge on the same four opcodes; only the way the fused/unfused split is *expressed* diverges.

---

## 7. Offloaded-FMA, register-load, sequence-bounds, inline-ASM, misc

### 7.1 `codegenMemsetOp` (_39) → bir `Memset` + dtype reinterpret

`codegenMemsetOp` (`0x1dd000`, ~12.9 KB) lowers a constant-fill / RNG-fill of a tile. It carries the `MemsetMode` enum (CONFIRMED token) with members `Const` and `Random` (both CONFIRMED standalone). The bulk of the 12.9 KB body is **dtype-aware constant marshalling**, not the inst itself.

```c
// codegenMemsetOp  —  Penguin MemsetOp → bir Memset.  FLAVOR B.
void codegenMemsetOp(self, op) {
    /* dtype / dt / dtype_size_in_bytes / itemsize ; is_x4_dtype gate (FP8-x4 packed) */
    if (op.is_random) {
        inst.setmode(MemsetMode.Random);          // RNG fill; no constant, RNG is engine-side
    } else {
        // materialise the immediate to the dst dtype: pick the matching-width unsigned
        // view (uint8/16/32 by dtype_size_in_bytes), reinterpret the fill bit-pattern.
        //   reinterpret / reinterpret_dst_inplace + view + is_int_type + static_cast
        //   + numpy np.full(shape, fill_value)
        v = reinterpret_dst_inplace(op.value, dst_dtype);
        inst.setmode(MemsetMode.Const);
        inst.setconstant(v);
    }
    inst.addAP(dst, isOutput=True);  // NeuronAP
}
```

CONFIRMED: `MemsetMode`, `Const`, `Random`, `setmode`, `setconstant`, `is_first_reduce`-style ordering tokens. Note bare `Memset` is **not** a standalone string (enum-selected). The `Const` arm fills the BIR inst's fill field; the `Random` arm leaves it empty (RNG state is engine-side).

### 7.2 `codegenOffloadedFMA` (_117) + `codegenTiledOffloadedFMA` (_115) → bir `DMACopy`

> **CORRECTION — the "offloaded FMA" is a DMA-engine copy, *not* a PE/Act compute FMA.** `codegenOffloadedFMA` (`0x19d570`, ~8.2 KB) lowers to a bir `DMACopy` inst on the **CCE** (collective/copy) DMA engine. CONFIRMED Opcode token `DMACopy` + `setCceOp`. The FMA is a copy with an inline affine (`scale·x + const`) applied **by the DMA engine**, offloading the multiply-add off the PE/Act engines. Any prior doc treating offloaded-FMA as a compute inst is wrong.

```c
// codegenOffloadedFMA  —  Penguin → bir DMACopy on the CCE/DMA engine.  FLAVOR A.
Instruction codegenOffloadedFMA(self, fma) {
    i = Instruction(Opcode.DMACopy);
    i.build_debuginfo(fma.id);
    i.setmode(...);                      // the DMACopy mode selecting the FMA/affine variant
    i.setconstants(fma.scales);          // FMA multiply coefficients ride as DMA constants
    i.setCceOp(...);                     // the CCE (DMA) sub-operation
    /* operands bound via addSeqAccess — the streamed/strided AP for the DMA engine,
       distinct from the static NeuronAP used elsewhere */
    i.addSeqAccess(fma.operands);
    i.addAP(dst, isOutput=True);
    bb.addInstruction(i);
    return i;
}
```

CONFIRMED: `DMACopy`, `setCceOp`, `setconstants`, `scales`, `addSeqAccess`. **`codegenTiledOffloadedFMA` (_115)** is the tiled wrapper: same token set but uses `addAP` (static AP per tile) instead of `addSeqAccess`, looping over tiles.

### 7.3 `codegenLoadTensorToRegister` (_45) → bir `TensorLoad` (register-addressed)

`codegenLoadTensorToRegister` (`0x765f0`, ~12.2 KB) → bir Opcode `TensorLoad` (CONFIRMED standalone). A "load tensor value(s) into named scalar register(s)" — the result is **named scalar registers, not an AP dst** — running on `EngineType.ALL`.

```c
// codegenLoadTensorToRegister  —  bir TensorLoad: tensor element(s) → named register(s).
Instruction codegenLoadTensorToRegister(self, op) {
    i = Instruction(Opcode.TensorLoad);
    i.build_debuginfo(op.id);
    /* dtype / dtype2str / dt */
    for (r in op.dst_registers) {
        ra = RegisterAccess(...);        // the bir register operand
        i.addRegister(ra);               // mint/declare the register
        ra.result_index = ...;           // pick the slot
    }
    i.addArgumentOrOutput(src, isInputOrOutput);  // src registered as a function arg/output
    i.addAP(src);                                 // (it crosses the kernel boundary)
    i.setEngine(EngineType.ALL);                  // broadcast to all engines
    /* reaches into curModule.functions → Function → BasicBlock → Axis to declare the
       register at function scope */
    bb.addInstruction(i);
    return i;
}
```

CONFIRMED: `TensorLoad`, `dst_registers`, `RegisterAccess`, `addRegister`, `result_index`, `addArgumentOrOutput`, `isInputOrOutput`, `EngineType`, `ALL`, and the `Function`/`BasicBlock`/`curModule` plumbing.

### 7.4 `codegenGetSequenceBounds` (_183) → bir `GetSequenceBounds`

`codegenGetSequenceBounds` (`0x1cfc30`) → bir Opcode `GetSequenceBounds` (CONFIRMED standalone). Computes the dynamic `[lo, hi]` bounds of a variable-length sequence — the masked/padded-seq length primitive used in attention / MoE. FLAVOR A **with explicit engine assignment**: `Instruction(Opcode.GetSequenceBounds)` → `setEngine(engineTrans(engine))` → src → `addAP(dst, is_output)` → `addInstruction`. Helper `_24 codegenGetSequenceBounds.ap_helper` (`0xde8d0`) builds the seq-bounds AP. The `engineTrans` (`_267`) mapper translates the Penguin engine → `birEngineType`.

### 7.5 Inline-ASM escape hatches — `codegenInlineASMInst` (_111) + `codegenInlineASMBytesInst` (_109)

Two inline-assembly escape hatches that emit a raw engine instruction the compiler treats opaquely.

- **`codegenInlineASMInst` (_111, `0x181360`)** — **structured** form. Uses a **dedicated** builder call `addInlineASMInst(opcode, asm_attrs, operands, num_inputs)` (not the generic `Instruction(Opcode.X)`). `num_inputs` (CONFIRMED) splits the operand list into inputs vs outputs; `asm_attrs` is the per-ISA attribute dict.
- **`codegenInlineASMBytesInst` (_109, `0x56a10`)** — **raw-bytes** form. `setAsmBytes(asm_bytes)` binds the raw machine-code byte string directly (the bytes *are* the encoded inst). Because the bytes are opaque, the scheduler must be **given** the sync semantics and latency explicitly: `setSyncType(sync_type)` + `setLatencyEstimate(latency_estimate)`. Engine via `setEngine(engineTrans(engine))`.

CONFIRMED: `addInlineASMInst`, `asm_attrs`, `num_inputs`, `setAsmBytes`, `setSyncType`, `setLatencyEstimate`. Both carry the literal `"'%.200s' object is unsliceable"` (operand-byte slicing).

### 7.6 The small misc lowerings

- **`codegenDropoutMaskInst` (_59, `0x6f7b0`) → bir `Dropout`** — generates the dropout keep/drop bit-mask. `set_keep_rate` ← `p` (the keep-probability), gated by `is_keep_rate`, bound as an `ImmediateValue` via `addImmValue`. `strip_fp32r` is the fp32-replicated unwrap shared with the reduces.
- **`codegenIndexValueInst` (_55, `0x15e110`) → iota / index-value** — produces a tile whose elements are an index/affine value. `set_base` (start), `step`, `set_pattern`, `set_channel_multiplier` (per-partition/channel stride), `depth`. The per-element value is a **quasi-affine** expression (`BirQuasiAffineExpr`, `block_index_expr`) of the partition/free indices (`idx_partition_ap` / `idx_free_ap`), and `lnc_id` folds the LNC (Logical-Neuron-Core / replica) id into the expression — so the generated indices are **multi-core-aware** (distinct per neuron core). This is the iota used for position / arange tensors.
- **`codegenStreamShuffleInst` (_159, `0x152200`) → stream-shuffle** — the lane/stream shuffle is encoded as a mask; `setMask(shuffle_mask)` binds it, and a genexpr closure (`__pyx_scope_struct_22_codegenStreamShuffleInst`) builds the per-lane `shuffle_mask`. This is the **only** matmul/compute-family codegen on this page that uses the `setMask` surface — the structured-sparse matmul (§2.3) does **not** (it carries `tags`/`compress_ratio`, not a boolean mask).
- **`codegenNoOp` (_51, `0x5c270`, ~2.3 KB)** — the no-op lowering; the smallest body (`inst` only, no attr setters). Emits a placeholder / drops the op — for ops with no silicon effect (scheduling / scope markers). The generic `_noop` (`_311`, `0x72c80`) is a *separate* Cython no-op shim.

---

## 8. Convergence / divergence vs. the klr→BIR twins

Every codegen on this page terminates at the **same bir L1 `Inst`** the C++ klr path reaches — they are **parallel emitters, not a pipeline**. The table maps each Penguin (beta3 / birpy) codegen to its bir node and its klr (beta2 / C++) twin.

| Penguin codegen (THIS / L2 birpy) | bir L1 Inst | klr twin (Lx / C++) |
|---|---|---|
| `codegenMatMulOp` | `Matmult` | `addMatmult` (klr `NcMatMul`) |
| `codegenMatMulMXOp` | `MatmultMx` | `MatMulMX` (stationaryScale/movingScale) |
| `codegenMatMulSparseOp` | `MatmultSparse` | klr sparse matmul |
| `codegenActivationOp` / `Activate2` | `Activation` / `ActivationAccumulationOp` | klr Activation |
| `codegenAffSelTensorScalarOp` / `GEPOp` | `TensorScalarPtr` | klr tensor-scalar (predicated) |
| `codegenMemsetOp` | `Memset` (IT10) | `codegenMemSet` |
| `codegenLocalReduceOp` | `Reduce` / `TensorReduce` (IT27) | `codegenTensorReduce` |
| `codegenNeuronReduceMacro` (multistep) | `Reduce` × N (`is_first_reduce`) | `TensorReduce` 1×`AxisListType` |
| `codegenSundaMax8` | `Max` (IT88) | `codegenMax8` |
| `codegenSundaMaxIndex8` | `MaxIndex` (IT89) | `codegenFindIndex8` |
| `codegenSundaMatchReplace8` | `MatchReplace` (IT90) | `codegenMatchReplace8` |
| `codegenMaxIndexAndMatchReplace` | `MaxIndexAndMatchReplace` (IT91) | `MatchReplace8` + idx flag → IT91 |
| `codegenOffloadedFMA` / `Tiled…` | `DMACopy` (+CCE op) | `NcDmaCopy` / `DmaCompute` |
| `codegenGetSequenceBounds` | `GetSequenceBounds` (IT64) | `codegenSequenceBounds` (k41) |
| `codegenLoadTensorToRegister` | `TensorLoad` (IT75) | `codegenTensorLoad` (IT75) |
| `codegenInlineASMInst` / `BytesInst` | inline-ASM inst | — (escape; no klr twin doc'd) |
| `codegenDropoutMaskInst` | `Dropout` | `codegenDropout` |
| `codegenIndexValueInst` | Iota / IndexValue (affine) | Iota |
| `codegenStreamShuffleInst` | stream-shuffle / mask | — (I-strand TBD) |
| `codegenNoOp` | no-op / dropped | — |

Three notable divergences, all in *how the same node is reached*, never in the node itself:

1. **Reduce expansion granularity** — `codegenNeuronReduceMacro` expands multiple axes to a multi-step `Reduce` chain (`is_first_reduce`), whereas the klr `InstTensorReduce` packs all axes into one `AxisListType` (§5.2).
2. **Fused/unfused max-index split** — two Penguin methods (`SundaMatchReplace8` vs `MaxIndexAndMatchReplace`) vs one klr op + a runtime index flag; both reach IT90 / IT91 (§6.2).
3. **Offloaded-FMA engine** — a `DMACopy` on the CCE/DMA engine (an *offload*), not a PE/Act compute inst; the FMA constants ride as DMA `scales` / `setconstants` (§7.2).

For the dispatcher that selects among these codegens, see [§6.5.10 BirCodeGenLoop](./bircodegenloop.md). For the BIR `Inst` data model they all produce, see [Part 7 — bir/](../bir/). For the parallel beta2/klr C++ emitter (`KlirToBirCodegen`), see the Part 7 KLR codegen pages.

---

## Confidence and corrections

**CONFIRMED** (nm / function_addresses / `.rodata` of this binary): the class + roster; the seven matmul/activation/tensor-scalar codegens and the tensor/reduce/copy/DVE/register-load/sequence-bounds/inline-asm/misc methods at the cited indices and addresses (re-verified against `function_addresses.json`); the module docstring; the `birpy.{Instruction, Opcodes, MemoryLocation, Function, Module, BirAffineExpr}` imports; the BIR class/Opcode tokens `MatmultMx` / `MatmultSparse` / `Activation` / `ActivationAccumulationOp` / `TensorScalarPtr` / `DMACopy` / `TensorLoad` / `GetSequenceBounds` / `MaxIndex` / `MaxIndexAndMatchReplace` / `Reduce` / `TensorReduce` / `MemsetMode{Const,Random}` / `DoubleRow`; the full setter vocabulary (`set_is_transpose` / `_is_weight_onezero` / `_is_fmap_onezero` / `set_perf_mode` / `set_tile_size` / `setnum_active_channels` / `set_psum_buf_shape` / `setIsActivate2` / `set_func[_args/_outs]` / `setreduce_op/_cmd` / `set_scale` / `setMask` / `set_replication_num_rows` / `setReduceAxes` / `is_first_reduce` / `setLocal` / `setReplicaGroups` / `setCceOp` / `setconstants` / `addSeqAccess` / `dst_registers` / `RegisterAccess` / `addRegister` / `addArgumentOrOutput` / `setAsmBytes` / `setSyncType` / `setLatencyEstimate` / `set_keep_rate` / `lnc_id` / `BirQuasiAffineExpr`); the MX scale fields (`lhs_scale` / `rhs_scale` / `getMxScalePartitionAp`); the affine-predicate surface (`BirAffinePredicate` / `predicates` / `enumerate_predicates_in_codegen_order`); the dual emit idiom (`build_debuginfo` / `id` / `addInstruction` vs `addInstToBir`); and all quoted validation literals.

**STRONG**: the emit-skeleton ordering; the weight/fmap ↔ stationary/moving operand mapping; AffSel→predicated-TS and GEP→indirect-TS folding; Activate2 sharing the Activation builder; the §8 convergence table; the bir-Inst ↔ IT-number mapping (via the klr twins).

**INFERRED**: the exact intra-body call order for the matmul/activation family (name-via-module-state blocks byte-tracing of those bodies; the misc/tensor family ordering, by contrast, was recovered from IDA disasm program order); the precise `MemsetMode.Random` RNG wiring; the BasicBlock-append call for FLAVOR-B ops.

**CORRECTIONS** issued in place: (1) layer 2 emits BIR *directly* via `birpy.Instruction`, not through klr (§1.1); (2) `NeuronReduceMacro` is a multi-step reduce, not a preprocessor macro (§5.2); (3) offloaded-FMA is a `DMACopy` on the CCE/DMA engine, not a PE/Act compute inst (§7.2).
