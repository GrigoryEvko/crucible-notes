# Per-Arch Backend Wiring

The `ndbg_<arch>_init` functions are the only place in libndbg where
architecture choice is made. After they run, every other call goes
through the vtable they installed on `ndbg_backend_t`.

## The init contract

Each `ndbg_<arch>_init(backend)` does three things, in this order:

1. Set the hardware-version tag (`backend->hardware_version = V2/V3/V4`).
2. Populate `backend->common_data` (208 bytes) with per-arch geometry:
   how many NCs, how many DMA engines per NC, how many CC cores, the
   SRAM/DRAM base addresses and sizes, the descriptor size, etc.
3. Install 19 of the 20 vtable function pointers (the
   `ndbg_engine_execute_instructions` slot at offset 336 is left for
   the NDL/NRT layer to fill, since execution is policy belonging to
   the runtime, not the debugger).

The slot-by-slot assignment is uniform across the three backends — each
init points the slot at its own `ndbg_csr_<arch>_*` symbol. The cayman
init body (excerpted):

```c
backend->hardware_version            = NDBG_HARDWARE_V3;
backend->common_data.sram_size       = 0x2000000;
...
backend->ndbg_csr_block_info         = ndbg_csr_block_cayman_info;
backend->ndbg_csr_bundle_info        = ndbg_csr_bundle_cayman_info;
backend->ndbg_csr_info               = ndbg_csr_cayman_info;
backend->ndbg_csr_printf             = ndbg_csr_cayman_printf;
backend->ndbg_csr_block_resolve_name = ndbg_csr_block_cayman_resolve_name;
backend->ndbg_csr_bundle_resolve_name= ndbg_csr_bundle_cayman_resolve_name;
backend->ndbg_csr_resolve_name       = ndbg_csr_cayman_resolve_name;
backend->ndbg_csr_block_suggestions  = ndbg_csr_block_cayman_suggestions;
backend->ndbg_csr_bundle_suggestions = ndbg_csr_bundle_cayman_suggestions;
backend->ndbg_csr_suggestions        = ndbg_csr_cayman_suggestions;
backend->ndbg_csr_block_suggestions_conditional  =
                                       ndbg_csr_block_cayman_suggestions_conditional;
backend->ndbg_csr_bundle_suggestions_conditional =
                                       ndbg_csr_bundle_cayman_suggestions_conditional;
backend->ndbg_csr_suggestions_conditional        =
                                       ndbg_csr_cayman_suggestions_conditional;
backend->ndbg_csr_from_device_address= ndbg_csr_cayman_from_device_address;
backend->ndbg_engine_run_state       = ndbg_cayman_engine_run_state;
backend->ndbg_engine_start_address   = ndbg_cayman_engine_start_address;
backend->ndbg_engine_program_counter = ndbg_cayman_engine_program_counter;
```

The mariana init is structurally identical with `mariana` substituted.
The sunda init is the same shape but does not touch `num_top_dma_engines_per_device`
or the upper four `sram_base_addresses[]` entries (sunda has fewer NCs
and a smaller memory map).

## Per-arch geometry table

Constants set by `common_data` (extracted from the three init bodies):

| common_data field                       | cayman (V3) | mariana (V4) | sunda (V2) |
|-----------------------------------------|------------:|-------------:|-----------:|
| `num_dma_engines_per_nc`                |          16 |           16 |         16 |
| `num_top_dma_engines_per_device`        |           4 |            4 |          2 |
| `num_ncs_per_device`                    |           8 |            8 |          2 |
| `num_cc_cores_per_device`               |          20 |           20 |          6 |
| `num_bytes_per_descriptor`              |          64 |           64 |         16 |
| `instruction_size`                      |          64 |           64 |         16 |
| `instruction_alignment`                 |          32 |           32 |        512 |
| `num_nc_registers`                      |          64 |           64 |         64 |
| `num_nc_semaphores`                     |          16 |           16 |        256 |
| `num_nc_events`                         |         256 |          256 |        256 |
| `num_sbufs`                             |           8 |            8 |          2 |
| `sram_size`                             |       32 MiB |       32 MiB |    768 KiB |
| `sram_partition_active_size`            |      224 KiB |      256 KiB |     16 MiB |
| `num_drams`                             |           4 |            4 |          2 |
| `dram_sizes[i]`                         | 4 GiB each  | 4 GiB each   | 16 GiB region |
| `sram_base_addresses[0..3]` (lower half)| 0x20–0x70 G | 0x20–0x70 G  | varies     |
| `sram_base_addresses[4..7]` (upper half)| 0x802–0x807 G | 0x802–0x807 G | unused  |

The sunda init notably packs several geometry fields into a single
64-bit assignment per pair (e.g. `*(_QWORD *)&num_dma_engines_per_nc =
0x200000010LL` sets `num_dma_engines_per_nc = 16` and
`num_top_dma_engines_per_device = 2`). The cayman and mariana inits
do the same pairing; the V4 (mariana) init differs from V3 (cayman)
ONLY in the `sram_partition_active_size` slot and in the choice of
per-arch function symbols. Everything else in common_data is identical
between cayman and mariana.

## Vtable destinations per backend

