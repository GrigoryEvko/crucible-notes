# DMA-Family Encoding & Descriptors

> *All symbols and addresses on this page apply to `neuronx_cc` 2.24.5133.0+58f8de22 (cp310 wheel; cp310/11/12 are byte-identical for the C++ core logic). The ten DMA-family encoders, the `assign64bitAddr` ADDR8 base writer, and the dynamic-AP packager all live in `libwalrus.so` (`.text`/`.rodata` base `0x62d660`/`0x1c72000`, VA == file offset; build-id `92b4d331…`, md5 `1d93972b81e619ce6d178a0e4b9003b3`). The BIR enums (`bir::DGEType`, `bir::DMAQoSClass`, `NEURON_ISA_TPB_DTYPE`) live in `libBIR.so` (`a9b1ea38`). Treat every address as version-pinned. See [Build & Version Provenance](../reference/versions.md).*

## Abstract

This page is the byte-for-byte field map of every instruction the **DMA subsystem** emits onto the wire — the descriptors that move tensors between HBM/DRAM ([1.06 DRAM & HBM geometry](../arch/dram-hbm-geometry.md)) and on-chip SBUF/PSUM ([1.05 SBUF/PSUM geometry](../arch/sbuf-psum-geometry.md)). There are **ten** distinct 64-byte bundles, all produced by `CoreV2GenImpl` (the Sunda/arch-20 baseline generator, whose offsets the CoreV3/V4/V5 overrides inherit unchanged for the DMA family). The family is reached from six `visitInst*` entry points plus the embedding/prototype variants:

- **DMA DIRECT2D** (`generateDynamicDMA`) — the 2-D block copy, opcode `0xD4` static / `0xDA` dynamic (HW-DGE / vector-indirect). The `visitInstDMACopy` / `visitInstLoad` / `visitInstSave` paths all funnel here.
- **DMATrigger** (`visitInstDMATrigger`, opcode `0xC1`) — the fire-only queue trigger: queue-name + DMA-block-id + sync, no AP.
- **IndirectLoad / IndirectSave** (`generateIndirectLoadSave`, opcode `0xC4` main / `0xD6` Pool-bound-check) — the index-driven gather/scatter (carries the [2.7 indirect descriptors](indirect-descriptors.md) per-partition address list).
- **IndirectSaveAccumulate** (`visitInstIndirectSaveAccumulate`, opcode `0xCA`) — the embedding-update scatter (per-index accumulate into a table row).
- **SwitchQueueInstance** (opcode `0xCF`) / **ResetQueueInstance** (opcode `0xC2`) — the queue-context micro-ops that re-point / re-arm the trigger engine at a descriptor-queue instance.
- **IndirectCopy** (opcode `0xE7`, TRN1 prototype) and **MoveShape** (opcode `0xB2`, the transpose shape-register descriptor) round out the family.

Cutting across all of them is **`assign64bitAddr`** (`@0x1260290`): the 8-byte `NEURON_ISA_TPB_PSEUDO_ADDR8` writer whose mode byte selects **16-bit physical** (static), **32-bit immediate** (DGE) or **40-bit offset-register** (DGE) addressing, and **`assembleDynamicInfo`** (`@0xf20230`): the codegen-time packager that turns a runtime offset into a symbolic `bir::DynamicAPINFO` (ties to [5.25 register-ALU materialization](../penguin/)).

The bar for this page: a reader can **byte-encode any DMA instruction and its descriptor by hand, including dynamic addressing** — for each control byte its offset, width, semantic, value source, the assert/`*_STRUCT` string that *names* it, and the disassembly store-site that pins it. Every field row carries a confidence tag (CONFIRMED = exact store disassembled; STRONG = LUT/predicate/sibling cross-checked; INFERRED = zero-init or relative-offset implied, no direct store; SPECULATIVE). No field name is fabricated — every named wire field is either a `setupSyncWait/Update<…_STRUCT>` template string or an `instr.<field>` assert literal recovered from `.rodata`.

## At a glance

The whole family shares the 4-byte header skeleton of [2.1 the bundle](instruction-bundle.md) (`opcode` / `inst_word_len=0x10` / reserved), then a sync header at `+0x04..+0x0B`, then the op-specific body. Ten opcodes, all CoreV2 ISA immediates (= the census-map keys):

| Op | dec | Encoder (libwalrus) | Wire struct | Body summary |
|---|---|---|---|---|
| `0xD4` | 212 | `generateDynamicDMA` (static) `@0x1276b10` | `PSEUDO_DMA_DIRECT2D_STRUCT` | src/dst `ADDR8` + 2 step/num pairs/side + elem_size + dtype |
| `0xDA` | 218 | `generateDynamicDMA` (dyn) `@0x1276b10` | DIRECT2D (DGE / idx variant) | adds bound-size / idx-start-addr / DGE compute-op |
| `0xC1` | 193 | `visitInstDMATrigger` `@0x1229710` | `PSEUDO_DMA_TRIGGER_STRUCT` | queue-name@`+0x0C` + block-id@`+0x30` + sync |
| `0xC4` | 196 | `generateIndirectLoadSave` (main) `@0x1268c00` | indirect gather/scatter | indirect_*/direct_* + direction + sem_wait_value |
| `0xD6` | 214 | `generateIndirectLoadSave` (bound) `@0x1268c00` | Pool-engine bound-check indirect | bound size + branch-label + TENSOR2D slots |
| `0xCA` | 202 | `visitInstIndirectSaveAccumulate` `@0x1269f00` | embedding scatter | table-base + entry-width + sequence_length |
| `0xCF` | 207 | `visitInstSwitchQueueInstance` `@0x1264240` | `PSEUDO_DMA_SWAP_QUEUE_SET_STRUCT` | queue-name + instance flag@`+0x3B` + swap word@`+0x3C` |
| `0xC2` | 194 | `visitInstResetQueueInstance` `@0x1264830` | `PSEUDO_DMA_REARM_STRUCT` | queue-name + sync (re-arm, no instance bytes) |
| `0xE7` | 231 | `visitInstIndirectCopy` `@0x1275c40` | (TRN1 prototype) | index_addr + data_addr + TENSOR4D AP |
| `0xB2` | 178 | `generateMoveShape` `@0x1213d00` | (transpose shape-register) | DGE-transpose 4-D shape descriptor |

**The three ADDR8 address modes** (the `assign64bitAddr` mode byte `a2[7]`):

