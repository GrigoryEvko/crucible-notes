# NeuronCodegen `builtin_custom_op` — the GPSIMD custom-op emitter

> *All symbols, offsets, and Python line numbers on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310). The forward emitter lives in `neuronxcc/nki/compiler/backends/neuron/KernelBuilder.cpython-310-x86_64-linux-gnu.so` (UNSTRIPPED, BuildID `9eb1020ebbb2a46b230a16ada272daa71f3001bf`, 14,588,400 bytes). Other wheels differ; treat every address as version-pinned.*

## Abstract

`builtin_custom_op` is the NKI front door for emitting a **GPSIMD custom op** — a call into an embedded CPU/GPSIMD `.so` (the bitonic SORT/TOPK kernels, among others) — into the Penguin IR graph. It is a **thin marshalling emitter**: it takes six structured arguments, reduces the NKI tile-view operand wrappers to their underlying IR tensors, packs everything into a `penguin.ir.CustomOp` node, and hands that node to `self.insert_raw`. It builds **no byte payload, no opcode, and no `klr::ExtendedInst`**. The op identity is carried verbatim by the `function_name` string; the embedded library is named by `lib_file_name`; two version handles (`ulib_to_ucode_version`, `ulib_to_isa_version`) gate ABI compatibility with the embedded binary.

The interesting design fact is the **division of labor**. Everything the wire needs — the dispatch handle (`CustomOpFunctionId`), the packed access patterns of every operand, the ≤1-output hardware rule — is computed *downstream* in the backend custom-op generator, not here. This emitter's job is only to get a well-formed `CustomOp` node into the IR with its operands bound and its identity strings attached. SORT and TOPK are not special cases in the emitter: there is **no `"sort"`/`"topk"` literal anywhere in its body**. They are ordinary callers that pass `function_name="sort"`/`"topk"` and express K, axis, and descending-ness through *which* builtin they name and through the *shapes and access patterns* of their `dsts`/`srcs` — not through any scalar attribute on this method.

This NKI path is **disjoint** from the HLO/XLA TopK path (`TopkRewriter` → `AwsNeuronTopK` custom-call, [4.26 topk-legalize](../hlo-opt/topk-legalize.md)). They both terminate in a BIR custom op, but they share no code and reach it through entirely separate front-ends. Do not conflate them.

| | |
|---|---|
| **Forward emitter** | `NeuronCodegen.builtin_custom_op` @ `0xb8890` (`KernelBuilder.py:3609–3619`) |
| **Public signature** | `builtin_custom_op(function_name, lib_file_name, ulib_to_ucode_version, ulib_to_isa_version, srcs, dsts, **kwargs)` — pinned by `neuronxcc-stubs/nki/isa/__init__.pyi` |
| **IR node built** | `penguin.ir.CustomOp` (module-global, `CustomOp.py:40`) — "Wrapper class for XLA Custom Call" |
| **Operand bind** | `srcs=[s.tensor for s in srcs]`, `dsts=[d.tensor for d in dsts]` — tile views reduced to IR tensors |
| **Insertion** | `self.insert_raw(op)` @ `0x88be0` → `update_debugloc` · `cur_scope.add_predicates` · `builder.insert` |
| **NKI handoff up** | `nki.isa.builtin_custom_op` (`neuron_isa.py:2798`) → `nki_ctx` → this method (identical sig) |
| **BIR handoff down** | `CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0` → 1×`0x85` header + (1+N)×`0x86` payload |
| **Builtin library** | `libbuiltincustomop_cpu*` (literal `__pyx_k_libbuiltincustomop_cpu` @ `CustomOp.so:0x15090`) |
| **Returns** | `None` — void emitter |

> **NOTE — evidence scope.** The public signature comes from the shipped `.pyi` stub, and the `CustomOp` field set is corroborated by the `SundaCustomOpGen` Cython string pool (cp310), which carries the identical attribute names: `function_name`, `lib_file_name`, `ulib_to_isa_version`, `ulib_to_ucode_version`, `is_builtin`, `srcs_shapes`, `dsts_shapes`, plus the AP decomposition `srcs_par_indices`/`srcs_free_indices`. The `0xb8890` body and its `.rodata` literals come from the unstripped `KernelBuilder.so`; the `0x85`/`0x86` wire offsets come from the `libwalrus.so` encoder body. §7 states what each rests on.

