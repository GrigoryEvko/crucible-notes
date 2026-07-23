# Per-Engine TPB-Sequencer Microcode (64-byte word)

This page is the **canonical byte-level reference** for the **per-engine TPB-sequencer
microcode** carried inside a NEFF: the fixed **64-byte instruction word** that the
PE / ACT / POOL / DVE engines fetch and decode, plus the separate **8-byte SP word**.
It is the wire format that the [assembly pipeline](assembly-pipeline.md) *emits*, the
[weight/relocation pass](relocation-weights.md) *patches*, the [container byte
format](container-byte-format.md) *packs*, and the [SEQ decode / dispatch
hub](../firmware/seq/dispatch-hub.md) *decodes on device*. For the POOL engine it is
also the **host half of the GPSIMD custom-op bridge**: the 1-byte opcode the POOL
sequencer carries is the *same* number the device-side
[`kernel_info_table`](../firmware/pool/kernel-info-table.md) looks up to reach a
Vision-Q7 kernel. The compiler-side BIR→opcode mapping that produces these bytes is the
[BIR instruction roster](../compiler/bir-inst-roster.md).

Everything below is decoded **byte-for-byte from a shipped NEFF sample** — a real
decompressed NEFF binwalk-carved out of `libnrt.so` at file offset `0xC08220`
(`…/C08220/tar/sg00/{pe,pool,act,dve,sp}.{bin,asm,json}`, `def.json`) — and
cross-checked against the **`STATIC_ASSERT`ed shipped arch-isa headers** in the
GPSIMD custom-op lib (`aws-neuronx-gpsimd-customop-lib_0.21.2.0`,
`…/c10/include/arch-isa/tpb/aws_tonga_isa_tpb_common.h`,
`…/tpb/aws_tonga_isa_tpb_pseudo_dma_trigger.h`,
`…/sp/aws_tonga_isa_sp_common.h`). These engine `.bin` words are a **TPB microcode
format, not Xtensa**; they are decoded from the header structs, *not* with the native
`ncore2gp` `xtensa-elf-objdump` (which is for Vision-Q7 device code).

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/string/`STATIC_ASSERT`; `INFERRED` = reasoned over
OBSERVED facts; `CARRIED` = consolidated from a cited cross-page anchor. Crossed with
`HIGH`/`MED`/`LOW`. Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **CORRECTION** (overturns a naive reading), **NOTE**
(orientation).

> **GOTCHA — the "16-bit opcode" is not a 16-bit opcode.** A naive reader of the
> little-endian lead word (`0x10C8` in `pe.bin`, `0x10C1` / `0x10A0` in `pool.bin`)
> sees a 16-bit opcode. It is not: it is the **first two bytes of the 4-byte
> instruction header** — `byte0 = opcode` (1-byte `TONGA_ISA_TPB_OPCODE`), `byte1 =
> inst_word_len = 0x10 = 16 = NWORDS`. The `0x10` high byte is the **constant 64-byte
> slot-length marker**, identical for every instruction regardless of opcode, *not*
> part of the opcode. So `0x10C8 == {opcode 0xC8, len 16}`. `[HIGH/OBSERVED]`

---

## 0. The two sequencer ISAs in one diagram

There are **two distinct sequencer ISAs in a NEFF**; do not conflate them.

```
 NEFF sg00/ engine streams
 ┌───────────────────────────────────────────────────────────────────────────┐
 │ (1) TPB-ENGINE sequencer   pe.bin act.bin pool.bin dve.bin                  │
 │     64-BYTE slots, 0x40 stride.  1-byte TONGA_ISA_TPB_OPCODE = base|(eng<<5)│
 │                                                                             │
 │     +0x00 ┌── header (4 B) ──┐  +0x04 ┌── events ──┐  +0x.. ┌── payload ──┐ │
 │           │op│len│dcmd│dhint │        │ 4B or 8B   │        │ to 64 B     │ │
 │           └──┴───┴────┴──────┘        └────────────┘        └─────────────┘ │
 │            ^op   ^=0x10 always                                              │
 │                                                                             │
 │ (2) SP sequencer            sp.bin                                          │
 │     8-BYTE slots.  1-byte header {opcode:7, phase:1}, OWN opcode table.     │
 │     per-NeuronCore SYNC sequencer (WRITE/WAIT/BRANCH/CNTR/AR). NOT 64-B.    │
 └───────────────────────────────────────────────────────────────────────────┘

 opcode byte0  =  base[4:0] | (engine[7:5] << 5)
     engine:  PE=0  ACT=1  POOL=2  ALL=3  RT=6  SIM=7      (RT = runtime-patched)
```

The C08220 sample's five streams: `pe.bin` = **64 B** (1 slot), `pool.bin` = **192 B**
(3 slots), `act.bin` / `dve.bin` / `sp.bin` = **0 B** (empty). `[HIGH/OBSERVED]`

---

## 1. The 64-byte instruction slot — the wire record `[HIGH/OBSERVED]`

Every TPB-engine instruction is a fixed **64-byte** slot. The shipped
`aws_tonga_isa_tpb_common.h` pins the constants and the header with `STATIC_ASSERT`:

```c
TONGA_ISA_TPB_INST_NBYTES   = 64;   /* slot size                         */
TONGA_ISA_TPB_INST_NWORDS   = 16;   /* 64 / 4-B word  → the 0x10 marker   */
TONGA_ISA_TPB_RT_MAX_NAME   = 32;   /* DMA queue-name field width         */
TONGA_ISA_TPB_NUM_EVENTS    = 256;
TONGA_ISA_TPB_NUM_SEMAPHORES= 32;
TONGA_ISA_TPB_MEM_PATTERN_MAX_NUM_DIM = 4;
```

### 1.1 The common 4-byte header

`TONGA_ISA_TPB_INST_HEADER` — `STATIC_ASSERT(sizeof == 4)`:

| off | sz | field | type | sample value | meaning |
|---|---|---|---|---|---|
| `+0x00` | 1 | `opcode` | `TONGA_ISA_TPB_OPCODE` | varies | 1-byte packed enum (§2) |
| `+0x01` | 1 | `inst_word_len` | `uint8_t` | **`0x10`** | `== NWORDS == 16` for every 64-B slot |
| `+0x02` | 1 | `debug_cmd` | `uint8_t` | `0x00` | bits[1:0]=`TRACE_MODE`, bit[7]=`halt_en` |
| `+0x03` | 1 | `debug_hint` | `uint8_t` | `0x00` | software trace hint, pads header to 4 B |

`debug_cmd` parsing is itself header-pinned: `TRACE_MODE_SHIFT=0, SIZE=2`,
`HALT_EN_SHIFT=7, SIZE=1`; `TRACE_MODE` ∈ {DISABLE 0, GEN_ON_START 1, GEN_ON_END 2,
GEN_ON_START_AND_END 3}. Both bytes are `0` across the entire C08220 sample.
`[HIGH/OBSERVED]`

> **QUIRK — `inst_word_len` is measured in 4-byte words, not bytes.** `0x10 = 16`
> *words* × 4 B = 64 B. The same `0x10` is asserted in the DMA-trigger validity check as
> `inst_word_len == TONGA_ISA_TPB_INST_NWORDS`. A reimplementer who reads it as a byte
> count (expecting `0x40`) will reject every valid slot. `[HIGH/OBSERVED]`

### 1.2 The events block — two coexisting dialects

The 4 bytes (or 8 bytes) after the header are the embedded synchronization block. **Two
dialects coexist in the corpus**; the C08220 sample uses the *8-byte* form (proven in
§7.2).

**(a) Legacy TONGA (4 B)** — `TONGA_ISA_TPB_INST_EVENTS`, `STATIC_ASSERT(sizeof == 4)`:

| off | sz | field | enum values (shipped) |
|---|---|---|---|
| `+0` | 1 | `wait_event_mode` | `WAIT_NONE 0` / `WAIT_FOR_SET 1` / `WAIT_FOR_SET_THEN_CLEAR 2` |
| `+1` | 1 | `wait_event_idx` | index into the 256-event space |
| `+2` | 1 | `set_event_mode` | `SET_NONE 0` / `ON_DONE_RD_SRC 1` / `ON_DONE_WR_DST 2` / `ON_INST_DONE 3` |
| `+3` | 1 | `set_event_idx` | — |

**(b) NEURON_ISA semaphore form (8 B)** — the dialect the sample carries:

| off | sz | field | sample-decoded semantics |
|---|---|---|---|
| `+0` | 1 | `wait_mode` | `0x04` ⇒ "wait semaphore ≥ value" (GE / `>0`) |
| `+1` | 1 | `wait_idx` | semaphore index `$S[n]` |
| `+2` | 1 | `update_mode` | `0x13` ⇒ increment-on-complete; `0x00` ⇒ none |
| `+3` | 1 | `update_idx` | semaphore index to update |
| `+4` | 4 | `semaphore_value` | `u32`, `0` in the sample |

> **CORRECTION — `$S[n]` not `$E[n]`.** The sample's disassembler prints **semaphore**
> predicates (`$S[10]>0`, `$S[22]++`), not event predicates, *because the events block
> is the 8-byte semaphore dialect* — a `wait_idx`/`update_idx` into the 32-semaphore
> space plus a 32-bit `semaphore_value`. The numeric 8-B mode bytes (`wait 0x04`,
> `update 0x13`) are a different enum from the 4-B `WAIT_EVENT_MODE` enum above; the
> *semantics* (GE-wait / increment) are `[HIGH]`, the formal 8-B enum constant *names*
> were not located as a shipped header and are `[LOW]` (see §11). `[HIGH/OBSERVED]`

### 1.3 The payload and the memory-access operands

After the 8-byte prefix the per-opcode payload fills to 64 B. Data-moving ops use
`TONGA_ISA_TPB_MEM_ACCESS_{1,2,3,4}D` (8 / 12 / 16 / 20 B), each
`{ tpb_addr start_addr (4B); int16 step_elem[N]; uint16 num_elem[N] }` over up to
`MEM_PATTERN_MAX_NUM_DIM = 4` dims (all `STATIC_ASSERT`ed). A `COPY` slot, for example,
lays out `header(4) + events(4) + dtype(1) + rsvd(3) + src 4D(20) + dst 4D(20) +
num_active_channels(1) + rsvd(11) = 64`. `[CARRIED HIGH — container/struct anchor]`

> **NOTE — library-index magic vs opcode.** When `libnrtucode_internal` builds its
> *library index* it stamps the word `0x1095` (LE) at the head of every 64-B slot
> (`+0, +0x40, +0x80, …`), confirming the `0x40` stride independent of opcode. The
> `0x1095` magic is a **library-index slot marker**, distinct from a per-instruction
> opcode; do not mistake it for an instruction opcode. `[CARRIED HIGH]`

---

## 2. The opcode space — `opcode = base | (engine<<5)` `[HIGH/OBSERVED]`

The 1-byte opcode packs the **execution-engine class in bits[7:5]** and a **per-engine
base in bits[4:0]**. This is not inferred — it is *literally the form of the shipped
enum*. From `aws_tonga_isa_tpb_common.h`:

```c
typedef enum TONGA_ISA_TPB_ENGINE {
    PE  = 0x0,  ACT = 0x1,  POOL = 0x2,  ALL = 0x3,
    RT  = 0x6,  /* runtime-patched */   SIM = 0x7,  INVALID = 0xFF
} TONGA_ISA_TPB_ENGINE;

TONGA_ISA_TPB_OPCODE_TENSOR_TENSOR_ARITH_OP = 0x01 | (POOL<<5);  /* = 0x41 */
TONGA_ISA_TPB_OPCODE_PSEUDO_DMA_TRIGGER     = 0x01 | (RT  <<5);  /* = 0xC1 */
```

