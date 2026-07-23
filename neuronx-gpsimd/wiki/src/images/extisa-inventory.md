# EXTISA Q7 SO-Blob Inventory + Blob→Getter Map

This page is the **foundational image-carving inventory for Part 6**: the complete
census of the embedded **EXTISA Q7 SO-blobs** — the compiled Cadence Tensilica
**Vision-Q7 NX "Cairo"** (`ncore2gp`) extended-ISA POOL kernel libraries that the
host runtime bakes into its accessor libraries and hands to the device at
instruction-block time. Two independent embedding mechanisms carry the same corpus:

* **`libnrtucode_internal.so`** (host accessor library) — **16 Xtensa ELFs** reachable
  through the `*_Q7_POOL_PERF_EXTISA_<n>_SO_get()` accessor functions (per-generation,
  per-engine-index), each surfaced as a `(pointer, size)` pair, plus the four
  **MAVERICK** images that live *only* here.
* **`libnrtucode_extisa.so`** (runtime container) — **13 flat sub-images**, laid out as
  4 generation blocks of `[JSON-manifest | ELF]` pairs.

All **29 carves** are `e_machine = 94` (`EM_XTENSA`, Tensilica Xtensa), `ELFCLASS32`,
little-endian. The deliverable here is exhaustive and reimplementation-grade: the
**blob→getter binding table** (which getter yields which ELF), the **JSON-decoy
verdict** (is the per-image JSON real metadata or an inert placeholder?), the **per-gen
EXTISA descriptor schema** `Q7_POOL_PERF_EXTISA_{0..3}`, and the **carve recipe** as
annotated pseudocode. Everything below is carved, hashed, and disassembled
from the shipped binaries with stock `binutils` on the x86-64 host side
and the native Cadence `xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) on the device side.
The page default is `[HIGH/OBSERVED]`; claims that depart from it carry an explicit tag.

> **NOTE — what "EXTISA" means here.** The GPSIMD POOL engine has a *base* opcode set
> (carved as the per-gen base IRAM/DRAM firmware getters; see
> [image-catalog-index.md](image-catalog-index.md)) and an *extended* set — the
> ext-ISA — delivered as separate compiled `.so` images selected by silicon
> generation. This page is **only** about the ext-ISA images; the base-POOL firmware
> regions that share the same container `.rodata` are explicitly out of scope (and are
> flagged below so the carve sublane does not mistake them for EXTISA blobs).

Cross-links: the host resolver that calls these getters lives at
[runtime/image-hwdecode-resolvers.md](../runtime/image-hwdecode-resolvers.md); the
opcode→funcVA table *inside* each carved SO is decoded byte-by-byte at
[firmware/pool/kernel-info-table.md](../firmware/pool/kernel-info-table.md); the
device-side library selection/upload path is at
[firmware/pool/external-lib-loader.md](../firmware/pool/external-lib-loader.md); the
per-engine profiling tables addressed by the *sibling* `hwdecode` enum are at
[prof-cam-table-formats.md](prof-cam-table-formats.md); the full accessor census is at
[image-catalog-index.md](image-catalog-index.md).

---

## 1. Targets, toolchain, identity

All offsets/sizes/sha256 below are anchored to these exact binaries.

| role | path (relative to `neuronx-gpsimd/`) | sha256 | conf |
|---|---|---|---|
| host accessor lib (16 getters + MAVERICK blobs) | `extracted/aws-neuronx-gpsimd-customop-lib_0.21.2.0_amd64/opt/aws/neuron/gpsimd/custom_op/c10/lib/libnrtucode_internal.so` | `b7c67e898a116454…0632fc329b` | HIGH/OBSERVED |
| runtime container (13 sub-images) | `../neuronx-runtime/extracted/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libnrtucode_extisa.so` | `dc00763dbdb27cb4…1c39159f` | HIGH/OBSERVED |
| static archive (4th view) | `…/custom_op/c10/lib/libnrtucode.a` | — | HIGH/OBSERVED |
| host front lib (carries 12 dummy JSONs) | `…/custom_op/c10/lib/libnrtucode.so` | `06d3f0b1630e3882…82ccce5` | MED/CARRIED |
| device disassembler | `extracted/nested/gpsimd_tools_tgz/tools/XtensaTools/bin/xtensa-elf-objdump` | `d43bd4fad891e695…65a0d0e` | HIGH/OBSERVED |

* `libnrtucode_internal.so`: ELF64 x86-64 DYN, **not stripped** (full symtab — every
  accessor and `.data` blob symbol named), `BuildID 9cbf78c6…e585fd`, 10,276,288 bytes.
  Its first `R` `LOAD` segment maps **file offset 0 → VA 0** (`readelf -l` confirmed),
  so `.rodata` file-offset equals VA — carve directly at the getter's blob VA.
* `libnrtucode_extisa.so`: ELF64 x86-64 DYN, **stripped**, `BuildID 7bb03bc4…ae5e44`,
  9,656,488 bytes. `.rodata` is identity-mapped; all 13 embedded blobs live in it.
* Disassembler identity: `GNU objdump (GNU Binutils)
  2.34.20200201  Xtensa Tools 14.09`; registry `XTENSA_SYSTEM=…/XtensaTools/config`,
  `XTENSA_CORE=ncore2gp` (registry file `ncore2gp-params`); core
  `ConfigName=Xm_ncore2gp`, `uarchName=Cairo`, `arch=Xtensa24`,
  `SWToolsRelease=RI-2022.9`, `TargetHWVersion=NX1.1.4`,
  `IsaMaxInstructionSize=32` (FLIX/VLIW up to 32-byte bundles).

> **GOTCHA — `.data` VMA delta is per-section.** For these carves the only thing that
> matters is `.rodata` (where every blob lives), which **is** identity-mapped
> (file-off == VA) in both libs. Do **not** generalise that to `.data`: in the
> `ncore2gp` config DLLs the `.data`/`.data.rel.ro` VMA−file-offset delta is `0x200000`,
> and in `internal.so` the writable `LOAD` runs file `0x9b6cd0` → VA `0x9b8cd0`
> (delta `0x2000`). The per-gen lib tables (`*_libs`) sit in that writable segment, so
> their *values* are recovered from **relocations**, not from a raw file-offset read.

---

## 2. The blob→getter binding table (exhaustive)

### 2a. Getter form — the 4-instruction `(ptr,size)` stub

Every EXTISA accessor in `internal.so` is the identical 4-instruction stub that returns
an image pointer and a size through two out-pointers (`%rdi` = `*out_ptr`,
`%rsi` = `*out_len`):

```asm
; CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_get  @ .text VA 0x9b3aa0
9b3aa0:  lea  -0x6c42c7(%rip),%rax   ; -> 0x2ef7e0  (CAYMAN_0 SO blob, .rodata)
9b3aa7:  mov  %rax,(%rdi)            ; *out_ptr = blob
9b3aaa:  movq $0xa260,(%rsi)         ; *out_len = ELF size  (0xa260)
9b3ab1:  ret

