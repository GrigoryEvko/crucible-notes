# MAVERICK (NC-v5) Profile + Maximal-Observed Carve

This is the **single authoritative MAVERICK document** — the consolidated v5
generation profile that rolls up the five committed MAVERICK image pages
([ACT→DVE fold](../images/maverick-act.md), [DVE](../images/maverick-dve.md),
[PE](../images/maverick-pe.md), [POOL](../images/maverick-pool.md),
[SP](../images/maverick-sp.md)) into one identity / topology / ISA / reset / wall
profile, and pushes the thinnest generation from "ISA-headers-only" toward OBSERVED
wherever the shipped v5 arch-ISA headers, the customop-lib v5 firmware images, and a
native `ncore2gp` disassembly of those images permit. Every fact here is consistent
with the five image pages and re-grounded against the binary.

MAVERICK is the **fifth, newest** GPSIMD codename — `coretype 37` (OBSERVED),
`arch_id 36` (INFERRED), NC-v5 / `NeuronCoreVersion::V5` — the "Cairo" Vision-Q7 NX
8-core Q7-POOL GPSIMD/DVE cluster. It ships **only** in the clang-15 internal twin
`libnrtucode_internal.so` (sha256 `b7c67e89…632fc329b`); every shipped runtime path
(`libncfw`, the front `libnrtucode.so`, the EXTISA container, the static
`libnrtucode.a`) tops out at MARIANA_PLUS with **0** MAVERICK references.

> **THE v5 EPISTEMIC WALL — read before every claim below.**
> **MAVERICK (v5) is HEADER-OBSERVED only.** What IS `OBSERVED` (byte-grounded this
> pass): the carved `S:`/`P%i:` rosters, the getter counts + sizes + sha256s, the
> reset/boot bytes (decoded with `ncore2gp`), the EXTISA `kernel_info_table` entry
> counts + `(opcode,spec)` key sets, the PROF-CAM armed sets, the shipped
> arch-ISA **opcode-enum values** and the `get_ext_isa` `coretype 37` dispatch case.
> What stays **`INFERRED`**: every v5 *interior* — per-opcode→handler-body bindings,
> full-body decode (FLIX-VLIW desync frontier, FW-00), `arch_id 36`, the v5 Q7
> geometry / CSR programmer / run-stall / DKL, the v5 D2D transport IP. Never state a
> v5 interior as fact. Confidence is `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` per the
> [Confidence & Walls Model](../reference/confidence-model.md). [WALL]

> **PROVENANCE.** Every fact derives **solely** from static analysis of the shipped
> binaries with stock binutils (`nm`/`objdump`/`readelf`/`ar`/`dd`/`xxd`/`strings`/
> `sha256sum` + `python struct`) and the shipped Cadence Xtensa toolchain
> (`xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp`, GNU Binutils 2.34.20200201 / Xtensa
> Tools 14.09, uarch Cairo). Opcode/enum/struct naming is read from the shipped
> `neuron_maverick_arch_isa` clean C ISA headers (binary-derived, citeable). Lawful
> interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)); no vendor source
> snapshot referenced.

Related pages: [MAVERICK × ACT (the fold)](../images/maverick-act.md) ·
[MAVERICK × DVE](../images/maverick-dve.md) · [MAVERICK × PE](../images/maverick-pe.md) ·
[MAVERICK × POOL (dual-core)](../images/maverick-pool.md) ·
[MAVERICK × SP](../images/maverick-sp.md) ·
[Codename Cross-Walk caveat](../reference/codename-crosswalk.md) ·
[Codename ↔ Generation Map](./codename-generation-map.md) ·
[Master Capability Matrix](./master-capability-matrix.md).

---

## 1. Identity — `coretype 37` OBSERVED, `arch_id 36` INFERRED (the wall)

The single most important discipline on this page is the **identity split**: the
device coretype is OBSERVED; the NCFW arch_id is a `coretype−1` extrapolation that
**no byte confirms**. Carry it as a wall, never present `36` as binary-observed.

| identifier | value | conf | evidence |
|---|---|---|---|
| **coretype** | **37** | **HIGH/OBSERVED** | `nrtucode_get_num_ext_isa_libs` (`.text 0x9b2c90`): `cmp $0x25,%edi` (`0x25=37`) range-guard + `movabs $0x2020202000` mask (bits {13,21,29,37}); `maverick_libs @0x9b9050` (`nm`); the `NRTUCODE_CORE_MAVERICK_NX_POOL` core-kind enumerant |
| `NeuronCoreVersion` | **V5 = 5** | **HIGH/OBSERVED** | `aws_neuron_isa_tpb_common.h:136` `…NEURON_CORE_VERSION_V5 = 5` — present maverick, ABSENT mariana (caps V4); the per-gen `s3dmx1_quant_valid_nc(nc)` gate flips `V4`→`V5` (§4) |
| `arch_id` | **36 / 0x24** | **MED/INFERRED** | `coretype = arch_id + 1` across the four lower gens (+8 coretype stride); **no NCFW v5 image exists** — `libncfw` `get_image` ladder compares only `{0x05,0x0c,0x14,0x1c}`, the rodata blob region closes at v4+, no `maverick`/`v5` string in `libncfw`. **Never binary-observed; mark `36*`.** [WALL] |
| NCFW v# | none (`v5?`) | HIGH/OBSERVED | `libncfw` tops at `arch_id 0x1c` (MARIANA_PLUS); no v5/maverick NCFW IRAM/DRAM symbol |

