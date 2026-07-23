# ntff Trace Protobuf + simdjson NEFF Parse State

This page reconstructs three host-runtime data planes that bracket a GPSIMD (Vision-Q7
"POOL" engine) execution: (a) the **NTFF trace protobuf** wire schema — every message,
every field *number*, recovered from each message's `_InternalSerialize` body (a
wire-format reconstruction, **not** a `.proto` file); (b) the **device-notification-type →
`ntff::(trace_type, block_type)` jump table** that classifies a raw device notification
into a trace record; (c) the **simdjson NEFF `def.json` parse-state** structs the
version/section parser walks; and (d) the **kbin loadable-library function table**
byte layout the custom-op (Q7 ucode) loader consumes.

All offsets, field numbers, enum values, and addresses below are read out of
`libnrt.so.2.31.24.0` (neuronx-runtime `2.31.24.0-0b044f4ce`). The protobuf runtime is
statically linked: a **table-driven `google::protobuf::internal::TcParser`** (3.21-era).
Section layout for address arithmetic: `.text` (VMA `0x3dbc0` == file offset),
`.rodata` (VMA `0x7cf000` == file offset), and for this binary **`.data` VMA `0xc07e00`
== file offset** (`readelf -SW`; the `_table_` TcParseTables live there) — no
VMA/file-offset delta on any section of interest here.

> Cross-refs: NEFF container framing in [container-byte-format.md](./container-byte-format.md);
> the simdjson parse-state seen from the version/section side in
> [version-compat.md](./version-compat.md); the public entry points in
> [../runtime/public-api-table.md](../runtime/public-api-table.md). The pending device
> notification *producer* lives in `control/interrupt/device-host-notification.md`, the
> host profiling→ntff gating in `control/security/profiling-trace-debug-gating.md`, and
> the full host-struct census in `appendix/struct-host-runtime-layouts.md`.

---

## 1. The field-number Rosetta (how field numbers were recovered)  `[HIGH]`

protobuf does **not** store field numbers as data in the message struct; they only appear
as wire **tags** inside the generated (de)serializers. The recovery method, applied to
every `ntff::<msg>::_InternalSerialize(unsigned char*, EpsCopyOutputStream*)`:

| signal (in the serialize body) | meaning | confidence |
|---|---|---|
| `movb $0xNN,(%rbx)` immediately before writing a scalar value | tag `0xNN` → field = `0xNN >> 3`, wire-type = `0xNN & 7` | `[OBS]` |
| `mov $0xF,%edi ; call WireFormatLite::InternalWriteMessage` | sub-message field = `F` (first int arg) | `[OBS]` |
| sorted `MapSorterPtr<...>` loop emitting entries under one tag | the **map** field's tag | `[OBS]` |

The message **object** is `vptr(+0) · google::protobuf::Arena*(+8) · Impl_(+0x10)`. A value
read at object offset `O` in the serializer therefore lands at `Impl_` offset `O − 0x10`;
matching that `Impl_` offset to the member name in `structures.json` pins **field number →
member** unambiguously. This member-cross-check is what produced the corrections in §2.1.

> **GOTCHA** — the table-driven *parse* side carries no per-field `cmp $tag` in code.
> `ntff::ntrace_event::_InternalParse` (`0x48ba40`) is a 12-byte stub that tail-jumps to
> `google::protobuf::internal::TcParser::ParseLoop` (`0x6a4b00`) with a `TcParseTableBase`
> (`ntff::ntrace_event::_table_` @ `0xc0be80`, `.data`). Field numbers must be read from
> the *serialize* side (above); the parse table's binary packing is deferred. The presence
> of `TcParser::ParseLoop`, `ReadStringNoArena`, and the `TcParser::PackedEnum<u8,(u16)1024>`
> template clones (253 `TcParser` symbols, `nm -C | rg -c TcParser`) fixes the runtime as
> protobuf ≥ 3.21 table-driven. `[OBS HIGH]`

---

## 2. NTFF trace file format — message schema  `[HIGH/OBS]`

NTFF is one protobuf wire file carrying two arena-allocated roots:

- **`ntff::ntff_info`** — the NEFF-side static profile descriptor ("what will run").
- **`ntff::ntrace_info`** — the runtime event stream ("what happened").

There are **38** message families (`nm -C libnrt.so | rg -c 'ntff::.*::_InternalSerialize'`
= 38, exactly matching 38 `ntff::*::Impl_` structs in `structures.json`). Every `Impl_`
begins with `_has_bits_(HasBits<1>) @+0` and `_cached_size_ @+4`; scalar fields follow.

### 2.1 `ntff::ntrace_event::Impl_` (152 B) — the core stream record

Serializer `ntff::ntrace_event::_InternalSerialize` @ **`0x48e4b0`** (`end 0x48ef6c`).
The struct layout (`structures.json`, `ntff::ntrace_event::Impl_` size=152) plus the
serializer's value-offset reads give this **binary-authoritative** field map:

| field | tag literal (`movb`) | wire-type | member (Impl_ offset / obj offset) | C++ type |
|---|---|---|---|---|
| 1  | `0x08` | varint | `timestamp_`   (`+0x40` / obj `0x50`) | `uint64` |
| 2  | `0x10` | varint | `data_`        (`+0x50` / obj `0x60`) | `uint64` |
| 3  | `0x18` | varint | `nd_idx_`      (`+0x4c` / obj `0x5c`) | `uint32` |
| 4  | `0x20` | varint | `nc_idx_`      (`+0x58` / obj `0x68`) | `uint32` |
| 5  | `0x28` | varint | `event_type_`  (`+0x5c` / obj `0x6c`) | `uint32` |
| 6  | `0x32` | len-delim | `hint_`      (`+0x38` / obj `0x48`) | `ArenaStringPtr` |
| 7  | `0x38` | varint | `duration_`    (`+0x60` / obj `0x70`) | `uint64` |
| 8  | `0x40` | varint | `thread_id_`   (`+0x78` / obj `0x88`) | `uint32` |
| 9  | `0x48` | varint | `id_`          (`+0x68` / obj `0x78`) | `uint64` |
| 10 | `0x50` | varint | `parent_id_`   (`+0x70` / obj `0x80`) | `uint64` |
| 11 | `0x58` | varint | `status_`      (`+0x7c` / obj `0x8c`) | `uint32` |
| 12 | `0x62` | len-delim | `attributes_` (`+0x08` / obj `0x18`) | `MapField<string,string>` |
| 13 | `0x68` | varint | `phase_`       (`+0x90` / obj `0xa0`) | `int` (enum, §2.7) |
| 14 | `0x70` | varint | `tracking_id_` (`+0x80` / obj `0x90`) | `uint64` |
| 15 | `0x78` | varint | `nrta_seq_id_` (`+0x88` / obj `0x98`) | `uint64` |

Serialize evidence (`objdump -d --start/--stop` over `0x48e4b0..0x48ef6c`):

```text
48e4e0 movb $0x8 ,(%rbx) ; 48e4ef mov 0x50(%rbp),%rax   ; f1 timestamp_  @obj0x50
48e50a movb $0x10,(%rbx) ; 48e519 mov 0x60(%rbp),%rax   ; f2 data_       @obj0x60
48e543 mov 0x5c(%rbp),%eax → tag 0x18                    ; f3 nd_idx_     @obj0x5c
48e56a mov 0x68(%rbp),%eax → tag 0x20                    ; f4 nc_idx_     @obj0x68
48e5a6 mov 0x6c(%rbp),%eax → tag 0x28                    ; f5 event_type_ @obj0x6c
48e6b9 movslq 0xa0(%rbp),%rax ; 48e6aa movb $0x68,(%rbx) ; f13 phase_(int)@obj0xa0  <- VARINT
48e7c3 movb $0x62,(%rbx)  (MapSorterPtr<string,string>)  ; f12 attributes_ map
48eacc movb $0x32,(%rbx)  (single memcpy of ArenaString) ; f6  hint_
```

> **CORRECTION.** An earlier field↔member assignment for `ntrace_event` was
> scrambled relative to the binary.
> The binary (serialize value-offset → `Impl_` member name) is authoritative and gives the
> table above. Concretely: **f2 = `data_`** (not `type_`); **f4 = `nc_idx_`**, **f5 =
> `event_type_`**, **f7 = `duration_`**, **f8 = `thread_id_`**, **f11 = `status_`**;
> **f6 = `hint_`** (string, tag `0x32`) and **f12 = `attributes_`** (the `string→string`
> map, tag `0x62`) — i.e. an earlier reading's f6/f12 are *swapped*; and **f13 = `phase_`** read
> as a sign-extended `int` **varint** (`movslq 0xa0(%rbp)`, tag `0x68`), **not** the
> length-delimited f12 an earlier reading posited. `type_` is **not** serialized by this method in
> this build (no tag references obj `0x58`). The `attributes_` map entry uses the standard
> `MapEntry` inner tags `0x0a` (key=f1 string) + `0x12` (value=f2 string), wrapped by the
> outer `0x62`. `[OBS HIGH — serialize bytes + struct member offsets]`

### 2.2 Device-instruction & DMA records (the POOL/GPSIMD anchors)  `[OBS]`

`ntff::engine_instruction_info::_InternalSerialize` @ **`0x483db0`** (`Impl_` 56 B). Tags:

| field | tag | member | note |
|---|---|---|---|
| 1 | `0x08` | `start_address_` (u64) | |
| 2 | `0x10` | `nc_engine_type_` (int) | **POOL = 2 ⇒ a GPSIMD/Q7 instruction block** |
| 3 | `0x1a` | `instructions_` (string) | raw Q7 bundle bytes → device-ISA disassembler |
| 4 | `0x20` | `engine_index_` (u32) | |
| 5 | `0x28` | `block_type_` (int) | NC=0 / DMA=1 / TOPSP=2 (§2.7) |
| 6 | `0x30` | `function_section_` (bool) | |
| 7 | `0x38` | `instructions_offset_` (u64) | |
| 8 | `0x40` | `instructions_size_` (u64) | |

