# getSequencerType — SCS / TAC / TEC Engine Selection

> *Every function address, enum value, attribute-string byte pattern, and routing constant on this page was read from `libtpu.so` in the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`; build `libtpu_lts_20260413_b_RC00`) — from the decompiled C++ of the named functions, the demangled symbol table, the embedded proto-descriptor strings, and the `.rodata` jump table at `off_22010DE0`. Other versions differ.*

## Abstract

The SparseCore back-end places every lowered op onto exactly one of the three SparseCore sub-engines — **SCS** (scalar control), **TAC** (tile-access / DMA), or **TEC** (vector compute) — and the binary records this placement as a single per-function string attribute named `sc.sequencer`. There is no monolithic "selector" that reasons about an op and returns an engine; instead the decision is split across three layers that this page documents end to end:

1. A **policy classifier**, `GetTransferKind` (`@0x1351b140`), that decides whether an off-tile data movement is a *Stream* (indirect gather/scatter) or a *DMA* (bulk contiguous) transfer, keyed on the source/destination memory-space pair plus one SparseCore capability bit.
2. A **region outliner** (`LowerSequencerFunctionsPass` / `OutlineSequencerFunction`) that stamps each per-engine outlined `func.func` with `sc.sequencer = "scs"` / `"access"` / `"execute"`.
3. A trivial **attribute accessor**, `LowerMemrefToMlo::getSequencerType` (`@0x13507760`), that reads that string back so later passes pick the matching per-engine bundle codec (`SparseCoreScsCodecBase` / `…TacCodecBase` / `…TecCodecBase`).

Sitting underneath all of this is the `tpu::TpuSequencerType` enum and its `TpuSequencerTypeToString` jump table — the runtime numbering used to size per-engine resource arrays — which is **off by one** from the codec template-parameter numbering. This page reconciles both.

The reimplementation contract:

- **The op-level engine tag is a string, not a number.** Every op's engine membership is the `sc.sequencer` StringAttr on its enclosing outlined function — one of exactly three byte-confirmed values `"scs"` / `"access"` / `"execute"`. A reimplementer must route on the string; the two numeric `TpuSequencerType` enums never appear *at the op*.
- **`getSequencerType` is an accessor, not a decision.** `LowerMemrefToMlo::getSequencerType(Operation&)` returns `optional<StringRef>` — it reads `sc.sequencer` (via inherent-attr then dictionary-attr fallback) and returns its value, or `nullopt`. The decision was made upstream by `GetTransferKind` + outlining.
- **Stream-vs-DMA is decided by memory spaces + one capability bit.** `GetTransferKind` normalizes each `mlir::sparse_core::MemorySpace`, jump-tables on the source space, and sets *kStream* only when the destination space is in the HBM/SPMEM/TILE_SPMEM gather set (bitmasks `0x210018` / `0x210004`) and the target reports the SparseCore-variable capability (`vtable[+0xa0]`, =0 in this wheel); otherwise *kDma*, with an `InvalidArgument` diagnostic for an illegal pair.
- **Two `TpuSequencerType` enums, off by one.** The runtime proto numbering (the `TpuSequencerTypeToString` table index) is `{INVALID=0, TC=1, BARNA=2, BARNA_ADDR=3, SCS=4, TAC=5, TEC=6, …}`; the codec `EncoderBase` non-type template parameter is `{SCS=3, TAC=4, TEC=5}`. Trillium carries codec params 3 and 5 only — its `SparseCoreTacCodecBase` is entirely absent.

| | |
|---|---|
| **What it is** | The SparseCore op→engine assignment mechanism (SCS / TAC / TEC) |
| **Op-level tag** | `sc.sequencer` StringAttr (12-char name); values `"scs"` / `"access"` / `"execute"` |
| **Accessor** | `LowerMemrefToMlo::getSequencerType` `@0x13507760` → `optional<StringRef>` |
| **Policy classifier** | `xla::tpu::sparse_core::GetTransferKind` `@0x1351b140` (kStream vs kDma) |
| **Outliner** | `LowerSequencerFunctionsPass::runOnOperation` `@0x13532120`; `OutlineSequencerFunction` |
| **Enum** | `tpu::TpuSequencerType`; `TpuSequencerTypeToString` `@0x20b362e0` over `off_22010DE0` |
| **Runtime enum** | INVALID=0 · TC=1 · BARNA=2 · BARNA_ADDR=3 · **SCS=4 · TAC=5 · TEC=6** · SCv0=7/8 |
| **Codec template enum** | **SCS=3 · TAC=4 · TEC=5** (`EncoderBase<…, TpuSequencerType=N>`) |
| **Trillium** | No TAC: `gfc::…SparseCoreTacCodecBase` = 0 files; only codec params 3 & 5 |
| **Confidence** | CONFIRMED (function-byte / symbol / string-table anchored) unless a row says otherwise |

For the engine roles themselves see [SparseCore Overview](overview.md) and [Architecture](architecture.md); for what the outliner produces see [Region → Sequencer Outliner](region-to-sequencer-outliner.md).

---

## The Three Layers at a Glance

A single off-tile memory op (`tpu.enqueue_dma`, `tpu.enqueue_indirect_dma`) traverses three decision layers before it lands in a sequencer-specific bundle:

```text
  tpu.enqueue_dma / tpu.enqueue_indirect_dma        (TC-framework op, has memref operands)
            │
            ▼  LowerMemrefToMlo::lowerEnqueueDma         @0x135105a0
               LowerMemrefToMlo::lowerEnqueueIndirectDma @0x13511da0
            │
            ▼  getTransferKind<EnqueueDMAOp> @0x135114a0  →  GetTransferKind @0x1351b140
            │      (srcMemSpace, dstMemSpace, local/remote bits, capability)
            │
        ┌───┴────────────────────────┐
   kStream  ([result+8]=1)        kDma  ([result+8]=0 / InvalidArgument)
   gather/scatter slot            SparseCoreDma bulk slot
            │                              │
            ▼                              ▼
        emitted into a TileTask region (Access vs Execute)
            │
            ▼  LowerSequencerFunctionsPass / OutlineSequencerFunction
               stamps the outlined func.func with:
                 sc.sequencer = "scs"     (control sequencer  → SCS)
                 sc.sequencer = "access"  (tile-fetch / gather → TAC)   [VF/GL only]
                 sc.sequencer = "execute" (vector compute      → TEC)
            │
            ▼  later passes:
               LowerMemrefToMlo::getSequencerType(op) @0x13507760  → reads sc.sequencer
               → select per-engine codec: SparseCoreScsCodecBase / …Tac… / …Tec…
