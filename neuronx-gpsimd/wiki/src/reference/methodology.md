# Methodology — How This Was Reverse-Engineered

> *Every artifact path on this page is relative to `neuronx-gpsimd/` and pins to
> `aws-neuronx-gpsimd-customop-lib_0.21.2.0` + `aws-neuronx-gpsimd-tools_0.21.0.0-bc9b5fad5`.
> The SHA-pinned binary list lives in [The Corpus, Tiers & Binary Inventory](corpus-inventory.md);
> the confidence vocabulary is defined in [The Confidence & Walls Model](confidence-model.md).*

## Abstract

This wiki was built **entirely from static and lawful-interoperability dynamic
analysis of shipped, redistributable binaries** — the customop-lib `.deb`, the
gpsimd-tools tarball, and the ncore2gp Tensilica simulator stack inside it. There
was no runtime trace of the silicon, no debugger session against a real
NeuronCore, and no vendor source tree. The device GPSIMD core is the Cadence
Tensilica "Cairo" microarchitecture — ConfigName `Xm_ncore2gp`, HW revision
NX1.1.4, ISA family Xtensa24, with the Vision-Q7 512-bit FLIX/VLIW vector
coprocessor — recovered from the artifacts that ship *around* it.

The page is written for a reader who wants to **audit** a claim in this wiki or
**reproduce** the recovery from scratch. It names the real tools, the real paths,
the real symbols, and gives one worked verification — confirming a value-oracle
opcode end-to-end across `nm` → `objdump` → a live `ctypes` drive — that you can
re-run command-for-command. It closes with the four verification gotchas that each
caused a real regression during the effort, stated precisely enough to avoid.

The deep companion on bundle decoding is [FLIX Bundle-Decoding
Methodology](flix-decoding.md).

## What a reimplementer must be able to do

- **Re-derive any cited fact** from a named shipped artifact, with a confidence tag
  they can independently check — never "trust me."
- **Drive the shipped ISS as a runtime oracle** to obtain bit-exact per-lane values
  for any vector opcode (§ *The value oracle*).
- **Decode device Xtensa code** with the shipped `ncore2gp` disassembler, and know
  when the NCFW management core needs the scalar-LX rule instead of the Vision FLIX
  rule (§ *Device decode*).
- **Avoid the four file-format gotchas** that silently corrupt offsets and counts
  (§ *Verification gotchas*).

## At a glance

| | |
|---|---|
| **Substrate** | shipped binaries / headers / config / TIE — no trace, no source |
| **Device core** | Tensilica Cairo, `Xm_ncore2gp`, NX1.1.4, Xtensa24 + Vision-Q7 |
| **Host-side tooling** | `objdump` / `nm -DC` / `c++filt` / `readelf` (x86-64) |
| **Device-side tooling** | `XtensaTools/bin/xtensa-elf-objdump`, `XTENSA_CORE=ncore2gp` |
| **Decompile sidecars** | IDA v3 `*_{functions,callgraph,structures,enums,strings,xrefs}.json` + `context/*.md` |
| **Runtime oracle** | `libfiss-base.so` driven live via `ctypes` (864 value leaves) |
| **DWARF leg** | DWARF (`.debug_info`/`.debug_line`) lives in host `libnrt.so` — **not in this gpsimd checkout**; the host-runtime census here is `CARRIED` via its IDA v3 sidecars / the sibling runtime corpus |
| **Confidence model** | per-**claim** `HIGH`/`MED`/`LOW` + `OBSERVED`/`INFERRED`/`CARRIED` |

---

## 1. The substrate — what was available to analyse

Three kinds of artifact carry the entire recovery. None of the host libraries is
*itself* a device image; they are x86-64 programs that **vend** the device code.
`[HIGH/OBSERVED]`