---

## 1. The emitter body

The method is ten Python lines (`KernelBuilder.py:3609–3619`). Reconstructed from the recovered Cython wrapper `__pyx_pw_..._13NeuronCodegen_249builtin_custom_op` @ `0xb8890`, the keyword arg-name array, and the six `PyDict_SetItem` keys resolved through the module-state field map:

```c
// NeuronCodegen.builtin_custom_op  —  KernelBuilder.py:3609, @0xb8890
// Cython method #249. Wrapper accepts FASTCALL-positional (nargs==7 fastpath)
// AND full keyword form; a trailing **kwargs is captured and dropped (see note below).
PyObject* builtin_custom_op(self,
        function_name,           // op-name: ABI entry point inside the embedded lib
        lib_file_name,           // path/name of the embedded custom-op .so
        ulib_to_ucode_version,   // ulib -> microcode version-compat handle
        ulib_to_isa_version,     // ulib -> ISA      version-compat handle
        srcs,                    // iterable of NKI input  tile-views (AP/tensor wrappers)
        dsts) {                  // iterable of NKI output tile-views
    // ---- operand bind: reduce tile-views to their underlying IR .tensor ----
    PyObject* src_tensors = [ s.tensor for s in srcs ];   // 3617, loop @0xb8ca8
    PyObject* dst_tensors = [ d.tensor for d in dsts ];   // 3618, loop @0xb8e46

    // ---- build the penguin.ir.CustomOp node (kwargs only; no byte payload) ----
    op = CustomOp(                                        // module-global, @0xb9019
        function_name         = function_name,           // dict key off 0x1ed8
        lib_file_name         = lib_file_name,            //          off 0x26d8
        ulib_to_ucode_version = ulib_to_ucode_version,    //          off 0x3ce8
        ulib_to_isa_version   = ulib_to_isa_version,      //          off 0x3ce0
        srcs                  = src_tensors,              //          off 0x3880
        dsts                  = dst_tensors);             //          off 0x1bd0

    self.insert_raw(op);                                  // @0xb905f → §2
    return Py_None;                                       // void emitter
}
```

Three things are *absent* and that absence is the point. There is **no opcode**, **no `backend_config` byte-blob**, and **no `klr::ExtendedInst`** — the string `"ExtendedInst"` does not appear anywhere in `KernelBuilder.so`. The Penguin-side carrier of all this information is the `CustomOp` node itself (§4); the ExtendedInst tag belongs to the parallel KLR-AST path, not this one.

> **NOTE — there is a trailing `**kwargs` after the six named parameters.** The shipped stub reads `def builtin_custom_op(function_name, lib_file_name, ulib_to_ucode_version, ulib_to_isa_version, srcs, dsts, **kwargs)`. Reading the arg-name array alone suggests a fixed six-parameter signature, because its 8th slot is NULL. Extra keywords are accepted and then silently dropped — none of them is threaded into `CustomOp` — so a reimplementation should accept and ignore unknown keywords here.

### 1a. Operand binding

The two list comprehensions are the marshalling core (`srcs` @ `0xb8ca8`, `dsts` @ `0xb8e46`). For each element the code does `getattr(element, "tensor")` (`__pyx_n_s_tensor`) and appends to a fresh `PyList`, with a fast inline walk when the argument is already a `list`/`tuple`. The semantic effect: the **NKI tile-view wrappers** (the AP-carrying `srcs`/`dsts` the kernel author passes) are unwrapped to the **underlying Penguin IR tensor values**, which become the op's operand (src) and result (dst) lists. The resulting lists are bound to the `CustomOp` keys `srcs` and `dsts` — **not** `srcs_ir`/`dsts_ir` (see the next note).

> **GOTCHA — `srcs_ir` / `dsts_ir` are a different emitter's keys.** The string pool also holds `__pyx_k_srcs_ir` and `__pyx_k_dsts_ir`. Those are the keys used by the **collective** sibling `insert_raw_cc` (the collective-channel emitter), not by `builtin_custom_op`. The module-state field-map offsets pin this method's six keys to `function_name`/`lib_file_name`/`ulib_to_isa_version`/`ulib_to_ucode_version`/`srcs`/`dsts` (offsets `0x1ed8`/`0x26d8`/`0x3ce0`/`0x3ce8`/`0x3880`/`0x1bd0`). The `is_builtin` key (off `0x2438`) is *also* not set here — it is derived inside `CustomOp.__init__`.

