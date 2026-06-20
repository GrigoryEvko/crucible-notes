# The Reimplementation Checklist

> *This is the **final capstone of the whole book** — the actionable, work-through-it checklist for
> the one reader the guide is written for: a senior C++/LLVM engineer who wants to **build a
> Vision-Q7-compatible GPSIMD engine**. Everywhere else the guide decodes, names, or executes one
> thing and proves it. This page sequences all of it into a **build order** — six stages, each a
> numbered set of `- [ ]` steps — and for every step gives **what to build · the wiki page(s) that
> specify it · the validation that proves it done · the walls to expect.** It is the "where do I
> start / am I done" page: it opens with the start-here entry point and closes with the
> done-criteria (the headline coverage fractions) and the honest residual (the v5 walls).*

This page is the procedural twin of three orientation capstones. The
[Reimplementation Verdict & Open-Questions Map](../orientation/verdict-and-open-questions.md)
answers *can it be rebuilt, and at what confidence?* The
[Gen-Invariance Thesis](../orientation/gen-invariance.md) answers *how do I cover all five
generations from one machine?* The two worked traces —
[A Custom Op, End to End](../orientation/customop-end-to-end.md) and
[A Collective, End to End](../orientation/collective-end-to-end.md) — answer *what does one
request actually do, hop by hop?* This page answers the remaining question: **in what order do I
build it, and how do I know each piece is correct?** The completeness accounting it closes against
is the [Coverage Ledger](coverage-ledger.md); the residuals it flags are the
[Open-Questions Register](open-questions-register.md).

Every step carries a `[CONF/PROV]` tag per [The Confidence & Walls Model](../reference/confidence-model.md)
**where it rests on an INFERRED or CARRIED fact** — so a reimplementer knows exactly which steps
are byte-grounded hard requirements (`OBSERVED`) and which are bounded extrapolations to confirm on
hardware (`INFERRED` / `CARRIED`). Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a
reimplementation trap), **NOTE** (orienting context), **CORRECTION** (overturns a naive reading).

---

## Where do I start

> **Start here.** Build the **gen-invariant core first**, validate it against the **shipped value
> oracle**, and only then move outward to microarch, firmware, host, collectives, and the
> per-generation parameter vector. The core is the part that is *identical on every generation*
> (the [Gen-Invariance Thesis §3](../orientation/gen-invariance.md) equation:
> **`A + parameterize(B)` = all five generations**); everything else either rides on it or
> parameterizes it. Do **not** start at the firmware opcodes or the host runtime — both assume a
> working decoder and a working value model beneath them.

The reason this order is forced, not a preference: the value semantics, the encoding cover, and the
formal model are **core-IP properties** of the Vision-Q7 "Cairo" (`ncore2gp`) config — they do not
change across SUNDA / CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK. The per-generation deltas are an
**additive parameter vector** (opcode/dtype/ALU counts, coretype, EXTISA set, transport family)
sitting *around* that core. So a reimplementer who builds the core once and parameterizes the
vector has, by construction, covered the whole line — but only if the core is built and validated
*first*. `[HIGH/INFERRED — the gen-invariance synthesis; sound over A1–A8 / B1–B10 OBSERVED facts]`

The single highest-leverage fact for the whole build: the shipped value simulator
(`libfiss-base.so`) is **callable in-process via ctypes with no license** — its **864**
`module__xdref_` value leaves are the per-element function of every GPSIMD value opcode, and you
can run any leaf live on a sweep of inputs and diff against your implementation. **The binary
itself is your test oracle.** Do not hand-derive a value semantics you can execute.
`[HIGH/OBSERVED]` See [Keystone Fact K13](../orientation/keystone-facts.md) and
[The ISS as Executable Oracle](../iss/iss-oracle-synthesis.md).

The eight pages you will lean on hardest are listed once here so you can open them before Stage 1:
[FLIX Encoding](../isa/core/flix-encoding.md), [The Eight Register Files](../isa/core/register-files.md),
[the ISA Reference (B01–B30)](../isa/ref/template-and-partition.md),
[the Formal ISA Model](../isa/semantics/formal-isa-model.md),
[the ISS Oracle](../iss/iss-oracle-synthesis.md),
[the VAL Capstone](../validation/capstone-matrix.md),
[the Gen-Invariance Thesis](../orientation/gen-invariance.md), and
[the Coverage Ledger](coverage-ledger.md).

---

## Stage 1 — The core (the part you build once, identical on every generation)

This is the foundation and the bulk of the value. Build it against **CAYMAN (v3)**, the
byte-grounded reference SoC; it ports to every other generation by Stage 6's parameter vector. The
four sub-deliverables — decoder, register files, ISA reference, formal semantics — and the **single
validation gate that proves them all bit-exact**: the 864-leaf value oracle.

### 1.1 The FLIX decoder

- [ ] **Build a FLIX/VLIW bundle decoder** reproducing the device `xtensa-elf-objdump`
  byte-for-byte. Seed it from the three exact counts read out of `libisa-core.so`: **14 instruction
  formats** (`num_formats` → `mov $0xe,%eax` @ `0x3b65e0`), **46 total slots** (`num_slots` →
  `mov $0x2e,%eax` @ `0x3b6510`), over **8 register files** (`num_regfiles` → `mov $0x8,%eax` @
  `0x3b5c20`). The length side is a **256-entry `length_table`** (@ `0x3d4100`) producing **7
  length-class outcomes** that resolve to exactly **4 distinct positive byte-sizes `{2, 3, 8, 16}`**.
  · **Pages:** [FLIX Encoding](../isa/core/flix-encoding.md) ·
  [FLIX Decoding Methodology](../reference/flix-decoding.md). `[HIGH/OBSERVED]`
