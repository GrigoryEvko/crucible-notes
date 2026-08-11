# The libnrt Runtime Synthesis

> **RT-lane capstone.** This page is the narrative consolidation of the whole
> host runtime lane: the end-to-end host-side model a reimplementer needs to
> bring up a GPSIMD / Vision-Q7 ("Pool") engine, install a custom kernel, run it,
> and tear it down. It threads six arcs — **(1)** the runtime surface + layering,
> **(2)** the LOAD path, **(3)** the EXECUTE path, **(4)** the multi-core SPMD /
> multi-model model, **(5)** the lifecycle + error/recovery model, **(6)** the
> `NRT_2.0.0` vs `NRT_3.0.0` version axis — and points each into the RT detail
> page that owns the byte-level evidence. It does **not** re-derive the per-call
> API reference table.

Every claim is anchored to an address, symbol, string, or detail-page reference;
the page default is `[HIGH·OBSERVED]` and claims that depart from it carry an
explicit tag. All addresses are file offsets in the host x86-64 ELF, which is
VMA==file-offset for `.text`/`.rodata`/`.data` (verified below); the device-side
Vision-Q7 facts are CARRIED from the device carves (`ncore2gp xtensa-elf-objdump`),
never host-resident.

---

## 0. One screen — the whole host runtime in one breath

`libnrt.so.2.31.24.0` is the **entire** AWS Neuron userspace runtime in a single
ELF: `122,956,336` bytes, ELF64 x86-64, `BuildID 8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`,
not stripped (`.debug_info` present), `17,372` functions, version string
`libnrt version 2.31.24.0`, git `0b044f4ce`, internally branded `KaenaRuntime` +
`KaenaHal-2.31.0.0`. **[HIGH·OBSERVED]** (`readelf -n` / `objdump -T` /
`rg -a 'libnrt version'`.)

To a GPSIMD reimplementer it presents **four** things stacked into one library:

- a flat **C ABI** — `145` real text exports across two ELF symbol-version nodes
  (`NRT_2.0.0` parent + `NRT_3.0.0` child); see [the libnrt surface map](libnrt-surface.md);
- a C++ **device-programming core** (`nrt` / `kmgr` / `tdrv` / `kbin` / `dmem` /
  `ucode` shim) that turns a NEFF into a live device program;
- a vendored Annapurna/Cadence **hardware-abstraction layer** (`aws_hal_*`,
  `KaenaHal-2.31.0.0`, per-arch Sunda/Cayman/Mariana dispatch); see
  [the aws_hal_q7 HAL](aws-hal-q7.md);
- a statically-linked **driver portal** (`ndl_*` IOCTL/mmap) plus a small **Rust**
  slice (`neuron_rustime` → the `nrta_*` `NRT_3.0.0` family).

The GPSIMD spine through all four is one path, and the single most important
reimplementation fact is what it omits:

```c
/* The host runtime NEVER runs the kernel. It (a) INSTALLS the kernel image into
 * the Pool engine over BAR0 at init, (b) LOWERS the NEFF into device DMA rings +
 * a POOL instruction stream at load, (c) at each execute fires exactly ONE
 * semaphore increment, and (d) POLLS for completion. Everything else is
 * device-side. This page is the host half. */

nrt_set_pool_eng_ucode(iram, dram);  /* OPTIONAL, pre-init: register custom Q7 image */
nrt_init();                          /* bring up devices; dlopen the ucode bridge; build 5 NX + 8 Q7 cores;
                                        SILENTLY substitute the custom Pool ucode; BAR0-install it; boot Q7s */
nrt_load(neff, size, &model);        /* parse gzip-tar NEFF; build kbin IR; stage custom-op DMA queues + ucode table */
nrt_execute(model, in_set, out_set); /* fire ONE semaphore doorbell that releases the prebuilt POOL stream
                                        carrying LOAD_POOL_ARGUMENT (0x107A); 8 Q7 cores run SPMD; NQ + stdio return */
nrt_unload(model);                   /* ref-counted device un-install */
nrt_close();                         /* teardown */
```

**[HIGH·OBSERVED]** — every symbol in that skeleton is byte-verified below.

---

## 1. The runtime surface + the layering

### 1.1 The exported API

`nm -D --defined-only` prints **151 lines**, but only **145** are real text
exports — `121 nrt_*` + `16 nec_*` (both `NRT_2.0.0`) + `8 nrta_*` (`NRT_3.0.0`);
the other 6 lines are 4 leaked `std::string` `_M_*` helpers and the 2 `*ABS*`
version-node markers. **[HIGH·CARRIED** — exact partition lives in
[libnrt-surface.md](libnrt-surface.md) §"151 vs 145"**]**

> **CORRECTION.** Earlier lane prose said "151-export surface" loosely. The
> precise figure is **145 real exports**; "151" is the raw `nm -D` line count
> including ABI-accident leaks. This page uses 145.

For the GPSIMD path the essential members are a small subset of nine families:

| Family | GPSIMD-essential members | Role |
| --- | --- | --- |
| LIFECYCLE/VERSION | `nrt_init` `nrt_close` `nrt_get_version` | bring devices up/down |
| GPSIMD UCODE | **`nrt_set_pool_eng_ucode`** @`0xc1630` | **THE custom-kernel install seam** |
| NEFF MODEL | **`nrt_load`** @`0xa9fe0` `nrt_unload` @`0xaa190` | NEFF → device program |
| TENSORS/MEMORY | `nrt_tensor_allocate` `nrt_allocate_tensor_set` | host↔device I/O slots |
| EXECUTE | **`nrt_execute`** @`0x91de0` `nrt_execute_repeat` @`0x91650` | release a staged program |
| NRT_3.0.0 ASYNC | `nrta_cc_prepare` `nrta_cc_schedule` `nrta_execute_schedule` … | collectives async overlay (§6) |

**[HIGH·OBSERVED** — all six bolded addresses confirmed via `objdump -T` /
`function_addresses.json`.]** The C ABI is the **only** supported entry;
everything below is internal C++ (subsystem histogram: `aws_hal 815` · `encd 289`
· `tdrv 260` · `nrt 243` · `ndl 122` · `ucode 37` · `kmgr 29` · `dmem 23` …,
CARRIED from [libnrt-surface.md](libnrt-surface.md)).

### 1.2 The four-layer stack

A GPSIMD operation descends through four cleanly separated layers. This layering
is the central structural fact of the runtime — and the one a reimplementer can
partly collapse.

