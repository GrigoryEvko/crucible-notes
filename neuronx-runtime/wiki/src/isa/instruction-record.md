# The 64-Byte Instruction Record Format

> *All addresses, offsets, and enum values on this page apply to `libnrt.so` from `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce` (runtime-lib **2.31.24.0-0b044f4ce**; `libnrt.so.2.31.24.0`, ELF64, **not stripped**, DWARF present, build-id `8bb57aba…`). `.text` VMA equals file offset for the cited ranges. Struct offsets are read from DWARF (`structures.json`), enum values from DWARF (`enums.json`), and the encode/decode bodies from the disassembly. Other versions will differ.*

## Abstract

Every instruction the Neuron sequencer fetches — on every engine, for every NeuronCore generation V2/V3/V4 — is a **fixed 64-byte record**. `TPB_INST_NBYTES` is `0x40`; the instruction stream is a flat array of these records, walked in `+64` strides with no length prefix, no alignment padding, and no variable-width encoding. This is the runtime-side analog of a classic fixed-width VLIW/RISC instruction word: where a conventional ISA packs a 32- or 64-*bit* word, the TPB ISA packs a 64-*byte* word, because each record must carry a full tensor descriptor (3-D start/step/count), an inter-engine synchronization event tuple, immediates, and a multi-byte opcode payload in one atomic fetch. Contrast the libtpu `BarnaCore` 32-byte bundle, which is a true bit-packed VLIW filled field-by-field by a `BitCopy(dst_bitoff, …)` primitive: the TPB record is **byte-granular and struct-shaped** — a tagged union whose first four bytes are a `NEURON_ISA_TPB_HEADER` and whose remaining 60 bytes are one of 118 distinct C structs selected by the opcode byte. There is no bit-packer; fields are plain `uint8`/`uint16`/`uint32` members at fixed byte offsets, written by the compiler and read back by `memcpy`-shaped accessors.

The record is a discriminated union. Byte `[0]` is the `opcode`; the remaining 63 bytes are reinterpreted as the matching `NEURON_ISA_TPB_<OP>_STRUCT` (e.g. `CTRL_AL_STRUCT` for an ALU op, `DMA_DIRECT2D_STRUCT` for a 2-D DMA, `PSEUDO_FUNCTION_CALL_STRUCT` for a call). All 118 arms share a 12-byte preamble — a 4-byte header (`opcode`, `inst_word_len`, `debug_cmd`, `debug_hint`) and an 8-byte `EVENTS` tuple (`wait_mode`/`wait_idx`/`update_mode`/`update_idx`/`semaphore_value`) — and then diverge into per-opcode payload from byte `[12]` onward. The opcode space splits cleanly: `0x01..0xFF` real (hardware-executed) opcodes, and a `0xC1..0xDF` **pseudo-opcode** band that the host-side lowering pass ([Pseudo-Instruction Lowering](pseudo-instruction-lowering.md)) rewrites into real records before the record ever reaches the device. Two records never leave the host: `PSEUDO_FUNCTION_BEGIN` (`0xD1`) splits the stream into functions, and `PSEUDO_INST` (`0xDF`) with sub-opcode `1` is the EXIT_EXECUTION sentinel that terminates a program.

This page documents three artifacts a reimplementer must reproduce: (1) the **64-byte union layout** — the shared 12-byte preamble plus the per-opcode payload, pinned to byte offsets from DWARF; (2) the **opcode taxonomy** — the 70 real + 50 pseudo opcode values and the field-group dimensions that vary across opcode classes, given as axes rather than a 118-row dump; and (3) the **instruction-block I/O algorithms** — the three `ib_*` workhorses (`ib_read_write`, `ib_has_pseudo_exit`, `ib_final_inst_validity_check`) that copy, scan, and validate a record stream as it is staged from host into device HBM. The per-opcode *legality* rules are owned by the [ISA Validator Architecture](validator-architecture.md); the per-arch deltas by the [Per-Arch Validators](validators-per-arch.md) page; the host-to-real lowering of the pseudo band by [Pseudo-Instruction Lowering](pseudo-instruction-lowering.md).

For reimplementation, the contract is:

- **The fixed-width model**: `TPB_INST_NBYTES = 0x40`; the stream is `record[i]` at byte `i*64`; every walker uses a `+0x40` stride and `byte[0]` as the discriminant. There is no framing byte and no length field in the stream itself.
- **The shared 12-byte preamble**: `NEURON_ISA_TPB_HEADER` (4 B at `+0`) + `NEURON_ISA_TPB_EVENTS` (8 B at `+4`), identical across all 118 union arms.
- **The opcode→struct dispatch**: `opcode` (byte `+0`) selects one of 118 `NEURON_ISA_TPB_<OP>_STRUCT` reinterpretations of bytes `+12..+63`.
- **The pseudo band**: `0xC1..0xDF` (plus `0xA9` COMPARE_BRANCH, `0xB5`/`0xDD` branch-hint) is host-only; `0xD1` FUNCTION_BEGIN and `0xDF`/sub-1 EXIT are stream sentinels.
- **The `ib_*` I/O contract**: a record stream is copied to/from device dmem segments by `ib_read_write` (chunked, segment-split via `ib_translate_addr`), scanned for the EXIT sentinel by `ib_has_pseudo_exit`, and final-validated by `ib_final_inst_validity_check`.

| | |
|---|---|
| **Record width** | 64 bytes / `TPB_INST_NBYTES = 0x40` (every union arm; every engine; V2/V3/V4) |
| **Discriminant** | `opcode` = byte `[0]` (`NEURON_ISA_TPB_OPCODE`, `uint8`) |
| **Shared preamble** | `HEADER` (4 B `@+0`) + `EVENTS` (8 B `@+4`) = 12 bytes, all arms |
| **Union arms** | 118 distinct 64-byte `NEURON_ISA_TPB_<OP>_STRUCT` types (DWARF) |
| **Real opcodes** | 70 values in `0x01..0xFF` (`MATMUL=0x02`, `ACTIVATE=0x21`, `TENSOR_SCALAR_ARITH_OP=0x43`, …) |
| **Pseudo opcodes** | 50 values; host-only band `0xC1..0xDF` + `0xA9`/`0xB5`/`0xDD` |
| **Stream walker** | `ib_has_pseudo_exit` `@0x323df0` — `+0x40` stride, `byte[0]==0xDF && byte[0x0C]==1` |
| **Stream I/O** | `ib_read_write` `@0x323cc0` (284 B) — dmem copy in/out, segment-split |
| **Final validity** | `ib_final_inst_validity_check` `@0x323cb0` — 5-byte jmp thunk into arch vtable |
| **Source TUs** | `tdrv/instruction_block.c` (EIB I/O); `aws_neuron_isa_tpb_{enums,assert,util}.h` (ISA spec) |

---

## 1. The Record Layout

### Purpose

The 64-byte record is the single physical unit the entire model-load and execute path manipulates. The NEFF carries it, `kelf2kbin` emits it, the lowering pass rewrites it, the validator checks it, `ib_read_write` stages it into HBM, and the sequencer fetches it. Reading every downstream offset table requires first internalizing the shared shape: bytes `[0..11]` are opcode-independent; bytes `[12..63]` are opcode-dependent. A reimplementer who treats the record as opaque bytes will mis-place every field; one who internalizes the 12-byte preamble and the opcode→struct table can decode any record.

### Encoding

The record is a C union whose every arm begins with the same 12-byte preamble. The preamble is two structs:

```text
NEURON_ISA_TPB record  (64 bytes; TPB_INST_NBYTES = 0x40)

byte: 0      1            2          3        4 ........... 11   12 ......................... 63
     +------+------------+----------+--------+------------------+------------------------------+
     |opcode|inst_word_len|debug_cmd|dbg_hint|   EVENTS (8 B)   |   per-opcode payload (52 B)  |
     | [0]  |    [1]      |   [2]   |  [3]   |   wait/update     |   reinterpreted by opcode    |
     +------+------------+----------+--------+------------------+------------------------------+
       \________ HEADER (4 B @+0) ________/  \__ EVENTS @+4 __/  \____ union arm @+12 _________/
```

#### `NEURON_ISA_TPB_HEADER` (4 bytes `@+0`)

| Offset | Width | Field | Meaning |
|---|---|---|---|
| `+0` | 1 | `opcode` (`NEURON_ISA_TPB_OPCODE`) | the union discriminant; selects the byte-`+12` payload struct |
| `+1` | 1 | `inst_word_len` | record length in units the sequencer fetches; the stream stride is always `0x40` bytes regardless |
| `+2` | 1 | `debug_cmd` (`NEURON_ISA_TPB_DEBUG_CMD_LEVELS`) | profiling marker: `NONE=0`, `NOTIFY=1`, `START=3`, `END=5`, `START_END=7`, `INS_FIELD=9` |
| `+3` | 1 | `debug_hint` | per-instruction debug/trace hint byte (extended by `debug_hint_ext` in some payloads) |

#### `NEURON_ISA_TPB_EVENTS` (8 bytes `@+4`)

The synchronization tuple every instruction carries. It is how the TPB ISA expresses inter-engine ordering inline in each record rather than via separate barrier instructions — a wait predicate and an update action both ride in the 8 bytes.

