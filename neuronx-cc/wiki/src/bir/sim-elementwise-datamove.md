# The BIR simulator: elementwise / reduce, data-movement, and indirect gather/scatter evaluation

> *Version pin: `neuronx_cc 2.24.5133.0+58f8de22` (cp310; cp311 is size-identical, cp312 drifts — names/offsets/LUT bytes are the stable artifacts, not literal VAs). Subject binary: `neuronxcc/starfish/lib/libBIRSimulator.so` (md5 `f3acdcba9176056cb50daac01389dd13`, 36,264,288 B, 16,821 functions) — the **Strand-F** functional reference interpreter. `VA == file offset` for `.text` (`0xf1ad0 .. 0x58e798`) and `.rodata` (`0x58f000+`), confirmed via `readelf -S`, so every jump table, LUT, and string literal below is read by file offset. The `.data.rel.ro` map (vtables) carries a **+0x1000** delta — the `birsim::Memory` vtable at VA `0x2276370` reads at file offset `0x2275370`; subtract `0x1000` for any `xxd` on a vtable slot. All evidence is `nm -DC`, `objdump -d`, decompiled C bodies, and `.rodata` byte decode against this binary. Confidence tags: **CERTAIN** (read directly or `nm`-grounded), **HIGH** (multi-evidence), **MEDIUM** (structure-derived).*

## Abstract

This page documents how `libBIRSimulator.so` — the BIR-level functional reference interpreter that the compiler uses to numerically validate a lowered kernel — **evaluates** three op families: the per-element **elementwise + reduce** ops (TensorScalar / TensorTensor / TensorReduce / Pool), the **data-movement** ops (copy / DMA / transpose / shuffle / iota / memset), and the **indirect gather/scatter** ops (Gather / IndirectLoad / IndirectSave{Accumulate} / Generic indirect). It is the evaluation half of the simulator: the [dispatch + state model](sim-dispatch-state.md) (the 110-case `bir::IRVisitor::visit` switch and the `birsim::Memory` `DenseMap` tensor store) routes an opcode to a `visitInst<X>` handler, and the [shared cast/accumulate/reduce core](sim-core-arithmetic.md) (`cast_copy_or_accumulate`, the fp8/bf16/fp16 narrows, the four ALU functor maps) supplies the numerics. This page documents the *middle layer*: how each handler **walks an access pattern over the flat `MemoryObject` store** (the address generator), how the **reduce fold** is initialised and stepped, and how an **indirect index tensor is read and resolved** into a base+offset element address. Those three mechanisms — the AP walk, the reduce recurrence, and the index resolution — are the high-value content; the numeric leaves are cross-referenced, not duplicated.

The whole subsystem rests on one observation: **every one of these ops is one shared primitive — read an access pattern into a flat byte buffer, optionally transform in the flat domain, write it back through another access pattern.** Elementwise ops differ only in *what functor runs per element*; data-movement ops differ only in *how source and destination APs relate*; indirect ops differ only in *where the per-element source/destination offset comes from* (an index tensor instead of a static stride). Recognising this collapses ~40 opcodes onto ~6 helper bodies.

## 1. The shared substrate — AP → `MemoryObject` → AP

### 1.1 Operand fetch: `getInOutPhysicalAP`

Every handler resolves its operands through one accessor (per `nm -DC` and the disasm of `getInOutArg@0x1f7e30`):

```c
// getInOutArg @0x1f7e30, getInOutPhysicalAP @0x1cfdc0
PhysicalAccessPattern& getInOutPhysicalAP(InstVisitor* v, Instruction& I,
                                          unsigned idx, bool isOutput) {
    Argument a = isOutput ? I.getOutput(idx)        // output operand list
                          : I.getArgument(idx);      // input  operand list
    return getOrCreatePhysicalAP(cast<AccessPattern>(a));   // → PhysicalAccessPattern
}
```

The convention across **all** kernels is uniform (identical call-site usage in every body):

| Call | Operand |
|------|---------|
| `getInOutPhysicalAP(I, 0, 0)` | input arg 0 (primary data) |
| `getInOutPhysicalAP(I, 1, 0)` | input arg 1 (2nd tensor / indices) |
| `getInOutPhysicalAP(I, 2, 0)` | input arg 2 (predicate / 2nd scalar) |
| `getInOutPhysicalAP(I, k, 1)` | output `k` |

So a plain copy is exactly `read getInOutPhysicalAP(I,0,0)` → `write getInOutPhysicalAP(I,0,1)`. The `getInOutPhysicalAP@0x1cfdc0` defining body Hex-Rays-failed; the `(idx, isOutput)` bucket semantics come from `getInOutArg@0x1f7e30` (`getOutput` versus `getArgument`) plus the uniform call sites. That much is HIGH; the `cast` path inside the accessor is not read.

### 1.2 The `birsim::Memory` vtable — the read/write/gather/scatter entry points

The `InstVisitor` holds a `birsim::Memory*` at `this+0x138` (`= *((void**)this + 39)`, set by the ctor `*(this+312)=mem`). All tensor access goes through that object's vtable. Slots are byte-grounded from the `.data.rel.ro` relocations off vptr base `0x2276380` (`= _ZTVN6birsim6MemoryE + 0x10`); `nm -DC` confirms each target's signature:

| Slot (vptr+) | Function | Addr | Role |
|---|---|---|---|
| `+0x18` | `Memory::read(PhysicalAP&, bool)` | `0x17a420` | read source AP → `MemoryObject` |
| `+0x38` | `Memory::readIndirect(AP, MO, idx*, n, max)` | `0x16daa0` | **GATHER** dispatch |
| `+0x48` | `Memory::write(PhysicalAP&, MO&, Inst&, bool)` | `0x179fd0` | commit `MemoryObject` → output AP |
| `+0x58` | `Memory::writeAccumulate` | `0x173c50` | PSUM accumulate write |
| `+0x60` | `Memory::writeIndirect(…, MemoryReductionOp)` | `0x16db80` | **SCATTER** + RMW dispatch |
| `+0x68` | `Memory::writeIndirectLocal` | `0x17cca0` | local scatter |
| `+0x70` | `Memory::getPartitionStep` | `0x16d960` | partition stride |