| Mode byte | Width | Form | `a2[0:4]` | `a2[4:6]` | Selector |
|---|---|---|---|---|---|
| `0x10` (16) | 16-bit | **physical var-id** (static DMA) | immediate `v26` | phys var-id low16 (0 if SB) | DGE-table-id < 0 **or** `a5` clear |
| `0x20` (32) | 32-bit | **immediate** (DGE/dynamic) | immediate `v26` | DGE table-entry id | AP-mode == 1 (IMMEDIATE) |
| `0x28` (40) | 40-bit | **offset-register** (DGE/dynamic) | RegId | DGE table-entry id | AP-mode == 3 (REGISTER) |

Mode-byte modifiers (OR'd in): `| 0x04` = 2-physical-register DGE; `| 0x40` = move/transpose flag.

**Header skeleton (all ten), from `setupHeader @0x1172120` (vtbl slot `+0x48`):**

```
 byte +0x00  opcode        (per-family immediate)
 byte +0x01  inst_word_len = 0x10  (16 dwords = 64 bytes)
 byte +0x02..+0x03  reserved = 0x0000
```

```asm
1172120:  movzx eax, BYTE [rsi]      ; (caller-staged) opcode source
1172123:  mov   BYTE [rsi+1], 0x10   ; inst_word_len = 16 dwords = 64 bytes
```

So the 16-bit word at `bundle[0:2]` is little-endian `(0x10<<8) | opcode`: `0x10D4` (DIRECT2D), `0x10C1` (Trigger), `0x10CF`/`0x10C2` (queue ops). CONFIRMED — `setupHeader` disassembled; the `inst_word_len` constant store is hard-pinned across the entire TPB ISA ([2.1](instruction-bundle.md)).

> **NOTE — there is no DMA opcode that *means* "dynamic".** Static-vs-DGE is resolved at **two** independent levels: the **address mode byte** inside `ADDR8` (16 physical = static; 32/40 = DGE) and the **descriptor opcode** (`0xD4` 2-D record vs `0xDA` bound/idx record). A DMA can be `0xD4` with a `0x20`-mode immediate ADDR8 (immediate-addressed but still a plain 2-D copy), so neither level alone classifies the instruction — see [The static-vs-DGE distinction](#the-static-vs-dge-distinction).

---

## The bundle lifecycle (shared by all ten)

Every encoder runs the same cycle; the `CodeGenMode` at `this+0x270` (read as `*((_DWORD*)a1+156)`) selects the sink:

1. **Emplace + zero.** `SmallVectorImpl<std::array<u8,64>>::emplace_back`, then a full `memset`/`movups`-zero blanket. *Consequence: every reserved/unwritten byte reads `0x00`.*
2. **Header.** `setupHeader` via the vtable slot `+0x48` (or an inlined devirtualised store — identical wire result).
3. **Sync header.** `setupSyncWait<…_STRUCT>` / `setupSyncUpdate<…_STRUCT>` fill `+0x04..+0x0B` (the per-struct template — the `_STRUCT` name *is* the recovered ISA struct identity).
4. **Body.** the per-opcode field fills mapped below.
5. **Emit.** `findBin(I)` → `fwrite(buf, 1, 0x40, bin)`; then census `++map<u32,u32>(this+480)[opcode]` and engine census via `I+0x90`.

`CodeGenMode` arms: **1 = GENERATE_ISACODE** (fill + `fwrite`); **2 = RUN_ISA_CHECKS** (build the *byte-identical* bundle on a stack frame, feed the ISA checker via `vcall *vtbl[0]`, no `fwrite`); **0 = COLLECT_OPCODES** (insert the opcode into a `DenseMap<Instr*,set<u32>>(this+4)` census, emit nothing); else `reportError "Wrong CodeGenMode…"`. The field maps below are shared between modes 1 and 2; only the destination differs (the offsets are identical — INFERRED for mode 2, from identical field-writer calls / named strings / relative casts).

---

## DMA DIRECT2D — `generateDynamicDMA` (op `0xD4` / `0xDA`)

The 2-D block copy. Wire struct `NEURON_ISA_TPB_PSEUDO_DMA_DIRECT2D_STRUCT`. CoreV2 `generateDynamicDMA @0x1276b10` (bundle base `v64`, a `std::array<u8,64>`). Entry points:

| Entry | addr | QoS-class source |
|---|---|---|
| `visitInstDMACopy` | `@0x127a630` | `*(I+0x1D8)` (the `DMACopy` QoS field) |
| `visitInstLoad` | `@0x127a620` | `*(I+0x150)` |
| `visitInstSave` | `@0x127a640` | `*(I+0x150)` |

Operands: `v13 = arg<AccessPattern>(0)` = **SRC AP**; `v14 = out<AccessPattern>(0)` = **DST AP**. For `DMACopy` (IT 32) the cce/transpose mode (`I+304`) and transpose-output flag (`I+336==1`) are also read to select the descriptor sub-form.

### Static arm — DIRECT2D record (op `0xD4` = 212)

Taken when **not** vector-indirect and **not** reduce-DGE (the L1515 gate: `!HIBYTE(v247) && !v243 && !isDstReduceDGE(I,dstPAP)`). The opcode byte `0xD4` store is pinned at `1277a62: movb $0xd4,…`. Full field map (mode-1 GENERATE arm; base `v64`):

| Off | W | Field | `instr.<name>` / source | Store / cast | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xD4` | `setupHeader` (src byte) | `byte0` (`movb $0xd4 @1277a62`) | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | `setupHeader` const | hdr | CONFIRMED |
| `+0x02` | 2 | format/reserved = 0 | `setupHeader` | hdr | CONFIRMED |
| `+0x04` | 1 | SyncWait mode | `setupSyncWait<…DIRECT2D_STRUCT>` (`sub_122F700`) | sync | CONFIRMED |
| `+0x05` | 1 | `events.wait_idx` | `SyncRef::getId` | sync | CONFIRMED |
| `+0x06` | 1 | SyncUpdate mode | `setupSyncUpdate<…DIRECT2D_STRUCT>` | sync | CONFIRMED |
| `+0x07` | 1 | `events.update_idx` | `SyncRef::getId` | sync | CONFIRMED |
| `+0x08` | 4 | sync wait/update value | `Wait/Update::getValue` | `+8` dword | CONFIRMED |
| `+0x0C` | 1 | **QoS bits[0:2] + flags** | `encodeISAOrRuntimeDMAQoS` / raw `@+0xF0` | `(qos&7)\|(d[12]&0xF8)` | CONFIRMED |
| `+0x0D` | 1 | `instr.semaphore` | `SyncRef::getId` (`sub_12173A0`) | `byte +13` | CONFIRMED |
| `+0x0E` | 1 | `instr.sem_increment` | `Update::getValue` | `byte +14` | CONFIRMED |
| `+0x0F` | 1 | **sync_command_byte** | `3` / `2·BYTE1(v247)` / `1` (transpose) | `byte +15` (`mov %al,0xf(%r14) @1277c2f`) | CONFIRMED |
| `+0x10` | 8 | **SRC `ADDR8`** | `assign64bitAddr(srcAP, v247, 1)` | `v64+1` ([ADDR8](#assign64bitaddr--the-8-byte-addr8-address-base)) | CONFIRMED |
| `+0x18` | 4 | `instr.src_step_elem[0]` | `getAPStepOnHW(src, dim-2)` | `(_DWORD)v64+6` | CONFIRMED |
| `+0x1C` | 4 | `instr.src_step_elem[1]` | `getAPStepOnHW(src, dim-3)` | `(_DWORD)v64+7` | CONFIRMED |
| `+0x20` | 2 | `instr.src_num_elem[0]` | src `APPair[dim-2].num` | `(_WORD)v64+16` | CONFIRMED |
| `+0x22` | 2 | `instr.src_num_elem[1]` | src `APPair[dim-3].num` | `(_WORD)v64+17` | CONFIRMED |
| `+0x24` | 2 | `instr.src_elem_size` | `dtype_bytes(src)×APPair[last].num` (`0x10000→0`) | `(_WORD)v64+18` | CONFIRMED |
| `+0x26` | 1 | **src offset-reg id + flags** | `getRegId&0x3F \| 0x80`; bit6 = `(v248^1)` mode | `(_BYTE)v64+38` | CONFIRMED |
| `+0x27` | 1 | **dst offset-reg id + flags** | `getRegId&0x3F \| 0x80`; bit6 mode | `(_BYTE)v64+39` | CONFIRMED |
| `+0x28` | 8 | **DST `ADDR8`** | `assign64bitAddr(dstAP, MoveShape, 1)` | `v64+40` | CONFIRMED |
| `+0x30` | 4 | `instr.dst_step_elem[0]` | `getAPStepOnHW(dst, dim-2)` | `(_DWORD)v64+12` | CONFIRMED |
| `+0x34` | 4 | `instr.dst_step_elem[1]` | `getAPStepOnHW(dst, dim-3)` | `(_DWORD)v64+13` | CONFIRMED |
| `+0x38` | 2 | `instr.dst_num_elem[0]` | dst `APPair[dim-2].num` | `(_WORD)v64+28` | CONFIRMED |
| `+0x3A` | 2 | `instr.dst_num_elem[1]` | dst `APPair[dim-3].num` | `(_WORD)v64+29` | CONFIRMED |
| `+0x3C` | 2 | `instr.dst_elem_size` | `dtype_bytes(dst)×APPair[last].num` (`0x10000→0`) | `(_WORD)v64+30` | CONFIRMED |
| `+0x3E` | 1 | **SRC dtype** (ISA wire-tag) | `sub_120E650(src.Dtype@+48)` (`byte_1DF5760`) | `(_BYTE)v64+62` | CONFIRMED |
| `+0x3F` | 1 | **DST dtype** (ISA wire-tag) | `sub_120E650(dst.Dtype@+12)` | `(_BYTE)v64+63` | CONFIRMED |

> **The element-span rule.** `*_elem_size = dtype_bytes × num_elements_of_last_dim`. If that product equals `0x10000` (64Ki), it is stored as `0`: the ISA encodes a full 64K-element span as zero. `getAPStepOnHW` (`DescGenHelper @0x11e8ff0`) = `APPair.step × dtype_bytes`, both keyed off the dtype-size LUT `qword_1DF59E0` (`.rodata`; byte `{1,1,2,1,1,1,1,1,4,4,2,2,…}` — disassembled at `1277c66: lea 0x1df59e0(%rip)`). CONFIRMED.

> **QoS is three bits.** `+0x0C[0:2]` carries the DMA QoS class. `supportsDMAQoSOnISA(arch)` gates it: `arch<=49` → `encodeISAOrRuntimeDMAQoS(qosClass,I)` (ISA class-1 encoding); `core_v5>=50` → raw `*(u32)(*(I+30q)+0xF0)`. The store is `descriptor[12] = (qos&7) | (descriptor[12]&0xF8)` — disassembled at `1277ad0: and $0x7,%edx` / `1277ad3: and $0xfffffff8,%eax`. Inferentia (arch 10) and Sunda (arch 20) emit no QoS. CONFIRMED.

### Dynamic / DGE / indirect arm — descriptor op `0xDA` = 218

Re-tagged (`1277586: movl $0xda,…`) when the copy is vector-indirect or HW-DGE. Three sub-forms fold into the one `0xDA` record:

**(A) Vector-indirect gather/scatter** — the mode word `@+1` = `direction{0,1,2} | 0x8000 | 4·!QoS`, index-dtype packed into `+1` bits[4:5] (`sub_1204220`), plus:

| Off | W | Field | Source | Tag |
|---|---|---|---|---|
| `(_DWORD)v104+1` | 4 | `instr.src_idx_start_addr.addr_immediate` | assignAddr(src idx) | CONFIRMED |
| `(_DWORD)v104+2` | 4 | `instr.dst_idx_start_addr.addr_immediate` | assignAddr(dst idx) | CONFIRMED |

Asserts: *"Vector-dynamic-offsets AP must exist"*, *"…location must be SB"*, *"Must have Indirect direction!"*.

**(B) HW-DGE bound-size** — the per-side address bound the runtime validates each materialised address against:

| Off | W | Field | Source | Tag |
|---|---|---|---|---|
| `(_DWORD)v104+3` | 4 | `instr.src_bound_size_bytes` | `**(memloc+264)×**(memloc+256)` | CONFIRMED |
| `(_DWORD)v104+14` | 4 | `instr.src_bound_size_bytes_upper` | (high half) | CONFIRMED |
| `(_DWORD)v104+4` | 4 | `instr.dst_bound_size_bytes` | dst-side product | CONFIRMED |
| `(_DWORD)v104+15` | 4 | `instr.dst_bound_size_bytes_upper` | (high half) | CONFIRMED |

**(C) CCE / reduce compute-op byte** — `desc+3 = AluOpType2DGEComputeOp(I, I+0x178 reduce)` (`@0x120b9e0`): `0` (none) or `1` (add). Gated by `isDstReduceDGE(I,dstPAP)` (libBIR `0x312240`) when `IT==32 && arch<=49`. HARD-PINNED None/Add — `arch>=50` FATALs *"CoreV5 cannot support DGE with compute op yet"*. CONFIRMED.

> **Descriptor-mode folding.** The four BIR descriptor sub-opcodes — COPY(69), REPLICATE(72), TRANSPOSE(71), CCE/reduce(70) — are **not** separately encoded at codegen; they fold into the single DIRECT2D/`0xDA` record via the `sync_command` byte (`+0x0F`: COPY=3, TRANSPOSE=1) and the compute-op byte. TRANSPOSE (IT 32 && `I+336==1`) additionally calls `generateMoveShape` (op `0xB2`) twice for the shape-register descriptor; asserts *"Transpose with DynamicAP only on Pool"*, *"DGE transpose must have 4D AP"*. CONFIRMED.

Pre-asserts (`NeuronAssertion`): *"must have assigned DMA queue already"*, *"must be dynamic DMA"*, *"DGE's number of args/outputs is wrong"*, *"DGE in/out dimensions must match"*, *"DGE fastest moving dim must be continuous"*, *"DMA instruction must read and write same number of elements"*.

---

## DMATrigger — `visitInstDMATrigger` (op `0xC1` = 193) `@0x1229710`

The fire-only trigger. Wire struct `NEURON_ISA_TPB_PSEUDO_DMA_TRIGGER_STRUCT`. Carries **no** src/dst AP and **no** dtype — pure queue-name + block-id + sync. The opcode byte `0xC1` store is pinned at `122997d: movb $0xc1,…`; the block-id at `1229975: movups %xmm0,0x30(%rax)`.

> **The trigger names its queue by lookup, not by carrying it.** The trigger reads `queueKey = *(I+0xF0)` (the `DMAQueue*`) and looks it up in `DenseMap<DMAQueue*,BasicBlock*>(this+54).at(queueKey)` — the **same map** that `visitInstSwitchQueueInstance` / `visitInstResetQueueInstance` / `lower_dma` **populate**. The BB's name (`block+88`) is what gets `strncpy`'d. A `DenseMap::at` miss is a hard *"missing key"* assert, so the queue BB **must** be pre-registered before any trigger.

| Off | W | Field | Source | Store / cast | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xC1` | `setupHeader` | `byte0` (`@122997d`) | CONFIRMED |
| `+0x01` | 1 | inst_word_len = `0x10` | `setupHeader` | hdr | CONFIRMED |
| `+0x02` | 2 | format/reserved = 0 | `setupHeader` | hdr | CONFIRMED |
| `+0x04` | 1 | SyncWait mode | `setupSyncWait<…TRIGGER_STRUCT>` (`sub_1229370`) | sync | CONFIRMED |
| `+0x05` | 1 | `events.wait_idx` | `SyncRef::getId` | sync | CONFIRMED |
| `+0x06` | 1 | SyncUpdate mode | `setupSyncUpdate<…TRIGGER_STRUCT>` (`sub_12290B0`) | sync | CONFIRMED |
| `+0x07` | 1 | `events.update_idx` | `SyncRef::getId` | sync | CONFIRMED |
| `+0x08` | 4 | sync wait/update value | `Wait/Update::getValue` | `+8` dword | CONFIRMED |
| `+0x0C` | 31 | **DMA QUEUE NAME** (asciiz) | `strncpy(bundle+12, queueBB.name, 0x1F)` | `v20+12` | CONFIRMED |
| `+0x30` | 4 | **DMA BLOCK ID** | `InstDMABlock::getBlockId(getDmaBlock(0))` | `(_DWORD)v20+12` (`@1229975`) | CONFIRMED |

Asserts: *"queName.length() < maxSize"* (maxSize=32, the 31-byte slot + NUL), *"DmaTrigger Instr: bad queue name"*, *"id < DmaBlocks.size()"*.

---

## IndirectLoad / IndirectSave — `generateIndirectLoadSave` (op `0xC4` / `0xD6`)

The index-driven gather (LOAD: DRAM→SB) / scatter (SAVE: SB→DRAM). Both `visitInst*` are thin tail-call thunks into `generateIndirectLoadSave(I, isLoad)`:

```
visitInstIndirectLoad  @0x1269ee0 → generateIndirectLoadSave(I, true)   (edx=1)
visitInstIndirectSave  @0x1269ef0 → generateIndirectLoadSave(I, false)  (edx=0)
```

> **The bool is `isLoad`, not `isSave`.** `edx=1` is the **LOAD** path. Operands: `v4 = arg(0)` is always the **INDEX** (offset list) AP; if `!isLoad` the data/dst APs are swapped so `v7` is **always** the DIRECT (data) AP. The data AP carries the [2.7 indirect descriptor](indirect-descriptors.md) the runtime software-materialises into a per-partition address list.

Two descriptor forms, selected by the bound-check feature flag `*(*(this+86)+0x1ED)`:

### Form A — Pool bound-check indirect (op `0xD6` = 214)

The flag is **set**. Allocates a branch-target id and asserts *"Indirect DMA bound-check can only be on Pool engine"*, *"must have 3 args and 2 outputs for bound-check"*. Opcode byte `0xD6` pinned at `1269bd3: movl $0xd6,…`. Base `v70`/`v94`:

| Off | W | Field | Source | Tag |
|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xD6` | `setupHeader` | CONFIRMED |
| `+0x18` | 4 | bound-result reg | `vcall(*(this+76)+32)(ptr)` | STRONG |
| `+0x20` | 8 | bound size bytes | `**(v6.AP+56 +264) × **(+256)` | CONFIRMED |
| `+0x28` | 1 | const tag = `17` (`0x11`) | constant | CONFIRMED |
| `+0x29` | 1 | src dtype `\| 0x30` | `sub_120E650(v4.Dtype@+48)&0xF \| 0x30` | CONFIRMED |
| `+0x2A` | 2 | const = `1` | constant | CONFIRMED |
| `+0x2E` | 1 | idx-AP meta | `*(v4.AP+80 +8)` | STRONG |
| `+0x30` | 4 | branch-label id | branch target id `v73` | CONFIRMED |

Plus two `assignAccess<TENSOR2D>` AP slots in the descriptor body. The Pool-engine bound-loop label is emitted after `fwrite`.

### Form B — main indirect gather/scatter (op `0xC4` = 196)

The dominant path; flag **clear**. Opcode byte `0xC4` pinned at `12694b8: movl $0xc4,…`. Asserts: *"Currently only support 1 address per partition for Indirect DMA"*, *"Currently only support 2D DataAP for Indirect DMA"*, *"Unknown dtype"* (`llvm_unreachable`, dtype index > `0x13`). Base `v22`:

| Off | W | Field (named) | Source | Tag |
|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xC4` | `setupHeader` | CONFIRMED |
| `+0x04..+0x0B` | — | sync header (mode/idx/value) | `sub_12301E0` | STRONG |
| `+0x0C` | 4 | `instr.indirect_start_offset` | assignAddr(idx AP `v4`, `sub_1250E50`) | CONFIRMED |
| `+0x10` | 4 | const = `1` (addr-mode/valid) | constant | CONFIRMED |
| `+0x14` | 4 | engine-config dword | `*(*(this+14)+40)+4` (arch tile stride) | STRONG |
| `+0x18` | 4 | `instr.indirect_num_elems[0]` | `idxAP[0].num` | CONFIRMED |
| `+0x1C` | 4 | `instr.indirect_num_elems[1]` | `idxAP[1].num` | CONFIRMED |
| `+0x20` | 4 | `instr.direct_start_offset` | assignAddr(data AP `v7`) | CONFIRMED |
| `+0x24` | 4 | `instr.direct_step_elems[0]` | `dtype_bytes(v7)×stride` (`sub_124A9D0`) | CONFIRMED |
| `+0x28` | 4 | engine-config dword | `*(*(this+14)+40)+4` | STRONG |
| `+0x2C` | 4 | `instr.direct_num_elems[0]` | `dataAP[0].num` | CONFIRMED |
| `+0x30` | 4 | `instr.direct_num_elems[1]` | `dataAP[1].num` | CONFIRMED |
| `+0x34` | 4 | `instr.direct_elem_size` | `dtype_bytes(v7)×getNumEltsPerPart` | CONFIRMED |
| `+0x38` | 2 | reserved = 0 | constant | CONFIRMED |
| `+0x3A` | 2 | `instr.semaphore_wait_value` | `sub_12055F0(I)` low word | CONFIRMED |
| `+0x3C` | 1 | **direction = `isLoad ^ 1`** | `0`=load, `1`=save | CONFIRMED |
| `+0x3E` | 2 | `instr.semaphore_wait_value_hi` | `sub_12055F0(I)` high word | CONFIRMED |

> **Direction is the inverse of the `isLoad` bool.** `+0x3C = isLoad ^ 1`: a LOAD (gather, `edx=1`) writes `0`; a SAVE (scatter, `edx=0`) writes `1`. The `semaphore_wait_value(+hi)` is the 32-/64-bit completion the trigger semaphore must reach; if the hi word is non-zero the module gets a *"64-bit sem wait value"* feature attr. `indirect_*` = the index AP (one address per partition, 2-D parts×1); `direct_*` = the data AP (2-D parts×elems).

---

## IndirectSaveAccumulate — `visitInstIndirectSaveAccumulate` (op `0xCA` = 202) `@0x1269f00`

The embedding-update scatter: per index, accumulate the update vector into the embedding-table row at `scratch+base`. Opcode byte `0xCA` pinned at `126a308: movl $0xca,…`. Operands: `arg0` = update data, `arg1` = INDEX list, `arg2` = SCRATCH space, `out1` = embedding TABLE. Base `v13`:

| Off | W | Field (named) | Source | Tag |
|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xCA` | `setupHeader` | CONFIRMED |
| `+0x04..+0x0B` | — | sync header | `sub_122FE40` | STRONG |
| `+0x0C` | 1 | index dtype (wire-tag) | `sub_120E650(arg1.Dtype@+48)` (`byte_1DF5760`) | CONFIRMED |
| `+0x2C` | 4 | `instr.embedding_table_base_addr` | assignAddr(arg2 scratch) | CONFIRMED |
| `+0x30` | 2 | `instr.embed_entry_num_elements` | arg1 last-APPair step×num | CONFIRMED |
| `+0x32` | 2 | embed entry byte-size (low) | `dtype_bytes(arg1)×*(I+0x150)` | CONFIRMED |
| `+0x34` | 2 | `instr.sequence_length` | `*(I+0x128)` (u16) | CONFIRMED |
| `+0x38` | 4 | `…table_num_entries.addr_immediate` | `NumEltsPerPart×dtype×row` (bound-check only) | CONFIRMED |
| `+0x3C` | 2 | embed entry byte-size (high) | `HIWORD(v52)` | CONFIRMED |

Plus `assignAccess<TENSOR1D>` (index) / `<TENSOR2D>` (data) / `<TENSOR1D>` (table) AP slots. Assert: *"IndirectSaveAccumulate scratch space address must be 64-byte aligned"*. If the entry byte-size `v52 > 0xFFFF` → module attr *"embedding entry too large for 16-bit field"*. `embed_entry_num_elements` = row width; `sequence_length` = number of indices.

---

## SwitchQueueInstance / ResetQueueInstance — the queue micro-ops

The two queue-management bundles. They carry **no** src/dst AP and **no** dtype — they name their target `DMAQueue` by **string** (the queue's name `@+0x58`) and re-point / re-arm the trigger engine. Both register the `DMAQueue → BasicBlock` binding into `DenseMap(this+0x1B0 / this+54)` (key `*(I+0xF0)` = `DMAQueue*`, value `*(I+0xF8)` = `BasicBlock*`) — the map the trigger later reads. Both `checkQueueNameLen(name, 0x20)` FATAL if the name is ≥ 32 bytes.

> **`I+0xF8` means different things on different instructions.** On `InstDMA` (Copy/Load/Save) `I+0xF8` is the `DGEType`. On the queue-instance instructions `I+0xF8` is the `DMAQueue` object and `I+0xF0` is the `DMAQueue*` map key. Different instruction layouts — do not conflate them.

### SwitchQueueInstance (op `0xCF` = 207) `@0x1264240`

Wire struct `NEURON_ISA_TPB_PSEUDO_DMA_SWAP_QUEUE_SET_STRUCT`. Sets a module feature attr (attr#5) *"swap-queue-set used"*. Opcode byte `0xCF` pinned at `12644b9: movb $0xcf,…`. Base `v64`/`r14`:

| Off | W | Field | Source | Store | Tag |
|---|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xCF` | `setupHeader` | `@12644b9` | CONFIRMED |
| `+0x04..+0x0B` | — | sync header | `setupSyncWait/Update<…SWAP_QUEUE_SET_STRUCT>` | sync | CONFIRMED |
| `+0x0C` | 31 | DMA QUEUE NAME (asciiz) | `strncpy(bundle+12, queue->name@+0x58, 0x1F)` | — | CONFIRMED |
| `+0x3B` | 1 | **queue-instance flag** | `*(I+0x100)` | `mov %al,0x3b(%r14) @1264535` | CONFIRMED |
| `+0x3C` | 4 | **sub-id / instance = `1`** (swap marker) | constant 1 | `movl $0x1,0x3c(%r14) @126452d` | CONFIRMED |

### ResetQueueInstance (op `0xC2` = 194) `@0x1264830`

Wire struct `NEURON_ISA_TPB_PSEUDO_DMA_REARM_STRUCT`. The bare "re-arm this queue" command: queue-name + sync only, **no** `+0x3B`/`+0x3C` instance bytes. Opcode byte `0xC2` pinned at `1264db5: movb $0xc2,…`.

> **GOTCHA — a REARM is legal only under a feature flag.** `visitInstResetQueueInstance` opens with a hard pre-assert: `I.getModule()->getAttribute(ModuleAttribute::neff_feature_SQI_no_rearm)`. A `ResetQueue` bundle is only legal when the module carries `neff_feature_SQI_no_rearm`; otherwise `NeuronAssertion` (errcode 1106). `SwitchQueue` **writes** a feature attr; `ResetQueue` **reads** the no-rearm attr — they are the set/clear pair of the Swap-Queue-Instance hardware feature.

| Off | W | Field | Source | Tag |
|---|---|---|---|---|
| `+0x00` | 1 | opcode = `0xC2` | `setupHeader` (`@1264db5`) | CONFIRMED |
| `+0x04..+0x0B` | — | sync header | `setupSyncWait/Update<…REARM_STRUCT>` | CONFIRMED |
| `+0x0C` | 31 | DMA QUEUE NAME (asciiz) | `strncpy(bundle+12, queue->name@+0x58, 0x1F)` | CONFIRMED |

---

## IndirectCopy / MoveShape — the two carried variants

**`visitInstIndirectCopy` (op `0xE7` = 231) `@0x1275c40`** — the TRN1 prototype indirect. Args: `arg0`=data AP, `arg1`=index AP, `out0`=dst AP. Assert: *"INDIRECT_COPY is a prototype instruction of Tensor Indirect on TRN1, it must not have TensorIndirect AP. On TRN3+, use COPY with TensorIndirect AP instead."* Fields: `+0x0C` `index_addr.addr_immediate`; `+0x0F` flag `|= 0x20` (indirect-active); `+0x10` `data_addr.addr_immediate`; `+0x14` num_elem; `+0x20` src dtype; `+0x21` dst dtype; `+0x23` src_num_elem_per_idx; `+0x26` src_buffer_size; + a `TENSOR4D` AP slot. STRONG (opcode byte CONFIRMED; field list carried, not re-disassembled this pass).

**`generateMoveShape` (op `0xB2` = 178) `@0x1213d00`** — the transpose shape-register descriptor, emitted twice by the DIRECT2D transpose sub-form. It encodes the 4-D shape the DGE engine uses to permute the access; the descriptor body mirrors the DIRECT2D step/num pairs against the shape register. STRONG.

---

## `assign64bitAddr` — the 8-byte ADDR8 address base

`@0x1260290`, signature `assign64bitAddr(NEURON_ISA_TPB_PSEUDO_ADDR8& a2, AccessPattern& a3, bool a4=moveFlag, bool a5)`. This writes the 8-byte address field used at DIRECT2D src (`+0x10`) / dst (`+0x28`) and as the `addr_immediate` of the indirect/embedding descriptors. **The static-vs-DGE addressing decision lives here.** Pre-assert: *"Hardware Restriction: HBM accessing instruction cannot have TensorIndirect AP"* (`vcall *(AP+128)`).

### ADDR8 8-byte layout (within the `a2` slot)

| Off | W | Field | Source | Tag |
|---|---|---|---|---|
| `+0` | 4 | address immediate / RegId / var-id | `v26` (imm) / `RegId` (offreg) / var-id | CONFIRMED |
| `+4` | 2 | **DGE table-entry id** (DGE arm) | `*(memloc+142)` | CONFIRMED |
| `+4` | 2 | *(phys arm)* physical var-id low 16 | `v16` (= `*(memloc+141)`, 0 if space==16) | CONFIRMED |
| `+6` | 1 | *(phys arm)* physical var-id high byte | `BYTE2(v16)` | CONFIRMED |
| `+7` | 1 | **ADDRESS-MODE byte** | `16/32/40` + bit2/bit6 | CONFIRMED |

### The mode byte `a2[7]` — the static-vs-DGE selector

The DGE arm is taken when `*(memloc+142) >= 0 && a5`. Within it the AP-mode (`*((_DWORD*)a3+6)`, i.e. `*(AP+24)`) selects immediate vs register:

```c
if (DGE_table_entry_id >= 0 && a5) {                  /* DGE / DYNAMIC addressing */
    if (AP_mode == 1 /*IMMEDIATE*/) {
        a2[7] = 0x20;                  /* 32-bit immediate */   /* movb $0x20,0x7(%r13) @1260478 */
        *(u32*)&a2[0] = v26;           /* the immediate    */
    } else if (AP_mode == 3 /*REGISTER*/) {
        a2[7] = 0x28;                  /* 40-bit offset-reg */  /* movb $0x28,0x7(%r13) @12608f0 */
        *(u32*)&a2[0] = RegId;         /* "DGE's RegisterAP must be offset register" */
        if (Reg.getNumPhysicalRegs() > 1)
            a2[7] |= 0x04;             /* 2-phys-reg DGE */     /* orb $0x4,0x7(%r13)  @1260bc8 */
    }                                  /* sets attr "2-phys-reg DGE"; "RegisterAP 1 or 2 RegAPPairs" */
    *(u16*)&a2[4] = DGE_table_entry_id;                        /* mov %ax,0x4(%r13)   @1260512 */
} else {                                               /* PHYSICAL var-id — STATIC form */
    a2[7] = 0x10;                      /* 16-bit physical */    /* movb $0x10,0x7(%r13) @126059a */
    *(u32*)&a2[0] = v26;
    *(u16*)&a2[4] = v16;               /* phys var-id low16 (0 if memloc space==16, i.e. SB) */
    a2[6]         = BYTE2(v16);        /* phys var-id high byte */
    /* "physical variable mode can only have physical AP"; "physical tensor must be DRAM or SB" */
}
if (a4 /*move/transpose flag*/)
    a2[7] |= 0x40;                     /* bit6 = move/transpose */ /* orb $0x40,0x7(%r13) @1260766 */
```

> **What the mode byte tells you at a glance.** `0x10` = static physical address (DRAM/SB var-id, no DGE table entry). `0x20` = DGE immediate. `0x28` = DGE offset-register. `+0x04` = the register is a 2-physical-register pair (only set under mode `0x28`). `+0x40` = the access is a move/transpose. So a DGE register-addressed transpose with a 2-reg pair encodes mode byte `0x28 | 0x04 | 0x40 = 0x6C`. CONFIRMED — every store-site disassembled above.

Errors that name these arms: *"DGE table entry ID too large"* (the `+4` field overflows), *"DGE's RegisterAP must be offset register"*, *"Register has not been allocated yet!"*, *"physical Var Id is not assigned yet"*, *"physical tensor cannot be Function argument / NEFF IO"*.

### The static-vs-DGE distinction

Resolved at **two** levels, never by one family-level opcode:

| Level | STATIC | DGE / DYNAMIC |
|---|---|---|
| **Addressing** (`assign64bitAddr`) | ADDR8 mode `0x10` (physical var-id, no DGE table entry) | mode `0x20` (immediate) or `0x28` (offset-reg) + 16-bit DGE table-entry id `@ADDR8+4` |
| **Descriptor** (`generateDynamicDMA`) | op `0xD4`, the DIRECT2D 2-D record (2 step/num pairs/side, elem_size, dtype) | op `0xDA`, adds src/dst_bound_size + src/dst_idx_start_addr + DGE compute-op |

`bir::DGEType {None=0, SWDGE=1, HWDGE=2, Unassigned=3}` is bound **upstream** at lowering (`translateDGEMode`, see below) — `generateDynamicDMA` reads the already-bound engine/queue and never re-derives the type. The codegen-time selectors are the AP register/immediate mode + `isDstReduceDGE` + the vector-indirect direction, all set before codegen.

---

## Dynamic-AP packaging — `assembleDynamicInfo` (`@0xf20230`)

A DMA whose offset is computed at runtime (a dynamic-shape base, or a gather index) carries a symbolic companion built at codegen time. This is the codegen-side mirror of the wire fields above: it produces the `bir::DynamicAPINFO` that hangs off `PhysicalAccessPattern+0x1D8` and serialises to the `dynamic_ap_info` key. Built in `KlirToBirCodegen::assembleDynamicInfo` (libwalrus, the KLR→BIR codegen). It is the codegen leg of the register-ALU materialization in [5.25](../penguin/).

### What "dynamic" means — the `klr::BirAccessPattern` fields

A klr access is dynamic ⇔ its `BirAccessPattern` (BAP) carries a runtime offset, in exactly one of two mutually-exclusive flavours, XOR-validated by `_validateOnlyOneOfScalarAndVectorDynamicOffsetIsProvided` (`assert bap[+0x40] != bap[+0x58]`):

| Off | Type | Meaning |
|---|---|---|
| `+0x10` | u32 | **static offset portion** `c` (the compile-time residue) |
| `+0x28` | ptr | `klr::Shape*` (per-dim shape list, for the stride coef) |
| `+0x38` | Access | SCALAR-offset inner Access (the runtime offset) |
| `+0x40` | byte | SCALAR-dynamic-offset PRESENT |
| `+0x48` | Access | VECTOR-offset inner Access (the gather index) |
| `+0x58` | byte | VECTOR-dynamic-offset PRESENT (gather) |
| `+0x60` | i32 | `indirect_dim` (the gathered axis ordinal) |

### The package it builds

`isAccessDynamic(BirAccessPattern) @0xf14030` is literally `return *(byte*)(bap+0x40) | *(byte*)(bap+0x58)`. The packager:

1. `castToBirAp(ref)` → BAP (asserts `ref.kind==1`, then `Access.kind==4`).
2. `setActualPattern(arg.Pattern @arg+8)` → `DynamicAPINFO+0x00` = the **static-resolved AP** (`actual_ap`) the op's `codegenAccess` already produced.
3. Builds **one** `pelican IndirectArgExpr` (kind **3**, `_ZTVN7pelican15IndirectArgExprE`, `arg_id=1`) up front — pinned at `f202f9: mov $0x3,%esi` / `f20310: movq $0x1,0x20(%rbp)`.
4. Branches on the presence bit:

   **VECTOR branch** (`bap[+0x58]`, a gather index): `codegenAccess(bap+0x48)` (encode the index AP); assert `indirect_dim(bap+0x60) == 0` (*"When using vector offset, the only supported indirectDim is 0"* — a gather addresses the W/partition axis only); `dyn[+0x74] = 0`; `dyn[+0x70] = indirect_dim_max_index` = the index tensor's per-partition element count (`ML+0x100` or `bytes ÷ qword_1DE98C0[dtype]`, the dtype-bytes LUT `{1,1,2,1,1,1,1,1,4,4,2,2,…}`).

   **SCALAR branch** (`bap[+0x40]`, a register-backed offset): `dyn[+0x74]=0`; if the offset Access is `kind==1` (a bare register name) → `getRegisterByName(name)` (else *"Cannot find register: … for scalar offset"*) → build a `bir::BirIntRuntimeValue` (pelican kind **7**, `f20619: mov $0x7,%esi`; `regref @+0x40`, no static index `@+0x20 = -1`); else `codegenAccess` the full Access.

5. Both branches install the per-axis term `{QuasiAffineExpr(RefPtr<Expr>), coef}` into `DynamicAPINFO+0x50` (the `aff_expr` vector, 40-byte stride, via `sub_F13410`/`sub_F1FB60`), where `coef = _computeAccumulatedShape(Shape, indirect_dim)` = the product of dim extents from that axis inward (the row-major element stride). Then `setStaticOffsetPortion(bap+0x10)` → `DynamicAPINFO+0x68` (`c`).

The net runtime address algebra (pelican):

```
addr = c  +  Σ_axis  coef_axis · expr_axis
```

where `expr_axis` is an `IndirectArgExpr` (gather index, kind 3) or a `BirIntRuntimeValue` (register read, kind 7), `coef_axis` is the per-axis stride, and `c` is the static residue.

### `DynamicAPINFO` field map (what serialises)

| Off | Wire key | Source | Tag |
|---|---|---|---|
| `+0x00` | `actual_ap` | `setActualPattern(arg+8)` (static AP) | CONFIRMED |
| `+0x50` | `aff_expr` + `coef` + `offset_expr` | `vector<pair<QuasiAffineExpr,long>>` | CONFIRMED |
| `+0x68` | `c` | `setStaticOffsetPortion(bap+0x10)` | CONFIRMED |
| `+0x70` | `indirect_dim_max_index` | gatherable-row count (vector path) | CONFIRMED |
| `+0x74` | `indirect_dim` = 0 | always 0 (W/partition axis) | CONFIRMED |

### The sibling — `extractDynamicApToStandAlone` (`@0xf1e2c0`)

A **codegen-time pre-materialisation** of the gather **index**, not nested in `assembleDynamicInfo` but running alongside it. It asserts `isAccessDynamic` (*"cannot extract dynamic ap from a static access!"*), `castToBirAp`, then `!bap[+0x40]` / `bap[+0x58]` (*"When unique_indices=True, it's expected that the vector_offset is set. Check the nisa.dma_copy call"* / *"dynamic access should have vector offset."*), `codegenAccess(bap+0x48)` → a standalone `InstArg`, then **clears** the vector-offset flag (`bap[+0x58]=0`) so the parent AP is no longer dynamic at that slot. It does **not** build a `DynamicAPINFO` and does **not** emit register-ALU.

> **Who emits the registers?** Neither of these. The register-ALU address compute is emitted **downstream** by `lower_ap` (`convertSymAP @0x11b7f80`, `lowerAffineExpr → InstRegisterAlu`, IT 73) which lowers the symbolic (kind-2) AP that `assembleDynamicInfo`'s pelican exprs created into a kind-3 `RegisterAccessPattern` (`regref` / `reg_ap_offset`) before reg-alloc; `lower_dynamic_dma` (`createDescForReadVarAddr @0x1198860`) then builds the runtime-address descriptor. The encoder ([N strand](../walrus/)) finally stamps the kind-3 register-mode descriptor — which is exactly the ADDR8 mode `0x28` (offset-register) this page describes. Lifecycle: **codegen kind-1 (static) + `assembleDynamicInfo` kind-2 (symbolic) ‖ `extractDynamicApToStandAlone` (hoist index) → lower_ap kind-2→kind-3 (register-ALU) → encoder ADDR8 register-mode**.

The two flavours map onto two front-end constructs: **scalar/kind-7** = dynamic-shape / data-dependent base (a register-read offset); **vector/kind-3** = gather = MoE expert-routing (the per-token expert-id index that selects which weight rows to gather — the `nisa.dma_copy` "unique_indices" path the `extractDynamicApToStandAlone` error string guards).

---

## Worked example — a static SB→DRAM `Save`

A plain `Save` of one 128×512 fp16 tile, no DGE, no QoS (Sunda):

```
bundle[0x00] = 0xD4              ; DIRECT2D opcode
bundle[0x01] = 0x10              ; inst_word_len
bundle[0x02..0x03] = 0x0000
bundle[0x04..0x0B] = <sync>      ; setupSyncWait/Update<DIRECT2D>
bundle[0x0C] = (qos&7)|0         ; 0 on Sunda
bundle[0x0F] = 0x03              ; sync_command = COPY
; SRC ADDR8 @0x10 — SB tile, static physical
bundle[0x10..0x13] = imm         ; v26
bundle[0x14..0x15] = phys_varid  ; 0 if SB-space==16
bundle[0x16]       = varid_hi
bundle[0x17]       = 0x10        ; mode = 16-bit physical (STATIC)
bundle[0x18..0x1B] = src_step0   ; getAPStepOnHW
bundle[0x20..0x21] = src_num0    ; APPair[dim-2].num
bundle[0x24..0x25] = src_elem_sz ; dtype_bytes(fp16=2)×last.num
; DST ADDR8 @0x28 — DRAM, static physical (same layout, mode 0x10)
bundle[0x28..0x2F] = <dst ADDR8>
bundle[0x30..0x33] = dst_step0
bundle[0x38..0x39] = dst_num0
bundle[0x3C..0x3D] = dst_elem_sz
bundle[0x3E] = dtype_tag(fp16)   ; src
bundle[0x3F] = dtype_tag(fp16)   ; dst
```

Flip it to a HW-DGE register-addressed copy and: the opcode becomes `0xDA`, the two ADDR8 mode bytes become `0x28` (offset-reg) with a DGE table-entry id at `ADDR8+4`, and the bound-size / idx-start fields appear in the dynamic record.

---

## Cross-References

- [2.1 The 64-Byte Instruction Bundle](instruction-bundle.md) — the shared header skeleton and `inst_word_len=0x10`.
- [2.07 Indirect-Gather Descriptors](indirect-descriptors.md) — the INDIRECT16B / 20B / MXINDIRECT16B descriptors the IndirectLoad/Save (`0xC4`/`0xD6`) carry.
- [2.10 PE Matmul Encoding](pe-matmul-encoding.md) — the sibling ~L field-table page; same lifecycle, `setupHeader` and `CodeGenMode` arms.
- [1.05 SBUF/PSUM Geometry](../arch/sbuf-psum-geometry.md) — the on-chip side of every DMA.
- [1.06 DRAM & HBM Geometry](../arch/dram-hbm-geometry.md) — the off-chip side; the HBM TensorIndirect restriction `assign64bitAddr` asserts.
- [Part 8 — `lower_dma` / DGE](../walrus/) — where the `DMAQueue → BasicBlock` map is populated and the DGEType decides SW vs HW descriptor generation (H37/H33).
- [5.25 Dynamic-AP / Register-ALU Materialization](../penguin/) — `lower_ap` converts the symbolic AP `assembleDynamicInfo` mints into `InstRegisterAlu` register-compute ops.
- [Build & Version Provenance](../reference/versions.md) — the `92b4d331` / `a9b1ea38` build-id pins.
