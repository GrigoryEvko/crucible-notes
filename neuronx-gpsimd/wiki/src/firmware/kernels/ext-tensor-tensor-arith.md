# Extended Tensor-Tensor Arith — `decode_extended_inst_tensor_tensor_arith` (`0xF0`/spec 2, struct `EXTENDED_TTA`)

This page decodes the **`0xF0`-EXTENDED variant** of the GPSIMD NeuronCore's binary
elementwise tensor-tensor arithmetic — the leaf kernel the firmware self-names
`decode_extended_inst_tensor_tensor_arith(bool, unsigned)`. It is a **second, narrower
entry point** into the same add/multiply elementwise datapath the
[base tensor-tensor kernel](tensor-tensor.md) (`TensorTensorArithOp` `0x41` /
`TensorTensorBitvecOp` `0x51`, struct `S3S3D3_TT`) implements, but reached through the
**ExtendedInst opcode-extension escape** (base opcode `0xF0`, sub-selected by the **spec
byte**) rather than a top-level opcode, and carrying a *different* 64-byte wire struct
(`NEURON_ISA_TPB_EXTENDED_TTA_STRUCT`). The [`0xF0` extended-opcode dispatch](../pool/pool-ext-0xf0.md)
page owns *how* `0xF0`/spec resolves to one of five handlers; this page owns *what spec 2's
handler does*, and frames it strictly as the **delta over the base op** — it does **not**
re-derive the 60-entry `NEURON_ISA_TPB_ALU_OP` table (that is pinned definitively on the
[base page](tensor-tensor.md#3-the-full-alu-op-table--neuron_isa_tpb_alu_op)).

This is the **Cadence Tensilica Vision-Q7 *Cairo* (`ncore2gp`) GPSIMD compute core's** own
POOL-engine firmware — hand-scheduled, windowed-ABI Xtensa FLIX/VLIW code in the `ncore2gp`
configuration (Xtensa24, RI-2022.9, NX1.1.4, 512-bit datapath). Every device fact below is
byte-pinned to the carved `CAYMAN` POOL **PERF** EXTISA image
(`extisa_CAYMAN_POOL_PERF_EXTISA_0`, embedded in `libnrtucode_internal.so`; `.text` VMA
`0x01000000` == file off `0x100`), re-disassembled with the native `xtensa-elf-objdump`
(`XTENSA_CORE=ncore2gp`); every host-ISA fact (the operand struct, the extended-opcode enum,
the completion-info bitfield) is read field-exact (and **compile-asserted**, §2) from the
`aws_neuron_isa_tpb_extended*.h` arch-isa headers shipped in the same customop-lib
(`0.21.2.0`) package. The `extracted/` and `ida/` trees are gitignored — reach them with
`fd --no-ignore` or absolute paths. Confidence/evidence tags follow the project
[Confidence & Walls model](../../reference/confidence-model.md): `[HIGH/OBSERVED]` =
read-from-byte / read-from-header / compile-asserted, `[MED/INFERRED]` = reasoned
over OBSERVED (typically across a FLIX/literal-pool desync), `[…/CARRIED]` = re-used at a
cited sibling page's confidence.

> **Scope of the device disassembly.** The `0x010034b0` body is **FLIX VLIW with interleaved
> literal pools** that desync under *stock* `objdump` (the documented carve limitation):
> the function-start record, the `entry a1,32` prologue, the `.bss` state-slot `const16`
> relocs, and the surviving IVP mnemonics recover cleanly, but the per-arm switch *bodies*
> sit inside the desyncing bundles. Every structural claim is anchored to a `kernel_info_table`
> entry, a relocation, a `.xt.prop` record, a compile-asserted offset, or an IVP mnemonic that
> survives the desync — **never** to a guessed bundle interior. v2–v4
> (`SUNDA`/`CAYMAN`/`MARIANA`/`MARIANA_PLUS`) are byte-grounded; v5/`MAVERICK` is
> header-OBSERVED + structurally-OBSERVED (stripped image) only — flag any v5 interior
> claim INFERRED.

---

## 1. The verdict up front — what `decode_extended_inst_tensor_tensor_arith` IS

`decode_extended_inst_tensor_tensor_arith` is the leaf kernel behind the `0xF0` ExtendedInst
opcode, **sub-code spec 2**
(`NEURON_ISA_TPB_EXAMPLE_EXTENDED_OPCODES1_EXTENDED_TENSOR_TENSOR_ARITH = 2`). It is a
deliberately **minimal** elementwise tensor-tensor **add/multiply** variant, distinguished
from the base op on three axes:

* **a different operand wire-struct** — `NEURON_ISA_TPB_EXTENDED_TTA_STRUCT` (64 B) with
  **2-D** (`TENSOR2D`) mem patterns and a 2-byte ExtendedInst preamble (`extended_opcode` +
  `completion_info`), vs the base `S3S3D3_TT`'s **3-D** (`TENSOR3D`) patterns +
  `num_active_channels` (§2/§3);
* **a restricted ALU-op subset** — the header pins `op` "restricted to add/multiply" and the
  type comment says "adds two tensors together"; it does **not** carry the full 60-op ALU
  table the base `0x41`/`0x51` accept (§3);
* **a different dispatch route** — it is reached via the `0xF0` spec-2 trampoline
  (`kernel_info_table` idx 8), **not** the base `0x41`/`0x51` rows (§1.1).

> **NOTE — "EXTENDED" means *reached through the extension mechanism*, not *more capable*.**
> The `EXTENDED_INST` opcode `0xF0` is the *escape hatch* for "customer-specific ops" — the
> header comment on `NEURON_ISA_TPB_OPCODE_EXTENDED_INST` reads "extended instruction space
> for customer specific ops" (`common.h:301`). New ops can be added under `0xF0`/spec **without
> consuming a top-level opcode**. On the operand axis the extended TTA is actually **narrower**
> than the base: 2-D patterns, no explicit channel count (a port model instead), add/mul-only.
> `[HIGH/OBSERVED — header comment + struct diff]`

| property | value | evidence |
|---|---|---|
| funcVA (CAYMAN) | trampoline `0x01003484` (`kernel_info_table` idx 8) → `callx8` → body `0x010034b0` | `[HIGH/OBSERVED — table row + .xt.prop start]` |
| `.xt.prop` symbol | `_Z40decode_extended_inst_tensor_tensor_arithbj` = `decode_extended_inst_tensor_tensor_arith(bool, unsigned)` | `[HIGH/OBSERVED — c++filt]` |
| opcode / spec | opcode `0xF0` (240, `EXTENDED_INST`), spec **2** (`EXTENDED_TENSOR_TENSOR_ARITH`) | `[HIGH/OBSERVED — common.h:301 + extended_utils.h:25]` |
| operand struct | `NEURON_ISA_TPB_EXTENDED_TTA_STRUCT`, **64 bytes** (compile-asserted, §2) | `[HIGH/OBSERVED]` |
| ALU subset | header: `op` "restricted to add/multiply" | `[HIGH/OBSERVED — extended_utils.h:95]` |

### 1.1 The `0xF0`/spec-2 route — `kernel_info_table` idx 8

The two-level POOL dispatch is realized **entirely by the `kernel_info_table`**: there is **no
in-loop `if (opcode == 0xF0)` branch** (the [`0xF0` dispatch page](../pool/pool-ext-0xf0.md#0-the-extended-dispatch-in-one-diagram)
characterizes this fully). Opcode `0xF0` is **registered five times** with five distinct spec
bytes; one linear scan over the packed key `(opcode<<24 | spec<<16)` lands a `(0xF0, spec)`
microcode instruction on exactly one of the five rows. The byte layout (little-endian key)
reads `00 00 <spec> f0` on disk:

| idx | opcode | spec | funcVA (CAYMAN) | handler |
|----:|:------:|:----:|:----------------|:--------|
| 6 | `0xf0` | 0 | `0x01003370` | `ExtendedInstEngineNop` (clean no-op `entry; movi a2,0; retw.n`) |
| 7 | `0xf0` | 1 | `0x01003380` | `pool_extended_inst_copy()` (funcVA == impl) |
| **8** | **`0xf0`** | **2** | **`0x01003484`** → `callx8` → **`0x010034b0`** | **`decode_extended_inst_tensor_tensor_arith` ← THIS PAGE** |
| 9 | `0xf0` | 4 | `0x010037a8` | Rand/Cptc band (`entry a1,48`; state `0x46c`; via `decode_pool`) |
| 10 | `0xf0` | 3 | `0x01003a60` | Rand/Cptc band (`entry a1,32`; state `0x470`; via `decode_pool`) |

`[HIGH/OBSERVED — re-parsed kernel_info_table @ VMA 0x02000380, file off 0x7400; CARRIED from pool-ext-0xf0.md §1]`

> **QUIRK — registration order is `0,1,2,4,3`, not sorted.** The five `0xF0` rows are stored
> in registration order, so **spec 4 precedes spec 3** in the table. A linear key-scan is
> order-independent (harmless to dispatch), but a reimplementation that binary-searches the
> table assuming `(opcode,spec)` sort would mis-locate the spec-3/4 rows. Scan linearly, or
> sort first. `[HIGH/OBSERVED — CARRIED]`

The spec-2 trampoline at `0x01003484` is itself FLIX-scheduled, but its skeleton recovers
cleanly (native `xtensa-elf-objdump`, byte-0 re-anchored):

```
 1003484:  36 41 00     entry   a1, 32
 1003487:  24 00 02     const16 a2, 0x200
 100348a:  24 68 04     const16 a2, 0x468    ; a2 = 0x02000468 — per-kernel .bss state ptr
            …(FLIX body — partial DESYNC)…
 10034a4:  24 b0 34     const16 a2, 0x34b0   ; a2 = 0x010034b0 — impl VA
 10034a7:  e0 02 00     callx8  a2           ; call decode_extended_inst_tensor_tensor_arith()
 10034aa:  0c 02        movi.n  a2, 0
 10034ac:  1d f0        retw.n
```

The `callx8` target `0x010034b0` is the function start of
`.xt.prop._Z40decode_extended_inst_tensor_tensor_arithbj` (first prop record `b0 34 00 01` =
VMA `0x010034b0`), landing on a clean `entry a1,32`. The `bool` first parameter is the same
decode-mode flag the trampoline's `decode_pool`-family siblings take; the `unsigned` is the
per-instruction SBUF descriptor index. `[HIGH/OBSERVED — trampoline disasm + .xt.prop start; CARRIED from pool-ext-0xf0.md §2]`

> **NOTE — the `const16` pair idiom.** Xtensa `const16 aR, imm16` computes
> `aR = (aR << 16) | imm16`, so `const16 a2,0x200 ; const16 a2,0x468` materializes the 32-bit
> literal `0x02000468` (the `.bss` state slot, shared with `ExtendedInstCopy` per
> [pool-ext-0xf0.md §4](../pool/pool-ext-0xf0.md#4-per-kernel-state-slots--the-bss-descriptor-band)),
> and `const16 a2,0x34b0` (preceded by a `0x0100`-band high half) forms `0x010034b0`.

### 1.2 Contrast with the FW-49 base routes

Base tensor-tensor and extended tensor-tensor are **three distinct `kernel_info_table` rows**,
three distinct decode entries, all reading from the same `NEURON_ISA_TPB_ALU_OP` enum but the
extended one restricted to add/mul:

| route | opcode/spec | table idx | decode entry (CAYMAN) | state slot |
|---|---|---|---|---|
| base arith | `0x41` | idx 5 | `0x01000f1c` `pool_tensor_tensor_arith_op` | `0x0200045c` |
| base bitvec | `0x51` | idx 4 | `0x0100105c` → `decode_tensor_tensor_arith@0x01000f60` | (shared) |
| **extended** | **`0xF0`/spec 2** | **idx 8** | **`0x01003484` → `0x010034b0`** | **`0x02000468`** |

`[HIGH/OBSERVED — table rows; base VMAs CARRIED from tensor-tensor.md §1.1]` The base rows do
**not** carry the ExtendedInst preamble; the `0xF0` row does (§3).

### 1.3 Host-side registration cross-check

The host driver registers the spec presence at boot. `nrtucode_opset_add_instruction`
(x86-64 symbol `0x9b2660` in `libnrtucode_internal.so`, 294 B / `0x126`) reads the instruction
word's **byte 12** (`= extended_opcode`, the wire field at off 12, §2) **only** when the base
opcode is `0xF0`, and records that spec in a 256-entry presence table. The x86-64 disassembly
pins the gate and the read exactly:

```asm
 9b2688:  41 81 fe f0 00 00 00   cmp    $0xf0, %r14d      ; opcode == 0xF0 gate
            …(je extended-path; calloc(1,0x100) — the 256-byte spec table)…
 9b26e1:  44 0f b6 4e 0c         movzbl 0xc(%rsi), %r9d   ; read instruction byte [rsi+0xC] = extended_opcode
            …(cmpb $0x0,(%rcx,%r9,1) ; movb $0x1,(…) — index the 256-entry seen-flag table by spec)…
 9b2710:  41 b8 f0 00 00 00      mov    $0xf0, %r8d       ; 0xF0 passed to the registration log
```

So the wire byte at off 12 IS the spec the device's `kernel_info_table` key matches *and* the
host registers (indexed into a 256-entry table) — the chain is **cross-consistent
host↔device↔struct**. `[HIGH/OBSERVED — x86-64 disasm of nrtucode_opset_add_instruction; see [nrtucode-opset](../../runtime/nrtucode-opset.md)]`

---

## 2. The operand struct — `NEURON_ISA_TPB_EXTENDED_TTA_STRUCT` (64 B), compile-asserted

Read field-exact from `aws_neuron_isa_tpb_extended_utils.h:88`, and **compile-asserted** this
pass (`gcc -I<tpb>` of the real shipped headers `extended.h` + `extended_utils.h` + `common.h`
through `offsetof`/`sizeof` — output reproduced below). The struct is the wire format spec 2's
handler decodes.

| off | size | field | type | notes |
|---|---|---|---|---|
| 0–3 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode:1, inst_word_len:1, debug_cmd:1, debug_hint:1}`; `opcode`=`0xF0` (`EXTENDED_INST`) |
| 4–11 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | `{wait_mode, wait_idx, update_mode, update_idx, semaphore_value:4}` (sync) |
| **12** | **1** | **`extended_opcode`** | **`EXAMPLE_EXTENDED_OPCODES1`** | **`= 2` (`EXTENDED_TENSOR_TENSOR_ARITH`) — `0xF0` sub-selector; `<==NEW` vs base** |
| **13** | **1** | **`completion_info`** | **`EXT_COMPLETION_INFO`** | **`{has_read:1, _:1, has_write:1, num_active_ports:3, _:2}`; `<==NEW` vs base** |
| 14 | 1 | `in0_in1_dtype` | `NEURON_ISA_TPB_DTYPE_PAIR` | `{dtype_lo:4 (src0), dtype_hi:4 (src1)}` |
| 15 | 1 | `out_dtype` | `NEURON_ISA_TPB_DTYPE` | destination element dtype |
| **16** | **1** | **`op`** | **`NEURON_ISA_TPB_ALU_OP`** | **header comment: "op restricted to add/multiply"** |
| 17–19 | 3 | `reserved0[3]` | `uint8_t[3]` | must be 0 |
| 20–31 | 12 | `src0_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | INPUT TENSOR 0 (**2-D** strided) |
| 32–43 | 12 | `src1_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | INPUT TENSOR 1 (**2-D** strided) |
| 44–55 | 12 | `dst_mem_pattern` | `NEURON_ISA_TPB_TENSOR2D` | OUTPUT TENSOR (**2-D** strided) |
| 56–63 | 8 | `reserved1[8]` | `uint8_t[8]` | must be 0 |

`[HIGH/OBSERVED — aws_neuron_isa_tpb_extended_utils.h:88; offsets compile-asserted]`

```c
// aws_neuron_isa_tpb_extended_utils.h:88 — verbatim
// TensorTensor: adds two tensors together, writes to dest.
//   Data converters enabled, specified by dtypes;  128 active channels.
//   completion_info.has_read = 1; has_write = 1; num_active_ports = 0; // 128 partitions
typedef struct NEURON_ISA_TPB_EXTENDED_TTA_STRUCT {
    NEURON_ISA_TPB_HEADER                    header;            // 4   ( 0 -  3)
    NEURON_ISA_TPB_EVENTS                    events;            // 8   ( 4 - 11)
    NEURON_ISA_TPB_EXAMPLE_EXTENDED_OPCODES1 extended_opcode;   // 1   (12     )
    NEURON_ISA_TPB_EXT_COMPLETION_INFO       completion_info;   // 1   (13     )
    NEURON_ISA_TPB_DTYPE_PAIR                in0_in1_dtype;     // 1   (14     )  // src0 lo, src1 hi
    NEURON_ISA_TPB_DTYPE                     out_dtype;         // 1   (15     )
    NEURON_ISA_TPB_ALU_OP                    op;                // 1   (16     )  // op restricted to add/multiply
    uint8_t                                  reserved0[3];      // 3   (17 - 19)
    NEURON_ISA_TPB_TENSOR2D                  src0_mem_pattern;  // 12  (20 - 31)
    NEURON_ISA_TPB_TENSOR2D                  src1_mem_pattern;  // 12  (32 - 43)
    NEURON_ISA_TPB_TENSOR2D                  dst_mem_pattern;   // 12  (44 - 55)
    uint8_t                                  reserved1[8];      // 8   (56 - 63)
} NEURON_ISA_TPB_EXTENDED_TTA_STRUCT;
```

Compile-assertion output (`gcc -I<cayman/tpb>`):

```
sizeof EXTENDED_TTA = 64
header=0 events=4 ext_op=12 compl=13 in0_in1=14 out=15 op=16 rsvd0=17 src0=20 src1=32 dst=44 rsvd1=56
sz TENSOR1D=8 TENSOR2D=12 TENSOR3D=16 COMPL=1 DTYPE_PAIR=1 HEADER=4 EVENTS=8
```

`[HIGH/OBSERVED — gcc offsetof/sizeof, exact match to the header comments]`

### 2.1 The `TENSOR2D` mem pattern (12 B) — the narrowing axis

```c
// aws_neuron_isa_tpb_common.h:643  (sizeof == 12)
typedef struct NEURON_ISA_TPB_TENSOR2D {
    NEURON_ISA_TPB_ADDR4 start_addr;    // 4  — partition-offset / addr-reg / shape-reg union (marker byte)
    int16_t              step_elem[2];  // 4  — per-dim signed stride (elements); 0 => broadcast axis
    uint16_t             num_elem[2];   // 4  — per-dim count
} NEURON_ISA_TPB_TENSOR2D;
```

The base `S3S3D3_TT` uses **`TENSOR3D` (16 B)** — `{ADDR4, int16 step[3], uint16 num[3]}` — a
**3-D** iteration space. The extended TTA's `TENSOR2D` drops one dimension (`step[3]→step[2]`,
`num[3]→num[2]`), saving 4 B per pattern × 3 = 12 B; that 12 B (plus the absent
`num_active_channels` byte) is exactly the space the 2-byte ExtendedInst preamble +
`reserved0[3]` + `reserved1[8]` reclaim, keeping the struct at 64 B. The `start_addr` /
`ADDR4` semantics (immediate `PARTITION_OFFSET`, `ADDR_REG4`, shape-reg, marker byte) are
**identical** to the base — see [tensor-tensor.md §2](tensor-tensor.md#2-the-operand-struct--neuron_isa_tpb_s3s3d3_tt_struct-64-b).
`[HIGH/OBSERVED — common.h:643; sizeof compile-asserted = 12]`

### 2.2 The `completion_info` bitfield (1 B) and the port model

```c
// aws_neuron_isa_tpb_extended.h:22  (NEURON_ISA_PACKED, 1 byte; sizeof compile-asserted = 1)
typedef struct NEURON_ISA_TPB_EXT_COMPLETION_INFO {
    uint8_t has_read           : 1;   // instruction issues ≥1 read tensor
    uint8_t reserved0_bitfield : 1;   // must be 0
    uint8_t has_write          : 1;   // instruction issues ≥1 write tensor
    uint8_t num_active_ports   : 3;   // see encoding below
    uint8_t reserved1_bitfield : 2;   // must be 0
} NEURON_ISA_PACKED NEURON_ISA_TPB_EXT_COMPLETION_INFO;

// num_active_ports (extended.h:31): number of active read/write ports, 16 partitions per port:
//   0   == 8 ports (128 partitions)
//   1-7 == 1-7 ports ([1..7]*16 active partitions)
//   Each active "port" corresponds to a single Q7, starting at q7[0].
```

For the TTA the header fixes `has_read=1, has_write=1, num_active_ports=0` (128 partitions,
8 Q7 lanes). This is the **wire-level confirmation that a NeuronCore's POOL work fans across
8 Vision-Q7 lanes** — one port per Q7. `[HIGH/OBSERVED — extended.h:22-38 bitfield + comment]`

> **GOTCHA — `num_active_ports == 0` means *all 8 ports*, not *zero ports*.** The encoding is
> "0 ⇒ 8 ports (128 partitions); 1..7 ⇒ that many ports × 16 partitions." A reimplementation
> that treats `0` as "no ports" would idle the whole engine. The 3-bit field cannot encode
> "8" literally, so `0` is overloaded as the all-on case. `[HIGH/OBSERVED — extended.h:31]`

> **QUIRK — the channel count is *derived*, not a field.** The base struct carries
> `num_active_channels @off15` (a `uint8_t`, 1..128). The extended struct has **no such
> field** — the active span is `num_active_ports × 16` partitions (or 128 when the field is 0).
> So the extended TTA cannot select an arbitrary 1..128 channel count; it is quantized to a
> multiple of 16 (a port granularity). This is a real capability *reduction* vs the base.
> `[HIGH/OBSERVED — struct diff]`

---

## 3. What "EXTENDED" adds / changes vs the FW-49 base — the central deliverable

"Extended" = "reached via the `0xF0` ExtendedInst opcode-extension mechanism," **not** "more
capable." Diffing the `EXTENDED_TTA` decode against the base tensor-tensor decode, byte-exact:

| axis | BASE (`0x41`/`0x51`, `S3S3D3_TT`) | EXTENDED (`0xF0`/spec 2, `EXTENDED_TTA`) | verdict |
|---|---|---|---|
| opcode / route | `0x41` idx5 / `0x51` idx4 | `0xF0` idx8 spec 2 | **different ROUTE** |
| decode entry funcVA | `0x01000f1c` / `0x01000f60` | `0x01003484` → `0x010034b0` | different fn |
| wire preamble (off 12–13) | `in0_in1_dtype`, `out_dtype` | `extended_opcode`, `completion_info` | **ADDS** 2-byte ext preamble |
| dtype-pair position | off 12–13 | off 14–15 (shifted by the preamble) | relocated |
| `op` field position | off 14 | off 16 | relocated |
| channel-count field | `num_active_channels @off15` (1..128) | `completion_info.num_active_ports` (×16) | **REPLACED** (ports, not chans) |
| mem-pattern dims | `TENSOR3D` (16 B, 3-D strided) × 3 | `TENSOR2D` (12 B, 2-D strided) × 3 | **NARROWER** (2-D not 3-D) |
| ALU op set | `op @off14`, **FULL 60-op table** | `op @off16`, **"restricted to add/multiply"** | **RESTRICTED** op subset |
| second op selector | none (TT) | none | NO (unlike STT) |
| 64-bit (i64/u64) split | **YES** — `tensor_tensor_64bit_dispatch<…>` | **NO** (no reloc to those fns, §4/§6) | **NOT** in extended |
| bitvec class | YES (`0x51` path, CRC32, shifts, AND/OR/XOR) | NO (arith add/mul only) | **NARROWER** |
| body size | impl `0x3b4` (948 B) + four dispatchers | body `0x010034b0` ≈ 742 B, self-contained | **MUCH SMALLER** |

`[HIGH/OBSERVED — struct diff (compile-asserted, §2) + reloc-pinned body (§4) + base CARRIED from tensor-tensor.md]`

**So "EXTENDED" does NOT add:** more dtypes (same 16-value `NEURON_ISA_TPB_DTYPE` enum, §5),
wider/64-bit operands (64-bit is the **base** kernel's job, §6), additional ALU ops (it has
**fewer** — add/mul only), a second op selector (Scalar-Tensor-Tensor has that, not TTA), or
extra addressing dimensions (it has **fewer**: 2-D vs 3-D).

**What it actually adds / changes:**

1. **the ExtendedInst (`0xF0`) dispatch envelope** — `extended_opcode @12` +
   `completion_info @13` — i.e. it is the *same* elementwise add/mul reachable through the
   `0xF0` opcode-extension escape, so new ops can be added **without consuming a top-level
   opcode**;
2. **a port-based channel model** (`num_active_ports`, 16 partitions/port, 1 Q7/port) instead
   of an explicit channel count;
3. **a leaner 2-D wire format** (`TENSOR2D` × 3).

The header type comment is explicit: *"adds two tensors together, writes to dest … Data
converters enabled, specified by dtypes; 128 active channels."* This is a deliberately
**minimal** elementwise add/multiply variant exposed through the extensible-opcode slot.
`[HIGH/OBSERVED — extended_utils.h:77-85 type comment + struct diff]`

> **CORRECTION — the base "owns" 64-bit; the extended op is *not* the wide path.** A natural
> guess is that "extended" means "extended width" (64-bit). The binary refutes this: the 64-bit
> (INT64/UINT64) split lives in the **base** decode (`decode_tensor_tensor_arith` →
> `tensor_tensor_64bit_dispatch<VectorInt64/Uint64>`), and the extended body has **zero**
> relocations/calls to those dispatchers (§4 step 4, reloc-pinned). "Extended" is about the
> *dispatch escape*, not operand width. `[HIGH/OBSERVED — reloc-pinned no-64b reference]`

> **GOTCHA — extended-inst headers are flagged "not official architecture."** The
> `extended_utils.h` preamble states these structs are "examples of the use of the ExtendedInst
> opcode" and "may change or be deleted without notice." Treat the extended-TTA path as a
> real-but-narrow capability — `op` restricted to add/multiply, 2-D patterns, 128 active
> channels by default. The *full* ALU table (§3 of the base page) is exercised by the
> `0x41`/`0x51` path, not the extended one. `[HIGH/OBSERVED — header preamble + op comment]`

> **NOTE — honest reading of "restricted to add/multiply."** The wire `op` field is still
> *typed* `NEURON_ISA_TPB_ALU_OP` (the full 1-byte enum). The restriction is the header comment
> + the kernel switch only implementing the `ADD`(`0x04`)/`MULT`(`0x06`) arms; any other op
> falls to the shared switch-default string `"P%i: Error, Unimplemented tensor-tensor ALU
> op(0x%x)"` (§4, recovered from `libnrtucode_internal.so`). The IVP vocabulary in the body
> (§4) additionally exposes a multiply MAC chain, an abs-sub datapath, and an fp16 max — two
> honest readings of those (saturate/convert helpers inside add/mul, *or* a couple of extra
> arms) are flagged. **add + multiply** are the documented + IVP-supported core ops
> `[HIGH from header comment]`; that the device switch implements *exactly* those two arms is
> `[MED]` (the FLIX desync prevents a byte-exact jump-table recovery).

---

## 4. The algorithm — the extended elementwise loop

**Function span** (from `.xt.prop` records, OBSERVED): `0x010034b0 .. ~0x01003796`
(≈ `0x2e6` = 742 bytes; 20 prop records — `FUNC-START 0x2804 @0x010034b0`, code spans
`0x82`/`0xa2`/`0x92`, literal/data spans `0x08` at `0x363e`/`0x3778`/`0x3789`/`0x3796`). The
body is hand-scheduled FLIX VLIW with interleaved literal pools and **desyncs** under stock
`objdump` (~30–40 % `.byte`) — the documented carve limitation; flagged. Structure is HIGH
(struct + DEBUG strings + reloc pins); per-arm body is MED.

```c
// decode_extended_inst_tensor_tensor_arith(bool mode, unsigned idx)   // 0x010034b0
// [structure HIGH from struct+DEBUG+relocs; per-arm body MED — FLIX desync]
void decode_extended_inst_tensor_tensor_arith(bool mode, unsigned idx)
{
    // 1) load this kernel's .bss runtime state slot 0x02000468 (const16+R_XTENSA_SLOT0_* reloc,
    //    OBSERVED at body entry 0x010034b3 and again at exit ~0x0100379b — read at entry,
    //    written at exit). Shared with ExtendedInstCopy (spec 1) per pool-ext-0xf0.md §4.
    kstate_t *st = *(kstate_t **)0x02000468;

    // 2) read the EXTENDED_TTA wire struct from the SBUF-resident instruction word (§2):
    const NEURON_ISA_TPB_EXTENDED_TTA_STRUCT *s = sbuf_instr(idx);
    //   extended_opcode == 2 (already matched by the kernel_info_table key — re-read for the host bitmap)
    ext_completion_info_t ci = s->completion_info;       // has_read=1, has_write=1, num_active_ports
    Dtype  d0 = dtype_lo(s->in0_in1_dtype);              // src0 dtype (low nibble)
    Dtype  d1 = dtype_hi(s->in0_in1_dtype);              // src1 dtype (high nibble)
    Dtype  od = s->out_dtype;                            // dst dtype
    AluOp  op = s->op;                                   // restricted to ADD/MULT
    // 2-D element count = product over the TWO dims of src0 (vs the base's 3-D product):
    uint32_t n = (uint32_t)s->src0_mem_pattern.num_elem[0]
               * (uint32_t)s->src0_mem_pattern.num_elem[1];
    // DEBUG anchors (baked into the DEBUG build's dbg_dram, OBSERVED in libnrtucode_internal.so):
    //   "P%i: Decode : ExtendedInstTensorTensorArith"
    //   "P%i: ExtendedInstTensorTensorArith : num_tensor_elements = %d : alu_op = 0x%x"
    //  => the kernel materialises num_tensor_elements and switches on alu_op (prints it hex).

    // 3) channel/partition span from completion_info.num_active_ports (16 partitions/port;
    //    0 => 128 partitions = 8 ports). Each pool core partitions its share by get_cpu_id().
    unsigned partitions = ci.num_active_ports ? (ci.num_active_ports * 16) : 128;

    // 4) the ALU-op switch — NO 64-bit branch, NO callback (reloc-pinned, see below):
    switch (op) {
        case NEURON_ISA_TPB_ALU_OP_ADD:   // 0x04  — abs-sub / add datapath (ivp_babssub2nx8 …)
        case NEURON_ISA_TPB_ALU_OP_MULT:  // 0x06  — MAC chain (ivp_dmulq2n8xr8 / ivp_muluupan16xr16 /
                                          //          ivp_mulsun_2x16x32_0), with select/pack + converters
            for (unsigned p = first_partition(); p < partitions; p += POOL_LANES) {
                // strided-load src0/src1 by their TENSOR2D patterns (ivp_lat2nx8_xp / ivp_la2nx8_xp),
                // apply op across the 2-D dst iteration space, dtype-convert (converters enabled),
                // strided-store dst.
                ext_tta_inner(s, p, op, d0, d1, od);
            }
            break;
        default:
            // shared switch-default — same string the base 32-bit path uses:
            //   "P%i: Error, Unimplemented tensor-tensor ALU op(0x%x)"
            log_unimplemented_aluop(op);
            break;
    }

    // 5) write back runtime state to slot 0x02000468; return 0 via the trampoline.
    *(kstate_t **)0x02000468 = st;
}
```

**Recovered IVP vocabulary across the 742-byte body** (native `xtensa-elf-objdump`, OBSERVED —
the mnemonics survive the FLIX desync even where the per-arm scheduling does not):

| group | IVP intrinsics (recovered) |
|---|---|
| multiply | `ivp_dmulq2n8xr8` (8-bit dual MAC), `ivp_muluupan16xr16` (16-bit widening MAC), `ivp_mulsun_2x16x32_0` (signed/unsigned 16→32 MAC) |
| add / abs | `ivp_babssub2nx8` (`abs(src0−src1)`, the add/sub datapath) |
| max | `ivp_maxnxf16t` (fp16 max — clamp/saturate helper or a third arm; see §3 honest note) |
| select / pack | `ivp_dselnx16t`, `ivp_sel2nx8i`, `ivp_sel2nx8i_s4` (lane-select / predication / output pack) |
| load | `ivp_lat2nx8_xp`, `ivp_la2nx8_xp` (strided `2Nx8` vector loads for the 2-D patterns) |

This is a **subset** of the base IVP vocabulary (compare [tensor-tensor.md §4](tensor-tensor.md#4-per-op-ivp-lowering--recovered-vector-vocabulary)) —
consistent with the narrower op set. The 512-bit `2nx8`/`nx16`/`2x16x32` forms process a
partition's element vector per FLIX bundle. `[MED — IVP vocabulary recovered; per-op selection in the desynced switch reported structurally]`

> **NOTE — the dispatch is a C++ switch with an error default, not a table jump.** The
> string-anchored default `"P%i: Error, Unimplemented tensor-tensor ALU op(0x%x)"` (recovered
> from `libnrtucode_internal.so` at file off `0x269468`) is the *same* default the base 32-bit
> path uses — proving a compiled `switch (op)` with an error default, **not** a `.rodata`
> op→address jump table. `[HIGH/OBSERVED — string]`

> **GOTCHA — the extended body is self-contained: zero calls into the base worker or the
> 64-bit dispatchers.** The **only** relocations in the whole `0x010034b0` body target the
> `.bss` state slot `0x02000468`. There is **no** reloc/call to `tensor_tensor_arith_impl`
> (`0x01001280`), `setup_64bit_rw` (`0x01001108`), or any
> `tensor_tensor_64bit_dispatch<>` (`0x1f7c`/`0x2720`). The extended decode does **not**
> re-enter the base worker and has **no** i64/u64 split (§6). `[HIGH/OBSERVED — reloc-pinned]`

---

## 5. The dtype matrix — same enum, ≤32-bit effective reach

The `EXTENDED_TTA` carries `in0_in1_dtype` (`NEURON_ISA_TPB_DTYPE_PAIR`: `dtype_lo`=src0,
`dtype_hi`=src1; packed 4+4 bits) and `out_dtype` (`NEURON_ISA_TPB_DTYPE`). It uses the
**identical 16-value `NEURON_ISA_TPB_DTYPE` enum** as the base — pinned on the
[base page §2.1](tensor-tensor.md#21-dtype-encoding-neuron_isa_tpb_dtype-4-bit-commonh722):

| code | dtype | | code | dtype |
|---|---|---|---|---|
| `0x0` | `INVALID` | | `0x8` | `INT32` |
| `0x1` | `UINT64` | | `0x9` | `UINT32` |
| `0x2` | `INT8` | | `0xA` | `FP32` |
| `0x3` | `UINT8` | | `0xB` | `FP32R` |
| `0x4` | `INT16` | | `0xC` | `INT64` |
| `0x5` | `UINT16` | | `0xD` | `FP8_EXP3` |
| `0x6` | `BFLOAT16` | | `0xE` | `FP8_EXP4` |
| `0x7` | `FP16` | | `0xF` | `FP8_EXP5` |

`[HIGH/OBSERVED — common.h:722; CARRIED from tensor-tensor.md]`

The header type comment says *"Data converters **enabled**, specified by dtypes"* — so the
extended TTA **does** perform in/out dtype conversion (unlike `ExtendedInstCopy`, whose
comment disables converters). So the extension does **not** add new dtypes over the base; it
reuses the identical enum.

> **QUIRK — the effective dtype reach is ≤32-bit, even though the enum still contains
> i64/u64.** Although `NEURON_ISA_TPB_DTYPE` still defines `INT64`(`0xC`)/`UINT64`(`0x1`), the
> extended decode has **no 64-bit split path** (§4 step 4, reloc-pinned). The recovered IVP
> vocabulary tops out at 16→32-bit MACs (`ivp_mulsun_2x16x32`). So the *effective* dtype reach
> of the extended TTA is the ≤32-bit set
> `{fp8_e3/e4/e5, fp16, bf16, fp32 (+fp32r out), int8/16/32, uint8/16/32}`; **i64/u64 operands
> are not serviced by this kernel** — they are the base kernel's 64-bit dispatchers (§6).
> `[HIGH/OBSERVED — no-64b reloc + IVP vocabulary ceiling]`

---

## 6. Relation to the 64-bit path — siblings under different parents

**The 64-bit path is NOT a sub-case of the extended decode; it is a sub-case of the base
decode.** (The [tensor-tensor-64bit kernel page](tensor-tensor-64bit.md) — planned — owns the
64-bit synthesis in detail; this section pins only the *non-relation* to the extended op.)

* The 64-bit dispatchers are **base-kernel children**: `setup_64bit_rw(uint, ALU_OP)`
  (`0x01001108`), `tensor_tensor_64bit_dispatch<VectorInt64>` (`0x01001f7c`),
  `<VectorUint64>` (`0x01002720`), `tensor_tensor_64bit_bitvec_dispatch<VectorInt64>`
  (`0x01002c2c`), `<VectorUint64>` (`0x01002fc4`). They take `NEURON_ISA_TPB_ALU_OP` and are
  reached from `decode_tensor_tensor_arith` (`0x41`/`0x51`), which reads the `S3S3D3_TT` struct
  and branches `INT64`/`UINT64` operands to them. `[HIGH/OBSERVED — CARRIED from tensor-tensor.md §1.1/§5.1]`
* The **extended** decode (`0x010034b0`) has **no reloc/call** to any of those addresses
  (§4 step 4, reloc-pinned). So the extended TTA and the 64-bit dispatch are
  **siblings under different parents**:

  ```
  base  decode_tensor_tensor_arith (0x41/0x51)
        ├── ≤32-bit : tensor_tensor_arith_impl (0x01001280)
        └── 64-bit  : tensor_tensor_64bit_dispatch<VectorInt64/Uint64> (0x1f7c / 0x2720)

  ext   decode_extended_inst_tensor_tensor_arith (0xF0/spec 2)
        └── its OWN inline ≤32-bit add/mul loop (0x010034b0) — no 64-bit, no callback
  ```

* The 64-bit sibling therefore covers the **base** kernel's `INT64`/`UINT64` synthesis (no
  native 64-bit ALU on `ncore2gp`; 64-bit built from 32-bit halves via the
  `<VectorInt64>`/`<VectorUint64>` templates). The extended TTA neither uses nor is used by it.
  The two are independent.

---

## 7. Per-generation presence — gen-wide across POOL_PERF, absent in SUNDA

Every per-gen POOL EXTISA_0 image is embedded in the shipped host driver
`libnrtucode_internal.so` (the gpsimd customop-lib package), addressed by named getters
`<GEN>_Q7_POOL_<TAG>_EXTISA_0_SO_get.data`; `readelf -sW` pins the blob VMAs.

| GEN | tag | idx8 `0xF0`/spec2 funcVA | body VA | `.xt.prop` count | `decode_ext…` symbol |
|---|---|---|---|---|---|
| CAYMAN | POOL_PERF | `0x01003484` | `0x010034b0` | 21 | YES (`_Z40decode_extended_inst…bj`) |
| MARIANA | POOL_PERF | `0x010034a4` (+0x20) | `0x010034d0` (+0x20) | 21 | YES |
| MARIANA_PLUS | POOL_PERF | `0x010034a4` | `0x010034d0` | 21 | YES |
| MAVERICK | POOL_PERF | `0x0000393c` | (in trampoline) | 0 (stripped) | NO |
| SUNDA | POOL_RELEASE | **ABSENT** (not embedded) | — | — | NO |

`[HIGH/OBSERVED — kernel_info_table 0xF0/spec2 idx8 row + .xt.prop census per blob]`

The five `0xF0` rows are byte-identical in spec order `[0,1,2,4,3]` across all five gens; only
the funcVAs shift. The full idx8-band funcVA sets:
CAYMAN `0x01003370 / 0x01003380 / 0x01003484 / 0x010037a8 / 0x01003a60`;
MARIANA / MARIANA_PLUS `0x01003390 / 0x010033a0 / 0x010034a4 / 0x010037d8 / 0x01003a90`;
MAVERICK `0x00003824 / 0x00003840 / 0x0000393c / 0x00003c5c / 0x00003e58` (re-based `.text`
VMA `0x00000000`). `[HIGH/OBSERVED — re-carved blob byte scan]`

**(A) The four POOL_PERF gens all register `0xF0`/spec 2 EXTENDED_TTA.** The
`kernel_info_table` is **byte-identical in opcode/spec layout** across CAYMAN / MARIANA /
MARIANA_PLUS / MAVERICK — same 17 real entries, same five `0xF0` rows in the same registration
order (idx6 spec0 / idx7 spec1 / idx8 spec2 / idx9 spec4 / idx10 spec3). Only the funcVAs
shift by build delta: MARIANA == MARIANA_PLUS (idx8 `0x010034a4`, uniform +0x20 over CAYMAN's
`0x01003484` — pure layout delta, not a structural change); MAVERICK uses a **different `.text`
base VMA** (`0x00000000`, not `0x01000000`) and a leaner image, so its idx8 funcVA is
`0x0000393c` — the *same* `0xF0`/spec2 row, just re-based. The trampoline shape is invariant
across all four: idx8 funcVA lands on `entry a1,32` (`36 41 00`), loads the `.bss` state-slot
band, then `const16`+`callx8` to the body.

**(B) The named `.xt.prop` record** `_Z40decode_extended_inst_tensor_tensor_arithbj` is present
in CAYMAN / MARIANA / MARIANA_PLUS. **MAVERICK is fully stripped** (no `.xt.prop`, no
symbol/string table for kernels) — the function exists (table row + trampoline + body,
byte-shape-identical) but the symbolic name is gone. MAVERICK presence is therefore
established **structurally** (idx8 `0xF0`/spec2 row + matching trampoline), not by symbol.

**(C) SUNDA does NOT ship the `0xF0` ExtendedInst dispatch in this driver copy.** In
`libnrtucode_internal.so` the SUNDA EXTISA getter `SUNDA_Q7_POOL_RELEASE_EXTISA_0_SO_get` (and
its `_JSON_get` sibling) are **weak undefined** symbols (value 0, size 0, not linked into this
internal `.so`). SUNDA ships a **POOL_RELEASE** build, not POOL_PERF, and the POOL images it
*does* embed here (`SUNDA_Q7_POOL_RELEASE_IRAM/DRAM/EXTRAM`) are **raw loadable-memory dumps,
not Xtensa ELF objects** — they carry no `\x7fELF` magic and no 8-byte `kernel_info_table`
structure, so there is **no `0xF0`/spec row to find** (any `00 00 ?? f0` byte hit inside them
points at junk funcVAs). `[HIGH/OBSERVED — readelf weak-UND + non-ELF raw dumps]`

So from *this* binary, SUNDA's absence of an embedded EXTISA `kernel_info_table` is OBSERVED;
the stronger reading — that SUNDA **re-tables** its POOL dispatch and reaches elementwise add
through the **base** tensor-tensor opcode `0x41` (with a `decode_pool`-style re-architecture
that also dropped the `0xF0` escape) — is the cross-page POOL-dispatch finding, carried at its
original confidence (it cannot be re-grounded against a structured table in this driver copy,
because SUNDA's POOL image here is a raw dump). What is **firmly OBSERVED here**: SUNDA has **no
`decode_extended_inst_tensor_tensor_arith` symbol** and **no embedded `0xF0` EXTISA table** in
this driver. `[OBSERVED no-EXTISA-table / MED-CARRIED for the "base 0x41 only" routing]`

**(D) Header (wire-format) presence is BROADER than firmware presence.** The `EXTENDED_TTA`
**struct block** is **byte-identical** (md5 `dca0c816…` over the 14-line typedef region) across
**all four** arch-isa header sets — cayman, mariana, maverick **and** sunda. (The *whole*
`extended_utils.h` files differ per gen — md5s `99853d22…`/`867705e7…`/`1431da5d…`/`7ce59859…`
— but the TTA struct region is identical; the divergence is elsewhere in the file.)
MARIANA_PLUS reuses MARIANA's headers (no separate arch_isa dir). So the wire **struct is
arch-uniform even where the device dispatch is absent**: SUNDA's header still *defines*
`NEURON_ISA_TPB_EXTENDED_TTA_STRUCT` (and the `op restricted to add/multiply` comment), but
SUNDA's firmware never registers a kernel for it. The struct is a stable ABI; the kernel is
per-gen. `[HIGH/OBSERVED — struct-region md5]`

> **CORRECTION — the struct-region md5 is `dca0c816…`, not the file md5.** A whole-file md5 of
> `extended_utils.h` differs across gens (`99853d22…` etc.), which would *falsely* suggest the
> struct diverges. Hashing the **14-line `EXTENDED_TTA` typedef block** shows it is
> byte-identical (`dca0c816…`) in all four. Re-ground any "struct identical across gens" claim
> to the *struct region*, not the file.

**PER-GEN VERDICT.** `decode_extended_inst_tensor_tensor_arith` (`0xF0`/spec 2 `EXTENDED_TTA`)
is present **gen-wide across the POOL_PERF family** — CAYMAN, MARIANA, MARIANA_PLUS, MAVERICK —
with a byte-identical table/opcode/spec layout and an invariant trampoline shape (only
build-delta funcVAs + MAVERICK's re-based/stripped image). It is **absent in SUNDA**
(POOL_RELEASE, re-tabled dispatch, `0xF0` escape removed). The wire struct itself ships in
every header set including SUNDA's. There is **no MARIANA_PLUS-specific addition** — MARIANA
already carries it byte-for-byte identically to MARIANA_PLUS; the op predates the
MARIANA→MARIANA_PLUS step.

---

## 8. Reimplementation checklist & honest limitations

A Vision-Q7-compatible extended-TTA path must:

1. **Register `0xF0`/spec 2 in the `kernel_info_table`** as `key = 0xF0<<24 | 2<<16` →
   `funcVA`, alongside the other four `0xF0` rows (specs `0,1,4,3`); one linear key-scan
   resolves it. **Do not special-case `0xF0`.** `[HIGH]`
2. **Decode the 64-byte `EXTENDED_TTA` wire format** field-exact: `extended_opcode@12`,
   `completion_info@13` (`{has_read:1, _:1, has_write:1, num_active_ports:3, _:2}`),
   `in0_in1_dtype@14` (packed `dtype_lo:4`/`dtype_hi:4`), `out_dtype@15`, `op@16`,
   `reserved0[3]@17`, three `TENSOR2D@20/32/44` (`{ADDR4, int16 step[2], uint16 num[2]}`),
   `reserved1[8]@56`. `sizeof == 64`, compile-asserted. `[HIGH]`
3. **Derive the active span from `num_active_ports`** (`0 ⇒ 128 partitions`; `1..7 ⇒ ×16`),
   one port = one Q7 starting at q7[0]. There is **no** `num_active_channels` field. `[HIGH]`
4. **Compute `num_tensor_elements` as the 2-D product** `src0.num_elem[0] * num_elem[1]`
   (not a 3-D product). `[HIGH]`
5. **Implement only the add/multiply arms**; route any other `op` to a switch-default error
   (the string-anchored `"Unimplemented tensor-tensor ALU op(0x%x)"`). Converters are
   **enabled** — perform in/out dtype conversion per the dtype fields. `[HIGH op set / MED exact arm set]`
6. **Do NOT split i64/u64 here** and do **not** call into the base 32-bit worker or the 64-bit
   dispatchers — the body is self-contained and only touches its `.bss` state slot
   (`0x02000468`). 64-bit operands are the base kernel's job. `[HIGH]`

**Honest limitations.** The body is FLIX VLIW with interleaved literal pools that desync under
stock `objdump` on ~30–40 % of bundles; recovered & reported are the `entry a1,32` prologue
(HIGH), the `.xt.prop` function start (HIGH/EXACT), the `.bss` reloc (HIGH), the IVP vocabulary
(MED), the DEBUG-string anchors (HIGH), and the operand struct (HIGH, compile-asserted). The
exact ALU-op arm set is `[HIGH add+mul from the header comment + IVP vocabulary / MED whether
`max`/`abssub` are distinct user-visible arms or internal saturate/convert helpers]` — the FLIX
desync prevents a byte-exact jump-table recovery. The per-arm micro-schedule is MED. v5
(MAVERICK) presence is structural (re-based/stripped image), not symbol-pinned.

---

## Related pages

* [Tensor-Tensor Elementwise Arith (`0x41`/`0x51`, `S3S3D3_TT`)](tensor-tensor.md) — the
  **BASE** op; pins the 60-entry `NEURON_ISA_TPB_ALU_OP` table, the op-class predicates, and
  the 64-bit split this page frames the delta against.
* [POOL Extended-Opcode (`0xF0`) Dispatch](../pool/pool-ext-0xf0.md) — the five-row
  `kernel_info_table` sub-dispatch that routes `0xF0`/spec 2 here.
* [Tensor-Tensor 64-bit](tensor-tensor-64bit.md) *(planned)* — the `INT64`/`UINT64` synthesis
  this page proves is a **base**-kernel sibling, not part of the extended decode.
* [ALU-Op matrix](alu-op-matrix.md) *(planned)* — the full per-op × per-dtype support cells.
* [Host `nrtucode_opset_add_instruction` / `0xF0` spec registration](../../runtime/nrtucode-opset.md)
  *(planned)* — where the host reads `extended_opcode@12` (gated on opcode `0xF0`) into the
  spec presence bitmap.
* [Scalar-Tensor-Tensor (`S2S2D2_STT`, `0x9d`/`0x9e`)](scalar-tensor-tensor.md) — the fused
  variant that *does* carry a second op selector (the extended TTA does not).
