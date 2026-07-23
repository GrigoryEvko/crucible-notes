# Memory Location and Storage

> *All addresses on this page apply to neuronx_cc 2.24.5133.0+58f8de22 (cp310), `libBIR.so` md5 `12bb979f7ca41248252abb0f16b2da98`. cp311/cp312 VAs drift; the field offsets, enum values, and ABI are stable across them. VA == file offset for `.text` (0x1820c0–0x707a44) and `.rodata` (0x708000–0x7a118c); `.data.rel.ro` (vtables/typeinfo) carries a −0x1000 delta. Two symbol copies exist for most methods — a low-addr thunk and the real high-addr body; every address cited here is the real body.*

## Abstract

A BIR tensor is split into two objects: a **`MemoryLocationSet`** that carries the *logical* tensor — its dtype, shape, partition dimension, tensor-class, I/O role, and address-space — and one-or-more **`MemoryLocation`** records that each carry one *physical* placement of that tensor in a specific memory (SBUF, PSUM, or DRAM) at a specific address. Both derive from a common **`bir::StorageBase`** that holds the SSA use-def lists the allocator consumes; a third sibling, **`bir::Storage`**, is the pre-placement SSA value (the "virtual register"). The `MemoryLocation` is the object the walrus coloring allocator paints: it is to BIR what a colored interference-graph node is to a register allocator, except the "color" is a `(MemoryType, address, bank, base-partition)` tuple instead of a physical register number.

The 584-byte `MemoryLocation` struct is the unit of this page. It holds the placement fields, an identity block (`tensor_id`/`var_id`/`memLocSetId`), a back-pointer to its owning Set, a `pointer_ref_memloc` for indirect tensors, and — the heart of the alias model — a **sorted `boost::container::vector<AliasPtr>`** where each `AliasPtr` is an `llvm::PointerIntPair<MemoryLocation*, 3, AliasKind>` packing a peer location pointer with a 3-bit alias-kind bitmask into a single 8-byte word. The alias relation is **bidirectional** (every edge is recorded on both endpoints) and **DRAM-only**: `addAlias` hard-throws unless *both* endpoints are `MemoryType::DRAM`. That single guard is the reimplementation-critical fact — the BIR alias graph is an equivalence/overlap relation over off-chip buffers, never over on-chip SBUF or PSUM.

If you know LLVM, the closest analogues are `MachineOperand`'s frame-index plus `AliasSetTracker`: the Set is the logical SSA value, the `MemoryLocation` is a stack-slot/frame placement, `StorageBase` is the def-use root, and the `AliasPtr` vector is a per-location must/may-alias edge list. BIR diverges in that aliasing is *typed* (a 3-bit OR-able kind, of which only `1` and `3=MustAlias` are observed), *symmetric by construction*, and *scoped to DRAM by a runtime assertion baked into the edge-insert path*.

For reimplementation, the contract is:

- **The `MemoryLocation` layout** — the 584-byte (`0x248`) struct: placement fields (`type`/`addr`/`bank`/`base`/`pack_id_hint`), the `StorageBase` use-def prefix, the pointer-indirected enum block, the identity block, and the alias vector triple.
- **The `MemoryType` / `MemoryAddressSpace` taxonomy** — `MemoryType` is a power-of-two bit-flag enum (DRAM=8 / SB=16 / PSUM=32 are the only placeable memories); `MemoryAddressSpace` (Local=0 / Shared=1 / Debug=2) is a DRAM-only attribute.
- **The `AliasPtr` `PointerIntPair` encoding** — `word = (MemoryLocation* & ~7) | (kind & 7)`, with the alignment and field-width asserts that pin the 3-bit split.
- **The alias model** — the DRAM-only guard, the sorted bidirectional insert, the kind-OR merge, and how the wire `"alias"` array round-trips through it.

| | |
|---|---|
| **Defining binary** | `neuronxcc/starfish/lib/libBIR.so` (source `walrus/ir/lib/IR/{MemoryLocation,StorageBase}.cpp`) |
| **`MemoryLocation` size** | **584 bytes** (`operator new(0x248u)` @0x34e7e0; `operator delete(_,0x248u)`) |
| **`MemoryLocation` typeinfo** | `_ZTI` @0x8fdb88 — `__vmi`: `StorageBase`@0 + `NamedObject<ML,MLS>`@0x118 + `SrcHandle`@0x118 |
| **`StorageBase` root** | `_ZTI` @0x9000e0 (`__class`, no base); vtable @0x900140; sole virtual `setUserAllocated` |
| **`AliasPtr`** | `llvm::PointerIntPair<MemoryLocation*, 3, AliasKind>` — 8 bytes; ctor @0x231560 |
| **Alias vector** | `boost::container::vector<AliasPtr>` @ +504 (begin) / +512 (size) / +520 (cap), sorted |
| **DRAM-only guard** | `addAlias(AliasPtr)` @0x331680 — throws `"… . Aliasing only permitted for DRAM locations."` |
| **Placeable memories** | DRAM=8, SB=16, PSUM=32 (proven by `getLatency` @0x3365a0 branch set) |

---

## The three-class memory model

### Purpose

BIR keeps a logical tensor and its physical placement(s) in separate objects so that one tensor can be placed more than once (e.g. a DRAM buffer plus an SBUF tile copy) while sharing one identity, one shape, and one set of alias relations. The `MemoryLocationSet` is the logical owner; the `MemoryLocation` is the placement; `bir::Storage` is the pre-placement SSA value. All three share a `StorageBase` prefix.

