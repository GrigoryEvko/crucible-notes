# The 8-Core SPMD Execution Model + Teardown

> **Scope — the host-side life of an 8-core GPSIMD custom op, from broadcast
> install to the join after each `nrt_execute` to full teardown.** This page
> reverse-engineers, inside the **host** Neuron runtime
> (`libnrt.so.2.31.24.0`, x86-64), four physical facts of the Vision-Q7 "POOL"
> SPMD model and one teardown spine:
> 1. **The SPMD rank is the silicon PRID** — every pool core runs the *same*
>    library image and self-identifies via `get_cpu_id()` ∈ {0..7}; the per-op
>    fan-out is a baked `total_cpus` ∈ {1,8} attribute, not a host branch.
> 2. **One image is broadcast, the DMA queues are per-core** —
>    `ucode_stage_libs_table` collects exactly **8** custom-op DMA queues, bakes
>    **one** opcode→library table per NeuronCore, and `ucode_switch_libs` installs
>    it with a **single** `aws_hal_q7_swap_table`.
> 3. **There is no device Q7↔Q7 collective** — the only cross-core memory is the
>    shared SBUF; the only join is the **host-emitted `DRAIN` (`0x10A2`) +
>    completion `EVENT_SEMAPHORE`**, observed as one NeuronCore semaphore
>    increment. Each core's own `memw` in `customop_cleanup` is its commit fence.
> 4. **The `pool_stdio` back-channel is an 8×2 ring array** drained every progress
>    step (`exec_request_progress_one_step`) with wraparound-aware copy-out and a
>    host-write-only read cursor published back to the Q7.
>
> And the **teardown spine**:
> `tdrv_destroy` → `ucode_core_destroy` ×13 (5 NX + 8 Q7) → `ucode_ll_destroy` ×N
> → `notification_destroy` → `pool_stdio_block_destroy` → `ucode_free_lib_set`.
>
> This is the **lifecycle + join** companion to
> [Execute-Time GPSIMD Custom-Op Dispatch](execute-time-dispatch.md) (which lowers
> and fires one op) and the host-side elaboration of the device-side model in
> [The Multicore API (8-core SPMD)](../abi/multicore-spmd.md). The stdio ring's
> device producer is [On-Device Virtual File-I/O Manager](../firmware/kernels/file-io-manager.md);
> the error/abort surface of the teardown path is
> [Host Model Lifecycle + Error-Handling Model](lifecycle-error-model.md).
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read this task
> from `objdump`/`nm`/`readelf`/`c++filt`/DWARF on the shipped binary, or the
> ncore2gp `xtensa-elf-objdump` on the device `.a`), `INFERRED` (a rule applied to
> an observed fact), `CARRIED` (consolidated from a cited committed page and
> re-confirmed here only where spot-checked).

> **NOTE — provenance & artifacts.** Every **host** fact is derived **solely from
> static analysis** of `libnrt.so.2.31.24.0`
> (`…/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/`), an
> ELF64 x86-64 shared object, **122 956 336 bytes**, BuildID
> `8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, version **2.31.24.0** (git
> `0b044f4ce`), shipped **with** DWARF (not stripped). Confirmed this pass:
> `.text` VMA `0x3dbc0` == file offset `0x3dbc0`, `.data` VMA `0xc07e00` == file
> offset `0xc07e00` — **`libnrt.so` has no VMA↔offset delta**, so `objdump`/DWARF
> offsets are file offsets directly. Every **device** fact is from the
> ncore2gp Xtensa archive
> `libneuroncustomop.a` (`…/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/…/custom_op/neuron/`),
> disassembled with `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`). No vendor source
> was consulted; every statement reads as derived from shipped-binary analysis.

---

## 0. Orientation — five facts, one diagram

A GPSIMD custom op is **one** Vision-Q7 device-code library, installed **once** per
NeuronCore and run **SPMD** across the **8** Q7 "POOL" cores of that NeuronCore.
The whole model is five physical facts:

| Fact | Mechanism | Granularity | Anchor |
|---|---|---|---|
| **SPMD rank = PRID** | ctor caches `rsr.prid` into `.bss cpu_id`; `get_cpu_id()` reads it | per-core (silicon) | §1 |
| **Image broadcast** | one opcode→lib table, one `aws_hal_q7_swap_table` | shared by 8 | §2 |
| **DMA queues per-core** | 8 `m2s`/`s2m` doorbell pairs; PRID-rebased SoC window | per-core | §2,§3 |
| **Join = host DRAIN + sem** | `DRAIN 0x10A2` + `EVENT_SEMAPHORE`; **no** device collective | host-emitted | §4 |
| **stdio = 8×2 ring drain** | `consume_all_entries` every progress step, wraparound, publish read-idx | per-core array | §5 |

Teardown reverses the bring-up: `tdrv_destroy` → **13** `ucode_core_destroy` →
`ucode_ll_destroy` → `pool_stdio_block_destroy` → `ucode_free_lib_set` (§6).

**Key host addresses** (all confirmed against `libnrt`'s `functions.json` sidecar
and re-disassembled this pass): `tdrv_destroy 0x269a70`,
`ucode_core_destroy 0x2267d0`, `ucode_ll_destroy 0x226a70`,
`ucode_free_lib_set 0x311060`, `ucode_stage_libs_table 0x3108e0`,
`ucode_switch_libs 0x311100`, `add_drain 0x273e40`,
`aws_hal_q7_swap_table 0x451120`, `aws_hal_q7_swap_file_io_table 0x451220`,
`pool_stdio_block_init 0x300c70`, `pool_stdio_block_destroy 0x300f50`,
`pool_stdio_queue_init 0x300a10` (`.constprop.0`),
`pool_stdio_queue_available_count 0x3010f0`,
`pool_stdio_queue_consume_all_entries 0x3011d0`,
`pool_stdio_get_entry_size 0x300a00`,
`exec_request_progress_one_step 0x263330` (drain call sites `0x263928`/`0x2639a0`).

**Key device addresses** (ncore2gp): `get_cpu_id 0x0`, `get_cpu_count 0x10`,
`_GLOBAL__sub_I_parallel.cpp 0x18` (`parallel.o`); `dram_addr_to_soc_addr 0x210`,
`init_dma_queue 0x314` (`data_transfer.o`); `customop_cleanup 0x5d4`
(`wrapper_api.o`).

---

## 1. The SPMD rank — `get_cpu_id()` is the PRID, `get_cpu_count()` is the constant 8

`[HIGH × OBSERVED]` — `parallel.o` re-disassembled byte-exact this pass with
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`).

