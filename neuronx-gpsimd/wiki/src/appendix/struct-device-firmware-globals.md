# Device-Firmware Global Structs (field-exact)

This appendix is the **single field-exact reference** for the on-device GPSIMD
firmware's two struct families: the **dispatch structs** the firmware reads on every
decoded instruction (Part 1) and the **global structs** that hold the firmware's boot
identity, per-engine sequencer state, profiler config, and the host-readable stdio
ring (Part 2). It is the appendix half of the device-firmware survey: where the
[firmware](../firmware/pool/kernel-info-table.md) and [SEQ](../firmware/seq/dispatch-hub.md)
pages narrate *how* the firmware uses each structure, this page pins the **byte
layout** — offset, size, type, field, meaning — for every structure named in the
census, re-verified against the carved image this session and CORRECTION-flagged where
a naive reading diverges from the bytes.

Every structure below is recovered from one of four binary sources, all
static-analysis-derived and citeable:

1. the **carved CAYMAN `EXTISA_0` device firmware ELF** (`sha256
   910d41c3…b4b55527`, `e_machine = 94` Tensilica-Xtensa, `e_entry 0x01005610`),
   carved from the host loader `libnrtucode_internal.so` (the data behind getter
   `CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get`, host `.rodata` vaddr `0x2ef7e0`, size
   41,568 B), disassembled with the native Cadence `xtensa-elf-objdump`
   (`XTENSA_CORE=ncore2gp`);
2. the **carved SEQ DEBUG firmware** (`img_CAYMAN_NX_POOL_DEBUG_{IRAM,DRAM}`,
   `iram.bin` sha256 `8e4412b9…`, `dram.bin` sha256 `7bdf6ed7…`) and the **SUNDA Q7
   POOL** stdio image;
3. the **host loader/decoder libs** — `libnrtucode_internal.so` (PROF getters, the
   boot/claim handshake `nrtucode_core_on_ucode_booted`), `libnrt.so` (the `enc_*`
   collectives encoder + DWARF), `libncfw.so` (the firmware config-struct
   pretty-printer);
4. the **arch-isa headers + CSR schemas** shipped in the customop-lib
   (`aws_neuron_isa_notification.h`, `notific_10_queue.json`,
   `tpb_xt_local_reg.json`, `core-isa.h`).

> **GOTCHA — two distinct cores, two decode rules.** The POOL/SEQ compute firmware
> runs on the **Vision-Q7 `ncore2gp` "Cairo"** datapath core (`XCHAL_HAVE_VISION = 1`,
> `XCHAL_VISION_TYPE = 7`, the FLIX/VLIW layer; `XCHAL_HAVE_FLIX3 = 0` is **not**
> "scalar"). The collectives **NCFW** management firmware runs on the **scalar
> Xtensa-LX** survival core (decode with the LX `op0 e/f` = 3-byte-length rule; the
> `ncore2gp` config mis-decodes its bytes as Vision bundles). Every disassembly below
> states which core it belongs to. `[HIGH/OBSERVED]`

> **GOTCHA — the `.data`/`.data.rel.ro` offset delta is `0x200000` for `ncore2gp`
> config DLLs** (NOT libtpu's `0x400000`). `.text`/`.rodata` are VMA==file-offset; the
> carved device ELF's `kernel_info_table` / `.globstruct` are in a `0x02000000`
> PT_LOAD whose VMA==file-offset by construction. Confirm per-section with `readelf
> -SW` before any `xxd`/`objdump` on a `.data`-resident struct.

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/string/header field read from a shipped artifact this session;
`INFERRED` = reasoned over OBSERVED facts (often across a FLIX/literal-pool desync);
`CARRIED` = consolidated from a cited cross-page anchor at its original confidence.
Crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real),
**GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a naive reading),
**NOTE** (orientation). Tables escape a literal pipe in a cell as `\|`.

> **WALL — v5/Maverick interiors are INFERRED.** Every layout here is `OBSERVED` on
> **CAYMAN (NC-v3, `arch_id 0x0c`, coretype `0x0d`)**, the reference generation. Where
> a structure is byte-identical under MARIANA / MARIANA_PLUS it is noted `CARRIED`.
> The **MAVERICK (NC-v5)** interior is `INFERRED`: no v5-specific firmware image or
> CSR schema ships (the NCFW `get_image` selector ladder closes at `arch_id 0x1c`).
> Note the seam — **`coretype 37` (`ct37`) is OBSERVED** (the `maverick_libs` jump-table
> target, the `nrtucode.h:56` ordinal, and the two resolver bitmasks both setting bit 37,
> §1.5i / [Cross-Walk card §3](codename-crosswalk-table.md)); the dependent `arch_id 36`
> is `INFERRED` (`coretype − 1`, no NCFW image, no `cmp $0x24`).

---

## Part 1 — Device dispatch structs

These are the structures the firmware reads on the **decode → dispatch** path of every
instruction. The two engines dispatch in opposite styles (SEQ = dense direct-indexed
ASCII table → C++ `Handler`; POOL = sparse keyed `(opcode,spec)` table → flat C
kernel), so each owns its own dispatch struct.

### 1.1 POOL `kernel_info_table` entry — 8 bytes `[HIGH/OBSERVED]`

The POOL engine's opcode→kernel dispatch table is its own ELF section
(`kernel_info_table`, PROGBITS, VMA `0x02000380`, size `0x88` = 17 × 8). Each entry is
a flat **8-byte** record: a 4-byte packed key followed by a 4-byte load-relocated
kernel entry VMA. There is **no header and no in-band terminator** — the count comes
only from the section size and the base/end getter pair (`(0x02000408 −
0x02000380) >> 3 = 17`).

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 1 | u8 | `pad0` | always `0x00` (top byte of the BE key) |
| `+0x01` | 1 | u8 | `pad1` | always `0x00` |
| `+0x02` | 1 | u8 | `spec` | sub-opcode / spec selector (`0` for most opcodes; `0/1/2/4/3` for the five `0xF0` rows) |
| `+0x03` | 1 | u8 | `opcode` | POOL / extended-instruction opcode |
| `+0x04` | 4 | u32 LE | `funcVA` | kernel entry VMA in `.text` (`0x010xxxxx`); the **only** relocated field (`R_XTENSA_RELATIVE`, addend 0, one per row at `base + 8·i + 4`) |

```c
/* POOL kernel_info_table entry — ELF section @ VMA 0x02000380, 17 × 8 B. [HIGH/OBSERVED]
 * The first four bytes read big-endian (opcode @ +3, spec @ +2); as a native-LE u32
 * (Xtensa is LE) they equal exactly (opcode<<24) | (spec<<16) — the dispatch key. */
typedef struct {
    uint32_t key;        /* +0x00  packed key == (opcode<<24)|(spec<<16); NOT relocated */
    uint32_t funcVA;     /* +0x04  kernel entry VMA; R_XTENSA_RELATIVE, load-relocated  */
} KernelInfo;            /* sizeof == 8; count = (end−base)>>3 = 17                      */
```

> **GOTCHA — "BE key" and "`opcode<<24\|spec<<16`" are the same four bytes.** The
> byte-exact invariant is `opcode @ entry+3, spec @ entry+2, +0/+1 = 0`. Read those
> four bytes as a native-LE `u32` and you get `opcode<<24 | spec<<16` — the comparand
> the dispatcher (`@0x01005610`) builds from the decoded microcode word, then
> linear-scans for. Do not byte-swap one side only. `[HIGH/OBSERVED]`

**Re-verified this session** (carve `910d41c3…`, `kernel_info_table` file off `0x7400`,
0x88 bytes; 17 rows parsed): the entire CAYMAN table, every row `spec@+2 opcode@+3
funcVA@+4` —

| idx | opcode | spec | funcVA | routes to |
|--:|:--:|:--:|:--|:--|
| 0 | `0x7e` | 0 | `0x01000080` | `iota` |
| 1 | `0x7c` | 0 | `0x010003f8` | `pool_cross_lane_reduce_arith` |
| 2 | `0x7d` | 0 | `0x01000410` | `pool_cross_lane_reduce_bitvec` |
| 3 | `0x45` | 0 | `0x01000b90` | `decode_pool` |
| 4 | `0x51` | 0 | `0x0100105c` | `tensor_tensor_arith` |
| 5 | `0x41` | 0 | `0x01000f1c` | `tensor_tensor_arith` (indirect/vtable) |
| 6 | `0xf0` | 0 | `0x01003370` | `ExtendedInstEngineNop` |
| 7 | `0xf0` | 1 | `0x01003380` | `pool_extended_inst_copy` |
| 8 | `0xf0` | 2 | `0x01003484` | `decode_extended_inst_tensor_tensor_arith` |
| 9 | `0xf0` | 4 | `0x010037a8` | Rand/Cptc band (state `0x0200046c`) |
| 10 | `0xf0` | 3 | `0x01003a60` | Rand/Cptc band (state `0x02000470`) |
| 11 | `0x52` | 0 | `0x01003b40` | op `0x52` dispatch |
| 12 | `0x46` | 0 | `0x010040c0` | `pool_copy` |
| 13 | `0x47` | 0 | `0x01004160` | `pool_cast` |
| 14 | `0xbe` | 0 | `0x01004204` | `get_sequence_bounds_impl` |
| 15 | `0xf2` | 0 | `0x0100484c` | `nonzero_with_count_impl` |
| 16 | `0x7b` | 0 | `0x01004dc4` | `decode_tensor_dequantize` |

