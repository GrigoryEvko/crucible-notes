# NCFW IRAM Images + Host Selector

This page documents the **NCFW firmware image carve** — the four raw Xtensa-LX
IRAM(code)+DRAM(data) memory images the host library `libncfw.so` ships in its
`.rodata`, one per NeuronCore generation — and the **host selector**
`libncfw_get_image(arch_id)` that hands a `(iram, dram)` pointer/size tuple to
the device-init path. It is the *data-asset* companion to
[The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md), which owns the
*ISA / decode / dispatch-spine* story; this page owns **where the bytes are, what
their bounds and identities are, how the host picks one, and why there is no
fifth image**. The NCFW core is a **scalar Xtensa-LX** management processor — *not*
the Vision-Q7 "Cairo" FLIX DSP that runs custom-op kernels — and that distinction
is re-grounded against the blob bytes below.

> **GOTCHA — this is the NCFW management core, not the GPSIMD Q7 datapath.** Two
> different Xtensa cores live in the GPSIMD estate. The user-kernel **datapath**
> core is the Vision-Q7 NX (`ncore2gp`, 512-bit SIMD, real FLIX) whose ucode
> ships *separately* in `libnrtucode_extisa.so`. The core *here* is its quieter
> sibling: a scalar Xtensa-LX **control** core whose firmware ships as these four
> images in `libncfw.so`. Two firmwares, two cores, two selectors, two doorbells.
> Do not cross the wires. See the [scalar-LX core page](../../uarch/ncfw-lx-core.md)
> for the full ISA evidence.

**Provenance & confidence.** Every fact below is read from the shipped host library
`libncfw.so` (sha256 `598920d7…`) with stock binutils (`readelf` / `nm` / `objdump`
/ `sha256sum`) and a Python ELF/byte reader, plus a native Cadence
`xtensa-elf-objdump` (`XTENSA_CORE=ncore2gp`) run on the carved blob. Lawful
interoperability reverse engineering (DMCA 17 U.S.C. 1201(f)); no vendor source
snapshot consulted. The page default is `[HIGH/OBSERVED]`; claims that depart from
it carry an explicit tag, per the
[Confidence & Walls Model](../../reference/confidence-model.md): `OBSERVED` = a
byte / size / symbol / disassembler output read from the binary; `CARRIED` =
OBSERVED in a cited prior carve and reused; `INFERRED` = reasoned over those.
Callouts: **QUIRK** (counter-intuitive but real), **GOTCHA** (a reimplementation
trap), **NOTE** (orientation), **CORRECTION** (overturns a prior reading).

