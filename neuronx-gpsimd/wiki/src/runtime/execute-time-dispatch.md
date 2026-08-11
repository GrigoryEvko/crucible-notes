# Execute-Time GPSIMD Custom-Op Dispatch

> **Scope — the host-side end-to-end dispatch of a GPSIMD custom op.** This page
> reverse-engineers the path inside the **host** Neuron runtime
> (`libnrt.so.2.31.24.0`, x86-64) that decides a model carries custom ops, picks
> the dedicated custom-op DMA queues, lowers the GPSIMD pseudo-op into a concrete
> POOL-engine instruction stream, then — at every `nrt_execute` — fires a single
> doorbell and reaps completion. The through-line is a **two-stage opcode
> dispatch**: a `LOAD_POOL_ARGUMENT` (header word `0x107A`) marshals the kernel's
> argument block into the POOL stream, and an `EXTENDED_INST` (`0xF0`) hands the
> op to one of the eight Vision-Q7 cores. Three named functions are the hinges —
> `ucode_model_has_custom_ops`, `dma_is_custom_op_dma_v2`, and
> `add_load_pool_arguments`.
>
> This is the **host enqueue** companion to the device-side consumer ABI. Where
> [The Device-Side Custom-Op ABI Reference](../abi/device-abi-reference.md) and
> [End-to-End ABI Synthesis](../abi/abi-synthesis.md) document what the Q7 kernel
> *consumes* (`customop_setup` → `customop_next_*` → `customop_return_tensor` →
> `customop_cleanup`), this page documents what `libnrt` *produces and releases*.
> It is the runtime sibling of
> [The 8-Core SPMD Execution Model + Teardown](spmd-teardown.md) and
> [Host↔Device Descriptor Handoff (runtime side)](host-device-descriptor-handoff.md),
> and the deep technical backing for the orientation narrative
> [A Custom Op, End to End](../orientation/customop-end-to-end.md).
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read from
> `objdump`/`nm`/`readelf`/`c++filt`/DWARF on the shipped binary), `INFERRED`
> (a rule applied to an observed fact), `CARRIED` (consolidated from a cited
> committed page and re-confirmed here only where spot-checked). The page default
> is `[HIGH × OBSERVED]`; claims that depart from it carry an explicit tag.

> **NOTE — provenance & artifacts.** Every host fact is derived **solely from
> static analysis** of `libnrt.so.2.31.24.0`
> (`…/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/`),
> an ELF64 x86-64 shared object, **122 956 336 bytes**, BuildID
> `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, version **2.31.24.0** (git
> `0b044f4ce`), **not stripped** (`.debug_info` present). For this binary the
> load addresses equal file offsets for `.text` (VMA `0x3dbc0`), `.rodata`
> (`0x7cf000`), `.data.rel.ro` (`0xbf2d80`), **and `.data` (`0xc07e00`)** — the
> `.data` delta is **zero** here (unlike sibling config DLLs), so every address
> below disassembles directly. The device is a Cadence Tensilica **Vision-Q7 NX
> "Cairo"** DSP (config `ncore2gp`), **8 cores per TPB POOL cluster**; the
> domain is **AWS Annapurna Trainium**. There is no NVIDIA content here.

> **GOTCHA — the host never "runs" the kernel.** `libnrt` does four things and no
> more: (a) at load, decides the model carries custom ops
> (`ucode_model_has_custom_ops`); (b) at stage, selects the per-core custom-op DMA
> queues (`dma_is_custom_op_dma_v2`) and **lowers** the GPSIMD pseudo into a fixed
> POOL micro-program (`add_load_pool_arguments` + DMA-config + DRAIN +
> EVENT_SEMAPHORE); (c) at every execute, fires **one** NeuronCore semaphore
> increment (`ndl_nc_semaphore_increment`) that releases the already-resident
> descriptor program; (d) polls a Notification Queue and drains the Q7 stdio ring
> for completion. The argument marshalling, the queue selection, and the lowering
> all happen at **model-load/stage time** — execute time only rings the doorbell.

---

## 0. One-screen orientation

A GPSIMD custom op is a Vision-Q7 ("POOL" engine) kernel that rides the NEFF's
POOL TPB-sequencer stream. Each launch is fed a per-launch argument block by a
`LOAD_POOL_ARGUMENT` instruction and is finally handed to a Q7 core by an
`EXTENDED_INST`. The host's job is to *build that stream once* and *release it
per request*. Three functions are the named hinges:

| function | addr | one-line role |
|---|---|---|
| `ucode_model_has_custom_ops` | `0x311150` | *"does this model carry custom ops?"* — `model->ucode_lib_set_info->num_libs > 1` |
| `dma_is_custom_op_dma_v2` | `0x22e120` | *"is `(eng_id, queue_id)` the custom-op DMA queue?"* — a 16-byte tuple match in `v2_queue_bundle_alloc_table` |
| `add_load_pool_arguments` | `0x276780` | emits the 64-byte `LOAD_POOL_ARGUMENT` (header `0x107A`) marshalling `{emb-table base, SBUF base, one-value read addr, completion-write addr}` |

### 0.1 The execute-time spine

```
nrt_execute @0x91de0
 └─ nrt_execute_repeat @0x91650
     └─ kmgr_exec @0xdfd50
         ├─ kmgr_exec_pre @0xdf820                  (build kmgr_exec_resources; clone tensors → phys mem)
         ├─ {sync}  kmgr_sync_exec @0xdca70
         │    ├─ tpb_xu_schedule_exec @0xe8040 → tpb_xu_schedule_request @0xe7540
         │    │    └─ dlr_add_to_hw_exec_queue @0xdd820
         │    │         ├─ kbl_compute_setup @0x306fb0           (re-materialise template DMA rings)
         │    │         └─ kbl_infer_kickoff @0x307320
         │    │              └─ exec_kickoff_infer @0x2632e0
         │    │                   └─ ndl_nc_semaphore_increment @0xc3ba0   ← THE DOORBELL (one sem += 1)
         │    ├─ tpb_xu_sync_exec_get_pooled_comp_efd @0xe87e0 + read()    (block on completion eventfd)
         │    └─ exec_request_progress_one_step @0x263330
         │         ├─ notification_read_exec_queue @0x2ff170               (INFER_STATUS NQ poll)
         │         └─ pool_stdio_queue_consume_all_entries @0x3011d0  (×2) (drain Q7 stdout/stderr ring)
         └─ {async} kmgr_async_exec_add_work @0xe6d20 + kmgr_async_exec_poll @0xe6ab0