```
LAYER 1   PUBLIC C ABI            nrt_* / nrta_*            (the 145 exports)
          |
LAYER 2   RUNTIME CORE (C++)      kmgr / tdrv / kbin / dmem / ucode shim
          |   kmgr = exec manager (nrt_execute lower half, XU worker pool)
          |   tdrv = TPB driver  (device bring-up, BAR mapping, engine-HAL init,
          |          DMA rings, notifications, ucode dispatch)
          |   kbin = NEFF binary patcher -> mem_ref / dma_desc IR
          |   ucode = a 30-slot dlsym shim into the device-side host lib
          |
LAYER 3a  nrtucode (DEVICE-CODE HOST LIB)   libnrtucode_extisa.so   [dlopen'd at init]
          |   bound through a 30-entry fn-pointer table; libnrt drives it through
          |   TWO const vtables (platform_rw_impl + platform_memhandle_impl) that
          |   give it BAR0 register/memory access *through libnrt*. THE KEY SEAM:
          |   the device-code host library cannot touch the device directly.
          |
LAYER 3b  aws_hal_* (DEVICE HAL)            KaenaHal-2.31.0.0
          |   ONE dispatch substrate: global struct kaena_khal @0xcaeb80 whose
          |   per-arch fn-pointers are filled at init; every aws_hal_q7_* asserts
          |   arch in {Sunda,Cayman,Mariana} then tail-jumps through the slot.
          |
LAYER 4   DRIVER PORTAL (ndl_*)             statically-linked libndl
              ndl_bar_write / ndl_memory_alloc / ndl_nc_semaphore_increment ...
              the raw IOCTL/mmap ABI to /dev/neuron. NO msi/msix/irq symbol ->
              completion is POLL-driven. All physical device writes land here.
```

The two physical write primitives at the bottom — **(a)** bulk image write
(`al_mem_write_buf → axi_write → ndl_bar_write`, word-blasted, used for ucode
IRAM/DRAM install, **programmed-IO not DMA**) and **(b)** single-CSR write
(`al_reg_write32 → csr_write → ndl_bar_write`, one 32-bit word, every control
register) — are the only two ways anything reaches the silicon. **[HIGH·CARRIED**
from [aws-hal-q7.md](aws-hal-q7.md); `kaena_khal @0xcaeb80` and `write_padded
@0x473eb0` confirmed.]**

> **NOTE — what a reimplementer can collapse.** Layers 3a+3b exist in libnrt only
> because the device customop library is a *separate deliverable* that must also
> run on a simulator: the BAR0-callback vtables are the seam that lets the same
> device-code host lib bind to real hardware or a sim. A clean-room
> reimplementation that owns the device-code-host responsibilities itself can fold
> 3a into 3b. **Layer 3b's arch-dispatch is NOT optional** — the register offsets
> genuinely differ across Sunda/Cayman/Mariana (the per-arch `__FILE__` strings
> `aws_hal_q7_{sunda,cayman,mariana}.c` are right there in `.rodata`). **[HIGH·INFERRED]**

---

## 2. The LOAD path — NEFF → metaneff → silent Q7 install → BAR0 → Q7 boot

The load path has **two temporally separate halves** a reimplementer must order
correctly. The GPSIMD kernel **image** is installed at `nrt_init`; the per-model
custom-op **instructions** are staged at `nrt_load`. Getting this ordering wrong
is the first thing that breaks a reimplementation: a custom Pool kernel registered
*after* `nrt_init` is silently rejected.

### 2.1 Init-time engine bring-up (the silent override)

The orchestrator is `tpb_eng_init_hals_v2 @0x2687e0`, run once per NeuronCore. In
order (full per-step table in [nrtucode-bringup.md](nrtucode-bringup.md), the
device-write detail in [aws-hal-q7.md](aws-hal-q7.md)):

```c
/* (0) OPTIONAL, BEFORE nrt_init — the ONLY window a custom Pool-Q7 image is accepted.
 *     nrt_set_pool_eng_ucode @0xc1630 guards nrt_init_state:
 *       state==uninitialized -> store;  ==INIT -> reject 0xd;  ==CLOSED -> reject 0xe.
 *     It calls tdrv_set_pool_eng_ucode @0x2695f0, which stores {iram,dram}{bin,size}
 *     into four process globals pool_eng_*_bin. nrt_ucode_info is 32 B. */
nrt_set_pool_eng_ucode(&info);              /* edge confirmed: -> tdrv_set_pool_eng_ucode */

nrt_init() {
  ucode_init_module @0x225940;              /* dlopen("libnrtucode_extisa.so", RTLD_NOW|GLOBAL);
                                               dlsym 30 nrtucode_* into the table; assert api_level==3;
                                               liveness-probe the DEFAULT Q7 image */
  for each NeuronCore {
    ucode_nx_core_create  x5;               /* the PE/ACT/SP/DVE/POOL NX cores ("TENSOR/SCALAR/
                                               GPSIMD/VECTOR/SYNC"), into tpb+0x9918[] */
    ucode_pooling_q7_core_create x8;        /* the 8 Q7 GPSIMD pool cores, into tpb+0x9940[];
                                               each binds the platform_rw/memhandle vtables and
                                               on_ucode_booted() boots the core */

    /* (3) tpb_eng_init_hals_v2 — THE SILENT OVERRIDE SEAM: */
    rc = ucode_set_q7_ucode_bins(&cfg[Q7], AL_HAL_POOLING_Q7);   /* load the SHIPPED/signed DEFAULT */
    if (rc == 0) {
      if (pool_eng_iram_bin) cfg.iram = {pool_eng_iram_bin, _size};   /* UNCONDITIONAL OVERRIDE */
      if (pool_eng_dram_bin) cfg.dram = {pool_eng_dram_bin, _size};   /* no sig-check, no warn log */
      aws_hal_stpb_init(&tpb->sunda, ...);
    }

    /* (4) the physical device write: */
    aws_hal_q7_ucode_eng_init @0x451080;    /* validate img<=iram/dram size (else "ucode too big!" -> -1);
                                               loop 8 cores: write_padded(DRAM) THEN write_padded(IRAM)
                                               -> al_mem_write_buf -> axi_write -> ndl_bar_write  (BAR0 PIO) */

    /* (5) stdio + start: */
    pool_stdio_block_init @0x300c70;         /* HBM stdout/stderr ring; swap_file_io_table points Q7 at it */
    /* release run-stall: Cayman/Mariana clear host CSR 0x3000 (0xFF->0x00, all 8 at once);
       Sunda is a no-op — its Q7s come online under SEQ/SP via the EVT_SEM fabric, and Sunda
       ships ZERO v2 hw_decode blobs (SW-fetch FSM, not HW-Decode front end). */
  }
}
```

