# NCFW spad-ctrl `cc_op` Table + tsync

The NCFW management core (a scalar Xtensa-LX control core with no shipped disassembler
config) does not interpret a wire opcode stream. It walks a **SPAD control table** — an
array of 8-byte `spad_ctrl_entry` records the host stages in HBM and DMA-loads onto each
TOP_SP. **The `cc_op` table *is* the command set**: each entry's `algo_type` nibble selects
which collective algorithm (ring / mesh / hierarchical / kangaring / …) the firmware runs
for that step, and the byte-packed control word carries the per-step chain, completion, and
channel/semaphore payload. Beside the table sits the device **tsync** struct — the
clock-alignment surface that keeps the dies' timestamp counters coherent under the barriers.

Every byte offset, shift, and mask on this page is recovered two ways and cross-checked:

* The **host-side JSON pretty-printers** in `libncfw.so` (`ELF64 x86-64`, **not stripped**,
  615 640 B; SHA-256 `598920d7…aa3e49`; BuildID `a98f8e1c…db5`; SONAME
  `libncfw.so.2.31.1.0.cf13a49f` — all four confirmed) walk the exact runtime
  structs the LX core builds. They are the device-data ground truth.
* The **host packer + struct DB** in `libnrt.so.2.31.24.0` (`.text` VMA==fileoffset) builds
  the same records. `create_spad_ctrl_entry @0x232cd0` writes them; the IDA-recovered
  `spad_ctrl_entry` / `spad_ctrl_cc_op_entry_t` struct fixes the bitfields and the **total
  size = 8 B**.

> **NOTE — section offsets.** In `libncfw.so`, `.text` (`0x10c0`) and `.rodata` (`0x65000`)
> are VMA==fileoffset; `.data` carries a `0x1000` delta (VMA `0x95020` / fileoffset
> `0x94020`). Every decoder address and string offset below lives in `.text`/`.rodata`, so
> the delta never applies here. `libncfw` uses plain **C** function bodies (no C++ vtables),
> so the `_ZTV+0x10` rule is irrelevant. All counts are `nm`/`objdump`-grounded, never
> grepped from a decompile.

The page default is `[OBSERVED HIGH]` (read from bytes/disasm); claims that depart
from it carry an explicit tag — **HIGH / MED / LOW** × **OBSERVED** / **INFERRED**
(deduced from names + sibling pages) / **CARRIED** (asserted by a sibling page,
restated here).

---

## 1 · The decoder family — 5 pretty-printers × 4 arch copies

`nm -nS libncfw.so` locates five decoders, each cloned once per arch generation. The v2/sunda
copies (others are exact strided clones, §7):

| `@` (v2/sunda) | size | symbol | role |
|---|---|---|---|
| `0x3424` | `0x3cd` | `ncfw_log_spad_ctrl_entry` | wrapper: `{header}` + `{cc_op}` (§2) |
| `0x13b5` | `0x48b` | `ncfw_log_spad_ctrl_entry_header` | the 1-byte header (`cc_op` flag) (§2.2) |
| `0x1840` | `0x1be4` | `ncfw_log_spad_ctrl_cc_op_entry` | the `cc_op` command word (§3) |
| `0xad33` | `0x163e` | `ncfw_log_configs_dev_tsync` | the device tsync struct (§5) |
| `0xc371` | `0xa90` | `ncfw_log_dev_configs` | tsync parent + packed device ids (§5.2) |

`[OBSERVED HIGH — `nm -nS` sizes match exactly.]`

The call graph (under `libncfw_ctx_log`, the umbrella JSON dumper):

```
ncfw_log_op_ctx @0x150a2                       ← the LIVE executing op
 └─ @0x1529e  call ncfw_log_spad_ctrl_entry    (key "ctrl_entry" @0x65588)   ← ONLY caller
       ├─ @0x35ce  call ncfw_log_spad_ctrl_entry_header   (key "header" @0x650cd, entry+0)
       └─ @0x35ef  call ncfw_log_spad_ctrl_cc_op_entry    (key "cc_op"  @0x650d4, entry+1)

ncfw_log_dev_configs @0xc371
 └─ @0xc52c  call ncfw_log_configs_dev_tsync    (key "tsync" @0x65319)
```

The wrapper has **exactly one** caller (`ncfw_log_op_ctx @0x1529e`; `objdump | rg -c 'call
+3424'` → `1`): the `cc_op` command entry is dumped as part of the live op context — the
collective currently executing, passed in as the 4th arg (`rcx`). `[OBSERVED HIGH.]`

---

## 2 · The `spad_ctrl_entry` wrapper + the 1-byte header

### 2.1 · `ncfw_log_spad_ctrl_entry @0x3424` — header@+0, `cc_op`@+1

The wrapper opens the JSON object, decodes the header from `entry+0`, then decodes the
`cc_op` record from `entry+1`. The `+1` is the definitive `lea [rax+0x1]`:

```asm
; ncfw_log_spad_ctrl_entry(rdi=buf, esi=indent, rdx=key, rcx=entry_ptr)
35b6:  mov  rdx,[rbp-0x50]          ; rdx = entry_ptr
35c1:  mov  rcx,rdx                 ; rcx = entry_ptr + 0
35c4:  lea  rdx,[rip+0x61b02]       ; # 650cd  "header"
35ce:  call ncfw_log_spad_ctrl_entry_header        ; decode header @ entry+0
35d3:  mov  rax,[rbp-0x50]
35d7:  lea  rdx,[rax+0x1]           ; <<< rdx = entry_ptr + 1   (cc_op word)
35e2:  mov  rcx,rdx
35e5:  lea  rdx,[rip+0x61ae8]       ; # 650d4  "cc_op"
35ef:  call ncfw_log_spad_ctrl_cc_op_entry         ; decode cc_op @ entry+1
```

Output: `{ "header": { "cc_op": N }, "cc_op": { … } }`. `[OBSERVED HIGH — `lea [rax+0x1]` @0x35d7.]`

This matches the `libnrt` struct DB exactly: `spad_ctrl_entry` (ordinal 9350, **size 8**) =
`header` (`spad_ctrl_entry_header_t`, 1 B, offset 0) + an anonymous 7-byte union at offset 1.

### 2.2 · `ncfw_log_spad_ctrl_entry_header @0x13b5` — `cc_op` = byte0 bit0

The header is a **single byte**. The decoder reads exactly one field (the only `and 0x1` on
an entry byte in the whole function):

```asm
1548:  mov   eax,[rbp-0x60]         ; eax = (low 32b of) entry_ptr (header)
154b:  movzx eax,BYTE PTR [rax]     ; byte0
154e:  and   eax,0x1               ; bit0
1551:  movzx eax,al                ; -> "cc_op": %u   (key @0x65011, fmt "%s: %u")
```

`header.cc_op = byte0[0]` — the 1-bit "this entry is an active collective op" flag. The
struct DB names it `spad_ctrl_entry_header_t { cc_op : 1; __reserved : 7; }`. This is the
flag NRT asserts as `spad_ctrl->header.cc_op == 1` for a live cc-op (see
[Collective-Type + `cc_op` Enum Reference](../ops/collective-enums.md)). `[OBSERVED HIGH.]`

---

## 3 · The `cc_op` command word — `ncfw_log_spad_ctrl_cc_op_entry @0x1840`

