# The Corpus, Tiers & Binary Inventory

This page is the **SHA-pinned, role-annotated catalogue** of every artifact this reference is
built on. Its job is narrow and absolute: a reader who meets any claim anywhere in the guide
should be able to come here, find the exact file that backs it, and re-hash that file on their
own disk to confirm they are looking at the same bytes. Nothing here is interpretation — it is
the *index of witnesses*. The interpretation lives in the subsystem pages; this page only says
**what exists, where, how big, and what each thing is ground-truth for.**

Everything was recovered by **static analysis of shipped, redistributable binaries, headers,
config files, and TIE artifacts** — no runtime trace, no debugger session, no design document.
Two Debian packages and their nested toolchain tarball are the entire primary corpus. Two
sibling reverse-engineering corpora are used only for cross-checks. A wave of 677 internal
analysis reports is derived output, not a primary artifact, and is catalogued as such at the
end.

Every count and size on this page is **derived directly from the files on disk**
(`sha256sum`, `stat -c%s`, `file`, `nm`, `readelf`), not copied from a report. Where a count
matters, it is tagged `[CONF/PROV]` per the [Confidence & Walls Model](confidence-model.md), and
counts taken from a symbol table say so explicitly — **symbol-table counts via `nm`, never a
grep over decompiled text**, because decompile-grep inflates opcode/leaf tallies two-to-twelve-fold.

---

## 1. The tier model

Artifacts are graded by **how directly they ground a claim** — the shorter the chain from a
statement back to a byte, the higher the tier. A reimplementer encoding a hard requirement wants
a T0 witness; a T3 report is a pointer to one, never a substitute.

| Tier | What it is | Trust role | Re-derivable? |
|---|---|---|---|
| **T0 — Primary binary / config / data** | A shipped ELF, static archive, TIE database, JSON/pickle config, or header that the firmware/toolchain *actually ships and runs*. The byte **is** the witness. Includes the value/decode/timing **oracles** that can be executed in-process. | Ground truth. An `OBSERVED` claim must terminate here. | Yes — hash, `nm`, or **execute** the file yourself. |
| **T1 — IDA-extracted sidecar JSON** | Per-binary analysis databases produced by running IDA over a T0 ELF: functions, callgraph, structures, enums, strings, xrefs, decompiled C, disasm, per-function context notes. A *lens* onto a T0 binary, not a new source. | Strong, but one tool-step removed. Confirms structure; defer to the T0 binary on any disagreement. | Yes — re-run IDA on the same T0 ELF. |
| **T2 — Cross-check sibling corpora** | Separately-shipped AWS Neuron binaries (`neuronx-cc`, `neuronx-collectives`, `nki-0.3.0`) that name the same codenames, opcodes, or ABI and let a gpsimd claim be corroborated from an independent build. | Corroboration only. Used to confirm a gpsimd-internal fact, never as its sole source. | Yes — the sibling packages are in this repo tree. |
| **T3 — Derived analysis reports** | The 677-file `raw/` wave: investigation write-ups, atlases, censuses, repair queues. **Derived from T0/T1, not primary.** | A navigation and provenance layer. A `CARRIED` claim cites here; it must trace through to a T0/T1 witness to become `OBSERVED`. | Partially — they re-derive from T0/T1, which you re-run. |

The decisive property of the gpsimd corpus is that **several T0 artifacts are executable oracles,
not just readable data.** `libfiss-base.so` answers "what value does this opcode produce?" by
*running*, and `libcas-core.so` answers "how many cycles?" by *running* (behind a license gate).
That is why this reference reaches `OBSERVED-by-execution` — the strongest tier of fact short of
silicon — and it is why the oracle DLLs sit at the top of the T0 list below.

---

## 2. The primary corpus at a glance — two packages, one tarball

Everything T0 arrives in **two Debian packages** plus the toolchain tarball nested inside the
second. `[HIGH/OBSERVED]`

