# Per-Engine Firmware Depth (PE / SP / TOP_SP / ACT)

> **Scope.** This page is the **per-engine micro-op roster** of the four TPB engines whose firmware the
> sibling [SIMD compute-datapath](simd-datapath.md) and [activation/transcendental-table](activation-transcendental-tables.md)
> pages described in *datapath* terms — here pinned to **byte-exact opcodes, compile-verified operand
> structs, and named CSR handshake fields**. It documents (1) the **PE-array systolic-matmul micro-op
> sequence** — `Ldweights` / `Matmul` / `LdweightsMX` / `MatmulMX` / `LdTags` / `PeManageSeed` /
> `MatmulSparse` / `PeRegWrite` — the weight-load + matmul + MX datapath and the PE↔SEQ `arr_seq`
> handshake; (2) the **SP (`TPB_SP`, engine 4) + TOP_SP (engine 5)** sync / collective-lowering ops;
> (3) the **ACT activation-PWL quad** and the **MAVERICK ACT→DVE fold** at the HW-region level; (4) the
> **CSR live-usage** — who programs QoS / REMAPPER / NSM / PMU, and when.
>
> These leaves **upgrade** the engine sub-leaves of the image and firmware Parts: the per-`(gen×engine)`
> firmware images ([Part 6](../images/image-catalog-index.md) — forward) and the per-engine kernel paths
> ([Part 5](../firmware/kernels/pe-matmul.md) — forward) carry the carve bytes and the handler-body
> traces this page anchors. The Vision-Q7 microarchitecture proper is the [datapath](simd-datapath.md)
> and [pipeline](pipeline-timing.md) pages; this page is the **firmware-level** view: the SEQ-dispatch
> micro-op orchestrators that drive the systolic array, the sync block, and the activation PWL.

## Epistemic guard — read this first

**This is a SEQ-dispatch firmware roster, not the array RTL.** The PE-array systolic grid, the PSUM
accumulator banks, the per-cell multiplier, the stochastic-rounding RNG, and the EVT_SEM atomic
hardware are **silicon** — no netlist ships. What **is** in the corpus, and what this page is built
from, is four independently-OBSERVED layers:

| layer | what it gives | where |
|---|---|---|
| **L1 — the ISA opcode enum + operand-struct C contract** | the byte-exact opcode roster, the per-gen struct offsets, the enums | `aws_neuron_isa_tpb_common.h` + the per-op `aws_neuron_isa_tpb_*.h` headers (compile-verified `gcc offsetof`) |
| **L2 — the `struct2opcode` mapping** | the struct↔opcode binding, the pseudo-opcode classification | `instruction_mapping.json` (read with `jq`) |
| **L3 — the firmware DEBUG self-naming strings** | the per-gen handler presence (`S:`/`RS:`/`XS:` anchors) + the dispatch chain | `libnrtucode_internal.so` carved IRAM/DRAM blobs, decoded with the shipped `ncore2gp` `xtensa-elf-objdump` |
| **L4 — the RTL-generated CSR JSON** | the PE↔SEQ handshake surface; the ACT→DVE HW-region map; the QoS/remapper/NSM/PMU schema | the shipped `csrs/` + `arch-regs` JSON / address maps |

Tags per claim: `[HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED]`. `OBSERVED` = a symbol / byte / opcode /
compile-verified struct / CSR-JSON field read **this pass**; `INFERRED` = reasoned over OBSERVED;
`CARRIED` = re-used at a cited backing report's confidence. All prose reads as derived from
shipped-binary + shipped-public-header + device-disassembler static analysis (lawful interoperability
reverse engineering, DMCA 17 U.S.C. 1201(f)). Binary + header paths under
`extracted/aws-neuronx-gpsimd-customop-lib_…/…/custom_op/c10/` and
`extracted/nested/gpsimd_tools_tgz/…` (gitignored — reach with `fd --no-ignore` or an absolute path).
In the customop lib `libnrtucode_internal.so`, `.rodata` is **identity-mapped** (VMA==file-offset, so
the carved IRAM/DRAM getter blobs read directly), while `.text`/`.data` carry small monotone link
deltas — and the separate `ncore2gp` config DLLs carry the `0x200000` `.data`/`.data.rel.ro` delta.
Confirm per-section with `readelf -SW` before any `xxd`/`objdump` on a section-resident struct; do not
over-generalise one binary's delta to another.

> **WALL — the per-instruction FLIX body tail stays MED.** The PE/SP/ACT handlers are hand-scheduled
> FLIX/VLIW with interleaved literal/selector spans; the stock `ncore2gp` linear sweep loses bundle
> sync across those spans, so the **handler interiors** (the mid-body micro-op issue) do not linearly
> decode. What **decodes clean** and is reported `[HIGH]`: the reset/boot vectors, the dispatch
> compare-chains, the IVP-MAC mnemonic census, and **all** the DEBUG self-naming strings. The
> opcode/struct/enum facts are **header-compile-verified** (not disasm-dependent). The exact in-handler
> body schedule is `[MED]` — flagged, not over-claimed.

> **WALL — v5/Maverick interiors are header-OBSERVED only.** The Maverick PE/SP images are stripped
> (no DEBUG self-naming strings); the ACT engine does not exist on v5 as a standalone block. Every
> **Maverick-interior** claim is flagged `[INFERRED]`; the **ACT→DVE fold is OBSERVED at the HW-region
> level** (the address map names the relocated SRAM blocks), but the interior bodies are `[INFERRED]`.

---

## 0. Headline — the four engines in one paragraph

`[HIGH/OBSERVED]` All four engines are the **same `cayman/seq/` SEQ-dispatch chassis**, recompiled
with a different handler subset and self-identifying at boot by `engine_base_addr` (the engine enum,
read verbatim from `aws_neuron_isa_tpb_common.h:143-148`: `PE=0 ACT=1 POOL=2 DVE=3 TPB_SP=4
TOP_SP=5`). The **PE engine (idx 0)** is the systolic-matmul micro-op orchestrator: it byte-decodes
the compute opcode block `0x01..0x0A + 0xe4`, loads the stationary weight tile (`Ldweights 0x01`),
streams the moving activations through the array (`Matmul 0x02`) accumulating `dst = stationary.T @
moving` into the **FP32 PSUM banks**, and manages the PSUM stochastic-rounding seeds (`PeManageSeed
0x08`). The **SP engine (`TPB_SP`, idx 4)** is the **leanest** SEQ build — 18 pure sync/control
handlers, the 5-way intersection of all engines; its compute set is exactly the shared
`Event_Semaphore 0xa0` / `NOTIFY 0xa6` / `POLL_SEM 0xb3` / `EventSemaphoreRangeClear 0xb0` sync
opcodes. The **TOP_SP engine (idx 5)** is the standalone collective sequencer that walks the host-built
`cc_op` program. The **ACT engine (idx 1)** runs the activation-PWL quad (`Activate 0x21` /
`Activate2 0x25` / `ActivationTableLoad 0x23` / `ActivationReadAccumulator 0x24`), and on **Maverick
(v5) it is GONE** — its PWL SRAM physically migrated into the DVE block. Of the four FIS sprot control
blocks (QoS / REMAPPER / NSM / PMU), exactly **one** has an in-corpus programmer — the **REMAPPER**,
driven by the *host* runtime per memory-region; the other three sit at their reset (transparent /
bypassed / disabled) posture.

| engine | idx | role | compute opcode block | firmware self-name anchors |
|---|---:|---|---|---|
| **PE** | 0 | systolic matmul orchestrator | `0x01..0x0A`, `0xe4` | `S: Ldweights` / `S: Matmul` / `S: PeManageSeed` |
| **ACT** | 1 | activation-PWL apply | `0x21..0x26` | `RS: Activate2` / `S: Activate2` |
| POOL | 2 | (pool/DVE-class compute) | — | (sibling pages) |
| DVE | 3 | (vector + v5 activation host) | `0x30`+ | (sibling pages) |
| **TPB_SP** | 4 | per-core sync executor | `0xa0/0xa6/0xb0/0xb3` | `S: Event_Semaphore` / `S: NOTIFY` |
| **TOP_SP** | 5 | collective sequencer | (same SEQ build) | `engine_idx=%u` runtime identity |

`[HIGH/OBSERVED engine enum + the per-engine opcode block; the "one SEQ chassis" identity HIGH/OBSERVED
from the shared reset/boot/globstruct + the runtime engine-idx string.]`

---

## 1. The PE-array micro-op roster — byte-exact opcodes

`[HIGH/OBSERVED]` The PE compute opcodes come from the `NEURON_ISA_TPB_OPCODE` enum
(`aws_neuron_isa_tpb_common.h`, lines pinned this pass) — the `// Y` tag is the header's own
"tested/maintained" annotation:

| opcode | enum (`NEURON_ISA_TPB_OPCODE_…`) | line | operand struct | role |
|---:|---|---:|---|---|
| `0x01` | `LDWEIGHTS` | 158 | `S3_LW_STRUCT` | load stationary weight tile (the `x` of `nc_matmul`) |
| `0x02` | `MATMUL` | 159 | `S3D3_MM_STRUCT` | stream moving acts, MAC → PSUM |
| `0x03` | `PE_REG_WRITE` | 160 | (PE config-reg write) | write a PE configuration register |
| `0x04` | `WEIGHT_MASK` | 161 | (tonga, deprecated) | structured-sparsity weight mask |
| `0x05` | `WEIGHT_SHIFT` | 162 | (tonga, deprecated) | structured-sparsity weight shift |
| `0x06` | `LDTAGS` | 163 | (tag load) | load sparsity tags for `MatmulSparse` |
| `0x07` | `MATMUL_SPARSE` | 164 | `S3D3_MM` (tagged) | structured-sparse matmul |
| `0x08` | `PE_MANAGE_SEED` | 165 | `S2S1D2_PE_SEED_STRUCT` | PSUM SR-RNG seed save/restore `[v4+]` |
| `0x09` | `LDWEIGHTS_MX` | 166 | `SMX1_LW_STRUCT` | MX weight-load (separate v4 op) |
| `0x0A` | `MATMUL_MX` | 167 | `SMX1D3_MM_STRUCT` | MX matmul (separate v4 op) |
| `0xe4` | `CONV_LUT_LOAD` | 310 | `S2_CONVLUT` | 4-bit input-converter LUT load `[v4+]` |