- [ ] **Validate** the decoder by round-tripping it against the device-native objdump over hundreds
  of thousands of bundles — the certified cover round-trips with **zero disagreements**; the
  `length_table` census `{−1:2, 2:96, 3:128, 8:8, 16:22}` validates **167/167** against the device
  objdump. · **Page:** [ISA Coverage Tally](../isa/core/coverage-tally.md).

> **GOTCHA — "7 lengths" is two levels flattened onto one axis.** State the length model as
> **"7 length-class outcomes → 4 byte-sizes `{2,3,8,16}`"**, never either number alone. The decoder
> genuinely has 7 outcomes (the `{2,3,16}` directs, the `op0==0xF` 8-vs-16 split keyed on byte 3,
> and the illegal `−1`); the *advance* table has 4 byte-sizes. A resync sweep that hard-codes seven
> distinct byte-lengths has a dead arm and masks a real desync; one that drops the byte-3 split
> mis-frames every wide-vs-narrow bundle. See [Keystone K4/K5](../orientation/keystone-facts.md).

### 1.2 The eight register files

- [ ] **Model the 8 register files** the decoder's per-slot operands name:
  `{AR, BR, vec, vbool, valign, wvec, b32_pr, gvr}` — 2 core/scalar + 6 Vision-Q7 SIMD coprocessor
  files — read from the `regfiles` table @ `0x74a800` (stride 56) in `libisa-core.so`. These are the
  ABI-relevant register block every decoded operand resolves into. · **Page:**
  [The Eight Register Files](../isa/core/register-files.md). `[HIGH/OBSERVED]`
- [ ] **Validate** each file's width/count/aliasing against the `libisa-core.so` regfile table and
  the per-slot operand-class bindings the decoder emits.

### 1.3 The ISA reference (the 1534-mnemonic roster)

- [ ] **Build the instruction roster + per-mnemonic decode model**: **1,534 / 1,534** shipped
  Vision-Q7 mnemonics with **12,569 / 12,569** placements, folded from the **1,607 / 12,642**
  pre-fold TIE-DB authoring superset (the `+73` fold = 24 `.W18` wide-branch macros + 6 virtualops +
  43 no-body pseudo). This is the canonical `libisa-core.so` encode/decode model — one host-side
  roster for **every** generation. · **Pages:** the 30-batch per-instruction reference
  [B01–B30 + the partition index](../isa/ref/template-and-partition.md) ·
  [ISA Coverage Tally](../isa/core/coverage-tally.md). `[HIGH/OBSERVED]`
- [ ] **Validate** with the certified-perfect, non-overlapping cover check: the 28-package census
  sums to 1534, and the device objdump/assembler round-trips byte-exact with **0 disagreements**.

> **GOTCHA — pair the totals correctly.** The only valid pairs are **`1534 ↔ 12569`** (shipped /
> runtime — the denominator for *anything you build*) and **`1607 ↔ 12642`** (pre-fold / authoring).
> Pairing `1534` with `12642` mixes the runtime fold with the authoring superset and manufactures a
> `±73` phantom. `[HIGH/OBSERVED]`

### 1.4 The formal semantics

- [ ] **Encode the bit-precise per-element value function** of every value opcode — the formal
  ISA-semantics model: the arithmetic the silicon computes, per element, per dtype. · **Page:**
  [The Complete Formal ISA-Semantics Model](../isa/semantics/formal-isa-model.md) ·
  [the semantics coverage ledger](../isa/semantics/coverage-ledger.md). `[HIGH/OBSERVED]`

### 1.5 The value oracle — the "am I bit-exact" gate

- [ ] **Stand up the shipped value oracle as your differential arbiter.** `dlopen`/`ctypes`
  `libfiss-base.so` in-process (no license needed), enumerate its **864** `module__xdref_` value
  leaves (`nm libfiss-base.so | rg -c module__xdref_` = **864**), and drive each leaf live on input
  sweeps, diffing against your Stage-1.4 implementation. · **Pages:**
  [The ISS as Executable Oracle](../iss/iss-oracle-synthesis.md) ·
  [the VAL Capstone — per-family pass/fail matrix](../validation/capstone-matrix.md). `[HIGH/OBSERVED]`
- [ ] **Validate — the bit-exact gate.** Reproduce the proven result: **~95%** of value-bearing
  leaves driven **LIVE** and reproduced bit-exact across **18 op families** / **~2.09M** in-process
  differential comparisons, **zero** firmware value bugs. fp16 classify swept exhaustively (all
  65,536 patterns). If your implementation diverges from a leaf, your implementation is wrong — the
  binary won every one of the ~2.09M comparisons. · **Page:**
  [the VAL Capstone](../validation/capstone-matrix.md). `[HIGH/OBSERVED — the 0-bugs;
  MED — the ~95% fraction]`

