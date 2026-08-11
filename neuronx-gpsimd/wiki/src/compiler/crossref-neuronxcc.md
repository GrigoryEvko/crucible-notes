# Compiler Cross-Reference to the neuronx-cc Wiki

This is the **bridge page**: it draws the line between *this* GPSIMD device guide and the
**sibling neuronx-cc compiler wiki** — a separate mdBook documenting the full host
compiler. This Part touches that compiler only at the **GPSIMD seam** (the
`emit_*`→BIR→SundaISel→opcode bridge and the MX device bodies); everything else — the
`hlo-opt`/`hlo2penguin` frontend, the Penguin middle-end, the `libwalrus` descriptor
backend in its entirety, the scheduler/memory planner — is **owned there**, and this page
*points* at it rather than re-deriving it.

It also adds **one new datum to the ISA model**: the neuronx-cc `isa_tpb`/`sunda` tables
are a **5th independent ISA source** for the TPB opcode roster, sitting alongside the four
the [TIE-database page](../isa/core/tie-database.md) already enumerates. That section is
the substantive content here; the rest is boundary-drawing.

> **Audience & scope.** A senior C++/LLVM engineer rebuilding a Vision-Q7-compatible
> GPSIMD engine, who needs to know *which* facts live in this book vs the compiler book,
> and *why* the compiler's ISA table is a legitimate fifth witness to the device opcode
> ledger. This page **points, it does not duplicate** — the owned `emit_*`/BIR/ISel/MX
> material is cross-linked, not restated.
>
> **Provenance.** Every fact derives from static analysis of the shipped, redistributable
> neuronx-cc `2.24.5133.0+58f8de22` wheel artifacts (the Cython `.so`, the native
> `libwalrus.so`, the plain-`.py`/`.pyi` stubs) and the in-book device ledgers cited
> inline. Recovered symbols, strings, and shipped tables **are** binary-derived and
> citeable. No external vendor source tree is referenced. Confidence is tagged per claim
> `HIGH/MED/LOW × OBSERVED` `/ INFERRED` (reasoned over OBSERVED) `/
> CARRIED` (read in a cited in-book page, reused).

```
CC = neuronx-cc/extracted/neuronx_cc-2.24.5133.0+58f8de22-cp311-cp311-linux_x86_64/neuronxcc/
```

cp310/cp311/cp312 wheels are byte-identical in the Cython layer; the cp311 copy is cited
throughout.

> **GUARD — the `sunda`/`tonga`/`cayman`/`mariana`/`maverick` axis is the codegen-TARGET /
> arch-ISA axis, not silicon.** `sunda`=CoreV2 (Trn1/Inf2 floor), `tonga`=Inf1 legacy,
> `cayman`=CoreV3/Trn2, `mariana`=CoreV4/Trn3, `maverick`=CoreV5/NCFW. The shipped
> `isa_tpb` table mined here is the **`sunda` floor**. No silicon-generation fact is
> inferred from any compiler descriptor. [HIGH/OBSERVED — the tokens appear only as
> directory names under `CC/isa_tpb/` and `CC/starfish/penguin/targets/`.]

---

## 1. The 5th independent ISA source — `isa_tpb`/`sunda`

The [TIE-database page §4](../isa/core/tie-database.md) certifies the Cairo (`ncore2gp`)
ISA from **four** independent shipped sources: **(1)** the `libisa-core.so` runtime decode
tables, **(2)** the `libtie-core.so` / `Xtensa.xml` TIE database (the only one carrying
bit-precise semantics), **(3)** the native `xtensa-elf-objdump` disassembler, and **(4)**
the device-side `custom_op/c10/include/*` arch-isa headers + `instruction_mapping.json`.

Sources 1–3 witness the **Vision-Q7 core ISA** (the 1534-opcode Xtensa FLIX scalar/vector
instruction set the GPSIMD POOL kernels are *written in*). Source 4 witnesses a **different
layer**: the **NEURON_ISA_TPB** tensor-engine macro-op word — the 64-byte instruction
(`NEURON_ISA_TPB_INST_NBYTES = 64`) the host compiler emits and the device firmware
decodes. The neuronx-cc `isa_tpb`/`sunda` tables are a **5th source on that same Source-4
layer**: a *different serialization, produced by a different tool, shipped in a different
artifact* — the **compiler wheel**, not the device custom-op library.

