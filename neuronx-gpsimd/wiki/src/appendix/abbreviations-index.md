# Abbreviations & Symbol Index

This is the **fast-lookup index** for every acronym, opcode-mnemonic prefix, register name,
firmware idiom, runtime symbol family, CSR/address block, and collective term that recurs
across the wiki. It **complements** the [Master Glossary](../glossary.md): the glossary
*defines* (a crisp paragraph + canonical anchor + deep page per term); this index *cross-references*
(a one-line expansion + a link to the page that owns the term), and it adds the **symbol-prefix
families** the glossary does not enumerate (`nrt_*`, `nrtucode_*`, `aws_hal_q7_*`, `kmgr_*`,
`tdrv_*`, `enc_*`, `ivp_*`, `module__xdref_*`).

**How to use it.** Land here when you hit a bare token in another page — `cc_op`, `tdrv_ctx`,
`ivp_`, `P%i:`, `NQ`, `EVT_SEM` — and want "what is it, and where is it defined". Find the row,
read the one-line expansion, click the defining page. For the *full* definition + anchor, follow
the glossary link in the row (or the [Master Glossary](../glossary.md) directly). For the
byte-level encoding lookup, see the [Master ISA Encoding Appendix](isa-encoding-appendix.md).

**Provenance.** Every expansion is derived from static analysis of the shipped binaries —
recovered symbols, embedded prototype strings, config tokens, opcode-name string pools, and
device `.rodata` literals. Symbol-family counts are reproducible: an `nm`/`strings`/`rg` against
the one named binary by absolute path reproduces them. Where a row's *meaning* is inferred rather
than read, it carries a confidence tag (`[OBSERVED]` / `[INFERRED]` / `[MED]`); most are
`[HIGH/OBSERVED]` from symbols and are tagged only where confusable. The five **NOTE / GOTCHA**
callouts at the end disambiguate the genuinely-confusable abbreviations (the v5-label overload,
`SP` vs `TOP_SP`, `ct37` vs `arch_id 36`, `XRP` the not-Cadence-framework, `CCE` the not-an-engine).

Literal `|` inside a cell is escaped `\|`. A row that points at the glossary means the term is
*defined* there; a row that points at a subsystem page means that page owns the deep treatment.

---

## 1. Engine & codename abbreviations

The generation codenames, the `coretype`/`arch_id` identity bytes, and the engine ordinals.
Deep table: [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) and the
[Codename Cross-Walk Table](codename-crosswalk-table.md).

