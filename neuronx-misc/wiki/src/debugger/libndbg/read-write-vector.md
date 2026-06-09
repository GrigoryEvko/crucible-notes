# Read / Write Op-Vector — IOCTL to `/dev/neuronN`

All CSR reads and writes in libndbg ultimately go through one of two
ioctl numbers on the kernel module's char device:

| ioctl   | direction | size      | payload                                     |
|---------|-----------|-----------|---------------------------------------------|
| `0x80084E0B` | read   | 8 bytes   | `{uint8_t bar; uint64_t *addresses; uint32_t count; uint32_t *values;}` |
| `0x40084E0C` | write  | 8 bytes   | same shape                                  |
| `0x80084E03` | read   | (struct)  | `neuron_ioctl_device_info` — used at device open to populate `csr_base[]` |
| `0xC0204E70` | read   | (struct)  | BAR-region mmap query — populates `(va, size, offset)` used by an `mmap()` to map a BAR region directly |

The decoder for `_IOC_*` macros on `0x80084E0B`:
- `_IOC_DIR    = 2  (_IOC_READ)`
- `_IOC_TYPE   = 0x4E   ('N')` — the kernel module's IOC type
- `_IOC_NR     = 0x0B`
- `_IOC_SIZE   = 8` — the inline payload pointer + bar/count packed as
  one 64-bit and one 64-bit half (the actual payload struct is on the
  user side; the kernel only sees an opaque pointer through the ioctl
  arg).

libndbg only uses BAR0 (bar=0). The `bar` parameter is validated by
`(bar & 0xFD) == 0` in `ndl_bar_read`, which permits `bar ∈ {0, 2}`
only; bar=2 would address an alternate BAR (HBM aperture, used by the
mmap path for bulk tensor copies, not by CSR reads).

## `ndrv_csr_read` — single 32-bit register

```c
__int64 ndrv_csr_read(ndbg_backend_t *backend, uint32_t device_index,
                      uint32_t *out, ndbg_csr_t csr)
{
  ndl_device_t *dev = backend->private_data[device_index + 1];
  if (!dev) return 1;

  uint64_t bar_address[2];
  bar_address[0] =
      dev->csr_base[0]                    // BAR0 base mapped by kernel
    + csr.offset                          // CSR offset within bundle
    + csr.bundle.offset                   // bundle base offset within block
    + csr.bundle.block.bar_offset         // block's BAR0 byte offset
    + csr.bundle.block.device_address     // full device addr of block
    + csr.bundle.index * csr.bundle.size  // bundle replica index
    - csr.bundle.block.address_map_base;  // subtract map-base normalisation

  return ndl_bar_read(dev, /*bar=*/0, bar_address, /*count=*/1, out) != 0
       ? 0x13 : 0;   // 0x13 = NDBG_CSR_UNABLE_TO_READ (=19)
}
```

`ndl_bar_read` packages the address array and forwards to the kernel:

```c
int ndl_bar_read(ndl_device_t *dev, uint8_t bar,
                 uint64_t *addresses, uint32_t count, uint32_t *buffer)
{
  if ((bar & 0xFD) != 0) return -22;             // only bar=0 or 2
  int fd = *(int *)dev->context;                  // /dev/neuronN fd
  struct {
    uint8_t   bar;
    uint64_t *addresses;
    uint32_t  count;
    uint32_t *buffer;
  } payload = { bar, addresses, count, buffer };
  return ioctl(fd, 0x80084E0B, &payload, count, buffer);
}
```

## `ndrv_csr_read_bundle` — batch reads

For a full bundle dump, the library issues batched ioctls in chunks of
up to 100 dwords (the on-stack scratch holds `uint64_t v17[102]`):

```c
__int64 ndrv_csr_read_bundle(ndbg_backend_t *backend,
                             uint32_t device_index,
                             uint32_t **out, ndbg_csr_bundle_t bundle)
{
  ndl_device_t *dev = backend->private_data[device_index + 1];
  if (!dev) return 1;

  uint32_t total_dwords = bundle.size >> 2;
  uint32_t *values = (*out)
    ? realloc(*out, 4ULL * total_dwords)
    : malloc(4ULL * total_dwords);
  *out = values;

  uint64_t base =
      dev->csr_base[0]
    + bundle.size * bundle.index
    + bundle.offset
    + bundle.block.bar_offset
    + bundle.block.device_address
    - bundle.block.address_map_base;

  uint64_t addresses[102];
  for (uint32_t i = 0; i < total_dwords; ) {
    uint32_t chunk = (total_dwords - i > 100) ? 100 : (total_dwords - i);
    for (uint32_t j = 0; j < chunk; ++j)
      addresses[j] = base + 4 * (i + j);
    if (ndl_bar_read(dev, 0, addresses, chunk, &values[i]) != 0)
      return 21;                              // NDBG_CSR_BUNDLE_UNABLE_TO_READ
    i += 100;                                  // note: increment by 100 even if chunk < 100
                                                // (loop ends on i >= total_dwords)
  }
  return 0;
}
```

The 100-dword-per-ioctl batching minimises ioctl overhead — one
syscall per 400 bytes of register data — while staying well below the
typical kernel-side MMIO budget per syscall.

## Why IOCTL and not MMAP

libndl supports both. `ndl_mmap_bar_region` (and the underlying
`ndl_mmap_bar_region_`) wraps `ioctl(fd, 0xC0204E70, &{block, block_id,
resource, ...})` to ask the kernel for a file-offset token, then calls
`mmap()` to expose the corresponding BAR region directly in the
process's address space:

```c
int ndl_mmap_bar_region_(ndl_device_t *device, neuron_dm_block_type block,
                          uint32_t block_id, neuron_dm_resource_type resource,
                          void **va, uint64_t *size, uint64_t *offset)
{
  struct { neuron_dm_block_type block; uint32_t block_id;
           neuron_dm_resource_type resource; size_t len[2]; } req = {block, block_id, resource};
  if (ndl_device_ioctl(device->device_index, 0xC0204E70, &req) != 0)
    return -1;
  void *p = mmap(NULL, req.len[1], PROT_READ | (resource != NEURON_DM_RESOURCE_ALL ? PROT_WRITE : 0),
                 MAP_SHARED, fd, req.len[0]);
  *va = p; *size = req.len[1];
  if (offset) *offset = req.len[0];
  return p == MAP_FAILED ? -1 : 0;
}
```

This path is used by `ndl_open_device` to grab the HBM aperture
(`NEURON_DM_BLOCK_HBM`, allocated with `ndl_memory_alloc(...,
size=0x200000, ...)` per slot, 8 slots per device) for fast tensor
copy-in/copy-out. It is NOT used for CSR reads.

libndbg routes ALL CSR reads through ioctl — the reasoning is operational:

- Every CSR read is logged/audited/throttled by the kernel module,
  enabling debug-time visibility into register access patterns.
- Per-ioctl batching gives clear demarcation between logical reads
  ("dump bundle X") so the kernel can prioritise / queue / drop
  individual access bursts.
- The address-decoding chain on the libndbg side relies on subtracting
  `address_map_base` from `device_address`; the kernel module is the
  natural party to verify that the resulting BAR0 byte offset belongs
  to the requested block's window. An mmap-based path would let
  arbitrary BAR0 offsets be poked from userspace, defeating the
  per-block window verification.

## Read-modify-write paths

The library exposes:

| function                       | semantics                                            |
|--------------------------------|------------------------------------------------------|
| `ndbg_csr_read`                | 1 reg → 1 uint32                                     |
| `ndbg_csr_read_hi_lo`          | 2 regs (hi/lo) → 1 uint64                            |
| `ndbg_csr_write`               | 1 uint32 → 1 reg                                     |
| `ndbg_csr_write_hi_lo`         | 1 uint64 → 2 regs (hi/lo)                            |
| `ndbg_csr_bundle_read`         | bundle.size/4 dwords → uint32 array                  |
| `ndbg_csr_bundle_write`        | uint32 array → bundle.size/4 dwords                  |
| `ndbg_csr_printf`              | read 1 reg + format with field-decoder for op.       |
| `ndbg_read_tensor_impl`        | mapped-memory + CSR-mediated tensor read             |
| `ndbg_write_tensor_impl`       | mapped-memory + CSR-mediated tensor write            |

The hi/lo variants pair a `_LSB`/`_HI` and `_MSB`/`_LO` register lookup
(e.g. `NDBG_CSR_ACT_NX_SPC_LSB` and `NDBG_CSR_ACT_NX_SPC_MSB`) so the
PC, performance counters and other 64-bit values can be read in one
call.

## Device-info bootstrap

The arithmetic above relies on `dev->csr_base[0]` having been filled
in by `ndl_open_device`. That function:

1. Opens `/dev/neuron<device_index>`.
2. Allocates a 632-byte `ndl_device_t` via `calloc(0x27C, 1)`.
3. Issues `ioctl(fd, 0x80084E03, &device_info)` where `device_info` is
   a `neuron_ioctl_device_info` struct populated by the kernel with
   `bar_address[0..1]`, `bar_size[0..1]`, `architecture`, `revision`,
   `connected_device_count`, `connected_devices[64]`.
4. Copies those fields into the local `ndl_device_t`, stores the fd at
   `dev->context[0..3]`, and returns.

`bar_address[0]` becomes `dev->csr_base[0]` — the absolute byte address
in some address space that the kernel has agreed to use as the base
of "BAR0" for the ioctls. The kernel must use the same conversion when
it receives the BAR-read ioctl: take the user-supplied address,
subtract `csr_base[0]`, and the residual is the BAR0 byte offset to
MMIO. (Equivalently: the userland address is `csr_base[0] + bar_byte_offset`
and the kernel just performs `pci_io = bar_byte_offset` after stripping
the `csr_base[0]` prefix.)

## Error codes

The 47-entry `ndbg_error_code_t` enum is the union of all return values
from the read/write paths. The ones reachable from the CSR ioctl path:

```
 0  NDBG_SUCCESS
 1  NDBG_DEVICE_INDEX_OUT_OF_BOUNDS
 7  NDBG_CSR_UNRECOGNIZED
 8  NDBG_CSR_BUNDLE_UNRECOGNIZED
 9  NDBG_CSR_BLOCK_UNRECOGNIZED
14  NDBG_UNRECOGNIZED_ARCHITECTURE
19  NDBG_CSR_UNABLE_TO_READ      (= 0x13, from ndl_bar_read failure)
20  NDBG_CSR_UNABLE_TO_WRITE
21  NDBG_CSR_BUNDLE_UNABLE_TO_READ
22  NDBG_CSR_BUNDLE_UNABLE_TO_WRITE
29  NDBG_INEXACT_MATCH
30  NDBG_CSR_INCOMPATIBLE_WITH_BLOCK
31  NDBG_CSR_BUNDLE_INDEX_OUT_OF_BOUNDS
```