**[HIGH·OBSERVED** for the addresses/edges (`nrt_set_pool_eng_ucode →
tdrv_set_pool_eng_ucode`, `ucode_init_module @0x225940`, `tpb_eng_init_hals_v2
@0x2687e0`, `aws_hal_q7_ucode_eng_init @0x451080`, `write_padded @0x473eb0`,
`pool_stdio_block_init @0x300c70` — all confirmed); the override
instruction sequence and the per-arch run-stall split are **CARRIED** from
[nrtucode-bringup.md](nrtucode-bringup.md) / [aws-hal-q7.md](aws-hal-q7.md) and
the hw-decode divergence from [hw-decode-cam-programming.md](hw-decode-cam-programming.md).]**

> **GOTCHA — the silent substitution.** `tpb_eng_init_hals_v2` loads the stock
> signed Pool ucode, then — if a caller registered a blob before `nrt_init` —
> **unconditionally overwrites the `(bin,size)` pair** with the user image. No
> signature check, no size check beyond the HAL's later per-image bounds gate, no
> warning log. A custom GPSIMD kernel **is** a `{iram,dram}` bin pair registered
> before `nrt_init`. This is the single most important reimplementation seam.

> **QUIRK — DRAM before IRAM, broadcast to all 8.** `aws_hal_q7_ucode_eng_init`
> writes the data image (DRAM) *before* the code image (IRAM), and broadcasts the
> **same** image to all 8 Q7 cores (broadcast-by-replication). There is no
> per-core image differentiation at install — that arrives at execute time, by
> PRID (§3.4).

### 2.2 Load-time model stage

`nrt_load @0xa9fe0` calls `nrt_load_util` (edge confirmed:
`nrt_load → _Z13nrt_load_utilPKvmiPP9nrt_modelii`), which drives
`kmgr_load_nn_nc @0xde280`:

```c
nrt_load(neff, size, &model) -> nrt_load_util -> kmgr_load_nn_nc {
  neff_get_header_from_buffer + neff_copy_name;   /* header + model name */
  neff_parse;                                     /* a NEFF is a GZIPPED TAR -> libarchive
                                                     (tar + gzip filter + open_memory);
                                                     version gate -> NRT_UNSUPPORTED_NEFF_VERSION (10) */
  dlr_kelf_load -> kelf_load;                      /* parse the KELF: kbin sections, mem_refs, patch table */
  dlr_kelf_stage -> ... -> kbl_model_add @0x3058e0;/* *** the per-NC tdrv DEVICE INSTALL *** */
}
```

`kbl_model_add @0x3058e0` is where the NEFF becomes a live device program. Its
callee set is confirmed:

```c
kbl_model_add -> dma_queue_bundle_instance_lut_init   /* per-engine DMA queue bundles */
              -> sequencer_setup_instr                /* program the per-engine 64-B instruction streams */
              -> ucode_stage_libs @0x310ea0           /* *** the Pool-Q7 custom-op staging (see §3.1) *** */
              -> add_model;                           /* insert into the per-core model_db */
/* on success: dlr_model_state_t  STARTING -> RUNNING */
```

**[HIGH·OBSERVED** — `kbl_model_add @0x3058e0` and its four named callee edges
(`dma_queue_bundle_instance_lut_init`, `sequencer_setup_instr`, `ucode_stage_libs
@0x310ea0`, `add_model`) confirmed via `callgraph.json`.]**

> **NOTE — the instruction image is synthesized, not copied.** The compiler's
> per-engine `.bin` is **not** the loaded image. `sequencer_setup_instr`
> *assembles* a larger device image around it by fixed-order concatenation —
> `[PREAMBLE][MODEL_SWITCH][MAIN][POSTAMBLE][FUNCTION*]` — where PREAMBLE/POSTAMBLE
> are runtime-synthesized (notify / barrier / DGE / ulib-load / reset-branch) and
> MAIN is the expanded `def.json` stream. The ucode-library load sequence is
> *inserted* into the POOL preamble — the bridge to the device-side Q7
> `kernel_info_table`. Detail belongs to the NEFF assembly-pipeline part
> (the `neff/` chapter is the canonical home). **[CARRIED]**

### 2.3 The metaneff host I/O binding

A compiled model is a **pair**: the NEFF (device program + device var table) and
the `metaneff` protobuf (the **host** I/O key-ring, serialized *alongside* the
NEFF, not inside the tar). The framework bridge `nrt_load()`s the NEFF, then walks
`metaneff.input_tensors/output_tensors` **in order** and binds each to an `nrt`
tensor-set slot. The contract:

```
metaneff MetaTensor index i  ==  NEFF var_id i  ==  nrt tensor-set ordinal i  ==  device mem_ref[i].
```

`MetaTensor.name` (`"input{i}"`/`"output{i}"`) is the runtime lookup key;
`MetaTensor.type {USER_INPUT, INPUT_STATE, INPUT_WEIGHT}` partitions which device
slots the host fills from user args vs from checkpoint state/weights;
`output_aliases_to` is the in-place/donated-buffer map. For a GPSIMD custom op the
device-side I/O for `var_id i` rides a DMA descriptor on the
`KBIN_DMA_RING_TYPE_CUSTOM_OP` (=16) queue. **[HIGH·CARRIED** — the metaneff I/O
ABI is owned by the NEFF chapter; the `idx-16` custom-op ring is
grounded in [execute-time-dispatch.md](execute-time-dispatch.md).]** metaneff
is the host key-ring; the NEFF var table is the device key-ring; `nrt_tensor`
ordinals are the shared index space.

---

## 3. The EXECUTE path — doorbell → 0x107A → POOL dispatch → completion

By execute time the entire custom-op descriptor program — `LOAD_POOL_ARGUMENT` +
DMA rings + `EXTENDED_INST 0xF0` in the POOL stream + the staged Q7 library image
— is already resident. **`nrt_execute` only RELEASES it.** This is the second
structural fact a reimplementer must internalize: execute is one semaphore write,
not a program upload.

### 3.1 The load-time preconditions (decided at stage, fired at execute)

Three host hinges, all decided at `kbl_model_add` time:

- `ucode_model_has_custom_ops(model) == (model->ucode_lib_set_info->num_libs > 1)`
  — a NEFF carries custom ops iff it bundles >1 ucode lib (base + ≥1 custom);
- `dma_is_custom_op_dma_v2(eng, queue)` walks `v2_queue_bundle_alloc_table` as 16-B
  tuples `{marker, rel_queue_idx, eng_lo, eng_hi}`; `marker==16` (CUSTOM_OP) +
  eng-in-range == a custom-op DMA queue;
