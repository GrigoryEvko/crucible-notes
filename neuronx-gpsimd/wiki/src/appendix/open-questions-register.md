# The Open-Questions Register

> *This is the **exhaustive** companion to the orientation-level
> [Reimplementation Verdict & Open-Questions Map](../orientation/verdict-and-open-questions.md).
> Where that page is the navigable **map** — the defining subset a reader meets most often — this
> is the consolidated, categorized **ledger of every residual unknown across the full corpus**: the
> single document you open to ask "**what is STILL not known, WHY is it unreachable by static
> analysis, and WHAT concrete artifact / dynamic run / license / corpus would close it.**" Every
> residual is tagged by **closability** and by **confidence / wall**, pinned to the page that owns
> it, and stated at its *precise* current state — narrowed where it has been narrowed, never
> overstated.*

The tagging system (`[CONF/PROV]` and the closability classes) is the
[Confidence & Walls Model](../reference/confidence-model.md); the named-wall *defining subset* is
that page's §4. The per-axis coverage accounting behind the five headline fractions is the
[Coverage Ledger](coverage-ledger.md). This register is the
long-form *why-unreachable* + *what-would-close-it* for every row that ledger summarizes.

---

## 0. The headline thesis — read this first

> **NONE of the residuals in this register is a missing decode, a missing datapath, or a missing
> value-semantics.** `[HIGH/INFERRED]`
>
> The ISA **decode** is a certified-perfect, non-overlapping cover (1,534 / 1,534 shipped
> mnemonics, the canonical `libisa-core.so` encoder bit-exact). The **datapaths** are recovered —
> ~169/171 domain structs field-exact, every struct on the path a custom op travels. The **value
> oracle** is closed: all **864 / 864** `module__xdref_` value leaves in `libfiss-base.so` are
> resolved to a per-element function, and **~95%** are *proven-by-execution* against the live
> shipped simulator across ~2.09M differential comparisons with **zero firmware value bugs found**.
> This is the [VAL-lane capstone](../validation/capstone-matrix.md) verdict: **value semantics
> 100% known + ~95% execution-validated**.
>
> What survives is therefore *never* a hole in the machine. Every residual below is a
> **provenance / lineage / dynamic-state / v5-interior** question — a driver, a checkout, a license
> key, a runtime capture, a v5 firmware image, or a follow-on static pass away. The machine is
> recovered; what is walled off is a runtime input, an absent generation's image, an out-of-config
> core, a license-gated observable, or the *lineage* behind a table that is itself validated truth.

