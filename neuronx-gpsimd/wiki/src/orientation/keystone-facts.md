# Keystone Facts Reimplementers Get Wrong

This is the short list of traps — the facts that *look* one way on a first plausible reading
and are provably another under a direct re-read of the shipped binaries. Each is the kind of
mistake that survives a first guess, gets encoded into a reimplementation, and then costs a
debugging pass when the silicon disagrees. If you read only one orientation page before
opening the ISA, read this one.

Each entry is the same shape: **the naive assumption** a confident reader brings, **the binary
truth** that overrides it, **the one-line evidence anchor** you re-run if you doubt it, and
**why it matters** for a reimplementation. Every fact is tagged `[CONF / PROV]` per
[The Confidence & Walls Model](../reference/confidence-model.md).

This page is the *distillation*. It is deliberately **not** the full register — the long tail of
single-number refinements and per-row affected-page lists is omitted here. What follows is the
subset of corrections a reimplementer meets first and falls into hardest.

> **How to use this page.** When you are about to assert something that *feels* like a settled
> constant — what GPSIMD computes, how many instruction lengths the FLIX decoder has, which
> codename owns which generation, how to read a `.data` struct — find it below first. If it is
> not here, check the full ledger, then the binary. The corrections below are precisely the
> ones that look settled and are not.

---

## The identity traps — what GPSIMD *is*

### K1 — GPSIMD is a stock Cadence Vision-Q7 DSP, not a bespoke AWS ISA

**Naive assumption.** "GPSIMD" is an Annapurna/AWS-designed accelerator with its own
instruction set, to be reverse-engineered opcode by opcode from scratch.

**Binary truth.** GPSIMD is **one frozen configuration of the off-the-shelf Cadence Tensilica
Vision-Q7 NX DSP** — config name `Xm_ncore2gp`, microarchitecture `Cairo`, arch `Xtensa24`
(XEA3) — instantiated eight times as the per-NeuronCore POOL cluster. The whole ISA is
Tensilica's published TIE/FLIX vector ISA, and Cadence's own `xtensa-elf-objdump` (driven with
`XTENSA_CORE=ncore2gp`) disassembles every generation's device image correctly. `[HIGH/OBSERVED]`

**Evidence.** `ConfigName = Xm_ncore2gp`, `uarchName = Cairo`, `arch = Xtensa24` read verbatim
from the shipped `ncore2gp-params`; the sole registered core in `XtensaTools/bin/xtensa-elf-objdump`.

**Why it matters.** A reimplementer does not invent an ISA — they target the Vision-Q7 Cairo
config. The Cadence TIE database, the `core-isa.h`, and the shipped disassembler are the
specification; building "from scratch" re-derives a published ISA badly.

### K2 — GPSIMD is *not* the float hot-path, and it cannot reach PSUM at all

**Naive assumption.** A "GPSIMD kernel" (RmsNorm, Softmax, MoE) runs its float math on the Q7
cores, so GPSIMD is the floating-point compute engine.

**Binary truth.** The marketing "GPSIMD kernel" is a **multi-engine micro-schedule** whose float
math runs on the Vector / Scalar / Tensor engines — *not* on the Q7 cluster. The literal Q7
GPSIMD engine owns exactly three narrow lanes: the native int32/uint32 add/sub/mul datapath
(`0x41`), the gather/scatter/custom-op lane (rides `0xF0 EXTENDED_INST`), and the SB2SB
collective hop (`0xBF`). And it **cannot address PSUM**: the PE-private accumulator is simply
not in the Q7 address space, so the compiler routes an int op to GpSimd but **falls back to the
Vector engine if any operand lives in PSUM**. `[HIGH/OBSERVED]`

**Evidence.** The compiler keystone string "Since GpSimd Engine cannot access PSUM…" + the
Vector-engine fallback in the `SundaISel` routing; SBUF (`STATE_BUF`) at SoC `0x2000000000` is
the only on-chip memory Q7 reaches (via an AXI aperture), PSUM is not.

**Why it matters.** Model GPSIMD as **SBUF/HBM-only, integer-and-movement**. A reimplementation
that targets float throughput on the Q7 cores, or that lets a kernel address PSUM from GPSIMD,
contradicts the actual datapath and the compiler's own placement rule.