; CAYMAN_Q7_POOL_PERF_EXTISA_0_JSON_get  @ .text VA 0x9b3ac0
9b3ac0:  lea  -0x6ba087(%rip),%rax   ; -> 0x2f9a40  (the dummy-JSON stub)
9b3ac7:  mov  %rax,(%rdi)
9b3aca:  movq $0x20,(%rsi)           ; *out_len = 0x20  (HARDCODED for ALL 16 JSON_get)
9b3ad1:  ret
```

* `*_SO_get` returns `(ELF-ptr, ELF-size)`; `*_JSON_get` returns `(stub-ptr, 0x20)`. The
  `movq $0x20` is **identical in all 16 `JSON_get` accessors** — the host API only ever
  surfaces the 32-byte stub (decisive for the decoy verdict, §4).
* The `MAVERICK_0_SO_get` @ `0x9b5ba0` follows the same form with `movq $0x7fb0`.

### 2b. Symbol census — where the "16/13/29" come from

| symbol class (`nm libnrtucode_internal.so`) | count | conf |
|---|---|---|
| `*_EXTISA_<n>_SO_get` text functions (`t`) | **16** | HIGH/OBSERVED |
| `*_EXTISA_<n>_JSON_get` text functions (`t`) | **16** | HIGH/OBSERVED |
| `*_EXTISA_<n>_SO_get.data` `.rodata` blob symbols (`r`) | 16 | HIGH/OBSERVED |
| `SUNDA_Q7_POOL_RELEASE_EXTISA_0_{SO,JSON}_get` (weak-undef `w`) | 2 | HIGH/OBSERVED |

> **CORRECTION / count gotcha.** A naïve `nm libnrtucode_internal.so | rg -c 'SO_get'`
> returns **33**, not 16, because each accessor contributes *two* symbols — the text
> function (`t … _SO_get`) **and** its `.rodata` data symbol (`r … _SO_get.data`) — plus
> the one SUNDA weak-undef. The headline **16** is the count of **accessor functions**:
> `nm … | rg ' t .*EXTISA_[0-9]+_SO_get$' | wc -l` = 16. Always disambiguate the symbol
> *type* before quoting an EXTISA count.

So the host accessor surface is **16 SO getters + 16 JSON getters** (4 generations ×
4 engine-index libs) + **2 SUNDA weak-undef placeholders** with no body in this binary
(resolved only when the runtime container is present, §3).

### 2c. Full binding table — getter VA ⇄ blob VA/size ⇄ container offset

Each container blob is **byte-identical (by sha256)** to the same-generation `internal.so`
getter blob — both carves are therefore proven correct (§8). Engine-index ⇄ size mapping
is fixed: `0xa260 → idx0`, `0xf5c → idx1`, `0x1500 → idx2`, `0x6974 → idx3` (the
container stores them in **reverse** order idx3,2,1,0).

| gen/idx | `SO_get` (.text VA) | SO blob VA (internal) | size | `JSON_get` (.text VA) | JSON stub VA | container ELF foff | entry | sha256\[:16\] |
|---|---|---|---|---|---|---|---|---|
| CAYMAN_0 | `0x9b3aa0` | `0x2ef7e0` | `0xa260` | `0x9b3ac0` | `0x2f9a40` | `0x5b0cc0` | `0x1005610` | `910d41c3ededce67` |
| CAYMAN_1 | `0x9b3ae0` | `0x2f9a60` | `0x0f5c` | `0x9b3b00` | `0x2fa9c0` | `0x5afd40` | `0x1000410` | `469c24137cc5fe08` |
| CAYMAN_2 | `0x9b3b20` | `0x2fa9e0` | `0x1500` | `0x9b3b40` | `0x2fbee0` | `0x5ae820` | `0x1000828` | `efd06876f5701b97` |
| CAYMAN_3 | `0x9b3b60` | `0x2fbf00` | `0x6974` | `0x9b3b80` | `0x302880` | `0x5a7e80` | `0x1003c74` | `052ac31c4e096212` |
| MARIANA_0 | `0x9b4720` | `0x5893c0` | `0xa260` | `0x9b4740` | `0x593620` | `0x1ce60` | `0x1005658` | `9f2ce049608c0a88` |
| MARIANA_1 | `0x9b4760` | `0x593640` | `0x0f5c` | `0x9b4780` | `0x5945a0` | `0x1bee0` | `0x1000410` | `3c2dea8c7359d1ea` |
| MARIANA_2 | `0x9b47a0` | `0x5945c0` | `0x1500` | `0x9b47c0` | `0x595ac0` | `0x1a9c0` | `0x1000828` | `e8564f9c0eb648b2` |
| MARIANA_3 | `0x9b47e0` | `0x595ae0` | `0x6974` | `0x9b4800` | `0x59c460` | `0x14020` | `0x1003c88` | `8477ff2690f30cc3` |
| MARIANA_PLUS_0 | `0x9b53a0` | `0x855240` | `0xa260` | `0x9b53c0` | `0x85f4a0` | `0x2ffee0` | `0x1005658` | `9f2ce049608c0a88` |
| MARIANA_PLUS_1 | `0x9b53e0` | `0x85f4c0` | `0x0f5c` | `0x9b5400` | `0x860420` | `0x2fef60` | `0x1000410` | `3c2dea8c7359d1ea` |
| MARIANA_PLUS_2 | `0x9b5420` | `0x860440` | `0x1500` | `0x9b5440` | `0x861940` | `0x2fda40` | `0x1000828` | `e8564f9c0eb648b2` |
| MARIANA_PLUS_3 | `0x9b5460` | `0x861960` | `0x6974` | `0x9b5480` | `0x8682e0` | `0x2f70a0` | `0x1003c88` | `8477ff2690f30cc3` |
| MAVERICK_0 | `0x9b5ba0` | `0x994de0` | `0x7fb0` | `0x9b5bc0` | `0x99cd90` | *(internal-only)* | `0x594c` | `a92c8ba0e9dfb2d8` |
| MAVERICK_1 | `0x9b5be0` | `0x99cdb0` | `0x0c64` | `0x9b5c00` | `0x99da20` | *(internal-only)* | `0x39c` | `f6a232efc3c1c9c8` |
| MAVERICK_2 | `0x9b5c20` | `0x99da40` | `0x1280` | `0x9b5c40` | `0x99ecc0` | *(internal-only)* | `0x900` | `a86e957daf2a658f` |
| MAVERICK_3 | `0x9b5c60` | `0x99ece0` | `0x55a0` | `0x9b5c80` | `0x9a4280` | *(internal-only)* | `0x3ccc` | `e6a63e8c72164ae6` |
| SUNDA_0 | *(weak-undef)* | — | `0xd308` | *(weak-undef)* | — | `0x921660` | `0x10000c8` | `444497066f5e1738` |

* **MAVERICK** is `ET_DYN` (PIC) — its entry/funcVAs are `.text`-relative (`0x594c`, …),
  not the `0x010xxxxx` band the `ET_EXEC` images use. It exists **only** in
  `internal.so` (not in the container, not in `libnrtucode.a`).
* **SUNDA** is the inverse: present **only** in the container (`@0x921660`, `0xd308` —
  the single largest image), bound in `internal.so` as the two weak-undef symbols.

### 2d. Distinct-image accounting (16 + 13 carves, 13 *unique*)

The 16 internal getters + 13 container sub-images give **29 carves**, but by byte-identity
there are **13 distinct** ELF images: *[HIGH]*

* **MARIANA ≡ MARIANA_PLUS** for all 4 indices (same sha256 each) — the two generation
  labels ship the **same compiled ucode** (so MARIANA_PLUS adds **0** new images).
* **CAYMAN** is a distinct compile (different sha256, *same* opcode contract).
* **MAVERICK**'s 4 images are entirely distinct (newer toolchain, stripped, PIC).
* **SUNDA**'s single image is distinct (newest, largest).

Tally: CAYMAN{0,1,2,3}=4 + MARIANA{0,1,2,3}=4 (== MPLUS) + MAVERICK{0,1,2,3}=4 + SUNDA=1
= **13 unique** EXTISA Q7 kernel ELFs.

---

## 3. Per-gen descriptor schema: coretype → blob-set selection

The runtime does not address blobs by name — it selects the ext-ISA **library set** by
**coretype** (silicon generation), then by **lib-index** (0..3 = engine-index tier). Two
dispatcher functions in `internal.so` encode this, both disassembled below.

### 3a. `nrtucode_get_num_ext_isa_libs` @ `0x9b2c90` — how many libs per gen

```asm
9b2c90:  movq $0x0,(%rsi)            ; *out = 0
9b2c97:  mov  $0x1,%eax             ; default return = 1 (status)
9b2c9c:  cmp  $0x25,%edi            ; coretype > 37 ?
9b2c9f:  ja   .ret                  ;   yes -> status 1
9b2ca3:  movabs $0x2020202000,%rcx  ; bitmask
9b2cad:  bt   %rdx,%rcx             ; test bit[coretype]
9b2cb1:  jae  .check6               ;   clear -> fall through
9b2cb3:  mov  $0x4,%ecx ; *out = 4 ; return 0   ; bits {13,21,29,37} => 4 libs
.check6:
9b2cc3:  cmp  $0x6,%rdx ; je -> *out = 1, return 0   ; SUNDA => 1 lib
         ; else status 1
