# Register Grouping: block → bundle → csr → index

libndbg names every 32-bit MMIO register through a four-level
hierarchy. The four levels correspond to four enum types and four
runtime lookup steps:

| level     | enum                    | width | role                                |
|-----------|-------------------------|------:|--------------------------------------|
| **block** | `ndbg_csr_block_name_t` | 3,265 | Physical address window on the BAR. One instance of one IP block. |
| **bundle**| `ndbg_csr_bundle_name_t`|   140 | Register-group category within the block (e.g. `M2S_Q`, `INTC_BYPASS`). |
| **csr**   | `ndbg_csr_name_t`       | 1,182 | Individual 32-bit register within the bundle. |
| **index** | `uint64_t`              |     ∞ | Replicated-bundle instance (0 ≤ index < bundle.length). |

## Each level adds an address component

The composition is multiplicative-additive:

```
final_BAR0_byte_offset =
      csr.offset                     // CSR field inside its bundle
    + csr.bundle.offset              // bundle base inside its block
    + csr.bundle.index * csr.bundle.size   // pick which replica of the bundle
    + (csr.bundle.block.bar_offset
       + csr.bundle.block.device_address
       - csr.bundle.block.address_map_base);    // block base on BAR0
```

The lookups themselves:

1. **block**: `ndbg_csr_block_info(backend, block_name, &out)`
   returns a constant 32-byte literal:
   `{bar_offset, address_map_base, device_address, size}`.

2. **bundle**: `ndbg_csr_bundle_info(backend, block_name, bundle_name,
   index, &out)` returns the 64-byte composite struct, copying the
   block info into `out->bundle.block` and adding `{size, length, offset,
   index}` to it. The `index` argument is bounds-checked against
   `length` and stored.

3. **csr**: `ndbg_csr_info(backend, block_name, bundle_name, csr_name,
   index, &out)` returns the 72-byte composite that embeds the bundle
   info and adds the inner `offset`.

## Why the three-level split

A real example clarifies. The block name
`APB_SE_0_SDMA_3_BCAST_UDMA_M2S` identifies one of 32 DMA engines per
NC × per side (SE_0 is one of 4 subsystems, SDMA_3 is one of 32 DMA
engines in that subsystem, BCAST_UDMA_M2S identifies the M2S broadcast
flavour of UDMA). On cayman this block has:

- `bar_offset = …` (large), `device_address = …` — the BAR0 byte
  address of this one DMA engine, ~128 KiB window.

Inside that window the M2S DMA has many register categories:

- `M2S_Q` — per-queue config (e.g. ring base, ring size, prod/cons
  pointers). The bundle here typically has `length = 32` (one per
  queue), `size = 256` bytes per queue.
- `M2S_RATE_LIMITER` — global rate-limiting. `length = 1`, `size = 64`.
- `M2S_COMP` — completion handling.
- `M2S_DWRR` — weighted round-robin scheduling.
- `M2S_FEATURE` — feature-enables.
- `M2S_STAT` — read-only statistics.
- `M2S_STREAM_RATE_LIMITER`, `M2S_RD`, etc.

Within `M2S_Q`, individual registers (CSRs) are e.g. `PROD_CONF`,
`HEAD_CONS`, `BASE_LO`, `BASE_HI`, `RING_SIZE`, `TAIL_PTR`, etc.

The 4-step lookup means an operator types one short string per level,
gets autocomplete on each, and the library composes the final BAR0
address. No giant flat name table of `(block, bundle, csr)` triples
needs to live in memory: the three orthogonal indexes are kept
separate, and the lookup is a constant-time enum-dispatch at each
level.

## Bundle replication

The `bundle.length × bundle.size` window is laid out contiguously in
the block. For `M2S_Q` (length=32, size=256) the queue 0 registers
start at `bundle.offset + 0*256`, queue 1 at `bundle.offset + 1*256`,
…, queue 31 at `bundle.offset + 31*256`. The 4-byte index argument to
`ndbg_csr_info(...)` picks which replica:

```c
csr.offset = csr.bundle.offset + csr.bundle.size * csr.bundle.index + field_offset;
```

This is why every bundle info function returns
`{size, length, offset}` instead of an array of offsets:
replication is regular, so one stride+count suffices.

For most bundles `length = 1` (e.g. all the `_SEQUENCER` bundles in
the cayman `tpb` info table). For dense per-queue/per-engine bundles,
`length` ranges up to 32 (matching the 32 SDMA queues).

## Concrete: cayman `tpb` block