`ntff::dma_queue_usage_info::_InternalSerialize` @ `0x488150`: f1 `engine_index_`(`0x08`),
f2 `queue_index_`(`0x10`), f3 `queue_type_`(`0x18`, `ntff::dma_queue_type` §2.7),
f4 `name_`(`0x22`), f5 `desc_block_info_` (rep, `InternalWriteMessage` field 5),
f6 `dma_tracing_event_id_`(`0x30`), f7 `axi_port_`(`0x38`), f8 `channel_` (sub-msg field 8).

`ntff::desc_block_info::_InternalSerialize` @ `0x4838d0` (`Impl_` 32 B): f1 `block_id_`(`0x08`),
f2 `packet_count_`(`0x10`), f3 `m2s_desc_count_`(`0x18`), f4 `s2m_desc_count_`(`0x20`),
f5 `function_name_`(`0x2a`). `block_id_`/`function_name_` join to the kbin DMA descriptor
block (see [container-byte-format.md](./container-byte-format.md)).

### 2.3 Collectives (`enc_*`) sub-tree — Cairo network packets  `[OBS]`

The on-wire image of the device collectives engine, the GPSIMD-relevant network plane:

| message (`_InternalSerialize` addr) | fields (tag) |
|---|---|
| `collectives_info` (`0x47f860`) | f1 `channels_` (rep), f2 `topsp_` (rep `engine_instruction_info`) |
| `collectives_channel` (`0x4833a0`) | f1 `name_`, f3 `subchannels_` (rep), f4 `engine_index_` |
| `collectives_subchannel` (`0x47f9f0`) | f1 `index_`(`0x08`), f2 `dma_`(msg), f3 `semaphore_`(rep), f4 `packets_`(rep) |
| `collectives_dma_packets` (`0x47f0a0`) | f1 `type_`(`0x08`), f2 `size_`(`0x10`), f3 `neigh_`(`0x18`), f6 `op_idx_`(`0x30`), f7 `stream_idx_`(`0x38`) + **oneof** §2.4 |
| `collectives_op` (`0x487f20`) | f1 `type_`, f2 `algorithm_`, f3 `num_elements_`(`0x18`), f4 `comm_id_`(`0x20`), f5 `data_type_`(`0x28`), f6 `algorithm_name_`(`0x32`) |
| `collectives_comm_info` (`0x481f30`) | f1..f6 inline varints (`0x08..0x30`): `id_/rank_/rank_n_/node_n_/local_rank_n_/stream_id_` |
| `collectives_stream_info` (`0x47f2e0`) | f1 `cc_core_id_` (packed rep u64), f2 `id_`(`0x12` len-delim) |

`collectives_dma_packets` tags read out byte-exact at `0x47f0c8/ef/118/153/17a`
(`movb $0x8/$0x10/$0x18/$0x30/$0x38`).

### 2.4 The two oneof unions — exact storage  `[OBS clear_*/set_allocated_*]`

A protobuf oneof = `{ union (1 ptr slot) , _oneof_case_ (u32) }`; the active arm's
**discriminant value equals its protobuf field number** in both ntff oneofs (case 0 = unset).

**(A) `collectives_dma_packets::PacketTypeDataUnion`** — slot @ `Impl_+0x18` (obj `0x28`),
`_oneof_case_` @ `Impl_+0x24`. Arms, confirmed by `InternalWriteMessage($field,%edi)`:

```text
47f1a5 mov $0x5,%edi ; call InternalWriteMessage   ; case 5 = net_idx_update (collectives_net_idx_update*)
47f1bb mov $0x4,%edi ; call InternalWriteMessage   ; case 4 = sema_update    (collectives_semaphore_update*)
```

**(B) `neff_node_info::NodeInfoUnion`** — slot @ obj `0x28`, `_oneof_case_` @ **obj `0x34`**
(`cmp $0x3,%eax` / `cmp $0x4,%eax` / `cmpl $0x6,0x34(%rbx)` in `neff_node_info::_InternalSerialize`
@ `0x487dc0`). Arms:

```text
487e3b mov $0x4,%edi ; InternalWriteMessage   ; case 4 = nd_node  (nd_node_info*)    NeuronDevice node
487e51 mov $0x3,%edi ; InternalWriteMessage   ; case 3 = cpu_node (cpu_node_info*)   CPU node
487eb8 mov $0x6,%edi ; InternalWriteMessage   ; case 6 = outcc    (outcc_info*)      collectives node
```

`neff_node_info` also emits a fixed64 `runtime_latency_` at tag `0x11` (`movb $0x11` →
field 2, wire-type 1, `double`) and a length-delimited field 5 (tag `0x2a`).

### 2.5 Container nesting (who-holds-whom)  `[OBS structures.json]`

