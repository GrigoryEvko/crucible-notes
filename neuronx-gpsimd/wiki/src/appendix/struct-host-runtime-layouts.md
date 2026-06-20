# Host-Runtime Struct Layouts (field-exact)

> **Scope — the host-runtime struct atlas, one table per type.** This is the
> field/offset/size/type reference appendix for the structs a GPSIMD custom-op
> reimplementer touches when driving the AWS Neuron host runtime. It collates,
> byte-exact, the high-interest host objects of `libnrt.so` (the 122 MB host
> x86-64 runtime) and the codec-side `libnrtucode_internal.so`: the on-disk
> **`kbin_mem_ref`** (152 B union), the **metaneff** `MetaTensor` /
> `MetaNeff` / `ModelConfig` host message layouts, the per-NeuronCore
> **`aws_hal_stpb`** (1104 B) and its **`dma_queue_info_t`** (360 B)
> instruction-fetch ring, the **`aws_hal_ens` / `aws_hal_ens_nq`** notification
> mirror, the **`dma_queue_bundle`** model and its 16-queue instance
> (**idx 16 = the custom-op queue**), the **OPCODE** instruction-variant table,
> the **`ngc` / gconf** global, the 16-way per-device top-SP / context arrays,
> the **`kmd_context`** driver portal, and the **`ntff::`** trace oneofs /
> act-table family. Each named struct gets a field table (offset hex · size ·
> type · field · meaning), a reconstructed C declaration naming the real symbol,
> a confirmed total size, and a cross-check.
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read from a
> shipped ELF's DWARF / `nm` / `objdump` / `c++filt` / `readelf`, or its IDA
> `*_structures.json` / `*_enums.json` sidecar), `INFERRED` (an ABI/layout rule
> applied to an observed fact), `CARRIED` (taken byte-exact from a cited sibling
> page whose source artifact is binary-derived, re-grounded here). Callouts use
> literal `QUIRK` / `GOTCHA` / `NOTE` / `CORRECTION`.

> **NOTE — provenance & the two ELFs.** Every layout below is derived **solely
> from static analysis** of two shipped, redistributable artifacts and the
> committed sibling pages that decode them:
>
> 1. **`libnrt.so.2.31.24.0`** — ELF64 x86-64, **122 956 336 B**,
>    `BuildID[sha1]=8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, SONAME
>    `libnrt.so.1`, *not stripped*, **with DWARF `.debug_info`** (17 372
>    functions). Section layout (`readelf -SW`): `.text` VMA `0x3dbc0` ==
>    fileoff, `.rodata` VMA `0x7cf000` == fileoff, `.data` VMA `0xc07e00` ==
>    fileoff. **There is no `.data` delta for this host x86 binary** — the libtpu
>    `0x400000` and ncore2gp `0x200000` deltas do **not** apply; every `.bss` /
>    `.data` global address quoted here is offset-clean. The struct facts are
>    read from its DWARF and its IDA `*_structures.json` / `*_enums.json`.
> 2. **`libnrtucode_internal.so`** — ELF64 x86-64, **10 276 288 B**,
>    `BuildID 9cbf78c6…`, *not stripped*. Section VA−fileoff deltas are
>    `.rodata Δ=0`, `.text Δ=0x1000`, `.data.rel.ro Δ=0x2000`, `.data Δ=0x3000`
>    — a raw `xxd` of a `.data`-resident table must subtract its section Δ.
>
> Embedded `__FILE__` / assert / descriptor literals (`/opt/workspace/…`,
> `/opt/brazil-pkg-cache/…/KaenaHal-2.31.0.0/…`, the proto descriptor strings)
> are compiler-baked into the ELF and are **binary-derived and citeable**. The
> protobuf message layouts are recovered from the native `libtorchneuron.so`
> consumer (the host I/O ABI page); they are not lifted from any `.proto`.

This appendix is the field-level companion to the prose pages that *use* these
structs. Each section cross-links the page that recovers it:
[the libnrt surface map](../runtime/libnrt-surface.md),
[multi-model / context tree + dmem](../runtime/multimodel-context-dmem.md),
[the DGE host-private API](../runtime/dge-host-api.md),
[the nrtucode object-model graph](../runtime/object-model-graph.md),
[metaneff I/O ABI](../neff/metaneff-io-abi.md),
[relocation / weights](../neff/relocation-weights.md),
[descriptor + ring field tables](../dma/descriptor-ring-field-tables.md),
[the al_udma HW engine](../dma/udma-hw-engine.md),
[the NOTIFIC queue CSR](../control/csr/notific-queue.md), and the
[struct census overview](./struct-census-overview.md).

> **CORRECTION — what this appendix can and cannot field-resolve.** The SCOPE
> names several `libnrt.so` structs whose **field interiors** are visible only in
> that ELF's `*_structures.json` DWARF sidecar. Where the sibling pages already
> reproduce a full layout (`kbin_mem_ref`, `MetaTensor`/`MetaNeff`/`ModelConfig`,
> the `tdrv_ctx → mla → tpb → model` tree, `dmem_t`, the nrtucode objects), this
> page reproduces it **field-exact** and re-verifies the arithmetic. Where only
> the **type and total size** are observable — `aws_hal_stpb` (1104 B),
> `dma_queue_info_t` (360 B), the `aws_hal_ens`/`ens_nq` mirror, the
> `dma_queue_bundle` instance, the `ngc` global, `kmd_context` — the interior is
> **reconstructed (INFERRED)** from adjacent observed facts (the HW ring bank, the
> NOTIFIC record, the coretype mask, the IOCTL portal) and flagged as such.
> v5/Maverick interiors are uniformly INFERRED (no `maverick` source dir / no
> dispatch suffix in either ELF).

---

## 0. The struct atlas at a glance

The high-interest host structs, with their confirmed total size and the section
that recovers them. The "size" column is the **DWARF byte-size** (libnrt) or the
**`malloc`/`new` prologue immediate** (nrtucode), each cross-checked below.

| struct | total | ELF / source | role | confidence |
|---|---|---|---|---|
| `kbin_mem_ref` | **152** (`0x98`) | libnrt `structures.json` | on-disk NEFF var → device mem ref (union arm at +0x88) | HIGH/OBSERVED |
| `mem_ref` (runtime C++) | **136** (`0x88`) | libnrt `parse_one_variable` | in-RAM mem_ref object (the staged form) | HIGH/OBSERVED |
| `metaneff::MetaTensor` | **≥0x60** | `libtorchneuron.so` | one host tensor at the I/O boundary | HIGH/OBSERVED |
| `metaneff::MetaNeff` | **≥0xE8** | `libtorchneuron.so` | the top-level host I/O contract | HIGH/OBSERVED |
| `metaneff::ModelConfig` | **≥0x30** | `libtorchneuron.so` | graph-level load/exec policy | HIGH/OBSERVED |
| `aws_hal_stpb` | **1104** | libnrt `tpb_t` union | per-NC 5-engine sequencer HAL state | HIGH size / INFERRED interior |
| `dma_queue_info_t` | **360** | libnrt `tpb_t` union (`[5]`=1800) | per-engine instruction-fetch DMA ring | HIGH size / INFERRED interior |
| `aws_hal_ens` / `_nq` | n/a | libnrt `notification_t` family | the NQ software mirror (ring head/tail/phase) | MED/INFERRED |
| `dma_queue_bundle` (×16) | n/a | libnrt `v2_queue_bundle_alloc_table` | the 16-queue instance; **idx 16 = custom-op** | HIGH (idx) / INFERRED (fields) |
| OPCODE variant table | 256 buckets | `nrtucode_opset_t` + ISA opcode enum | opcode → variant/specialization presence | HIGH/OBSERVED |
| `ngc` / gconf (`@.bss`) | n/a | libnrt `nrt_gconf()` | process-global runtime config | MED/INFERRED |
| `tpb_t` (16-way arrays) | **39 320** | libnrt DWARF | per-NeuronCore context (`top_sps[16]`, Q7[8]) | HIGH/OBSERVED |
| `kmd_context` | n/a | libnrt `ndl_*` portal | the kernel-mode-driver IOCTL portal handle | MED/INFERRED |
| `ntff::*` oneofs | per-msg | libnrt RTTI (protobuf) | trace-file message family (`ACT_TABLE` etc.) | MED/OBSERVED |
| `nrtucode_context_t` | **40** (`0x28`) | nrtucode ctor | codec context | HIGH/OBSERVED |
| `nrtucode_core_t` | **112** (`0x70`) | nrtucode ctor | booted-engine handle | HIGH/OBSERVED |
| `nrtucode_ll_t` | **72** (`0x48`) | nrtucode ctor | loadable-library (DKL) handle | HIGH/OBSERVED |
| `nrtucode_opset_t` | **2096** (`0x830`) | nrtucode ctor | opcode/specialization set | HIGH/OBSERVED |

---

## 1. `kbin_mem_ref` — the on-disk var → mem-ref (152 B union)

`[HIGH/OBSERVED]` `structures.json` `kbin_mem_ref`, `size = 152` (`0x98`). This is
the **on-disk** device-side descriptor `parse_one_variable` builds for each NEFF
`var{}` entry (the device twin of a `metaneff::MetaTensor`, §3); it carries the
`mr_type`, the tensor geometry, the constant-payload pointer, and a 16-byte
**union** whose arm is selected by `mr_type`.

**Table 1.0 — `kbin_mem_ref` fixed prefix (offset 0..0x88)**

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0x00` | 4 | `kbin_mr_type_t` | `mr_type` | the mem-ref kind (Table 1.1); selects the union arm |
| `+0x04` | 4 | — | *pad* | natural-alignment gap before the 8-byte `name` |
| `+0x08` | 8 | `const char*` | `name` | var name key (`"input{i}"`/`"output{i}"`/state/weight name) |
| `+0x10` | 8 | `size_t` | `size` | logical byte size of the tensor |
| `+0x18` | 8 | `size_t` | `alignment` | required device alignment |
| `+0x20` | 4 | `uint32_t` | `var_id` | dense `0..N-1` index — the shared I/O ordinal (§3) |
| `+0x24` | 16 | `char[16]` | `dtype` | dtype name string (e.g. `"float32"`), NUL-padded |
| `+0x34` | 4 | — | *pad* | gap to 8-align the `shape` array |
| `+0x38` | 64 | `uint64_t[8]` | `shape` | up to 8 dims (matches the device `mem_ref.shape[8]`) |
| `+0x78` | 8 | `kbin_debug_tensor_md_t*` | `dtensor_md` | optional per-var debug metadata (`malloc(0x204)`), else NULL |
| `+0x80` | 8 | `uint8_t*` | `buffer` | constant-payload pointer; **non-NULL only for `MR_BUFFER`** |
| `+0x88` | 16 | `union { … }` | `u` | type-keyed arm (Table 1.2) |

