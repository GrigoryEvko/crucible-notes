# MhloToPythonPrinter — Penguin Emission Driver & Elementwise Emitters

> *All addresses on this page apply to neuronx-cc `2.24.5133.0+58f8de22` (the `hlo2penguin` binary, cp310 build). `.rodata = VA − 0x200000`, `.text = VA − 0x201000` — `VA == file-offset` is **false** for this binary. Other versions will differ.*

## Abstract

This is the last MLIR pass in the hlo2penguin pipeline, and it does something no other pass in the compiler does: it **does not lower MLIR to another MLIR dialect — it prints Python source code.** The pass `mlir::hlo::MhloToPyPenguin` (registered as `--mhlo-to-py-penguin`) walks the `mhlo`/`func` module and emits a textual `.py` program that, when executed, reconstructs the graph as live `neuronxcc.starfish.penguin` IR objects. There is no `penguin` MLIR dialect; there are no `penguin.*` op mnemonics anywhere in the emitter. The output is a string of `import` statements, a `Function(...)` constructor call, one `Tensor(...)` per parameter, and one `<module>.NeuronTensorOp(...)` (or specialized constructor) per op — assembled with `raw_ostream <<` and written to a file or `llvm::outs()`. This page proves that finding against the binary and reconstructs the driver and the elementwise-op emitters that funnel through it.

The shape is unusual but the rationale is clean. Penguin (the "Starfish" middle-end) is a Python library; `hlo2penguin` is a *front-end transpiler* whose deliverable is the Python program that drives that library. So the terminal pass is a code generator targeting Python text, not a dialect conversion. Every reverse-engineering instinct that expects a `ConversionPattern` rewriting `mhlo.add` into `penguin.add` is wrong here: the pass emits the literal string `m0.NeuronTensorOp(..., np.add, 'mhlo.binary')\n`. Anchors throughout confirm the format strings, the import set, and the op→funcRef table at the byte level.

This is the MLIR→Penguin-Python bridge. Everything Part 5 documents — the `Function`, `Tensor`, `NeuronTensorOp`, `DependencyEdge` objects of the Penguin IR model — is *constructed by the Python text this pass emits*. The driver tier (module skeleton, per-op dispatch, type/tensor/constant serialization, name/scalar/weight helpers) is reconstructed in §1–§5; the elementwise emitter family (unary/binary/ternary/convert/iota and the cbrt/clamp decomposers) is in §6–§8. The ~40 heavyweight per-op emitters (`printDotOp`, `printCollectiveOp<…>`, fusion emitters) share this spine, but their bodies belong to the Dot/Reduce/collective emitter pages.

For reimplementation, the contract is:

- **The output is Python text, not IR.** Reproduce the emitted program shape: import header, `Function()`, parameter `Tensor()`s, per-op `NeuronTensorOp()` statements, trailing `.add_dep_edge()` lines.
- **The per-op statement assembler** (`printOperandsAndAttributes`) — how one op becomes one Python line: LHS names, `srcs=[…]`, `dsts=[…]`, `op=`, frontend attrs, `dl=DebugLocation(…)`.
- **The four name families** the driver mints (`m<N>`, `input_<i>`, `v<N>`, `<base>.<k>`) and the fixed `weight_load` helper.
- **The elementwise op→Python-funcRef table** — `mhlo.exp`→`np.exp`, `mhlo.divide`→`np.divide`/`np.int_div`, `mhlo.compare{GT}`→`np.greater`, plus the rewrites (`sigmoid`→`scipy.special.expit`, `cbrt`/`clamp` decompose).

| | |
|---|---|
| **Pass** | `mlir::hlo::MhloToPyPenguin` — arg `--mhlo-to-py-penguin`, `OperationPass<ModuleOp>` |
| **Pass entry** | `runOnOperation` @ `0x20a9870` (1078 bytes) |
| **Printer class** | `mlir::MhloToPythonPrinter` (StableHLO twin: `mlir::StableHLOToPythonPrinter`) |
| **Driver entry** | `start` @ `0x20f5560` → `printStart` @ `0x20e6b10` (4533 bytes) |
| **Per-op dispatch** | `printOperation` @ `0x20ee320` (5280 bytes, flat TypeID cascade) |
| **Statement assembler** | `printOperandsAndAttributes` @ `0x20c3e30` (3185 bytes) |
| **Output sink** | `raw_ostream*` at printer `this+0x138` (file or `llvm::outs()`) |
| **Target library** | `neuronxcc.starfish.penguin` (str @ `0x26a78d`) — a **Python** package |
| **IR level** | `mhlo`/`func` dialect in → Python source text out |
| **Source anchor** | `hilo/MLIRPasses/Transforms/MhloToPythonPrinter.cc` @ `0x3cd468` |

---

## 1. The Headline — Penguin IR is Emitted as Python Text

The single most important fact about this pass: **the lowering writes Python source, not an MLIR dialect.** This is provable from the binary three ways.

**1. The emitted format strings are Python.** The `.rodata` string pool of `hlo2penguin` contains the verbatim Python fragments the emitter concatenates. Each is a verbatim entry in the string pool:

```text
"import "                 @ 0x24a562   →  import <module> as <alias>
" as "                    @ 0x27ae30
".Function("              @ ...         →  v0 = m0.Function(id_=0, batch_ids=[], attrs=(...))
".markInput("                            →  input_0 = m5.Tensor(...).markInput(v0)
".Tensor("                               →  <name> = m5.Tensor(...)
".SingleValueTensor("                    →  scalar/constant tensor ctor
".NeuronTensorOp("                       →  <dst> = m1.NeuronTensorOp(...)
".add_dep_edge("                         →  <dst>.add_dep_edge(mK.DependencyEdge(...))
".DependencyEdge(src="                   →  control-edge ctor
", dst="
"def weight_load(p):\n"   →  the weight-loader function, emitted verbatim:
"  t = "                       def weight_load(p):
".load(p)\n"                     t = np.load(p)
"  return t\n"                   return t
"'mhlo.unary'" / "'mhlo.binary'" / "'mhlo.ternary'" / "xla_op"  ← op-class tags
```