```asm
; parallel.o — symbol table: _ZL6cpu_id is a 4-byte .bss global

00000000 <_Z10get_cpu_idv>:                 ; get_cpu_id()
   0: entry  a1, 32
   3: const16 a2, &cpu_id                    ; (reloc against .bss _ZL6cpu_id)
   9: l32i.n a2, a2, 0                        ; a2 = *cpu_id   — the CACHED rank
   b: retw.n

00000010 <_Z13get_cpu_countv>:              ; get_cpu_count()
  10: entry  a1, 32
  13: movi.n a2, 8                            ; the COMPILE-TIME constant 8
  15: retw.n

00000018 <_GLOBAL__sub_I_parallel.cpp>:     ; module ctor — runs once at lib load
  18: entry  a1, 32
  1b: const16 a2, &cpu_id
  21: rsr.prid a3                             ; read Xtensa Processor-ID special reg
  24: s32i.n a3, a2, 0                        ; cpu_id = PRID  — cache it once
  26: retw.n
```

**Semantics.** All 8 Q7 pool cores execute the **same** library image (single
program); the only differentiator is the hardware PRID, read once by the module
constructor and cached into `.bss cpu_id`. Therefore `get_cpu_id()` **is** the SPMD
rank ∈ {0..7} and `get_cpu_count()` is the fixed cluster width **8**. There is no
host-passed rank — the cores self-identify by silicon. `[HIGH × OBSERVED]`

### 1.1 `total_cpus` ∈ {1,8} — the per-library fan-out attribute

A library statically declares its fan-out as `total_cpus`, parsed at NEFF load by
`kelf2kbin parse_one_ucode_lib`:

- `cpu_id` field **must** be `0` (assert `"cpu_id == 0"`);
- `total_cpus` field **must** be `1` or `8` (else rejected with
  `"UCode Library %s has invalid number of total cpus %u"`).

So a GPSIMD library targets **either** a single Vision-Q7 core (`total_cpus=1`)
**or** the full 8-core cluster (`total_cpus=8`, the default/built-in case).
`total_cpus` rides every representation of the lib (`kbin_ucode_lib @+0x15`,
`ucode_lib @+0x35`, `ucode_lib_info @+0x0d`, `SUNDA_UCODE_LIB_LIBRARY_TABLE_ENTRY
@+0x04`; `cpu_id` always one byte before). `[HIGH × OBSERVED]` for the parse + table
carry.

> **NOTE — `total_cpus` is a *fan-out* attribute, not a runtime branch.** The host
> never branches its execution over `total_cpus`; it bakes the byte into the
> per-NeuronCore opcode table (§2) and the **device** side honours it by reading
> `get_cpu_id()`/`get_cpu_count()`. A `total_cpus=1` op runs on the one Q7 core
> bound via `cpu_id==0`; a `total_cpus=8` op runs the SPMD partition on all 8.
> The device dispatcher routine that actually spawns the N-way fan-out for
> `EXTENDED_INST 0xF0` is device firmware and is **out of scope** here (`LOW`).
> `[HIGH × OBSERVED` parse/carry; `MED × INFERRED` device honouring.]

---

## 2. Broadcast install — one opcode→lib table per NeuronCore, 8 cores share it

### 2.1 `ucode_stage_libs_table @0x3108e0` — collect 8 DMA queues, bake one table

`[HIGH × OBSERVED]` — re-disassembled this pass. Reached at LOAD/STAGE time via
`dlr_kelf_stage_model_add → kbl_model_add → ucode_stage_libs @0x310ea0 →
ucode_stage_libs_impl @0x310c00 → ucode_stage_libs_table`, under
`ulib_staging_lock` keyed by `db_physical_core_get_mla_and_tpb`.