### K3 — there are *three* distinct cores in the stack; do not conflate them

**Naive assumption.** "The GPSIMD firmware core" is one thing — the DSP that runs custom ops and
collectives alike.

**Binary truth.** Three different cores with three different ISAs sit in the stack: **Q7** = the
Vision-Q7 POOL/DVE DSP (FLIX/VLIW — this wiki's subject); **NCFW** = a *separate* scalar
**Xtensa-LX** management core that orchestrates collectives; **TOP_SP** = the NX sequencer that
walks the `cc_op` collective program. `[HIGH/OBSERVED]`

**Evidence.** `libncfw.so` carries the scalar-LX `v{2,3,4,4_plus}_ncfw_{iram,dram}_bin` blobs;
`TOP_SP_0_TPB_SP` sits at SoC `0x8280200000`; the Q7 EXTISA blobs are FLIX ELF32-Xtensa
(`e_machine = 94`). Three core families, three image families.

**Why it matters.** This is also a tooling trap (see K11): the FLIX decoder applied to an NCFW
LX image yields a spurious "~26–28% FLIX" artifact. Pick the right ISA per core, or every NCFW
decode is wrong.

---

## The FLIX-encoding traps

### K4 — FLIX has 7 length-class outcomes that collapse to *4* distinct byte-sizes — two levels, not one

**Naive assumption.** "FLIX is 14 formats / 46 slots / **7 lengths**," read as if **7** were the
number of distinct instruction byte-lengths.

**Binary truth.** Two different levels are being flattened onto one "lengths" axis. The runtime
`length_decoder` produces **7 length-class outcomes** (the `{2, 3, 16}` direct lengths, the
`op0==0xF` 8-vs-16 split keyed on byte 3, and the illegal `−1`). Those outcomes resolve to
exactly **4 distinct positive instruction byte-sizes `{2, 3, 8, 16}`**. State it as
**"7 length-class outcomes → 4 byte-sizes `{2,3,8,16}`"**, never either number alone as "the
number of lengths." `[HIGH/OBSERVED]`

**Evidence.** Verified this pass straight from the shipped Cadence config header `tie.h`:
`XCHAL_OP0_FORMAT_LENGTHS = 3,3,3,3,3,3,3,3,2,2,2,2,2,2,16,8` — sixteen entries whose *set* of
values is `{2, 3, 8, 16}`, four distinct sizes. The 7-outcome figure comes from the runtime
256-entry `length_table` (value census `{−1:2, 2:96, 3:128, 8:8, 16:22}`), validated 167/167
against the device objdump.

**Why it matters.** When you write a length-resync sweep, the *advance* table has **4** byte-size
outcomes — but the decoder genuinely has 7 length-classes, so you must keep the `op0==0xF`
byte-3 branch. A sweep that hard-codes seven distinct byte-lengths has a dead arm and will mask a
real desync; one that deletes the byte-3 split mis-frames every wide-vs-narrow bundle.

### K5 — `num_formats=14`, `num_slots=46` — read these from the binary, not assumed round numbers

**Naive assumption.** The format and slot counts are approximate / vendor-marketing figures.

**Binary truth.** They are exact and byte-read: the Cairo config exposes **14 instruction
formats** (`num_formats` → `mov $0xe,%eax`) and **46 total slots** (`num_slots` →
`mov $0x2e,%eax`), over **8 register files** (`num_regfiles` → `mov $0x8,%eax`).
`[HIGH/OBSERVED]`

**Evidence.** Disassembled this pass from `libisa-core.so`: `num_formats @ 0x3b65e0` = `0xe`,
`num_slots @ 0x3b6510` = `0x2e`, `num_regfiles @ 0x3b5c20` = `0x8`.

**Why it matters.** These three integers parameterize the entire decoder — the format table, the
slot partition, the 8-file register model. A reimplementation seeds its decode tables from these
exact counts; a guessed "16 formats" or "32 slots" produces a decoder that desyncs on the first
wide bundle.

---

## The generation traps

### K6 — `coretype` is OBSERVED; `arch_id` is INFERRED (= `coretype − 1`)

**Naive assumption.** Both the `coretype` and the `arch_id` of each generation are firmware
bytes read straight from a table.

**Binary truth.** The **coretype** byte is OBSERVED for all five generations — `{6, 13, 21, 29,
37}` for SUNDA / CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK, read directly from the
`nrtucode_get_ext_isa_internal` switch. The **`arch_id`** column (`{0x05, 0x0c, 0x14, 0x1c,
0x24}`) is **not a literal table anywhere** — each value is `coretype − 1`, so the whole
`arch_id` axis is **INFERRED from a stride**. `[coretype HIGH/OBSERVED; arch_id HIGH-bounded/INFERRED]`

**Evidence.** The `case 6/13/21/29/37` switch + the `*_Q7_POOL_PERF_EXTISA_0_SO_get` accessors in
`libnrtucode_internal.so`; the only `arch_id`-keyed firmware (`libncfw_get_image`) compares
exactly `{0x05,0x0c,0x14,0x1c}` and routes anything `> 0x1c` to default — there is no literal
`arch_id` table to read.

**Why it matters.** Key codenames and image selection off the **coretype** switch, never off a
fabricated `arch_id` table. The distinction is load for v5 (next fact): conflating the two lets
you write a v5 `arch_id` byte that was never read.

### K7 — MAVERICK (v5) is header-OBSERVED only; `ct37` is real, `arch_id 36` is not byte-read

**Naive assumption.** MAVERICK is a fully-grounded fifth generation like the others, with a
shipped NCFW image and a read `arch_id`.

**Binary truth.** MAVERICK exists **only at the header/identity surface**. Its **coretype 37
(`ct37`) is OBSERVED** — the `nrtucode.h` enum ordinal *and* the two resolver `movabs`
immediates whose bit 37 is set. But there is **no shipped v5 NCFW image** (`libncfw_get_image`
tops out at MARIANA_PLUS), so the v5 **`arch_id = 36 (0x24)` is INFERRED** from the `coretype − 1`
stride, not read. Every v5 *interior* claim is flagged INFERRED. `[ct37 HIGH/OBSERVED; arch_id 36 INFERRED]`

**Evidence.** Verified this pass: `maverick_libs` and `MAVERICK_Q7_POOL_PERF_EXTISA_0_SO_get`
present in `libnrtucode_internal.so`; the `maverick` literal appears in the not-stripped
*internal* twin but **0×** in the shipped front `libnrtucode.so`. No `cmp $0x24` exists in
`libncfw`; zero maverick `ctx_log` symbols.

**Why it matters.** Publish v5 as **header-OBSERVED + bounded-INFERRED**, never as a byte-read
silicon part-binding. Do not fabricate a v5 collective firmware, a v5 `arch_id` byte, or a "Trn4"
product name (none is in the corpus). This is one of the two standing walls carried on every v5
claim wiki-wide.

### K8 — codename↔arch binding: `0x0c = CAYMAN/v3 = Trn2`, `0x14 = MARIANA/v4` (an early pass had v3/v4 swapped)

**Naive assumption.** Carry forward whatever codename text an early report attached to the v3/v4
rows — including the inverted "`0x0c → mariana`, `0x14 → cayman`" and the "cayman = Trn1/Inf2"
conflation.

**Binary truth.** The bindings are **`0x05 = SUNDA/v2` (Trn1+Inf2), `0x0c = CAYMAN/v3` (Trn2),
`0x14 = MARIANA/v4`, `0x1c = MARIANA_PLUS/v4+`**. The arch_id→`v#` numbers were always right; an
early pass inverted only the *codename text* on the v3/v4 rows and mis-attached *cayman* to the
v2 Trn1/Inf2 row. `[HIGH/OBSERVED]`

**Evidence.** The `libncfw get_image` selector loads `v3_ncfw_*` at `cmpl $0xc` and `v4_ncfw_*`
at `cmpl $0x14`; `topo_neuron_cayman.o` builds **Trn2** topologies while `topo_neuron_sunda.o`
builds the **Trn1+Inf2** set.

**Why it matters.** Every structural count is arch_id-keyed and therefore unaffected — but the
*name* you attach to a count flips. A synthesis keyed on the swapped text inverts CAYMAN and
MARIANA across the whole guide. Key codenames off the `get_image` dispatch, never off stale row
text.

### K9 — TONGA (v1) is an outlier, *not* a sixth GPSIMD generation

**Naive assumption.** TONGA is the v1 entry in the same `coretype = arch_id + 1` family, to be
documented like SUNDA-through-MAVERICK.

**Binary truth.** TONGA (legacy NC-v1, the "L" family, Inf1) **predates the unified
`NEURON_ISA_TPB_OPCODE` namespace**. It has no `coretype`, no `arch_id`, no NCFW image, no EXTISA
blob, and only a register-block ISA (a 1-byte opcode *field*, no enumerated roster). Its dtype
enum is a *different name family* (`TONGA_ISA_TPB_DTYPE_*` vs `NEURON_*`). `[HIGH/OBSERVED]`

**Evidence.** `strings | rg -i tonga` on `libnrtucode_internal.so` returns **zero**; the
`TONGA_ISA_TPB_DTYPE_*` enum lives in a separate `aws_tonga_isa_tpb_common.h`.

**Why it matters.** The gen-invariant core thesis R(Q7) covers exactly **v2..v5**. Treating TONGA
as a sixth row of the same family imports a pre-unified register ISA into a roster that does not
contain it.

---

## The datapath / opcode traps

### K10 — SortMerge is a phantom: named in a dead comment, never a shipped opcode

**Naive assumption.** "SortMerge" is a real hardware instruction at opcode `0x97`, to be
documented as a datapath.

**Binary truth.** There is **no SortMerge HW opcode.** The byte `0x97` is **not** an opcode at
all — it is `NEURON_ISA_TPB_UPDATE_MODE_SEM_SUB_REG_COMPLETE` (an update-mode field), and
"SortMerge" survives only as a **dead `// SortMerge wip 0x97` comment** on the *adjacent*
`0x98 = TENSOR_SCALAR_SELECT` line. Plain `SORT` (`0x96`) is real and decoded; cross-partition
merge is host-side / future work. `[HIGH/OBSERVED — negative]`

**Evidence.** Verified this pass in `aws_neuron_isa_tpb_common.h` (cayman/mariana/maverick):
`NEURON_ISA_TPB_OPCODE_SORT = 0x96` and
`NEURON_ISA_TPB_OPCODE_TENSOR_SCALAR_SELECT = 0x98, // SortMerge wip 0x97`.

**Why it matters.** A SortMerge opcode body is a *fabricated* datapath. Reject any page that
documents one; the only honest statement is "named-but-never-shipped."

### K11 — the PROF_CAM is a per-engine instruction profiler, not the activation lookup table

**Naive assumption.** The 47-record `PROF_CAM` holds the activation opcodes and *is* the
transcendental piecewise lookup — so `0x30` Exponential "routes through the PWL via PROF_CAM."

**Binary truth.** `PROF_CAM`/`PROF_TABLE` is a **generic, cross-engine hardware-decode instruction
*profiler* CAM** present on every NX engine (a 16-byte `{opcode, mask, enable, rsvd}` record).
The activation lookup is a **separate, ACT-only piecewise-*cubic* (PWP)** datapath — each bucket
is a degree-≤3 polynomial `{d0,d1,d2,d3,x0}`, not a linear `{intercept,slope,breakpoint}`.
`[HIGH/OBSERVED]`

**Evidence.** All four CAYMAN per-engine PROF_CAM blobs (ACT/DVE/PE/POOL) are byte-identical
(sha `8fd7e422`) — a generic profiler, not 47 activation entries; a profiler that can *arm*
`0x30` for profiling is plainly not the table that *computes* `0x30`. The `tpb_activation_entries.h`
bucket struct carries the `d2`/`d3` cubic terms physically.

**Why it matters.** Do not describe PROF_CAM as the activation lookup, and do not model the
activation table as linear-PWL — the cubic terms exist. (The per-function coefficients are
host-supplied and never in the image — state the format, never invent coefficients.)

### K12 — the NKI gather split is 2:1 and pinned, not one ambiguous row

**Naive assumption.** "gather / IndirectCopy → `0x68` / `0xe7`" — a combined row that does not say
which NKI name maps to which opcode.

**Binary truth.** The mapping is **2:1 and exact**:
`nki.isa.local_gather → emit_indirect_copy → INDIRECT_COPY 0xe7` (an 8-core / 16-partition SW
per-index loop) and `nki.isa.nc_n_gather → emit_gather → GATHER 0x68` (a within-partition flat
gather). `[HIGH/OBSERVED]`

**Evidence.** `GATHER 0x68` and `INDIRECT_COPY 0xe7` are distinct opcodes in the maverick enum
(`// Y`); the routing is the verbatim `isa.py` comment in the NKI frontend.

**Why it matters.** A reimplementer wiring the NKI frontend to opcodes must split these — they
are genuinely different datapaths (cross-partition indirect copy vs flat gather), not one op with
two names.

---

## The methodology / tooling traps

### K13 — the value oracle is 864 leaves, callable in-process with no license

**Naive assumption.** Value semantics must be *reasoned out* from decoded opcodes — the strongest
fact available is a careful static derivation.

**Binary truth.** The shipped `libfiss-base.so` carries **864 `module__xdref_` value leaves** —
the per-element value function of every GPSIMD value opcode — and they are **callable in-process
via ctypes with no license**. Running a leaf live on a sweep of inputs and diffing against a
reference model is OBSERVED-by-execution: the binary itself is the arbiter, the strongest static
fact short of silicon. `[HIGH/OBSERVED]`

**Evidence.** Verified this pass: `nm libfiss-base.so | rg -c module__xdref_` = **864**. ~95% of
value-bearing leaves carry a differential certificate across ~2.09M comparisons, zero firmware
value bugs found.

**Why it matters.** This is the single highest-leverage technique in the whole effort — do not
hand-derive a value semantics you can *execute*. (The companion `libcas-core.so` cycle oracle is
present but its retirement path hits `AUTH::check_iss_licenses` — cycles/faults are
license-gated, the value lane is free.)

### K14 — the `.data` VMA↔file-offset delta is per-binary; `.text`/`.rodata` are 1:1, `.data` is not

**Naive assumption.** "VMA equals file-offset" (or "add a fixed `0x400000`") holds for every
section of every binary, so you can `xxd` a `.data` struct at its file offset directly.

**Binary truth.** Only **`.text` and `.rodata` have VMA == file-offset.** The `.data` (and
`.data.rel.ro`) delta is **per-binary and must be measured with `readelf -SW`** before reading a
`.data`-resident struct. It is *not* a constant `0x400000` — that value appears in none of the
corpus binaries. `[HIGH/OBSERVED]`

**Evidence.** Verified this pass on `libisa-core.so`: `.text`/`.rodata` delta `0`, but `.data`
Addr `0x764040` / Off `0x564040` → delta **`0x200000`**. Other families differ
(`libnrtucode_internal.so` `.data` delta `0x3000`).

**Why it matters.** Over-generalizing the delta lands every `.data` struct read on the wrong
bytes — a spurious "wrong struct" finding that has already happened. Always `readelf -SW` the
*specific* file first.

### K15 — DWARF lives in the host `libnrt.so` (a sibling corpus), not in the device config libs

**Naive assumption.** The `ncore2gp` config libraries (`libisa-core.so`, `libfiss-base.so`) carry
debug info, so struct field offsets resolve from them.

**Binary truth.** The config libs carry a **full `.symtab`** (use `nm` — e.g. 19,720 `FUNC`
symbols in `libisa-core.so`, so symbol-keyed reads work) but **zero debug sections**. The DWARF
that resolves struct field offsets lives only in the **host `libnrt.so`** — which is **not in
this gpsimd checkout** (it ships in the sibling `neuronx-runtime` corpus), so host-runtime
DWARF/struct facts here are `[CARRIED]`. `[HIGH/OBSERVED]`

**Evidence.** `readelf -S <config lib> | rg debug` → empty; `nm` returns full names. `libnrt.so`,
`libncfw.so`, and `libnrtucode_extisa.so` are absent as files here (their device payload rides
embedded inside `libnrtucode_internal.so`).

**Why it matters.** Name a function from the config lib's symtab, but reach for DWARF only in the
host runtime library — and tag anything carried across that checkout boundary `CARRIED`. Don't
go hunting for `.debug_info` in a config lib that has none.

### K16 — counts come from `nm | rg -c` on the binary, not from grep over a decompile

**Naive assumption.** A symbol-hit count grepped from a large decompile output is the symbol
count.

**Binary truth.** Decompile-grepped figures are **inflated** (a symbol appears at every call site
and reference, not once). Ground every count claim in **`nm <binary> | rg -c <pattern>`** against
the actual symbol table. `[HIGH/OBSERVED — methodology]`

**Evidence.** This pass re-grounded two counts directly: the **864** xdref leaves (`nm
libfiss-base.so | rg -c`), and the **`maverick`** literal census — at the **bare-lowercase**
scope (`rg -c 'maverick'`) the binary shows **2** occurrences in `libnrtucode_internal.so` and
**0** in the front `libnrtucode.so`. (This is the *lowercase-literal* count, not the
case-insensitive line count: `rg -ci maverick` = **189** = 187 `MAVERICK` + 2 `maverick`, which
the [glossary](../glossary.md) and [cross-walk](../reference/codename-crosswalk.md)
cite — same binary, different grep scope, not a contradiction.) The point stands either way: an
early prose figure had claimed a much larger *literal* count.

**Why it matters.** Encoding an inflated count as a hard requirement (entry-table sizes,
opcode-roster totals) propagates a wrong constant. Re-run `nm | rg -c` before you publish a
count.

### K17 — the empty `MODULE_SCHEDULE` matrices mean the 1+1 co-issue ceiling is the *sound* bound

**Naive assumption.** The pipeline-timing XML contains per-port reservation matrices you can read
to model fine scheduling.

**Binary truth.** All **1994/1994 `<MODULE_SCHEDULE>` reservation matrices are structurally empty**
in the shipped file. What *is* recovered is the **1+1 FLIX co-issue ceiling** (from the
1564-record `INSTR_SCHEDULE` table); only the fine per-port single-issue reservation *below* that
ceiling is unrecoverable — the matrix bodies are simply not present. `[ceiling HIGH/OBSERVED;
per-port LOW]`

**Evidence.** The XML dump shows empty `<MODULE_SCHEDULE>` bodies; the class-level ceiling comes
from `INSTR_SCHEDULE`.

**Why it matters.** This is a *fundamental* wall (no read of this XML produces bodies it lacks) —
but the bound is **sound**: the FLIX-slot + per-format mul-capable-slot model is the correct
substitute for a reimplementer's scheduler. Do not fabricate a per-port matrix; use the 1+1
ceiling.

### K18 — the device load path has *no* signing key, ever

**Naive assumption.** Loading a Q7 image is gated by a cryptographic signature check, as a modern
accelerator would have.

**Binary truth.** The GPSIMD device load path contains **no signature verification anywhere.**
Admission is an **unkeyed** integrity hash + structural checks (ELF / reloc / core-count) + a
ucode semver gate. The trust root is the **host OS / process boundary**, not a hardware root of
trust; the install seam `nrt_set_pool_eng_ucode` is a silent, unauthenticated override. This is a
deliberate single-tenant trusted-compiler design. `[HIGH/OBSERVED]`

**Evidence.** No crypto/signature symbol on the device admission path; the install seam overrides
an image with only hash + structural + semver checks.

**Why it matters.** A reimplementation that adds a signing requirement (or assumes one exists)
mis-models the security boundary. The honest model is integrity + structure + version, host-trust
root — and the GPSIMD path touches no crypto / sqlite / codec / ffi at all.

---

## What this page omits

This is the orientation distillation — the ~18 traps a reimplementer hits first. It does **not**
carry the long tail of single-number refinements (a stride `148` not `149`, a struct field at
`@12` not `@16`), the carried stale-copy hazards, or the per-row affected-page lists. When a fact
below *feels* like a settled constant, check it here first, then the binary — these are exactly the
corrections that survived a first plausible reading and a second look.

---

## See also

- [The Confidence & Walls Model](../reference/confidence-model.md) — what `[HIGH/OBSERVED]`,
  `[INFERRED]`, `[CARRIED]`, and "wall" mean; the `arch_id 36` / `ct37` walls in full.
- [What GPSIMD Is — the one-screen map](what-gpsimd-is.md) — the orientation these traps qualify.
- [The Seven Faces of the One Machine](seven-faces.md) — the one-core / many-views model behind
  K1–K3.
- [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) — the authoritative
  table behind K6–K9.
- [FLIX Bundle-Decoding Methodology](../reference/flix-decoding.md) — the 7-outcome → 4-byte-size
  decoder of K4–K5.
