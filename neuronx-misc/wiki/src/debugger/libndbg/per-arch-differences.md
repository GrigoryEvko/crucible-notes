# Per-Arch CSR Differences (V2 / V3 / V4)

The three backends shipped in libndbg cover three NeuronCore silicon
generations. Each backend's geometry, block namespace and CSR layout
differ along several axes. The codename / hardware-version mapping:

| codename | hardware tag         | covered in libndbg |
|----------|----------------------|---------------------|
| sunda    | `NDBG_HARDWARE_V2`   | yes                 |
| cayman   | `NDBG_HARDWARE_V3`   | yes                 |
| mariana  | `NDBG_HARDWARE_V4`   | yes                 |

`NDBG_HARDWARE_V1` exists in the enum but has no backend in this
libndbg build, suggesting a now-retired first-generation Inferentia
class.

## Headline geometry deltas

(From `ndbg_<arch>_init` setting `ndbg_backend_common_t common_data`.)

| dimension                          | sunda (V2) | cayman (V3) | mariana (V4) |
|------------------------------------|-----------:|------------:|-------------:|
| `num_ncs_per_device`               |          2 |           8 |            8 |
| `num_dma_engines_per_nc`           |         16 |          16 |           16 |
| `num_top_dma_engines_per_device`   |          2 |           4 |            4 |
| `num_cc_cores_per_device`          |          6 |          20 |           20 |
| `num_bytes_per_descriptor`         |         16 |          64 |           64 |
| `instruction_size`                 |         16 |          64 |           64 |
| `instruction_alignment`            |        512 |          32 |           32 |
| `num_nc_semaphores`                |        256 |          16 |           16 |
| `num_nc_events`                    |        256 |         256 |          256 |
| `num_sbufs`                        |          2 |           8 |            8 |
| `sram_size`                        |    768 KiB |      32 MiB |       32 MiB |
| `num_drams`                        |          2 |           4 |            4 |
| HBM region per DRAM                |     16 GiB |       4 GiB |        4 GiB |

Sunda is the "small" silicon — 2 NCs, 6 CC cores, 2 HBM banks at 16 GiB
each, narrow 16-byte descriptors. Cayman and mariana are nearly
geometrically identical at the common_data level — they share NC count,
descriptor size, SRAM size, HBM topology — and differ primarily in
their CSR catalog.

## Block-family deltas

Cross-arch block families (present in all three):

```
intc_1grp_msix_unit, intc_4grp_msix_unit, misc_ram_model,
notific_10_queue, qos_host_visible, rdm_model, tdma_model,
top_sp_ram, tpb, tpb_arr_seq_cluster_host_visible,
tpb_arr_seq_top_host_visible, tpb_sbuf_cluster, tpb_sbuf_pool_act,
udma_gen, udma_gen_ex, udma_m2s, udma_s2m
```

Cayman-only block families:

- `cxela500` — the ELA (Embedded Logic Analyzer) IP block, model 500.
- `papb_bcast` — PAPB broadcast control.
- `notific_1_queue` — single-queue notification controller (cayman
  has both 1-queue and 10-queue notification blocks; mariana keeps
  both; sunda has only 10-queue).
- `tpb_xt_local_reg` — per-TPB XT-core local registers.

Mariana-only block families (introduced in V4):

- `cce` — Compute Core Engine block family (NDBG_CSR_BUNDLE_CCE_FMA_CFG,
  CCE_FMA_CONST, CCE_BUFFER_ACCESS).
- `hbm_xbar_cfg` — HBM crossbar configuration (the
  `APB_IO_0_HBM_XBAR_<i>` block names appear only in mariana).
- `preproc_user` — preprocessor user-mode registers
  (NDBG_CSR_BUNDLE_PREPROC_AXCACHE, PREPROC_AXUSER).
- `top_sp_misc_user` — TOP_SP miscellaneous user registers.
- `xtensa_nx`, `xtensa_q7` — Cadence Xtensa core registers. Mariana
  exposes the NX and Q7 Xtensa cores as first-class register blocks;
  cayman's narrower `tpb_xt_local_reg` block is the V3 antecedent.

Sunda-only block family:

- `tpb_nx_local_reg` — sunda-specific TPB NX local registers (the
  V3/V4 backends pull these into the `NDBG_CSR_BUNDLE_NX` bundle on a
  per-TPB engine block, but sunda kept them as a dedicated block
  family).

## Block-count totals

