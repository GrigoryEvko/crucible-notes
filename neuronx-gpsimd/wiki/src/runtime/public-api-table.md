# The `nrt` Host API Surface Reference

This is the **per-export call table** for the host runtime `libnrt.so` — the document you read to call or implement *one specific* entry point. It is the per-call companion to the runtime narrative in [runtime synthesis](runtime-synthesis.md); where that page explains the load/execute *flow*, this page pins every exported C symbol to its DWARF-grounded signature, its ELF version node, the `nrt_status_t` band it can return, and the translation unit it is defined in.

Everything below is recovered by static analysis of the shipped binary only — symbol table (`.dynsym`), version-definition section (`.gnu.version_d`), DWARF `.debug_info` (signatures, parameter names, `DW_AT_decl_file:DW_AT_decl_line`), and bounded disassembly for the GPSIMD-relevant call spines. The GPSIMD corpus ships no standalone `libnrt.so`; the canonical copy analysed here lives in the sibling `neuronx-runtime` tree.

**Binary under analysis.** `libnrt.so.2.31.24.0`, x86-64 ELF, 122,956,336 bytes, `SONAME libnrt.so.1`, `BuildID[sha1] 8bb57aba0fb2e0035f1d88e9fc4fb3e7387c102e`, *with* `.debug_info` (not stripped), git `0b044f4ce`. Built from `KaenaRuntime` over `KaenaHal-2.31.0.0` (C++) plus a Rust `neuron_rustime` crate that backs the `nrta_*` family.

> **NOTE — addresses are file == VMA offsets.** `readelf -SW` shows `.text` at VMA `0x3dbc0` / file offset `0x3dbc0`, `.rodata` at `0x7cf000`/`0x7cf000`, and crucially `.data` at VMA `0xc07e00` == file offset `0xc07e00`. **This image has no `.data` VMA↔file-offset delta** (unlike `libtpu.so`'s `0x400000` or the ncore2gp config DLLs' `0x200000`). Every address in this table is therefore both a runtime VMA and a file offset; `objdump --start-address=` takes them verbatim.