```c
// ucode_stage_libs_table @0x3108e0  (annotated; real symbols named)
void ucode_stage_libs_table(model_t *model /* rbx */) {
    ulsi_t *ulsi = model + 0x1950;            // ucode_lib_set_info

    // (A) PER-CORE DMA QUEUE COLLECTION — the ONLY genuinely per-core artifact.
    //     Two nested loops scan 16 engines x 16 queues, collect UP TO 8 pairs.
    int n = 0;
    for (int eng_id = 0; eng_id < 16; eng_id++)            // r12d
      for (int q = 0; q < 16; q++)                          // ebx, cmp $0x10
        if (dma_is_custom_op_dma_v2(eng_id, q)) {           // idx-16 match
            uintptr_t pa = get_dma_engine_pa(eng_id);
            m2s[n] = get_dma_queue_offset(pa, q, /*ctl=*/1); // rsp+0x48+n*8
            s2m[n] = get_dma_queue_offset(pa, q, /*ctl=*/0); // rsp+0x88+n*8
            if (++n == 8) goto bake;                          // cmp $0x8 @0x3109b8
        }
    // => exactly 8 M2S/S2M doorbell offset pairs ==
    //    SUNDA_UCODE_LIB_INFO.dma_queue_m2s_offset[8] / _s2m_offset[8].
    //    "8" == the 8 pool cores (get_cpu_count()).

  bake:
    // (B) OPCODE-TABLE BAKE — one entry per custom-op function, 0x118-byte stride,
    //     up to 0x60..0x97 slots (cmp $0x60 @0x310af3 — SUNDA_UCODE_LIB_INFO table[97]).
    for (each func / ucode_lib_info uli) {
        entry->valid             = 1;
        entry->cpu_id_flag       = (uli->cpu_id(+0xc)    == 1);  // cmp $0x1 @0x310a8f
        entry->total_cpus        =  uli->total_cpus(+0xd);       // movzbl @0x310a99
        entry->stripped_lib_addr =  uli->address(+0x0);          // baked device VA
        entry->opcode            =  func->opcode;                // movzwl @0x310ada
        strncpy(entry->func_name, func->name, 256);              // @0x310ac3
    }
    // => ONE per-NeuronCore opcode->library resolution table carrying
    //    (opcode, cpu_id, total_cpus, baked addr, name) for every custom-op fn.
}
```

The 8-pair DMA-queue collection is the **only** per-core install artifact; the
opcode table itself is one table shared by all 8 cores.

### 2.2 `ucode_switch_libs @0x311100` — the broadcast install

`[HIGH × OBSERVED]` — full body re-disassembled this pass.

```asm
311110: call db_physical_core_get_mla_and_tpb     ; -> tpb (out param at 0x8(%rsp))
311119: mov  0x1950(%rbx),%rax                     ; ulsi = model->ucode_lib_set_info
311120: mov  $0x1,%edx                             ; flag = 1
311125: mov  0x8(%rax),%rax                        ; ulsi->ucode_table (dmem_t*)
311129: mov  0x28(%rax),%rsi                       ; table device VA
311131: mov  0x8(%rsp),%rax
311136: mov  (%rax),%rdi                           ; tpb_mem_base
311139: call aws_hal_q7_swap_table                 ; (tpb_mem_base, table_VA, 1)
```

> **QUIRK — broadcast by one HAL call.** There is **no per-core image-DMA loop** in
> `ucode_switch_libs`: a *single* `aws_hal_q7_swap_table` points the NeuronCore's Q7
> engine at the staged table, and all 8 pool cores resolve opcodes through it. The
> cores differentiate purely at runtime (PRID/`get_cpu_id`) plus their 8 per-core DMA
> queues. (Same mechanism shape as `aws_hal_q7_swap_file_io_table` for stdio, §5.)
> `[HIGH × OBSERVED]`

### 2.3 Broadcast vs per-core, tabulated

| Resource | Granularity | Why |
|---|---|---|
| Library image / opcode table | **BROADCAST** | one `swap_table` per NeuronCore; 8 cores share it |
| Custom-op DMA queues | **PER-CORE** | 8 `m2s`/`s2m` doorbell pairs (idx-16 rings) |
| SoC DRAM window | **PER-CORE** | PRID-rebased in `dram_addr_to_soc_addr` (§3) |
| `pool_stdio` ring | **SHARED ring** per NeuronCore | 8×2 array dimensioned; this build wires slot `[0]` (§5) |
| Work / data partition | **PER-CORE at runtime** | by `get_cpu_id()` over `total_cpus` |

---

## 3. Per-core SoC remap — `dram_addr_to_soc_addr` (PRID → private window)

`[HIGH × OBSERVED]` — `data_transfer.o dram_addr_to_soc_addr @0x210` re-disassembled
byte-exact this pass. This is the **device half** of the per-core DMA selection §2.1
stages.

```c
// data_transfer.o dram_addr_to_soc_addr(uint32 dram_addr) @0x210  (ncore2gp)
uint64 dram_addr_to_soc_addr(uint32 dram_addr) {
    // (1) APERTURE GUARD: input must be in LOCAL DRAM window [0x80000, 0x90000).
    if ((dram_addr & 0xFFFF0000) != 0x80000)   // a4 = 1<<19
        _Assert(...);

    memw;                                       // ordering fence
    uint64 base = SoC_base;                      // *(.data 0x2c) lo, *(.data 0x28) hi

    // (2) PER-CORE INDEX FROM PRID, via a balanced bgei/bnei tree:
    uint32 cpu = rsr.prid;  if (cpu >= 8) _Assert(...);
    uint32 index = 9 + 2*cpu;                    // 0->9 1->11 ... 7->23

    // (3) soc = base + (index<<16) + (dram_addr - 0x80000), 64-bit w/ carry.
    return base + ((uint64)index << 16) + (dram_addr & 0xFFFF);
}
```

Each Q7 pool core rebases the **same** local DRAM address into **its own** slice of
SoC address space, keyed by its PRID; the per-index step is `0x10000` (`<<16`), so the
8 cores' DRAM apertures are **disjoint**. A load/store naming a local DRAM address
resolves to a different physical SoC location per core — SPMD addressing closed end to
end. `init_dma_queue @0x314` asserts `READ_LOCAL_UREG64(MEM_WINDOW0_LO) ==
SUNDA_APB_BASE` before programming the per-core DMA queue. `[HIGH × OBSERVED]` for the
bytes; `[MED × INFERRED]` for the "9..23 = coretype numbering" label.

---

## 4. Inter-core coordination — shared SBUF, host-emitted join, **no** Q7 collective

