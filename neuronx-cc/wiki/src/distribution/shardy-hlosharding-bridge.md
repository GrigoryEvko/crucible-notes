# Shardy ↔ HloSharding Bridge

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22, front-end binary `neuronxcc/starfish/bin/hlo-opt` (cp310 wheel). The cp311/cp312 wheels carry the same symbols at the same VAs. Other versions will differ.*

## Abstract

Neuron's SPMD sharding propagation runs on OpenXLA **Shardy** (`mlir::sdy`) — a
factor-space dialect whose per-tensor result is a `TensorShardingAttr` (which mesh
axis shards which tensor dimension). But the rest of XLA — the HLO module, the
SPMD partitioner, the wire format — speaks `xla::HloSharding`, the classic GSPMD
tile-assignment object, and serializes it to the `xla::OpSharding` protobuf. This
page documents the **bidirectional bridge** between the two representations: the
two converter functions `convertToHloSharding` / `convertToSdySharding`, and the
proto round-trip `HloSharding::FromProto` / `HloSharding::ToProto`.

A reader who knows GSPMD will recognize the shape immediately. A `TensorShardingAttr`
is a *named-axis* description; an `HloSharding` is a *flat device-grid* description.
The bridge's whole job is the change of basis between them: flatten the per-dimension
axis-ref lists into a single `TileAssignment` over the mesh's device order (export),
or factor a tile assignment back into mesh axes and sub-axes (import). Everything on
this page is **stock OpenXLA** — `mlir::sdy` (github.com/openxla/shardy), `xla::sdy`
(`xla/service/spmd/shardy`), and `xla::HloSharding` (XLA core), all linked verbatim
into `hlo-opt`. Not one symbol, field offset, or enum value carries a Neuron delta;
Neuron *drives* this bridge from its propagation pipeline ([13.2](sharding-propagation.md))
and consumes the resulting `HloSharding` downstream. Provenance D-AB06.

This page covers, in order: the five `mlir::sdy` attribute storage layouts (§A);
the export converter `convertToHloSharding` and its axis→tile-dim flattening (§B);
the in-memory `HloSharding` object the converter fills (§B.3); the import converter
`convertToSdySharding` (§C); and the `HloSharding`↔`OpSharding` proto round-trip with
the field offsets and the type-enum dispatch ladder (§D). The factor *algebra* that
produces the attrs in the first place — `OpShardingRule`, `ShardingProjection`,
`createTensorShardingAttr` — is owned by [13.3 Sharding Algebra](sharding-algebra.md);
this page treats it as a producer and picks up at the attr boundary.

For reimplementation, the contract is:

- The **attr↔proto round-trip**: from a `TensorShardingAttr` (named axes per dim) to
  an `OpSharding` proto and back, reproducing the same tile assignment each way.
- The **five attribute storage layouts** — every accessor is `mov rax,[rdi];
  mov ...,[rax+OFF]`, so the offsets *are* the wire format of the in-memory attr.
- The **axis→tile-dim flattening** rule (export) and its inverse factorization (import),
  including the iota-vs-explicit-device-list fork.
- The **`OpSharding` proto field offsets** and the **`OpSharding_Type` dispatch ladder**
  in `FromProto`.

| | |
|---|---|
| **Export converter** | `xla::sdy::convertToHloSharding` @ `0x2bc58f0` (279 bb, 29 callees) |
| **Import converter** | `xla::sdy::convertToSdySharding` @ `0x2bd7b00` (146 bb, 25 callees) |
| **Proto deserialize** | `xla::HloSharding::FromProto(OpSharding const&)` @ `0x979d580` |
| **Proto serialize** | `xla::HloSharding::ToProto() const` @ `0x97a0600` (80 bb, 22 callees) |
| **Export driver** | `ExportStablehloShardingsPass::runOnOperation` (+ `setHloShardingAttr`) |
| **Import driver** | `ImportShardingsPass::runOnOperation` (+ `importShardings`) |
| **IR level** | StableHLO / `HloModule`; the wire object is `xla::OpSharding` |
| **Provenance** | **stock OpenXLA** — `mlir::sdy` + `xla::sdy` + `xla::HloSharding`, zero Neuron deltas |

> **NOTE —** the IDA export for `hlo-opt` was produced with decompilation skipped
> (`decompiled/*.c` are stubs). Every offset below was read from the leaf-accessor
> and converter `disasm/*.asm` listings and cross-checked against the `context/*.md`
> callee sidecars — not statement-level decompilation. Where a claim rests on the
> callgraph plus strings plus the upstream Shardy contract rather than on a read
> instruction, the text says so; gaps filled from upstream knowledge are tagged
> [INFERRED].

---

## §A — The `mlir::sdy` Attribute Hierarchy

### Purpose

A Shardy sharding is a tree of uniqued MLIR attributes. An `Attribute` is a single
pointer to an immutable storage struct; every accessor dereferences that handle
(`mov rax,[rdi]`) and then loads a field at a fixed offset inside the storage. The
offsets below therefore *are* the in-memory layout — a reimplementation that lays
its storage structs out the same way produces accessors with identical displacements.

All five storage structs begin with an 8-byte `AbstractAttribute`/TypeID header at
`+0x00` (the part every accessor skips). `ArrayRef<T>` fields are stored as the
canonical `{pointer @OFF, length @OFF+8}` pair. The tree nests:

```text
TensorShardingAttr            one tensor's sharding (attached per op result)
 ├─ meshOrRef    : Attribute  (StringAttr sym-ref to a sdy.mesh op, OR inline MeshAttr)
 ├─ dimShardings : [DimensionShardingAttr]   one per tensor dim, in dim order
 │    └─ axes    : [AxisRefAttr]             mesh axes sharding this dim, major→minor
 │         ├─ name        : StringRef
 │         └─ subAxisInfo : SubAxisInfoAttr? (null = full axis; else a slice {preSize,size})
 ├─ replicatedAxes : [AxisRefAttr]           axes explicitly held replicated
 └─ unreducedAxes  : [AxisRefAttr]           axes the tensor is "unreduced" over

MeshAttr                      the named device mesh
 ├─ axes      : [MeshAxisAttr]  ordered (name,size); order = major→minor device nesting
 └─ deviceIds : [long]          OPTIONAL explicit device permutation (empty = iota order)
```

### Storage Layouts

Every offset is read from the named leaf accessor's `mov` displacement. The
"Accessor" column gives the symbol whose disasm was read.

**`MeshAxisAttr`** — one named mesh axis (`get` @ `0x2c83b40`):

| Field | Offset | Type | Accessor |
|---|---|---|---|
| header | `+0x00` | TypeID | — |
| `name` ptr / len | `+0x08` / `+0x10` | `StringRef` | `getName` @ `0x2c71f70` → `{[+8],[+10]}` |
| `size` | `+0x18` | `int64` | `getSize` @ `0x2c71f80` → `[+0x18]` |

**`MeshAttr`** — axes + optional device order (`get` @ `0x2c995a0`; with deviceIds @ `0x2c9a0a0`; maximal @ `0x2c994f0`):

| Field | Offset | Type | Accessor |
|---|---|---|---|
| `axes` ptr / len | `+0x08` / `+0x10` | `ArrayRef<MeshAxisAttr>` | `getAxes` @ `0x2c71f90` |
| `deviceIds` ptr / len | `+0x18` / `+0x20` | `ArrayRef<long>` | `getDeviceIds` @ `0x2c71fa0` |

> **GOTCHA — `deviceIds` empty is not "no devices", it means *iota* device order.**
> `getDeviceIds().len == 0` signals the canonical permutation `0..totalSize-1`, which
> lets the export emit a *compact* `IotaTileAssignment` (no device vector). A
> *non-empty* `deviceIds` forces a materialized `Array<long>` device list. The whole
> iota-vs-explicit fork in §B.4 hinges on this one length word. `isMaximal` @ `0x2c70510`
> reads exactly this: `[+0x10]==0 && [+0x20]==1` (no axes, one device) → a single fixed
> device → `HloSharding::AssignDevice` / `MAXIMAL`. `getMaximalDeviceId` @ `0x2c70530`
> then dereferences `[+0x18]` (deviceIds ptr) to read that one device id.

`MeshAttr` derived helpers: `getAxisSize(StringRef)` @ `0x2c70480`
(linear scan of `axes` comparing name length `[+0x10]` then `memcmp` of name ptr `[+0x08]`,
returning size `[+0x18]`; falls to `report_fatal_error("unknown axis name")` on miss),
`getTotalSize` @ `0x2c70500` (product of axis sizes, tail-calls `getTotalAxesSize`).

**`SubAxisInfoAttr`** — a contiguous slice of one axis (`get` @ `0x2c71fb0`):

| Field | Offset | Type | Accessor |
|---|---|---|---|
| `preSize` | `+0x08` | `int64` | `getPreSize` @ `0x2c721c0` → `[+8]` |
| `size` | `+0x10` | `int64` | `getSize` @ `0x2c721d0` → `[+0x10]` |

`preSize` is the product of the sizes of the sub-axes to this slice's *major* side
within the same axis; `size` is this slice's extent. A full axis of size 8 split for
two factors is `(pre=1,size=2)` + `(pre=2,size=4)`. Printed `"x":(2)4`.

**`AxisRefAttr`** — a full axis OR a sub-axis slice (`get` (full) @ `0x2c84cc0`;
`get(name,preSize,size)` (builds `SubAxisInfoAttr`) @ `0x2c83d70`; `get(name,SubAxisInfoAttr)` @ `0x2c84b20`):

| Field | Offset | Type | Accessor |
|---|---|---|---|
| `name` ptr / len | `+0x08` / `+0x10` | `StringRef` | `getName` @ `0x2c722b0` |
| `subAxisInfo` | `+0x18` | `SubAxisInfoAttr` (ptr; `0` = full axis) | `getSubAxisInfo` @ `0x2c722c0` → `[+0x18]` |

The discriminator function `getSize(mesh)` @ `0x2c70a60` is the per-axis **tile
multiplier** used by the export — "how many device-ways does this ref shard":

```c
long AxisRefAttr::getSize(MeshAttr mesh):           // 0x2c70a60
    storage = *this;                                // mov rdx,[rdi]
    sub = storage[+0x18];                            // mov rax,[rdx+18h]  — subAxisInfo
    if (sub != 0):                                   // test rax,rax; jz
        return sub[+0x10];                           // mov rax,[rax+10h]  — SubAxisInfoAttr.size
    return mesh.getAxisSize(storage.name);           // {[rdx+8],[rdx+10h]} → MeshAttr::getAxisSize @0x2c70480
```