```

Addresses confirmed against `libnrt`'s `functions.json` (IDA sidecar) and
bounded `objdump`.

---

## 1. `ucode_model_has_custom_ops` @0x311150 — the predicate

A NEFF bundles one **base** ucode library plus, for each custom op, an
**additional** loadable library. The runtime's gate for *"are there any custom
ops?"* is simply *"is the staged library count greater than one?"*. The function
is four instructions:

```asm
311150: 48 8b 87 50 19 00 00   mov    0x1950(%rdi),%rax   ; rax = model->ucode_lib_set_info
311157: 83 78 18 01            cmpl   $0x1,0x18(%rax)      ; compare ulsi->num_libs against 1
31115b: 0f 9f c0               setg   %al                  ; al = (num_libs > 1)
31115e: c3                     ret
```

### 1.1 The two structs you must not conflate

The compared field is at `model + 0x1950 → ucode_lib_set_info` then `+0x18`.
DWARF (`structures.json`) gives the exact layout:

```c
struct ucode_lib_set_info_t {        // DW_AT_byte_size 48
    dmem_t*           scratch_space;  // +0
    dmem_t*           ucode_table;    // +8
    dmem_t*           extram;         // +16
    int               num_libs;       // +24 (0x18)   ← the compared field
    ucode_lib_info_t* libs;           // +32
    dmem_t*           lib_dmem;       // +40
};
```

Full member list read from the structure sidecar.

> **CORRECTION — `num_libs` is at `+0x18` (24), not `+8`.** A *separate* 16-byte
> struct `kbin_ucode_lib_set` exists as `{nrtucode_loadable_library* libs@0;
> uint64 num_libs@8}`. Any reading that places `num_libs` at `+8` has confused
> the 16-byte `kbin_ucode_lib_set` with the 48-byte `ucode_lib_set_info_t`. The
> instruction `cmpl $0x1,0x18(%rax)` dereferences the **48-byte** struct's field
> at offset 24, confirmed by `DW_AT_data_member_location = 24`.

### 1.2 The lifetime bracket proves the role

The immediate consumer, `ucode_free_libs` @0x311160, calls the predicate and
bails if false — the staged lib set is **only** torn down when custom ops were
present:

```asm
311164: e8 e7 ff ff ff   call  311150 <ucode_model_has_custom_ops>
311169: 84 c0            test  %al,%al
31116b: 75 03            jne   311170                ; skip free if no custom ops
311170: 48 8b bb 50 19.. mov   0x1950(%rbx),%rdi
311177: e8 e4 fe ff ff   call  311060 <ucode_free_lib_set>
31117c: 48 c7 83 50 19.. movq  $0x0,0x1950(%rbx)     ; null the ulsi pointer
```

So the model's `ucode_lib_set_info` lifetime is bracketed by
`has_custom_ops`.

---

## 2. `dma_is_custom_op_dma_v2` @0x22e120 — the queue classifier

At stage time the runtime must learn **which** DMA queues will carry each pool
core's custom-op kernel image and arguments. This classifier answers *"is
`(eng_id, queue_id)` a custom-op DMA queue?"* by walking a static table of
16-byte tuples. Args: `edi = eng_id`, `esi = queue_id`.

```asm
22e120: 48 8d 05 19 ac 9d 00  lea    0x9dac19(%rip),%rax   ; rax = v2_queue_bundle_alloc_table @0xc08d40
22e127: 48 8d 90 e0 01 00 00  lea    0x1e0(%rax),%rdx      ; rdx = table end (+0x1e0)
22e12e: eb 09                 jmp    22e139
22e130: 48 83 c0 10           add    $0x10,%rax            ; advance one 16-byte tuple
22e134: 48 39 d0              cmp    %rdx,%rax
22e137: 74 1f                 je     22e158                ; reached end → not custom-op
22e139: 83 38 10              cmpl   $0x10,(%rax)          ; tuple.marker == 16 (CUSTOM_OP)?
22e13c: 75 f2                 jne    22e130
22e13e: 39 70 04              cmp    %esi,0x4(%rax)        ; tuple.rel_queue_idx == queue_id?
22e141: 75 ed                 jne    22e130
22e143: 3b 78 08              cmp    0x8(%rax),%edi        ; eng_id >= tuple.eng_lo?
22e146: 72 e8                 jb     22e130
22e148: 3b 78 0c              cmp    0xc(%rax),%edi        ; eng_id <  tuple.eng_hi?
22e14b: 73 e3                 jae    22e130
22e14d: b8 01 00 00 00        mov    $0x1,%eax ; ret       ; all four hold → custom-op DMA
22e158: 31 c0                 xor    %eax,%eax ; ret        ; else 0
```

### 2.1 The tuple and the table

```c
struct queue_bundle_alloc_entry {   // 16 bytes
    uint32_t marker;        // +0   == 16 (KBIN_DMA_RING_TYPE_CUSTOM_OP)
    uint32_t rel_queue_idx; // +4   relative queue index within the engine
    uint32_t eng_lo;        // +8   engine range, inclusive
    uint32_t eng_hi;        // +12  engine range, exclusive
};
```

- The literal `0x10 == 16 == KBIN_DMA_RING_TYPE_CUSTOM_OP`. The enum sidecar
  confirms the surrounding values: `…EMBEDDING_UPDATE = 15`, **`CUSTOM_OP = 16`**,
  `GENERIC = 17`.
- The walk bound `0x1e0` is not a magic constant — it is independently published
  as `v2_queue_bundle_alloc_table_len @0x9ba590`, whose `.rodata` bytes read
  `1e 00 00 00 00 00 00 00`. So **`0x1e0 / 0x10 = 30` tuples maximum**, and the
  loop stops at the first tuple that crosses the end pointer.

> **CORRECTION — the stride is 16 bytes, not 8.** Any reading of this table as an
> "8-byte pair walk" mis-strides it. The `+0/+4/+8/+0xC` offset loads above prove
> a packed `{marker, rel_queue_idx, eng_lo, eng_hi}` tuple at **stride `0x10`**;
> the `add $0x10,%rax` advance confirms it.

### 2.2 The one caller: model-load-time queue selection

`dma_is_custom_op_dma_v2` has **exactly one caller** in `libnrt`
(`functions.json` callgraph): `ucode_stage_libs_table @0x3108e0`, at site
`0x310975`. The staging loop iterates `eng_id ∈ 0..15`, `queue_id ∈ 0..15`, and
for each custom-op hit records the engine's physical address and its m2s/s2m tail
offsets:

```c
// ucode_stage_libs_table @0x3108e0 (reconstructed)
for (uint32_t eng = 0; eng < 16; ++eng)
  for (uint32_t q = 0; q < 16; ++q)          // 16 queues/engine (cmp $0x10,%ebx)
    if (dma_is_custom_op_dma_v2(eng, q)) {    // call @0x310975
        pa  = get_dma_engine_pa(eng);                  // @0x3186e0
        m2s = get_dma_queue_offset(eng, q, /*ctl=*/1); // @0x3188f0  read/tail
        s2m = get_dma_queue_offset(eng, q, /*ctl=*/0); // @0x3188f0  write/tail
        record_custom_op_queue(pa, m2s, s2m);
        if (++n == 8) break;                  // up to 8 pool-core queues (cmp $0x8,%rbp)
    }
