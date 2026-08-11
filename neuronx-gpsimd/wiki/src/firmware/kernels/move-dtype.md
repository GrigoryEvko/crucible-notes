# Move (general-purpose register move + dtype gate)

The **`MOVE`** instruction — opcode `NEURON_ISA_TPB_OPCODE_MOVE = 0xa7` — is the GPSIMD
**SEQ control-engine general-purpose register move**: it copies **1..8 full 32-bit register
words** (or **1..8 immediates**) into 1..8 destination registers, **without touching any
tensor, SBUF, or PSUM memory**. It is the scalar-register sibling of the POOL data-engine
`Copy`/`Cast` element-wise ops ([Cast and Copy](cast-copy.md)) — not a funnel of them, not a
shape move — and its dtype field is a **hard three-value whitelist** `{UINT32, INT32, FP32}`,
the three 32-bit full-register interpretations, enforced by a single decode-time predicate.

This is a **host-decoder page**. `MOVE`'s decode and dtype gate live in the shipped host
x86-64 ucode decoder `libnrtucode_internal.so` (the C++ source file `move.cpp`, compiled in,
**not** stripped) and in the shipped arch-isa C headers — **not** in a hand-scheduled Xtensa
FLIX body. So no device disassembly is needed: the facts come from (a) the host decoder's
`.rodata` assert string and embedded firmware-image `.data` blobs, and (b) the
`aws_neuron_isa_tpb_ctrl_mv.h` header predicate, **compile-verified with gcc**.
Everything below is re-grounded against the binary `libnrtucode_internal.so`
(sha256 `b7c67e89…632fc329b`) and the four arch-isa header trees under
`extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/…/c10/{lib,include}` (gitignored —
reach with `fd --no-ignore` or absolute paths).

Confidence tags per [the Confidence & Walls model](../../reference/confidence-model.md):
`[HIGH/OBSERVED]` = read-from-byte / compiled / proven; `[MED/INFERRED]` = reasoned over
OBSERVED; `[…/CARRIED]` = re-used at a sibling report's confidence without re-reading the
artifact.

---

## 0. TL;DR — the move in seven facts

1. **`MOVE` is a SEQ control instruction, opcode `0xa7`, struct `NEURON_ISA_TPB_CTRL_MV_STRUCT`
   (64 B).** It sits in the sequencer control band `0xa0..0xbf` adjacent to `ALU_OP (0xa8)`
   and `COMPARE_BRANCH (0xa9)` — the `R[]` register ISA — **not** the POOL tensor band where
   `Copy (0x46)` / `Cast (0x47)` live. `[HIGH/OBSERVED — common.h:255, instruction_mapping.json:30]`
2. **It moves whole 32-bit register words, never tensors.** The `ctrl_mv.h` header states it
   verbatim: *"This is the general purpose register move instruction. It does not use tensors
   or access SBUF/PSUM memory."*
3. **The dtype field is a hard 3-value whitelist `{UINT32(0x9), INT32(0x8), FP32(0xA)}`** —
   the three 4-byte (full-register) dtypes only. A **single** dtype byte (offset 13) is shared
   by source and destination, so `MOVE` **cannot convert**.
4. **The gate `ctrl_mv_has_valid_dtype` is byte-for-byte the `move.cpp:41` assert.** The host
   decoder's `.rodata` carries the assert string in **59 copies** (one per gen × engine × build
   firmware image that embeds the `MOVE` decoder).
5. **Within the gate the move is a raw 32-bit word copy** — no convert, no sign-extend, no
   width branch. All three admitted dtypes hit the *identical* path; the dtype only records the
   word's interpretation and gates legality.
6. **Two source modes only:** `move_source ∈ {REGISTER=0, IMMEDIATE=1}` (offset 14) — the sole
   runtime branch. Confirmed by the two DEBUG print forms `S: R[%d] = R[%d] = 0x%x` (reg) and
   `S: R[%d] = imm = 0x%x` (imm).