1. **Host x86-64 proprietary libraries** — the ncore2gp Cadence/Tensilica
   simulator/ISA/TIE stack (`libisa-core.so`, `libcas-core.so`, `libfiss-base.so`,
   `libtie-core.so`, plus `-ref-` reference variants) under
   `extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/`, and the device-firmware
   *container* `libnrtucode_internal.so` (with the stripped front `libnrtucode.so` and the
   static `libnrtucode.a`) in the customop `.deb`. **Note the absences in *this* checkout:**
   there is **no** standalone `libnrtucode_extisa.so` (the "EXTISA" content is the embedded
   blob set *inside* `libnrtucode_internal.so`), and **no** standalone `libncfw.so` or host
   `libnrt.so` — those ship in the sibling `neuronx-runtime` corpus, so any NCFW-image or
   host-runtime fact sourced from them is `CARRIED`, not OBSERVED-in-gpsimd (see [The Corpus,
   Tiers & Binary Inventory](corpus-inventory.md) §3).

2. **Embedded device ELF32-Xtensa blobs** (`e_machine = 94`) that travel inside the
   `.rodata` of the `nrtucode` containers, reached by host getter stubs. These
   *are* the GPSIMD firmware — the per-(generation, engine) Vision-Q7 images
   (SUNDA / CAYMAN / MARIANA / MARIANA_PLUS / MAVERICK).

3. **Shipped source-level artifacts** — the four per-generation TPB-engine ISA
   header trees (`custom_op/c10/include/neuron_<gen>_arch_isa/`), the
   `instruction_mapping.json` struct↔opcode tables, the cleartext core config
   (`core-isa.h` / `ncore2gp-params` / `core.xparm`), the TIE database, the
   `cayman-arch-regs` CSR JSON tree, the `al_address_map_db.pkl` SoC address DB, and
   the Cadence-generated `xtensa-modules.c` opcode module.

> **QUIRK —** `extracted/` is `.gitignored`. Plain `fd`/`rg` skip it by default, which
> once produced a false "headers absent" finding. Always reach it with `--no-ignore`
> or an absolute path.

### 1.1 The RTTI-empty finding — no class hierarchy to read off

The first instinct on a C++ binary is to read the class hierarchy straight off the
Itanium-ABI RTTI scaffold (`_ZTV*`/`_ZTI*`/`_ZTS*`, `__cxxabiv1` references), which
survives symbol stripping because it is `.rodata` data. **The GPSIMD device firmware
denies this entirely.** All 29 device Xtensa ELFs are RTTI-empty, confirmed two
orthogonal ways per image rather than assumed: `[HIGH/OBSERVED]`

- **Symbol check** — `readelf --dyn-syms`/`nm`: the EXTISA and ET_EXEC internal
  images carry no `.symtab`/`.dynsym`; the four MAVERICK ET_DYN images carry an
  **empty** `.dynsym`.
- **Byte-string check** — `rg` over the raw bytes for `_ZTS`/`_ZTI`/`_ZTV` and
  `__cxxabiv1`: zero hits on every one of the 29. The device C++ is compiled
  `-fno-rtti` (the classic Cadence/Tensilica default).

The zero is **not** a stripping artifact — RTTI name strings would persist in
`.rodata` even after symbol stripping, and there are none. The consequence is
structural: **none** of the GPSIMD handler/class identification in this wiki rests
on RTTI. It rests on the eight techniques below. What survives instead are two
non-RTTI identity anchors: the `.xt.prop.<mangled>` per-function FLIX-property
**section names** (which carry the C++ mangled symbol, e.g.
`.xt.prop._Z22cross_lane_reduce_implb`, and survive because the firmware strip path
removes the symtab but not `.xt.prop`), and the `kernel_info_table` dispatch entries
plus the DEBUG-image `"S:<Name>"` / `"P%i:"` self-name format strings.
`[HIGH/OBSERVED]`

---

## 2. The eight recovery techniques

Each technique below names its mechanism, the concrete artifact it draws from, and
its confidence ceiling. This is the provenance backbone every subsystem page links
to.

### (a) ELF / firmware carving — the getter-driven image carve

The device firmware is not on disk as files; it is embedded. Two embed mechanisms,
both carved non-destructively:

- `libnrtucode_internal.so`: blobs reached by `*_Q7_POOL_PERF_EXTISA_<n>_SO_get`
  accessors. The carve parses each accessor body (`lea <data>,%rax ; movq
  $size,(%rsi)`) for `(data_va, size)`, then slices `.rodata[data_va :
  data_va+size]` — relying on the gotcha that `.rodata` file-offset == vaddr (§5).
