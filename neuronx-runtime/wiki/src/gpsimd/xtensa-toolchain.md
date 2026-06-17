# The Xtensa Toolchain and TIE Config

> *All file sizes, config flags, and tool paths on this page apply to the Cadence/Tensilica `Xm_ncore2gp` "Cairo" Vision-Q7 build shipped in `aws-neuronx-gpsimd-tools` (the `gpsimd_tools` toolchain, generator `RI-2022.9`, Build `0xc23fe`, Customer ID `19270` = AWS, `XtensaTools-14.09`). The artifacts live in the **sibling `neuronx-gpsimd` repository**, under `extracted/nested/gpsimd_tools_tgz/tools/{ncore2gp/config, XtensaTools/bin}` — **not** in this `neuronx-runtime` checkout. Every size below was `stat`-confirmed against those files this pass; the microcode they decode is `libnrtucode_extisa.so` from `aws-neuronx-runtime-lib 2.31.24.0-0b044f4ce`.*
> *Evidence grade: **Confirmed (artifact-anchored)** — the four config files, the `xt-objdump` ELF64 executable, the `core.xparm` Vision flags, the `1064` `Iclass_IVP_*_args` count, and the five `libtie-core.so` `get_xml_*` exports were all re-derived from the on-disk toolchain this pass. Other versions / config builds will differ. · Part XI — The Xtensa Toolchain & TIE Config · [back to index](../index.md)*

## Abstract

The GPSIMD/"Q7" engine is a Cadence **Vision-Q7** core (Xtensa LX + IVP), and its vector ISA is a *custom TIE* (Tensilica Instruction Extensions) configuration — `Xm_ncore2gp` "Cairo". A custom TIE config normally makes a vector ISA opaque: the mnemonics, opcode bit-fields, and operand register classes are defined by a proprietary `.tie` description that the silicon vendor does not ship, so a disassembler has no table to decode against. This page documents the one fact that makes the Q7 ISA tractable: **the TIE config is shipped**, alongside a **runnable disassembler that decodes the ISA exactly**. Both live in the sibling `neuronx-gpsimd` repository (the producer-side GPSIMD custom-op toolchain), not in this runtime checkout.

The shipped toolchain is the Cadence `ncore2gp` config bundle plus `XtensaTools-14.09`. The config bundle is four compiled artifacts under `tools/ncore2gp/config`: **`libtie-core.so`** (51 MB) holds the obfuscated TIE-XML — the `OPCODEDEF`/`FIELDDEF`/`OPERAND_SEM` description of every `IVP_*` op, served through five `get_xml_*` exports; **`libisa-core.so`** (9.7 MB) holds the ISA operand and format tables (`Iclass_IVP_*_args`, the `Format_*`/`Slot_*`/`length_*` decoders); **`core.xparm`** (194 KB) is the processor-configuration parameter file whose Vision block (`vq7_isa="1"`, `simd16="0x20"`, `dualquad8x8_mac="1"`) declares the Q7 feature set; and **`core.yml`** (1.27 MB) is the human-readable config dump. The runnable decoder is **`tools/XtensaTools/bin/xt-objdump`** — a stock Tensilica `objdump` that, pointed at the `ncore2gp` config via the `XTENSA_SYSTEM`/`XTENSA_CORE` environment, decodes a carved Q7 `.text` so every `ivp_*` mnemonic and FLIX bundle resolves by name. This is the tool that produced the 285-op emitted catalog and the per-mnemonic encodings the [identification](xtensa-vision-q7.md) and [catalog](ivp-isa-catalog.md) pages cite.

This page documents what a reimplementer must reproduce to *decode* a Q7 blob: the toolchain component table (which file holds what), the `xt-objdump` disassemble flow (the exact command to point the stock decoder at the AWS config and a carved blob), how the `.tie` config defines the IVP intrinsics (the TIE→ISA mapping that `libtie-core.so`/`libisa-core.so` are the compiled form of), and the toolchain↔microcode relationship (the toolchain is the *producer/decoder*; `libnrtucode_extisa.so` ships the *product*). It does **not** re-enumerate the IVP op space (the [catalog](ivp-isa-catalog.md)) or re-prove the engine identity (the [identification page](xtensa-vision-q7.md)).