This is the heart of the table: the per-step collective command. The decoder body is
**linear** — 49 `snprintf` calls, **no `algo_type` switch**. The only structural branch is
the JSON wrapper-key gate at `190e: je 19ad` (`test BYTE PTR [rbp-0x108]` — "is a key string
present?"); the remaining `jb +0x07` branches are the `0x100000`-buffer overflow guards.
**Every field is printed unconditionally**, and the ring/mesh union is dumped under *both*
name sets — it is a debug pretty-printer, while the live selector (`algo_type`) is resolved
on-device. `[OBSERVED HIGH — 49 `snprintf`, single wrapper branch.]`

### 3.1 · The byte-exact bitfield map

The entry pointer (`entry_base + 1`) is held in `[rbp-0x110]`. Every load + mask:

| field | bits | extractor `@` | recovered insn |
|---|---|---|---|
| `algo_type` | byte0 `[0:3]` | `0x1a40` | `movzx eax,[rax]` → `0x1a43: and eax,0xf` |
| `algo_sub_type` | byte0 `[4:6]` | `0x1c97` | `movzx eax,[rax]` → `shr al,0x4; and eax,0x7` |
| `trigger_next` | byte0 `[7]` | `0x1ef7` | `movzx eax,[rax]` → `shr al,0x7` |
| `reporter` | byte1 `[0]` | `0x2154` | `movzx eax,[rax+0x1]; and eax,0x1` |
| `ring_wait_complete` | byte1 `[1]` | `0x23ac` | `movzx eax,[rax+0x1]; shr al,1; and eax,0x1` |
| `ring_send_complete` | byte1 `[2]` | `0x2602` | `movzx eax,[rax+0x1]; shr al,0x2; and eax,0x1` |
| `channel_list` (RING) | `+0x3` u32 | `0x2a64` | `mov ebx,DWORD PTR [rax+0x3]` |
| `sema_shift_offset` (MESH) | `+0x3` u16 | `0x2eb6` | `movzx eax,WORD PTR [rax+0x3]` |
| `sema_mask` (MESH) | `+0x5` u16 | `0x30da` | `movzx eax,WORD PTR [rax+0x5]` |

(All offsets are relative to the `cc_op` pointer = `entry+1`. So `byte0` here is `entry+1`,
`byte1` is `entry+2`, and the union begins at `entry+4`.) Field key strings (`.rodata`):
`"algo_type"@0x6502a`, `"algo_sub_type"@0x65039`, `"trigger_next"@0x65049`,
`"reporter"@0x65058`, `"ring_wait_complete"@0x65063`, `"ring_send_complete"@0x65078`,
ring-tag `"ring"@0x6508d` + `"channel_list"@0x65092`, mesh-tag `"mesh"@0x650a1` +
`"sema_shift_offset"@0x650a6` + `"sema_mask"@0x650be`. `[OBSERVED HIGH — every shift/mask
immediate and every key string read.]`

### 3.2 · The full struct (from the `libnrt` struct DB) — including the fields the printer omits

The `libncfw` pretty-printer stops at `ring_send_complete (byte1[2])`, but the IDA-recovered
`spad_ctrl_cc_op_entry_t` (ordinal 9386, **size 7**, lives at `entry+1`) declares **two more
byte1 flags** the printer never decodes — plus a `+2` reserved byte before the union:

```c
// libnrt struct DB — spad_ctrl_cc_op_entry_t (7 B, at entry+1)
struct spad_ctrl_cc_op_entry_t {
    // byte0 (entry+1):
    uint8_t algo_type      : 4;   // [0:3]  THE algorithm selector
    uint8_t algo_sub_type  : 3;   // [4:6]  mesh sub-kind
    uint8_t trigger_next   : 1;   // [7]    chain/step flag
    // byte1 (entry+2):
    uint8_t reporter           : 1;  // [0]
    uint8_t ring_wait_complete : 1;  // [1]
    uint8_t ring_send_complete : 1;  // [2]
    uint8_t safe_mode          : 1;  // [3]  ← NOT printed by libncfw
    uint8_t unique_tensors     : 1;  // [4]  ← NOT printed by libncfw
    uint8_t __reserved0        : 3;  // [5:7]
    // byte2 (entry+3):
    uint8_t __reserved;              // 1 reserved byte
    // bytes3..6 (entry+4..+7): the 4-byte union
    union {
        channel_list_t channel_list;                       // RING
        struct { uint16_t sema_shift_offset, sema_mask; }; // MESH
    };
};
```

`safe_mode` and `unique_tensors` are real, live fields — `encd_dma_mark_end @0x237200`
logs them in the host trace (`CTRL mark -alg %d-subalg %d-trignext %d-chlist %d-reporter
%d-sema_shift_offset %u-sema_mask %u-safe_mode %d`, `libnrt @0x804088`) — the `libncfw`
formatter simply doesn't surface them. `[struct DB OBSERVED HIGH; the byte1 flag widths match
the packer's 5-bit `& 0x1F` nibble (§4).]`

> **GOTCHA — printer ≠ wire.** Treat `libncfw`'s `cc_op` JSON as a *subset* view. The wire
> record is the `libnrt` struct: 8 B total (header + 7-byte `cc_op_entry`), byte1 holds 5
> flags (not 3), and there is a reserved byte at `entry+3` before the union. A reimplementer
> must size the entry at **8 B** and reserve byte1`[3:4]`/byte1`[5:7]` and byte2.

### 3.3 · Field semantics

* **`algo_type`** `[0:3]` — the union selector and the single switch that binds an entry to
  an algorithm. It is exactly the low nibble of NRT's `enc_alg_type` (verified from the
  `libnrt` enum DB):

  | val | `enc_alg_type` | routes to |
  |---|---|---|
  | `0` | `ENC_ALG_RING` | ring channel tape |
  | `1` | `ENC_ALG_HIER` | hierarchical intra+inter |
  | `2` | `ENC_ALG_MESH` | mesh event tape |
  | `3` | `ENC_ALG_KANGARING` | ring channel tape (direct-reduce) |
  | `4` | `ENC_ALG_SINGLE_CYCLE_RING` | ring (all-reduce only) |
  | `5` | `ENC_ALG_INTRA_RDH` | hierarchical (intra leg) |
  | `6` | `ENC_ALG_SINGLE_STEP_MESH` | mesh |
  | `7` | `ENC_ALG_INTER_RDH` | hierarchical (inter leg) |
  | `8` | `ENC_ALG_TWO_STEP_POD_MESH` | mesh |
  | `9` | `ENC_ALG_LATENCY_OPT_MESH` | mesh |
  | `10` | `ENC_ALG_BW_OPT_MESH` | mesh |
  | `11` | `ENC_ALG_INVALID` | — |

  12 values fit the 4-bit field exactly (`11 = INVALID`). The device dispatch indexes this
  nibble straight into the NCFW DRAM `+0xB0` 12-entry computed-goto table (see
  [Main Dispatch Loop](./main-dispatch-loop.md): `const16 a2,0xB0; addx4 a2,a3,a2; l32i.n
  a5,a2,0`). `[host `enc_alg_type` 0..11 OBSERVED HIGH from the `libnrt` enum DB; the
  `algo_type ≡ enc_alg_type` numeric identity is MED — width(4b) + shared vocabulary; the
  host→device write crosses into the LX core and is not byte-traceable.]`

* **`algo_sub_type`** `[4:6]` — the 3-bit mesh sub-kind. Maps to `enc_alg_mesh_type_t`
  (`libnrt` enum DB): `ENC_ALG_FULL_MESH=0`, `ENC_ALG_GROUPED_MESH=1`, `ENC_ALG_MESH_TRN2=2`,
  `ENC_ALG_MESH_SWITCH=3`, `ENC_ALG_MESH_INVALID=4` — 4 real sub-kinds, all inside 3 bits.
  `[width MATCH HIGH; value binding MED.]`

* **`trigger_next`** `[7]` — signal the next peer/op after this entry completes. Pairs with
  the static table's `trigger`/`trigger_next` base addresses (§6) and `instr_chaining`
  `{NONE/INITIAL/BODY/FINAL}` + `chained_cc_op_n`. `[name OBSERVED; chain role INFERRED MED.]`

