# NKI Architecture Overview & the 3-Layer Lowering Stack

> *All symbols, addresses, build-IDs, and string literals on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 Cython `.so` modules under `neuronxcc/nki/` and `neuronxcc/starfish/penguin/`). The cp311/cp312 copies are source-identical Cython; addresses are version-pinned — treat every offset as specific to this wheel.*

## Abstract

NKI — the **Neuron Kernel Interface** — is the second front door into `neuronx_cc`. Where the XLA path feeds an `HloModule` protobuf through `hlo2penguin` ([Part 4](../hlo-opt/)), the NKI path takes a *Python function* decorated with `@nki.jit`, **traces** it by abstract interpretation, and lands the result in the same Penguin IR the XLA descent produces. From there both front doors share one backend. This page is the orientation map for Part 6: it names every stage of the NKI descent, pins each to the real class/symbol/binary that owns it, and marks the two places where the naive reading of the pipeline is wrong. The deep mechanism of each stage is deferred to the page that owns it.

The descent has three conceptual layers, and the central subtlety is that the **lowest two are not a chain — they are parallel front-ends onto the same BIR node family.** Layer 1 is *tracing-and-building*: the `@nki.jit` body runs under a `TraceContext`, every `nl.*`/`nisa.*` op calls `nki_ctx()` to reach the semantic builder, and `NeuronCodegen` (in `KernelBuilder.cpython-310…so`) emits Penguin IR `Inst` nodes — `MatMulOp`, `ActivationOp`, `SBAtomLoad`, and ~150 siblings. Layer 2 is *Penguin → BIR codegen*: `BirCodeGenLoop` (in `…/targets/codegen/`) walks the Penguin `Inst` graph and constructs `birpy.Instruction` objects **directly** — there is no KLR AST between Penguin and BIR on the live (beta3) path. The KLR-based C++ lowering (`KlirToBirCodegen` in `libwalrus`) is the *other* (beta2) front-end onto the identical `bir::InstMatmult` family; the two converge only at the BIR data model, gated by a `beta2`/`beta3` switch the binary makes explicit.

The most important thing to internalize before reading any other Part-6 page: **`NeuronCodegen` is a forward builder, not a printer.** It consumes traced NKI ops and *produces* Penguin IR; it does not pretty-print or round-trip IR back to NKI text. The binary backs this directly — `KernelBuilder.cpython-310…so` is the most readable module in the whole stack (unstripped, with DWARF), its single public class `NeuronCodegen` carries the verbatim module docstring *"All the intermediate AST representation of Neuron Kernel Interface (nki)"*, and every emit method resolves the name `add_named_instruction` (a builder-append primitive), never a serialize-to-source one.

| | |
|---|---|
| **Front door** | `@nki.jit` Python kernel → traced (this Part) · vs. XLA HLO → `hlo2penguin` ([Part 4](../hlo-opt/)) |
| **Tracing engine** | `TraceContext` (`TraceContext.cpython-310…so`) + ambient `nki_ctx()` singleton accessor |
| **Layer 1 builder** | `NeuronCodegen` — single public class of `KernelBuilder.cpython-310…so` (14,588,400 B, BuildID `9eb1020e…`, **unstripped + DWARF**) |
| **Layer-1 output IR** | Penguin IR `Inst` nodes (`MatMulOp` / `ActivationOp` / `SBAtomLoad` / …; 94 generated `<Op>Gen` bases) |
| **Layer 2 (beta3, live)** | `BirCodeGenLoop` (`…/targets/codegen/BirCodeGenLoop.cpython-310…so`, 11,139,256 B, BuildID `cb19fae0…`) → `birpy.Instruction` **directly** |
| **Layer 2 (beta2, alt)** | `KlirToBirCodegen` (C++, `libwalrus`) ← KLR AST → `bir::InstMatmult(8)` ([Part 7](../bir/)) |
| **Convergence node** | both paths terminate at the BIR `Matmult`/`MatmultMx`/`Activation`/`TensorScalarPtr` family ([Part 7](../bir/)) |
| **Path selector** | option `"beta2"` (KLIR tracing, default) vs `"beta3"` (BIR compilation); env `NKI_FRONTEND` / `NKI_BETA3_FE` |

---

## 1. The two front doors, and where NKI joins the descent

