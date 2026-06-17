# Penguin Tensor / Buffer Node & Memory-Space Placement

> *All symbols and offsets on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310; cp311/cp312 are byte-identical Cython rebuilds). The abstract tensor lives in `neuronxcc/starfish/penguin/ir/Tensor.cpython-310…so`; the physical placement lives in `neuronxcc/starfish/penguin/targets/tonga/TongaTensor.cpython-310…so`. Both ship with debug info and are **not** stripped, so every method name below is a verbatim `__pyx_pw_…`/`__pyx_k_…` symbol. Treat every offset as version-pinned.*

## Abstract

A Penguin tensor is an SSA value with a shape, a dtype, and — eventually — a place to live. The middle-end splits that into **two strata** that a reimplementer must keep distinct. The *abstract* tensor (`ir/Tensor.py`, a `ComputeValue` subclass with 119 methods) carries shape, dtype, name, source-vs-device layout, and an *IO role* — but **no physical address**. The *target-bound* tensor (`targets/tonga/TongaTensor.py`) is the same SSA value re-classed into one of a small set of physical subtypes, each of which names a hardware memory and carries a `BufferAllocation` describing where in that memory the data sits. Placement in Penguin is therefore **not a flag on a tensor** — it is a *change of class*: the tiler (`InferTongaTensor`, [Part 5 layout passes]) lowers an `ir/Tensor` into a `NeuronSBTensor`, `NeuronPSUMTensor`, or a `DRAM{2D,3D}BlockTensor`, and that choice of subclass *is* the memory-space declaration.

This mirrors LLVM only loosely. In LLVM a pointer carries an `addrspace(n)` integer and the value type is orthogonal; here the address space is reified as the Python type of the node, and the byte address is a *quasi-affine expression* over the loop block axes rather than a constant. The two on-chip memories the subclasses target — **SBUF** (the 128-partition state buffer) and **PSUM** (the small array of fixed banks the PE array drains into) — are physically different address spaces with different allocation tuples, and the `BufferAllocation` serialize format `[{bnk_idx}, {block_addr}]…` exposes that bank/partition structure directly. The abstract tensor's only nod to placement is the `NonLocal` bit in its `TensorType` kind-flags, which marks a value the allocator is allowed to spill off-chip to DRAM.

This page recovers: the abstract `Tensor` serialization and field model; the `TensorType` kind bit-flags and the composite predicate masks built over them; the `TongaTensor` subclasses (`DRAM{2D,3D}BlockTensor`, `NeuronLocalTensor`, `NeuronSBTensor`, `NeuronPSUMTensor`, `NeuronBlockTensor`) and which memory each names; and the `BufferAllocation` family that holds the physical bank/partition address. It is a sibling of [5.1 — the Penguin node model](ir-node-model.md), which covers the SSA `Value`/`Instruction`/`Operator` base that `Tensor` descends from.

For reimplementation, the contract is:

- The abstract `Tensor` serialize format and its five core fields (`attr`, `dtype`, `shape`, `name`, `init`), plus the `TensorType` kind enum and the seven composite flag-masks that gate every IO query.
- The two-strata model: abstract `ir/Tensor` has no address; physical placement is realized by **re-classing** into a `TongaTensor` subclass — `NeuronSBTensor` (SBUF), `NeuronPSUMTensor` (PSUM), `DRAM{2D,3D}BlockTensor` (off-chip), with `NeuronLocalTensor` the common on-chip base and `NeuronBlockTensor` the partition/block-tiled form.
- The `BufferAllocation` physical-address struct: its `(bank_id, block_addr)` fields, its `[{bnk_idx}, {block_addr}]{alloc_bnk_shape}{alloc_blk_shape}` serialization, and the PSUM 3-tuple vs SBUF 2-tuple distinction.
- The address-as-quasi-affine-expression model: the bank id and byte offset are functions of the block index, not constants.