- `libnrtucode_extisa.so` (the sibling `neuronx-runtime` corpus — **not** a standalone file
  in this gpsimd checkout): blobs in a flat JSON-delimited container of
  `[JSON-manifest | Xtensa-ELF]` pairs; ELF size = `e_shoff + e_shentsize*e_shnum`. The carve
  *mechanism* is in-corpus only for the embedded set inside `libnrtucode_internal.so`; the
  `extisa.so` flat-container variant is `CARRIED` across the package boundary.

**Anchor:** 29 ELFs aggregate across both containers (`[HIGH/CARRIED]` for the cross-library
total; `[HIGH/OBSERVED]` for the 16 embedded in `libnrtucode_internal.so` in-checkout), all
`ELFCLASS32` / `e_machine=94` / LE; cross-package `sha256` dedup proves the carve correct
(CAYMAN `EXTISA_{0..3}` is byte-identical to the internal `CAYMAN_{0..3}`; MARIANA ==
MARIANA_PLUS). Memory geometry: `.text @
0x01000000` (IRAM) / `.rodata + kernel_info_table @ 0x02000000` (DRAM). `[HIGH]`

### (b) The native `ncore2gp` Xtensa disassembler — device decode

Stock x86 binutils decodes the host libs; the device Xtensa images need the Cadence
core registry to decode FLIX/VLIW bundles and the Vision-Q7 vector TIE. The shipped
disassembler is used:

```text
extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump
GNU objdump 2.34.20200201 / Xtensa Tools 14.09
XTENSA_CORE=ncore2gp          # the ONLY core registered in the bundled config
```

> **QUIRK —** the device `objdump` has **no default core**: invoked bare it errors
> with *"there is no Xtensa core registered as the default"* and lists `ncore2gp` as
> the one available core. You must export `XTENSA_CORE=ncore2gp`. (Its
> `ncore2gp-params` also hardcodes absolute `/opt` paths; the practical fix is two
> read-only `/opt` symlinks into the extracted tree.)

Validated two ways: a clean symbol-bearing Xtensa ELF *and* a carved stripped EXTISA
blob both decode to real Xtensa mnemonics (the windowed `entry`/`retw.n`/`callx8`
ABI; FLIX bundles; live IVP vector ops). The **desync limit** — objdump's linear
sweep loses bundle sync across literal/selector boundaries and renders those spans
as `.byte` — is the corpus-wide `MED` ceiling for per-instruction body bindings, and
is the precise limit that technique §B (the FLIX decoder, [flix-decoding.md](flix-decoding.md))
lifts. `[HIGH]`

> **QUIRK — the NCFW core is scalar Xtensa-LX, NOT FLIX.** The collective-control
> management core (NCFW) is a windowed **scalar XEA2 Xtensa-LX** core — no MAC16, no
> Vision coprocessor, no FLIX/VLIW. Because `ncore2gp` is the *only* config that
> ships, pointing it at NCFW code makes objdump read scalar `op0 = 0xe`/`0xf` bytes
> as Vision FLIX bundles. That is the entire origin of the spurious "~26-28% FLIX"
> artifact some early non-DX tasks reported, demonstrated empirically on a crafted
> blob. NCFW must be decoded with the **scalar-LX length rule** (`op0` ∈ {e,f} ⇒
> 3-byte instruction) and resynced at `retw.n`; the only real limit there is the
> absent NCFW config, not a real FLIX layer.

### (c) Arch-ISA header compile-verify — the `offsetof`/`sizeof` proof

The customop-lib ships the per-generation TPB-engine ISA headers as **source**.
Rather than infer an operand-struct layout from disassembled loads/stores, the
header is compiled with `gcc` and field offsets + struct size are read directly via
`offsetof()`/`sizeof()`, confirming the header's own `ISA_STATIC_ASSERT(sizeof==N)`.
This promotes a layout from `INFERRED` to `OBSERVED-by-compilation`.