This thesis is not a flourish; it is the **falsifiable** governing claim of the whole effort, and
it is re-verified on three independent pages: the
[orientation map §B.3](../orientation/verdict-and-open-questions.md#b3-what-the-map-is-not), the
[Confidence & Walls Model §3](../reference/confidence-model.md#3-what-a-wall-is), and the
[capstone meta-finding §3](../validation/capstone-matrix.md). Across ~2.09M comparisons, **every**
apparent value divergence root-caused to the reference model (nki numpy), the analyst's lift/SEM,
the python harness, or an analyst's INFERRED reconstruction-form of a ROM — **never once** to the
firmware. The differential was anchored to the *executed* binary, so a misreading on the
*measuring* side could not masquerade as a firmware defect.

> **NOTE — what "closable" means here.** A residual is a **wall** only when the artifact that would
> answer it is *provably not in this corpus*, or the answer is *gated behind a runtime/license event
> no static read can trigger*. An unfinished decode of a present, decodable function is a **task**,
> not a wall, and does not appear in this register. The closability tag names the *one thing* that
> would cross each wall:
>
> | Closability | What crosses it |
> |---|---|
> | **closable-with-corpus** | A different/fuller checkout containing the missing artifact (a later firmware image, an absent host library, an unshipped disassembler config). A corpus boundary, not a knowledge boundary. |
> | **closable-with-license** | The capability is *present and runnable* in the shipped simulator but halts at a FlexNet check (`AUTH::check_iss_licenses`). A node-locked key crosses it; nothing else changes. |
> | **closable-with-hardware / runtime-capture** | A value that exists only at runtime — a host-loaded table payload, a soft-float dispatch object populated by a device round-trip, a per-cycle ordering. A captured payload or a device run crosses it. |
> | **closable-with-static** | A follow-on RE pass on the *binary already in hand* — present and drivable, only the analysis work unfinished. (A soft wall, listed for completeness.) |
> | **fundamental** | No static corpus of this subsystem can answer it: the thing asked for *does not exist in this subsystem* (a scope boundary), or the data is structurally absent and only a *bound* is recoverable. |

---

## 1. The consolidated register table

Every residual, grouped by **closability**, then by subsystem. Columns: the residual; its precise
current state; the owning page; **why static analysis cannot close it on this corpus**; the
**concrete closer**; and the **confidence / wall tag**. Literal `|` inside a cell is escaped `\|`.

The set is a **fully-named ledger of twelve open questions** (Q1–Q12) plus three carried
secondaries (Q12a / Q12c / Q12e) and the **four v5 firmware-internal walls** (W1–W4) the orientation
map carries on every Maverick claim — eighteen rows in total. None is a behavioral hole.

### 1.1 closable-with-corpus — a fuller/different checkout contains the artifact

| Residual | Precise current state | Owning page | Why static analysis can't close it | Concrete closer | Conf / wall |
|---|---|---|---|---|---|
| **W1 · `arch_id 36` (v5/Maverick identity)** | INFERRED as `coretype − 1`. The OBSERVED anchor is `coretype 37` (`ct37`); `arch_id 36 (0x24)` is the stride extrapolation. **No `cmp $0x24` exists** anywhere (re-confirmed: 0 matches in `libnrtucode_internal.so`); `libncfw`'s `get_image` ladder caps at `0x1c`. | [maverick-profile §1](../generations/maverick-profile.md) · [confidence-model §4.1](../reference/confidence-model.md#41-arch_id-36--inferred-maverick-v5-identity) | The byte that *states* the v5 `arch_id` lives in an **NCFW v5 image that is not in this checkout**. The stride `arch_id = coretype − 1` is invariant over four observed gens, but invariance is an inference, not a read. | **A `libncfw` checkout shipping the coretype-37 image** — it would byte-read the v5 `arch_id` and confirm `36` or correct it. | `ct37` `[HIGH/OBSERVED]`; `arch_id 36` `[MED/INFERRED]` · **closable-with-corpus** |
| **W2 · v5 Q7 geometry / CSR / run-stall / DKL** | INFERRED Cayman-class+. OBSERVED: 8× `Q7_POOL` + 4 EXTISA (62 getters, KIT carves); the v5 device-CSR headers (`xt_defines` / `xt_general_local_reg_defines` / `notification`) are **byte-identical to v4** (a *negative* OBSERVED). INFERRED: `{base_offset, IRAM/DRAM size, reserved tail, num-Q7 POOL/CC, POOL local-reg base}`. | [maverick-profile §7 (W2)](../generations/maverick-profile.md) · [codename-generation-map](../generations/codename-generation-map.md) | There is **no v5-specific device-CSR header to read** (it equals v4 byte-for-byte) and **no v5 KaenaHal slot** in libnrt 2.31.x. The geometry exists only in a v5 host HAL / firmware this checkout does not carry. | **The v5 KaenaHal / a v5 device-CSR delta** in a fuller host-runtime checkout. | `[INFERRED]` · **closable-with-corpus** |
| **W3 · v5 D2D transport (UCIe re-IP)** | INFERRED at the IP level. OBSERVED: the remote-capable descriptor model (`REMOTE_SEM_INC` / `REMOTE_COLLSYNC_INC`, `remote_core_id` in `DmaMemcpy2`); the firmware names the **DGE** dispatcher. CARRIED: the "UCIe 2nm chiplet / 7-entry D2D IP / `H_DIE_SCRATCHPAD`" PHY reading. | [maverick-profile §7 (W3)](../generations/maverick-profile.md) | Neither the v5 ISA headers nor the v5 customop firmware **name the PHY/IP**. The native UCIe transport lives in firmware images this checkout does not carry. | **A v5 firmware image / PHY-naming artifact** that binds the transport IP. | `[INFERRED · IP-level; CARRIED · PHY]` · **closable-with-corpus** |
| **W4 / Q8 · v5 `Q7_CC_TOP` collective firmware — FILE-ABSENT** | The Maverick collective top-sync image is **not in this corpus** — re-confirmed: `MAVERICK_Q7_CC_TOP*_get` = **0**, a genuine provisioning gap (parallel to SUNDA's missing `Q7_CC_TOP`). `libncfw_get_image` tops at MARIANA_PLUS. The v5-specific dispatch bodies + native-UCIe D2D live here; the **v4/v5-shared kernels ARE decoded via the Mariana images**. | [maverick-profile §1a/§7 (W4)](../generations/maverick-profile.md) · [collectives architecture-synthesis](../collectives/ops/architecture-synthesis.md) · [codename-generation-map](../generations/codename-generation-map.md) | The image is **absent from the artifact**, not unread. No amount of reading `libncfw` produces an image it does not contain (the ladder compares only `{0x05,0x0c,0x14,0x1c}`). The *absence* is itself a definitive OBSERVED fact. | **A fuller/later `libncfw` checkout shipping the coretype-37 collective image.** | absence `[HIGH/OBSERVED]`; interiors not observable · **closable-with-corpus** |
| **Q12a · FW-42 transcendental seed coefficient bytes — NARROWED** | The seed *tables* (`RECIP_Data8` / `RSQRT_Data8` and the recipqli QLI LUTs) are **PROVEN-BY-EXECUTION**: read from `.rodata` AND reproduced bit-exact by the live leaf over all 128 buckets × {fp16, fp32}, 0 mismatches — **the ROM is validated truth**. CARRIED is now exactly: (i) the literal **generator lineage**, and (ii) **two half-ULP boundary entries** where the recovered closed-form rounds one ULP off the ROM — **RECIP `i=127` (`0x81`)** and **RSQRT hi-range `idx=13` (`0xa5`)**, both re-confirmed against the binary this pass. | [transcendental-seed §7](../validation/transcendental-seed.md) · [capstone §4.2](../validation/capstone-matrix.md) | The validated *table* is everything a reimplementer needs (copy the bytes, correct by construction). What is unrecoverable is the **generator program / fixed-point rounding** that *produced* the bytes — it lives in the **out-of-carve FW-42 firmware driver**, not in `libfiss-base.so`. | **The FW-42 firmware / a wider `.rodata` carve** exposing the seed-table generator source. | table `[HIGH/OBSERVED·exec]`; lineage + 2 boundaries `[MED/CARRIED]` · **closable-with-corpus** |
| **Q2 · 7 uncited `nrtucode` prelink/loader functions** | Present and partly decoded; a **citation** residual only (IDA name-coverage 98.5% → 100%). Not a behavioral gap. | [runtime-synthesis](../runtime/runtime-synthesis.md) | The functions are *in-corpus and decodable* — this is a follow-on naming micro-pass, the softest possible wall. | **An in-corpus citation pass** over `libnrtucode_internal.so`. | `[HIGH/OBSERVED · structure; citation pending]` · **closable-with-corpus** |
| **Q7 · NCFW dense `op0=e/f` case-body interiors** | The scalar Xtensa-**LX** management core's ring (`0x3c..`) / hierarchical-barrier (`0x3e..`) step-schedule bodies are **read but MED** — the shipped `ncore2gp` disassembler is configured for the Vision-Q7 **FLIX** core and greedily mis-frames the LX `e/f`-dense bytes as Vision bundles. The 3-byte resync recovers the spine to HIGH; the dense interiors stay MED/OBSERVED. | [microarch-synthesis (NCFW LX)](../uarch/microarch-synthesis.md) | The NCFW's **own (unshipped) Tensilica LX disassembler config** never ships in this corpus. The bytes are observed; only the *naming* of the `e/f` leader ops is tooling-bounded — not a missing body. | **NCFW's own LX disassembler config** (a corpus item absent here). | spine `[HIGH/OBSERVED]`; dense interiors `[MED/OBSERVED]` · **closable-with-corpus** |
| **Q9 · raw core-kind vs ext-isa-id indirection** | Mechanism OBSERVED, labeling open. Two gates in `libnrtucode_internal.so` appear to share one generation axis with *different* id values — a raw core-kind `_bittest64` `{2,9,21,29,37}` vs a 32-case ext-ISA-id switch `{6,13,21,29,37}`. The bodies are decoded; that they are two encodings of one axis is a one-step inference with no third witness. | [codename-generation-map](../generations/codename-generation-map.md) | The two id schemes are both OBSERVED, but **no third in-corpus witness** confirms the labeling — a struct-lane cross-reference would, and it is in-corpus but unfound. | **An in-corpus struct-lane cross-reference** binding the two id schemes. | `[MED/INFERRED]` · **closable-with-corpus** |
| **Q11 · host collective-compose binaries** | The device half (`SB2SB 0xBF`, the CCE in-transfer reduce, the `0xC7`/`0xC8`/`0xD9` host triggers) is **re-OBSERVED in-checkout**. The host **SELECT / COMPOSE / EMIT** machinery (`libnccom` `findPath` / `EdgeRemoteMLA`; host `libnrt` 2.31.24.0) is **CARRIED** from prior reports — those binaries are **not in this checkout**. | [collectives architecture-synthesis](../collectives/ops/architecture-synthesis.md) | The host compose libraries are **absent from this checkout**; the carry crosses a checkout boundary (the cautionary MED/CARRIED case). | **The host collective-compose libraries (`libnccom` / host `libnrt`) in-checkout.** | device `[HIGH/OBSERVED]`; host `[MED/CARRIED]` · **closable-with-corpus** |

### 1.2 closable-with-license — present-but-gated cycle / fault / observable-trace

| Residual | Precise current state | Owning page | Why static analysis can't close it | Concrete closer | Conf / wall |
|---|---|---|---|---|---|
| **Q3 · DVE read-back ops `0x9b` / `0xe9`** | OBSERVED-only. The output is engine **state** a prior producer left — outside the `f(A,B)→R` value-leaf model — so there is **no value leaf to drive** (`nm \| rg -i read_state` = 0 in `libfiss-base.so`). The CSR-bridge analogue (`movscfv`) *was* driven live; the named state-read op carries no arithmetic. | [dve-read-state](../firmware/kernels/dve-read-state.md) · [confidence-model §6.1](../reference/confidence-model.md#61-the-differential-issval-lane-how-observed-becomes-proven-by-execution) | Reading hidden per-lane flops a *prior* producer wrote needs the **full-ISS cycle model** (producer → state → read-back differential) — runnable in `libcas-core.so` but gated. | **A FlexNet license** on the already-present `libcas-core` cycle model. | `[MED/INFERRED · named op; HIGH/OBSERVED·exec · bridge analogue]` · **closable-with-license** |
| **Q4 · license-gated cycle / fault / observable-trace oracle** | `libcas-core.so` loads, but instruction **retirement** hits `AUTH::check_iss_licenses` ("Unable to get license"). The cycle counts, the 124-slot fault machine, single-stepping, and observable trace are present-but-gated; the harness is already reconstructed. | [iss-oracle-synthesis](../iss/iss-oracle-synthesis.md) | The capability is **present and runnable** — it halts only at a FlexNet node-locked check no static read can satisfy. | **A FlexNet node-locked key** (nothing else needs to change). | `[present HIGH/OBSERVED; gated]` · **closable-with-license** |

### 1.3 closable-with-hardware / runtime-capture — a value that exists only at runtime

| Residual | Precise current state | Owning page | Why static analysis can't close it | Concrete closer | Conf / wall |
|---|---|---|---|---|---|
| **Q1 · the 3 recipqli soft-float QLI refine leaves (the sole value-leaf wall)** | **3 / 864 ≈ 0.35%.** Everything *under* the wall is proven: the 6-bit segment index (`shr $0x19` / `and $0x3f`, re-confirmed @0x87df20), the four QLI coefficient LUTs (read bit-exact, `base == ref`), the two-substage split, the device round-trip. The **full-context value leg** — the value-producing soft-float FMA on top — routes through a host dispatch object (`call *0x38(%rax)` ×2, re-confirmed @0x87e518/0x87e5e9, `%rax` from the saved `xstate`). A bare ctypes drive cannot populate it: **NULL and zeroed `xstate` both SIGSEGV (signal 11)**, fork-isolation-proven. The fp64 sub-stages (`__0`/`__1` @0x1b07e0/0x1b0870) tail-call the same body and inherit the wall. | [capstone §4.1](../validation/capstone-matrix.md) · [transcendental-seed §7.2](../validation/transcendental-seed.md) · [iss-oracle-synthesis](../iss/iss-oracle-synthesis.md) | The dispatch slot `0x38` is **populated only by a live device round-trip / a fully-instantiated ISS soft-float object** — it is NULL in any bare drive. This is a **driver gap, not a knowledge gap**: structure + tables + `base == ref` identity are all proven. | **A live `xstate` to drive recipqli** — either route a (the **licensed full ISS** single-stepping the leaf in cycle/turbo mode, reading the dest vreg via state introspection) or route b (the **FW-42 full-iteration driver / a device run** that exercises the dispatch object). | wall `[HIGH/OBSERVED·exec]`; end-to-end value `[MED/OBSERVED — CARRIED interior]` · **closable-with-hardware/runtime-capture** *(+ secondary closable-with-license via route a)* |
| **Q6 · host-loaded PWP activation coefficients** | The format is byte-exact; the per-function `{d0..d3, x0}` cubic **content** is **host-loaded** via the `0x23` table-load DMA — it is not resident in the firmware image. | [dve-read-state](../firmware/kernels/dve-read-state.md) · the ACT-engine table page | The coefficient *content* is a **per-model runtime payload**, never present in a static firmware read — only its loader and format are. | **A captured `0x23` table-load DMA payload** (primary), or **the host PWP generator** in a fuller checkout (secondary corpus route). | format `[HIGH/OBSERVED]`; content `[runtime-only]` · **closable-with-hardware/runtime-capture** |
| **Q12e · scatter-add per-cycle RMW interleave under collision** | The **value is bit-exact** — a commutative histogram sum (`collide_all → +32`), order-independent. Only the silicon's **exact per-cycle ordering** under collision is untraced, and it is **value-immaterial** (no value depends on it). | [descriptor-model](../dma/descriptor-model.md) | Per-cycle memory-port ordering exists **only at runtime**; static reads see the value, not the cycle interleave. It is observational, not corrective. | **A device run** observing the per-cycle RMW order (purely observational — the value is already certified). | value `[HIGH/OBSERVED·exec]`; ordering `[MED/INFERRED · value-immaterial]` · **closable-with-hardware** |

### 1.4 closable-with-static — a follow-on pass on the binary already in hand

| Residual | Precise current state | Owning page | Why static analysis can't close it *now* | Concrete closer | Conf / wall |
|---|---|---|---|---|---|
| **Q10 · the C16 FMA `_2` multi-output reassembly** | The leaf **executes live** and its components are observed; only mapping the **5-output decomposition** back to one bit-exact value is deferred. Not a knowledge gap — a drive-and-reassemble pass. | [four-oracle-method](../validation/four-oracle-method.md) | The artifact is **present and drivable**; only the analysis work (driving the 5 outputs and recombining) is unfinished. A soft wall. | **A follow-on drive pass on the shipped binary** reassembling the 5 outputs. | `[MED/OBSERVED — reassembly pending]` · **closable-with-static** |

### 1.5 fundamental — the artifact does not exist in this subsystem, or only a bound is recoverable

| Residual | Precise current state | Owning page | Why no static corpus of this subsystem can answer it | The sound substitute | Conf / wall |
|---|---|---|---|---|---|
| **Q5 · stochastic rounding** | There is **no RNG-seed value leaf** in the 864-leaf oracle (`nm \| rg -i 'iota\|seq'` = 0; no stochastic-rounding root). Stochastic rounding lives **outside the GPSIMD value datapath** entirely. | [four-oracle-method](../validation/four-oracle-method.md) | It is a **scope boundary** — the feature is **not in this subsystem**. No static read of GPSIMD can find a value function that does not exist here. | (none needed — it is correctly *absent*; do not fabricate an RNG leaf). | `[fundamental — scope boundary]` |
| **Q12c · empty `MODULE_SCHEDULE` per-port reservation matrices** | `1994 / 1994` `<MODULE_SCHEDULE>` matrices are **structurally empty** in the shipped XML. The class-level **1+1 FLIX co-issue ceiling IS recovered** (from the 1564-record `INSTR_SCHEDULE` table); only the *fine per-port single-issue reservation below it* is absent — because the matrix bodies are simply not in the file. | [microarch-synthesis](../uarch/microarch-synthesis.md) · [confidence-model §4.6](../reference/confidence-model.md#46-empty-module_schedule-reservation-matrices--fundamental-bound-is-the-substitute) | **Fundamental for the per-port claim**: no read of *this* XML produces bodies it does not contain. Honest absence, not unfinished work. | **The 1+1 ceiling + the FLIX-slot / per-format mul-capable-slot model** — the correct scheduler substitute. Do not fabricate a per-port matrix. | empty `[HIGH/OBSERVED]`; per-port `[LOW]` · **fundamental** |

### 1.6 anti-fabrication markers — walls that *aren't* (named to forestall invention)

| Marker | What it is | Owning page | The discipline |
|---|---|---|---|
| **SortMerge phantom** | **Named-but-never-shipped.** The only trace of "SortMerge" is a dead comment `// SortMerge wip 0x97` on the adjacent `0x98 = TENSOR_SCALAR_SELECT` line; `0x97` is the update-mode field `UPDATE_MODE_SEM_SUB_REG_COMPLETE`, not an opcode. **No struct, no body, no debug string** anywhere. Plain `SORT 0x96` is real and decoded. | [confidence-model §4.5](../reference/confidence-model.md#45-the-sortmerge-phantom--a-wall-that-isnt-named-to-forestall-fabrication) · [verdict-and-open-questions §B.2](../orientation/verdict-and-open-questions.md) | The absence is a **positive, byte-exact finding**. Do not invent a SortMerge opcode body from the leftover comment. `[HIGH/OBSERVED — absence is positive]` |

---

## 2. Roll-up by closability

The primary closability tag per residual (Q-rows and the four v5 walls; the v5 walls W1/W2/W3 are
the standing interiors the verdict map carries, and W4 ≡ Q8):

| Closability | Count | Residuals |
|---|---|---|
| **closable-with-corpus** | **9** | W1 (`arch_id 36`), W2 (v5 geometry), W3 (v5 D2D), W4/Q8 (`Q7_CC_TOP`), Q12a (FW-42 lineage + 2 boundaries), Q2, Q7 (NCFW LX), Q9, Q11 |
| **closable-with-license** | **2** *(+ secondary route on Q1)* | Q3, Q4 |
| **closable-with-hardware / runtime-capture** | **3** | Q1 (primary), Q6, Q12e *(value-immaterial)* |
| **closable-with-static** | **1** | Q10 |
| **fundamental** | **2** | Q5, Q12c |

The two **fundamental** rows are *not* unrecovered mechanisms: **Q5** is a scope boundary
(stochastic rounding is not a GPSIMD value-datapath feature) and **Q12c** is an absent-XML-body
residual whose practical bound — the FLIX-slot + per-format mul-capable-slot model — is already
sound. Every other row is a checkout, a key, a capture, or a follow-on pass away.
`[HIGH/INFERRED over the cited closers]`

---

## 3. The SCOPE-named residuals, each at its precise current state

The task SCOPE names a specific set of residuals; each is registered above, but they warrant an
explicit, **do-not-overstate** restatement because each is exactly the kind a downstream page is
tempted to widen.

### 3.1 The v5 firmware-internal walls (W1–W4)

Maverick (v5) is **header-OBSERVED only**; flag each interior on every v5 claim, never fabricate
across them. The orientation map develops these; here is the precise state of each.

- **`arch_id 36` is INFERRED, `ct37` is OBSERVED — do not conflate.** The OBSERVED anchor is
  `coretype 37` (the `cmp $0x25,%edi` range-guard + the `movabs $0x2020202000` bitmask setting bit
  37 + the `case 37 → maverick_libs` switch arm @0x9b9050 + the 62 `MAVERICK_*_get` accessors —
  all re-confirmed this pass). `arch_id 36 (0x24)` is the `coretype − 1` extrapolation; **there is
  no `cmp $0x24` anywhere** (re-confirmed: 0 matches), and `libncfw`'s `get_image` ladder routes
  `0x24` to the unsupported path. `[ct37 HIGH/OBSERVED; arch_id 36 MED/INFERRED]`

  > **CORRECTION — the `coretype` stride is irregular; only the `arch_id` relation is uniform.** Do
  > **not** assert a flat `+8` `coretype` stride. The observed `coretype` set is `{6, 13, 21, 29, 37}`
  > — that is **`+7` then `+8`, `+8`, `+8`** (6→13 is +7). What *is* uniform is
  > `arch_id = coretype − 1` (`{0x05, 0x0c, 0x14, 0x1c, 0x24}`), and it is that `−1` relation — not
  > any `coretype` stride — that extends the v5 `arch_id` to 36. A reimplementer reading "+8 stride"
  > off the `coretype` axis mis-derives the floor. (The maverick-profile §1 table that states a
  > "+8 coretype stride" is using the +8 that holds for 13→21→29→37; the 6→13 step is +7. Cite the
  > `−1` `arch_id` relation, not the `coretype` stride, as the invariant.)

- **v5 Q7 geometry — INFERRED.** The IRAM/DRAM geometry, the CSR bundle, the run-stall/DKL surface
  are the bounded extrapolation of the monotone v2–v4 scaling axes — **not a read of v5 firmware**.
  The structural reason it stays INFERRED: the v5 device-CSR headers are **byte-identical to v4** (a
  negative OBSERVED), so there is no v5-specific surface to read, and no v5 KaenaHal slot in libnrt
  2.31.x. `[INFERRED]`

- **v5 D2D transport — INFERRED at the IP level.** The remote-capable descriptor model is OBSERVED
  (`REMOTE_*_INC`, `remote_core_id`); the **PHY/IP is named by neither header nor firmware**, so the
  native-UCIe re-IP reading is CARRIED. `[INFERRED · IP-level; CARRIED · PHY]`

- **v5 `Q7_CC_TOP` collective firmware — FILE-ABSENT.** Re-confirmed: `MAVERICK_Q7_CC_TOP*_get` = 0.
  A genuine gap, not an unread region; the v5-specific dispatch bodies and native-UCIe D2D transport
  live in firmware images this checkout does not carry. **The v4/v5-shared kernels ARE decoded via
  the Mariana images** — only the v5-*specific* bodies are absent. `[absence HIGH/OBSERVED]`

- **v5-body decode (the FLIX-desync overlap).** Where a flat v5 NX region *is* present, the
  per-instruction bodies inside a FLIX-desynced span are read but tooling-bounded — table bases and
  string-anchored structures stay HIGH, the interior bindings sit at MED (the SX-FW-00 ceiling).
  `[wall HIGH/OBSERVED; bodies MED]`

> **QUIRK — the largest v5 structural delta is real and OBSERVED, not a wall.** The **ACT→DVE fold**
> (Maverick ships **no** `NX_ACT` image; the activation datapath is re-homed onto DVE, with
> `0x23`/`0x25` newly armed on the DVE PROF CAM and `ActivationReadAccumulator` → `DveReadAccumulator
> 0x9b`) is a *byte-grounded* finding, and the `QUANTIZE_MX 0xe3` engine-binding (DVE forward op; POOL
> hosts only the `0x7b` dequant) is **RESOLVED**, not open. These are *not* residuals — they are the
> v5 facts the residuals sit *between*. Do not file them as unknowns.

### 3.2 FW-42 seed coefficients — NARROWED to exactly 2 half-ULP boundaries (Q12a)

> **CORRECTION (the single most important narrowing in this register) — carry FW-42 as TWO bytes,
> not "the whole table."** An earlier framing held that the seed ROM's source coefficients are "not
> recoverable from this corpus," which a careless reader rounds up to "the seed table is unknown."
> **That is wrong and overstates the wall.** The transcendental-seed pass made both seed tables
> **PROVEN-BY-EXECUTION**: all 128 `RECIP_Data8` + 128 `RSQRT_Data8` bytes are read from `.rodata`
> AND reproduced **bit-exact** by the live `recip0`/`rsqrt0` leaf over all 128 buckets at **both**
> fp16 and fp32, **0 mismatches**. The `.rodata` table is OBSERVED / validated truth — a
> reimplementer copies those bytes and is correct by construction. What stays CARRIED is exactly:
> (i) the **literal generator lineage** (the program / fixed-point rounding that *produced* the
> bytes, in the out-of-carve FW-42 driver), and (ii) **two** entries where the analyst's recovered
> closed-form rounds one ULP off the ROM at a half-ULP boundary:
>
> | table | entry | shipped byte (re-confirmed) | closed-form | exact float | verdict |
> |---|---|---|---|---|---|
> | `RECIP_Data8` | `i = 127` | `0x81` (129) | `round(256/1.99609) = 128` | `128.2505` | **table = truth**, form CARRIED |
> | `RSQRT_Data8` | hi-range `idx = 13` | `0xa5` (165) | `164` | `164.4993` (near-tie) | **table = truth**, form CARRIED |
>
> So the wall moved from "the whole table is unverifiable lineage" to "254/256 bytes are
> closed-form-explained *and* execution-validated; 256/256 are execution-validated against the ROM;
> only 2 boundary roundings and the generator lineage are carried." The `.rodata` byte is the
> validated arbiter at both — the live leaf and the ROM agree; it is the *INFERRED closed-form* that
> misses by one ULP. This is **D13** in the divergence catalog, and it lived on the **measuring
> side, never the firmware**. `[HIGH/OBSERVED·exec — narrowing; CARRIED — 2 entries + lineage]`
> **closable-with-corpus** (the FW-42 driver / a wider `.rodata` carve).

### 3.3 The recipqli full-context leg (Q1)

The single value-leaf residual in the entire 864-leaf oracle. **3 of 864 ≈ 0.35%.** State it
precisely: everything *under* the wall is proven — the 6-bit segment extract, the four QLI
coefficient LUTs (`base == ref`, config-invariant), the two-substage split, the device round-trip.
Only the **value-producing soft-float FMA on top** is gated, because it routes through a host
soft-float dispatch object — `call *0x38(%rax)` ×2 where `%rax` derives from the saved `xstate`
(arg0), re-confirmed at @0x87e518 / @0x87e5e9. A bare ctypes drive cannot populate slot `0x38`:
**both NULL and zeroed `xstate` SIGSEGV (signal 11)**, fork-isolation-proven. The fp64 sub-stages
(`__0` @0x1b07e0, `__1` @0x1b0870) tail-call the fp32 body and inherit the wall.

> **NOTE — this is the SIGSEGV-on-zeroed-state full-context leg, a driver gap, not a knowledge gap.**
> The structure and tables are proven; only the live end-to-end value of three leaves is deferred.
> It is closable two ways: route a (the **licensed full ISS** single-stepping the leaf in
> cycle/turbo, reading the dest vreg via `InstDone` state introspection — `closable-with-license`)
> or route b (a **live `xstate` from a device round-trip / the FW-42 full-iteration driver** that
> populates the dispatch object — `closable-with-hardware`). The triage signature is exact: any leaf
> with `call *0x..(%rax)` where `%rax` came from `xstate` is heavy-leg-only — and `recipqli` is the
> **only** leaf in the entire oracle that carries it (fifteen seed-family leaves drive clean).

### 3.4 The FLIX-desync device interiors (corpus-wide MED ceiling)

The shipped device disassembler has `IsaMaxInstructionSize = 32` and desyncs on the 512-bit
FLIX/VLIW bundle stream: once mis-aligned, the cursor emits plausible-but-wrong per-instruction
decodes until it resynchronizes. **Everything *above* the desync line is HIGH** — table bases,
`kernel_info` entries, dispatch-table addresses, reset vectors, string-anchored structures, opcode
membership — because those are cursor reads at known addresses that do not depend on a linear sweep.
The per-*instruction* body bindings *inside* a desynced region are not byte-resolved.

> **NOTE — the methodology mitigates this without crossing it.** Encoding is closed independently on
> the certified-perfect ISA cover; value semantics are closed by *executing* the simulator. **Neither
> path walks the desynced linear stream.** The desync limits *narrative body-reading*, not the
> encoding or value closures — which is why the corpus-wide value-semantics claim is unaffected by
> it. The wall has *caught real errors* (two desync literals mis-read as branch targets, later
> corrected — the flag doing its job). `[wall HIGH/OBSERVED; interior bodies MED/OBSERVED]`
> **closable-with-corpus** (a FLIX-aware disassembler config). This overlaps Q12c (Maverick FLIX
> interiors are the secondary, image-gated sub-case).

### 3.5 The empty `MODULE_SCHEDULE` reservation matrices (Q12c)

`1994 / 1994` `<MODULE_SCHEDULE>` matrices are structurally empty in the shipped XML. The class-level
**1+1 FLIX co-issue ceiling IS recovered** (the 1564-record `INSTR_SCHEDULE` table); only the fine
per-port single-issue reservation *below* it is unrecoverable, because the matrix bodies are simply
not present.

> **GOTCHA — this is `fundamental` for the per-port claim, but the bound is sound. Do not fabricate
> a per-port matrix.** No read of *this* XML produces bodies it does not contain. The correct
> substitute for a reimplementer's scheduler is the **FLIX-slot + per-format mul-capable-slot model**
> under the 1+1 ceiling. `[empty HIGH/OBSERVED; per-port LOW]`

---

## 4. What this register is *not* (the meta-finding, restated)

It bears repeating because it is the whole point: **this register contains no missing decode, no
missing datapath, and no missing value semantics.** Read the table top to bottom and every residual
falls into one of six honest boundaries:

- a **driver** away — Q1 (recipqli soft-float dispatch);
- a **checkout** away — W1/W2/W3, W4/Q8, Q2, Q7, Q9, Q11, Q12a (corpus items absent here);
- a **license key** away — Q3, Q4 (present-but-gated cycle/fault observables);
- a **runtime capture** away — Q6 (host-loaded PWP content), Q12e (value-immaterial ordering);
- a **follow-on static pass** away — Q10 (the C16 reassembly);
- or **fundamentally out of scope** — Q5 (stochastic rounding), Q12c (per-port reservation).

That partition *is* the answer to the register's framing question. Across ~2.09M differential
comparisons the firmware value oracle had **zero** value defects; all thirteen catalogued
divergences (D1–D13) lived in the reference model, the lift, the harness, or an inferred
reconstruction-form — never the firmware. The value oracle is the arbiter, and the binary won every
time. The residuals are honestly-flagged *provenance / lineage / dynamic-state / v5-interior*
boundaries — the precise edge between what this reference proves today and what one key, one
checkout, or one capture would unlock tomorrow.

---

## 5. Adversarial self-verification — the five strongest claims, re-challenged

Each re-tested against the committed pages **and** the binary before publishing; a claim survives
only if a second independent witness agrees.

1. **"None of the residuals is a missing decode / datapath / value-semantics."** *Challenge:* could
   a residual secretly be a behavioral hole dressed as a provenance question? *Re-test:* the thesis
   is stated verbatim on three independent pages
   ([orientation §B.3](../orientation/verdict-and-open-questions.md), [confidence-model
   §3](../reference/confidence-model.md), [capstone §3](../validation/capstone-matrix.md)); the
   value oracle is closed at **864/864** (re-confirmed `nm \| rg -c module__xdref_` = 864) with
   ~95% execution-validated and **0 firmware value bugs** over ~2.09M comparisons; the encoding is a
   certified-perfect 1,534/1,534 cover; structs are ~169/171 field-exact. Every row in §1 names a
   *corpus / license / capture / static / scope* closer — not one names "a body we cannot decode."
   **Survives.** `[HIGH/INFERRED]`

2. **"FW-42 is narrowed to exactly two half-ULP seed boundaries, not the whole table."**
   *Challenge:* is this a real narrowing or a re-label? *Re-test:* both seed ROMs are read from
   `.rodata` (re-confirmed: `RECIP_Data8[127] = 0x81`, `RSQRT_Data8` hi-`idx=13` = `0xa5`, tails
   `…81 81 81` / `b4 b3 b2 b0`) **and** reproduced bit-exact by the live leaf (0 mismatches, both
   widths, [transcendental-seed §4](../validation/transcendental-seed.md)). 254/256 bytes are
   closed-form-explained and **256/256 are execution-validated**; only the generator lineage and the
   two boundary roundings stay CARRIED. The narrowing is *bounded and named*, not asserted.
   **Survives.** `[HIGH/OBSERVED·exec — narrowing; CARRIED — 2 entries + lineage]`

3. **"recipqli is the lone value-leaf wall; its full-context leg is the SIGSEGV-on-zeroed-state
   soft-float FMA."** *Challenge:* could other leaves also fault, or could recipqli be drivable with
   a different shape? *Re-test:* `nm` confirms exactly **3** recipqli leaves
   (@0x87df20/0x1b07e0/0x1b0870 — re-confirmed); `objdump` confirms `call *0x38(%rax)` at the two
   cited sites (@0x87e518/0x87e5e9) and the 6-bit seg-extract (`shr $0x19` / `and $0x3f`); NULL and
   zeroed `xstate` both SIGSEGV (fork-proven). All fifteen other seed-family leaves drive clean. The
   structure/tables/`base == ref` are proven; only the FMA value is gated. **Survives.**
   `[HIGH/OBSERVED·exec]`

4. **"`arch_id 36` is INFERRED; `ct37` is OBSERVED — the split is real."** *Challenge:* is 36 maybe
   byte-read after all, or 37 maybe also extrapolated? *Re-test:* `ct37` has the `cmp $0x25,%edi`
   range-guard + the `movabs $0x2020202000` bitmask (bit 37) + the `maverick_libs @0x9b9050` switch
   arm + **62** `MAVERICK_*_get$` accessors (re-confirmed this pass) — multiple independent
   witnesses. `arch_id 36` has **no `cmp $0x24`** anywhere (re-confirmed: 0 matches) and no v5 NCFW
   image (`MAVERICK_Q7_CC_TOP*_get` = 0). The split holds exactly. **Survives.** `[ct37
   HIGH/OBSERVED; arch_id 36 MED/INFERRED]`

5. **"v5 `Q7_CC_TOP` is FILE-ABSENT and the empty `MODULE_SCHEDULE` matrices are fundamental."**
   *Challenge:* could either be an unread-but-present region rather than a true absence? *Re-test:*
   `MAVERICK_Q7_CC_TOP*_get` = **0** (re-confirmed — a positive absence, parallel to SUNDA's missing
   `Q7_CC_TOP`); the `libncfw` ladder compares only `{0x05,0x0c,0x14,0x1c}`. The `MODULE_SCHEDULE`
   matrices are `1994/1994` empty *in the file* — no read produces bodies the file does not contain;
   the **1+1 ceiling from the 1564-record `INSTR_SCHEDULE` table** is the sound bound. Both are
   honest absences, not unfinished tasks. **Survives.** `[absence HIGH/OBSERVED; per-port LOW]`

All five survive re-challenge against the committed pages and the binary.

**Single strongest CORRECTION on this page:** carry **FW-42 as exactly two `.rodata` bytes**
(`RECIP_Data8[127] = 0x81`, `RSQRT_Data8` hi-`idx=13` = `0xa5`) **plus the generator lineage — not
"the whole seed table is unverifiable."** Both seed ROMs are execution-validated (read from
`.rodata` *and* reproduced bit-exact by the live leaf over all 128 buckets at both precisions, 0
mismatches); the recovered closed-form explains 254/256 bytes; the two that resist are half-ULP
boundary roundings where the **ROM byte is the validated truth and the closed-form is INFERRED and
wrong by one ULP**. A reimplementer ships the ROM bytes, not the formula, at exactly those two
indices, and is correct everywhere. Overstating this wall as "the seed table is unknown" is the
single easiest way to mis-rate a residual that is, in fact, 256/256 execution-validated.

---

## 6. See also

- [The Reimplementation Verdict & Open-Questions Map](../orientation/verdict-and-open-questions.md)
  — the **orientation** companion: the navigable map this register is the exhaustive long form of.
- [The Confidence & Walls Model](../reference/confidence-model.md) — the `[CONF/PROV]` tag system and
  the **reference** wall taxonomy (§3 closability, §4 the named-wall defining subset).
- [The Coverage Ledger](coverage-ledger.md) — the per-axis, per-lane coverage accounting behind the
  five headline fractions (the *what-is-covered* counterpart to this *what-is-not*).
- The residual-owning pages: [VAL — transcendental seed](../validation/transcendental-seed.md) (Q12a,
  the FW-42 narrowing) · [VAL — capstone matrix](../validation/capstone-matrix.md) (Q1, the recipqli
  wall; the value verdict) · [VAL — regfile-bridge / divergence catalog](../validation/regfile-bridge-divergence.md)
  (the D1–D13 catalog) · [MAVERICK (NC-v5) profile](../generations/maverick-profile.md) (W1–W4) ·
  [ISS oracle synthesis](../iss/iss-oracle-synthesis.md) (Q3/Q4, the license heavy leg) ·
  [collectives architecture-synthesis](../collectives/ops/architecture-synthesis.md) (Q11) ·
  [microarch synthesis](../uarch/microarch-synthesis.md) (Q7, Q12c) · [DMA descriptor model](../dma/descriptor-model.md)
  (Q12e) · [DVE read-state](../firmware/kernels/dve-read-state.md) (Q3, Q6).

Source: DX-SYN-09 (the consolidated residual synthesis); all constants re-confirmed against the
shipped binaries this pass (lawful interoperability RE — recovered symbols/strings are
binary-derived and citeable).
