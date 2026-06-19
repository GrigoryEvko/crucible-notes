# SEQ SoC Window Manager

This page reconstructs the **SoC window manager** of the NX **sequencer (SEQ)
engine** — the device-side firmware that drives the per-engine **SoC↔Q7
address-translation windows**. When the sequencer (or a Q7 compute core sharing
its engine) issues a request against a Q7-local "XT" address, a hardware window
block rewrites it into a full SoC address; this firmware is the software that
*programs, re-points, allocates, and tears down* those windows. The class is
`soc_window_manager` (file `soc_window_manager.hpp`); it owns a per-window record
type (`xt_window.hpp`, the `xt_window_view`) and an engine/die address decoder
(`translate_cayman+.hpp` on the CAYMAN family). It is the load-time XT-address
resolver behind the [external-lib loader](../pool/external-lib-loader.md) and the
reclaim primitive behind library unload.

**The headline finding is a per-generation hardware-model shift.** The SEQ
manager exposes a *gen-stable* C++ interface
(`translate_soc_to_xt_address` / `program_window` / `update_window` /
`push_unallocated_window` / `get_window_addr`) over a *gen-variant* hardware
window block: SUNDA drives a `tpb_nx_local_reg` block of plain `{lo,hi}` 64-bit
SoC-base relocations (8 `MEM_WINDOW` + 2 `TPB_WINDOW` + 1 `SEQUENCER_WINDOW`, no
mask, no match, no replace), while CAYMAN and later drive a renamed
`tpb_xt_local_reg` block of 40 `{control, mask, match, replace}` TCAM windows
that compare *and rewrite* the address. This is the same shape as the
[run-state machine](run-state.md)'s SUNDA-vs-CAYMAN CSR-aperture shift: the
firmware interface is frozen; the register block underneath changes.

Everything below is **byte-pinned to a shipped artifact this session**. The
anchor image is the carved `CAYMAN_NX_POOL_DEBUG` device firmware extracted from
`libnrtucode.a` (member `img_CAYMAN_NX_POOL_DEBUG_IRAM_contents.c.o` for code,
`…_DRAM_contents.c.o` for strings). The carve is byte-identical to the sibling
SEQ pages:

| Image | VA base | Carve method | Size | sha256 |
| ----- | ------- | ------------ | ---- | ------ |
| IRAM `.rodata` | `0x0` | `objcopy -O binary --only-section=.rodata` | `0x1c820` (116768 B) | `8e4412b9…ed70a` |
| DRAM `.rodata` | `0x80000` | same | `0x6f20` (28448 B) | `7bdf6ed7…16ecd` |

Disassembly uses the native `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`,
Vision-Q7 / Cairo µarch, `HAVE_VISION=1`/`VISION_TYPE=7`) shipped inside the
gpsimd-tools package — **not** scalar-LX. CSR offsets, field names, bit
positions, and array dimensions are read from the shipped `tpb_xt_local_reg.json`
(CAYMAN family) and `tpb_nx_local_reg.json` (SUNDA) register descriptions. The
DEBUG build keeps the `'S:'`/`'R:'` format strings; the PERF build strips them
and re-lays-out code, so PERF offsets do *not* map 1:1 onto the addresses here.
Where the prompt-level report disagrees with the disassembly, **the binary
wins**, and an in-place **CORRECTION** says so (see §5, the memw fence).

Confidence tags follow [the Confidence & Walls Model](../../reference/confidence-model.md):
`OBSERVED` = a byte/string/JSON-field read from a shipped image this session;
`INFERRED` = reasoned over OBSERVED facts; `CARRIED` = consolidated from a cited
cross-page anchor; crossed with `HIGH`/`MED`/`LOW`. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap),
**CORRECTION** (overturns a naive reading), **NOTE**.

---

## 1. Where the manager lives — the string anchor set

The manager is a **device-resident** Xtensa construct linked into every NX
sequencer engine image — not the host, and not the customop Q7-compute library.
Its functions were located by their DRAM-string loads (the `const16` DRAM-string
idiom, §3). The DRAM `.rodata` carries a tight cluster of file names and format
strings; every offset below was re-read this session directly from the carved
`CAYMAN_NX_POOL_DEBUG_DRAM` bytes (`DRAM_VA = off + 0x80000`):

| DRAM off | string | role |
| -------- | ------ | ---- |
| `0x000f6f` | `soc_window_manager.hpp` | **the manager** (owns the table; program/update/push/get) |
| `0x000f86` | `translate_cayman+.hpp` | the SoC→XT engine/die decoder (CAYMAN+) |
| `0x000f9c` | `S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u` | translate's engine-classify log |
| `0x000ff0` | `xt_window_view` | a single window record view |
| `0x000fff` | `xt_window.hpp` | the per-window record file |
| `0x00100d` | `R: program_window: num=%d, vld=%d, xt_addr=0x%llx, soc_addr=0x%llx, u_mask=0x%llx, l_mask=0x%llx` | the **abstract** program_window log |
| `0x00106e` | `R: program_window: num=%d, mask=0x%llx, match=0x%llx, replace=0x%llx` | the **HW-register** program_window log |
| `0x0010b3` | `push_unallocated_window` | the window allocator / free-push |
| `0x001222` | `get_window_addr` | read a window's resolved XT addr |
| `0x001232` | `S: WIN: @%llx -> %x\n` | device per-window resolve log |
| `0x001247` | `R: update_window: num=%d, xt_addr=0x%llx, soc_addr=0x%llx, u_mask=0x%llx, l_mask=0x%llx` | update_window (re-point) log |
| `0x00129f` | `soc2xt_addr` | `xt_window.hpp` soc→xt accessor |