```

Layer 1 (`GetTransferKind`) answers *"is this a gather/scatter or a bulk copy?"*. Layer 2 (the outliner) answers *"which engine's program does this op belong to?"* and writes the answer as a string. Layer 3 (`getSequencerType`) is the read-back that drives codec selection. The three numeric `TpuSequencerType` values exist only at the *resource-sizing* layer (per-engine bundle-limit tables), never at the op.

> **NOTE — there is no `getSequencerType` that returns SCS/TAC/TEC from an op's opcode.** A reimplementer expecting a `switch(op.kind)` selector will not find one. The mapping op→engine is fully materialized as the `sc.sequencer` string on the *outlined function*, and ops inherit their engine from the function they were outlined into (enforced by the `ParentFuncHasCoreSequencerTypeAttribute` trait, below). `getSequencerType` only re-reads that string.

---

## Layer 3: The `getSequencerType` Accessor

The named function is the simplest of the three layers and the one this page is titled after. Decompiled (`@0x13507760`), it is a string-attribute getter returning a 17-byte `optional<StringRef>` (8-byte data ptr, 8-byte length, 1-byte present flag):

```c
// mlir::tpu::LowerMemrefToMlo::getSequencerType(this=result, op)  @0x13507760
//   result layout: [0]=StringRef.data, [8]=StringRef.size, [16]=present
optional<StringRef> getSequencerType(Operation& op) {
  // 1. fast path: inherent attr if the op's registered-info bit is set
  //    (*((u32*)op + 11) >= 0x1000000) AND getInherentAttr succeeds
  Attribute a = op.getInherentAttr("sc.sequencer", /*len=*/12);
  // 2. fallback: dictionary attr lookup on the op's attr dict (op + 56)
  if (!a) a = op.getDiscardableAttrDictionary().get("sc.sequencer", 12);
  // 3. must be a StringAttr (TypeID check against StringAttr::id)
  if (a && typeid(a) == StringAttr::id) {
    result = { StringAttr::getValue(a), /*present=*/1 };   // "scs"/"access"/"execute"
  } else {
    result = { /*present=*/0 };                            // nullopt
  }
  return result;
}
```

Two structural facts to preserve:

- **The attribute name is the 12-character string `"sc.sequencer"`** (the literal and length `12` are baked into the `getInherentAttr` call — `@0x85b7432` in the binary). The identical name+length pair appears in `HasCoreSequencerTypeAttribute` and `HasExecuteSequencerTypeAttribute` (below), confirming all three read the same attribute.
- **The two-step inherent→dictionary lookup** mirrors MLIR's split between *inherent* attributes (declared on the op definition) and *discardable* dictionary attributes. The accessor accepts either, so the outliner may attach `sc.sequencer` through whichever path is convenient for the op kind.

| Property | Value | Confidence |
|---|---|---|
| Function VA | `0x13507760` | CONFIRMED |
| Attribute name / length | `"sc.sequencer"` / 12 | CONFIRMED |
| Return type | `optional<StringRef>` (data, size, present-byte at +16) | CONFIRMED |
| Lookup order | inherent attr, then discardable dictionary attr | CONFIRMED |
| Type guard | `StringAttr` TypeID (`TypeIDResolver<StringAttr>::id`) | CONFIRMED |
| Returned values | `"scs"` / `"access"` / `"execute"` | CONFIRMED (string set) |

---

## The Attribute Values — Byte-Confirmed

The three legal `sc.sequencer` values are not just strings in a table — two of them are matched by dedicated predicate functions whose decompiled byte-comparisons pin the exact spelling and length. `ScDialect::HasCoreSequencerTypeAttribute` (`@0x14599ec0`) and `ScDialect::HasExecuteSequencerTypeAttribute` (`@0x1459a020`) both reuse the same `sc.sequencer` (len-12) accessor, then compare the StringAttr value against a length and a packed byte literal:

```c
// HasCoreSequencerTypeAttribute @0x14599ec0  — value == "scs"
if (len == 3)
  return ( (*(u16*)v ^ 0x6373) | (*(u8*)(v+2) ^ 0x73) ) == 0;   // 's','c' | 's'