```text
ntff_info (216B Impl_)
  +-- neff_nodes_           rep<neff_node_info>            per-NeuronDevice node
  +-- execution_timeline_   rep<execution_info>
  +-- session_notifications_ rep<trace_info>
  +-- id_/arch_/profile_start_time_/ncfw_notification_version_/start_nc_/pid_/session_id_

neff_node_info (Impl_ ONEOF) -> cpu_node(3) | nd_node(4) | outcc(6)
  outcc_info (112B): comm_info_(+0x08) stream_info_(+0x20) collectives_dma_info_(+0x38)
                     participants_ packed u32(+0x50) cc_op_info_(+0x68)
  nd_node_info (32B): graphs_ rep<subgraph_info>          per-NeuronCore graphs

subgraph_info (280B) - the per-NeuronCore execution graph; GPSIMD POOL trace lives here:
  +0x08 instruction_info_ rep<engine_instruction_info>   <- nc_engine_type_==POOL(2) => Q7
  +0x38 traces_           rep<trace_info>
  +0x80 collectives_dma_info_ rep<collectives_channel>
  +0x98 collectives_instruction_info_ rep<engine_instruction_info>
  +0xe8 coll_info_(collectives_info*) +0xf0 timestamp_info_ +0xf8 cc_op_info_

ntrace_info (184B) - runtime event stream root:
  +0x08 events_             rep<ntrace_event>            <- the time-ordered stream
  +0x20 collected_profiles_ rep<ntrace_data_file>
  +0x38 interned_data_      rep<interned_data_db>        string-pool dedup (hint_/phase_)
  +0x88 arch_ +0x8c pid_ +0x90 session_id_ +0x94 data_version_ +0xb0 logical_nc_size_
```

`nc_timestamp_info::_InternalSerialize` @ `0x47f680` carries 9 per-engine sub-messages;
**f4 = `tpb_pool_`** is the GPSIMD/Q7 timestamp channel (f1 `tpb_pe_` … f5 `tpb_sp_`,
f8 `dma_` rep, f9 `top_sp_`), each a `block_timestamp_info*`
(`block_timestamp_info` @ `0x481de0`: f1 `init_timestamp_`, f2 `actual_timestamp_inc_`,
f3 `expected_timestamp_inc_`).

### 2.6 Arena allocation  `[OBS C2(Arena) decompile]`

Every ntff message is arena-allocated; on-arena layout is `vptr(+0)·Arena*(+8)·Impl_(+0x10)`.
The `C2(Arena*)` ctor (e.g. `ntff::ntrace_event::ntrace_event(Arena*)` @ `0x48cc10`) writes
`this+8 = arena`, zeroes `_has_bits_`/`_cached_size_`, binds `ArenaStringPtr` fields to
`fixed_address_empty_string` and the `MapField` to the empty-table vtable, propagates the
`Arena*` into nested sub-messages, and `memset`s the remainder. Per-message dtors skip frees
on the arena path (the low-bit `test $0x1,%dil` guard on the stored `Arena*`); destruction
is en-masse via the `Arena` dtor.

### 2.7 Enum code points (the field *values*)  `[OBS enums.json]`

```text
ntff::ntrace_event_phase_type : INVALID=0 START=1 STOP=2 INSTANT=3
ntff::nc_engine_type          : PE=0 ACT=1 POOL=2 DVE=3 SP=4         (POOL=2 == GPSIMD/Q7)
ntff::block_type              : NC=0 DMA=1 TOPSP=2
ntff::dma_queue_type          : UNKNOWN=0 INSTRUCTIONS=1 WEIGHTS=2 INPUT=3 OUTPUT=4 H2T=6
ntff::collectives_packet_type : MEMCPY=0 ADD=1 SEMA_UPDATE_RECV=2 SEMA_UPDATE_SEND=3 NET_IDX_UPDATE=4
ntff::instruction_patch_type  : INSERT=0 MODIFY=1 DELETE=2
ntff::notification_trace_type : INSTRUCTION=0 EVENT=1 ERROR=2 EXPLICIT=3 DMA=4 THROTTLE=5
```

---

## 3. Device-notification-type → `ntff::(trace_type, block_type)` jump table  `[HIGH/OBS]`

The host classifier that turns a raw **device** notification kind into the two ntff trace
discriminants is `nrt_profile_convert_trace_type_from_ntff_params(notification_type,
ntff::notification_trace_type&, ntff::block_type&)` @ **`0xaaf40`** (215 B, `end 0xab017`).
SysV arg mapping: `%edi` = device `notification_type`; `%rsi` = `notification_trace_type&`
(out); `%rdx` = `block_type&` (out). The branch is a real jump table.

The two relevant enums (`enums.json`, byte-exact):

```text
notification_type (device, 0..10):
  TRACE=0 EVENT=1 ERROR=2 INFER_STATUS=3 DMA=4 THROTTLE=5
  TOPSP_TRACE=6 TOPSP_EVENT=7 TOPSP_ERROR=8 TOPSP_CC_STATUS=9 MAX=10
ntff::notification_trace_type (0..5):
  INSTRUCTION=0 EVENT=1 ERROR=2 EXPLICIT=3 DMA=4 THROTTLE=5
ntff::block_type: NC=0 DMA=1 TOPSP=2
```

Dispatch (decompiled, naming real symbols):

