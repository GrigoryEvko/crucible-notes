# libndbg — Neuron Debugger Library: Overview

`/opt/aws/neuron/lib/libndbg.so` is the C/C++ debugger library shipped
with the AWS Neuron tools package. It exposes a stable C API used by the
`neuron-dbg` CLI (and any tooling that links against it) to interrogate
NeuronCore device state symbolically — "read me register `M2S_Q.PROD_CONF`
on block `APB_SE_0_SDMA_3_BCAST_UDMA_M2S`" — without the caller knowing
which silicon generation it is talking to.

## Shape

- 56,432,488 bytes stripped.
- 15,127 native exports; 1,038 user-visible (non-mangled) C symbols.
- Three per-arch backends, addressed via codenames in the public API:
  - `cayman` — `ndbg_cayman_init`, hardware tag `NDBG_HARDWARE_V3`
  - `mariana` — `ndbg_mariana_init`, hardware tag `NDBG_HARDWARE_V4`
  - `sunda` — `ndbg_sunda_init`, hardware tag `NDBG_HARDWARE_V2`
- Built from an internal source tree named `KaenaDebuggerLib-2.29.11.0`
  (confirmed by an `__assert_fail` path string embedded in the binary).

## How the API factors

The library exposes a single struct `ndbg_backend_t` (368 bytes) and a
suite of free functions that all forward through that struct's vtable.
A caller opens a device, allocates an `ndbg_backend_t`, calls one of the
three `ndbg_<arch>_init` functions to populate the vtable for the
intended silicon, and then drives the rest of the API through generic
non-arch-prefixed thunks:

```c
ndbg_backend_t backend;
ndbg_cayman_init(&backend);          // or mariana / sunda
// backend->ndbg_csr_block_resolve_name is now ndbg_csr_block_cayman_resolve_name

ndbg_csr_block_name_t block;
ndbg_csr_block_resolve_name(&backend, "APB_SE_0_SDMA_3_BCAST_UDMA_M2S", 30, &block);

ndbg_csr_bundle_name_t bundle;
ndbg_csr_bundle_resolve_name(&backend, "M2S_Q", 5, &bundle);

ndbg_csr_name_t csr;
ndbg_csr_resolve_name(&backend, "PROD_CONF", 9, &csr);

ndbg_csr_t register_descriptor;
ndbg_csr_info(&backend, block, bundle, csr, /*index=*/0, &register_descriptor);

uint32_t value;
ndbg_csr_read(&backend, /*device_index=*/0, &value, register_descriptor);
```

The chain on the right of each call is identical regardless of which
backend was initialised. The arch-specific logic lives entirely behind
the vtable.

## Three subsystems

The library splits into three concentric layers.

1. **Symbolic layer** — string ↔ enum dictionaries. The three
   `ndbg_csr_<level>_resolve_name` entry points convert CSR-block
   names, CSR-bundle names and CSR-field names into compact enum IDs
   (`ndbg_csr_block_name_t` 3,265 entries, `ndbg_csr_bundle_name_t` 140,
   `ndbg_csr_name_t` 1,182). A parallel set of `ndbg_csr_<level>_suggestions`
   entry points implement five-result-deep autocomplete for interactive
   REPL use, realised as a hand-rolled trie of thousands of
   per-prefix C++ functions. See [CSR Trie](csr-trie.md).

2. **Geometry layer** — enum → address. Once the symbolic resolution
   has produced `(block, bundle, csr, index)` enum tuples,
   `ndbg_csr_block_info`, `ndbg_csr_bundle_info` and `ndbg_csr_info`
   look up the BAR0 byte offsets, window sizes and instance counts
   stored in giant per-arch switch tables. These tables ARE the
   register database. See [Register Catalog](register-catalog.md).

3. **I/O layer** — address → device. `ndrv_csr_read` and `ndrv_csr_write`
   compose the geometry results into a single 64-bit BAR address and
   dispatch one PCIe MMIO via the kernel module's ioctl interface on
   `/dev/neuronN`. See [Read / Write Vector](read-write-vector.md).

## Backend object layout

`ndbg_backend_t` (offsets in bytes):