The opcode↔struct binding is **triple-confirmed**: (i) the ISA enum (above); (ii) the
`struct2opcode` map in `instruction_mapping.json` (read with `jq` this pass) —
`S3_LW_STRUCT → LDWEIGHTS`, `S3D3_MM_STRUCT → MATMUL`, `S2S1D2_PE_SEED_STRUCT → PE_MANAGE_SEED`,
`SMX1_LW_STRUCT → LDWEIGHTS_MX`, `SMX1D3_MM_STRUCT → MATMUL_MX`; (iii) the firmware DEBUG handler
self-name strings (`S: Ldweights` / `S: Matmul` / `S: PeManageSeed(LOAD)` / `S: PeManageSeed(SAVE)` /
`S: LdweightsMX` / `XS: MatmulMX` / `S: ConvLutLoad`) and the byte-decoded dispatch chain (§2).
`[HIGH/OBSERVED enum + JSON + strings]`

### 1.1 Per-gen handler presence `[HIGH/OBSERVED]`

The handler subset GROWS across the generations — read from the carved PE DEBUG-DRAM `S:` self-names
([backing GX-ENG-01](#cross-references)):

| gen | PE handler set | notes |
|---|---|---|
| **CAYMAN (v3)** | `{Ldweights, Matmul, PeRegWrite, LdTags, MatmulSparse}` | **NO** `PeManageSeed` / MX / `ConvLutLoad` (0 string hits) |
| **MARIANA (v4)** | + `PeManageSeed(LOAD/SAVE)` + `LdweightsMX` + `MatmulMX` + `ConvLutLoad` | `S: BEGIN on mariana` anchor |
| **MARIANA_PLUS (v4+)** | same as v4 | `S: BEGIN on mariana_plus` |
| **MAVERICK (v5)** | (PERF only; **stripped** — names not symbol-recoverable) | opcode presence pinned by the ISA enum + the PE PERF MAC census |

> **NOTE — Maverick PE names are not recoverable.** The Maverick PE image is PERF-only with no DEBUG
> self-naming strings. The opcode *presence* is `[HIGH]` from the ISA enum; the firmware handler
> *names* are `[MED]` (the strip wall). The MX path is **retained in the enum** but on v5 the preferred
> path folds MX into `Ldweights`/`Matmul` (§5). `[HIGH enum; MED firmware-name]`

---

## 2. The PE dispatch — byte-decoded opcode compare-chain

`[HIGH/OBSERVED dispatch chain; MED interiors]` The MARIANA PE DEBUG IRAM dispatch site is a
**raw-opcode segmented compare-chain** — the PE compares the raw opcode byte (no `addi`-normalisation),
each opcode routing to a distinct dispatch stub in the `0x2b8x..0x2bc7` cluster. Carved from the
`MARIANA_NX_PE_DEBUG_IRAM_get.data` blob at `.rodata` file-offset `0x42c520`, the dispatch site
(IRAM `+0x2934`) decoded with the shipped `ncore2gp` `xtensa-elf-objdump` — the `66 X2 02` BNEI
encodings confirm the chain byte-for-byte:

```text
2934:  l32i.n a2,[a1+16]            ; a2 = opcode word
2936:  bnei a2, 1, … ; j 0x2b86    ; 0x01 Ldweights    -> stub 0x2b86
293f:  bnei a2, 2, … ; j 0x2b8e    ; 0x02 Matmul       -> 0x2b8e
2948:  bnei a2, 3, … ; j 0x2ba6    ; 0x03 PeRegWrite   -> 0x2ba6
2951:  bnei a2, 6, … ; j 0x2b96    ; 0x06 LdTags       -> 0x2b96
295a:  bnei a2, 7, … ; j 0x2b9e    ; 0x07 MatmulSparse -> 0x2b9e
2963:  bnei a2, 8, … ; j 0x2bc7    ; 0x08 PeManageSeed -> 0x2bc7
296c:  movi.n a3,9 ; bne a2,a3,… ; j 0x2baf  ; 0x09 LdweightsMX -> 0x2baf
2977:  bnei a2,10, … ; j 0x2bb7    ; 0x0a MatmulMX     -> 0x2bb7
…     (0x9f.. SEQ-core control opcodes follow)
2a70:  movi a3,228 (=0xe4) …       ; 0xe4 ConvLutLoad
```

So the **+4 MARIANA opcodes over CAYMAN** are byte-confirmed to be exactly
`{0x08 PeManageSeed, 0x09 LdweightsMX, 0x0A MatmulMX, 0xe4 ConvLutLoad}` — matching the ISA enum (§1)
and the DEBUG handler names. The dispatch **chain** decodes clean; the handler **bodies** the stubs
jump into FLIX-bundle and do not linearly decode (the WALL). The PE compute handlers are **SEQ-engine
handlers**, *not* entries in the POOL Q7 EXTISA `kernel_info_table`. `[HIGH/OBSERVED chain; MED bodies]`

---

## 3. The weight-load + matmul datapath

### 3.1 `Ldweights` (0x01, `S3_LW_STRUCT`) — load the stationary tile `[HIGH/OBSERVED]`

`Ldweights` reads a stationary weight tile from SBUF (`mem_pattern3d`, `AllowedInSBUF::True` /
`AllowedInPSUM::False`) and loads it into the systolic array as the **stationary** operand. In the
GPSIMD/IVP MAC datapath this is the `*XR8`/`*XR16` **XR reduce-register weight broadcast** (the
weight bus, [B04 §3](../isa/ref/b04-mac-integer.md)). The maverick `S3_LW_STRUCT` (compile-verified
`sizeof == 64`) carries `src_mem_pattern`@16, `in_dtype`@32, `fp32_mode`@33 (the TF32 select),
`num_active_rows`@34, `num_active_cols`@35, `tile_size`@36, `tile_sel`@37, `flags`@38
(`LD_WEIGHT_FLAGS`: `xpose_mode`, `scalar_mode`, `seed_mode`, `load_order`), `perf_mode`@39.

> **NOTE — `S3_LW` packs one byte tighter than `S3D3_MM`.** Because `Ldweights` has **no** `out_dtype`
> field (it produces no PSUM output), its tail is shifted one byte earlier than `Matmul`'s:
> `S3_LW.flags`@**38** / `perf_mode`@**39** vs `S3D3_MM.flags`@**39** / `perf_mode`@**40**. A
> reimplementer must NOT reuse one struct's offset table for the other — both are 64 B but the
> `tile_size`/`tile_sel`/`flags`/`perf_mode` tail sits at different offsets. `[HIGH/OBSERVED —
> compile-verified both structs this pass.]`

### 3.2 `Matmul` (0x02, `S3D3_MM_STRUCT`) — stream, MAC, accumulate `[HIGH/OBSERVED]`

`Matmul` streams the **moving** activation tile through the loaded array; each PE cell
multiply-accumulates; partial sums accumulate down the columns into PSUM, computing
`dst = stationary.T @ moving`. **Internal accumulation = FP32** (the PSUM banks); the `dst` dtype is
FP32 (all gens) or BF16 (v4+).

The maverick `S3D3_MM_STRUCT` field layout (compile-verified `gcc offsetof` this pass — the comment
column is the header's own byte-offset annotation):

```c
typedef struct NEURON_ISA_TPB_S3D3_MM_STRUCT {
    NEURON_ISA_TPB_HEADER        header;          // 0    (opcode=0x02 MATMUL)
    NEURON_ISA_TPB_EVENTS        events;          // 4
    NEURON_ISA_TPB_MEM_PATTERN3D src_mem_pattern; // 16   moving acts (SBUF / MX union)
    NEURON_ISA_TPB_DTYPE         in_dtype;        // 32
    NEURON_ISA_TPB_PE_FP32MODE   fp32_mode;       // 33   None/Low/High/Low_High
    NEURON_ISA_TPB_DTYPE         out_dtype;       // 34   FP32 (all) | BF16 (v4+)
    uint8_t                      num_active_rows; // 35
    uint8_t                      num_active_cols; // 36
    NEURON_ISA_TPB_TILE_SIZE     tile_size;       // 37   {row r64/r128, col c128/c256}
    NEURON_ISA_TPB_TILE_SEL      tile_sel;        // 38
    NEURON_ISA_TPB_MATMUL_FLAGS  flags;           // 39   {scalar, seed:2, accumulate:1, xpose}
    NEURON_ISA_TPB_MX_PERF_MODE  perf_mode;       // 40   QUAD_ROW / OCT_ROW (MX pump)
    NEURON_ISA_TPB_MEM_PATTERN3D dst_mem_pattern; // 48   PSUM
}; // sizeof == 64
```

### 3.3 The PSUM accumulate group + zero-region `[HIGH/OBSERVED]`

The accumulate group is the `MATMUL_FLAGS.accumulate_flag` plus the
`MATMUL_PSUM_ACCUMULATE_FLAGS` enum (`common.h:1335-1349`), with the derived modes pinned as
`static const` values:

```c
// NEURON_ISA_TPB_MATMUL_PSUM_ACCUMULATE_FLAGS (the bit flags):
BEGIN_MATMUL_GROUP       = (1 << 0);   // 0x1
END_MATMUL_GROUP         = (1 << 1);   // 0x2
BEGIN_ACCUM_MATMUL_GROUP = (1 << 2);   // 0x4
// the derived group modes (static const, common.h:1346-1349):
MULTI_MID   = 0;   // continue accumulating into the prior PSUM
MULTI_START = 1;   // begin (zero) a multi-matmul accumulate group
MULTI_END   = 2;   // commit the group
SINGLE      = 3;   // one-shot matmul (zero, compute, commit)
```

The PSUM zero-region selects how many physical banks the matmul clears
(`NEURON_ISA_TPB_MATMUL_ZERO_REGION`, `common.h:1350-1355`):

| enum value | clears | physical banks |
|---:|---|---:|
| `SIZE2048` = 0 | 2048 fp32 cells | **1 bank** |
| `SIZE4096` = 1 | 4096 cells | 2 banks |
| `SIZE8192` = 2 | 8192 cells | 4 banks |
| `SIZE16384` = 3 | 16384 cells | **8 banks** |

So PSUM = **8 physical banks × 2048 fp32 accumulator cells** — and the `2048`-per-bank figure is the
same count `PeManageSeed` manages (§4): one stochastic-rounding seed per accumulator cell of a bank.
The PSUM SBUF/PSUM region geometry is the [SBUF/PSUM bank model](../dma/sbuf-psum-banks.md)
(forward, Part 9). `[HIGH/OBSERVED enums; CARRIED region]`

### 3.4 The inner MAC — the IVP widening family `[HIGH/OBSERVED]`

On the GPSIMD/int-quantized path the PE engine carries the full **IVP widening-MAC** datapath
(re-read from the PE PERF IRAM census). The `*XR8`/`*XR16` suffix = the XR reduce-register **weight**
broadcast loaded by `Ldweights`; the other factor = the **moving activation** lane. The densest cores
(observed counts in parentheses):

| mnemonic | role |
|---|---|
| `ivp_mul4t2n8xr8` (64) | 4-TERM dot, i8 × XR8 — the i8 matmul core |
| `ivp_mul4tn16xr8` (35) | 4-TERM dot, i16 × XR8 |
| `ivp_mulpan16xr16` (21) | PAIR-accumulate MAC (`acc += a1·b1 + a2·b2`) |
| `ivp_mul4ta2n8xr8` (12) | 4-TERM accumulate, i8 × XR8 → 1536-bit `wvec` |
| `ivp_dmulq2n8…` / `ivp_dmulusq2n8xr8` | QUAD MAC (8 products) — the MX `OCT_ROW` pump |
| `ivp_packvr2nx24` / `ivp_packvru2nx24` | `wvec` pack/drain (the i8→24 accumulator) |

The **integer** accumulator is the 4-entry **1536-bit `wvec` regfile** (i8→24 / i16→48 / i32→96 per
lane, wrapping mod-`2^acc_w`, no saturate — see [datapath §3](simd-datapath.md)); the **TensorE float**
accumulator is the FP32 PSUM banks. The MAC family is gen-stable v4→v5 (the Maverick PE PERF IRAM
carries the same `ivp_mul4t*`/`mulpa*`/`dmulq*` set). `[HIGH/OBSERVED census]`

```c
// ---------------------------------------------------------------------------
// PE systolic weight-load -> matmul -> readout micro-op sequence.
// Real symbols/opcodes: LDWEIGHTS=0x01 (S3_LW_STRUCT), MATMUL=0x02 (S3D3_MM_STRUCT),
// PE_MANAGE_SEED=0x08 (S2S1D2_PE_SEED_STRUCT). MAC family ivp_mul4t*/ivp_packvr*.
// The firmware is the ORCHESTRATOR; the systolic array + PSUM banks are HW.
// ---------------------------------------------------------------------------

// One accumulate group: dst[col] = sum_k stationary[k,col] * moving[k]  (FP32 PSUM)
static void pe_matmul_group(const s3_lw_t   *lw,       // Ldweights descriptor
                            const s3d3_mm_t *mm_tiles, // a run of Matmul descriptors
                            int n_tiles)
{
    // 1) LDWEIGHTS (0x01): load the STATIONARY tile into the array as the XR weight bus.
    //    Reads SBUF (AllowedInSBUF only), in_dtype/fp32_mode select the multiplier path.
    pe_load_weights(lw);                              // "S: Ldweights" ; -> stub 0x2b86

    for (int t = 0; t < n_tiles; ++t) {
        const s3d3_mm_t *mm = &mm_tiles[t];
        // 2) zero-or-accumulate per the group flags (the decode-selected PSUM path):
        //    MULTI_START/SINGLE zero psum_zero_region banks; MULTI_MID/END read-modify-write.
        unsigned mode = (mm->flags.accumulate_flag) | psum_group_flags(mm);
        if (mode == SINGLE || mode == MULTI_START)
            psum_zero(mm->dst_mem_pattern, /*banks=*/zero_region_banks(mm));

        // 3) MATMUL (0x02): stream the MOVING tile, MAC into PSUM.
        //    Inner loop = the IVP widening MAC: XR=weight, vec=moving act, acc forwarded.
        //    per output col group: per contraction tile (K folded 2/4/8 at a time):
        //        ivp_mul4t*/mulpa*/dmulq*  (4-term / pair / quad), acc-chain II=1
        pe_stream_matmul(mm);                         // "S: Matmul : is_load=0" ; -> 0x2b8e

        // 4) on MULTI_END / SINGLE: commit the group; out_dtype (FP32 | BF16 v4+) drains.
        //    BF16 drain uses the PSUM stochastic-rounding RNG (seeds managed by op 0x08).
        if (mode == SINGLE || mode == MULTI_END)
            psum_commit(mm->dst_mem_pattern, mm->out_dtype);   // matmul_done_last -> SBUF response
    }
}
```

---

## 4. `PeManageSeed` (0x08) — the PSUM stochastic-rounding seeds

`[HIGH/OBSERVED]` `PeManageSeed` has its **own** 64-byte operand struct
(`S2S1D2_PE_SEED_STRUCT`, compile-verified) and manages the **PSUM fp32→bf16 stochastic-rounding RNG
seed state** — *not* a "PE-array per-cell PRNG". The struct (offsets from the header byte-comments,
compile-verified):

```c
typedef struct NEURON_ISA_TPB_S2S1D2_PE_SEED_STRUCT {
    NEURON_ISA_TPB_HEADER       header;               // 0   (opcode=0x08)
    NEURON_ISA_TPB_EVENTS       events;               // 4
    NEURON_ISA_TPB_TENSOR2D     src_seed_mem_pattern; // 12  LoadSeed: input seeds in SBUF (16x128)
    NEURON_ISA_TPB_TENSOR1D     identity_mem_pattern; // 24  the HW "magic" 16x16 identity tensor
    NEURON_ISA_TPB_DTYPE        identity_dtype;       // 32  BF16/FP16/FP32r/FP8
    NEURON_ISA_TPB_PE_SEED_MODE mode;                 // 33  None(0)/LoadSeed(1)/SaveSeed(2)
    uint8_t                     num_active_rows;      // 34  hard-coded 16
    uint8_t                     num_active_cols;      // 35  hard-coded 128
    NEURON_ISA_TPB_TENSOR2D     dst_seed_mem_pattern; // 36  SaveSeed: output seeds in PSUM (128x16)
    // reserved[…]                                    // 48..63 must be 0
}; // sizeof == 64
```

The header verbatim: **NeuronCore-v4 has 2048 seeds per PSUM, each 32-bit** (each PSUM partition has
16 stochastic-rounding states = 8 banks × 2 rounding/bank); `LoadSeed` shape is **16×128** in SBUF
partitions 0–15 (128/partition); `SaveSeed` shape is **128×16** in PSUM partitions 0–127 (16/partition)
— a **transpose is required** between save and reload. The `2048`-per-PSUM count is exactly the
`MATMUL_ZERO_REGION_SIZE2048` "one physical bank" figure (§3.3): one seed per fp32 accumulator cell.
`[HIGH/OBSERVED struct + header semantics]`

The `mode` enum is `NONE=0, LOAD_SEED=1, SAVE_SEED=2` (`PE_SEED_MODE`, `common.h:1288-1292`). The
seed rides the weight/matmul ports: the handler loads the **identity** matrix as the stationary
weight (`PeManageSeed : micro-op : LdWeight`) then runs a **transposing** Matmul
(`PeManageSeed : micro-op : Matmul : is_load=%d`) to move the 2048 seeds between SBUF and PSUM. The
same `seed_mode` field also rides `S3D3_MM.flags` and `S3_LW.flags` (the `MATMUL_FLAGS`/
`LD_WEIGHT_FLAGS` bitfields, `common.h:1380-1394`: `seed_mode : 2`). `[HIGH/OBSERVED]`

> **CORRECTION — the `s2s1d2_pe_seed.h` PROSE COMMENT is off-by-one; the ENUM is authoritative.** The
> struct header's comment block reads "LoadSeed (value of **0**)" and "SaveSeed (value of **1**)", but
> the actual `NEURON_ISA_TPB_PE_SEED_MODE` enum (read this pass at `common.h:1288`) is
> `NONE=0, LOAD_SEED=1, SAVE_SEED=2`. The enum is the wire contract; the prose comment is **stale**. A
> reimplementer must encode **LoadSeed=1, SaveSeed=2, None=0** (the enum), not the comment's 0/1. The
> firmware DEBUG strings `S: PeManageSeed(LOAD)` / `S: PeManageSeed(SAVE)` are the two non-`None`
> modes; the polarity is now pinned to the enum. `[HIGH/OBSERVED — enum vs comment diff this pass.]`

> **QUIRK — `PeManageSeed`'s in-array LdWeight/Matmul is the IDENTITY-matrix transport, not the matmul
> arithmetic.** The thing pushed through the array is the `identity_mem_pattern` 16×16 identity tensor
> (the header's "magic tensor required by HW to make load/save seed work"); the **seed vectors** live in
> SBUF (`src_seed_mem_pattern`, 16×128) and PSUM (`dst_seed_mem_pattern`, 128×16). The validity
> contract (`s3d3_mm_check_valid_load_seed_fields`) pins the micro-op shape: tile `{r64, c256}`, src a
> single element of value 1, `in_dtype` FP32, `fp32_mode` High, `xpose_mode` Enabled, `num_active_rows`
> 1, `num_active_cols` 0. `[HIGH/OBSERVED struct + validity; in-array routing INFERRED-HIGH.]`

---

## 5. The MX (microscaling) matmul datapath

`[HIGH/OBSERVED]` The MX path has **two mechanisms, per gen**:

* **v4 (MARIANA / MARIANA_PLUS): SEPARATE ops.** `LdweightsMX` (0x09, `SMX1_LW_STRUCT`) +
  `MatmulMX` (0x0A, `SMX1D3_MM_STRUCT`). The operand is an `MXMEM_PATTERN1D` carrying its **own**
  `scale_addr` (the out-of-band E8M0 per-block scale) + `data_addr` (+ indirect `index_addr`).
  `in_dtype ∈ {FP8_E4M3, FP8_E5M2, FP4_E2M1}`; the array is 128-row (`num_active_rows ∈ {128,64,32}`,
  `num_active_cols ≤ 128`); `dst → PSUM`. Both structs `sizeof == 64`.
* **v5 (MAVERICK): UNIFIED.** The MX mode folds into `Ldweights`(0x01)/`Matmul`(0x02). The header is
  explicit (`s3d3_mm.h:18`, read verbatim): *"when the source uses MXTensorV2 access pattern, the
  instruction operates in MX mode … This replaces the separate LdWeightMX/MatmulMX instruction from
  previous chips."* The `src_mem_pattern` is a `MEM_PATTERN3D` union `{t: TENSOR3D | i: INDIRECT16B |
  mx: MXTENSOR_V2}`; the `start_addr`'s ADDR4 marker (`mxtensorv2_pattern()` validity selector,
  `s3d3_mm.h:73`) selects the `mx` member with its `scale_addr`. The `perf_mode` is `MX_PERF_MODE`
  (`common.h:1396-1404`): `QUAD_ROW`(0x1) / `OCT_ROW`(0x4) + INTERLEAVE/TILED variants — the
  4×/8×-pumped MX matmul perf modes.

The DTYPE ordinals (read this pass, `common.h`): normal matmul `in_dtype ∈ {BFLOAT16=0x6, FP16=0x7,
FP32=0xA, FP8_EXP2=0x11(e2m5), FP8_EXP3=0xD(e3m4), FP8_EXP4=0xE(e4m3), FP8_EXP5=0xF(e5m2)}`;
MX `in_dtype ∈ {FP8_E4M3, FP8_E5M2, FP4_E2M1}` with the OCP-MX power-of-two scale `SFP8_E8=0x13`
(`FP8_S0E8M0`). The `fp32_mode` (`PE_FP32MODE`, `common.h:1281-1286`) is `None=0/Low=1/High=2/Low_High=3`
— the TF32-vs-FP32 mantissa select (FP32 input requires Low or High; `Low_High` is a two-pass full-FP32
split-mantissa accumulate). `[HIGH/OBSERVED struct + validity selector + DTYPE/enum ordinals]`

> **NOTE — the MX block-scale TAP is `[MED/INFERRED]`.** That the per-block E8M0 scale is applied **in
> the MAC datapath** (the IVP `mul*`/`dmulq*` family carries the packed-register scale operand) is
> `[HIGH/OBSERVED]`; whether the scale enters at the multiplier input vs at the PSUM drain is **not**
> byte-decodable from the FLIX-desynced handler interior — the array RTL is out of corpus. The MX
> compute paths are the [MX dequant kernel page](../firmware/kernels/mx-dequant.md) (forward, Part 5).

---

## 6. The PE↔SEQ handshake — the `arr_seq` CSR block

`[HIGH/OBSERVED — CSR-JSON field names read verbatim this pass]` Two CSR surfaces sequence the array
(the field names below are byte-exact from `tpb_arr_seq_top_host_visible.json` and
`tpb_arr_seq_cluster_host_visible.json`):

### 6.1 The host-visible config + telemetry (`tpb_arr_seq_top`)

The host window is **96% telemetry** — three per-tile perf banks and a tiny control surface:

* **`arr_seq_cfg` — the control surface:**
  * `queue_cfg.fifo_size_sel` — FIFO-full policy (full-on-fill vs single-entry).
  * `queue_cfg.bypass_peregwrite_instr` — `PeRegWrite` skips the SBUF fetch (reg-immediate).
  * `queue_cfg.disable_dependency_check` — queues become single-threaded (no inter-instr dependency
    check; serialises in-flight micro-ops).
  * `perf_cntr_cfg.{cntr_en, cntr_rst}` — perf-counter master enable / reset-pulse.
* **Three perf banks** named `{weight_load, matmul, pe_regwrite}` (verbatim in the JSON), byte-identical
  layout — **per-tile 64-bit instruction counters**, one per tile shape (128/64/32 × 128/64/32). So the
  sequencer counts `Ldweights`(0x01)/`Matmul`(0x02)/`PeRegWrite`(0x03) issue **per sub-tile** — the
  3-micro-op-class telemetry directly mirroring the PE micro-op set.

> **NOTE — the host window has NO precision/dtype/accumulate/dimension register.** There is no PSUM
> accumulate reg, no dtype reg, no array-dimension-select reg in the host CSR window — those are carried
> by the **micro-op descriptors** (this firmware's `S3_LW`/`S3D3_MM` structs), not host CSRs. The
> descriptor format is in the instruction stream; only the queue policy + the per-tile telemetry are
> host-visible. `[HIGH/OBSERVED CSR negative.]`

### 6.2 The cluster sequencer (`tpb_arr_seq_cluster`) — the matmul sequencing bits

The actual matmul/weight-load sequencing lives in the cluster sibling `arr_cluster_cfg` (field names
verbatim from the cluster JSON):

| field | role |
|---|---|
| `enable_wl_last_active_col` | set the last-active column for a weight-load's target column |
| `flush_p2f_fifos` | flush the PE-to-fabric response FIFOs |
| `matmul_done_last` | generate Matmul "DONE" on the **last** SBUF response (vs first) — the completion-notification control |
| `en_inter_instr_dly` + `inter_instr_dly_cnt` | inter-instruction response delay (reset 8) |
| `p2fifo_af` | p2f FIFO almost-full threshold |
| `array_stagger_…` | the array idle-timer max limit |

**The handshake:** the SEQ engine (this firmware) decodes the instruction stream and issues the
`LdWeight`/`Matmul`/`PeRegWrite` micro-op descriptors into the array's input FIFO; the array consumes
weights/activations from SBUF and writes partials to PSUM; **completion** is signalled by the matmul-done
on the SBUF response (`matmul_done_last` selects last-vs-first); dependency checking between in-flight
micro-ops is the queue's job (`disable_dependency_check` serialises). The descriptor format is
in-stream, NOT host CSRs. `[HIGH/OBSERVED CSR names; end-to-end flow INFERRED-HIGH from the CSR
semantics + the structs.]`

### 6.3 Per-gen struct + array evolution `[HIGH/OBSERVED]`

| field group | CAYMAN (v3) | MARIANA (v4) | MAVERICK (v5) |
|---|---|---|---|
| `S3_LW` / `S3D3_MM` flags byte | **none** (no `seed_mode`) | `MATMUL_FLAGS`@42 (+`seed_mode:2`, `accumulate_flag:1`) | re-packed: `flags`@39 + `tile_size`/`tile_sel`/`perf_mode` |
| src/dst mem-pattern | `TENSOR3D` | `MEM_PATTERN3D` (MX-capable union) | `MEM_PATTERN3D` (MX-capable union) |
| array tiling | `row_grp`/`col_grp` | `row_grp`/`col_grp` | `tile_size{r64/r128 × c128/c256}` + `tile_sel` |
| array columns | 128 | 128 | **256** (`c256` = two 128-col halves, double-pumped) |
| `PeManageSeed` / MX / `ConvLutLoad` | absent | present | present (MX unified) |

The `c256` double-pump is pinned by the `TILE_COL_SIZE` enum (`common.h:1318-1321`: `C128=7`=2⁷,
`C256=8`=2⁸) and the MM validity ("c256 → the upper dim of dst must have a size of 2, to hold MM output
from both PE col 0-127 and PE col 128-255"). So the **Maverick PE array is 256 columns**, run as two
128-col halves. `[HIGH/OBSERVED struct + enum diffs, compile-verified per gen.]` The per-`(gen×PE)`
carve bytes are the forward image pages [cayman-pe](../images/cayman-pe.md) /
[mariana-pe](../images/mariana-pe.md) / [maverick-pe](../images/maverick-pe.md) (Part 6).

---

## 7. The SP (`TPB_SP`, engine 4) + TOP_SP (engine 5)

`[HIGH/OBSERVED]` `TPB_SP` (engine 4) is the **leanest** `cayman/seq/` SEQ build — **18 handlers**,
the exact 5-way intersection of PE/ACT/POOL/DVE/SP (pure SYNC/CONTROL, **zero** compute handlers). The
18, re-extracted from the SP DEBUG-DRAM `S:` logs: `AluOp`, `BRANCH`, `BranchPrefetchHint`,
`Event_Semaphore`, `EXT_BREAK`, `Halt`, `INS_BREAK`, `INS_FL`, `MOVE`, `NOP`, `NOTIFY`, `POLL_SEM`,
`Redirect`, `SET_OM`, `STRONG_ORDER`, `TensorLoad`, `TensorStore`, `WRITE`. The dispatch loop is the
**same SEQ FSM** as the other four engines (the identical reset vector `06 76 00 00 = j 0x1dc`, the
identical boot trampoline `const16 a0,0x90; jx → enter_run @0x90`, the identical `.globstruct` magic
`0x6099cb34`, the same `start_ctrl`/`run_state` RUNNING=1/PAUSED=2 FSM).

### 7.1 The sync-primitive ops — ISA opcodes, not synthesised MMIO `[HIGH/OBSERVED]`

SP's sync handlers are **shared SEQ ISA opcodes** (present on all engines; SP is the engine whose
*entire* set is these). The opcodes (`common.h`) + operand structs (compile-verified `sizeof == 64`):

| opcode | enum (`…_OPCODE_…`) | struct | role |
|---:|---|---|---|
| `0xa0` | `EVENT_SEMAPHORE` | `CTRL_ES_STRUCT` | arrive / wait / update on the EVT_SEM array |
| `0xa1` | `HALT` | (run-state op) | engine pause / halt |
| `0xa2` | `DRAIN` | — | drain in-flight |
| `0xa3` | `INSTRUCTION_FLUSH` | — | IFIFO flush |
| `0xa6` | `NOTIFY` | `CTRL_NO_STRUCT` | emit notification record + optional IRQ |
| `0xb0` | `EVENT_SEMAPHORE_RANGE_CLEAR` | — | bulk event range-clear |
| `0xb3` | `POLL_SEM` | `CTRL_POLL_SEM_STRUCT` | the **TOP_SP** Min-fold poll accelerator |

**`Event_Semaphore` (0xa0, `CTRL_ES_STRUCT`)** — the arrive/wait/update primitive. Its
`NEURON_ISA_TPB_EVENTS` field (8 B, `common.h:469-474`) is
`{wait_mode, wait_idx, update_mode, update_idx, semaphore_value}`. `wait_idx`/`update_idx` are u8 (the
0..255 EVT_SEM index); `semaphore_value` is u32 (the 32-bit counter width). The instruction WAITS on
`semaphore[wait_idx]` per `wait_mode` (e.g. `WAIT_FOR_SEM_GE_IMM=0x5`, or `…_GE_REG=0x85`), then
UPDATES `semaphore[update_idx]` per `update_mode` (e.g. `SEM_INC_READ=0x3`, `SEM_DEC_READ=0x4`,
`SEM_ADD_IMM_READ=0x5`, the `_COMPLETE` twins `0x13`/`0x14`/`0x15`). `CTRL_ES` adds a `setter_signature`
(@12) and a second `events_extended` (12 B @32) for the two-stage wait/update. **This is the primitive
the collective barriers lower into.** `[HIGH/OBSERVED struct + WAIT/UPDATE enums.]`

> **NOTE — `WAIT_MODE_UNORDERED=0xfe`.** Beyond the SEM_{EQ,LT,LE,GT,GE}_{IMM,REG} compares, the
> `WAIT_MODE` enum (`common.h`) ships an `UNORDERED=0xfe` mode: the instruction waits on **no**
> condition and is not required to wait for prior-instruction wait conditions either (use when a direct
> semaphore read / watcher already proved it can run). It is "often faster than `None`" because it
> avoids hardware synchronisation through the semaphore block. A reimplementer sequencing independent
> sync ops should expose this. `[HIGH/OBSERVED.]`

**`POLL_SEM` (0xb3, `CTRL_POLL_SEM_STRUCT`)** — the TOP_SP accelerator, **named verbatim in the shipped
header** (`aws_neuron_isa_tpb_ctrl_poll_sem.h:21-22`, read this pass): *"a specialized semi-custom
operation to help Neuron accelerate polling semaphores on **TOP_SP**."* It does `n_read` (≤16)
consecutive u32 reads from `addr`, folds an ALU-op (Min) over them, and — if nonzero — writes `n_read`
copies of the result to `res_writeback_addr`, leaving the result in GPR `result`. The header's own
example: `n_read=6, op=Min, *addr=[8,3,5,4,4,10]` ⇒ `result=3`, `*res_writeback_addr=[3,3,3,3,3,3]` —
i.e. the all-reduce "select the minimum of N semaphores and decrement them all by that amount". Struct
fields (compile-verified): `n_read`@12, `result`@13 (`REG_NUM`), `op`@14 (`ALU_OP`), `addr`@16
(`NEURON_ADDR`, 8 B), `res_writeback_addr`@24. `[HIGH/OBSERVED struct + the verbatim TOP_SP attribution
— the single strongest tie of a sync OP to TOP_SP.]`

**`NOTIFY` (0xa6, `CTRL_NO_STRUCT`)** — emits a notification record (the per-engine SP EVT_SEM notif
type 0x17) and optionally an interrupt (the DEBUG strings `S: sending notification` /
`S: sending interrupt`). `CTRL_NO_STRUCT` also backs `ACTIVATION_TABLE_LOAD` / `NOP` / `HALT` /
`DRAIN` / `INSTRUCTION_FLUSH` (the `struct2opcode` map binds all six to `CTRL_NO_STRUCT`).
`[HIGH/OBSERVED struct2opcode]`

### 7.2 The barriers — no dedicated handler (pre-lowered) `[HIGH/OBSERVED]`

`CORE_BARRIER`(0xd8) / `SYNC_BARRIER`(0xd5) / `DMA_BARRIER`(0xc3) are **PSEUDO** opcodes — the header
(`common.h:275`) is explicit: *"Pseudo instructions are generated by compiler and translated into
non-pseudo HW instructions by NRT … upper three bits of the opcode equal `0b110`."* (Confirmed:
`PSEUDO_DMABARRIER=0xc3`, `PSEUDO_SYNC_BARRIER=0xd5`, `PSEUDO_CORE_BARRIER=0xd8` — all `0b110xxxxx`.)
The host/compiler (NRT) lowers them into concrete `Event_Semaphore`(0xa0) arrive/wait + `POLL_SEM`(0xb3)
HW ops **before** the stream reaches the SP sequencer; the SP dispatch table carries **no**
0xd8/0xd5/0xc3 row. So the SP's barrier participation IS its `Event_Semaphore`/`POLL_SEM` execution
against the EVT_SEM array — there is no "Barrier" kernel. `[HIGH/OBSERVED — pseudo-opcode classification
+ table absence.]`

### 7.3 TOP_SP + the collective-lowering execution `[HIGH structure / INFERRED on-core]`

`TOP_SP` (engine 5) is the standalone top-level Sync-Processor block (`TOP_SP_0..19`, 4 MiB container =
`TPB_EVT_SEM`(1M) + `RAM`(1M) + `TPB_SP`(1M) + `RESERVED`(1M)); it hosts the global 256-event/256-
semaphore EVT_SEM array + the `timestamp_inc` time-sync tick + its own NX core. Exactly **one** NX SP
firmware image family ships (`CAYMAN_NX_SP`); `engine_idx` is **runtime-computed** from
`engine_base_addr` at boot (the DEBUG string `engine_base_addr=%llx … engine_idx=%u`), so the same
build self-identifies as whichever SP slot it loads into. The three-way collective division of labour:

| actor | role |
|---|---|
| **`TPB_SP` (engine 4)** | the per-core **sync executor** — runs the lowered `Event_Semaphore`/`POLL_SEM`/`NOTIFY` stream on the shared per-TPB EVT_SEM (the firmware this page decodes) |
| **`TOP_SP` (engine 5)** | the per-NeuronCore **collective sequencer** — walks the host-built SPAD `cc_op` program, issues the DMA-tail increments + EVT_SEM ops, drives tsync, gates barriers |
| **SB2SB kernel (POOL/Q7)** | the **data plane** — the actual SBUF→SBUF iDMA copy ([sb2sb kernel](../firmware/kernels/sb2sb-remote-copy.md), forward) |
| **NCFW (mgmt core)** | the firmware whose per-`TOP_SP` context (`ncfw_ctx_top_sp`) the `TOP_SP` NX core executes |

The CCL barrier-lane → firmware-op mapping is pinned at the op level: `SemaphoreWrite`/arrive →
`Event_Semaphore`(0xa0) `update_mode SEM_INC/SET`; barrier wait → `Event_Semaphore`(0xa0)
`wait_mode SEM_GE` + `POLL_SEM`(0xb3) Min-fold; `CORE/SYNC/DMA barrier` → pre-lowered, no firmware
handler. `[HIGH/OBSERVED at the op-struct level; the on-NX-core `cc_op` WALK SCHEDULE is the firmware
boundary — INFERRED, no shipped LX/NX collective-schedule disassembler.]`

### 7.4 Per-gen SP evolution `[HIGH/OBSERVED]`

The SP firmware is **byte-stable across the Cayman class** (CAYMAN/MARIANA/MARIANA_PLUS — same
algorithm/encoding, relocated only). The sync-handler set GROWS over the gens (dispatch table walked,
base DRAM `0x6c4`): **CAYMAN** has only `Event_Semaphore` + `NOTIFY` real (POLL_SEM/SB2SB/RangeClear
fall to Bad-Opcode); **MARIANA** adds `SB2SB`(0x33e6) + `RangeClear`; **MARIANA_PLUS** adds the
`POLL_SEM`(0x2c66) Min-fold accelerator (previously TOP_SP-only) and merges ES/NOTIFY into one
trampoline. **MAVERICK** relocates SP code from IRAM to **SRAM** (the IRAM getter returns size 0; the
SP body lives in `MAVERICK_NX_SP_PERF_SRAM`). **SUNDA** is the older gen, RELEASE-only, at a different
CSR aperture (`0x00100000` window vs `0x04000000`). The per-`(gen×SP)` carve bytes are the forward image
pages ([cayman-sp](../images/cayman-sp.md) etc., Part 6). `[HIGH/OBSERVED dispatch tables + carve.]`

> **HW-vs-FW boundary (SP).** The 256-event/256-semaphore array, the atomic inc/dec read-modify-write,
> the GE/Min compare, and the cross-die routing are **HARDWARE** (the EVT_SEM unit + the ISA-op
> execution). The firmware does NOT compute semaphore arithmetic; it **issues** the ops (which the HW
> EVT_SEM block performs) and **spin-polls** the HW status (the wait worker) until the wait condition is
> met, parking in PAUSE (`run_state=2`) on a long wait. `[HIGH structure.]`

---

## 8. The ACT activation-PWL quad + the MAVERICK ACT→DVE fold

`[HIGH/OBSERVED]` The ACT engine (idx 1) runs the **activation-PWL apply** path. This section keeps the
framing **identical** to the [Activation + Transcendental Table Engine](activation-transcendental-tables.md)
page — that page is the *table-HW* view (Engine B), this is the *per-engine micro-op + fold* view.

### 8.1 The ACT opcode quad + the two support ops `[HIGH/OBSERVED]`

Read from `common.h` + `struct2opcode` (`jq`, this pass):

| opcode | enum | struct | role |
|---:|---|---|---|
| `0x21` | `ACTIVATE` | `S3D3_AC` | scale+bias+act_func, 3D, accumulator_cmd |
| `0x22` | `ACTIVATE_QUANTIZE` | `S3D3_AQ` | act + inline requantize → UINT8 |
| `0x23` | `ACTIVATION_TABLE_LOAD` | `CTRL_NO` | the PWP-table DMA install (`LoadActFuncSet`) |
| `0x24` | `ACTIVATION_READ_ACCUMULATOR` | `D1_RD` | drain the per-lane fp32 ACT accumulator |
| `0x25` | `ACTIVATE2` | `S2D2_AC` | fused act + dual-ALU + reduce, 2D `[v4+]` |
| `0x26` | `ACTIVATE_MULTIPASS` | `S1S2D2_AM` | act + prev-pass 1D accumulator, 2D `[v5]` |

### 8.2 The PWP table format (Engine B, recap) `[HIGH/OBSERVED]`

The activation table is a **piecewise-CUBIC** (PWP = Piece-Wise Polynomial) machine, **not** a linear
breakpoint/slope/intercept PWL — four tables, compile-verified `sizeof` **32/128/32/32** B across all 5
gens (`tpb_activation_entries.h`):

* **CAM** (32 B) — `{opcode:8, func_id:8, opcode_mask:8, func_id_mask:8, valid:1}` matches
  `(opcode, func_id)` → selects the PROFILE/CONTROL/BUCKET slot.
* **CONTROL** (32 B) — the fp value → bucket-INDEX extract: `{act_tbl_base:11, extract_lsb:5,
  extract_size:4}`. The HW extracts `extract_size` bits at `extract_lsb` from the fp bit pattern,
  offset by `act_tbl_base` — a **log-spaced** (exponent+mantissa-MSB) segmentation.
* **BUCKET** (32 B) — the per-segment **cubic** `{float d0@0, d1@4, d2@8, d3@12, x0@16}`. Eval (local
  coord `t = x − x0`): `func(x) ≈ d0 + d1·t + d2·t² + d3·t³`. The `d2`/`d3` terms are real and
  compile-verified present (a linear PWL would carry `{d0,d1,x0}` only).
* **PROFILE** (128 B) — the per-function config: region split (small/large × pos/neg), `symmetry_opt`
  (evaluate one half + reflect, for odd/even funcs), `exponent_offset`, bias/scale fuse, FMA control,
  and `func_rslt_for_{zero,nan,pos_inf,neg_inf}` (the exact fp32 results the HW substitutes for the
  asymptotes so the PWL never approximates the saturating tails).

The `activation_func` field is a **raw uint8 index** into the loaded PWP table — there is **no**
ISA-named activation enum; the host loads relu/gelu/sigmoid/tanh/exp/… as PWP table DATA via
`0x23 ACTIVATION_TABLE_LOAD`, and that per-function coefficient CONTENT is **out-of-corpus** (the
host-supplied-content wall). For the full table-HW treatment + the cross-gen sha / SUNDA delta, see the
[table-engine page §2](activation-transcendental-tables.md). `[HIGH/OBSERVED format; LOW/not-claimed
content.]`

### 8.3 The `Activate2` (0x25, `S2D2_AC`) apply/fusion path `[HIGH/OBSERVED struct; MED order]`

`S2D2_AC` (compile-verified `sizeof == 64`; `activation_func`@35, `reduce_cmd`@26) carries a dual
TensorScalar ALU pair (`op0`@29, `op1`@30, drawing from the general `ALU_OP` table) + 3 immediates
(`imm0`@36, `imm1`@40, `relu_param`@44) + a `reduce_cmd`/`reduce_op`@31 + the `activation_func`@35 PWL
index. The `ALU_OP` table (`common.h:1189+`): `BYPASS=0x00, ADD=0x04, SUBTRACT=0x05, MULT=0x06,
DIVIDE=0x07, MAX=0x08, MIN=0x09, RE_LU=0x22, SQUARE=0x23`. The `REDUCE_CMD` enum (`common.h:948-953`):
`IDLE=0, RESET=1, REDUCE=2, RESET_REDUCE=3`. The fused 2D pass, by field group:

* **STAGE A (dual-ALU affine):** `src ⊗op0 imm0 ⊗op1 imm1` — `op0=MULT, op1=ADD` ⇒ the classic affine
  `scale·x + bias`; other op pairs give clamp/min/max/square. `reverse_operands` (@60) swaps the
  scalar/tensor operand order.
* **STAGE B (PWL func):** `activation_func` indexes the PWP quad (§8.2); `relu_param` is the
  parametric-ReLU negative slope (the `relu_param_src`@24 comment "byte 24-27 → RTL imm12").
* **STAGE C (reduce):** `reduce_cmd`/`reduce_op` folds the func outputs into the ACT per-lane running
  accumulator (drained by `0x24 ACTIVATION_READ_ACCUMULATOR`). `RESET` zeroes it; `RESET_REDUCE`
  zeroes-then-accumulates.

> **NOTE — the affine→PWL→reduce ORDER is `[MED/INFERRED]`.** The stage *identity* of each field group
> is `[HIGH/OBSERVED]`, but the precise A→B→C ordering is the natural reading of the field groups + the
> base-`Activate` `OUT=func(scale·x+bias)` (affine PRE func) + the ACT-accumulator POST-func reduction —
> the device body is FLIX-desynced and **not** byte-pinned. `[MED/INFERRED ordering — the FLIX body
> WALL.]`

> **NOTE — `0x26 ACTIVATE_MULTIPASS` is fully IMAGE-DORMANT.** "Multipass" appears **0** times in the
> firmware; "Activate2" appears exactly **2** (`RS: Activate2`, both in the MARIANA(_plus) ACT band).
> `0x26` is spec-present (enum + `S1S2D2_AM` struct + validator: `prev_pass_mem_pattern` TENSOR1D@40,
> SBUF-only) but has **no** firmware handler and is emitted by no shipped kernel. `[HIGH/OBSERVED string
> counts; MED "compiler-but-unused" reading.]`

### 8.4 The MAVERICK ACT→DVE fold — a HW-region migration `[HIGH/OBSERVED region; INFERRED interior]`

On **cayman/mariana (v3/v4)** the activation PWL tables are an **ACT-engine** block
(`TPB_n_ACT_{PROFILE_CAM, PROFILE_TABLE, BUCKET_TABLE, CONTROL_TABLE}` + `LOCAL_STORAGE` shadows). On
**MAVERICK (v5) there is NO ACT engine block at all** — the PWL SRAM physically MIGRATED into the
**TPB_DVE** block. From `maverick/vpc-mirror/arch-regs/.../TPB_DVE.json` (read this pass; keep this
table identical to the [table-engine page §2.5](activation-transcendental-tables.md)):

| block | AddressOffset | size | note |
|---|---|---|---|
| `DVE_PROFILE_CAM` | `0x8D000` | `0x1000` | the DVE HW-decode profiler |
| `DVE_PROFILE_TABLE` | `0x8E000` | `0x2000` | (every engine has this) |
| `ACT_CONTROL_TABLE` | `0xA0000` | `0x2000` | |
| `PWP_CONTROL_TABLE` | `0xB0000` | `0x10000` (64 KiB) | the relocated CONTROL |
| `PWP_BUCKETS_TABLE` | `0xC0000` | `0x80000` (512 KiB) | the relocated BUCKET cubic pool |

So the **v5 fold is a real hardware-region migration**: the DVE engine HOSTS the activation PWL
datapath, not just its schedule. The scheduling shadow is consistent — on Maverick the named ACT
handlers are absent firmware-wide; the DVE `PROF_CAM` arms `0x23 ACTIVATION_TABLE_LOAD` + `0x25
ACTIVATE2` (which the MARIANA DVE PROF did not), and the read-accumulator is re-expressed as DVE-native
`DveReadAccumulator`(0x9b). The PWP table FORMAT is byte-identical across the fold (sha `8f6f5f49…`
cayman..maverick). The per-`(gen×ACT/DVE)` carve bytes are the forward image pages
[maverick-act](../images/maverick-act.md) (the fold) / [maverick-dve](../images/maverick-dve.md)
(absorbs ACT) (Part 6). `[HIGH/OBSERVED region migration; the v5 PWP eval micro-sequence INFERRED — the
Maverick-interior WALL.]`

---

## 9. CSR live-usage — who programs QoS / REMAPPER / NSM / PMU, and when

`[HIGH/OBSERVED]` The CSR-schema lane found four FIS sprot peripheral blocks (QoS shaper, REMAPPER CAM,
NSM, QoS PMU) by **address-map placement**; the question this section answers is *who programs them,
when, and with what*. The one-line model: of the four, **exactly one** has an in-corpus programmer — the
**REMAPPER**, driven by the **host** runtime per memory-region. The QoS shaper, NSM, and PMU are all
left at their reset (transparent / bypassed / disabled) posture by every in-corpus actor.

| block | WHO programs it (in-corpus) | WHEN | WHAT |
|---|---|---|---|
| **REMAPPER** (CAM) | **HOST** runtime (`libnrt aws_hal_sprot_config_remap_entry_internal_<gen>`) | per **memory-region** at mem-ref staging (`use_sprot` set) | write a CAM entry (6 `wr_buf` words) {57b masked addr + AXI-ID + pass/remap/valid}; clear on teardown |
| **QoS shaper** (`qos_prot`) | **NOBODY** in-corpus | n/a (boots transparent) | left at reset (`chicken=1` ⇒ block transparent) |
| in-engine **UDMA AxQOS** (NOT FIS) | HOST runtime (`aws_hal_<gen>_sdma_set_udma_qos`) | **BOOT** (`tdrv_init`) + **PER-COLLECTIVE** (`set_collectives_dma_queues_props`) | `tdma_model.udma_qos_ctrl` m2s/s2m AxQOS + AWQOS/ARQOS (val `0x3`) |
| **NSM** (AXI mon) | **NOBODY** in-corpus | n/a (boots bypassed) | left at reset (`bypass.enable=1` ⇒ monitor off); armed by out-of-corpus mgmt-core boot FW |
| **QoS PMU** (`qos_pmu`) | **NOBODY** in-corpus | n/a (boots disabled) | left at reset (`event_select=0` ⇒ all 8 counters off); debug tooling (out-of-corpus) would arm |

### 9.1 The REMAPPER — host-programmed per memory-region `[HIGH/OBSERVED]`

`libnrt.so` (host runtime, DWARF) carries the named CAM-entry writer
`aws_hal_sprot_config_remap_entry_internal_{mariana@0x466130, sunda@0x46cbe0}` (+ the
`_clear_entry`/`_logical_memory`/`_logical_addr_base` family). It writes **six consecutive 32-bit
words** via `al_reg_write32` at HAL-handle base `+0x130..0x144` (the indirect CAM `wr_buf_0..5`
window), with `wr_buf_5.wr_index` selecting the destination entry and the value math
(`shl $0x19` valid/control bit; `aws_hal_get_local_bits` 57-bit physical address; `shr $0xc`
page-align; `or $0xf4000000` wr_index + pass/valid top byte). The trigger is the memory-reference
staging path (`mem_ref_copy_and_stage_mr@0x2fb780`, the `use_sprot=%u` gate); **WHEN** = per registered
region that needs a logical→physical scratchpad remap, NOT at boot and NOT per-collective.

> **NEGATIVE — neither the device NX FW, NCFW, nor the DKMS driver programs the remapper.** The device
> `libnrtucode.a` has 0 remap/sprot/qos tokens; `libncfw.so` 0; the DKMS driver has only a comment. The
> REMAPPER CAM is host-runtime-programmed **exclusively**. `[HIGH/OBSERVED across all four corpora.]`

### 9.2 The QoS shaper is left at reset; the UDMA AxQOS is what the host DOES program `[HIGH/OBSERVED]`

No in-corpus writer touches `qos_prot`'s shaper fields (chicken / outstanding-txn limits / the 15 LFSR
stallers / fairness). It boots `chicken=1` (transparent — all shaping disabled) and stays so. What the
host **does** program is the **in-engine UDMA AxQOS** sideband — a *different* block:
`aws_hal_mariana_sdma_set_udma_qos@0x4650c0` writes the `tdma_model.udma_qos_ctrl` m2s/s2m AxQOS +
UDMA AWQOS/ARQOS shift fields, called at **BOOT** (`tdrv_init@0x26a310`) and **per-collective**
(`set_collectives_dma_queues_props@0x22c9e0`, passing the literal priority `0x3`). So the "outstanding-txn
limit" lives in `qos_prot` and is **never** programmed in-corpus; the "priority" IS set, but as the
engine AxQOS at boot+per-collective, not as `qos_prot` fairness. `[HIGH/OBSERVED callgraph edges + the
0x3 value.]`

### 9.3 NSM + PMU — consumer/debug only `[HIGH absence; consumer flow CARRIED]`

**NSM** (the AXI Network-Security Monitor) has no in-corpus writer; it boots BYPASSED (`bypass.enable=1`).
It is a **consumer-driven** block — its violation drives the PCIe isolation SM and raises the
`intr_peb_nsm_axi_timeout` critical IRQ; the only NSM writes that happen at all are the **fault-path
recovery** writes (`reset_staging_fifo` drain, `cfg_clear`) by the Q7/management-core ISR, and the
**arming** writes by the out-of-corpus secure boot FW — neither ships. **QoS PMU** has no in-corpus
writer; it boots DISABLED (`event_select=0`). The runtime's `nrt_sys_trace_*` targets a separate SW
event RING (host-RAM), not the qos_pmu AXI-beat PMU CSRs; the hardware PMU is left to out-of-corpus
debug tooling. `[HIGH/OBSERVED absence; NSM consumer flow CARRIED from the interrupt cause tables.]`

---

## 10. Adversarial self-verification — the five strongest claims, re-challenged

Each headline claim re-tested against the binary / header / JSON this pass; a claim survives only if a
second independent witness agrees.

1. **The PE micro-op sequence + opcodes (`Ldweights 0x01` … `MatmulMX 0x0A`, `ConvLutLoad 0xe4`).**
   *Challenge:* could the opcode numbers be stale or the struct binding wrong? *Re-test:* the
   `NEURON_ISA_TPB_OPCODE` enum (`common.h:158-167,310`) gives `LDWEIGHTS=0x01 … MATMUL_MX=0x0A,
   CONV_LUT_LOAD=0xe4` byte-exact; the `struct2opcode` JSON independently binds `S3_LW_STRUCT→LDWEIGHTS`,
   `S3D3_MM_STRUCT→MATMUL`, `S2S1D2_PE_SEED_STRUCT→PE_MANAGE_SEED`, `SMX1_LW→LDWEIGHTS_MX`,
   `SMX1D3_MM→MATMUL_MX`; the MARIANA PE dispatch chain byte-decodes `bnei a2,1/2/3/6/7/8 + movi a3,9/10`
   to the same set; the DEBUG strings name them. **Triple-witnessed. Survives.** `[HIGH/OBSERVED]`

2. **The `arr_seq` PE↔SEQ handshake (3 per-tile perf banks + the cluster `matmul_done_last`
   completion).** *Challenge:* are the field names real or paraphrased? *Re-test:* `rg` of
   `tpb_arr_seq_top_host_visible.json` returns `weight_load`, `matmul`, `pe_regwrite`, `queue_cfg`,
   `fifo_size_sel`, `bypass_peregwrite_instr`, `disable_dependency_check`, `perf_cntr_cfg`, `cntr_en`,
   `cntr_rst` verbatim; `tpb_arr_seq_cluster_host_visible.json` returns `matmul_done_last`,
   `enable_wl_last_active_col`, `flush_p2f_fifos`, `en_inter_instr_dly`, `inter_instr_dly_cnt`,
   `p2fifo_af`, `array_stagger`. **All present verbatim. Survives.** `[HIGH/OBSERVED]`

3. **The SP/TOP_SP sync ops (`Event_Semaphore 0xa0`, `POLL_SEM 0xb3` = the TOP_SP Min-fold).**
   *Challenge:* could `POLL_SEM` be a generic poll, not TOP_SP-specific? *Re-test:* the opcodes are in
   the enum (`EVENT_SEMAPHORE=0xa0`, `NOTIFY=0xa6`, `EVENT_SEMAPHORE_RANGE_CLEAR=0xb0`, `POLL_SEM=0xb3`);
   the `ctrl_poll_sem.h` header comment reads *"accelerate polling semaphores on TOP_SP"* verbatim with
   the `[8,3,5,4,4,10]→3` Min-fold example; the barrier pseudo-opcodes `0xc3/0xd5/0xd8` carry the
   `0b110`-upper-bits pseudo classification. **Survives** (verbatim TOP_SP attribution). `[HIGH/OBSERVED]`

4. **The ACT PWL quad (cubic BUCKET) + the Maverick ACT→DVE HW-region fold.** *Challenge:* could `d2`/`d3`
   be padding, or the fold be only a schedule change? *Re-test:* `tpb_activation_entries.h` reads
   `float d0,d1,d2,d3,x0` at offsets 0/4/8/12/16 (a linear PWL would carry `{d0,d1,x0}` only);
   `sizeof` 32/128/32/32 compile-verified all gens; the maverick `TPB_DVE.json` carries
   `ACT_CONTROL_TABLE`@0xA0000 + `PWP_CONTROL_TABLE`@0xB0000 + `PWP_BUCKETS_TABLE`@0xC0000 **inside** the
   DVE block with **no** ACT engine block on v5 — a physical SRAM migration, not a schedule. **Survives.**
   `[HIGH/OBSERVED]`

5. **The CSR programmers (REMAPPER = host per-region; QoS/NSM/PMU = unprogrammed).** *Challenge:* could a
   device-FW or NCFW writer exist that the sweep missed? *Re-test:* `libnrt.so` carries the named
   `aws_hal_sprot_config_remap_entry_internal_<gen>` writer (`base+0x130..0x144` 6-word CAM window) +
   the `use_sprot` mem-ref-staging trigger; the cross-corpus negative is 0 sprot/qos/nsm tokens in
   `libnrtucode.a`, 0 in `libncfw.so`, DKMS comment-only; `qos_prot`/NSM/PMU boot
   `chicken=1`/`bypass=1`/`event_select=0` with no in-corpus clear. **Survives.** `[HIGH/OBSERVED
   presence + cross-corpus absence]`

No claim here rests on an unnamed symbol or a single uncorroborated witness: every opcode is enum +
JSON + (where carved) dispatch-chain + DEBUG-string witnessed; every struct is compile-verified; every
CSR field is read verbatim from the RTL JSON; the FLIX-body interiors and the Maverick interiors are
flagged `[MED]`/`[INFERRED]`, never asserted as freshly-decoded fact.

---

## 11. Confidence ledger

**HIGH / OBSERVED (read / compile-verified / disassembled / CSR-JSON-read this pass):**

* The PE opcode roster byte-exact (`LDWEIGHTS=0x01 … MATMUL_MX=0x0A, CONV_LUT_LOAD=0xe4`) from the ISA
  enum + the `struct2opcode` JSON + the MARIANA PE dispatch chain + the DEBUG handler strings.
* The PE operand structs compile-verified `sizeof == 64` with per-gen byte-offsets (the
  CAYMAN-no-flags → MARIANA-`+MATMUL_FLAGS seed_mode` → MAVERICK re-pack `tile_size/tile_sel/perf_mode`,
  `c256` 256-col double-pump evolution); the PSUM accumulate enums (FLAGS BEGIN/END/BEGIN_ACCUM; modes
  SINGLE=3/START=1/MID=0/END=2; ZERO_REGION 2048..16384 = 1..8 banks); TILE_SIZE/SEL (r64/r128 ×
  c128/c256); FP32MODE; MX_PERF_MODE (QUAD/OCT_ROW); the DTYPE ordinals.
* `PeManageSeed`'s own `S2S1D2_PE_SEED_STRUCT` (PSUM SR-RNG seeds: 2048/PSUM, 32-bit; Load 16×128 SBUF /
  Save 128×16 PSUM; transpose between); the enum `NONE=0/LOAD_SEED=1/SAVE_SEED=2`; the
  comment-vs-enum off-by-one CORRECTION.
* The MX path two-mechanism (v4 separate `0x09/0x0A` + out-of-band E8M0 `scale_addr`; v5 unified into
  `0x01/0x02` via the `mxtensorv2_pattern()` selector); the `SFP8_E8=0x13` MX scale dtype.
* The `arr_seq` handshake CSR field names verbatim (3 perf banks `{weight_load,matmul,pe_regwrite}`;
  `queue_cfg.{fifo_size_sel,bypass_peregwrite_instr,disable_dependency_check}`; cluster
  `{matmul_done_last,enable_wl_last_active_col,flush_p2f_fifos,en_inter_instr_dly,inter_instr_dly_cnt,p2fifo_af,array_stagger}`).
* The engine enum (`PE=0/ACT=1/POOL=2/DVE=3/TPB_SP=4/TOP_SP=5`); the SP 18-handler intersection; the SP
  sync ops + structs (`CTRL_ES`/`CTRL_POLL_SEM`/`CTRL_NO`, the `EVENTS` field, the WAIT/UPDATE enums incl.
  `WAIT_FOR_SEM_GE_IMM=0x5`/`…GE_REG=0x85`/`UNORDERED=0xfe`); the verbatim `POLL_SEM`→TOP_SP header
  attribution; the barrier pseudo-opcodes `0xc3/0xd5/0xd8` (`0b110` upper bits).
* The ACT opcode quad `0x21..0x26`; the PWP quad format (CAM/PROFILE/CONTROL/BUCKET 32/128/32/32,
  BUCKET cubic `{d0,d1,d2,d3,x0}`, CONTROL `{act_tbl_base:11,extract_lsb:5,extract_size:4}`,
  REDUCE_CMD/ALU_OP); the `S2D2_AC` field order; the `0x26` image-dormancy (Multipass 0 / Activate2 2).
* The MAVERICK ACT→DVE fold as a HW-region migration (`ACT_CONTROL_TABLE`/`PWP_CONTROL_TABLE`/
  `PWP_BUCKETS_TABLE` inside `TPB_DVE` at `0xA0000`/`0xB0000`/`0xC0000`; no ACT engine block on v5).
* The CSR live-usage (REMAPPER host-programmed per memory-region via `aws_hal_sprot_config_remap_entry_*`
  `base+0x130..0x144`; the UDMA AxQOS at boot + per-collective; the cross-corpus negative for
  qos_prot/NSM/PMU + their reset postures).

**MED / INFERRED:**

* The exact in-handler micro-op issue order (the LdWeight/Matmul/MX/seed handler **bodies** FLIX-desync
  under the linear sweep — the dispatch chain + opcode→stub + struct contract are HIGH, the byte-exact
  body schedule is MED).
* The MX block-scale TAP (multiplier-input vs PSUM-drain) — IVP MAC family + MXTensorV2 scale OBSERVED;
  the precise HW tap is array RTL.
* The `Activate2` fusion ORDER (affine → PWL → reduce) — field-group reading, not device-body byte-pinned.
* The on-NX-core `cc_op` collective walk schedule (executes on the TOP_SP/NX core; no shipped
  collective-schedule disassembler).
* The Maverick PE/SP/DVE **interiors** (the ACT→DVE fold is OBSERVED at the HW-region level; the v5 PWP
  eval + the stripped-name handlers are INFERRED).

**LOW / NOT CLAIMED:**

* The host-supplied per-function PWP table CONTENT (relu/gelu/sigmoid/… cubic coefficients) — the
  host-content wall.
* The PE-array converter-table SRAM geometry (`ConvLutLoad` target — out of corpus).
* The PE matmul HW cycle cost (the systolic fill/drain latency — silicon, no NKI cost formula).
* The exact armed VALUES of qos_prot limits / NSM isolation rules / qos_pmu event-selects (no in-corpus
  writer; only the reset values are known).

---

## Cross-references

* [The SIMD Compute-Datapath](simd-datapath.md) — the QUAD-MAC array + the 1536-bit `wvec` accumulator
  the PE int MAC feeds, and the Booth/PP/carry-save multiply structure behind `Matmul`.
* [Activation + Transcendental Table Engine](activation-transcendental-tables.md) — the table-HW (Engine
  B) view of the ACT PWL quad + the identical Maverick ACT→DVE fold framing this page upgrades to the
  per-engine micro-op level.
* [ISA Batch 04 — Integer MAC](../isa/ref/b04-mac-integer.md) / [B05 — Mixed MAC](../isa/ref/b05-mac-mixed.md)
  — the per-instruction view of the IVP widening-MAC family the PE inner loop carries.
* [ISA core — Coverage Tally](../isa/core/coverage-tally.md) — the full Vision-Q7 opcode census.
* [On-Chip SBUF + PSUM Bank Model](../dma/sbuf-psum-banks.md) *(Part 9 — forward)* — the SBUF/PSUM
  geometry the weight/activation reads and the FP32 accumulate target.
* [PE Matrix-Multiply Path (LdWeight/Matmul/ManageSeed)](../firmware/kernels/pe-matmul.md) *(Part 5 —
  forward)* — the PE matmul handler-body trace this page's roster anchors.
* [SB2SB Remote-Copy Collective Kernel](../firmware/kernels/sb2sb-remote-copy.md) /
  [MX (Microscaling) Dequant Compute Paths](../firmware/kernels/mx-dequant.md) /
  [Activate + the PWL Application Mechanism](../firmware/kernels/activate-pwl.md) *(Part 5 — forward)* —
  the collective data plane, the MX dequant, and the `0x23` PWP install paths.
* [CAYMAN × PE image](../images/cayman-pe.md) / [MARIANA × PE image](../images/mariana-pe.md) /
  [MAVERICK × PE image](../images/maverick-pe.md) + [MAVERICK × ACT (the fold)](../images/maverick-act.md)
  / [MAVERICK × DVE (absorbs ACT)](../images/maverick-dve.md) *(Part 6 — forward)* — the per-`(gen×engine)`
  carve bytes.
* [The Confidence & Walls Model](../reference/confidence-model.md) — the tag system, the FLIX-body wall,
  and the host-supplied-content wall this page rides.
