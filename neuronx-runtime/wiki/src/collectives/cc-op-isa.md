# The cc_op_entry On-Device Collective ISA

> *Emit-side addresses apply to `libnrt.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce` (build-id `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, **not** stripped, source paths in `.rodata`; `.text` VMA == file offset, every `0x23…`/`0xfe…` is an analysis VMA). Consume-side addresses apply to `libncfw.so` from the same package (build-id `a98f8e1ca2294582835310c3a1092e0a5e500db5`, symtab present, no DWARF; `.rodata` band `0x65xxx` is VMA == file offset). Source TU on the emit side is `/opt/workspace/KaenaRuntime/tdrv/encd.c`.*
> *Evidence grade: **Confirmed (byte-anchored, dual-binary)** — the wire layout is authored by libnrt's `encd` packer and reflected by libncfw's context serializer; IDA recovered the **typed `spad_ctrl_cc_op_entry_t` bit-field + union** on the emit side, and the consumer's `snprintf` decode chain matches it field-for-field modulo a fixed +1 header-byte base offset (§5, §6). Other versions will differ. · Part IX — On-Device Collectives · [back to index](../index.md)*

## Abstract

When a NeuronCore runs a collective (all-reduce, all-gather, reduce-scatter), the per-operation schedule that the on-device sync core executes is **not** instructions in the usual sense — it is a stream of fixed-size scheduler descriptors written into the SP / TopSP **scratchpad-control** (`spad_ctrl`) SRAM at NEFF load time. The atom of that stream is `spad_ctrl_cc_op_entry_t`: a 7-byte (8 with alignment) packed descriptor that names the collective algorithm family, the ring/mesh completion-handshake flags, and a 4-byte algorithm-specific union. This page is the byte-accurate spec of that descriptor — the on-device collective ISA — recovered from the two code lines that bracket it.

The descriptor is best understood by analogy to a VLIW micro-op with a tagged operand union. A leading 4-bit `algo_type` (the `enc_pattern_t` device family selector) plays the role of an opcode; `algo_sub_type` and the ring-handshake bits are modifiers; and the trailing 4 bytes are a **union** whose interpretation the opcode chooses — either a 32-bit `channel_list` ring-channel bitmap **or** a `{sema_shift_offset, sema_mask}` mesh-semaphore pair. The packing is authored by `create_spad_ctrl_entry` (`@0x232cd0` in libnrt), wrapped in a one-byte `spad_ctrl_entry` header (bit 0 = `cc_op` op-enable), and consumed two ways: by the on-device Xtensa sync-core firmware that actually executes it, and — for host-side context dumps — by libncfw's `ncfw_log_spad_ctrl_cc_op_entry` (`@0x1840`), which serializes every field to JSON. libnrt `dlopen`s libncfw and pins `libncfw_get_version() == 2` (`encd_libncfw_init @0x251cc0`), so the two libraries are version-locked to the same descriptor contract.

> **NOTE —** the JSON serializer is the cleanest available *reflection* of the firmware's read pattern, but it is not the executor. The on-device sequencer (Xtensa LX IRAM) owns the `algo_type → behavior` table and the actual ring-vs-mesh select; libncfw only mirrors the bytes. Where the firmware semantics of a flag are unknown, this page says so (§4, §7).

This page documents four artifacts a reimplementer must reproduce: (1) the **`spad_ctrl_entry` container** — `{u8 header; cc_op_entry}`, header bit 0 = op-enable; (2) the **`cc_op_entry` bit-field** — `algo_type`/`algo_sub_type`/`trigger_next` in byte +0, the ring-handshake and `safe_mode`/`unique_tensors` flags in byte +1, a padding byte +2; (3) the **4-byte union** at +3 — `channel_list` (ring) overlaying `{sema_shift_offset, sema_mask}` (mesh); and (4) the **emit == consume agreement**, exact once the +1 header base is accounted for, with the two divergences (two emitter-set flags the serializer drops) called out.

For reimplementation, the contract is:

- **The container**: `spad_ctrl_entry = {u8 header; cc_op_entry}`; `header.cc_op` (bit 0) gates whether the slot carries a collective op; the `cc_op_entry` begins at `header + 1`.
- **The `cc_op_entry` bit-field**: byte +0 `{algo_type[3:0], algo_sub_type[6:4], trigger_next[7]}`; byte +1 `{reporter[0], ring_wait_complete[1], ring_send_complete[2], safe_mode[3], unique_tensors[4], reserved[7:5]}`; byte +2 padding.
- **The union** at `cc_op_entry + 3`: `u32 channel_list` *is the same 4 bytes as* `{u16 sema_shift_offset @+0, u16 sema_mask @+2}`; `algo_type` chooses which view the device reads — `ENC_PATTERN_RING (0)` → ring, `ENC_PATTERN_MESH (1)` → mesh.
- **The cross-binary base shift**: a consumer offset `cc_op + N` equals an emitter offset `spad_ctrl_entry + N + 1`; every shared field's bit position and width matches under that shift.

| | |
|---|---|
| **Descriptor** | `spad_ctrl_cc_op_entry_t` — 7 bytes meaningful, 8 with alignment (IDA-typed) |
| **Container** | `spad_ctrl_entry` = `{spad_ctrl_entry_header_t header @+0; cc_op_entry @+1}`, `sizeof == 8` |
| **Header** | `spad_ctrl_entry_header_t` — `cc_op : 1` (op-enable), `__reserved : 7` |
| **Emit packer** | `create_spad_ctrl_entry` `@0x232cd0` (libnrt) → returns the 8-byte struct in `rax` |
| **Emit caller** | `prep_spad_ctrl_entry` (inlined in `encd_dma_mark_end @0x237200`) — CTRL / SLOT-CONTINUE marks |
| **Consume serializer** | `ncfw_log_spad_ctrl_cc_op_entry` `@0x1840` (libncfw, sunda); per-arch clones `@0x1a5e7 / 0x3338e / 0x4c135` |
| **Container serializer** | `ncfw_log_spad_ctrl_entry @0x3424` (`lea +1` before the cc_op call); header `@0x13b5` |
| **Union @ cc_op +3** | `u32 channel_list` ≡ `{u16 sema_shift_offset @+0, u16 sema_mask @+2}` (IDA-typed union) |
| **algo_type enum** | `enc_pattern_t` — `ENC_PATTERN_RING = 0`, `ENC_PATTERN_MESH = 1`, `ENC_PATTERN_INVALID = 2` |
| **Version lock** | libnrt `dlopen("libncfw.so")` + `libncfw_get_version() == 2` (`encd_libncfw_init @0x251cc0`) |
| **Byte-agreement** | emit == consume, bit-for-bit, under the `consumer +N == emitter +N+1` shift (§6) |

---

## 1. The Container Model

### Purpose

A collective op never rides alone: it is one slot in the TopSP scratchpad-control stream. The slot is `spad_ctrl_entry` — a one-byte header followed by the `cc_op_entry`. The header's bit 0, `cc_op`, is the op-enable: a slot with `cc_op == 1` carries a collective operation; the firmware (and the dumper) decode the `cc_op_entry` only when that bit is set. This mirrors a tagged-union submission slot anywhere else in the runtime — a presence byte, then the payload.

### Container layout

IDA recovered the container type directly (`spad_ctrl_entry`, `sizeof == 8`):

| offset | size | field | type | meaning |
|---|---|---|---|---|
| +0 | 1 | `header` | `spad_ctrl_entry_header_t` | `cc_op : 1` (op-enable) + `__reserved : 7` |
| +1 | 7 | *(anon)* | `cc_op_entry` | the collective scheduler descriptor (§2) |

The header type is `spad_ctrl_entry_header_t { __int8 cc_op : 1; __int8 __reserved : 7; }`. The consumer proves the +1 split structurally: `ncfw_log_spad_ctrl_entry` (`@0x3424`) calls the header serializer with the base pointer `a4`, then calls the cc_op serializer with `a4 + 1`:

```c
// ncfw_log_spad_ctrl_entry @0x3424 (libncfw) — the +1 base split
function log_spad_ctrl_entry(buf, indent, key, entry):       // entry = a4
    log_header(buf, indent+2, "header", entry);              // @0x13b5 — reads entry[0]
    log_cc_op(buf, indent+2, "cc_op", entry + 1);            // @0x1840 — reads (entry+1)[...]
    // ... closing braces
```

The header serializer reads exactly bit 0:

```c
// ncfw_log_spad_ctrl_entry_header @0x13b5 — header.cc_op
v29 = *a4 & 1;                                                // movzbl(a4); & 1
snprintf(..., "%u", v29);  // under key "cc_op"
```

On the emit side the same container is asserted, not re-derived: `prep_spad_ctrl_entry` gates every store with `curr_spad_ctrl->header.cc_op == 1 && spe->spad_slot_used[0] != 0 && spe->op_cnt == op_idx + 1` (`@0x804028`, `encd.c:0xC68`) and `spad_ctrl->header.cc_op == 1 && slot_spad_section->size_n != 0` (`@0x8043d8`, `encd.c:0xBF9`), and the packer sets `cc_op` to 1 unconditionally (`or $0x1` at `0x232d81`, §3).

> **NOTE —** the only non-bounds-check branch in the whole cc_op serializer is the leading `if (*a3)` "is a JSON key present?" test. There is **no** branch on `algo_type` anywhere in the serializer — the union is dumped unconditionally (§3). The container header's `cc_op` test lives one level up, in the firmware's slot walk, not in the descriptor decode.

---

## 2. The `cc_op_entry` Bit-Field

### Purpose

`cc_op_entry` is the collective scheduler descriptor proper. Byte +0 is the op selector — a 4-bit algorithm family, a 3-bit sub-variant, and a trigger-next chaining bit. Byte +1 is the completion-handshake flag byte — who reports, and the ring send/recv wait conditions. Byte +2 is padding. Bytes +3..+6 are the algorithm union (§3). IDA recovered the full typed bit-field; this is the authoritative layout, with the emit packer and the consume serializer as the two independent confirmations.

### Layout (offsets relative to `cc_op_entry` base = `header + 1`)

All bit positions are confirmed on **both** binaries (the `Evidence` column gives `P` = libnrt packer `create_spad_ctrl_entry @0x232cd0`, `E` = libnrt CTRL-mark decode `@encd_dma_mark_end`, `C` = libncfw serializer `@0x1840`). Confidence **HIGH** for every row except where noted.

| offset | bit(s) | width | field | meaning | evidence |
|---|---|---|---|---|---|
| +0 | 3:0 | 4 | `algo_type` | device algorithm family (`enc_pattern_t`); selects the union view | C `*p & 0xF`; E `v64[1] & 0xF`; P `algo<<8` of word0 |
| +0 | 6:4 | 3 | `algo_sub_type` | sub-variant within the family (firmware-owned meaning) | C `(*p>>4)&7`; E `(v64[1]>>4)&7`; P `sub<<12` (`shl $0xc`) |
| +0 | 7 | 1 | `trigger_next` | auto-trigger the next op when this one completes | C `(*p>>7>>7)&1`; E `v64[1]>>7`; P `trig<<15` (`shl $0xf`) |
| +1 | 0 | 1 | `reporter` | this op reports completion (TPB completion notify) | C `*(p+1)&1`; E `v64[2]&1`; P `or %r13d` (bit 0) |
| +1 | 1 | 1 | `ring_wait_complete` | ring: wait for **recv**-complete before send | C `(*(p+1)>>1)&1`; P `lea(%r9,%r9)` = `r9<<1` |
| +1 | 2 | 1 | `ring_send_complete` | ring: wait for **send**-complete | C `(*(p+1)>>2)&1`; P `or %r15d` (`r15<<2`) |
| +1 | 3 | 1 | `safe_mode` | TopSP "safe-mode" handshake flag (emitter-set; serializer-dropped) | P `or %esi` (`esi<<3`); E mark `(hdr>>19)&1` |
| +1 | 4 | 1 | `unique_tensors` | unique-tensors handshake flag (emitter-set; serializer-dropped) | P `or %ecx` (`ecx<<4`) |
| +1 | 7:5 | 3 | `__reserved0` | preserved from prior init (`& 0xE0` merge of old byte) | P `and $0xffffffe0` of old +2 byte |
| +2 | — | 8 | `__reserved` | padding (`uint8_t[1]`); unread by both sides | IDA struct; no C/E read, no P write |
| +3..+6 | — | 32 | *(union)* | ring `channel_list` **or** mesh `{sema_shift_offset, sema_mask}` (§3) | — |

> **CORRECTION (CCOP-1) —** an earlier survey labeled byte +1 bits 3 and 4 generic "extra flags" sourced from a config struct, and treated `safe_mode` as an emit-time *log-only* value not present in the wire descriptor. The IDA-recovered `spad_ctrl_cc_op_entry_t` type names them outright: **bit 3 = `safe_mode`, bit 4 = `unique_tensors`** — both are packed wire bits. The CTRL-mark logger reads `safe_mode` straight out of the descriptor word as `(*(_DWORD *)&spad_ctrl_entry.header >> 19) & 1` — bit 19 of the dword = byte +2 bit 3 of `spad_ctrl_entry` = byte +1 bit 3 of `cc_op_entry`. It is a descriptor bit, not a side value. In the packer they come from `sp_engine->ctx->permute_chain` (→ `safe_mode`) and `->unique_tensors` (→ `unique_tensors`).

### Algorithm — the byte +0 / byte +1 pack

The packer builds the descriptor as a 16-bit word (header + byte +0) and a separate flag byte (+1), then stores the union. The word and flag merges preserve the high reserved bits of whatever was already in the slot:

```c
// create_spad_ctrl_entry @0x232cd0 (libnrt) — the word + flag pack (@0x232d62..0x232dbc)
function create_spad_ctrl_entry(channel, mark_first, mark_continue, sema_shift_offset, sema_mask):
    assert(channel->id != 0 || channel->reporter);          // @0x803520, encd.c:0xB31
    reporter = channel->reporter;
    // ... ring/mesh discovery fills algo_type(v18), algo_sub_type(v19),
    //     trigger_next(pipeline_stage), and the union (below) ...

    // word0 = header(byte+0) | cc_op_entry.byte+0:
    word = (algo_type << 8) | 1                              // 0x232d78 shl $8; 0x232d81 or $1 (cc_op=1)
    word |= (algo_sub_type << 12)                            // 0x232d7b shl $0xc
    word |= (trigger_next << 15)                             // 0x232d7e shl $0xf
    word |= (old_word & 0xFE)                                // 0x232d96 and $0xfe — keep old low bits but cc_op
    store16(slot + 0, word)                                  // 0x232da2 mov %ax,(%rsp)

    // byte+1 (= slot byte+2) flags, masked to 5 bits, merged into reserved[7:5]:
    flag  = reporter                                         // bit 0   (0x232daa or %r13d)
    flag |= (ring_wait_complete << 1)                        // bit 1   (0x232da6 lea(%r9,%r9))
    flag |= (ring_send_complete << 2)                        // bit 2   (0x232db0 or %r15d, r15<<2)
    flag |= (safe_mode << 3)                                 // bit 3   (0x232db3 or %esi,  esi<<3)
    flag |= (unique_tensors << 4)                            // bit 4   (0x232db5 or %ecx,  ecx<<4)
    flag &= 0x1F                                             // 0x232db7 and $0x1f
    flag |= (old_flag & 0xE0)                                // 0x232dad and $0xffffffe0 — keep reserved[7:5]
    store8(slot + 2, flag)                                   // 0x232dbc mov %al,0x2(%rsp)

    store16(slot + 4, union_lo); store16(slot + 6, union_hi) // 0x232d6c/0x232d72 (§3)
    return slot                                              // 8-byte struct in rax
```

> **QUIRK —** `algo_type` is written into bits 8..11 of `word` (`v18 << 8`), and `word` covers slot bytes +0/+1 — so `algo_type` lands in slot byte +1 = `cc_op_entry` byte +0, bits 0..3. The `or $0x1` at `0x232d81` is the **header** `cc_op` bit (slot byte +0, bit 0), packed into the *same 16-bit store* as the cc_op_entry's first byte. A reimplementer who treats the header and the cc_op_entry as two separate `u8` writes is also correct on the wire; the reference packs them as one aligned `u16`.

### The consumer double-shift artifact

The serializer's decode of `algo_sub_type` is `((*p >> 4) & 7) >> 4) & 7` (`@0x1840`), and `trigger_next` is `(*p >> 7 >> 7) & 1`. The trailing `>> 4` / second `>> 7` operate on an already-isolated small value and are no-ops:

```text
algo_sub_type : ((b0 >> 4) & 7)        // first  shr4;&7 isolates bits[6:4]
              then >> 4 & 7            // second shr4;&7 on a <8 value == identity
              net = (b0 >> 4) & 7