```c
// nrt_profile_convert_trace_type_from_ntff_params @ 0xaaf40
static int
nrt_profile_convert_trace_type_from_ntff_params(notification_type nt,
                                                ntff::notification_trace_type *trace /*rsi*/,
                                                ntff::block_type             *block /*rdx*/)
{
    if ((unsigned)nt > 8)                 // cmp $0x8,%edi ; ja default
        goto invalid;                     // 9=TOPSP_CC_STATUS, 10=MAX fall here
    // jump table @ .rodata 0x8570c0 (rcx-relative int32 offsets), 9 entries:
    switch (nt) {
    case 0: case 3:  *block=NC;    *trace=INSTRUCTION; return 0; // 0xaaf60
    case 1:          *block=NC;    *trace=EVENT;       return 0; // 0xaaf80
    case 2:          *block=NC;    *trace=ERROR;       return 0; // 0xaaf90
    case 4:          *block=DMA;   *trace=DMA;         return 0; // 0xaafa0
    case 5:          *block=NC;    *trace=THROTTLE;    return 0; // 0xaafb0
    case 6:          *block=TOPSP; *trace=INSTRUCTION; return 0; // 0xaafc0
    case 7:          *block=TOPSP; *trace=EVENT;       return 0; // 0xaafd0
    case 8:          *block=TOPSP; *trace=ERROR;       return 0; // 0xaaf70
    }
invalid:                                  // 0xaafdf
    nlog_write(..., "nrt_profile_convert_trace_type_from_ntff_params",
               "Invalid notification type: %d", nt);             // @ rodata 0x83e9b7
    return 2;
}
```

The full classification matrix (`(notification_type) → (trace_type, block_type)`):

| device `notification_type` | → `notification_trace_type` (`%rsi`) | → `block_type` (`%rdx`) | case target |
|---|---|---|---|
| 0 `TRACE`        | `INSTRUCTION` (0) | `NC` (0)    | `0xaaf60` |
| 1 `EVENT`        | `EVENT` (1)       | `NC` (0)    | `0xaaf80` |
| 2 `ERROR`        | `ERROR` (2)       | `NC` (0)    | `0xaaf90` |
| 3 `INFER_STATUS` | `INSTRUCTION` (0) | `NC` (0)    | `0xaaf60` (shares 0) |
| 4 `DMA`          | `DMA` (4)         | `DMA` (1)   | `0xaafa0` |
| 5 `THROTTLE`     | `THROTTLE` (5)    | `NC` (0)    | `0xaafb0` |
| 6 `TOPSP_TRACE`  | `INSTRUCTION` (0) | `TOPSP` (2) | `0xaafc0` |
| 7 `TOPSP_EVENT`  | `EVENT` (1)       | `TOPSP` (2) | `0xaafd0` |
| 8 `TOPSP_ERROR`  | `ERROR` (2)       | `TOPSP` (2) | `0xaaf70` |
| 9 `TOPSP_CC_STATUS`, 10 `MAX`, > 8 | — (default) | — | `0xaafdf` → log + `return 2` |

Jump table bytes (`.rodata 0x8570c0`, decoded `target = 0x8570c0 + int32(entry)`):

```text
8570c0  a03e85ff c03e85ff d03e85ff a03e85ff   idx0->0xaaf60 idx1->0xaaf80 idx2->0xaaf90 idx3->0xaaf60
8570d0  e03e85ff f03e85ff 003f85ff 103f85ff   idx4->0xaafa0 idx5->0xaafb0 idx6->0xaafc0 idx7->0xaafd0
8570e0  b03e85ff                              idx8->0xaaf70
```