```

`0x2020202000` sets bits **{13, 21, 29, 37}** (verified by direct bit-scan). The
per-gen library count is therefore:

| gen | ext_isa coretype | libs | image identity | toolchain (`.comment`) |
|---|---|---|---|---|
| SUNDA | 6 | 1 | external link (container) | `XtensaTools-14.09 clang 10` |
| CAYMAN | 13 | 4 | own compile | `XtensaTools-14.09 clang 10` |
| MARIANA | 21 | 4 | ≡ MARIANA_PLUS | `XtensaTools-14.09 clang 10` |
| MARIANA_PLUS | 29 | 4 | ≡ MARIANA | `XtensaTools-14.09 clang 10` |
| MAVERICK | 37 | 4 | distinct, PIC, stripped | `XtensaTools-15.05 clang 15` |

> **NOTE — coretype spacing is `+8` per generation.** The ext-ISA coretypes
> `{6, 13, 21, 29, 37}` (one slot per generation, stride 8) is a strong "one row per
> silicon gen" signature. Binding those ids to specific Trainium/Inferentia parts is
> **INFERRED** (SUNDA≈NC-v2, CAYMAN=v3, MARIANA=v4, MARIANA_PLUS=v4+, MAVERICK=v5);
> the numeric coretype→part map is not resolvable inside these binaries. *[MED/INFERRED]*

### 3b. `nrtucode_get_ext_isa` / `_internal` — selecting one descriptor

The public wrapper forces lib-index 0; deeper libs are reachable only via the internal
entry with a non-zero `%rcx`:

```asm
; nrtucode_get_ext_isa @ 0x9b2c80
9b2c80:  xor %ecx,%ecx              ; lib_index = 0
9b2c82:  jmp 0x9b2b30               ; tail to nrtucode_get_ext_isa_internal
```

```c
// nrtucode_get_ext_isa_internal @ 0x9b2b30 (reconstructed)
int nrtucode_get_ext_isa_internal(uint32_t coretype, uint32_t flavor,
                                  void *out /*32-byte descriptor*/, uint64_t lib_index) {
    // flavor==0: getenv("NEURON_UCODE_FLAVOR") strcmp {debug,DEBUG,test,TEST};
    //   only the PERF flavor of EXTISA is embedded, so non-PERF falls to status 2.
    uint32_t idx = coretype - 6;
    if (idx > 0x1f) return 1;                         // coretype outside 6..37
    void **base = jump_table[idx];                    // 32-entry table @ VA 0x556c
    //   coretype 6 -> sunda_libs@0x9b8f80 (1 entry)
    //   coretype 13-> cayman_libs@0x9b8f90 (4)   coretype 21-> mariana_libs@0x9b8fd0 (4)
    //   coretype 29-> mariana_plus_libs@0x9b9010 (4)  coretype 37-> maverick_libs@0x9b9050 (4)
    //   all other  -> return 2 (not present)
    if (!base) return 2;
    void **entry = base + lib_index * 2;              // 16-byte stride = {SO_get, JSON_get}
    auto SO_get   = entry[0];
    auto JSON_get = entry[1];
    if (!SO_get || !JSON_get) return 3;               // missing accessor
    SO_get  (&out[0x00], &out[0x08]);                 // out{ so_ptr,  so_len  }
    JSON_get(&out[0x10], &out[0x18]);                 //    { json_ptr,json_len }
    return 0;
}
```

The **32-byte out-struct** is therefore
`{ void* so_ptr; size_t so_len; void* json_ptr; size_t json_len; }` — the substantive
part being the SO image; the JSON is a 32-byte sidecar (§4).

### 3c. The per-gen lib tables (recovered from relocations)

Each `*_libs` table is an array of 16-byte `{SO_get, JSON_get}` entries. Because the
tables sit in the writable segment, their pointer values are the **`R_X86_64_RELATIVE`
addends** (and `R_X86_64_64` for SUNDA), recovered with `readelf -rW`:

| table | VA | entry\[0\] `{SO_get, JSON_get}` | … last entry |
|---|---|---|---|
| `sunda_libs` | `0x9b8f80` | `{SUNDA…_SO_get, SUNDA…_JSON_get}` *(weak `R_X86_64_64`, addend 0)* | — (1 entry) |
| `cayman_libs` | `0x9b8f90` | `{0x9b3aa0, 0x9b3ac0}` | `{0x9b3b60, 0x9b3b80}` |
| `mariana_libs` | `0x9b8fd0` | `{0x9b4720, 0x9b4740}` | `{0x9b47e0, 0x9b4800}` |
| `mariana_plus_libs` | `0x9b9010` | `{0x9b53a0, 0x9b53c0}` | `{0x9b5460, 0x9b5480}` |
| `maverick_libs` | `0x9b9050` | `{0x9b5ba0, 0x9b5bc0}` | `{0x9b5c60, 0x9b5c80}` |

> **GOTCHA — SUNDA binds differently.** `sunda_libs` uses **`R_X86_64_64`** relocs
> against the *weak-external* symbols `SUNDA_Q7_POOL_RELEASE_EXTISA_0_{SO,JSON}_get`
> (addend 0), while the four embedded generations use **`R_X86_64_RELATIVE`** to local
> accessor bodies. SUNDA's `_get` functions have **no body in `internal.so`**; they are
> satisfied only when `libnrtucode_extisa.so` (which carries the SUNDA image + its real
> JSON manifest) is in the link. This is why SUNDA is the one generation whose blob is
> *absent* from `internal.so`'s `.rodata`.

A **separate** coretype enum drives `nrtucode_get_hwdecode_table` @ `0x9b2cd0`
(`hwdecode_table_list@0x9b9090`, stride `0x10`) — the per-(generation, engine)
`PROF_CAM`/`PROF_TABLE` accessors (`which` ∈ {0,1} → slot +0/+8). That enum is
`{7-10, 15-18, 23-26, 31-33}` and is **orthogonal** to the ext-ISA `{6,13,21,29,37}`
enum; it is documented at [prof-cam-table-formats.md](prof-cam-table-formats.md). Do not
conflate the two.

---

## 4. JSON-decoy verdict

**VERDICT: CONFIRMED DECOY for 12 of the 13 container sub-images; the 13th (SUNDA) is a
genuine build-time opcode manifest.** Four independent lines of evidence.

**(i) Content.** The 32 bytes immediately preceding each of the 12 non-SUNDA embedded
ELFs are byte-for-byte identical (single sha256 `ba7458eb462a9866…6212a7d`):

```
{"dummy_message": "hello world"}     ; exactly 0x20 bytes, no NUL
```

The SUNDA sub-image is instead preceded by a `0x6c0`-byte **valid JSON**
(sha256 `d282bd4f37e71d87…48e5ab82`) that `json.load` parses cleanly: top keys
`{library, ulib_to_ucode_version, functions}`, `library="all.stripped.so"`,
version `1.21.1.0`, **17 functions**.

**(ii) Accessor size.** Every one of the 16 `*_JSON_get` accessors hardcodes
`movq $0x20,(%rsi)` (§2a) — the host API only ever returns the 32-byte stub. The real SO
image is `0xa260`/`0xf5c`/… bytes; the JSON slot is always 32.

**(iii) No parser anywhere.** None of `libnrtucode_internal.so`,
`libnrtucode_extisa.so`, or `libnrtucode.so` contains any JSON parser — a
`rg -a 'nlohmann|rapidjson|json::parse|basic_json|parse_error'` over all three returns
**0 hits**. The dummy JSON is therefore **structurally inert**: nothing in these
libraries deserializes it.

**(iv) The real descriptor lives in the SO.** Opcode→function resolution is performed
against the SO image's **`kernel_info_table`** PROGBITS section (WA, 8-aligned, 8-byte
entries `{BE u32 key = opcode<<24 | spec<<16 ; LE u32 funcVA}`), *not* against any JSON.
For CAYMAN_0 the table is `@VA 0x02000380`, `0x88` bytes = **17 entries** (opcodes
`0x7e iota @0x01000080`, `0x7c/0x7d` cross-lane-reduce, `0x41` tensor_tensor, `0xf0`
specs 0..4 extended, `0x46` copy, `0x47` cast, …). See
[kernel-info-table.md](../firmware/pool/kernel-info-table.md) for the full byte-by-byte
decode.

> **CORRECTION — the SUNDA JSON is genuine, and provably a *subset* of the SO.** The
> SUNDA SO's `kernel_info_table` (`@VA 0x02000760`, `0x90` bytes = **18 entries**) carries
> opcodes `{65,67,68,70,71,73,103,104,116,121,122,124,125,126,146,184,187,231}`; the
> SUNDA JSON names **17** of those — exactly the set minus **opcode 68 (`0x44`)**, a
> scalar-arith variant the JSON does not list (and which shares `funcVA 0x0100a0d8` with
> op 67/`0x43`). So `JSON ⊆ SO`: the real JSON is a faithful, *slightly abridged*
> descriptor of the SO — confirming it is genuine metadata, not a decoy.

**SUNDA real-JSON opcode→function table** (re-extracted, `library "all.stripped.so"`,
`ulib_to_ucode_version 1.21.1.0`, 17 functions):

| fn | op | fn | op |
|---|---|---|---|
| `pool_tensor_tensor_arith_op` | 65 | `pool_tensor_scalar_addr` | 116 |
| `pool_tensor_scalar_arith_op` | 67 | `pool_embedding_update` | 121 |
| `pool_copy` | 70 | `pool_load_pool_argument` | 122 |
| `pool_cast` | 71 | `pool_cross_lane_reduce_arith` | 124 |
| `pool_memset` | 73 | `pool_cross_lane_reduce_bitvec` | 125 |
| `pool_pool_buffer_load` | 103 | `pool_iota` | 126 |
| `pool_gather` | 104 | `pool_tensor_scalar_affine_select` | 146 |
| `dma_memcopy` | 184 | `dma_memcopy_indirect` | 187 |
| `pool_indirect_copy` | 231 | | |

**Interpretation (intent).** The 12 dummy JSONs are best read as a **vestigial build
artifact**, not active anti-RE obfuscation: the packaging pipeline emits a `(JSON, SO)`
pair per ext-ISA library, but for the per-gen builds the JSON slot was stubbed with a
placeholder because the descriptor migrated into the SO's `kernel_info_table`, while the
legacy `all.stripped.so` (SUNDA) build still emits the real JSON. The **observable** fact
(inert 32-byte placeholder, never parsed) is HIGH; the **intent** attribution is *[MED/INFERRED]*.
Either way, for a reverse engineer the effect is identical: **the JSON is a decoy — read
the SO.**

JSON-blob inventory (all 13 container sub-images):

| sha256 | len | count | verdict |
|---|---|---|---|
| `ba7458eb462a98669a2bb5c07ce792dd…` | `0x20` | 12 | DUMMY (inert) |
| `d282bd4f37e71d87e9f522ba88cf7313…` | `0x6c0` | 1 | REAL (SUNDA manifest) |

> **NOTE — count cross-check.** `rg -a -c 'dummy_message'` returns **16** in
> `internal.so` (4 gens × 4 libs, *including* MAVERICK) but **12** in both the container
> and the front lib (CAYMAN + MARIANA + MARIANA_PLUS only — MAVERICK is internal-only and
> SUNDA carries the real JSON). The 12-vs-16 split is itself a consequence of MAVERICK
> living only in `internal.so`.

---

## 5. Embedding format + container geometry

**RAW, UNCOMPRESSED ELF.** No prelink/UCPL wrapper, no compression, no XOR/cipher. The
`\x7fELF` magic sits directly at each sub-image; the ELF32 header parses natively
(`e_ident`, `e_type`, `e_machine=94`, `e_shoff`/`e_shentsize`/`e_shnum` self-consistent)
and the shipped `xtensa-elf-objdump` reads sections without any unwrap step.
**ELF size = `e_shoff + e_shentsize·e_shnum`** (for CAYMAN_0:
`0x9ce8 + 0x28·35 = 0xa260` ✓).

**Container layout.** `libnrtucode_extisa.so` `.rodata` (file-off `0xb000`, VA==off)
holds **4 generation blocks**. A `\x7fELF` scan finds **14** magics — 1 host container
@0 plus the 13 embedded, at offsets:

```
host @0x0
MARIANA      : 0x14020 0x1a9c0 0x1bee0 0x1ce60     (block 1, ELFs idx3,2,1,0)
MARIANA_PLUS : 0x2f70a0 0x2fda40 0x2fef60 0x2ffee0 (block 2)
CAYMAN       : 0x5a7e80 0x5ae820 0x5afd40 0x5b0cc0 (block 3)
SUNDA        : 0x921660                             (block 4, single image)
```

Within each block the 4 POOL libs are packed in **reverse engine order**
(idx3,idx2,idx1,idx0 by size `0x6974`/`0x1500`/`0xf5c`/`0xa260`), each as
`[32B dummy JSON | ELF]`, inter-sub-image gap ≈ `0x20`.

> **GOTCHA — JSON-delimited, NOT sentinel-delimited.** There is **no
> `ffffffff00000000` sub-image delimiter** at block boundaries: a boundary is
> zero-padding, then the JSON manifest, then `\x7fELF`. (`ffffffff00000000` *does* occur
> ~220× in the file — 13 inside ELF bodies, ~207 in `.rodata` lookup tables — but none
> acts as a delimiter.) The large inter-block gaps (`0x2cffe0`/`0x29dd40`/`0x366740`
> bytes) are **not empty**: they carry the per-generation **base** POOL IRAM/DRAM/SRAM
> firmware (the non-EXTISA base getters of [image-catalog-index.md](image-catalog-index.md)).
> Do not mistake those base images for EXTISA blobs.

---

## 6. Q7-code disassembly proof (the blobs are real `ncore2gp` firmware)

The blobs are not opaque data — they disassemble as live **Vision-Q7 FLIX/VLIW GPSIMD**
code with the native `xtensa-elf-objdump --xtensa-core=ncore2gp`, confirming `ncore2gp`
(NOT scalar-LX). Extract `.text` (`xtensa-elf-objcopy -O binary --only-section=.text`)
and disassemble (`-D -b binary -m xtensa --adjust-vma=0x01000000`; objdump auto-detects
FLIX from the format bytes). For **CAYMAN_0**:

* `.comment` = `XtensaTools-14.09 clang version 10.0.1`; `.text @VA 0x01000000`
  (AX, align 32); `kernel_info_table @VA 0x02000380` (WA, 8-aligned).
* **45 `entry` prologues, 50 `retw`, 149 distinct `ivp_` mnemonics** — windowed ABI +
  Cadence Vision IVP TIE vector ISA. Representative bundles:

```asm
10000b8: { bgeui.w15 a5,16,..; ivp_lsrn_2x32_xp v16,a3,a12;
           ivp_dmulq2n8xr8 wv1,wv1,v25,v10,v0,v11,pr3;
           ivp_dselnx16t v0,v2,v26,v9,v9,vb0 }
