# The Gen-Invariance Thesis

This is the argument that makes the rest of the book generation-portable. Everywhere else
the guide decodes *one* Vision-Q7 device image, names *one* opcode body, or executes *one*
value leaf — and almost always against the Cayman (v3) artifacts, because they are the
byte-grounded reference SoC. This page is the proof that a reimplementer who builds **that
one machine** and then **parameterizes a short, bounded list of axes** has covered all five
NeuronCore generations — SUNDA (v2), CAYMAN (v3), MARIANA (v4), MARIANA_PLUS (v4+), and
MAVERICK (v5) — not just the one that was decoded.

The thesis has a precise shape, and the precision is the point. It is **not** "the five
generations are similar." It is: **the GPSIMD core is one frozen Vision-Q7 `Cairo` config,
built once, instantiated identically across the line; the per-generation differences are an
enumerable set of *additive table extensions and engine re-shapes* sitting around that core,
not changes to the core itself.** Most of the machine is a constant; the variation is a
parameter vector with named, OBSERVED entries.

Every claim below carries a `[CONF / PROV]` tag per
[The Confidence & Walls Model](../reference/confidence-model.md), and the page enforces that
model's generation-grounding policy without exception: **v2–v4 are byte-grounded; v5 /
MAVERICK is header-OBSERVED only, and every v5-interior invariance claim is flagged INFERRED.**
You cannot prove the MAVERICK core *interior* is identical to Cayman's — only that its
identity surface matches the stride. That wall is carried on every v5 line, never elided.

> **How to read the two ledgers.** Ledger **A** is the invariant core — the things a
> reimplementer builds *once* and never re-derives per generation. Ledger **B** is the
> scaling vector — the bounded axes that *do* move, each with its per-generation delta and
> anchor. The verdict (§3) is the equation: **A + parameterize(B) = all five generations.**

---

## 1. The thesis, stated precisely

The five unified-NeuronCore GPSIMD generations are **one Cadence Tensilica Vision-Q7 NX DSP
in the `Cairo` microarchitecture, config `Xm_ncore2gp` (CoreID `ncore2gp`), HW revision
NX1.1.4 (= 281040 = RI-2020.4 = LX7.1.4), ISA family `Xtensa24` (XEA3)** — embedded eight
times as the per-NeuronCore POOL cluster, reused across every generation in the line. There
is exactly **one** core configuration in the entire corpus: one `core-isa.h`, one real
`-params`, one `ConfigName`, one `uarchName`. No second Xtensa core config ships anywhere.
`[HIGH/OBSERVED — `fd --no-ignore` finds exactly one of each across the corpus]`

The differences between generations partition cleanly into three buckets:

1. **INVARIANT** — the core identity, the FLIX encoding, the register files, the decode and
   value-semantics models, the dispatch mechanism. These are properties of the Vision-Q7
   **IP**, not the GPSIMD generation. They are constant across v2–v5. (Ledger A.)
2. **SCALES** — a bounded set of *additive table extensions* (opcodes, dtypes, ALU ops,
   `struct2opcode` bindings, `kernel_info` entries) plus a handful of *engine re-shapes*
   (the MAVERICK ACT→DVE fold, the v4+ DGE fast-path, the transport re-IP). These are the
   parameter vector. (Ledger B.)
3. **ABSENT / WALLED** — what the corpus cannot witness: the v5 NCFW collective firmware
   image (file-absent), the v5 `arch_id` byte (no NCFW selector), the v5 core *interior*
   (header-OBSERVED only). Carried as walls, never fabricated. (§4.)

The single strongest anchor for the whole thesis sits in the shipped firmware itself: the
embedded Vision-Q7 device images for the four shipped generations all carry the **same**
toolchain `.comment` and the **same** ABI flag word. Twelve embedded Q7 ELF `.comment`
strings across the SUNDA/CAYMAN/MARIANA/MARIANA_PLUS offset region read exactly
`XtensaTools-14.09 clang version 10.0.1`, with ELF type `EXEC` and `e_flags 0x300`. One core,
one toolchain, one ABI, four generations. `[HIGH/OBSERVED — `strings -t` + `readelf` this
session: 12× the 14.09/clang-10 comment]` The MAVERICK blobs are the exception that proves
the rule — a *rebuild*, not a *redesign* (§4).