| Package (`.deb`) | Role | Notable T0 contents |
|---|---|---|
| `aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64` | The **custom-op runtime + arch artifacts**. ~197 MB, 8 240 files. | `libnrtucode.a` / `.so` / `_internal.so`; the 4 `neuron_<gen>_arch_isa/` header sets + `instruction_mapping.json`; the `arch-headers/<gen>/` + `arch-isa/` trees (incl. Tonga); the Maverick `al_address_map_db.pkl`/`.json`. |
| `aws-neuronx-gpsimd-tools_0.21.0.0-bc9b5fad5_amd64` | The **toolchain**. ~937 MB; ships as a *single* 954 585 328-byte tarball `…/tools/gpsimd_tools.tgz`. | Unpacked → `tools/ncore2gp/config/` (the oracle DLLs + `core.yml`/`core.xparm`) and `tools/XtensaTools/` (the native Xtensa disassembler + LLVM-10 Xtensa backend). |

The package versions are themselves OBSERVED facts (they are the directory names of the
extracted trees). The customop package is `0.21.2.0`; the tools package is
`0.21.0.0-bc9b5fad5`. Treat the build suffix `bc9b5fad5` as the tools-package build id.

> **Path convention for this page.** All T0/T1 paths are written relative to the corpus root
> `neuronx-gpsimd/`. The two extracted package roots are abbreviated:
> - **`CUSTOMOP/`** = `extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/`
> - **`TOOLS/`** = `extracted/nested/gpsimd_tools_tgz/tools/`
>
> `extracted/` is **git-ignored**; locate files there with `fd --no-ignore` or an absolute path.

---

## 3. T0 — Device firmware (the Vision-Q7 images)

These are the artifacts that carry the actual GPSIMD device code. The firmware that runs *on the
Q7 DSP* is **not** a standalone file — it is a set of **ELF32-Xtensa images embedded as `.rodata`
data inside two host-side x86-64 wrapper libraries**, exposed through `…_SO_get` / `…_JSON_get`
accessor symbols. That is the single most important structural fact a reimplementer must
internalise here, and the inventory makes it explicit.

| Artifact (`CUSTOMOP/c10/lib/…`) | Size (B) | Type | Symbols | Role / ground-truth-for |
|---|---|---|---|---|
| `libnrtucode.a` | 10 235 636 | `ar` archive, 435 members | named `.o` objects | The **static firmware library**: object files `nrtucode_core.c.o`, `nrtucode_images.c.o`, `nrtucode_opset.c.o`, `prelink*.c.o`, … — the link-time form of the device microcode loader and image set. |
| `libnrtucode.so` | 3 208 440 | ELF64 x86-64 `.so`, **stripped** | dynsym only | Shared form of the loader; carries **12 embedded ELF32-Xtensa device images** (`e_machine = 94`). Stripped → structure read via T1 IDA sidecar + embedded-image carving. |
| `libnrtucode_internal.so` | 10 276 288 | ELF64 x86-64 `.so`, **not stripped** | 946 (`nm`) | The **richer wrapper**: 66 `…_EXTISA_*_(SO\|JSON)_get` blob accessors and **16 embedded ELF32-Xtensa device images** (`e_machine = 94`). Carries the `NRTUCODE_CORE_*_NX_POOL` core-kind enum and the per-codename image getters. Ground truth for the **image set, the core-kind enum, and the loader dispatch.** |

SHA-256 of the two firmware `.so` wrappers:
- `libnrtucode_internal.so` = `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b`
- `libnrtucode.so` = `06d3f0b1630e38828ace79d3c9f3123ac14b3ea4c5cde4aa906a31b3e82ccce5`
- `libnrtucode.a` = `158dadc5c76dc0491b9243091458b43c2d59091ba3ba5a727206915bd7bd6130`

**Counts** (all `[HIGH/OBSERVED]`):
- `libnrtucode_internal.so` has **946** total `.symtab` symbols (`nm | wc -l`) and **66**
  `…_EXTISA_*_(SO|JSON)_get` accessor symbols. Per-codename `…_SO_get` image getters:
  **1 SUNDA, 4 CAYMAN, 4 MARIANA, 4 MARIANA_PLUS, 4 MAVERICK = 17** (the JSON-twins double this
  to 34 getter symbols).
