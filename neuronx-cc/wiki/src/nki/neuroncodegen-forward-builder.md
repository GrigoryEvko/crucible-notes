# NeuronCodegen — the Forward Builder (Overview & Matmul)

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22, recovered from the **cp310** wheel. The subject is the Cython extension `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.cpython-310-x86_64-linux-gnu.so` — an ELF64 shared object shipped **unstripped, with DWARF debug-info**, which makes it the most readable binary in the entire NKI codegen stack. Method addresses below are `.text` virtual addresses inside that `.so` (VA == file-offset for `.text`/`.rodata`); the `__pyx_pw_*`/`__pyx_pf_*` symbol names are the Cython-mangled method labels read straight from the symbol table. A byte-near-identical twin ships at `neuronxcc/generated/nki/.../KernelBuilder.cpyt…` (differing only in the embedded build-path metadata) and exposes the **`GeneratedNeuronCodegen`** base class; cp311/cp312 carry their own copies. Other wheels and Python versions renumber addresses — treat every value as version-pinned.*

## Abstract

`NeuronCodegen` is the **forward builder** of the NKI compiler: the single public class of `KernelBuilder.py` (Cython-compiled to `KernelBuilder.so`), whose ~150 emit methods translate one lowered `nl`/`nisa` Python call at a time into one **Penguin-IR instruction node**. It is the **Penguin-IR-producing front layer** of the matmul lowering descent. When a kernel author writes `nisa.nc_matmul(out, w, x)`, the call ultimately reaches `NeuronCodegen.matmult`, which validates the operands, computes the SBUF/PSUM shape split, constructs a Penguin `MatMulOp` value-object, and appends it to the current basic block via the inherited `add_named_instruction` primitive. Downstream, the Penguin IR reaches BIR through **one of two parallel front-ends** ([6.5.0](architecture-overview.md), [6.5.10](bircodegenloop.md)): the **beta3** driver `BirCodeGenLoop.codegenMatMulOp`, which builds `birpy`/`bir::Instruction` objects **directly** (no KLR), or the **beta2** path `KlirToBirCodegen::codegenNcMatMul` (klr→BIR, the separate `libwalrus` C++ driver). The two are *alternatives that produce the same BIR*, not serial stages; this page owns the Penguin-IR-producing layer.