There is no `penguin.add`, no `penguin.tensor`, no dialect mnemonic anywhere. There **is** a literal Python function definition (`def weight_load(p):\n`) and a literal `import` keyword. An MLIR dialect lowering emits none of these.

**2. The target is a Python package path, not a dialect namespace.** The base module string `neuronxcc.starfish.penguin` (@ `0x26a78d`), the control-dep module `neuronxcc.starfish.penguin.ir.Dependency` (@ `0x2c9158`, len 40), and `neuronxcc.starfish.support` (@ `0x27ef66`) are all dotted **Python import paths**. They are interned into a `getImport` `StringMap` and emitted as `import <path> as m<N>`. A dialect would register a namespace in an `MLIRContext`, not emit an import statement.

**3. The function references are numpy/scipy symbols.** Elementwise ops do not map to `penguin.*` ops; they map to `np.exp`, `np.add`, `scipy.special.expit`. The emitter calls `getImport("numpy")` → alias `"np"`, then builds the funcRef string `"np" + "." + "exp"`. The Penguin op is `NeuronTensorOp`, and the actual elementwise *operation* is carried as a numpy ufunc reference passed as an argument:

```python
# what mhlo.add becomes, verbatim shape:
out = m1.NeuronTensorOp(lhs, rhs, np.add, 'mhlo.binary')
```

> **QUIRK —** the operation is not encoded in the constructor *name*. Every elementwise op emits the same constructor, `NeuronTensorOp`; the *which-op* is the numpy/scipy callable passed as a positional argument (`np.add`, `np.exp`, `scipy.special.erf`) together with an op-class tag string (`'mhlo.binary'`). A reimplementer who expects a distinct `penguin.AddOp` per opcode will not find one — the Penguin layer dispatches at runtime on the passed ufunc. This is why the elementwise table (§6) is a *string-rewrite* table from mhlo op → numpy symbol name, not an op→op table.

> **NOTE — one statement, two vantage points.** You will see the emitted line written two ways: as the generic `m<N>.<OpName>(srcs=[…], dsts=[…], op="<n>", dl=…)` skeleton and as the elementwise `NeuronTensorOp(<src>, np.<func>, 'mhlo.<class>')`. These are the same statement. `printOperandsAndAttributes` formats the `srcs=` / `dsts=` / `op=` / `dl=` frame; the leaf emitter (`printUnaryTensorOp` and its siblings) supplies the constructor name — which for every elementwise op *is* `NeuronTensorOp` — plus the ufunc and the class tag.

---

## 2. Module Skeleton — `start` → `printStart`

### Purpose

`printStart` writes the module header — imports, the `Function()` object, the `weight_load` helper, and one `Tensor()` per entry parameter — then walks the body and emits one statement per op. Everything downstream writes through the single `raw_ostream*` it selects.

### Entry Point

```text
runOnOperation (0x20a9870)                      ── pass entry (OperationPass<ModuleOp>)
  ├─ hilo::getMainFunction(ModuleOp&)           ── locate the entry FuncOp
  ├─ hilo::getInputNames / getOutputNames       ── I/O tensor name lists
  ├─ hilo::getTransposableIdxs(FuncOp&)         ── transposable arg indices
  ├─ MhloToPythonPrinter::ctor (0x20a9f40)      ── (filename, names, PassConfig&, bool)
  └─ MhloToPythonPrinter::start (0x20f5560)     ── open ostream → printStart
       └─ printStart (0x20e6b10)                ── header + body walk
```

`start` stores the bool emit-flag at `this+0x60`, then selects the sink: if the filename length at `this+0x120` is 0, the sink is `llvm::outs()`; otherwise it opens an `llvm::raw_fd_ostream` over the filename StringRef at `this+0x118`. The chosen `raw_ostream*` lands at `this+0x138` and is the destination for *all* subsequent emission (every helper writes through `[rbx+138h]`).

### Algorithm

```c
function printStart(FuncOp main):                 // 0x20e6b10
    // ── Phase 1: imports (each via getImport, alias cached in a printer field) ──
    f140 = getImport(tensorizerIR)                // "neuronxcc.starfish.penguin.ir.ir"
    getImport(tensorizerIRDebugInfo)              // "...penguin.ir.DebugInfo"
    getImport(tensorizerAPIndex)                  // "...targets.tonga.APIndex"
    getImport(tensorizerTargetIndependentIR)      // "...targets.tonga.TongaInst"
    getImport(tensorizerTargetDependentIR)        // "...targets.tonga.TongaISAInst"
    getImport(tensorizerTensor)                   // "...targets.tonga.TongaTensor"
    getImport("numpy")                            // → "import numpy as np"
    if main.attr("mhlo.frontend_attributes") is DictionaryAttr with "has_control_deps":
        f180 = getImport(tensorizerDependencyGraph)   // "neuronxcc.starfish.penguin.ir.Dependency"

    // ── Phase 2: the Function() object ──
    fnvar = defScalar()                           // a fresh "v<N>", stored at this+0x160
    emit fnvar, " = ", f_alias, ".Function(id_=0, batch_ids=[], attrs=("
    switch this+0x28:                             // precomputed model-type tag
        case 1: emit "\"model-type=compute-bound\","
        case 2: emit "\"model-type=memory-bound\","
    emit "\"mac-count=", dec(this+0x30), "\","
    if this+0x40 != 0:                            // hlo-metrics present
        emit "'hlo-metrics=", bytes(this+0x38 .. this+0x40), "'"
    emit "))\n"

    // ── Phase 3: entry-parameter Tensor declarations ──
    slowSet = parse_dense_int_indices(main.attr("slowChangingIns"))   // str @ 0x236853
    for i, argType in enumerate(main.getFunctionType().getInputs()):
        name = "input_" + to_string(i)            // "input_" @ 0x20e707e
        printTensor(argType, /*DenseElementsAttr=*/none, name, ...,
                    NeuronTensorAttribute{ is_parameter = (i in slowSet) }, ...)
        emit ".markInput(", fnvar, ")"            // ".markInput(" @ 0x211b67
        if i in input_index_table:
            emit ", is_parameter_tensor=True"     // str @ 0x222afd
    // (the "weight_files" path @ 0x20e7618 registers per-input npy paths instead,
    //  keyed by tensor name into the import StringMap at this+0x220)

    // ── Phase 4: walk and emit body ──
    walk<GetGlobalOp>(main, lambda#1)             // pre-pass for memref.GetGlobalOp globals
    walk(main, printChildren_cb)                  // printChildren INLINED; cb → printOperation
    emit fnvar, ".id=", dec(this+0xF0), "\n"
    emit "ir=", fnvar, "\n"
```