`read`/`write` route to `readLocal@0x173e80` / `writeLocal@0x178b00` (or the `PhysicalMemory` / `NeuronCoresManager` remote variants) on `MemoryLocation::isRemote` / `::isShared`; `readIndirect` / `writeIndirect` split local-vs-remote on `loc+0x110 == 4` (the `KIND==4` remote gate). This routing is the [dispatch/state page](sim-dispatch-state.md)'s territory; here we use the slots as black-box read/write/gather/scatter primitives.

### 1.3 The `[W,Z,Y,X]` AP walk — `runAP` over `linearizedElementIntervals`

The address generator at the heart of every copy is `MemoryObject::runAP@0x298390` (per `nm`: `runAP(PhysicalAccessPattern const&, MemoryObject const&, bool, int, unsigned long, bool)`). `readAP@0x2989e0` = `runAP(dir=0)`; `writeAP@0x298a90` = `runAP(dir=1)`. It walks the AP's per-dim `(step, num)` list — the `SmallVector<APPair,4>` "Pattern" at AP+0x50 in canonical axis order `[W,Z,Y,X]` (Pattern[0]=W=partition/outermost, Pattern[3]=X=innermost) — by first flattening it into a list of contiguous byte intervals, then copying each interval into its physical address:

```c
// MemoryObject::runAP @0x298390  (dir=1 swaps src/dst)
elemBytes      = dword_5F7760[2*Dtype];                 // dt<=0x13, else "Unknown dtype"
assert AP.dtype == MemObj.dtype;                        // "the dtype between AP and MemObj must be the same"
partStride     = MemoryLocation.numBytesPerPartition;   // loc @AP+0x38
numPartBytes   = partStride;
intervals      = bir::linearizedElementIntervals(AP.Pattern, AP.offset);  // libBIR import

for (ivBase in intervals) {                             // each = an outer [W..] base element offset
    linElem  = elemBytes * ivBase;
    runBytes = intervalLen * elemBytes;                 // intervalLen = fused contiguous inner run
    while (runBytes) {
        partIdx  = linElem / numPartBytes;              // which partition lane
        intra    = linElem % numPartBytes;              // byte within that lane
        physByte = AP.offset + intra + partStride*partIdx;
        take     = min(runBytes, numPartBytes - intra); // stop at partition boundary
        src = MemObj.data + dstOff;  dst = loc.data + physByte;   // (swapped if dir=1)
        if (TRACKED)                                    // matmul-output write path only
            for (i in 0..take step elemBytes) {
                cast_copy_or_accumulate(validByte[i]!=0, dst+i, src+i, dtype);  // → core
                fast_memset(&validByte[i], -1);          // mark element produced
            }
        else
            memcpy(dst, src, take);                     // ordinary copy
        advance dstOff, linElem, runBytes by take;
    }
}
```

`bir::linearizedElementIntervals@0x205030` (a libBIR import; the consumer is fully read here, the producer is libBIR — see [the AccessPattern codegen page](codegen-accesspattern.md)) does the geometry (structurally):

1. **Drop unit dims** (`num==1`), pack the surviving `(step,num)` levels innermost-last.
2. **Contiguity fusion**: from the innermost dim outward, while `step[i] == Π(num[inner..])` the dims are contiguous and fuse into one run; `intervalLen` = product of the fused trailing `num`s. The first non-contiguous dim count = number of outer nested loops.
3. **Emit base offsets**: nested loops over the outer dims write one linear base element offset each (SSE2-unrolled inner store, `_mm_add_epi64` by `2*step`).

So an AP becomes a **list of interval base-offsets** (the non-contiguous outer `[W..]` product), each of length `intervalLen` elements (the fused contiguous inner `[..X]` run). Because `runAP` re-derives the physical byte address per interval from `(offset + intra + partStride*partIdx)`, **source and destination APs may differ arbitrarily in stride and offset** — a strided copy, a broadcast, a reshape, all fall out of the same loop with no special case. The only branch is the `TRACKED` gate: set iff the source instruction is a matmul/PE output (`inst+0x58` opcode `== 95` or `(opcode-7) <= 2`) AND `dir=1` AND the `MemObj+72` validity flag — i.e. PSUM-accumulator dataflow tracking; normal copies are a plain `memcpy`. The same `cast_copy_or_accumulate@0x28ff00` documented on [the arithmetic core page](sim-core-arithmetic.md) is reused here for the per-element validity/cast path.

This walk is the canonical `[W,Z,Y,X]` nesting: Pattern[0]=W is the partition/outermost axis (the 128 SBUF lanes), Pattern[3]=X is the innermost free axis that fuses into the contiguous run.

## 2. Elementwise — the four ALU functor maps and the two-stage element loop

### 2.1 The shared ALU substrate

Every elementwise/reduce kernel binds its `AluOpType` against one of **four** global `std::map<bir::AluOpType, std::function<…>>` ALU-functor tables, constructed once by the module static-init `sub_133280@0x133280` (the 3002-insn static-init packs each lambda as an `__m128i {key, _M_manager, _M_invoke}` triple). The map a kernel uses is selected by dtype/op class:

| Map | Addr | Functor type | Membership (op ordinals) |
|---|---|---|---|
| `ts_ops` | `0x22966c0` | `float(float,float)` | 21 FP32 ops: bypass0 add4 sub5 mult6 div7 max8 min9 and13 or14 xor15 is_eq18 ne19 gt20 ge21 lt22 le23 pow26 rsqrt28 abs29 abs_max30 abs_min31 |
| `ts_int_ops` | `0x2296560` | `int(int,int)` | INT32 bit/shift + saturating `sq_*` arithmetic |
| `ta_ops` | `0x2296440` | `long(long,long)` | INT64/address ALU (the "Pool/address" path) |
| `ts_int_cmp_ops` | `0x2296340` | `bool(int,int)` | 12 predicate functors (signed/unsigned compares) |