| Offset | Width | Field | Meaning |
|---|---|---|---|
| `+4` | 1 | `wait_mode` (`NEURON_ISA_TPB_WAIT_MODE`) | pre-execute predicate: `NONE=0`, `SEM_EQ/LT/LE/GT/GE_IMM=1..5`, `EVT_SET=6`, `EVT_SET_THEN_CLEAR=7`, `EVT_CLEAR=14`, `EVT_CLEAR_THEN_SET=15`, plus a `*_REG` family (register-sourced compare) |
| `+5` | 1 | `wait_idx` | semaphore/event index the wait predicate reads |
| `+6` | 1 | `update_mode` (`NEURON_ISA_TPB_UPDATE_MODE`) | post-execute action: `NONE=0`, `EVT_SET/CLR_READ=1/2`, `SEM_INC/DEC_READ=3/4`, `SEM_ADD/SUB/WR_IMM_READ=5/7/9`, and the matching `*_COMPLETE` set at `+16` (`17..25`), plus `*_REG_*` variants |
| `+7` | 1 | `update_idx` | semaphore/event index the update action writes |
| `+8` | 4 | `semaphore_value` (`uint32`) | immediate operand for the wait compare / update arithmetic |

> **NOTE —** `EVENTS` has an extended 12-byte sibling `NEURON_ISA_TPB_EVENTS_EXTENDED` that splits `semaphore_value` into separate `sem_wait_value` (`+4`) and `sem_update_value` (`+8`). The 8-byte form is the one embedded at `+4` in the standard 64-byte record; the extended form appears in records that need independent wait/update immediates. A reimplementer decoding a record reads the 8-byte form unless the opcode struct declares the extended one.

### The per-opcode payload (`+12..+63`)

Bytes `+12` onward are reinterpreted by the opcode. There are 118 distinct 64-byte arm structs in DWARF; their first 12 bytes are always `HEADER`+`EVENTS`, so they differ only in the 52-byte payload. The payload regions recur across families:

| Payload region | Typical offset(s) | Carried by | Role |
|---|---|---|---|
| ALU op / dtype quad | `+12..+15` | `CTRL_AL`, `S3D3_TS`, `CTRL_MV` | `op`, `dtype`, `src_datasrc`, `dst_dtype` selectors |
| Register operands | `+16..+21` (×6) | `CTRL_AL` (`src0_lo/hi`,`src1_lo/hi`,`dst_lo/hi`) | scalar register file selectors (`REG_NUM`) |
| Tensor descriptor (3-D) | `+16..+31` and `+48..+63` | `S3D3_TS`, `S3_LW`, matmul | `TENSOR3D` = `ADDR4 start_addr` + `int16[3] step_elem` + `uint16[3] num_elem` |
| 8-byte address operands | `+16`, `+40` | `DMA_DIRECT2D` (`src/dst_start_addr`) | `ADDR8` with register/immediate marker form |
| Immediates | `+24` (8 B) / `+40`,`+44` (4 B) / `+32` (32 B) | `CTRL_AL`, `S3D3_TS`, `CTRL_MV` | per-op literal operands |
| Function metadata | `+12..+50` | `PSEUDO_FUNCTION_*` | name, return-address regs, reset-semaphore flag, args-table var id |

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `NEURON_ISA_TPB_HEADER` | DWARF struct (4 B) | shared opcode/len/debug preamble | CERTAIN |
| `NEURON_ISA_TPB_EVENTS` | DWARF struct (8 B) | shared wait/update sync tuple | CERTAIN |
| 118 × `NEURON_ISA_TPB_<OP>_STRUCT` | DWARF structs (64 B each) | per-opcode union arms | CERTAIN |
| `NEURON_ISA_TPB_OPCODE` | DWARF enum | the byte-`[0]` discriminant value space | CERTAIN |

> **GOTCHA —** `inst_word_len` (byte `+1`) is **not** the stream stride. Every record occupies exactly 64 bytes in the stream and every walker (`ib_has_pseudo_exit`, the lowering splitter, the validator) advances by `0x40`. `inst_word_len` is a sequencer-fetch hint, not a record size; a reimplementer that strides by `inst_word_len` will desynchronize the stream immediately.

---

## 2. The Opcode Taxonomy

### Purpose

The opcode byte at `[0]` is the discriminant for the entire union. There are 120 defined opcode values across two bands: a **real** band the hardware executes, and a **pseudo** band the host lowers away. Rather than dump all 118 arm structs, this section gives the opcode value space and the field-group dimensions that vary across opcode classes — the axes a reimplementer reconstructs the per-opcode layout from.

### Encoding