`neuronx_cc` lowers through six IR levels — framework graph → XLA HLO → Penguin IR → BIR → per-engine ISA → NEFF ([1.0 pipeline](../front/pipeline.md)). The XLA front door owns the top two transitions: `libneuronpjrt` emits an `HloModule`, and the `HLOToTensorizer` job (`hlo2penguin`) lowers HLO → Penguin Python IR.

The **NKI front door skips both**. An `@nki.jit`-decorated Python function is not a graph protobuf; it is executable Python. NKI reaches Penguin IR by *running* that Python under an abstract-interpretation harness that replaces every tensor op with an IR-emit call. So the NKI entry point is exactly where XLA's two top jobs *finish*:

```text
  XLA path:   framework graph ─libneuronpjrt─► XLA HLO ─hlo2penguin─► Penguin IR ─┐
                                                                                  ├─► BIR ─► ISA ─► NEFF
  NKI path:   @nki.jit Python  ─TraceContext.trace_kernel─► NeuronCodegen ────────┘
                               (abstract interpretation)    (Penguin Inst emit)
```

This is why Part 6 is a *peer* of Part 4, not a sub-part: NKI is a complete alternative producer of Penguin IR, after which the middle-end ([Part 5](../penguin/)) and BIR codegen ([Part 7](../bir/)) are shared verbatim with the XLA path. The worked flash-attention example in [0.8](../front/worked-example-flash-attention.md) traces a full NKI kernel through this descent end-to-end.

> **NOTE — "trace", not "compile".** NKI tracing is *partial evaluation*: Python control flow (`if`/`while`/`range`) runs concretely at trace time; only tensor ops are deferred into IR. This is the source of NKI's defining constraints — fully-unrolled `static_range`, the ban on data-dependent `if`, and the SPMD launch-grid model — all owned by the tracing pages ([6.W tracing & SPMD](#)). The mechanism lives in §2; the consequences are a whole sub-Part.

---

## 2. Layer 1 — tracing and the forward builder `NeuronCodegen`

Layer 1 turns the running Python kernel into a Penguin `Inst` graph. It has two halves: the **tracing harness** that decides *what* to emit, and the **builder** that *does* the emitting.

### 2.1 The tracing harness — `TraceContext` + the ambient `nki_ctx()`

`TraceContext` (`neuronxcc/nki/compiler/backends/neuron/TraceContext.py`, Cython `.so`) is the abstract-interpretation engine. Its module docstring is verbatim *"This file define the TraceContext class, it define the interface for tracing a nki function."* A single process-global `TraceContext.global_ctx` (installed by `TraceContext.new_ctx`) is the cell every language intrinsic reads.

The harness draws the trace boundary around the user's kernel with three gates, all confirmed in the binary:

```c
// TraceContext.call(self, func, *args, **kwargs)  — the per-call-site dispatcher
// decides, for ONE callee, trace-into-IR vs run-concretely-in-Python.
if (is_nki_func(func))                                 // @nki-marked op
    return self.sema.<emit>(func, ...);                // -> Penguin IR via the semantic builder
if (getattr(func, "__nki_dont_trace__", False))        // dont_trace decorator marker
    return func(...);                                  // run concretely
mod = inspect.getmodule(func);
if ((isfunction(func) || isbuiltin(func)) &&
    module_in(mod, self._exclude_modules()))           // {neuronxcc.nki, .penguin, numpy, math}
    return func(...);                                  // library code -> concrete
return inline_function(func, ...);                     // trace the body (recurse, new scope)
```

The `__nki_dont_trace__` attribute string, `global_ctx`, `trace_kernel`, `sema`, and `cur_scope` are all present as name constants in `TraceContext.cpython-310…so`. The `sema` attribute is the **semantic builder** — `sema.cpython-310…so`, docstring *"NKI Semantic Analysis functions"* — and being non-`None` is the operational definition of "we are currently tracing a kernel."

Every `nl.*`/`nisa.*` intrinsic reaches that builder through the ambient accessor module `nki_ctx.py` (docstring *"the API to obtain the (thread local) global context of IR construction during NKI tracing"*):

```c
nki_ctx(allow_none=False):                 // the singleton + sema guard
    ctx = TraceContext.global_ctx;
    if (ctx == None || ctx.sema == None) { // not inside a traced kernel
        if (allow_none) return None;
        err_nki_api_outside_of_nki_kernel();   // raise: NKI API used outside a kernel
    }
    return ctx;
get_cur_scope():  return nki_ctx().cur_scope;   // the active emission scope
```