A lookup miss throws `runtime_error "…op0/op1 <name> … is not supported yet."` or a `NeuronAssertion` (codes 1931/1932/1990). Two ordinals are *deliberately absent* from `ts_ops`: `average(24)` (handled by a dedicated inline `1/N` functor in TensorReduce, §3) and `elemwise_mul(25)` (never reaches a map). The functor *bodies* (commutativity, `acc=LHS` convention) are the [arithmetic core page](sim-core-arithmetic.md)'s scope; here the maps are the *membership oracle* (which ops a kernel accepts).

### 2.2 `applyVecOperation<T>` — the two-stage TensorScalar element loop

The TensorScalar family funnels into one templated loop core (per `nm`: `applyVecOperation<float>@0x225d20`, `<int>@0x225790`, `<unsigned long>@0x225ac0`). It applies **two** functors in series — `out = op1(op0(in, s0), s1)` — with a per-stage operand-order swap and a broadcast cursor for the scalars:

```c
// applyVecOperation<T> @0x225d20<float> / 0x225790<int> / 0x225ac0<ulong> for (p in 0..npart)                          // outer = partitions (128 SBUF lanes)
  for (i in 0..free_per_part) {              // inner = free elements
    x = in[p][i];
    tmp      = (s0_ptr) ? (swap0 ? op0(*s0_ptr, x)   : op0(x, *s0_ptr))   : x;
    out[p][i]= (s1_ptr) ? (swap1 ? op1(*s1_ptr, tmp) : op1(tmp, *s1_ptr)) : tmp;
    if (i % s0_bcast_stride == s0_bcast_stride-1) ++s0_ptr;   // advance scalar0
    if (i % s1_bcast_stride == s1_bcast_stride-1) ++s1_ptr;   // advance scalar1
  }
```

With `s*_bcast_stride == free_per_part` the scalar is broadcast one-per-partition-row (a 128-lane scalar vector). An empty functor → `__throw_bad_function_call`. The three dtype paths of `doTensorScalar@0x1d8890` (`nm`-confirmed) all reach this loop, differing only in which map is consulted and the cast:

- `bir::isBitVecInstruction` → `ts_int_ops` (cast dt15, `applyVecOperation<int>`); assert else `NeuronAssertion "bitvec op is not valid"` (1931).
- `bir::isTensorScalarAddr` → `ta_ops` (cast dt18, `applyVecOperation<ulong>`); the two float scalars convert to int64 via the `2^63` unsigned-saturate trick.
- default arith → `ts_ops` (cast dt16, `applyVecOperation<float>`).

The scalar *source* is the only structural difference across the family: `TensorScalar` uses immediates (`scalar0@+0xF4`, `scalar1@+0x124`); `TensorScalarPtr@0x201fc0` reads them from scalar-tensor APs (`getInOutPhysicalAP(I,1,0)` / `(I,2,0)`); `TensorScalarCache@0x20a340` uses a *running cached value* (§4); `TensorTensor` uses a second full tensor (§2.3).

### 2.3 `doTensorTensor` — the flat two-tensor loop

`visitInstTensorTensor@0x1fd7f0` is a 10-byte wrapper that fetches the 2nd input AP and tail-calls `doTensorTensor@0x1fc550` (both per `nm`). It reads a *single* `AluOpType@+0xF0` and applies it elementwise across two tensors:

```c
// doTensorTensor @0x1fc550  (verbatim float arm)
op   = *((int*)inst + 60);                // AluOpType @+0xF0
in0  = read getInOutPhysicalAP(I,0,0);
in1  = read a3;                           // 2nd AP, supplied by the wrapper
fn   = (IsAluOpOnPool(op)) ? ta_ops[op]   // INT64 cast dt18 (Pool/address long path)
                           : (intDtype ? ts_int_ops[op] : ts_ops[op]);  // INT32 dt15 / FP32 dt16
for (i in 0..numElems)  out[i] = fn(in0[i], in1[i]);
```

`IsAluOpOnPool@0x22b1040` is an 8-byte reloc stub (real body in libBIR) that gates the `ta_ops` INT64 path — which AluOps + AP shapes route there is not disassembled in this binary (INFERRED that it covers Pool/address-ALU ops). The optional accumulate-into-output variant calls `birsim::InstVisitor::doEngineAccumReduce@0x1f9900` (a RMW fold of the TT result into an existing output buffer), which reuses the [arithmetic core](sim-core-arithmetic.md)'s per-dtype accumulate.

## 3. Reduce — `visitInstTensorReduce` and `visitInstPool`

### 3.1 The reduce axis selection and the fold core

`visitInstTensorReduce@0x1d7720` (per `nm`) reads `reduce_op@+0xF0` (`AluOpType`) and `axis@+0xF4` (`AxisListType`), then collapses the **innermost** `num_dims` AP dims (matching the [axis-enum crosswalk](codegen-enum-crosswalk.md)):

| `axis` | `num_dims` | reduced dims |
|---|---|---|
| 0 (X) | 1 | {X} (innermost) |
| 1 (XY) | 2 | {X, Y} |
| 2 (XYZ) | 3 | {X, Y, Z} |
| 3 (XYZW) | 4 | {X, Y, Z, W} — reduces the partition axis too |
| 5 (C) | 1 | channel/collapse-all path |

`reduce_extent = Π` of the innermost `num_dims` AP-dim counts; the kernel asserts `inAP.size() >= num_dims` ("Input AP dimension should be larger than num_dims, which is N"). The W axis (Pattern[0], the partition) is reduced **only** at `axis==XYZW(3)`; otherwise W is a kept/output axis. **Indirection is only allowed for `AxisListType::X`** — any other axis trips `NeuronAssertion "Indirection is only allowed for AxisListType::X"` + `"num_dims == 1"` (code 1929; the string is present in the binary).

The float fold is `applyEngReduce<float>@0x225600`:

```c
// applyEngReduce<float> @0x225600  (verbatim)
for (k in 0..n_out) {
    acc = in[base];
    if (is_average)  acc = acc / N;                  // divide-FIRST (running mean seed)
    for (j in 1..extent)  acc = reduceFn(acc, in[base + j*stride]);
    if (has_accumulate) {                            // EngineAccumulationType fold
        if (negate)  acc = -acc;
        acc = accFn(acc, out[k]);                     // fold into existing output
    } else if (negate)  acc = -acc;                   // negate AFTER reduce
    out[k] = acc;
}
```

Two subtleties matter for byte-for-byte fidelity: **negate is applied to the reduced scalar** (before the optional accumulate), and **average divides first** (`acc/N` is the seed, then `+x/N` folds — a running mean, not a sum-then-divide). When `reduce_op==24` (average, absent from `ts_ops`) the kernel binds a dedicated inline functor `sub_1A1DB0` capturing `N`: `redFn(acc,x) = acc + (1/N)*x`. `apply_transpose@+0xF8`, when set, first casts to FP32 and partition-transposes via a 32-lane (`& 0x1F`) doubly-nested copy before reducing. The INT path (output cast dt15) runs an explicit inner loop with `ts_int_ops[reduce_op]`; an unsupported `reduce_op` throws `"…visitInstTensorReduce(): op <name> … is not supported yet."`

### 3.2 `visitInstPool` — hard-coded 2-dim window reduce

`visitInstPool@0x1d7340` (`nm`-confirmed) reads `func@+0xF0` (`PoolFunctionType`) and **hard-codes a 2-dim reduce** over the two innermost AP dims (`window = AP[size-1].num * AP[size-2].num`), ignoring any `AxisListType`:

```c
window = AP[size-1].num * AP[size-2].num;            // two innermost dims, ALWAYS
out_count = window_count * getNumElementsPerPartition(inAP);
if (func == 1) {                                     // AvgPool
    sum = Σ window;                                  // SSE 4-wide horizontal add + scalar tail
    out = sum * (1.0 / (double)window_N);
} else {                                             // MaxPool (default arm)
    acc = -FLT_MAX;  for (x in window) acc = fmaxf(x, acc);  out = acc;
}
```

An unknown `func` throws `"…visitInstPool(): Unrecognized PoolFunctionType <name>…"` (via `PoolFunctionType2string`). From kernel semantics, `PoolFunctionType {0 = Max, 1 = Average}` — HIGH (the enum2string body is in libBIR, with only a thunk `@0xf0b70` here, so the literal spellings are behavioural).

## 4. The TensorScalarCache recurrence — reduce / cumulative / scan

There is **no** separate TensorScalarReduce or TensorScalarCumulative opcode. They are `TSCMode` values of `InstTensorScalarCache`, evaluated by `doTensorScalarCache@0x1b68d0` (driven by `visitInstTensorScalarCache@0x20a340`). The handler reads `op0@+0xF0`, `op1@+0x120` (both → `ts_ops`), `mode@+0x150` (`TSCMode {Reduce0, Cumulative1, TensorScan2}`), and `acc@+0x154` (`EngineAccumulationType`), then runs a **per-partition recurrence** over the scan dimension with a running cache:

```c
// doTensorScalarCache @0x1b68d0  (cache = a5, per-partition running value)
// acc-init by EngineAccumulationType: 3=Zero → *cache=0 ; 5=LoadAccumulate → *cache=*Imm1Ptr
for (i in scan_dim) {
    v34 = swap0 ? op0(*cache, in[i]) : op0(in[i], *cache);       // stage-0
    tmp = swap1 ? op1(*cache, v34)   : op1(v34, *cache);         // stage-1
    if (mode >= TensorScan)  out[i] = tmp;                       // scan/cumulative: write each
    // else (Reduce): write only the final folded scalar
    *cache = tmp;                                                // recurrence update
}
```

`Reduce` writes only the final cache; `Cumulative`/`TensorScan` write the running value at every element (a prefix scan). The `reverse0`/`reverse1` flags pick scan direction (operand-order swap = scan from the other end). The driver asserts that a 3-argument instance uses `acc == 5` (`LoadAccumulate`) and that `Imm1Ptr` (the initial cache value) is non-null in that mode. The `TensorScalarAffineSelect` op shares the `doIota@0x1edda0` affine-index machinery instead (§6).

## 5. Data-movement — copy, DMA, transpose, shuffle, replicate

### 5.1 The copy family → `doCopy`

`TensorCopy(23)`, `GenericCopy(1)`, `AbstractCopy(3)`, `Load(19)`, `Save(22)` are **all one-line thunks** to `doCopy@0x1d2660` (each sidecar body is `return doCopy(this, inst);`). `doCopy` is the universal AP→AP copy:

```c
// doCopy @0x1d2660 srcAP = getInOutPhysicalAP(I,0,0);   dstAP = getInOutPhysicalAP(I,0,1);   oob = 0;
if (I.opcode==32 /*DMACopy*/ && isScalarDynamicOffsetDMA(I)
    && (!srcAP.isAccessInBound(0) || !dstAP.isAccessInBound(0))) {
    if (DMAInst.getOobIsErr())  NeuronAssertion("!DMAInst->getOobIsErr()");  // FATAL
    oob = 1;                                                                  // else TOLERATE
}
tmp = Memory.read (srcAP, oob);              // vtable +0x18 → runAP read
Memory.write(dstAP, tmp, I, oob);            // vtable +0x48 → runAP write
```

All the `[W,Z,Y,X]` iteration and any dtype cast happen inside `runAP`/`read` (§1.3): the `MemObj` is shaped from the dst AP dtype, so differing src/dst dtypes produce a **cast-on-copy**, and differing strides produce an arbitrary strided/broadcast copy. `DMACopy` is the only one that adds the scalar-dynamic-offset OOB tolerance.

### 5.2 `visitInstDMACopy` — the mode multiplexer

`visitInstDMACopy@0x20b7b0` (`nm`-confirmed) reads `CopyMode@+0x128` and multiplexes (the `do*` asserts name each mode):