The key column is **registration order, not sorted** (`7e 7c 7d 45 51 41 f0 f0 f0 f0
f0 52 46 47 be f2 7b`) — a reimplementer must linear-scan, not binary-search. Full
per-row resolution: [kernel_info_table Binary Layout](../firmware/pool/kernel-info-table.md).
`[HIGH/OBSERVED]`

### 1.2 The `0xF0` EXTENDED_INST byte-13 spec-multiplexed dispatch `[HIGH/OBSERVED]`

The POOL extended instruction (base opcode `0xF0`) is sub-dispatched **not by a
`switch`** but by registering opcode `0xF0` **five times** in the `kernel_info_table`,
one row per spec byte. The spec is the **third byte of the packed key** (key bits
`[23:16]`, i.e. the `+0x02` byte of the entry, §1.1) — what the SCOPE calls "byte-13"
of the multiplexed instruction word: the discriminator the front-end peels out and
folds into `opcode<<24 | spec<<16` before the single linear scan.

| byte+2 `spec` | row idx | funcVA | sub-op handler | resolution |
|:--:|:--:|:--|:--|:--|
| `0x00` | 6 | `0x01003370` | `ExtendedInstEngineNop` | clean no-op `entry;movi a2,0;retw.n` — HIGH |
| `0x01` | 7 | `0x01003380` | `pool_extended_inst_copy()` | funcVA == `.xt.prop` start (EXACT) — HIGH |
| `0x02` | 8 | `0x01003484` | `decode_extended_inst_tensor_tensor_arith(bool,uint)` | `const16+callx8 → 0x010034b0` — HIGH |
| `0x04` | 9 | `0x010037a8` | Rand/Cptc band, `entry a1,48`, state `0x0200046c` → `decode_pool@0x01000b90` | MED |
| `0x03` | 10 | `0x01003a60` | Rand/Cptc band, `entry a1,32`, state `0x02000470` → `decode_pool@0x01000b90` | MED |

```c
/* The 0xF0 spec sub-dispatch is the SAME linear scan, no special case. [HIGH/OBSERVED]
 * spec is byte +0x02 of the kernel_info_table key; a (0xF0, spec) microcode instr
 * lands on EXACTLY ONE of the five 0xF0 rows. */
uint32_t key = (0xF0u << 24) | (spec << 16);   /* spec ∈ {0,1,2,4,3}, byte-13 of the instr word */
/* miss on a registered opcode but unregistered spec → "UNKNOWN EXTENDED OPCODE=%d" (decimal spec)
 * miss on no opcode match at all                    → "UNKNOWN OPCODE=0x%x"        (hex opcode)  */
```

> **CORRECTION — there is no in-loop `if (opcode == 0xF0)` branch.** A naive reading
> expects a dedicated `dispatch_extended_inst(spec)`. The binary has **no such
> branch**: the two-level dispatch is realized entirely by the table holding five
> `0xF0` rows. The five handlers do **not** converge to one address; each owns its own
> `.bss` state slot (`0x468`/`0x46c`/`0x470`), which is itself proof they are
> independent kernels, not one shared switch. `[HIGH/OBSERVED]`

> **QUIRK — registration order is `0,1,2,4,3`, so spec 4 precedes spec 3.** A linear
> key-scan is order-independent (harmless), but a reimplementation that assumes the
> table is sorted by `(opcode,spec)` and binary-searches mis-locates spec 3/4. The
> two miss-path format strings differ on purpose: the extended miss prints **decimal**
> (`=%d`, a small spec) while the top-level miss prints **hex** (`=0x%x`, a full
> opcode) — itself corroborating that the spec byte is the extended discriminator.
> Full per-spec disassembly + the `cptc_decode_impl<1..6>` codec family:
> [POOL Extended-Opcode (`0xF0`) Dispatch](../firmware/pool/pool-ext-0xf0.md). `[HIGH/OBSERVED]`

### 1.3 `nxlib_notification` — the 16-byte TIE-queue record `[HIGH/OBSERVED]`

The fixed **16-byte (128-bit)** notification record (`NEURON_ISA_NOTIFICATION`,
`NEURON_ISA_NOTIFICATION_NBYTES = 0x10`, from `aws_neuron_isa_notification.h`) is the
wire format every TPB engine (PE/ACT/POOL=Q7/DVE/SP), the DMA/DGE path, error logic,
HAM, and the AXI evt/sem path writes through its NOTIFIC block's TIE notification queue
into a software-owned ring in SoC RAM. Four little-endian `uint32` words; the header is
a single packed byte at `word0[31:24]`.

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 2 | u16 | `metadata_0` | record-class payload `[15:0]` |
| `+0x02` | 1 | u8 | `metadata_1` | payload `[23:16]` |
| `+0x03` | 1 | u8 | `header` | packed: `notific_type:5 [28:24]`, `software_queue_overflow:1 [29]`, `hardware_queue_overflow:1 [30]`, `phase:1 [31]` |
| `+0x04` | 4 | u32 | `metadata_2` | record-class payload `[63:32]` |
| `+0x08` | 8 | u64 | `timestamp` | `{u32 low; u32 high}`, free-running counter snapshot, **fixed unit 1 ps** `[127:64]` |

```c
/* NEURON_ISA_GENERIC_NOTIFICATION — 16 B, the TIE-queue record. [HIGH/OBSERVED]
 * sizeof == NEURON_ISA_NOTIFICATION_NBYTES == 0x10. */
typedef struct {
    uint8_t notific_type:5;             /* header byte [4:0] = word0[28:24] */
    uint8_t software_queue_overflow:1;  /* header byte [5]   = word0[29]    */
    uint8_t hardware_queue_overflow:1;  /* header byte [6]   = word0[30]    */
    uint8_t phase:1;                    /* header byte [7]   = word0[31]    */
} __attribute__((packed)) NEURON_ISA_NOTIFICATION_HEADER;

typedef struct {
    uint16_t metadata_0;                /* +0x00  word0[15:0]  */
    uint8_t  metadata_1;                /* +0x02  word0[23:16] */
    NEURON_ISA_NOTIFICATION_HEADER header; /* +0x03  word0[31:24] */
    uint32_t metadata_2;                /* +0x04  bits[63:32]  */
    struct { uint32_t low, high; } timestamp; /* +0x08  bits[127:64], 1 ps */
} __attribute__((packed)) NEURON_ISA_GENERIC_NOTIFICATION;
```

The `notific_type:5` discriminator selects the record class — **24 values** in the
5-bit space (engine quad `{INST_START, INST_END, EXPLICIT, EVT_SEM}` strides by 4 per
engine): `DMA=0x02`, `ERROR=0x03`, PE `0x04..0x07`, ACT `0x08..0x0b`, **POOL (the
GPSIMD compute engine) `0x0c..0x0f`**, DVE `0x10..0x13`, SP `0x14..0x17`, `AXI_EVT_SEM
0x1b`, `HAM 0x1e`, `TPB_ERROR 0x1f`.

> **QUIRK — the `phase` bit at byte+3 bit7 is the poll primitive.** The producer
> toggles `phase` once per ring wrap (epoch); a consumer detects a fresh record by the
> phase flip at `base_addr + head_ptr` **without** any head/tail bookkeeping. The
> `tail_ptr` does *not* guarantee AXI drain completion — the in-RAM `phase` bit is the
> authoritative arrival signal. `[HIGH/OBSERVED]`

> **NOTE — per-class bodies overlay the same 16 B.** All bodies share the `+0x03`
> header byte and the trailing `+0x08` 64-bit timestamp; they differ in how the 24-bit
> `[23:0]` + 32-bit `[63:32]` payload is interpreted (e.g. TPB `*_INST_START` =
> `{program_counter:20, debug_hint:4, soft_reset_counter:16, evt_wait_num_cycles:16}`;
> the DGE-completion EXPLICIT carries `{dma_map:16, num_packets:12, …}`). Full per-type
> bodies + the NOTIFIC ring descriptor: [CSR — NOTIFIC Queue](../control/csr/notific-queue.md)
> and the [device→host notification path](../control/interrupt/device-host-notification.md).
> `[HIGH/OBSERVED]`

