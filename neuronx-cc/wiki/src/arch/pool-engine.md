# Pool Engine — Windowed Pooling and the Reduce Leg

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; see [versions](../reference/versions.md)). The CoreV2 wire encoders live in `libwalrus.so` (`.text` VA == file offset); the `PoolFunctionType`/`AluOpType`/`AxisListType` enums live in `libBIR.so`; the reference datapath kernels live in `libBIRSimulator.so`. Other wheels differ; treat every address as version-pinned.*

## Abstract

The **Pool engine** is Trainium's windowed-reduction unit — the leg of the TPB that owns 2-D **average/max pooling**, free-axis **tensor reduction**, and the 8-wide **argmax** primitives. Unlike the [PE engine](pe-engine.md), it is not one array with one job: four distinct BIR ops route across this leg, and they fan out to **ten** different wire opcodes spread over three physical datapaths (the Pool engine proper, a reduce engine, and the DVE). A reimplementer's first task is therefore not "encode a pool" but "decide *which* of ten opcodes a given pooling/reduction op becomes" — and that decision is driven by enum values the front-end set long before codegen.

Two design choices make this leg counter-intuitive. First, **the pooling window is not a field** — there is no `window_size`/`stride` in the bundle. The window is the *product of the two innermost access-pattern dimensions*, and for average pooling the encoder folds it into a single reciprocal constant `1/N` written at bundle `+0x28`, so the silicon does a sum and one multiply, never a divide. Second, **`TensorReduce` is two physically distinct datapaths wearing one BIR op**: a *free-axis* path that reduces innermost loop dimensions, and a *cross-lane* path that reduces across the partition (channel) axis — different opcodes, different reduce-command encodings, different legality rules. Choosing the wrong one is silently wrong.

This page is the **functional/architectural model**: the op→datapath→opcode routing, the window-as-reciprocal trick, the two reduce datapaths, and the MaxIndex 1→2 bundle split — enough to map a pooling or reduction op onto the correct Pool-leg datapath and opcode, and to know the engine's hard limits. It does **not** cover the bit-level layout of the sync-command regions or the `TENSORxD` descriptor interiors; that is [ISA: Pool encoding](../isa/pool-encoding.md) (2.12).

For reimplementation, the contract is:

- **The four-op / ten-opcode routing** — which BIR op (`InstPool`/`InstTensorReduce`/`InstMax`/`InstMaxIndex`) becomes which wire opcode(s), and the enum field that forks each.
- **`PoolFunctionType = {Max, Avg}` — and nothing else.** There is no `Sum` pool variant; sum/min/argmax reductions are *separate ops*, not pool functions.
- **The window-as-reciprocal-fold** — window `N = Pattern[3].num × Pattern[4].num`, encoded as `1/N` at `+0x28` for Avg and `1.0f` for Max; stride lives inside the descriptor, never as a discrete field.
- **The two reduce datapaths** — free-axis (TR) vs cross-lane (CR), forked on `axis@+0xF4`, with their distinct reduce-command, negate, and legality semantics.
- **The MaxIndex 1→2 split** — one BIR `InstMaxIndex` emits *two* co-issued DVE bundles (opcodes 109 + 110), and the split happens in the encoder, not in KLR→BIR codegen.

| | |
|---|---|
| **BIR ops** | `InstPool` (IT20), `InstTensorReduce` (IT27), `InstMax` (IT88), `InstMaxIndex` (IT89) |
| **Wire opcodes** | Pool `69`; reduce `124`/`125` (free-axis) + `66`/`82`/`131`/`132` (cross-lane); Max `108`; MaxIndex `109`+`110` — **ten total** |
| **Engines** | `InstPool` → Pool engine; `InstTensorReduce` → reduce engine; `InstMax`/`InstMaxIndex` → DVE (engine 5) — all read from `inst+144`, *not* re-assigned in the encoder |
| **Pool encoder** | `CoreV2GenImpl::visitInstPool` @ `0x1239e50` (3236 B, 117 bb) |
| **Reduce encoder** | `CoreV2GenImpl::visitInstTensorReduce` @ `0x12383a0` (5807 B, 208 bb) |
| **Max / MaxIndex encoders** | `visitInstMax` @ `0x1273120`; `visitInstMaxIndex` @ `0x1254650` |
| **`PoolFunctionType`** | `libBIR` `PoolFunctionType2string` @ `0x4010e0` — exactly `{0 Max, 1 Avg}` |
| **`AxisListType`** | `libBIR` `AxisListType2string` @ `0x4011b0` — `{0 X, 1 XY, 2 XYZ, 3 XYZW, 4 XYZWC, 5 C}` |
| **Reference model** | `birsim` `visitInstPool` @ `0x1d7340`; `visitInstTensorReduce` @ `0x1d7720`; `applyEngReduce<float>` @ `0x225600` |

---

## The Op-to-Datapath Routing

### Purpose

The Pool leg is the single most heavily *overloaded* leg of the ISA: four BIR ops, ten wire opcodes, three engines. Before any encoding detail matters, a reimplementer has to internalize the routing table — because the same source-level intent ("reduce this tensor") splits across four different ops, and one of those ops (`TensorReduce`) splits *again* into two datapaths. Get the routing wrong and you encode a perfectly well-formed bundle for the wrong silicon.

### The Routing Table