> **NOTE — what makes this the 5th source, not a re-read of Source 4.** Source 4 is the
> **device-side** TPB layer (`neuronx-gpsimd/extracted/.../custom_op/c10/include/<gen>_arch_isa/tpb/`,
> a hand-authored C++ header + a generated `instruction_mapping.json` whose
> `struct2opcode` has **89** entries on the sunda floor — byte-verified
> `jq '.struct2opcode|length'`). The 5th source is the **compiler-side** TPB layer
> (`CC/isa_tpb/sunda/{neuron_isa,neuron_isa_tpb_pybind}.so` — RTTI-registered C++ enums +
> a per-opcode pybind builder/validator module). An error in one — a stale header, a
> wrong pybind encode template — would *not* propagate to the other; their agreement is a
> genuine confidence signal, exactly the independence criterion
> [tie-database §4](../isa/core/tie-database.md) requires of a source. They share *design*
> provenance (one TPB ISA spec) but independent *serialization + tool*. [HIGH/OBSERVED]

This 5th source is **orthogonal** to Sources 1–3 (the Vision-Q7 FLIX core ISA): trying to
find a "missing ISA-39 Xtensa opcode" in `isa_tpb` is a category error — the two rosters
describe different machines at different abstraction levels. The device firmware *decodes*
the TPB opcode and *runs* a Vision-Q7 FLIX kernel to implement it (the
`kernel_info_table` binding documented in
[opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md)).

### 1.1 What the two `isa_tpb/sunda` modules are

| Artifact (`CC/isa_tpb/sunda/`) | size | what it registers | OBSERVED via |
|---|---|---|---|
| `neuron_isa.cpython-311-…so` | 533 KB | the TPB ISA **enum** module: `NEURON_ISA_TPB_OPCODE` + 20 sibling enums (DTYPE, ALU_OP, CCE_OP, DGE_OPCODE, DGE_COMPUTE_OP, COLLECTIVE_TYPE, ACCUM_CMD, IMM_SRC(_N), INDIRECT_DIM, MATMUL_PSUM_ACCUMULATE_FLAGS, MATMUL_ZERO_REGION, PE_FP32MODE, PERF_OPT_MODE, RAND_ALGORITHM, TENSOR_SUBDIM, TENS_SCALAR_REV_OPS, UPDATE_MODE, WAIT_MODE, DROPOUT_THRESHOLD_TYPE) | RTTI `_ZTI…NEURON_ISA_TPB_*` + strings |
| `neuron_isa_tpb_pybind.cpython-311-…so` | 930 KB | the per-opcode **builder + validator** module: a `NeuronInstruction` with `get_bytes`/`set_bytes` (the encode/serialize boundary), `is_valid_neuron_instruction`, the CamelCase per-opcode builders, and the per-field `<field>_valid_*` / `check: '…'` validators | strings + symbols |

`[HIGH/OBSERVED — both `.so` present; the
`NEURON_ISA_TPB_OPCODE`/`_ALU_OP`/`_CCE_OP`/`_COLLECTIVE_TYPE`/`_DGE_COMPUTE_OP` RTTI
typeinfo strings are present in `neuron_isa.so`.]`

### 1.2 The corroboration — what the 5th source confirms