trigger_next  : (b0 >> 7) >> 7 & 1     // second shr7 on a 0/1 value == identity
              net = (b0 >> 7) & 1
```

> **NOTE —** this is compiler redundancy in the serializer, not a layout fact. The emit side (`v64[1]>>4 &7`, `v64[1]>>7`) and the IDA-typed widths (`algo_sub_type : 3`, `trigger_next : 1`) confirm the net fields. Do **not** reimplement the double shift. (HIGH)

---

## 3. The Algorithm Union (`cc_op_entry + 3`)

### Purpose

The last four bytes are the algorithm-specific operands. A ring op needs a 32-bit bitmap of the ring channels active on this TopSP; a mesh op needs a 16-bit semaphore shift offset plus a 16-bit semaphore mask. These never coexist, so they share storage. IDA recovered the union as two 4-byte views inside an anonymous union at struct offset +3.

### Union layout

| view | offset (rel. cc_op) | field | type | meaning |
|---|---|---|---|---|
| **ring** | +3 | `channel_list` | `channel_list_t` (u32) | bitmap of active ring channels on this TopSP |
| **mesh** | +3 | `sema_shift_offset` | `uint16_t` | semaphore shift offset |
| **mesh** | +5 | `sema_mask` | `uint16_t` | semaphore mask |

i.e. `channel_list[31:0]` exactly overlays `{ sema_shift_offset[15:0] @ low half, sema_mask[15:0] @ high half }`. The emit packer writes the union with the same typed accessors IDA recovered:

```c
// create_spad_ctrl_entry @0x232cd0 — the union write (both branches)
// RING branch (algo_type = ENC_PATTERN_RING): build channel_list bitmap
for each active ring channel c whose sp_engine maps to this TopSP idx:    // get_sp_engine_idx @0x2312b0
    v24 |= 1 << c.abs_id                                   // channel_list bitmap
