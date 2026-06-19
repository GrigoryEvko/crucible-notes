# Master Per-Generation Capability Matrix

> **Part-6 capstone.** This page folds every committed per-generation image page,
> per-generation profile page, and cross-gen synthesis into ONE authoritative
> capability matrix for the Vision-Q7 "Cairo" GPSIMD engine across the five
> NeuronCore generations plus the legacy TONGA V1 floor. It is the payload that
> the orientation [gen-invariance thesis](../orientation/gen-invariance.md) cites:
> a reimplementer writes **one** Vision-Q7 engine plus a per-gen capability table,
> **not** five engines.
>
> It is re-verified, not transcribed. A sample of grid cells was carved directly
> from the shipped binaries/headers this pass (§9); every other cell is CARRIED
> from a committed page with its own binary anchor. Confidence is tagged per cell
> and **never inflated by synthesis**.

All facts on this page derive from static analysis of shipped artifacts only —
the `libnrtucode*.so` symbol tables, the per-gen `neuron_<gen>_arch_isa` C
headers, the `arch-headers/<gen>` register-map mirrors, and the EXTISA/firmware
image getter symbols in the customop-lib. The corpus lives under the gitignored
`extracted/` tree (use `--no-ignore`). No external source tree was consulted.

---

## 0. The headline

The five GPSIMD generations are **not five processors** — they are one Cadence
Tensilica Vision-Q7 NX "Cairo" DSP (`CoreID ncore2gp`, HW `NX1.1.4`) wrapped in a
per-generation SoC envelope. The whole stack partitions into three classes:

- **INVARIANT** — byte-/structurally-identical across every Q7-bearing gen (the
  FLIX ISA core, the 64-byte instruction word, the SEQ dispatch model, the
  frozen Q7-control CSR core, the XEA3 boot/IRQ spine, the engine-idx enum).
- **SCALING** — present on all gens but re-parameterized by a single scalar/enum
  (DTYPE 16→30, opcode count 145→165, CSR-bundle count 7→11, transport IP,
  Q7 geometry, handler-roster size).
- **ABSENT-then-ADDED** (presence flips) — a capability that appears or
  disappears at a specific gen step (RNG@CAYMAN, MX@MARIANA, ACT-fold@MAVERICK,
  DGE-fast-path@v4+ then dropped@v5).

> **The gen-invariance verdict.** A decoder/emulator built against the single
> Vision-Q7 model `R(Q7)` carries **one** microarch and **one** opcode map; it
> selects the live opcode membership + the device-CSR/geometry envelope by
> reading the coretype (`6/13/21/29/37`) and applies the presence flags. **No
> per-gen decoder fork is required for v2..v5.** TONGA (V1) is the lone exception
> — outside the envelope (§7).

The capability lattice is a **per-subsystem partial order**, not a single total
order. ISA/DTYPE/struct surfaces are strict superset on every step; collectives
are flat in the middle; transport is a generation *swap* at MAVERICK; the DGE
fast-path is *non-monotone* (added v4+, dropped v5). The generation **number**
advances monotone on six independent anchors (§3); the per-subsystem
**capability** does not advance at every step.

---

## 1. The ladder (the spine)

```
[TONGA V1]  ⊳  SUNDA(v2)  ⊳  CAYMAN(v3)  ⊳  MARIANA(v4)
               ⊳  MARIANA_PLUS(v4+)  ⊳  MAVERICK(v5)
```

| Generation | NC-ver | coretype | arch_id | NCFW sel-byte | get_ext_isa case | Shipped? |
|------------|:------:|:--------:|:-------:|:-------------:|:----------------:|----------|
| **TONGA** (V1) | — | — | — | — | — | N/A (legacy ISA header only) |
| **SUNDA** (v2) | NC-v2 | 6 | 5 (0x05) | 0x05 | 6 | YES (all 4 libs) |
| **CAYMAN** (v3) | NC-v3 | 13 | 12 (0x0c) | 0x0c | 13 | YES (all 4 libs) |
| **MARIANA** (v4) | NC-v4 | 21 | 20 (0x14) | 0x14 | 21 | YES (all 4 libs) |
| **MARIANA_PLUS** (v4+) | NC-v4 (shared) | 29 | 28 (0x1c) | 0x1c | 29 | YES (all 4 libs) |
| **MAVERICK** (v5) | NC-v5 | 37 (OBS) | 36* (INFERRED, doubly) | (none) | 37 (OBS) | NO (internal twin only) |

`coretype = arch_id + 1`, uniform across all five. Strides `{+7,+8,+8,+8}` — the
SUNDA→CAYMAN step is +7 (SUNDA sits one slot lower at 5/6), the rest +8. The
`get_ext_isa` 32-case dispatch live-set is `{6,13,21,29,37}` — keyed on coretype,
not arch_id.

