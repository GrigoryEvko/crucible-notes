# Register Catalog (per-arch info() switches)

The `ndbg_csr_block_<arch>_info` functions are the actual register
database. Each is a single switch with one case per `ndbg_csr_block_name_t`
value that the arch recognises, returning a constant-literal
`ndbg_csr_block_t` (32 bytes):

```c
typedef struct {
  uint64_t bar_offset;        // BAR0 byte offset where this block starts
  uint64_t address_map_base;  // device "memory map" base addr (large region)
  uint64_t device_address;    // fully-qualified device byte address of block
  uint64_t size;              // block window size, typ. 0x1000–0x100000
} ndbg_csr_block_t;
```

## Sizes

| function                                | cases | total bytes |
|-----------------------------------------|------:|------------:|
| `ndbg_csr_block_cayman_info`            | 2,356 |     100,496 |
| `ndbg_csr_block_mariana_info`           | 2,630 |       ~100 K |
| `ndbg_csr_block_sunda_info`             |   465 |        ~20 K |

i.e. *the cayman backend recognises 2,356 distinct register blocks,
mariana 2,630, sunda 465*. The global enum
`ndbg_csr_block_name_t` declares 3,265 enumerators because it must
accommodate the union of all three backends.

Unrecognised `block_name` values return `NDBG_CSR_BLOCK_UNRECOGNIZED`
(error code 9). The companion `ndbg_csr_block_name_to_string` function
covers all 3,265 names regardless of arch — it is the pretty-printer
for any backend's enum value.

## Sample entries (cayman)

```c
case NDBG_CSR_BLOCK_TOP_SP_0_TPB_SP_LOCAL_REG:
  out->size              = 0x10000;
  out->bar_offset        = 0xF0000000;
  out->address_map_base  = 0x8280000000;
  out->device_address    = 0x8280260000;
  return NDBG_SUCCESS;

case NDBG_CSR_BLOCK_TPB_0_ACT_LOCAL_REG:
  out->size              = 0x10000;
  out->bar_offset        = 0xD2000000;
  out->address_map_base  = 0x2802000000;
  out->device_address    = 0x2802460000;
  return NDBG_SUCCESS;

case NDBG_CSR_BLOCK_TPB_0_DVE_LOCAL_REG:
  out->size              = 0x10000;
  out->bar_offset        = 0xD2000000;       // same bar_offset as ACT
  out->address_map_base  = 0x2802000000;
  out->device_address    = 0x2802B60000;     // different device_address within
  return NDBG_SUCCESS;
```

The window size is consistently 0x10000 (64 KiB) for the TPB local
register blocks and 0x1000 (4 KiB) for the smaller QoS / ELA / NTS
blocks under the `APB_IO` and `APB_SE` clusters:

```c
case NDBG_CSR_BLOCK_APB_IO_0_USER_FIS_IO_D2D_SUBSYS_0_DEBUG_FIS_0_INTERNAL_ELA:
  out->bar_offset        = 0;                       // computed via map_base only
  out->size              = 4096;
  out->address_map_base  = 0x8000000000;
  out->device_address    = 0x800D975000;
  return NDBG_SUCCESS;
```

Cases where `bar_offset = 0` indicate that the entire block address is
derived from `device_address - address_map_base + ndl_device.csr_base[0]`
i.e. the BAR0 base of the kernel-side mapping plus the device-address
delta. Cases where `bar_offset != 0` indicate the block belongs to a
secondary BAR window mapped at the given fixed BAR0 byte offset
(typically 4 GiB-aligned).

## Top-level layout: which block lives where

The cayman backend uses two memory-map roots:

- `0x80_0000_0000` for the IO complex (APB_IO_*) and its sub-blocks
  (PEB, USER_FIS, NTS_QOS, ERRTRIG_*, ELA, etc.)
- `0x82_0000_0000` for `TOP_SP_<i>_*` (i = 0..5) — the CC-core local
  registers
- A per-NC root in 0x28_0..0x78_0 range for each of the 8 NCs
  (`TPB_<i>_*`)