v49.cc_op.ring.channel_list = v24                          // u32 store (slot +4)

// MESH branch (algo_type = ENC_PATTERN_MESH): pass through sema params
v49.cc_op.mesh.sema_shift_offset = sema_shift_offset       // u16 store (slot +4)
v49.cc_op.mesh.sema_mask         = sema_mask               // u16 store (slot +6)
```

Both branches feed the *same* `mov %r12w, 0x4(%rsp)` / `mov %r8w, 0x6(%rsp)` store pair (`0x232d6c`/`0x232d72`): in the ring branch the low and high halves of `channel_list` ride `r12w`/`r8w`; in the mesh branch `r12w = sema_shift_offset`, `r8w = sema_mask`. One store pair, two interpretations — the union, proven from the instruction stream.

The consumer reads the **same bytes both ways, unconditionally** (no `algo_type` branch):

```c
// ncfw_log_spad_ctrl_cc_op_entry @0x1840 — unconditional union dump (p = cc_op base)
// "ring": { channel_list }
v66 = *(_DWORD  *)(p + 3);   snprintf(..., "%u",  v66);    // u32 @ cc_op+3
// "mesh": { sema_shift_offset, sema_mask }
v83 = *(unsigned __int16 *)(p + 3);  snprintf(..., "%hu", v83);  // u16 @ cc_op+3
v92 = *(unsigned __int16 *)(p + 5);  snprintf(..., "%hu", v92);  // u16 @ cc_op+5
```

### Union proof (HIGH, both binaries)

```text
emit  : one store pair  store16(slot+4) ; store16(slot+6)   (slot = spad_ctrl_entry)
        ring : r12w/r8w = channel_list[15:0] / [31:16]
        mesh : r12w/r8w = sema_shift_offset  / sema_mask