> **NOTE.** "Cairo" is the Tensilica *microarchitecture* codename; SUNDA / CAYMAN / MARIANA /
> MARIANA_PLUS / MAVERICK are the AWS *silicon* codenames. They live in different namespaces
> (the `uarchName` config field vs the `NRTUCODE_CORE_*` enum / `*.c` source strings). The
> correct framing is "the Cairo core *inside* the CAYMAN SoC", never "Cairo == CAYMAN". The
> full mapping is the [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md).

---

## 2. The two ledgers

### Ledger A — INVARIANT across v2–v5 (the core a reimplementer builds once)

Each row is a property of the Vision-Q7 Cairo IP that does **not** change with the GPSIMD
generation. The anchor is the symbol / offset / byte where it is read; the confidence tag is
the strength of that read. For v5 specifically, the **identity surface** is OBSERVED (the
symbol prefix, the coretype, the toolchain comment), but the **interior** behind it is
INFERRED — flagged per row.

| # | Invariant | What it is | Binary anchor | Confidence |
|---|---|---|---|---|
| A1 | **Core identity** (`ncore2gp` / `Cairo` / NX1.1.4 / `Xtensa24` / ConfigID `0xC4019686`/`0x2908E4E3`) | One frozen Vision-Q7 NX config, one HW revision (MIN==MAX==281040), one registered core | `ConfigName = Xm_ncore2gp`, `uarchName = Cairo`, `arch = Xtensa24`, `TargetHWVersion = NX1.1.4` in `ncore2gp-params` (lines 19–25); the sole core in `xtensa-elf-objdump` | **HIGH / OBSERVED** (shipped-4); v5 core-config **not shipped** → its NX-rev is **INFERRED** (§4) |
| A2 | **The FLIX encoding** — 14 formats / 46 slots; 7 length-classes → 4 byte-sizes `{2,3,8,16}` | The VLIW bundle scheme the whole ISA uses; a property of `libisa-core`, not any generation | `num_formats` @ `0x3b65e0` = `mov $0xe,%eax`; `num_slots` @ `0x3b6510` = `mov $0x2e,%eax`; `length_table[256]` @ `0x3d4100` in `libisa-core.so` (byte-verified) | **HIGH / OBSERVED** |
| A3 | **The 8 register files** (`AR`,`BR`,`vec`,`vbool`,`valign`,`wvec`,`b32_pr`,`gvr`) | 2 core/scalar + 6 Vision-Q7 SIMD coproc files; the ABI-relevant register block | `num_regfiles` @ `0x3b5c20` = `mov $0x8,%eax`; `regfiles` table @ `0x74a800` (stride 56) in `libisa-core.so` | **HIGH / OBSERVED** |
| A4 | **The libisa decode model** — opcode / iclass / operand / slot / field tables; 1 `coproc` (`Vision`) | The canonical ISA decode model is one host-side roster (1534 Q7 mnemonics, 12569 mnemonic×slot placements) for every generation | the `opcode/proto/ctype/regfile/iclass/operand/slot/decode` export namespaces in `libisa-core.so` (`8fe68bf4…`) | **HIGH / OBSERVED** |
| A5 | **The 864 `xdref` value semantics** | The per-element value functions of every GPSIMD value opcode — the bit-exact arithmetic the silicon computes | `nm libfiss-base.so \| rg -c module__xdref_` = **864**; callable in-process (proven-by-execution) | **HIGH / OBSERVED (by execution)** |
| A6 | **The 64-byte instruction word** | `NEURON_ISA_TPB_INST_NBYTES = 64` — every operand struct is 64 B, every generation incl. TONGA | every `S4D4_*`/`S3D3_*`/`S3_LW`/extended-family operand struct `sizeof == 64` (gcc `_Static_assert`, compile-verified all gens) | **HIGH / OBSERVED** |
| A7 | **The dispatch mechanism** — SEQ ASCII-opcode sequencer + the `0xF0` ExtendedInst NX→Q7 bridge + the `engine_idx` enum | The control-core dispatch model: one 18-handler SEQ intersection; only POOL has the `0xF0` escape (hence POOL is the sole dual-core engine); `engine_idx` = {PE 0, ACT 1, POOL 2, DVE 3, TPB_SP 4, TOP_SP 5} | the `cayman/seq` dispatcher retained on every gen incl. MAVERICK; `NEURON_ISA_TPB_NEURON_ENGINE` enum in every gen's header; `globstruct` magic `0x6099cb34` byte-identical SUNDA/CAYMAN/MARIANA/MAVERICK | **HIGH / OBSERVED** (v5 dispatch-table membership OBSERVED; v5 *handler-body* interiors **INFERRED**, §4) |
| A8 | **The collective-reducible dtype set** — `{BF16, FP16, FP32R, FP8_E3/E4/E5}` (6, fixed) | The dtypes a collective can *reduce* (not merely copy) never grows across the line — a flat invariant inside the otherwise-scaling dtype axis | `cce_dtypes` @ host `0x9b9f40`; the 9 collective pseudo-ops + `COLLECTIVE_TYPE`/`CCE_OP`/`LNC_SIZE_FMT` enums byte-identical all gens incl. maverick header | **HIGH / OBSERVED** |