### Class hierarchy

The hierarchy is recovered from the `__vmi_class_type_info` base relocations (`readelf -rW`), not from naming. `StorageBase` is the true root; `MemoryLocation` derives it **directly** (not through `Storage`), while `MemoryLocationSet` and `Register` derive `Storage`.

```text
bir::StorageBase                         _ZTI 0x9000e0  (__class root; vtable 0x900140)
  │  use-def lists (readers/writers DenseSets, alias-rdr/wtr + BB-arg SmallVectors),
  │  user_allocated flag, storage-kind discriminator @+272
  ├── bir::Storage                       _ZTI 0x9000f0  (__vmi: StorageBase@0 + NamedObject<Storage,Function>)
  │     │  the SSA value / "virtual register" — pre-placement; refcounted payload @+376
  │     ├── bir::MemoryLocationSet        _ZTI 0x8fdc70  (+ NOC<MLS,MemLoc> + SrcHandle@0x180)
  │     │       one LOGICAL tensor; owns a MemoryLocation list + name→MemLoc symtab;
  │     │       carries tensor_class / kind / addr_space / dtype / shape / partition_dim
  │     └── bir::Register                 _ZTI 0x8ffde8  (__si Storage)
  └── bir::MemoryLocation                 _ZTI 0x8fdb88  (__vmi: StorageBase@0 + NamedObject<ML,MLS>@0x118 + SrcHandle@0x118)
          one PHYSICAL placement; type(MemoryType) / addr / bank / base / pack_id_hint /
          alias vector / pointer_ref_memloc
```

`setUserAllocated` is `StorageBase`'s *sole* own virtual; `toJson` is added per concrete leaf at vtable slot 3, so `StorageBase` is never serialized as a bare base. `MemoryLocation`'s primary vtable @0x8fdc08 is `{D1, D0, setUserAllocated, getDebugInfoSource}` plus `_ZThn360_` (`360`=`0x168`) thunks for the `SrcHandle` sub-object. *Anchors: the `__vmi` base relocs and the vtable slot relocs.* The full polymorphism map of the storage family — and the four other structural roots (`Argument`, `pelican::Expr`, `sync::SyncRef`, `DMAQueue`/`Hwm`) — is on the [BIR structural hierarchy](container-model.md) page's sibling analysis.

> **NOTE —** `MemoryLocation` IS-A `StorageBase` directly but is *also* a `NamedObject<MemoryLocation, MemoryLocationSet>` (its name/id sub-object) and a `logging::SrcHandle` (its source-location diagnostic). The `NamedObject` sub-object sits at `MemoryLocation+0x118` (280) — proven below by the id cascade — and the `SrcHandle` third base shares that `0x118` offset region. This is why the offsets in the layout table jump around the `+280`/`+352` band: those bytes are the embedded `NamedObject` node (list hooks, name, origin, id, owner-back-pointer), not placement data.

### Set vs Location: which attribute lives where

This split is structurally decisive for a reimplementation because the wire schema and every accessor obey it. `TensorClass` and `MemoryAddressSpace` are **Set-level** (one per logical tensor); `MemoryType` is **Location-level** (one per placement). The xref map proves it: the `TensorClass` serializer is called only from `MemoryLocationSet::{toJson,createFromJson}`, while `MemoryType2string`/`MemoryAddressSpace2string` are called from `MemoryLocation::{toString,getLatency}`.

| Attribute | Lives on | Wire key | Why |
|---|---|---|---|
| `TensorClass` | `MemoryLocationSet` | `tensor_class` | the buffer family is a property of the logical tensor |
| `TensorKind` (I/O role) | Set + mirrored on Location | `kind` | module-boundary marking, set once |
| `MemoryAddressSpace` | `MemoryLocationSet` | `addr_space` | DRAM sharing scope — one per tensor |
| dtype / shape / partition_dim | `MemoryLocationSet` | `dtype` / `tensor_shape` / `partition_dim` | byte size the colorer must fit |
| `MemoryType` (SB/PSUM/DRAM) | `MemoryLocation` | `type` | which physical memory *this placement* lands in |
| addr / bank / base / pack_id_hint | `MemoryLocation` | `addr` / `bank` / `base` / `pack_id_hint` | the placement coordinates |
| alias edges | `MemoryLocation` | `alias` | physical overlap is per-placement |

---

## The MemoryLocation struct (584 bytes)

### Purpose

`MemoryLocation` is the object the walrus allocator colors. Its fields divide into five bands: (1) the `StorageBase` use-def prefix (`+0..+272`), (2) a block of *pointer-indirected* enum fields (`type`/`kind`/`addr_space`/`pinned`) that are read as `**(this+offset)`, (3) the embedded `NamedObject` name/id node (`+280..+352`), (4) the alias vector triple (`+504..+520`), and (5) the inline scalar placement/identity tail (`+528..+579`).

### Size, anchored

The size is `0x248` = **584 bytes**, taken directly from the minting and freeing sites — not inferred. `NamedObjectContainer<MemoryLocationSet,MemoryLocation>::insertElement<MemoryLocation>` @0x34e7e0 does `v64 = operator new(0x248u)`, runs the ctor, calls `getUniqueId(v64 + 280)`, and on the name-collision rollback does `operator delete(v64, 0x248u)` — both `0x248u` constants read straight from the body.

### The pointer-indirected enum block