The real opcodes cluster by engine/datapath in value ranges; the pseudo opcodes occupy a contiguous `0xC1..0xDF` band plus three control-flow stragglers.

#### Real opcode value clusters (70 values, abbreviated by range)

| Value range | Engine / datapath class | Representative opcodes |
|---|---|---|
| `0x01..0x0A` | PE (matmul array) | `LDWEIGHTS=0x01`, `MATMUL=0x02`, `MATMUL_SPARSE=0x07`, `MATMUL_MX=0x0A` |
| `0x21..0x30` | ACT (activation) | `ACTIVATE=0x21`, `ACTIVATE_QUANTIZE=0x22`, `ACTIVATION_TABLE_LOAD=0x23`, `EXPONENTIAL=0x30` |
| `0x41..0x5F` | POOL / tensor-arith | `TENSOR_TENSOR_ARITH_OP=0x41`, `TENSOR_SCALAR_ARITH_OP=0x43`, `POOL=0x45`, `COPY=0x46`, `CAST=0x47`, `RNG=0x4D`, bitvec variants `0x51..0x5F` |
| `0x60..0x7F` | POOL / DVE (batch-norm, gather, shuffle) | `BATCH_NORM_STATS=0x60`, `GATHER=0x68`, `STREAM_TRANSPOSE=0x6B`, `IOTA=0x7E`, `DROPOUT=0x7F` |
| `0xA1..0xBF` | SP / control | `HALT=0xA1`, `DRAIN=0xA2`, `NOP=0xA4`, `NOTIFY=0xA6`, `COMPARE_BRANCH=0xA9`, `DMAMEMCPY=0xB8`, `DMA_INDIRECT=0xBB` |
| `0x81` | JPEG decode | `JPEG_DECODE=0x81` (host-lowered form `PSEUDO_JPEG_DECODE=0xD7`) |
| `0xE0..0xFF` | extended / DMA-gather | `SPARSITY_COMPRESS=0xE0`, `QUANTIZE_MX=0xE3`, `INDIRECT_COPY=0xE7`, `EXTENDED_INST=0xF0`, `INVALID=0xFF` (terminal) |

> **CORRECTION (ISA-IR-2) —** an earlier revision of this cluster table placed `JPEG_DECODE=0xFF` in the `0xE0..0xFF` row alongside `INVALID=0xFF`. That value was wrong. The authoritative DWARF `NEURON_ISA_TPB_OPCODE` enum (1-byte underlying type) gives **`JPEG_DECODE=0x81`**, its host-lowered CC-top form **`PSEUDO_JPEG_DECODE=0xD7`**, and **`0xFF`=`INVALID`** (the terminal sentinel only). `0xFF` is *not* JPEG decode; a reimplementer keying the JPEG-decode path off `0xFF` would never match the real opcode and would collide with the INVALID sentinel. This matches the [ISA overview sibling page](overview.md) (`PSEUDO_JPEG_DECODE 0xD7`).

#### Pseudo opcode band (host-only; lowered before device fetch)

| Value | Pseudo opcode | Lowered to |
|---|---|---|
| `0xA9` | `COMPARE_BRANCH` | real branch record (`ib_process_special_instr` 0xA9 path) |
| `0xCC` | `PSEUDO_BRANCH_LABEL` | resolved to a PC; `num_extra_instrs=-1` (deleted) |
| `0xCF` | `PSEUDO_DMASWAP_QUEUE_SET` | `psqs_insert_qsi_swap` drain/reset/swap/drain sequence |
| `0xD1` | `PSEUDO_FUNCTION_BEGIN` | stream split sentinel — never reaches device |
| `0xD2` | `PSEUDO_FUNCTION_RETURN` | drain + sync-barrier + `add_br_reg` to return address |
| `0xD3` | `PSEUDO_FUNCTION_CALL` | TOPSP block-switch + args-table-swap + return-addr save |
| `0xDD` | `PSEUDO_BRANCH_PREFETCH_HINT` | branch-hint offset record |
| `0xDF` | `PSEUDO_INST` | extension prefix; sub-opcode at `+12` (`1`=EXIT_EXECUTION, `2`=LIBRARY_RELOAD_INDEX) |

`PSEUDO_INST` (`0xDF`) is an **extension prefix**: byte `+12` is a `NEURON_ISA_TPB_PSEUDO_OPCODE` (`INVALID=0`, `PSEUDO_EXIT_EXECUTION=1`, `PSEUDO_LIBRARY_RELOAD_INDEX=2`) that sub-dispatches the record. This two-level encoding is why `ib_has_pseudo_exit` checks **both** `byte[0]==0xDF` and `byte[12]==1`.