### 1.4 SEQ instruction-slot / sequencer state `[HIGH/OBSERVED]`

The SEQ engine's per-instruction dispatch is a **dense direct-indexed 178-slot jump
table** at DRAM `0x80814`, each slot a **4-byte** absolute IRAM trampoline target.
Indexing is `opcode_byte − 0x41` (opcode range `0x41..0xf2`); a single `bgeu 177,
index` rejects both ends (unsigned underflow on `opcode < 0x41`).

| structure | offset/stride | size | meaning |
|:--|:--|:--|:--|
| SEQ dispatch table | base DRAM `0x80814` | 178 × **4 B** = 712 B (`0x814..0xadc`) | 55 real trampolines / 123 default fills (`= 0x3198`) |
| dispatch table slot `i` | `0x80814 + 4·i` | 4 | absolute IRAM target (`< 0x1c820`); NOT relocated (baked at link) |
| (mode-2) HW-decode table | base DRAM `0x80adc` | 178 × 4 B | structurally identical; default fill `0x3a04`; per-runtime-mode (not per-gen) |

Each real slot is an **8-byte trampoline** (`call8 <impl> ; j 0x31a3`); the impl shim
(`entry; const16 a2,<handler-fn>; …; call8 <thunk>; retw`) materialises a boot-built
`Handler` object pointer into `a10` and calls one of two notify-bracketing thunks
(`0x96d4` compute, 32 ops, pre-drain only; `0x85c4` control, 15 ops, pre-drain + POST
completion-notify), each ending in the identical double-indirect `execute()` core
(`l32i [obj+0]; callx8` — vtable slot 0 at `vptr+0`, **no `+0x10` header skip**).

```c
/* SEQ Handler object — the dispatch landing target. [MED/INFERRED across the FLIX desync]
 * No static vtable array exists in the image (a full word-search finds zero static
 * occurrences of any handler-fn address); the singletons are constructed at boot and
 * the impl's FLIX bundle loads the live pointer per call. */
struct Handler {
    void (**vtable)(struct Handler *);   /* [obj+0] -> slot 0 == execute(); slot 0 at vptr+0 */
};
```

> **CORRECTION — the `0x80814` table is NOT a vtable; the object is boot-built.** A
> reader might assume the table (or some `.rodata` array) holds the `Handler` vptrs.
> The bytes refute it: the table holds **trampoline** addresses, and a full scan finds
> **zero** static occurrences of any handler-fn address as a data word in IRAM or
> DRAM. The vptr lives in a boot-constructed object the impl's FLIX bundle loads into
> `a10`. `[MED/INFERRED — the double-indirect thunk bytes + the zero static-table count
> are HIGH/OBSERVED; the window-rotation object-materialisation is the inferred part.]`

The **operand data is not passed in a register at the indexed jump** — handlers read
operands from (a) the fetch cursor `a4` (still addressing the instruction word) and (b)
a per-engine state struct (§2.3). Full table + the trampoline/thunk mechanism:
[SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md);
the fetch front-end that feeds it: [SEQ Fetch + PC-Redirect Front-End](../firmware/seq/fetch-pc-redirect.md).
`[HIGH/OBSERVED]`

### 1.5 The `enc_*` collectives class family `[HIGH/OBSERVED — host DWARF + libncfw decoder]`

The collective-communication (CC) program is built **host-side** by the `enc_*`
encoder classes in `libnrt.so` (fully symbolized, DWARF-typed), lowered into a
**persistent DMA descriptor ring (`pring`)** plus an **algorithm config tape** that the
**NCFW scalar-Xtensa-LX management core** walks at runtime. The config-struct
pretty-printer lives in `libncfw.so`, whose byte-offsets mirror the firmware's
DRAM-resident structs. The op-list / proxy-task / comm / channel / peer / topology
structures decompose as follows.

#### 1.5a The op-list — `neff_configs` program block + the `cc_op` command word

The collective **op-list** is the `neff_configs` program block (DWARF DIE
`<14f9029>`, **3592 B**) the host DMAs into device SRAM. Its `cc_op` command table
lives at `ctrl_spad_base`; each table slot is an 8-byte `spad_ctrl_entry`.

`neff_configs` (the op-list block, selected fields):

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0` | — | array | `dma_alloc_bitmap` | which DMA engines this program owns |
| `+64` | — | array | `desc_count` | per-block descriptor counts |
| `+2144` | 48 | `addr_t[6]` | `slot_spad_base[6]` | SLOT scratchpad bases → TPB IRAM (loaded at START) |
| `+2192` | 8 | `addr_t` | `ctrl_spad_base` | CTRL scratchpad base → holds the `cc_op` command table |
| `+2200` | 296 | `neff_barrier_configs_t` | `barrier_configs` | host/device barrier union |
| `+2496` | 1048 | `basic_block_configs_t` | `basic_block_configs` | per-block pring descriptors |
| `+3544` | 8 | `semaphore_t` | `trigger` | kick THIS op |
| `+3552` | 8 | `semaphore_t` | `trigger_next` | chain to the NEXT op |
| `+3560` | 8 | `semaphore_t` | `complete` | this op's completion sema |
| `+3568` | 8 | `semaphore_t` | `complete_prev` | PREV op's completion (back-edge) |
| `+3576` | 8 | `semaphore_t` | `tpb_stop_sema` | TPB quiesce sema |
| `+3584` | 4 | u32 | `op_num` | # `cc_op` entries in the ctrl-spad table |
| `+3588` | 2 | u16 | `function_n` | # NEFF functions / basic blocks |
| `+3590` | 1 | u8 | `tpb_compl_addr_num` | # TPB completion addresses |
| `+3591` | 1 | u8 (packed) | `leader:1 dbg_cc_nop:1 host_cc:1 __reserved0:5` | program flags |

`addr_t` (8 B) is `struct addr { uint64_t soc_addr; }`; `semaphore_t` (DIE `<14f85c8>`,
8 B) wraps one `addr`. `[HIGH/OBSERVED]`

> **CORRECTION — `function_n` is u16 and `tpb_compl_addr_num` is u8, not wider.** A
> naive "three u32 counters" reading is wrong: only `op_num@+3584` is u32; `function_n`
> is **u16 @+3588**, `tpb_compl_addr_num` is **u8 @+3590**, and `slot_spad_base@+2144`
> is an **array of 6** `addr_t`, not a scalar. `[HIGH/OBSERVED — ring-protocol-config-command.md.]`

**`spad_ctrl_entry` — 8 B** (one op-list slot; libnrt struct DB ordinal 9350): byte
`+0` is the `spad_ctrl_entry_header_t` (`cc_op:1` at byte0 bit0 = "active collective
op", `__reserved:7`); bytes `+1..+7` are the `spad_ctrl_cc_op_entry_t` command word.

**`spad_ctrl_cc_op_entry_t` — 7 B** (the per-step `cc_op` command word at entry+1;
struct DB ordinal 9386):

| field | location | width | meaning |
|:--|:--|:--|:--|
| `algo_type` | byte0 `[0:3]` | 4b | `enc_alg_type` (selects which config tape) |
| `algo_sub_type` | byte0 `[4:6]` | 3b | `enc_alg_mesh_type` sub-selector |
| `trigger_next` | byte0 `[7]` | 1b | chain to next op |
| `reporter` | byte1 `[0]` | 1b | this rank reports completion |
| `ring_wait_complete` | byte1 `[1]` | 1b | wait on ring completion |
| `ring_send_complete` | byte1 `[2]` | 1b | wait on ring send |
| `safe_mode` | byte1 `[3]` | 1b | wire bit (host-only in the mark log; name ambiguous safe_mode/permute_chain) |
| `unique_tensors` | byte1 `[4]` | 1b | wire bit (not printed by libncfw) |
| `__reserved0` | byte1 `[5:7]` | 3b | — |
| `__reserved` | byte2 (entry+3) | 1B | reserved before the union |
| `channel_list` (RING) | entry+4 | u32 | bitmask of 32 ring channels |
| `sema_shift_offset` (MESH) | entry+4 | u16 | mesh sema-addressing union arm |
| `sema_mask` (MESH) | entry+6 | u16 | mesh sema mask |

```c
/* one op-list slot — 8 B; the device walks this table per collective step. [HIGH/OBSERVED] */
typedef struct { uint8_t cc_op:1, __reserved:7; } spad_ctrl_entry_header_t; /* +0 */
typedef struct {
    uint8_t  algo_type:4, algo_sub_type:3, trigger_next:1;   /* byte0 */
    uint8_t  reporter:1, ring_wait_complete:1, ring_send_complete:1,
             safe_mode:1, unique_tensors:1, __reserved0:3;    /* byte1 */
    uint8_t  __reserved;                                      /* byte2 (entry+3) */
    union { uint32_t channel_list;                            /* RING  (entry+4) */
            struct { uint16_t sema_shift_offset, sema_mask; }; }; /* MESH */
} __attribute__((packed)) spad_ctrl_cc_op_entry_t;            /* 7 B at entry+1 */
typedef struct { spad_ctrl_entry_header_t header; spad_ctrl_cc_op_entry_t cc_op; } spad_ctrl_entry; /* 8 B */
```

> **GOTCHA — the libncfw printer prints fewer flags than the wire record carries.** The
> decoder emits only 3 of the byte1 flags; the on-wire record also carries `safe_mode`
> (byte1 bit3) and `unique_tensors` (byte1 bit4) plus a reserved byte at entry+3. Size
> the entry at **8 B total** and the packer is `create_spad_ctrl_entry @0x232cd0`
> (libnrt). The **reduce op is NOT in this word** — `algo_type` is how to route;
> `SDMA_CCETYPE` (rides the CCE descriptor) is what to compute. `[HIGH/OBSERVED.]`

#### 1.5b The op-kind / scope / algorithm enums

| enum | DIE | values | notes |
|:--|:--|:--|:--|
| `enc_op_type` | `<0x3bbd0>` | 14 ords `0..13`, prefix `ENC_` (`ENC_ALLGATHER=0` … `ENC_ALLTOALL_V=12`, `ENC_OP_INVALID=ENC_OP_N=13`) | the high-level op the op-list lowers; 13 real kinds 0..12 |
| `enc_alg_type` | `<0x61eb3>` | 12 ords `0..11` (`ENC_ALG_RING=0`, `ENC_ALG_HIER=1`, `ENC_ALG_MESH=2`, `ENC_ALG_KANGARING=3`, … `ENC_ALG_BW_OPT_MESH=10`, `ENC_ALG_INVALID=11`) | the `cc_op` `algo_type` nibble → which config tape + which DRAM+0xB0 case |
| `enc_alg_mesh_type` | `<0x125d7c>` | `FULL_MESH=0, GROUPED_MESH=1, MESH_TRN2=2, MESH_SWITCH=3, INVALID=4` | the `algo_sub_type` (3-bit) |
| `enc_comm_type` | `<0x34a15>` | `H_COMM_INTRA_ID=0, H_COMM_INTER_ID=1, H_COMM_MAX_ID=2` | topology SCOPE: intra-die vs inter-die |
| `SDMA_CCETYPE` | `<0x337be>` | `ADD=0, FMA=1, MAX=2, MIN=3, EXT=4, GCE=5` | the reduce op (rides the CCE descriptor, not the `cc_op` word) |
| `reduction_type_t` | `<0x60c987>` | `RING_2R1W=0, RING_2R2W=1, KANGARING_NR1W=2` | fold access pattern |

Each is a 4-byte enum (`DW_AT_const_value`). The DRAM+0xB0 12-entry computed-goto
table on the NCFW core is indexed by the `algo_type` nibble. Full roster
cross-referenced to ISA `COLLECTIVE_TYPE` and `nrt_op_type`:
[collective-enums](../collectives/ops/collective-enums.md). `[HIGH/OBSERVED]`

#### 1.5c The communicator + proxy-task

**`enc_comm_info` — 72 B** (the communicator topology descriptor, built by
`nrt_cc_global_comm_init @0x7fd90`):

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0` | 4 | u32 | `neuron_dev` | device id |
| `+4` | 4 | u32 | `rank` | this rank |
| `+8` | 4 | u32 | `rank_n` | global rank count |
| `+12` | 4 | u32 | `local_rank_n` | ranks in the local group |
| `+16` | 4 | u32 | `local_rack_rank_n` | ranks in the local rack |
| `+20` | 4 | u32 | `node` | this node |
| `+24` | 4 | u32 | `node_n` | node count |
| `+28` | 4 | u32 | `enc_topo_mode` | topology mode |
| `+32` | 1 | u8 | `enable_pod` | pod enabled |
| `+33` | 1 | u8 | `use_net` | RDMA/network in use |
| `+36` | 4 | u32 | `pod` | this pod |
| `+40` | 4 | u32 | `pod_n` | pod count (… through +72) |