consume: u32  @ cc_op+3   == channel_list           (slot+4 absolute)
         u16  @ cc_op+3   == sema_shift_offset       (slot+4 absolute)
         u16  @ cc_op+5   == sema_mask               (slot+6 absolute)
  cc_op+3 (consumer) == spad_ctrl_entry+4 (emitter) == header+4 absolute.  SAME RANGE.
⇒ channel_list[31:0]  ≡  { sema_shift_offset[15:0] @low, sema_mask[15:0] @high }
```

> **QUIRK —** the libncfw dumper *always* emits **both** a `"ring": { channel_list }` object and a `"mesh": { sema_shift_offset, sema_mask }` object for every `cc_op` slot — it is an unconditional union reflection, **not** a tagged select. A reader of the JSON context dump must apply `algo_type` themselves to know which object is live: `ENC_PATTERN_RING (0)` ⇒ the `ring` object is meaningful and `mesh` is a reinterpretation of the same bytes; `ENC_PATTERN_MESH (1)` ⇒ vice-versa. The actual select happens on the device, which reads `algo_type` and dereferences one view.

---

## 4. Enums and the Two Algorithm Axes

There are **two** independent algorithm enumerations, and conflating them is the trap. The 4-bit `algo_type` packed in the descriptor is the **device** family (`enc_pattern_t`); it is *not* the host-side `enc_alg_type` that the composer dispatches on.

### `algo_type` — the device family (`enc_pattern_t`, 4-bit field)

IDA recovered the enum directly:

| value | `enc_pattern_t` | union view |
|---|---|---|
| 0 | `ENC_PATTERN_RING` | ring (`channel_list`) |
| 1 | `ENC_PATTERN_MESH` | mesh (`sema_shift_offset`, `sema_mask`) |
| 2 | `ENC_PATTERN_INVALID` | — |

> **CORRECTION (CCOP-2) —** an earlier note guessed `RING = 1 / MESH = 2` (from a sibling cell) and read the packer's `algo_type<<8` constant-1 path as "RING = 1". The recovered `enc_pattern_t` enum is **`RING = 0, MESH = 1, INVALID = 2`**. The field is 4 bits and the device select is binary ring-vs-mesh; the numeric values are now pinned from the enum, not inferred. The `<<8` constants in the packer are data-driven by the ring/mesh discovery branches, not a fixed "RING" tag.

### `enc_alg_type` — the host composer axis (separate, 11 valid (0..10) + INVALID=11 sentinel)

The host runtime picks a *composer/scheduler* from a wider algorithm enum, and `enc_get_algorithm_name` (`@0xfef30`) names it. This axis is **not** a `cc_op_entry` field — it selects which composer builds the ccop stream, after which each op carries only the 4-bit device `algo_type`. Verbatim from the recovered switch and the `.rodata` string block at `0x840d11` (`"Ring\0Hier\0Bw Optimal Mesh\0Kangaring\0Single Step Mesh\0UltraServer Mesh\0Invalid\0"`):

| `enc_alg_type` | value | name | string |
|---|---|---|---|
| `ENC_ALG_RING` | 0 | Ring | `0x840d11` |
| `ENC_ALG_HIER` | 1 | Hier | `0x840d16` |
| `ENC_ALG_MESH` | 2 | Mesh | `0x840d26` |
| `ENC_ALG_KANGARING` | 3 | Kangaring | `0x840d2b` |
| `ENC_ALG_SINGLE_CYCLE_RING` | 4 | *(→ Invalid name)* | `0x840d57` |
| `ENC_ALG_INTRA_RDH` | 5 | RDH | `0x84278d` |
| `ENC_ALG_SINGLE_STEP_MESH` | 6 | Single Step Mesh | `0x840d35` |
| `ENC_ALG_INTER_RDH` | 7 | RDH | `0x84278d` |
| `ENC_ALG_TWO_STEP_POD_MESH` | 8 | UltraServer Mesh | `0x840d46` |
| `ENC_ALG_LATENCY_OPT_MESH` | 9 | Mesh | `0x840d26` |
| `ENC_ALG_BW_OPT_MESH` | 10 | Bw Optimal Mesh | `0x840d1b` |
| `ENC_ALG_INVALID` | 11 | Invalid | `0x840d57` |

A third, topology axis — `metaring_type` — derives from `enc_alg_type` via `enc_get_metaring_type` (`@0xfc860`): `RING = 0` for alg ≤ 3 except `KANGARING (3) → KANGARING (1)`, `SINGLE_CYCLE_RING (4) → SINGLE_CYCLE_RING (2)`, `INTER_RDH (7) → RDH (3)`; anything else asserts `meta_ring_type < INVALID_METARING`. Recorded for context; not a `cc_op_entry` field. (HIGH)

> **GOTCHA —** three enums, three roles. `algo_type` (device, `enc_pattern_t`, RING/MESH) is the **only** one in the wire descriptor. `enc_alg_type` (host, 11 valid (0..10) + INVALID=11 sentinel) picks the composer. `metaring_type` (topology) is a derived label. A reimplementer who packs the host `enc_alg_type` into the 4-bit `algo_type` field will overflow it and mis-select the device union. The descriptor only ever carries the binary device pattern.

### `algo_sub_type` (3-bit) and reserved bits

`algo_sub_type` is a 3-bit sub-variant whose value→meaning table lives in the firmware IRAM and is opaque to both host binaries; the packer writes it via the ring/mesh discovery branches (`v19`, e.g. constant `3` on one mesh path). The byte +1 reserved bits `[7:5]` and the byte +2 padding are confirmed unread by the consumer and not written by the packer's flag merge. (`algo_sub_type` value semantics: **MED**; widths and packing: **HIGH**.)

---

## 5. Emit and Consume Chains

### Emit — libnrt `encd` (producer)

The packer is called from the descriptor-emit funnel that walks each collective op and stages its TopSP scratchpad-control slot:

```text
encd_dma_mark_end (0x237200)                              ── op-stream emitter
  └─ prep_spad_ctrl_entry (inlined)                       ── per-op slot stager
       ├─ assert header.cc_op == 1 && spad_slot_used[0]   ── 0x804028 / encd.c:0xC68
       ├─ create_spad_ctrl_entry (0x232cd0)               ── PACK the 8-byte struct
       │    ├─ assert channel->id != 0 || channel->reporter   (0x803520, encd.c:0xB31)
       │    ├─ get_sp_engine_idx (0x2312b0)               ── map channel→TopSP idx for channel_list
       │    └─ build word0 + flag + union, return in rax
       └─ nlog_write (CTRL mark / SLOT CONTINUE mark)     ── 0x804088 / 0x804198 (debug reflection)
  (enc_validate_and_merge_ccops 0x132b80 + enc_op_list:: 0x10cd50 + enc_fnc:: 0xf5940
   merge/validate the ccop stream before scheduling — boundary, not bit-decoded here)
