# The TIE Database & Four Independent ISA Sources

Every other ISA page in this guide cites *one* artifact as its witness — the
[libisa decode model](libisa-decode-model.md) reads `libisa-core.so`, the
[register-files](register-files.md) page reads its `regfiles[]` table, the
[FLIX encoding](flix-encoding.md) page reads the format tree. This page is where those
single witnesses are tied together. The Cairo (`ncore2gp`) Vision-Q7 ISA is recoverable from
**four independent shipped sources** that were generated from one common origin and therefore
must — and do — agree. This page is two things at once: the authoritative reference for the
**TIE database** (the Tensilica Instruction Extension model the whole config is generated from),
and the **four-source cross-validation model** — the multi-source confidence backbone every
per-opcode claim downstream rests on. When a later page tags a mnemonic `[HIGH/OBSERVED]`, the
strength of that tag is in large part the *agreement count* established here: the same opcode
read out of four files that were produced by four different tools.

The headline number a reimplementer must internalize: the ISA has **two roster sizes**, and they
are reconciled, not contradictory. The TIE database — the authoring source — holds **1607**
distinct mnemonics across **12642** opcode-placements; the runtime decode tables in
`libisa-core.so` hold **1534** mnemonics across **12569** placements. The runtime set is a *clean
subset* of the authoring set; the `+73` delta is fully characterized (assembler-macro and
pseudo-op forms the runtime table folds away). The correct pairs are **1607 ↔ 12642** (pre-fold
TIE DB) and **1534 ↔ 12569** (post-fold runtime). The whole
[1534 / 1607 / 12642 tally](coverage-tally.md) is a dedicated page; here it is *derived* from the
four sources.