So **`0x41 = 0x01 | (2<<5)`** and **`0xC1 = 0x01 | (6<<5)`** — the engine class is
recoverable as `opcode >> 5`, the base as `opcode & 0x1F`. This single rule explains
every opcode in the corpus and reconciles the `.bin` with the device-side dispatch
tables (§8–9).

### 2.1 The shipped catalog (byte values each opcode takes)

| eng | base | **opcode** | mnemonic | notes |
|---|---|---|---|---|
| PE(0) | `0x01` | **`0x01`** | `LDWEIGHTS` | PE-array weight load |
| PE(0) | `0x02` | **`0x02`** | `MATMUL` | systolic matmul |
| ACT(1) | `0x01` | **`0x21`** | `ACTIVATE` | activation engine |
| ACT(1) | `0x02` | **`0x22`** | `ACTIVATE_QUANTIZE` | |
| POOL(2) | `0x01` | **`0x41`** | `TENSOR_TENSOR_ARITH_OP` | GpSimd; SEQ `0x41`; kit op `0x41` |
| POOL(2) | `0x11` | **`0x51`** | `TENSOR_TENSOR_BITVEC_OP` | |
| POOL(2) | `0x02` | **`0x42`** | `TENSOR_REDUCE_ARITH_OP` | |
| POOL(2) | `0x12` | **`0x52`** | `TENSOR_REDUCE_BITVEC_OP` | kit op `0x52` |
| POOL(2) | `0x03` | **`0x43`** | `TENSOR_SCALAR_ARITH_OP` | compile-time scalar |
| POOL(2) | `0x13` | **`0x53`** | `TENSOR_SCALAR_BITVEC_OP` | |
| POOL(2) | `0x04` | **`0x44`** | `TENSOR_SCALAR_PTR_ARITH_OP` | runtime scalar (ptr) |
| POOL(2) | `0x14` | **`0x54`** | `TENSOR_SCALAR_PTR_BITVEC_OP` | |
| POOL(2) | `0x05` | **`0x45`** | `POOL` | kit op `0x45` (`decode_pool`) |
| POOL(2) | `0x06` | **`0x46`** | `COPY` | kit op `0x46` (`pool_copy`) |
| POOL(2) | `0x07` | **`0x47`** | `CAST` | kit op `0x47` (`pool_cast`) |
| POOL(2) | `0x08` | **`0x48`** | `RECIPROCAL` | (DIVIDE only allowed here) |
| POOL(2) | `0x09` | **`0x49`** | `MEMSET` | |
| POOL(2) | `0x0A` | **`0x4A`** | `REG_LOAD` | |
| POOL(2) | `0x0B` | **`0x4B`** | `REG_STORE` | |
| POOL(2) | `0x0C` | **`0x4C`** | `REG_SHUFFLE` | |
| POOL(2) | `0x0D` | **`0x4D`** | `RNG` | (XORWOW) |
| POOL(2) | `0x0E` | **`0x4E`** | `TENSOR_CUMULATIVE_ARITH_OP` | |
| POOL(2) | `0x1E` | **`0x5E`** | `TENSOR_CUMULATIVE_BITVEC_OP` | |
| ALL(3) | `0x01` | **`0x61`** | `EVENT_WAIT` | |
| ALL(3) | `0x02` | **`0x62`** | `EVENT_SET` | |
| ALL(3) | `0x03` | **`0x63`** | `EVENT_CLEAR` | |
| ALL(3) | `0x05` | **`0x65`** | `SEMAPHORE_TEST_AND_SET` | |
| ALL(3) | `0x08` | **`0x68`** | `NOP` | |
| ALL(3) | `0x09` | **`0x69`** | `WRITE` | the concrete op a pseudo lowers to |
| ALL(3) | `0x0A` | **`0x6A`** | `NOTIFY` | |
| RT(6) | `0x01` | **`0xC1`** | `PSEUDO_DMA_TRIGGER` | **`pool.bin` lead** (runtime-patched) |
| RT(6) | `0x02` | **`0xC2`** | `PSEUDO_DMA_REARM` | |
| RT(6) | `0x03` | **`0xC3`** | `PSEUDO_DMA_BARRIER` | |
| RT(6) | `0x04` | **`0xC4`** | `PSEUDO_DMA_MEMCPY` | |
| RT(6) | `0x05` | **`0xC5`** | `PSEUDO_SEMAPHORE_SET` | |
| RT(6) | `0x06` | **`0xC6`** | `PSEUDO_LOAD_ACT_FUNC_SET` | |
| RT(6) | `0x07` | **`0xC7`** | `PSEUDO_SET_TRANSFER_ADJUST` | |
| RT(6) | `0x08` | **`0xC8`** | `PSEUDO_READ_VAR_ADDR` (ulib 0.21.2) | **`pe.bin` lead** — see QUIRK below |
| SIM(7) | `0x0B` | **`0xEB`** | `SIM_DMA_COPY` | simulator-only |
| SIM(7) | `0x0C`..`0x0F` | `0xEC`..`0xEF` | `SIM_WRFILE`/`WRNPY`/`RDNPY`/`MEMCPY` | simulator-only |
| — | — | **`0xFF`** | `INVALID` | |

> **CORRECTION / QUIRK — `0xC8` is `PSEUDO_READ_VAR_ADDR` in the header, but the sample
> calls it `PSEUDO_TRIGGER_COLLECTIVE`.** The shipped ulib 0.21.2 header names RT base
> `0x08` = `PSEUDO_READ_VAR_ADDR`. The C08220 sample is a *newer NEURON_ISA NEFF*: its
> `pe.asm` prints `PSEUDO_TRIGGER_COLLECTIVE` and the 64-byte payload decodes byte-exact
> as a collective record (§6–7.1). So **RT base `0x08` was reassigned across generations**
> (`READ_VAR_ADDR` → `TRIGGER_COLLECTIVE`); what is *stable* is the engine-class encoding
> `0x08 | (RT<<5) = 0xC8`. Pin the encoding, not the mnemonic. `[HIGH — header OBSERVED;
> sample collective decode OBSERVED]`

