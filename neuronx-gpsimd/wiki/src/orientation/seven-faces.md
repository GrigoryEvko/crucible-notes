# The Seven Faces of the One Machine

> *There is exactly **one** physical processor in this wiki: a single Cadence Tensilica
> **Vision-Q7 NX "Cairo"** DSP (`ncore2gp`), instantiated eight times as the per-NeuronCore
> **POOL** cluster. Every chapter that follows is a different **face** of that one machine. This
> page is the conceptual spine: it enumerates the seven faces, and for each names the **literal
> opcode byte, symbol, firmware-image accessor, or device string** that selects it — so "it's all
> one core" is a chain of binary anchors, not a slogan.*

The [front door](../index.md) names the seven faces in a table and moves on. This page is where
they are *grounded*: same seven faces, but each one pinned to the byte that activates it and the
deep page that owns it. A reimplementer who holds all seven at once has the orientation the rest
of the guide assumes. The framing is not decorative — it is the structure of the recovered
machine. One core, one [FLIX decoder](../reference/flix-decoding.md), one
[opcode→semantics map](../glossary.md#the-engines), reached through seven dispatch lenses.

> **NOTE — why "faces", not "modes".** A *mode* would be a runtime state the core switches
> between. These are not that. They are **analysis lenses** on a single, unchanging core — the
> same `vec`/`wvec` register files, the same FLIX bundles, the same `kernel_info_table` dispatch,
> viewed from the core, the engine slot, the op, the collective, the generation, the trust
> boundary, and the evidence basis. The core does not "become" a collective engine; the host
> lowers a collective *to* the one core's `0xBF` opcode. The faces overlap on purpose — that
> overlap **is** the one-machine thesis.

The seven, with their canonical anchor and deep page, are the spine of this guide:

| # | Face | The one machine, seen as… | Selected / dispatched by | Deep page |
|---|------|---------------------------|--------------------------|-----------|
| 1 | **The Core** | the Vision-Q7 NX "Cairo" DSP itself | config `ConfigName = Xm_ncore2gp` / `HWMicroArch* = 281040` | [Part 2](../isa/core/identity-config.md) |
| 2 | **The Engine Context** | the **POOL** engine — 8 Q7 cores, one of five TPB engines | a *routing attribute* on a BIR node; opcode `0x41` runs on POOL or DVE | [Part 4](../uarch/microarch-synthesis.md) · [Part 12](../compiler/compiler-map.md) |
| 3 | **The Custom Op** | a freestanding PyTorch op in a libc/libc++ sandbox on the DSP | opcode `0xF0 EXTENDED_INST` → `kernel_info_table` → `callx8 funcVA` | [Part 7](../abi/abi-synthesis.md) |
| 4 | **The Collective** | a reduction that **leaves** the compute engines (in-SDMA CCE) | trigger `0xC7`/`0xC8`/`0xD9`; intra-node hop `0xBF SB2SB` | [Part 10](../collectives/ops/architecture-synthesis.md) |
| 5 | **The Generation** | one gen-invariant core R(Q7) + a scaling envelope | `coretype ∈ {6,13,21,29,37}` → `*_libs` table → EXTISA blob | [Part 6](../generations/codename-generation-map.md) |
| 6 | **The Security** | a fabric + firmware perimeter with **no** crypto root of trust | unkeyed-hash + structural admission; `nrt_set_pool_eng_ucode` override | [Part 13](../control/security/security-synthesis.md) |
| 7 | **The Evidence** | the ISA / ISS / VAL execution-validated basis | the `module__xdref_*` value leaves, driven live via `ctypes` | [Part 14](../iss/iss-oracle-synthesis.md) · [Part 15](../validation/capstone-matrix.md) |

The rest of this page takes each face in turn, states the role it plays, names the exact anchor
that activates it (an opcode byte, a symbol at a hex address, a device trace string, or a firmware
image), and hands off to the page that develops it. Every anchor below is read directly from the
shipped binaries; the ones quoted with a hex address reproduce under
`nm -C`/`rg`/`objdump` against the named file.

---

## Face 1 — The Core

**Role.** Before it is an "engine" or a "custom-op host", GPSIMD is a *processor*: one frozen
Cadence Tensilica configuration. Not a family, not a custom ISA — a single Vision-Q7 NX "Cairo"
build that every other face is a view of.

**The anchor.** The identity is pinned to the byte in the processor-generator config
`ncore2gp-params`: `ConfigName = Xm_ncore2gp`, `CoreID ncore2gp`, `uarchName = Cairo`,
`arch = Xtensa24` (XEA3). The decisive datum is
`HWMicroArchEarliest == HWMicroArchLatest == 281040` — a **zero-width** hardware-accept window:
the software targets *exactly one* HW revision (`NX1.1.4 = LX7.1.4 = RI-2020.4`), with no range.
The issue engine is **FLIX** — `num_formats = 0xE` (14) @ `0x3b65e0` and `num_slots = 0x2E` (46)
@ `0x3b6510` in `libisa-core.so`, both `mov`-immediate byte-verified — over **8 register files**
(`num_regfiles = 0x8` @ `0x3b5c20`): `vec` 512b×32, `wvec` 1536b×4, `valign` 512b×4, `vbool`
64b×16, `gvr` 512b×8, `b32_pr` 64b×16, `AR` 32b×64, `BR` 1b×16. `[HIGH/OBSERVED]`

> **The decoder corollary.** Because there is one config, there is one disassembler:
> `XtensaTools/bin/xtensa-elf-objdump` with `XTENSA_CORE=ncore2gp` — the sole core in its
> registry — decodes *every* generation's device image. The same FLIX decoder that reads a SUNDA
> blob reads a MAVERICK blob. That is Face 5 (the generations) anticipated at the core level.

**Deep page:** [Core Identity & Configuration](../isa/core/identity-config.md), then
[The FLIX VLIW Encoding](../isa/core/flix-encoding.md) and
[The Eight Register Files](../isa/core/register-files.md).

---

## Face 2 — The Engine Context

**Role.** The one core does not stand alone — it is the **POOL** engine (`engine_idx 2`), one of
the five engines of a NeuronCore's **Tensor-Processing-Block (TPB)**: **PE** (the 128×128 systolic
Tensor array, the only writer of PSUM), **ACT** (Scalar / activation / PWL), **POOL** (the eight
Vision-Q7 cores, *this* engine), **DVE** (the Vector engine), **SP** (the collective / sync
front-end). All five share one 32 MiB on-chip SBUF. The orientation fact this face carries is the
one reimplementers most often get wrong: **GPSIMD is not the float hot-path, and it cannot reach
PSUM.**

