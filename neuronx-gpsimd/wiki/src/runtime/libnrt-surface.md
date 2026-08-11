# The libnrt Surface Map (GPSIMD lens)

> **Scope — the whole host runtime on one screen.** This is the **anchor** page
> for Part 8. Every other Part-8 page deep-dives one slice of a single ELF:
> `libnrt.so.2.31.24.0` — the **entire AWS Neuron userspace runtime** in one
> ~122 MB host x86-64 shared object. It carries a flat C ABI (the `nrt_*` /
> `nrta_*` / `nec_*` exports), a C++ device-programming core
> (`tdrv`/`encd`/`enc`/`ucode`/`kmgr`/`kbin`), a vendored Annapurna/Cadence
> hardware-abstraction layer (`aws_hal_*`, 815 functions, per-arch
> Sunda/Cayman/Mariana dispatch), a statically-embedded driver portal (`ndl_*`),
> and a small Rust slice (the `neuron_rustime` crate → the `nrta_*` C ABI). This
> page is **the map**: the exported-API catalog by family, the RTTI C++
> class-hierarchy + vtable-slot→method method, the build provenance and version
> string, and — the reason Part 8 exists — the **end-to-end GPSIMD custom-op /
> Pool-Q7 ucode dispatch path** the rest of Part 8 deep-dives. The device-side
> half of that op (the Vision-Q7 "Pool" engine and its ABI) is **Part 7**; this
> page is the *host* that drives it.
>
> Tags per claim: `[CONF × PROV]` — `HIGH/MED/LOW` × `OBSERVED` (read from
> `nm`/`objdump`/`readelf`/`c++filt`/`strings` on the shipped ELF, or its DWARF
> `.debug_info`), `INFERRED` (an ABI/control-flow rule applied to an observed
> fact), `CARRIED` (taken from a cited sibling page, re-confirmed here). The page
> default is `[HIGH × OBSERVED]`; claims that depart from it carry an explicit tag.

> **NOTE — artifact & tooling.** Every claim is derived **solely from static
> analysis** of the shipped `aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64`
> package, file `/opt/aws/neuron/lib/libnrt.so.2.31.24.0`:
> **ELF64 x86-64**, **122 956 336 bytes**, **`BuildID[sha1]=8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`**,
> SONAME `libnrt.so.1`, *not stripped*, **with DWARF `.debug_info`**, **17 372
> functions**. Section layout (`readelf -SW`):
> ```
> [12] .text         VMA 0x0003dbc0  fileoff 0x03dbc0   (== VMA)
> [15] .rodata       VMA 0x007cf000  fileoff 0x7cf000   (== VMA)
> [26] .data.rel.ro  VMA 0x00bf2d80  fileoff 0xbf2d80   (== VMA)
> [30] .data         VMA 0x00c07e00  fileoff 0xc07e00   (== VMA)
> ```
> **`.data` VMA == file offset for *this* binary — there is no `.data` delta**
> (confirmed before any `.data`/`.data.rel.ro` work; do **not** assume the
> libtpu `0x400000` or the ncore2gp `0x200000` deltas). So RTTI parsing in
> `.data.rel.ro` and the global stores in `.bss`/`.data` are offset-clean.
> Anti-hang: addresses below come from the IDA `*_functions.json` sidecar; every
> disassembly is **address-bounded** (`objdump -d --start-address … --stop-address …`),
> never a whole-binary `objdump`/un-piped `nm`. Embedded build-path strings
> (`/opt/workspace/KaenaRuntime/…`, `/opt/brazil-pkg-cache/…/KaenaHal-2.31.0.0/…`)
> are `__FILE__`/assert literals the compiler baked in — they are
> **binary-derived and citeable**, not from any external tree.

---

## 0. One-screen orientation

`libnrt.so` is the whole Neuron runtime: a stable flat C ABI on top of a C++
device-programming core, a vendored register-level HAL, an IOCTL driver portal,
and a thin Rust async-schedule layer. The **GPSIMD spine** — the call chain Part
8 exists to document — is:

```
nrt_set_pool_eng_ucode(iram,dram)            ← register custom Q7 kernel (pre-init)
        │
nrt_init → … → tpb_eng_init_hals_v2          ← engine-HAL wiring; the OVERRIDE seam
        │        └ ucode_set_q7_ucode_bins → aws_hal_stpb_init → aws_hal_q7_ucode_eng_init_<arch>
        │                                                          (programs Pool/Q7 IRAM/DRAM over BAR0)
nrt_load(NEFF) → kbin builds mem_ref/dma_desc IR   ← >1 ucode lib ⇒ custom ops present
        │
nrt_execute → instruction chunks (add_load_pool_arguments, pool config/buffer load)
        │        └ DMA descriptor rings on the custom-op queue bundle (idx 16)
        │        └ Pool/Q7 runs; stdio via pool_stdio_* ring; completion via
        │          dma_desc_inc_semaphore + Notification Queue → nrt_tensor_check_output_completion
```

The custom GPSIMD kernel is a `{iram, dram}` binary pair the caller registers
*before* `nrt_init`; `tpb_eng_init_hals_v2` then **silently substitutes** it for
the stock Pool ucode. That substitution is the single most important
reimplementation seam in the host runtime, and it is fully traced in §5.

Cross-references: [nrtucode bring-up](nrtucode-bringup.md) (the ucode subsystem
that owns §5's Q7 core handle), [execute-time dispatch](execute-time-dispatch.md)
(the §5.4 execute half), [public API table](public-api-table.md) (the full §1
export list), [callgraph spine](callgraph-spine.md) (the reachability skeleton).
The device-side op ABI is in Part 7 — see
[the complete custom-op ABI](../abi/complete-customop-abi.md) and
[customop marshalling](../abi/customop-marshalling.md).

---

## 1. Exported API surface — the flat C ABI

`nm -D --defined-only` prints **151 lines**. Counted by symbol type and by ELF
symbol-version node (`objdump -T`), they decompose cleanly:

| bucket | count | what |
|---|---|---|
| Global `T` text exports (the real C ABI) | **145** | `121` `nrt_*` + `16` `nec_*` (NRT_2.0.0) + `8` `nrta_*` (NRT_3.0.0) |
| Leaked libstdc++ `std::string` `_M_*` helpers | **4** | `_M_dispose`/`_M_assign`/`_M_mutate`/`_M_replace` — ABI accident, local bind, NRT_2.0.0-tagged, **not API** |
| `*ABS*` version-node markers | **2** | the `NRT_2.0.0` and `NRT_3.0.0` `_d` records themselves |
| **total `nm -D` lines** | **151** | |

There are **two version nodes** in `.gnu.version_d`: `NRT_2.0.0` (the stable
public C ABI) and `NRT_3.0.0` (recorded as a child of `NRT_2.0.0`, holding only
the 8 `nrta_*` async-schedule symbols). `objdump -T` global `DF .text` rows:
**141 NRT_2.0.0** (= 137 public `nrt_`/`nec_` + the 4 leaked `_M_` locals) **+ 8
NRT_3.0.0**.

> **CORRECTION — the export tally.** An earlier reading framed this as "151 dynamic
> exports … 143 NRT_2.0.0 + 8 NRT_3.0.0." Grounded on the binary:
> `objdump -T` gives **141** versioned `NRT_2.0.0` `DF .text` rows (not 143), of
> which **4 are the leaked `_M_` libstdc++ helpers**, leaving **137** genuine
> public C exports under NRT_2.0.0. The clean public surface is **145 global
> `T` functions** (`121 nrt_ + 16 nec_ + 8 nrta_`); "151" only holds if you also
> count the 4 leaks and the 2 ABS version markers. Verify:
> `nm -D --defined-only libnrt.so | rg -c ' T '` → `145`;
> `objdump -T libnrt.so | rg 'DF .text' | rg -c 'NRT_3.0.0'` → `8`.

### 1.1 Families (representative, by prefix)

Addresses are file VAs (== file offset in `.text`).

| family | representative exports (addr) | role |
|---|---|---|
| **Lifecycle / version** | `nrt_init` @`0x94e90`, `nrt_close` @`0x93c20`, `nrt_get_version` @`0x940b0` | bring-up (idempotent; negotiates driver+runtime+collectives compat ranges, then brings up devices); version fills ≥160 B, +git hash if ≥200 B |
| **NC topology** | `nrt_get_instance_info`, `nrt_get_total_nc_count`, `nrt_get_visible_nc_count`, `nrt_get_total_vnc_count`, `nrt_host_device_id_get` | `nc` = NeuronCore; `vnc` = virtual NeuronCore under LNC partitioning |
| **NEFF model** | `nrt_load`, `nrt_unload`, `nrt_load_collectives`, `nrt_get_model_info`, `nrt_get_model_tensor_info` / `_free_model_tensor_info` | model lifecycle + introspection |
| **Tensors / memory** | `nrt_tensor_allocate` / `_allocate_empty` / `_allocate_slice` / `_free`, `nrt_tensor_read` / `_write` / `_copy` / `_memset`, `nrt_tensor_check_output_completion`, `nrt_get_hbm_mmap_va` @`0x949f0`, `nrt_get_dmabuf_fd` @`0xc0500` | host↔device buffers; tensor-sets; EFA/peer DMA-BUF |
| **Execute** | `nrt_execute`, `nrt_execute_repeat`, `nrt_async_drain_queued_execs`, `nrt_register_async_exec_callback` | sync (XU) vs implicit-async engine, selected at `nrt_init` |
| **★ GPSIMD / Pool ucode** | `nrt_set_pool_eng_ucode(const nrt_ucode_info*)` @`0xc1630` | **the one task-critical export** — registers a custom Q7 Pool kernel (§5) |
| **Collectives + P2P** | `nrt_all_gather`, `nrt_build_global_comm`, `nrt_barrier`, `nrt_cc_create_stream`, `nrt_get_libnccl_net`; async-sendrecv thunks (`0x7ed80`–`0x7ee20`); 16 low-level `nec_*` exports (libnccom calls back into these) | comm bring-up + topology bridge |
| **Profiling / trace** | `nrt_profile_*`, `nrt_trace_*`, `nrt_inspect_*`, `nrt_sys_trace_*`, `nrt_debug_client_*`, `nrt_get_vnc_memory_stats` @`0xb90a0` | serialized format = `ntff::*` protobuf (§4.3) |
| **NRT_3.0.0 async schedule** | `nrta_cc_prepare` @`0x7c980`, `nrta_cc_schedule` @`0x7bd80`, `nrta_execute_schedule` @`0x7bb00`, `nrta_get_sequence` @`0x7c450`, `nrta_is_completed` @`0x7c720`, `nrta_tensor_copy` @`0x7d8c0` / `_read` @`0x7e440` / `_write` @`0x7df10` | the Rust-backed CC prepare→schedule→execute pipeline (§3.2) |

---

## 2. Major subsystems — the symbol-prefix histogram

`nm libnrt.so | rg ' [tT] '` then per word-boundary prefix:

| prefix | funcs | role (source root from embedded `__FILE__` paths) |
|---|---|---|
| `aws_hal` | **815** | vendored Annapurna/Cadence HAL (`KaenaHal-2.31.0.0`); per-arch register/offset truth — **biggest subsystem**, §3 |
| `encd` | 289 | on-device firmware-payload encode/decode + collectives queue/resource pools (per-arch sunda/cayman/mariana maps) |
| `tdrv` | 260 | TPB driver: device bring-up, BAR mapping, **engine-HAL init**, DMA rings, notifications, ucode dispatch |
| `nrt` (`nrt_`) | 243\* | public C ABI surface (`nrt/`) |
| `ndl` | 122 | Neuron Driver Layer: raw IOCTL/mmap (statically-embedded `libndl`) |
| `enc` (`enc_`) | 100\* | collectives-comm driver (global-comm, replica groups, mesh/hier algos, proxy-task workers); polymorphic `enc_*` classes (§4.1) |
| `dma` | 53 | DMA descriptor/ring builders |
| `ucode` | **37** | microcode subsystem: per-engine sequencer ucode + Vision-Q7 Pool/CC cores + external-ISA libs — **primary GPSIMD subsystem** (§5) |
| `nds` | 36 | `neuron_ds` cross-process shared-memory device-state store |
| `kmgr` | 29 | kernel/exec manager: NEFF lifecycle, per-XU worker pool, async-exec deque |
| `nec` | 27 | Neuron Experimental Comms bridge to `libnccom.so.2` |
| `vtpb` | 26 | virtual-TPB / LNC virtual-core indirection |
| `dmem` | 23 | device-HBM allocator (`dma_memory.c`) |
| `tpb` | 20 | TPB engine config (`tpb_eng_init_hals_v2` lives here) |
| `notification` | 18 | mmap'd Notification-Queue completion rings |
| `nlog` | 14 | logging (`nlog_write`) |
| `kbin` | 10 | NEFF binary patcher / IP-space relocation; `mem_ref`/`dma_desc` IR classes (§4.2) |

> **NOTE — prefix-count framing.** The `nrt`/`enc` rows are marked `*`: a naïve
> `rg ' nrt'` over the full symtab returns ~264 because it also catches
> `nrta_*`, `nrtucode_*`, and `nrt_async_sendrecv_*`; a naïve `rg ' enc'`
> returns ~389 because it swallows `encd`. The word-boundary public-ABI figures
> (`nrt_` = 243, `enc_` = 100) are the meaningful ones and are what the
> subsystem ranking (§6) uses. `[MED × OBSERVED]`

**Per-arch dispatch.** The HAL is a 3-arch function table — Sunda (NC-v2),
Cayman (NC-v3), Mariana (NC-v4). `nm | rg -c` counts hundreds of arch-suffixed
variants each (`*_sunda` ≈ 198, `*_cayman` ≈ 200, `*_mariana` ≈ 223). A generic
dispatcher (e.g. `aws_hal_get_q7_params` @`0x44bfb0`) asserts the arch type via
`al_hal_tpb_get_arch_type()` (`0`=INVALID, `2`=Sunda, `3`=Cayman, `4`=Mariana)
and calls through a populated pointer in a `kaena_khal.khal_arch.*` dispatch
struct; each pointer resolves to one arch-suffixed implementation.

> **WALL — Maverick (NC-v5).** The byte-grounded arches here are Sunda/Cayman/
> Mariana only. A v5 "Maverick" family (`arch_id` 36) is header-OBSERVED
> elsewhere but **not present** as a dispatch suffix in *this* `libnrt.so`; any
> v5 claim is `INFERRED`. `[LOW × INFERRED]`

---

## 3. Build provenance & version

### 3.1 Version string and git hash

`strings libnrt.so | rg 'libnrt version|0b044f4ce'`:

```
libnrt version %s
0b044f4ce917b633a70eb3d0bc460f34ac3da620        ← GIT_HASH_STR
```

`nrt_get_version` @`0x940b0` formats `"libnrt version %s"` with **`2.31.24.0`**
(also the SONAME-package version) and appends the **git hash `0b044f4ce…`** when
the caller's buffer is large enough (≥200 B). The version is therefore both the
package name (`2.31.24.0-0b044f4ce`) and the embedded format string.

### 3.2 The three build worlds

The single ELF is fused from three source trees, all visible as `__FILE__`
literals:

| world | provenance string (recovered `.rodata`) | what it contributes |
|---|---|---|
| **KaenaRuntime** (C/C++ core) | `/opt/workspace/KaenaRuntime/nrt/nrt_async.cpp`, `…/tdrv/init.c`, … | the `nrt_*`/`tdrv`/`encd`/`enc`/`ucode`/`kmgr`/`kbin`/`dmem` core |
| **KaenaHal-2.31.0.0** (C HAL) | `/opt/brazil-pkg-cache/packages/KaenaHal/KaenaHal-2.31.0.0/AL2_x86_64/generic-flavor/src/src/common/arch/al_hal_tpb_arch.c`, `…/cayman/sdma/aws_hal_cayman_sdma_m2m.c`, … | the 815-function `aws_hal_*` register HAL and its 0/2/3/4 arch dispatch (`al_hal_tpb_arch.c`) |
| **`neuron_rustime`** (Rust crate, cbindgen → `nrta_*`) | `/opt/workspace/KaenaRuntime/build/private/.cargo/registry/…/{serde_json-1.0.145,crossbeam-queue-0.3.12,log-0.4.…}/…` | the `NRT_3.0.0` `nrta_*` async-schedule + the `neuron_rustime::sys_trace` capture layer |

The Rust slice is small but real: the `.rodata` carries the crate's panic
strings, `neuron_rustime` and `neuron_rustime::sys_trace::api`/`::capture`
literals, the `nrta_seq_id` field, and — notably for GPSIMD — the trace event
**`exec_consume_gpsimd_stdio` / `ExecConsumeGpsimdStdio`**: the Rust trace layer
names the host draining the Q7 Pool engine's stdio ring (§5.4). The `nrta_*`
async path is a CC (collectives) `cc_prepare → cc_schedule → execute_schedule`
pipeline distinct from the NRT_2.0.0 sync/implicit-async execute engine.

---

## 4. C++ class hierarchy (reconstructed from RTTI)

**Counts** (`nm libnrt.so | rg -c`): **266** `_ZTI` / **225** `_ZTV` / **245**
`_ZTS`. Inheritance is read from `__si_class_type_info`
records in `.data.rel.ro` (each record = `[vptr, _ZTS-name-ptr, base-_ZTI-ptr]`),
resolving the base pointer against the `_ZTI` addr→name map. Vtable slot→method
is read from the qwords at **`vptr = _ZTV symbol + 0x10`** (past the
offset-to-top and typeinfo-header words) and resolving each to a `.text` symbol.

**Worked example — `mem_ref_sp` (`_ZTV` @`0xbf8c88`).**
`objdump -s -j .data.rel.ro` over the record:

```
0xbf8c88 + 0x00  offset-to-top = 0x0
0xbf8c88 + 0x08  typeinfo ptr  = 0xbf8af8   (_ZTI for mem_ref_sp)
0xbf8c88 + 0x10  slot[0]       = 0x4c4710 → mem_ref_sp::dump(kbin_mem_ref&)
0xbf8c88 + 0x18  slot[1]       = 0x4c39d0 → mem_ref_sp::~mem_ref_sp()   (D1)
0xbf8c88 + 0x20  slot[2]       = 0x4c3b60 → mem_ref_sp::~mem_ref_sp()   (D0, deleting)
```

This is the canonical 3-slot `mem_ref` interface: `[0]dump [1]dtor [2]deleting-dtor`.
Of the 184 named non-anon vtables, ~110 are third-party
(`google::protobuf`, `absl::lts_20230802`, `simdjson`); the **73 Neuron-domain
polymorphic classes** fall into four families.

### 4.1 `enc_*` collectives — op + proxy-task model

```
enc_ins  (abstract base, no own vtable symbol)
  ├─ enc_op_list   vt@0xbf6998  [0]post_instr [1]update_param_for_signature
  │                             [2]update_param_for_signature_src_tgt_pairs
  │                             [3]validate_and_merge_ccops [4,5]dtor
  └─ enc_fnc       vt@0xbf69d8  (same 4-slot interface, fnc specialization)
enc_proxy_task   vt@0xbf6980  (worker-thread step task base)
  ├─ enc_network_proxy_task  vt@0xbf6b18  [0]step()
  └─ enc_barrier_proxy_task  vt@0xbf6b30  [0]step()
alg_mesh_initializer  vt@0xbf6958
  ├─ alg_mesh_initializer_pd      vt@0xbf6a18  (point-to-point/direct mesh)
  └─ alg_mesh_initializer_switch  vt@0xbf6b98  (switch-topology mesh)
```

### 4.2 ★ `kbin` device-programming classes — GPSIMD-relevant

These are the in-memory IR `kbin` builds from a NEFF and lowers into device DMA
rings. `mem_ref_sp` (on-chip SRAM ref) and `dma_desc_inc_semaphore`
(semaphore-increment descriptor) are *exactly* what a custom-op kernel's
descriptors look like — the most important non-protobuf class families for
GPSIMD reimplementation.

```
mem_ref  vt@0xbf8c60  — abstract NEFF memory-reference (relocation target). 3-slot: [0]dump [1,2]dtor
  ├─ mem_ref_sp              vt@0xbf8c88  (State-Buffer / on-chip SRAM ref)
  ├─ mem_ref_io              vt@0xbf8cb0  (model I/O tensor ref)
  ├─ mem_ref_buffer          vt@0xbf8cd8  (DRAM/HBM buffer ref)
  ├─ mem_ref_tmp_buf         vt@0xbf8d00  (scratch)
  ├─ mem_ref_virtual_tmp_buf vt@0xbf8d28  (deferred/virtual scratch)
  ├─ mem_ref_pointer         vt@0xbf8d50  (indirect pointer ref)
  ├─ mem_ref_remote_variable vt@0xbf8dc8  (cross-core/remote variable — collectives/CC)
  └─ mem_ref_list            vt@0xbf8d78
        └─ mem_ref_ptr_table vt@0xbf8da0  (inherits mem_ref_list::dump in slot0)

dma_desc  (base, no emitted vtable symbol — pure-virtual base elided). 4-slot: [0]dump [1,2]dtor [3]mark_block_end_desc
  ├─ dma_desc_data           vt@0xbf8bd0  (payload-copy descriptor)
  ├─ dma_desc_event          vt@0xbf8c00  (event/trigger descriptor)
  └─ dma_desc_inc_semaphore  vt@0xbf8c30  (semaphore-increment descriptor)
```

> **GOTCHA — `dma_desc` has no `_ZTV` of its own.** The base is pure-virtual and
> the compiler elided its vtable symbol; the 4-slot layout is reconstructed from
> the three concrete subclasses, which agree. `[HIGH × INFERRED]`

### 4.3 `ntff::` profiling trace format — protobuf-generated `[MED × OBSERVED]`

50+ classes, all deriving from `google::protobuf::Message` via
`__si_class_type_info` (e.g. `ntff::ntrace_data_file`, `ntff::ntrace_event`,
`ntff::engine_instruction_info`, `ntff::collectives_*`, `ntff::nc_memory_usage`).
"ntff" = **Neuron Trace File Format** — the on-disk profiler schema serialized by
`nrt_profile_*` / `nrt_sys_trace_*`. Third-party vtables (simdjson for NEFF JSON,
~70 protobuf, absl) are listed for completeness only (LOW GPSIMD relevance).

---

## 5. ★ The GPSIMD custom-op → device dispatch path

On Trainium/Inferentia the GPSIMD engine is the **"Pool" engine**, a Cadence
Vision-Q7 NX DSP. A custom op is a user kernel compiled to Q7 IRAM/DRAM ucode
that replaces or augments the stock Pool ucode. There are two halves: **(A)**
ucode *injection* at init, **(B)** custom-op *DMA* at execute.

### 5.1 The injection seam — `nrt_set_pool_eng_ucode` → 4 globals

The export stores the caller's `nrt_ucode_info` into four process globals. From
`objdump -d` of `tdrv_set_pool_eng_ucode` @`0x2695f0`:

```asm
2695f0:  mov  (%rdi),%rax              ; iram.bin   (nrt_ucode_info+0x00)
2695f3:  mov  %rax,0xa2da36(%rip)      ; → pool_eng_iram_bin      @0xc97030
2695fa:  mov  0x8(%rdi),%rax           ; iram.size  (+0x08)
2695fe:  mov  %rax,0xa2da23(%rip)      ; → pool_eng_iram_bin_size @0xc97028
269605:  mov  0x10(%rdi),%rax          ; dram.bin   (+0x10)
269609:  mov  %rax,0xa2da10(%rip)      ; → pool_eng_dram_bin      @0xc97020
269610:  mov  0x18(%rdi),%rax          ; dram.size  (+0x18)
269614:  mov  %rax,0xa2d9fd(%rip)      ; → pool_eng_dram_bin_size @0xc97018
26961b:  ret
```

This *also* recovers the `nrt_ucode_info` layout (DWARF-confirmed, **32 B**):

```c
struct nrt_ucode_img  { void  *bin;  size_t size; };           // 16 B
struct nrt_ucode_info { nrt_ucode_img iram;  /* +0  */         // 32 B
                        nrt_ucode_img dram;  /* +16 */ };
```

The four globals are zero-init (`.bss`, `nm` type `b`) at `0xc97018`–`0xc97030`.
The wrapper `nrt_set_pool_eng_ucode` @`0xc1630` guards `nrt_init_state` (must be
**pre-init**; it warns on `NRT_CLOSED` / errors on a bad state) and then calls
`tdrv_set_pool_eng_ucode(ui)`.

### 5.2 Stock Q7 Pool ucode selection (per arch)

`ucode_set_q7_ucode_bins(aws_hal_stpb_ucode_one_eng* bin, al_hal_q7_owner_t owner)`
@`0x2261e0` selects the stock image by arch (strings confirmed in `.rodata`):

| arch | owner `AL_HAL_POOLING_Q7` (GPSIMD) | owner `AL_HAL_COMPUTE_CLUSTER_Q7` (CC) |
|---|---|---|
| 2 Sunda | `NRTUCODE_CORE_SUNDA_Q7_POOL` | — |
| 3 Cayman | `NRTUCODE_CORE_CAYMAN_Q7_POOL` | `…v3_q7_xt_cc_*` |
| 4 Mariana | `NRTUCODE_CORE_MARIANA_PLUS_Q7_POOL` (HW rev B; flavor `…DYNAMIC_KERNEL_LOAD`) | `…v4_plus_q7_xt_cc_*` |

`AL_HAL_POOLING_Q7` is the GPSIMD/custom-op engine; `AL_HAL_COMPUTE_CLUSTER_Q7`
is the collectives CC engine. Image kinds: `NRTUCODE_IMAGE_IRAM` / `_DRAM`.

### 5.3 The override — `tpb_eng_init_hals_v2`

`tpb_eng_init_hals_v2(pcore, tpb_init_config, program_tpb)` @`0x2687e0`
(`KaenaRuntime/tdrv/init.c`) is the consumer. It builds an `aws_hal_stpb`
5-engine config (PE / ACT / **POOL** / SP / DVE sequencer engines, each with an
instruction-fetch DMA ring and `hw_decode_*` table), then, in order:

```c
// (a) per-engine sequencer ucode for all 5 engines
for (e in {PE,ACT,POOL,SP,DVE}) ucode_set_seq_ucode_bins(&cfg[e]);

// (b) stock GPSIMD Pool ucode (§5.2)
ucode_set_q7_ucode_bins(&cfg[Q7_slot], AL_HAL_POOLING_Q7);

// (c) ★ THE OVERRIDE — silently swap in the registered custom kernel
if (pool_eng_iram_bin) cfg[Q7_slot].iram = (img){pool_eng_iram_bin, pool_eng_iram_bin_size};
if (pool_eng_dram_bin) cfg[Q7_slot].dram = (img){pool_eng_dram_bin, pool_eng_dram_bin_size};

// (d) program the device — loads the (possibly custom) Q7 image into Pool IRAM/DRAM
memcpy(&tpb->sunda, &cfg, sizeof cfg);
aws_hal_stpb_init(&tpb->sunda, &tpb->notification.ens,
                  notifications_performance_mode, program_tpb);
//   └ aws_hal_stpb_init @0x458350 → aws_hal_q7_ucode_eng_init_<arch> writes Pool IRAM/DRAM over BAR0
```

**This is the reimplementation seam.** A custom GPSIMD kernel is a `{iram,dram}`
pair registered before `nrt_init`; `tpb_eng_init_hals_v2` substitutes it for the
stock Pool ucode with no further ceremony. The RIP-relative loads of the
`pool_eng_*` globals are observable at the override site and match the store
site in §5.1.

### 5.4 Custom-op at execute time

* **Detection.** `ucode_model_has_custom_ops(model_t*)` @`0x311150` ⟺
  `m->ulib_set_info->num_libs > 1` — a NEFF carries custom ops iff it bundles
  more than one ucode lib (base lib + ≥1 custom).
* **Argument marshalling.** `add_load_pool_arguments(…, NEURON_ISA_TPB_EVENTS*)`
  @`0x276780` emits a *load-pool-arguments* instruction — **opcode `0x107A`
  (4218)**, observable as `mov $0x107a,%r10d` at `0x276788` — into an
  instruction chunk via `add_ins`. It marshals the SBUF base, embedding-table
  offset, a one-value read address, and a completion write-back address into the
  Pool engine's argument block. This is the per-launch arg-passing primitive:
  the host writes args to SBUF, the kernel signals completion to
  `completion_write_addr`.
* **DMA routing.** `dma_is_custom_op_dma_v2(eng_id, queue_id)` @`0x22e120` scans
  the per-arch `v2_queue_bundle_alloc_table`: a DMA whose bundle entry has
  `queue_id == 16` (a dedicated bundle the Pool/Q7 engine claims for custom
  kernels) is a custom-op DMA. The `queue_id == 16` ↔ "custom-op queue" mapping
  is table-structure-OBSERVED, label `INFERRED`. `[HIGH × INFERRED]`
* **Stdio / completion.** `pool_stdio_queue_init` / `_block_init` /
  `_queue_consume_all_entries` / `_block_destroy` is the ring the host drains for
  Q7 `printf`/stdio + completion (the Rust `ExecConsumeGpsimdStdio` trace event
  from §3.2 names this drain).
* **Validators.** `dbg_is_valid_custom_op_header` @`0x285890`,
  `dbg_is_valid_custom_op_payload` @`0x2860a0`, and the per-instruction
  `dbg_is_valid_{load_pool_argument @0x2b1390, modify_pool_config @0x288390,
  pool_buffer_load @0x293fc0}` enforce the `custom_op_header.h` /
  `custom_op_payload.h` schemas; `ib_*_sunda_pooling_opcode_v2` are the Sunda
  Pool ISA opcode recognizers.

### 5.5 End-to-end summary `[HIGH × OBSERVED steps; INFERRED ordering]`

```
[register] nrt_set_pool_eng_ucode(iram,dram) ─► pool_eng_*_bin globals (§5.1)
[init]     nrt_init ─► … ─► tpb_eng_init_hals_v2 ─► (stock or OVERRIDDEN) Q7 ucode
           ─► aws_hal_stpb_init ─► aws_hal_q7_ucode_eng_init_<arch> programs Pool IRAM/DRAM over BAR0 (§5.3)
[load]     nrt_load(NEFF) ─► kbin builds mem_ref/dma_desc IR; >1 ulib ⇒ custom ops present (§5.4)
[execute]  nrt_execute ─► chunks incl. add_load_pool_arguments / pool config / buffer load
           ─► DMA descriptor rings (dma_desc_*) on the custom-op queue bundle (idx 16)
           ─► Pool/Q7 runs the kernel; results + stdio via pool_stdio_* ring;
              completion via dma_desc_inc_semaphore + Notification Queue ─► nrt_tensor_check_output_completion
```

The exact execute-time interleaving of `add_load_pool_arguments` vs DMA-ring
submission is assembled from the instruction-builder and queue-bundle evidence,
not a single traced call — hence `INFERRED` on ordering, `OBSERVED` on each step.

---

## 6. Subsystem ranking → the rest of Part 8

Ranked by GPSIMD relevance, then size/complexity. This is the scoping skeleton
the rest of Part 8 follows. `[MED × INFERRED ranking; OBSERVED sizes]`

| # | subsystem (syms) | GPSIMD rel. | deep-dive |
|---|---|---|---|
| 1 | `ucode`/`nrtucode` (37) | **CRITICAL** | [nrtucode bring-up](nrtucode-bringup.md) — `ucode_core_create`, `ucode_pooling_q7_core_create`, ext-ISA libs, IB bounds-check |
| 2 | `aws_hal` Pool-Q7 (~60) | **CRITICAL** | [aws_hal_q7_* HAL](aws-hal-q7.md) — `ucode_eng_init`, swap-table/DKL, `get_q7_params`, register offsets ×3 arch |
| 3 | `kbin` + `mem_ref`/`dma_desc` | **CRITICAL** | NEFF→device IR lowering (descriptor block builders) |
| 4 | custom-op marshalling | **CRITICAL** | [execute-time dispatch](execute-time-dispatch.md) — `add_load_pool_arguments`, `pool_stdio_*`, opcode recognizers |
| 5 | `tdrv` engine init (260) | HIGH | `tpb_eng_init_hals_v2`, BAR mapping, `tdrv_arch_*` offsets, hw-decode tables |
| 6 | `aws_hal` SP/sequencer | HIGH | `aws_hal_sp_*` + `aws_hal_stpb_init` (5 seq engines) — how Pool fits the seq-engine model |
| 7 | `dma`/`dmem` (53+23) | HIGH | dmem allocator, DMA ring producer, queue-bundle routing |
| 8 | `notification`/NQ (18) | HIGH | Notification-Queue completion + semaphores |
| 9 | `kmgr` execute (29) | HIGH | `nrt_execute` lower half, XU vs async workers |
| 10 | NEFF loader (`nrt/load`) | HIGH | `nrt_load` pipeline, simdjson parse, `ulib_set_info` (custom-op count) |
| 11+ | `ndl` (122) / `encd` (289) / `enc` (100) / `nec` (27) / `nrta` (10) / `ntff` / `nds` (36) / `vtpb` | MED–LOW | driver portal, firmware encode/decode, collectives, NCCL bridge, Rust runtime, profiling, shared store, LNC mapping |

---

## 7. Confidence & gaps

* **HIGH × OBSERVED:** binary identity (size/BuildID/SONAME/sections); the export
  tally (corrected, §1); the RTTI counts (266/225/245) and the
  `mem_ref`/`dma_desc`/`enc_*` vtable maps; the `nrt_set_pool_eng_ucode` →
  4-global → `tpb_eng_init_hals_v2` override chain (§5.1/§5.3, disassembled at the
  store *and* override sites); the `ucode_set_q7_ucode_bins` arch table; custom-op
  DMA detection; `add_load_pool_arguments` opcode `0x107A`; all build-provenance
  strings.
* **INFERRED (flagged inline):** the execute-time ordering of arg-load vs
  DMA-ring submission; the `queue_id == 16` = "custom-op/Pool queue" label; the
  elided `dma_desc` base vtable layout (taken from its 3 concrete subclasses).
* **WALL:** v5/Maverick (`arch_id` 36) is not a dispatch suffix in this ELF — v5
  is `INFERRED`.
* **Not covered here (deferred to Part 8 deep-dives or other Parts):** `ndl`
  IOCTL numbers, `libncfw` firmware images, the device-side Q7 ISA itself
  (native Xtensa disasm, Part 7), full `encd` per-arch resource maps.
