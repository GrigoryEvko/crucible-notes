# neuronx-gpsimd Internals — The Vision-Q7 GPSIMD Reimplementation Reference

> *A reimplementation-grade reverse-engineering reference for AWS Neuron's **GPSIMD** engine — the custom-op / collective compute substrate inside every Trainium and Inferentia NeuronCore. Reconstructed entirely from static analysis of shipped binaries, device disassembly, in-process execution of the shipped simulator libraries, and the recovered Tensilica config / TIE / ISA tables. There is no access to source.*

This is the front door. It tells you in one screen **what GPSIMD physically is**, the **one-machine / seven-faces** mental model, the **five NeuronCore generations** that share that one machine, the **reimplementation verdict** (what is byte-grounded versus honestly flagged), and a navigable **tour of the 16 Parts** below it. Everything past this page descends into detail; read this first so the rest has a frame.

---

## What GPSIMD physically is

GPSIMD is **not** a bespoke AWS accelerator and **not** a custom ISA. It is a single, off-the-shelf **Cadence Tensilica Vision-Q7 NX DSP** in one frozen "Cairo" configuration, instantiated **eight times** as the per-NeuronCore **POOL** compute cluster, wrapped in a per-generation SoC envelope.

The identity is pinned to the byte. The shipped processor-generator config file `ncore2gp-params` reads, verbatim:

```
ConfigName        = Xm_ncore2gp        CoreID   ncore2gp
uarchName         = Cairo              arch     Xtensa24   (XEA3)
TargetHWVersion   = NX1.1.4            (= LX7.1.4, the NX-/LX-family equivalence)
HWMicroArchEarliest = 281040           HWMicroArchLatest = 281040   ← zero-width window
HWConfigID0       = 0xC4019686         HWConfigID1       = 0x2908E4E3
SWToolsRelease    = RI-2022.9
```

`HWMicroArchEarliest == HWMicroArchLatest == 281040` is the decisive fact: the software targets **exactly one** hardware revision, with no version range. The shipped `xtensa-versions.h` decodes that integer — `XTENSA_HWVERSION_RI_2020_4 281040 /* versions NX1.1.4, LX7.1.4 */` — so the **HW IP** is the RI-2020.4 release (NX1.1.4 = LX7.1.4) while the **toolchain** that builds for it is RI-2022.9. Every product generation loads device firmware built for this *one* config; the single shipped `ncore2gp` `xtensa-elf-objdump` disassembles every generation's device image. `[HIGH/OBSERVED]`

What it actually **does** in the product is narrow and specific. The marketing phrase "GPSIMD kernel" (RmsNorm, Softmax, MoE) is a *multi-engine* micro-schedule whose float math runs on the Vector / Scalar / Tensor engines — **not** on the Q7 cores. The literal Q7 GPSIMD cluster owns exactly three lanes:

- the native-**integer** int32/uint32 add/sub/mul datapath (opcode `0x41`, int-routed);
- the **gather / scatter / cross-lane-reduce / iota / custom-op** lane (a hand-authored PyTorch op rides `0xF0 EXTENDED_INST` and DMAs its own Q7 image);
- the **SB2SB collective hop** (`0xBF`) — one intra-node step of a ring all-reduce.

> **GOTCHA — the keystone fact reimplementers get wrong.** GPSIMD is *not* the float hot-path, and it **cannot reach PSUM at all**. The Q7 cores address the on-chip State Buffer (SBUF) directly through an AXI aperture, but the PE-private PSUM accumulator is simply not in the Q7 address space. The compiler enforces this: an int32/uint32 op routes to GpSimd, but **falls back to the Vector engine if any operand lives in PSUM**. Model GPSIMD as SBUF/HBM-only. `[HIGH/OBSERVED — keystone CC finding]` See [Keystone Facts](orientation/keystone-facts.md).

---

## The one machine, seven faces

