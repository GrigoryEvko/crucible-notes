# DmaParameters Selector

> *All addresses on this page apply to `libtpu.so` from the `libtpu-0.0.40-cp314` wheel (build `libtpu_lts_20260413_b_RC00`, build-id md5 `89edbbe81c5b328a958fe628a9f2207d`). The image is **not** stripped; demangled C++ symbol names are quoted verbatim. `.text` VMA equals file offset (`.text` base `0xe63c000`). All `+0xNN` offsets are into the `DmaParameters` bundle (an in-memory C++ struct, not the wire descriptor). Other versions will differ.*

## Abstract

Once `LowerPassBase` has split a tiled `enqueue_dma` into per-tile transfers, each tile is handed to a single per-tile *emitter*, `LowerMemrefToMlo::issueStridedTransfer` (`0x1350cb60`, `lower_memref_to_mlo.cc`). Its job is to pick the **cheapest concrete LLO DMA-start op** that can express the tile's access pattern. The decision is a 2-D selector: one axis is the `TransferKind` (`kDma` vs `kStream`), the other is **how many stride levels the tile has** — read directly off `DmaParameters::src_byte_strides.size()` (`+0x18`). Zero stride levels means a flat contiguous run (the `DmaSimpleStart` / `LinearStream` fast path); one stride level means a regular 2-D rectangle (`DmaSingleStridedStart` / `StridedStream`); more than one means an irregular gather that needs the full descriptor (`GeneralDma`, or — for streams — a hard error, because streams support at most one level of striding).

Picking the form off `src_byte_strides.size()` alone would be naive: a 4-D tile whose inner three dims happen to be row-major-contiguous *should* be emitted as a flat copy, not a 4-level general DMA. So the selection is preceded by a **dimension-coalescing optimizer** that runs inside the caller, `LowerPassBase::issueRolledTransfer` (`0x13516ca0`, `lower_pass_base.cc:123`). It walks the per-dim stride arrays from the innermost dim outward and *flattens* any adjacent pair `(d, d+1)` that is either degenerate (inner extent is the constant `1`) or genuinely row-major-contiguous (the outer stride is exactly the inner stride scaled by the outer extent, checked independently for source and destination). Each merge collapses two dims into one larger stride via `arith.muli`, shrinks the three parallel arrays by one with `memmove`, and logs `VLOG(1) "Flattening striding dimension <d>"`. Coalescing runs *first*; only the residual dims that could not be flattened become the per-tile `DmaParameters` the selector then keys on. The net effect: the more contiguity a transfer has, the cheaper the descriptor form the compiler lands on.

This page owns two things: the **form-selection rule** inside `issueStridedTransfer` (the `TransferKind × src_byte_strides.size()` 3×2 grid, the `DmaParameters` size invariant the entry checks enforce, the stream gather/scatter gates, and the length→granularity divisor), and the **dim-coalescing optimizer** (the backward adjacent-pair scan and the exact contiguity-merge predicate). The six emitter *bodies* — how each `::create` binds operands into a descriptor — live on [Rolled / Strided / General Emitters](rolled-strided-general.md); the descriptor field layout and the memory-space/opcode enums live on [Intra-Chip DMA Descriptor](intra-chip-descriptor.md); the tile-index→stride algebra that *constructs* the `DmaParameters` arrays lives on [Tile-Index Expansion](tile-index-expansion.md).

For reimplementation, the contract is:

- **The 3×2 selection grid** — `TransferKind` (`kDma=0` / `kStream=1` / else→`OpError`) crossed with `src_byte_strides.size()` (`0` / `1` / `>1`), mapping to six concrete SparseCore ops plus two error arms.
- **The `DmaParameters` size invariant** — `|src_byte_strides| == |tgt_byte_strides| == |steps_per_stride| − 1`, enforced by two `CHECK_EQ`s at the strided-arm entry, and the rule that the form is keyed on `|src_byte_strides|`.
- **The coalescing predicate** — collapse `(d, d+1)` iff `inner_extent == 1` **OR** (`outer_base` is a compile-time constant **AND** per-side `stride[outer] == stride[inner] × outer_extent`), with the `arith.muli` + 3× `memmove` collapse.
- **The stream gates** — a `kStream` transfer may carry at most one stride level (two distinct `<= 1` assertions, one per side); a gather may not stride its destination; a scatter may not stride its source.