The emitted header therefore looks like:

```python
import numpy as np
import neuronxcc.starfish.penguin.ir.ir            as m0
import neuronxcc.starfish.penguin.ir.DebugInfo     as m1
import neuronxcc.starfish.penguin.targets.tonga.APIndex      as m2
import neuronxcc.starfish.penguin.targets.tonga.TongaInst    as m3
import neuronxcc.starfish.penguin.targets.tonga.TongaISAInst as m4
import neuronxcc.starfish.penguin.targets.tonga.TongaTensor  as m5
# (if has_control_deps): import neuronxcc.starfish.penguin.ir.Dependency as m6

def weight_load(p):
  t = np.load(p)
  return t

v0 = m0.Function(id_=0, batch_ids=[], attrs=("model-type=memory-bound","mac-count=12345",))
input_0 = m5.Tensor(name="input_0", shape=(4,128), parent=..., id=..., dtype="float32",
                    layout="NC", attrs={}).markInput(v0)
# ... one statement per op ...
v0.id=0
ir=v0
```

> **NOTE —** the `tensorizer*` module-name strings are not literals — they are built at static-init (`GLOBAL static_init` @ `0x20b74d0`) by `llvm::Twine::concat` chains over the shared prefix `neuronxcc.starfish.penguin`. Only the prefix and leaf names (`ir`, `DebugInfo`, `APIndex`, `TongaInst`, `TongaISAInst`, `TongaTensor`, `Dependency`) appear as discrete strings; the full paths are assembled at process start. The `.ir.Dependency` path can be read back whole from the `.rodata` StringRef global @ `0x41fa70` (→ ptr `0x2c9158`, len 40).

---

## 3. Per-Op Dispatch — `printOperation`

### Purpose

`printOperation` (5280 bytes) is the switchboard: it reads the op's TypeID and routes to exactly one emitter. It is the per-op fan-out of the body walk.

### Algorithm

```c
function printOperation(Operation* op):           // 0x20ee320
    tid = op->getName().getImpl()[+0x10]          // TypeIDResolver<mhlo::XOp>::id
    // flat cascade: cmp tid against &TypeIDResolver<mhlo::XOp>::id for each op

    // ELEMENTWISE UNARY  → printUnary(op, "<label>") (router @ 0x20f0840)
    if tid == ExpOp:   printUnary(op, "exp")              // @ 0x27ef62
    if tid == RsqrtOp: printUnary(op, "rsqrt")            // ... (full table §6)
    ...
    // ELEMENTWISE BINARY → printBinaryTensorOp(op, "<label>")
    if tid == AddOp:   printBinaryTensorOp(op, "add")
    if tid == DivOp:                                       // dtype-selected
        label = "int_div"
        if !getElementType(op.getResult()).isInteger(): label = "divide"
        printBinaryTensorOp(op, label)                    // @ 0x20ef50f
    if tid == CompareOp:
        printBinaryTensorOp(op, mapXla2PgDir[op.getComparisonDirection()])
    ...
    // TERNARY / CONVERT / IOTA / CONSTANT
    if tid == SelectOp:   printTernaryTensorOp(op, "select")   // @ 0x20eea36
    if tid == ClampOp:    print<ClampOp>(op)                   // DECOMPOSE, §8
    if tid == ConvertOp:  printConvert(op)                     // @ 0x20ef455
    if tid == IotaOp:     printIota(op)                        // @ 0x20ef176
    if tid == ConstantOp: printConstant(op)
    ... // ~40 templated print<X> emitters (Dot, Conv, Reduce, collectives …)

    // FALLTHROUGH — unsupported op
    NEURON_LOG(ERROR, "hilo/MLIRPasses/Transforms/MhloToPythonPrinter.cc", line)
    // codes by sub-chain exit: NCC_PYP054/055/056/057
```

The dispatch is a flat TypeID-comparison cascade (279 basic blocks, 60 callees, 898 string refs). IDA names every comparand verbatim — `TypeIDResolver<mlir::mhlo::ExpOp,void>::id`, etc. — so the cascade *is* the authoritative op→method map. The full enumerated mhlo op set is confirmed by the dialect registration symbol `mlir::Dialect::addOperations<…>` (read from the symbol table: `AbsOp, AddOp, …, SineOp, …` — ~140 mhlo ops registered).

### The four name families

