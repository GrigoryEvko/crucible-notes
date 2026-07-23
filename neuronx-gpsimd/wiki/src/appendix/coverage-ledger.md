# The Coverage Ledger

This is the **completeness anchor** of the whole guide — the single page the
[Reimplementation Verdict](../orientation/verdict-and-open-questions.md) cites when it answers
*"how much of the Vision-Q7 'Cairo' (`ncore2gp`) GPSIMD engine is actually known, and at what
confidence?"* It is the long form behind the verdict's five headline fractions: a **per-lane
coverage assessment** that, for each of the nineteen reverse-engineering lanes, states the
coverage, splits it into what is **OBSERVED** (read or executed from a shipped binary), what is
**INFERRED** (reasoned over OBSERVED bytes), and what is a genuine **static-analysis WALL** (a
corpus / license / runtime boundary, not a knowledge gap), and pins the key deliverables to the
owning synthesis page.

The verdict page is the navigable map; this is the accounting. Where the verdict states
"~95% execution-validated," this page shows the per-lane denominators that roll up to it; where
the verdict says "v5 header-OBSERVED + bounded-INFERRED," this page carries the exact
OBSERVED/INFERRED seam per lane. Every headline number below is grounded against its owning
synthesis page and, where cheap, re-read from the binary (`nm` on the one file by
absolute path — never a folder-wide scan).

Confidence tags follow [the Confidence & Walls Model](../reference/confidence-model.md):
`OBSERVED` = a byte/symbol/immediate read from the shipped binary (or a value computed by
executing the shipped simulator); `INFERRED` = reasoned over OBSERVED facts; `CARRIED` = re-used
at a cited report's confidence; each crossed with `HIGH` / `MED` / `LOW`. Callouts: **QUIRK**
(counter-intuitive but real), **GOTCHA** (a reimplementation trap), **CORRECTION** (overturns a
naive reading), **NOTE** (orienting context).

> **GOTCHA — count with `nm`, never a decompile grep.** Every count cell on this page is the
> owning page's `nm <abs> | rg -c` figure, re-verified against the shipped binary where it is the
> keystone. A "symbol hit count" grepped from the 884k-file decompile inflates **2–12×** against
> the binary's true `.symtab`/`.dynsym` table. The binary is the arbiter; the decompile is not.
> `[HIGH/OBSERVED]`

> **NOTE — what a "lane" is.** A lane is one reverse-engineering axis with one owning capstone:
> ISA, ISS, VAL, HW, FW, IMG, GEN, ABI, RT, DMA, CCL, NCFW, NEFF, CC, STRUCT, CSR, ADDR, INT, SEC.
> They are not disjoint subsystems — VAL *validates* ISA's value leaves, ISS *executes* them, CC
> *produces* the opcodes FW *dispatches* — but each carries an independent coverage claim with its
> own binary witness, so they total honestly.

---

## 1. The headline fractions — each tied to its owning page

These four fractions are the completeness anchor. The verdict page quotes them; this page
grounds each against the owning synthesis page **and** the binary where it is the keystone.

### 1.1 Value semantics — **100% (864/864)** `[HIGH/OBSERVED]`

Every value-producing opcode of the GPSIMD datapath has its per-element function recovered. The
denominator is the **864** `module__xdref_` value leaves in `libfiss-base.so`; all 864 are
enumerated and their semantics read from the bytes, and for all but the one `recipqli` wall
(§3, VAL lane) **executed** in-process.