> **WALL — the one value-leaf residual (recipqli).** Exactly **3 of 864 leaves (~0.35%)** — the
> recipqli soft-float QLI refine leaves (`recipqli_..._32f_32f` + an fp64 pair) — cannot be driven
> *end-to-end* by a bare ctypes drive: the value-producing soft-float FMA routes through a host
> dispatch object (`call *0x38(%rax)`, `%rax` from the saved `xstate`), and NULL **or** zeroed
> `xstate` both **SIGSEGV** (fork-isolation-proven). Everything *under* the wall is proven — the
> 6-bit segment index, the four QLI coefficient LUTs (`base == ref`), the two-substage split, the
> device round-trip. This is a **driver gap, not a knowledge gap**. Close it with the **licensed
> full ISS** (single-stepping the leaf in cycle/turbo, reading the dest vreg via state
> introspection) or a **device round-trip / FW-42 driver** that populates the dispatch object.
> Registered as **Q1**. `[wall HIGH/OBSERVED·exec; end-to-end value of 3 leaves MED/CARRIED]`

> **WALL — FW-42 transcendental seeds: ship the ROM bytes, not the formula.** The seed tables
> (`RECIP_Data8` / `RSQRT_Data8` + the recipqli QLI LUTs) are **execution-validated truth** —
> read from `.rodata` *and* reproduced bit-exact by the live leaf over all 128 buckets × {fp16, fp32},
> 0 mismatches. **Copy those bytes and you are correct by construction.** What is *not* recoverable
> is the **literal source coefficients / the generator program** the firmware author started from
> (the closed-form derivation, the exact Newton/QLI iteration counts) — it lives in the
> out-of-carve FW-42 driver and is CARRIED. The narrowing is exact: 256/256 bytes
> execution-validated, 254/256 closed-form-explained, and **2** half-ULP boundary roundings
> (`RECIP_Data8[127] = 0x81`, `RSQRT_Data8` hi-`idx=13` = `0xa5`) where the recovered closed-form
> is one ULP off the ROM — **ship the ROM byte at those two indices, never the formula.**
> Registered as **Q12a**. `[table HIGH/OBSERVED·exec; lineage + 2 boundaries MED/CARRIED]`

> **NOTE — the FLIX-desync ceiling and the empty `MODULE_SCHEDULE`.** Two corpus-wide walls bound
> *narrative body-reading*, **not** the Stage-1 closures: (a) the shipped device disassembler
> desyncs on the 512-bit bundle stream (`IsaMaxInstructionSize = 32`), so per-*instruction* bodies
> inside a desynced span are MED — but **neither the encoding cover nor the value closure walks the
> desynced linear stream** (the cover is certified independently; the value lane *executes* the
> simulator), so this does not touch Stage 1's bit-exact result; (b) the `MODULE_SCHEDULE` matrices
> are `1994/1994` empty (a Stage-2 concern, below). See [Open-Questions §3.4](open-questions-register.md).

---

## Stage 2 — The microarchitecture (the cycle-approximate model)

With a correct *value* model, add the *timing* model. This is the cycle-approximate envelope a
reimplementer needs for a scheduler and a memory subsystem — complete except the one flagged
per-port cycle tier.

- [ ] **Build the co-issue model — the 1+1 FLIX ceiling.** The class-level co-issue bound is **1+1**
  (recovered from the 1564-record `INSTR_SCHEDULE` table): per bundle, at most one of each issuable
  class. Model the per-format mul-capable-slot composition. · **Page:**
  [FLIX Co-Issue Matrix](../uarch/co-issue-matrix.md). `[HIGH/OBSERVED]`
- [ ] **Build the pipeline timing model** — the cycle-level latency / read-stage / write-stage
  model: the 2-pipe stage stamps, FCR/FSR + single-round FMA, the reservation bodies (2149 issue /
  1746 stall / ~160k stage). · **Page:** [Pipeline Timing Model](../uarch/pipeline-timing.md).
  `[HIGH/OBSERVED; box-to-box datapath topology MED/INFERRED — RTL not in corpus]`
- [ ] **Build the LSU / local-memory model** — the data side: the **2-LSU split**, **no-PSUM**
  (the Q7 cluster cannot address PSUM at all), **no D-cache**, SBUF (`STATE_BUF`) at SoC
  `0x2000000000` as the only on-chip memory Q7 reaches via an AXI aperture. · **Page:**
  [Local-Memory / System-Bus / LSU Model](../uarch/lsu-memory.md). `[HIGH/OBSERVED]`
- [ ] **Build the atomics + memory-ordering model** — the memory-consistency model for the
  `ncore2gp` config. · **Page:** [Atomic + Memory-Ordering Model](../uarch/atomics-ordering.md).
  `[HIGH/OBSERVED]`
- [ ] **Validate** the timing tier against the OBSERVED stage stamps and reservation bodies; treat
  the box-to-box wiring as INFERRED (re-confirm on hardware).

> **WALL — the empty `MODULE_SCHEDULE` per-port matrices (fundamental).** The pipeline-timing XML
> ships `1994/1994` `<MODULE_SCHEDULE>` reservation matrices that are **structurally empty**. The
> **1+1 co-issue ceiling IS recovered**; only the *fine per-port single-issue reservation below it*
> is unrecoverable — the matrix bodies are simply not in the file. This is **fundamental** for the
> per-port claim (no read produces bodies the file lacks), but the bound is **sound**: use the
> FLIX-slot + per-format mul-capable-slot model under the 1+1 ceiling. **Do not fabricate a per-port
> matrix.** Registered as **Q12c**. `[empty HIGH/OBSERVED; per-port LOW; fundamental]`