10000d0: { bgeui.w15 a2,0x10000,..; ivp_labvdcmprs2nx8_xp vb0,v0,u0,a0,a9,1;
           ivp_subn_2xf32t v0,v0,v16,vb6; ivp_counteqm4nx8 v8,a2,v1,v1,vb6,vb8 }
```

* **MAVERICK_0** (`ET_DYN`, clang-15, stripped): `.comment`
  `XtensaTools-15.05 clang version 15.0.7`; decodes to real FLIX (143 bundles, 446 IVP
  ops, 51 `entry`). No `.symtab`/`.xt.prop` (stripped) — only its `kernel_info_table`
  opcode contract is recoverable. Its kernel **names** are therefore *[MED/INFERRED]*
  (assumed congruent with CAYMAN/MARIANA from the matching opcode table).
* **SUNDA_0** (newest, `0xd308`): entry `0x010000c8`; decodes cleanest of all (its
  `.xt.prop` names new kernels not in CAYMAN — `ic_util::send_gather_request` (op 104),
  `iota_kernel` (op 126), `embedding_update` (op 121)).

> **LIMITATION (reported honestly).** This is densely-scheduled FLIX VLIW with literal
> pools interleaved in `.text` and hand-written boot stubs in the first `0x100` bytes
> (uncovered by property records). A raw linear sweep decodes the recognizable FLIX
> bundles correctly but loses bundle-sync across some literal/selector-byte boundaries,
> rendering those spans as `.byte`. This is a disassembler-mode artifact, **not** a data
> defect, and does not affect the inventory or the ELF-identity proof.

---

## 7. Carve recipe (annotated pseudocode)

Both embedding mechanisms reduce to a getter→`(ptr,size)`→carve→ELF pipeline. Reproduced
faithfully; the carve bytes are never altered (only `/tmp` working copies get section
surgery for disassembly).

```sh
export XTENSA_SYSTEM=…/gpsimd_tools_tgz/tools/XtensaTools/config
export XTENSA_CORE=ncore2gp
OBJD=…/XtensaTools/bin/xtensa-elf-objdump   ; OBJCOPY=…/xtensa-elf-objcopy
INT=…/libnrtucode_internal.so               ; EXT=…/libnrtucode_extisa.so
```

```c
// ---- PART 1: internal.so, getter-driven (16 SO ELFs) ----
// For each *_EXTISA_<n>_SO_get accessor body, read its (blob_va, size):
//     lea  blob_va(%rip),%rax ; mov %rax,(%rdi) ; movq $size,(%rsi) ; ret
// .rodata file_offset == VA (first R LOAD maps file 0 -> VA 0), so:
for (auto &g : SO_getters)                         // 16 of them
    carve(INT, /*foff=*/g.blob_va, /*len=*/g.size) -> elf;   // dd skip=blob_va count=size