```

The CTRL-mark logger decodes the just-packed struct (with `v64` = `spad_ctrl_entry` base, `%rbp`), and its offsets are the cross-binary Rosetta stone — they are exactly the consumer offsets **plus one**:

```c
// prep_spad_ctrl_entry CTRL-mark decode @encd_dma_mark_end (v64 = spad_ctrl_entry base)
alg               = v64[1] & 0xF                  // spad_ctrl_entry+1 == cc_op+0  bits[3:0]
subalg            = (v64[1] >> 4) & 7             // +1 == cc_op+0  bits[6:4]
trignext          =  v64[1] >> 7                  // +1 == cc_op+0  bit 7
chlist            = *((u32 *)v64 + 1)             // +4 == cc_op+3  (channel_list)
reporter          =  v64[2] & 1                   // +2 == cc_op+1  bit 0
sema_shift_offset = *((u16 *)v64 + 2)             // +4 == cc_op+3
sema_mask         = *((u16 *)v64 + 3)             // +6 == cc_op+5
safe_mode         = (*(u32 *)&entry.header >> 19) & 1   // bit 19 == +2 bit3 == cc_op+1 bit3
```

The CTRL-mark format string is verbatim at `0x804088`:
`"[nec_dev %2u, TOPSP %d, op %d] CTRL mark -alg %d-subalg %d-trignext %d-chlist %d-reporter %d-sema_shift_offset %u-sema_mask %u-safe_mode %d"`. The multi-function variant is at `0x804490`. The **SLOT-CONTINUE** variants (`0x804198` / `0x804538`) drop the `reporter` field and read the cc_op at a different container offset — the continuation slot stores the `spad_ctrl_entry` at `prev_final_entry + 8`, so the marks read `+9 / +12 / +14` (= the same `cc_op` layout, shifted by the +8 container base). The continuation marker constant is `SPAD_SLOT_CONTINUE_MARK = 0xFFFEFFFE00000000` (`encd.c:0xC87`).

### Consume — libncfw (serializer)

The host-side context dumper walks the op context and serializes each scratchpad-control slot to JSON. There are four per-arch clones, dispatched by a coretype front:

```text
{sunda,cayman,mariana,mariana_plus}_ncfw_ctx_log (0x1a12b / 0x32ed2 / 0x4bc79 / 0x64a20)
  └─ ncfw_log_op_ctx (0x150a2 …)
       └─ ncfw_log_spad_ctrl_entry (0x3424)              ── lea +1 before cc_op call
            ├─ ncfw_log_spad_ctrl_entry_header (0x13b5)   ── header.cc_op (entry[0] & 1)
            └─ ncfw_log_spad_ctrl_cc_op_entry (0x1840)    ── full cc_op decode (entry+1)
                 → algo_type, algo_sub_type, trigger_next, reporter,
                   ring_wait_complete, ring_send_complete,
                   then "ring":{channel_list}, then "mesh":{sema_shift_offset, sema_mask}