> **CORRECTION (TDRV-CORE-15) —** an earlier note labeled opcode `0xDF` simply "exit". It is the `PSEUDO_INST` extension prefix; the EXIT sentinel is specifically `PSEUDO_INST` with `pseudo_opcode==1` (`PSEUDO_EXIT_EXECUTION`, the `NEURON_ISA_TPB_PSEUDO_EXIT_EXECUTION_STRUCT` whose `pseudo_opcode` field sits at `+12`). A scanner that keys on `byte[0]==0xDF` alone will false-positive on the LIBRARY_RELOAD_INDEX sub-form (`pseudo_opcode==2`).

### The field-group dimension table

The 118 arm structs are not 118 independent layouts — they are combinations of a small set of payload field-groups. A reimplementer reconstructs any arm from these axes plus the shared preamble:

| Axis (field group) | Values | Source |
|---|---|---|
| Preamble | always `HEADER`@+0 + `EVENTS`@+4 (12 B) | every arm (DWARF) |
| ALU/dtype selectors | `{op, dtype, src_datasrc, dst_dtype}` at `+12..+15` | `CTRL_AL`, `S3D3_TS` arms |
| Register operand count | 0 / 6 (`CTRL_AL`) / 16 (`CTRL_MV` src+dst ×8) | per-arm `REG_NUM` members |
| Tensor descriptor count | 0 / 1 (`S3_LW`) / 2 (`S3D3_TS` src+dst `TENSOR3D`) | `TENSOR3D` members at `+16`/`+48` |
| Address operand width | `ADDR4` (4 B, tensor start) / `ADDR8` (8 B, DMA) | `start_addr` member type |
| Immediate footprint | 0 / 4 B / 8 B / 32 B | `IMM_VAL_INST_FIELD`/`ALU_IMMEDIATE`/`MOVE_IMMEDIATE` |
| Bound-check regs | 0 / 2 (`src_bound_reg`,`dst_bound_reg`, DMA) | `BOUND_CHECK_REG` members |
| Pseudo sub-opcode | present only for `0xDF` (`pseudo_opcode`@+12) | `PSEUDO_OPCODE` enum |

### Worked arm layouts

Four representative arms, pinned to DWARF offsets, show the dimension combinations:

```text
CTRL_AL (ALU op; opcode in CTRL band)               DMA_DIRECT2D (opcode 0xB.. 2-D DMA)
 +0  HEADER (4)                                       +0  HEADER (4)
 +4  EVENTS (8)                                       +4  EVENTS (8)
 +12 op / dtype / src_datasrc / dst_dtype (4×1)       +12 dma_configs / semaphore / sem_increment / compute_op
 +16 src0_lo/hi src1_lo/hi dst_lo/hi (6× REG_NUM)     +16 src_start_addr (ADDR8, 8)
 +22 reserved1 (2)                                    +24 src_step_elem (int32[2]) / +32 src_num_elem (u16[2])
 +24 immediate (ALU_IMMEDIATE, 8)                     +36 src_elem_size / +38 src_bound_reg / +39 dst_bound_reg
 +32 reserved2 (32)                                   +40 dst_start_addr (ADDR8, 8) / +48 dst_step_elem ...
                                                      +62 in_dtype / +63 out_dtype

S3D3_TS (tensor-scalar; two TENSOR3D + 2 imms)        PSEUDO_FUNCTION_CALL (opcode 0xD3, host-only)
 +0  HEADER (4) / +4 EVENTS (8)                        +0  HEADER (4) / +4 EVENTS (8)
 +12 accumulator_cmd / reserved0 (3)                   +12 function_name (int8[36])
 +16 src_mem_pattern (TENSOR3D, 16)                    +48 args_table_var_id (uint32)
 +32 in_dtype / out_dtype / num_active_channels        +52 reserved0 (12)
 +35 imm0_src / op0 / op1 / reverse_operands / imm1_src
 +40 imm0 (4) / +44 imm1 (4)                          PSEUDO_FUNCTION_BEGIN (opcode 0xD1, host-only)
 +48 dst_mem_pattern (TENSOR3D, 16)                    +12 function_name (int8[36])
                                                       +48 return_reset_semaphores / +49 return_addr_reg_lo
                                                       +50 return_addr_reg_hi / +51 reserved0 (13)
```

> **QUIRK —** the `PSEUDO_FUNCTION_BEGIN` record stores `return_addr_reg_lo` at `+49` and `return_addr_reg_hi` at `+50`, and `return_reset_semaphores` at `+48`. The lowering pass reads exactly these three bytes to build the function's call/return frame (`itf_identify_functions`); a reimplementer of the lowering pass must read them at these offsets, not from a separate metadata table. The 36-byte `function_name` at `+12` is the hash-table key.

