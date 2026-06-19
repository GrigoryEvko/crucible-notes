# PROF_CAM / PROF_TABLE Blob Formats

This page is the **authoritative format reference** for the per-engine
`PROF_CAM` / `PROF_TABLE` blobs embedded in `libnrtucode_internal.so`. Every
Part-6 per-gen image page (`cayman-{act,dve,pe,pool}`, `mariana-act`, …) and the
runtime resolver page forward-link here for the byte layout, the
counter-binding mechanism, and the resolution of a long-standing conflation.

The headline result, proven below six independent ways:

> **`PROF_CAM` / `PROF_TABLE` are the per-engine HW *instruction-decode
> profiler* — an opcode-match CAM that arms which decoded opcodes the hardware
> profiler counts, plus a parallel per-opcode profiler-descriptor table. They
> are NOT the activation/transcendental PWL lookup.** The activation PWL is a
> *separate*, ACT-only, DMA-staged **four-table quad**
> (`aws_hal_stpb_act_{cam,profile,control,bucket}_entry_t`) that is not carried
> in `libnrtucode` at all. The 16 B opcode-only `PROF_CAM` record is structurally
> distinct from the 32 B activation CAM keyed on `(opcode, func_id)`.

All facts derive from static analysis of the shipped host library
`libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`) with stock binutils,
plus the in-package interoperability evidence shipped in the same customop-lib
package — the arch-isa header `tpb_activation_entries.h` and the
`tpb_xt_local_reg.json` device CSR block. Confidence tags: **HIGH** = direct
disasm / byte read / header read / CSR read; **MED** = strong inference;
**LOW** = plausible. **OBSERVED** = read directly; **INFERRED** = derived;
**CARRIED** = adopted from a committed sibling page and re-verified here.

---

## 1. Container and carve geometry