| | |
|---|---|
| **Abstract tensor** | `ir/Tensor` — `Tensor` (119 methods), `ComputeValue` subclass; string table @ `0x8e1a` |
| **Abstract serialize** | `"{attr}{dtype} {shape} {name}{init}"` (verbatim, ir/Tensor) |
| **Kind enum** | `TensorType` — `{Input, Output, Const, InternalIO, NonLocal}` + 7 composite masks; membership via `TensorType.contains` |
| **Address-space tag** | `VNCAddrSpace` (interned in ir/Tensor) |
| **Physical tensor** | `tonga/TongaTensor` — `DRAM{2D,3D}BlockTensor` / `NeuronLocalTensor` / `NeuronSBTensor` / `NeuronPSUMTensor` / `NeuronBlockTensor`; string table @ `0x9193`, module-exec @ `0x168d2` |
| **Buffer alloc** | `BufferAllocation` / `ArbitraryBufferAllocation` / `SymbolicBufferAllocation` |
| **Alloc serialize** | `"[{bnk_idx}, {block_addr}]{alloc_bnk_shape}{alloc_blk_shape}"` (verbatim @ `0xfd32`) |
| **PSUM / SBUF tuple** | PSUM = `(bank_id, start_partition, base_addr)` · SBUF = `(start_partition, base_addr)` |
| **Lowering pass** | `InferTongaTensor` (`penguin/targets/tonga/passes`) — abstract → physical |

---

## The abstract tensor — `ir/Tensor`

### Purpose

`Tensor` is the tensor-valued SSA def: a `ComputeValue` (and through it a `Value`/`User`, [5.1](ir-node-model.md)) that names a multi-dimensional buffer the program reads and writes. It is the operand and result type of every `Operator` and ISA `Instruction`. At this level the tensor is *abstract*: it knows its shape, dtype, name, and the layout it wants, but it has no bank, no partition, and no byte address. Everything physical is added later by re-classing into a `TongaTensor` subtype (§ *The target-bound tensors*).

### Serialization and core fields

The textual form is the cleanest window onto the node's identity. The serialize template is interned verbatim:

```text
"{attr}{dtype} {shape} {name}{init}"
```

So an abstract tensor is exactly five textual fields: an **attribute prefix** (`attr` — the IO/placement annotations, e.g. an `Input`/`Const` marker), a **dtype**, a **shape**, a **name** (the SSA name string, which is also the dep-edge hash key on the BIR side), and an optional **init**ializer (constant data). The `{name}` and `{init}` template slots are interned only inside that format string, not as standalone field strings; the backing storage fields that *are* independently interned are: `shape` (@ `0x9cfc8`, with `orig_shape` / `new_shape` / `source_shape` / `access_shape` variants), `dtype` (@ `0x9cff2`, + `dtype_size_in_bytes`), `layout` (@ `0x9cf33`, + `src_layout` / `dst_layout` / `deviceLayout` / `deviceShape`), and `addrs` (@ `0x9d00b`).

> **NOTE —** `addrs` (@ `0x9d00b`) exists on the *abstract* tensor, but it holds address **expressions** (quasi-affine `AffineExpr` over loop axes — [5.x affine algebra]), not a resolved bank/byte. The resolved physical address is only present after a `BufferAllocation` is attached to a `TongaTensor` subclass. Do not read `addrs` as "this tensor is placed."

The layout model is two-faced: a `src_layout`/`source_shape` (the layout the HLO producer emitted) and a `deviceLayout`/`deviceShape` (the on-device layout the backend wants), bridged by a transpose view (`transposeView` / `sourceTranspose` / `applyStaticTranspose`). This is why a single tensor can be "logically NCHW, physically partition-major" without a copy: the view carries the permutation. (Full layout-solver treatment is out of scope here; see [Part 5 layout].)

### The `TensorType` kind bit-flags

A tensor's **IO role** is a `TensorType` value — a bit-flag set, *not* a scalar enum. The five primitive members are interned verbatim in the ir/Tensor pool:

| Bit | Member | Meaning |
|---|---|---|
| 0 | `Input` | A graph input parameter (read-only, externally supplied). |
| 1 | `Output` | A graph output (written, externally observed). |
| 2 | `Const` | A compile-time constant / weight (has an `init` payload). |
| 3 | `InternalIO` | An internal cross-function / cross-subgraph value (an induced IO, not a user-facing one). |
| 4 | `NonLocal` | **The spill marker:** this value may live off-chip (DRAM). Set by `set_non_local` / `set_optionally_non_local`. |