| | |
|---|---|
| **Per-tile emitter (selector)** | `mlir::tpu::LowerMemrefToMlo::issueStridedTransfer(OpBuilder&, EnqueueDMAOp, Value src, Value dst, DmaParameters const&, optional<DeviceAndCoreIds>, int priority, …::TransferKind, bool)` @ `0x1350cb60` |
| **Coalescing optimizer** | `mlir::tpu::LowerPassBase::issueRolledTransfer(…)` @ `0x13516ca0` — coalesce loop body, `lower_pass_base.cc:123` |
| **Per-side stride validator** | `issueStridedTransfer::$_0::operator()(ArrayRef<int>)` @ `0x13510340` |
| **General-form helper** | `mlir::tpu::(anonymous namespace)::issueGeneralDma(…)` @ `0x1350b3a0` |
| **Constant-int probe** | `LowerPassBase` vtable `+0x20` (`(*(this)+0x20)(this, value)`) — a `getConstantIntValue`-style fold |
| **Form key** | `DmaParameters::src_byte_strides.size()` @ `+0x18` |
| **Source files** | `lower_memref_to_mlo.cc` (selector), `lower_pass_base.cc` (coalescing) |
| **Evidence grade** | Reimplementation-grade / byte-confirmed against IDA decompile (`issueStridedTransfer` body, `issueRolledTransfer` coalesce loop) |

---

## 1. The Selection Grid (`TransferKind × stride-count`)

### Purpose

`issueStridedTransfer` is the single point where an abstract per-tile transfer becomes a concrete hardware op. It does not decide *which tiles* exist (that is the rolled-loop machinery upstream) — it decides, for **one** already-tiled transfer, the descriptor form. There are exactly six concrete forms, three per `TransferKind`, and the choice is fully determined by two integers: the kind, and the number of stride levels.

### The two axes

**Axis 1 — `TransferKind`** is the eighth argument (a byte, after the `int priority`). The dispatch is a three-way switch:

| `TransferKind` | Value | Path |
|---|---:|---|
| `kDma` | 0 | DMA forms (Simple / SingleStrided / General) |
| `kStream` | 1 | Stream forms (Linear / Strided / *error*) |
| (anything else) | ≥2 | `emitOpError("Unsupported transfer kind: %d")` |

The default arm formats the raw integer through `absl::str_format_internal::FormatPack("Unsupported transfer kind: %d", 29, …)` into an `InFlightDiagnostic` and returns a failing `LogicalResult` — it is a *user-facing* error, not a `CHECK`, because the kind originates from the op being lowered.

**Axis 2 — the stride-level count** is `DmaParameters::src_byte_strides.size()`, read as a `u32` from bundle offset `+0x18`. Within each kind the selector branches on `0` / `1` / `>1`:

```c
// issueStridedTransfer @ 0x1350cb60 — form selection (reconstructed)
n = p.src_byte_strides.size();        // *(u32*)(p + 0x18)
switch (kind) {
case kDma:                            // TransferKind == 0
    if (n == 0 && !force_descriptor)  // also gated by the bool arg / cross-device presence
        emit DmaSimpleStartOp;        // contiguous
    else if (n == 1)
        emit DmaSingleStridedStartOp; // one stride level
    else /* n > 1 */
        issueGeneralDma(...);         // multi-stride -> GeneralDma
    break;
case kStream:                         // TransferKind == 1
    if (n == 0)
        emit LinearStreamStartOp;
    else if (n == 1)
        emit StridedStreamStartOp;    // CHECK: steps_per_stride.size() == 2
    else /* n > 1 */
        // hard error: streams support <= 1 stride level (see §4)
    break;
default:
    return emitOpError("Unsupported transfer kind: %d", kind);
}
```

### The 3×2 grid