> **GOTCHA — K2: GPSIMD is not the float hot-path, and it cannot reach PSUM.** A marketing "GPSIMD
> kernel" (RmsNorm, Softmax, MoE) is a **multi-engine micro-schedule** whose float math runs on the
> Vector/Scalar/Tensor engines — *not* on the Q7 cluster. The literal Q7 GPSIMD engine owns three
> narrow lanes: the native int32/uint32 add/sub/mul datapath (`0x41`), the gather/scatter/custom-op
> lane (rides `0xF0`), and the SB2SB collective hop (`0xBF`). Model GPSIMD as **SBUF/HBM-only,
> integer-and-movement**; a reimplementation that targets Q7 float throughput or lets a kernel reach
> PSUM contradicts the datapath and the compiler's own placement rule. `[HIGH/OBSERVED]`

---

## Stage 3 — The device firmware (the dispatch mechanism + the kernel catalog)

Now the v2–v4 firmware mechanism — the control-core dispatch, the opcode→kernel table, the DGE, and
the dtype model. Field-exact for the four shipped generations; v5 interiors are walled (Stage 6).

- [ ] **Build the SEQ dispatch front-end** — the control-core sequencer: an 18-handler SEQ
  intersection, with the `0xF0 EXTENDED_INST` NX→Q7 bridge that makes **POOL the sole dual-core
  engine** (only POOL has the `0xF0` escape). The `engine_idx` enum is
  `{PE 0, ACT 1, POOL 2, DVE 3, TPB_SP 4, TOP_SP 5}`. · **Page:**
  [SEQ Decode / Dispatch Hub](../firmware/seq/dispatch-hub.md). `[HIGH/OBSERVED]`
- [ ] **Build the kernel catalog** — the **140 real HW opcodes** (`172 union − 31 PSEUDO − 1
  INVALID = 140`), dispatched over the device image's `.kernel_info_table` (8-byte entries
  `{BE key (spec<<8 | opcode) | LE funcVA}`, linear-scanned). Per-gen rosters: 145/150/159/165. ·
  **Page:** [The Opcode Catalog Ledger (140 real opcodes)](../firmware/kernels/opcode-catalog-ledger.md).
  `[HIGH/OBSERVED roster; interiors MED]`
- [ ] **Build the DGE (Descriptor-Generation Engine) setup / context-init path** — the device-side
  DGE setup the v4+ fast-path rides. · **Page:** [DGE Setup + Context Init](../firmware/dge/dge-setup.md).
  `[HIGH/OBSERVED]`
- [ ] **Build the dtype model** — the unified datatype model across the GPSIMD pipeline (the dtype
  bracket, the 8 marshallable immediates, the dtype-match gate). · **Page:**
  [The Unified Datatype Model](../firmware/kernels/dtype-model.md). `[HIGH/OBSERVED]`
- [ ] **Validate** the dispatch by replaying the [custom-op worked trace](../orientation/customop-end-to-end.md)
  Stage 8–9 (`load_external_libraries_impl` binds the table; the POOL scan matches the packed
  `(spec<<8 | opcode)` key) and asserting the `NUM_POOL_CORES = 8` invariant (`total_cpus ∈ {1, 8}`).

> **WALL — the FW interiors are MED under the FLIX-desync ceiling.** The opcode **roster** and
> **membership** are byte-exact (HIGH); several per-opcode *interiors* — `0x45` Pool reduce,
> `0xBD`/`0xF1` DMA-transpose, the late MAVERICK ops — are interior-MED (opcode byte-exact, body
> inside a FLIX-desynced span). The FW lane self-reports **55.7% body-decoded** (78/140) + 7.9%
> planned. **SUNDA's POOL kernels live in an out-of-corpus EXTISA container** (`libnrtucode_extisa.so`,
> not in this checkout — its blobs ride embedded in `libnrtucode_internal.so` for the four modern
> gens only). And **SortMerge is a phantom** — `// SortMerge wip 0x97` is a dead comment (`0x97` is
> the update-mode field `UPDATE_MODE_SEM_SUB_REG_COMPLETE`, not an opcode); plain `SORT 0x96` is
> real. Do not fabricate a SortMerge body. `[roster HIGH/OBSERVED; interiors MED; SortMerge
> absence HIGH/OBSERVED — positive finding]`

> **WALL — the host-loaded PWP activation coefficients (Q6).** The piecewise-cubic (PWP) activation
> table *format* is byte-exact (each bucket is a degree-≤3 polynomial `{d0,d1,d2,d3,x0}`), but the
> per-function cubic **content** is **host-loaded** via the `0x23` table-load DMA — a per-model
> runtime payload, never resident in the firmware image. State the format; **never invent
> coefficients.** Close it with a captured `0x23` payload or the host PWP generator.
> `[format HIGH/OBSERVED; content runtime-only]`

---

## Stage 4 — The host stack (ABI · runtime load/dispatch · NEFF · prelinker)

The x86-64 host half: how a custom op is built, loaded, relocated, and dispatched to the device.
Build it against the [custom-op worked trace](../orientation/customop-end-to-end.md), which walks
all nine hops end to end — this stage is its byte-level reference.

