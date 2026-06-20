# libtie-core + libtie-Xtensa-msem — the TIE Runtime

> **The fourth ISA source, and where the oracle's truth comes from.** The
> [cas Core Surface](./cas-core-surface.md) and [fiss Datapath Oracle](./fiss-datapath-oracle.md)
> pages established the *cas/fiss ISS pair* — a timing oracle bolted to a value
> oracle — as one independent description of the Vision-Q7 ISA. This page documents
> the **TIE runtime**: the two `libtie-*` provider DLLs that *carry* the TIE
> database in shared-object form, plus the `libtie.so` parser that walks it. The
> cas/fiss reference functions (`xdref_*`, `xdsem`) were **generated from** the very
> XML these DLLs hold. So the TIE runtime is not a fifth opinion on the ISA — it is
> the **source the ISS was compiled from**, and the place a re-implementer reads
> bit-precise reference semantics directly. The mnemonic count where all sources
> agree is **1607** (pre-fold authoring superset; see the
> [1534/1607/12642 tally](../isa/core/coverage-tally.md)).

All facts below are derived from static analysis of the shipped binaries only.
Three files carry the story:

```
extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libtie-core.so        51,098,208 B (51 MB)  — the BASE TIE-DB container
extracted/nested/gpsimd_tools_tgz/tools/ncore2gp/config/libtie-Xtensa-msem.so    258,120 B          — the MSEM (machine-semantics) container
extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/lib/libtie.so                424,616 B          — the TIE RUNTIME / parser (657 xtie_* accessors)
```

Tooling: `nm -D`, `readelf -SW/-V/-d`, `objdump -d`, plus a `python3` cipher decoder
run against the embedded blobs. Each claim is tagged **confidence** × **provenance**
(`OBSERVED` = read directly off the binary this pass, `INFERRED` = strong deduction
from observed evidence, `CARRIED` = taken from a prior report and re-checked).

---

## 1. Three files, three roles

The TIE runtime is *split* across a parser and one-or-more data providers, in the
classic Tensilica plugin shape: the parser `dlopen`s a provider DLL and pulls the
TIE-XML out through a fixed five-symbol ABI.

| File | Size | ELF | Strip | Role | Conf · Prov |
|---|---|---|---|---|---|
| `libtie-core.so` | 51,098,208 B | ELF64 x86-64 | not stripped | BASE TIE-DB **data container** (the full 1607/12642 DB) | HIGH · OBSERVED |
| `libtie-Xtensa-msem.so` | 258,120 B | ELF64 x86-64 | not stripped | MSEM **data container** (memory/control overlay) | HIGH · OBSERVED |
| `libtie.so` (XtensaTools/lib) | 424,616 B | ELF64 x86-64 | **stripped** | the TIE **runtime / parser** (`xtie_*`) | HIGH · OBSERVED |
| `libtie.def` (TIE/lib/TIE) | text | — | — | the 5-getter EXPORTS **contract** | HIGH · OBSERVED |

> **NOTE — naming.** `tie` = *Tensilica Instruction Extension*. The TIE database is
> the authoring source for the entire ISA (operations, encodings, formats, register
> files, and the bit-precise reference semantics). The two `libtie-*.so` files are
> **not** parsers — they are the on-disk, in-shared-object serialization of the
> TIE-compiler's XML export, vended by trivial getters. The parser is a separate
> file, `XtensaTools/lib/libtie.so`. *(HIGH · OBSERVED.)*

> **GOTCHA — `libtie.so` ≠ `libtie-core.so`.** The 424 KB `libtie.so` under
> `XtensaTools/lib/` is the **runtime** (657 `xtie_*` exports, parses XML). The
> 51 MB `libtie-core.so` under `ncore2gp/config/` is the **data** (4 XML blobs, 5
> exports). They share a `VERS_1.1` version-def whose BASE name is the same generic
> identity string `libtie-Xtensa.so`, which makes them easy to conflate. Keep them
> distinct: one is code, one is the 49 MB ciphered DB. *(HIGH · OBSERVED.)*

Both providers carry **zero RTTI** (no `_ZTV`/`_ZTI`/`_ZTS`) and **zero imports** —
structurally confirmed below. Neither has a GNU build-id (`readelf -n` shows only
the `.comment` GCC tag); the `libtie.so` runtime's mtime is Jun-2022 (the
RI-2022.9 / XtensaTools `xti2752.9` toolchain — that exact version string is in
`libtie-core.so`'s `.rodata`, see §2), the provider DLLs were repackaged Nov-2025.

