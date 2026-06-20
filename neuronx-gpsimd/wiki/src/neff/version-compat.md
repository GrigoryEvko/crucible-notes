# NEFF Version / Compatibility Model

The NEFF container carries version and capability information at **three independent
tiers**, each enforced by a *different* function inside `libnrt.so`, in a fixed load
order. A failure at any tier aborts `nrt_load` with a distinct status code or error
cause, so the failing tier is diagnosable from the log. This page reproduces each
gate's accept/reject control flow as annotated C, anchored to the real symbol, address,
struct offset, enum value, and `.rodata` string.

All addresses are from `libnrt.so.2.31.24.0` (`opt/aws/neuron/lib/`,
build `2.31.24.0-0b044f4ce`). `.text`/`.rodata` are VMA == file-offset
(`readelf -SW`: `.text` @ `0x3dbc0`, `.rodata` @ `0x7cf000`); offsets below are
verified against `*_structures.json` / `*_enums.json`. Tags: confidence `[HIGH/MED/LOW]`
× evidence `[OBSERVED/INFERRED/CARRIED]`.

The three tiers, in load order:

| Tier | Concern | Function @ addr | What it gates |
|:---|:---|:---|:---|
| **1** | Container version | `neff_parse` @`0x4ca3f0` | `neff_version_major` ceiling, `feature_bits` trapdoor, `pkg_version` archive format |
| **2** | Architecture target | `kelf::kelf::parse_target` @`0x497c10` + `kelf::kelf::load` @`0x497dc0` | `kelf-a.json` `target` → arch vs **live instance** arch |
| **3** | Per-section | `parse_one_ucode_lib` @`0x4b1610`, `kbl_model_add` @`0x3058e0`, `parse_one_dma_ring` @`0x4b5f80` | GPSIMD ucode semver, EVTACCEL SB carveout shape, typed DMA rings |

