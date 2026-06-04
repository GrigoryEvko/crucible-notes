# LlvmTpu Intrinsic Table

> *All counts, names, and addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build-id `89edbbe81c5b328a958fe628a9f2207d`, 745 MB, ELF x86-64, not stripped). Other libtpu builds add/remove intrinsics per generation; the 1356 figure is exact for this build.*

## Abstract

The TPU LLVM backend exposes a flat namespace of `llvm.tpu.*` target intrinsics — the bottom-of-stack IR surface that the SparseCore MLIR dialect (`mlir::sparse_core::*`) lowers into and that the SelectionDAG instruction selector matches against. This appendix is the master enumeration of that surface: **exactly 1356 distinct intrinsics**, recovered two independent ways from the binary that agree to the op (see [§Verification](#verification)). Each is both an ODS-registered MLIR op (C++ class `tpu_X_Y_Z`) and an LLVM intrinsic name (`llvm.tpu.X.Y.Z`, underscore→dot); the two sets are identical.

This is a *reference catalog*, not an algorithm page. The deep semantics — slot bit layouts, lowering bodies, ISel matcher arms, per-engine descriptor encodings — live on the [`sparsecore/`](../sparsecore/overview.md) and [`isa/`](../isa/overview.md) pages, which this table cross-links per family. Here a reimplementer gets the shape of the space: the families, their exact per-family counts, a representative enumerated subset of each, and the LLO op / hardware unit / SparseCore engine each family lowers to.

The single most important structural fact: **the `llvm.tpu.*` surface is the SparseCore intrinsic ISA, not the TensorCore one.** It contains no MXU/matmul and no XLU/transpose intrinsics — those go through a separate `tpu→LLO` TensorCore ODS path. The `llvm.tpu.*` family is dominated (62%) by the SparseCore embedding stream engine, detailed in the [note](#note-intr-1) below.

For reimplementation, the catalog must capture:

- **The name↔class bijection** — every `llvm.tpu.X` intrinsic has one `mlir::sparse_core::tpu_X` Model and vice-versa; the printed-name string IS the LLVM intrinsic name carried into the backend.
- **The functional taxonomy** — every intrinsic belongs to exactly one of ~20 families; the 20 enumerated families recover 1341 of the 1356, with ~15 left on MEDIUM-confidence family boundaries (see [§At-a-Glance](#at-a-glance--family-taxonomy)).
- **The family→hardware map** — which LLO slot/op, SparseCore compute unit, or stream-engine descriptor each family lowers to, and via which pass.
- **Per-gen variation** — newer generations add intrinsics (this build is the union for its target gens); the count is a per-build snapshot.

| | |
|---|---|
| **Total intrinsics** | **1356** (confirmed two ways, zero mismatch) |
| **Printed-name strings** | 1356 distinct `llvm.tpu.*` in `.rodata` |
| **Model classes** | 1356 distinct `mlir::sparse_core::tpu_*` `RegisteredOperationName::Model` vtables |
| **Name↔class map** | mechanical: `tpu_X_Y` ↔ `llvm.tpu.X.Y` (underscore→dot) |
| **Registration** | `registerLlvmTpuDialectOperations` `@0x146d0560`, 10 batch sub-registrars |
| **Dialect** | `mlir::sparse_core::LlvmTpuDialect` (ctor `@0x146cde80`) |
| **MLIR→IR translation** | `convertOperation` `@0x13933e40` → `createIntrinsicCall` `@0x1683f440` |
| **Consuming lowering pass** | `LowerToSparseCoreLlvmPass::runOnOperation` `@0x13566d00` |
| **Dominant family** | `llvm.tpu.stream.*` — 834 ops (62%) |
| **Surface scope** | SparseCore only — no MXU, no XLU intrinsics |

---

## Verification

The 1356 figure is not taken on faith; it is the agreement point of two independent recoveries from this binary.

- **Printed-name strings.** The `.rodata` printed-name table holds **1356** distinct strings matching `^llvm\.tpu\.` (e.g. `llvm.tpu.addrspacecast`, `llvm.tpu.stream.linear.gather.add.f32.hbm.to.tilespmem`). Caveat for a re-prober: a few names carry uppercase (`scan2xN`, `scan1xNf`) — an `[a-z0-9_.]` character class undercounts to 1060; the correct count needs `[^"]` or an explicit uppercase allowance.
- **Model class vtables.** The symbol table holds **1356** distinct `mlir::sparse_core::tpu_*` class names (the per-intrinsic `RegisteredOperationName::Model<…>` registration concept). The vtable span runs `tpu_16i1_to_32i1` … `tpu_wrcbreg_tilespmem_base`.
- **Set identity.** Mapping every class `tpu_X` to `llvm.tpu.X` (underscore→dot) and diffing against the string set yields zero asymmetric difference — every Model has exactly one printed name and vice-versa.

> **NOTE —** the per-family subcounts below are the byte-grep totals from the printed-name strings, classified by name prefix. A handful of ops are dual-classifiable (e.g. `sc.permute` is both a lane op and an SC-control op), so two analysts may shift one or two ops between adjacent families; the *total* is exact and the *dominant families* (stream, pack/unpack, vld/vst, wait/watch, dma, scan, convert) are exact. Family rows carrying that ambiguity are marked `MEDIUM` confidence.

> **NOTE —** <a id="note-intr-1"></a>the `llvm.tpu.*` surface contains **no MXU/matmul and no XLU/transpose-permute intrinsics**. A whole-table grep for `mat` returns 0; for `xlu`/`transp` returns 0. The MXU and XLU op rosters are TensorCore concepts reached through the separate `tpu→LLO` 322-op ODS path (see [`isa/slot-mxu.md`](../isa/slot-mxu.md) and [`isa/xlu-op-roster.md`](../isa/xlu-op-roster.md)), not through `llvm.tpu.*`. A reimplementer who expects `llvm.tpu.matmul` will not find it; the matmul handshake on SparseCore is documented at [`sparsecore/sc-mxu-handshake.md`](../sparsecore/sc-mxu-handshake.md). This page's families are SparseCore vector/scalar/memory/stream/sync/control primitives only.

---

## At-a-Glance — Family Taxonomy

Every intrinsic is assigned to exactly one family. The 20 enumerated family rows recover 1341 of the 1356 grep-confirmed names; the remaining 15 sit on the MEDIUM-confidence boundaries between adjacent families and are carried in the explicit remainder row rather than force-assigned. The LLO/HW column is the lowering target: a TensorCore-style LLO slot/op where the SparseCore reuses it, a SparseCore-specific compute unit, or the SparseCore stream engine. Status: `C` = encoding byte-confirmed elsewhere, `I` = engine identified by name family, opcode not individually byte-dumped.

| Family | # | LLO op / hardware unit | St |
|---|---:|---|---|
| [stream (gather/scatter/vreg)](#stream-engine-the-dominant-family) | 834 | SparseCore stream/scatter engine descriptor → `LowerToSparseCoreLlvm` → LLVM call | I |
| [pack / unpack (subelement)](#pack--unpack-subelement-staging) | 87 | VPU pack/unpack slot → `llo.vpack*` / `llo.vunpack*` | C |
| [vector load / store](#vector-load--store-vmem--cb) | 74 | VPU mem slot → `llo.vector_{load,store}[_masked]` | C |
| [semaphore wait / watch](#sync--wait--watch-sflag-atomics) | 47 | sflag VWait slot → `llo.vwait.{eq,ne,lt,le,gt,ge}[.done]` | C |
| [DMA descriptor](#dma-descriptor-builders) | 40 | SparseCore DMA engine cmd (3 complexity tiers) | C |
| [scan / segment-scan / reduce](#scan--reduce-embedding-aggregation) | 32 | SparseCore scan unit (add/max/min × full/half/1xN/2xN × seg × index) | I |
| [vector convert](#vector-convert) | 28 | VPU convert slot → `llo.vcvt.*` | C |
| [alloca / allocate](#alloca--allocate) | 24 | SparseCore allocator (smem/spmem/vmem/sflag/hbm/iova/timem/tilespmem/dreg/cbreg) | C |
| [semaphore set / add (sflag)](#sync--wait--watch-sflag-atomics) | 22 | sflag VSync slot → `llo.vsync.{add,set}[.done,.remote]` | C |
| [transcendental / EUP](#transcendental--eup) | 22 | EUP VALU3 push (Alu3 op0 + 5-bit selector) + `PopEupResult` | C |
| [control register rd / set](#control-register-rd--set) | 21 | scalar `RdReg`/`SetReg` → SCS scalar slot | C |
| [pointer / addressing / loop-bc](#pointer--addressing--loop-bytecode) | 16 | LLVM `inttoptr`/`ptrtoint`/`addrspace` + loop bytecode | C |
| [addrspacecast](#addrspacecast) | 16 | surviving `@llvm.tpu.addrspacecast.*` IR intrinsic call (IDs `0x33b1`–`0x33c0`) | C |
| [scalar ALU / scalar mem](#scalar-alu--scalar-mem) | 14 | SCS scalar slot (shifts, overflow-add, addcarry, add_high/low_f32) | C |
| [lane / sublane permute](#lane--sublane-permute) | 14 | VPU cross-lane slot → `llo.vrot.slane` / `vperm.sublane` / `sc.permute` | C |
| [CBREG / circular buffer](#cbreg--circular-buffer) | 12 | scalar CBREG ops (`ReadCbreg`/`WriteCbreg`/`AddCbreg`/`MoveCbreg`/`SLD/SST.cb`) | C |
| [trace / telemetry / sc-control](#trace--telemetry--sc-control) | 12 | SparseCore control/trace (strace, event, cycle-count, ssetpstate) | C |
| [sort / unique / dupcount](#sort--unique--dupcount) | 11 | SparseCore sort/dedup unit (embedding dedup) | I |
| [task / control / structural](#task--control--structural) | 10 | SparseCore tile-task + structural (task_dispatch, loop_*, barrier, nop) | C |
| [i1-mask width conversion](#i1-mask-width-conversion) | 5 | VPU mask slot — vector-mask width re-pack | C |
| unclassified remainder (boundary/misc) | 15 | spread across the MEDIUM-confidence families — not separately enumerated | I |
| **Total** | **1356** | | |

Sum check: `834+87+74+47+40+32+28+24+22+22+21+16+16+14+14+12+12+11+10+5 = 1341`; the 20 enumerated families recover 1341 of the 1356 grep-confirmed names, leaving 15 intrinsics on the MEDIUM-confidence family boundaries that this taxonomy does not separately bucket (the "unclassified remainder" row above). The 1356 total is the byte-confirmed truth; the per-family split is the analyst classification and is exact only for the grep-anchored families (stream, pack/unpack, vld/vst, wait/watch, dma, scan, convert, alloca, sync, addrspacecast, i1-width).

---

## Stream Engine — the Dominant Family

834 of the 1356 intrinsics (62%) are `llvm.tpu.stream.*`: the SparseCore embedding stream/scatter engine. This is a combinatorial cross-product encoded as distinct ops — the hardware stream-command variant is selected by *op identity*, not by an attribute, so the type system carries the `(pattern, verb, dtype, memspace)` choice. Deep semantics live on [`sparsecore/stream-gather-scatter.md`](../sparsecore/stream-gather-scatter.md) and [`sparsecore/indirect-vreg-stream.md`](../sparsecore/indirect-vreg-stream.md); do not reproduce the 834 rows here.

### Cross-Product Axes

| Axis | Values | Count contribution |
|---|---|---|
| pattern | `linear` · `strided` · `indirect` | linear 180 · strided 114 · indirect 540 |
| verb | `vreg` (gather+scatter), `gather` (non-vreg), `scatter` (non-vreg) | vreg 360 · gather 246 · scatter 228 |
| dtype | `bf16` · `e4m3` · `e5m2` · `f32` · `s16` · `s32` | 111 ops per dtype (×6 = 666 dtyped) |
| transfer | `_to_tilespmem` · `_to_spmem` · `_to_smem` · `_to_hbm4b` · `_to_hbm` | tilespmem 399 · spmem 210 · smem 27 · hbm4b 15 · hbm 15 |
| modifier | `.cb.` (CBREG-windowed) · `.add` (scatter-add) · `.np` (no-predicate) | cb-windowed 556 of 834 |

### Naming Template

```text
llvm.tpu.stream.<pattern>.[vreg.]{gather|scatter}.[cb.][add.]<dtype>.<src>.to.<dst>

e.g.  llvm.tpu.stream.linear.gather.add.f32.hbm.to.tilespmem
      (class tpu_stream_linear_gather_add_f32_hbm_to_tilespmem;
       create(OpBuilder&, Location, Value×6) — 6 SSA operands)
```

The CBREG-windowed indirect forms (556 ops) use the `INDIRECT_OFFSET_SOURCE_CBREG` source for embedding-table windowing (see [`sparsecore/cbreg.md`](../sparsecore/cbreg.md)). An indirect gather pulls embedding rows from an HBM/SPMEM table — indexed by a CBREG-windowed offset stream — into tile-local SPMEM; the matching scatter-add pushes accumulated gradients back. These lower via `LowerToSparseCoreLlvmPass` `@0x13566d00` to an LLVM call into the stream/scatter engine.

> **GOTCHA —** there is no single `llvm.tpu.stream` op with attribute selectors. A reimplementer who models the stream engine as one parameterized op will mismatch the binary, which registers all 834 combinations as separate ops and dispatches them by op identity in the ISel matcher. The 834-way explosion *is* the encoding.

---

## Pack / Unpack (subelement staging)

87 ops. VPU pack/unpack slot — sub-byte width staging for quantized MXU feeds. Lowers to `llo.vpack*` / `llo.vunpack*` (see [`isa/pack-unpack-precision.md`](../isa/pack-unpack-precision.md) and [`isa/slot-vpu.md`](../isa/slot-vpu.md)).

| Representative intrinsic | What it does | Lowers to |
|---|---|---|
| `llvm.tpu.packc` | scalar pack-compress | VPU pack slot |
| `llvm.tpu.pack.c.b32.b16` | pack b32 → b16 | `llo.vpack` (width stage) |
| `llvm.tpu.pack.c.b16.b8` | pack b16 → b8 | `llo.vpack` |
| `llvm.tpu.pack.c.b8.b4` | pack b8 → b4 | `llo.vpack` |
| `llvm.tpu.pack.c.b4.b2` | pack b4 → b2 | `llo.vpack` |
| `llvm.tpu.pack.c.b2.b1` | pack b2 → b1 | `llo.vpack` |
| `llvm.tpu.pack.c.f32.bf16` | pack f32 → bf16 | `llo.vpack` |
| `llvm.tpu.pack.c.bf16.e4m3` | pack bf16 → e4m3 (FP8) | `llo.vpack` |
| `llvm.tpu.pack.c.bf16.e5m2` | pack bf16 → e5m2 (FP8) | `llo.vpack` |
| `llvm.tpu.pack.c.bf16.s8` / `.bf16.u8` | pack bf16 → s8/u8 | `llo.vpack` |
| `llvm.tpu.pack.c.b16i1.b8i1` | pack mask b16i1 → b8i1 | VPU mask pack |
| `llvm.tpu.unpack.*` | inverse width expansion | `llo.vunpack` |

The width ladder is `b32 → b16 → b8 → b4 → b2 → b1`, with FP8 (`e4m3`/`e5m2`) and integer (`s8`/`u8`) endpoints for quantization.

---

## Vector Load / Store (vmem / cb)

74 ops. VPU mem slot → `llo.vector_{load,store}[_masked]`. Semantics on [`sparsecore/vectorload-slot.md`](../sparsecore/vectorload-slot.md) and [`sparsecore/vectorstore-slot.md`](../sparsecore/vectorstore-slot.md); slot encoding on [`isa/slot-memory-load.md`](../isa/slot-memory-load.md) / [`isa/slot-memory-store.md`](../isa/slot-memory-store.md).

The family is a modifier cross-product on a `vld`/`vst` base. Every load is masked (`.msk`); stores add scatter (`.add`) and FP8 store-pack (`.e4m3`/`.e5m2`).

| Modifier | Meaning |
|---|---|
| `.msk` | masked (predicated) — present on all |
| `.cb` | CBREG-windowed address |
| `.upd` | post-update auto-increment |
| `.idx` | indexed (gather/scatter address) |
| `.strided` | strided access |
| `.add` | scatter-add (store-side reduction) |
| `.e4m3` / `.e5m2` | FP8 store-pack |
| `.np` | no-predicate fast variant |

| Representative intrinsic | What it does |
|---|---|
| `llvm.tpu.vld.msk` | masked vector load |
| `llvm.tpu.vld.msk.idx` | masked indexed (gather) load |
| `llvm.tpu.vld.msk.strided` | masked strided load |
| `llvm.tpu.vld.cb.msk` | CBREG-windowed masked load |
| `llvm.tpu.vld.cb.upd.msk` | CBREG-windowed post-update masked load |
| `llvm.tpu.vst.cb.msk` | CBREG-windowed masked store |
| `llvm.tpu.vst.cb.msk.add` | CBREG-windowed masked scatter-add store |
| `llvm.tpu.vst.cb.msk.add.e4m3` | scatter-add store, FP8-packed |
| `llvm.tpu.vst.cb.msk.idx.add` | indexed scatter-add store |

---

## Sync / Wait / Watch (sflag atomics)

The sflag (semaphore-flag) atomic surface splits into sub-families that this page counts together. The byte-confirmed union (`^llvm\.tpu\.(sync|sfence|wait|watch|fetch)`) is **73 ops**: `sync*`/`sfence` = 25, `wait*` = 31, `watch*` = 16, `fetch.and.add` = 1. (The taxonomy rows above split this differently — wait/watch = 47, sync set/add = 22 — with the three `sync{donemov,pamov,readpa}` ops and `fetch.and.add` carried in adjacent rows.) All target the sflag bank via the VPU/SPU sync/wait slots (see [`isa/slot-vpu.md`](../isa/slot-vpu.md), [`isa/slot-spu-scalar.md`](../isa/slot-spu-scalar.md)).

### sync / set / add — 22 ops → `llo.vsync.{add,set}`

| Representative intrinsic | What it does |
|---|---|
| `llvm.tpu.syncadd` | atomic add to sflag |
| `llvm.tpu.syncadd.done` / `.notdone` | add + signal done / not-done |
| `llvm.tpu.syncadd.remote` / `.remote.done` / `.remote.doneinv` | add to a peer core's sflag over ICI |
| `llvm.tpu.syncadd.tile` / `.both` / `.other` | per-tile / both-direction / other-bank target |
| `llvm.tpu.syncset.done` / `.notdone` / `.both` / `.both.done` | atomic set |
| `llvm.tpu.syncset.remote[.done/.doneinv]` | remote set over ICI |
| `llvm.tpu.syncset.tile[.done/.doneinv]` | per-tile set |
| `llvm.tpu.syncsetpa` | set, public-access bank |
| `llvm.tpu.sfence` | sflag memory fence |

### wait / watch — 47 ops → `llo.vwait.{eq,ne,lt,le,gt,ge}[.done]`

| Representative intrinsic | What it does |
|---|---|
| `llvm.tpu.waiteq` / `waitne` / `waitlt` / `waitle` / `waitgt` / `waitge` | wait until sflag {==,!=,<,<=,>,>=} threshold |
| `llvm.tpu.wait{eq,ge,…}ordone` | wait-or-done variant |
| `llvm.tpu.wait{…}.yieldable` | sequencer-yield-on-wait variant |
| `llvm.tpu.wait{eq,ge,gt,lt,ne}.imem` | instruction-memory-flag wait |
| `llvm.tpu.waitdone` / `.yieldable` | wait for done signal |
| `llvm.tpu.waitnotdone` / `.yieldable` | wait for not-done |
| `llvm.tpu.watch{eq,ne,lt,gt,…}[ordone]` | non-blocking watch arm |
| `llvm.tpu.watch.wait` / `.wait.sel` / `.end` / `.end.sel` | watch register lifecycle |
| `llvm.tpu.fetch.and.add` | sflag read-modify-write `(V, V, V)` |

> **QUIRK —** `_remote` routes over ICI to a *peer core's* sflag (megascale barrier path), `.tile`/`.pa` target the per-tile / public-access bank, `.doneinv` inverts the done sense, and `.yieldable` lets the sequencer yield while blocked. A reimplementer must treat these suffixes as distinct hardware behaviors, not cosmetic aliases.

---

## DMA Descriptor Builders

40 ops. SparseCore DMA engine command builders. Naming is `llvm.tpu.dma.<src>.to.<dst>.sc.{simple,single.strided,general}`. The suffix is the descriptor-complexity tier; the tier is confirmed by the typed-create operand count.

| Tier | Suffix | # ops | Operands (after Location) |
|---|---|---:|---|
| simple | `.sc.simple` | 16 | 8 Value (src,dst + base/offset/size + sflag) |
| single-strided | `.sc.single.strided` | 12 | 11 Value (+1 stride triple) |
| general | `.sc.general` | 12 | 16 Value (+ multi-dim strides/sizes) |

`src`/`dst` range over `hbm`, `iova`, `smem`, `spmem`, `timem`, `simem`. Representative: `llvm.tpu.dma.hbm.to.spmem.sc.simple`, `llvm.tpu.dma.spmem.to.hbm.sc.single.strided`, `llvm.tpu.dma.hbm.to.spmem.sc.general`. (The `iova` src/dst appears only in the `simple`/`single.strided` tiers, not in `general`.) These are the SparseCore equivalents of the high-level `DmaSimpleStart` / `DmaSingleStridedStart` / `DmaGeneralStart` dialect ops; the intrinsic is the post-lowering form the backend turns into the DMA engine command.

---

## Scan / Reduce (embedding aggregation)

32 ops. SparseCore scan unit — the embedding-aggregation primitives. Deep datapath on [`sparsecore/scan-datapath.md`](../sparsecore/scan-datapath.md), [`sparsecore/segmented-scan.md`](../sparsecore/segmented-scan.md), and [`sparsecore/segmented-add-scan.md`](../sparsecore/segmented-add-scan.md).

The family is a clean cross-product: `{add, max, min} × {scan1xN, scan2xN} × {seg, non-seg} × {index, value} × {f, i}`.

| Representative intrinsic | What it does |
|---|---|
| `llvm.tpu.add.scan1xNf` / `.add.scan1xNi` | 1×N prefix-sum, float / int |
| `llvm.tpu.add.seg.scan1xNf` | segmented 1×N prefix-sum |
| `llvm.tpu.add.full.scan2xN` / `.add.half.scan2xN` | 2×N scan, full / half |
| `llvm.tpu.add.full.seg.scan2xN` | segmented 2×N full scan |
| `llvm.tpu.max.scan1xNf` / `.max.scan2xN` | max scan |
| `llvm.tpu.max.index.scan1xNf` | argmax-index scan |
| `llvm.tpu.max.seg.index.scan2xN` | segmented argmax scan |
| `llvm.tpu.min.scan1xNi` / `.min.seg.scan2xN` | min / segmented-min scan |

`.seg` marks segmented (segment-boundary-aware) scans; `.index` yields the arg-position rather than the value.

---

## Vector Convert

28 ops. VPU convert slot → `llo.vcvt.*` (see [`isa/slot-vpu.md`](../isa/slot-vpu.md)). Float↔int and float↔narrow-float conversions, with stochastic-round (`.sr`) and probabilistic-round (`.pr`) variants.

| Representative intrinsic | What it does |
|---|---|
| `llvm.tpu.vcvt.f32.bf16` / `.f32.bf8` / `.f32.hf16` / `.f32.if8` | widen narrow-float → f32 |
| `llvm.tpu.vcvt.s32.f32` / `.f32.s32` | int ↔ float |
| `llvm.tpu.vcvt.bf16.s4` / `.bf16.s8` / `.bf16.u4` / `.bf16.u8` | dequant int → bf16 |
| `llvm.tpu.vcvt.s4.bf16` / `.s8.bf16` / `.u4.bf16` / `.u8.bf16` | quant bf16 → int |
| `llvm.tpu.vcvt.sr.f32.bf16` / `.sr.fptobf16` | stochastic-round narrowing |
| `llvm.tpu.vcvt.fptobf16` / `.fptobf8` / `.fptohf16` / `.fptoif8` | float → narrow-float |
| `llvm.tpu.cvt.fptosi` / `.cvt.pr.fptosi` | scalar float → signed int |

---

## Alloca / Allocate

24 ops. SparseCore allocator → per-bank allocation. `tpu_alloca_*` is stack-style allocation; `tpu_allocate_*` is the per-bank (incl. CBREG) allocator. Banks: `smem`, `spmem`, `vmem`, `sflag`, `hbm`, `iova`, `timem`, `tilespmem`, `dreg`, `cbreg`, plus `_dyn` (dynamic) and `_any` (any-bank). Representative: `llvm.tpu.alloca.smem`, `llvm.tpu.alloca.sflag`, `llvm.tpu.allocate.dreg`, `llvm.tpu.allocate.cbreg`.

---

## Transcendental / EUP

22 ops. The Extended-Unit Pipeline (EUP) transcendentals — VALU slot-3 push + pop pair. Each maps 1:1 onto a V5+ EUP transcendental whose function→selector value is decoded on [`isa/slot-eup-transcendental.md`](../isa/slot-eup-transcendental.md). The push is VALU `Alu3` (opcode `0x0` + 5-bit selector); results drain via the `VectorResult0 PopEupResult` slot.

| Intrinsic (+`.macro`) | Function | EUP selector (F32 / Bf16) |
|---|---|---|
| `llvm.tpu.rcp` | reciprocal | `0x15` / `0x1d` |
| `llvm.tpu.rsqrt` | reciprocal-sqrt | `0x10` / `0x0c` |
| `llvm.tpu.tanh` | tanh | `0x13` / `0x1b` |
| `llvm.tpu.sigshft` | shifted-sigmoid | `0x14` / `0x1c` |
| `llvm.tpu.log2` | log₂ | `0x12` / `0x1a` |
| `llvm.tpu.pow2` | 2ˣ | `0x11` / `0x19` |
| `llvm.tpu.erf` | error function | `0x0e` / `0x0f` |
| `llvm.tpu.sin` | sine (Sinq) | `0x17` / `0x1e` |
| `llvm.tpu.cos` | cosine (Cosq) | `0x18` / `0x1f` |

The bare form (`llvm.tpu.sin`) is the raw EUP push; the `.macro` form (`llvm.tpu.sin.macro`, typed `(Type, Value)` → 1 result, 1 operand) is the push+pop macro. Plus the non-paired EUP ops: `llvm.tpu.exponent`, `llvm.tpu.significand`, `llvm.tpu.vclass` (FP classify), `llvm.tpu.eup.pop` (explicit result drain).

---

## Control Register rd / set

21 ops. Scalar control-register reads/writes → SCS scalar slot `RdReg`/`SetReg` (see [`isa/slot-spu-scalar.md`](../isa/slot-spu-scalar.md)).

| Representative intrinsic | Register read / set |
|---|---|
| `llvm.tpu.rdreg.gtc.hi` / `.gtc.lo` | global time counter (64-bit, hi/lo) |
| `llvm.tpu.rdreg.lcc.hi` / `.lcc.lo` | local cycle counter |
| `llvm.tpu.rdreg.crr.hi` / `.crr.lo` | core resource counter |
| `llvm.tpu.rdreg.tid` / `.scid` / `.tag` / `.tbm` / `.tm` | thread/sparse-core/tag/tile-base/tile-mask id |
| `llvm.tpu.rdreg.fsr` / `.ddr` / `.dmacrdt` / `.btr` / `.yieldreq` | status/credit/yield registers |
| `llvm.tpu.setreg.sflagrange` / `.dmacrdt` / `.pdepth` / `.tag` / `.ifvalue` | configure sflag range / DMA credit / predicate depth |

---

## Pointer / Addressing / Loop-bytecode

16 ops. LLVM `inttoptr`/`ptrtoint`/addrspace plus loop-bytecode helpers. Representative: `llvm.tpu.inttoptr`, `llvm.tpu.ptrtoint`, `llvm.tpu.make.restrict.ptr`, `llvm.tpu.bc.load.aliaddr`, `llvm.tpu.bc.store.aliaddr`, `llvm.tpu.bc.select.predicate`, `llvm.tpu.bc.extractvalue.loopindex`, `llvm.tpu.bc.insertvalue.loopindex`. The `bc.*` (loop-bytecode) ops are handled in `PerformDAGCombine` opcode-`0x30` arm for IDs `0x33d9`/`0x33da`. (`MEDIUM` — boundary with task/structural overlaps on a couple of `bc.*loopindex` ops.)

---

## AddrSpaceCast

16 ops. The SparseCore address-space transition casts. These are the one family whose backend handling is byte-traced end-to-end and corrects a common assumption — they are emitted as *surviving LLVM-IR intrinsic calls*, not folded to a generic IR `addrspacecast` or to `ISD::ADDRSPACECAST`. Deep treatment on [`sparsecore/addrspacecast-isel.md`](../sparsecore/addrspacecast-isel.md) and [`sparsecore/tile-id-cast.md`](../sparsecore/tile-id-cast.md).

### The Full 16

```text
llvm.tpu.addrspacecast                          (plain, 1-operand)
llvm.tpu.addrspacecast.scs
llvm.tpu.addrspacecast.smem
llvm.tpu.addrspacecast.spmem
llvm.tpu.addrspacecast.tac
llvm.tpu.addrspacecast.tc
llvm.tpu.addrspacecast.tec
llvm.tpu.addrspacecast.scs.sflag.scs
llvm.tpu.addrspacecast.tec.sflag.tec
llvm.tpu.addrspacecast.smem.tile.scs
llvm.tpu.addrspacecast.smem.tile.tec
llvm.tpu.addrspacecast.sflag.tile.scs
llvm.tpu.addrspacecast.sflag.tile.tac
llvm.tpu.addrspacecast.sflag.tile.tec
llvm.tpu.addrspacecast.sflag.tile.sflag.scs
llvm.tpu.addrspacecast.sflag.tile.sflag.tec
```

The plain cast is 1-operand (`create(Type, Value)`); the per-core (`tec`/`tac`/`scs`) and tile-windowed forms take an extra `i32` tile-id Value.

### Emission and Severance

| Stage | Site | Action |
|---|---|---|
| MLIR op → IR call | `convertOperation` `@0x13933e40` (16 arms `0x1393c460`–`0x1393c8da`) → trampoline `@0x1393bf27` → `createIntrinsicCall` `@0x1683f440` | emits `@llvm.tpu.addrspacecast.*` IR call, intrinsic IDs `0x33b1`–`0x33c0` (13233–13248) — byte-verified from the storage order of `IntrinsicNameTableStorage @0x4179440` (`llvm.tpu.addrspacecast` = ID `0x33b1`; the prior `0x33b0`–`0x33bf` derivation was off by one) |
| IR intrinsic survives | (no conversion) | NOT lowered to `ISD::ADDRSPACECAST` (`0xf4`) — no matcher arm, no `Select`/`LowerOp`/`Combine` arm |
| discharge | generic `INTRINSIC_WO_CHAIN` fold / consuming SC load-store ISel | value-preserving cast absorbed by the consumer (inferred) |

> **NOTE —** the SparseCore `addrspacecast` intrinsics do **not** become `ISD::ADDRSPACECAST` (`0xf4`) nodes. The `0xf4` node arises only from a *real IR `addrspacecast` instruction* (via `SelectionDAGBuilder::visitAddrSpaceCast` `@0x19333020` → `getAddrSpaceCast` `@0x192e2360`), which no TPU/SparseCore code emits — a whole-`.text` xref of the `addrspacecast` constructors places every caller in generic LLVM, none in the TPU/SC bands. The cast intrinsic family and the `0xf4`/`0xf3` lowering are two separate mechanisms; the `0xf4`→`0xf3` register-copy path serves only the generic TensorCore front. The only ID-keyed backend sites for `0x33b1`–`0x33c0` are: the `convertOperation` emit, the `Select` default-route table (`@0xaec81ec` idx `0x10`–`0x1f` → `SelectCode` default), and `TPUVerifier::runImpl` `@0x13c54912` (validation only). No lowering site.

---

## Scalar ALU / scalar mem

14 ops. SCS scalar slot — shifts, overflow-arithmetic, carry, FP-component add. Representative: `llvm.tpu.shll`, `llvm.tpu.shra`, `llvm.tpu.shrl`, `llvm.tpu.sshllo`, `llvm.tpu.sadd.ov`, `llvm.tpu.ssub.ov`, `llvm.tpu.sshla.ov` (overflow-flagged add/sub/shift), `llvm.tpu.addcarry`, `llvm.tpu.add.high.f32.bf16`, `llvm.tpu.add.low.f32.bf16` (the bf16-decomposed-f32 add halves). (`MEDIUM` — `add.high/low.f32.bf16` could alternatively be grouped under EUP/precision.)

---

## Lane / sublane permute

14 ops. VPU cross-lane slot — sublane shuffle/rotate/permute and SC permute. Representative: `llvm.tpu.vrot.sublane`, `llvm.tpu.vrot.sublane.down`, `llvm.tpu.vperm.sublane`, `llvm.tpu.vshift.insert`, `llvm.tpu.sc.permute`, `llvm.tpu.sc.mask.permute`, plus the `vlaneseq` sequence-generator forms (`llvm.tpu.vlaneseq.c.bf16`, `.i.bf16`, `.u32`). See [`sparsecore/rank-and-permute-radixsort.md`](../sparsecore/rank-and-permute-radixsort.md) for the permute-driven radix-sort use. (`MEDIUM` — `sc.permute`/`sc.mask.permute` straddle lane-op vs sc-control.)

---

## CBREG / circular buffer

12 ops. Scalar CBREG (circular-buffer register) ops driving the 16-CBREG-per-bank circular buffers, windowing both SMEM and TILE_SPMEM. Full encoding on [`sparsecore/cbreg.md`](../sparsecore/cbreg.md).

| Intrinsic | LLO op |
|---|---|
| `llvm.tpu.rdcbreg.offset` / `.size` / `.smem.base` / `.tilespmem.base` | `ReadCbreg` (`0x36`), `CbregMetadata {BASE=0,SIZE=1,OFFSET=2}` |
| `llvm.tpu.wrcbreg.offset` / `.size` / `.smem.base` / `.tilespmem.base` | `WriteCbreg` (`0x35`) |
| `llvm.tpu.cbreg.add.offset` / `.add.offset.in.place` | `AddCbreg` (`0x33`, targets OFFSET) |
| `llvm.tpu.copy.cbreg` | `MoveCbreg` (escape `0x00`/sub `0x1b`) |
| `llvm.tpu.allocate.cbreg` | CBREG allocation (one of 16 per SCS/TAC/TEC bank) |
| `llvm.tpu.sld.cb` / `.sld.cb.upd` | `SLDCircularBuffer` (`0x3f` / `0x3e`, `.upd` = post-update) |
| `llvm.tpu.sst.cb` / `.sst.cb.upd` | `SStoreCircularBuffer` (`0x3d` / `0x3c`) |

The dual base (`smem.base` vs `tilespmem.base`) is the dual-address-space window: CBREG windows both SMEM and TILE_SPMEM.

---

## Trace / Telemetry / sc-control

12 ops. SparseCore control/trace and telemetry. Representative: `llvm.tpu.sc.strace`, `llvm.tpu.event`, `llvm.tpu.spill.debug`, `llvm.tpu.mprefix`, `llvm.tpu.read.global.cycle.count`, `llvm.tpu.read.local.cycle.count`, `llvm.tpu.ssetpstate`, `llvm.tpu.sc.ssettm`, `llvm.tpu.sc.dma.core.id`, `llvm.tpu.sc.sint`. (`MEDIUM` — boundary with task/structural.)

---

## Sort / Unique / dupcount

11 ops. SparseCore sort/dedup unit — embedding-dedup primitives. See [`sparsecore/dedup-multiplicity.md`](../sparsecore/dedup-multiplicity.md) and [`sparsecore/rank-and-permute-radixsort.md`](../sparsecore/rank-and-permute-radixsort.md).

| Intrinsic | What it does |
|---|---|
| `llvm.tpu.sort.ascdf` / `.ascdi` | ascending sort, float / int |
| `llvm.tpu.sort.dscdf` / `.dscdi` | descending sort, float / int |
| `llvm.tpu.uniquef` / `.uniquei` | unique-reduce, float / int |
| `llvm.tpu.dupcntf` / `.dupcnti` | duplicate-count, float / int |
| `llvm.tpu.vmctz` | vector count-trailing-zeros |
| `llvm.tpu.vmpcnt.ones` | vector mask popcount |

---

## Task / Control / structural

10 ops. SparseCore tile-task + structural ops. Representative: `llvm.tpu.task.dispatch`, `llvm.tpu.task.dispatch.clear.ibuf`, `llvm.tpu.loop.name`, `llvm.tpu.loop.parallel`, `llvm.tpu.barrier`, `llvm.tpu.nop`, `llvm.tpu.delay`, `llvm.tpu.tileid`, `llvm.tpu.halt.trap`, `llvm.tpu.capture.hbm.stack`/`init.stack`. The `tpu_tileid` op (typed `(Type)` → 0-operand id) reads the `STILEID` register; it is the tile-id source consumed by the per-core `addrspacecast` casts. (`MEDIUM` — overlaps with control-register and pointer families on stack ops.)

---

## i1-mask Width Conversion

5 ops. VPU mask slot — vector-mask width re-pack. The complete set: `llvm.tpu.8i1.to.16i1`, `llvm.tpu.8i1.to.32i1`, `llvm.tpu.16i1.to.8i1`, `llvm.tpu.16i1.to.32i1`, `llvm.tpu.32i1.to.8i1`. See [`sparsecore/m-register-predicate.md`](../sparsecore/m-register-predicate.md) and [`isa/slot-vcreate-mask-mregister.md`](../isa/slot-vcreate-mask-mregister.md).

---

## ODS Operand Shapes (representative)

466 of 1356 carry a typed `create(OpBuilder&, Location, …)` whose argument list is the ODS declaration; the other 890 use the generic default builder (shape inferred from name-family arity + `verifyInvariantsImpl` presence). `T` = result Type, `V` = Value operand.

| Intrinsic (family) | create args (after Location) | Shape |
|---|---|---|
| `tpu_addrspacecast` (addrspacecast) | `T, V` | 1 res, 1 opnd |
| `tpu_addrspacecast_smem` (addrspacecast) | `T, V, V` | +tile-window |
| `tpu_dma_*_sc_simple` (DMA) | `V×8` | 8-field descriptor |
| `tpu_dma_*_sc_single_strided` (DMA) | `V×11` | +stride triple |
| `tpu_dma_*_sc_general` (DMA) | `V×16` | multi-dim |
| `tpu_stream_*_{gather,scatter}_*` (stream) | `V×6` | stream descriptor |
| `tpu_syncadd` (sync) | `V, V` | sflag, delta |
| `tpu_syncadd_remote` / `syncset_remote` (sync) | `V×5` | +dev/core/id |
| `tpu_fetch_and_add` (wait) | `V, V, V` | sflag, addr, val |
| `tpu_waitge` / `waiteq` / … (wait) | `V, V` | sflag, threshold |
| `tpu_alloca_smem` / `alloca_sflag` (alloca) | `T, V` | result, size |
| `tpu_rdcbreg_offset` / `rdcbreg_size` (CBREG) | `T, V` | result, cbreg |
| `tpu_wrcbreg_offset` (CBREG) | `T, V, V` | cbreg, value |
| `tpu_cbreg_add_offset` (CBREG) | `T, V, V` | cbreg, delta |
| `tpu_inttoptr` / `ptrtoint` (ptr) | `T, V` | result, val |
| `tpu_setreg_sflagrange` (ctl-reg) | `V` | range value |
| `tpu_sin_macro` / `tpu_*_macro` (EUP) | `T, V` | result, operand |
| `tpu_tileid` (task/struct) | `T` | 0-operand id |
| `tpu_barrier` (task/struct) | `V, V, V` | barrier args |
| `tpu_delay` (task/struct) | `V` | cycles |

The 890 default-builder ops (bare transcendentals `tpu_sin`/`tpu_rcp`, the `tpu_*i1_to_*i1`, `scan2xN`, pack/unpack, `vld`/`vst`, `rdreg_*` counters, `sld_cb`, `eup_pop`) use the generic `(TypeRange, ValueRange, ArrayRef<NamedAttribute>)` builder — result type inferred (`SameOperandsAndResultType`/`InferType`), operand count by name-family arity.

---

## Default-Builder Arity (byte-read from the ODS trait pack)

The 890 default-builder ops do **not** need their operand count guessed from name-family heuristics: TableGen bakes the result-count and operand-count traits into the mangled `Op<…>` class template, and that template name is byte-present in the symbol table. Every `tpu_*` op is instantiated as

```text
mlir::Op<sparse_core::tpu_NAME,
         OpTrait::ZeroRegions, OneResult|ZeroResults,
         OneTypedResult<mlir::Type>::Impl, ZeroSuccessors,
         OneOperand | ZeroOperands | NOperands<Lj N>::Impl,
         OpInvariants [, MemoryEffectOpInterface::Trait]>
```

and that trait list appears verbatim inside the per-op `getHasTraitFn` / `getFoldHookFn` / `printAssembly` callback symbols (`nm` token `OpINS…sparse_core<len>tpu_NAME EJ … OpInvariants`). The operand count is the literal `OneOperand` (1), `ZeroOperands` (0), or `NOperands<Lj N>` (N) token; the result count is `OneResult` (1) vs `ZeroResults` (0). No disassembly is required — the arity is a string in the symbol.

> **NOTE — the result `TypeConstraint` is *not* a refined `Vreg`/`Mask`/`Scalar`/`Ptr` predicate at this layer.** Every op's result trait is the generic `OneTypedResult<mlir::Type>` — `mlir::Type`, not a register-class subtype. The verifier body (`tpu_NAME::verifyInvariantsImpl`) discharges its single result-type check through one shared constraint function, `__mlir_ods_local_type_constraint_…llvm_tpu_ops1` (e.g. `@0x149de120`), whose *entire* body is `if (!LLVM::isCompatibleOuterType(t)) emitOpError(...)` (`call isCompatibleOuterType @0x17473060` at `149de146`, byte-verified). The `StringRef` argument that distinguishes call sites is only the diagnostic role label — `"result"` (`.rodata @0x84f7815`) vs `"operand"` (`.rodata @0x86f4942`), read by `xxd` — not a predicate selector. There is **no per-op `Vreg`/`Mask`/`Scalar`/`Ptr` result constraint encoded in this MLIR layer**; the register-class refinement lives only in the LLVM intrinsic signature consumed downstream by ISel, not in the ODS verifier. So the "result `TypeConstraint`" half of this gap is *not statically separable per op* here — every leaf carries the same generic LLVM-compatible-type result check. (`HIGH`)

### Arity distribution — 1060 of 1356 byte-read

Parsing the `OneResult`/`ZeroResults` × `OneOperand`/`ZeroOperands`/`NOperands<Lj N>` token from each op's `Op<…>` symbol recovers the exact `(#results, #operands)` shape for **1060 distinct `tpu_*` ops** (the 296 not listed have their `Op<…>` pack emitted only inline and carry no standalone callback symbol; their arity follows the same name-family pattern). The full byte-read distribution:

| #res | #operands | # ops | Dominant family in this bucket |
|---:|---:|---:|---|
| 0 | 8 | 358 | `stream_{strided,indirect}_*` (342) + `dma_*_sc_simple` non-iova-windowed (16) |
| 1 | 1 | 147 | bare EUP (`sin`/`rcp`/`eup_pop`/`sigshft`/`exponent`), `unpackl`/`unpacku`, `inttoptr`/`ptrtoint`, `i1`-width casts |
| 0 | 6 | 118 | `stream_linear_*` + `sync{add,set}_remote_*` |
| 0 | 9 | 114 | `stream_indirect_vreg_vreg_*` (gather+scatter) |
| 1 | 2 | 94 | `sld_cb`, `pack_c_*`, `dupcnt{f,i}`, `uniquef`, `sshllo`, `vclass`, `wrcbreg_*` |
| 0 | 2 | 51 | `syncadd`/`waiteq`/… (sflag, threshold) |
| 0 | 4 | 29 | strided / multi-arg sync + vst variants |
| 0 | 1 | 26 | `delay`, single-arg stores |
| 1 | 0 | 25 | `tileid` + all `rdreg_*` counters (0-operand register reads) |
| 0 | 5 | 23 | sync/dma mid-tier |
| 0 | 3 | 21 | `vst_msk`-class stores, `fetch_and_add`-class, `barrier` |
| 1 | 3 | 14 | `sort_{asc,dsc}d{f,i}`, 3-operand vector ops |
| 0 | 16 | 12 | `dma_*_sc_general` |
| 1 | 4 | 9 | 4-operand typed ops |
| 0 | 11 | 10 | `dma_*_sc_single_strided` (non-iova) |
| 0 | 0 | 6 | `nop`-class zero-everything ops |
| 0 | 12 | 2 | `dma_{hbm_to_iova,iova_to_hbm}_sc_single_strided` (iova adds one operand) |
| 1 | 10 | 1 | `tpu_sfence` (10 sflag operands) |

Sum = 1060.

### Byte-verified leaf sample (token read straight from the symbol)

Each row's `#res`/`#operands` is the `OneResult`/`ZeroResults` and `OneOperand`/`ZeroOperands`/`NOperands<Lj N>` token extracted from that op's mangled `Op<…sparse_core…tpu_NAME EJ…OpInvariants>` callback symbol in the `nm` table — the exact string is the evidence.

| Intrinsic (class) | result token | operand token | (#res, #opnd) |
|---|---|---|---:|
| `tpu_sin` (bare EUP) | `OneResult` | `OneOperand` | (1, 1) |
| `tpu_addrspacecast` | `OneResult` | `OneOperand` | (1, 1) |
| `tpu_tileid` | `OneResult` | `ZeroOperands` | (1, 0) |
| `tpu_vld_msk` | `OneResult` | `NOperands<Lj2>` | (1, 2) |
| `tpu_sld_cb` | `OneResult` | `NOperands<Lj2>` | (1, 2) |
| `tpu_pack_c_b32_b16` | `OneResult` | `NOperands<Lj2>` | (1, 2) |
| `tpu_sort_ascdf` | `OneResult` | `NOperands<Lj3>` | (1, 3) |
| `tpu_vst_msk` | `ZeroResults` | `NOperands<Lj3>` | (0, 3) |
| `tpu_syncadd` | `ZeroResults` | `NOperands<Lj2>` | (0, 2) |
| `tpu_stream_linear_gather_hbm_to_tilespmem` | `ZeroResults` | `NOperands<Lj6>` | (0, 6) |
| `tpu_stream_strided_gather_hbm_to_tilespmem` | `ZeroResults` | `NOperands<Lj8>` | (0, 8) |
| `tpu_stream_indirect_vreg_vreg_gather_hbm_to_tilespmem` | `ZeroResults` | `NOperands<Lj9>` | (0, 9) |
| `tpu_dma_hbm_to_spmem_sc_single_strided` | `ZeroResults` | `NOperands<Lj11>` | (0, 11) |
| `tpu_dma_hbm_to_iova_sc_single_strided` | `ZeroResults` | `NOperands<Lj12>` | (0, 12) |
| `tpu_sfence` | `OneResult` | `NOperands<Lj10>` | (1, 10) |

> **QUIRK — the stream family is *not* uniform `V×6`.** The [§Stream](#stream-engine-the-dominant-family) typed-create example shows a 6-operand linear form, but the byte-read arity splits the 834 stream ops into **three operand counts keyed by addressing pattern**: `stream_linear_*` = 6, `stream_{strided,indirect}_*` = 8, `stream_indirect_vreg_vreg_*` = 9. The extra operands carry the stride/index/vreg-offset sources the more complex patterns need. A reimplementer who builds every stream op with a fixed 6-operand list will under-supply the strided/indirect forms. Likewise the DMA `single.strided` tier is **11 operands normally but 12 when an `iova` endpoint is involved** (`dma_hbm_to_iova` / `dma_iova_to_hbm`), refining the flat "11 Value" in [§DMA](#dma-descriptor-builders).

> **NOTE — coverage, no silent cap.** 15 leaves are byte-verified above (token read directly from each op's mangled `Op<…>` symbol), spanning EUP / addrspacecast / task / vld / cbreg / pack / sort / vst / sync / stream-{linear,strided,indirect-vreg} / dma-{single-strided,iova} / fence families. The full `(#res, #operands)` shape is byte-read for **1060 of the 1356** ops (every one whose `Op<…>` pack survives as a standalone callback symbol); the distribution table above is the exact census of those 1060. The remaining 296 ops emit their trait pack only inline and are **not** individually transcribed — their arity follows the same name-family layout (each is one `rg -o 'sparse_core…tpu_NAME EJ…OpInvariants'` token away). This covers the **arity** half of the "890 default-builder ops" gap in full; the **result-`TypeConstraint`** half is closed by the NOTE above (uniformly generic `mlir::Type` + `isCompatibleOuterType`, no per-op `Vreg`/`Mask`/`Scalar`/`Ptr`).

---

## Per-Intrinsic IntrProperties (LLVM attribute table)

Each `llvm.tpu.*` intrinsic carries an `IntrProperties` set — the `IntrNoMem`/`IntrArgMemOnly`/`IntrReadMem`/`IntrWillReturn`/… bits TableGen lowers into the LLVM function-attribute list returned by `llvm::Intrinsic::getAttributes` (`@0x1da0b460`). This is the backend's alias-analysis and scheduling contract for the call. The set is byte-recoverable from a two-table lookup; the sample below is read directly from those tables.

### The Lookup (byte-decoded from `getAttributes`)

`getAttributes(ctx, ID, FT)` reads `IntrinsicsToAttributesMap` (`_ZL25IntrinsicsToAttributesMap @0x416fb30`, a `uint16_t[17648]`, one entry per LLVM intrinsic ID, indexed `[ID−1]`). The packed `uint16` splits:

| Field | Bits | Decode site (in `getAttributes`) |
|---|---|---|
| arg-attr-set index | `[8:0]` (`& 0x1ff`) | `1da0b4ab: and $0x1ff,%ecx` → `ArgAttributesInfoTable @0x4178510` (4-byte stride: `1da0b4c0: movzwl 0x2(%rdx,%rsi,4)`) |
| **fn-attr-set index** | `[15:9]` (`>> 9`) | `1da0b564: shr $0x9,%esi`; `0x7f` = "no fn attrs" sentinel (`1da0b567: cmp $0x7f`) |

The fn-attr-set index selects a case in `getIntrinsicFnAttributeSet` (`_ZL26getIntrinsicFnAttributeSet @0x1da0d460`) via the jump table at `0xb550f54` (`int32` rel-offsets, indexed by set ID). Each case is a fixed sequence of `Attribute::get(ctx, AttrKind, 0)` calls (`@0x1d912ee0`) optionally followed by one `Attribute::getWithMemoryEffects(ctx, ME)` (`@0x1d9139a0`). The `AttrKind` immediate in `mov $0xNN,%esi` before each call is the LLVM `Attribute::AttrKind` enum value; the `ME` immediate is the `MemoryEffects` bitmask.

**AttrKind enum values** (byte-read from the `mov $imm,%esi` operands; names cross-checked against the binary's `IRAttribute<AttrKind N>`/`AA*Impl` template instantiations in the symbol table): `0x2c`=44 NoUnwind, `0x50`=80 WillReturn, `0x47`=71 Speculatable (`INFERRED` name — enum value byte-read, name from the NoMem+WillReturn pure-intrinsic pairing; no Attributor AA carries 71). **MemoryEffects bitmask** (2 bits/location, ModRef = Ref(1)/Mod(2)/ModRef(3), locations ArgMem/InaccessibleMem/Other): `0x0`=`memory(none)` (IntrNoMem), `0x555`=`memory(read)` (IntrReadMem), `0xaaa`=`memory(write)` (IntrWriteMem), `0x3`=`memory(argmem: readwrite)` (IntrArgMemOnly), `0x1`=`memory(argmem: read)`, `0x2`=`memory(argmem: write)`, `0xc`=`memory(inaccessiblemem: readwrite)`; **absent** = full unmodeled ModRef (side-effecting).

### The 12 fn-attr sets the `llvm.tpu.*` surface uses

Census over all 1356 (fn-attr-set index read from each intrinsic's map entry; each set's contents decoded by tracing its `getIntrinsicFnAttributeSet` case to the `AttributeSetNode::get` finalizer `@0x1da0f134`):

| Set | # of 1356 | Enum attrs | Memory effect | LLVM `IntrProperties` shorthand |
|---:|---:|---|---|---|
| 11 | 215 | NoUnwind | `memory(none)` | `IntrNoMem` |
| 13 | 128 | NoUnwind | `memory(argmem: readwrite)` | `IntrArgMemOnly` |
| 112 | 843 | NoUnwind | `memory(argmem: readwrite)` | `IntrArgMemOnly` |
| 14 | 8 | NoUnwind | `memory(argmem: read)` | `IntrReadMem`+`IntrArgMemOnly` |
| 32 | 11 | NoUnwind | `memory(read)` | `IntrReadMem` |
| 34 | 8 | NoUnwind, WillReturn | `memory(write)` | `IntrWriteMem`+`IntrWillReturn` |
| 36 | 26 | NoUnwind, WillReturn | `memory(write)` | `IntrWriteMem`+`IntrWillReturn` |
| 83 | 8 | NoUnwind | `memory(argmem: write)` | `IntrWriteMem`+`IntrArgMemOnly` |
| 108 | 50 | NoUnwind, WillReturn, Speculatable | `memory(none)` | `IntrNoMem`+`IntrWillReturn`+`IntrSpeculatable` |
| 114 | 19 | NoUnwind | `memory(inaccessiblemem: readwrite)` | `IntrInaccessibleMemOnly` |
| 5 | 19 | NoUnwind | *(none)* | side-effecting (full ModRef) |
| 10 | 21 | NoUnwind | *(none)* | side-effecting (full ModRef) |

Sum: `215+128+843+8+11+8+26+8+50+19+19+21 = 1356` — exact, every `llvm.tpu.*` intrinsic maps to one of these 12 sets.

### Byte-verified per-leaf sample (name → ID → map entry → set)

Each row: intrinsic name, its LLVM intrinsic ID (storage-order index in `IntrinsicNameTableStorage @0x4179440`, where `not_intrinsic`=0, `llvm.abs`=1, …), the `uint16` read from `IntrinsicsToAttributesMap[ID−1]`, and the decoded fn-attr-set.

| Intrinsic | ID | Map `uint16` | fn set | Resolved `IntrProperties` |
|---|---:|---|---:|---|
| `llvm.tpu.addrspacecast` | `0x33b1` | `0x1601` | 11 | `IntrNoMem` |
| `llvm.tpu.pack.c.b32.b16` | `0x3453` | `0x1601` | 11 | `IntrNoMem` |
| `llvm.tpu.vcvt.f32.bf16` | `0x384e` | `0x1601` | 11 | `IntrNoMem` |
| `llvm.tpu.16i1.to.32i1` | `0x33a1` | `0x1601` | 11 | `IntrNoMem` |
| `llvm.tpu.nop` / `llvm.tpu.delay` / `llvm.tpu.sfence` | — | `0x1601` | 11 | `IntrNoMem` |
| `llvm.tpu.dma.hbm.to.spmem.sc.simple` | `0x3408` | `0x1afb` | 13 | `IntrArgMemOnly` |
| `llvm.tpu.dma.hbm.to.spmem.sc.general` | `0x3407` | `0x1afa` | 13 | `IntrArgMemOnly` |
| `llvm.tpu.syncadd` | `0x37e8` | `0x1a01` | 13 | `IntrArgMemOnly` |
| `llvm.tpu.waiteq` | `0x38bb` | `0x1a01` | 13 | `IntrArgMemOnly` |
| `llvm.tpu.stream.linear.gather.add.f32.hbm.to.tilespmem` | `0x36ce` | `0xe0ff` | 112 | `IntrArgMemOnly` |
| `llvm.tpu.vst.msk.idx.add` (+ all indexed scatter-stores) | — | `0x…ff`/`0xe0…` | 112 | `IntrArgMemOnly` |
| `llvm.tpu.vld.cb.msk` / `llvm.tpu.rdcbreg.offset` / `llvm.tpu.sin.macro` | `0x346a`/`0x3498` | `0x4001` | 32 | `IntrReadMem` |
| `llvm.tpu.vst.cb.msk.add` / `llvm.tpu.vst.cb.msk` | `0x387e` | `0x4801` | 36 | `IntrWriteMem`+`IntrWillReturn` |
| `llvm.tpu.rcp` / `rsqrt` / `tanh` / `sort.ascdf` / `add.scan1xNf` | `0x3468`/`0x349b`/`0x33ac` | `0xd801` | 108 | `IntrNoMem`+`IntrWillReturn`+`IntrSpeculatable` |
| `llvm.tpu.rdreg.gtc.hi` / `read.global.cycle.count` | `0x3474` | `0x1401` | 10 | side-effecting (full ModRef) |
| `llvm.tpu.fetch.and.add` / `task.dispatch` / `eup.pop` | `0x342d`/`0x3802` | `0x0a01` | 5 | side-effecting (full ModRef) |

The map `uint16` for each is the exact little-endian halfword at file offset `0x416fb30 + (ID−1)*2`; e.g. ID `0x33b1` reads `01 16` at `0x4176290` (= `0x416fb30 + (0x33b1−1)·2`) → `0x1601` → `arg = 0x1601 & 0x1ff = 1`, `fnset = 0x1601 >> 9 = 11`.

> **NOTE — coverage, no silent cap.** 16 representative leaves are byte-verified here against the map (spanning addrspacecast / pack / convert / mask-width / DMA / sync / wait / stream / CBREG load+store / EUP-macro / transcendental / scan / sort / control-reg / atomic / task families), and **all 12 fn-attr sets are byte-decoded in full** from `getIntrinsicFnAttributeSet`. The per-leaf set assignment for the remaining 1340 intrinsics is *not* individually transcribed, but the fn-set census above is exact (sums to 1356) and the lookup is deterministic: `set = (IntrinsicsToAttributesMap[ID−1] >> 9)`, `ID` from the storage order of `IntrinsicNameTableStorage @0x4179440`. A reimplementer reads any leaf's `IntrProperties` with one halfword load + one table dispatch.

> **GOTCHA —** the dominant fn-set is **112 (843 ops, ≈ all 834 stream + 9 indexed scatter-stores) = `nounwind memory(argmem: readwrite)` = `IntrArgMemOnly`**, *not* `IntrNoMem`. A reimplementer who marks the stream ops `IntrNoMem` (because they look like pure data movers) will let the scheduler hoist/CSE/dead-code-eliminate them across the embedding table they actually read and write — a correctness bug. The stream engine touches argument-pointed memory, so `argmem: readwrite` is the binary's verdict. Conversely the pure-math set 108 (`rcp`/`rsqrt`/`tanh`/`sort`/`scan`) is the only set carrying `IntrSpeculatable` — those are the safely-hoistable ones.

---

## Registration Binding

`mlir::sparse_core::registerLlvmTpuDialectOperations` `@0x146d0560` tail-calls 10 batch sub-registrars; each `RegisteredOperationName::insert`s ~135 ops (the TableGen op-registration split into ≤256-op batches to bound per-function instantiation size). 10 × ~135 = 1356.

| Sub-registrar | Address |
|---|---|
| `registerLlvmTpuDialectOperations0` | `0x146d05c0` |
| `registerLlvmTpuDialectOperations1` | `0x1472bea0` |
| `registerLlvmTpuDialectOperations2` | `0x1478b500` |
| `registerLlvmTpuDialectOperations3` | `0x147e1c40` |
| `registerLlvmTpuDialectOperations4` | `0x14835b80` |
| `registerLlvmTpuDialectOperations5` | `0x148891c0` |
| `registerLlvmTpuDialectOperations6` | `0x148dc3c0` |
| `registerLlvmTpuDialectOperations7` | `0x1492d5c0` |
| `registerLlvmTpuDialectOperations8` | `0x14982d00` |
| `registerLlvmTpuDialectOperations9` | `0x149d88e0` (final batch) |

> **NOTE —** these intrinsics register through this separate 10-batch `LlvmTpuDialect` path, *not* through the high-level `ScDialect` 115-op `addOperations` `@0x14594f60`. A reimplementer tracing only the `ScDialect` registration will see "none distinct" for the intrinsic surface and miss all 1356.

---

## Per-Generation Variation

The 1356 count is the union for the generations this build targets; the intrinsic surface grows per generation. The dimensions that vary:

| Source of variation | Effect |
|---|---|
| New dtypes (FP8 `e4m3`/`e5m2`, narrow ints) | adds stream/convert/pack op rows per dtype |
| New stream patterns / memspaces | extends the 834-way stream cross-product |
| New EUP transcendental selectors | adds transcendental + `.macro` pairs |
| New address spaces | adds `addrspacecast` leaf variants |
| Generation-gated ops | a name may be absent on older gens (the `getSequencerType` / EmitX gen dispatch gates which are reachable) |

The deep per-gen reachability is on [`isa/sequencer-ops-per-gen.md`](../isa/sequencer-ops-per-gen.md) and [`isa/v5plus-emitx-bit-positions.md`](../isa/v5plus-emitx-bit-positions.md). This appendix snapshots the full registered set for *this* build; a reimplementer targeting a single generation must gate names against the generation's EmitX dispatch.

---

## The Stream Command Is Composed, Not Per-Leaf

The 834-way `llvm.tpu.stream.*` explosion was the prime suspect for a hidden per-leaf
numeric command table: 834 distinct ops *looks like* 834 distinct hardware opcodes. The
binary says otherwise. The numeric command the SparseCore stream sequencer consumes is
**assembled** from four orthogonal `SparseCoreStream` proto bitfields at encode time; there
is no static array indexed by `(pattern,verb,dtype,memspace)` and no per-intrinsic command
constant. The 834 leaves collapse onto **4 addressing forms × an 8-value verb × a dtype bit
× a memspace enum**, packed into one slot. This was confirmed by reading the encoder's
`oneof` dispatch and each field accessor directly — addresses are gfc/TPU7x; `.text`
VA==file-offset at `0xe63c000`.

### The encoder dispatches on the *form*, not the leaf

`SparseCoreStreamEncoder::Encode` @ `0x1eb9b4c0` selects what to encode by reading the proto
`oneof` discriminator and bounding it at the message's field count — **not** by an
intrinsic-ID-keyed table:

```
1eb9b55e: mov    0x58(%r15),%eax        ; oneof discriminator (which addressing form)
1eb9b562: cmp    $0xa,%rax              ; bound = 0xa  → at most 11 cases (fields 0..10)
1eb9b566: ja     1eb9bd64               ; default/error arm
1eb9b571: lea    -0x13363470(%rip),%rcx ; jump table @0xb838108 (11 × int32 rel offsets)
1eb9b5a2: cmpl   $0x8,0x58(%r15)        ; field #8 == LinearStream
1eb9b5a7: lea    SparseCoreStream_LinearStream_globals_(%rip),%r12
```

The jump table at `.rodata` `0xb838108` is **11 entries** (fields 0–10), and `xxd` shows
entries 8/9/10 are the only ones with distinct targets — the Linear/Strided/Indirect form
arms — while fields 0–7 share the default arm. A 745 MB binary registering 834 separate
stream ops still routes them all through an 11-case switch: the explosion is in the *MLIR op
roster*, not in any HW-command table. (The SCS encoder bounds at `0xa`; the TEC encoder
bounds at `0xb` to admit the TEC-only `IndirectVregStream` 4th form — see
[Indirect Vreg Stream](../sparsecore/indirect-vreg-stream.md).)

### The four command fields, each byte-verified from its accessor

Each axis of the `(pattern,verb,dtype,memspace)` tuple is a separate bitfield with its own
`GetConcatenatedValue`/`Matches` accessor. The shift/mask read straight from the
disassembly is the field's exact slot position and width:

| Axis | Field | Accessor @ | Byte-read body | Slot bits | Verified value(s) |
|------|-------|-----------|----------------|-----------|-------------------|
| **pattern** (form) | form opcode | `0x1eb9aa60` Linear | `(q[+0x18] & 0x7E0…<<52)==0x76…<<52` | bits 53–58 | Linear = `0x76>>1` = **`0x3b`** |
| | | `0x1eb9aa80` Strided | `…==0x74…<<52` | bits 53–58 | Strided = `0x74>>1` = **`0x3a`** |
| | | `0x1eb9aaa0` Indirect | `…==0x72…<<52` | bits 53–58 | Indirect = `0x72>>1` = **`0x39`** |
| **verb** | `StreamOpcode` | `0x1eb9b3a0` | `(d[+0x18] >> 9) & 7` | +0x18 bit 9, w3 | GATHER=0 … SCATTER_FLOAT_ADD=6 |
| **dtype** | `GatherScatterAddIsB16` | `0x1eb9b3c0` | `(d[+0x18] >> 0xc) & 1` | +0x18 bit 12, w1 | bf16-add = 1, f32-add = 0 |
| **memspace** | `OffTileMemoryType` | `0x1eb9b420` | `(q[+0x10] >> 0x2f) & 7` | +0x10 bit 47, w3 | SPMEM=0 · TILE_SPMEM_N=1 · HBM=2 · HBM_4B=3 |

Worked sample — 12 representative leaves and the *composed* slot command each produces. The
command is the tuple `(form, StreamOpcode, IsB16, OffTileMemoryType)`; no single integer is
assigned per leaf, so the "command" column is the byte-derived field assembly:

| Stream leaf (pattern × verb × dtype × memspace) | form | verb | IsB16 | memspace | byte-evidence |
|---|---|---|---|---|---|
| `stream_linear` gather → HBM, f32 | `0x3b` | 0 | 0 | 2 | Linear `Matches`==`0x76`<<52; verb `>>9&7`=0 |
| `stream_linear_add` scatter-f32-add → HBM | `0x3b` | 6 | 0 | 2 | `StreamOpcode>>9&7`=6; `IsB16>>0xc&1`=0 |
| `stream_strided` gather → HBM, f32 | `0x3a` | 0 | 0 | 2 | Strided `Matches`==`0x74`<<52 |
| `stream_strided_add` scatter-f32-add | `0x3a` | 6 | 0 | 2 | form `0x3a`; verb 6 |
| `stream_indirect` gather → HBM, f32 | `0x39` | 0 | 0 | 2 | Indirect `Matches`==`0x72`<<52 |
| `stream_indirect` gather → HBM_4B | `0x39` | 0 | 0 | 3 | `OffTileMemoryType>>0x2f&7`=3 |
| `stream_indirect_add` scatter-f32-add → HBM | `0x39` | 6 | 0 | 2 | verb 6, IsB16 0 |
| `stream_indirect_add` scatter-bf16-add → HBM | `0x39` | 6 | 1 | 2 | `IsB16>>0xc&1`=1 |
| `stream_indirect` gather-f32-add → HBM | `0x39` | 2 | 0 | 2 | verb `GATHER_FLOAT_ADD`=2 |
| `stream_indirect` gather-int-add → HBM | `0x39` | 1 | — | 2 | verb `GATHER_INTEGER_ADD`=1 |
| `stream_indirect` scatter → SPMEM pool | `0x39` | 4 | 0 | 0 | verb `SCATTER`=4; memspace 0 |
| `stream_indirect_vreg` gather (TEC-only) | `0x38` | 0 | 0 | 2 | TEC oneof bound `0xb`; form 4th case |

> **NOTE — no silent cap; this is a coverage-honest negative result.** Of the **834**
> stream leaves, **0** have an individually-byte-dumped per-leaf command integer, because
> **no such integer exists** — the search for one terminated at the encoder's 11-case
> `oneof` switch (`cmp $0xa`/`$0xb`) and four orthogonal field accessors. What *is*
> byte-verified: **3 of 4** form opcodes (`0x3b`/`0x3a`/`0x39` directly from their `Matches`
> constants; the 4th, IndirectVreg `0x38`, is documented on the
> [sibling page](../sparsecore/indirect-vreg-stream.md) as TEC-only) and **all four** command
> fields' shift/mask. Any one leaf's command is therefore *reconstructable* from its
> `(pattern,verb,dtype,memspace)` name decomposition without a table — but the table itself
> does not exist to dump. A reimplementer must build the slot by *packing these four fields*,
> not by looking up a leaf opcode.

> **GOTCHA — `(verb, dtype, memspace)` are independent of the op identity at the slot.** Two
> distinct intrinsics (`stream_indirect` vs `stream_indirect_add`) differ only by the 3-bit
> `StreamOpcode` they set; the bf16-vs-f32 split is one bit, not two ops with two opcodes;
> the HBM-vs-HBM_4B split is the memspace enum, not a third op family. The 834-way ISel
> roster encodes these *as op identity* (see the [§Stream GOTCHA](#stream-engine-the-dominant-family)
> above), but they *converge onto* the same four-field slot. The per-leaf "opcode" a
> reimplementer might expect is an artifact of the MLIR-layer op explosion, not a HW command
> number.

Deep field semantics, the full encode/decode bit map, and the `StreamOpcode`/`OffTileMemoryType`
enum rosters live on [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md); this
section's contribution is the proof that the per-leaf command gap was a *category error* — the
command is composed, and the composition is the four fields above.

---

## What Is Not Enumerated Here

Honest gaps in this catalog:

- **Per-leaf stream→HW opcode** — *resolved as a negative result* (see [§The Stream Command Is Composed, Not Per-Leaf](#the-stream-command-is-composed-not-per-leaf)): there is **no** 834-entry static per-leaf command table. The numeric slot command the SparseCore stream sequencer consumes is *assembled at lowering time* from four orthogonal `SparseCoreStream` proto bitfields — a 6-bit addressing-**form** opcode plus 3-bit **verb** (`StreamOpcode`), a 1-bit **dtype** select (`GatherScatterAddIsB16`), and a 3-bit **memspace** (`OffTileMemoryType`) — each read by its own confirmed accessor. The `(pattern,verb,dtype,memspace)` choice is not one opcode; it is the *Cartesian assembly* of these fields. The earlier `INFERRED` "per-leaf opcode" framing was wrong: there is nothing per-leaf to byte-dump because the encoder switch is bounded at the 4 forms, not the 834 leaves.
- **Per-intrinsic LLVM `IntrProperties`** — *recovered* (see [§Per-Intrinsic IntrProperties](#per-intrinsic-intrproperties-llvm-attribute-table)): all 12 fn-attr sets the surface uses are byte-decoded and the per-set census is exact (sums to 1356); the `set = IntrinsicsToAttributesMap[ID−1] >> 9` lookup is deterministic. Only the per-leaf assignment for the 1340 non-sampled IDs is not individually transcribed (each is one halfword load away). The OpInterface presence on the MLIR side (`MemoryEffect` 285, `AliasAnalysis` 546, `AccessGroup` 180, `Bytecode` 188) is the dialect-layer counterpart.
- **The 890 default-builder ops' exact arity + result `TypeConstraint`** — *arity recovered* (see [§Default-Builder Arity](#default-builder-arity-byte-read-from-the-ods-trait-pack)): the `(#results, #operands)` shape is byte-read from each op's mangled `Op<…OneResult/ZeroResults…OneOperand/NOperands<Lj N>…>` trait pack for 1060 of the 1356 ops, with the full distribution tabulated. The result `TypeConstraint` half is *resolved as a negative result*: there is no per-op `Vreg`/`Mask`/`Scalar`/`Ptr` predicate at the MLIR layer — every result trait is the generic `OneTypedResult<mlir::Type>` and the verifier discharges it through one shared `isCompatibleOuterType` check; the register-class refinement is carried only by the downstream LLVM intrinsic signature, not the ODS verifier.
- **The scan/sort/unique-engine opcode bit layouts** — mapped to the SparseCore scan/sort/dedup units by name; the per-op HW command bit layout is not decoded (SparseCore-specific compute units, not TensorCore LLO slots).
- **The full numeric address-space ID table** — the `AddressSpaceDescription` switch base (201) and sampled case strings are known; the complete ID↔space map (and which `addrspacecast` leaf casts between which numeric IDs) needs the full switch-arm walk.

---

## Cross-References

- [SparseCore Overview](../sparsecore/overview.md) — the subsystem these intrinsics are the bottom-of-stack ISA for
- [SparseCore Backend Pipeline](../sparsecore/sc-backend-pipeline.md) — where `LowerToSparseCoreLlvm` sits in the lowering chain
- [Stream Gather/Scatter](../sparsecore/stream-gather-scatter.md) — semantics of the 834-op dominant family
- [Indirect Vreg Stream](../sparsecore/indirect-vreg-stream.md) — the CBREG-windowed indirect stream forms
- [CBREG Circular-Buffer Register](../sparsecore/cbreg.md) — the 16-CBREG-per-bank ops the CBREG family drives
- [AddrSpaceCast ISel](../sparsecore/addrspacecast-isel.md) — why the 16 casts survive as IR intrinsics, not `0xf4` nodes
- [Tile-ID Cast](../sparsecore/tile-id-cast.md) — the tile-id operand to the per-core casts
- [Scan Datapath](../sparsecore/scan-datapath.md) / [Segmented Scan](../sparsecore/segmented-scan.md) — the scan/reduce family
- [Sort, Rank, Radixsort](../sparsecore/rank-and-permute-radixsort.md) — the sort/unique/permute families
- [Scalar Opcode Enum](../sparsecore/scalar-opcode-enum.md) / [Vector Opcode Enum](../sparsecore/vector-opcode-enum.md) — SC scalar/vector slot opcodes the intrinsics lower to
- [EUP / Transcendental Slot](../isa/slot-eup-transcendental.md) — the EUP push selector values for the transcendental family
- [VPU Slot](../isa/slot-vpu.md) — pack/unpack/convert/lane/mask LLO encodings
- [Memory Load Slot](../isa/slot-memory-load.md) / [Memory Store Slot](../isa/slot-memory-store.md) — the `vld`/`vst` slot encodings
- [SPU Scalar Slot](../isa/slot-spu-scalar.md) — the scalar-ALU and control-register slot
- [Pack/Unpack Precision](../isa/pack-unpack-precision.md) — the sub-byte width ladder
- [LLO Opcode Enum](../isa/llo-opcode-enum.md) — the LLO op numbers the family lowering targets
- [LLO Opcode Table](llo-opcode-table.md) — sibling appendix, the LLO opcode master list
- [Memory Space Table](memory-space-table.md) — sibling appendix, the address-space IDs the stream/dma/alloca families reference
- [ISA Overview](../isa/overview.md) — the LLO slot/bundle model these intrinsics feed