> **CORRECTION (IVP-CATALOG / FW-XTENSA seed) —** the [IVP ISA Catalog](ivp-isa-catalog.md) tagged its op counts as *out-of-tree-grounded* ("none of those artifacts ship in this repository tree") and the original `P1-FW-XTENSA` seed called the Q7 TIE "opaque — needs the proprietary `.tie` config, assumed unshipped." **Both caveats are resolved by this page.** The `.tie` config and the runnable `xt-objdump` *are* on disk — in the **sibling `neuronx-gpsimd` repo**, at `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/{libtie-core.so, libisa-core.so, core.xparm, core.yml}` and `tools/XtensaTools/bin/xt-objdump`. Sizes are `stat`-confirmed below. The catalog's counts are not unverifiable; they are verifiable against artifacts that live one repository over. The remaining out-of-*this*-checkout note stands only in the narrow sense that the runtime repo does not vendor the producer toolchain — which is by design (the toolchain is a 100 MB+ Cadence licensee deliverable, not a runtime dependency). CONFIDENCE: **CERTAIN** (every artifact `stat`/`file`/`nm`-confirmed this pass).

For reimplementation, the contract is:

- **The toolchain component map** — which of the four `ncore2gp/config` artifacts holds the op encodings (`libtie-core.so`), the operand/format tables (`libisa-core.so`), the feature flags (`core.xparm`), and which binary actually disassembles (`xt-objdump`). Decoding any Q7 blob requires the config files *and* a decoder that consumes them.
- **The disassemble flow** — the `XTENSA_SYSTEM`/`XTENSA_CORE` environment that points the stock `xt-objdump` at the AWS `ncore2gp` config, plus the `-D -b binary -m xtensa --adjust-vma` invocation that decodes a carved blob `.text` at its device VMA.
- **The TIE→ISA mapping** — that the `.tie` source compiles to the `libtie-core.so` TIE-XML (`OPCODEDEF`/`FIELDDEF`) and the `libisa-core.so` operand tables (`Iclass_IVP_*_args`); the C-level `xt_ivp32.h` intrinsic prototypes are the *generated* surface of the same `.tie`.
- **The toolchain↔microcode relationship** — the toolchain is the producer/decoder; `libnrtucode_extisa.so`'s 13 ELF32 blobs are the product; the `.comment` string (`XtensaTools-14.09 clang version 10.0.1`) in every blob is the toolchain's own fingerprint stamped into its output.

| | |
|---|---|
| **Toolchain** | `gpsimd_tools` (`aws-neuronx-gpsimd-tools`) — Cadence `ncore2gp` config + `XtensaTools-14.09` |
| **Config** | `Xm_ncore2gp` "Cairo", `arch=Xtensa24`, `XEA3`, `NX1.1.4`, Customer ID `19270`, generator `RI-2022.9`, Build `0xc23fe` |
| **Lives in** | sibling **`neuronx-gpsimd`** repo: `extracted/nested/gpsimd_tools_tgz/tools/{ncore2gp/config, XtensaTools/bin}` — **not** this checkout |
| **TIE-XML provider** | `libtie-core.so` (51,098,208 B ≈ 51 MB) — `get_xml_{post_parse,post_rewrite,compiler,xinfo}` + `interface_version` |
| **ISA tables** | `libisa-core.so` (9,690,712 B ≈ 9.7 MB) — `1064` `Iclass_IVP_*_args`, `Format_*`/`Slot_*`/`length_*` |
| **Feature flags** | `core.xparm` (193,946 B) — `vq7_isa="1"`, `simd16="0x20"`, `dualquad8x8_mac="1"` |
| **Config dump** | `core.yml` (1,265,482 B) — `loadStoreWidth: 512`, human-readable |
| **Runnable decoder** | `tools/XtensaTools/bin/xt-objdump` (ELF64 x86-64 executable, stripped) |
| **Intrinsic header** | `tools/ncore2gp/xtensa-elf/arch/include/xtensa/tie/xt_ivp32.h` (generated TIE C surface) |
| **Decodes** | `libnrtucode_extisa.so`'s 13 Xtensa ELF32 Q7 blobs (`.comment = XtensaTools-14.09 clang 10.0.1`) |