**Proxy-task** = `enc_network_proxy_task` (source `proxy_queue.cc`). The
basic-block-switch poll is `enc_network_proxy_task::check_basic_block_switch(uint32_t,
bool*) @0x1d01c0`; the driver is `proxy_progress @0x1d03f0`. It polls per-stream
`basic_block_id` / `processing` / `remaining` from an `enc_fifo_set` map and resolves
the switch doorbell at `LOCAL_REG + 0x15e0` (abs `0x615e0`). Proxy tasks are created
by `enc_enq_proxy_tasks @0x105560` during `nrt_cc_prepare` (the barrier + network proxy
tasks). `[HIGH/OBSERVED — host symbols + polled field names; no byte-offset struct
layout beyond the polled fields.]`

#### 1.5d The algorithm config tape — the three-region container

The runtime config struct (`cfg` ptr passed to `ncfw_log_algo_configs`) is a single
object with **three adjacent sub-regions**, each emitted by its own `ncfw_log_algo_*`
printer:

| sub-region | base in `cfg` | element | printer |
|:--|:--|:--|:--|
| RING configs | `+0x0000` | 32 channels × **148 B** (stride `0x94`) | `ncfw_log_algo_ring_configs` |
| MESH configs | `+0x1280` | N events × **80 B** (`0x50`) — N = 50 (SUNDA) / 108 (CAYMAN+) | `ncfw_log_algo_mesh_configs` |
| HIERARCHICAL configs | `+0x2220` (SUNDA) / `+0x3440` (CAYMAN+) | 8 B (`__stub` u64 handle) | `ncfw_log_algo_hierarchical_configs` |

Total container = `0x2228` (SUNDA, mesh region `0xFA0` = 50×80) / `0x3448` (CAYMAN /
MARIANA / MARIANA_PLUS, mesh region `0x21C0` = 108×80) — the mesh element count is the
`mov r8d` immediate in `ncfw_log_algo_mesh_configs`. `[HIGH/OBSERVED.]`

