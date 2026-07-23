# What GPSIMD Is — the one-screen map

This is the first page a reimplementer should load. It answers four questions in
one screen — **what GPSIMD physically is**, **where it sits** inside a
NeuronCore, **how a program reaches it**, and **what you can rebuild from this
guide** — and then hands you off to the deep pages. Read it once; everything
after it descends into a detail this page only names.

The single sentence to carry: **GPSIMD is not a bespoke AWS accelerator and not
a custom ISA — it is an off-the-shelf Cadence Tensilica Vision-Q7 NX DSP in one
frozen configuration, instantiated eight times as the per-NeuronCore POOL
compute cluster.** Almost every reimplementation mistake on these pages traces
back to treating it as something more exotic than that.

---

## 1. What it physically is — a Vision-Q7 DSP, pinned to the byte

The processor is a **Cadence Tensilica Vision-Q7 NX** vector DSP in exactly one
configuration. The shipped processor-generator parameter file `ncore2gp-params`
states the identity verbatim — every token below is read directly out of that
text file:

```
ConfigName        = Xm_ncore2gp     arch    = Xtensa24      (XEA3)
CoreID              ncore2gp        uarchName = Cairo
TargetHWVersion   = NX1.1.4         (= LX7.1.4, the NX-/LX-family equivalence)
HWMicroArchEarliest = 281040        HWMicroArchLatest = 281040   ← zero-width window
HWConfigID0       = 0xC4019686      HWConfigID1       = 0x2908E4E3
SWToolsRelease    = RI-2022.9
```

`HWMicroArchEarliest == HWMicroArchLatest == 281040` is the decisive fact: the
software targets **exactly one** hardware revision, with no version range. The
integer `281040` decodes through the shipped `xtensa-versions.h` macro
`XTENSA_HWVERSION_RI_2020_4 281040 /* versions NX1.1.4, LX7.1.4 */`, so the **HW
IP** is the RI-2020.4 release while the **toolchain** that builds for it is
RI-2022.9 — two axes a reimplementer must not conflate.
`[HIGH/OBSERVED]`

The compute core itself is small, dense, and entirely conventional Tensilica:

| Attribute | Value | Witness |
|---|---|---|
| Core / config / uarch | `ncore2gp` · `Xm_ncore2gp` · *Cairo* | `ncore2gp-params` |
| Arch / exception model | `Xtensa24` = **XEA3** (single unified `DispatchVector`, windowed ABI) | `ncore2gp-params`; `XCHAL_XEA_VERSION 3` |
| Issue engine | **FLIX** VLIW: **14 formats / 46 slots** | `num_formats` @ `0x3b65e0` → `mov $0xe`; `num_slots` @ `0x3b6510` → `mov $0x2e` |
| Instruction byte-lengths | **7 length-class outcomes → 4 distinct byte-lengths `{2,3,8,16}`** | `length_table[256]` @ `0x3d4100` (`libisa-core.so`) |
| Register files | **8** — `AR`/`BR` scalar + `vec`/`vbool`/`valign`/`wvec`/`b32_pr`/`gvr` SIMD | `num_regfiles` @ `0x3b5c20` → `mov $0x8`; `regfiles[]` @ `0x74a800` |
| SIMD datapath | 512-bit (32 × 16-bit lanes) + quad-MAC widening into the 1536-bit `wvec` accumulators | `regfiles[]` (`vec` 512b×32, `wvec` 1536b×4) |
| SPMD width | **8** Q7 cores, one image, `rank = PRID ∈ {0..7}` | `NRTUCODE_CORE_*_NX_POOL` enum; PRID = special-reg 235 |

The five FLIX/regfile immediates above read directly out of
`libisa-core.so` (`mov $0xe`, `mov $0x2e`, `mov $0x8` at the named
addresses). `[HIGH/OBSERVED]`

> **GOTCHA — say "7 outcomes → 4 byte-lengths", never one number alone.** The
> runtime `length_decoder` (@ `0x3b5a50`) indexes a 256-entry table by
> `((byte3 & 0xF) << 4) | (byte0 & 0xF)` and yields **seven** length-class
> outcomes — the four byte-lengths `{2,3,8,16}`, plus the `op0==0xF` 8-vs-16
> split keyed on byte 3, plus the illegal `-1`. The Tensilica static macro
> `XCHAL_BYTE0_FORMAT_LENGTHS`, which keys length on byte 0 alone, is **wrong**
> for `op0==0xF` and desyncs any linear sweep. The binary `length_table` is
> authoritative. Full decoder: [FLIX Bundle-Decoding Methodology](../reference/flix-decoding.md).