//   0x6373 LE = bytes {0x73='s', 0x63='c'};  v[2]=0x73='s'  →  "scs"

// HasExecuteSequencerTypeAttribute @0x1459a020 — value == "execute"
if (len == 7)
  return ( (*(u32*)v ^ 0x63657865) | (*(u32*)(v+3) ^ 0x65747563) ) == 0;
//   0x63657865 LE = {'e','x','e','c'};  (v+3) 0x65747563 LE = {'c','u','t','e'}
//   overlapping at offset 3  →  "exec"+"cute" = "execute"
```

Decoding the little-endian masks:

| `sc.sequencer` value | Engine | Length | Byte-literal evidence | Predicate |
|---|---|:---:|---|---|
| `"scs"` | **SCS** (scalar control) | 3 | `0x6373`="sc", `0x73`="s" | `HasCoreSequencerTypeAttribute` `@0x14599ec0` |
| `"execute"` | **TEC** (vector compute) | 7 | `0x63657865`="exec", `0x65747563`="cute" | `HasExecuteSequencerTypeAttribute` `@0x1459a020` |
| `"access"` | **TAC** (tile-access / DMA) | 6 | — (no dedicated `Has*` predicate) | — |

> **GOTCHA — `"access"` has no dedicated predicate.** SCS and TEC each get a `Has…SequencerTypeAttribute` test because the SC-MLO pipeline operates on the SCS↔TEC boundary; the third value `"access"` (TAC) carries no `Has*` function in this binary. This is consistent with Trillium having dropped TAC altogether — on the newest gen the work that would land in an `"access"` function is folded into the `"execute"` function (see [TAC Engine](tac-engine.md) and [Region → Sequencer Outliner](region-to-sequencer-outliner.md)). A reimplementer that only models the SCS/TEC pair will produce correct Trillium code; the `"access"` value is needed only for Viperfish/Ghostlite.

### The parent-function trait

The `sc.sequencer` attribute lives on the *outlined function*, not on individual ops. Ops that require it to exist carry the `OpTrait::ParentFuncHasCoreSequencerTypeAttribute` trait (verified for `TileTaskWaitOp` at `@0x14689880`; the same trait is attached to the TileTask family). The shared check is `ParentHasSequencerTypeAttribute` (`@0x1353e980`):

```c
// ParentHasSequencerTypeAttribute @0x1353e980
//   walk parent ops until the enclosing LLVMFuncOp, then test BOTH predicates
for (op = start; ; op = op->getBlock()->getParentOp()) {
  if (!op) return false;
  if (typeid(*op) == LLVMFuncOp::id) break;          // reached the outlined func
}
h_core = HasCoreSequencerTypeAttribute(func);        // "scs"?
h_exec = HasExecuteSequencerTypeAttribute(func);     // "execute"?
// require BOTH predicate calls to have evaluated (present bit 0x100 set on each),
// then return their OR of the low (match) bits
return (h_core & 0x100) && (h_exec & 0x100) ? (h_core | h_exec) & 1 : false;
```

So the trait climbs the op tree to the enclosing `LLVM::LLVMFuncOp` and asserts that function is tagged either `"scs"` or `"execute"`. This is the binary's enforcement that *every TileTask op runs inside a function whose engine is known at verify time* — engine membership is a function-scoped property, not a per-op field.

---

## Layer 1: `GetTransferKind` — Stream vs DMA

Before an op can be outlined into an engine, the lowering must decide whether it is a *Stream* (indirect gather/scatter, the embedding datapath) or a *DMA* (contiguous bulk move). That is `xla::tpu::sparse_core::GetTransferKind` (`@0x1351b140`), reached from `LowerMemrefToMlo::lowerEnqueueDma` (`@0x135105a0`) and `lowerEnqueueIndirectDma` (`@0x13511da0`) via the typed wrappers `getTransferKind<EnqueueDMAOp>` (`@0x135114a0`) and `getTransferKind<WaitDMA2Op>` (`@0x135145e0`).

Its signature (demangled): `GetTransferKind(const jellyfish::Target&, mlir::sparse_core::MemorySpace src, MemorySpace dst, bool, bool, bool, bool)` returning `FailureOr<TransferKind>`. The decompiled body:

```c
// GetTransferKind @0x1351b140  (args: target a2; src a3; dst a4;
//   a5=src-local, a6=dst-local, a7=capability-allowed-flag, a8=strict-ordering)
// 1. normalize spmem: a space encoded as 1 maps to (16 if a7 else 21)
if (src == 1) src = 5*(a7 ^ 1) + 16;       // → 16 (cap) or 21 (no cap)
if (dst == 1) dst = 5*(a7 ^ 1) + 16;
// 2. kStream only when BOTH endpoints are local (a6 & a5 == 1)
if ((a6 & a5) == 1) {
  switch (src) {                            // jump table on the source memory space
    case 2:  /* HBM   */  ... if (dst<=0x15 && bittest(0x210018, dst)) ok;   // 2162712
                          else if (dst==6) ok only if target.vtable[+0xa0]()  // SupportsScVar
    case 3:  /* HBM_4B*/  ... if (dst<=0x15 && bittest(0x210004, dst)) ok;    // 2162692
    case 4:  case 21:     ... if (dst==2) ok;                                 // → HBM
    case 6:               ... ok only if a7 && dst==2 && target.vtable[+0xa0]()
    case 16: /* SPMEM */  ... if (a7 && ((dst-2)&~2)==0) ok;                  // dst in {2,4}
    default:              goto kDma;
  }
  result.kind = kStream; result.present = 1;   // [this+8]=1; [this]=1
  return;
}
// 3. otherwise kDma — but only for a recognized legal contiguous pair;
//    an unrecognized pair builds an InvalidArgument status
kDma:
  if (legal_dma_pair(src, dst, a6)) { result.kind = kDma; [this]=1; return; }
  // diagnostic (transfer_emitter.cc:196):
  return InvalidArgument(
    "SparseCore does not support transfers with %s ordering from %s %v to %s %v "
    "issued %sfrom TEC.", ordering, srcLocality, src, dstLocality, dst, fromTec);