### 2.2 The `0xA0` opcode — a class-5 SEQ/SP control op, not a TPB engine

`0xA0 = base 0x00 | (class 5 << 5)`. **Class 5 is not a TONGA TPB engine** (the TPB
engines are 0,1,2,3,6,7). In the NEURON_ISA / SEQ-dispatch dialect, `0xA0..0xBF` are the
**SP/SEQ control-stream opcodes**: `0xA0 Event_Semaphore`, `0xA1` sync/Pause-Halt,
`0xA2 MOVE`, `0xA6 NOTIFY`, `0xA8 BRANCH`, `0xA9/0xAA TensorLoad/Store`,
`0xBF SB2SB_Collective`. The sample's `pool.bin` rec2 lead `0xA0` is the
**`EVENT_SEMAPHORE`** control op (§7.2). `[HIGH — sample OBSERVED; dispatch names CARRIED]`

---

## 3. The two sequencer ISAs in detail

### 3.1 TPB-engine sequencer (PE/ACT/POOL/DVE `.bin`) — this report's subject

64-byte slots, 1-byte `TONGA_ISA_TPB_OPCODE = base|(engine<<5)`, 4-B header + (4 or 8-B)
events + per-opcode payload. The four data engines each get one such stream; the loader
stores each as `instr_set{uint8_t* buffer; uint32_t size}` keyed by engine name. The
instruction *kind* is selected by the engine-class bits, so a PE stream legally carries
PE/ALL/RT opcodes, a POOL stream POOL/ALL/RT opcodes, etc. `[HIGH/OBSERVED — sizes;
INFERRED — per-stream legality]`

### 3.2 SP sequencer (`sp.bin`) — a separate, smaller 8-byte ISA

From `aws_tonga_isa_sp_common.h`:

```c
#define TONGA_ISA_SP_INST_NBYTES   8     /* NWORDS = 2                       */
#define TONGA_ISA_SP_NUM_AR        16
#define TONGA_ISA_SP_NUM_COUNTERS  16
#define TONGA_ISA_SP_NUM_EVENTS    128
#define TONGA_ISA_SP_MAX_COUNTER_VALUE  (1<<24)

typedef struct { TONGA_ISA_SP_OPCODE opcode:7; uint8_t p:1; } /* one byte */
    TONGA_ISA_SP_INST_HEADER;
```

The SP opcode set (`aws_tonga_isa_sp_common.h`, `STATIC_ASSERT(sizeof==1)`):

| op | mnemonic | op | mnemonic |
|---|---|---|---|
| `0x04` | `WRITE8_IND` | `0x30` | `BRANCH_U` |
| `0x05` | `WRITE32_IND` | `0x34` | `BRANCH_C_EVENT_SET` |
| `0x08` | `WRITE8_DIR` | `0x38` | `BRANCH_C_CNTR_Z` |
| `0x10` | `NOTIFY_HALT` | `0x39` | `BRANCH_C_CNTR_NZ` |
| `0x20` | `WAIT_CYCLES` | `0x40` | `SET_CNTR` |
| `0x24` | `WAIT_EVENT_SET` | `0x41` | `DECR_CNTR` |
| `0x25` | `WAIT_EVENT_SET_M` | `0x50` | `CLR_ALL_EVENTS` |
| | | `0x51` | `CLR_EVENT` |
| | | `0x60` | `SET_AR` |

The SP is the **per-NeuronCore SYNC/control sequencer** (event/semaphore-gated branch +
counter control) — it is *not* a 64-B-slot data engine. `sp.bin` is **0 bytes** in the
sample: the toy graph needs no SP control program. `[HIGH/OBSERVED]`

> **GOTCHA — two unrelated things are both called "SP".** (i) The `AL_HAL_TPB_ENG_SP`
> engine whose NEFF file is `sp.bin` — *this* legacy 8-B sync sequencer. (ii) The
> "TOP_SP" / SEQ control processor that runs the on-device dispatch and *lowers
> collectives*. The `pe.bin` `TRIGGER_COLLECTIVE` is **placed in the PE stream but
> executes on the TOP SPs** after relocation — the engine `.bin` filename is the
> compiler's *placement slot*, not necessarily the executor. `[HIGH/INFERRED]`

---

## 4. The RT-class pseudo opcodes — runtime-patched, never hardware-executed

The RT engine class (engine `6`) is the **compiler→runtime handoff band**. The shipped
header comment is explicit (verbatim): *"RUNTIME modifiable instructions / not the real
instructions, rather a placeholders for the runtime to add appropriate WRITE based on
DMA queue allocation. NEVER executed by the H/W."* The DMA-trigger header adds:
*"DmaTrigger … is a pseudo-instruction generated by compiler. It is replaced by Write,
by KRT."* (KRT = Kaena RunTime = libnrt.) `[HIGH/OBSERVED — shipped comments]`

**Consequences a reimplementer must honor:**

- `0xC1` (`PSEUDO_DMA_TRIGGER`) and `0xC8` (the collective pseudo) **never appear in any
  device-side decode table** — the SEQ never sees them, because the loader has already
  rewritten them.
- The engine `.bin` in the NEFF is a **pre-relocation stream**. At `nrt_load` the patch
  engine (`kbin_patch` `INSERT`/`MODIFY`/`DELETE` across `PREAMBLE`/`POSTAMBLE`/`MAIN`/
  `FUNCTION` sections) and the `translate_pseudo_instrs` pass expand each pseudo into the
  concrete `WRITE` (`ALL` opcode `0x69`) to the model's allocated DMA tail-pointer /
  semaphore CSR. See [relocation & weight patching](relocation-weights.md). `[HIGH/CARRIED]`