// sanity: ulsi->num_libs <= 9   (cmp $0x9,(num_libs))
```

The `8` is the **8 GPSIMD pool cores** (cf. device `get_cpu_count() == 8`); the
`9` bound (`base lib + ≤8 op libs`) sanity-caps `num_libs`.

> **NOTE — staging is a load-time activity.** The call chain is
> `dlr_kelf_stage_model_add → kbl_model_add @0x3058e0 → ucode_stage_libs @0x310ea0
> → ucode_stage_libs_impl @0x310c00 → ucode_stage_libs_table @0x3108e0`.
> Per-core staged-lib state is allocated under a mutex
> (`ucode_alloc_new_staged_libs @0x310660`), keyed by
> `db_physical_core_get_mla_and_tpb`. Custom-op DMA-queue selection therefore
> happens at **model load/stage**; execute time only fires the prebuilt rings
> (§5).

---

## 3. `add_load_pool_arguments` @0x276780 — opcode `0x107A` marshalling

This is the host producer for the device's argument-pull stream. It builds a
64-byte `NEURON_ISA_TPB_CTRL_MV_STRUCT` on the stack, fills five address fields,
and `memmove`s it into the instruction chunk. The header word `0x107A` decodes as
**opcode `0x7A` (`LOAD_POOL_ARGUMENT`) | word_len `0x10` (16 words × 4 B = 64-byte
slot)**.

### 3.1 Prototype (DWARF, decl_line 2775)

```c
uint32_t add_load_pool_arguments(
    void*    chunk,                                // rdi  instruction buffer
    uint32_t offset,                               // esi  byte offset in chunk
    uint32_t embedding_table_base_addr_sb_offset,  // edx
    uint64_t sbuf_base_addr,                        // rcx
    uint64_t one_value_read_addr,                   // r8
    uint64_t completion_write_addr,                 // r9
    const NEURON_ISA_TPB_EVENTS* events);           // [rsp+0x50]  (stack arg 7)