- [ ] **Build the custom-op ABI** — the on-device contract: `Q7PtrType` (16-byte
  `{hbm_addr, neuron_translate_ctx()}`), the `at::Tensor` marshalling chain
  (`customop_setup`/`customop_next_tensor`/`customop_return_tensor`/`customop_cleanup`), the
  `ARG_TENSOR` descriptor (`byte_size = 48`), `get_cpu_id` = raw `rsr.prid`, the 8 marshallable
  dtype immediates, and the stack-switch launcher (`switch_stack_or_call_wrapper`, stack 4196). ·
  **Page:** [The Complete Custom-Op ABI](../abi/complete-customop-abi.md). `[HIGH/OBSERVED]`
- [ ] **Build the host runtime spine** — `NRT_2.0.0`: the load → install → execute → reap path
  (`nrt_load` / `nrt_set_pool_eng_ucode` / `nrt_execute`), `nrtucode_ll_create` (the host load *is*
  create — there is **no** `nrtucode_ll_load` symbol), the per-coretype switch `{6,13,21,29}`, and
  EXECUTE = **one** semaphore doorbell on a pre-staged `0x107A` POOL stream. · **Page:**
  [Runtime Synthesis](../runtime/runtime-synthesis.md). `[HIGH/OBSERVED host half]`
- [ ] **Build the NEFF format reader** — the 1024-B `neff_header_t`, the container / `pkg_version`
  gates, the device Xtensa-PI-ELF32 carve (`e_machine 0x5E`, `.rela.got`, `kernel_info_table` rows),
  and the 3-tier version/compat gates. · **Page:** [The NEFF Format Reference](../neff/format-reference.md).
  `[HIGH/OBSERVED v2–v4]`
- [ ] **Build the prelinker (UCPL).** The static `prelink` (@ `0x9b5d60` in
  `libnrtucode_internal.so`) runs **entirely host-side** (no device-resident prelink): validate +
  load the two `PT_LOAD` segments, apply **every** `R_XTENSA` relocation in scratch against the
  device base (symbol resolution is degenerate by contract — reject any reloc with nonzero symbol
  index), and emit the **0x20-byte UCPL header** (magic `"UCPL "` = qword `0x204c504355`). The
  result is a fully-relocated image with **zero** residual relocations; the device only DMA-pulls it
  and binds the table. · **Page:**
  [The Host Prelinker — UCPL / Segment Loader / R_XTENSA / Staging](../runtime/prelinker-ucpl.md).
  `[HIGH/OBSERVED]`
- [ ] **Validate** by replaying the [custom-op trace](../orientation/customop-end-to-end.md) Stage
  5–7: `nrtucode_ll_create` → `prelink` emits `[UCPL][code][data]` → `nrtucode_ll_get_load_sequence`
  emits the `0x1095` load records. Confirm the two distinct magics (`"UCPL "` = device image;
  `0x1095` = host→device load record). The internal twin accepts coretype 37 (MAVERICK); the shipped
  jump table does not — the single genuine divergence between the two binaries.

> **QUIRK — the host does all the relocation; the device only DMA-pulls a finished image and binds
> a table.** Every "where does X happen?" question reduces to *which side of the `0x1095` record am
> I on*. The mental model from the trace: build host-side, relocate host-side, stage a finished
> image, and the device just installs a `.kernel_info_table`. `[HIGH/OBSERVED]`

> **WALL — the device-side facts are not host-resident.** The on-core Q7 ISA, the
> `.kernel_info_table` dispatch, and customop consumption are **CARRIED** from the device `ncore2gp`
> carves — they are not in the host `libnrt.so`. The customer `_cpuN.so` are **never shipped** (they
> are built on the customer machine). The per-kernel SPMD channel-slicing formula (G2) lives in
> customer/firmware kernel images, not in `libneuroncustomop.a`. `[HIGH/OBSERVED host; device
> CARRIED]`

---

## Stage 5 — The collectives + control plane + security

The cross-core and platform layers: the collective lowering, the SoC memory map, and the security
model. Build the collective half against the
[collective worked trace](../orientation/collective-end-to-end.md).

- [ ] **Build the collective spine** — the 6-layer host→device lowering. A collective pseudo-op
  (`0xc7` / `0xc8` / `0xd9`) **never runs on hardware**: the host (`libnrt`) lowers it at load time
  into a **TOP_SP `cc_op` SPAD program** + **DMA `pring`s**; the **NCFW** scalar-LX management core
  walks the dispatch; the one real device hop is **`SB2SB 0xBF`** (POOL/Q7 iDMA, NC≥v3), and the
  reduce is the **SDMA CCE in-transfer compute** (`SDMA_CCETYPE`, byte-exact the HW `CCE_OP`
  encoding). The reducible dtype set is a constant **6** `{BF16, FP16, FP32R, FP8_E3/E4/E5}`. ·
  **Page:** [Collective Architecture Synthesis](../collectives/ops/architecture-synthesis.md).
  `[HIGH device · CARRIED host compose]`
- [ ] **Build the control-plane address model** — the unified SoC memory map: the CAYMAN flat map
  (34,858 nodes / 19,012 json bindings), the 3-coordinate conversion chain (SoC / Q7-local NX /
  json), and the gen-invariant offset set. · **Page:**
  [The Unified GPSIMD / Cayman SoC Memory Map](../control/address/unified-soc-memory-map.md).
  `[HIGH/OBSERVED CAYMAN]`