---

## 2. The provider ABI — the five-symbol "XML provider" contract

Both `libtie-core.so` and `libtie-Xtensa-msem.so` export the **identical** five
symbols, exactly matching the `EXPORTS` clause of `libtie.def`:

```
$ nm -D --defined-only libtie-core.so       (libtie-Xtensa-msem.so is byte-identical here)
0000000000000340 T interface_version@@VERS_1.1
0000000000000350 T get_xml_post_parse@@VERS_1.1
0000000000000360 T get_xml_post_rewrite@@VERS_1.1
0000000000000370 T get_xml_compiler@@VERS_1.1
0000000000000380 T get_xml_xinfo@@VERS_1.1
0000000000000000 A VERS_1.1
```

```
$ cat XtensaTools/TIE/lib/TIE/libtie.def
EXPORTS
  interface_version
  get_xml_compiler
  get_xml_post_parse
  get_xml_post_rewrite
  get_xml_xinfo
```

| Export | Addr | Body | Returns | Conf · Prov |
|---|---|---|---|---|
| `interface_version` | `0x340` | `movsd 0x58(%rip),%xmm0 ; ret` | `double` **1.0** (`.rodata 0x3a0` = `00 00 00 00 00 00 f0 3f`) | HIGH · OBSERVED |
| `get_xml_post_parse` | `0x350` | `lea xml_data_post_parse(%rip),%rax ; ret` | `&` 50-B stub blob | HIGH · OBSERVED |
| `get_xml_post_rewrite` | `0x360` | `lea xml_data_post_rewrite(%rip),%rax ; ret` | `&` the **full DB** blob | HIGH · OBSERVED |
| `get_xml_compiler` | `0x370` | `lea xml_data_compiler(%rip),%rax ; ret` | `&` compiler side-table blob | HIGH · OBSERVED |
| `get_xml_xinfo` | `0x380` | `lea xml_data_xinfo(%rip),%rax ; ret` | `&` regfile-info blob | HIGH · OBSERVED |

The `.text` section is **0x48 bytes total** (5 stubs); there is **no parser, no
decompressor, no decoder** in either DLL, and the import list is **empty**:

```
$ readelf -SW libtie-core.so | rg 'text|rodata|\.data'
  [ 7] .text   PROGBITS  0000000000000340 000340 000048 AX     ; 0x48 B = the 5 stubs
  [ 8] .rodata PROGBITS  0000000000000388 000388 000020 A      ; the double 1.0 + version str
  [13] .data   PROGBITS  0000000000201018 001018 30b98c8 WA    ; 51.0 MB = the 4 blobs
$ nm -D libtie-core.so | rg ' U ' | wc -l
0
```