The 5th source's value is **agreement**, op-by-op, with the device ledger
([opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md), the device-side
#688-class ledger this Part binds against) and with the four enumerated sources:

1. **The full TPB opcode roster, byte-name-identical.** Every opcode name in the device
   ledger is present in the pybind enum, including the 30 `Pseudo*` and the bf16 cluster
   — a **5-way roster agreement** (the 4 device-side gen headers + this compiler enum).
   No *new* opcode appears in the compiler that the ledger lacks (it cannot: the ledger is
   the maverick superset; the sunda compiler is the v2 floor). The compiler **adds no
   opcode; it confirms the roster** and supplies the per-opcode descriptor struct +
   validators. [HIGH/OBSERVED]

2. **The 20 operand/mode sibling enums** match the device firmware enum reads field for
   field — e.g. `COLLECTIVE_TYPE ∈ {ALL_REDUCE, ALL_GATHER, REDUCE_SCATTER, ALL_TO_ALL,
   ALL_TO_ALL_V, PERMUTE_REDUCE, PERMUTE_REDUCE_IMPLICIT}` (strings verified), and
   `DGE_COMPUTE_OP` (RTTI `_ZTI29NEURON_ISA_TPB_DGE_COMPUTE_OP`) confirms the device
   `dma_compute` reduce-op set from a 5th source. [HIGH/OBSERVED]

3. **The pseudo-count gen-floor refinement.** The sunda compiler enum carries **30**
   `Pseudo*` names, the maverick device header **31** — the delta is exactly
   `PseudoJpegDecode` (a later-gen addition absent from the sunda floor), consistent with
   the ledger's per-gen matrix, not a conflict. [HIGH/OBSERVED — no-gen guard honored.]

> **QUIRK — the BIR spelling `Matmult` (two-t) vs the TPB spelling `Matmul` (one-t) marks
> the IR↔ISA boundary.** The Penguin BIR node is `InstMatmult` (`_ZN3bir11InstMatmult…`);
> the TPB opcode it lowers to is `Matmul 0x02`. This is the cleanest single tell of which
> layer a name belongs to — the [BIR roster](bir-inst-roster.md) owns the IR spellings,
> the device ledger owns the TPB spellings. [HIGH/OBSERVED]

### 1.3 The gap-opcode corroboration (the sharpest claim)

The device decode alone left a set of **maintained gap opcodes** with "name/semantics
inferred" (the firmware decode is a `NONE`-tagged stub, or the descriptor literals were
unrecoverable from the carved firmware `.literal` pool). For the **sunda-present** subset,
the 5th source upgrades those to OBSERVED: it carries the opcode **name**, the **builder
class**, the **descriptor struct**, and the **field validators**:

| device gap op (numeric CARRIED from the ledger) | 5th-source corroboration (byte-verified) | conf |
|---|---|---|
| `0x48 RECIPROCAL` (POOL) | `s_reciprocal_opc…` builder (pybind) | HIGH/OBSERVED |
| `0x49 MEMSET` (POOL) | `s_memset_opcode` + `memset_set_value` + `is_valid_memset` (pybind) | HIGH/OBSERVED |
| `0x67 POOL_BUFFER_LOAD` (POOL) | `S4_PB_STRUCT` (libwalrus) + `s4_pb_reserved_zero` + `pool_buffer_load_element_count` (pybind) | HIGH/OBSERVED |
| `0x69 LOAD_MASK_SELECT` (POOL) | `s_load_mask_select_opcode` (pybind) | HIGH/OBSERVED |
| `0x6A STREAM_SHUFFLE` (POOL) | `s_stream_shuffle` / `s_stream_transpose_opcode` (pybind) | HIGH/OBSERVED |
| `0x74 TENSOR_SCALAR_ADDR` (POOL) | `S2D2_TS_AS_STRUCT` + `S2D2_ADDR_STRUCT` (libwalrus) | HIGH/OBSERVED |
| `0x7A LOAD_POOL_ARGUMENT` (POOL) | `s_load_pram_opco…` builder (pybind) | HIGH/OBSERVED |
| `0x85 / 0x86 CUSTOM_OP_HEADER/PAYLOAD` (POOL) | `CUSTOM_OP_HEADER_STRUCT` + `CUSTOM_OP_PAYLOAD_STRUCT` (libwalrus) + `custom_op_header_scratch_space_val` (pybind) | HIGH/OBSERVED |
| `0x77 / 0x78 RAND_GET/SET_STATE` (DVE) | `rand_get_state_valid_dtype` + `s_rand_set_state` + `S1_RAND_STRUCT` (libwalrus) | HIGH/OBSERVED |
| `0x95 MODIFY_POOL_CONFIG` (POOL) | `s_modify_pool_config_opcode` + `modify_pool_config_reserved_zero` (pybind) | HIGH/OBSERVED |
| `0xBD DMA_TRANSPOSE` (DMA) | `neuronxcc::core_v3::is_valid_dma_transpose(NEURON_ISA_TPB_INST_UNION, NEURON_CORE_VERSION)` + `s_valid_dma_transpose_nc` (libwalrus, core_v3/v4) | HIGH/OBSERVED |
| `0x8A–0x8F BF16 cluster` (POOL) | `TensorTensorAddBf16` / `MultBf16` / `SubBf16` + `TensorReduceAddBf16` / `MaxBf16` (enum) — **SUNDA-only**, present as flagged | HIGH/OBSERVED |

