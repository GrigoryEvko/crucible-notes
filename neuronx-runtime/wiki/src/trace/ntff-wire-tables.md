# NTFF Wire Tables (TcParseTable Decode)

> *All addresses, offsets, and bytes on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, package `KaenaProfilerFormat-2.31.0.0`). The ELF is **not** stripped; full `.symtab` + DWARF are present. The 38 `_table_` statics live in `.data` (VMA range `0xc09680..0xc0bf20`), and for this binary `.data` VMA equals file offset (`.data` base `0xc07e00`), so every `xxd -s 0x…` / `objdump -s` offset cited below reads the byte shown. The protobuf runtime is vendored `google::protobuf` 26.1 (`GOOGLE_PROTOBUF_VERSION 5026001`). Other versions will differ.*
> *Evidence grade: **Confirmed (byte-anchored)** — every header field, `FastFieldEntry`, `FieldEntry`, and `type_card` below is byte-decoded from `.data` and cross-validated against the independently-recovered `FileDescriptorProto` schema; 207/207 `FieldEntry` records agree with [zero mismatches](#verification). The on-disk struct layout is corroborated by IDA's recovered protobuf TIL (`TcParseTable<…>` template instances). · Part XIII — Profiling, Trace & Telemetry · [back to index](../index.md)*

## Abstract

NTFF deserialization is **table-driven**, not code-driven. Each `ntff::<message>` class exposes a 12-byte `_InternalParse` thunk that does nothing but `lea` its static `TcParseTableBase _table_` into `%rcx` and tail-`jmp` into one shared interpreter, `google::protobuf::internal::TcParser::ParseLoop` @`0x6a4b00`. All per-message parsing knowledge — which field number lands at which struct offset, what wire type to expect, which sub-message table to recurse into — is encoded as **data** in that `_table_` static. This page byte-decodes all **38** `_table_` statics and reverses the full protobuf-26.1 fast-parse layout: the `0x30`-byte header, the inline `FastFieldEntry[]` dispatch table, the field-number lookup table, the `FieldEntry[]` array (one 12-byte record per declared field), and the `FieldAux[]` sub-table pointers.

The familiar reference frame is **protobuf's `TcParser` (table-driven "tail-call") parser**, introduced in protobuf 3.21 and shipped in the 26.1 runtime statically linked here. A reimplementer who has read the open-source `generated_message_tctable_*.{h,cc}` will recognize every structure on this page; what is new is the exact on-disk byte layout *as protoc emitted it for this schema*, with each field tied to a concrete struct offset and a concrete `type_card` word. The two NTFF pages are complementary: the [schema page](ntff-format.md) recovers the source `.proto` from the embedded `FileDescriptorProto` blobs (the authoritative definition); this page recovers the **wire-parse machine** from the parse tables and uses it as an *independent* check on that schema. They agree field-for-field, 207 records, zero contradictions.

The centerpiece is the **`type_card`** — a `uint16` per field that packs the entire parse decision (`FieldKind`, cardinality/presence, numeric width, and proto representation) into 16 bits. Reversing it from the 17 distinct values observed across the schema yields a 1:1 map from `type_card` to a `(proto_type, label)` pair, which is what lets the wire tables stand alone as a schema cross-check. The page also pins the two protobuf-26.1 details that bite a reimplementer: `FieldEntry[]` is stored in **field-number** order (not source-declaration order), and a `map<>` or a `oneof` field encodes very differently from a plain scalar (`FieldAux` holds a *packed `MapAuxInfo` word* for maps, and oneof members share one struct offset plus a oneof-case word).

For reimplementation, the contract is:

- **The `TcParseTableBase` on-disk layout** — the `0x30`-byte header and the order of the five sub-arrays that follow it (`fast_entries`, `field_lookup_table`, `field_entries`, `aux_entries`, `field_names`), all addressed by base-relative offsets in the header.
- **The 16-byte `FastFieldEntry`** — `{TailCallParseFunc target, TcFieldData bits}` — and how `ParseLoop` indexes it by `(coded_tag >> 3) & fast_idx_mask`.
- **The 12-byte `FieldEntry`** — `{offset, has_idx, aux_idx, type_card}` — and the full `type_card` bit encoding, so a reader can classify any field from its 16-bit card alone.
- **The aux model** — `FieldEntry.aux_idx → FieldAux[]` is a `const TcParseTableBase*` for sub-messages (reproducing the whole containment graph) and a packed `MapAuxInfo` word for the two `map<>` fields.

| | |
|---|---|
| **Interpreter** | `google::protobuf::internal::TcParser::ParseLoop` @`0x6a4b00` (all 38 messages) |
| **Parse thunks** | `ntff::<msg>::_InternalParse` — 12-byte `lea _table_,%rcx ; jmp ParseLoop` |
| **Tables** | **38** `TcParseTableBase` statics, `.data` `0xc09680..0xc0bf20` (`nm \| rg '4ntff.*7_table_E'`) |
| **Header size** | `0x30` bytes (`google::protobuf::internal::TcParseTableBase`, IDA TIL `size=48`) |
| **FastFieldEntry** | 16 bytes `{target(8), TcFieldData bits(8)}` — `(fast_idx_mask>>3)+1` slots |
| **FieldEntry** | 12 bytes `{offset(u32), has_idx(i32), aux_idx(u16), type_card(u16)}` |
| **FieldEntry total** | **207** records across the 38 tables (= schema's 205 scalar/message + 2 `map<>`) |
| **type_card values** | **17** distinct, each 1:1 to a `(proto_type, label)` pair |
| **Fallback** | `TcParser::GenericFallback` @`0x699900` (every table, header `+0x28`) |
| **Sub-message recursion** | `FastMtS1/MtR1` @`0x6a5ac0…` + `FieldAux` `const TcParseTableBase*` |
| **Container framing** | see [ntff-format §4](ntff-format.md#4-the-on-disk-ntff-container) (128-B header on `.ntff`) |

---

## 1. The Table-Driven Parse Model

### Purpose

A protobuf message generated by protoc-26.1 carries no hand-written field loop. Its `_InternalParse` is a stub; the real work is a single shared interpreter driven by a per-message descriptor. Reimplementing NTFF deserialization therefore means reimplementing two things: the `TcParseTableBase` data structure (this page) and the `ParseLoop` interpreter (the open-source `TcParser`, vendored unchanged). Everything specific to NTFF lives in the 38 static tables.

### Entry Point

Every parser is a 12-byte thunk; `notification_buffer_size` is representative (disassembly verbatim):

```asm
; ntff::notification_buffer_size::_InternalParse  @0x47dc60
47dc60: 48 8d 0d 99 d9 78 00   lea    0x78d999(%rip),%rcx   ; -> 0xc0b600 _table_
47dc67: e9 94 6e 22 00         jmp    0x6a4b00              ; TcParser::ParseLoop  (TAIL CALL)
```

The `lea` resolves RIP-relative to the message's `_table_` static; the `jmp` tail-calls the shared interpreter. There is no per-message logic in the thunk. `ntff::ntff_info::_InternalParse` @`0x47dc70` is byte-identical but for the displacement (`-> 0xc0b240`).

```text
ntff::<msg>::_InternalParse (0x47dc60..)         ── 12-byte lea+jmp thunk, one per message
  └─ TcParser::ParseLoop (0x6a4b00)              ── the shared table interpreter
       ├─ fast path: index FastFieldEntry[(tag>>3)&fast_idx_mask] -> FastV32S1 / FastMtR1 / …
       ├─ slow path: TcParser::MiniParse (0x6a08f0) -> field_lookup_table -> FieldEntry[]
       └─ miss:      TcParser::GenericFallback (0x699900)  ── header +0x28, every table
```

### Algorithm

`ParseLoop` reads a wire tag, tries the small inline fast table first, and falls to the field-number lookup table on a fast miss. The fast/slow split is the whole point of the design — the common low-numbered, single-occurrence scalar fields resolve in a handful of instructions.

```c
// Models google::protobuf::internal::TcParser::ParseLoop @0x6a4b00 over an ntff _table_.
// `tbl` is the 0x30-byte TcParseTableBase header; the sub-arrays follow it inline.
function ParseLoop(msg, ptr, ctx, tbl):
    while ptr < end:
        coded_tag = read_tag(ptr)                       // 1- or 2-byte varint tag
        slot = (coded_tag >> 3) & tbl.fast_idx_mask      // header +0x08; #slots=(mask>>3)+1
        fe   = tbl.fast_entries[slot]                    // 16-byte FastFieldEntry
        if fe.bits.coded_tag == coded_tag:               // exact tag match -> FAST PATH
            ptr = fe.target(msg, ptr, ctx, fe.bits, tbl) // FastV32S1 / FastMtR1 / FastUS1 / …
            continue
        // FAST MISS -> SLOW PATH (TcParser::MiniParse @0x6a08f0)
        idx = field_number_lookup(tbl.field_lookup_table, coded_tag >> 3)  // header +0x0A
        if idx < 0:                                       // unknown field
            ptr = GenericFallback(msg, ptr, ctx, …, tbl)  // header +0x28 @0x699900
            continue
        entry = tbl.field_entries[idx]                    // 12-byte FieldEntry, field-NUMBER order
        ptr = parse_one(msg, ptr, ctx, entry, tbl.aux_entries)  // dispatch on entry.type_card
```

The fast handler embedded in each slot already knows the wire type and the struct offset (both packed into `TcFieldData bits`), so the fast path never consults `FieldEntry[]`. `FieldEntry[]` is the authority for the slow path and for sub-message recursion; it is the array this page decodes.

### Function Map

The handlers `ParseLoop` dispatches to are all in the vendored libprotobuf `.text`; every address below is `nm`-resolved and `c++filt`-demangled from the live binary.

| Function | VMA | Role | Confidence |
|---|---|---|---|
| `TcParser::ParseLoop` | `0x6a4b00` | the shared table interpreter; target of all 38 `_InternalParse` | CERTAIN |
| `TcParser::MiniParse` | `0x6a08f0` | slow-path entry; also fills empty/oneof/2-byte-tag fast slots | CERTAIN |
| `TcParser::GenericFallback` | `0x699900` | unknown-field fallback; header `+0x28` of every table | CERTAIN |
| `TcParser::FastV32S1` / `FastV64S1` | `0x6a1430` / `0x6a1590` | singular 32/64-bit varint (int/uint/enum/bool) | CERTAIN |
| `TcParser::FastV32S2` / `FastV64S2` | `0x6a7390` / `0x6a7410` | same, **2-byte tag** (e.g. field #16 `nrta_seq_id`) | CERTAIN |
| `TcParser::FastMtS1` / `FastMtR1` | (`0x6a5…`) / `0x6a5ac0` | singular / repeated **message** field | HIGH |
| `TcParser::FastMtR2` | `0x6a5e50` | repeated message, 2-byte tag | HIGH |
| `TcParser::FastUS1` / `FastUS2` | `0x6a2c40` / `0x6a2d60` | UTF8-validated **string** (1-/2-byte tag) | CERTAIN |

> **NOTE —** the fast handler suffix encodes its dispatch shape: `V32`/`V64` = varint width, `Mt` = message, `U`/`B` = UTF8-string/bytes, `F32`/`F64` = fixed float/double; the trailing `S1`/`R1`/`S2` = Singular/Repeated × 1-byte/2-byte tag. A reimplementer does not need to replicate this naming, but it is the fastest way to read a decoded fast table.

---

## 2. TcParseTableBase Layout

### Purpose

`TcParseTableBase` is the per-message static the interpreter walks. It is a fixed `0x30`-byte header followed by five inline sub-arrays, each located by a base-relative offset stored in the header. The whole thing is one contiguous `.data` object (protoc emits it as a `TcParseTable<…>` template instance); the header offsets make it self-describing.

### Encoding — the header

The header is byte-decoded below and corroborated against IDA's recovered protobuf TIL (`google::protobuf::internal::TcParseTableBase`, `size=48`). Two fields the L-HAL-23 first pass mis-split are corrected in place.

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `has_bits_offset` | `+0x00` | `uint16` | byte offset of `_has_bits_` in the C++ message (`0` if the message has none) |
| `extension_offset` | `+0x02` | `uint16` | `0` for every ntff message (no proto2 extensions) |
| `max_field_number` | `+0x04` | `uint32` | largest declared field number |
| `fast_idx_mask` | `+0x08` | `uint8` | `(tag>>3) & mask` indexes `fast_entries`; `#slots = (mask>>3)+1` |
| (reserved) | `+0x09` | `uint8` | `0` in every table |
| `lookup_table_offset` | `+0x0A` | `uint16` | base-relative → `field_lookup_table` (field-number skip table) |
| `skipmap32` | `+0x0C` | `uint32` | 32-bit "present-in-fast-table" skip bitmap |
| `field_entries_offset` | `+0x10` | `uint32` | base-relative → `FieldEntry[]` |
| `num_field_entries` | `+0x14` | `uint16` | == declared field count for this message |
| `num_aux_entries` | `+0x16` | `uint16` | number of `FieldAux` records (sub-msg tables / map info) |
| `aux_offset` | `+0x18` | `uint32` | base-relative → `FieldAux[]` |
| (reserved) | `+0x1C` | `uint32` | `0` in every table |
| `default_instance` | `+0x20` | `const MessageLite*` | `ntff::_<msg>_default_instance_` (`.bss`) |
| `fallback` | `+0x28` | `TailCallParseFunc` | `TcParser::GenericFallback` @`0x699900` (all 38) |

> **CORRECTION (L-HAL-23 §3c) —** an earlier reading of `notification_buffer_size@0xc0b600` split `field_entries_offset` into a `uint16` at `+0x10` plus a "reserved `+0x12`", and read `skipmap32` as a signed delta. IDA's TIL shows `field_entries_offset` and `aux_offset` are both **`uint32`** (`+0x10`, `+0x18`), and `+0x0C` is the unsigned `skipmap32`. The decoded values are identical for these small tables (offsets < `0x10000`), but the `uint32` reading is the correct one and is what this page uses.

### Encoding — the five sub-arrays

After the header, the body is laid out in a fixed order. IDA's `TcParseTable<5,18,6,93,2>` template instance (the `ntrace_info`-shaped table) pins the order and strides exactly:

```text
TcParseTable<NumFastLog2, NumFieldEntries, NumFieldAux, NameTableLen, NumFastFns>:
  +0x000  header          (48 bytes)                      ── TcParseTableBase, table above
  +0x030  fast_entries    (16 * #fast_slots)              ── FastFieldEntry[],  §3
  +N      field_lookup_table (4-byte aligned skip table)  ── header.lookup_table_offset
  +M      field_entries   (12 * num_field_entries)        ── FieldEntry[],  §4 — FIELD-NUMBER order
  +A      aux_entries     (8  * num_aux_entries)           ── FieldAux[],  §5 — sub-table ptrs / MapAuxInfo
  +F      field_names     (packed length-prefixed names)   ── e.g. "ntff.subgraph_info" + field names
```

For example `TcParseTable<0,2,1,44,2>` (a 2-field, 1-aux table) measures `header 48 + fast 16 + lookup 4 + field_entries 24 + aux 8 + names 44 = 152` bytes, exactly matching the `_table_` symbol-to-symbol gap in `.data`. The `field_names` tail is a faithful echo of the schema's names (e.g. `subgraph_info`'s table embeds the verbatim ASCII `ntff.subgraph_info`); the [schema page](ntff-format.md) is the authority for names, so this tail is read only as a spot-check.

### The C struct, annotated

```c
// google::protobuf::internal::TcParseTableBase — protobuf 26.1, x86-64.
// 0x30-byte header; the five sub-arrays follow inline, located by header offsets.
struct TcParseTableBase {                               // size 0x30
    uint16_t has_bits_offset;     // +0x00  _has_bits_ position in the C++ msg (0 = none)
    uint16_t extension_offset;    // +0x02  0 for all ntff messages
    uint32_t max_field_number;    // +0x04  largest declared field number
    uint8_t  fast_idx_mask;       // +0x08  (tag>>3)&mask -> fast slot; #slots=(mask>>3)+1
    uint8_t  _rsv9;               // +0x09  = 0
    uint16_t lookup_table_offset; // +0x0A  -> field_lookup_table (base-relative)
    uint32_t skipmap32;           // +0x0C  fast-table presence bitmap
    uint32_t field_entries_offset;// +0x10  -> FieldEntry[]      (base-relative)
    uint16_t num_field_entries;   // +0x14  == declared field count
    uint16_t num_aux_entries;     // +0x16  # FieldAux records
    uint32_t aux_offset;          // +0x18  -> FieldAux[]        (base-relative)
    uint32_t _rsv1C;              // +0x1C  = 0
    const MessageLite* default_instance;  // +0x20  ntff::_<msg>_default_instance_ (.bss)
    TailCallParseFunc  fallback;          // +0x28  = GenericFallback @0x699900
    // +0x30: FastFieldEntry fast_entries[(fast_idx_mask>>3)+1];
    // then : uint32 field_lookup_table[...];
    // then : FieldEntry field_entries[num_field_entries];   // FIELD-NUMBER order
    // then : FieldAux  aux_entries[num_aux_entries];
    // then : char      field_names[...];                    // packed length-prefixed
};
```

---

## 3. The FastFieldEntry Dispatch Table

### Purpose

`fast_entries` is the hot path. `ParseLoop` indexes it directly by `(coded_tag >> 3) & fast_idx_mask`; a slot whose embedded `coded_tag` matches is parsed by its `target` handler with no further table lookup. Slots that cannot be served fast — empty indices, oneof members, 2-byte-tag overflow — carry `target = MiniParse` and a zero `TcFieldData`, deflecting to the slow path.

### Encoding

Each slot is 16 bytes: an 8-byte handler pointer and an 8-byte `TcFieldData` word that pre-packs everything the handler needs (the tag, the hasbit index, the aux index, and the struct offset).

| Field | Offset | Width | Meaning |
|---|---|---|---|
| `target` | `+0x00` | 8 | `TailCallParseFunc` — the fast handler (`FastV32S1`, `FastMtR1`, …) or `MiniParse` |
| `bits.coded_tag` | `+0x08`, `[15:0]` | 16 | wire tag (`field<<3 \| wiretype`), 1- or 2-byte encoded |
| `bits.hasbit_idx` | `+0x08`, `[23:16]` | 8 | `0x3f` = "no hasbit" sentinel on the fast path |
| `bits.aux_idx` | `+0x08`, `[31:24]` | 8 | aux index (0 unless the handler needs it) |
| `bits.offset` | `+0x08`, `[63:48]` | 16 | destination struct field offset |

### Worked decode — `notification_buffer_size` fast table

With `fast_idx_mask = 0x18`, the table has `(0x18>>3)+1 = 4` slots. Decoded from `.data` @`0xc0b630`:

```text
slot0  target=MiniParse @0x6a08f0   tag=0x0000  (empty index 0)
slot1  target=FastV32S1 @0x6a1430   tag=0x0008 (field#1,wt0)  hasbit=0x3f  off=0x10   ; block_type   (enum)
slot2  target=FastV32S1 @0x6a1430   tag=0x0010 (field#2,wt0)  hasbit=0x3f  off=0x14   ; trace_type   (enum)
slot3  target=FastV64S1 @0x6a1590   tag=0x0018 (field#3,wt0)  hasbit=0x3f  off=0x18   ; buffer_size  (uint64)
```

The two enum fields take the 32-bit varint fast path (`FastV32S1`), the `uint64` takes the 64-bit one (`FastV64S1`), and the destination offsets `0x10/0x14/0x18` match the `FieldEntry[]` offsets in §4 exactly. The `hasbit=0x3f` sentinel marks proto3 implicit presence — no has-bit is tracked on this path.

> **QUIRK —** the *2-byte-tag* fields never appear in a fast slot with their real handler. `ntrace_event.nrta_seq_id` (#16, tag `0x80 0x01`) is the only such field in NTFF; its fast slot holds `MiniParse`, and the real parse runs through the slow path into `FastV64S2`-class logic. A reimplementer that assumes every field number ≤ `fast_idx_mask>>3` has a populated fast slot will mis-handle the high-numbered and oneof fields, which deliberately deflect to `MiniParse`.

---

## 4. The FieldEntry Array and `type_card`

### Purpose

`field_entries` is the authoritative per-field descriptor and the array this page exists to decode. It has exactly `num_field_entries` records of 12 bytes each, **stored in field-number ascending order regardless of source-declaration order**. Each record names a struct offset, a presence/has-bit index, an aux index, and a `type_card` that encodes the full parse decision.

### Encoding — the 12-byte record

| Field | Offset | Type | Meaning |
|---|---|---|---|
| `offset` | `+0x00` | `uint32` | destination struct field byte offset in the C++ message |
| `has_idx` | `+0x04` | `int32` | has-bit index; `0xffffffff` (= `-1`) for proto3 implicit presence; a oneof-case word index for oneof members |
| `aux_idx` | `+0x08` | `uint16` | index into `FieldAux[]` (sub-message table / map info); `0` if unused |
| `type_card` | `+0x0A` | `uint16` | packed type + cardinality + width + representation (below) |

### Encoding — the `type_card` bit-fields

The `type_card` is the dense core of the format: 16 bits that classify any field. Reversed from the 17 distinct values observed across the 207 records, each value maps 1:1 to a `(proto_type, label)` pair — which is exactly why the wire tables can stand alone as a schema cross-check.

| Bits | Field | Values |
|---|---|---|
| `[2:0]` | `FieldKind` | `1`=Varint · `2`=PackedVarint · `3`=Fixed · `5`=String · `6`=Message · `7`=Map |
| `[5:3]` | `Cardinality` | `0`=Singular(implicit) · `2`=Optional/HasBit (singular message & proto3 `optional`) · `4`=Repeated · `6`=Oneof |
| `[7:6]` | `Size/xform` | `0`=len-delimited · `2`=32-bit · `3`=64-bit |
| `[15:8]` | `Representation` | `0x04`=Message · `0x08`=Int(varint) · `0x0c`=String(UTF8) · `0x10`=ZigZag/signed · `0x18`=Fixed/Enum |

### The 17 observed cards

This is the entire `type_card` codomain for the schema. The right-hand `count` is the number of `FieldEntry` records carrying that card across all 38 tables (the column sums to 207).

| `type_card` | proto type / label | count |
|---|---|---|
| `0x0001` | `bool` | 3 |
| `0x0805` | `bytes` | 7 |
| `0x0c05` | `string` (UTF8) | 21 |
| `0x0881` | `uint32` (singular) | 40 |
| `0x08a2` | `uint32` (packed repeated) | 1 |
| `0x08c1` | `uint64` (singular) | 42 |
| `0x08d1` | `uint64` (proto3 `optional`, hasbit) | 1 |
| `0x08e2` | `uint64` (packed repeated) | 1 |
| `0x1081` | `int32` | 10 |
| `0x10c1` | `int64` | 1 |
| `0x1881` | `enum` | 17 |
| `0x1883` | `float` | 3 |
| `0x18c3` | `double` | 1 |
| `0x0416` | `message` (singular, hasbit) | 22 |
| `0x0426` | `message` (repeated) | 30 |
| `0x0436` | `message` (oneof member) | 5 |
| `0x0027` | `map<>` | 2 |

> **GOTCHA —** the card alone tells presence semantics. `0x0416` (singular message) carries `Cardinality=2` ⇒ an explicit has-bit; `0x0426` (repeated message) carries `Cardinality=4` ⇒ no has-bit (a repeated field is present iff non-empty). A reimplementer that gives a repeated message a has-bit, or omits one from a singular message, will desync `_has_bits_` against the serializer — fields will silently not round-trip. The same `Cardinality` bits distinguish `0x08c1` (singular `uint64`, implicit presence) from `0x08d1` (proto3 `optional uint64`, real has-bit) — the single field `ntrace_event.nrta_seq_id`.

### Worked decode — `subgraph_info` (the 17-field hub)

`subgraph_info` (`has_bits_offset=0x10`, `num_field_entries=17`, `field_entries` @ base+`0x234`) is the device-profile node-tree hub; its decode exercises singular-message hasbits, repeated messages, a scalar bool, and a `uint64`, all in field-number order:

```text
#1  name                          off=0xf0  tc=0x0c05 string     implicit
#2  nd_idx                        off=0x110 tc=0x0881 uint32     implicit
#3  nc_idx                        off=0x114 tc=0x0881 uint32     implicit
#4  instruction_info              off=0x18  tc=0x0426 msg[]   aux0 -> engine_instruction_info
#5  patch_info                    off=0x30  tc=0x0426 msg[]   aux1 -> instruction_patch_info
#6  traces                        off=0x48  tc=0x0426 msg[]   aux2 -> trace_info
#7  dma_queue_usage               off=0x60  tc=0x0426 msg[]   aux3 -> dma_queue_usage_info
#8  io_dma_data_host              off=0x120 tc=0x0001 bool       implicit
#9  coll_info                     off=0xf8  tc=0x0416 msg     aux4 -> collectives_info     hasbit=0x80
#10 timestamp_info                off=0x100 tc=0x0416 msg     aux5 -> nc_timestamp_info     hasbit=0x81
#11 memory_usage_breakdown        off=0x78  tc=0x0426 msg[]   aux6 -> nc_memory_usage
#12 collectives_dma_info          off=0x90  tc=0x0426 msg[]   aux7 -> collectives_channel
#13 collectives_instruction_info  off=0xa8  tc=0x0426 msg[]   aux8 -> engine_instruction_info
#14 exec_duration                 off=0x118 tc=0x08c1 uint64     implicit
#15 cc_op_info                    off=0x108 tc=0x0416 msg     aux9 -> collectives_op_info   hasbit=0x82
#16 comm_info                     off=0xc0  tc=0x0426 msg[]   aux10-> collectives_comm_info
#17 stream_info                   off=0xd8  tc=0x0426 msg[]   aux11-> collectives_comm_info
```

Each of the three singular sub-messages (`coll_info`, `timestamp_info`, `cc_op_info`) carries a distinct has-bit (`0x80/0x81/0x82`); the repeated sub-messages carry `has_idx = -1`. The 12 `aux_idx` values index the 12-entry `FieldAux[]` decoded in §5.

> **QUIRK —** `FieldEntry[]` is **field-number** order, but the *source* declares fields out of order in four messages (`engine_instruction_info` declares 5,6 first; `trace_info`, `neff_node_info`, `ntrace_event` likewise — see [ntff-format §1](ntff-format.md#1-the-schema-recovery-model)). The wire is driven by field number, so the table re-sorts. A reimplementer reading the raw `FileDescriptorProto` (source order) and a reader of these tables (number order) must reconcile the two orderings; the field **numbers** are the invariant.

---

## 5. FieldAux — the Sub-Message Graph and Maps

### Purpose

`aux_entries` is the recursion table. For a message field, `FieldEntry.aux_idx` indexes an 8-byte `FieldAux` that is a `const TcParseTableBase*` pointing at the sub-message's own `_table_`; following these pointers reproduces the entire NTFF containment graph from the parse tables alone. For a `map<>` field the same slot holds a *packed `MapAuxInfo` word*, not a plain pointer.

### Encoding

| `FieldEntry.type_card` kind | `FieldAux[aux_idx]` content |
|---|---|
| Message (`0x0416` / `0x0426` / `0x0436`) | `const TcParseTableBase*` → the sub-message `_table_` static |
| Map (`0x0027`) | packed `MapAuxInfo` word (low32 indexes a `.text` map create/parse region; high bits encode entry layout) |
| Open proto3 enum (`0x1881`) | *none* — open enums need no aux, so `num_aux_entries` excludes them |

### Worked decode — `ntff_info` aux graph

`ntff_info` (`num_aux_entries=8`, `aux_entries` @ base+`0x328`) resolves all 8 `FieldAux` pointers to sub-message tables (`nm`-resolved), reproducing the root of the device-profile tree:

```text
aux[0] = 0xc0ac80 -> neff_node_info
aux[1] = 0xc0b060 -> version_info        (neuron_runtime_version,  field 8)
aux[2] = 0xc0b060 -> version_info        (neuron_driver_version,   field 11)
aux[3] = 0xc0b060 -> version_info        (neuron_collectives_version, field 12)
aux[4] = 0xc0b600 -> notification_buffer_size
aux[5] = 0xc0b180 -> ultraserver_info
aux[6] = 0xc0afc0 -> execution_info
aux[7] = 0xc0aa40 -> trace_info
```

Three distinct fields (`neuron_*_version`) share one `version_info` table pointer — the table is per-*type*, not per-*field*. Every `default_instance` pointer at header `+0x20` likewise resolves to the matching `ntff::_<msg>_default_instance_` `.bss` symbol (e.g. `notification_buffer_size` → `0xcafc80`).

### Oneof and map fields

Two field shapes break the plain-pointer model:

**Oneof members** share one struct offset and one oneof-case word. `collectives_dma_packets` (a `packet_type_data` oneof over fields 4/5) decodes as:

```text
#4  sema_update     off=0x28  tc=0x0436 msg-oneof  aux0 -> collectives_semaphore_update  case_word_idx=0x34
#5  net_idx_update  off=0x28  tc=0x0436 msg-oneof  aux1 -> collectives_net_idx_update     case_word_idx=0x34
```

Both fields write the same union at `+0x28`, and `has_idx = 0x34` is the **oneof-case word** index (which member is active), not a has-bit. `neff_node_info`'s 3-way `node_info` oneof (fields 3/4/6, all at `+0x28`, case word `0x34`) is identical in shape. Oneof members carry `target=MiniParse` in the fast table — they are always slow-path.

**Map fields** (`type_card=0x0027`) store a packed `MapAuxInfo`, byte-decoded but only partially interpreted:

```text
ntrace_event.attributes  (#12, map<string,string>)  FieldAux[0] = 0x0048002800055a5a
interned_data_db.id_map  (#2,  map<uint64,string>)  FieldAux[0] = 0x0030001000055a10
```

The low 32 bits (`0x00055a5a` / `0x00055a10`) index a `.text` map create/parse region; the upper bits encode the entry layout (key/value offsets `0x28`/`0x48` and `0x10`/`0x30` are visible). The **key/value wire types are HIGH-confidence** from the `MapEntry` RTTI typeinfo (`FieldType 9,9` = string/string; `FieldType 4,9` = uint64/string — see [ntff-format §3](ntff-format.md#3-neuron_trace.proto--the-host-trace-schema)); the exact `MapAuxInfo` bitfield split is **MED** confidence — not fully byte-walked.

> **NOTE —** a `map<K,V>` field is **one** `FieldEntry` in the parent table (it dispatches to a `FastMtR1`/`MapAuxInfo` handler that owns the nested entry). The map-entry message's own `key`/`value` fields live in *its* two parse tables (`AttributesEntry`, `IdMapEntry`), which is why the parent-table `FieldEntry` total is **207**, not 211 — the +4 map-entry records sit in the two extra tables, outside the 38 counted here. See [§5 of the schema page](ntff-format.md#5-the-205-vs-207-field-tension).

---

## 6. Verbatim Table Bytes

Three representative tables are reproduced byte-exact (`objdump -s -j .data`; address column = VMA == `.data` file offset). These let a reader replay every decode above. The simplest 3-field table:

```text
### notification_buffer_size  _table_ @0xc0b600  (size 0xa0) ###
 c0b600 00000000 03000000 18007000 f8ffffff   <- header: max_field=3, fast_mask=0x18, lut=0x70, skipmap=0xfffffff8
 c0b610 74000000 03000000 98000000 00000000   <- fe_off=0x74, num_fe=3, num_aux=0, aux_off=0x98
 c0b620 80fcca00 00000000 00996900 00000000   <- default_instance=0xcafc80, fallback=GenericFallback(0x699900)
 c0b630 f0086a00 00000000 00000000 00000000   <- fast slot0: MiniParse(0x6a08f0), TcFieldData=0
 c0b640 30146a00 00000000 08003f00 00001000   <- fast slot1: FastV32S1(0x6a1430) tag=0x08 hasbit=0x3f off=0x10
 c0b650 30146a00 00000000 10003f00 00001400   <- fast slot2: FastV32S1 tag=0x10 off=0x14
 c0b660 90156a00 00000000 18003f00 00001800   <- fast slot3: FastV64S1(0x6a1590) tag=0x18 off=0x18
 c0b670 ffffffff 10000000 00000000 00008118   <- field_lookup_table=0xffffffff ; FieldEntry#1 off=0x10 has=-1 tc=0x1881
 c0b680 14000000 00000000 00008118 18000000   <- FieldEntry#2 off=0x14 tc=0x1881 ; FieldEntry#3 off=0x18
 c0b690 00000000 0000c108 00000000 00000000   <- FieldEntry#3 tc=0x08c1 (uint64) ; (pad)
```

The oneof + aux table (`collectives_dma_packets`), showing fields 4/5 sharing offset `0x28` with `type_card=0x0436` and oneof-case word `0x34`, and the root `ntff_info` (20 fields, 8 aux pointers) are reproduced verbatim in the source evidence; their decodes appear in §4/§5 above. The bytes of any of the 38 tables are reproducible with:

```bash
# replay any _table_ decode straight from the binary
xxd -s 0xc0b600 -l 0xa0 \
  extracted/.../libnrt.so.2.31.24.0      # notification_buffer_size, byte-identical to above
nm libnrt.so.2.31.24.0 | rg '4ntff.*7_table_E'   # all 38 table addresses
```

---

## Verification

Every claim on this page is anchored to the live binary (build-id `8bb57aba…`) and cross-checked against the independently-recovered schema:

- **38 tables, byte-decoded.** `nm | rg '4ntff.*7_table_E'` returns exactly 38 `_table_` statics in `.data` `0xc09680..0xc0be80`; the header of each parses cleanly with the protobuf-26.1 layout (corroborated by IDA's `TcParseTableBase size=48` and the `TcParseTable<…>` template strides).
- **207 FieldEntry records, zero mismatches.** Summing `num_field_entries` over all 38 tables yields **207**, equal to the schema's 205 scalar/message + 2 `map<>` fields. Decoding each record's `type_card` and reconciling against the `FileDescriptorProto` field types gives **207/207 agreement** ([schema §5](ntff-format.md#5-the-205-vs-207-field-tension) owns this reconciliation).
- **17 type_cards, each 1:1.** The `type_card` histogram has exactly 17 distinct values; each maps to one `(proto_type, label)` pair with no aliasing (the table in §4 sums to 207).
- **The containment graph reproduces.** Following `FieldEntry.aux_idx → FieldAux[] → const TcParseTableBase*` (`nm`-resolved) rebuilds the entire ntff message tree — identical to the schema's containment tree decoded from the FDP blobs.
- **The thunks and handlers resolve.** All 38 `_InternalParse` are 12-byte `lea _table_,%rcx ; jmp 0x6a4b00` thunks (disassembly verbatim, §1); every fast-handler and the `GenericFallback` @`0x699900` fallback are `nm`/`c++filt`-confirmed protobuf `TcParser` symbols.

**Inferred / not fully traced (MED–LOW):**

- The `MapAuxInfo` packed-word bitfield split for the two `map<>` fields is decoded but only partially interpreted (low32 = `.text` map-routine region `0x55a5a`/`0x55a10`; high bits = entry layout). **Key/value wire types are HIGH** (from `MapEntry` RTTI); the exact `MapAuxInfo` layout is **MED**.
- The header `+0x09` / `+0x1C` bytes are zero in every observed table and are labeled reserved; not independently confirmed as named members beyond the IDA TIL.
- The `field_lookup_table` small-vs-large skip-block format (slow-path field-number resolution for the high-numbered/oneof fields) was spot-checked (`0xffffffff` terminator on small tables) but not fully formalized.
- The packed `field_names` tail is read only as a name spot-check; the [schema page](ntff-format.md) is the authoritative name source.

## Cross-References

- [NTFF Trace File Format (ntff.proto)](ntff-format.md) — the authoritative schema decoded from the embedded `FileDescriptorProto` blobs; owner of the message/field/enum definitions and the 205-vs-207 reconciliation that this page's wire decode confirms
- [SysTraceEventType Taxonomy (46 Variants)](event-taxonomy.md) — the `ntrace_event` producer side, whose 16 fields are byte-decoded in §4 here
- [C++ Class Hierarchy and RTTI](../forensics/rtti-class-hierarchy.md) — the `_ZTVN4ntff` vtable band that pins the 40-class roster, the `default_instance` `.bss` symbols, and the two `MapEntry` typeinfo names that fix the map key/value wire types
- [Overview: the Three Trace Producers](overview.md) — how the device-profile, sys_trace, and ntrace producers all serialize into this one schema family
- [back to index](../index.md)