> **GOTCHA — counts come from `nm`/byte-parse, never a decompile grep.** Every count on this page
> reproduces with `nm libisa-core.so | rg -c …` or a byte-exact parse of the decoded TIE-XML. A
> grep over a function decompile inflates symbol-hit counts 2–12×; that path is never used. The
> cipher-decoded `OPCODEDEF` count was re-derived from the binary in-checkout (`12642`,
> [§3.4](#34-self-verification--the-counts-from-the-binary)).

---

## 1. Key facts

| Fact | Value | Source |
|---|---|---|
| TIE-DB container | `libtie-core.so`, **51,098,208 B** (≈51 MB) | `stat`; SHA-256 `06fc43eaf3622ae1…` |
| Container `.text` | **0x48 = 72 B** (5 stub thunks only) | `readelf -SW` |
| Container `.data` | **0x30b98c8 ≈ 51.0 MB** (the four blobs) | `readelf -SW` |
| Exported symbols | exactly **5** getters | `nm -D --defined-only` |
| Standalone DB (XML) | `Xtensa.xml`, **45,533,206 B** (ciphered) | `control/TIE/` |
| Standalone DB (tielib) | `Xtensa.tl`, **112,537,066 B** (cleartext binary) | `control/TIE/` |
| Cipher | ASCII checksum + `'|'` + body where `byte = plaintext + 13 (mod 256)` | decoded in-checkout |
| TIE-DB roster (pre-fold) | **1607** mnemonics / **12642** `OPCODEDEF` | decoded `post_rewrite` blob |
| Runtime roster (post-fold) | **1534** opcodes / **12569** placements | `libisa-core.so` `nm` |
| Structural totals | **14** FORMAT, **46** SLOTDEF, **8** REGFILE | decoded XML == `libisa` tables |
| msem overlay | `libtie-Xtensa-msem.so`, **258,120 B** | same 5-getter ABI |
| TIE-XML parser | `libtie.so`, **657** `xtie_*` accessors | `nm -D` |

All of these are read from the shipped binaries in this checkout (under the gitignored
`extracted/nested/gpsimd_tools_tgz/tools/` tree — reached with absolute paths / `--no-ignore`).
`[HIGH/OBSERVED]` throughout except where flagged. The TIE-XML and its symbols **are**
binary-derived and directly citeable — they are bytes in a shipped `.so`, decoded by a cipher
recovered from those same bytes.

---

## 2. What the TIE database is

**TIE** (Tensilica Instruction Extension) is the language Cadence's configurable-core flow uses
to *define* an ISA extension: register files, C types, operations, encodings, issue
slots/formats, pipeline schedules, and bit-precise reference semantics. The TIE compiler reads
the `.tie` sources for a config and emits an internal **database** — a single elaborated
construct tree — that every downstream tool (assembler, disassembler, ISS, the LLVM config-gen)
consumes. For the Cairo `ncore2gp` core, that database is what `libtie-core.so` carries, and what
the standalone `Xtensa.xml` / `Xtensa.tl` pair carries.

The config itself names exactly **two** TIE modules that built it (from `config.cf`'s
`TIEInternalModules`): `mul32.tie` (the MUL32 multiply option) and `vision.tie` (the Cadence
"XDG" Vision-Q7 coprocessor, package `xt_ivp32`). Everything else in the roster is base Xtensa24
+ the standard options. So "the TIE DB" for this core = base Xtensa + {MUL32 option} + {Vision
coprocessor} — and that Vision coprocessor is the GPSIMD vector compute. The
[core-identity page](identity-config.md) carries the full option vector; this page is about the
*database* those options elaborate into.

### 2.1 Where it lives — `libtie-core.so` (the shipped container)

`libtie-core.so` is a 51 MB host-`x86-64` ELF whose **entire substance is `.data`**. Its `.text`
is `0x48` (72) bytes — five two-instruction stubs and nothing else. It is **not** a parser,
**not** a simulator: it is a *data container*, the on-disk shared-object form of the TIE
compiler's XML export. Its five — and only five — dynamic exports are:

```
$ nm -D --defined-only libtie-core.so
0000000000000340 T interface_version       # movsd 0x58(%rip),%xmm0 ; ret  — provider ABI version (a double)
0000000000000350 T get_xml_post_parse       # lea xml_data_post_parse(%rip),%rax   ; ret
0000000000000360 T get_xml_post_rewrite     # lea xml_data_post_rewrite(%rip),%rax ; ret
0000000000000370 T get_xml_compiler         # lea xml_data_compiler(%rip),%rax     ; ret
0000000000000380 T get_xml_xinfo            # lea xml_data_xinfo(%rip),%rax         ; ret
```

Each getter is literally `lea blob(%rip),%rax ; ret` — it hands back a pointer to one of four
ciphered XML byte-strings embedded in `.data`. The consumer (the TIE runtime / ISS) `dlopen`s the
DLL and `dlsym`s the getter it wants. The container has zero imports and an empty class hierarchy
(RTTI-empty: `0` `_ZTI`/`_ZTV`/`_ZTS`). `libtie-Xtensa-msem.so` (258 KB) is a smaller instance of
the *identical* 5-getter container schema (§6).

> **NOTE — `libtie-core` is a shipped config DLL, present here.** Unlike host-only runtime
> libraries (`libnrt` etc.) that are absent from the gpsimd checkout, the TIE-DB container, the
> standalone `Xtensa.xml`/`.tl`, the `libtie.so` parser, and `libisa-core.so` all ship in
> `gpsimd_tools_tgz/tools/`. Every fact on this page is therefore re-OBSERVable in-checkout, not
> carried across a corpus boundary.

### 2.2 The four blobs — TIE-compiler phase serializations

`libtie-core.so`'s `.data` carries four XML blobs, one per compiler **phase** of the *same one*
Cairo database (verified symbol offsets + inter-symbol spans; the `.data` VMA→file-offset delta is
`0x200000` for this binary):

| blob (`.data` symbol) | getter | size | decoded `<xdoc>` header |
|---|---|---:|---|
| `xml_data_post_parse` | `get_xml_post_parse` | **50 B** | XML decl only — an emptied placeholder stub |
| `xml_data_post_rewrite` | `get_xml_post_rewrite` | **≈49.0 MB** | `type="tie" name="core"` — **THE FULL DB** |
| `xml_data_compiler` | `get_xml_compiler` | **≈2.06 MB** | `type="minimal-tie" name="compiler_xml"` — directive side-table |
| `xml_data_xinfo` | `get_xml_xinfo` | **11,563 B** | `type="minimal-tie" name="xinfo_xml"` — regfile-geometry quick-ref |

The `post_rewrite` blob is the one that matters: it is the **post-rewrite / post-`gen`** database —
the fully-elaborated, folded operation/encoding/semantics tree the tools actually consume. The
other three are intermediate-representation phases: `post_parse` (the just-after-parse snapshot,
here a 50-byte stub), `compiler_xml` (a `PROPERTY` list of compiler directives like
`specialized_op_BEQZ_BEQZ_W15`), and `xinfo_xml` (a `REGFILE` geometry table).

> **CORRECTION — phase/format versions are *not* silicon generations.** The blob phases
> (`post_parse`/`post_rewrite`/`compiler`/`xinfo`) and the `xdocversion` attributes (`202000` on
> the base DB, `201000` on the minimal-tie side-tables) and the pipeline stages
> (`rstage`/`estage`/`mstage`/`wstage` = `0/3/4/6`) are all axes of the **one** Cairo config. They
> are **not** the five GPSIMD silicon generations (Sunda/Cayman/Mariana/Mariana+/Maverick — a
> *firmware-image* axis, invisible in any TIE descriptor). Do not read a "version" token out of
> the TIE-XML as a generation. `[HIGH/OBSERVED]`

### 2.3 The standalone pair — `Xtensa.xml` and `Xtensa.tl`

The same database also ships as two standalone files under `ncore2gp/control/TIE/`:

- **`Xtensa.xml`** (45,533,206 B) — the **ciphered, human-readable** `<xdoc type="tie">` XML dump:
  the complete construct tree (operations, encodings, semantics `MODULE`s, regfiles, ctypes,
  schedules). Same cipher as the container blobs ([§3](#3-the-cipher-and-the-tie-xml-schema)). Best
  source for human RE of per-op semantics.
- **`Xtensa.tl`** (112,537,066 B) — the **cleartext** compiled "tielib" the tools/ISS load
  directly: a record/index section (`0..≈45 MB`) followed by a string/symbol pool (from offset
  `≈45.14 MB`). It is **not** ciphered, so a direct `grep` over it finds names/strings —
  `1596/1607` XML mnemonics appear verbatim as `.tl` strings (the 11 "missing" are `≤2`-char names
  like `B`/`OR`/`J` excluded by the matcher), proving the two files are the same DB.

The `libtie-core.so` container's `post_rewrite` blob, the standalone `Xtensa.xml`, and the
`Xtensa.tl` tielib are three serializations of one database. The container blob and the standalone
XML differ **only** in their checksum prefix; the ciphered body is byte-identical (verified: both
start `… | 49 4c 05 7a 79 2d 03 72 7f ff 76 7c 7b 4a …` after the `'|'`).

---

## 3. The cipher and the TIE-XML schema

### 3.1 The recovered transform (the post-rewrite TIE-XML cipher)

`Xtensa.xml` and the container blobs are obfuscated, not encrypted. The transform recovered from
the bytes (the formal-semantics decode lane, "GX-SEM") is trivial: each blob is an **ASCII-hex
checksum prefix** terminated by `'|'`, followed by a body where every byte is
`plaintext + 13 (mod 256)`. Decoding is the inverse:

```c
// Recovered TIE-XML deobfuscation. Input: one whole getter blob (or Xtensa.xml).
// The transform was recovered from known plaintext "<?xml version" — see §3.4.
char *tie_xml_decode(const uint8_t *blob, size_t n, size_t *out_len) {
    const uint8_t *bar = memchr(blob, '|', n);   // skip the "2ec0cd3|" ASCII checksum prefix
    const uint8_t *body = bar + 1;
    size_t body_len = n - (body - blob);
    char *out = malloc(body_len + 1);
    for (size_t i = 0; i < body_len; i++) {
        unsigned p = (unsigned)(body[i] - 13u) & 0xFF;   // the +13/mod-256 inverse
        // QUIRK: plaintext letters s..z (0x73..0x7A) overflowed 0x7F on +13 and land in
        // 0xF2..0xFA on the naive inverse; remap them back to ASCII s..z.
        switch (p) {
            case 0xF2: p = 's'; break;  case 0xF6: p = 'v'; break;
            case 0xF4: p = 't'; break;  case 0xF7: p = 'w'; break;
            case 0xF5: p = 'u'; break;  case 0xF8: p = 'x'; break;
            case 0xF9: p = 'y'; break;  case 0xFA: p = 'z'; break;
            default: break;
        }
        out[i] = (char)p;
    }
    out[body_len] = '\0';
    *out_len = body_len;
    return out;   // a well-formed <?xml …?><xdoc type="tie" …> document
}
```

The decoded first line is `<?xml version="1.0" encoding="ISO-8859-1" ?>`, and the root opens
`<xdoc type="compound" name="core">` wrapping `<xdoc type="tie" name="post_gen"
xdocversion="202000" endian="little" rstage="0" estage="3" mstage="4" wstage="6"
instbuf_width="256">`. The pipeline stages `0/3/4/6` and the 256-bit instruction-fetch buffer
match the [config](identity-config.md) exactly. The post-rewrite container blob's root is
`name="core"` rather than `name="post_gen"`, but the construct tree below is the same.

> **QUIRK — the cipher self-documents its origin.** Because `'<'`/`'?'`/`'x'` decode cleanly but
> `'s'..'z'` need the overflow remap, an undecoded blob shows a recognizable signature: ASCII
> structure punctuation intact, alphabetic high-letters showing as `0xF2..0xFA`. That signature is
> how the transform was recovered without any key — the cipher is a single additive constant with
> a wrap quirk, not a keyed stream. The `+13` constant is the same across all four blobs and both
> container DLLs.

### 3.2 The TIE-XML construct schema

The decoded XML is a tree of `<UPPERCASE>` construct elements, every one carrying
`package=`/`line=`/`file=` attributes. (The TIE compiler merges all sources into one synthetic
`file=` value, `coretie2_internalmodules.tie`; the real module boundary is the **package**, not
the file.) The structural construct types — the ISA objects a reimplementer cares about — census
as follows over the decoded base DB:

| construct | count | what it is |
|---|---:|---|
| `OPCODEDEF` | **12642** | per-`(op,slot)` opcode-with-encoding definition — the placement matrix |
| `ICLASS` | **1458** | instruction class: operand list + signal args |
| `MODULE` | **1045** | datapath reference-implementation block (the bit-precise semantics) |
| `INSTR_SCHEDULE` | 1564 | per-op USE/DEF pipeline-stage latencies |
| `PROTO` | 3595 | C-intrinsic prototypes (the `IVP_*` signatures) |
| `STATE` | **81** | architectural state registers |
| `CTYPE` | **64** | C types bound to regfiles |
| `EXCEPTION` | 61 | exception definitions |
| `SLOTDEF` | **46** | issue slots |
| `SREG` / `UREG` | 31 / 3 | special / user registers (RSR/WSR, RUR/WUR) |
| `FORMAT` | **14** | instruction formats / bundles |
| `REGFILE` | **8** | register files (`+` 8 `REGFILE_VIEWS`) |

Below the structural objects, the per-op *semantics* are an expression-node AST
(`ASSIGNMENT`/`WIRE`/`CONCATENATION`/`BITWISE_*`/`CONDITIONAL`/`ADD`/…) — the entire op behavior
is wire/assign logic. A representative read: the `IVP_MULNX16` widening multiply is the `PROTO`
`IVP_MULNX16(out xb_vecNx48, in xb_vecNx16, in xb_vecNx16)` over the `MODULE`
`ivp_mult_add_16_16_48`, whose body is a 4× partial-product generator feeding a 4:2 compressor
array leaving the result in carry-save redundant form (the `wvec` `Nx48` accumulator). The
**formal ISA semantics** chapter (Part 3) is *decoded from these `MODULE`/`SEMANTIC`/`REFERENCE`
blocks* — see [§7](#7-relationship-to-the-formal-semantics-part-3).

### 3.3 How to extract it

A reimplementer reproduces the database in three steps:

1. **Get the blob.** `dlopen("libtie-core.so")`, `dlsym("get_xml_post_rewrite")`, call it — or,
   statically, `nm libtie-core.so | rg xml_data_post_rewrite` for the symbol, subtract the
   `0x200000` `.data` delta for the file offset, and read to the next blob symbol
   (`xml_data_compiler`) or the `_xti_tie_base` end marker.
2. **Decode** with the [§3.1](#31-the-recovered-transform-the-post-rewrite-tie-xml-cipher) transform.
3. **Parse** the resulting `<?xml…?>` document with any XML reader, or — for the in-tree path — the
   shipped `libtie.so` runtime, whose `xtie_get_post_rewrite_phase()` consumes the getter and its
   `xtie_xml_item_*` DOM walker + typed `xtie_<construct>` accessors query every construct family
   ([§6](#6-the-msem-overlay-and-the-tie-runtime)).

Alternatively, grep the **cleartext** `Xtensa.tl` directly for symbol/string presence (no decode
needed) and use the XML for structure + semantics.

### 3.4 Self-verification — the counts from the binary

The four-source claim is only as strong as its byte-grounding, so the central counts were
re-derived in-checkout this pass. Decoding `libtie-core.so`'s `post_rewrite` blob with the
[§3.1](#31-the-recovered-transform-the-post-rewrite-tie-xml-cipher) transform and counting tags
yields, with zero adjustment:

```
OPCODEDEF placements : 12642     distinct mnemonics : 1607
FORMAT : 14   SLOTDEF : 46   REGFILE : 8
ICLASS : 1458   MODULE : 1045   STATE : 81   CTYPE : 64
BEQZ.W18 present : True          xdocversion 202000 : True
```

These match the [§4](#4-the-four-independent-isa-sources) cross-source numbers exactly, and the
`FORMAT 14 / SLOTDEF 46 / REGFILE 8` triple is **byte-identical** to the `libisa-core.so` runtime
tables ([§5](#5-the-synthesis--the-certified-isa-model)). `[HIGH/OBSERVED]`

---

## 4. The four independent ISA sources

The ISA is recoverable from four artifacts produced by four different tools from the one TIE
origin. They are *independent* in that an error in any one would not propagate to the others — a
transcription slip in the decode tables, a desync in a disassembly, a stale header — shows up as a
disagreement, which is exactly what makes their **agreement** a confidence signal. (The honest
dependency: all four ultimately trace to the same Cadence TIE compile, so they share *design*
provenance; what is independent is the *serialization and the tool that reads it*.)

| # | Source | Artifact | What it provides | Coverage | Confidence character |
|---|---|---|---|---|---|
| 1 | **libisa decode tables** | `libisa-core.so` | the runtime opcode/encoding tables + encode/decode thunks | **1534** opcodes / **12569** placements (post-fold runtime) | `[HIGH/OBSERVED]` — direct `nm`/byte reads; the authoritative *runtime* roster |
| 2 | **The TIE database** | `libtie-core.so` / `Xtensa.xml` / `Xtensa.tl` | the full authoring DB: 1607 ops, encodings, **and the bit-precise semantics** | **1607** / **12642** (pre-fold authoring) + 1045 `MODULE`s | `[HIGH/OBSERVED]` — decoded XML; the *superset* + the only source carrying semantics |
| 3 | **Native disassembler** | `xtensa-elf-objdump` (`--xtensa-core=ncore2gp`) | decode of *real* instruction streams → mnemonics + operands | what it resolves; FLIX-desync MED ceiling on 512-bit bundles | `[HIGH/OBSERVED]` above the desync line; `[MED]` inside it |
| 4 | **customop arch-isa headers** | `custom_op/c10/include/*` + `instruction_mapping.json` | the host-side **TPB tensor-engine** opcode/operand layer + the v1–v5 codename map | the engine-routing axis — **not** the Q7 Vision opcodes | `[HIGH/OBSERVED]` for what it *is*; the weakest leg for the *core* ISA |

### 4.1 Source 1 — the libisa decode tables (the runtime authority)

`libisa-core.so` is the config's runtime ISA library: a not-stripped 9,690,712-byte host ELF (SHA
`8fe68bf462ce76ee…`) whose tables drive the assembler/disassembler/ISS at run time. It is the
authoritative source for the **runtime** roster — `1534` `opcodes[]` entries (469 scalar + 1065
`ivp_*`-**name-prefix** vector; the `xt_ivp32` *package* count is **1072**, the 7 extra being the
scalar-FP-control ops `rur/wur.fcr/.fsr` + `recipqli.s`/`mulsone.s`/`mulsone.h`, which are in the
package but not the `ivp_` prefix — package ≠ prefix, see
[the coverage tally §4.1](coverage-tally.md#41-the-28-package-census-the-binary-partition-sums-to-1534))
and `12569` `(opcode × slot)` placements, the latter confirmed three ways: the
`num_encode_fns()` accessor returns `0x3119 = 12569`, `nm` shows exactly `12569`
`Opcode_*_Slot_*_encode` symbols, and the per-slot placement re-count sums to `12569`. Each
placement is a tiny `movl $imm,(%rdi); ret` encode thunk whose immediate is the opcode-selector
template for that `(mnemonic, slot)`. This source owns the **raw table layout and codec ABI** —
documented in depth on the sibling [libisa table-schema page](libisa-table-schema.md); this page
uses it only as the *runtime* leg of the cross-validation.

`libisa-core.so` is what the [canonical decode model](libisa-decode-model.md) page reads end to
end. It is **independent** of source 2 in serialization: it carries the *folded* tables a tool
indexes at run time, not the authoring XML. Its agreement with the TIE DB (same FORMAT/SLOTDEF/
REGFILE counts, same per-slot encoding constants — e.g. `IVP_ADDNX16`'s `F0_S3_ALU` template
`0x1016a` appears identically in both) is the primary 1↔2 cross-check.

### 4.2 Source 2 — the TIE database (the superset + the semantics)

The TIE DB ([§2](#2-what-the-tie-database-is)–[§3](#3-the-cipher-and-the-tie-xml-schema)) is the
**authoring** source: `1607` mnemonics / `12642` placements, a *superset* of the runtime roster.
Crucially, it is the **only** source that carries the bit-precise reference semantics — the `1045`
`MODULE` blocks (the 16×16→48 Wallace-tree MAC, the multi-precision reduce fold, the sign/zero
converts, the IEEE-754 round-to-nearest-even / toward-zero / ±inf / away FP rounding with
overflow/underflow/inexact flags) that source 1's decode tables only *index* and could only
*infer* the layout of. Where the libisa tables say "this slot encodes `IVP_OEQN_2XF32`", the TIE
DB says "and here is exactly what it computes." This is why the TIE DB is the backbone: it is both
the widest roster and the only semantic ground truth.

### 4.3 Source 3 — the native disassembler

The shipped `xtensa-elf-objdump` (core `ncore2gp`; banner *"GNU objdump (GNU Binutils)
2.34.20200201, Xtensa Tools 14.09"*) is a *fourth, executable* witness: pointed at a real
`.o`/`.a`/firmware image it decodes the actual instruction stream to mnemonics + operands. Unlike
sources 1–2 (which *describe* the ISA), source 3 *exercises* it on real bytes, so it catches any
divergence between the table model and what the toolchain actually emits — the same mnemonics must
come out. It is the only Xtensa core registered in the toolchain (`objdump -i`), and it is the
device-side tool of record (host `x86` objects use plain `objdump`).

**Coverage — what it CAN decode `[HIGH]`.** Real FLIX/VLIW bundles + scalar runs to correct
mnemonics, including the live GPSIMD vector ISA (`ivp_dselnx16t`, `ivp_oltnxf16t`,
`ivp_labvdcmprs2nx8_xp`, `ivp_float16nx16t`, …). A concrete validated decode: `crtbegin.o` →
correct windowed ABI (`entry a1,32` / `retw.n` / `callx8`) and a FLIX bundle
`{ beqz.w15 a3,…; addi.a a4,a4,4 }`, correct even after the symbol table is stripped. The reset
vector of the Q7 POOL core reads `j 0x200` (bytes `06 7f 00`) on Cayman/Mariana/Mariana+ and
`j 0x1e4` (`06 78 00`) on Maverick — read straight out of each generation's SRAM image with this
disassembler, the canonical `[HIGH/OBSERVED]` example.

**What it CANNOT — the FLIX-desync ceiling `[MED]`.** Its config has `IsaMaxInstructionSize = 32`
(`LoadStoreWidth = 512`): bundles run up to 32-byte / 512-bit VLIW. On densely-scheduled FLIX with
interleaved literal pools and hand-written boot/vector stubs (no FLIX property records), the linear
sweep loses bundle sync across some literal/selector-byte boundaries and renders those spans as
`.byte`. Once mis-aligned it produces plausible-but-wrong per-instruction decodes until it
resynchronizes. So everything *above* the desync line (table bases, reset vectors, string-anchored
structures, opcode-enum membership) is `[HIGH/OBSERVED]`; the per-instruction bodies *inside* a
desynced bundle region are `[MED/OBSERVED]` — read, but tooling-bounded. (SUNDA decodes cleanest:
279 FLIX bundles vs 285 `.byte` spans after a non-destructive FLIX-property merge + synthetic
symbol table.) This is the corpus-wide MED ceiling the
[confidence model](../../reference/confidence-model.md) names as wall §4.4, and the
[FLIX-decoding methodology](../../reference/flix-decoding.md) gives the resync discipline that
lifts the spine back to HIGH.

> **GOTCHA — the NCFW management core is a *different* Xtensa and is mis-decoded here.** The
> scalar Xtensa-LX NCFW core has no shipped config; pointing the `ncore2gp` disassembler at it
> greedily mis-frames its `op0=e/f` bytes as Vision FLIX bundles. Source 3 is the Vision-Q7
> witness only; the NCFW core needs its own decode discipline.

**Cross-validation.** Source 3 decodes the *same* instruction streams sources 1–2 describe — three
independent witnesses emitting the same mnemonics. It is the ground-truth check that the
table-derived decode (sources 1–2) is right on real bytes, not just internally consistent.

### 4.4 Source 4 — the customop arch-isa headers (a *different layer*)

The `custom_op/c10/include/` arch-isa headers (`neuron_sunda_arch_isa` = NC-v2, `cayman` = NC-v3,
`mariana` = NC-v4, `maverick` = NC-v5; Mariana+ shares the `mariana` dir; the legacy `arch-isa/` =
TONGA/v1) and the per-generation `instruction_mapping.json` are the **weakest** leg for the *core*
ISA — and the most important to keep in its own box, because it is easy to mistake for the Q7 ISA
and it is not.

> **CORRECTION — the arch-isa headers are TPB tensor-*engine* operand structs, not the Xtensa Q7
> core ISA.** These headers describe the **host-side TPB (Tensor Processing Block) instruction
> word** — the 64-byte pseudo-op (`NEURON_ISA_TPB_INST_NBYTES = 64`, every generation) the host
> compiler (`libwalrus`) emits and the resident Q7 *scheduler firmware* consumes — keyed per
> silicon generation. They are **not** Vision-Q7 machine code. `libwalrus` emits TPB words, *not*
> Q7 Vision FLIX bundles (it has no Q7 instruction emitter at all); the GPSIMD-routed
> TensorTensor opcodes (e.g. the int32 Add/Sub/Mul redirects) are host BIR routing, not Vision TIE
> opcodes. So source 4 cross-validates the **engine-routing / 5-generation axis** — which op runs
> on which engine, and the v1–v5 codename→NC-version map — *not* the Vision opcode set. It is the
> source that grounds *generation* claims; the other three ground the *ISA* claims. `[HIGH/OBSERVED]`
> for what the headers are; treating them as the Q7 ISA is the defect this note forecloses.

What source 4 *does* provide, precisely: a per-generation `OPCODE` enum that grows monotonically
(SUNDA 145 → MAVERICK 165 codes), a `DTYPE` set growing 16 → 30, and an `instruction_mapping.json`
giving the opcode↔struct binding (`struct2opcode` 89 → 114 entries SUNDA → MAVERICK, plus
`struct2pseudo_opcode`) — all generated from the same headers, with binding deltas mirroring
header additions. Those numbers are the **TPB engine ISA** axis. The actual Cadence Q7 NX 32-byte
VLIW encoding is produced offline by `xt-clang++ --xtensa-core=ncore2gp` (custom ops) or baked into
NCFW firmware — never in `libwalrus`, never in these headers (source 3 decodes *that* machine
code). The practical reading: sources 1–3 are three witnesses to the **same Vision-Q7 core ISA**;
source 4 is a witness to the **engine-level layer above it**, included in the four because a
reimplementer must know it exists, must route to it, and must *not* confuse its TPB opcode enum
with the Vision FLIX roster.

---

## 5. The synthesis — the certified ISA model

The four sources combine into one certified model. The combination rule is: **source 2 (TIE DB) is
the authoring superset and the semantic ground truth; source 1 (libisa) is the runtime subset and
the encoding authority; source 3 (disassembler) is the execution witness; source 4 is the
engine-layer crosswalk.** Where they overlap they agree; where they differ, the difference is
characterized and one source wins by role.

### 5.1 The reconciled counts

```
TIE DB (authoring, pre-fold) :  1607 mnemonics  /  12642 OPCODEDEF placements   [source 2]
libisa (runtime, post-fold)  :  1534 opcodes     /  12569 placements             [source 1]
                                ----                ----
delta                        :   +73                +73
```

The pairs are **1607 ↔ 12642** and **1534 ↔ 12569**. The runtime `1534` is a *clean subset* of the
authoring `1607` — **zero** opcodes exist in the runtime tables that are absent from the DB. The
`+73` delta is fully itemized: 24 `*.W18` wide-branch macro expansions (`BEQZ.W18`, `BALL.W18`, …),
6 `xt_virtualops` assembler pseudo-ops (`POPC`, `FFS`, `CLAMPSF`, `SEXTF`, `ADDI.A.N`, `POPCE`), 6
`xt_branchprediction` `RBTB`/`WBTB` BTB-access ops, `HALT`/`HALT.N`, `WAITI`, `WSR.MMID`, and 43
no-body decode-tree pseudo-mnemonics that never reach a leaf instruction. These are forms the
authoring DB carries but the runtime decode tables either fold into a base form or omit — they are
not behavior a reimplementer loses.

### 5.2 The structural agreement

The structural totals are **identical** across the two describing sources and the disassembler
config:

| quantity | TIE DB (source 2) | libisa (source 1) | verdict |
|---|---:|---:|---|
| FORMAT | 14 | 14 | EXACT |
| SLOTDEF / slots | 46 | 46 | EXACT |
| REGFILE | 8 | 8 | EXACT |
| STATE / states | 81 | 81 | consistent |
| CTYPE / ctypes | 64 | 64 | EXACT |
| pipeline `r/e/m/w` | 0/3/4/6 | 0/3/4/6 | EXACT |

The [14-format / 46-slot FLIX grid](flix-encoding.md), the [8 register files](register-files.md),
and the [64 ctypes](ctype-coproc-funcunit.md) all reproduce on both sources. The TIE DB
additionally *supplies* dimensions the libisa `regfiles[]` table lacks (`vec` = 512 b × 32, `wvec`
= 1536 b × 4) — the cross-validation is not just equality, it is one source filling another's gaps
without contradiction.

### 5.3 The coverage certificate

Folding in the TIE DB's semantics (source 2) closes the model. Of the **1607**-mnemonic authoring
roster: **1558** have a bit-precise reference-compute body (member of a rendered `SEMANTIC`/`MODULE`
iclass), **6** are categorical-only ordering fences (`DSYNC`/`ESYNC`/`FSYNC`/`ISYNC`/`RSYNC` +
`SIMCALL`) with no datapath, and **43** are no-body decode-tree pseudo-mnemonics —
`1558 + 6 + 43 = 1607`, exactly. Of the **1534**-mnemonic shipped runtime roster: **1528** are
bit-precise / **6** are fences / **0** are no-body — i.e. **99.6%** of every shipped opcode has a
bit-precise TIE reference body, and the six exceptions are ordering fences that *have* no behavior
to model. There is **no** genuine datapath gap: no semantically-significant shipped op lacks a
reference body.

### 5.4 Where the sources disagree — and which wins

Disagreements are few, characterized, and resolved by role:

| disagreement | nature | resolution |
|---|---|---|
| **roster size** (1607 vs 1534) | DB superset vs runtime subset | both correct for their level; the `+73` is folded macros/pseudo-ops (§5.1). The DB is authoritative for *what can be authored*; libisa for *what the runtime decodes*. |
| **regfile count** (12 vs 8) | the TIE DB enumerates 12 *regfile-class entries*; the hardware file count is 8 | **8 wins** — the extra 4 are `BR` sub-views (`BR2`/`BR4`/`BR8`/`BR16`), not independent files. See [register-files §7](register-files.md#7-the-8-vs-12-reconciliation--files-vs-views). |
| **`gvr` in `tie.h`** | `tie.h`'s `SA_LIST` omits the `gvr`/`gsr` eighth file | **the binary wins** — `libisa`/TIE ship full `gvr` instruction support; `tie.h` omits it because it is a CSR/state file outside the ABI save-context, not because it is absent. |
| **disassembler desync** (source 3) | inside a 512-bit FLIX bundle the linear sweep mis-decodes | **sources 1–2 win** — the table model is the authority; the desync is a tooling limit (§4.3), not an ISA fact. |
| **source 4 vs the Q7 ISA** | the arch-isa headers look like an opcode enum | **not a disagreement** — source 4 is a *different layer* (TPB engine words), not a competing Q7 roster (§4.4). |

In every case the resolution is a *role assignment*, not a coin-flip: the certified model is the
TIE DB's semantics + the libisa runtime encoding + the disassembler execution witness, with the
engine-routing layer kept separate. No two sources contradict on any op that exists in both at the
same level.

---

## 6. The msem overlay and the TIE runtime

The per-op *compute* semantics live in `libtie-core.so` (the base DB). The *memory and control*
semantics live in a **separate** provider, `libtie-Xtensa-msem.so` — a 258,120-byte DLL with the
*identical* 5-getter container ABI, but carrying only a `post_rewrite` blob (its
compiler/xinfo/post_parse blobs are empty stubs). Decoded, that blob is a `<xdoc type="tie">` whose
package is literally `msem` / `xt_msem`, and its census is the complement of the base DB:

```
645  INTERFACE   core control/exception/memory/external-register signal wires
                 (ReplayNextInstruction, WaitForLoads/Stores, ExceptionCause,
                  InstTLBMissException, ER* external-register roster, …)
 ──  FUNCTION    load/store reference semantics — xtms_aligned_load, xt_load_semantic,
                  with IsaMisAlignedLoadExc / RotateAmount / SignExtend* / IsaIsBigEndian
  0  OPCODEDEF / 0 ICLASS / 0 MODULE   — NO per-opcode content
```

So the two providers are **complementary**, named by role in the config (`ncore2gp-params`:
`xml-base-dll = libtie-core.so`, `xml-msem-dll = libtie-Xtensa-msem.so`): `base` = per-op datapath,
`msem` = the memory/control interface seam (`msem` = the machine/memory-semantics half). The token
`msem` appears `0` times in the base DB; the two never overlap.

Neither container parses anything. The **parser** is `libtie.so` (the TIE runtime), exporting
**657** `xtie_*` accessors: `xtie_get_{post_parse,post_rewrite,compiler,xinfo}_phase()` (which
`dlsym` and consume the matching getter), the generic DOM walker (`xtie_xml_item_get_tag` /
`_get_name` / `_find_attr_by_name` / `_get_attr_int_value` / …), and a typed query API over every
construct family (`xtie_iclass`, `xtie_operand`, `xtie_format`, `xtie_slot`, `xtie_state`,
`xtie_module_get_statements`, `xtie_reference_get_statements`, `xtie_semantic_get_statements`, …). A
code generator that walks `xtie_reference_*`/`xtie_module_*` emits exactly the `xdref_*`/`xdsem`
reference functions the ISS embeds — which is the bridge to Part 3.

---

## 7. Relationship to the formal semantics (Part 3)

This page establishes *where the semantics come from*; the **[formal ISA-semantics
model](../../isa/semantics/formal-isa-model.md)** (Part 3) is *what they are*. The relationship is
direct and name-exact:

- The TIE DB's `<MODULE>`/`<SEMANTIC>`/`<REFERENCE>` blocks (source 2) are the **source-of-truth**
  from which the ISS's value and cycle models were generated. The functional simulator
  `libfiss-base.so` carries `xdref_*` value leaves whose names appear **verbatim** in the decoded
  base DB (`xdref_cvt24s_24_16`, `xdref_widestshift_512_64_6`, `xdref_xor_512_512_512`, …); the
  cycle-accurate `libcas-core.so` carries `xdsem` references (`ivp_sem_reduce`, `ivp_mult_add`).
  The name-identity is the proof: `libtie-core` **is** the ISS reference-model source.
- Part 3 therefore decodes its bit-precise per-op model *from `libtie-core`* via the
  [§3.1](#31-the-recovered-transform-the-post-rewrite-tie-xml-cipher) cipher, then triangulates each
  op across up to three independent legs: **(A)** the TIE reference (this DB), **(B)** the
  `cas`/`fiss` binary ISS (the soft-float value oracle the TIE was generated from), and **(C)** an
  external numpy reference simulator. No leg contradicts another in any op group; the gather path
  and the `acc48` MAC are triple-confirmed. Part 3's coverage statement — `1528/1534` shipped
  (99.6%) bit-precise, `1558/1607` of the DB — is the [§5.3](#53-the-coverage-certificate)
  certificate, proven semantic by semantic.

In short: the four sources here certify the *roster and encoding*; Part 3 walks the TIE DB's
`MODULE` AST to certify the *behavior*, and validates it by executing the ISS the TIE generated.
The TIE DB is the hinge between the two.

---

## 8. Cross-references

- [The Canonical ISA Decode Model (libisa-core)](libisa-decode-model.md) — source 1 in depth: the
  format/length predicate tree and the per-slot decoders.
- [The libisa Table Schema & Codec ABI](libisa-table-schema.md) — the raw `opcodes[]`/`opcodedefs[]`
  table layout and the encode-thunk ABI (this page's source-1 leg, owned there).
- [ISA Coverage & the 1534 / 1607 / 12642 Tally](coverage-tally.md) — the dedicated reconciliation
  of the two roster sizes this page derives.
- [The FLIX VLIW Encoding (14 format / 46 slot)](flix-encoding.md) — the format/slot grid both
  describing sources agree on.
- [The Eight Register Files](register-files.md) — the 8-vs-12 files-vs-views reconciliation
  ([§5.4](#54-where-the-sources-disagree--and-which-wins)) and `tie.h`'s `SA_LIST` (the `dbnum`
  source that omits `gvr`).
- [ctype / coproc / funcUnit Tables](ctype-coproc-funcunit.md) — the 64 ctypes and the single
  `Vision` coproc the TIE DB and libisa share.
- [The Complete Formal ISA-Semantics Model](../../isa/semantics/formal-isa-model.md) (Part 3) — the
  bit-precise per-op behavior decoded *from* `libtie-core`, the downstream this page feeds.
- [The Confidence & Walls Model](../../reference/confidence-model.md) — the tag system and the
  FLIX-desync wall (§4.4) that caps source 3.
- [FLIX Bundle-Decoding Methodology](../../reference/flix-decoding.md) — the resync discipline that
  lifts source 3's spine to HIGH.
- [Master Glossary → TIE DB](../../glossary.md) — the one-line anchor (`libtie-core.so`, SHA
  `06fc43ea…`).

---

*Provenance: `libtie-core.so` (51,098,208 B, SHA `06fc43eaf3622ae1…`), its 5 getter exports, the
four `.data` XML blobs, the `+13/mod-256` decode, and the resulting `12642`/`1607` counts are
`[HIGH/OBSERVED]` — re-disassembled, byte-read, and cipher-decoded in-checkout this pass.
`libisa-core.so`'s `12569` encode symbols and `libtie-Xtensa-msem.so`'s `645`-INTERFACE msem
package are `[HIGH/OBSERVED]` from `nm` + decode. The native-disassembler desync ceiling is
`[HIGH/OBSERVED]` for its existence, `[MED]` for desynced interiors. The arch-isa-headers =
TPB-engine-layer distinction is `[HIGH/OBSERVED]`. The Part-3 name-identity (TIE `MODULE`s →
`xdref_`/`xdsem` ISS leaves) is `[HIGH/OBSERVED]`. The four sources share design provenance (one
TIE compile) but independent serializations/tools — that independence is what makes their agreement
a confidence signal. All facts read as derived from shipped-artifact static analysis (lawful
interoperability RE).*