**The anchor.** The engine is a **routing attribute on a compiler node, not an opcode change** —
the cleanest possible proof that POOL and DVE are the same opcode plane seen from two engines. The
Cayman arch-ISA header `aws_neuron_isa_tpb_common.h` ships, side by side, the predicates
`is_valid_tensor_tensor_arith_pool(i)` and `is_valid_tensor_tensor_arith_dve(i)` — the **same
opcode `0x41`** (`TENSOR_TENSOR_ARITH_OP = 0x41`) validated for *either* engine. The compiler
(`SundaISel`) routes an int32/uint32 `0x41` op to POOL only when no operand lives in PSUM, and
**falls back to the Vector engine** otherwise. The Q7 cores reach SBUF through a 32 MiB AXI
aperture but PSUM is simply **not in the Q7 address space** — model GPSIMD as SBUF/HBM-only.
`[HIGH/OBSERVED]`

> **GOTCHA — the firmware `engine_idx` is not the NKI engine enum.** Firmware numbers the engines
> **PE 0, ACT 1, POOL 2, DVE 3, SP 4, TOP_SP 5**; the NKI/compiler enum is a *different integer
> space* (`tensor=1, scalar=2, gpsimd=3, dma=4, vector=5, sync=6`). Never equate the two. See the
> [glossary engine entries](../glossary.md#the-engines).

**Deep page:** [Microarchitecture Synthesis](../uarch/microarch-synthesis.md) and
[The GPSIMD-Relevant Compiler Map](../compiler/compiler-map.md); the wall itself is in
[Keystone Facts Reimplementers Get Wrong](keystone-facts.md).

---

## Face 3 — The Custom Op

**Role.** Seen from a kernel author, the one core is a place to run a **real, freestanding PyTorch
op** — hand-authored C++ in a libc/libc++ sandbox on the DSP, marshalling its own tensors. This is
the face that makes "GPSIMD" *general-purpose*: a custom op is not a fixed kernel, it is arbitrary
device code DMA'd in and dispatched.

**The anchor.** A hand-authored op rides opcode **`0xF0 EXTENDED_INST`** —
`NEURON_ISA_TPB_OPCODE_EXTENDED_INST = 0xf0`, literally commented *"extended instruction space for
customer specific ops"* in the arch-ISA header. On the device, dispatch is a **linear scan of the
`kernel_info_table`**: an array of **8-byte** entries laid out
`{ u8 0; u8 0; u8 spec; u8 opcode; u32_le funcVA }` — a big-endian key `(opcode << 24) | (spec << 16)`
at `+0`, a relocated little-endian `funcVA` at `+4` (one `R_XTENSA_RELATIVE` per entry, stride 8).
The POOL dispatch loop (`@0x01005610`) reads the table base (`0x02000380`) and end (`0x02000408`),
computes the entry count as `(end − base) >> 3`, and on a key match does `callx8 funcVA`. For
CAYMAN the `0xF0` rows are spec-multiplexed — e.g. spec1 → `pool_extended_inst_copy()` @
`0x01003380`, spec2 → `decode_extended_inst_tensor_tensor_arith` @ `0x010034b0`. A matched custom
op then runs the device ABI
`customop_setup → customop_next_tensor ×N → get_dst_tensor → customop_return_tensor → customop_cleanup`,
each tensor a 48-byte `NEURON_ISA_TPB_CUSTOM_OP_ARG_TENSOR`, fanned out **SPMD across the 8 cores
by PRID**. `[HIGH/OBSERVED]`

> **NOTE — built-in and custom ops converge.** A built-in ISA op (e.g. the `0x41` integer add, no
> device code shipped) and a hand-authored `0xF0` ucode-lib op **meet at the same
> `kernel_info_table` `callx8`**. Both end in a Q7 kernel whose per-lane body is the same bit-exact
> value math (Face 7). The two lanes differ only in *where the funcVA's code came from* — the
> embedded image vs. a DMA'd ucode lib.

**Deep page:** [End-to-End ABI Synthesis](../abi/abi-synthesis.md),
[POOL Extended-Opcode (0xF0) Dispatch](../firmware/pool/pool-ext-0xf0.md),
[kernel_info_table Binary Layout](../firmware/pool/kernel-info-table.md); the byte-by-byte walk is
[A Custom Op, End to End](customop-end-to-end.md).

---

## Face 4 — The Collective

**Role.** Seen as a node in a distributed model, the one core participates in collectives — but
this is the face where the *compute leaves the compute engines*. A reduce-class collective does
its arithmetic **inside the SDMA transfer** (the CCE "compute-DMA" block), not in any Q7 FMA loop.
GPSIMD's literal contribution is one **intra-node hop**.

**The anchor.** Two distinct opcode tiers. The **trigger** tier is host-lowered pseudo-opcodes
that never reach the device datapath: `PSEUDO_TRIGGER_ALL_REDUCE = 0xC7`,
`PSEUDO_TRIGGER_COLLECTIVE = 0xC8`, `PSEUDO_TRIGGER_COLLECTIVE2 = 0xD9` (all carry the `0b110`
pseudo prefix). The NRT host runtime rewrites a trigger via `__select_algorithms` — picking one of
**RING / HIER / MESH / KANGARING / SINGLE_CYCLE_RING** over `(op, size, dtype, topology, gen)` —
and emits a chain of SDMA CCE descriptors that reduce (ADD / FMA / MAX / MIN, with dtype-convert +
stochastic rounding) in-flight. The **device** tier is the one real Q7 collective op:
`SB2SB_COLLECTIVE = 0xBF` ("State-Buffer to State-Buffer"), one intra-node step of a ring
all-reduce, handled on-device by `decode_sb2sb_collective` over the 64-byte
`NEURON_ISA_TPB_S3D3_COLLECTIVE_STRUCT`. `[HIGH/OBSERVED]`

> **QUIRK — the collective and the fused kernel share `0xBF`.** The same `0xBF SB2SB` hop is also
> the partial-statistic exchange inside fused norm/softmax (LNC2) kernels. So the "collective" and
> the "fused-compute" faces literally share a device leg — another overlap that is the
> one-machine thesis in action.

> **NOTE — three cores, do not conflate.** The collective stack involves two *other* cores besides
> the Q7: **NCFW** (a separate scalar Xtensa-**LX** management core that runs the ring/mesh
> schedulers) and **TOP_SP** (an NX sequencer that walks the `cc_op` program). Three cores, three
> ISAs — never run the Q7 FLIX decoder on an NCFW image. See the
> [glossary engine entries](../glossary.md#the-engines).

**Deep page:** [The Unified Collective-Communication Architecture](../collectives/ops/architecture-synthesis.md),
[S3D3 Collective (SB2SB, 0xBF)](../collectives/ops/s3d3-collective.md),
[TOP_SP Collective Lowering](../collectives/ops/top-sp-lowering.md); the worked lifecycle is
[A Collective, End to End](collective-end-to-end.md).

---

## Face 5 — The Generation

**Role.** Seen across products, the one core appears as five "generations" — SUNDA (v2), CAYMAN
(v3), MARIANA (v4), MARIANA_PLUS (v4+), MAVERICK (v5). They are **not five processors**. They are
one Vision-Q7 "Cairo" core (R(Q7)) wrapped in a per-generation SoC envelope, selected at runtime by
a single firmware byte.

**The anchor.** The host resolver `nrtucode_get_ext_isa_internal` (@ `0x9b2b30` in
`libnrtucode_internal.so`) switches on the **coretype** byte directly — the decompiled `case`
constants are literal:

```c
switch (coretype) {            // idx = coretype − 6 into a 32-entry jump table
  case  6: ... &sunda_libs;        // @0x9b8f80
  case 13: ... cayman_libs;        // @0x9b8f90
  case 21: ... mariana_libs;       // @0x9b8fd0
  case 29: ... mariana_plus_libs;  // @0x9b9010
  case 37: ... maverick_libs;      // @0x9b9050
}                              // idx > 0x1f → return 1; any other coretype → return 2 (absent)
```

Each `*_libs` table is a stride-16 array of `{SO_get, JSON_get}` accessor pairs; the device image
is reached through `<GEN>_Q7_POOL_PERF_EXTISA_<n>_SO_get` — e.g.
`CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get` @ `0x9b3aa0`. The blob count per generation is a bitmask in
`nrtucode_get_num_ext_isa_libs` (@ `0x9b2c90`): `0x2020202000` sets bits {13,21,29,37} → **4 libs**
each, while coretype 6 (SUNDA) returns **1** (and uses the `_RELEASE_` not `_PERF_` accessor name).
`[HIGH/OBSERVED]`

> **CORRECTION / the v5 wall — flag, do not fabricate.** `coretype 37` (MAVERICK) is
> `[HIGH/OBSERVED]` (read directly from the switch; the literal `maverick` appears **189×** in
> `libnrtucode_internal.so`, **0×** in the stripped `libnrtucode.so`). But its `arch_id = 36/0x24`
> is `[MED/INFERRED]` from the `coretype − 1` stride: the NCFW image selector `libncfw_get_image`
> (@ `0x1179` in `libncfw.so`) is a `cmpl` ladder on `{0x1c, 0x14, 0x05, 0x0c}` with a
> `ja → default` at any `arch_id > 0x1c` — there is **no `cmp $0x24`** and **no v5 NCFW image** (its
> `.rodata` closes before any maverick blob). So `arch_id 0x24` sends MAVERICK straight to the
> unsupported `return 2`. Every v5 *interior* claim on these pages is tagged INFERRED.

> **GOTCHA — two different generation-ordinal schemes.** The ext-ISA resolver keys on coretype
> `{6, 13, 21, 29, 37}`; the DGE priority-class map (`dge_set_priority_class_map`, `libnrtucode.so`)
> keys on a *different* per-gen ordinal `{2, 9, 17, 25}` for SUNDA/CAYMAN/MARIANA/MARIANA_PLUS. They
> are not the same number space — a cross-package enum divergence to honor, never reconcile by
> arithmetic.

**Deep page:** [Codename ↔ Generation Map](../generations/codename-generation-map.md), with the
formal R(Q7) argument in [The Gen-Invariance Thesis](gen-invariance.md).

---

## Face 6 — The Security

**Role.** Seen as an attack surface, the one core's device load path has **no cryptographic root of
trust**. This is a deliberate single-tenant trusted-compiler design — admission is integrity +
structure + version, and the trust boundary is the host OS/process, not hardware.

**The anchor.** A byte-grounded *negative* finding: a scan of the custom-op path for
RSA/ECDSA/signature/pubkey/x509/hmac turns up only third-party Python packages and the host build's
bundled OpenSSL headers; the secure-monitor lib ships only ldscripts + `mpu_table` + memmap
(`XCHAL_HAVE_SECURE = 0` — no compiled secure-monitor objects). Admission of a Q7 image is an
**unkeyed hash** (integrity, not authentication) + structural ELF/reloc/core-count checks + the
ucode semver gate. The install seam **`nrt_set_pool_eng_ucode`** is a silent, unauthenticated
override: any process that calls it before `nrt_init` substitutes the Pool Q7 ucode. The one real
fabric gate is the SoC NSM (Network-Security Monitor) AXI-integrity watchdog; the perimeter is
*firmware-armed*, not reset-default-secure, and **fails stop**. `[HIGH/OBSERVED]`

> **QUIRK — the boot handshake is a sentinel CAS, not an attestation.** The Q7 cold-boots, writes
> the ready sentinel **`0x6099CB34`** to `DRAM[0]` (the device `.globstruct` literally begins
> `34 cb 99 60`), and the host CAS-claims it with **`0x502B2DA1`**. That is the entire ownership
> protocol — no key exchange. The two constants are the firmware-image-load anchor for this face.

**Deep page:** [SEC-Lane Synthesis (boot → attest → fault)](../control/security/security-synthesis.md).

---

## Face 7 — The Evidence

**Role.** Seen as a *reference*, the one core is only as trustworthy as the basis behind every
claim. This face is the meta-face: the execution-validated evidence that makes the other six
byte-grounded rather than asserted. The strongest static fact in the whole corpus lives here.

**The anchor.** The shipped value oracle `libfiss-base.so` carries **864** `module__xdref_*` value
leaves (`nm libfiss-base.so | rg -c module__xdref_` = **864**, exactly) — the per-element value
function of every GPSIMD value opcode, **callable in-process with no license** via `ctypes` `dlopen`
of the shipped host binary. Running a leaf on a known input and diffing it against a reference model
is what makes a value claim *proven-by-execution* — ~95% of value-bearing leaves carry such a
certificate across ~2.09M comparisons, with **zero** firmware value bugs found. The encoding side is
the canonical `libisa-core.so` (1,534 shipped mnemonics, a certified non-overlapping cover); the
timing side is `libcas-core.so` (license-gated past retirement — the `AUTH::check_iss_licenses`
wall). `[HIGH/OBSERVED]`

> **NOTE — the one wall that is real.** `libcas-core` loads and surfaces fine, but instruction
> *retirement* hits a FlexNet license gate ("Unable to get license") — so cycles, the fault
> machine, and single-step are `closable-with-license`, while the **value** lane (Face 7's anchor)
> runs free. The corpus is a complete, execution-validated reference for **values**; **cycles** are
> bounded by that one named, closable wall.

**Deep page:** [ISS Oracle Synthesis](../iss/iss-oracle-synthesis.md) and
[The Per-Family Pass/Fail Capstone](../validation/capstone-matrix.md).

---

## Holding all seven at once

The faces are not a taxonomy to memorize — they are **seven dispatch lenses on one
`kernel_info_table`**. Trace any GPSIMD operation and you cross them in order:

- you start at **the core** (Face 1) — one `ncore2gp` FLIX decoder;
- the compiler routes your op to **the POOL engine** (Face 2) — `0x41` to POOL not DVE, never to
  PSUM;
- if it is hand-authored it rides **the custom-op lane** (Face 3) — `0xF0` → `callx8 funcVA`;
- if it is a reduction it becomes **a collective** (Face 4) — `0xC8` trigger, `0xBF` device hop,
  CCE reduce in the SDMA fabric;
- which device image runs is **the generation** (Face 5) — `coretype` → `*_libs` → EXTISA blob;
- it is admitted with **no signature** (Face 6) — unkeyed hash, `0x6099CB34`/`0x502B2DA1`
  handshake;
- and its per-lane value is **proven by execution** (Face 7) — a live `module__xdref_*` leaf.

One core. Seven anchors. Every face above resolves to a symbol, an opcode byte, or a device string
in a shipped binary, reproducible under `nm`/`rg`/`objdump`. That reproducibility
*is* the thesis: there is one machine here, and the seven faces are how a reimplementer reads it.

---

## See also

- [What GPSIMD Is — the one-screen map](what-gpsimd-is.md) — the orientation a reimplementer reads first.
- [Keystone Facts Reimplementers Get Wrong](keystone-facts.md) — the PSUM wall, the not-the-float-hot-path fact, the three-cores rule.
- [The Gen-Invariance Thesis](gen-invariance.md) — Face 5 developed into the formal R(Q7) statement.
- [The Reimplementation Verdict & Open-Questions Map](verdict-and-open-questions.md) — Face 7 developed into the named-wall ledger.
- [Master Glossary](../glossary.md) — the canonical anchor for every term used above.