There is **one** physical machine here. The wiki keeps returning to it from seven angles; holding all seven at once is the orientation a reimplementer needs. (Full treatment: [The Seven Faces of the One Machine](orientation/seven-faces.md).)

| # | Face | What it is | Where it lives |
|---|------|------------|----------------|
| 1 | **The Core** | The Cadence Vision-Q7 NX "Cairo" DSP — `ncore2gp`, XEA3, 512-bit FLIX/VLIW, 8 register files | [Part 2](isa/core/identity-config.md) |
| 2 | **The Engine Context** | 8 Q7 cores as the **POOL** engine, one of the five TPB engines (PE / ACT / **POOL** / DVE / SP); the "not the float hot-path" fact | [Part 4](uarch/microarch-synthesis.md) · [Part 12](compiler/compiler-map.md) |
| 3 | **The Custom Op** | NKI → BIR → NEFF → install → Q7 → completion: a real freestanding PyTorch op in a libc/libc++ sandbox on the DSP | [Part 7](abi/abi-synthesis.md) |
| 4 | **The Collective** | Reductions that **leave the compute engines** and happen *in the SDMA datapath* (CCE in-transfer reduce) over the die mesh | [Part 10](collectives/ops/architecture-synthesis.md) |
| 5 | **The Generation** | One gen-invariant core R(Q7) + a small scaling envelope covers all five gens | [Part 6](generations/codename-generation-map.md) |
| 6 | **The Security** | Fabric + firmware; **no cryptographic root of trust** on the device load path; fail-stop faults | [Part 13](control/security/security-synthesis.md) |
| 7 | **The Evidence** | The ISA / ISS / VAL execution-validated basis — the binary itself is the value arbiter | [Part 14](iss/iss-oracle-synthesis.md) · [Part 15](validation/capstone-matrix.md) |

**The one-paragraph mental model.** A NeuronCore is a Tensor-Processing-Block (TPB) of five engines — **PE** (the 128×128 systolic Tensor array, the only writer of PSUM), **ACT** (the Scalar / activation / PWL engine), **POOL** (the GPSIMD cluster: 8 Vision-Q7 DSP cores), **DVE** (the Vector engine), and **SP** (the collective / sync front-end) — all sharing one on-chip SBUF. The host compiler `neuronx-cc` lowers a traced PyTorch / NKI graph into per-engine 64-byte sequencer streams packed into a **NEFF** container; the host runtime `libnrt` loads the NEFF, DMAs any custom Q7 image into Q7 instruction memory over BAR0, boots the cores, and fires a doorbell; each engine's sequencer fetches its stream and dispatches opcodes to its datapath; collectives reduce in the SDMA fabric in-flight; completion semaphores release the host. The 8 Q7 cores run one **SPMD** image (rank = PRID ∈ {0..7}, count = 8) over per-core-private memory — **data-race-free by construction**, not by synchronization. `[HIGH — synthesized across the lanes]`

> **NOTE — three distinct cores, do not conflate.** "**Q7**" is the Vision-Q7 POOL/DVE DSP (the subject of this wiki). "**NCFW**" is a *separate* scalar Xtensa-**LX** core that manages collective firmware. "**TOP_SP**" is the NX sequencer that walks the `cc_op` collective program. Three cores in the stack, three different ISAs.

### Identity quick-reference