That the two descents are alternatives rather than stages is visible in the binary. `BirCodeGenLoop.so` imports `neuronxcc.starfish.birpy.{Instruction,Opcodes,MemoryLocation,Function,Module,BirAffineExpr}`, and its `codegenMatMulOp` / `codegenMatMulMXOp` / `codegenMatMulSparseOp` build `birpy` objects directly — there is no klr AST anywhere in the matmul lowering. The only `klr`/`KLIR` tokens in that module belong to the *separate* `codegenExternalNativeNkiKlirKernel` branch. `BirCodeGenLoop` (beta3) and `KlirToBirCodegen` (beta2/klr) are therefore parallel drivers onto one `bir::Inst` model; the layer diagram in [§1](#1-where-neuroncodegen-sits--the-front-layer--two-parallel-bir-descents) shows the klr stage only on the beta2 branch, where `KlirToBirCodegen` genuinely is downstream of a klr AST.

> **GOTCHA — the descent is not a serial three-stage chain.** The natural reading of the module names — Penguin → `BirCodeGenLoop` → klr → `KlirToBirCodegen` → BIR — is wrong. `BirCodeGenLoop` never produces klr; it emits `birpy` instructions itself. Pick one of the two drivers per compilation, selected by the `beta2`/`beta3` switch. See [6.5.0](architecture-overview.md) and [6.5.10](bircodegenloop.md).

The module docstring, recovered verbatim from `.rodata`, states the class's job exactly:

```text
KernelBuilder.py - All the intermediate AST representation of Neuron Kernel Interface (nki)
```

The reader should leave this page able to (a) place `NeuronCodegen` precisely in the lowering stack and distinguish it from the *re-emit printer* that walks Penguin IR back to NKI text ([NkiCodegen printer](nkicodegen-printer.md)); (b) reimplement the four matmul-family emitters — `matmult`, `matmult_mx`, `matmult_sparse`, `matmult_transpose` — naming the real method symbol, the Penguin `Op` class each constructs, and the per-Op keyword vocabulary; and (c) understand the cached 128×128 identity matrix used by the transpose-via-matmul path. Two facts dominate everything below. First, `matmult` is the **largest single method in the binary** — its arg-parse wrapper at `0x266520` is 64,948 bytes, and `matmult_mx` at `0x279fe0` is larger still at 106,470 bytes; these are not thin shims but the geometric-validation heart of the PE-array contract. Second, the *emit* primitive is `add_named_instruction`, a name the body resolves and calls on an inherited builder — **not** a method of the separate HLO-facing `penguin.ir.IRBuilder` class (which inserts via `insert`/`insert_inst`); confusing the two is the single most common layer-1 error, and [§2.4](#24-the-emit-add_named_instruction-not-an-irbuilder-method) pins the distinction.

| | |
|---|---|
| **Binary** | `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.cpython-310…so` (unstripped, DWARF) |
| **Public class** | `NeuronCodegen` (Cython prefix `…_13KernelBuilder_13NeuronCodegen_`) |
| **Base class** | `GeneratedNeuronCodegen` (in the `generated/` twin) — supplies `add_named_instruction` |
| **Role** | layer 1: lowered `nl`/`nisa` call → **one Penguin-IR Inst node** |
| **Emit primitive** | `add_named_instruction` (inherited; resolved & called, *not* a local `__pyx_pw` method) |
| **Output IR** | Penguin `MatMulOp` / `MatMulMXOp` / `MatMulSparseOp` / `TransposeOp` ([tensor-op family](../penguin/tensor-op-family.md)) |
| **Downstream (two parallel paths)** | **beta3:** `BirCodeGenLoop.codegenMatMul*Op` → `birpy`/`bir::InstMatmult(8)`/`InstMatmultMx(95)` **directly** (no klr). **beta2:** `KlirToBirCodegen::codegenNcMatMul*` (klr→BIR). Not a serial chain — see [6.5.0](architecture-overview.md)/[6.5.10](bircodegenloop.md) |
| **Re-emit printer (NOT this)** | the Penguin-IR → NKI-text printer ([NkiCodegen printer](nkicodegen-printer.md), page 6.5.9) |

---

## 1. Where NeuronCodegen sits — the front layer + two parallel BIR descents

A single `nisa.nc_matmul` first becomes a Penguin IR node here (THIS PAGE); from there it reaches BIR through **one of two parallel front-ends** (beta3 ‖ beta2), not a serial 3-stage chain. This page owns the **Penguin-IR-producing** layer.

```text
  FRONT     nl/nisa Python  →  Penguin IR        NeuronCodegen.matmult*          (THIS PAGE)
            KernelBuilder.so                      builds MatMulOp / MatMulMXOp /
                                                  MatMulSparseOp / TransposeOp
                  │                               via self.add_named_instruction
                  ▼
            Penguin IR  ──────────────┬───────────────────────────────┐
                                      │ beta3 (default Penguin path)   │ beta2 (klr path)
                                      ▼                                ▼
            BirCodeGenLoop.so: codegenMatMulOp /     KlirToBirCodegen (libwalrus):
            codegenMatMulMXOp / codegenMatMulSparseOp   codegenNcMatMul / codegenNcMatMulMX
            builds birpy/bir DIRECTLY (no klr)       ← klr::NcMatMul / klr::MatMulMX AST
                                      │                                │
                                      └──────────────┬─────────────────┘
                                                     ▼
                                      bir::InstMatmult(8) / InstMatmultMx(95)   (same BIR model)
```

The two descents converge on the identical `bir::Inst` data model and are selected by the `beta2`/`beta3` switch ([6.5.0 §3.1](architecture-overview.md)). `BirCodeGenLoop` (beta3) constructs `birpy` objects directly, so there is no Penguin→klr stage on that path; the klr AST exists only on the beta2 descent.

The layering is real, not nominal: `NeuronCodegen` is the per-instruction emit *surface* that runs first; the compiled-vs-readable kernel selector `_INTERNAL_KERNEL_REGISTRY` is **not** here — it lives in layer 2 (`BirCodeGenLoop.so`), consulted later. Keeping the layers straight matters because the same conceptual field — say `tile_position` — is a named Python kwarg here, then either a `birpy` Instruction attribute set directly by `BirCodeGenLoop` (beta3) or a klr slot then a `tile_position[2]` BIR array via `KlirToBirCodegen` (beta2).

> **NOTE — forward builder vs. re-emit printer.** `NeuronCodegen` *builds* Penguin IR going **forward** (NKI → IR). A **separate** class, the re-emit printer ([NkiCodegen printer](nkicodegen-printer.md), page 6.5.9), walks Penguin IR **backward** to regenerate human-readable NKI text. They share neither code nor direction; do not conflate "NeuronCodegen" (forward) with the printer (backward). This page found **no** `NkiCodegen` class symbol inside `KernelBuilder.so`; the printer lives elsewhere in the package, and 6.5.9 owns it.

### 1.1 The class and its method roster

The Cython mangling prefix shared by every method is:

```text
__pyx_{pw,pf,mdef,gb,doc}_9neuronxcc_3nki_8compiler_8backends_6neuron_13KernelBuilder_13NeuronCodegen_<IDX><name>
```

where `pw_` is the argument-parsing wrapper (and in this binary usually holds the inlined body too), `pf_` the inner body, `mdef_` the `PyMethodDef`, `gb_` a generator body, and `doc_` a docstring. The class carries **194** distinct `__pyx_mdef_…NeuronCodegen_<N><name>` method-definition symbols, with indices running up to 379 — the gap between 194 mdefs and index 379 is wrappers, generator bodies, and `<locals>` closures/genexprs that share a parent method's number. The 194 mdefs ground the "~150 user-visible emit methods" figure (≈150 methods + ~44 helper closures).

The imports that matter for matmul — all recovered as `.rodata` module-path literals referenced by the binary — establish what `NeuronCodegen` consumes and produces:

```text
  neuronxcc.starfish.penguin.ir.IRBuilder           ← the Penguin-IR builder lineage
  neuronxcc.starfish.penguin.ir.AffineExpr / .Axis  ← index algebra + loop axes
  neuronxcc.starfish.penguin.targets.transforms.InstBuilder
  neuronxcc.starfish.penguin.targets.transforms.LowerTranspose   ← the transpose-lowering pass
  neuronxcc.starfish.penguin.targets.sunda.Sunda / .SundaISAInst  ← Trn2 ISA (MX path)
  neuronxcc.starfish.penguin.targets.tonga.TongaISAInst          ← Trn1 baseline ISA
  neuronxcc.starfish.penguin.targets.core_v4.CoreV4
  neuronxcc.starfish.penguin.targets.cayman.Cayman
```

`NeuronCodegen` *imports* the builder/ISA layer and *emits into it*; the `MatMulOp` family node classes themselves are generated `<Op>Gen` bases bound per-target in `SundaISAInst`/`TongaISAInst` ([tensor-op family](../penguin/tensor-op-family.md)).

### 1.2 The matmul-family method indices

Every index, name, address, and body size below was read directly from the `KernelBuilder.so` symbol+function table. The double index `N`/`N+1` is Cython's convention: the `pf`/`mdef` carry the even number `N`, the `pw` wrapper the odd `N+1`.

| idx (pf/pw) | method | wrapper symbol (`…NeuronCodegen_`) | `pw` addr | body size | `KernelBuilder.py` lines |
|---|---|---|---|---|---|
| 100/101 | `matmult` | `101matmult` | `0x266520` | 64,948 B | def @ 777 |
| 102/103 | `matmult_sparse` | `103matmult_sparse` | `0x1d3c20` | 36,075 B | def @ 908 |
| 104/105 | `matmult_transpose` | `105matmult_transpose` | `0x127670` | 10,730 B | def @ 1019 |
| 106/107 | `matmult_mx` | `107matmult_mx` | `0x279fe0` | 106,470 B | def @ 1056 |
| 108/109 | `get_identity_tensor` | `109get_identity_tensor` | `0xa3470` | 14,026 B | def @ 1220 |
| 110/111 | `transpose` | `111transpose` | `0x2235f0` | 12,911 B | def @ 1269 |
| 284/285 | `shared_identity_matrix` | `285shared_identity_matrix` | `0x7b490` | 4,915 B | def @ 4426 |
| 92/93 | `get_sb_and_psum_shape` | `93get_sb_and_psum_shape` | `0x6ec10` | 2,830 B | def @ 700 (matmult shape helper) |

The indices 101 / 103 / 105 / 107 (`matmult` / `matmult_sparse` / `matmult_transpose` / `matmult_mx`) and the addresses above come straight from the `KernelBuilder.so` `__pyx_pw_*` symbol table and match the index table in the architecture overview exactly. The MX scale-partition split closure `__pyx_pw_…NeuronCodegen_10matmult_mx_1split_par_shape` is present as its own symbol.

The **source order** in `KernelBuilder.py` is `get_sb_and_psum_shape`(700) < `matmult`(777) < `matmult_sparse`(908) < `matmult_transpose`(1019) < `matmult_mx`(1056) < `get_identity_tensor`(1220) < `transpose`(1269) < `shared_identity_matrix`(4426) — a useful sanity anchor when cross-reading line numbers in any DWARF dump. Every one of those numbers is the `DW_AT_decl_line` of the corresponding `__pyx_pw_…NeuronCodegen_<idx>` subprogram DIE, readable with `objdump --dwarf=info`; note that the whole matmul family except `get_sb_and_psum_shape` sits *after* `matmult`, and `shared_identity_matrix` lives near line 4426, far down the file.

### 1.3 Module-level tile-combine helpers

Three **free functions** at module scope (not `NeuronCodegen` methods) recombine per-column / per-double-row matmul tiles back into one logical matmul — the Penguin-side analog of the column-tiled accumulate group at the BIR layer. All three names are string literals in the binary:

```text
  combine_matmult_tiles
  combine_sparse_matmult_tiles
  combine_trn2_double_row_matmult_tiles
```

These tie the matmul family to the column / double-row tiling that the PE array imposes (a single PE pass contracts at most 128 partition rows; larger `K` is split into column tiles that accumulate into the same PSUM region).

---

## 2. `matmult` — `nisa.nc_matmul` → Penguin `MatMulOp`

### 2.1 The source primitive

`nisa.nc_matmul(dst, stationary, moving, …)` computes `dst = stationaryᵀ @ moving`, contracting over the 128-row **partition** dimension. `stationary` is the weight matrix loaded resident in the 128×128 PE systolic array; `moving` is the IFMAP streamed through it; `dst` is the PSUM accumulator (always fp32). `NeuronCodegen.matmult` is the emit method that receives this lowered call.

### 2.2 Method signature — 14 keyword arguments

The keyword count is readable without decompiling the body: `__pyx_pw_…NeuronCodegen_101matmult` issues **14** `__Pyx_GetKwValue_FASTCALL` calls, one per keyword parameter. (`matmult_mx`, for contrast, issues 11.)

The parameter/local name set, from the interned `.rodata` identifiers tied to the `matmult` code object, is:

```text
  stationary, moving, psum, outputs, perf_mode, tile_position, tile_size,
  is_transpose, engine, name                (+ sb_shape / psum_shape locals)
```

`psum`/`outputs` are the destination PSUM result; `sb_shape`/`psum_shape` are locals produced by the shape helper ([§2.3](#23-shape-split-get_sb_and_psum_shape)). The byte position of each of the 14 `GetKwValue` calls maps the keyword *order*, but the keyword **names** are not captured as direct string references — Cython routes them through the module-state struct — so the name-to-position binding is reconstructed from the vocabulary rather than byte-traced.

### 2.3 Shape split — `get_sb_and_psum_shape`

`matmult` calls `self.get_sb_and_psum_shape` (method 92/93, body `0x6ec10`) to split the operand shapes into the SBUF-side shape `sb_shape` (covering `stationary`/`moving`) and the PSUM-side shape `psum_shape` (the result geometry). The destination-buffer guard is a `.rodata` literal:

```text
  "Result buffer of matmult must be psum!"
```

i.e. `matmult` asserts the `dst` is a PSUM buffer before emitting. This mirrors the hardware: the PE array can only accumulate into PSUM.

### 2.4 The emit — `add_named_instruction` (not an IRBuilder method)

The body constructs a Penguin `MatMulOp` value-object and appends it through the inherited builder. The emit below is reconstructed: its shape follows from the `MatMulOp` kwarg vocabulary plus the `add_named_instruction` call, rather than a byte-traced call list, because the per-name loads go through the Cython module-state indirection.

```c
/* NeuronCodegen.matmult, pw body @ 0x266520 (KernelBuilder.so, lines ~777-880).
   stationary = PE-resident weights, moving = streamed IFMAP, dst = PSUM (fp32). */
PyObject *matmult(self, stationary, moving, psum, outputs, perf_mode,
                  tile_position, tile_size, is_transpose, engine, name, ...)
{
    /* (1) split operand geometry into the SBUF and PSUM halves */
    (sb_shape, psum_shape) = self.get_sb_and_psum_shape(stationary, moving, psum);

    /* (2) destination must be PSUM — the PE array can only accumulate there */
    if (!is_psum(psum))
        raise("Result buffer of matmult must be psum!");

    /* (3) double_row geometry validation (see §2.5) is interleaved here */
    if (perf_mode == "double_row" || perf_mode == "double_row_gen3")
        validate_double_row_geometry(stationary, moving, dtype, target);

    /* (4) build the Penguin MatMulOp node and append it to the current BB.
       add_named_instruction is INHERITED (GeneratedNeuronCodegen/IRBuilder
       lineage) — it is NOT a penguin.ir.IRBuilder method. */
    inst = MatMulOp(stationary=stationary_AP, moving=moving_AP,
                    outputs=[psum_AP],
                    perf_mode=perf_mode, tile_position=tile_position,
                    tile_size=tile_size, is_transpose=is_transpose,
                    engine=engine, name=name);
    self.add_named_instruction(inst);          /* the layer-1 emit */
    return ...;
}
```

Three observations pin down where `add_named_instruction` actually lives. The string is present in `KernelBuilder.so` as a name constant, so the body definitely resolves and calls it. It is *not* a `__pyx_pw_*`/`__pyx_pf_*` method symbol of this `.so` — the function symbol table has no entry for it — so it is not defined here. And it is not a method of `penguin.ir.IRBuilder` either: that class's insertion primitives are `insert` and `insert_inst`, and it builds the high-level HLO Operator graph (conv, softmax, collectives), a different surface entirely. What remains is the `GeneratedNeuronCodegen` base, the gen-base split that supplies the low-level Inst-naming primitive. `add_named_instruction` uses the same `insert`-family plumbing as `IRBuilder` over a different op vocabulary; model it as "name `inst` and append it to the cursor's current basic block."

> **GOTCHA — `add_named_instruction` is not an `IRBuilder` method.** The name is easy to attribute to `penguin.ir.IRBuilder`, which `KernelBuilder.so` does import. It arrives instead through the `GeneratedNeuronCodegen` base class; a reimplementation should put it on the gen base, not on the HLO `IRBuilder`.

The operand marshalling is the `{stationary, moving}` pair plus the single-element `outputs=[psum]` list; `tile_position`/`tile_size` are passed straight through (the same 2-element tile descriptor that the BIR layer reads as `tile_size[2]`/`tile_position[2]`).

### 2.5 `perf_mode` and `is_transpose`

`perf_mode` selects the PE double-pumping mode. The enum *values* are `.rodata` literals:

```text
  "double_row", "double_row_gen3"    (+ None/Default = no special mode)
```

`matmult` validates double-row geometry with these error literals, each naming a hard constraint a reimplementer must enforce:

```text
  "The double_row matmult only support fp8e4m3 and fp8e5m2"
  "The double_row matmult only support uint8"
  "first F dim of LHS and RHS of the double_row matmult must be 2"
  "The last dimension of double_row matmult must have size of 2"
  "The first dimension of Cayman double_row matmult must have size of 2"
  "perf_mode=`double_row_gen3` is not supported on <target>"
```

The double-row path is backed by locals `lhs_free_and_double_row_shape`, `double_row_indices`, `is_fp8_kernel`, and the `combine_trn2_double_row_matmult_tiles` recombiner. At the BIR layer this `perf_mode="double_row"` becomes the numeric `DoubleRow(1)` code — the field name matches end-to-end.

`is_transpose` is a boolean kwarg selecting the PE-array transpose mode (matmul-against-identity performed by the PE engine itself). Its validation literal — `"'nc_matmul' transpose mode on trn2 only supports matching input dtypes…"` — gates the transpose-via-matmul trick that `matmult_transpose` ([§5](#5-transpose--matmult_transpose--the-identity-matrix-cache)) drives.

### 2.6 The accumulate group is structural

`matmult` does **not** itself resolve PSUM start/stop accumulate flags. The accumulate-group identity is carried *structurally*: by the `tile_position == [0,0]` head, the `combine_matmult_tiles` column-tile recombination, and the shared-PSUM `outputs` target. No explicit accumulate-group-id field appears anywhere in the `matmult` vocabulary; grouping is by shared PSUM plus tile position. The downstream "head iff `tilePosition[0]|tilePosition[1]==0`" rule reads exactly the `tile_position` this emitter sets.

---

## 3. `matmult_mx` — `nisa.nc_matmul_mx` → Penguin `MatMulMXOp`

### 3.1 The source primitive

`nisa.nc_matmul_mx(dst, stationary, moving, stationary_scale, moving_scale)` is the **5-operand** microscaling matmul: E8M0 per-32-element block scales ride *inside* the matmul, with activations online-quantized to FP8 (via `nisa.quantize_mx`). This is the op the BIR layer lowers to `bir::InstMatmultMx(95)`. `matmult_mx` (`0x279fe0`, 106,470 B) is the single largest method in the binary — almost entirely scale-geometry validation.

### 3.2 Method and Op

```text
  Op class : MatMulMXOp                          (.rodata)
  Op kwargs: stationary, moving, stationary_scale, moving_scale, perf_mode,
             tile_position, tile_size, is_transpose, outputs, engine, name
  scale ops: stationary_scale, moving_scale      (.rodata — the TWO E8M0 scale operands)
```

Reconstructed emit:

```c
/* NeuronCodegen.matmult_mx, pw body @ 0x279fe0 (lines ~1056-1135+).
   11 GetKwValue calls. The two E8M0 scale operands are FIRST-CLASS kwargs here. */
PyObject *matmult_mx(self, stationary, moving, stationary_scale, moving_scale, ...)
{
    (P_split, F_split) = split_par_shape(...);      /* <locals> closure, idx 10 */
    check_mx_scale(stationary, stationary_scale);    /* module helper, see §3.3 */
    check_mx_scale(moving, moving_scale);

    inst = MatMulMXOp(stationary=w_AP, moving=ifmap_AP,
                      stationary_scale=w_scale_AP, moving_scale=ifmap_scale_AP,
                      outputs=[psum_AP], tile_position=tp, tile_size=ts, name=name);
    self.add_named_instruction(inst);
    return ...;
}
```

> **NOTE — named scales here, positional downstream.** The two scale operands are **named** kwargs of `MatMulMXOp` (`stationary_scale` / `moving_scale`). At the klr/BIR layer they are flattened to ordinary positional operands (`moving_scale → arg2`, `stationary_scale → arg3`). The Penguin IR keeps them named; the positional ordering the BIR layer enforces is produced by that flattening, not by the emitter here.

### 3.3 Scale validation — `check_mx_scale` + `split_par_shape`

`matmult_mx` holds a `<locals>` closure `split_par_shape` (symbol `…NeuronCodegen_10matmult_mx_1split_par_shape`) and calls the module helper `check_mx_scale` (a `.rodata` name) to validate the `[P/8, F/4]` E8M0 block-scale geometry. The `check_mx_scale` error literals reveal that geometry directly:

```text
  "src_buffer and index must have same partition dimension size"
  "partition dimension size must be a multiple of 16"
  "<scale> must have indexing i * 32 + j at partition dimension for src tensor with
    more than 32 partitions but got <…>"
  "<…> (P // 8) partitions for src with <…>"
  "Unexpected tile shape! more than 1 partition dimensions?"
```

This is the Penguin-level enforcement of the OCP MXFP block geometry: `block_size = 32` elements (8 partitions × 4 columns), one E8M0 byte per 32-element block, partition counts a multiple of 16, scale access pattern `i*32 + j`. A reimplementer copies these five predicates verbatim.

### 3.4 MX restrictions

```text
  "nc_matmul_mx does not support column PE tiling"     (.rodata)
```

`matmult_mx` forbids column tiling — `MatMulMXOp` is excluded from the column-tiled accumulate grouping that the plain path uses. The data dtype is x4-packed FP4/FP8 (`float4_e2m1fn_x4`, `float8_e4m3fn_x4`, `float8_e5m2_x4`); the scale dtype is E8M0 (uint8). MX is always a PE, non-transpose op — it does not exercise the `is_transpose`/`perf_mode` special cases the plain path does.

---

## 4. `matmult_sparse` — sparse matmul → Penguin `MatMulSparseOp`

```text
  Op class : MatMulSparseOp                       (.rodata)
  genexpr  : NeuronCodegen.matmult_sparse.<locals>.genexpr   (operand marshalling)
  helper   : combine_sparse_matmult_tiles         (module-level tile recombiner)
```

Validation literals:

```text
  "matmult_sparse tile_size must be a 2D tuple"
  "matmult_sparse tile_position must be a 2D tuple"
  "matmult_sparse cannot work with column tiling"
```

Sparse matmul requires 2-D `tile_size`/`tile_position` (the same 2-element tile descriptor as the plain and MX paths) and **forbids** column tiling. `MatMulSparseOp` is the Penguin node that layer 2 (`BirCodeGenLoop.codegenMatMulSparseOp`, with its `addSparseMatmulAP` access-pattern builder) lowers toward the BIR sparse-matmul path. The silicon sparse-PE encoding itself belongs to the ISA pages; what is readable here is the Op name, the genexpr operand marshalling, and the AP-builder hand-off.

---

## 5. transpose / matmult_transpose / the identity-matrix cache

The hardware has no native transpose unit on the PE array; a transpose is *computed* by multiplying the input against an **identity** stationary matrix (`Iᵀ @ X = Xᵀ`, with `is_transpose` set), by a vector-engine stream-shuffle, or by a DMA-engine transpose. `NeuronCodegen` exposes both the high-level `transpose` emitter and the lower-level `matmult_transpose` it can lower to.

### 5.1 `matmult_transpose` (idx 104/105, `0x127670`)

The PE-array transpose-via-matmul-against-identity emitter. It pairs the input with an identity `stationary` matrix and emits a `MatMulOp` with `is_transpose` semantics, so the PE array produces the transpose directly. Engine = PE/Tensor. It shares `matmult`'s `MatMulOp` + `is_transpose` machinery (same Op class, same emit path) and draws its identity operand from `get_identity_tensor` / `shared_identity_matrix` ([§5.3](#53-the-identity-matrix-cache)).

### 5.2 `transpose` (idx 110/111, `0x2235f0`)

Emits a Penguin `TransposeOp` (`.rodata`), later lowered by the `LowerTranspose` pass (`…targets.transforms.LowerTranspose`, imported §1.1) into the engine-specific realization. The engine selector is gated by a validation literal:

```text
  "Transpose engine can only be Tensor or Vector or Unknown."
```

so the transpose engine ∈ {Tensor (PE, the identity-matmul path), Vector (a vector-engine shuffle), Unknown (the pass decides)}. The transpose variants and permutation guards are `.rodata` literals:

```text
  variants  : CaymanPackedPETranspose, DMATransposeLoad, DMATransposeCopy,
              DMAIndirectTranspose
  perm guards: " provided for 2D transpose, only (1, 0) supported."
               " provided for 3D transpose, only (2, 1, 0) supported."
               " provided for 4D transpose, only (3, 1, 2, 0) supported."
               " provided for gather transpose, only 3D supported."
```

i.e. `transpose` supports only the full-reverse permutation per rank; the locals `transpose_type`/`transpose_dtype` select the variant and the identity dtype. (The DMA-engine variants are routed through a separate `dma_transpose` method, idx 112/113.)

### 5.3 The identity-matrix cache

`get_identity_tensor` (idx 108/109, `0xa3470`) materializes the identity operand used as the **stationary** input for transpose-via-matmul. `shared_identity_matrix` (idx 284/285, `0x7b490`) is the **cached** 128×128 identity, built at most once and shared across transposes.

The cache mechanism is readable from the `shared_identity_matrix` body's callees: it calls `PyDict_GetItemWithError` (the lookup), and on a miss `PyDict_New`/`PyDict_SetItem` (the populate-and-store) — a memoizing dict keyed (by dtype) so the 128×128 identity is constructed once per dtype and reused. This is the Penguin-layer-1 analog of the BIR-layer per-dtype `getIdentityMatrix(bir::Dtype)` cache and `legalizeIdentityMatrices()`: the identity is created **here** at the Penguin level and re-materialized/cached again at BIR.

> **GOTCHA — the identity exists twice.** A reimplementer who caches the identity matrix only at the BIR layer will still see `NeuronCodegen` construct one at the Penguin layer (and vice-versa). The two caches are independent: the Penguin `shared_identity_matrix` dict feeds the *forward* transpose emit; the BIR `getIdentityMatrix` feeds the *back-half* codegen. Both are per-dtype, both 128×128, but they are not the same object and neither subsumes the other.

---

## 6. The Penguin MatMul Op nodes — attributes and the handoff

The per-Op keyword vocabularies, all `.rodata` literals, define the layer-1 output contract:

```text
  MatMulOp        : stationary, moving, outputs, perf_mode, tile_position,
                    tile_size, is_transpose, engine, name
  MatMulMXOp      : + stationary_scale, moving_scale     (− perf_mode/is_transpose use)
  MatMulSparseOp  : stationary, moving, outputs, tile_position(2D), tile_size(2D),
                    engine, name                          (− column tiling)
  TransposeOp     : input, outputs, transpose_type, transpose_dtype, engine, name
```

Contraction geometry rides on the `{stationary, moving}` access patterns plus the 2-element `{tile_position, tile_size}`. The accumulate group is **structural** (shared `outputs` PSUM + `tile_position == [0,0]` head + `combine_*_matmult_tiles`), with no explicit group-id field observed in any of the four bodies.

The field lineage is end-to-end name-preserving on **both** descents. On the **beta3** path the attribute rides straight from Penguin into BIR via `BirCodeGenLoop`'s `birpy` setters (no klr slot). On the **beta2** (klr) path it passes through a klr AST slot first. For one attribute (`tile_position`), the beta2/klr lineage is:

```text
  Penguin MatMulOp.tile_position
    → (beta2 klr AST)  klr::NcMatMul.tilePosition (slot 7)
    → (KlirToBirCodegen, libwalrus C++)  → bir::InstMatmult.tile_position[2]
  (beta3: BirCodeGenLoop.codegenMatMulOp sets tile_position on the birpy Instruction DIRECTLY)
```

and likewise (beta2) `is_transpose` → klr byte 50 → BIR `+0x1B8`, `perf_mode="double_row"` → klr perfMode → BIR `DoubleRow(1)`. `BirCodeGenLoop`'s `codegenMatMul*Op` bodies (the beta3 `birpy`-direct emit) are documented on [BirCodeGenLoop](bircodegenloop.md); the BIR-level `InstMatmult` encoding on the BIR instruction pages. This page's job ends at `self.add_named_instruction(inst)`.

---

## 7. Evidence summary

**Read directly** from the symbol table, the `.rodata` literals, and the DWARF line table of `KernelBuilder.so`:

- Class `NeuronCodegen` in `KernelBuilder.cpython-310…so` (unstripped, DWARF); module docstring verbatim; 194 mdef methods, indices to 379.
- Matmul-family method **indices, symbols, addresses, and body sizes**: `matmult` 101 @ `0x266520` (64,948 B), `matmult_sparse` 103 @ `0x1d3c20`, `matmult_transpose` 105 @ `0x127670`, `matmult_mx` 107 @ `0x279fe0` (106,470 B), `get_identity_tensor` 109 @ `0xa3470`, `transpose` 111 @ `0x2235f0`, `shared_identity_matrix` 285 @ `0x7b490`, `get_sb_and_psum_shape` 93 @ `0x6ec10`.
- Op class names `MatMulOp`/`MatMulMXOp`/`MatMulSparseOp`/`TransposeOp`; `add_named_instruction` as a name constant; the kwarg vocabularies; `perf_mode` enum {`double_row`,`double_row_gen3`,None}; transpose engine set {Tensor,Vector,Unknown}; `split_par_shape` closure symbol.
- Validation literals: `"Result buffer of matmult must be psum!"`, `"nc_matmul_mx does not support column PE tiling"`, the `matmult_sparse` 2D-tile / no-column literals, the `check_mx_scale` `[P/8,F/4]`/`i*32+j`/multiple-of-16 literals, the double_row fp8/uint8 geometry literals, the transpose permutation guards.
- `matmult` issues **14** `__Pyx_GetKwValue_FASTCALL` calls; `matmult_mx` issues 11.
- `shared_identity_matrix` cache mechanism: `PyDict_GetItemWithError` lookup + `PyDict_New`/`PyDict_SetItem` populate (the memoizing identity cache).
- `add_named_instruction` is **not** a `__pyx_pw`/`pf` symbol of this `.so` and **not** a `penguin.ir.IRBuilder` method (whose primitives are `insert`/`insert_inst`) — it is inherited via `GeneratedNeuronCodegen`.

**Reconstructed** from the string vocabulary, the DWARF line ranges, and the downstream contract, rather than byte-traced:

- The exact emit call shape (`MatMulOp(...); self.add_named_instruction(inst)`).
- The 14-kwarg name-to-position binding for `matmult`; the `get_sb_and_psum_shape` split.
- `matmult_transpose` = `MatMulOp` + `is_transpose` against `get_identity_tensor`.
- Accumulate group = structural (shared PSUM `outputs` + `tile_position[0,0]` head), no group-id field.

### Limits of this reading

The per-instruction *call order* inside each body is **INFERRED**. Cython routes every name load through the module-state struct (`mov OFF(%rbx),%rax`), which blocks a byte-traced opcode→Python listing; the emit order here comes from the string vocabulary, the DWARF line ranges, and the downstream klr/BIR contract instead. Recovering it properly needs a Cython module-state offset resolver.

Two other things remain open. The `split_par_shape` closure (the MX scale-partition split) is pinned as a symbol with a known role, but its body has not been traced. And the layer-2 `BirCodeGenLoop.codegenMatMul*Op` bodies are out of scope here — they belong to [BirCodeGenLoop](bircodegenloop.md).