// (the matching *_JSON_get always yields a 0x20 stub — the dummy JSON)

// ---- PART 2: extisa container, magic-scan-driven (13 sub-images) ----
for (off in find_all(EXT, "\x7fELF"))              // 14 hits: skip the host @0
    if (off != 0) {
        hdr = read_elf32_header(EXT, off);
        size = hdr.e_shoff + hdr.e_shentsize * hdr.e_shnum;   // exact ELF length
        carve(EXT, off, size) -> elf;
        json = carve(EXT, off - 0x20, 0x20);       // the preceding dummy JSON
        // (SUNDA: real 0x6c0 JSON @ 0x920fa0, not 0x20)
    }

// ---- verify each carve ----
assert(elf[0:4] == "\x7fELF" && elf[4]==1 /*ELFCLASS32*/ && elf[5]==1 /*LE*/);
assert(u16le(elf, 18) == 94 /*EM_XTENSA*/);
// byte-identity cross-check: internal.so CAYMAN_0 blob == container CAYMAN_0 ELF
assert(sha256(INT[0x2ef7e0 : +0xa260]) == sha256(EXT[0x5b0cc0 : +0xa260]));   // 910d41c3…
// Q7-code proof:
//   $OBJCOPY -O binary --only-section=.text elf t.bin
//   $OBJD -D -b binary -m xtensa --adjust-vma=0x01000000 t.bin   // FLIX auto-detect
```

---

## 8. Four-view byte-identity (one corpus, four packagings)

For CAYMAN_0 (representative) the **same firmware bytes** (sha256 `910d41c3ededce67…`)
appear in four independent packaging views, all verified identical:

1. `libnrtucode_extisa.so` container ELF (carved `@foff 0x5b0cc0, sz 0xa260`).
2. `libnrtucode_internal.so` getter blob (`.rodata[0x2ef7e0 : +0xa260]`) — `cmp` == (1).
3. `libnrtucode.a` member `img_CAYMAN_Q7_POOL_PERF_EXTISA_0_SO_contents.c.o` `.rodata`
   (the archive carries **12 `*_SO_contents.c.o` + 12 `*_JSON_contents.c.o`** —
   CAYMAN/MARIANA/MARIANA_PLUS only; no MAVERICK, no SUNDA).
4. `libnrtucode.so` front lib carries the same 12 dummy JSONs (`rg dummy_message` = 12).

So the EXTISA Q7 SO firmware is **one byte-exact corpus surfaced four ways**; the dummy
JSON travels with it as an inert sidecar in every view.

---

## 9. Confidence ledger + honest gaps

**HIGH / OBSERVED (binary-exact):**

* 16 internal getters + 13 container sub-images = 29 carves, 13 unique; `ELFCLASS32` /
  LE / `e_machine=94` confirmed on `ET_EXEC` (CAYMAN/MARIANA/SUNDA) and `ET_DYN`
  (MAVERICK) carves; all sha256 match the carve manifest.
* JSON-decoy verdict (content + hardcoded `movq $0x20` + no-parser + SO `kernel_info_table`)
  and the SUNDA `JSON ⊆ SO` reconciliation (SO-only op `0x44`).
* Blob→getter binding (16 SO + 16 JSON text fns + 2 SUNDA weak-undef; img-ptr/size; per-gen
  `*_libs` tables from relocs; coretype bitmask `0x2020202000` + `cmp $6`); four-view
  byte-identity for CAYMAN_0.
* Embedding format = raw uncompressed JSON-delimited ELF (no prelink/compression);
  reverse engine order; container geometry.
* Q7-code proof (CAYMAN_0/MAVERICK_0/SUNDA_0 decode to real `ncore2gp` FLIX + IVP TIE).

**MED / INFERRED:**

* "Dummy JSON = vestigial build artifact rather than deliberate anti-RE decoy" — the
  *observable* (inert, never parsed) is HIGH; the *intent* is MED.
* coretype `{6,13,21,29,37}` ↔ silicon-part binding (SUNDA=NC-v2 … MAVERICK=NC-v5) is
  inferred from spacing + toolchain bump, not proven in-binary.
* MAVERICK kernel **names** (its images are stripped — only the opcode contract is
  recoverable; names assumed congruent with CAYMAN/MARIANA).

**LOW / NOT CLAIMED HERE:**

* Per-spec dtype semantics of the `0xF0` extended sub-ops / CPTC decode family
  (`lib3` op `0xe4` + `0xF0` spec 7) — see [kernel-info-table.md](../firmware/pool/kernel-info-table.md)
  and the per-kernel pages.
* The large inter-block **base**-POOL firmware regions were located but not carved here
  (out of EXTISA scope; [image-catalog-index.md](image-catalog-index.md)).