- **Owning pages:** [VAL capstone §6.3](../validation/capstone-matrix.md) ("Value semantics: 100%
  known"); [the ISA value-cover §5](../isa/core/coverage-tally.md) (`864/864` leaves);
  [verdict A.2](../orientation/verdict-and-open-questions.md) ("864 / 864 = 100%").
- **Binary re-verify:**

  ```
  nm -D libfiss-base.so | rg -c 'module__xdref_'   = 864
  nm    libfiss-base.so | rg -c 'module__xdref_'   = 864   (same in .symtab)
  ```

  Reproduces exactly. `[HIGH/OBSERVED]`

### 1.2 Execution-validated — **~95%**, ~2.09M comparisons, **0 firmware bugs** `[HIGH/OBSERVED — the 0; MED — the ~95%]`

The overwhelming majority of value-bearing leaves were driven **LIVE** against the shipped
simulator and reproduced bit-exact, across **18 op families** / **~2.09M** in-process differential
comparisons, with **zero** firmware value defects found — every one of the catalogued divergences
(D1–D13) lived in the reference model, the analyst's lift, the python harness, or an inferred
closed-form, **never the firmware**.

- **Owning page:** [VAL capstone §3, §6](../validation/capstone-matrix.md) (the meta-finding +
  the grand totals); [verdict A.2](../orientation/verdict-and-open-questions.md).

> **NOTE — why `~95%` is `MED` on the fraction but `HIGH` on the fact.** The *fact* that ~2.09M
> live comparisons over 18 families returned **zero** firmware value mismatches is `[HIGH/OBSERVED]`
> — it is what the binary returned when executed. The exact *fraction* (`~95%`) is `MED`: it is a
> census over a denominator the later VAL passes widened (from ~85–90% over 9 families to ~95% over
> 18). The VAL capstone is also honest that the **2.09M aggregate** is ≈0.60M bottom-up summable +
> ~1.49M fp/MAC/4-leg remainder CARRIED — but the **0-mismatch** count is summable and confirmed on
> every family. Encode "the value lane is the arbiter and found no firmware bug"; treat the
> percentage as a bounded estimate.

### 1.3 Encoding — a **certified-perfect cover (1534/1607/12642)** `[HIGH/OBSERVED]`

The shipped Vision-Q7 encoding is a certified-perfect, non-overlapping cover: **1534 / 1534**
shipped mnemonics and **12569 / 12569** shipped placements, folded from the **1607 / 12642**
pre-fold TIE-DB authoring superset (the `+73` fold = 24 `.W18` wide-branch macros + 6 virtualops +
43 no-body pseudo). The device-native objdump/as round-trips byte-exact over hundreds of thousands
of bundles with zero disagreements.

- **Owning page:** [the ISA coverage tally](../isa/core/coverage-tally.md);
  [the semantics coverage ledger](../isa/semantics/coverage-ledger.md);
  [the formal ISA model](../isa/semantics/formal-isa-model.md).
- **Binary re-verify (`libisa-core.so`):**

  ```
  nm libisa-core.so | rg -c 'Opcode_.*_Slot_.*_encode'                       = 12569  (placements)
  nm libisa-core.so | rg -o 'Opcode_(.+)_Slot_[A-Za-z0-9_]+_encode' | sort -u = 1534  (mnemonics)
  1534 + 73 = 1607      12569 + 73 = 12642   (the fold arithmetic — exact)
  ```

  Reproduces exactly. `[HIGH/OBSERVED]`

> **GOTCHA — pair the totals correctly.** The only valid pairs are **`1534 ↔ 12569`** (shipped /
> runtime) and **`1607 ↔ 12642`** (pre-fold / authoring). Pairing `1534` with `12642`, or `1607`
> with `12569`, mixes the runtime fold with the authoring superset and manufactures a `±73` phantom.
> The shipped `1534 / 12569` is the denominator for *anything a reimplementer builds* (decoder,
> assembler, ISS). `[HIGH/OBSERVED]`

### 1.4 The net grade — **≥97% reimplementation-grade for v2–v4**; header-OBSERVED + bounded-INFERRED for v5 `[HIGH/INFERRED over the named denominators]`

Folding the three closed axes (encoding · value semantics · execution-proof) with the firmware
mechanism, the custom-op ABI, the host runtime spine, and the device dispatch, the consolidated
grade is a **byte-grounded, execution-validated reimplementation reference at ≥97% coverage for
v2–v4** (SUNDA / CAYMAN / MARIANA / MARIANA_PLUS), and a **header-OBSERVED + bounded-INFERRED**
reference for v5 (MAVERICK) and v1 (TONGA).

- **Owning page:** [verdict A.2 net grade](../orientation/verdict-and-open-questions.md).

> **GOTCHA — v2–v4 is hard spec; v5/v1 is header-OBSERVED only.** A reimplementer can target
> **v2–v4** as a hard specification — their firmware images, config, and ABI are present and read
> directly, their value semantics closed by execution. **v5 (MAVERICK)** and **v1 (TONGA)** are
> publishable only as **header-OBSERVED + bounded-INFERRED**, interiors flagged on every use. The
> `1534/12569` encoding cover is the **gen-invariant Cairo core** (one `libisa-core.so`, no
> `arch_id`/`coretype` gate in its decode path), so the *encoding* tally holds across all shipped
> generations; the per-generation *firmware/image* residuals (v5 absent) are a separate axis. See
> §4 for the v5 walls. `[HIGH/OBSERVED on gen-invariance of the encoding]`

---

## 2. The per-lane coverage table

One row per lane. **Coverage** is the lane's own self-assessed completeness (the owning page's
figure, in its own wording — many lanes report a structural cover with a flagged interior rather
than a single percentage). **OBSERVED / INFERRED / WALL** are the three-way split per lane.
**Deliverables** are the lane's key reimplementer artifacts. **Owning page** is the synthesis /
capstone that holds the full accounting. Confidence is HIGH/MED/LOW × OBSERVED/INFERRED/CARRIED.