> **QUIRK — `NonLocal` is a placement *permission*, not a placement.** It does not say "this tensor is in DRAM"; it says "the allocator is *allowed* to put it there." A tensor can be `Output | NonLocal` (a graph output the allocator spilled) or a purely on-chip `Output`. The actual memory is decided by which `TongaTensor` subclass the tiler picks (§ next), not by this bit. `set_optionally_non_local` / `unset_optionally_non_local` are the tiler's hooks to toggle the *option* without committing.

Each member occupies one bit position; membership is tested by `TensorType.contains` (interned verbatim as `TensorType.contains` @ `0x9b8f0`, and fully-qualified as `neuronxcc.starfish.penguin.ir.Tensor.TensorType.contains` @ `0x944b0`) — i.e. a bit-mask AND. To avoid recomputing common masks, the pool also interns **seven composite flag-sets**, all confirmed verbatim:

```text
InputOrConst                       = Input | Const
InputOrNonLocal                    = Input | NonLocal
InputOrOutput                      = Input | Output
InputOrOutputOrConst               = Input | Output | Const
InputOrConstOrNonlocal             = Input | Const | NonLocal
InputOrOutputOrNonLocal            = Input | Output | NonLocal
InputOrOutputOrConstOrNonLocal     = Input | Output | Const | NonLocal
```

Each composite is the precomputed mask behind a predicate method. The query API on `Tensor` is interned verbatim:

| Method | Mask tested |
|---|---|
| `is_const` | `Const` |
| `isInput` | `Input` |
| `isOutput` | `Output` |
| `isInputOrConst` | `InputOrConst` |
| `isInputOrOutput` | `InputOrOutput` |
| `isInputAndOutput` | `Input & Output` (both bits) |
| `isInputOrConstOrNonlocal` | `InputOrConstOrNonlocal` |
| `isParameter` | `Input | Const` (a weight/parameter) |
| `is_internal_io` | `InternalIO` |

> **GOTCHA — `isInputAndOutput` is conjunction; the rest are disjunction.** Every `…Or…` method is a "does the flag set intersect this mask" test. `isInputAndOutput` is the only one that asks "are *both* bits set" — an aliased in-place IO tensor (`donated` parameter). A reimplementer who codes them all as `(kind & mask) != 0` will mis-classify donated buffers.

### The address-space tag — `VNCAddrSpace`

Alongside the kind-flags the tensor carries a `VNCAddrSpace` value (interned in ir/Tensor) — the *address-space* tag (the "VNC" = virtual Neuron core address space). This is the coarse space label (on-chip vs DRAM vs the collective/replica address window) that travels with the tensor independent of the fine-grained `BufferAllocation`. It is the nearest analog to an LLVM `addrspace(n)`: a small enum the lowering threads down to BIR, distinct from the per-subclass physical class.

### Splat / padded specializations

Two abstract specializations matter for placement:

- **`SingleValueTensor`** (7 methods: `value`, `splat_value`, `is_const`, `convertToTensor`, `_reshape_value`, `serialize`, `verify_value`) — a broadcast-scalar / splat: one value over a shape. It needs no per-element storage; the lowering can materialize it as a constant-splat rather than a real buffer. The related `BroadcastScalar` (ir umbrella) is the broadcast-of-scalar value.
- **`PaddedTensor`** / **`ZeroPaddedTensor`** (`ir/PaddedTensor.py`) — a tensor with implicit edge padding (`down_paddings` / up paddings), the pad-op result view. The padding is logical; the underlying buffer is still placed by the normal mechanism.

---

## The target-bound tensors — `tonga/TongaTensor`

### Purpose

This module realizes the **physical memory model**. After tiling, each abstract `ir/Tensor` is lowered to one `TongaTensor` subclass, and *that choice declares the memory space*. There is no `memory_space=` field; the type *is* the field. The module string table (@ `0x9193`) and module-exec body (@ `0x168d2`) intern all five subclass names verbatim, with their docstrings and method rosters.