- `ucode_stage_libs @0x310ea0` collects exactly **8** per-core M2S/S2M doorbell
  offset pairs (the 8 Q7 pool cores) and bakes **one** opcode→library resolution
  table per NeuronCore. `ucode_switch_libs @0x311100` then points the core's Q7
  engine at it with a **single `aws_hal_q7_swap_table` call** — **the image/table
  is BROADCAST**; all 8 Q7 cores resolve opcodes through it.

**[HIGH·OBSERVED** — `ucode_stage_libs @0x310ea0`, `ucode_switch_libs @0x311100 →
aws_hal_q7_swap_table` confirmed; the three predicates' internals
**CARRIED** from [execute-time-dispatch.md](execute-time-dispatch.md).]** What is
per-core is only **(i)** the 8 custom-op DMA queues and **(ii)** each core's
PRID-rebased private SoC-DRAM window.

### 3.2 The host-emitted per-op micro-program (0x107A)

The host lowering for a pool-custom op is `translate_one_pseudo_embedding_update_instr_v2
@0x2770f0`. It emits, at NEFF load/stage, this ordered POOL-stream micro-program:

```
[dma_config x2][dma_config_size][LOAD_POOL_ARGUMENT 0x107A][DRAIN 0x10A2][EVENT_SEMAPHORE update_mode=0x13]
```

The `LOAD_POOL_ARGUMENT` is built by **`add_load_pool_arguments @0x276780`**
(caller confirmed: `translate_one_pseudo_embedding_update_instr_v2`). I disassembled
its prologue and read the literal directly:

```asm
276780 <add_load_pool_arguments>:
  276788:  41 ba 7a 10 00 00     mov  $0x107a,%r10d   ; header word 0x107A
  ...
  2767ed:  b9 79 10 00 00        mov  $0x1079,%ecx    ; adjacent dma_config-class opcode
  2769b5:  ba 7c 10 00 00        mov  $0x107c,%edx    ; adjacent POOL opcode
```

The header word `0x107A` decodes as **opcode `0x7A` (`LOAD_POOL_ARGUMENT`) |
word_len `0x10`** (16 words × 4 B = a 64-byte instruction). It marshals, into the
POOL engine's argument block:

```c
embedding_table_base_addr (lo/hi);
sbuf_base_addr;
one_value_read_addr   = tdrv_arch_get_evt_accel_addr;  /* the scalar-arg source */
completion_write_addr = tdrv_arch_get_sem_inc_addr;    /* a NeuronCore semaphore-INCREMENT CSR */
```

These are **exactly** the bytes the device customop ABI consumes
(`customop_next_tensor` translates `sbuf_base`/`emb base`; `customop_next_int`
reads `one_value_read_addr`; the kernel posts on `completion_write_addr` at done).
Every TPB-sequencer instruction is a fixed 64-byte / `0x40`-stride slot.

**[HIGH·OBSERVED** — `add_load_pool_arguments @0x276780`, the `mov $0x107a,%r10d`
at `0x276788`, and the caller edge are first-hand; the device-side ABI
consumption is **CARRIED** from [execute-time-dispatch.md](execute-time-dispatch.md)
and [../abi/programming-model.md](../abi/programming-model.md).]**

> **NOTE — the `0x107A` neighbours are not noise.** The adjacent `0x1079` /
> `0x107C` / `0x1049` literals in the same function are the dma_config /
> DRAIN-class opcodes of the surrounding micro-program — a reimplementer reading
> the disasm should expect them, not treat them as stray constants.

### 3.3 The per-request firing — one semaphore write

`nrt_execute @0x91de0` is a thin profiling/trace bracket that tail-calls
`nrt_execute_repeat @0x91650 → kmgr_exec @0xdfd50`. The spine, every edge
confirmed:

```c
nrt_execute -> nrt_execute_repeat -> kmgr_exec {
  kmgr_exec_pre;                 /* clone tensor set to physical mem; build kmgr_exec_resources */
  /* SYNC branch:  */ kmgr_sync_exec -> tpb_xu_schedule_exec @0xe8040 -> ...
        -> dlr_add_to_hw_exec_queue -> kbl_compute_setup @0x306fb0   /* re-materialise template DMA rings */
        -> dlr_kickoff_exec -> kbl_infer_kickoff @0x307320 -> exec_kickoff_infer @0x2632e0
              ~~> ndl_nc_semaphore_increment @0xc3ba0      // *** THE DOORBELL: ONE semaphore write ***
  /* ASYNC branch: */ kmgr_async_exec_add_work + kmgr_async_exec_poll
}
/* the caller then blocks on a pooled completion eventfd (comp_efd) */
```

**[HIGH·OBSERVED** — `nrt_execute → nrt_execute_repeat`, `nrt_execute_repeat →
kmgr_exec`, `kmgr_exec → {kmgr_exec_pre, kmgr_sync_exec, kmgr_async_exec_add_work,
kmgr_async_exec_poll}`, `dlr_add_to_hw_exec_queue → kbl_compute_setup`,
`dlr_kickoff_exec → kbl_infer_kickoff`, `kbl_infer_kickoff → exec_kickoff_infer`,
`exec_kickoff_infer → ndl_nc_semaphore_increment` — all confirmed via
`callgraph.json` + `function_addresses.json`.]**

That single NeuronCore semaphore increment is the entire kickoff. The SP/SEQ
sequencer, gated on it, unblocks and begins fetching the per-engine 64-B
instruction streams — the POOL stream carrying `LOAD_POOL_ARGUMENT`, the relocated
custom-op DMA triggers (the idx-16 ring), and `EXTENDED_INST 0xF0` that hands the
op to the Q7 cluster.

### 3.4 The SPMD kernel run (device-side, CARRIED)

All 8 Q7 pool cores run the **same** library image, SPMD. The only per-core
differentiator is the hardware **PRID**: a static-init constructor caches `rsr.prid`
into a `.bss cpu_id` at image load; `get_cpu_id()` returns the rank in `{0..7}`,
`get_cpu_count()` is the constant `8`. A library declares `total_cpus ∈ {1,8}`,
validated at NEFF load (`cpu_id` MUST==0, `total_cpus` MUST be 1 or 8). Each core
rebases the same local DRAM address into its **own** SoC slice
(`index = 9 + 2*cpu_id`, aperture `[0x80000,0x90000)`) and selects its pair of the
8 staged DMA queues by PRID. The kernel partitions work by `get_cpu_id` over
`total_cpus` across the shared on-chip SBUF (the only cross-core memory) and ends
with `customop_cleanup()` issuing a `memw` write-barrier. **[HIGH·CARRIED** from
[spmd-teardown.md](spmd-teardown.md) and the device carves — these are device-side
Vision-Q7 facts, NOT host-resident; the canonical home for the byte detail is that
page + the ABI part [../abi/programming-model.md](../abi/programming-model.md).]**