So the full path from a source-level op to the IR is: `nl.<op>(...)` → `nki_ctx()` (singleton + guard) → `ctx.sema` (semantic builder) → emit into `get_cur_scope()`. The SPMD launch-grid model (`program_id`/`num_programs`, `affine_range`/`sequential_range`/`static_range`, `nc()`/`cc_pipeline()` dims) rides on top of this same `sema` and is the subject of the tracing/SPMD pages.

### 2.2 The builder — `NeuronCodegen` (the central "forward builder")

`KernelBuilder.cpython-310…so` is the most readable binary in the NKI stack: **14,588,400 B, BuildID `9eb1020ebbb2a46b…`, ELF64 "with debug_info, not stripped."** Its single public class is `NeuronCodegen`, and its module docstring is verbatim:

> *"KernelBuilder.py - All the intermediate AST representation of Neuron Kernel Interface (nki)"*

`NeuronCodegen` is the **forward Inst-emit surface**: ~150 user-visible emit methods (194 distinct `__pyx_mdef_…NeuronCodegen_<N><name>` PyMethodDef symbols; method indices run to 379, the gap being Cython wrappers/closures/genexprs). Each method takes a traced NKI op and *constructs and appends* a Penguin IR `Inst` node. There is no inverse — `NeuronCodegen` never serializes IR back to NKI text; it is a pure producer.

The matmul family is the canonical example and is pinned exactly (method symbols read from `nm`):

| NKI primitive | `NeuronCodegen` method | idx (`13NeuronCodegen_<N>`) | Penguin `Inst` emitted |
|---|---|---|---|
| `nisa.nc_matmul` | `matmult` | `101` | `MatMulOp` |
| `nisa.nc_matmul_mx` | `matmult_mx` | `107` | `MatMulMXOp` (+ named `stationary_scale`/`moving_scale`) |
| sparse matmul | `matmult_sparse` | `103` | `MatMulSparseOp` |
| PE-array transpose | `matmult_transpose` | `105` | `MatMulOp` (`is_transpose`) |
| stream/DMA transpose | `transpose` | `111` | `TransposeOp` |
| identity operand | `get_identity_tensor` / `shared_identity_matrix` | `109` / `285` | cached 128×128 identity |

The emit shape is the same for every method: build the operand access patterns, construct the value-object, append it. For `matmult` (13 keyword args: `stationary, moving, psum, outputs, perf_mode, tile_position, tile_size, is_transpose, engine, name`, + `sb_shape`/`psum_shape` locals):

```c
// NeuronCodegen.matmult — nisa.nc_matmul → Penguin MatMulOp  (STRONG: vocab+contract, not byte-traced)
sb_shape, psum_shape = self.get_sb_and_psum_shape(stationary, moving, psum);
assert is_psum(psum);                          // "Result buffer of matmult must be psum!"
inst = MatMulOp(stationary=<w AP>, moving=<ifmap AP>, outputs=[<psum AP>],
                perf_mode=perf_mode,           // "double_row" / "double_row_gen3" / None
                tile_position=tile_position, tile_size=tile_size,   // 2-elt PE-tile descriptor
                is_transpose=is_transpose, engine=engine, name=name);
self.builder.add_named_instruction(inst);      // append to the current basic block
```