Total `0x88 + 0x10 = 0x98 = 152`. **Cross-check** (`static_assert`-equivalent):
the recovered field offsets sum exactly to the `structures.json` `size = 152`,
with two natural-alignment pads (`+0x04` before the 8-byte `name`, `+0x34` before
the 8-aligned `shape[8]`). `[OBSERVED]`

```c
// kbin_mem_ref — structures.json `kbin_mem_ref`, size = 152 (0x98).  [HIGH/OBSERVED]
// Built by parse_one_variable @0x4b36b0; the on-disk var-table entry.
struct kbin_mem_ref {                              /* size 0x98 */
  /* +0x00 */ kbin_mr_type_t            mr_type;   // selects `u` arm (Table 1.1)
  /* +0x04 */ /* pad[4] */
  /* +0x08 */ const char               *name;      // var key
  /* +0x10 */ size_t                    size;
  /* +0x18 */ size_t                    alignment;
  /* +0x20 */ uint32_t                  var_id;    // dense 0..N-1 ordinal
  /* +0x24 */ char                      dtype[16]; // dtype-name string
  /* +0x34 */ /* pad[4] */
  /* +0x38 */ uint64_t                  shape[8];  // up to 8 dims
  /* +0x78 */ kbin_debug_tensor_md_t   *dtensor_md;// NULL unless debug md present
  /* +0x80 */ uint8_t                  *buffer;    // const bytes for MR_BUFFER; else NULL
  /* +0x88 */ union {                              // 16-byte type-keyed union (Table 1.2)
      struct { uint32_t var_id;  uint32_t io_ref;        } io;      // MR_INPUT/MR_OUTPUT
      struct { uint64_t offset;  uint32_t backing_var_id;} backing; // staged/backed buffers
      struct { void    *elems;   uint64_t count;         } list;    // MR_LIST / MR_PTR_TABLE
      struct { uint32_t remote_core; uint32_t remote_var;} remote;  // MR_REMOTE
  } u;
};
```

**Table 1.1 — `kbin_mr_type_t`** (`enums.json`; matches
[relocation/weights §7.3](../neff/relocation-weights.md) and
[metaneff §8](../neff/metaneff-io-abi.md) byte-for-byte):

| val | name | `buffer` (+0x80) | union arm (`u`) |
|---|---|---|---|
| 0 | `MR_INVALID` | NULL | — |
| 1 | `MR_SB` | NULL | offset/backing (SBUF AXI aperture) |
| 2 | `MR_BUFFER_STAGED` | NULL | backing |
| 3 | `MR_BUFFER` | **const ptr** | — (HBM-staged) |
| 4 | `MR_TMP_BUF` | NULL | — (scratch) |
| 5 | `MR_INPUT` | NULL | `io` (`var_id`/`io_ref`) |
| 6 | `MR_OUTPUT` | NULL | `io` |
| 7 | `MR_PTR` | NULL | backing (indirect) |
| 8 | `MR_VIRTUAL_TMP_BUF` | NULL | — (CC scratchpad, 4K-aligned) |
| 9 | `MR_LIST` | NULL | `list` (builds a `ptr_table`) |
| 10 | `MR_PTR_TABLE` | NULL | `list` |
| 11 | `MR_REMOTE` | NULL | `remote` (cross-core / collectives) |

**Table 1.2 — the +0x88 union, per arm** `[INFERRED arm split; OBSERVED 16-byte
span]`. The `structures.json` records a 16-byte trailing union; the arm
**interpretations** below are reconstructed from the `mr_type` consumers in
`parse_one_variable` and `mem_ref_copy_and_stage_mr`
([relocation/weights §7–8](../neff/relocation-weights.md)). The 16-byte span and
its `mr_type` keying are `OBSERVED`; the exact field split inside each arm is
`INFERRED` from the consumer disasm.

> **GOTCHA — two distinct `mem_ref` structs; do not conflate.** The **on-disk**
> `kbin_mem_ref` above is 152 B with `buffer` at `+0x80`. The **runtime C++**
> `mem_ref` object (`operator new(0x88)`, **136 B**) that `parse_one_variable`
> stages it into has `buffer` at `+0x58`, `size` at `+0x48`, `alignment` at
> `+0x50`, `dtype_id` at `+0x80`. They share a `mr_type` enum but **not** a
> layout; `mem_ref_copy_and_stage_mr` translates one into the other. The runtime
> object is the one whose `_ZTV mem_ref_sp` (vtable @`0xbf8c88`) carries the
> 3-slot `[0]dump [1,2]dtor` interface
> ([libnrt surface §4.2](../runtime/libnrt-surface.md)); the on-disk form is
> plain data. `[OBSERVED — both in structures.json]`

**Table 1.3 — the runtime C++ `mem_ref` (136 B), for contrast** `[HIGH/OBSERVED]`
(from `parse_one_variable` field stores; the `dump()`-override Rosetta vtable
slot map confirms it is the polymorphic `mem_ref_sp`/`mem_ref_io`/… hierarchy):

| off | size | field | note |
|---|---|---|---|
| `+0x00` | 8 | `_vptr` | one of the 8 `mem_ref_*` vtables (`@0xbf8c88..0xbf8da0`) |
| `+0x48` | 8 | `size` | tensor byte size |
| `+0x50` | 8 | `alignment` | |
| `+0x58` | 8 | `buffer` | constant-payload pointer (zero-copy into the tar for `.npy`) |
| `+0x80` | 4 | `dtype_id` | resolved dtype index |

> **NOTE — vtable slot rule for the runtime `mem_ref`.** The `mem_ref_*` family
> are real C++ polymorphic classes; their vtable pointer is `_ZTV<name> + 0x10`
> (past the offset-to-top + typeinfo header), and slot N is `vptr + 8*N`. The
> 3-slot interface is `[0]dump(kbin_mem_ref&) [1]~dtor (D1) [2]deleting-dtor (D0)`
> ([libnrt surface §4.2](../runtime/libnrt-surface.md)). The `dump()` override is
> the Rosetta that names each subclass.

---

## 2. The on-disk → runtime mem_ref staging path (where the two forms meet)

`[HIGH/OBSERVED — CARRIED from relocation/weights §7–8]` For completeness, the
fixed contract that joins the two §1 forms: the on-disk `kbin_mem_ref.buffer`
(const bytes inside the tar for `.npy`, a private copy for raw `.bin`) is staged
into HBM by `mem_ref_copy_and_stage_mr @0x2fb780`, which resolves each mem-ref to
a `dmem_t` (§5.3) and sets the runtime object's resolved
`physical_address = dmem->_pa + dmem->align_offset`. Only the four staged types
`{MR_SB(1), MR_BUFFER(3), MR_TMP_BUF(4), MR_VIRTUAL_TMP_BUF(8)}` (clear-bit mask
of `-283 = 0xFFFFFFFFFFFFFEE5`) bulk-stage at load; `INPUT/OUTPUT/PTR/LIST/…`
resolve at execute. This makes `kbin_mem_ref.var_id` the device-side index that
lines up with `metaneff::MetaTensor` ordinal *i* (§3).