`[ALL HIGH/OBSERVED — read directly from the carved DEBUG DRAM bytes this
session.]`

The two `program_window` strings are the most telling artifact in the file. One
logs the **abstract** view `(num, vld, xt_addr, soc_addr, u_mask, l_mask)` — the
software's idea of a window — and the other logs the **hardware-register** view
`(num, mask, match, replace)` — the literal CAYMAN window CSR fields. The
existence of both proves the manager bridges an abstract `{xt, soc, mask}`
descriptor down to the `{mask, match, replace}` TCAM hardware (§5).

> **NOTE (file split).** The class is split across three source files, all
> OBSERVED as DRAM strings: `soc_window_manager.hpp` (the manager that owns the
> table and the allocator), `xt_window.hpp` (one window *record*, the
> `xt_window_view`, with the `soc2xt_addr` accessor), and — on CAYMAN+ —
> `translate_cayman+.hpp` (the engine/die address decoder). SUNDA names a
> `soc_window.hpp` instead and has no `translate_cayman+.hpp` (§7). The
> per-file responsibility split is `MED/INFERRED` from the string grouping and
> the log fields each file emits; the *names* are `HIGH/OBSERVED`.

---

## 2. The hardware window block — what the manager programs

The manager is the software driver over the per-engine TPB **XT-local register
block's** WINDOW bundle. Two structurally distinct hardware models, split by
generation — the central finding of this page.

### 2a. CAYMAN+ — the `{control, mask, match, replace}` TCAM model

Register file `tpb_xt_local_reg.json` (CAYMAN/MARIANA/MARIANA_PLUS/MAVERICK).
The block is APB, `AddrWidth 16` (a `0x10000` aperture), and its bundle-array
table — read directly from the JSON this session — is:

| Bundle | base (`AddressOffset`) | stride (`BundleSizeInBytes`) | count (`ArraySize`) |
| ------ | ---------------------- | ---------------------------- | ------------------- |
| `nx` | `0x0000` | `0x1000` | 1 |
| `general` | `0x1000` | `0x20` | 60 |
| **`window`** | **`0x2000`** | **`0x1C`** | **40** |
| `q7` | `0x3000` | `0x1000` | 1 |
| `hw_decode` | `0x4000` | `0x1000` | 1 |

So `window[i].<reg> = 0x2000 + i*0x1C + reg_off`. The per-window register
layout — field names, bit positions, and the reset-value description — verbatim
from the JSON:

| rel off | register | fields (bit position) |
| ------- | -------- | --------------------- |
| `+0x00` | `control` | `window_valid[0]`, `nx_dedicated[1]`, `q7_dedicated[2]`, `single_q7_enable[3]`, `single_q7_select[6:4]`, `reserved[31:7]` |
| `+0x04` | `mask_lo` | `value[31:20]` = `mask_value[31:20]` |
| `+0x08` | `mask_hi` | `value[7:0]` = `mask_value[39:32]` |
| `+0x0C` | `match_lo` | `value[31:20]` = `match_value[31:20]` |
| `+0x10` | `match_hi` | `value[7:0]` = `match_value[39:32]` |
| `+0x14` | `replace_lo` | `value[31:20]` = `replace_value[31:20]` |
| `+0x18` | `replace_hi` | `value[25:0]` = `replace_value[63:32]` |

`[ALL HIGH/OBSERVED — the bundle table and every per-window bitfield read from
`tpb_xt_local_reg.json` this session.]`

**Address widths, read precisely from the bit ranges:** `mask` and `match` are
each a **20-bit field spanning address bits `[39:20]`** (`_lo` supplies `[31:20]`,
`_hi` supplies `[39:32]`) — so the compare granule is the low **20 bits = 1 MiB**
and the comparable address range is **40 bits**. `replace` spans bits `[63:20]`
(`replace_lo`→`[31:20]`, `replace_hi`→`[63:32]`) — a **44-bit** stored value
addressing the full 64-bit SoC space at 1 MiB granularity.

> **GOTCHA (`replace` is not 40-bit).** It is tempting to treat all three of
> mask/match/replace as the same width. They are not: `replace_hi` carries
> `value[25:0]` (→ address bits `[63:32]`), versus 8 bits in `mask_hi`/`match_hi`.
> The window matches on a 40-bit address but rewrites to a 64-bit SoC target. A
> reimplementation that truncates `replace` to 40 bits silently mis-targets every
> high-memory window.