> **QUIRK (A8 inside B-territory).** The *compute* dtype set scales hard (16→30, Ledger B2),
> but the *reducible* subset is a constant 6. A reimplementer must not assume the wider
> superset is reducible — the extra codes are copy-able byte movement only. This is the one
> place where an invariant lives *inside* a scaling axis.

> **GOTCHA — A2/A4/A5 anchors live in the toolchain package, not the customop package.**
> `libisa-core.so` and `libfiss-base.so` are under `TOOLS/ncore2gp/config/`, not in the
> `libnrtucode*` runtime libs. They are the *Vision-Q7 model itself*, which is precisely why
> they are gen-invariant: there is one model, shared by every generation's firmware. See the
> [Corpus, Tiers & Binary Inventory](../reference/corpus-inventory.md).

### Ledger B — SCALES per generation (the bounded parameter vector)

Each row is an axis that **does** move, with the per-generation delta and the anchor. The
shape of every axis is the same: **additive** (codes are reserved — a code never changes
meaning; generations only *add*) with two structural re-shapes at the v5 edge. MARIANA_PLUS
is the degenerate case — it scales the *identity* axis (+8 coretype slot) and adds exactly
one firmware feature, but is byte-identical to MARIANA on every ISA/dtype/collective axis.

| Axis | SUNDA v2 | CAYMAN v3 | MARIANA v4 | MARIANA_PLUS v4+ | MAVERICK v5 | Anchor / shape |
|---|---|---|---|---|---|---|
| **B1 · `struct2opcode` (opcode→struct bindings) / `kernel_info` growth** | **89** | **99** | **108** | ≡ MARIANA (108) | **114** | `jq '.struct2opcode \| length'` on each `neuron_<gen>_arch_isa/tpb/instruction_mapping.json` (`struct2pseudo_opcode = 2` all gens). Monotone, additive. `[HIGH/OBSERVED]` |
| **B2 · DTYPE bracket** (`NEURON_ISA_TPB_DTYPE`) | **16** | 16 (≡ SUNDA) | **24** (+FP4_EXP2, +CPTC1..7, +DTYPE_BASIC alias, +MXTENSOR1D) | ≡ MARIANA (24) | **30** (+FP8_EXP2, +INT4, +SFP8_E8..E5, +MXTENSOR_V2) | the `NEURON_ISA_TPB_DTYPE` enum in each gen's `common.h`. Strict superset chain. `[HIGH/OBSERVED]` |
| **B3 · ALU_OP count** (`NEURON_ISA_TPB_ALU_OP`) | **33** | **60** (+27 INT/UINT variant family) | **64** (+ABS_MAX/ABS_MIN/RE_LU/SQUARE) | ≡ MARIANA (64) | **65** (+SYMMETRIC_CLAMP) | the `NEURON_ISA_TPB_ALU_OP` enum body. `[HIGH/OBSERVED]` |
| **B4 · OPCODE enum** (`NEURON_ISA_TPB_OPCODE`) | **145** | **150** | **159** | ≡ MARIANA (159) | **165** | the `NEURON_ISA_TPB_OPCODE` enum body. `[HIGH/OBSERVED]` |
| **B5 · coretype / arch_id stride** | ct **6** / ai **5** (`0x05`) | ct **13** / ai **12** (`0x0c`) | ct **21** / ai **20** (`0x14`) | ct **29** / ai **28** (`0x1c`) | ct **37** / ai **36\*** (**INFERRED**) | `coretype` switch @ `0x9b2b30` in `libnrtucode_internal.so` (cases 6/13/21/29/37); `arch_id` = the `libncfw_get_image` `cmpl` ladder `{0x05,0x0c,0x14,0x1c}`, `ja` default > `0x1c`. The uniform relation is `coretype = arch_id + 1` (the `coretype` deltas are **+7 then +8, +8, +8** — *not* a flat +8). `[ct HIGH/OBSERVED; arch_id 36* MED/INFERRED]` |
| **B6 · EXTISA blob set** | 1 (`EXTISA_0`, weak-undef) | 4 (`EXTISA_0..3`, own compile) | 4 (distinct compile, same opcode contract) | 4 (**byte-identical to MARIANA**, adds 0 new EXTISA bytes) | 4 (internal-twin only; ET_DYN/PIC, clang-15) | the 5 per-gen `*_libs` tables @ `sunda_libs 0x9b8f80 < cayman 0x9b8f90 < mariana 0x9b8fd0 < mariana_plus 0x9b9010 < maverick 0x9b9050`; opcode contract `{17,1,2,9}` entries identical across the 4 shipped gens. `[HIGH/OBSERVED]` |
| **B7 · on-chip collective uplift (SB2SB `0xBF`)** | **ABSENT** (cross-die RDMA + P2P only; the reduced baseline) | **0xBF present** (on-chip S3D3 reduce-copy) | 0xBF (≡ CAYMAN) | 0xBF (≡ MARIANA) | 0xBF (sync primitive re-modeled, §B8) | `rg SB2SB_COLLECTIVE` in each gen's `common.h` → SUNDA absent; cayman/mariana/maverick = `0xbf`. `[HIGH/OBSERVED]` |
| **B8 · engine folds & re-shapes** | 5 NX engines | 5 NX engines | 5 NX engines | 5 NX engines (+ DGE fast-path firmware feature) | **ACT folded into DVE** (no `NX_ACT` image); MX unified into Matmul/MXTensorV2; DGE fast-path **dropped**; SYNC re-modeled (EVENT family dropped, SEM_*_REG_OFFSET/UNORDERED/NONBLOCKING added) | the engine-image getter census per gen; `dge_reshape_memcopy_transpose_fast` strings present v4+ only; `NX_ACT` getter count = 0 on MAVERICK. `[HIGH/OBSERVED; the v5 sync re-lowering MED/INFERRED]` |
| **B9 · transport / memory re-IP** | SDMA, SerDes-d2d (Cayman-class) | SDMA, DWC-PCIe/SerDes d2d | SDMA, abstracted d2d | ≡ MARIANA (Cayman-class) | **SDMA→DDMA/CDMA/UDMA; SerDes→native UCIe; HBM doubled (273→512 PHY); flat→explicit-3-die** | the Maverick `al_address_map_db.pkl` (`"SDMA" keyword = 0`); the v5 UCIe link descriptors. A *generation swap*, not a superset. `[HIGH/OBSERVED]` |
| **B10 · build / toolchain epoch** | 14.09 / clang-10 / EXEC | 14.09 / clang-10 / EXEC | 14.09 / clang-10 / EXEC | 14.09 / clang-10 (recompile, not independent build) | **15.05 / clang-15.0.7 / ET_DYN** | the embedded Q7 ELF `.comment`: **12×** `XtensaTools-14.09 clang version 10.0.1` (the 4 shipped gens) + **4×** `XtensaTools-15.05 clang version 15.0.7` (the 4 MAVERICK blobs). `[HIGH/OBSERVED]` |