Decoded from `ndbg_csr_bundle_cayman_info_tpb`:

| bundle name                  | offset | size | length |
|------------------------------|-------:|-----:|-------:|
| `PE_SEQUENCER`               |     0  |  256 |     1  |
| `POOL_SEQUENCER`             |   256  |  256 |     1  |
| `ACT_SEQUENCER`              |   512  |  256 |     1  |
| `DVE_SEQUENCER`              |   768  |  256 |     1  |
| `EVENTS_SEMAPHORES`          |  2048  |  512 |     1  |
| `NOTIFIC`                    |  2560  |  256 |     1  |
| `MISC`                       |  2816  |  256 |     1  |
| `PERFORMANCE_COUNTER`        |  3072  |  512 |     1  |
| `INTC_BYPASS`                |  3584  |   32 |     1  |

i.e. the `TPB_<i>_<engine>_LOCAL_REG` blocks (one per NC × per engine)
hold 9 bundles totalling about 4 KiB, with a 480-byte gap between
`POE_SEQUENCER + DVE_SEQUENCER` and `EVENTS_SEMAPHORES` and another
gap between `INTC_BYPASS+32 = 3616` and the 4 KiB end. The unrelated
register windows (`PE_NX_LOCAL_REGS`, `XT_LOCAL_REG` etc.) are in
SEPARATE blocks, not in the `tpb` block.

## Concrete: cayman `intc_1grp_msix_unit` block

Three bundles under one block:

| bundle name                  | offset |        size | length |
|------------------------------|-------:|------------:|-------:|
| `CTRL` (via `ctrl` sub-info) |     0  |          64 |     1  |
| `MSIX_VECTOR_TABLE_SPACE`    |    64  |  2048+      |     1  |
| `PBA` (Pending-Bit Array)    |   ...  |       128+  |     1  |

The `CTRL` bundle is decoded by
`ndbg_csr_cayman_info_intc_1grp_msix_unit_ctrl` (decompiled in
[Register Catalog](register-catalog.md)) and holds the standard
intc/msix-controller registers (INT_MASK_GRP, INT_CAUSE_GRP,
INT_STATUS_GRP, etc., 12 CSRs at fixed byte offsets in a 64-byte page).

## ndbg_csr_t physical layout

```c
typedef struct {
  uint64_t bar_offset;        // copy of block.bar_offset
  uint64_t address_map_base;  // copy of block.address_map_base
  uint64_t device_address;    // copy of block.device_address
  uint64_t size;              // block size
} ndbg_csr_block_t;            // 32 bytes

typedef struct {
  ndbg_csr_block_t block;     // embedded copy of block info
  uint64_t size;              // bundle replica size
  uint64_t length;            // # of replicas
  uint64_t offset;            // bundle start within block
  uint64_t index;             // chosen replica
} ndbg_csr_bundle_t;           // 64 bytes

typedef struct {
  ndbg_csr_bundle_t bundle;   // embedded copy of bundle info
  uint64_t offset;            // CSR field offset within bundle
} ndbg_csr_t;                  // 72 bytes
```

Each lookup struct embeds the previous level's struct rather than
pointing to it, so the read path needs no pointer chasing — the
descriptor is value-copied through the API.

## Engine-state helpers compose the lookup

The engine-state probes are good examples of multi-level
composition. `ndbg_<arch>_engine_run_state` does:

```c
const char *engine_str = ndbg_nc_engine_string(engine);  // e.g. "PE"
char block_str[64];
snprintf(block_str, 64, "TPB_%d_%s_LOCAL_REG", index, engine_str);
ndbg_csr_block_name_t block_name;
ndbg_csr_block_resolve_name(backend, block_str,
                            strlen(block_str), &block_name);
return ndbg_csr_info(backend, block_name,
                     NDBG_CSR_BUNDLE_NX,           // cayman/mariana
                     NDBG_CSR_RUN_STATE,           // = 674
                     0,                             // index
                     out);                          // populates ndbg_csr_t
```

I.e. one string lookup (block) + two enum-id passes (bundle, csr) +
one index. The caller can then `ndbg_csr_read(backend, device_index,
&value, out)` to fetch the actual register value.

The CC-core PC variant is more elaborate — it builds two strings
(`%s_NX_SPC_MSB` and `%s_NX_SPC_LSB`) and resolves them through the
CSR-name resolver, then composes both into a 64-bit PC. See the
`ndbg_cayman_engine_program_counter` decompilation for the full
sequence.