| vtable slot                        | cayman                                  | mariana                                  | sunda                                  |
|------------------------------------|-----------------------------------------|------------------------------------------|----------------------------------------|
| `ndbg_csr_block_info`              | `ndbg_csr_block_cayman_info`            | `ndbg_csr_block_mariana_info`            | `ndbg_csr_block_sunda_info`            |
| `ndbg_csr_bundle_info`             | `ndbg_csr_bundle_cayman_info`           | `ndbg_csr_bundle_mariana_info`           | `ndbg_csr_bundle_sunda_info`           |
| `ndbg_csr_info`                    | `ndbg_csr_cayman_info`                  | `ndbg_csr_mariana_info`                  | `ndbg_csr_sunda_info`                  |
| `ndbg_csr_printf`                  | `ndbg_csr_cayman_printf`                | `ndbg_csr_mariana_printf`                | `ndbg_csr_sunda_printf`                |
| `ndbg_csr_block_resolve_name`      | `ndbg_csr_block_cayman_resolve_name`    | `ndbg_csr_block_mariana_resolve_name`    | `ndbg_csr_block_sunda_resolve_name`    |
| `ndbg_csr_bundle_resolve_name`     | `ndbg_csr_bundle_cayman_resolve_name`   | `ndbg_csr_bundle_mariana_resolve_name`   | `ndbg_csr_bundle_sunda_resolve_name`   |
| `ndbg_csr_resolve_name`            | `ndbg_csr_cayman_resolve_name`          | `ndbg_csr_mariana_resolve_name`          | `ndbg_csr_sunda_resolve_name`          |
| `ndbg_csr_block_suggestions`       | `ndbg_csr_block_cayman_suggestions`     | `ndbg_csr_block_mariana_suggestions`     | `ndbg_csr_block_sunda_suggestions`     |
| `ndbg_csr_bundle_suggestions`      | `ndbg_csr_bundle_cayman_suggestions`    | `ndbg_csr_bundle_mariana_suggestions`    | `ndbg_csr_bundle_sunda_suggestions`    |
| `ndbg_csr_suggestions`             | `ndbg_csr_cayman_suggestions`           | `ndbg_csr_mariana_suggestions`           | `ndbg_csr_sunda_suggestions`           |
| `ndbg_csr_block_suggestions_conditional`  | `..._conditional` (cayman variant)     | `..._conditional` (mariana variant)     | `..._conditional` (sunda variant)     |
| `ndbg_csr_bundle_suggestions_conditional` | (cayman variant)                        | (mariana variant)                        | (sunda variant)                        |
| `ndbg_csr_suggestions_conditional`        | (cayman variant)                        | (mariana variant)                        | (sunda variant)                        |
| `ndbg_csr_from_device_address`     | `ndbg_csr_cayman_from_device_address`   | `ndbg_csr_mariana_from_device_address`   | `ndbg_csr_sunda_from_device_address`   |
| `ndbg_engine_run_state`            | `ndbg_cayman_engine_run_state`          | `ndbg_mariana_engine_run_state`          | `ndbg_sunda_engine_run_state`          |
| `ndbg_engine_start_address`        | `ndbg_cayman_engine_start_address`      | `ndbg_mariana_engine_start_address`      | `ndbg_sunda_engine_start_address`      |
| `ndbg_engine_program_counter`      | `ndbg_cayman_engine_program_counter`    | `ndbg_mariana_engine_program_counter`    | `ndbg_sunda_engine_program_counter`    |
| `ndbg_engine_execute_instructions` | *NOT SET* — caller installs            | *NOT SET* — caller installs              | *NOT SET* — caller installs            |

The `_execute_instructions` slot is populated externally by symbols
like `ndbg_ndl_cayman_engine_execute_instructions` (in libndl or in
the NRT layer) — splitting the "queue a NEFF on the engine" path from
the pure-debugger lookups keeps libndbg itself free of any runtime
dependency on the actual NEFF format.

## Global ctors

Each per-arch translation unit ships a generated `_GLOBAL__sub_I_*.cpp`
constructor, visible in the binary as:

- `_GLOBAL__sub_I_ndbg_cayman.cpp`  @ 0x294240
- `_GLOBAL__sub_I_ndbg_mariana.cpp` @ 0x2942A0
- `_GLOBAL__sub_I_ndbg.cpp`         @ 0x2942D0
- `_GLOBAL__sub_I_ndbg_sunda.cpp`   @ 0x294300

These do nothing more than call `std::ios_base::Init::Init` and
register `~Init` via `__cxa_atexit`. The per-arch register databases
are NOT pulled in via global construction; they live in `.rodata` and
are accessed by direct switch dispatch from the per-arch functions
listed above.

## Trampoline pattern

Each vtable slot has a corresponding stable C export (the
non-arch-prefixed name), realised as a 6-byte trampoline:

```c
ndbg_error_code_t ndbg_csr_block_info(ndbg_backend_t *backend,
                                      ndbg_csr_block_name_t block,
                                      ndbg_csr_block_t *out) {
  return backend->ndbg_csr_block_info(backend, block, out);
}
```

This is what the IDA decompilation calls
`indirect<...>(backend, block, out) @ 0x78a240 *indirect*`. The 6
bytes are `mov rax, [rdi+0xE0]; jmp rax` (where `0xE0 = 224`).

The library also exports a `._ndbg_csr_block_resolve_name` thunk
(at 0x262B20) called by the 18 internal helpers that need the
arch-polymorphic resolver during e.g. `cayman_dma_get_m2s_block_name`
or `ndbg_<arch>_engine_program_counter` — these helpers were originally
written without knowing which backend they were on. The thunk simply
tail-calls the vtable slot, the same as the exported trampoline.