| Lane | Coverage | OBSERVED | INFERRED | Genuine static-analysis WALL | Key deliverables | Owning synthesis page |
|---|---|---|---|---|---|---|
| **ISA** | **certified-perfect cover** — 1534/1534 mnemonics, 12569/12569 placements (folded from 1607/12642 pre-fold) | shipped encode/decode tables; 28-package census sums to 1534; objdump round-trip 0 disagreements | the `+73` fold direction reasoned from roster present/absent; `F4`/`F6` per-slot interiors from identical decode path | `F4`/`F6` INFERRED interiors `[MED]`; the FLIX-desync linear-sweep wall (cover does **not** depend on the sweep); empty `MODULE_SCHEDULE` → `1+1` ceiling | the 1534/1607/12642 certified cover; 14-format/46-slot/8-regfile decode; the four-source agreement tally | [isa/core/coverage-tally](../isa/core/coverage-tally.md) `[HIGH/OBSERVED]` |
| **ISS** | **100% of the value+decode oracle** — the cas+fiss ISS *is* the executable golden oracle (drivable value/timing/fault/trace) | 119 cas `nx_*_interface` value ports; 864 fiss value leaves; 61 `exception__` handlers; cas state 4,852,208 B; `nsau_16_16` driven LIVE | the fault-injection-as-oracle recipe (mechanism OBSERVED, recipe INFERRED); the `dll_cycle_advance` reverse-commit rule | **no SystemC/TLM wrapper exists** (0/0/0 — hard absence); `libcas-core` ships `.symtab` but **no DWARF** (offsets pinned from `mov`/`lea`); license-gated *retirement* (`AUTH::check_iss_licenses`) | the introspection / single-step / fault / SystemC debug seam; the 5-phase value pipeline; the 4-step differential-reference recipe | [iss/iss-oracle-synthesis](../iss/iss-oracle-synthesis.md) + [iss/iss-semantic-synthesis](../iss/iss-semantic-synthesis.md) `[HIGH/OBSERVED]` |
| **VAL** | **value semantics 100% known** (864/864); **~95% execution-validated** (~2.09M comparisons, **0 firmware bugs**) | 18 op families driven LIVE bit-exact; 0 firmware-value mismatches every family; fp16 classify swept exhaustively (65,536 patterns) | the ~95% fraction `[MED]`; the ~1.49M fp/MAC/4-leg remainder of the 2.09M aggregate `[CARRIED]` | **`recipqli` soft-float dispatch** (3/864 leaves, SIGSEGV on bare drive — heavy-leg only); FW-42 seed lineage + 2 half-ULP closed-form boundaries (ROM=truth) | the per-family pass/fail matrix; the D1–D13 divergence catalog (all in model/harness/inferred-form, none in firmware); the residual-closure ledger | [validation/capstone-matrix](../validation/capstone-matrix.md) `[HIGH/OBSERVED·exec]` |
| **HW** (uarch) | **complete cycle-approximate model** except the flagged cycle-count tier | config census; 14-format/46-slot decode; 2-pipe stage stamps; reservation bodies (2149 issue/1746 stall/~160k stage); FCR/FSR + single-round FMA; 2-LSU split, no-PSUM, no-D-cache | all block-to-block datapath WIRING / topology (RTL not in corpus); 2×FMAC dual-issue micro-binding; peak-compute bundle composition | host-supplied ACT PWP coefficient **content** (out-of-corpus `[LOW]`); RTL topology absent; per-port reservation below the `1+1` ceiling (`MODULE_SCHEDULE` empty); FW-42 QLI polynomial | one unified cycle-approximate model; the 6-divergence reconciliation ledger; the block diagram + connectivity map; `R(Q7)` gen-invariance | [uarch/microarch-synthesis](../uarch/microarch-synthesis.md) `[HIGH/OBSERVED; topology MED/INFERRED]` |
| **FW** | **55.7% body-decoded** (78/140 real HW opcodes), +7.9% planned, 36.4% NONE | `172 − 31 PSEUDO − 1 INVALID = 140` real opcodes from 4 shipped `common.h` enums; per-gen 145/150/159/165; 3 MAVERICK ops byte-pinned (`0xB6`/`0xB9`/`0xBA`) | MAVERICK late ops `0x26`/`0xF3`/`0xF4` — names read HIGH, byte/engine/operand MED | **FLIX-desync device interiors** (`0x45` Pool reduce, `0xBD`/`0xF1` DMA-transpose, MAVERICK late ops — opcode byte-exact, interior MED, the "corpus-wide MED ceiling"); SUNDA POOL in out-of-corpus EXTISA; **SortMerge phantom** (never shipped) | the `172→140` real-opcode derivation; the master opcode→kernel table (140 rows); the three-source crossing (140≠17≠55); per-gen presence + coverage tally | [firmware/kernels/opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) `[HIGH/OBSERVED roster; MED interiors]` |
| **IMG** | **100% of the container/getter index** (kernels out of scope) — 3 partitions close to 386, 0 unmapped getters | 386-getter `(gen×engine×flavor×region)` index; per-gen 24/100/100/100/62; 225 real / 161 zero-cursor; 3 resolvers disassembled; 6 carve SHAs | `engine_idx` POOL=1/ACT=2 (PE=0, DVE=3 OBSERVED); why NX_SP takes two image slots/gen; `build_version` tracks ulib not gen | **MAVERICK image INTERIORS** (per-v5 device-code decode); which silicon part each `(gen,engine)` binds to; which image the live host driver selects (upstream of these binaries); SUNDA EXTISA standalone-only | the 386-getter index; the 3 resolvers as annotated C; the container model (SUNDA standalone / MAVERICK twin-only); the closed completeness check | [images/firmware-image-catalog](../images/firmware-image-catalog.md) `[HIGH/OBSERVED]` |
| **GEN** | **complete capability matrix** (15 subsystems × 5 gens + TONGA floor); 13/13 spot-checks PASS; v5 interiors INFERRED | v2–v4+ cells byte-grounded (every count/sha/enum/selector); `coretype {6,13,21,29,37}`; 13 spot-checks re-carved | all MAVERICK runtime-INTERIOR cells (geometry/transport/NCFW Cayman-class+, no v5 runtime programmer in checkout); arch_id 36 (doubly) | **arch_id 36 doubly inferred** (the "headline wall", §4); MAVERICK image interiors getter-OBSERVED only; NCFW dark at v5; empty `MODULE_SCHEDULE`; SortMerge phantom; v5 `Q7_CC_TOP` FILE-ABSENT | the 15×5 master grid; the INVARIANT/SCALING/ABSENT partition (12/9/11) + strict-superset chain; the `R(Q7)` formal gen-invariance; the spot-verification ledger | [generations/master-capability-matrix](../generations/master-capability-matrix.md) `[v2–v4 HIGH/OBSERVED; v5 INFERRED]` |
| **ABI** | **complete end-to-end host↔device custom-op ABI**; top-5 spot-checks CONFIRMED; G1–G5 flagged | `libneuroncustomop.a` (10 ELF32-Xtensa REL); `customop_*` byte-exact; `ARG_TENSOR byte_size=48`; `get_cpu_id` = raw `rsr.prid`; 8 marshallable dtype immediates; 1479 FLIX bundles | G3 host window-manager vtable write `[MED]` (producer side INFERRED byte-identical; device consumer fully OBSERVED) | **G2 per-kernel SPMD channel-slicing formula** lives in customer/firmware kernel images, not in `libneuroncustomop.a`; the customer `_cpuN.so` are not in the shipped package (built on customer machine) | the 5-phase BUILD/LOAD/INVOKE/EXECUTE/RETURN pipeline; the master end-to-end C-pseudocode trace; the 8-dtype Rosetta; the unified Q7 32-bit NX memory model | [abi/abi-synthesis](../abi/abi-synthesis.md) `[HIGH/OBSERVED]` |
| **RT** | **complete host half** of the runtime spine (`NRT_2.0.0`); device-side disasm deferred | host `libnrt.so.2.31.24.0` (17,372 funcs, VMA==file-offset); spine edges/addresses (`nrt_set_pool_eng_ucode`/`nrt_load`/`nrt_execute`); 145 real exports (121 `nrt_*` + 16 `nec_*` + 8 `nrta_*`) | cross-process LNC/k8s 70/30 arbitration `[INFERRED]`; device-side Q7 ISA / kernel_info_table dispatch / customop consumption (CARRIED from device carves) | the **device-side Q7 facts are not host-resident** (CARRIED from `ncore2gp` carves); the on-core kernel run is outside the host binary; a device-halt timeout is unobservable from the host | the 4-layer stack (C ABI / runtime core / nrtucode+aws_hal / ndl portal); the LOAD path + silent ucode-override seam; EXECUTE = one semaphore doorbell on a pre-staged `0x107A` POOL stream; the FAIL-STOP lifecycle | [runtime/runtime-synthesis](../runtime/runtime-synthesis.md) `[HIGH/OBSERVED host]` |
| **DMA** | **complete data-movement reference** (11 siblings); 1 cross-page contradiction resolved | six 64-B descriptor structs + `struct2opcode`; opcode bytes (`0xb8`/`bb`/`bd`/`68`/`e7`/`f1`/`bf`/`f0`); SoC bases (STATE_BUF/DGE_MEMORY/PSUM_BUF); `en_comp_ring_update` S2M=1/M2S=0 | §3.2 submit→complete inter-step ordering (register-semantics-implied dataflow `[MED]`); v5 inline-descriptor runtime / UCIe PHY | **v5/MAVERICK interiors header-OBSERVED only** (no v5 NX-POOL firmware ships; MAVERICK `PSUM_BUF_SZ=0`, no PSUM region); CARRIED items (16-B BD bitfields, `cce_info` 140-B, `prio_cap`) rest on binaries absent from this extraction | the descriptor taxonomy (six 64-B words + 16-B BD + CCE); the DGE micro-op byte encoding; SDMA + 3-arbiter QoS + `prio_cap`; the SBUF/PSUM (Q7-cannot-reach-PSUM) keystone; the 3-way completion model | [dma/data-movement-reference](../dma/data-movement-reference.md) `[HIGH/OBSERVED]` |
| **CCL** | **complete collective spine** (13 op pages); 9 spine facts re-confirmed; O-1..O-8 open | all collective opcodes; `host_trigger 0x615a0`/`0x60848`; the `+0x708`/`+0x710` vtable split; `enc_alg_type` 11-enum; SB2SB `0xBF`; EVT_SEM windows | host `enc_alg_type ≡` firmware `algo_type` identity `[MED]`; routing_id→SoC high-bit rewrite; "TOP_SP executes the NCFW firmware" `[INFERRED-STRONG]` | **O-1 the NCFW LX-core decode wall** (no shipped LX disassembler config; `ncore2gp` mis-decodes ~26–28% of LX bytes as Vision FLIX bundles); per-algorithm case bodies / on-core step schedule / byte-level host→device `algo_type` write not instruction-decodable | the 6-layer spine diagram + per-boundary handoff; the pseudo-op opcode catalog; the `enc_alg_type` family + selection legality matrix; the barrier/EVT_SEM/tsync model | [collectives/ops/architecture-synthesis](../collectives/ops/architecture-synthesis.md) `[HIGH device · CARRIED host]` |
| **NCFW** | **~67–79% of NCFW LX bytes landed** for v2–v4 (LX 3-byte length rule); 4 deliverables complete for the 4 shipped gens | `libncfw.so` v2–v4 byte-grounded (`WindowOverflow8` @ IRAM `0x24`, the `libncfw_get_image` ladder, DRAM+0xB0 dispatch, 8 blob SHAs); v4→v4+ IRAM 70.2% byte-identical | **every v5/MAVERICK claim INFERRED or ABSENT** (arch_id `0x24` from `coretype−1`); `algo_type ≡ enc_alg_type` `[MED]` | **(1) the NCFW LX-core decode wall** (no LX config ships — dispatch spine OBSERVED, per-algorithm bodies + on-core step schedule **not** instruction-decodable); **(2) the v5/MAVERICK file-absent wall** — **no NCFW management-core image of either kind**, and MAVERICK does **not** reuse v4+ | the NCFW LX ISA + FLIX-mis-decode demo + LX length rule; the DMA-engine naming taxonomy; the arch_id/coretype reconciliation + 4-image-vs-5-gen map; the end-to-end collective-orchestration flow | [collectives/ncfw/lx-isa-naming-archid-synthesis](../collectives/ncfw/lx-isa-naming-archid-synthesis.md) `[v2–v4 HIGH/OBSERVED; v5 file-absent]` |
| **NEFF** | **complete publishable format spec** (v2–v4 byte-grounded); 7 LOW open items | 1024-B `neff_header_t`; container/`pkg_version` gates; device Xtensa-PI-ELF32 carved (`e_machine 0x5E`, `.rela.got` 240 entries, `kernel_info_table` 17 rows @ `0x02000380`); UCPL magic | on-device ucode-lib placement `[MED]`; NEFF-supplied `library` BIN same ELF32-PI format `[HIGH × INFERRED]` (none byte-present); v5 device interior | the single fixture is a toy model (no constant weights, no custom-op ucode lib) — weight/ucode-lib sections carved elsewhere; no NEFF-supplied `ucode_lib` ELF tar member in this corpus subset; `numpy_load` v2/v3 headers unproven | the 1024-B header byte map + container gates; the `def.json` section→parser catalog + var-table device I/O ABI; the device Xtensa-PI-ELF32 kernel + `kernel_info_table` dispatch; the 3-tier version/compat gates | [neff/format-reference](../neff/format-reference.md) `[HIGH/OBSERVED v2–v4]` |
| **CC** (compiler) | **complete fan-in lowering model** — every GPSIMD-relevant BIR `Inst*` maps to a ledger opcode; **zero** produce an opcode the firmware roster lacks | validator field names + dtype enum byte-exact; 62 `emit_`; 110 BIR concrete classes; 172 opcode union (140 real + 31 PSEUDO + 1 INVALID); the int32-no-PSUM→GpSimd frontend predicate; 864/864 xdref | engine attribution for `0xf3`/`0xf4` (validator constant only, both =128); BF16 "fold into generalised dtype dispatch"; SundaISel in-ISel branch micro-order | **device body of the fan-in opcodes not byte-traced** — SUNDA ships RELEASE (string-stripped); MAVERICK ships **no `MAVERICK_NX_POOL_DEBUG` image** so a `0xf3` POOL self-name cannot appear; `0xf4` no dedicated self-name in present MAVERICK DVE DEBUG `[MED/deferred]` | the validator-gated `{dtype,engine,gen,flag}` fan-in model; the 7-hop CC lane (NKI/HLO→emit→BIR→SundaISel→SundaISAInst→opcode→engine→ISS); the producer accounting; the 3 lowering shapes (1:1 / MACRO / FAN-IN) | [compiler/dtype-engine-fanin-synthesis](../compiler/dtype-engine-fanin-synthesis.md) `[HIGH/OBSERVED roster; MED interiors]` |
| **STRUCT** | **~169/~171 domain structs field-exact (~99%)** — every struct on the path a custom op actually travels is recovered | every host struct size+offset is a `libnrt` DWARF read (`tdrv_ctx_t` 18,884,656 B; `tpb_t` 39,320 B w/ all 5 GPSIMD anchors; `model_t`/`hw_exec_queue_t`/`exec_request_state`); the 4 `nrtucode_*` quartet sizes from ctor immediates; `0x102020204` NX_POOL gate | the §3 ranking ordering; the two exec anon sub-struct interiors (byte_size OBSERVED, members unnamed); per-engine map of `nrtucode_core[5]` | **v5/Maverick struct interiors INFERRED** — `NRTUCODE_CORE_MAVERICK_*` exist only behind `#if NRTUCODE_INTERNAL_NAMES`; no byte-grounded Maverick struct interior observable `[LOW × INFERRED]`; the device-management quartet is forward-declared / no-DWARF in both binaries (ctor-store reads, never a DWARF lift) | the recovery method (dump/parse Rosetta, IDA JSON, header `static_assert`, native byte-decode); the per-binary corpus census; the host exec-state object tree field-exact; the opaque device quartet byte-exact + the census-close ledger | [appendix/struct-census-overview](struct-census-overview.md) + [appendix/struct-exec-state-census](struct-exec-state-census.md) `[HIGH/OBSERVED host; v5 LOW/INFERRED]` |
| **CSR** | **65/85 byte-exact**, 2 shape-captured, 18 byte-undumped peripheral residual; 76 referenced / 9 orphans / **0 missing** | all counts re-derived by `jq`/`rg` over shipped `csrs/*.json` + `address_map_flat.yaml`; cce/cme/dre + notific + erg + misc_ram byte-exact; `0xDEADBEEF`/`0x2` SLVERR fault-terminator | §2.3 variant rationale; the "tensor-DMA"/"collective" role readings for DRE/CCE; the cross-gen notific "grew/predates" reading `[MED]` | **v5/Maverick interior register layout** — no v5 register schema ships; `maverick/address_map/` is per-die C headers (header-OBSERVED) + pkl/json mirror only; every v5-interior register claim INFERRED | the NOTIFIC 1-vs-10-queue variant reconciliation; the SDMA residual cce/cme/dre byte-exact; the MISC residual (erg ECC/parity); the 85-schema two-lens coverage ledger + the AXI fault-terminator family | [control/csr/notific-sdma-residual](../control/csr/notific-sdma-residual.md) `[HIGH/OBSERVED v2–v4]` |
| **ADDR** | **dual-view complete** — CAYMAN flat map 34,858 nodes / 19,012 json bindings; MAVERICK pkl 323,198 records (both axes reconcile, residual 0) | CAYMAN/NC-v3 byte-grounded in `address_map_flat.yaml` (every region base line-cited); Q7-local NX view; the 3-coordinate conversion chain; the gen-invariant offset set | the "forward-portable engine" reading of gen-invariance; the `0x2802012345` host-window choice `[MED]`; run-stall offset within LOCAL_REG | **MAVERICK / NC-v5 interior behaviour** — the pkl DB *structure* is OBSERVED; **what a v5 address does inside the silicon is INFERRED**; the byte-grounded behavioral reference is the CAYMAN flat YAML | the one canonical unified CAYMAN/MAVERICK region table; the 3 coordinate systems + conversion chain + worked round-trip; the CAYMAN-vs-MAVERICK delta table; the gen-invariance thesis | [control/address/unified-soc-memory-map](../control/address/unified-soc-memory-map.md) `[HIGH/OBSERVED CAYMAN; v5 structure-OBSERVED, behavior-INFERRED]` |
| **INT** | **complete fault chain** — 1,688 Cayman sources, 1,932 INTC instances, 962 errtrig pairs; HIGH to the apex MSI-X + FW cause table | NSM 9 protocol-shape causes (4 wr + 5 rd); 6 isolation-enable bits; `reset_handshake_intr[8..15]`; `0xDEADBEEF` register census 8/1/0; apex idx111 NSM critical:1 | isolation-SM transition arrows (debounce/inject/drain order); summarized fan-in through `pcie_*_nmi`; FW handler step logic | the **apex → Q7/GIC vector hop is firmware/HW-owned, in no shipped artifact** (explicit non-claim, `[LOW/INFERRED]`); ISR bodies not register-encoded; all v5/Maverick interior behaviour header-OBSERVED (the Maverick `nsm.json` is the one OBSERVED-on-disk exception) | the end-to-end NSM PCIe fault chain (6 stages); the unified source→detection→routing→delivery map; the shared host-PCIe isolation state machine (states OBSERVED, arrows INFERRED); the fifo-drain recovery teardown | [control/interrupt/nsm-flow-unified](../control/interrupt/nsm-flow-unified.md) `[HIGH to apex; final hop MED/LOW]` |
| **SEC** | **complete 3-layer model** (SoC-fabric / on-core XEA3-MPU / firmware-load integrity); 61 `*_exc` handlers re-counted | the 3 load-path keystone symbols (`xtlib_verify_magic`/`validate_dynamic_load`/`prelink_relocate_lib`); zero crypto-auth dynsyms, NEEDED=libc only; `XCHAL_HAVE_SECURE=0`, MPU 16+2, XEA3; deadbeef 8/1/0; dual remapper amzn=fail-**CLOSED** / user=fail-**OPEN** | "no cryptographic root of trust" / "host is the trust root" / "co-located ops not isolated" framings (follow from OBSERVED facts); the arming order between OBSERVED CSR writes | **Maverick (NC-v5) interiors INFERRED** unless a Maverick artifact is cited directly (the remapper geometry is CARRIED, not re-decoded); the apex→Pacific GIC vector hop firmware/HW-owned `[LOW/OPEN]`; firmware ISR bodies not in repo; secmon dead in silicon (zero compiled secure objects) | the one-sentence model + 3 enforcement layers; the trust model (integrity-rooted, the silent unauthenticated `nrt_set_pool_eng_ucode` override, the fail-open/fail-closed boundary); the XEA3 fault model (9 EXCCAUSE, 61 `*_exc`); the co-tenancy findings + M1–M7 mitigations | [control/security/security-synthesis](../control/security/security-synthesis.md) `[HIGH/OBSERVED; v5 INFERRED]` |