Multiple blocks share an `address_map_base` and a `bar_offset` (the
window they're carved out of) but differ in their `device_address` (the
fully-qualified offset within that window). The arithmetic at read
time subtracts `address_map_base` so the residual is the in-window
offset; see [Read / Write Vector](read-write-vector.md).

## Bundles and CSRs

Beneath each block, `ndbg_csr_bundle_<arch>_info` returns a 64-byte
`ndbg_csr_bundle_t` that encodes the register-group offset, size and
replication count:

```c
typedef struct {
  ndbg_csr_block_t block;     // a copy of the block info
  uint64_t size;              // size in bytes of one bundle instance
  uint64_t length;            // # of replicated bundle instances
  uint64_t offset;            // bundle base offset within block
  uint64_t index;             // caller-supplied index, 0 ≤ index < length
} ndbg_csr_bundle_t;
```

Within `tpb` (the per-NC TPB block-family), the cayman bundle info
function returns:

| `ndbg_csr_bundle_name_t`        | offset | size  | length |
|---------------------------------|-------:|------:|-------:|
| `NDBG_CSR_BUNDLE_PE_SEQUENCER`  |     0  |  256  |     1  |
| `NDBG_CSR_BUNDLE_POOL_SEQUENCER`|   256  |  256  |     1  |
| `NDBG_CSR_BUNDLE_ACT_SEQUENCER` |   512  |  256  |     1  |
| `NDBG_CSR_BUNDLE_DVE_SEQUENCER` |   768  |  256  |     1  |
| `NDBG_CSR_BUNDLE_EVENTS_SEMAPHORES` | 2048 | 512 |     1  |
| `NDBG_CSR_BUNDLE_NOTIFIC`       |  2560  |  256  |     1  |
| `NDBG_CSR_BUNDLE_MISC`          |  2816  |  256  |     1  |
| `NDBG_CSR_BUNDLE_PERFORMANCE_COUNTER` | 3072 | 512 |     1  |
| `NDBG_CSR_BUNDLE_INTC_BYPASS`   |  3584  |   32  |     1  |

That single bundle table covers the 4 KiB TPB register window
(0–4095 = sum of all the (offset, size) ranges with a gap).

Beneath each bundle, the per-CSR-field offset is filled by
`ndbg_csr_<arch>_info_<blockfamily>_<bundle>`. A representative
function — `ndbg_csr_cayman_info_intc_1grp_msix_unit_ctrl(csr, out)` — is
a 166-byte 15-basic-block switch:

```c
case NDBG_CSR_INT_MASK_GRP:        out->offset = 16; return NDBG_SUCCESS;
case NDBG_CSR_INT_CAUSE_GRP:       out->offset = 0;  return NDBG_SUCCESS;
case NDBG_CSR_INT_CAUSE_SET_GRP:   out->offset = 8;  return NDBG_SUCCESS;
case NDBG_CSR_INT_STATUS_GRP:      out->offset = 32; return NDBG_SUCCESS;
case NDBG_CSR_INT_CDC_BYPASS_GRP:  out->offset = 36; return NDBG_SUCCESS;
case NDBG_CSR_INT_CONTROL_GRP:     out->offset = 40; return NDBG_SUCCESS;
case NDBG_CSR_INT_ERROR_MSK_GRP:   out->offset = 44; return NDBG_SUCCESS;
case NDBG_CSR_INT_ABORT_MSK_GRP:   out->offset = 48; return NDBG_SUCCESS;
case NDBG_CSR_INT_FATAL_MSK_GRP:   out->offset = 52; return NDBG_SUCCESS;
case NDBG_CSR_INT_LOG_MSK_GRP:     out->offset = 56; return NDBG_SUCCESS;
case NDBG_CSR_INT_POSEDGE_GRP:     out->offset = 60; return NDBG_SUCCESS;
case NDBG_CSR_INT_MASK_CLEAR_GRP:  out->offset = 24; return NDBG_SUCCESS;
default:                          return NDBG_CSR_UNRECOGNIZED;
```

This pattern repeats across dozens of `_info_<blockfamily>_<bundle>`
leaf functions: each is a small switch returning the byte offset of a
specific named register within a specific named bundle.