### 4.1 No device-side inter-core primitive `[HIGH × OBSERVED]`

An `xtensa-elf-nm` sweep across **all** members of `libneuroncustomop.a` finds **zero**
`barrier` / `semaphore` / `allreduce` / `collective` / `spinlock` / `__atomic` /
`rendezvous` symbols (verified empty this pass). The only "sync"-like symbols are weak
libc++ condvar **stubs** (intra-core threading shims) and `fsync`. The Q7 device runtime
implements **no** pool-core-to-pool-core handshake in software.

> **GOTCHA — this matches, and is the host elaboration of,
> [The Multicore API (8-core SPMD)](../abi/multicore-spmd.md).** That page proves the
> *device* side ships no cross-core barrier/semaphore/atomic and that the 8 cores are
> "fully independent within the custom-op library"; **this** page supplies the
> *host-side* join that actually serialises them — the `DRAIN` + completion-semaphore
> below. The two pages are consistent: the only barrier per op lives in the **host**
> program, not in device code. `[HIGH × OBSERVED]`

### 4.2 Per-core commit fence = `memw` in `customop_cleanup()` `[HIGH × OBSERVED]`

```asm
; wrapper_api.o customop_cleanup @0x5d4  (tail)
 5e9: callx8 a2        ; ArgParser dtor
 5ee: callx8 a2
 5f1: memw             ; flush THIS core's SBUF/HBM/DMA writes to global visibility
 5fa: retw.n
```

This is each core's "my results are committed" fence — the precondition for the host
completion gate to observe correct data.

### 4.3 The join is host-emitted in the POOL instruction stream `[HIGH × OBSERVED]`

The host lowering for a pool-custom op
(`translate_one_pseudo_embedding_update_instr_v2`) emits, per op:

```
[dma_config x2][dma_config_size]
[LOAD_POOL_ARGUMENT 0x107A]   ; emb base, sbuf base, one_value_read_addr, completion_write_addr
[DRAIN 0x10A2]                ; add_drain @0x273e40 (also emits a paired 0x10A1)
[EVENT_SEMAPHORE]             ; events.update_mode = 0x13 = increment-on-done
```

The `DRAIN` opcode is confirmed byte-exact: `add_drain @0x273e40` does
`mov $0x10a2,%eax; mov %ax,(%rsp)`.

- **`DRAIN` (POOL barrier)** stalls the SP/SEQ sequencer until the op's descriptor block
  fully drains — i.e. until **all 8 cores'** DMA/compute for that op complete. It is the
  cross-core **JOIN**.
- **`EVENT_SEMAPHORE`** posts `completion_write_addr` (= `tdrv_arch_get_sem_inc_addr`, a
  NeuronCore semaphore-increment CSR); the host observes that **one** increment as the
  op's completion.

So the 8 cores run independently; their synchronisation is the host-built `DRAIN` + the
single completion semaphore, **not** a peer Q7↔Q7 protocol. Exactly **one** logical
barrier per op, in the host program. `[HIGH × OBSERVED` on the emit; `INFERRED` that
`DRAIN` waits on all 8 — from its barrier semantics + the absence of any other join.]

### 4.4 Shared SBUF + cross-core reduction `[HIGH/MED]`

`LOAD_POOL_ARGUMENT` carries **one** `sbuf_base_addr` / embedding-table base per op; all 8
cores attached to the POOL engine see that same on-chip State-Buffer window (a partitioned
resource — `cayman_get_num_sbuf_partitions`, `cayman_is_address_in_my_sbuf`,
`cayman_sbuf_base_addresses @0x9ca0c0`). Each core indexes its slice by `get_cpu_id()` over
`total_cpus` — the classic SPMD partition. The SBUF **is** the inter-core shared memory; no
separate Q7 scratch-exchange protocol exists.

Because no `allreduce`/reduction symbol exists device-side, a reduction across the 8 cores'
partials is **data-movement-driven**: either (a) the host CCE/SDMA compute-DMA path
(`add_dma_packet_cce`, `SDMA_CCETYPE ADD/MIN/MAX/FMA`) folds partials in the DMA engine, or
(b) the kernel writes all partials to a shared SBUF/HBM region and one designated core
(e.g. `get_cpu_id()==0`) gathers them **after** the `DRAIN`. Either way it is movement-/
single-core-gather based, not a Q7 collective. `[MED × INFERRED]`

---

## 5. The `pool_stdio` per-core ring drain — end to end (8×2 layout, wraparound)

### 5.1 Layout `[HIGH × OBSERVED]` (DWARF, this pass)

`pool_stdio_block_t` member offsets and the `[8]` array bounds read directly from DWARF
(`DW_AT_upper_bound = 7` ⇒ `[8]`; `DW_AT_byte_size = 56` for the queue):

```c
struct pool_stdio_block_t {                    // ends at +0x490 (hbm_space_dmem), == tpb_t+0x3DE8
    char               name[256];              // +0x000  (data_member_location 0)
    bool               initialized;            // +0x100  (256)
    const physical_core_t *pcore;              // +0x108  (264)
    pool_stdio_queue_t stdout_queues[8];       // +0x110  (272;   upper_bound=7)
    pool_stdio_queue_t stderr_queues[8];       // +0x2d0  (720;   upper_bound=7)
    dmem_t            *hbm_space_dmem;          // +0x490  (1168;  0x440-B HBM block, align 0x800)
};                                             //   the [8] arrays = the 8 pool cores

struct pool_stdio_queue_t {                    // DW_AT_byte_size 56 (0x38 stride)
    pool_stdio_block_t *stdio_block;           // +0x00  back-pointer
    bool                initialized;           // +0x08
    uint32              q7_idx;                // +0x0c  (which pool core this ring is for)
    uint32              queue_size_entries;     // +0x10  capacity
    dmem_t             *host_queue_dmem;        // +0x18  ring storage
    void               *read_idx_ptr_write_only;// +0x20 host-WRITE-only, published to device
    void               *write_idx_ptr;          // +0x28 device-advanced write cursor
    uint64              read_idx_local;         // +0x30 host's local consumed counter
};
```