---

## 3. metaneff host messages — `MetaTensor` / `MetaNeff` / `ModelConfig`

`[HIGH/OBSERVED]` These are the in-RAM libprotobuf `Message` objects the native
`libtorchneuron.so` binds `at::Tensor`s through. Offsets are cross-confirmed
between the `_InternalParse` writers and the `neuron::` readers
([metaneff I/O ABI §2–3](../neff/metaneff-io-abi.md)). Every protobuf `Message`
carries the standard header: `_vptr @+0`, `_internal_metadata @+8`,
`_has_bits_`/`_cached_size_` @+0x10.

### 3.1 `metaneff::MetaTensor` — one tensor at the I/O boundary

`[HIGH/OBSERVED]` RTTI `_ZTSN8metaneff10MetaTensorE @0x4d3d80`.

| off | size | type | field | proto # / meaning |
|---|---|---|---|---|
| `+0x00` | 8 | `void*` | `_vptr` | generated Message vtable |
| `+0x08` | 8 | `uint64_t` | `_internal_metadata` | arena/unknown-fields |
| `+0x10` | 4 | `uint32_t` | `_has_bits_` | bit0=`checkpoint_key`, bit1=`user_input_key` |
| `+0x14` | 4 | `uint32_t` | `_cached_size_` | proto cached byte size |
| `+0x18` | 24 | `RepeatedField<int64>` | `shape` | #2 — `{size@+0x18, total@+0x1c, Arena*, int64* data@+0x20}` |
| `+0x30` | 8 | `ArenaStringPtr` | `name` | #1 — the `"input{i}"`/`"output{i}"` device key |
| `+0x38` | 8 | `ArenaStringPtr` | `content` | #4 — inline const bytes |
| `+0x40` | 8 | `ArenaStringPtr` | `checkpoint_key` | #6 — proto3-opt (state buffers) |
| `+0x48` | 8 | `ArenaStringPtr` | `user_input_key` | #8 — proto3-opt |
| `+0x50` | 4 | `int32_t` | `data_type` | #3 — `MetaTensor.DataType` (Table 3.4) |
| `+0x54` | 1 | `uint8_t` | `allow_dynamic_batch_size` | #5 — dynamic-batch leading-dim override |
| `+0x58` | 4 | `int32_t` | `type` | #7 — `MetaTensor.Type` (==1 USER_INPUT is the hot test) |

The last field (`type`) ends at `0x5C`; the Message object is ≥`0x60` (8-aligned).
**Cross-check:** the parser stores (`mov %eax,0x58(%r12)` for `type`,
`setne 0x54(%r12)` for the bool, `lea 0x30(%r12)` for `name`) pin each offset; the
`RepeatedField<int64> shape` 24-byte span is the standard libprotobuf size. `[OBSERVED]`

```c
// metaneff::MetaTensor — RTTI @0x4d3d80.  [HIGH/OBSERVED]
struct MetaTensor {
  /* +0x00 */ void*                 _vptr;
  /* +0x08 */ uint64_t              _internal_metadata;
  /* +0x10 */ uint32_t              _has_bits_;   // bit0=checkpoint_key, bit1=user_input_key
  /* +0x14 */ uint32_t              _cached_size_;
  /* +0x18 */ RepeatedField<int64>  shape;        // field 2  (count@+0x18, data@+0x20)
  /* +0x30 */ ArenaStringPtr        name;         // field 1
  /* +0x38 */ ArenaStringPtr        content;      // field 4
  /* +0x40 */ ArenaStringPtr        checkpoint_key;  // field 6 (proto3-opt)
  /* +0x48 */ ArenaStringPtr        user_input_key;  // field 8 (proto3-opt)
  /* +0x50 */ int32_t               data_type;    // field 3 -> DataType
  /* +0x54 */ uint8_t               allow_dynamic_batch_size; // field 5
  /* +0x58 */ int32_t               type;         // field 7 -> Type
};
```

### 3.2 `metaneff::MetaNeff` — the top-level host I/O contract

`[HIGH/OBSERVED]` RTTI `_ZTSN8metaneff8MetaNeffE @0x4d4010`.

| off | size | type | field | proto # / meaning |
|---|---|---|---|---|
| `+0x00` | 8 | `void*` | `_vptr` | |
| `+0x08` | 8 | `uint64_t` | `_internal_metadata` | |
| `+0x10` | 24 | `RepeatedPtrField<MetaTensor>` | `input_tensors` | #1 — count `*(int)(+0x18)`, rep `*(+0x20)` |
| `+0x28` | 24 | `RepeatedPtrField<MetaTensor>` | `output_tensors` | #2 — count `*(int)(+0x30)`, rep `*(+0x38)` |
| `+0x40` | 120 | `MapField<int64,int64>` | `output_aliases_to` | #6 — `out_idx → in_idx` donation map |
| `+0xB8` | 8 | `ArenaStringPtr` | `serialized_graph_def` | #4 — the HLO module |
| `+0xC0` | 8 | `ArenaStringPtr` | `name` | #5 |
| `+0xC8` | 8 | `ModelConfig*` | `model_config` | #3 |
| `+0xD0` | 8 | `int64_t` | `num_user_inputs` | #7 |
| `+0xD8` | 8 | `int64_t` | `num_states` | #8 |
| `+0xE0` | 8 | `int64_t` | `num_weights` | #9 |

`num_weights` ends at `0xE8`; the object is ≥`0xE8`. **Cross-check:** the
`MapField<int64,int64>` at `+0x40` occupies the `0x40..0xB8` span (`0x78` = the
standard libprotobuf MapField in-RAM size: control struct + bucket table); the
i64 fields land at `mov %rax,0xd0/0xd8/0xe0(%r12)`. `[OBSERVED]`

```c
// metaneff::MetaNeff — RTTI @0x4d4010.  [HIGH/OBSERVED]
struct MetaNeff {
  /* +0x00 */ void*                          _vptr;
  /* +0x08 */ uint64_t                       _internal_metadata;
  /* +0x10 */ RepeatedPtrField<MetaTensor>   input_tensors;   // field 1
  /* +0x28 */ RepeatedPtrField<MetaTensor>   output_tensors;  // field 2
  /* +0x40 */ MapField<int64,int64>          output_aliases_to;// field 6 (out->in)
  /* +0xB8 */ ArenaStringPtr                 serialized_graph_def; // field 4 (HLO)
  /* +0xC0 */ ArenaStringPtr                 name;            // field 5
  /* +0xC8 */ ModelConfig*                   model_config;    // field 3
  /* +0xD0 */ int64_t                        num_user_inputs; // field 7
  /* +0xD8 */ int64_t                        num_states;      // field 8
  /* +0xE0 */ int64_t                        num_weights;     // field 9
};
```

### 3.3 `metaneff::ModelConfig` — load/exec policy