| term | expansion (terse) | defining page |
|---|---|---|
| **GPSIMD** | General-Purpose SIMD substrate = the POOL engine, 8× Vision-Q7 Cairo DSP cores | [What GPSIMD Is](../orientation/what-gpsimd-is.md) |
| **Cairo** | Tensilica µarch name of the GPSIMD core (`uarchName = Cairo`); one frozen Vision-Q7 NX config | [glossary · Cairo](../glossary.md#core--isa-identity) |
| **Vision-Q7 / Q7** | the off-the-shelf Cadence Tensilica vector DSP IP that GPSIMD *is* | [Core Identity & Configuration](../isa/core/identity-config.md) |
| **`ncore2gp`** | Tensilica CoreID of the Cairo config; the `XTENSA_CORE=` handle for the device disassembler | [Core Identity & Configuration](../isa/core/identity-config.md) |
| **`coretype`** | firmware byte identifying a generation — `{6,13,21,29,37}` `[HIGH/OBSERVED]` | [Codename Cross-Walk](../reference/codename-crosswalk.md) |
| **`arch_id`** | host NCFW image-selector byte; `arch_id = coretype − 1` (the **only** uniform relation — the `coretype` set `{6,13,21,29,37}` is *not* a flat +8 stride). `{5,12,20,28}` OBSERVED (double-anchored v2–v4); `36*` INFERRED (v5, no NCFW byte) | [Codename Cross-Walk](../reference/codename-crosswalk.md) |
| **SUNDA** | gen v2 (NC-v2; Trn1/Inf2) · `coretype 6` / `arch_id 0x05` · the no-HW-Decode floor | [SUNDA v2 Baseline](../generations/sunda-v2-baseline.md) |
| **CAYMAN** | gen v3 (NC-v3; Trn2) · `coretype 13` / `arch_id 0x0c` · the byte-grounded reference gen | [Codename ↔ Generation Map](../generations/codename-generation-map.md) |
| **MARIANA** | gen v4 (NC-v4) · `coretype 21` / `arch_id 0x14` · distinct compile of the CAYMAN contract | [Codename ↔ Generation Map](../generations/codename-generation-map.md) |
| **MARIANA_PLUS** | gen v4+ (Trn3-pre) · `coretype 29` / `arch_id 0x1c` · feature-flag delta, byte-identical EXTISA | [MARIANA_PLUS Delta](../generations/mariana-plus-delta.md) |
| **MAVERICK** | gen v5 (NC-v5) · `coretype 37` OBSERVED / `arch_id 36` INFERRED · header-only, no NCFW image | [MAVERICK Profile](../generations/maverick-profile.md) |
| **TONGA** | legacy NC-v1 (Inf1) outlier — **not** a 6th gen; no `coretype`/`arch_id`/NCFW | [Cross-Gen Opcode Diff + TONGA](../generations/cross-gen-opcode-diff.md) |
| **NC-v1 … NC-v5** | the NeuronCore-version axis (v1=TONGA … v5=MAVERICK) | [Codename Cross-Walk](../reference/codename-crosswalk.md) |
| **EXTISA** | per-gen statically-embedded Vision-Q7 ext-ISA device image (ELF32-Xtensa, `e_machine=94`) | [EXTISA Q7 SO-Blob Inventory](../images/extisa-inventory.md) |
| **TPB** | Tensor-Processing Block = one NeuronCore as a block of engines on one SBUF | [The Seven Faces](../orientation/seven-faces.md) |
| **PE** | Processing-Element array (128×128 systolic Tensor engine) · `engine_idx 0` · sole PSUM writer | [PE Matrix-Multiply Path](../firmware/kernels/pe-matmul.md) |
| **ACT** | Scalar / activation / PWL engine · `engine_idx 1` · folds into DVE on MAVERICK | [Activate + PWL](../firmware/kernels/activate-pwl.md) |
| **POOL** | the GPSIMD engine (8× Vision-Q7 Cairo) · `engine_idx 2` · subject of this wiki | [POOL Dispatch Loop](../firmware/pool/pool-dispatch.md) |
| **DVE** | the Vector engine · `engine_idx 3` · predicated-op family; absorbs ACT on MAVERICK | [DVE State Read-Back](../firmware/kernels/dve-read-state.md) |
| **SP (`TPB_SP`)** | per-NeuronCore Sync/control front-end executor · `engine_idx 4` | [Per-Engine Firmware Depth](../uarch/per-engine-depth.md) |
| **TOP_SP** | standalone NX-core sequencer that walks the `cc_op` collective program · `engine_idx 5` | [TOP_SP Collective Lowering](../collectives/ops/top-sp-lowering.md) |
| **NCFW** | NeuronCore Firmware — a **separate scalar Xtensa-LX** collective-management core | [The NCFW Scalar-LX Core](../uarch/ncfw-lx-core.md) |
| **`engine_idx`** | firmware engine ordinal (PE 0 … TOP_SP 5) — **not** the NKI compiler engine enum | [The Compiler Map](../compiler/compiler-map.md) |

---

## 2. ISA mnemonic families & opcode prefixes

The Vision-Q7 mnemonic family, the scalar base, and the recurring opcode bytes. Byte-level
encodings: [Master ISA Encoding Appendix](isa-encoding-appendix.md).

| prefix / opcode | expansion (terse) | defining page |
|---|---|---|
| **`ivp_*`** | Vision-Q7 vector-instruction mnemonic family (IVP SIMD TIE package `xt_ivp32`); **1065** distinct mnemonics in the `libisa-core.so` opcode string pool | [ISA Template & Partition](../isa/ref/template-and-partition.md) |
| *(scalar base)* | base-Xtensa LX7 scalar ops (`addi`, `l32i.n`, `entry`, `retw.n`) — **not** `ivp_*`-prefixed | [Eight Register Files](../isa/core/register-files.md) |
| **`0x41` TENSOR_TENSOR_ARITH_OP** | the int32/uint32 add/sub/mul datapath (GpSimd-native lane) | [Collective + cc_op Enums](../collectives/ops/collective-enums.md) |
| **`0xBF` SB2SB_COLLECTIVE** | one SBUF→SBUF collective hop (a ring all-reduce step) | [S3D3 Collective (SB2SB)](../collectives/ops/s3d3-collective.md) |
| **`0xF0` EXTENDED_INST** | custom-op extended-instruction space; DMAs its own Q7 EXTISA image | [POOL Extended-Opcode (0xF0)](../firmware/pool/pool-ext-0xf0.md) |
| **`0xC7/0xC8/0xD9`** | `PSEUDO_TRIGGER_ALL_REDUCE` / `_COLLECTIVE` / `_COLLECTIVE2` triggers | [Collective + cc_op Enums](../collectives/ops/collective-enums.md) |
| **`0xCB/0xD5/0xD8`** | `PSEUDO_SEND_RECV` / `_SYNC_BARRIER` / `_CORE_BARRIER` | [Collective + cc_op Enums](../collectives/ops/collective-enums.md) |
| **`0xC3` PSEUDO_DMABARRIER** | DMA barrier opcode | [Collective + cc_op Enums](../collectives/ops/collective-enums.md) |
| **`0xDB` PSEUDO_CUR_PROCESSING_RANK_ID** | read this core's PRID/rank | [PseudoCurProcessingRankID](../collectives/ops/rank-id.md) |
| **`NEURON_ISA_TPB_OPCODE_*`** | the arch-ISA opcode enum (the byte→name table) in `aws_neuron_isa_tpb_common.h` | [Collective + cc_op Enums](../collectives/ops/collective-enums.md) |
| **TIE** | Tensilica Instruction Extension — the DB/language defining the Vision-Q7 coproc ISA | [TIE Database](../isa/core/tie-database.md) |

---

## 3. FLIX encoding terms

The Tensilica VLIW scheme. Deep decoder: [FLIX Bundle-Decoding Methodology](../reference/flix-decoding.md);
byte tables: [FLIX Encoding](../isa/core/flix-encoding.md).

| term | expansion (terse) | defining page |
|---|---|---|
| **FLIX** | Flexible-Length Instruction eXtensions — the VLIW scheme; **14 formats**, **46 slots** | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **bundle** | one fetched FLIX word (16 B wide / 8 B narrow / 2–3 B scalar fallback) | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **format** | one of the 14 bundle layouts (selector encoding) | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **slot** | one operation position within a format; 14 formats → 46 slots | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **`x24` / `x16a` / `x16b`** | the three scalar formats (24-bit + two 16-bit-density) | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **`F0`/`F1`/`F2`/`F3`/`F4`/`F6`/`F7`/`F11`** | the eight **wide** (16-byte / 128-bit) bundle formats | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **`N0`/`N1`/`N2`** | the three **narrow** (8-byte) bundle formats | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **wide / narrow** | wide = 16 B / 4–5 slots (`op0` E, or F odd `b3lo`); narrow = 8 B / 2–4 slots (F even `b3lo`) | [FLIX Encoding](../isa/core/flix-encoding.md) |
| **`length_table`** | 256-entry table → "7 length-class outcomes → 4 byte-lengths `{2,3,8,16}`" | [FLIX Decoding](../reference/flix-decoding.md) |
| **FLIX-desync** | a linear byte sweep desyncs on literal pools / the F0 8-vs-16 split → bundle interior | [FLIX Decoding](../reference/flix-decoding.md) |
| **`b3lo`** | low nibble of byte 3 — the wide-vs-narrow + 8-vs-16 discriminator for `op0==0xF` | [FLIX Decoding](../reference/flix-decoding.md) |

---

## 4. Register-file names & ISA metadata fields

The eight register files (two scalar, six Vision-Q7 SIMD) and the per-operand metadata tables.
Deep page: [The Eight Register Files](../isa/core/register-files.md).

| name (short) | expansion (terse) | defining page |
|---|---|---|
| **`AR` (`a`)** | scalar address/general registers (32-bit × 64, windowed ABI) | [Eight Register Files](../isa/core/register-files.md) |
| **`BR` (`b`)** | scalar boolean registers (1-bit × 16; `BR2/4/8/16` views) | [Eight Register Files](../isa/core/register-files.md) |
| **`vec` (`v`)** | 512-bit SIMD vector file (32 × 16-bit lanes; 32 regs) | [Eight Register Files](../isa/core/register-files.md) |
| **`vbool` (`vb`)** | per-lane SIMD predicate (boolean) file (64-bit × 16) | [Eight Register Files](../isa/core/register-files.md) |
| **`valign` (`u`)** | alignment registers priming unaligned vector loads (512-bit × 4) | [B06 Vector Loads + valign](../isa/ref/b06-loads.md) |
| **`wvec` (`wv`)** | 1536-bit wide MAC accumulators (quad-width readout; 4 regs) | [Eight Register Files](../isa/core/register-files.md) |
| **`b32_pr` (`pr`)** | 64-bit predicate/pack registers (`int64pr` ctype; 16 regs) | [Eight Register Files](../isa/core/register-files.md) |
| **`gvr` (`gr`)** | global/state vector registers (`gsr` ctype, flags `0x0d`; 8 regs) | [Eight Register Files](../isa/core/register-files.md) |
| **SuperGather** | the IVP two-phase gather-register-staged gather/scatter unit | [B19 SuperGather](../isa/ref/b19-scatter-gather.md) |
| **`ctype`** | TIE C value-type a register/operand maps to (64-entry `ctypes` table) | [ctype/coproc/funcUnit Tables](../isa/core/ctype-coproc-funcunit.md) |
| **`coproc`** | the coprocessor (exactly one: `{name="Vision", number=1}`) | [ctype/coproc/funcUnit Tables](../isa/core/ctype-coproc-funcunit.md) |
| **`funcUnit`** | functional unit an opcode binds (one: `XT_LOADSTORE_UNIT`, 2 copies) | [ctype/coproc/funcUnit Tables](../isa/core/ctype-coproc-funcunit.md) |
| **`MS`** | XEA3 dispatch-mode/state register (SR `0xe5`) — software-managed dispatch demux | [XEA3 Interrupt Architecture](../control/interrupt/xea3-interrupt-architecture.md) |
| **`VECBASE`** | vector-base SR `0xe7` — the `[31:6]` page all dispatch vectors hang off | [XEA3 Interrupt Architecture](../control/interrupt/xea3-interrupt-architecture.md) |
| **`PRID`** | special-register `235` — the SPMD rank of a Q7 core (`PRID_ID & 0xF ∈ {0..7}`) | [The 8-Core SPMD Model](../runtime/spmd-teardown.md) |

---

## 5. Firmware & device symbol idioms

The device-firmware build idioms used as recovery anchors. The trace tags are confirmed in the
device `.rodata` of `libnrtucode_internal.so` (`S:` literals 16×, `P%i:` literals 1016×).

| idiom | expansion (terse) | defining page |
|---|---|---|
| **`kernel_info_table`** | per-image opcode→handler map; 8-byte entries `{0,0,spec,opcode,u32 funcVA}` (CAYMAN: 17) | [kernel_info_table Layout](../firmware/pool/kernel-info-table.md) |
| **`.xt.prop`** | per-function FLIX property sections (`.xt.prop.<mangled>`) — the FLIX-aware code-mode anchor (**138×** in `libnrtucode_internal.so`) | [FLIX Decoding](../reference/flix-decoding.md) |
| **`'S:'` tag** | DEBUG trace string of the **SEQ control-engine** stream (e.g. `S: Dispatch opcode=0x%x`, `S: BEGIN on cayman`) | [SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md) |
| **`'P%i:'` tag** | DEBUG trace string of the **per-CPU POOL/Q7 data-plane** stream; `%i` = the core's PRID (e.g. `P%i: SB2SB_Collective ...`) | [SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md) |
| **HW-Decode** | the later-gen hardware opcode-decode CAM dispatch mode (SUNDA disables it) | [HW-Decode vs Sunda Dual Fetch](../firmware/seq/dual-fetch.md) |
| **Sunda mode** | the SUNDA software dual-fetch path (`S: NX in Sunda mode: HW decode disabled`) | [HW-Decode vs Sunda Dual Fetch](../firmware/seq/dual-fetch.md) |
| **SEQ** | the device sequencer front-end (boot, FSM, fetch + PC-redirect, decode/dispatch hub) | [SEQ Main FSM Loop](../firmware/seq/main-loop.md) |
| **DGE** | Descriptor Generation Engine — 3-backend selector (Pool / RTL / software) | [DGE 3-Backend Selector](../firmware/dge/dge-backend-selector.md) |
| **Boot handshake** | unbooted sentinel `0x6099CB34` → host CAS-writes claim `0x502B2DA1` | [Boot / Reset Sequence](../uarch/boot-reset.md) |
| **`XCHAL_*`** | the Tensilica config-token prefix (`XCHAL_VISION_TYPE`, `XCHAL_XEA_VERSION`, …) in `core-isa.h` | [Core Identity & Configuration](../isa/core/identity-config.md) |

---

## 6. Runtime / host symbol families

The host x86-64 runtime prefixes. Counts are reproducible with `nm`/`strings` against the one
named binary; the host families (`aws_hal_*`, `kmgr_*`, `tdrv_*`, `enc_*`) live in `libnrt.so`
as embedded prototype strings (the library is **not stripped** — these are its symbolized C++
prototypes). `nrtucode_*` is a real `.symtab` family in `libnrtucode_internal.so`.

| prefix | expansion (terse) | binary + count | defining page |
|---|---|---|---|
| **`nrt_*`** | host-runtime public API (`nrt_load`, `nrt_set_pool_eng_ucode`, …) | `libnrt.so` · **121** dynsym | [libnrt Surface Map](../runtime/libnrt-surface.md) |
| **`nrtucode_*`** | host micro-code subsystem managing Q7 images (resolvers, ll-load/unload, opset) | `libnrtucode_internal.so` · **60** `nm` | [nrtucode Subsystem](../runtime/nrtucode-bringup.md) |
| **`aws_hal_q7_*`** | host Q7 HAL — register/window/swap-table accessors (`aws_hal_q7_swap_table`, …) | `libnrt.so` · **99** strings | [The aws_hal_q7_* HAL](../runtime/aws-hal-q7.md) |
| **`aws_hal_*`** | the wider KaenaHal hardware-abstraction surface | `libnrt.so` · **2528** strings | [The aws_hal_q7_* HAL](../runtime/aws-hal-q7.md) |
| **`kmgr_*`** | host kernel/model manager (`kmgr_init`, `kmgr_sync_exec`, `kmgr_exec_resources_t`) | `libnrt.so` · **471** strings | [Exec-State Census](struct-exec-state-census.md) |
| **`tdrv_*`** | host **t**ensor-**dr**i**v**er layer (`tdrv_ctx_t`, `tdrv_arch_get_num_tpb`, …) | `libnrt.so` · **1226** strings | [Exec-State Census](struct-exec-state-census.md) |
| **`tdrv_ctx_t`** | the process-global host root struct (`tdrv_ctx_0`, embeds `mla[32]`) | `libnrt.so` (DWARF) | [Exec-State Census](struct-exec-state-census.md) |
| **`enc_*` / `encd_*`** | host-side collective-communication (CC) program builder family (`enc_alg_type`, `enc_comm_info`, `encd_context`) | `libnrt.so` · **10208** strings (ENC_ALG/ALLGATHER 38) | [Device-Firmware Globals §1.5](struct-device-firmware-globals.md) |
| **`module__xdref_*`** | the `libfiss-base.so` per-lane value-leaf family (`module__xdref_add_16_16_16`) | `libfiss-base.so` · **864** `nm` | [fiss 864-Leaf Value Oracle](../iss/fiss-datapath-oracle.md) |
| **`ncfw_*`** | NCFW host logging/scheduler symbols (`ncfw_log_spad_ctrl_cc_op_entry`) | `libncfw.so` · **156** `nm` | [NCFW spad-ctrl cc_op](../collectives/ncfw/spad-ccop-tsync.md) |
| **`nrtucode_core_t`** | per-core host handle (`0x70` B; `coretype` byte at `+0x10`) | `libnrtucode_internal.so` | [nrtucode_core_t Struct](../runtime/nrtucode-core.md) |
| **`nrtucode_context_t`** | per-model host context (`0x28` B; lifecycle + `dmem` allocator) | `libnrtucode_internal.so` | [nrtucode_context_t](../runtime/nrtucode-context.md) |
| **ISS** | Instruction-Set Simulator (host config libs used as a live value oracle) | `libfiss-base` / `libcas-core` | [ISS Semantic Synthesis](../iss/iss-semantic-synthesis.md) |
| **`xdref`** | the value-leaf naming stem (`<op>_<wout>_<win0>_<win1>`) — "cross-decode reference value fn" | `libfiss-base.so` | [fiss Value Oracle](../iss/fiss-datapath-oracle.md) |

---

## 7. CSR & address-block names

The device control/status-register block names and SoC address regions. Block-name index:
[CSR Field-Table Index](csr-field-table-index.md).

| block / region | expansion (terse) | defining page |
|---|---|---|
| **`udma_m2s`** | al_udma **m**emory-**2**-**s**tream outbound descriptor engine CSR block | [CSR — udma_m2s](../control/csr/udma-m2s.md) |
| **`udma_s2m`** | al_udma **s**tream-**2**-**m**emory inbound descriptor engine CSR block | [CSR — udma_s2m](../control/csr/udma-s2m.md) |
| **`udma_gen` / `udma_gen_ex` / `tdma_model`** | shared SDMA control CSR blocks | [CSR — udma_gen/tdma](../control/csr/udma-gen-tdma.md) |
| **`al_udma`** | the Annapurna-Labs micro-DMA hardware engine the `udma_*` blocks program | [al_udma HW DMA Engine](../dma/udma-hw-engine.md) |
| **NOTIFIC / `notific_n_queue`** | hardware instruction-notification queue → coalesces into SW **NQ** rings | [CSR — NOTIFIC Queue](../control/csr/notific-queue.md) |
| **EVT_SEM** | event-semaphore CSR region (`*_EVT_SEM_SEMAPHORE_SET/READ/DEC_BASE`; **2649** strings in `libnrt.so`) | [Address — EVT_SEM Regions](../control/address/evt-sem-regions.md) |
| **FIS / `fis_control`** | per-fabric-master error-trigger / isolation block (acronym not spelled in corpus) | [CSR — FIS + errtrig + spad](../control/csr/fis-errtrig-spad.md) |
| **`sprot`** | the security-protection (firewall) vector within the FIS/error-routing fabric | [CSR — FIS + errtrig + spad](../control/csr/fis-errtrig-spad.md) |
| **`errtrig`** | the error-trigger vector routing faults (e.g. APB timeout) into the interrupt fabric | [CSR — FIS + errtrig + spad](../control/csr/fis-errtrig-spad.md) |
| **`intc_*`** | the interrupt-controller CSR blocks (1-group APINTC / 4-group) | [CSR Field-Table Index](csr-field-table-index.md) |
| **SBUF / `STATE_BUF`** | on-chip State Buffer — shared 32 MiB @ SoC `0x2000000000` (Q7-reachable) | [SBUF + PSUM Bank Model](../dma/sbuf-psum-banks.md) |
| **PSUM** | PE-private accumulator bank — **structurally unreachable** from GPSIMD (no AXI aperture) | [SBUF + PSUM Bank Model](../dma/sbuf-psum-banks.md) |
| **SPAD** | the scratchpad the NCFW / TOP_SP walks the `cc_op` program in | [NCFW spad-ctrl cc_op](../collectives/ncfw/spad-ccop-tsync.md) |

---

## 8. Collective & NCFW vocabulary

The collective-communication control plane: the program the NCFW/TOP_SP walks, the host-side
encoder family, the messaging transport, and the DMA datapaths.

| term | expansion (terse) | defining page |
|---|---|---|
| **`cc_op`** | the collective-communication operation program the NCFW/TOP_SP walks (per-step `spad-ctrl` descriptors); `"cc_op"` string @ `0x650d4` in `libncfw.so` | [NCFW spad-ctrl cc_op + tsync](../collectives/ncfw/spad-ccop-tsync.md) |
| **`cc_op_info`** | the per-entry descriptor record (`{cc_op, op_type, alg, alg_name}`) | [NCFW spad-ctrl cc_op](../collectives/ncfw/spad-ccop-tsync.md) |
| **`tsync`** | the time-sync sequencing layer realising a collective step-by-step | [NCFW spad-ctrl cc_op](../collectives/ncfw/spad-ccop-tsync.md) |
| **`enc_op_type`** | the high-level op enum the op-list lowers (`ENC_ALLGATHER=0` … `ENC_ALLTOALL_V=12`, 13 kinds) | [Device-Firmware Globals §1.5](struct-device-firmware-globals.md) |
| **`enc_alg_type`** | the algorithm enum (`ENC_ALG_RING=0`, `_HIER=1`, `_MESH=2`, `_KANGARING=3`, … `_INVALID=11`) | [Device-Firmware Globals §1.5](struct-device-firmware-globals.md) |
| **`enc_alg_mesh_type`** | the mesh sub-selector (`FULL_MESH=0` … `INVALID=4`) | [Device-Firmware Globals §1.5](struct-device-firmware-globals.md) |
| **`enc_comm_type`** | topology scope (`H_COMM_INTRA_ID=0`, `H_COMM_INTER_ID=1`) | [Device-Firmware Globals §1.5](struct-device-firmware-globals.md) |
| **`enc_comm_info`** | the 72-byte communicator topology descriptor | [Device-Firmware Globals §1.5](struct-device-firmware-globals.md) |
| **EVT_SEM** | event-semaphore — the engine-quad `{INST_START, INST_END, EXPLICIT, EVT_SEM}` notify member + the CSR region | [Address — EVT_SEM Regions](../control/address/evt-sem-regions.md) |
| **NQ** | SW **N**otification **Q**ueue — up to ten software-owned ring queues the NOTIFIC block writes over AXI | [CSR — NOTIFIC Queue](../control/csr/notific-queue.md) |
| **CCE** | Compute-DMA / in-transfer compute — reduction **inside** the SDMA transfer (`CDMA = DDMA + CCE`) | [CCE In-Transfer Compute](../dma/cce-in-transfer.md) |
| **SB2SB** | "State-Buffer to State-Buffer" — the `0xBF` intra-/inter-die collective hop | [S3D3 Collective (SB2SB)](../collectives/ops/s3d3-collective.md) |
| **RDMA** | cross-core/cross-die SBUF→SBUF peer-to-peer byte movement over the die mesh | [RDMA Cross-Die P2P](../dma/rdma-cross-die.md) |
| **XRP** | host↔DSP collective-control messaging transport — a **bespoke** Annapurna queue, **not** Cadence XRP | [XRP Host↔DSP Messaging](../collectives/ops/xrp-host-dsp-messaging.md) |
| **`pring` / `vring`** | persistent vs template DMA-descriptor rings (NCFW copies `vring`→`pring` once, reuses) | [pring Descriptors](../collectives/ncfw/pring-descriptors.md) |
| **`ncfw_log_spad_ctrl_cc_op_entry`** | the NCFW logger that emits a `cc_op` spad-ctrl entry (the `cc_op` string anchor) | [NCFW spad-ctrl cc_op](../collectives/ncfw/spad-ccop-tsync.md) |

---

## 9. Container, compiler & general acronyms

| term | expansion (terse) | defining page |
|---|---|---|
| **NEFF** | Neuron Executable File Format — 1024-byte header + inner gzip→tar container | [NEFF Container Byte Format](../neff/container-byte-format.md) |
| **`metaneff`** | the NEFF host-side protobuf I/O key-ring (`MetaNeff`/`MetaTensor`; binds tensor→`var_id`→`mem_ref`) | [metaneff I/O ABI](../neff/metaneff-io-abi.md) |
| **BIR / Penguin** | the `neuronx-cc` "penguin" backend IR (`libBIR.so`, ~110 `Inst*` classes) | [Penguin BIR Instruction Set](../compiler/bir-inst-roster.md) |
| **NKI** | Neuron Kernel Interface — the Python frontend (engine enum `tensor=1…sync=6`, **not** `engine_idx`) | [NKI Frontend](../compiler/nki-frontend.md) |
| **MX** | microscaling — the block-scaled low-precision dequant/compute path (`MXTENSOR_*`) | [MX Microscaling Path](../compiler/mx-path.md) |
| **`SundaISel`** | the compiler instruction-selection pass for the GPSIMD/Sunda lane (PSUM fall-back enforcer) | [SundaISel Deep-Dive](../compiler/sundaisel.md) |
| **`CoreV5` / `core_v5`** | compiler ArchLevel slot = **MARIANA_PLUS / Trn3-PRE** — **NOT** MAVERICK (see §10 NOTE) | [Codename Cross-Walk](../reference/codename-crosswalk.md) |
| **ISS / FISS / CAS** | Instruction-Set Sim / Fast ISS (value) / Cycle-Accurate Sim (timing, license-gated) | [ISS Semantic Synthesis](../iss/iss-semantic-synthesis.md) |
| **XEA3** | Xtensa Exception Architecture 3 — single unified DispatchVector, `MS`-managed dispatch | [XEA3 Interrupt Architecture](../control/interrupt/xea3-interrupt-architecture.md) |
| **`Xtensa24`** | the core's ISA name (`arch = Xtensa24`) — the LX7 family (windowed ABI + 16-bit density + FLIX) | [glossary · Xtensa24/XEA3](../glossary.md#core--isa-identity) |
| **RTTI absence** | device firmware is `-fno-rtti` — `_ZTS/_ZTI/_ZTV` count is **0** across all 29 device ELFs | [Methodology](../reference/methodology.md) |
| **OBSERVED / INFERRED / CARRIED** | provenance tags (read-from-artifact / reasoned-over / reused-from-prior) | [Confidence & Walls Model](../reference/confidence-model.md) |
| **HIGH / MED / LOW** | confidence tags (trust level), orthogonal to provenance | [Confidence & Walls Model](../reference/confidence-model.md) |
| **Wall** | a genuine static-analysis boundary (closable-with-license / -corpus / -hardware / fundamental) | [Confidence & Walls Model](../reference/confidence-model.md) |
| **QUIRK / GOTCHA / CORRECTION / NOTE** | the inline callout markers used on every page | [How to Read This Guide](../reference/how-to-read.md) |

---

## 10. Disambiguation callouts — the confusable abbreviations

> **GOTCHA — the "v5" label is overloaded three ways; none derives the other.**
> `CoreV5` / `core_v5` is the **compiler ArchLevel** slot (host platform / `libwalrus`), and it
> maps to **MARIANA_PLUS / Trn3-PRE** — `coretype 29` / `arch_id 0x1c`, an **NCFW-present** gen.
> `NC-v5` / GPSIMD **`coretype 37`** is the genuine 5th *silicon* — **MAVERICK** — with **no**
> NCFW image. `NeuronCoreVersion::V5 = 5` is the arch-ISA cap enum (present in maverick, absent in
> mariana). A page citing "v5" must say *which* axis. See
> [Codename Cross-Walk §CoreV5-vs-NC-v5](../reference/codename-crosswalk.md) and
> [MAVERICK Profile §1](../generations/maverick-profile.md).

> **GOTCHA — `SP` vs `TOP_SP` are two distinct engines.** `SP` (`TPB_SP`) is the per-NeuronCore
> Sync/control front-end executor (`engine_idx 4`); `TOP_SP` is the standalone NX-core sequencer
> that walks the `cc_op` collective program in SPAD (`engine_idx 5`). They share the `_SP` suffix
> and the `TPB_SP` token but are not the same core. See [TOP_SP Lowering](../collectives/ops/top-sp-lowering.md).

> **NOTE — `ct37` is OBSERVED, `arch_id 36` is INFERRED.** MAVERICK's `coretype 37` is anchored
> twice in firmware (the header enum ordinal and the twin resolver bitmasks) → `[HIGH/OBSERVED]`.
> Its `arch_id = 36 (0x24)` has **no** `cmp $0x24` anywhere and no v5 NCFW image — it is the
> `coretype − 1` extrapolation only → `[MED/INFERRED]`. Never hard-code `arch_id 36`. See
> [Codename Cross-Walk](../reference/codename-crosswalk.md).

> **NOTE — `XRP` is *not* a Cadence framework here.** Despite the name there is **no Cadence XRP**
> present in the corpus; the host↔DSP transport is a bespoke Annapurna-Labs message queue. The only
> `xrp` substring corpus-wide is `libnrt`'s unrelated `AF_RXRPC`. See
> [XRP Host↔DSP Messaging](../collectives/ops/xrp-host-dsp-messaging.md).

> **NOTE — `CCE` is a *datapath*, not an engine, and `NCFW` is a *core*, not the FLIX one.** `CCE`
> (Compute-DMA) is the in-transfer reduce path *inside* the SDMA fabric — there is no "CCE engine".
> `NCFW` is a separate **scalar Xtensa-LX** management core: do **not** run the FLIX decoder on its
> images (the spurious "~26–28% FLIX" artifact comes from doing exactly that). Three cores, three
> ISAs: **Q7** (Vision-Q7 FLIX, this wiki), **NCFW** (scalar-LX), **TOP_SP** (NX sequencer). See
> [Keystone Facts](../orientation/keystone-facts.md) and [The NCFW Scalar-LX Core](../uarch/ncfw-lx-core.md).

---

## See also

- [Master Glossary](../glossary.md) — the definitional reference (this index complements it).
- [Master ISA Encoding Appendix](isa-encoding-appendix.md) — the byte-level opcode-encoding lookup.
- [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) + [Cross-Walk Table](codename-crosswalk-table.md) — the codename ↔ `coretype` ↔ `arch_id` map.
- [CSR Field-Table Index](csr-field-table-index.md) — the CSR-block-name index.
- [Device-Firmware Globals](struct-device-firmware-globals.md) · [Exec-State Census](struct-exec-state-census.md) · [Host-Runtime Layouts](struct-host-runtime-layouts.md) — the struct censuses behind the symbol families.
- [The Confidence & Walls Model](../reference/confidence-model.md) — the tag and wall taxonomy.