> **CORRECTION — `+0x0c` is `q7_idx`, not a "read-byte cursor".** A prior framing labelled
> `pool_stdio_queue_t +0x0c` a "read-byte cursor"; DWARF this pass names it **`q7_idx`** (the
> pool-core index the ring belongs to). Likewise `+0x20` is precisely
> **`read_idx_ptr_write_only`** (host writes it, device reads it) and `+0x18` is
> **`host_queue_dmem`**. The read cursor the host advances is `read_idx_local` at **`+0x30`**.
> `[HIGH × OBSERVED]`

### 5.2 `pool_stdio_block_init @0x300c70` — wires slot `[0]` `[HIGH × OBSERVED]`

```c
void pool_stdio_block_init(pool_stdio_block_t *sb, ...) {
    if (sb->initialized) return;               // guard +0x100
    sb->pcore = pcore;                          // +0x108
    int hbm = get_default_hbm_index();
    dmem_alloc_aligned(&sb->hbm_space_dmem, 0x440, DMA_MEM_USAGE_TYPE_POOL_STDIO, 0x800);
    dmem_memset(sb->hbm_space_dmem, 0, 0x440);

    if (entries_stdout)                          // pool_stdout_queue_size_bytes / 0x100
        pool_stdio_queue_init(&sb->stdout_queues[0], sb, &info, entries_stdout, 0x400);
    if (entries_stderr)                          // 0x400 / 0x100 = 4
        pool_stdio_queue_init(&sb->stderr_queues[0], sb, &info, entries_stderr, 0x420);

    dmem_buf_copyin(/* 0x280 info_table */ ...); // -> device
    aws_hal_q7_swap_file_io_table(tpb, hbm_block_VA); // point Q7 file-IO table at HBM block
}
```

> **NOTE — the `[8]` arrays are dimensioned, but this build wires slot `[0]` only.** `block_init`
> initialises **one** shared stdout queue + **one** shared stderr queue per NeuronCore. The 8-slot
> arrays exist for 8 pool cores, but per-core fan-out in this build is by ring **entry** (each entry
> carries `q7_idx`), not by a separate per-core queue; slots `[1..7]` stay un-initialised and are
> **skipped** at drain time (each guarded by its `+0x08 initialized` byte). `[HIGH × OBSERVED]`

### 5.3 `pool_stdio_queue_init.constprop.0 @0x300a10` — one queue `[HIGH × OBSERVED]`

- `entries` (r8d) must be **power-of-two** (`lea -1; test; jne → error log`).
- Allocate `entries*0x100 + 4` (capped `0x40000`) via `dmem_alloc_aligned(align 0xe)`; zero.
- Fields: `+0 stdio_block`; `+0x08 initialized=1`; `+0x0c q7_idx=0`; `+0x10 queue_size_entries`;
  `+0x18 host_queue_dmem`; `+0x20 read_idx_ptr` VA (`DMEM_GET_VA + base 0x400/0x420`);
  `+0x28 write_idx_ptr` VA; `+0x30 read_idx_local=0`.
- On-device descriptor head: `u16 MAGIC=0x201 @+0`; `u8 0 @+2`; `u32 capacity @+0xc`;
  `u64 data PA @+0x10`; head/write VAs `@+0x18/+0x20`.
- Device ring entry size = `0x100 = 256 B` (`pool_stdio_get_entry_size @0x300a00` is literally
  `mov $0x100,%eax; ret`).

### 5.4 `pool_stdio_queue_available_count @0x3010f0` — pending, clamped `[HIGH × OBSERVED]`

```asm
301106: cmpb $0x0,0x8(%rdi)      ; guard: queue->initialized
30111f: mov  0x28(%rdi),%rax     ; write_idx_ptr
301123: mov  0x10(%rdi),%ebp     ; cap = queue_size_entries
301126: mov  (%rax),%eax         ; write = *write_idx_ptr   (device cursor)
301128: sub  0x30(%rdi),%eax     ; pending = write - read_idx_local
30112b: cmp  %eax,%ebp           ; if cap >= pending ...
30112f: mov  %eax,(%rbx)         ;   *out = pending ; ret 0
        ; else *out = cap (CLAMP) ; imul $0xf0 -> byte size in 0xf0 host-record unit ; nlog overflow
```

Pending entries since the last drain, clamped to capacity. A **Q7-faster-than-host overrun is
logged but not fatal** — the host keeps only the last `cap` entries. `[HIGH × OBSERVED]`

### 5.5 `pool_stdio_queue_consume_all_entries @0x3011d0` — wraparound copy-out `[HIGH × OBSERVED]`