`[HIGH/OBSERVED]` RTTI `_ZTSN8metaneff11ModelConfigE @0x4d3da0`. The three bools
land at contiguous `+0x28`/`+0x29`/`+0x2a`; the three i64 fields (#1–3) parse but
have no observed native setter (consumed by NCG sizing in libnrt).

| off | size | type | field | proto # / meaning |
|---|---|---|---|---|
| `+0x00..0x17` | — | — | header + i64 #1–3 | `num_infer`(#1)/`timeout`(#2)/`optimal_ncg_size`(#3) |
| `+0x28` | 1 | `uint8_t` | `async_load` | #4 — `set_async_load` store @0x28385c |
| `+0x29` | 1 | `uint8_t` | `lazy_load` | #5 — `set_lazy_load` store |
| `+0x2a` | 1 | `uint8_t` | `return_aliases` | #6 — parser `setne +0x2a` |

> **CORRECTION (CARRIED from metaneff §2.2).** A backing report left
> `return_aliases` with no offset and tagged #1–3 tentative. The
> `ModelConfig::_InternalParse` body resolves all three bools precisely
> (`+0x28`/`+0x29`/`+0x2a`); only the i64 fields #1–3 lack an observed setter.
> `[OBSERVED]`

### 3.4 `MetaTensor.DataType` / `.Type` enums

`[HIGH/OBSERVED]` From the descriptor blob; **value 11 is a reserved gap**:

```
DataType: UNDEFINED=0 FLOAT=1 INT32=2 BYTE=3 STRING=4 BOOL=5 UINT8=6 INT8=7
          UINT16=8 INT16=9 INT64=10 <11 reserved> FLOAT16=12 DOUBLE=13
          BFLOAT16=14 F8E4M3FN=15 F8E5M2=16
Type:     UNDEFINED_TYPE=0 USER_INPUT=1 INPUT_STATE=2 INPUT_WEIGHT=3
```

The shared index that ties the host and device key-rings: `MetaTensor` ordinal *i*
== NEFF `var_id i` == `nrt` tensor-set ordinal *i* == device `kbin_mem_ref[var_id i]`
([metaneff §1](../neff/metaneff-io-abi.md)).

---

## 4. `aws_hal_stpb` — per-NeuronCore TPB HAL state (1104 B)

`[HIGH × OBSERVED size; INFERRED interior]` DWARF member `hal_stpb` of type
`aws_hal_stpb`, **size 1104 B**, embedded at the head of the `sunda` union inside
`tpb_t` (union offset `+0`, absolute `tpb_t+17032`;
[multimodel-context §6](../runtime/multimodel-context-dmem.md)). This is the
per-NeuronCore programmable-sequencer HAL state that `aws_hal_stpb_init @0x458350`
populates from a 5-engine config and programs over BAR0; it is the host mirror of
the device's five sequencer engines.

> **NOTE — interior is reconstructed, not field-dumped.** The 1104-B total is
> DWARF-`OBSERVED`; the sibling pages do **not** expose `aws_hal_stpb`'s member
> table (it lives only in the libnrt `*_structures.json` not present in this
> extraction). The layout below is **INFERRED** from three observed facts: (a)
> `tpb_eng_init_hals_v2 @0x2687e0` builds a **5-engine** config (PE / ACT /
> **POOL** / SP / DVE), each with an instruction-fetch DMA ring and a
> `hw_decode_*` table ([libnrt surface §5.3](../runtime/libnrt-surface.md)); (b)
> the embedding `sunda` union pairs `aws_hal_stpb` (1104) directly with
> `dma_queue_info_t[5]` (1800) — i.e. the 5 instruction-fetch rings sit
> **alongside**, not inside, `aws_hal_stpb`; (c) `1104 / 5 ≈ 220.8` is not an
> integer, so the per-engine sub-blocks are **not** a clean `T[5]` — `aws_hal_stpb`
> holds engine-shared HAL state (register-window bases, the arch-dispatch
> pointer, the Q7 ucode-image handles) plus per-engine `hw_decode` references,
> with the fetch rings factored out into the adjacent array.

**Table 4.0 — `aws_hal_stpb` reconstructed shape** `[INFERRED]`:

| region | role (from the §5.3 consumer) | confidence |
|---|---|---|
| register-window bases | the BAR0 sequencer-CSR aperture(s) `aws_hal_stpb_init` writes through | MED/INFERRED |
| arch dispatch | the `al_hal_tpb_get_arch_type()`-keyed function table (Sunda=2/Cayman=3/Mariana=4) | HIGH/OBSERVED (the dispatch exists) |
| per-engine `hw_decode_*` | one decode table reference per {PE,ACT,POOL,SP,DVE} | MED/INFERRED |
| Q7 ucode image handles | `{iram,dram}` for the Pool/Q7 GPSIMD engine (the override target, §10) | HIGH/OBSERVED (the globals exist) |
| `aws_hal_q7_ucode_eng_init_<arch>` state | the per-arch Q7 init bookkeeping | MED/INFERRED |

> **GOTCHA — the 5 fetch rings are NOT inside `aws_hal_stpb`.** The `sunda` union
> lays `aws_hal_stpb` (1104) then `dma_queue_info_t[5]` (1800) **consecutively**
> (union offsets `+0` and `+1104`), so the instruction-fetch rings are a separate
> member (`instr_fetch_queue`, §5), not a sub-array of the HAL struct. A
> reimplementer must size `aws_hal_stpb` at exactly **1104** and place the five
> 360-B rings immediately after it. `[HIGH/OBSERVED — union member offsets]`

```c
// aws_hal_stpb — DWARF member `hal_stpb`, size 1104.  [HIGH size / INFERRED interior]
// Embedded at tpb_t+17032 (sunda union +0); programmed by aws_hal_stpb_init @0x458350.
struct aws_hal_stpb {                  /* size 1104 (0x450) */
  /* engine-shared HAL: BAR0 sequencer-CSR window bases, arch-dispatch table,   */
  /* per-engine hw_decode_* references (PE/ACT/POOL/SP/DVE), and the Q7 ucode    */
  /* {iram,dram} image handles the override (§10) swaps. Exact member offsets    */
  /* are not exposed by the shipped sibling artifacts → INFERRED.                */
  uint8_t _opaque[1104];
};
```

---

## 5. `dma_queue_info_t` — per-engine instruction-fetch ring (360 B)

`[HIGH × OBSERVED size; INFERRED interior]` DWARF member `instr_fetch_queue` of
type `dma_queue_info_t[5]`, **array total 1800 B ⇒ 360 B per element**, at the
`sunda` union offset `+1104` (absolute `tpb_t+18136`). One ring per sequencer
engine (PE/ACT/POOL/SP/DVE): the engine fetches its 64-byte instruction slots
through this ring.

> **NOTE — 360 B confirmed by the array bound, not a `sizeof`.** The `[5]`-array
> byte-size `1800` divided by 5 yields exactly `360` (`0x168`) — this is the
> SCOPE-named size, doubly grounded by the array bound and the five-engine
> sequencer model. The per-field interior is not in the sibling artifacts; the
> reconstruction below maps the al_udma v4 per-queue ring bank
> ([descriptor field tables §6](../dma/descriptor-ring-field-tables.md)) into the
> host-side bookkeeping a fetch ring must carry.

**Table 5.0 — `dma_queue_info_t` reconstructed shape (360 B)** `[INFERRED]`. A
host DMA-queue info block tracks the device ring it drives: the `dmem_t` backing
the descriptor ring, the ring base/length, the producer/consumer pointers
(mirroring `TDRBP`/`TDRL`/`TDRTP`/`TDRHP` of the M2S_Q bank), the completion
semaphore, the QoS/priority class, and the engine binding.

| region | role | grounded against |
|---|---|---|
| ring `dmem_t*` + base | the HBM-resident descriptor ring buffer | `dmem_t` (§ allocator); M2S_Q `TDRBP` |
| ring length / capacity | descriptor count | M2S_Q `TDRL` (`offset[23:0]`) |
| tail / head shadows | host-side producer/consumer mirrors | M2S_Q `TDRTP`/`TDRHP` |
| doorbell handle | the `TDRTP_inc` MMIO the host pings to launch | al_udma bank `+0x38` |
| completion semaphore | index + increment for `dmacomplete` | descriptor `semaphore`/`sem_increment` |
| `priority_class` (0..4) | device DGE priority feeding AXI QoS | `DMA_CONFIGS.priority_class:3` |
| engine binding | which of {PE,ACT,POOL,SP,DVE} owns this ring | the `[5]` array index |

```c
// dma_queue_info_t — DWARF member instr_fetch_queue : dma_queue_info_t[5] (1800 B).
// Per-element 360 B (0x168).  [HIGH size / INFERRED interior]
struct dma_queue_info_t {              /* size 360 (0x168) */
  /* Host bookkeeping for one sequencer-engine instruction-fetch DMA ring:      */
  /* the backing dmem_t + ring base/length, the producer/consumer shadow ptrs   */
  /* (mirroring the al_udma M2S_Q TDRBP/TDRL/TDRTP/TDRHP bank), the TDRTP_inc    */
  /* doorbell handle, the completion semaphore (idx+inc), the priority_class     */
  /* (0..4), and the owning engine id. Interior offsets not exposed → INFERRED.  */
  uint8_t _opaque[360];
};
```

> **QUIRK — two "queue" word-senses on this page.** `dma_queue_info_t` (this
> section) is the **host descriptor-ring bookkeeping** struct (5 of them, one per
> sequencer engine). The **`dma_queue_bundle` 16-queue instance** (§6) is the
> per-arch table that maps a `(engine, queue_id)` pair to a HW al_udma queue —
> and it is where **queue_id 16** marks the custom-op queue. They are unrelated
> structs that both contain "queue"; do not merge them.

---

## 6. `dma_queue_bundle` + the 16-queue instance (idx 16 = custom-op)

`[HIGH idx-16 / INFERRED fields]` A `dma_queue_bundle` is one entry of the
per-arch **`v2_queue_bundle_alloc_table`** that the queue-bundle LUT
(`dma_ring_setup_queue_bundles`, named in
[multimodel-context §5.3](../runtime/multimodel-context-dmem.md)) populates at
model-add. `dma_is_custom_op_dma_v2(eng_id, queue_id) @0x22e120` scans this table;
**a DMA whose bundle entry has `queue_id == 16` is a custom-op DMA**, the
dedicated bundle the Pool/Q7 GPSIMD engine claims for custom kernels
([libnrt surface §5.4](../runtime/libnrt-surface.md)).

> **NOTE — the 16-queue model is grounded; the bundle's fields are not.** The HW
> al_udma engine exposes a **16-queue** per-engine ring bank (`M2S_Q[16]` /
> `S2M_Q[16]`, base `0x1000`, stride `0x1000`;
> [descriptor field tables §6](../dma/descriptor-ring-field-tables.md)). The
> `queue_id == 16` custom-op tag is therefore the **17th index** beyond the 16
> HW ring banks (`0..15`) — a software bundle id, not a HW ring slot — used by
> `dma_is_custom_op_dma_v2` to route the GPSIMD custom-op descriptor stream. The
> `queue_id == 16` ↔ custom-op mapping is **table-structure-OBSERVED**; the field
> layout of one `dma_queue_bundle` entry is **INFERRED** (it must carry at least
> `{engine_id, queue_id, ring/dmem handle}`).

**Table 6.0 — the 16-queue bundle instance** `[HIGH idx / INFERRED per-entry]`:

| bundle idx | HW al_udma ring | role |
|---|---|---|
| `0..15` | `M2S_Q[i]` / `S2M_Q[i]` (the 16 HW ring banks) | general DMA queues (per-engine, per-direction) |
| **`16`** | — (software bundle) | **the custom-op / GPSIMD Pool queue** — `dma_is_custom_op_dma_v2` returns true |

```c
// dma_queue_bundle — one entry of the per-arch v2_queue_bundle_alloc_table.
// Scanned by dma_is_custom_op_dma_v2 @0x22e120.  [HIGH idx-16 / INFERRED fields]
struct dma_queue_bundle {              /* per-entry size not exposed -> INFERRED */
  uint32_t engine_id;     // the owning TPB engine
  uint32_t queue_id;      // 0..15 = HW al_udma ring; 16 = custom-op (GPSIMD) queue
  void    *ring;          // the dmem_t / al_udma ring handle for this bundle
  /* additional per-bundle state (priority, completion sema) not exposed.        */
};
// The 16-queue instance is the table[engine] -> bundle[17] mapping; index 16 is
// the dedicated custom-op bundle the Pool/Q7 engine claims.
```

---

## 7. The OPCODE instruction-variant table

The SCOPE's "OPCODE_1 instruction-variant table" resolves to **two
binary-grounded forms**: (a) the device DMA/TPB **opcode enum** (the 1-byte
`byte0` selector), and (b) the codec-side **opcode → variant/specialization
presence dictionary** (`nrtucode_opset_t.opcode_bucket[256]`). Both are
`[HIGH/OBSERVED]`.

### 7.1 The TPB/DMA opcode enum (the `byte0` selector)

`[HIGH/OBSERVED — CARRIED from descriptor field tables §2/§4]` Each 64-byte
sequencer slot's `byte0` is the opcode; `byte1 = inst_word_len = 0x10`:

| opcode | mnemonic | struct |
|---|---|---|
| `0xb8` | `DMAMEMCPY` | `DMA_DIRECT2D` |
| `0xb9` | `DMA_MEMCPY2` | `DMA_COPY2D` (v5) |
| `0xba` | `DMA_IMMEDIATE` | `DMA_IMMEDIATE` (v5) |
| `0xbb` | `DMA_INDIRECT` | `DMA_INDIRECT1D` |
| `0xbd` | `DMA_TRANSPOSE` | `DMA_DIRECT2D_XPOSE` (NC-v3+) |
| `0xbf` | `SB2SB_COLLECTIVE` | `S3D3_COLLECTIVE` |
| `0xd4` | `PSEUDO_DMA_DIRECT2D` | compiler pseudo |
| `0xf0` | `EXTENDED_INST` | `EXTENDED_STRUCT` (RDMA op8/op9) |
| `0xf1` | `DMA_GATHER_TRANSPOSE` | `DMA_GATHER_XPOSE` (NC-v3+) |

> **QUIRK — `0xf0` (`EXTENDED_INST`) is the "OPCODE_1" overload point.** The
> extended form keys a **second** opcode byte (`extended_opcode @+12`) — e.g.
> `RDMA_DESC_GEN==8` / `RDMA_DESC_START==9` — so `0xf0` is precisely where a
> single ISA opcode fans out into a variant table. The codec dictionary (§7.2)
> tracks this: when `opcode == 0xF0` it indexes a **specialization** sub-array by
> `instr[12]`. This is the "instruction-variant table" the SCOPE names.

### 7.2 `nrtucode_opset_t` — opcode → specialization presence (`malloc(0x830)`)

`[HIGH/OBSERVED]` The codec opcode dictionary
([object-model graph §5](../runtime/object-model-graph.md)). ctor
`nrtucode_opset_create @0x9b24c0` (`mov $0x830,%edi`,
`memset(opset+8,0,0x800)`); dtor `@0x9b25c0`.

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0x000` | 8 | `nrtucode_context_t*` | `context` | ctor arg1 (back-edge) |
| `+0x008` | 8×256 | `void*[256]` | `opcode_bucket[op]` | one slot per opcode `0..255`; NULL=absent; a present bucket is a `calloc(1,0x100)` 256-entry `uint8` specialization-presence array |
| `+0x808` | 40 | `char[0x28]` | `friendly_name` | inline name buffer (`+0x808..+0x830`) |

Total `0x830 = 8 + 256·8 + 0x28`. **Cross-check:** `mov $0x830,%edi @0x9b24df`;
the name `snprintf` writes at `opset+0x808` (`add $0x808,%rdi @0x9b250c`), and
`set_friendly_name` forces the NUL at `+0x828` — reachable only if the field
extends past `+0x828`, so the trailing `0x28` is all name buffer (no separate
pad). `[OBSERVED]`

```c
// nrtucode_opset_t — malloc(0x830).  [HIGH/OBSERVED]
struct nrtucode_opset_t {
  /* +0x000 */ nrtucode_context_t *context;
  /* +0x008 */ uint8_t            *opcode_bucket[256]; // NULL or calloc(1,0x100) presence array
  /* +0x808 */ char                friendly_name[0x28];
};
// add_instruction: op = instr[0]; if (!bucket[op]) bucket[op]=calloc(1,0x100);
//   if (op==0xF0) bucket[op][instr[12]] = 1;   // extended-opcode specialization
```

> **QUIRK — `nrtucode_opset_t` is NOT leak-tracked.** Unlike the context/core/ll
> objects, `opset_destroy` calls no `objcount_decrement` (and `opset_create` no
> `increment`) — verified by the absence of `call 9b17b0` in the body. The
> opcode dictionary is a lighter, untracked object.
> ([object-model graph §5 CORRECTION/QUIRK](../runtime/object-model-graph.md).)

---

## 8. `aws_hal_ens` / `aws_hal_ens_nq` — the NQ software mirror

`[MED × INFERRED]` The SCOPE names `aws_hal_ens` (per-NeuronCore **ENS** =
event/notification subsystem state) and `aws_hal_ens_nq` (the per-queue **NQ
software mirror**). Neither struct's field table is exposed by the committed
artifacts; the observable anchors are: the `tpb_t` member `ens_regs_base`
(`volatile void* @ tpb_t+0x10`) — the BAR aperture of the ENS register block —
and the `notification` member (`notification_t`, **size 15816 B** @ `tpb_t+32`,
"NQ rings") ([multimodel-context §2](../runtime/multimodel-context-dmem.md)). The
`aws_hal_stpb_init` call passes a `&tpb->notification.ens` argument
([libnrt surface §5.3](../runtime/libnrt-surface.md)), confirming `ens` is a
sub-object of the notification block.

> **NOTE — reconstructed from the HW NOTIFIC block, not field-dumped.** The HW
> side is fully specified ([NOTIFIC queue CSR](../control/csr/notific-queue.md)):
> a per-queue **ring descriptor** is `{base_addr_lo/hi, size, head_ptr (consumer,
> SW-written), tail_ptr (producer, HW-advanced), threshold}` (6 registers,
> 0x28 bytes per queue), and there are up to **10 SW NQs** (`NUM_SW_Q`). The
> `aws_hal_ens_nq` **software mirror** is the host shadow of exactly that ring
> descriptor plus the phase-bit epoch the fast-path drain polls. The `aws_hal_ens`
> parent holds the per-NeuronCore ENS register-window base and the array of
> per-queue mirrors.

**Table 8.0 — `aws_hal_ens_nq` reconstructed shape (one SW NQ mirror)**
`[INFERRED, grounded on the NOTIFIC ring descriptor]`:

| field | role | HW counterpart |
|---|---|---|
| `ring_base` (u64) | SoC/RAM address of the NQ ring | `base_addr_lo`/`_hi` |
| `ring_size` (u32) | ring size in bytes | `size` |
| `head` (u32) | host consumer pointer (delta from base) | `head_ptr` (SW-written, re-arms `intr_6`) |
| `tail` (u32) | last producer pointer (cached) | `tail_ptr` (HW-advanced, RO) |
| `threshold` (u32) | usage-threshold interrupt level | `threshold` |
| `phase` (u8) | the expected ring-wrap epoch for the fast path | `NOTIFICATION_PHASE_BIT` |

```c
// aws_hal_ens_nq — host software mirror of one NOTIFIC SW NQ ring descriptor.
//   [MED/INFERRED — grounded on the 0x28-byte HW per-queue descriptor]
struct aws_hal_ens_nq {
  uint64_t ring_base;   // base_addr_lo/hi
  uint32_t ring_size;   // size (bytes)
  uint32_t head;        // consumer ptr (SW advances; drain-ACK + intr_6 re-arm)
  uint32_t tail;        // producer ptr (HW advances; cached shadow)
  uint32_t threshold;   // usage-threshold interrupt level
  uint8_t  phase;       // expected ring-wrap epoch (NOTIFICATION_PHASE_BIT)
};

// aws_hal_ens — per-NeuronCore ENS state: register-window base + the NQ mirrors.
//   [MED/INFERRED]  (a sub-object of notification_t @ tpb_t+32; ens_regs_base @ tpb_t+0x10)
struct aws_hal_ens {
  volatile void   *ens_regs_base;        // BAR aperture of the ENS register block
  aws_hal_ens_nq   nq[/* up to NUM_SW_Q = 10 */];
};
```

> **GOTCHA — `tail_ptr` is not the arrival signal.** Mirroring the HW rule, the
> `aws_hal_ens_nq.tail` shadow does **not** guarantee the last records reached
> RAM; the authoritative arrival signal is the in-RAM **phase bit** of the next
> entry at `ring_base + head`. A drain that trusts `tail` over the phase bit will
> read stale/torn entries. `[MED/INFERRED — CARRIED from NOTIFIC §4.3]`

---

## 9. `ngc` / gconf — the process-global runtime config (`@.bss 0xc5c460`)

`[MED × INFERRED]` `gconf` (recovered via the accessor `nrt_gconf()`) is the
process-global runtime configuration object, **zero-initialized in `.bss`**.
SCOPE pins it at `0xc5c460`.

> **NOTE — `.bss` is zero-init: the layout is the type, not the bytes.** Because
> `ngc` lives in `.bss`, its *bytes* are all zero at load and are filled at
> `nrt_init` from environment variables and the negotiated driver/runtime
> compat ranges. So this section documents the **type** (the fields the runtime
> reads through `nrt_gconf()`), not a byte dump. The accessor pattern is
> `nrt_gconf()->FIELD`; one confirmed read is `nrt_gconf()->test_zerocopy`
> (gating the BAR4 zero-copy weight-stage path,
> [relocation/weights §8.4](../neff/relocation-weights.md)).

> **CORRECTION — the global is named/sized only by its accessor; the .bss address
> is SCOPE-asserted, not re-grounded here.** The `0xc5c460` address is not
> independently confirmable from the artifacts in this extraction (the nearest
> re-grounded `.bss`/`.data` globals are `last_model_handle.50 @ 0xc09180` and the
> `pool_eng_*_bin` ucode globals at `0xc97018..0xc97030`,
> [libnrt surface §5.1](../runtime/libnrt-surface.md)). Treat `0xc5c460` as
> `CARRIED` from SCOPE; the *field semantics* below are INFERRED from observed
> `nrt_gconf()->…` read sites and the documented runtime tunables.

**Table 9.0 — `ngc` (gconf) observed/inferred fields** `[MED/INFERRED]`:

| field | role | grounded against |
|---|---|---|
| `test_zerocopy` (bool) | BAR4 direct weight-stage vs bounce DMA | `dmem_buf_copyin` branch (OBSERVED) |
| `visible_virtual_cores` (count/list) | the LNC slice this process owns | `nrt_tensor_allocate` range gate (OBSERVED) |
| `enable_metrics` (bool) | turn on the dmem usage ledger / ntrace | `dmem_alloc_internal` metrics branch (OBSERVED) |
| `virtual_core_size` (u32) | LNC grouping (default 2; `NEURON_RT_VIRTUAL_CORE_SIZE`) | `parse_vnc_config @0x83b40` (OBSERVED) |
| CC / mesh / ring tunables | the `dbg_*` block carried in `tdrv_ctx_t` | `tdrv_ctx_t` tail fields (OBSERVED) |

```c
// ngc / gconf — process-global runtime config, .bss (SCOPE: @0xc5c460), zero-init.
//   accessor nrt_gconf().  [MED/INFERRED — fields from observed read sites]
struct nrt_gconf_t {
  bool      test_zerocopy;        // BAR4 zero-copy weight stage (OBSERVED read)
  uint32_t  virtual_core_size;    // LNC grouping (NEURON_RT_VIRTUAL_CORE_SIZE)
  /* visible_virtual_cores, enable_metrics, and the CC/mesh/ring dbg_* tunables  */
  /* are read through nrt_gconf()->… ; exact offsets not re-grounded here.       */
};
```

---

## 10. The Pool/Q7 ucode-override globals (the GPSIMD injection seam)

`[HIGH/OBSERVED]` Adjacent to `ngc`, the four `.bss` globals the custom-op
registration writes — the single most important reimplementation seam — are
field-exact ([libnrt surface §5.1](../runtime/libnrt-surface.md)):

| `.bss` addr | size | global | written by |
|---|---|---|---|
| `0xc97030` | 8 | `pool_eng_iram_bin` | `nrt_set_pool_eng_ucode` ← `nrt_ucode_info+0x00` |
| `0xc97028` | 8 | `pool_eng_iram_bin_size` | ← `+0x08` |
| `0xc97020` | 8 | `pool_eng_dram_bin` | ← `+0x10` |
| `0xc97018` | 8 | `pool_eng_dram_bin_size` | ← `+0x18` |

```c
// nrt_set_pool_eng_ucode argument — DWARF-confirmed, 32 B.  [HIGH/OBSERVED]
struct nrt_ucode_img  { void  *bin;  size_t size; };          // 16 B
struct nrt_ucode_info { nrt_ucode_img iram;  /* +0  */        // 32 B
                        nrt_ucode_img dram;  /* +16 */ };
```

`tpb_eng_init_hals_v2` reads these four globals at the override site and, when
non-NULL, **silently substitutes** the registered `{iram,dram}` for the stock
Pool ucode in the `aws_hal_stpb` (§4) config before `aws_hal_stpb_init` programs
the device. This is the host seam a GPSIMD custom kernel rides.

---

## 11. The per-NeuronCore context — `tpb_t` (39 320 B) and its 16-way arrays

`[HIGH/OBSERVED]` The SCOPE's "16-way kmd_context" and the per-core context array.
The per-NeuronCore context **is** the `tpb_t` (DWARF size **39 320 B == 0x9998**),
8 of which sit in each `mla_t`, 32 of which sit in the one process-global
`tdrv_ctx_t` ([multimodel-context §1–2](../runtime/multimodel-context-dmem.md)).
Its GPSIMD-material members and **two 16-way arrays**:

| off | size | type | field | meaning |
|---|---|---|---|---|
| `+0x00` | 8 | `volatile void*` | `tpb_mem_base` | core SBUF/reg window |
| `+0x08` | 8 | `volatile void*` | `tpb_regs_base` | |
| `+0x10` | 8 | `volatile void*` | `ens_regs_base` | the ENS register aperture (§8) |
| `+0x18` | 4 | `int` | `idx` | `device_tpb_idx` 0..7 |
| `+0x20` | 15816 | `notification_t` | `notification` | the NQ rings (holds `ens`, §8) |
| `+0x3DE8` | 1176 | `pool_stdio_block_t` | `pool_stdio_block` | the Q7 `printf`/stdio ring |
| `+0x4280` | 8 | `dmem_allocator_t*` | `tpb_allocator` | per-core GLOBAL heap |
| `+0x4288` | 2960 | `sunda` union | — | holds `aws_hal_stpb` (§4) + `dma_queue_info_t[5]` (§5) + ulib cache |
| `+0x4E18` | 40 | `pthread_mutex_t` | `model_db_lock` | guards `model_db` |
| `+0x4E40` | 8 | `ht_t*` | `model_db` | per-core model database |
| `+0x4E48` | 8 | `H_MODEL` | `h_running_model` | currently-executing model |
| `+0x4E50` | 1280 | `aws_hal_dma[32]` | `dma` | 32 DMA-engine HAL handles |
| `+0x9928` | 40 | `nrtucode_core_t*[5]` | `nrtucode_core` | NX PE/ACT/POOL/DVE/SP cores |
| `+0x9940` | 64 | `nrtucode_core_t*[8]` | `pooling_q7_nrtucode_core` | the **8** GPSIMD Q7 cores |
| `+0x9980` | 8 | `nrtucode_context_t*` | `nrtucode_context` | host handle to the device ulib ctx |

> **NOTE — the 16-way arrays.** The literal **16-element** array in this tree is
> `mla_t.top_sps : top_sp_t[16]` (262 016 B @ `mla_t+314672`,
> [multimodel-context §1.1](../runtime/multimodel-context-dmem.md)) — the
> per-device top-SP block. The GPSIMD-specific fan-out is the **8**-way
> `pooling_q7_nrtucode_core[8]` (the eight on-device Q7 cores, `NUM_POOL_CORES`).
> The SCOPE's "16-way kmd_context" is the **driver portal** (§12), not a `tpb_t`
> sub-array. `[HIGH/OBSERVED]`

The full `tdrv_ctx_t` (18 884 656 B) / `mla_t` (590 008 B) / `model_t` (7744 B) /
`dmem_t` (192 B) / allocator tree is field-exact in
[multimodel-context §1–5](../runtime/multimodel-context-dmem.md); the salient
strides are `imul $0x900b8` (`mla_t`) and `imul $0x9998` (`tpb_t`) in
`db_physical_core_get_mla_and_tpb @0x2272a0`.

```c
// tpb_t — the per-NeuronCore context, DWARF size 0x9998 (39 320).  [HIGH/OBSERVED]
// (GPSIMD-material members; full 24-member layout in multimodel-context §2.)
struct tpb_t {                                  /* size 0x9998 */
  /* +0x0010 */ volatile void          *ens_regs_base;
  /* +0x0018 */ int                     idx;             // device_tpb_idx 0..7
  /* +0x0020 */ notification_t          notification;    // NQ rings (ens sub-object)
  /* +0x4280 */ dmem_allocator_t       *tpb_allocator;   // per-core GLOBAL heap
  /* +0x4288 */ union {                                  // the `sunda` union (2960 B)
      aws_hal_stpb      hal_stpb;            /* +0     (1104, §4) */
      /* dma_queue_info_t instr_fetch_queue[5] @ union+1104 (1800, §5) */
  } sunda;
  /* +0x4E40 */ ht_t                   *model_db;        // per-core model database
  /* +0x9928 */ nrtucode_core_t        *nrtucode_core[5];          // NX seq cores
  /* +0x9940 */ nrtucode_core_t        *pooling_q7_nrtucode_core[8];// GPSIMD Q7 cores
};
```

---

## 12. `kmd_context` — the kernel-mode-driver IOCTL portal

`[MED × INFERRED]` The SCOPE's `kmd_context` is the host's handle onto the
**Neuron kernel-mode driver** — the `ndl_*` (Neuron Driver Layer) portal that
every device alloc/copy/IOCTL flows through. The observable anchor is the
`mla_t.device` member (`ndl_device_t* @ mla_t+314664`,
[multimodel-context §1.1](../runtime/multimodel-context-dmem.md)): the per-device
driver handle the resolver hands to `ndl_memory_alloc` / `ndl_memory_copy_as` /
`ndl_memory_get_pa`. The `ndl_*` subsystem is 122 statically-embedded functions
([libnrt surface §2](../runtime/libnrt-surface.md)).

> **NOTE — `kmd_context` is named by SCOPE; the host portal it denotes is
> `ndl_device_t`.** No struct literally named `kmd_context` is exposed by the
> committed artifacts; the runtime's KMD handle is `ndl_device_t*` (one per
> `mla_t`). Its interior is opaque to libnrt (it wraps the driver file
> descriptor + the BAR mmap apertures + the IOCTL multiplexer). Treat
> `kmd_context` as a synonym for the per-device `ndl_device_t` portal. The "16-way"
> qualifier most plausibly refers to a per-device fan-out of driver queues/cores;
> the only re-grounded 16-element device array is `mla_t.top_sps[16]` (§11).
> `[MED/INFERRED]`

```c
// kmd_context  ==  the per-device ndl_device_t portal handle.  [MED/INFERRED]
// mla_t.device : ndl_device_t* @ mla_t+314664.  Opaque to libnrt; wraps the
// driver fd + BAR0/BAR2/BAR4 mmap apertures + the ndl_* IOCTL multiplexer.
struct ndl_device_t;   // opaque KMD portal; one per attached device (mla_t)
```

---

## 13. The `ntff::` trace oneofs / act-table family

`[MED × OBSERVED]` `ntff` = **Neuron Trace File Format** — the on-disk profiler
schema serialized by `nrt_profile_*` / `nrt_sys_trace_*`. The 50+ `ntff::`
classes all derive from `google::protobuf::Message` via `__si_class_type_info`
([libnrt surface §4.3](../runtime/libnrt-surface.md)), so they are generated
protobuf messages with the standard `{_vptr, _internal_metadata, _has_bits_,
_cached_size_}` header (§3) and `oneof`-discriminated bodies.

**Table 13.0 — representative `ntff::` messages** `[MED/OBSERVED — RTTI names]`:

| RTTI class | role |
|---|---|
| `ntff::ntrace_data_file` | the top-level trace-file message |
| `ntff::ntrace_event` | one trace event (the body is a `oneof` over event kinds) |
| `ntff::engine_instruction_info` | per-engine instruction telemetry |
| `ntff::collectives_*` | collectives-op trace records |
| `ntff::nc_memory_usage` | per-NeuronCore memory-usage snapshot |

> **NOTE — the "act-table family" is the `ACT_TABLE` usage class, not an `ntff`
> message.** The SCOPE pairs "ntff oneofs / act-table". The only `ACT_TABLE`
> re-grounded in the committed pages is the dmem **usage category**
> `DMA_MEM_USAGE_TYPE_ACT_TABLE = 8`
> ([multimodel-context §3.3](../runtime/multimodel-context-dmem.md)) — the byte
> class under which the per-model **activation-function lookup tables** are
> allocated (the per-model MODEL allocator,
> [multimodel-context §10](../runtime/multimodel-context-dmem.md)). The act
> tables themselves are device data (HBM-resident lookup tables); the host struct
> is the `dmem_t` (§ allocator) booked under usage 8. The `ntff::` oneofs and the
> act-table allocations are two distinct things the SCOPE lists together; neither
> exposes a field-exact host struct beyond the generic protobuf-Message header
> (`ntff`) and the `dmem_t` allocation (act-table). `[MED]`

```c
// ntff::ntrace_event — generated protobuf Message; body is a oneof.
//   [MED/OBSERVED — RTTI _ZTSN4ntff*]  (standard libprotobuf Message header, §3)
struct ntrace_event /* : google::protobuf::Message */ {
  /* +0x00 */ void*    _vptr;
  /* +0x08 */ uint64_t _internal_metadata;
  /* +0x10 */ uint32_t _has_bits_;
  /* +0x14 */ uint32_t _cached_size_;
  /* + …  */  /* oneof-discriminated body (event-kind variants) */
};
// "act-table" is the dmem usage class DMA_MEM_USAGE_TYPE_ACT_TABLE=8, not an ntff msg:
//   per-model activation lookup tables -> dmem_t booked under usage 8.
```

---

## 14. The nrtucode codec-side host objects (sizes confirmed)

`[HIGH/OBSERVED]` For completeness, the four `libnrtucode_internal.so` heap
objects a GPSIMD codec embedder builds
([object-model graph §1–5](../runtime/object-model-graph.md)). Each is a flat
`malloc` block of **plain C** (no per-object C++ vtable); sizes are byte-exact
from each ctor's `mov $size,%edi ; call malloc@plt`.

| struct | `malloc` | key fields | ctor / dtor |
|---|---|---|---|
| `nrtucode_context_t` | `0x28` | `+0x00 rw_impl`, `+0x08 memhandle_impl`, `+0x10 userdata`, `+0x18 log_scratch_size`, `+0x20 log_scratch_buf` | `0x9b0290` / `0x9b03d0` |
| `nrtucode_core_t` | `0x70` | `+0x00 context`, `+0x10 coretype`, `+0x18 iram_base`, `+0x20 dram_base`, `+0x28 apb_base`, `+0x30 boot_state`, `+0x38 log_memhandle`, `+0x48 friendly_name[0x21]` | `0x9b0640` / `0x9b0780` |
| `nrtucode_ll_t` | `0x48` | `+0x00 context`, `+0x08 device_memhandle`, `+0x10 flags/flavor`, `+0x18 device_size`, `+0x20 friendly_name[0x21]` | `0x9b1a90` / `0x9b1da0` |
| `nrtucode_opset_t` | `0x830` | §7.2 | `0x9b24c0` / `0x9b25c0` |

The `nrtucode_core_t` **device control block** at `dram_base` (poked via the
memhandle vtable, not a host struct): `+0x00` claim magic
(`0x6099CB34` unclaimed → `0x502B2DA1` claimed), `+0x04` log size, `+0x08` log
ptr, `+0x10` log cursor, `+0x18` `priority_classes[≤4]`, `+0x28`
`dge_mailbox[4]` (each a 4-B `{reserved, priority, bitmask:u16}`), `+0x38`
`pc_bounds_lo`, `+0x40` `pc_bounds_hi` ([DGE host API](../runtime/dge-host-api.md)).

```c
// nrtucode_core_t — malloc(0x70).  [HIGH/OBSERVED]
struct nrtucode_core_t {
  /* +0x00 */ nrtucode_context_t *context;
  /* +0x08 */ void               *userdata;
  /* +0x10 */ uint32_t            coretype;   // NRTUCODE_CORE_* (Table 14.1)
  /* +0x14 */ /* pad */
  /* +0x18 */ uint64_t            iram_base;
  /* +0x20 */ uint64_t            dram_base;  // device control block (above)
  /* +0x28 */ uint64_t            apb_base;
  /* +0x30 */ uint32_t            boot_state; // 0=NOT BOOTED, 1=BOOTED_LEGACY
  /* +0x34 */ /* pad */
  /* +0x38 */ void               *log_memhandle;
  /* +0x40 */ uint32_t            log_buf_size;
  /* +0x44 */ uint32_t            log_read_cursor;
  /* +0x48 */ char                friendly_name[0x21];
  /* +0x69 */ /* tail pad to 0x70 */
};
```

**Table 14.1 — `NRTUCODE_CORE_*` coretype** (two families;
[object-model graph §9](../runtime/object-model-graph.md)) `[HIGH/OBSERVED]`:

```
Q7_POOL (Q7/GPSIMD engine):  6=SUNDA 13=CAYMAN 21=MARIANA 29=MARIANA_PLUS 37=MAVERICK
NX_POOL (NX seq engine):     2=SUNDA  9=CAYMAN 17=MARIANA 25=MARIANA_PLUS 32=MAVERICK
```

> **GOTCHA — two different arch numbering schemes.** The host libnrt
> `al_hal_tpb_arch_type_t` is `INVALID=0, SUNDA=2, CAYMAN=3, MARIANA=4`
> ([multimodel-context §1](../runtime/multimodel-context-dmem.md)); the codec
> `NRTUCODE_CORE_*` (above) uses `(arch, engine)` composite values. **`2` means
> the whole `SUNDA` arch in libnrt but specifically `SUNDA_NX_POOL` in nrtucode.**
> Keep the two enums separate. The Q7_POOL kind mask `0x102020204` (bits
> `{2,9,17,25,32}`) is the NX_POOL set the DGE/mailbox gate checks; the customop
> build masks `0x2020204` (drops MAVERICK bit 32). `[HIGH/OBSERVED]`

---

## 15. Adversarial self-verification

The five strongest offset/size claims, re-challenged against the binary-derived
artifacts this session:

1. **`kbin_mem_ref = 152 (0x98)`** — field offsets `{mr_type@0, name@8, size@0x10,
   alignment@0x18, var_id@0x20, dtype[16]@0x24, shape[8]@0x38, dtensor_md@0x78,
   buffer@0x80, u[16]@0x88}` sum to exactly `0x98 = 152` with two natural pads
   (`+0x04`, `+0x34`); matches `structures.json size = 152` and the
   [relocation/weights §7.3](../neff/relocation-weights.md) recap. **Confirmed.**
2. **`MetaNeff` i64 trio @ `0xD0/0xD8/0xE0`** — the parser stores
   `mov %rax,0xd0/0xd8/0xe0(%r12)` for `num_user_inputs/num_states/num_weights`;
   the `MapField` at `+0x40` spans to `+0xB8` (`0x78`, the libprotobuf MapField
   size), leaving `serialized_graph_def@0xB8, name@0xC0, model_config@0xC8`
   contiguous. **Confirmed** against
   [metaneff §2.3/§3](../neff/metaneff-io-abi.md).
3. **`aws_hal_stpb = 1104` and `dma_queue_info_t = 360`** — the `sunda` union
   lays `aws_hal_stpb (1104) @ union+0` then `dma_queue_info_t[5] (1800) @
   union+1104`; `1800/5 = 360` exactly. The fetch rings are **adjacent to**, not
   inside, the HAL struct (union member offsets prove it). **Confirmed** against
   [multimodel-context §6](../runtime/multimodel-context-dmem.md).
4. **`idx 16 = custom-op queue`** — `dma_is_custom_op_dma_v2 @0x22e120` returns
   true for a bundle whose `queue_id == 16`, one beyond the 16 HW al_udma ring
   banks (`M2S_Q[16]`/`S2M_Q[16]`, `0..15`). **Confirmed** against
   [libnrt surface §5.4](../runtime/libnrt-surface.md) +
   [descriptor field tables §6](../dma/descriptor-ring-field-tables.md).
5. **`nrtucode_opset_t = 0x830`** — `mov $0x830,%edi @0x9b24df`,
   `memset(opset+8,0,0x800) @0x9b2504`, name buffer `+0x808..+0x830` (NUL forced
   at `+0x828`): `8 + 256·8 + 0x28 = 0x830`. **Confirmed** against
   [object-model graph §5](../runtime/object-model-graph.md).

> **CORRECTION (the single strongest, surfaced by self-verify).** The SCOPE frames
> `dma_queue_info_t` and the `dma_queue_bundle` 16-queue instance as one structure
> ("dma_queue_bundle_t + the 16-queue instance"), inviting the reading that
> `dma_queue_info_t` *is* the 16-queue bundle. The binary says **they are
> distinct**: `dma_queue_info_t` is a **360-B host ring-bookkeeping** struct of
> which there are exactly **5** (one per sequencer engine: PE/ACT/POOL/SP/DVE,
> the `instr_fetch_queue[5]` member at `tpb_t+18136`), while the **16-queue
> instance** is the per-arch `v2_queue_bundle_alloc_table` whose `queue_id == 16`
> entry routes the custom-op DMA. Conflating them would size the custom-op routing
> table at 360 B and the per-engine fetch ring at 16 entries — both wrong. Size
> `dma_queue_info_t` at 360 B × 5, and model the bundle table as `[engine][≤17]`
> with index 16 reserved for the GPSIMD Pool queue. `[HIGH/OBSERVED]`

---

## 16. Confidence & gaps

* **HIGH × OBSERVED** — `kbin_mem_ref` (152) + `mr_type` enum + the runtime
  `mem_ref` (136); the three metaneff messages (`MetaTensor`/`MetaNeff`/
  `ModelConfig`) field-exact + their DataType/Type enums; the `tpb_t` (39 320) +
  `tdrv_ctx`/`mla`/`model_t` tree + the 23 usage categories; the four nrtucode
  objects (0x28/0x70/0x48/0x830) + `nrtucode_opset_t.opcode_bucket[256]`; the
  TPB/DMA opcode enum; the `aws_hal_stpb` (1104) and `dma_queue_info_t` (360)
  **total sizes**; the `idx 16 = custom-op` routing fact; the Pool/Q7 override
  globals (§10); the `nrt_ucode_info` (32) layout.
* **MED/INFERRED (flagged inline):** the `aws_hal_stpb` / `dma_queue_info_t`
  **interiors** (reconstructed from the 5-engine config + al_udma ring bank);
  the `aws_hal_ens` / `aws_hal_ens_nq` mirror (reconstructed from the NOTIFIC
  ring descriptor); the `ngc`/gconf field set (from `nrt_gconf()->…` read sites);
  the `kmd_context` ↔ `ndl_device_t` identification; the `dma_queue_bundle`
  per-entry fields; the `ntff::`/act-table pairing.
* **CARRIED:** the `ngc` `.bss` address `0xc5c460` (SCOPE-asserted; nearest
  re-grounded globals are `last_model_handle.50 @0xc09180` and `pool_eng_*` at
  `0xc97018..0xc97030`).
* **WALL — v5/Maverick interiors are INFERRED.** No `maverick` source dir / no
  dispatch suffix appears in either ELF; any v5 field claim is INFERRED.
* **Not field-resolvable from this extraction:** the byte-level member tables of
  `aws_hal_stpb`, `dma_queue_info_t`, `aws_hal_ens(_nq)`, `dma_queue_bundle`,
  `ngc`, and `kmd_context` — these live only in the libnrt `*_structures.json`
  DWARF sidecar, which is not present here. Their **total sizes / type
  identities / roles** are grounded; their interiors are the documented gaps.