**Semantics** `[bit layout HIGH/OBSERVED; the match/rewrite rule MED/INFERRED
from the field names + the two log strings]`: a request address `A` matches
window `i` iff `window_valid` and `(A & mask) == (match & mask)` over the
comparable high bits (a region/TCAM compare; `mask` bit 1 = compare,
0 = don't-care at 1 MiB granularity), qualified by the requester gate
(`nx_dedicated` / `q7_dedicated` / `single_q7_*`). On a hit the address is
**rewritten** by substituting the matched high bits with `replace`. This is
exactly what the `program_window: num, mask, match, replace` log records
field-for-field.

The `control` register's own description (from the JSON) states the
reconfiguration contract: *"When changing the window state, this bit should
first be cleared, and only set after the full window state is valid."* The
firmware honours this byte-for-byte (§5).

### 2b. SUNDA — the older `{lo,hi}` base-relocation model

Register file `tpb_nx_local_reg.json` (SUNDA only). Note the bundle is named
**NX-local**, not XT-local — the gen rename. Everything sits in one `0x10000`
bundle; the window registers, read from the JSON this session:

| register | offset | field | windows |
| -------- | ------ | ----- | ------- |
| `sequencer_window_{lo,hi}` | `0x100`/`0x104` | `addr[31:0]` | 1 (SEQUENCER) |
| `mem_window0..7_{lo,hi}` | `0x200`..`0x23c` | `addr[31:0]` | 8 (MEM) |
| `tpb_window0..1_{lo,hi}` | `0x300`..`0x30c` | `addr[31:0]` | 2 (TPB) |

Each window is a `{lo,hi}` pair of plain 32-bit `addr` fields — a **64-bit SoC
base, with no mask, no match, and no replace**. The NX side is a fixed slice of
the low-NX space; a request inside a window's NX slice resolves to
`window_soc_base + (ptr & ~granule_mask)` — a pure base-add, **no TCAM compare**.
`[HIGH/OBSERVED — every register name/offset/field from `tpb_nx_local_reg.json`.]`

### 2c. The headline: the per-gen shift

| | SUNDA | CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK |
| --- | --- | --- |
| HW block | `tpb_nx_local_reg` | `tpb_xt_local_reg` (NX→XT rename) |
| window count | 8 MEM + 2 TPB + 1 SEQ | **40** (`ArraySize`) |
| base / stride | MEM @ `0x200`, `{lo,hi}` 8 B | `0x2000` / `0x1C` |
| per-window fields | `{lo,hi}` 64-bit SoC base | `{control, mask, match, replace}` |
| match model | none — fixed NX-slice base-add | TCAM `mask`/`match` + requester gate |
| action | base relocation | address **rewrite** (`replace`) |
| firmware logs | *(none — nothing to log for a base write)* | `program_window(mask,match,replace)`, `update_window`, `S: WIN:` |

The `40 / 0x1C / 0x2000` triple is **byte-identical across CAYMAN, MARIANA,
MARIANA_PLUS, and MAVERICK** — confirmed this session by reading the `window`
bundle from all four `tpb_xt_local_reg.json` files:

```
cayman:       base=0x02000 stride=0x0001C count=40
mariana:      base=0x02000 stride=0x0001C count=40
mariana_plus: base=0x02000 stride=0x0001C count=40
maverick:     base=0x02000 stride=0x0001C count=40
```

> **QUIRK (frozen interface, swapped hardware).** The software class
> `soc_window_manager` and its method set are present in *every* gen's NX
> firmware (§7), but the hardware block it drives is structurally different on
> SUNDA. The interface is the invariant; the register block is the variant. This
> mirrors the [run-state machine](run-state.md)'s gen shift exactly — the
> firmware abstracts over a register-layout change rather than re-deriving it.

> **NOTE (v5 / MAVERICK).** MAVERICK's `tpb_xt_local_reg.json` is header-OBSERVED
> (the `40 / 0x1C / 0x2000` window bundle matches the CAYMAN family), but no
> MAVERICK *firmware* image was carved this session, so the MAVERICK
> *control-flow* interiors are `INFERRED` (carried from the CAYMAN decode). The
> bit-grounded firmware decode below is the CAYMAN (v3) image.

**SoC placement** `[HIGH/OBSERVED]`: the WINDOW bundle is at
`<engine>_LOCAL_REG_BASE + 0x2000` per TPB engine — e.g.
`TPB_0_POOL_LOCAL_REG_BASE 0x2803060000` → windows at `0x2803062000`; ACT/PE/SP/DVE
each have their own base, and `TPB_1_*` (die 1) is at `0x38…`. Each of the 5 TPB
engines × 2 dies has its **own** 40-window block; the SEQ on that engine programs
its block. Device-side, the SEQ dereferences the **device-local** view at
`0x04000000 + nx-offset` (CARRIED from [run-state](run-state.md)), i.e. windows
at `0x04002000 + i*0x1C` (§5).

---

## 3. The device functions — byte-pinned

The window-manager functions were located in the `CAYMAN_NX_POOL_DEBUG` IRAM by
their DRAM-string loads. The Vision-Q7 firmware addresses DRAM constants with the
**`const16` pair idiom**: `movi a3, 0x400` builds the high half `0x04000000`-class
and `const16 a3, OFF` overlays the low half; for DRAM the high half resolves to
the `0x80000` base and the low half to the string offset. Because this idiom does
*not* go through the flat-image l32r literal pool, the function entry points and
their string call-sites are byte-exact (resolving the flat-image ambiguity of the
loader family). Every entry below was confirmed this session to land on a clean
`entry aN, M` window-frame prologue:

| IRAM VA | function | first insn | role |
| ------- | -------- | ---------- | ---- |
| `0x03dc4` | `translate_soc_to_xt_address` | `entry a1, 0x1b0` | the central lookup (§4) |
| `0x046e8` | `program_window` | `entry a1, 112` | the HW-register installer (§5) |
| `0x04994` | `push_unallocated_window` | `entry a1, 48` | the free-list allocator (§6) |
| `0x04f28` | `get_window_addr` | `entry a1, 80` | resolve a window's XT addr (§7) |
| `0x05048` | `soc2xt_addr` | `entry a1, 48` | `xt_window.hpp` record accessor (§5d) |
| `0x05084` | `update_window` | `entry a1, 80` | re-point a window's target (§5c) |
| `0x0a304` | `assert_fail` | `entry a1, 48` | the FATAL panic sink ([error-handler](error-handler.md)) |

`[ALL HIGH/OBSERVED — the entry VAs and prologues re-disassembled this session
with the native ncore2gp objdump.]`

**Assert `__LINE__` immediates** (the `movi a12, N` feeding `call8 0xa304`) — all
five found this session at the addresses shown:

| addr | `movi a12, N` | source line | meaning |
| ---- | ------------- | ----------- | ------- |
| `0x3ead` | `223` | `translate_cayman+.hpp:223` | engine/die decode default (unknown engine_base) |
| `0x3ff3` | `0x101` (257) | `translate_cayman+.hpp:257` | a second engine-classify default |
| `0x41bc` | `0x16d` (365) | `translate_cayman+.hpp:365` | cayman-id / die default |
| `0x4ff5` | `0x283` (643) | `translate_cayman+.hpp:643` | `get_window_addr`: **no window matched** (the window-miss, §8) |
| `0x49b6` | `0x176` (374) | `soc_window_manager.hpp:374` | `push_unallocated_window`: `free_count < 15` invariant (§6) |

`[ALL HIGH/OBSERVED — the immediates and their `call8 0xa304` targets
re-disassembled this session.]`

---

## 4. `translate_soc_to_xt_address` — the lookup

Entry `0x03dc4`. The function decodes *this engine's* SoC identity from MMIO,
classifies the requesting address, then composes and installs this engine's fixed
window set. Annotated C pseudocode, every named address OBSERVED:

```c
// translate_soc_to_xt_address(self, ctx)   self=a2, IRAM 0x03dc4
// ---- STEP 1: read this engine's SoC identity from the general-lr bundle ----
// 0x3de0: movi a3,0x400 ; const16 a3,0x1040 ; l32i.n a10,[a3]
// 0x3de8: movi a3,0x400 ; const16 a3,0x1020 ; l32i.n a11,[a3]
u32 engine_base = *(volatile u32*)0x04001040;   // general[2]  (lr)
u32 tpb_base    = *(volatile u32*)0x04001020;   // general[1]  (lr)
// log "S: engine_base_addr=%llx tpb_base_addr=%llx -> is_tpb=%u is_die_0=%u engine_idx=%u"

// ---- STEP 2: classify the engine/die (translate_cayman+.hpp) ----
// jump-table dispatch over a small key (bltui rc<8 ; addx4 into DRAM table ; jx),
// then a chain of 64-bit equality tests against the 8 TPB-engine SoC-base constants.
int engine_idx; bool is_tpb, is_die_0;
switch (classify(engine_base, tpb_base)) {       // xor/or/beqz chain over 8 bases
  /* matched -> set the three fields */
  default: assert_fail(..., "translate_cayman+.hpp", 223 /*or 257/365*/);  // PANIC
}
ctx->engine_idx = engine_idx;   // s32i.n -> ctx+0x10
ctx->is_tpb     = is_tpb;        // s8i    -> ctx+0x14
ctx->is_die_0   = is_die_0;      // s8i    -> ctx+0x15

// ---- publish the packed result to general[17] ----
// 0x4009: is_tpb<<16 | is_die_0<<8 | engine_idx ; 0x4022 const16 a4,0x1220 ; s32i.n
*(volatile u32*)0x04001220 = (is_tpb << 16) | (is_die_0 << 8) | engine_idx;

// ---- STEP 3: compose this engine's fixed window descriptors ----
// NX-side bases are built with the slli granule idioms (CARRIED, ADDR/ABI pages):
//   movi.n a3,1 ; slli a3,a3,24      -> 0x01000000  (16 MiB granule base)
//   movi.n a14,-31 ; slli a14,a14,26 -> 0x84000000  (hbm_scratch 64 MiB NX base)
// each descriptor staged at ctx+0x100 (addmi a3,a1,0x100), passed to a build helper.

// ---- STEP 4: the install loop (eager, up to 8 windows) ----
// 0x435b: i=0 ; 0x4364: bgei a2,8 -> exit ; ... call8 helpers ; 0x4389: i++ ; loop
for (int i = 0; i < 8; i++) {
    stage_descriptor(i);
    program_window(/*desc*/, /*flag*/);   // or update_window (§5/§5c)
}
```

`[engine-decode reads, the field stores at ctx+0x10/+0x14/+0x15, the packed
result write to 0x04001220, and the i<8 loop are HIGH/OBSERVED; the per-constant
engine→label mapping is MED/INFERRED from the address-map TPB bases (§2c).]`

> **NOTE (this is not a 16-entry tag scan).** The device fast path is *not* a
> linear scan over a pre-filled 16-entry tag table. translate **classifies** the
> engine identity from two MMIO CSRs, then **installs** this engine's fixed region
> windows. The actual per-access address match is done in *hardware* by the
> window block's `mask`/`match` compare (§2a). The 16-entry table is a software
> **shadow** kept for read-back and reallocation (§7), not the match path
> `[MED/INFERRED — the install path is OBSERVED; "16-entry table = read-back
> shadow, not match path" is the inference]`.

> **QUIRK (eager install, not lazy-on-miss).** translate programs the window set
> for this engine's regions up front in one pass of up to 8 windows. This is the
> opposite of the customop Q7-compute `neuron_translate`, which is a lazy
> 3-slot round-robin that programs a window only when a miss occurs. The SEQ
> control-plane sequencer pre-arms; the data-plane compute path evicts on demand.

---

## 5. `program_window` — the HW-register install

Entry `0x046e8`. Args: `a2` = the window descriptor (`num` at `+0x28`, the six
field words, gate bits at `+0x2c`, valid bit at `+0x30`), `a3` = a bool flag.
This is the keystone — the exact HW-register address arithmetic and the field
write order, re-disassembled byte-exact this session.

### 5a. The window-register address

```
473a: l32i.n a3, [a2,0x28]    ; a3 = i = descriptor's window num
473c: subx8  a3, a3, a3       ; a3 = (i<<3) - i = 7*i        (Xtensa SUBX8 = (rs<<3)-rt)
473f: slli   a3, a3, 2        ; a3 = 28*i = i * 0x1C  (== the WINDOW stride!)
4742: addmi  a4, a3, 0x2000   ; a4 = 0x2000 + i*0x1C         (window block rel offset)
4745: movi.n a3, 1
4747: slli   a3, a3, 26       ; a3 = 0x04000000               (CSR aperture base)
474a: add.n  a4, a4, a3       ; a4 = 0x04002000 + i*0x1C      (ABS window-reg addr)
```

The window block sits at SEQ-NX-local **`0x04002000 + i*0x1C`** — the device-local
CSR aperture base `0x04000000` (CARRIED from [run-state](run-state.md)) + the
`window` bundle base `0x2000` + `i × stride 0x1C`. This is byte-identical to the
`tpb_xt_local_reg.json` layout (§2a). The `subx8`/`slli` pair computing `i*0x1C`
reconstructs the stride from the index alone.

### 5b. The field write order

Each field's CSR address is rebuilt per-store with its own idiom; the firmware
writes `control` to **clear `window_valid` first**, stages all six field words,
then writes `control` **last to set valid + gates**:

| step | reg (rel off) | abs CSR | address idiom OBSERVED |
| ---- | ------------- | ------- | ---------------------- |
| 1 | `control` (`+0x00`) = **0** | `0x04002000 + 28i` | `movi.n a5,0 ; s32i.n a5,[a4,0]` — clear valid FIRST |
| 2 | `mask_lo` (`+0x04`) | `0x2004` | `movi.n a7,4 ; addmi a7,a7,0x2000` |
| 3 | `mask_hi` (`+0x08`) | `0x2008` | `movi a7,0x401 ; slli a7,a7,3` |
| 4 | `match_lo` (`+0x0c`) | `0x200c` | `movi a7,0x494 ; subx8 a7,a7,a7` (`0x494*7`) |
| 5 | `match_hi` (`+0x10`) | `0x2010` | `movi a7,0x201 ; slli a7,a7,4` |
| 6 | `replace_lo` (`+0x14`) | `0x2014` | `movi.n a6,20 ; addmi a6,a6,0x2000` |
| 7 | `replace_hi` (`+0x18`) | `0x2018` | `movi a6,0x403 ; slli a6,a6,3` |
| 8 | `control` (`+0x00`) again | `0x04002000 + 28i` | `(desc+0x2c) \| (valid bit from desc+0x30 bit0)`, written LAST |

`[ALL HIGH/OBSERVED — the per-field address idioms, the absolute offsets, and the
clear-first/set-last ordering re-disassembled this session.]`

This is exactly the `control`-register contract from the JSON: *clear
`window_valid`, write the full window state, set `window_valid` only when the
window is valid.* The function logs **both** view strings before installing — the
abstract `(num, vld, xt_addr, soc_addr, u_mask, l_mask)` and the HW-register
`(num, mask, match, replace)`.

> **CORRECTION (one conditional `memw`, not two bracketing fences).** The
> prompt-level report stated *"two `memw` fences bracket the control writes."* A
> raw byte scan (`c0 20 00`) over `0x46e8`–`0x48c6` and a full re-disassembly this
> session find exactly **one** `memw`, at `0x48be`, and it is **conditional**:
> `0x48b5: l8ui a2,[a1,76] ; 0x48b8: bbci a2,0,0x48c4` skips it on a frame flag,
> and it sits *after* the final `control` write — it does not bracket the
> clear/stage/set sequence. There is no opening fence before the clear and none
> between the control-clear and control-set. A reimplementation should emit a
> single trailing (optionally-gated) `memw` after the install, not a paired
> fence. `[HIGH/OBSERVED — single memw at 0x48be confirmed by byte scan +
> disasm.]`

> **GOTCHA (`l32i.n`/`s32i.n` narrow forms).** The objdump emits the narrow
> `l32i.n`/`s32i.n` encodings for these CSR accesses (4-byte-aligned within the
> `0x10000` aperture), not the wide `l32i`/`s32i`. Semantically identical, but
> match the mnemonic when transcribing bytes.

### 5c. `update_window` — the lightweight re-point

Entry `0x05084`. Writes **only** `replace_lo` (`0x04002014 + 28i`) and
`replace_hi` (`0x04002018 + 28i`), leaving `mask`/`match` (the match region) and
`control` untouched. So `update_window` **re-points** an existing window's SoC
target without changing which addresses it matches — the cheap "this window now
maps to a different SoC base" path versus `program_window`'s full reinstall. It
drives a `memw`'d reg-write helper twice. `[HIGH/OBSERVED — the two replace-only
stores at `0x2014`/`0x2018`.]`

### 5d. `soc2xt_addr` — the per-record accessor

Entry `0x05048` (`xt_window.hpp`). Loads the record's `soc` value
(`l32i a3,[record,0]`), resolves it, stores the result into `record+4`, returns a
success bool. The per-window `{soc → xt}` field accessor the manager uses when
composing or reading one `xt_window` record. `[HIGH/OBSERVED.]`

---

## 6. `push_unallocated_window` — the allocator

Entry `0x04994`. Args: `a2` = the `soc_window_manager` (`self`), `a3` = the
`window_id` to free. The free-list, re-disassembled byte-exact this session:

```
4994: entry  a1, 48
4997: s32i.n a2, [a1,12]      ; spill self
4999: s32i.n a3, [a1,8]       ; spill window_id
499b: l32i.n a2, [a1,12]      ; a2 = self
49a0: l32i.n a3, [a2,8]       ; a3 = self->free_count        (self+0x08)
49a2: movi.n a4, 15
49a4: bltu   a3, a4, 0x49bc   ; assert free_count < 15  -> else PANIC
                              ;   (49aa: const16 push_unallocated_window @0x10b3;
                              ;    49b0: const16 soc_window_manager.hpp @0xf6f;
                              ;    49b6: movi a12,374 ; call8 0xa304)