> **CORRECTION — the per-channel ring stride is 148 (`0x94`), not 149.** An earlier
> framing (and the `mesh-collective` page's "32 channels × 149 B" shorthand) recorded
> `0x95 = 149` from the index arithmetic `i<<3,+i,<<2,+i,<<2,+i`. The byte-exact
> `lea`/`shl` chain (`ncfw_log_algo_ring_configs @0x8544`) is `cfg + 148·i`: the last
> stored field is `kangaring_num_peers @+0x90` (1 byte), so a channel spans
> `+0x00..+0x90` = 145 used bytes with **3 pad bytes** (`+0x91..+0x93`) inside a
> **148-byte** stride. Pin 148. `[OBSERVED HIGH — ring-kangaring.md.]`

#### 1.5e The per-channel ring config (channel/peer) — 148 bytes

`ncfw_log_algo_ring_configs` (`@0x8544`) emits a `"channels"` array of **32 entries**,
base `cfg + 148·i`. One "channel" is one CC-topology ring; `channel_id` is the loop
index (not a stored field). The peer/rank topology lives here (the `next_neigh` /
`prev_neigh` neighbours), distinct from the `pring` transport (§1.5h).

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | `0x1c` | `ring_neighbor` | `next_neigh` | downstream peer descriptor (§1.5f) |
| `+0x1c` | `0x1c` | `ring_neighbor` | `prev_neigh` | upstream peer descriptor |
| `+0x38` | 8 | u64 | `recv_sema.soc_addr` | inbound rendezvous semaphore (SOC phys) |
| `+0x40` | 8 | u64 | `send_sema.soc_addr` | outbound semaphore |
| `+0x48` | 8 | u64 | `post_sema.soc_addr` | post/completion semaphore |
| `+0x50` | 8 | u64 | `dma_compl_sema.soc_addr` | DMA-completion semaphore |
| `+0x58` | `0x20` | `kring_peer_semas` | `kring_peer_semas` | kangaring peer-semaphore handshake block (§1.5f) |
| `+0x78` | `0x14` | `dma_apb_bcast` | `dma_apb_bcast` | APB-broadcast DMA descriptor (20 B, §1.5f) |
| `+0x8d` | 1 | u8 | `spad_slot_idx` | scratchpad slot index |
| `+0x8e` | 1 | u8 | `fold_n` | fold factor (channel-level) |
| `+0x8f` | 1 | u8 | `kangaring_is_primary` | kangaring primary-node flag |
| `+0x90` | 1 | u8 | `kangaring_num_peers` | kangaring peer count (≤ 3); last stored byte → 3 pad → 148 stride |

`[HIGH/OBSERVED — every offset from a host `mov`/`lea`/`movzx` site in libncfw.]`

#### 1.5f The peer / neighbour sub-structs

**`ring_neighbor` — 28 B (`0x1c`)** (one folded logical neighbour;
`ncfw_log_algorithm_ring_neighbor @0x37f1`):

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | `fold_n`×8 | u64[] | `net_idx_addrs[]` | physical RDMA/network endpoints per folded neighbour (≤ 3 entries, bounded by `fold_n@+0x18`) |
| `+0x18` | 1 | u8 | `fold_n` | array bound (neighbour-level, distinct from channel `fold_n@+0x8e`) |
| `+0x19` | 1 | u8 | `type` | link type |

**`kring_peer_semas` — 32 B (`0x20`)** (the kangaring peer-semaphore fanout;
`ncfw_log_algo_ring_kring_peer_semas @0x4d64`): a fixed 4-slot semaphore array —

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 8 | u64 | `mine.soc_addr` | this node's own semaphore |
| `+0x08` | 8 | u64 | `peers[0].soc_addr` | peer 0 semaphore |
| `+0x10` | 8 | u64 | `peers[1].soc_addr` | peer 1 |
| `+0x18` | 8 | u64 | `peers[2].soc_addr` | peer 2 (`peer_id` is positional, not stored) |

**`dma_apb_bcast` — 20 B (`0x14`)** (the per-channel APB-broadcast DMA descriptor,
shared verbatim by ring AND mesh; `ncfw_log_dma_channel_apb_bcast @0x4899`):

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 8 | u64 | `m2s_tail_ptr.soc_addr` | memory→semaphore ring tail ptr |
| `+0x08` | 8 | u64 | `s2m_tail_ptr.soc_addr` | semaphore→memory ring tail ptr |
| `+0x10` | 4 | u32 | `mask` | peer mask |

The peer ordinals are the `encd_neigh` enum (DIE `<0x3a717>`, prefix `ENCD_NEIGH_`):
`LOCAL=0, NEXT=1, PREV=2, GATEWAY=3, PEER_RMTV=4, PEER_RMTV2=5, PEER_LOCAL=6,
NEXT_PEER_RMTV=7, INVALID=NUM=8` — `NEXT_PEER_RMTV=7` is the skip-ahead "kangaroo" hop.
`[HIGH/OBSERVED — ring-kangaring.md, every offset from a host `mov`/`movzx` site.]`

#### 1.5g The mesh event struct (`ncfw_mesh_event`) — 80 bytes (`0x50`)

The MESH config region is N × this struct (`ncfw_log_configs_algo_mesh_events`). It
reuses the ring's APB-broadcast DMA descriptor verbatim.

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | `0x14` | `dma_apb_bcast_t` | `dma_apb_bcast[0]` | APB-broadcast DMA leg 0 (20 B) |
| `+0x14` | `0x14` | `dma_apb_bcast_t` | `dma_apb_bcast[1]` | APB-broadcast DMA leg 1 |
| `+0x28` | 8 | u64 | `direct_trigger_sema[0]` | neighbour-die fanout sema 0 (SOC phys) |
| `+0x30` | 8 | u64 | `direct_trigger_sema[1]` | fanout sema 1 |
| `+0x38` | 8 | u64 | `direct_trigger_sema[2]` | fanout sema 2 |
| `+0x40` | 8 | u64 | `event_wait_sema` | inbound guard semaphore (SOC phys) |
| `+0x48` | 4 | u32 | `wait_val` | counted-wait target |
| `+0x4c` | 1 | u8 | `event_type` | `enc_mesh_event_type` |
| `+0x4d` | 1 | u8 | `dma_trigger` | do the DMA legs this event? |
| `+0x4e` | 1 | u8 | `direct_trigger` | do the sema fanout this event? |
| `+0x4f` | 1 | u8 | `wait_event` | do the inbound wait this event? |

`[HIGH/OBSERVED — mesh-collective.md, every offset from a host `mov`/`movzx`.]`

#### 1.5h The persistent ring (`pring`) descriptors

The `pring` is the **physical** DMA descriptor ring the collective steps launch — a
contiguous array of 16-byte Annapurna-Labs UDMA hardware buffer descriptors in HBM,
owned host-side by a 32-byte `dma_ring_info`. It is **not** the peer/rank topology
(that is the §1.5e channel tape).

**`union al_udma_desc` — 16 B** (DWARF DIE `<0x124eda>`, `DW_AT_byte_size 16`): the
M2S/S2M/raw views of one UDMA descriptor; `len_ctrl @+0x00` packs the byte count plus
flags (NOT a plain length). This is the same 16-B BD documented in the
[DMA descriptor-ring field tables](../dma/descriptor-ring-field-tables.md).

```c
al_udma_desc pring[pring_total_desc_num];   /* contiguous @ pring_base_addr in HBM */
```

**`struct dma_ring_info` (`dma_ring_info_t`) — 32 B** (DWARF DIE `<0x128d55>`,
`DW_AT_byte_size 32`):

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 4 | enum | `type` | `dma_ring_type_t` (`INVALID=0` \| `TX=1` (M2S) \| `RX=2` (S2M); never INVALID at use) |
| `+0x08` | 8 | ptr | `ring_mem` | backing memory handle |
| `+0x10` | 8 | u64 | `ring_offset_bytes` | byte offset of the ring base within `ring_mem` |
| `+0x18` | 4 | u32 | `used_desc_count` | live descriptor count |
| `+0x1c` | 4 | u32 | `allocated_desc_count` | descriptors allocated (`== pring_total_desc_num`) |

`pring_base_addr = device(ring_mem) + ring_offset_bytes`;
`pring_total_desc_num == allocated_desc_count`. `[HIGH/OBSERVED — pring-descriptors.md;
`type@+0`, `ring_mem@+8` from the DWARF DIE.]`

#### 1.5i Proxy-task → hierarchical (metaring / hier topology)

The **hierarchical** algorithm (`enc_alg_type 1 = ENC_ALG_HIER`) is the classic
Rabenseifner two-level decomposition: a cheap intra-level (cores within a die-group)
plus an expensive inter-level. It does **not** carry its own descriptor tape — the host
`enc_hier_primitive` builder (`libnrt.so`, fully symbolized) composes a multi-leg
program from `enc_*` primitives that each emit ring/mesh tapes. Key builder members:
`__select_algorithms() @0x14c620` (chooses the **5 per-leg algos**: `alg_intra_allg`,
`alg_intra_redsct`, `alg_inter_allr`, `alg_inter_allg`, `alg_inter_redsct`, each an
`enc_alg_type`), `compose_operation(enc_mr_cache*) @0x1a8d20` (driver), and the
INTRA/INTER reduce-scatter / all-gather / all-reduce page-table builders
(`__build_*_pgt_{intra,inter}_* @0x14e320..0x1558f0`). The two scopes are the
`enc_comm_type` enum (`H_COMM_INTRA_ID=0`, `H_COMM_INTER_ID=1`). The proxy-task /
metaring orchestration is part of this host compose pipeline; at the `libncfw` firmware
boundary the hierarchical config reduces to an **8-byte `__stub` u64 handle** (the
HIERARCHICAL region of §1.5d, `ncfw_log_algo_hierarchical_configs @0x18fcb`, INFERRED to
be a DRAM pointer to a per-leg level-descriptor table) plus the **per-leg ring/mesh
tapes** (§1.5e/§1.5g) the NCFW core walks — there is no distinct field-rich
firmware-resident "hierarchical struct". Full treatment:
[hierarchical-collective](../collectives/ncfw/hierarchical-collective.md),
[mesh-collective](../collectives/ncfw/mesh-collective.md),
[ring/kangaring](../collectives/ncfw/ring-kangaring.md),
[pring-descriptors](../collectives/ncfw/pring-descriptors.md). `[HIGH/OBSERVED — host
DWARF + libncfw printer offsets.]`

> **WALL — NCFW carry walls.** The NCFW config structs are `OBSERVED` on SUNDA (NC-v2,
> `arch_id 0x05`, coretype `0x06`) / CAYMAN (NC-v3, `0x0c`, ct `0x0d`) / MARIANA
> (NC-v4, `0x14`, ct `0x15`) / MARIANA_PLUS (NC-v4+, `0x1c`, ct `0x1d`) — the host DWARF
> and `libncfw` printer offsets are arch-common. The relation `arch_id = coretype − 1`
> is `OBSERVED` on those four. **No NCFW image ships for MAVERICK (NC-v5)**: the host
> `get_image` selector ladder stops at `arch_id 0x1c`, there is **no** `cmp 0x24`
> (arch_id 36) arm, and the blob region closes into `__GNU_EH_FRAME_HDR @0x918e4`. So
> the v5 interior is `INFERRED`. **`coretype 37` itself is OBSERVED** (the `maverick_libs`
> target + `nrtucode.h:56` + the two resolver bitmasks, §3 of the
> [Cross-Walk card](codename-crosswalk-table.md)); only the v5 NCFW *image* is file-absent
> and the dependent `arch_id 36` is INFERRED. `[arch_id 0x05/0x0c/0x14/0x1c + coretype
> {6,13,21,29,37} OBSERVED; v5 NCFW image OBSERVED-absent; v5 arch_id 36 INFERRED.]`
>
> > **CORRECTION — coretype 37 (`ct37`) is OBSERVED; only the NCFW *image* is absent.
> > Do NOT conflate the two.** A prior draft of this callout flipped `ct37` to INFERRED
> > by folding the (true) NCFW-image absence into a (false) coretype-value absence. The
> > binary refutes the flip: **`ct37` is OBSERVED three independent ways** — re-grounded
> > against `libnrtucode_internal.so` this pass — and the do-not-repeat distinction is
> > **"v5 NCFW *image* file-absent (TRUE, OBSERVED-negative) ≠ coretype *value* 37
> > unobserved (FALSE — it IS observed, OBSERVED-positive)."** The three reads:
> > **(1)** the `maverick_libs` jump-table target (`get_ext_isa` case `idx 31` ⇒
> > `lea … 0x9b9050 <maverick_libs>`, `nm`-confirmed @`0x9b9050`); **(2)** the enum
> > ordinal `NRTUCODE_CORE_MAVERICK_Q7_POOL = 37` (`nrtucode.h:56`); **(3)** the two
> > resolver bitmasks `movabs $0x2020202000` (`→ {13,21,29,37}`) and `$0x2020202040`
> > (`→ {6,13,21,29,37}`) — **bit 37 set in both** — gated by `cmp $0x25,%edi` (37). So
> > `ct37` is `[HIGH/OBSERVED]`. What remains absent is the *image*: the host `libncfw
> > get_image` selector ladder closes at `arch_id 0x1c`, there is **no `cmp $0x24` arm**,
> > and `MAVERICK_Q7_CC_TOP*_get = 0` — so the **v5 NCFW orchestration image is
> > file-absent** (OBSERVED-negative), and the dependent `arch_id 36` is INFERRED (§3
> > / [the master ledger §1](do-not-repeat-full-ledger.md), the
> > [Cross-Walk card §3](codename-crosswalk-table.md)). These are different artifacts;
> > the image's absence does not unobserve the coretype value. `[ct37 HIGH/OBSERVED; v5
> > NCFW image HIGH/OBSERVED-absent; arch_id 36 MED/INFERRED.]`

---

## Part 2 — Device global structs

These structures hold the firmware's **global state** — boot identity, per-engine
sequencer state, profiler config, and the host-readable stdio ring. Unlike Part 1's
per-instruction dispatch structs, these persist across the dispatch loop.

### 2.1 `.globstruct` — the boot/runtime global (DRAM[0]) `[HIGH/OBSERVED]`

The on-device firmware image's `.globstruct` section (PROGBITS, VMA `0x02000408`, size
`0x48` = 72 B, `CONTENTS, ALLOC, LOAD` inside the `0x02000000` PT_LOAD) is the shared
dispatcher state block. Its first word is the **boot ready sentinel**, present in DRAM
the moment the host BAR0-writes the segment (a *linked image initializer*, not a
runtime store). It sits **immediately after** the `kernel_info_table` — the table's end
VMA (`0x02000408`) is exactly `.globstruct`'s base, which is why the table scan uses
that boundary as its terminator.

The full 72-byte word map, **re-read this session from the carved ELF** (file off
`0x7488`):

| offset | word | offset | word | offset | word |
|:--|:--|:--|:--|:--|:--|
| `+0x00` | **`0x6099CB34`** ⟵ READY sentinel | `+0x18` | `0x00001000` | `+0x30` | `0xFFFFFF00` |
| `+0x04` | `0x00000000` | `+0x1c` | `0x00001000` | `+0x34` | `0xFFFFFF00` |
| `+0x08` | `0x00000000` | `+0x20` | `0x00001000` | `+0x38` | `0x00000000` |
| `+0x0c` | `0x00000000` | `+0x24` | `0x00001000` | `+0x3c` | `0x00000000` |
| `+0x10` | `0x00000000` | `+0x28` | `0xFFFFFF00` | `+0x40` | `0xFFFFFFFF` |
| `+0x14` | `0x00000000` | `+0x2c` | `0xFFFFFF00` | `+0x44` | `0xFFFFFFFF` |

```c
/* .globstruct @ VMA 0x02000408, 0x48 B — the shared dispatcher state block. [HIGH/OBSERVED]
 * Word 0 is the boot-identity magic; the four 0x00001000 are per-something size fields,
 * the four 0xFFFFFF00 are masks, the two trailing 0xFFFFFFFF are terminators. */