```c
// args: rdi = queue, esi = stream_tag (3 = stdout, 1 = stderr)
int pool_stdio_queue_consume_all_entries(pool_stdio_queue_t *q, int stream_tag) {
    uint32 n; int rc = pool_stdio_queue_available_count(q, &n);
    if (n == 0) return rc;

    void *recs = malloc((n + 1) * 0xf0);        // host records, 0xf0 stride
    /* preamble seeded by two 16-B SIMD consts @0x853c80/0x853c90; stderr path when esi==1 */

    uint32 cap   = q->queue_size_entries;        // +0x10
    uint32 start = q->read_idx_local % cap;      // ring start  (div)
    uint32 head_run = cap - start;
    uint32 esz   = pool_stdio_get_entry_size();  // 0x100

    if (head_run >= n) {                          // single contiguous segment
        for (uint32 i = 0; i < n; i++)
            dmem_buf_copyout(q->host_queue_dmem,  // ring base VA (+0x18)
                             rec_slot(recs, i),    // host record (+0xf0 stride)
                             esz*(start + i) + 0x10, // +0x10 SKIPS the 16-B entry header
                             0xf0);
    } else {                                      // TWO segments — wrap to ring base
        copy_segment(start, cap - 1);             // seg A: [start..cap-1]
        copy_segment(0, n - head_run - 1);        // seg B: [0..tail-1]
    }
    nul_terminate_last(recs);

    // ADVANCE + PUBLISH back to the Q7 — frees ring slots for reuse.
    q->read_idx_local += n;                       // +0x30
    *(uint64*)q->read_idx_ptr_write_only = q->read_idx_local; // +0x20 host-WRITE-only

    nlog(stream_tag, recs);                        // emit drained text (3=stdout / 1=stderr)
    free(recs);
    return rc;
}
```

The `+0x10` skip drops the 16-byte device entry header; the host record is `0xf0` bytes per
`0x100`-byte device entry. Publishing `read_idx_local` through `read_idx_ptr_write_only` is what
lets the Q7 reuse ring slots.

### 5.6 The block-level drain — `exec_request_progress_one_step @0x263330` `[HIGH × OBSERVED]`

```c
// per-progress-step host consumer — the 8×2 drain loop
for (int i = 0; i < 8; i++) {                              // cmp $0x8 @0x2638f1, stride 0x38
    if (sb->stdout_queues[i].initialized)                  // guard tpb+0x3f00+i*8
        pool_stdio_queue_consume_all_entries(&sb->stdout_queues[i], /*stream_tag=*/3); // call @0x263928
    if (sb->stderr_queues[i].initialized)                  // guard tpb+0x40c0+i*8
        pool_stdio_queue_consume_all_entries(&sb->stderr_queues[i], /*stream_tag=*/1); // call @0x2639a0
}
```

Up to **8×2 = 16** queue drains per step; in this build **1×2** (only slot `[0]` is wired). The
loop runs interleaved with `notification_read_exec_queue` (the `INFER_STATUS` POOL NQ poll) in
the same progress step.

> **End-to-end.** Q7 kernel `printf` → device file-IO table (`swap_file_io_table`) writes a 256-B
> entry into the HBM ring and advances `write_idx` → host `available_count` sees
> `write_idx − read_idx_local` → `consume_all_entries` copies entries out (wraparound-aware,
> `0x10` header skipped) → host publishes `read_idx` back → Q7 reuses slots. Overflow is logged,
> not fatal. The device producer side is
> [On-Device Virtual File-I/O Manager](../firmware/kernels/file-io-manager.md). `[HIGH × OBSERVED]`

---

## 6. The teardown path — `tdrv_destroy` → core_destroy ×13 → ll/stdio/lib_set

`tdrv_destroy @0x269a70` (2204 B) runs **per NeuronCore** and reverses the bring-up. Re-disassembled
byte-exact this pass. `[HIGH × OBSERVED]`

### 6.1 Outer frame (per device / mla)

```c
ctx = db_tdrv_ctx_get();
for (mla in ctx->mla[]; stride 0x900b8; end ctx+0x1201700) {
    if (!mla->valid /*+0x8*/) continue;
    per_tpb_teardown(mla);                       // §6.2 (entered at 0x269c80)
    aws_hal_intc_destroy(mla + 0x8d418);
    dml_free(mla + 0x8cd08);
    tdrv_close_nds_for_device(mla);
    pthread_create(device_close_fn, mla);        // async device-close thread
}
pthread_join(/* the spawned close threads */);
tdrv_free_instance_info_cache(); vtpb_ctx_destroy(); free(ctx);
db_tdrv_ctx_clear(); csr_deregister();
```

### 6.2 Per-TPB teardown — the GPSIMD spine

```c
tpb = db_get_tpb_from_mla(mla, tpb_idx);
int halt_rc = hw_exec_queue_add_halt_request(tpb);   // gates the core destroys

// (a) 5 NX ENGINE CORES — PE, ACT, SP, DVE, POOL
for (int i = 0; i < 5; i++) {                         // cmp $0x5 @0x269cf4
    core = tpb[0x9918 + i*8];                          // nrtucode_core[5]
    if (core) { ucode_core_print_logs(core); ucode_core_destroy(core); } // @0x2267d0
}

// (b) LOADABLE LIBRARIES
for (int i = 0; i < tpb[0x9988] /* num_pooling_q7_ll */; i++) {
    ll = ((void**)tpb[0x9980])[i];                     // pooling_q7_ll array
    if (ll) ucode_ll_destroy(ll);                       // @0x226a70
}
free(tpb[0x9980]);                                     // the ll array pointer

// (c) 8 Q7 POOL CORES
for (int i = 0; i < 8; i++) {                          // cmp $0x8 @0x269dbf
    core = tpb[0x9940 + i*8];                           // pooling_q7_nrtucode_core[8]
    if (core) { ucode_core_print_logs(core); ucode_core_destroy(core); }
}
// => 5 NX + 8 Q7 = 13 ucode_core_destroy calls per NeuronCore.

// (d)-(g) rest of the spine
notification_destroy(tpb + 0x20);                      // per-engine NQ ring
if (!halt_error)
    pool_stdio_block_destroy(tpb + 0x3de8);            // the 8x2 stdio block
pthread_mutex_lock(tpb + 0x4de8 /* ulib_staging_lock */);
ucode_free_lib_set(tpb + 0x4de0 /* ulib_set_info_extisa_only */);
pthread_mutex_unlock(...); pthread_mutex_destroy(...);
free(tpb + 0x42b0);
ht_destroy(tpb + 0x4e40 /* model_db */);
// (h) per-seq-engine loop (stride 0x3ff8): notification_destroy, dma_ring_free,
//     dma_queue_free_h2d_queue (is_h2d_engine), free.
```