- `PSEUDO_DMA_TRIGGER` carries the **queue name** (e.g. `q_gradient_in`) as a 32-B ASCII
  field; the runtime resolves that name (via `def.json` `dma_queue` → `kbin_dma_ring`) to
  the physical ring and emits the tail-pointer `WRITE`. `use_raw_count` / `block_id`
  select which descriptor block (from `pool.json` `"dma"`) to transfer. `[HIGH/OBSERVED]`

---

## 5. The `PSEUDO_DMA_TRIGGER` record (a `pool.bin` slot)

`TONGA_ISA_TPB_PSEUDO_DMA_TRIGGER_INST` — `STATIC_ASSERT(sizeof == NBYTES == 64)`,
verbatim from the shipped header:

```c
typedef struct {
    TONGA_ISA_TPB_INST_HEADER inst_header;        /* +0x00, 4 B  {0xC1, 0x10, …}   */
    TONGA_ISA_TPB_INST_EVENTS inst_events;        /* +0x04, 4 B  (legacy dialect)  */
    char     dma_queue_name[TONGA_ISA_TPB_RT_MAX_NAME];  /* +0x08, 32 B  (legacy)  */
    uint32_t use_raw_count;                        /* +0x28, 4 B                    */
    union { uint32_t block_id; uint32_t count; };  /* +0x2C, 4 B                    */
    uint8_t  reserved[16];                         /* +0x30, 16 B                   */
} TONGA_ISA_TPB_PSEUDO_DMA_TRIGGER_INST;          /* == 64 B                       */
```

Shipped validity check `tonga_isa_tpb_dma_trigger_check_validity` asserts, in order:
`opcode == PSEUDO_DMA_TRIGGER`, `inst_word_len == NWORDS (16)`, **`dma_queue_name[0] !=
'\0'`** (non-empty), and valid event modes. The non-empty-name assert is *why* every
DMA-trigger slot must embed its queue name. `[HIGH/OBSERVED]`

> **GOTCHA — the sample's events block is 8 B, so the name lands at `+0x0C`, not `+0x08`.**
> The shipped struct declares `inst_events` as the **4-byte** legacy `INST_EVENTS`,
> placing `dma_queue_name` at `+0x08`. The C08220 sample uses the **8-byte** NEURON_ISA
> events dialect, shifting the name to `+0x0C`. This is byte-proven in §7.2 (read at
> `+0x08` the name is empty; read at `+0x0C` it decodes cleanly). Same struct, the
> events field widened by 4 B across the dialect. `use_raw_count`/`block_id` shift by the
> same +4. `[HIGH/OBSERVED]`

---

## 6. The `PSEUDO_TRIGGER_COLLECTIVE` record (the `pe.bin` slot)

The collective pseudo (RT base `0x08` in the newer dialect) is a 64-byte record whose
field layout reproduces `pe.asm` exactly:

```c
struct {  /* sizeof == 64; the pe.bin slot                                      */
  /* +0x00 */ header   { opcode 0xC8, inst_word_len 16, debug_cmd, debug_hint } /* 4  */
  /* +0x04 */ events   { wait_mode, wait_idx, update_mode, update_idx, u32 sem };/* 8  */
  /* +0x0C */ u8  op;            /* TONGA_ISA_TPB_ALU_OP reduce-op  ADD=0x04     */
  /* +0x0D */ u8  dtype;         /* TONGA_ISA_TPB_DTYPE             FP32=0x0A    */
  /* +0x0E */ u16 group_id;      /* replica-group id                            */
  /* +0x10 */ u32 input_tensor_id;
  /* +0x14 */ u32 output_tensor_id;
  /* +0x18 */ u64 num_elements;
  /* +0x20 */ u8  ctype;         /* COLLECTIVE_TYPE  ALL_REDUCE=0x01            */
  /* +0x21 */ u8  reserved1[15]; /* pad to 8-B align for the trailing u64s      */
  /* +0x30 */ u64 src_offset_elems;
  /* +0x38 */ u64 dst_offset_elems;
};                               /* == 64 B                                     */
```

`op` uses `TONGA_ISA_TPB_ALU_OP` (header: `ADD=0x04`, `MAX=0x08`, …) and `dtype` uses
`TONGA_ISA_TPB_DTYPE` (header: `FP32=0x0A`, `BF16=0x06`, `FP16=0x07`, `INT32=0x08`). There
is **no static `is_valid` assert** for this struct; it is validated and lowered by the
runtime (the collective expands to a TOP-SP instruction series). `[HIGH — struct CARRIED;
field bytes OBSERVED]`

---

## 7. Worked decode — the C08220 sample, byte-for-byte `[HIGH/OBSERVED]`

### 7.1 `pe.bin` (64 B, 1 slot) → `PSEUDO_TRIGGER_COLLECTIVE`

Raw `xxd` (the entire 64-byte file):

```
00000000: c810 0000 040a 1316 0000 0000 040a 0000  ................
00000010: 0300 0000 0400 0000 2000 0000 0000 0000  ........ .......
00000020: 0100 0000 0000 0000 0000 0000 0000 0000  ................
00000030: 0000 0000 0000 0000 0000 0000 0000 0000  ................
```

Bytes → fields → meaning (all little-endian):

