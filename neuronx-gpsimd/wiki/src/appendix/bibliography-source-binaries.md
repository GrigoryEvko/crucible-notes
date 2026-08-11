# Bibliography of Source Binaries

This appendix is the **formal, SHA-256-pinned citation list** for the whole reference: one row
per shipped artifact the wiki derives a fact from, with its role, its absolute on-disk location,
its hash, its byte size, its file class, the tool that decodes it, and the wiki Parts it grounds.
It is the citation-grade companion to the working catalogue in
[The Corpus, Tiers & Binary Inventory](../reference/corpus-inventory.md): that page tiers and
narrates the corpus; **this page is the bibliography you cite a hash from.**

Every hash and size below is **computed directly from the file on disk**
(`sha256sum`, `stat -c %s`, `readelf -h`). A reader who
meets any `OBSERVED` claim anywhere in the guide can come here, re-hash the named file on their
own disk, and confirm they are looking at the same bytes. Every figure on this page
matches [corpus-inventory](../reference/corpus-inventory.md) to the byte.

> **NOTE — what "binary-derived" means here.** Everything catalogued is a *shipped, redistributable*
> artifact: an ELF, a static archive, a JSON config, a Python pickle, or a C header that the AWS
> Neuron firmware / toolchain actually ships. Reading, hashing, disassembling, and (for the
> freely-callable value/decode oracles) *executing in-process* these bytes is the entire method.
> No vendor source tree was consulted; the JSON/pickle/header artifacts are themselves shipped
> files, as citeable as any ELF. See [Methodology](../reference/methodology.md) for how each is driven.

---

## How to read a row