49c3: addi.n a4, a2, 12       ; a4 = &self->free_list[0]      (self+0x0c)
49c5: l32i.n a5, [a2,8]       ; a5 = free_count
49c7: addx4  a4, a5, a4       ; a4 = &free_list[free_count]
49ca: s32i.n a3, [a4,0]       ; free_list[free_count] = window_id
49cc: l32i.n a3, [a2,8]
49ce: addi.n a3, a3, 1
49d0: s32i.n a3, [a2,8]       ; free_count++
```

The proven `soc_window_manager` struct fields:

| offset | field | type | role |
| ------ | ----- | ---- | ---- |
| `+0x08` | `free_count` | `u32` | number of free window ids on the list |
| `+0x0c` | `free_list[]` | `u32[]` | the LIFO stack of unallocated window indices |
| `+0x3a0`/`+0x3a4` | scratch/ctx ptrs | — | used by translate |

And the per-window **descriptor** record passed to `program_window`:
`desc+0x28` = `num` (window slot 0..39), `desc+0x2c` = control gate bits,
`desc+0x30` bit0 = valid, plus the six field words → `mask_lo/hi`, `match_lo/hi`,
`replace_lo/hi`.

`push_unallocated_window` is a **free-list push**: it returns a window index to
the pool of unallocated windows (a LIFO stack at `self+0x0c`, length
`self+0x08`). The `free_count < 15` invariant bounds the list at ≤ 15 stored — a
**16-slot stack** — i.e. the software manages a **16-window budget** out of the 40
hardware windows. This `16` agrees with the host `xt_addrs[16]` shadow (§7). An
ALLOCATE (the inverse) pops the top; a window is handed out by popping a free
index, programmed via `program_window`, and reclaimed by pushing it back here.

`[HIGH/OBSERVED — the `free_count@+0x08`/`free_list@+0x0c` layout, the `addx4`
stack indexing, the `count<15` assert, and the `++` are byte-exact. The
"16-window SW budget of 40 HW windows" reading is HIGH (16 = the host shadow +
the `count<15` bound, both OBSERVED); the LIFO allocate-pop inverse is
MED/INFERRED — push is OBSERVED, the matching pop was not separately located in
this carve.]`

> **NOTE (reclaim on unload).** The [external-lib loader (device side)](../pool/external-lib-loader.md)'s
> unload path is the caller that **reclaims** a library's window by pushing its
> index back onto `self->free_list` (which drops `window_valid` via the
> `program_window` clear path). The resident free-list is what lets a window be
> reclaimed without the host re-sending an address. `[push_unallocated is the
> reclaim primitive HIGH/OBSERVED; the unload→push wiring is HIGH-named / MED on
> the exact caller — not re-traced in this carve.]`

---

## 7. `get_window_addr` and the 16-entry shadow

Entry `0x04f28`. `get_window_addr` walks the engine's window descriptors and
resolves each to its XT address (calling `soc2xt_addr` per record), logging the
device-side `S: WIN: @%llx -> %x` per window. The loop bound observed is `i < 8`
(`0x4fa4: saltu a3,a3,a4` with `a4 = 8`) — it resolves this engine's up-to-8
fixed windows. If no window matches the requested region the function panics
(`movi a12,643 ; call8 0xa304`, `translate_cayman+.hpp:643`, §8). `[HIGH/OBSERVED.]`

**The 16-entry table — device vs host split** `[HIGH/OBSERVED]`:

- **DEVICE** (SEQ NX firmware): logs the singular `S: WIN: @%llx -> %x` (one per
  window in `get_window_addr`). The device DRAM carries only this singular form —
  no `xt_addrs[16]`, no `WIN[%u]` (verified by string search of the carved DRAM
  this session).
- **HOST** (`libnrtucode_internal.so` runtime): keeps a **16-entry software
  shadow** dumped as
  `P%i: Q7:   xt_addrs[16] = [0x%08x%08x, … ]` — re-counted this session as
  **exactly 16** `0x%08x%08x` pairs (host `.rodata` offset `0x26c55a`). It also
  logs `R: program_window` / `R: update_window` / `P%i: WIN[%u]: @%llx -> %x`.
  The `R:` prefix = **R**untime (host); `P%i:` = per-pool-core.

So the 16-entry `xt_addrs` table is the *host's* mirror of the device's
allocated windows — one 64-bit XT addr per software-managed window, 16 of them.

> **GOTCHA (16 SW vs 40 HW).** The hardware exposes **40** windows
> (`tpb_xt_local_reg.json`); the software window manager — the device free-list
> (`count<15`/16-slot) plus the host `xt_addrs[16]` shadow — manages a **16-window
> subset**. The remaining 24 hardware windows are statically pinned by the
> boot/RTL config or reserved for non-SW paths `[16 and 40 both OBSERVED; the
> "24 reserved/static" split MED/INFERRED]`. A reimplementation must not assume
> all 40 are software-allocatable.

---

## 8. Window-miss handling — fail-fast panic

Two miss surfaces, by layer:

**(a) Hardware-level miss** — a request address matching no valid window is
handled *in hardware* by the `tpb_xt_local_reg` window block's `mask`/`match`
compare. An address that matches no `window_valid` window is not remapped (it
passes through / faults per the block's default); there is **no software trap on
the per-access fast path**. `[HIGH/OBSERVED that the match is HW; the no-match
default (pass vs fault) is not in the JSON schema = MED.]`

**(b) Software-level miss** — when translate / `get_window_addr` cannot classify
or find a window, it is a **hard assert** (`call8 0xa304` → `assert_fail` → the
FATAL spin documented in the [error handler](error-handler.md)):

| condition | site | line |
| --------- | ---- | ---- |
| engine/die decode found an unclassifiable `engine_base` | translate (`0x3ead`/`0x3ff3`/`0x41bc`) | `translate_cayman+.hpp:223`/`257`/`365` |
| **no window matched the requested region** | `get_window_addr` (`0x4ff5`) | `translate_cayman+.hpp:643` |
| free-list overflow (`free_count >= 15` on push) | `push_unallocated_window` (`0x49b6`) | `soc_window_manager.hpp:374` |

So the SEQ window manager is **fail-fast**: an unclassifiable SoC address, an
unmatched region, or a free-list overflow all hit `assert_fail` at `0x0a304`.
There is **no silent default-window fallback** at the software level.
`[HIGH/OBSERVED — the five assert sites + line numbers + the `0xa304` target
re-disassembled this session; "no soft fallback" is HIGH from the absence of any
non-panic miss branch.]`

> **QUIRK (fail-fast SEQ vs fail-silent customop).** The customop Q7-compute
> `neuron_translate` (the data-plane translator) has no bounds/null guard and
> *silently* programs an out-of-range window on a miss (a round-robin evict). The
> SEQ `soc_window_manager` (the control-plane sequencer) is the opposite: it
> **panics** on an unclassifiable or unmatched address. The control plane is
> fail-fast; the data plane is fail-silent-fast. `[both behaviors HIGH/OBSERVED;
> the design-intent contrast MED/INFERRED.]`

---

## 9. Relation to the HW remapper (`amzn_remapper`)

The SEQ `soc_window_manager` and the SoC-wide `amzn_remapper` are **distinct,
non-overlapping** address-translation layers:

| | `soc_window_manager` (this page) | `amzn_remapper` |
| --- | --- | --- |
| what it is | SW driver over `tpb_xt_local_reg` WINDOW block (40 windows) | HW per-master AXI-egress address-remap CAM (sprot FIS leaf) |
| where the regs live | `tpb_xt_local_reg` @ `<engine>_LOCAL_REG_BASE+0x2000` (device-local `0x04002000`) | `amzn_remapper` @ sprot FIS (SoC `0x…05000`, APB) |
| granularity | per-NX-engine (each TPB engine's own 40-window block) | per-AXI-master (SDMA / D2D / PCIe / TPB egress edge) |
| match | `mask`/`match` (40-bit, page-aligned) + requester gate (nx/q7/single_q7) | `cmp_addr`/`cmp_addr_mask` (57-bit) + optional 10-bit AXI-ID |
| action | `replace` (rewrite to a SoC target) | pass/deny per-dir + remap low bits + interleave swap |
| who programs it | the SEQ NX firmware (this page) | the privileged (AMZN) firmware |
| on miss | SW: panic; HW: block default | `control.pass_on_miss` (AMZN deny / USER pass) |

**Verdict:** the SW window manager is **not** programming the HW remap CAM. It
programs the per-engine `tpb_xt_local_reg` WINDOW registers — the NX/Q7 *local*
address-translation aperture that turns a Q7-local XT address into a SoC address
for *this engine's* requests. The `amzn_remapper` sits much further downstream,
at every AXI master's fabric-egress edge, doing a SoC-wide address+ID
firewall/remap. This session confirmed the SEQ NX firmware accesses `0x04002000+`
(the window block) and never the `amzn_remapper` sprot CSRs (no `0x…05000` /
remapper access in the NX IRAM). A Q7 request flows:
`NX-local addr → (soc_window_manager window) → SoC addr → (amzn_remapper CAM at
master egress) → final SoC addr`. `[the two register blocks, the access-site
evidence, and the non-overlap are HIGH/OBSERVED; the two-stage flow ordering is
MED/INFERRED from layer placement.]`

> **NOTE (two requester gates, two scopes).** The window `control` gate
> (`nx_dedicated`/`q7_dedicated`/`single_q7_select`) filters by *which on-engine
> requester* (NX core vs a specific Q7) — a **local, per-engine** filter. The
> remapper's 10-bit AXI-ID gate is a **global, fabric-master** filter. Both gate
> translation by requester, but at different scopes (engine-local core-select vs
> fabric-wide master-id). `[both fields OBSERVED; the scope contrast
> MED/INFERRED.]`

---

## 10. Cross-references

- [Run-state machine](run-state.md) — the device-local `0x04000000` CSR
  aperture, the `const16` `__FILE__`-string idiom, and the sibling SUNDA-vs-CAYMAN
  gen shift this page mirrors.
- [Error handler](error-handler.md) — `assert_fail @0xa304`, the FATAL
  halt-spin that every window-miss / free-list-overflow panic in §8 falls into.
- [External-lib loader (device side)](../pool/external-lib-loader.md) — the
  caller that drives `translate_soc_to_xt_address` at library load time and
  `push_unallocated_window` on unload (the load/unload wiring lands there).
- [The SoC ↔ Q7 Translation Windows](../../control/address/soc-q7-translation-windows.md)
  — the address-map view of the same window apertures (the NX/SoC base placement,
  the granule pins). *(Forward link — not yet authored.)*
- [`neuron_translate` Window Family](../../abi/neuron-translate-windows.md)
  — the customop Q7-compute translator this page contrasts against in §4 and §8
  (lazy 3-slot round-robin, fail-silent). *(Forward link — not yet authored.)*
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the
  `OBSERVED` / `INFERRED` / `CARRIED` × `HIGH`/`MED`/`LOW` tags used throughout.

> **NOTE (forward links).** `control/address/soc-q7-translation-windows.md` and
> `abi/neuron-translate-windows.md` are planned (their directories exist but the
> pages are not yet authored). The links are kept against their SUMMARY-pinned slugs and will
> resolve when those pages land.