> **QUIRK — there is no device Q7↔Q7 collective.** A symbol sweep across the
> device `.a` finds no barrier/semaphore/allreduce. The **JOIN** across the 8
> cores is the **host-emitted `DRAIN 0x10A2`** in the POOL stream — the SP/SEQ
> stalls until all 8 cores drain — followed by the single completion
> `EVENT_SEMAPHORE`. Any cross-core reduction is data-movement-driven (the compute-DMA
> path) or a single-core gather after the DRAIN, never a Q7 collective. **[CARRIED]**

### 3.5 The completion return

Completion comes back **two** ways and is reaped by the XU worker thread:

1. **HARD signal:** the Q7 kernel writes `completion_write_addr`, the
   `EVENT_SEMAPHORE` gate is satisfied, and the device pushes a **16-byte**
   `NEURON_ISA` notification record into the ENS Notification-Queue ring. The only
   PCIe interrupt is the kernel-driver MSI-X for *errors/throttle* — **steady-state
   completion is NOT a host interrupt** (libnrt has no `msi/msix/irq` symbol; it is
   poll-driven).
2. **SOFT channel:** `pool_stdio_queue_consume_all_entries` drains the Q7
   printf/diagnostics ring (256-B device entries → host-side `0xf0`-B records,
   ring-wrap aware).

The host poll: `exec_request_progress_one_step → notification_read_exec_queue →
aws_hal_notific_nq_read` + the 8×2 `pool_stdio` block-drain loop. The calling
thread does **not** spin on a register; it blocks on a pooled completion eventfd
(`comp_efd`) that the libnrt reap thread signals once the NQ completion is seen.
**[HIGH·CARRIED** — execute-completion detail in
[execute-time-dispatch.md](execute-time-dispatch.md); the device-side notification
PRODUCER (the TIE WUR `UR#0x14`/`#0x15` publish/error pair) and the three-path NQ
consume model are device/interconnect carves.]**

### 3.6 One op, 8 cores, end-to-end

```
[H]=host libnrt   [HW]=SP/SEQ + DMA   [Q7n]=pool core n (0..7)
 [H]  nrt_execute -> ... -> exec_kickoff_infer
        ~~> ndl_nc_semaphore_increment(start_sem)            ===DOORBELL===
 [H]  block on pooled comp_efd (read())
 [HW] SP/SEQ sees start_sem; fetches the POOL stream:
        - relocated DMA-trigger WRITEs fire the 8 idx-16 custom-op DMA queues (HBM<->SBUF),
          each core's pair selected by PRID
        - LOAD_POOL_ARGUMENT(0x107A): {sbuf_base, emb base, one_value_read, completion_write}
        - EXTENDED_INST(0xF0): SEQ hands the op to the Q7 cluster
 [Q70..7] all 8 run the SAME kernel SPMD: cpu=get_cpu_id(); n=8; slice SBUF by cpu/total_cpus;
          customop_next_tensor/_int -> compute -> customop_return_tensor -> customop_cleanup() -> memw
 [HW] DRAIN(0x10A2): SP/SEQ waits until ALL 8 cores drain      ===JOIN===
 [HW] EVENT_SEMAPHORE: completion_write_addr CSR += 1  ->  16-B NQ record pushed
 [H]  reap thread: notification_read_exec_queue + pool_stdio drain x(8x2) -> signal comp_efd
        -> tpb_xu_get_last_completed -> return to caller
```

For the full orientation trace of this exact path see
[../orientation/customop-end-to-end.md](../orientation/customop-end-to-end.md).

---

## 4. The multi-core / multi-model model

### 4.1 The process context tree

The host owns exactly **one** process-global object: `tdrv_ctx_0` (a `tdrv_ctx_t`,
~18.88 MB). Inside it: `mla[32]` (one per attached Neuron device), each `mla_t`
embedding `_tpbs[8]` (one `tpb_t` per on-die NeuronCore). The `tpb_t` **is** the
per-NeuronCore context — it carries the per-core **global** device-memory allocator
(`tpb_allocator`), the per-core **model database** (`model_db`, an `ht_t` under
`model_db_lock`), the **8 Q7 pool-core handles** (`@+0x9940`) + **5 NX core
handles** (`@+0x9918`), the notification ring, the `pool_stdio` block, the ext-isa
ulib staging cache, and the HW exec queue. Every alloc/model/tensor path begins at
`db_physical_core_get_mla_and_tpb(pcore)` — pure pointer arithmetic into the static
tree, **no locking**. **[HIGH·CARRIED** from
[multimodel-context-dmem.md](multimodel-context-dmem.md), which is the canonical
home for the field offsets; `STRUCT-08`.]**

### 4.2 Multi-model sharing of a NeuronCore

Multiple loaded models share a core purely by **co-residence** in that core's one
`model_db` (keyed by `H_MODEL`, minted by a process-global atomic counter). The
runtime does **not** time-slice or preempt. Concurrency is mediated by:

- `h_running_model` + per-core exec serialisation: only **one** model executes at a
  time per core (even though many are resident);
- per-model `ref_count`: a reading/executing thread bumps the model's ref so an
  in-flight unload cannot free it; `remove_model` removes from the lookup **first**
  (no new exec can acquire it), then busy-waits (`_mm_pause`) until every
  outstanding exec drops its ref;
- distinct per-model device memory (§4.3) so loads/unloads are independent.

**[HIGH·CARRIED]**

### 4.3 The two-tier dmem allocator

Device memory is one flat heap per `(NeuronCore, HBM channel)`, carved through the
driver portal (`ndl_memory_alloc`) over host-granted HBM (`TONGA_DRAM`) or pinned
host DRAM (`HOST_DRAM`). Two allocator tiers:

| Tier | Owner | Lifetime | Holds |
| --- | --- | --- | --- |
| **GLOBAL** | `tpb->tpb_allocator` | core | notification queues, scratchpad, the **shared** ext-isa ulib, DMA rings, the `pool_stdio` ring, and USER I/O tensors |
| **MODEL** | `model->dmem_allocator` | model | instruction streams, weights/constants, act tables, per-model DMA rings, `mr` pointer vars, per-model custom-op ulib copies |