- Embedded ELF32-Xtensa device images, counted by scanning `\x7fELF` magic and reading
  `e_machine`: **16** in `libnrtucode_internal.so`, **12** in `libnrtucode.so` — a corpus device-image
  total of **≈28–29**. The combined figure of **29 embedded ELF32-Xtensa device blobs** is the
  cross-page anchor used in [How to Read This Guide](how-to-read.md). `[HIGH/OBSERVED]` for the
  per-library 16/12; `[HIGH/CARRIED]` for the aggregate 29 (spans both libs).
- The device firmware is compiled `-fno-rtti`: `_ZTI`/`_ZTV`/`_ZTS` symbol count = **0** in the
  wrapper, confirming no C++ class hierarchy is recoverable from the device side. `[HIGH/OBSERVED]`

### NOT-PRESENT in this checkout — named but absent

The following are referenced in the broader Neuron literature but **do not exist as files in this
corpus**; any page that needs them must say so and treat the claim as `CARRIED` or walled. Marking
them prevents fabrication.

| Named artifact | Status | What it actually is here |
|---|---|---|
| `libnrtucode_extisa.so` | **NOT-PRESENT** (not a file) | There is no separate ext-ISA `.so`. The "EXTISA" content is the **embedded blob set** (`CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get`, …) *inside* `libnrtucode_internal.so`. |
| `libncfw.so` | **NOT-PRESENT** | No standalone NCFW (scalar-Xtensa-LX management-core) shared object ships here. NCFW analysis rests on its image *inside* the wrapper / sibling reports. |
| `libnrt.so` (host runtime, `.2.x`) | **NOT-PRESENT** | The host NeuronCore runtime library is **absent from this checkout**. Its struct/enum/RTTI census (the large host-side census referenced in the appendix) is therefore a **`CARRIED`** claim from sibling/host-runtime corpora, not OBSERVED here — see §6 and the host-runtime struct-census appendix. The v5 `Q7_CC_TOP` collective firmware image is likewise file-absent (a named wall in the [Confidence & Walls Model](confidence-model.md)). |

---

## 4. T0 — The ncore2gp oracle DLLs (decode, value, timing)

These nine host x86-64 DLLs under `TOOLS/ncore2gp/config/` are the **most valuable artifacts in
the corpus**, because they are not merely readable — they *are the Vision-Q7 model*, callable
in-process. They are **all `not stripped`** (they retain a full `.symtab`; none carries DWARF
`.debug_*` sections — "not stripped" here means named symbols, not source-line debug info).
`[HIGH/OBSERVED]`

| Artifact (`TOOLS/ncore2gp/config/…`) | Size (B) | SHA-256 (first 16) | `.symtab` syms | Role / ground-truth-for |
|---|---|---|---|---|
| `libisa-core.so` | 9 690 712 | `8fe68bf462ce76ee` | 45 198 | The **canonical ISA decode model** — opcode/iclass/operand/regfile/slot/field/decode tables. The home of the decode table set; its export namespaces are `opcode/proto/ctype/regfile/iclass/operand/slot/decode/…`. |
| `libisa-core-hw.so` | 36 576 | `569bddc1623d30f9` | small | Hardware-variant ISA shim companion to `libisa-core`. |
| `libfiss-base.so` | 12 330 016 | `260b110cd59c76b0` | 20 525 | The **value oracle**. **864** `module__xdref_*` per-element value leaves (`nm`-counted) — the per-opcode value functions, **callable via ctypes with no license.** This is the lane that yields `OBSERVED-by-execution` value facts. |
| `libfiss-ref-base.so` | 12 330 216 | — | ~ | Reference twin of `libfiss-base` (differential value cross-check). |
| `libcas-core.so` | 45 878 080 | `7f1d86da52891b3c` | 179 079 | The **cycle / timing oracle** (latency, stall, issue, fault). Instruction *retirement* gates on `AUTH::check_iss_licenses` → FlexNet-key-walled. Carries `.symtab`. |
| `libcas-ref-core.so` | 32 715 096 | `d9c9b5da…` | ~ | Reference twin of `libcas-core` (timing cross-check). |
| `libtie-core.so` | 51 098 208 | `06fc43eaf3622ae1` | large | The **TIE database** core — the Tensilica instruction-extension model the whole config is generated from. |
| `libctype.so` | 388 648 | `eb79ff9fc9a8f6fe` | 497 | The **ctype / coprocessor / functional-unit** classification tables. `not stripped`. |
| `libtie-Xtensa-msem.so` | 258 120 | — | ~ | TIE memory-semantics helper. |