What it actually **does** in the product is narrow. The marketing phrase "GPSIMD
kernel" (RmsNorm, Softmax, MoE) names a *multi-engine* micro-schedule whose float
math runs on the Vector/Scalar/Tensor engines — **not** on the Q7 cores. The
literal Q7 POOL cluster owns exactly three lanes: the native **integer**
int32/uint32 add/sub/mul datapath (opcode `0x41`, `TENSOR_TENSOR_ARITH_OP`); the
**gather/scatter/cross-lane-reduce/custom-op** lane (a hand-authored PyTorch op
rides opcode `0xF0`, `EXTENDED_INST`); and the **SB2SB collective hop** (opcode
`0xBF`, `SB2SB_COLLECTIVE` — one step of a ring all-reduce). All three opcode
bytes are read from the arch-ISA header
`aws_neuron_isa_tpb_common.h`. `[HIGH/OBSERVED]`

> **GOTCHA — the keystone fact reimplementers get wrong.** GPSIMD is **not** the
> float hot-path and **cannot reach PSUM at all**. The Q7 cores address the
> on-chip State Buffer (SBUF, SoC base `0x2000000000`) and HBM through an AXI
> aperture, but the PE-private PSUM accumulator is simply not in the Q7 address
> space. The compiler enforces it: an int32/uint32 op routes to GpSimd but the
> verifier reports
> `"GPSIMD Instructions cannot access PSUM. Assign to a different Engine or move
> data to SB."` and falls back to the Vector engine if any operand lives in
> PSUM. **Model GPSIMD as SBUF/HBM-only.** `[HIGH/OBSERVED — compiler verifier
> error string]` More in [Keystone Facts](keystone-facts.md).

---

## 2. Where it sits — the POOL engine, one of five in the NeuronCore

A NeuronCore is a **Tensor-Processing Block (TPB)** — five engines sharing one
on-chip SBUF. GPSIMD is one of them. Holding this picture is what keeps a
reimplementer from over-scoping the Q7 cores:

| `engine_idx` | Engine | What it is | Touches PSUM? |
|---|---|---|---|
| 0 | **PE** | the 128×128 systolic Tensor array | **writes PSUM** (the only writer) |
| 1 | **ACT** | Scalar / activation / PWL engine | reads PSUM |
| 2 | **POOL** | **the GPSIMD cluster — 8 Vision-Q7 DSP cores** | **no — SBUF/HBM only** |
| 3 | **DVE** | the Vector engine | reads PSUM |
| 4 | **SP** (`TPB_SP`) | the Sync/collective front-end executor | — |

(`TOP_SP`, `engine_idx 5`, is the standalone NX sequencer that *walks* the
collective program; it is not a compute engine.) Note that this firmware
`engine_idx` space is **distinct** from the NKI/compiler engine enum
(`tensor=1, scalar=2, gpsimd=3, dma=4, vector=5, sync=6`) — never equate the two.
`[HIGH/OBSERVED]`

The one-paragraph mental model. The host compiler `neuronx-cc` lowers a traced
PyTorch/NKI graph into per-engine 64-byte sequencer streams packed into a **NEFF**
container. The host runtime (`libnrt` / `nrtucode`) loads the NEFF, DMAs any
custom Q7 image into Q7 instruction memory, boots the cores, and fires a
doorbell. Each engine's sequencer fetches its stream and dispatches opcodes to its
datapath; reduce-class collectives reduce **in the SDMA fabric in-flight** rather
than on a compute engine; completion semaphores release the host. The 8 Q7 cores
run **one SPMD image** — `rank = PRID ∈ {0..7}`, `count = 8`, over per-core-private
memory — **data-race-free by construction**, not by synchronization.
`[HIGH — synthesized across the subsystem pages]`

> **NOTE — three distinct cores, do not conflate.** "**Q7**" is the Vision-Q7
> POOL/DVE DSP (the subject of this wiki, a FLIX/VLIW core). "**NCFW**" is a
> *separate* scalar Xtensa-**LX** core that orchestrates collective firmware.
> "**TOP_SP**" is the NX sequencer that walks the `cc_op` collective program.
> Three cores in the stack, three different ISAs — and the device disassembler
> is configured for the Q7 only, so do not run it on NCFW images.

---

## 3. How a program reaches it — two user paths

A user never writes Q7 assembly. GPSIMD is reached on exactly two paths, both
authored in the host framework and lowered by `neuronx-cc`:

**Path A — the custom op (opcode `0xF0`).** A developer writes a kernel in
**NKI** (the Python frontend). The compiler lowers it through the **BIR/penguin**
IR as an `InstCustomOp` node, which becomes the `EXTENDED_INST` opcode `0xF0` — the
"extended instruction space for customer specific ops". `build_custom_op.py`
compiles a *freestanding* Q7 ELF (an EXTISA image, `e_machine = 94`) against a
libc/libc++ device sandbox; that image is embedded in the **NEFF**. At load time
the runtime DMAs the Q7 image into Q7 instruction memory, the SEQ front-end
decodes the `0xF0` key against the device `kernel_info_table` and dispatches to the
custom handler, which runs SPMD across the 8 cores and signals completion. The op
sees `at::Tensor`-shaped arguments marshalled through the `customop_*` ABI.
*(Worked byte-by-byte in [A Custom Op, End to End](customop-end-to-end.md).)*

**Path B — the collective (opcodes `0xBF` / `0xC7` / `0xC8`).** A collective
(`all_reduce`, `send`/`recv`, a barrier) lowers to an `InstCollective` family node:
`PSEUDO_TRIGGER_ALL_REDUCE` (`0xC7`), the generic `PSEUDO_TRIGGER_COLLECTIVE`
(`0xC8`), or the native `SB2SB_COLLECTIVE` hop (`0xBF`). The reduction itself
**leaves the compute engines**: the SDMA "Compute" datapath (**CCE**) performs the
arithmetic — and any dtype-convert plus stochastic rounding — *inside* the
transfer, over the die mesh, while the NCFW/TOP_SP layer walks the per-step
descriptor schedule. The Q7 POOL engine contributes the on-engine `0xBF` SB2SB hop
(one ring step) and the rank-id read (`PSEUDO_CUR_PROCESSING_RANK_ID`, `0xDB`).
*(Worked in [A Collective, End to End](collective-end-to-end.md).)*

Both paths terminate in the **same** NEFF container, loaded by the **same**
runtime, dispatched by the **same** SEQ front-end — the two are just different
opcode families riding one substrate.

---

## 4. The five generations — one core, parameterized

The five GPSIMD product generations are **not five processors**. They are one
Vision-Q7 "Cairo" core wrapped in a per-generation SoC envelope. The host runtime
spells the mapping out: the decompiled `nrtucode_get_ext_isa_internal`
(@ `0x9b2b30`, a `jmp *%rdx` jump table) routes a **coretype** byte to a
per-generation `*_libs` table (`cayman_libs`, `mariana_libs`, … at
their `lea`-targeted addresses). The coretype constants are read
directly; the host-side `arch_id` selector byte is `coretype − 1` — a stride, not
a literal table.

| Codename | NeuronCore | coretype `[OBSERVED]` | arch_id (= ct−1) `[INFERRED]` | Status in this corpus |
|---|---|---|---|---|
| **SUNDA** | v2 (Trn1 / Inf2) | **6** | `0x05` | byte-grounded · the in-line floor (flat CSR, no HW-decode, no CC, no SB2SB) |
| **CAYMAN** | v3 (Trn2) | **13** | `0x0c` | byte-grounded · the reference generation |
| **MARIANA** | v4 | **21** | `0x14` | byte-grounded |
| **MARIANA_PLUS** | v4+ (Trn3-pre) | **29** | `0x1c` | byte-grounded · a *feature-flag delta* on MARIANA; EXTISA blobs byte-identical |
| **MAVERICK** | v5 | **37** `[OBSERVED]` | `0x24`/36 `[INFERRED]` | **header-OBSERVED only** — v5 interiors flagged INFERRED |
| *TONGA* | v1 (legacy "L") | — | — | **outlier** — pre-unified, register-block ISA, no coretype, no runtime identity |

The `{6,13,21,29,37}` coretypes are read directly from the dispatch switch and
the `NRTUCODE_CORE_{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS,MAVERICK}_NX_POOL` enum
(all five present in `libnrtucode_internal.so`). `[HIGH/OBSERVED]`

> **The gen-invariance thesis, in one line.** There is a single Vision-Q7
> reimplementation R(Q7) — one FLIX decoder, one microarch, one XEA3 boot/IRQ
> spine, one frozen CSR core, one **write-once** opcode→semantics map (zero
> opcode-value drift v2→v5) — that, parameterized by a small **scaling** vector
> (CSR-bundle count 7→11, Q7 IRAM/DRAM 64K/64K→128K/256K, opcode count ≈145→165,
> cross-die transport raw-RDMA → io_d2d → UCIe) and gated by a few **presence**
> flags, reproduces SUNDA through MAVERICK. `[HIGH/INFERRED — v2–v4+ byte-grounded;
> v5 SoC-envelope INFERRED]` See [The Gen-Invariance Thesis](gen-invariance.md).