| bytes | off | field | value | meaning |
|---|---|---|---|---|
| `c8 10 00 00` | `+0x00` | header | op `0xC8`, len `0x10`, dcmd `0`, dhint `0` | `PSEUDO_TRIGGER_COLLECTIVE`, 64-B slot |
| `04 0a 13 16` | `+0x04` | events (8B) | wait `0x04`/idx `0x0A`, upd `0x13`/idx `0x16` | wait `$S[10]>0`; `$S[22]++ @complete` |
| `00 00 00 00` | `+0x08` | events `sem_value` | `0` | — |
| `04` | `+0x0C` | `op` | `0x04` | `ALU_OP_ADD` → reduce op = ADD |
| `0a` | `+0x0D` | `dtype` | `0x0A` | `DTYPE_FP32` |
| `00 00` | `+0x0E` | `group_id` | `0` | replica-group 0 |
| `03 00 00 00` | `+0x10` | `input_tensor_id` | `3` | `def.json` `gradient_in` (var_id 3) |
| `04 00 00 00` | `+0x14` | `output_tensor_id` | `4` | `def.json` `gradient_out` (var_id 4) |
| `20 00 00 00 00 00 00 00` | `+0x18` | `num_elements` | `0x20 = 32` | matches `internal_shape [1,1,1,32]` |
| `01` | `+0x20` | `ctype` | `0x01` | `ALL_REDUCE` |
| `00…` | `+0x30`/`+0x38` | `src/dst_offset_elems` | `0` / `0` | — |

This reproduces `pe.asm` **exactly**:

```
PSEUDO_TRIGGER_COLLECTIVE $S[10]>0 $S[22]++@complete ctype=ALL_REDUCE
  input_tensor_id=3 output_tensor_id=4 num_elements=32 dtype=fp32 op=ADD group_id=0;
```

`def.json` `replica_groups = [[]]` → a single trivial group → `group_id 0`. The
collective reduces `gradient_in` across the (trivial) replica group into `gradient_out`.
`[HIGH/OBSERVED]`

### 7.2 `pool.bin` (192 B, 3 slots)

Raw `xxd`:

```
00000000: c110 0000 0000 0000 0000 0000 715f 6772  ............q_gr
00000010: 6164 6965 6e74 5f69 6e00 0000 0000 0000  adient_in.......
00000020: 0000 0000 0000 0000 0000 0000 0000 0000  ................
00000030: 0000 0000 0000 0000 0000 0000 0000 0000  ................
00000040: c110 0000 0416 0000 0000 0000 715f 6772  ............q_gr
00000050: 6164 6965 6e74 5f6f 7574 0000 0000 0000  adient_out......
00000060: 0000 0000 0000 0000 0000 0000 0000 0000  ................
00000070: 0000 0000 0000 0000 0000 0000 0000 0000  ................
00000080: a010 0000 040b 0000 0000 0000 0000 0000  ................
00000090: 0000 0000 0000 0000 0000 0000 0000 0000  ................
000000a0: 0000 0000 0000 0000 0000 0000 0000 0000  ................
000000b0: 0000 0000 0000 0000 0000 0000 0000 0000  ................
```

**slot0 @ `0x00` — `PSEUDO_DMA_TRIGGER` (input load):**

| bytes | off | field | value |
|---|---|---|---|
| `c1 10 00 00` | `+0x00` | header | op `0xC1` `PSEUDO_DMA_TRIGGER`, len `0x10` |
| `00 00 00 00 00 00 00 00` | `+0x04` | events (8B) | all `0` → no wait predicate |
| `71 5f 67 72 61 64 …` | `+0x0C` | `dma_queue_name` | `"q_gradient_in\0"` |
| `00 00 00 00` | `+0x28+Δ` | `use_raw_count` | `0` (block_id refs a k-elf block) |
| `00 00 00 00` | `+0x2C+Δ` | `block_id` | `0` |

→ `PSEUDO_DMA_TRIGGER q_gradient_in block_id=0;`

**slot1 @ `0x40` — `PSEUDO_DMA_TRIGGER` (gated output store):**

| bytes | off | field | value |
|---|---|---|---|
| `c1 10 00 00` | `+0x00` | header | op `0xC1`, len `0x10` |
| `04 16 00 00 00 00 00 00` | `+0x04` | events (8B) | wait `0x04`/idx `0x16(22)` → `$S[22]>0` |
| `71 5f 67 72 …` | `+0x0C` | `dma_queue_name` | `"q_gradient_out\0"` |
| `00 00 00 00` | — | `block_id` | `0` |

→ `PSEUDO_DMA_TRIGGER $S[22]>0 q_gradient_out block_id=0;`

> **GOTCHA / PROOF of the 8-byte events dialect.** At `pool.bin+0x08` the bytes are
> `00 00 00 00` and the queue name begins at `+0x0C` (`71 5f 67 72` = `"q_gr"`). Reading
> the name at `+0x08` (assuming 4-B events) yields an *empty* string — which the shipped
> validity check would reject. The clean decode **only** at `+0x0C` proves the sample is
> the NEURON_ISA 8-B-events dialect (§1.2, §5). `[HIGH/OBSERVED]`

**slot2 @ `0x80` — `EVENT_SEMAPHORE` (completion gate):**

| bytes | off | field | value |
|---|---|---|---|
| `a0 10 00 00` | `+0x00` | header | op `0xA0` `EVENT_SEMAPHORE` (SEQ control), len `0x10` |
| `04 0b 00 00 00 00 00 00` | `+0x04` | events (8B) | wait `0x04`/idx `0x0B(11)` → `$S[11]>0` |

→ `EVENT_SEMAPHORE $S[11]>0;`

This reproduces `pool.asm` **exactly** (3 lines). `[HIGH/OBSERVED]`

### 7.3 The end-to-end dataflow this toy NEFF encodes

`def.json` wires the semaphores: `q_gradient_in` `owner=pool semaphore=10 type=in`;
`q_gradient_out` `owner=pool semaphore=11 type=out`. The numbers **10 / 11 / 22** thread
through `def.json` and the PE collective's `$S[22]` set:

```
 POOL slot0 : DMA  input → gradient_in            (q_gradient_in,  sem 10)
 PE   slot0 : wait $S[10]; ALL_REDUCE(ADD,fp32) gradient_in(3) → gradient_out(4);
              set $S[22] @complete          [runs on TOP SPs after relocation]
 POOL slot1 : wait $S[22]; DMA gradient_out → output  (q_gradient_out, sem 11)
 POOL slot2 : EVENT_SEMAPHORE $S[11]          (gate completion)
```

`act.bin` / `dve.bin` / `sp.bin` are empty — no activation, vector, or SP-sync work in
this graph. The whole 5-engine program is internally consistent. `[HIGH/OBSERVED;
INFERRED — the cross-engine ordering narrative]`

---

## 8. The POOL → Vision-Q7 custom-op bridge `[HIGH/OBSERVED + CARRIED]`

The **POOL engine is the GPSIMD attach point.** There are **two opcode dispatches in
series**; the `.bin` sequencer word is Stage A, the device-side
[`kernel_info_table`](../firmware/pool/kernel-info-table.md) is Stage B.

```
 NEFF tar (pool.bin + ucode_lib bytes)
   │  load: relocate pool.bin RT pseudos → WRITE;  DMA ucode_lib image → Q7 IMEM
   ▼
 STAGE A  host/NEFF: SEQ fetches the POOL .bin, hands opcode byte0 to the front-end
   │      ("S: Dispatch opcode=0x%x"); compute opcodes (0x41/0x46/0x7e/0xF0…) → a Q7 core
   ▼
 STAGE B  device/Vision-Q7: POOL Q7 core (entry 0x01005610) LINEAR-SCANS its
   │      kernel_info_table  {0,0,spec,opcode, u32 funcVA}  for key (opcode<<24|spec<<16)
   ▼      on hit → callx8 funcVA   (miss → "P%i: UNKNOWN OPCODE=0x%x")
 the matched per-opcode kernel runs on the 8-core ncore2gp cluster
```

### 8.1 The opcode identity that makes the bridge work

**The `kernel_info_table`'s 1-byte opcode column IS the `TONGA_ISA_TPB_OPCODE` low byte.**
The device CAYMAN table's opcodes — `0x7e`/`0x7c`/`0x7d`/`0x45`/`0x51`/`0x41`/`0xf0`(×5)/
`0x52`/`0x46`/`0x47`/`0xbe`/`0xf2`/`0x7b` — map 1:1 to IOTA / CROSS_LANE_REDUCE / POOL /
TENSOR_TENSOR_ARITH / EXTENDED_INST / TENSOR_REDUCE_BITVEC / COPY / CAST /
GET_SEQUENCE_BOUNDS / NONZERO / TENSOR_DEQUANTIZE. The compute bytes `0x41=TT_ARITH`,
`0x46=COPY`, `0x47=CAST` are exactly the §2 `base|(POOL<<5)` values, plus the extended
POOL set (`0x7x`). **A POOL opcode in the `.bin` stream is the same number the Q7 core
looks up — no translation layer; the byte is the index key.** `[HIGH — both surfaces
OBSERVED/CARRIED]`

### 8.2 The custom-op entry = `EXTENDED_INST 0xF0`

Opcode `0xF0` has **five `kernel_info_table` rows** differing only by the `spec` byte
(`spec 0,1,2,4,3`) — a two-level dispatch: the POOL scan matches `0xF0`, then the row's
kernel sub-selects on `spec`. `spec1 = pool_extended_inst_copy`,
`spec2 = decode_extended_inst_tensor_tensor_arith` (both confirmed by `.xt.prop` section
names); `spec 0/3/4 = EngineNop / Cptc / Rand` band (`[MED]`). **This is where a GPSIMD
custom op lands**: the NEFF's `ucode_lib` bytes are DMA'd into Q7 IMEM as the kernel, and
the POOL stream's `EXTENDED_INST 0xF0(spec)` dispatches into that loaded kernel via the
table. Within the custom-op family, opcode `0x85` (`= -123`) selects a built-in op; any
other selects a user ExtISA op (`total_cpus` ∈ {1, 8}). `[HIGH — table rows CARRIED;
spec↔variant 1/2 HIGH, 0/3/4 MED]`

> **NOTE — engine-polymorphism: the same opcode, two datapaths.** `0x41`/`0x43`
> (`TENSOR_TENSOR` / `TENSOR_SCALAR_ARITH`) run on **GpSimd** (a POOL Q7 kernel) *or* on
> the hardwired **Vector (DVE)** datapath; the compiler routes int32 add/sub/mul to
> GpSimd (POOL), fp to Vector. So a POOL `.bin` carrying `0x41` *is* the GpSimd route; the
> Q7 `kernel_info_table` `funcVA` for `0x41` (`0x01000f1c`, the `tensor_tensor_arith`
> trampoline) is the per-lane integer kernel. `[CARRIED HIGH]`

---

## 9. Reconciliation — three dispatch surfaces, one opcode byte `[HIGH]`

The single 1-byte opcode (the `.bin` slot `byte0`) threads through **three** tables, each
a different consumer of the *same number*:

| surface | keyed by | entries | role |
|---|---|---|---|
| (A) `.bin` TPB-seq slot | `byte0 = base\|(eng<<5)` | n/a | shipped TPB enum (§2) — what the compiler emits |
| (B) SEQ ASCII dispatch | `index = opcode − 0x41` | 178 | the SP/SEQ front-end control/fetch layer |
| (C) POOL Q7 `kernel_info_table` | `(opcode<<24)\|(spec<<16)` | 17 / image | the POOL compute kernels |

Consistency checks:

- **`0x41 TENSOR_TENSOR_ARITH`** — (A) POOL base `0x01` → `0x41`; (B) SEQ idx
  `0x41−0x41 = 0` → "Tensor-Tensor"; (C) kit opcode `0x41` → `tensor_tensor_arith`. **AGREE.**