## Block-family list (cayman info exports)

Trimmed list of cayman block-family info leaves (each corresponds to
one named member of `ndbg_csr_bundle_cayman_info`'s outer dispatch):

```
cxela500, intc_1grp_msix_unit, intc_4grp_msix_unit, misc_ram_model,
notific_10_queue, notific_1_queue, papb_bcast, qos_host_visible,
rdm_model, tdma_model, top_sp_ram, tpb,
tpb_arr_seq_cluster_host_visible, tpb_arr_seq_top_host_visible,
tpb_sbuf_cluster, tpb_sbuf_pool_act, tpb_xt_local_reg,
udma_gen, udma_gen_ex, udma_m2s, udma_s2m
```

Mariana adds: `cce` (Compute Core Engine), `hbm_xbar_cfg`,
`preproc_user`, `top_sp_misc_user`, `xtensa_nx`, `xtensa_q7`.

Sunda is much shorter: only 17 block families, missing `cxela500`,
`papb_bcast`, `intc_4grp_msix_unit`, `notific_1_queue`,
`tpb_xt_local_reg`, `tpb_arr_seq_*` (kept), `top_sp_ram` (renamed),
and adds `tpb_nx_local_reg`.

## The `ndbg_csr_*_from_device_address` reverse path

`ndbg_csr_cayman_from_device_address(backend, device_address, out)` is
the inverse of the lookup chain: given a raw device byte address, it
returns a fully-populated `ndbg_csr_t`. Implemented as a 108-basic-block
function (2,531 bytes) that successively narrows the address by
subtracting candidate `address_map_base` values:

```c
v3 = device_address - 0x8000000000ULL;
if (v3 <= 0x1FFFFFFF) {
  out->bundle.block.bar_offset       = 0;
  out->bundle.block.address_map_base = 0x8000000000ULL;
  out->bundle.block.device_address   = 0x8000000000ULL;
  out->bundle.block.size             = 0x20000000;
  out->offset                        = v3;
  return NDBG_SUCCESS;
}
v3 = device_address - 0x808000000000ULL;
if (v3 <= 0x1FFFFFFF) { ... bar_offset = 0x40000000 ... }
v4 = device_address - 0x1000000000ULL;
if (v4 <= 0x0C7FFFFF) { ... bar_offset = 0x80000000 ... }
...
```

Useful when an external trace (e.g. a PCIe analyser) captures a raw
device address and the operator wants to know which CSR was touched.

## Per-arch coverage tally

```
ndbg_csr_block_name_t enum   3,265 total
  cayman info() covers       2,356
  mariana info() covers      2,630
  sunda info() covers          465
ndbg_csr_bundle_name_t enum    140 total
ndbg_csr_name_t enum         1,182 total
```

Of the 140 bundle names, several are clearly arch-conditional based on
naming alone:
- `NDBG_CSR_BUNDLE_SUNDA` (76) — sunda only
- `NDBG_CSR_BUNDLE_VMPR_V4` (86) — mariana only
- `NDBG_CSR_BUNDLE_CCE_FMA_CFG` (106), `NDBG_CSR_BUNDLE_CCE_FMA_CONST` (107),
  `NDBG_CSR_BUNDLE_CCE_BUFFER_ACCESS` (105) — mariana only (CCE block family)
- `NDBG_CSR_BUNDLE_HW_DECODE` (93), `NDBG_CSR_BUNDLE_ROBERT` (97) — mariana only
- `NDBG_CSR_BUNDLE_PREPROC_AXCACHE` (125), `NDBG_CSR_BUNDLE_PREPROC_AXUSER` (126) — mariana only
- `NDBG_CSR_BUNDLE_NX` (95) — cayman/mariana (sunda uses
  `NDBG_CSR_BUNDLE_TPB_NX_LOCAL_REGS` (80) instead)
- `NDBG_CSR_BUNDLE_Q7` (96) — cayman/mariana
- `NDBG_CSR_BUNDLE_INTC_BYPASS` (94) — cayman/mariana