> **GOTCHA — the confirmation runs *both* ways: presence AND absence.** The maverick /
> CoreV5-only opcodes the ledger flags as absent from the sunda floor are
> **confirmed-absent** in the 5th source: `TENSOR_TENSOR_INT_WIDE 0xF3` /
> `TENSOR_SCALAR_INT_WIDE 0xF4` — the string `int_wide` (any case) has **grep count 0**
> across `neuron_isa.so`, `neuron_isa_tpb_pybind.so`, *and* `libwalrus.so`. Likewise
> `ACTIVATE_MULTIPASS 0x26` and `DMA_MEMCPY2 0xB9` are absent from the
> sunda table. So the 5th source corroborates the ledger's **per-gen Y/n matrix** in both
> directions — the sunda-floor gap ops are present-and-named; the maverick-only ones are
> verifiably-absent. The compiler does **not** reveal the maverick INT_WIDE byte
> encodings (out-of-table on the sunda floor). [HIGH/OBSERVED]

### 1.4 The descriptor emitter (the device-side gap, closed from the compiler side)

The device firmware's opcode→descriptor literals were unrecoverable from the carved
`.literal` pool. The compiler **is the producer** of those descriptors, and resolves the
gap directly: `libwalrus.so` names exactly **68** distinct `NEURON_ISA_TPB_*_STRUCT`
descriptor types (byte-verified: `strings libwalrus.so | rg -o 'NEURON_ISA_TPB_…_STRUCT' |
sort -u | wc -l = 68`) — the descriptor FORMAT taxonomy keyed by the `S<n>S<n>D<m>_<op>`
tag (N source + M destination access-patterns) — plus the access-pattern types
(`TENSOR1D`–`4D`, `ADDR4`) and the emitter API
`neuronxcc::backend::CoreV{2,3,4}GenImpl::assignAccess{1,2,3,4}D(…, const bir::AccessPattern&, …)`
+ `assignAccessToType` + `assignStaticPattern` + `assignStartAddr` + `setupSyncUpdate`
(mangled symbols present in the `backend::` namespace).

This is a **pointer, not a re-derivation**: the *full* descriptor backend — the
per-`STRUCT` field byte-offsets, the `get_bytes` 64-byte serializer body, the
`CoreVNGenImpl` weak-inlined bodies — is **owned by the neuronx-cc wiki** (§3). This Part
owns only where that emitter **meets the GPSIMD seam**: the
[compiler-map §1](compiler-map.md) descriptor-emit leg and the
[MX device bodies](mx-device-bodies.md) page, which byte-decodes the *device* consumer of
two of those structs (`S3DMX1_QUANT` and `MXMEM_PATTERN1D`).

### 1.5 The cost model + version anchor

`CC/hwm/ctm.…so` carries the per-gen, per-engine Pool cost classes `{InferentiaPool,
SundaPool, CaymanPool, CoreV4Pool}` (RTTI-verified: the mangled `_ZN9SundaPoolC1Ev` etc.
+ `getReorderWindowSize`) — the GPSIMD/Q7 launch-cost model the scheduler queries. The
architecturally meaningful datum (the ~150-cycle `GPSIMD_START` vs the 64-cycle Vector
`MIN_II`, from the cc-stubs `.pyi`) is **why** the compiler keeps small tiles on Vector
and routes to GPSIMD only what Vector cannot do; that tie is already stated and owned at
[compiler-map §1.1](compiler-map.md) and [sundaisel §4](sundaisel.md). The full scheduler
cost model is **owned by the neuronx-cc wiki** (§3).

**Version.** The `isa_tpb`/`sunda` tables ship under the neuronx-cc wheel
`__version__ = '2.24.5133.0+58f8de22'`, `__buildtime__ = 'Apr 08 2026, 21:07:10 UTC'`
(byte-verified `cat CC/version/__init__.py`). This is the **toolchain** version;
it is **distinct** from the device-image `ulib_to_ucode_version 1.21.1.0` /
`ulib_to_isa_version 1.0.2520.0` handshake (CARRIED; see the version table at
[compiler-map §7](compiler-map.md)), which is the runtime/firmware axis. The cc table is
the compile-time TPB ISA whose ucode target is `1.21.1.0`.

> **NOTE — `kernel_info_table` is NOT compiler-emitted.** No `kernel_info_table` / `funcVA`
> emission appears in any cc artifact (zero strings across `isa_tpb`, `libwalrus`,
> `penguin`). This confirms the device-side model: the `(spec,opcode)→funcVA` dispatch
> table is baked into the **device firmware image**, not produced by the host compiler.
> The division of labor is exact: **neuronx-cc emits** the TPB instruction (opcode byte +
> descriptor); **the device firmware provides** the `kernel_info_table` that routes that
> opcode to its Vision-Q7 FLIX kernel. The `kernel_info_table` is the runtime/firmware
> binding between the two ISAs; the compiler emits only the upstream (TPB) half.
> [HIGH/OBSERVED — absence in cc; the device-side placement is CARRIED from
> [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md).]