### The placement taxonomy

```text
ComputeValue (ir/Tensor)                 — abstract, no address
   └─ NeuronTensor                       — "Tonga specific tensor type"   (docstring confirmed)
        ├─ DRAM2DBlockTensor             — off-chip (HBM/DRAM) buffer, 2-D block
        ├─ DRAM3DBlockTensor             — off-chip (HBM/DRAM) buffer, 3-D block
        └─ NeuronLocalTensor             — "Tonga specific tensor type on
             │                              statebuffer or psumbuffer"     (on-chip base)
             ├─ NeuronSBTensor           — SBUF  (state buffer)   engine tag "TongaSB"
             ├─ NeuronPSUMTensor         — PSUM  (matmul accumulator) tag "TongaPSum"
             └─ NeuronBlockTensor        — partition/block-tiled on-chip tensor
        └─ NeuronWeightTensor            — PE-array weight stream (set_split/split_axis)
             └─ IdentityWeightTensor     — the 128×128 identity (matmul-transpose)
```

> **CORRECTION (PEN-TB-1) —** there is no standalone `DRAM` class. An earlier reading named the off-chip type `DRAM`; the binary interns only `DRAM2DBlockTensor` (@ `0xc7550`) and `DRAM3DBlockTensor` (@ `0xc7530`), with partition-diag strings `"DRAM2DBlk partitions[%s]"` / `"DRAM3DBlk partitions[%s]"`. The off-chip space is realized by these two block-shaped DRAM tensor classes, not a single `DRAM`.

The memory-space mapping is the central fact a reimplementer needs:

| Subclass | Memory space | What distinguishes it | Confidence |
|---|---|---|---|
| `DRAM2DBlockTensor` / `DRAM3DBlockTensor` | Off-chip HBM/DRAM | The off-chip block-shaped types; what `NonLocal`-spilled values become. | CONFIRMED (class names @ `0xc7550`/`0xc7530`) |
| `NeuronLocalTensor` | On-chip (SBUF **or** PSUM) | Common base; docstring (verbatim @ `0xc4ca0`) `"NeuronLocalTensor - Tonga specific tensor type on statebuffer or psumbuffer. For a NeuronLocalTensor, the dims that are higher than partition_dim are mapped to time, while the dims lower or equal to the partition_dim are mapped to space."` Holds the allocation API. | CONFIRMED (24 methods incl. `createAllocation`, `has_physical_address`) |
| `NeuronSBTensor` | **SBUF** | State-buffer tensor; 2-D (partition × byte). Engine tag `TongaSB`. | CONFIRMED (4 methods: `__init__`, `attr_str`, `dim_size`, `serialize`) |
| `NeuronPSUMTensor` | **PSUM** | Matmul-accumulator tensor; lives in fixed 2 KiB banks. Engine tag `TongaPSum`. | CONFIRMED (2 methods: `init_state`, `attr_str`) |
| `NeuronBlockTensor` | On-chip, **partition/block-tiled** | The tiled form: explicit `npartitions`/`nblocks`, `partition_size`, `block_shape`, `tonga_shape`. | CONFIRMED (33 methods) |
| `NeuronWeightTensor` / `IdentityWeightTensor` | SBUF → PE array | Weight stream into the systolic array; `set_split` / `split_axis` / `split_indices`. Identity = the 128×128 transpose helper. | CONFIRMED |

> **QUIRK — SBUF and PSUM are different address spaces, hence different classes.** A reimplementer coming from a flat-memory IR will want one `OnChipTensor` with a `space` field. The binary refuses: `NeuronSBTensor` and `NeuronPSUMTensor` are *siblings*, because their allocation tuples differ in arity (SBUF is `(start_partition, base_addr)`; PSUM is `(bank_id, start_partition, base_addr)` — the PSUM bank is a third coordinate SBUF does not have). The common behavior they *do* share (allocation lifecycle, `has_physical_address`, address linking) lives one level up in `NeuronLocalTensor`.