- [ ] **Build the security model — and do NOT add a signing key.** The 3-layer model (SoC-fabric /
  on-core XEA3-MPU / firmware-load integrity): admission is an **unkeyed** integrity hash +
  structural checks (ELF / reloc / core-count) + a ucode semver gate — **zero crypto-auth dynsyms,
  NEEDED=libc only, `XCHAL_HAVE_SECURE=0`**. The trust root is the **host OS / process boundary**,
  not a hardware root of trust; the install seam `nrt_set_pool_eng_ucode` is a **silent,
  unauthenticated override**. The dual remapper is amzn=fail-**CLOSED** / user=fail-**OPEN**. ·
  **Page:** [SEC-Lane Synthesis (boot → attest → fault)](../control/security/security-synthesis.md).
  `[HIGH/OBSERVED]`
- [ ] **Validate** the collective lowering by replaying the
  [collective trace](../orientation/collective-end-to-end.md) hops 1–14 (trigger → relocate →
  algorithm-select → compose → SPAD → pring → kick → NCFW dispatch → SB2SB+CCE → barrier).

> **QUIRK — K18: there is no signing key, ever.** A reimplementation that *adds* a signature
> requirement (or assumes one exists) mis-models the security boundary — this is a deliberate
> single-tenant trusted-compiler design. The honest model is integrity + structure + version,
> host-trust root; the GPSIMD path touches no crypto / sqlite / codec / ffi at all. `[HIGH/OBSERVED]`