> **THE v5 / MAVERICK WALL — read before any v5 claim.** `libncfw` ships **exactly
> four** NCFW generations (v2/v3/v4/v4_plus). **There is no MAVERICK (v5) NCFW
> image in this binary** — every byte, symbol, and selector arm below proves its
> absence. Any statement about the *interior* of a v5 NCFW image would be
> fabrication: the file does not contain one. v5 is referenced here only to mark
> the wall, never as observed firmware. See [§5](#5-the-v5--maverick-wall-no-ncfw-image-ships)
> and the [MAVERICK profile](../../generations/maverick-profile.md). `[WALL]`

---

## 0. Target binary identity

| Field | Value | How verified |
|---|---|---|
| Path | `…/aws-neuronx-runtime-lib_2.31.24.0-0b044f4ce_amd64/opt/aws/neuron/lib/libncfw.so` | `fd --no-ignore` |
| Size | **615640** bytes | `stat -c %s` |
| SHA-256 | `598920d743762c03b3007c089829c02d0095408bf431fa3533e508c5f0aa3e49` | `sha256sum` |
| ELF | ELF64 LSB DYN, x86-64, **not stripped** (HOST library) | `file` |
| SONAME | `libncfw.so.2.31.1.0.cf13a49f` | `readelf -dW` |
| BuildID | `a98f8e1ca2294582835310c3a1092e0a5e500db5` | `readelf -nW` |
| `.text` | PROGBITS, VMA==off==`0x10c0`, size `0x63991` | `readelf -SW` |
| `.rodata` | PROGBITS, **VMA==off==`0x65000`**, size `0x2c8e4` | `readelf -SW` |

All eight firmware payloads live in `.rodata`, where **VMA equals file offset**, so
each symbol's address *is* its file offset and every blob is a direct
`f[off : off+size]` slice — no `.data` VMA↔file delta correction is needed (that
trap applies only to `.data`-resident structs, not these). The four `.c` source
strings in `.rodata` are **`sunda.c` / `cayman.c` / `mariana.c` / `mariana_plus.c`**
plus `ncfw_bins.c` (the blob-defining TU), `libncfw.c`, `ctx_log.c` — **exactly
four codenames, no `maverick.c` / `v5.c`.**

---

## 1. The image inventory — four IRAM + four DRAM blobs

Each generation ships an **IRAM** (code) blob and a **DRAM** (data) blob, each
followed by a `u32 *_size` word that the selector loads. The size word sits
*immediately at the blob's end* (`start + size`); the **carved length, the `u32`
size word, and the `nm` symbol size all agree** for every blob — no padding slack.
Read from `nm -S`, then carved and hashed with Python:

| gen | codename | NC | kind | file-off | size (B / hex) | `u32` szword | tz | sha256 (carved) |
|---|---|---|---|---|---|---|---|---|
| v2 | SUNDA | NC-v2 | IRAM | `0x06a140` | 43232 / `0xa8e0` | 43232 ✓ | 10 | `e379980b7ec3f2fe…` *(2.2× larger)* |
| v2 | SUNDA | NC-v2 | DRAM | `0x066a60` | 14016 / `0x36c0` | 14016 ✓ | — | `ca01951124e505b6…` |
| v3 | CAYMAN | NC-v3 | IRAM | `0x079860` | 19392 / `0x4bc0` | 19392 ✓ | 6 | `d7bc8b814b03c1f0…` |
| v3 | CAYMAN | NC-v3 | DRAM | `0x074a40` | 19968 / `0x4e00` | 19968 ✓ | — | `2418ab0f6350ce93…` |
| v4 | MARIANA | NC-v4 | IRAM | `0x083260` | 19488 / `0x4c20` | 19488 ✓ | 22 | `ed8eed3429da3834…` |
| v4 | MARIANA | NC-v4 | DRAM | `0x07e440` | 19968 / `0x4e00` | 19968 ✓ | — | `1c3ac5f445865844…` |
| v4+ | MARIANA_PLUS | NC-v4+ | IRAM | `0x08ccc0` | 19488 / `0x4c20` | 19488 ✓ | 22 | `abc4d4521dd857ab…` |
| v4+ | MARIANA_PLUS | NC-v4+ | DRAM | `0x087ea0` | 19968 / `0x4e00` | 19968 ✓ | — | `1c3ac5f445865844…` *(== MARIANA DRAM)* |

(`tz` = trailing-zero pad bytes on the IRAM blob — 6–22 only; these are **dense
code images, not page-/BSS-padded**.) The `.rodata` layout per generation is
`[dram_blob][dram_size u32 + pad to 0x20][iram_blob][iram_size u32 + pad]`. The
codename↔NC binding is per the committed
[Codename ↔ Generation Map](../../generations/codename-generation-map.md):
SUNDA=NC-v2, CAYMAN=NC-v3, MARIANA=NC-v4, MARIANA_PLUS=NC-v4+, MAVERICK=NC-v5.
`[HIGH/OBSERVED for inventory; codename↔NC HIGH/CARRIED]`

Three structural facts fall straight out of the table:

- **MARIANA and MARIANA_PLUS share a byte-identical DRAM** (both sha256
  `1c3ac5f4…`, md5 `7ff55158…`) — verified by carving both and comparing.
- **SUNDA's IRAM is ~2.2× larger** (43232 B vs ~19.5 KB) — it is the older,
  structurally-different monolith (the others are a later refactored lineage,
  [§3](#3-the-v4_plus-identity-and-the-shared-lineage)).
- **All four are RAW Xtensa images, not ELF.** No `e_machine`, no ELF header —
  the first bytes are live `j` reset vectors. This contrasts with the GPSIMD
  nrtucode EXTISA blobs, which *are* embedded ELF32-Xtensa.

> **NOTE — DRAM is a table image, IRAM is pure code.** The IRAM blobs contain no
> extractable ASCII (`strings` returns only dense-instruction noise); all
> human-readable NCFW log strings live HOST-side in the `*_ncfw_ctx_log` decoders,
> not in the device image. The **DRAM** blob is a flat table of IRAM code
> addresses: `v4_dram[0..4]` reads as the `u32 LE` quad
> `{0x1399, 0x13b1, 0x13c5, 0x1399}` — the dispatch (A) command/event vector (slot
> 3 == slot 0 = the unknown-command guard) — and `v4_dram+0xB0` is the 12-entry
> `algo_type` jump table `{0x3c38, 0x3c35, 0x3c2e, 0x48f0, …}`. The
> [main dispatch loop](main-dispatch-loop.md) page decodes those tables; here they
> only confirm the kind split (DRAM=data, IRAM=code).

### 1.1 The raw-image head — reset vectors at the IRAM base

There is **no load-address header**: the carved blob *is* the IRAM-resident image,
with the Xtensa XEA2 vector table at offset `0x0` and code following. The first
twelve bytes are two back-to-back `j` (opcode `0x06`) reset/window vectors; the
`j` immediate is the only head difference between the two lineages:

| lineage | head bytes | reset `j` @ `0x0` | secondary `j` @ `0x6` |
|---|---|---|---|
| v2 / v3 | `06 76 00 00 00 00  86 77 00 00 00 00` | `j 0x1dc` | `j 0x1e8` |
| v4 / v4+ | `06 7d 00 00 00 00  86 7e 00 00 00 00` | `j 0x1f8` | `j 0x204` |

The `j` targets were decoded by hand (`op0=6`, 18-bit signed offset in bits
`[23:6]`, `target = PC+4+off`) and confirm the `+0x1c` (28-byte) shift in v4/v4+ —
caused by a D-cache-invalidate (`dii`) loop inserted in early boot that pushes every
inline handler 28 bytes later. The **UserException prologue is pinned at `0x6c`**
(bytes `20 ee 03` = `rsr.excvaddr a2`) **byte-identical across all four** — it must
align with the Xtensa VECBASE convention. The boot-stage-2 entry (`0x1018`) is
byte-identical for ~60 bytes across all four **except** a per-generation marker byte.

> **CORRECTION — the per-gen marker byte is at `0x1022`, not `0x1021`.** A prior
> carve placed the boot-stage-2 generation marker at IRAM `0x1021`. Byte-exact
> reading shows `[0x1021] = 0x14` (an opcode byte, **identical** across
> all four); the **differing** byte is one further on, at **`0x1022`**:
> SUNDA=`0x08`, CAYMAN=`0x09`, MARIANA=`0x0a`, MARIANA_PLUS=`0x0a`. (SUNDA also
> differs at `0x1026`: `0x40` vs `0x00`.) The prior "`14 0X 00` at `0x1021`" was
> reading the marker as the *middle* byte of a three-byte span anchored at `0x1021`;
> the gen-specific nibble is the byte at `0x1022`. The PRID self-id gates at `0x10ce`
> and `0x1118` (`50 eb 03` = `rsr.prid a5`) are **byte-identical across all four** —
> the master/worker per-core split is generation-invariant.

---

## 2. The host selector `libncfw_get_image` (`.text 0x1179`)

The image is chosen by the **exported** (`nm -D` → `T`) host function
`libncfw_get_image`, fully disassembled (`0x1179`–`0x12f9`, 385 B). It
takes `arch_id` in `edi` and an out-pointer in `rsi`, and writes a four-slot struct.
The out-struct offsets are *proven from the actual stores* — e.g. the v2 arm writes
`mov [rax+0x10],rdx` (dram_ptr) / `mov [rax+0x18],rdx` (dram_size) / `mov [rax],rdx`
(iram_ptr) / `mov [rax+0x8],rdx` (iram_size):

```c
// Recovered signature + out-struct (offsets proven from the per-arm stores):
//   struct ncfw_img { void *iram; u64 iram_size; void *dram; u64 dram_size; };
//                       +0x00       +0x08          +0x10       +0x18
// @ libncfw.so .text 0x1179  (exported; arch_id in edi, out in rsi)
int libncfw_get_image(uint32_t arch_id, struct ncfw_img *out)
{
    if (out == NULL)                                  // 0x1188: cmp [rbp-0x10],0  / je
        return 22;                                    // 0x118f: EINVAL (0x16)

    // Binary-search-ordered cmpl ladder on arch_id:
    if (arch_id == 0x1c) {                            // 0x1199: cmp 0x1c ; je 0x12a7
        out->iram = &v4_plus_ncfw_iram_bin;           //   lea 0x8ccc0
        out->iram_size = v4_plus_ncfw_iram_bin_size;  //   u32 @ 0x918e0 = 19488
        out->dram = &v4_plus_ncfw_dram_bin;           //   lea 0x87ea0
        out->dram_size = v4_plus_ncfw_dram_bin_size;  //   u32 @ 0x8cca0 = 19968
        return 0;
    }
    if (arch_id >  0x1c)  return 2;                    // 0x11a3/0x11a7: cmp 0x1c ; ja 0x12ec
                                                       //   <-- ANY arch_id > 0x1c (incl. 0x24 MAVERICK) → unsupported
    if (arch_id == 0x14) { *out = v4 (MARIANA);     return 0; }   // 0x11ad: cmp 0x14 ; je 0x1262
    if (arch_id >  0x14)  return 2;                    // 0x11b7/0x11bb: cmp 0x14 ; ja 0x12ec
    if (arch_id == 0x05) { *out = v2 (SUNDA);       return 0; }   // 0x11c1: cmp 0x05 ; je 0x11d2
    else if (arch_id == 0x0c) { *out = v3 (CAYMAN); return 0; }   // 0x11c7: cmp 0x0c ; je 0x121a
    else return 2;                                     // 0x11cd: jmp 0x12ec (unknown arch_id)
}
```

The `lea rip+…` displacements in the disassembly resolve to the **exact blob
symbols** — `# 6a140 <v2_ncfw_iram_bin>`, `# 79860 <v3_ncfw_iram_bin>`,
`# 83260 <v4_ncfw_iram_bin>`, `# 8ccc0 <v4_plus_ncfw_iram_bin>` (and the four DRAM
counterparts) — so the selector→blob binding is byte-grounded, not inferred. The
size loads (`mov eax,[size_sym]`) zero-extend a `u32`.

### 2.1 The arch_id → image → coretype map

`arch_id = coretype − 1`, with a **+8 generation stride** `[HIGH/OBSERVED for the
four shipped arms; the v5 row INFERRED]`:

| arch_id | blob | codename | coretype | NC | check |
|---|---|---|---|---|---|
| `0x05` (5) | v2 | SUNDA | 6 | NC-v2 | 6−1 = 5 ✓ |
| `0x0c` (12) | v3 | CAYMAN | 13 | NC-v3 | 13−1 = 12 ✓ |
| `0x14` (20) | v4 | MARIANA | 21 | NC-v4 | 21−1 = 20 ✓ |
| `0x1c` (28) | v4+ | MARIANA_PLUS | 29 | NC-v4+ | 29−1 = 28 ✓ |
| `0x24`* (36) | *(none)* | *MAVERICK* | 37 | NC-v5 | **no arm — [§5](#5-the-v5--maverick-wall-no-ncfw-image-ships)** |

The `0x24`* row is **INFERRED** from the stride; it is *never* OBSERVED — there is
no `cmp 0x24` and no v5 blob (the row exists only to show where the ladder *stops*).

### 2.2 A second, independent arch_id anchor — `libncfw_ctx_log`

The exported `libncfw_ctx_log` (`.text 0x1309`) runs the **same** `cmpl`
ladder (`cmp 0x1c / 0x14 / 0x05 / 0x0c`) but routes to **codename-named** log
decoders — `call sunda_ncfw_ctx_log (0x1a12b)`, `cayman… (0x32ed2)`,
`mariana… (0x4bc79)`, `mariana_plus… (0x64a20)`. Two dispatchers keyed on the same
four arch_ids, one routing to `v#_*_bin` images and the other to codename functions,
independently nail the `arch_id ↔ codename` binding **and** the count of exactly
four. The deep ISA-naming/arch_id reconciliation is consolidated
in the [LX-ISA / arch_id synthesis](lx-isa-naming-archid-synthesis.md) page.

> **NOTE — `libncfw_get_version` returns the *library* ABI, not the firmware
> version.** The third exported function, `libncfw_get_version` (`0x12fa`), is a
> two-instruction stub: `mov eax,0x2 ; ret`. It returns the constant **2** — the
> `libncfw` host-library ABI version — and has nothing to do with the embedded
> firmware. No IRAM blob carries a semver / git-hash / build-date string at all
> (`strings` on each returns only instruction noise). **The stable per-image
> fingerprint is `arch_id` + the sha256/md5 in [§1](#1-the-image-inventory--four-iram--four-dram-blobs).**

---

## 3. The v4_plus identity, and the shared lineage

The open question on these images is *what `v4_plus` actually is*. Byte-diffing the
two 19488-byte IRAM blobs settles it:

| pair | shared prefix | shared suffix | bytes differ / len | identical |
|---|---|---|---|---|
| v4 vs v4+ | `0x1190` (4496 B) | `0x173` (371 B) | 5813 / 19488 = 29.8% | **70.2%** |
| v2 vs v3 | `0x79` (121 B) | 6 B | 15103 / 19392 = 77.9% | 22.1% |
| v3 vs v4 | 1 B | 6 B | 9173 / 19392 = 47.3% | — *(prefix-shift de-align)* |

**`v4_plus` (MARIANA_PLUS) is a code-only handler-table rewrite of `v4`
(MARIANA):** same 19488-byte size, **byte-identical DRAM** (sha `1c3ac5f4…`), an
identical 4496-byte boot/vector prefix, and an identical 371-byte init/idle/panic
suffix — only the engine-dispatch *middle* `[0x1190 … 0x4aad)` (14621 B, 39.8% of
which actually differs) is rewritten. It is a feature-flag refresh of the same
MARIANA (NC-v4) silicon family, **not** a new generation.

> **QUIRK — v3↔v4 "share only 1 byte" is a de-alignment artifact, not a redesign.**
> The literal common prefix of v3 vs v4 collapses to **one** byte, yet their
> *layout* is the same (same vector table, same boot pattern, same handler
> structure). The cause is the 28-byte `dii`-loop insertion in v4's early boot,
> which shifts everything downstream so a naive byte-prefix compare de-aligns
> immediately. v2↔v3, by contrast, **genuinely** share only the first 121 bytes —
> the packed window-vector skeleton — then diverge structurally (SUNDA monolith vs
> CAYMAN refactor). So the lineage is: **one common firmware base across
> CAYMAN/MARIANA/MARIANA_PLUS**, with SUNDA an older, 2.2×-larger ancestor sharing
> only the vector prologue.

The size-delta story in one line: `v2 SUNDA 43232 B → v3 CAYMAN 19392 B`
(−55%, monolith→refactor) `→ v4 MARIANA 19488 B` (+96 B, +`dii`/D-cache-writeback)
`→ v4+ MARIANA_PLUS 19488 B` (±0, handler-middle rewrite, 70.2% byte-identical to
v4). The shared **end-of-image stub** `36 81 00 … 1d f0` (`entry a1,…; <body>;
retw.n`) is present verbatim at the tail of all four.

---

## 4. Why the core is scalar Xtensa-LX — decoded against the blob

The NCFW core is a **scalar Xtensa-LX control core, not the Vision-Q7 FLIX DSP**.
The full ISA proof lives on the [scalar-LX core page](../../uarch/ncfw-lx-core.md);
two pieces of it are re-grounded *here, on the carved bytes*, because they bound how
a reimplementer must treat these images.

**(a) The width profile is exclusively 2-byte and 3-byte.** A scalar-LX length walk
over each carved IRAM (op0 `0..7` ⇒ 3-byte 24-bit core ops; op0 `8..d` ⇒ 2-byte
density ops; op0 `e/f` ⇒ 3-byte resync) finds **~22–24% 2-byte / ~76–78% 3-byte**
across all four — the textbook Xtensa-LX shape. There are **no genuine wide
bundles.** A real FLIX core's body would be dominated by 8-/16-byte instructions;
NCFW's is not.

**(b) The native Vision-Q7 disassembler mis-decodes the LX stream.** The only
Xtensa config that ships anywhere in the corpus is `ncore2gp` (the Vision-Q7
datapath core) — there is exactly one `core-isa.h`, and the native
`xtensa-elf-objdump` registers no other core. Pointing it (`XTENSA_CORE=ncore2gp`)
at the **real v4 IRAM** decodes the base op `rsr.excvaddr a2` @ `0x6c` correctly but
spews **impossible-for-a-control-core** Vision-DSP opcodes around it — verbatim at
`0x76`: `ivp_scatterw` (a Cairo vector-scatter), plus `l32dis.it`,
`s32stk`, `s32dis.h`, `orb b1,b6,b0`, and `.byte` resyncs. Those `ivp_*`/scatter
opcodes **cannot** exist on a scalar management core; they are the wrong-config
artifact — the `ncore2gp` FLIX length table greedily eating `op0=e/f` operand bytes
as 16-/8-byte Vision bundles. This is the entire mechanism of the spurious
"~26–28% FLIX" earlier readings measured.

> **GOTCHA — decode these blobs with the LX rule, not the Vision FLIX rule.** For
> the NCFW management core, treat `op0 ∈ {e,f}` as a **3-byte resync width, not a
> FLIX bundle leader**, and resync at `retw.n` (`1d f0`). Under that scalar
> `(e3,f3)` rule the `retw.n` anchors land far better than under the `ncore2gp`
> `(e16,f8)` Vision rule, on the carved blobs:

| gen | scalar `(e3,f3)` | `ncore2gp` `(e16,f8)` |
|---|---|---|
| v3 | **90 / 133** retw.n aligned | 66 / 133 |
| v4 | **101 / 134** | 74 / 134 |
| v4+ | **100 / 134** | 69 / 134 |
| v2 | 26 / 37 | 27 / 37 *(small-sample monolith; see uarch page §1.5)* |

> The honest limit: **no NCFW LX disassembler config ships** (no params / `core-isa.h`
> / `.tie` / `.flix` for it), so the exact `op0=e/f` leader ops cannot be *named* —
> only the base-ISA / windowed-vector skeleton decodes reliably. There is **no real
> FLIX layer to recover**; do not budget a FLIX issue port for this core. The full
> debunk + four-way proof is on the [scalar-LX core page](../../uarch/ncfw-lx-core.md).
> `[HIGH/OBSERVED for the resync table; the un-nameable leaders are the residual]`

---

## 5. The v5 / MAVERICK wall — no NCFW image ships

**There is no MAVERICK (v5) NCFW image in `libncfw.so`.** This is not an absence of
evidence — it is evidence of absence, on **five** independent byte-grounded lines
`[HIGH/OBSERVED]`:

1. **No v5 symbol exists.** `nm` over the binary lists exactly the four
   `{v2,v3,v4,v4_plus}_ncfw_{iram,dram}_bin[_size]` families — `rg -i
   'maverick|v5_|v6_'` returns **nothing**.
2. **The selector stops at `0x1c`.** The `get_image` ladder
   ([§2](#2-the-host-selector-libncfw_get_image-text-0x1179)) is `cmp 0x1c ; ja
   12ec` → any `arch_id > 0x1c` (including `0x24` = 36 = MAVERICK) falls **straight**
   to `mov eax,2 ; ret`. There is **no `cmp 0x24`** anywhere in `.text`.
3. **The `.rodata` blob region is physically closed.** `v4_plus_ncfw_iram_bin_size`
   ends at `0x918e4`, and the very next symbol (`nm -n`) is the compiler-generated
   `__GNU_EH_FRAME_HDR` @ `0x918e4` (the `.eh_frame_hdr` section base). There is
   literally no room for a fifth image — the firmware-blob region butts directly
   against the EH frame.
4. **No v5 string.** `strings -i maverick|v5` over the whole binary returns
   nothing; the four `.c` source strings are `sunda/cayman/mariana/mariana_plus.c`,
   no `maverick.c`.
5. **Only four `ctx_log` codename decoders.** `libncfw_ctx_log`
   ([§2.2](#22-a-second-independent-arch_id-anchor--libncfw_ctx_log)) routes to
   exactly four codename functions — no fifth.

> **NOTE — this matches the sibling walls, not a one-off omission.** `libncfw` tops
> out at MARIANA_PLUS **identically** to the runtime microcode container
> (`libnrtucode.so` also omits coretype-37). MAVERICK *is* a known codename — it
> surfaces in the internal-twin `libnrtucode_internal.so` accessor names and the v5
> arch-ISA headers — but **no shipped 2.31.x runtime provisions a coretype-37
> firmware image.** Whatever drives the v5 collective path does so differently; that
> story (and the v5 epistemic wall in general) is the
> [MAVERICK profile](../../generations/maverick-profile.md). For NCFW specifically,
> the **definitive count is four.** `[HIGH/OBSERVED for the libncfw absence; the
> "driven differently" claim CARRIED/INFERRED]`

---

## 6. Reimplementer's checklist (the carve + selector obligations)

To rebuild a Vision-Q7-compatible GPSIMD engine, the **image-asset** layer obligates
you to (the *core* obligations — windowed ABI, `0x24` handler, dispatch spine — are
on the [scalar-LX core page](../../uarch/ncfw-lx-core.md) §7):

1. **Ship one raw IRAM+DRAM image pair per generation** in a host-readable
   `.rodata`-equivalent region, each followed by its own `u32` size word at
   `start+size`; no ELF header, no load-address header. `[HIGH]`
2. **Lay the IRAM image vectors-first**: `j reset_body` @ `0x0`, `j window/exc` @
   `0x6`, the windowed-overflow handler @ `0x24`, the UserException prologue
   (`rsr.excvaddr`) **pinned** @ `0x6c`. `[HIGH]`
3. **Provide a `get_image(arch_id, out)` selector** with a binary-search `cmpl`
   ladder writing `{iram, iram_size, dram, dram_size}` at out offsets
   `{0x00, 0x08, 0x10, 0x18}`; return `22` (EINVAL) on NULL out, `2` on unsupported
   arch_id, `0` on success. `arch_id = coretype − 1`. `[HIGH]`
4. **Stop the ladder at your top generation** — there is no fall-through default
   image; an out-of-range arch_id returns `2`. (This is exactly why MAVERICK gets no
   image.) `[HIGH]`
5. **DMA each image to the device IRAM/DRAM base** via the BAR2 aperture; the base
   is set by the upload path, *not* embedded in the blob (the in-image offsets are
   PC-relative; some `l32r` literals point to an off-image `0xfffeXXXX` mask-ROM the
   static image references but does not contain). `[MED — exact device base
   INFERRED; not in `libncfw.so`]`

---

## 7. Cross-references

- [The NCFW Scalar-LX Management Core](../../uarch/ncfw-lx-core.md) *(Part 6)* — the
  full ISA evidence: the `WindowOverflow8` handler at `0x24`, the windowed XEA2 ABI
  census, the standard-LX SR set, the FLIX-mis-decode debunk, and the dispatch
  spine. **The authoritative core characterization.**
- [NCFW DRAM Images + `ctx_log` Decoder](ncfw-dram-ctx-log.md) *(Part 10)* — the
  DRAM data tables (the `soc_addr` integers, the (A) vector, the `algo_type` table)
  and the host-side codename log decoders.
- [NCFW Main Dispatch Loop](main-dispatch-loop.md) *(Part 10)* — the (A) command
  vector, the `DRAM+0xB0` jump table, and the `waiti 15` idle loop at full depth.
- [NCFW LX-ISA / arch_id-Diff / Orchestration Synthesis](lx-isa-naming-archid-synthesis.md)
  *(Part 10)* — the consolidated arch_id ↔ codename ↔ silicon reconciliation.
- [Codename ↔ Generation Map](../../generations/codename-generation-map.md)
  *(Part 6)* — the authoritative `(arch_id, coretype, NC, codename)` join and the
  MAVERICK `arch_id 36` fiction resolved.
- [MAVERICK (NC-v5) Profile](../../generations/maverick-profile.md) *(Part 6)* — the
  v5 epistemic wall and why no shipped runtime (`libncfw` included) carries a
  coretype-37 image.

---

## 8. Verification ledger

| # | claim | how grounded | verdict |
|---|---|---|---|
| 1 | Four IRAM + four DRAM blobs; carved size == `u32` szword == `nm` size for all eight | `nm -S` + Python carve + `<I` unpack: all eight `szword==size` True; sha256/md5 match the cited carves | **HIGH/OBSERVED** |
| 2 | `get_image` selector: `cmpl {0x1c,0x14,0x05,0x0c}`, out-struct `{iram@0,iram_size@8,dram@16,dram_size@24}`, EINVAL=22 / unsupported=2 | `objdump -d 0x1179`: stores `mov [rax+{0,8,10,18}]`, `lea` to the eight `v#_*_bin` symbols, `0x118f mov eax,0x16` | **HIGH/OBSERVED** |
| 3 | NCFW core is **scalar Xtensa-LX, not Vision-Q7 FLIX** | scalar walk = 22–24% 2B / 76–78% 3B (no wide bundles); `ncore2gp` objdump on real v4 IRAM emits `ivp_scatterw`/`s32stk` artifacts; scalar `(e3,f3)` retw.n resync beats `(e16,f8)` (v4: 101 vs 74 /134) | **HIGH/OBSERVED** |
| 4 | **MAVERICK (v5) ships no NCFW image** | no v5 symbol (`nm`), `ja 0x1c→ret 2` with no `cmp 0x24`, region closes at `0x918e4` into `__GNU_EH_FRAME_HDR`, zero v5 string, four `ctx_log` codenames | **HIGH/OBSERVED** |
| 5 | `v4_plus` = code-only handler-rewrite of `v4` (same size, byte-identical DRAM, 70.2% identical IRAM) | byte-diff: shared prefix 0x1190 + suffix 0x173, 5813/19488 = 29.8% differ; v4 DRAM == v4+ DRAM (sha `1c3ac5f4…`) | **HIGH/OBSERVED** |

### Corrections recorded on this page

1. **Per-gen boot marker byte is at IRAM `0x1022`, not `0x1021`.** Byte-exact read:
   `[0x1021]=0x14` identical all four; the gen-specific byte (`08/09/0a/0a`) is at
   `0x1022` (§1.1 CORRECTION).
2. **v4↔v4+ first divergence is at byte `0x1190` (4496, 0-indexed).** A cited carve
   wrote "byte 4497"; the byte-exact first-difference index is **4496** (the images
   share an identical 4496-byte prefix, bytes `0..4495`). The 1-larger figure was a
   1-indexed count.