> **CORRECTION — the `0x1c`/`0x14` ladder is *unordered*; read the join, not the order.**
> The `libncfw_get_image` `cmpl` ladder tests `0x1c` first, then a `ja` guard, then `0x14`,
> `0x05`, `0x0c`. A prior cross-reference wave mis-paired the labels reading the ladder
> top-down. The canonical pairing is triple-anchored (blob-address order, `*.c` string
> order, increasing `.rodata` offsets all agree): `0x05=v2=SUNDA`, `0x0c=v3=CAYMAN`,
> `0x14=v4=MARIANA`, `0x1c=v4+=MARIANA_PLUS`. The codename-crosswalk §2 carries the full
> proof.

> **NOTE — the only *removal* in the whole chain.** SUNDA → CAYMAN dropped
> `CUSTOM_OP_HEADER` / `CUSTOM_OP_PAYLOAD` from `struct2opcode` (the custom-op header/payload
> pair). Every other step is strictly additive. The B1 climb 89→99 is therefore +12 / −2,
> not +10; the table figure is the net count. `[HIGH/OBSERVED]`

> **GOTCHA — B9 does not transfer.** A Cayman-class d2d/SerDes/HBM fact is **not** a MAVERICK
> fact. The DMA/transport axis is the one place the additive model breaks: v5 is a re-IP
> *swap* (DDMA/CDMA/UDMA + UCIe + 3-die), not an extension of the SDMA family. Treat
> SUNDA..MARIANA_PLUS as one transport family and MAVERICK as a separate one.