struct globstruct {
    uint32_t ready_sentinel;   /* +0x00  0x6099CB34 — host claim handshake key (§2.2) */
    uint32_t reserved0[5];     /* +0x04..+0x17  zero */
    uint32_t size_field[4];    /* +0x18..+0x27  0x00001000 each */
    uint32_t mask_field[4];    /* +0x28..+0x37  0xFFFFFF00 each */
    uint32_t reserved1[2];     /* +0x38..+0x3f  zero */
    uint32_t terminator[2];    /* +0x40..+0x47  0xFFFFFFFF each */
};                             /* sizeof == 0x48 */
```

The per-kernel `.bss` state slots (`0x02000458 … 0x0200047c`) the POOL trampolines
load are **past** `.globstruct` (which ends at `0x02000450`), in the image's `.bss`
(VMA `0x02000450`, size `0x3c`) — zero-initialised per-opcode scratch pointers, not
part of `.globstruct`. `[HIGH/OBSERVED]`

> **NOTE — per-core globals and the NX-local heap/window apertures.** `.globstruct` is
> the *shared* dispatcher state block (one copy, the boot-identity magic + size/mask
> fields). The firmware's **per-core** mutable state is kept disjoint, not in
> `.globstruct`: each of the 8 SPMD pool cores owns a **PRID-keyed hardware-disjoint
> DRAM aperture** (`idx = 9 + 2·cpu_id`, step `0x10000`), so there is no shared mutable
> state to race (the cores are data-race-free by construction; the XEA3 `L32EX`/`S32EX`
> exclusive monitor is essentially unexercised). The per-engine SEQ state struct
> (`state[0x855e0]`, §2.3), the SEQ stdio `FILE*` at DRAM `0x84d28`, and the SOC-window
> global the stdio ring routes through (`0x80093c4`) are the **NX-local heap/window
> globals** — per-core/per-engine DRAM-resident state distinct from the shared
> `.globstruct`. `[per-core PRID aperture CARRIED from the concurrency/boot pages; the
> `0x855e0`/`0x84d28`/`0x93c4` DRAM addresses OBSERVED.]`

### 2.2 The DRAM[0] boot/claim handshake — two magic constants `[HIGH/OBSERVED]`

The two magic constants are the device↔host boot/claim handshake. The host
`nrtucode_core_on_ucode_booted` (x86, `libnrtucode.so @0x308f90`) reads DRAM[0]
(`core->target` at `0x20(%rbx)`) and branches:

| word | hex (LE bytes) | direction | meaning |
|:--|:--|:--|:--|
| `0x6099CB34` | `34 cb 99 60` | device → host | **READY**: the booted image's identity magic is present at DRAM[0]. On match, host stages and writes the CLAIM word, sets `boot_state = 1` (host field at `0x30(%rbx)`). |
| `0x502B2DA1` | `a1 2d 2b 50` | host → device | **CLAIM**: this core is now claimed by THIS `nrtucode_core_t` handle (single-owner lock). |
| any other | — | — | "booted image is incompatible" → fail 8 (version/identity gate). |
| reads `0x502B2DA1` | — | — | "already claimed by another handle" → fail 8. |

```c
/* nrtucode_core_on_ucode_booted — the one-word spin-mailbox claim. [HIGH/OBSERVED] */
int on_ucode_booted(nrtucode_core_t *core) {
    uint32_t v;
    read_device(core->target /* DRAM[0] */, 4, &v);      /* vtable slot 0 */
    if (v == 0x6099CB34u) {                              /* READY sentinel */
        v = 0x502B2DA1u;                                 /* stage CLAIM */
        write_device(core->target, 4, &v);              /* vtable slot +8 */
        core->boot_state = 1;
        return 0;                                        /* core CLAIMED */
    }
    if (v == 0x502B2DA1u) return 8;                       /* already claimed */
    return 8;                                            /* magic mismatch */
}
```

**Re-verified this session:** READY `0x6099CB34` appears **99×** and CLAIM
`0x502B2DA1` appears **2×** in `libnrtucode_internal.so` (the 99 = host compare/stage
sites + the embedded device images carrying the `.globstruct` initializer). The
compares are byte-exact (`41 81 f9 34 cb 99 60` / `41 81 f9 a1 2d 2b 50`). This is a
**spin-mailbox claim, not a hardware CAS** — serialization comes from per-core
single-host-thread bring-up ordering. Full boot spine: [Boot / Reset Sequence +
Startup Config](../uarch/boot-reset.md). `[HIGH/OBSERVED]`

> **GOTCHA — read/write target vs boot_state are different addresses.** The handshake
> reads/writes 4 bytes at the **device** DRAM address in `core->target` (`0x20(%rbx)`);
> `boot_state` is a **host-side** field of `nrtucode_core_t` (`0x30(%rbx)`). A
> reimplementer wiring the `rw_impl` vtable maps slot 0 → `read_device`, slot +8 →
> `write_device`, both `(impl, target, len, buf)`. `[HIGH/OBSERVED]`

### 2.3 Per-engine SEQ/dispatch state (`instr_fetch_queue` / pop_state) `[HIGH/OBSERVED]`

The SEQ engine's running state lives in a central per-engine state struct at DRAM
`0x855e0` (`state[0x855e0]`), with a second per-engine descriptor at `0x85f88`. The
fetch front-end's cursor and the dispatch preamble read it on every iteration.

| field | address | size | meaning |
|:--|:--|:--|:--|
| SW running flag | `state[0x855e0 + 100]` | 1 (bit0) | `1` on `enter_run`, `0` on pause/halt; read by poll-surprises `@0x6af4` (`l8ui; extui …,0,1`). The empty-flag fast exit. |
| Sunda-mode flag | `state[0x855e0 + 108]` | 1 (bit0) | `1` = Sunda SW-fetch FSM (table `0x80814`), `0` = HW-decode FSM (table `0x80adc`). Per-runtime-mode, not per-gen. |
| per-engine descriptor base | `state[0x855e0 + 24]` | — | the pc→iram_pc translate descriptor base (read by `0x6b70`/`0x57a1`). |
| HW-decode-active flag | `state[0x85070]` | 1 | SW-cache vs HW-cache fork (read in the redirect machine `0x5750 @0x58dc`). |
| second per-engine descriptor | `state[0x85f88 + 20]` | 1 (bit0) | the dispatch-preamble's second descriptor (read `@0x249c`). |

The **architectural PC** is a 64-bit value in the core CSR pair `0x1060` (PC_lo) /
`0x1080` (PC_hi = `0x1060 + 0x20`), materialised onto the stack at `enter_run` and
rewritten on a redirect. The **fetch cursor** is register `a4` — it addresses the
32-bit instruction word; the low byte is the opcode.

```c
/* SEQ per-engine state (DRAM 0x855e0) + the fetch/dispatch preamble. [HIGH/OBSERVED] */
static inline bool seq_is_running(void) {
    return (*(volatile uint8_t *)(0x855e0 + 100)) & 0x1u;   /* poll-surprises @0x6af4 */
}
/* the kernel_info_table scan preamble (POOL side): build the packed key, load the
 * base/end getters, derive count = (end-base)>>3 = 17, linear-scan, callx8 on hit. */