The MODEL allocator is created per-LNC at stage time and **handed** to the model at
`kbl_model_add`; freeing it in one call (`dmem_allocator_destroy`) mass-releases
the model's entire device footprint — **the unload-independence guarantee**. Each
`dmem_t` (192 B) is booked under one of 23 `dma_mem_usage_type` categories in the
per-device ledger (`memory_usage_nc[8][23]`, `[nc][22]` = per-core total vs
`aws_hal_get_hbm_size()` cap) — the OOM forensic trail. **[HIGH·CARRIED** from
[multimodel-context-dmem.md](multimodel-context-dmem.md).]**

> **NOTE — SBUF is not in this heap.** The on-chip SBUF is a static NEFF
> **carveout** (`KBIN_SB_CARVEOUT_TYPE_EVTACCEL`), *validated* (not allocated) at
> model-add. A reimplementer must not try to `dmem_alloc` SBUF.

### 4.4 ulib_set sharing — the single ext-isa lib is staged once

`ucode_stage_libs @0x310ea0` policy on `num_libs`:

- `>9` → `"Exceeded max number of GPSIMD libs"` (cap = 9);
- `==1` → the single EXT-ISA library: under `ulib_staging_lock`, **reuse** the
  per-core cached `ulib_set_info_extisa_only` if present, else stage it **once**
  into the GLOBAL allocator and cache it — **shared by every model** on that core;
- `2..9` → multi-core custom-op sets: staged **per-model** from the model's own
  MODEL allocator (no sharing).

So the common single-library GPSIMD case stages into device memory once per
NeuronCore and is shared. **[HIGH·CARRIED]**

### 4.5 The LNC / fractional-core model

Processes never see physical TPBs; they see **logical** NeuronCores (LNC / vtpb /
vnc). A `virtual_core_t` groups 1-2 physical cores; `NEURON_RT_VIRTUAL_CORE_SIZE`
(default 2, power-of-two, `nc_per_device % size == 0`; **Sunda forces size 1**)
partitions a device's physical cores into LNCs; `owned_vcores[]` is the process's
slice.

> **NOTE — the k8s 70/30 fractional split.** A KAI-scheduler generate/upscale
> GPU-fractional split lands here as **separate processes**, each with its own
> disjoint `owned_vcores` slice, its own `tdrv_ctx_0`, its own per-core
> `tpb_allocator` and `model_db`. There is **no** single-process fraction and **no**
> cross-process model sharing inside libnrt; the driver/scheduler partitions
> physical cores + the HBM carveout below libnrt's visibility. **[HIGH·OBSERVED
> for the LNC mechanics · INFERRED for the cross-process arbitration]**

### 4.6 The teardown spine

Two granularities, both reversing the bring-up:

```c
/* PER-MODEL: nrt_unload @0xaa190 -> kmgr_unload_nn */
ref-counted (nn_ref_decrement until 0);  RUNNING -> STOPPING;  stop async worker; drain in-flight execs;
kmgr_unstage_kelf_model (free DMA rings, mem_ref tables, staged ucode, hw_exec_queue);
tpb_xu_model_unstage;  STOPPED;  release the vNC slot last.

/* PER-DEVICE: nrt_close @0x93c20 -> tdrv_destroy @0x269a70 */
per NeuronCore: halt exec queue -> destroy 5 NX cores -> ucode_ll_destroy xN
  -> destroy 8 Q7 cores (= 13 ucode_core_destroy total, ALWAYS all 8 Q7 regardless of any op's total_cpus)
  -> notification_destroy -> pool_stdio_block_destroy -> ucode_free_lib_set -> free model db / DMA rings
  -> async device-close -> free ctx.
ucode_teardown_module dlclose's libnrtucode_extisa.so.
```

**[HIGH·OBSERVED** for the edges (`tdrv_destroy @0x269a70 → {ucode_core_destroy,
ucode_ll_destroy, notification_destroy, pool_stdio_block_destroy}`, `nrt_close
@0x93c20`, `nrt_unload @0xaa190` — confirmed); the ordered loop is
**CARRIED** from [spmd-teardown.md](spmd-teardown.md).]**

> **NOTE — "13 core_destroy" is the runtime loop count, not the static edge count.**
> The static callgraph shows only **4** `tdrv_destroy → ucode_core_destroy` call
> sites (the NX and Q7 destroys are issued from loops, so they collapse to a small
> number of distinct call instructions). The **13** figure (5 NX + 8 Q7) is the
> dynamic iteration count — the cited page owns that derivation. Do not read the
> static edge count as the destroy count.

---

## 5. The lifecycle + error / recovery model

### 5.1 The four host state machines

| Machine | States |
| --- | --- |
| GLOBAL runtime (`nrt_init_state`) | `START(0) → INIT(1) → CLOSED(3)` (+ `CHILD(2)` post-fork) |
| PER-MODEL (`dlr_model_state_t`) | `INVALID(0) → STARTING(1) → RUNNING(2) → STOPPING(3) → STOPPED(4)` |
| PER-INFERENCE (`exec_state_t`) | `INIT(0) → WAIT_BARRIER_PROXY(1) → WAIT_CORE(2) → DONE(3)` |
| PER-STEP poller (`ERP_STEP`) | `DONE(0) / CONTINUE(1) / FATAL(2)` |

`nrt_init_state` is read at the top of every public entry: calls before `INIT`
return `NRT_UNINITIALIZED(13)`, after `CLOSE` return `NRT_CLOSED(14)`. **[HIGH·CARRIED**
from [lifecycle-error-model.md](lifecycle-error-model.md), the canonical home;
the `nrt_set_pool_eng_ucode` state guard (reject `0xd`/`0xe`) is grounded in
[nrtucode-bringup.md](nrtucode-bringup.md).]**

### 5.2 The nrt_status_t taxonomy

A single flat enum (28 values, three numeric bands) is the host's **only** return
channel; device faults surface to it through the NQ:

- **Band A — generic (0..15):** `SUCCESS 0`, `FAILURE 1`, `INVALID 2`,
  `INVALID_HANDLE 3`, `RESOURCE 4`, `TIMEOUT 5`, `HW_ERROR 6`, `QUEUE_FULL 7`,
  `LOAD_NOT_ENOUGH_NC 9`, `UNSUPPORTED_NEFF_VERSION 10`, `FAIL_HOST_MEM_ALLOC 11`,
  `UNINITIALIZED 13`, `CLOSED 14`, `QUEUE_EMPTY 15`.
- **Band B — execution result (101, 1002..1100):** `EXEC_UNIT_UNRECOVERABLE 101`,
  `EXEC_BAD_INPUT 1002`, `EXEC_COMPLETED_WITH_NUM_ERR 1003`, `_WITH_ERR 1004`,
  `EXEC_NC_BUSY 1005`, `EXEC_OOB 1006`, `COLL_PENDING 1100`.