The driver mints exactly four kinds of Python identifier:

| Family | Form | Minter | Counter source |
|---|---|---|---|
| Module aliases | `m<N>` | `getImport` @ `0x20b48a0` | StringMap entry count `this+0x1AC` |
| Entry inputs | `input_<i>` | `printStart` | function-arg index `i` |
| Result tensors | `<base>` / `<base>.<k>` | `getUniqueName` @ `0x20ae810` | static `perOpCounter` StringMap @ `0x9c6f1e0` |
| Scalars | `v<N>` | `defScalar()` @ `0x20ae630` | `this+0xF4` (post-incremented) |

Plus the fixed `weight_load` helper (emitted once by `getWeightLoad` @ `0x20b4ea0`). `getUniqueName` takes a base name from the op's `neuron.symName` attribute (StringAttr) or the substring of the op location after the last `.`, then disambiguates via the process-global `perOpCounter`.

> **GOTCHA —** `perOpCounter` @ `0x9c6f1e0` is a **static** `StringMap<int>` shared across all printer instances. Name counters persist across functions and modules within one process invocation. This is benign for the single-module compile path the binary actually runs, but a reimplementer who instantiates the printer twice in one process and expects reset counters will get `<base>.1`, `<base>.2`, … carried over. Make the counter an instance member if you need per-module determinism.

---

## 4. The Statement Assembler — `printOperandsAndAttributes`

### Purpose

This is the spine every per-op emitter calls to materialize *one* Python statement. It builds the line in a `raw_string_ostream` over an internal buffer and **returns** the assembled `std::string`; the caller writes it to the sink. Its signature carries the full per-op metadata: `(Operation*, StringRef funcRef, optional<string> opName, optional<bitset<20>> skipMask, ArrayRef<pair<string,string>> attrs, StringRef classMarker)`.

### Algorithm

```c
function printOperandsAndAttributes(op, funcRef, opName, skipMask, attrs, marker):  // 0x20c3e30
    lhs   = getOutputTensors(op)                  // LHS name vector (§5)
    alias = getImport(tensorizerXLAFEImport)      // "neuronxcc.starfish.penguin.frontends.XlaFE"
    emit lhs_joined, " = ", alias, ".", funcRef, "("   // <dst> = m<N>.NeuronTensorOp(
    // skip control-dep operands: result-users where hilo::isControlDep
    emit printSrcs(op, skipMask)                  // srcs=[<s0>, <s1>, …]
    emit ", ", printDsts(op, lhs)                 // dsts=[<d0>, …]
    if opName.has_value():
        emit ", ", printOpName(opName)            // op="<n>"   ("op=" @ 0x23a6ed)
    if op[+0x2F] (has-attrs) and getInherentAttr("mhlo.frontend_attributes") is DictionaryAttr:
        emit ", ", printAttributes(pairs, sep="=", "")   // k=v, k=v (StringAttr values only)
    emit ", ", printMeta(op)                      // dl=…DebugLocation(…)   (§5)
    emit ")\n"
    emit printControlDeps(op, lhs[0])             // trailing .add_dep_edge(…) lines (§5)
    return buffer
```

So the canonical non-control statement is:

```python
dst0 = m1.NeuronTensorOp(srcs=[s0, s1], dsts=[d0], op="add",
    dl=m1.DebugLocation(tensor_op_name="…", file="…", line=L, column=0, hlo_id=H, parent=…))
```

`printOpName` (@ `0x20b2fc0`) emits `op="<name>"` when the optional is present and nothing when it is `nullopt`. `printAttributes` (@ `0x20b4510`) joins `{key,val}` pairs with `", "`, skipping empty values; with a non-empty prefix it wraps the result as a dict literal `<prefix> = {…}`. Non-`StringAttr` frontend-attribute values are silently dropped — a reimplementer must filter to `StringAttr` before formatting.

---

## 5. Type, Tensor, Constant & Dependency Serialization

### `printType` — MLIR Type → Python dtype string

`printType` (@ `0x20bf2c0`) is the single dtype renderer used by convert/iota/tensor. It probes in this order:

```c
function printType(Type t) -> string:             // 0x20bf2c0
    if t.isF32(): return "\"float32\""
    if t.isF16(): return "\"float16\""
    if t.isBF16(): return getImport(tensorizerSupportBase) + ".dtype.bfloat16"
    switch float8-family TypeID:
        Float8E3M4Type     → ".dtype.float8_e3m4"
        Float8E4M3Type     → ".dtype.float8_e4m3"
        Float8E5M2Type     → ".dtype.float8_e5m2"
        Float8E5M2FNUZType → ".dtype.float8_e5m2fnuz"
        Float8E4M3FNType   → ".dtype.float8_e4m3fn"
        Float8E4M3FNUZType → ".dtype.float8_e4m3fnuz"
        Float8E8M0FNUType  → ".dtype.float8_e8m0fnu"
        Float8E4M3B11FNUZType → ".dtype.float8_e4m3b11fnuz"
    if signed-int 32/16/8/64:   return "\"int32\"" / "\"int16\"" / "\"int8\"" / "\"int64\""
    if unsigned-int 32/16/8/64: return "\"uint32\"" / … / "\"uint64\""
    NEURON_LOG(ERROR, …) NCC_PYP004                // unsupported dtype
```

`float32`/`float16` and the int dtypes emit as JSON-quoted strings; `bfloat16` and the float8 family emit as `<support>.dtype.<name>` attribute references (so the Python runtime resolves the Penguin dtype object). The mix is deliberate: the simple types are recognized by name downstream, the exotic ones must reference the Penguin dtype singletons.

### `printTensor` — the `Tensor()` constructor