Alongside the DLLs, the config directory ships the **plain-text core description** that the DLLs
are built from — these are themselves T0:

| Config file (`TOOLS/ncore2gp/config/…`) | Size (B) | Role |
|---|---|---|
| `core.yml` | 1 265 482 | The Vision-Q7 **core configuration** (registers, options, sizes). |
| `core.p.yml` | 773 788 | Processed/expanded core config. |
| `core.xparm` | 193 946 | The Xtensa parameter dump (`xparm`) — the canonical config-token source. |
| `init_code.c` | 9 362 | Core init sequence. |

The complete TIE description in human-readable form is **not** in the config dir but under
`TOOLS/ncore2gp/control/TIE/`: `Xtensa.xml` (**45 533 206** B — the TIE-XML) and `Xtensa.tl`
(**112 537 066** B — the TIE source). These are the fourth independent ISA witness (alongside
`libisa-core`, the arch-ISA headers, and the embedded images). `[HIGH/OBSERVED]`

> **Re-derivation note on opcode/leaf counts.** A naïve `rg -c 'OPCODEDEF'` or `rg -c 'Opcode'`
> over a symbol dump returns ~15 700 — that is **inflated noise**: there is no literal `OPCODEDEF`
> symbol or string in `libisa-core.so` at all. The honest, useful figures are the **864**
> `module__xdref_` value leaves in `libfiss-base.so` and `libisa-core.so`'s decode-table export
> namespaces, both `nm`-grounded. Do not cite the grep number. `[HIGH/OBSERVED]`

---

## 5. T0 — Arch-ISA headers, instruction maps, and register/address artifacts

The cleartext per-generation specification. Five generations are represented, in **two distinct
naming families** — a fact a reimplementer must not flatten:

- The **four unified generations** ship as `CUSTOMOP/c10/include/neuron_<gen>_arch_isa/`
  (`aws_neuron_isa_*` headers), for `<gen>` ∈ {`sunda`, `cayman`, `mariana`, `maverick`}.
- **Tonga (v1)** is the **pre-unified outlier**: it ships separately as
  `CUSTOMOP/c10/include/arch-headers/tonga/` (215 files) and the older `arch-isa/` tree (63 files,
  `aws_tonga_isa_*` headers). It is *not* one of the four `neuron_<gen>_arch_isa/` sets. `[HIGH/OBSERVED]`

| Per-gen header set (`neuron_<gen>_arch_isa/`) | `.h` headers | `instruction_mapping.json` (B) | `struct2opcode` entries |
|---|---|---|---|
| `sunda` | 100 | 13 619 | **89** |
| `cayman` | 111 | 14 482 | **99** |
| `mariana` | 120 | 15 483 | **108** |
| `maverick` | 126 | 16 158 | **114** |

Each `instruction_mapping.json` has top-level keys `{struct2opcode, struct2pseudo_opcode}`
(`struct2pseudo_opcode` = 2 in every gen). The monotone climb 89→99→108→114 is the OBSERVED
per-generation opcode growth. The `cayman` map hashes to
`4e9c1f6abe0d015d…`. `[HIGH/OBSERVED]`