- **Band C — hardware-fault (1200..1206):** `HW_ERR_COLLECTIVES 1200`,
  `HW_ERR_HBM_UE 1201`, `HW_ERR_NC_UE 1202`, `HW_ERR_DMA_ABORT 1203`,
  `SW_NQ_OVERFLOW 1204`, `HW_ERR_REPAIRABLE_HBM_UE 1205`, `NETWORK_PROXY_FAILURE 1206`.

When several NeuronCores fault in one inference, `nrt_get_status_priority` ranks
them (NONE/MEDIUM/HIGH/CRITICAL) and the **worst** status is returned. **[HIGH·CARRIED]**

### 5.3 The device-fault → status mapper

An on-core SEQ/GPSIMD fault packs a **16-byte** `NEURON_ISA` `TPB_ERROR` record
(`error_id 0x09 NONFATAL` / `0x0a FATAL` + subtype), latches it (TIE `UR#0x15`),
and raises an NQ interrupt. The host (`exec_request_process_errors`):
`notification_consume_errors` drains each 16-B record, decodes the
`SEQUENCER_FATAL/NONFATAL` subtype to text, then classifies —
`exec_check_hbm_uncorrectable` (→ `1201`/`1205`), `check_dma_queue_on_aborted_eng`
(engine state==3 → `1203`), OOB (→ `1006`), collectives/fabric (→ `1200`),
network-proxy (→ `1206`), NQ ring full (→ `1204`) — sets the Band-C/B status + the
latched `exec_fatal_status` + a `"(FATAL-RT-UNDEFINED-STATE)"` log line, dumps the
per-engine instruction pointers, and returns `ERP_STEP_FATAL`. **[HIGH·CARRIED**
from [lifecycle-error-model.md](lifecycle-error-model.md).]**

### 5.4 Timeout / hang detection

The budget is env `NEURON_RT_EXEC_TIMEOUT → gconf → dlr_model.exec_timeout`; the
host wall-clock wait on `comp_efd` is bounded by it. When a completion notification
never arrives in budget, the per-NC scan sets `NRT_TIMEOUT(5)` +
`EXEC_FATAL_STATUS_TIMEOUT` and logs `"(FATAL-RT-UNDEFINED-STATE) … execution
timeout … waiting for execution completion notification"`.