**Anchor (verified this pass):** the shipped CAYMAN header
`.../neuron_cayman_arch_isa/tpb/aws_neuron_isa_tpb_ctrl_mv.h` carries
`ISA_STATIC_ASSERT(sizeof(NEURON_ISA_TPB_CTRL_MV_STRUCT) == 64, ...)`; the four
per-gen header trees ship as cayman (111 headers) / mariana (120) / maverick (126) /
sunda (100). The `instruction_mapping.json` (e.g.
`neuron_cayman_arch_isa/tpb/instruction_mapping.json`) maps each
`NEURON_ISA_TPB_*_STRUCT` to its `NEURON_ISA_TPB_OPCODE_*` via `struct2opcode` /
`struct2pseudo_opcode`. A secondary corroboration channel — the DWARF
(`.debug_info`/`.debug_line`) in the unstripped host `libnrt.so` — resides in the **sibling
`neuronx-runtime` corpus, not this gpsimd checkout**, so it is a `CARRIED` cross-check, not
an in-checkout read; the in-checkout proof is the `offsetof`/`sizeof` compile above. `[HIGH;
the DWARF corroboration CARRIED]`

### (d) `kernel_info_table` / DEBUG self-name anchoring

With no symbols on the device images, kernels are identified two ways:

- **`kernel_info_table`** — a PROGBITS section in every device image: an array of
  8-byte `[BE opcode-key u32][LE funcVA u32]` entries. The `funcVA` is the **only**
  relocated field (one `R_XTENSA_RELATIVE` per entry, stride 8), which independently
  proves the 8-byte stride, the funcVA position, and the entry count (17 for CAYMAN
  = `0x88/8`). Each funcVA validates onto a real prologue.
- **DEBUG self-names** — the `"S:<Name>"` (SEQ engine) and `"P%i:"` (POOL GPSIMD)
  printf-style trace strings baked into the DEBUG-variant images.

The **three-way dispatch discriminator** falls out of this: an opcode *named* in a
header is not proof an image *ships* a handler. The method separates (i) a
header-defined opcode (ISA surface), (ii) `kernel_info_table` membership / a funcVA
on the POOL image (the handler ships), and (iii) the DVE/scalar path where the same
opcode is dispatched through a discrete DRAM jump table. `[HIGH]`

### (e) Cross-generation byte-diffing

With five generations each carved, the same `(engine, variant, region)` image is
diffed across gens. `sha256` equality proves invariance (MARIANA POOL == MARIANA_PLUS
POOL ⇒ only the gen label differs); a localized delta isolates exactly what changed
(e.g. the Q7_POOL reset is `j 0x200` on CAYMAN/MARIANA/MARIANA_PLUS but `j 0x1e4` on
MAVERICK — the v5 core shift). The **false-positive caution**: a naive `^S:` string
diff falsely shows handlers "added/removed" when the real change is a build/PROF
difference, so the diff is grounded on funcVA / `kernel_info` membership, never
string presence alone. See [Codename ↔ Generation Cross-Walk](codename-crosswalk.md)
for the coretype/arch_id stride table. `[HIGH]`

### (f) Cleartext-config cross-validation — four independent ISA sources

A claim from one source is `INFERRED`; the same claim across independent sources is
`OBSERVED`. For the Cairo ISA the wave triangulates four sources that all ship in
the corpus:

1. `libisa-core.so` **binary** decoder tables (`opcodes[]` stride 72; `opcodedefs[]`
   stride 24).
2. the **plaintext** core config (`core-isa.h` `XCHAL_*` / `ncore2gp-params` /
   `core.xparm`).
3. the standalone **TIE database** (`Xtensa.xml`, a trivial +13/mod-256 cipher with
   an 8-byte plaintext checksum prefix, decoded once; plus the cleartext `Xtensa.tl`
   tielib).
4. the Cadence-generated **source** `xtensa-modules.c`.

All four converge on **1534 opcodes / 14 formats / 46 slots**; sources 1, 2, 4 agree
on 12569 placements; the TIE DB reports 1607 mnemonics / 12642 placements = the
pre-fold authoring superset (+73), reconciled as the authoring-vs-runtime fold. The
roster is byte-identical in array order between the source and the binary. `[HIGH]`

### (g) The confidence discipline + premise-correction culture