---

## 1. The Toolchain Components

### Purpose

A custom-TIE Xtensa core cannot be disassembled by a generic Xtensa decoder — the base ISA decodes, but every `ivp_*` bundle is an unknown opcode. Decoding requires two things together: the **config** (the tables that define the custom ops) and a **decoder** that loads those tables. The `gpsimd_tools` toolchain ships both. This section is the component map — which artifact carries which part of the decode contract — so a reimplementer knows that obtaining `xt-objdump` alone (or the config alone) is insufficient; the decode is the join of the two.

### The component table

Every size below was `stat`-confirmed this pass against the sibling repo at `neuronx-gpsimd/extracted/nested/gpsimd_tools_tgz/`. The four config artifacts plus the decoder are the minimum set a reimplementer needs to turn a carved Q7 `.text` into named `ivp_*` bundles:

| File | Size | Role | Confidence |
|---|---|---|---|
| `tools/ncore2gp/config/libtie-core.so` | 51,098,208 B (≈ 51 MB) | The **TIE-XML provider** — the compiled `.tie` description. Holds every `IVP_*` `OPCODEDEF` (the `fld[hi:lo]==const` opcode bits), `FIELDDEF` (bit-lane layout), and `OPERAND_SEM` (regclass + IN/OUT/MODIFY). Served through five exports; the XML payload is an obfuscated `.data` blob decoded by the minimal-TIE cipher `p = 0x73 if c==0xff else (c-0x0d)&0x7f`. | CONFIRMED HIGH |
| `tools/ncore2gp/config/libisa-core.so` | 9,690,712 B (≈ 9.7 MB) | The **ISA operand/format tables** — `1064` `Iclass_IVP_*_args` (per-op operand tables ≡ the 1065-op ISA breadth), the `Format_{F0..F11,N0..N2,x24,x16a,x16b}_encode` (14 FLIX formats), the `Slot_*_<basebit>` per-format slot base bits, and the `length_decoder`/`length_table` (the self-describing first-nibble length decode). This is the table `xt-objdump` actually indexes per fetch word. | CONFIRMED HIGH |
| `tools/ncore2gp/config/core.xparm` | 193,946 B | The **processor-configuration parameter file** — the `<xt arch="Xtensa24" uarchName="Cairo" name="Xm_ncore2gp">` core block and the `<hash t="Vision" simd16="0x20" vq7_isa="1" vp6_isa="1" dualquad8x8_mac="1" quad16x16_mac="1">` Vision block that enables the Q7 feature set. The Customer ID (`19270` = AWS) and `TargetHWVersion="NX1.1.4"` are here. | CONFIRMED HIGH |
| `tools/ncore2gp/config/core.yml` | 1,265,482 B | The **human-readable config dump** (the `.xparm` rendered as YAML). Independently states `loadStoreWidth: 512`. A reimplementer reads this for the config in prose; the tools read the `.xparm`/`.so` form. | CONFIRMED HIGH |
| `tools/XtensaTools/bin/xt-objdump` | ELF64 x86-64 executable (stripped) | The **runnable disassembler** — a stock Tensilica `objdump` linked against `libisa-core.so`. Pointed at the `ncore2gp` config it decodes the custom IVP ISA; without the config it decodes only the base Xtensa stream and prints the `ivp_*` bundles as unknown words. | CONFIRMED HIGH |