The NX and Q7 loop bounds are byte-exact this pass:

```asm
269ccf: mov  0x9918(%rbp,%rbx,8),%rdi    ; NX core[i]
269ce0: call 2267d0 <ucode_core_destroy>
269cf4: cmp  $0x5,%rbx                    ; 5 NX cores
...
269da4: call 2267d0 <ucode_core_destroy> ; Q7 core[i]  (mov 0x9940(...) just above)
269dbf: cmp  $0x8,%rbx                    ; 8 Q7 cores
```

> **GOTCHA — teardown always destroys all 8 Q7 cores, regardless of any op's `total_cpus`.**
> `total_cpus` is an execution-time fan-out attribute (§1.1), **not** a lifecycle count. The 8
> pool cores are built unconditionally at bring-up and destroyed unconditionally here. A
> reimplementation must not skip Q7 cores `[1..7]` for a `total_cpus=1` library. `[HIGH × OBSERVED]`

> **NOTE — these tpb offsets re-confirm the `tpb_t` map byte-exact.** `0x9918` (NX core array),
> `0x9940` (Q7 core array), `0x9980`/`0x9988` (ll array + count), `0x3de8` (stdio block),
> `0x4de0`/`0x4de8` (ext-ISA lib set + its lock), `0x4e40` (model db). `[HIGH × OBSERVED]`

### 6.3 `ucode_core_destroy @0x2267d0` — one core teardown `[HIGH × OBSERVED]`

```c
void ucode_core_destroy(core_t *core) {
    ctx = ucode_lib_core_get_context(core);     // dlsym'd nrtucode_core_get_context
    ucode_lib_core_destroy(core);               // device-side: stop image, free IRAM/DRAM
    ud = ucode_lib_context_get_userdata(ctx);
    free(ud);                                    // the 0x18 nrtucode_userdata_t from core_create
    ucode_lib_context_destroy(ctx);             // nrtucode_context_destroy
}
```

Each of the 13 cores follows this through the `dlsym` bridge into `libnrtucode_extisa.so`.

### 6.4 `ucode_ll_destroy @0x226a70` `[HIGH × OBSERVED]`

A thin trampoline to the `dlsym`'d `ucode_lib_ll_destroy` (`= nrtucode_ll_destroy`) for one
loadable-library handle.

### 6.5 `ucode_free_lib_set @0x311060` — release staged ext-ISA lib set `[HIGH × OBSERVED]`

```asm
311072: mov 0x28(%rdi),%rdi ; call dmem_free   ; set->lib_dmem
31107b: mov 0x00(%rbp),%rdi ; call dmem_free   ; set->scratch_space
311084: mov 0x08(%rbp),%rdi ; call dmem_free   ; set->ucode_table
31108d: mov 0x10(%rbp),%rdi ; call dmem_free   ; set->extram
3110a4: mov 0x18(%rbp),%edx                     ; count
3110b7: ...  free(libs[i].funcs /*+0x18*/) ... ; then free the libs[] array (+0x20)
```

Frees the device opcode→lib table, scratch, extram, and each per-lib image — matching the
`ucode_lib_set_info` layout exactly.

### 6.6 Device-side per-core CRT teardown `[HIGH × OBSERVED]`

`libneuroncustomop.a` registers `my_exit @0xaf0` via `atexit`; `_exit @0x890` runs `_fini`. When a
Q7 core's image is unloaded, its self-contained libc/libc++ CRT runs `my_exit` then `_fini` on that
core. `customop_cleanup`'s `memw` (§4.2) has **already** flushed results before any such unload.

### 6.7 Teardown order (synthesis)

```
halt exec queue
  -> [5 NX cores: print_logs + core_destroy]
  -> [ll_destroy xN + free ll array]
  -> [8 Q7 cores: print_logs + core_destroy]        (= 13 core_destroy)
  -> notification_destroy
  -> pool_stdio_block_destroy   (detach Q7 file-IO table, dmem_free queues + 0x440 block)
  -> ucode_free_lib_set         (under ulib_staging_lock)
  -> free model db / DMA rings
  -> (outer) device_close threads + ctx free
  -> [each Q7n] my_exit + _fini on image unload
```

Module-level `ucode_teardown_module` `dlclose(libnrtucode_extisa.so)` follows later at `nrt_close`.
The abort/error surface around `hw_exec_queue_add_halt_request` and the conditional skip of
`pool_stdio_block_destroy` on a halt error is detailed in
[Host Model Lifecycle + Error-Handling Model](lifecycle-error-model.md). `[HIGH × OBSERVED]`

---

## 7. The full diagram — 8 pool cores, one op, install → execute → teardown

Legend: `[H]` host libnrt (x86); `[HW]` SP/SEQ sequencer + DMA; `[Q7n]` pool core n (0..7).