The **core-kind enum** that ties these names to firmware is read directly from
`libnrtucode_internal.so` strings: `NRTUCODE_CORE_{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS,MAVERICK}_NX_POOL`
— **5 NX-POOL core kinds**. (The plain `libnrtucode.so` assert lists only the first four;
the `_internal` variant lists all five.) `[HIGH/OBSERVED]`

### Register / CSR / address-map artifacts

| Artifact | Location | Size | Role |
|---|---|---|---|
| **Cayman CSR JSON set** | `extracted/nested/cayman-arch-regs_tgz/csrs/**/*.json` | 85 JSON files (78.8 MB tree, 127 files total) | The **Cayman control/status register schema** — per-block CSR field definitions (`tpb`, `sdma`, `hbm`, `pcie`, `d2d`, `xtensa_q7`, `xtensa_nx`, …) and RTL/address maps under `address_map/` + `output/address_map/`. |
| **Maverick `al_address_map_db.pkl`** | `CUSTOMOP/c10/include/arch-headers/maverick/ext/al_address_map_db.pkl` | **216 631 794** B | The Maverick **address-map value oracle** (Python pickle). Its JSON twin `al_address_map_db.json` is **514 276 583** B. The ground truth for v5 address decoding. |
| Maverick `vpc-mirror/arch-regs/…` | `CUSTOMOP/c10/include/arch-headers/maverick/vpc-mirror/…` | (large) | Maverick secure/user internal address maps + per-block CSR JSON. |

`[HIGH/OBSERVED]` on every size/path above. Note: the `al_address_map_db.pkl`
lives in the **maverick** customop arch-headers, not in the `cayman-arch-regs` tarball — both are
catalogued above so the distinction is explicit.

---

## 6. T0/T1 — The native Xtensa toolchain (the device disassembler)

The corpus ships a **complete, runnable Xtensa toolchain** under `TOOLS/XtensaTools/` (55 tools
in `bin/`). The single most useful tool for this reference is the **device disassembler**:

| Tool (`TOOLS/XtensaTools/bin/…`) | Size (B) | SHA-256 (first 16) | Role |
|---|---|---|---|
| `xtensa-elf-objdump` | 1 337 968 | `d43bd4fad891e695` | The **device disassembler** — an x86-64 host binary (`stripped`) that decodes Xtensa `e_machine = 94` object files / firmware with the `ncore2gp` core config. Use it on device `.o`/`.a`/embedded-image bytes; use plain host `objdump`/`nm`/`readelf` for the x86-64 wrappers and oracle DLLs. |

The toolchain also carries the LLVM-10 Xtensa backend (`libLLVMXtensaCodeGen.so.10`,
`libXtensaCodeGen.so`, `xt-clang`/`xt-clang++`) and ~10 ISS variants of `libsimxtcore.so`
(per-GCC/clang build). The firmware itself records two build identities: **XtensaTools-14.09 /
clang-10** (older generations) and **XtensaTools-15.05 / clang-15** (Maverick). `[HIGH/OBSERVED]`

### Host analysis tooling (used on the x86-64 side)

Plain GNU `objdump` / `nm` / `readelf` / `sha256sum` are used directly on the host-side ELFs.
**All symbol counts on this page are `nm` on the binary**, never a grep over decompiled C. The
[Toolchain Inventory & Versions](toolchain-versions.md) page details exact versions; the
[Methodology](methodology.md) page details how the oracles are driven in-process.

---

## 7. T1 — The IDA sidecar JSON set

For each analysed T0 ELF, an IDA pass produces a directory of sidecar artifacts under
`ida/<mangled-target>/`. **12 binaries** in this corpus carry a full IDA database (`.i64`); the
domain-relevant ones are `libnrtucode_internal.so` and `libnrtucode.so` (the remaining ten are the
bundled third-party `libcrypto`/`libssl`/`libsqlite3`/`libbz2`/`liblzma`/`libz`/`libffi` and the
OpenSSL `engines-1.1/*.so` providers — **not** GPSIMD device code, listed only to account for the
12). `[HIGH/OBSERVED]`