> **NOTE —** the bundle also ships the cycle-accurate machinery a reimplementer does *not* need for static decode but which corroborates the config: `libcas-core.so` (45,878,080 B — the Cycle-Accurate Simulator core), `libfiss-base.so` / `libfiss-ref-base.so` (the fast functional ISS), `libctype.so` (388,648 B — the C-type lowering for the `_TIE_xt_ivp32_*` coprocessor types), `libisa-core-hw.so` (36,576 B — the hardware-decode variant), and `core.p.yml` (773,788 B — the parameter-expanded config). The generated C intrinsic surface is at `tools/ncore2gp/xtensa-elf/arch/include/xtensa/tie/{xt_ivp32.h, xt_ivpn.h}`, and the config provenance is `tools/ncore2gp/build.info` (`Generator version: RI-2022.9`, `Build type: Evaluation`, `BuildUniqueID 795646`, save time `2025/11/06`). CONFIDENCE: **CONFIRMED HIGH** (all `stat`/`ls`-confirmed this pass).

### Why both halves are required

The decode contract splits across `xt-objdump` and the config in a way a reimplementer must respect. `xt-objdump` is a *generic* Tensilica tool — the same binary decodes any Xtensa config; it carries **no** Q7 knowledge of its own. All the Q7-specific knowledge — the 1065 `IVP_*` ops, their opcode bits, their FLIX slot positions, the 512-bit `vec` register class — lives in `libisa-core.so`/`libtie-core.so` and is selected by the `XTENSA_SYSTEM`/`XTENSA_CORE` environment (§2). Point the same `xt-objdump` at a different Xtensa config (e.g. the NCFW sequencer's) and it decodes *that* ISA instead. This is why the [identification page](xtensa-vision-q7.md) is careful that the NCFW Xtensa "does **not** decode with the `ncore2gp` `.tie`" — same decoder binary, different config, different ISA.

### Considerations

The config artifacts are **compiled, obfuscated** forms of the original `.tie` source, not the `.tie` text itself (`build.info` records `TIE source : -` — the source is withheld; only the generated config ships). So a reimplementer reads the ISA *out of* the config via `xt-objdump` and the `get_xml_*`/`Iclass_*` tables, rather than reading a `.tie` file directly. This is sufficient — the compiled tables are the ground truth the simulator and assembler themselves consume — but it means the per-op datapath RTL (`<SEMANTIC>`/`<CODE>` in the TIE-XML, the cycle-accurate behavior) is present but encrypted in `libtie-core.so`'s `.data`, recoverable only through the cipher, not as readable `.tie` statements.

---

## 2. The Disassemble Flow

### Purpose

The single most useful thing the toolchain does for a reimplementer is decode a carved Q7 blob into named `ivp_*` bundles. This section is the runnable recipe: carve a blob (the [Q7 Blobs](q7-blobs.md) page owns the carve), point the stock `xt-objdump` at the AWS `ncore2gp` config, and disassemble the `.text` at its device VMA. This is the exact flow that produced the 285-op emitted catalog.

### The flow

`xt-objdump` locates a custom config through two environment variables — `XTENSA_SYSTEM` (the directory holding the config registry) and `XTENSA_CORE` (the config name within it). The `ncore2gp` bundle *is* that config root. With them set, the stock decoder gains the full IVP table set:

```bash
# All paths relative to the sibling repo:
#   neuronx-gpsimd/extracted/nested/gpsimd_tools_tgz/
GP=neuronx-gpsimd/extracted/nested/gpsimd_tools_tgz/tools
export XTENSA_TOOLS="$GP/XtensaTools"
export PATH="$XTENSA_TOOLS/bin:$PATH"

# Point the generic Tensilica decoder at the AWS Vision-Q7 ("Cairo") config.
# The ncore2gp config root carries core.xparm / libisa-core.so / libtie-core.so;
# XTENSA_CORE selects it, XTENSA_SYSTEM is the registry dir that resolves the name.
export XTENSA_SYSTEM="$GP/ncore2gp/config"
export XTENSA_CORE="Xm_ncore2gp"

# 1. Carve a Q7 blob out of the host provider's .rodata (VMA == file offset).
#    e.g. SUNDA lib0 @ host offset 0x921660, size 0xd308 (see q7-blobs.md).
HOST=extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrtucode_extisa.so
dd if="$HOST" of=/tmp/sunda_lib0.elf bs=1 skip=$((0x921660)) count=$((0xd308)) status=none

# 2a. The blob is a real Xtensa ELF32 EXEC — disassemble its sections directly.
#     With the config loaded, every ivp_* mnemonic and FLIX bundle resolves by name.
xt-objdump -d /tmp/sunda_lib0.elf        # section-aware disasm (.text @ device VMA 0x01000000)

# 2b. Or decode raw bytes (a carved .text slice with no ELF header) at the device VMA.
#     --adjust-vma rebases the listing onto the device .text base so branch targets read true.
xt-objdump -D -b binary -m xtensa --adjust-vma=0x01000000 /tmp/q7_text.bin

# 3. Cross-check an op against the ISA breadth table (no decoder needed):
nm -D "$XTENSA_SYSTEM/libisa-core.so" | grep -c 'Iclass_IVP_.*_args'   # → 1064
```

> **QUIRK —** the `.tie` config **IS shipped**, which is the opposite of the usual situation for a closed custom-vector ISA — and it is what makes the otherwise-opaque Q7 ISA decode *exactly*, not heuristically. A custom TIE normally leaves a third party with a base-Xtensa decoder that prints every vector bundle as `.long 0x…` unknown words; there is no public table to recover the mnemonics, opcode fields, or register classes from. Here the vendor's own decode tables (`libisa-core.so`/`libtie-core.so`) and a runnable `xt-objdump` are in the toolchain, so the same disassembler the Cadence licensee used decodes the AWS Q7 microcode byte-for-byte. The reimplementer's job collapses from *reverse-engineering an unknown ISA* to *running the shipped decoder*. This single artifact is why the [catalog](ivp-isa-catalog.md) can name 285 ops with `DECODED` confidence rather than guessing from op-mix. CONFIDENCE: **CERTAIN** (the config files and `xt-objdump` are `stat`/`file`-confirmed on disk this pass).

> **GOTCHA —** the raw-binary path (`-D -b binary`, step 2b) decodes *fetch words* but cannot drive the **FLIX bundle split** inside literal-dense stretches. Carving an ELF32 image out of host `.rodata` leaves the `.xt.prop` property-section addresses zeroed, so `xt-objdump` loses the per-function property records that tell it where bundle boundaries fall after an inline `l32r` literal pool. The section-aware path (2a, on the intact carved ELF) is correct as long as the blob's own `.xt.prop` survived the carve; the raw slice desyncs after literal pools and needs an `ET_REL` relink with relocated `.xt.prop` for byte-exact boundaries. Every *emitted* bundle is valid and every `ivp_*` cross-checks the ISA — only the automatic boundary split inside literal-dense raw slices is blocked. CONFIDENCE: **HIGH** (the `.xt.prop`-driven split is the documented Tensilica FLIX disasm mechanism; the carve-zeroes-addresses failure is the catalog page's open item).