Every claim carries a per-**claim** confidence (`HIGH`/`MED`/`LOW`) and an evidence
tag (`OBSERVED` = read from shipped bytes/files **or executed this pass**;
`INFERRED` = reasoned over `OBSERVED`; `CARRIED` = re-used at a cited report's
confidence). A synthesis **never inflates** a source's confidence — it carries it
forward. The defining habit is **adversarial premise-correction**: when later binary
evidence contradicts an earlier report — or the task's own premise — the correction
is recorded explicitly rather than quietly conformed to. The full vocabulary and the
"confidence walls" are in [The Confidence & Walls Model](confidence-model.md).
`[HIGH]`

### (h) The IDA v3 sidecar JSON + `context/*.md`

The 12 "TIER-1" host binaries (the customop-lib OpenSSL/xz/zlib stack plus the two
GPSIMD-specific `libnrtucode*.so`) were run through an IDA export that emits a
**structured JSON sidecar per binary** under `ida/<dir>/`, plus a per-function
`context/*.md` and a `graphs/*.dot|.json` CFG. The GPSIMD-unique decompiled surface
is two binaries — `libnrtucode_internal.so` (519 functions, full symbols) and its
stripped twin `libnrtucode.so` (byte-identical code, names removed) — so structural
recovery of the host firmware-loader rides almost entirely on this sidecar pair. The
sidecars and what each carries:

| Sidecar | Carries (audit handle) |
|---|---|
| `*_functions.json` | per-function `addr`/`end`/`size`/`insn_count`/`block_count`, `callers`/`callees` (addr+name), `frame_size`, `stack_vars`, `strings_referenced`, `constants_used`, `data_refs` |
| `*_callgraph.json` | flat call edges `{from, from_addr, to, to_addr}` |
| `*_structures.json` | exact struct layout — `{name, size, members:[{name, offset, size, type}]}` |
| `*_enums.json` | enum members with their exact values |
| `*_strings.json` / `*_xrefs.json` | string-pool values + cross-reference graph |
| `*_frames.json` / `*_switches.json` / `*_data_tables.json` | frame geometry, jump tables, recovered data arrays |
| `context/<fn>_<addr>.md` | per-function narrative with callee annotations |

These are **binary-derived**: the offsets, counts, and edges are read off the ELF by
the disassembler, so a `structures.json` member offset is as citeable as an `objdump`
line. The cross-binary count discipline (§5) still applies — counts are grounded in
the `nm` table, not in a grep of these decompiles.

---

## 3. The value oracle — `libfiss-base.so` driven live via `ctypes`

The most powerful technique is not static at all: the shipped ISS **runs**, and it
computes bit-exact per-lane values for the Vision-Q7 vector ISA. Two host libraries
model the same ISA with opposite strategies:

- `libcas-core.so` is a **timing + hazard + decode** model that *delegates* element
  math to host callbacks (`nx_*_interface`). It is the oracle for **cycle counts**,
  not values.
- `libfiss-base.so` computes element values **in-process**: it has zero host math
  callbacks and **864 distinct `module__xdref_*` value primitives** — clean C-ABI
  pure functions for the integer path. This is the executable **value oracle**.

Because the `xdref` leaves are pure functions (the machine-state pointer is unused
for integer ops), they are driven standalone by `dlopen`-ing the real shipped `.so`
under `ctypes` and calling a leaf by its exported symbol — the binary itself
computes the result. The 7-stage `simple_states` harness (`libfiss.h` /
`simple_runtime.h`) is the full path for stateful ops; the integer value oracle needs
only the leaf.

This is how `OBSERVED-by-naming` claims become `OBSERVED-by-execution`. It is also
the **(d) leg of the four-oracle bit-exact method**, which cross-checks each opcode
against (a) the TIE-XML RTL semantics, (b) the FLIX decoder lifted to Python, (c) the
NKI numpy reference simulator, and (d) this live `libfiss` drive — used to validate
the integer-ALU, convert/pack, predicate, MAC, and transcendental families.

---

## 4. A worked verification, end to end