> **QUIRK** — `TOPSP_CC_STATUS = 9` (the topsp collective-status notification, the same
> value Part-10 #871 anchors as `NOTIFICATION_TYPE_TOPSP_CC_STATUS = 9`) is **outside** the
> `cmp $0x8 / ja` guard window. It is therefore **not** assigned a trace classification by
> this converter — it hits the default arm, logs `"Invalid notification type: 9"`, and
> returns `2`. A reimplementation must classify CC-status notifications on a *separate*
> path (the collectives `cc_op_trace` / `collectives_op_trace.notifications_` plane, §2.3),
> not through this table. `[OBS HIGH]`

> **NOTE** — `INFER_STATUS=3` collapses onto the same `(INSTRUCTION, NC)` target as `TRACE=0`
> (the jump-table idx0/idx3 entries are identical `0xaaf60`); the two device kinds are
> indistinguishable in the resulting trace record. The TOPSP family (6/7/8) re-uses the
> `INSTRUCTION/EVENT/ERROR` trace types but stamps `block_type = TOPSP(2)`; only `DMA(4)`
> produces `block_type = DMA(1)`. The full producer of these device notifications is
> `control/interrupt/device-host-notification.md`.

---

## 4. simdjson NEFF `def.json` parse state  `[HIGH/OBS]`

NEFF `def.json` is parsed by a statically-linked **simdjson DOM** parser
(`simdjson::dom::parser`), runtime-dispatched to the **haswell** backend on x86, and walked
with `simdjson::dom::element` accessors. The on-disk builder leaks its source path via an
`__assert_fail`: `/opt/workspace/KaenaRuntime/kelf/kelf2kbin.cpp`. Entry chain:

```text
json_parse_load_elements @ 0x4af3e0  (.constprop.0)
  -> simdjson::dom::parser::parse_into_document @ 0xe4b50   (parser, &parser.doc, buf, len)
  -> parse_neff_json @ 0xe1d10                              (io tensors, node map)
      -> parse_one_ucode_lib @ 0x4b1610                     (GPSIMD custom-op library object)
          -> parse_one_ucode_lib_function @ 0x4b1180        (per-function metadata)
```

### 4.1 `simdjson::dom::parser` (104 B)  `[OBS structures.json]`

| off | member | type |
|---|---|---|
| `+0x00` | `threaded` | `bool` |
| `+0x08` | `implementation` | `unique_ptr<internal::dom_parser_implementation>` (backend) |
| `+0x18` | `valid` | `bool` |
| `+0x1c` | `error` | `simdjson::error_code` |
| `+0x20` | `doc` | `simdjson::dom::document_0` (40 B inline; §4.2) |
| `+0x48` | `_max_capacity` | `size_t` — the file-size guard |
| `+0x50` | `loaded_bytes` | `unique_ptr<char[]>` (padded file image) |
| `+0x60` | `_loaded_bytes_capacity` | `size_t` |

`_max_capacity @ parser+0x48` backs the guard string
`"File %s size (%zu) exceeds json parser maximum (%zu)"`.

### 4.2 Documents & DOM access objects  `[OBS]`

```text
simdjson::dom::document   (24B): tape unique_ptr<u64[]> @0 ; string_buf unique_ptr<u8[]> @8 ;
                                 allocated_capacity size_t @16
simdjson::dom::document_0 (40B): the in-parser inline form (unique_ptrs expanded to 16B each):
                                 tape @0 ; string_buf @16 ; allocated_capacity @32
internal::tape_ref        (16B): const dom::document* doc @0 ; size_t json_index @8
dom::element/array/object (16B): each = { tape_ref } (a doc-relative cursor)
padded_string             (16B): size_t viable_size @0 ; char* data_ptr @8
```

`document_stream` (184 B, the threaded multi-doc form) exists but is **not** on the NEFF
path (the runtime uses single-document `parse_into_document`).

### 4.3 `internal::dom_parser_implementation` + haswell concrete  `[OBS]`

```text
internal::dom_parser_implementation (48B base):
  +0x00 vptr
  +0x08 uint32 n_structural_indexes
  +0x10 unique_ptr<u32[]> structural_indexes
  +0x18 uint32 next_structural_index
  +0x20 size_t _capacity
  +0x28 size_t _max_depth
haswell::dom_parser_implementation (88B):
  +0x00 [48] internal::dom_parser_implementation base
  +0x30 unique_ptr<haswell::open_container[]> open_containers   (stage-2 stack)
  +0x38 unique_ptr<bool[]> is_array
  +0x40 const uint8_t* buf ; +0x48 size_t len ; +0x50 dom::document* doc
```

### 4.4 Tape type markers read during NEFF parse  `[OBS decompile]`

Each simdjson tape word `w`: low 56 bits = payload/index, `HIBYTE(w)` = an ASCII type marker.
Confirmed live in `parse_one_ucode_lib_function` (`0x4b1180`):

```text
4b1232  shr $0x38,%rcx ; 4b1236 cmp $0x22,%rcx    ; HIBYTE=='"' (0x22) -> string
4b1397  movabs $0x7500000000000000,%rcx           ; w=='u' word -> uint64 (value at tape[idx+1])
4b1590  movabs $0x6c00000000000000,%rcx           ; w=='l' word -> int64  (sign-checked)
```

`element::at_key` performs the keyed object lookup; on a miss the `simdjson_result.second`
holds a non-`SUCCESS` `error_code`. This accessor backs every `parse_one_*`.

### 4.5 The NEFF `def.json` ucode-lib parse tree  `[OBS]`

`parse_one_ucode_lib` (`0x4b1610`) reads keys `"library"` (file path), the min-lib version
gate `"1.21.1.0"` (string present in `.rodata`), `"opcode"`, `"cpu_id"` (asserted `== 0` on
the base path — the `"cpu_id == 0"` assert string), and `"functions"` (array). Each element
goes to `parse_one_ucode_lib_function` (`0x4b1180`), keys `"name"`, `"opcode"`,
`"sub_opcode"` (`opcode`/`sub_opcode` accept tape `'u'`/`'l'`, narrowed to `u8` on store),
appending a `ucode_lib::function_entry`. Strings cited at `0x848f7d` (`"opcode"`),
`0x848f60` (`"parse_one_ucode_lib_function"`), `0x82c168` (`"failed to parse function field"`).

---

## 5. kbin loadable-library function table (byte-level)  `[HIGH/OBS]`

Three byte-exact host forms of the GPSIMD custom-op function metadata, plus the device-staged
baked table and its producer.

### 5.1 On-disk (parsed from NEFF) — `kbin_*`  `[OBS structures.json]`

```text
kbin_ucode_lib_sets_t (16B):   libs(kbin_ucode_lib*) @0 ; num_libs(size_t) @8
                               (custom ops present <=> num_libs > 1: base lib + >=1 custom)
kbin_ucode_lib (40B):          content(void*) @0 ; content_size(u64) @8 ; flags(u32) @16 ;
                               cpu_id(u8) @20 ; total_cpus(u8) @21 ; function_set(16B) @24
kbin_ucode_lib_function_set (16B): functions(kbin_ucode_lib_function*) @0 ; num_functions(u8) @8

*** kbin_ucode_lib_function (258B) - PER-FUNCTION METADATA, byte-exact:
       +0x000 [256] char    name[256]    function symbol (NUL-padded fixed buffer)
       +0x100 [1]   uint8_t opcode       the TPB opcode this fn handles
       +0x101 [1]   uint8_t sub_opcode   extended-opcode specializer
    (no name-length field; fixed 256-byte name => table stride is exactly 258.)
```

### 5.2 Runtime (the simdjson twin) — `ucode_lib`  `[OBS]`

```text
ucode_lib (80B):   name(std::string,32B) @0 ; content(void*) @32 ; content_size(u64) @40 ;
                   flags(u32) @48 ; cpu_id(u8) @52 ; total_cpus(u8) @53 ;
                   functions(std::vector<function_entry>) @56
ucode_lib::function_entry (40B): name(std::string,32B) @0 ; opcode(u8) @32 ; sub_opcode(u8) @33
```

The runtime adds the `name` field the on-disk kbin form omits; `cpu_id`/`total_cpus` are
filled by `parse_one_ucode_lib`. The JSON key set `{"name","opcode","sub_opcode"}` is
byte-identical to the kbin field set.

### 5.3 Device-staged descriptor — `ucode_lib_set_info`  `[OBS]`

```text
ucode_lib_set_info (48B): scratch_space/ucode_table/extram(dmem_t*) @0/8/16 ;
                          num_libs(int) @24 ; libs(ucode_lib_info_t*) @32 ; lib_dmem(dmem_t*) @40
ucode_lib_info (32B):     address(u64) @0 ; flags(u32) @8 ; cpu_id(u8) @12 ; total_cpus(u8) @13 ;
                          num_funcs(int) @16 ; funcs(ucode_func_info_t*) @24
ucode_func_info (258B):   == kbin_ucode_lib_function (name[256] @0 + opcode @256 + sub_opcode @257)
```

### 5.4 The baked per-arch table `SUNDA_UCODE_LIB_INFO` (27304 B)  `[OBS structures.json]`

```text
SUNDA_UCODE_LIB_INFO (27304B):
  +0x000 [8]     uint64_t scratch_base
  +0x008 [8]     uint64_t scratch_size
  +0x010 [64]    uint64_t dma_queue_m2s_offset[8]   (8 == NUM_POOL_CORES / Q7 cores)
  +0x050 [64]    uint64_t dma_queue_s2m_offset[8]
  +0x090 [27160] SUNDA_UCODE_LIB_LIBRARY_TABLE_ENTRY table[97]    (97 * 280 = 27160)
  (math: 144 header + 97*280 = 27304 OK)

*** SUNDA_UCODE_LIB_LIBRARY_TABLE_ENTRY (280B) - the device opcode->lib resolver row:
       +0x000 [1]   uint8_t                 valid           (=1 when populated)
       +0x001 [1]   NEURON_ISA_TPB_OPCODE_1 opcode          (typed; the builtin this row handles)
       +0x002 [1]   uint8_t                 sub_opcode
       +0x003 [1]   uint8_t                 cpu_id
       +0x004 [1]   uint8_t                 total_cpus
       +0x005 [7]   uint8_t                 reserved[7]
       +0x00C [4]   uint32_t                flags           (= kbin flags & 7)
       +0x010 [8]   uint64_t                stripped_lib_addr (device SOC addr of lib image)
       +0x018 [256] char                    func_name[256]
```

> **GOTCHA** — two distinct strides. The **on-disk / staged** per-function record is **258 B**
> (`kbin_ucode_lib_function` ≡ `ucode_func_info`: `name[256] + opcode + sub_opcode`). The
> **device/baked** row is **280 B** (`SUNDA…ENTRY`: a `valid/opcode/cpu/total/reserved/flags/
> addr` header **then** `func_name[256]`). The producer reads the source at stride 258 and
> writes the destination at stride 280. Do not conflate them.

### 5.5 The producer `ucode_stage_libs_table` @ `0x3108e0`  `[OBS decompile]`

Fills the `SUNDA…` table from the staged `ucode_lib_set_info`. Per lib `i` (32-B
`ucode_lib_info`) with `num_funcs > 0`, per fn `j`:

```c
// ucode_stage_libs_table @ 0x3108e0 (789B, end 0x310bf5)
for (lib in co->libs[0..num_libs]) {                  // ucode_lib_info, 32B each
    flags3 = *(u32*)(lib + 8) & 7;                    // 310a4f mov 0x8(r14); 310a5a and $0x7
    funcs  = *(ucode_func_info**)(lib + 24);          // 310a4b mov 0x18(r14),r15
    for (j = 0; j < lib->num_funcs; ++j) {
        src = funcs + 258*j;                           // on-disk 258 stride
        SUNDA_ENTRY *e = &table[row++];                // dst 280 stride (310aac add $0x118,rbp)
        e->valid           = 1;                        // 310a7d movb $0x1,-0x18(rbp)
        e->flags           = flags3;                   // 310a8c mov eax,-0xc(rbp)
        e->cpu_id          = *(u8*)(lib + 12);         // 310a8f movzbl 0xc(r14)
        e->total_cpus      = *(u8*)(lib + 13);         // 310a99 movzbl 0xd(r14)
        e->stripped_lib_addr = *(u64*)(lib + 0);       // 310ab9 mov (r14); 310abc -> -0x120(rbp)
        strncpy(e->func_name, src /*name[256]*/, 0xFF);// 310a84 mov $0xff,edx; 310ac3 strncpy
        {u16 w = *(u16*)(src + 0x100);                 // 310ada movzwl 0x100(rax,rbx)
         e->opcode = (u8)w; e->sub_opcode = (u8)(w>>8);}
    }
}
```

Guards (byte-exact constants + `.rodata` strings, all `[OBS]`):

| check | instruction | constant | string |
|---|---|---|---|
| global table cap | `cmp $0x60,%r12d` (`0x310aef`) | row > 96 ⇒ max **97** | `"too many ucode functions in this table.  Max is %d.  Add support for mid-neff reload?"` |
| per-function name | `cmp $0x3f,%rax` (`0x310a6f`) | `strlen(name)` > 63 ⇒ max **64** | `"ucode function name (%s) is too long.  Max length = %d"` |
| per-core fn cap | `cmp $0x1b,%rax` (`0x310b61`) | per-`cpu_id==0` count > 27 ⇒ max **28** | `"Number of ucode functions: %lu per core exceeded the maximum allowed %u"` |
| DMA queue / core | — | one queue per Q7 core (8) | `"DMA allocation for CustomOp fail: need one queue for each Q7 Core, there are %d cores, only %d queues were found"` |

The `opcode` field is typed `NEURON_ISA_TPB_OPCODE_1` (the 145-value TPB opcode enum; the
opcode→builtin-name table is covered separately). The sibling baked table
`SUNDA_UCODE_RT_INFO` is **27312 B** (8 B larger than `SUNDA_UCODE_LIB_INFO`; the per-arch
runtime, non-lib resolver) — members not expanded here.

---

## 6. Cross-checks  `[OBS]`

- **Oneof discriminant == protobuf field number** in both unions:
  `cpu/nd/outcc = 3/4/6`, `sema/net_idx = 4/5` — `clear_*`/`set_allocated_*` case ints match
  the `InternalWriteMessage($field,%edi)` tags exactly (§2.4).
- **GPSIMD/Q7 anchors** in the trace tree: `engine_instruction_info.nc_engine_type_ == POOL(2)`
  (§2.2), `nc_timestamp_info.tpb_pool_` (f4, §2.5), and `subgraph_info.collectives_*`
  (§2.5). The `engine_instruction_info.instructions_` (f3, tag `0x1a`) bytes feed the device-ISA
  disassembler. `nc_engine_type` enum: `PE=0 ACT=1 POOL=2 DVE=3 SP=4`.
- **Packet-type ↔ oneof** consistency: `collectives_packet_type {SEMA_UPDATE_RECV(2),
  SEMA_UPDATE_SEND(3)}` ↔ the `sema_update` oneof arm (field 4); `NET_IDX_UPDATE(4)` ↔ the
  `net_idx_update` arm (field 5) (§2.3/§2.4).
- **Triple-consistent ucode record**: `kbin_ucode_lib_function` (258 B) ≡ `ucode_func_info`
  (258 B) ≡ the `SUNDA…ENTRY` trailing `func_name[256] + opcode + sub_opcode`, with the
  258-stride source read in `ucode_stage_libs_table` (§5.5) confirming the on-disk layout.
- **Notification classifier** (§3) shares the device `notification_type` enum (`TOPSP_CC_STATUS
  = 9`) used by the Part-10 collectives notification consumers and the Part-8 runtime
  notification path; the `ntff::block_type {NC=0,DMA=1,TOPSP=2}` and `ntff::notification_trace_type`
  outputs feed the trace record (`trace_info.trace_type_`/`block_type_`,
  `notification_buffer_size.{trace_type,block_type}`).
- **Message object invariant**: every `C2(Arena)` writes `Arena*` at `+8` and
  `Impl_._has_bits_` at `+0x10`; a serialize read at object offset `O` is `Impl_` offset
  `O − 0x10` (the basis for the §2.1 member cross-check).

---

## 7. Residuals (carry forward)  `[CARRIED]`

- **`TcParseTableBase` byte format** (the `_table_` in `.data`, e.g.
  `ntff::ntrace_event::_table_` @ `0xc0be80`, `collectives_dma_packets::_table_` @ `0xc098e0`):
  the parse-side `FastFieldEntry`/`FieldEntry`/field-number lookup. The serialize-tag method
  (§1) supersedes it for field numbers; full parse-table decode deferred.
- **`ntrace_event` host-telemetry & interning**: `attributes_` (f12 map) and `hint_`/`phase_`
  paths interact with `interned_data_db` (`id_map_<u64,string>`); the interning indirection
  is not traced here. `phase_` (f13) is a bare `int` varint in this build (§2.1).
- **`neff_node_info` field 5** (tag `0x2a`, length-delimited, co-located with the outcc oneof
  path): the `uid_` vs embedded-message slot is not disambiguated. `[MED]`
- **Host stat field numbers** (`host_stats`/`cpu_util`/`nc_memory_usage`/`host_mem_usage`):
  captured structurally; serialize-tag map not enumerated (low GPSIMD relevance).
- **`SUNDA_UCODE_RT_INFO` (27312 B)**: the sibling runtime (non-lib) per-arch resolver;
  members un-analysed.