```

The routing constants matter for reimplementation:

| Mechanism | Value | Meaning | Confidence |
|---|---|---|---|
| spmem normalization | `src/dst==1 → 5*(¬cap)+16` | encoded `1` → 16 (cap) or 21 (no cap) | CONFIRMED |
| both-local gate | `(dst_local & src_local) == 1` | kStream requires both endpoints local | CONFIRMED |
| HBM dst-set bitmask | `0x210018` (2162712) over `dst` | gatherable destinations from HBM source | CONFIRMED |
| HBM_4B dst-set bitmask | `0x210004` (2162692) over `dst` | gatherable destinations from HBM_4B | CONFIRMED |
| capability slot | `target` vtable `+0xa0` | the `SupportsScVar` predicate | HIGH |
| kStream result | `[result+0]=1, [result+8]=1` | FailureOr success + `kind=kStream` | CONFIRMED |
| kDma result | `[result+0]=1, [result+8]=0` | FailureOr success + `kind=kDma` | CONFIRMED |
| illegal-pair diag | `transfer_emitter.cc:196` `InvalidArgument` | "SparseCore does not support transfers…" | CONFIRMED |

> **NOTE — the capability bit is the `SupportsScVar` predicate, and it is 0 in this wheel.** The cases that gate on `target.vtable[+0xa0]()` (`src==6` and the `dst==6` sub-branch of HBM) call a virtual method on the `SparseCoreTarget`; the cross-references resolve this slot to `SupportsScVar` (Ghostlite `0x1d499340`, Viperfish `0x1d49c7e0`), which returns 0 for every generation shipped here. So those capability-gated Stream routes are *compiled out* in this build — they fall through to the kDma path. A reimplementer targeting these chips must treat `SupportsScVar` as false.

> **GOTCHA — the diagnostic says "from TEC", confirming the kDma/Stream split is TEC-centric.** The `transfer_emitter.cc:196` message ("…issued %sfrom TEC") and the `MemorySpace` operands show the classifier is reasoning about transfers issued *from the TEC vector engine*. This ties directly to [IndirectVregStream](indirect-vreg-stream.md) being a TEC-only Stream form: the gather/scatter datapath is anchored on TEC, and `GetTransferKind` is the gate that decides whether an off-tile move uses that datapath (kStream) or a plain bulk descriptor (kDma).

The full memory-space enum and the per-(core,mem) DMA-destination resolution are out of scope here; see [Stream Gather/Scatter](stream-gather-scatter.md) for the Stream-slot descriptor and [SC Backend Pipeline](sc-backend-pipeline.md) for where these lowerings run in the pass order.

---

## Layer 2: Region Outlining — Where `sc.sequencer` Is Written

`GetTransferKind`'s kStream/kDma result, together with the op's data dependencies, determines which TileTask region an op is emitted into. `LowerSequencerFunctionsPass::runOnOperation` (`@0x13532120`) then outlines each region into a standalone `LLVM::LLVMFuncOp` and stamps it with the `sc.sequencer` string. The decompiled pass body is large (it also builds per-engine parameter tables via `GetParameterTable` `@0x13534ec0` and loads HBM pointers via `LoadPointersFromHbm` `@0x13536c40`); the engine-tagging step is the `OutlineSequencerFunction` callback that attaches the StringAttr.

The mapping the outliner produces:

| TileTask region | `sc.sequencer` | Engine | Carries | Present on |
|---|---|---|---|---|
| Control / sequencer | `"scs"` | **SCS** | program counter, addressing, sync-flag/atomic issue, SCS Stream/DMA slots | VF · GL · GF |
| Access | `"access"` | **TAC** | tile-fetch DMA issue, gather-stream issue, address staging | VF · GL only |
| Execute | `"execute"` | **TEC** | vector reductions, pack/unpack, the TEC Stream slot (incl. IndirectVreg) | VF · GL · GF |

> **CONFIRMED — Trillium folds "access" into "execute".** On Viperfish/Ghostlite the three regions outline to three engines; on Trillium there is no TAC engine to receive an `"access"` function, so the tile-fetch/gather work that would be tagged `"access"` is instead emitted into the `"execute"` (TEC) function — the TEC Stream slot issues the gather directly. This is the binary-level corollary of [IndirectVregStream](indirect-vreg-stream.md) being TEC-exclusive and of the missing `SparseCoreTacCodecBase` (next section). The absence of a `HasAccessSequencerTypeAttribute` predicate is the structural tell that the pipeline does not need to *query* for `"access"` on the newest gen.

The exact per-op rule that chooses the Access region versus the Execute region for a given lowered op was not bit-traced in this analysis (the `GetTransferKind` result plus the op's tile-data dependencies feed it). It is flagged LOW here and owned by [Region → Sequencer Outliner](region-to-sequencer-outliner.md).

---

## The `TpuSequencerType` Enum and Its Jump Table

Underneath the string mechanism is the numeric `tpu::TpuSequencerType` enum, used to size and index per-engine resource tables (e.g. bundle limits). It is rendered to text by `TpuSequencerTypeToString` (`@0x20b362e0`), which is a pure jump table:

```c
// tpu::TpuSequencerTypeToString(unsigned a1)  @0x20b362e0
__int64 TpuSequencerTypeToString(unsigned a1) {
  return (__int64) *(&off_22010DE0 + a1);   // indexed array of C-string pointers
}
```

The `off_22010DE0` table indexes directly into the string-table literals confirmed in `.rodata`. Their order fixes the **runtime** numbering:

| Runtime value (table index) | `TpuSequencerType` literal | Short | Bundle |
|:---:|---|---|---|
| 0 | `TPU_SEQUENCER_TYPE_INVALID` | — | — |
| 1 | `TPU_SEQUENCER_TYPE_TENSOR_CORE_SEQUENCER` | TC | — |
| 2 | `TPU_SEQUENCER_TYPE_BARNA_CORE_SEQUENCER` | Barna | — |
| 3 | `TPU_SEQUENCER_TYPE_BARNA_CORE_ADDRESS_HANDLER` | Barna-AH | — |
| 4 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_SEQUENCER` | **SCS** | 32 B |
| 5 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_ACCESS_CORE_SEQUENCER` | **TAC** | 64 B |
| 6 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_TILE_EXECUTE_CORE_SEQUENCER` | **TEC** | 64 B |
| 7 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_V0_SEQUENCER` | SCv0 | (legacy) |
| 8 | `TPU_SEQUENCER_TYPE_SPARSE_CORE_V0_ADDRESS_HANDLER` | SCv0-AH | (legacy) |

All nine literal strings were read from the binary's string table; the index ordering is fixed by the `off_22010DE0` array that `TpuSequencerTypeToString` walks.

### The off-by-one: runtime enum vs codec template enum

The **codec** layer uses a *different* numbering. The per-engine codecs are template-parameterized on `TpuSequencerType` as a non-type template argument — `EncoderBase<…SparseCore{Scs,Tac,Tec}CodecBase…, TpuSequencerType=N>` — and there the values are `{SCS=3, TAC=4, TEC=5}`. The two enums are off by one (the codec numbering omits the `INVALID` slot, or equivalently the runtime numbering inserts one ahead of the SparseCore block):

| Engine | `sc.sequencer` string | Runtime proto enum | Codec template enum |
|---|---|:---:|:---:|
| SCS | `"scs"` | 4 | 3 |
| TAC | `"access"` | 5 | 4 (vfc/glc only) |
| TEC | `"execute"` | 6 | 5 |

> **GOTCHA — never cross the two numberings without the +1.** The runtime proto value (the `TpuSequencerTypeToString` index, used by per-engine resource arrays like the bundle-limit tracker) is one *greater* than the codec template parameter for the same engine. A reimplementer that feeds a runtime `TpuSequencerType` directly into a codec template selector will pick the wrong engine (or `INVALID`). The two values serve different layers; the *op-level* assignment uses neither — it uses the `sc.sequencer` string. The exact conversion site where a numeric value crosses from the runtime proto into codec selection was not located (flagged LOW).

### Codec-base presence confirms Trillium's missing TAC

Counting decompiled `SparseCore{Scs,Tac,Tec}CodecBase` instantiation files per family namespace directly confirms the TAC-removal that the off-by-one table implies (gfc carries codec params 3 and 5 only, never 4):

| Family ns | Gen | `SparseCoreScsCodecBase` files | `SparseCoreTacCodecBase` files | `SparseCoreTecCodecBase` files |
|---|---|---:|---:|---:|
| `vfc` | Viperfish | 13 | 13 | 13 |
| `glc` | Ghostlite | 30 | 30 | 30 |
| `gfc` | Trillium | 31 | **0** | 32 |

The Trillium (`gfc`) namespace has **zero** `SparseCoreTacCodecBase` files against 13/30 for Viperfish/Ghostlite, while SCS and TEC codec bases are present. There is no codec template parameterized on `TpuSequencerType=4` in `gfc`, so the runtime can never select a TAC engine on Trillium, and the `"access"` sequencer value is unreachable there — exactly the folding documented in Layer 2.

---

## SCv0 — Enum-Only

The two trailing `TpuSequencerType` values (`SPARSE_CORE_V0_SEQUENCER`, `SPARSE_CORE_V0_ADDRESS_HANDLER`) name the legacy monolithic SparseCore predecessor. They survive in this build only as the two string-table literals indexed by `TpuSequencerTypeToString`; no SCv0 codec, encoder, decoder, or `sc.sequencer` value (`"scs0"` etc.) ships. The engine-selection machinery never produces an SCv0 tag — `getSequencerType` returns only the three live values. See [SparseCore Overview](overview.md) for the full SCv0-deprecation account.

---

## Reimplementation Checklist

To reproduce SparseCore engine selection:

1. **Model `sc.sequencer` as a function-scoped StringAttr** with exactly three legal values `"scs"` / `"access"` / `"execute"`. Attach it during outlining; never attach it per-op. Enforce its presence on TileTask ops via a parent-function trait.
2. **Implement `getSequencerType` as a pure accessor** — inherent-attr lookup with dictionary-attr fallback, StringAttr type guard, returning `optional<StringRef>`. It makes no decisions.
3. **Implement `GetTransferKind` as the kStream/kDma gate** with the exact memory-space normalization (`1 → 5*(¬cap)+16`), the both-local gate, the `0x210018`/`0x210004` destination bitmasks per source space, the `SupportsScVar` capability call (false on these chips), and the `InvalidArgument` fallback for illegal pairs.
4. **Outline by region, fold "access" into "execute" when TAC is absent** (Trillium). Gate the existence of an `"access"` function on whether the target ships a `SparseCoreTacCodecBase`.
5. **Keep the two `TpuSequencerType` numberings separate** — runtime `{SCS=4,TAC=5,TEC=6}` for resource arrays, codec template `{SCS=3,TAC=4,TEC=5}` for codec selection, with a +1 at any boundary that crosses them.

---

## Confidence Summary

| Claim | Evidence | Confidence |
|---|---|---|
| `getSequencerType` is an attribute accessor returning `optional<StringRef>` | decompiled `@0x13507760`: inherent→dictionary `sc.sequencer` lookup, StringAttr guard | CONFIRMED |
| Attribute name is `"sc.sequencer"` (12 chars) | `getInherentAttr(…, 12)` literal in all three reader functions | CONFIRMED |
| `"scs"` → SCS, `"execute"` → TEC, `"access"` → TAC | byte-literal compares in `HasCore…` `@0x14599ec0` / `HasExecute…` `@0x1459a020`; `"access"` is the third value (no predicate) | CONFIRMED (scs/execute); HIGH (access) |
| Engine tag is function-scoped; ops inherit via parent-func trait | `ParentHasSequencerTypeAttribute` `@0x1353e980` walks to `LLVMFuncOp`; trait verified on `TileTaskWaitOp` `@0x14689880` | CONFIRMED |
| `GetTransferKind` selects kStream vs kDma on memory-space pair + capability | decompiled `@0x1351b140`: both-local gate, bitmasks `0x210018`/`0x210004`, `vtable[+0xa0]`, `transfer_emitter.cc:196` diag | CONFIRMED |
| `SupportsScVar` capability is 0 on these gens (capability-gated Stream routes compiled out) | `vtable[+0xa0]` resolves to `SupportsScVar` (GL `0x1d499340` / VF `0x1d49c7e0`), =0 | HIGH |
| Feeders: `lowerEnqueueDma` `@0x135105a0`, `lowerEnqueueIndirectDma` `@0x13511da0`, `getTransferKind<…>` `@0x135114a0`/`@0x135145e0` | demangled decompiled symbols present | CONFIRMED |
| Outliner stamps `sc.sequencer` per region | `LowerSequencerFunctionsPass::runOnOperation` `@0x13532120` + `OutlineSequencerFunction` | HIGH |
| Trillium folds "access" into "execute" (no TAC) | `gfc::…SparseCoreTacCodecBase` = 0 files vs 13/30; no `HasAccessSequencerTypeAttribute` | CONFIRMED |
| `TpuSequencerTypeToString` is a jump table over `off_22010DE0` | decompiled `@0x20b362e0`: `*(&off_22010DE0 + a1)` | CONFIRMED |
| Runtime enum order {INVALID=0…SCS=4,TAC=5,TEC=6,SCv0=7/8} | nine string-table literals + table index order | CONFIRMED |
| Codec template enum {SCS=3,TAC=4,TEC=5}, off by one from runtime | `EncoderBase<…, TpuSequencerType=N>` instantiations; gfc carries codec bases for Scs/Tec, none for Tac | HIGH |
| Per-op Access-vs-Execute region rule; runtime→codec conversion site | not bit-traced in this analysis | LOW |

---

## Cross-References

- [SparseCore Overview](overview.md) — the three engine classes, per-gen presence, and the SCv0 enum-only story.
- [Architecture](architecture.md) — engine roles and the embedding datapath that the engine split serves.
- [SCS (Scalar) Engine](scs-engine.md) — the `"scs"` control sequencer.
- [TAC Engine](tac-engine.md) — the `"access"` tile-fetch engine and its Trillium removal.
- [TEC (Vector) Engine](tec-engine.md) — the `"execute"` vector compute engine.
- [Region → Sequencer Outliner](region-to-sequencer-outliner.md) — the pass that partitions a computation into per-engine functions and writes `sc.sequencer`.
- [IndirectVregStream](indirect-vreg-stream.md) — the TEC-only Stream form whose existence anchors the kStream datapath on TEC.
- [Stream Gather/Scatter](stream-gather-scatter.md) — the indirect-DMA descriptor reached on the kStream path.
- [SC Backend Pipeline](sc-backend-pipeline.md) — where `GetTransferKind`, outlining, and `getSequencerType` sit in the SparseCore pass order.
- **Binary:** `extracted/libtpu-0.0.40-cp314-cp314-manylinux_2_31_x86_64/libtpu/libtpu.so` (build-id `89edbbe81c5b328a958fe628a9f2207d`)
- **Index entry:** Part IX — SparseCore & BarnaCore / SparseCore engines — [back to index](../index.md)