The enum-valued fields are *not* stored inline. `type`/`kind`/`addr_space`/`pinned` each hold a **pointer** into a separately-allocated `StorageBase` enum sub-block whose `[0]` is the actual int. `toString` reads the type as `**(unsigned int **)(this + 216)` and `MemoryAddressSpace` as `**(unsigned int **)(this + 240)`; the wire ctor writes the same way (`**(this+216) = MemoryType`). A reimplementation may flatten these to inline ints, but a binary-faithful one must allocate the indirection block. *Anchors: `toString` @0x330560 lines 95/249; `createFromJson` @0x33bcf0.*

### Layout

Offsets are byte-exact unless a confidence tag says otherwise. Every offset is anchored to a setter write, an accessor read, or a (de)serializer site.

| Field | Offset | Type | Meaning / wire key | Anchor | Conf |
|---|---|---|---|---|---|
| vtable | +0 | `StorageBase*` vptr | typeinfo 0x8fdb88; vtable 0x8fdc08 | dtor | CERTAIN |
| `StorageBase` use-def | +8..+207 | DenseSets + SmallVecs | readers @+8/+24, writers @+48/+64, alias-rdr/wtr + BB-arg lists | `register*` | CERTAIN |
| `allocated` | +168 | bool | `"allocated"` (`setAllocated` mirrors onto owning Set) | 0x330390 | CERTAIN |
| `user_allocated` | +208 | bool | `"user_allocated"` (`StorageBase::setUserAllocated`) | toJson:502 | CERTAIN |
| `type` (`MemoryType`)\* | +216 | `ptr → [0]=i32` | `"type"` — DRAM8/SB16/PSUM32/… | cFJ:119 | CERTAIN |
| `kind` (`TensorKind`)\* | +224 | `ptr → [0]=i32` | I/O role; `Pointer=8` gates `pointer_ref_memloc` | setRefMemLoc | CERTAIN |
| `addr_space`\* | +240 | `ptr → [0]=i32` | `MemoryAddressSpace` — read only when `type==DRAM` | toString:249 | CERTAIN |
| `pinned`\* | +248 | `ptr → [0]=bool` | `"pinned"` | toJson:854 | CERTAIN |
| dims count (?) | +256 | `ptr → [0]=u32` | part of `"dims"`/`"memLocSetId"` 2-tuple | cFJ:561 | MED |
| name / addr-origin | +264 | `ptr → [0]=qword` | variable/address name | toString:289 | CERTAIN |
| storage-kind tag | +272 | i32 | `4 = MemoryLocation` (isa<> discriminator) | PhysAP::getMLS 0x3aee40 | HIGH |
| `NamedObject` node | +280..+352 | name/id sub-object | list hooks, name, origin, `unique_id`, owner-ptr | insertElement | CERTAIN |
| `optin_passes` | +296 | container | `"optin_passes"` (NamedObjectBase mask) | toJson:717 | HIGH |
| `origin` | +328 | i32 | `"origin"` (`NamedObjectOrigin`) | toJson:344 | CERTAIN |
| owning Set back-ptr | +352 | `MemoryLocationSet*` | `getMemoryLocationSet` (`this+44`) | 0x32db30 | CERTAIN |
| `remote_local_target` | +368/+376/+408 | bool/string/u32 | `"remote_local_target"` (gated by +368) | toJson:915 | CERTAIN |
| `alloced_func` | +416/+424 | bool/string | `"alloced_func"` | toJson:881 | CERTAIN |
| `remote_writer_func` | +456/+464 | bool/string | `"remote_writer_func"` | toJson:898 | CERTAIN |
| `pointer_ref_memloc` | +496 | `MemoryLocation*` | `"pointer_ref_memloc"` (asserts `kind==Pointer`) | setRefMemLoc 0x32e0a0 | CERTAIN |
| **alias vector begin** | +504 | `AliasPtr*` | `boost::container::vector<AliasPtr>` begin | addAlias 0x331680 | CERTAIN |
| **alias vector size** | +512 | u64 | `"alias"` array length | addAlias | CERTAIN |
| **alias vector cap** | +520 | u64 | capacity | addAlias | CERTAIN |
| `bank` | +528 | **i64 (signed)** | `"bank"` PSUM bank (toString takes abs) | setBankId 0x32dc40 | CERTAIN |
| `addr` | +536 | u64 | `"addr"` linear BYTE address (SB/PSUM/DRAM offset) | setAddress 0x32db60 | CERTAIN |
| `device_print_metadata` | +544 | `DevicePrintMD*` | `"device_print_metadata"` (lazy `new(0x28)`) | toJson:1228 | CERTAIN |
| `tensor_id` | +552 | i32 | `"tensor_id"` (node+272 in ilist; `-1`=unset) | toJson:729 | CERTAIN |
| `pack_id_hint` | +556 | i32 | `"pack_id_hint"` (PSUM-only; default `-1`) | setPackIdHint 0x32df90 | CERTAIN |
| `base` partition | +560 | u32 | `"base"` SB/PSUM base partition | setBasePartition 0x32dfb0 | CERTAIN |
| `var_id` | +564 | i32 | `"var_id"` (emitted if `>=0`) | toJson:732 | CERTAIN |
| `table_entry_id` | +568 | i32 | `"table_entry_id"` (emitted if `>=0`) | toJson:798 | CERTAIN |
| `memLocSetId` | +572 | i32 | `"memLocSetId"` (emitted if `>=0`) | toJson:659 | CERTAIN |
| `location_alt` | +576 | bool | `"location_alt"` | toJson:825 | CERTAIN |
| `written_by_another_core` | +577 | bool | `"written_by_another_core"` | setIsWritten… 0x32e470 | CERTAIN |
| `is_shared_post_dram_alloc` | +578 | bool | `"is_shared_post_dram_alloc"` | 0x32e480 | CERTAIN |
| `runtime_reserved` | +579 | bool | `"runtime_reserved"` | toJson:688 | CERTAIN |