```

| clone | address | arch | string band |
|---|---|---|---|
| `ncfw_log_spad_ctrl_cc_op_entry` | `0x1840` | SUNDA | `0x65005…0x650cd` |
| `ncfw_log_spad_ctrl_cc_op_entry_0` | `0x1a5e7` | (clone) | `0x656a6…` |
| `ncfw_log_spad_ctrl_cc_op_entry_1` | `0x3338e` | CAYMAN | `0x65d3b…` |
| `ncfw_log_spad_ctrl_cc_op_entry_2` | `0x4c135` | MARIANA(+) | `0x663d0…` |

The four clones are byte-identical in decode; they differ only in which arch's context they are reached from (the `*_ncfw_ctx_log` dispatch front). The serializer keys live in `.rodata`, repeated four times (one band per arch). The first-band keys, byte-anchored:

```text
0x65005 "\"%s\": {\n"   0x6500e "{\n"          0x65011 "\"cc_op\""      0x6501e "%u"
0x6502a "\"algo_type\""        0x65039 "\"algo_sub_type\""   0x65049 "\"trigger_next\""
0x65058 "\"reporter\""         0x65063 "\"ring_wait_complete\""  0x65078 "\"ring_send_complete\""
0x6508d "ring"  0x65092 "\"channel_list\""
0x650a1 "mesh"  0x650a6 "\"sema_shift_offset\""  0x650ba "%hu"  0x650be "\"sema_mask\""
0x650cd "header"  0x650d4 "cc_op"   (keys passed from the container serializer to the two children)
```

> **QUIRK —** `channel_list` (a u32) is printed with `%u`, but `sema_shift_offset` / `sema_mask` (u16 each) are printed with `%hu`. The format-specifier width *is* the field-width tell: the dumper truncates the union to 16-bit halves on the mesh path and reads the full 32 bits on the ring path. The emit-side CTRL mark uses `%u` for all three (`sema_shift_offset %u`, `sema_mask %u`) — a cosmetic logger difference, not a wire difference; the underlying loads are `movzwl` (16-bit) on both sides.

---

## 6. Emit ↔ Consume Byte-Agreement

The descriptor is authored by libnrt and reflected by libncfw, and the two agree field-for-field once the +1 header base is applied. The consumer's `cc_op` base pointer is `spad_ctrl_entry + 1`; the emitter's CTRL-mark base is `spad_ctrl_entry + 0`. So `consumer +N == emitter +N+1`.

| field | consumer offset (cc_op base) | emitter offset (spad_ctrl_entry base) | bit / width | agree |
|---|---|---|---|---|
| `header.cc_op` | +(-1) bit0 | +0 bit0 | 1 | **yes** (`& 1` both) |
| `algo_type` | +0 [3:0] | +1 [3:0] | 4 | **yes** (`& 0xF`) |
| `algo_sub_type` | +0 [6:4] | +1 [6:4] | 3 | **yes** (`>>4 & 7`) |
| `trigger_next` | +0 [7] | +1 [7] | 1 | **yes** (`>>7`) |
| `reporter` | +1 [0] | +2 [0] | 1 | **yes** (`& 1`) |
| `ring_wait_complete` | +1 [1] | +2 [1] | 1 | **yes** (P `r9<<1`) |
| `ring_send_complete` | +1 [2] | +2 [2] | 1 | **yes** (P `r15<<2`) |
| `channel_list` (ring) | +3 u32 | +4 u32 | 32 | **yes** (header+4 absolute) |
| `sema_shift_offset` (mesh) | +3 u16 | +4 u16 | 16 | **yes** |
| `sema_mask` (mesh) | +5 u16 | +6 u16 | 16 | **yes** |

> **CORRECTION (CCOP-3) — the +1 base shift is a header byte, not a layout disagreement.** The emit-side decode reads byte `+1/+2/+4/+6` of the `spad_ctrl_entry`; the consume-side reads byte `+0/+1/+3/+5` of the `cc_op_entry`. These are the *same physical bytes* — the emitter measures from the slot (which includes the 1-byte header), the consumer measures from `header + 1` (the serializer is called with `entry + 1`, §1). Subtract one and every offset, bit position, and width matches. Anyone reconciling the two binaries who forgets the header byte will see a phantom one-byte skew on every field.

### Divergences

Two fields are packed by the emitter but **not** serialized by libncfw — the consumer dumps only `reporter` / `ring_wait_complete` / `ring_send_complete` (bits 0/1/2) of byte +1, never bits 3/4:

| field | emitter | consumer JSON | note |
|---|---|---|---|
| `safe_mode` (byte+1 bit3) | packed (P `esi<<3`); printed in CTRL mark | **dropped** | wire bit present; serializer just doesn't reflect it |
| `unique_tensors` (byte+1 bit4) | packed (P `ecx<<4`) | **dropped** | wire bit present; not even in the CTRL-mark format |

Both are real descriptor bits the firmware can read; the JSON dumper is simply a partial reflection. (HIGH that they are packed; the firmware semantics of each are firmware-owned — §7.) This is the inverse of the union situation: there the serializer emits *more* than any single op uses (both views); here it emits *less* than the packer writes (two flags hidden).

---

## 7. Considerations

- **On-device execution semantics.** The Xtensa sync-core IRAM owns the `algo_type → behavior` table, the `algo_sub_type` value→meaning map, the ring-vs-mesh union select, and the runtime effect of `trigger_next` / `reporter` / `ring_wait_complete` / `ring_send_complete` / `safe_mode` / `unique_tensors`. The two host binaries pack and reflect the bits; they do not execute them. The executor is out of scope here — see [The NCFW Sequencer](../firmware/ncfw-sequencer.md) and the ext-ISA microcode. *(behavior: LOW/UNKNOWN; bit layout: HIGH.)*
- **The two hidden flags' provenance.** `safe_mode` ← `sp_engine->ctx->permute_chain`, `unique_tensors` ← `sp_engine->ctx->unique_tensors` (read in `create_spad_ctrl_entry`). The TopSP/SP context fields that feed them are characterized in the encd context layer, not here. *(MED on the source mapping; HIGH that the bits are packed from `ctx`.)*
- **`channel_list` construction.** The ring bitmap is `OR`-of `(1 << channel.abs_id)` over each active ring channel whose `sp_engine` maps to *this* TopSP `idx` (via `get_sp_engine_idx`, `@0x2312b0`). The kangaring path uses `kangaring_active_channel_n` channels; both fold into the same `channel_list` u32. The per-channel `abs_id → bit` mapping and the channel-descriptor structure are owned by [The 148-Byte Ring Channel Descriptor](channel-descriptor.md).
- **Sema-shift validation.** A separate per-rank check, `"[nec_dev %u] mismatching sema_shift_offset parameter with rank idx %d"` (`@0x7ec9a8`), validates the mesh `sema_shift_offset` against rank index before emission. Its rule is not bit-decoded here.
- **The `cc_op` stream merge.** `enc_validate_and_merge_ccops` (`@0x132b80`, plus `enc_op_list::` `@0x10cd50` and `enc_fnc::` `@0xf5940`) merges/validates adjacent ccops (how `channel_list` / `trigger_next` combine across ops) before scheduling — a boundary not decoded on this page.
- **Version lock.** libnrt `dlopen`s libncfw and refuses to proceed unless `libncfw_get_version() == 2` (`encd_libncfw_init @0x251cc0`, which then resolves `libncfw_get_image` + `libncfw_ctx_log`). The descriptor contract is therefore pinned across the two libraries; a layout change would bump that version.

---

## Related Components

| Name | Relationship |
|---|---|
| `create_spad_ctrl_entry` (`@0x232cd0`, libnrt) | The packer that authors the 8-byte `spad_ctrl_entry` / `cc_op_entry` |
| `prep_spad_ctrl_entry` (inlined in `encd_dma_mark_end @0x237200`) | The per-op stager; emits the CTRL / SLOT-CONTINUE marks |
| `ncfw_log_spad_ctrl_cc_op_entry` (`@0x1840`, libncfw) | The consumer-side serializer; unconditional union dump |
| `ncfw_log_spad_ctrl_entry` (`@0x3424`) / `_header` (`@0x13b5`) | The container serializer and the header (`cc_op` enable) decode |
| `enc_get_algorithm_name` (`@0xfef30`) / `enc_get_metaring_type` (`@0xfc860`) | The *host* `enc_alg_type` / `metaring_type` axes — distinct from device `algo_type` |
| `encd_libncfw_init` (`@0x251cc0`) | The `dlopen` bridge that version-locks emitter and consumer (`get_version == 2`) |

## Cross-References

- [Serializer Families (per-arch CC-context dumpers)](../firmware/serializer-families.md) — the four `ncfw_ctx_log` arch clones that reach this `cc_op` serializer, and the per-arch `.rodata` key bands
- [encd: Device-Side Descriptor Emitter](encd-overview.md) — the `encd_dma_mark_end` op-stream emitter that calls `create_spad_ctrl_entry`
- [The 148-Byte Ring Channel Descriptor](channel-descriptor.md) — the `channel` whose `abs_id` / `reporter` / `id` fields drive `channel_list` and the byte +1 flags
- [Algorithm Taxonomy (Ring / Mesh / Hier / Kangaring / RDH)](algorithm-taxonomy.md) — the host `enc_alg_type` composer axis that selects which ccop stream gets built
- [The NCFW Sequencer (Xtensa LX Disassembly)](../firmware/ncfw-sequencer.md) — the on-device executor that owns the `algo_type` / `algo_sub_type` behavior tables and the union select
- [SDMA / CCE / TDG Meta-Control Overlays](../dma/meta-ctrl-overlays.md) — the sibling "two C overlays on one word" pattern in the DMA descriptor's `meta_ctrl`
- [The 16-Byte Descriptor Wire Format](../dma/descriptor-format.md) — the DMA descriptor stream these collective ops schedule onto
- [back to index](../index.md)