> **GOTCHA — a timeout means the DEVICE halted, and the host cannot see why.** A
> timeout is the host-side mirror of an on-core self-halt (the SEQ/GPSIMD
> `ErrorHandler`'s permanent self-loop that never posts completion). The host
> **infers** the spin from the missing notification + the expired timeout — it has
> no register-level view of the stuck core. The collectives variant
> (`EXEC_FATAL_STATUS_BARRIER → NRT_EXEC_HW_ERR_COLLECTIVES 1200`) logs
> `"Suspected hang in waiting for barrier"`. **[HIGH·CARRIED·INFERRED]**

### 5.5 The recovery posture = FAIL-STOP

There is **no** in-place retry and **no** host-driven core reset on a
hardware/data fault. `exec_request_process_errors` returns `ERP_STEP_FATAL`, the
wait unwinds, the FATAL status propagates out of `nrt_execute` unchanged, and the
model stays `DMSTATE_RUNNING` but **poisoned**. The host's only mechanical action
is log + report; recovery is **operator-directed** (spelled out in the strings):
HBM-UE → "reload the neuron driver or reboot your EC2 instance"; NC-mem-UE →
"terminate or stop/start this instance".

> **CORRECTION / reimplementer trap.** The per-arch `q7.release_run_stall` write
> (host CSR `0x3000`) is a **BRING-UP** primitive, **not** a fault-recovery
> primitive. Do not re-issue it to "un-stick" a hung core — the design intent is
> reload-the-model. The lone "soft" band is B `1003`/`1004`
> (`COMPLETED_WITH_(NUM_)ERR`): a recoverable FP/numerical fault lets the inference
> **complete** and merely flags the result — no halt, no reload. **[HIGH·OBSERVED·INFERRED]**

### 5.6 Resource cleanup on error — RAII-style unwinding

LOAD failure releases the vNC slot, then `kelf_free` / `model_free` /
`dmem_list_free` / `kbl_free_fmap_set` / `kmgr_unstage_kelf_model` /
`release_tmp_neff_cache`. EXEC failure frees the in/out feature-map sets,
`kmgr_exec_resources_free`, `exec_request_cleanup_state`, releases the pooled
eventfd, `nn_ref_decrement` — **the model is NOT freed on exec error** (it may be
re-run or explicitly unloaded). **[HIGH·CARRIED** from
[lifecycle-error-model.md](lifecycle-error-model.md).]**

---

## 6. The version axis — NRT_2.0.0 (sync) vs NRT_3.0.0 (async nrta_*)

The `145` real exports split across **two ELF symbol-version nodes**
(`readelf -V` / `objdump -T`):

| Node | Count | Surface |
| --- | --- | --- |
| **`NRT_2.0.0`** (parent) | 137 globals | the stable public C ABI: the entire sync + implicit-async surface (`nrt_*` + `nec_*`) |
| **`NRT_3.0.0`** (child of `NRT_2.0.0`) | **8** | the `nrta_*` async-SCHEDULE family |

The 8 `NRT_3.0.0` symbols, enumerated first-hand:
`nrta_cc_prepare @0x7c980`, `nrta_cc_schedule @0x7bd80`, `nrta_execute_schedule
@0x7bb00`, `nrta_get_sequence @0x7c450`, `nrta_is_completed @0x7c720`,
`nrta_tensor_copy @0x7d8c0`, `nrta_tensor_read @0x7e440`, `nrta_tensor_write
@0x7df10`. **[HIGH·OBSERVED.]**

The `NRT_2.0.0` execute engine is selected at `nrt_init` (`kmgr_exec_mode_t`:
`KMGR_EXEC_MODE_ASYNC=0` the implicit worker-queue path vs `_EXPLICIT_ASYNC=1`);
backpressure surfaces as `NRT_QUEUE_FULL(7)`, an empty poll as `NRT_QUEUE_EMPTY(15)`.
This is the path everything in §2-§5 describes.

`NRT_3.0.0`'s `nrta_*` family is backed by the Rust crate `neuron_rustime`
(`cbindgen → nrta_*` C ABI; `crossbeam-queue` / `serde_json` / `log`). It is a
**collectives** prepare → schedule → execute pipeline
(`nrta_cc_prepare → nrta_cc_schedule → nrta_execute_schedule`, with
`nrta_get_sequence` / `nrta_is_completed` for polling and the `nrta_tensor_*` data
plane) layered on the same async worker pool — distinct from the `NRT_2.0.0`
sync/implicit-async model. **[MED·OBSERVED for the node split · CARRIED/INFERRED
for the crate internals.]**

> **REIMPLEMENTATION GUIDANCE.** A GPSIMD reimplementer needs **only** the
> `NRT_2.0.0` path: `nrt_init` / `nrt_set_pool_eng_ucode` / `nrt_load` /
> `nrt_execute` / `nrt_unload` / `nrt_close` + the tensor/tensor-set APIs. The
> `nrta_*` `NRT_3.0.0` schedule family is a collectives-oriented async overlay and
> does **not** change the custom-op install / dispatch / completion model of §2-§3.

---

## 7. The end-to-end host model — one picture

```
[register] nrt_set_pool_eng_ucode(iram,dram)  @0xc1630 -> tdrv_set_pool_eng_ucode @0x2695f0
[init]     nrt_init @0x94e90
             -> ucode_init_module @0x225940: dlopen libnrtucode_extisa.so + 30 dlsym
             -> per NC: ucode_nx_core_create x5 + ucode_pooling_q7_core_create x8
             -> tpb_eng_init_hals_v2 @0x2687e0: stock Q7 ucode, then SILENT pool_eng_* OVERRIDE
                -> aws_hal_q7_ucode_eng_init @0x451080 -> write_padded @0x473eb0 (DRAM then IRAM) x8 -> BAR0 PIO
             -> release run-stall (Cayman/Mariana host CSR 0x3000; Sunda EVT_SEM, no hw_decode)
             -> pool_stdio_block_init @0x300c70 (Q7 printf ring)
[load]     nrt_load @0xa9fe0 -> nrt_load_util -> kmgr_load_nn_nc @0xde280
             -> neff_parse (gzip-tar) -> kelf_load -> kbl_model_add @0x3058e0
             -> sequencer_setup_instr assembles PREAMBLE/MAIN/POSTAMBLE/FUNCTION
             -> ucode_stage_libs @0x310ea0: 8 per-core DMA queues + ONE opcode->lib table
             -> ucode_switch_libs @0x311100: aws_hal_q7_swap_table (BROADCAST to 8 cores)
             -> metaneff binds MetaTensor[i] -> nrt tensor-set[i] -> mem_ref[i]
             -> DMSTATE_STARTING -> RUNNING
[execute]  nrt_execute @0x91de0 -> nrt_execute_repeat @0x91650 -> kmgr_exec @0xdfd50
             -> kmgr_sync_exec -> ... -> exec_kickoff_infer @0x2632e0
                ~~> ndl_nc_semaphore_increment @0xc3ba0   ===DOORBELL (one sem write)===
             [HW] SP/SEQ fetches POOL stream: custom-op DMA (idx-16),
                  add_load_pool_arguments @0x276780 emits LOAD_POOL_ARGUMENT(0x107A), EXTENDED_INST(0xF0)
             [Q7x8] SPMD by PRID; customop_* ABI; per-core memw
             [HW] DRAIN(0x10A2) JOIN -> EVENT_SEMAPHORE -> 16-B NQ record
             [H]  reap: notification_read_exec_queue + pool_stdio drain -> comp_efd
[error]    device fault -> 16-B TPB_ERROR -> NQ -> exec_request_process_errors
             -> Band-B/C nrt_status + FATAL-RT log -> ERP_STEP_FATAL; FAIL-STOP
[unload]   nrt_unload @0xaa190 -> ref-count to 0 -> kmgr_unstage_kelf_model -> release vNC
[close]    nrt_close @0x93c20 -> tdrv_destroy @0x269a70: 13 core_destroy + ll/stdio/lib_set free
             -> dlclose libnrtucode_extisa.so
```

---

## 8. Confidence / scope / what a reimplementer still needs

**HIGH · OBSERVED:** the binary identity
(`122,956,336` B, BuildID `8bb57aba…`, git `0b044f4ce`, `17,372` functions,
VMA==file-offset for `.text`/`.rodata`/`.data`, **no `.data` delta**); the two
version nodes (`NRT_2.0.0` parent, `NRT_3.0.0` child with exactly 8 `nrta_*`); the
per-arch HAL `__FILE__` strings (`aws_hal_q7_{sunda,cayman,mariana}.c`); and the
spine edges — `nrt_set_pool_eng_ucode → tdrv_set_pool_eng_ucode`,
`nrt_load → nrt_load_util → kmgr_load_nn_nc`,
`kbl_model_add → {sequencer_setup_instr, ucode_stage_libs, add_model}`,
`ucode_switch_libs → aws_hal_q7_swap_table`,
`add_load_pool_arguments @0x276780` emitting `mov $0x107a` (caller
`translate_one_pseudo_embedding_update_instr_v2`),
`nrt_execute → nrt_execute_repeat → kmgr_exec → … → exec_kickoff_infer →
ndl_nc_semaphore_increment`, and `tdrv_destroy → {ucode_core_destroy,
notification_destroy, pool_stdio_block_destroy}`.

**HIGH · CARRIED:** the four-layer stack and HAL dispatch (the detail pages own the
byte evidence — [libnrt-surface.md](libnrt-surface.md),
[aws-hal-q7.md](aws-hal-q7.md), [nrtucode-bringup.md](nrtucode-bringup.md),
[execute-time-dispatch.md](execute-time-dispatch.md),
[hw-decode-cam-programming.md](hw-decode-cam-programming.md)); the SPMD/teardown
model ([spmd-teardown.md](spmd-teardown.md)); the multi-model/dmem/LNC model
([multimodel-context-dmem.md](multimodel-context-dmem.md)); the lifecycle/error
taxonomy ([lifecycle-error-model.md](lifecycle-error-model.md)). The metaneff I/O
ABI and instruction-image assembly are owned by the NEFF chapter; the orientation
walk-through is [../orientation/customop-end-to-end.md](../orientation/customop-end-to-end.md).

**MED/INFERRED:** the device-side Q7 ISA, `kernel_info_table` funcVA dispatch, and
customop ABI consumption are device-resident (`ncore2gp`), not host-resident; the
cross-process LNC arbitration is below libnrt's visibility; the `nrta_*`
`neuron_rustime` crate internals are surface-level only; Maverick/NC-v5 has no
host-side per-arch HAL string (header-observed elsewhere, **INFERRED** here).

**DEFERRED (not this page):** the per-export API reference table (signatures +
addresses); the `nrta` Rust runtime internals; the device-side Q7 kernel funcVA
disasm. A reimplementer driving the device from the host needs **only** the
`NRT_2.0.0` spine of §7; the rest is context.