- **`0x7e IOTA` / `0x7c-0x7d CROSS_LANE_REDUCE` / `0x7b DEQUANT` / `0xf2 NONZERO` /
  `0xf0 EXTENDED_INST`** — present and consistent in both (B) and (C). **AGREE.**
- **`0xC1`/`0xC8` PSEUDO** — present in **(A) only**; absent from (B) and (C) because they
  are relocated away before any device decode (§4). **AGREE.**

> **GOTCHA — (B) and (C) are *not* byte-identical maps; do not assume (B)==(C).** At
> overlapping bytes they diverge (e.g. SEQ `0x46` is a "Tensor-Reduce"-band control entry
> while the Q7 kit `0x46` is `pool_copy`). (B) is the **SEQ control/fetch layer**; the
> matched compute pair is **(A)+(C)**. They overlap in range but are distinct
> opcode-consumers (the "two SPs" of §3.2). Per-byte reconciliation of all 178 SEQ entries
> vs the per-image kits is out of scope here. `[HIGH]`

---

## 10. PE / ACT / POOL / DVE / SP program differential `[HIGH/OBSERVED + INFERRED]`

| engine | `.bin` | slot | opcode classes carried | role |
|---|---|---|---|---|
| **PE** | `pe.bin` | 64-B | PE(`LDWEIGHTS`/`MATMUL`) + ALL + RT | systolic matmul; in the sample, only an RT pseudo (`TRIGGER_COLLECTIVE`) — placed on PE, **runs on TOP SPs**. |
| **ACT** | `act.bin` | 64-B | ACT(`ACTIVATE`/`ACTIVATE_QUANTIZE`) + ALL + RT | activation/PWL; carries the `act_info`/PWL side tables. **Empty in sample.** |
| **POOL** | `pool.bin` | 64-B | POOL(`0x41`..`0x5E` compute) + ALL + RT(DMA triggers) | **the GPSIMD attach engine.** Compute opcodes → Q7 `kernel_info_table`; DMA pseudos move HBM↔SB; custom op = `0xF0`. The 8-core ncore2gp cluster hangs off POOL. |
| **DVE** | `dve.bin` | 64-B | POOL-shaped vector ops on the Vector datapath + ALL + RT | data-vector engine (reshape/transpose/gather, max8/sort). **Empty in sample.** |
| **SP** | `sp.bin` | **8-B** | SP ISA (`WRITE`/`WAIT`/`BRANCH`/`CNTR`/`AR`) | per-core SYNC sequencer — a **distinct 8-B ISA** (§3.2), **not** a 64-B engine. **Empty in sample.** |

**Key differences:**

1. **SP is the odd one out** — 8-byte slots, 1-byte `{opcode:7, phase:1}` header, its own
   opcode table. A control sequencer, not a 64-B data engine. `[HIGH/OBSERVED]`
2. The **four data engines share** the 64-B slot + `TONGA_ISA_TPB_OPCODE` format; they
   differ only in *which* engine-class opcodes are legal (bits[7:5]) and the per-opcode
   payload. ACT additionally carries large side tables (PWL). `[HIGH]`
3. **Only POOL bridges to the Vision-Q7 GPSIMD cluster** (`kernel_info_table` + `ucode_lib`
   + `EXTENDED_INST 0xF0`). DVE runs the same vector opcodes on the hardwired Vector
   datapath instead of Q7 (engine-polymorphism, §8). `[HIGH/CARRIED]`
4. **RT-class pseudos** (DMA triggers, collective triggers) can appear in **any** engine
   stream; they are placement hints, relocated to the real executor at load. `[HIGH/INFERRED]`
5. **Missing engines ship a 0-byte `.bin`** → empty-placeholder `instr_set` + a load WARN.
   `act`/`dve`/`sp` empty here. `[HIGH/OBSERVED]`

---

## 11. Open items `[LOW]`

- The exact **8-B NEURON_ISA event-mode numeric enum** (`wait_mode 0x04`, `update_mode
  0x13`) is decoded *operationally* (`$S` wait/inc) from `.asm` + bytes; the formal enum
  constant names for the 8-B dialect were not located as a shipped header (the 4-B TONGA
  event-mode enum *is* shipped). Semantics (GE-wait / increment) are `[HIGH]`; the enum
  *names* are `[LOW]`.
- A **worked POOL-compute slot** (e.g. a real `0x41 TENSOR_TENSOR` with its 4D
  `MEM_ACCESS` payload) is *not* in this 5-file sample — the sample's POOL stream is only
  DMA triggers + a semaphore gate. The 4D access-pattern layout is carried from the
  container/struct anchor; a byte-worked POOL-compute slot awaits a richer NEFF.
- The `spec`↔`ExtendedInst`-variant pairing for `0xF0` specs `0/3/4` is `[MED]`; specs
  `1/2` are `[HIGH]`.

---

## Cross-references

- [Assembly pipeline](assembly-pipeline.md) — the toolchain stage that **emits** these
  64-B words; the `opcode = base|(engine<<5)` rule and the 64-B layout are the shared
  anchor with this page.
- [Relocation & weight patching](relocation-weights.md) — the `nrt_load` pass that
  **patches** the RT-class pseudos (§4) into concrete `WRITE`s.
- [NEFF container byte format](container-byte-format.md) — how the engine `.bin` streams
  are packed into the NEFF tar.
- [SEQ decode / dispatch hub](../firmware/seq/dispatch-hub.md) — the on-device front-end
  that **decodes** the stream (surface B, §9).
- [`kernel_info_table` binary layout](../firmware/pool/kernel-info-table.md) — the POOL
  Q7 Stage-B dispatch (surface C, §8): `funcVA`s, the `(opcode,spec)` key, the `0xF0`
  five-spec rows.
- [BIR instruction roster](../compiler/bir-inst-roster.md) — the compiler-side BIR→ISA map
  that chooses the opcode bytes this page decodes.