---

## 3. The Operand Sub-Structures

### Purpose

The payload field-groups are themselves small structs reused across arms. Pinning their byte layout lets a reimplementer decode any tensor descriptor, address operand, or register field without re-deriving it per opcode.

### Encoding

| Struct | Size | Layout | Used by |
|---|---|---|---|
| `TENSOR3D` | 16 B | `ADDR4 start_addr`@+0; `int16[3] step_elem`@+4; `uint16[3] num_elem`@+10 | matmul, load-weights, tensor-scalar |
| `ADDR4` | 4 B | union: `{regnum@+0, reserved0[2], marker@+3}` (register form) **or** `uint32 addr_immediate` (29-bit ISA byte address, mask `0x1FFFFFFF`) | tensor `start_addr` |
| `ADDR8` | 8 B | union: `{reg_lo@+0, reg_hi@+1, reserved0[5], marker@+7}` (register form) **or** immediate table-offset form | DMA `src/dst_start_addr` |
| `IMM_REG` | 4 B | `regnum@+0`; `reserved0[3]@+1` (must be zero for well-formed) | immediate-register operands |
| `BOUND_CHECK_REG` | 1 B | bitfield: `bc_reg:6`, `bc_disable_oob_error_notif:1`, `bc_enabled:1` | DMA bound checks |
| `EVENTS` | 8 B | `wait_mode/wait_idx/update_mode/update_idx`@+0..3; `semaphore_value`@+4 | preamble, all arms |

#### The `ADDR4`/`ADDR8` marker discipline

An address operand is either an immediate (a 29-bit ISA byte address in the low bits of the `uint32`/`uint64`) or register-sourced, distinguished by the `marker` byte at the top of the operand:

- `ADDR4.marker & 0xA0 == 0x80` → register addressing (the `regnum` field is a live register selector).
- `ADDR4.marker & 0x60 == 0x20` → indirect addressing (a `data_addr` field participates).
- Immediate form: `addr_immediate & 0x1FFFFFFF` is the 29-bit address; quadrant bits `& 0x1E000000` select the SBUF/PSUM aperture.

> **NOTE —** the 29-bit ISA address space is *encoding* address, not physical SRAM address. Its quadrant apertures (q0 `0..0x37FFF`, q1 base `0x800000`, q2 `0x1000000`, q3 `0x1800000`, each `0x38000` wide; PSUM `0x2000000..0x2203FFF`) are decoded by the validator's `addresses_in_same_sbuf_quadrant` / `tpb_addr_active_channels` predicates. The ISA-address-to-physical mapping is done elsewhere (the per-arch device geometry); these apertures are partition-relative and arch-independent at the record level. See [Per-Arch Validators](validators-per-arch.md).

---

## 4. Instruction-Block I/O: `ib_read_write`

### Purpose

Once the host has built a record stream for an engine, the stream must be copied into device-resident dmem (HBM/TDRAM) so the sequencer can fetch it — and copied back out for inspection. `ib_read_write` is the bidirectional workhorse: one function handles both directions, picking the dmem copy primitive by a read/write flag and splitting each transfer at dmem-segment boundaries via `ib_translate_addr`.

### Entry Point

```text
kbl_instruction_stream_read / kbl_instruction_stream_write
  └─ ib_read_write (0x323cc0, 284 B)
       ├─ ib_translate_addr   (0x323ca0, neighbour cell)  ── map linear offset → (dmem, off)
       ├─ dmem_buf_copyout    (read direction)            ── device → host
       └─ dmem_buf_copyin     (write direction)           ── host → device
```

### Algorithm

```c
// Models ib_read_write @0x323cc0 (284 B). r8b = is_read flag.
// eng_ib_addrs is an ib_addrs_one_eng_t (56 B): base dmem + 3 ib_code_range segments.
function ib_read_write(eng_ib_addrs, offset, buffer, size, is_read):
    copy_func = is_read ? dmem_buf_copyout      // 0x323ce4: read = device→host
                        : dmem_buf_copyin        // 0x323db8: write = host→device
    if size == 0: return NRT_SUCCESS             // 0x323cee
    cursor = 0
    while cursor < size:                         // 0x323d50 loop head
        found_dmem0 = found_dmem1 = 0
        dmem_offset = 0
        // map the current linear stream offset to a (dmem segment, in-segment offset)
        if ib_translate_addr(eng_ib_addrs, offset,           // 0x323d81
                             &found_dmem0, &dmem_offset, &found_dmem1) != 0:
            return NRT_FAILURE                   // 0x323dc8 → return 1
        dmem = found_dmem0 ? found_dmem0 : found_dmem1        // 0x323d8a/0x323d94
        seg_end  = found_dmem0 ? dmem_offset : 0
        // clamp this chunk to the segment boundary (cmovnb at 0x323d3b)
        seg_size = dmem.size - dmem_offset                   // 0x323d2a/0x323d31
        chunk    = min(seg_size, size - cursor)              // cmovnb: don't overrun size
        copy_func(dmem, buffer + cursor, dmem_offset, chunk) // 0x323d42
        cursor += chunk                          // 0x323d45
        offset += chunk                          // 0x323d48
    return NRT_SUCCESS                            // 0x323da4 → return 0
```