| Field | Value | Confidence |
|---|---|---|
| Core ID / config | `ncore2gp` · `ConfigName = Xm_ncore2gp` · uarch `Cairo` | HIGH/OBSERVED |
| Arch / exception model | `Xtensa24` = **XEA3** (single-context, single-dispatch, 37-entry cause table, RER/WER controller) | HIGH/OBSERVED |
| HW revision | **NX1.1.4** = LX7.1.4 (RI-2020.4 IP) · toolchain RI-2022.9 | HIGH/OBSERVED |
| HW accept window | `281040 .. 281040` (zero-width) | HIGH/OBSERVED |
| Config IDs | `0xC4019686` / `0x2908E4E3` | HIGH/OBSERVED |
| Issue engine | **FLIX** VLIW: **14 formats / 46 slots** (`num_formats=0xe`, `num_slots=0x2e` in `libisa-core.so`); the 14 formats decode to **4 distinct instruction byte-lengths** `{2,3,8,16}` (`tie.h: XCHAL_OP0_FORMAT_LENGTHS`); 256-bit instbuf, max issue width 5 | HIGH/OBSERVED |
| Register files (8) | `vec` 512b×32 · `wvec` 1536b×4 · `valign` 512b×4 · `vbool` 64b×16 · `gvr` 512b×8 · `b32_pr` 64b×16 · `AR` 32b×64 · `BR` 1b×16 | HIGH/OBSERVED |
| Datapath | 512-bit SIMD (32 lanes × 16-bit) + quad-MAC widening into the 1536-bit `wvec` accumulators | HIGH/OBSERVED |
| Shipped opcode roster | **1,534** Vision-Q7 mnemonics (1,607 pre-fold TIE-DB; 12,642 placements) | HIGH/OBSERVED |
| Value-function leaves | **864** `module__xdref_` leaves in `libfiss-base.so` (`nm`-counted) | HIGH/OBSERVED |
| SPMD width | **8** cores (linker specs `lsp_fll_load_cpu0..7`) | HIGH/OBSERVED |
| Boot handshake | unbooted sentinel `0x6099CB34` → host CAS-writes claim `0x502B2DA1` | HIGH/OBSERVED |

Full per-field grounding: [Core Identity & Configuration](isa/core/identity-config.md), [The FLIX VLIW Encoding](isa/core/flix-encoding.md), [The Eight Register Files](isa/core/register-files.md).

---

## The five generations — one core, parameterized

The five GPSIMD product generations are **not five processors**. They are one Vision-Q7 "Cairo" core wrapped in a per-generation SoC envelope. The host runtime even spells the mapping out: the decompiled `nrtucode_get_ext_isa_internal` (`@0x9b2b30`) switches on the **coretype** byte directly — the `case` constants below are literal in the binary `[HIGH/OBSERVED]`:

```c
switch (coretype) {                  // arch_id column = coretype − 1 (a stride, not a
  case  6: ... sunda_libs;           //                  literal switch — INFERRED): 0x05
  case 13: ... cayman_libs;          //                                                0x0c
  case 21: ... mariana_libs;         //                                                0x14
  case 29: ... mariana_plus_libs;    //                                                0x1c
  case 37: ... maverick_libs;        //                                                0x24
}
```

> **NOTE — coretype is OBSERVED; arch_id is derived.** The `{6,13,21,29,37}` coretype constants are read directly from the decompiled switch. The `arch_id` values `0x05/0x0c/0x14/0x1c/0x24` are *not* a literal table anywhere in these artifacts — each is `coretype − 1`, so the whole `arch_id` column is **INFERRED** (the v5 row most acutely; see the wall below).

| Codename | NeuronCore | coretype (OBSERVED) | arch_id (= ct−1, INFERRED) | Status in this corpus |
|---|---|---|---|---|
| **SUNDA** | v2 (Trn1 / Inf2) | 6 | `0x05` | byte-grounded · the in-line floor (flat CSR schema, no hw_decode / no CC / no SB2SB) |
| **CAYMAN** | v3 (Trn2) | 13 | `0x0c` | byte-grounded |
| **MARIANA** | v4 | 21 | `0x14` | byte-grounded |
| **MARIANA_PLUS** | v4+ | 29 | `0x1c` | byte-grounded · ISA-/CSR-identical to MARIANA; differs only in NCFW image + register-map dir |
| **MAVERICK** | v5 | 37 (OBSERVED) | `0x24`/36 (**INFERRED**) | header-OBSERVED only · v5-interior claims flagged INFERRED |
| *TONGA* | v1 (legacy "L") | — | — | **outlier** — pre-unified, register-block ISA, no opcode roster, no runtime identity |