// returns offset + 0x40 (next free slot)
```

DWARF + register usage confirmed.

### 3.2 The 64-byte instruction it builds (byte-exact)

```asm
276780: sub    $0x48,%rsp
276784: pxor   %xmm0,%xmm0
276788: mov    $0x107a,%r10d                ; header opcode|word_len
27678e: mov    0x50(%rsp),%rax              ; rax = events (stack arg 7)
276793: movups %xmm0,0x2(%rsp)              ; zero +0x02..+0x11
276798: mov    %r10w,(%rsp)                 ; +0x00  header = 0x107A
27679d: movups %xmm0,0x10(%rsp)             ; zero +0x10..+0x1F
2767a2: test   %rax,%rax ; je 2767af        ; if (events)
2767a7: mov    (%rax),%rax
2767aa: mov    %rax,0x4(%rsp)               ; +0x04  events (8 bytes)
2767af: mov    %edx,0x20(%rsp)              ; +0x20  embedding_table_base_addr_lo
2767b3: add    $0x4,%edx
2767b6: mov    %edx,0x24(%rsp)              ; +0x24  embedding_table_base_addr_hi (= lo+4)
2767ba: mov    %rsp,%rdx                    ; src for add_ins
2767bd: mov    %rcx,0x28(%rsp)              ; +0x28  sbuf_base_addr
2767c2: mov    %r8,0x30(%rsp)               ; +0x30  one_value_read_addr
2767c7: mov    %r9,0x38(%rsp)               ; +0x38  completion_write_addr
2767cc: movb   $0x3,0xe(%rsp)               ; +0x0E  move_source = 0x03
2767d1: call   322480 <add_ins>             ; copy 64 bytes into chunk[offset]
2767d6: add    $0x48,%rsp ; ret
```

The five address fields are an inner 32-byte union, DWARF-typed as:

```c
struct load_pool_arguments {            // DW_AT_byte_size 32 (sits at instruction +0x20)
    uint32_t embedding_table_base_addr_lo;  // +0   (instr +0x20)
    uint32_t embedding_table_base_addr_hi;  // +4   (instr +0x24)  = lo + 4
    uint64_t sbuf_base_addr;                // +8   (instr +0x28)
    uint64_t one_value_read_addr;           // +16  (instr +0x30)
    uint64_t completion_write_addr;         // +24  (instr +0x38)
};
```

### 3.3 The CTRL_MV container, and what `0x03` at `+0x0E` really is

The 64-byte slot is a `NEURON_ISA_TPB_CTRL_MV_STRUCT` (DWARF `byte_size 64`),
into which the `load_pool_arguments` union is aliased over the move payload:

```c
struct NEURON_ISA_TPB_CTRL_MV_STRUCT {       // 64 bytes
    NEURON_ISA_TPB_HEADER  header;            // +0    (4 B) opcode|word_len + hint
    NEURON_ISA_TPB_EVENTS  events;            // +4    (8 B) wait/update semaphore gate
    uint8_t                num_mov;           // +12   (= 0)
    NEURON_ISA_TPB_DTYPE   dtype;             // +13   (= 0)
    NEURON_ISA_TPB_DATA_SRC move_source;      // +14   ← the 0x03 store
    uint8_t                reserved0[1];      // +15
    NEURON_ISA_TPB_REG_NUM src_registers[8];  // +16
    NEURON_ISA_TPB_REG_NUM dst_registers[8];  // +24
    NEURON_ISA_TPB_MOVE_IMMEDIATE immediate;  // +32 (32 B) ← aliased by load_pool_arguments
};
```

> **CORRECTION — the `0x03` at `+0x0E` is `move_source`, not a "header tail
> byte"; and the argument union sits at instruction offset `+0x20` (32), not
> `+12`.** The structure sidecar resolves the byte that earlier passes flagged as
> a LOW-confidence "header tail byte": instruction offset `+0x0E` (14) is
> `NEURON_ISA_TPB_CTRL_MV_STRUCT.move_source` (type `NEURON_ISA_TPB_DATA_SRC`),
> stored as `0x03`. Likewise `num_mov@+12`/`dtype@+13`/`reserved0@+15` and the
> `src/dst_registers` region `+16..+31` stay zero (from the two `movups` clears),
> and the five address words land in the `immediate@+32` slot — i.e. the 32-byte
> `load_pool_arguments` union is overlaid on `MOVE_IMMEDIATE` at **instruction
> offset `0x20`**. The earlier "union at slot+12" narrative is corrected here; the
> *stack-store offsets* (`0x20/0x24/0x28/0x30/0x38`) were always right — only the
> field labels and the union base were off.

### 3.4 `add_ins` @0x322480 — the universal 64-byte slot writer

Every TPB-sequencer instruction `libnrt` emits is a fixed **64-byte / `0x40`
stride** slot:

```asm
322487: mov   $0x40,%edx            ; length = 64
32248c: add   %rbx,%rdi             ; dst = chunk + offset
32248f: call  3d660 <memmove@plt>   ; copy the 64-byte instruction
322494: lea   0x40(%rbx),%rax ; ret ; return offset + 0x40
```

This confirms the 64-byte slot from the host emit side.

### 3.5 What each field means to the device

| field | from | device consumer |
|---|---|---|
| `embedding_table_base_addr_{lo,hi}` | `edx`, `edx+4` (a 2-word SB byte-address split) | on-chip SBUF base for the op's operand table |
| `sbuf_base_addr` | `rcx` | SBUF base that `customop_next_tensor()` / `get_dst_tensor()` translate + dereference (`ARG_TENSOR.storage.tpb.addr`) |
| `one_value_read_addr` | `r8` ← `tdrv_arch_get_evt_accel_addr` | the scalar source `customop_next_int/_float` reads ("scalar arg == 1-element tensor") |
| `completion_write_addr` | `r9` ← `tdrv_arch_get_sem_inc_addr` | the semaphore-increment CSR the kernel posts on done → host Notification Queue |

`[HIGH × OBSERVED]` for the bytes on each side; `[MED × INFERRED]` for the 1:1
host-field → device-consumer binding (each end is observed; the seam is reasoned
across the host/device boundary). See
[The Device-Side Custom-Op ABI Reference](../abi/device-abi-reference.md) for the
consuming side.

> **QUIRK — `embedding_table_base_addr_hi = lo + 4`.** The `add $0x4,%edx` is a
> *byte* increment of a 32-bit SB offset, producing a hi-word that is the lo-word
> plus four bytes — a two-word (8-byte) SB byte-address split, not a 64-bit
> high-half. The `+4` is `OBSERVED`; the "two-word SB byte-address split" reading
> is `[MED × INFERRED]`.

---

## 4. Where `0x107A` is emitted — the lowering site

`add_load_pool_arguments` has **exactly one caller**
(`functions.json` callgraph, confirmed):
`translate_one_pseudo_embedding_update_instr_v2 @0x2770f0`, at site `0x277886`.
This translator lowers a `PSEUDO_EMBEDDING_UPDATE` (opcode `0xCA`) — itself a
GPSIMD/POOL custom-kernel pseudo — into a concrete TPB micro-program. The ordered
emit around the call:

```c
// translate_one_pseudo_embedding_update_instr_v2 @0x2770f0 (emit order, callees confirmed)
add_dma_config_base(..., read_side);              // @0x2736d0  DMA cfg base #1
r8 += 0x40000;                                    // +0x40000 SBUF/DRAM aperture step
add_dma_config_base(..., write_side);             // @0x2736d0  DMA cfg base #2
add_dma_config_size(...);                         // @0x273750  transfer size
r8 = tdrv_arch_get_evt_accel_addr(1);             // @0x309ef0  → one_value_read_addr
r9 = tdrv_arch_get_sem_inc_addr(...);             // @0x309a60  → completion_write_addr
add_load_pool_arguments(chunk, off, emb, sbuf, r8, r9, events); // @0x276780  emit 0x107A
add_drain(...);                                   // @0x273e40  emit 0x10A2 DRAIN (barrier)
// events.update_mode = 0x13 (inc-on-done) → an EVENT_SEMAPHORE completion gate
```

Emit order matches the callee list of the translator
(`add_dma_config_base ×2`, `add_dma_config_size`, `tdrv_arch_get_evt_accel_addr`,
`tdrv_arch_get_sem_inc_addr`, `add_load_pool_arguments`, `add_drain`; the
translator also drives `add_dma_rearm @0x2735d0` and `add_dma_tail_inc @0x273f30`
around the DMA config).

So the per-op micro-program the host lays into the POOL stream is:

```
[ dma_config_base × 2 ][ dma_config_size ][ LOAD_POOL_ARGUMENT 0x107A ][ DRAIN 0x10A2 ][ EVENT_SEMAPHORE ]
```

### 4.1 `add_drain` @0x273e40 — the barrier (opcode `0x10A2`)

Same shape as `add_load_pool_arguments`: build a 64-byte CTRL slot, header word
`0x10A2` (opcode `0xA2` `DRAIN`, word_len `0x10`), copy via `add_ins`:

```asm
273e48: mov   $0x10a2,%eax
273e52: mov   %ax,(%rsp)          ; +0x00  header = 0x10A2 (DRAIN)
...
273e7e: call  322480 <add_ins>
```

### 4.2 The arch-dispatch addresses

- `completion_write_addr` comes from **`tdrv_arch_get_sem_inc_addr`** — an
  arch-dispatch trampoline through `tdrv_arch_ops @0xc97180` slot `+0x160` that
  returns a semaphore-**increment** CSR MMIO address. The Q7 kernel writing there
  increments a NeuronCore semaphore = the device→host completion signal.
- `one_value_read_addr` comes from **`tdrv_arch_get_evt_accel_addr`**
  (`tdrv_arch_ops +0x108`), an "event accelerator" read address for the scalar
  value.

Both have per-generation back-ends — `…_sunda`, `…_cayman`, `…_mariana` — i.e.
**NC-v2 / NC-v3 / NC-v4** specializations selected through the `tdrv_arch_ops`
vtable. The dispatch slots and the `{sunda, cayman, mariana}` variant set are
read directly from the binary.

> **WALL — generation naming.** `sunda` = NC-v2, `cayman` = NC-v3, `mariana` =
> NC-v4 are taken as committed. The dispatch tables ship those three back-ends;
> an NC-v5 (`maverick`) arch path and `arch_id 36` are `INFERRED` and not relied
> on by this page.

### 4.3 The pseudo dispatcher

`translate_one_pseudo_instr_v2 @0x273ff0` reads the 1-byte pseudo opcode from
`r13[0]`, fast-paths `0xC1` (`PSEUDO_DMA_TRIGGER`, which binds the queue name to
its bundle via `find_queue_bundle_and_instance_by_name @0x22dcc0`), `0xC2`,
`0xD6`, `0xC4`, `0x23`, and for the rest computes `al = opcode + 0x7A` and indexes
a jump table `@0x9da260`; the `0xCA` case lands at `0x2755a5 →
translate_one_pseudo_embedding_update_instr_v2`. The same dispatcher both
relocates DMA-trigger pseudos to real queue writes **and** lowers GPSIMD
pool-custom pseudos into `LOAD_POOL_ARGUMENT + DMA + completion`. It runs at NEFF
load/stage (`translate_pseudo_instrs_partial_v2 @0x2763d0`).

> **NOTE — opcode bytes, signed-enum encoding.** The IDA enum stores TPB opcodes
> as signed ints; the byte in the stream is the low 8 bits.
> `LOAD_POOL_ARGUMENT = 122 = 0x7A`; `EMBEDDING_UPDATE = 121 = 0x79`;
> `DRAIN = -94 → 0xA2`; `PSEUDO_EMBEDDING_UPDATE = -54 → 0xCA`;
> `EXTENDED_INST = -16 → 0xF0`. The header word is `word_len << 8 | opcode`, so
> `0x107A = 0x10·256 + 0x7A`.

---

## 5. The execute-time firing — doorbell, NQ poll, stdio drain

By execute time the entire custom-op descriptor program (the `LOAD_POOL_ARGUMENT`
slot, the custom-op DMA rings, the `EXTENDED_INST 0xF0` in the POOL stream, and
the staged Q7 library image) is already resident. `nrt_execute` only **releases**
it.

### 5.1 `nrt_execute` → `kmgr_exec`

`nrt_execute @0x91de0` is a thin profiling/trace bracket
(`nrt_sys_trace_new_event`, `nrt_profile_start`) that tail-calls
`nrt_execute_repeat @0x91650`, which interns the model id, counts ifmaps
(`kmgr_get_ifmap_count`), and calls `kmgr_exec @0xdfd50`. `kmgr_exec_pre @0xdf820`
builds `kmgr_exec_resources` and clones the tensor set to physical memory
(`kbl_tensor_set_clone_to_physical_mem`); then SYNC → `kmgr_sync_exec @0xdca70`
or ASYNC → `kmgr_async_exec_add_work @0xe6d20` + `kmgr_async_exec_poll @0xe6ab0`.

### 5.2 The doorbell — one semaphore increment

`dlr_add_to_hw_exec_queue @0xdd820 → kbl_compute_setup @0x306fb0` (re-materialise
template DMA rings) `→ kbl_infer_kickoff @0x307320 → exec_kickoff_infer
@0x2632e0`:

```asm
2632f0: call  2272a0 <db_physical_core_get_mla_and_tpb>
2632f9: call  30a5c0 <tdrv_sync_get_inference_start>   ; → start-semaphore descriptor
2632fe: mov   0x4(%rbx),%esi                            ; esi = sem id/arg
263301: mov   $0x1,%ecx                                 ; value = 1
263306: movzbl %al,%edx                                 ; idx = (from get_inference_start)
263309: mov   (%rsp),%rax
26330d: mov   0x4cd28(%rax),%rdi                        ; rdi = semaphore handle (struct +0x4cd28)
263314: call  c3ba0 <ndl_nc_semaphore_increment>        ; ← ONE NeuronCore semaphore write
```

That single increment is the kickoff. The SP/SEQ sequencer, gated on this
semaphore, unblocks and begins fetching the per-engine 64-byte instruction
streams — including the POOL stream carrying `LOAD_POOL_ARGUMENT`, the custom-op
DMA triggers, and `EXTENDED_INST 0xF0`.

> **CORRECTION — the semaphore handle comes from `tdrv_sync_get_inference_start`,
> with `0x4cd28` a field into its result.** The handle loaded at
> `mov 0x4cd28(%rax),%rdi` is *not* a free-standing global; `%rax` is restored
> from the stack slot set up around the `tdrv_sync_get_inference_start @0x30a5c0`
> call, and `0x4cd28` is the byte offset of the inference-start semaphore handle
> within that returned per-core sync structure. The increment value (`1`) and
> index (`%al` → `%dl`) likewise derive from that call.

### 5.3 Completion poll — NQ read + Q7 stdio drain

After blocking on the pooled completion eventfd
(`tpb_xu_sync_exec_get_pooled_comp_efd @0xe87e0 + read()`), the XU worker runs
`exec_request_progress_one_step @0x263330`:

```c
notification_read_exec_queue(...);                 // @0x2ff170  per-engine INFER_STATUS NQ
pool_stdio_queue_consume_all_entries(...);         // @0x3011d0  Q7 stdout ring
pool_stdio_queue_consume_all_entries(...);         // @0x3011d0  Q7 stderr ring (twice)
```

- `notification_read_exec_queue` reads the ENS Notification-Queue mirror; the
  INFER_STATUS fan-out maps `{PE, ACT, POOL, DVE, SP}` to NQ ids `{4, 5, 6, 7,
  8}` — the **POOL** NQ id is `6`.
- `pool_stdio` is a dmem ring allocated at `tdrv_init` via `pool_stdio_block_init
  @0x300c70 → pool_stdio_queue_init @0x300a10` (`dmem_alloc_aligned` +
  `dmem_memset`; `DMEM_GET_VA` for a host-readable VA). The host both consumes Q7
  `printf`/diagnostics **and** observes the kernel's `completion_write_addr`
  semaphore/NQ posting to detect completion.

The request then reaps via `tpb_xu_get_last_completed @0xe8410 →
kmgr_exec_resources_free`, and `nrt_tensor_check_output_completion` observes the
posted output semaphore.

---

## 6. The execute-time sequence (host → Q7 kernel → completion)

Legend: `[H]` host `libnrt` (x86), `[HW]` NeuronCore SP/SEQ + DMA, `[Q7]`
Vision-Q7 POOL core. `==>` control, `~~>` DMA/MMIO.

```
PRECONDITION  (at nrt_load / dlr_kelf_stage_model_add — §2, §4):
  [H] ucode_model_has_custom_ops(model) → num_libs>1 → custom ops present
  [H] ucode_stage_libs → ucode_stage_libs_table:
        ∀(eng,queue): dma_is_custom_op_dma_v2 → pick the ≤8 pool-core custom-op
        DMA queues; record engine PA + m2s/s2m tail offsets
  [H] translate_pseudo_instrs_partial_v2 → translate_one_pseudo_instr_v2:
        0xC1 PSEUDO_DMA_TRIGGER       → find_queue_bundle_and_instance_by_name → real queue-tail WRITE
        0xCA PSEUDO_EMBEDDING_UPDATE  → dma_config×2 + dma_config_size +
                                        add_load_pool_arguments(0x107A){emb, sbuf,
                                          one_value=evt_accel, completion=sem_inc} +
                                        add_drain(0x10A2) + EVENT_SEMAPHORE
  [H] staged Q7 library image DMA'd into POOL IMEM/DRAM; kernel_info_table maps
        opcode 0xF0(spec) → kernel funcVA