### Encoding

`ib_read_write` operates on whole bytes, not records — but because the stream is a flat 64-byte array, any byte range it copies is a clean run of records when `offset` and `size` are multiples of `0x40` (which the callers ensure). The function's only structural knowledge of the record is indirect: `ib_translate_addr` maps a linear offset into one of three dmem segments (`one_offset`, `model_switch_offset`, `core_offset` in `ib_addrs_one_eng_t` `@+0x08/+0x18/+0x28`), so a record that straddles a segment boundary is copied in two chunks. The `cmovnb` at `0x323d3b` is the boundary clamp.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ib_read_write` | `0x323cc0` (284 B) | bidirectional dmem record-stream copy, segment-split | HIGH |
| `ib_translate_addr` | `0x323ca0` (neighbour) | map linear offset → `(dmem, in-segment offset)` | HIGH |
| `dmem_buf_copyout` | (dma_memory.c) | device → host copy primitive (read direction) | HIGH |
| `dmem_buf_copyin` | (dma_memory.c) | host → device copy primitive (write direction) | HIGH |

### Considerations

The direction flag (`r8b`) selecting between two `lea`'d function pointers (`0x323ce4` read, `0x323db8` write) is the entire read/write fork — there is no other behavioral difference between the two directions. A reimplementation can collapse to one loop with a function-pointer parameter, exactly as the binary does.

---

## 5. Instruction-Block I/O: `ib_has_pseudo_exit`

### Purpose

A record stream must be checked for a program-terminating EXIT sentinel before it is staged (a stream missing its EXIT would run off the end). `ib_has_pseudo_exit` is the scanner: it walks the stream in 64-byte strides looking for the `PSEUDO_INST`/EXIT two-byte signature.

### Entry Point

```text
kbl_model_add
  └─ ib_has_pseudo_exit (0x323df0, 51 B)   ── scan instr_buf for the EXIT sentinel
```

### Algorithm

```c
// Models ib_has_pseudo_exit @0x323df0 (51 B). Verbatim 64-byte stride scan.
// instr_buf = base pointer (rdi); size = byte length (rsi/edx).
function ib_has_pseudo_exit(instr_buf, size):
    if size == 0: return false                   // 0x323df2: empty stream
    offset = 0
    while offset < size:                         // 0x323e04/0x323e07 (jnb)
        // record discriminant: opcode byte at the record base
        if instr_buf[offset + 0x00] == 0xDF      // 0x323e09: PSEUDO_INST prefix
           && instr_buf[offset + 0x0C] == 1:     // 0x323e0f: pseudo_opcode == PSEUDO_EXIT_EXECUTION
            return true                          // 0x323e16
        offset += 0x40                           // 0x323e00: TPB_INST_NBYTES stride
    return false                                 // 0x323e20
```

### Encoding

This 51-byte function is the most compact independent proof of three record facts at once: (1) the stride is exactly `0x40` (`add offset, 40h` at `0x323e00`); (2) the opcode is byte `[0]` (`cmp byte ptr [instr_buf+offset], 0DFh` at `0x323e09`); and (3) the `PSEUDO_INST` sub-opcode is byte `[+0x0C]`, i.e. `+12` — the first payload byte past the 12-byte preamble (`cmp byte ptr [instr_buf+offset+0Ch], 1` at `0x323e0f`). The constant `1` is `NEURON_ISA_TPB_PSEUDO_OPCODE_PSEUDO_EXIT_EXECUTION`.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ib_has_pseudo_exit` | `0x323df0` (51 B) | scan record stream for EXIT sentinel (`0xDF`/sub-`1`) | CERTAIN |

> **GOTCHA —** the sentinel check is a logical AND of two bytes 12 apart. Checking only `byte[0]==0xDF` matches every `PSEUDO_INST` record, including the `LIBRARY_RELOAD_INDEX` sub-form (`pseudo_opcode==2`). A reimplementer must check `byte[12]==1` too, or a library-reload record will be mistaken for the program terminator.

---

## 6. Instruction-Block I/O: `ib_final_inst_validity_check`

### Purpose

Before a staged record stream is committed, its **final** record is validated against per-arch rules (the last record must be a legal terminator for that engine on that NeuronCore generation). `ib_final_inst_validity_check` is the dispatch shim into the arch-specific validator.

### Entry Point

```text
sequencer_setup_instr
  └─ ib_final_inst_validity_check (0x323cb0, 5 B jmp thunk)
       └─ tdrv_arch_instr_block_final_inst_validity_check (arch vtable)
            └─ is_valid_<terminator> → is_valid_enum / type_size_check / ...