### 1b. What carries the op identity

There is no hard-coded op-name. The identity is the `function_name` **string**, threaded verbatim into `CustomOp(function_name=...)`. The surviving payload from this method is structured kwargs only:

| Field | Role |
|---|---|
| `function_name` | ABI entry-point name inside the embedded lib — *this is the op-name* (`"sort"`, `"topk"`, …) |
| `lib_file_name` | name of the embedded custom-op `.so` (`libbuiltincustomop_cpu*` for the builtin set) |
| `ulib_to_ucode_version` | ulib→microcode version-compat handle for the embedded binary |
| `ulib_to_isa_version` | ulib→ISA version-compat handle |
| `srcs` / `dsts` | the operand & result IR-tensor lists |

> **GOTCHA — no payload bytes are built here.** `NeuronCodegen` assembles no `backend_config` and no opaque byte-blob; it passes structured Python kwargs and nothing else. The byte-packing, the `CustomOpFunctionId` allocation, and the per-line library-spec parsing all happen in the *backend* encoder (§5).

---

## 2. The `insert_raw` handoff

`insert_raw(self, inst)` @ `0x88be0` (`KernelBuilder.py:4752`) is the standard IRBuilder insertion spine, identical to the pattern used by every other forward emitter on this class:

```c
// NeuronCodegen.insert_raw — KernelBuilder.py:4752, @0x88be0
void insert_raw(self, inst) {
    self.update_debugloc(inst);              // stamp current source location
    self.cur_scope.add_predicates(inst, …);  // predicate into the active scope
    self.builder.insert(inst);               // append into the builder's current block
}
```

After this returns, the `CustomOp` node lives in the Penguin IR graph as an `InstCustomOp` (Penguin instruction type `IT53`), carrying the current debug location and any active predicates. The collective sibling `insert_raw_cc` @ `0xff350` (line 4753) is the channel-aware variant and is **not** on this path.

---

## 3. Handoff up — what calls this emitter

The string `builtin_custom_op` appears in exactly three cp310 modules: this `KernelBuilder.so` (the emitter), `nki/isa/neuron_isa.so` (the ISA wrapper), and `nki/isa/__init__.so` (a re-export). The ISA wrapper is the public entry:

```c
// nki.isa.neuron_isa.builtin_custom_op — neuron_isa.py:2798, @0x3d2a0
PyObject* builtin_custom_op(function_name, lib_file_name,
        ulib_to_ucode_version, ulib_to_isa_version, srcs, dsts) {
    ctx = <module-global nki_ctx>;                  // the live trace/codegen context
    fn  = getattr(ctx, "builtin_custom_op");        // → the active NeuronCodegen
    return fn(function_name, lib_file_name,         // forward all six, unchanged
              ulib_to_ucode_version, ulib_to_isa_version, srcs, dsts);
}
```

The ISA layer hard-codes **nothing** about the op — it is a pure forwarder through the live `nki_ctx`. The full upward chain:

```text
<NKI kernel: chooses function_name="sort"/"topk", lib path, builds srcs/dsts tiles>
  → nki.isa.builtin_custom_op(...)            # neuron_isa.py:2798
    → nki_ctx.builtin_custom_op(...)          # nki_ctx == the live NeuronCodegen
      → NeuronCodegen.builtin_custom_op(...)  # KernelBuilder.py:3609   (§1)
        → CustomOp(**6 kwargs); self.insert_raw(op)                     (§2)
```

---

## 4. The `penguin.ir.CustomOp` container

`CustomOp` is a module-global of `KernelBuilder.py`, imported from `neuronxcc.starfish.penguin.ir`; the class itself ships as `…/penguin/ir/CustomOp.cpython-310-…so` with docstring **"Wrapper class for XLA Custom Call"** (`@0x15040`). Its `__init__` (`CustomOp.py:40`) consumes the six kwargs 1:1 and *derives* the rest:

| Stored field | Source |
|---|---|
| `function_name`, `lib_file_name`, `ulib_to_ucode_version`, `ulib_to_isa_version` | the four scalar kwargs, verbatim |
| `srcs`, `dsts` | the two IR-tensor lists (each wrapped to a singleton via `_wrap_single_elt` if a bare tensor) |
| `srcs_shapes`, `dsts_shapes` | computed `getattr(t, "shape")` per tensor — **this is where #outputs / K / out-length land** |
| `is_builtin`, `target_name` | builtin/dispatch flags derived in `__init__` |

`CustomOp.serialize` (`CustomOp.py:76`) BIR-serializes exactly `function_name`, `lib_file_name`, `ulib_to_isa_version`, `ulib_to_ucode_version`, `srcs_shapes`, `dsts_shapes`, `is_builtin` (string-escaped via `quote`, written through `ctx`); `rhs_str` renders the same set plus `args_str` and `id`. That tuple — **op-name + lib + versions + shapes + builtin-flag** — *is* the surviving Penguin-level payload.

> **NOTE — builtin library literal.** `__pyx_k_libbuiltincustomop_cpu` @ `CustomOp.so:0x15090` = `"libbuiltincustomop_cpu"`. The builtin set (SORT/TOPK and friends) ships in `libbuiltincustomop_cpu*`. The `SundaCustomOpGen` string pool corroborates the *whole* field set at backend codegen time — it carries `function_name`, `lib_file_name`, `ulib_to_isa_version`, `ulib_to_ucode_version`, `is_builtin`, `srcs_shapes`, `dsts_shapes`, and the access-pattern decomposition `srcs_par_indices` / `srcs_free_indices` (the partition vs free axes of each source AP). The field set is pinned by the `SundaCustomOpGen` strings; the `libbuiltincustomop_cpu` literal comes from `CustomOp.so`.

This `CustomOp` is the Penguin-level equivalent of the KLR `ExtendedInst:210,0` container: the *same* information (`function_name` + lib + operands) rides the KLR path on an `InstNKIKLIRKernel` carrier, reconciled to this `CustomOp` on the Penguin path. The equivalence is [INFERRED] from both carriers holding the identical field tuple.

---

## 5. Handoff down — `InstCustomOp` → BIR `0x85`/`0x86`

The Penguin `InstCustomOp` (`IT53`) is lowered to the BIR custom-op wire by **`CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0`** (in `libwalrus.so`; the largest long-tail encoder). This is where `KernelBuilder`'s structured kwargs finally become bytes. The byte stamps below are read off that encoder body.

**Validation prologue** (`CoreV2GenImpl.cpp` line strings):

| Line | Check |
|---|---|
| 4195 | `"Custom ops cannot have more than 1 output"` — the ≤1-output rule |
| 4200 | `"All args to a customop must be located in SBUF or HBM"` |
| 4206 | `"All of a customop's outputs must be located in SBUF or HBM"` |
| 4252 | `"Number of unique custom op functions cannot exceed "` — cap `0xFE` = 254 |
| 4275 | `"…CUSTOM_OP instruction cannot have TensorIndirect AP"` |

**ABI / embedded-binary registration** — the landing site for `lib_file_name`/`function_name`:

```c
// CoreV2GenImpl::visitInstCustomOp  —  libwalrus.so @0x12613c0
getModule(I);                                   // mark module as carrying a custom op
foreach line in getline(lib_spec):              // multi-line lib spec split by std::getline
    ModuleArtifactInfo::addCustomOpLibFile(libpath, fn_name, flags);  // CustomOpLibInfo map @Module+376
    ModuleArtifactInfo::addCustomOpFunction(...);                     // allocates CustomOpFunctionId
// CustomOpFunctionId : uint8, default 0xFF — the runtime ABI dispatch handle into the embedded .so.
// fn-id counter @Module+456 ; allocator @Module+216.
```

`function_name` + `lib_file_name` are exactly what `addCustomOpLibFile`/`addCustomOpFunction` consume, yielding the one-byte `CustomOpFunctionId` that the wire uses to dispatch.

**Wire bundles** — each `fwrite` is a `0x40` = 64-byte BIR instruction:

```text
HEADER bundle   opcode 0x85 (= -123)  CUSTOM_OP_HEADER
  bundle[0]  = 0x85 (setupHeader)        bundle[12..13] = num_payloads (u16 @ +0x0C)
  bundle[14] = CustomOpFunctionId        bundle[15]     = num_arguments
  bundle[16] = 0 (reserved)              (sub_122ED00 packs PC/branch-hint tail)

OUTPUT bundle   opcode 0x86 (= -122)  CUSTOM_OP_PAYLOAD  (dst)
  bundle[0]  = 0x86   bundle[15] = 1 ("is output")
  &bundle[16] ← packed access pattern of the dst tensor   (sub_1210900)

PER-ARG bundle  opcode 0x86  CUSTOM_OP_PAYLOAD  (loop k over num_arguments)
  bundle[0]  = 0x86   bundle[15] = 1   if (k==last) mark-last (sub_122EA40)
  &bundle[16] ← packed access pattern of src arg k          (sub_1210900)
```

> **GOTCHA — `num_payloads` sits at header byte `+0x0C`, and the decompiler will tell you `+0x06`.** Hex-Rays renders the store as `sub_…((_WORD*)hdr + 6, "instr.num_payloads", …)`; because the pointer is a `u16*`, `+6` means `6 × 2 = +0x0C` bytes. Reading that `6` as a byte offset is the standard trap here. The disassembly is unambiguous: `0x1262f75: 49 8d 7d 0c  lea rdi,[r13+0Ch]` followed immediately by `lea rsi, "instr.num_payloads"` puts `num_payloads` (u16) at `+0x0C`; `0x1262f37: 41 88 45 0e  mov [r13+0Eh],al` puts `CustomOpFunctionId` at `+0x0E` (= `bundle[14]`); `0x1262fbe: 49 8d 7d 0f  lea rdi,[r13+0Fh]` with `"instr.num_arguments"` puts `num_arguments` at `+0x0F` (= `bundle[15]`). The three count/id fields form a contiguous band at `+0x0C..+0x10`. See [11.x custom-op wire-layout](../custom-ops/customop-wire-layout.md) §2 for the full byte map.

So one custom op emits **1×`0x85` header + 1×`0x86` output + N×`0x86` per src argument**. Field provenance to the wire:

| Penguin field | → wire |
|---|---|
| `function_name` + `lib_file_name` | → `addCustomOpLibFile`/`Function` → `CustomOpFunctionId` → header `bundle[14]` |
| `srcs` (operand tensors) | → per-arg `0x86` PAYLOAD bundles (packed AP) |
| `dsts` (result tensor) | → the `0x86` OUTPUT bundle (packed AP) |
| `ulib_to_isa/ucode_version` | → `ModuleArtifactInfo` lib registration (version-compat metadata) |

> **NOTE — `InstBIRKernel` / `InstNKIKernel` reuse this path.** Neither `IT54` (`InstBIRKernel`) nor `IT55` (`InstNKIKernel`) has its own `CoreV*Gen` emitter. They are **lowered to `InstCustomOp` before backend codegen** and emitted through this same `0x85`/`0x86` path. (See [2.22 collective-customop-encoding](../isa/collective-customop-encoding.md) for the encoding family.)

---

## 6. SORT / TOPK routing — and the disjoint HLO path

Two distinct routes can reach a custom op for top-k/sort. **They share no code.** Getting them confused is the easiest mistake on this topic.

**(A) HLO / XLA path — *not* this method.** XLA's `TopkRewriter::SortIsInTopK` recognizes the sort+slice idiom in the framework graph and rewrites it: `TransformPatternToCustomCall` → `CreateTopKCustomCall` → a `"TopK"` / `AwsNeuronTopK` custom-call; then `legalize-topk` (pass order 8) maps `AwsNeuronTopK` → `TopK_f32/f16/bf16`. The `AwsNeuronTopK` opaque has *no* separate dim/sorted field — the axis is the implicit minor dim and "sorted" is implicit. This is the framework-graph route; it **never** passes through `NeuronCodegen.builtin_custom_op`. (Full treatment: [4.26 topk-legalize](../hlo-opt/topk-legalize.md).)

**(B) NKI path — *this* method.** A NKI kernel (a user kernel, or one of the `_private_kernels/topk` library kernels — `cascaded_2_stage_topk`, `naive_scanning_topk`, `rotational_topk`, dispatched by `topk_method_mapping`) invokes the SORT/TOPK builtin directly:

```python
nki.isa.builtin_custom_op(
    function_name = "sort" | "topk" | …,       # selects the builtin entry point
    lib_file_name = <libbuiltincustomop_cpu*>, # the embedded builtin .so
    ulib_to_ucode_version = …, ulib_to_isa_version = …,
    srcs = [input_tensor(s)],
    dsts = [out_values, out_indices?])          # → §3 → §1 → CustomOp → §5 wire
```

The crucial observation: **K, axis, descending-ness, and index-output are NOT parameters of `builtin_custom_op`.** There is no `k=`, no `axis=`, no `descending=`. The caller encodes them structurally:

| Semantic | How it is encoded (NKI path) |
|---|---|
| **K** and **descending/sorted** | baked into *which* `function_name` is chosen, and into the **`dst` tensor shapes** (out length = K); captured as `dsts_shapes` in `CustomOp.__init__` (§4). The bitonic-sort lib implements the comparator/K internally. |
| **axis** | expressed by the **layout / access pattern** of the src/dst tensors — i.e. the AP packed into the `0x86` payload bundles (§5) — not as a scalar attribute. |
| **index output** (argsort indices) | an **additional `dst` tensor** in `dsts` (TOPK returns values *and* indices → `dsts` has 2 entries). |

> **GOTCHA — the ≤1-output rule vs TOPK's two outputs.** The CoreV2 validator forbids more than one output per custom op (`l.4195`). A TOPK that returns both values *and* indices cannot emit two `0x86` OUTPUT bundles. It is therefore realized either as a builtin whose single `CUSTOM_OP` output is a *packed values+indices buffer*, or split upstream into separate ops. The exact values/indices packing lives inside `libbuiltincustomop`, whose internal ABI is not in this binary — which of the two realizations applies is [SPECULATIVE].

---

## 7. What pins the five strongest claims

1. **The six-parameter signature (plus `**kwargs`).** The shipped type stub `neuronxcc-stubs/nki/isa/__init__.pyi` gives `builtin_custom_op(function_name, lib_file_name, ulib_to_ucode_version, ulib_to_isa_version, srcs, dsts, **kwargs)` — an exact match on the six names and their order, and the source of the `**kwargs` tail noted in §1.
2. **The `CustomOp` field set and its AP decomposition.** The `SundaCustomOpGen` (cp310) Cython string pool carries `function_name`, `lib_file_name`, `ulib_to_isa_version`, `ulib_to_ucode_version`, `is_builtin`, `srcs_shapes`, `dsts_shapes`, `srcs_par_indices`, `srcs_free_indices`, plus the methods `operands`/`loadTensor`/`serialize`/`verify`/`ap_indices` — independently corroborating the §4/§5 field tuple and the access-pattern packing.
3. **SORT/TOPK are not hard-coded in the emitter; K and axis are encoded structurally.** No `k`/`axis`/`descending` parameter exists in the stub signature, and the only shape-bearing fields on `CustomOp` are `*_shapes`/`*_indices`. The NKI topk library kernels (`_private_kernels/topk/*`) are the §6(B) callers, though their per-kernel call sites live in `.pyc`, not in the compiled `.so` string pool.
4. **The `0x85`/`0x86` two-opcode wire (1 header + 1 output + N args).** `CoreV2GenImpl::visitInstCustomOp` @ `0x12613c0` in `libwalrus.so` supplies the count-band stamps byte-for-byte: `num_payloads` u16 @ `+0x0C` via `lea [r13+0Ch]` + `"instr.num_payloads"`, `CustomOpFunctionId` @ `+0x0E`, `num_arguments` @ `+0x0F`. The cap-`0xFE` unique-function rule and the SBUF/HBM-only operand rule are both consistent with `CustomOpFunctionId` being a `uint8` handle.
5. **No `ExtendedInst` and no byte payload in `KernelBuilder.so`.** This is a negative, established by grep over the whole unstripped binary, and it is consistent with the Penguin `CustomOp` node being the sole carrier of the op's identity.

The unstripped `KernelBuilder.cpython-310…so` (14,588,400 bytes, BuildID `9eb1020e…`) makes the `0xb8890` wrapper, its `0x1b49`-byte body size, and the `.rodata` literals directly checkable with `nm`/`rg`; `libwalrus.so` supplies the encoder side. Everything above rests on those two binaries plus the `.pyi` stub and the `SundaCustomOpGen` field strings.