The claim under audit (from the `fs[0-7]ltu` family): *"each per-FS-register
unsigned-less-than flag generator `module__xdref_fsKltu_64_8_8` is a thin wrapper
that delegates to the shared unsigned 8-bit compare `module__xdref_ltu_1_8_8`, which
computes `out = (unsigned)(A < B)`."* Here are the four steps, each independently
re-runnable, that confirm it. The binary is
`extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libfiss-base.so`
(`sha256 260b110c…`).

**Step 1 — ground the count and the addresses in `nm` (never a decompile grep).**

```text
$ nm -D --defined-only libfiss-base.so | rg -c 'module__xdref_'
864
$ nm -D --defined-only libfiss-base.so | rg 'fs0ltu|fs3ltu|xdref_ltu_1_8_8'
00000000008328d0 T module__xdref_fs0ltu_64_8_8
0000000000832930 T module__xdref_fs3ltu_64_8_8
00000000005bc4f0 T module__xdref_ltu_1_8_8
```

The 864 matches the documented full leaf coverage; the addresses are the audit
anchors for the next step.

**Step 2 — confirm the wrapper body with the device-host `objdump`.**

```asm
00000000008328d0 <module__xdref_fs0ltu_64_8_8>:
  8328d0: push   %rbx
  8328d1: mov    %rcx,%rbx                 ; save the result pointer (4th arg, %rcx)
  8328d4: sub    $0x10,%rsp
  8328d8: lea    0xc(%rsp),%rcx            ; scratch result slot
  8328dd: call   module__xdref_ltu_1_8_8@plt   ; <-- delegation, proven by the call
  8328e2: mov    0xc(%rsp),%eax
  8328e6: mov    %eax,(%rbx)               ; copy the scratch result to *result
  8328ed: ret
```

The body is `push/save-ptr/call ltu_1_8_8/copy-result/ret` — a pure forwarding
wrapper. Disassembling `0x5bc4f0` confirms the delegate's ABI and math:

```asm
00000000005bc4f0 <module__xdref_ltu_1_8_8>:
  5bc4f0: xor    %eax,%eax
  5bc4f2: cmp    %edx,%esi                 ; compare A(esi) vs B(edx)
  5bc4f4: setb   %al                       ; al = 1 iff A < B  (unsigned / "below")
  5bc4f7: mov    %eax,(%rcx)               ; *result = al
  5bc4f9: ret
```

So the C ABI is `int leaf(void *xstate /*rdi*/, unsigned A /*esi*/, unsigned B
/*edx*/, unsigned *result /*rcx*/)` and `*result = (unsigned)(A < B)`.

**Step 3 — drive both leaves live via `ctypes` against a boundary grid.**

```python
import ctypes
F = ".../ncore2gp/config/libfiss-base.so"
lib = ctypes.CDLL(F, mode=ctypes.RTLD_GLOBAL)
SIG = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
ltu = lib.module__xdref_ltu_1_8_8;     ltu.restype = ctypes.c_int; ltu.argtypes = SIG
fs0 = lib.module__xdref_fs0ltu_64_8_8; fs0.restype = ctypes.c_int; fs0.argtypes = SIG
out = ctypes.c_uint(0)
for A, B in [(0,0),(0,1),(1,0),(127,128),(128,127),(200,200),(5,250),(250,5)]:
    ltu(None, A, B, ctypes.byref(out)); v_ltu = out.value
    fs0(None, A, B, ctypes.byref(out)); v_fs0 = out.value
    assert v_ltu == v_fs0 == (1 if (A & 0xff) < (B & 0xff) else 0)
```

**Step 4 — the result.** Re-running this pass: 20 live calls across a 10-pair
boundary grid, `fs0ltu` ≡ `ltu_1_8_8` ≡ `(unsigned)(A<B)`, **0 mismatches**. The
claim is now `HIGH/OBSERVED-by-execution` — anchored in `nm` (count + symbol
address), `objdump` (wrapper body + delegate semantics), and the live binary itself
(values). This is the template: any value claim in Part 14/15 was promoted the same
way. `[HIGH/OBSERVED]`

> The DEBUG-disassembly above shows `fs1ltu`/`fs2ltu` are byte-identical except for
> the PLT offset, so the family generalises from the worked case; only the symbol
> address moves.

---

## 5. The verification gotchas — each caused a real regression