```

### Algorithm

```c
// Models ib_final_inst_validity_check @0x323cb0 — a 5-byte tail-call thunk.
function ib_final_inst_validity_check(kbin, eng_type, final_inst):
    // 0x323cb0: e9 9b 70 fe ff  → jmp tdrv_arch_instr_block_final_inst_validity_check
    return tdrv_arch_instr_block_final_inst_validity_check(kbin, eng_type, final_inst);
    // the arch vtable picks the SUNDA/CAYMAN/MARIANA validator and runs the leaf predicates
```

### Encoding

The thunk is literally a single `jmp` (`0x323cb0: e9 9b 70 fe ff`). It exists so the call site (`sequencer_setup_instr`) has a stable, arch-neutral entry symbol while the actual validation routes through `tdrv_arch_instr_block_final_inst_validity_check` and its per-arch vtable. The leaf predicates it eventually reaches — `is_valid_enum` (the opcode/operand legality oracle, with the four OPCODE bitmasks `0x400082F07FBFFFFF` / `0x024BC3FFFFDF3F7F` / `0x7FEFFFFFE08F7FFF` / `0x1001E0000003E`), `is_valid_dtype`, `type_size_check`, and the address-quadrant checks — are documented on the [ISA Validator Architecture](validator-architecture.md) page.

### Function Map

| Function | Address | Role | Confidence |
|---|---|---|---|
| `ib_final_inst_validity_check` | `0x323cb0` (5 B) | jmp thunk → arch final-inst validator | CERTAIN |
| `tdrv_arch_instr_block_final_inst_validity_check` | `0x30ad50` | arch-vtable validity dispatcher | HIGH |
| `is_valid_enum` | `0x27b650` / `0x324050` | opcode/operand legality oracle (OPCODE bitmasks) | HIGH |
| `type_size_check` | `0x27b9d0` / `0x324460` | dtype↔byte-size legality (1/2/4/8 B) | HIGH |

> **NOTE —** the validator primitives appear at multiple addresses (`0x27b650` and `0x324050` for `is_valid_enum`; `0x27b9d0` and `0x324460` for `type_size_check`) — these are arch-specialized copies (SUNDA/CAYMAN/MARIANA), all decoding the **same** record format. The record layout on this page is arch-neutral; only the *legality* of specific field values varies per arch. See [Per-Arch Validators](validators-per-arch.md).

---

## Related Components

| Name | Relationship |
|---|---|
| `NEURON_ISA_TPB_HEADER` / `NEURON_ISA_TPB_EVENTS` | the shared 12-byte preamble of every record |
| 118 × `NEURON_ISA_TPB_<OP>_STRUCT` | the per-opcode union arms reinterpreting bytes `+12..+63` |
| `ib_read_write` / `ib_has_pseudo_exit` / `ib_final_inst_validity_check` | the EIB stream I/O, scan, and validity entry points (`tdrv/instruction_block.c`) |
| `is_valid_enum` / `type_size_check` | the leaf legality predicates the final-inst check reaches |

## Cross-References

- [Overview: the TPB Engine Instruction Model](overview.md) — where the 64-byte record sits in the engine pipeline
- [Pseudo-Instruction Lowering (SP / TopSP Builders)](pseudo-instruction-lowering.md) — how the `0xC1..0xDF` pseudo band is rewritten into real records before device fetch
- [FP8 and dtype Encoding](fp8-dtype-encoding.md) — the `NEURON_ISA_TPB_DTYPE` enum the record's dtype fields select from
- [ISA Validator Architecture and Entry Tree](validator-architecture.md) — `is_valid_enum`, the OPCODE bitmasks, and the per-opcode legality predicates
- [Per-Arch Instruction Validators (SUNDA / CAYMAN / MARIANA Deltas)](validators-per-arch.md) — the arch-specialized validator copies and address-aperture deltas
- [The Load Pipeline (Parse → Build → Stage → Relocate)](../neff/load-pipeline.md) — the stage step `ib_read_write` services; where record streams enter device HBM
- [KBIN Structures](../neff/kbin-structs.md) — the KBIN container the record stream is carried in