---

## 3. The verdict — A + parameterize(B) covers all five generations

The thesis reduces to an equation a reimplementer can act on directly:

**Build Ledger A once** — the Vision-Q7 Cairo core, the 14-format/46-slot FLIX decoder, the
8 register files, the libisa decode model, the 864 xdref value semantics, the 64-byte word,
the SEQ/`0xF0` dispatch — **and you have built the part that is identical on every
generation.** None of it is re-derived per generation; it is the constant.

**Then parameterize Ledger B** — feed the per-generation `struct2opcode` count, the dtype
bracket, the ALU_OP/OPCODE counts, the coretype/arch_id pair, the EXTISA blob set, the SB2SB
presence, the engine-fold shape, the transport family, and the toolchain epoch as a
**ten-entry parameter vector** — and you have specialized that one core to any of the five
generations. The parameter vector is small, OBSERVED, and (B9 aside) additive.

The decisive structural fact that makes this hold: the per-generation `neuron_<gen>_arch_isa`
headers that *look* like they differentiate the core are the **TPB tensor-engine** operand
ISA (the opcode/struct/dtype tables — Ledger B), **not** the Xtensa core ISA. The Xtensa core
ISA is the gen-invariant `Xtensa24`/Vision-Q7 model in `libisa-core.so` (Ledger A). The
guide's deep decode work — done once against Cayman — is therefore portable by construction:
the encoding cover and the value semantics are core-IP properties; only the table extents
move. `[HIGH/INFERRED — this is the synthesis the whole guide rests on; it is a thesis over a
large body of OBSERVED facts (A1–A8, B1–B10), not a single read]`

A worked picture of the parameter vector, oldest → newest:

```
              B1     B2    B3    B4    B5(ct/ai)   B6     B7      B8           B10
              s2o    dtype alu   op    core/arch   extisa SB2SB   engines      build
  SUNDA  v2    89     16    33    145   6 / 5       1      —       5 NX         14.09/clang-10
  CAYMAN v3    99     16    60    150  13 /12       4      0xBF    5 NX         14.09/clang-10
  MARIANA v4  108     24    64    159  21 /20       4      0xBF    5 NX         14.09/clang-10
  M_PLUS v4+  ≡v4    ≡v4   ≡v4   ≡v4   29 /28       ≡v4    0xBF    5 NX +DGE    14.09/clang-10
  MAVERICK v5 114     30    65    165  37 /36*      4      0xBF    ACT→DVE      15.05/clang-15
              └──────── additive (codes reserved) ────────┘     └ re-shapes ┘  └ rebuild ┘
```