> **NOTE — "complete" means coverage of what the lane *can* recover from this corpus, with the
> residual named.** No lane claims to have recovered an artifact that is not in the corpus; where a
> body is absent (the v5 NCFW image, the customer `_cpuN.so`, the host PWP coefficient content), the
> lane carries it as a WALL with its closability, not as a silent gap. The honest reading of every
> "complete" cell is "complete up to the named wall." `[HIGH]`

---

## 3. The OBSERVED / INFERRED / WALL discipline — what each lane's three columns mean

The split is not cosmetic — it is the whole point of the ledger. A reimplementer encodes the
**OBSERVED** column as a hard requirement, treats the **INFERRED** column as a bounded estimate to
re-confirm on hardware, and reads the **WALL** column as "do not fabricate past here — close it
with the cited artifact."

- **OBSERVED** is a byte read from a shipped binary (`nm`/`readelf`/`objdump`) or a value the
  shipped simulator returned when executed in-process. The 864 value leaves, the 1534/12569
  encoding tables, the 386 image getters, the 65/85 byte-exact CSR schemas, the `tpb_t` 39,320-byte
  layout — all OBSERVED. This is the bedrock.
- **INFERRED** is reasoned over OBSERVED bytes: the HW datapath wiring (RTL absent, so box-to-box
  topology is reasoned from the stage stamps), the `arch_id 36` (from the `coretype − 1` relation),
  the cross-process 70/30 GPU arbitration. INFERRED is honest reasoning, flagged so it is never
  mistaken for a byte-read.
