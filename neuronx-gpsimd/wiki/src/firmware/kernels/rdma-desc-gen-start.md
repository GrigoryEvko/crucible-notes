# RDMA Descriptor Gen/Start — the Peer-to-Peer TX/RX Transport Primitives

This page documents the two GPSIMD POOL-engine firmware primitives that actually move
bytes from one NeuronCore's SBUF to another core's SBUF — across the die boundary when the
peer is on a different Cayman die: **`rdma_desc_gen`** (ExtendedInst sub-opcode **8**) and
**`rdma_desc_start`** (ExtendedInst sub-opcode **9**). `rdma_desc_gen` is the *builder*: it
runs the Q7 SWDGE (software descriptor-generation engine) to pre-fill, for each selected
DMA engine, a CME BD descriptor ring plus two extra semaphore-increment descriptors (one
**local**, one **remote**). `rdma_desc_start` is the *launcher*: it drains the descriptor
writes, splits this core into a **TX (sender)** or **RX (receiver)** role by a PRID-parity
check, and rings the per-queue **tail-pointer-increment doorbell** — TX hits the **M2S**
queue's `TDRTP_inc`, RX hits the **S2M** queue's `RDRTP_inc` — bracketed by a
**`left_pop`/`right_push`** producer/consumer handshake. The pair is what the
[SB2SB collective](sb2sb-remote-copy.md) (ExtendedInst opcode **6**, the `0xBF` sequencer
path) lowers to: one ring/mesh step = one `rdma_desc_gen` + one `rdma_desc_start`.

This is the **Cadence Tensilica Vision-Q7 NX "Cairo" GPSIMD compute core's** own firmware —
windowed-ABI Xtensa (`ncore2gp`, `IsaMaxInstructionSize=32` → FLIX/VLIW bundles up to
32 bytes). Every device fact below is byte-pinned to a carve re-derived this session from
`libnrtucode_internal.so`; the 64-byte instruction-word layouts are read out of the
Amazon-shipped `aws_neuron_isa_tpb_extended_utils.h` ISA headers and **compile-verified
here** with `_Static_assert`; the doorbell register identity is grounded in the shipped
Cayman CSR JSON. Confidence and evidence tags follow the project
[Confidence & Walls Model](../../reference/confidence-model.md): **HIGH/MED/LOW** ×
**OBSERVED/INFERRED/CARRIED**.