7. **Universal across engines and generations.** The `MOVE` decoder is embedded in the
   firmware DRAM of **all five** GPSIMD engines (ACT, DVE, PE, POOL, SP) on every gen; the
   `CTRL_MV` **struct body and dtype gate are byte-identical** across
   SUNDA/CAYMAN/MARIANA/MAVERICK. (One **per-gen header nuance** in the validity *contract* —
   see the [CORRECTION](#71-per-gen--the-op-and-its-gate) in §7.)

---

## 1. The string and symbol anchors

All anchors below are read directly from `libnrtucode_internal.so`.

### 1.1 The `move.cpp:41` assert (the dtype gate, in host `.rodata` and embedded firmware)

```text
$ strings libnrtucode_internal.so | rg -c 'move.cpp:41'
59
$ strings libnrtucode_internal.so | rg 'move.cpp:41' | head -1
/opt/workspace/NeuronUcode/src/decode/move.cpp:41
  ((ins.dtype == NEURON_ISA_TPB_DTYPE_UINT32) ||
   (ins.dtype == NEURON_ISA_TPB_DTYPE_INT32)  ||
   (ins.dtype == NEURON_ISA_TPB_DTYPE_FP32))
  && "highest priority is full-register moves. TODO other dtypes"
```

The decoder source path is `…/NeuronUcode/src/decode/move.cpp`; the assert is at **line 41**.
The 59 copies are not 59 distinct asserts — the decoder body is **inlined into each embedded
per-(gen × engine × build) firmware image's decode table**, so the same assert string is baked
into every image that carries the `MOVE` leg (§7.2). `[HIGH/OBSERVED]`

> **NOTE — assert vs. release gate.** The `move.cpp:41` line is a DEBUG-build *developer
> assert* (the *"highest priority … TODO other dtypes"* comment marks it as such). The
> identical `{UINT32, INT32, FP32}` restriction is **also** enforced in release builds by the
> `ctrl_mv_has_valid_dtype` *validity predicate* that gates the instruction at decode time
> (§3.3) — and the assert string is present in `SUNDA_*_RELEASE_DRAM` images too. The dtype
> restriction therefore holds in **both** build flavors; the predicate is build-independent.

### 1.2 The device DEBUG self-name and the two print forms

The DEBUG firmware images print the decoded `MOVE`. The print roster sits in the SEQ
instruction-name table **in opcode order**, between `ActivationReadAccumulator` and the
`MoveShape` prints:

```text
$ strings libnrtucode_internal.so | rg '^S: '
S: ActivationReadAccumulator
S: MOVE
S: R[%d] = R[%d] = 0x%x      <== move_source == REGISTER  (reg -> reg)
S: R[%d] = imm = 0x%x        <== move_source == IMMEDIATE (imm -> reg)
S: MoveShape(Immediate)      <== the SEPARATE op 0xb2 (§5.2)
S: MoveShape(RegToShape)
S: MoveShape(ShapeToReg)
```

So the disassembler prints `MOVE`, then **per destination register** either a
register-source line or an immediate-source line — exactly the `move_source`
`REGISTER`/`IMMEDIATE` split (§4.2).

### 1.3 The C-ABI surface: the decoder is internal, not an exported symbol

```text
$ nm -C --defined-only libnrtucode_internal.so | rg -ic 'move'
0
```

The library exports only thin C wrappers (`nrtucode_get_hwdecode_table`,
`nrtucode_opset_add_instruction`, `nrtucode_core_print_logs`, …); **there is no exported
`move` symbol.** The `MOVE` decode is a static/inlined body **inside the embedded firmware
images** — the host `.text` is a wrapper that `lea`'s the firmware `.data` blobs (e.g.
`MAVERICK_NX_POOL_TEST_IRAM_get.data`). `MOVE` is decoded *inside* those images, not as a
host-lib function.

> **GOTCHA — this is why the host lib, not the device Xtensa, is authoritative here.** Unlike
> the POOL data kernels (`Copy`/`Cast` — device Q7 `.xt.prop` bodies), `MOVE` is a *SEQ control*
> instruction handled by the *ucode decoder*, whose C++ (`move.cpp`) + dtype assert compile into
> the **host** x86-64 decoder. That same lib also *embeds* the per-arch × per-engine device
> firmware as `.data` blobs, and the `move.cpp:41` assert + `S: MOVE` self-name are baked into
> those images. So the gate is readable from **both** the host `.rodata` and the embedded device
> firmware, cross-confirmed by the header predicate. A device-side Xtensa walk would add nothing:
> the gate is a *decode-time* check, and the device execution is a trivial regfile write.

---

## 2. The opcode and struct binding

### 2.1 Opcode — the SEQ control band

`MOVE = 0xa7` lives in the sequencer control-instruction band `0xa0..0xbf`, **not** the data
band. Read verbatim from `aws_neuron_isa_tpb_common.h` (all four gens identical):

| code | opcode | band role |
|------|--------|-----------|
| `0xa0` | `EVENT_SEMAPHORE` | SEQ control |
| `0xa1` | `HALT` | SEQ control |
| `0xa2` | `DRAIN` | SEQ control |
| `0xa3` | `INSTRUCTION_FLUSH` | SEQ control |
| `0xa4` | `NOP` | SEQ control |
| `0xa5` | `WRITE` | SEQ control |
| `0xa6` | `NOTIFY` | SEQ control |
| **`0xa7`** | **`MOVE`** | **SEQ register move (this page)** |
| `0xa8` | `ALU_OP` | SEQ scalar evaluator → [ALU-op matrix](alu-op-matrix.md) |
| `0xa9` | `COMPARE_BRANCH` | SEQ control flow |
| `0xb2` | `MOVE_SHAPE` | SEQ shape-descriptor move (§5.2) |
| `0xb3` | `POLL_SEM` | SEQ control |

`MOVE` is a **sequencer register-file instruction**, adjacent to `ALU_OP` (the scalar
evaluator) and `COMPARE_BRANCH`. It is in the `R[]` register ISA — *worlds away* from the POOL
tensor ISA where `COPY (0x46)` and `CAST (0x47)` sit. `[HIGH/OBSERVED — common.h:248–263]`

### 2.2 Struct binding — `instruction_mapping.json`

```json
"NEURON_ISA_TPB_CTRL_MV_STRUCT": [ "NEURON_ISA_TPB_OPCODE_MOVE" ],
"NEURON_ISA_TPB_CTRL_MS_STRUCT": [ "NEURON_ISA_TPB_OPCODE_MOVE_SHAPE" ]
```

The `CTRL_*` family is the SEQ control-engine struct set: `CTRL_MV` = move, `CTRL_MS` =
move-shape, `CTRL_AL` = alu, `CTRL_NO` = no-operand, etc. The `0xa7 → CTRL_MV` and
`0xb2 → CTRL_MS` bindings are **byte-identical** across all four gens'
`instruction_mapping.json`. `[HIGH/OBSERVED — mapping JSON lines 27–31]`

---

## 3. The operand struct — `NEURON_ISA_TPB_CTRL_MV_STRUCT` (64 B, compile-verified)

The struct is **compile-verified** with gcc against the maverick header (the
`ISA_STATIC_ASSERT(==64)` is in the header itself):

```c
typedef struct NEURON_ISA_TPB_CTRL_MV_STRUCT {
    NEURON_ISA_TPB_HEADER         header;            //  4  ( 0 -  3)  opcode=0xa7, inst_word_len, …
    NEURON_ISA_TPB_EVENTS         events;            //  8  ( 4 - 11)  wait/update semaphore sync
    uint8_t                       num_mov;           //  1  (12     )  1..8 moves this instruction
    NEURON_ISA_TPB_DTYPE          dtype;             //  1  (13     )  THE gate: {UINT32,INT32,FP32}
    NEURON_ISA_TPB_DATA_SRC       move_source;       //  1  (14     )  REGISTER(0) | IMMEDIATE(1)
    uint8_t                       reserved0[1];      //  1  (15     )  MUST be 0
    NEURON_ISA_TPB_REG_NUM        src_registers[8];  //  8  (16 - 23)  up to 8 source reg specifiers
    NEURON_ISA_TPB_REG_NUM        dst_registers[8];  //  8  (24 - 31)  up to 8 dest reg specifiers
    NEURON_ISA_TPB_MOVE_IMMEDIATE immediate;         // 32  (32 - 63)  32-byte full-reg immediate union
} NEURON_ISA_TPB_CTRL_MV_STRUCT;
ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_CTRL_MV_STRUCT) == 64, "…NOT 64B.");
```

```text
$ gcc -std=c11 -I<maverick-isa-inc> verify_mv.c -o verify_mv && ./verify_mv
sizeof(CTRL_MV)=64
num_mov@12 dtype@13 move_source@14 reserved0@15 src@16 dst@24 imm@32
sizeof(MOVE_IMMEDIATE)=32 sizeof(DTYPE)=1 sizeof(DATA_SRC)=1 sizeof(REG_NUM)=1
OPCODE_MOVE=0xa7 OPCODE_MOVE_SHAPE=0xb2 INT32=0x8 UINT32=0x9 FP32=0xa REG=0 IMM=1
```

Every offset, the `64`-byte total, the `0xa7` opcode, the three dtype ordinals, and the
`REG=0`/`IMM=1` source codes reproduce exactly. `NEURON_ISA_TPB_REG_NUM` is a `uint8_t`.
`[HIGH/OBSERVED]`

### 3.1 The dtype field — a single shared byte, src == dst

The header is explicit (`ctrl_mv.h`): *"There is a single dtype field, both source and
destination must be the same types."* So `MOVE` — unlike `Cast`, which carries two dtype bytes
(`in_dtype@32`, `out_dtype@33`, which **may differ**) — has exactly **one** dtype byte. The
single field is what makes conversion impossible: there is no second type to convert *to*. The
dtype selects the 32-bit interpretation for the disassembler and the legality gate, **not** a
datapath.

### 3.2 `NEURON_ISA_TPB_MOVE_IMMEDIATE` — the 32-byte (8×u32) immediate union

```c
typedef union NEURON_ISA_TPB_MOVE_IMMEDIATE {   // common.h:1131
    uint8_t  uint8[32];   int8_t  int8[32];
    uint16_t uint16[16];  int16_t int16[16];
    uint32_t uint32[8];   int32_t int32[8];
    uint16_t fp16[16];    // stored as u16
    float    fp32[8];
} NEURON_ISA_TPB_MOVE_IMMEDIATE;                 // sizeof = 32  [compile-verified]
```

The union is **32 bytes = 8 × u32 = the full register-array width**: with `num_mov ≤ 8`
destinations × 32 bits each, the op moves up to **256 bits** (8 full register words) in one
instruction. `[HIGH/INFERRED]`

> **QUIRK — the immediate union *encodes* narrow views the decoder *rejects*.** The union
> exposes `uint8[32]`, `uint16[16]`, and `fp16[16]` views, but the `move.cpp:41` gate **admits
> only** the `uint32`/`int32`/`fp32` views. The narrow views are the *encoding surface* for the
> *"TODO other dtypes"* — the sub-register (8/16-bit) element moves that were **never
> implemented** (§4.4). The union is wider than the implemented subset on purpose; the decoder
> is the narrower reality.

### 3.3 The validity predicate — `is_valid_ctrl_mv`

The full validity contract, transcribed verbatim from the header's predicate group:

```c
// is_valid_ctrl_mv(i) =
//      has_valid_neuron_header(i)
//   && has_valid_neuron_events(i)
//   && has_tile_idx_zero(i)                  // MAVERICK only — see §7 CORRECTION
//   && has_move_opcode(i)                    // opcode == 0xa7
//   && ctrl_mv_valid_num(i)                  // 0 < num_mov <= 8
//   && ctrl_mv_reserved_zero(i)              // reserved0[0] == 0
//   && ctrl_mv_has_valid_dtype(i)            // <== THE DTYPE GATE
//   && is_valid_enum(DataSrc, move_source)   // move_source in {REGISTER, IMMEDIATE}
//   && ctrl_mv_valid_register(i, 0..7)       // per-slot register legality
//
// ctrl_mv_has_valid_dtype(i) =
//      (i.ctrl_mv.dtype == Dtype::FP32)
//   || (i.ctrl_mv.dtype == Dtype::UINT32)
//   || (i.ctrl_mv.dtype == Dtype::INT32)     // <-- byte-for-byte the move.cpp:41 assert
//
// ctrl_mv_valid_register(i, idx) =
//      ( num_mov <= idx && src_registers[idx] == 0 )            // unused slot must be 0
//   || ( is_valid_register_read(src_registers[idx])
//        && is_valid_register_write(dst_registers[idx]) )
```

`ctrl_mv_has_valid_dtype` is **byte-for-byte** the `move.cpp:41` host assert — the same three
clauses in the same order. Two further header **NOTES** constrain the register lists:
*"Each destination register must be unique (no duplicates)"* and *"No register used in the
destination register list can be used in source register list."* `[HIGH/OBSERVED — all four gens]`

**Register specifiers** (`ctrl_mv.h`, *"Registers"* section):

| specifier | meaning | access |
|-----------|---------|--------|
| `0–63` | general-purpose 32-bit registers | Read / Write |
| `128` | low 32 bits of the current instruction's Neuron address (PC) | Read-only (write ignored) |
| `129` | high 32 bits of the current PC | Read-only (write ignored) |
| all others | **illegal** | — |

> **NOTE — `MOVE` can snapshot the PC.** Register specifiers `128`/`129` read the low/high
> halves of the current instruction's Neuron address into a GP register — a PC-capture path
> with no equivalent in `Copy`/`Cast`. Writes to `128`/`129` are silently ignored.

---

## 4. The dtype handling — a raw 32-bit word, not a per-dtype element-wise op

### 4.1 The decode is byte-identical for all three admitted dtypes

The header is explicit: *"for uint32, int32, fp32, the full 32 bits will be placed into the
destination register."* `MOVE` copies a **complete 32-bit register word verbatim**.
`UINT32` / `INT32` / `FP32` produce the **same bit-for-bit move** — no convert, no
sign-extend, no rounding. The dtype is **purely** the legality gate plus the disassembler's
print interpretation. This is the dtype-agnostic raw-word path in its purest form, **but
gated to the 32-bit band** ([the dtype model](dtype-model.md) §6). The host-side decode is the
following — note the dtype never touches the copy, only the gate:

```c
// move.cpp decode (reconstructed from the host x86 decoder + ctrl_mv.h predicate)
// Symbols: NEURON_ISA_TPB_CTRL_MV_STRUCT, ctrl_mv_has_valid_dtype,
//          NEURON_ISA_TPB_DATA_SRC_{REGISTER,IMMEDIATE}, NEURON_ISA_TPB_MOVE_IMMEDIATE
bool decode_move(const NEURON_ISA_TPB_CTRL_MV_STRUCT *ins, uint32_t R[/*64+PC*/]) {
    // ---- decode-time legality gate (is_valid_ctrl_mv) ----
    if (!(ins->num_mov > 0 && ins->num_mov <= 8))   return false;  // ctrl_mv_valid_num
    if (ins->reserved0[0] != 0)                      return false;  // ctrl_mv_reserved_zero
    // ctrl_mv_has_valid_dtype  ==  the move.cpp:41 assert, verbatim:
    if (!(ins->dtype == NEURON_ISA_TPB_DTYPE_UINT32 ||
          ins->dtype == NEURON_ISA_TPB_DTYPE_INT32  ||
          ins->dtype == NEURON_ISA_TPB_DTYPE_FP32))  return false;  // <== THE GATE
    if (!(ins->move_source == NEURON_ISA_TPB_DATA_SRC_REGISTER ||
          ins->move_source == NEURON_ISA_TPB_DATA_SRC_IMMEDIATE)) return false;

    // ---- execute: a RAW 32-bit WORD copy, dtype-agnostic within the gate ----
    for (uint8_t k = 0; k < ins->num_mov; ++k) {
        // dst/src register legality checked per-slot by ctrl_mv_valid_register(k)
        if (ins->move_source == NEURON_ISA_TPB_DATA_SRC_REGISTER)
            R[ins->dst_registers[k]] = R[ins->src_registers[k]];     // "S: R[%d] = R[%d] = 0x%x"
        else /* IMMEDIATE */
            R[ins->dst_registers[k]] = ins->immediate.uint32[k];     // "S: R[%d] = imm = 0x%x"
        // ^ NOTE: the same word-copy regardless of dtype; FP32/UINT32/INT32 are identical here.
    }
    return true;
}
```

`[HIGH/OBSERVED gate; INFERRED loop body]`

### 4.2 The two source modes — the only runtime branch

| `move_source` | code | behavior (per `k < num_mov`) | print form |
|---------------|------|------------------------------|------------|
| `REGISTER` | `0` | `dst[k] = R[src_registers[k]]` | `S: R[%d] = R[%d] = 0x%x` |
| `IMMEDIATE` | `1` | `dst[k] = immediate.uint32[k]` | `S: R[%d] = imm = 0x%x` |

This is the **single** runtime decision in the whole instruction.

### 4.3 The per-dtype support table — a 3-row 32-bit whitelist

Every dtype **outside** `{UINT32, INT32, FP32}` is rejected at `ctrl_mv_has_valid_dtype`
*before* execution. The table is the complement of the three-clause gate:

| dtype | code | accepted? | handling / reason |
|-------|------|-----------|-------------------|
| `INT32` | `0x8` | **Y** | raw 32-bit word copy (full register) — admitted (32-bit) |
| `UINT32` | `0x9` | **Y** | raw 32-bit word copy (full register) — admitted (32-bit) |
| `FP32` | `0xA` | **Y** | raw 32-bit word copy (full register) — admitted (32-bit) |
| `INT8` / `UINT8` | `0x2`/`0x3` | — | rejected — the *"TODO other dtypes"* sub-register moves, **not** implemented |
| `INT16` / `UINT16` | `0x4`/`0x5` | — | rejected — *"TODO"* sub-register moves, **not** implemented |
| `BFLOAT16` | `0x6` | — | rejected — sub-32-bit fp, **not** implemented |
| `FP16` | `0x7` | — | rejected — sub-32-bit fp, **not** implemented |
| `FP32R` | `0xB` | — | rejected — the round-mode/partial fp32, **not** a register dtype |
| `INT64` / `UINT64` | `0xC`/`0x1` | — | rejected — 64-bit exceeds the 32-bit register word |
| `FP8_E3/E4/E5` | `0xD`/`0xE`/`0xF` | — | rejected — sub-32-bit fp |
| MAVERICK MX (`FP4`, `FP8_E2`, `INT4`, `SFP8`, `CPTC`) | `0x10`+ | — | rejected — **MX does not widen the gate** |

The per-dtype "support" reduces to: the **32-bit band is fully (and identically)
implemented**; sub-32-bit is a baked TODO; 64-bit, `FP32R`, and all MX are gate-rejected. This
is *narrower* than `Cast` (12 in-dtypes) and the *opposite* of `Cast`'s per-dtype convert —
but *one dtype wider* than the sequence-bounds gate, which admits only `{INT32, FP32}`
(`MOVE` adds `UINT32`).

### 4.4 "Full-register move" and the "TODO other dtypes"

The SEQ register file is **32-bit-word addressed** — GP registers `0–63` are "32-bit
registers" (`ctrl_mv.h`). A *full-register move* copies a complete 32-bit register word. The
assert's *"highest priority is full-register moves. TODO other dtypes"* therefore means: the
**implemented** path is the full-32-bit-word move (the three 32-bit dtypes); the
**unimplemented** (`TODO`) path would be **sub-register-width (8/16-bit element) moves** —
which the `MOVE_IMMEDIATE` union's `uint8[32]`/`uint16[16]` views already *encode* but the
decoder *rejects*. There is exactly **one** move width — 32 bits — × `num_mov` elements; there
is no element-size branch. `[HIGH gate + union views; INFERRED TODO reading]`

> **GOTCHA — `MOVE` does *not* widen on MAVERICK.** The MAVERICK MX dtype wave
> (`FP4`, `FP8_E2`, `INT4`, `SFP8`, `CPTC1..7`) does **not** appear in `MOVE`'s gate — the
> whitelist is the same three 32-bit codes on v5 as on v2. A would-be `MOVE` of an MX value
> dies at `ctrl_mv_has_valid_dtype`. `[HIGH/OBSERVED]`

---

## 5. Disambiguation — `MOVE` vs. `Cast`/`Copy` vs. `MoveShape`

The GPSIMD ISA carries **three** distinct "move" concepts in **different engines on different
structs**. Conflating them is the single most likely error here; the binary makes them
crisply separable.

### 5.1 `MOVE (0xa7)` vs. `Copy (0x46)` / `Cast (0x47)` — register-granular vs. element-granular

| aspect | `MOVE` (`0xa7`, this page) | `Copy` (`0x46`) | `Cast` (`0x47`) |
|--------|---------------------------|-----------------|-----------------|
| engine | **SEQ control** (`R[]` regfile) | **POOL data** engine (Q7) | **POOL data** engine (Q7) |
| struct | `CTRL_MV` (64 B) | `S4D4_TR` (64 B) | `S4D4_TR` (64 B) |
| operates on | 32-bit **REGISTERS** (no memory) | 4-D **TENSOR** in SBUF | 4-D **TENSOR** in SBUF |
| granularity | full 32-bit **register word** | per-**ELEMENT** (dtype width) | per-**ELEMENT** |
| dtype fields | 1 (src == dst, `@13`) | 2 (`in@32`, `out@33`; in == out) | 2 (`in@32`, `out@33`; may differ) |
| dtype gate | `{UINT32, INT32, FP32}` | 12 in-dtypes (== out) | 12 in \| 13 out |
| conversion | **NONE** (raw word copy) | NONE (bit-accurate move) | **YES** (in → FP32 → out) |
| datapath | regfile word copy | `ivp_dsel` lane move | `ivp_float`/`trunc` + `dsel` |
| memory access | **none** (regs only) | SBUF read + write | SBUF read + write |
| decode home | host ucode `move.cpp` | device Q7 `.xt.prop` | device Q7 `.xt.prop` |

The resolution of the central framing — *"register-granular move vs. element-granular copy"* —
is **YES, they are different ops in different engines**:

- `MOVE` is the **register-granular** move: it copies whole 32-bit **SEQ register words** (loop
  counters, addresses, the PC, scalar immediates) inside the sequencer's scalar register file.
- `Copy` is the **element-granular** move: it copies a 4-D **tensor** of data elements (at the
  dtype's element width) through SBUF in the POOL data engine.

They share the *word* "move" and the *bypass / no-convert* idea, but operate on entirely
different state in different engines. **There is no funnel:** a tensor `Copy` does **not**
invoke `MOVE`, and `MOVE` never touches a tensor. See [Cast and Copy](cast-copy.md) for the
element-wise convert sibling. `[HIGH/OBSERVED — common.h:183–184]`

### 5.2 `MoveShape (0xb2)` — a third "move", dtype-less, on `CTRL_MS`

The strings also surface `move_shape.cpp` and the three `S: MoveShape(…)` prints. This is a
**separate** opcode `MOVE_SHAPE = 0xb2`, struct `NEURON_ISA_TPB_CTRL_MS_STRUCT` (also 64 B),
that moves **tensor-shape descriptors** — not data, not registers-as-data — between
shape-registers, GP registers, and immediates. Its struct, read verbatim:

```c
typedef struct NEURON_ISA_TPB_CTRL_MS_STRUCT {
    NEURON_ISA_TPB_HEADER                 header;          //  4  ( 0 -  3)
    NEURON_ISA_TPB_EVENTS                 events;          //  8  ( 4 - 11)
    NEURON_ISA_TPB_TENSOR_SHAPE_MOVE_TYPE move_type;       //  1  (12     )  Immediate(0)|RegToShape(1)|ShapeToReg(2)
    uint8_t                               num_move;        //  1  (13     )  number of tensor_shapes to move
    NEURON_ISA_TPB_SHAPE_REG_TYPE         shape_reg_type;  //  1  (14     )
    uint8_t                               reserved0;       //  1  (15     )
    uint8_t                               shape_registers[4]; // 4 (16 - 19)
    uint8_t                               reserved1[12];   // 12  (20 - 31)
    NEURON_ISA_TPB_TENSOR_SHAPE_DATA      data;            // 32  (32 - 63)  immediate data or general registers
} NEURON_ISA_TPB_CTRL_MS_STRUCT;                            // sizeof = 64  [ISA_STATIC_ASSERT]
```

`MoveShape` has **no dtype field at all** (shapes are not typed data). Its variant key is
`move_type ∈ {IMMEDIATE(0x0), REG_TO_SHAPE(0x1), SHAPE_TO_REG(0x2)}` — the three
`S: MoveShape(Immediate/RegToShape/ShapeToReg)` prints — plus `shape_reg_type`. It is **out of
scope** for dtype handling, documented here only to disambiguate the three "move" strings.

> **CORRECTION — offset 13 collides between `MOVE` and `MoveShape`.** Both `CTRL_MV` and
> `CTRL_MS` put a one-byte field at **offset 13**, but it means **different things**: in
> `CTRL_MV` byte 13 is the **`dtype`** (the gate); in `CTRL_MS` byte 13 is **`num_move`** (a
> count). And the byte at **offset 12** is `num_mov` in `CTRL_MV` but `move_type` in `CTRL_MS`.
> A decoder that disambiguates these two by struct offset alone — without first reading the
> **opcode** (`0xa7` vs `0xb2`) — will mis-interpret both. **Dispatch on the opcode first.**
> `[HIGH/OBSERVED]`

**Three "moves", three engines, three structs:**
`move.cpp`/`MOVE (0xa7)`/`CTRL_MV` = register data move (this page);
`move_shape.cpp`/`MOVE_SHAPE (0xb2)`/`CTRL_MS` = shape-descriptor move;
`Copy`/`Cast (0x46/0x47)`/`S4D4_TR` = tensor data move (POOL engine).

---

## 6. Reconciliation — the dtype-dispatch model and the ALU-op neighbor

### 6.1 `MOVE` is the degenerate case of the global dtype-dispatch model

[The dtype model](dtype-model.md) frames the global pattern as: *the operand struct carries
1–2 dtype bytes; the decoder gates them; then dispatches on `(op, dtype-width, signedness)`*.
`MOVE` is the **degenerate** case: it carries **1** dtype byte, gates it to the 32-bit band,
then does **no** width/signedness dispatch at all — all three admitted dtypes hit the same
raw-32-bit-word path. So `MOVE` is the dtype-agnostic raw-word baseline **within** the 32-bit
band, but it is **not** a universal dtype-agnostic copy: it *rejects every non-32-bit dtype*.
It is the **scalar-register analogue** of the bit-accurate tensor `Copy` — one altitude below
the tensor datapath.

> **NOTE — "non-32-bit dtype legs" are a deliberate non-feature, not unrecovered code.** A
> reader could expect `move.cpp`'s narrow-dtype handling to be a *hidden datapath* still to be
> recovered. It is not: the narrow legs are the **explicit `"TODO other dtypes"`** — they were
> *never written*. The `MOVE_IMMEDIATE` union *encodes* them (the `uint8`/`uint16` views), the
> decoder *rejects* them at the gate. There is nothing more to recover; the gap is a baked,
> characterized TODO.

### 6.2 `MOVE (0xa7)` vs. `ALU_OP (0xa8)` — adjacent, not the same

`MOVE` is the opcode **immediately before** `ALU_OP` in the SEQ control band, and both operate
the SEQ `R[]` register file. But `MOVE` is **not** an `ALU_OP`: it does **not** go through the
`alu_op.cpp` arithmetic switch (no add/sub/and/shift), and where `ALU_OP` dispatches on
`(op, dtype-width, signedness)` to a native Xtensa leaf, `MOVE` is a pure register write with a
32-bit gate. They share the register file and the SEQ engine; the dispatch is independent. See
[the ALU-op datapath + dtype matrix](alu-op-matrix.md) for the arithmetic neighbor.

---

## 7. Per-gen + per-engine presence

### 7.1 Per-gen — the op and its gate

| gen | NC | `OPCODE_MOVE` | `ctrl_mv.h` | dtype gate | `MOVE_SHAPE 0xb2` |
|-----|-----|---------------|-------------|------------|-------------------|
| SUNDA | v2 | `0xa7` | present | `{FP32, UINT32, INT32}` | present |
| CAYMAN | v3 | `0xa7` | present | `{FP32, UINT32, INT32}` | present |
| MARIANA | v4 | `0xa7` | present | `{FP32, UINT32, INT32}` | present |
| MAVERICK | v5 | `0xa7` | present | `{FP32, UINT32, INT32}` | present |

The `CTRL_MV` **struct body** is **byte-identical** SUNDA ↔ MAVERICK
(`diff` of the `typedef struct` block = IDENTICAL on all four), and the dtype gate is the same
three-value whitelist on every gen. There is **no per-gen `MOVE` change** to the operand or the
gate. (TONGA / V1 is not in this package's arch-isa set; out of scope.)
`[HIGH/OBSERVED — all four gens]`

> **CORRECTION — "byte-identical header" overstates it; the struct + gate are identical, the
> validity *contract* grew one clause on v5.** A `diff` of the **whole** `ctrl_mv.h` file
> SUNDA ↔ MAVERICK is **not** empty — it differs in exactly **two** lines: (1) the copyright
> comment (`ISA header for NC-v2` vs `NC-v5`), and (2) MAVERICK's `is_valid_ctrl_mv` contract
> adds a `has_tile_idx_zero(i)` clause that SUNDA / CAYMAN / MARIANA omit
> (`rg -c has_tile_idx_zero` = 0/0/0/**1**). The **executable surface** — the 64-byte struct
> layout, the dtype gate, the opcode, the `instruction_mapping` binding — is identical across
> all four gens; only the v5 validity *contract* additionally requires the instruction's tile
> index to be zero. Prefer "struct + dtype gate byte-identical across gens" over "header
> byte-identical". `[HIGH/OBSERVED]`

### 7.2 Per-engine — the decoder is in every engine's firmware DRAM

Mapping each `move.cpp:41` assert-string offset to its owning `*_get.data` firmware blob
(blob-owner bisection over the `nm` blob table), the assert — i.e. the `MOVE` decoder leg — is
present in:

| gen | engines carrying the `MOVE` decoder | builds |
|-----|-------------------------------------|--------|
| SUNDA | ACT, DVE, PE, POOL, SP | `RELEASE_DRAM` (1) |
| CAYMAN | ACT, DVE, PE, POOL, SP | `{DEBUG, PERF, TEST}_DRAM` (×3) |
| MARIANA | ACT, DVE, PE, POOL, SP | `{DEBUG, PERF, TEST}_DRAM` (×3) |
| MARIANA_PLUS | ACT, DVE, PE, POOL, SP | `{DEBUG, PERF, TEST}_DRAM` (×3) |
| MAVERICK | DVE, PE, POOL, SP (partial image set carved) | `{DEBUG/PERF/TEST}_IRAM` (partial) |

`MOVE` is the **universal SEQ control-engine register move** — its decoder is baked into the
firmware of **all five** GPSIMD engines (Activation, DVE/Vector, PE/Matmul, POOL, SP/Sync) on
every gen. It is shared infrastructure, not a per-engine kernel. The `S: MOVE` DEBUG self-name
appears in the 16 DEBUG-build images.

---

## 8. Limitations and flags

- **No FLIX-desync here.** `MOVE`'s gate and decode are **not** in a hand-scheduled Xtensa
  FLIX body. The authoritative facts come from (a) the compile-verified `ctrl_mv.h` predicate
  and (b) the host x86-64 decoder's clean-ASCII `.rodata` assert. This is a *higher* bar than a
  desynced device body. `[flagged — header + host-decoder-authoritative.]`
- **The device execution body was not disassembled.** The actual Xtensa regfile write is a
  trivial 32-bit register copy with no dtype branch (the gate is a decode-time check), so a
  device-body walk would add nothing to the dtype model. The `for k < num_mov` loop in §4.1 is
  INFERRED-HIGH, not byte-walked. `[flagged.]`
- **`TODO other dtypes` == sub-register (8/16-bit) element moves.** The *existence* of the TODO
  is OBSERVED (the assert comment + the `MOVE_IMMEDIATE` `uint8[32]`/`uint16[16]` views). That
  it specifically means sub-32-bit element moves (not, e.g., 64-bit) is **INFERRED-HIGH** from
  the *"the full 32 bits will be placed"* line + the union's narrower views + 64-bit exceeding
  the register word. `[INFERRED-HIGH — flagged.]`
- **No host / NKI marshalling surface.** `MOVE` is an internal ucode/sequencer instruction (like
  `GetSequenceBounds`), not a custom-op kernel; there is no `at::Tensor` host API, and the
  `c10::ScalarType` bridge is irrelevant (its three dtypes map trivially to `Int`/`Float`;
  `UINT32` is unmapped in the pre-FP8 `c10`, but `MOVE` is not an `at::Tensor` op). `[flagged.]`

---

## See also

- [Cast and Copy](cast-copy.md) — the element-wise convert sibling: `Copy (0x46)` /
  `Cast (0x47)`, POOL data engine, `S4D4_TR` tensor struct (the *element-granular* move).
- [The Unified Datatype Model](dtype-model.md) — the `NEURON_ISA_TPB_DTYPE` enum, the dtype
  ordinals (`INT32 0x8` / `UINT32 0x9` / `FP32 0xA`), the FP32 convert hub, and the global
  dtype-dispatch model `MOVE` is the degenerate case of.
- [The ALU-Op Datapath + Dtype Matrix](alu-op-matrix.md) — the adjacent SEQ opcode `0xa8`, the
  scalar arithmetic evaluator over the same `R[]` register file.
- [The Confidence & Walls model](../../reference/confidence-model.md) — the
  `[HIGH/OBSERVED]` / `[MED/INFERRED]` / `[…/CARRIED]` tag semantics.