> **The gen-invariance thesis (in one line).** There exists a single Vision-Q7 reimplementation R(Q7) — one FLIX decoder, one microarch, one XEA3 boot/IRQ spine, one frozen Q7-control CSR core, one **write-once** opcode→semantics map (zero opcode-value drift v2→v5; the full-span sunda↔maverick mismatch join is *empty*) — that, parameterized by a small **scaling** vector and gated by a handful of **presence** flags, exactly reproduces SUNDA through MAVERICK. The scaling axes are monotonic/additive: CSR-bundle count (7→11), Q7 geometry (IRAM/DRAM 64K/64K→128K/256K), opcode count (≈145→165, append-only after v3), and cross-die transport (raw-RDMA → io_d2d/PCIe → UCIe). `[HIGH/OBSERVED for v2–v4+; v5 SoC-envelope INFERRED]` See [The Gen-Invariance Thesis](orientation/gen-invariance.md).

> **CORRECTION / wall — flag this, do not fabricate it.** MAVERICK (v5) is **header-OBSERVED only**. There is no shipped v5 NCFW image: `libncfw_get_image` tops out at MARIANA_PLUS, and `arch_id > 0x1C → return 2` sends `arch_id 0x24` straight to the unsupported path (no `cmp` against `0x24`). Corroborated by exactly **four** codename `ctx_log` symbols (sunda/cayman/mariana/mariana_plus), **zero** maverick. So `coretype 37` is OBSERVED, but **arch_id 36/0x24 is INFERRED** from the coretype−1 stride. Any claim about v5 *interiors* on these pages is tagged INFERRED. `[wall]` `[ct37 OBSERVED · arch_id 36 INFERRED]`

> **TONGA is out of scope by design.** TONGA (V1, the legacy "L"-family) predates the unified `NEURON_ISA_TPB_OPCODE` namespace, exposes only a register-block ISA (a 1-byte opcode *field*, no enumerated roster), and ships in a separate package with zero runtime identity. R(Q7) covers exactly v2..v5; the V1→V2 bridge is not in its domain.

---

## The reimplementation verdict

After the analysis wave closed, the GPSIMD / Vision-Q7 "Cairo" toolchain is a **byte-grounded, execution-validated reimplementation reference for the v2–v4 silicon**, and a **header-OBSERVED + bounded-INFERRED reference for v5 (MAVERICK) and v1 (TONGA)**. The single most powerful technique behind that: the shipped `libfiss-base.so` value leaves are driven **live, per input, via `ctypes`**, so the binary itself is the arbiter of value semantics.

| Axis | Result | Confidence |
|---|---|---|
| **Value semantics** | **100%** — all 864/864 `module__xdref_` leaves resolved to a root with per-element semantics; the element function of every GPSIMD value opcode is known | HIGH/OBSERVED |
| **Execution-validated** | **~95%** of value-bearing leaves proven-by-execution against the live shipped simulator across 18 op families / **~2.09M** in-process comparisons; **zero** firmware value bugs found | HIGH/OBSERVED · MED for the exact % |
| **Encoding** | **1,534/1,534** shipped (1,607 pre-fold; 12,642 placements) — a certified-perfect, non-overlapping cover; canonical encoder `libisa-core.so` bit-exact | HIGH/OBSERVED |
| **Struct census** | **~169/171** domain structs field-exact (~99%); every struct on the path a custom op travels is recovered | HIGH/OBSERVED |
| **Runtime** | host load/install/execute/reap spine + device-side `.o` contract field-exact (v2–v4) | HIGH/OBSERVED |
| **Microarch** | every fact carries a config-FILE citation (`Xm_ncore2gp` / `NX1.1.4` / `0xC4019686`) | HIGH/OBSERVED |
| **Security** | complete boot→attest→fault model; the GPSIMD path touches **no** crypto / sqlite / codec / ffi (attack surface proven small) | HIGH/MED |

