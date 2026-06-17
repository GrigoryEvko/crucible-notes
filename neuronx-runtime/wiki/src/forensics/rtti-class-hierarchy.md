# C++ Class Hierarchy and RTTI

> **Binary:** `extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so` → `libnrt.so.1` → `libnrt.so.2.31.24.0` · **Version** `2.31.24.0-0b044f4ce` (`.nrt_brazil_version` @`0xad41f0` = `"2.31.24.0"`) · **BuildID[sha1]** `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e` · ELF64 LSB **DYN** x86-64, NOT stripped, DWARF v4.
>
> **Part II — Binary Anatomy & Forensics** / REFERENCE census · **Evidence grade:** every count below is re-derived from this exact binary — `nm -C --defined-only` bucketed on the `_ZTV`/`_ZTI`/`_ZTS` mangling prefix for the **defined** census, `readelf -rW` at `_ZTI+0` (RTTI kind) and `_ZTI+16` (single-inheritance base edge) for the inheritance graph, and the IDA `…_rtti.json` sidecar (770 xref-discovered records) for the demangled roster and the import-vs-defined reconciliation. · [back to forensics index](overview.md)

## Abstract

This page is the **C++ type map of `libnrt.so`** — the RTTI surface that the C++ compiler emitted for every polymorphic class the runtime links, both first-party and statically-vendored. The runtime defines **201 vtables (`_ZTV`), 244 typeinfo objects (`_ZTI`), and 245 typeinfo-names (`_ZTS`)** in its own `.symtab` (`nm --defined-only`), and the typeinfo records carry an inheritance graph that is, remarkably, **flat**: every one of the 244 typeinfos is either a `__class_type_info` root (53) or a `__si_class_type_info` single-base node (191) — there is **no `__vmi_class_type_info` anywhere**, so libnrt contains zero multiple-inheritance and zero virtual-base hierarchies. The map matters because it is the cleanest provenance signal in the binary: 137 of the 201 vtables are vendored (97 `google::protobuf`, 16 Abseil, 13 `std`, 11 simdjson), leaving exactly **40 `ntff::` AWS-authored protobuf-message schemas and 21 first-party global-namespace classes** (`enc_*`/`alg_mesh_*`/`dma_desc_*`/`mem_ref_*`) that carry domain logic. Those 61 first-party vtables are the only ones a reimplementer rebuilds; the other 140 are object code that should be linked from the real library at its pinned version ([SBOM](vendored-sbom.md)).

The second job of this page is to pin down the **vtable-slot measurement convention** that every other page in the book depends on when it cites a virtual call. A `_ZTV` symbol does **not** point at the first virtual function pointer — the Itanium C++ ABI prepends a two-word header (`offset-to-top`, then `typeinfo-pointer`), so the actual *vptr* installed into an object is `_ZTV + 0x10`. A disassembled `call *0xN(%rax)` reads slot `N/8` measured **from that vptr**, not from the `_ZTV` symbol. Confusing the two overcounts every slot by exactly 0x10 (two slots); the worked example below shows how `mem_ref::dump` lands at vt+0x0 while the `_ZTV` symbol sits 0x10 earlier. The page is organized as: the namespace census (the 201/244/245 split, re-derived per namespace); the four first-party class hierarchies as reloc-recovered trees (`mem_ref` 10-class, `dma_desc` 4-class, `enc_*` composers, `ntff::` 40-message); the canonical vtable shapes; and the slot-convention NOTE.

For reimplementation, the contract this page establishes is:

- **The provenance partition** — which 61 vtables are first-party (rebuild) and which 140 are vendored (link). The `ntff::` 40-message set is AWS-authored *schema* even though its `.pb.cc` accessor bodies are `protoc`-generated mechanics.
- **The inheritance shape** — single-inheritance-only (SI or root); a port that introduces multiple or virtual inheritance will not match libnrt's typeinfo records or its `__dynamic_cast` walk ([CRT/PLT](crt-plt.md)).
- **The vtable-slot convention** — vptr = `_ZTV + 0x10`; cite virtual-call slots from the vptr, never from the `_ZTV` symbol. This is the convention every dispatch citation in the book uses.