`*` = pointer-indirected enum field (read/written as `**(this+offset)`).

> **GOTCHA — `+536` is the byte address, not the SB base partition.** The three one-line setters pin the split exactly: `setAddress` writes `*((u64*)this + 67)` = **+536** = the linear byte *address* (wire key `"addr"`); `setBasePartition` writes `*((u32*)this + 140)` = **+560** = the base *partition* (wire key `"base"`); `setBankId` writes `*((i64*)this + 66)` = **+528** = the PSUM *bank* (wire key `"bank"`). `toString`'s SBUF branch prints `+536` because it prints the byte address — reading that as the partition is the easy mistake. (Setters @0x32db60 / 0x32dfb0 / 0x32dc40.)

### Addressing by MemoryType

`MemoryLocation::toString` @0x330560 renders the placement differently per `MemoryType`. The branch set is the canonical evidence for which memories are placeable:

```c
function toString(this):                          // @0x330560
    name = MemoryType2string(**(u32**)(this+216))  // line 95
    type = **(u32**)(this+216)                      // line 99
    if type == 16:                  // SB
        render *(u64*)(this+536)    // byte addr (line 103) + base partition (+560)
    else if type == 32:             // PSUM
        bank = *(i64*)(this+528)    // SIGNED bank id (lines 166-168)
        render abs(bank)            // toString prints |bank| + addr
    else if type == 8:              // DRAM
        render MemoryAddressSpace2string(**(u32**)(this+240))  // line 249 — addr_space, DRAM-only
    // all other MemoryType (Unallocated1/Input2/Output4/RNGSTATE64/REG128): no placement render
```

> **QUIRK —** the PSUM `bank` field is a **signed** `int64`, and `toString` takes its absolute value. A reimplementation that stores the bank unsigned will round-trip the magnitude but lose whatever the sign encodes (the producer side that writes a negative bank was not located; the sign is consumed only by the abs here). Store it signed. [CERTAIN — `toString` lines 166–169 compute `v10 = (bank > 0) ? bank : -bank`.]

> **GOTCHA —** `pack_id_hint` (+556) is **PSUM-only**. `toJson` asserts `getType()==PSUM` (`**(this+216)==32`) before emitting it, fataling `"… PackIdHint only applies to PSUM."` (MemoryLocation.cpp:0x7BD); `createFromJson` carries the symmetric assert. The default is `-1` (`setPackIdHint(-1)`). A reimplementation that sets a non-`-1` `pack_id_hint` on a non-PSUM location will hard-fail at serialize time. [CERTAIN — string-anchored assert.]

---

## The memory taxonomy

### MemoryType — a power-of-two bit-flag enum

`MemoryType` is **not** an ordinal `0..7` enum — its values are powers of two, decoded byte-exact from both `MemoryType2string` @0x3ca040 (the forward switch) and `string2MemoryType` @0x3ca160 (the inverse `StringSwitch` magic). The forward body is `if (a2 <= 32) switch(case 1/2/4/8/16/32); if (a2==64) RNGSTATE; if (a2==128) REG; else fatal "Unknown memory type"`.

| Value (`1<<n`) | Name (verbatim) | Bit | Role |
|---|---|---|---|
| 1 | `Unallocated` | 0 | pre-allocation placeholder |
| 2 | `Input` | 1 | host-boundary input buffer |
| 4 | `Output` | 2 | host-boundary output buffer |
| 8 | **`DRAM`** | 3 | off-chip HBM — **placeable**, aliasable |
| 16 | **`SB`** | 4 | on-chip SBUF — **placeable** |
| 32 | **`PSUM`** | 5 | on-chip PSUM — **placeable** |
| 64 | `RNGSTATE` | 6 | RNG engine state |
| 128 | `REG` | 7 | scalar register file |

All eight values are CERTAIN, byte-decoded in both directions. The enum is *defined* in `walrus/ir/lib/IR/StorageBase.cpp` (the fatal-path string carries the source path).

> **QUIRK —** because the values are powers of two, a `type` field *could* OR multiple flags, but no observed site combines them — each `MemoryLocation` has exactly one type. Whether `Input|DRAM` is ever formed is unverified (D-D05 G2); treat `type` as a single enum value in practice but as a bitmask in the struct. [LOW — no OR-combination site found.]

The three placeable memories are exactly **DRAM=8 / SB=16 / PSUM=32**, proven independently by `getLatency` @0x3365a0: it branches only on own-type ∈{8,16,32} × dst-type ∈{8,16,32}, dispatching to the `bir::Hwm` cost model per `(src,dst)` pair; any other combination fatals `"RESOLUTION_CONTACT_SUPPORT"` with `src_mem_type`/`dst_mem_type` keys. *Anchors: `getLatency` branch lines 184–206, plus the assert string at line 239.*

### MemoryAddressSpace — a DRAM-only attribute

`MemoryAddressSpace` (Set-level, wire key `addr_space`) is read **only** in the DRAM branch of `toString`. Its values, byte-decoded both ways from `MemoryAddressSpace2string` @0x3cad10 (`if (a2==1) Shared; else if (a2==2) Debug; else { if(a2) fatal; Local }`) and the inverse:

| Value | Name | Meaning |
|---|---|---|
| 0 | `Local` | per-core private off-chip buffer (falls through from 0) |
| 1 | `Shared` | off-chip buffer shared across cores |
| 2 | `Debug` | instrumented / debug buffer |

> **NOTE — the ordinals are `Local=0 / Shared=1 / Debug=2`.** Both directions agree: the forward switch falls `0` through to `Local`, and the inverse `StringSwitch` magic for `"Local"`/`"Shared"`/`"Debug"` decodes the same way. A roster that starts `Shared=0` has the order wrong.

The address-space axis exists because off-chip DRAM buffers can be shared across cores, whereas on-chip SBUF/PSUM are inherently per-core. This is the same reason the alias graph is DRAM-only (next section), and it couples to the multi-core sharing fields `is_shared_post_dram_alloc` (+578), `remote_local_target` (+368), and `remote_writer_func` (+456). SB vs PSUM, by contrast, is discriminated at descriptor-emit time by the `ADDR4` byte-address region class (PSUM occupies a fixed 4 MiB window), not by an address-space tag — see [SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md).

### TensorClass and TensorKind (Set-level)

`TensorClass` (13 values, `0..12`, dense switch, wire key `tensor_class`) names the buffer family — `Neuron*` prefixes are SBUF/on-chip families, `DRAM*` are off-chip, the `2D`/`3D` suffix is the access-pattern descriptor dimensionality. `TensorKind` (10 values, `0..9`, wire key `kind`) is the I/O role, deliberately blocked so that `0..2` are input kinds, `3..5` output kinds (proven by the `isTensorKind*` group predicates `k<=1`, `k<=2`, `(k-3)<=1`, `(k-3)<=2`), with `6=Const`, `7=Internal`, `8=Pointer`, `9=InternalInterface`. The two axes are orthogonal: a `NeuronWeightTensor` (class 3) typically has kind `ExternalInputParameter` (1). These two enums are owned by the logical Set, not the placement; their full rosters are tabulated where the loader reads them ([Penguin tensor/buffer nodes](../penguin/tensor-buffer-node.md)).

> **NOTE —** there is **no** `TensorClass→MemoryType` classifier function in `libBIR` (no `getMemoryType()`/`inferTensorClass()`). The class→memory binding is established by the walrus allocator and merely *serialized* here as both `tensor_class` (Set) and per-location `type`. The allocation pass that performs the binding is documented under the walrus allocators *(planned)*.

---

## The alias model

### Purpose

The alias relation answers "do these two physical placements share storage, or may they overlap?" — the input to the allocator's coalescing/forced-equivalence step. It is stored as a per-`MemoryLocation` sorted vector of `AliasPtr`s, each a packed pointer-to-peer plus a 3-bit alias kind, and it is **symmetric**: every edge is recorded on both endpoints. Crucially it is **DRAM-only** — the edge-insert path hard-throws on any non-DRAM endpoint.

### AliasPtr — the PointerIntPair encoding

`AliasPtr` is exactly `llvm::PointerIntPair<MemoryLocation*, 3, AliasKind>` — confirmed by the assert strings naming the template instantiation. It packs an 8-byte-aligned `MemoryLocation*` (high 61 bits) with a 3-bit `AliasKind` mask (low 3 bits) into one 8-byte word:

```c
function AliasPtr(self, MemoryLocation* ptr, AliasKind kind):   // ctor @0x231560
    *self = 0
    assert (ptr  & 7) == 0   // "Pointer is not sufficiently aligned"  (PointerIntPair.h:0xCA)
    assert (kind & ~7) == 0  // "Integer too large for field"          (PointerIntPair.h:0xD1, IntBits=3)
    *self = kind | (ptr & ~7)        // low 3 bits = kind, high bits = ptr

function AliasPtr::addAlias(self, AliasKind kind):              // @0x231600 — OR a kind in
    new = (*self & 7) | kind
    assert (new & ~7) == 0
    *self = new | (*self & ~7)       // merge: ptr unchanged, kind bits OR'd

function AliasPtr::hasAlias(self, AliasKind kind) -> bool:      // @0x2315f0 — SUBSET test
    return (kind & *self & 7) == kind       // every bit of `kind` is set

function AliasPtr::getAliasKind(self) -> vector<AliasKind>:     // @0x231660
    out = {}
    if hasAlias(self, 1): out.push(1)
    if hasAlias(self, 3): out.push(3)       // overlapping kinds → multiple entries
    return out
```

The 3-bit field is an **OR-able bitmask**, not an enum index. Only kinds `1` and `3` are observed in the binary: `3 = MustAlias` (recovered from `getMustAlias`, which scans for `hasAlias(3)`), and `1` a weaker/may-alias bit. Because `hasAlias` is a subset test, `3` implies `hasAlias(1)` is *false* unless bit 0 is also set — the two kinds are independent bits 0 and 1 combined as `1` and `3` (= bits {0} and {0,1}). [CERTAIN for kinds 1,3; the full `AliasKind` roster is a GAP — there is no `AliasKind2string` in `libBIR`, so whether a `2` or `4` exists is unverified. Up to 3 independent kind bits are structurally possible.]

### getMustAlias — reading the relation