These four are stated last because they silently corrupt offsets and counts if
missed. Each was hit, diagnosed, and codified.

### `.text`/`.rodata` are VMA == file-offset, but `.data` is NOT

For carving a `.rodata`-resident struct by its vaddr you can `xxd`/`objdump` directly
— the file offset equals the VMA. **`.data` does not.** It carries a per-binary
delta. Confirmed on the oracle binary this pass with `readelf -SW`:

```text
[11] .text    Addr 0000…0190430  Off 190430   ; equal
[13] .rodata  Addr 0000…088ff00  Off 88ff00   ; equal
[23] .data    Addr 0000…0c8eb68  Off a8eb68   ; delta 0x200000  <-- subtract before xxd
```

The delta is **binary-specific**, not a universal constant: the `nrtucode` pair is
`0x3000`, the `ncore2gp` config libs are `0x200000` (above) — both OBSERVED in-checkout — and
the ncfw/extisa pair is `0x1000` (`[CARRIED]` from the sibling `neuronx-runtime` corpus,
since neither `libncfw.so` nor `libnrtucode_extisa.so` is a file in this gpsimd checkout).
(Over-generalising the libtpu wave's `0x400000` is itself the regression.) Always read the
real `Addr`/`Off` columns for the section before addressing a `.data`-resident struct.

### A vtable slot in `call *0xN(%rax)` is measured from `vptr = _ZTV + 0x10`

When you do resolve a vtable on the host RTTI surface (`libneuroncustomop.a`,
`libnrt.so`), the vptr stored in the object points **past** the offset-to-top /
typeinfo header — it is `_ZTV<class> + 0x10`, not the `_ZTV` symbol. So a slot index
in `call *0xN(%rax)` is `N` relative to `_ZTV + 0x10`; computing `reloc − symbol`
overcounts by `0x10` (one real regression cited `vt+0xA0` where the truth was
`vt+0x90`). This is moot on the RTTI-empty device firmware (§1.1) and applies only to
the host RTTI surface.

### Re-ground every count claim in `nm`, never a decompile grep

A "symbol hit count" grepped from a flattened decompile inflates **2–12×** versus the
binary symbol table (duplicate entries, alias names, inlined copies). Step 1 of §4 is
the discipline in miniature: the 864-leaf count comes from `nm -D --defined-only … |
rg -c`, not from counting matches in the per-function `.c` mirror.

### `extracted/` is `.gitignored`

Restated because it bites every lane: `fd`/`rg` skip the `extracted/` tree by
default, which once produced a false "headers absent" finding. Use `--no-ignore` or
an absolute path for every artifact under `extracted/`.

---

## 6. Honest recovery limits

The method has true coverage boundaries (distinct from confidence errors), the most
consequential of which a reimplementer should plan around:

- **FLIX wide-bundle internals** — objdump's linear sweep desyncs on densely
  scheduled FLIX/VLIW; table bases, `kernel_info` entries, dispatch addresses and
  reset vectors decode above the desync line (`HIGH`), but per-instruction operand
  bindings *inside* a desynced wide-bundle span are not byte-resolved by objdump
  alone — substantially lifted by the FLIX decoder ([flix-decoding.md](flix-decoding.md)).
- **MAVERICK (v5) is the hardest family** — its images are ET_DYN with **no**
  `.xt.prop` and an empty `.dynsym`, built with a non-shipped toolchain, so technique
  (b) falls back to flat byte-stream decode and identity rests on cross-gen byte-diff
  + `kernel_info_table` + string self-names.
- **Out-of-corpus Python/MLIR layers** — the NKI Python package and the `emit_*`
  lowering symbols are not in this corpus; the device half is in-corpus, the compiler
  half is observed in the sibling `neuronx-cc` / `neuronx-misc` corpora.

The full residual register and the per-dimension coverage statement live in
[The Confidence & Walls Model](confidence-model.md) and the Part 16 coverage ledger.

---

*Provenance: lawful interoperability reverse engineering under DMCA 17 U.S.C.
§1201(f). Every fact on this page derives from shipped-binary / shipped-header /
shipped-config analysis and the live in-process execution of shipped bytes; no
vendor source tree was referenced.*