- **SHA-256** — the first 16 hex are shown in the table for density; the **full 64-hex** for every
  artifact is listed once in [§ Full hashes](#full-sha-256-digests) so a citation can pin the
  complete digest.
- **Size** — exact byte count from `stat -c %s`, `[HIGH/OBSERVED]`.
- **Class/machine** — `readelf -h`: device firmware *containers* are **x86-64** host wrappers
  (the device code is *embedded* inside them as ELF32-Xtensa `e_machine = 0x5e = 94` blobs, not a
  standalone machine on the file itself); the ncore2gp config DLLs and `libnrt.so` are **x86-64**;
  JSON / pickle / header / `.a`-archive artifacts are tagged as such.
- **How-accessed** — the tool that turns the bytes into a fact (`readelf`/`nm`/`objdump` for host
  ELFs; the shipped `xtensa-elf-objdump --xtensa-core=ncore2gp` for device Xtensa; `ctypes` for the
  freely-callable value oracle; `jq` for JSON; `pickletools` **only** — never `pickle.load` — for
  the pickle).

All confidence tags are `[HIGH/OBSERVED]` for every hash and size, because each is measured
against the file (see [The Confidence & Walls Model](../reference/confidence-model.md)).

---

## 1. Device firmware containers (the Vision-Q7 images live *inside* these)

The GPSIMD device code that runs on the Q7 DSP is **not a standalone file**. It is a set of
ELF32-Xtensa (`e_machine = 94`) images embedded as `.rodata` data inside host x86-64 wrapper
libraries, reached through `…_SO_get` / `…_JSON_get` accessor symbols. The container *file* is
x86-64; the firmware it vends is Xtensa. `[HIGH/OBSERVED]`

| Artifact | Role | Location (`CUSTOMOP/c10/lib/…`) | SHA-256 (16) | Size (B) | Class / machine | How-accessed | Grounds Parts |
|---|---|---|---|---|---|---|---|
| `libnrtucode.a` | Static firmware library (link-time form of the device microcode loader + image set) | `…/c10/lib/libnrtucode.a` | `158dadc5c76dc049` | 10 235 636 | **`.a` archive**, 435 ELF64 x86-64 REL members | `ar t` / `nm` / `objdump` per member | P2 firmware, P12 compiler |
| `libnrtucode.so` | Stripped shared loader; carries **12** embedded ELF32-Xtensa device images | `…/c10/lib/libnrtucode.so` | `06d3f0b1630e3882` | 3 208 440 | ELF64 x86-64 DYN, **stripped** | `readelf`/IDA sidecar + `\x7fELF` carve → `xtensa-elf-objdump` | P2, P3 ISA, P4 images |
| `libnrtucode_internal.so` | Richer wrapper (946 `.symtab` syms, 66 EXTISA accessors); carries **16** embedded ELF32-Xtensa device images + the `NRTUCODE_CORE_*_NX_POOL` core-kind enum | `…/c10/lib/libnrtucode_internal.so` | `b7c67e898a116454` | 10 276 288 | ELF64 x86-64 DYN, **not stripped** | `nm`/IDA sidecar + carve → `xtensa-elf-objdump` | P2, P3, P4, P8 runtime |

> **CORRECTION — `libnrtucode_extisa.so` and `libncfw.so` are NOT standalone files in this checkout.**
> TASK #995's scope names `…_extisa.so` and `libncfw.so` among the firmware containers. Checked
> against disk (`fd --no-ignore` over the whole tree): **neither exists as a file here.**
> - The **"EXTISA"** content is the embedded blob set *inside* `libnrtucode_internal.so`
>   (`CAYMAN_Q7_POOL_PERF_EXTISA_<n>_SO_get`, …) — 66 EXTISA accessor symbols, not a separate `.so`.
> - **NCFW** (the scalar-Xtensa-LX management core) ships only as an *embedded image*; no standalone
>   `libncfw.so` is present. The flat-container `libnrtucode_extisa.so` variant ships in the sibling
>   `neuronx-runtime` corpus, so any fact sourced from it is `CARRIED` across the package boundary.
>
> This matches [corpus-inventory §3 "NOT-PRESENT"](../reference/corpus-inventory.md) exactly —
> recorded here so a citation never reaches for a file that is not on disk.

The embedded-image counts come from scanning `\x7fELF` magic and reading
`e_machine`: **16** ELF32/`e_machine=94` images in `libnrtucode_internal.so`, **12** in
`libnrtucode.so`. `[HIGH/OBSERVED]` per-library; the cross-library aggregate **≈29** is
`[HIGH/CARRIED]` (spans both libs — the dedup overlap is why it is not a simple 16+12).

---

## 2. Host runtime — `libnrt.so` (sibling `neuronx-runtime` corpus)

> **CORRECTION — `libnrt.so` is file-absent from the gpsimd checkout; it lives in the sibling
> `neuronx-runtime` tree.** [corpus-inventory §3](../reference/corpus-inventory.md) and
> [methodology §1](../reference/methodology.md) both mark the host NeuronCore runtime as
> NOT-PRESENT *in this gpsimd checkout*. It **is** present, and hashed here, in the sibling
> `neuronx-runtime` package — so host-runtime facts are `OBSERVED` against *that* corpus and
> `CARRIED` into gpsimd pages. The DWARF (`.debug_*`) corroboration leg referenced in
> methodology §2(c) reads from this very file.

| Artifact | Role | Location | SHA-256 (16) | Size (B) | Class / machine | How-accessed | Grounds Parts |
|---|---|---|---|---|---|---|---|
| `libnrt.so.2.31.24.0` | Host NeuronCore runtime (struct/enum/RTTI census; DWARF corroboration source) | `neuronx-runtime/extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrt.so.2.31.24.0` | `956382de73f4cced` | 122 956 336 | ELF64 x86-64 DYN | `nm -DC` / `readelf --debug-dump` / IDA v3 sidecar | P8 runtime (CARRIED into gpsimd) |

The package build-id is `0b044f4ce` (from the extraction directory name); the host runtime version
is `2.31.24.0`. `[HIGH/OBSERVED]`

---

## 3. ncore2gp ISS config DLLs (decode / value / timing oracles)

The nine host x86-64 DLLs under `TOOLS/ncore2gp/config/` are the executable model of the Vision-Q7
core — callable in-process, not merely readable. All ship **not stripped** (full `.symtab`, no
DWARF `.debug_*`). The `-ref-` twins are differential cross-check references. `[HIGH/OBSERVED]`

| Artifact | Role | Location (`TOOLS/ncore2gp/config/…`) | SHA-256 (16) | Size (B) | Class / machine | How-accessed | Grounds Parts |
|---|---|---|---|---|---|---|---|
| `libisa-core.so` | Canonical ISA **decode** model (opcode/iclass/operand/regfile/slot/field tables) | `…/config/libisa-core.so` | `8fe68bf462ce76ee` | 9 690 712 | ELF64 x86-64 DYN, not stripped | `nm` / `objdump -d` | P3 ISA, P6 encoding |
| `libisa-core-hw.so` | Hardware-variant ISA shim companion to `libisa-core` | `…/config/libisa-core-hw.so` | `569bddc1623d30f9` | 36 576 | ELF64 x86-64 DYN | `nm` | P3 |
| `libfiss-base.so` | **Value oracle** — 864 `module__xdref_*` per-element value leaves, **callable via `ctypes`, no license** | `…/config/libfiss-base.so` | `260b110cd59c76b0` | 12 330 016 | ELF64 x86-64 DYN, not stripped | `nm` + live `ctypes` drive | P14/P15 values |
| `libfiss-ref-base.so` | Reference twin of `libfiss-base` (differential value cross-check) | `…/config/libfiss-ref-base.so` | `1f351b545ceb5369` | 12 330 216 | ELF64 x86-64 DYN | `ctypes` differential | P14/P15 |
| `libcas-core.so` | **Cycle / timing oracle** (latency/stall/issue/fault); retirement gated by `AUTH::check_iss_licenses` (FlexNet) | `…/config/libcas-core.so` | `7f1d86da52891b3c` | 45 878 080 | ELF64 x86-64 DYN, not stripped | `nm` / `objdump`; ISS run is license-walled | P7 timing |
| `libcas-ref-core.so` | Reference twin of `libcas-core` (timing cross-check) | `…/config/libcas-ref-core.so` | `d9c9b5da324cd8ed` | 32 715 096 | ELF64 x86-64 DYN | `nm` / differential | P7 |
| `libtie-core.so` | **TIE database** core — the Tensilica instruction-extension model the config is generated from | `…/config/libtie-core.so` | `06fc43eaf3622ae1` | 51 098 208 | ELF64 x86-64 DYN | `nm` / `objdump` | P3, P6 |
| `libctype.so` | ctype / coprocessor / functional-unit classification tables | `…/config/libctype.so` | `eb79ff9fc9a8f6fe` | 388 648 | ELF64 x86-64 DYN, not stripped | `nm` | P3, P5 |
| `libtie-Xtensa-msem.so` | TIE memory-semantics helper | `…/config/libtie-Xtensa-msem.so` | `ae1afe1786dacdf9` | 258 120 | ELF64 x86-64 DYN | `nm` | P6 |

> **GOTCHA — the `.data` VMA ≠ file-offset for these DLLs (delta `0x200000`).** When `xxd`/`objdump`
> a `.data`-resident struct in any ncore2gp config DLL, subtract `0x200000` from the VMA to get the
> file offset (`.text`/`.rodata` are VMA==offset; `.data` is not). The delta is binary-specific —
> `0x200000` here, `0x3000` for the `nrtucode` pair — read the real `Addr`/`Off` columns with
> `readelf -SW` before addressing a `.data` struct. See [Methodology §5](../reference/methodology.md).

> **QUIRK — `libnrtucode_internal.so` has zero C++ RTTI; this DLL set retains a full `.symtab`.**
> The device firmware is `-fno-rtti` (`_ZTV`/`_ZTI`/`_ZTS` count = 0), so its class hierarchy is
> not recoverable from RTTI scaffold. The config DLLs are the opposite: named-symbol-rich, which is
> exactly why `nm` + `ctypes` reach `OBSERVED-by-execution` on the value lane.

---

## 4. customop-lib arch-ISA headers + `instruction_mapping.json` (4 generations)

The cleartext per-generation specification. Four **unified** generations ship as
`CUSTOMOP/c10/include/neuron_<gen>_arch_isa/` (`aws_neuron_isa_*` headers); **Tonga (v1)** is the
pre-unified outlier under `arch-headers/tonga/` + `arch-isa/`, *not* one of the four. `[HIGH/OBSERVED]`

| Artifact | Role | Location (`CUSTOMOP/c10/include/…`) | SHA-256 (16) | Size (B) | Class | How-accessed | Grounds Parts |
|---|---|---|---|---|---|---|---|
| `neuron_sunda_arch_isa/` (100 `.h`) + `tpb/instruction_mapping.json` | v2 SUNDA ISA headers; `struct2opcode` = 89 | `…/neuron_sunda_arch_isa/tpb/instruction_mapping.json` | `f5d5c888b7e0c28d` | 13 619 (json) | C header set + JSON | `gcc -E` `offsetof`/`sizeof` proof; `jq` on JSON | P3, P5, P6 |
| `neuron_cayman_arch_isa/` (111 `.h`) + `tpb/instruction_mapping.json` | v3 CAYMAN ISA headers (the focus arch); `struct2opcode` = 99 | `…/neuron_cayman_arch_isa/tpb/instruction_mapping.json` | `4e9c1f6abe0d015d` | 14 482 (json) | C header set + JSON | `gcc` compile-verify; `jq` | P3, P5, P6, P12 |
| `neuron_mariana_arch_isa/` (120 `.h`) + `tpb/instruction_mapping.json` | v4 MARIANA ISA headers; `struct2opcode` = 108 | `…/neuron_mariana_arch_isa/tpb/instruction_mapping.json` | `cedc8bbcfcd4b231` | 15 483 (json) | C header set + JSON | `gcc` compile-verify; `jq` | P3, P5, P6 |
| `neuron_maverick_arch_isa/` (126 `.h`) + `tpb/instruction_mapping.json` | v5 MAVERICK ISA headers; `struct2opcode` = 114 | `…/neuron_maverick_arch_isa/tpb/instruction_mapping.json` | `6d956f84051c8bed` | 16 158 (json) | C header set + JSON | `gcc` compile-verify; `jq` | P3, P5, P6 |

The monotone climb **89 → 99 → 108 → 114** is the OBSERVED per-generation opcode growth; each
`instruction_mapping.json` has top-level keys `{struct2opcode, struct2pseudo_opcode}` with
`struct2pseudo_opcode = 2` in every gen. The header `.h` counts (100/111/120/126) and JSON sizes
(13 619 / 14 482 / 15 483 / 16 158) match [corpus-inventory §5](../reference/corpus-inventory.md)
to the byte. `[HIGH/OBSERVED]`

The firmware-side **core-kind enum** that ties these names to the loader is read from
`libnrtucode_internal.so` strings: `NRTUCODE_CORE_{SUNDA,CAYMAN,MARIANA,MARIANA_PLUS,MAVERICK}_NX_POOL`
— 5 NX-POOL core kinds. `[HIGH/OBSERVED]`

---

## 5. CSR JSON + RTL address maps + the Maverick address-map pickle

| Artifact | Role | Location | SHA-256 (16) | Size (B) | Class | How-accessed | Grounds Parts |
|---|---|---|---|---|---|---|---|
| Cayman CSR JSON set | Cayman control/status-register schema — per-block CSR field defs (`tpb`, `sdma`, `hbm`, `pcie`, `d2d`, `xtensa_q7`, `xtensa_nx`, …) + RTL/address maps | `extracted/nested/cayman-arch-regs_tgz/csrs/**/*.json` | (85 JSON files; 124 files / 76 MB tree) | per-file | **JSON tree** | `jq` per block | P13 control/CSR |
| Cayman RTL address maps | Per-block address decode (`address_map/`, `output/address_map/`, `sprot/`, `intc/`) | `extracted/nested/cayman-arch-regs_tgz/{address_map,output/address_map}/` | (in 76 MB tree) | JSON/RTL | `jq` | P13 address |
| Maverick `al_address_map_db.pkl` | Maverick **address-map value oracle** (v5 address decoding) | `CUSTOMOP/c10/include/arch-headers/maverick/ext/al_address_map_db.pkl` | `e5f4dae2d5f16ad6` | 216 631 794 | **Python pickle** (proto 4, magic `80 04`) | `pickletools` / `stat` / `sha256sum` **only — never `pickle.load`** | P13 address (Maverick) |
| Maverick `al_address_map_db.json` | JSON twin of the Maverick address DB | `CUSTOMOP/c10/include/arch-headers/maverick/ext/al_address_map_db.json` | `056d7b6123a5188b` | 514 276 583 | **JSON** | `jq --stream` | P13 address (Maverick) |

> **GOTCHA — `al_address_map_db.pkl` is Maverick, not Cayman; and never deserialize it.**
> The pickle lives under the **`maverick/`** customop arch-headers (`…/arch-headers/maverick/ext/`)
> — *not* in the `cayman-arch-regs` tarball. Its first two bytes are `80 04`
> (pickle `PROTO 4`), confirming a valid pickle; inspect it with `pickletools.genops` / `stat` /
> `sha256sum` **only**. A `pickle.load` of an untrusted 207 MB blob executes arbitrary `__reduce__`
> opcodes — it is treated as opaque bytes here. `[HIGH/OBSERVED]` (path provenance + magic).

---

## 6. Toolchain — the native Xtensa device disassembler + ISS cores

| Artifact | Role | Location (`TOOLS/XtensaTools/…`) | SHA-256 (16) | Size (B) | Class / machine | How-accessed | Grounds Parts |
|---|---|---|---|---|---|---|---|
| `xtensa-elf-objdump` | The **device disassembler** — decodes Xtensa `e_machine=94` objects/firmware with the `ncore2gp` core config (`GNU Binutils 2.34.20200201 Xtensa Tools 14.09`) | `…/XtensaTools/bin/xtensa-elf-objdump` | `d43bd4fad891e695` | 1 337 968 | ELF64 x86-64 **EXEC**, stripped | run with `--xtensa-core=ncore2gp` | every device-ISA Part (P3/P4/P6) |
| `libsimxtcore.so` (default `iss/`) | Xtensa ISS core (cycle-accurate sim); 11 per-toolchain variants ship (`iss/`, `iss-GCC-{4.8,6.2,6.3,6.3-XCM,7.3,9.2,9.3,10.2,11.2}`, `iss-clang-10`) | `…/XtensaTools/lib/iss/libsimxtcore.so` | `4e935ae00de67c82` | 2 804 296 | ELF64 x86-64 DYN | host `nm`; ISS run is license-walled | P7 timing |

> **QUIRK — `xtensa-elf-objdump` has no default core.** Invoked bare it errors
> *"there is no Xtensa core registered as the default"*; you must
> `export XTENSA_CORE=ncore2gp` (the only core in the bundled registry). It is the *Vision* config —
> the **wrong** config for the scalar Xtensa-LX **NCFW** management core, which must be decoded with
> the manual LX `op0 ∈ {e,f} ⇒ 3-byte` length rule (resync at `retw.n`). Using `ncore2gp` on NCFW
> bytes produces the spurious "~26–28% FLIX" artifact. See
> [Toolchain Inventory & Versions §B.1](../reference/toolchain-versions.md).

Plain host GNU binutils (`objdump`/`nm`/`readelf`/`sha256sum`, `2.46-3.fc44`) decode every x86-64
host ELF and `.a` archive above; the device `xtensa-elf-objdump` is reserved for `e_machine=94`
device code. `[HIGH/OBSERVED]`

---

## 7. Sibling cross-check corpora (T2 — corroboration only)

Three independently-shipped AWS Neuron corpora live in the same repo tree, used **only** to
corroborate a gpsimd-internal fact from a separate build — never as the sole source of one.
`[HIGH/OBSERVED]`

| Corpus | Role | Location | How-accessed | Grounds Parts |
|---|---|---|---|---|
| `neuronx-cc` | Neuron compiler — confirms codename↔generation mapping, opcode mnemonics, the host compile contract | `neuronx-cc/` (present, 236 GB tree) | `nm`/`objdump`/IDA sidecars | cross-check P3/P12 |
| `neuronx-collectives` | Collectives runtime — corroborates device collective firmware (`Q7_CC_TOP`) interface + host compose pipeline | `neuronx-collectives/` (present, 297 MB) | `nm`/IDA sidecars | cross-check P10 |
| `nki-0.3.0` | NKI kernel surface — confirms opcode/intrinsic names + user-facing instruction vocabulary | `neuronx-misc/…/nki-0.3.0+23928721754.g18aa1271-cp312-…` wheel | `unzip`/`jq` | cross-check P3/P12 |

The `libnrt.so` host runtime (§2) is itself a sibling-corpus artifact (`neuronx-runtime`),
catalogued separately because it is the host-runtime *primary* source, not merely a cross-check.

---

## 8. Lawful-interop posture

Every artifact catalogued here is an **already-shipped, redistributable** AWS Neuron package
component, and every result derived from it is the product of **static analysis** — reading,
hashing, disassembling, and (for the freely-callable value/decode oracles) **executing
in-process** — of those shipped bytes. No vendor source tree was consulted; no design document was
read; no proprietary firmware was decrypted or circumvented. The work is authored under the
**interoperability research exemption of 17 U.S.C. §1201(f)** (and the analogous EU
software-directive provisions): the sole purpose is to document the interfaces and behaviour
necessary to build an **independently interoperable** Vision-Q7-compatible GPSIMD engine,
toolchain, runtime, or simulator.

Two license walls are *reported, not crossed*: the cycle/timing oracle (`libcas-core.so`,
`libsimxtcore.so`) gates instruction *retirement* on a FlexNet check (`AUTH::check_iss_licenses`),
and **no license circumvention is performed or documented** — the wall is recorded as a wall (see
[The Confidence & Walls Model](../reference/confidence-model.md)). The freely-callable value lane
(`libfiss-base.so`) requires no license and is the only oracle this reference executes. The 207 MB
Maverick pickle is treated as **opaque bytes** (`pickletools` only — never deserialized), so no
arbitrary-code path is reached. `[HIGH/OBSERVED]`

---

## Full SHA-256 digests

The complete 64-hex digest for every hashed artifact (all `[HIGH/OBSERVED]`):

| Artifact | SHA-256 (full) | Size (B) |
|---|---|---|
| `libnrtucode.a` | `158dadc5c76dc0491b9243091458b43c2d59091ba3ba5a727206915bd7bd6130` | 10 235 636 |
| `libnrtucode.so` | `06d3f0b1630e38828ace79d3c9f3123ac14b3ea4c5cde4aa906a31b3e82ccce5` | 3 208 440 |
| `libnrtucode_internal.so` | `b7c67e898a116454a8e0ce257b1d6523a23ffa237a6ec21021ecb70632fc329b` | 10 276 288 |
| `libnrt.so.2.31.24.0` | `956382de73f4cced5d9a0dc040ca82843fb37aef00d5bf2241f343ff02cd59a6` | 122 956 336 |
| `libisa-core.so` | `8fe68bf462ce76ee17dfbe2167ff8443d473a66385ed115364e9677bf143e451` | 9 690 712 |
| `libisa-core-hw.so` | `569bddc1623d30f9f39f6a690cefe674297bedb18d3740af58c8c9df08bb7988` | 36 576 |
| `libfiss-base.so` | `260b110cd59c76b090cbdeb4d5d90f5245be34792618c023ab963ce108d3cc94` | 12 330 016 |
| `libfiss-ref-base.so` | `1f351b545ceb5369fd51b3d559c456fbf5142db5d31c4d4afa0474897c097ac4` | 12 330 216 |
| `libcas-core.so` | `7f1d86da52891b3c65533d394ace4902b101536fedb31dff7ed976dc40b1041a` | 45 878 080 |
| `libcas-ref-core.so` | `d9c9b5da324cd8ed679e0ba01361f658646417a8eeca582cf9e4bb74ff6c0991` | 32 715 096 |
| `libtie-core.so` | `06fc43eaf3622ae1d010150537906b473798cce31dc32fa89c1827bef35c2b24` | 51 098 208 |
| `libctype.so` | `eb79ff9fc9a8f6fec7394eade074c16f2b3a1d4bebcaecd5cbc7e718c5a3f91c` | 388 648 |
| `libtie-Xtensa-msem.so` | `ae1afe1786dacdf9f8c7d7b395e2e42eb183cde2d5465a769f662e4c642ea0f7` | 258 120 |
| `xtensa-elf-objdump` | `d43bd4fad891e695bf9e459bac9e5230ed16df0780291b469a202a9bc65a0d0e` | 1 337 968 |
| `libsimxtcore.so` (`iss/`) | `4e935ae00de67c8282f80b07b5938bb74db4ba9c10ab2423b968966f66032432` | 2 804 296 |
| `sunda/instruction_mapping.json` | `f5d5c888b7e0c28d8dcaa0b0dbbd2d2ea796eaa4fede86facd555a43ce56b81b` | 13 619 |
| `cayman/instruction_mapping.json` | `4e9c1f6abe0d015d4cb8e99c7556c5ead090faf7e2effbc3cc16c5700d0199ae` | 14 482 |
| `mariana/instruction_mapping.json` | `cedc8bbcfcd4b23143b1e9360251bdf965ec64016da4d02ced5006723b493a09` | 15 483 |
| `maverick/instruction_mapping.json` | `6d956f84051c8bed1c0da946ed907e1a10e17a3928521424587353376266c932` | 16 158 |
| `al_address_map_db.pkl` (Maverick) | `e5f4dae2d5f16ad6aff5e7b5225f6cddca78aa91b580e91c9d600837e72d1b75` | 216 631 794 |
| `al_address_map_db.json` (Maverick) | `056d7b6123a5188bc350b5fa3e27b6e5b0012d794800942ce35ef3c745fd3d11` | 514 276 583 |

> **Path convention.** `CUSTOMOP/` =
> `neuronx-gpsimd/extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/`;
> `TOOLS/` = `neuronx-gpsimd/extracted/nested/gpsimd_tools_tgz/tools/`. `extracted/` is git-ignored
> — locate files there with `fd --no-ignore` or an absolute path. Package versions (themselves
> OBSERVED, from the extraction directory names): customop-lib `0.21.2.0`; tools `0.21.0.0-bc9b5fad5`;
> runtime `2.31.24.0-0b044f4ce`.

---

## Cross-references

- **[The Corpus, Tiers & Binary Inventory](../reference/corpus-inventory.md)** — the tiered,
  role-narrated working catalogue this bibliography pins to (the reference for every figure here).
- **[Methodology — How This Was Reverse-Engineered](../reference/methodology.md)** — how each
  artifact above is driven (the value-oracle `ctypes` drive, the device `objdump`, the `offsetof`
  compile-verify, the four verification gotchas).
- **[Toolchain Inventory & Versions](../reference/toolchain-versions.md)** — the exact
  `xtensa-elf-objdump` / `ncore2gp` / clang-10 / FlexLM version pinning for §6.
- **[The Confidence & Walls Model](../reference/confidence-model.md)** — the `[HIGH/OBSERVED]` tag
  system and the file-absent / license walls this page records.
- **[Codename ↔ Generation Cross-Walk](../reference/codename-crosswalk.md)** — the five-codename
  axis the §4 header sets and §1 core-kind enum are keyed on.