```c
function getMustAlias(this) -> MemoryLocation*:    // @0x32ffe0
    size  = *(u64*)(this+512)                      // alias vector size  (+0x200)
    if size == 0: return 0
    begin = *(AliasPtr**)(this+504)                // alias vector begin (+0x1F8)
    for entry in [begin, begin+size):
        if AliasPtr::hasAlias(entry, 3):           // first MustAlias edge
            return *entry & ~7                      // the peer MemoryLocation*
    return 0
```

So a "must-alias" query is a linear scan for the first `hasAlias(3)` entry, returning the masked peer pointer. The vector is kept sorted (by `MemoryLocation::AliasPtrCmp`), so a reimplementation could binary-search, but the stock code linear-scans `getMustAlias` (@0x32ffe0).

### addAlias — the DRAM-only bidirectional insert

This is the reimplementation-critical function. `addAlias(MemoryLocation* dst, AliasKind k)` @0x331b10 builds an `AliasPtr(dst,k)` and calls `addAlias(AliasPtr)` @0x331680, which (a) **asserts both endpoints are DRAM**, (b) sorted-inserts into `this`'s alias vector (OR-merging the kind if a `dst` edge already exists), and (c) does the **same** on `dst`'s alias vector — making the edge symmetric.

```c
function addAlias(this, AliasPtr ap):                            // @0x331680
    dst = ap & ~7
    // ── THE DRAM-ONLY GUARD ──────────────────────────────────────────────
    if **(u32**)(this+216) != 8  ||  **(u32**)(dst+216) != 8:    // own type, dst type
        throw std::runtime_error(
            this.name + " -> " + dst.name +
            ". Aliasing only permitted for DRAM locations.")     // MemoryLocation.cpp
    // ── INSERT INTO this->alias (sorted by AliasPtrCmp) ──────────────────
    pos = lower_bound(this.alias[+504..], ap, AliasPtrCmp)       // binary search
    if pos found and pos->ptr == dst:
        *pos = (*pos & ~7) | ((*pos | ap) & 7)                   // OR the kind in
    else:
        emplace ap at pos (boost::container::vector, may reallocate at cap +520)
    // ── INSERT THE REVERSE EDGE INTO dst->alias ─────────────────────────
    back = AliasPtr(this, ap & 7)                                // reverse direction
    pos2 = lower_bound(dst.alias[+504..], back, AliasPtrCmp)
    if pos2 found and pos2->ptr == this:
        *pos2 = (*pos2 & ~7) | ((*pos2 | ap) & 7)
    else:
        emplace back at pos2
```