| function                                | switch cases | size on disk |
|-----------------------------------------|-------------:|-------------:|
| `ndbg_csr_block_cayman_info`            |        2,356 |     100,496 B |
| `ndbg_csr_block_mariana_info`           |        2,630 |       ~100 K |
| `ndbg_csr_block_sunda_info`             |          465 |        ~20 K |

Mariana's namespace is 12% larger than cayman's; sunda's is roughly
1/5 of either. The 3,265-entry `ndbg_csr_block_name_t` enum is the
union of all three, so the per-arch info() coverage is strictly less
than 3,265.

## Bundle deltas

Of the 140 `ndbg_csr_bundle_name_t` enumerators, the arch-conditional
ones detected by symbol names:

| bundle name                              | id | arch                  |
|------------------------------------------|---:|-----------------------|
| `NDBG_CSR_BUNDLE_NX`                     | 95 | cayman / mariana      |
| `NDBG_CSR_BUNDLE_TPB_NX_LOCAL_REGS`      | 80 | sunda                 |
| `NDBG_CSR_BUNDLE_Q7`                     | 96 | cayman / mariana      |
| `NDBG_CSR_BUNDLE_INTC_BYPASS`            | 94 | cayman / mariana      |
| `NDBG_CSR_BUNDLE_SUNDA`                  | 76 | sunda                 |
| `NDBG_CSR_BUNDLE_VMPR_V4`                | 86 | mariana               |
| `NDBG_CSR_BUNDLE_HW_DECODE`              | 93 | mariana               |
| `NDBG_CSR_BUNDLE_ROBERT`                 | 97 | mariana               |
| `NDBG_CSR_BUNDLE_CCE_BUFFER_ACCESS`      |105 | mariana               |
| `NDBG_CSR_BUNDLE_CCE_FMA_CFG`            |106 | mariana               |
| `NDBG_CSR_BUNDLE_CCE_FMA_CONST`          |107 | mariana               |
| `NDBG_CSR_BUNDLE_PREPROC_AXCACHE`        |125 | mariana               |
| `NDBG_CSR_BUNDLE_PREPROC_AXUSER`         |126 | mariana               |
| `NDBG_CSR_BUNDLE_CORESIGHT_REGISTERS`    |108 | mariana / sunda (xtensa / nx cores) |
| `NDBG_CSR_BUNDLE_TRAX_REGISTERS`         |136 | mariana / sunda       |
| `NDBG_CSR_BUNDLE_OCD_REGISTERS`          |122 | mariana / sunda       |
| `NDBG_CSR_BUNDLE_PERFORMANCE_MONITOR_REGISTERS` |124 | mariana / sunda |
| `NDBG_CSR_BUNDLE_MISCELLANEOUS_REGISTERS` |117| mariana / sunda      |

## Engine-state bundle choice

| backend | run_state CSR location                          |
|---------|--------------------------------------------------|
| cayman  | `(NDBG_CSR_BUNDLE_NX, NDBG_CSR_RUN_STATE=674)` (= bundle id 95) |
| mariana | `(NDBG_CSR_BUNDLE_NX, NDBG_CSR_RUN_STATE=674)` (= bundle id 95) |
| sunda   | `(NDBG_CSR_BUNDLE_TPB_NX_LOCAL_REGS, NDBG_CSR_RUN_STATE=674)` (= bundle id 80) |

Same CSR field; different bundle. Sunda's V2 layout had the NX
sub-block hanging off a TPB-local-registers bundle; V3/V4 promoted
those NX registers to their own bundle. Either way, the CSR field
name (`NDBG_CSR_RUN_STATE`) is shared.

## CC-core bounds checks

The cayman / mariana CC-core program-counter accessor checks
`if (index - 8 <= 1 || index - 18 <= 1) return NDBG_CC_CORE_INDEX_OUT_OF_BOUNDS;`
i.e. CC-core indices 8, 9, 18, 19 are *not* valid CC cores on V3/V4 (the
20-CC layout is `0..7` for the first cluster and `10..17` for the second,
with indices 8, 9, 18, 19 left as "holes" in the encoding).

Sunda's CC-core path has no such guard — its 6 CC cores are
contiguous at indices 0..5.

The same gap is enforced in `*_engine_run_state` and
`*_engine_start_address` on cayman / mariana.

## Detectable arch-conditional strings

Among the source-string evidence embedded in the binary:

- `"Configures max outstanding data reads when axi_m2s_mla_cfg_outstanding_max_enable
  is set. Max value is 287 in Cayman - writing a higher value in this register
  will set it to 287."`
- `"Configures max outstanding data reads when axi_m2s_mla_cfg_outstanding_max_enable
  is set. Max value is 511 in Mariana - writing a higher value in this register
  will set it to 511."`
- `"Chicken - Enable Data Tail Pointer for enhanced prefetch, disabling all other
  rate-limiting per queue - introduced in Cayman"`
- `"Chicken - Enable independent Descriptor Tail Pointer and Data Tail Pointer per
  queue - introduced in Cayman"`
- `"Chicken - Enable programming of maximum outstanding read transactions,
  enabling register enhanced_ostand_cfg - introduced in Cayman"`
- `"Enable high priority per M2S queue - introduced in Cayman"`
- `"Block-size to fetch into Sunda IRAM (default is 8KB)"`
- `"bit[31:0] => Sunda memory window<i> bits [63:31]"` (×8)
- `"bit[31:24] => Sunda memory window<i> bits [31:24] bit[23:0] => 24b0"` (×8)
- `"lfsr_cayman_seeding: 0x%x"`

These are the only register-level field descriptions surfaced in
strings — they hint at:

- Sunda has explicit 64-bit address-window programming via a pair of
  high-bit / low-bit registers per window (8 windows total), a legacy
  V2 mechanism for full 64-bit MMIO addressing before the V3/V4
  unified memory map.
- Cayman introduces multiple "chicken" prefetch / rate-limiter
  enables that mariana inherits without renaming.
- Mariana doubles cayman's max-outstanding-reads counter (287 → 511),
  matching the wider tensor engine.

## Per-arch source-path strings

The source paths visible in the binary cleanly map to arch directories:

```
/opt/brazil-pkg-cache/packages/KaenaDebuggerLib/KaenaDebuggerLib-2.29.11.0/AL2_x86_64/generic-flavor/src/src/cayman/dma.c
/opt/brazil-pkg-cache/packages/KaenaDebuggerLib/KaenaDebuggerLib-2.29.11.0/AL2_x86_64/generic-flavor/src/src/sunda/dma.c
/opt/brazil-pkg-cache/packages/KaenaDebuggerLib/KaenaDebuggerLib-2.29.11.0/AL2_x86_64/generic-flavor/src/src/dma.c          (shared)
/opt/brazil-pkg-cache/packages/KaenaDebuggerLib/KaenaDebuggerLib-2.29.11.0/AL2_x86_64/generic-flavor/src/src/ndrv/engine.c
/opt/brazil-pkg-cache/packages/KaenaDebuggerLib/KaenaDebuggerLib-2.29.11.0/AL2_x86_64/generic-flavor/src/src/debug_info.cpp
```

No `mariana/` source path appears in the strings even though
`ndbg_mariana_*` symbols exist — suggesting mariana's per-arch code
was either inlined into the shared `dma.c` (less likely) or the
mariana-arch source directory was added late and is named differently
from the codename in the on-disk source layout. The function symbols
themselves use `mariana` throughout, so the externally observable API
is consistent regardless of source-tree naming.

## Engine enum (cross-arch shared)

`ndbg_nc_engine_t` (6 enumerators) is shared by all three backends:

| value | name                       | string (`ndbg_nc_engine_string`) |
|------:|----------------------------|----------------------------------|
|     0 | `NDBG_NC_ENGINE_TENSOR`    | `"PE"`                           |
|     1 | `NDBG_NC_ENGINE_GPSIMD`    | `"POOL"`                         |
|     2 | `NDBG_NC_ENGINE_SCALAR`    | `"ACT"`                          |
|     3 | `NDBG_NC_ENGINE_VECTOR`    | `"DVE"`                          |
|     4 | `NDBG_NC_ENGINE_SYNC`      | `"SP"`                           |
|     5 | `NDBG_NC_ENGINE_CC_CORE`   | `"TOP_SP"`                       |

These are the canonical engine codenames used in every block-string
template (`TPB_<i>_%s_LOCAL_REG`, `%s_NX_SPC_MSB`, `%s_NX_SPC_LSB`,
`TOP_SP_<i>_TPB_SP_LOCAL_REG`, etc.). The fact that the engine names
are shared across V2/V3/V4 means the high-level "give me the PC of
NC 5's tensor engine" operation has identical code paths even though
the underlying block enum values it resolves to differ.