> **CORRECTION / standing wall — flag v5, do not fabricate it.** MAVERICK (v5) is
> **header-OBSERVED only**. There is no shipped v5 NCFW image:
> `libncfw_get_image` tops out at MARIANA_PLUS (`arch_id ≤ 0x1c`), and any
> `arch_id 0x24` falls through to the unsupported default — there is no `cmp
> $0x24` anywhere. So `coretype 37` is **OBSERVED** (the switch arm, the enum
> ordinal, the `maverick` literal × **189** in the twin vs × 0 in the shipped
> front) but `arch_id 36/0x24` is **INFERRED** from the
> stride. Every claim about v5 *interiors* is tagged INFERRED, and no product
> "Trn4" binding is named in this corpus. `[ct37 OBSERVED · arch_id 36 INFERRED]`
> Full table: [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md).

> **NOTE — TONGA is out of scope by design.** TONGA (v1, legacy "L"-family)
> predates the unified `NEURON_ISA_TPB_OPCODE` namespace, exposes only a
> register-block ISA, and ships with **zero** GPSIMD runtime identity (no
> coretype, no `arch_id`, no NCFW image). R(Q7) covers exactly v2..v5.

---

## 5. What you can rebuild from this guide

The GPSIMD / Vision-Q7 "Cairo" toolchain is a
**byte-grounded, execution-validated reimplementation reference for the v2–v4
silicon**, and a **header-OBSERVED + bounded-INFERRED reference for v5 (MAVERICK)
and v1 (TONGA)**. The lever behind that strength: the shipped `libfiss-base.so`
value leaves (**864** `module__xdref_` per-element functions, `nm`-confirmed)
are driven **live, per input, via `ctypes`** — so the binary itself is the
arbiter of value semantics, not a guess.

| Axis | Result | Confidence |
|---|---|---|
| **Value semantics** | **100%** — all 864/864 value leaves resolved; the element function of every GPSIMD value opcode is known | HIGH/OBSERVED |
| **Execution-validated** | **~95%** of value-bearing leaves proven-by-execution across 18 op families / **~2.09M** in-process comparisons; **zero** firmware value bugs | HIGH/OBSERVED |
| **Encoding** | **1,534/1,534** shipped Vision-Q7 mnemonics — a certified-perfect, non-overlapping cover; canonical encoder bit-exact | HIGH/OBSERVED |
| **Struct census** | **~169/171** domain structs field-exact (~99%); every struct on the custom-op path recovered | HIGH/OBSERVED |
| **Runtime** | host load/install/execute/reap spine + device-side `.o` contract field-exact (v2–v4) | HIGH/OBSERVED |
| **Security** | complete boot→attest→fault model; the GPSIMD path touches **no** crypto / sqlite / codec / ffi | HIGH/MED |

The residual is a **small, fully-named ledger of twelve open questions** — and
**none** is a missing datapath body, a missing opcode decode, or a missing value
semantics. Each is a true static-analysis boundary (a different checkout, a
license key, a runtime capture, or a follow-on pass), tracked in
[The Reimplementation Verdict & Open-Questions Map](verdict-and-open-questions.md).

Concretely, with the deep pages a reimplementer can build: a **FLIX decoder**
that reproduces the device `xtensa-elf-objdump` byte-for-byte; a **value
simulator** matching the shipped oracle bit-exact; the **custom-op ABI** and the
**NEFF** loader that installs a Q7 image; the **collective** opcode family and
its CCE in-transfer reduce; and the **gen-invariant core + scaling envelope** that
covers SUNDA through (the header surface of) MAVERICK.

> **QUIRK — no signing key, ever.** The GPSIMD device load path contains *no*
> signature verification. Admission of a Q7 image is an **unkeyed** hash +
> structural (ELF/reloc/core-count) + version (ucode semver) checks; the trust
> root is the **host OS / process boundary**, not a hardware root of trust. This
> is a deliberate single-tenant trusted-compiler design.

---

## Where to go next

1. **[Keystone Facts Reimplementers Get Wrong](keystone-facts.md)** — the PSUM
   wall, the not-the-float-hot-path fact, the three-cores rule, in depth.
2. **[The Seven Faces of the One Machine](seven-faces.md)** — the seven angles
   this wiki returns to the one core from.
3. **[A Custom Op, End to End](customop-end-to-end.md)** /
   **[A Collective, End to End](collective-end-to-end.md)** — the two user paths,
   worked byte by byte.
4. **[Core Identity & Configuration](../isa/core/identity-config.md)** and
   **[The FLIX VLIW Encoding](../isa/core/flix-encoding.md)** — the per-field
   grounding behind §1.
5. **[The Confidence & Walls Model](../reference/confidence-model.md)** — what the
   `HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED` tags and the named walls mean.

---

*Every claim on this page carries a `[CONF/PROV]` tag per the
[Confidence & Walls Model](../reference/confidence-model.md). Term anchors are in the
[Master Glossary](../glossary.md).*