```c
switch (inst+0x128) {                        // CopyMode (D-E07)
  case 1: doDMATranspose(I); break;          // permute axes per transpose_op
  case 3: doDMAReplicate(I); break;          // broadcast src across dst partitions
  case 2: doDMACCE(I);       break;          // N-input in-flight reduce (add/min/max)
  case 0:                                    // Copy
    if (!isVectorDynamicOffsetDMA(I)) { doCopy(I); break; }   // plain
    // VECTOR-INDIRECT (HW-DGE scatter) path → §7
}
```

The simulator does **not** branch on the `DGEType` enum (`InstDMA+0xF8`); `rg` finds no `getDGEType`/`isHWDGE` read in any DMA kernel. Instead it branches on the AP-level manifestation: **SW-DGE = an explicit descriptor list** (`DMATrigger → DMABlock → DMADescriptor*`, re-fed through the same dispatcher — `visitInstDMATrigger@0x2124a0` advances a per-trigger fire-count cursor over its block list, `visitInstDMABlock@0x212420` iterates descriptors and re-`visit`s each), and **HW-DGE = a dynamic-offset AP** (`isVectorDynamicOffsetDMA`), which the sim materialises into a software address list and feeds to `readIndirect`/`writeIndirect` (§7). This unifies inline DMA and descriptor execution onto the same `do*` bodies. The three sub-modes:

- `doDMATranspose@0x1dd3b0` reads `transpose_op@+0x158` (IT32) / `+0x118` (IT71); if `0` it's an identity `doCopy`, else it relabels axes via `birsim::transpose@0x1b7770` (`iop→top` over "WZYX" labels, the **DMA-descriptor** consumer of the 14-perm `TransposeOps` table — distinct from the DVE `StreamTranspose` of §5.3).
- `doDMAReplicate@0x20b430` requires 2-dim in/out APs, computes `replCount = dst.partitionSize / src.partitions`, and broadcasts one source row-set across the destination partition dim.
- `doDMACCE@0x1da0c0` is an N-input in-flight reduce (`op@+0x178`): `add(4)` accumulates `acc[e] += (Constants ? Constants[i]*in_i[e] : in_i[e])`, `max(8)`/`min(9)` use `fmaxf`/`fminf`, all on FP32.

### 5.3 `StreamShuffle` and `StreamTranspose` — the DVE reorganizers

`visitInstStreamShuffle@0x1e1480 → doStreamShuffle@0x1e1090` permutes **whole partition rows** by a 32-entry byte mask, within 32-partition blocks:

```c
// doStreamShuffle @0x1e1090 mask = StreamShuffle::getMaskEvalIfNeeded();   assert mask.size()==32;
assert dstAP.dtype == srcAP.dtype;             // "StreamShuffle does not support cast"
partBytes = elemBytes * NumElementsPerPartition;
for (blockStart = 0; blockStart < numOutPartitions; blockStart += 32)
  for (i in 0..31) {
    m = mask[i];
    if (m == 255) continue;                    // 255 = leave output partition untouched
    assert m < 32;                             // "StreamShuffle Mask entry must be <32"
    memcpy(dst[blockStart+i], src[blockStart+m], partBytes);   // out_part[i] = in_part[m]
  }
```

`visitInstStreamTranspose@0x1e1490` (a **defining** body, not a thunk) is a 32×32 blocked partition⇄element transpose — the SBUF in-place tile transpose. Within each aligned 32×32 tile it swaps the low-5 bits of `p` (partition) with the low-5 bits of `e` (element), and the tile-block coordinates likewise:

```c
// visitInstStreamTranspose @0x1e1490 assert dstAP.dtype == srcAP.dtype;             // "StreamTranspose does not support cast"
for (p in 0..inPart-1)
  for (e in 0..inElem-1) {
    eBlk = e & ~31; eLow = e & 31;             // 32-tile split of element index
    pBlk = p & ~31; pLow = p & 31;             // 32-tile split of partition index
    dstIdx = (pLow | eBlk) + outElem * (pBlk | eLow);   // swap low-5 of p ↔ e
    memcpy(dstObj + elemBytes*dstIdx, srcObj[p] + elemBytes*e, elemBytes);
  }
```

`StreamTranspose` hard-codes the 32×32 swap and does **not** consume a `TransposeOps` perm code; the perm-table path is the DMA-descriptor transpose (§5.2). See [the codegen data-movement leaf page](codegen-data-movement.md) §6 for the three distinct transposes on the producing side.

### 5.4 `Iota` and `Memset` — synthesis from coordinates

`visitInstIota@0x1efbd0 → doIota@0x1edda0` fills each output element with an **affine function of its `[W,Z,Y,X]` coordinates** (`doIota` dispatches on `inst+0x58`: `61`=Iota, `62`=AffineSelect):

```c
// doIota @0x1edda0 base = getBaseEvalIfNeeded();                   // affine constant term
for (w in 0..num_W) for (y in 0..num_Y) for (z in 0..num_Z) for (x in 0..num_X)
    out[flat] = (int32)( base + step_W*w + step_Y*y + step_Z*z + step_X*x );
```

For `TensorScalarAffineSelect(62)` the affine index is then compared against a threshold via `ts_int_cmp_ops[compare_op]` and selected against a `fill_value@+0x194` (`pred ? in[i] : fill`). `visitInstMemset@0x1d5060` fills the output AP with a constant (`mode@+0xF0 == 0`, value `@+0xF8`) or a random value (`mode==1`: a per-partition Xorwow state when `inst+0x90==1`, else a global `std::mersenne_twister_engine`), store-width = dtype size.

### 5.5 Predicated move and ternary select

`visitInstCopyPredicated(52)@0x206a90` (HIGH; a 1413-line body, mostly logging) is a per-element conditional move keyed by a **byte** predicate mask (`pred[i] = *(int32*)(predBase + 4*i)`, widened to dtype 14), with a `useFalseFill@+0x118` mode and a `Reverse@+0x140` mode, followed by a `doEngineAccumReduce` accumulator side-effect. `visitInstSelect(51)@0x206280` is the simpler ternary `out[i] = pred[i] ? True[i] : False[i]` with a **strict 0/1** predicate (any other value throws); True/False may each be a full tensor or a broadcast scalar immediate, with no accumulate.