> **GOTCHA — arch_id 36 is DOUBLY inferred (the headline wall).** coretype 37 is
> OBSERVED (the `0x2020202000` bitmask → bits `{13,21,29,37}` plus the SUNDA `cmp
> $0x6` leg, and a `cmp $0x25`=37 MAVERICK gate in the internal-twin dispatch,
> 6 hits this pass). arch_id 36 is **not** observed: (1) it rests on the
> `coretype = arch_id + 1` rule, and (2) the `NX_TOPSP = arch_id` rule that
> grounds the other four gens **fails** for MAVERICK — the enum slot 36 is a
> `MAVERICK_NX__REMOVED__` placeholder and the real `MAVERICK_NX_TOPSP` sits at
> index 54. There is no v5 NCFW image to confirm 0x24=36 (the `libncfw` selector
> ladder closes at `0x1c`). Always carry `36*`. [CARRIED — codename-generation-map #777; SX-GEN-09 §1.1; DX-GEN-01 §9c] MED/INFERRED.

See [codename-generation-map.md](codename-generation-map.md) for the full
identity contract and the four reconciled naming enumerations (NCFW arch_id /
coretype / NX_POOL mask-bit family vs the host KaenaHal arch_type `{2,3,4}` and
the `libnrtucode` idx-enum `{6,7,10,11}` — never conflate them).

---

## 2. The master grid — 15 subsystems × 5 generations (+ TONGA V1 floor)

Cell legend: **value** + confidence (`H`=HIGH, `M`=MED) × evidence (`OBS`=observed
from bytes/disasm/header, `INF`=inferred). `SPOT` = re-verified against the
shipped artifact **this pass** (§9). `≡<gen>` = byte/semantically identical to
that gen. `—` = absent / not applicable. Source anchors are abbreviated:
`GEN-09`/`GEN-12`/`DX-01`/`DX-05` = the four backing reports; per-gen pages are
linked in §10.

### Block A — Identity, ISA, dtype (the strict-superset compute core)

| Subsystem | TONGA (V1) | SUNDA (v2) | CAYMAN (v3) | MARIANA (v4) | M_PLUS (v4+) | MAVERICK (v5) |
|-----------|------------|------------|-------------|--------------|--------------|---------------|
| **(1) IDENTITY** | no runtime id; ISA hdr only `H/OBS` | ct6/ai5, NC-v2, sel 0x05 `H/OBS SPOT` | ct13/ai12, NC-v3, sel 0x0c `H/OBS SPOT` | ct21/ai20, NC-v4, sel 0x14 `H/OBS SPOT` | ct29/ai28, NC-v4, sel 0x1c `H/OBS SPOT` | ct37/ai36\*, NC-v5, no NCFW `H/OBS (ai36\* M/INF) SPOT` |
| **(2) ISA OPCODE** count | legacy `*_INST` (no roster) `H/OBS` | 145 `H/OBS SPOT` | 150 `H/OBS SPOT` | 159 `H/OBS SPOT` | ≡MARIANA (shares ISA) `H/OBS SPOT` | 165 `H/OBS SPOT` |
| **(3) ALU_OP** count | — (8-dt era) | 33 `H/OBS SPOT` | 60 (+27 INT/UINT) `H/OBS SPOT` | 64 (+ABS_MAX/MIN/RE_LU/SQUARE) `H/OBS SPOT` | ≡64 `H/OBS SPOT` | 65 (+SYMMETRIC_CLAMP) `H/OBS SPOT` |
| **(4) struct2opcode** keys | legacy | 89 `H/OBS` | 99 `H/OBS` | 108 `H/OBS` | ≡108 `H/OBS` | 114 `H/OBS` |
| **(5) DTYPE** count | 8 (`TONGA_ISA_TPB_DTYPE_*`, subset) `H/OBS` | 16 (base) `H/OBS SPOT` | 16 (≡SUNDA) `H/OBS SPOT` | 24 (+FP4_EXP2 0x10, CPTC1..7 0x19–0x1F) `H/OBS SPOT` | ≡24 `H/OBS SPOT` | 30 (+FP8_EXP2 0x11, INT4 0x12, SFP8 0x13–0x16, MXTENSOR_V2) `H/OBS SPOT` |

> **The 64-byte instruction word is INVARIANT across all six rows incl TONGA**
> (`NEURON_ISA_TPB_INST_NBYTES = 64`; every operand struct `sizeof == 64`,
> compile-verified). The underlying Vision-Q7 FLIX ISA (14 formats / 46 slots /
> 8 regfiles / 1,534 mnemonics / 12,642 OPCODEDEF placements) is a property of
> the Q7 core IP, not the GPSIMD generation. [DX-05 §1.2; GEN-12 §1] H/OBS.

> **GOTCHA — header-declared vs image-shipped.** A gen's `arch_isa` header
> declares a **superset namespace**; the gen's IMAGE ships a **subset** (the
> kernel body + the `kernel_info` row). For example SUNDA's header declares
> `0x7b TENSOR_DEQUANTIZE` and the `DEQUANT_FMT` family even though SUNDA's POOL
> image ships no NX-side MX. The matrix below distinguishes **header-declared**
> (the namespace) from **image-shipped** (a kernel body present) — this is the
> key framing the per-gen image pages use. [GEN-12 §1.3; sunda-v2-baseline #778] H/OBS.

> **NOTE — `tpb/` header-file counts.** The byte-true OBSERVED `tpb/` `.h` counts
> (`fd --no-ignore -e h` / `find -type f -name '*.h'`, re-run this pass) are
> **99 / 108 / 117 / 123** (SUNDA / CAYMAN / MARIANA / MAVERICK), monotone (+9/+9/+6) — a
> capability signature. M_PLUS reuses the MARIANA `tpb/` dir (no separate header tree).
> This agrees with [arch-isa-header-diff.md](arch-isa-header-diff.md); an earlier
> `100/109/118/124` reading on this page was off by +1 per gen (a counted umbrella/index
> `.h`) and is **corrected to 99/108/117/123**. [SPOT §9; arch-isa-header-diff #781] H/OBS.

### Block B — Engines, firmware, RNG, MX, DGE

| Subsystem | TONGA | SUNDA (v2) | CAYMAN (v3) | MARIANA (v4) | M_PLUS (v4+) | MAVERICK (v5) |
|-----------|-------|------------|-------------|--------------|--------------|---------------|
| **(6) ENGINES** | — | 5 NX (PE/ACT/POOL/DVE/SP) + Q7_POOL `H/OBS` | 5 NX + Q7_POOL `H/OBS` | 5 NX + Q7_POOL `H/OBS` | ≡MARIANA roster `H/OBS` | **ACT FOLDED into DVE** — no `NX_ACT` image; DVE is head; PE/DVE ×4, SP ×16 `H/OBS SPOT` |
| **(7) FW HANDLERS** | — | monolithic NCFW dispatch (no table); RELEASE only `H/OBS` | 5-engine table dispatch (178-entry SEQ @DRAM 0x80814) `H/OBS` | retains all CAYMAN + adds DVE/PE/ACT MX+RNG handlers; dispatch-set ADD=0 `H/OBS` | ≡MARIANA handler set (+0/−0); +DGE fast-path linked `H/OBS SPOT` | DEBUG dropped on PE/POOL(NX)/SP; MX unified; QuantizeMx → DVE bind; SP+Q7 from SRAM `H/OBS (interiors M/INF)` |
| **(8) RNG** | — | SW Xorwow on Q7 POOL `H/OBS` | SW Xorwow/XorwowRng (RAND XORWOW appends) `H/OBS` | TIE+LFSR (Xorwow-TIE + LfsrGet/SetSeeds) + PeManageSeed `H/OBS` | ≡MARIANA `H/OBS` | TIE+LFSR retained (impl M/INF, stripped) `H/OBS` |
| **(9) MX** | — | POOL-Q7 dequant only (0x7b TensorDequantize) `H/OBS SPOT` | POOL-Q7 dequant (≡SUNDA); no FP4/CPTC `H/OBS` | DVE/PE split: QUANTIZE_MX 0xe3 (DVE), MATMUL_MX 0x0A / LDWEIGHTS_MX 0x09 (PE) `H/OBS SPOT` | ≡MARIANA MX surface `H/OBS` | **UNIFIED**: 0x09/0x0A folded → Matmul via MXTensorV2 ADDR4 marker; QUANTIZE_MX 0xe3 binds DVE; POOL MX surface = 0x7b TensorDequantize `H/OBS hdr (fold M/INF)` |
| **(10) DGE FAST-PATH** | — | NONE `H/OBS` | NONE `H/OBS` | NONE `H/OBS` | **ADDED gen-wide** (dge_decode_fast / dge_reshape_memcopy_transpose_fast / tensor_reshape_transpose_sb2sb / wait_for_credit) `H/OBS SPOT` | **DROPPED** (re-IP to HW DMA; only dge_shape infra survives) `H/OBS` |

> **QUIRK — MX fold at v5 keeps the 0x09/0x0A enumerators.** The MAVERICK header
> still **lists** `LDWEIGHTS_MX = 0x09` and `MATMUL_MX = 0x0A` (SPOT confirmed this
> pass), but the runtime enters MX mode by the **MXTENSOR_V2 ADDR4 marker**
> (`start_addr` LSB set) alone — no separate opcode dispatched — plus the
> `MX_PERF_MODE` QUAD/OCT row-pump for 4×/8× throughput. The opcodes keep their
> values (zero drift); only the *entry mechanism* is re-parameterized. This is a
> SCALING-with-re-encode special case, not an opcode removal. [DX-01 §8; DX-05 §2.3] H/OBS.

> **QUANTIZE_MX 0xe3 binds DVE on v5** — it is absent from POOL KITs; the POOL MX
> surface is the gen-invariant `0x7b TensorDequantize` (SPOT: present on both
> SUNDA and MAVERICK headers). The named QuantizeMx handler roster dropped 60→59
> on the POOL side. [maverick-profile #780; images cross-gen-kernel-info-matrix] H/OBS (POOL detail M/INF).

### Block C — Collectives, transport, security, NCFW, PROF, build, structural

| Subsystem | TONGA | SUNDA (v2) | CAYMAN (v3) | MARIANA (v4) | M_PLUS (v4+) | MAVERICK (v5) |
|-----------|-------|------------|-------------|--------------|--------------|---------------|
| **(11) COLLECTIVES** (op surface) | — | pseudo-ops + RDMA legs + P2P + ring/mesh(50-ev)/hier; **NO SB2SB 0xBF** `H/OBS SPOT` | +SB2SB 0xBF + 12-entry NCFW table + 108-ev mesh + gather_xpose `H/OBS SPOT` | ≡CAYMAN (ADD=0 coll ops) `H/OBS SPOT` | ≡CAYMAN (0 new coll bytes) `H/OBS` | same coll ops/algos; **SYNC re-model** (EVENT family dropped; +SEM_*_REG_OFFSET 0x91–0x95, UNORDERED 0xfe, NONBLOCKING); 0xBF present `H/OBS hdr (re-lower M/INF)` |
| **(11b) COLL-REDUCE dtype** | — | 6 `{BF16,FP16,FP32R,FP8_E3/E4/E5}` `H/OBS` | ≡6 (GEN-INVARIANT) | ≡6 | ≡6 | ≡6 (no superset growth) `H/OBS` |
| **(12) DMA/TRANSPORT** | — | SDMA; raw RDMA d2d; HBM Cayman-class; 1 SP/TPB; SBUF 32 MiB `H/OBS` | SDMA; io_d2d (DWC-PCIe + Marvell XSR SerDes, 216-trig); 256 GiB HBM; 2-die `H/OBS` | SDMA; ≡Cayman io_d2d / HBM / die `H/OBS` | ≡MARIANA (reg refresh: hbm_xbar→crc_hash, +xbar_ctrl) `H/OBS` | **re-IP**: DDMA/CDMA/UDMA; native UCIE 2nm (39 links); 512 GiB HBM (512 DDR ctrl); 3-die C/H/IO; SBUF 128 MiB (×4); SP ×16; UDMA window 0x40000→0x80000 `H/OBS pkl (host tuple M/INF)` |
| **(13) SECURITY/RAS** | — | baseline; errtrig pair; 239 SDMA-trig; no nts_isolation `H/OBS` | centralized errtrig pair + peb_intc apex; 254 SDMA-trig `H/OBS` | +AXI-parity/checker + remapper ID-widen 8→10b (fabric hardening) `H/OBS` | ≡MARIANA + 9 intc_*_enums.svh `H/OBS` | decentralized: 13 per-IP INTCs; iofic_x8_msix + int_sec_grp SWOM; 119-entry per-die apex; 256 SDMA-trig `H/OBS` |
| **(14) NCFW** | — | MONOLITHIC (23 fns; 50-ev mesh; 4 soc CSRs) `H/OBS` | TABLE dispatch (DRAM+0xB0 12-entry; 57 fns; 108-ev; 20 CSRs) `H/OBS` | ≡CAYMAN, reloc +0x18 (DRAM byte-identical) `H/OBS` | ≡MARIANA DRAM (sha 1c3ac5f4); IRAM +DGE code delta `H/OBS` | **NO NCFW IMAGE** (libncfw tops at 0x1c) — not observable `H/OBS (negative)` |
| **(15) PROF** | — | NONE (no PROF getters) `H/OBS` | SHARED (1 CAM 8fd7e422 / 1 TABLE ce761f81, all 4 NX) `H/OBS` | PER-ENGINE (ACT 326bc0dd / DVE ca588683 / PE 43475cec / POOL 0951b326) `H/OBS` | REUSE (per-engine CAM+TABLE byte-identical from MARIANA) `H/OBS` | RE-AUTHORED (new per-engine CAM+TABLE on DVE/PE/POOL); SP never has PROF `H/OBS` |
| **(16) BUILD** | clang/2018 (copyright; V1 separate pkg) `H/OBS` | XtensaTools-14.09 / clang-10 / EXTISA ET_EXEC `H/OBS` | ≡SUNDA (14.09 / clang-10) `H/OBS` | ≡CAYMAN `H/OBS` | ≡MARIANA (recompile-reloc, not independent build) `H/OBS` | XtensaTools-15.05 / clang-15.0.7 / ET_DYN (PIC, stripped); genuine independent build (~6–8% block sim to M+ twin) `H/OBS` |
| **(17) STRUCTURAL** (reset / DEBUG / shipping) | — | reset geometry pending; DEBUG ABSENT (RELEASE only); 24 getters `H/OBS (reset pending)` | NX j 0x1dc / Q7 j 0x200; enter_run @0x90; DEBUG on all; 100 getters `H/OBS` | NX j 0x1f8 (+0x1c shift); enter_run @0x90; DEBUG on all; 100 getters `H/OBS` | reset ≡MARIANA; @0x90; DEBUG on all; 100 getters `H/OBS` | enter_run @0x94 (+4 v5 marker); NX-compute j 0x1d8 / Q7-SRAM j 0x1e4; DEBUG only on NX_DVE+Q7_POOL; internal-twin-exclusive (187/0); 62 getters `H/OBS SPOT` |

> **NOTE on the 64-byte operand-struct family.** All operand structs are 64B on
> every modern gen; TONGA's `INST_EVENTS` is 4B vs the modern `EVENTS` 8B
> (+32-bit `semaphore_value`). The legacy→modern transition (TONGA→SUNDA) was a
> naming/organization rewrite — per-mnemonic `*_INST` → shape-coded `*_STRUCT` —
> over the SAME 64-byte word. [arch-isa-header-diff #781; GEN-12 §3] H/OBS.

This grid is **15 subsystems × 5 generations (+ TONGA V1 floor)**. The
opcode×gen view (which named opcodes / KIT keys exist per gen) is the companion
Rosetta in [cross-gen-kernel-info-matrix.md](../images/cross-gen-kernel-info-matrix.md);
the opcode-table step-diff (with TONGA) is the peer synthesis in
[cross-gen-opcode-diff.md](cross-gen-opcode-diff.md) — built independently from
the binary so the two corroborate.

---

## 3. The strict-superset chain + the partial-order exceptions

The generation **number** advances monotone on **six independent anchors**, all
in lockstep oldest→newest:

1. coretype `{6,13,21,29,37}` (deltas `{+7,+8,+8,+8}`) — SPOT (bitmask).
2. arch_id `{5,12,20,28,36*}` = coretype − 1 — CARRIED (36* INFERRED).
3. NCFW selector bytes `{0x05,0x0c,0x14,0x1c}` — CARRIED.
4. NC-ISA banners NC-v2 < v3 < v4 < v5 + the cumulative `NEURON_CORE_VERSION`
   enum — SPOT.
5. The DTYPE / ALU_OP / OPCODE / struct2opcode superset chains — SPOT (all four).
6. The clang-10 → clang-15 / 14.09 → 15.05 / EXEC → ET_DYN toolchain bump at
   MAVERICK — CARRIED.

No anchor contradicts another. But the per-subsystem **capability** does not
advance at every step:

### A — Strict-superset subsystems (each step ADDS; codes never change meaning)

| Axis | SUNDA | CAYMAN | MARIANA | M_PLUS | MAVERICK |
|------|:-----:|:------:|:-------:|:------:|:--------:|
| ISA OPCODE | 145 ⊂ | 150 ⊂ | 159 | ≡159 | ⊂ 165 |
| ALU_OP | 33 ⊂ | 60 ⊂ | 64 | ≡64 | ⊂ 65 |
| DTYPE | 16 ≡ | 16 ⊂ | 24 | ≡24 | ⊂ 30 |
| struct2opcode | 89 ⊂ | 99 ⊂ | 108 | ≡108 | ⊂ 114 |

The **only removals** in the whole chain: SUNDA's `CUSTOM_OP_HEADER`/`PAYLOAD`
(and `nx_map`) dropped at CAYMAN, plus the **7 v2-only BF16/dual-ptr ops**
(`0x87 0x88 0x8a 0x8b 0x8c 0x8d 0x8f`) retired at the v3 re-baseline — their
bytes ABANDONED, **never reused (ZERO byte-reuse across the chain)**. CAYMAN's
`JPEG_DECODE = 0x81` is **not** a reuse — it fills a **pre-existing free byte**
(`0x80`/`0x81` are permanent holes in all four gens; the SUNDA enum jumps
`DROPOUT 0x7f → TRANSPOSE_BATCH_NORM_STATS2 0x82`). A stray `0x8a–0x8d/0x8f` in a
v3+ stream is a decode error or a v2 artifact. [DX-05 §3.3; cross-gen-opcode-diff #782] H/OBS.

### B — Partial-order EXCEPTIONS (where strict-superset does NOT hold)

- **(E1) Collective-op surface is FLAT in the middle.**
  `SUNDA ⊂ {CAYMAN ≡ MARIANA ≡ MARIANA_PLUS} ⊂ MAVERICK`. The only post-CAYMAN
  collective delta is MAVERICK's SYNC re-model (EVENT family dropped;
  UNORDERED/REG_OFFSET/NONBLOCKING added).
- **(E2) Collective-reducible dtype set is GEN-INVARIANT** — a fixed 6, no
  superset growth. The wider compute-dtype superset is COPY-able byte movement,
  not reducible.
- **(E3) DMA / transport / HBM / die is a generation SWAP (re-IP), NOT a
  superset.** Cayman-class family (SUNDA..M+) ⊏ MAVERICK re-IP (DDMA/CDMA/UDMA +
  UCIE + 512-ctrl HBM + 3-die). A Cayman d2d/SerDes fact does **not** transfer to
  Maverick.
- **(E4) NCFW dispatch tops out and goes dark.** SUNDA-monolithic ⊏ CAYMAN-table
  ≡ MARIANA(+reloc) ≡ M+(+DGE); MAVERICK is **not-NCFW-observable** (no v5 image)
  — a gap, not a superset rung.
- **(E5) DGE fast-path is NON-MONOTONE** — none → ADDED(v4+) → DROPPED(v5). A
  feature that appears then disappears: the clearest non-superset edge.
- **(E6) PROF / DEBUG-image / engine-count are STRUCTURAL re-shapes at MAVERICK**
  (PROF re-authored; DEBUG dropped on PE/POOL/SP; ACT folded into DVE) — not
  additions.

> **The two MAVERICK non-monotone edges (the most-cited exceptions).** MAVERICK
> is the one gen that is **NOT a strict superset** of its predecessor: it
> **DROPS** the DGE fast-path (E5) and **FOLDS** ACT into DVE (E6, with 5 ACT
> handlers absent firmware-wide and `NX_ACT` FILE-ABSENT — SPOT: `img_MAVERICK_NX_*`
> has no ACT, vs `img_MARIANA_NX_ACT` present). It also re-IPs transport (E3) and
> goes NCFW-dark (E4). Everything ELSE about MAVERICK is additive (DTYPE/ALU_OP/
> OPCODE/struct all superset). [GEN-12 §2; maverick-profile #780] H/OBS.

See [arch-isa-header-diff.md](arch-isa-header-diff.md) for the full
register-map/CSR step diff and the SUNDA flat-schema outlier.

---

## 4. The per-gen "what's new" ledger (the lattice edges)

[oldest→newest; each ADD anchored to a committed page.]

**TONGA (V1) → SUNDA (v2)** — *the legacy→modern rewrite.*
ISA reorg: per-mnemonic `*_INST` → shape-coded `*_STRUCT`; `TONGA_` → `NEURON_`
name family; `INST_EVENTS` 4B → `EVENTS` 8B (+32-bit `semaphore_value`). +8 dtype
codes (8→16). Runtime identity introduced (coretype 6 / arch_id 5 / NCFW v2 /
`NRTUCODE_CORE_SUNDA`). Same 64B word.

**SUNDA (v2) → CAYMAN (v3)** — *the reference-SoC jump.*
+ `SB2SB_COLLECTIVE` (0xbf on-chip S3D3 reduce-copy) + gather_xpose/DmaTranspose;
+ the +27 INT/UINT ALU_OP family (33→60); transpose/sparse-matmul structs
(89→99; OPCODE 145→150); + `CONV_LUT_LOAD 0xe4` (CAYMAN-first); + RAND XORWOW
appends; + the DRAM+0xB0 12-entry NCFW TABLE dispatch (vs monolithic) + 108-event
mesh + 20 soc CSRs + the full 4-lib EXTISA; + PROF SHARED + the multi-flavor
(DEBUG/PERF/TEST) image model (24→100 getters). DTYPE UNCHANGED (16≡16).
− `CUSTOM_OP_HEADER`/`PAYLOAD` (the only structural removals).

**CAYMAN (v3) → MARIANA (v4)** — *the MX / dtype jump.*
+ the MX matmul trio (`MATMUL_MX 0x0A` / `LDWEIGHTS_MX 0x09` / `QUANTIZE_MX 0xe3`;
99→108; OPCODE 150→159) + `PeManageSeed 0x08` (MARIANA-first) + FP4_EXP2/CPTC1..7
dtypes (16→24) + `MEM_PATTERN` indirect-addressing union; + 4 ALU_OPs (60→64);
+ TIE-HW Xorwow + LFSR RNG (SW→TIE); + PROF SHARED→PER-ENGINE; + the AXI-parity/
checker + remapper master-ID-widening fabric hardening; + the reset +0x1c shift
(NX j 0x1dc→0x1f8). COLLECTIVE OPS UNCHANGED. NCFW = CAYMAN +0x18 code reloc.

**MARIANA (v4) → MARIANA_PLUS (v4+)** — *the narrowest step (recompile / flag-refresh, NOT a silicon step).*
The one functional add: the **gen-wide DGE reshape fast-path** (`dge_decode_fast`
/ `dge_reshape_memcopy_transpose_fast` / `tensor_reshape_transpose_sb2sb` /
`wait_for_credit`) on all 5 NX images incl the lean SP; REGWRITE-emit retired.
+ the +8 identity slot (coretype 29 / arch_id 28 / sel 0x1c); the flag
`NRTUCODE_MPLUS_ON_MARIANA` → `NEURON_RT_DBG_V4_PLUS=0/1`. + the register-map
refresh (hbm_xbar ctrl→crc_hash, +9 INTC enums, +xbar_ctrl). **Identical opcode/
KIT key-sets v4↔v4+**, byte-identical PROF + EXTISA Q7 kernels + NCFW DRAM
(sha 1c3ac5f4) + reset. PROF REUSED byte-identical. **The only gen in the line
that adds NO ISA and NO model change.** See [mariana-plus-delta.md](mariana-plus-delta.md).

**MARIANA_PLUS (v4+) → MAVERICK (v5)** — *the one real silicon step at the top.*
+ FP8_EXP2/INT4/SFP8/MXTENSOR_V2 dtypes (24→30); +1 ALU_OP (SYMMETRIC_CLAMP,
64→65); +6 v5 opcodes (`ACTIVATE_MULTIPASS 0x26`, `COMPACT_CONTROL_INST 0xb6`,
`DMA_MEMCPY2 0xb9`, `DMA_IMMEDIATE 0xba`, `TENSOR_TENSOR_INT_WIDE 0xf3`,
`TENSOR_SCALAR_INT_WIDE 0xf4`; 108→114; OPCODE 159→165).
+ **MX UNIFIED** into Matmul via MXTensorV2 (0x09/0x0A deprecated as dispatch);
**DGE fast-path DROPPED**; **ACT FOLDED** into DVE; PROF RE-AUTHORED; DEBUG
dropped on PE/POOL/SP; QUANTIZE_MX 0xe3 binds DVE (POOL MX surface = 0x7b).
+ the SYNC re-model (EVENT family dropped; +SEM_*_REG_OFFSET 0x91–0x95,
UNORDERED 0xfe, EVT_SEM_NONBLOCKING_CMD); + the transport swap (SDMA→DDMA/CDMA/
UDMA; SerDes→native UCIE 2nm 39 links + HW H_DIE_SCRATCHPAD); + topology uplift
(SBUF ×4 → 128 MiB; SP ×16; PE/DVE ×4; PSUM host-CSR-driven; DGE 512 KiB stub);
+ HBM 256→512 GiB (273→512 PHY / 512 DDR ctrl; HBM_XBAR_8X32/4X2) + 3-die; + the
decentralized security re-architecture; + clang-10→15 / EXEC→ET_DYN; new reset
geometry (enter_run @0x94 invariant; NX-compute j 0x1d8 / Q7-SRAM j 0x1e4).
**NO NCFW IMAGE** (internal-twin only). COLLECTIVE OPS / reducible-dtypes
UNCHANGED from MARIANA. **~60–65% smaller independent build.** v5 INTERIORS are
HEADER/getter/dispatch-OBSERVED only → tag every v5-interior cell INFERRED. See
[maverick-profile.md](maverick-profile.md).

---

## 5. The top cross-gen invariants (the reimplementation-stable core)

What NEVER changes across the line — the bytes a reimplementer hard-wires once:

| Invariant | Evidence | Conf |
|-----------|----------|:----:|
| **The Vision-Q7 ISA opcode namespace** — zero value-drift v2..v5 (the sunda↔maverick value-mismatch join is EMPTY; ~138 shared ops keep their v2 byte through 3 steps). Write-once: new gens only APPEND. | DX-05 §1.1; DX-GEN-04 §5.1 | H/OBS |
| **The single fixed `ncore2gp` microarch** — `XCHAL_HW_MIN_VERSION == HW_MAX == 281040`; ConfigID `0xC4019686:0x2908E4E3`. 8 regfiles, 14-format/46-slot FLIX, fixed pipeline, 1,534-mnemonic roster. | DX-05 §1.2; DX-HW-01 | H/OBS |
| **The 64-byte instruction word** — `INST_NBYTES = 64` on every gen incl TONGA; every operand struct `sizeof == 64`. | GEN-12 §4(V) | H/OBS |
| **The SEQ engine model** — every NX engine on every gen is the same ASCII-opcode dispatch sequencer; the 18-handler control core is the 5-way intersection (the SP roster). | GEN-12 §4(I) | H/OBS |
| **The 0xF0 ExtendedInst NX→Q7 bridge** — POOL-exclusive escape op routing to the Q7_POOL `kernel_info` back-end (the reason only POOL is dual-core); on every gen shipping POOL. | GEN-12 §4(II) | H/OBS |
| **The engine_idx enum** — `PE=0, ACT=1, POOL=2, DVE=3, TPB_SP=4, TOP_SP=5` on every gen's header **incl maverick** (the ACT fold is firmware-IMAGE level, NOT an ISA-enum removal — SPOT). | GEN-12 §4(III); SPOT §9 | H/OBS |
| **The globstruct magic `0x6099CB34`** (Q7 ready sentinel) + host claim `0x502B2DA1` — byte-identical CAYMAN/MARIANA/MAVERICK/SUNDA; reset state nx=1 / q7=0xFF. | DX-05 §1.4 | H/OBS |
| **The frozen Q7-control CSR core** — shared-7 bundle sha `eeebb647`, q7-run-stall `f764ef74`, hw_decode `c7ee050e`; byte-identical cayman..maverick. | DX-05 §1.3 | H/OBS |
| **The XEA3 interrupt controller** — 37-entry table, RER/WER external-register controller, 25 BInterrupt pins; fixed by the ncore2gp config. | DX-05 §1.5 | H/OBS |
| **The collective-reducible dtype set** — `{BF16,FP16,FP32R,FP8_E3/E4/E5}` (cce_dtypes @libnrt 0x9b9f40) + the 9 collective pseudo-ops + the CCE/COLLECTIVE_TYPE enums; gen-invariant incl maverick header. | GEN-12 §4(VI) | H/OBS |
| **The NCFW ctx_log sub-structs** — ring channel 148B, mesh event 80B, hier 8B, barrier step 52B, ring_ctx 16B; byte-identical across all 4 NCFW gens. | GEN-12 §4(VIII) | H/OBS |
| **The FIS sprot stack** (remapper → qos_prot → nsm) + the errtrig PAIR INTC leaf — universal on every gen; nsm byte-identical. | GEN-12 §4(IX) | H/OBS |

---

## 6. The gen-ordering + capability-tier lattice (the partial order)

The lattice with TONGA as the disconnected V1 floor below SUNDA. `⊳` = generation
succession; `⊂`/`⊊` = strict capability superset; `≡` = capability-identical;
`⊏` = re-IP swap (not a superset).

```
                      ┌─────────────── COMPUTE / ISA axis (strict superset) ───────────────┐
   TONGA(V1)  ⊳  SUNDA  ⊊  CAYMAN  ⊊  MARIANA  ≡  MARIANA_PLUS  ⊊  MAVERICK
   (outside the      (16dt/    (16dt/     (24dt/      (24dt/64alu,      (30dt/65alu,
    envelope —        33alu)    60alu)     64alu)      +DGE fast-path)   −DGE, ACT-fold)
    no opcode
    roster)      └── COLLECTIVE-OP axis (flat in the middle) ──┘
                  SUNDA  ⊂  {CAYMAN  ≡  MARIANA  ≡  MARIANA_PLUS}  ⊂  MAVERICK (sync re-model)

                  └── TRANSPORT / HBM / die axis (Cayman-class family ⊏ re-IP swap) ──┘
                  SUNDA  ≡  CAYMAN  ≡  MARIANA  ≡  MARIANA_PLUS   ⊏   MAVERICK (UCIE/3-die/512-ctrl)

                  └── NCFW axis (tops out, then dark) ──┘
                  SUNDA(monolithic)  ⊏  CAYMAN(table)  ≡  MARIANA  ≡  M+(+DGE)  ;  MAVERICK = not observable
```

- **SUNDA** is the in-line structural **FLOOR** — it diverges on more than the
  scaling axes (a genuinely different flat `tpb_nx_local_reg` CSR schema, no
  hw_decode bundle, no CC, host-resident DKL), but it carries the SAME invariant
  core (opcode namespace, ncore2gp microarch, XEA3 boot spine + magic words).
  It is the smallest parameter point, **inside** the envelope.
- **TONGA** is the **OUTLIER** — outside the envelope (§7).
- **CAYMAN ≡ MARIANA ≡ MARIANA_PLUS** form a flat collective/device-CSR tier;
  MARIANA adds MX/dtype on the ISA axis; M+ adds zero ISA bytes and zero
  device-CSR bytes (the same parameter point as MARIANA on both scaling axes).
- **MAVERICK** keeps the MARIANA opcode superset but re-IPs transport, widens the
  sync fabric, folds MX, folds ACT, and drops DGE — additive on ISA, swap on
  transport, non-monotone on DGE/ACT.

---

## 7. The INVARIANT / SCALING / ABSENT partition + the gen-invariance verdict

Every capability partitions into exactly one of three classes. The partition is
the page's thesis payload.

### INVARIANT (12 components) — byte/struct-identical across all Q7-bearing gens

The Vision-Q7 ISA opcode namespace; the PSEUDO_OPCODE band (0xc1–0xdf); the base
datapath block (0x41–0x7f); the ncore2gp microarch config; the 8 register files;
the FLIX decoder (14/46/7); the pipeline/bypass model; the 1,534-mnemonic roster;
the shared-7 TPB-local CSR bundle (sha `eeebb647`) with its q7 (`f764ef74`) and
hw_decode (`c7ee050e`) sub-bundles; the XEA3 boot/reset spine + the
`0x6099CB34`/`0x502B2DA1` handshake; the XEA3 interrupt controller (37-entry
RER/WER); the per-core Xtensa debug IP (Plane C). **Evidence: a hash, a
fixed-config byte, or an empty-mismatch join — OBSERVED, not inferred.**

### SCALING (9 axes) — monotonic / additive, one scalar/enum each

CSR-bundle count (7→11, +4 atomic_*); Q7 geometry (64K/64K → 128K/256K + v5 slot
re-home 0x100000→0x80000); opcode count (145→165, append-only after v3);
struct2opcode (89→114); DTYPE set (16→30); the MX encoding sub-axis (v4 separate
PE pair → v5 MXTENSOR_V2 fold); the UDMA window (0x40000→0x80000); the cross-die
transport IP (RDMA → io_d2d/PCIe → UCIE); the sync fabric (EVT_SEM →
+COLLECTIVE_SYNC/GPSIMD_SYNC).

### ABSENT-PER-GEN (11 presence flips) — appear/disappear by flag, not re-encoding

`hw_decode` (absent@v2, present@v3+); NCFW runtime image (v2..v4+ shipped, v5
ABSENT); the 7 retired BF16/dual-ptr ops (live@v2, abandoned@v3+); COMPUTE_CLUSTER
(absent@v2, =4@v3+); `SB2SB_COLLECTIVE 0xBF` (absent@v2, present@v3+); MX opcodes
(absent@{v2,v3}, present@{v4,v5}); host-resident DKL swap regs (@v2 device,
@v3+ firmware); Q7_CC_TOP firmware (present@v3..v4+, ABSENT@v5 in this build);
the EVENT wait/update primitive (present@v2..v4+, DROPPED@v5); the **DGE fast-path**
(absent → ADDED@v4+ → DROPPED@v5); the **ACT image** (present@v2..v4+, FOLDED@v5).

> **Partition counts: 12 INVARIANT components, 9 SCALING axes, 11 ABSENT-PER-GEN
> presence flips** (the DGE non-monotone edge and the ACT fold are the two flips
> internal to the v4→v5 step).

### The formal gen-invariance statement

> **There exists a SINGLE Vision-Q7 reimplementation `R(Q7)`** — one FLIX decoder
> over the fixed 8-regfile / 14-format / 46-slot microarch, one XEA3 boot/reset
> spine (ready `0x6099CB34` / claim `0x502B2DA1`, reset nx=1 / q7=0xFF), one XEA3
> interrupt controller (37-entry RER/WER), one frozen Q7-control CSR core
> (shared-7 `eeebb647`, q7 `f764ef74`, hw_decode `c7ee050e`), and one write-once
> opcode→semantics map (the v2..v5 zero-drift namespace) — **such that `R(Q7)`,
> parameterized by the SCALING axis vector `P` and gated by the ABSENT-PER-GEN
> flag set `F`, EXACTLY reproduces ALL FIVE generations** SUNDA(v2), CAYMAN(v3),
> MARIANA(v4), MARIANA_PLUS(v4+), MAVERICK(v5).
>
> `P = ⟨ csr_bundle_count ∈ {7,11}, q7_geometry, opcode_set_version ∈ {145,150,159,165}, transport_ip ∈ {RDMA, io_d2d/PCIe, UCIE}, udma_window ∈ {0x40000,0x80000} ⟩`
>
> `F = ⟨ has_hw_decode, has_NCFW_image, has_CC, has_SB2SB, has_MX, dkl_resident, has_Q7_CC_TOP_fw, has_DGE_fastpath, has_ACT_image, sync_event_primitive ⟩`
>
> **MARIANA_PLUS is the SAME parameter point as MARIANA** on both scaling axes
> (`P_{v4} = P_{v4+}` on opcode and device-CSR). **SUNDA is the smallest parameter
> point** (the floor), still inside the envelope. **TONGA (V1) is excluded** — it
> predates the `NEURON_ISA_TPB_OPCODE` namespace, exposes only a register-block
> ISA, uses the distinct `TONGA_ISA_TPB_DTYPE_*` family, and ships in a separate
> package with zero runtime identity.

> **THE VERDICT.** A reimplementer writes **ONE engine + a per-gen capability
> table**, NOT five engines. The decoder carries one microarch and one opcode
> map; it selects live opcode membership + the device-CSR/geometry envelope by
> reading the coretype `(6/13/21/29/37)` and applies the presence flags `F`. The
> INVARIANT core is the SAME bytes on every gen (hash-verified); only `P` and `F`
> differ per gen. This is the thesis the orientation
> [gen-invariance page](../orientation/gen-invariance.md) is built on.

---

## 8. Walls / confidence (what is OBSERVED, what is carried, what is INFERRED)

- **v2–v4+ cells: byte-grounded (HIGH × OBSERVED).** Every count, sha, enum value,
  and selector for SUNDA/CAYMAN/MARIANA/MARIANA_PLUS is read from a shipped header
  or symbol table — many re-verified this pass (§9).
- **MAVERICK / v5 cells: header/getter/dispatch-level OBSERVED; interiors
  INFERRED.** The ISA delta (165 opcodes, 30 dtypes, MXTENSOR_V2, SEM_*_REG_OFFSET)
  is read directly from the shipped `maverick_arch_isa` headers + the customop-lib
  v5 firmware getters; the runtime geometry/transport/NCFW interiors are INFERRED
  Cayman-class+ (no v5 runtime programmer in this checkout). **Tag every
  v5-interior cell INFERRED.**
- **arch_id 36 is INFERRED (doubly — §1 GOTCHA); coretype 37 is OBSERVED.**
- **TONGA cells are OBSERVED from its own legacy ISA package** (`arch-isa/` family,
  `arch-headers/tonga` register-map), never from a `neuron_tonga_arch_isa` tree
  (which does not exist).
- **Carried walls** (open items the capstone does not close): the FW-42 RNG seed
  bytes are CARRIED; the FLIX-desync interiors (the NCFW LX management-core legs
  are un-disassemblable in the shipped config); the SortMerge phantom; the empty
  MODULE_SCHEDULE; the v5 `Q7_CC_TOP` firmware is FILE-ABSENT (no MAVERICK
  Q7_CC_TOP getters in this build); the SUNDA per-engine reset geometry is
  PENDING (monolithic NCFW, IMG-23/24/25 not carved).

---

## 9. Spot-verification this pass (the cells re-carved from the binary)

A capstone re-verifies; it does not merely transcribe. The following grid cells
were carved **directly** from the shipped artifacts this pass (all under the
gitignored `extracted/.../custom_op/c10/`, read with `--no-ignore`). Every other
grid cell is **CARRIED** from a committed page with its own binary anchor.

| # | Cell | Re-verification this pass | Result |
|---|------|---------------------------|:------:|
| 1 | **(2/3/5) ISA superset** | `python` enum parse of `NEURON_ISA_TPB_{DTYPE,ALU_OP,OPCODE}` in each gen's `common.h` | DTYPE 16/16/24/30, ALU_OP 33/60/64/65, OPCODE 145/150/159/165 — **PASS** |
| 2 | **(1) IDENTITY** | `nm libnrtucode_internal.so \| rg '_libs$'` | 5 symbols at `0x9b8f80/90, 0x9b8fd0, 0x9b9010, 0x9b9050` — **PASS** |
| 3 | **(1) MAVERICK split** | `strings INT/SO \| rg -o MAVERICK \| wc -l` | internal 187 / front 0 — **PASS** |
| 4 | **(1) coretype gate** | `python` bitmask `0x2020202000`; `objdump \| rg -c 'cmp.*\$0x25'` | bits `{13,21,29,37}`; 6 hits — **PASS** |
| 5 | **NC banners** | `rg 'ISA header for NC-v[0-9]'` per gen | v2/v3/v4/v5 — **PASS** |
| 6 | **(11) SB2SB 0xBF** | `rg 'SB2SB_COLLECTIVE = 0x..'` per gen | ABSENT@SUNDA, `0xbf`@cayman/mariana/maverick — **PASS** |
| 7 | **(9) MX opcodes** | `rg 'LDWEIGHTS_MX\|MATMUL_MX\|QUANTIZE_MX'` per gen | absent@{sunda,cayman}; `0x09/0x0A/0xe3`@{mariana,maverick} — **PASS** |
| 8 | **(10) DGE fast-path** | `strings INT \| rg -c 'dge_reshape_memcopy_transpose_fast\|tensor_reshape_transpose_sb2sb\|dge_decode_fast'` | 10/13/10 + `NEURON_RT_DBG_V4_PLUS` ×1 — **PASS** |
| 9 | **(6) ACT-fold** | `strings INT \| rg -o 'img_MAVERICK_NX_[A-Z]+'` vs `img_MARIANA_NX_*` | MAVERICK = {DVE,PE,POOL,SP} **no ACT**; MARIANA = {ACT,DVE,PE,POOL,SP} — **PASS** |
| 10 | **engine_idx enum** | `rg 'ACT = 1'` in maverick `common.h` | `ACT=1` still in the ISA enum (fold is image-level) — **PASS** |
| 11 | **arch-isa dirs** | `ls neuron_*_arch_isa` / `ls arch-headers/*` | 4 ISA dirs (no `mariana_plus`); 6 reg-map dirs (incl `mariana_plus`, `tonga`) — **PASS** |
| 12 | **(2) header counts** | `fd --no-ignore -e h neuron_<g>_arch_isa/tpb \| wc -l` | 99/108/117/123 (byte-true; agrees with #781) — **PASS** |
| 13 | **(9) POOL dequant** | `rg 'TENSOR_DEQUANTIZE = 0x..'` sunda/maverick | `0x7b` on both (POOL MX surface invariant) — **PASS** |

All 13 PASSED. The carved cells span IDENTITY / ISA / DTYPE / ENGINES / MX / DGE /
COLLECTIVE / structural — at least one cell per generation, re-grounded against
the bytes this session.

---

## 10. Cross-references

**The five per-generation pages:**

- [codename-generation-map.md](codename-generation-map.md) — the identity
  contract; the four reconciled enumerations; the arch_id-36-doubly-inferred wall.
- [sunda-v2-baseline.md](sunda-v2-baseline.md) — the v2 floor; the
  header-declared-superset vs image-shipped-subset framing.
- [mariana-plus-delta.md](mariana-plus-delta.md) — the v4+ recompile/flag-refresh
  step (DGE fast-path the lone functional add).
- [maverick-profile.md](maverick-profile.md) — the v5 silicon step (ACT-fold,
  MX-unify, DGE-drop, transport re-IP, security re-architecture).
- [arch-isa-header-diff.md](arch-isa-header-diff.md) — the per-gen CSR/register-map
  + DTYPE/ALU_OP/OPCODE/struct step diff; the TONGA→modern struct transition.

**The cross-gen syntheses:**

- [cross-gen-opcode-diff.md](cross-gen-opcode-diff.md) — the opcode-table step-diff
  (with TONGA) — the peer synthesis; built independently from the binary so the
  two corroborate.
- [cross-gen-kernel-info-matrix.md](../images/cross-gen-kernel-info-matrix.md) —
  the opcode×gen KIT Rosetta (which named handler/KIT key exists per gen).

**The thesis + the crosswalk:**

- [gen-invariance.md](../orientation/gen-invariance.md) — the orientation thesis
  this matrix is the payload for.
- [codename-crosswalk.md](../reference/codename-crosswalk.md) — the
  codename ↔ arch_id ↔ coretype ↔ NC-version quick reference.