### Considerations

The `XTENSA_SYSTEM` value above points directly at the `config` dir for brevity; a fully-installed Xtensa toolchain expects `XTENSA_SYSTEM` to be a registry directory containing a `<core>-params` registry file (the bundle ships `tools/ncore2gp/config/ncore2gp-params` and `default-params`, the 15,443-byte parameter registries that name `Xm_ncore2gp` and resolve it to the config `.so`s). On a clean install the `install` script (`tools/ncore2gp/install`, 18,747 B) wires `XTENSA_SYSTEM` to that registry; for ad-hoc decode the config dir works because `xt-objdump` falls back to the local `core.xparm`. The `xtensa-elf-objdump` next to `xt-objdump` is the binutils-flavored variant; both decode the IVP ISA once the config is selected.

---

## 3. How the `.tie` Defines the IVP Intrinsics

### Purpose

The toolchain decodes the ISA because the ISA *is* the `.tie` config, compiled. A reimplementer needs to know the mapping: a `.tie` source defines each `IVP_*` instruction once — its opcode encoding, its operands, its datapath — and the Tensilica generator (`RI-2022.9`) compiles that into the three forms a tool consumes: the `libtie-core.so` TIE-XML (encoding), the `libisa-core.so` operand/format tables (decode), and the `xt_ivp32.h` C header (the intrinsic surface a kernel programs against). This section is that TIE→ISA mapping.