> **NOTE — the partition dim splits a `NeuronLocalTensor`'s dims into *time* and *space*.** The `NeuronLocalTensor` docstring (@ `0xc4ca0`) states it verbatim: dims **higher than** `partition_dim` map to **time** (the schedule/loop order), and dims **lower than or equal to** `partition_dim` map to **space** (the physical 128-partition × byte layout). So the partition axis is the boundary between "iterated over in the loop nest" and "laid out across the hardware" — a reimplementer must place exactly the dims at/below `partition_dim` into the on-chip address, and leave the higher dims as loop iterations.

### `NeuronLocalTensor` — the on-chip allocation API

`NeuronLocalTensor` (24 methods) is where on-chip placement actually happens. Its allocation lifecycle (all interned verbatim):

```c
// NeuronLocalTensor — on-chip (SBUF|PSUM) allocation lifecycle
createAllocation(...)           // bind a concrete BufferAllocation
createSymbolicAllocation(...)   // bind a SymbolicBufferAllocation (address still a free var)
resetAllocation() / cleanAllocation() / cleanAllocationModule()
has_physical_address()          // true once a non-symbolic allocation is bound
_link_address() / _unlink_address() / cleanAddress()   // wire addr into the def-use graph
addr_str()                      // textual address
linearizePartitionAddrs(...)    // flatten the 2-D (partition × free) addr to 1-D
// size queries answered from the bound allocation:
alloc_size_in_bytes / alloc_bank_size_in_bytes / alloc_block_size_in_bytes
num_alloc_banks / num_alloc_blocks / num_alloc_element_per_bank
```

> **GOTCHA — a tensor can be *placed in space* but *not yet addressed*.** `createSymbolicAllocation` binds a `SymbolicBufferAllocation` whose bank/byte are free variables; `has_physical_address` returns false until a concrete `BufferAllocation` replaces it. The two-phase split (choose the *space* by re-classing, then resolve the *address* later) is deliberate: the layout solver fixes the class early and the bank/byte allocator (Part 8) resolves the symbol late. Treat "has a `NeuronSBTensor`" and "has an SBUF address" as independent facts.

### `NeuronBlockTensor` — the partition/block-tiled form

After tiling, an on-chip tensor is usually a `NeuronBlockTensor`: the 128-partition axis and the block-tiling outer axis are made explicit. Confirmed methods (33 total) split into three groups:

```c
// geometry — the explicit partition × block shape
npartitions / nblocks
partition_size / partition_size_in_bytes / total_partition_size_in_bytes
partition_start / partition_ap / partitionDimIndices
block_shape / tile_size
tonga_rank / tonga_shape                 // the device-side (partition,block,free) rank/shape
calculate_partition_size(...)

// dim motion — move a logical dim onto the partition / block / free axis
moveToBlkDim / moveToFreeDim / moveToOuterFreeDim
expandPartitionDim / canDelinearizeParitionDim   // [sic — "Parition" is the real symbol]
linearizePartitionAddrs

// sharding (LNC)
access_by_lnc_comm / used_by_shard_reduce
```

The `tonga_shape` is the device view: `(partition, [block…], [free…])`, where the partition dim is the 128-wide SBUF/PE axis. `moveToBlkDim` / `moveToFreeDim` are how the layout solver re-assigns which logical dimension is the partition, the block, or a free dim — the P/F/B classification consumed downstream.

> **NOTE — the misspelling `canDelinearizeParitionDim` is the real interned symbol** (`Parition`, missing a `t`). Cross-check against the binary, not against a corrected spelling — a grep for `Partition` will miss it.

### Layout-requirement hints

`NeuronLayoutDimHint` (35 methods) / `NeuronShardDimHint` are *per-dimension layout requirements* the layout solver reads to decide P/F/B. Each dim can demand `must_be_partition`, `must_be_free`, or `must_be_block` (all interned verbatim), plus per-op hints (`matmulLoadHint`, `transposeLoadHint`/`StoreHint`, `genericLoadHint`/`StoreHint`, …). These are inputs to placement, not placement itself.

---