PER nrt_execute REQUEST:
  [H] nrt_execute ==> nrt_execute_repeat ==> kmgr_exec ==> kmgr_exec_pre
        (clone tensor set → phys mem; build kmgr_exec_resources)
   |
  [H] kmgr_sync_exec ==> tpb_xu_schedule_exec ==> tpb_xu_schedule_request
        ==> dlr_add_to_hw_exec_queue
              ==> kbl_compute_setup           (re-materialise template DMA rings)
              ==> kbl_infer_kickoff ==> exec_kickoff_infer
                    ~~> ndl_nc_semaphore_increment(start_sem += 1)   === THE DOORBELL ===
   |                                                                   |
   |  [H] block on pooled completion eventfd (read())                  v
   |                                          [HW] SP/SEQ sees start_sem, fetches
   |                                               per-engine 64-B streams
   |                                          [HW] POOL stream:
   |                                               - relocated DMA-trigger WRITEs fire
   |                                                 custom-op DMA (idx-16 ring) HBM<->SBUF ~~>
   |                                               - LOAD_POOL_ARGUMENT (0x7A): args resident
   |                                               - EXTENDED_INST (0xF0, spec): SEQ hands op to a Q7 core
   |                                                   |
   |                                          [Q7] kernel_info_table linear scan
   |                                               (spec, 0xF0) → callx8 funcVA  == device customop entry
   |                                          [Q7] KERNEL BODY consumes the marshalled args:
   |                                                 customop_setup()
   |                                                 x = customop_next_tensor()   (reads sbuf_base_addr / ARG_TENSOR.storage.tpb)
   |                                                 k = customop_next_int()      (reads one_value_read_addr)
   |                                                 y = get_dst_tensor()
   |                                                 ... SPMD compute on 8 pool cores (get_cpu_id partition) ...
   |                                                 customop_return_tensor(y)
   |                                                 customop_cleanup()           (memw + fsync)
   |                                          [Q7] write completion:
   |                                               ~~> completion_write_addr CSR (semaphore increment)
   |                                               ~~> pool_stdio ring (stdout)
   |                                                   |
   v                                          [HW] EVENT_SEMAPHORE gate satisfied;
  [H] eventfd signalled  <==================== Notification Queue (INFER_STATUS POOL, NQ id 6)
   |
  [H] exec_request_progress_one_step:
        notification_read_exec_queue                 (read per-engine infer-status NQ)
        pool_stdio_queue_consume_all_entries × 2     (drain Q7 stdout/diagnostics)
   |
  [H] tpb_xu_get_last_completed → kmgr_exec_resources_free → return to caller
        (nrt_tensor_check_output_completion observes the posted output semaphore)