uint32_t key   = (opcode << 24) | (spec << 16);
KernelInfo *b  = kernel_info_table_base();   /* getter @0x010055f8 -> 0x02000380 */
KernelInfo *e  = kernel_info_table_end();    /* getter @0x01005607 -> 0x02000408 */
for (size_t i = 0; i < (size_t)(((char*)e - (char*)b) >> 3); ++i)
    if (b[i].key == key) { ((void(*)())b[i].funcVA)(); return; }   /* 1 hop */
```

The **`instr_fetch_queue` / pop_state** advance is the notify-consume path
(`0x1c64 → 0x3a78` drains the surprise/notify queue, returns the next in-stream word in
`a2`); the cursor store `s32i a2,[a4]` commits the advance. The **kernel_info_table
scan preamble** (the POOL side) derives `count = (end − base) >> 3 = 17` from two tiny
getters before the linear scan. Full fetch FSM + the redirect machine:
[SEQ Fetch + PC-Redirect Front-End](../firmware/seq/fetch-pc-redirect.md);
the FSM loop and the `state[0x855e0]` lifecycle: [SEQ Main FSM Loop](../firmware/seq/main-loop.md).
`[HIGH/OBSERVED]`

### 2.4 Profiler `PROF_CAM` (64×16 B) / `PROF_TABLE` (64×128 B) `[HIGH/OBSERVED]`

The per-engine HW instruction-decode profiler is two parallel arrays staged from
`libnrtucode_internal.so` getters. **Re-verified this session:** the CAM getter
(`CAYMAN_NX_ACT_PROF_CAM_get @0x9b3ba0`) writes `movq $0x400,(%rsi)` = **1 KiB = 64
slots × 16 B**; the TABLE getter (`@0x9b3bc0`) writes `movq $0x2000,(%rsi)` = **8 KiB =
64 records × 128 B**.

**`hwdecode_cam_record` — 16 B** (the opcode-match CAM; armed slots occupy contiguous
`0..N-1`, last armed is always the `{0,0,1}` wildcard catch-all):

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 4 | u32 | `opcode_id` | full-width 32-bit opcode to match (admits the 9-bit extended opcode `0x1e3` on DVE) |
| `+0x04` | 4 | u32 | `mask` | match mask, strictly ∈ `{0x00 wildcard, 0xff exact-8-bit, 0x1ff 9-bit}` |
| `+0x08` | 4 | u32 | `enable` | `1` = armed; strictly ∈ `{0,1}` |
| `+0x0c` | 4 | u32 | `reserved` | strictly `0` |

**`hwdecode_table_record` — 128 B** (the parallel per-opcode descriptor; `PROF_TABLE[i]`
binds to `PROF_CAM[i]`; sparse — global max nonzero offset is `0x17`):

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 4 | u32 | `cfg0` | packed cfg word (compute opcodes only) |
| `+0x04` | 4 | u32 | `cfg1` | packed cfg word |
| `+0x08` | `0x0c` | u8[12] | `descr` | packed per-opcode descriptor bytes (`0x08..0x13`) |
| `+0x14` | 4 | u32 | `klass` | per-opcode counter/latency CLASS word (control opcodes carry ONLY this) |
| `+0x18` | `0x68` | u8[104] | `zero` | always 0 (`0x18..0x7f`) |

```c
struct hwdecode_cam_record   { uint32_t opcode_id, mask, enable, reserved; };  /* 16 B  */
struct hwdecode_table_record { uint32_t cfg0, cfg1; uint8_t descr[12];
                               uint32_t klass; uint8_t zero[0x68]; };           /* 128 B */
```

The CAM decides *whether* an opcode is profiled and which slot it binds; the TABLE's
`+0x14 klass` word decides *which counter class* it increments (the device CSRs
`ic0_opcode`/`ic1_opcode` "if matches → INCREMENT an ICn counter"). CAYMAN arms 47
opcodes on every engine (one shared blob); from MARIANA the profiler is per-`(gen,
engine)`. Full format + the PROF-vs-activation-PWL disambiguation:
[PROF_CAM / PROF_TABLE Blob Formats](../images/prof-cam-table-formats.md).
`[HIGH/OBSERVED]`

> **CORRECTION — `PROF_CAM`/`PROF_TABLE` are the instruction-decode profiler, NOT the
> activation PWL.** A 47-of-64 / 128-B *size* coincidence drove a prior conflation with
> the activation transcendental lookup. The 16-B opcode-only CAM (no `func_id`), the
> 9-bit `0x1e3`/mask-`0x1ff` record, the `≤0x17` sparse TABLE, and the absence of any
> BUCKET/CONTROL table refute it. The activation PWL is a separate ACT-only four-table
> quad, not in `libnrtucode` at all. `[HIGH/OBSERVED]`

### 2.5 The device `pool_stdio_queue` ring descriptor (256-B slots) `[HIGH/OBSERVED]`

On the **SUNDA** generation, the Q7 custom-op kernel's `stdout`/`stderr` is a
hand-rolled single-producer DRAM ring of **256-byte slots**, owned by a static
two-file set (`stdout_file`, `stderr_file`). Each file object is **`0x130` (304) bytes**
(`stdout_file` at `manager+0`, `stderr_file` at `manager+0x130`).

| offset | size | type | field | meaning |
|:--|:--|:--|:--|:--|
| `+0x00` | 1 | u8 | `enabled` | write/flush/guard all bail if 0 (disabled = silent no-op) |
| `+0x01` | 1 | u8 | `full_policy` | asserted `== 2 == ..._QUEUE_FULL_POLICY_OVERWRITE` |
| `+0x0c` | 4 | u32 | `num_slots` | ring size in 256-byte slots (`head % num_slots` wrap) |
| `+0x10` | 8 | u64 | `ring_dram` | ring DRAM base (lo@+0x10, hi@+0x14) |
| `+0x18` | 8 | u64 | `headptr_dram` | head-publish DRAM addr (lo@+0x18, hi@+0x1c) |
| `+0x28` | 256 | u8[256] | `stage` | the 256-B staging slot: `+0x28` 16-B header + `+0x38` `char[240]` line buffer |
| `+0x34` | 4 | u32 | `fill_count` | bytes in the 240-B buffer — **aliases inside** the `+0x28` staging block (offset 12) |
| `+0x128` | 4 | u32 | `head` | monotonic write-sequence counter |

```c
/* file_io_file — 0x130 (304) B; one virtual file, SUNDA Q7 file_io_manager.hpp. [HIGH/OBSERVED]
 * The transfer unit is a 256-B slot = 16-B header (+0x28) + 240-B body (+0x38). */