> **WALL — the NCFW LX-core decode wall (O-1 / Q7).** The NCFW management core is a **scalar
> Xtensa-LX** core, *not* a Vision-Q7 FLIX core — and its device images ship **no** TIE/disassembler
> config. Decode its case-bodies with the **scalar-LX length rule** (`op0 ∈ {e,f}` → 3-byte), never
> the FLIX decoder (which manufactures a spurious "~26–28% FLIX" artifact). The dispatch **spine** is
> OBSERVED (the DRAM`+0xB0` 12-entry table, the `const16 0xB0; addx4; l32i.n` one-shot pattern); the
> per-algorithm **case bodies / on-core step schedule** are not instruction-decodable here. Close it
> with NCFW's own LX disassembler config (a corpus item absent here). `[spine HIGH/OBSERVED; dense
> interiors MED/OBSERVED]`

> **WALL — the host collective-compose binaries (Q11).** The device half (`SB2SB 0xBF`, the CCE
> reduce, the `0xC7`/`0xC8`/`0xD9` triggers) is **re-OBSERVED in-checkout**; the host
> **SELECT/COMPOSE/EMIT** machinery (`libnccom` `findPath`/`EdgeRemoteMLA`; host `libnrt`
> 2.31.24.0) is **CARRIED** — those binaries are not in this checkout. `[device HIGH/OBSERVED; host
> MED/CARRIED]`

---

## Stage 6 — Per-generation parameterization (the scaling axes) + the v5 walls

The final stage: specialize the one core you built (Stages 1–5, against CAYMAN) to any of the five
generations by feeding a **small, OBSERVED, additive parameter vector**. This is the
[Gen-Invariance Thesis](../orientation/gen-invariance.md) equation made actionable:
**`A + parameterize(B)` = all five generations.**

- [ ] **Feed the ten-entry scaling vector (Ledger B).** Per generation, parameterize:
  **B1** `struct2opcode`/`kernel_info` count (89/99/108/108/114) · **B2** DTYPE bracket
  (16/16/24/24/30) · **B3** ALU_OP count (33/60/64/64/65) · **B4** OPCODE enum (145/150/159/159/165)
  · **B5** coretype/arch_id (6·5 / 13·12 / 21·20 / 29·28 / **37·36\***) · **B6** EXTISA blob set
  (1/4/4/4/4) · **B7** SB2SB `0xBF` (ABSENT on SUNDA; present v3+) · **B8** engine folds (5 NX;
  MARIANA_PLUS +DGE fast-path; MAVERICK ACT→DVE) · **B9** transport family (SDMA v2–v4+; v5 re-IP) ·
  **B10** toolchain epoch (14.09/clang-10 for the four shipped; **15.05/clang-15** for MAVERICK). ·
  **Pages:** [Master Per-Generation Capability Matrix](../generations/master-capability-matrix.md) ·
  [The Codename ↔ NC-ver ↔ coretype ↔ arch_id Cross-Walk](codename-crosswalk-table.md). `[HIGH/OBSERVED for v2–v4]`
- [ ] **Validate** v2–v4 cells byte-by-byte against the per-gen `neuron_<gen>_arch_isa` headers and
  the `libnrtucode_internal.so` coretype switch — every count/sha/enum/selector is OBSERVED for the
  four shipped gens (13/13 spot-checks PASS in the GEN lane).

> **GOTCHA — B9 (transport) does not transfer; the additive model breaks at v5.** Every axis except
> B9 is **additive** (codes are reserved — a code never changes meaning; gens only *add*). B9 is a
> re-IP **swap**: v5 is DDMA/CDMA/UDMA + native UCIe + 3-die, *not* an extension of the SDMA family.
> Treat SUNDA..MARIANA_PLUS as one transport family and MAVERICK as a separate one. A Cayman-class
> d2d/SerDes/HBM fact is **not** a MAVERICK fact. `[HIGH/OBSERVED]`

> **CORRECTION — the `coretype` stride is irregular; only the `arch_id` relation is uniform.** Do
> **not** assert a flat `+8` `coretype` stride. The observed set is `{6, 13, 21, 29, 37}` — that is
> **`+7` then `+8`, `+8`, `+8`** (6→13 is +7). What *is* uniform is `arch_id = coretype − 1`
> (`{0x05, 0x0c, 0x14, 0x1c, 0x24}`), and it is that `−1` relation — not any `coretype` stride —
> that extends the v5 `arch_id` to 36. A reimplementer reading "+8 stride" off the `coretype` axis
> mis-derives the floor. `[HIGH/OBSERVED]`

### The v5 / MAVERICK walls — exactly what is byte-grounded vs header-OBSERVED-only

MAVERICK (v5) is **header-OBSERVED + bounded-INFERRED**, never byte-grounded in its interior. Flag
each of the four walls on every v5 claim; never fabricate across them. The hard fact all pages agree
on: **the v5 NCFW image is FILE-ABSENT.**

- [ ] **`arch_id 36 (0x24)` — INFERRED (doubly). Carry the `36*` asterisk.** It is **not** byte-read
  anywhere — there is **no `cmp $0x24`** in `libnrtucode_internal.so` and no v5 NCFW image. It rests
  on `arch_id = coretype − 1`, *and* a second cross-check fails (the `NX_TOPSP = arch_id` rule: enum
  slot 36 is a `MAVERICK_NX__REMOVED__` placeholder; the real `MAVERICK_NX_TOPSP` is at index 54).
  `[arch_id 36 MED/INFERRED·doubly]` **closable-with-corpus.**
- [ ] **`coretype 37 (ct37)` — OBSERVED.** Byte-read in the live `get_ext_isa` jump arm
  (`case 37 → maverick_libs` @ `0x9b9050`), the internal-twin gate (`cmp $0x25` = 37), the `movabs
  $0x2020202000` bit-37 bitmask, and 62 `MAVERICK_*_get` accessors. `[ct37 HIGH/OBSERVED in the
  IMG/GEN getter binaries]`
- [ ] **v5 NCFW image — FILE-ABSENT.** `libncfw` carries exactly eight blobs `{v2,v3,v4,v4_plus} ×
  {iram,dram}`; `libncfw_get_image` tops out at MARIANA_PLUS (`0x1c`); the rodata closes at
  `__GNU_EH_FRAME_HDR` with **no** `maverick.c` token, no `v5_ncfw_*` blob, physically no room for a
  fifth. (MAVERICK still *does* collectives — the same on-engine `0xBF` way; what is absent is the
  NCFW management layer.) `[absence HIGH/OBSERVED]` **closable-with-corpus.**
- [ ] **v5 `Q7_CC_TOP` collective firmware — FILE-ABSENT.** `MAVERICK_Q7_CC_TOP*_get` = **0** — a
  genuine provisioning gap. The v5-specific dispatch bodies and the native-UCIe D2D transport live in
  firmware images this checkout does not carry; the **v4/v5-shared kernels ARE decoded via the
  Mariana images**. `[absence HIGH/OBSERVED]` **closable-with-corpus.**

> **GOTCHA — never fabricate a v5 byte.** No v5 `arch_id` byte, no v5 collective firmware, no v5
> silicon part-binding, no "Trn4" product name is ever asserted as fact. The **encoding cover holds
> across v5** because it is gen-invariant (one `libisa-core.so`, no `arch_id`/`coretype` gate in its
> decode path); everything v5-*firmware-internal* is INFERRED or file-absent, flagged on every use.
> Build v2–v4 as a hard specification; build v5 only as far as its OBSERVED identity surface, and
> mark every interior INFERRED. `[HIGH/OBSERVED on the file-absence; interiors INFERRED]`

> **NOTE — TONGA (v1) is out of scope by design.** TONGA is a **pre-unified outlier**, *not* a
> sixth GPSIMD generation: no `coretype`, no `arch_id`, no NCFW image, no EXTISA blob, a distinct
> `TONGA_ISA_TPB_DTYPE_*` enum family, and a register-block ISA that predates the unified opcode
> namespace. The gen-invariant core R(Q7) covers exactly **v2..v5**. Treat any `tonga` token as the
> legacy "L" ISA ancestor. `[HIGH/OBSERVED]`

---

## Am I done — the done-criteria

You are **done** when each closed axis reproduces its headline fraction against its binary witness.
These are the exact numbers from the [Coverage Ledger §1](coverage-ledger.md) and the
[Verdict A.2](../orientation/verdict-and-open-questions.md) — re-ground each with `nm <binary> |
rg -c` against the shipped file, never a decompile grep.

| Axis | Done-criterion | Witness | Tag |
|---|---|---|---|
| **Value semantics** | **864 / 864 = 100%** value leaves resolved | `nm libfiss-base.so \| rg -c module__xdref_` = 864 | `[HIGH/OBSERVED]` |
| **Execution-validated** | **~95%** of value-bearing leaves driven LIVE bit-exact; **18** op families; **~2.09M** comparisons; **0** firmware value bugs | the VAL capstone per-family matrix | `[HIGH/OBSERVED — the 0; MED — the ~95%]` |
| **Encoding** | certified-perfect cover — **1534 / 1534** mnemonics, **12569 / 12569** placements (from **1607 / 12642** pre-fold); objdump round-trip 0 disagreements | `nm libisa-core.so \| rg -c …_encode` = 12569; `… \| sort -u` = 1534 | `[HIGH/OBSERVED]` |
| **Struct census** | **~169 / ~171 (~99%)** domain structs field-exact — every struct on the path a custom op travels | `libnrt` DWARF reads | `[HIGH/OBSERVED]` |
| **Net grade** | **≥97% reimplementation-grade for v2–v4** (SUNDA/CAYMAN/MARIANA/MARIANA_PLUS); **header-OBSERVED + bounded-INFERRED** for v5 (MAVERICK) and v1 (TONGA) | the consolidated verdict | `[HIGH/INFERRED over the named denominators]` |

If those five reproduce, you have built a **byte-grounded, execution-validated, Vision-Q7-compatible
GPSIMD engine for v2–v4** as a hard specification, and a **bounded reference for v5/v1**.

### The honest residual — what stays open even when you are "done"

Done does **not** mean zero residuals. It means every residual is a **named, categorized
boundary** — not a missing decode, a missing datapath, or a missing value semantics. The
[meta-finding](open-questions-register.md#0-the-headline-thesis--read-this-first) governs the whole
list: across ~2.09M comparisons, **every** apparent value divergence root-caused to the reference
model or a tool, **never** the firmware. The residual partitions into six honest closers:

- **a driver away** — Q1 (the 3 recipqli soft-float leaves, the sole value-leaf wall);
- **a checkout away** — Q2 (7 uncited nrtucode fns), Q7 (NCFW LX bodies), Q8/W4 (v5 `Q7_CC_TOP`),
  Q9 (raw-kind vs ext-isa-id labeling), Q11 (host collective-compose), Q12a (FW-42 seed lineage +
  2 half-ULP boundaries), W1 (v5 `arch_id 36`), W2 (v5 geometry), W3 (v5 D2D PHY);
- **a license key away** — Q3 (DVE read-back `0x9b`/`0xe9` state), Q4 (the gated cycle/fault/trace
  oracle behind `AUTH::check_iss_licenses`);
- **a runtime capture away** — Q6 (host-loaded PWP coefficient content), Q12e (value-immaterial
  scatter-add per-cycle ordering);
- **a follow-on static pass away** — Q10 (the C16 FMA `_2` 5-output reassembly);
- **fundamentally out of scope** — Q5 (stochastic rounding is not a GPSIMD value-datapath feature),
  Q12c (the empty `MODULE_SCHEDULE` per-port matrices — the 1+1 ceiling is the sound bound).

**Flag every v5-interior fact INFERRED.** A reimplementer must know exactly what is byte-grounded
(v2–v4) versus header-OBSERVED-only (v5): `arch_id 36` **INFERRED·doubly** (carry `36*`), `ct37`
**OBSERVED**, the v5 NCFW image **FILE-ABSENT**, v5 `Q7_CC_TOP` **FILE-ABSENT**. Build v5 only to
its OBSERVED identity surface; never fabricate across the walls.

---

## See also — the capstones this checklist sequences

- [The Reimplementation Verdict & Open-Questions Map](../orientation/verdict-and-open-questions.md)
  — *can it be rebuilt, at what confidence?* The verdict this checklist operationalizes; the source
  of the done-criteria fractions and the v5 walls.
- [The Gen-Invariance Thesis](../orientation/gen-invariance.md) — *how do I cover all five
  generations?* The `A + parameterize(B)` equation that forces the build order (core first,
  parameter vector last); the source of Stage 6.
- [A Custom Op, End to End](../orientation/customop-end-to-end.md) — the worked trace Stages 3–4
  build against, hop by hop (codegen → compile → link → prelink → load → dispatch → run).
- [A Collective, End to End](../orientation/collective-end-to-end.md) — the worked trace Stage 5
  builds against (trigger → lower → TOP_SP `cc_op` → pring → NCFW → SB2SB + CCE → barrier).
- [The Coverage Ledger](coverage-ledger.md) — the per-lane, per-axis completeness accounting behind
  the done-criteria; the *what-is-covered* anchor.
- [The Open-Questions Register](open-questions-register.md) — the exhaustive *what-is-not-covered*:
  every residual, partitioned by closability, with its concrete closer.
- [Keystone Facts Reimplementers Get Wrong](../orientation/keystone-facts.md) — the ~18 traps
  (the FLIX 7→4 length model, the `coretype` stride, GPSIMD-not-the-float-path, the no-signing-key
  boundary, the SortMerge phantom) this checklist's GOTCHAs draw from.
- [The Codename ↔ Generation Cross-Walk](codename-crosswalk-table.md) — the pinned lookup the
  Stage-6 parameter vector keys on.

---

> **Provenance.** Every fact on this page derives solely from static analysis of shipped,
> redistributable binaries, headers, and config — lawful interoperability reverse engineering.
> Recovered symbols, strings, immediates, and `nm`/`readelf`/`objdump` counts are binary-derived and
> citeable; the value-semantics results are what the shipped simulator returned when executed
> in-process. No vendor source tree was referenced, consulted, or quoted.