| | |
|---|---|
| **`_ZTV` vtables (defined)** | **201** (`nm -C --defined-only \| grep -c 'vtable for'`) |
| **`_ZTI` typeinfo (defined)** | **244** = 53 `__class_type_info` (root) + 191 `__si_class_type_info` (single-base) + **0** `__vmi_class_type_info` |
| **`_ZTS` typeinfo-names (defined)** | **245** (one orphan `_ZTS` without a `_ZTI`: an aliased name) |
| **VTT / thunks** | `_ZTT`/`_ZTc`/`_ZTh`/`_ZTv` = **0** (no virtual-table-tables, no covariant/thunk vtables) |
| **First-party vtables** | **61** = 40 `ntff::` (AWS schema) + 21 global-ns (`enc_*`/`alg_mesh_*`/`dma_desc_*`/`mem_ref_*`) |
| **Vendored vtables** | **140** = 97 protobuf + 16 Abseil + 13 std + 11 simdjson + 1 `pb::CppFeatures` + 1 `__gnu_cxx` + 1 `std::experimental::filesystem` |
| **First-party `_ZTV` band** | `0xbf6958` … `0xbf8dc8` (`.data.rel.ro`, contiguous) |
| **Slot convention** | **vptr = `_ZTV` + 0x10** (skips `offset-to-top` + `typeinfo*`); `call *0xN(%rax)` slot = `N/8` from vptr |
| **Dynamic exports** | **zero** RTTI exported (`nm -D` shows no `_ZT*`); all RTTI is LOCAL/hidden — internal only |

> **NOTE — two counts, two methods, one binary.** The headline 201/244/245 is the **defined** census: `nm --defined-only` over libnrt's own `.symtab`. The IDA sidecar (`…_rtti.json`) instead reports **247 `_ZTV` / 278 `_ZTI` / 245 `_ZTS`**, because it discovers typeinfo by *xref* and so also picks up libstdc++ vtables that libnrt only *references* against the dynamic `libstdc++.so.6` — e.g. `std::bad_alloc`, `std::basic_filebuf`, `std::regex_error` are imported, not defined here. The two `_ZTS` totals agree (245) because typeinfo-names are emitted locally even for imported types. When a page cites a *defined-in-libnrt* class, use the 201/244 census; the 247/278 figure is the xref superset including dynamic imports. [§ census reconciliation](#1-namespace-census).

---

## 1. Namespace census