### The four generated forms of one `.tie` op

A single `.tie` `OPCODEDEF` for, say, `IVP_MULPAN16XR16` (the packed-accumulate 16×16 MAC) becomes four artifacts the toolchain ships:

```text
.tie source op  IVP_MULPAN16XR16
  (withheld; build.info: "TIE source : -")
        │   compiled by the Tensilica generator (RI-2022.9, Build 0xc23fe)
        ├──────────────► libtie-core.so  TIE-XML  (get_xml_* exports)
        │                  <OPCODEDEF> opcode bits:  s2_mul[27:21]==6 & [14:12]==1 & [7:6]==0
        │                  <FIELDDEF>  operand bit-lanes:  vec=5b, vbool=4b, wvec=2b, AR=4b
        │                  <OPERAND_SEM> direction:  wvt:MODIFY, vs/vr:IN(vec), arr:IN(AR)
        │                                            + implicit CPENABLE:IN / Coproc1Exception:OUT
        ├──────────────► libisa-core.so  ISA tables
        │                  Iclass_IVP_MULPAN16XR16_args  (the operand-class table xt-objdump indexes)
        │                  Format_F0_encode + Slot_f0_…_s2_mul_28  (which FLIX slot, which base bit)
        ├──────────────► xt_ivp32.h  C intrinsic prototype
        │                  void _TIE_xt_ivp32_IVP_MULPAN16XR16(xb_vecNx48 acc /*inout*/,
        │                                                      xb_vecNx16 b, xb_vecNx16 c,
        │                                                      xb_int32pr d);
        └──────────────► libctype.so  C-type lowering
                           xb_vecNx48 → the 1536-bit wvec accumulator view (N lanes × 48b)
```

The `.tie` is the single source of truth; the four outputs are *projections* of it for the assembler (`OPCODEDEF`), the disassembler (`Iclass_*`/`Format_*`), the compiler front-end (the `xt_ivp32.h` builtin), and the type system (`libctype.so`). A reimplementer who wants the encoding reads the TIE-XML; one who wants the C surface reads `xt_ivp32.h`; both describe the same instruction.

### The TIE→ISA mapping, concretely