> **GOTCHA — three parallel enumerations, none derives the other.** `NeuronCoreVersion`
> (the codegen-target ISA version, 2..5) is the **arch-ISA axis**; `coretype`
> (6/13/21/29/**37**) is the device dispatch key; `arch_id` (0x05/0x0c/0x14/0x1c/**0x24\***)
> is the NCFW image selector. Two of the three (`NeuronCoreVersion::V5`, `coretype 37`)
> are OBSERVED; only the NCFW `arch_id` byte is **not**. Do not conflate them, and do not
> "derive" `36` from the OBSERVED two — it is independently un-confirmable on this corpus.
> [WALL]

**Position in the capability order** (monotone, +8 coretype stride, corroborated by
the monotone dtype-expansion superset and the clang-10→clang-15 toolchain bump):
`SUNDA(v2,ct6) < CAYMAN(v3,ct13) < MARIANA(v4,ct21) < MARIANA_PLUS(v4+,ct29) <
MAVERICK(v5?,ct37)`. The exact silicon / marketing name is **OPEN** — the
`coretype→silicon-part` binding is not in this corpus; do **not** fabricate a
Trn-next binding (see the [codename cross-walk caveat](../reference/codename-crosswalk.md)).
The `/proj/maverick/…`-rooted `al_address_map_db.pkl` proves MAVERICK is a *distinct
later SoC instance* (OBSERVED); the part name is not.

### 1a. Internal-twin-exclusive — `0` `.a` members, single-source carve

`nm libnrtucode_internal.so` exposes **62** `MAVERICK_*_get` accessors; `ar t
libnrtucode.a` (sha256 `158dadc5…`) carries **0** MAVERICK members
(**435** total = **420 image members** [CAYMAN 124 / MARIANA 124 / MARIANA_PLUS 124 /
SUNDA 48 / MAVERICK 0] **+ 15 framework `.c.o`** objects, re-verified
`ar t | rg -ic maverick` → empty). So **every MAVERICK carve is
single-source by necessity** — there is no `.a` member to byte-reconcile against
(unlike the v4/v4+ carves, which reconciled `.so`==`.a`). The shipped static archive
topping out at MARIANA_PLUS is itself a gen-step signature. [HIGH/OBSERVED]

| ENGINE | `_get` accessors | DEBUG? | PROF? | EXTISA? | notes |
|---|---|---|---|---|---|
| **`NX_ACT`** | **0** | — | — | — | **🧱 FILE-ABSENT — the ACT→DVE fold** (§3) |
| `NX_DVE` | 14 | **yes** | yes | — | head of the v5 NX block; the only DEBUG-keeping engine |
| `NX_PE` | 10 | no | yes | — | matmul; DEBUG dropped |
| `NX_POOL` | 10 | no | yes (disarmed) | — | SEQ half of the dual-core POOL |
| `Q7_POOL` | 20 | yes | — | 4 | compute half; SRAM-resident; the MX-dequant home |
| `NX_SP` | 8 | no | **no** | — | SRAM-resident; lean sync/control core |
| **`Q7_CC_TOP`** | **0** | — | — | — | **🧱 FILE-ABSENT** — the v5 collective top-sync FW (§7 W4) |
| **TOTAL** | **62** | | | | matches the catalog index |

---

## 2. Memory / topology deltas (the `/proj/maverick` SoC instance)

From the MAVERICK `al_address_map_db.pkl` TPB-engine subtree (safe-loaded), cross-read
against the Cayman flat-YAML baseline. Every absolute number is Maverick-specific; the
schema is family-general. The defining shape is a **markedly larger, more-parallel
tile** with a re-architected sync fabric.

| aspect | CAYMAN (flat YAML) | MAVERICK (pkl) | Δ | conf |
|---|---|---|---|---|
| STATE_BUF (SBUF) | 32 MiB @ `0x2000000000` | 128 MiB @ `0x2000000000` (`MEM_SIZE 134217728`) | **4×** | HIGH/OBS |
| SP engines / TPB | 1 | **16** (`SP_0..15`, `0x100000` pitch) | **16×** | HIGH/OBS |
| PE engines | 1 PE-array sequencer | 4 (2 clusters × 2) | 4× | HIGH/OBS |
| DVE engines | 1 DVE + bank RAMs | 4 (2 clusters × 2) + banks | 4× | HIGH/OBS |
| ACT engine | separate `ACT_*` block | **FOLDED INTO DVE** (`ACT_CONTROL_TABLE` + `PWP_*` under the DVE node) | structural | HIGH/OBS |
| PSUM_BUF | 4 MiB @ `0x2802000000` | **ABSENT** (0 pkl records; host-CSR-driven) | structural | HIGH/OBS |
| DGE_MEMORY | 1 GiB @ `0x2040000000` | **512 KiB RESERVED stub** (`RESERVED_SIZE 524288`) | size HIGH / home MED | — |
| POOL (8×Q7) | per-core IRAM+DRAM, NX_DRAM 64 KiB | per-core DRAM (256 KiB), NX_DRAM 128 KiB + `GPSIMD_SYNC` + `POOL_SHARED_RAM` | 2× DRAM | HIGH/OBS |
| COLLECTIVE_SYNC | (absent) | **NEW TPB-wide** (2048 sema / 64 watchers / 16 agents) | new | HIGH/OBS |
| POOL.GPSIMD_SYNC | (absent) | **NEW POOL-local** (64 sema / 16 watchers / 8 agents) | new | HIGH/OBS |
| TPB engine window | `0x804000000` (~32 GiB) | `0x4000000000` (256 GiB) | wider | HIGH/OBS |
| TPB count | 8 (TPB_0..7 flat) | 8 = 4 SENG × 2 TPB | re-tiered | HIGH/OBS |

> **NOTE — the dual collective-sync fabric has DIFFERENT capacities.** The TPB-wide
> `COLLECTIVE_SYNC` (`tpb_coll_sync_sp.json`) is the **large** block —
> `NUM_SEMAPHORES=2048`, `WATCHERS_PER_AGENT=64`, `NUM_AGENTS=16`; the POOL-local
> `GPSIMD_SYNC` (`tpb_gpsimd_sync.json`) is the **small** one — `64 / 16 / 8`. Each
> param-triple appears 16× in the json (8 TPB × 2 planes). The 16-wide SP array + this
> dual fabric together signal a Maverick collective subsystem built for far more
> concurrent synchronization. [HIGH/OBSERVED]

> **GOTCHA — PSUM is no longer a named region.** The PE-array accumulator is driven by
> the host-visible PE-array-sequencer CSRs (`tpb_arr_seq_top` on the APB plane), not a
> named SBUF-adjacent `PSUM_BUF` leaf — `0` PSUM records across all 323,198. A
> reimplementer must not look for a PSUM address region on v5. [HIGH negative/OBSERVED]

---

## 3. The ACT→DVE fold — MAVERICK-only, a RENAME-NOT-MERGE

The single largest structural gen-step, and a MAVERICK-only event. Where
SUNDA/CAYMAN/MARIANA/MARIANA_PLUS each ship a **standalone** `NX_ACT` image,
**MAVERICK ships none** — and the activation datapath is re-homed onto DVE. The fold
is real at the **profiling/scheduling** and **datapath-rename** levels; it is **not**
a handler-image merge. See [maverick-act.md](../images/maverick-act.md) /
[maverick-dve.md](../images/maverick-dve.md) (the two halves of the fold thesis).

The four OBSERVED footprints:

1. **`NX_ACT` is FILE-ABSENT — four independent zeros.** `nm | rg -c 'MAVERICK.*NX_ACT'`
   = 0; case-insensitive `maverick`+`act` symbols = 0; `.rodata` strings = 0; the IDA
   `_names.json` sidecar = 0. The five-NX MARIANA roster (`ACT/DVE/PE/POOL/SP`) is a
   **four**-NX MAVERICK roster (`DVE/PE/POOL/SP`). [HIGH/OBSERVED]
2. **The 5 ACT-specific handlers are GONE firmware-wide** — `Activate`,
   `ActivateQuantize`, `ActivationTableLoad`, `ActivationReadAccumulator`, `Activate2`
   = **0 each**, region-wide over `0x871300..EOF`. [HIGH/OBSERVED]
3. **ACT opcodes `0x23`/`0x25` newly armed on the DVE PROF CAM.** The MAVERICK DVE PROF
   CAM (`dbff2b84`) arms `{op=0x23 ACTIVATION_TABLE_LOAD}` + `{op=0x25 ACTIVATE2}` —
   which the MARIANA/MPLUS DVE PROF (`ca588683`) does **not**. This is the dispatch-level
   smoking gun. [HIGH/OBSERVED]
4. **The ACT read-accumulator survives renamed** as DVE-native `DveReadAccumulator`
   (`NEURON_ISA_TPB_OPCODE_DVE_READ_ACCUMULATOR = 0x9b`, confirmed in the enum this
   pass); `ActivationReadAccumulator` = 0 firmware-wide. [HIGH/OBSERVED string + enum]

> **NOTE — it is a RENAME, not a MERGE.** The MAVERICK DVE handler roster is
> **DVE-native — 59 strict** (vs MARIANA_PLUS DVE's 60; the *only* strict difference is
> `QuantizeMx`, which leaves the named DVE roster, §6). It is **not** a DVE+ACT union;
> no `Activate*` appears anywhere. The address-map corroborates: `…DVE_0_0_ACT_CONTROL_TABLE`
> is namespaced under the DVE block, with **0** standalone `…ACT_` MMIO engine instances
> (192 `…DVE_` / 128 `…PE_`). The fold is a *dispatch re-homing + datapath rename*, not a
> collapse of ACT into a vector op. [roster HIGH/OBSERVED; the causal fold reading
> INFERRED-HIGH, corroborated by the address map]

---

## 4. The MAVERICK-vs-MARIANA delta table (opcodes verified byte-for-value)

The central cross-gen deliverable. `v5 = strict v4 opcode superset (+6)` — **165
opcodes**, all 159 mariana ops recurring with identical name=value (zero drift, zero
drop), plus six genuine new free slots. Each opcode value below was read directly from
`neuron_maverick_arch_isa/tpb/aws_neuron_isa_tpb_common.h` (line + `// Y`
marker shown where it decides the result). `OBS` = OBSERVED; `INF` = inferred; `CIT`
= carried.

| FEATURE / AXIS | MARIANA (v4) | MAVERICK (v5) | EVID |
|---|---|---|---|
| `NeuronCoreVersion` enum | caps V4 | **V2..V5** (`V5 = 5` `:136`) | OBS |
| coretype | 21 | **37** | OBS |
| NCFW arch_id | 0x14 | **0x24\*** (image ABSENT) | INF |
| OPCODE enumerators | 159 | **165** (+6 strict superset) | OBS |
| ENUM_LIST kinds (`enums.h`) | 78 | **91** (+13) | OBS |
| `tpb/` header files | 117 | **123** (+6, 0 drop) | OBS |
| common/ device-CSR headers | 3 (`xt_defines` etc.) | 3, **BYTE-IDENTICAL to v4** | OBS |
| **`COMPACT_CONTROL_INST`** | — | **`0xb6`** (`:266 // Y`, `ctrl_cci.h`, 15 micro-ops/64-B §5) | OBS |
| **`TENSOR_TENSOR_INT_WIDE`** | — | **`0xf3`** (`:320 // Y`, `s2s2d2d2_tt.h`, 32→64-bit int, lo/hi dst) | OBS |
| **`TENSOR_SCALAR_INT_WIDE`** | — | **`0xf4`** (`:321 // Y`, `s2d2d2_ts_wide.h`, 32→64-bit, 2× perf) | OBS |
| `ACTIVATE_MULTIPASS` | — | `0x26` (`:173 // Y`, `s1s2d2_am.h`, prev-pass TENSOR1D feedback) | OBS |
| `DMA_MEMCPY2` | — | `0xb9` (`:268 // Y`, `dma_copy2d.h`, 2-D copy + sem wait/update) | OBS |
| `DMA_IMMEDIATE` | — | `0xba` (`:269 // Y`, `dma_immediate.h`, 1-3 inline 16-B descriptors → DGE) | OBS |
| WAIT_MODE event family | `WAIT_FOR_EVT_*` 0x6/7/e/f | **DROPPED** | OBS |
| WAIT_MODE sem-reg-offset | — | **`SEM_*_REG_OFFSET` 0x91-0x95** (backed by `SEM_BASE_OFFSET{reg:8,off:24}`) | OBS |
| WAIT_MODE unordered | — | **`UNORDERED 0xfe`** ("no wait; faster than NONE") | OBS |
| UPDATE_MODE event | `EVT_*_READ/COMPLETE` | **DROPPED** (pure-semaphore) | OBS |
| DMA descriptor model | SDMA M2S/S2M ring + tail-ptr doorbell | **DGE inline `DESCRIPTOR_RAW[16]`** + `DMA_ENGINE_CONFIG` (1/2/4/8) + 12 `DMA_ADDR_MODE`s | OBS(hdr+fw) |
| DMA sema-update target | normal semaphore | **sem-block OR COLL-SYNC-block, local OR remote** (`DMA_SEMA_UPDATE_MODE` LOCAL/REMOTE × SEM/COLLSYNC = `0x1/0x5/0x9/0xd`) | OBS |
| DGE dispatch (firmware) | (not present) | **`DGE DIRECT2D / INDIRECT / GATHER TRANSPOSE`** | OBS(fw) |
| on-engine SB2SB `0xBF` | present (POOL) | present (POOL) — firmware-confirmed | OBS(fw) |
| cross-die transport IP | `io_d2d` (DWC-PCIe+XSR) | UCIe re-IP (PHY not named) | INF/CIT |
| sync fabric | EVT_SEM (event+sem) | **dual `COLLECTIVE_SYNC` + `GPSIMD_SYNC`**; coll-sync reachable from the DMA descriptor | OBS(hdr) |
| **MX matmul encoding** | separate PE pair `0x09 LDWEIGHTS_MX` + `0x0A MATMUL_MX` | **folded into `Matmul`/`Ldweights`** via `MXTENSOR_V2` ADDR4 marker (0x01) + `MX_PERF_MODE` QUAD/OCT; `0x09`/`0x0A` RETAINED + DEPRECATED | OBS |
| **`QUANTIZE_MX`** | `0xe3` (gate `nc==V4`) | **`0xe3`** (`:309 // Y`, gate `nc==V5`) — **DVE forward op** (§6) | OBS |
| `TENSOR_DEQUANTIZE` | `0x7b` | `0x7b` (`:222 // Y`) — **POOL's only MX surface** (dequant) | OBS |
| MX dtypes | `FP4_EXP2 0x10` | **+`FP8_EXP2 0x11`/`INT4 0x12`/`SFP8_E8..E5 0x13..0x16`** | OBS |
| MX descriptor | `MXTENSOR1D` (16-B) | `MXTENSOR_V2` (16-B) + `_20B` union | OBS |
| compact-control ISA | — | `CTRL_CCI` 15 micro-ops/64-B (`CCOP`/`CCINST_TYPE`, §5) | OBS |
| multi-pass activation | single `Activate2` | `ActivateMultipass` + prev-pass TENSOR1D feedback | OBS |
| **8× `Q7_POOL` + 4 EXTISA** | (single Q7 image set / 4 EXTISA) | **8 Q7_POOL cores; 4 EXTISA `kernel_info_table`s (17/1/2/9)** | OBS(fw) |
| Q7_POOL EXTISA KIT key-set | (mariana_plus set) | **IDENTICAL to MARIANA_PLUS** (`+0/−0` rows, all 4 KITs) | OBS |
| DGE fast-path | present | **DROPPED gen-wide** (`dge_decode_fast`-family = 0) | OBS |
| host KaenaHal arch_type | 4 | (no v5 slot in libnrt 2.31.x) | OBS |
| runtime Q7 geometry / CSR / run-stall | OBSERVED (host HAL) | **INFERRED** Cayman-class+ (no v5 KaenaHal/libncfw; CSR hdr ==v4) | INF |

**One line:** v5 = strict v4 opcode superset (+6) with the MX pair folded into
`0x01`/`0x02` via the `MXTENSOR_V2` ADDR4 marker, the EVENT sync primitive retired in
favour of register-offset semaphores + a coll-sync block reachable straight from the
DMA descriptor (local/remote), a new DGE inline-descriptor DMA model, a 15-micro-op
compact-control ISA, 32→64 wide-int ops, and multipass activation — all OBSERVED from
the v5 ISA headers and corroborated in the v5 customop firmware; only the NCFW
`arch_id`, the UCIe PHY IP, and the host runtime geometry remain INFERRED/ABSENT.

> **NOTE — the `+6` PROF arming is NOT opcode-space growth.** The DVE PROF gains 10
> armed records (`+10/−3` vs MARIANA DVE) — but the `+10` (`0x23/0x25/0x58/0x61/0x62/
> 0x6c/0x6d/0x6e/0x6f/0x99`) are **pre-existing mariana opcodes re-armed on the DVE
> engine**, distinct from the genuine OPCODE-enum growth (159→165, the 6 different new
> names above). Two related but distinct enum spaces; do not double-count. [HIGH/OBSERVED]

The MX dtype space is a strict superset chain `TONGA(8) ⊂ CAYMAN==SUNDA(16) ⊂ MARIANA
(+FP4_EXP2/CPTC) ⊂ MAVERICK (+FP8_EXP2/INT4/SFP8_E8..E5 + MXTENSOR_V2)`. The
`SFP8_E8` (E8M0) scale-only dtype is the MX shared-exponent format the POOL dequant
kernel consumes. **ABI caveat:** `libneuroncustomop.a` is built against the
cayman/sunda 16-code dtype set — the FP4/FP8/MX/INT4 codes are read from the maverick
header (OBSERVED) but are not exercised through the `at::Tensor` boundary in 0.21.2.0
(no `Float8`/`Float4` ScalarType to marshal them); they live in the ISA/ucode/SDMA/
collective layers. [HIGH/OBSERVED — ABI-06]

---

## 5. The compact-control ISA (`COMPACT_CONTROL_INST 0xb6`)

A standout v5 addition. `COMPACT_CONTROL_INST` (`0xb6`, `ctrl_cci.h`) packs **fifteen**
4-byte micro-ops into one 64-B instruction (`CTRL_CCI_STRUCT = header + CCINST insts[15]`,
60-B `insts[]` area) — a full embedded control/ALU ISA on the NeuronCore engine, the v5
mechanism for dense scalar control flow without round-tripping to the sequencer.
`CompactControlInst` is engine-agnostic (`is_control_instruction`). [HIGH/OBSERVED —
`ctrl_cci.h` read field-by-field]

`CCOP` families (`NEURON_ISA_TPB_CCOP`, OBSERVED): `MOV` (NOP/MOV/MOV_IMM 0x0-0x4),
`ALU32` (ADD..MIN 0x10-0x16, signed-sat 0x18-0x1e), `ALU64` (0x20-0x2e), `FP`
(0x30-0x39), `ABS` (0x3a-0x3f), `SHIFT` (ASL/ASR/LSL/LSR 0x40-0x43, ROL/ROR, *64,
CLZ/CLZ64), `LOGIC` (0x50-0x57), `CMP` (0x60-0x85), `BRANCH` (BR/BL rel-imm/rel-reg/
reg64 0x90-0x9b), `MEM` (LDR*/STR* 0xa0-0xab), `SEM` (SEM_READ 0xb0 / SEM_WRITE 0xb1 /
SEM_ADD 0xb2, the 32-b register naming the semaphore group — ties to the
`SEM_BASE_OFFSET` register-addressed model). `CCINST_TYPE {ALU,MOV,MOVIMM,BR,BL,MEM,
SEMR,SEMU,NOP}=0..8`. [HIGH/OBSERVED]