The op-class names (`MatMulOp`/`MatMulMXOp`/`MatMulSparseOp`/`TransposeOp`), the `add_named_instruction` append name, the `perf_mode` enum, the `"Result buffer of matmult must be psum!"` guard, and the `check_mx_scale` `[P/8,F/4]` E8M0 geometry literals are all `.rodata` string constants in this binary. The field lineage of a single attr runs all the way to BIR — e.g. `MatMulOp.tile_position` → BIR `tile_position[2]` — and is the subject of [6.5 codegen](#).

> **GOTCHA — `add_named_instruction` is *not* a `penguin.ir.IRBuilder` method.** This corrects an earlier reading. `add_named_instruction` appears as a name constant **only** in `KernelBuilder.cpython-310…so` (verified: 0 occurrences as symbol or string in `IRBuilder.cpython-310…so`). The `penguin.ir.IRBuilder` class is a *separate*, higher-level builder (§4) whose insert primitives are `insert` / `insert_inst`; its public roster is the HLO Operator vocabulary (`conv2d`/`softmax`/`matmul`/`all_reduce`), not the low-level NKI Inst emitters. `NeuronCodegen` resolves `add_named_instruction` on a builder it inherits via its generated base; same insert-family plumbing, different op vocabulary. Do not assume the two builders are one class.

---

## 3. Layer 2 — Penguin → BIR, and the beta2/beta3 fork

Once Layer 1 has a Penguin `Inst` graph, the program must reach **BIR** — the C++ backend IR (`bir::Module → Function → BasicBlock → Instruction`, ~110-opcode enum) that the Walrus backend consumes ([Part 7](../bir/)). There are **two** independent ways to make that crossing, and which one runs is a configuration switch the binary exposes directly.

### 3.1 The selector — `beta2` (KLIR) vs `beta3` (direct BIR)

`BirCodeGenLoop.cpython-310…so` carries an option docstring, verbatim:

```text
    - "beta2": Use beta2 KLIR tracing (default)
    - "beta3": Use beta3 BIR compilation
    Environment variable NKI_FRONTEND controls the path:
```

and two dispatch methods, `BirCodeGenLoop._trace_kernel_beta2` and `BirCodeGenLoop._trace_kernel_beta3` (confirmed `__pyx_mdef_…_236_trace_kernel_beta2` / `…_238_trace_kernel_beta3`), plus the env names `NKI_FRONTEND` and `NKI_BETA3_FE`. These are the two parallel front-ends onto BIR:

```text
  Penguin Inst graph (from §2.2 NeuronCodegen)
        │
        ├── beta3 (live) ─► BirCodeGenLoop.codegen<OpName>  ─► birpy.Instruction("Matmult")  ─┐
        │                   (Python/Cython, builds BIR DIRECTLY)                               │
        │                                                                                      ├─► bir::Inst
        └── beta2 (default tracing) ─► KLR AST ─► KlirToBirCodegen (C++, libwalrus)  ──────────┘
                                       e.g. klr::NcMatMul ─► bir::InstMatmult(8)
```

### 3.2 The beta3 path — `BirCodeGenLoop` emits BIR *directly*

`BirCodeGenLoop.cpython-310…so` (11,139,256 B, BuildID `cb19fae0…`, unstripped + DWARF) has the module docstring *"BirCodeGen - Generate Backend IR from tensoriser IR at the TongaISAInst level."* Its single public class `BirCodeGenLoop` carries ~120 `codegen*` per-instruction lowerings (173 PyMethodDef symbols).

The decisive fact: it constructs BIR nodes **directly through the Python BIR binding**. The imported BIR construction surface (confirmed `.rodata` module-path literals) is:

```text
neuronxcc.starfish.birpy.Instruction   neuronxcc.starfish.birpy.Opcodes
neuronxcc.starfish.birpy.MemoryLocation neuronxcc.starfish.birpy.Function
neuronxcc.starfish.birpy.Module        neuronxcc.starfish.birpy.BirAffineExpr
```

A representative lowering (matmul) names a `birpy.Instruction` class and binds operands/attrs onto it:

```c
// BirCodeGenLoop.codegenMatMulOp — Penguin MatMulOp → birpy Matmult  (INFERRED order; tokens CONFIRMED)
createMemLoc(stationary); createMemLoc(moving); createMemLoc(dst);   // 3 BIR MemoryLocations
getMatmultOperandType(...);                          // classify Stationary/Moving/Output(psum)
inst = Instruction("Matmult");                       // birpy.Instruction
inst.set_is_transpose(...); inst.set_is_weight_onezero(...); inst.set_is_fmap_onezero(...);
inst.set_tile_size(...); inst.set_perf_mode(DoubleRow if perf_mode=="double_row" else ...);
addInstToBir(inst);                                  // set_name / set_engine(PE) / addInstruction
```

The BIR Inst-class tokens `MatmultMx`, `MatmultSparse`, `Activation`, `TensorScalarPtr` are inline `.rodata` strings here; the full setter vocabulary (`set_is_transpose`, `set_perf_mode`, `set_func`, …) is confirmed.

> **CORRECTION — Layer 2 emits BIR directly; there is no Penguin → KLR → BIR chain.** An earlier draft of the matmul lineage drew Layer 2 as "Penguin IR → KLR AST → `klr::NcMatMul` → BIR." The binary contradicts the KLR-intermediate claim on this path: `BirCodeGenLoop` imports `birpy.Instruction`/`Opcodes`/`MemoryLocation` and builds BIR objects directly — there is no `klr::NcMatMul` emit in its matmul codegen. The only `klr`/`KLIR` tokens in this binary belong to a *separate* branch, `codegenExternalNativeNkiKlirKernel` (the external-NKI beta2 KLIR-tracing kernel path), not the per-instruction matmul lowering. The corrected picture is two **parallel** front-ends onto BIR (beta3 Python-`birpy` ‖ beta2 C++-KLR), converging at the shared `bir::Inst` data model, **not** a three-stage pipeline.

### 3.3 The beta2 path — KLR → `KlirToBirCodegen` (C++)

The default tracing mode (`"beta2"`) routes through a **KLR** (KLIR) intermediate AST and a C++ lowering, `KlirToBirCodegen` (in `libwalrus`), which turns `klr::NcMatMul` / `klr::MatMulMX` into `bir::InstMatmult(8)` / `bir::InstMatmultMx(95)`. This is the historically-first NKI→BIR path and is owned by the I-strand BIR-codegen pages ([Part 7](../bir/)). It is named on this map only to fix its relationship to beta3: the two are *alternatives that produce the same BIR*, selected by §3.1, never composed.

> **QUIRK — the internal-kernel registry lives in Layer 2, not Layer 1.** `_INTERNAL_KERNEL_REGISTRY` + `_build_internal_kernel_registry` + `get_internal_kernel_registry` are confirmed inside `BirCodeGenLoop.cpython-310…so` (consulted by `_trace_internal_kernel_to_new_nki_frontend` / `codegenExternalNativeNkiKlirKernel`). It is the compiled-vs-readable internal-kernel selector and is *not* part of the per-instruction `NeuronCodegen` emit surface of Layer 1. When you go looking for the "which kernel implementation do we use" decision, look in Layer 2.

---

## 4. The two builder tiers in Penguin — why "IRBuilder" is ambiguous

A reader new to this stack will meet *two* objects called "builder," and conflating them is the single most common error. They are distinct classes in distinct binaries with distinct op vocabularies.

| | TIER A — HLO Operator builder | TIER B — NKI Inst builder |
|---|---|---|
| **Class / binary** | `IRBuilder` (`penguin/ir/IRBuilder.cpython-310…so`, unstripped) | `NeuronCodegen` (`KernelBuilder.cpython-310…so`) |
| **Built by** | `hlo2penguin` frontend + autograd (`GradIRBuilder`) | the `@nki.jit` tracer (§2) |
| **Op vocabulary** | HLO ops: `conv2d`, `softmax`, `matmul`, `dense`, `all_reduce`, `dropout`, … (~100 methods) | hardware Inst ops: `MatMulOp`, `ActivationOp`, `SBAtomLoad`, … (~150 methods) |
| **Insert primitive** | `insert` / `insert_inst` / `create_bb` (confirmed method symbols) | `add_named_instruction` (resolved via generated base; §2.2 GOTCHA) |
| **Output node class** | `penguin.ir.Operator` subclasses (`SoftmaxOp`/`BinaryOp`/`TensorContractOp`/…) | the 94 generated `<Op>Gen` Inst bases |

Both build into a `penguin.ir.Function` (the "compilation unit"), and both ultimately feed Layer 2. But TIER A is the *high-level HLO graph* builder shared with the XLA front door, while TIER B (`NeuronCodegen`) is the *low-level hardware-Inst* emitter unique to NKI. The 94 Inst node types are generated bases (`targets/generated/<Op>Gen.so`), specialized per architecture by the per-target `ISAInst` modules (`TongaISAInst` = classic compute/memory/transpose; `SundaISAInst` = + MX matmul / DVE top-8 / LNC collectives / tiled softmax-rmsnorm), gated by the ordered target lattice `Tonga < Sunda < Cayman < CoreV4`. That node model and the per-target binding are owned by [Part 5 penguin/](../penguin/).

---

## 5. Stage map — the whole NKI descent in one table

This is the reference index for Part 6. Each row is a stage; the "owns" column is the page that traces its mechanism in depth.

| # | Stage | Real symbol / binary | Confidence | Owned by |
|---|---|---|---|---|
| T0 | install trace singleton | `TraceContext.new_ctx` → `TraceContext.global_ctx` | CONFIRMED | 6.W tracing |
| T1 | walk kernel body | `TraceContext.trace_kernel` / `trace_kernel_impl` | CONFIRMED | 6.W tracing |
| T2 | trace-vs-concrete dispatch | `TraceContext.call` (`__nki_dont_trace__`, `_exclude_modules`) | CONFIRMED | 6.W tracing |
| T3 | ambient builder access | `nki_ctx()` / `get_cur_scope()` → `ctx.sema` | CONFIRMED | 6.W tracing |
| T4 | SPMD launch grid | `program_id`/`num_programs`; `nc()`/`cc_pipeline()` dims | CONFIRMED | 6.W SPMD |
| **L1** | **emit Penguin Inst** | **`NeuronCodegen.<op>` (`KernelBuilder…so`) → `add_named_instruction`** | **CONFIRMED** | **§2, 6.5** |
| L1a | Inst node model | 94 `<Op>Gen` bases + `Tonga/Sunda ISAInst` | CONFIRMED | [Part 5](../penguin/) |
| — | path selector | `_trace_kernel_beta2` / `_trace_kernel_beta3`; `NKI_FRONTEND` | CONFIRMED | §3.1 |
| **L2-β3** | **Penguin → BIR (live)** | **`BirCodeGenLoop.codegen*` → `birpy.Instruction`** | **CONFIRMED** | **§3.2, 6.5** |
| L2-β2 | KLR → BIR (alt) | `KlirToBirCodegen` (C++, `libwalrus`) | STRONG | [Part 7](../bir/) |
| L3 | BIR data model | `bir::Module/Function/Instruction` (~110 opcodes) | CONFIRMED | [Part 7](../bir/) |

---

## 6. Adversarial verification ledger

The five strongest claims on this page, each re-challenged against the binary:

1. **`NeuronCodegen` is the single public class of an unstripped `KernelBuilder.so`.** ✅ `file` reports "with debug_info, not stripped"; BuildID `9eb1020ebbb2a46b…`; size 14,588,400 B; 456 `13NeuronCodegen` mangled symbols; module docstring *"…All the intermediate AST representation of …(nki)"* read verbatim from `.rodata`.
2. **The beta2/beta3 fork is real and selectable.** ✅ `BirCodeGenLoop._trace_kernel_beta2` / `_trace_kernel_beta3` are method symbols; the option doc *"beta2: Use beta2 KLIR tracing (default)"* / *"beta3: Use beta3 BIR compilation"* and *"Environment variable NKI_FRONTEND controls the path"* are `.rodata` strings; `NKI_BETA3_FE` present.
3. **Layer 2 (beta3) emits BIR directly via `birpy`, not via KLR.** ✅ `BirCodeGenLoop` imports `birpy.Instruction`/`Opcodes`/`MemoryLocation`/`Function`/`Module`/`BirAffineExpr`; the only `klir`/`NcMatMul`-adjacent tokens are confined to `codegenExternalNativeNkiKlirKernel`. The earlier "Penguin→KLR→BIR" chain is corrected in §3.2.
4. **`add_named_instruction` is not an `IRBuilder` method.** ✅ 0 occurrences (symbol or string) in `IRBuilder.cpython-310…so`; the string is present in `KernelBuilder…so`; `IRBuilder`'s confirmed insert primitives are `insert`/`insert_inst`/`create_bb` and its op roster is HLO (`matmul`/`softmax`).
5. **The matmul method indices are exact.** ✅ `nm` confirms `13NeuronCodegen_101matmult`, `_103matmult_sparse`, `_105matmult_transpose`, `_107matmult_mx`, `_109get_identity_tensor`, `_111transpose`, `_285shared_identity_matrix`.

**INFERRED / not pinned here.** (a) The exact in-body call order of each emit method — Cython routes attribute names through the module-state struct, so a byte-traced GetAttr/Call list is blocked; emit shapes are reconstructed from string vocabulary + line ranges + the downstream BIR contract (STRONG, not byte-traced). (b) This page does **not** locate a `penguin.ir → NKI-text` *re-emit printer* class inside this wheel — no such symbol/string (`NkiCodegen`/`NkiPrinter`/`to_nki`) was found in the cp310 modules. If a re-emit printer exists, it is in a different artifact (e.g. a standalone NKI wheel) and is out of scope for the neuronx-cc descent; `NeuronCodegen` here is unambiguously a *forward builder only*, and the "forward-builder vs re-emit-printer" distinction is resolved on this map in favor of the forward builder being the one binary-confirmed surface.