| Fact | Value | Tag |
|------|-------|-----|
| Container | `libnrtucode_internal.so`, 10,276,288 B, ELF64 x86-64, **not stripped** | HIGH/OBS |
| sha256 | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` | HIGH/OBS |
| BuildID | `9cbf78c6f59cdb5839f155fdb2113bbe51e585fd` | HIGH/OBS |
| Path | `…/customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/` | HIGH/OBS |
| `.rodata` mapping | section hdr `[9]` VA `0x46b0` == file offset `0x46b0` → **blob VA == file offset** (carves are correct) | HIGH/OBS |

> **GOTCHA.** The identity-map holds for `.text`/`.rodata` only. The getter
> *stubs* live in `.text` and read VA-aware (`.text` carries a `0x2000`
> VA−fileoffset delta), but the blob `lea` targets are `.rodata` offsets, so the
> blob carve at `blob_VA` for `size` is byte-correct. For unrelated `.data`
> structs on this gen, the VMA−fileoffset delta is `0x200000` — irrelevant to
> the `.rodata`-resident PROF blobs but a recurring source of false "wrong
> struct" findings elsewhere.

---

## 2. The ~30 accessors (the getter inventory)

There are **exactly 30** host getter functions, `nm`-verified:

```
$ nm libnrtucode_internal.so | rg -c 'PROF_(CAM|TABLE)_get$'
30
```

These are **15 `(gen, engine)` entries × {CAM, TABLE}**:
CAYMAN / MARIANA / MARIANA_PLUS each × {ACT, DVE, POOL, PE} (12 entries) +
MAVERICK × {DVE, POOL, PE} (3 entries — **no MAVERICK ACT**).

### 2a. The getter stub (4 instructions)

Every getter is a constant trampoline: load the blob pointer, write it through
`*out_ptr`, write the fixed size through `*out_len`, return.

```asm
; CAYMAN_NX_ACT_PROF_CAM_get @0x9b3ba0
9b3ba0: 48 8d 05 f9 ec 94 ff   lea    -0x6b1307(%rip),%rax  ; → 0x3028a0 (the blob, .rodata)
9b3ba7: 48 89 07               mov    %rax,(%rdi)           ; *out_ptr = blob
9b3baa: 48 c7 06 00 04 00 00   movq   $0x400,(%rsi)         ; *out_len = 0x400 (CAM = 1 KiB)
9b3bb1: c3                     ret                          ; (padded with int3)
```

Signature: `void get(void** out_ptr /*rdi*/, size_t* out_len /*rsi*/)`.
**ALL CAM getters write `$0x400` (1 KiB); ALL TABLE getters write `$0x2000`
(8 KiB)** — verified by the immediate in the `movq` (the TABLE stub at
`0x9b3bc0` writes `48 c7 06 00 20 00 00` = `$0x2000`). [HIGH/OBSERVED]

### 2b. The 30 getters → blob VA == file offset → sha256 (re-carved this page)

`nm` symbol → `lea` target (`.rodata` VA == file off) → carve → `sha256sum`.
All 15 CAM + 15 TABLE blobs carved and hashed; **every hash matches RT-18 and
the committed per-engine image pages**.

| GEN | ENG | CAM_get | CAM blob | TABLE_get | TABLE blob | CAM sha | TAB sha |
|-----|-----|---------|----------|-----------|------------|---------|---------|
| CAYMAN | ACT | `9b3ba0` | `3028a0` | `9b3bc0` | `302ca0` | `8fd7e422` | `ce761f81` |
| CAYMAN | DVE | `9b3be0` | `304ca0` | `9b3c00` | `3050a0` | `8fd7e422` | `ce761f81` |
| CAYMAN | POOL | `9b3c60` | `3094a0` | `9b3c80` | `3098a0` | `8fd7e422` | `ce761f81` |
| CAYMAN | PE | `9b3c20` | `3070a0` | `9b3c40` | `3074a0` | `8fd7e422` | `ce761f81` |
| MARIANA | ACT | `9b4820` | `59c480` | `9b4840` | `59c880` | `326bc0dd` | `8786dd11` |
| MARIANA | DVE | `9b4860` | `59e880` | `9b4880` | `59ec80` | `ca588683` | `d72b339f` |
| MARIANA | POOL | `9b48e0` | `5a3080` | `9b4900` | `5a3480` | `0951b326` | `534f2239` |
| MARIANA | PE | `9b48a0` | `5a0c80` | `9b48c0` | `5a1080` | `43475cec` | `d93b723e` |
| MARIANA_PLUS | ACT | `9b54a0` | `868300` | `9b54c0` | `868700` | `326bc0dd` | `8786dd11` |
| MARIANA_PLUS | DVE | `9b54e0` | `86a700` | `9b5500` | `86ab00` | `ca588683` | `d72b339f` |
| MARIANA_PLUS | POOL | `9b5560` | `86ef00` | `9b5580` | `86f300` | `0951b326` | `534f2239` |
| MARIANA_PLUS | PE | `9b5520` | `86cb00` | `9b5540` | `86cf00` | `43475cec` | `d93b723e` |
| MAVERICK | DVE | `9b5ca0` | `9a42a0` | `9b5cc0` | `9a46a0` | `dbff2b84` | `f349e417` |
| MAVERICK | POOL | `9b5d20` | `9a8aa0` | `9b5d40` | `9a8ea0` | `0951b326` | `534f2239` |
| MAVERICK | PE | `9b5ce0` | `9a66a0` | `9b5d00` | `9a6aa0` | `85d857a7` | `e94d413a` |

> **NOTE — CAYMAN is engine-generic.** Every CAYMAN row carves
> CAM `8fd7e422` / TABLE `ce761f81` — **one shared blob** across all four
> engines (the same `.rodata` content reached via four distinct getters). From
> MARIANA on, the profiler is per-`(gen, engine)`. MAVERICK has no ACT row. See
> §8 for the full cross-gen sha matrix.

### 2c. The resolver `nrtucode_get_hwdecode_table` @`0x9b2cd0` (0x44 B)

The host driver reaches the blobs by index, not by symbol:

```asm
9b2cd0: b8 01 00 00 00   mov  $0x1,%eax        ; default ret = 1 (bad idx)
9b2cd5: 83 ff 25         cmp  $0x25,%edi        ; idx > 0x25 (37)?
9b2cd8: 77 3a            ja   9b2d14            ;   → ret 1
9b2cda: 89 f8            mov  %edi,%eax
9b2cdc: 48 c1 e0 04      shl  $0x4,%rax          ; rax = idx * 16
9b2ce0: 48 8d 3d ...     lea  0x63a9(%rip),%rdi  ; → 0x9b9090 hwdecode_table_list
9b2ce7: 48 01 c7         add  %rax,%rdi          ; rdi = &list[idx]
9b2cea: b8 02 00 00 00   mov  $0x2,%eax          ; default ret = 2
9b2cef: 85 f6            test %esi,%esi          ; kind == 0 (CAM)?
9b2cf1: 74 09            je   9b2cfc             ;   → use list[idx].CAM_get (+0)
9b2cf3: 83 fe 01         cmp  $0x1,%esi          ; kind == 1 (TABLE)?
9b2cf6: 75 1c            jne  9b2d14             ;   else → ret 2 (bad kind)
9b2cf8: 48 83 c7 08      add  $0x8,%rdi          ; rdi = &list[idx].TABLE_get (+8)
9b2cfc: 4c 8b 07         mov  (%rdi),%r8         ; r8 = getter ptr
9b2cff: 4d 85 c0         test %r8,%r8            ; NULL getter?
9b2d02: 74 10            je   9b2d14             ;   → ret 2
9b2d04: 50               push %rax
9b2d05: 48 89 d7         mov  %rdx,%rdi          ; out_ptr  (3rd arg)
9b2d08: 48 89 ce         mov  %rcx,%rsi          ; out_len  (4th arg)
9b2d0b: 41 ff d0         call *%r8               ; *out_ptr=blob; *out_len=size
9b2d0e: 31 c0            xor  %eax,%eax          ; ret 0 (ok)
...
9b2d14: c3               ret
```

`nrtucode_get_hwdecode_table(idx, kind, out_ptr, out_len)` returns
**0** ok | **1** `idx > 37` | **2** (bad `kind` OR NULL getter). No env gate,
no flavor switch, no deprecation guard. `hwdecode_table_list` @`0x9b9090` is a
`38 × 16 B` array of `{CAM_get, TABLE_get}` pairs; **30** of the 76 slots are
populated, verified by reloc count:

```
$ readelf -rW … | rg R_X86_64_RELATIVE  # 30 in range [0x9b9090, +38*16)
relocs in list range: 30
```

> **NOTE — the front-lib twin.** A second resolver/list exists at `0x30b150` /
> list `0x30f810` that omits the MAVERICK entries → returns `2` for those
> indices. The internal twin (this page's anchors) carries all 30.

### 2d. The `.a` ground truth (the two tables don't share a library)

```
$ ar t libnrtucode.a | rg -c 'hwdecode.*PROF_(CAM|TABLE)'   → 24
$ ar t libnrtucode.a | rg -c 'MAVERICK.*hwdecode'           → 0
$ ar t libnrtucode.a | rg -ci 'stpb|act_profile|bucket|control_table'  → 0
```

`libnrtucode.a` ships **24** `hwdecode_{CAYMAN,MARIANA,MARIANA_PLUS}_NX_{ACT,DVE,PE,POOL}_PROF_{CAM,TABLE}_contents.c.o`
members (3 gens × 4 eng × 2). **Zero** MAVERICK members (the MAVERICK PROF blobs
in the internal twin are linked from an external object) and **zero**
activation/stpb/bucket/control members — **the activation PWL tables are not in
this library at all.** [HIGH/OBSERVED]

---

## 3. `PROF_CAM` — byte-exact format (the opcode-match CAM)

`0x400` = **1 KiB = 64 slots × 16 B**.

```c
// the HW-decode profiler opcode-match CAM record (16 B fixed stride)
struct hwdecode_cam_record {
    uint32_t opcode_id;  // +0x00  instruction opcode to match.
                         //        FULL-WIDTH 32-bit field; admits the 9-bit
                         //        extended opcode 0x1e3 (DVE) — NOT an 8-bit
                         //        opcode + separate func_id.
    uint32_t mask;       // +0x04  match mask. Strictly ∈ {0x00, 0xff, 0x1ff}:
                         //          0xff   exact single-byte opcode match
                         //          0x00   WILDCARD / catch-all (matches every opcode)
                         //          0x1ff  9-bit match (the one 0x1e3 entry)
    uint32_t enable;     // +0x08  1 = armed; strictly ∈ {0,1}. No other value.
    uint32_t reserved;   // +0x0c  strictly 0.
};
```

**Field domains, verified by decoding all 15 blobs × 64 slots = 960 records:**

```
enable  values : [0, 1]
mask    values : [0x0, 0xff, 0x1ff]
reserved values: [0]
```

[HIGH/OBSERVED]

### 3a. Layout invariant — contiguous armed + trailing wildcard

Armed records (`enable == 1`) occupy **contiguous slots `0 .. N-1`**; the
**LAST** armed slot is **always** `{opcode 0, mask 0, enable 1}` — the
**wildcard catch-all** (`mask == 0` matches every opcode → the "all other
opcodes" profile bucket). The remaining `64 - N` slots are zeroed.

Armed counts per `(gen, engine)`, **OBSERVED** (carve-decoded this page):

| | CAYMAN | MARIANA | MARIANA_PLUS | MAVERICK |
|---|---|---|---|---|
| ACT | **47** | **25** | 25 | (no ACT) |
| DVE | 47 | **48** | 48 | **54** |
| PE | 47 | **22** | 22 | **19** |
| POOL | 47 | **1** (wildcard only) | 1 | 1 |

CAYMAN arms 47 on every engine (one shared blob). From MARIANA the count is
per-engine. [HIGH/OBSERVED]

### 3b. Armed-opcode reading (per-engine specialisation)

Each engine arms **its own** opcode space plus a shared control band plus the
wildcard. The armed set decomposes as:

> `{ SEQ control block 0xa0–0xb2 (armed on every engine) }` +
> `{ THAT engine's own compute opcodes }` + `{ 0x9f status/idle }` +
> `{ 0x00 wildcard }`.

* **MARIANA ACT (25):** `0xa0–0xb2` control + `0x21 22 23 24 25`
  (ACTIVATE/QUANTIZE/TABLE_LOAD/READ_ACCUM/ACTIVATE2) + `0x43 44 46 47` +
  `0x77 78 9f` + `0x00`.
* **MARIANA DVE (48):** `0xa0–0xb2` control + the DVE tensor band
  (`0x41 4d 51 42 52 83 84 43 93 53 45 46 47 48 4e 5e 64 8e 65 6a 6b 70 71 72`)
  + `0x77 78` + `0xe0 e1 e2 e3` + **`0x1e3` (mask `0x1ff`)** + `0x98` +
  **`0x30` (Exponential)** + `0x9f` + `0x00`.
* **MARIANA PE (22):** `0xa0–0xb2` control + `0x01 06 02 07 03 09 0a` (matmul) +
  `0xe4` + `0x9f` + `0x00`.
* **MARIANA POOL (1):** `0x00` **only** — per-opcode profiling disarmed, one bin.
* **CAYMAN (47, all engines):** `0x01 06 02 07 03` + the `0xa0–0xb2` control band
  + `0x21 22 23 24 49` + the `0x41–0x68` tensor/move band + `0x00`.

That armed set is precisely the opcode space the SEQ decode hub fetches and
routes — the profiler counts the executing instruction stream per opcode.
[HIGH armed bytes/OBSERVED; "counts at decode" INFERRED-HIGH from the
`{opcode, mask, enable}` shape + the device CSR ic0/ic1 "increment counter"
semantics of §6.]

### 3c. The 9-bit opcode (the structural fingerprint of an opcode CAM)

```
MARIANA DVE CAM slot 43 @offset 0x2b0:
  raw = e3010000 ff010000 01000000 00000000
      → {opcode 0x1e3, mask 0x1ff, enable 1, rsvd 0}
```

A **9-bit** opcode with a **9-bit** match mask is something only an
instruction-opcode-match CAM has. The activation func-CAM keys on an 8-bit
`func_id` + 8-bit `func_id_mask` — it has no 9-bit-opcode concept. This single
record is, on its own, sufficient to refute the activation-CAM reading of
`PROF_CAM`. [HIGH/OBSERVED]

---

## 4. `PROF_TABLE` — byte-exact format (the parallel per-opcode descriptor)

`0x2000` = **8 KiB = 64 records × 128 B**.

### 4a. Geometry — `PROF_CAM` and `PROF_TABLE` are parallel arrays

The number of **populated** (any-nonzero) records **exactly equals** the
`PROF_CAM` armed count, on every engine, and the populated indices are
**contiguous `0 .. N-1`**:

```
GEN ENG  CAM_sha   armed  TAB_sha   pop  (armed==pop?)
CAYMAN  ACT  8fd7e422  47   ce761f81  47   YES
MARIANA DVE  ca588683  48   d72b339f  48   YES
MARIANA PE   43475cec  22   d93b723e  22   YES
MARIANA POOL 0951b326   1   534f2239   1   YES
MARIANA ACT  326bc0dd  25   8786dd11  25   YES
…all 15 (gen,engine) pairs: armed == populated.
```

→ `PROF_CAM[i]` is the opcode arm, `PROF_TABLE[i]` is *that opcode's* profiler
descriptor. [HIGH/OBSERVED]

### 4b. Record content — extremely sparse

The **global** maximum nonzero byte offset across all 15 TABLE blobs is
**`0x17`** (of `0x7f`). Each 128 B record uses at most bytes `0x00 .. 0x17`; the
upper `0x18 .. 0x7f` are always zero.

```c
// per-opcode HW-decode profiler descriptor (128 B stride, ≤0x17 used)
struct hwdecode_table_record {
    uint32_t cfg0;        // +0x00  packed cfg word (compute opcodes only).
                          //        e.g. CAYMAN ACT 0x00000201 / 0x02000205.
    uint32_t cfg1;        // +0x04  packed cfg word. e.g. 0x26000010 / 0x22300010.
    uint8_t  descr[0x0c]; // +0x08..0x13  packed per-opcode descriptor bytes
                          //        (small ints clustered 0x60–0x70 = field-
                          //        position/bitfield bytes; NOT ASCII, NOT floats).
    uint32_t klass;       // +0x14  per-opcode counter/latency CLASS word.
                          //        control opcodes carry ONLY this byte.
                          //        CAYMAN: {0x1000 | class}; MARIANA: bare small
                          //        class (0x03/0x0b/0x19/0x25/0x300…).
    uint8_t  zero[0x68];  // +0x18..0x7f  always 0.
};
```

[geometry + parallel binding + `+0x14` class byte: **HIGH/OBSERVED**; the exact
packed bit layout of `cfg0`/`cfg1`/`descr`: **MED/INFERRED** — not fully decoded.]

Observed samples (CAYMAN ACT, byte-read this page):

```
rec0: bytes[0:0x18] = 01020000 100000 26 6061636f66676c6d62 0000 00 00100000
      → cfg0=0x00000201  cfg1=0x26000010  +0x14 class=0x1000
rec2: bytes[0:0x18] = 05020002 100030 26 6061630066676c6d62 6f6b 00 00100000
      → cfg0=0x02000205  cfg1=0x26300010  +0x14 class=0x1000
```

On MARIANA, control/status/wildcard opcodes carry **only** the `+0x14` class
byte (e.g. `0xa1 → {0x14:0x03}`, `0x9f/0x00 → {0x14:0x19}`); compute opcodes
additionally carry `cfg0`/`cfg1`/`descr` (e.g. DVE `0x30 → {cfg, 0x04:0x10,
descr packed, 0x12:1, 0x14:0x19}`). [HIGH/OBSERVED]

### 4c. Why this is NOT the activation PROFILE table (structural falsification)

| Test | `PROF_TABLE` | activation `aws_hal_stpb_act_profile_entry_t` |
|------|--------------|-----------------------------------------------|
| Density | nonzero **≤ byte 0x17** | **dense to bit 1023** (byte 0x7f); `func_rslt_for_{zero,nan,pos_inf}` alone at bits 592–687 |
| Float test | `float32(rec)` = `7.19e-43`, `4.44e-16`, `7.04e+28` → **denormals / wild magnitudes**, physically impossible as cubic-PWL coefficients | coherent O(1) fp coefficients |
| Key | indexed by the CAM's **opcode slot** | keyed by activation **`func_id`** |

The 47-of-64 / 128 B *size* coincidence is the entire basis of the FW-76
conflation (§5). It dies under the sparse-byte bound, the float-as-denormal
disproof, and the device CSR semantics (§6). [HIGH/OBSERVED]

> **NOTE — the largely-zeroed MARIANA headers.** MARIANA per-engine TABLE
> record-0 headers are zeroed (`cfg0 = cfg1 = 0`) with only the `+0x14` class
> byte set (e.g. MARIANA ACT rec0 `+0x14 = 0x300`) — consistent with a *cleared
> counter-descriptor region the profiler populates*, **not** a baked PWL
> coefficient set. The CAYMAN header carries `0x00000201`; the MARIANA ACT
> header is zeroed (see the re-arm delta, §9).

---

## 5. The conflation, resolved (the headline)

> **QUESTION.** Is `PROF_CAM` (a) purely the instruction-decode profiler
> (RT-18), (b) *also* the activation/transcendental PWL lookup (FW-76/FW-42), or
> (c) two separate tables?
>
> **ANSWER. (a) + (c).** `PROF_CAM`/`PROF_TABLE` are **purely** the per-engine
> HW instruction-decode profiler. The activation PWL is a **separate** four-table
> quad, ACT-only, DMA-staged, **not present in `libnrtucode` at all**. FW-76 and
> FW-42 both conflated "an opcode appears in `PROF_CAM`" with "that opcode is
> selected into a PWL" — a category error.

### 5a. The separate activation PWL — located + named

The activation/transcendental PWL is the four-struct quad in the arch-isa
header `tpb_activation_entries.h` (sha256 `8f6f5f49…`; **byte-identical across
cayman / mariana / mariana_plus / maverick** — see CORRECTION below for sunda):

```c
// tpb_activation_entries.h  (sizeof compile-verified: 32/128/32/32 B)
typedef struct __attribute__((__packed__)) {   // CAM — keyed on (opcode, func_id)
    uint8_t  opcode;        // 7:0
    uint8_t  func_id;       // 15:8   ← the discriminator PROF_CAM does NOT have
    uint16_t rsvd0;         // 31:16
    uint8_t  opcode_mask;   // 39:32
    uint8_t  func_id_mask;  // 47:40
    uint16_t rsvd1;         // 63:48
    uint32_t valid : 1;     // 64
    uint32_t unused1 : 31;  // 95:65
    uint32_t unused2[5];    // 255:96
} aws_hal_stpb_act_cam_entry_t;                 // 32 bytes

aws_hal_stpb_act_profile_entry_t   // 128 B DENSE: pwl_bypass, symmetry_opt_en,
                                   //  symmetry_point, exponent_offset, pos/neg
                                   //  signal thresholds, pwl_control_base_{pos,neg},
                                   //  per-region 20-bit pwl_control words, bias/scale/
                                   //  fma constants, func_rslt_for_{zero,nan,pos_inf,
                                   //  neg_inf} (bits 592–687)
aws_hal_stpb_act_control_entry_t   // 32 B: {act_tbl_base:11, extract_lsb:5,
                                   //        extract_size:4} = the fp→index EXTRACT
aws_hal_stpb_act_bucket_entry_t    // 32 B: {float d0,d1,d2,d3; float x0} = per-segment
                                   //        CUBIC-POLY coeffs + breakpoint
```

The four are the PWL machine: **CONTROL** extracts a bitfield index from the fp
input; **BUCKET** supplies the cubic-poly segment; **PROFILE** supplies the
region/symmetry/affine/special-input config; **CAM** matches `{opcode, func_id}`
→ the slot. The full PWL evaluation pipeline is documented on
[`firmware/kernels/activate-pwl.md`](../firmware/kernels/activate-pwl.md) and
[`uarch/activation-transcendental-tables.md`](../uarch/activation-transcendental-tables.md).
[HIGH/OBSERVED]

### 5b. The contrast that dissolves the coincidence

| | activation (PWL) | `PROF_CAM` / `PROF_TABLE` (profiler) |
|---|---|---|
| CAM stride / key | 32 B, `{opcode, func_id, opcode_mask, func_id_mask}` | 16 B, `{opcode(32), mask, enable, rsvd}` (**no func_id**) |
| TABLE | 128 B **DENSE** PWL config (to byte 0x7f) | 128 B **SPARSE** profiler descriptor (to byte 0x17) |
| 9-bit opcode | impossible (8-bit opcode + func_id) | **`0x1e3` / mask `0x1ff`** present |
| Lives in `libnrtucode.a`? | **no** member | **24** `hwdecode_*_PROF_*` members |
| Extra tables | **adds** BUCKET + CONTROL (ACT only) | none — just the CAM/TABLE pair (every engine) |

### 5c. Resolving FW-76 (the activation conflation)

FW-76's activation PWL decode is **correct as the activation subsystem**; it
merely mis-attributed the carved `PROF_CAM`/`PROF_TABLE` getters to it on the
47-of-64 / 128 B coincidence. The activation tables live in dedicated HW SRAM
at the **ACT-only** regions `TPB_0_ACT_PROFILE_CAM` (`0x1000`) /
`ACT_PROFILE_TABLE` (`0x2000`) / `ACT_BUCKET_TABLE` (`0x10000`) /
`ACT_CONTROL_TABLE` (`0x1000`) plus the `*_LOCAL_STORAGE` shadows, DMA-staged at
runtime by `ActivationTableLoad`/`LoadActFuncSet`.

> **GOTCHA — the ACT name collision (where the conflation grew).** On the **ACT**
> engine, the profiler's `PROFILE_CAM`/`PROFILE_TABLE` HW regions **share the
> name** with the activation `PROFILE_CAM`/`PROFILE_TABLE`. The disambiguator is:
> the activation use is the **four-table quad** (it *adds* BUCKET + CONTROL); the
> profiler use is the **two-table pair present on every engine** (PE/DVE/POOL
> have `PROFILE_CAM`/`PROFILE_TABLE` but **no** BUCKET/CONTROL). The
> BUCKET/CONTROL-only-on-ACT split is what separates them.

### 5d. Resolving FW-42 (the Exponential `0x30` conflation)

```
MARIANA DVE CAM slot 45 @offset 0x2d0:
  raw = 30000000 ff000000 01000000 00000000 → {0x30 Exp, mask 0xff, en 1}
MARIANA DVE CAM slot 41 @offset 0x290:
  raw = e2000000 ff000000 01000000 00000000 → {0xe2 Rand2, mask 0xff, en 1}
```

FW-42's "`0x30` armed @`0x2d0` enable=1" is **correct**, but it is a **profiler
arm** — the identical 16 B `{opcode, mask, enable}` record used to arm the
`0xa0–0xb2` control opcodes and the `0x41–0x53` tensor opcodes. It marks `0x30`
as one of the 48 opcodes the DVE decode-profiler **counts**; it does **not**
select a PWL function.

**Decisive disproof of "`0x30` routes through a DVE PWL":** the address map gives
the DVE engine `PROFILE_CAM` + `PROFILE_TABLE` but **no** `BUCKET_TABLE` and
**no** `CONTROL_TABLE` (those exist **only** for ACT). The DVE has no
activation-style PWL datapath for `0x30` to be routed into. The DVE
transcendental runs on the DVE **BANK datapath**
(`BANK_DATAPATH_RAM`/`BANK_PARAMETER_RAM`/`BANK_OPCODE_RAM`) with the firmware
fp-decompose/convert primitives feeding it; any exp coefficients are host-loaded
into the DVE bank parameter RAM, not into an activation bucket/control table.
[HIGH that DVE has no bucket/control region OBSERVED; "`0x30` profiled-not-PWL-
selected" is the direct consequence — HIGH.]

> **CORRECTION (sunda outlier).** The backing survey stated the activation header
> was byte-identical across **all five** gens. Re-hashing this page: cayman /
> mariana / mariana_plus / maverick share `8f6f5f49…`, but **sunda differs**
> (`dbdca26b…`). The divergence is a field-order swap of `opcode_mask` /
> `func_id_mask` (and 3 reserved-bit reallocations in PROFILE) — the **same**
> 32/128/32/32 B sizes and the **same** `(opcode, func_id)` key. The PROF-vs-PWL
> distinction is unaffected.

---

## 6. The device-side reader — the HW-decode profiler block

The per-engine local register block `tpb_xt_local_reg.json` (csrs/tpb/, attached
to `TPB_0_<ENG>_LOCAL_REG` for ACT/DVE/POOL/PE/SP) names the profiler control +
reader in CSR vocabulary that is, **verbatim**, an instruction-decode profiler:

* `hw_decode` / `disable_hw_decode` — *"Select between Instr FIFO output &
  HW-decode path NX instr. … TPB SP will use this in production (no fast-path).
  All other engines will use this as a chicken bit."* → the HW-decode path is the
  instruction-decode unit the `PROF_CAM` keys; **SP runs off the Instr FIFO** (no
  decode-profiler → **no SP PROF region**).
* `ic0_opcode` / `ic1_opcode` — *"If matches instr.opcode & ICn_mask, INCREMENT
  an ICn counter."* → opcode-match-then-**COUNT**, exactly the `PROF_CAM`
  `{opcode, mask}` arm semantics.
* `profile_cam_search_vector` (32-bit bus) + `profile_cam_search_byte_0..3` —
  *"select byteN IN THE INSTRUCTION and use as part of the cam search process
  (range 0..63)."* → the CAM searches against the decoded **instruction word**
  (the `u32 opcode_id` field), and the 32-bit width matches the record's
  `opcode_id` (and the 9-bit `0x1e3` / mask `0x1ff`).
* `breakpoint_instr_enable` / `breakpoint_profile_table_enable` — *"breakpoint
  sourced from profile table … without having to reload profile table entries."*
  → `PROF_TABLE` is a per-opcode profile/debug descriptor that can also drive
  breakpoints — **not** a function-value lookup.
* `hw_decode_flush_cntr` — the profiler counter flush control.

This device-side evidence closes the conflation beyond RT-18's host-side
argument. [HIGH/OBSERVED]

---

## 7. The CAM-match + counter-arm mechanism (reproduced)

Annotated C pseudocode for how the HW decoder matches a decoded opcode against
the 64-entry CAM and arms the bound counter (the silicon behaviour the CSRs in
§6 describe; the host blobs in §3/§4 are the *staged config* this consumes):

```c
// Per decoded instruction, on an engine whose hw_decode chicken-bit is set.
// CAM/TABLE here are the staged copies of the carved PROF_CAM/PROF_TABLE blobs.
void hwdecode_profile_instruction(uint16_t decoded_opcode /* up to 9 bits */)
{
    // profile_cam_search selects which instruction byte(s) feed the CAM key;
    // for the opcode profiler that key is the (up to 9-bit) opcode field.
    for (int i = 0; i < 64; ++i) {
        const struct hwdecode_cam_record *cam = &PROF_CAM[i];
        if (cam->enable != 1)
            continue;                         // unarmed slot (or trailing zeros)

        // mask == 0xff  → exact 8-bit match
        // mask == 0x1ff → 9-bit match (the one 0x1e3 entry)
        // mask == 0x00  → WILDCARD: (op & 0) == (id & 0) == 0 → matches ALL.
        //                 Always the LAST armed slot → the catch-all bucket.
        if ((decoded_opcode & cam->mask) == (cam->opcode_id & cam->mask)) {
            // PARALLEL ARRAY: slot i in PROF_TABLE is THIS opcode's descriptor.
            const struct hwdecode_table_record *desc = &PROF_TABLE[i];

            // +0x14 class word selects the counter/latency class this opcode
            // increments (ic0/ic1 per the device CSR "increment an ICn counter").
            ic_counter[desc->klass]++;        // the profiler COUNT

            // breakpoint_profile_table_enable: the same descriptor can source a
            // breakpoint without reloading table entries.
            if (breakpoint_profile_table_enable && desc_breakpoint_armed(desc))
                raise_decode_breakpoint(decoded_opcode);

            break;  // first match wins; the trailing 0x00-wildcard catches the rest
        }
    }
    // If hw_decode is disabled (SP), the instruction never reaches this path —
    // it is consumed from the Instr FIFO and is never opcode-decode-profiled.
}
```

The key point for reimplementers: `PROF_CAM[i]` and `PROF_TABLE[i]` are
**parallel arrays** (§4a). The CAM decides *whether* an opcode is profiled and
which slot it binds; the TABLE's `+0x14` class word decides *which counter
class* it increments. The wildcard slot (`mask == 0`, always last) is the
"everything else" bucket. POOL is a profiler armed to **only** the wildcard —
"profile all opcodes in one bin", i.e. per-opcode profiling effectively
disarmed (NOT a PWL that is "off"). [mechanism INFERRED-HIGH from the
`{opcode, mask, enable}` shape + the §6 CSRs; the staged config is OBSERVED.]

### 7a. Per-engine arming reconciled with the profiler role

| ENGINE | Arming (MARIANA) | Profiler role |
|--------|------------------|---------------|
| ACT | 25 armed (per-opcode) | profiles ACT's activation + control opcodes |
| DVE | 48 armed (per-opcode) | profiles DVE tensor/transcendental + `0x1e3` (9-bit) + `0x30`/`0xe2` |
| PE | 22 armed (per-opcode) | profiles PE matmul opcodes |
| POOL | 1 armed (wildcard only) | per-opcode profiling **disarmed** — single `0x00` catch-all bin |
| SP | **no PROF region** | SP runs off the Instr FIFO (`disable_hw_decode`); no decode-profiler to arm |

CAYMAN profiles engine-generically (one 47-arm blob across all four engines);
from MARIANA the profiler is per-`(gen, engine)`. "Armed vs disarmed" is
orthogonal to "profiler vs PWL": POOL is a profiler armed to one bin, not a
PWL turned off. [HIGH/OBSERVED]

---

## 8. Per-gen stability (the cross-gen sha matrix)

`PROF_CAM` sha256 (first 8), this page's carve:

| ENG | CAYMAN | MARIANA | MARIANA_PLUS | MAVERICK |
|-----|--------|---------|--------------|----------|
| ACT | `8fd7e422` | `326bc0dd` | `326bc0dd` | (no ACT) |
| DVE | `8fd7e422` | `ca588683` | `ca588683` | `dbff2b84` |
| PE | `8fd7e422` | `43475cec` | `43475cec` | `85d857a7` |
| POOL | `8fd7e422` | `0951b326` | `0951b326` | `0951b326` |

`PROF_TABLE` sha256: CAYMAN all `ce761f81`; MARIANA ACT `8786dd11` / DVE
`d72b339f` / PE `d93b723e` / POOL `534f2239`; **MARIANA_PLUS == MARIANA** (all 4,
CAM + TABLE); MAVERICK DVE `f349e417` / PE `e94d413a` / **POOL `534f2239`
(== MARIANA POOL)**.

Readings (all OBSERVED this page):

* **CAYMAN** — one blob for all four engines (engine-generic profiler).
* **MARIANA** — per-engine (4 distinct CAM + 4 distinct TABLE).
* **MARIANA_PLUS == MARIANA** byte-for-byte, CAM **and** TABLE, all four engines.
* **MAVERICK** — DVE/PE distinct; **POOL CAM (`0951b326`) and TABLE (`534f2239`)
  byte-identical to MARIANA POOL**. The disarmed-POOL profiler config is frozen
  MARIANA → MARIANA_PLUS → MAVERICK.

The activation PWL header (`8f6f5f49…`) is the orthogonal stability story: its
*format* is gen-stable (cayman/mariana/mplus/maverick identical; sunda swaps two
mask bytes), while the profiler *blobs* are gen-stable from MARIANA on,
per-engine. [HIGH/OBSERVED]

---

## 9. Per-gen image diffs — how they read the PROF arming

The per-gen image pages diff the PROF blobs to characterise an engine variant.
The canonical worked example is the **MARIANA ACT re-arm** (already observed by
[`mariana-act.md`](./mariana-act.md), re-verified byte-exact here):

| Property | CAYMAN ACT | MARIANA ACT | Delta |
|----------|-----------|-------------|-------|
| `PROF_CAM` enabled | 47 records | **25** records | **47 → 25** |
| Added opcodes | — | `0x25` (Activate2), `0x77`, `0x78`, `0x9f` | **+4** |
| `PROF_TABLE` header (`cfg0`/`cfg1`) | `0x00000201` / `0x26000010` | **zeroed** (`0x0` / `0x0`) | header cleared |
| `PROF_TABLE` nonzero 32-bit words | **150** | **68** | `150 → 68` |

```
$ python  # MARIANA ACT vs CAYMAN ACT armed-opcode delta
MARIANA-only (added): ['0x25', '0x77', '0x78', '0x9f']
nonzero words: CAYMAN ACT TABLE = 150   MARIANA ACT TABLE = 68
CAYMAN ACT TABLE rec0 first word = 0x00000201   MARIANA ACT rec0 = 0x00000000
```

The reading: MARIANA's ACT decode-profiler is re-armed for the MARIANA opcode
mix — fewer, different opcodes counted, headers cleared to the
counter-descriptor baseline. A per-gen image page reads this as "the HW-decode
profiler was re-armed", **not** "the activation function changed" (the activation
*function* is the separate PWL of §5). [HIGH/OBSERVED]

The generic recipe a per-gen page applies: (1) carve `PROF_CAM`/`PROF_TABLE` via
the getter `lea` targets; (2) sha256 vs the prior gen's same `(engine)` blob —
identical ⇒ frozen, different ⇒ re-armed; (3) decode the armed-opcode set delta
(`enable == 1`); (4) decode the `PROF_TABLE` header/class-byte delta. The
WALL arch_id binding (`36`) is INFERRED upstream of these binaries; this page is
the authoritative PROF-format source other pages defer to.

---

## 10. Adversarial self-verification

The five strongest claims, re-challenged against the binary:

1. **The 16 B CAM layout `{opcode:32, mask, enable, rsvd}`.** *Challenge:* could
   `mask`/`enable` be a single 64-bit field, or `opcode` be 16-bit + a 16-bit
   func_id? *Result:* decoding all 960 records, `enable ∈ {0,1}`,
   `mask ∈ {0x00, 0xff, 0x1ff}`, `reserved ≡ 0` — three disjoint 32-bit fields
   with tightly bounded domains, and the `0x1e3`/`0x1ff` record forces a
   ≥9-bit `opcode` with **no** func_id. **HOLDS.**
2. **The 128 B TABLE layout (sparse, `+0x14` class).** *Challenge:* is the
   128 B stride real or just 64 records of variable size? *Result:* populated ==
   armed and contiguous on all 15 pairs; global max nonzero offset `0x17`;
   `+0x14` class byte present on control-only records. A 128 B fixed stride with
   ≤`0x17` used is the only consistent reading. **HOLDS** (exact `cfg0`/`cfg1`
   bit layout remains MED).
3. **PROF-vs-PWL distinction.** *Challenge:* is the activation CAM really a
   different struct? *Result:* `tpb_activation_entries.h` declares
   `aws_hal_stpb_act_cam_entry_t` = 32 B with `func_id`/`func_id_mask`; `.a`
   ships **0** activation members and **24** `hwdecode_*_PROF_*`; the float test
   yields denormals; the address map puts BUCKET/CONTROL on ACT only; the device
   CSRs say "increment a counter". Six independent disproofs. **HOLDS.**
4. **4/4 byte-identity within CAYMAN.** *Challenge:* coincidental hash collision?
   *Result:* all four CAYMAN getters `lea` the *same* `.rodata` content reachable
   by hash (`8fd7e422` / `ce761f81`) across ACT/DVE/POOL/PE; `armed == populated`
   on each. **HOLDS.**
5. **Per-gen re-arm deltas.** *Challenge:* is MARIANA_PLUS truly == MARIANA and
   MAVERICK POOL == MARIANA POOL? *Result:* sha matrix confirms all four
   MARIANA_PLUS CAM+TABLE equal MARIANA, and MAVERICK POOL CAM `0951b326` /
   TABLE `534f2239` equal MARIANA POOL; MARIANA ACT re-arm `47→25` adds exactly
   `{0x25, 0x77, 0x78, 0x9f}` with header zeroed (`150→68` nonzero words).
   **HOLDS.**

One survey claim was **corrected**: the activation header is *not* byte-identical
across all five gens — sunda diverges (§5d CORRECTION). The PROF-vs-PWL verdict
is unaffected.

---

## Reproduction

```sh
INT=…/custom_op/c10/lib/libnrtucode_internal.so   # b7c67e89…632fc329b
AR=…/custom_op/c10/lib/libnrtucode.a
# accessors:  nm $INT | rg -c 'PROF_(CAM|TABLE)_get$'            → 30
# .a members: ar t $AR | rg -c 'hwdecode.*PROF_(CAM|TABLE)'      → 24 (0 MAVERICK, 0 activation)
# resolver:   objdump -d --start=0x9b2cd0 --stop=0x9b2d18 $INT
# list relocs: readelf -rW $INT  (30 R_X86_64_RELATIVE in [0x9b9090, +38*16))
# getter stub: objdump -d --start=0x9b3ba0 --stop=0x9b3bc0 $INT  (lea;mov(rdi);movq 0x400;ret)
# carve (.rodata id-mapped): CAM @blobVA size 0x400 ; TABLE @blobVA size 0x2000
#   CAYMAN ACT CAM 0x3028a0 / TAB 0x302ca0 ; MARIANA DVE CAM 0x59e880 / TAB 0x59ec80 (§2b)
# CAM decode: 16B {op,mask,en,rsvd}; en==1 armed; mask 0=wildcard, 0x1ff=9-bit
#   MARIANA DVE: 0xe2@0x290, 0x1e3@0x2b0 (mask 0x1ff), 0x30@0x2d0
# TABLE decode: 64×128B; populated==armed; global max nonzero offset 0x17; float test=denorm
# activation PWL header: arch-headers/cayman/tpb_activation_entries.h
#   (cam32 {opcode,func_id,…} / profile128 dense / control32 / bucket32-float ; sha 8f6f5f49)
# device CSR: csrs/tpb/tpb_xt_local_reg.json
#   hw_decode / ic0_opcode (increment counter) / profile_cam_search_byte_0..3 / breakpoint_profile_table
```

## See also

* [`images/cayman-act.md`](./cayman-act.md) · [`images/mariana-act.md`](./mariana-act.md)
  — committed per-gen ACT image pages that forward-link here.
* [`firmware/kernels/activate-pwl.md`](../firmware/kernels/activate-pwl.md) —
  the **separate** activation PWL quad (CAM/PROFILE/CONTROL/BUCKET).
* [`uarch/activation-transcendental-tables.md`](../uarch/activation-transcendental-tables.md)
  — the transcendental table machine.
* `runtime/image-hwdecode-resolvers.md` (#831) — the host `get_hwdecode_table`
  resolver (planned).
* `control/security/profiling-trace-debug-gating.md` (#949) — the
  profiling/trace/debug CSR gating (planned).