* **`reporter`** `[byte1 0]` — this op's channel is the leader/reporter that posts
  completion. Pairs with the op-ctx `leader` index (§6). `[INFERRED MED.]`

* **`ring_wait_complete`** `[byte1 1]` / **`ring_send_complete`** `[byte1 2]` — the two ring
  step-completion handshake flags. The packer sets them by comparing a neighbour channel's
  `sp->idx` against this one (`> idx ⇒ send_complete` needs `neff.complete.addr.soc_addr`;
  `< idx ⇒ wait_complete` needs `neff.complete_prev.addr.soc_addr`). `[OBSERVED HIGH width;
  role per the ring handshake MED.]`

* **`channel_list`** (`u32 @entry+4`, RING) — a bitmask of which of the ring's channels this
  entry sequences. The packer builds it by walking `ring->channels[]` and OR-ing `1 <<
  abs_id` for every channel whose owning `sp->idx` matches this TOP_SP. `[OBSERVED HIGH width;
  mask semantics MED.]`

* **`sema_shift_offset`** (`u16 @entry+4`, MESH) / **`sema_mask`** (`u16 @entry+6`, MESH) —
  the mesh variant overlays the same 4 bytes with a semaphore-address shift + mask used to
  compute the mesh event's wait/post semaphore from a base. `[overlay OBSERVED HIGH; the
  shift/mask arithmetic runs on-device — MED.]`

> **NOTE — the reduce op is NOT in the `cc_op` word.** The decoder reads no `SDMA_CCETYPE`-
> width field, and the struct DB declares none. The reduce op (`SDMA_CCETYPE` =
> `{ADD0, FMA1, MAX2, MIN3, EXT4, GCE5}`, from the `libnrt` enum DB) rides in the DMA CCE
> descriptor and the host `cc_op_info{op_type}`, *beside* the spad word — not in it. So
> `cc_op.algo_type` = **how to move/route**; `SDMA_CCETYPE` = **what to compute**.
> `[absence OBSERVED HIGH.]`

---

## 4 · The host packer — `create_spad_ctrl_entry @0x232cd0` (byte-exact)

The host (`libnrt`) packs each 8-byte entry; the packer's shifts land exactly where the
firmware decoder (§3) reads. The header word `(trigger_next << 15) | (sub_type << 12) |
(algo_type << 8) | 1` was disassembled — **not trusted blind**:

```asm
; create_spad_ctrl_entry @0x232cd0   (range 0x232cd0..0x2331c6, 300 insns, 8-B entry)
; — header WORD (entry byte0 = low, byte1 = high) —
232d78:  shl  eax,0x8       ; algo_type     << 8
232d7b:  shl  edi,0xc       ; algo_sub_type << 12
232d7e:  shl  edx,0xf       ; trigger_next  << 15
232d81:  or   eax,0x1       ; | 1   (header.cc_op active flag, lands in byte0 bit0)
232d8b:  or   eax,edi       ; OR in algo_sub_type
232d90:  or   eax,edx       ; OR in trigger_next
; — byte2 flag nibble (5 bits, & 0x1F) → reads back as cc_op byte1 —
232d84:  shl  r15d,0x2      ; ring_wait_complete << 2
232d88:  shl  esi,0x3       ; (permute_chain)    << 3   ; struct DB names this bit "safe_mode"
232d8d:  shl  ecx,0x4       ; (unique_tensors)   << 4
232db7:  and  eax,0x1f      ; mask to 5 bits  ; bit0=reporter, bit1=ring_send_complete
```

So the packed **header uint16** at the entry base = `(trigger_next<<15) | (sub_type<<12) |
(algo_type<<8) | 1`. Its low byte = `1` (the header `cc_op` flag at `entry+0`); its high byte
(`entry+1`) = `(trigger_next<<7) | (sub_type<<4) | algo_type` — precisely what
`ncfw_log_spad_ctrl_cc_op_entry` reads from `[rax]` with `rax = entry+1`. The two views are
consistent. `[OBSERVED HIGH — `shl 0x8/0xc/0xf; or 0x1` @0x232d78.]`

> **NOTE — naming divergence on byte1 bit3.** The IDA struct DB labels `cc_op` byte1 bit3
> `safe_mode`; the packer's variable for the `<<3` term cross-references a `permute_chain`
> identifier (both `safe_mode` and `permute_chain` strings exist in `libnrt`). The **bit
> position is fixed (bit3)**; only the recovered name is ambiguous. A reimplementer should
> reserve the bit and treat the name as `safe_mode`/`permute_chain` (TODO: disambiguate from
> a call-site that writes the field). `[bit position OBSERVED HIGH; name MED.]`

The union write mirrors the decoder: `if (channel->ring) v.cc_op.channel_list = bitmask;`
else `{ v.cc_op.sema_shift_offset = …; v.cc_op.sema_mask = …; }`. `create_spad_ctrl_entry`
has two callers, both `encd_dma_mark_end` (`@0x2379b9`, `@0x237b88`).

---

## 5 · The device tsync struct

### 5.1 · `ncfw_log_configs_dev_tsync @0xad33` — byte-exact

```c
// ncfw_log_configs_dev_tsync(rdi=buf, esi=indent, rdx=enable, rcx=tsync)  ; tsync -> [rbp-0xd0]
struct dev_tsync {
    soc_addr dma_apb_rsvd_addr;  // +0x00  u64  — via ncfw_log_addr(edi=1, "dma_apb_rsvd_addr")
    uint64_t eng_tpb;            // +0x08  — mov r12,[rax+0x8]   @0xb249
    uint64_t timestamp_local;    // +0x10  — mov r12,[rax+0x10]  @0xb7e8
    uint64_t timestamp_tpb;      // +0x18  — mov r12,[rax+0x18]  @0xbd6c
    uint32_t timestamp_tpb_val;  // +0x20  — mov ebx,[rax+0x20]  @0xc144  (32-bit load ⇒ u32)
};
```

The `+0x00` field is printed via `ncfw_log_addr` (`edi=1`, key `"dma_apb_rsvd_addr"@0x652cd`,
`r8=tsync+0` — set up at `0xaf2c..0xaf52`); `ncfw_log_addr @0x41c3` reads `QWORD[r8]` and emits
`{ "soc_addr": "0x%016lX" }` (`"soc_addr"@0x6511c`). Strings: `"eng_tpb"@0x652df`,
`"timestamp_local"@0x652e7`, `"timestamp_tpb"@0x652f7`, `"timestamp_tpb_val"@0x65305`.
`[OBSERVED HIGH — every offset + every string read.]`

### 5.2 · `ncfw_log_dev_configs @0xc371` — the tsync parent + packed device ids

Emits key `"tsync"@0x65319`, calls `ncfw_log_configs_dev_tsync` (`@0xc52c`), then prints the
device-identity fields. The `tpb_id`/`seng_id`/`dev_id` are a **packed nibble bitfield**, not
three bytes:

```c
tpb_id       = byte[0x2c] & 0xf;          // bits[0:3]   movzx[rax+0x2c]; and 0xf   @0xc62f
seng_id      = byte[0x2c] >> 4;           // bits[4:7]   movzx[rax+0x2c]; shr al,4  @0xc827
dev_id       = byte[0x2d] & 0xf;          // bits[0:3]   movzx[rax+0x2d]; and 0xf   @0xca17
tpb_ctrl_ack = *(uint64_t*)&byte[0x24];   // u64         mov rbx,[rax+0x24]         @0xcc0b
// followed by queue_id ("%lu") and dma_engines_bitmap
```

`seng_id` is the SyncEngine/SuperEngine id that owns the collective. Strings:
`"tpb_id"@0x6531f`, `"seng_id"@0x6532b`, `"dev_id"@0x65335`, `"tpb_ctrl_ack"@0x6533e`,
`"queue_id"@0x65351`, `"dma_engines_bitmap"@0x6535c`. `[OBSERVED HIGH — exact `movzx/and/shr`.]`

### 5.3 · The tsync protocol — a TOP_SP-rooted global-timestamp broadcast

The dev_tsync struct exposes a three-value clock-alignment surface: `timestamp_local` (this
core's local counter), `timestamp_tpb` (the TPB/Top-SP global value), and `timestamp_tpb_val`
(the latched 32-bit tick value). Reconciling against the host-side driver in `libnrt` (which
the device struct mirrors):

* **The global tick.** The TOP_SP NX core advances a global timestamp counter by
  `timestamp_inc` per tick — `top_sp_ram +0x4`, RW`[23:0]`, reset `0x400` (carried from
  [TOP_SP Lowering](../ops/top-sp-lowering.md) / [Architecture Synthesis](../ops/architecture-synthesis.md)).
  `tsync.timestamp_tpb`/`timestamp_tpb_val` are the firmware's read of that counter;
  `tsync.timestamp_local` is the per-core counter to be aligned to it. `[tick CARRIED HIGH;
  the dev_tsync read of it INFERRED MED — names + the unique tick source line up.]`

* **The host driver** (`libnrt`, the algorithm the device struct serves):

  ```
  tdrv_tsync_timestamps                                     ; "Collecting timestamp adjustment details ..."
   ├─ encd_ncfw_tsync_start @0x2526e0
   │    └─ aws_hal_sp_topsp_set_tsync_signal @0x457ac0      ; the tsync START doorbell
   │         └─ …_cayman @0x471b10 : write 1 → LOCAL_REG + 0x1560   (lea [rax+0x1560]; mov esi,1)
   ├─ tsync_timestamps_start @0x304610 / tsync_load_one_program @0x3037a0
   │                                                        ; load timestamp-sync program to each TPB eng + TOP_SP
   ├─ tsync_read_notifications_and_calc_offsets @0x303b00   ; read per-engine notification timestamps,
   │                                                        ;   compute the per-engine offset
   └─ tsync_timestamps_finish @0x304b90                     ; "Collected timestamp adjustment details!"
  ```

  Per-engine init is logged as `tpb_init_timestamp=[evtsem, sp, pe, act, dve, pool,
  ham_and_err] dma_init_timestamp=… top_sp_init_timestamp=…` (`libnrt @0x816d60`) — i.e.
  every TPB sub-engine (`pe`/`act`/`dve`/`pool`/…), the `EVT_SEM` unit, the DMA, and the
  TOP_SP each latch a timestamp; the driver reads them back as notifications and calculates
  alignment offsets. Each sequencer carries its own `TIMESTAMP_INC_VAL` register (e.g.
  `CAYMAN_TPB_POOL_SEQUENCER_TIMESTAMP_INC_VAL @0x108`, `SUNDA_TPB_DVE_SEQUENCER_…@0x308`,
  `SUNDA_NOTIFIC_n_QUEUE_NOTIFIC_TIMESTAMP_INC @0xcc`). `[the host driver + doorbell OBSERVED
  HIGH; the dev_tsync struct being the firmware's view of this MED.]`

* **The transport.** `dma_apb_rsvd_addr` (`+0x0`) and `eng_tpb` (`+0x8`) are `soc_addr`
  handles — the reserved APB-broadcast address and the TPB engine address the tsync
  reads/writes timestamps through, the same DMA-APB-broadcast fabric the barriers use to fan
  a value to all dies. The TOP_SP `EVT_SEM` array (read `+0x1000` / set `+0x1400` / inc
  `+0x1800` / dec `+0x1C00`, `+ idx*4`, ArraySize 256) is the global event/semaphore unit the
  timestamps live behind. `[structure HIGH; exact broadcast/align algorithm LX/NX-resident — MED.]`

> **The verdict.** tsync is **not** a one-shot barrier. It is a TOP_SP-rooted global timestamp
> counter (advanced by the `timestamp_inc` tick, kicked by the `LOCAL_REG+0x1560` doorbell)
> made coherent across dies through the `EVT_SEM`/APB-broadcast fabric; each NCFW core holds
> `{local, tpb, tpb_val}` to perform the per-core alignment. This is the clock substrate
> *under* the collective barriers: a barrier sequences ops; tsync keeps the dies' timestamps
> aligned so the sequenced ops are temporally coherent. `[verdict MED-strong; every named
> field OBSERVED HIGH.]`

> **GOTCHA — `tsync` vs `host_trigger` doorbells.** They are distinct `LOCAL_REG` apertures:
> tsync START = `+0x1560` (`set_tsync_signal_cayman @0x471b10`); host_trigger (the per-op
> collective kick) = `+0x15a0` (`set_host_trigger_cayman @0x471b40`; cayman/mariana abs
> `0x615a0`, sunda `0x60848`); init = `+0x1540`; stop = `+0x15c0`. Do not conflate the tsync
> doorbell with the per-op trigger. `[OBSERVED HIGH — `lea [rax+0x1560]` @0x471b20 vs `lea
> [rax+0x15a0]` @0x471b50.]`

---

## 6 · The static spad-ctrl table — base / trigger / complete surface

Beside the per-op `cc_op` word, the firmware exposes the scratchpad **address** surface that
drives a collective. These fields live in the static NEFF config
(`ncfw_log_neff_configs @0x12864` → `ncfw_log_basic_block_table @0x11452`) and the runtime
basic-block ctx (`ncfw_log_basic_block_ctx @0x15ff1`). Located by string xref:

| field (`.rodata @`) | role |
|---|---|
| `slot_spad_base_%d` `@0x65506` | per-slot data scratchpad base (algo writes per-step data) |
| `ctrl_spad_base` `@0x65518` | the **CC-op table base** (array of `op_num` entries) |
| `trigger` `@0x65527` | trigger semaphore/address (kick the op) |
| `trigger_next` `@0x6552f` | next-op trigger (chains to the `trigger_next` bit, §3) |
| `complete` `@0x6553c` | completion semaphore/address |
| `complete_prev` `@0x65545` | previous-op completion (chain back-edge) |
| `tpb_stop_sema` `@0x65553` | TPB stop/quiesce semaphore |
| `op_num` `@0x65561` | number of ops in this spad table |
| `leader` `@0x6556a` | the leader/reporter op index (pairs with `cc_op.reporter`) |
| `tpb_compl_addr_num` `@0x65573` | count of TPB completion addresses |
| `ctrl_spad_addr` `@0x655ac` (runtime) | the live control-spad address |
| `slot_spad_addr_%d` `@0x655bb` (runtime) | the live per-slot spad addresses |

So spad-ctrl is a control scratchpad in device memory: `ctrl_spad_base` holds the `cc_op`
command table (`op_num` of the §2/§3 entries); each `slot_spad_base_%d` is a per-slot data
scratchpad; `trigger`/`trigger_next`/`complete`/`complete_prev`/`tpb_stop_sema` are the
semaphores that sequence it; `leader` is the reporting op. The host stages this in HBM, then
DMA-loads the two regions onto each TOP_SP (carried from
[TOP_SP Lowering](../ops/top-sp-lowering.md)):

```c
// CTRL SPAD (the cc_op command table) → SP SRAM
sz = encd_arch_get_sp_spad_ctrl_sram_size();       // cayman → 0x1000  (4 KiB)   @0x25af00
dma_load(sp->spad,            sz, "TOPSP CTRL SPAD", ctrl_spad_base.soc_addr);
// SLOT SPAD (per-slot data)   → TPB IRAM   (staged at spad + 0x100000)
sz = encd_arch_get_sp_spad_slot_tpb_iram_size();   // cayman → 0x8000  (32 KiB)  @0x25af10
dma_load((char*)sp->spad + 0x100000, sz, "TOPSP SLOT SPAD", slot_spad_base[0].soc_addr);
```

> **NOTE — SPAD region sizes (via the sibling's `libnrt` accessors).**
> **CTRL SPAD → SP SRAM = `0x1000` (4 KiB)** (`cayman_get_sp_spad_ctrl_sram_size @0x25af00`);
> **SLOT SPAD → TPB IRAM = `0x8000` (32 KiB)** (`cayman_get_sp_spad_slot_tpb_iram_size
> @0x25af10`). The NCFW-ctx ctrl/slot SPAD offsets are `0x42d0`/`0x42a0`
> (`@0x257000`/`@0x257010`). `[CARRIED HIGH from [TOP_SP Lowering](../ops/top-sp-lowering.md).]`

---

## 7 · Per-arch ×4 — the schema is generation-invariant

`arch 0x05 = v2 = SUNDA`, `0x0c = v3 = CAYMAN`, `0x14 = v4 = MARIANA`, `0x1c = v4+ =
MARIANA_PLUS`. Each of the five decoders is cloned once per arch (stride table):

| decoder | sunda | cayman | mariana | mariana_plus |
|---|---|---|---|---|
| `…_entry_header` | `0x13b5` | `0x1a15c` | `0x32f03` | `0x4bcaa` |
| `…_cc_op_entry` | `0x1840` | `0x1a5e7` | `0x3338e` | `0x4c135` |
| `…_spad_ctrl_entry` | `0x3424` | `0x1c1cb` | `0x34f72` | `0x4dd19` |
| `…_dev_tsync` | `0xad33` | `0x23ada` | `0x3c881` | `0x55628` |
| `…_dev_configs` | `0xc371` | `0x25118` | `0x3debf` | `0x56c66` |

**Both the `cc_op` schema and the tsync struct are byte-identical across all four arches.**
Verified by extracting the schema-defining immediates from each copy:

* `cc_op_entry` — the bitfield extractors (`and 0xf`, `shr al,4`, `and 7`, `shr al,7`,
  `[rax+0x1]` masks, `mov ebx,[rax+0x3]`, `movzx [rax+0x3]`, `movzx [rax+0x5]`) are
  **identical** across `0x1840`/`0x1a5e7`/`0x3338e`/`0x4c135` (per-copy `objdump | rg -o`
  histogram matched exactly). `[OBSERVED HIGH.]`
* `dev_tsync` — the struct offsets `{+0x8, +0x10, +0x18, +0x20}` + the `edi=1 ncfw_log_addr`
  call are **identical** across `0xad33`/`0x23ada`/`0x3c881`/`0x55628`. `[OBSERVED HIGH.]`
* `dev_configs` — the `+0x2c` nibble split + `+0x2d` + `+0x24` are identical across copies.

The only per-arch deltas are the rip-relative call/string displacements (every copy points
into the **one** `.rodata` string table at `0x65000`). The firmware *data* images differ
(DRAM size, `soc_addr` count, mesh event count) but the **control-word contract is fixed**: a
single `cc_op` table format and a single tsync struct serve all four Trainium/Inferentia NCFW
generations. `[OBSERVED HIGH — normalized-immediate identity.]`

> **QUIRK — v5/MAVERICK NCFW is FILE-ABSENT.** `libncfw.so` ships only the four arch copies
> above (sunda/cayman/mariana/mariana_plus). There is no v5/MAVERICK NCFW decoder in this
> binary, so **any v5 `cc_op`/tsync claim is INFERRED/ABSENT, never observed**. A v5
> reimplementation would have to assume schema-continuity (likely, given the v2→v4+ identity)
> but cannot verify it here. `[ABSENT — no v5 decoder symbol present.]`

---

## 8 · Algo integration — how `algo_type` routes each entry

`algo_type` (`cc_op` byte0`[0:3]`) is the single switch that binds a spad entry to an
algorithm. Each entry is one step of a heterogeneous program; the table sequences them:

* **RING(0) / KANGARING(3) / SINGLE_CYCLE_RING(4)** → the ring channel tape. `channel_list`
  (`u32 @entry+4`) selects which channels this entry sequences; `ring_wait_complete` /
  `ring_send_complete` are its step flags. See [Ring / KangaRing](./ring-kangaring.md).
* **MESH(2) / SINGLE_STEP_MESH(6) / TWO_STEP_POD_MESH(8) / LATENCY_OPT_MESH(9) /
  BW_OPT_MESH(10)** → the mesh event tape. The `sema_shift_offset`(`@entry+4`) /
  `sema_mask`(`@entry+6`) overlay computes the mesh event's wait/post semaphore;
  `algo_sub_type` picks the sub-kind (`FULL/GROUPED/TRN2/SWITCH`). See
  [Mesh Collective](./mesh-collective.md).
* **HIER(1) / INTRA_RDH(5) / INTER_RDH(7)** → hierarchical (intra + inter legs). See
  [Hierarchical Collective](./hierarchical-collective.md).

So an all-reduce can be expressed as multiple **chained** spad entries (e.g. a HIER entry
whose intra leg is a ring and inter leg a mesh), with `trigger_next`/`complete_prev` forming
the chain and `op_num` counting them — matching `instr_chaining {INITIAL/BODY/FINAL}` +
`chained_cc_op_n`. The device walks the table; `algo_type` routes each entry to the matching
algorithm through the DRAM `+0xB0` dispatch table. `[binding via the shared
`algo_type`/`enc_alg_type` vocabulary HIGH-structure / MED-numeric; the per-entry chain
schedule is LX-resident — MED.]`

---

## 9 · Reimplementation checklist

| Constant | Value | Source `@` |
|---|---|---|
| `spad_ctrl_entry` total size | **`8` B** | `libnrt` struct DB (ordinal 9350) |
| `header.cc_op` | byte0 bit0 (1-bit active flag) | decoder `@0x154e`; struct `spad_ctrl_entry_header_t` |
| `cc_op` word at | `entry + 1` | wrapper `lea [rax+0x1] @0x35d7` |
| `algo_type` | byte0`[0:3]` (`& 0xf`) | `@0x1a43` |
| `algo_sub_type` | byte0`[4:6]` (`>>4 & 7`) | `@0x1c9a` |
| `trigger_next` | byte0`[7]` (`>>7`) | `@0x1efa` |
| `reporter` / `ring_wait_complete` / `ring_send_complete` | byte1`[0]`/`[1]`/`[2]` | `@0x2154`/`@0x23b0`/`@0x2606` |
| `safe_mode` / `unique_tensors` | byte1`[3]`/`[4]` (struct DB; printer omits) | `libnrt` struct DB; packer `@0x232d88`/`@0x232d8d` |
| `__reserved` byte | `entry + 3` (cc_op`+2`) | `libnrt` struct DB |
| `channel_list` (RING) | `u32 @entry+4` | `@0x2a64` |
| `sema_shift_offset` / `sema_mask` (MESH) | `u16 @entry+4` / `u16 @entry+6` | `@0x2eb6` / `@0x30da` |
| packer header word | `(trigger_next<<15)\|(sub_type<<12)\|(algo_type<<8)\|1` | `create_spad_ctrl_entry @0x232d78` |
| `enc_alg_type` vocab | `0..11` (`11=INVALID`) | `libnrt` enum DB |
| `enc_alg_mesh_type_t` | `0..4` (`FULL/GROUPED/TRN2/SWITCH/INVALID`) | `libnrt` enum DB |
| `SDMA_CCETYPE` (reduce, NOT in `cc_op`) | `0..5` (`ADD/FMA/MAX/MIN/EXT/GCE`) | `libnrt` enum DB |
| CTRL SPAD → SP SRAM | `0x1000` (4 KiB) | `cayman_get_sp_spad_ctrl_sram_size @0x25af00` |
| SLOT SPAD → TPB IRAM | `0x8000` (32 KiB) | `cayman_get_sp_spad_slot_tpb_iram_size @0x25af10` |
| `dev_tsync` | `dma_apb_rsvd_addr@+0` (soc_addr) / `eng_tpb@+0x8` / `timestamp_local@+0x10` / `timestamp_tpb@+0x18` / `timestamp_tpb_val@+0x20` (u32) | `ncfw_log_configs_dev_tsync @0xad33` |
| `dev_configs` ids | `tpb_id`=byte`[0x2c]&0xf` / `seng_id`=byte`[0x2c]>>4` / `dev_id`=byte`[0x2d]&0xf` / `tpb_ctrl_ack`=u64`@+0x24` | `@0xc62f`/`@0xc827`/`@0xca17`/`@0xcc0b` |
| tsync START doorbell | write `1` → `LOCAL_REG + 0x1560` | `set_tsync_signal_cayman @0x471b10` |
| `timestamp_inc` tick | `top_sp_ram +0x4`, RW`[23:0]`, reset `0x400` | carried from [TOP_SP Lowering](../ops/top-sp-lowering.md) |
| `EVT_SEM` windows | read `+0x1000` / set `+0x1400` / inc `+0x1800` / dec `+0x1C00` (`+ idx*4`, ArraySize 256) | carried from [TOP_SP Lowering](../ops/top-sp-lowering.md) |
| arch schema identity | `cc_op` + tsync byte-identical ×4 (sunda/cayman/mariana/mariana_plus); v5 ABSENT | normalized-immediate ×4 |

### Cross-references

* [Collective-Type + `cc_op` Enum Reference](../ops/collective-enums.md) — `enc_alg_type` /
  `SDMA_CCETYPE` / `header.cc_op == 1` source of truth.
* [TOP_SP Lowering](../ops/top-sp-lowering.md) — `create_spad_ctrl_entry`, SPAD DMA-load,
  doorbells, `EVT_SEM`, `timestamp_inc` tick.
* [NCFW Main Dispatch Loop](./main-dispatch-loop.md) — the DRAM `+0xB0` 12-entry
  `algo_type`→handler computed-goto table.
* [All-Reduce](../ops/all-reduce.md) · [Architecture Synthesis](../ops/architecture-synthesis.md)
  — how chained `cc_op` entries compose a full collective.