## 6. Indirect gather/scatter — index resolution and the serial apply loop

The six indirect kernels — `IndirectLoad(43)`, `IndirectSave(45)`, `IndirectSaveAccumulate(46)`, `IndirectCopy(26)`, `GenericIndirectLoad(42)`, `GenericIndirectSave(44)` (all `nm`-confirmed at the documented addresses) — share **one** gather/scatter machine. The per-opcode visitor does only the **front end**: resolve operands, **build a flat `u64` index/offset list `idx[]`**, then hand it to one of two `Memory` virtual entries. Gather (`Load` family) → `readIndirect` (slot `+0x38`); scatter (`Save` family) → `writeIndirect` (slot `+0x60`) + a reduce op. Both route local/remote on `isRemote()` and funnel into the **same** inner executor `Memory::doCopyIndirect@0x17a930`.

### 6.1 Index-vector dtype decode

The index tensor is read into a `MemoryObject` (slot `+0x18`); its dtype field selects the decode (`xmmword_5F2720` verified byte-exact = `{0xffffffff00000000, 0xffffffff00000000}`, the per-lane `−0x100000000` rebase):

```c
// per-kernel index decode (Load/Save family)
if (dtype == 15 /*int32*/)        idx[k] = (u64) *(uint32*)(indices + 4*k);        // zero-extend (_mm_cvtepu32_epi64)
else if (dtype ∈ {18,19} /*u64/i64*/) idx[k] = *(u64*)(indices + 8*k) − 0x100000000; // 64-bit rebase (top dword −1)
else  throw "loading mem addr, which must be uint64";                              // index dtype ∈ {i32,u64,i64}
```

The `−0x100000000` rebase (decrement the top dword by 1) is applied per lane via `_mm_add_epi64` with the SIMD constant above. Each `idx[k]` is then scaled to **bytes** by `qword_5F4500[dataDtype] * Pattern[final].step` (the dtype byte-stride LUT = `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}`, verified byte-exact). A separate standalone resolver `Memory::obtainDataAddrsInByte@0x179610` requires **uint16** indices ("TensorIndirect Indices data type must be uint16") — the compact representation used by the AP-geometry resolver; the six live kernels inline their own offset math instead.

### 6.2 The three index-resolution shapes

**Plain gather (`IndirectLoad`):** one index list, scaled, fed straight to `readIndirect`. `out[e] = dataSrc[idx[e]]`. Output AP asserts final-dim step `== 1` ("Must have step 1 in final dim of IndirectLoad output AP").

**Scatter-accumulate (`IndirectSaveAccumulate`, the embedding-bag scatter-add):** a 5-operand descriptor (indices, ifmap values, embedding-table base-addr, dst data, num-entries). The index build is a 2-D nested loop (partition × per-partition entry) that **pads unused slots with `−1`**:

```c
// visitInstIndirectSaveAccumulate @0x1e2640  (index build)
for (p, entry) {
    v15 = p + entry*numElemsPerPartition;                      // flat scatter slot
    if (entry >= getNumIndices /*inst+0x128*/)  idx[v15] = -1; // OOB/UNUSED → skip
    else  idx[v15] = embTableBase(*v40) − 0x100000000
                   + qword_5F4500[ifmapDtype] * ( entry_step_elements /*+0x150*/
                                                  * indexTensor[v15] );   // base + dtypeBytes*(stride*tokenIdx)
}
redOp = AluOpType2MemoryReduction(I, op /*+0x178*/);           // → {copy, add} only (see §6.4)
```

`getNumIndices > AddrCount` trips `"AddrCount >= I.getNumIndices() … insufficient indices"`. So `idx[e] = tableBase + dtypeBytes*(stride*tokenIndex[e])`; slots past the declared count are `−1` (skipped).

**Multi-dim gather/scatter (`GenericIndirectLoad`/`Save`):** an offset AP plus a **list of index APs, one per indirect dim**. The kernel reads one index `MemoryObject` per indirect dim, computes a per-dim coefficient `coef_d` (the product of the AP free-dim sizes inboard of dim `d` — so a smaller dim ordinal carries a larger stride), and **sums** the contributions:

```c
// offset[e] = Σ_d  dtypeBytes * coef_d * indexTensor_d[e]
```

The `IndirectDim` enum (W0/Z1/Y2/X3 — see [axis enums](codegen-enum-crosswalk.md)) names *which AP axis* each index vector drives; `indirect_dims@inst+0x128` supplies the per-dim ordinal list, whose `.size()` must equal the number of index APs ("number of indirect dims must match number of indices vector"). `IndirectCopy(26)` is a row-wise gather-copy with a `num_valid_indices@+0xF0` validity counter, applied in 16-partition blocks via `readIndirect`.

### 6.3 The serial per-element apply loop — `doCopyIndirect`

All six kernels' offset lists converge on `Memory::doCopyIndirect@0x17a930` (source string `"Memory.cpp:doCopyIndirect"`). It is a **serial, in-order** loop. The `−1` sentinel is the *only* OOB action — skip; not clamp, not wrap-to-modulo, not error:

```c
// Memory::doCopyIndirect @0x17a930 for (i in 0..count) {
    v20 = idx[i];
    if (v20 == -1)  continue;                          // OOB / UNUSED → SKIP (only OOB action)
    if (apply_wrap)                                    // IndirectDim axis-rebase (NOT a bounds clamp)
        v20 = base + (v20 % numIndirectIndices) + idHi * (v20 / numIndirectIndices);
    dst = ...; src = ...;                              // direction picks src/dst
    if (redOp == 0 /*copy*/)
        memcpy(dst, src, entrySize);                   // gather / scatter overwrite
    else {                                             // RMW (add)
        elemSz = qword_5F33C0[apDtype];                // assert entrySize % elemSz == 0
        for (k in 0 .. entrySize/elemSz) {
            cast_copy_or_accumulate(redOp, dst, src, apDtype);   // → arithmetic core
            dst += elemSz; src += elemSz;
        }
    }
}
```