Tags per row: confidence **HIGH/MED/LOW** × provenance **OBSERVED** (read from this binary's symtab/DWARF/disasm) / **INFERRED** (reasoned from neighbours where DWARF was thin) / **CARRIED** (from a sibling report, re-checked here). Escaped `\|` inside table cells is a literal pipe. Every per-export row is
`[HIGH/OBSERVED]`; the page default is `[HIGH/OBSERVED]` and claims that depart from it carry an explicit tag.

---

## 0. Orientation — what the surface is, and how big it is

The public ABI is a **flat C surface over a C++ core**. Everything a framework (PyTorch / TensorFlow) or a collectives plugin (`libnccom`) may legally touch is one of these exports; every `tdrv_*`/`kmgr_*`/`encd_*`/`enc_*`/`ucode_*`/`aws_hal_*` symbol is **private** — present in `.symtab` as a local/internal name but never offered as a versioned dynamic-symbol contract.

### Export count

```
nm -D libnrt.so | rg -c ' T (nrt_|nrta_|nec_)'         → 145   (callable public exports)
readelf --dyn-syms | rg 'FUNC' | rg '@@NRT_2.0.0' | rg -c '(nrt_|nrta_|nec_)' → 137
readelf --dyn-syms | rg 'FUNC' | rg '@@NRT_3.0.0' | rg -c '(nrt_|nrta_|nec_)' →   8
```

| Quantity | Count | How counted |
|---|---:|---|
| Callable public exports (`FUNC GLOBAL`, `nrt_`/`nrta_`/`nec_`) | **145** | `nm -D … \| rg -c ' T (nrt_\|nrta_\|nec_)'` |
| …of which `@@NRT_2.0.0` | 137 | `readelf --dyn-syms` filtered to `FUNC` + `@@NRT_2.0.0` |
| …of which `@@NRT_3.0.0` (the `nrta_*` family) | 8 | `readelf --dyn-syms` filtered to `FUNC` + `@@NRT_3.0.0` |
| Per-family split | `nrt_`=121, `nrta_`=8, `nec_`=16 | `nm -D \| rg -c ' T nrt_'` etc. |
| Version-node `OBJECT` markers (`NRT_2.0.0`, `NRT_3.0.0`) | 2 | `readelf --dyn-syms \| rg 'OBJECT.*NRT_'` — **not API** |
| `std::string _M_*` `LOCAL` ABI leaks | 4 | `_M_dispose@0xc6a60`, `_M_assign@0xc6a90`, `_M_mutate@0xc6bd0`, `_M_replace@0xc6dc0` — **not API** |

> **CORRECTION — use 145, not 151.** Earlier inventory work cites "151": that is the raw `nm -D --defined-only \| rg -c '(nrt_\|nrta_\|nec_\|NRT_)'` line count (= 151), which folds in the 2 `OBJECT` version-node markers and the 4 `LOCAL` `std::string` `_M_*` helpers. The canonical **callable** figure is **145**. Use 151 only when reconciling against the older line-count inventory.

There are **no** `nrt`/`nrta`/`nec` `OBJECT` (data) exports: the ABI is functions only. All state is opaque behind handles (`nrt_model_t*`, `nrt_tensor_t*`, `nrt_tensor_set_t*`, …); the layouts those handles wrap are covered by [the exec-state struct census](../appendix/struct-exec-state-census.md).

### Version nodes (`.gnu.version_d`)

```
Version definition section '.gnu.version_d' contains 3 entries:
  Index: 2  Cnt: 1  Name: NRT_2.0.0
  Index: 3  Cnt: 2  Name: NRT_3.0.0
            Parent 1: NRT_2.0.0
```

- **`NRT_2.0.0`** — the base node; 137 of the callable exports.
- **`NRT_3.0.0`** — a *child* of `NRT_2.0.0` (the version_d node carries `Parent 1: NRT_2.0.0`), holding exactly the 8 `nrta_*` async-schedule exports. A loader that resolves `NRT_3.0.0` transitively pulls in `NRT_2.0.0`. The split is a pure soname-versioning device to add the async-schedule family without perturbing the 2.0.0 surface; **there is no `NRT_1.x`**.

### Reading the table

Every section table uses the columns:

`addr` · `ver` (2.0.0 / 3.0.0) · `ret` · `signature` (DWARF param names) · `decl_file:line` · `tag`.

`decl_file:line` is the `DW_AT_decl_file:DW_AT_decl_line` of the **defining** subprogram DIE (the out-of-line body), with the build-root `/opt/workspace/KaenaRuntime/` prefix elided. These are the runtime's own source coordinates, recovered from DWARF, and they group the ABI exactly as the runtime does.

> **GOTCHA — pick the `.cpp` DIE, not the `.h` DIE.** Most exports have **two** subprogram DIEs in `.debug_info`: a *declaration* instance pointing at a header (`inc/nrt/nrt.h`, `inc/nrt/nrt_sys_trace.h`, …, often with a `low_pc` at an *inlined* call site), and the *out-of-line definition* in a `.cpp`/`.cc`. The definition is the one to cite; verify by matching the DIE's `low_pc`/`high_pc` against the export's `nm -D` address. For example `nrt_tensor_allocate` (export `0xbc320`) has an `nrt.h:319` DIE *and* the real `nrt_tensor.cpp:43` body — the table cites `nrt_tensor.cpp:43`.

---

## 1. Lifecycle / version / topology

The bring-up family. `nrt_init` is the mandatory first call; nothing else (except the pure getters that fail with `NRT_UNINITIALIZED`) is legal before it. The host init state machine is `START → INITED → CLOSED` (the global `nrt_init_state` lives at `0xc5d1a0`); calls in the wrong state return `NRT_UNINITIALIZED`(13) or `NRT_CLOSED`(14).

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x94e90` | 2.0.0 | `NRT_STATUS` | `nrt_init(nrt_framework_type_t framework, const char *fw_version, const char *fal_version)` | `nrt_init.cpp:491` |
| `0x93c20` | 2.0.0 | `void` | `nrt_close(void)` | `nrt_init.cpp:134` |
| `0x940b0` | 2.0.0 | `NRT_STATUS` | `nrt_get_version(nrt_version_t *ver, size_t size)` | `nrt_init.cpp:779` |
| `0x84210` | 2.0.0 | `NRT_STATUS` | `nrt_get_instance_info(nrt_instance_info_t *info, size_t instance_info_len)` | `nrt_config.cpp:1871` |
| `0x84200` | 2.0.0 | `NRT_STATUS` | `nrt_get_total_nc_count(uint32_t *nc_count)` | `nrt_config.cpp:1794` |
| `0x83e10` | 2.0.0 | `NRT_STATUS` | `nrt_get_total_vnc_count(uint32_t *nc_count)` | `nrt_config.cpp:1765` |
| `0x85c60` | 2.0.0 | `NRT_STATUS` | `nrt_get_visible_nc_count(uint32_t *nc_count)` | `nrt_config.cpp:1752` |
| `0x855b0` | 2.0.0 | `NRT_STATUS` | `nrt_get_visible_vnc_count(uint32_t *nc_count)` | `nrt_config.cpp:1704` |
| `0x944a0` | 2.0.0 | `NRT_STATUS` | `nrt_host_device_id_get(int neuron_dev, uint32_t *host_device_id)` | `nrt_init.cpp:839` |
| `0x94740` | 2.0.0 | `NRT_STATUS` | `nrt_host_device_id_rid_map_get(uint32_t *count, uint32_t *host_did_to_rid_map)` | `nrt_init.cpp:849` |

**`nrt_init`** — full runtime bring-up; idempotent guard on the init state. `framework` tags the caller and gates a few framework-specific defaults; `fw_version`/`fal_version` are the caller's framework + FAL (framework-abstraction-layer) version strings, logged and fed into the driver↔runtime↔collectives compat-id negotiation. On success transitions to `INITED` and brings up all visible NeuronCores. **Returns** `NRT_SUCCESS`; `NRT_FAILURE`/`NRT_RESOURCE`/`NRT_HW_ERROR` on device bring-up failure; `NRT_INVALID` on a bad framework tag.

```
nrt_framework_type_t [DWARF, byte-confirmed]:
  NRT_FRAMEWORK_TYPE_INVALID=0  NO_FW=1  TENSORFLOW=2  PYTORCH=3  MXNET=4  PRECHECK=5
```

**`nrt_close`** — tear down every device, worker pool, and mapping; transition to `CLOSED`. `void` return — it cannot fail at the ABI; internal failures are `nlog`-logged only. After this, all stateful exports return `NRT_CLOSED`(14). Iterates the per-core `vnc` bitmap (`uint64_t[4]`) to quiesce each core.

**`nrt_get_version`** — fills the caller's `nrt_version_t` (pass `sizeof`). This is a **version-tolerant truncating copy**: `size` is the caller-supplied buffer length; the runtime fills `rt_detail` only if `size` covers it and `git_hash` only if `size` covers the full struct.

> **CORRECTION — `nrt_version_t` is 224 bytes, not 232.** `DW_AT_byte_size` is `0xe0` = **224**. The DWARF member layout is `uint64_t rt_major@+0; rt_minor@+8; rt_patch@+16; rt_maintenance@+24; char rt_detail[128]@+32; char git_hash[64]@+160` — i.e. `32 + 128 + 64 = 224`. The earlier "232 B" figure is an arithmetic slip; reimplementers must size the buffer to **224**.

**`nrt_get_instance_info`** — fills the EC2/Neuron instance descriptor. `nrt_instance_info_t` is **32 bytes** (`0x20`): `uint32_t family@+0; uint32_t size@+4; char arch_name[16]@+8; char device_revision[8]@+24`. `arch_name` is the device-arch family string; `device_revision` the silicon rev. Pass `instance_info_len = sizeof(struct)` for the version-tolerant copy.

**Topology getters** — all take an `out` `uint32_t*` count and return `NRT_STATUS` (`NRT_UNINITIALIZED` if pre-init). "nc" = NeuronCore; "vnc" = virtual NeuronCore under LNC partitioning; "total" = whole instance; "visible" = the subset this process is permitted. **`nrt_host_device_id_get`/`_rid_map_get`** map a local Neuron device index → global host-device-id, and the host-device-id → RID (routing-id) map that the `nec_*`/collectives layer (§7) routes on.

---

## 2. NEFF model load / unload / introspect

A NEFF blob is parsed host-side, lowered to a KELF, staged, and installed on `vnc_count` virtual cores starting at base `vnc`; the opaque `nrt_model_t*` wraps the `dlr_model` handle, whose state walks `INVALID → STARTING → RUNNING`.

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0xa9fe0` | 2.0.0 | `NRT_STATUS` | `nrt_load(const void *neff_bytes, size_t size, int32_t vnc, int32_t vnc_count, nrt_model_t **model)` | `nrt_model.cpp:331` |
| `0xaa4c0` | 2.0.0 | `NRT_STATUS` | `nrt_load_collectives(const void *neff_bytes, size_t size, int32_t vnc, int32_t vnc_count, uint32_t ctx_device_id, uint32_t ctx_device_count, nrt_model_t **model)` | `nrt_model.cpp:88` |
| `0xaa190` | 2.0.0 | `NRT_STATUS` | `nrt_unload(nrt_model_t *model)` | `nrt_model.cpp:347` |
| `0xaadf0` | 2.0.0 | `NRT_STATUS` | `nrt_get_model_info(const nrt_model_t *model, nrt_model_info_t *info, size_t info_size_in, size_t *info_size_out)` | `nrt_model.cpp:399` |
| `0xaade0` | 2.0.0 | `NRT_STATUS` | `nrt_get_model_instance_count(nrt_model_t *model, uint32_t *instance_count)` | `nrt_model.cpp:393` |
| `0xaadd0` | 2.0.0 | `NRT_STATUS` | `nrt_get_model_nc_count(const nrt_model_t *model, uint32_t *vnc_count)` | `nrt_model.cpp:388` |
| `0xaadc0` | 2.0.0 | `NRT_STATUS` | `nrt_get_model_vnc_count(const nrt_model_t *model, uint32_t *vnc_count)` | `nrt_model.cpp:382` |
| `0xc0090` | 2.0.0 | `NRT_STATUS` | `nrt_get_model_tensor_info(nrt_model_t *model, nrt_tensor_info_array_t **tensor_info)` | `nrt_tensor.cpp:353` |
| `0xc0390` | 2.0.0 | `NRT_STATUS` | `nrt_free_model_tensor_info(nrt_tensor_info_array_t *tensor_info)` | `nrt_tensor.cpp:417` |

**`nrt_load`** — parse + admit + install a NEFF onto `vnc_count` cores at base `vnc`. `*model` receives the handle. **Status:** `NRT_SUCCESS`; `NRT_INVALID` (malformed NEFF); `NRT_UNSUPPORTED_NEFF_VERSION`(10) (version gate); `NRT_LOAD_NOT_ENOUGH_NC`(9) ("insufficient pncs"); `NRT_RESOURCE` (no device memory/queues); `NRT_FAIL_HOST_MEM_ALLOC`(11). `vnc=-1` conventionally means "any".

**`nrt_load_collectives`** — as `nrt_load`, plus the global-comm context (`ctx_device_id` = this rank's global device id, `ctx_device_count` = world size) so the model joins a collective replica group. Same status set; additionally surfaces `NRT_NETWORK_PROXY_FAILURE`(1206)-class load faults when the fabric context cannot be established.

**`nrt_unload`** — ref-counted teardown (inverse of load): unstage + uninstall, free device memory, drop the handle when the last reference goes. `NRT_INVALID_HANDLE`(3) on a bad handle; `NRT_FAILURE` if device teardown errors.

> **CORRECTION — `nrt_get_model_instance_count` *does* have a full DWARF prototype.** It is elsewhere described as a "thin `.text` thunk with no `debug_info`" with an UNVERIFIED return type. This binary's DWARF gives the complete definition at `nrt_model.cpp:393`: **`NRT_STATUS nrt_get_model_instance_count(nrt_model_t *model, uint32_t *instance_count)`** — a 2-argument `out`-param getter returning `NRT_STATUS`, **not** the single-argument `(nrt_model_t*)` guess. Reimplement it like the neighbouring `_nc_count`/`_vnc_count` getters.

**`nrt_get_model_info`** — version-tolerant: pass your buffer size in `info_size_in`; `*info_size_out` returns the size the runtime would have written (probe-then-fill). **`_get_model_nc_count`/`_vnc_count`** fill a core-count `out`-param (`_nc_` = physical, `_vnc_` = LNC-virtual).

**`nrt_get_model_tensor_info`** — allocates and returns the input/output tensor descriptor array (names, shapes, dtypes, sizes). **This is the host-side tensor-name contract**: the names here are exactly the keys passed to `nrt_add_tensor_to_tensor_set`, which in turn match the metaneff I/O names (see [the metaneff I/O ABI](../neff/metaneff-io-abi.md)). Free the array with **`nrt_free_model_tensor_info`** — note it lives in `nrt_tensor.cpp`, not `nrt_model.cpp`, because the tensor-info type is owned by the tensor module.

---

## 3. Tensors / tensor-sets / memory

A `nrt_tensor_t` is an opaque buffer handle (device, host, or virtual placement); a `nrt_tensor_set_t` is a NAME→tensor map that `nrt_execute` consumes as I/O binding.

```
nrt_tensor_placement_t [DWARF, byte-confirmed]:
  NRT_TENSOR_PLACEMENT_DEVICE=0  HOST=1  VIRTUAL=2
```

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0xbc320` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_allocate(nrt_tensor_placement_t tensor_placement, int vnc, size_t size, const char *name, nrt_tensor_t **tensor)` | `nrt_tensor.cpp:43` |
| `0xbc910` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_allocate_empty(const char *name, nrt_tensor_t **tensor)` | `nrt_tensor.cpp:103` |
| `0xbf980` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_allocate_slice(const nrt_tensor_t *tensor_source, size_t offset, size_t size, const char *name, nrt_tensor_t **tensor_slice)` | `nrt_tensor.cpp:325` |
| `0xbcd00` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_attach_buffer(nrt_tensor *tensor, void *buffer, size_t size)` | `nrt_tensor.cpp:117` |
| `0xbd0e0` | 2.0.0 | `void` | `nrt_tensor_free(nrt_tensor_t **tensor)` | `nrt_tensor.cpp:128` |
| `0xbd280` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_read(const nrt_tensor_t *tensor, void *buf, size_t offset, size_t size)` | `nrt_tensor.cpp:141` |
| `0xbd570` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_write(nrt_tensor_t *tensor, const void *buf, size_t offset, size_t size)` | `nrt_tensor.cpp:161` |
| `0xbd9e0` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_read_batch(const nrt_tensor_batch_t *batches, uint64_t num_batches, bool unsafe)` | `nrt_tensor.cpp:181` |
| `0xbde50` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_write_batch(const nrt_tensor_batch_t *batches, uint64_t num_batches, bool unsafe)` | `nrt_tensor.cpp:202` |
| `0xbe2c0` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_copy(const nrt_tensor_t *src, size_t src_offset, nrt_tensor_t *dst, size_t dst_offset, size_t size)` | `nrt_tensor.cpp:224` |
| `0xbe590` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_memset(nrt_tensor_t *tensor, uint64_t offset, int value, size_t size)` | `nrt_tensor.cpp:243` |
| `0xbfdb0` | 2.0.0 | `void *` | `nrt_tensor_get_va(const nrt_tensor_t *tensor)` | `nrt_tensor.cpp:344` |
| `0xbe9d0` | 2.0.0 | `size_t` | `nrt_tensor_get_size(const nrt_tensor_t *tensor)` | `nrt_tensor.cpp:260` |
| `0xc08e0` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_get_device_allocation_info(const nrt_tensor_t *tensor, nrt_tensor_device_allocation_info *alloc_info)` | `nrt_tensor.cpp:468` |
| `0xc0cc0` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_check_output_completion(const nrt_tensor_t *output_tensor, int64_t timeout, uint64_t expected_completion_count)` | `nrt_tensor.cpp:479` |
| `0xc12f0` | 2.0.0 | `NRT_STATUS` | `nrt_tensor_reset_output_completion(nrt_tensor_t *output_tensor)` | `nrt_tensor.cpp:562` |
| `0xbeae0` | 2.0.0 | `NRT_STATUS` | `nrt_allocate_tensor_set(nrt_tensor_set_t **result)` | `nrt_tensor.cpp:266` |
| `0xbeea0` | 2.0.0 | `void` | `nrt_destroy_tensor_set(nrt_tensor_set_t **tensor_set)` | `nrt_tensor.cpp:282` |
| `0xbf1b0` | 2.0.0 | `NRT_STATUS` | `nrt_add_tensor_to_tensor_set(nrt_tensor_set_t *tensor_set, const char *tensor_name, nrt_tensor_t *tensor)` | `nrt_tensor.cpp:294` |
| `0xbf580` | 2.0.0 | `NRT_STATUS` | `nrt_get_tensor_from_tensor_set(nrt_tensor_set_t *tensor_set, const char *tensor_name, nrt_tensor_t **tensor)` | `nrt_tensor.cpp:306` |
| `0xc04b0` | 2.0.0 | `NRT_STATUS` | `nrt_memcpy_to_device(void *dest, const void *src, size_t size)` | `nrt_tensor.cpp:430` |
| `0x949f0` | 2.0.0 | `NRT_STATUS` | `nrt_get_hbm_mmap_va(int device_id, int hbm_idx, void **addr, size_t *size)` | `nrt_init.cpp:859` |
| `0xc0500` | 2.0.0 | `NRT_STATUS` | `nrt_get_dmabuf_fd(uint64_t va, uint64_t size, int *fd)` | `nrt_tensor.cpp:443` |

> **QUIRK — two non-status returns.** `nrt_tensor_get_va` returns `void *` (NULL if device-only / not mapped) and `nrt_tensor_get_size` returns `size_t` (0 on error) — they hand back the value directly, **not** an `NRT_STATUS`. Two exports are `void` and cannot fail at the ABI: `nrt_tensor_free` (frees and NULLs `*tensor`) and `nrt_destroy_tensor_set`.

- **`nrt_tensor_allocate_empty`** allocates a zero-size placeholder handle; bind a caller-owned buffer later with **`nrt_tensor_attach_buffer`** (zero-copy host buffer). *Note* the attach param is the bare struct `nrt_tensor *`, not the `nrt_tensor_t *` typedef used elsewhere — same type.
- **`nrt_tensor_allocate_slice`** creates a sub-view `[offset, offset+size)` aliasing the source's storage (no copy).
- **`_read_batch`/`_write_batch`** do scatter/gather; `unsafe=true` skips bounds/validation for speed. `nrt_tensor_batch_t` (DWARF) = `{const nrt_tensor_t *tensor; const nrt_tensor_batch_op_t *ops; uint32_t num_ops}`.
- **`nrt_tensor_check_output_completion`** is the implicit-async completion contract (pairs with `nrt_execute`): wait up to `timeout` ns (`<0` = block) until the tensor's completion counter reaches `expected_completion_count`. `NRT_TIMEOUT`(5) on expiry; execution-result codes (`1002..1206`) if the producing inference faulted.
- **`nrt_add_tensor_to_tensor_set`** binds `tensor` under `tensor_name` — the name **must** match a model tensor-info name. This is how a model's I/O slots get wired before `nrt_execute`.
- **`nrt_get_hbm_mmap_va`** returns the mmap'd host VA + size of HBM bank `hbm_idx`; **`nrt_get_dmabuf_fd`** exports a device VA range as a Linux `dma-buf` fd for EFA / peer-DMA zero-copy sharing.

---

## 4. Execute

The inference-firing family. Sync vs implicit-async is selected at `nrt_init`: the sync path returns only after completion; the implicit-async path returns at submission and the caller polls via `nrt_tensor_check_output_completion` (§3). Status codes `101` and `1002..1206` are the execution-result band (§10).

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x91de0` | 2.0.0 | `NRT_STATUS` | `nrt_execute(nrt_model_t *model, const nrt_tensor_set_t *input, nrt_tensor_set_t *output)` | `nrt_exec.cpp:117` |
| `0x91650` | 2.0.0 | `NRT_STATUS` | `nrt_execute_repeat(nrt_model_t *model, const nrt_tensor_set_t *input, nrt_tensor_set_t *output, int repeat_count)` | `nrt_exec.cpp:83` |
| `0x92110` | 2.0.0 | `NRT_STATUS` | `nrt_async_drain_queued_execs(int32_t vnc_idx)` | `nrt_exec.cpp:162` |
| `0x94490` | 2.0.0 | `NRT_STATUS` | `nrt_register_async_exec_callback(NRT_ASYNC_EXEC_STATUS_CALLBACK callback, void *params)` | `nrt_init.cpp:834` |

**`nrt_execute`** — fire one inference: bind `input`/`output` tensor-sets to the model and run. **Returns** `NRT_SUCCESS` or an execution-result code: `NRT_EXEC_BAD_INPUT`(1002), `NRT_EXEC_NC_BUSY`(1005), `NRT_EXEC_OOB`(1006), `NRT_EXEC_COMPLETED_WITH_{NUM_,}ERR`(1003/1004), or a HW-fault code (`1200..1206`). Multi-core faults are ranked by `nrt_status_priority_t` — the **worst** code wins.

**`nrt_execute_repeat`** — fire the same binding `repeat_count` times (benchmark / warm-loop); same status set. **`nrt_async_drain_queued_execs`** — drain/flush all queued async executions on core `vnc_idx` (barrier before teardown/reuse).

**`nrt_register_async_exec_callback`** — register a completion callback for the implicit-async engine. Callback type (DWARF): `void (*)(void *user, uint32_t a, uint32_t b, uint64_t c, NRT_STATUS status)` — `user = params`, the `(a,b,c)` triple are exec-identity fields, `status` the per-exec result.

---

## 5. GPSIMD pool-engine ucode

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0xc1630` | 2.0.0 | `NRT_STATUS` | `nrt_set_pool_eng_ucode(const nrt_ucode_info *ucode_info)` | `nrt_ucode.cpp:10` |

This is the **one public hook** by which a GPSIMD custom kernel replaces the stock Vision-Q7 "Pool" engine microcode. The caller hands two `{pointer,length}` blobs — one for instruction RAM, one for data RAM — describing the Q7 Pool-core image to swap in.

```
struct nrt_ucode_info  [DWARF, byte_size 0x20 = 32]   { nrt_ucode_img iram@+0; nrt_ucode_img dram@+16; }
struct nrt_ucode_img   [DWARF, byte_size 0x10 = 16]   { uint8_t *bin@+0; size_t size@+8; }
```

### Call spine (disasm `@0xc1630`, `low_pc..high_pc = 0xc1630..0xc1708`)

```c
NRT_STATUS nrt_set_pool_eng_ucode(const nrt_ucode_info *ucode_info /* RDI */) {
    // 0xc1630: lea nrt_init_state (global @0xc5d1a0); branch on init-state value
    //          — validates START/INITED via the state machine; the human-readable
    //            name comes from nrt_state_get_string @0xb9060 (_Z20nrt_state_get_stringv)
    if (state_invalid) {
        nlog_write(...);                 // 0xc164f call _Z20nrt_state_get_stringv ; 0xc1682 call nlog_write @0x224d40
        return NRT_UNINITIALIZED;        // (13) — pre-init
    }
    tdrv_set_pool_eng_ucode(ucode_info); // 0xc1690 call tdrv_set_pool_eng_ucode @0x2695f0
    nlog_write(...);                     // 0xc16c9 / 0xc1701 call nlog_write @0x224d40 (success / path logs)
    return NRT_SUCCESS;
}

// tdrv_set_pool_eng_ucode @0x2695f0 — the private store that stages the four 64-bit fields:
//   *(0xc97030) = ucode_info->iram.bin   (RDI+0x00)   // pool_eng_iram_bin
//   *(0xc97028) = ucode_info->iram.size  (RDI+0x08)   // pool_eng_iram_bin_size
//   *(0xc97020) = ucode_info->dram.bin   (RDI+0x10)   // pool_eng_dram_bin
//   *(0xc97018) = ucode_info->dram.size  (RDI+0x18)   // pool_eng_dram_bin_size
```

The four BSS globals occupy the contiguous range **`0xc97018..0xc97038`** (`pool_eng_dram_bin_size@0xc97018`, `pool_eng_iram_bin_size@0xc97028`, `pool_eng_dram_bin@0xc97020`, `pool_eng_iram_bin@0xc97030`). These are later streamed into Q7 IRAM via the `aws_hal_q7_swap_table` DKL mechanism at load/dispatch. **Returns** `NRT_SUCCESS`; `NRT_UNINITIALIZED`(13) if pre-init; `NRT_INVALID` on a null/zero-size image. Everything downstream of `tdrv_set_pool_eng_ucode` is private `tdrv`.

> **GOTCHA — call order.** Call `nrt_set_pool_eng_ucode(ucode_info)` **after** `nrt_init` (it returns `NRT_UNINITIALIZED` otherwise) and **before/at** model load, so the Pool image is staged in the BSS globals before dispatch streams it into IRAM.

---

## 6. Collectives / P2P / async-sendrecv

The fabric family: NeuronCore↔NeuronCore collectives over EFA/NeuronLink. Replica groups are addressed by `(vnc, g_device_id, g_device_count)` = (local core, this rank's global id, world).

### Collective primitives + comm setup

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x7f2d0` | 2.0.0 | `NRT_STATUS` | `nrt_all_gather(int32_t vnc, uint32_t g_device_id, uint32_t g_device_count, uint32_t rank_input_size, void *input, void *output)` | `nrt_barrier.cpp:367` |
| `0xa9390` | 2.0.0 | `NRT_STATUS` | `nrt_build_global_comm(int32_t vnc, uint32_t g_device_id, uint32_t g_device_count)` | `nrt_model.cpp:35` |
| `0x7ee30` | 2.0.0 | `NRT_STATUS` | `nrt_barrier(int32_t vnc, uint32_t g_device_id, uint32_t g_device_count)` | `nrt_barrier.cpp:326` |
| `0x7ff10` | 2.0.0 | `NRT_STATUS` | `nrt_cc_create_stream(uint32_t stream_id)` | `nrt_collectives.cpp:325` |
| `0x7fd90` | 2.0.0 | `NRT_STATUS` | `nrt_cc_global_comm_init(uint32_t vnc, uint32_t g_device_id, uint32_t g_device_count)` | `nrt_collectives.cpp:290` |

> **NOTE.** `nrt_build_global_comm` (barrier/all-gather path) and `nrt_cc_global_comm_init` (the `cc_*` stream path) are two generations of the same setup; `cc` = collective-comm.

### Plugin / fabric bridge

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x94400` | 2.0.0 | `void *` | `nrt_get_libnccl_net(int *err, char *err_msg, size_t err_msg_size)` | `nrt_init.cpp:823` |
| `0x94cb0` | 2.0.0 | `NRT_STATUS` | `nrt_get_attached_efa_bdf(const void *va, char *efa_bdf, size_t *len)` | `nrt_init.cpp:869` |

**`nrt_get_libnccl_net`** returns the embedded NCCL-net plugin vtable pointer (the `libnccl-net.so` contract the collectives layer exposes to NCCL); `*err`/`err_msg` report dlopen-style failures. **`nrt_get_attached_efa_bdf`** returns the PCI BDF string of the EFA NIC backing VA `va`.

### Async send/recv (point-to-point tensor transfer — `nrt_async_sendrecv.cpp`)

A comm is opened connect/accept, tensors are posted, requests/comm are polled, then closed. `test_*` are non-blocking polls (`*done=true` when complete; `test_request` also returns bytes transferred via `*size`). `lnc`/`host_lnc` = the logical-neuron-core endpoint index. All return `NRT_STATUS`, `ver 2.0.0`, tag `HIGH/OBSERVED`.

| addr | signature | decl |
|---|---|---|
| `0x7ed80` | `nrt_async_sendrecv_get_max_num_communicators_per_lnc(int *num)` | `nrt_async_sendrecv.cpp:9` |
| `0x7ed90` | `nrt_async_sendrecv_get_max_num_pending_request(int *num)` | `nrt_async_sendrecv.cpp:14` |
| `0x7eda0` | `nrt_async_sendrecv_init(int local_host_lnc)` | `nrt_async_sendrecv.cpp:19` |
| `0x7edb0` | `nrt_async_sendrecv_close(int local_host_lnc)` | `nrt_async_sendrecv.cpp:24` |
| `0x7edc0` | `nrt_async_sendrecv_connect(const char *peer_ip, int peer_host_lnc, int local_host_lnc, nrt_async_sendrecv_comm_t **send_comm)` | `nrt_async_sendrecv.cpp:29` |
| `0x7edd0` | `nrt_async_sendrecv_accept(const char *peer_ip, int peer_host_lnc, int local_host_lnc, nrt_async_sendrecv_comm_t **recv_comm)` | `nrt_async_sendrecv.cpp:34` |
| `0x7ede0` | `nrt_async_sendrecv_test_comm(nrt_async_sendrecv_comm_t *comm, bool *done)` | `nrt_async_sendrecv.cpp:39` |
| `0x7edf0` | `nrt_async_sendrecv_send_tensor(nrt_tensor_t *tensor, size_t offset, size_t length, nrt_async_sendrecv_comm_t *send_comm, nrt_async_sendrecv_request_t **request)` | `nrt_async_sendrecv.cpp:44` |
| `0x7ee00` | `nrt_async_sendrecv_recv_tensor(nrt_tensor_t *tensor, size_t offset, size_t length, nrt_async_sendrecv_comm_t *recv_comm, nrt_async_sendrecv_request_t **request)` | `nrt_async_sendrecv.cpp:50` |
| `0x7ee10` | `nrt_async_sendrecv_test_request(nrt_async_sendrecv_request_t *request, bool *done, size_t *size)` | `nrt_async_sendrecv.cpp:56` |
| `0x7ee20` | `nrt_async_sendrecv_flush(int lnc)` | `nrt_async_sendrecv.cpp:61` |

---

## 7. `nec_*` device + topology ABI — 16 exports

The low-level "Neuron Experimental Comms" bridge that `libnccom.so.2` calls back **into** — device-enumeration + fabric-topology + dynamic-buffer-sizing primitives beneath the collectives layer.

> **GOTCHA — most `nec_*` return a plain `int`, not `NRT_STATUS`.** Several return `int` (0=ok / `-errno`-style), `bool`, `size_t`, or `void`. Read the `ret` column per row; do **not** assume `NRT_STATUS`.

```
nec_pod_type_t [DWARF]: NONE=0  P2P=1  SWITCH=2  INVALID=3
mla = "ML accelerator" device ; rid = routing-id
```

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x1bfd40` | 2.0.0 | `void` | `nec_inc_semaphore(volatile uint32_t *sem_inc_addr, uint32_t val)` | `nec.cc:19` |
| `0x1bfd50` | 2.0.0 | `int` | `nec_get_device_count(int *available_mla_array, uint32_t array_size)` | `nec.cc:25` |
| `0x1bfd60` | 2.0.0 | `int` | `nec_get_device_pci_bdf(int neuron_dev, uint32_t *domain, uint32_t *bus_num, uint8_t *pci_slot, uint8_t *dev_func)` | `nec.cc:31` |
| `0x1bfdd0` | 2.0.0 | `NRT_STATUS` | `nec_get_virtual_core_size(uint32_t *virtual_core_size)` | `nec.cc:46` |
| `0x1bfdf0` | 2.0.0 | `NRT_STATUS` | `nec_build_port_and_rid_map(int local_nec_dev_id, int *mla_indexes, int *host_device_ids, int count)` | `nec.cc:57` |
| `0x1bfe00` | 2.0.0 | `bool` | `nec_is_mla_available(int local_nec_dev_id, int mla_idx)` | `nec.cc:61` |
| `0x1bfe10` | 2.0.0 | `int` | `nec_mla_idx_to_rid(int local_nec_dev_id, int mla_idx)` | `nec.cc:66` |
| `0x1bfe20` | 2.0.0 | `int` | `nec_rid_to_mla_idx(int local_nec_dev_id, int rid)` | `nec.cc:71` |
| `0x1bfe30` | 2.0.0 | `int` | `nec_get_peer_mla_idx(int local_nec_dev_id, int mla_idx, int port)` | `nec.cc:76` |
| `0x1bfeb0` | 2.0.0 | `int` | `nec_get_p2p_pod_peer_node(uint32_t nec_dev_id, int node, uint32_t port_distance, int *peer_node)` | `nec.cc:91` |
| `0x1bfed0` | 2.0.0 | `NRT_STATUS` | `nec_pod_node_can_access_peer_node(nec_pod_type_t pod_type, uint32_t local_rid, uint32_t local_node_id, uint32_t remote_rid, uint32_t remote_node_id, int *can_access_peer)` | `nec.cc:98` |
| `0x1bfee0` | 2.0.0 | `void` | `nec_ndl_printk(char *str, uint32_t size, uint32_t action)` | `nec.cc:107` |
| `0x1bfef0` | 2.0.0 | `size_t` | `nec_get_dynamic_send_size_bytes(enc_host_mem_t *dyn_input, size_t data_type_sz, int dst_rank, int rank_n)` | `nec.cc:112` |
| `0x1bff00` | 2.0.0 | `size_t` | `nec_get_dynamic_send_offset_bytes(enc_host_mem_t *dyn_input, size_t data_type_sz, int dst_rank, int rank_n)` | `nec.cc:118` |
| `0x1bff10` | 2.0.0 | `size_t` | `nec_get_dynamic_recv_offset_bytes(enc_host_mem_t *dyn_input, size_t data_type_sz, int src_rank, int rank_n)` | `nec.cc:123` |
| `0x1bff20` | 2.0.0 | `void` | `nec_set_recv_size_bytes(enc_host_mem_t *dyn_input, size_t recv_size_bytes, size_t data_type_sz, int src_rank, int rank_n)` | `nec.cc:128` |

The four `nec_get_dynamic_*` / `nec_set_recv_size_bytes` primitives compute bytes-per-rank for variable-length (ragged) all-to-all.

---

## 8. Profile / trace / inspect / sys_trace / debug `[MED→HIGH/OBSERVED — one NQ capture path]`

The observability family. All share the device Notification-Queue capture path and serialize to the `ntff::*` protobuf. Builder objects (config/options) are heap-allocated by an `_allocate`, mutated by `_set_*`, consumed by a `start`/`fetch`, and released by a `_free`. Several setters/frees return `void`.

### Legacy profile, session profile, continuous profile, trace

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0xaee40` | 2.0.0 | `NRT_STATUS` | `nrt_profile_start(nrt_model_t *model, const char *filename)` | `nrt_profile.cpp:1039` |
| `0xb47c0` | 2.0.0 | `NRT_STATUS` | `nrt_profile_stop(const char *filename)` | `nrt_profile.cpp:1367` |
| `0xb6800` | 2.0.0 | `NRT_STATUS` | `nrt_profile_session_start(int32_t vnc)` | `nrt_profile.cpp:2204` |
| `0xb1dd0` | 2.0.0 | `NRT_STATUS` | `nrt_profile_session_stop(int32_t vnc, uint64_t *session_id)` | `nrt_profile.cpp:2271` |
| `0xb61d0` | 2.0.0 | `NRT_STATUS` | `nrt_profile_session_serialize(uint64_t session_id, const char *filename)` | `nrt_profile.cpp:2381` |
| `0xb6070` | 2.0.0 | `NRT_STATUS` | `nrt_profile_session_drop(uint64_t session_id)` | `nrt_profile.cpp:2347` |
| `0xb5f90` | 2.0.0 | `NRT_STATUS` | `nrt_profile_session_drop_all(int32_t vnc)` | `nrt_profile.cpp:2328` |
| `0xacda0` | 2.0.0 | `NRT_STATUS` | `nrt_profile_continuous_options_allocate(nrt_profile_continuous_options_t **options)` | `nrt_profile.cpp:1375` |
| `0xace00` | 2.0.0 | `NRT_STATUS` | `nrt_profile_continuous_options_free(nrt_profile_continuous_options_t *options)` | `nrt_profile.cpp:1387` |
| `0xace40` | 2.0.0 | `NRT_STATUS` | `nrt_profile_continuous_options_set_output_dir(nrt_profile_continuous_options_t *options, const char *output_dir)` | `nrt_profile.cpp:1395` |
| `0xacee0` | 2.0.0 | `NRT_STATUS` | `nrt_profile_continuous_start(nrt_profile_continuous_options_t *options)` | `nrt_profile.cpp:1415` |
| `0xacf90` | 2.0.0 | `NRT_STATUS` | `nrt_profile_continuous_stop(void)` | `nrt_profile.cpp:1503` |
| `0xb47d0` | 2.0.0 | `NRT_STATUS` | `nrt_profile_continuous_save(uint32_t vnc, nrt_profile_continuous_options_t *options)` | `nrt_profile.cpp:1444` |
| `0xad140` | 2.0.0 | `NRT_STATUS` | `nrt_trace_start(bool trace_mem)` | `nrt_profile.cpp:1542` |
| `0xb5ad0` | 2.0.0 | `NRT_STATUS` | `nrt_trace_stop(const char *filename)` | `nrt_profile.cpp:1560` |

### Inspect (activity-typed capture + rich config builder — `nrt_inspect.cpp` / `nrt_inspect_config.cpp`)

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x997e0` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_begin(void)` | `nrt_inspect.cpp:260` |
| `0x99050` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_begin_with_options(nrt_inspect_config_t *options)` | `nrt_inspect.cpp:282` |
| `0x9f130` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_stop(void)` | `nrt_inspect.cpp:341` |
| `0x97b10` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_get_instance_output_dir(const char *output_dir, char *instance_dir_buf, size_t buf_size)` | `nrt_inspect.cpp:1464` |
| `0xa8050` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_allocate(nrt_inspect_config_t **options)` | `nrt_inspect_config.cpp:22` |
| `0xa80a0` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_free(nrt_inspect_config_t *options)` | `nrt_inspect_config.cpp:44` |
| `0xa7f60` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_defaults(nrt_inspect_config_t *options)` | `nrt_inspect_config.cpp:31` |
| `0xa8100` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_output_dir(nrt_inspect_config_t *options, const char *output_dir)` | `nrt_inspect_config.cpp:60` |
| `0xa80e0` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_session_id(nrt_inspect_config_t *options, int session_id)` | `nrt_inspect_config.cpp:52` |
| `0xa85f0` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_activity(nrt_inspect_config_t *options, const char *activity_type, bool enabled)` | `nrt_inspect_config.cpp:165` |
| `0xa8200` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_capture_enabled_for_nc(nrt_inspect_config_t *options, uint32_t nc_idx, bool enabled)` | `nrt_inspect_config.cpp:97` |
| `0xa8280` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_capture_enabled_for_event_type_string(nrt_inspect_config_t *options, const char *event_type, bool enabled)` | `nrt_inspect_config.cpp:111` |
| `0xa82e0` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_enable_inspect(nrt_inspect_config_t *options, bool enable_inspect)` | `nrt_inspect_config.cpp:127` |
| `0xa8300` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_enable_inspect_on_fail(nrt_inspect_config_t *options, bool enable_inspect_on_fail)` | `nrt_inspect_config.cpp:135` |
| `0xa8320` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_inspect_device_profile_mode(nrt_inspect_config_t *options, nrt_inspect_device_profile_mode_t device_profile_mode)` | `nrt_inspect_config.cpp:143` |
| `0xa81e0` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_set_sys_trace_max_events_per_nc(nrt_inspect_config_t *options, uint64_t sys_trace_max_events_per_nc)` | `nrt_inspect_config.cpp:89` |
| `0xa8650` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_get_all_activity_types(const char ***activity_types, size_t *count)` | `nrt_inspect_config.cpp:186` |
| `0xa8730` | 2.0.0 | `NRT_STATUS` | `nrt_inspect_config_get_enabled_activity_types(nrt_inspect_config_t *options, const char ***activity_types, size_t *count)` | `nrt_inspect_config.cpp:217` |
| `0xa8870` | 2.0.0 | `void` | `nrt_inspect_config_free_activity_types(const char **activity_types, size_t count)` | `nrt_inspect_config.cpp:255` |

### Sys-trace (system-event ring — `nrt_sys_trace*.cpp`)

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0xb9800` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_start(nrt_sys_trace_config_t *options)` | `nrt_sys_trace.cpp:11` |
| `0xb9870` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_stop(void)` | `nrt_sys_trace.cpp:18` |
| `0xb9880` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_fetch_events(char **buffer, size_t *written_size, const nrt_sys_trace_fetch_options_t *options)` | `nrt_sys_trace_api.cpp:17` |
| `0xb9910` | 2.0.0 | `void` | `nrt_sys_trace_buffer_free(char *buffer)` | `nrt_sys_trace_api.cpp:24` |
| `0xb9920` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_get_event_types(const char ***event_types, size_t *count)` | `nrt_sys_trace_api.cpp:29` |
| `0xb99f0` | 2.0.0 | `void` | `nrt_sys_trace_free_event_types(const char **event_types, size_t count)` | `nrt_sys_trace_api.cpp:58` |
| `0xb9fa0` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_config_allocate(nrt_sys_trace_config_t **options)` | `nrt_sys_trace_capture_config.cpp:24` |
| `0xb9fd0` | 2.0.0 | `void` | `nrt_sys_trace_config_free(nrt_sys_trace_config_t *options)` | `nrt_sys_trace_capture_config.cpp:44` |
| `0xb9f10` | 2.0.0 | `void` | `nrt_sys_trace_config_set_defaults(nrt_sys_trace_config_t *options)` | `nrt_sys_trace_capture_config.cpp:33` |
| `0xb9ff0` | 2.0.0 | `void` | `nrt_sys_trace_config_set_max_events_per_nc(nrt_sys_trace_config_t *options, uint64_t max_events_per_nc)` | `nrt_sys_trace_capture_config.cpp:50` |
| `0xba000` | 2.0.0 | `void` | `nrt_sys_trace_config_set_capture_enabled_for_nc(nrt_sys_trace_config_t *options, uint32_t nc_idx, bool enabled)` | `nrt_sys_trace_capture_config.cpp:56` |
| `0xba0d0` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_config_set_capture_enabled_for_event_type(nrt_sys_trace_config_t *options, const char *event_type, bool enabled)` | `nrt_sys_trace_capture_config.cpp:67` |
| `0xba8a0` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_config_get_enabled_event_types(nrt_sys_trace_config_t *options, const char ***event_types, size_t *count)` | `nrt_sys_trace_capture_config.cpp:102` |
| `0xb9a60` | 2.0.0 | `NRT_STATUS` | `nrt_sys_trace_fetch_options_allocate(nrt_sys_trace_fetch_options_t **options)` | `nrt_sys_trace_api_options.cpp:8` |
| `0xb9a90` | 2.0.0 | `void` | `nrt_sys_trace_fetch_options_free(nrt_sys_trace_fetch_options_t *options)` | `nrt_sys_trace_api_options.cpp:25` |
| `0xb9a40` | 2.0.0 | `void` | `nrt_sys_trace_fetch_options_set_defaults(nrt_sys_trace_fetch_options_t *options)` | `nrt_sys_trace_api_options.cpp:17` |
| `0xb9aa0` | 2.0.0 | `void` | `nrt_sys_trace_fetch_options_set_max_events_per_nc(nrt_sys_trace_fetch_options_t *options, uint64_t max_events_per_nc)` | `nrt_sys_trace_api_options.cpp:29` |
| `0xb9ab0` | 2.0.0 | `void` | `nrt_sys_trace_fetch_options_set_nc_idx(nrt_sys_trace_fetch_options_t *options, uint64_t nc_idx)` | `nrt_sys_trace_api_options.cpp:35` |

> **CORRECTION — `nrt_sys_trace_stop` is defined in `nrt_sys_trace.cpp:18`, not `nrt_sys_trace.h:119`.** The export body at `0xb9870` (a 5-byte thunk, `low_pc 0xb9870..high_pc 0xb9875`) is the out-of-line definition whose `DW_AT_decl_file` is `nrt_sys_trace.cpp:18`. The `nrt_sys_trace.h:119` DIE (`low_pc 0x9fa20`) is an *inlined* instance elsewhere, not the export. The table above uses the `.cpp` coordinate, consistent with every other row.

### Debug stream + memory stats

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x91370` | 2.0.0 | `NRT_STATUS` | `nrt_debug_client_connect(int32_t logical_nc_idx, int *stream_fd)` | `nrt_debug_stream.cpp:17` |
| `0x91490` | 2.0.0 | `void` | `nrt_debug_client_connect_close(int stream_fd)` | `nrt_debug_stream.cpp:45` |
| `0x91530` | 2.0.0 | `NRT_STATUS` | `nrt_debug_client_read_one_event(int stream_fd, ndebug_stream_event_header_t *header, void **payload)` | `nrt_debug_stream.cpp:54` |
| `0xb90a0` | 2.0.0 | `NRT_STATUS` | `nrt_get_vnc_memory_stats(uint32_t vnc, nrt_vnc_memory_stats_t *stats, size_t stats_size_in, size_t *stats_size_out)` | `nrt_stats.cpp:19` |

`nrt_get_vnc_memory_stats` uses the same version-tolerant probe-then-fill idiom (`stats_size_in`/`out`) as `nrt_get_model_info`.

---

## 9. `NRT_3.0.0` async schedule (`nrta_*`) `[MED→HIGH/OBSERVED — nrt_async.cpp — 8 exports, the only NRT_3.0.0 node]`

A finer-grained submission model layered on the Rust `neuron_rustime` crate: build a schedule (`cc_prepare`), enqueue it (`cc_schedule`/`execute_schedule`/`tensor_*`), get a sequence token, then poll it (`is_completed`). Distinct from §4's sync/implicit-async path. Every submit returns an `nrta_seq_t` in `*req_sequence` and the eventual exec result in `*exec_ret`; `queue` is the hardware queue index, `lnc_idx` the logical-neuron-core.

```
nrta_xu_t        [DWARF]: NRTA_XU_TENSOR_OP=0  NRTA_XU_COMPUTE=1  NRTA_XU_COLLECTIVES=2  NRTA_XU_TYPE_NUM=3
nrt_cc_op_type_t [DWARF]: ALLGATHER=0  ALLREDUCE=1  REDUCESCATTER=2
nrt_op_type_t    [DWARF]: ADD=0  FMA=1  MAX=2  MIN=3  INVALID=15
nrt_dtype_t      [DWARF, sparse]: UNKNOWN/INVALID=0  UINT64=1  INT8=2  UINT8=3  INT16=4  UINT16=5
                   BFLOAT16=6  FLOAT16=7  INT32=8  UINT32=9  FLOAT32=10  FP32R=11  INT64=12
                   FP8_E3=13  FP8_E4=14  FP8_E5=15
```

| addr | ver | ret | signature | decl |
|---|---|---|---|---|
| `0x7c980` | 3.0.0 | `NRT_STATUS` | `nrta_cc_prepare(nrt_cc_comm_t *comm, nrt_tensor_list_t *input, nrt_tensor_list_t *output, nrt_dtype_t dtype, nrt_op_type_t op, nrt_cc_op_type_t cc_op, nrt_cc_context_t **cc_ctx)` | `nrt_async.cpp:691` |
| `0x7bd80` | 3.0.0 | `NRT_STATUS` | `nrta_cc_schedule(nrt_cc_context_t **cc_ctx, int queue, NRT_STATUS *exec_ret, nrta_seq_t *req_sequence)` | `nrt_async.cpp:749` |
| `0x7bb00` | 3.0.0 | `NRT_STATUS` | `nrta_execute_schedule(nrt_model_t *model, const nrt_tensor_set_t *input, nrt_tensor_set_t *output, int queue, NRT_STATUS *exec_ret, nrta_seq_t *req_sequence)` | `nrt_async.cpp:629` |
| `0x7c450` | 3.0.0 | `NRT_STATUS` | `nrta_get_sequence(uint32_t lnc_idx, nrta_xu_t xu_type, int queue, nrta_seq_t *seq)` | `nrt_async.cpp:822` |
| `0x7c720` | 3.0.0 | `NRT_STATUS` | `nrta_is_completed(nrta_seq_t seq, bool *is_completed)` | `nrt_async.cpp:794` |
| `0x7d8c0` | 3.0.0 | `NRT_STATUS` | `nrta_tensor_copy(nrt_tensor_t *src, uint64_t src_offset, nrt_tensor_t *dst, uint64_t dst_offset, uint64_t size, int lnc_idx, int queue, NRT_STATUS *exec_ret, nrta_seq_t *req_sequence)` | `nrt_async.cpp:540` |
| `0x7e440` | 3.0.0 | `NRT_STATUS` | `nrta_tensor_read(void *buf, nrt_tensor_t *tensor, uint64_t offset, uint64_t size, int lnc_idx, int queue, NRT_STATUS *exec_ret, nrta_seq_t *req_sequence)` | `nrt_async.cpp:476` |
| `0x7df10` | 3.0.0 | `NRT_STATUS` | `nrta_tensor_write(nrt_tensor_t *tensor, const void *buf, uint64_t offset, uint64_t size, int lnc_idx, int queue, NRT_STATUS *exec_ret, nrta_seq_t *req_sequence)` | `nrt_async.cpp:412` |

> **GOTCHA — `nrta_is_completed` takes `nrta_seq_t` *by value*.** Every other `nrta_*` passes the sequence token by pointer (`nrta_seq_t *seq` / `*req_sequence`); the poll alone takes it by value. `nrta_is_completed` is a non-blocking poll: `*is_completed=true` once the token's work has retired.

---

## 10. The status-code contract (`nrt_status_t` / `NRT_STATUS`)

Every `NRT_STATUS`-returning export shares one flat enum (the typedef `NRT_STATUS = enum nrt_status`). Read verbatim from this binary's DWARF (`DW_AT_const_value` per enumerator). Gaps at **8** and **12** are intentional/reserved — there is no enumerator with those values.

### Band A — generic runtime status (0..15)

| val | name | val | name |
|---:|---|---:|---|
| 0 | `NRT_SUCCESS` | 9 | `NRT_LOAD_NOT_ENOUGH_NC` |
| 1 | `NRT_FAILURE` | 10 | `NRT_UNSUPPORTED_NEFF_VERSION` |
| 2 | `NRT_INVALID` | 11 | `NRT_FAIL_HOST_MEM_ALLOC` |
| 3 | `NRT_INVALID_HANDLE` | 13 | `NRT_UNINITIALIZED` |
| 4 | `NRT_RESOURCE` | 14 | `NRT_CLOSED` |
| 5 | `NRT_TIMEOUT` | 15 | `NRT_QUEUE_EMPTY` |
| 6 | `NRT_HW_ERROR` | | *(8, 12 reserved — no enumerator)* |
| 7 | `NRT_QUEUE_FULL` | | |

### Band B — execution result (101, 1002..1100)

| val | name | val | name |
|---:|---|---:|---|
| 101 | `NRT_EXEC_UNIT_UNRECOVERABLE` | 1004 | `NRT_EXEC_COMPLETED_WITH_ERR` |
| 1002 | `NRT_EXEC_BAD_INPUT` | 1005 | `NRT_EXEC_NC_BUSY` |
| 1003 | `NRT_EXEC_COMPLETED_WITH_NUM_ERR` | 1006 | `NRT_EXEC_OOB` |
| | | 1100 | `NRT_COLL_PENDING` |

### Band C — hardware-fault execution result (1200..1206)

| val | name | val | name |
|---:|---|---:|---|
| 1200 | `NRT_EXEC_HW_ERR_COLLECTIVES` | 1204 | `NRT_EXEC_SW_NQ_OVERFLOW` |
| 1201 | `NRT_EXEC_HW_ERR_HBM_UE` | 1205 | `NRT_EXEC_HW_ERR_REPAIRABLE_HBM_UE` |
| 1202 | `NRT_EXEC_HW_ERR_NC_UE` | 1206 | `NRT_NETWORK_PROXY_FAILURE` |
| 1203 | `NRT_EXEC_HW_ERR_DMA_ABORT` | | |

### Which family returns which band (per-call channel)

| Family | Bands it can return |
|---|---|
| Pure getters (counts, ids, `get_va`/`get_size`) | Band A subset only (`SUCCESS`/`INVALID`/`UNINITIALIZED`/`CLOSED`); `get_va`/`get_size` return the value directly (NULL/0 on error), not a status |
| Load family (`nrt_load`, `nrt_load_collectives`) | Band A + load-specific **9/10/11** (+ 1206-class for collectives) |
| Execute family + `nrt_tensor_check_output_completion` + `nrta_*` | Band A timeout/queue codes **and** the full Band B/C set; multi-NC faults ranked via `nrt_status_priority_t` (`NONE`=0, `MEDIUM`=1, `HIGH`=2, `CRITICAL`=3) — the **worst** wins |
| `nec_*` | Mostly `int` (0/`-errno`), `bool`, or `size_t`; only `_get_virtual_core_size`/`_build_port_and_rid_map`/`_pod_node_can_access_peer_node` return `NRT_STATUS` |
| `void` exports (`nrt_close`, `nrt_tensor_free`, `nrt_destroy_tensor_set`, the `_set_*`/`_free` builder mutators) | Cannot fail at the ABI — internal errors are `nlog`-logged only |

---

## 11. Handle + struct quick-ref

The opaque types the table points at. Layouts (byte-confirmed here) for the structs a reimplementer must size; everything else is an opaque heap handle created/freed through its own allocate/free export.

| Type | What it wraps / layout |
|---|---|
| `nrt_model_t*` | the `dlr_model` handle; state walks `INVALID → STARTING → RUNNING` (layout in [the exec-state struct census](../appendix/struct-exec-state-census.md)) |
| `nrt_tensor_t*` | opaque buffer handle; placement `DEVICE`/`HOST`/`VIRTUAL` |
| `nrt_tensor_set_t*` | name→tensor map consumed by execute; names = model tensor-info names = metaneff I/O names |
| `nrt_version_t` | **224 B** (`0xe0`): `u64 rt_major@+0, rt_minor@+8, rt_patch@+16, rt_maintenance@+24; char rt_detail[128]@+32; char git_hash[64]@+160` |
| `nrt_instance_info_t` | **32 B** (`0x20`): `u32 family@+0; u32 size@+4; char arch_name[16]@+8; char device_revision[8]@+24` |
| `nrt_ucode_info` | **32 B** (`0x20`): `{ nrt_ucode_img iram@+0; nrt_ucode_img dram@+16 }` |
| `nrt_ucode_img` | **16 B** (`0x10`): `{ uint8_t *bin@+0; size_t size@+8 }` |
| `nrt_tensor_batch_t` | `{ const nrt_tensor_t *tensor; const nrt_tensor_batch_op_t *ops; u32 num_ops }` |
| `NRT_ASYNC_EXEC_STATUS_CALLBACK` | `void (*)(void *user, u32, u32, u64, NRT_STATUS)` |
| `nrta_seq_t`, `nrt_cc_context_t*`, `nrt_cc_comm_t*`, `nrt_tensor_list_t*`, `nrt_async_sendrecv_comm_t*`, `nrt_async_sendrecv_request_t*`, `nrt_*_config_t*`, `nrt_*_options_t*` | opaque heap handles — created/freed only through their own allocate/free exports above |

---

## 12. Reimplementer notes + confidence ledger

**Sync inference calling order.**
`nrt_init` → `nrt_load` → `nrt_get_model_tensor_info` → (`nrt_tensor_allocate` ×N) → `nrt_allocate_tensor_set` ×2 → `nrt_add_tensor_to_tensor_set` ×N → `nrt_execute` → `nrt_tensor_read` → … → `nrt_free_model_tensor_info` / `nrt_unload` / `nrt_close`.

**GPSIMD custom kernel.** `nrt_set_pool_eng_ucode(ucode_info)` **after** `nrt_init` and **before/at** load, so the Q7 Pool image is staged in `pool_eng_*_bin` (`0xc97018..0xc97038`) before dispatch streams it into IRAM.

**Version-tolerant getters.** `nrt_get_version`, `nrt_get_instance_info`, `nrt_get_model_info`, `nrt_get_vnc_memory_stats` all take a caller size and (where applicable) return the would-be size — implement the truncate / probe-then-fill idiom; **never** hard-assume a struct size (and size `nrt_version_t` to 224, not 232).

**Confidence ledger.**
- All **145** signatures, addresses, version nodes, and `decl_file:line` are **OBSERVED** from this binary's DWARF/symtab/disasm — **HIGH**.
- Three items are **CORRECTED** against the binary: (1) `nrt_get_model_instance_count` *has* a full DWARF prototype `(nrt_model_t*, uint32_t*) → NRT_STATUS`; (2) `nrt_sys_trace_stop` is defined in `nrt_sys_trace.cpp:18`, not `.h:119`; (3) `nrt_version_t` is 224 B (`0xe0`), not 232.
- The full `nrt_status` enum (Bands A/B/C, gaps at 8/12) is **byte-confirmed** from DWARF `DW_AT_const_value`. Status-code-per-family channels are OBSERVED for the fault path and INFERRED-from-convention where a family's exact returnable subset was not individually disassembled (**MED** for those mappings).
- The 4 `std::string _M_*` `LOCAL` helpers and the 2 version-node `OBJECT` markers are **not API** — ignore them when building the contract. Use **145**, not 151, as the callable-export count.

**Cross-references.** [public-vs-internal partition](public-vs-internal-partition.md) (which symbols are the *private* `tdrv_*`/`kmgr_*` surface) · [runtime synthesis](runtime-synthesis.md) (the load/execute narrative this table backs) · [metaneff I/O ABI](../neff/metaneff-io-abi.md) (the metaneff host I/O / tensor-name binding) and [exec-state struct census](../appendix/struct-exec-state-census.md) (`dlr_model` / tensor struct layouts behind these handles).