MARIANA_PLUS is the cleanest illustration of the thesis: it is a **distinct codename at the
identity layer** (its own coretype 29 / arch_id 0x1c / NCFW v4+ image / `*_libs` table /
register-map dir) but a **feature-flag delta on the MARIANA silicon ISA** everywhere else —
its EXTISA blobs and NCFW DRAM are byte-identical to MARIANA, and it has no
`neuron_mariana_plus_arch_isa` dir at all. It adds exactly one firmware feature (the DGE
reshape fast-path) and zero ISA bytes. It is the narrowest step in the line and the proof
that the identity axis (B5) and the capability axes (B1–B4) move independently. `[HIGH/OBSERVED]`

---

## 4. The v5 walls — what the thesis cannot prove for MAVERICK

The thesis is **byte-grounded for v2–v4** and **header-OBSERVED-only for v5**. The
distinction is decisive and is carried on every MAVERICK line above. What is OBSERVED
for MAVERICK is the *identity surface*; what is INFERRED or absent is everything *behind* it.

**What is OBSERVED for MAVERICK** `[HIGH/OBSERVED]`:

- **coretype 37** — anchored two independent ways: the `NRTUCODE_CORE_*` header enum ordinal
  (`29 + 8 = 37`), and the twin resolver immediates (`cmp $0x25` (= 37) with the bit-37
  bitmasks `0x2020202000`/`0x2020202040`). The `cmp $0x25` site is present in
  `libnrtucode_internal.so` (re-disassembled this session).
- **the identity census** — the five per-gen `*_libs` symbols including `maverick_libs` @
  `0x9b9050`; the toolchain comment `XtensaTools-15.05 clang version 15.0.7` (4 blobs);
  ET_DYN type; the same `e_flags 0x300` Xtensa core/ABI flag word as the shipped four.
- **the header surface** — the `neuron_maverick_arch_isa` set, self-labeled "ISA header for
  NC-v5", with `struct2opcode 114`, DTYPE 30, ALU_OP 65, OPCODE 165 (all re-counted).
- **the internal-twin-exclusivity** — re-counted this session: **187 `MAVERICK`** + **2
  `maverick`** literal occurrences (= **189** case-insensitive lines) in
  `libnrtucode_internal.so`, and **0** in the shipped front `libnrtucode.so`.

> **GOTCHA — the "189×" maverick count is a case-insensitive line count, not a literal
> occurrence count.** `strings $INT \| rg -ic maverick` = **189**; the precise breakdown is
> **187 uppercase `MAVERICK`** (`rg -o MAVERICK \| wc -l`) **+ 2 lowercase `maverick`**. The
> spot-check figure "187/0" some pages cite is the uppercase-only `rg -o` count. Ground any
> MAVERICK count claim to one of these exact forms; do not cite "189" as if it were a literal
> string count. `[HIGH/OBSERVED — re-counted three ways this session]`

**The named v5 walls** (per the [Confidence & Walls Model](../reference/confidence-model.md)):

- **`arch_id 36*` — INFERRED, closable-with-corpus.** There is **no** firmware byte for it.
  The only `arch_id`-keyed structures are the two NCFW selectors, whose ladders compare
  *exactly* `{0x05,0x0c,0x14,0x1c}` with a `ja` default — there is no `cmp $0x24` and no
  `cmp $0x25` anywhere in the NCFW selector. `arch_id 36 = coretype − 1` is the stride
  extrapolation only. Mark it `36*`; never present it as binary-observed. `[MED/INFERRED]`