The residual is a **small, fully-named, fully-categorized ledger of twelve open questions** — and **none** of them is a missing datapath body, a missing opcode decode, or a missing value semantics. Each is a true static-analysis boundary on *this* shipped corpus, each with an explicit "what would close it" (a different checkout, a FlexNet license key, a runtime/hardware capture, or a follow-on static pass). The full register is in [The Reimplementation Verdict & Open-Questions Map](orientation/verdict-and-open-questions.md); two named walls a reader meets repeatedly:

- **`arch_id 36` (v5) — INFERRED.** Closes with a libncfw checkout that ships the coretype-37 firmware image.
- **`ct37` (v5 coretype) — OBSERVED.** Read directly from the host ext-ISA switch; it is the only firm v5 identity datum.

> **QUIRK — no signing key, ever.** The GPSIMD device load path contains *no* signature verification anywhere. Admission of a Q7 image is integrity (an **unkeyed** hash) + structural (ELF/reloc/core-count) + version (the ucode semver gate). The trust root is the **host OS / process boundary**, not a hardware root of trust — and the install seam `nrt_set_pool_eng_ucode` is a silent, unauthenticated override. This is a deliberate single-tenant trusted-compiler design, not an oversight.

---

## A tour of the 16 Parts

The reference is a journey: a reimplementer reads roughly top-to-bottom to rebuild a Vision-Q7-compatible GPSIMD engine. **Part 0** is the apparatus (how to read, the confidence model, methodology, the corpus inventory, the crosswalk, the glossary). **Parts 1–16** descend from orientation, through the core and ISA, into firmware, the ABI, the runtime, the data plane, the collectives, the container, the compiler seam, the control plane, the executable oracle, validation, and the appendices.

| Part | Subject — what you rebuild here | Dir |
|---|---|---|
| **1 — Orientation** | The one-screen map, the seven faces, the keystone facts, two end-to-end worked lifecycles, the gen-invariance thesis, the verdict | [`orientation/`](orientation/what-gpsimd-is.md) |
| **2 — Q7 Core & ISA Foundations** | Core identity, the FLIX 14/46/7 encoding, the 8 register files, the FP sub-ISA, the TIE database, the libisa decode model | [`isa/core/`](isa/core/identity-config.md) |
| **3 — Per-Instruction ISA Reference** | The 30-batch per-opcode reference (vector ALU, MAC, loads/stores, reduce, shuffle, convert, gather, scatter) + formal semantics | [`isa/ref/`](isa/ref/template-and-partition.md) |
| **4 — Microarchitecture & Timing** | Pipeline + co-issue matrix, regfile ports/bypass, boot/reset, LSU/memory, atomics ordering, the SIMD datapath, the VFPU, the NCFW LX core | [`uarch/`](uarch/microarch-synthesis.md) |
| **5 — Device Firmware & Kernel Catalog** | SEQ front-end, POOL dispatch, DGE, and the 140-opcode kernel catalog (every device kernel body) | [`firmware/`](firmware/kernels/opcode-catalog-ledger.md) |
| **6 — Firmware Images & Generations** | Per-(gen × engine) firmware images + the codename↔generation map and capability matrix | [`images/`](images/firmware-image-catalog.md) · [`generations/`](generations/codename-generation-map.md) |
| **7 — Custom-Op ABI** | `Q7PtrType`, the `at::Tensor` chain, the `customop_*` marshalling, `build_custom_op.py` codegen, the LSP/ELF layout, the FlexLM gate, the device-side ABI | [`abi/`](abi/abi-synthesis.md) |
| **8 — Host Runtime** | The `libnrt` surface, the `nrtucode` subsystem + device bring-up, the HAL, execute-time dispatch, the 8-core SPMD model, the prelinker | [`runtime/`](runtime/runtime-synthesis.md) |
| **9 — DMA / Descriptors / Memory** | The descriptor model, gather/scatter, cross-die RDMA, CCE in-transfer compute, the SBUF/PSUM bank model, the `al_udma` engine | [`dma/`](dma/descriptor-model.md) |
| **10 — Collectives & NCFW** | The collective ops (`TriggerCollective`, `ALL_REDUCE`, SB2SB, barriers, sendrecv) + the scalar-LX NCFW management core | [`collectives/`](collectives/ops/architecture-synthesis.md) |
| **11 — NEFF Container Format** | The byte-level container (1024-byte header + in-memory tar), `metaneff`, per-engine microcode, relocation/weights, a concrete carve | [`neff/`](neff/format-reference.md) |
| **12 — Compiler Seam** | The GPSIMD-relevant compiler map, `SundaISel`, the MX microscaling path, BIR→ISA lowering, the collective load-time rewrite, the NKI frontend | [`compiler/`](compiler/compiler-map.md) |
| **13 — Control Plane** | The SoC address map, the CSR field tables, the XEA3 interrupt/exception architecture, and the boot/fault/security model | [`control/`](control/address/unified-soc-memory-map.md) |
| **14 — ISS as Executable Oracle** | `libcas-core` / `libfiss-base` reconstructed as a runnable value/cycles oracle — "given a bundle, here is the exact lib+symbol that computes its value" | [`iss/`](iss/iss-oracle-synthesis.md) |
| **15 — Validation & Verification** | The 4-oracle bit-exact differential method + per-family pass/fail (soft-float, MAC, convert, reduce, gather, predicate, transcendental) | [`validation/`](validation/four-oracle-method.md) |
| **16 — Appendices** | Struct census, the opcode↔kernel↔engine matrix, the master ISA encoding appendix, the CSR field-table index, the crosswalk, the open-questions register, the reimplementation checklist | [`appendix/`](appendix/reimplementation-checklist.md) |