Per target, the sidecar set provides (25 JSON kinds + binary/text dumps):

| Sidecar | Provides |
|---|---|
| `_functions.json` / `_function_addresses.json` / `_entries.json` | Function inventory, entry points, addresses. |
| `_callgraph.json` / `_callgraph.dot` | Call relationships (machine + Graphviz). |
| `_structures.json` / `_enums.json` | Recovered aggregate types and enumerations. |
| `_strings.json` / `_names.json` / `_comments.json` | String literals, symbol names, analyst comments. |
| `_xrefs.json` | Cross-references (who-reads/who-writes/who-calls). |
| `_data_tables.json` / `_switches.json` | Jump/dispatch tables and switch statements. |
| `_imports.json` / `_native_imports.json` / `_native_exports.json` | Import/export boundary. |
| `_rtti.json` / `_prototypes.json` / `_frames.json` / `_tryblks.json` | RTTI (empty for the `-fno-rtti` device libs), prototypes, stack frames, try-blocks. |
| `_segments.json` / `_fixups.json` / `_problems.json` / `_metadata.json` / `_complete.json` | Section/segment map, relocations, IDA problem log, DB metadata, completeness flag. |
| `_full.c` / `_ctree.json` | Decompiled C and the ctree AST. |
| `_rodata.bin` / `.i64` | Carved `.rodata` (for embedded-image extraction) and the IDA database. |
| `context/*.md` | **Per-function notes** — one markdown file per analysed function (e.g. `CAYMAN_NX_ACT_DEBUG_DRAM_get_0x9b3540.md`), the human-readable analysis layer. |

When a sidecar and the T0 binary disagree, **the binary wins** — the sidecar is a lens, not a
source. `[HIGH/INFERRED]`

---

## 8. T2 — Sibling cross-check corpora

Three independently-shipped Neuron corpora live in the same repo tree and are used **only to
corroborate** a gpsimd-internal fact from a separate build — never as the sole source of one.
`[HIGH/OBSERVED]`

| Corpus | Location | Cross-check role |
|---|---|---|
| `neuronx-cc` | `neuronx-cc/` (present) | The Neuron compiler. Confirms codename↔generation mapping, opcode mnemonics, and the host-side compile contract from the toolchain side. |
| `neuronx-collectives` | `neuronx-collectives/` (present) | The collectives runtime. Corroborates the device collective firmware (`Q7_CC_TOP`) interface and the host compose pipeline that the device firmware pairs with. |
| `nki-0.3.0` | under `neuronx-misc/` (`nki-0.3.0+…-cp31x` wheels) | The NKI kernel surface. Confirms opcode/intrinsic names and the user-facing instruction vocabulary independently of the firmware. |

---

## 9. T3 — The 677-report `raw/` wave (derived analysis, not a primary artifact)

The `raw/` directory holds **680 entries — 677 `.txt` analysis reports + 3 data files**
(`E007_native_link_graph_edges.tsv`, `W028_package_payload_tree_atlas.tsv`,
`W031_ida_sidecar_manifest_catalog.jsonl`). **This wave is derived output of T0/T1 analysis — it
is documentation, not a witness.** A `CARRIED` claim cites a report here; to become `OBSERVED` it
must trace through to the T0/T1 file the report was built from. `[HIGH/OBSERVED]`

Breakdown by report family:

| Prefix family | Count | What it covers |
|---|---|---|
| `SX-*` | 323 | Single-subsystem deep dives — firmware (`SX-FW` 81), ISA (`SX-ISA` 42), images (`SX-IMG` 28), runtime (`SX-RT` 22), NCFW (`SX-NCFW` 20), CSR (`SX-CSR` 20), address (`SX-ADDR` 20), plus ISS/ABI/INT/CCL/GEN/AUD/NKI. |
| `DX-*` | 207 | Cross-domain — decode/value (`DX-ISA` 30, `DX-VAL` 20, `DX-ISS` 20), runtime (`DX-RT` 20), IDA (`DX-IDA` 30), plus HW/SYN/STRUCT/SEC/NEFF/DMA/CC/GEN/INT. |
| `GX-*` | 47 | Collectives + semantics + FLIX (`GX-OP` 13, `GX-SEM` 10, `GX-REF` 8, `GX-ENG` 5, `GX-FLIX` 4, `GX-AUD` 4, `GX-MAV` 3). |
| `W001–W035` | 34 | The **wave inventory/atlas reports** — package surface (`W011`/`W024`/`W028`), ELF dependency/section/symbol fingerprints (`W025`/`W030`), binary capsule atlas (`W027`), sidecar health (`W031`). These are the primary backing for *this* page. |
| `W-00 … W-06` | 7 | Consolidation/synthesis plans (`W-01 isa_semantics`, `W-02 firmware_kernels`, …). |
| `P*` / `p0-*` / `p1.*` | 33 | The P-series atlases (`p0-01` toolchain map, `p0-02` register atlas, `p0-03` ISA-mapping atlas) + address-band sweeps. |
| `NX-*` | 4 | Cross-package indices. |
| `A/D/E/F/G/H/Q/R` numbered | 25 | Coverage/gap audits, repair queues, evidence packs, link-graph edges. |

**Reconciliation against the binaries (why the FILE wins).** Several T3 reports state large
counts that are *broader censuses* than this page's headline figures, and that is fine as long as
the granularity is named. The clearest example: the ELF-fingerprint report (`W030`) counts
`libfiss-base.so` as having **40 758 exports** (of which `slotfill:25138`); this page cites
**864** value leaves. Both are true — 864 is the `nm`-counted `module__xdref_*` per-element
value-function count (the reimplementer's "how many opcode value semantics" number), while 40 758
is the whole export table including slotfill/regload/writeback rows. **When a report's number and
a re-`nm`'d number disagree on the same quantity, this page uses the binary's number.** `[HIGH/OBSERVED]`

---

## 10. Lawful-interop posture

Every result in this reference is the product of **static analysis only** — reading, hashing,
disassembling, and (for the freely-callable value/decode oracles) *executing in-process* — of
**already-shipped, redistributable** AWS Neuron packages. No vendor source tree was used; no
design document was consulted; no proprietary firmware was decrypted or circumvented. The wiki is
authored under the **interoperability research exemption of 17 U.S.C. § 1201(f)** (and the
analogous EU software-directive provisions): the sole purpose is to document the interfaces and
behaviour necessary to build an **independently interoperable** Vision-Q7-compatible GPSIMD engine,
toolchain, runtime, or simulator. The cycle/timing oracle (`libcas-core.so`) is described only at
the level its *retirement* path is gated by a FlexNet license check (`AUTH::check_iss_licenses`) —
**no license circumvention is performed or documented**; the license wall is reported as a wall
(see the [Confidence & Walls Model](confidence-model.md)), not crossed. The freely-callable value
lane (`libfiss-base.so`) requires no license and is the only oracle this reference executes.

---

## Cross-references

- **[How to Read This Guide](how-to-read.md)** — the 13-host-library / 29-device-blob framing and
  the page-anatomy conventions this inventory feeds.
- **[The Confidence & Walls Model](confidence-model.md)** — the `[CONF/PROV]` tag system every
  count here uses, and the file-absent walls (`libnrt.so`, v5 `Q7_CC_TOP`) this page records.
- **[Methodology — How This Was Reverse-Engineered](methodology.md)** — how the T0 oracle DLLs are
  driven in-process to turn `OBSERVED` into `OBSERVED-by-execution`.
- **[Toolchain Inventory & Versions](toolchain-versions.md)** — exact versions of the native Xtensa
  toolchain and host analysis tools named in §6.
- **[Codename ↔ Generation Cross-Walk](codename-crosswalk.md)** — the five-codename axis the §5
  header sets and §3 core-kind enum are keyed on.
- The **Bibliography of Source Binaries** appendix is the formal, fully-hashed citation
  list this page's working inventory feeds into.