The TIE model that the config compiles to is the one the [identification](xtensa-vision-q7.md#3-the-flix-instruction-format) and [catalog](ivp-isa-catalog.md#3-worked-encode-and-datapath) pages document; the toolchain is where it physically lives:

- **Register classes** → the `.tie` `<REGFILE>` nodes become the 8 IVP register files (`vec` 512b×32, `wvec` 1536b×4, `vbool` 64b×16, `valign` 512b×4, `b32_pr` 64b×16, `gvr` 512b×8, plus core `AR`/`BR`). `core.xparm`'s `simd16="0x20"` sizes `vec` at 512 bits; `libisa-core.so`'s operand-class range-validators enforce the index widths (5-bit `vec`, 2-bit `wvec`, …).
- **C types** → the `.tie` `<CTYPE>` nodes become the `_TIE_xt_ivp32_xb_vec*` typedefs in `xt_ivp32.h`. The one that survives in the stripped microcode — `_TIE_xt_ivp32_xb_vec2Nx8U`, the 2N×8-bit-unsigned vector class in the six `cptc_decode_impl<1..6>` instantiations — is a *direct quote* of the type the shipped `xt_ivp32.h` defines. The in-blob type name and the toolchain header agree because both are the same `.tie` `<CTYPE>` compiled to two outputs.
- **Opcode encoding** → the `.tie` `<OPCODEDEF>`/`<FIELDDEF>` become the `libtie-core.so` TIE-XML: opcode = AND of slot-local `fld[hi:lo]==const`, one `OPCODEDEF` per (op, FLIX format) — which is why ~1065 ops expand to ~12642 `OPCODEDEF` rows.
- **FLIX formats** → the `.tie` `<FORMAT>`/`<SLOT>`/`<LENGTH>` become the `libisa-core.so` `Format_*_encode`/`Slot_*`/`length_decoder`: the 14 formats, the self-describing first-nibble length, the 5-slot F3/F11.
- **Builtins** → the `.tie` instruction names become the `IVP_*` C builtins / `ivp_*` Xtensa mnemonics; `xt-clang` (the toolchain's compiler, present at `tools/XtensaTools/bin/xt-clang`) lowers a `_TIE_xt_ivp32_IVP_*(...)` call to the FLIX bundle, and `xt-objdump` reverses it.

### Considerations

The op count a reimplementer trusts comes from whichever projection they query: `nm -D libisa-core.so | grep -c Iclass_IVP_.*_args` returns **1064** (the operand-table count, ≡ 1065 `OPCODEDEF`-named ops in the TIE-XML, the off-by-one being one op without a distinct `_args` table); `xt-objdump` over the carved blobs returns **285** distinct emitted ops; `xt_ivp32.h` enumerates a still-larger set of `IVP_*` macros (it includes pseudo-ops and multi-form aliases). These are three different *counts of three different things* (operand tables vs encodings vs C macros vs emitted), all consistent — the [catalog](ivp-isa-catalog.md) reconciles them. The per-op cycle-accurate behavior (`<SEMANTIC>`/`<CODE>` in the TIE-XML) is what `libcas-core.so` executes; it is present in `libtie-core.so` but encrypted, so a reimplementer gets exact *encoding* from the toolchain but not readable *RTL*.

---

## 4. The Toolchain ↔ Microcode Relationship

### Purpose

Two artifacts on opposite sides of one boundary must not be confused: the **toolchain** (`gpsimd_tools`, in the sibling `neuronx-gpsimd` repo) is the *producer and decoder*; the **microcode** (`libnrtucode_extisa.so`, in this `neuronx-runtime` repo) is the *product*. The runtime ships the product; the toolchain built it and can read it back. This section fixes that relationship so a reader knows why one repo has the blobs and the other has the means to decode them.

### Producer, product, and the fingerprint that links them

The relationship is stamped into the product itself. Every one of the 13 Q7 ELF32 blobs carries a `.comment` of `XtensaTools-14.09 clang version 10.0.1` — the exact `XtensaTools` version sitting in `tools/XtensaTools/bin` next to `xt-objdump`. The blob is the output of *this* toolchain's `xt-clang`; the same toolchain's `xt-objdump` decodes it. The chain:

```text
PRODUCER (sibling neuronx-gpsimd repo, gpsimd_tools toolchain)
  GPSIMD custom-op source  ──xt-clang (XtensaTools-14.09)──►  Q7 microcode .text
        │                         (lowers IVP_* builtins → FLIX bundles per the .tie)
        │   ncore2gp config (core.xparm + libisa-core.so + libtie-core.so)
        ▼
PRODUCT (this neuronx-runtime repo)
  libnrtucode_extisa.so  ── embeds 13 Xtensa ELF32 blobs (.comment = XtensaTools-14.09 clang 10.0.1)
        │                    served to silicon by the 52 nrtucode_* exports (overview.md)
        │
DECODER (back in the sibling repo)
  xt-objdump + ncore2gp config  ──►  decodes the carved blob .text into named ivp_* bundles
                                      (the 285-op catalog, the per-mnemonic encodings)
```

> **NOTE —** the `.comment` fingerprint is the decisive cross-repo link. A reimplementer who finds `XtensaTools-14.09 clang version 10.0.1` in a carved blob and `XtensaTools-14.09` in `tools/XtensaTools` knows the decoder version *matches the producer version* — there is no ISA drift between the toolchain that built the microcode and the toolchain that decodes it. This is why `xt-objdump` decodes the blobs exactly rather than approximately: it is literally the same toolchain release. CONFIDENCE: **HIGH** (the `.comment` is byte-confirmed in every blob per the [q7-blobs](q7-blobs.md) and [identification](xtensa-vision-q7.md) pages; the `XtensaTools-14.09` directory is `ls`-confirmed in the sibling repo this pass).

### Why the runtime repo does not vendor the toolchain

The split is deliberate, not an omission. The toolchain is a 100 MB+ Cadence licensee deliverable (`libtie-core.so` 51 MB + `libcas-core.so` 45 MB + `libisa-core.so` 9.7 MB alone), an `Evaluation`-build (`build.info: Build type: Evaluation`) Tensilica config under a vendor license — it is the *producer-side* artifact, used at custom-op compile time, and has no place in a *runtime* package whose job is to ship and load the already-compiled microcode. The runtime ships only the product (`libnrtucode_extisa.so`) and the host-side provider API that delivers it to silicon. So a reimplementer working purely from this `neuronx-runtime` checkout has the microcode but not the decoder; the decoder is one repository over, in `neuronx-gpsimd`, which is where the GPSIMD custom-op *compiler* (the producer) also lives.

### Considerations

The toolchain↔microcode relationship is the reason this page resolves the [catalog](ivp-isa-catalog.md)'s out-of-tree caveat rather than merely restating it. The catalog correctly noted that its counts cannot be reproduced from the runtime checkout — but they *can* be reproduced from the toolchain checkout, and this page pins exactly where (`neuronx-gpsimd/extracted/nested/gpsimd_tools_tgz/tools/...`) and with what command (§2). A reimplementer who wants to verify any catalog figure clones the sibling repo, sets `XTENSA_SYSTEM`/`XTENSA_CORE`, and runs `xt-objdump` — the figures are externally anchored, but the external anchor is on disk, named, and sized.

---

## Related Components

| Name | Relationship |
|---|---|
| Vision-Q7 Identification | Uses `core.xparm`/`xt_ivp32.h` as Fingerprint 3; this page is where those config files physically live |
| IVP ISA Catalog | Sources every op count and bit-field from the `libisa-core.so`/`libtie-core.so` this page maps; its out-of-tree caveat is resolved here |
| Q7 Microcode Blobs | The product this toolchain built (`.comment = XtensaTools-14.09`) and decodes |
| `gpsimd_tools` (sibling repo) | The toolchain itself — the producer-side GPSIMD custom-op compiler + the `ncore2gp` config + `xt-objdump` |
| `xt-objdump` + `ncore2gp` config | The runnable decode pair; the exact disassemble flow is §2 |

## Cross-References

- [Toolchain Inventory (Tensilica Xtensa Q7, FlexLM)](../../neuronx-gpsimd/wiki/src/topics/toolchain.md) — the **sibling toolchain wiki**: the full `gpsimd_tools` / `XtensaTools-14.09` inventory, FlexLM licensing, and the producer-side view of the artifacts this page maps
- [The IVP Vector ISA Catalog (1065 Ops)](ivp-isa-catalog.md) — the op roster decoded *by* this toolchain; this page resolves that page's "out-of-tree-grounded" caveat by pinning the artifacts in the sibling repo
- [The 13 Q7 Microcode Blobs](q7-blobs.md) — the carved ELF32 images `xt-objdump` consumes; the carve the §2 flow starts from
- [Tensilica Xtensa and Vision-Q7 Identification](xtensa-vision-q7.md) — the three-way ISA proof; `core.xparm` and `xt_ivp32.h` (Fingerprint 3) are the config files this page inventories
- [Overview: the Q7 GPSIMD Engine](overview.md) — where the product (`libnrtucode_extisa.so`) sits in the runtime stack the toolchain feeds
- [back to index](../index.md)