`printTensor` (@ `0x20c0510`, ~5700 bytes) emits the tensor-declaration line. It takes a `NeuronTensorAttribute`, a distinct type named in the symbol `…printTensor…NS_21NeuronTensorAttributeEb`:

```python
<name> = m5.Tensor(name="<n>", shape=(d0,d1,…), parent=…, id=…, dtype=<printType>,
                   layout="NC", attrs={'transposable': False, …})
```

Layout is inferred from rank: 2D→`"NC"`, 4D→`"NHWC"`, 3D→`"NHC"`, 1D→`"N"`, else `""`. Two builder variants share the path: `.SingleValueTensor(value=<v>, …)` for a 1-element `DenseElementsAttr` (value cast via `<support>.dtype.static_cast(<v>, .float32)` for f32), and `.TensorView(view=…)` for views. The `attrs={…}` dict carries the `neuron.*` tensor flags gathered by `collectTensorAttributes` (@ `0x20adb50`): `neuron.CrossPassTensor`, `neuron.non_local`, `neuron.no_opt`, `neuron.remat.value`, `neuron.transposable` → `non_local=True`, `'CrossPassTensor': ""`, `'no-opt': "…"`, `'remat.value': "…"`, `'transposable': True/False`.

### `printConstant` — externalize to `.npy`

Constants are **not** inlined as Python literals. `printConstant` (@ `0x20ecde0`) writes a real NumPy `.npy` file by hand for the `ConstantOp` value and emits a `SingleValueTensor`/`weight_load("<path>.npy")` reference:

```text
\x93NUMPY  V2  {'descr': '<dt>', 'fortran_order': False, 'shape': (<dims>,)}
dtype codes:  f2(f16) f4(f32) i1(s8) i4(s32) i8(s64) u1 u2 u4 u8 ; little-endian '<'
on open failure → NEURON_LOG(ERROR) NCC_PYP003 "Error opening const-file …"
```