The full mapping from `(kind, src_byte_strides.size())` to the emitted op (see [Rolled / Strided / General Emitters](rolled-strided-general.md) for each op's operand binding, and [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) for the descriptor the op fills):

| `src_byte_strides.size()` | `kDma` (0) | `kStream` (1) |
|---|---|---|
| **0** (contiguous) | `DmaSimpleStartOp::create` @ `0x145b9740` | `LinearStreamStartOp::create` @ `0x145e3440` |
| **1** (one stride level) | `DmaSingleStridedStartOp::create` @ `0x145bcd20` | `StridedStreamStartOp::create` @ `0x1460b8e0` |
| **>1** (multi-stride) | `issueGeneralDma` @ `0x1350b3a0` → `GeneralDma` | **error** — `"Streams support up to 1 level of striding"` |
| (else `TransferKind`) | `emitOpError("Unsupported transfer kind: %d")` | same |

> **NOTE —** the `kDma`/`n==0` Simple fast path is additionally gated by the trailing `bool` arg (`[rbp+0x38]`) and the presence of the `optional<DeviceAndCoreIds>` cross-device id: when either is set, the contiguous case is forced into the Strided/General arm rather than taking the Simple shape. The gating arithmetic is byte-confirmed; the precise semantics of the flag are **HIGH** — consistent with an "is-remote / force-descriptor" role (a cross-device transfer cannot use the local Simple descriptor). A reimplementer can treat the rule as: *Simple is taken only for a purely-local, zero-stride tile.*

### The priority precondition

The very first thing the function does is assert its `int priority` argument is zero:

```c
// @ entry — lower_memref_to_mlo.cc:1142
if (priority != 0)
    LogMessageFatal("priority == 0");   // MakeCheckOpString<long,long>
```

This is a hard `CHECK` (`absl::log_internal::LogMessageFatal`), not a recoverable error — the per-tile issuer is only ever invoked at priority 0. The `priority` parameter exists for the API shape shared with the higher-level enqueue path; here it must be 0.

---

## 2. The `DmaParameters` Bundle

### Purpose

`DmaParameters` is the per-tile descriptor the selector consumes — the in-memory C++ struct (passed by `const&`), distinct from the wire `OciDescriptorCommonIssuedFromTcs` it eventually produces. It is built per tile by the rolled-loop machinery (the construction site is in `issueRetiledTransfer`; see [Tile-Index Expansion](tile-index-expansion.md)) and read field-by-field here. The selector reads it; it never mutates it.

### Layout

Field names are recovered from the `CHECK_EQ` string literals (`p.src_byte_strides` / `p.tgt_byte_strides` / `p.steps_per_stride`); offsets and access widths from the decompiled reads:

| Offset | Size | Field | Type | Evidence |
|---|---:|---|---|---|
| `+0x00` | 8 | `src` | `mlir::Value` | `getMemRefType` → `GetMemorySpace` |
| `+0x08` | 8 | `dst` | `mlir::Value` | `getMemRefType` → `GetMemorySpace` |
| `+0x10` | 8 | `src_byte_strides.data()` | `int*` (4-B entries) | read `*(u32*)(p+0x10)`; entries `movsxd` |
| `+0x18` | 4 | `src_byte_strides.size()` | `u32` | **form selector**; `CHECK_EQ` source |
| `+0x50` | 8 | `tgt_byte_strides.data()` | `int*` (4-B entries) | per-side stride array |
| `+0x58` | 4 | `tgt_byte_strides.size()` | `u32` | `CHECK_EQ == +0x18` |
| `+0x90` | 8 | transfer length / sflag | `mlir::Value` | passed as `len`/`sflag` create arg |
| `+0x98` | 8 | `steps_per_stride.data()` | `Value*` (8-B entries) | read `*(Value*)(p+0x98)` |
| `+0xA0` | 4 | `steps_per_stride.size()` | `u32` | `CHECK_EQ == +0x18 + 1` |

> **NOTE —** `+0x90` is a `Value` used as the `len`/`sflag` create argument; the exact field name (transfer length vs. sync-flag) is not in any `CHECK` string and is **INFERRED**. Offset, 8-byte `Value` type, and use as the create arg are byte-pinned.

### The size invariant

At the entry to the strided/general arm, two `CHECK_EQ`s enforce that the three vectors are coupled (`lower_memref_to_mlo.cc:843`/`:844`):

```c
CHECK_EQ(p.src_byte_strides.size(), p.tgt_byte_strides.size());
//   *(u32*)(p+0x18) == *(u32*)(p+0x58)
CHECK_EQ(p.src_byte_strides.size(), p.steps_per_stride.size() - 1);
//   *(u32*)(p+0x18) == *(u32*)(p+0xA0) - 1
```

So a well-formed bundle has matching source/target stride-vector lengths and exactly **one more** `steps_per_stride` entry than stride levels — `steps_per_stride` is the `N+1` cumulative-extent vector for `N` stride levels. A contiguous (`N=0`) descriptor therefore has `steps_per_stride.size() == 1`. The form is then chosen by `N = src_byte_strides.size()`: `0` Simple/Linear, `1` SingleStrided/Strided, `>1` General/error. This is the single integer the whole 3×2 grid keys on, which is exactly why the coalescing pass (§5) — which shrinks `N` — runs before it.

---

## 3. The Length → Granularity Divisor

Before an op is built, the transfer length is converted from bytes into hardware *granule* units (the descriptor carries a granule count, not a raw byte count — see the `(length, length_granule)` pair on [Intra-Chip DMA Descriptor](intra-chip-descriptor.md)). The two kinds use different granularity sources:

- **`kDma`** divides by `xla::jellyfish::Target::GranuleBytes()` (`0x1d617f80`) using a ceil-division idiom (`(len + gran − 1) / gran`, emitted as an unsigned `idiv`).
- **`kStream`** divides by `xla::tpu::sparse_core::xla_mlo_util::TransferGranularityInBytes(SparseCoreTarget const&, MemorySpace, bool)` (`0x14a89ea0`), reached through `target()->[+0x948]` — the SPMEM-stripe / DMA-word unit, which depends on the memory space.

The divisor is materialized as an `arith.ConstantIndexOp` and the division as a `DivUIOp` (or an inlined `idiv`). The HBM endpoint test (`srcSpace == HBM (4)` / `dstSpace == HBM (4)`, computed by an `XOR 4; SETE`) feeds the `dstIsHbm` bool that the Linear/Strided stream `::create` takes.

---

## 4. Stream Striding Gates

A stream is more constrained than a DMA: the hardware stream engine handles at most **one** level of striding, and gather/scatter streams forbid striding on one of their two sides. `issueStridedTransfer` enforces all three constraints before emitting a `LinearStream`/`StridedStream`.

### The one-level limit (two assertions)

For the `kStream` strided path the binary asserts the level bound **per side**, with two distinct `CHECK`s and two distinct diagnostic strings:

```c
// lower_memref_to_mlo.cc:1189
CHECK(src_byte_strides.size() <= 1)
    else "Streams support up to 1 level of striding. Got %d levels of source striding.";
// lower_memref_to_mlo.cc:1193
CHECK(tgt_byte_strides.size() <= 1)
    else "Streams support up to 1 level of striding. Got %d levels of steps per stride.";
```

The single-stride stream emit is itself guarded by `CHECK_EQ(off_tile_byte_strides.size(), 1)` (`:1263`) and `CHECK_EQ(p.steps_per_stride.size(), 2)` (`:1264`) — i.e. exactly one stride level and the matching two cumulative steps.

> **GOTCHA —** the ">1 stride" stream case is a fatal `CHECK`, *not* the recoverable `emitOpError` that the `kDma` `>1` case routes to `issueGeneralDma`. There is no "general stream" form: a multi-stride transfer that must be a stream is a compiler bug at this point, so the binary traps. A reimplementer must guarantee the upstream tiler never hands a >1-stride bundle to the `kStream` arm (coalescing in §5 is what makes that guarantee hold for contiguous inner dims).

### Gather / scatter side constraints

After classifying the op with `mlir::tpu::isGather` (`0x14afb1e0`), the stream arm runs the per-side stride-level validator lambda `$_0` (`0x13510340`, `operator()(ArrayRef<int>)`) once for the source side and once for the destination side. `$_0` reads `steps_per_stride` (`+0x98`) and probes it through the `LowerPassBase` vtable `+0x20` `getConstantIntValue` fold — the *same* probe the coalescing loop uses (§5) — checking the stride level is `<= 1`. The classifier then rejects the asymmetric cases:

| Condition | Diagnostic (`emitOpError`) |
|---|---|
| `isGather` **and** destination is strided | `"Gather streams do not support destination striding. Got %d level(s) of target striding."` |
| `isScatter` **and** source is strided | `"Scatter streams do not support source striding. Got %d level(s) of source striding."` |

A gather pulls scattered source elements into a packed destination, so the destination is inherently contiguous; a scatter is the mirror. Striding the "packed" side is meaningless and is rejected with a user-facing error.

---

## 5. The Dimension-Coalescing Optimizer

### Purpose

The selector (§1) keys on `src_byte_strides.size()`, so the *fewer* stride levels a tile has, the cheaper the form it lands on. The coalescing pass exists to minimize that count: it folds every adjacent dim pair that is contiguous (or degenerate) into a single larger stride *before* the bundle reaches the selector. A transfer whose access pattern is, mathematically, a flat run will be flattened to `N=0` and emitted as `DmaSimpleStart` even if it was described with several nominal dims. Coalescing lives in the caller `issueRolledTransfer` (`lower_pass_base.cc`); the residual leading dim that *cannot* be flattened becomes the single `scf.for` loop, and the inner flattened dims become the per-iteration `DmaParameters` the selector consumes.

### The backward adjacent-pair scan

`issueRolledTransfer` keeps three parallel per-dim arrays — `bases` (the per-dim extents, 8-byte `Value` entries), `src_byte_strides` (4-byte ints), and `dst_byte_strides` (4-byte ints) — with three independent counts. The coalescing loop walks adjacent pairs from the **innermost** dim toward the outer, indexing `r15 → bases[outer]`, `r12 = r15−8 → bases[inner]`. For each pair it folds both bases through the constant-int probe (`(*(this)+0x20)(this, base)`), which returns an `optional<int64>` packed as `{ bit32 = present | low32 = value }`:

```c
v47 = getConstantIntValue(bases[outer]);   // (*(a1)+0x20)(a1, &bases[outer])
v48 = getConstantIntValue(bases[inner]);    // same probe on the inner dim
```

### The exact contiguity-merge predicate

A pair `(outer d, inner d+1)` is collapsed iff (byte-exact, `issueRolledTransfer` decompile lines 317–320; `0x100000001` = present-bit | value 1):

```c
if (   (v48 & 0x1FFFFFFFF) == 0x100000001                          // (i)
    || (v47 & 0x100000000) != 0                                    // (ii) gate
       && src_stride[outer] == (int)v47 * src_stride[inner]        // (ii) src contiguity
       && dst_stride[outer] == dst_stride[inner] * (int)v47 )      // (ii) dst contiguity
```

- **Condition (i) — degenerate inner dim.** `getConstantIntValue(bases[inner])` is present **and** equals `1`. An inner extent of 1 contributes no iterations, so the pair always folds — no stride check needed. (The `(x & 0x1ffffffff) == 0x100000001` mask tests "present **and** value == 1" in one compare.)
- **Condition (ii) — row-major contiguity.** `getConstantIntValue(bases[outer])` is present (a compile-time constant extent — the `test r13, 0x100000000` present-bit gate) **AND**, *per side*, the outer stride equals the inner stride scaled by the outer extent: `src_stride[outer] == src_stride[inner] × outer_extent` **AND** `dst_stride[outer] == dst_stride[inner] × outer_extent`. This is exactly the algebra of two row-major dims that abut with no gap — they describe one larger contiguous stride.

If neither holds, the pair is left intact and becomes a residual stride/loop dim.

| Step | Test | Meaning |
|---|---|---|
| a | `v47 = getConstantIntValue(bases[outer])` | vtable `+0x20` fold on the outer extent |
| b | `v48 = getConstantIntValue(bases[inner])` | same fold on the inner extent |
| c | `(v48 & 0x1ffffffff) == 0x100000001` | **(i)** inner extent is the constant `1` |
| d | `v47 & 0x100000000` | **(ii)** gate: outer extent is a constant |
| e | `src_stride[outer] == (int)v47 × src_stride[inner]` | **(ii)** source contiguity |
| f | `dst_stride[outer] == dst_stride[inner] × (int)v47` | **(ii)** destination contiguity |
| MERGE | `(c)` **OR** `(d && e && f)` | collapse `(d, d+1)` → one dim |

> **NOTE —** condition (i) is a deliberate fast path, not a subset of (ii): a count-1 dim folds without consulting either side's stride. Whether (i) is *logically* subsumed by (ii) when the inner stride is 0 is **INFERRED** — the two branch targets are distinct in the binary, and (i) is a correctness-preserving superset of (ii) for degenerate dims.

### The collapse

On a merge, the inner extent is multiplied into the outer to form the merged dim's extent, the diagnostic is logged, and the three arrays are shifted down by one over the consumed slot:

```c
// merged extent
merged = OpBuilder::createOrFold<arith::MulIOp>(bases[outer], bases[inner]);
VLOG(1) << "Flattening striding dimension " << d;     // lower_pass_base.cc:123
bases[d] = merged;
memmove(bases + d*8 ...,         8 * (count - 1));     --bases_count;   // 8-B Value entries
memmove(src_strides + d*4 ...,   4 * (count - 1));     --src_count;     // 4-B int entries
memmove(dst_strides + d*4 ...,   4 * (count - 1));     --dst_count;     // 4-B int entries
```

The `arith.muli` is a `createOrFold` (`mlir::arith::MulIOp` over two `Value`s), so two compile-time-constant extents fold to a constant immediately. The three `memmove`s shrink the parallel arrays in lock-step (8-byte bases, 4-byte source strides, 4-byte destination strides), each decrementing its own count. The scan then continues over the shrunk arrays. When it exhausts, the residual leading dim becomes the single `scf::ForOp` and the flattened bundle is handed to the per-tile callback — i.e. to `issueStridedTransfer` and the selector of §1.

---

## 6. Cost Ordering

The selector encodes a cost hierarchy: cheaper descriptors are preferred whenever the access pattern allows. From cheapest to most expensive:

| Rank | Form (`kDma` / `kStream`) | When chosen | Cost rationale |
|---:|---|---|---|
| 1 | `DmaSimpleStart` / `LinearStream` | `src_byte_strides.size() == 0` (and local) | one contiguous run; no stride operands, smallest descriptor |
| 2 | `DmaSingleStridedStart` / `StridedStream` | `src_byte_strides.size() == 1` | one stride level; a single 2-D rectangle, four extra stride `Value`s |
| 3 | `GeneralDma` (`kDma` only) | `src_byte_strides.size() > 1` | multi-level / cross-core; full stride lists + two-sided sync |
| — | error | `kStream` with `> 1` level, or unknown kind | unrepresentable as a single op |

Coalescing (§5) is the mechanism that *moves a transfer up* this ranking: by flattening contiguous dims it drives `src_byte_strides.size()` toward 0, so the same logical transfer is emitted with the cheapest form its actual memory layout permits. The selection rule itself is then a flat lookup on the post-coalescing count — there is no cost model or search; the count *is* the cost class.

---

## 7. Diagnostic Strings

Every string the two functions can print, with the trap kind and source site. `CHECK`/`LogMessageFatal` are *compiler-internal* invariants (a violation is a bug in the upstream tiler); `emitOpError` is *user-facing* (a violation is an unrepresentable input op). `VLOG(1)` is informational.

| String | Kind | Site | Trigger |
|---|---|---|---|
| `priority == 0` | `CHECK` (fatal) | `lower_memref_to_mlo.cc:1142` | `priority != 0` at entry |
| `p.src_byte_strides.size() == p.tgt_byte_strides.size()` | `CHECK_EQ` (fatal) | `:843` | source/target stride vectors differ in length |
| `p.src_byte_strides.size() == p.steps_per_stride.size() - 1` | `CHECK_EQ` (fatal) | `:844` | `steps_per_stride` not `N+1` for `N` strides |
| `off_tile_byte_strides.size() == 1` | `CHECK_EQ` (fatal) | `:1263` | single-stride stream with ≠1 off-tile stride |
| `p.steps_per_stride.size() == 2` | `CHECK_EQ` (fatal) | `:1264` | single-stride stream with ≠2 cumulative steps |
| `src_byte_strides.size() <= 1` | `CHECK` (fatal) | `:1189` | >1 source stride level on a stream |
| `tgt_byte_strides.size() <= 1` | `CHECK` (fatal) | `:1193` | >1 target stride level on a stream |
| `Unsupported transfer kind: %d` | `emitOpError` | (kind dispatch default) | `TransferKind` ∉ {`kDma`, `kStream`} |
| `Streams support up to 1 level of striding. Got %d levels of source striding.` | `emitOpError` | (stream gate) | stream source over-strided |
| `Streams support up to 1 level of striding. Got %d levels of steps per stride.` | `emitOpError` | (stream gate) | stream target over-strided |
| `Gather streams do not support destination striding. Got %d level(s) of target striding.` | `emitOpError` | (gather gate) | `isGather` with a strided destination |
| `Scatter streams do not support source striding. Got %d level(s) of source striding.` | `emitOpError` | (scatter gate) | `isScatter` with a strided source |
| `Flattening striding dimension <d>` | `VLOG(1)` | `lower_pass_base.cc:123` | coalescing pass merged dim pair `(d, d+1)` |

---

## 8. Function Map

| Address | Symbol | Role |
|---|---|---|
| `0x1350cb60` | `LowerMemrefToMlo::issueStridedTransfer` | the per-tile selector (this page §1–§4) |
| `0x13510340` | `issueStridedTransfer::$_0::operator()(ArrayRef<int>)` | per-side stream stride-level validator (§4) |
| `0x13516ca0` | `LowerPassBase::issueRolledTransfer` | rolled-loop caller hosting the coalescing pass (§5) |
| `0x1350b3a0` | `(anonymous)::issueGeneralDma` | the `>1`-stride / cross-core `GeneralDma` emitter |
| `0x145b9740` | `DmaSimpleStartOp::create` | contiguous DMA emitter |
| `0x145bcd20` | `DmaSingleStridedStartOp::create` | one-stride DMA emitter |
| `0x145e3440` | `LinearStreamStartOp::create` | contiguous stream emitter |
| `0x1460b8e0` | `StridedStreamStartOp::create` | one-stride stream emitter |
| `0x14afb1e0` | `mlir::tpu::isGather` | gather/scatter classifier (§4) |
| `0x1d617f80` | `Target::GranuleBytes` | `kDma` length divisor (§3) |
| `0x14a89ea0` | `xla_mlo_util::TransferGranularityInBytes` | `kStream` length divisor (§3) |
| `0xfaaae00` | `OpBuilder::createOrFold<arith::MulIOp, Value&, Value&>` | coalescing base-merge (§5) |
| `LowerPassBase` vtable `+0x20` | (abstract) `getConstantIntValue` probe | constant-fold used by both `$_0` and the coalescing loop |

---

## Cross-References

- [Rolled / Strided / General Emitters](rolled-strided-general.md) — the six `::create` emitter bodies (`DmaSimpleStart` / `DmaSingleStridedStart` / `issueGeneralDma` / `LinearStream` / `StridedStream`) and how each binds `DmaParameters` operands into a descriptor
- [Intra-Chip DMA Descriptor](intra-chip-descriptor.md) — the `OciDescriptorCommonIssuedFromTcs` wire layout, the `(mem_id, core_id, opcode)` endpoint enums, and the `(length, length_granule)` size pair the granularity divisor (§3) feeds
- [Tile-Index Expansion](tile-index-expansion.md) — `ExpandTiledMemRefs` / `expandTiledIndices`, which constructs the `DmaParameters` stride arrays this selector keys on
- [Continuation Queue](continuation-queue.md) — the runtime SFLAG completion protocol the emitted DMA/stream ops bump
- [back to index](../index.md) — Part X — On-Chip Memory & DMA / DMA