| offset | type                                | field |
|-------:|------------------------------------|-------|
|     0  | `void *`                           | `private_data` (typically an `ndl_device_t *` array) |
|     8  | `ndbg_backend_common_t` (208 B)    | `common_data` — per-arch geometry constants |
|   216  | `ndbg_hardware_version_t`          | `hardware_version` (V1..V4) |
|   224  | function pointer                   | `ndbg_csr_block_info` |
|   232  | function pointer                   | `ndbg_csr_bundle_info` |
|   240  | function pointer                   | `ndbg_csr_info` |
|   248  | function pointer                   | `ndbg_csr_printf` |
|   256  | function pointer                   | `ndbg_csr_block_resolve_name` |
|   264  | function pointer                   | `ndbg_csr_bundle_resolve_name` |
|   272  | function pointer                   | `ndbg_csr_resolve_name` |
|   280  | function pointer                   | `ndbg_csr_block_suggestions` |
|   288  | function pointer                   | `ndbg_csr_bundle_suggestions` |
|   296  | function pointer                   | `ndbg_csr_suggestions` |
|   304  | function pointer                   | `ndbg_csr_block_suggestions_conditional` |
|   312  | function pointer                   | `ndbg_csr_bundle_suggestions_conditional` |
|   320  | function pointer                   | `ndbg_csr_suggestions_conditional` |
|   328  | function pointer                   | `ndbg_csr_from_device_address` |
|   336  | function pointer                   | `ndbg_engine_execute_instructions` (set by NDL layer, not by init()) |
|   344  | function pointer                   | `ndbg_engine_run_state` |
|   352  | function pointer                   | `ndbg_engine_start_address` |
|   360  | function pointer                   | `ndbg_engine_program_counter` |

The 20-slot vtable maps directly onto the public API: every
`ndbg_csr_*` and `ndbg_engine_*` external function is a 6-byte
trampoline that does `jmp *backend->[fixed_offset]`. The library
exports both the trampolines (top-level `ndbg_csr_block_info` etc.) and
the per-arch implementations (`ndbg_csr_block_cayman_info`,
`ndbg_csr_block_mariana_info`, `ndbg_csr_block_sunda_info`), so callers
can choose either polymorphic dispatch (`ndbg_csr_block_info`) or a
direct call.

## Public API surface

```
Lifecycle:        ndbg_{cayman,mariana,sunda}_init, ndbg_cleanup
Engine state:     ndbg_{cayman,mariana,sunda}_engine_program_counter,
                  ndbg_{cayman,mariana,sunda}_engine_run_state,
                  ndbg_{cayman,mariana,sunda}_engine_start_address
CSR access:       ndbg_csr_read, ndbg_csr_read_hi_lo,
                  ndbg_csr_write, ndbg_csr_write_hi_lo,
                  ndbg_csr_bundle_read, ndbg_csr_bundle_write,
                  ndbg_csr_info, ndbg_csr_printf,
                  ndbg_csr_block_info, ndbg_csr_bundle_info,
                  ndbg_csr_block_resolve_name,
                  ndbg_csr_bundle_resolve_name,
                  ndbg_csr_resolve_name,
                  ndbg_csr_block_suggestions{,_conditional},
                  ndbg_csr_bundle_suggestions{,_conditional},
                  ndbg_csr_suggestions{,_conditional},
                  ndbg_csr_block_name_to_string,
                  ndbg_csr_bundle_name_to_string,
                  ndbg_csr_name_to_string
Debug info:       ndbg_debug_info_load, ndbg_debug_info_cleanup,
                  ndbg_debug_info_get_{device_id,core_id,num_subgraphs},
                  ndbg_debug_info_get_instructions_{base,size}
Strings:          ndbg_nc_engine_string, ndbg_error_string
```

`ndbg_cleanup(backend)` is intentionally trivial: it calls
`ndrv_lifecycle_cleanup(backend)` and then `free(backend)`. There is
no per-arch destructor slot.

## Where to read next

- [Per-Arch Backend](per-arch-backend.md) — the vtable wiring set up by
  `ndbg_<arch>_init`.
- [CSR Trie](csr-trie.md) — how a string register name becomes a
  numeric ID.
- [Register Catalog](register-catalog.md) — what the per-arch
  `info()` switch tables encode.
- [Read / Write Vector](read-write-vector.md) — the IOCTL bridge to
  `/dev/neuronN`.
- [Register Grouping](register-grouping.md) — block → bundle → csr →
  index hierarchy.
- [Per-Arch Differences](per-arch-differences.md) — concrete deltas
  between V2 (sunda), V3 (cayman) and V4 (mariana).