> **NOTE — what was carved this session, and the exact objects used.** The firmware
> container is
> `…/custom_op/c10/lib/libnrtucode_internal.so`
> (sha256 `b7c67e898a116454…` — the FW-26/FW-29/FW-41 anchor, re-verified in-task). The Q7
> POOL DEBUG images are `.rodata`-resident `_get.data` symbols (so **file offset == symbol
> VA**; `.rodata` `Address==Off==0x46b0`, no `.data` delta applies). Carved with `dd`,
> disassembled and byte-read with the native `xtensa-elf-objdump`/`xxd`
> (`XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa Tools 14.09) shipped in the
> gpsimd-tools package.
>
> | carved object | file off / size | sha256 (first 8) | role |
> |---|---|---|---|
> | `CAYMAN_Q7_POOL_DEBUG_IRAM` | `0x249020` / `0x1ea40` (125 504 B) | `513a8a22` | both fn bodies + TX/RX block |
> | `CAYMAN_Q7_POOL_DEBUG_DRAM` | `0x267a60` / `0x15d00` (89 344 B) | `226f4254` | the log strings + role/queue tokens |
> | `CAYMAN_NX_POOL_DEBUG_IRAM` | `0x1b1420` / `0x1c820` (116 768 B) | `8e4412b9` | SB2SB/`0xBF` sequencer anchor |
> | `CAYMAN_NX_POOL_DEBUG_DRAM` | `0x1cdc40` / `0x6f20` (28 448 B) | `7bdf6ed7` | sequencer error strings |
>
> The raw blob begins with the reset vector — bytes `06 7f 00 00` at offset 0 = `j 0x200`
> (`op0=0x6`), confirming it is a fully-linked firmware image, **not** an ELF: it carries
> no `.xt.prop` FLIX property tables and no `.symtab`. The Q7 IRAM sha `513a8a22` and DRAM
> sha `226f4254` reproduce the FW-29 anchor exactly; the RDMA function bodies + the TX/RX
> block all live in the **Q7** carve (the NX carve quoted alongside is the sequencer
> anchor). `[HIGH/OBSERVED — both shas reproduced this task.]`

> **DISASSEMBLY-FIDELITY CAVEAT.** `remote_copy.cpp` is densely-scheduled FLIX VLIW with
> per-function literal pools. Because the carved image has no `.xt.prop`, a linear sweep
> loses bundle sync across literal/FLIX-selector boundaries inside both builders' *setup
> bodies* (those spans render as `.byte`/spurious `ivp_*`). The **scalar** control flow —
> the function entries, the role-determination + TX/RX doorbell block, the `const16`-pair
> log loads, and the tail-pointer-increment stores — decodes **byte-exact** and is tagged
> `OBSERVED` accordingly. Where a span is desynced, the DEBUG build's own verbose log
> strings (which name every descriptor field) and the compile-grade ISA header (which gives
> the 64-byte instruction-word layout) are the authoritative sources. The `l32r` literal
> pool resolves to **negative** PC-relative offsets (the image's true load VA is the
> non-zero IRAM base, not 0), so the **absolute** doorbell-register and inc-count *values*
> cannot be read from the raw blob at `adjust-vma=0`; they are bound by reconciliation with
> the CSR JSON (`[MED]`). `[HIGH/OBSERVED — desync boundaries re-confirmed this task.]`

---

## 1. The headline

`rdma_desc_gen` and `rdma_desc_start` are sub-opcodes **8** and **9** of the Q7
ExtendedInst opcode space. Each is a first-class 64-byte TPB instruction word with its own
union member (`ExtendedRdmaDescGen` / `ExtendedRdmaDescStart`); the SB2SB collective
(opcode 6) **composes** them — it is the high-level all-reduce/all-gather leg, these two are
the transport. The decomposition is:

| | `rdma_desc_gen` (op 8) | `rdma_desc_start` (op 9) |
|---|---|---|
| firmware entry (Q7 IRAM) | `0x161f4` | `0x1723c` |
| `entry` frame | **`0x2540`** (9.5 KiB — holds `shuffled_sbuf_swizzle[16]` / `xt_addrs[16]`) | **`0x100`** (256 B) |
| job | run SWDGE → build per-engine CME BD ring + **local**+**remote** sema descriptors | drain → role-split → ring the tail-pointer doorbell |
| operands | full `{local_sem, remote_sem, remote_core_id, remote_routing_id, dma_engine_mask, src_addr, dst_addr, free_dim_bytes, …}` | **none of its own** — consumes the ring `gen` already built |
| critical-path? | **off** — "can happen earlier without waiting" (header) | **on** — carries the `events` wait/update that gates the launch |
| completion | posts the two sema descriptors into the ring | the TX/RX doorbell + the `events` arrive |

`[HIGH/OBSERVED — entries by `entry a1,imm` scan, struct split compile-verified §2.]`

Both byte anchors confirm directly in the carved IRAM: `0x161f4` = `36 81 4a …`
(`entry` opcode `0x36`; frame field `0x4a8 << 3 = 0x2540`), `0x1723c` = `36 01 02 …`
(`0x20 << 3 = 0x100`). `[HIGH/OBSERVED — bytes read from `q7_iram.bin`.]`

> **GOTCHA — `gen` and `start` are *not* two phases of opcode 6; they are ops 8 and 9.**
> An earlier framing (FW-29) treated them as inner phases of the SB2SB(op-6) decode. They
> *are* called by the SB2SB driver — the call edge is `0x3742 → 0x161f4`, byte-confirmed
> in §3 — but they are **also** their own ExtendedInst ops with their own 64-byte words.
> A reimplementer must expose all three: SB2SB is the collective, gen/start are reusable
> peer-to-peer DMA primitives that any kernel can issue directly (e.g. `gen` early, off the
> critical path, then `start` under a semaphore wait).

---

## 2. The two instruction words — compile-verified 64-byte ISA layout

**Source (Amazon-shipped, compile-grade C header — not a vendor source snapshot):**
`…/custom_op/c10/include/neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_extended_utils.h`
(byte-identical siblings exist under `neuron_{sunda,mariana,maverick}_arch_isa/…`). The
header's own banner reads *"ISA header for NC-v3"* (Cayman = NC-v3).

The ExtendedInst opcode enum (`NEURON_ISA_TPB_EXAMPLE_EXTENDED_OPCODES1`), read verbatim:

```c
EXTENDED_ENGINE_NOP          = 0
EXTENDED_COPY                = 1
EXTENDED_TENSOR_TENSOR_ARITH = 2
EXTENDED_RAND_SET_STATE      = 3
EXTENDED_RAND_GET_STATE      = 4
EXTENDED_SBUF_TO_SBUF        = 6   // the SB2SB collective (sb2sb-remote-copy)
EXTENDED_CPTC_DECODE         = 7
EXTENDED_RDMA_DESC_GEN       = 8   // *** rdma_desc_gen — this page
EXTENDED_RDMA_DESC_START     = 9   // *** rdma_desc_start — this page
```

`[HIGH/OBSERVED — header enum bytes; note value **5 is absent** from the enum.]`

> **COMPILE-VERIFICATION (this task).** Both structs were compiled against the shipped
> header (with `aws_neuron_isa_tpb_extended.h` included first for `EXT_COMPLETION_INFO` /
> `EXTENDED_STRUCT`) and checked with `_Static_assert`: `sizeof == 64` for **both**, and
> every member offset below asserted with `__builtin_offsetof` — **all PASS**. Repeated
> against `sunda` (NC-v2), `mariana` (NC-v4), and `maverick` (NC-v5) headers: **all four
> gens PASS identically.** `[HIGH/OBSERVED — `gcc -c` clean on all four; `extended_opcode`
> at off 12 confirmed for both.]`

### 2.1 `NEURON_ISA_TPB_EXTENDED_RDMA_DESC_GEN_STRUCT` (64 B)

| off | size | field | C type | meaning |
|---|---|---|---|---|
| 0 | 4 | `header` | `NEURON_ISA_TPB_HEADER` | `{opcode, inst_word_len, debug_cmd, debug_hint}` |
| 4 | 8 | `events` | `NEURON_ISA_TPB_EVENTS` | per-instr arrive/wait `{wait/update mode+idx, sem_value u32}` |
| 12 | 1 | `extended_opcode` | `EXTENDED_OPCODES1` | **== 8** (RDMA_DESC_GEN) |
| 13 | 1 | `completion_info` | `EXT_COMPLETION_INFO` | `has_read=0, has_write=0, num_active_ports=0` (128 part) |
| 14 | 1 | `local_sem` | `uint8_t` | LOCAL semaphore index (§5 LOCAL sema descriptor) |
| 15 | 1 | `remote_sem` | `uint8_t` | REMOTE semaphore index (§5 REMOTE sema descriptor) |
| 16 | 4 | `remote_core_id` | `IMM_VAL_INST_FIELD` | **physical** id of remote core (from GpSimd register) |
| 20 | 4 | `remote_routing_id` | `IMM_VAL_INST_FIELD` | **routing** id of remote core → SoC high bits (§6) |
| 24 | 4 | `dma_engine_mask` | `IMM_VAL_INST_FIELD` | 16-bit mask: which DMA engines (pow-of-2 1/2/4/8/16) |
| 28 | 4 | `src_addr` | `NEURON_ISA_TPB_ADDR4` | local SBUF partition offset |
| 32 | 4 | `dst_addr` | `NEURON_ISA_TPB_ADDR4` | remote SBUF partition offset |
| 36 | 4 | `free_dim_bytes` | `uint32_t` | bytes/partition to move (**0 ⇒ semaphore-only**) |
| 40 | 1 | `is_bidirectional` | `uint8_t` | enable 2-peer send — **currently NOT supported** (header) |
| 41 | 1 | `remote_sem_prev` | `uint8_t` | prev neighbor's sema (bidirectional mode) |
| 42 | 2 | `reserved0[2]` | | pad |
| 44 | 20 | `reserved1[20]` | | pad |

Header prose, verbatim (the authoritative semantics): *"Pre-generates DMA descriptors for
peer-to-peer remote DMA transfers… Uses Q7 ucode with SWDGE to generate descriptors
including data transfers and semaphore updates. Currently supports: Transfer across all 128
SBUF partitions simultaneously; Contiguous buffers `[src_addr, src_addr+free_dim_bytes]`;
Single DMA queue per engine; Power-of-2 # of DMA engines (1/2/4/8/16) by `dma_engine_mask`.
Semaphore semantics: `local_sem` incremented by all 16 DMA engines when local gpsimd
finishes triggering DMA, releases handle on local buffer so it can be written into again;
`remote_sem` incremented by # of DMA engines when all bytes arrive at remote core's data
buffer, notifies remote engines `dst_buffer` is ready to read."* `[HIGH/OBSERVED — header
comment; matches the firmware logs §3 verbatim.]`

### 2.2 `NEURON_ISA_TPB_EXTENDED_RDMA_DESC_START_STRUCT` (64 B)

| off | size | field | meaning |
|---|---|---|---|
| 0 | 4 | `header` | `{opcode…}` |
| 4 | 8 | `events` | the per-instr semaphore WAIT/UPDATE — *"Semaphore wait conditions are typically placed on RdmaDescStart; RdmaDescGen can happen earlier without waiting"* |
| 12 | 1 | `extended_opcode` | **== 9** (RDMA_DESC_START) |
| 13 | 1 | `completion_info` | `has_read=0, has_write=0, num_active_ports=0` |
| 14 | 2 | `reserved0[2]` | |
| 16 | 48 | `reserved1[48]` | **start carries NO operands of its own** — it consumes the ring `gen` already built |

Header prose, verbatim (the authoritative execution flow): *"Triggers the remote DMA
transfer by sending DMA tail pointer increment. Notifies SDMA engines to consume all
descriptors generated by RdmaDescGen. Execution flow: 1. Gpsimd sequencer sends DMA trigger
(tail ptr incr) to involved SDMA engines; 2. SDMA engines read data from local SBUF and
write to remote SBUF — Data traverses PCIE/RMVT/D2D links if cores are not HBM neighbors;
uses on-chip routing if cores are not one-hop from each other; 3. Local semaphore updated
when local DMA engine finishes receiving all packets so that the source memory can be
released; 4. Remote semaphore updated when all bytes fully transferred to remote core."*
`[HIGH/OBSERVED.]`

> **NOTE — `start` is operand-free; all transfer state lives in the ring `gen` produced.**
> This is the architectural reason `gen` can be hoisted off the critical path: the only
> live state `start` needs is the per-engine ctx/ring (the descriptor stream and the
> `_dma_ctx_t` holding `m2s_inc_reg`/`s2m_inc_reg`) and the `events` wait. A reimplementer
> can issue `gen` arbitrarily early, then gate the *launch* with a single `start` under a
> semaphore wait.

---

## 3. The DEBUG log strings + the call graph — instruction-exact

Each format string lives at a fixed DRAM offset; device VA = offset + `0x80000` (the DRAM
image loads at VA `0x80000`). The logger is `callx8 a5` (`a5` = a printf-like logger fn ptr
loaded once at entry); the format-string device VA is passed in `a10` via a **`const16`
pair** — `const16 a10,8 ; const16 a10,0xNNNN` builds `a10 = (8<<16)|0xNNNN = 0x8NNNN`
(Xtensa `const16` shifts the prior immediate into the high half). Verified byte-exact at the
`gen` Start site: `0x16210` = `a4 08 00` (`const16 a10,8`), `0x16216` = `a4 51 48`
(`const16 a10,0x4851`) → `a10 = 0x84851` = DRAM `0x4851` + `0x80000`. `[HIGH/OBSERVED.]`

The string set, read directly from `q7_dram.bin` at the exact offsets shown (every one
confirmed present this task):

```
--- rdma_desc_gen ---
 0x4851  "P%i: Q7: rdma_desc_gen [%s] Start, cpu_id=%d"
 0x4885  "P%i: Q7: rdma_desc_gen [%s] ring_num=%d, tpb_idx=%d, free_dim_bytes=%d,
          remote_tpb_idx=%d, routing_id=%d, dma_mask=0x%x"
 0x48fe  "… dma_mask=0x%04x, n_active_dmas=%d"
 0x493d  "… src_addr=0x%llx, dst_addr=0x%llx"
 0x497b  "… after remote_routing_id=$%d, dst_addr=0x%llx, n_active_dmas=%d"
 0x49d7  "… Starting descriptor loop, num_indices=%d, num_dma_descs=%d"
 0x4a2f  "… Loop iter %zu/%d: base_addr=0x%llx, n_active_dmas=%d, bytes_per_P=%d"
 0x4a91  "P%i: Q7:   shuffled_sbuf_swizzle[16] = [%u, …x16]"
 0x4afa  "P%i: Q7:   xt_addrs[16] = [0x%08x%08x, …x16]"
 0x4bd6  "… Pushing local  semaphore descriptor, tpb_idx=%d, sem=%d"   <- LOCAL  sema descriptor
 0x4c2a  "… Pushing remote semaphore descriptor, remote_tpb=%d, routing_id=%d, sem=%d"  <- REMOTE
 0x4c91  "… End"
--- rdma_desc_start ---
 0x4cb2  "P%i: Q7: rdma_desc_start [%s] Start, cpu_id=%d"
 0x4ce2  "… ring_num=%d, num_descriptors=%d, sdma_bcast_base=0x%08x"
 0x4d39  "… Trigger %s DMA; addr=0x%08x, mask=0x%04x, n_desc=%d"   (%s = M2S/S2M token)
 0x4d94  "… Draining descriptor writes"
 0x4dce  "… Descriptor writes drained"
 0x4e07  "rdma_desc_start"                                         (fn-name token, error path)
 0x4e27  "P%i: Q7: rdma_desc_start [TX] Waiting for RX sync (left_pop)"
 0x4e65  "… [TX] Writing tail pointer increment"
 0x4ea3  "… [TX] Tail pointer increment written"
 0x4ee1  "… [RX] Writing tail pointer increment"
 0x4f1f  "… [RX] Signaling TX to proceed (right_push)"
 0x4f63  "… [RX] TX signaled"
 0x4f8e  "… End"
 0x5040  "P%i: ERROR: DescriptorStream wrote %d descriptors, expected %d"
--- the role / queue substitution tokens (NUL-terminated, read raw) ---
 0x487f  "TX\0RX\0"     (TX@0x487f, RX@0x4882)   <- the [%s] role token
 0x4d8c  "M2S\0S2M\0"   (M2S@0x4d8c, S2M@0x4d90) <- the %s queue token for "Trigger %s DMA"
```

`[HIGH/OBSERVED — every offset read this task: `strings -t x` for the format strings, `od -c`
for the token blocks; independently re-confirmed by a second carve.]`

> **QUIRK — the DRAM token layout PINS the role→queue mapping byte-exact.** The block
> `…cpu_id=%d\0 TX\0 RX\0` sits immediately after the `gen` Start string, and
> `…n_desc=%d\0 M2S\0 S2M\0` immediately after the Trigger string. Combined with the
> doorbell stores in §4, this proves: **role TX prints "M2S" and drives the M2S
> (outbound, read-from-local) queue; role RX prints "S2M" and drives the S2M (inbound,
> write-to-remote) queue.** No inference needed — the binding is read straight from the
> adjacency of the NUL-terminated tokens. `[HIGH/OBSERVED.]`

### 3.1 The call graph (instruction-exact)

The SB2SB collective driver (the large fn at IRAM `0x3300`) reaches the builder at:

```
0x3742:  25 ab 12      call8 0x161f4        ; *** rdma_desc_gen  (build the ring)
```

This decodes exactly: Xtensa `call8` is `op0 = 0x5, n = 2`; `imm18 = 0x4aac`; target
`= (0x3742 & ~3) + 4 + (0x4aac << 2) = 0x161f4`. `[HIGH/OBSERVED — bytes `25 ab 12`
read and decoded this task; **upgraded from CARRIED to OBSERVED** vs the FW-29 framing.]`
`rdma_desc_start` (`0x1723c`) is reached as the second leg of the SB2SB lowering after
`gen` returns. `[HIGH structure.]`

`rdma_desc_gen` prologue (scalar, byte-exact):

```
0x161f4:  entry  a1, 0x2540          ; 9.5 KiB frame
0x161f7:  movi   a10, -64            ; \  align frame to 64 B
0x161fa:  and    a8, a1, a10         ;  | a8 = a1 & ~63
0x161fd:  movsp  a1, a8              ; /  set aligned SP
0x1620a:  l32r   a5, …              ; a5 = logger fn ptr (callx8 a5 throughout)
0x16210:  const16 a10, 8            ; \  build fmt-VA 0x84851
0x16213:  rsr.prid a11             ;  | a11 = this core's PRID (= cpu_id arg)
0x16216:  const16 a10, 0x4851      ; / → LOG "rdma_desc_gen [%s] Start, cpu_id=%d"
…
0x16b58:  const16 a10, 0x4a91       ; → LOG shuffled_sbuf_swizzle[16]
0x16d64:  const16 a10, 0x4afa       ; → LOG xt_addrs[16]
```

The descriptor-array build + the two semaphore-descriptor pushes (logs `0x4bd6`/`0x4c2a`)
are in the FLIX-scheduled inner loop and desync; their semantics come from §3 logs + §2
header + §5. `[Start/inputs/swizzle/xt_addrs logs HIGH/OBSERVED; inner build MED.]`

`rdma_desc_start` setup (same align prologue, then a 4-way `dma_engine_mask` select):

```
0x1723c:  entry  a1, 0x100
0x1723f:  movi a10,-64 ; and a8,a1,a10 ; movsp a1,a8   ; align
0x172ad:  bnone  a6, a12, 0x172b9     ; \ four bnone a6,a12 / l32r a1 blocks =
0x172b0:  l32r   a1, …               ;  | the per-DMA-engine SELECTION over the mask bits,
0x172b7:  bnone  a6, a12, 0x172c3     ;  | building each engine's queue base / doorbell addr
0x172ba:  l32r   a1, …               ;  | (a12 = the running mask bit)
…  (×4)
0x172d7:  l32r   a3, …               ; a3 = tail-pointer INCREMENT count (= num_descriptors)
0x17354:  const16 a10,0x4d94 ; callx8 a5   ; LOG "[%s] Draining descriptor writes"
0x17365:  const16 a10,0x4dce ; callx8 a5   ; LOG "[%s] Descriptor writes drained"
…  → the role split + TX/RX doorbell (§4)
```

`[entry/prologue/drain logs + the 4-way mask-select *shape* HIGH/OBSERVED; the inner mask
arithmetic MED — FLIX-desynced.]`

---

## 4. The TX/RX protocol + role split — byte-exact

The canonical block, disassembled fresh from the scalar boundary `0x1736b` (every byte
re-read from `q7_iram.bin`). Register roles: `a5` = logger; `a3` = the tail-inc COUNT
(`num_descriptors`); `a4` = the per-role doorbell register address; `a6` = the role flag
(**0 = RX, ≠0 = TX**). **All bytes shown are OBSERVED.**

### 4.1 Drain (before the split)

```
0x17354:  const16 a10,0x4d94 ; callx8 a5   ; LOG "[%s] Draining descriptor writes"
0x17365:  const16 a10,0x4dce ; callx8 a5   ; LOG "[%s] Descriptor writes drained"
```

The drain is the DescriptorStream flush — it guarantees the SWDGE-generated BDs have
landed in the ring **before** the doorbell. `[logs OBSERVED HIGH; that the `callx8` between
them is the flush routine INFERRED — MED.]`

### 4.2 Role determination

```
0x1736b:  78 07      l32i.n a7, a7, 0       ; a7 = the role/parity word (loaded)
0x1736d:  b0 eb 03   rsr.prid a11           ; a11 = this core's PRID
0x17370:  70 70 04   extui  a7, a7, 0, 1    ; a7 = bit0 of the role word (parity)
0x17373:  67 97 75   bne    a7, a6, 0x173ec ; parity != expected role → ERROR path (§4.5)
0x17376:  ac 66      beqz.n a6, 0x173a0     ; *** a6==0 → RX path ; a6!=0 → TX path
```

The `bne` displacement decodes to target `0x173ec` (the error path); the `beqz.n` to
`0x173a0` (the RX path). `[HIGH/OBSERVED — branch targets decoded from the bytes this task.]`

### 4.3 TX path (`a6 != 0` — the SENDER)

```
0x17378:  const16 a10,8
0x1737b:  const16 a10,0x4e27 ; callx8 a5    ; LOG "[TX] Waiting for RX sync (left_pop)"
(0x17388: l32r a4, …                        ; a4 = the M2S TDRTP_inc doorbell addr; MED value)
0x1738f:  const16 a10,0x4e65 ; callx8 a5    ; LOG "[TX] Writing tail pointer increment"
0x17398:  const16 a10,0x4ea3               ; (LOG "[TX] … written")
0x1739b:  39 04      s32i.n a3, a4, 0       ; *** WRITE tail-inc:  *[a4] = a3
                                            ;     = M2S_Q.TDRTP_inc <- num_descriptors
                                            ;     == the SDMA CME COPY LAUNCH
0x1739d:  06 0a 00   j 0x173c9              ; → common END
```

### 4.4 RX path (`a6 == 0` — the RECEIVER)

```
0x173a0:  const16 a10,8
0x173a3:  const16 a10,0x4ee1 ; callx8 a5    ; LOG "[RX] Writing tail pointer increment"
0x173a9:  39 04      s32i.n a3, a4, 0       ; *** WRITE tail-inc:  *[a4] = a3
                                            ;     = S2M_Q.RDRTP_inc <- num_descriptors
                                            ;     (publish empty receive buffers)
0x173ab:  const16 a10,8 ; rsr.prid a11
0x173b1:  const16 a10,0x4f1f ; callx8 a5    ; LOG "[RX] Signaling TX to proceed (right_push)"
0x173c3:  l32r   a12, …                     ; a12 = the right_push signal target (TX go-sema)
                                            ; → falls through to common END
```

### 4.5 Common END + error path

```
0x173c9:  rsr.prid a11 ; callx8 a5          ; (trailing per-core log)
0x173da:  const16 a10,0x4f8e ; callx8 a5    ; LOG "[%s] End"
0x173ea:  1d f0      retw.n                  ; return
--- error @0x173ec ---
0x173ec:  builds "ERROR: DescriptorStream wrote %d descriptors, expected %d" (fmt 0x5040),
          with fn-name token "rdma_desc_start" (0x4e07) → assert/error sink.
```

`[HIGH/OBSERVED — both `s32i.n a3,a4,0` stores (`39 04` at `0x1739b` TX and `0x173a9` RX),
the role-split sequence, `retw.n`, and the error fmt loads all byte-read; the assert-sink
identity INFERRED — MED.]`

### 4.6 The handshake — C pseudocode

The two `s32i.n a3,a4,0` stores are *exactly* the DmaTrigger primitive ("initiate a DMA
data-transfer by writing N to the tail pointer to advance it by N descriptors"). The
`left_pop`/`right_push` wrapping is the two ends of one SDMA producer/consumer ring:

```c
// rdma_desc_start — both ops already built by rdma_desc_gen into the per-engine ring.
// a3 = num_descriptors ; a6 = role (0 == RX/receiver, !=0 == TX/sender)
// a4 = per-role doorbell MMIO addr ; the queue group is selected from dma_engine_mask.

void rdma_desc_start(dma_ctx_t *ctx, uint32_t num_descriptors, int role) {
    descriptor_stream_drain(ctx);                  // 0x17354/0x17365: flush BDs into ring

    uint32_t role_word = *ctx->role_ptr;           // 0x1736b  l32i.n a7,a7,0
    if ((role_word & 1) != role)                   // 0x17373  bne a7,a6 -> 0x173ec
        FATAL("DescriptorStream wrote %d, expected %d");   // 0x173ec error path

    if (role != 0) {                               // 0x17376  beqz.n a6 -> RX ; else TX
        // ---- TX (sender / local source) ----
        wait_for_rx_signal(ctx);                   // "left_pop": WAIT the RX go-sema
        *(volatile uint32_t *)ctx->m2s_inc_reg     // 0x1739b  s32i.n a3,a4,0
            = num_descriptors;                     //   M2S_Q.TDRTP_inc += num_descriptors
                                                   //   == launches the CME COPY (read local
                                                   //      SBUF, stream to remote SBUF)
    } else {
        // ---- RX (receiver / remote sink) ----
        *(volatile uint32_t *)ctx->s2m_inc_reg     // 0x173a9  s32i.n a3,a4,0
            = num_descriptors;                     //   S2M_Q.RDRTP_inc += num_descriptors
                                                   //   publishes the empty receive buffers
        signal_tx_proceed(ctx);                    // "right_push": SIGNAL the TX go-sema
    }
    // 0x173c9 common END, 0x173ea retw.n
}
```

> **QUIRK — RX advances *first*, then signals; TX waits, then advances.** RX is the
> *producer of empty descriptors*: it bumps its S2M tail to arm the receive ring, **then**
> `right_push` signals the sender. TX is the *consumer of that readiness*: it `left_pop`
> **waits** for the RX signal (the `[TX] Waiting for RX sync (left_pop)` log precedes the
> store), **then** bumps its M2S tail — which is the actual CME COPY launch. Ordering RX
> before TX is what prevents the sender from writing into a receive aperture that isn't yet
> armed. `[handshake structure HIGH/OBSERVED (logs + store order); the wait-vs-signal
> *direction* INFERRED from the log wording + ring semantics — MED.]`

> **GOTCHA — `a4` (the doorbell addr) and the `right_push` target are NOT readable from
> the raw blob.** Both ride `l32r` literals that resolve to **negative** PC-relative
> offsets because the image loads at a non-zero IRAM base. At `adjust-vma=0` the absolute
> values cannot be read; their **identity** is pinned from the CSR JSON (§4.7) and the DRAM
> token block (§3), not their literal value. A reimplementer must compute the doorbell SoC
> addr from the queue group the `dma_engine_mask` selects (`dma_apb_bcast{m2s_tail_ptr,
> s2m_tail_ptr}`), not expect it as an immediate. `[MED.]`

### 4.7 The doorbell registers — grounded in the Cayman CSR JSON

The two stores hit the per-queue tail-pointer-increment doorbells. From the shipped Cayman
CSR JSON (`csrs/sdma/udma_m2s.json`, `udma_s2m.json`):

| | register | within-group offset | absolute (queue 0) | access | `val` field | semantics |
|---|---|---|---|---|---|---|
| **TX → M2S** | `TDRTP_inc` | `0x038` | **`0x1038`** | RW | `val[23:0]` | *"Increments the value in Q_TDRTP (descriptors)"* |
| **RX → S2M** | `RDRTP_inc` | `0x038` | **`0x1038`** | WO | `val[23:0]` | *"Increments the value in Q_RDRTP … in number of descriptors"* |

Both queue groups are `M2S_Q` / `S2M_Q` at group base `AddressOffset 0x01000`,
`BundleSizeInBytes 4096`, **`ArraySize 16`** — i.e. 16 M2S queues mirrored by 16 S2M queues,
so the per-queue doorbell for queue *q* is `0x1000 + q*0x1000 + 0x038`. `[HIGH/OBSERVED —
register names, the `0x038` within-group offset, the 24-bit `val` width, and the 16-queue
array all read from the CSR JSON this task; the `+0x1038` absolute for queue 0 = group base
`0x1000` + `0x038`.]`

> **NOTE — `TDRTP_inc` is RW, `RDRTP_inc` is WO.** The increment registers are
> *write-only* on the S2M side and read-write on the M2S side; `val` is a 24-bit
> descriptor count (`[23:0]`, upper 8 bits reserved). The store is a single `s32i.n` of
> `num_descriptors` — there is no read-modify-write, the hardware does the add. The
> `dma_engine_mask` being a power of 2 (1/2/4/8/16) means the firmware can broadcast one
> tail-inc to a *group* of queues (`sdma_bcast`), launching a multi-engine move with one
> doorbell write (§5.2). `[HIGH/OBSERVED — CSR access types + bitfields.]`

### 4.8 The `events`-field semaphore (instruction-level arrive/wait)

`rdma_desc_start`'s `events` field (off 4) is where the wait/update semaphore lives — the
header states *"Semaphore wait conditions are typically placed on RdmaDescStart; RdmaDescGen
can happen earlier without waiting."* So `gen` is **free** (issuable early, off the critical
path) and `start` carries the per-instruction arrive/wait that gates the launch and posts
completion — the on-chip end of the NCFW counted barrier (§7). `[HIGH/OBSERVED from header;
the NCFW mapping MED.]`

---

## 5. The descriptor ring `rdma_desc_gen` builds

`rdma_desc_gen`, per the §2 header + §3 logs, builds for **each** selected DMA engine (the
`dma_engine_mask`, pow-of-2 1/2/4/8/16; *"Single DMA queue per engine"*) a **CME BD
descriptor ring** that copies the contiguous `[src_addr, src_addr+free_dim_bytes]` span
across all 128 SBUF partitions, **plus two extra semaphore-increment BDs**.

### 5.1 Field placement (`rdma_desc_gen` operand → ring/descriptor)

| `rdma_desc_gen` field (header / log) | → ring / descriptor meaning |
|---|---|
| `dma_engine_mask` (16-bit, pow-of-2) | bitmap of which M2S/S2M queues participate; **16-bit = the 16 S2M queues mirroring 16 M2S** (CSR `ArraySize 16`). 1 engine = 1 BD ring = 1 M2S + 1 S2M queue |
| `ring_num` / `tpb_idx` | which per-engine BD ring / this NeuronCore's TPB index (LOCAL) |
| `remote_tpb_idx` / `remote_core_id` | the PEER NeuronCore (physical id) |
| `routing_id` / `remote_routing_id` | cross-die routing id → SoC high bits (§6). Log `0x497b` "after remote_routing_id=$%d, dst_addr=0x%llx" is exactly the rewrite |
| `src_addr` (log `0x493d`) | descriptor `buf_ptr` (u64 SoC) on the TX/read BD = **local** SBUF SoC addr |
| `dst_addr` (log `0x493d`) | `buf_ptr` on the RX/write BD = **remote** SBUF SoC addr (post routing-id rewrite) |
| `free_dim_bytes` (logs "free_dim_bytes=%d", "bytes_per_P=%d") | descriptor length per BD (bytes/partition; chunked if it exceeds the BD length field) |
| `shuffled_sbuf_swizzle[16]` / `xt_addrs[16]` (logs `0x4a91`/`0x4afa`) | per-engine SBUF partition swizzle + the 16 Q7-window 64-bit SoC addresses (the working tables in the 9.5 KiB `gen` frame) |
| **LOCAL sema descriptor** (log `0x4bd6` `{tpb_idx, sem=local_sem}`) | an extra BD that, on the local engine completing its trigger, increments `local_sem` on **this** core → releases the source buffer |
| **REMOTE sema descriptor** (log `0x4c2a` `{remote_tpb, routing_id, sem=remote_sem}`) | an extra BD that, routed by `routing_id` across the die, increments `remote_sem` on the **peer** when all bytes land |

`[field NAMES + existence HIGH/OBSERVED (header §2 + logs §3); the byte-exact word0/word1
descriptor encoding INFERRED by reconciliation with the DWARF-decoded SDMA_CME_BD_DESC
(16 B = `{word0 length+gen-tag, word1 CME COPY command, buf_ptr u64 SoC}`) — MED, because
the `gen` inner build is FLIX-desynced in this image. The descriptor *format* itself is HIGH
(DWARF-decoded from a different object); only its instantiation HERE is MED.]`

### 5.2 The broadcast launch (`sdma_bcast`)

`rdma_desc_start` logs `sdma_bcast_base=0x%08x` (`0x4ce2`) and `"Trigger %s DMA;
addr=0x%08x, mask=0x%04x, n_desc=%d"` (`0x4d39`, `%s` = M2S/S2M token). `sdma_bcast_base` is
a BCAST M2S/S2M aperture: a single tail-inc write to the broadcast doorbell advances the
tails of the *group* of queues the `dma_mask` selects, launching the multi-engine move with
one trigger. `[sdma_bcast usage + mask HIGH/OBSERVED (logs); the broadcast-group semantics
cross-ref the CSR M2S/S2M `ArraySize 16` + the NCFW `dma_apb_bcast{m2s_tail_ptr,
s2m_tail_ptr, mask}` — HIGH; the exact group cut MED.]`

### 5.3 The two-semaphore + generation-tag completion model

| completion event | mechanism |
|---|---|
| **LOCAL** (source release) | the LOCAL sema descriptor (log `0x4bd6`) increments `local_sem` on this core when the local engine finishes triggering → *"the source memory can be released"* (all 16 engines bump it) |
| **REMOTE** (data-ready) | the REMOTE sema descriptor (log `0x4c2a`), routed by `routing_id` across the die, increments `remote_sem` on the peer when all bytes land → *"notifies remote engines that `dst_buffer` is ready to read"* (incremented by # of DMA engines) |
| **generation-tag poll** | the ring's completion BD carries a 2-bit generation tag; the firmware busy-polls it until it matches the expected generation before reusing a slot. This is the synchronous, polling completion underneath the semaphore — **no interrupt path** for the collective data plane |
| **structural guard** | the DescriptorStream count check: `start`'s role/parity `bne 0x17373 → 0x173ec` and `"ERROR: DescriptorStream wrote %d descriptors, expected %d"` (`0x5040`) is a **HARD error** if SWDGE produced ≠ expected descriptors |

`[two-semaphore semantics HIGH/OBSERVED (headers + logs); the gen-tag poll HIGH by
reconciliation with the DWARF-decoded descriptor; the exact Q7 poll site in this image is
FLIX-desynced — MED.]`

---

## 6. Cross-die addressing — peer rank → SoC address

The SoC address each descriptor `buf_ptr` carries:

| bits | field | meaning |
|---|---|---|
| `[46:0]` | LOCAL | 47-bit per-die byte addr (selects the SBUF byte in the STATE_BUF aperture) |
| `[47]` | DIE | which die of the 2-die Cayman package |
| `[53:48]` | CAYMAN_ID | chip id in the 64-die mesh (2^6) |
| `[54]` | CAYMAN_ID_VALID | `1` ⇒ route by chip id across the inter-die fabric; `0` ⇒ stay on chip |

A neighbor decoder repurposes `[53:48]` as `{NEIGHBOR_RSVD[49:48], EXIT_SENG[50],
EXIT_DIE[51], NEIGHBOR_ROUTE[52], PEB[53]}` with `ID_VALID[54]`. `rdma_desc_gen`'s
`remote_routing_id` (off 20, *"from GpSimd register"*) is the value that, written into
`{CAYMAN_ID, CAYMAN_ID_VALID}` (or the neighbor `EXIT_DIE`/`NEIGHBOR_ROUTE`), turns a local
`dst_addr` into a remote-die SoC address — exactly the `"after remote_routing_id=$%d,
dst_addr=0x%llx"` log (`0x497b`). `remote_core_id` (off 16) is the peer's **physical** id;
`remote_routing_id` (off 20) is its **routing** id — both from GpSimd registers.

The link the transfer traverses, header §2.2 verbatim: *"Data traverses PCIE/RMVT/D2D links
if cores are not HBM neighbors; uses on-chip routing if cores are not one-hop from each
other"* — the `io_d2d` die-to-die data fabric. The remote SBUF must also be programmed into
a Q7-local remapper window before the iDMA can reach it (the `xt_addrs[16]` table holds the
16 per-engine Q7-window views of the peer's SBUF). `[SoC bitfield HIGH (sibling ADDR
report); the `routing_id` → high-bit rewrite INFERRED from the log + bitfield — MED; the
`io_d2d` / PCIE/RMVT/D2D path text HIGH (header).]`
See the planned [cross-die RDMA](../../dma/rdma-cross-die.md) page (forward link — Part 9).

---

## 7. NCFW reconciliation — one ring/mesh step = one gen+start pair

A collective (e.g. ring all-reduce) is a **sequence** of SB2SB legs, one per ring/mesh step
(the NCFW barrier carries `barrier_sema[0..3]`/`target_sema_val[0..3]` — a 4-step counted
barrier). Per step *k*:

1. NCFW (the LX management firmware) selects the algo + per-channel `next_neigh`/`prev_neigh`
   peers, the recv/send/post/dma_compl semaphore SoC addrs, and the
   `dma_apb_bcast{m2s_tail_ptr, s2m_tail_ptr}` doorbell SoC addrs — **these ARE the `a4`
   targets of the §4 TX/RX stores**. The host lowering emits the SB2SB op (opcode 6) which
   the Q7 driver decomposes into:
   - `rdma_desc_gen` (op 8) — build the ring + LOCAL/REMOTE sema descriptors; issuable
     **early**, off the critical path.
   - `rdma_desc_start` (op 9) — drain, role-split, doorbell the M2S(TX)/S2M(RX) tail;
     carries the `events` wait/update that gates the step.
2. The TX/RX handshake (§4): RX advances S2M tail (publish empty buffers) + `right_push`;
   TX `left_pop`-waits then advances M2S tail = the CME COPY launch.
3. On completion (§5.3): the LOCAL sema descriptor bumps `local_sem` (= NCFW
   `dma_compl_sema` / send-buffer release); the REMOTE sema descriptor bumps the peer's
   `remote_sem` (= NCFW `recv_sema` on the next peer). The next step's `rdma_desc_start` on
   the peer **waits** its `recv_sema ≥ target` before issuing — chaining the legs.
4. Cross-die legs set `CAYMAN_ID_VALID`/`EXIT_DIE`/`NEIGHBOR_ROUTE` in `dst_addr` (§6) so the
   copy + sema traverse the `io_d2d` data fabric.

So: **one NCFW ring/mesh step = one SB2SB leg = one `rdma_desc_gen` + one
`rdma_desc_start`** (each potentially fanning out over up-to-16 DMA engines via
`dma_engine_mask`); step-to-step ordering is the NCFW counted semaphore barrier. `[leg↔step
correspondence + sema chaining HIGH from the header + the device decode + the NCFW config;
the exact per-(ctype, topology, world-size) leg SCHEDULE lives in the LX/NCFW firmware, not
decodable from the Q7 image — MED/LOW.]`
See the planned [ring-protocol config command](../../collectives/ncfw/ring-protocol-config-command.md)
(forward link — Part 10).

---

## 8. Per-generation presence

The RDMA TX/RX transport path is present in **all four** shipped generations' ISA headers:
the `EXTENDED_RDMA_DESC_GEN=8` / `EXTENDED_RDMA_DESC_START=9` enum entries and **both
64-byte structs (`sizeof==64`, all offsets)** compile-verify identically under `sunda`
(NC-v2), `cayman` (NC-v3), `mariana` (NC-v4), and `maverick` (NC-v5).

| GEN | ISA header has RDMA ops 8/9? | struct compile-check | Q7 POOL device decode |
|---|---|---|---|
| **SUNDA** (v2) | YES | `sizeof==64`, offsets PASS | header-OBSERVED (device decode not re-carved this task) |
| **CAYMAN** (v3) | YES | `sizeof==64`, offsets PASS | **full byte-exact decode here** (`513a8a22`/`226f4254`) |
| **MARIANA** (v4) | YES | `sizeof==64`, offsets PASS | header-OBSERVED + carried from FW-29 family |
| **MAVERICK** (v5) | YES | `sizeof==64`, offsets PASS | **header-OBSERVED only → interior INFERRED** |

`[header presence + the 4-gen compile-check HIGH/OBSERVED this task; the per-gen *device*
decode is CAYMAN-grounded byte-exact, others CARRIED/INFERRED.]`

> **NOTE — v4+ delta: the RDMA TX/RX path is *not* a late addition.** Unlike the RNG family
> (where LFSR/XORWOW arrive at v3/v4), the RDMA descriptor gen/start ops are present and
> structurally identical in the ISA header all the way back to **SUNDA (v2)** —
> `EXTENDED_RDMA_DESC_GEN=8` / `EXTENDED_RDMA_DESC_START=9`, both 64-byte structs with the
> same field offsets. The compile-check passes byte-for-byte across v2/v3/v4/v5; there is no
> per-gen struct delta to reconcile. `[HIGH/OBSERVED — 4-gen compile-check.]`

> **MAVERICK (v5) interior — header-OBSERVED only → INFERRED.** The MAVERICK ISA header
> carries the same opcode-8/9 enum and both 64-byte structs (compile-verified PASS this
> task), but the MAVERICK Q7 POOL DEBUG firmware body was **not separately carved/disasm'd**
> here — its IRAM RDMA bodies are presumed structurally identical to CAYMAN by header
> equivalence. Treat the MAVERICK *interior* (the exact entry addresses, the TX/RX block
> bytes, the doorbell literal binding) as **INFERRED**, not byte-verified. `[the v4 deltas
> are likewise carried; only CAYMAN is the byte witness.]`

---

## 9. Honesty ledger

**HIGH / OBSERVED (direct disasm byte-read / compile-verified header / CSR JSON):**
- **Carve:** `CAYMAN_Q7_POOL_DEBUG` IRAM (`513a8a22`) + DRAM (`226f4254`), shas reproduced
  this task; reset vector `06 7f 00 00` = `j 0x200`.
- **Opcodes 8/9 + both 64-byte structs:** compile-verified `sizeof==64` + every offset via
  `__builtin_offsetof` PASS, on **all four** gens; the verbatim header semantics.
- **Function entries:** `rdma_desc_gen @0x161f4` (`36 81 4a` = frame `0x2540`),
  `rdma_desc_start @0x1723c` (`36 01 02` = frame `0x100`).
- **SB2SB → gen call edge:** `25 ab 12` @ `0x3742` decoded = `call8 0x161f4` (exact).
- **The full gen/start log string set** at the exact DRAM offsets; the `const16`-pair
  fmt-VA convention byte-exact (`a4 08 00` / `a4 51 48` = `0x84851`).
- **The TX/RX role-split + doorbell block byte-exact:** `78 07` (l32i.n role load),
  `70 70 04` (extui bit0), `67 97 75` (bne parity → `0x173ec`), `ac 66` (beqz role split →
  `0x173a0`), **both** `39 04` (s32i.n a3,a4,0 tail-inc) at `0x1739b` (TX) and `0x173a9`
  (RX), the `[TX]`/`[RX]` log sequence, `1d f0` retw.n.
- **The role→queue token mapping:** DRAM `TX\0RX\0` @ `0x487f` and `M2S\0S2M\0` @ `0x4d8c`
  (read with `od -c`), proving TX→M2S / RX→S2M.
- **The doorbell registers:** `TDRTP_inc` (M2S, RW) / `RDRTP_inc` (S2M, WO), both at group
  offset `0x038` (absolute `0x1038` for queue 0), `val[23:0]` "in descriptors", `M2S_Q`/
  `S2M_Q` `ArraySize 16` — all from the Cayman CSR JSON.

**MED (strong inference, often across a documented FLIX/literal desync):**
- The `gen` inner build (per-engine descriptor word0/word1 encoding, the LOCAL/REMOTE sema
  descriptor push sites, the swizzle/xt_addr table fills) — from logs + header + the
  DWARF-decoded descriptor, not instruction-exact (the inner loop FLIX-desyncs in this
  property-table-less image).
- The exact doorbell register **value** loaded into `a4` and the `right_push` target — the
  literal pool resolves to **negative** PC-relative offsets (non-zero IRAM load base), so
  the absolute values are bound by reconciliation, not read from the blob.
- `left_pop`=wait / `right_push`=signal direction (from log wording + ring semantics).
- `remote_routing_id` → SoC high-bit rewrite; the 4-way `dma_engine_mask` select arithmetic.
- The leg↔NCFW-step correspondence + counted-barrier chaining.

**LOW / UNRECOVERED:**
- Concrete remapper-window CSR offsets and the absolute per-queue doorbell SoC addresses are
  not literal in this image (same gap the SB2SB/CCL leg flagged).
- The exact pre-doorbell barrier form (`memw` vs the logged flush `callx8`) — no clean
  inline `memw` found; likely FLIX-bundled or a helper.

---

## Cross-references

- [SB2SB Remote-Copy Collective](sb2sb-remote-copy.md) — the opcode-6 collective (the
  `0xBF` sequencer path) that lowers to these two ops; the call edge `0x3742 → 0x161f4` is
  byte-confirmed above.
- [Cross-die RDMA](../../dma/rdma-cross-die.md) — the `io_d2d`/PCIE/RMVT/D2D fabric the
  copy + sema traverse. *(planned — Part 9)*
- [UDMA M2S CSR](../../control/csr/udma-m2s.md) — the `TDRTP_inc` tail-pointer doorbell the
  TX store rings. *(planned)*
- [UDMA S2M CSR](../../control/csr/udma-s2m.md) — the `RDRTP_inc` tail-pointer doorbell the
  RX store rings. *(planned)*
- [NCFW ring-protocol config command](../../collectives/ncfw/ring-protocol-config-command.md)
  — supplies the `dma_apb_bcast{m2s_tail_ptr, s2m_tail_ptr}` doorbell addrs and the
  per-step peers/semaphores. *(planned — Part 10)*
- [Confidence & Walls Model](../../reference/confidence-model.md) — the tag system used
  throughout this page.