- **v5 `Q7_CC_TOP` collective firmware — FILE-ABSENT, closable-with-corpus.** MAVERICK ships
  **no** NCFW image: `libncfw` carries exactly eight blob symbols
  `{v2,v3,v4,v4_plus}×{iram,dram}`, the region physically closed by `__GNU_EH_FRAME_HDR`
  immediately after. `libncfw_get_image` tops out at MARIANA_PLUS. The v5 collective
  orchestration layer is not in this corpus. (MAVERICK still *has* collectives — it does them
  the same on-engine `0xBF` way; what is absent is the NCFW management layer, not the
  feature.) `[HIGH/OBSERVED on the absence — the interiors are not observable]`
- **the v5 core interior — INFERRED, not OBSERVED.** MAVERICK's *own* core config (its NX
  revision, its `uarchName`) does **not** ship. The toolchain bump to 15.05/clang-15 is
  OBSERVED; the `e_flags 0x300` match is *suggestive* of the same Xtensa core family, but it
  does not pin the exact NX revision. Whether MAVERICK's Q7 is still NX1.1.4/Cairo recompiled
  or a newer revision **cannot be determined from this corpus**. Every Ledger-A row that
  asserts an *interior* property of the MAVERICK core (A2 decode tables, A5 value semantics,
  A7 handler bodies) is therefore an INFERRED extrapolation from the shipped four — sound on
  the stride, but never byte-proven for v5. `[shipped-4 HIGH/OBSERVED; MAVERICK interior
  MED/INFERRED — a toolchain rebuild is OBSERVED, a core redesign is neither proven nor
  ruled out]`

The honest one-line summary of the v5 footing: **MAVERICK is a rebuild of the same core
family with an extended table set and two engine re-shapes — OBSERVED at its identity surface,
INFERRED in its interior, and walled (file-absent) at its NCFW layer.** Do not fabricate a v5
silicon part-binding, a v5 collective firmware, or a v5 `arch_id` byte that was not read.

> **The TONGA caveat.** TONGA (legacy NC-v1, the "L" family / Inf1) is **not** a sixth GPSIMD
> generation and is **outside** the `coretype = arch_id + 1` family. It has no
> `coretype`, no `arch_id`, no NCFW image, no EXTISA blob, and a distinct 8-code
> `TONGA_ISA_TPB_DTYPE_*` enum family (a strict subset of the modern 16-code base). It shares
> the 64-byte word but uses per-mnemonic `*_INST` structs (`INST_EVENTS` 4 B) rather than the
> modern shape-coded `*_STRUCT` (`EVENTS` 8 B). Treat any `tonga` token as the legacy "L" ISA
> ancestor, never as a fifth-plus coretype.

---

## See also

- [Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md) — the canonical
  join table (coretype / arch_id / NC-ver / NCFW selector) Ledger B5 keys on, with the
  unordered-ladder correction proved row by row.
- [The Confidence & Walls Model](../reference/confidence-model.md) — the `[CONF/PROV]` tag
  system and the named v5 walls (`arch_id 36`, `ct37`, v5 `Q7_CC_TOP`) carried in §4.
- [The Corpus, Tiers & Binary Inventory](../reference/corpus-inventory.md) — where the
  Ledger-A anchors physically live (the `TOOLS/ncore2gp/config/` oracle DLLs vs the
  `libnrtucode*` runtime libs) and the per-gen header/`struct2opcode` counts.
- [Keystone Facts Reimplementers Get Wrong](keystone-facts.md) — K1 (GPSIMD is a stock
  Vision-Q7) and the FLIX/codename traps this thesis builds on.
- [FLIX Bundle-Decoding Methodology](../reference/flix-decoding.md) — the 14-format /
  46-slot / 7-length → 4-byte decoder of invariant A2.
- The Part-6 per-generation deep-dives — `generations/codename-generation-map.md`,
  `generations/sunda-v2-baseline.md`, `generations/mariana-plus-delta.md`,
  `generations/maverick-profile.md`, `generations/arch-isa-header-diff.md`,
  `generations/cross-gen-opcode-diff.md` — and the
  `generations/master-capability-matrix.md` appendix: the 15-subsystem × 5-generation grid
  this orientation page is the thesis over.