```
INSTALL (nrt_load / stage, §2):
 [H] ucode_stage_libs_table: collect 8 per-core custom-op DMA queues (m2s/s2m);
     bake ONE opcode->lib table {opcode, cpu_id, total_cpus, addr, name}[<=97].
 [H] ucode_switch_libs -> aws_hal_q7_swap_table(tpb, table_VA)    (BROADCAST to 8).
 [H] pool_stdio_block_init -> swap_file_io_table (HBM stdio ring, §5).
 [Q7n @ image load] _GLOBAL__sub_I_parallel.cpp: cpu_id = rsr.prid   (rank n).

PER nrt_execute (one pool-custom op, §4):
 [H] doorbell: ndl_nc_semaphore_increment(start_sem)              === KICKOFF ===
 [HW] SP/SEQ fetches the POOL stream:
       - DMA-trigger WRITEs fire the 8 idx-16 custom-op DMA queues (HBM<->SBUF),
         each core's pair selected by PRID / dram_addr_to_soc_addr.
       - LOAD_POOL_ARGUMENT(0x107A): one {sbuf_base, emb base, one_value_read,
         completion_write} for the op.
       - EXTENDED_INST(0xF0): SEQ hands the op to the Q7 POOL engine.
  |
  +--> [Q70..Q77] ALL 8 cores run the SAME image, SPMD:
  |       cpu = get_cpu_id() in {0..7};  n = get_cpu_count() == 8
  |       x = customop_next_tensor()   (shared sbuf_base; slice by cpu/total_cpus)
  |       ... compute this core's partition into shared SBUF / per-core DRAM ...
  |       customop_return_tensor(y)
  |       customop_cleanup() -> memw    (flush THIS core's writes)
  |
 [HW] DRAIN(0x10A2): sequencer waits until ALL 8 cores drain        === JOIN ===
 [HW] EVENT_SEMAPHORE: completion_write_addr CSR += 1
  |                                                            |
 [H] exec_request_progress_one_step:                          v
       notification_read_exec_queue (INFER_STATUS POOL NQ) <== one completion
       for i in 0..7: consume_all_entries(stdout_queues[i],3)  (drain Q7 printf)
                      consume_all_entries(stderr_queues[i],1)
 [H] eventfd signalled -> reap; return to caller.

TEARDOWN (tdrv_destroy, §6):
 [H] halt exec queue -> 5 NX print+destroy -> ll_destroy xN -> 8 Q7 print+destroy
     (= 13 core_destroy) -> notification_destroy -> pool_stdio_block_destroy ->
     ucode_free_lib_set -> ht/DMA-ring free -> device_close threads -> ctx free.
 [Q7n] my_exit + _fini on image unload.
```

---

## 8. Confidence ledger & open items

**`HIGH × OBSERVED` (this pass):** `get_cpu_id`/`get_cpu_count`/`_GLOBAL__sub_I` ctor (PRID,
byte-exact); `dram_addr_to_soc_addr` aperture + `9+2*cpu` index; `customop_cleanup` `memw`; **no**
device inter-core primitive (`nm` sweep empty); `ucode_stage_libs_table` 8-DMA-queue collection +
opcode-table bake (`cpu_id`/`total_cpus` carry); `ucode_switch_libs` single `swap_table` broadcast
(re-disassembled); `pool_stdio_block_t` `[8]`/`[8]` arrays (DWARF `upper_bound=7`) + full member
offsets; `pool_stdio_queue_t` 56-B layout (DWARF field names: `q7_idx`,
`read_idx_ptr_write_only`, `read_idx_local`); `available_count` write−read clamp;
`consume_all_entries` wraparound (`0x100` entry / `0x10` header skip / `0xf0` host record, read-idx
publish, stream tags 3/1); `get_entry_size` literal `0x100`; `exec_request_progress` 8×2 drain
loop; `tdrv_destroy` spine (5 NX + 8 Q7 = 13 `ucode_core_destroy`, `ll_destroy`,
`pool_stdio_block_destroy`, `ucode_free_lib_set`) with byte-exact loop bounds (`cmp $0x5`/`$0x8`);
`ucode_free_lib_set` `dmem_free` chain; `add_drain` `0x10A2`.

**`MED × INFERRED`:** `total_cpus` "fan across N cores" device use (parse + table OBSERVED, the
device honouring INFERRED); `DRAIN` waits-on-all-8 (barrier semantics + absence of other join);
per-core SBUF slicing convention (shared base OBSERVED, by-`cpu_id` slicing INFERRED); cross-core
reduction via CCE or single-core gather; the `9..23` SoC index "= coretype numbering" label.

**`LOW` / not covered:** the exact device dispatch routine that reads `total_cpus` to spawn the
N-way fan-out for `EXTENDED_INST 0xF0` (device firmware — a separate pass); the meaning of the
paired `0x10A1` DRAIN variant; the field encoding of the `0xf0` host stdio record preamble (the
two 16-B SIMD constants); the async-exec teardown variant (`kmgr_async_exec_*`).

---

## 9. See also

- [The Multicore API (8-core SPMD)](../abi/multicore-spmd.md) — the device-side proof that no
  cross-core barrier/semaphore/atomic ships in `libneuroncustomop.a`; this page is its host-side
  join elaboration.
- [Execute-Time GPSIMD Custom-Op Dispatch](execute-time-dispatch.md) — the host enqueue path
  (`LOAD_POOL_ARGUMENT 0x107A`, `EXTENDED_INST 0xF0`) that this page joins and tears down.
- [Host Model Lifecycle + Error-Handling Model](lifecycle-error-model.md) — the abort/error
  surface around `tdrv_destroy`'s halt gate.
- [On-Device Virtual File-I/O Manager](../firmware/kernels/file-io-manager.md) — the device
  producer for the `pool_stdio` ring this page drains.