---

## 2. The ownership boundary — owned here vs cross-linked there

The line between this device guide and the sibling neuronx-cc compiler wiki, one row per
subsystem. **Owned here** means a committed page in *this* Part decodes it to the GPSIMD
seam; **in the neuronx-cc wiki** means the full internals live in the separate compiler
book (§3) and this Part only *points* at the seam.

| subsystem | owned HERE (this Part) | in the neuronx-cc wiki (the sibling book) |
|---|---|---|
| `hlo-opt` / `hlo2penguin` (HLO frontend) | only the **fact** that the XLA path converges on the same Penguin BIR + same opcodes ([compiler-map §6](compiler-map.md)) \| no HLO-pass internals | the full HLO optimizer + the HLO→BIR lowering passes |
| Penguin middle-end (transforms, tiling, scheduling, memory planning) | only the **GPSIMD/POOL-touching** passes named at [compiler-map §1.1](compiler-map.md) (`SundaSizeTiling`, `LegalizePartitionReduce`, the launch-cost tie) | every BIR pass, the list scheduler, the modulo/spill memory planner, the latency model |
| Penguin **BIR** `Inst*` node set | the **full 110-class roster** + the GPSIMD-relevant BIR→ISA→opcode map ([bir-inst-roster](bir-inst-roster.md)) | (the BIR is a device-seam object; this Part owns it) |
| **SundaISel** (the GPSIMD ISel leg) | the **engine-poly decision, intrinsic fusion, partition-reduce legalization, DGE selector** ([sundaisel](sundaisel.md)) | the base `NeuronISel` + the non-GPSIMD `targets/generated/*Gen` leaf codegen for PE/ACT |
| `isa_tpb`/`sunda` ISA tables (the **5th source**) | the roster/gap/descriptor **corroboration** + version (§1) | the `get_bytes`/`set_bytes` 64-byte serializer body, the full pybind builder/validator catalog |
| **`libwalrus`** descriptor backend | only **which** `NEURON_ISA_TPB_*_STRUCT` each GPSIMD opcode lowers to + the access-pattern model ([compiler-map §1](compiler-map.md), [sundaisel §6.5](sundaisel.md) `AssignHWDGEEngine`) | the **65 MB** codegen core in full: per-`STRUCT` field byte-offsets, the `CoreVNGenImpl` emitter bodies, the 688-class validator suite, `walrus_driver` |
| descriptor / access-pattern emission | the GPSIMD opcode→struct **bindings** + the MX `S3DMX1_QUANT`/`MXMEM_PATTERN1D` *device consumer* ([mx-device-bodies §2.5, §3.2](mx-device-bodies.md)) | the host-side `assignAccessND`/`assignStaticPattern`/`assignStartAddr` emitter implementation |
| **MX device bodies** (the `0xE3` DVE kernel, the v4/v5 PE pair) | **byte-decoded native** + the E8M0 value tie ([mx-device-bodies](mx-device-bodies.md)) | the host-side MX lowering map (`mx-path`) is owned **here**; the cc wiki owns nothing of the *device* MX body |
| scheduler **cost model** | only the GPSIMD launch-cost **tie** (`GPSIMD_START≈150` vs `MIN_II≈64`) and the `ctm.so` Pool-class **existence** (§1.5) | the full `hwm/ctm.so` cost classes, the per-op cycle formulas, the latency→schedule feedback |

> **CORRECTION (the earlier "73 InstXxx" count).** The backing report
> tabulated **73** Penguin BIR `Inst*` classes from an intermediate `birpy` read. The
> committed [bir-inst-roster](bir-inst-roster.md) grounds the count on the **full
> `libBIR.so`** `createFromJson`/`_ZTV` symbol sets: **110** concrete classes (the "73" was
> the GPSIMD/compute-touching subset, before the 24-class control spine, 4 load/store, and
> 5 kernel containers). This page defers to the **110** figure — point, don't re-derive.
> [HIGH/OBSERVED — owned at [bir-inst-roster §2, §8](bir-inst-roster.md).]