## The physical address — `BufferAllocation`

### Purpose

A `BufferAllocation` is the struct a `NeuronLocalTensor` holds once placed: it answers *which bank, which partition, which byte, how big*. Three variants are interned verbatim:

| Class | Role | Confidence |
|---|---|---|
| `BufferAllocation` | Base — docstring `"BufferAllocation -- Represent the alloc…"`. The resolved physical address. | CONFIRMED |
| `ArbitraryBufferAllocation` | A general (possibly non-rectangular) allocation; full method roster mirrors the base. | CONFIRMED (15 methods) |
| `SymbolicBufferAllocation` | Address is still a free variable (pre-resolution); 3 methods. | CONFIRMED |

### Fields and serialization

The base `BufferAllocation` interns these field/accessor names verbatim:

```c
struct BufferAllocation {            // fields are Python __dict__ entries, names CONFIRMED
    bank_id;                         // which PSUM bank (PSUM only)
    block_addr;                      // byte address within the bank/partition
    // size accessors (computed):
    alloc_size_in_bytes;
    alloc_bank_size_in_bytes;
    num_alloc_banks;
    num_alloc_blocks;
    allocated_bank_upper_bound;      // highest bank index this alloc touches
    allocated_byte_addr_upper_bound; // highest byte address this alloc touches
};
// methods: __init__, __str__/serialize, expandPartition,
//          enumerate_indices, is_static_allocation
```

The serialize template is interned verbatim (@ `0xfd32`):

```text
"[{bnk_idx}, {block_addr}]{alloc_bnk_shape}{alloc_blk_shape}"
```

So a serialized allocation reads as `[bank, byte_addr]` followed by the **bank shape** and **block shape** — i.e. the coordinate is a `(bank, byte)` pair and the two trailing shapes give how many banks and blocks the allocation spans. The diagnostic string `"bank_id on "` and `"NeuronPSUMTensor must be initialized to…"` confirm that PSUM placement validates a bound bank.

### PSUM vs SBUF allocation tuples

The arity of the allocation differs by target memory. The address-map docstring (@ `0xc3700`) states both forms verbatim: `"For PSUM, it's a three-element tuple (bank_id, start_partition, base_addr)"` and `"For SBUF, it's a two-element tuple (start_partition, base_addr)"`.

| Memory | Allocation tuple | Why |
|---|---|---|
| **PSUM** | `(bank_id, start_partition, base_addr)` — 3-tuple | PSUM is an array of fixed banks; the **bank id** is a required third coordinate. |
| **SBUF** | `(start_partition, base_addr)` — 2-tuple | SBUF is a single 2-D (partition × byte) space; no bank dimension. |

> **QUIRK — the bank id and byte offset can be *quasi-affine functions of the block index*, not constants.** The allocation is not necessarily a fixed `(bank, byte)`; for a block-tiled tensor each block can map to a different bank/offset via a quasi-affine expression over the block axes (the same affine algebra the access patterns use). `enumerate_indices` walks those per-block addresses. A reimplementer who models a `BufferAllocation` as a constant base+size will be correct for the simple case and wrong for any tensor whose tiles round-robin across banks.

### Where placement is decided vs resolved

```text
ir/Tensor (abstract, NonLocal-permission only)
   │
   │  InferTongaTensor pass (tonga/passes)   ── chooses the SUBCLASS = the SPACE
   ▼
NeuronSBTensor / NeuronPSUMTensor / NeuronBlockTensor / DRAM
   │
   │  createSymbolicAllocation                ── space fixed, address still a free var
   ▼
SymbolicBufferAllocation   ── has_physical_address() == False
   │
   │  bank/byte allocators (Part 8)           ── resolve the symbol to a concrete bank+byte
   ▼
BufferAllocation           ── has_physical_address() == True
```

The **space** is declared by the layout/`InferTongaTensor` stage (the subclass choice); the **address** is resolved later by the backend allocators. The Penguin tensor node is the carrier of both decisions but commits them at different times.

---

## Adversarial self-verification

The five strongest claims on this page, re-challenged against the binary:

1. **Abstract serialize `"{attr}{dtype} {shape} {name}{init}"`** — re-grepped the ir/Tensor string table (`0x8e1a`); the literal is present verbatim. **CONFIRMED.**
2. **`TensorType` = `{Input,Output,Const,InternalIO,NonLocal}` + 7 composite masks + membership predicate** — all five primitives, all seven `…Or…` composites, and `TensorType.contains` (@ `0x9b8f0`) are interned verbatim in ir/Tensor. **CONFIRMED.** *Correction:* I originally claimed a `kind_shift` packer; re-grepping both modules returns **no** `kind_shift` string. That claim is **withdrawn** — the bit-packing exists (the composite masks prove it) but no symbol named `kind_shift` backs it, so this page does not name one.
3. **`BufferAllocation` serialize `[{bnk_idx}, {block_addr}]{alloc_bnk_shape}{alloc_blk_shape}`** — the format string is at `0xc5c40` (ida) / `0xfd32` (disasm table) in TongaTensor.so; both `alloc_bnk_shape` and `alloc_blk_shape` tokens are independently interned. **CONFIRMED.**
4. **The subclasses and their docstrings** — `NeuronLocalTensor`, `NeuronSBTensor`, `NeuronPSUMTensor`, `NeuronBlockTensor`, `NeuronWeightTensor`, `IdentityWeightTensor` all interned; the full `NeuronLocalTensor` docstring (@ `0xc4ca0`) — including the time/space dim-mapping — is binary-direct. *Correction:* there is **no** standalone `DRAM` class; the off-chip types are `DRAM2DBlockTensor` (@ `0xc7550`) and `DRAM3DBlockTensor` (@ `0xc7530`) — see CORRECTION (PEN-TB-1). **CONFIRMED** (corrected).
5. **PSUM 3-tuple vs SBUF 2-tuple** — upgraded to **CONFIRMED**: the address-map docstring (@ `0xc3700`) states both tuples verbatim (`"…three-element tuple (bank_id, start_partition, base_addr)"` / `"…two-element tuple (start_partition, base_addr)"`). The `bank_id`/`block_addr`/`start_partition` fields and the `"NeuronPSUMTensor must be initialized to…"` diagnostic are independently interned.

No field meaning here was invented to fill a gap: the `__dict__`-resident fields are name-CONFIRMED with owner-class INFERRED only where a method roster does not pin them. The three NOT-FOUND items the verification swept up — standalone `DRAM`, standalone `name`/`addr`/`init` field strings (they appear only inside the serialize template), and `kind_shift` — are explicitly *not* claimed as interned symbols on this page.

---

## Related Components

| Name | Relationship |
|---|---|
| `ir/Tensor.Tensor` | The abstract tensor node; base of every physical subclass. |
| `tonga/TongaTensor.*` | The physical placement subclasses documented here. |
| `BufferAllocation` family | The resolved/symbolic physical address held by a placed tensor. |
| `InferTongaTensor` pass | Lowers abstract → physical (chooses the subclass = the space). |
| `NeuronLayoutDimHint` | Per-dim P/F/B layout requirements the solver reads before placement. |

## Cross-References

- [Penguin Node Model](ir-node-model.md) — 5.1; the `Value`/`ComputeValue`/`Instruction`/`Operator` SSA base that `Tensor` descends from, and the def-use graph the loads/stores ride on.
- [SBUF / PSUM Bank Geometry](../arch/sbuf-psum-geometry.md) — 1.05; the physical partition count, per-partition byte budget, and 2 KiB PSUM bank size the `BufferAllocation` addresses index into.
- [DRAM / HBM Geometry & the DRAM Split](../arch/dram-hbm-geometry.md) — 1.06; the off-chip window a `DRAM` tensor / `NonLocal`-spilled value lands in.
- [Part 8 — The libwalrus Backend](../walrus/) — the bank/byte allocators that resolve a `SymbolicBufferAllocation` into a concrete `BufferAllocation`.
- [Part 7 — BIR & the Simulator](../bir/) — the BIR `MemoryLocation` the lowered tensor address becomes once Penguin descends to BIR.