```

The device half (`kernel_info_table` scan, `customop_*` pull stream, the 8-core
SPMD join via DRAIN + completion semaphore) is the committed device ABI — see
[The 8-Core SPMD Execution Model + Teardown](spmd-teardown.md),
[The Device-Side Custom-Op ABI Reference](../abi/device-abi-reference.md), and the
Part-11 sequencer note [`neff/seq-microcode.md`](../neff/seq-microcode.md) for the
POOL `.bin` stream layout. The host descriptor staging is detailed in
[Host↔Device Descriptor Handoff (runtime side)](host-device-descriptor-handoff.md).

---

## 7. Cross-ties

| binds to | how |
|---|---|
| [A Custom Op, End to End](../orientation/customop-end-to-end.md) | the narrative INVOKE/EXECUTE/RETURN stages; this page is the host-runtime detail behind "Stage 9 — Invoke & run" |
| [End-to-End ABI Synthesis](../abi/abi-synthesis.md) | the consolidated host↔device flow; `add_load_pool_arguments` is the host emit of the args that synthesis's MARSHAL stage consumes |
| [The Device-Side Custom-Op ABI Reference](../abi/device-abi-reference.md) | the device consumer: `customop_setup`/`customop_next_{tensor,int,float}`/`get_dst_tensor`/`customop_return_tensor`/`customop_cleanup` read exactly the `{sbuf_base, one_value_read, ARG_TENSOR storage}` this page marshals |
| [The 8-Core SPMD Execution Model + Teardown](spmd-teardown.md) | the 8 pool cores `ucode_stage_libs_table` collects are the 8 SPMD cores; `completion_write_addr` is the host side of the kernel's semaphore post |
| [Host↔Device Descriptor Handoff (runtime side)](host-device-descriptor-handoff.md) | the custom-op DMA rings the doorbell releases; the m2s/s2m tail offsets `ucode_stage_libs_table` records |
| [`neff/seq-microcode.md`](../neff/seq-microcode.md) *(Part 11)* | the POOL `.bin` stream where the 1-byte opcodes `0x7A`/`0xA2`/`0xF0` live; the Q7 `kernel_info_table` looks up the same byte this page emits |

---

## 8. Confidence ledger & open items

**`HIGH × OBSERVED`**: every host address; the three named
functions' byte-exact disassembly; `add_ins` 64-byte `memmove`; the
`add_load_pool_arguments` stack→slot field map vs DWARF (`ucode_lib_set_info_t`,
`NEURON_ISA_TPB_CTRL_MV_STRUCT`, `load_pool_arguments` union); `num_libs @+0x18`;
the `dma_is_custom_op_dma_v2` 16-byte tuple walk and `0x1e0`/`30`-tuple bound
(via `v2_queue_bundle_alloc_table_len`); `KBIN_DMA_RING_TYPE_CUSTOM_OP = 16`; the
single caller of each of `add_load_pool_arguments` (→ embedding-update lowering)
and `dma_is_custom_op_dma_v2` (→ `ucode_stage_libs_table`); the opcode bytes
`0x7A/0xA2/0xCA/0xF0`; the `nrt_execute → doorbell (ndl_nc_semaphore_increment) →
NQ-read / pool_stdio-drain` spine.

**`MED × INFERRED`**: (a) the 1:1 host-field → device-consumer mapping (each side
`OBSERVED`, the binding `INFERRED` across the boundary); (b) the label
`embedding_table_base_addr_hi = lo + 4` as a "2-word SB byte-address split" (the
`+4` is `OBSERVED`; the semantic is inferred).

**Open / deferred** (not covered here): the device-side Q7 `kernel_info_table`
funcVA disassembly for `LOAD_POOL_ARGUMENT` consumption (native `ncore2gp`
`xtensa-elf-objdump` on the POOL members — see the ABI lane); the async-exec
worker (`kmgr_async_exec_*`) completion-poll detail.

> **NOTE — corrections summary.** Three in-place corrections vs earlier
> readings: (1) `num_libs` is at `+0x18` in the 48-byte
> `ucode_lib_set_info_t`, **not** `+8` in the 16-byte `kbin_ucode_lib_set`; (2)
> `dma_is_custom_op_dma_v2` walks **16-byte** tuples, not 8-byte pairs; (3) the
> `0x03` at instruction offset `+0x0E` is `CTRL_MV.move_source`, and the
> `load_pool_arguments` union is aliased over `MOVE_IMMEDIATE` at instruction
> offset `+0x20` (32) — **not** "slot+12". The doorbell handle is sourced from
> `tdrv_sync_get_inference_start` (`+0x4cd28` is a field, not a global).