- **WALL** is a genuine static-analysis boundary — `closable-with-corpus` (a fuller checkout),
  `closable-with-license` (a FlexNet key on the already-runnable ISS), `closable-with-hardware`
  (a runtime capture / device run), `closable-with-static` (a follow-on pass on the binary in
  hand), or `fundamental` (the thing does not exist in this subsystem). The taxonomy is the
  [Confidence & Walls Model §3](../reference/confidence-model.md#3-what-a-wall-is).

> **NOTE — the meta-finding the whole ledger rests on.** Across all nineteen lanes and the ~2.09M
> VAL comparisons, **not one open question is a missing datapath body, a missing opcode decode, or a
> missing value semantics.** Every WALL is a corpus / license / runtime / scope boundary — a
> checkout, a key, a capture, or a follow-on pass away. The machine is recovered; what is walled is
> a runtime input, an absent generation's image, an out-of-config core, or a license-gated
> observable. `[HIGH/INFERRED over the named denominators]`

The one value-leaf wall worth naming up front (the VAL lane's only structural wall): the **3
`recipqli` soft-float QLI refine leaves** (3/864 ≈ 0.35%) route their value-producing FMA through a
host soft-float dispatch object (`call *0x38(%rax)`) a bare ctypes drive cannot populate — a
fork-isolated probe SIGSEGVs on the NULL slot. Everything *under* the wall (the 6-bit segment
index, the four QLI coefficient tables read bit-exact, the two-substage split, the device
round-trip) is proven; only the soft-float FMA on top is gated. This is a **driver gap, not a
knowledge gap**, reachable via the heavy-leg ISS. `[wall HIGH/OBSERVED; refine interior CARRIED]`

---

## 4. The v5 / MAVERICK walls — the OBSERVED/INFERRED seam, stated carefully

The v5 (MAVERICK) generation is the single largest INFERRED region in the guide, and the ledger
states its seam precisely because several downstream pages cite it. **The hard fact is that the v5
NCFW management-core image is FILE-ABSENT** — there is no MAVERICK NCFW image of either kind in this
corpus, and MAVERICK does not reuse the v4+ image.

The binary evidence (from the [NCFW synthesis §3.2](../collectives/ncfw/lx-isa-naming-archid-synthesis.md)
and the [IMG catalog](../images/firmware-image-catalog.md)):

- the `libncfw_get_image` ladder compares only `{0x05, 0x0c, 0x14, 0x1c}` (SUNDA / CAYMAN /
  MARIANA / MARIANA_PLUS); a `cmp $0x1c; ja …` sends any `arch_id > 0x1c` — including `0x24` (= 36)
  — to `return 2`;
- the `libncfw` rodata closes at `0x918e4` with zero v5/maverick symbols and exactly four codename
  `.c` strings.

### 4.1 The `ct37` OBSERVED vs `arch_id 36` INFERRED split

> **NOTE — `coretype 37` is OBSERVED; `arch_id 36` is INFERRED. State both; pick neither
> silently.** The two facts live on opposite sides of the OBSERVED/INFERRED line, and the SCOPE
> requires the seam be stated, not collapsed:
>
> - **`coretype 37` (`ct37`) — OBSERVED.** It is byte-read in the live `get_ext_isa` jump-table arm
>   (`case 37: base = maverick_libs`) and in the internal-twin dispatch gate (`cmp $0x25` = 37, 6
>   hits). The [IMG catalog](../images/firmware-image-catalog.md) and the
>   [GEN matrix](../generations/master-capability-matrix.md) both tag it `[ct37 HIGH/OBSERVED]`.
> - **`arch_id 36` (`0x24`) — INFERRED.** It is **not** byte-read anywhere. It rests on the
>   `arch_id = coretype − 1` relation extrapolated from the four observed generations; there is no
>   `cmp $0x24` and no v5 NCFW image to confirm it. The [GEN matrix](../generations/master-capability-matrix.md)
>   escalates it to **"doubly inferred"** because a second cross-check — the `NX_TOPSP = arch_id`
>   rule — actually *fails* for MAVERICK (the enum slot 36 is a `MAVERICK_NX__REMOVED__` placeholder;
>   the real `MAVERICK_NX_TOPSP` sits at index 54). Tag `[arch_id 36 MED/INFERRED]`; always carry the
>   `36*` asterisk.
>
> The relation that ties them is `coretype = arch_id + 1`; it is that `−1` relation — not any
> `coretype` stride — that extends the v5 `arch_id` to 36. (Do **not** assert a flat `+8` coretype
> stride: the observed set `{6, 13, 21, 29, 37}` is `+7` then `+8, +8, +8`.)

> **CORRECTION — there is an OPEN cross-page question on whether `ct37` is fully OBSERVED, and this
> ledger does not silently resolve it.** The IMG/GEN/CCL pages tag `coretype 37` OBSERVED (a live
> jump-table arm and a `cmp $0x25` gate). The NCFW page, which owns the v5 file-absence finding,
> reaches the *same* `arch_id` conclusion but frames the whole v5 region as "every v5/MAVERICK claim
> is INFERRED or ABSENT, never OBSERVED" — i.e. it does not separately re-assert `ct37` as
> independently OBSERVED, because its own binary (`libncfw.so`) carries **no** v5 arm to read.
> **Both are internally consistent** (`ct37` is OBSERVED *in the IMG/GEN binaries*, absent *in the
> NCFW binary*), but the pages differ on whether to call the v5 surface "header-OBSERVED" or
> "INFERRED-or-absent." The hard, non-negotiable fact all four pages agree on is the one stated at
> the top of §4: **the v5 NCFW image is FILE-ABSENT.** The **appendix-internal** standardization is
> now applied (Part-16 reconcile): the one appendix outlier — the
> [device-firmware globals §1.5i](struct-device-firmware-globals.md) CORRECTION that had flipped
> `ct37` to INFERRED — was rewritten to **OBSERVED** (keeping its true v5-NCFW-image-absent point),
> so every appendix page now reads `[ct37 HIGH/OBSERVED in the IMG/GEN getter binaries; arch_id 36
> MED/INFERRED·doubly; v5 NCFW image FILE-ABSENT HIGH/OBSERVED-absence]`. The residual cross-Part
> framing question — whether the **NCFW synthesis page** should re-assert `ct37` OBSERVED or keep its
> "INFERRED-or-absent" framing (its own `libncfw.so` carries no v5 arm to read) — is the only piece
> left for the **global cross-Part pass**. `[CORRECTION]`

### 4.2 The other v5 interiors — INFERRED, per lane

Every lane that touches v5 carries the interior as INFERRED, never OBSERVED:

- **GEN / IMG:** MAVERICK image interiors are getter-/dispatch-OBSERVED only; the per-v5 device-code
  decode is a wall; `Q7_CC_TOP` collective firmware FILE-ABSENT.
- **DMA:** v5 interiors header-OBSERVED — `PSUM_BUF_SZ = 0`, no PSUM region; SBUF re-bases to 128 MiB
  (OBSERVED header values), runtime semantics INFERRED.
- **ADDR:** the MAVERICK pkl DB *structure* (323,198 records, 5 views) is OBSERVED; what a v5 address
  *does in silicon* is INFERRED; the byte-grounded behavioral reference is the CAYMAN flat YAML.
- **STRUCT:** `NRTUCODE_CORE_MAVERICK_*` exist only behind `#if NRTUCODE_INTERNAL_NAMES`; no
  byte-grounded Maverick struct interior is observable `[LOW × INFERRED]`.
- **CSR:** no v5 register schema ships; `maverick/address_map/` is per-die C headers + pkl/json
  mirror only; interior register layout INFERRED.
- **INT:** the Maverick `nsm.json` ships and is byte-identical to Cayman (OBSERVED-on-disk), but the
  v5-interior behaviour (13 per-IP-block INTCs, per-die 119-entry apex) is INFERRED.
- **SEC:** the `pass_on_miss` + `master_prot` invariants are frozen across SUNDA→CAYMAN→MARIANA, but
  the Maverick remapper geometry is CARRIED, not re-decoded.
- **CC:** the `MXTENSOR_V2` handler body (the v5 MX interior) is header-OBSERVED only and
  FLIX-desynced → INFERRED; MAVERICK ships no `MAVERICK_NX_POOL_DEBUG` image.

> **GOTCHA — never fabricate a v5 byte.** No v5 `arch_id` byte, no v5 collective firmware, no v5
> silicon part-binding is ever asserted as fact. The encoding cover (§1.3) holds across v5 because
> it is gen-invariant; everything v5-firmware-internal is INFERRED or file-absent, flagged on every
> use. `[HIGH/OBSERVED on the file-absence; the interiors INFERRED]`

---

## 5. Adversarial self-verification — the five strongest claims

1. **"Value semantics 100% (864/864)."** *Challenge:* is 864 a decompile grep or a binary symbol
   count? *Resolved:* `nm -D libfiss-base.so | rg -c 'module__xdref_'` = **864** (and the
   `.symtab` count is also 864); the [VAL capstone §6.3](../validation/capstone-matrix.md) reads
   "Value semantics: 100% known" and the [verdict A.2](../orientation/verdict-and-open-questions.md)
   "864 / 864 = 100%". **Survives.** `[HIGH/OBSERVED]`

2. **"~95% execution-validated, ~2.09M comparisons, 0 firmware bugs."** *Challenge:* is the 2.09M a
   round number, and is the 0 transcribed? *Resolved:* the [VAL capstone §6.2](../validation/capstone-matrix.md)
   is explicit that 2.09M = ≈0.60M summable + ~1.49M fp/MAC/4-leg CARRIED, and that the
   **0-mismatch** count is summable on every family (re-confirmed by 18 representative leaves
   `dlopen`'d and called). The ~95% is `MED` on the fraction, `HIGH` on the fact. **Survives,
   honestly bounded.** `[HIGH/OBSERVED 0-bugs; MED ~95%; CARRIED 1.49M remainder]`

3. **"Certified-perfect cover 1534/1607/12642."** *Challenge:* do the counts reproduce and pair
   correctly? *Resolved:* `nm libisa-core.so` gives **12569** placements and **1534** distinct
   mnemonics; `1534 + 73 = 1607` and `12569 + 73 = 12642` (exact); the valid pairs are
   `1534 ↔ 12569` (shipped) and `1607 ↔ 12642` (pre-fold), never the cross-pair. **Survives.**
   `[HIGH/OBSERVED]`

4. **"≥97% reimplementation-grade for v2–v4; v5 header-OBSERVED + bounded-INFERRED."** *Challenge:*
   is the ≥97% defensible, and is v5 correctly bounded? *Resolved:* the
   [verdict A.2 net grade](../orientation/verdict-and-open-questions.md) states exactly "≥97%
   coverage for v2–v4" and "header-OBSERVED + bounded-INFERRED reference for v5 and v1"; every
   v5-touching lane in §4.2 carries the interior as INFERRED, and the v5 NCFW image is the
   file-absent hard fact. **Survives.** `[HIGH/INFERRED over the named denominators]`

5. **"The per-lane walls are real boundaries, and the v5 seam is ct37-OBSERVED / arch_id-36-INFERRED."**
   *Challenge:* is any wall actually a hidden knowledge gap, and is the v5 seam collapsed? *Resolved:*
   every WALL column entry maps to a closability tag in the
   [Walls Model](../reference/confidence-model.md) (corpus / license / hardware / static /
   fundamental) — none is a missing decode or value; and the v5 seam is stated as *both* facts with
   the open cross-page divergence flagged for Part-16 (§4.1 CORRECTION), not silently picked.
   **Survives.** `[HIGH/OBSERVED on the walls; CORRECTION-flagged on the ct37 divergence]`

**Single strongest CORRECTION:** the `coretype 37` / `arch_id 36` v5 seam is **not** one fact but
two on opposite sides of the OBSERVED/INFERRED line, and the pages diverge on how to label the v5
surface. `coretype 37` is byte-OBSERVED *in the IMG/GEN getter binaries* (a live `case 37` jump arm
and a `cmp $0x25` gate); `arch_id 36` is **INFERRED** (doubly — it rests on `coretype − 1` *and* the
`NX_TOPSP = arch_id` cross-check fails for v5). The NCFW page, owning the v5 file-absence, frames the
whole v5 region as "INFERRED-or-absent" because its own binary carries no v5 arm. The one hard fact
all pages agree on is that **the v5 NCFW image is FILE-ABSENT**. This ledger states both halves and
flags the labeling divergence for the Part-16 reconcile rather than resolving it silently.

---

## 6. Cross-references

This page is the completeness anchor; it cross-links the verdict it feeds, the two companion
registers, and every lane's owning synthesis page.

**The anchor and its companions:**

- [The Reimplementation Verdict & Open-Questions Map](../orientation/verdict-and-open-questions.md)
  — the navigable verdict this ledger is the long-form accounting behind; it quotes the §1 headline
  fractions.
- [The Confidence & Walls Model](../reference/confidence-model.md) — the `[CONF/PROV]` tag system
  every cell uses and the named-wall closability taxonomy (§3).
- [The Open-Questions Register](open-questions-register.md) — the per-residual
  *why-unreachable / what-would-close-it*, partitioned by closability; the companion register to this
  coverage accounting.

**Per-lane owning synthesis pages** (one per row of §2):

- ISA — [coverage tally](../isa/core/coverage-tally.md) · [semantics coverage ledger](../isa/semantics/coverage-ledger.md) · [formal ISA model](../isa/semantics/formal-isa-model.md)
- ISS — [oracle synthesis](../iss/iss-oracle-synthesis.md) · [semantic synthesis](../iss/iss-semantic-synthesis.md)
- VAL — [the per-family pass/fail capstone](../validation/capstone-matrix.md)
- HW — [microarch synthesis](../uarch/microarch-synthesis.md)
- FW — [opcode-catalog ledger](../firmware/kernels/opcode-catalog-ledger.md)
- IMG — [firmware-image catalog](../images/firmware-image-catalog.md)
- GEN — [master capability matrix](../generations/master-capability-matrix.md)
- ABI — [ABI synthesis](../abi/abi-synthesis.md)
- RT — [runtime synthesis](../runtime/runtime-synthesis.md)
- DMA — [data-movement reference](../dma/data-movement-reference.md)
- CCL — [collective architecture synthesis](../collectives/ops/architecture-synthesis.md)
- NCFW — [LX-ISA / naming / arch-id synthesis](../collectives/ncfw/lx-isa-naming-archid-synthesis.md)
- NEFF — [format reference](../neff/format-reference.md)
- CC — [dtype/engine fan-in synthesis](../compiler/dtype-engine-fanin-synthesis.md)
- STRUCT — [struct-census overview](struct-census-overview.md) · [exec-state census](struct-exec-state-census.md)
- CSR — [notific/SDMA residual](../control/csr/notific-sdma-residual.md)
- ADDR — [unified SoC memory map](../control/address/unified-soc-memory-map.md)
- INT — [NSM flow unified](../control/interrupt/nsm-flow-unified.md)
- SEC — [security synthesis](../control/security/security-synthesis.md)

No silicon-generation, gen-count, or codename is inferred as a hard fact anywhere on this page:
v2–v4 (SUNDA / CAYMAN / MARIANA / MARIANA_PLUS) are byte-grounded and execution-validated; v5
(MAVERICK) and v1 (TONGA) are header-OBSERVED + bounded-INFERRED, with the v5 NCFW image stated as
the file-absent hard fact and every v5 interior flagged INFERRED.