The two 32→64 wide-int ops complete the v5 ALU widening: `TENSOR_TENSOR_INT_WIDE`
(`0xf3`, `s2s2d2d2_tt.h`, two src + two dst — lo result / hi carry, POOL engine) and
`TENSOR_SCALAR_INT_WIDE` (`0xf4`, `s2d2d2_ts_wide.h`, `dst0` = low 32 / `dst1` = high
32, 2× perf, no indirect addressing). These are the MAVERICK end of the INT_WIDE
gen-bracket (see the committed kernel page #748). [HIGH/OBSERVED]

---

## 6. CORRECTION — the `QUANTIZE_MX 0xe3` binding RESOLVED

The brief flagged an **open divergence**: [maverick-dve.md](../images/maverick-dve.md)
says the QuantizeMx *handler* leaves the v5 DVE named roster (60→59) and "migrates to
the Q7 POOL MX path"; [maverick-pool.md](../images/maverick-pool.md) says opcode
`0xe3` is **absent** from all four MAVERICK Q7 EXTISA POOL `kernel_info_table`s and
attributes `0xe3` to DVE. This is the two-POOL ambiguity (NX_POOL `engine_idx 2` SEQ
vs the Q7_POOL SRAM compute core). The maximal carve resolves it.

> **CORRECTION / RESOLUTION [HIGH/OBSERVED].** Opcode **`0xe3 QUANTIZE_MX` binds the
> DVE engine** (the **forward**-direction MX quantize op). It is provably **absent
> from all four POOL `kernel_info_table`s** — the union of all POOL KIT opcodes is
> `{0x41,0x45,0x46,0x47,0x51,0x52,0x7b,0x7c,0x7d,0x7e,0xbe,0xe4,0xf0,0xf2}`, with no
> `0xe3`, `0x09`, or `0x0A` (each checked `present? False`). POOL's **only** MX surface
> is the pre-existing **`0x7b TENSOR_DEQUANTIZE`** (the *dequant*-direction op,
> EXTISA_0 idx 16, funcVA `0x50ec` → `decode_tensor_dequantize` → `proc_4bit_mx_8`,
> present since NC-v3). The two are **distinct opcodes on distinct engines / cores**.
>
> The two image pages are therefore **consistent, not contradictory**, once the axes
> are separated:
> - **opcode `0xe3`** → DVE engine, forward MX quantize. Header-confirmed (`common.h:309
>   // Y`, gate `nc==V5`); the maverick DVE PROF CAM arms `0xe3` (the
>   [maverick-dve.md](../images/maverick-dve.md) `0x1e3` finding is the 9-bit→8-bit
>   *mask*-respec of the same `0xe3` arming, **not** a 4th deprecation). [HIGH/OBSERVED]
> - **the QuantizeMx *named handler*** → **DROPPED** from the v5 DVE roster (the 60→59
>   strict diff), **not migrated**. Binary-verified twice: `QuantizeMx` has **0** string hits
>   in the entire `0x871300+` MAVERICK region (the 2 lib-wide hits are MARIANA/MPLUS DVE DEBUG
>   DRAM, both `<0x871300`). The Q7 POOL does **not** gain the `0xe3` opcode or a `0xe3` KIT
>   row; the MX *dequant* machinery the Q7 POOL hosts is the **separate** `0x7b` path, present
>   since NC-v3. The earlier "migrates to the Q7 POOL MX path" phrasing conflated the
>   handler-drop with POOL's pre-existing `0x7b` dequant — it is corrected to **dropped, not
>   migrated**. [HIGH/OBSERVED — the handler drop + the opcode binding]
> - **the POOL Q7 dequant** (`0x7b → proc_4bit_mx_8`) is the **dequant** direction —
>   an older, in-band block-of-8 mechanism, retained byte-for-name on v5, distinct from
>   the v5 `0xe3`/`MATMUL_MX` forward path with its out-of-band `MXTENSOR_V2`/`SFP8_E8`
>   scale surface. [HIGH/OBSERVED]

The net resolution: **`0xe3` is a DVE opcode; the Q7 POOL adds no `0xe3` KIT row; the
`QuantizeMx` named handler is DROPPED (not migrated).** The brief's
worry — "if the binary cannot disambiguate, flag it" — does **not** apply: the binary
disambiguates cleanly (KIT key-set carve + PROF arming + the engine-tagged enum
comment `// DVE` + the 0-hit `QuantizeMx` region sweep). The Part-6 reconcile has now
harmonized [maverick-dve.md](../images/maverick-dve.md),
[maverick-act.md](../images/maverick-act.md), and
[maverick-pool.md §2.2](../images/maverick-pool.md) to the same wording: the named handler
is dropped, `0xe3` stays DVE-bound, POOL's MX surface is `0x7b`. **Confidence: HIGH/OBSERVED
for the opcode binding and the handler drop.**

```c
// The v5 MX direction split, in one block (the OBSERVED opcode→engine binding;
// the per-handler bodies are FLIX-desynced and INFERRED):
//
//   DVE forward quantize:  op 0xe3 QUANTIZE_MX,  gate (nc == V5),  armed on DVE PROF CAM
//                          // fp32/bf16 → MX block (out-of-band SFP8_E8 scale, MXTENSOR_V2)
//   PE   MX matmul:        op 0x01/0x02 in MX mode via MXTENSOR_V2 marker  (0x09/0x0A deprecated)
//   POOL dequant (Q7):     op 0x7b TENSOR_DEQUANTIZE  →  proc_4bit_mx_8
//                          // in-band block-of-8, 8:5 expansion (group_size==8 ⇒ MX micro-scale)
//   POOL KIT rows for 0xe3 / 0x09 / 0x0A:  NONE  (forward MX is not a POOL routing key)
```

---

## 7. The six v5 walls — observed-vs-inferred, row-by-row

Each wall states what IS observed (the header/getter/dispatch evidence) vs what
remains inferred, converted toward OBSERVED wherever the shipped v5 arch-ISA headers +
customop-lib v5 firmware images + native `ncore2gp` disasm permit.

| # | wall | what IS observed | what stays INFERRED |
|---|---|---|---|
| **W1** | **`arch_id 36` INFERRED** | `coretype 37` (the `get_ext_isa` ct37 dispatch case + `maverick_libs`); `NeuronCoreVersion::V5`; the v5 NCFW image is **definitively ABSENT** (`libncfw` ladder = `{0x05,0x0c,0x14,0x1c}` only; no v5/maverick string) | the specific value **36** — `coretype−1` extrapolation, **no byte confirms it**. Mark `36*`. [HIGH that v5 NCFW is absent; LOW that the arch_id is 36] |
| **W2** | **v5 Q7 geometry / CSR programmer / run-stall / DKL** | the firmware confirms **8× Q7_POOL + 4 EXTISA** (62 getters; the KIT carves); the v5 device-CSR headers (`xt_defines`, `xt_general_local_reg_defines`, `notification`) are **byte-identical to v4** (a negative OBSERVED) | `{base_offset, IRAM/DRAM size, reserved tail, num_q7 POOL/CC, POOL local-reg base}` — no v5 KaenaHal slot in libnrt 2.31.x, no v5 device-CSR delta to read. Cayman-class+ INFERRED. Q7 DKL is **dropped** (12 DKL getters gone) — OBSERVED absence; the runtime swap path INFERRED |
| **W3** | **v5 D2D transport changes** | the ISA exposes the remote-capable descriptor model (`REMOTE_SEM_INC`/`REMOTE_COLLSYNC_INC`, `remote_core_id` in `DmaMemcpy2`); the `SEMA_SYNC.CLEAR_PCIE_RO` hook persists; the firmware names the **DGE** dispatcher | the PHY/IP. The "UCIe 2nm chiplet, 7-entry abstract D2D IP, H_DIE_SCRATCHPAD" reading is **CARRIED** from GEN-08 — neither header nor firmware names the PHY. INFERRED at the IP level |
| **W4** | **v5 `Q7_CC_TOP` collective FW FILE-ABSENT** | `nm | rg -c 'MAVERICK_Q7_CC_TOP.*_get'` = **0** — a genuine provisioning gap (parallel to SUNDA's missing Q7_CC_TOP), not merely unread | the v5 compute-cluster collective top-sync firmware is **not in this artifact**. Its existence/shape on real silicon is out of scope of this corpus |
| **W5** | **v5 DVE/PE/Q7 full-body decode PARTIAL** | the reset/boot spine, the dispatch *structure* (187-entry `addi −48` table / `addx4`-indexed DRAM table / SP base-subtraction), the PROF arming, the `S:`/`P%i:` rosters, and the MX IVP fingerprints (`ivp_srln_2x32`/`ivp_bmaxn_2x32`/`ivp_mulus4tan16xr16`/`ivp_dextrprn_2x32`) are OBSERVED | the **per-opcode→handler-*body* binding** — the PERF/TEST IRAM vector datapath desyncs under FLIX-VLIW bundling in the linear sweep (the FW-00 ceiling). No DEBUG image on PE/POOL/SP to anchor a body. Per-row body INFERRED |
| **W6** | residual roll-up | the QuantizeMx `0xe3` ambiguity is **RESOLVED** (§6, no longer a wall); the PROF `0x1e3` "4th deprecation" is the `0xe3` mask-respec (resolved); `MODULE_SCHEDULE`/SortMerge phantoms do not bite v5 (no MAVERICK image references them) | nothing residual blocks v5 beyond W1–W5; the §6 resolution removes the largest open divergence |

> **GOTCHA — the device-CSR header equality is WHY the geometry stays inferred.**
> `diff(maverick, mariana)` on `common/aws_neuron_isa_xt_defines.h` /
> `…xt_general_local_reg_defines.h` / `…notification.h` is **EMPTY** (byte-identical).
> The shipped v5 headers carry **no** new Q7/hw_decode/run-stall device-CSR or NX-local
> register surface beyond v4 — all v5 ISA novelty lives in the `tpb/` opcode-struct-enum
> layer. There is no v5-specific device-CSR header to read, and no v5 libncfw/KaenaHal
> programmer. That is the structural reason W2/W3 cannot be pushed to OBSERVED on this
> corpus. [HIGH/OBSERVED negative]

---

## 8. The unified per-engine reset geometry (the v5 invariant)

The `−0x20` shorthand from the image pages is **approximate**; the binding v5
invariant is **`enter_run @0x94`** (vs v4/v4+ `@0x90`), shared by **every** MAVERICK
NX engine. The reset *targets* split by residence class: NX compute engines
(DVE/PE/NX_POOL) head `06 75` → `j 0x1d8`; the SRAM-resident cores (Q7_POOL, SP) head
`06 78` → `j 0x1e4`. SP and Q7_POOL each carry a *different* shift magnitude. Present
the unified table, not the `−0x20` shorthand. All bytes decoded with `ncore2gp` (exit
0). [HIGH/OBSERVED]

| engine | residence | head bytes | primary `j` | secondary `j` | enter_run | shift vs MPLUS | notes |
|---|---|---|---|---|---|---|---|
| `NX_DVE` (3) | IRAM | `06 75 … 86 76` | `0x1d8` | `0x1e4` | `@0x94` | **−0x20** / +0x4 | head of the NX block; base `0x1490` |
| `NX_PE` (0) | IRAM | `06 75 … 86 76` | `0x1d8` | `0x1e4` | `@0x94` | **−0x20** / +0x4 | boot dispatch `@0x1f7` |
| `NX_POOL` (2) | IRAM | `06 75 … 86 76` | `0x1d8` | `0x1e4` | `@0x94` | **−0x20** / +0x4 | SEQ sub-table base `0x1330` |
| `Q7_POOL` (2) | **SRAM** (IRAM size 0) | `06 78 … 86 79` | `0x1e4` | `0x1f0` | — | **−0x1c** | the Q7 core moves for the **first time** on any gen (CAYMAN/MARIANA/MPLUS Q7 all reset `j 0x200`) |
| `NX_SP` (4) | **SRAM** (IRAM size 0) | `06 78 … 86 79` | `0x1e4` | `0x1f0` | `@0x94` | **−0x14** | the **"Top-Sync"** stub (`+0xc` vs DVE/PE/POOL); SRAM base `0x04100000` (`const16 a0,0x410`) |

> **CORRECTION — the SP/Q7 shift is NOT `−0x20`.** A coarse "v5 = `−0x20`" reading is
> true only for DVE/PE/NX_POOL. **SP** is `−0x14` (primary `0x1f8`→`0x1e4`; the shorter
> Top-Sync stub, `+0xc` vs `j 0x1d8`); **Q7_POOL** is `−0x1c` (`0x200`→`0x1e4`). What all
> five share is **`enter_run @0x94`** and the unchanged `.globstruct` magic `0x6099cb34`
> + init block. A reimplementer must use the **per-engine** targets above, not the
> `−0x20` shorthand. [HIGH/OBSERVED — every reset byte decoded with `ncore2gp`]

**Build independence.** Every MAVERICK NX engine diverges from its MARIANA_PLUS twin at
**byte 1** (the reset immediate `75`/`78` vs `7d`) with **5.9–7.6%** positional 16-byte
block similarity (SP 5.9% / DVE 6.1% / PE 7.6% / POOL NX 7.0% / Q7 4.5%) — fully
independent v5 builds, recompiled-and-restructured, **not** relocated recompiles
(contrast the v4→v4+ step's `0x212`-byte shared prefix). Combined with the **~60–65%
smaller** footprint (DGE fast-path dropped + DEBUG dropped on PE/POOL/SP + ACT folded
out) and **0 `.a` members**, MAVERICK is a genuine new generation at the image level.
[HIGH/OBSERVED]

---

## 9. Per-engine v5 rollup (the matrix in one table)

| engine | idx | image shape | PROF (v5) | DGE FP | the v5 change | page |
|---|---:|---|---|:---:|---|---|
| **ACT** | 1 | **ABSENT** (folded into DVE) | n/a | n/a | AMPUTATED: no `NX_ACT` image; `0x23`/`0x25` armed on DVE PROF; read-accum → `DveReadAccumulator 0x9b` | [act](../images/maverick-act.md) |
| **DVE** | 3 | 14 getters, **full DEBUG** (only one) | CAM `dbff2b84` + TABLE `f349e417` (re-authored) | DROPPED | HEAD engine: absorbed ACT; +6 v5 opcodes in the enum; `0xe3 QuantizeMx` armed (forward MX); the named `QuantizeMx` roster handler dropped (60→59); independent build | [dve](../images/maverick-dve.md) |
| **PE** | 0 | 10 getters, NO DEBUG | CAM `85d857a7` + TABLE `e94d413a` (re-authored, −3 armed) | DROPPED | Matmul roster RETAINED (`0x01/0x02` + MX `0x09/0x0A` armed); MX REORGANIZED — folded into `Matmul`/`Ldweights` via `MXTENSOR_V2` + `TILE_SIZE`/`MX_PERF_MODE`; +FP8/INT4/SFP8 dtypes; ≈64% smaller | [pe](../images/maverick-pe.md) |
| **POOL** | 2 | NX 10 getters (NO DEBUG) + Q7 20 (DEBUG + 4 EXTISA, SRAM) | NX PROF reused **verbatim** (disarmed) | DROPPED | dual-core; **all 4 EXTISA KITs = 17/1/2/9, key-set == MARIANA_PLUS (`+0/−0`)**; **NO MX expansion on POOL** (`0xe3`/`0x09`/`0x0A` absent from all KITs); `0x7b` dequant + TIE+LFSR RNG retained; Q7 → SRAM, DKL dropped | [pool](../images/maverick-pool.md) |
| **SP** | 4 | 8 getters, NO DEBUG, **NO PROF**; SRAM-resident | NONE (no PROF) | DROPPED | lean sync/control core; SRAM-resident (the v5 anomaly); DGE fast-path DROPPED (was PRESENT on MPLUS SP — the decisive lean-engine proof the drop is gen-wide); Top-Sync reset `j 0x1e4`; no new SP opcodes; ≈62% smaller | [sp](../images/maverick-sp.md) |

**Gen-wide v5 invariants** (each OBSERVED, cross-engine): genuine independent builds
(byte-1 divergence, 5.9–7.6% block-sim); `enter_run @0x94`; internal-twin-exclusive (0
`.a` members); the **DGE fast-path dropped gen-wide** (the `dge_decode_fast`-family = 0
on DVE/PE/SP — the decisive v4+→v5 contrast); PROF re-authored per-engine on the
*armed* engines (DVE/PE/POOL-NX disarmed) while SP has no PROF; DEBUG dropped on
PE/POOL/SP (only DVE keeps it); the opcode/MX/dtype expansion is **engine-localized**
(the NX sequencer dtype surface stays numeric `UINT32/INT32/FP32` on every engine; MX
rides the PE Matmul fields + the Q7 POOL dequant kernel); `mariana-4062` errata dropped
gen-wide; `cayman/seq` source tree retained; NC-v5 own ISA dir.

---

## 10. Adversarial self-verification — the strongest claims, re-challenged

1. **`coretype 37` OBSERVED / `arch_id 36` INFERRED.** *Challenge:* is 37 really
   byte-grounded, or extrapolated like 36? *Re-verify:* `nrtucode_get_num_ext_isa_libs`
   `@0x9b2c90` does `cmp $0x25,%edi` (`0x25 = 37`) range-guard + `movabs $0x2020202000`
   (bits {13,21,29,37}) — ct37 is a real dispatch case; `maverick_libs @0x9b9050`; the
   `NRTUCODE_CORE_MAVERICK_NX_POOL` enumerant. `arch_id 36` has no NCFW image
   (`libncfw` ladder caps at `0x1c`). **HOLDS — split respected.** [HIGH/OBSERVED vs MED/INFERRED]
2. **The ACT-fold four zeros.** *Challenge:* a carve gap could fake the absence.
   *Re-verify:* `nm | rg -c 'MAVERICK.*NX_ACT'` = 0; the 62
   getters resolve to DVE/PE/POOL/SP/Q7 with no ACT family; the 5 ACT handlers = 0
   firmware-wide; `0x23`/`0x25` armed on DVE PROF (absent on MARIANA DVE). **HOLDS.** [HIGH/OBSERVED]
3. **The `0xe3` resolution.** *Challenge:* maybe `0xe3` IS a POOL row and the pages
   genuinely contradict. *Re-verify:* the union of all four POOL KIT opcodes is
   `{0x41,0x45,0x46,0x47,0x51,0x52,0x7b,0x7c,0x7d,0x7e,0xbe,0xe4,0xf0,0xf2}` — no `0xe3`;
   the enum tags `0xe3 QUANTIZE_MX // DVE`, gate `nc==V5`; POOL's only MX surface is
   `0x7b` (dequant). The DVE-page "migration" is MX-machinery-home, not the opcode.
   **HOLDS — disambiguated, not flagged.** [HIGH/OBSERVED]
4. **The per-engine reset table.** *Challenge:* is it uniformly `−0x20`? *Re-verify:*
   DVE/PE/NX_POOL `06 75`→`j 0x1d8` (−0x20); SP `06 78`→`j 0x1e4` (−0x14, Top-Sync +0xc);
   Q7_POOL `06 78`→`j 0x1e4` (−0x1c). The shorthand `−0x20` is wrong for SP/Q7; the true
   invariant is `enter_run @0x94`. **HOLDS — unified table, not the shorthand.** [HIGH/OBSERVED]
5. **The opcode-enum values.** *Challenge:* are `0xb6/0xf3/0xf4/0xe3` exact?
   *Re-verify, read from `common.h`:* `COMPACT_CONTROL_INST = 0xb6` (`:266`),
   `TENSOR_TENSOR_INT_WIDE = 0xf3` (`:320`), `TENSOR_SCALAR_INT_WIDE = 0xf4` (`:321`),
   `QUANTIZE_MX = 0xe3` (`:309`), `MATMUL_MX = 0x0A` (`:167`), `LDWEIGHTS_MX = 0x09`
   (`:166`), `TENSOR_DEQUANTIZE = 0x7b` (`:222`); MX dtypes `0x11/0x12/0x13-0x16`.
   **HOLDS — all byte-confirmed.** [HIGH/OBSERVED]

**Honesty ledger.** *HIGH/OBSERVED:* container sha `b7c67e89…` MATCH; the
62-getter histogram (DVE 14 / PE 10 / NX_POOL 10 / SP 8 / Q7_POOL 20 / Q7_CC_TOP 0 /
NX_ACT 0); 0 `.a` members of 435; `maverick_libs @0x9b9050`; the ct37 dispatch
(`cmp 0x25` + mask `0x2020202000`); `NeuronCoreVersion::V5=5` (maverick) vs caps-V4
(mariana); the per-gen `s3dmx1_quant_valid_nc` gate `V4`(mariana)→`V5`(maverick); every
delta-table opcode value (`0x09/0x0A/0x23/0x25/0x26/0x7b/0x9b/0xb6/0xb9/0xba/0xe3/0xe4/
0xf3/0xf4`) + the MX dtypes `0x11/0x12/0x13-0x16`; the four POOL KIT key-sets
(`{…}` union, no `0xe3`); the per-engine reset bytes (`ncore2gp` exit 0); the device-CSR
header byte-identity to v4; the `QuantizeMx` named-handler drop (0 hits region-wide) +
the `0xe3` DVE binding (DVE PROF CAM) + POOL `0x7b`-only MX surface. *MED/INFERRED:*
`arch_id 36*` (no NCFW v5); "DGE re-architected to HW DMA"; the v5 Q7 geometry / CSR programmer
/ run-stall / DKL (Cayman-class+ inferred); the per-opcode→handler-body binding
(FLIX-desync frontier). *LOW / NOT CLAIMED:* the `coretype→silicon-part` (Trn-next)
binding; the exact marketing name; shipping-vs-pre-release status; the UCIe PHY IP; the
v5 `Q7_CC_TOP` shape (FILE-ABSENT). *N/A:* nothing fabricated; no vendor source
snapshot referenced.

---

## 11. Cross-references

- **[MAVERICK × ACT](../images/maverick-act.md)** / **[× DVE](../images/maverick-dve.md)**
  — the two halves of the ACT→DVE fold thesis (the `NX_ACT` FILE-ABSENCE, `0x23`/`0x25`
  on the DVE PROF, `DveReadAccumulator 0x9b`, the 59-handler DVE-native roster).
- **[MAVERICK × PE](../images/maverick-pe.md)** — Matmul RETAINED + MX REORGANIZED
  (`MatmulMX`/`LdWeightMX` folded into `Matmul`/`Ldweights` via `MXTENSOR_V2`; the new
  `0x11/0x12/0x13-0x16` dtypes; the re-authored PROF; the no-DEBUG roster).
- **[MAVERICK × POOL](../images/maverick-pool.md)** — the dual-core (NX_POOL idx2 +
  Q7_POOL SRAM core); the four EXTISA KITs (17/1/2/9, key-set == MARIANA_PLUS); the §2.2
  CORRECTION that pins `0xe3` to DVE and `0x7b` to POOL (the resolution this page §6
  consolidates).
- **[MAVERICK × SP](../images/maverick-sp.md)** — the degenerate lower bound
  (SRAM-resident, DEBUG-dropped, DGE-fast-path-dropped, Top-Sync reset, 8 getters).
- **[Codename Cross-Walk caveat](../reference/codename-crosswalk.md)** — why the
  `coretype→silicon-part` / marketing-name binding is OPEN (do not fabricate Trn-next).
- **[Codename ↔ Generation Map](./codename-generation-map.md)** · **[Master Capability
  Matrix](./master-capability-matrix.md)** — the cross-gen siblings this profile feeds.