Because the loop is serial, **duplicate indices accumulate in list order** (the 2nd visit to a dst cell reads the 1st's result) — a *non-atomic* embedding scatter-add. For `redOp=add`, K colliding contributions reduce to `ALU^K(dst0, src_i1..iK)`; for `redOp=copy` (`IndirectSave`) it is last-writer-wins. The `apply_wrap` rebase splits the linear index across the indirected dim and its complement (an axis-rebase, **not** a bounds operation) — gated by a flag, not by bounds.

### 6.4 The reduce op — only `{copy, add}` are reachable

The scatter `redOp` comes from `AluOpType2MemoryReduction@0x1b0ed0` (per `nm`), which maps **only** `bypass(0) → copy(0)` and `add(4) → add(1)`; **any other `AluOpType`, including `min(9)`/`max(8)`, throws** a `NeuronAssertion(false)` (`inst_visitor.cpp:363`, code 1465). So at the simulator level the indirect ops reach only `{copy, add}` — the CCE verifier *permits* `{add, min, max}`, but this build's converter realises copy/add only. The `cast_copy_or_accumulate@0x28ff00` kernel *has* `op==2(min)`/`op==3(max)` branches (SSE `minss`/`maxss`, `pminsd`/`pmaxsd`, int64 `cmov`), but they are **unreachable** through every current producer — dead/vestigial in this build. The full per-dtype reduce/cast semantics (which 8 dtypes reduce, fp8/bf16/fp16 narrows, the WRAP-not-saturate integer behaviour) are documented in full on [the arithmetic core page](sim-core-arithmetic.md); this page only establishes *where the op comes from* and *which ops survive the converter*.

## 7. The HW-DGE bridge — dynamic-offset DMA materialised as a scatter

The `DMACopy` `Copy` mode's vector-indirect arm (§5.2) reuses the §6 machine. When `isVectorDynamicOffsetDMA(I)`, `getIndirectAddrsVector@0x1db900` materialises the descriptor address list: it finds the offset arg (`getIndirectArgId`), reads it into a `MemoryObject` (indices must be uint16), and computes two parallel flat-byte-address vectors:

```c
// getIndirectAddrsVector @0x1db900  (HW-DGE address materialisation)
dstAddr[i] += dtype_size * stride * index[i];
srcAddr[i] += i * partition_stride * dtype_size;
// bounded by IndirectDimMaxIndex; OOB index (index >= maxIndex):
//   if !getOobIsErr() → address = -1 sentinel (skipped); else FATAL
```

The address vectors then feed `readIndirect`(slot `+0x38`) / `writeIndirect`(slot `+0x60`), with `isDstReduceDGE(I)` selecting the accumulate-on-write (CCE reduce) path — `op = AluOpType2MemoryReduction(I, cce_op@+0x178)`, i.e. the same `{copy, add}` restriction as §6.4. So a hardware-DGE DMA is, in the simulator, **a software-materialised offset list run through the indirect scatter machine** — confirming the unification: gather/scatter and dynamic DMA are one mechanism.

## 8. Function map

| Function | Addr | Role | Conf |
|---|---|---|---|
| `MemoryObject::runAP` | `0x298390` | `[W,Z,Y,X]` interval walk + memcpy/accumulate | CERTAIN |
| `bir::linearizedElementIntervals` | libBIR import | AP → byte-interval list | CERTAIN (consumer) |
| `InstVisitor::getInOutPhysicalAP` | `0x1cfdc0` | operand → PhysicalAP (HR-failed) | HIGH |
| `InstVisitor::doCopy` | `0x1d2660` | universal AP→AP copy + DMA OOB | CERTAIN |
| `applyVecOperation<float/int/ulong>` | `0x225d20`/`0x225790`/`0x225ac0` | two-stage TensorScalar loop | CERTAIN |
| `InstVisitor::doTensorScalar` | `0x1d8890` | TensorScalar dtype paths | CERTAIN |
| `InstVisitor::doTensorTensor` | `0x1fc550` | flat two-tensor ALU | CERTAIN |
| `visitInstTensorReduce` | `0x1d7720` | axis reduce + avg/negate/accumulate | CERTAIN |
| `applyEngReduce<float>` | `0x225600` | per-partition reduce/scan fold | CERTAIN |
| `visitInstPool` | `0x1d7340` | 2-dim window Avg/Max pool | CERTAIN |
| `doTensorScalarCache` | `0x1b68d0` | reduce/cumulative/scan recurrence | CERTAIN |
| `doIota` | `0x1edda0` | affine index ramp (+ AffineSelect) | CERTAIN |
| `visitInstDMACopy` | `0x20b7b0` | CopyMode mux (Copy/Transpose/Repl/CCE) | CERTAIN |
| `doDMATranspose` / `doDMAReplicate` / `doDMACCE` | `0x1dd3b0`/`0x20b430`/`0x1da0c0` | DMA sub-modes | CERTAIN |
| `doStreamShuffle` | `0x1e1090` | 32-partition mask permute | CERTAIN |
| `visitInstStreamTranspose` | `0x1e1490` | 32×32 tile transpose | CERTAIN |
| `visitInstGather` → `instGatherImpl` | `0x1f39d0` → `0x1b48b0` | per-row gather | CERTAIN |
| `visitInstIndirect{Load,Save,SaveAccumulate,Copy}` | `0x1e17a0`/`0x1e1c20`/`0x1e2640`/`0x1e2070` | gather/scatter front-ends | CERTAIN |
| `visitInstGenericIndirect{Load,Save}` | `0x1f5200`/`0x1f3b30` | multi-dim gather/scatter | CERTAIN |
| `Memory::readIndirect` / `writeIndirect` | `0x16daa0` / `0x16db80` | gather/scatter dispatch (local/remote) | CERTAIN |
| `Memory::doCopyIndirect` | `0x17a930` | serial per-element apply (−1 skip) | CERTAIN |
| `getIndirectAddrsVector` | `0x1db900` | HW-DGE address materialisation | CERTAIN |
| `AluOpType2MemoryReduction` | `0x1b0ed0` | AluOp → {copy, add} (else throw) | CERTAIN |
| `sub_133280` (ALU-map static-init) | `0x133280` | builds the four functor maps | CERTAIN |
| `ts_ops`/`ts_int_ops`/`ta_ops`/`ts_int_cmp_ops` | `0x22966c0`/`0x2296560`/`0x2296440`/`0x2296340` | ALU functor maps | CERTAIN |

Struct-field anchors (cross-checked against the [container model](container-model.md) / [memory-location](memory-location.md)): `PhysicalAccessPattern` — Dtype `@+0x30`, `MemoryLocation*` `@+0x38`, Pattern(`APPair[W,Z,Y,X]`) `@+0x50`, offset `@+0xD0`, `DynamicAPINFO*` `@+0x1D8` (indirect_dim `@+0x74`, indirect_dim_max_index `@+0x70`). Instruction — opcode `@+0x58`, reduce/op0/mode/scalars at the `+0xF0..+0x158` window per op. Dtype byte-stride LUTs `qword_5F4500` / `qword_5F33C0` / `unk_5F7760` = `{1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8}`; index rebase `xmmword_5F2720` = `{0xffffffff00000000, 0xffffffff00000000}`.

## 9. Where the model is pinned, and where it isn't

The five claims this page turns on, and what carries each (binary md5 `f3acdcba9176056cb50daac01389dd13`):

1. **`runAP` is the single AP-walk address generator, and it reuses the arithmetic core for matmul outputs.** `nm -DC` gives `MemoryObject::runAP@0x298390` with the documented signature; the dtype-equality assert `"the dtype between AP and MemObj must be the same"` is in the binary; the `TRACKED` branch's `cast_copy_or_accumulate@0x28ff00` resolves. The interval *producer* `linearizedElementIntervals` is a libBIR import, so [INFERRED] its exact `(step,num)` interleave — the consumer is fully read, the producer is not here.

2. **Indirect index decode: int32 zero-extend; u64/i64 rebase by `−0x100000000`; else throw.** The rebase SIMD constant `xmmword_5F2720` reads byte-exact `{0xffffffff00000000, 0xffffffff00000000}` at file offset `0x5f2720`, and the gate string `"loading mem addr, which must be uint64"` is present. The dtype byte-stride LUT at `0x5f7760` reads byte-exact `{1,1,2,…,8,8}`.

3. **`doCopyIndirect`: `−1` is the only OOB action (skip), and duplicate indices accumulate serially.** `Memory::doCopyIndirect@0x17a930` resolves with source string `"Memory.cpp:doCopyIndirect"`; the `EntrySize % DtypeSize == 0` and `IndirectSaveAccumulate: insufficient indices` strings are present. The serial-accumulate model comes from the loop structure plus the sentinel; no NEFF with colliding token-ids was available to fixture-verify it.

4. **The scatter reduce op is restricted to `{copy, add}`; min/max throw.** `AluOpType2MemoryReduction@0x1b0ed0` resolves, with two clean return blocks (`a2==0 → 0`, `a2==4 → 1`) and an `inst_visitor.cpp:363` `NeuronAssertion` else-path, read from disasm on [the arithmetic core page](sim-core-arithmetic.md). The min/max kernels inside `cast_copy_or_accumulate` exist but are unreachable from every producer — dead in this build rather than removed, which is a HIGH reading, not a byte-proof.

5. **`StreamTranspose` is a fixed 32×32 partition⇄element swap, with no perm code and no cast.** `visitInstStreamTranspose@0x1e1490` is a defining body, not a thunk; the `"StreamTranspose does not support cast"` and `"StreamShuffle Mask must have 32 elements"` strings are present. The DMA-descriptor transpose — which *does* consume the 14-perm `TransposeOps` table — is a separate kernel family (`doDMATranspose@0x1dd3b0`).

**What static analysis cannot reach here.** Addresses, signatures, strings, and the two `.rodata` constants are byte-grounded. The *control flow inside* each handler — loop bounds, the exact `apply_wrap` rebase arithmetic, the `IsAluOpOnPool` predicate, the `TRACKED`-gate predicate — is taken from the decompiled C bodies and is HIGH. No NEFF or BIR-JSON fixture was available to byte-diff a real copy, gather, or scatter against a reference run, so the numerical-equivalence claims (serial accumulate order, the `−1` skip, the average divide-first seed) rest on the binary's structure rather than on execution. The `getInOutPhysicalAP` cast path and the `linearizedElementIntervals` interval-construction algorithm are the two genuinely [INFERRED] links in the chain — one Hex-Rays-failed, one libBIR-resident.

## 10. Cross-references

- [The BIR simulator: dispatch + state model](sim-dispatch-state.md) — the 110-case `visit` switch and the `birsim::Memory` `DenseMap` tensor store that the AP walk reads/writes.
- [The BIR simulator: core arithmetic](sim-core-arithmetic.md) — `cast_copy_or_accumulate`, the fp8/bf16/fp16 narrows, the four ALU functor maps' bodies, and the full per-dtype reduce matrix referenced throughout.
- [KlirToBirCodegen: the data-movement leaf family](codegen-data-movement.md) — the *producing* side: how `codegenNcCopy` / `codegenNcDmaCopy` / `codegenGenericIndirectSave` emit the `InstTensorCopy` / `InstDMACopy` / `InstGenericIndirectSave` nodes this page evaluates, and the three distinct transposes.
- [AccessPattern codegen](codegen-accesspattern.md) — the `APPair[W,Z,Y,X]` descriptor geometry the AP walk consumes.
- [Dynamic AP codegen](codegen-dynamic-ap.md) — the `DynamicAPINFO` (indirect_dim, max_index) the gather/scatter index resolution reads.
- [ALU-op modes](aluop-modes.md) and [op-family enums](op-family-enums.md) — the `AluOpType` / `AxisListType` / `IndirectDim` ordinals used above.