The guard reads `**(this+216)` (own `MemoryType` through the indirection pointer) and `**((ap&~7)+216)` (dst's `MemoryType`); both must equal `8` (DRAM). The thrown string is built as `"<src> -> <dst>. Aliasing only permitted for DRAM locations."`. *Anchors: `addAlias(AliasPtr)` @0x331680 — the `*v5 != 8 || **(...+216) != 8` branch and the `runtime_error` construction.*

> **GOTCHA —** the alias graph is a **DRAM-only equivalence/overlap relation**. SBUF and PSUM placements *cannot* carry alias edges — attempting it throws. A reimplementation that builds an alias edge between two SBUF tiles (the obvious thing to do for a coalescing register allocator) will diverge from the binary, which models on-chip non-interference entirely through the def-use live ranges, not the alias vector. On-chip overlap is the allocator's coloring job; the alias vector exists only to force *off-chip* buffers to share storage. [CERTAIN.]

> **QUIRK —** the edge is symmetric *by construction* (the reverse half always runs), and the kind is *merged by OR* when the edge already exists. So `addAlias(A, B, 1)` followed by `addAlias(A, B, 3)` leaves both `A→B` and `B→A` with kind `1|3 = 3`. Adding the same edge twice never duplicates; it accumulates kind bits. A reimplementation must OR, not overwrite. [CERTAIN — the `(*pos | ap) & 7` merge on both halves.]

### Wire round-trip

The on-disk `"alias"` array round-trips through this in-memory field. `MemoryLocation::toJson` @0x339140 walks the +504 vector, masks `*entry & ~7` for the peer `MemoryLocation` and `*entry & 7` for the kind, and emits `(destSetName, destMemLocName, kind)` triples. The loader `addMemLocAliasesFromJson` @0x26f020 runs as the **second pass** (after all `MemoryLocation`s exist), resolving each `[destSetName, destMemLocName, aliasKind]` entry to a `(Src, Dst, kind)` and calling `addAlias` — so the wire array re-enters the same DRAM-only, OR-merging, bidirectional insert. It supports two schema versions:

| Schema | `"alias"` entry shape | Resolution | Asserts |
|---|---|---|---|
| **NEW** (set-keyed) | `[destSetName, destMemLocName, kind]` (3-tuple) | `getMemoryLocationSetByName` → `getMemoryLocationByName` | `"'alias' must be a JSON array"`, `"Unknown alias destination"`, `"Found MemoryLocation that has not been read"` |
| **OLD** (memloc-keyed) | `[destMemLocName, kind]` (2-tuple) | `Function::getMemoryLocationByName` | parallel asserts at 0x248/0x24B/0x24D/0x24F |

The `aliasKind` is the last array element, coerced from JSON number types `4(bool)/5(u32)/6(i64)/7(double)` to `int` and passed straight to `addAlias`, which ORs it — this is exactly what makes the wire and the in-memory 3-bit field consistent. *Anchors: `addMemLocAliasesFromJson` @0x26f020, both schema branches.* The two-pass model and the tensor-side `tensor_map` aliasing it feeds are documented under the BIR JSON loader and the tensor-map alias page *(both planned)*.

---

## Identity and the global id cascade

### Purpose

Every `MemoryLocation` carries a stable identity the allocator and the downstream NEFF emit key on: `tensor_id` (+552), `var_id` (+564), `table_entry_id` (+568), `memLocSetId` (+572), plus the global `unique_id` in the embedded `NamedObject` node. The `unique_id` is allocated by the same cross-tree cascade that numbers every BIR object — it is *not* per-Set.

### The cascade, cross-linked

`MemoryLocation` is a `NamedObject<MemoryLocation, MemoryLocationSet>`, and its `getUniqueId` chains up to the single `Module+0x98` counter. The binary makes the chain explicit:

```c
function getUniqueId_MemoryLocation(node):          // @0x32dab0 (node = MemoryLocation+0x118)
    MemoryLocationSet* mls = *(node + 0x48)         // owner slot (NamedObject sub+0x48)
    tailcall getUniqueId_Storage(mls + 0x118)       // MLS's NamedObject<Storage,Function> sub-object
```

Two offsets pin this. First, the `NamedObject` sub-object sits at **`MemoryLocation+0x118`** (280) — `insertElement` calls `getUniqueId(v64 + 280)` on the fresh object. Second, the owner back-pointer that `getMemoryLocationSet` returns is `*((u64*)this + 44)` = **`MemoryLocation+352`** (= `+280 + 0x48`, the `NamedObject` sub-object's owner slot). So the same `+0x48` universal owner slot that the [container model](container-model.md) page traces lands at `MemoryLocation+352`, and the cascade adds `+0x118` to reach the *MLS's* `NamedObject<Storage,Function>` sub-object before delegating upward. *Anchors: `getUniqueId` @0x32dab0, `getMemoryLocationSet` @0x32db30, `insertElement` @0x34e7e0 — consistent with the 7.3 id-cascade analysis.*

> **NOTE —** `tensor_id` (+552) is *also* read off the ilist node at `node+272` during the loader's `buildTensorId2MemLoc` pass, which asserts every `getTensorId() >= 0` (`"Unassigned tensorId"`, MemoryLocationSet.cpp:0x1D9) and populates the Set's `tensorId2MemLoc` lookup vector. Do not conflate this node-relative `+272` (= `MemoryLocation+552`, the `tensor_id`) with the `StorageBase` storage-kind discriminator at struct-`+272` (value `4`=MemoryLocation). They are different fields at coincidentally the same numeric offset relative to different bases. [HIGH — both verified independently; D-E13 note.]

---

## Allocator-facing summary

What the walrus coloring allocator reads and writes on these objects, grouped by role:

| Role | Object | Fields | Direction |
|---|---|---|---|
| **Placement** | `MemoryLocation` | `type`(+216), `addr`(+536), `bank`(+528), `base`(+560), `pack_id_hint`(+556), `allocated`(+168), `user_allocated`(+208), `pinned`(+248), `runtime_reserved`(+579) | allocator **output** |
| **Liveness / interference** | `StorageBase` | readers DenseSet(+8/+24), writers DenseSet(+48/+64), alias-rdr/wtr + BB-arg SmallVecs | allocator **input** |
| **Alias / coalescing** | `MemoryLocation` | alias vector (+504/+512/+520) — DRAM-only must/may relation | allocator **input** |
| **Constraints** | `MemoryLocationSet` | `do_not_spill`/`virtual`(+1152), `do_not_rotate`(+1153) | allocator **input** |
| **Identity** | both | `tensor_id`(+552), `var_id`(+564), `table_entry_id`(+568), `memLocSetId`(+572); Set's `tensorId2MemLoc` vector | stable keys |
| **Size / class** | `MemoryLocationSet` | `tensor_class`(+232), `dtype`(+816), `tensor_shape`, `partition_dim`(+820) | allocator **input** |

The def-use lists (readers/writers DenseSets) yield the live ranges that build the interference graph; the alias vector is the forced-equivalence input; the shape/class/dtype drive the byte size the colorer must fit. `setAllocated` mirrors the `allocated` flag onto the owning Set too (`+168` on both). The allocator that consumes all of this lives in the walrus libraries *(planned)*.

---

## Function map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `MemoryLocation::toString[cxx11]` | 0x330560 | placement render — the field map | CERTAIN |
| `MemoryLocation::getLatency(MemoryType)` | 0x3365a0 | (src,dst) Hwm cost; proves DRAM/SB/PSUM-only | CERTAIN |
| `MemoryLocation::toJson` | 0x339140 | wire serializer (29 keys) | CERTAIN |
| `MemoryLocation::createFromJson` | 0x33bcf0 | wire ctor + setters | CERTAIN |
| `MemoryLocation::addAlias(MemLoc*, AliasKind)` | 0x331b10 | builds `AliasPtr`, delegates | CERTAIN |
| `MemoryLocation::addAlias(AliasPtr)` | 0x331680 | DRAM-only guard + bidirectional sorted insert | CERTAIN |
| `MemoryLocation::getMustAlias` | 0x32ffe0 | scan alias vec for `hasAlias(3)` | CERTAIN |
| `MemoryLocation::getMemoryLocationSet` | 0x32db30 | owner back-ptr (`this+352`) | CERTAIN |
| `MemoryLocation::isSharedPostDRAMAlloc` | 0x32e480 | read +578 | CERTAIN |
| `MemoryLocation::setAddress / setBankId / setBasePartition / setPackIdHint` | 0x32db60 / 0x32dc40 / 0x32dfb0 / 0x32df90 | placement setters (+536/+528/+560/+556) | CERTAIN |
| `AliasPtr::AliasPtr(MemLoc*, AliasKind)` | 0x231560 | `PointerIntPair<*,3,kind>` pack | CERTAIN |
| `AliasPtr::addAlias / hasAlias / getAliasKind` | 0x231600 / 0x2315f0 / 0x231660 | 3-bit kind bitmask ops | CERTAIN |
| `NamedObject<ML,MLS>::getUniqueId` | 0x32dab0 | id cascade → `MLS+0x118` → Storage | CERTAIN |
| `NOC<MLS,MemLoc>::insertElement<MemLoc>` | 0x34e7e0 | `new(0x248)` = 584-byte mint | CERTAIN |
| `addMemLocAliasesFromJson(Function&, json)` | 0x26f020 | second-pass alias-graph wire loader | CERTAIN |
| `MemoryType2string` / `MemoryAddressSpace2string` | 0x3ca040 / 0x3cad10 | enum decoders (both directions) | CERTAIN |

---

## Evidence summary

| Claim | Grounding | Confidence |
|---|---|---|
| The struct is 584 bytes | `insertElement<MemoryLocation>` @0x34e7e0 contains `operator new(0x248u)` (line 398) and the rollback `operator delete(v64, 0x248u)` (line 450); `0x248` = 584. Read from the allocation site, not inferred from a field span | CERTAIN |
| `MemoryType` values are bit flags | `MemoryType2string` @0x3ca040 is `if(a2<=32) switch{1:Unallocated,2:Input,4:Output,8:DRAM,16:SB,32:PSUM}; if(a2==64)RNGSTATE; if(a2==128)REG; else "Unknown memory type"` — case labels 1/2/4/8/16/32, not 0..5 | CERTAIN |
| Aliasing is DRAM-only | `addAlias(AliasPtr)` @0x331680 opens with `if (**(this+216) != 8 \|\| **((ap&~7)+216) != 8) throw runtime_error("… . Aliasing only permitted for DRAM locations.")`, reading both endpoints' `MemoryType` through the +216 indirection and requiring `8` | CERTAIN |
| The `PointerIntPair` encoding | the `AliasPtr` ctor @0x231560 asserts `(ptr & 7)==0` ("Pointer is not sufficiently aligned", PointerIntPair.h) and `(kind & ~7)==0` ("Integer too large for field", IntBits=3), then stores `kind \| (ptr & ~7)`; the asserts name `PointerIntPair<bir::MemoryLocation*, 3, …>` verbatim | CERTAIN |
| The id-cascade cross-link | `getUniqueId<ML,MLS>` @0x32dab0 reads `*(node+0x48)` (owner = MLS) and tail-calls `getUniqueId_Storage(mls+0x118)`; `insertElement` calls `getUniqueId(v64+280)`, putting the `NamedObject` node at `MemoryLocation+0x118`; `getMemoryLocationSet` returns `this+352` = node+0x48 — consistent with the [container model](container-model.md) cascade | CERTAIN |

### Open questions

- The full **`AliasKind` roster** is **[UNRESOLVED]**: only kinds `1` and `3` (`MustAlias`) are observed, and `libBIR` has no `AliasKind2string`, so whether a `2` or `4` exists is untested. The field is structurally a 3-bit mask, so up to three independent kind bits are possible.
- The **pointer-indirected enum sub-block** layout — whether `type`/`kind`/`addr_space`/`pinned` point into one packed struct or into per-field allocations — is unpinned, because the (de)serializers only ever touch `[0]`.
- The **storage-kind discriminator** at struct-`+272` is anchored only for the `==4` (MemoryLocation) case, via `PhysicalAccessPattern::getMemoryLocationSet`; the kind enum for the other `StorageBase` subtypes was not enumerated.
- The `+256` `dims`/`memLocSetId` field's semantic (element count vs secondary id) needs a producer-side site to disambiguate.

Everything in the layout table, the alias model, the enum values, and the 584-byte size is read at the byte level.

---

## Related Components

| Name | Relationship |
|---|---|
| `MemoryLocationSet` | the logical-tensor owner; holds `tensor_class`/`addr_space`/dtype/shape and owns the MemoryLocation list + symtab |
| `bir::StorageBase` | the use-def root all three storage classes share; supplies the live-range edges |
| `bir::Storage` / `Register` | the pre-placement SSA value and the register sibling, the other `Storage` subtypes |
| `AliasPtr` | the 8-byte `PointerIntPair` edge in the alias vector |
| `bir::Hwm` | the per-`(src,dst)`-memory cost model `getLatency` dispatches into |

## Cross-References

- [NamedObject and the Container Model](container-model.md) — the CRTP/id-cascade machinery; `MemoryLocation`'s `NamedObject` node @+0x118 and the `Module+0x98` counter
- [Penguin Tensor and Buffer Nodes](../penguin/tensor-buffer-node.md) — the higher-level (5.2) tensor abstraction that lowers into MemoryLocationSets; full `TensorClass`/`TensorKind` rosters
- [SBUF / PSUM Geometry](../arch/sbuf-psum-geometry.md) — how the `MemoryType` SB/PSUM split maps onto the `ADDR4` byte-address region class and the PSUM 4 MiB window
- [Instruction Base](instruction-base.md) — the sibling structural class whose operands reference these MemoryLocations through `AccessPattern`/`StorageBase*`