```text
BIR op            IT   engine          wire opcode(s)              datapath
----------------  ---  --------------  --------------------------  ---------------------
InstPool          20   Pool engine     69                          windowed Max/Avg
InstTensorReduce  27   reduce engine   124 / 125  (free-axis TR)   innermost-dims fold
                                        66 / 82 / 131 / 132 (X-lane) partition / channel fold
InstMax           88   DVE (5)         108        (MAX8)           8-wide running max
InstMaxIndex      89   DVE (5)         109 + 110  (FIND_INDEX8)    8-wide argmax (two bundles)
```

The ten opcodes are: **69** (Pool); **124, 125** (free-axis reduce, float / bit-vector); **66, 82, 131, 132** (cross-lane reduce, forked on bit-vector × transpose — see [The Cross-Lane Opcode Fork](#the-cross-lane-opcode-fork)); **108** (Max8); **109, 110** (the two MaxIndex bundles). That is 1 + 2 + 4 + 1 + 2 = **10**. (CONFIRMED — re-disassembled from `libwalrus.so` cp310; see the per-op `### Algorithm` sections for the byte anchors.)

> **CORRECTION —** the wire-opcode count is **ten**, not nine. The enumerated set above (`69`; `124`/`125`; `66`/`82`/`131`/`132`; `108`; `109`/`110`) sums to ten distinct opcodes; the opcode list was always right, only the running tally said "nine".

> **QUIRK — the engine is *not* in the opcode byte.** None of the four CoreV2 encoders assigns the engine. They *read* `EngineInfo` at `inst+144` (set by an upstream scheduling/placement pass) only for the per-engine census and stream binding: Pool→Pool engine, Max/MaxIndex→DVE(5), TensorReduce→its engine field. A reimplementer who tries to derive the engine from the opcode will fail — the opcode tells you the *micro-op*, the engine binding is a separate scheduling decision carried on the IR node. The same fact holds for the [PE engine](pe-engine.md) (`getDefaultEngine`), so the pattern is consistent across the backend.

### The Pool / Reduce Semantic Split

`PoolFunctionType` lives **only** on `InstPool`. Sum-style reductions, min reductions, argmax, and partition/channel reduction are *not* pool functions — they are separate opcodes (`TensorReduce` / `Max` / `MaxIndex`). This is the single most important framing for the whole leg:

| Source intent | Op | Why |
|---|---|---|
| 2-D max/avg pooling (sliding window) | `InstPool` | windowed; `PoolFunctionType` selects Max vs Avg |
| sum / min / and / or / xor over innermost dims | `InstTensorReduce` (free-axis) | `AluOpType` selects the operator; `AxisListType` selects how many dims |
| reduce across partitions (channel collapse) | `InstTensorReduce` (cross-lane) | a *different* datapath of the same op |
| 8 largest values per partition | `InstMax` | DVE 8-wide running max |
| positions of those 8 maxima | `InstMaxIndex` | DVE argmax, back-resolves indices |

> **GOTCHA — there is no `Sum` pool variant.** `PoolFunctionType` has exactly two members, `Max(0)` and `Avg(1)` (CONFIRMED, `libBIR` `PoolFunctionType2string` @ `0x4010e0`). A reimplementer driving off a "max/avg/sum" pool enum will emit a third byte the encoder never produces and the verifier rejects (`"has unimplemented Pooling type"`). Sum is a `TensorReduce` with `reduce_op = add`; min is `reduce_op = min`. The pool engine *only* averages and maxes.

`InstPool` also hard-codes a **2-innermost-dim window** — its `AxisListType` is ignored on the Pool path. `TensorReduce`, by contrast, *reads* `AxisListType` to choose how many innermost dims to collapse. So "how many dimensions are reduced" is **dynamic for `TensorReduce`, fixed at 2 for `InstPool`**.

---

## InstPool — Windowed Max/Avg (opcode 69)

### Purpose

`InstPool` is 2-D sliding-window pooling: for each output position it reduces a rectangular window of the input either by maximum or by mean. It is the only op on this leg that carries a `PoolFunctionType`, and the only one whose window is *implicit in the access-pattern geometry* rather than a discrete field.

### Operand Roles

| Role | What it is | Binding | Wire slot | Confidence |
|---|---|---|---|---|
| **in** (data) | the tensor being pooled (5-dim band-pool AP on the Avg path) | `arg0` | `+0x0C` `TENSOR4D` | CONFIRMED |
| **out** (pooled) | the reduced output | `out0` | `+0x2C` `TENSOR4D` | CONFIRMED |

The wire struct is `S4D4_PL_STRUCT` — a 4-dim source + 4-dim destination descriptor pair (`PL` = Pool).

### The Window-as-Reciprocal Fold

This is the headline mechanism. **There is no explicit window or stride field.** The window is implicit in the input AP:

```text
WINDOW SIZE  N = Pattern[3].num × Pattern[4].num
             (the product of the two innermost AP-dim element counts — a 2-D window;
              the Average path asserts in0.size() == 5, the band-pool AP shape)

WINDOW STRIDE  carried inside the TENSOR4D descriptor as Pattern[*].step
               (rendered by assignAccess<TENSOR4D> into +0x0C — never a discrete field)

SCALE   Max pool: bundle[+0x28] = 1.0f          (no divide)
        Avg pool: bundle[+0x28] = 1.0f / N      (the reciprocal window size)
```

For **average** pooling the encoder pre-computes `1/N` at encode time and writes it as a float constant. The silicon datapath then computes `Σwindow × (1/N)` — a sum plus a single multiply, never a hardware divide. The window *extent* is thus encoded **twice and redundantly**: once as the reciprocal constant `1/N` at `+0x28`, and once as the descriptor's innermost `(step, num)` pairs (which drive the actual iteration). For **max** pooling the scale is `1.0f` and the datapath ignores it.

> **QUIRK — fold the divide into the encoder, not the silicon.** The reason `1/N` is computed at encode time is that `N` is statically known from the AP geometry, so the divide can happen once on the host instead of every output element on the chip. A reimplementer who writes `N` into the bundle and divides on-device will produce numerically-correct but slower silicon, *and* will mismatch the reference model, which multiplies by the encoder-supplied reciprocal (CONFIRMED — `birsim` `visitInstPool` @ `0x1d7340`: `AvgPool out = sum × (1.0/window_N)`).

### Algorithm

```c
// CoreV2GenImpl::visitInstPool @ 0x1239e50
function visitInstPool(InstPool &I):
    pf = I.func                                 // PoolFunctionType @ BIR +0xF0

    setupHeader(bundle, /*opcode*/69)           // +0x00 = 0x45, +0x01 = 16
    setupSyncWait<S4D4_PL>(bundle, I); setupSyncUpdate<S4D4_PL>(bundle, I)

    bundle[+0x20] = dtypeTag(I.in0.Dtype)       // sub_120E650
    bundle[+0x21] = dtypeTag(I.out.Dtype)
    bundle[+0x22] = I.in0.Pattern[0].num        // lane / partition count
    bundle[+0x25] = 3                            // pool sub-mode (constant; meaning INFERRED)

    if pf == Max(0):                            // @ 0x123a068
        bundle[+0x24] = 1                        //   wire func byte (BIR enum +1 shift)
        bundle[+0x28] = 1.0f  (0x3F800000)       //   scale = 1.0, no divide
    elif pf == Avg(1):                          // @ 0x123a45c
        if I.in0.isTensorIndirect():            //   GUARD: Avg forbids indirect src
            reportError("POOL with Average Function doesn't support "
                        "TensorIndirect AP as src AP")
        assert I.in0.size() == 5                 //   the band-pool AP shape
        N    = I.in0.Pattern[3].num * I.in0.Pattern[4].num
        bundle[+0x24] = 2                        //   wire func byte
        bundle[+0x28] = 1.0f / (float)N          //   movss — the reciprocal fold (@ 0x123a977)
    else:
        reportError("has unimplemented Pooling type")   // still emits

    assignAccess<TENSOR4D>(bundle+0x0C, I.in0)   // in descriptor (carries window stride)
    assignAccess<TENSOR4D>(bundle+0x2C, I.out)   // out descriptor
    emitBundle(bundle)                            // append to Pool stream + fwrite 0x40 bytes
```

> **NOTE — the function byte is `BIR enum + 1`.** `PoolFunctionType` is `{Max=0, Avg=1}` in BIR, but the wire byte at `+0x24` is `Max→1`, `Avg→2` (a `+1` shift; CONFIRMED — `movb $0x1,0x24` / `movb $0x2,0x24`). A reimplementer copying the raw enum value to the wire produces a `0`/`1` pair the silicon reads as the wrong function. The shift is deliberate — wire value `0` is reserved.

### Field Map (opcode 69)

| Offset | Field | Source | Confidence |
|---|---|---|---|
| `+0x00` | opcode = 69 (`0x45`) | `setupHeader` seed | CONFIRMED |
| `+0x01` | hdr len/version = 16 | `setupHeader` (`0x10`) | CONFIRMED |
| `+0x04…` | sync-wait / update region | `setupSyncWait/Update<S4D4_PL>` | CONFIRMED |
| `+0x0C` | **in** `TENSOR4D` descriptor | `assignAccess<TENSOR4D>(in0)` | CONFIRMED |
| `+0x20` | in dtype wire-tag | `sub_120E650(in0.Dtype)` | CONFIRMED |
| `+0x21` | out dtype wire-tag | `sub_120E650(out.Dtype)` | CONFIRMED |
| `+0x22` | lane / partition count | `in0.Pattern[0].num` | CONFIRMED |
| `+0x24` | **pool function** | `Max→1, Avg→2` (BIR `func+1`) | CONFIRMED |
| `+0x25` | mode byte = 3 | constant pool sub-mode | CONFIRMED value / INFERRED meaning |
| `+0x28` | **scale = 1/window** | `Max→1.0f`, `Avg→1.0f/(p3.num·p4.num)` | CONFIRMED |
| `+0x2C` | **out** `TENSOR4D` descriptor | `assignAccess<TENSOR4D>(out)` | CONFIRMED |

### Datapath Semantics (reference model)

```c
// birsim visitInstPool @ 0x1d7340 (CONFIRMED)
window = AP[size-1].num * AP[size-2].num         // two innermost dims; AxisListType IGNORED
out_count = window_count * getNumElementsPerPartition(inAP)

if func == Avg(1):                               // AvgPool
    sum = horizontal_add_4wide(window) + scalar_tail(<=3)   // SSE _mm_unpackhi/_mm_shuffle
    out = sum * (1.0 / window_N)                 // the +0x28 reciprocal feeds this multiply
else:                                            // MaxPool (func != 1)
    acc = -FLT_MAX
    for x in window: acc = fmaxf(x, acc)
    out = acc                                    // +0x28 ignored on the Max path
```

---

## InstTensorReduce — The Two Reduce Datapaths

### Purpose

`InstTensorReduce` is the general N-dimensional reduce: it collapses one or more innermost access-pattern dimensions (or the partition axis) under a chosen ALU operator. It is the home of sum, min, and bitwise reductions — everything the pool engine does *not* do. Its defining feature is that it is **two physically distinct silicon datapaths sharing one BIR op**, and the fork is on a single enum field.

### Operand and IR Fields

The BIR node carries: `reduce_op@+0xF0` (`AluOpType`), `axis@+0xF4` (`AxisListType`), an `apply_transpose` `MaybeAffine{tag@+0xF8, val@+0x140}`, and a `negate` `MaybeAffine{val@+0x120}`. **There is no `reduce_cmd` field** on `InstTensorReduce` — see [The Dormant ReduceCmdType](#the-dormant-reducecmdtype).

### The Axis Fork

The encoder reads `axis@+0xF4` and forks the **whole descriptor template and the whole opcode** (not just one bit):

```text
axis ∈ {0=X, 1=XY, 2=XYZ, 3=XYZW}   (value ≤ 3)  →  FREE-AXIS  (TR) datapath
axis ∈ {4=XYZWC, 5=C}                            →  CROSS-LANE (CR) datapath
other                                            →  assert
```

This is the silicon realization of "`axis = C` ⇒ cross-lane reduce." The two datapaths differ in opcode, in how the reduce command is encoded, and in what is legal (negate, indirection):

| Facet | Free-axis (TR) | Cross-lane (CR) |
|---|---|---|
| Reduces | innermost loop dim(s) `{X,Y,Z,W}` | partition / channel axis |
| Opcode | `124` (float) / `125` (bit-vector) | `66` / `82` / `131` / `132` |
| Reduce command | full `AluOp → ALU_OP` convert @ `+0x24` | inline 6-entry switch @ `+0x23` |
| Axis byte | `AxisListType + 2` @ `+0x25` | `CROSS_LANE` flag @ `+0x24` |
| Negate | post-reduce negate @ `+0x23` | **none** (verifier-banned) |
| Mean (Avg) | datapath `1/N` functor | `1/N` @ `+0x28` |
| Indirection | allowed only for `AxisListType::X` | not yet ISA-legal |

### AxisListType — How Many Dims

`AxisListType` (`libBIR` `AxisListType2string` @ `0x4011b0`, 6 members) names the reduced axes innermost-first. The canonical AP axis order is `[W,Z,Y,X]` = AP index `[0,1,2,3]`, and the **reduce-count = `AxisListType` value + 1**:

```text
X(0)     → reduce 1 dim   {X}            (innermost only)
XY(1)    → reduce 2 dims  {X,Y}
XYZ(2)   → reduce 3 dims  {X,Y,Z}
XYZW(3)  → reduce 4 dims  {X,Y,Z,W}      (W/partition reduced ONLY here on the TR path)
XYZWC(4) → cross-lane (partition/channel) collapse, flag OFF
C(5)     → cross-lane collapse, flag ON
```

`reduce_extent = Π` of the innermost reduce-count AP-dim element counts. On the free-axis path the axis *number* is encoded at `+0x25` via `sub_120EAB0(axis) = (axis > 3 ? assert : axis + 2)` ⇒ `X→2, XY→3, XYZ→4, XYZW→5`. The helper asserts on `axis > 3` precisely because cross-lane axes 4/5 take the CR path and never reach this byte (CONFIRMED).

### The Reduce-Command — Two Op-Byte Channels

The two datapaths encode the reduce *operator* completely differently — this is the second headline of the page.

**Free-axis (TR)** uses the full `AluOp → ALU_OP` wire convert:

```c
bundle[+0x24] = CoreV2Convert::convert(I.reduce_op)   // sub_12039C0
// AluOpType (33 members) → ISA ALU_OP byte:
//   0..18 identity; the comparison family REORDERS
//     (19→24, 20→19, 21→20, 22→22, 23→21, 28→29, 29→25);
//   mod_int(32) → 0xC8 (int32 ext band);
//   average(24) / elemwise_mul(25) → ERR on this generic path (avg folds via +0x28)
```

**Cross-lane (CR)** uses a *separate 6-entry* reduce-cmd at `+0x23` — an inline switch on `reduce_op`, **not** the full `AluOp` table:

```c
switch (I.reduce_op):                  // inline; sourced into mov %al,0x23(%r13)
    case add(4):         bundle[+0x23] = 0
    case average(24):    bundle[+0x23] = 1
    case max(8):         bundle[+0x23] = 2
    case bitwise_or(11): bundle[+0x23] = 3
    case bitwise_and(10):bundle[+0x23] = 4
    case bitwise_xor(12):bundle[+0x23] = 5
    default: assert false  // + AluOpType2string in the message
```

> **GOTCHA — only six operators are legal cross-lane.** The CR path accepts exactly `{add, average, max, or, and, xor}`. A `min`, `subtract`, or comparison cross-lane reduce — perfectly legal on the free-axis path via the full `AluOp` convert — hits the `default: assert "false"`. A reimplementer must gate the operator set by datapath: the free-axis path takes the whole `AluOpType` table; the cross-lane path takes only these six.

### Negate / Abs / Accumulate Modifiers

- **Negate** is a *post-reduce* negate at `+0x23` on the **TR path only**, sourced from `InstTensorReduce.negate@+0x120`. The CR path has no negate slot — matching the verifier ban `"Cross-lane-reduce cannot perform negate"` (string CONFIRMED). In the datapath, negate is applied to the reduced scalar **after** the fold, **before** the optional accumulate.
- **Abs / abs_max / abs_min** are *not* separate modifier bits — they are ordinary `AluOpType` values (`abs=29, abs_max=30, abs_min=31`) that reach the datapath as reduce ops via `sub_12039C0` (`abs→0x19, abs_max→0x20, abs_min→0x21`). An abs-reduce is simply `reduce_op ∈ {29,30,31}` on the TR path. (STRONG.)
- **Accumulate** flows through `EngineAccumulationType` (the live RMW path), *not* through `ReduceCmdType`.

### The Dormant ReduceCmdType

> **CORRECTION (M09-C1) — `reduce_cmd` is NOT a `bir::ReduceCmdType` field on `InstTensorReduce`.** The enum `ReduceCmdType {Idle, Reset, Reduce, ResetReduce}` exists in `libBIR` with full `2string`/`string2`/`from_json`/`to_json` bodies, but it has **no `Instruction` consumer** — `InstTensorReduce::readFields` @ `0x41a7c0` reads only `{axis, op, negate, apply_transpose}`. The live reduce-command modifiers surfaced at the wire are the inline 6-entry CR switch (`+0x23`) and the TR negate (`+0x23`) shown above — *not* the `ReduceCmdType` enum, which is a sim/runtime RMW-phase enum only. The JSON key `"reduce_cmd"` that *does* appear (on `InstExponential`/`InstRangeSelect`, **not** `TensorReduce`) deserializes to `EngineAccumulationType`. A reimplementer wiring a `ReduceCmdType` field into `TensorReduce` encodes dead state. (D-D02 C1, re-affirmed.)

### The Cross-Lane Opcode Fork

The cross-lane opcode forks **four ways** on `(isBitVecInstruction, apply_transpose-tag@+0xF8)`:

```text
non-bitvec, tag==0 → 66      bitvec, tag==0 → 82
non-bitvec, tag==1 → 131     bitvec, tag==1 → 132
```

(CONFIRMED — `add $0x83,%ebx` = 131 / `add $0x84,%ebx` = 132 on the transpose arm; `sub $0x7d` = 125 / `sub $0x7c` = 124 base seeds on the TR arm.) The `CROSS_LANE` flag at `+0x24` is `axis==5(C)→1` (on), `axis==4(XYZWC)→0` (off), else `assert "getAxis() == bir::AxisListType::XYZWC"`. Indirection on the cross-lane path is gated by the TODO string `"TODO: support CROSS_LANE_REDUCE with TensorIndirect AP once ISA supports it"` — not yet ISA-legal.

### Algorithm

```c
// CoreV2GenImpl::visitInstTensorReduce @ 0x12383a0
function visitInstTensorReduce(InstTensorReduce &I):
    axis = I.axis                                // AxisListType @ +0xF4
    bitvec = isBitVecInstruction(I)

    if axis <= XYZW(3):                          // ---- FREE-AXIS (TR) ----
        opcode = 124 + bitvec                    // 124 float / 125 bitvec
        setupHeader(bundle, opcode)
        bundle[+0x23] = I.negate                 // post-reduce negate
        bundle[+0x24] = CoreV2Convert::convert(I.reduce_op)   // full AluOp wire byte
        bundle[+0x25] = axis + 2                 // sub_120EAB0: X→2 … XYZW→5
    else:                                        // ---- CROSS-LANE (CR) ----
        tag = I.apply_transpose_tag              // +0xF8
        opcode = select(bitvec, tag, {66,82,131,132})
        setupHeader(bundle, opcode)
        bundle[+0x23] = crossLaneReduceCmd(I.reduce_op)   // 6-entry switch (add0..xor5)
        if axis == C(5):      bundle[+0x24] = 1  // CROSS_LANE on
        elif axis == XYZWC(4):bundle[+0x24] = 0  // CROSS_LANE off
        else: assert "getAxis() == bir::AxisListType::XYZWC"
        if I.reduce_op == average(24):
            bundle[+0x28] = 1.0f / (in.Pattern[?].num * getNumElementsPerPartition(in))

    bundle[+0x20] = dtypeTag(I.in.Dtype)
    bundle[+0x21] = dtypeTag(I.out.Dtype)
    bundle[+0x22] = I.in.Pattern[0].num          // lane count
    assignAccess<TENSOR4D>(bundle+0x0C, I.in)
    assignAccess<TENSOR4D>(bundle+0x2C, I.out)
    emitBundle(bundle)
```

### Field Maps

**Free-axis (TR, opcode 124 float / 125 bit-vector):**

| Offset | Field | Source | Confidence |
|---|---|---|---|
| `+0x00` | opcode 124/125 | `124 + isBitVecInstruction` | CONFIRMED |
| `+0x0C` | in `TENSOR4D` | `assignAccess(in)` | CONFIRMED |
| `+0x20` | in dtype tag | `sub_120E650(in.Dtype)` | CONFIRMED |
| `+0x21` | out dtype tag | `sub_120E650(out.Dtype)` | CONFIRMED |
| `+0x22` | lane count | `in.Pattern[0].num` | CONFIRMED |
| `+0x23` | **negate** | `negate@+0x120` | CONFIRMED |
| `+0x24` | **reduce-op (ISA)** | `sub_12039C0(op)` — full AluOp wire | CONFIRMED |
| `+0x25` | **axis (ISA)** | `sub_120EAB0(axis)` = `AxisListType + 2` | CONFIRMED |
| `+0x2C` | out `TENSOR4D` | `assignAccess(out)` | CONFIRMED |

**Cross-lane (CR, opcode 66/82/131/132):**

| Offset | Field | Source | Confidence |
|---|---|---|---|
| `+0x00` | opcode 66/82/131/132 | `f(isBitVec, apply_transpose@+0xF8)` | CONFIRMED |
| `+0x0C` | in `TENSOR4D` | `assignAccess(in)` | CONFIRMED |
| `+0x20` | in dtype tag | `sub_120E650(in.Dtype)` | CONFIRMED |
| `+0x21` | out dtype tag | `sub_120E650(out.Dtype)` | CONFIRMED |
| `+0x22` | lane count | `in.Pattern[0].num` | CONFIRMED |
| `+0x23` | **reduce-cmd (6-set)** | inline switch `op→{add0/avg1/max2/or3/and4/xor5}` | CONFIRMED |
| `+0x24` | **CROSS_LANE flag** | `axis==5→1`, `axis==4→0` | CONFIRMED |
| `+0x28` | mean `1/N` | avg → `1.0f/(p?.num·numElemPerPart)` | CONFIRMED |
| `+0x2C` | out `TENSOR4D` | `assignAccess(out)` | CONFIRMED |

### Datapath Semantics (reference model)

```c
// birsim visitInstTensorReduce @ 0x1d7720 / applyEngReduce<float> @ 0x225600 (CONFIRMED)
for k in 0..n_out:
    acc = in[base]
    if is_average: acc = acc / N                 // divide-first (mean)
    for j in 1..extent: acc = reduceFn(acc, in[base + j*stride])
    if has_accumulate:                           // EngineAccumulationType (the LIVE acc path)
        if negate: acc = -acc
        acc = accFn(acc, out[k])                  // fold into existing out
    elif negate: acc = -acc                       // negate AFTER reduce, BEFORE acc
    out[k] = acc
// reduceFn = ts_ops[op] (FP32) or ts_int_ops[op] (INT32); average(24) binds the inline
//   1/N functor sub_1A1DB0 (acc + x/N). apply_transpose: input is FP32-cast and 32-lane
//   partition-transposed before the reduce. Indirection allowed only for AxisListType::X.
```

---

## InstMax / InstMaxIndex — The 8-Wide DVE Argmax

### Purpose

`InstMax` and `InstMaxIndex` are the DVE engine's two-stage **argmax**: `InstMax` produces the 8 largest *values* per partition; `InstMaxIndex` back-resolves their *positions*. The "8" is the silicon DVE lane width — neither op carries a width attribute, and both **hard-assert** `out == 8`. This is the leg's clearest example of one BIR op fanning out to multiple wire bundles.

### InstMax — MAX8 (opcode 108)

`InstMax` is an 8-lane running max on the DVE. It is a header-only instruction (no width field); the output is the 8 running per-partition maxes, written as a **compact 1-D mem-pattern**, not a `TENSORxD` descriptor — because the output is always exactly the 8-slot descending max vector (a per-partition 8-slot insertion-sort buffer).

```c
// CoreV2GenImpl::visitInstMax @ 0x1273120 (opcode 108 = 0x6c)
function visitInstMax(InstMax &I):
    assert I.numOutPerPartition == 8                     // THE 8-wide output constraint
    assert 8 <= I.numInPerPartition <= 16384
    assert I.in0.size() <= 5 and <= 3                    // two-tier dim cap
    assert I.in0.getNumPartitionsAccessed() == I.out.getNumPartitionsAccessed()
    // GUARD: "TODO: support MAX8 with TensorIndirect AP"  (out indirect not yet legal)

    setupHeader(bundle, 108)
    assignAccess<TENSOR4D>(bundle+0x0C, I.in0)            // only the DATA input is a full 4D desc

    last = I.out.Pattern[last]                            // the 8-slot output mem-pattern:
    bundle[+0x30] = engRelAddr + (last.num-1)*last.step*dtypeBytes[dtype]  // dst start_addr
    bundle[+0x34] = -(last.step)                          // dst step (negated)
    bundle[+0x36] = 0                                     // 16-bit flag word
    bundle[+0x38] = last.num   (= 8)                      // dst num_elem
    bundle[+0x3A] = 1                                     // 16-bit flag word
    emitBundle(bundle)
```

`dtypeBytes` comes from the `.rodata` stride LUT @ `0x1DF59E0` — a qword table decoding to `[1,1,2,1,1,1,1,1,4,4,2,2,2,2,4,4,4,4,8,8]` (CONFIRMED byte-exact). The `+0x36`/`+0x3A` flag words (`0`/`1`) are INFERRED to be the mem-pattern `is_immediate`/`dim-valid` flags.

### InstMaxIndex — FIND_INDEX8 (opcodes 109 + 110)

`InstMaxIndex` is the index back-resolution pass, and its defining structural fact is the **1→2 bundle split**:

> **QUIRK — one `InstMaxIndex` emits TWO co-issued DVE bundles, and the split is in the encoder.** `visitInstMaxIndex` @ `0x1254650` emits opcode **109** (`MATCH_VALUE_LOAD`-class — loads the 8 maxVals to match against) *and* opcode **110** (the index-emit micro-op — scans the data array and writes the first-match index). Both `movl $0x6d` and `movl $0x6e` seeds are present in the body (CONFIRMED — at `0x12546d3` / `0x1254732`, plus 8 seed/fwrite sites total). This is **distinct** from `MatchReplace8`, whose 1→2 split happens at KLR→BIR codegen; here the split is purely in the CoreV2 silicon encoder. A reimplementer who emits one bundle per `InstMaxIndex` produces a half-instruction the DVE cannot execute.

```c
// CoreV2GenImpl::visitInstMaxIndex @ 0x1254650
function visitInstMaxIndex(InstMaxIndex &I):
    // OPERANDS: maxVals (the 8 maxes to locate), data (the array),
    //           uint32 indices-out (preset to 0xFFFFFFFF = not-found sentinel)
    assert I.numInPerPartition == 8                       // the maxVals input is exactly 8
    assert 8 <= I.data.numInPerPartition <= 16384
    assert I.numOutPerPartition == 8
    assert I.out.size() == 2 and I.in.size() <= 5 and <= 3 and I.inPhy != nullptr
    assert I.in.getNumPartitionsAccessed() == I.out.getNumPartitionsAccessed()

    // ---- bundle 1: opcode 109 — MATCH_VALUE_LOAD (load the 8 maxVals) ----
    setupHeader(b1, 109); setupSync<S2_BN>(b1, I)
    assignAccess<TENSOR4D>(b1+0x0C, I.data); assignAccess<TENSOR2D>(b1+0x30, I.maxVals)
    emitBundle(b1)

    // ---- bundle 2: opcode 110 — scan + emit first-match uint32 index ----
    setupHeader(b2, 110); setupSync<S4D2_BN>(b2, I)
    assignAccess<TENSOR4D>(b2+0x0C, I.data); assignAccess<TENSOR2D>(b2+0x30, I.indices)
    emitBundle(b2)
```

Each bundle pairs a 4D **data** descriptor at `+0x0C` with a 2D **index/maxVals** descriptor at `+0x30` (the 8-wide vector). The index output dtype is `uint32`, flowing through the `+0x21` dtype tag. The DVE machine: bundle 109 loads the 8 max *values* (computed by a prior MAX8 / opcode-108 pass) into the match register; bundle 110 scans the data array per partition and, for each of the 8 maxVals, writes the index of the **first** array element that equals it — elements with no match keep the `-1` sentinel. (STRONG — the value/index pairing is the MAX8→FIND_INDEX8 two-stage argmax.)

### Max / Index Relationship

| Op | Input → Output | Output encoding |
|---|---|---|
| MAX8 (108) | data → 8 descending running maxes | 1-D mem-pattern @ `+0x30`/`+0x34`/`+0x38` |
| FIND_INDEX8 (109+110) | (maxVals[8], data) → uint32 index[8] | 4D-data @ `+0x0C` + 2D-index @ `+0x30`, two co-issued bundles, `-1` sentinel |

The 8-wide width is the DVE silicon lane count, hard-asserted (`out == 8`) on both ops.

---

## The Shared 64-Byte Bundle Prologue

Every Pool/reduce/Max/MaxIndex bundle is 64 bytes, emitted via the **same** skeleton (CONFIRMED, the N11 common ABI shared with the rest of the backend):

```text
setupHeader @ 0x1172120  → bundle[0] = opcode byte, bundle[1] = 16 (0x10 hdr len/ver),
                           bundle[2..3] = 0   (16-byte shim; NO engine field)
setupSyncWait<…_STRUCT>(bundle, inst)    (semaphore wait region; SEM_GE_IMM/REG)
setupSyncUpdate<…_STRUCT>(bundle, inst)  (semaphore update region)
op-specific payload (rows +0x20…)
assignAccess<NEURON_ISA_TPB_TENSORxD>(AP → descriptor @ +0x0C / +0x2C / +0x30)
sub_12095A0(this+64, &bundle)            (append to engine compound stream)
fwrite(bundle, 1, 0x40, bin)
census: ++map<opcode> ; ++DenseMap<EngineInfo> ; vector<Instruction*>.push_back
```

The descriptor-template family (named by the `setupSync*` tag) is: `Pool→S4D4_PL`, free-axis reduce`→S4D4_TR`, cross-lane reduce`→S4D4_CR`, `Max→S4D2_BN`, MaxIndex `op109→S2_BN` / `op110→S4D2_BN`. `CodeGenMode` on `*(this+156)` dispatches: `1` = GENERATE_ISACODE (real bytes), `2` = RUN_ISA_CHECKS (stack scratch, no `fwrite`), `0` = COLLECT_OPCODES (record opcode only).

### Shared Field-Build Helpers

| Helper | Address | Role | Confidence |
|---|---|---|---|
| `sub_120E650` | `0x120E650` | `Dtype → NEURON_ISA_TPB_DTYPE` wire-tag (reads `AP.Dtype@+0x30`) → `+0x20`/`+0x21` | CONFIRMED |
| `sub_12039C0` | `0x12039C0` | `CoreV2Convert::convert(AluOpType) → ISA ALU_OP` byte → TR reduce-op `+0x24` | CONFIRMED |
| `sub_120EAB0` | `0x120EAB0` | `AxisListType → ISA axis` byte = `(a>3 ? assert : a+2)` → TR axis `+0x25` | CONFIRMED |
| `sub_12095A0` | `0x12095A0` | append bundle ptr into the engine compound stream | CONFIRMED |
| `assignAccess<TENSOR4D>` | `0x603EE0` | AP → in-bundle wire descriptor (`<TENSOR2D>` sibling) | CONFIRMED |

---

## The ALU Substrate

The reduce/pool operators draw from four global functor maps, built once by `sub_133280` @ `0x133280` (CONFIRMED):

| Map | Domain | What it holds |
|---|---|---|
| `ts_ops` | FP32 (21 ops) | the float reduce/pool/TS functors |
| `ts_int_ops` | INT32 | bit/shift/saturating-int functors |
| `ta_ops` | INT64 / address | the Pool/address-ALU long path (`bir::IsAluOpOnPool`) |
| `ts_int_cmp_ops` | PREDICATE | the compare family (RangeSelect / AffineSelect) |

The reduce-op *semantics* (commutativity, `acc = LHS`) are exactly these functor bodies. `add/max/min/and/or/xor/abs_max/abs_min/average` are commutative; `subtract/divide/shift/pow/mod` and the `gt/ge/lt/le` compares are order-sensitive — which is **why** the CoreV2 ALU wire convert (`sub_12039C0`) reorders the comparison band: the wire opcode space groups the order-sensitive compares so the silicon ALU can apply the LHS/RHS convention consistently.

---

## Gaps and Confidence

- **CONFIRMED (re-disassembled, `libwalrus.so` cp310):** all four visitor addresses; Pool opcode `0x45`, func byte `Max→1`/`Avg→2` @ `+0x24`, mode `3` @ `+0x25`, scale `1.0f`/computed `1/N` @ `+0x28`; TensorReduce opcode arithmetic (`0x83`/`0x84` = 131/132, `0x7c`/`0x7d` = 124/125), the `+0x20..+0x23` byte stores, the CR axis fork (`cmp $0x5→1`, `cmp $0x4→0`), the CR mean `movss` @ `+0x28`; Max opcode `0x6c` with the `out==8` / `[8,16384]` asserts and `+0x36`/`+0x3A` flag stores; MaxIndex **both** `0x6d` and `0x6e` seeds; the dtype stride LUT @ `0x1DF59E0`; `PoolFunctionType = {Max, Avg}` only. All headline strings (`"has unimplemented Pooling type"`, `"in0.size() == 5"`, `"Cross-lane-reduce cannot perform negate"`, `"getAxis() == bir::AxisListType::XYZWC"`, `"CROSS_LANE_REDUCE"`, `"MATCH_VALUE_LOAD"`) verbatim.
- **STRONG:** MaxIndex op109 = `MATCH_VALUE_LOAD`-class (string + single-2D `S2_BN` descriptor); the two bundles are co-issued find-index8 micro-ops; the MAX8→FIND_INDEX8 two-stage argmax; the engine bindings (DVE(5) for Max/MaxIndex, Pool engine for Pool, read from `inst+144`); the `1/N` Avg-fold rationale (sum + single multiply, no on-silicon divide).
- **INFERRED:** the `+0x36`/`+0x3A` Max mem-pattern flag words (`0`/`1`) as `is_immediate`/`dim-valid` flags (byte values confirmed, meaning inferred); the Pool `+0x25` "mode = 3" sub-mode (value confirmed, meaning inferred).
- **SPECULATIVE / not field-walked:** the exact bit layout *within* the `S4D4`/`S4D2`/`S2` sync-command regions and the `TENSORxD` descriptor interiors (rendered by `assignAccess`/`setupSync*`, the shared N-strand common code, out of this leg's scope — see [ISA: Pool encoding](../isa/pool-encoding.md)).

## Related Components

| Name | Relationship |
|---|---|
| [PE Engine](pe-engine.md) | the sibling engine page (1.08) — same 64-byte N11 ABI, same `inst+144` engine binding; PE is the matmul leg, Pool is the reduce leg |
| `InstMaxIndexAndMatchReplace` (IT91, CoreV3) | gen3 fusion that folds the MaxIndex 1→2 split + MatchReplace into one DVE pass — out of CoreV2 scope |
| `InstRangeSelect` (IT63), `InstNonzeroWithCount` (IT104) | gen3-new DVE primitives that share the ALU substrate above |

## Cross-References

- [ISA: Pool Encoding](../isa/pool-encoding.md) — the bit-level 64-byte bundle layout this page describes functionally (2.12): the `S4D4`/`S4D2`/`S2` descriptor interiors and the dtype/AluOp/Axis wire tables.
- [PE Engine](pe-engine.md) — the systolic matmul array (1.08); the sibling per-engine functional model and the shared bundle-prologue ABI.
- [SBUF / PSUM Bank Geometry](sbuf-psum-geometry.md) — the partition/free-axis memory model the `Pattern[0]` lane count and the 5-dim band-pool AP shape live in.
- [BIR Instruction Hierarchy](../bir/) — `InstPool` / `InstTensorReduce` / `InstMax` / `InstMaxIndex` as `bir::Instruction` nodes (Part 7), the dormant `ReduceCmdType` enum, and the `readFields` accessors.
- [NKI Reduction Kernels](../nki/) — `ReduceWindow` / `SelectAndScatter` (6.8.5) and how a front-door pooling/topk kernel lowers onto this leg.