> **CORRECTION (the earlier framing of `isa_tpb` as confirming a "~172-opcode"
> roster).** The committed [compiler-map §7](compiler-map.md) and
> [opcode-catalog-ledger](../firmware/kernels/opcode-catalog-ledger.md) reconcile the
> ledger as **172 union enum values − 31 PSEUDO − 1 INVALID = 140 real HW opcodes**. The
> 5th source's "~172" is the **union enum cardinality** (every name incl. pseudos +
> invalid), not the real-HW-opcode count; both are correct at their level. This page cites
> the **140 real / 172 union** split per the owned ledger. [HIGH/OBSERVED.]

---

## 3. Explicit pointers to the neuronx-cc wiki (the non-GPSIMD internals)

The following live in the **separate neuronx-cc compiler mdBook** — *not* a relative link
from this book (it is a different `book.toml`). Cite them by **path/name**. A reimplementer
of the device engine does **not** need these; a reimplementer of the *compiler* does.

- **`neuronx-cc/wiki/` — the `hlo-opt` / `hlo2penguin` HLO frontend.** The XLA-HLO optimizer
  and the HLO→Penguin-BIR lowering. This Part records only that it converges on the same
  BIR ([compiler-map §6](compiler-map.md)).
- **`neuronx-cc/wiki/` — the Penguin middle-end.** Every BIR transform/legalize pass, the
  tiling/layout pipeline, the list scheduler (`build_ddg`/`enumerate_ready`/`pick_candidate`),
  and the modulo-allocation / max-live-spiller memory planner.
- **`neuronx-cc/wiki/` — the `libwalrus` backend in full.** The 65 MB descriptor-emission
  core: the per-`NEURON_ISA_TPB_*_STRUCT` field byte-offsets, the `CoreV{2,3,4}GenImpl`
  emitter bodies, the `get_bytes` 64-byte serializer, the ~688-entry `is_valid_*`/`valid_*`
  validator suite over `NEURON_ISA_TPB_INST_UNION`, and `walrus_driver`/`hlo-neff-wrapper`
  NEFF output.
- **`neuronx-cc/wiki/` — the scheduler cost model.** The `hwm/ctm.so` per-gen×per-engine
  cost classes and the per-op cycle formulas, beyond the single GPSIMD launch-cost tie this
  Part needs.
- **`neuronx-cc/wiki/` — the `isa_tpb` builder/validator catalog.** The full pybind
  `get_bytes`/`set_bytes` encode boundary and the per-opcode CamelCase builders, beyond the
  roster/gap corroboration §1 cites.

> **NOTE — the neuronx-cc wiki is a SEPARATE BOOK.** It has its own `SUMMARY.md` and
> `book.toml`; a relative `[link]` from this book would dangle. These are deliberately
> **inline path/name citations**, not hyperlinks. Within *this* book, the GPSIMD seam is
> fully cross-linked (§4). [convention — keeps `mdbook build` link-clean.]

---

## 4. Cross-references (within this book)

- [The GPSIMD-Relevant Compiler Map + `emit_*`→opcode](compiler-map.md) — the 4-stage
  HLO→BIR→SundaISel→Generator→NEFF map and the 62-row `emit_*`→opcode table; the seam
  opener.
- [The Penguin BIR Instruction Set + BIR→ISA Map](bir-inst-roster.md) — the 110-class BIR
  `Inst*` roster and the per-class device-opcode binding (the 73→110 correction above).
- [SundaISel Deep-Dive](sundaisel.md) — the engine-poly ISel, intrinsic fusion, the
  `AssignHWDGEEngine` libwalrus pass, the DGE selector.
- [Byte-Decode of the MX Device Bodies](mx-device-bodies.md) — the device consumer of the
  `S3DMX1_QUANT`/`MXMEM_PATTERN1D` descriptors this 5th source names.
- [Dtype/Engine/Gen Fan-In + CC-Lane Synthesis](dtype-engine-fanin-synthesis.md) — the
  bf16/INT_WIDE dtype→opcode dispatch (the sibling capstone; the `0x8A`–`0x8F` cluster §1.3
  corroborates).
- [The TIE Database & Four Independent ISA Sources](../isa/core/tie-database.md) — the four
  sources this page adds a 5th to (the 5th is parallel to Source 4, orthogonal to 1–3).
- [Opcode Catalog Ledger](../firmware/kernels/opcode-catalog-ledger.md) — the device opcode
  roster the 5th source corroborates op-by-op.