The header byte layout these gates read is in
[container byte format](container-byte-format.md#1-the-1024-byte-header--neff_header_t--high--observed); the per-section
`def.json` schema each Tier-3 gate consumes is the
[`kelf_load_from_neff` section→parser map](container-byte-format.md#5-section--parser-map--kelf_load_from_neff-0x4c0870--high--observed);
the ucode ABI version this tier checks ties to the
[nrtucode version getters](../runtime/version-extisa-getters.md).

---

## 1. Structural pre-gate — `neff_get_header_from_buffer` @`0x4ca2c0`  `[HIGH × OBSERVED]`

Before any version field is read, `neff_parse` calls the structural validator. There is
**no literal magic number**; the header is identified by size invariants alone. The
header is `neff_header_t` (IDA ordinal **5896**, size **0x400 = 1024**).

```c
// neff_get_header_from_buffer @0x4ca2c0 — returns the header ptr or NULL
const neff_header_t *neff_get_header_from_buffer(const uint8_t *neff, uint64_t neff_size)
{
    if (!neff)                          // "Invalid NEFF buffer"
        return NULL;
    if (neff_size <= 0x3FF)             // floor: a header is 0x400 bytes
        return NULL;                    // "Invalid number of NEFF bytes received %lx"
    if (*((uint64_t*)neff + 1) >= neff_size)   // header_size (+0x8) must be < file size
        return NULL;                    // "Invalid NEFF file size hdr sz: %lx, file sz: %lx:"
    return (const neff_header_t *)neff;  // never reads a version field here
}
```

A NULL return makes `neff_parse` bail with code **2** (`NRT_INVALID`). The version fields
are never touched in this function — it is purely a buffer-size sanity check.

The version/compat-relevant `neff_header_t` members (byte-exact from `*_structures.json`,
offsets HEX):

| Off | Type | Member | Role |
|:---|:---|:---|:---|
| `+0x000` | `u64` | `pkg_version` | `1` = raw-tar + SHA256, `2` = gzip-tar + MD5 (§4) |
| `+0x008` | `u64` | `header_size` | `== 0x400`; validated `< file_size` above |
| `+0x010` | `u64` | `data_size` | inner-archive bytes; `<= file_size − 0x400` |
| `+0x018` | `u64` | `neff_version_major` | accepted `0..2` (§2) |
| `+0x020` | `u64` | `neff_version_minor` | **not** range-checked (informational) |
| `+0x028` | `u8[128]` | `neff_build_version` | ASCII label, never parsed by the loader |
| `+0x0AC` | `u8[32]` | `hash` | pkg1: 32-B SHA256; pkg2: 16-B MD5 (first 16) |
| `+0x220` | `u64` | `feature_bits` | the forward-compat mask (§3) |
| `+0x228` | `u32` | `vnc_size` | virtual-NeuronCore size (1=single, 2=dual-fuse) |
| `+0x400` | `u8[]` | `data` | inner archive payload |

> **NOTE.** `neff_version_minor` (`+0x20`) is accepted at **any** value — there is no
> minor-version gate. Forward minor compatibility is delegated entirely to `feature_bits`
> (§3). `neff_build_version` (`+0x28`, e.g. `"2.0.NNNNN.0%kaena-tools/…@<sha>"`) is a human
> label and is never read by the loader.

---

## 2. Tier 1 — the `neff_version_major` ceiling  `[HIGH × OBSERVED]`

`neff_parse` reads `neff_version_major` from `+0x18` and rejects anything above 2. The
raw disassembly grounds the field offset and constant:

```asm
4ca538:  mov  0x18(%rax),%rax          ; neff_version_major  (+0x18)
4ca53c:  cmp  $0x2,%rax                ; hard ceiling at major 2
```

```c
// neff_parse @0x4ca3f0, lines 170–175
uint64_t major = header->neff_version_major;   // +0x18
if (major > 2) {
    nlog("NEFF version: %lu.%lu is not supported, accepted version range: %u.x-%u.x",
         major, header->neff_version_minor, /*lo=*/0, /*hi=*/2);
    return NRT_UNSUPPORTED_NEFF_VERSION;        // code 10
}
```

Accepted majors are `{0, 1, 2}`. Majors 0 and 1 are the pre-`feature_bits` container
formats (a back-compat ceiling); the shipped fixture is major 2. The `feature_bits`
trapdoor (§3) is armed **only** for major 2.

---

## 3. Tier 1 — the `feature_bits` forward-compat trapdoor  `[HIGH × OBSERVED]`

When (and only when) `neff_version_major == 2`, `neff_parse` masks `feature_bits`
(`+0x220`) against a 36-bit trapdoor mask. The disassembly:

```asm
4ca790:  movabs $0x7ffffffff8000000,%rax   ; trapdoor mask = bits 27..62
4ca79a:  and    0x220(%r15),%rax           ; & feature_bits (+0x220)
4ca7a7:  push   $0x7ffffff                  ; supported = bits 0..26  (printf arg)
```

```c
// neff_parse @0x4ca3f0, lines 176–192
if (major == 2 && (header->feature_bits & 0x7FFFFFFFF8000000ULL) != 0) {
    nlog("This NEFF (version: %lu.%lu, features: 0x%lx) has been compiled by a newer "
         "version of Neuron compiler. Features supported by this Neuron Runtime: 0x%lx. "
         "Please update the aws-neuronx-runtime-lib package to the latest version.",   // @0x82d3b0
         /*LITERAL*/ 2, header->neff_version_minor,
         header->feature_bits & 0x7FFFFFFFF8000000ULL,   // the *masked* (offending) bits
         /*supported=*/ 0x7FFFFFF);
    return NRT_UNSUPPORTED_NEFF_VERSION;     // code 10
}
```

The bit arithmetic, verified exactly:

| Quantity | Value | Bits | Count |
|:---|:---|:---|:---|
| trapdoor mask | `0x7FFFFFFFF8000000` | **27..62** | 36 |
| supported space | `0x7FFFFFF` | **0..26** | 27 |
| `mask & supported` | `0` | — | (disjoint) |
| `mask \| supported` | `0x7FFFFFFFFFFFFFFF` | 0..62 | (clean partition) |
| bit 63 | — | in **neither** mask | never tested |

**Semantics.** `feature_bits` is a forward-compatibility capability bitmap, *not* a
version number. Bits **0..26** are the feature space this runtime knows how to honor
(the printf prints `0x7FFFFFF` as "supported"). If a *newer* compiler sets any bit in
**27..62**, the runtime cannot interpret that feature and refuses to load — a **clean
refusal** ("update the runtime") instead of silently mis-executing an unknown feature.
This is the "trapdoor": the compiler opts a forward feature in by setting a high bit, and
**every shipped runtime that predates that feature deterministically rejects**, whereas
an unbounded version number would let an old runtime silently accept. It converts
forward-compat from a version inequality into a capability mask.

> **GOTCHA — the literal `2`.** The reject printf passes the *literal* `2` for the
> version (not the variable `major`), because the branch is reachable only when
> `major == 2`. A major-0 or major-1 NEFF bypasses the `feature_bits` test entirely —
> those container formats predate the bitmap, so `feature_bits` is don't-care for them.

> **NOTE — vs. [container byte format §4](container-byte-format.md#4-the-feature_bits-forward-compat-trapdoor--high--observed).**
> The decompiled `features: 0x%lx` argument is the **masked** value
> (`feature_bits & 0x7FFFFFFFF8000000`), i.e. only the *offending* high bits are printed,
> not the whole `feature_bits`. The shipped fixture's `feature_bits == 0` ⇒ vanilla NEFF,
> no optional features, trapdoor never trips.

There is **no override** for the trapdoor — it is a hard interop barrier. The per-bit
names within `0..26` are producer-side (compiler) and are not present in the runtime.

---

## 4. Tier 1 — `pkg_version`: archive format + integrity gate  `[HIGH × OBSERVED]`

After the version gates, `neff_parse` checks the data-size fit, then dispatches on
`pkg_version` (`+0x0`) to select the inner-archive format and integrity hash. `a3` is the
verify flag passed by `nrt_load` (only verify when set).

```c
// neff_parse @0x4ca3f0, lines 193–240
data_size = header->data_size;                  // +0x10
if (neff_size - 1024 < data_size) {             // inner archive must fit
    nlog("Invalid NEFF data size(%lx vs %lx)", data_size, neff_size);
    return NRT_INVALID;                          // code 2
}

if (header->pkg_version == 1) {                  // LEGACY: raw GNU ustar tar + SHA256
    data = header->data;
    if (a3 /*verify*/) {
        if (data_size == 0)
            __assert_fail("len > 0", "/opt/workspace/KaenaRuntime/kelf/neff.cpp", 0x34,
                          "void sha256(const uint8_t*, size_t, uint8_t*)");
        sha256_init/update(data, data_size)/final → 32-B digest;     // all 32 B of hash[]
        if (digest != header->hash[0..31]) { nlog("SHA256 mismatch!"); return NRT_INVALID; }
    }
} else if (header->pkg_version == 2) {           // CURRENT: gzip-compressed tar + MD5
    data = header->data;
    if (a3 /*verify*/) {
        if (data_size == 0)
            __assert_fail("len > 0", "/opt/workspace/KaenaRuntime/kelf/neff.cpp", 0x40,
                          "void md5(const uint8_t*, size_t, uint8_t*)");
        MD5_Init/Update(data, data_size)/Final → 16-B digest;        // first 16 B of hash[]
        if (digest != header->hash[0..15]) { nlog("MD5 mismatch!"); return NRT_INVALID; }
    }
} else {
    nlog("Unsupported NEFF packager: %lu", header->pkg_version);     // @0x82d550
    return NRT_UNSUPPORTED_NEFF_VERSION;          // code 10
}

// both paths converge on one tar walk:
a = archive_read_new();
archive_read_support_format_tar(a);
archive_read_support_filter_gzip(a);              // registered for BOTH; inert for pkg1
archive_read_open_memory(a, data, data_size);     // in-memory; no temp files
while (archive_read_next_header(a, &entry) == 0)  // insert {pathname → (buf,size)} into neff_t::files
    …
```

The archive-format gate, byte-exact:

| property | `pkg_version 1` (legacy) | `pkg_version 2` (current) |
|:---|:---|:---|
| inner archive | raw **GNU ustar** tar | gzip-compressed **GNU ustar** tar |
| integrity hash | SHA-256 (32 B, all of `hash[]`) | MD5 (16 B, first 16 of `hash[]`) |
| `__assert_fail` line | `neff.cpp:0x34` (`sha256`) | `neff.cpp:0x40` (`md5`) |
| gzip filter | registered but inert | decompresses the stream |
| unsupported value | → `NRT_UNSUPPORTED_NEFF_VERSION` (10), `"Unsupported NEFF packager"` | |

> **CORRECTION (vs an earlier "POSIX-pax tar" label; per [concrete-carve](concrete-carve.md) §3,
> [format-reference](format-reference.md) §1.2, [container-capstone](container-capstone.md) §1.2).**
> The carved fixture's inner tar is **GNU `ustar`**, not POSIX-pax: member-0 magic `ustar ` +
> version ` \0` at `+0x100`/`+0x106`, GNU **base-256** `uid`/`gid` (high bit `0x80`), and **zero**
> PAX/GNU-longname extension records (no `typeflag 'x'/'g'/'L'/'K'`). The format gate itself does
> not test the tar dialect — `archive_read_support_format_tar` accepts GNU `ustar` transparently —
> so this is a producer-side fact, not a version gate; a writer emits GNU `ustar`. `[HIGH × OBSERVED]`

Key points:

- The gzip filter is registered **unconditionally** for both; pkg1's raw tar simply has
  no gzip member, so the filter is a no-op. The archive-walk code path is identical.
- The hash is **unkeyed** — integrity only, **not** authentication. A modified NEFF with
  a recomputed hash passes.
- Verification is **gated on `a3`**: when `a3 == 0` the hash is skipped entirely and the
  loader trusts the bytes. Integrity is optional at the loader seam.
- Per-member `…checksum` side-files (entries whose pathname ends in an 18-byte suffix,
  `memcmp` against `&byte_84987E-18`) are **skipped** during the tar walk
  (`neff_parse` line ~392). They are producer-side per-file checksums the loader ignores.

---

## 5. Tier 2 — the `kelf-a.json` architecture-target gate  `[HIGH × OBSERVED]`

Once the container is accepted and the tar unpacked, `kelf::kelf::load` @`0x497dc0` parses
`kelf-a.json` and runs the arch gate. The arch enum is `al_hal_tpb_arch_type` (IDA ordinal
**5522**): `INVALID = 0`, `INVALID_1 = 1`, **`SUNDA = 2`**, **`CAYMAN = 3`**,
**`MARIANA = 4`**, `NUM = 5`.

### 5.1 `kelf::parse_version` @`0x4970f0` — parse-only, no gate

```c
// kelf::kelf::parse_version @0x4970f0
//   "version" e.g. "0.5": split at first '.', strtol each as uint16
if (version.find('.') == npos)
    return 2;                                    // "invalid string (.) in %s"
this->major_version = (uint16_t)strtol(before_dot);   // the kelf-version field
this->minor_version = (uint16_t)strtol(after_dot);
nlog("KELF version: %u.%u-%s", major_version, minor_version, trailing_label);  // DEBUG
return NRT_SUCCESS;                              // value is recorded, never gated
```

This records the kelf-graph schema version for the loader's own dispatch; it is **not** a
compat barrier. It rejects only on a missing `'.'`. The shipped fixture's `kelf-a.json`
carries `version: "0.5"` and is logged, not rejected.

### 5.2 `kelf::parse_target` @`0x497c10` — string → arch ordinal

The `target` string maps to an `al_hal_tpb_arch_type`, stored at **`kelf + 184`** (the
field the §5.3 gate reads). Constants are decoded from the disassembly's compare
immediates:

```c
// kelf::kelf::parse_target @0x497c10
switch (target) {
  case "sunda":  // len 5: *(u32)=0x6473_6E75 ('sund'=1684960627) && [4]=='a'(97)
  case "v2":     // len 2: *(u16)=0x3276 ('v2'=12918)
      this->target = SUNDA;   break;            // 2
  case "cayman": // len 6: *(u32)=0x6D79_6163 ('caym'=1836671331) && *(u16+2)=0x6E61 ('an'=28257)
  case "v3":     // len 2: *(u16)=0x3376 ('v3'=13174)
      this->target = CAYMAN;  break;            // 3
  case "mariana":
  case "v4":
      this->target = MARIANA; break;            // 4
  case "*":
      this->target = al_hal_tpb_get_arch_type(); break;   // CURRENT instance arch
  case "":       // empty
      this->target = SUNDA;   break;            // 2 (default fall-through)
  default:
      nlog("Invalid target '%s'", target);
      return NRT_INVALID;                        // code 2
}
nlog("Target found '%s', id=%u", target, this->target);   // DEBUG
return NRT_SUCCESS;
```

> **NOTE.** The shipped fixture's `kelf-a.json` carries `target: "*"` — the
> architecture-neutral form. Because `"*"` copies the live instance arch into
> `kelf->target`, it always matches in §5.3 and never trips the skew gate. (The
> `arch_type` ordinal here — `2/3/4` — is the software/HAL scale, **not** the hardware
> `arch_id` codename byte `0x05`/`0x0c`/`0x14`/`0x1c` used elsewhere in the wiki; do not
> conflate the two scales — see the
> [container §5 GOTCHA](container-byte-format.md#5-section--parser-map--kelf_load_from_neff-0x4c0870--high--observed).)

### 5.3 The arch-skew compat gate — `kelf::load` @`0x497dc0` (lines 421–449)

```c
// kelf::kelf::load @0x497dc0, after parse_version + parse_target
if (kelf->target /*+184*/ == al_hal_tpb_get_arch_type())   // arch matches live instance
    goto load_graphs;                                       // OK

if (!nrt_gconf()->allow_legacy_neff) {                      // strict mode (default)
    nlog("Loading a NEFF compiled for a different arch is not allowed, "
         "NEFF arch: v%u, instance arch: v%u", kelf->target, al_hal_tpb_get_arch_type());  // @0x82b5a0
    nlog("Please ensure that --target <correct_arch> is passed as a neuronx-cc command line flag.");
    nlog("To override, set NEURON_RT_ALLOW_LEGACY_NEFF=1 and restart.");
    nlog_set_error_cause(NEFF_ARCH_INCOMPAT);               // NLOG_ERR_CAUSE::NEFF_ARCH_INCOMPAT
    return /*reject*/ 2;
}
// legacy mode: WARN and proceed anyway
nlog_WARN("Loading a NEFF compiled for a different arch, NEFF arch: v%u, instance arch: v%u");
load_graphs();
```

This is the compiler↔runtime **arch-skew handler**. A NEFF compiled with
`--target sunda/cayman/mariana` (or `"*"`) is checked against the actual silicon
generation at load. A mismatch is a **hard reject** (error cause `NEFF_ARCH_INCOMPAT`)
**unless** the operator sets `NEURON_RT_ALLOW_LEGACY_NEFF=1`, which downgrades it to a
warning. Target `"*"` always matches (it copies the instance arch), so
architecture-neutral NEFFs never trip this gate.

> **QUIRK — the field at `kelf + 184`.** `parse_target` writes the resolved arch ordinal
> into `kelf->target` (`+0xB8 = 184`), and the skew gate reads the *same* offset
> (`*(u32)(a1+184)`). The compare is against the *runtime-resolved* live arch
> (`al_hal_tpb_get_arch_type()`), not against a header byte — the silicon decides the
> reference, the NEFF only declares.

---

## 6. Tier 3 — the GPSIMD ucode-lib semver gate `"1.21.1.0"`  `[HIGH × OBSERVED]`

`parse_one_ucode_lib` @`0x4b1610` decodes each GPSIMD custom-op library element and is the
per-section version gate. It reads two keys from the ucode-lib JSON element: `"library"`
(the `.bin` filename, → `load_bin_file`) and the **version** key.

> **CORRECTION — the version key is `"ulib_to_ucode_version"`, not `"version"`.** The key
> is built at runtime: `_mm_load_si128(&xmmword_8551E0)` loads the 16-byte pooled string
> `"ulib_to_ucode_ve"` into a buffer, then `*(u64)(buf+13) = 0x6E6F69737265765F`
> (`"_version"`) overwrites from byte 13, yielding the 21-byte key `"ulib_to_ucode_version"`
> (confirmed whole in `.rodata` via `strings`). The `at_key` look-up uses this exact
> 21-char key; a missing key emits `"Missing GPSIMD lib version!"` → code 2. (Earlier
> reports abbreviated this to `"version"`.)

### 6.1 The semver compare (lines 194–231)

```c
// parse_one_ucode_lib @0x4b1610
char nrt_ver[] = "1.21.1.0";                     // strcpy of literal; .rodata 0x84fe68

// split both version strings at the FIRST '.':
//   parse_version_string @0x4ade60 → major = substring BEFORE '.', rest = AFTER '.'
//   returns 2 ("invalid string version string (missing '.')") if no '.'
v22 = parse_version_string(lib_version, &lib_major, &lib_rest);   // lib's "ulib_to_ucode_version"
if (v22) nlog_WARN("GPSIMD lib: Failed to parse GPSIMD lib version string! Version: %s", lib_version);

v59 = parse_version_string("1.21.1.0", &nrt_major, &nrt_rest);    // "1.21.1.0" → "1", "21.1.0"
if (v59) nlog_WARN("GPSIMD lib: Failed to parse NRT version string! Version: %s", nrt_ver);

int nrt_maj = stoi(nrt_major);          // = stoi("1") = 1
int lib_maj = stoi(lib_major);          // lib's major
bool majors_equal = (nrt_maj == lib_maj);
int mismatch = majors_equal ? v59 : 1;  // 1 = ALWAYS mismatch if majors differ

if (mismatch) {
    nlog("Mismatch between GPSIMD lib version and NRT version!");   // @0x82c240
    nlog("invalid GPSIMD lib");
    return NRT_INVALID;                  // code 2
}
```

The runtime's ucode ABI version is the **hardcoded constant `"1.21.1.0"`** — byte-exact at
`.rodata` `0x84fe68` (`31 2e 32 31 2e 31 2e 30` = `"1.21.1.0"`, NUL-padded). The gate is a
**MAJOR-version equality** check: a library passes only if its major equals **1**. Minor
and patch fields are split off (`lib_rest`/`nrt_rest`) but **not** compared in the
equality — only the major substrings feed `stoi`. A library compiled against a future
major (`2.x`) is refused at load: the device-side analog of the Tier-1 `feature_bits`
trapdoor, but for the ucode ABI. **Not overridable.** This `"1.21.1.0"` constant relates
to the [Part-8 nrtucode version getters](../runtime/version-extisa-getters.md).

> **NOTE — parse-failure asymmetry.** A failed parse on *either* string only **warns**;
> but a malformed lib version still feeds the equality, so `lib_maj` derived from garbage
> typically `!= 1` ⇒ mismatch ⇒ reject. The NRT side `"1.21.1.0"` always parses, so
> `nrt_maj == 1` is invariant.

### 6.2 The `cpu_id == 0` assert (lines 339–340)

```c
// cpu_id ("cpu_id" key, u64 narrowed to u8) MUST be 0 on this base path
if (cpu_id != 0)
    __assert_fail("cpu_id == 0", "/opt/workspace/KaenaRuntime/kelf/kelf2kbin.cpp", 0x691,
                  "NRT_STATUS parse_one_ucode_lib(const neff_t*, mla_resources&, "
                  "const std::string&, simdjson::dom::element&)");
```

A NEFF-supplied ucode library is **always authored against cpu 0**; the runtime's
`ucode_stage_libs` replicates it across the Q7 cores (the per-core fan-out is a *staging*
concern, not a NEFF field). A non-zero `cpu_id` is a malformed/incompatible library and
aborts. The producer file path (`kelf2kbin.cpp:0x691`) leaks through the assert.

### 6.3 `total_cpus`, `opcode`, duplicate-name (lines 264–355)

```c
// total_cpus ("total_cpus" key, u8): accepted set is {0, 1, 8}
if (total_cpus > 1 && total_cpus != 8) {
    nlog("UCode Library %s has invalid number of total cpus %u", name, total_cpus);  // @0x82c310
    return /*reject*/ 2;
}
// 1 = a single Vision-Q7 core, 8 = the full 8-core ncore2gp GPSIMD cluster, 0 = unset

// opcode ("opcode" key, i64): the value -123 (== 0x85 narrowed) marks the baked default lib
int flags = (opcode == -123) ? 0 : 6;     // 0 = built-in/registered op; 6 = user ExtISA op

// duplicate library name → reject
if (already_added(name)) {
    nlog("UCode Library %s has already been added", name);
    return /*reject*/ 2;
}
```

`opcode 0x85` (`-123`) distinguishes the baked default library (`libnrtucode_extisa.so`,
`flags = 0`) from a NEFF-supplied user custom op (`flags = 6`).

All four parsed fields write into the runtime `ucode_lib` record (size **80**, from
`*_structures.json`):

| Off | Member | Source |
|:---|:---|:---|
| `+0x00` | `name` (`std::string`, 32 B) | the `"library"` key |
| `+0x20` | `content` (`void *`) | the loaded `.bin` |
| `+0x28` | `content_size` (`u64`) | |
| `+0x30` | `flags` (`u32`) | opcode → `{0, 6}` |
| `+0x34` | `cpu_id` (`u8`) | the `"cpu_id"` key (asserted 0) |
| `+0x35` | `total_cpus` (`u8`) | the `"total_cpus"` key (`{0,1,8}`) |
| `+0x38` | `functions` (`std::vector`, 24 B) | per-function entries |

### 6.4 Downstream staging caps `[HIGH × CARRIED]`

After parse, `ucode_stage_libs(_table)` stages the libs into the per-arch baked table and
enforces capacity caps (strings present in `.rodata`):

- global table cap: `"too many ucode functions in this table.  Max is %d.  Add support
  for mid-neff reload?"` (`@0x81a1d0`);
- per-function name: `"ucode function name (%s) is too long.  Max length = %d"`;
- per-core fn cap: `"Number of ucode functions: %lu per core exceeded the maximum
  allowed %u"` (`@0x81a260`).

These are staging-time capacity gates that complement the parse-time semver gate (the
numeric limits — 97/64/28 — are passed as runtime args). See
[seq-microcode](seq-microcode.md) for the function-table layout.

---

## 7. Tier 3 — the EVTACCEL SB-carveout value gate  `[HIGH × OBSERVED]`

`runtime_statebuffer_reservation[]` (in `def.json`) parses into `kbin_sb_carveout_t`
entries (size **40**, ordinal verified): `+0x00 type` (`kbin_sb_carveout_type_t`:
`INVALID = 0`, `EVTACCEL = 1`), `+0x08 offset`, `+0x10 size`, `+0x18 start_partition`,
`+0x20 num_partitions`. The compat enforcement lives in `kbl_model_add` @`0x3058e0`
(`kbl_check_evt_accel_region`), **gated on `arch_type == 3` (CAYMAN) only**.

```c
// kbl_model_add @0x3058e0, lines 125–181 (kbl_check_evt_accel_region)
if (al_hal_tpb_get_arch_type() != 3 /*CAYMAN*/)
    goto skip_evtaccel;                          // SUNDA/MARIANA take the no-EVTACCEL path

model->flag[7584] = 1;  model->flag[7218] = 0;   // default: EVTACCEL not enabled
if (kbin->sb_carveouts && kbin->sb_carveouts->count) {
    for (carveout c : kbin->sb_carveouts->carveouts) {     // scan for EVTACCEL(1)
        if (c.type != KBIN_SB_CARVEOUT_TYPE_EVTACCEL) continue;

        if (c.start_partition /*+0x18*/ != 0)
            FAIL("Event Accel carveout SB partition must start at 0. Got %lu", c.start_partition);  // @0x817570
        if (c.num_partitions  /*+0x20*/ != 128)
            FAIL("Event Accel carveout SB partition count must be %u. Got %lu", 128, c.num_partitions);
        if (c.offset          /*+0x08*/ != FIXED /*&stru_37FF8*/)
            FAIL("Event Accel carveout SB partition offset must start at 0x%x. Got 0x%lx", FIXED, c.offset);
        if (c.size            /*+0x10*/ != 8)
            FAIL("Event Accel carveout SB size must be 8. Got %lu", c.size);

        model->flag[7584] = 0;  model->flag[7218] = 1;     // all-pass: EVTACCEL enabled
        goto skip_evtaccel;
    }
    // FAIL: → nlog("Failed to verify Event Accel State Buffer carveout"); return NRT_INVALID;
}
// no EVTACCEL carveout present:
nlog_DEBUG("model: %s on: nd%d:nc%d has no evtaccel reservation on SBUF", ...);   // ALLOWED
skip_evtaccel: ;
```

The EVTACCEL carveout is a **fixed-shape contract**: exactly
`{start_partition 0, num_partitions 128, offset FIXED, size 8}`. The compiler must emit
precisely these values or the model is rejected — a strict **value-level** compat check,
not a range. Only CAYMAN (`arch_type == 3`) enforces it; SUNDA/MARIANA take the
no-EVTACCEL path. An absent reservation is allowed; an off-contract one is rejected. This
reserves the Event-Accelerator State-Buffer region the CC/EFA collectives subsystem needs.

> **NOTE — the sibling legacy-SB guard.** A separate guard fires when
> `runtime_statebuffer_reservation` is *missing/corrupt* on an arch that requires it,
> unless `NEURON_RT_ALLOW_LEGACY_NEFF=1` (string `"Missing or corrupted
> runtime_statebuffer_reservation field in def.json … Potentially a legacy neff without
> proper compiler support"`). It shares the env var with the Tier-2 arch gate — both
> express "this NEFF predates the current compiler contract". The shipped fixture's
> `runtime_statebuffer_reservation` is `[]` (target `"*"`, no EVTACCEL), so neither path
> fires.

---

## 8. Tier 3 — typed DMA-ring NEFF-section parsing  `[HIGH × OBSERVED]`

`parse_one_dma_ring` @`0x4b5f80` decodes each named `dma_queue` in `def.json`. The ring is
typed by a string switch into `kbin_dma_ring_type_t` (ordinal **6028**): `ENG = 0`,
`H2T = 1`, `H2T_SERVICE = 2`, `H2T_COPY = 3`, `COLLECTIVES = 4`, `MODEL = 5`, `DATA = 6`,
`IN = 7`, `OUT = 8`, `INDIRECT_MEMCPY = 9`, `ACT_TBL = 10`, `DYNAMIC_ACT_TBL = 11`,
`DVE_TBL = 12`, `IOQ_SWITCHER = 13`, `TOPSP_INIT = 14`, `EMBEDDING_UPDATE = 15`,
**`CUSTOM_OP = 16`**, `GENERIC = 17`, `LAST = 18`.

```c
// parse_one_dma_ring @0x4b5f80
owner = al_hal_tpb_get_tpb_eng_type_from_str(json["owner"]);   // PE/ACT/POOL/DVE/SP
if (owner == 5)                                                // == invalid sentinel
    REJECT("Invalid owner '%s' for DMA queue %s", owner_str, queue_name);   // line 180

if (json missing "type")
    REJECT("missing <type> in %s", queue_name);                // line 488

if (al_hal_tpb_get_arch_type() == 2 /*SUNDA*/) { …legacy dynamic-DMA sub-path… }  // line 522

switch (type) {                                                // dma_info::add_dma_queue(a1,a2,RING_TYPE,owner,…)
  case "in":   add_dma_queue(…, 7,  …);  break;   // *(u16)==0x6E69 ('in'); IN(7)
  case "out":  add_dma_queue(…, 8,  …);  break;   // OUT(8)
  case "data": add_dma_queue(…, json["indirect_memcpy"] ? 9 : 6, …);  // INDIRECT_MEMCPY(9) / DATA(6)
               break;
  case "act_load":         add_dma_queue(…, 11, …);  break;   // DYNAMIC_ACT_TBL(11)
  case "embedding_update": add_dma_queue(…, 15, …);  break;   // EMBEDDING_UPDATE(15)
  default: REJECT;
}
// input/output/embedding queues reject queue-set instances:
//   "Invalid Neff: Queue set instances are not supported for {input|output|embedding update} queues (%s)"
```

> **GOTCHA — `CUSTOM_OP(16)` is not produced by this string switch.** The GPSIMD custom-op
> transfer ring is plumbed by the ucode custom-op path (the EMBEDDING_UPDATE /
> LOAD_POOL_ARGUMENT pseudo-lowering + ucode dispatch), not by a `"type"` arm here. The
> materialized ring is `kbin_dma_ring_instance_t` (264 B: `name[256]` `+0`, `ndesc u32`
> `+256`, `desc[]` `+264`); the runtime twin `dma_ring_instance` is 56 B. See
> [relocation/weights](relocation-weights.md) for the descriptor expansion.

Each `def.json` section is parsed **schema-tolerantly** (simdjson `at_key`; a missing
*optional* key is silently skipped). The enforcing gates are the explicit ones above;
structural invariants (the dense-`var_id` `max+1 == map.size` check, `num_outputs > 0`)
fail *malformed* graphs, not version-skewed ones — see
[metaneff I/O ABI](metaneff-io-abi.md).

---

## 9. The complete version-gate table

| # | gate | function @ addr | field / source | accept condition | on-fail (code / cause) | override |
|:--|:--|:--|:--|:--|:--|:--|
| 1 | byte-count floor | `neff_get_header` @`0x4ca2c0` | `neff_size` | `neff_size > 0x3FF` | ret NULL → code 2 | none |
| 2 | container size | `neff_get_header` @`0x4ca2c0` | `header_size` `+0x8` | `header_size < neff_size` | ret NULL → code 2 | none |
| 3 | major ceiling | `neff_parse` @`0x4ca3f0` | `neff_version_major` `+0x18` | `major <= 2` | 10 `NRT_UNSUPPORTED_NEFF_VERSION` | none |
| 4 | feature trapdoor | `neff_parse` @`0x4ca3f0` | `feature_bits` `+0x220` | `major != 2` OR `(fb & 0x7FFFFFFFF8000000) == 0` | 10 "newer Neuron compiler" | none |
| 5 | data-size fit | `neff_parse` @`0x4ca3f0` | `data_size` `+0x10` | `neff_size − 1024 >= data_size` | 2 `NRT_INVALID` | none |
| 6 | pkg format | `neff_parse` @`0x4ca3f0` | `pkg_version` `+0x0` | `pkg_version ∈ {1,2}` | 10 "Unsupported NEFF packager" | none |
| 7 | integrity (pkg1) | `neff_parse` @`0x4ca3f0` | `hash[0..31]` vs SHA256 | `digest == hash` (when `a3`, `len>0`) | 2 "SHA256 mismatch!" | `a3=0` skips |
| 8 | integrity (pkg2) | `neff_parse` @`0x4ca3f0` | `hash[0..15]` vs MD5 | `digest == hash` (when `a3`, `len>0`) | 2 "MD5 mismatch!" | `a3=0` skips |
| 9 | kelf graph version | `kelf::parse_version` @`0x4970f0` | `kelf-a.json` `version` | split on `'.'` (parse-only) | 2 only if no `'.'` | n/a (informational) |
| 10 | arch target parse | `kelf::parse_target` @`0x497c10` | `kelf-a.json` `target` | string → `{SUNDA 2, CAYMAN 3, MARIANA 4, *}` | 2 "Invalid target" | n/a |
| 11 | arch-skew compat | `kelf::load` @`0x497dc0` | `kelf->target` `+184` vs instance | `kelf->target == al_hal_tpb_get_arch_type()` | reject, cause `NEFF_ARCH_INCOMPAT` | `ALLOW_LEGACY_NEFF=1` |
| 12 | ucode semver | `parse_one_ucode_lib` @`0x4b1610` | `ulib_to_ucode_version` vs `"1.21.1.0"` | `major(lib) == major(NRT) = 1` | 2 "Mismatch GPSIMD lib ver" | none |
| 13 | ucode `cpu_id` | `parse_one_ucode_lib` @`0x4b1610` | `cpu_id` | `cpu_id == 0` | assert `kelf2kbin.cpp:0x691` | none |
| 14 | ucode `total_cpus` | `parse_one_ucode_lib` @`0x4b1610` | `total_cpus` | `total_cpus ∈ {0,1,8}` | 2 "invalid number of total cpus" | none |
| 15 | ucode dup name | `parse_one_ucode_lib` @`0x4b1610` | lib name | name not already added | 2 "already been added" | none |
| 16 | ucode table caps | `ucode_stage_libs_tbl` | staged table | `≤ 97` fns, name `≤ 64`, `≤ 28`/core | "too many" / "too long" / "per core" | none |
| 17 | EVTACCEL carveout | `kbl_model_add` @`0x3058e0` | `sb_carveout` EVTACCEL | `start=0, size=8, offset=fixed, nparts=128` | 2 "verify Event Accel" | none (CAYMAN only) |
| 18 | legacy-SB guard | kbl/kelf path | `runtime_statebuffer_reservation` | present (on requiring arch) | reject "legacy neff" | `ALLOW_LEGACY_NEFF=1` |
| 19 | dma owner valid | `parse_one_dma_ring` @`0x4b5f80` | `owner` | `eng_type_from_str != 5` | "Invalid owner" | none |
| 20 | dma type valid | `parse_one_dma_ring` @`0x4b5f80` | `type` | `∈ {in,out,data,act_load,embedding_update}` | "missing <type>" | none |

---

## 10. Compiler↔runtime skew resolution

The complete skew-resolution policy, in load order:

1. **Container skew (Tier 1).** An older runtime + newer NEFF: `major > 2` → hard reject
   ("accepted version range: 0.x-2.x"); `major == 2` with a high feature bit → hard reject
   (`feature_bits` trapdoor, "update runtime"); unknown `pkg_version` → hard reject
   ("Unsupported NEFF packager"). Forward-incompat is **always** a clean refusal, never
   silent.
2. **Arch skew (Tier 2).** A NEFF for the wrong silicon generation: strict mode → hard
   reject (`NEFF_ARCH_INCOMPAT`, `--target` hint); `NEURON_RT_ALLOW_LEGACY_NEFF=1` → warn
   and proceed. Target `"*"` always matches. **Operator-overridable** (unlike container
   skew).
3. **Ucode-ABI skew (Tier 3).** A GPSIMD library for a different ucode major:
   `major(lib) != 1` → hard reject ("Mismatch between GPSIMD lib version and NRT
   version!"); `cpu_id != 0` / `total_cpus ∉ {1,8}` → assert/reject. **Not overridable.**
4. **SB / section skew (Tier 3).** EVTACCEL values off-contract → hard reject (CAYMAN
   only); missing reservation on a requiring arch → reject unless `allow_legacy_neff`.

**Precedence:** (1) before (2) before (3)/(4) — the container is validated first
(`neff_parse`), then the kelf graph is loaded (arch gate), then per-section objects
(ucode / SB / dma) are parsed and gated. The **override surface** is exactly one env var:
`NEURON_RT_ALLOW_LEGACY_NEFF=1` relaxes the Tier-2 arch gate and the legacy-SB guard to
warnings. There is **no override** for the Tier-1 `feature_bits` trapdoor or the ucode
semver gate — those are hard interop barriers.

---

## 11. Reimplementation notes

- **Error codes** (`NRT_STATUS`, ordinal verified): `NRT_INVALID = 2` (structural /
  data-size / ucode / EVTACCEL / dma failures), `NRT_UNSUPPORTED_NEFF_VERSION = 10`
  (major > 2, feature trapdoor, bad `pkg_version`). Error **cause**:
  `NLOG_ERR_CAUSE::NEFF_ARCH_INCOMPAT` (arch skew).
- The `metaneff` `ModelConfig` host execution-policy contract
  ([metaneff I/O ABI](metaneff-io-abi.md)) carries **no** version field and is **not**
  gated here — version/compat is purely a NEFF-container + kelf + ucode concern. The
  [relocation/weights](relocation-weights.md) and
  [assembly pipeline](assembly-pipeline.md) passes are version-agnostic; they consume
  whatever an already-gated NEFF supplies. The simdjson parse-state struct that drives
  every `at_key` look-up above is documented in
  [host/runtime struct layouts](../appendix/struct-host-runtime-layouts.md).
- **Constants to hardcode** (all OBSERVED): trapdoor mask `0x7FFFFFFFF8000000` (bits
  27..62); supported `0x7FFFFFF` (bits 0..26); ucode ABI `"1.21.1.0"` (`.rodata`
  `0x84fe68`, major 1); EVTACCEL `{start 0, 128 partitions, size 8, fixed offset}`; arch
  `SUNDA 2 / CAYMAN 3 / MARIANA 4`; assert sites `neff.cpp:0x34` (sha256) /
  `neff.cpp:0x40` (md5) / `kelf2kbin.cpp:0x691` (`cpu_id == 0`). v2–v4 are byte-grounded;
  v5/MAVERICK is header-observed only and absent from the runtime arch enum (`NUM = 5`).