### Start here

1. **[What GPSIMD Is — the one-screen map](orientation/what-gpsimd-is.md)** — the orientation a reimplementer needs first.
2. **[Keystone Facts Reimplementers Get Wrong](orientation/keystone-facts.md)** — the PSUM wall, the not-the-float-hot-path fact, the three-cores rule.
3. **[The Confidence & Walls Model](reference/confidence-model.md)** — what the HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED tags mean and which walls are real.
4. **[Methodology — How This Was Reverse-Engineered](reference/methodology.md)** — what "binary-derived" means here, and what is provably *not* recoverable.
5. **[A Custom Op, End to End](orientation/customop-end-to-end.md)** / **[A Collective, End to End](orientation/collective-end-to-end.md)** — the two worked lifecycles, byte by byte.

---

## Reading conventions

Every non-trivial claim on these pages is tagged inline with a **confidence × evidence** pair:

- **Confidence** — `HIGH` (byte-exact disasm / direct header or config read / sha-verified / `nm`/`jq` ground truth) · `MED` (strong cross-file inference) · `LOW` (plausible, flagged).
- **Evidence** — `OBSERVED` (read directly from a shipped artifact) · `INFERRED` (reasoned over OBSERVED facts) · `CARRIED` (OBSERVED in a cited analysis, reused at its confidence).

Callout markers you will meet: **QUIRK** (a counter-intuitive but real behavior), **GOTCHA** (a trap that breaks a naive reimplementation), **CORRECTION** (a fact that overturns an earlier or naive reading), **NOTE** (orienting context). The two standing walls — `arch_id 36` (v5, INFERRED) and `ct37` (v5 coretype, OBSERVED) — are carried wherever a v5 claim appears. See [The Confidence & Walls Model](reference/confidence-model.md) and [Master Glossary](glossary.md).

## Companion wikis

- [`neuronx-cc/wiki/`](../../neuronx-cc/wiki/) — the compiler that lowers NKI/HLO into the BIR `InstCustomOp` / `InstCollective` nodes and embeds GPSIMD kernels into NEFFs.
- [`neuronx-runtime/wiki/`](../../neuronx-runtime/wiki/) — the `libnrt` host runtime that loads NEFFs and dispatches custom-op kernels onto the Q7 cores.