The richer per-axis algebra (`contains`/`prefixOf`/`overlaps`/`getPrefixWithoutOverlap`/
`getMeshComparator`, etc.) that propagation uses to decide *which* axes a factor may
take is owned by [13.3 Sharding Algebra](sharding-algebra.md#axisrefattr-algebra); this
page needs only `getSize` (the multiplier) and `getMeshComparator` (the device order).

**`DimensionShardingAttr`** — one tensor dim's sharding (`get` @ `0x2c97600`; with priority @ `0x2c97320`):

| Field | Offset | Type | Accessor |
|---|---|---|---|
| `axes` ptr / len | `+0x08` / `+0x10` | `ArrayRef<AxisRefAttr>` | `getAxes` @ `0x2c722d0` |
| `isClosed` | `+0x18` | `i1`/byte | `getIsClosed` @ `0x2c722e0` → `movzx [+0x18]` |
| `priority` value / flag | `+0x20` / `+0x28` | `optional<long>` | `getPriority` @ `0x2c722f0` |

`axes` is the ordered (major→minor) list of mesh refs sharding this dim; the product
of their `getSize(mesh)` is how many ways the dim is split. `isClosed` is the `}` vs
`...` syntax — closed means propagation may not append more axes.

> **QUIRK — `getPriority` returns a raw two-word pair, not a `std::optional`.** The
> leaf accessor @ `0x2c722f0` is `mov rdx,[rax+28h]; mov rax,[rax+20h]` — it hands back
> both words (`{value @+0x20, engaged-flag @+0x28}`) with no branch. The engaged-flag
> semantics live in whatever wraps the two words, not in this accessor. A reimplementer
> who models `priority` as a tagged optional must remember the tag is the second word,
> stored at `+0x28`, not folded into the value.

**`TensorShardingAttr`** — the full per-tensor sharding (`get` @ `0x2c9df40`; overloads @ `0x2c9e060` / `0x2c9eb60`):

| Field | Offset | Type | Accessor |
|---|---|---|---|
| `meshOrRef` | `+0x08` | `Attribute` (StringAttr or MeshAttr) | `getMeshOrRef` @ `0x2c72530` → `[+8]` |
| `dimShardings` ptr / len | `+0x10` / `+0x18` | `ArrayRef<DimensionShardingAttr>` | `getDimShardings` @ `0x2c72540` |
| `replicatedAxes` ptr / len | `+0x20` / `+0x28` | `ArrayRef<AxisRefAttr>` | `getReplicatedAxes` @ `0x2c72550` |
| `unreducedAxes` ptr / len | `+0x30` / `+0x38` | `ArrayRef<AxisRefAttr>` | `getUnreducedAxes` @ `0x2c72560` |

`verifyInvariantsImpl` @ `0x2c86750` takes exactly `(meshOrRef, dimShardings[],
replicatedAxes[], unreducedAxes[])` — an independent check on the four-field shape.
`meshOrRef` is resolved to a concrete `MeshAttr` by `getMesh(SymbolTable)` @ `0x2c71120`
(the named `sdy.mesh` op lookup) — and *that* is exactly the `getMesh` callback passed
into `convertToHloSharding`. The semantic split of the four fields:

- **`dimShardings`** — one entry per tensor dim, in dim order; an empty `axes` list ⇒ that
  dim is unsharded (tile size 1 along it).
- **`replicatedAxes`** — mesh axes explicitly held replicated; these become the
  **REPLICATED** trailing subgroup tile in `HloSharding` (`replicate_on_last_tile_dim`).
- **`unreducedAxes`** — axes the tensor is *unreduced* over (a partial, not-yet-all-reduced
  result, e.g. a dot's contracting axis before the reduction collective). SDY-specific
  bookkeeping; it has no `OpSharding` representation and is dropped on export.

Printed: `#sdy.sharding<@mesh, [{"x"}, {"y", ?}], replicated={"z"}>`.

> **NOTE —** the attribute actually attached to an op's `sdy.sharding` is usually a
> `TensorShardingPerValueAttr` (`ArrayRef<TensorShardingAttr>`, one per result), not a
> bare `TensorShardingAttr`. The export driver `setHloShardingAttr` iterates that list
> and calls `convertToHloSharding` once per result.

---

## §B — `convertToHloSharding` (Export: sdy → HloSharding)

### Purpose

`convertToHloSharding` @ `0x2bc58f0` is the export half of the bijection: it lowers one
tensor's `TensorShardingAttr` into an XLA `HloSharding` (the in-memory form that
`ToProto`, §D.3, then serializes to an `OpSharding`). It is **stock `xla::sdy`**
(`xla/service/spmd/shardy/...`). The demangled signature:

```c
xla::HloSharding convertToHloSharding(            // 0x2bc58f0  — sret in rdi
    mlir::sdy::TensorShardingAttr sharding,        // rdx
    std::function<MeshAttr(TensorShardingAttr)> getMesh,  // rcx
    llvm::ArrayRef<mlir::StringAttr> manualAxes);  // r8/r9
```

Callers, from the context sidecar: `ExportStablehloShardingsPass::runOnOperation`
and `setHloShardingAttr`.

### Entry Point

```text
ExportStablehloShardingsPass::runOnOperation       ── per-op driver
  └─ setHloShardingAttr(op, perValueShardings[], getMesh, manualAxes)
       └─ convertToHloSharding(sharding, getMesh, manualAxes)   ── 0x2bc58f0, once per result
```

`createExportStablehloShardingsPass(bool)` @ `0x2bc3dc0` constructs the pass.

### Algorithm

The control-flow skeleton is read from the `0x2bc58f0..0x2bc6e57` disassembly; the
flattening arithmetic in B.2 rests on the callee graph plus the upstream contract.

```c
function convertToHloSharding(sharding, getMesh, manualAxes):    // 0x2bc58f0
    // --- guard: the getMesh std::function must be engaged ---
    if (getMesh[+0x10] == 0):                       // 0x2bc592f cmp [rdx+10h],0
        __throw_bad_function_call();                // 0x2bc6dda
    mesh = getMesh(sharding);                        // call [rdx+18h] @0x2bc594b
    axes = mesh.getAxes();                           // 0x2bc595c {ptr,len}

    // --- degenerate meshes ---
    if (axes.len == 0):                              // mesh has no named axes
        if (mesh.getDeviceIds().len != 0):           // 0x2bc596d/72
            return HloSharding::AssignDevice(dev[0], md);   // MAXIMAL — 0x97970d0
        else:
            return HloSharding(/*replicated=*/true,false,false, md);  // 0x28e95d0

    // --- main tiled path ---
    dimShardings = sharding.getDimShardings();       // 0x2bc59c7
    // PASS 1: tile_assignment dim sizes
    for d in dimShardings:                            // 0x2bc5aaf getAxes per dim
        tileDim[d] = product over a in d.getAxes() of a.getSize(mesh);  // 0x2bc5b2d
    // ordered device-nesting order across ALL refs (dims + replicated + manual):
    orderedAxes = getOrderedAxisRefs(sharding, mesh);    // 0x2bec9d0 @ call 0x2bc5da5
    axisToPos   = { axisRef -> index in orderedAxes };   // SmallDenseMap<AxisRefAttr,long>, grow @0x2bbc570

    // PASS 2: device assignment — iota vs explicit
    if (mesh.getDeviceIds().len == 0):               // iota device order
        tileAssign = IotaTileAssignment::Create(dims, reshape_dims, transpose_perm); // 0x97a2870 @ 0x2bc675b
    else:                                            // explicit device list
        materialize Array<long> from deviceIds;
        Array<long>::TransposeDimensionsImpl(perm);  // 0x2bc4d00 @ 0x2bc614a — into mesh device order

    // --- result selection (trailing subgroup tiles) ---
    if (only replicated / all dims unsharded):
        return HloSharding(replicated,...);          // collapses — 0x28e95d0
    return HloSharding::Subgroup(tileAssign, subgroupTypes, md);   // 0x979c050 @ 0x2bc6290/67cb
    //   subgroupTypes: REPLICATED for replicatedAxes tile, MANUAL for manualAxes;
    //   empty span => plain OTHER/tiled.
```

### B.2 — The axis → tile-dim flattening

This is the change of basis, and the part a reimplementer most needs to get right. The
callee set is exact; the permutation arithmetic below is reconstructed from it rather
than traced block by block.

1. **Tile dims.** For each tensor dim `d`, the tile size along `d` is the product of
   `a.getSize(mesh)` over the refs `a` in `dimShardings[d].axes`. A dim with no axes
   gets tile size 1.
2. **Trailing subgroup dims.** `replicatedAxes` contributes one extra trailing tile of
   size `product(replicated axis sizes)`, tagged **REPLICATED** in `subgroupTypes`
   (this *is* `replicate_on_last_tile_dim` / a `last_tile_dims=REPLICATED`). `manualAxes`
   (the third argument) contributes a **MANUAL** trailing subgroup dim.
3. **Device nesting order.** `getOrderedAxisRefs(sharding, mesh)` @ `0x2bec9d0` returns
   every ref used (across all dims + replicated + manual) sorted by the mesh's axis order
   (`AxisRefAttr::getMeshComparator`: axis position in `mesh.getAxes()`, then sub-axis
   `preSize`). This ordered list is the **major→minor device-iteration order**; `axisToPos`
   maps each ref to its slot, and the per-dim axis lists are then expressed as a
   reshape+transpose of the flat iota device range `[0, mesh.getTotalSize())`.
4. **Iota vs explicit.** If `mesh.getDeviceIds()` is empty (iota), the tile assignment is
   the compact `(dims, reshape_dims, transpose_perm)` triple — no device vector;
   `reshape_dims` is the per-axis sizes in mesh order, `transpose_perm` maps mesh-axis
   order to tensor-dim(major→minor)+subgroup order. If non-empty, a dense `Array<long>`
   is built from the device list and transposed into the same logical order, producing
   an explicit (V1) `tile_assignment_devices`.

```text
result selection:
  axes.len==0 & deviceIds.len==0   →  HloSharding(replicated)
  axes.len==0 & deviceIds.len!=0   →  AssignDevice(dev[0])     (MAXIMAL)
  all dims unsharded & only repl.  →  replicated (collapses)
  otherwise                         →  Subgroup(tileAssign, types)
       types = {}  (plain OTHER/tiled)  |  {REPLICATED}  |  {MANUAL}  |  both
```

### B.3 — The `HloSharding` in-memory layout

`convertToHloSharding` fills this object; `ToProto` (§D.3) reads it. Offsets are read
from the `(bool,bool,bool, Span<OpMetadata>)` ctor @ `0x28e95d0`:

| Field | Offset | Notes |
|---|---|---|
| `tuple_elements_` | `+0x00` | `vector<HloSharding>` (begin/end/cap); empty for non-tuple |
| `tile_assignment_` | `+0x30` | `xla::TileAssignment` — `shared_ptr<Array<long>>` at `+0x30`/`+0x38` xor in-place `IotaTileAssignment` at `+0x48..+0x78` |
| packed flags | `+0x80` | byte; low 6 bits = `replicated_`/`manual_`/`unknown_`/`replicate_on_last_tile_dim_`/… |
| unique-device sentinel | `+0x88` | `-1` (0xffffffffffffffff) = "no maximal device" |
| `subgroup_types_` len | `+0x90` | 16-bit length word of the `Span<OpSharding_Type>` trailing-tile kinds |
| `metadata_` | (after) | `vector<OpMetadata>`, each `0x78` bytes |

The ctor disassembly shows each of these stores: the flags byte is a read-modify-write
`movzx edx,[r12+80h]` … `and edx,0xFFFFFFC0` (clear low 6) … `mov [r12+80h],al` — i.e.
the three ctor bools pack into bits of a 6-bit field; the `-1` sentinel is a literal
`mov qword [r12+88h], 0FFFFFFFFFFFFFFFFh`; the subgroup length is a 16-bit `mov [r12+90h],ax`
after `xor eax,eax`; and metadata is copied in a `add rbx,78h` stride loop.

> **NOTE — the exact bit positions inside the `+0x80` flags byte are not byte-verified.**
> The ctor packs three bools into the low 6 bits with mask `0x3F`, and `ToProto`
> (§D.3) `test`s individual bits to branch — but the precise bit→field mapping is named
> by role (`replicated_`/`manual_`/`unknown_`/`replicate_on_last_tile_dim_`), following
> the upstream `HloSharding` bitfield, not by traced bit index. [INFERRED]

---

## §C — `convertToSdySharding` (Import: HloSharding → sdy)

### Purpose

`convertToSdySharding` @ `0x2bd7b00` is the import inverse: it reads an `HloSharding`
(already parsed from an `OpSharding`/HLO string by `FromProto`, §D.2) and rebuilds a
Shardy `TensorShardingAttr` against a known `MeshAttr`. **Stock `xla::sdy`.** The
demangled signature:

```c
mlir::sdy::TensorShardingAttr convertToSdySharding(   // 0x2bd7b00  — sret
    xla::HloSharding const& hloSharding,               // rsi
    mlir::sdy::MeshAttr mesh,                           // rdx
    llvm::SmallDenseMap<long, StringRef, 4> const& deviceIdToAxisName,  // rcx
    long base, bool openDims);                          // r8, r9b
```

Callers: `ImportShardingsPass::runOnOperation` and the `importShardings`
lambda. This is the counterpart of the `kImportMhloShardings` pass.

### Entry Point

```text
ImportShardingsPass::runOnOperation
  └─ importShardings(funcOp, mesh, deviceIdToAxisName, openMask, ...)   ── lambda
       └─ convertToSdySharding(hloSharding, mesh, deviceIdToAxisName, base, openDims)   ── 0x2bd7b00
```

### Algorithm

Reconstructed from the callee set as the inverse of §B:

```c
function convertToSdySharding(hlo, mesh, devMap, base, openDims):   // 0x2bd7b00
    // --- maximal / replicated shortcuts ---
    if (hlo.UniqueDevice() has value):              // 0x9793210 / GetUniqueDevice 0x97a0570
        return maximal/fully-replicated TensorShardingAttr (no per-dim axes);
    if (hlo is REPLICATED):
        return openDims ? TensorShardingAttr::getFullyOpen : getFullyClosed;

    // --- decompose the tile assignment back into mesh axes ---
    subDims = analyzeTileAssignment(hlo.tile_assignment_);   // 0x2bd7010 @ 0x2bd7c0a
    //   walks the iota (reshape_dims, transpose_perm) into SubDimInfo
    //   {dim, axisName, sub-axis preSize & size}, sorted by axis order;
    //   internally calls shortestCommonFactorization (0x2bd6a60) to reconcile
    //   tile-dim sizes with mesh axis sizes (the inverse of B.2's product).

    // --- rebuild per-dim shardings ---
    for d in tile dims (excluding trailing subgroup dims):
        axes_d = [ AxisRefAttr::get(ctx, name)               // 0x2c84cc0 — full axis
                   or AxisRefAttr::get(ctx, name, pre, size) ]  // 0x2c83d70 — sub-axis
                 for each axis|sub-axis the factorization gave dim d;
        dimSharding_d = DimensionShardingAttr::get(ctx, axes_d, isClosed=!openDims); // 0x2c97600

    // trailing subgroup dims:
    //   REPLICATED last_tile_dim  -> axes go to replicatedAxes (NOT a tensor dim)
    //   MANUAL    last_tile_dim   -> consumed by the manual-computation import
    return TensorShardingAttr::get(ctx, meshSymOrAttr, dimShardings,        // 0x2c9df40
                                   replicatedAxes, unreducedAxes={});
```

`shortestCommonFactorization` @ `0x2bd6a60` is not a direct callee of
`convertToSdySharding`; it runs one frame deeper, reached through `analyzeTileAssignment`
@ `0x2bd7010` (`call` @ `0x2bd70eb`).

It is the inverse of B.2's "product of axis sizes": it finds
the minimal factorization mapping each tile-dim to whole axes or sub-axis slices. A tile
dim of size 8 over a mesh `(x=2,y=4)` becomes axes `{x,y}`; a tile dim of size 2 over an
axis `x=8` becomes the sub-axis `x:(1)2`. `deviceIdToAxisName` (the inverse of the mesh
device order) recovers named axes when the `HloSharding` arrived with an explicit device
list rather than iota.

### C.2 — Round-trip property and the one asymmetry

```text
convertToSdySharding(convertToHloSharding(T, getMesh, {}), mesh, devMap, .., open)  ≡  T
  up to:
   - axis MERGING: adjacent sub-axes that export split are re-merged by
     shortestCommonFactorization into the coarsest equivalent refs;
   - open/closed: NOT recoverable from HloSharding.
```

> **GOTCHA — `isClosed` is the one bit the bridge cannot round-trip.** A
> `DimensionShardingAttr` carries `isClosed` (`+0x18`), but `OpSharding` has no field for
> open/closed. Export drops it; import *cannot* read it back and must be **told** via the
> `openDims` argument (which sets `isClosed = !openDims` uniformly). This is the single
> non-bijective point of the entire bridge — every other attr field survives the
> round-trip. A reimplementation that assumes a clean bijection will silently flip
> open dims to closed (or vice-versa) on every import.

---

## §D — `HloSharding` ↔ `OpSharding` Proto Round-Trip

### Purpose

`xla::OpSharding` is the protobuf (the wire form embedded in the HLO as `mhlo.sharding`
/ a frontend attr); `xla::HloSharding` is the in-memory class. Export (§B) produces an
`HloSharding`; `ToProto` serializes it to `OpSharding`; on import `FromProto` rebuilds
the `HloSharding`, which `convertToSdySharding` (§C) lifts back to a Shardy attr. Both
are **stock XLA** (`xla/hlo/ir/hlo_sharding.cc`).

### D.1 — `OpSharding` proto field offsets

These are offsets into the generated protobuf message object, read from the
`FromProto` disassembly. The proto base enters in `rsi` and is aliased to `r14` at
`0x979d606` (`mov r14, rsi`); the two early reads at `0x979d5a1`/`0x979d5e0` are
pre-alias against the same object.

| Field | Offset | Read instruction |
|---|---|---|
| `replicate_on_last_tile_dim` | `+0x28` | `movsxd rsi,[r14+28h]` @ `0x979d802`, `cmp esi,1` |
| `tuple_shardings` count / ptr | `+0x48` / `+0x50` | `movsxd rsi,[r14+48h]` / `mov rax,[r14+50h]` |
| `metadata` count / ptr | `+0x60` / `+0x68` | `movsxd rax,[rsi+60h]` / `mov rbx,[rsi+68h]` |
| `tile_assignment_dimensions` count / ptr | `+0x70` / `+0x78` | `movsxd rax,[r14+70h]` / `mov rbx,[r14+78h]` |
| `last_tile_dims` count / ptr | `+0x88` / `+0x90` | `movsxd rdi,[r14+88h]` / `mov rax,[r14+90h]` |
| `tile_assignment_devices` count | `+0xA0` | `cmp edi,[r14+0A0h]` (V1 explicit list) |
| `type` (enum int32) | `+0xC0` | `mov edx,[r14+0C0h]` @ `0x979d7d8` |
| iota present flag (byte) | `+0xC5` | `cmp byte [r14+0C5h], 0` (9 sites) |
| iota data ptr | `+0xC8` | `mov rcx,[r14+0C8h]` |
| iota marker (dword) | `+0xD0` | `cmp dword [r14+0D0h], 1` (19 sites) |

The proto field names appear verbatim in the binary string pool:
`"tile_assignment_dimensions"`, `"tile_assignment_devices"`, `"iota_reshape_dims"`,
`"iota_transpose_perm"`, `"replicate_on_last_tile_dim"`, `"last_tile_dims"`,
`"tuple_shardings"`, plus the descriptor `".xla.OpSharding.Type"`. The V2 iota invariant
is enforced by the string `"proto.iota_reshape_dims().size() == proto.iota_transpose_perm().size()"`
— note the `proto.` prefix on *both* operands — and the device-list path by
`"proto.tile_assignment_devices().size() > 1"`. That is exactly the upstream
`xla_data.proto` `OpSharding` shape.

### D.2 — `FromProto(OpSharding const&)` @ `0x979d580` and the type-enum ladder

`FromProto` returns `absl::StatusOr<HloSharding>` (sret; bad protos → `InvalidArgument`
via `RET_CHECK`). It copies `metadata` (`0x78`-byte `OpMetadata` each) and
`tile_assignment_dimensions` into locals, then switches on `type = [r14+0xC0]`.

The dispatch ladder is traced instruction-by-instruction. The `OpSharding_Type`
values follow upstream `xla_data.proto`:

| Value | Type | Top-ladder test | Result ctor |
|---|---|---|---|
| 0 | REPLICATED | `test edx,edx; jz` @ `0x979d7e8` | `HloSharding(true,false,false,md)` @ `0x28e95d0` |
| 1 | MAXIMAL | (tail; `[r14+28h]==1` → `loc_979E704`) | `HloSharding(device, md)` @ `0x9796e40` |
| 2 | TUPLE | `cmp edx,2; jz` @ `0x979d7df` | recurse → `HloSharding(vector)` @ `0x9794a90` |
| 3 | OTHER | (fall-through, tiled) | see below |
| 4 | MANUAL | `cmp edx,4; jz` @ `0x979d7f9` (label `0x979d7f0`) | `HloSharding(false,true,false,md)` |
| 5 | UNKNOWN | `cmp edx,5; jz` @ `0x979d7f9` | `HloSharding(false,false,true,md)` |

```c
absl::StatusOr<HloSharding> FromProto(op):           // 0x979d580
    switch (op.type /* [r14+0xC0] */):
      case TUPLE (2):                                 // 0x979d8d0
          reserve(op.tuple_shardings_count);          // [r14+0x48]
          for child in op.tuple_shardings:            // ptr [r14+0x50]
              push_back(FromProto(child));            // recurse @0x979d977
          return HloSharding(move(vector));           // 0x9794a90
      case REPLICATED (0): return HloSharding(true,false,false, md);   // 0x28e95d0
      case MANUAL (4):     return HloSharding(false,true,false, md);
      case UNKNOWN (5):    return HloSharding(false,false,true, md);
      default /* OTHER(3) & MAXIMAL(1) share this tail */:
          if (op.replicate_on_last_tile_dim /* [r14+0x28] */ == 1 && /*maximal cond*/):
              return HloSharding(op.tile_assignment_devices[0], md);   // MAXIMAL 0x9796e40
          tileAssign = (op.iota_flag /* [r14+0xC5] */)
                         ? IotaTileAssignment(dims, reshape /*+0xC8*/, perm)   // V2
                         : Array<long>(op.tile_assignment_devices);           // V1
          if (op.replicate_on_last_tile_dim == 1):
              return HloSharding::PartialTile(tileAssign, md);        // 0x97971a0
          if (op.last_tile_dims /* [r14+0x88] */ non-empty):
              types = collect OpSharding_Type from [r14+0x90];
              return HloSharding::Subgroup(tileAssign, types, md);    // 0x979c050
          return HloSharding(tileAssign, /*replicate_on_last=*/false, md);  // 0x2a9ea70
```

> **GOTCHA — MAXIMAL has no top-level branch.** The ladder tests only `2, 0, 4, 5`;
> MAXIMAL(1) and OTHER(3) both fall through to a *shared tail* and are disambiguated
> there by the `[r14+0x28]==1` test plus a downstream `cmp edx,1`
> (`0x979d88d`/`0x979e090`). Adding a fifth top-level branch for MAXIMAL — the natural
> reading of a six-valued enum — will mis-route OTHER.

### D.3 — `ToProto() const` @ `0x97a0600`

`ToProto` is the exact inverse. It allocates an `OpSharding` (`OpSharding(Arena*, bool)`
@ `0x984f2b0`), reads the packed flags byte `[this+0x80]` to pick the type, and emits the
tile assignment. Its callees: `RepeatedField<long>::Reserve`, `c_copy(Span<long>,
RepeatedFieldBackInserter)`, `TileAssignment::dimensions`/`num_dimensions`/`array`,
`OpMetadata::CopyFrom`, `Arena::CreateMaybeMessage<OpMetadata>`/`<OpSharding>`. Callers
include `ConvertSharding`, `HloInstruction::ToProto`, `HloModule::ToProto`,
`NormalizeAndAssignSharing` — the actual HLO wire boundary.

```c
OpSharding ToProto():                                // 0x97a0600
    p = OpSharding(arena=null, false);               // 0x984f2b0
    if (this->tuple_elements_ non-empty):            // [this+0x00]
        for e in tuple_elements_: p.add_tuple_shardings(e.ToProto());  // recurse
        p.type = TUPLE; return p;
    flags = this[+0x80];                             // test bits
    if (replicated_):  p.type = REPLICATED;
    elif (maximal_):   p.type = MAXIMAL; p.add_tile_assignment_devices(unique_device);
    elif (manual_):    p.type = MANUAL;
    elif (unknown_):   p.type = UNKNOWN;
    else:                                            // OTHER — emit tile assignment
        p.type = OTHER;
        RepeatedField<long>::Reserve(tile.num_dimensions());
        c_copy(tile.dimensions(), back_inserter(p.tile_assignment_dimensions));
        if (tile is iota): set p.iota_reshape_dims / p.iota_transpose_perm;   // V2 compact
        else:              p.tile_assignment_devices <- tile.array();         // V1 dense
        if (replicate_on_last_tile_dim_): p.replicate_on_last_tile_dim = true;
        copy subgroup_types_ -> p.last_tile_dims;     // RepeatedField<OpSharding_Type>
    copy metadata_ -> p.metadata;                     // OpMetadata::CopyFrom
    return p;
```

> **QUIRK — V2 iota tile assignments stay compact across the round-trip.** `ToProto` only
> falls back to the dense `tile_assignment_devices` list when the assignment is *not* an
> iota; an iota stays `(iota_reshape_dims, iota_transpose_perm)`. So an export that built an
> `IotaTileAssignment` (the common case — iota mesh device order, §B.4) serializes to a
> handful of small dims, not an `N`-device vector. A reimplementation that always
> materializes the device list is correct but produces bloated protos and breaks
> byte-for-byte comparison against XLA's output.

### D.4 — End-to-end bridge

```text
EXPORT (sdy → HLO):
  TensorShardingAttr --convertToHloSharding(§B)--> HloSharding --ToProto(§D.3)-->
  OpSharding  --(serialized into mhlo.sharding / HLO frontend attr)
  driver: ExportStablehloShardingsPass

IMPORT (HLO → sdy):
  OpSharding --FromProto(§D.2)--> HloSharding --convertToSdySharding(§C)-->
  TensorShardingAttr   (against a known MeshAttr + deviceId→axisName map)
  driver: ImportShardingsPass / importShardings
```

The only non-bijective step is `DimensionShardingAttr.isClosed` (open/closed), which has
no `OpSharding` field and is reconstructed from the import pass's `openDims` flag (§C.2).

---

## Stock vs Neuron

Everything in §A–§D is **stock OpenXLA**, linked verbatim into `hlo-opt`:

- `mlir::sdy::{MeshAttr, MeshAxisAttr, AxisRefAttr, SubAxisInfoAttr, DimensionShardingAttr,
  TensorShardingAttr}` — the Shardy dialect.
- `xla::sdy::{convertToHloSharding, convertToSdySharding, getOrderedAxisRefs,
  analyzeTileAssignment, shortestCommonFactorization, Export/ImportShardingsPass}` — the
  `xla/service/spmd/shardy` round-trip glue.
- `xla::{HloSharding, OpSharding, TileAssignment, IotaTileAssignment, OpSharding_Type}` —
  XLA core.

The evidence for "stock" is positive, not absence-of-evidence: the symbols are
un-renamed (full `xla::`/`mlir::sdy::` namespaces), the proto field offsets and the
`OpSharding_Type` value order match upstream `xla_data.proto`, and there is no extra
field or Neuron namespace anywhere in the converters or the proto round-trip. Neuron's
sharding code is elsewhere — the propagation driver wiring ([13.2](sharding-propagation.md)),
the LNC (Logical-NeuronCore) constraint that enters only via `MeshAttr` axis sizes and
device order, and the downstream lowering of the resulting `HloSharding` into Penguin/BIR.

## Evidence Anchors and Limits

| Claim | Grounding |
|---|---|
| All five sdy attr storage offsets (§A) | every offset read from its leaf-accessor disassembly |
| `convertToHloSharding` control flow + callee set + result selection | traced in the disassembly |
| `HloSharding` object layout `+0x80`/`+0x88`/`+0x90` + `0x78` metadata stride | read from the ctor's stores |
| `OpSharding` proto field offsets + `FromProto` type-enum ladder | read instruction-by-instruction |
| `ToProto` field emission order + iota-vs-explicit branch | traced in the disassembly |
| Exact `+0x80` flags-byte bit positions; `+0xC5`/`+0xD0` | named by role, not by traced bit index — not byte-verified |
| `shortestCommonFactorization` re-merge behavior | reached transitively via `analyzeTileAssignment`; behavior from the upstream contract |
| The precise `(dims, reshape_dims, transpose_perm)` perm arithmetic in `convertToHloSharding` | [INFERRED] — call order is traced, the permutation vector is not |
| Whether `manualAxes` (3rd convert arg) is ever non-empty in the Neuron pipeline | [INFERRED] — the manual path exists; its activation in Neuron is unverified |

## Related Components

| Name | Relationship |
|---|---|
| `xla::sdy` factor algebra | produces the `TensorShardingAttr` this bridge exports ([13.3](sharding-algebra.md)) |
| `ShardingPropagation` | drives the export pass after the fixpoint converges ([13.2](sharding-propagation.md)) |
| mesh → replica-group math | consumes the exported tile assignment's device order ([13.7](mesh-replica-group-math.md)) |
| `SpmdPartitioner` | rewrites the HLO using the `HloSharding` this bridge yields ([13.1](spmd-partitioner-driver.md)) |

## Cross-References

- [Sharding Algebra](sharding-algebra.md) — 13.3; the factor-space projection
  (`ShardingProjection`, `createTensorShardingAttr`, `AxisRefAttr` algebra) that produces
  the `TensorShardingAttr` this page exports
- [Sharding Propagation](sharding-propagation.md) — 13.2; the fixpoint that fills the
  shardings before `ExportStablehloShardingsPass` calls `convertToHloSharding`
- [Mesh → Replica-Group Math](mesh-replica-group-math.md) — 13.7; how the exported tile
  assignment's device order becomes collective `replica_groups`
- [SPMD Partitioner Driver](spmd-partitioner-driver.md) — 13.1; the consumer that
  partitions the HLO from the resulting `HloSharding`
- [The Compile Pipeline at a Glance](../front/pipeline.md) — where `hlo-opt` sits in the
  driver pipeline