Every polymorphic class in `libnrt.so` belongs to one of nine top-level namespaces. The census below is the provenance partition: it says, per namespace, how many vtables and typeinfos exist, whether the namespace is first-party or vendored, and which page owns the classes. Counts are re-derived by bucketing the demangled `_ZTV`/`_ZTI` symbol on its leading namespace component — **not** carried over from a stub. The `#vtables` column is the `nm --defined-only` count (the binary's own definitions); the role column names what the namespace is.

### 1.1 The defined census, by namespace

| Namespace | #vtables (`_ZTV`) | #typeinfo (`_ZTI`) | Role | Owning page | Confidence |
|---|---:|---:|---|---|---|
| `google::protobuf` | 97 | 113 | Vendored Protocol Buffers runtime (incl. 3 `MapEntry<>`); message/descriptor/IO interfaces | [SBOM](vendored-sbom.md) | HIGH |
| `ntff::` | **40** | **40** | **FIRST-PARTY** `.ntff` trace-file protobuf-message schema (AWS-authored, `protoc`-generated accessors) | [NTFF Format](../trace/ntff-format.md) | HIGH |
| first-party global ns | **21** | **24** (23 class + 1 ABI) | **FIRST-PARTY** `enc_*`/`alg_mesh_*`/`dma_desc_*`/`mem_ref_*` — collectives encoder + tensor/DMA model | [Engine Core](../collectives/engine-core.md), [KBIN Structs](../neff/kbin-structs.md) | HIGH |
| `absl::lts_20230802` | 16 | 19 | Vendored Abseil (`log_internal`/`cord`/`crc`/`cctz`) | [SBOM](vendored-sbom.md) | HIGH |
| `std::` | 13 | 31 | Vendored libstdc++ (defined subset; iostream/exception bases) | [SBOM](vendored-sbom.md) | HIGH |
| `simdjson::` | 11 | 11 | Vendored simdjson (per-µarch `dom_parser_implementation` + `simdjson_error`) | [SBOM](vendored-sbom.md) | HIGH |
| `pb::CppFeatures` | 1 | 1 | Vendored protobuf feature-set message | [SBOM](vendored-sbom.md) | HIGH |
| `std::experimental::filesystem::v1` | 1 | 1 | Vendored libstdc++ filesystem | [SBOM](vendored-sbom.md) | HIGH |
| `__gnu_cxx::` | 1 | 1 | Vendored libstdc++ support | [SBOM](vendored-sbom.md) | HIGH |
| first-party lambda closures | 0 | **4** | **FIRST-PARTY** compiler-synthesized closure typeinfos (no own vtable) — see §1.3 | [Engine Core](../collectives/engine-core.md) | HIGH |
| **Total (defined)** | **201** | **244** | | | HIGH |

The `_ZTI` column runs ahead of `_ZTV` in several namespaces because typeinfo exists for **abstract bases that have no instantiable vtable** (`enc_ins`, `dma_desc`, `enc_proxy_task` carry `_ZTI` but their concrete derived classes carry the reified `_ZTV`) and for the 4 lambda closures (§1.3), which have typeinfo but no vtable.

> **CORRECTION (F-RTTI census) —** the seed map's per-namespace `_ZTI` line read "27 first-party global (24 class + 3 lambda)". Re-bucketing the defined `_ZTI` set strictly on the namespace component gives **24 global-ns typeinfos** (23 genuine first-party classes + `__cxxabiv1::__forced_unwind`, which is an ABI-runtime type, not first-party) plus **4** first-party *lambda* closures that demangle under the `Z…` local scope, not the global namespace. So the correct split is **23 first-party global classes + 1 ABI type + 4 first-party lambdas = 28 records touching first-party code**, and the lambda count is 4, not 3. The **totals 201/244/245 are unchanged**; only the first-party sub-bucketing is corrected. The fourth lambda is `enc_mesh_primitive::__compose_alltoall_full_mesh()::{lambda(int)#1}` (§1.3).

### 1.2 RTTI kind — the inheritance shape

The `_ZTI+0` word of every typeinfo is a relocation to one of the three `__cxxabiv1` type_info vtables; reading that reloc (`readelf -rW` at `_ZTI+0`) classifies each typeinfo's inheritance kind. The split is decisive:

| RTTI kind | Count | What it means | Confidence |
|---|---:|---|---|
| `__class_type_info` | 53 | Root — no polymorphic base | HIGH |
| `__si_class_type_info` | 191 | Single inheritance — exactly one polymorphic base, at `_ZTI+16` | HIGH |
| `__vmi_class_type_info` | **0** | Multiple / virtual inheritance | HIGH |
| **Total** | **244** | = `_ZTI` defined count | HIGH |

The absence of any `__vmi_class_type_info` is a hard structural fact: **libnrt has no multiple inheritance and no virtual bases**. Every polymorphic class is a root or a single-base SI node, so the entire type graph is a forest of trees with no diamonds. The single base edge of each SI node lives at `_ZTI+16` as an `R_X86_64_RELATIVE` relocation; **183 of the 191 SI base edges resolve to a `_ZTI` defined in this `.symtab`**, and the remaining 8 point at libstdc++-external typeinfos (std/abi parents) not present here — immaterial to the first-party trees (LOW-confidence on those 8 only because the parent symbol is off-binary).

### 1.3 The four first-party lambda closures

Four `_ZTI` records under local (`Z…`) scope are compiler-synthesized closure typeinfos with **no vtable** — they exist because a lambda was passed somewhere that captured its type (e.g. into a `std::function` or an OFI callback). All four are first-party collectives code:

```text
async_sr_progress_connect_send_comm(async_sr_context*, nrt_async_sendrecv_comm*)::{lambda(unsigned char*, int)#1}
async_sr_progress_connect_recv_comm(async_sr_context*, nrt_async_sendrecv_comm*)::{lambda(unsigned char*, int)#1}
enc_mesh_primitive::__compose_alltoallv_full_mesh()::{lambda(int)#1}
enc_mesh_primitive::__compose_alltoall_full_mesh()::{lambda(int)#1}
```

The two `async_sr_*` lambdas are the async send/recv progress callbacks of the rendezvous P2P path; the two `enc_mesh_primitive::__compose_*` lambdas are per-rank index generators inside the all-to-all mesh composer ([Engine Core](../collectives/engine-core.md)). They carry typeinfo but never a vtable because a closure is not polymorphic — RTTI is emitted only so the type can be named in an exception-spec or a `std::function` target-type query.

---

## 2. First-party class hierarchies

The 61 first-party vtables form five small trees, all single-inheritance, all in the contiguous `_ZTV` band `0xbf6958`…`0xbf8dc8` in `.data.rel.ro`. Each tree below is reconstructed from the `_ZTI+16` base-edge relocations (the SI parent pointer); the trees are HIGH confidence — every edge is a verbatim reloc to a named `_ZTI` in this binary.

### 2.1 `mem_ref` — the 10-class tensor/DMA reference tree

`mem_ref` is the runtime's tensor-reference model: a polymorphic, instantiable root (208-byte struct) with nine SI subclasses, one of which (`mem_ref_ptr_table`) is itself a 2-level SI child of `mem_ref_list`. This is the deepest first-party tree.

```text
mem_ref                          @_ZTV 0xbf8c60   (root, __class_type_info, instantiable, 208 B)
 ├─ mem_ref_sp                   @0xbf8c88   si:mem_ref
 ├─ mem_ref_io                   @0xbf8cb0   si:mem_ref   (+72 size, +80 alignment members)
 ├─ mem_ref_buffer               @0xbf8cd8   si:mem_ref   (+88 buffer*, +96 debug_tensor_md)
 ├─ mem_ref_tmp_buf              @0xbf8d00   si:mem_ref
 ├─ mem_ref_virtual_tmp_buf      @0xbf8d28   si:mem_ref
 ├─ mem_ref_pointer              @0xbf8d50   si:mem_ref
 ├─ mem_ref_list                 @0xbf8d78   si:mem_ref
 │    └─ mem_ref_ptr_table       @0xbf8da0   si:mem_ref_list   (2-level chain; inherits list::dump)
 └─ mem_ref_remote_variable      @0xbf8dc8   si:mem_ref
```

Every node shares a **3-slot vtable** shape (`dump`, `~T` complete, `~T` deleting); the subclasses differ only by overriding slot 0 (`dump`) and supplying their own destructors. The tensor/DMA-memory model these reference is owned by [Memory Planning](../neff/memory-planning.md) (the `mem_ref` datastore) and [TDRV DMEM](../runtime/tdrv-dmem.md); `mem_ref::dump` emits the on-disk `kbin_mem_ref` struct ([KBIN Structs](../neff/kbin-structs.md)).

### 2.2 `dma_desc` — the 4-class DMA-descriptor tree

`dma_desc` is an **abstract** root (8-byte struct = vptr only, no instantiable `_ZTV`) with three concrete SI descriptor kinds. Each concrete class is a 4-slot vtable.

```text
dma_desc                         (abstract root; _ZTI 0x9f5f10 _ZTS "8dma_desc"; no own _ZTV)
 ├─ dma_desc_data                @_ZTV 0xbf8bd0   si:dma_desc   (416-byte DMA-tiling descriptor)
 ├─ dma_desc_event               @0xbf8c00        si:dma_desc
 └─ dma_desc_inc_semaphore       @0xbf8c30        si:dma_desc
```

All three share the 4-slot shape `[dump, ~T(complete), ~T(deleting), mark_block_end_desc]`; the per-block DMA descriptor layout (`dma_desc_data`'s 416-byte tiling/transpose struct) is owned by [KBIN Structs](../neff/kbin-structs.md).

### 2.3 `enc_*` / `alg_mesh_*` — the collectives-encoder composer classes

The collectives encoder splits into four small abstract-root trees. `enc_ins`, `enc_proxy_task`, and `alg_mesh_initializer` are abstract bases (the first two carry `_ZTI` but no reified `_ZTV`; `alg_mesh_initializer` has a stub `_ZTV` with zero virtual functions). The concrete classes carry the reified interface.

```text
enc_ins                          (abstract base; _ZTI 0x857cd8 _ZTS "7enc_ins"; no own _ZTV)
 ├─ enc_op_list                  @_ZTV 0xbf6998   si:enc_ins   (7-slot enc_ins interface)
 └─ enc_fnc                      @0xbf69d8        si:enc_ins   (7-slot)

enc_proxy_task                   @_ZTV 0xbf6980   (abstract; 2 null slots = pure-virtual step())
 ├─ enc_network_proxy_task       @0xbf6b18        si:enc_proxy_task   (1-slot step())
 └─ enc_barrier_proxy_task       @0xbf6b30        si:enc_proxy_task   (1-slot step())

alg_mesh_initializer             @_ZTV 0xbf6958   (stub _ZTV, 0 vfns; _ZTS "20alg_mesh_initializer")
 ├─ alg_mesh_initializer_pd      @0xbf6a18        si   (3-slot: initialize, ~T, ~T)
 └─ alg_mesh_initializer_switch  @0xbf6b98        si   (3-slot + embedded platform_memhandle_* op block)
```

These are the host-side collective-op object model: `enc_op_list`/`enc_fnc` build the instruction stream, the `enc_proxy_task` pair drives the proxy-task command pattern for collective scheduling, and `alg_mesh_initializer{_pd,_switch}` select point-to-point versus switch-fabric mesh topology. All owned by [Engine Core](../collectives/engine-core.md).

> **NOTE — `alg_mesh_initializer_switch`'s trailing op-block is not classic vfns (MED).** Past its 3 standard slots, `alg_mesh_initializer_switch` @`0xbf6b98` carries a secondary pointer block ending in five `platform_memhandle_*` functions (`device_malloc`, `device_free`, `read_memhandle`, `write_memhandle`, `get_memhandle_soc_addr`). The reloc-walker resolves those targets but cannot prove they are vtable slots versus an inline ops-struct embedded after the vtable; treat the exact virtual-fn count of this class as MED, and the five `platform_memhandle_*` entries as an embedded ops table, not classic virtuals.

### 2.4 `ntff::` — the 40 trace-message classes

All 40 `ntff::` classes are SI children of `google::protobuf::Message` (itself `si:MessageLite`), occupying the `_ZTV` band `0xbf6fa8`…`0xbf8708`. Two of the 40 are protobuf map entries whose base is `protobuf::internal::MapEntry<…>` rather than `Message` directly:

```text
google::protobuf::MessageLite    (abstract root)
 └─ google::protobuf::Message    (si:MessageLite)
     ├─ ntff::version_info, ntff::ntff_info, ntff::trace_info, …   (38 direct message classes)
     ├─ ntff::ntrace_event_AttributesEntry_DoNotUse
     │     └─ protobuf::internal::MapEntry<…, string, string, TYPE_STRING, TYPE_STRING>   (si)
     └─ ntff::interned_data_db_IdMapEntry_DoNotUse
           └─ protobuf::internal::MapEntry<…, uint64, string, TYPE_UINT64, TYPE_STRING>   (si)
```

The 40 classes are the binary wire-schema of the `.ntff` runtime-trace file: `version_info`, `ntff_info`, `execution_info`, `cpu_node_info`, `nd_node_info`, `neff_node_info`, `engine_instruction_info`, `trace_info`, `block_timestamp_info`, `nc_timestamp_info`, `nc_memory_usage`, the `collectives_*` family (15 messages: `stream_info`, `comm_info`, `op`, `op_trace`, `op_info`, `semaphore`, `semaphore_update`, `net_idx_update`, `dma_packets`, `subchannel`, `channel`, `info`, …), `subgraph_info`, `instruction_patch_{location,info}`, `desc_block_info`, `dma_queue_usage_info`, `ntrace_{event,info,data_file}`, `interned_data_db`, `cpu_util`, `host_{mem_usage,stats}`, plus the two `…_DoNotUse` map entries. The full field-level schema (wire numbers, types) is owned by [NTFF Format](../trace/ntff-format.md) — this page records only the class set and base edges.

> **GOTCHA — the `ntff::` accessors are mechanical, the schema is first-party.** The `.pb.cc` bodies (the `_InternalParse`/`_InternalSerialize`/`GetMetadata` slot functions) are `protoc` output a reimplementer regenerates, not reverse-engineers. But the 40-class *set* and their field layout are AWS-authored and reimplementation-relevant. Treat the vtable bodies as generated and the message roster as a first-party artifact.

---

## 3. Canonical vtable shapes

Three vtable shapes recur across the first-party trees; the fourth is the protobuf-Message ABI shared by all 40 `ntff::` classes. Slots are listed **from the vptr** (`_ZTV + 0x10`), per the convention in §4. All shapes are reloc-walked from `.data.rel.ro` (HIGH).

| Class family | `_ZTV` | Slots | Slot layout (from vptr) | Confidence |
|---|---|---:|---|---|
| `mem_ref` + 9 derived | `0xbf8c60`… | 3 | `[0] dump(kbin_mem_ref&)` · `[1] ~T` (complete/D2) · `[2] ~T` (deleting/D0) | HIGH |
| `dma_desc_data`/`_event`/`_inc_semaphore` | `0xbf8bd0`… | 4 | `[0] dump(kbin_dma_desc&)` · `[1] ~T` (D2) · `[2] ~T` (D0) · `[3] mark_block_end_desc()` | HIGH |
| `enc_op_list` / `enc_fnc` | `0xbf6998`/`0xbf69d8` | 7 | `[0] post_instr()` · `[1] update_param_for_signature(…)` · `[2] …_src_tgt_pairs(…)` · `[3] validate_and_merge_ccops(…)` · `[4] ~T` (D2) · `[5] ~T` (D0) · `[6] null terminator` | HIGH |
| `ntff::*` (protobuf::Message ABI) | `0xbf6fa8`… | 11 | `[0] ~T` (D2) · `[1] ~T` (D0) · `[2] New(Arena*)` · `[3] Clear()` · `[4] IsInitialized()` · `[5] CheckTypeAndMergeFrom(MessageLite&)` · `[6] ByteSizeLong()` · `[7] _InternalParse(…)` · `[8] GetClassData()` · `[9] _InternalSerialize(…)` · `[10] GetMetadata()` | HIGH |

For the proxy-task pair, `enc_network_proxy_task` (`0xbf6b18`) and `enc_barrier_proxy_task` (`0xbf6b30`) carry a single virtual at slot 0 (`step()`); their abstract base `enc_proxy_task` (`0xbf6980`) has two null slots (pure-virtual). `mem_ref_ptr_table` (`0xbf8da0`) does **not** override slot 0 — its slot 0 is `mem_ref_list::dump` @`0x4c4f80` inherited unchanged, the one case in the `mem_ref` tree where a subclass reuses its parent's `dump`.

---

## 4. The vptr = _ZTV + 0x10 slot convention

This is the convention every page that cites a virtual call depends on. State it once, here; other pages reference it.

> **NOTE — vptr = `_ZTV` + 0x10; cite slots from the vptr, never from the `_ZTV` symbol.** Under the Itanium C++ ABI, a `_ZTV` symbol points at the **start of the vtable structure**, which begins with a two-word header — `offset-to-top` (8 B, normally `0`) then a pointer to the class `_ZTI` (8 B) — *before* the first virtual-function pointer. The value the constructor stores into an object's vptr field is therefore `_ZTV + 0x10`, the address of slot 0. So when a disassembled virtual call reads `call *0xN(%rax)` (where `%rax` holds the loaded vptr), the slot index is `N / 8` measured **from the vptr**, i.e. from `_ZTV + 0x10`. Measuring `N` from the `_ZTV` *symbol* instead overcounts by 0x10 — two slots — because it counts the header words as if they were virtual functions.
>
> **Worked example — `mem_ref::dump`.** `mem_ref`'s vtable symbol `_ZTV7mem_ref` is at `0xbf8c60`. Its header occupies `0xbf8c60` (offset-to-top = 0) and `0xbf8c68` (→ `_ZTI7mem_ref`). The vptr installed into a `mem_ref` object is `0xbf8c70` = `0xbf8c60 + 0x10`. Slot 0 — `mem_ref::dump` — lives at `*(0xbf8c70 + 0x0)`; a call site dispatches it as `call *(%rax)` with `%rax = 0xbf8c70`. A reader who (wrongly) measured from the `0xbf8c60` symbol would compute `dump` at `+0x10` and mislabel the two header words as slots `-2`/`-1`. Always anchor the slot to the vptr; the `_ZTV` symbol is the header, two words earlier.

> **CORRECTION (F-RTTI slot anchoring) —** any citation of a first-party slot in the form "`_ZTV…+0xN`" must subtract `0x10` before dividing by 8 to recover the slot index. Reloc-walkers that emit slot offsets relative to the `_ZTV` symbol (rather than the vptr) **overcount every slot by 0x10**; the slot tables in §3 are stated from the vptr (slot 0 = first vfn) and are the canonical form. This convention also governs the `__dynamic_cast` path: because all 244 typeinfos are root or SI ([§1.2](#12-rtti-kind--the-inheritance-shape)), a cross-cast is a single `__si_class_type_info` parent-chain walk, never a `__vmi` offset table.

---

## 5. Vendored vtables — recognize and skip

The 140 vendored vtables are object code; a reimplementer links the real library rather than rebuilding them. They are catalogued here only so a reader can recognize-and-skip them when walking `.data.rel.ro`.

| Namespace | #vtables | What to recognize | Treatment |
|---|---:|---|---|
| `google::protobuf` | 97 | 18 root abstract interfaces (`MessageLite`, `Message`, `DescriptorDatabase`, `MessageFactory`, `Closure`, the `io::*Stream` family, `TextFormat::*`) + generated/templated message and `internal::*` accessors; 3 `MapEntry<>` | Link protobuf 5.26.1 ([SBOM](vendored-sbom.md)) |
| `absl::lts_20230802` | 16 | `log_internal::*`, `LogSink`, `BadStatusOrAccess`, `cord_internal::Cordz*`, `crc_internal::CRC*`, `cctz::TimeZone*`/`ZoneInfoSource` | Link Abseil LTS 20230802 |
| `std::` | 13 | iostream/exception bases (the *defined* subset; the imported-only set is larger — see census NOTE) | Link libstdc++ |
| `simdjson::` | 11 | per-ISA `implementation`/`dom_parser_implementation` (fallback/haswell/westmere/…) + `simdjson_error` | Link simdjson 0.9.0 |
| `pb::CppFeatures` · `std::experimental::filesystem::v1` · `__gnu_cxx::` | 1 each | feature-set message · filesystem `_Dir` · `__gnu_cxx` support | Link the respective runtime |

> **QUIRK — RTTI is internal-only; nothing is exported.** `nm -D libnrt.so` lists **zero** `_ZT*` symbols — every vtable, typeinfo, and typeinfo-name is LOCAL/hidden binding. libnrt exposes only its C `nrt_*`/`NRT_*` API ([ELF Anatomy §2](elf-anatomy.md)); the entire C++ type system is an implementation detail behind that façade. A consumer cannot `dynamic_cast` a libnrt object or query its typeinfo across the ABI boundary — the RTTI exists purely for libnrt's *own* internal `__dynamic_cast`, exception-matching, and `~T` deleting-destructor dispatch.

---

## Related Components

| Component | Relationship |
|---|---|
| `__dynamic_cast` / `__si_class_type_info` | the RTTI down/cross-cast path; SI-only graph means a single parent-chain walk ([CRT/PLT](crt-plt.md)) |
| `kaena_khal` / `tdrv_arch_ops` | per-arch C **function-pointer** dispatch tables — **not** C++ RTTI; counted by [Dispatch Tables](dispatch-tables.md), absent from the `_ZTV` set |
| `.init_array` constructors | the 77 ctors that register the protobuf descriptor pools backing the `ntff::` message types ([Static-Init](static-init.md)) |
| `ntff::` `.pb.cc` accessors | `protoc`-generated bodies of the 40-message vtables; regenerate from the recovered schema ([NTFF Format](../trace/ntff-format.md)) |

## Cross-References

- [CRT / PLT / Loader Surface](crt-plt.md) — the `__dynamic_cast`/`__cxa_*` CXXABI imports that consume this RTTI, and why a SI-only graph is a single-chain cast
- [Dispatch-Table Taxonomy](dispatch-tables.md) — the per-arch C function-pointer vtables (`kaena_khal`, `tdrv_arch_ops`) that are *not* in the `_ZTV` set, to keep the two dispatch mechanisms distinct
- [NTFF Trace Format](../trace/ntff-format.md) — the field-level schema of the 40 `ntff::` message classes whose class set and base edges this page enumerates
- [Collectives Engine Core](../collectives/engine-core.md) — the `enc_*`/`alg_mesh_*` composer object model and the 4 first-party lambda closures
- [KBIN Structs](../neff/kbin-structs.md) — the `kbin_mem_ref`/`kbin_dma_desc` on-disk structs that `mem_ref::dump`/`dma_desc_data::dump` emit
- [Overview and Heavy-Frame Census](overview.md) — where the 61-vs-140 first-party/vendored vtable split anchors the provenance partition
- [Vendored-Library SBOM](vendored-sbom.md) — the pinned versions of the 140 vendored vtables (protobuf/Abseil/simdjson/libstdc++)