> **QUIRK —** the emitter hand-rolls the `.npy` header (magic, version `V2`, the `descr`/`fortran_order`/`shape` dict, alignment padding) rather than calling numpy. Constants are externalized to files and reloaded at runtime via the emitted `weight_load` function — keeping the generated `.py` compact even for multi-megabyte weight tensors. A reimplementer must match the npy byte format exactly (including the `V2` 4-byte header-length field, not `V1`'s 2-byte) or the Python `np.load` will reject it.

### `printControlDeps` — the `.add_dep_edge` seam

`printControlDeps` (@ `0x20b8480`) scans operands; for each whose defining op satisfies `hilo::isControlDep` (an `AwsNeuronControlDep` custom-call), it emits:

```python
dst.add_dep_edge(m6.DependencyEdge(src=srcName, dst=dstName, idx_map=[],
    kind=m6.EdgeKind.ORDERED, predicates=[]))
```

`m6` is the `Dependency` module alias imported in `printStart` when `has_control_deps` is set. This is the terminal Penguin side of the control-edge handoff: the `AwsNeuronControlDep` custom-calls reified by the HLO `PreserveControlDeps` pass and flattened by `NeuronControlDepTupleSimplifier` become Penguin `.add_dep_edge` calls here (cross-ref [Control-Dependency Reification](control-dep-reification.md), [Control-Dep Tuple-Flatten](controldep-tuple-flatten-mlir.md)).

### `printMeta` — the `dl=` debug keyword

`printMeta` (@ `0x20ba3a0`) emits the trailing `dl=<support>.DebugLocation(tensor_op_name="…", file="…", line=N, column=0, hlo_id=…, parent=…)` plus a `{hlo_id=…, id=…}` block. Location text comes from `xla::hlo_utils::getSourceInfo` + `stripLoc` (@ `0x20aded0`), which flattens `FusedLoc`/`NameLoc` wrappers and escapes embedded quotes.

---

## 6. Elementwise Tier — the op→numpy Rewrite Table

### Purpose

The elementwise emitters are thin: they pick a numpy/scipy function reference for the mhlo op and call the assembler. The "operation" travels as the ufunc argument; the op-class tag (`'mhlo.unary'`/`'mhlo.binary'`/`'mhlo.ternary'`) is the second marker. This is the concrete shape of every elementwise NeuronTensorOp.

### The unary router

`printUnary` (@ `0x20f0840`) is a 37-byte trampoline: if the op is `mhlo.cbrt` it tail-jumps to `printCubeRootTensorOp` (decomposer, §8); otherwise it tail-jumps to `printUnaryTensorOp(op, label)`. `printUnaryTensorOp` (@ `0x20c83e0`) pre-resolves `getImport("numpy")`→`"np"` and `getImport("scipy.special")`→`m<N>`, defaults `funcRef = "np." + label`, then applies a **rewrite switch keyed on label length**, each arm loading its replacement string:

| Label (len) | Rewrite → funcRef | Anchor |
|---|---|---|
| `erf` (3) | `scipy.special.erf` | `.erf` @ `0x21a2db` |
| `gelu`/`silu` (4) | `<penguin@this+0x140>.gelu` / `.silu` | `.gelu` @ `0x2728e9` |
| `atan` (4) | `np.arctan` | `.arctan` @ `0x24659d` |
| `rsqrt` (5) | `np.rsqrt` (kept) | `.rsqrt` @ `0x2522d6` |
| `gelu_dx`/`silu_dx` (7) | `<penguin>.gelu_dx` / `.silu_dx` | @ `0x24e400` |
| `sigmoid` (7) | `scipy.special.expit` | `.expit` @ `0x226343` |
| `round_nearest_even` (18) | `np.rint` | `.rint` @ `0x2465a5` |
| (no match) | `np.<label>` | — |

The `not` op is dtype-selected by `isLogicalBoolean(op)` (@ `0x20af5b0`): `i1`→`np.logical_not`, integer→`np.bitwise_not`. Unary is shape- and dtype-preserving; no explicit `dtype=` is emitted (the numpy ufunc carries it).

### Consolidated elementwise table

Every row is `mhlo op → emit method → emitted NeuronTensorOp(funcRef, class)`. Confidence CERTAIN unless noted (all funcRef strings anchored in `_strings.json`).

| mhlo op | method | emitted call |
|---|---|---|
| `exp`/`log`/`sqrt`/`rsqrt`/`tanh`/`abs`/`sign`/`ceil`/`floor`/`sin`/`cos`/`tan`/`is_finite` | `printUnaryTensorOp` | `NeuronTensorOp(np.<f>, 'mhlo.unary')` |
| `negate` | `printUnaryTensorOp` | `NeuronTensorOp(np.negative, 'mhlo.unary')` |
| `round_nearest_even` | `printUnaryTensorOp` | `NeuronTensorOp(np.rint, …)` — REWRITE |
| `not` (i1) | `printUnaryTensorOp` | `NeuronTensorOp(np.logical_not, …)` |
| `not` (int) | `printUnaryTensorOp` | `NeuronTensorOp(np.bitwise_not, …)` |
| `logistic`/`sigmoid` | `printUnaryTensorOp` | `NeuronTensorOp(scipy.special.expit, …)` — REWRITE |
| `erf` | `printUnaryTensorOp` | `NeuronTensorOp(scipy.special.erf, …)` — REWRITE |
| `cbrt` | `printCubeRootTensorOp` | DECOMPOSE → 5 ops (§8) |
| `add`/`subtract`/`multiply` | `printBinaryTensorOp` | `NeuronTensorOp(np.add\|subtract\|multiply, 'mhlo.binary')` |
| `divide` (float) | `printBinaryTensorOp` | `NeuronTensorOp(np.divide, …)` |
| `divide` (integer) | `printBinaryTensorOp` | `NeuronTensorOp(np.int_div, …)` — elem-type picked |
| `maximum`/`minimum`/`power`/`remainder` | `printBinaryTensorOp` | `NeuronTensorOp(np.maximum\|minimum\|power\|fmod, …)` |
| `and`/`or`/`xor` | `printBinaryTensorOp` | `NeuronTensorOp(np.bitwise_and\|or\|xor, …)` |
| `shift_left` | `printBinaryTensorOp` | `NeuronTensorOp(np.logical_left_shift, …)` |
| `shift_right_logical` | `printBinaryTensorOp` | `NeuronTensorOp(np.logical_right_shift, …)` |
| `shift_right_arithmetic` | `printBinaryTensorOp` | `NeuronTensorOp(np.arith_right_shift, …)` |
| `compare {EQ/NE/GE/GT/LE/LT}` | `printBinaryTensorOp` | `NeuronTensorOp(np.{equal/not_equal/greater_equal/greater/less_equal/less}, …)` |
| `select` | `printTernaryTensorOp` | `NeuronTensorOp(np.select, 'mhlo.ternary')` |
| `clamp` | `print<ClampOp>` | DECOMPOSE → max(lo, min(x,hi)) (§8) |
| `convert` | `printConvert` | `NeuronTensorOp(dtype='<T>', xla_op)` |
| `iota` | `printIota` | `NeuronTensorOp(<penguin>.iota, iota_dim=<n>, shape=<tuple>, dtype='<T>', xla_op)` |

> **NOTE —** `mhlo.divide` integer→`int_div` is decided in `printOperation` (@ `0x20ef50f`): `r14 = "int_div"`; if `!getElementType(result).isInteger()` → `r14 = "divide"`. `printBinaryTensorOp` also independently holds `.int_div` @ `0x25608c`. `mhlo.remainder` emits `np.fmod`, not `np.remainder` — match the numpy semantics, not the mhlo name.

### Compare-direction map

`CompareOp` routes through `mlir::mapXla2PgDir` (a `DenseMap<ComparisonDirection,string>` built at static-init `0x20b74d0`): `EQ→"equal"`, `NE→"not_equal"`, `GE→"greater_equal"`, `GT→"greater"`, `LE→"less_equal"`, `LT→"less"` (all six strings are present in the pool). So `mhlo.compare {GT}` → `np.greater`, emitted as `'mhlo.binary'`.

---

## 7. Convert and Iota — the dtype-carrying emitters

`printConvert` (@ `0x20d5020`) emits a pure dtype-cast NeuronTensorOp — the only elementwise op that carries an explicit `dtype=`:

```python
out = m1.NeuronTensorOp(src, dtype='<T>', xla_op, '<frontend>')
```

`T = printType(cast<ShapedType>(result).getElementType())` (the cast *target* dtype); op-class tag `xla_op` (@ `0x256085`); the op's StringAttr at `[op+0x30]` is single-quoted as a frontend tag. Shape-preserving.

`printIota` (@ `0x20cec70`) emits:

```python
out = m1.NeuronTensorOp(m0.iota, iota_dim=<n>, shape=(d0,d1,…), dtype='<T>', xla_op, '<frontend>')
```

`funcRef = <penguin@this+0x140> + ".iota"`; `shape` via an anonymous `printTuple<long>` over the result shape; `iota_dim` from the `iota_dimension` IntegerAttr, sign-extended to its bitwidth and rendered via `std::to_string`.

> **GOTCHA —** the *source* mhlo attribute is `iota_dimension` (key str @ `0x252311`); the *emitted* Penguin keyword is shortened to `iota_dim` (@ `0x23e4a0`). Both strings exist in the binary. A reimplementer who copies the mhlo attribute name into the Python output produces `iota_dimension=` and the Penguin `iota` constructor — which expects `iota_dim=` — will throw. Do not conflate the two.

---

## 8. The Emit-Time Decomposers — cbrt and clamp

Two "elementwise" ops are **not** single Penguin calls — they synthesize new mhlo ops at emit time and recurse through `printOperation`. This is the only place the terminal printer *constructs IR* rather than printing it.

`printCubeRootTensorOp` (@ `0x20efe20`) implements sign-preserving cube root `cbrt(x) = (x / |x|) * |x|^(1/3)` as **5 new ops**, each built via `RegisteredOperationName::lookup` + `Op::build` + `OpBuilder::create`, then re-printed:

```c
function printCubeRootTensorOp(op):               // 0x20efe20
    c   = ConstantOp::build(1/3 as DenseElementsAttr)   // mhlo.constant @ 0x20f003f
    a   = AbsOp::build(x)                                // mhlo.abs      @ 0x20f0103
    p   = PowOp::build(a, c)                             // mhlo.power    @ 0x20f01dd
    s   = DivOp::build(x, a)        // sign factor x/|x| // mhlo.divide   @ 0x20f02c2
    r   = MulOp::build(p, s)                             // mhlo.multiply @ 0x20f03af
    for new_op in [c,a,p,s,r]: printOperation(new_op)   // recurse → np.power/abs/...
```

`print<ClampOp>` (@ `0x20ef830`) decomposes `clamp(lo,x,hi)` → `maximum(lo, minimum(x,hi))`: it `build`s a `MinOp(x,hi)` and a `MaxOp(lo,min)`, then `printOperation`s each → two `np.minimum`/`np.maximum` `NeuronTensorOp('mhlo.binary')` lines.

Neither op ever reaches a tensor-op emitter of its own, and neither belongs to the ternary family despite its arity: `printTernaryTensorOp` is used by `mhlo.select` and nothing else. `cbrt` expands to five ops (`constant`/`abs`/`power`/`divide`/`multiply`), `clamp` to two (`maximum`/`minimum`). Each synthesized op's `RegisteredOperationName::lookup` is guarded by a FATAL — "Building op `mhlo.<X>` … but it isn't known in this MLIRContext" — so a missing registration fails loudly rather than emitting a broken line.

> **QUIRK —** because the decomposers build *real* mhlo ops and recurse, the `1/3` exponent constant in `cbrt` flows through `printConstant` and is **externalized to a `.npy` file** like any other constant — the cube root of a tensor pulls a weight file into the emitted program. The decomposition is opaque to the Penguin layer: it sees five ordinary `NeuronTensorOp`s, not a cbrt.

---

## 9. Diagnostics — the NCC_PYP error catalog

Every failure path routes through `hilo::formatErrorMessage` + `NEURON_LOG(ERROR)` with the source path `hilo/MLIRPasses/Transforms/MhloToPythonPrinter.cc` (@ `0x3cd468`) and a `NCC_PYP###` code. The string pool holds the full `NCC_PYP001`–`NCC_PYP058` range; the driver-tier codes are:

| Code | Site | Reason |
|---|---|---|
| `NCC_PYP003` | `printConstant` | const-file open failure |
| `NCC_PYP004` | `printType` | unsupported dtype |
| `NCC_PYP054` | `printOperation` | `Expm1Op` reaches the terminal printer (must be expanded upstream) |
| `NCC_PYP055`/`056`/`057` | `printOperation` | generic unsupported op (one per dispatch sub-chain exit) |

> **GOTCHA —** `Expm1Op` has *no* lowering in the terminal printer (`NCC_PYP054`). It must be expanded upstream by `NeuronOpFusion::fuseExpm1Op` ([op-fusion](op-fusion-dot-elementwise.md)); reaching `printOperation` with an `mhlo.expm1` op is a compiler bug, not a user error. The same is true for any op missing from the dispatch cascade — the printer is the *last* pass, so an unhandled op here means an earlier legalization pass failed to lower it.

---

## Driver Infrastructure Functions

| Function | Address | Size | Role | Confidence |
|---|---|---|---|---|
| `runOnOperation` (MhloToPyPenguin) | `0x20a9870` | 1078 | pass entry → start | CERTAIN |
| `start` | `0x20f5560` | 181 | open ostream → printStart | CERTAIN |
| `printStart` | `0x20e6b10` | 4533 | imports + Function + inputs + walk | CERTAIN |
| `printOperation` | `0x20ee320` | 5280 | per-op TypeID dispatch | CERTAIN |
| `printOperandsAndAttributes` | `0x20c3e30` | 3185 | assemble one statement | CERTAIN |
| `printUnary` (router) | `0x20f0840` | 37 | cbrt-split / unary trampoline | CERTAIN |
| `printUnaryTensorOp` | `0x20c83e0` | 1771 | unary label → numpy/scipy funcRef | CERTAIN |
| `printBinaryTensorOp` | `0x20c77f0` | 2897 | binary label → numpy funcRef | CERTAIN |
| `printTernaryTensorOp` | `0x20c5500` | 939 | select → `np.select` | CERTAIN |
| `printCubeRootTensorOp` | `0x20efe20` | 2578 | cbrt decomposer (5 ops) | CERTAIN |
| `printConvert` | `0x20d5020` | 1126 | dtype-cast NeuronTensorOp | CERTAIN |
| `printIota` | `0x20cec70` | 1918 | iota NeuronTensorOp | CERTAIN |
| `printType` | `0x20bf2c0` | 1791 | Type → dtype string | CERTAIN |
| `printTensor` | `0x20c0510` | ~5700 | `.Tensor()`/`.SingleValueTensor()`/`.TensorView` | CERTAIN |
| `printConstant` | `0x20ecde0` | ~5000 | hand-write `.npy` + ref | CERTAIN |
| `printMeta` | `0x20ba3a0` | ~2200 | `dl=DebugLocation(…)` | CERTAIN |
| `printControlDeps` | `0x20b8480` | 2024 | `.add_dep_edge(…)` lines | CERTAIN |
| `getImport` | `0x20b48a0` | 1515 | memoized `import X as m<N>` | CERTAIN |
| `getUniqueName` | `0x20ae810` | 1765 | result base-name + dedup | CERTAIN |
| `defScalar()` | `0x20ae630` | 461 | mint `v<N>` | CERTAIN |
| `getWeightLoad` | `0x20b4ea0` | 433 | emit `def weight_load(p)` once | CERTAIN |
| `getOutputTensors` | `0x20c2780` | 4614 | LHS result-name vector | CERTAIN |
| `collectTensorAttributes` | `0x20adb50` | ~700 | gather `neuron.*` flags | HIGH |
| `stripLoc` | `0x20aded0` | ~900 | flatten Fused/NameLoc | HIGH |
| `perOpCounter` (static) | `0x9c6f1e0` | data | name-dedup StringMap | CERTAIN |

The StableHLO twin (`mlir::StableHLOToPythonPrinter`) is byte-parallel — same labels, same router, `stablehlo.*` TypeIDs — at: `printOperation` `0x218d6b0`, `printStart` `0x2182ff0`, `printUnaryTensorOp` `0x2162ac0`, `printBinaryTensorOp` `0x2161da0`, `printTernaryTensorOp` `0x215fb20`, `printCubeRootTensorOp` `0x218f2e0`, `printConvert` `0x2170d20`, `printIota` `0x2169650`.

---

## Evidence summary and limits of this reading

The headline — that this pass emits textual Python rather than an MLIR dialect — rests on
the string pool, not on interpretation. It holds `def weight_load(p):\n`, `  t = `,
`.load(p)\n`, `  return t\n`, `import `, `.Function(`, `.Tensor(`, `.NeuronTensorOp(`,
`.markInput(`, `.add_dep_edge(`, the class tags `'mhlo.unary'` / `'mhlo.binary'` /
`'mhlo.ternary'` and `xla_op`. Those are Python source fragments, including a literal
`def`. The targets `neuronxcc.starfish.penguin`, `…penguin.ir.Dependency` and
`neuronxcc.starfish.support` are dotted Python import paths. No `penguin.*` op mnemonic
exists anywhere, and there is no `penguin` dialect-registration symbol — while the
`mlir::Dialect::addOperations<mhlo::…>` symbol for the *input* dialect is present.

The dispatch structure is equally direct. `_ZN4mlir19MhloToPythonPrinter14printOperationEPNS_9OperationE`
resolves to `0x20ee320` (StableHLO twin `0x218d6b0`), and every emitter it fans out to —
`printUnaryTensorOp` `0x20c83e0`, `printConvert`, `printIota`, `printCubeRootTensorOp` —
resolves in the same symbol table. The elementwise funcRefs (`numpy`-derived names,
`scipy.special`, and the rewrite suffixes `.expit` / `.erf` / `.arctan` / `.rint` /
`.gelu` / `.silu`) are all present verbatim, as are both `iota_dim` and `iota_dimension`
as distinct tokens — the keyword shortening is real, not a transcription artifact.

Three things here are reconstructed rather than pinned. This binary is built without
decompilation, so control flow is read from disassembly: the decomposition of `cbrt` and
`clamp` is certain (`printCubeRootTensorOp` `0x20efe20` and `printTernaryTensorOp`
`0x20c5500` are distinct symbols, and the constituent mnemonics survive as FATAL-guard
strings), but the *exact* offsets of the `Op::build` + `printOperation` recursion sites
are less firm than the surrounding facts. The `NeuronTensorAttribute` field layout
(is_parameter / non_local / transposable) is inferred from `printTensor`'s `attrs={}`
branches rather than mapped field by field. And the `bool` emit-flag threaded from
`PassConfig+0x770` into printer `this+0x60` has unknown semantics — file-vs-stdout or a
verbosity toggle are both plausible. None of this touches the op→call table.

---

## Related Components

| Name | Relationship |
|---|---|
| Elementwise emitters (this page) | the spine that the Dot/Reduce/collective/fusion emitter bodies also route through |
| Control-Dep reification | upstream producer of the `AwsNeuronControlDep` calls `printControlDeps` consumes |
| Penguin IR model (Part 5) | the `Function`/`Tensor`/`NeuronTensorOp`/`DependencyEdge` objects this Python text constructs |
| hlo2penguin MLIR pipeline | places `--mhlo-to-py-penguin` as the terminal pass |

## Cross-References

- [hlo2penguin MLIR Pipeline Order & Entry Flow](hlo2penguin-mlir-pipeline.md) — where this terminal pass sits in the dialect pipeline
- [Control-Dependency Reification (HLO→MLIR→Penguin)](control-dep-reification.md) — produces the `AwsNeuronControlDep` custom-calls reified as `.add_dep_edge`
- [Control-Dep Tuple-Flatten (MLIR)](controldep-tuple-flatten-mlir.md) — flattens control-dep tuples before this pass
- [Neuron Op-Fusion — Dot, Elementwise, Transcendental Families](op-fusion-dot-elementwise.md) — `fuseExpm1Op` (why `Expm1Op` must never reach `printOperation`)
- [hlo2penguin Entry & the Native cl::opt Surface](../frontend/hlo2penguin-entry.md) — the binary that runs this pass
- [Pipeline Overview](../front/pipeline.md) — the compiler's full HLO→Penguin→BIR→walrus flow