> **CORRECTION — `interface_version()` returns 1.0, not an opaque stamp.** The body
> loads `.rodata 0x3a0`, which is the IEEE-754 double `0x3ff0000000000000` = exactly
> **1.0**. The adjacent `.rodata` at `0x388` spells the ASCII string
> `Xtensa xti2752.9` — the XtensaTools release tag. So `interface_version` is a
> *provider-ABI version* the runtime checks before trusting the blob layout, and its
> current value is `1.0`. *(HIGH · OBSERVED — `movsd`/double bytes both read off the
> file; supersedes the prior report's "returns a double" without the value.)*

### 2.1 The provider-role binding

The `ncore2gp` params file names the two providers **by role**, and `nativesim`
(the native ISS driver) references both the role names and all four getter names:

```
$ rg 'xml-(base|msem|tie)-dll' XtensaTools/config/ncore2gp-params
561:xml-base-dll = .../ncore2gp/config/libtie-core.so
562:xml-msem-dll = .../ncore2gp/config/libtie-Xtensa-msem.so
570:xml-tie-dll  =                       (third role, empty in this config)

$ rg -ao 'get_xml_\w+|xml-(base|msem|tie)-dll' XtensaTools/lib/tc/nativesim | sort -u
get_xml_compiler  get_xml_post_parse  get_xml_post_rewrite  get_xml_xinfo
xml-base-dll      xml-msem-dll        xml-tie-dll
```

So `libtie-core` = the **`xml-base`** provider (the per-op datapath DB);
`libtie-Xtensa-msem` = the **`xml-msem`** provider (the machine-semantics overlay).
The runtime selects a provider per role and reads it through the five-getter
contract. *(HIGH · OBSERVED — both the params binding and the `nativesim` string
refs are read off the files.)*

---

## 3. The four blobs — TIE-compiler **phase** serializations

`libtie-core.so`'s 51 MB `.data` carries **four XML blobs**, one per TIE-compiler
*phase* of the **same single** Cairo DB. Symbol VMAs give their boundaries; the end
marker is `_xti_tie_base`. Because `.data` is **not** VMA==file-offset, sizes and
file reads use the ncore2gp delta of **`0x200000`** (`.data` VMA `0x201018` ↔ file
offset `0x1018`):

| Blob (symbol) | Getter | VMA | Size (next-sym − this) | Decoded `<xdoc>` | Conf · Prov |
|---|---|---|---|---|---|
| `xml_data_post_parse` | `get_xml_post_parse` | `0x201018` | **50 B** | near-empty placeholder (header only) | HIGH · OBSERVED |
| `xml_data_post_rewrite` | `get_xml_post_rewrite` | `0x20104a` | **49,024,221 B** | `type="tie" name="core"` — **THE FULL DB** | HIGH · OBSERVED |
| `xml_data_compiler` | `get_xml_compiler` | `0x30c1d27` | **2,055,814 B** | `type="minimal-tie" name="compiler_xml"` | HIGH · OBSERVED |
| `xml_data_xinfo` | `get_xml_xinfo` | `0x32b7bad` | **11,563 B** | `type="minimal-tie" name="xinfo_xml"` | HIGH · OBSERVED |
| `_xti_tie_base` (end) | — | `0x32ba8d8` | — | end marker | HIGH · OBSERVED |

The phases are TIE-compiler intermediate-representation snapshots of the one DB —
**not different cores**:

- **post_parse** — the just-after-parse snapshot, *emptied* in the shipped build (a
  50-byte stub).
- **post_rewrite** — the post-rewrite / post-`gen` DB: the folded, fully-elaborated
  operation / encoding / semantics tree the tools consume. Decoded header (this
  pass): `<xdoc type="tie" name="core" xdocversion="202000" endian="little"
  rstage="0" estage="3" mstage="4" wstage="6" instbuf_width="256">` — byte-for-
  structure identical to the standalone `Xtensa.xml`'s decoded head. This is the
  blob that matters.
- **compiler_xml** — a `minimal-tie` `<PROPERTY>` list (`specialized_op_BEQZ_BEQZ_W15`,
  `specialized_op`, …): the compiler-directive / property side-table.
- **xinfo_xml** — a `minimal-tie` `<REGFILE>` geometry table (`<ID name="AR"/>
  <INT value="32"/>` …): the register-info quick-reference.

> **GOTCHA — phase ≠ silicon generation.** `post_parse / post_rewrite / compiler /
> xinfo` are TIE-**compiler phases**, and `xdocversion="202000"` / `"201000"` are
> TIE-XML **format versions** — both are axes of the *one* Cairo config (Xtensa24,
> NX1.1.4, RI-2022.9), **not** one of the five firmware silicon generations. The
> `rstage/estage/mstage/wstage = 0/3/4/6` are pipeline stages of that one core. No
> silicon-gen fact is read out of any TIE/msem descriptor on this page (§8).
> *(HIGH · OBSERVED for the tokens; the gen-distinction is CARRIED from GEN-/ISA-side
> findings.)*

### 3.1 The cipher — `+13 / mod-256` with the `s..z` overflow remap

Every blob is `[ASCII-hex checksum] '|' [body]`, where the body is the XML plaintext
transformed by **add-13 mod-256** plus a small high-byte remap. The **inverse**
(decode) is a per-byte map — annotated C:

```c
/* Decode one libtie-core / libtie-Xtensa-msem XML blob in place.
 * `blob` points at the bytes returned by get_xml_*(); `n` is its length.
 * Layout:  <hex-checksum> '|' <ciphered-body>
 * Cipher:  ciphertext = (plaintext + 13) mod 256, with a remap that pulls the
 *          high letters s..z back into ASCII (they would otherwise land at
 *          0xF2..0xFA after the +13 wrap of 'f'..'m'-class source bytes).
 * Returns a pointer to the start of the decoded XML body, or NULL on malformed
 * input. Decodes the body buffer in place. O(n) over the body length.
 *
 * Verified: libtie-core.so post_rewrite first body bytes
 *   49 4c 05 7a 79 2d 03 72 7f ff 76 7c 7b 4a ...
 * decode to "<?xml version=\"1.0\" encoding=\"ISO-8859-1\" ?>\n<xdoc type=\"tie\"
 *   name=\"core\" xdocversion=\"202000\" ...>".  The checksum prefix observed is
 *   "2ec0cd3"; the standalone Xtensa.xml carries the *same* body under prefix
 *   "2b6c80e" — same content, fresh checksum. */
static char *tie_xml_decode(unsigned char *blob, size_t n) {
    /* 1. locate the '|' that terminates the hex checksum */
    unsigned char *bar = memchr(blob, '|', n);
    if (!bar)                       /* malformed: no checksum terminator */
        return NULL;
    unsigned char *body = bar + 1;
    size_t body_len = (size_t)(blob + n - body);

    /* 2. high-byte remap table: post-(+13) value -> recovered ASCII letter.
     *    Only these eight pre-remap codepoints occur in well-formed bodies. */
    static const unsigned char remap_F2_FA[9] = {
        /*0xF2*/ 's', 0, /*0xF4*/ 't', /*0xF5*/ 'u', /*0xF6*/ 'v',
        /*0xF7*/ 'w', /*0xF8*/ 'x', /*0xF9*/ 'y', /*0xFA*/ 'z'
    };

    /* 3. inverse transform, byte by byte */
    for (size_t i = 0; i < body_len; i++) {
        unsigned p = (unsigned)(body[i] - 13) & 0xFF;   /* undo +13 mod 256 */
        if (p >= 0xF2 && p <= 0xFA && remap_F2_FA[p - 0xF2])
            p = remap_F2_FA[p - 0xF2];                  /* pull s..z back to ASCII */
        body[i] = (unsigned char)p;
    }
    return (char *)body;            /* decoded <?xml ...><xdoc ...> ... */
}
```

> **NOTE — the cipher is binary-resident and identical across all six TIE files.**
> The same `+13/mod-256` + `s..z` remap decodes `libtie-core`'s four blobs, the
> `libtie-Xtensa-msem` blob, and the standalone `Xtensa.xml`. The ciphered *body*
> of `libtie-core`'s post_rewrite is **byte-identical** to the standalone
> `Xtensa.xml` body; only the checksum prefix differs (`2ec0cd3` vs `2b6c80e`). So
> the DLL embeds the same DB the standalone file holds, re-emitted with a fresh
> checksum. The recovered XML *is* a binary-derived artifact — it is the decode of
> bytes in a shipped `.so`. The formal-semantics model
> ([The Complete Formal ISA-Semantics Model](../isa/semantics/formal-isa-model.md))
> was built by running this decode and walking the resulting tree.
> *(HIGH · OBSERVED — the decode was run this pass; the body byte-identity is a
> direct comparison.)*

---

## 4. The base DB and the **1607-mnemonic** alignment

Decoding the 49 MB `post_rewrite` blob and counting structural tags gives the
authoring-superset census — and it matches the standalone TIE-DB **exactly**, with
**no silicon-gen inference**:

| Token (decoded base DB) | Count | Standalone DB | Match | Conf · Prov |
|---|---|---|---|---|
| `<OPCODEDEF` placements | **12,642** | 12,642 | EXACT | HIGH · OBSERVED |
| distinct mnemonics (per-OPCODEDEF first `<ID name>`) | **1,607** | 1,607 | EXACT | HIGH · OBSERVED |
| `<ICLASS` | 1,458 | 1,458 | EXACT | HIGH · OBSERVED |
| `<SEMANTIC` | 304 | 304 | EXACT | HIGH · OBSERVED |
| `<MODULE` (open tags) | 8,250 | — | (open+close over 1045 elements) | HIGH · OBSERVED |
| `xdref_` token | 19,476 | — | — | HIGH · OBSERVED |
| `xdsem` token | 1,047 | — | — | HIGH · OBSERVED |

Sample decoded context (this pass): `<OPCODEDEF package="xt_wide_branch"
file="coretie2_internalmodules.tie"><ID name="BEQZ.W18"/>…` — the same single
synthetic source file, and the same `BEQZ.W18` TIE-DB-only wide-branch macro.

> **The 1607 vs 1534 split (why all four sources agree).** **1607** is the *pre-fold
> authoring superset* — every `<OPCODEDEF>` distinct `<ID>` in the TIE database. The
> runtime `opcodes[]` table the disassembler/ISS actually consume holds **1534**;
> the difference (**+73**) is the alias/macro/wide-branch (`*.W18`, virtualops, btb)
> forms the generator folds away. The TIE runtime is where the **1607** authoring
> number is directly observable (count the decoded `<OPCODEDEF>` IDs); the shipped
> binaries carry the folded **1534**. Both are correct for their layer — see the
> [1534/1607/12642 tally](../isa/core/coverage-tally.md) for the full reconciliation
> and the [TIE database four-source model](../isa/core/tie-database.md) for why four
> independently-generated files all back-trace to this one DB.
> *(HIGH · OBSERVED — the 1607/12642 counts were decoded from the blob this pass,
> upgrading the tally page's CARRIED tag on those two figures to OBSERVED.)*

---

## 5. What **msem** is — the machine/memory-semantics overlay

`libtie-Xtensa-msem.so` is a *smaller instance of the same five-getter container
schema*, but it carries **only** a `post_rewrite` blob; its other three blobs are
empty. Sizes from the symbol table (same `0x200000` delta):

| msem blob | Size | Content |
|---|---|---|
| `xml_data_post_parse` | 50 B | stub |
| `xml_data_post_rewrite` | **251,263 B** (decoded 251,257 chars) | **the msem content** |
| `xml_data_compiler` | 123 B | empty `<xdoc type="minimal-tie" name="compiler_xml">` |
| `xml_data_xinfo` | 124 B | empty `<xdoc type="minimal-tie" name="xinfo_xml">` |

Decoding the msem `post_rewrite` and taking a construct census (this pass):

| Construct | Count | Meaning | Conf · Prov |
|---|---|---|---|
| `<INTERFACE` | **645** | core hardware-interface SIGNAL declarations | HIGH · OBSERVED |
| `<FUNCTION` | **118** | shared reference-compute / load-store semantic helpers | HIGH · OBSERVED |
| `<PACKAGE` / `<ENDPACKAGE` | 4 / 4 | the `msem` › `xt_msem` package nesting | HIGH · OBSERVED |
| `<OPCODEDEF` / `<ICLASS` / `<MODULE` / `<SEMANTIC` | **0 / 0 / 0 / 0** | **no per-opcode content** | HIGH · OBSERVED |

So **msem ≠ the per-instruction operation semantics** (those live in the base DB's
MODULEs/OPCODEDEFs). msem is the core's **machine / memory-system semantics**
package, with two halves:

**(a) the 645 INTERFACE wires** — the control / exception / memory / external-register
signal surface. Decoded names include `ReplayNextInstruction`, the exception roster
(`ExceptionCause`, `ExceptionVAddr`, `InstTLBMissException`, `EncodedExceptionVector`),
the external-register bus (`ERAddr`, `ERData`, `ERRead`, `ERWrite`), the branch
predictor (`BranchPredictorReadData`), block prefetch, and the exclusive-access
state (`EXCLRES`/`EXCLRES_next`). These are the *signal* boundary the pipeline drives.

**(b) the 118 FUNCTIONs** — the **load/store reference semantics**: `xtms_aligned_load`,
`xtms_aligned_store`, `xtms_aligned_store_check`, `xt_load_semantic`, gated by the
access predicates `IsaMisAlignedLoadExc`, `IsaSysHandlesMisAlignedLoad`,
`IsaHardwareHandlesMisAlignedLoad`, `IsaIsBigEndian`, with operand helpers `VAddr`,
`LSBytes`, `RotateAmount`, `LoadByteDisable`, `SignExtendFrom`/`To`. The `xtms_`
prefix = *Xtensa machine semantics*.

> **CORRECTION — the msem `<xdoc>` header still says `name="core"`.** It is tempting
> to expect `name="msem"`, but the decoded msem doc header is
> `<xdoc type="tie" name="core" xdocversion="202000" … instbuf_width="256">` — the
> **same** doc identity as the base DB. The `msem` / `xt_msem` identity lives in the
> `<PACKAGE>` body (the 4 PACKAGE/ENDPACKAGE pairs nest `msem` › `xt_msem`), **not**
> in the doc header. Distinguish the two providers by their *content* (645 INTERFACE
> + 118 FUNCTION, 0 OPCODEDEF) and by the `xml-msem-dll` role binding, not by the
> `<xdoc name>` attribute. *(HIGH · OBSERVED — both decoded headers read off the
> blobs this pass.)*

> **NOTE — "m" = memory or machine?** The *content* (memory-access INTERFACE +
> load/store reference FUNCTIONs) is OBSERVED; the exact gloss of the "m" in `msem`
> /`xtms_` is *machine* (the prefix `xt_ms` / `xtms_` reads "Xtensa machine
> semantics"), with the dominant content being the memory pipe. Read it as the
> **memory/control half** of the TIE model, kept in a separate provider from the
> per-op datapath DB. *(HIGH · OBSERVED content; MED · INFERRED on the acronym.)*

The two providers are **complementary, not overlapping**: the token `msem` appears
**0 times** in the base DB, and the base DB's `<OPCODEDEF>`/`<ICLASS>`/`xt_ivp32`
constructs appear **0 times** in msem. `xml-base-dll` = per-op datapath;
`xml-msem-dll` = memory/control interface. *(HIGH · OBSERVED.)*

---

## 6. The parser — `libtie.so` and its 657 `xtie_*` accessors

The blobs carry no parser. The parser is `XtensaTools/lib/libtie.so` — the TIE
**runtime**, **657** exported `xtie_*` accessors (stripped of local symbols; only
the `@@VERS_1.1` dynamic exports remain). Its NEEDED set is `libcommon.so`,
`libstdc++`, `libm`, `libgcc_s`, `libc` — a normal C++ tool, not a self-contained
table.

> **CORRECTION — the runtime exports 657 `xtie_*`, not 658.**
> `nm -D --defined-only libtie.so | rg -c 'xtie_'` = **657**, and the total
> `T`-export count is also 657 (every dynamic export is an `xtie_*`; there are no
> non-`xtie_` exports beyond the `VERS_1.1` anchor). Prior framing of "658" is off
> by one. *(HIGH · OBSERVED.)*

### 6.1 The phase getters — one runtime accessor per provider getter

The four runtime phase accessors map 1:1 onto the provider's four getters:

| Runtime (`libtie.so`) | Addr | Consumes (provider) | Conf · Prov |
|---|---|---|---|
| `xtie_get_post_parse_phase` | `0xeb50` | `get_xml_post_parse()` | HIGH · OBSERVED |
| `xtie_get_post_rewrite_phase` | `0xeb60` | `get_xml_post_rewrite()` | HIGH · OBSERVED |
| `xtie_get_compiler_phase` | `0xeb70` | `get_xml_compiler()` | HIGH · OBSERVED |
| `xtie_get_xinfo_phase` | `0xeb80` | `get_xml_xinfo()` | HIGH · OBSERVED |

### 6.2 The generic XML DOM walker

The decoded blob is walked as a tag tree by ten `xtie_xml_item_*` accessors:

```
xtie_xml_item_get_tag   _get_name   _get_text   _get_first_item   _get_next
xtie_xml_item_find_attr_by_name   _get_attr_int_value   _get_attr_value
xtie_xml_item_is_text   _write
```

### 6.3 The typed query API (per ISA-construct family)

On top of the DOM walker, `libtie.so` exposes a typed accessor family per TIE
construct. Export counts per family (`nm -D --defined-only | rg -c 'xtie_<fam>'`):

| Family | Exports | | Family | Exports | | Family | Exports |
|---|---|---|---|---|---|---|---|
| `operand` | 47 | | `function` | 16 | | `format` | 13 |
| `imap` | 37 | | `ctype` | 16 | | `slot` | 12 |
| `find` | 37 | | `opcode` | 15 | | `state` | 11 |
| `interface` | 28 | | `property` | 14 | | `semantic` | 8 |
| `iclass` | 27 | | `module` | 13 | | `reference` | 8 |
| `operation` | 25 | | `regport` | 20 | | `proto` | 8 |
| `signals` | 21 | | `regfile` | 17 | | `opcodedef` | 8 |
| | | | | | | `exception` | 7 |

### 6.4 The semantic accessors — the ISS-generation path

The accessors that return the **bit-precise reference-compute AST** are the ones a
code generator walks to emit the ISS's `xdref_*`/`xdsem` functions:

```
xtie_semantic_get_statements  / _get_signals / _get_inst_iter      (the <SEMANTIC> blocks)
xtie_reference_get_statements / _get_signals / _get_inst_iter      (the <REFERENCE> blocks)
xtie_module_get_statements    / _get_arg_iter / _arg_get_dir / _from_index / _to_index
```

```c
/* Sketch of the lookup a generator runs to emit one op's reference function.
 * Walks libtie.so's runtime API; all names are real exported symbols.
 * Returns the statement-list AST root for `mnemonic`'s reference compute,
 * or NULL if the op has no <REFERENCE> block. */
xtie_xml_item *tie_reference_ast_for(xtie_phase phase, const char *mnemonic) {
    /* 1. obtain the parsed post_rewrite DOM (this drives get_xml_post_rewrite()
     *    under the hood, decodes the blob, and builds the tag tree) */
    xtie_xml_item *root = xtie_get_post_rewrite_phase(phase);   /* libtie.so 0xeb60 */

    /* 2. find this op's <REFERENCE> by name via the typed query family */
    xtie_reference ref = xtie_reference_find(root, mnemonic);   /* xtie_find/_reference */
    if (!ref)
        return NULL;                                            /* no reference model */

    /* 3. pull the bit-precise wire/assign statement list — the AST the
     *    generator lowers into xdref_<mnemonic>_<W>_<W> (fiss) or xdsem (cas) */
    return xtie_reference_get_statements(ref);                  /* libtie.so 0x11170 */
}
```

Consumers of the getters (string + reloc refs): `XtensaTools/lib/tc/nativesim`
(the native ISS), `XtensaTools/libexec/xt-gen-os`, the LLVM config generators
(`llvm-tblgen`, `llvm-xtensa-config-gen`), and `libtie.so` itself; `libauto.so` and
`libfusion.so` list `libtie.so` as NEEDED. The TIE runtime is an
authoring / config-generation / native-sim path. *(HIGH · OBSERVED.)*

---

## 7. The link to cas / fiss — `libtie-core` **is** the ISS source

The cas (cycle-accurate; [cas Core Surface](./cas-core-surface.md)) and fiss
(functional; [fiss Datapath Oracle](./fiss-datapath-oracle.md)) models do **not**
dynamically link `libtie.so` or import any `xtie_*`. They are the
statically-built, pre-folded decode/exec libraries that **embed** reference
functions generated *from* the base DB's semantic blocks. The proof is
**name-identity**:

```
$ nm -D libfiss-base.so | rg -c 'xtie_'              ; nm -D libcas-core.so | rg -c 'xtie_'
0                                                      0                       (neither imports the runtime)
$ nm -D --defined-only libfiss-base.so | rg 'xdref_widestshift_512_64_6|xdref_cvt24s_24_16'
... T xdref_widestshift_512_64_6     ... T xdref_cvt24s_24_16
$ nm libcas-core.so | rg -c 'xdsem'
148
```

| ISS binary | Carries | Source in the base DB | Conf · Prov |
|---|---|---|---|
| `libfiss-base.so` | `xdref_*` value helpers (`xdref_widestshift_512_64_6`, `xdref_cvt24s_24_16`, `xdref_xor_512_512_512`, …) | base-DB `<REFERENCE>` blocks; `xdref_` token count 19,476 in the decoded DB | HIGH · OBSERVED |
| `libcas-core.so` | 148 `xdsem` references (+ `ivp_sem_*`) | base-DB `<SEMANTIC>` blocks; `xdsem` token count 1,047 in the decoded DB | HIGH · OBSERVED |

The **same** `xdref_*` names appear *inside* the decoded `libtie-core` base DB and
*as exports* of `libfiss-base.so` — the name-identity **is** the proof that the TIE
DB's `<REFERENCE>`/`<MODULE>`/`<SEMANTIC>` blocks are the source-of-truth from which
the fiss `xdref` and cas `xdsem` functions were generated, and that
`xtie_reference_*`/`xtie_module_*`/`xtie_semantic_*` are the exact query path a
generator walks to emit them.

> **NOTE — msem's link is structural, not name-identical.** Unlike the
> `xdref`/`xdsem` datapath link, the msem `xtms_*` helper names do **not** appear
> verbatim in fiss/cas — the memory model is compiled into the pipeline, not kept as
> named helpers. So the msem → ISS link is *structural* (same reference behavior:
> alignment, misaligned-exception routing, byte-disable, rotate, sign-extend,
> endianness — the load/store side documented at the binary level by the
> functional-ISS pages) rather than name-exact. *(HIGH · OBSERVED for the
> `xdref`/`xdsem` link; MED · INFERRED for the msem path.)*

The takeaway for a re-implementer: `libtie-core` (base DB) feeds the per-op
**datapath** semantics, `libtie-Xtensa-msem` (msem) feeds the **memory/control**
semantics, and together they are the TIE-XML origin of the ISS `xdsem`/`xdref`
model. To read a bit-precise reference for any op, decode the post_rewrite blob and
walk its `<REFERENCE>`/`<MODULE>` for that mnemonic — that *is* the reference the
oracle was compiled from.

---

## 8. False-positive guard — no silicon-gen fact from any TIE descriptor

No silicon-generation / gen-count / codename is inferred from any TIE or msem
descriptor on this page. Every "version / variant / stage / phase" token in the TIE
data is a **config-version** or **compiler-phase** axis of the one Cairo config:

- `xdocversion="202000"` (base/post_rewrite) and `"201000"` (compiler/xinfo) =
  TIE-XML **format** versions, not silicon gens.
- `post_parse` / `post_rewrite` / `compiler` / `xinfo` = TIE-compiler **phases** of
  one DB, not generations.
- `rstage/estage/mstage/wstage = 0/3/4/6` = **pipeline** stages of the one Cairo
  core, not generations.
- the package name `msem` / `xt_msem` = a TIE **package**, not a gen codename.

The five firmware silicon generations are a *firmware-image* axis and are **not**
visible in — and were **not** read out of — any TIE-config descriptor here.
*(HIGH — the guard is held by construction; the gen-distinction is CARRIED from the
GEN-/ISA-side findings.)*

---

## 9. Key functions (re-implementation entry points)

| # | Symbol (binary) | Addr | Role | Conf · Prov |
|---|---|---|---|---|
| K1 | `get_xml_post_rewrite` (`libtie-core` / msem) | `0x360` | returns `&` the folded TIE-DB blob — the primary entry the ISS/tools `dlsym` to obtain the full (base) or memory-semantics (msem) DB | HIGH · OBSERVED |
| K2 | `interface_version` (`libtie-core` / msem) | `0x340` | returns the provider-ABI version double `1.0`; the runtime checks it before trusting the blob layout | HIGH · OBSERVED |
| K3 | `xtie_get_post_rewrite_phase` (`libtie.so`) | `0xeb60` | runtime side: drives the getter, decodes the blob, builds the in-memory TIE DOM walked by `xtie_xml_item_*` | HIGH · OBSERVED |
| K4 | `xtie_reference_get_statements` / `xtie_module_get_statements` / `xtie_semantic_get_statements` (`libtie.so`) | `0x11170` / `0xfee0` / `0x11810` | return the bit-precise reference-compute AST per op — the accessors a generator walks to emit the ISS `xdref`/`xdsem` functions | HIGH · OBSERVED |
| K5 | `xtms_aligned_load` / `xt_load_semantic` (msem FUNCTIONs) | (XML) | the core's load reference semantics: alignment, misaligned-exception routing, byte-disable, rotate, sign-extend, endianness — the memory-pipe half of the ISS reference model | HIGH · OBSERVED |

---

## 10. Open items / uncertainty

- **[MED]** The exact gloss of "m" in `msem`/`xtms_` (*machine* vs *memory*) — the
  content (memory/control INTERFACE + load/store reference FUNCTIONs) is OBSERVED;
  only the acronym is inferred.
- **[MED]** The msem → ISS link is *structural* (the `xtms_*` helpers are compiled
  into the pipeline, not retained as named fiss/cas symbols), unlike the
  name-identical `xdref`/`xdsem` datapath link.
- **[LOW]** The standalone `.tl` on-disk record schema and `libtie.so`'s internal
  DOM layout were not decoded — the decoded XML is the better semantics source, and
  it is not needed for this inventory.
- **[HIGH]** Everything in §1–§7 and §9 (the three files, the five-getter ABI, the
  four blobs + cipher, the 1607/12642 base-DB census, the msem census, the 657
  `xtie_*` parser API + phase mapping, and the `xdref`/`xdsem` name-identity to
  cas/fiss) is direct binary evidence read or decoded this pass.