struct file_io_file {
    uint8_t  enabled;        /* +0x00 */
    uint8_t  full_policy;    /* +0x01  == 2 OVERWRITE */
    uint32_t num_slots;      /* +0x0c */
    uint64_t ring_dram;      /* +0x10 */
    uint64_t headptr_dram;   /* +0x18 */
    uint8_t  stage[256];     /* +0x28  (fill_count@+0x34 aliases here) */
    uint32_t head;           /* +0x128 */
};
```

The flush protocol is textbook SPSC: build 240-B line → `memcpy` whole 256-B slot to
`ring_soc[(head % num_slots) * 256]` → clear slot → `head++` → publish new head to a
**separate** DRAM word (`headptr_dram`) → `memw` fence. The full policy is `OVERWRITE`
(no back-pressure; the ring math always wraps). The host polls the published head and
drains 256-B slots.

> **GOTCHA — `fill_count@+0x34` aliases INSIDE the staging slot.** `+0x34` falls at
> offset 12 within the `+0x28` staging block. The firmware reuses a word of the slot
> region as the live fill cursor, then flushes the whole `+0x28..+0x127` block. A
> reimplementation that puts `fill_count` in its own non-overlapping field is
> functionally fine but **not** byte-compatible, and cannot blindly `memcpy` the
> staging region without re-deriving the live fill value. `[HIGH/OBSERVED]`

> **CORRECTION — `pool_stdio_queue` is SUNDA-only; CAYMAN+ uses newlib `printf`.** The
> `file_io_manager` 256-B-slot ring ships on **SUNDA Q7-POOL** (DEBUG + RELEASE) and is
> **retired** on CAYMAN / MARIANA / MARIANA_PLUS Q7, which emit kernel stdio via a
> newlib `printf` path with a `'P%i:'` prefix (the same family as the SEQ `'S:'`
> logger). This ring is the SUNDA-era predecessor, distinct from the `'S:'`/`'P%i:'`
> firmware-log channel. Full machinery: [On-Device Virtual File-I/O Manager](../firmware/kernels/file-io-manager.md).
> `[HIGH/OBSERVED]`

---

## Adversarial self-verification

The five strongest claims, each re-challenged against the carved binaries **this
session** (carve sha256 `910d41c3…b4b55527` matches; host loader
`libnrtucode_internal.so`):

| # | claim | challenge | verdict |
|--:|:--|:--|:--|
| 1 | **Two boot magics** `0x6099CB34` READY / `0x502B2DA1` CLAIM | `python3 bytes.count` over `libnrtucode_internal.so` | **HOLDS.** READY `34 cb 99 60` = **99×**, CLAIM `a1 2d 2b 50` = **2×** — matches boot-reset.md exactly. OBSERVED. |
| 2 | **`0xF0` byte-13 spec dispatch** = 5 table rows, specs `{0,1,2,4,3}`, spec @ key byte+2 | parse `kernel_info_table` @ off `0x7400`, decode `spec=byte[+2]` per row | **HOLDS.** Rows 6–10 = `op 0xf0` with `byte[+2] = 0/1/2/4/3` (registration order), funcVAs `0x01003370/80/0x3484/0x37a8/0x3a60`. OBSERVED. |
| 3 | **PROF sizes** CAM 64×16 B / TABLE 64×128 B | `objdump` the getter `movq` immediates | **HOLDS.** CAM getter `movq $0x400` (1 KiB = 64×16), TABLE getter `movq $0x2000` (8 KiB = 64×128). OBSERVED. |
| 4 | **`kernel_info_table` entry = 8 B**, `spec@+2 opcode@+3 funcVA@+4`, 17 rows | parse 0x88 bytes, count `len//8`, check byte positions | **HOLDS.** 136 B = 17 rows; every row `byte[+2]=spec, byte[+3]=opcode, [+4:+8]=funcVA LE`. Section geometry `readelf -SW`: `kernel_info_table @0x02000380 sz 0x88`, `.globstruct @0x02000408 sz 0x48`, `.bss @0x02000450 sz 0x3c`. OBSERVED. |
| 5 | **`.globstruct[0]` = `0x6099CB34`** + full 0x48 word map | read 72 bytes @ file off `0x7488` from the carved ELF | **HOLDS.** `+0x00 = 0x6099CB34`; `+0x18..+0x24 = 0x00001000`×4; `+0x28..+0x34 = 0xFFFFFF00`×4; `+0x40/+0x44 = 0xFFFFFFFF` — byte-for-byte the boot-reset.md map. OBSERVED. |

> **Single strongest CORRECTION (folded in-place, §1.5b/§1.5c).** The collectives
> per-channel **ring config stride is 148 (`0x94`), not 149.** The `mesh-collective`
> shorthand "32 channels × 149 B" came from the index arithmetic
> `i<<3,+i,<<2,+i,<<2,+i`; the byte-exact `lea`/`shl` chain in
> `ncfw_log_algo_ring_configs @0x8544` is `cfg + 148·i`, and the last stored field
> (`kangaring_is_primary @+0x8f`) plus 3 trailing pad bytes fits a 148-byte stride with
> 145 used bytes. The 80-byte mesh-event stride is unaffected. Pin 148 for the ring
> channel; the algo_configs container places MESH at `+0x1280` regardless.

---

## See also

- [kernel_info_table Binary Layout](../firmware/pool/kernel-info-table.md) — the POOL
  8-byte `(key, funcVA)` entry, full 17-row dump, and the linear-scan dispatcher (§1.1).
- [POOL Extended-Opcode (`0xF0`) Dispatch](../firmware/pool/pool-ext-0xf0.md) — the five
  `0xF0` spec rows, per-spec handler disassembly, and the Cptc codec family (§1.2).
- [SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md) — the 178-slot table, the
  trampoline→impl→thunk→`Handler::execute()` mechanism (§1.4).
- [SEQ Fetch + PC-Redirect Front-End](../firmware/seq/fetch-pc-redirect.md) — the fetch
  cursor, the `instr_fetch_queue`/pop_state advance, the PC-redirect machine (§2.3).
- [SEQ Main FSM Loop](../firmware/seq/main-loop.md) — the `state[0x855e0]` per-engine
  state lifecycle (§2.3).
- [CSR — NOTIFIC Queue](../control/csr/notific-queue.md) ·
  [Device→Host Interrupt / Notification Path](../control/interrupt/device-host-notification.md)
  — the 16-B notification record's NOTIFIC block, per-type bodies, and ring descriptor (§1.3).
- [On-Device Virtual File-I/O Manager](../firmware/kernels/file-io-manager.md) — the
  SUNDA Q7 256-B-slot stdio ring machinery (§2.5).
- [PROF_CAM / PROF_TABLE Blob Formats](../images/prof-cam-table-formats.md) — the full
  CAM/TABLE format + the PROF-vs-PWL disambiguation (§2.4).
- [Boot / Reset Sequence + Startup Config](../uarch/boot-reset.md) — the boot spine,
  `.globstruct` sentinel, and the `0x6099CB34 → 0x502B2DA1` claim handshake (§2.1/§2.2).
- [collective-enums](../collectives/ops/collective-enums.md) ·
  [pring-descriptors](../collectives/ncfw/pring-descriptors.md) ·
  [ring/kangaring](../collectives/ncfw/ring-kangaring.md) ·
  [mesh-collective](../collectives/ncfw/mesh-collective.md) ·
  [hierarchical-collective](../collectives/ncfw/hierarchical-collective.md) — the
  `enc_*` collectives class family (§1.5).
- [Struct Census Overview](struct-census-overview.md) — the master device/host struct
  census this page is the device-firmware half of.
- [The Confidence & Walls Model](../reference/confidence-model.md) — the
  OBSERVED/INFERRED/CARRIED × HIGH/MED/LOW tags used throughout.
